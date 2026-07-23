"""QH flow, crowding, liquidation reversal, and aggregate alpha suite."""

from __future__ import annotations


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
        qh_cfg = self._qh_flow_runtime_config(cfg)
        if not bool(qh_cfg.get('qh_confirmation_enabled', True)):
            return {
                'state': 'disabled',
                'allowed': True,
                'risk_multiplier': 1.0,
                'reason': 'QH confirmation disabled',
            }
        now_ms = int(time.time() * 1000)
        phase = qh_boundary_phase(now_ms, qh_cfg)
        pre_boundary = max(
            0.0,
            float(qh_cfg.get('qh_confirmation_pre_boundary_seconds', 180) or 180),
        )
        if phase['phase'] == 'stale' and 0.0 < phase['seconds_to_next_boundary'] <= pre_boundary:
            return {
                'state': 'pending',
                'allowed': False,
                'risk_multiplier': 0.0,
                'reason': (
                    f"QH boundary in {phase['seconds_to_next_boundary']:.0f}s; "
                    'wait for first 10-second flow'
                ),
                'reject_code': 'REJECTED_QH_CONFIRMATION_PENDING',
            }
        if phase['phase'] != 'ready':
            return {
                'state': 'not_applicable',
                'allowed': True,
                'risk_multiplier': 1.0,
                'reason': f"QH confirmation not active ({phase['phase']})",
            }
        qh_status = await self._fetch_qh_flow_evaluation(
            symbol,
            cfg,
            force_refresh=force_reprocess,
            now_ms=now_ms,
        )
        if qh_status.get('allowed') and qh_status.get('side') == side:
            return {
                'state': 'confirmed',
                'allowed': True,
                'risk_multiplier': 1.0,
                'reason': qh_status.get('reason'),
                'qh_flow': qh_status,
            }
        if qh_status.get('allowed') and qh_status.get('side') in {'long', 'short'}:
            if bool(qh_cfg.get('qh_confirmation_opposite_blocks', True)):
                return {
                    'state': 'conflict',
                    'allowed': False,
                    'risk_multiplier': 0.0,
                    'reason': (
                        f"QH {str(qh_status.get('side')).upper()} conflicts with "
                        f"{str(side).upper()}"
                    ),
                    'reject_code': 'REJECTED_QH_DIRECTION_CONFLICT',
                    'qh_flow': qh_status,
                }
        multiplier = max(
            0.0,
            min(1.0, float(qh_cfg.get('qh_confirmation_no_signal_multiplier', 0.60) or 0.60)),
        )
        return {
            'state': 'no_signal',
            'allowed': True,
            'risk_multiplier': multiplier,
            'reason': f"QH has no accepted signal: {qh_status.get('reason')}",
            'qh_flow': qh_status,
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
        if int(cfg.get('max_daily_trades', 0) or 0) > 0 and daily_entries >= int(cfg['max_daily_trades']):
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
        closed_rows = completed_candle_rows(rows, '15m', {'exclude_incomplete_live_candle': True})
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
            rows = completed_candle_rows(rows, '15m', {'exclude_incomplete_live_candle': True})
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
        if int(cfg.get('max_daily_trades', 0) or 0) > 0 and daily_entries >= int(cfg['max_daily_trades']):
            return _finish(
                None,
                f'risk_limit_blocked: daily trade count {daily_entries}',
                'REJECTED_DAILY_TRADE_LIMIT',
            )
        latest = rows[-1]
        entry_price = _safe_float_or_none(latest.get('close'))
        atr_value = _safe_float_or_none(decision.metrics.get('atr'))
        if entry_price is None or entry_price <= 0 or atr_value is None or atr_value <= 0:
            return _finish(None, 'Crowding entry price/ATR unavailable', 'REJECTED_CROWDING_DATA')
        total_balance, free_balance, _ = await self.get_balance_info()
        balance_for_risk = total_balance if total_balance > 0 else free_balance
        common_cfg = self.get_runtime_common_settings()
        leverage = int(max(1.0, float(common_cfg.get('leverage', 5) or 5)))
        risk_multiplier = min(
            1.0,
            max(0.0, float(decision.risk_multiplier or 0.0)),
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
                take_profit_r_multiple=float(crowd_cfg.get('take_profit_r_multiple', 2.25) or 2.25),
                take_profit_front_run_atr=0.0,
                take_profit_front_run_pct=0.0,
                min_risk_reward=min(2.0, float(crowd_cfg.get('take_profit_r_multiple', 2.25) or 2.25)),
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
            'l2_gate': dict(l2_gate or {}),
            'l2_state': l2_gate.get('state'),
            'l2_risk_multiplier': l2_gate.get('risk_multiplier'),
            'atr': atr_value,
            'partial_take_profit_enabled': True,
            'partial_take_profit_r_multiple': 1.25,
            'partial_take_profit_ratio': 0.30,
            'second_take_profit_enabled': True,
            'second_take_profit_r_multiple': float(crowd_cfg.get('take_profit_r_multiple', 2.25) or 2.25),
            'second_take_profit_ratio': 0.50,
            'atr_trailing_enabled': True,
            'atr_trailing_activation_r': 1.50,
            'atr_trailing_multiplier': 2.25,
            'ev_time_stop_enabled': True,
            'ev_time_stop_bars': int(crowd_cfg.get('time_stop_bars', 24) or 24),
            'ev_time_stop_min_mfe_r': 0.50,
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
        if int(cfg.get('max_daily_trades', 0) or 0) > 0 and daily_entries >= int(cfg['max_daily_trades']):
            return _finish(None, f'risk_limit_blocked: daily trade count {daily_entries}', 'REJECTED_DAILY_TRADE_LIMIT')

        latest_15m = rows[-1] if rows else {}
        entry_price = _safe_float_or_none(latest_15m.get('close'))
        metrics = dict(decision.metrics or {})
        atr_value = _safe_float_or_none(metrics.get('atr'))
        if entry_price is None or entry_price <= 0 or atr_value is None or atr_value <= 0:
            return _finish(None, 'LXR entry price/ATR unavailable', 'REJECTED_LXR_DATA')

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
                take_profit_r_multiple=float(lxr_cfg.get('take_profit_r_multiple', 2.60) or 2.60),
                take_profit_front_run_atr=0.0,
                take_profit_front_run_pct=0.0,
                min_risk_reward=min(2.0, float(lxr_cfg.get('take_profit_r_multiple', 2.60) or 2.60)),
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
            'l2_gate': dict(l2_gate or {}),
            'l2_state': l2_gate.get('state'),
            'l2_risk_multiplier': l2_gate.get('risk_multiplier'),
            'market_quality_summary': market_quality.get('summary'),
            'atr': atr_value,
            'atr_pct': atr_value / entry_price * 100.0,
            'partial_take_profit_enabled': True,
            'partial_take_profit_r_multiple': 1.0,
            'partial_take_profit_ratio': 0.25,
            'second_take_profit_enabled': True,
            'second_take_profit_r_multiple': float(lxr_cfg.get('take_profit_r_multiple', 2.60) or 2.60),
            'second_take_profit_ratio': 0.35,
            'runner_pct': 0.40,
            'atr_trailing_enabled': True,
            'atr_trailing_activation_r': 1.20,
            'atr_trailing_multiplier': 2.0,
            'runner_exit_enabled': True,
            'runner_chandelier_enabled': True,
            'tp1_breakeven_enabled': True,
            'tp1_breakeven_wait_for_partial': True,
            'ev_time_stop_enabled': True,
            'ev_time_stop_bars': int(lxr_cfg.get('time_stop_bars', 8) or 8),
            'ev_time_stop_min_mfe_r': 0.35,
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
        qh_cfg = self._qh_flow_runtime_config(cfg)
        qh_cfg['qh_flow_enabled'] = True
        qh_cfg['qh_flow_live_enabled'] = True
        qh_cfg['qh_confirmation_enabled'] = False
        cfg['qh_flow'] = qh_cfg
        cfg['qh_flow_live_enabled'] = True
        cfg['qh_flow_confirmation_enabled'] = False
        cfg['relative_strength_pullback_trend_live_enabled'] = False
        cfg['entry_strategy'] = ENTRY_STRATEGY_UT_BREAKOUT
        params['active_strategy'] = QH_FLOW_STRATEGY
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

        qh_params = self._triple_alpha_strategy_params(strategy_params, QH_FLOW_STRATEGY)
        qh_sig, qh_reason, qh_status = await self._calculate_qh_flow_signal(
            base_symbol,
            df,
            qh_params,
            force_reprocess=force_reprocess,
        )
        qh_status = dict(qh_status or {})
        qh_symbol = self._canonical_futures_symbol(
            qh_status.get('plan_symbol') or qh_status.get('symbol') or base_symbol
        )
        qh_plan = (
            dict(self._get_utbot_filtered_breakout_entry_plan(qh_symbol, qh_sig) or {})
            if qh_sig in {'long', 'short'}
            else None
        )
        branch_results.append((QH_FLOW_STRATEGY, 'QH-Flow', qh_sig, qh_reason, qh_status, qh_plan, 2))
        self._clear_utbot_filtered_breakout_entry_plan(qh_symbol)

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
        final_status = dict((selected or {}).get('status') or qh_status or rsp_status or ut_status or {})
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

        summary = {
            'enabled': True,
            'utbreak': self._dual_alpha_light(statuses.get(ENTRY_STRATEGY_UT_BREAKOUT), 'UTBreakout'),
            'rspt': self._dual_alpha_light(statuses.get(ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND), 'RSPT-v3'),
            'qh_flow': self._dual_alpha_light(statuses.get(QH_FLOW_STRATEGY), 'QH-Flow'),
            'agreement_state': agreement_state,
            'agreement_risk_multiplier': agreement_multiplier,
            'confirmation_count': len(choices),
            'selected': selected.get('key') if selected else None,
            'selected_label': selected.get('label') if selected else None,
            'selected_side': selected.get('side') if selected else None,
            'selection_score': selected.get('score') if selected else None,
            'scores': {choice['key']: choice['score'] for choice in choices},
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
                f"QH={reasons.get(QH_FLOW_STRATEGY)}"
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
        for key, label in (('utbreak', 'UTBreak'), ('rspt', 'RSPT-v3'), ('qh_flow', 'QH-Flow')):
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
            5: max(0.0, min(1.0, float(base_cfg.get('quad_alpha_five_signal_risk_multiplier', 1.0) or 1.0))),
            4: max(0.0, min(1.0, float(base_cfg.get('quad_alpha_four_signal_risk_multiplier', qh_cfg.get('quad_four_signal_multiplier', 1.0)) or 1.0))),
            3: max(0.0, min(1.0, float(base_cfg.get('quad_alpha_three_signal_risk_multiplier', qh_cfg.get('quad_three_signal_multiplier', 0.90)) or 0.90))),
            2: max(0.0, min(1.0, float(base_cfg.get('quad_alpha_two_signal_risk_multiplier', qh_cfg.get('quad_two_signal_multiplier', 0.75)) or 0.75))),
            1: max(0.0, min(1.0, float(base_cfg.get('quad_alpha_single_signal_risk_multiplier', qh_cfg.get('quad_single_signal_multiplier', 0.45)) or 0.45))),
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

        if QH_FLOW_STRATEGY in enabled_set:
            qh_params = self._quad_alpha_strategy_params(strategy_params, QH_FLOW_STRATEGY)
            branch_results.append(await _run_branch(
                QH_FLOW_STRATEGY,
                'QH-Flow v2',
                2,
                lambda: self._calculate_qh_flow_signal(
                    base_symbol,
                    df,
                    qh_params,
                    force_reprocess=force_reprocess,
                ),
            ))
        else:
            branch_results.append(_disabled_branch(QH_FLOW_STRATEGY, 'QH-Flow v2', 2))

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
            'qh_flow': self._dual_alpha_light(statuses.get(QH_FLOW_STRATEGY), 'QH-Flow v2'),
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
                    f"{selected['side'].upper()} at {agreement_multiplier:.0%} risk: "
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
                f"QH={reasons.get(QH_FLOW_STRATEGY)}; "
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
                'qh_flow': _empty_light(QH_FLOW_STRATEGY),
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
            ('qh_flow', 'QH-Flow v2'),
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
            f"Selected: {summary.get('selected_label') or 'NONE'} {str(summary.get('selected_side') or '').upper()}",
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
