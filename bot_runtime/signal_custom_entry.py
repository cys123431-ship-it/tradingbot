"""User-directed futures entries with the bot's existing risk controls."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from types import SimpleNamespace

from utbreakout.engine_router import LadderTP, TradeDecision

from .live_context import _closed_live_ohlcv_rows
from .live_risk import (
    InvalidOrderPlan,
    TradingSafetyError,
    _normalize_live_real_stage,
    apply_runtime_safety_defaults,
    build_live_order_plan_from_decision,
    enforce_activation_stage,
    enforce_live_real_order_caps,
    validate_live_order_plan,
)


logger = logging.getLogger(__name__)


class SignalCustomEntryMixin:
    """Build and execute a manual market entry without a strategy signal."""

    def get_user_custom_entry_settings(self):
        config_owner = getattr(getattr(self, "ctrl", None), "cfg", None)
        if config_owner is None or not hasattr(config_owner, "get"):
            return {}
        signal_cfg = config_owner.get("signal_engine", {}) or {}
        raw = signal_cfg.get("user_custom_entry", {}) or {}
        return raw if isinstance(raw, dict) else {}

    def is_user_custom_entry_mode_enabled(self):
        return bool(self.get_user_custom_entry_settings().get("enabled", False))

    def _build_user_custom_live_config(self):
        common = dict(self.get_runtime_common_settings() or {})
        strategy_params = self.get_runtime_strategy_params()
        filtered = dict(self._get_utbot_filtered_breakout_config(strategy_params) or {})
        custom = self.get_user_custom_entry_settings()

        cfg = dict(filtered)
        cfg.update(common)
        cfg.update(
            {
                "timeframe": str(
                    custom.get("timeframe")
                    or common.get("entry_timeframe")
                    or common.get("timeframe")
                    or "15m"
                ),
                "adx_length": max(
                    2,
                    int(
                        custom.get("atr_period")
                        or filtered.get("atr_period")
                        or filtered.get("utbot_atr_period")
                        or 14
                    ),
                ),
                "initial_stop_atr_distance": max(
                    0.1,
                    float(
                        custom.get(
                            "stop_atr_multiplier",
                            filtered.get("stop_atr_multiplier", 1.5),
                        )
                        or 1.5
                    ),
                ),
                "entry_order_type": "MARKET",
                "max_spread_bps": max(
                    0.1,
                    float(custom.get("max_spread_pct", 0.08) or 0.08) * 100.0,
                ),
            }
        )
        cfg = apply_runtime_safety_defaults(cfg)
        return enforce_activation_stage(cfg)

    @staticmethod
    def _scale_user_custom_plan_qty(plan, new_qty):
        old_qty = float(getattr(plan, "qty", 0.0) or 0.0)
        new_qty = float(new_qty or 0.0)
        if old_qty <= 0 or new_qty <= 0:
            raise InvalidOrderPlan("custom entry quantity is unavailable")
        ratio = min(1.0, new_qty / old_qty)
        plan.qty = old_qty * ratio
        for tp in list(getattr(plan, "tp_orders", []) or []):
            tp.qty = float(tp.qty) * ratio
        return plan

    async def _assert_user_custom_flat_state(self):
        if not hasattr(self.exchange, "fetch_positions"):
            raise TradingSafetyError("open futures positions cannot be verified")
        try:
            positions = await asyncio.to_thread(self.exchange.fetch_positions)
        except Exception as exc:
            raise TradingSafetyError(
                f"open futures positions cannot be verified: {type(exc).__name__}: {exc}"
            ) from exc
        active = [
            pos
            for pos in (positions or [])
            if abs(float(self._position_signed_contracts(pos) or 0.0)) > 0
        ]
        if active:
            symbol = str(active[0].get("symbol") or "unknown")
            raise TradingSafetyError(
                f"global single-position protection: an open position already exists ({symbol})"
            )

        if not hasattr(self.exchange, "fetch_open_orders"):
            raise TradingSafetyError("open futures orders cannot be verified")
        try:
            open_orders = await asyncio.to_thread(self.exchange.fetch_open_orders)
        except Exception as exc:
            raise TradingSafetyError(
                f"open futures orders cannot be verified: {type(exc).__name__}: {exc}"
            ) from exc
        if open_orders:
            raise TradingSafetyError(
                "open futures orders already exist; reconcile or cancel them before a custom entry"
            )

    async def prepare_user_custom_entry(self, symbol, side):
        """Return a fresh, non-submitted market order plan for user confirmation."""

        if not self.is_user_custom_entry_mode_enabled():
            raise TradingSafetyError("USER_CUSTOM_MODE_OFF")
        if bool(getattr(self.ctrl, "is_paused", False)):
            raise TradingSafetyError("the bot is paused; resume it before entering")
        if self.is_upbit_mode():
            raise TradingSafetyError("user custom entry currently supports Binance Futures only")

        normalized_side = str(side or "").strip().lower()
        if normalized_side not in {"long", "short"}:
            raise ValueError("side must be long or short")

        cfg = self._build_user_custom_live_config()
        symbol = await self.ctrl._assert_symbol_tradeable_in_current_exchange_mode(symbol)

        if await self.check_daily_loss_limit():
            raise TradingSafetyError("daily loss limit reached")

        stage = _normalize_live_real_stage(cfg.get("live_activation_stage"))
        if stage == "LIVE_REAL_SMALL_CAP":
            # The owner-selected symbol is the allowlist for this one request;
            # configured blocklists and leveraged/unsupported symbol guards remain.
            cfg["live_real_allowlist"] = [symbol]
            preflight = await self.preflight_live_real_check(symbol, cfg)
            account_equity = float(preflight.get("account_equity") or 0.0)
        else:
            await self._assert_user_custom_flat_state()
            total, _, _ = await self.get_balance_info()
            account_equity = float(total or 0.0)

        total_balance, free_balance, mmr = await self.get_balance_info()
        if account_equity <= 0:
            account_equity = float(total_balance or 0.0)
        if account_equity <= 0 or float(free_balance or 0.0) <= 0:
            raise TradingSafetyError("real futures balance or available margin is zero")

        settings = self.get_user_custom_entry_settings()
        if bool(settings.get("require_quote_volume_gate", True)):
            liquidity = await self._validate_mainnet_entry_quote_volume(symbol)
            if not liquidity.get("allowed"):
                raise TradingSafetyError(
                    str(liquidity.get("reason") or liquidity.get("status") or "quote volume rejected")
                )
        else:
            liquidity = {"allowed": True, "status": "DISABLED"}

        market_exchange = getattr(self, "market_data_exchange", None) or self.exchange
        try:
            ticker = await asyncio.to_thread(market_exchange.fetch_ticker, symbol)
        except Exception as exc:
            raise TradingSafetyError(
                f"current market price cannot be fetched: {type(exc).__name__}: {exc}"
            ) from exc
        ticker = ticker if isinstance(ticker, dict) else {}
        price = float(
            ticker.get("last")
            or ticker.get("close")
            or ticker.get("bid")
            or ticker.get("ask")
            or 0.0
        )
        if price <= 0:
            raise TradingSafetyError("current market price is unavailable")

        bid = float(ticker.get("bid") or 0.0)
        ask = float(ticker.get("ask") or 0.0)
        spread_pct = None
        if bid > 0 and ask > bid:
            spread_pct = ((ask - bid) / ((ask + bid) / 2.0)) * 100.0
            max_spread_pct = float(settings.get("max_spread_pct", 0.08) or 0.08)
            if spread_pct > max_spread_pct:
                raise TradingSafetyError(
                    f"spread safety rejected {spread_pct:.4f}% > {max_spread_pct:.4f}%"
                )

        l2_gate = await self._evaluate_shared_l2_gate(
            symbol,
            cfg,
            force_refresh=True,
            side=normalized_side,
        )
        if bool(settings.get("require_orderbook_gate", True)):
            state = str(l2_gate.get("state") or "unavailable").lower()
            if state in {"unavailable", "disabled"} or not l2_gate.get("allowed", False):
                raise TradingSafetyError(
                    f"orderbook safety rejected: {l2_gate.get('reason') or state}"
                )
        l2_spread_pct = l2_gate.get("spread_pct")
        if spread_pct is None and l2_spread_pct is not None:
            spread_pct = float(l2_spread_pct)
        if spread_pct is not None:
            max_spread_pct = float(settings.get("max_spread_pct", 0.08) or 0.08)
            if spread_pct > max_spread_pct:
                raise TradingSafetyError(
                    f"spread safety rejected {spread_pct:.4f}% > {max_spread_pct:.4f}%"
                )

        rows = await self._fetch_live_ohlcv_rows_for_context(symbol, cfg)
        rows = _closed_live_ohlcv_rows(rows, cfg)
        if len(rows) < int(cfg.get("adx_length", 14) or 14) + 2:
            raise TradingSafetyError("not enough closed candles to calculate ATR risk")
        indicators = self._calculate_live_adx_dmi(rows, cfg)
        atr = float(indicators.get("atr") or 0.0)
        if atr <= 0:
            raise TradingSafetyError("ATR risk distance is unavailable")

        tp1_r = max(
            0.1,
            float(cfg.get("live_tp1_rr", cfg.get("partial_take_profit_r_multiple", 1.0)) or 1.0),
        )
        tp2_r = max(
            2.0,
            float(cfg.get("live_tp2_rr", cfg.get("second_take_profit_r_multiple", 3.5)) or 3.5),
        )
        tp1_ratio = max(0.0, float(cfg.get("live_tp1_pct", 0.25) or 0.25))
        tp2_ratio = max(0.0, float(cfg.get("live_tp2_pct", 0.75) or 0.75))
        ratio_total = tp1_ratio + tp2_ratio
        if ratio_total <= 0:
            tp1_ratio, tp2_ratio, ratio_total = 0.25, 0.75, 1.0
        ladder = LadderTP(
            tp1_r=tp1_r,
            tp1_pct=tp1_ratio / ratio_total * 100.0,
            tp2_r=tp2_r,
            tp2_pct=tp2_ratio / ratio_total * 100.0,
        )

        risk_pct = float(
            cfg.get("risk_per_trade_pct")
            or cfg.get("risk_per_trade_percent")
            or 0.0
        )
        decision = TradeDecision(
            valid=True,
            side=normalized_side.upper(),
            engine="USER_CUSTOM",
            risk_pct=risk_pct,
            ladder_tp=ladder,
            runner_enabled=False,
            regime="USER_DIRECTED",
            confidence=1.0,
            expected_r=tp2_r,
            reasons=[
                "USER_DIRECTED_MARKET_ENTRY",
                "MAX_TRADE_COUNT_BYPASSED",
                "BOT_RISK_AND_PROTECTION_REQUIRED",
            ],
        )
        context = SimpleNamespace(close=price, atr=atr)
        plan = build_live_order_plan_from_decision(
            symbol,
            decision,
            context,
            cfg,
            account_equity=account_equity,
        )
        plan.entry_type = "MARKET"
        plan.entry_price = price
        plan = enforce_live_real_order_caps(plan, context, cfg)
        l2_risk_multiplier = max(
            0.0,
            min(1.0, float(l2_gate.get("risk_multiplier", 1.0) or 0.0)),
        )
        if l2_risk_multiplier <= 0:
            raise TradingSafetyError("orderbook risk multiplier blocks entry")
        if l2_risk_multiplier < 1.0:
            plan = self._scale_user_custom_plan_qty(
                plan,
                float(plan.qty) * l2_risk_multiplier,
            )

        leverage = max(1.0, float(cfg.get("leverage", 1.0) or 1.0))
        margin_notional_cap = float(free_balance) * leverage * 0.98
        configured_notional_cap = float(cfg.get("max_real_position_notional_usdt", 0.0) or 0.0)
        if configured_notional_cap > 0:
            margin_notional_cap = min(margin_notional_cap, configured_notional_cap)
        planned_notional = float(plan.qty) * price
        if margin_notional_cap > 0 and planned_notional > margin_notional_cap:
            plan = self._scale_user_custom_plan_qty(plan, margin_notional_cap / price)

        cfg["last_price"] = price
        cfg["last_context_atr"] = atr
        validate_live_order_plan(plan, cfg)
        min_notional = float(self._get_min_notional_for_symbol(symbol, cfg) or 0.0)
        if float(plan.qty) * price < min_notional:
            raise InvalidOrderPlan(
                f"risk-sized order notional is below exchange minimum ({min_notional:.4f} USDT)"
            )

        plan.user_custom_entry = True
        plan.max_trade_count_bypassed = True
        plan.l2_risk_multiplier = l2_risk_multiplier
        plan.signal_timestamp = f"user:{time.time_ns()}:{uuid.uuid4().hex[:12]}"
        plan.account_equity_usdt = account_equity
        plan.available_margin_usdt = float(free_balance)
        plan.mmr_pct = float(mmr or 0.0)
        return {
            "symbol": symbol,
            "side": normalized_side,
            "price": price,
            "atr": atr,
            "spread_pct": spread_pct,
            "liquidity": liquidity,
            "l2_gate": l2_gate,
            "plan": plan,
            "cfg": cfg,
        }

    async def execute_user_custom_entry(self, symbol, side):
        """Rebuild the plan at current prices and submit it once."""

        lock = getattr(self, "user_custom_entry_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self.user_custom_entry_lock = lock

        async with lock:
            prepared = await self.prepare_user_custom_entry(symbol, side)
            result = await self.execute_live_order_plan(
                prepared["plan"],
                prepared["cfg"],
            )
            if result.get("status") == "LIVE_ORDER_PLAN_EXECUTED":
                actual_plan = result.get("plan") or prepared["plan"]
                try:
                    self.db.log_trade_entry(
                        actual_plan.symbol,
                        str(actual_plan.side).lower(),
                        float(actual_plan.entry_price or prepared["price"]),
                        float(actual_plan.qty),
                        strategy="user_custom",
                    )
                except Exception:
                    logger.exception(
                        "USER_CUSTOM entry was filled but its monthly-report DB row could not be written"
                    )
                self.last_entry_reason[actual_plan.symbol] = (
                    "USER_CUSTOM market entry executed with bot risk controls"
                )
                if isinstance(getattr(self, "active_symbols", None), set):
                    self.active_symbols.add(actual_plan.symbol)
                self.last_live_entry_snapshot = {
                    "symbol": actual_plan.symbol,
                    "side": str(actual_plan.side).lower(),
                    "strategy": "USER_CUSTOM",
                    "entry_price": float(actual_plan.entry_price or prepared["price"]),
                    "qty": float(actual_plan.qty),
                    "stop_loss": float(actual_plan.initial_sl_price),
                    "tp_orders": [
                        {
                            "label": tp.tp_label or tp.tp_name,
                            "price": float(tp.price),
                            "qty": float(tp.qty),
                        }
                        for tp in list(actual_plan.tp_orders or [])
                    ],
                }
            return result


__all__ = ("SignalCustomEntryMixin",)
