"""Persistent and fail-closed Binance Options trading service."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import secrets
import tempfile
import time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN

from .client import BinanceOptionsApiError, BinanceOptionsClient
from .config import OPTIONS_CAPITAL_LIMIT_USDT, normalize_options_config
from .risk import build_long_option_entry_plan, estimate_option_fee
from .strategy import (
    evaluate_underlying_trend,
    score_option_contract,
    shortlist_option_contracts,
)


logger = logging.getLogger(__name__)
BOT_CLIENT_PREFIX = "tbopt"


def _f(value, default=0.0):
    try:
        result = float(value)
        return result if math.isfinite(result) else float(default)
    except (TypeError, ValueError):
        return float(default)


def _rows(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "rows", "positions", "orders", "assets", "asset"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def _first(payload):
    rows = _rows(payload)
    if rows:
        return rows[0] if isinstance(rows[0], dict) else {}
    return payload if isinstance(payload, dict) else {}


def _position_quantity(position):
    for key in ("quantity", "positionAmt", "position", "qty"):
        if key in (position or {}):
            return _f(position.get(key))
    return 0.0


def _order_executed_quantity(order):
    for key in ("executedQty", "executedQuantity", "cumQty", "filledQty", "quantity"):
        value = _f((order or {}).get(key))
        if value > 0:
            if key == "quantity" and str((order or {}).get("status") or "").upper() not in {
                "FILLED",
                "PARTIALLY_FILLED",
            }:
                continue
            return value
    return 0.0


def _order_average_price(order, fallback=0.0):
    for key in ("avgPrice", "averagePrice", "price"):
        value = _f((order or {}).get(key))
        if value > 0:
            return value
    return _f(fallback)


def _option_balance(account):
    candidates = []
    scopes = [account] if isinstance(account, dict) else []
    scopes.extend(_rows(account))
    for scope in scopes:
        if not isinstance(scope, dict):
            continue
        asset = str(scope.get("asset") or scope.get("currency") or scope.get("coin") or "").upper()
        if asset and asset != "USDT":
            continue
        available = None
        equity = None
        for key in ("available", "availableBalance", "availableFunds", "free"):
            if key in scope:
                available = _f(scope.get(key))
                break
        for key in ("equity", "marginBalance", "walletBalance", "balance"):
            if key in scope:
                equity = _f(scope.get(key))
                break
        if available is not None or equity is not None:
            candidates.append(
                {
                    "available": available if available is not None else equity or 0.0,
                    "equity": equity if equity is not None else available or 0.0,
                }
            )
    return candidates[0] if candidates else {"available": 0.0, "equity": 0.0}


def _account_can_trade(account):
    if isinstance(account, dict) and "canTrade" in account:
        return bool(account.get("canTrade"))
    return None


def _default_state():
    return {
        "version": 1,
        "cash_bankroll_usdt": OPTIONS_CAPITAL_LIMIT_USDT,
        "active_position": None,
        "last_scan_ts": 0.0,
        "last_manual_scan_ts": 0.0,
        "last_manage_ts": 0.0,
        "last_manage_success_ts": 0.0,
        "manage_error_streak": 0,
        "pending_entry": None,
        "pending_exit": None,
        "last_reason": "옵션 전략이 아직 실행되지 않았습니다.",
        "last_error": "",
        "last_candidate": None,
        "consumed_signal_keys": [],
        "trades": [],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


class OptionsStateError(RuntimeError):
    pass


class OptionsTradingService:
    def __init__(
        self,
        *,
        config_getter,
        credentials_getter,
        market_data_exchange,
        state_path,
        notifier=None,
        client_factory=None,
    ):
        self.config_getter = config_getter
        self.credentials_getter = credentials_getter
        self.market_data_exchange = market_data_exchange
        self.state_path = os.path.abspath(state_path)
        self.notifier = notifier
        self.client_factory = client_factory or BinanceOptionsClient
        self._client_instance = None
        self._credential_identity = None
        self._exchange_info = None
        self._exchange_info_ts = 0.0
        self._lock = asyncio.Lock()
        self._state_unreadable = False
        self.state = self._load_state()

    def config(self):
        return normalize_options_config(self.config_getter() or {})

    def _load_state(self):
        if not os.path.exists(self.state_path):
            state = _default_state()
            self._save_state(state)
            return state
        try:
            with open(self.state_path, "r", encoding="utf-8") as handle:
                state = json.load(handle)
            if not isinstance(state, dict) or int(state.get("version", 0)) != 1:
                raise ValueError("unsupported options state")
            state.setdefault("cash_bankroll_usdt", OPTIONS_CAPITAL_LIMIT_USDT)
            state["cash_bankroll_usdt"] = max(
                0.0, _f(state.get("cash_bankroll_usdt"), OPTIONS_CAPITAL_LIMIT_USDT)
            )
            state.setdefault("active_position", None)
            state.setdefault("trades", [])
            state.setdefault("last_reason", "")
            state.setdefault("last_error", "")
            state.setdefault("last_candidate", None)
            state.setdefault("consumed_signal_keys", [])
            state.setdefault("last_manual_scan_ts", 0.0)
            state.setdefault("last_manage_success_ts", 0.0)
            state.setdefault("manage_error_streak", 0)
            state.setdefault("pending_entry", None)
            state.setdefault("pending_exit", None)
            return state
        except Exception as exc:
            self._state_unreadable = True
            logger.error("Options state is unreadable; trading fail-closed: %s", exc)
            state = _default_state()
            state["last_error"] = f"OPTIONS_STATE_UNREADABLE: {exc}"
            return state

    def _save_state(self, state=None):
        state = state if state is not None else self.state
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        fd, temp_path = tempfile.mkstemp(
            prefix=".options_state_", suffix=".tmp", dir=os.path.dirname(self.state_path)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.state_path)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    def _client(self):
        creds = self.credentials_getter() or {}
        api_key = str(creds.get("api_key") or "").strip()
        secret_key = str(creds.get("secret_key") or "").strip()
        identity = hashlib.sha256(
            f"{api_key}\0{secret_key}".encode("utf-8")
        ).hexdigest()
        if self._client_instance is None or identity != self._credential_identity:
            self._client_instance = self.client_factory(
                api_key=api_key,
                secret_key=secret_key,
                timeout=self.config().get("request_timeout_seconds", 10),
            )
            self._credential_identity = identity
        return self._client_instance

    async def _call(self, function, *args, **kwargs):
        return await asyncio.to_thread(function, *args, **kwargs)

    async def _notify(self, text):
        if not self.notifier:
            return
        try:
            result = self.notifier(text)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            logger.exception("Options Telegram notification failed")

    async def _private_snapshot(self):
        client = self._client()
        if not client.authenticated:
            raise BinanceOptionsApiError("OPTIONS_API_CREDENTIALS_MISSING")
        await self._call(client.sync_time)
        account, positions, orders = await asyncio.gather(
            self._call(client.margin_account),
            self._call(client.positions),
            self._call(client.open_orders),
        )
        return {
            "account": account,
            "balance": _option_balance(account),
            "can_trade": _account_can_trade(account),
            "positions": [row for row in _rows(positions) if abs(_position_quantity(row)) > 1e-12],
            "orders": _rows(orders),
        }

    async def preflight(self):
        try:
            client = self._client()
            await self._call(client.ping)
            snapshot = await self._private_snapshot()
            return {"ok": True, **snapshot, "error": ""}
        except Exception as exc:
            return {
                "ok": False,
                "account": {},
                "balance": {"available": 0.0, "equity": 0.0},
                "positions": [],
                "orders": [],
                "error": str(exc),
            }

    async def status_snapshot(self, *, refresh=True):
        snapshot = await self.preflight() if refresh else None
        cfg = self.config()
        active = self.state.get("active_position") or None
        return {
            "enabled": bool(cfg.get("enabled")),
            "state_ok": not self._state_unreadable,
            "api_ok": bool(snapshot and snapshot.get("ok")),
            "api_error": (snapshot or {}).get("error", ""),
            "balance": (snapshot or {}).get("balance", {}),
            "can_trade": (snapshot or {}).get("can_trade"),
            "exchange_positions": len((snapshot or {}).get("positions", [])),
            "exchange_orders": len((snapshot or {}).get("orders", [])),
            "cash_bankroll_usdt": _f(self.state.get("cash_bankroll_usdt")),
            "capital_limit_usdt": OPTIONS_CAPITAL_LIMIT_USDT,
            "active_position": active,
            "last_reason": self.state.get("last_reason", ""),
            "last_error": self.state.get("last_error", ""),
            "last_candidate": self.state.get("last_candidate"),
            "last_manage_success_ts": _f(self.state.get("last_manage_success_ts")),
            "manage_error_streak": int(self.state.get("manage_error_streak") or 0),
            "pending_order": bool(
                self.state.get("pending_entry") or self.state.get("pending_exit")
            ),
        }

    async def _recover_runtime_ownership(self):
        """Production subclass hook for crash-safe order/position adoption."""

        return None

    async def _record_manage_health(self, result):
        failed = result.get("action") == "blocked"
        if failed:
            streak = int(self.state.get("manage_error_streak") or 0) + 1
            self.state["manage_error_streak"] = streak
            if streak in {3, 10}:
                await self._notify(
                    "🚨 옵션 포지션 관리 API 오류가 "
                    f"{streak}회 연속 발생했습니다. 신규 진입은 차단되고 기존 포지션 재확인을 계속합니다."
                )
        else:
            self.state["manage_error_streak"] = 0
            self.state["last_manage_success_ts"] = time.time()
            self.state["last_error"] = ""
        self._save_state()
        return result

    async def run_cycle(self, *, force_scan=False, force_exit=False):
        if self._lock.locked():
            return {"action": "busy", "reason": "OPTIONS_CYCLE_ALREADY_RUNNING"}
        async with self._lock:
            cfg = self.config()
            now = time.time()
            if self._state_unreadable:
                return self._record_reason("OPTIONS_STATE_UNREADABLE_FAIL_CLOSED", error=True)
            active = self.state.get("active_position")
            if not active:
                recovered = await self._recover_runtime_ownership()
                if recovered is not None:
                    active = self.state.get("active_position")
                    if not active:
                        return recovered
            if active:
                interval = cfg.get("manage_interval_seconds", 10)
                if force_exit or force_scan or now - _f(self.state.get("last_manage_ts")) >= interval:
                    self.state["last_manage_ts"] = now
                    self._save_state()
                    result = await self._manage_active_position(force_exit=force_exit)
                    return await self._record_manage_health(result)
                return {"action": "waiting", "reason": "OPTIONS_MANAGE_INTERVAL"}
            if force_exit:
                return self._record_reason("청산할 봇 옵션 포지션이 없습니다.")
            if not cfg.get("enabled"):
                return self._record_reason("옵션 자동매매 OFF — 신규 진입하지 않습니다.")
            if force_scan:
                cooldown = cfg.get("manual_scan_cooldown_seconds", 60)
                elapsed = now - _f(self.state.get("last_manual_scan_ts"))
                if elapsed < cooldown:
                    return {
                        "action": "waiting",
                        "reason": f"OPTIONS_MANUAL_SCAN_COOLDOWN ({cooldown - elapsed:.0f}s)",
                    }
                self.state["last_manual_scan_ts"] = now
            elif now - _f(self.state.get("last_scan_ts")) < cfg.get("scan_interval_seconds", 300):
                return {"action": "waiting", "reason": "OPTIONS_SCAN_INTERVAL"}
            self.state["last_scan_ts"] = now
            self._save_state()
            return await self._scan_and_maybe_enter()

    def _record_reason(self, reason, *, error=False, candidate=None):
        self.state["last_reason"] = str(reason)
        if error:
            self.state["last_error"] = str(reason)
        elif candidate is not None:
            self.state["last_candidate"] = candidate
            self.state["last_error"] = ""
        self._save_state()
        return {"action": "blocked" if error else "waiting", "reason": str(reason)}

    async def _get_exchange_info(self):
        now = time.time()
        if self._exchange_info and now - self._exchange_info_ts < 600:
            return self._exchange_info
        payload = await self._call(self._client().exchange_info)
        self._exchange_info = payload
        self._exchange_info_ts = now
        return payload

    @staticmethod
    def _ccxt_symbol(underlying):
        base = str(underlying or "").upper()
        if base.endswith("USDT"):
            base = base[:-4]
        return f"{base}/USDT:USDT"

    async def _underlying_signal(self, underlying, cfg):
        symbol = self._ccxt_symbol(underlying)
        fast, slow = await asyncio.gather(
            asyncio.to_thread(
                self.market_data_exchange.fetch_ohlcv,
                symbol,
                cfg.get("signal_timeframe", "1h"),
                None,
                240,
            ),
            asyncio.to_thread(
                self.market_data_exchange.fetch_ohlcv,
                symbol,
                cfg.get("slow_timeframe", "4h"),
                None,
                160,
            ),
        )
        fast = list(fast or [])
        slow = list(slow or [])
        # Exchange OHLCV responses include the currently forming bar. Options
        # entries consume completed candles only so one signal is immutable.
        result = evaluate_underlying_trend(
            fast[:-1] if len(fast) > 1 else fast,
            slow[:-1] if len(slow) > 1 else slow,
            cfg,
        )
        result["underlying"] = underlying
        result["signal_key"] = (
            f"{underlying}:{result.get('direction')}:{result.get('signal_bar_ts', 0)}"
        )
        return result

    async def _scan_contract(self, contract, signal, cfg):
        symbol = contract.get("symbol")
        mark, ticker, depth = await asyncio.gather(
            self._call(self._client().mark_price, symbol),
            self._call(self._client().ticker, symbol),
            self._call(self._client().depth, symbol, 20),
        )
        scored = score_option_contract(contract, mark, ticker, depth, signal, cfg)
        if not scored.get("accepted"):
            return None
        return {**contract, **scored, "signal": signal}

    async def _scan_and_maybe_enter(self):
        cfg = self.config()
        try:
            snapshot = await self._private_snapshot()
            if snapshot.get("can_trade") is False:
                return self._record_reason(
                    "옵션 계정 canTrade=false — European Options 거래 권한을 다시 확인하세요.",
                    error=True,
                )
            if snapshot["positions"]:
                return self._record_reason(
                    "기존 옵션 포지션이 있어 신규 진입을 차단했습니다. 봇 소유 포지션만 자동 관리합니다."
                )
            if snapshot["orders"]:
                return self._record_reason(
                    "기존 옵션 미체결 주문이 있어 신규 진입을 차단했습니다."
                )
            if _f(snapshot["balance"].get("available")) <= 0:
                return self._record_reason("옵션 계좌 사용 가능 USDT가 없습니다.", error=True)
            exchange_info = await self._get_exchange_info()
            signals = []
            for underlying in cfg.get("underlyings", []):
                try:
                    result = await self._underlying_signal(underlying, cfg)
                    if result.get("accepted"):
                        signals.append(result)
                except Exception as exc:
                    logger.warning("Options underlying signal failed for %s: %s", underlying, exc)
            signals.sort(key=lambda row: abs(_f(row.get("score"))), reverse=True)
            candidates = []
            consumed = set(str(value) for value in self.state.get("consumed_signal_keys") or [])
            for signal in signals[:3]:
                if signal.get("signal_key") in consumed:
                    continue
                contracts = shortlist_option_contracts(
                    exchange_info,
                    signal.get("underlying"),
                    signal.get("direction"),
                    signal.get("spot_price"),
                    cfg,
                )
                for contract in contracts:
                    try:
                        scored = await self._scan_contract(contract, signal, cfg)
                    except Exception as exc:
                        logger.debug("Option contract scan failed for %s: %s", contract.get("symbol"), exc)
                        continue
                    if scored:
                        candidates.append(scored)
            if not candidates:
                return self._record_reason(
                    "추세·IV·델타·스프레드·유동성 조건을 함께 통과한 옵션이 없습니다.",
                    candidate=None,
                )
            candidates.sort(key=lambda row: _f(row.get("score")), reverse=True)
            selected = candidates[0]
            candidate_summary = {
                "symbol": selected.get("symbol"),
                "underlying": selected.get("underlying"),
                "direction": selected.get("side"),
                "signal_score": _f((selected.get("signal") or {}).get("score")),
                "option_score": _f(selected.get("score")),
                "ask": _f(selected.get("ask")),
                "spread_pct": _f(selected.get("spread_pct")),
                "delta": _f(selected.get("delta")),
                "iv_to_realized": _f(selected.get("iv_to_realized")),
            }
            self.state["last_candidate"] = candidate_summary
            self._save_state()
            return await self._enter(selected, snapshot)
        except BinanceOptionsApiError as exc:
            return self._record_reason(f"옵션 API 진입 점검 실패: {exc}", error=True)
        except Exception as exc:
            logger.exception("Options scan cycle failed")
            return self._record_reason(f"옵션 스캔 오류: {exc}", error=True)

    @staticmethod
    def _format_price(value, tick_size):
        tick = Decimal(str(tick_size or "0.0001"))
        price = Decimal(str(value))
        if tick <= 0:
            tick = Decimal("0.0001")
        rounded = (price / tick).to_integral_value(rounding=ROUND_DOWN) * tick
        return format(rounded.normalize(), "f")

    @staticmethod
    def _client_order_id(kind):
        return f"{BOT_CLIENT_PREFIX}{kind}{int(time.time() * 1000)}{secrets.token_hex(2)}"[:32]

    async def _resolve_submitted_order(self, symbol, client_order_id, response, fallback_price):
        order = response if isinstance(response, dict) else {}
        executed = _order_executed_quantity(order)
        if executed > 0:
            return order
        try:
            queried = await self._call(
                self._client().query_order,
                symbol,
                client_order_id=client_order_id,
            )
            return queried if isinstance(queried, dict) else order
        except Exception as exc:
            logger.warning("Options order verification failed for %s: %s", client_order_id, exc)
            if order:
                return order
            raise BinanceOptionsApiError(
                "OPTIONS_ORDER_STATUS_UNKNOWN", uncertain=True
            ) from exc

    async def _enter(self, selected, snapshot):
        cfg = self.config()
        ask = _f(selected.get("ask"))
        tick = _f(selected.get("tick_size"))
        limit_price = ask + (tick if tick > 0 else 0.0)
        plan = build_long_option_entry_plan(
            ask_price=limit_price,
            index_price=(selected.get("signal") or {}).get("spot_price"),
            unit=selected.get("unit", 1),
            min_qty=selected.get("min_qty") or selected.get("minQty"),
            step_size=selected.get("step_size") or selected.get("minQty"),
            cash_bankroll_usdt=self.state.get("cash_bankroll_usdt"),
            entry_fraction=cfg.get("entry_fraction", 0.90),
            capital_limit_usdt=OPTIONS_CAPITAL_LIMIT_USDT,
        )
        if not plan.get("accepted"):
            return self._record_reason(
                f"{OPTIONS_CAPITAL_LIMIT_USDT:.0f} USDT 한도에서 주문 수량을 만들 수 없습니다: {plan.get('reason')}",
                candidate=self.state.get("last_candidate"),
            )
        if plan["total_entry_cost_usdt"] > _f(snapshot["balance"].get("available")):
            return self._record_reason("옵션 계좌 사용 가능 잔고가 계획 금액보다 적습니다.")
        symbol = selected.get("symbol")
        signal_key = str((selected.get("signal") or {}).get("signal_key") or "")
        if signal_key:
            consumed = list(self.state.get("consumed_signal_keys") or [])
            if signal_key not in consumed:
                consumed.append(signal_key)
            self.state["consumed_signal_keys"] = consumed[-200:]
            self._save_state()
        client_order_id = self._client_order_id("b")
        price_text = self._format_price(limit_price, tick)
        try:
            response = await self._call(
                self._client().new_order,
                symbol,
                "BUY",
                "LIMIT",
                plan["quantity"],
                price=price_text,
                time_in_force="IOC",
                reduce_only=False,
                client_order_id=client_order_id,
            )
        except BinanceOptionsApiError as exc:
            if exc.uncertain:
                try:
                    response = await self._call(
                        self._client().query_order,
                        symbol,
                        client_order_id=client_order_id,
                    )
                except Exception:
                    return self._record_reason(
                        "옵션 주문 응답이 불확실해 신규 주문을 중단했습니다. 거래소 주문을 확인하세요.",
                        error=True,
                    )
            else:
                return self._record_reason(f"옵션 진입 주문 거절: {exc}", error=True)
        order = await self._resolve_submitted_order(symbol, client_order_id, response, limit_price)
        filled_qty = _order_executed_quantity(order)
        if filled_qty <= 0:
            return self._record_reason("옵션 IOC 주문이 체결되지 않아 포지션을 만들지 않았습니다.")
        fill_price = _order_average_price(order, limit_price)
        unit = _f(selected.get("unit"), 1.0)
        premium = fill_price * filled_qty * unit
        entry_fee = estimate_option_fee(
            fill_price,
            (selected.get("signal") or {}).get("spot_price"),
            filled_qty,
            unit,
        )
        total_cost = premium + entry_fee
        if total_cost > OPTIONS_CAPITAL_LIMIT_USDT + 1e-8:
            self.state["last_error"] = "OPTIONS_FILLED_COST_EXCEEDED_HARD_CAP"
            self._save_state()
            await self._notify(
                f"🚨 옵션 체결 비용이 {OPTIONS_CAPITAL_LIMIT_USDT:.0f} USDT 한도를 넘었습니다. "
                "추가 진입을 차단하고 현재 포지션만 관리합니다."
            )
        self.state["cash_bankroll_usdt"] = max(
            0.0, _f(self.state.get("cash_bankroll_usdt")) - total_cost
        )
        position = {
            "symbol": symbol,
            "underlying": selected.get("underlying"),
            "side": selected.get("side"),
            "quantity": filled_qty,
            "original_quantity": filled_qty,
            "entry_price": fill_price,
            "entry_premium_usdt": premium,
            "entry_fee_usdt": entry_fee,
            "entry_total_usdt": total_cost,
            "entry_time_ms": int(time.time() * 1000),
            "expiry_date_ms": int(_f(selected.get("expiryDate"))),
            "unit": unit,
            "tick_size": tick,
            "peak_mark": fill_price,
            "client_order_id": client_order_id,
            "order_id": order.get("orderId"),
            "signal_score": _f((selected.get("signal") or {}).get("score")),
            "option_score": _f(selected.get("score")),
            "status": "OPEN",
        }
        self.state["active_position"] = position
        self.state["last_reason"] = f"{symbol} 옵션 매수 체결"
        self.state["last_error"] = ""
        self._save_state()
        await self._notify(
            "\n".join(
                [
                    "🟣 옵션 자동매매 진입",
                    f"종목: {symbol}",
                    f"방향: {selected.get('side')} | 수량 {filled_qty:g}",
                    f"프리미엄: {premium:.4f} USDT | 예상 수수료 {entry_fee:.4f}",
                    f"전략 잔여예산: {self.state['cash_bankroll_usdt']:.4f} / "
                    f"{OPTIONS_CAPITAL_LIMIT_USDT:.4f} USDT",
                    "네이키드 매도 없이 매수 프리미엄만 위험에 노출됩니다.",
                ]
            )
        )
        return {"action": "entered", "position": position}

    async def _resolve_pending_exit(self, position):
        pending = self.state.get("pending_exit") or None
        if not pending:
            return None
        symbol = str(pending.get("symbol") or "")
        client_order_id = str(pending.get("client_order_id") or "")
        if not symbol or not client_order_id.startswith(BOT_CLIENT_PREFIX):
            return self._record_reason("OPTIONS_PENDING_EXIT_INVALID", error=True)
        try:
            order = await self._call(
                self._client().query_order,
                symbol,
                client_order_id=client_order_id,
            )
        except Exception as exc:
            return self._record_reason(
                f"옵션 미확정 청산 주문 확인 실패: {exc}", error=True
            )
        status = str((order or {}).get("status") or "").upper()
        filled = _order_executed_quantity(order)
        if filled > 0:
            return await self._book_exit_order(
                position,
                order,
                _f(pending.get("limit_price")),
                pending.get("reason") or "OPTION_RECOVERED_EXIT",
            )
        if status in {"CANCELED", "REJECTED", "EXPIRED", "FILLED"}:
            self.state["pending_exit"] = None
            self._save_state()
            return None
        return {
            "action": "waiting",
            "reason": "옵션 청산 주문 상태 확인 중 — 중복 청산을 차단했습니다.",
        }

    async def _manage_active_position(self, *, force_exit=False, prefetched=None):
        position = self.state.get("active_position") or {}
        symbol = position.get("symbol")
        if not symbol:
            return self._record_reason("OPTIONS_ACTIVE_POSITION_INVALID", error=True)
        pending_result = await self._resolve_pending_exit(position)
        if pending_result is not None:
            return pending_result
        try:
            prefetched = prefetched or {}
            exchange_positions = prefetched.get("exchange_positions")
            if exchange_positions is None:
                exchange_positions = _rows(
                    await self._call(self._client().positions, symbol)
                )
            exchange_qty = 0.0
            for row in exchange_positions:
                if str(row.get("symbol") or "") == symbol:
                    exchange_qty = max(0.0, _position_quantity(row))
                    break
            tracked_qty = max(0.0, _f(position.get("quantity")))
            if exchange_qty + 1e-12 < tracked_qty:
                await self._reconcile_external_reduction(position, exchange_qty)
                position = self.state.get("active_position") or position
            if exchange_qty <= 1e-12:
                return await self._finalize_trade(
                    position,
                    exit_price=_f(position.get("last_external_exit_price")),
                    exit_quantity=tracked_qty,
                    proceeds=0.0,
                    exit_fee=0.0,
                    reason="EXTERNAL_CLOSE_OR_EXPIRY",
                )
            mark_payload = prefetched.get("mark_payload")
            depth = prefetched.get("depth")
            if mark_payload is None or depth is None:
                mark_payload, depth = await asyncio.gather(
                    self._call(self._client().mark_price, symbol),
                    self._call(self._client().depth, symbol, 20),
                )
            mark = _first(mark_payload)
            mark_price = _f(mark.get("markPrice"), position.get("entry_price"))
            bids = list((depth or {}).get("bids") or [])
            bid = _f(bids[0][0]) if bids else mark_price
            entry_price = max(1e-12, _f(position.get("entry_price")))
            pnl_pct = mark_price / entry_price - 1.0
            peak = max(_f(position.get("peak_mark"), entry_price), mark_price)
            position["peak_mark"] = peak
            position["last_mark"] = mark_price
            position["last_pnl_pct"] = pnl_pct
            self.state["active_position"] = position
            self._save_state()

            cfg = self.config()
            now_ms = int(time.time() * 1000)
            reason = ""
            if force_exit:
                reason = "TELEGRAM_FORCE_CLOSE"
            elif pnl_pct <= -_f(cfg.get("stop_loss_pct"), 0.45):
                reason = "OPTION_PREMIUM_STOP"
            elif pnl_pct >= _f(cfg.get("take_profit_pct"), 0.80):
                reason = "OPTION_PREMIUM_TARGET"
            elif peak >= entry_price * (1.0 + _f(cfg.get("trail_activation_pct"), 0.35)) and mark_price <= peak * (1.0 - _f(cfg.get("trail_drawdown_pct"), 0.25)):
                reason = "OPTION_PREMIUM_TRAIL"
            elif now_ms - int(position.get("entry_time_ms") or now_ms) >= _f(cfg.get("max_hold_hours"), 72.0) * 3_600_000:
                reason = "OPTION_TIME_STOP"
            elif int(position.get("expiry_date_ms") or 0) - now_ms <= _f(cfg.get("expiry_exit_hours"), 8.0) * 3_600_000:
                reason = "OPTION_EXPIRY_GUARD"
            if not reason:
                self.state["last_reason"] = (
                    f"{symbol} 보유 관리 중 — 프리미엄 {pnl_pct * 100:+.1f}%"
                )
                self._save_state()
                return {"action": "managed", "reason": self.state["last_reason"]}
            managed_qty = min(exchange_qty, _f(position.get("quantity")))
            if managed_qty <= 1e-12:
                return self._record_reason(
                    "거래소 수량과 봇 관리 수량이 일치하지 않아 청산을 차단했습니다.",
                    error=True,
                )
            return await self._exit_position(position, managed_qty, bid, reason)
        except BinanceOptionsApiError as exc:
            return self._record_reason(f"옵션 포지션 관리 API 오류: {exc}", error=True)
        except Exception as exc:
            logger.exception("Options position management failed")
            return self._record_reason(f"옵션 포지션 관리 오류: {exc}", error=True)

    async def _exit_position(self, position, quantity, best_bid, reason):
        symbol = position.get("symbol")
        tick = _f(position.get("tick_size"))
        limit_price = max(tick, best_bid - (tick if tick > 0 else 0.0))
        client_order_id = self._client_order_id("s")
        price_text = self._format_price(limit_price, tick)
        self.state["pending_exit"] = {
            "symbol": symbol,
            "quantity": _f(quantity),
            "limit_price": limit_price,
            "client_order_id": client_order_id,
            "reason": reason,
            "created_at_ms": int(time.time() * 1000),
        }
        self._save_state()
        try:
            response = await self._call(
                self._client().new_order,
                symbol,
                "SELL",
                "LIMIT",
                format(Decimal(str(quantity)).normalize(), "f"),
                price=price_text,
                time_in_force="IOC",
                reduce_only=True,
                client_order_id=client_order_id,
            )
        except BinanceOptionsApiError as exc:
            if exc.uncertain:
                try:
                    response = await self._call(
                        self._client().query_order,
                        symbol,
                        client_order_id=client_order_id,
                    )
                except Exception:
                    return self._record_reason(
                        "옵션 청산 주문 상태가 불확실합니다. 중복 청산을 막고 다음 주기에 재확인합니다.",
                        error=True,
                    )
            else:
                self.state["pending_exit"] = None
                self._save_state()
                return self._record_reason(f"옵션 청산 주문 거절: {exc}", error=True)
        order = await self._resolve_submitted_order(symbol, client_order_id, response, limit_price)
        return await self._book_exit_order(
            position, order, limit_price, reason
        )

    async def _book_exit_order(self, position, order, limit_price, reason):
        filled = _order_executed_quantity(order)
        if filled <= 0:
            status = str((order or {}).get("status") or "").upper()
            if status in {"CANCELED", "REJECTED", "EXPIRED", "FILLED"}:
                self.state["pending_exit"] = None
                self._save_state()
            return self._record_reason("옵션 청산 IOC가 미체결되어 다음 관리 주기에 재시도합니다.")
        exit_price = _order_average_price(order, limit_price)
        unit = _f(position.get("unit"), 1.0)
        proceeds = exit_price * filled * unit
        # With no underlying quote in this branch, use the 10%-of-premium fee
        # cap. It over-reserves rather than silently consuming the fixed sleeve.
        exit_fee = estimate_option_fee(exit_price, 0.0, filled, unit)
        remaining = max(0.0, _f(position.get("quantity")) - filled)
        order_id = order.get("orderId")
        if order_id is not None:
            accounted = list(position.get("accounted_exit_order_ids") or [])
            order_id_text = str(order_id)
            if order_id_text not in accounted:
                accounted.append(order_id_text)
            position["accounted_exit_order_ids"] = accounted[-100:]
        self.state["cash_bankroll_usdt"] = max(
            0.0, _f(self.state.get("cash_bankroll_usdt")) + proceeds - exit_fee
        )
        self.state["pending_exit"] = None
        if remaining > 1e-12:
            position["quantity"] = remaining
            position["partial_exit_proceeds_usdt"] = _f(
                position.get("partial_exit_proceeds_usdt")
            ) + proceeds
            position["partial_exit_fees_usdt"] = _f(
                position.get("partial_exit_fees_usdt")
            ) + exit_fee
            position["reconcile_after_ms"] = int(time.time() * 1000)
            self.state["active_position"] = position
            self.state["last_reason"] = f"옵션 부분 청산 {filled:g}, 잔여 {remaining:g}"
            self._save_state()
            return {"action": "partial_exit", "remaining": remaining}
        return await self._finalize_trade(
            position,
            exit_price=exit_price,
            exit_quantity=filled,
            proceeds=proceeds,
            exit_fee=exit_fee,
            reason=reason,
        )

    async def _finalize_trade(self, position, *, exit_price, exit_quantity, proceeds, exit_fee, reason):
        proceeds += _f(position.get("partial_exit_proceeds_usdt"))
        exit_fee += _f(position.get("partial_exit_fees_usdt"))
        entry_cost = _f(position.get("entry_total_usdt"))
        pnl = proceeds - exit_fee - entry_cost
        trade = {
            "symbol": position.get("symbol"),
            "side": position.get("side"),
            "quantity": _f(position.get("original_quantity"), exit_quantity),
            "entry_price": _f(position.get("entry_price")),
            "exit_price": exit_price,
            "entry_total_usdt": entry_cost,
            "exit_proceeds_usdt": proceeds,
            "exit_fee_usdt": exit_fee,
            "pnl_usdt": pnl,
            "reason": reason,
            "closed_at": datetime.now(timezone.utc).isoformat(),
        }
        trades = list(self.state.get("trades") or [])
        trades.append(trade)
        self.state["trades"] = trades[-200:]
        self.state["active_position"] = None
        self.state["last_reason"] = f"{position.get('symbol')} 청산 완료 ({reason})"
        self.state["last_error"] = ""
        self._save_state()
        await self._notify(
            "\n".join(
                [
                    "🟣 옵션 자동매매 청산",
                    f"종목: {position.get('symbol')}",
                    f"사유: {reason}",
                    f"실현손익(추정 수수료 포함): {pnl:+.4f} USDT",
                    f"전략 잔여예산: {self.state['cash_bankroll_usdt']:.4f} / "
                    f"{OPTIONS_CAPITAL_LIMIT_USDT:.4f} USDT",
                ]
            )
        )
        return {"action": "exited", "trade": trade}

    async def _reconcile_external_reduction(self, position, exchange_quantity):
        """Book only the unaccounted part of a manual close or expiry.

        A user can reduce a bot-owned option directly on Binance.  Exchange
        quantity is authoritative, while trade/order identifiers prevent a
        bot IOC fill that was already booked from being credited twice.
        """
        symbol = position.get("symbol")
        tracked_quantity = max(0.0, _f(position.get("quantity")))
        missing_quantity = max(0.0, tracked_quantity - max(0.0, exchange_quantity))
        if missing_quantity <= 1e-12:
            return position
        start_time = int(position.get("entry_time_ms") or 0)
        proceeds = 0.0
        exit_fee = 0.0
        sold_qty = 0.0
        weighted = 0.0
        processed = set(str(value) for value in position.get("processed_exit_trade_ids") or [])
        accounted_orders = set(
            str(value) for value in position.get("accounted_exit_order_ids") or []
        )
        newly_processed = []
        try:
            trades = _rows(
                await self._call(
                    self._client().user_trades,
                    symbol,
                    start_time=start_time,
                    limit=100,
                )
            )
            for row in trades:
                if str(row.get("side") or "").upper() != "SELL":
                    continue
                trade_key = str(
                    row.get("id")
                    or row.get("tradeId")
                    or (
                        f"{row.get('orderId')}:{row.get('time')}:{row.get('price')}:"
                        f"{row.get('quantity') or row.get('qty')}"
                    )
                )
                if trade_key in processed:
                    continue
                newly_processed.append(trade_key)
                if str(row.get("orderId")) in accounted_orders:
                    continue
                qty = max(0.0, _f(row.get("quantity") or row.get("qty")))
                accepted_qty = min(qty, max(0.0, missing_quantity - sold_qty))
                if accepted_qty <= 1e-12:
                    continue
                price = _f(row.get("price"))
                ratio = accepted_qty / qty if qty > 0 else 0.0
                sold_qty += accepted_qty
                weighted += accepted_qty * price
                quote_value = _f(row.get("quoteQty"), qty * price)
                proceeds += quote_value * ratio * _f(position.get("unit"), 1.0)
                exit_fee += abs(_f(row.get("fee") or row.get("commission"))) * ratio
        except Exception as exc:
            logger.warning("Options external-close trade reconciliation failed: %s", exc)
        if sold_qty <= 1e-12 and exchange_quantity <= 1e-12:
            try:
                records = _rows(
                    await self._call(
                        self._client().exercise_records,
                        symbol,
                        start_time=start_time,
                        limit=100,
                    )
                )
                for row in records:
                    if str(row.get("symbol") or "") != symbol:
                        continue
                    record_key = str(
                        row.get("id")
                        or row.get("exerciseId")
                        or f"exercise:{row.get('time')}:{row.get('amount')}"
                    )
                    if record_key in processed:
                        continue
                    newly_processed.append(record_key)
                    proceeds += max(
                        0.0,
                        _f(
                            row.get("amount")
                            or row.get("exerciseAmount")
                            or row.get("settleAmount")
                            or row.get("profit"),
                        ),
                    )
                    exit_fee += abs(_f(row.get("fee") or row.get("exerciseFee")))
            except Exception as exc:
                logger.warning("Options exercise reconciliation failed: %s", exc)
        booked_qty = sold_qty if sold_qty > 1e-12 else missing_quantity
        if sold_qty > 1e-12:
            exit_price = weighted / sold_qty
        elif proceeds > 0:
            exit_price = proceeds / max(
                missing_quantity * _f(position.get("unit"), 1.0), 1e-12
            )
        else:
            exit_price = 0.0
        self.state["cash_bankroll_usdt"] = max(
            0.0,
            _f(self.state.get("cash_bankroll_usdt")) + proceeds - exit_fee,
        )
        position["quantity"] = max(0.0, exchange_quantity)
        position["partial_exit_proceeds_usdt"] = _f(
            position.get("partial_exit_proceeds_usdt")
        ) + proceeds
        position["partial_exit_fees_usdt"] = _f(
            position.get("partial_exit_fees_usdt")
        ) + exit_fee
        position["processed_exit_trade_ids"] = list(processed.union(newly_processed))[-200:]
        position["last_external_exit_price"] = exit_price
        position["last_external_exit_quantity"] = booked_qty
        position["reconcile_after_ms"] = int(time.time() * 1000)
        self.state["active_position"] = position
        self._save_state()
        return position


__all__ = (
    "BOT_CLIENT_PREFIX",
    "OptionsStateError",
    "OptionsTradingService",
)
