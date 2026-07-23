"""Relative-strength pullback strategy evaluation and status."""

from __future__ import annotations


class SignalRsptMixin:
    def _relative_strength_pullback_runtime_config(self, cfg=None):
        base = default_relative_strength_pullback_config()
        source = cfg if isinstance(cfg, dict) else {}
        nested = source.get('relative_strength_pullback_trend')
        if isinstance(nested, dict):
            base.update(nested)
        for key in (
            'relative_strength_pullback_trend_shadow_enabled',
            'relative_strength_pullback_trend_live_enabled',
            'relative_strength_pullback_trend_paper_enabled',
            'forced_direction',
            'rspt_forced_direction',
            'direction_source',
            'rspt_direction_source',
        ):
            if key in source:
                base[key] = source[key]
        if source.get('entry_strategy'):
            base['entry_strategy'] = resolve_entry_strategy(source)
        # RSPT always confirms on the last completed candle and submits the
        # accepted signal immediately through the existing market-order path.
        # Legacy persisted next_open/incomplete-candle values cannot override it.
        base['entry_execution'] = 'market'
        base['exclude_incomplete_live_candle'] = True
        base['signal_basis'] = 'last_completed_candle'
        return base

    @staticmethod
    def _relative_strength_pullback_timestamp_ms(value):
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return 0
        if not np.isfinite(parsed) or parsed <= 0:
            return 0
        return int(parsed * 1000.0 if parsed < 10_000_000_000 else parsed)

    def _relative_strength_pullback_completed_rows(
        self,
        rows,
        rsp_cfg,
        decision_logs=None,
        *,
        now_ms=None,
    ):
        completed = completed_candle_rows(
            rows,
            str((rsp_cfg or {}).get('signal_tf') or '4h'),
            rsp_cfg,
            now_ms,
        )
        expected_ts = self._relative_strength_pullback_timestamp_ms(
            (decision_logs or {}).get('signal_candle_ts')
        )
        if expected_ts <= 0:
            return completed
        exact_index = None
        for idx, row in enumerate(completed):
            row_ts = self._relative_strength_pullback_timestamp_ms(
                row.get('timestamp') if isinstance(row, dict) else None
            )
            if row_ts == expected_ts:
                exact_index = idx
        if exact_index is None:
            return []
        return completed[:exact_index + 1]

    def _relative_strength_pullback_rows_from_ohlcv(self, ohlcv):
        rows = []
        for row in ohlcv or []:
            if isinstance(row, dict):
                rows.append(dict(row))
                continue
            if not isinstance(row, (list, tuple)) or len(row) < 6:
                continue
            rows.append({
                'timestamp': row[0],
                'open': row[1],
                'high': row[2],
                'low': row[3],
                'close': row[4],
                'volume': row[5],
            })
        return rows

    def _relative_strength_pullback_candidate_items(self):
        report = (
            self.coin_selector_last_result
            if isinstance(getattr(self, 'coin_selector_last_result', None), dict)
            else {}
        )
        selected = list(report.get('selected') or [])
        if selected:
            return selected
        return []

    async def _evaluate_relative_strength_pullback_candidates(
        self,
        focus_symbol=None,
        cfg=None,
        *,
        record_state=True,
        resolve_ut_directions=False,
        strategy_params=None,
        direction_consumer='RSPT_STATUS',
    ):
        strategy_params = (
            strategy_params
            if isinstance(strategy_params, dict)
            else self.get_runtime_strategy_params()
        )
        cfg = cfg if isinstance(cfg, dict) else self._get_utbot_filtered_breakout_config(
            strategy_params
        )
        rsp_cfg = self._relative_strength_pullback_runtime_config(cfg)
        candidates = self._relative_strength_pullback_candidate_items()
        signal_tf = str(rsp_cfg.get('signal_tf') or '4h').strip().lower() or '4h'
        htf_tf = str(rsp_cfg.get('trend_htf') or '1d').strip().lower() or '1d'
        signal_limit = max(
            260,
            int(rsp_cfg.get('relative_strength_long_lookback_bars', 120) or 120) + 20,
            int(rsp_cfg.get('ema_trend', 100) or 100) + 40,
        )
        htf_limit = max(
            260,
            int(rsp_cfg.get('ema_htf', 200) or 200) + 40,
        )
        normalized_candidates = []
        seen = set()
        for item in candidates:
            raw_symbol = ''
            if isinstance(item, dict):
                raw_symbol = (
                    item.get('exchange_symbol')
                    or item.get('normalized_symbol')
                    or item.get('symbol')
                    or ''
                )
            else:
                raw_symbol = str(item or '')
            if not raw_symbol:
                continue
            ok_market, canonical, invalid_reason = self._ensure_valid_utbreakout_market_symbol(
                raw_symbol,
                source='relative_strength_pullback_candidate',
            )
            if not ok_market:
                self._utbreakout_trace_event(
                    canonical,
                    'RSPT_CANDIDATE_REJECTED',
                    'INVALID_MARKET',
                    reason=invalid_reason,
                )
                continue
            key = self._utbreakout_trace_key(canonical)
            if key in seen:
                continue
            seen.add(key)
            if isinstance(item, dict):
                normalized = dict(item)
                normalized['exchange_symbol'] = canonical
                normalized['normalized_symbol'] = canonical
                normalized_candidates.append(normalized)
            else:
                normalized_candidates.append(canonical)

        symbol_values = []
        for item in normalized_candidates:
            symbol_value = item.get('exchange_symbol') if isinstance(item, dict) else item
            symbol_value = str(symbol_value or '').strip()
            if symbol_value:
                symbol_values.append(symbol_value)

        # Rank against the broader liquid selector universe (selected + watch)
        # while keeping actual entry decisions restricted to selected symbols.
        rank_symbol_values = list(symbol_values)
        if bool(rsp_cfg.get('rspt_v2_enabled', True)):
            selector_report = (
                self.coin_selector_last_result
                if isinstance(getattr(self, 'coin_selector_last_result', None), dict)
                else {}
            )
            universe_size = max(
                len(symbol_values),
                int(rsp_cfg.get('relative_strength_universe_size', 30) or 30),
            )
            broader_items = list(selector_report.get('selected') or []) + list(
                selector_report.get('watch_only') or []
            )
            for item in broader_items:
                if len(rank_symbol_values) >= universe_size:
                    break
                raw_symbol = (
                    item.get('exchange_symbol')
                    or item.get('normalized_symbol')
                    or item.get('symbol')
                    or ''
                ) if isinstance(item, dict) else str(item or '')
                if not raw_symbol:
                    continue
                ok_market, canonical_rank, _ = self._ensure_valid_utbreakout_market_symbol(
                    raw_symbol,
                    source='relative_strength_pullback_rank_universe',
                )
                if ok_market and canonical_rank not in rank_symbol_values:
                    rank_symbol_values.append(canonical_rank)
            rsp_cfg['_relative_strength_rank_symbols'] = list(rank_symbol_values)

        # RSPT-v2 ranks scanner candidates against a broader liquid universe;
        # BTC/ETH reference returns are additionally fetched to remove market beta.
        fetch_symbol_values = list(rank_symbol_values)
        if bool(rsp_cfg.get('rspt_v2_enabled', True)) and bool(rsp_cfg.get('residual_strength_enabled', True)):
            for reference_symbol in list(rsp_cfg.get('relative_strength_reference_symbols') or []):
                ok_market, canonical_reference, _ = self._ensure_valid_utbreakout_market_symbol(
                    reference_symbol,
                    source='relative_strength_pullback_reference',
                )
                if ok_market and canonical_reference not in fetch_symbol_values:
                    fetch_symbol_values.append(canonical_reference)

        if not isinstance(getattr(self, 'relative_strength_pullback_eval_cache', None), dict):
            self.relative_strength_pullback_eval_cache = {}
        cache_key = (
            tuple(fetch_symbol_values),
            signal_tf,
            htf_tf,
            str(rsp_cfg.get('forced_direction') or rsp_cfg.get('rspt_forced_direction') or '').lower(),
            str(rsp_cfg.get('direction_source') or rsp_cfg.get('rspt_direction_source') or '').lower(),
            bool(resolve_ut_directions),
            str(direction_consumer or '').upper(),
            int(time.time() // 60),
        )
        cached = self.relative_strength_pullback_eval_cache.get(cache_key)
        if isinstance(cached, dict):
            decisions = list(cached.get('decisions') or [])
            if not isinstance(getattr(self, 'relative_strength_pullback_last_decisions', None), dict):
                self.relative_strength_pullback_last_decisions = {}
            for decision in decisions:
                self.relative_strength_pullback_last_decisions[decision.symbol] = decision
                if record_state and decision.state_update:
                    if not isinstance(getattr(self, 'relative_strength_pullback_states', None), dict):
                        self.relative_strength_pullback_states = {}
                    self.relative_strength_pullback_states[decision.symbol] = dict(decision.state_update)
            return (
                decisions,
                cached.get('signal_rows_by_symbol') or {},
                cached.get('htf_rows_by_symbol') or {},
                list(cached.get('normalized_candidates') or normalized_candidates),
                dict(cached.get('rsp_cfg') or rsp_cfg),
            )

        signal_rows_by_symbol = {}
        htf_rows_by_symbol = {}
        for symbol_value in fetch_symbol_values:
            try:
                signal_ohlcv, htf_ohlcv = await asyncio.gather(
                    asyncio.to_thread(
                        self.market_data_exchange.fetch_ohlcv,
                        symbol_value,
                        signal_tf,
                        limit=signal_limit,
                    ),
                    asyncio.to_thread(
                        self.market_data_exchange.fetch_ohlcv,
                        symbol_value,
                        htf_tf,
                        limit=htf_limit,
                    ),
                )
            except Exception as exc:
                self._utbreakout_trace_event(
                    symbol_value,
                    'RSPT_DATA_FETCH',
                    'ERROR',
                    signal_tf=signal_tf,
                    htf=htf_tf,
                    reason=str(exc),
                )
                continue
            signal_rows_by_symbol[symbol_value] = self._relative_strength_pullback_rows_from_ohlcv(signal_ohlcv)
            htf_rows_by_symbol[symbol_value] = self._relative_strength_pullback_rows_from_ohlcv(htf_ohlcv)

        direction_status_by_symbol = {}
        if resolve_ut_directions:
            for symbol_value in symbol_values:
                signal_rows = signal_rows_by_symbol.get(symbol_value) or []
                htf_rows = htf_rows_by_symbol.get(symbol_value) or []
                side, reason, direction_status = self._calculate_shared_ut_direction_from_frames(
                    signal_rows,
                    htf_rows,
                    strategy_params,
                    consumer=direction_consumer,
                )
                direction_status = dict(direction_status or {})
                direction_status['resolved_side'] = side
                direction_status['reason'] = reason
                direction_status_by_symbol[symbol_value] = direction_status

            def _evaluate_for_direction(direction):
                scoped_cfg = dict(rsp_cfg)
                scoped_cfg['forced_direction'] = direction
                scoped_cfg['rspt_forced_direction'] = direction
                scoped_cfg['direction_source'] = 'UTBreakout'
                scoped_cfg['rspt_direction_source'] = 'UTBreakout'
                return evaluate_relative_strength_pullback_trend(
                    normalized_candidates,
                    signal_rows_by_symbol,
                    htf_rows_by_symbol,
                    state_by_symbol=getattr(self, 'relative_strength_pullback_states', {}),
                    config=scoped_cfg,
                    now_ms=int(time.time() * 1000),
                )

            evaluated = {
                'long': _evaluate_for_direction('long'),
                'short': _evaluate_for_direction('short'),
                None: _evaluate_for_direction(None),
            }
            indexed = {
                key: {
                    self._utbreakout_trace_key(decision.symbol): decision
                    for decision in decisions_for_side or []
                }
                for key, decisions_for_side in evaluated.items()
            }
            decisions = []
            for symbol_value in symbol_values:
                status = direction_status_by_symbol.get(symbol_value) or {}
                side = self._normalize_relative_strength_pullback_direction(status.get('resolved_side'))
                decision = indexed.get(side, indexed[None]).get(
                    self._utbreakout_trace_key(symbol_value)
                )
                if decision is None:
                    continue
                decision.logs.update({
                    'ut_direction_consumer': str(direction_consumer or 'RSPT_STATUS'),
                    'ut_direction_authority': 'UTBreakout',
                    'ut_direction_reason': status.get('reason'),
                    'ut_direction_reason_code': status.get('direction_reason_code'),
                    'ut_direction_4h_side': status.get('ut_4h_side'),
                    'ut_direction_1d_side': status.get('ut_1d_side'),
                    'ut_direction_4h_fresh_signal': status.get('ut_4h_fresh_signal'),
                    'ut_direction_1d_fresh_signal': status.get('ut_1d_fresh_signal'),
                    'ut_direction_resolved_side': side,
                })
                decisions.append(decision)
        else:
            decisions = evaluate_relative_strength_pullback_trend(
                normalized_candidates,
                signal_rows_by_symbol,
                htf_rows_by_symbol,
                state_by_symbol=getattr(self, 'relative_strength_pullback_states', {}),
                config=rsp_cfg,
                now_ms=int(time.time() * 1000),
            )

        if not isinstance(getattr(self, 'relative_strength_pullback_last_decisions', None), dict):
            self.relative_strength_pullback_last_decisions = {}
        for decision in decisions:
            self.relative_strength_pullback_last_decisions[decision.symbol] = decision
            if record_state and decision.state_update:
                if not isinstance(getattr(self, 'relative_strength_pullback_states', None), dict):
                    self.relative_strength_pullback_states = {}
                self.relative_strength_pullback_states[decision.symbol] = dict(decision.state_update)
        self.relative_strength_pullback_eval_cache = {
            cache_key: {
                'decisions': list(decisions or []),
                'signal_rows_by_symbol': signal_rows_by_symbol,
                'htf_rows_by_symbol': htf_rows_by_symbol,
                'normalized_candidates': list(normalized_candidates or []),
                'rsp_cfg': dict(rsp_cfg or {}),
                'direction_status_by_symbol': direction_status_by_symbol,
            }
        }
        return decisions, signal_rows_by_symbol, htf_rows_by_symbol, normalized_candidates, rsp_cfg

    def _relative_strength_pullback_find_decision(self, symbol, decisions):
        target_key = self._utbreakout_trace_key(symbol)
        for decision in decisions or []:
            if self._utbreakout_trace_key(decision.symbol) == target_key:
                return decision
        return None

    def _normalize_relative_strength_pullback_direction(self, value):
        text = str(value or '').strip().lower()
        if text in {'long', 'buy', 'bull', 'bullish'}:
            return 'long'
        if text in {'short', 'sell', 'bear', 'bearish'}:
            return 'short'
        return None

    def _extract_relative_strength_pullback_ut_direction(self, sig=None, status=None):
        side = self._normalize_relative_strength_pullback_direction(sig)
        if side:
            return side
        status = status if isinstance(status, dict) else {}
        if status.get('accepted_code') == 'ACCEPTED_ENTRY':
            return self._normalize_relative_strength_pullback_direction(status.get('accepted_side'))
        for key in ('candidate_side', 'candidate_signal', 'ut_bias_side', 'fresh_signal'):
            side = self._normalize_relative_strength_pullback_direction(status.get(key))
            if side:
                return side
        return None

    async def _resolve_relative_strength_pullback_ut_direction(
        self,
        symbol,
        df,
        strategy_params,
        *,
        force_reprocess=False,
    ):
        # Standalone RSPT and DUAL must use the exact same 4h/1d UT direction
        # provider. The caller's signal dataframe is intentionally not used for
        # direction selection because it may belong to a different timeframe.
        _ = df
        return await self._shared_ut_direction_filter(
            symbol,
            strategy_params,
            force_reprocess=force_reprocess,
            consumer='RSPT_STANDALONE',
        )

    async def _calculate_relative_strength_pullback_signal(
        self,
        symbol,
        df,
        strategy_params,
        *,
        force_reprocess=False,
        forced_direction=None,
        direction_source=None,
        resolve_ut_direction=True,
    ):
        cfg = self._get_utbot_filtered_breakout_config(strategy_params)
        self._clear_utbot_filtered_breakout_entry_plan(symbol)
        rsp_cfg = self._relative_strength_pullback_runtime_config(cfg)
        independent_direction = bool(rsp_cfg.get('rspt_v2_enabled', True)) and bool(
            rsp_cfg.get('independent_direction_enabled', True)
        )
        resolved_direction = None if independent_direction else self._normalize_relative_strength_pullback_direction(forced_direction)
        direction_reason = 'RSPT-v2 residual-strength direction' if independent_direction else None
        resolved_via_provider = False
        if not independent_direction and resolved_direction is None and resolve_ut_direction:
            resolved_direction, direction_reason, provider_status = await self._resolve_relative_strength_pullback_ut_direction(
                symbol,
                df,
                strategy_params,
                force_reprocess=force_reprocess,
            )
            resolved_via_provider = True
        else:
            provider_status = {}
        if resolved_via_provider:
            self._clear_utbot_filtered_breakout_entry_plan(symbol)
        source_label = (
            'RSPT-v3 BTC/ETH/alt/vol residual strength'
            if independent_direction
            else (direction_source or 'UTBreakout 4h/1d shared direction')
        )
        rsp_cfg['forced_direction'] = resolved_direction
        rsp_cfg['direction_source'] = source_label
        nested_rsp_cfg = dict(cfg.get('relative_strength_pullback_trend') or {})
        nested_rsp_cfg.update({
            'forced_direction': resolved_direction,
            'direction_source': source_label,
            'rspt_v2_enabled': bool(rsp_cfg.get('rspt_v2_enabled', True)),
            'independent_direction_enabled': independent_direction,
        })
        cfg['relative_strength_pullback_trend'] = nested_rsp_cfg
        status = {
            'strategy': STRATEGY_DISPLAY_NAMES.get(
                ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND,
                ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND,
            ),
            'entry_strategy': ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND,
            'stage': 'evaluate',
            'entry_timeframe': rsp_cfg.get('signal_tf', '4h'),
            'htf_timeframe': rsp_cfg.get('trend_htf', '1d'),
            'relative_strength_pullback_live_enabled': bool(
                rsp_cfg.get('relative_strength_pullback_trend_live_enabled', False)
            ),
            'rspt_forced_direction': resolved_direction,
            'rspt_direction_source': source_label,
            'rspt_direction_provider_reason': direction_reason,
            'rspt_internal_direction_disabled': not independent_direction,
            'rspt_direction_authority': 'RSPT-v3' if independent_direction else 'UTBreakout',
            'rspt_v2_enabled': bool(rsp_cfg.get('rspt_v2_enabled', True)),
            'entry_execution': 'market',
            'signal_basis': 'last_completed_candle',
            'exclude_incomplete_live_candle': True,
        }
        if provider_status:
            status['rspt_direction_provider_status'] = {
                'stage': provider_status.get('stage'),
                'accepted_side': provider_status.get('accepted_side'),
                'reject_code': provider_status.get('reject_code'),
                'reason': provider_status.get('reason'),
            }

        def _finish(sig, reason, code=None, *, record_failure=False, side=None):
            status['reason'] = reason
            if code:
                status['reject_code'] = code
                status['stage'] = 'entry_rejected'
            else:
                status['stage'] = 'entry_ready' if sig else 'waiting'
            if sig:
                status['accepted_code'] = 'ACCEPTED_ENTRY'
                status['accepted_side'] = sig
            self._store_utbot_filtered_breakout_status(symbol, status)
            self.last_entry_reason[symbol] = reason
            self._record_utbreakout_diagnostic_event(symbol, status)
            if record_failure and side and not bool(cfg.get('dual_alpha_direction_filter_enabled', False)):
                self._record_utbot_filtered_breakout_failure(
                    symbol,
                    side,
                    status.get('decision_candle_ts'),
                    code or reason,
                )
            return sig, reason, status

        if self.is_upbit_mode():
            return _finish(None, 'RSPT unsupported in Upbit mode', 'REJECTED_UNSUPPORTED_MODE')
        if not bool(rsp_cfg.get('relative_strength_pullback_trend_live_enabled', False)):
            return _finish(None, 'RSPT live disabled; enable from Telegram first', 'REJECTED_RSPT_LIVE_DISABLED')
        if not independent_direction and resolved_direction not in {'long', 'short'}:
            return _finish(None, 'RSPT waiting: no_ut_direction', 'REJECTED_RSPT_NO_UT_DIRECTION')

        decisions, signal_rows_by_symbol, _, candidates, rsp_cfg = await self._evaluate_relative_strength_pullback_candidates(
            focus_symbol=symbol,
            cfg=cfg,
        )
        status['scanner_candidate_count'] = len(candidates)
        status['rspt_candidate_count'] = len(decisions)
        if not candidates:
            return _finish(None, 'no_scanner_candidates', 'REJECTED_RSPT_NO_SCANNER_CANDIDATE')
        decision = self._relative_strength_pullback_find_decision(symbol, decisions)
        if decision is None:
            return _finish(None, 'RSPT waiting: symbol is not in scanner candidates', 'REJECTED_RSPT_NOT_SCANNER_CANDIDATE')

        symbol = decision.symbol
        status['rspt_decision'] = {
            'side': decision.side,
            'entry_ready': decision.entry_ready,
            'entry_execution': decision.entry_execution,
            'reason': decision.reason,
            'logs': dict(decision.logs or {}),
        }
        decision_logs = dict(decision.logs or {})
        status['rspt_forced_direction'] = decision_logs.get('rspt_forced_direction', resolved_direction)
        status['rspt_direction_source'] = decision_logs.get('rspt_direction_source', source_label)
        status['rspt_ignored_opposite_side'] = decision_logs.get('rspt_ignored_opposite_side')
        status['rspt_original_candidate_sides'] = decision_logs.get('rspt_original_candidate_sides')
        status['setup_type'] = decision_logs.get('setup_type')
        status['rspt_size_multiplier'] = decision_logs.get('size_multiplier', decision_logs.get('risk_multiplier', 1.0))
        status['rspt_size_reduction_reasons'] = decision_logs.get('size_reduction_reasons')
        status.update({
            'candidate_signal': decision.side,
            'candidate_side': decision.side,
            'candidate_type': 'relative_strength_pullback_trend',
        })
        if decision.state_update:
            status['rspt_state_update'] = dict(decision.state_update)
        self._utbreakout_trace_event(
            symbol,
            'RSPT_DECISION',
            'READY' if decision.entry_ready else 'WAIT',
            side=decision.side,
            reason=decision.reason,
            entry_execution=decision.entry_execution,
        )
        if not decision.entry_ready or decision.side not in {'long', 'short'}:
            return _finish(None, f"RSPT waiting: {decision.reason}", None)

        side = decision.side
        if not self.is_trade_direction_allowed(side):
            return _finish(
                None,
                self.format_trade_direction_block_reason(side),
                'REJECTED_DIRECTION_FILTER',
                record_failure=False,
                side=side,
            )

        daily_count, daily_pnl = self.db.get_daily_stats()
        daily_entries = self.get_automatic_daily_entry_count()
        status['daily_pnl'] = daily_pnl
        status['daily_entries'] = daily_entries
        if float(cfg.get('daily_max_loss_usdt', 0) or 0) > 0 and float(daily_pnl or 0) <= -float(cfg['daily_max_loss_usdt']):
            return _finish(
                None,
                f"risk_limit_blocked: daily pnl {daily_pnl:.2f}",
                'REJECTED_DAILY_LOSS_LIMIT',
                side=side,
            )
        if int(cfg.get('max_daily_trades', 0) or 0) > 0 and daily_entries >= int(cfg['max_daily_trades']):
            return _finish(
                None,
                f"risk_limit_blocked: daily trade count {daily_entries}",
                'REJECTED_DAILY_LOSS_LIMIT',
                side=side,
            )

        rows = signal_rows_by_symbol.get(symbol) or []
        completed_rows = self._relative_strength_pullback_completed_rows(
            rows,
            rsp_cfg,
            decision_logs,
            now_ms=int(time.time() * 1000),
        )
        if len(completed_rows) < 30:
            return _finish(
                None,
                'REJECTED_RSPT_DATA: completed signal rows insufficient or decision candle mismatch',
                'REJECTED_RSPT_DATA',
                side=side,
            )
        signal_df = pd.DataFrame(completed_rows)
        for col in ['timestamp', 'open', 'high', 'low', 'close', 'volume']:
            if col in signal_df.columns:
                signal_df[col] = pd.to_numeric(signal_df[col], errors='coerce')
        closed = signal_df.dropna(subset=['timestamp', 'open', 'high', 'low', 'close']).reset_index(drop=True)
        if len(closed) < 30:
            return _finish(None, 'REJECTED_RSPT_DATA: valid completed rows insufficient', 'REJECTED_RSPT_DATA', side=side)

        decision_row = closed.iloc[-1]
        decision_ts = self._relative_strength_pullback_timestamp_ms(decision_row.get('timestamp'))
        expected_decision_ts = self._relative_strength_pullback_timestamp_ms(
            decision_logs.get('signal_candle_ts')
        )
        if expected_decision_ts > 0 and decision_ts != expected_decision_ts:
            return _finish(
                None,
                'REJECTED_RSPT_DATA: completed decision candle mismatch',
                'REJECTED_RSPT_DATA',
                side=side,
            )
        entry_price = float(decision_row['close'])
        status['decision_candle_ts'] = decision_ts
        status['signal_candle_close_ts'] = decision_logs.get('signal_candle_close_ts')
        status['signal_candle_closed'] = True
        status['entry_execution'] = 'market'
        status['entry_price'] = entry_price
        metrics = self._calculate_utbreakout_timeframe_metrics(closed, cfg)
        atr_value = metrics.get('atr')
        atr_pct = metrics.get('atr_pct')
        if not self._is_valid_number(atr_value) or float(atr_value) <= 0:
            return _finish(None, 'REJECTED_ATR_TOO_LOW: ATR unavailable', 'REJECTED_ATR_TOO_LOW', record_failure=True, side=side)

        if bool(rsp_cfg.get('entry_chase_guard_enabled', True)):
            fetch_ticker = getattr(self.market_data_exchange, 'fetch_ticker', None)
            if callable(fetch_ticker):
                try:
                    ticker = await asyncio.to_thread(fetch_ticker, symbol)
                    live_price = _safe_float_or_none(
                        (ticker or {}).get('last')
                        or (ticker or {}).get('close')
                        or (ticker or {}).get('mark')
                    )
                    if live_price and live_price > 0:
                        adverse_move_atr = (live_price - entry_price) / float(atr_value)
                        if side == 'short':
                            adverse_move_atr = (entry_price - live_price) / float(atr_value)
                        status['rspt_live_entry_reference'] = live_price
                        status['rspt_entry_chase_atr'] = adverse_move_atr
                        max_chase_atr = float(rsp_cfg.get('entry_chase_max_atr', 0.35) or 0.35)
                        if adverse_move_atr > max_chase_atr:
                            return _finish(
                                None,
                                f'RSPT waiting: entry chase {adverse_move_atr:.2f} ATR exceeds {max_chase_atr:.2f}',
                                'REJECTED_RSPT_ENTRY_CHASE',
                                side=side,
                            )
                        entry_price = float(live_price)
                        status['entry_price'] = entry_price
                except Exception as exc:
                    status['rspt_entry_chase_guard_error'] = str(exc)

        filter_values = dict(metrics or {})
        filter_values['entry_price'] = entry_price
        filter_values['atr_pct'] = atr_pct
        filter_values['entry_timeframe'] = rsp_cfg.get('signal_tf', '4h')
        try:
            futures_context = await self._fetch_utbreakout_futures_context(symbol)
            if isinstance(futures_context, dict):
                filter_values.update(futures_context)
                status.update(futures_context)
        except Exception as exc:
            status['futures_context_error'] = str(exc)
        try:
            market_regime_context = await self._fetch_utbreakout_market_regime_context(cfg)
            if isinstance(market_regime_context, dict):
                filter_values['market_regime_context'] = market_regime_context
                status['market_regime_summary'] = market_regime_context.get('summary')
        except Exception as exc:
            status['market_regime_error'] = str(exc)
        try:
            structure_lookback = max(
                3,
                int(cfg.get('structure_stop_lookback_bars', cfg.get('runner_structure_lookback', 5)) or 5),
            )
            recent_structure = closed.tail(structure_lookback)
            filter_values['recent_swing_low'] = float(recent_structure['low'].astype(float).min())
            filter_values['recent_swing_high'] = float(recent_structure['high'].astype(float).max())
        except Exception:
            logger.debug("RSPT structure stop values unavailable for %s", symbol, exc_info=True)

        l2_gate = await self._evaluate_shared_l2_gate(symbol, cfg, force_refresh=force_reprocess)
        status['l2_gate'] = l2_gate
        status['l2_state'] = l2_gate.get('state')
        status['l2_risk_multiplier'] = l2_gate.get('risk_multiplier')
        if not l2_gate.get('allowed', False):
            return _finish(
                None,
                f"REJECTED_L2_STRESSED: {l2_gate.get('reason')}",
                'REJECTED_L2_STRESSED',
                record_failure=True,
                side=side,
            )
        qh_confirmation = await self._qh_flow_confirmation(
            symbol,
            side,
            cfg,
            force_reprocess=force_reprocess,
        )
        status['qh_confirmation'] = qh_confirmation
        status['qh_confirmation_state'] = qh_confirmation.get('state')
        status['qh_confirmation_risk_multiplier'] = qh_confirmation.get('risk_multiplier')
        if not qh_confirmation.get('allowed', True):
            return _finish(
                None,
                f"{qh_confirmation.get('reject_code') or 'REJECTED_QH_CONFIRMATION'}: {qh_confirmation.get('reason')}",
                qh_confirmation.get('reject_code') or 'REJECTED_QH_CONFIRMATION',
                record_failure=False,
                side=side,
            )

        market_quality = self._evaluate_utbreakout_market_quality(side, cfg, filter_values)
        status['market_quality'] = market_quality
        status['market_quality_summary'] = market_quality.get('summary')
        if market_quality.get('state') is False or market_quality.get('hard_block'):
            return _finish(
                None,
                f"market_quality_rejected: {market_quality.get('summary')}",
                'REJECTED_MARKET_QUALITY',
                record_failure=True,
                side=side,
            )

        total_balance, free_balance, _ = await self.get_balance_info()
        balance_for_risk = total_balance if total_balance > 0 else free_balance
        common_cfg = self.get_runtime_common_settings()
        leverage = int(max(1.0, float(common_cfg.get('leverage', 5) or 5)))
        market_quality_multiplier = max(0.0, min(1.0, float(market_quality.get('risk_multiplier', 1.0) or 1.0)))
        rspt_size_multiplier = max(0.0, min(1.0, float(decision_logs.get('size_multiplier', decision_logs.get('risk_multiplier', 1.0)) or 1.0)))
        l2_multiplier = max(0.0, min(1.0, float(l2_gate.get('risk_multiplier', 1.0) or 0.0)))
        qh_multiplier = max(0.0, min(1.0, float(qh_confirmation.get('risk_multiplier', 1.0) or 0.0)))
        risk_multiplier = market_quality_multiplier * rspt_size_multiplier * l2_multiplier * qh_multiplier
        risk_budget = resolve_utbreakout_risk_budget(
            balance_for_risk,
            cfg,
            multiplier=risk_multiplier,
            daily_pnl_usdt=daily_pnl,
        )
        risk_per_trade_percent = risk_budget['risk_per_trade_percent']
        max_risk_per_trade_usdt = risk_budget['max_risk_per_trade_usdt']
        structure_stop = _safe_float_or_none(decision_logs.get('pullback_structure_stop'))
        if structure_stop is None:
            structure_stop = (
                filter_values.get('recent_swing_low')
                if side == 'long'
                else filter_values.get('recent_swing_high')
            )
        status['rspt_structure_stop'] = structure_stop
        try:
            plan = calculate_risk_plan(
                side=side,
                entry_price=entry_price,
                atr_value=atr_value,
                stop_atr_multiplier=cfg.get('stop_atr_multiplier', 1.5),
                ut_stop=None,
                structure_stop=structure_stop,
                structure_buffer_atr=rsp_cfg.get('structure_stop_buffer_atr', cfg.get('structure_stop_buffer_atr', 0.20)),
                take_profit_r_multiple=cfg.get('take_profit_r_multiple', 3.50),
                take_profit_front_run_atr=cfg.get('take_profit_front_run_atr', 0.14),
                take_profit_front_run_pct=cfg.get('take_profit_front_run_pct', 0.055),
                min_risk_reward=cfg.get('min_risk_reward', 2.0),
                balance_usdt=balance_for_risk,
                risk_per_trade_percent=risk_per_trade_percent,
                max_risk_per_trade_usdt=max_risk_per_trade_usdt,
                leverage=leverage,
            )
            stop_distance_atr = float(plan.get('risk_distance', 0.0) or 0.0) / float(atr_value)
            stop_distance_min_atr = float(rsp_cfg.get('stop_distance_min_atr', 0.60) or 0.60)
            stop_distance_max_atr = float(rsp_cfg.get('stop_distance_max_atr', 2.00) or 2.00)
            if stop_distance_atr < stop_distance_min_atr or stop_distance_atr > stop_distance_max_atr:
                raise ValueError(
                    f'RSPT stop distance {stop_distance_atr:.2f} ATR outside '
                    f'{stop_distance_min_atr:.2f}-{stop_distance_max_atr:.2f} ATR'
                )
            plan = cap_utbreakout_risk_plan_to_margin(
                plan,
                free_balance=free_balance,
                leverage=leverage,
                entry_price=entry_price,
            )
        except ValueError as exc:
            return _finish(
                None,
                f"REJECTED_RSPT_RISK_PLAN: {exc}",
                'REJECTED_RSPT_RISK_PLAN',
                record_failure=True,
                side=side,
            )

        plan.update({
            'strategy': ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND,
            'plan_symbol': symbol,
            'effective_profile_version': cfg.get('effective_profile_version'),
            'entry_timeframe': rsp_cfg.get('signal_tf', '4h'),
            'exit_timeframe': cfg.get('exit_timeframe', cfg.get('entry_timeframe', '15m')),
            'htf_timeframe': rsp_cfg.get('trend_htf', '1d'),
            'decision_candle_ts': decision_ts,
            'signal_candle_close_ts': decision_logs.get('signal_candle_close_ts'),
            'signal_candle_closed': True,
            'signal_basis': 'last_completed_candle',
            'entry_execution': 'market',
            'entry_execution_policy': 'market_immediately_after_completed_candle',
            'atr': atr_value,
            'atr_pct': atr_pct,
            'market_quality_enabled': bool(cfg.get('market_quality_enabled', True)),
            'market_quality_risk_multiplier': market_quality_multiplier,
            'rspt_size_multiplier': rspt_size_multiplier,
            'rspt_risk_multiplier': risk_multiplier,
            'l2_gate': l2_gate,
            'l2_state': l2_gate.get('state'),
            'l2_risk_multiplier': l2_multiplier,
            'qh_confirmation': qh_confirmation,
            'qh_confirmation_state': qh_confirmation.get('state'),
            'qh_confirmation_risk_multiplier': qh_multiplier,
            'market_quality_summary': market_quality.get('summary'),
            'fixed_take_profit_enabled': bool(cfg.get('fixed_take_profit_enabled', True)),
            'partial_take_profit_enabled': bool(cfg.get('partial_take_profit_enabled', True)),
            'partial_take_profit_r_multiple': float(rsp_cfg.get('partial_take_profit_r_multiple', 1.50) or 1.50),
            'partial_take_profit_ratio': float(rsp_cfg.get('partial_take_profit_ratio', 0.25) or 0.25),
            'second_take_profit_enabled': bool(cfg.get('second_take_profit_enabled', True)),
            'second_take_profit_r_multiple': float(cfg.get('second_take_profit_r_multiple', cfg.get('take_profit_r_multiple', 3.50)) or 3.50),
            'second_take_profit_ratio': float(cfg.get('second_take_profit_ratio', 0.40) or 0.40),
            'atr_trailing_enabled': bool(cfg.get('atr_trailing_enabled', True)),
            'atr_trailing_multiplier': float(rsp_cfg.get('atr_trailing_multiplier', 2.75) or 2.75),
            'atr_trailing_activation_r': float(rsp_cfg.get('atr_trailing_activation_r', 2.00) or 2.00),
            'tp1_breakeven_enabled': bool(rsp_cfg.get('tp1_breakeven_enabled', False)),
            'tp1_breakeven_trigger_r': float(cfg.get('tp1_breakeven_trigger_r', cfg.get('partial_take_profit_r_multiple', 1.00)) or 1.00),
            'tp1_breakeven_offset_r': float(cfg.get('tp1_breakeven_offset_r', 0.03) or 0.03),
            'tp1_breakeven_wait_for_partial': bool(cfg.get('tp1_breakeven_wait_for_partial', True)),
            'tp1_breakeven_qty_tolerance': float(cfg.get('tp1_breakeven_qty_tolerance', 0.08) or 0.08),
            'ev_time_stop_enabled': bool(rsp_cfg.get('ev_time_stop_enabled', True)),
            'ev_time_stop_bars': int(rsp_cfg.get('ev_time_stop_bars', 8) or 8),
            'ev_time_stop_min_mfe_r': float(rsp_cfg.get('ev_time_stop_min_mfe_r', 0.50) or 0.50),
            'rspt_stop_distance_atr': stop_distance_atr,
            'rspt_structure_stop': structure_stop,
            'rspt_reason': decision.reason,
            'rspt_setup_type': decision_logs.get('setup_type'),
            'rspt_logs': decision_logs,
        })
        self._set_utbot_filtered_breakout_entry_plan(symbol, plan)
        if isinstance(getattr(self, 'relative_strength_pullback_states', None), dict):
            self.relative_strength_pullback_states.pop(symbol, None)
        status['entry_plan'] = dict(plan)
        status['risk_summary'] = (
            f"risk={plan['risk_usdt']:.4f} USDT, distance={plan['risk_distance']:.4f}, "
            f"SL={plan['stop_loss']:.4f}, TP={plan['take_profit']:.4f}, "
            f"qty={plan['qty']:.8f}, margin={plan['planned_margin']:.2f}, "
            f"notional={plan['planned_notional']:.2f}, RR={plan['rr_multiple']:.2f}"
        )
        return _finish(
            side,
            f"ACCEPTED_ENTRY: RSPT {side.upper()} {decision_logs.get('setup_type') or decision.reason} confirmed on completed candle; market entry",
            None,
        )

    async def build_relative_strength_pullback_status_text(self, symbol=None):
        strategy_params = self.get_runtime_strategy_params()
        cfg = self._get_utbot_filtered_breakout_config(strategy_params)
        rsp_cfg = self._relative_strength_pullback_runtime_config(cfg)
        active_strategy = str(strategy_params.get('active_strategy', '') or '').lower()
        live_enabled = bool(
            rsp_cfg.get('relative_strength_pullback_trend_live_enabled', False)
        )
        signal_tf = str(rsp_cfg.get('signal_tf', '4h') or '4h')
        htf_tf = str(rsp_cfg.get('trend_htf', '1d') or '1d')
        execution = str(rsp_cfg.get('entry_execution', 'market') or 'market')
        execution_label = {
            'next_open': '다음 봉 시가',
            'market': '완성봉 확인 직후 시장가',
            'close': '현재 봉 종가',
        }.get(execution, execution)

        reason_labels = {
            'no_ut_direction': 'UT 방향 대기',
            'no_entry_setup': 'RSPT 진입 패턴 대기',
            'stale_signal': '신호가 오래되어 새 신호 대기',
            'insufficient_data': '분석 데이터 부족',
            'indicator_not_ready': '지표 계산 대기',
            'adx_too_low': 'ADX 추세 강도 기준 미달',
            'relative_strength_hard_block': '상대강도 기준 미달',
            'trend_filter_failed': '추세 조건 불일치',
            'breakout_continuation_confirmed': '돌파 지속 확인',
            'trend_pullback_confirmed': '추세 눌림 확인',
            'market_quality_rejected': '시장 품질 기준 미달',
            'risk_limit_blocked': '리스크 한도 차단',
            'no_scanner_candidates': '스캐너 후보 없음',
        }
        setup_labels = {
            'breakout_continuation': '돌파 지속',
            'trend_pullback': '추세 눌림',
            'relative_strength_pullback_trend': '상대강도 눌림 추세',
        }
        reduction_labels = {
            'adx_weak_size_reduced': 'ADX 약세',
            'relative_strength_size_reduced': '상대강도 약세',
            'market_quality_size_reduced': '시장 품질 감액',
        }
        explicit_wait_reasons = {
            'no_ut_direction',
            'no_entry_setup',
            'stale_signal',
            'insufficient_data',
            'indicator_not_ready',
            'no_scanner_candidates',
        }
        explicit_block_reasons = {
            'adx_too_low',
            'relative_strength_hard_block',
            'trend_filter_failed',
            'market_quality_rejected',
            'risk_limit_blocked',
        }

        def _safe_multiplier(value):
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                return 1.0
            if not np.isfinite(parsed):
                return 1.0
            return max(0.0, min(1.0, parsed))

        def _reason_text(reason):
            key = str(reason or 'not_evaluated').strip()
            return reason_labels.get(key, key.replace('_', ' '))

        def _setup_text(value):
            key = str(value or '').strip()
            return setup_labels.get(key, key.replace('_', ' ') if key else '대기')

        def _reduction_text(logs, size_mult):
            raw = logs.get('size_reduction_reasons')
            if isinstance(raw, str):
                reasons = [raw]
            elif isinstance(raw, (list, tuple, set)):
                reasons = list(raw)
            else:
                reasons = []
            labels = []
            for item in reasons:
                key = str(item or '').strip()
                label = reduction_labels.get(key, key.replace('_', ' '))
                if label and label not in labels:
                    labels.append(label)
            if not labels and size_mult < 0.999:
                adx_mult = _safe_multiplier(logs.get('adx_size_multiplier', 1.0))
                rs_mult = _safe_multiplier(logs.get('relative_strength_size_multiplier', 1.0))
                if adx_mult < 0.999:
                    labels.append('ADX 약세')
                if rs_mult < 0.999:
                    labels.append('상대강도 약세')
            return ', '.join(labels) if labels else '품질 필터에 따른 자동 감액'

        def _classify(decision):
            logs = dict(decision.logs or {})
            reason = str(decision.reason or 'not_evaluated').strip()
            side = str(decision.side or '').upper()
            setup = _setup_text(logs.get('setup_type'))
            size_mult = _safe_multiplier(
                logs.get('size_multiplier', logs.get('risk_multiplier', 1.0))
            )
            if bool(decision.entry_ready):
                light = 'yellow' if size_mult < 0.999 else 'green'
            elif reason in explicit_block_reasons or any(
                token in reason
                for token in ('hard_block', 'blocked', 'rejected', 'too_low', 'failed', 'risk_limit')
            ):
                light = 'red'
            else:
                light = 'white'
            if reason in explicit_wait_reasons:
                light = 'white'
            return {
                'decision': decision,
                'logs': logs,
                'reason': reason,
                'reason_text': _reason_text(reason),
                'side': side,
                'setup': setup,
                'size_mult': size_mult,
                'percent': int(round(size_mult * 100)),
                'light': light,
            }

        active_label = '🟢 활성화' if live_enabled else '⚪ 비활성화'
        lines = [
            '📊 RSPT 전략 상태',
            f"전략: Relative Strength Pullback Trend ({active_strategy or 'n/a'})",
            f'상태: {active_label}',
            f'시간 프레임: 신호 {signal_tf} / 상위 추세 {htf_tf}',
            '방향 기준: UT 4시간봉 + 1일 추세만 사용',
            f'진입 실행: {execution_label} ({execution})',
        ]

        try:
            decisions, _, _, candidates, _ = await self._evaluate_relative_strength_pullback_candidates(
                focus_symbol=symbol,
                cfg=cfg,
                record_state=False,
                resolve_ut_directions=True,
                strategy_params=strategy_params,
                direction_consumer='RSPT_STATUS',
            )
            classified = [_classify(decision) for decision in decisions]
            counts = {
                light: sum(1 for item in classified if item['light'] == light)
                for light in ('green', 'yellow', 'red', 'white')
            }
            lines.extend([
                f'분석 종목: {len(decisions)}/{len(candidates)}',
                (
                    '상태 요약: '
                    f"🟢 진입 가능 {counts['green']} | "
                    f"🟡 수량 감소 {counts['yellow']} | "
                    f"🔴 진입 차단 {counts['red']} | "
                    f"⚪ 신호 대기 {counts['white']}"
                ),
                '',
                '종목별 상태',
            ])

            if symbol:
                focus_key = self._utbreakout_trace_key(symbol)
                classified.sort(
                    key=lambda item: 0
                    if self._utbreakout_trace_key(item['decision'].symbol) == focus_key
                    else 1
                )

            for item in classified[:8]:
                decision = item['decision']
                symbol_text = str(decision.symbol)
                side_text = item['side'] or '방향 미정'
                code = item['reason']
                if item['light'] == 'green':
                    lines.extend([
                        f"🟢 {symbol_text} — {side_text} 진입 가능",
                        (
                            f"   진입 형태: {item['setup']} | "
                            f"수량/증거금 {item['percent']}% (x{item['size_mult']:.2f}) | "
                            f"실행: {execution_label}"
                        ),
                        f"   근거: {item['reason_text']} | 코드: {code}",
                    ])
                elif item['light'] == 'yellow':
                    reduction = _reduction_text(item['logs'], item['size_mult'])
                    lines.extend([
                        f"🟡 {symbol_text} — {side_text} 조건부 진입",
                        (
                            f"   수량/증거금 {item['percent']}% (x{item['size_mult']:.2f}) | "
                            f"감소 이유: {reduction}"
                        ),
                        (
                            f"   진입 형태: {item['setup']} | 실행: {execution_label} | "
                            f"코드: {code}"
                        ),
                    ])
                elif item['light'] == 'red':
                    lines.extend([
                        f"🔴 {symbol_text} — {side_text} 진입 차단",
                        f"   이유: {item['reason_text']} | 주문 없음 | 코드: {code}",
                    ])
                elif code == 'no_ut_direction':
                    direction_reason = str(item['logs'].get('ut_direction_reason') or 'UT 방향 계산 대기')
                    side_4h = str(item['logs'].get('ut_direction_4h_side') or '-').upper()
                    side_1d = str(item['logs'].get('ut_direction_1d_side') or '-').upper()
                    lines.extend([
                        f"⚪ {symbol_text} — UT 방향 대기",
                        (
                            f"   {direction_reason} | 4h {side_4h} / 1d {side_1d} | "
                            'UT 방향 확인 후 RSPT 조건 검사 | 주문 없음 | 코드: no_ut_direction'
                        ),
                    ])
                else:
                    lines.extend([
                        f"⚪ {symbol_text} — {side_text} 신호 대기",
                        f"   이유: {item['reason_text']} | 주문 없음 | 코드: {code}",
                    ])

            if not classified:
                lines.append('⚪ 분석 가능한 스캐너 후보 없음')
        except Exception as exc:
            lines.extend([
                '',
                f'🔴 상태 조회 오류: {type(exc).__name__}: {exc}',
            ])

        lines.extend([
            '',
            '안내: 스캐너·리스크·TP/SL·주문 안전 경로는 기존 로직을 그대로 사용합니다.',
        ])
        return "\n".join(lines)
