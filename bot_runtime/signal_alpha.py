"""VMT trend, crowding, liquidation reversal, and aggregate alpha suite."""

from __future__ import annotations

import asyncio
import time

from .entry_reason_ko import build_entry_diagnostic
from trading_safety.market_session import tradfi_primary_session_status
from utbreakout.adaptive_breakout_trend import (
    ADAPTIVE_BREAKOUT_TREND_STRATEGY,
    ADAPTIVE_TREND_PORTFOLIO_PROFILE_VERSION,
    AdaptiveBreakoutTrendDecision,
    evaluate_adaptive_breakout_trend,
    evaluate_independent_event_context,
    evaluate_small_account_entry_refinement,
    normalize_adaptive_breakout_trend_config,
    resolve_independent_event_allocation,
)
from utbreakout.change_point_flow import (
    evaluate_change_point_flow_entry,
    resolve_trend_event_candidate,
    select_independent_change_point_flow_candidate,
)
from utbreakout.coinselector import market_tradifi_underlying_type
from utbreakout.profit_capture import (
    QUAD_CONFIRMATION_RISK_MULTIPLIERS,
    bounded_structure_anchor,
)
from utbreakout.relative_strength_pullback import completed_candle_rows
from utbreakout.small_account_regime import (
    SMALL_ACCOUNT_REGIME_PROFILE_VERSION,
    evaluate_small_account_exhaustion_reversal,
    resolve_regime_ensemble_candidate,
    reversal_exit_plan_overrides,
)
from utbreakout.tradfi_pattern_profile import (
    TRADFI_PATTERN_PROFILE_VERSION,
    evaluate_tradfi_pattern_profile,
    normalize_tradfi_pattern_profile_config,
    tradfi_trend_direction,
)
from utbreakout.tradfi_small_account import (
    TRADFI_SMALL_ACCOUNT_PROFILE_VERSION,
    cap_tradfi_risk_tier,
    classify_tradfi_instrument,
    evaluate_tradfi_small_account_guardrails,
)


_ADAPTIVE_RISK_TIER_ORDER = {'base': 0, 'strong': 1, 'elite': 2}


def _resolve_adaptive_trend_risk_tier(
    absolute_tier,
    relative_tier,
    *,
    relative_valid,
    tradfi,
):
    """Let TradFi relative rank upgrade, but never downgrade, trend quality."""

    absolute = str(absolute_tier or 'base').strip().lower()
    if absolute not in _ADAPTIVE_RISK_TIER_ORDER:
        absolute = 'base'
    relative = str(relative_tier or '').strip().lower()
    if not relative_valid or relative not in _ADAPTIVE_RISK_TIER_ORDER:
        return absolute
    if tradfi:
        return max(
            (absolute, relative),
            key=lambda tier: _ADAPTIVE_RISK_TIER_ORDER[tier],
        )
    return relative


