"""Execution service for the BTC EMA pullback strategy.

Flow (one position at a time, never added to):

    IDLE -> SIGNAL_DETECTED -> PRE_TRADE_VALIDATION -> ENTRY_SUBMITTED
         -> ENTRY_FILLED -> PROTECTION_ORDERS_SUBMITTED -> POSITION_OPEN
         -> EXIT_FILLED -> TRADE_RECORDED -> IDLE

Error phases: POSITION_RECONCILIATION, PROTECTION_FAILED, ERROR_RECOVERY.

LIVE orders reuse the bot's IdempotentOrderGateway (deterministic
clientOrderId per 15m signal, ambiguous-response recovery, partial-fill
remainder cancel) and BinanceAlgoOrderGateway (STOP_MARKET /
TAKE_PROFIT_MARKET, closePosition=true, workingType from config).  Each
network keeps its own state store so testnet records never mix with the
main bot's mainnet records.  DRY_RUN uses real candles, prices and trading
rules but never sends an order.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from trading_safety.binance_algo_gateway import AlgoLookupStatus, BinanceAlgoOrderGateway
from trading_safety.order_gateway import IdempotentOrderGateway
from trading_safety.order_state import OrderState, SQLiteTradingStateStore, build_client_order_id

from .config import (
    CLIENT_ID_STRATEGY,
    NETWORK_TESTNET,
    STRATEGY_VERSION,
    TRADING_MODE_DRY_RUN,
    TRADING_MODE_LIVE,
    effective_trading_mode,
    normalize_btc_pullback_config,
    sizing_mode_for,
)
from .ledger import StrategyLedger, compute_stats, daily_entry_block_reason, trading_day_bounds
from .rules import D, TradingRulesError, protection_prices, rules_from_exchange_info, validate_and_normalize_order_quantity
from .signals import TIMEFRAME_MS, generate_signal

logger = logging.getLogger("btc_ema_pullback")

CCXT_SYMBOL = "BTC/USDT:USDT"
MARKET_ID = "BTCUSDT"
OPEN_ALGO_STATUSES = {"NEW", "PARTIALLY_FILLED", "WORKING", ""}

IDLE = "IDLE"
SIGNAL_DETECTED = "SIGNAL_DETECTED"
PRE_TRADE_VALIDATION = "PRE_TRADE_VALIDATION"
ENTRY_SUBMITTED = "ENTRY_SUBMITTED"
ENTRY_FILLED = "ENTRY_FILLED"
PROTECTION_ORDERS_SUBMITTED = "PROTECTION_ORDERS_SUBMITTED"
POSITION_OPEN = "POSITION_OPEN"
EXIT_FILLED = "EXIT_FILLED"
TRADE_RECORDED = "TRADE_RECORDED"
ERROR_RECOVERY = "ERROR_RECOVERY"
PROTECTION_FAILED = "PROTECTION_FAILED"
POSITION_RECONCILIATION = "POSITION_RECONCILIATION"


def _f(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _iso(ms):
    return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).isoformat()


def _market_key(symbol):
    return str(symbol or "").upper().split(":", 1)[0].replace("/", "")


def _position_qty(position):
    if not position:
        return 0.0
    for value in (position.get("contracts"), (position.get("info") or {}).get("positionAmt")):
        if value not in (None, ""):
            return abs(_f(value))
    return 0.0


def _position_side(position):
    side = str((position or {}).get("side") or "").lower()
    if side in {"long", "short"}:
        return side.upper()
    amount = _f(((position or {}).get("info") or {}).get("positionAmt"))
    return "LONG" if amount > 0 else ("SHORT" if amount < 0 else "")


def _position_entry(position):
    return _f((position or {}).get("entryPrice") or ((position or {}).get("info") or {}).get("entryPrice"))


class BtcEmaPullbackService:
    # Overridable by subclasses (other standalone BTC strategies).
    CLIENT_ID = CLIENT_ID_STRATEGY
    RUNTIME_SUBDIR = "btc_ema_pullback"
    LABEL = "BTC 눌림목"
    PROTECTION_KINDS = ("sl", "tp")
    EXIT_REASON_BY_KIND = {"sl": "SL", "tp": "TP"}
    normalize_config = staticmethod(normalize_btc_pullback_config)

    def __init__(
        self,
        *,
        config_getter,
        credentials_getter,
        exchange_factory,
        runtime_dir,
        notifier=None,
        main_network_getter=None,
        clock=time.time,
        sleep=asyncio.sleep,
        environ=None,
    ):
        self.config_getter = config_getter
        self.credentials_getter = credentials_getter
        self.exchange_factory = exchange_factory
        self.runtime_dir = Path(runtime_dir) / self.RUNTIME_SUBDIR
        self.notifier = notifier
        self.main_network_getter = main_network_getter
        self._clock = clock
        self._sleep = sleep
        self._environ = environ
        self._exchanges = {}
        self._ledgers = {}
        self._stores = {}
        self._gateways = {}
        self._rules = {}
        self._account_verified = {}
        self._cycle_lock = asyncio.Lock()
        self._last_notice = {}

    # ------------------------------------------------------------------ basics
    def config(self):
        return self.normalize_config(self.config_getter() or {})

    def mode(self, cfg=None):
        return effective_trading_mode(cfg or self.config(), self._environ)

    def current_network(self, cfg=None):
        """The bot's own /setup exchange (demo testnet or mainnet); None for Upbit."""
        if self.main_network_getter is None:  # tests / offline backtests
            return (cfg or self.config()).get("network") or NETWORK_TESTNET
        return self._main_network()

    @staticmethod
    def _signal_bar(signal):
        return int(signal.get("bar_open_ms") or signal.get("15m_bar_open_ms") or 0)

    def now_ms(self):
        return int(self._clock() * 1000)

    async def _call(self, fn, *args, **kwargs):
        return await asyncio.to_thread(fn, *args, **kwargs)

    async def _notify(self, text, key=None):
        if key:
            if self._last_notice.get(key) == text:
                return
            self._last_notice[key] = text
        if not self.notifier:
            return
        try:
            result = self.notifier(text)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            logger.exception("BTC pullback notification failed")

    def ledger(self, network):
        if network not in self._ledgers:
            self._ledgers[network] = StrategyLedger(
                self.runtime_dir / f"{network}_trades.sqlite3",
                self.runtime_dir / f"{network}_events.jsonl",
            )
        return self._ledgers[network]

    def store(self, network):
        if network not in self._stores:
            self.runtime_dir.mkdir(parents=True, exist_ok=True)
            self._stores[network] = SQLiteTradingStateStore(self.runtime_dir / f"{network}_orders.sqlite3")
        return self._stores[network]

    def has_credentials(self, network):
        creds = self.credentials_getter(network) or {}
        return bool(str(creds.get("api_key") or "").strip() and str(creds.get("secret_key") or "").strip())

    async def exchange(self, network):
        if network not in self._exchanges:
            creds = self.credentials_getter(network) or {}
            exchange = self.exchange_factory(network, creds)
            await self._call(exchange.load_markets)
            self._exchanges[network] = exchange
        return self._exchanges[network]

    async def gateway(self, network):
        if network not in self._gateways:
            exchange = await self.exchange(network)
            self._gateways[network] = (
                IdempotentOrderGateway(exchange, self.store(network)),
                BinanceAlgoOrderGateway(exchange),
            )
        return self._gateways[network]

    def _state_key(self, mode):
        return f"state:{mode}"

    def load_state(self, network, mode):
        state = self.ledger(network).get_state(self._state_key(mode), None)
        if not isinstance(state, dict):
            state = {}
        state.setdefault("phase", IDLE)
        state.setdefault("trade", None)
        state.setdefault("processed_signals", [])
        state.setdefault("last_evaluated_bar", 0)
        state.setdefault("reconciled", False)
        state.setdefault("last_error", "")
        return state

    def save_state(self, network, mode, state):
        state["processed_signals"] = list(state.get("processed_signals") or [])[-300:]
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.ledger(network).set_state(self._state_key(mode), state)

    def _set_phase(self, network, mode, state, phase, **extra):
        previous = state.get("phase")
        state["phase"] = phase
        state.update(extra)
        self.save_state(network, mode, state)
        if previous != phase:
            self.ledger(network).event("STATE", mode=mode, network=network, previous=previous, phase=phase)

    # ------------------------------------------------------------- market data
    async def trading_rules(self, network, cfg):
        cached = self._rules.get(network)
        if cached and self.now_ms() - cached[0] < int(cfg["rules_refresh_seconds"]) * 1000:
            return cached[1]
        exchange = await self.exchange(network)
        payload = await self._call(exchange.fapiPublicGetExchangeInfo)
        rules = rules_from_exchange_info(payload, MARKET_ID)
        self._rules[network] = (self.now_ms(), rules)
        self.ledger(network).event("RULES", network=network, **rules.as_log())
        return rules

    async def candles(self, network, cfg):
        exchange = await self.exchange(network)
        limit = int(cfg["candle_history"])
        trend, entry = await asyncio.gather(
            self._call(exchange.fetch_ohlcv, CCXT_SYMBOL, "1h", None, limit),
            self._call(exchange.fetch_ohlcv, CCXT_SYMBOL, "15m", None, limit),
        )
        if not isinstance(trend, list) or not isinstance(entry, list):
            raise ValueError("invalid kline response")
        return trend, entry

    async def premium_index(self, network):
        exchange = await self.exchange(network)
        payload = await self._call(exchange.fapiPublicGetPremiumIndex, {"symbol": MARKET_ID})
        row = payload[0] if isinstance(payload, list) and payload else payload
        if not isinstance(row, dict) or _f(row.get("markPrice")) <= 0:
            raise ValueError("invalid premiumIndex response")
        return {
            "mark_price": _f(row.get("markPrice")),
            "funding_rate": _f(row.get("lastFundingRate")),
            "next_funding_time": int(_f(row.get("nextFundingTime"))),
        }

    async def taker_fee_rate(self, network, cfg):
        if self.has_credentials(network):
            try:
                exchange = await self.exchange(network)
                payload = await self._call(exchange.fapiPrivateGetCommissionRate, {"symbol": MARKET_ID})
                rate = _f((payload or {}).get("takerCommissionRate"), -1)
                if rate >= 0:
                    return D(str(rate)), "exchange"
            except Exception as exc:
                logger.info("BTC pullback commission rate unavailable: %s", exc)
        return D(cfg["estimated_taker_fee_rate"]), "config"

    async def balances(self, network, cfg, mode):
        """(wallet, available, equity, source)."""
        if self.has_credentials(network):
            exchange = await self.exchange(network)
            payload = await self._call(exchange.fetch_balance)
            info = (payload or {}).get("info") or {}
            wallet = _f(info.get("totalWalletBalance"))
            available = _f(info.get("availableBalance"), wallet)
            equity = _f(info.get("totalMarginBalance"), wallet)
            if wallet <= 0:
                usdt = (payload or {}).get("USDT") or {}
                wallet = _f(usdt.get("total"))
                available = _f(usdt.get("free"), wallet)
                equity = wallet
            return wallet, available, equity, "exchange"
        if mode == TRADING_MODE_LIVE:
            raise RuntimeError("API_KEYS_MISSING")
        closed = self.ledger(network).trades(mode=TRADING_MODE_DRY_RUN, network=network, status="CLOSED")
        balance = _f(cfg["dry_run_balance_usdt"]) + sum(_f(t.get("net_pnl")) for t in closed)
        return balance, balance, balance, "paper"

    # ------------------------------------------------------------------ status
    def daily_snapshot(self, network, mode, cfg, equity):
        start, end, day_key = trading_day_bounds(datetime.fromtimestamp(self._clock(), timezone.utc), cfg["day_timezone"])
        key = f"day_start_equity:{mode}"
        stored = self.ledger(network).get_state(key, {})
        if not isinstance(stored, dict) or stored.get("day") != day_key:
            if equity > 0:
                stored = {"day": day_key, "equity": equity}
                self.ledger(network).set_state(key, stored)
        trades_today = self.ledger(network).trades(mode=mode, network=network, since=start, until=end)
        return trades_today, _f((stored or {}).get("equity")), day_key

    async def status(self):
        cfg = self.config()
        mode = self.mode(cfg)
        network = self.current_network(cfg)
        if not network:
            return {"config": cfg, "mode": mode, "network": None, "unsupported": True}
        state = self.load_state(network, mode)
        start, end, day_key = trading_day_bounds(datetime.fromtimestamp(self._clock(), timezone.utc), cfg["day_timezone"])
        trades_today = self.ledger(network).trades(mode=mode, network=network, since=start, until=end)
        last_signal = next(iter(self.ledger(network).recent_events(1, kinds={"SIGNAL"})), None)
        last_skip = next(iter(self.ledger(network).recent_events(1, kinds={"SKIP", "SIGNAL"})), None)
        all_trades = self.ledger(network).trades(mode=mode, network=network)
        return {
            "config": cfg,
            "mode": mode,
            "network": network,
            "has_credentials": self.has_credentials(network),
            "state": state,
            "day": day_key,
            "trades_today": trades_today,
            "last_signal": last_signal,
            "last_skip": last_skip,
            "stats": compute_stats(all_trades),
        }

    def _main_network(self):
        if not self.main_network_getter:
            return None
        try:
            return self.main_network_getter()
        except Exception:
            return None

    # -------------------------------------------------------------- main cycle
    async def run_cycle(self):
        async with self._cycle_lock:
            cfg = self.config()
            mode = self.mode(cfg)
            network = self.current_network(cfg)
            if not network:
                return {"action": "blocked", "reason": "EXCHANGE_MODE_UNSUPPORTED"}
            state = self.load_state(network, mode)
            try:
                if mode == TRADING_MODE_LIVE:
                    if not self.has_credentials(network):
                        await self._notify(
                            f"⚠️ {self.LABEL} 전략: {network} API 키가 없어 LIVE를 진행할 수 없습니다.",
                            key="no_keys",
                        )
                        return {"action": "blocked", "reason": "API_KEYS_MISSING"}
                    if not state.get("reconciled"):
                        await self.reconcile_live(network, cfg, state)
                        if not state.get("reconciled"):
                            # Exchange state unknown: never open anything new.
                            return {"action": "blocked", "reason": "RECONCILIATION_PENDING"}
                    if state.get("trade"):
                        return await self.manage_live(network, cfg, state)
                elif state.get("trade"):
                    return await self.manage_paper(network, cfg, state)
                if not cfg["enabled"]:
                    return {"action": "disabled"}
                return await self.maybe_enter(network, cfg, mode, state)
            except Exception as exc:
                logger.exception("%s cycle failed", self.LABEL)
                state["last_error"] = f"{type(exc).__name__}: {exc}"
                self.save_state(network, mode, state)
                self.ledger(network).event("ERROR", mode=mode, network=network, error=state["last_error"])
                return {"action": "error", "reason": state["last_error"]}

    # ------------------------------------------------------------------- entry
    async def maybe_enter(self, network, cfg, mode, state):
        ledger = self.ledger(network)
        now = self.now_ms()
        try:
            trend_rows, entry_rows = await self.candles(network, cfg)
        except Exception as exc:
            # REST/market-data outage (the strategy polls REST; a dropped
            # connection lands here): no entry, positions keep protection.
            reason = f"MARKET_DATA_UNAVAILABLE: {type(exc).__name__}: {exc}"
            state["last_error"] = reason
            self.save_state(network, mode, state)
            ledger.event("SKIP", mode=mode, network=network, skip_reason=reason)
            return {"action": "skip", "reason": reason}

        signal = generate_signal(trend_rows, entry_rows, cfg, now)
        bar = int(signal.get("15m_bar_open_ms") or 0)
        if bar and bar == int(state.get("last_evaluated_bar") or 0):
            return {"action": "waiting", "reason": "WAITING_NEXT_15M_CLOSE"}
        if bar:
            state["last_evaluated_bar"] = bar
        signal_log = {k: v for k, v in signal.items() if k != "checks"}
        ledger.event("SIGNAL", mode=mode, network=network, checks=signal.get("checks"), **signal_log)
        state["last_error"] = ""
        if not signal.get("side"):
            self.save_state(network, mode, state)
            return {"action": "skip", "reason": signal.get("skip_reason") or "NO_SIGNAL"}

        signal_id = signal["signal_id"]
        if signal_id in state["processed_signals"]:
            self.save_state(network, mode, state)
            return {"action": "skip", "reason": "DUPLICATE_SIGNAL"}
        self._set_phase(network, mode, state, SIGNAL_DETECTED, last_signal_id=signal_id)
        self._set_phase(network, mode, state, PRE_TRADE_VALIDATION)

        decision, reason, context = await self.pre_trade_validation(network, cfg, mode, signal)
        # One decision per 15m signal: a skipped signal is never retried.
        state["processed_signals"].append(signal_id)
        if reason:
            ledger.event("SKIP", mode=mode, network=network, signal_id=signal_id, side=signal["side"], skip_reason=reason, **context)
            self._set_phase(network, mode, state, IDLE)
            if reason.startswith(("DAILY_", "CONSECUTIVE_")):
                await self._notify(f"⏸ {self.LABEL} 전략: 오늘 신규 진입 중단 — {reason}", key=f"daily:{context.get('day')}")
            return {"action": "skip", "reason": reason}
        if mode == TRADING_MODE_LIVE:
            return await self.enter_live(network, cfg, state, signal, decision, context)
        return await self.enter_paper(network, cfg, state, signal, decision, context)

    async def pre_trade_validation(self, network, cfg, mode, signal):
        """Return (decision, skip_reason, log_context)."""
        context = {}
        try:
            wallet, available, equity, balance_source = await self.balances(network, cfg, mode)
        except Exception as exc:
            return None, f"BALANCE_UNAVAILABLE: {exc}", context
        trades_today, day_start_equity, day_key = self.daily_snapshot(network, mode, cfg, equity)
        context.update(wallet_balance=wallet, available_balance=available, equity=equity,
                       balance_source=balance_source, day=day_key, daily_trade_count=len(trades_today),
                       day_start_equity=day_start_equity)
        reason = daily_entry_block_reason(trades_today, cfg, day_start_equity=day_start_equity, current_equity=equity)
        if reason:
            return None, reason, context

        if mode == TRADING_MODE_LIVE:
            try:
                positions = await self.fetch_account_positions(network)
            except Exception as exc:
                return None, f"POSITION_STATE_UNCERTAIN: {exc}", context
            if positions:
                held = ", ".join(f"{p.get('symbol')} {_position_side(p)} {_position_qty(p)}" for p in positions[:3])
                return None, f"ACCOUNT_POSITION_EXISTS: {held}", context
            setup_error = await self.ensure_account_setup(network, cfg)
            if setup_error:
                return None, setup_error, context
            stray = await self.cancel_stray_protection(network)
            if stray:
                return None, stray, context

        try:
            rules = await self.trading_rules(network, cfg)
            market = await self.premium_index(network)
        except (TradingRulesError, Exception) as exc:
            return None, f"TRADING_RULES_UNAVAILABLE: {type(exc).__name__}: {exc}", context
        fee_rate, fee_source = await self.taker_fee_rate(network, cfg)
        context.update(mark_price=market["mark_price"], funding_rate=market["funding_rate"],
                       next_funding_time=market["next_funding_time"], taker_fee_rate=fee_rate, fee_source=fee_source,
                       tick_size=rules.tick_size, step_size=rules.effective_step, min_qty=rules.effective_min_qty,
                       min_notional=rules.min_notional)
        decision = validate_and_normalize_order_quantity(
            rules=rules, price=market["mark_price"], wallet_balance=wallet, available_balance=available,
            cfg=cfg, taker_fee_rate=fee_rate, sizing_mode=sizing_mode_for(cfg),
        )
        context.update({k: v for k, v in decision.items() if k not in {"accepted"}})
        if not decision["accepted"]:
            return None, decision["skip_reason"], context
        decision["rules"] = rules
        decision["fee_rate"] = fee_rate
        return decision, "", context

    def _new_trade(self, network, mode, signal, decision, context, *, trade_id, entry_price, quantity, entry_ms, rules, cfg):
        stop, take = protection_prices(signal["side"], entry_price, cfg, rules)
        initial_risk = abs(D(entry_price) - stop) * D(quantity)
        return {
            "trade_id": trade_id,
            "signal_id": signal["signal_id"],
            "signal_bar_ms": self._signal_bar(signal),
            "mode": mode,
            "network": network,
            "status": "OPEN",
            "side": signal["side"],
            "quantity": str(quantity),
            "entry_price": str(entry_price),
            "stop_price": str(stop),
            "take_profit_price": str(take),
            "entry_time": _iso(entry_ms),
            "entry_ms": entry_ms,
            "initial_risk": str(initial_risk),
            "leverage": cfg["leverage"],
            "fee_rate": str(decision["fee_rate"]),
            "strategy_version": STRATEGY_VERSION,
            "rule_violation": "",
            "sl_revision": 0,
            "tp_revision": 0,
        }

    def _order_log(self, trade, decision, context, cfg, requested_entry):
        return {
            "signal_id": trade["signal_id"],
            "side": trade["side"],
            "quantity": trade["quantity"],
            "requested_entry": requested_entry,
            "actual_avg_entry": trade["entry_price"],
            "stop_price": trade["stop_price"],
            "take_profit_price": trade["take_profit_price"],
            "leverage": cfg["leverage"],
            "margin_mode": cfg["margin_type"],
            "notional": D(trade["entry_price"]) * D(trade["quantity"]),
            "estimated_margin": D(trade["entry_price"]) * D(trade["quantity"]) / D(cfg["leverage"]),
            "estimated_fee": decision.get("estimated_entry_fee"),
            "estimated_risk": decision.get("estimated_total_risk"),
            "wallet_balance": context.get("wallet_balance"),
            "sizing_mode": decision.get("sizing_mode"),
        }

    # ---------------------------------------------------------------- DRY_RUN
    async def enter_paper(self, network, cfg, state, signal, decision, context):
        mode = TRADING_MODE_DRY_RUN
        mark = D(context["mark_price"])
        slip = D(cfg["slippage_buffer_pct"])
        fill = mark * (1 + slip) if signal["side"] == "LONG" else mark * (1 - slip)
        fill = fill.quantize(decision["rules"].tick_size)
        now = self.now_ms()
        trade = self._new_trade(
            network, mode, signal, decision, context, trade_id=f"paper-{self._signal_bar(signal)}-{signal['side'][0]}",
            entry_price=fill, quantity=decision["quantity"], entry_ms=now, rules=decision["rules"], cfg=cfg,
        )
        trade["paper_last_checked_ms"] = now
        trade["funding_settled"] = []
        trade["funding"] = "0"
        self.ledger(network).upsert_trade(trade)
        self.ledger(network).event("ORDER", mode=mode, network=network, dry_run=True, **self._order_log(trade, decision, context, cfg, str(mark)))
        self._set_phase(network, mode, state, POSITION_OPEN, trade=trade)
        await self._notify(self._entry_text(trade, mode))
        return {"action": "entered", "trade": trade}

    async def manage_paper(self, network, cfg, state):
        mode = TRADING_MODE_DRY_RUN
        trade = state["trade"]
        exchange = await self.exchange(network)
        since = int(trade.get("paper_last_checked_ms") or trade["entry_ms"])
        try:
            rows = await self._call(exchange.fetch_ohlcv, CCXT_SYMBOL, "1m", since - 60_000, 1000)
        except Exception as exc:
            self.ledger(network).event("SKIP", mode=mode, network=network, skip_reason=f"PAPER_PRICE_UNAVAILABLE: {exc}")
            return {"action": "managed", "reason": "PRICE_UNAVAILABLE"}
        long = trade["side"] == "LONG"
        stop, take = _f(trade["stop_price"]), _f(trade["take_profit_price"])
        exit_price = exit_reason = None
        exit_ms = None
        for row in sorted(rows or [], key=lambda r: r[0]):
            if int(row[0]) + 60_000 <= int(trade["entry_ms"]):
                continue
            high, low = _f(row[2]), _f(row[3])
            hit_sl = low <= stop if long else high >= stop
            hit_tp = high >= take if long else low <= take
            if hit_sl:  # both inside one candle: assume the stop first (conservative)
                exit_price, exit_reason, exit_ms = stop, "SL", int(row[0]) + 60_000
                break
            if hit_tp:
                exit_price, exit_reason, exit_ms = take, "TP", int(row[0]) + 60_000
                break
        now = self.now_ms()
        await self._settle_paper_funding(network, trade, min(exit_ms or now, now))
        if exit_price is None:
            trade["paper_last_checked_ms"] = now
            state["trade"] = trade
            self.save_state(network, mode, state)
            return {"action": "managed"}
        slip = _f(cfg["slippage_buffer_pct"])
        if exit_reason == "SL":
            exit_price = exit_price * (1 - slip) if long else exit_price * (1 + slip)
        return await self.finalize_paper(network, cfg, state, exit_price, exit_reason, exit_ms or now)

    async def _settle_paper_funding(self, network, trade, until_ms):
        """Estimate funding for every 8h settlement (00/08/16 UTC) the trade spans."""
        period = 8 * 3_600_000
        settled = list(trade.get("funding_settled") or [])
        first = (int(trade["entry_ms"]) // period + 1) * period
        due = [t for t in range(first, int(until_ms) + 1, period) if t not in settled]
        if not due:
            return
        try:
            rate = (await self.premium_index(network))["funding_rate"]
        except Exception:
            return
        notional = _f(trade["entry_price"]) * _f(trade["quantity"])
        sign = -1.0 if trade["side"] == "LONG" else 1.0  # longs pay positive funding
        trade["funding"] = str(_f(trade.get("funding")) + sign * notional * rate * len(due))
        trade["funding_settled"] = settled + due
        trade["funding_estimated"] = True

    async def finalize_paper(self, network, cfg, state, exit_price, exit_reason, exit_ms):
        mode = TRADING_MODE_DRY_RUN
        trade = state["trade"]
        fee_rate = _f(trade.get("fee_rate"), cfg["estimated_taker_fee_rate"])
        qty = _f(trade["quantity"])
        commission = (_f(trade["entry_price"]) + exit_price) * qty * fee_rate
        result = self._close_trade(trade, exit_price, exit_reason, exit_ms, commission=commission, funding=_f(trade.get("funding")))
        self._set_phase(network, mode, state, EXIT_FILLED)
        self.ledger(network).upsert_trade(result)
        self.ledger(network).event("EXIT", mode=mode, network=network, dry_run=True, **self._exit_log(network, mode, cfg, result))
        self._set_phase(network, mode, state, TRADE_RECORDED, trade=None)
        self._set_phase(network, mode, state, IDLE)
        await self._notify(self._exit_text(result, mode))
        return {"action": "exited", "trade": result}

    # --------------------------------------------------------------- PnL / logs
    def _close_trade(self, trade, exit_price, exit_reason, exit_ms, *, commission, funding, extra=None):
        long = trade["side"] == "LONG"
        qty, entry = _f(trade["quantity"]), _f(trade["entry_price"])
        gross = (exit_price - entry) * qty * (1 if long else -1)
        net = gross - commission + funding
        risk = _f(trade.get("initial_risk"))
        result = dict(trade)
        result.update(
            status="CLOSED",
            exit_time=_iso(exit_ms),
            exit_price=str(exit_price),
            exit_reason=exit_reason,
            gross_pnl=str(gross),
            commission=str(commission),
            funding=str(funding),
            net_pnl=str(net),
            pnl_pct=str(net / (entry * qty) * 100 if entry and qty else 0),
            r_multiple=str(net / risk) if risk > 0 else "",
            result="WIN" if net > 0 else ("LOSS" if net < 0 else "BREAKEVEN"),
            extra=extra or {},
        )
        return result

    def _exit_log(self, network, mode, cfg, trade):
        start, end, _ = trading_day_bounds(datetime.fromtimestamp(self._clock(), timezone.utc), cfg["day_timezone"])
        today = self.ledger(network).trades(mode=mode, network=network, since=start, until=end)
        streak = 0
        for row in reversed([t for t in today if t.get("status") == "CLOSED"]):
            if _f(row.get("net_pnl")) < 0:
                streak += 1
            else:
                break
        return {
            "trade_id": trade["trade_id"],
            "exit_reason": trade["exit_reason"],
            "entry_price": trade["entry_price"],
            "exit_price": trade["exit_price"],
            "gross_pnl": trade["gross_pnl"],
            "commission": trade["commission"],
            "funding_fee": trade["funding"],
            "net_pnl": trade["net_pnl"],
            "pnl_pct": trade["pnl_pct"],
            "R_multiple": trade["r_multiple"],
            "consecutive_losses": streak,
            "daily_trade_count": len(today),
        }

    def _entry_text(self, trade, mode):
        tag = "🧪 DRY_RUN" if mode == TRADING_MODE_DRY_RUN else "💥 LIVE"
        return (
            f"📈 {self.LABEL} 진입 ({tag} · {trade['network']})\n"
            f"{trade['side']} {trade['quantity']} BTC @ {trade['entry_price']}\n"
            f"손절 {trade['stop_price']} · 익절 {trade['take_profit_price']}"
        )

    def _exit_text(self, trade, mode):
        tag = "🧪 DRY_RUN" if mode == TRADING_MODE_DRY_RUN else "💥 LIVE"
        return (
            f"🏁 {self.LABEL} 청산 ({tag} · {trade['network']}) — {trade['exit_reason']}\n"
            f"{trade['side']} {trade['entry_price']} → {_f(trade['exit_price']):.1f}\n"
            f"순손익 {_f(trade['net_pnl']):+.4f} USDT (수수료 {_f(trade['commission']):.4f}, 펀딩 {_f(trade['funding']):+.4f}) · "
            f"{_f(trade['r_multiple']):+.2f}R"
        )

    # --------------------------------------------------------------- LIVE: io
    async def fetch_account_positions(self, network):
        exchange = await self.exchange(network)
        positions = await self._call(exchange.fetch_positions)
        if not isinstance(positions, list):
            raise ValueError("invalid positions response")
        return [row for row in positions if _position_qty(row) > 0]

    async def fetch_position(self, network):
        exchange = await self.exchange(network)
        positions = await self._call(exchange.fetch_positions, [CCXT_SYMBOL])
        if not isinstance(positions, list):
            raise ValueError("invalid positions response")
        for row in positions:
            if _market_key(row.get("symbol")) == MARKET_ID and _position_qty(row) > 0:
                return row
        return None

    async def ensure_account_setup(self, network, cfg):
        """One-way mode, ISOLATED margin and the configured leverage, verified."""
        verified_at = self._account_verified.get(network, 0)
        if self.now_ms() - verified_at < 600_000:
            return ""
        exchange = await self.exchange(network)
        try:
            dual = await self._call(exchange.fapiPrivateGetPositionSideDual)
            if str((dual or {}).get("dualSidePosition")).lower() == "true":
                return "POSITION_MODE_HEDGE: 바이낸스에서 One-way 모드로 바꾼 뒤 다시 켜세요"
        except Exception as exc:
            return f"POSITION_MODE_UNKNOWN: {exc}"
        try:
            await self._call(exchange.set_margin_mode, "isolated", CCXT_SYMBOL)
        except Exception as exc:
            if "no need to change" not in str(exc).lower() and "-4046" not in str(exc):
                logger.info("BTC pullback set_margin_mode: %s", exc)
        try:
            await self._call(exchange.set_leverage, int(cfg["leverage"]), CCXT_SYMBOL)
        except Exception as exc:
            logger.info("BTC pullback set_leverage: %s", exc)
        try:
            rows = await self._call(exchange.fapiPrivateV2GetPositionRisk, {"symbol": MARKET_ID})
        except Exception as exc:
            return f"ACCOUNT_SETUP_UNVERIFIED: {exc}"
        row = next((r for r in rows or [] if str(r.get("symbol")) == MARKET_ID), None)
        if not row:
            return "ACCOUNT_SETUP_UNVERIFIED: BTCUSDT positionRisk missing"
        leverage = int(_f(row.get("leverage")))
        margin_type = str(row.get("marginType") or "").lower()
        if leverage != int(cfg["leverage"]) or margin_type != "isolated":
            return f"ACCOUNT_SETUP_MISMATCH: leverage {leverage}x / {margin_type or '?'} (need {cfg['leverage']}x isolated)"
        self._account_verified[network] = self.now_ms()
        self.ledger(network).event("ACCOUNT_SETUP", network=network, leverage=leverage, margin_type=margin_type, position_mode="ONE_WAY")
        return ""

    async def open_algo_orders(self, network):
        _, algo = await self.gateway(network)
        snapshot = await algo.fetch_open_orders()
        if not snapshot.ok:
            raise RuntimeError(snapshot.error or "open algo orders unavailable")
        return [o for o in snapshot.orders if str(o.get("symbol") or "").upper() == MARKET_ID]

    async def cancel_algo(self, network, client_algo_id):
        exchange = await self.exchange(network)
        try:
            await self._call(exchange.fapiPrivateDeleteAlgoOrder, {"clientAlgoId": client_algo_id})
            return True
        except Exception as exc:
            text = str(exc).lower()
            if "unknown order" in text or "-2011" in text or "-2013" in text or "does not exist" in text:
                return False
            raise

    async def cancel_stray_protection(self, network):
        """Before a new entry: our leftover TP/SL triggers are cancelled, foreign ones block."""
        try:
            orders = await self.open_algo_orders(network)
        except Exception as exc:
            return f"OPEN_ORDERS_UNKNOWN: {exc}"
        foreign = []
        for order in orders:
            client_id = str(order.get("clientAlgoId") or "")
            if client_id.startswith(self.CLIENT_ID):
                try:
                    await self.cancel_algo(network, client_id)
                except Exception as exc:
                    return f"OPEN_ORDERS_CANCEL_FAILED: {client_id}: {exc}"
                self.ledger(network).event("CANCEL_STRAY_PROTECTION", network=network, client_algo_id=client_id)
            else:
                foreign.append(client_id or str(order.get("algoId")))
        if foreign:
            return f"FOREIGN_OPEN_ORDERS: {', '.join(foreign[:3])}"
        return ""

    # --------------------------------------------------------------- LIVE: entry
    async def enter_live(self, network, cfg, state, signal, decision, context):
        mode = TRADING_MODE_LIVE
        ledger = self.ledger(network)
        order_gateway, _ = await self.gateway(network)
        # Persist the deterministic clientOrderId *before* sending, so a crash
        # between submit and fill confirmation is reconciled to this signal.
        client_order_id = build_client_order_id(
            self.CLIENT_ID, CCXT_SYMBOL, signal["side"].lower(), self._signal_bar(signal), "entry"
        )
        self._set_phase(network, mode, state, ENTRY_SUBMITTED, pending_entry={
            "client_order_id": client_order_id, "signal": signal, "decision_qty": str(decision["quantity"]),
        })
        result = await order_gateway.submit_entry(
            strategy=self.CLIENT_ID,
            symbol=CCXT_SYMBOL,
            side=signal["side"].lower(),
            signal_timestamp=self._signal_bar(signal),
            qty=float(decision["quantity"]),
        )
        ledger.event("ENTRY_RESULT", mode=mode, network=network, signal_id=signal["signal_id"],
                     client_order_id=result.client_order_id, state=result.state, error=result.error)
        if result.state in {"BLOCKED", OrderState.FAILED.value, OrderState.CANCELED.value}:
            self._set_phase(network, mode, state, IDLE, pending_entry=None, last_error=f"ENTRY_REJECTED: {result.error}")
            await self._notify(f"⚠️ {self.LABEL} 진입 실패: {result.error}")
            return {"action": "rejected", "reason": result.error}
        try:
            position = result.position or await self.fetch_position(network)
        except Exception as exc:
            position = None
            ledger.event("POSITION_FETCH_FAILED", network=network, error=str(exc))
        if not position or _position_side(position) != signal["side"]:
            # Ambiguous: reconcile on the next cycles; never resubmit blindly.
            self._set_phase(network, mode, state, POSITION_RECONCILIATION, reconciled=False,
                            pending_entry={"client_order_id": result.client_order_id, "signal": signal,
                                           "decision_qty": str(decision["quantity"])},
                            last_error=f"ENTRY_UNCONFIRMED: {result.state} {result.error or ''}")
            return {"action": "reconcile", "reason": result.state}
        qty = _position_qty(position)
        avg = _position_entry(position) or _f((result.order or {}).get("average"))
        trade = self._new_trade(network, mode, signal, decision, context, trade_id=result.client_order_id,
                                entry_price=D(str(avg)), quantity=D(str(qty)), entry_ms=self.now_ms(),
                                rules=decision["rules"], cfg=cfg)
        if D(str(qty)) < decision["quantity"]:
            trade["rule_violation"] = f"PARTIAL_FILL: {qty} of {decision['quantity']}"
        trade["entry_client_order_id"] = result.client_order_id
        ledger.upsert_trade(trade)
        self._set_phase(network, mode, state, ENTRY_FILLED, trade=trade, pending_entry=None)
        ledger.event("ORDER", mode=mode, network=network, **self._order_log(trade, decision, context, cfg, str(context.get("mark_price"))))
        await self._notify(self._entry_text(trade, mode))
        return await self.attach_protection(network, cfg, state)

    def _algo_client_id(self, trade, kind):
        revision = int(trade.get(f"{kind}_revision") or 0)
        return build_client_order_id(self.CLIENT_ID, MARKET_ID, trade["side"], trade["signal_bar_ms"], kind,
                                     revision=revision or None)

    async def place_protection(self, network, cfg, trade, kind):
        """Place (or confirm) one closePosition trigger; returns 'OK', 'BREACHED' or 'FAILED:<why>'."""
        _, algo = await self.gateway(network)
        client_id = self._algo_client_id(trade, kind)
        spec = self._protection_spec(cfg, trade, kind)
        close_side = "SELL" if trade["side"] == "LONG" else "BUY"
        delays = list(cfg["protection_retry_delays_seconds"])
        last_error = ""
        for attempt in range(int(cfg["protection_retry_attempts"])):
            lookup = await algo.fetch_by_client_id(client_id)
            if lookup.status == AlgoLookupStatus.FOUND:
                status = str((lookup.order or {}).get("status") or "").upper()
                if status in OPEN_ALGO_STATUSES:
                    trade[f"{kind}_client_algo_id"] = client_id
                    return "OK"
                # A finished/cancelled trigger with this id cannot be reused.
                trade[f"{kind}_revision"] = int(trade.get(f"{kind}_revision") or 0) + 1
                client_id = self._algo_client_id(trade, kind)
                continue
            if lookup.status == AlgoLookupStatus.UNKNOWN:
                last_error = lookup.error or "lookup unknown"
            else:
                try:
                    await algo.create_conditional_order(
                        CCXT_SYMBOL, spec["order_type"], close_side, spec.get("quantity"),
                        trigger_price=spec["trigger"], client_algo_id=client_id,
                        close_position=spec.get("close_position", True),
                        reduce_only=True, working_type=spec["working_type"],
                    )
                except Exception as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    if "-2021" in last_error or "immediately trigger" in last_error.lower():
                        return "BREACHED"
                    self.ledger(network).event("PROTECTION_RETRY", network=network, kind=kind, attempt=attempt + 1, error=last_error)
                else:
                    confirm = await algo.fetch_by_client_id(client_id)
                    if confirm.status == AlgoLookupStatus.FOUND:
                        trade[f"{kind}_client_algo_id"] = client_id
                        return "OK"
                    last_error = confirm.error or "created but not confirmed"
            if attempt < len(delays):
                await self._sleep(delays[attempt])
        return f"FAILED:{last_error}"

    def _protection_spec(self, cfg, trade, kind):
        """Order type / trigger / sizing of one protection kind."""
        if kind == "sl":
            return {"order_type": "STOP_MARKET", "trigger": trade["stop_price"],
                    "close_position": True, "working_type": cfg["working_type"]}
        return {"order_type": "TAKE_PROFIT_MARKET", "trigger": trade["take_profit_price"],
                "close_position": True, "working_type": cfg["tp_working_type"]}

    async def attach_protection(self, network, cfg, state):
        mode = TRADING_MODE_LIVE
        trade = state["trade"]
        self._set_phase(network, mode, state, PROTECTION_ORDERS_SUBMITTED)
        sl = await self.place_protection(network, cfg, trade, "sl")
        self.ledger(network).event("PROTECTION", network=network, kind="sl", result=sl, trigger=trade["stop_price"],
                                   working_type=cfg["working_type"])
        if sl != "OK":
            state["trade"] = trade
            return await self.failsafe_close(network, cfg, state, f"SL_{sl}")
        tp = await self.place_protection(network, cfg, trade, "tp")
        self.ledger(network).event("PROTECTION", network=network, kind="tp", result=tp, trigger=trade["take_profit_price"],
                                   working_type=cfg["tp_working_type"])
        trade["tp_missing"] = tp != "OK"
        trade["last_protection_check_ms"] = self.now_ms()
        entry_id = trade.get("entry_client_order_id")
        if entry_id and self.store(network).get(entry_id):
            self.store(network).transition(entry_id, OrderState.PROTECTED, stop_order_id=trade.get("sl_client_algo_id"))
        self._set_phase(network, mode, state, POSITION_OPEN, trade=trade)
        if trade["tp_missing"]:
            await self._notify(f"⚠️ {self.LABEL}: 익절 주문 등록 실패({tp}). 손절은 걸려 있고 다음 주기에 재시도합니다.")
        return {"action": "entered", "trade": trade}

    async def failsafe_close(self, network, cfg, state, reason):
        """Entry filled but no confirmed stop: verify on Binance, then market-close."""
        mode = TRADING_MODE_LIVE
        trade = state["trade"]
        self._set_phase(network, mode, state, PROTECTION_FAILED, last_error=reason)
        self.ledger(network).event("FAILSAFE", network=network, trade_id=trade["trade_id"], reason=reason)
        try:
            position = await self.fetch_position(network)
            orders = await self.open_algo_orders(network)
        except Exception as exc:
            self._set_phase(network, mode, state, ERROR_RECOVERY, last_error=f"{reason}; state unknown: {exc}")
            await self._notify(f"🚨 {self.LABEL}: 보호주문 실패 + 거래소 상태 확인 불가 ({exc}). 다음 주기에 재시도합니다.")
            return {"action": "error", "reason": reason}
        if not position:
            return await self.finalize_live(network, cfg, state, "FAILSAFE")
        close_side = "sell" if trade["side"] == "LONG" else "buy"
        has_stop = any(
            o.get("orderType") in {"STOP_MARKET", "STOP"} and o.get("side") == close_side
            for o in orders
        )
        if has_stop:
            self._set_phase(network, mode, state, POSITION_OPEN, last_error="")
            self.ledger(network).event("FAILSAFE_ABORTED", network=network, reason="stop order found on exchange")
            return {"action": "managed", "reason": "STOP_FOUND"}
        order_gateway, _ = await self.gateway(network)
        result = await order_gateway.submit_reduce_only_close(
            strategy=self.CLIENT_ID, symbol=CCXT_SYMBOL, position_side=trade["side"].lower(),
            position_signature=trade["signal_bar_ms"], qty=_position_qty(position), reason="emergency_failsafe",
        )
        trade["failsafe"] = True
        state["trade"] = trade
        self.ledger(network).event("FAILSAFE_CLOSE", network=network, state=result.state, error=result.error)
        await self._notify(f"🚨 {self.LABEL}: 손절 주문을 걸지 못해 포지션을 시장가로 정리했습니다 ({reason}) → {result.state}")
        if result.state == OrderState.CLOSED.value:
            return await self.finalize_live(network, cfg, state, "FAILSAFE")
        self._set_phase(network, mode, state, ERROR_RECOVERY, trade=trade, last_error=f"FAILSAFE_CLOSE_{result.state}")
        return {"action": "error", "reason": result.state}

    # ------------------------------------------------------------ LIVE: manage
    async def manage_live(self, network, cfg, state):
        mode = TRADING_MODE_LIVE
        trade = state["trade"]
        try:
            position = await self.fetch_position(network)
        except Exception as exc:
            self.ledger(network).event("POSITION_FETCH_FAILED", network=network, error=str(exc))
            return {"action": "managed", "reason": "POSITION_UNKNOWN"}
        if not position:
            return await self.finalize_live(network, cfg, state, None)
        if state.get("phase") in {PROTECTION_FAILED, ERROR_RECOVERY} or trade.get("failsafe"):
            return await self.failsafe_close(network, cfg, state, state.get("last_error") or "RETRY_FAILSAFE")
        if _position_side(position) != trade["side"] or _position_qty(position) > _f(trade["quantity"]) * 1.000001:
            note = f"POSITION_CHANGED_EXTERNALLY: {_position_side(position)} {_position_qty(position)}"
            if note not in str(trade.get("rule_violation")):
                trade["rule_violation"] = (str(trade.get("rule_violation") or "") + " " + note).strip()
                self.ledger(network).upsert_trade(trade)
                self.ledger(network).event("RULE_VIOLATION", network=network, trade_id=trade["trade_id"], note=note)
                await self._notify(f"⚠️ {self.LABEL}: 포지션이 외부에서 바뀌었습니다 ({note}). 추가 진입 없이 기존 손절/익절만 유지합니다.")
        due = self.now_ms() - int(trade.get("last_protection_check_ms") or 0) >= int(cfg["protection_check_interval_seconds"]) * 1000
        if due or trade.get("tp_missing"):
            await self.verify_protection(network, cfg, state)
        return {"action": "managed"}

    async def verify_protection(self, network, cfg, state):
        trade = state["trade"]
        try:
            orders = await self.open_algo_orders(network)
        except Exception as exc:
            self.ledger(network).event("PROTECTION_CHECK_FAILED", network=network, error=str(exc))
            return
        ids = {str(o.get("clientAlgoId") or "") for o in orders}
        if trade.get("sl_client_algo_id") not in ids:
            trade["sl_revision"] = int(trade.get("sl_revision") or 0) + 1
            result = await self.place_protection(network, cfg, trade, "sl")
            self.ledger(network).event("PROTECTION_REPAIR", network=network, kind="sl", result=result)
            if result != "OK":
                state["trade"] = trade
                await self.failsafe_close(network, cfg, state, f"SL_MISSING_{result}")
                return
        if trade.get("tp_missing") or trade.get("tp_client_algo_id") not in ids:
            if not trade.get("tp_missing"):
                trade["tp_revision"] = int(trade.get("tp_revision") or 0) + 1
            result = await self.place_protection(network, cfg, trade, "tp")
            trade["tp_missing"] = result != "OK"
            self.ledger(network).event("PROTECTION_REPAIR", network=network, kind="tp", result=result)
        trade["last_protection_check_ms"] = self.now_ms()
        state["trade"] = trade
        self.save_state(network, TRADING_MODE_LIVE, state)

    async def finalize_live(self, network, cfg, state, forced_reason):
        """Position is flat: cancel leftover triggers, account PnL, record, go IDLE."""
        mode = TRADING_MODE_LIVE
        trade = state["trade"]
        self._set_phase(network, mode, state, EXIT_FILLED)
        _, algo = await self.gateway(network)
        reason = forced_reason
        for kind in self.PROTECTION_KINDS:
            client_id = trade.get(f"{kind}_client_algo_id")
            if not client_id:
                continue
            lookup = await algo.fetch_by_client_id(client_id)
            status = str((lookup.order or {}).get("status") or "").upper()
            if lookup.status == AlgoLookupStatus.FOUND and status in OPEN_ALGO_STATUSES:
                try:
                    await self.cancel_algo(network, client_id)
                except Exception as exc:
                    self.ledger(network).event("CANCEL_FAILED", network=network, client_algo_id=client_id, error=str(exc))
            elif lookup.status == AlgoLookupStatus.FOUND and reason is None and status in {"TRIGGERED", "FINISHED", "FILLED"}:
                reason = self.EXIT_REASON_BY_KIND.get(kind, kind.upper())
        fills = await self._exit_fills(network, trade)
        exit_reason = reason or ("FAILSAFE" if trade.get("failsafe") else "MANUAL")
        result = self._close_trade(trade, fills["exit_price"], exit_reason, fills["exit_ms"],
                                   commission=fills["commission"], funding=fills["funding"],
                                   extra={"pnl_source": fills["source"]})
        self.ledger(network).upsert_trade(result)
        self.ledger(network).event("EXIT", mode=mode, network=network, **self._exit_log(network, mode, cfg, result))
        entry_id = trade.get("entry_client_order_id")
        store = self.store(network)
        if entry_id and store.get(entry_id):
            store.transition(entry_id, OrderState.CLOSED)
            store.release_entry_lease(entry_id, OrderState.CLOSED)
        self._set_phase(network, mode, state, TRADE_RECORDED, trade=None, last_error="")
        self._set_phase(network, mode, state, IDLE)
        await self._notify(self._exit_text(result, mode))
        return {"action": "exited", "trade": result}

    async def _exit_fills(self, network, trade):
        """Exit price, commission (entry+exit) and funding from Binance; estimates as fallback."""
        exchange = await self.exchange(network)
        start = int(trade.get("entry_ms") or 0) - 5_000
        now = self.now_ms()
        close_side = "SELL" if trade["side"] == "LONG" else "BUY"
        try:
            fills = await self._call(exchange.fapiPrivateGetUserTrades, {"symbol": MARKET_ID, "startTime": start, "limit": 1000})
            commission = sum(_f(f.get("commission")) for f in fills or [] if str(f.get("commissionAsset") or "USDT") == "USDT")
            closing = [f for f in fills or [] if str(f.get("side")).upper() == close_side]
            qty = sum(_f(f.get("qty")) for f in closing)
            if qty <= 0:
                raise ValueError("no closing fills")
            exit_price = sum(_f(f.get("price")) * _f(f.get("qty")) for f in closing) / qty
            exit_ms = max(int(_f(f.get("time"))) for f in closing)
            source = "exchange"
        except Exception as exc:
            logger.info("BTC pullback exit fills unavailable: %s", exc)
            try:
                exit_price = (await self.premium_index(network))["mark_price"]
            except Exception:
                exit_price = _f(trade["entry_price"])
            fee_rate = _f(trade.get("fee_rate"), 0.0005)
            commission = (_f(trade["entry_price"]) + exit_price) * _f(trade["quantity"]) * fee_rate
            exit_ms, source = now, "estimated"
        funding = 0.0
        try:
            rows = await self._call(exchange.fapiPrivateGetIncome, {"symbol": MARKET_ID, "incomeType": "FUNDING_FEE",
                                                                     "startTime": start, "endTime": now, "limit": 1000})
            funding = sum(_f(r.get("income")) for r in rows or [])
        except Exception as exc:
            logger.info("BTC pullback funding income unavailable: %s", exc)
        return {"exit_price": exit_price, "exit_ms": exit_ms, "commission": commission, "funding": funding, "source": source}

    # ------------------------------------------------------- LIVE: reconcile
    async def reconcile_live(self, network, cfg, state):
        """Exchange state is the source of truth after a (re)start or mode switch."""
        mode = TRADING_MODE_LIVE
        ledger = self.ledger(network)
        self._set_phase(network, mode, state, POSITION_RECONCILIATION)
        pending = state.get("pending_entry")
        if pending:
            order_gateway, _ = await self.gateway(network)
            record = self.store(network).get(pending.get("client_order_id"))
            if record is not None:
                await order_gateway.recover(record, wait=False)
        try:
            position = await self.fetch_position(network)
            orders = await self.open_algo_orders(network)
        except Exception as exc:
            ledger.event("RECONCILE_FAILED", network=network, error=str(exc))
            state["last_error"] = f"RECONCILE_FAILED: {exc}"
            self.save_state(network, mode, state)
            return
        exchange = await self.exchange(network)
        risk = {}
        try:
            rows = await self._call(exchange.fapiPrivateV2GetPositionRisk, {"symbol": MARKET_ID})
            row = next((r for r in rows or [] if str(r.get("symbol")) == MARKET_ID), {})
            risk = {"leverage": row.get("leverage"), "margin_type": row.get("marginType")}
        except Exception as exc:
            risk = {"error": str(exc)}
        ledger.event("RECONCILE", network=network, position_side=_position_side(position), position_qty=_position_qty(position),
                     position_entry=_position_entry(position), open_algo_orders=[o.get("clientAlgoId") for o in orders],
                     had_trade=bool(state.get("trade")), **risk)
        trade = state.get("trade")
        if position:
            side, qty, entry = _position_side(position), _position_qty(position), _position_entry(position)
            if not trade and pending:
                rules = await self.trading_rules(network, cfg)
                signal = pending["signal"]
                decision = {"fee_rate": (await self.taker_fee_rate(network, cfg))[0]}
                trade = self._new_trade(network, mode, signal, decision, {}, trade_id=pending["client_order_id"],
                                        entry_price=D(str(entry)), quantity=D(str(qty)), entry_ms=self.now_ms(), rules=rules, cfg=cfg)
                trade["entry_client_order_id"] = pending["client_order_id"]
            elif not trade and not any(
                str(o.get("clientAlgoId") or "").startswith(self.CLIENT_ID) for o in orders
            ):
                # Shared /setup account: a BTCUSDT position without our own
                # trade record or our own TP/SL belongs to the main bot or to
                # a manual trade.  Leave it alone (new entries stay blocked
                # while any position exists).
                ledger.event("RECONCILE_FOREIGN_POSITION", network=network, side=side, qty=qty, entry=entry)
                state["pending_entry"] = None
                self._set_phase(network, mode, state, IDLE, reconciled=True)
                return
            elif not trade:
                rules = await self.trading_rules(network, cfg)
                bar = self.now_ms()
                signal = {"side": side, "signal_id": f"RECOVERED:{bar}", "15m_bar_open_ms": bar}
                decision = {"fee_rate": (await self.taker_fee_rate(network, cfg))[0]}
                trade = self._new_trade(network, mode, signal, decision, {}, trade_id=f"recovered-{bar}",
                                        entry_price=D(str(entry)), quantity=D(str(qty)), entry_ms=bar, rules=rules, cfg=cfg)
                trade["rule_violation"] = "RECOVERED_FROM_OWN_PROTECTION_ORDERS"
                await self._notify(f"⚠️ {self.LABEL}: 이 전략의 손절/익절 주문이 걸린 BTCUSDT {side} {qty} 포지션을 기록 없이 발견해 관리 대상으로 복구합니다.")
            else:
                if side != trade["side"] or abs(qty - _f(trade["quantity"])) > 1e-9 or abs(entry - _f(trade["entry_price"])) > 1e-6:
                    ledger.event("RECONCILE_DIFF", network=network, bot=dict(side=trade["side"], qty=trade["quantity"], entry=trade["entry_price"]),
                                 exchange=dict(side=side, qty=qty, entry=entry))
                trade["quantity"] = str(qty)
            ledger.upsert_trade(trade)
            state["trade"] = trade
            state["pending_entry"] = None
            ids = {str(o.get("clientAlgoId") or "") for o in orders}
            if trade.get("sl_client_algo_id") in ids:
                trade["tp_missing"] = trade.get("tp_client_algo_id") not in ids
                self._set_phase(network, mode, state, POSITION_OPEN, trade=trade, reconciled=True)
                return
            state["reconciled"] = True
            await self.attach_protection(network, cfg, state)
            return
        state["pending_entry"] = None
        for order in orders:
            client_id = str(order.get("clientAlgoId") or "")
            if client_id.startswith(self.CLIENT_ID):
                await self.cancel_algo(network, client_id)
                ledger.event("CANCEL_STRAY_PROTECTION", network=network, client_algo_id=client_id)
        state["reconciled"] = True
        if trade:
            await self.finalize_live(network, cfg, state, None)
            return
        self._set_phase(network, mode, state, IDLE, reconciled=True)

    # ------------------------------------------------------------ operator
    async def close_position(self, reason="manual_telegram"):
        cfg = self.config()
        mode, network = self.mode(cfg), self.current_network(cfg)
        if not network:
            return {"action": "none", "reason": "EXCHANGE_MODE_UNSUPPORTED"}
        async with self._cycle_lock:
            state = self.load_state(network, mode)
            trade = state.get("trade")
            if not trade:
                return {"action": "none", "reason": "NO_POSITION"}
            if mode == TRADING_MODE_DRY_RUN:
                price = (await self.premium_index(network))["mark_price"]
                return await self.finalize_paper(network, cfg, state, price, "MANUAL", self.now_ms())
            position = await self.fetch_position(network)
            if not position:
                return await self.finalize_live(network, cfg, state, "MANUAL")
            order_gateway, _ = await self.gateway(network)
            result = await order_gateway.submit_reduce_only_close(
                strategy=self.CLIENT_ID, symbol=CCXT_SYMBOL, position_side=trade["side"].lower(),
                position_signature=trade["signal_bar_ms"], qty=_position_qty(position), reason=reason,
            )
            if result.state == OrderState.CLOSED.value:
                return await self.finalize_live(network, cfg, state, "MANUAL")
            return {"action": "closing", "reason": f"{result.state} {result.error or ''}".strip()}

    def needs_cycle(self):
        """True when the loop must run even with new entries switched off."""
        cfg = self.config()
        network = self.current_network(cfg)
        if not network:
            return False
        if cfg["enabled"]:
            return True
        state = self.load_state(network, self.mode(cfg))
        return bool(state.get("trade") or state.get("pending_entry") or state.get("phase") not in {IDLE, None})

    def reset_reconciliation(self):
        """Force a fresh exchange reconciliation on the next LIVE cycle."""
        cfg = self.config()
        network = self.current_network(cfg)
        if network:
            state = self.load_state(network, TRADING_MODE_LIVE)
            state["reconciled"] = False
            self.save_state(network, TRADING_MODE_LIVE, state)
        self._account_verified.clear()

    def owned_position_keys(self):
        """Market ids whose live position belongs to this strategy (main engine must skip them)."""
        network = self.current_network()
        if not network:
            return set()
        state = self.load_state(network, TRADING_MODE_LIVE)
        return {MARKET_ID} if (state.get("trade") or state.get("pending_entry")) else set()


__all__ = ("BtcEmaPullbackService", "CCXT_SYMBOL", "MARKET_ID")
