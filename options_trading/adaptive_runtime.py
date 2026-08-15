"""Adaptive option runtime layered on the existing fail-closed execution service.

The base runtime continues to own order submission, reconciliation, the $20 hard
cap, long-only entry semantics and reduce-only exits.  This subclass changes
only signal/candidate selection, scan diagnostics and optional early exits.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter

from . import runtime as base_runtime
from .config import OPTIONS_CAPITAL_LIMIT_USDT
from .risk import build_long_option_entry_plan
from .strategy import (
    choose_underlying_signal,
    find_skew_peer,
    score_option_contract,
    shortlist_option_contracts_with_diagnostics,
)


logger = logging.getLogger(__name__)

SCAN_OUTCOME_LABELS = {
    "DIRECTION_SIGNAL": "방향 신호 부족",
    "DTE": "DTE 부적합",
    "DELTA": "Delta 부적합",
    "IV": "IV/IV-RV 과열",
    "SPREAD": "Spread 초과",
    "LIQUIDITY": "유동성 부족",
    "BUDGET": "20 USDT 예산/최소수량 문제",
    "EXISTING_POSITION": "기존 옵션 포지션",
    "OPEN_ORDER": "기존 미체결 옵션 주문",
    "CAN_TRADE": "옵션 거래 권한/API 상태",
    "API": "API/시장데이터 오류",
    "OTHER": "기타 fail-closed 조건",
    "ORDERABLE_CANDIDATE": "주문 가능한 후보 발견",
}


def _scan_category(reason):
    text = str(reason or "").upper()
    if "DELTA" in text:
        return "DELTA"
    if "IV" in text:
        return "IV"
    if "SPREAD" in text or "ORDERBOOK" in text:
        return "SPREAD"
    if "VOLUME" in text or "DEPTH" in text or "LIQUID" in text:
        return "LIQUIDITY"
    if "MINIMUM" in text or "HARD_CAP" in text or "BUDGET" in text or "PREMIUM_PLUS_FEE" in text:
        return "BUDGET"
    if "DTE" in text or "EXPIR" in text:
        return "DTE"
    return "OTHER"


class OptionsTradingService(base_runtime.OptionsTradingService):
    """Selection/diagnostics upgrade that deliberately reuses base execution."""

    def _load_state(self):
        state = super()._load_state()
        state.setdefault("recent_scan_outcomes", [])
        if not isinstance(state.get("recent_scan_outcomes"), list):
            state["recent_scan_outcomes"] = []
        state.setdefault("last_scan_diagnostics", {})
        return state

    def _record_scan_outcome(self, category, *, diagnostics=None):
        category = category if category in SCAN_OUTCOME_LABELS else "OTHER"
        window = int(self.config().get("rejection_stats_window", 100) or 100)
        history = list(self.state.get("recent_scan_outcomes") or [])
        history.append(category)
        self.state["recent_scan_outcomes"] = history[-window:]
        if diagnostics is not None:
            self.state["last_scan_diagnostics"] = diagnostics
        self._save_state()

    async def status_snapshot(self, *, refresh=True):
        status = await super().status_snapshot(refresh=refresh)
        history = list(self.state.get("recent_scan_outcomes") or [])
        counts = Counter(history)
        status["scan_outcomes_window"] = len(history)
        status["scan_rejection_stats"] = {
            code: int(counts.get(code, 0)) for code in SCAN_OUTCOME_LABELS
        }
        status["scan_outcome_labels"] = dict(SCAN_OUTCOME_LABELS)
        status["last_scan_diagnostics"] = self.state.get("last_scan_diagnostics") or {}
        return status

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
        result = choose_underlying_signal(
            fast[:-1] if len(fast) > 1 else fast,
            slow[:-1] if len(slow) > 1 else slow,
            cfg,
        )
        result["underlying"] = underlying
        result["signal_key"] = (
            f"{underlying}:{result.get('strategy')}:{result.get('direction')}:{result.get('signal_bar_ts', 0)}"
        )
        return result

    async def _scan_contract_adaptive(self, contract, signal, cfg, snapshot, exchange_info):
        symbol = contract.get("symbol")
        mark, ticker, depth = await asyncio.gather(
            self._call(self._client().mark_price, symbol),
            self._call(self._client().ticker, symbol),
            self._call(self._client().depth, symbol, 20),
        )
        peer = find_skew_peer(exchange_info, contract)
        peer_mark = None
        if peer and peer.get("symbol"):
            try:
                peer_mark = await self._call(self._client().mark_price, peer.get("symbol"))
            except Exception as exc:
                logger.debug("Option skew peer mark unavailable for %s: %s", symbol, exc)

        scored = score_option_contract(
            contract,
            mark,
            ticker,
            depth,
            signal,
            cfg,
            skew_mark_payload=peer_mark,
        )
        if not scored.get("accepted"):
            return None, _scan_category(scored.get("reason")), scored.get("reason")

        ask = base_runtime._f(scored.get("ask"))
        tick = base_runtime._f(contract.get("tick_size"))
        limit_price = ask + (tick if tick > 0 else 0.0)
        available = max(0.0, base_runtime._f((snapshot.get("balance") or {}).get("available")))
        bankroll = min(
            max(0.0, base_runtime._f(self.state.get("cash_bankroll_usdt"))),
            available,
            OPTIONS_CAPITAL_LIMIT_USDT,
        )
        plan = build_long_option_entry_plan(
            ask_price=limit_price,
            index_price=signal.get("spot_price"),
            unit=contract.get("unit", 1),
            min_qty=contract.get("min_qty") or contract.get("minQty"),
            step_size=contract.get("step_size") or contract.get("minQty"),
            cash_bankroll_usdt=bankroll,
            entry_fraction=cfg.get("entry_fraction", 0.90),
            capital_limit_usdt=OPTIONS_CAPITAL_LIMIT_USDT,
        )
        if not plan.get("accepted"):
            return None, "BUDGET", plan.get("reason")
        if plan.get("total_entry_cost_usdt", 0.0) > available + 1e-9:
            return None, "BUDGET", "OPTION_AVAILABLE_BALANCE_BELOW_PLAN"

        candidate = {**contract, **scored, "signal": signal, "entry_plan": plan}
        return candidate, None, "OPTION_ORDERABLE"

    @staticmethod
    def _dominant_rejection(rejections):
        if not rejections:
            return "OTHER"
        counts = Counter(rejections)
        priority = ["BUDGET", "LIQUIDITY", "SPREAD", "IV", "DELTA", "DTE", "DIRECTION_SIGNAL", "API", "OTHER"]
        best_count = max(counts.values())
        tied = {key for key, value in counts.items() if value == best_count}
        for key in priority:
            if key in tied:
                return key
        return counts.most_common(1)[0][0]

    async def _scan_and_maybe_enter(self):
        cfg = self.config()
        diagnostics = {"signal_rejections": {}, "contract_rejections": {}, "orderable_candidates": 0}
        try:
            snapshot = await self._private_snapshot()
            if snapshot.get("can_trade") is False:
                self._record_scan_outcome("CAN_TRADE", diagnostics=diagnostics)
                return self._record_reason(
                    "옵션 계정 canTrade=false — European Options 거래 권한을 다시 확인하세요.",
                    error=True,
                )
            if snapshot["positions"]:
                self._record_scan_outcome("EXISTING_POSITION", diagnostics=diagnostics)
                return self._record_reason(
                    "기존 옵션 포지션이 있어 신규 진입을 차단했습니다. 봇 소유 포지션만 자동 관리합니다."
                )
            if snapshot["orders"]:
                self._record_scan_outcome("OPEN_ORDER", diagnostics=diagnostics)
                return self._record_reason(
                    "기존 옵션 미체결 주문이 있어 신규 진입을 차단했습니다."
                )
            if base_runtime._f(snapshot["balance"].get("available")) <= 0:
                self._record_scan_outcome("BUDGET", diagnostics=diagnostics)
                return self._record_reason("옵션 계좌 사용 가능 USDT가 없습니다.", error=True)

            exchange_info = await self._get_exchange_info()
            signals = []
            scan_rejections = []
            for underlying in cfg.get("underlyings", []):
                try:
                    result = await self._underlying_signal(underlying, cfg)
                    if result.get("accepted"):
                        signals.append(result)
                    else:
                        scan_rejections.append("DIRECTION_SIGNAL")
                        reason = str(result.get("reason") or "NO_ADAPTIVE_OPTION_SIGNAL")
                        diagnostics["signal_rejections"][reason] = diagnostics["signal_rejections"].get(reason, 0) + 1
                except Exception as exc:
                    logger.warning("Options underlying signal failed for %s: %s", underlying, exc)
                    scan_rejections.append("API")
                    diagnostics["signal_rejections"]["UNDERLYING_DATA_ERROR"] = diagnostics["signal_rejections"].get("UNDERLYING_DATA_ERROR", 0) + 1

            signals.sort(key=lambda row: abs(base_runtime._f(row.get("score"))), reverse=True)
            candidates = []
            consumed = set(str(value) for value in self.state.get("consumed_signal_keys") or [])
            for signal in signals[:3]:
                if signal.get("signal_key") in consumed:
                    continue
                contracts, shortlist_diag = shortlist_option_contracts_with_diagnostics(
                    exchange_info,
                    signal.get("underlying"),
                    signal.get("direction"),
                    signal.get("spot_price"),
                    signal=signal,
                    cfg=cfg,
                )
                if not contracts:
                    if shortlist_diag.get("DTE", 0) > 0:
                        scan_rejections.append("DTE")
                    else:
                        scan_rejections.append("OTHER")
                for key, value in shortlist_diag.items():
                    if value:
                        diagnostics["contract_rejections"][f"SHORTLIST_{key}"] = diagnostics["contract_rejections"].get(f"SHORTLIST_{key}", 0) + int(value)
                for contract in contracts:
                    try:
                        candidate, category, raw_reason = await self._scan_contract_adaptive(
                            contract, signal, cfg, snapshot, exchange_info
                        )
                    except Exception as exc:
                        logger.debug("Option contract scan failed for %s: %s", contract.get("symbol"), exc)
                        scan_rejections.append("API")
                        diagnostics["contract_rejections"]["CONTRACT_DATA_ERROR"] = diagnostics["contract_rejections"].get("CONTRACT_DATA_ERROR", 0) + 1
                        continue
                    if candidate:
                        candidates.append(candidate)
                        diagnostics["orderable_candidates"] += 1
                    else:
                        scan_rejections.append(category or "OTHER")
                        diagnostics["contract_rejections"][str(raw_reason or "UNKNOWN")] = diagnostics["contract_rejections"].get(str(raw_reason or "UNKNOWN"), 0) + 1

            if not candidates:
                outcome = self._dominant_rejection(scan_rejections)
                self._record_scan_outcome(outcome, diagnostics=diagnostics)
                return self._record_reason(
                    "Adaptive Trend/Squeeze 후보 중 20 USDT 예산·DTE·Delta·IV·Spread·유동성 안전조건을 모두 만족한 계약이 없습니다.",
                    candidate=None,
                )

            candidates.sort(key=lambda row: base_runtime._f(row.get("score")), reverse=True)
            selected = candidates[0]
            signal = selected.get("signal") or {}
            plan = selected.get("entry_plan") or {}
            candidate_summary = {
                "symbol": selected.get("symbol"),
                "underlying": selected.get("underlying"),
                "direction": selected.get("side"),
                "strategy": signal.get("strategy"),
                "signal_score": base_runtime._f(signal.get("score")),
                "option_score": base_runtime._f(selected.get("score")),
                "ask": base_runtime._f(selected.get("ask")),
                "spread_pct": base_runtime._f(selected.get("spread_pct")),
                "delta": base_runtime._f(selected.get("delta")),
                "target_delta": base_runtime._f(selected.get("target_delta")),
                "dte_days": base_runtime._f(selected.get("dte_days")),
                "target_dte_days": base_runtime._f(selected.get("target_dte_days")),
                "iv_to_realized": base_runtime._f(selected.get("iv_to_realized")),
                "skew_ratio": base_runtime._f(selected.get("skew_ratio")),
                "planned_cost_usdt": base_runtime._f(plan.get("total_entry_cost_usdt")),
            }
            self.state["last_candidate"] = candidate_summary
            self._save_state()
            self._record_scan_outcome("ORDERABLE_CANDIDATE", diagnostics=diagnostics)
            return await self._enter(selected, snapshot)
        except base_runtime.BinanceOptionsApiError as exc:
            self._record_scan_outcome("API", diagnostics=diagnostics)
            return self._record_reason(f"옵션 API 진입 점검 실패: {exc}", error=True)
        except Exception as exc:
            logger.exception("Adaptive options scan cycle failed")
            self._record_scan_outcome("OTHER", diagnostics=diagnostics)
            return self._record_reason(f"옵션 스캔 오류: {exc}", error=True)

    async def _enter(self, selected, snapshot):
        result = await super()._enter(selected, snapshot)
        if result.get("action") != "entered":
            return result
        position = self.state.get("active_position") or {}
        signal = selected.get("signal") or {}
        position.update(
            {
                "signal_strategy": signal.get("strategy") or "ADAPTIVE_TREND",
                "entry_mark_iv": base_runtime._f(selected.get("mark_iv")),
                "entry_delta": base_runtime._f(selected.get("delta")),
                "entry_gamma": base_runtime._f(selected.get("gamma")),
                "entry_theta": base_runtime._f(selected.get("theta")),
                "entry_vega": base_runtime._f(selected.get("vega")),
                "target_delta": base_runtime._f(selected.get("target_delta")),
                "target_dte_days": base_runtime._f(selected.get("target_dte_days")),
                "entry_iv_to_realized": base_runtime._f(selected.get("iv_to_realized")),
                "entry_skew_ratio": base_runtime._f(selected.get("skew_ratio")),
            }
        )
        self.state["active_position"] = position
        self._save_state()
        result["position"] = position
        return result

    async def _refresh_exit_signal(self, position, cfg, now_ms):
        refresh_ms = int(cfg.get("exit_signal_refresh_seconds", 900)) * 1000
        last_ms = int(position.get("last_exit_signal_refresh_ms") or 0)
        if now_ms - last_ms < refresh_ms:
            return
        underlying = position.get("underlying")
        if not underlying:
            return
        try:
            signal = await self._underlying_signal(underlying, cfg)
            position["last_exit_signal_refresh_ms"] = now_ms
            position["exit_underlying_score"] = base_runtime._f(signal.get("score"))
            position["exit_underlying_direction"] = signal.get("direction")
            position["exit_underlying_accepted"] = bool(signal.get("accepted"))
            position["exit_underlying_strategy"] = signal.get("strategy")
            self.state["active_position"] = position
            self._save_state()
        except Exception as exc:
            logger.debug("Adaptive option exit signal refresh failed for %s: %s", underlying, exc)

    async def _manage_active_position(self, *, force_exit=False):
        if force_exit:
            return await super()._manage_active_position(force_exit=True)
        position = self.state.get("active_position") or {}
        symbol = position.get("symbol")
        if not symbol:
            return await super()._manage_active_position(force_exit=False)
        try:
            exchange_positions = base_runtime._rows(
                await self._call(self._client().positions, symbol)
            )
            exchange_qty = 0.0
            for row in exchange_positions:
                if str(row.get("symbol") or "") == symbol:
                    exchange_qty = max(0.0, base_runtime._position_quantity(row))
                    break
            tracked_qty = max(0.0, base_runtime._f(position.get("quantity")))
            if exchange_qty <= 1e-12 or exchange_qty + 1e-12 < tracked_qty:
                return await super()._manage_active_position(force_exit=False)

            mark_payload, depth = await asyncio.gather(
                self._call(self._client().mark_price, symbol),
                self._call(self._client().depth, symbol, 20),
            )
            mark = base_runtime._first(mark_payload)
            mark_price = base_runtime._f(mark.get("markPrice"), position.get("entry_price"))
            bids = list((depth or {}).get("bids") or [])
            bid = base_runtime._f(bids[0][0]) if bids else mark_price
            entry_price = max(1e-12, base_runtime._f(position.get("entry_price")))
            pnl_pct = mark_price / entry_price - 1.0
            cfg = self.config()
            now_ms = int(time.time() * 1000)
            expiry_ms = int(position.get("expiry_date_ms") or 0)
            expiry_hours = (expiry_ms - now_ms) / 3_600_000.0 if expiry_ms else 9999.0

            peak = max(base_runtime._f(position.get("peak_mark"), entry_price), mark_price)
            legacy_triggered = (
                pnl_pct <= -base_runtime._f(cfg.get("stop_loss_pct"), 0.45)
                or pnl_pct >= base_runtime._f(cfg.get("take_profit_pct"), 0.80)
                or (
                    peak >= entry_price * (1.0 + base_runtime._f(cfg.get("trail_activation_pct"), 0.35))
                    and mark_price <= peak * (1.0 - base_runtime._f(cfg.get("trail_drawdown_pct"), 0.25))
                )
                or now_ms - int(position.get("entry_time_ms") or now_ms)
                >= base_runtime._f(cfg.get("max_hold_hours"), 72.0) * 3_600_000
                or expiry_hours <= base_runtime._f(cfg.get("expiry_exit_hours"), 8.0)
            )
            if legacy_triggered:
                return await super()._manage_active_position(force_exit=False)

            await self._refresh_exit_signal(position, cfg, now_ms)
            position = self.state.get("active_position") or position
            side = str(position.get("side") or "").upper()
            underlying_score = base_runtime._f(position.get("exit_underlying_score"))
            aligned_strength = underlying_score if side == "CALL" else -underlying_score
            signal_threshold = base_runtime._f(cfg.get("min_abs_signal"), 0.46)
            opposed = aligned_strength <= -(signal_threshold * 0.70)
            weak = aligned_strength < signal_threshold * 0.50

            current_iv = base_runtime._f(mark.get("markIV"))
            entry_iv = base_runtime._f(position.get("entry_mark_iv"))
            delta = abs(base_runtime._f(mark.get("delta")))
            theta = base_runtime._f(mark.get("theta"))
            theta_burden = abs(theta) / max(mark_price, 1e-9) if theta else 0.0
            dte_days = expiry_hours / 24.0
            elapsed_hours = (now_ms - int(position.get("entry_time_ms") or now_ms)) / 3_600_000.0

            position["last_mark_iv"] = current_iv
            position["last_delta"] = delta
            position["last_theta"] = theta
            position["last_theta_burden"] = theta_burden
            position["last_dte_days"] = dte_days
            self.state["active_position"] = position
            self._save_state()

            reason = ""
            if opposed and dte_days <= 7.0:
                reason = "OPTION_UNDERLYING_TREND_BREAK"
            elif (
                entry_iv > 0
                and current_iv > 0
                and current_iv <= entry_iv * (1.0 - base_runtime._f(cfg.get("iv_collapse_exit_pct"), 0.30))
                and weak
            ):
                reason = "OPTION_IV_COLLAPSE_WITH_WEAK_TREND"
            elif dte_days <= base_runtime._f(cfg.get("near_expiry_risk_days"), 2.0) and (
                theta_burden >= base_runtime._f(cfg.get("theta_burden_exit_pct"), 0.12)
                or delta < base_runtime._f(cfg.get("delta_collapse_exit"), 0.15)
                or weak
            ):
                reason = "OPTION_NEAR_EXPIRY_DECAY_RISK"
            elif (
                elapsed_hours >= base_runtime._f(cfg.get("adaptive_time_stop_hours"), 36.0)
                and pnl_pct <= 0.10
                and weak
            ):
                reason = "OPTION_ADAPTIVE_TIME_STOP"

            if reason:
                managed_qty = min(exchange_qty, tracked_qty)
                if managed_qty > 1e-12 and bid > 0:
                    return await self._exit_position(position, managed_qty, bid, reason)
            return await super()._manage_active_position(force_exit=False)
        except base_runtime.BinanceOptionsApiError:
            return await super()._manage_active_position(force_exit=False)
        except Exception:
            logger.exception("Adaptive option exit pre-check failed; falling back to legacy manager")
            return await super()._manage_active_position(force_exit=False)


__all__ = ("OptionsTradingService", "SCAN_OUTCOME_LABELS")