class SignalAlphaMixin:
    def _qh_flow_runtime_config(self, cfg=None):
        source = dict(cfg or {})
        base = default_qh_flow_config()
        nested = source.get('qh_flow')
        if isinstance(nested, dict):
            base.update(nested)
        aliases = {
            'qh_flow_live_enabled': 'qh_flow_live_enabled',
            'qh_flow_confirmation_enabled': 'qh_confirmation_enabled',
            'l2_gate_enabled': 'l2_gate_enabled',
            'triple_alpha_three_signal_risk_multiplier': 'triple_three_signal_multiplier',
            'triple_alpha_two_signal_risk_multiplier': 'triple_two_signal_multiplier',
            'triple_alpha_single_signal_risk_multiplier': 'triple_single_signal_multiplier',
        }
        for source_key, target_key in aliases.items():
            if source_key in source:
                base[target_key] = source[source_key]
        return base

    async def _qh_flow_fetch_trade_window(self, symbol, start_ms, end_ms):
        rest_symbol = self.ctrl._build_binance_futures_rest_symbol(symbol)
        if not rest_symbol:
            return []
        rows = await self.ctrl._fetch_binance_public_json(
            '/fapi/v1/aggTrades',
            {
                'symbol': rest_symbol,
                'startTime': int(start_ms),
                'endTime': int(end_ms),
                'limit': 1000,
            },
        )
        return rows if isinstance(rows, list) else []


    async def _qh_flow_confirmation(self, symbol, side, cfg=None, *, force_reprocess=False):
        # QH was retired as an entry strategy.  Keep this compatibility hook
        # neutral so persisted configs cannot silently shrink or block UT/RSPT.
        return {
            'state': 'retired',
            'allowed': True,
            'risk_multiplier': 1.0,
            'reason': 'QH confirmation retired; shared L2 safety remains active',
        }

    async def _calculate_qh_flow_signal(
        self,
        symbol,
        df,
        strategy_params,
        *,
        force_reprocess=False,
    ):
        cfg = self._get_utbot_filtered_breakout_config(strategy_params)
        qh_cfg = self._qh_flow_runtime_config(cfg)
        canonical = self._canonical_futures_symbol(symbol)
        self._clear_utbot_filtered_breakout_entry_plan(canonical)
        status = {
            'strategy': STRATEGY_DISPLAY_NAMES.get(QH_FLOW_STRATEGY, 'QH_FLOW'),
            'entry_strategy': QH_FLOW_STRATEGY,
            'symbol': canonical,
            'stage': 'waiting',
        }

        def _finish(sig, reason, code=None):
            status['reason'] = reason
            status['accepted_side'] = sig
            if code:
                status['reject_code'] = code
            if sig:
                status['accepted_code'] = 'ACCEPTED_ENTRY'
                status['stage'] = 'entry_ready'
            self.qh_flow_last_status[canonical] = dict(status)
            self._store_utbot_filtered_breakout_status(canonical, status)
            self.last_entry_reason[canonical] = reason
            return sig, reason, status

        if self.is_upbit_mode():
            return _finish(None, 'QH-Flow unsupported in Upbit mode', 'REJECTED_UNSUPPORTED_MODE')
        if not bool(qh_cfg.get('qh_flow_enabled', True)) or not bool(qh_cfg.get('qh_flow_live_enabled', False)):
            return _finish(None, 'QH-Flow live disabled', 'REJECTED_QH_LIVE_DISABLED')

        qh_status = await self._fetch_qh_flow_evaluation(
            canonical,
            cfg,
            force_refresh=force_reprocess,
        )
        status.update(qh_status)
        if not qh_status.get('allowed') or qh_status.get('side') not in {'long', 'short'}:
            return _finish(None, f"QH waiting: {qh_status.get('reason')}")
        side = qh_status['side']
        if not self.is_trade_direction_allowed(side):
            return _finish(None, self.format_trade_direction_block_reason(side), 'REJECTED_DIRECTION_FILTER')

        daily_count, daily_pnl = self.db.get_daily_stats()
        daily_entries = self.get_automatic_daily_entry_count()
        status['daily_pnl'] = daily_pnl
        status['daily_entries'] = daily_entries
        if float(cfg.get('daily_max_loss_usdt', 0) or 0) > 0 and float(daily_pnl or 0) <= -float(cfg['daily_max_loss_usdt']):
            return _finish(None, f"risk_limit_blocked: daily pnl {daily_pnl:.2f}", 'REJECTED_DAILY_LOSS_LIMIT')
        daily_trade_limit = int(
            await self.get_effective_automatic_daily_trade_limit_for_entry()
            if hasattr(self, 'get_effective_automatic_daily_trade_limit_for_entry')
            else cfg.get('max_daily_trades', 0) or 0
        )
        if daily_trade_limit > 0 and daily_entries >= daily_trade_limit:
            return _finish(None, f"risk_limit_blocked: daily trade count {daily_entries}", 'REJECTED_DAILY_TRADE_LIMIT')

        try:
            ohlcv = await asyncio.to_thread(
                self.market_data_exchange.fetch_ohlcv,
                canonical,
                '15m',
                limit=220,
            )
        except Exception as exc:
            return _finish(None, f'QH 15m OHLCV unavailable: {exc}', 'REJECTED_QH_DATA')
        rows = self._relative_strength_pullback_rows_from_ohlcv(ohlcv)
        closed_rows = completed_candle_rows(
            rows,
            '15m',
            {'exclude_incomplete_live_candle': True},
            now_ms=int(time.time() * 1000.0),
        )
        closed = pd.DataFrame(closed_rows)
        for column in ['open', 'high', 'low', 'close', 'volume']:
            if column in closed.columns:
                closed[column] = pd.to_numeric(closed[column], errors='coerce')
        closed = closed.dropna(subset=['open', 'high', 'low', 'close']).reset_index(drop=True)
        if len(closed) < 30:
            return _finish(None, 'QH insufficient completed 15m candles', 'REJECTED_QH_DATA')
        metrics = self._calculate_utbreakout_timeframe_metrics(closed, cfg)
        atr_value = _safe_float_or_none(metrics.get('atr'))
        if atr_value is None or atr_value <= 0:
            return _finish(None, 'QH ATR unavailable', 'REJECTED_ATR_TOO_LOW')
        entry_price = _safe_float_or_none(qh_status.get('entry_reference_price'))
        if entry_price is None or entry_price <= 0:
            ticker = await asyncio.to_thread(self.market_data_exchange.fetch_ticker, canonical)
            entry_price = _safe_float_or_none((ticker or {}).get('last') or (ticker or {}).get('close'))
        if entry_price is None or entry_price <= 0:
            return _finish(None, 'QH entry price unavailable', 'REJECTED_QH_DATA')

        filter_values = dict(metrics or {})
        filter_values.update(qh_status.get('futures_context') or {})
        filter_values['entry_price'] = entry_price
        filter_values['entry_timeframe'] = '15m'
        market_quality = self._evaluate_utbreakout_market_quality(side, cfg, filter_values)
        status['market_quality'] = market_quality
        if market_quality.get('hard_block') or market_quality.get('state') is False:
            return _finish(None, f"market_quality_rejected: {market_quality.get('summary')}", 'REJECTED_MARKET_QUALITY')
        l2_gate = dict(qh_status.get('l2_gate') or {})
        if not l2_gate.get('allowed', False):
            return _finish(None, f"L2 stressed: {l2_gate.get('reason')}", 'REJECTED_L2_STRESSED')

        total_balance, free_balance, _ = await self.get_balance_info()
        balance_for_risk = total_balance if total_balance > 0 else free_balance
        common_cfg = self.get_runtime_common_settings()
        leverage = int(max(1.0, float(common_cfg.get('leverage', 5) or 5)))
        risk_multiplier = min(
            1.0,
            max(0.0, float(qh_status.get('risk_multiplier', 0.0) or 0.0)),
            max(0.0, float(market_quality.get('risk_multiplier', 1.0) or 1.0)),
            max(0.0, float(l2_gate.get('risk_multiplier', 0.0) or 0.0)),
        )
        risk_budget = resolve_utbreakout_risk_budget(
            balance_for_risk,
            cfg,
            multiplier=risk_multiplier,
            daily_pnl_usdt=daily_pnl,
        )
        try:
            plan = calculate_risk_plan(
                side=side,
                entry_price=entry_price,
                atr_value=atr_value,
                stop_atr_multiplier=float(qh_cfg.get('stop_atr_multiplier', 1.25) or 1.25),
                ut_stop=None,
                structure_stop=None,
                structure_buffer_atr=0.0,
                take_profit_r_multiple=float(qh_cfg.get('take_profit_r_multiple', 2.50) or 2.50),
                take_profit_front_run_atr=0.0,
                take_profit_front_run_pct=0.0,
                min_risk_reward=min(2.0, float(qh_cfg.get('take_profit_r_multiple', 2.50) or 2.50)),
                balance_usdt=balance_for_risk,
                risk_per_trade_percent=risk_budget['risk_per_trade_percent'],
                max_risk_per_trade_usdt=risk_budget['max_risk_per_trade_usdt'],
                leverage=leverage,
            )
            plan = cap_utbreakout_risk_plan_to_margin(
                plan,
                free_balance=free_balance,
                leverage=leverage,
                entry_price=entry_price,
            )
        except ValueError as exc:
            return _finish(None, f'QH risk plan rejected: {exc}', 'REJECTED_QH_RISK_PLAN')

        plan.update({
            'strategy': QH_FLOW_STRATEGY,
            'plan_symbol': canonical,
            'entry_timeframe': '15m',
            'exit_timeframe': cfg.get('exit_timeframe', '15m'),
            'htf_timeframe': cfg.get('htf_timeframe', '1h'),
            'entry_execution': 'market',
            'decision_candle_ts': int(qh_status.get('boundary_ms') or 0),
            'qh_boundary_ms': int(qh_status.get('boundary_ms') or 0),
            'qh_score': float(qh_status.get('score') or 0.0),
            'qh_risk_multiplier': risk_multiplier,
            'qh_metrics': dict(qh_status.get('metrics') or {}),
            'l2_gate': l2_gate,
            'l2_state': l2_gate.get('state'),
            'l2_risk_multiplier': l2_gate.get('risk_multiplier'),
            'market_quality_summary': market_quality.get('summary'),
            'atr': atr_value,
            'atr_pct': metrics.get('atr_pct'),
            'partial_take_profit_enabled': True,
            'partial_take_profit_r_multiple': 1.25,
            'partial_take_profit_ratio': 0.25,
            'second_take_profit_enabled': True,
            'second_take_profit_r_multiple': float(qh_cfg.get('take_profit_r_multiple', 2.50) or 2.50),
            'second_take_profit_ratio': 0.50,
            'atr_trailing_enabled': True,
            'atr_trailing_activation_r': 1.50,
            'atr_trailing_multiplier': 2.25,
            'ev_time_stop_enabled': True,
            'ev_time_stop_bars': 32,
            'ev_time_stop_min_mfe_r': 0.50,
        })
        self._set_utbot_filtered_breakout_entry_plan(canonical, plan)
        status['entry_plan'] = dict(plan)
        return _finish(side, f"ACCEPTED_ENTRY: {qh_status.get('reason')}")

    async def build_qh_flow_status_text(self, symbol=None):
        cfg = self._get_utbot_filtered_breakout_config()
        qh_cfg = self._qh_flow_runtime_config(cfg)
        target = self._canonical_futures_symbol(symbol or self.current_utbreakout_candidate_symbol or 'BTC/USDT')
        status = await self._fetch_qh_flow_evaluation(target, cfg, force_refresh=True)
        l2 = status.get('l2_gate') if isinstance(status.get('l2_gate'), dict) else {}
        metrics = status.get('metrics') if isinstance(status.get('metrics'), dict) else {}
        return '\n'.join([
            '📊 QH-Flow 전략 상태',
            f"Symbol: {target}",
            f"Live: {bool(qh_cfg.get('qh_flow_live_enabled', False))}",
            f"Phase: {status.get('phase')} / boundary age {float(status.get('boundary_age_seconds', 0.0) or 0.0):.1f}s",
            f"Signal: {str(status.get('side') or 'NONE').upper()} / allowed={bool(status.get('allowed'))}",
            f"Score: {float(status.get('score', 0.0) or 0.0):.1f} / risk x{float(status.get('risk_multiplier', 0.0) or 0.0):.2f}",
            f"Flow: imbalance={float(status.get('current_imbalance', 0.0) or 0.0):+.3f}, notional={float(status.get('current_notional', 0.0) or 0.0):.0f}, z={float(metrics.get('imbalance_z', 0.0) or 0.0):+.2f}",
            f"L2: {str(l2.get('state') or 'unknown').upper()} / {l2.get('reason') or '-'}",
            f"Reason: {status.get('reason') or '-'}",
        ])

    def _strategy_allocator_runtime_config(self, cfg=None):
        source = dict(cfg or {})
        base = default_strategy_allocator_config()
        nested = source.get('strategy_allocator')
        if isinstance(nested, dict):
            base.update(nested)
        if 'strategy_allocator_enabled' in source:
            base['enabled'] = bool(source.get('strategy_allocator_enabled'))
        return base

    def _strategy_allocator_key_for_plan(self, plan):
        plan = dict(plan or {})
        if plan.get('quad_alpha_agreement_state') or plan.get('quad_alpha_selected_strategy'):
            return QUAD_ALPHA_STRATEGY
        if plan.get('triple_alpha_agreement_state') or plan.get('triple_alpha_selected_strategy'):
            return TRIPLE_ALPHA_STRATEGY
        if plan.get('dual_alpha_agreement_state') or plan.get('dual_alpha_selected_strategy'):
            return DUAL_ALPHA_STRATEGY
        return str(plan.get('strategy') or plan.get('entry_strategy') or 'unknown').strip().lower()

    def _load_strategy_allocator_trades(self):
        store = getattr(self, 'trading_state_store', None)
        if store is None:
            store = getattr(getattr(self, 'ctrl', None), 'trading_state_store', None)
        loader = getattr(store, 'load_trade_results', None)
        if not callable(loader):
            return []
        try:
            rows = loader()
        except TypeError:
            rows = loader(limit=500)
        except Exception:
            logger.debug('strategy allocator trade load failed', exc_info=True)
            return []
        return list(rows or [])

    def _apply_strategy_allocator_to_plan(self, plan):
        stored = dict(plan or {})
        if stored.get('strategy_allocator_applied'):
            return stored
        try:
            cfg = self._get_utbot_filtered_breakout_config()
        except Exception:
            cfg = {}
        allocator_cfg = self._strategy_allocator_runtime_config(cfg)
        strategy_key = self._strategy_allocator_key_for_plan(stored)
        metrics = summarize_strategy_trades(
            self._load_strategy_allocator_trades(),
            strategy_key,
            allocator_cfg,
        )
        allocation = evaluate_strategy_allocation(metrics, allocator_cfg)
        scaled = scale_plan_risk(stored, allocation.multiplier)
        scaled.update({
            'strategy_allocator_applied': True,
            'strategy_allocator_key': strategy_key,
            'strategy_allocator_multiplier': float(allocation.multiplier),
            'strategy_allocator_reason': allocation.reason,
            'strategy_allocator_metrics': dict(allocation.metrics),
        })
        if not isinstance(getattr(self, 'strategy_allocator_last_status', None), dict):
            self.strategy_allocator_last_status = {}
        self.strategy_allocator_last_status[strategy_key] = {
            'multiplier': float(allocation.multiplier),
            'reason': allocation.reason,
            'metrics': dict(allocation.metrics),
        }
        return scaled

    async def _evaluate_shared_l2_gate(
        self,
        symbol,
        cfg=None,
        *,
        force_refresh=False,
        side=None,
    ):
        qh_cfg = self._qh_flow_runtime_config(cfg)
        if not bool(qh_cfg.get('l2_gate_enabled', True)) or self.is_upbit_mode():
            return {
                'state': 'disabled',
                'dynamic_state': 'disabled',
                'allowed': True,
                'risk_multiplier': 1.0,
                'reason': 'L2 gate disabled',
            }
        canonical = self._canonical_futures_symbol(symbol)
        cache_key = f"{canonical}:{str(side or 'none').lower()}"
        now = time.time()
        if not isinstance(getattr(self, 'l2_gate_cache', None), dict):
            self.l2_gate_cache = {}
        if not isinstance(getattr(self, 'l2_gate_history', None), dict):
            self.l2_gate_history = {}
        cached = self.l2_gate_cache.get(cache_key)
        if (
            not force_refresh
            and isinstance(cached, dict)
            and now - float(cached.get('cached_at', 0.0) or 0.0) < 5.0
        ):
            return dict(cached.get('data') or {})
        fetcher = getattr(getattr(self, 'ctrl', None), '_fetch_binance_public_json', None)
        if not callable(fetcher):
            result = {
                'state': 'unavailable',
                'dynamic_state': 'unavailable',
                'allowed': True,
                'risk_multiplier': 1.0,
                'reason': 'L2 fetcher unavailable in this runtime',
            }
            self.l2_gate_cache[cache_key] = {'cached_at': now, 'data': dict(result)}
            return result
        try:
            rest_symbol = self.ctrl._build_binance_futures_rest_symbol(canonical)
            depth = await fetcher('/fapi/v1/depth', {'symbol': rest_symbol, 'limit': 20})
            history = list(self.l2_gate_history.get(canonical) or [])[-8:]
            result = evaluate_l2_gate(
                depth,
                qh_cfg,
                history=history,
                side=side,
                symbol=canonical,
            )
            sample = {
                key: result.get(key)
                for key in (
                    'bid_depth_usdt',
                    'ask_depth_usdt',
                    'imbalance_pct',
                    'spread_pct',
                )
            }
            sample['timestamp'] = now
            history.append(sample)
            self.l2_gate_history[canonical] = history[-12:]
        except Exception as exc:
            result = {
                'state': 'stressed_thin',
                'dynamic_state': 'stressed_thin',
                'allowed': False,
                'risk_multiplier': 0.0,
                'reason': f'L2 fetch failed: {type(exc).__name__}: {exc}',
                'error': str(exc),
            }
        self.l2_gate_cache[cache_key] = {'cached_at': now, 'data': dict(result)}
        return result

    async def _fetch_qh_flow_evaluation(self, symbol, cfg=None, *, force_refresh=False, now_ms=None):
        qh_cfg = self._qh_flow_runtime_config(cfg)
        canonical = self._canonical_futures_symbol(symbol)
        now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
        phase = qh_boundary_phase(now_ms, qh_cfg)
        boundary_ms = int(phase['boundary_ms'])
        cache_key = f'{canonical}:{boundary_ms}:v2'
        if not isinstance(getattr(self, 'qh_flow_signal_cache', None), dict):
            self.qh_flow_signal_cache = {}
        cached = self.qh_flow_signal_cache.get(cache_key)
        if not force_refresh and isinstance(cached, dict) and str(cached.get('phase')) in {'ready', 'stale'}:
            return dict(cached)
        status = {
            'strategy': STRATEGY_DISPLAY_NAMES.get(QH_FLOW_STRATEGY, 'QH_FLOW_V2'),
            'entry_strategy': QH_FLOW_STRATEGY,
            'strategy_version': 'v2',
            'symbol': canonical,
            'phase': phase['phase'],
            'boundary_ms': boundary_ms,
            'capture_end_ms': phase['capture_end_ms'],
            'persistence_end_ms': phase.get('persistence_end_ms'),
            'boundary_age_seconds': phase['age_seconds'],
            'seconds_to_next_boundary': phase['seconds_to_next_boundary'],
            'allowed': False,
            'side': None,
            'score': 0.0,
            'risk_multiplier': 0.0,
        }
        if phase['phase'] != 'ready':
            status['reason'] = {
                'collecting': 'QH-v2 collecting first 10 seconds',
                'confirming': 'QH-v2 checking 10-30 second persistence',
                'stale': 'QH signal window expired',
            }.get(phase['phase'], f"QH phase {phase['phase']}")
            self.qh_flow_signal_cache[cache_key] = dict(status)
            return status

        capture_ms = max(1, int(float(qh_cfg.get('capture_seconds', 10) or 10) * 1000))
        persistence_ms = max(1, int(float(qh_cfg.get('persistence_seconds', 20) or 20) * 1000))
        baseline_windows = max(1, int(qh_cfg.get('baseline_windows', 8) or 8))
        previous_boundaries = [boundary_ms - index * 15 * 60 * 1000 for index in range(1, baseline_windows + 1)]
        benchmark_symbols = list(qh_cfg.get('benchmark_symbols') or ['BTC/USDT:USDT', 'ETH/USDT:USDT'])
        tasks = [
            self._qh_flow_fetch_trade_window(canonical, boundary_ms, boundary_ms + capture_ms - 1),
            self._qh_flow_fetch_trade_window(
                canonical,
                boundary_ms + capture_ms,
                boundary_ms + capture_ms + persistence_ms - 1,
            ),
        ]
        tasks.extend(
            self._qh_flow_fetch_trade_window(item, boundary_ms, boundary_ms + capture_ms - 1)
            for item in benchmark_symbols
        )
        tasks.extend(
            self._qh_flow_fetch_trade_window(canonical, item, item + capture_ms - 1)
            for item in previous_boundaries
        )
        results = await asyncio.gather(*tasks, return_exceptions=True)
        current_rows = [] if isinstance(results[0], Exception) else results[0]
        persistence_rows = [] if isinstance(results[1], Exception) else results[1]
        benchmark_results = results[2:2 + len(benchmark_symbols)]
        baseline_results = results[2 + len(benchmark_symbols):]
        current_snapshot = summarize_agg_trades(
            current_rows,
            start_ms=boundary_ms,
            end_ms=boundary_ms + capture_ms - 1,
        )
        persistence_snapshot = summarize_agg_trades(
            persistence_rows,
            start_ms=boundary_ms + capture_ms,
            end_ms=boundary_ms + capture_ms + persistence_ms - 1,
        )
        benchmarks = {}
        for benchmark, result in zip(benchmark_symbols, benchmark_results):
            if isinstance(result, Exception):
                continue
            benchmarks[benchmark] = summarize_agg_trades(
                result,
                start_ms=boundary_ms,
                end_ms=boundary_ms + capture_ms - 1,
            )
        baseline_snapshots = []
        baseline_errors = []
        for previous_boundary, result in zip(previous_boundaries, baseline_results):
            if isinstance(result, Exception):
                baseline_errors.append(f'{previous_boundary}:{type(result).__name__}')
                continue
            snapshot = summarize_agg_trades(
                result,
                start_ms=previous_boundary,
                end_ms=previous_boundary + capture_ms - 1,
            )
            if snapshot.get('total_notional', 0.0) > 0:
                baseline_snapshots.append(snapshot)
        preliminary_side = (
            'long'
            if float(current_snapshot.get('imbalance', 0.0) or 0.0) > 0
            else 'short'
            if float(current_snapshot.get('imbalance', 0.0) or 0.0) < 0
            else None
        )
        l2_gate, derivatives = await asyncio.gather(
            self._evaluate_shared_l2_gate(
                canonical,
                cfg,
                force_refresh=force_refresh,
                side=preliminary_side,
            ),
            self._fetch_utbreakout_futures_context(canonical),
        )
        decision = evaluate_qh_flow(
            current_snapshot,
            baseline_snapshots,
            l2_gate,
            derivatives,
            qh_cfg,
            benchmarks=benchmarks,
            persistence=persistence_snapshot,
        )
        status.update({
            'allowed': bool(decision.allowed),
            'side': decision.side,
            'score': float(decision.score),
            'risk_multiplier': float(decision.risk_multiplier),
            'reason': decision.reason,
            'metrics': dict(decision.metrics),
            'l2_gate': dict(l2_gate or {}),
            'futures_context': dict(derivatives or {}),
            'benchmarks': benchmarks,
            'persistence': persistence_snapshot,
            'current_trade_count': current_snapshot.get('trade_count'),
            'current_notional': current_snapshot.get('total_notional'),
            'current_imbalance': current_snapshot.get('imbalance'),
            'current_return_pct': current_snapshot.get('return_pct'),
            'entry_reference_price': current_snapshot.get('last_price'),
            'baseline_windows_loaded': len(baseline_snapshots),
            'baseline_errors': baseline_errors,
        })
        self.qh_flow_signal_cache[cache_key] = dict(status)
        if not isinstance(getattr(self, 'qh_flow_last_status', None), dict):
            self.qh_flow_last_status = {}
        self.qh_flow_last_status[canonical] = dict(status)
        return status

    def _volatility_managed_trend_runtime_config(self, cfg=None):
        source = dict(cfg or {})
        base = default_volatility_managed_trend_config()
        nested = source.get('volatility_managed_trend')
        if isinstance(nested, dict):
            base.update(nested)
        if 'volatility_managed_trend_live_enabled' in source:
            base['live_enabled'] = bool(source.get('volatility_managed_trend_live_enabled'))
        return base

    async def _calculate_volatility_managed_trend_signal(
        self,
        symbol,
        df,
        strategy_params,
        *,
        force_reprocess=False,
    ):
        cfg = self._get_utbot_filtered_breakout_config(strategy_params)
        vmt_cfg = self._volatility_managed_trend_runtime_config(cfg)
        canonical = self._canonical_futures_symbol(symbol)
        self._clear_utbot_filtered_breakout_entry_plan(canonical)
        status = {
            'strategy': STRATEGY_DISPLAY_NAMES.get(
                VOLATILITY_MANAGED_TREND_STRATEGY,
                'VOLATILITY_MANAGED_TREND',
            ),
            'entry_strategy': VOLATILITY_MANAGED_TREND_STRATEGY,
            'symbol': canonical,
            'stage': 'waiting',
        }

        def _finish(sig, reason, code=None):
            status['reason'] = reason
            status['accepted_side'] = sig
            if code:
                status['reject_code'] = code
            if sig:
                status['accepted_code'] = 'ACCEPTED_ENTRY'
                status['stage'] = 'entry_ready'
            if not isinstance(getattr(self, 'volatility_managed_trend_last_status', None), dict):
                self.volatility_managed_trend_last_status = {}
            self.volatility_managed_trend_last_status[canonical] = dict(status)
            self._store_utbot_filtered_breakout_status(canonical, status)
            self.last_entry_reason[canonical] = reason
            return sig, reason, status

        if self.is_upbit_mode():
            return _finish(None, 'VMT unsupported in Upbit mode', 'REJECTED_UNSUPPORTED_MODE')
        if not bool(vmt_cfg.get('enabled', True)) or not bool(vmt_cfg.get('live_enabled', False)):
            return _finish(None, 'VMT live disabled', 'REJECTED_VMT_LIVE_DISABLED')

        timeframe = str(vmt_cfg.get('timeframe', '1h') or '1h')
        try:
            ohlcv = await asyncio.to_thread(
                self.market_data_exchange.fetch_ohlcv,
                canonical,
                timeframe,
                limit=240,
            )
            rows = self._relative_strength_pullback_rows_from_ohlcv(ohlcv)
            rows = completed_candle_rows(
                rows,
                timeframe,
                {'exclude_incomplete_live_candle': True},
                now_ms=int(time.time() * 1000.0),
            )
        except Exception as exc:
            return _finish(None, f'VMT OHLCV unavailable: {exc}', 'REJECTED_VMT_DATA')

        base_l2 = await self._evaluate_shared_l2_gate(
            canonical,
            cfg,
            force_refresh=force_reprocess,
        )
        preliminary = evaluate_volatility_managed_trend(rows, base_l2, vmt_cfg)
        candidate_side = preliminary.side
        l2_gate = await self._evaluate_shared_l2_gate(
            canonical,
            cfg,
            force_refresh=True,
            side=candidate_side,
        ) if candidate_side in {'long', 'short'} else base_l2
        decision = evaluate_volatility_managed_trend(rows, l2_gate, vmt_cfg)
        metrics = dict(decision.metrics or {})
        status.update({
            'allowed': bool(decision.allowed),
            'side': decision.side,
            'score': float(decision.score),
            'risk_multiplier': float(decision.risk_multiplier),
            'metrics': metrics,
            'l2_gate': dict(l2_gate or {}),
        })
        if not decision.allowed or decision.side not in {'long', 'short'}:
            return _finish(None, f'VMT waiting: {decision.reason}')
        side = decision.side
        if not self.is_trade_direction_allowed(side):
            return _finish(None, self.format_trade_direction_block_reason(side), 'REJECTED_DIRECTION_FILTER')

        daily_count, daily_pnl = self.db.get_daily_stats()
        daily_entries = self.get_automatic_daily_entry_count()
        status['daily_pnl'] = daily_pnl
        status['daily_entries'] = daily_entries
        if float(cfg.get('daily_max_loss_usdt', 0) or 0) > 0 and float(daily_pnl or 0) <= -float(cfg['daily_max_loss_usdt']):
            return _finish(None, f'risk_limit_blocked: daily pnl {daily_pnl:.2f}', 'REJECTED_DAILY_LOSS_LIMIT')
        daily_trade_limit = int(
            await self.get_effective_automatic_daily_trade_limit_for_entry()
            if hasattr(self, 'get_effective_automatic_daily_trade_limit_for_entry')
            else cfg.get('max_daily_trades', 0) or 0
        )
        if daily_trade_limit > 0 and daily_entries >= daily_trade_limit:
            return _finish(None, f'risk_limit_blocked: daily trade count {daily_entries}', 'REJECTED_DAILY_TRADE_LIMIT')

        reference_price = _safe_float_or_none(metrics.get('reference_price'))
        atr_value = _safe_float_or_none(metrics.get('atr'))
        structure_stop = _safe_float_or_none(metrics.get('structure_stop'))
        if reference_price is None or reference_price <= 0 or atr_value is None or atr_value <= 0:
            return _finish(None, 'VMT reference price/ATR unavailable', 'REJECTED_VMT_DATA')
        try:
            ticker = await asyncio.to_thread(self.market_data_exchange.fetch_ticker, canonical)
            entry_price = _safe_float_or_none((ticker or {}).get('last') or (ticker or {}).get('close'))
        except Exception as exc:
            return _finish(None, f'VMT live price unavailable: {exc}', 'REJECTED_VMT_DATA')
        if entry_price is None or entry_price <= 0:
            return _finish(None, 'VMT live price unavailable', 'REJECTED_VMT_DATA')

        chase_atr = (
            (entry_price - reference_price) / atr_value
            if side == 'long'
            else (reference_price - entry_price) / atr_value
        )
        status['entry_chase_atr'] = chase_atr
        if chase_atr > float(vmt_cfg.get('entry_chase_max_atr', 0.50) or 0.50):
            return _finish(
                None,
                f'VMT entry moved {chase_atr:.2f} ATR beyond completed-candle signal',
                'REJECTED_VMT_STALE_CHASE',
            )
        if structure_stop is not None and (
            (side == 'long' and entry_price <= structure_stop)
            or (side == 'short' and entry_price >= structure_stop)
        ):
            return _finish(None, 'VMT structure invalidated before order', 'REJECTED_VMT_INVALIDATED')

        filter_values = {
            'entry_price': entry_price,
            'entry_timeframe': timeframe,
            'atr': atr_value,
            'atr_pct': atr_value / entry_price * 100.0,
        }
        market_quality = self._evaluate_utbreakout_market_quality(side, cfg, filter_values)
        status['market_quality'] = market_quality
        if market_quality.get('hard_block') or market_quality.get('state') is False:
            return _finish(None, f"market_quality_rejected: {market_quality.get('summary')}", 'REJECTED_MARKET_QUALITY')
        if not l2_gate.get('allowed', False):
            return _finish(None, f"L2 stressed: {l2_gate.get('reason')}", 'REJECTED_L2_STRESSED')

        total_balance, free_balance, _ = await self.get_balance_info()
        balance_for_risk = total_balance if total_balance > 0 else free_balance
        common_cfg = self.get_runtime_common_settings()
        leverage = int(max(1.0, float(common_cfg.get('leverage', 5) or 5)))
        risk_multiplier = min(
            1.0,
            max(0.0, float(decision.risk_multiplier or 0.0)),
            max(0.0, float(market_quality.get('risk_multiplier', 1.0) or 1.0)),
            max(0.0, float(l2_gate.get('risk_multiplier', 0.0) or 0.0)),
        )
        risk_budget = resolve_utbreakout_risk_budget(
            balance_for_risk,
            cfg,
            multiplier=risk_multiplier,
            daily_pnl_usdt=daily_pnl,
        )
        structure_for_risk, structure_distance_atr = bounded_structure_anchor(
            entry_price=entry_price,
            atr_value=atr_value,
            structure_stop=structure_stop,
            max_distance_atr=vmt_cfg.get('stop_atr_multiplier', 1.60),
        )
        status['structure_distance_atr'] = structure_distance_atr
        try:
            plan = calculate_risk_plan(
                side=side,
                entry_price=entry_price,
                atr_value=atr_value,
                stop_atr_multiplier=float(vmt_cfg.get('stop_atr_multiplier', 1.60) or 1.60),
                ut_stop=None,
                structure_stop=structure_for_risk,
                structure_buffer_atr=float(vmt_cfg.get('structure_buffer_atr', 0.10) or 0.10),
                take_profit_r_multiple=float(vmt_cfg.get('take_profit_r_multiple', 4.00) or 4.00),
                take_profit_front_run_atr=0.0,
                take_profit_front_run_pct=0.0,
                min_risk_reward=2.0,
                balance_usdt=balance_for_risk,
                risk_per_trade_percent=risk_budget['risk_per_trade_percent'],
                max_risk_per_trade_usdt=risk_budget['max_risk_per_trade_usdt'],
                leverage=leverage,
            )
            plan = cap_utbreakout_risk_plan_to_margin(
                plan,
                free_balance=free_balance,
                leverage=leverage,
                entry_price=entry_price,
            )
        except ValueError as exc:
            return _finish(None, f'VMT risk plan rejected: {exc}', 'REJECTED_VMT_RISK_PLAN')

        plan.update({
            'strategy': VOLATILITY_MANAGED_TREND_STRATEGY,
            'plan_symbol': canonical,
            'signal_candle_ts': metrics.get('signal_candle_ts'),
            'entry_timeframe': timeframe,
            'timeframe': timeframe,
            'exit_timeframe': '15m',
            'htf_timeframe': '4h',
            'entry_execution': 'market',
            'vmt_score': float(decision.score),
            'vmt_risk_multiplier': risk_multiplier,
            'vmt_metrics': metrics,
            'structure_reference_stop': structure_stop,
            'entry_chase_atr': chase_atr,
            'l2_gate': dict(l2_gate or {}),
            'l2_state': l2_gate.get('state'),
            'l2_risk_multiplier': l2_gate.get('risk_multiplier'),
            'market_quality_summary': market_quality.get('summary'),
            'atr': atr_value,
            'atr_pct': atr_value / entry_price * 100.0,
            'partial_take_profit_enabled': True,
            'partial_take_profit_r_multiple': 1.50,
            'partial_take_profit_ratio': 0.20,
            'second_take_profit_enabled': True,
            'second_take_profit_r_multiple': float(vmt_cfg.get('take_profit_r_multiple', 4.00) or 4.00),
            'second_take_profit_ratio': 0.25,
            'runner_pct': 0.55,
            'preserve_runner_qty': True,
            'atr_trailing_enabled': True,
            'atr_trailing_activation_r': 2.00,
            'atr_trailing_multiplier': 3.25,
            'runner_exit_enabled': True,
            'runner_chandelier_enabled': True,
            'runner_chandelier_lookback': 32,
            'tp1_breakeven_enabled': True,
            'tp1_breakeven_wait_for_partial': True,
            'ev_time_stop_enabled': True,
            # Exit monitoring runs on 15m bars; preserve the configured VMT
            # holding period expressed in completed 1h signal bars.
            'ev_time_stop_bars': int(vmt_cfg.get('time_stop_bars', 48) or 48) * 4,
            'ev_time_stop_min_mfe_r': 0.35,
        })
        self._set_utbot_filtered_breakout_entry_plan(canonical, plan)
        status['entry_plan'] = dict(plan)
        return _finish(side, f'ACCEPTED_ENTRY: {decision.reason}')

    async def build_volatility_managed_trend_status_text(self, symbol=None):
        target = self._canonical_futures_symbol(
            symbol or self.current_utbreakout_candidate_symbol or 'BTC/USDT'
        )
        status = dict((getattr(self, 'volatility_managed_trend_last_status', {}) or {}).get(target) or {})
        if not status:
            return '\n'.join([
                '📈 VMT 변동성 관리 추세 상태',
                f'Symbol: {target}',
                '아직 완료된 1시간봉 평가 기록이 없습니다.',
            ])
        metrics = status.get('metrics') if isinstance(status.get('metrics'), dict) else {}
        votes = metrics.get('horizon_votes') if isinstance(metrics.get('horizon_votes'), dict) else {}
        vote_text = ', '.join(f'{key}h={str(value or "NONE").upper()}' for key, value in votes.items())
        return '\n'.join([
            '📈 VMT 변동성 관리 추세 상태',
            f'Symbol: {target}',
            f"Signal: {str(status.get('side') or 'NONE').upper()} / allowed={bool(status.get('allowed'))}",
            f"Score: {float(status.get('score', 0.0) or 0.0):.1f} / risk x{float(status.get('risk_multiplier', 0.0) or 0.0):.2f}",
            f'Horizons: {vote_text or "N/A"}',
            f"Efficiency: {float(metrics.get('efficiency_ratio', 0.0) or 0.0):.2f} / vol ratio {float(metrics.get('volatility_ratio', 0.0) or 0.0):.2f}",
            f"Extension: {float(metrics.get('extension_atr', 0.0) or 0.0):.2f} ATR",
            f"Reason: {status.get('reason') or '-'}",
        ])

    def _adaptive_breakout_trend_runtime_config(self, cfg=None):
        source = dict(cfg or {})
        base = normalize_adaptive_breakout_trend_config()
        nested = source.get('adaptive_breakout_trend')
        if isinstance(nested, dict):
            base.update(nested)
        if 'adaptive_breakout_trend_live_enabled' in source:
            base['live_enabled'] = bool(source.get('adaptive_breakout_trend_live_enabled'))
        return normalize_adaptive_breakout_trend_config(base)

    async def _adaptive_trend_balance_snapshot(self, *, ttl_seconds=15.0):
        """Share one balance read across rapid scanner candidate evaluations."""

        now_ts = time.time()
        cached = getattr(self, '_adaptive_trend_balance_cache', None)
        if (
            isinstance(cached, dict)
            and now_ts - float(cached.get('cached_at', 0.0) or 0.0)
            < max(1.0, float(ttl_seconds))
        ):
            values = cached.get('values')
            if isinstance(values, (list, tuple)) and len(values) == 3:
                return tuple(float(value or 0.0) for value in values)
        values = await self.get_balance_info()
        normalized = tuple(float(value or 0.0) for value in values)
        self._adaptive_trend_balance_cache = {
            'cached_at': now_ts,
            'values': normalized,
        }
        return normalized

    @staticmethod
    def _adaptive_trend_event_decision(event_candidate):
        """Convert an independent event candidate into the shared plan contract."""

        candidate = dict(event_candidate or {})
        event = dict(candidate.get('decision') or {})
        side = str(candidate.get('side') or '').strip().lower()
        reference_price = _safe_float_or_none(event.get('event_reference_price'))
        atr_value = _safe_float_or_none(event.get('event_atr'))
        if (
            not candidate.get('allowed')
            or side not in {'long', 'short'}
            or reference_price is None
            or reference_price <= 0
            or atr_value is None
            or atr_value <= 0
        ):
            return AdaptiveBreakoutTrendDecision(
                reason='independent_change_point_flow_data_unavailable'
            )
        direction = 1.0 if side == 'long' else -1.0
        drift_strength = min(
            1.5,
            abs(float(event.get('directional_drift_z', 0.0) or 0.0)) / 2.5,
        )
        state = str(event.get('state') or 'event_only')
        metrics = {
            'reference_price': reference_price,
            'atr': atr_value,
            'structure_stop': event.get('event_structure_stop'),
            'signal_candle_ts': event.get('event_signal_candle_ts'),
            'risk_tier': event.get('risk_tier') or 'base',
            'weighted_momentum': direction * drift_strength,
            'fast_momentum_retention': 1.0,
            'trend_clarity': min(
                1.0,
                float(event.get('regime_change_score', 0.0) or 0.0) / 100.0,
            ),
            'trend_efficiency': float(
                event.get('directional_persistence', 0.5) or 0.5
            ),
            'volatility_scale': 1.0,
            'entry_opportunity_score': float(
                event.get('total_score', 0.0) or 0.0
            ),
            'change_point_flow_entry': True,
            'change_point_flow_state': state,
            'ema_crossover': False,
            'compression_breakout': state == 'new_regime',
            'pullback_resumption': False,
            'impulse_breakout': state in {'new_regime', 'persistent_flow'},
            'weighted_continuation': False,
            'signed_fast_ema_distance_atr': 0.0,
        }
        return AdaptiveBreakoutTrendDecision(
            allowed=True,
            side=side,
            score=float(event.get('total_score', 0.0) or 0.0),
            risk_multiplier=1.0,
            reason=f"Independent Change-Point Flow {side} {state}",
            metrics=metrics,
        )

    @staticmethod
    def _adaptive_trend_reversal_decision(reversal_candidate):
        """Convert the regime challenger into the shared risk-plan contract."""

        candidate = dict(reversal_candidate or {})
        side = str(candidate.get('side') or '').strip().lower()
        reference_price = _safe_float_or_none(candidate.get('reference_price'))
        atr_value = _safe_float_or_none(candidate.get('atr'))
        if (
            not candidate.get('allowed')
            or side not in {'long', 'short'}
            or reference_price is None
            or reference_price <= 0
            or atr_value is None
            or atr_value <= 0
        ):
            return AdaptiveBreakoutTrendDecision(
                reason='small_account_exhaustion_reversal_data_unavailable'
            )
        direction = 1.0 if side == 'long' else -1.0
        metrics = {
            'reference_price': reference_price,
            'atr': atr_value,
            'structure_stop': candidate.get('structure_stop'),
            'signal_candle_ts': candidate.get('signal_candle_ts'),
            'risk_tier': 'base',
            'weighted_momentum': float(
                candidate.get('weighted_momentum', 0.0) or 0.0
            ),
            'fast_momentum_retention': 1.0,
            'trend_clarity': float(
                candidate.get('trend_clarity', 0.0) or 0.0
            ),
            'trend_efficiency': 0.5,
            'volatility_scale': 1.0,
            'entry_opportunity_score': float(
                candidate.get('score', 0.0) or 0.0
            ),
            'exhaustion_reversal': True,
            'exhaustion_reversal_profile': candidate.get('profile'),
            'reversal_mean_target_price': candidate.get(
                'reversal_mean_target_price'
            ),
            'ema_crossover': False,
            'compression_breakout': False,
            'pullback_resumption': False,
            'impulse_breakout': False,
            'weighted_continuation': False,
            'signed_fast_ema_distance_atr': direction * 0.0,
        }
        return AdaptiveBreakoutTrendDecision(
            allowed=True,
            side=side,
            score=float(candidate.get('score', 0.0) or 0.0),
            risk_multiplier=1.0,
            reason=str(candidate.get('reason') or 'confirmed exhaustion reversal'),
            metrics=metrics,
        )

    @staticmethod
    def _tradfi_pattern_profile_runtime_config(trend_cfg=None):
        source = trend_cfg if isinstance(trend_cfg, dict) else {}
        nested = source.get('tradfi_pattern_profile')
        return normalize_tradfi_pattern_profile_config(
            nested if isinstance(nested, dict) else None
        )

    async def _fetch_tradfi_profile_completed_rows(
        self,
        symbol,
        timeframe,
        limit,
        *,
        now_ms,
    ):
        ohlcv = await asyncio.to_thread(
            self.market_data_exchange.fetch_ohlcv,
            symbol,
            timeframe,
            limit=int(limit),
        )
        rows = self._relative_strength_pullback_rows_from_ohlcv(ohlcv)
        return completed_candle_rows(
            rows,
            timeframe,
            {'exclude_incomplete_live_candle': True},
            now_ms=int(now_ms),
        )

    async def _fetch_tradfi_pattern_context(self, symbol, trend_cfg, *, now_ms=None):
        """Fetch slow TradFi context once per cache window.

        Benchmark data is corroborating evidence only. A newly listed contract
        must not be disabled solely because SPY or QQQ history is unavailable.
        """

        canonical = self._canonical_futures_symbol(symbol)
        current_ms = int(now_ms or time.time() * 1000.0)
        cache = getattr(self, 'tradfi_pattern_context_cache', None)
        if not isinstance(cache, dict):
            cache = {}
            self.tradfi_pattern_context_cache = cache
        cached = cache.get(canonical)
        cache_ttl_ms = 300_000
        if (
            isinstance(cached, dict)
            and current_ms - int(cached.get('cached_at_ms', 0) or 0) < cache_ttl_ms
        ):
            return dict(cached.get('context') or {})

        profile_cfg = self._tradfi_pattern_profile_runtime_config(trend_cfg)
        underlying_type = "EQUITY"
        for exchange in (
            getattr(self, 'market_data_exchange', None),
            getattr(self, 'exchange', None),
        ):
            markets = getattr(exchange, 'markets', None) if exchange is not None else None
            if not isinstance(markets, dict) or not markets:
                continue
            market = self._coin_selector_market_for_symbol(canonical, markets)
            if isinstance(market, dict):
                underlying_type = market_tradifi_underlying_type(market) or "EQUITY"
                break
        instrument_profile = classify_tradfi_instrument(
            canonical,
            underlying_type,
        )
        higher_tf = str(profile_cfg.get('higher_timeframe', '4h') or '4h')
        daily_tf = str(profile_cfg.get('daily_timeframe', '1d') or '1d')
        requests = {
            'higher_timeframe_rows': (canonical, higher_tf, 240),
            'daily_rows': (canonical, daily_tf, 180),
            'SPY': ('SPY/USDT:USDT', higher_tf, 240),
            'QQQ': ('QQQ/USDT:USDT', higher_tf, 240),
        }

        async def _fetch(payload):
            return await self._fetch_tradfi_profile_completed_rows(
                *payload,
                now_ms=current_ms,
            )

        results = await asyncio.gather(
            *(_fetch(payload) for payload in requests.values()),
            return_exceptions=True,
        )
        resolved = dict(zip(requests, results))
        errors = {
            key: str(value)
            for key, value in resolved.items()
            if isinstance(value, Exception)
        }
        benchmark_directions = {
            key: tradfi_trend_direction(value)
            for key, value in resolved.items()
            if key in {'SPY', 'QQQ'} and not isinstance(value, Exception)
        }
        context = {
            'higher_timeframe_rows': (
                []
                if isinstance(resolved['higher_timeframe_rows'], Exception)
                else resolved['higher_timeframe_rows']
            ),
            'daily_rows': (
                []
                if isinstance(resolved['daily_rows'], Exception)
                else resolved['daily_rows']
            ),
            'benchmark_directions': benchmark_directions,
            'session_status': tradfi_primary_session_status(underlying_type),
            'underlying_type': underlying_type,
            'instrument_profile': instrument_profile,
            'errors': errors,
        }
        cache[canonical] = {
            'cached_at_ms': current_ms,
            'context': context,
        }
        if len(cache) > 96:
            oldest_key = min(
                cache,
                key=lambda key: int((cache.get(key) or {}).get('cached_at_ms', 0) or 0),
            )
            if oldest_key != canonical:
                cache.pop(oldest_key, None)
        return dict(context)

    async def _calculate_adaptive_breakout_trend_signal(
        self,
        symbol,
        df,
        strategy_params,
        *,
        force_reprocess=False,
    ):
        cfg = self._get_utbot_filtered_breakout_config(strategy_params)
        trend_cfg = self._adaptive_breakout_trend_runtime_config(cfg)
        canonical = self._canonical_futures_symbol(symbol)
        self._clear_utbot_filtered_breakout_entry_plan(canonical)
        status = {
            'strategy': STRATEGY_DISPLAY_NAMES.get(
                ADAPTIVE_BREAKOUT_TREND_STRATEGY,
                'ADAPTIVE_BREAKOUT_TREND',
            ),
            'entry_strategy': ADAPTIVE_BREAKOUT_TREND_STRATEGY,
            'symbol': canonical,
            'stage': 'waiting',
        }

        def _finish(sig, reason, code=None):
            status['reason'] = reason
            status['accepted_side'] = sig
            if code:
                status['reject_code'] = code
            if sig:
                status['accepted_code'] = 'ACCEPTED_ENTRY'
                status['stage'] = 'entry_ready'
            if not isinstance(getattr(self, 'adaptive_breakout_trend_last_status', None), dict):
                self.adaptive_breakout_trend_last_status = {}
            self.adaptive_breakout_trend_last_status[canonical] = dict(status)
            self._store_utbot_filtered_breakout_status(canonical, status)
            self.last_entry_reason[canonical] = reason
            return sig, reason, status

        if self.is_upbit_mode():
            return _finish(
                None,
                'Adaptive Breakout Trend is unavailable in Upbit mode',
                'REJECTED_UNSUPPORTED_MODE',
            )
        if not bool(trend_cfg.get('enabled', True)) or not bool(trend_cfg.get('live_enabled', False)):
            return _finish(
                None,
                'Adaptive Breakout Trend live mode is OFF',
                'REJECTED_ADAPTIVE_TREND_LIVE_DISABLED',
            )

        timeframe = str(trend_cfg.get('timeframe', '1h') or '1h')
        status['entry_timeframe'] = timeframe
        evaluation_now_ms = int(time.time() * 1000.0)
        config_signature = tuple(
            sorted((str(key), repr(value)) for key, value in trend_cfg.items())
        )
        try:
            ohlcv = await asyncio.to_thread(
                self.market_data_exchange.fetch_ohlcv,
                canonical,
                timeframe,
                limit=max(220, int(trend_cfg.get('fetch_limit', 360) or 360)),
            )
            rows = self._relative_strength_pullback_rows_from_ohlcv(ohlcv)
            rows = completed_candle_rows(
                rows,
                timeframe,
                {'exclude_incomplete_live_candle': True},
                now_ms=evaluation_now_ms,
            )
        except Exception as exc:
            return _finish(
                None,
                f'Adaptive Breakout Trend OHLCV unavailable: {exc}',
                'REJECTED_ADAPTIVE_TREND_DATA',
            )

        # Always refresh the public OHLCV snapshot.  The exchange can publish
        # or correct the just-closed candle shortly after a time boundary, so a
        # wall-clock bucket is not a valid immutable-candle cache key.
        completed_candle_ts = (
            _safe_float_or_none(rows[-1].get('timestamp'))
            if rows
            else None
        )
        candle_fingerprint = (
            len(rows),
            tuple(
                tuple(
                    repr(row.get(key))
                    for key in ('timestamp', 'open', 'high', 'low', 'close', 'volume')
                )
                for row in rows[-2:]
            ),
        )
        status['completed_candle_ts'] = completed_candle_ts
        if not isinstance(
            getattr(self, 'adaptive_breakout_trend_candle_cache', None),
            dict,
        ):
            self.adaptive_breakout_trend_candle_cache = {}
        if (
            canonical not in self.adaptive_breakout_trend_candle_cache
            and len(self.adaptive_breakout_trend_candle_cache) >= 96
        ):
            oldest_symbol = next(iter(self.adaptive_breakout_trend_candle_cache), None)
            if oldest_symbol is not None:
                self.adaptive_breakout_trend_candle_cache.pop(oldest_symbol, None)
        cached_candle = self.adaptive_breakout_trend_candle_cache.get(canonical)
        cache_hit = bool(
            isinstance(cached_candle, dict)
            and cached_candle.get('timeframe') == timeframe
            and cached_candle.get('completed_candle_ts') == completed_candle_ts
            and cached_candle.get('candle_fingerprint') == candle_fingerprint
            and cached_candle.get('config_signature') == config_signature
            and cached_candle.get('preliminary') is not None
        )
        if cache_hit:
            preliminary = cached_candle['preliminary']
        else:
            preliminary = evaluate_adaptive_breakout_trend(rows, None, trend_cfg)
            self.adaptive_breakout_trend_candle_cache[canonical] = {
                'timeframe': timeframe,
                'completed_candle_ts': completed_candle_ts,
                'candle_fingerprint': candle_fingerprint,
                'config_signature': config_signature,
                'preliminary': preliminary,
            }

        status['candle_cache_hit'] = cache_hit
        profile_cfg = self._tradfi_pattern_profile_runtime_config(trend_cfg)
        tradfi_profile_applied = False
        tradfi_context = {}
        classifier = getattr(self, '_is_tradifi_perpetual_symbol', None)
        try:
            is_tradfi = bool(await classifier(canonical)) if callable(classifier) else False
        except Exception as exc:
            status['tradfi_classification_error'] = str(exc)
            is_tradfi = False
        status['tradfi_perpetual'] = is_tradfi
        if is_tradfi and bool(profile_cfg.get('enabled', True)):
            rollout_checker = getattr(self, 'ensure_tradfi_profile_rollout_active', None)
            if not callable(rollout_checker):
                return _finish(
                    None,
                    'TradFi profile rollout safety boundary unavailable',
                    'REJECTED_TRADFI_PROFILE_ROLLOUT_UNAVAILABLE',
                )
            rollout_state = await rollout_checker()
            status['tradfi_profile_rollout'] = dict(rollout_state or {})
            if not bool((rollout_state or {}).get('active')):
                rollout_reason = (rollout_state or {}).get('reason') or 'transition pending'
                return _finish(
                    None,
                    f'TradFi profile transition pending: {rollout_reason}',
                    'REJECTED_TRADFI_PROFILE_TRANSITION_PENDING',
                )
            tradfi_context = await self._fetch_tradfi_pattern_context(
                canonical,
                trend_cfg,
                now_ms=evaluation_now_ms,
            )
            preliminary = evaluate_tradfi_pattern_profile(
                rows,
                preliminary,
                symbol=canonical,
                underlying_type=tradfi_context.get('underlying_type'),
                higher_timeframe_rows=tradfi_context.get('higher_timeframe_rows'),
                daily_rows=tradfi_context.get('daily_rows'),
                benchmark_directions=tradfi_context.get('benchmark_directions'),
                session_status=tradfi_context.get('session_status'),
                config=profile_cfg,
            )
            tradfi_profile_applied = True
            status.update({
                'tradfi_profile_version': TRADFI_PATTERN_PROFILE_VERSION,
                'tradfi_pattern_profile_applied': True,
                'tradfi_pattern_context_errors': dict(tradfi_context.get('errors') or {}),
            })
        total_balance, free_balance, _ = await self._adaptive_trend_balance_snapshot()
        balance_for_risk = total_balance if total_balance > 0 else free_balance
        small_account_threshold = max(
            0.0,
            float(
                trend_cfg.get('small_account_equity_threshold_usdt', 1_000.0)
                or 1_000.0
            ),
        )
        small_account_aggressive_candidate = bool(
            trend_cfg.get('small_account_aggressive_enabled', True)
            and balance_for_risk > 0
            and balance_for_risk < small_account_threshold
            and free_balance > 0
        )

        event_timeframe = str(cfg.get('entry_timeframe', '15m') or '15m')
        event_rows = []
        if df is not None and callable(getattr(df, 'to_dict', None)):
            try:
                event_rows = completed_candle_rows(
                    df.to_dict(orient='records'),
                    event_timeframe,
                    {'exclude_incomplete_live_candle': True},
                    now_ms=evaluation_now_ms,
                )
            except (TypeError, ValueError):
                event_rows = []
        if not event_rows:
            event_rows = rows
            event_timeframe = timeframe

        futures_context = {}
        event_candidate = {
            'allowed': False,
            'side': None,
            'score': 0.0,
            'reason': 'independent event path not applicable',
            'source': 'change_point_flow',
            'evaluations': {},
        }
        market_regime_context = {}
        multi_timeframe_context = {}
        regime_promotion_status = {}
        reversal_candidate = {
            'allowed': False,
            'side': None,
            'score': 0.0,
            'reason': 'exhaustion reversal path not applicable',
            'source': 'exhaustion_reversal',
        }
        if small_account_aggressive_candidate:
            context_fetcher = getattr(
                self,
                '_fetch_utbreakout_futures_context',
                None,
            )
            if callable(context_fetcher):
                try:
                    fresh_context = await context_fetcher(canonical)
                    if isinstance(fresh_context, dict):
                        futures_context.update(fresh_context)
                except Exception as exc:
                    status['change_point_flow_context_error'] = str(exc)
            mtf_seed = {
                event_timeframe: event_rows,
                timeframe: rows,
            }
            if is_tradfi and tradfi_context:
                mtf_seed['4h'] = tradfi_context.get('higher_timeframe_rows') or []
                mtf_seed['1d'] = tradfi_context.get('daily_rows') or []
            mtf_fetcher = getattr(
                self,
                '_fetch_small_account_multitimeframe_context',
                None,
            )
            if callable(mtf_fetcher):
                try:
                    multi_timeframe_context = await mtf_fetcher(
                        canonical,
                        seed_rows=mtf_seed,
                        config=trend_cfg.get('small_account_regime_ensemble'),
                        now_ms=evaluation_now_ms,
                    )
                except Exception as exc:
                    status['small_account_multitimeframe_error'] = str(exc)
            event_candidate = select_independent_change_point_flow_candidate(
                event_rows,
                futures_context=futures_context,
                config=trend_cfg.get('change_point_flow'),
                tradfi=is_tradfi,
            )
            if not is_tradfi:
                regime_fetcher = getattr(
                    self,
                    '_fetch_utbreakout_market_regime_context',
                    None,
                )
                if callable(regime_fetcher):
                    try:
                        fresh_regime = await regime_fetcher(cfg, canonical)
                        if isinstance(fresh_regime, dict):
                            market_regime_context.update(fresh_regime)
                    except Exception as exc:
                        status['small_account_regime_context_error'] = str(exc)
                reversal_candidate = evaluate_small_account_exhaustion_reversal(
                    event_rows,
                    trend_metrics=getattr(preliminary, 'metrics', None),
                    futures_context=futures_context,
                    market_regime_context=market_regime_context,
                    multi_timeframe_context=multi_timeframe_context,
                    config=trend_cfg.get('small_account_regime_ensemble'),
                    tradfi=False,
                )
                promotion_getter = getattr(
                    self,
                    '_get_small_account_regime_promotion_status',
                    None,
                )
                if callable(promotion_getter):
                    regime_promotion_status = promotion_getter(
                        trend_cfg.get('small_account_regime_ensemble')
                    )
                register_challenger = getattr(
                    self,
                    '_register_small_account_regime_challenger',
                    None,
                )
                if callable(register_challenger):
                    register_challenger(
                        canonical,
                        reversal_candidate,
                        trend_cfg.get('small_account_regime_ensemble'),
                        multi_timeframe_context,
                        futures_context,
                    )

        trend_candidate = {
            'allowed': bool(
                preliminary.allowed
                and preliminary.side in {'long', 'short'}
            ),
            'side': preliminary.side,
            'score': float(preliminary.score or 0.0),
            'reason': preliminary.reason,
            'fresh_continuation': bool(
                (getattr(preliminary, 'metrics', None) or {}).get(
                    'continuation_reacceleration'
                )
                or (getattr(preliminary, 'metrics', None) or {}).get(
                    'compression_breakout'
                )
                or (getattr(preliminary, 'metrics', None) or {}).get(
                    'pullback_resumption'
                )
                or (getattr(preliminary, 'metrics', None) or {}).get(
                    'impulse_breakout'
                )
            ),
        }
        conflict_margin = float(
            (trend_cfg.get('change_point_flow') or {}).get(
                'candidate_conflict_margin',
                12.0,
            )
            or 12.0
        )
        candidate_resolution = resolve_trend_event_candidate(
            trend_candidate,
            event_candidate,
            conflict_margin=conflict_margin,
            allow_event_conflict_override=bool(
                (trend_cfg.get('change_point_flow') or {}).get(
                    'allow_event_conflict_override',
                    False,
                )
            ),
        )
        candidate_resolution = resolve_regime_ensemble_candidate(
            candidate_resolution,
            reversal_candidate,
            multi_timeframe_context=multi_timeframe_context,
            cost_context=futures_context,
            promotion_status=regime_promotion_status,
            config=trend_cfg.get('small_account_regime_ensemble'),
        )
        status.update({
            'small_account_aggressive_candidate': small_account_aggressive_candidate,
            'small_account_equity_usdt': balance_for_risk,
            'trend_event_resolution': dict(candidate_resolution),
            'small_account_regime_engine': candidate_resolution.get(
                'regime_engine'
            ),
            'small_account_multitimeframe': dict(multi_timeframe_context),
            'small_account_regime_promotion': dict(regime_promotion_status),
            'exhaustion_reversal_candidate': {
                key: reversal_candidate.get(key)
                for key in ('allowed', 'side', 'score', 'reason', 'code', 'profile')
            },
            'independent_event_candidate': {
                key: event_candidate.get(key)
                for key in ('allowed', 'side', 'score', 'reason', 'code')
            },
        })
        if not candidate_resolution.get('allowed'):
            return _finish(
                None,
                'Adaptive regime ensemble waiting: '
                f"{candidate_resolution.get('reason')}; "
                f"trend={preliminary.reason}; "
                f"event={event_candidate.get('reason')}; "
                f"reversal={reversal_candidate.get('reason')}",
                'REJECTED_ADAPTIVE_REGIME_ENSEMBLE',
            )

        tradfi_small_account_guardrail = {
            'profile': TRADFI_SMALL_ACCOUNT_PROFILE_VERSION,
            'allowed': True,
            'code': 'TRADFI_SMALL_ACCOUNT_CONTEXT_NOT_APPLICABLE',
            'reason': 'TradFi small-account context not applicable',
            'risk_tier_ceiling': None,
            'leverage_ceiling': int(
                trend_cfg.get('small_account_elite_leverage', 7) or 7
            ),
        }
        if is_tradfi and small_account_aggressive_candidate:
            tradfi_small_account_guardrail = evaluate_tradfi_small_account_guardrails(
                symbol=canonical,
                side=candidate_resolution.get('side'),
                candidate_source=candidate_resolution.get('source'),
                session_status=tradfi_context.get('session_status'),
                futures_context=futures_context,
                underlying_type=tradfi_context.get('underlying_type'),
                instrument_profile=tradfi_context.get('instrument_profile'),
            )
            status['tradfi_small_account_guardrail'] = dict(
                tradfi_small_account_guardrail
            )
            if not bool(tradfi_small_account_guardrail.get('allowed')):
                return _finish(
                    None,
                    'TradFi small-account waiting: '
                    f"{tradfi_small_account_guardrail.get('reason')}",
                    tradfi_small_account_guardrail.get('code')
                    or 'REJECTED_TRADFI_SMALL_ACCOUNT_CONTEXT',
                )

        candidate_side = str(candidate_resolution.get('side') or '').lower()
        l2_gate = await self._evaluate_shared_l2_gate(
            canonical,
            cfg,
            force_refresh=True,
            side=candidate_side,
        )
        candidate_source = str(
            candidate_resolution.get('source') or 'trend_only'
        )
        event_context = {
            'allowed': True,
            'code': 'INDEPENDENT_EVENT_CONTEXT_NOT_APPLICABLE',
            'reason': 'independent event context not applicable',
        }
        if candidate_source in {'event_only', 'event_conflict_winner'}:
            event_context = evaluate_independent_event_context(
                candidate_side,
                getattr(preliminary, 'metrics', None),
                config=trend_cfg,
            )
            status['independent_event_context'] = dict(event_context)
            if not bool(event_context.get('allowed')):
                return _finish(
                    None,
                    f"Independent event waiting: {event_context.get('reason')}",
                    event_context.get('code')
                    or 'REJECTED_INDEPENDENT_EVENT_CONTEXT',
                )
        if candidate_source in {'event_only', 'event_conflict_winner'}:
            decision = self._adaptive_trend_event_decision(event_candidate)
            decision_entry_timeframe = event_timeframe
        elif candidate_source == 'exhaustion_reversal':
            decision = self._adaptive_trend_reversal_decision(
                reversal_candidate
            )
            decision_entry_timeframe = event_timeframe
        else:
            decision = evaluate_adaptive_breakout_trend(rows, l2_gate, trend_cfg)
            if tradfi_profile_applied:
                decision = evaluate_tradfi_pattern_profile(
                    rows,
                    decision,
                    symbol=canonical,
                    underlying_type=tradfi_context.get('underlying_type'),
                    higher_timeframe_rows=tradfi_context.get('higher_timeframe_rows'),
                    daily_rows=tradfi_context.get('daily_rows'),
                    benchmark_directions=tradfi_context.get('benchmark_directions'),
                    session_status=tradfi_context.get('session_status'),
                    config=profile_cfg,
                )
            decision_entry_timeframe = timeframe
        metrics = dict(decision.metrics or {})
        status.update({
            'allowed': bool(decision.allowed),
            'side': decision.side,
            'score': float(decision.score),
            'risk_multiplier': float(decision.risk_multiplier),
            'risk_tier': metrics.get('risk_tier'),
            'metrics': metrics,
            'l2_gate': dict(l2_gate or {}),
            'decision_candle_ts': metrics.get('signal_candle_ts'),
            'candidate_source': candidate_source,
            'candidate_agreement': candidate_resolution.get('agreement'),
        })
        if not decision.allowed or decision.side not in {'long', 'short'}:
            return _finish(None, f'Adaptive Breakout Trend waiting: {decision.reason}')
        side = decision.side
        if not self.is_trade_direction_allowed(side):
            return _finish(
                None,
                self.format_trade_direction_block_reason(side),
                'REJECTED_DIRECTION_FILTER',
            )

        selector_quality_builder = getattr(
            self,
            '_build_utbreakout_selector_quality',
            None,
        )
        selector_quality = (
            selector_quality_builder(canonical)
            if callable(selector_quality_builder)
            else {}
        )
        selector_candidate = (
            selector_quality.get('candidate')
            if isinstance(selector_quality, dict)
            and isinstance(selector_quality.get('candidate'), dict)
            else {}
        )
        rotation_score = _safe_float_or_none(
            selector_candidate.get('convex_rotation_score')
        )
        rotation_percentile = _safe_float_or_none(
            selector_candidate.get('convex_rotation_percentile')
        )
        rotation_tier = str(
            selector_candidate.get('convex_rotation_tier') or ''
        ).strip().lower()
        selector_side = str(
            selector_candidate.get('adaptive_breakout_trend_side') or ''
        ).strip().lower()
        selector_tier_valid = bool(
            rotation_tier in {'base', 'strong', 'elite'}
            and selector_candidate.get('adaptive_breakout_trend_allowed')
            and selector_side == side
        )
        absolute_risk_tier = str(
            metrics.get('risk_tier') or 'base'
        ).strip().lower()
        if absolute_risk_tier not in _ADAPTIVE_RISK_TIER_ORDER:
            absolute_risk_tier = 'base'
        effective_risk_tier = _resolve_adaptive_trend_risk_tier(
            absolute_risk_tier,
            rotation_tier,
            relative_valid=selector_tier_valid,
            tradfi=is_tradfi,
        )
        # The reversal challenger is intentionally a base-sized, finite-target
        # trade. Cross-sectional rank must not promote it into a trend-sized
        # strong/elite allocation.
        if candidate_source == 'exhaustion_reversal':
            effective_risk_tier = 'base'
        pre_tradfi_guardrail_risk_tier = effective_risk_tier
        if is_tradfi and small_account_aggressive_candidate:
            effective_risk_tier = cap_tradfi_risk_tier(
                effective_risk_tier,
                tradfi_small_account_guardrail.get('risk_tier_ceiling'),
            )
        tradfi_tier_floor_applied = bool(
            is_tradfi
            and selector_tier_valid
            and _ADAPTIVE_RISK_TIER_ORDER[absolute_risk_tier]
            > _ADAPTIVE_RISK_TIER_ORDER[rotation_tier]
        )
        status.update({
            'convex_rotation_score': rotation_score,
            'convex_rotation_percentile': rotation_percentile,
            'convex_rotation_rank': selector_candidate.get('convex_rotation_rank'),
            'convex_rotation_tier': effective_risk_tier,
            'convex_rotation_relative_tier_applied': selector_tier_valid,
            'convex_rotation_profile': selector_candidate.get(
                'convex_rotation_profile'
            ),
            'adaptive_trend_absolute_risk_tier': absolute_risk_tier,
            'tradfi_relative_rank_upgrade_only': bool(is_tradfi),
            'tradfi_absolute_tier_floor_applied': tradfi_tier_floor_applied,
            'risk_tier': effective_risk_tier,
            'tradfi_small_account_profile': (
                TRADFI_SMALL_ACCOUNT_PROFILE_VERSION
                if is_tradfi and small_account_aggressive_candidate
                else None
            ),
            'tradfi_pre_guardrail_risk_tier': pre_tradfi_guardrail_risk_tier,
            'tradfi_risk_tier_cap_applied': bool(
                effective_risk_tier != pre_tradfi_guardrail_risk_tier
            ),
        })

        _, daily_pnl = self.db.get_daily_stats()
        daily_entries = self.get_automatic_daily_entry_count()
        status['daily_pnl'] = daily_pnl
        status['daily_entries'] = daily_entries
        daily_trade_limit = int(
            await self.get_effective_automatic_daily_trade_limit_for_entry()
            if hasattr(self, 'get_effective_automatic_daily_trade_limit_for_entry')
            else cfg.get('max_daily_trades', 0) or 0
        )
        if daily_trade_limit > 0 and daily_entries >= daily_trade_limit:
            return _finish(
                None,
                f'risk_limit_blocked: daily trade count {daily_entries}',
                'REJECTED_DAILY_TRADE_LIMIT',
            )

        reference_price = _safe_float_or_none(metrics.get('reference_price'))
        atr_value = _safe_float_or_none(metrics.get('atr'))
        structure_stop = _safe_float_or_none(metrics.get('structure_stop'))
        if reference_price is None or reference_price <= 0 or atr_value is None or atr_value <= 0:
            return _finish(
                None,
                'Adaptive Breakout Trend reference price/ATR unavailable',
                'REJECTED_ADAPTIVE_TREND_DATA',
            )
        try:
            ticker = await asyncio.to_thread(self.market_data_exchange.fetch_ticker, canonical)
            entry_price = _safe_float_or_none((ticker or {}).get('last') or (ticker or {}).get('close'))
        except Exception as exc:
            return _finish(
                None,
                f'Adaptive Breakout Trend live price unavailable: {exc}',
                'REJECTED_ADAPTIVE_TREND_DATA',
            )
        if entry_price is None or entry_price <= 0:
            return _finish(
                None,
                'Adaptive Breakout Trend live price unavailable',
                'REJECTED_ADAPTIVE_TREND_DATA',
            )

        chase_atr = (
            (entry_price - reference_price) / atr_value
            if side == 'long'
            else (reference_price - entry_price) / atr_value
        )
        status['entry_chase_atr'] = chase_atr
        if chase_atr > float(trend_cfg.get('entry_chase_max_atr', 0.80) or 0.80):
            return _finish(
                None,
                f'Adaptive Breakout Trend entry moved {chase_atr:.2f} ATR beyond its signal',
                'REJECTED_ADAPTIVE_TREND_STALE_CHASE',
            )
        if structure_stop is not None and (
            (side == 'long' and entry_price <= structure_stop)
            or (side == 'short' and entry_price >= structure_stop)
        ):
            return _finish(
                None,
                'Adaptive Breakout Trend structure invalidated before order',
                'REJECTED_ADAPTIVE_TREND_INVALIDATED',
            )

        filter_values = {
            'entry_price': entry_price,
            'entry_timeframe': decision_entry_timeframe,
            'atr': atr_value,
            'atr_pct': atr_value / entry_price * 100.0,
        }
        market_quality = self._evaluate_utbreakout_market_quality(side, cfg, filter_values)
        status['market_quality'] = market_quality
        if market_quality.get('hard_block') or market_quality.get('state') is False:
            return _finish(
                None,
                f"market_quality_rejected: {market_quality.get('summary')}",
                'REJECTED_MARKET_QUALITY',
            )
        if not l2_gate.get('allowed', False):
            return _finish(
                None,
                f"L2 stressed: {l2_gate.get('reason')}",
                'REJECTED_L2_STRESSED',
            )

        effective_daily_loss_percent = (
            0.0
            if small_account_aggressive_candidate
            else float(trend_cfg.get('daily_loss_limit_percent', 10.0) or 0.0)
        )
        adaptive_daily_loss_cap = max(
            0.0,
            balance_for_risk
            * effective_daily_loss_percent
            / 100.0,
        )
        status['adaptive_daily_loss_cap_usdt'] = adaptive_daily_loss_cap
        status['small_account_aggressive_candidate'] = small_account_aggressive_candidate
        status['small_account_equity_usdt'] = balance_for_risk
        status['effective_daily_loss_percent'] = effective_daily_loss_percent
        small_account_entry_refinement = {
            'allowed': True,
            'code': 'SMALL_ACCOUNT_ENTRY_REFINEMENT_NOT_APPLICABLE',
            'reason': 'small-account entry refinement not applicable',
        }
        change_point_flow = {
            'allowed': True,
            'code': 'CHANGE_POINT_FLOW_NOT_APPLICABLE',
            'reason': 'change-point flow overlay not applicable',
            'state': 'not_applicable',
            'risk_tier': effective_risk_tier,
            'initial_margin_fraction': float(
                trend_cfg.get('small_account_initial_margin_fraction', 0.65)
                or 0.65
            ),
            'stop_atr_multiplier': float(
                trend_cfg.get('stop_atr_multiplier', 2.00) or 2.00
            ),
        }
        event_allocation = {
            'requested_risk_tier': effective_risk_tier,
            'risk_tier_cap': effective_risk_tier,
            'applied_risk_tier': effective_risk_tier,
            'risk_tier_capped': False,
            'initial_margin_fraction': change_point_flow.get(
                'initial_margin_fraction'
            ),
        }
        if small_account_aggressive_candidate:
            event_evaluations = (
                event_candidate.get('evaluations')
                if isinstance(event_candidate.get('evaluations'), dict)
                else {}
            )
            selected_event_evaluation = event_evaluations.get(side)
            if (
                candidate_source in {'event_only', 'event_conflict_winner', 'aligned'}
                and isinstance(selected_event_evaluation, dict)
            ):
                change_point_flow = dict(selected_event_evaluation)
            else:
                combined_futures_context = dict(selector_candidate)
                combined_futures_context.update(futures_context)
                change_point_flow = evaluate_change_point_flow_entry(
                    side,
                    event_rows,
                    futures_context=combined_futures_context,
                    trend_metrics=metrics,
                    config=trend_cfg.get('change_point_flow'),
                    tradfi=is_tradfi,
                )
            if candidate_source == 'exhaustion_reversal':
                # The challenger already performed its own participation and
                # broad-regime confirmation. Keep it out of the continuation
                # flow scorer so that neither a contradictory trend veto nor a
                # conviction upgrade changes its intentionally base risk.
                change_point_flow = {
                    'allowed': True,
                    'code': reversal_candidate.get('code'),
                    'reason': reversal_candidate.get('reason'),
                    'profile': reversal_candidate.get('profile'),
                    'state': 'exhaustion_reversal',
                    'risk_tier': 'base',
                    'initial_margin_fraction': reversal_candidate.get(
                        'initial_margin_fraction',
                        0.65,
                    ),
                    'stop_atr_multiplier': reversal_candidate.get(
                        'stop_atr_multiplier',
                        trend_cfg.get('stop_atr_multiplier', 2.00),
                    ),
                    'event_structure_stop': reversal_candidate.get(
                        'event_structure_stop'
                    ),
                }
            change_point_flow['event_timeframe'] = event_timeframe
            status['change_point_flow'] = dict(change_point_flow)
            event_path_required = candidate_source in {
                'event_only',
                'event_conflict_winner',
                'aligned',
            }
            if (
                event_path_required
                and not bool(change_point_flow.get('allowed'))
            ):
                return _finish(
                    None,
                    'Change-point flow waiting: '
                    f"{change_point_flow.get('reason')}",
                    change_point_flow.get('code')
                    or 'REJECTED_CHANGE_POINT_FLOW',
                )

            refinement_metrics = metrics
            if candidate_source in {'event_only', 'event_conflict_winner'}:
                preliminary_metrics = getattr(preliminary, 'metrics', None)
                if isinstance(preliminary_metrics, dict) and preliminary_metrics:
                    refinement_metrics = preliminary_metrics
            if candidate_source == 'exhaustion_reversal':
                small_account_entry_refinement = {
                    'allowed': True,
                    'code': 'SMALL_ACCOUNT_REVERSAL_REFINEMENT_COMPLETE',
                    'reason': (
                        'reversal-specific sweep, participation, and regime '
                        'checks already passed'
                    ),
                    'profile': reversal_candidate.get('profile'),
                    'entry_opportunity_score': reversal_candidate.get('score'),
                    'trend_clarity': reversal_candidate.get('trend_clarity'),
                    'pullback_resumption': False,
                    'pullback_recovery_confirmed': False,
                    'impulse_breakout': False,
                }
            else:
                small_account_entry_refinement = evaluate_small_account_entry_refinement(
                    side,
                    refinement_metrics,
                    entry_chase_atr=chase_atr,
                    selector_candidate=selector_candidate,
                    config=trend_cfg,
                )
            status['small_account_entry_refinement'] = dict(
                small_account_entry_refinement
            )
            if not bool(small_account_entry_refinement.get('allowed')):
                soft_veto_codes = {
                    'REJECTED_SMALL_ACCOUNT_FAST_TREND_DECAY',
                    'REJECTED_SMALL_ACCOUNT_WEAK_MATURE_CONTINUATION',
                }
                refinement_code = str(
                    small_account_entry_refinement.get('code') or ''
                )
                if (
                    refinement_code in soft_veto_codes
                    and bool(
                        change_point_flow.get('override_soft_mature_veto')
                    )
                ):
                    small_account_entry_refinement.update({
                        'allowed': True,
                        'overridden': True,
                        'override_code': change_point_flow.get('code'),
                        'reason': (
                            f"{small_account_entry_refinement.get('reason')}; "
                            'overridden by a fresh high-conviction regime/flow event'
                        ),
                    })
                    status['small_account_entry_refinement'] = dict(
                        small_account_entry_refinement
                    )
                else:
                    return _finish(
                        None,
                        'Small-account entry refinement waiting: '
                        f"{small_account_entry_refinement.get('reason')}",
                        refinement_code
                        or 'REJECTED_SMALL_ACCOUNT_ENTRY_REFINEMENT',
                    )

            flow_risk_tier = str(
                change_point_flow.get('risk_tier') or 'base'
            ).strip().lower()
            if (
                bool(change_point_flow.get('allowed'))
                and flow_risk_tier in _ADAPTIVE_RISK_TIER_ORDER
                and _ADAPTIVE_RISK_TIER_ORDER[flow_risk_tier]
                > _ADAPTIVE_RISK_TIER_ORDER[effective_risk_tier]
            ):
                effective_risk_tier = flow_risk_tier
            if candidate_source == 'exhaustion_reversal':
                effective_risk_tier = 'base'
            if candidate_source in {'event_only', 'event_conflict_winner'}:
                event_allocation = resolve_independent_event_allocation(
                    effective_risk_tier,
                    config=trend_cfg,
                )
                effective_risk_tier = str(
                    event_allocation['applied_risk_tier']
                )
                change_point_flow['initial_margin_fraction'] = min(
                    float(
                        change_point_flow.get('initial_margin_fraction', 1.0)
                        or 1.0
                    ),
                    float(event_allocation['initial_margin_fraction']),
                )
            # Flow conviction may upgrade the tier after the first TradFi cap.
            # Reapply the product/session/basis ceiling so an aligned event
            # cannot silently restore elite sizing outside the cash session or
            # through an adverse mark/index dislocation.
            if is_tradfi:
                pre_reapplied_tier = effective_risk_tier
                effective_risk_tier = cap_tradfi_risk_tier(
                    effective_risk_tier,
                    tradfi_small_account_guardrail.get('risk_tier_ceiling'),
                )
                change_point_flow['tradfi_guardrail_tier_before_reapply'] = (
                    pre_reapplied_tier
                )
                change_point_flow['tradfi_guardrail_tier_cap_reapplied'] = bool(
                    effective_risk_tier != pre_reapplied_tier
                )
            status['independent_event_allocation'] = dict(event_allocation)
            status['risk_tier'] = effective_risk_tier
            status['convex_rotation_tier'] = effective_risk_tier
        if (
            not small_account_aggressive_candidate
            and
            adaptive_daily_loss_cap > 0
            and float(daily_pnl or 0) <= -adaptive_daily_loss_cap
        ):
            return _finish(
                None,
                f'risk_limit_blocked: adaptive trend daily pnl {daily_pnl:.2f} '
                f'<= -{adaptive_daily_loss_cap:.2f}',
                'REJECTED_DAILY_LOSS_LIMIT',
            )
        common_cfg = self.get_runtime_common_settings()
        leverage = int(max(1.0, float(common_cfg.get('leverage', 5) or 5)))
        risk_multiplier = min(
            1.0,
            max(0.0, float(decision.risk_multiplier or 0.0)),
            max(0.0, float(market_quality.get('risk_multiplier', 1.0) or 1.0)),
            max(0.0, float(l2_gate.get('risk_multiplier', 0.0) or 0.0)),
        )
        volatility_scale = max(
            0.0,
            float(metrics.get('volatility_scale', 1.0) or 1.0),
        )
        target_risk_percent = float(
            trend_cfg.get(f'{effective_risk_tier}_risk_percent', 1.75) or 1.75
        ) * volatility_scale
        target_risk_percent = max(
            float(
                trend_cfg.get(
                    f'{effective_risk_tier}_risk_percent_min',
                    target_risk_percent,
                )
                or target_risk_percent
            ),
            min(
                float(
                    trend_cfg.get(
                        f'{effective_risk_tier}_risk_percent_max',
                        target_risk_percent,
                    )
                    or target_risk_percent
                ),
                target_risk_percent,
            ),
        )
        status['target_risk_percent'] = target_risk_percent
        # Adaptive Trend owns one absolute stop-loss budget. Shared market and
        # L2 quality remain hard safety gates above, but their correlated soft
        # reductions do not shrink the same stop budget a second time.
        trend_risk_cfg = dict(cfg)
        trend_risk_cfg.update({
            'risk_per_trade_percent': target_risk_percent,
            'min_risk_per_trade_percent': target_risk_percent,
            'max_risk_per_trade_percent': target_risk_percent,
            'daily_max_loss_usdt': (
                0.0
                if small_account_aggressive_candidate
                else adaptive_daily_loss_cap
            ),
        })
        risk_budget = resolve_utbreakout_risk_budget(
            balance_for_risk,
            trend_risk_cfg,
            multiplier=1.0,
            daily_pnl_usdt=(
                None if small_account_aggressive_candidate else daily_pnl
            ),
        )
        # A 20-bar structure point can be many ATR away in a healthy trend.
        # Using that distant point as the hard-stop anchor would shrink an
        # otherwise excellent entry to a token position. Keep the 2 ATR hard
        # risk budget and only use structure as a soft anchor when it is nearby.
        stop_atr_multiplier = float(
            change_point_flow.get('stop_atr_multiplier')
            if small_account_aggressive_candidate
            else trend_cfg.get('stop_atr_multiplier', 2.00)
            or 2.00
        )
        event_structure_stop = _safe_float_or_none(
            change_point_flow.get('event_structure_stop')
        )
        structure_for_risk = structure_stop
        if event_structure_stop is not None and (
            (side == 'long' and event_structure_stop < entry_price)
            or (side == 'short' and event_structure_stop > entry_price)
        ):
            event_structure_distance_atr = (
                abs(entry_price - event_structure_stop) / atr_value
            )
            status['change_point_flow_structure_distance_atr'] = (
                event_structure_distance_atr
            )
            if event_structure_distance_atr <= stop_atr_multiplier:
                structure_for_risk = event_structure_stop
        if structure_stop is not None:
            structure_distance_atr = abs(entry_price - structure_stop) / atr_value
            status['structure_distance_atr'] = structure_distance_atr
            if (
                structure_for_risk == structure_stop
                and structure_distance_atr > stop_atr_multiplier
            ):
                structure_for_risk = None
        try:
            plan = calculate_risk_plan(
                side=side,
                entry_price=entry_price,
                atr_value=atr_value,
                stop_atr_multiplier=stop_atr_multiplier,
                ut_stop=None,
                structure_stop=structure_for_risk,
                structure_buffer_atr=float(trend_cfg.get('structure_buffer_atr', 0.15) or 0.15),
                take_profit_r_multiple=float(trend_cfg.get('take_profit_r_multiple', 4.00) or 4.00),
                take_profit_front_run_atr=0.0,
                take_profit_front_run_pct=0.0,
                min_risk_reward=2.5,
                balance_usdt=balance_for_risk,
                risk_per_trade_percent=risk_budget['risk_per_trade_percent'],
                max_risk_per_trade_usdt=risk_budget['max_risk_per_trade_usdt'],
                leverage=leverage,
            )
            full_target_qty = float(plan.get('qty', 0.0) or 0.0)
            full_target_risk = float(plan.get('risk_usdt', 0.0) or 0.0)
            full_target_notional = float(plan.get('planned_notional', 0.0) or 0.0)
            initial_fraction_source = (
                change_point_flow.get('initial_margin_fraction')
                if small_account_aggressive_candidate
                else trend_cfg.get('initial_entry_fraction', 0.65)
            )
            initial_fraction = min(
                1.0,
                max(
                    0.40,
                    float(initial_fraction_source or 0.65),
                ),
            )
            plan.update({
                'qty': full_target_qty * initial_fraction,
                'risk_usdt': full_target_risk * initial_fraction,
                'planned_notional': full_target_notional * initial_fraction,
                'planned_margin': full_target_notional * initial_fraction / max(float(leverage), 1.0),
                'expected_profit_usdt': float(plan.get('expected_profit_usdt', 0.0) or 0.0) * initial_fraction,
                'adaptive_trend_target_qty': full_target_qty,
                'adaptive_trend_target_risk_usdt': full_target_risk,
                'adaptive_trend_target_notional': full_target_notional,
                'adaptive_trend_initial_fraction': initial_fraction,
                # Dynamic leverage may restore only a margin-capped initial
                # order; winner-only additions remain staged.
                'position_cap_original_notional': full_target_notional * initial_fraction,
            })
            plan = cap_utbreakout_risk_plan_to_margin(
                plan,
                free_balance=free_balance,
                leverage=leverage,
                entry_price=entry_price,
            )
        except ValueError as exc:
            return _finish(
                None,
                f'Adaptive Breakout Trend risk plan rejected: {exc}',
                'REJECTED_ADAPTIVE_TREND_RISK_PLAN',
            )

        plan.update({
            'strategy': ADAPTIVE_BREAKOUT_TREND_STRATEGY,
            'plan_symbol': canonical,
            'signal_candle_ts': metrics.get('signal_candle_ts'),
            'entry_timeframe': decision_entry_timeframe,
            'timeframe': decision_entry_timeframe,
            'exit_timeframe': '15m',
            'htf_timeframe': '4h',
            'entry_execution': 'market',
            'adaptive_breakout_trend_score': float(decision.score),
            'adaptive_breakout_trend_risk_multiplier': risk_multiplier,
            'adaptive_breakout_trend_target_risk_percent': target_risk_percent,
            'convex_rotation_score': rotation_score,
            'convex_rotation_percentile': rotation_percentile,
            'convex_rotation_rank': selector_candidate.get('convex_rotation_rank'),
            'convex_rotation_universe_size': selector_candidate.get(
                'convex_rotation_universe_size'
            ),
            'convex_rotation_tier': effective_risk_tier,
            'convex_rotation_profile': selector_candidate.get(
                'convex_rotation_profile'
            ),
            'adaptive_trend_absolute_risk_tier': absolute_risk_tier,
            'tradfi_relative_rank_upgrade_only': bool(is_tradfi),
            'tradfi_absolute_tier_floor_applied': tradfi_tier_floor_applied,
            'convex_rotation_entry_reacceleration': bool(
                metrics.get('continuation_reacceleration')
                or metrics.get('compression_breakout')
                or metrics.get('pullback_resumption')
                or metrics.get('impulse_breakout')
            ),
            'entry_profile_version': (
                TRADFI_PATTERN_PROFILE_VERSION
                if tradfi_profile_applied
                else ADAPTIVE_TREND_PORTFOLIO_PROFILE_VERSION
            ),
            'tradfi_pattern_profile_applied': tradfi_profile_applied,
            # The ROE staircase is shared with crypto small accounts. Keep an
            # explicit marker on TradFi plans so restart/recovery tests can
            # prove that this exit policy was selected without changing any
            # TradFi entry, leverage, sizing, session, or basis rule.
            'tradfi_small_account_roe_profit_lock_applied': bool(
                tradfi_profile_applied
                and small_account_aggressive_candidate
                and trend_cfg.get('small_account_roe_profit_lock_enabled', True)
            ),
            'risk_budget_mode': (
                'adaptive_trend_small_account_aggressive_pending'
                if small_account_aggressive_candidate
                else 'adaptive_trend_unified'
            ),
            'adaptive_trend_risk_tier': effective_risk_tier,
            'adaptive_breakout_trend_metrics': metrics,
            'trend_event_candidate_source': candidate_source,
            'trend_event_candidate_agreement': candidate_resolution.get(
                'agreement'
            ),
            'trend_event_candidate_score': candidate_resolution.get('score'),
            'trend_event_trend_score': candidate_resolution.get('trend_score'),
            'trend_event_event_score': candidate_resolution.get('event_score'),
            'adaptive_regime_engine': candidate_resolution.get('regime_engine'),
            'adaptive_regime_profile': (
                (
                    candidate_resolution.get('regime_profile')
                    or SMALL_ACCOUNT_REGIME_PROFILE_VERSION
                )
                if small_account_aggressive_candidate
                else None
            ),
            'adaptive_regime_reversal_score': candidate_resolution.get(
                'reversal_score'
            ),
            'adaptive_regime_selected_net_edge': candidate_resolution.get(
                'selected_net_edge'
            ),
            'adaptive_regime_primary_net_edge': candidate_resolution.get(
                'primary_net_edge'
            ),
            'adaptive_regime_reversal_net_edge': candidate_resolution.get(
                'reversal_net_edge'
            ),
            'adaptive_regime_multitimeframe': dict(
                multi_timeframe_context
            ),
            'small_account_regime_transition': (
                multi_timeframe_context.get('transition')
            ),
            'small_account_multi_speed_agreement': (
                multi_timeframe_context.get('multi_speed_agreement')
            ),
            'small_account_regime_persistence_score': (
                multi_timeframe_context.get('persistence_score')
            ),
            'adaptive_regime_promotion': dict(regime_promotion_status),
            'small_account_aggressive_enabled': bool(
                trend_cfg.get('small_account_aggressive_enabled', True)
            ),
            'small_account_equity_threshold_usdt': small_account_threshold,
            'small_account_margin_budget_fraction': float(
                trend_cfg.get('small_account_margin_budget_fraction', 0.95) or 0.95
            ),
            'small_account_initial_margin_fraction': float(
                change_point_flow.get('initial_margin_fraction')
                if small_account_aggressive_candidate
                else trend_cfg.get('small_account_initial_margin_fraction', 0.65)
                or 0.65
            ),
            'small_account_base_max_loss_percent': float(
                trend_cfg.get('small_account_base_max_loss_percent', 20.0) or 20.0
            ),
            'small_account_strong_max_loss_percent': float(
                trend_cfg.get('small_account_strong_max_loss_percent', 30.0) or 30.0
            ),
            'small_account_elite_max_loss_percent': float(
                trend_cfg.get('small_account_elite_max_loss_percent', 35.0) or 35.0
            ),
            'small_account_daily_loss_limit_percent': 0.0,
            'small_account_cost_buffer_percent': float(
                trend_cfg.get('small_account_cost_buffer_percent', 0.20) or 0.20
            ),
            'small_account_liquidation_stop_buffer_multiple': float(
                trend_cfg.get(
                    'small_account_liquidation_stop_buffer_multiple',
                    1.50,
                )
                or 1.50
            ),
            'small_account_min_leverage': int(
                trend_cfg.get('small_account_min_leverage', 4) or 4
            ),
            'small_account_strong_leverage': int(
                trend_cfg.get('small_account_strong_leverage', 6) or 6
            ),
            'small_account_elite_leverage': int(
                min(
                    profile_cfg.get('maximum_leverage', 10),
                    trend_cfg.get('small_account_elite_leverage', 7) or 7,
                )
                if tradfi_profile_applied
                else trend_cfg.get('small_account_elite_leverage', 7) or 7
            ),
            'small_account_leverage_steps': tuple(
                trend_cfg.get('small_account_leverage_steps', (4, 5, 6, 7))
            ),
            'small_account_aggressive_leverage_ceiling': (
                min(
                    int(profile_cfg.get('maximum_leverage', 10)),
                    int(trend_cfg.get('small_account_elite_leverage', 7) or 7),
                    int(tradfi_small_account_guardrail.get('leverage_ceiling', 7) or 7),
                )
                if tradfi_profile_applied and small_account_aggressive_candidate
                else min(
                    int(profile_cfg.get('maximum_leverage', 10)),
                    int(trend_cfg.get('small_account_elite_leverage', 7) or 7),
                )
                if tradfi_profile_applied
                else int(trend_cfg.get('small_account_elite_leverage', 7) or 7)
            ),
            'strategy_leverage_ceiling': (
                min(
                    int(profile_cfg.get('maximum_leverage', 10)),
                    int(tradfi_small_account_guardrail.get('leverage_ceiling', 7) or 7),
                )
                if tradfi_profile_applied and small_account_aggressive_candidate
                else int(profile_cfg.get('maximum_leverage', 10))
                if tradfi_profile_applied
                else None
            ),
            'tradfi_underlying_type': tradfi_context.get('underlying_type'),
            'tradfi_instrument_profile': dict(
                tradfi_context.get('instrument_profile') or {}
            ),
            'tradfi_small_account_guardrail': dict(
                tradfi_small_account_guardrail
            ),
            'small_account_roe_profit_lock_enabled': bool(
                trend_cfg.get('small_account_roe_profit_lock_enabled', True)
            ),
            'small_account_roe_profit_lock_first_trigger_percent': float(
                trend_cfg.get(
                    'small_account_roe_profit_lock_first_trigger_percent', 5.0
                ) or 5.0
            ),
            'small_account_roe_profit_lock_second_trigger_percent': float(
                trend_cfg.get(
                    'small_account_roe_profit_lock_second_trigger_percent', 10.0
                ) or 10.0
            ),
            'small_account_roe_profit_lock_step_percent': float(
                trend_cfg.get('small_account_roe_profit_lock_step_percent', 10.0)
                or 10.0
            ),
            'small_account_roe_profit_lock_atr_multiplier': float(
                trend_cfg.get('small_account_roe_profit_lock_atr_multiplier', 0.50)
                or 0.50
            ),
            'small_account_roe_profit_lock_min_gap_percent': float(
                trend_cfg.get('small_account_roe_profit_lock_min_gap_percent', 1.0)
                or 1.0
            ),
            'small_account_roe_profit_lock_max_gap_percent': float(
                trend_cfg.get('small_account_roe_profit_lock_max_gap_percent', 3.0)
                or 3.0
            ),
            'small_account_roe_profit_lock_min_floor_percent': float(
                trend_cfg.get('small_account_roe_profit_lock_min_floor_percent', 1.0)
                or 1.0
            ),
            'small_account_aggressive_daily_pnl_usdt': float(daily_pnl or 0.0),
            'small_account_entry_refinement_profile': (
                small_account_entry_refinement.get('profile')
            ),
            'small_account_fast_momentum_retention': (
                small_account_entry_refinement.get('fast_momentum_retention')
            ),
            'small_account_lower_timeframe_side': (
                small_account_entry_refinement.get('lower_timeframe_side')
            ),
            'small_account_entry_opportunity_score': (
                small_account_entry_refinement.get('entry_opportunity_score')
            ),
            'small_account_trend_clarity': (
                small_account_entry_refinement.get('trend_clarity')
            ),
            'small_account_pullback_resumption': bool(
                small_account_entry_refinement.get('pullback_resumption')
            ),
            'small_account_pullback_recovery_confirmed': bool(
                small_account_entry_refinement.get(
                    'pullback_recovery_confirmed'
                )
            ),
            'small_account_impulse_breakout': bool(
                small_account_entry_refinement.get('impulse_breakout')
            ),
            'change_point_flow_profile': change_point_flow.get('profile'),
            'change_point_flow_state': change_point_flow.get('state'),
            'change_point_flow_code': change_point_flow.get('code'),
            'change_point_flow_total_score': change_point_flow.get('total_score'),
            'change_point_flow_price_score': change_point_flow.get('price_score'),
            'change_point_flow_flow_score': change_point_flow.get('flow_score'),
            'change_point_flow_open_interest_score': change_point_flow.get(
                'open_interest_score'
            ),
            'change_point_flow_regime_score': change_point_flow.get(
                'regime_change_score'
            ),
            'change_point_flow_orderflow_age_seconds': change_point_flow.get(
                'orderflow_age_seconds'
            ),
            'change_point_flow_orderflow_stale': bool(
                change_point_flow.get('orderflow_stale')
            ),
            'change_point_flow_event_timeframe': change_point_flow.get(
                'event_timeframe'
            ),
            'change_point_flow_stop_atr_multiplier': stop_atr_multiplier,
            'change_point_flow_event_structure_stop': event_structure_stop,
            'change_point_flow_soft_veto_override': bool(
                small_account_entry_refinement.get('overridden')
            ),
            'independent_event_context_profile': event_context.get('profile'),
            'independent_event_context_broad_side': event_context.get(
                'broad_side'
            ),
            'independent_event_context_weighted_momentum': event_context.get(
                'weighted_momentum'
            ),
            'independent_event_context_dominant_votes': event_context.get(
                'dominant_votes'
            ),
            'independent_event_context_minimum_votes': event_context.get(
                'minimum_votes'
            ),
            'independent_event_context_broad_conflict_min_momentum': event_context.get(
                'broad_conflict_min_momentum'
            ),
            'independent_event_context_fast_ema_distance_atr': event_context.get(
                'fast_ema_distance_atr'
            ),
            'independent_event_context_max_fast_ema_distance_atr': event_context.get(
                'max_fast_ema_distance_atr'
            ),
            'independent_event_risk_tier_cap': event_allocation.get(
                'risk_tier_cap'
            ),
            'independent_event_risk_tier_capped': bool(
                event_allocation.get('risk_tier_capped')
            ),
            'structure_reference_stop': structure_stop,
            'entry_chase_atr': chase_atr,
            'l2_gate': dict(l2_gate or {}),
            'l2_state': l2_gate.get('state'),
            'l2_risk_multiplier': l2_gate.get('risk_multiplier'),
            'market_quality_summary': market_quality.get('summary'),
            'market_quality_risk_multiplier': market_quality.get('risk_multiplier'),
            'atr': atr_value,
            'atr_pct': atr_value / entry_price * 100.0,
            'partial_take_profit_enabled': True,
            'partial_take_profit_r_multiple': float(trend_cfg.get('partial_take_profit_r_multiple', 2.00) or 2.00),
            'partial_take_profit_ratio': float(trend_cfg.get('partial_take_profit_ratio', 0.15) or 0.15),
            'second_take_profit_enabled': False,
            'second_take_profit_r_multiple': float(trend_cfg.get('take_profit_r_multiple', 10.00) or 10.00),
            'second_take_profit_ratio': 0.0,
            'runner_pct': float(trend_cfg.get('runner_pct', 0.85) or 0.85),
            'preserve_runner_qty': True,
            'atr_trailing_enabled': True,
            'atr_trailing_activation_r': float(trend_cfg.get('atr_trailing_activation_r', 2.00) or 2.00),
            'atr_trailing_multiplier': float(trend_cfg.get('atr_trailing_multiplier', 3.80) or 3.80),
            'runner_exit_enabled': True,
            'runner_chandelier_enabled': True,
            'runner_chandelier_lookback': 48,
            'runner_structure_lookback': 12,
            'tp1_breakeven_enabled': True,
            'tp1_breakeven_wait_for_partial': True,
            'adaptive_trend_pyramid_enabled': bool(trend_cfg.get('pyramiding_enabled', True)),
            'adaptive_trend_pyramid_trigger_r': tuple(trend_cfg.get('pyramid_trigger_r', (0.50, 1.00, 1.50))),
            'adaptive_trend_pyramid_target_fractions': tuple(trend_cfg.get('pyramid_target_fractions', (0.80, 0.90, 1.00))),
            'adaptive_trend_pyramid_add_count': 0,
            'ev_time_stop_enabled': True,
            'ev_time_stop_bars': int(trend_cfg.get('time_stop_hours', 168) or 168) * 4,
            'ev_time_stop_min_mfe_r': 0.35,
            'ev_time_stop_max_current_r': 0.0,
            'convex_rotation_exit_enabled': bool(
                trend_cfg.get('rotation_exit_enabled', True)
            ),
            'convex_rotation_min_holding_bars': int(
                trend_cfg.get('rotation_min_holding_hours', 8) or 8
            ) * 4,
            'convex_rotation_max_holding_bars': int(
                trend_cfg.get('rotation_max_holding_hours', 12) or 12
            ) * 4,
            'convex_rotation_max_mfe_r': float(
                trend_cfg.get('rotation_max_mfe_r', 0.35) or 0.35
            ),
            'convex_rotation_max_current_r': float(
                trend_cfg.get('rotation_max_current_r', 0.25) or 0.25
            ),
            'convex_rotation_rank_percentile_floor': float(
                trend_cfg.get('rotation_rank_percentile_floor', 35.0) or 35.0
            ),
            'convex_rotation_rank_confirmations': int(
                trend_cfg.get('rotation_rank_confirmations', 2) or 2
            ),
        })
        plan.update(
            reversal_exit_plan_overrides(
                candidate_source,
                reversal_candidate,
                trend_cfg.get('small_account_regime_ensemble'),
            )
        )
        self._set_utbot_filtered_breakout_entry_plan(canonical, plan)
        status['entry_plan'] = dict(plan)
        return _finish(side, f'ACCEPTED_ENTRY: {decision.reason}')

    async def build_adaptive_breakout_trend_status_text(self, symbol=None):
        strategy_getter = getattr(self, 'get_runtime_strategy_params', None)
        strategy_params = strategy_getter() if callable(strategy_getter) else {}
        filtered_cfg = (
            strategy_params.get('UTBotFilteredBreakoutV1', {})
            if isinstance(strategy_params, dict)
            else {}
        )
        trend_cfg = normalize_adaptive_breakout_trend_config(
            filtered_cfg.get('adaptive_breakout_trend')
            if isinstance(filtered_cfg, dict)
            else None
        )
        universe_mode = str(trend_cfg.get('universe_mode', 'auto') or 'auto')
        single_symbol = str(trend_cfg.get('single_symbol', '') or '').strip()
        configured_target = single_symbol if universe_mode == 'single' else None
        target = self._canonical_futures_symbol(
            symbol
            or configured_target
            or self.current_utbreakout_candidate_symbol
            or 'BTC/USDT'
        )
        universe_text = (
            f'single / {single_symbol or "not configured"}'
            if universe_mode == 'single'
            else 'automatic scanner'
        )
        status = dict(
            (getattr(self, 'adaptive_breakout_trend_last_status', {}) or {}).get(target)
            or {}
        )
        if not status:
            scan_scope = str(self.get_automatic_scan_scope() or '').strip().lower()
            ctrl = getattr(self, 'ctrl', None)
            exchange_mode_getter = getattr(ctrl, 'get_exchange_mode', None)
            exchange_mode = (
                str(exchange_mode_getter() or '').strip().lower()
                if callable(exchange_mode_getter)
                else ''
            )
            if universe_mode == 'single':
                detail = '지정한 단일 코인의 완료된 1시간봉 평가를 기다리는 중입니다.'
            elif scan_scope == 'tradfi_only' and exchange_mode != 'binance_mainnet':
                detail = (
                    '스캐너 차단: 테스트넷에서는 TradFi ONLY 상품을 주문할 수 없습니다. '
                    '스캔 범위를 순수 코인 ONLY 또는 전체 허용으로 변경하세요.'
                )
            else:
                selector_report = getattr(self, 'coin_selector_last_result', None)
                selector_report = selector_report if isinstance(selector_report, dict) else {}
                selected = selector_report.get('selected') or []
                watch_only = selector_report.get('watch_only') or []
                if selector_report and not selected and not watch_only:
                    reject_counts = selector_report.get('reject_counts') or {}
                    reject_text = ', '.join(
                        f'{key}={value}' for key, value in reject_counts.items()
                    )
                    detail = (
                        f'스캐너 후보 없음: scan_scope={scan_scope or "unknown"}'
                        + (f' / {reject_text}' if reject_text else '')
                    )
                else:
                    detail = '아직 완료된 1시간봉 평가 기록이 없습니다.'
            return '\n'.join([
                '📈 Adaptive Breakout Trend 상태',
                f'Symbol: {target}',
                f'Universe: {universe_text}',
                f'진입하지 않은 이유: {detail}',
            ])
        metrics = status.get('metrics') if isinstance(status.get('metrics'), dict) else {}
        votes = metrics.get('horizon_votes') if isinstance(metrics.get('horizon_votes'), dict) else {}
        vote_text = ', '.join(
            f'{key}h={str(value or "NONE").upper()}' for key, value in votes.items()
        )
        breakout_mode = (
            'TradFi pattern OR'
            if metrics.get('tradfi_entry_mode') == 'pattern_or_entry'
            else f"EMA crossover ({int(metrics.get('ema_crossover_age_bars', 0) or 0)}h ago)"
            if metrics.get('ema_crossover')
            else 'compression breakout'
            if metrics.get('compression_breakout')
            else 'impulse breakout'
            if metrics.get('impulse_breakout')
            else 'pullback resumption'
            if metrics.get('pullback_resumption')
            else 're-acceleration'
            if metrics.get('breakout_entry_enabled') and metrics.get('reacceleration')
            else 'weighted continuation'
            if metrics.get('weighted_continuation')
            else 'waiting'
        )
        risk_tier = str(status.get('risk_tier') or 'base').lower()
        small_account_active = bool(status.get('small_account_aggressive_candidate'))
        entry_diagnostic = build_entry_diagnostic(
            self,
            target,
            fallback_reason=status.get('reason'),
            status=status,
        )
        if small_account_active:
            max_loss_percent = float(
                trend_cfg.get(
                    f'small_account_{risk_tier}_max_loss_percent',
                    {'base': 20.0, 'strong': 30.0, 'elite': 35.0}.get(risk_tier, 20.0),
                )
                or 0.0
            )
            risk_status_text = (
                f"Small-account aggressive: ON / equity "
                f"{float(status.get('small_account_equity_usdt', 0.0) or 0.0):.2f} USDT / "
                f"tier loss cap {max_loss_percent:.1f}%"
            )
        else:
            risk_status_text = (
                f"Risk target: {float(status.get('target_risk_percent', metrics.get('target_risk_percent', 0.0)) or 0.0):.2f}% "
                f"/ tier={status.get('risk_tier') or 'waiting'}"
            )
        try:
            fast_retention_text = f"{float(metrics.get('fast_momentum_retention')):.2f}"
        except (TypeError, ValueError):
            fast_retention_text = "N/A"
        lines = [
            '📈 Adaptive Breakout Trend 상태',
            f'Symbol: {target}',
            f'Universe: {universe_text}',
            f"Signal: {str(status.get('side') or 'NONE').upper()} / allowed={bool(status.get('allowed'))}",
            f"Score: {float(status.get('score', 0.0) or 0.0):.1f}",
            (
                f"Rotation: {float(status.get('convex_rotation_score', 0.0) or 0.0):.1f} "
                f"/ percentile {float(status.get('convex_rotation_percentile', 0.0) or 0.0):.1f} "
                f"/ tier {status.get('convex_rotation_tier') or 'N/A'}"
            ),
            risk_status_text,
            f'Horizons: {vote_text or "N/A"}',
            f"Momentum: {float(metrics.get('weighted_momentum', 0.0) or 0.0):+.2f}",
            f"Fast trend retention: {fast_retention_text}",
            f"Entry mode: {breakout_mode}",
            f"Entry opportunity: {float(metrics.get('entry_opportunity_score', 0.0) or 0.0):.1f}",
            f"Trend clarity: {float(metrics.get('trend_clarity', 0.0) or 0.0):.2f}",
            f"Trend efficiency: {float(metrics.get('trend_efficiency', 0.0) or 0.0):.2f}",
            f"Volatility scale: {float(metrics.get('volatility_scale', 0.0) or 0.0):.2f}",
            f"L2: {(status.get('l2_gate') or {}).get('state') or 'N/A'}",
            f"진입하지 않은 이유: {entry_diagnostic.get('message')}",
            f"Reason: {status.get('reason') or '-'}",
        ]
        candidate_resolution = status.get('trend_event_resolution')
        if isinstance(candidate_resolution, dict):
            lines.insert(
                7,
                "후보 통합: "
                f"{candidate_resolution.get('source') or 'none'} / "
                f"{str(candidate_resolution.get('side') or 'NONE').upper()} / "
                f"trend {float(candidate_resolution.get('trend_score', 0.0) or 0.0):.1f} / "
                f"event {float(candidate_resolution.get('event_score', 0.0) or 0.0):.1f}",
            )
        refinement = status.get('small_account_entry_refinement')
        if small_account_active and isinstance(refinement, dict):
            lines.insert(
                7,
                "Small-account entry refinement: "
                f"{'PASS' if refinement.get('allowed') else 'WAIT'} / "
                f"{refinement.get('reason') or '-'}",
            )
        flow_overlay = status.get('change_point_flow')
        if small_account_active and isinstance(flow_overlay, dict):
            lines.insert(
                8,
                "체제·주문흐름: "
                f"{str(flow_overlay.get('state') or 'waiting')} / "
                f"종합 {float(flow_overlay.get('total_score', 0.0) or 0.0):.1f} / "
                f"가격 {float(flow_overlay.get('price_score', 0.0) or 0.0):.1f} / "
                f"흐름 {float(flow_overlay.get('flow_score', 0.0) or 0.0):.1f} / "
                f"데이터 {'STALE 제외' if flow_overlay.get('orderflow_stale') else 'FRESH'}",
            )
        if status.get('tradfi_perpetual'):
            chart_patterns = metrics.get('tradfi_chart_patterns') or {}
            side = str(status.get('side') or '').lower()
            pattern_names = chart_patterns.get(side) if isinstance(chart_patterns, dict) else []
            rollout = status.get('tradfi_profile_rollout') or {}
            lines.insert(4, (
                f"TradFi profile: {status.get('tradfi_profile_version') or TRADFI_PATTERN_PROFILE_VERSION} "
                f"/ rollout={rollout.get('state') or 'unknown'}"
            ))
            lines.insert(5, f"Patterns: {', '.join(pattern_names or []) or 'none'}")
            lines.insert(6, (
                f"US regular session: "
                f"{'OPEN' if metrics.get('tradfi_regular_session_open') else 'CLOSED'} "
                f"({metrics.get('tradfi_session_reason') or 'unknown'})"
            ))
            lines.insert(7, (
                "TradFi ranking: trend-first / upgrade-only "
                f"(absolute={status.get('adaptive_trend_absolute_risk_tier') or 'N/A'}, "
                f"effective={status.get('risk_tier') or 'N/A'})"
            ))
        return '\n'.join(lines)

    def _crowding_unwind_runtime_config(self, cfg=None):
        source = dict(cfg or {})
        base = default_crowding_unwind_config()
        nested = source.get('crowding_unwind')
        if isinstance(nested, dict):
            base.update(nested)
        if 'crowding_unwind_live_enabled' in source:
            base['live_enabled'] = bool(source.get('crowding_unwind_live_enabled'))
        return base

    async def _calculate_crowding_unwind_signal(
        self,
        symbol,
        df,
        strategy_params,
        *,
        force_reprocess=False,
    ):
        cfg = self._get_utbot_filtered_breakout_config(strategy_params)
        crowd_cfg = self._crowding_unwind_runtime_config(cfg)
        canonical = self._canonical_futures_symbol(symbol)
        self._clear_utbot_filtered_breakout_entry_plan(canonical)
        status = {
            'strategy': STRATEGY_DISPLAY_NAMES.get(CROWDING_UNWIND_STRATEGY, 'FUNDING_OI_CROWDING_UNWIND'),
            'entry_strategy': CROWDING_UNWIND_STRATEGY,
            'symbol': canonical,
            'stage': 'waiting',
        }

        def _finish(sig, reason, code=None):
            status['reason'] = reason
            status['accepted_side'] = sig
            if code:
                status['reject_code'] = code
            if sig:
                status['accepted_code'] = 'ACCEPTED_ENTRY'
                status['stage'] = 'entry_ready'
            if not isinstance(getattr(self, 'crowding_unwind_last_status', None), dict):
                self.crowding_unwind_last_status = {}
            self.crowding_unwind_last_status[canonical] = dict(status)
            self._store_utbot_filtered_breakout_status(canonical, status)
            self.last_entry_reason[canonical] = reason
            return sig, reason, status

        if self.is_upbit_mode():
            return _finish(None, 'Crowding Unwind unsupported in Upbit mode', 'REJECTED_UNSUPPORTED_MODE')
        if not bool(crowd_cfg.get('enabled', True)) or not bool(crowd_cfg.get('live_enabled', False)):
            return _finish(None, 'Crowding Unwind live disabled', 'REJECTED_CROWDING_LIVE_DISABLED')
        try:
            ohlcv = await asyncio.to_thread(
                self.market_data_exchange.fetch_ohlcv,
                canonical,
                '15m',
                limit=220,
            )
            rows = self._relative_strength_pullback_rows_from_ohlcv(ohlcv)
            rows = completed_candle_rows(
                rows,
                '15m',
                {'exclude_incomplete_live_candle': True},
                now_ms=int(time.time() * 1000.0),
            )
        except Exception as exc:
            return _finish(None, f'Crowding 15m data unavailable: {exc}', 'REJECTED_CROWDING_DATA')
        derivatives = await self._fetch_utbreakout_futures_context(canonical)
        base_l2 = await self._evaluate_shared_l2_gate(
            canonical,
            cfg,
            force_refresh=force_reprocess,
        )
        preliminary = evaluate_crowding_unwind(rows, derivatives, base_l2, crowd_cfg)
        side = preliminary.side
        l2_gate = await self._evaluate_shared_l2_gate(
            canonical,
            cfg,
            force_refresh=True,
            side=side,
        ) if side in {'long', 'short'} else base_l2
        decision = evaluate_crowding_unwind(rows, derivatives, l2_gate, crowd_cfg)
        status.update({
            'allowed': bool(decision.allowed),
            'side': decision.side,
            'score': float(decision.score),
            'risk_multiplier': float(decision.risk_multiplier),
            'metrics': dict(decision.metrics),
            'l2_gate': dict(l2_gate or {}),
            'futures_context': dict(derivatives or {}),
        })
        if not decision.allowed or decision.side not in {'long', 'short'}:
            return _finish(None, f'Crowding waiting: {decision.reason}')
        side = decision.side
        if not self.is_trade_direction_allowed(side):
            return _finish(None, self.format_trade_direction_block_reason(side), 'REJECTED_DIRECTION_FILTER')
        daily_count, daily_pnl = self.db.get_daily_stats()
        daily_entries = self.get_automatic_daily_entry_count()
        status['daily_pnl'] = daily_pnl
        status['daily_entries'] = daily_entries
        if float(cfg.get('daily_max_loss_usdt', 0) or 0) > 0 and float(daily_pnl or 0) <= -float(cfg['daily_max_loss_usdt']):
            return _finish(
                None,
                f'risk_limit_blocked: daily pnl {daily_pnl:.2f}',
                'REJECTED_DAILY_LOSS_LIMIT',
            )
        daily_trade_limit = int(
            await self.get_effective_automatic_daily_trade_limit_for_entry()
            if hasattr(self, 'get_effective_automatic_daily_trade_limit_for_entry')
            else cfg.get('max_daily_trades', 0) or 0
        )
        if daily_trade_limit > 0 and daily_entries >= daily_trade_limit:
            return _finish(
                None,
                f'risk_limit_blocked: daily trade count {daily_entries}',
                'REJECTED_DAILY_TRADE_LIMIT',
            )
        latest = rows[-1]
        reference_price = _safe_float_or_none(latest.get('close'))
        atr_value = _safe_float_or_none(decision.metrics.get('atr'))
        if reference_price is None or reference_price <= 0 or atr_value is None or atr_value <= 0:
            return _finish(None, 'Crowding entry price/ATR unavailable', 'REJECTED_CROWDING_DATA')
        try:
            ticker = await asyncio.to_thread(self.market_data_exchange.fetch_ticker, canonical)
            entry_price = _safe_float_or_none((ticker or {}).get('last') or (ticker or {}).get('close'))
        except Exception as exc:
            return _finish(None, f'Crowding live price unavailable: {exc}', 'REJECTED_CROWDING_DATA')
        if entry_price is None or entry_price <= 0:
            return _finish(None, 'Crowding live price unavailable', 'REJECTED_CROWDING_DATA')
        chase_atr = (
            (entry_price - reference_price) / atr_value
            if side == 'long'
            else (reference_price - entry_price) / atr_value
        )
        status['entry_chase_atr'] = chase_atr
        if chase_atr > float(crowd_cfg.get('entry_chase_max_atr', 0.50) or 0.50):
            return _finish(
                None,
                f'Crowding entry moved {chase_atr:.2f} ATR beyond completed-candle signal',
                'REJECTED_CROWDING_STALE_CHASE',
            )
        structure_level = _safe_float_or_none(decision.metrics.get('structure_level'))
        if structure_level is not None and (
            (side == 'long' and entry_price <= structure_level)
            or (side == 'short' and entry_price >= structure_level)
        ):
            return _finish(None, 'Crowding structure invalidated before order', 'REJECTED_CROWDING_INVALIDATED')
        filter_values = {
            'entry_price': entry_price,
            'entry_timeframe': '15m',
            'atr': atr_value,
            'atr_pct': atr_value / entry_price * 100.0,
        }
        market_quality = self._evaluate_utbreakout_market_quality(side, cfg, filter_values)
        status['market_quality'] = market_quality
        if market_quality.get('hard_block') or market_quality.get('state') is False:
            return _finish(None, f"market_quality_rejected: {market_quality.get('summary')}", 'REJECTED_MARKET_QUALITY')
        total_balance, free_balance, _ = await self.get_balance_info()
        balance_for_risk = total_balance if total_balance > 0 else free_balance
        common_cfg = self.get_runtime_common_settings()
        leverage = int(max(1.0, float(common_cfg.get('leverage', 5) or 5)))
        risk_multiplier = min(
            1.0,
            max(0.0, float(decision.risk_multiplier or 0.0)),
            max(0.0, float(market_quality.get('risk_multiplier', 1.0) or 1.0)),
            max(0.0, float(l2_gate.get('risk_multiplier', 0.0) or 0.0)),
        )
        risk_budget = resolve_utbreakout_risk_budget(
            balance_for_risk,
            cfg,
            multiplier=risk_multiplier,
            daily_pnl_usdt=daily_pnl,
        )
        try:
            plan = calculate_risk_plan(
                side=side,
                entry_price=entry_price,
                atr_value=atr_value,
                stop_atr_multiplier=float(crowd_cfg.get('stop_atr_multiplier', 1.35) or 1.35),
                ut_stop=None,
                structure_stop=None,
                structure_buffer_atr=0.0,
                take_profit_r_multiple=float(crowd_cfg.get('take_profit_r_multiple', 3.00) or 3.00),
                take_profit_front_run_atr=0.0,
                take_profit_front_run_pct=0.0,
                min_risk_reward=min(2.0, float(crowd_cfg.get('take_profit_r_multiple', 3.00) or 3.00)),
                balance_usdt=balance_for_risk,
                risk_per_trade_percent=risk_budget['risk_per_trade_percent'],
                max_risk_per_trade_usdt=risk_budget['max_risk_per_trade_usdt'],
                leverage=leverage,
            )
            plan = cap_utbreakout_risk_plan_to_margin(
                plan,
                free_balance=free_balance,
                leverage=leverage,
                entry_price=entry_price,
            )
        except ValueError as exc:
            return _finish(None, f'Crowding risk plan rejected: {exc}', 'REJECTED_CROWDING_RISK_PLAN')
        plan.update({
            'strategy': CROWDING_UNWIND_STRATEGY,
            'plan_symbol': canonical,
            'entry_timeframe': '15m',
            'exit_timeframe': cfg.get('exit_timeframe', '15m'),
            'htf_timeframe': cfg.get('htf_timeframe', '1h'),
            'entry_execution': 'market',
            'crowding_score': float(decision.score),
            'crowding_risk_multiplier': risk_multiplier,
            'crowding_metrics': dict(decision.metrics),
            'entry_chase_atr': chase_atr,
            'l2_gate': dict(l2_gate or {}),
            'l2_state': l2_gate.get('state'),
            'l2_risk_multiplier': l2_gate.get('risk_multiplier'),
            'market_quality_summary': market_quality.get('summary'),
            'atr': atr_value,
            'partial_take_profit_enabled': True,
            'partial_take_profit_r_multiple': 1.25,
            'partial_take_profit_ratio': 0.20,
            'second_take_profit_enabled': True,
            'second_take_profit_r_multiple': float(crowd_cfg.get('take_profit_r_multiple', 3.00) or 3.00),
            'second_take_profit_ratio': 0.35,
            'runner_pct': 0.45,
            'preserve_runner_qty': True,
            'atr_trailing_enabled': True,
            'atr_trailing_activation_r': 1.50,
            'atr_trailing_multiplier': 2.75,
            'runner_exit_enabled': True,
            'runner_chandelier_enabled': True,
            'ev_time_stop_enabled': True,
            'ev_time_stop_bars': int(crowd_cfg.get('time_stop_bars', 32) or 32),
            'ev_time_stop_min_mfe_r': 0.35,
        })
        self._set_utbot_filtered_breakout_entry_plan(canonical, plan)
        status['entry_plan'] = dict(plan)
        return _finish(side, f'ACCEPTED_ENTRY: {decision.reason}')

    async def build_crowding_unwind_status_text(self, symbol=None):
        target = self._canonical_futures_symbol(
            symbol or self.current_utbreakout_candidate_symbol or 'BTC/USDT'
        )
        status = dict((getattr(self, 'crowding_unwind_last_status', {}) or {}).get(target) or {})
        if not status:
            return '\n'.join([
                '🧨 Funding-OI Crowding Unwind 상태',
                f'Symbol: {target}',
                '아직 전략 평가 기록이 없습니다.',
            ])
        metrics = status.get('metrics') if isinstance(status.get('metrics'), dict) else {}
        l2 = status.get('l2_gate') if isinstance(status.get('l2_gate'), dict) else {}

        def _fmt(value, digits=2, signed=False, suffix=''):
            try:
                number = float(value)
            except (TypeError, ValueError):
                return 'N/A'
            sign = '+' if signed else ''
            return f"{number:{sign}.{digits}f}{suffix}"

        missing = list(metrics.get('missing_derivatives_fields') or [])
        lines = [
            '🧨 Funding-OI Crowding Unwind 상태',
            f'Symbol: {target}',
            f"Signal: {str(status.get('side') or 'NONE').upper()} / allowed={bool(status.get('allowed'))}",
            f"Score: {float(status.get('score', 0.0) or 0.0):.1f} / risk x{float(status.get('risk_multiplier', 0.0) or 0.0):.2f}",
            f"Funding: {_fmt(metrics.get('funding_rate'), 6, True)} / percentile {_fmt(metrics.get('funding_percentile'), 1)}",
            f"OI: z {_fmt(metrics.get('oi_z'), 2, True)} / 4h {_fmt(metrics.get('oi_change_4h_pct'), 2, True, '%')}",
            f"Long/Short ratio: {_fmt(metrics.get('long_short_ratio'), 2)}",
            f"Derivatives data: {'READY' if metrics.get('derivatives_data_ready') else 'MISSING'}",
            f"Confirmations: {int(metrics.get('confirmations', 0) or 0)} / L2 {str(l2.get('state') or 'unknown').upper()}",
            f"Reason: {status.get('reason') or '-'}",
        ]
        if missing:
            lines.append(f"Missing fields: {', '.join(str(value) for value in missing)}")
        return '\n'.join(lines)

    def _liquidation_exhaustion_reversal_runtime_config(self, cfg=None):
        source = dict(cfg or {})
        base = default_liquidation_exhaustion_reversal_config()
        nested = source.get('liquidation_exhaustion_reversal')
        if isinstance(nested, dict):
            base.update(nested)
        if 'liquidation_exhaustion_reversal_live_enabled' in source:
            base['live_enabled'] = bool(source.get('liquidation_exhaustion_reversal_live_enabled'))
        return base

    async def _calculate_liquidation_exhaustion_reversal_signal(
        self,
        symbol,
        df,
        strategy_params,
        *,
        force_reprocess=False,
    ):
        cfg = self._get_utbot_filtered_breakout_config(strategy_params)
        lxr_cfg = self._liquidation_exhaustion_reversal_runtime_config(cfg)
        canonical = self._canonical_futures_symbol(symbol)
        self._clear_utbot_filtered_breakout_entry_plan(canonical)
        status = {
            'strategy': STRATEGY_DISPLAY_NAMES.get(LXR_STRATEGY, 'LXR'),
            'entry_strategy': LXR_STRATEGY,
            'symbol': canonical,
            'stage': 'waiting',
        }

        def _finish(sig, reason, code=None):
            status['reason'] = reason
            status['accepted_side'] = sig
            if code:
                status['reject_code'] = code
            if sig:
                status['accepted_code'] = 'ACCEPTED_ENTRY'
                status['stage'] = 'entry_ready'
            if not isinstance(getattr(self, 'liquidation_exhaustion_reversal_last_status', None), dict):
                self.liquidation_exhaustion_reversal_last_status = {}
            self.liquidation_exhaustion_reversal_last_status[canonical] = dict(status)
            self._store_utbot_filtered_breakout_status(canonical, status)
            self.last_entry_reason[canonical] = reason
            return sig, reason, status

        if self.is_upbit_mode():
            return _finish(None, 'LXR unsupported in Upbit mode', 'REJECTED_UNSUPPORTED_MODE')
        if not bool(lxr_cfg.get('enabled', True)) or not bool(lxr_cfg.get('live_enabled', False)):
            return _finish(None, 'LXR live disabled', 'REJECTED_LXR_LIVE_DISABLED')

        try:
            ohlcv = await asyncio.to_thread(
                self.market_data_exchange.fetch_ohlcv,
                canonical,
                str(lxr_cfg.get('timeframe', '15m') or '15m'),
                limit=220,
            )
            rows = self._relative_strength_pullback_rows_from_ohlcv(ohlcv)
            rows = completed_candle_rows(
                rows,
                str(lxr_cfg.get('timeframe', '15m') or '15m'),
                {'exclude_incomplete_live_candle': True},
                now_ms=int(time.time() * 1000.0),
            )
        except Exception as exc:
            return _finish(None, f'LXR OHLCV unavailable: {exc}', 'REJECTED_LXR_DATA')

        derivatives = await self._fetch_utbreakout_futures_context(canonical)
        base_l2 = await self._evaluate_shared_l2_gate(
            canonical,
            cfg,
            force_refresh=force_reprocess,
        )
        preliminary = evaluate_liquidation_exhaustion_reversal(rows, derivatives, base_l2, lxr_cfg)
        candidate_side = preliminary.side
        l2_gate = await self._evaluate_shared_l2_gate(
            canonical,
            cfg,
            force_refresh=True,
            side=candidate_side,
        ) if candidate_side in {'long', 'short'} else base_l2
        decision = evaluate_liquidation_exhaustion_reversal(rows, derivatives, l2_gate, lxr_cfg)
        status.update({
            'allowed': bool(decision.allowed),
            'side': decision.side,
            'score': float(decision.score),
            'risk_multiplier': float(decision.risk_multiplier),
            'metrics': dict(decision.metrics),
            'l2_gate': dict(l2_gate or {}),
            'futures_context': dict(derivatives or {}),
        })
        if not decision.allowed or decision.side not in {'long', 'short'}:
            return _finish(None, decision.reason)
        side = decision.side
        if not self.is_trade_direction_allowed(side):
            return _finish(None, self.format_trade_direction_block_reason(side), 'REJECTED_DIRECTION_FILTER')

        daily_count, daily_pnl = self.db.get_daily_stats()
        daily_entries = self.get_automatic_daily_entry_count()
        status['daily_pnl'] = daily_pnl
        status['daily_entries'] = daily_entries
        if float(cfg.get('daily_max_loss_usdt', 0) or 0) > 0 and float(daily_pnl or 0) <= -float(cfg['daily_max_loss_usdt']):
            return _finish(None, f'risk_limit_blocked: daily pnl {daily_pnl:.2f}', 'REJECTED_DAILY_LOSS_LIMIT')
        daily_trade_limit = int(
            await self.get_effective_automatic_daily_trade_limit_for_entry()
            if hasattr(self, 'get_effective_automatic_daily_trade_limit_for_entry')
            else cfg.get('max_daily_trades', 0) or 0
        )
        if daily_trade_limit > 0 and daily_entries >= daily_trade_limit:
            return _finish(None, f'risk_limit_blocked: daily trade count {daily_entries}', 'REJECTED_DAILY_TRADE_LIMIT')

        latest_15m = rows[-1] if rows else {}
        reference_price = _safe_float_or_none(latest_15m.get('close'))
        metrics = dict(decision.metrics or {})
        atr_value = _safe_float_or_none(metrics.get('atr'))
        if reference_price is None or reference_price <= 0 or atr_value is None or atr_value <= 0:
            return _finish(None, 'LXR entry price/ATR unavailable', 'REJECTED_LXR_DATA')
        try:
            ticker = await asyncio.to_thread(self.market_data_exchange.fetch_ticker, canonical)
            entry_price = _safe_float_or_none((ticker or {}).get('last') or (ticker or {}).get('close'))
        except Exception as exc:
            return _finish(None, f'LXR live price unavailable: {exc}', 'REJECTED_LXR_DATA')
        if entry_price is None or entry_price <= 0:
            return _finish(None, 'LXR live price unavailable', 'REJECTED_LXR_DATA')
        chase_atr = (
            (entry_price - reference_price) / atr_value
            if side == 'long'
            else (reference_price - entry_price) / atr_value
        )
        status['entry_chase_atr'] = chase_atr
        if chase_atr > float(lxr_cfg.get('entry_chase_max_atr', 0.50) or 0.50):
            return _finish(
                None,
                f'LXR entry moved {chase_atr:.2f} ATR beyond completed-candle signal',
                'REJECTED_LXR_STALE_CHASE',
            )
        structure_stop = _safe_float_or_none(metrics.get('structure_stop'))
        if structure_stop is not None and (
            (side == 'long' and entry_price <= structure_stop)
            or (side == 'short' and entry_price >= structure_stop)
        ):
            return _finish(None, 'LXR structure invalidated before order', 'REJECTED_LXR_INVALIDATED')

        filter_values = {
            'entry_price': entry_price,
            'entry_timeframe': '15m',
            'atr': atr_value,
            'atr_pct': atr_value / entry_price * 100.0,
        }
        market_quality = self._evaluate_utbreakout_market_quality(side, cfg, filter_values)
        status['market_quality'] = market_quality
        if market_quality.get('hard_block') or market_quality.get('state') is False:
            return _finish(None, f"market_quality_rejected: {market_quality.get('summary')}", 'REJECTED_MARKET_QUALITY')
        status['l2_gate'] = dict(l2_gate or {})
        if not l2_gate.get('allowed', False):
            return _finish(None, f"L2 stressed: {l2_gate.get('reason')}", 'REJECTED_L2_STRESSED')

        total_balance, free_balance, _ = await self.get_balance_info()
        balance_for_risk = total_balance if total_balance > 0 else free_balance
        common_cfg = self.get_runtime_common_settings()
        leverage = int(max(1.0, float(common_cfg.get('leverage', 5) or 5)))
        risk_multiplier = min(
            1.0,
            max(0.0, float(decision.risk_multiplier or 0.0)),
            max(0.0, float(market_quality.get('risk_multiplier', 1.0) or 1.0)),
            max(0.0, float(l2_gate.get('risk_multiplier', 0.0) or 0.0)),
        )
        risk_budget = resolve_utbreakout_risk_budget(
            balance_for_risk,
            cfg,
            multiplier=risk_multiplier,
            daily_pnl_usdt=daily_pnl,
        )
        try:
            plan = calculate_risk_plan(
                side=side,
                entry_price=entry_price,
                atr_value=atr_value,
                stop_atr_multiplier=float(lxr_cfg.get('stop_atr_multiplier', 1.10) or 1.10),
                ut_stop=None,
                structure_stop=_safe_float_or_none(metrics.get('structure_stop')),
                structure_buffer_atr=float(lxr_cfg.get('structure_buffer_atr', 0.15) or 0.15),
                take_profit_r_multiple=float(lxr_cfg.get('take_profit_r_multiple', 3.20) or 3.20),
                take_profit_front_run_atr=0.0,
                take_profit_front_run_pct=0.0,
                min_risk_reward=min(2.0, float(lxr_cfg.get('take_profit_r_multiple', 3.20) or 3.20)),
                balance_usdt=balance_for_risk,
                risk_per_trade_percent=risk_budget['risk_per_trade_percent'],
                max_risk_per_trade_usdt=risk_budget['max_risk_per_trade_usdt'],
                leverage=leverage,
            )
            plan = cap_utbreakout_risk_plan_to_margin(
                plan,
                free_balance=free_balance,
                leverage=leverage,
                entry_price=entry_price,
            )
        except ValueError as exc:
            return _finish(None, f'LXR risk plan rejected: {exc}', 'REJECTED_LXR_RISK_PLAN')

        plan.update({
            'strategy': LXR_STRATEGY,
            'plan_symbol': canonical,
            'signal_candle_ts': latest_15m.get('timestamp'),
            'entry_timeframe': '15m',
            'timeframe': '15m',
            'exit_timeframe': '15m',
            'htf_timeframe': '1h',
            'entry_execution': 'market',
            'lxr_score': float(decision.score),
            'lxr_risk_multiplier': risk_multiplier,
            'lxr_metrics': metrics,
            'entry_chase_atr': chase_atr,
            'l2_gate': dict(l2_gate or {}),
            'l2_state': l2_gate.get('state'),
            'l2_risk_multiplier': l2_gate.get('risk_multiplier'),
            'market_quality_summary': market_quality.get('summary'),
            'atr': atr_value,
            'atr_pct': atr_value / entry_price * 100.0,
            'partial_take_profit_enabled': True,
            'partial_take_profit_r_multiple': 1.0,
            'partial_take_profit_ratio': 0.20,
            'second_take_profit_enabled': True,
            'second_take_profit_r_multiple': float(lxr_cfg.get('take_profit_r_multiple', 3.20) or 3.20),
            'second_take_profit_ratio': 0.35,
            'runner_pct': 0.45,
            'preserve_runner_qty': True,
            'atr_trailing_enabled': True,
            'atr_trailing_activation_r': 1.40,
            'atr_trailing_multiplier': 2.50,
            'runner_exit_enabled': True,
            'runner_chandelier_enabled': True,
            'tp1_breakeven_enabled': True,
            'tp1_breakeven_wait_for_partial': True,
            'ev_time_stop_enabled': True,
            'ev_time_stop_bars': int(lxr_cfg.get('time_stop_bars', 12) or 12),
            'ev_time_stop_min_mfe_r': 0.30,
        })
        self._set_utbot_filtered_breakout_entry_plan(canonical, plan)
        status['entry_plan'] = dict(plan)
        return _finish(side, f'ACCEPTED_ENTRY: {decision.reason}')

    async def build_liquidation_exhaustion_reversal_status_text(self, symbol=None):
        target = self._canonical_futures_symbol(
            symbol or self.current_utbreakout_candidate_symbol or 'BTC/USDT'
        )
        status = dict((getattr(self, 'liquidation_exhaustion_reversal_last_status', {}) or {}).get(target) or {})
        if not status:
            return '\n'.join([
                'LXR liquidation-exhaustion reversal status',
                f'Symbol: {target}',
                'No completed LXR evaluation is available yet.',
            ])
        metrics = status.get('metrics') if isinstance(status.get('metrics'), dict) else {}
        l2 = status.get('l2_gate') if isinstance(status.get('l2_gate'), dict) else {}
        rows = [
            'LXR liquidation-exhaustion reversal status',
            f'Symbol: {target}',
            f"Signal: {str(status.get('side') or 'NONE').upper()} / allowed={bool(status.get('allowed'))}",
            f"Score: {float(status.get('score', 0.0) or 0.0):.1f} / risk x{float(status.get('risk_multiplier', 0.0) or 0.0):.2f}",
            f"Shock: {str(metrics.get('direction') or 'NONE').upper()} / {float(metrics.get('shock_atr', 0.0) or 0.0):.2f} ATR / volume x{float(metrics.get('shock_volume_ratio', 0.0) or 0.0):.2f}",
            f"OI 1h: {float(metrics.get('open_interest_change_1h', 0.0) or 0.0):+.2f}% / z {float(metrics.get('open_interest_delta_z', 0.0) or 0.0):+.2f}",
            f"Reclaim: {float(metrics.get('reclaim_atr', 0.0) or 0.0):.2f} ATR / structure={bool(metrics.get('structure_reclaimed'))}",
            f"L2: {str(l2.get('state') or 'unknown').upper()} / {l2.get('reason') or '-'}",
            f"Reason: {status.get('reason') or '-'}",
        ]
        return '\n'.join(rows)

    def _dual_alpha_strategy_params(self, strategy_params, branch):
        params = copy.deepcopy(strategy_params if isinstance(strategy_params, dict) else {})
        cfg = dict(params.get('UTBotFilteredBreakoutV1') or {})
        branch = str(branch or '').lower()
        if branch == ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND:
            rsp_cfg = default_relative_strength_pullback_config()
            nested = cfg.get('relative_strength_pullback_trend')
            if isinstance(nested, dict):
                rsp_cfg.update(nested)
            rsp_cfg['entry_strategy'] = ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND
            rsp_cfg['relative_strength_pullback_trend_shadow_enabled'] = True
            rsp_cfg['relative_strength_pullback_trend_live_enabled'] = True
            rsp_cfg['relative_strength_pullback_trend_paper_enabled'] = False
            rsp_cfg['strategy_version'] = 'v2'
            rsp_cfg['rspt_v2_enabled'] = True
            rsp_cfg['independent_direction_enabled'] = True
            rsp_cfg['direction_source'] = 'RSPT-v3 BTC/ETH/alt/vol residual strength'
            rsp_cfg['forced_direction'] = None
            rsp_cfg['allow_breakout_continuation'] = False
            rsp_cfg['require_prior_impulse'] = True
            cfg['entry_strategy'] = ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND
            cfg['relative_strength_pullback_trend'] = rsp_cfg
            cfg['relative_strength_pullback_trend_shadow_enabled'] = True
            cfg['relative_strength_pullback_trend_live_enabled'] = True
            cfg['relative_strength_pullback_trend_paper_enabled'] = False
            cfg['adaptive_timeframe_enabled'] = False
            params['active_strategy'] = ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND
        else:
            cfg['entry_strategy'] = ENTRY_STRATEGY_UT_BREAKOUT
            cfg['relative_strength_pullback_trend_live_enabled'] = False
            cfg['relative_strength_pullback_trend_paper_enabled'] = False
            cfg['dual_alpha_direction_filter_enabled'] = False
            cfg['adaptive_timeframe_enabled'] = True
            params['active_strategy'] = UTBOT_ADAPTIVE_TIMEFRAME_STRATEGY
        params['UTBotFilteredBreakoutV1'] = cfg
        return params

    def _dual_alpha_direction_strategy_params(self, strategy_params):
        params = copy.deepcopy(strategy_params if isinstance(strategy_params, dict) else {})
        cfg = dict(params.get('UTBotFilteredBreakoutV1') or {})
        cfg['entry_strategy'] = ENTRY_STRATEGY_UT_BREAKOUT
        cfg['relative_strength_pullback_trend_live_enabled'] = False
        cfg['relative_strength_pullback_trend_paper_enabled'] = False
        cfg['dual_alpha_direction_filter_enabled'] = True
        cfg['dual_alpha_direction_filter_timeframe'] = '4h'
        cfg['dual_alpha_direction_filter_htf'] = '1d'
        cfg['entry_timeframe'] = '4h'
        cfg['exit_timeframe'] = '4h'
        cfg['htf_timeframe'] = '1d'
        cfg['adaptive_timeframe_enabled'] = False
        params['active_strategy'] = ENTRY_STRATEGY_UT_BREAKOUT
        params['UTBotFilteredBreakoutV1'] = cfg
        return params

    def _calculate_shared_ut_direction_from_frames(
        self,
        direction_rows,
        htf_rows,
        strategy_params,
        *,
        consumer='UNKNOWN',
    ):
        direction_params = self._dual_alpha_direction_strategy_params(strategy_params)
        direction_cfg = self._get_utbot_filtered_breakout_config(direction_params)
        direction_cfg.update({
            'dual_alpha_direction_filter_enabled': True,
            'dual_alpha_direction_filter_timeframe': '4h',
            'dual_alpha_direction_filter_htf': '1d',
            'entry_timeframe': '4h',
            'exit_timeframe': '4h',
            'htf_timeframe': '1d',
            'adaptive_timeframe_enabled': False,
        })

        def _frame(rows):
            if isinstance(rows, pd.DataFrame):
                frame = rows.copy()
            else:
                frame = pd.DataFrame(list(rows or []))
            required = ['timestamp', 'open', 'high', 'low', 'close', 'volume']
            for column in required:
                if column not in frame.columns:
                    frame[column] = np.nan
            frame = frame[required].copy()
            for column in ['timestamp', 'open', 'high', 'low', 'close', 'volume']:
                frame[column] = pd.to_numeric(frame[column], errors='coerce')
            return frame.dropna(subset=['timestamp', 'open', 'high', 'low', 'close']).reset_index(drop=True)

        direction_df = _frame(direction_rows)
        htf_df = _frame(htf_rows)
        ut_params = self._get_utbot_filtered_breakout_ut_params(direction_cfg)
        signal_4h, reason_4h, detail_4h = self._calculate_utbot_signal(direction_df, ut_params)
        signal_1d, reason_1d, detail_1d = self._calculate_utbot_signal(htf_df, ut_params)
        detail_4h = dict(detail_4h or {})
        detail_1d = dict(detail_1d or {})
        side_4h = self._normalize_relative_strength_pullback_direction(
            signal_4h or detail_4h.get('bias_side')
        )
        side_1d = self._normalize_relative_strength_pullback_direction(
            signal_1d or detail_1d.get('bias_side')
        )

        if side_4h is None:
            side = None
            reason_code = 'UT_DIRECTION_4H_UNAVAILABLE'
            reason = f'UT 4h 방향 계산 대기: {reason_4h}'
        elif side_1d is None:
            side = None
            reason_code = 'UT_DIRECTION_1D_UNAVAILABLE'
            reason = f'UT 1d 방향 계산 대기: {reason_1d}'
        elif side_4h != side_1d:
            side = None
            reason_code = 'UT_DIRECTION_CONFLICT'
            reason = f'UT 방향 불일치: 4h {side_4h.upper()} / 1d {side_1d.upper()}'
        else:
            side = side_4h
            reason_code = 'UT_DIRECTION_READY'
            reason = f'UT 방향 일치: 4h/1d {side.upper()}'

        status = {
            'entry_timeframe': '4h',
            'exit_timeframe': '4h',
            'htf_timeframe': '1d',
            'dual_direction_filter_timeframe': '4h',
            'dual_direction_filter_htf': '1d',
            'ut_direction_consumer': str(consumer or 'UNKNOWN'),
            'ut_direction_authority': 'UTBreakout',
            'direction_reason_code': reason_code,
            'candidate_side': side,
            'candidate_signal': side,
            'accepted_side': side,
            'ut_bias_side': side_4h,
            'ut_4h_side': side_4h,
            'ut_1d_side': side_1d,
            'ut_4h_fresh_signal': self._normalize_relative_strength_pullback_direction(signal_4h),
            'ut_1d_fresh_signal': self._normalize_relative_strength_pullback_direction(signal_1d),
            'ut_4h_reason': reason_4h,
            'ut_1d_reason': reason_1d,
            'ut_4h_detail': detail_4h,
            'ut_1d_detail': detail_1d,
            'stage': 'direction_ready' if side else 'direction_wait',
            'reason': reason,
        }
        return side, reason, status

    async def _shared_ut_direction_filter(
        self,
        symbol,
        strategy_params,
        *,
        force_reprocess=False,
        consumer='UNKNOWN',
    ):
        try:
            direction_ohlcv, htf_ohlcv = await asyncio.gather(
                asyncio.to_thread(
                    self.market_data_exchange.fetch_ohlcv,
                    symbol,
                    '4h',
                    limit=300,
                ),
                asyncio.to_thread(
                    self.market_data_exchange.fetch_ohlcv,
                    symbol,
                    '1d',
                    limit=300,
                ),
            )
        except Exception as exc:
            reason = f'UT direction fetch failed (4h/1d): {exc}'
            status = {
                'entry_timeframe': '4h',
                'exit_timeframe': '4h',
                'htf_timeframe': '1d',
                'direction_reason_code': 'UT_DIRECTION_FETCH_ERROR',
                'ut_direction_consumer': consumer,
                'ut_direction_authority': 'UTBreakout',
                'reason': reason,
            }
            self._utbreakout_trace_event(
                symbol,
                'UT_DIRECTION',
                'FILTER_ERROR',
                entry_timeframe='4h',
                htf_timeframe='1d',
                reason=str(exc),
                consumer=consumer,
            )
            return None, reason, status

        side, reason, status = self._calculate_shared_ut_direction_from_frames(
            direction_ohlcv,
            htf_ohlcv,
            strategy_params,
            consumer=consumer,
        )
        status = dict(status or {})
        status['force_reprocess'] = bool(force_reprocess)
        self._clear_utbot_filtered_breakout_entry_plan(symbol)
        self._utbreakout_trace_event(
            symbol,
            'UT_DIRECTION',
            'FILTER_RESULT',
            side=side,
            entry_timeframe='4h',
            htf_timeframe='1d',
            reason=reason,
            reason_code=status.get('direction_reason_code'),
            consumer=consumer,
        )
        return side, reason, status

    async def _dual_alpha_ut_direction_filter(self, symbol, strategy_params, *, force_reprocess=False):
        # Backward-compatible wrapper. Direction calculation remains centralized
        # in _shared_ut_direction_filter.
        return await self._shared_ut_direction_filter(
            symbol,
            strategy_params,
            force_reprocess=force_reprocess,
            consumer='DUAL_ALPHA',
        )

    def _dual_alpha_light(self, status, label):
        status = status if isinstance(status, dict) else {}
        if status.get('disabled') or str(status.get('stage') or '').lower() == 'disabled':
            return {
                'label': label,
                'light': 'off',
                'state': 'OFF',
                'side': None,
                'reason': str(status.get('reason') or 'disabled by Telegram strategy selection'),
                'setup_type': None,
                'direction_by': None,
                'forced_direction': None,
                'disabled': True,
            }
        side = str(status.get('accepted_side') or status.get('candidate_side') or status.get('candidate_signal') or '').lower()
        reason = str(status.get('reason') or status.get('reject_code') or 'no recent decision')
        stage = str(status.get('stage') or '').lower()
        accepted = side in {'long', 'short'} and status.get('accepted_code') == 'ACCEPTED_ENTRY'
        if accepted:
            light = 'green'
            state = 'READY'
        elif status.get('reject_code'):
            light = 'red'
            state = 'BLOCKED'
        elif stage in {'waiting', 'evaluate'} or reason:
            light = 'yellow'
            state = 'WAIT'
        else:
            light = 'gray'
            state = 'NONE'
        logs = status.get('rspt_decision', {}).get('logs') if isinstance(status.get('rspt_decision'), dict) else {}
        if not isinstance(logs, dict):
            logs = {}
        setup_type = status.get('setup_type') or logs.get('setup_type') or status.get('candidate_type')
        return {
            'label': label,
            'light': light,
            'state': state,
            'side': side or None,
            'reason': reason,
            'setup_type': setup_type,
            'direction_by': status.get('rspt_direction_source') or status.get('direction_by'),
            'forced_direction': status.get('rspt_forced_direction'),
        }

    def _dual_alpha_score(self, strategy_key, side, status, plan):
        status = status if isinstance(status, dict) else {}
        plan = plan if isinstance(plan, dict) else {}
        score = None
        for source in (status, plan, status.get('auto_scores') if isinstance(status.get('auto_scores'), dict) else {}):
            if not isinstance(source, dict):
                continue
            for key in (
                'profit_alpha_score',
                'entry_edge_score',
                'feature_score',
                'feature_score_value',
                'auto_selected_score',
                'score',
            ):
                value = _safe_float_or_none(source.get(key))
                if value is not None:
                    score = max(score if score is not None else value, value)
        if score is None:
            score = 72.0
        if strategy_key == ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND:
            size_mult = _safe_float_or_none(
                status.get('rspt_size_multiplier')
                or plan.get('rspt_size_multiplier')
                or plan.get('rspt_risk_multiplier')
            )
            if size_mult is not None:
                score += max(0.0, min(1.0, size_mult)) * 8.0
            setup_type = str(
                status.get('setup_type')
                or plan.get('rspt_setup_type')
                or ''
            )
            if setup_type == 'breakout_continuation':
                score += 3.0
        else:
            score += 2.0
        if side not in {'long', 'short'}:
            score -= 100.0
        return float(score)

    def _dual_alpha_scale_plan(self, plan, multiplier):
        scaled = dict(plan or {})
        multiplier = max(0.0, min(1.0, float(multiplier or 0.0)))
        for key in (
            'qty',
            'risk_usdt',
            'max_risk_per_trade_usdt',
            'planned_notional',
            'planned_margin',
            'expected_profit_usdt',
            'position_notional',
            'position_cap_original_notional',
            'position_cap_original_risk_usdt',
            'position_cap_max_notional',
        ):
            value = _safe_float_or_none(scaled.get(key))
            if value is not None:
                scaled[key] = value * multiplier
        percent = _safe_float_or_none(scaled.get('risk_per_trade_percent'))
        if percent is not None:
            scaled['risk_per_trade_percent'] = percent * multiplier
        scaled['dual_alpha_risk_multiplier'] = multiplier
        return scaled

    async def _calculate_dual_alpha_signal(
        self,
        symbol,
        df,
        strategy_params,
        *,
        force_reprocess=False,
    ):
        self._clear_utbot_filtered_breakout_entry_plan(symbol)
        base_symbol = symbol
        base_cfg = self._get_utbot_filtered_breakout_config(strategy_params)
        single_signal_multiplier = max(
            0.0,
            min(1.0, float(base_cfg.get('dual_alpha_single_signal_risk_multiplier', 0.60) or 0.60)),
        )

        direction_side, direction_reason, direction_status = await self._shared_ut_direction_filter(
            base_symbol,
            strategy_params,
            force_reprocess=force_reprocess,
            consumer='DUAL_ALPHA',
        )
        ut_params = self._dual_alpha_strategy_params(strategy_params, ENTRY_STRATEGY_UT_BREAKOUT)
        ut_sig, ut_reason, ut_status = await self._calculate_utbot_filtered_breakout_signal(
            base_symbol,
            df,
            ut_params,
            force_reprocess=force_reprocess,
        )
        ut_status = dict(ut_status or self._utbreakout_diag_for_symbol(base_symbol) or {})
        ut_plan = (
            dict(self._get_utbot_filtered_breakout_entry_plan(base_symbol, ut_sig) or {})
            if ut_sig in {'long', 'short'}
            else None
        )

        rsp_params = self._dual_alpha_strategy_params(
            strategy_params,
            ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND,
        )
        rsp_sig, rsp_reason, rsp_status = await self._calculate_relative_strength_pullback_signal(
            base_symbol,
            df,
            rsp_params,
            force_reprocess=force_reprocess,
            forced_direction=None,
            direction_source='RSPT-v3 BTC/ETH/alt/vol residual strength',
            resolve_ut_direction=False,
        )
        rsp_status = dict(rsp_status or self._utbreakout_diag_for_symbol(base_symbol) or {})
        rsp_symbol = rsp_status.get('plan_symbol') or rsp_status.get('symbol') or base_symbol
        rsp_plan = (
            dict(self._get_utbot_filtered_breakout_entry_plan(rsp_symbol, rsp_sig) or {})
            if rsp_sig in {'long', 'short'}
            else None
        )

        # Branch evaluations can each leave a plan behind.  DUAL owns the final
        # plan and therefore clears both before applying agreement rules.
        self._clear_utbot_filtered_breakout_entry_plan(base_symbol)
        if rsp_symbol != base_symbol:
            self._clear_utbot_filtered_breakout_entry_plan(rsp_symbol)

        valid_ut = None
        if ut_sig in {'long', 'short'} and isinstance(ut_plan, dict):
            if direction_side in {'long', 'short'} and ut_sig == direction_side:
                valid_ut = {
                    'key': ENTRY_STRATEGY_UT_BREAKOUT,
                    'label': 'UTBreakout',
                    'side': ut_sig,
                    'reason': ut_reason,
                    'status': ut_status,
                    'plan': ut_plan,
                    'score': self._dual_alpha_score(ENTRY_STRATEGY_UT_BREAKOUT, ut_sig, ut_status, ut_plan),
                    'priority': 0,
                }
            else:
                ut_status['dual_direction_filter_blocked'] = True
                ut_status['dual_direction_filter_side'] = direction_side
                ut_status['dual_direction_filter_reason'] = direction_reason
                self._utbreakout_trace_event(
                    base_symbol,
                    'DUAL_ALPHA',
                    'UT_OPPOSITE_DIRECTION_BLOCKED' if direction_side else 'UT_DIRECTION_FILTER_MISSING',
                    direction_filter_side=direction_side,
                    ut_direction=ut_sig,
                    reason=direction_reason,
                )

        valid_rsp = None
        if rsp_sig in {'long', 'short'} and isinstance(rsp_plan, dict):
            valid_rsp = {
                'key': ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND,
                'label': 'RSPT-v3',
                'side': rsp_sig,
                'reason': rsp_reason,
                'status': rsp_status,
                'plan': rsp_plan,
                'score': self._dual_alpha_score(
                    ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND,
                    rsp_sig,
                    rsp_status,
                    rsp_plan,
                ),
                'priority': 1,
            }

        selected = None
        agreement_state = 'none'
        agreement_multiplier = 0.0
        choices = [choice for choice in (valid_ut, valid_rsp) if choice]
        if valid_ut and valid_rsp:
            if valid_ut['side'] != valid_rsp['side']:
                agreement_state = 'conflict'
                self._utbreakout_trace_event(
                    base_symbol,
                    'DUAL_ALPHA',
                    'STRATEGY_DIRECTION_CONFLICT',
                    ut_direction=valid_ut['side'],
                    rspt_direction=valid_rsp['side'],
                )
            else:
                agreement_state = 'confirmed'
                agreement_multiplier = 1.0
                selected = sorted(
                    choices,
                    key=lambda item: (-float(item.get('score') or 0.0), int(item.get('priority') or 0)),
                )[0]
        elif choices:
            agreement_state = 'single'
            agreement_multiplier = single_signal_multiplier
            selected = choices[0]

        final_status = dict((selected or {}).get('status') or rsp_status or ut_status or {})
        if selected and isinstance(selected.get('plan'), dict):
            selected_plan = self._dual_alpha_scale_plan(selected['plan'], agreement_multiplier)
            selected_plan['dual_alpha_selected_strategy'] = selected['key']
            selected_plan['dual_alpha_score'] = selected['score']
            selected_plan['dual_alpha_agreement_state'] = agreement_state
            selected_plan['dual_alpha_confirmation_count'] = len(choices)
            selected_plan['dual_alpha_confirmation_strategies'] = [
                choice['key'] for choice in choices
            ]
            if selected['key'] == ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND:
                selected_plan['strategy'] = ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND
            else:
                selected_plan['strategy'] = (
                    selected_plan.get('strategy')
                    if str(selected_plan.get('strategy') or '').lower() in UTBREAKOUT_STRATEGIES
                    else UTBOT_ADAPTIVE_TIMEFRAME_STRATEGY
                )
            self._set_utbot_filtered_breakout_entry_plan(
                selected_plan.get('plan_symbol') or base_symbol,
                selected_plan,
            )

        dual_summary = {
            'enabled': True,
            'direction_filter': {
                'side': direction_side,
                'reason': direction_reason,
                'entry_timeframe': (direction_status or {}).get('entry_timeframe', '4h'),
                'htf_timeframe': (direction_status or {}).get('htf_timeframe', '1d'),
            },
            'utbreak': self._dual_alpha_light(ut_status, 'UTBreakout'),
            'rspt': self._dual_alpha_light(rsp_status, 'RSPT-v3'),
            'agreement_state': agreement_state,
            'agreement_risk_multiplier': agreement_multiplier,
            'confirmation_count': len(choices),
            'selected': selected.get('key') if selected else None,
            'selected_label': selected.get('label') if selected else None,
            'selected_side': selected.get('side') if selected else None,
            'selection_score': selected.get('score') if selected else None,
            'utbreak_score': valid_ut.get('score') if valid_ut else None,
            'rspt_score': valid_rsp.get('score') if valid_rsp else None,
        }
        final_status.update({
            'strategy': STRATEGY_DISPLAY_NAMES.get(DUAL_ALPHA_STRATEGY, 'DUAL_ALPHA'),
            'entry_strategy': DUAL_ALPHA_STRATEGY,
            'dual_alpha_enabled': True,
            'dual_selected_strategy': dual_summary['selected'],
            'dual_alpha': dual_summary,
        })
        if selected:
            final_status['accepted_code'] = 'ACCEPTED_ENTRY'
            final_status['accepted_side'] = selected['side']
            final_status['reason'] = (
                f"DUAL_ALPHA {agreement_state} selected {selected['label']} "
                f"{selected['side'].upper()} at {agreement_multiplier:.0%} risk: {selected['reason']}"
            )
            final_status['stage'] = 'entry_ready'
            self._utbreakout_trace_event(
                base_symbol,
                'DUAL_ALPHA',
                'SELECTED',
                selected=selected['key'],
                side=selected['side'],
                score=round(float(selected.get('score') or 0.0), 2),
                agreement_state=agreement_state,
                risk_multiplier=agreement_multiplier,
            )
        else:
            conflict_text = ' strategy conflict' if agreement_state == 'conflict' else ''
            final_status['reason'] = (
                f"DUAL_ALPHA waiting{conflict_text}: Direction={direction_reason}; "
                f"UT={ut_reason}; RSPT={rsp_reason}"
            )
            final_status['stage'] = 'waiting'
            if agreement_state == 'conflict':
                final_status['reject_code'] = 'REJECTED_DUAL_DIRECTION_CONFLICT'
            self._utbreakout_trace_event(
                base_symbol,
                'DUAL_ALPHA',
                'WAIT',
                agreement_state=agreement_state,
                ut_reason=ut_reason,
                rspt_reason=rsp_reason,
            )

        canonical = self._canonical_futures_symbol(final_status.get('plan_symbol') or base_symbol)
        if not isinstance(getattr(self, 'dual_alpha_last_status', None), dict):
            self.dual_alpha_last_status = {}
        self.dual_alpha_last_status[canonical] = dict(final_status)
        self._store_utbot_filtered_breakout_status(canonical, final_status)
        self.last_entry_reason[canonical] = final_status.get('reason')
        if selected:
            return selected['side'], final_status['reason'], final_status
        return None, final_status['reason'], final_status

    def _triple_alpha_strategy_params(self, strategy_params, branch):
        branch = str(branch or '').lower()
        if branch in {ENTRY_STRATEGY_UT_BREAKOUT, ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND}:
            params = self._dual_alpha_strategy_params(strategy_params, branch)
            cfg = dict(params.get('UTBotFilteredBreakoutV1') or {})
            qh_cfg = self._qh_flow_runtime_config(cfg)
            qh_cfg['qh_confirmation_enabled'] = False
            cfg['qh_flow'] = qh_cfg
            cfg['qh_flow_confirmation_enabled'] = False
            params['UTBotFilteredBreakoutV1'] = cfg
            return params
        params = copy.deepcopy(strategy_params if isinstance(strategy_params, dict) else {})
        cfg = dict(params.get('UTBotFilteredBreakoutV1') or {})
        vmt_cfg = self._volatility_managed_trend_runtime_config(cfg)
        vmt_cfg['enabled'] = True
        vmt_cfg['live_enabled'] = True
        cfg['volatility_managed_trend'] = vmt_cfg
        cfg['volatility_managed_trend_live_enabled'] = True
        cfg['qh_flow_live_enabled'] = False
        cfg['qh_flow_confirmation_enabled'] = False
        cfg['relative_strength_pullback_trend_live_enabled'] = False
        cfg['entry_strategy'] = ENTRY_STRATEGY_UT_BREAKOUT
        params['active_strategy'] = VOLATILITY_MANAGED_TREND_STRATEGY
        params['UTBotFilteredBreakoutV1'] = cfg
        return params

    async def _calculate_triple_alpha_signal(
        self,
        symbol,
        df,
        strategy_params,
        *,
        force_reprocess=False,
    ):
        base_symbol = self._canonical_futures_symbol(symbol)
        self._clear_utbot_filtered_breakout_entry_plan(base_symbol)
        base_cfg = self._get_utbot_filtered_breakout_config(strategy_params)
        qh_cfg = self._qh_flow_runtime_config(base_cfg)
        multipliers = {
            3: max(0.0, min(1.0, float(base_cfg.get('triple_alpha_three_signal_risk_multiplier', qh_cfg.get('triple_three_signal_multiplier', 1.0)) or 1.0))),
            2: max(0.0, min(1.0, float(base_cfg.get('triple_alpha_two_signal_risk_multiplier', qh_cfg.get('triple_two_signal_multiplier', 0.85)) or 0.85))),
            1: max(0.0, min(1.0, float(base_cfg.get('triple_alpha_single_signal_risk_multiplier', qh_cfg.get('triple_single_signal_multiplier', 0.55)) or 0.55))),
        }

        branch_results = []

        ut_params = self._triple_alpha_strategy_params(strategy_params, ENTRY_STRATEGY_UT_BREAKOUT)
        ut_sig, ut_reason, ut_status = await self._calculate_utbot_filtered_breakout_signal(
            base_symbol,
            df,
            ut_params,
            force_reprocess=force_reprocess,
        )
        ut_status = dict(ut_status or self._utbreakout_diag_for_symbol(base_symbol) or {})
        ut_plan = (
            dict(self._get_utbot_filtered_breakout_entry_plan(base_symbol, ut_sig) or {})
            if ut_sig in {'long', 'short'}
            else None
        )
        branch_results.append((ENTRY_STRATEGY_UT_BREAKOUT, 'UTBreakout', ut_sig, ut_reason, ut_status, ut_plan, 0))
        self._clear_utbot_filtered_breakout_entry_plan(base_symbol)

        rsp_params = self._triple_alpha_strategy_params(
            strategy_params,
            ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND,
        )
        rsp_sig, rsp_reason, rsp_status = await self._calculate_relative_strength_pullback_signal(
            base_symbol,
            df,
            rsp_params,
            force_reprocess=force_reprocess,
            forced_direction=None,
            direction_source='RSPT-v3 BTC/ETH/alt/vol residual strength',
            resolve_ut_direction=False,
        )
        rsp_status = dict(rsp_status or self._utbreakout_diag_for_symbol(base_symbol) or {})
        rsp_symbol = self._canonical_futures_symbol(
            rsp_status.get('plan_symbol') or rsp_status.get('symbol') or base_symbol
        )
        rsp_plan = (
            dict(self._get_utbot_filtered_breakout_entry_plan(rsp_symbol, rsp_sig) or {})
            if rsp_sig in {'long', 'short'}
            else None
        )
        branch_results.append((
            ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND,
            'RSPT-v3',
            rsp_sig,
            rsp_reason,
            rsp_status,
            rsp_plan,
            1,
        ))
        self._clear_utbot_filtered_breakout_entry_plan(rsp_symbol)

        vmt_params = self._triple_alpha_strategy_params(strategy_params, VOLATILITY_MANAGED_TREND_STRATEGY)
        vmt_sig, vmt_reason, vmt_status = await self._calculate_volatility_managed_trend_signal(
            base_symbol,
            df,
            vmt_params,
            force_reprocess=force_reprocess,
        )
        vmt_status = dict(vmt_status or {})
        vmt_symbol = self._canonical_futures_symbol(
            vmt_status.get('plan_symbol') or vmt_status.get('symbol') or base_symbol
        )
        vmt_plan = (
            dict(self._get_utbot_filtered_breakout_entry_plan(vmt_symbol, vmt_sig) or {})
            if vmt_sig in {'long', 'short'}
            else None
        )
        branch_results.append((VOLATILITY_MANAGED_TREND_STRATEGY, 'VMT Trend', vmt_sig, vmt_reason, vmt_status, vmt_plan, 2))
        self._clear_utbot_filtered_breakout_entry_plan(vmt_symbol)

        choices = []
        for key, label, side, reason, status, plan, priority in branch_results:
            if side not in {'long', 'short'} or not isinstance(plan, dict):
                continue
            choices.append({
                'key': key,
                'label': label,
                'side': side,
                'reason': reason,
                'status': status,
                'plan': plan,
                'score': self._dual_alpha_score(key, side, status, plan),
                'priority': priority,
            })

        unique_sides = {choice['side'] for choice in choices}
        selected = None
        agreement_state = 'none'
        agreement_multiplier = 0.0
        if len(unique_sides) > 1:
            agreement_state = 'conflict'
        elif choices:
            confirmation_count = len(choices)
            agreement_state = {1: 'single', 2: 'double', 3: 'triple'}.get(confirmation_count, 'confirmed')
            agreement_multiplier = multipliers.get(confirmation_count, multipliers[1])
            selected = sorted(
                choices,
                key=lambda item: (-float(item.get('score') or 0.0), int(item.get('priority') or 0)),
            )[0]

        statuses = {key: status for key, _, _, _, status, _, _ in branch_results}
        reasons = {key: reason for key, _, _, reason, _, _, _ in branch_results}
        final_status = dict((selected or {}).get('status') or vmt_status or rsp_status or ut_status or {})
        selected_plan_for_status = None
        if selected:
            selected_plan = self._dual_alpha_scale_plan(selected['plan'], agreement_multiplier)
            selected_plan.pop('dual_alpha_risk_multiplier', None)
            selected_plan.update({
                'strategy': selected['key'],
                'triple_alpha_selected_strategy': selected['key'],
                'triple_alpha_score': selected['score'],
                'triple_alpha_agreement_state': agreement_state,
                'triple_alpha_confirmation_count': len(choices),
                'triple_alpha_risk_multiplier': agreement_multiplier,
                'triple_alpha_confirmation_strategies': [
                    choice['key'] for choice in choices
                ],
            })
            self._set_utbot_filtered_breakout_entry_plan(
                selected_plan.get('plan_symbol') or base_symbol,
                selected_plan,
            )
            selected_plan_for_status = (
                self._get_utbot_filtered_breakout_entry_plan(
                    selected_plan.get('plan_symbol') or base_symbol,
                    selected.get('side'),
                )
                or selected_plan
            )

        summary = {
            'enabled': True,
            'utbreak': self._dual_alpha_light(statuses.get(ENTRY_STRATEGY_UT_BREAKOUT), 'UTBreakout'),
            'rspt': self._dual_alpha_light(statuses.get(ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND), 'RSPT-v3'),
            'vmt': self._dual_alpha_light(statuses.get(VOLATILITY_MANAGED_TREND_STRATEGY), 'VMT Trend'),
            'agreement_state': agreement_state,
            'agreement_risk_multiplier': agreement_multiplier,
            'confirmation_count': len(choices),
            'selected': selected.get('key') if selected else None,
            'selected_label': selected.get('label') if selected else None,
            'selected_side': selected.get('side') if selected else None,
            'selection_score': selected.get('score') if selected else None,
            'scores': {choice['key']: choice['score'] for choice in choices},
            'dynamic_leverage': (
                selected_plan_for_status.get('leverage')
                if isinstance(selected_plan_for_status, dict)
                else None
            ),
            'dynamic_leverage_tier': (
                selected_plan_for_status.get('dynamic_leverage_tier')
                if isinstance(selected_plan_for_status, dict)
                else None
            ),
            'dynamic_leverage_reason': (
                selected_plan_for_status.get('dynamic_leverage_reason')
                if isinstance(selected_plan_for_status, dict)
                else None
            ),
        }
        final_status.update({
            'strategy': STRATEGY_DISPLAY_NAMES.get(TRIPLE_ALPHA_STRATEGY, 'TRIPLE_ALPHA'),
            'entry_strategy': TRIPLE_ALPHA_STRATEGY,
            'triple_alpha_enabled': True,
            'triple_selected_strategy': summary['selected'],
            'triple_alpha': summary,
        })
        if selected:
            final_status.update({
                'accepted_code': 'ACCEPTED_ENTRY',
                'accepted_side': selected['side'],
                'stage': 'entry_ready',
                'reason': (
                    f"TRIPLE_ALPHA {agreement_state} selected {selected['label']} "
                    f"{selected['side'].upper()} at {agreement_multiplier:.0%} risk: "
                    f"{selected['reason']}"
                ),
            })
        else:
            final_status['stage'] = 'waiting'
            if agreement_state == 'conflict':
                final_status['reject_code'] = 'REJECTED_TRIPLE_DIRECTION_CONFLICT'
            final_status['reason'] = (
                f"TRIPLE_ALPHA waiting ({agreement_state}): "
                f"UT={reasons.get(ENTRY_STRATEGY_UT_BREAKOUT)}; "
                f"RSPT={reasons.get(ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND)}; "
                f"VMT={reasons.get(VOLATILITY_MANAGED_TREND_STRATEGY)}"
            )

        canonical = self._canonical_futures_symbol(final_status.get('plan_symbol') or base_symbol)
        if not isinstance(getattr(self, 'triple_alpha_last_status', None), dict):
            self.triple_alpha_last_status = {}
        self.triple_alpha_last_status[canonical] = dict(final_status)
        self._store_utbot_filtered_breakout_status(canonical, final_status)
        self.last_entry_reason[canonical] = final_status.get('reason')
        if selected:
            return selected['side'], final_status['reason'], final_status
        return None, final_status['reason'], final_status

    async def build_triple_alpha_status_text(self, symbol=None):
        target = self._canonical_futures_symbol(symbol or self.current_utbreakout_candidate_symbol or 'BTC/USDT')
        status = dict((getattr(self, 'triple_alpha_last_status', {}) or {}).get(target) or {})
        summary = status.get('triple_alpha') if isinstance(status.get('triple_alpha'), dict) else {}
        if not summary:
            return '\n'.join([
                '🚦 Triple 전략 상태',
                f'Symbol: {target}',
                '아직 Triple 평가 기록이 없습니다.',
            ])
        lines = [
            '🚦 Triple 전략 상태',
            f'Symbol: {target}',
            f"Agreement: {str(summary.get('agreement_state') or 'none').upper()} / confirmations={int(summary.get('confirmation_count') or 0)} / risk x{float(summary.get('agreement_risk_multiplier', 0.0) or 0.0):.2f}",
            f"Selected: {summary.get('selected_label') or 'NONE'} {str(summary.get('selected_side') or '').upper()}",
        ]
        for key, label in (('utbreak', 'UTBreak'), ('rspt', 'RSPT-v3'), ('vmt', 'VMT Trend')):
            item = summary.get(key) if isinstance(summary.get(key), dict) else {}
            lines.append(
                f"{label}: {str(item.get('light') or 'gray').upper()} {str(item.get('side') or 'NONE').upper()} - {item.get('reason') or '-'}"
            )
        lines.append(f"Reason: {status.get('reason') or '-'}")
        return '\n'.join(lines)

    def _quad_alpha_strategy_params(self, strategy_params, branch):
        branch = str(branch or '').lower()
        if branch not in {CROWDING_UNWIND_STRATEGY, LXR_STRATEGY}:
            return self._triple_alpha_strategy_params(strategy_params, branch)
        params = copy.deepcopy(strategy_params if isinstance(strategy_params, dict) else {})
        cfg = dict(params.get('UTBotFilteredBreakoutV1') or {})
        if branch == LXR_STRATEGY:
            lxr_cfg = self._liquidation_exhaustion_reversal_runtime_config(cfg)
            lxr_cfg['enabled'] = True
            lxr_cfg['live_enabled'] = True
            cfg['liquidation_exhaustion_reversal'] = lxr_cfg
            cfg['liquidation_exhaustion_reversal_live_enabled'] = True
            cfg['qh_flow_confirmation_enabled'] = False
            cfg['relative_strength_pullback_trend_live_enabled'] = False
            cfg['adaptive_timeframe_enabled'] = False
            cfg['entry_strategy'] = ENTRY_STRATEGY_UT_BREAKOUT
            params['active_strategy'] = LXR_STRATEGY
            params['UTBotFilteredBreakoutV1'] = cfg
            return params
        crowd_cfg = self._crowding_unwind_runtime_config(cfg)
        crowd_cfg['enabled'] = True
        crowd_cfg['live_enabled'] = True
        cfg['crowding_unwind'] = crowd_cfg
        cfg['crowding_unwind_live_enabled'] = True
        cfg['qh_flow_confirmation_enabled'] = False
        cfg['relative_strength_pullback_trend_live_enabled'] = False
        cfg['adaptive_timeframe_enabled'] = False
        cfg['entry_strategy'] = ENTRY_STRATEGY_UT_BREAKOUT
        params['active_strategy'] = CROWDING_UNWIND_STRATEGY
        params['UTBotFilteredBreakoutV1'] = cfg
        return params

    async def _calculate_quad_alpha_signal(
        self,
        symbol,
        df,
        strategy_params,
        *,
        force_reprocess=False,
    ):
        base_symbol = self._canonical_futures_symbol(symbol)
        self._clear_utbot_filtered_breakout_entry_plan(base_symbol)
        base_cfg = self._get_utbot_filtered_breakout_config(strategy_params)
        enabled_strategies = normalize_quad_alpha_enabled_strategies(
            base_cfg.get('quad_alpha_enabled_strategies')
        )
        enabled_set = set(enabled_strategies)
        qh_cfg = self._qh_flow_runtime_config(base_cfg)
        multipliers = {
            5: max(0.0, min(1.0, float(base_cfg.get('quad_alpha_five_signal_risk_multiplier', QUAD_CONFIRMATION_RISK_MULTIPLIERS[5]) or QUAD_CONFIRMATION_RISK_MULTIPLIERS[5]))),
            4: max(0.0, min(1.0, float(base_cfg.get('quad_alpha_four_signal_risk_multiplier', qh_cfg.get('quad_four_signal_multiplier', QUAD_CONFIRMATION_RISK_MULTIPLIERS[4])) or QUAD_CONFIRMATION_RISK_MULTIPLIERS[4]))),
            3: max(0.0, min(1.0, float(base_cfg.get('quad_alpha_three_signal_risk_multiplier', qh_cfg.get('quad_three_signal_multiplier', QUAD_CONFIRMATION_RISK_MULTIPLIERS[3])) or QUAD_CONFIRMATION_RISK_MULTIPLIERS[3]))),
            2: max(0.0, min(1.0, float(base_cfg.get('quad_alpha_two_signal_risk_multiplier', qh_cfg.get('quad_two_signal_multiplier', QUAD_CONFIRMATION_RISK_MULTIPLIERS[2])) or QUAD_CONFIRMATION_RISK_MULTIPLIERS[2]))),
            1: max(0.0, min(1.0, float(base_cfg.get('quad_alpha_single_signal_risk_multiplier', qh_cfg.get('quad_single_signal_multiplier', QUAD_CONFIRMATION_RISK_MULTIPLIERS[1])) or QUAD_CONFIRMATION_RISK_MULTIPLIERS[1]))),
        }
        branch_results = []

        def _disabled_branch(key, label, priority):
            reason = 'OFF: disabled by Telegram strategy selection'
            status = {
                'strategy': label,
                'symbol': base_symbol,
                'stage': 'disabled',
                'disabled': True,
                'reason': reason,
            }
            return key, label, None, reason, status, None, priority

        async def _run_branch(key, label, priority, evaluator, *, fallback_diag=False):
            plan_symbol = base_symbol
            self._clear_utbot_filtered_breakout_entry_plan(base_symbol)
            try:
                signal, reason, status = await evaluator()
                status = dict(
                    status
                    or (self._utbreakout_diag_for_symbol(base_symbol) if fallback_diag else {})
                    or {}
                )
                plan_symbol = self._canonical_futures_symbol(
                    status.get('plan_symbol') or status.get('symbol') or base_symbol
                )
                plan = (
                    dict(self._get_utbot_filtered_breakout_entry_plan(plan_symbol, signal) or {})
                    if signal in {'long', 'short'}
                    else None
                )
                return key, label, signal, reason, status, plan, priority
            except Exception as exc:
                reason = f"{label} unavailable: {type(exc).__name__}: {exc}"
                logger.exception("QUAD_ALPHA branch failed for %s: %s", base_symbol, label)
                status = {
                    'strategy': label,
                    'symbol': base_symbol,
                    'stage': 'waiting',
                    'reject_code': 'REJECTED_BRANCH_UNAVAILABLE',
                    'reason': reason,
                }
                return key, label, None, reason, status, None, priority
            finally:
                self._clear_utbot_filtered_breakout_entry_plan(plan_symbol)
                if plan_symbol != base_symbol:
                    self._clear_utbot_filtered_breakout_entry_plan(base_symbol)

        if ENTRY_STRATEGY_UT_BREAKOUT in enabled_set:
            ut_params = self._quad_alpha_strategy_params(strategy_params, ENTRY_STRATEGY_UT_BREAKOUT)
            branch_results.append(await _run_branch(
                ENTRY_STRATEGY_UT_BREAKOUT,
                'UTBreakout',
                0,
                lambda: self._calculate_utbot_filtered_breakout_signal(
                    base_symbol,
                    df,
                    ut_params,
                    force_reprocess=force_reprocess,
                ),
                fallback_diag=True,
            ))
        else:
            branch_results.append(_disabled_branch(ENTRY_STRATEGY_UT_BREAKOUT, 'UTBreakout', 0))

        if ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND in enabled_set:
            rsp_params = self._quad_alpha_strategy_params(
                strategy_params,
                ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND,
            )
            branch_results.append(await _run_branch(
                ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND,
                'RSPT-v3',
                1,
                lambda: self._calculate_relative_strength_pullback_signal(
                    base_symbol,
                    df,
                    rsp_params,
                    force_reprocess=force_reprocess,
                    forced_direction=None,
                    direction_source='RSPT-v3 BTC/ETH/alt/vol residual strength',
                    resolve_ut_direction=False,
                ),
                fallback_diag=True,
            ))
        else:
            branch_results.append(_disabled_branch(
                ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND,
                'RSPT-v3',
                1,
            ))

        if VOLATILITY_MANAGED_TREND_STRATEGY in enabled_set:
            vmt_params = self._quad_alpha_strategy_params(strategy_params, VOLATILITY_MANAGED_TREND_STRATEGY)
            branch_results.append(await _run_branch(
                VOLATILITY_MANAGED_TREND_STRATEGY,
                'VMT Trend',
                2,
                lambda: self._calculate_volatility_managed_trend_signal(
                    base_symbol,
                    df,
                    vmt_params,
                    force_reprocess=force_reprocess,
                ),
            ))
        else:
            branch_results.append(_disabled_branch(VOLATILITY_MANAGED_TREND_STRATEGY, 'VMT Trend', 2))

        if CROWDING_UNWIND_STRATEGY in enabled_set:
            crowd_params = self._quad_alpha_strategy_params(strategy_params, CROWDING_UNWIND_STRATEGY)
            branch_results.append(await _run_branch(
                CROWDING_UNWIND_STRATEGY,
                'Crowding Unwind',
                3,
                lambda: self._calculate_crowding_unwind_signal(
                    base_symbol,
                    df,
                    crowd_params,
                    force_reprocess=force_reprocess,
                ),
            ))
        else:
            branch_results.append(_disabled_branch(CROWDING_UNWIND_STRATEGY, 'Crowding Unwind', 3))

        if LXR_STRATEGY in enabled_set:
            lxr_params = self._quad_alpha_strategy_params(strategy_params, LXR_STRATEGY)
            branch_results.append(await _run_branch(
                LXR_STRATEGY,
                'LXR Reversal',
                4,
                lambda: self._calculate_liquidation_exhaustion_reversal_signal(
                    base_symbol,
                    df,
                    lxr_params,
                    force_reprocess=force_reprocess,
                ),
            ))
        else:
            branch_results.append(_disabled_branch(LXR_STRATEGY, 'LXR Reversal', 4))

        choices = []
        for key, label, side, reason, status, plan, priority in branch_results:
            if side not in {'long', 'short'} or not isinstance(plan, dict):
                continue
            choices.append({
                'key': key,
                'label': label,
                'side': side,
                'reason': reason,
                'status': status,
                'plan': plan,
                'score': self._dual_alpha_score(key, side, status, plan),
                'priority': priority,
            })

        unique_sides = {choice['side'] for choice in choices}
        selected = None
        agreement_state = 'none'
        agreement_multiplier = 0.0
        if len(unique_sides) > 1:
            agreement_state = 'conflict'
        elif choices:
            confirmation_count = len(choices)
            agreement_state = {1: 'single', 2: 'double', 3: 'triple', 4: 'quad', 5: 'five'}.get(
                confirmation_count,
                'confirmed',
            )
            agreement_multiplier = multipliers.get(confirmation_count, multipliers[1])
            selected = sorted(
                choices,
                key=lambda item: (-float(item.get('score') or 0.0), int(item.get('priority') or 0)),
            )[0]

        statuses = {key: status for key, _, _, _, status, _, _ in branch_results}
        reasons = {key: reason for key, _, _, reason, _, _, _ in branch_results}
        fallback_status = next(
            (
                statuses.get(key)
                for key in reversed(QUAD_ALPHA_BRANCH_ORDER)
                if key in enabled_set and statuses.get(key)
            ),
            {},
        )
        final_status = dict(
            (selected or {}).get('status')
            or fallback_status
            or {}
        )
        selected_plan_for_status = None
        if selected:
            selected_plan = self._dual_alpha_scale_plan(selected['plan'], agreement_multiplier)
            selected_plan.pop('dual_alpha_risk_multiplier', None)
            selected_plan.pop('triple_alpha_risk_multiplier', None)
            selected_plan.update({
                'strategy': selected['key'],
                'quad_alpha_selected_strategy': selected['key'],
                'quad_alpha_score': selected['score'],
                'quad_alpha_agreement_state': agreement_state,
                'quad_alpha_confirmation_count': len(choices),
                'quad_alpha_risk_multiplier': agreement_multiplier,
                'quad_alpha_confirmation_strategies': [choice['key'] for choice in choices],
                'quad_alpha_signal_sides': {
                    choice['key']: choice['side'] for choice in choices
                },
            })
            self._set_utbot_filtered_breakout_entry_plan(
                selected_plan.get('plan_symbol') or base_symbol,
                selected_plan,
            )
            selected_plan_for_status = (
                self._get_utbot_filtered_breakout_entry_plan(
                    selected_plan.get('plan_symbol') or base_symbol,
                    selected.get('side'),
                )
                or selected_plan
            )

        crowding_light = self._dual_alpha_light(
            statuses.get(CROWDING_UNWIND_STRATEGY),
            'Crowding Unwind',
        )
        crowding_status_metrics = (
            statuses.get(CROWDING_UNWIND_STRATEGY, {}).get('metrics')
            if isinstance(statuses.get(CROWDING_UNWIND_STRATEGY), dict)
            else None
        )
        crowding_light['metrics'] = dict(crowding_status_metrics or {})
        lxr_light = self._dual_alpha_light(
            statuses.get(LXR_STRATEGY),
            'LXR Reversal',
        )
        lxr_status_metrics = (
            statuses.get(LXR_STRATEGY, {}).get('metrics')
            if isinstance(statuses.get(LXR_STRATEGY), dict)
            else None
        )
        lxr_light['metrics'] = dict(lxr_status_metrics or {})
        summary = {
            'enabled': True,
            'enabled_strategies': list(enabled_strategies),
            'enabled_count': len(enabled_strategies),
            'disabled_strategies': [
                key for key in QUAD_ALPHA_BRANCH_ORDER if key not in enabled_set
            ],
            'utbreak': self._dual_alpha_light(statuses.get(ENTRY_STRATEGY_UT_BREAKOUT), 'UTBreakout'),
            'rspt': self._dual_alpha_light(statuses.get(ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND), 'RSPT-v3'),
            'vmt': self._dual_alpha_light(statuses.get(VOLATILITY_MANAGED_TREND_STRATEGY), 'VMT Trend'),
            'crowding_unwind': crowding_light,
            'lxr': lxr_light,
            'agreement_state': agreement_state,
            'agreement_risk_multiplier': agreement_multiplier,
            'confirmation_count': len(choices),
            'selected': selected.get('key') if selected else None,
            'selected_label': selected.get('label') if selected else None,
            'selected_side': selected.get('side') if selected else None,
            'selection_score': selected.get('score') if selected else None,
            'scores': {choice['key']: choice['score'] for choice in choices},
            'dynamic_leverage': (
                selected_plan_for_status.get('leverage')
                if isinstance(selected_plan_for_status, dict)
                else None
            ),
            'dynamic_leverage_tier': (
                selected_plan_for_status.get('dynamic_leverage_tier')
                if isinstance(selected_plan_for_status, dict)
                else None
            ),
            'dynamic_leverage_reason': (
                selected_plan_for_status.get('dynamic_leverage_reason')
                if isinstance(selected_plan_for_status, dict)
                else None
            ),
            'opportunity_risk_multiplier': (
                selected_plan_for_status.get('opportunity_risk_multiplier')
                if isinstance(selected_plan_for_status, dict)
                else None
            ),
            'opportunity_risk_tier': (
                selected_plan_for_status.get('opportunity_risk_tier')
                if isinstance(selected_plan_for_status, dict)
                else None
            ),
            'opportunity_risk_reason': (
                selected_plan_for_status.get('opportunity_risk_reason')
                if isinstance(selected_plan_for_status, dict)
                else None
            ),
        }
        final_status.update({
            'strategy': STRATEGY_DISPLAY_NAMES.get(QUAD_ALPHA_STRATEGY, 'QUAD_ALPHA'),
            'entry_strategy': QUAD_ALPHA_STRATEGY,
            'quad_alpha_enabled': True,
            'quad_selected_strategy': summary['selected'],
            'quad_alpha': summary,
        })
        if selected:
            final_status.update({
                'accepted_code': 'ACCEPTED_ENTRY',
                'accepted_side': selected['side'],
                'stage': 'entry_ready',
                'reason': (
                    f"QUAD_ALPHA {agreement_state} selected {selected['label']} "
                    f"{selected['side'].upper()} at {agreement_multiplier:.0%} base risk "
                    f"with opportunity x{float(summary.get('opportunity_risk_multiplier') or 1.0):.2f}: "
                    f"{selected['reason']}"
                ),
            })
        else:
            final_status['stage'] = 'waiting'
            if agreement_state == 'conflict':
                final_status['reject_code'] = 'REJECTED_QUAD_DIRECTION_CONFLICT'
            final_status['reason'] = (
                f"QUAD_ALPHA waiting ({agreement_state}): "
                f"UT={reasons.get(ENTRY_STRATEGY_UT_BREAKOUT)}; "
                f"RSPT={reasons.get(ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND)}; "
                f"VMT={reasons.get(VOLATILITY_MANAGED_TREND_STRATEGY)}; "
                f"CROWD={reasons.get(CROWDING_UNWIND_STRATEGY)}; "
                f"LXR={reasons.get(LXR_STRATEGY)}"
            )

        canonical = self._canonical_futures_symbol(final_status.get('plan_symbol') or base_symbol)
        if not isinstance(getattr(self, 'quad_alpha_last_status', None), dict):
            self.quad_alpha_last_status = {}
        self.quad_alpha_last_status[canonical] = dict(final_status)
        self._store_utbot_filtered_breakout_status(canonical, final_status)
        self.last_entry_reason[canonical] = final_status.get('reason')
        if selected:
            return selected['side'], final_status['reason'], final_status
        return None, final_status['reason'], final_status

    async def build_quad_alpha_status_text(self, symbol=None):
        target = self._canonical_futures_symbol(
            symbol or self.current_utbreakout_candidate_symbol or 'BTC/USDT'
        )
        status = dict((getattr(self, 'quad_alpha_last_status', {}) or {}).get(target) or {})
        summary = status.get('quad_alpha') if isinstance(status.get('quad_alpha'), dict) else {}
        if not summary:
            configured = normalize_quad_alpha_enabled_strategies(
                self._get_utbot_filtered_breakout_config().get('quad_alpha_enabled_strategies')
            )
            configured_set = set(configured)

            def _empty_light(key):
                if key not in configured_set:
                    return self._dual_alpha_light({
                        'stage': 'disabled',
                        'disabled': True,
                        'reason': 'OFF: disabled by Telegram strategy selection',
                    }, QUAD_ALPHA_BRANCH_LABELS[key])
                return {
                    'label': QUAD_ALPHA_BRANCH_LABELS[key],
                    'light': 'gray',
                    'state': 'NONE',
                    'side': None,
                    'reason': 'not evaluated since the latest strategy selection',
                }

            summary = {
                'enabled': True,
                'enabled_strategies': list(configured),
                'enabled_count': len(configured),
                'confirmation_count': 0,
                'agreement_state': 'none',
                'agreement_risk_multiplier': 0.0,
                'utbreak': _empty_light(ENTRY_STRATEGY_UT_BREAKOUT),
                'rspt': _empty_light(ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND),
                'vmt': _empty_light(VOLATILITY_MANAGED_TREND_STRATEGY),
                'crowding_unwind': _empty_light(CROWDING_UNWIND_STRATEGY),
                'lxr': _empty_light(LXR_STRATEGY),
            }
            status['reason'] = 'No evaluation has completed since the latest strategy selection.'

        def _traffic_view(item):
            item = item if isinstance(item, dict) else {}
            light = str(item.get('light') or 'gray').strip().lower()
            side = str(item.get('side') or '').strip().upper()
            reason = str(item.get('reason') or '-').strip()
            metrics = dict(item.get('metrics') or {}) if isinstance(item, dict) else {}
            if light == 'off' or item.get('disabled'):
                icon = '⚫'
                light = 'off'
                state = 'OFF'
                meaning = 'Excluded from evaluation, confirmations, conflicts, and new entries'
            elif 'crowding_derivatives_data_missing' in reason:
                icon = '⚪'
                light = 'gray'
                state = '파생데이터 누락'
                meaning = '과밀 여부를 계산할 수 없어 confirmations 제외'
            elif light == 'green':
                icon = '🟢'
                state = f'유효 {side} 신호' if side else '유효 진입 신호'
                meaning = '5전략 confirmations에 포함'
            elif light == 'red':
                icon = '🔴'
                state = f'{side} 후보 거절' if side else '진입 거절'
                meaning = '안전·품질 필터에서 차단되어 confirmations 제외'
            elif light == 'yellow':
                icon = '🟡'
                state = f'{side} 조건 대기' if side else '조건 대기'
                meaning = '유효 신호가 아직 없어 confirmations 제외'
            else:
                icon = '⚪'
                state = '미평가 또는 데이터 없음'
                meaning = '전략 평가가 완료되지 않아 confirmations 제외'
            return {
                'icon': icon,
                'state': state,
                'meaning': meaning,
                'reason': reason,
                'side': side,
                'light': light,
                'metrics': metrics,
            }

        strategy_rows = (
            ('utbreak', 'UTBreak'),
            ('rspt', 'RSPT-v3'),
            ('vmt', 'VMT Trend'),
            ('crowding_unwind', 'Crowding Unwind'),
            ('lxr', 'LXR Reversal'),
        )
        traffic = {
            key: _traffic_view(summary.get(key))
            for key, _ in strategy_rows
        }
        green_count = sum(
            1 for item in traffic.values() if item['light'] == 'green'
        )
        enabled_count = max(0, min(5, int(summary.get('enabled_count', 5) or 0)))

        lines = [
            '🧩 5-Strategy Alpha 상태',
            f'Symbol: {target}',
            f'Active strategies: {enabled_count}/5',
            '',
            '🚦 전략 신호등',
        ]
        label_width = max(len(label) for _, label in strategy_rows)
        for key, label in strategy_rows:
            item = traffic[key]
            lines.append(f"{label.ljust(label_width)}  {item['icon']} {item['state']}")
        lines.extend([
            f'🟢 유효 신호: {green_count}/{enabled_count} — 초록불만 confirmations에 포함',
            '',
            f"Agreement: {str(summary.get('agreement_state') or 'none').upper()} / confirmations={int(summary.get('confirmation_count') or 0)} / risk x{float(summary.get('agreement_risk_multiplier', 0.0) or 0.0):.2f}",
            (
                f"Opportunity risk: x{float(summary.get('opportunity_risk_multiplier') or 1.0):.2f} "
                f"({summary.get('opportunity_risk_tier') or 'baseline'})"
                if summary.get('selected')
                else "Opportunity risk: waiting for multi-strategy alignment"
            ),
            f"Selected: {summary.get('selected_label') or 'NONE'} {str(summary.get('selected_side') or '').upper()}",
            (
                f"Leverage: {int(summary.get('dynamic_leverage') or 0)}x "
                f"({summary.get('dynamic_leverage_tier') or 'waiting'})"
                if summary.get('dynamic_leverage')
                else "Leverage: waiting for a valid entry plan"
            ),
            '',
            '📋 전략별 상세 설명',
        ])
        def _metric_text(value, *, digits=2, signed=False):
            try:
                number = float(value)
            except (TypeError, ValueError):
                return 'N/A'
            pattern = f"{{:{'+' if signed else ''}.{digits}f}}"
            return pattern.format(number)

        for key, label in strategy_rows:
            item = traffic[key]
            lines.extend([
                f"{item['icon']} {label} — {item['state']}",
                f"  사유: {item['reason']}",
                f"  판정: {item['meaning']}",
            ])
            if key == 'crowding_unwind':
                metrics = item.get('metrics') if isinstance(item.get('metrics'), dict) else {}
                lines.append(
                    '  파생데이터: '
                    f"funding={_metric_text(metrics.get('funding_rate'), digits=6, signed=True)} | "
                    f"funding pct={_metric_text(metrics.get('funding_percentile'), digits=1)} | "
                    f"OI z={_metric_text(metrics.get('oi_z'), digits=2, signed=True)} | "
                    f"OI 4h={_metric_text(metrics.get('oi_change_4h_pct'), digits=2, signed=True)}% | "
                    f"L/S={_metric_text(metrics.get('long_short_ratio'), digits=2)}"
                )
                missing = list(metrics.get('missing_derivatives_fields') or [])
                if missing:
                    lines.append(f"  누락 필드: {', '.join(str(value) for value in missing)}")
            elif key == 'lxr':
                metrics = item.get('metrics') if isinstance(item.get('metrics'), dict) else {}
                lines.append(
                    '  LXR 데이터: '
                    f"shock={str(metrics.get('direction') or 'N/A').upper()} "
                    f"{_metric_text(metrics.get('shock_atr'), digits=2)}ATR | "
                    f"volume x{_metric_text(metrics.get('shock_volume_ratio'), digits=2)} | "
                    f"OI 1h={_metric_text(metrics.get('open_interest_change_1h'), digits=2, signed=True)}% | "
                    f"reclaim={_metric_text(metrics.get('reclaim_atr'), digits=2)}ATR"
                )
        lines.extend([
            '',
            f"최종 사유: {status.get('reason') or '-'}",
            '',
            '범례: 🟢 유효 신호 | 🟡 조건 대기 | 🔴 후보 거절·충돌 | ⚪ 미평가·데이터 없음 | ⚫ OFF',
        ])
        return '\n'.join(lines)

    def _resolve_dual_alpha_trading_mode(self, cfg, exchange_mode=None):
        cfg = cfg if isinstance(cfg, dict) else {}
        raw_stage = (
            cfg.get('live_activation_stage')
            or cfg.get('trading_mode')
            or cfg.get('mode')
            or ''
        )
        stage = _normalize_live_real_stage(raw_stage)
        if stage:
            return stage, None
        if (
            bool(cfg.get('live_trading', False))
            and bool(cfg.get('real_order_enabled', False))
            and not bool(cfg.get('testnet', False))
        ):
            return 'LIVE_REAL_SMALL_CAP', None
        if str(exchange_mode or '').lower() == BINANCE_TESTNET or bool(cfg.get('testnet', False)):
            return 'TESTNET_ONLY', None
        return 'unknown', 'missing_trading_mode_config'

    def resolve_live_order_path_status(self, cfg=None, exchange=None, *, selected=None):
        cfg = dict(cfg or {})
        ctrl = getattr(self, 'ctrl', None)
        try:
            exchange_mode = (
                ctrl.get_exchange_mode()
                if ctrl is not None and hasattr(ctrl, 'get_exchange_mode')
                else cfg.get('exchange_mode')
            )
        except Exception:
            exchange_mode = cfg.get('exchange_mode')
        exchange_mode = str(exchange_mode or 'unknown').lower()
        trading_mode, mode_reason = self._resolve_dual_alpha_trading_mode(cfg, exchange_mode)
        micro_cfg = self._get_micro_auto_config() if hasattr(self, '_get_micro_auto_config') else {}
        micro_cfg = micro_cfg if isinstance(micro_cfg, dict) else {}
        bridge_enabled = bool(getattr(self, 'utbreakout_auto_entry_bridge_enabled', False))
        dry_run = bool(cfg.get('dry_run', False)) or (
            bool(micro_cfg.get('enabled', False)) and bool(micro_cfg.get('dry_run', False))
        )
        demo_order_enabled = (
            exchange_mode == BINANCE_TESTNET
            or trading_mode == 'TESTNET_ONLY'
            or bool(cfg.get('testnet', False))
        ) and not dry_run
        paper_order_enabled = dry_run or trading_mode in {'PAPER_ONLY', 'DISABLED'}
        live_stage = trading_mode == 'LIVE_REAL_SMALL_CAP'
        live_flags_enabled = (
            bool(cfg.get('real_order_enabled', False))
            and bool(cfg.get('live_trading', False))
            and not bool(cfg.get('testnet', False))
        )
        has_live_flags = (
            'real_order_enabled' in cfg
            or 'live_trading' in cfg
            or 'testnet' in cfg
        )
        live_exchange = exchange_mode == BINANCE_MAINNET
        live_capable = live_stage and live_exchange and (live_flags_enabled or not has_live_flags)
        legacy_mainnet_live = bool(
            live_exchange
            and not dry_run
            and not bool(cfg.get('testnet', False))
            and not bool(cfg.get('live_parity_signal_enabled', False))
            and trading_mode in {'unknown', 'DISABLED'}
        )
        if legacy_mainnet_live:
            trading_mode = 'LEGACY_MAINNET_LIVE'
            live_capable = True
            paper_order_enabled = False
            mode_reason = None
        try:
            paused = bool(getattr(ctrl, 'is_paused', False)) if ctrl is not None else False
        except Exception:
            paused = False
        paused = paused or bool(cfg.get('global_trading_paused', False))
        active_strategy = ''
        try:
            params = self.get_runtime_strategy_params()
            active_strategy = str(params.get('active_strategy', '') or '').lower()
        except Exception:
            active_strategy = ''
        dispatcher_ready = active_strategy in UTBREAKOUT_STRATEGIES or not active_strategy
        if paused:
            order_action = 'signal_only'
            reason = 'bot_paused'
            live_order_enabled = False
        elif not bridge_enabled:
            order_action = 'signal_only'
            reason = 'bridge_off_signal_only'
            live_order_enabled = False
        elif dry_run:
            order_action = 'signal_only'
            reason = 'dry_run_signal_only'
            live_order_enabled = False
        elif demo_order_enabled:
            order_action = 'demo_order_enabled'
            reason = 'demo_or_testnet_exchange_path'
            live_order_enabled = False
        elif paper_order_enabled:
            order_action = 'signal_only'
            reason = 'paper_order_path'
            live_order_enabled = False
        elif not dispatcher_ready:
            order_action = 'signal_only'
            reason = 'active_strategy_not_entry_dispatchable'
            live_order_enabled = False
        elif live_capable:
            order_action = 'live_entry_enabled'
            reason = (
                'legacy_mainnet_entry_path_enabled'
                if legacy_mainnet_live
                else 'bridge_on_live_entry_path_enabled'
            )
            live_order_enabled = True
        else:
            order_action = 'signal_only'
            reason = mode_reason or 'live_order_path_not_enabled'
            live_order_enabled = False
        selected_value = None if selected is None else (str(selected or '').strip() or None)
        display_reason = (
            'bridge_on_but_no_selected_signal'
            if live_order_enabled and not selected_value
            else reason
        )
        summary = {
            'bridge_enabled': bool(bridge_enabled),
            'exchange_mode': exchange_mode,
            'trading_mode': trading_mode or 'unknown',
            'live_order_enabled': bool(live_order_enabled),
            'paper_order_enabled': bool(paper_order_enabled),
            'demo_order_enabled': bool(demo_order_enabled),
            'dry_run': bool(dry_run),
            'order_executor': (
                'execute_live_order_plan'
                if bool(cfg.get('live_parity_signal_enabled', False))
                else 'legacy_signal_entry'
            ),
            'order_action': order_action,
            'reason': display_reason,
            'base_reason': reason,
            'active_strategy': active_strategy or 'unknown',
            'legacy_mainnet_live': bool(legacy_mainnet_live),
        }
        if mode_reason:
            summary['mode_reason'] = mode_reason
        return summary

    def _format_dual_alpha_order_path_status(self, summary):
        summary = summary if isinstance(summary, dict) else {}
        return "\n".join([
            "Order Path:",
            f"bridge_enabled={str(bool(summary.get('bridge_enabled'))).lower()}",
            f"exchange_mode={summary.get('exchange_mode') or 'unknown'}",
            f"trading_mode={summary.get('trading_mode') or 'unknown'}",
            f"live_order_enabled={str(bool(summary.get('live_order_enabled'))).lower()}",
            f"paper_order_enabled={str(bool(summary.get('paper_order_enabled'))).lower()}",
            f"demo_order_enabled={str(bool(summary.get('demo_order_enabled'))).lower()}",
            f"dry_run={str(bool(summary.get('dry_run'))).lower()}",
            f"order_executor={summary.get('order_executor') or 'unknown'}",
            f"order_action={summary.get('order_action') or 'unknown'}",
            f"reason={summary.get('reason') or 'unknown'}",
        ])

    async def _dual_alpha_fetch_current_position_status(self, symbol=None):
        candidates = []
        if symbol:
            candidates.append(self._futures_symbol_for_order(symbol))
        try:
            active_symbols = await self.get_active_position_symbols(use_cache=False)
            for item in sorted(active_symbols or []):
                order_symbol = self._futures_symbol_for_order(item)
                if order_symbol and order_symbol not in candidates:
                    candidates.append(order_symbol)
        except Exception as exc:
            if candidates:
                logger.debug("DUAL status active-position scan failed: %s", exc)
            else:
                return {
                    'state': 'unknown',
                    'reason': 'exchange_position_fetch_failed',
                    'error': str(exc),
                }
        fetch_failed = False
        fetch_error = None
        for candidate in candidates:
            try:
                fetch_ok, pos = await self._fetch_server_position_checked(candidate)
            except Exception as exc:
                fetch_ok, pos = False, None
                fetch_error = str(exc)
            if not fetch_ok:
                fetch_failed = True
                continue
            if pos:
                return {'state': 'exists', 'source': 'exchange', 'symbol': candidate, 'position': pos}
        if fetch_failed:
            snapshot = getattr(self, 'last_live_entry_snapshot', None)
            if isinstance(snapshot, dict) and snapshot.get('symbol'):
                return {
                    'state': 'fallback',
                    'source': 'last_live_entry_snapshot',
                    'symbol': snapshot.get('symbol'),
                    'position': None,
                    'snapshot': dict(snapshot),
                    'reason': 'exchange_position_fetch_failed',
                }
            return {
                'state': 'unknown',
                'reason': 'exchange_position_fetch_failed',
                'error': fetch_error,
            }
        return {'state': 'none', 'source': 'exchange'}

    async def _dual_alpha_read_protection_order_status(self, symbol, pos=None):
        if not symbol or not pos:
            return {'status': 'SKIPPED', 'reason': 'no_position'}
        try:
            fetch_ok, orders = await self._collect_protection_orders_checked(symbol)
        except Exception as exc:
            return {
                'status': 'UNKNOWN',
                'reason': 'open_orders_fetch_failed',
                'error': str(exc),
            }
        if not fetch_ok:
            return {'status': 'UNKNOWN', 'reason': 'open_orders_fetch_failed'}
        tp_orders = []
        sl_orders = []
        reduce_only_bad = []
        for order in orders or []:
            kind = self._classify_protection_order(order)
            if kind == 'tp':
                tp_orders.append(order)
            elif kind == 'sl':
                sl_orders.append(order)
            if kind in {'tp', 'sl'} and not self._is_reduce_only_order(order):
                reduce_only_bad.append(order)
        if sl_orders and tp_orders and not reduce_only_bad:
            status = 'OK'
            reason = None
        elif not sl_orders:
            status = 'WARNING'
            reason = 'sl_order_missing'
        elif not tp_orders:
            status = 'WARNING'
            reason = 'tp_order_missing'
        else:
            status = 'WARNING'
            reason = 'reduce_only_missing'
        latest_status = dict((getattr(self, 'last_protection_order_status', {}) or {}).get(symbol, {}) or {})
        ladder_status = latest_status.get('tp_ladder') if isinstance(latest_status.get('tp_ladder'), dict) else None
        if ladder_status and ladder_status.get('mode') == 'single_tp2_with_warning' and status == 'OK':
            status = 'OK_WITH_WARNING'
            reason = ladder_status.get('reason') or 'qty_too_small_for_tp1_tp2_split'
        result = {
            'status': status,
            'reason': reason,
            'tp_count': len(tp_orders),
            'sl_count': len(sl_orders),
            'reduce_only_ok': not bool(reduce_only_bad),
            'orphan_orders': 0,
        }
        if ladder_status:
            result['tp_ladder'] = dict(ladder_status)
            result['tp_ladder_mode'] = ladder_status.get('mode')
            result['tp_ladder_reason'] = ladder_status.get('reason')
        return result

    def _format_dual_alpha_current_position_lines(self, position_status):
        status = position_status if isinstance(position_status, dict) else {}
        state = status.get('state')
        lines = ["Current Position:"]
        if state == 'exists':
            pos = status.get('position') if isinstance(status.get('position'), dict) else {}
            qty = abs(float(self._position_signed_contracts(pos) or pos.get('contracts', 0) or 0))
            lines.extend([
                f"symbol={pos.get('symbol') or status.get('symbol')}",
                f"side={str(pos.get('side') or '').upper() or 'unknown'}",
                f"qty={self._fmt_signal_trade_value(qty)}",
                f"entry={self._fmt_signal_trade_value(pos.get('entryPrice'))}",
                f"mark={self._fmt_signal_trade_value(pos.get('markPrice'))}",
                f"unrealized_pnl={self._fmt_signal_trade_value(pos.get('unrealizedPnl'))}",
                "source=exchange",
            ])
            snapshot = getattr(self, 'last_live_entry_snapshot', None)
            if (
                isinstance(snapshot, dict)
                and isinstance(snapshot.get('tp_ladder'), dict)
                and self._canonical_futures_symbol(snapshot.get('symbol')) == self._canonical_futures_symbol(pos.get('symbol') or status.get('symbol'))
            ):
                lines.extend(self._format_tp_ladder_lines(snapshot.get('tp_ladder')))
            return lines
        if state == 'fallback':
            snapshot = status.get('snapshot') if isinstance(status.get('snapshot'), dict) else {}
            lines.extend([
                "unknown",
                "source=last_live_entry_snapshot",
                f"reason={status.get('reason') or 'exchange_position_fetch_failed'}",
                f"last_symbol={snapshot.get('symbol') or 'unknown'}",
                f"last_side={str(snapshot.get('side') or '').upper() or 'unknown'}",
                f"last_qty={snapshot.get('filled_qty') or snapshot.get('final_order_qty') or 'unknown'}",
                f"last_entry={snapshot.get('price') or 'unknown'}",
            ])
            if isinstance(snapshot.get('tp_ladder'), dict):
                lines.extend(self._format_tp_ladder_lines(snapshot.get('tp_ladder')))
            return lines
        if state == 'unknown':
            lines.extend([
                "unknown",
                f"reason={status.get('reason') or 'exchange_position_fetch_failed'}",
            ])
            return lines
        lines.append("none")
        return lines

    def _format_dual_alpha_protection_lines(self, protection_status):
        status = protection_status if isinstance(protection_status, dict) else {}
        lines = ["Protection Orders:"]
        state = status.get('status') or 'UNKNOWN'
        lines.append(f"status={state}")
        if status.get('reason'):
            lines.append(f"reason={status.get('reason')}")
        if 'tp_count' in status:
            lines.append(f"tp_count={int(status.get('tp_count') or 0)}")
        if 'sl_count' in status:
            lines.append(f"sl_count={int(status.get('sl_count') or 0)}")
        if 'reduce_only_ok' in status:
            lines.append(f"reduce_only_ok={str(bool(status.get('reduce_only_ok'))).lower()}")
        if 'orphan_orders' in status:
            lines.append(f"orphan_orders={int(status.get('orphan_orders') or 0)}")
        if isinstance(status.get('tp_ladder'), dict):
            lines.extend(self._format_tp_ladder_lines(status.get('tp_ladder')))
        return lines

    def _dual_alpha_signal_final_action(self, order_path, position_status, selected):
        selected_present = bool(str(selected or '').strip() and str(selected or '').strip().lower() != 'none')
        position_exists = (
            isinstance(position_status, dict)
            and position_status.get('state') == 'exists'
        )
        if selected_present and position_exists:
            return 'preflight_blocked_existing_position'
        if selected_present and not bool((order_path or {}).get('live_order_enabled')):
            return 'signal_only_not_ordered'
        if selected_present:
            return 'ready_to_order'
        if position_exists:
            return 'holding_position_no_new_signal'
        return 'no_new_entry_signal'

    def _build_entry_execution_snapshot(
        self,
        symbol,
        side,
        *,
        requested_price=None,
        actual_entry_price=None,
        entry_plan=None,
        planned_qty=None,
        final_order_qty=None,
        filled_qty=None,
        target_notional=None,
        margin_to_use=None,
        leverage=None,
        strategy=None,
        order=None,
    ):
        plan = entry_plan if isinstance(entry_plan, dict) else {}
        entry_price = _safe_float_or_none(actual_entry_price) or _safe_float_or_none(requested_price)
        planned_qty_value = _safe_float_or_none(planned_qty)
        if planned_qty_value is None:
            planned_qty_value = _safe_float_or_none(plan.get('qty'))
        final_qty_value = _safe_float_or_none(final_order_qty)
        filled_qty_value = _safe_float_or_none(filled_qty)
        if filled_qty_value is None:
            filled_qty_value = final_qty_value
        risk_distance = _safe_float_or_none(plan.get('risk_distance'))
        sl_price = _safe_float_or_none(
            plan.get('stop_loss')
            or plan.get('stop_loss_price')
            or plan.get('sl_price')
            or plan.get('initial_sl_price')
        )
        if risk_distance is None and entry_price is not None and sl_price is not None:
            risk_distance = abs(float(entry_price) - float(sl_price))
        planned_notional = _safe_float_or_none(
            plan.get('planned_notional')
            or plan.get('target_notional')
            or target_notional
        )
        if planned_notional is None and planned_qty_value is not None and entry_price is not None:
            planned_notional = float(planned_qty_value) * float(entry_price)
        actual_notional = None
        if filled_qty_value is not None and entry_price is not None:
            actual_notional = float(filled_qty_value) * float(entry_price)
        planned_risk = _safe_float_or_none(plan.get('risk_usdt') or plan.get('risk_amount'))
        if planned_risk is None and planned_qty_value is not None and risk_distance is not None:
            planned_risk = float(planned_qty_value) * float(risk_distance)
        actual_risk = None
        if filled_qty_value is not None and risk_distance is not None:
            actual_risk = float(filled_qty_value) * float(risk_distance)
        qty_limiter = 'none'
        if (
            planned_qty_value is not None
            and final_qty_value is not None
            and abs(float(planned_qty_value) - float(final_qty_value)) > max(1e-12, abs(float(planned_qty_value)) * 1e-6)
        ):
            qty_limiter = 'exchange_step_size_or_min_qty_rounding'
        order_id = order.get('id') if isinstance(order, dict) else None
        return {
            'symbol': symbol,
            'side': str(side or '').lower(),
            'requested_price': _safe_float_or_none(requested_price),
            'price': entry_price,
            'planned_qty': planned_qty_value,
            'final_order_qty': final_qty_value,
            'filled_qty': filled_qty_value,
            'planned_notional': planned_notional,
            'actual_notional': actual_notional,
            'planned_risk': planned_risk,
            'actual_risk_estimate': actual_risk,
            'risk_distance': risk_distance,
            'sl_price': sl_price,
            'margin_to_use': _safe_float_or_none(margin_to_use),
            'leverage': _safe_float_or_none(leverage),
            'qty_limiter_reason': qty_limiter,
            'strategy': strategy,
            'order_id': order_id,
            'ts': datetime.now(timezone.utc).isoformat(),
        }

    def _record_last_live_entry_snapshot(self, snapshot):
        if isinstance(snapshot, dict):
            self.last_live_entry_snapshot = dict(snapshot)

    async def build_dual_alpha_status_text(self, symbol=None):
        strategy_params = self.get_runtime_strategy_params()
        active_strategy = str(strategy_params.get('active_strategy', '') or '').lower()
        common_cfg = self.get_runtime_common_settings() if hasattr(self, 'get_runtime_common_settings') else {}
        common_cfg = dict(common_cfg or {})
        symbol = symbol or getattr(self, 'scanner_active_symbol', None) or getattr(self, 'utbreakout_last_status_symbol', None)
        status = None
        if symbol:
            canonical = self._canonical_futures_symbol(symbol)
            status = (
                (getattr(self, 'dual_alpha_last_status', {}) or {}).get(canonical)
                or self._utbreakout_diag_for_symbol(canonical)
            )
        status = status if isinstance(status, dict) else {}
        dual = status.get('dual_alpha') if isinstance(status.get('dual_alpha'), dict) else {}
        selected = dual.get('selected_label') or dual.get('selected') if dual else None
        selected_for_path = selected if selected and str(selected).lower() != 'none' else None
        order_path = self.resolve_live_order_path_status(common_cfg, selected=selected_for_path)
        position_status = await self._dual_alpha_fetch_current_position_status(symbol)
        position = None
        position_symbol = None
        if isinstance(position_status, dict) and position_status.get('state') == 'exists':
            position = position_status.get('position') if isinstance(position_status.get('position'), dict) else {}
            position_symbol = position.get('symbol') or position_status.get('symbol')
        protection_status = await self._dual_alpha_read_protection_order_status(position_symbol, position)

        def _emoji(light):
            return {
                'green': 'GREEN',
                'yellow': 'YELLOW',
                'red': 'RED',
                'gray': 'GRAY',
            }.get(str(light or '').lower(), 'GRAY')

        def _line(item, fallback_label):
            item = item if isinstance(item, dict) else {}
            label = item.get('label') or fallback_label
            side = str(item.get('side') or '-').upper()
            state = item.get('state') or 'NONE'
            setup = item.get('setup_type') or '-'
            reason = item.get('reason') or 'no recent decision'
            direction_by = item.get('direction_by')
            direction_note = f" / direction_by {direction_by}" if direction_by else ""
            return f"{label}: {_emoji(item.get('light'))} {state} {side} / setup {setup}{direction_note} / {reason}"

        lines = [
            "DUAL Alpha status",
            f"Active: {active_strategy == DUAL_ALPHA_STRATEGY}",
            "Mode: UTBreakout + RSPT both watched, one selected, existing risk/TP/SL/order path.",
            f"Symbol: {symbol or 'no recent symbol'}",
            *_crypto_safety_status_lines(self),
            self._format_dual_alpha_order_path_status(order_path),
            "",
            *self._format_dual_alpha_current_position_lines(position_status),
            "",
            *self._format_dual_alpha_protection_lines(protection_status),
            "",
            "Signal Status:",
        ]
        if dual:
            lines.append(_line(dual.get('utbreak'), 'UTBreakout'))
            lines.append(_line(dual.get('rspt'), 'RSPT'))
            selected = dual.get('selected_label') or dual.get('selected') or 'none'
            selected_side = str(dual.get('selected_side') or '-').upper()
            score = _safe_float_or_none(dual.get('selection_score'))
            score_text = f" / score {score:.1f}" if score is not None else ""
            lines.append(f"Selected: {selected} {selected_side}{score_text}")
        else:
            lines.append("UTBreakout: GRAY NONE - / no recent dual evaluation")
            lines.append("RSPT: GRAY NONE - / no recent dual evaluation")
            lines.append("Selected: none")
            selected = 'none'
        if not selected or str(selected).lower() == 'none':
            lines.append("meaning=현재 새 진입 후보 없음. 현재 보유 포지션 존재 여부와는 별개.")
            signal_action = 'no_new_entry_signal'
        else:
            signal_action = 'selected_entry_signal'
        final_action = self._dual_alpha_signal_final_action(order_path, position_status, selected)
        lines.append(f"signal_action={signal_action}")
        lines.append(f"final_action={final_action}")
        snapshot = getattr(self, 'last_live_entry_snapshot', None)
        if isinstance(snapshot, dict) and snapshot.get('symbol'):
            lines.extend([
                "",
                "Last Live Entry:",
                f"symbol={snapshot.get('symbol')}",
                f"side={str(snapshot.get('side') or '').upper()}",
                f"filled_qty={snapshot.get('filled_qty')}",
                f"price={snapshot.get('price')}",
                f"strategy={snapshot.get('strategy')}",
                f"ts={snapshot.get('ts')}",
            ])
            if snapshot.get('planned_qty') is not None or snapshot.get('actual_risk_estimate') is not None:
                lines.extend([
                    "Execution Qty/Risk:",
                    f"planned_qty={snapshot.get('planned_qty')}",
                    f"final_order_qty={snapshot.get('final_order_qty')}",
                    f"filled_qty={snapshot.get('filled_qty')}",
                    f"planned_notional={snapshot.get('planned_notional')}",
                    f"actual_notional={snapshot.get('actual_notional')}",
                    f"planned_risk={snapshot.get('planned_risk')}",
                    f"actual_risk_estimate={snapshot.get('actual_risk_estimate')}",
                    f"qty_limiter={snapshot.get('qty_limiter_reason')}",
                ])
            if isinstance(snapshot.get('tp_ladder'), dict):
                lines.extend(self._format_tp_ladder_lines(snapshot.get('tp_ladder')))
        logger.info(
            "DUAL_STATUS_ORDER_PATH bridge_enabled=%s live_order_enabled=%s "
            "trading_mode=%s order_action=%s",
            order_path.get('bridge_enabled'),
            order_path.get('live_order_enabled'),
            order_path.get('trading_mode'),
            order_path.get('order_action'),
        )
        return "\n".join(lines)
