"""Candle processing, strategy dispatch, and execution confirmation."""

from __future__ import annotations


class SignalCandleMixin:
    async def check_status(self, symbol, price):
        try:
            total, free, mmr = await self.get_balance_info()
            count, daily_pnl = self.db.get_daily_stats()
            pos = await self.get_server_position(symbol, use_cache=False)

            # ?꾨왂 ?곹깭 媛?몄삤湲?
            strategy_params = self.get_runtime_strategy_params()
            comm_cfg = self.get_runtime_common_settings()
            active_strategy = strategy_params.get('active_strategy', 'utbot').upper()
            entry_mode = strategy_params.get('entry_mode', 'cross').upper()
            if active_strategy in {'UTBOT', 'UTSMC', 'RSIBB', 'UTRSIBB', 'UTRSI', 'UTBB', 'UTBOT_FILTERED_BREAKOUT_V1'}:
                entry_mode = active_strategy

            # MicroVBO State
            vbo_state = self.vbo_states.get(symbol, {})
            # FractalFisher State
            fisher_state = self.fisher_states.get(symbol, {})

            # [Fix] Multi-symbol Status Data
            pos_side = pos['side'].upper() if pos else 'NONE'
            symbol_status = {
                'engine': 'Signal', 'symbol': symbol, 'price': price,
                'total_equity': total, 'free_usdt': free, 'mmr': mmr,
                'daily_count': count, 'daily_pnl': daily_pnl,
                'network': self.ctrl.get_network_status_label(),
                'exchange_id': self.ctrl.get_exchange_display_name(),
                'market_data_exchange_id': getattr(self.market_data_exchange, 'id', 'unknown'),
                'market_data_source': getattr(self.ctrl, 'market_data_source_label', self.ctrl.get_exchange_display_name()),
                'pos_side': pos_side,
                'entry_price': float(pos['entryPrice']) if pos else 0.0,
                'coin_amt': float(pos['contracts']) if pos else 0.0,
                'pnl_pct': float(pos['percentage']) if pos else 0.0,
                'pnl_usdt': float(pos['unrealizedPnl']) if pos else 0.0,
                'quote_currency': self.get_quote_currency(),
                'is_spot': self.is_upbit_mode(),
                # Signal ?꾩슜 ?꾨뱶
                'kalman_enabled': False,
                'kalman_velocity': 0.0,
                'kalman_direction': None,
                'active_strategy': active_strategy,
                'entry_mode': entry_mode,
                'entry_reason': self.last_entry_reason.get(symbol, '대기'),
                'stateful_diag': self.last_stateful_diag.get(symbol, {}),
                'runtime_diag': self.ctrl.get_runtime_diag(),
                # MicroVBO ?꾩슜 ?꾨뱶
                'vbo_breakout_level': vbo_state.get('breakout_level'),
                'vbo_entry_atr': vbo_state.get('entry_atr'),
                # FractalFisher ?꾩슜 ?꾨뱶
                'fisher_hurst': fisher_state.get('hurst'),
                'fisher_value': fisher_state.get('value'),
                'fisher_trailing_stop': fisher_state.get('trailing_stop'),
                'fisher_entry_atr': fisher_state.get('entry_atr')
            }

            symbol_status['entry_filters'] = {}
            symbol_status['exit_filters'] = {}
            symbol_status['filter_config'] = {}
            utbot_filter_pack_status = dict(self.last_utbot_filter_pack_status.get(symbol, {}) or {})
            utbot_filter_pack_status.setdefault('config', self._get_utbot_filter_pack(strategy_params))
            symbol_status['utbot_filter_pack'] = utbot_filter_pack_status
            utbot_rsi_momentum_status = dict(self.last_utbot_rsi_momentum_filter_status.get(symbol, {}) or {})
            utbot_rsi_momentum_status.setdefault('config', self._get_utbot_rsi_momentum_filter_config(strategy_params))
            symbol_status['utbot_rsi_momentum_filter'] = utbot_rsi_momentum_status
            candidate_status = self.last_utsmc_candidate_filter_status.get(symbol, {})
            symbol_status['utsmc_candidate_filter_mode'] = candidate_status.get('mode_label', 'OFF')
            symbol_status['utsmc_candidate_filter'] = candidate_status
            symbol_status['utbot_filtered_breakout'] = self.last_utbot_filtered_breakout_status.get(symbol, {})
            tp_master_enabled = False if self.is_upbit_mode() else bool(comm_cfg.get('tp_sl_enabled', True))
            tp_enabled = tp_master_enabled and bool(comm_cfg.get('take_profit_enabled', True))
            sl_enabled = tp_master_enabled and bool(comm_cfg.get('stop_loss_enabled', True))
            expected_tp, expected_sl = self._protection_expected_from_config(symbol, pos)
            runner_state = self._get_utbreakout_trailing_state(symbol)
            if active_strategy in UTBREAKOUT_STRATEGIES and pos and not isinstance(runner_state, dict):
                runner_state = await self._recover_utbreakout_trailing_state_from_exchange(
                    symbol,
                    pos,
                    strategy_params,
                )
            if (
                active_strategy in UTBREAKOUT_STRATEGIES
                and pos
                and isinstance(runner_state, dict)
                and (runner_state.get('planned_tp_orders') or runner_state.get('tp_orders'))
            ):
                protection_audit = await self._audit_and_repair_live_ladder_protection(
                    symbol,
                    pos,
                    runner_state,
                    self._get_utbot_filtered_breakout_config(strategy_params),
                    reason='UTBreak periodic protection audit',
                )
            else:
                protection_audit = await self._audit_protection_orders(
                    symbol,
                    pos=pos,
                    expected_tp=expected_tp,
                    expected_sl=expected_sl,
                    alert=True
                )
            symbol_status['protection_config'] = {
                'tp_enabled': tp_enabled,
                'sl_enabled': sl_enabled,
                'tp_expected': expected_tp,
                'sl_expected': expected_sl,
                'tp_present': protection_audit.get('tp_present', False),
                'sl_present': protection_audit.get('sl_present', False),
                'tp_count': protection_audit.get('tp_count', 0),
                'sl_count': protection_audit.get('sl_count', 0),
                'missing_tp': protection_audit.get('missing_tp', False),
                'missing_sl': protection_audit.get('missing_sl', False),
                'orphan_cancelled': protection_audit.get('orphan_cancelled', 0),
                'duplicate_cancelled': protection_audit.get('duplicate_cancelled', 0),
                'invalid_price_cancelled': protection_audit.get('invalid_price_cancelled', 0),
                'audit_status': protection_audit.get('status', 'UNKNOWN')
            }

            # [New] Status Display Enhancement
            symbol_status['leverage'] = comm_cfg.get('leverage', 1 if self.is_upbit_mode() else 20)
            symbol_status['margin_mode'] = 'SPOT' if self.is_upbit_mode() else 'ISOLATED'
            utbreakout_status = self.last_utbot_filtered_breakout_status.get(symbol, {}) or {}
            if active_strategy in UTBREAKOUT_STRATEGIES and utbreakout_status.get('entry_timeframe'):
                symbol_status['entry_tf'] = utbreakout_status.get('entry_timeframe')
                symbol_status['exit_tf'] = utbreakout_status.get('exit_timeframe') or self._get_exit_timeframe(symbol)
            else:
                symbol_status['entry_tf'] = comm_cfg.get('entry_timeframe', comm_cfg.get('timeframe', '8h'))
                symbol_status['exit_tf'] = comm_cfg.get('exit_timeframe', '4h')

            self.ctrl.status_data[symbol] = symbol_status

            # MMR 寃쎄퀬 泥댄겕
            await self.check_mmr_alert(mmr)

            return pos_side

        except Exception as e:
            logger.error(f"Signal check_status error: {e}")

    def _reset_vbo_state(self):
        """MicroVBO ?곹깭 珥덇린??"""
        self.vbo_entry_price = None
        self.vbo_entry_atr = None

    def _reset_fisher_state(self):
        """FractalFisher ?곹깭 珥덇린??"""
        self.fisher_entry_price = None
        self.fisher_entry_atr = None
        self.fisher_trailing_stop = None

    def _update_stateful_diag(self, symbol, **kwargs):
        current = dict(self.last_stateful_diag.get(symbol, {}))
        if 'smc_reason' in kwargs:
            next_reason = kwargs.get('smc_reason')
            prev_reason = current.get('smc_reason')
            if next_reason:
                if next_reason != prev_reason:
                    reason_now = datetime.now()
                    current['smc_reason_ts'] = int(reason_now.timestamp())
                    current['smc_reason_ts_human'] = reason_now.strftime('%m-%d %H:%M:%S')
            else:
                current.pop('smc_reason_ts', None)
                current.pop('smc_reason_ts_human', None)
        current.update(kwargs)
        self.last_stateful_diag[symbol] = current

    async def _notify_stateful_diag(self, symbol, force=False):
        reporting_cfg = self.cfg.get('telegram', {}).get('reporting', {})
        if not reporting_cfg.get('periodic_reports_enabled', False):
            return
        if not reporting_cfg.get('stateful_diag_enabled', False):
            return
        info = self.last_stateful_diag.get(symbol)
        if not info:
            return

        payload = "|".join([
            str(info.get('stage', '')),
            str(info.get('strategy', '')),
            str(info.get('raw_state', '')),
            str(info.get('raw_signal', '')),
            str(info.get('entry_sig', '')),
            str(info.get('pos_side', '')),
            str(info.get('ut_key', '')),
            str(info.get('ut_atr', '')),
            str(info.get('ut_ha', '')),
            str(info.get('utsmc_candidate_filter_mode', '')),
            str(info.get('utsmc_candidate_final_long_pass', '')),
            str(info.get('utsmc_candidate_final_short_pass', '')),
            str(info.get('ut_pending_source', '')),
            str(info.get('ut_pending_side', '')),
            str(info.get('ut_pending_state', '')),
            str(info.get('ut_pending_execute_ts_human', '')),
            str(info.get('ut_flip_target_side', '')),
            str(info.get('ut_flip_retry_count', '')),
            str(info.get('note', '')),
        ])
        now = time.time()
        prev = self.last_stateful_diag_notice.get(symbol, {})
        if not force and prev.get('payload') == payload and (now - float(prev.get('ts', 0.0) or 0.0)) < 180:
            return

        self.last_stateful_diag_notice[symbol] = {'payload': payload, 'ts': now}
        lines = [
            f"🧪 UT 진단 {symbol}",
            f"stage={info.get('stage', '?')} strategy={info.get('strategy', '?')}",
            f"raw_state={info.get('raw_state', 'none')} raw_signal={info.get('raw_signal', 'none')}",
            f"entry_sig={info.get('entry_sig', 'none')} pos={info.get('pos_side', 'NONE')}",
        ]
        if info.get('ut_key') is not None:
            lines.append(
                f"ut=K{info.get('ut_key')} ATR{info.get('ut_atr')} HA={info.get('ut_ha')}"
            )
        if info.get('src') is not None and info.get('stop') is not None:
            lines.append(
                f"src={float(info.get('src')):.4f} stop={float(info.get('stop')):.4f}"
            )
        if info.get('signal_ts_human') or info.get('feed_last_ts_human'):
            lines.append(
                f"tf={info.get('tf_used', '?')} signal_ts={info.get('signal_ts_human', '?')} feed_last={info.get('feed_last_ts_human', '?')}"
            )
        pending_visible = any([
            info.get('ut_pending_source'),
            info.get('ut_pending_side'),
            info.get('ut_pending_state'),
            info.get('ut_pending_execute_ts_human')
        ])
        if pending_visible:
            pending_label = (
                f"{info.get('ut_pending_source')}:{info.get('ut_pending_side', '-')}"
                if info.get('ut_pending_source') else
                f"{info.get('ut_pending_side', '-')}"
            )
            pending_line = (
                f"pending={pending_label}"
                f" state={info.get('ut_pending_state', '-')}"
                f" execute={info.get('ut_pending_execute_ts_human', '-')}"
            )
            if info.get('ut_pending_expires_ts_human'):
                pending_line += f" expires={info.get('ut_pending_expires_ts_human')}"
            lines.append(pending_line)
        if info.get('ut_flip_target_side'):
            flip_line = (
                f"flip_target={info.get('ut_flip_target_side')} "
                f"signal={info.get('ut_flip_signal_ts_human', '-')}"
            )
            if info.get('ut_flip_retry_count') is not None:
                flip_line += f" retry={info.get('ut_flip_retry_count')}"
            if info.get('ut_flip_block_reason'):
                flip_line += f" block={info.get('ut_flip_block_reason')}"
            lines.append(flip_line)
        if info.get('utsmc_candidate_filter_mode'):
            lines.append(
                f"candidate={info.get('utsmc_candidate_filter_mode')} "
                f"long={'PASS' if info.get('utsmc_candidate_final_long_pass') else 'FAIL'} "
                f"short={'PASS' if info.get('utsmc_candidate_final_short_pass') else 'FAIL'}"
            )
        if info.get('closed_ohlc_text'):
            lines.append(f"closed={info.get('closed_ohlc_text')}")
        if info.get('live_ohlc_text'):
            lines.append(f"live={info.get('live_ohlc_text')}")
        note = info.get('note')
        if note:
            lines.append(f"note={note}")
        await self.ctrl.notify("\n".join(lines))

    def _fmt_signal_trade_value(self, value, digits=2, signed=False):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return '-'
        prefix = '+' if signed and number >= 0 else ''
        quote_currency = self.get_quote_currency()
        if str(quote_currency).upper() == 'KRW':
            return f"{prefix}{number:,.0f}"
        return f"{prefix}{number:.{digits}f}"

    def _append_signal_diag_lines(self, lines, diag, *, include_exit=False):
        if not isinstance(diag, dict) or not diag:
            return

        if diag.get('strategy'):
            lines.append(
                f"진단: stage `{diag.get('stage', '-')}` | raw `{diag.get('raw_state', '-')}` | "
                f"entry `{diag.get('entry_sig', '-')}` | pos `{diag.get('pos_side', '-')}`"
            )
        if diag.get('ut_key') is not None:
            lines.append(
                f"UT 설정: K `{diag.get('ut_key')}` | ATR `{diag.get('ut_atr')}` | HA `{diag.get('ut_ha', '-')}`"
            )
        if diag.get('src') is not None and diag.get('stop') is not None:
            lines.append(
                f"UT 값: src `{self._fmt_signal_trade_value(diag.get('src'))}` | "
                f"stop `{self._fmt_signal_trade_value(diag.get('stop'))}`"
            )
        if diag.get('signal_ts_human') or diag.get('feed_last_ts_human'):
            lines.append(
                f"UT 기준봉: tf `{diag.get('tf_used', '?')}` | signal `{diag.get('signal_ts_human', '?')}` | "
                f"feed_last `{diag.get('feed_last_ts_human', '?')}`"
            )
        if (
            diag.get('signal_open') is not None or diag.get('signal_high') is not None
            or diag.get('signal_low') is not None or diag.get('signal_close') is not None
        ):
            lines.append(
                f"UT 기준값: O `{self._fmt_signal_trade_value(diag.get('signal_open'))}` | "
                f"H `{self._fmt_signal_trade_value(diag.get('signal_high'))}` | "
                f"L `{self._fmt_signal_trade_value(diag.get('signal_low'))}` | "
                f"C `{self._fmt_signal_trade_value(diag.get('signal_close'))}`"
            )
        elif diag.get('closed_ohlc_text'):
            lines.append(f"UT 확정봉: `{diag.get('closed_ohlc_text')}`")
        pending_visible = any([
            diag.get('ut_pending_source'),
            diag.get('ut_pending_side'),
            diag.get('ut_pending_state'),
            diag.get('ut_pending_execute_ts_human')
        ])
        if pending_visible:
            pending_line = "UT pending: "
            if diag.get('ut_pending_source'):
                pending_line += f"source `{diag.get('ut_pending_source')}` | "
            pending_line += (
                f"side `{diag.get('ut_pending_side', '-')}` | "
                f"state `{diag.get('ut_pending_state', '-')}` | "
                f"execute `{diag.get('ut_pending_execute_ts_human', '-')}`"
            )
            if diag.get('ut_pending_expires_ts_human'):
                pending_line += f" | expires `{diag.get('ut_pending_expires_ts_human')}`"
            lines.append(pending_line)
        if diag.get('ut_flip_target_side'):
            flip_line = (
                f"UTBOT flip: target `{diag.get('ut_flip_target_side')}` | "
                f"signal `{diag.get('ut_flip_signal_ts_human', '-')}`"
            )
            if diag.get('ut_flip_execute_ts_human'):
                flip_line += f" | execute `{diag.get('ut_flip_execute_ts_human')}`"
            if diag.get('ut_flip_expires_ts_human'):
                flip_line += f" | expires `{diag.get('ut_flip_expires_ts_human')}`"
            if diag.get('ut_flip_retry_count') is not None:
                flip_line += f" | retry `{diag.get('ut_flip_retry_count')}`"
            lines.append(flip_line)
        if diag.get('ut_flip_block_reason'):
            lines.append(f"UTBOT flip 차단사유: `{diag.get('ut_flip_block_reason')}`")

        if diag.get('utsmc_entry_smc_reason'):
            lines.append(f"UTSMC 진입판정: `{diag.get('utsmc_entry_smc_reason')}`")
        if diag.get('utsmc_signal_high') is not None or diag.get('utsmc_signal_low') is not None:
            lines.append(
                f"UTSMC invalidation: high `{self._fmt_signal_trade_value(diag.get('utsmc_signal_high'))}` | "
                f"low `{self._fmt_signal_trade_value(diag.get('utsmc_signal_low'))}`"
            )
        if (
            diag.get('utsmc_entry_bullish_ob_entry') is not None
            or diag.get('utsmc_entry_bearish_ob_entry') is not None
            or diag.get('utsmc_entry_ob_tags')
        ):
            smc_entry_bull = 'Y' if diag.get('utsmc_entry_bullish_ob_entry') else 'N'
            smc_entry_bear = 'Y' if diag.get('utsmc_entry_bearish_ob_entry') else 'N'
            smc_entry_tf = diag.get('utsmc_entry_tf')
            line = f"UTSMC entry OB{f'[{smc_entry_tf}]' if smc_entry_tf else ''}: bull `{smc_entry_bull}` | bear `{smc_entry_bear}`"
            if diag.get('utsmc_entry_ob_tags'):
                line += f" | tags `{diag.get('utsmc_entry_ob_tags')}`"
            lines.append(line)
        if (
            diag.get('utsmc_entry_bullish_ob_bottom') is not None
            or diag.get('utsmc_entry_bullish_ob_top') is not None
            or diag.get('utsmc_entry_bearish_ob_bottom') is not None
            or diag.get('utsmc_entry_bearish_ob_top') is not None
        ):
            lines.append(
                f"UTSMC entry OB range: "
                f"bull `{self._fmt_signal_trade_value(diag.get('utsmc_entry_bullish_ob_bottom'))}` ~ "
                f"`{self._fmt_signal_trade_value(diag.get('utsmc_entry_bullish_ob_top'))}` | "
                f"bear `{self._fmt_signal_trade_value(diag.get('utsmc_entry_bearish_ob_bottom'))}` ~ "
                f"`{self._fmt_signal_trade_value(diag.get('utsmc_entry_bearish_ob_top'))}`"
            )
        if diag.get('utsmc_candidate_filter_mode'):
            c1_long = 'PASS' if diag.get('utsmc_candidate1_long_pass') else 'FAIL'
            c2_long = 'PASS' if diag.get('utsmc_candidate2_long_pass') else 'FAIL'
            final_long = 'PASS' if diag.get('utsmc_candidate_final_long_pass') else 'FAIL'
            c1_short = 'PASS' if diag.get('utsmc_candidate1_short_pass') else 'FAIL'
            c2_short = 'PASS' if diag.get('utsmc_candidate2_short_pass') else 'FAIL'
            final_short = 'PASS' if diag.get('utsmc_candidate_final_short_pass') else 'FAIL'
            lines.append(
                f"UTSMC 후보필터: mode `{diag.get('utsmc_candidate_filter_mode')}` | "
                f"LONG `C1 {c1_long} / C2 {c2_long} / FINAL {final_long}` | "
                f"SHORT `C1 {c1_short} / C2 {c2_short} / FINAL {final_short}`"
            )
            if diag.get('utsmc_candidate_reason_long'):
                lines.append(f"UTSMC 후보필터 LONG: `{diag.get('utsmc_candidate_reason_long')}`")
            if diag.get('utsmc_candidate_reason_short'):
                lines.append(f"UTSMC 후보필터 SHORT: `{diag.get('utsmc_candidate_reason_short')}`")

        if diag.get('utbreakout_reason') or diag.get('utbreakout_reject_code'):
            lines.append(
                f"Filtered Breakout: `{diag.get('utbreakout_reject_code') or 'ACCEPTED_ENTRY'}` | "
                f"`{diag.get('utbreakout_reason', '-')}`"
            )
        if diag.get('utbreakout_htf'):
            lines.append(f"HTF 필터: `{diag.get('utbreakout_htf')}`")
        if diag.get('utbreakout_metrics'):
            lines.append(f"지표 필터: `{diag.get('utbreakout_metrics')}`")
        if diag.get('utbreakout_risk'):
            lines.append(f"리스크 계획: `{diag.get('utbreakout_risk')}`")

        if include_exit:
            if (
                diag.get('utsmc_fixed_ob_side')
                or diag.get('utsmc_fixed_ob_bottom') is not None
                or diag.get('utsmc_fixed_ob_top') is not None
            ):
                lines.append(
                    f"UTSMC 고정 청산 OB: side `{diag.get('utsmc_fixed_ob_side', '-')}` | "
                    f"range `{self._fmt_signal_trade_value(diag.get('utsmc_fixed_ob_bottom'))}` ~ "
                    f"`{self._fmt_signal_trade_value(diag.get('utsmc_fixed_ob_top'))}`"
                )
            if diag.get('utsmc_fixed_ob_created_ts_human') or diag.get('utsmc_fixed_ob_origin_ts_human'):
                lines.append(
                    f"UTSMC 고정 OB 시각: created `{diag.get('utsmc_fixed_ob_created_ts_human', '-')}` | "
                    f"origin `{diag.get('utsmc_fixed_ob_origin_ts_human', '-')}`"
                )
            if diag.get('exit_reason_text'):
                lines.append(f"청산판정: `{diag.get('exit_reason_text')}`")
            if diag.get('exit_trigger_kind'):
                lines.append(f"청산트리거: `{diag.get('exit_trigger_kind')}`")
            if diag.get('exit_closed_ts_human') or diag.get('exit_tf'):
                lines.append(
                    f"청산봉: tf `{diag.get('exit_tf', '?')}` | close_ts `{diag.get('exit_closed_ts_human', '?')}`"
                )
            if diag.get('exit_closed_ohlc_text'):
                lines.append(f"청산 확정봉: `{diag.get('exit_closed_ohlc_text')}`")
            elif (
                diag.get('exit_closed_open') is not None or diag.get('exit_closed_high') is not None
                or diag.get('exit_closed_low') is not None or diag.get('exit_closed_close') is not None
            ):
                lines.append(
                    f"청산봉 값: O `{self._fmt_signal_trade_value(diag.get('exit_closed_open'))}` | "
                    f"H `{self._fmt_signal_trade_value(diag.get('exit_closed_high'))}` | "
                    f"L `{self._fmt_signal_trade_value(diag.get('exit_closed_low'))}` | "
                    f"C `{self._fmt_signal_trade_value(diag.get('exit_closed_close'))}`"
                )
            if (
                diag.get('smc_bullish_ob_entry') is not None
                or diag.get('smc_bearish_ob_entry') is not None
                or diag.get('smc_ob_tags')
            ):
                smc_bull = 'Y' if diag.get('smc_bullish_ob_entry') else 'N'
                smc_bear = 'Y' if diag.get('smc_bearish_ob_entry') else 'N'
                smc_tf = diag.get('smc_tf')
                line = f"UTSMC exit OB{f'[{smc_tf}]' if smc_tf else ''}: bull `{smc_bull}` | bear `{smc_bear}`"
                if diag.get('smc_ob_tags'):
                    line += f" | tags `{diag.get('smc_ob_tags')}`"
                lines.append(line)
            if diag.get('smc_reason'):
                lines.append(f"UTSMC exit 판정: `{diag.get('smc_reason')}`")
            if diag.get('smc_reason_ts_human'):
                lines.append(f"UTSMC exit 판정시각: `{diag.get('smc_reason_ts_human')}`")
            if (
                diag.get('smc_bullish_ob_bottom') is not None
                or diag.get('smc_bullish_ob_top') is not None
                or diag.get('smc_bearish_ob_bottom') is not None
                or diag.get('smc_bearish_ob_top') is not None
            ):
                lines.append(
                    f"UTSMC exit OB range: "
                    f"bull `{self._fmt_signal_trade_value(diag.get('smc_bullish_ob_bottom'))}` ~ "
                    f"`{self._fmt_signal_trade_value(diag.get('smc_bullish_ob_top'))}` | "
                    f"bear `{self._fmt_signal_trade_value(diag.get('smc_bearish_ob_bottom'))}` ~ "
                    f"`{self._fmt_signal_trade_value(diag.get('smc_bearish_ob_top'))}`"
                )

        note = diag.get('note')
        if note:
            lines.append(f"진단메모: `{note}`")

    def _build_signal_entry_notice(
        self,
        symbol,
        side,
        qty,
        requested_price,
        actual_entry_price,
        entry_plan=None,
        leverage=None,
        target_notional=None,
        margin_to_use=None,
        execution_snapshot=None
    ):
        display_symbol = self.ctrl.format_symbol_for_display(symbol)
        diag = dict(self.last_stateful_diag.get(symbol, {}) or {})
        strategy_name = str(self.get_runtime_strategy_params().get('active_strategy', 'utbot') or 'utbot').upper()
        cfg = self.get_runtime_common_settings()

        lines = [
            f"✅ [Signal Entry] {display_symbol} `{str(side).upper()}`",
            f"전략: `{strategy_name}` | 수량 `{qty}`",
            f"주문가: `{self._fmt_signal_trade_value(requested_price)}` | 체결가: `{self._fmt_signal_trade_value(actual_entry_price)}`",
            f"TF: 진입 `{(entry_plan or {}).get('entry_timeframe') or cfg.get('entry_timeframe', cfg.get('timeframe', '15m'))}` / 청산 `{(entry_plan or {}).get('exit_timeframe') or self._get_exit_timeframe(symbol)}`",
            f"네트워크: `{self.ctrl.get_network_status_label()}` | 거래소 `{self.ctrl.get_exchange_display_name()}`",
        ]
        entry_reason = self.last_entry_reason.get(symbol)
        if entry_reason:
            lines.append(f"진입근거: `{entry_reason}`")
        if strategy_name in {name.upper() for name in STRATEGY_DISPLAY_NAMES.values() if name.startswith('UTBOT_')} and isinstance(entry_plan, dict):
            try:
                lev = float(leverage or cfg.get('leverage', 1) or 1)
                notional = float(target_notional) if target_notional is not None else float(qty) * float(actual_entry_price)
                margin = float(margin_to_use) if margin_to_use is not None else notional / max(lev, 1e-9)
                risk_usdt = float(entry_plan.get('risk_usdt', 0.0) or 0.0)
                risk_distance = float(entry_plan.get('risk_distance', 0.0) or 0.0)
                rr_multiple = float(entry_plan.get('rr_multiple', 2.0) or 2.0)
                take_profit_distance = risk_distance * rr_multiple
                expected_profit = risk_usdt * rr_multiple
                risk_pct = risk_distance / max(abs(float(actual_entry_price)), 1e-9) * 100.0
                take_profit_pct = take_profit_distance / max(abs(float(actual_entry_price)), 1e-9) * 100.0
                lines.append(
                    f"진입금액: `증거금 {margin:.2f} USDT / 포지션 {notional:.2f} USDT / {lev:.0f}x`"
                )
                if entry_plan.get('micro_auto'):
                    lines.append(
                        f"Micro Auto: `equity cap {float(entry_plan.get('equity_for_micro', 0) or 0):.2f} / "
                        f"minNotional {float(entry_plan.get('min_notional_usdt', 0) or 0):.2f} / "
                        f"수수료부담 {float(entry_plan.get('fee_burden_pct', 0) or 0):.1f}%`"
                    )
                lines.append(
                    f"손절계획: `거리 {risk_distance:.4f} ({risk_pct:.3f}%) / 손실 {risk_usdt:.2f} USDT`"
                )
                lines.append(
                    f"익절계획: `{rr_multiple:.1f}R / 거리 {take_profit_distance:.4f} "
                    f"({take_profit_pct:.3f}%) / 예상수익 {expected_profit:.2f} USDT`"
                )
                snapshot = execution_snapshot if isinstance(execution_snapshot, dict) else {}
                if snapshot:
                    lines.append(
                        "리스크 계획: "
                        f"`requested_risk={self._fmt_signal_trade_value(snapshot.get('planned_risk'))} USDT / "
                        f"planned_qty={self._fmt_signal_trade_value(snapshot.get('planned_qty'))} / "
                        f"final_order_qty={self._fmt_signal_trade_value(snapshot.get('final_order_qty'))} / "
                        f"filled_qty={self._fmt_signal_trade_value(snapshot.get('filled_qty'))}`"
                    )
                    lines.append(
                        "실제 체결 기준: "
                        f"`planned_notional={self._fmt_signal_trade_value(snapshot.get('planned_notional'))} USDT / "
                        f"actual_notional={self._fmt_signal_trade_value(snapshot.get('actual_notional'))} USDT / "
                        f"actual_risk_estimate={self._fmt_signal_trade_value(snapshot.get('actual_risk_estimate'))} USDT / "
                        f"qty_limiter={snapshot.get('qty_limiter_reason') or 'none'}`"
                    )
                    if isinstance(snapshot.get("tp_ladder"), dict):
                        lines.extend(self._format_tp_ladder_lines(snapshot.get("tp_ladder")))
            except Exception as e:
                logger.debug(f"UT breakout entry notice sizing detail failed: {e}")
        self._append_signal_diag_lines(lines, diag, include_exit=False)
        return "\n".join(lines)

    def _build_signal_exit_notice(self, symbol, pos, reason, pnl, pnl_pct, exit_price):
        display_symbol = self.ctrl.format_symbol_for_display(symbol)
        diag = dict(self.last_stateful_diag.get(symbol, {}) or {})
        strategy_name = str(self.get_runtime_strategy_params().get('active_strategy', 'utbot') or 'utbot').upper()
        side = str(pos.get('side', 'unknown')).upper()
        entry_price = pos.get('entryPrice')

        lines = [
            f"📊 [Signal Exit] {display_symbol} `{side}`",
            f"전략: `{strategy_name}` | 호출사유 `{reason}`",
            f"PnL: `{self._fmt_signal_trade_value(pnl, signed=True)}` (`{float(pnl_pct):+.2f}%`)",
            f"진입가: `{self._fmt_signal_trade_value(entry_price)}` | 청산가: `{self._fmt_signal_trade_value(exit_price)}`",
        ]
        self._append_signal_diag_lines(lines, diag, include_exit=True)
        return "\n".join(lines)

    async def _handle_utbot_primary_strategy(
        self,
        symbol,
        k,
        pos,
        strategy_name,
        raw_strategy_sig,
        raw_state_sig,
        raw_ut_detail,
        sig,
        filter_pack_entry=None,
        rsi_momentum_entry_eval=None,
        rsi_momentum_exit_eval=None
    ):
        target_sig = raw_state_sig
        entry_sig = sig
        current_ts = int(k.get('t') or 0)
        tf = self.get_runtime_common_settings().get('entry_timeframe', self.get_runtime_common_settings().get('timeframe', '15m'))
        candle_ms = self._timeframe_to_ms(tf)
        timing_mode = self._get_ut_entry_timing_mode()
        timing_label = self._get_ut_entry_timing_label(timing_mode)
        pending = self._get_ut_pending_entry(symbol)
        signal_basis_ts = int(raw_ut_detail.get('signal_ts') or 0)
        signal_basis_side = str(raw_ut_detail.get('signal_side') or '').lower()

        def _fmt_pending_ts(ts):
            ts = int(ts or 0)
            return datetime.fromtimestamp(ts / 1000).strftime('%m-%d %H:%M') if ts else None

        pending_source = self._get_ut_pending_source(pending) if pending else None
        is_flip_pending = self._is_ut_flip_pending(pending) if pending else False

        def _entry_filter_gate(side, *, persistent=False):
            allowed, filter_reason = self._utbot_filter_pack_allows_entry(filter_pack_entry, side)
            if not allowed:
                suffix = " | persistent=Y" if persistent else ""
                self.last_entry_reason[symbol] = (
                    f"{strategy_name} {side.upper()} 진입 필터 차단 ({filter_reason})"
                )
                self._update_stateful_diag(
                    symbol,
                    stage='entry_blocked',
                    strategy=strategy_name,
                    raw_state=(target_sig or 'none'),
                    raw_signal=(raw_strategy_sig or 'none'),
                    entry_sig=(side or 'none'),
                    pos_side='NONE',
                    note=f"utbot filter pack blocked | reason={filter_reason}{suffix}"
                )
                return False, filter_reason

            allowed, filter_reason = self._utbot_rsi_momentum_filter_allows(rsi_momentum_entry_eval, side)
            if allowed:
                return True, filter_reason
            suffix = " | persistent=Y" if persistent else ""
            self.last_entry_reason[symbol] = (
                f"{strategy_name} {side.upper()} 진입 필터 차단 ({filter_reason})"
            )
            self._update_stateful_diag(
                symbol,
                stage='entry_blocked',
                strategy=strategy_name,
                raw_state=(target_sig or 'none'),
                raw_signal=(raw_strategy_sig or 'none'),
                entry_sig=(side or 'none'),
                pos_side='NONE',
                note=f"rsi momentum filter blocked | reason={filter_reason}{suffix}"
            )
            return False, filter_reason

        if self.is_upbit_mode():
            if pos and target_sig == 'short':
                self.last_entry_reason[symbol] = (
                    f"{strategy_name} SELL 상태 감지, 청산은 exit TF `{self._get_exit_timeframe(symbol)}` 확인 후 실행"
                )
            elif not pos and entry_sig == 'long':
                allowed, _ = _entry_filter_gate('long')
                if not allowed:
                    return
                self.last_entry_reason[symbol] = f"{strategy_name} BUY 상태 -> 현물 매수"
                logger.info(f"[{strategy_name}] Upbit spot entry LONG")
                await self.entry(symbol, 'long', float(k['c']))
            elif pos:
                self.last_entry_reason[symbol] = (
                    f"현물 보유 중, 청산은 {strategy_name} exit TF `{self._get_exit_timeframe(symbol)}` 기준"
                )
            elif target_sig == 'short':
                self.last_entry_reason[symbol] = f"{strategy_name} SELL 상태, 현물 대기"
            else:
                self.last_entry_reason[symbol] = f"{strategy_name} BUY 대기"
            return

        if pending and timing_mode == 'next_candle':
            pending_execute_ts = int(pending.get('execute_ts') or 0)
            pending_expires_ts = int(pending.get('expires_ts') or 0)
            if is_flip_pending:
                if (not pos) and pending_expires_ts > 0 and current_ts > pending_expires_ts:
                    self._clear_ut_pending_entry(symbol)
                    pending = None
                    pending_source = None
                    is_flip_pending = False
            elif current_ts > pending_execute_ts:
                self._clear_ut_pending_entry(symbol)
                pending = None
                pending_source = None
                is_flip_pending = False

        if pending and target_sig not in {'long', 'short'}:
            self._clear_ut_pending_entry(symbol)
            pending = None
            pending_source = None
            is_flip_pending = False
        elif pending and target_sig in {'long', 'short'} and target_sig != pending.get('side'):
            self._clear_ut_pending_entry(symbol)
            pending = None
            pending_source = None
            is_flip_pending = False

        raw_flip_detected = pos and target_sig and (
            (pos['side'] == 'long' and target_sig == 'short') or
            (pos['side'] == 'short' and target_sig == 'long')
        )

        exit_tf = self._get_exit_timeframe(symbol)
        flip_on_entry_tf = self._timeframes_equivalent(exit_tf, tf)
        flip_filter_confirmed = True
        flip_filter_reason = 'RSI Momentum Trend filter off'
        if raw_flip_detected and flip_on_entry_tf:
            flip_filter_confirmed, flip_filter_reason = self._utbot_rsi_momentum_filter_allows(
                rsi_momentum_exit_eval,
                str(pos['side']).lower()
            )

        if pos and raw_flip_detected and not flip_on_entry_tf:
            self._clear_ut_pending_entry(symbol)
            flip_label = "반전 신호" if raw_strategy_sig == target_sig else "상태 동기화"
            self.last_entry_reason[symbol] = (
                f"{strategy_name} {flip_label} 감지, 청산은 exit TF `{exit_tf}` 확인 후 실행"
            )
            self._update_stateful_diag(
                symbol,
                stage='exit_wait',
                strategy=strategy_name,
                raw_state=(target_sig or 'none'),
                raw_signal=(raw_strategy_sig or 'none'),
                entry_sig=(entry_sig or 'none'),
                pos_side=str(pos['side']).upper(),
                note=f"{flip_label} detected on entry TF | exit_tf={exit_tf}"
            )
            return
        if pos and raw_flip_detected and flip_on_entry_tf and not flip_filter_confirmed:
            self._clear_ut_pending_entry(symbol)
            flip_label = "반전 신호" if raw_strategy_sig == target_sig else "상태 동기화"
            self.last_entry_reason[symbol] = (
                f"{strategy_name} {flip_label} 감지, RSI Momentum Trend 반전 미확인으로 "
                f"{pos['side'].upper()} 유지 ({flip_filter_reason})"
            )
            self._update_stateful_diag(
                symbol,
                stage='exit_wait',
                strategy=strategy_name,
                raw_state=(target_sig or 'none'),
                raw_signal=(raw_strategy_sig or 'none'),
                entry_sig=(entry_sig or 'none'),
                pos_side=str(pos['side']).upper(),
                note=f"{flip_label} blocked by rsi momentum filter | reason={flip_filter_reason}"
            )
            return
        if pos and raw_flip_detected and flip_on_entry_tf and flip_filter_confirmed:
            flip_label = "반전 신호" if raw_strategy_sig == target_sig else "상태 동기화"
            reentry_allowed = False
            reentry_filter_reason = None
            can_reenter = entry_sig == target_sig
            execute_ts = current_ts + candle_ms if candle_ms > 0 else current_ts
            expires_ts = execute_ts + (candle_ms * 2 if candle_ms > 0 else 0)
            retry_count = int(pending.get('retry_count') or 0) if is_flip_pending and pending.get('side') == target_sig else 0

            if can_reenter:
                reentry_allowed, reentry_filter_reason = _entry_filter_gate(entry_sig)
                if reentry_allowed:
                    self._set_ut_pending_entry(
                        symbol,
                        entry_sig,
                        signal_basis_ts or current_ts,
                        execute_ts,
                        strategy_name=strategy_name,
                        source='flip',
                        expires_ts=expires_ts,
                        signal_side=signal_basis_side or entry_sig,
                        retry_count=retry_count,
                        pending_state='exit_pending'
                    )
                    pending = self._get_ut_pending_entry(symbol)
                    pending_source = self._get_ut_pending_source(pending)
                    is_flip_pending = self._is_ut_flip_pending(pending)
                elif pending and is_flip_pending and pending.get('side') == entry_sig:
                    self._clear_ut_pending_entry(symbol)
                    pending = None
                    pending_source = None
                    is_flip_pending = False
            elif pending and is_flip_pending and pending.get('side') == target_sig:
                self._clear_ut_pending_entry(symbol)
                pending = None
                pending_source = None
                is_flip_pending = False

            self._update_stateful_diag(
                symbol,
                stage='flip_detected',
                strategy=strategy_name,
                raw_state=(target_sig or 'none'),
                raw_signal=(raw_strategy_sig or 'none'),
                entry_sig=(entry_sig or 'none'),
                pos_side=str(pos['side']).upper(),
                ut_pending_source=pending_source,
                ut_pending_side=(target_sig.upper() if can_reenter and reentry_allowed else None),
                ut_pending_state=('exit_pending' if can_reenter and reentry_allowed else None),
                ut_pending_execute_ts=execute_ts if can_reenter and reentry_allowed else None,
                ut_pending_execute_ts_human=_fmt_pending_ts(execute_ts) if can_reenter and reentry_allowed else None,
                ut_pending_expires_ts=expires_ts if can_reenter and reentry_allowed else None,
                ut_pending_expires_ts_human=_fmt_pending_ts(expires_ts) if can_reenter and reentry_allowed else None,
                ut_flip_target_side=target_sig.upper(),
                ut_flip_signal_ts=(signal_basis_ts or current_ts),
                ut_flip_signal_ts_human=_fmt_pending_ts(signal_basis_ts or current_ts),
                ut_flip_execute_ts=execute_ts if can_reenter and reentry_allowed else None,
                ut_flip_execute_ts_human=_fmt_pending_ts(execute_ts) if can_reenter and reentry_allowed else None,
                ut_flip_expires_ts=expires_ts if can_reenter and reentry_allowed else None,
                ut_flip_expires_ts_human=_fmt_pending_ts(expires_ts) if can_reenter and reentry_allowed else None,
                ut_flip_retry_count=retry_count,
                ut_flip_block_reason=(
                    reentry_filter_reason if can_reenter and not reentry_allowed else
                    ('direction_or_timing' if not can_reenter else None)
                ),
                note=(
                    f"{flip_label} | flip pending armed"
                    if can_reenter and reentry_allowed else
                    (
                        f"{flip_label} | re-entry blocked ({reentry_filter_reason})"
                        if can_reenter else
                        f"{flip_label} | waiting state/timing alignment"
                    )
                )
            )
            await self._notify_stateful_diag(symbol)
            logger.info(f"[{strategy_name}] {flip_label}: {pos['side']} -> {target_sig}")
            await self.exit_position(symbol, f"{strategy_name}_Flip")
            await asyncio.sleep(1)
            self.position_cache = None
            check_pos = await self.get_server_position(symbol, use_cache=False)
            if not check_pos:
                self._update_stateful_diag(
                    symbol,
                    stage='flip_exit_done',
                    pos_side='NONE',
                    ut_pending_source=pending_source,
                    ut_pending_side=(target_sig.upper() if can_reenter and reentry_allowed else None),
                    ut_pending_state=('armed' if can_reenter and reentry_allowed else None),
                    ut_pending_execute_ts=execute_ts if can_reenter and reentry_allowed else None,
                    ut_pending_execute_ts_human=_fmt_pending_ts(execute_ts) if can_reenter and reentry_allowed else None,
                    ut_pending_expires_ts=expires_ts if can_reenter and reentry_allowed else None,
                    ut_pending_expires_ts_human=_fmt_pending_ts(expires_ts) if can_reenter and reentry_allowed else None,
                    ut_flip_target_side=target_sig.upper(),
                    ut_flip_signal_ts=(signal_basis_ts or current_ts),
                    ut_flip_signal_ts_human=_fmt_pending_ts(signal_basis_ts or current_ts),
                    ut_flip_execute_ts=execute_ts if can_reenter and reentry_allowed else None,
                    ut_flip_execute_ts_human=_fmt_pending_ts(execute_ts) if can_reenter and reentry_allowed else None,
                    ut_flip_expires_ts=expires_ts if can_reenter and reentry_allowed else None,
                    ut_flip_expires_ts_human=_fmt_pending_ts(expires_ts) if can_reenter and reentry_allowed else None,
                    ut_flip_retry_count=retry_count,
                    ut_flip_block_reason=(
                        reentry_filter_reason if can_reenter and not reentry_allowed else
                        ('direction_or_timing' if not can_reenter else None)
                    ),
                    note=f"{flip_label} exit complete"
                )
                if can_reenter and reentry_allowed:
                    self._touch_ut_pending_entry(symbol, pending_state='armed', retry_count=retry_count, block_reason=None)
                    self.last_entry_reason[symbol] = (
                        f"{strategy_name} {flip_label} -> {entry_sig.upper()} {timing_label} 대기"
                    )
                    self._update_stateful_diag(
                        symbol,
                        stage='flip_reentry_armed',
                        entry_sig=entry_sig,
                        pos_side='NONE',
                        ut_pending_source='flip',
                        ut_pending_side=entry_sig.upper(),
                        ut_pending_state='armed',
                        ut_pending_execute_ts=execute_ts,
                        ut_pending_execute_ts_human=_fmt_pending_ts(execute_ts),
                        ut_pending_expires_ts=expires_ts,
                        ut_pending_expires_ts_human=_fmt_pending_ts(expires_ts),
                        ut_flip_target_side=entry_sig.upper(),
                        ut_flip_signal_ts=(signal_basis_ts or current_ts),
                        ut_flip_signal_ts_human=_fmt_pending_ts(signal_basis_ts or current_ts),
                        ut_flip_execute_ts=execute_ts,
                        ut_flip_execute_ts_human=_fmt_pending_ts(execute_ts),
                        ut_flip_expires_ts=expires_ts,
                        ut_flip_expires_ts_human=_fmt_pending_ts(expires_ts),
                        ut_flip_retry_count=retry_count,
                        ut_flip_block_reason=None,
                        note=f"re-entry armed | execute_ts={execute_ts} | expires_ts={expires_ts} | mode={timing_mode}"
                    )
                else:
                    self._clear_ut_pending_entry(symbol)
                    self.last_entry_reason[symbol] = (
                        f"{strategy_name} {flip_label} 청산 완료, 재진입은 "
                        f"{'필터 차단' if can_reenter else '방향/타이밍 미정렬'}"
                    )
                    self._update_stateful_diag(
                        symbol,
                        stage='entry_blocked' if can_reenter else 'flip_exit_only',
                        pos_side='NONE',
                        ut_pending_source=None,
                        ut_pending_side=None,
                        ut_pending_state=None,
                        ut_pending_execute_ts=None,
                        ut_pending_execute_ts_human=None,
                        ut_pending_expires_ts=None,
                        ut_pending_expires_ts_human=None,
                        ut_flip_target_side=target_sig.upper(),
                        ut_flip_signal_ts=(signal_basis_ts or current_ts),
                        ut_flip_signal_ts_human=_fmt_pending_ts(signal_basis_ts or current_ts),
                        ut_flip_execute_ts=None,
                        ut_flip_execute_ts_human=None,
                        ut_flip_expires_ts=None,
                        ut_flip_expires_ts_human=None,
                        ut_flip_retry_count=retry_count,
                        ut_flip_block_reason=(
                            reentry_filter_reason if can_reenter and not reentry_allowed else 'direction_or_timing'
                        ),
                        note=(
                            f"flip re-entry blocked | reason={reentry_filter_reason}"
                            if can_reenter else
                            're-entry blocked by direction or timing'
                        )
                    )
                    await self._notify_stateful_diag(symbol)
            else:
                if can_reenter and reentry_allowed:
                    retry_count += 1
                    self._touch_ut_pending_entry(
                        symbol,
                        retry_count=retry_count,
                        pending_state='waiting_flat',
                        block_reason=None
                    )
                self.last_entry_reason[symbol] = f"{strategy_name} {flip_label} 청산 미확인, 상태 재동기화 재시도 대기"
                self.last_stateful_retry_ts[symbol] = 0.0
                self._update_stateful_diag(
                    symbol,
                    stage='flip_still_open',
                    pos_side=str(check_pos['side']).upper(),
                    ut_pending_source=('flip' if can_reenter and reentry_allowed else None),
                    ut_pending_side=(target_sig.upper() if can_reenter and reentry_allowed else None),
                    ut_pending_state=('waiting_flat' if can_reenter and reentry_allowed else None),
                    ut_pending_execute_ts=execute_ts if can_reenter and reentry_allowed else None,
                    ut_pending_execute_ts_human=_fmt_pending_ts(execute_ts) if can_reenter and reentry_allowed else None,
                    ut_pending_expires_ts=expires_ts if can_reenter and reentry_allowed else None,
                    ut_pending_expires_ts_human=_fmt_pending_ts(expires_ts) if can_reenter and reentry_allowed else None,
                    ut_flip_target_side=target_sig.upper(),
                    ut_flip_signal_ts=(signal_basis_ts or current_ts),
                    ut_flip_signal_ts_human=_fmt_pending_ts(signal_basis_ts or current_ts),
                    ut_flip_execute_ts=execute_ts if can_reenter and reentry_allowed else None,
                    ut_flip_execute_ts_human=_fmt_pending_ts(execute_ts) if can_reenter and reentry_allowed else None,
                    ut_flip_expires_ts=expires_ts if can_reenter and reentry_allowed else None,
                    ut_flip_expires_ts_human=_fmt_pending_ts(expires_ts) if can_reenter and reentry_allowed else None,
                    ut_flip_retry_count=retry_count,
                    ut_flip_block_reason=(
                        reentry_filter_reason if can_reenter and not reentry_allowed else
                        ('direction_or_timing' if not can_reenter else None)
                    ),
                    note=(
                        f"position still open after exit attempt | retry_count={retry_count}"
                        if can_reenter and reentry_allowed else
                        'position still open after exit attempt'
                    )
                )
            return
        elif not pos and entry_sig:
            allowed, filter_reason = _entry_filter_gate(entry_sig)
            if not allowed:
                return
            execute_ts = current_ts + candle_ms if candle_ms > 0 else current_ts
            self._set_ut_pending_entry(
                symbol,
                entry_sig,
                signal_basis_ts or current_ts,
                execute_ts,
                strategy_name=strategy_name
            )
            self.last_entry_reason[symbol] = (
                f"{strategy_name} {entry_sig.upper()} 신호 확정, {timing_label} 대기"
            )
            self._update_stateful_diag(
                symbol,
                stage='entry_armed',
                strategy=strategy_name,
                raw_state=(target_sig or 'none'),
                raw_signal=(raw_strategy_sig or 'none'),
                entry_sig=entry_sig,
                pos_side='NONE',
                note=f"pending {timing_mode} entry | execute_ts={execute_ts}"
            )
        elif not pos and timing_mode == 'persistent' and target_sig in {'long', 'short'}:
            if not self.is_trade_direction_allowed(target_sig):
                self._clear_ut_pending_entry(symbol)
                self.last_entry_reason[symbol] = (
                    f"{strategy_name} 현재 {target_sig.upper()} 상태지만 "
                    f"{self.format_trade_direction_block_reason(target_sig)}"
                )
                self._update_stateful_diag(
                    symbol,
                    stage='entry_blocked',
                    strategy=strategy_name,
                    raw_state=(target_sig or 'none'),
                    raw_signal=(raw_strategy_sig or 'none'),
                    entry_sig=target_sig,
                    pos_side='NONE',
                    note="persistent entry blocked by direction filter"
                )
                return
            if signal_basis_side == target_sig and signal_basis_ts > 0:
                allowed, filter_reason = _entry_filter_gate(target_sig, persistent=True)
                if not allowed:
                    self.last_entry_reason[symbol] = (
                        f"{strategy_name} 현재 {target_sig.upper()} 상태지만 진입 필터 차단 ({filter_reason})"
                    )
                    return
                execute_ts = current_ts + candle_ms if candle_ms > 0 else current_ts
                self._set_ut_pending_entry(
                    symbol,
                    target_sig,
                    signal_basis_ts,
                    execute_ts,
                    strategy_name=strategy_name
                )
                self.last_entry_reason[symbol] = (
                    f"{strategy_name} 현재 유지 중인 {target_sig.upper()} 상태 확인, 마지막 UT 시그널 확정봉 기준 {timing_label} 즉시 대기"
                )
                self._update_stateful_diag(
                    symbol,
                    stage='entry_armed',
                    strategy=strategy_name,
                    raw_state=(target_sig or 'none'),
                    raw_signal=(raw_strategy_sig or 'none'),
                    entry_sig=target_sig,
                    pos_side='NONE',
                    note=(
                        f"persistent state entry | execute_ts={execute_ts} | "
                        f"signal_ts={signal_basis_ts} | source=last_signal_candle"
                    )
                )
            else:
                self.last_entry_reason[symbol] = (
                    f"{strategy_name} 현재 상태는 {target_sig.upper()}지만 기준이 될 마지막 UT 시그널 확정봉 대기"
                )
        elif not pos and target_sig:
            if pending and timing_mode == 'persistent' and pending.get('side') == target_sig:
                self.last_entry_reason[symbol] = (
                    f"{strategy_name} 예약된 {target_sig.upper()} {timing_label} 대기"
                )
            else:
                self.last_entry_reason[symbol] = (
                    f"{strategy_name} 현재 상태는 {target_sig.upper()}지만 fresh signal 대기"
                )
        elif pos:
            if pending and self._is_ut_flip_pending(pending) and pending.get('side') in {'long', 'short'} and pending.get('side') != str(pos['side']).lower():
                self.last_entry_reason[symbol] = (
                    f"{strategy_name} flip {str(pending.get('side')).upper()} 대기 "
                    f"(현재 {pos['side'].upper()} 청산 확인 중)"
                )
            else:
                self._clear_ut_pending_entry(symbol)
                self.last_entry_reason[symbol] = (
                    f"포지션 보유 중 ({pos['side'].upper()}), {strategy_name} 청산은 exit TF `{exit_tf}` 기준"
                )

    async def _handle_utsmc_primary_strategy(self, symbol, k, pos, strategy_name, raw_strategy_sig, raw_state_sig, raw_smc_detail, sig):
        target_sig = raw_state_sig
        ut_signal = raw_strategy_sig if raw_strategy_sig in {'long', 'short'} else None
        current_ts = int(k.get('t') or 0)
        tf = self.get_runtime_common_settings().get('entry_timeframe', self.get_runtime_common_settings().get('timeframe', '15m'))
        candle_ms = self._timeframe_to_ms(tf)
        timing_mode = self._get_ut_entry_timing_mode()
        timing_label = self._get_ut_entry_timing_label(timing_mode)
        pending = self._get_utsmc_pending_entry(symbol)
        ut_detail = raw_smc_detail.get('ut_detail') or {}
        ut_signal_high = ut_detail.get('signal_high')
        ut_signal_low = ut_detail.get('signal_low')
        ut_signal_ts = int(ut_detail.get('signal_ts') or 0)
        ut_signal_side = str(ut_detail.get('signal_side') or '').lower()
        smc_reason = raw_smc_detail.get('smc_reason') or raw_smc_detail.get('smc_internal_ob_reason')
        ob_tags = raw_smc_detail.get('ob_tags') or []
        ob_tag_text = ", ".join(ob_tags) if ob_tags else "-"
        bullish_ob_entry = bool(raw_smc_detail.get('bullish_internal_ob_entry'))
        bearish_ob_entry = bool(raw_smc_detail.get('bearish_internal_ob_entry'))

        if pending and timing_mode == 'next_candle' and current_ts > int(pending.get('execute_ts') or 0):
            self._clear_utsmc_pending_entry(symbol)
            pending = None

        if pos:
            self._clear_utsmc_pending_entry(symbol)
            self.utsmc_last_entry_signal_ts[symbol] = int(self.utsmc_last_entry_signal_ts.get(symbol, 0) or 0)
            if pos['side'] == 'long':
                hold_note = "SMC internal bearish OB 진입 마감 청산 대기"
                if bearish_ob_entry:
                    hold_note = f"SMC internal bearish OB 감지({ob_tag_text}), exit TF 청산 대기"
            else:
                hold_note = "SMC internal bullish OB 진입 마감 청산 대기"
                if bullish_ob_entry:
                    hold_note = f"SMC internal bullish OB 감지({ob_tag_text}), exit TF 청산 대기"
            self.last_entry_reason[symbol] = f"포지션 보유 중 ({pos['side'].upper()}), {hold_note}"
            return

        if pending and target_sig not in {'long', 'short'}:
            self._clear_utsmc_pending_entry(symbol)
            self.last_entry_reason[symbol] = f"{strategy_name} UT 상태 해제, fresh UT 신호 재대기"
            return

        if pending and target_sig in {'long', 'short'} and target_sig != pending.get('side'):
            self._clear_utsmc_pending_entry(symbol)
            pending = None

        if pending and ut_signal and ut_signal != pending.get('side'):
            if not self.is_trade_direction_allowed(ut_signal):
                self._clear_utsmc_pending_entry(symbol)
                self.last_entry_reason[symbol] = (
                    f"{strategy_name} UT {ut_signal.upper()} "
                    f"{self.format_trade_direction_block_reason(ut_signal)}"
                )
                self._update_stateful_diag(
                    symbol,
                    stage='entry_blocked',
                    strategy=strategy_name,
                    raw_state=(target_sig or 'none'),
                    raw_signal=(ut_signal or 'none'),
                    entry_sig=(ut_signal or 'none'),
                    pos_side='NONE',
                    note="candidate replacement blocked by direction filter"
                )
                return
            allowed, candidate_reason = self._utsmc_candidate_filter_allows(raw_smc_detail, ut_signal, is_persistent=False)
            if not allowed:
                self._clear_utsmc_pending_entry(symbol)
                self.last_entry_reason[symbol] = (
                    f"{strategy_name} UT {ut_signal.upper()} 후보 필터 차단 "
                    f"({raw_smc_detail.get('candidate_filter_mode_label', 'OFF')} | {candidate_reason})"
                )
                self._update_stateful_diag(
                    symbol,
                    stage='entry_blocked',
                    strategy=strategy_name,
                    raw_state=(target_sig or 'none'),
                    raw_signal=(ut_signal or 'none'),
                    entry_sig=(ut_signal or 'none'),
                    pos_side='NONE',
                    note=(
                        f"candidate filter blocked | mode={raw_smc_detail.get('candidate_filter_mode_label', 'OFF')} | "
                        f"reason={candidate_reason}"
                    )
                )
                return
            execute_ts = current_ts + candle_ms if candle_ms > 0 else current_ts
            self._set_utsmc_pending_entry(
                symbol,
                ut_signal,
                ut_signal_ts or raw_smc_detail.get('ut_signal_ts'),
                execute_ts,
                signal_high=ut_signal_high,
                signal_low=ut_signal_low
            )
            self.last_entry_reason[symbol] = (
                f"{strategy_name} 기존 {pending.get('side', '').upper()} 예약 취소, "
                f"새 UT {ut_signal.upper()} 신호로 {timing_label} 대기"
            )
            self._update_stateful_diag(
                symbol,
                stage='entry_armed',
                strategy=strategy_name,
                raw_state=(target_sig or 'none'),
                raw_signal=(ut_signal or 'none'),
                entry_sig=(ut_signal or 'none'),
                pos_side='NONE',
                note=f"pending replaced | execute_ts={execute_ts} | mode={timing_mode} | ob={smc_reason or '-'}"
            )
            return

        if ut_signal in {'long', 'short'}:
            if not self.is_trade_direction_allowed(ut_signal):
                self._clear_utsmc_pending_entry(symbol)
                self.last_entry_reason[symbol] = (
                    f"{strategy_name} UT {ut_signal.upper()} "
                    f"{self.format_trade_direction_block_reason(ut_signal)}"
                )
                self._update_stateful_diag(
                    symbol,
                    stage='entry_blocked',
                    strategy=strategy_name,
                    raw_state=(target_sig or 'none'),
                    raw_signal=(ut_signal or 'none'),
                    entry_sig=(ut_signal or 'none'),
                    pos_side='NONE',
                    note="fresh entry blocked by direction filter"
                )
                return
            allowed, candidate_reason = self._utsmc_candidate_filter_allows(raw_smc_detail, ut_signal, is_persistent=False)
            if not allowed:
                self.last_entry_reason[symbol] = (
                    f"{strategy_name} UT {ut_signal.upper()} 후보 필터 차단 "
                    f"({raw_smc_detail.get('candidate_filter_mode_label', 'OFF')} | {candidate_reason})"
                )
                self._update_stateful_diag(
                    symbol,
                    stage='entry_blocked',
                    strategy=strategy_name,
                    raw_state=(target_sig or 'none'),
                    raw_signal=(ut_signal or 'none'),
                    entry_sig=(ut_signal or 'none'),
                    pos_side='NONE',
                    note=(
                        f"candidate filter blocked | mode={raw_smc_detail.get('candidate_filter_mode_label', 'OFF')} | "
                        f"reason={candidate_reason}"
                    )
                )
                return
            execute_ts = current_ts + candle_ms if candle_ms > 0 else current_ts
            self._set_utsmc_pending_entry(
                symbol,
                ut_signal,
                ut_signal_ts or raw_smc_detail.get('ut_signal_ts'),
                execute_ts,
                signal_high=ut_signal_high,
                signal_low=ut_signal_low
            )
            self.last_entry_reason[symbol] = (
                f"{strategy_name} UT {ut_signal.upper()} 확정, {timing_label} 대기"
            )
            self._update_stateful_diag(
                symbol,
                stage='entry_armed',
                strategy=strategy_name,
                raw_state=(target_sig or 'none'),
                raw_signal=(ut_signal or 'none'),
                entry_sig=(ut_signal or 'none'),
                pos_side='NONE',
                note=f"pending {timing_mode} entry | execute_ts={execute_ts} | ob={smc_reason or '-'}"
            )
            return

        if timing_mode == 'persistent' and target_sig in {'long', 'short'}:
            if not self.is_trade_direction_allowed(target_sig):
                self._clear_utsmc_pending_entry(symbol)
                self.last_entry_reason[symbol] = (
                    f"{strategy_name} 현재 UT {target_sig.upper()} 상태지만 "
                    f"{self.format_trade_direction_block_reason(target_sig)}"
                )
                self._update_stateful_diag(
                    symbol,
                    stage='entry_blocked',
                    strategy=strategy_name,
                    raw_state=(target_sig or 'none'),
                    raw_signal=(ut_signal or 'none'),
                    entry_sig=target_sig,
                    pos_side='NONE',
                    note="persistent entry blocked by direction filter"
                )
                return
            if ut_signal_side == target_sig and ut_signal_ts > 0:
                allowed, candidate_reason = self._utsmc_candidate_filter_allows(raw_smc_detail, target_sig, is_persistent=True)
                if not allowed:
                    self.last_entry_reason[symbol] = (
                        f"{strategy_name} 현재 UT {target_sig.upper()} 상태지만 후보 필터 차단 "
                        f"({raw_smc_detail.get('candidate_filter_mode_label', 'OFF')} | {candidate_reason})"
                    )
                    self._update_stateful_diag(
                        symbol,
                        stage='entry_blocked',
                        strategy=strategy_name,
                        raw_state=(target_sig or 'none'),
                        raw_signal=(ut_signal or 'none'),
                        entry_sig=target_sig,
                        pos_side='NONE',
                        note=(
                            f"candidate filter blocked | mode={raw_smc_detail.get('candidate_filter_mode_label', 'OFF')} | "
                            f"reason={candidate_reason} | persistent=Y"
                        )
                    )
                    return
                execute_ts = current_ts + candle_ms if candle_ms > 0 else current_ts
                self._set_utsmc_pending_entry(
                    symbol,
                    target_sig,
                    ut_signal_ts,
                    execute_ts,
                    signal_high=ut_signal_high,
                    signal_low=ut_signal_low
                )
                self.last_entry_reason[symbol] = (
                    f"{strategy_name} 현재 유지 중인 UT {target_sig.upper()} 상태 확인, 마지막 UT 시그널 확정봉 기준 {timing_label} 즉시 대기"
                )
                self._update_stateful_diag(
                    symbol,
                    stage='entry_armed',
                    strategy=strategy_name,
                    raw_state=(target_sig or 'none'),
                    raw_signal=(ut_signal or 'none'),
                    entry_sig=target_sig,
                    pos_side='NONE',
                    note=(
                        f"persistent state entry | execute_ts={execute_ts} | signal_ts={ut_signal_ts} | "
                        f"signal_high={ut_signal_high} | signal_low={ut_signal_low} | "
                        f"source=last_signal_candle | ob={smc_reason or '-'}"
                    )
                )
            else:
                self.last_entry_reason[symbol] = (
                    f"{strategy_name} 현재 UT 상태는 {target_sig.upper()}지만 기준이 될 마지막 UT 시그널 확정봉 대기"
                )
            return

        if pending:
            if timing_mode == 'persistent' and target_sig == pending.get('side'):
                self.last_entry_reason[symbol] = (
                    f"{strategy_name} 예약된 {str(pending.get('side', '')).upper()} {timing_label} 대기 "
                    f"(armed ts={int(pending.get('execute_ts') or 0)}, UT 상태 유지 중)"
                )
            else:
                self.last_entry_reason[symbol] = (
                    f"{strategy_name} 예약된 {str(pending.get('side', '')).upper()} {timing_label} 대기"
                )
            return

        if target_sig in {'long', 'short'}:
            self.last_entry_reason[symbol] = (
                f"{strategy_name} 현재 UT 상태는 {target_sig.upper()}지만 fresh UT 신호 재대기"
            )

    async def _handle_ut_hybrid_primary_strategy(self, symbol, k, pos, strategy_name, active_strategy, raw_strategy_sig, raw_state_sig, raw_hybrid_detail, sig):
        timing_label = raw_hybrid_detail.get('timing_label', UT_HYBRID_TIMING_LABELS.get(active_strategy, 'signal'))
        regime_sig = raw_state_sig
        entry_sig = sig
        fresh_entry_sig = self._remember_ut_strategy_signal_state(symbol, active_strategy, entry_sig)
        current_ts = int(k.get('t') or 0)
        tf = self.get_runtime_common_settings().get('entry_timeframe', self.get_runtime_common_settings().get('timeframe', '15m'))
        candle_ms = self._timeframe_to_ms(tf)
        timing_mode = self._get_ut_entry_timing_mode()
        entry_mode_label = self._get_ut_entry_timing_label(timing_mode)
        pending = self._get_ut_pending_entry(symbol)

        if pending and timing_mode == 'next_candle' and current_ts > int(pending.get('execute_ts') or 0):
            self._clear_ut_pending_entry(symbol)
            pending = None

        if pending and (entry_sig not in {'long', 'short'} or entry_sig != pending.get('side')):
            self._clear_ut_pending_entry(symbol)
            pending = None

        need_flip = pos and regime_sig and (
            (pos['side'] == 'long' and regime_sig == 'short') or
            (pos['side'] == 'short' and regime_sig == 'long')
        )

        if pos:
            self._clear_ut_pending_entry(symbol)
            exit_tf = self._get_exit_timeframe(symbol)
            if need_flip:
                self.last_entry_reason[symbol] = (
                    f"{strategy_name} UT {regime_sig.upper()} 상태 감지, 청산은 exit TF `{exit_tf}` 확인 후 실행"
                )
                self._update_stateful_diag(
                    symbol,
                    stage='exit_wait',
                    strategy=strategy_name,
                    raw_state=(regime_sig or 'none'),
                    raw_signal=(raw_strategy_sig or 'none'),
                    entry_sig=(entry_sig or 'none'),
                    pos_side=str(pos['side']).upper(),
                    note=f"UT regime flip detected on entry TF | exit_tf={exit_tf}"
                )
            else:
                self.last_entry_reason[symbol] = (
                    f"포지션 보유 중 ({pos['side'].upper()}), {strategy_name} 청산은 exit TF `{exit_tf}` 기준"
                )
            return
        elif not pos and fresh_entry_sig:
            execute_ts = current_ts + candle_ms if candle_ms > 0 else current_ts
            self._set_ut_pending_entry(
                symbol,
                fresh_entry_sig,
                current_ts,
                execute_ts,
                strategy_name=strategy_name,
                hybrid_strategy=active_strategy
            )
            self.last_entry_reason[symbol] = (
                f"{strategy_name} 조건 충족 -> {fresh_entry_sig.upper()} {entry_mode_label} 대기"
            )
            self._update_stateful_diag(
                symbol,
                stage='entry_armed',
                strategy=strategy_name,
                raw_state=(regime_sig or 'none'),
                raw_signal=(raw_strategy_sig or 'none'),
                entry_sig=fresh_entry_sig,
                pos_side='NONE',
                note=f"pending {timing_mode} entry | execute_ts={execute_ts} | timing={timing_label}"
            )
        elif not pos and timing_mode == 'persistent' and entry_sig in {'long', 'short'}:
            execute_ts = current_ts + candle_ms if candle_ms > 0 else current_ts
            self._set_ut_pending_entry(
                symbol,
                entry_sig,
                current_ts,
                execute_ts,
                strategy_name=strategy_name,
                hybrid_strategy=active_strategy
            )
            self.last_entry_reason[symbol] = (
                f"{strategy_name} 현재 유지 중인 {entry_sig.upper()} 조건 확인 -> {entry_mode_label} 즉시 대기"
            )
            self._update_stateful_diag(
                symbol,
                stage='entry_armed',
                strategy=strategy_name,
                raw_state=(regime_sig or 'none'),
                raw_signal=(raw_strategy_sig or 'none'),
                entry_sig=entry_sig,
                pos_side='NONE',
                note=f"persistent state entry | execute_ts={execute_ts} | timing={timing_label}"
            )
        elif not pos and pending and timing_mode == 'persistent' and entry_sig == pending.get('side'):
            self.last_entry_reason[symbol] = (
                f"{strategy_name} 예약된 {entry_sig.upper()} {entry_mode_label} 대기"
            )
        elif not pos and regime_sig:
            self.last_entry_reason[symbol] = (
                f"{strategy_name} UT {regime_sig.upper()} 상태, {timing_label} 타이밍 대기"
            )

    async def _handle_rsibb_primary_strategy(self, symbol, k, pos, strategy_name, raw_strategy_sig, sig):
        guard_ok, guard_reason = self._rsibb_runtime_guard(self.get_runtime_strategy_params())
        if not guard_ok:
            self.last_entry_reason[symbol] = guard_reason
            return
        if pos and raw_strategy_sig and (
            (pos['side'] == 'long' and raw_strategy_sig == 'short') or
            (pos['side'] == 'short' and raw_strategy_sig == 'long')
        ):
            logger.info(f"[{strategy_name}] Flip trigger: {pos['side']} -> {raw_strategy_sig}")
            await self.exit_position(symbol, f"{strategy_name}_Flip")
            await asyncio.sleep(1)
            self.position_cache = None
            check_pos = await self.get_server_position(symbol, use_cache=False)
            if not check_pos:
                if sig == raw_strategy_sig:
                    self.last_entry_reason[symbol] = f"{strategy_name} 반전 신호 -> {sig.upper()} 재진입"
                    await self.entry(symbol, sig, float(k['c']))
                else:
                    self.last_entry_reason[symbol] = f"{strategy_name} 반전 신호로 청산 완료, 재진입은 필터 또는 방향 설정으로 차단"
            else:
                logger.warning(f"[{strategy_name}] Flip re-entry skipped: position still open ({check_pos['side']})")
        elif not pos and sig:
            self.last_entry_reason[symbol] = f"{strategy_name} 조건 충족 -> {sig.upper()} 진입"
            logger.info(f"[{strategy_name}] New entry {sig.upper()}")
            await self.entry(symbol, sig, float(k['c']))
        elif pos:
            self.last_entry_reason[symbol] = f"포지션 보유 중 ({pos['side'].upper()}), {strategy_name} 반대신호 대기"

    def _build_utbb_long_exit_note(self, *, ut_signal=None, ut_state=None, bb_reentry_down=False):
        if ut_signal == 'short':
            return "UT short signal"
        if ut_state == 'short':
            return "UT short state"
        if bb_reentry_down:
            return "BB upper reentry down"
        return "BB middle above two bearish candles"

    def _build_utbb_short_exit_note(self, *, ut_signal=None, ut_state=None, special_short_mode=None):
        if ut_signal == 'long':
            return 'short exit by UT long signal'
        if ut_state == 'long':
            return 'short exit by UT long state'
        if special_short_mode == 'mid_rebound_fail':
            return 'short exit by BB middle reentry up'
        if special_short_mode == 'lower_reentry':
            return 'short exit by BB lower reentry up'
        return 'short exit by BB lower breakout'

    def _build_utbb_hold_note(self, strategy_name, pos_side, *, special_long_active=False, special_short_active=False, special_short_mode=None):
        if pos_side == 'long' and special_long_active:
            return "특수 LONG 하향돌파 청산 대기"
        if pos_side == 'long':
            return "LONG 청산 조건 대기 (UT 숏상태 또는 BB 상단 하향 돌파 또는 중간선 위 음봉 2개)"
        if pos_side == 'short' and special_short_mode == 'mid_rebound_fail':
            return "특수 SHORT 중간선 상향돌파 청산 대기"
        if pos_side == 'short' and special_short_active:
            return "특수 SHORT 하단 상향돌파 청산 대기"
        if pos_side == 'short':
            return f"{strategy_name} 청산 조건 대기 (UT 롱 또는 BB 하단 돌파)"
        return f"{strategy_name} 청산 조건 대기"

    def _evaluate_utbb_exit_context(self, symbol, raw_hybrid_detail, current_side):
        current_side = str(current_side or '').lower()
        ut_signal = raw_hybrid_detail.get('ut_signal')
        ut_state = raw_hybrid_detail.get('ut_state')
        bb_reentry_down = bool(raw_hybrid_detail.get('bb_reentry_down'))
        bb_mid_two_bearish_exit = bool(raw_hybrid_detail.get('bb_mid_two_bearish_exit'))
        bb_reentry_up = bool(raw_hybrid_detail.get('bb_reentry_up'))
        bb_mid_reentry_up = bool(raw_hybrid_detail.get('bb_mid_reentry_up'))
        bb_lower_breakout = bool(raw_hybrid_detail.get('bb_lower_breakout'))
        special_long_active = self._is_utbb_special_long_active(symbol)
        special_short_active = self._is_utbb_special_short_active(symbol)
        special_short_mode = self._get_utbb_special_short_mode(symbol)

        result = {
            'raw_exit_long': False,
            'raw_exit_short': False,
            'exit_note': None,
            'hold_note': self._build_utbb_hold_note(
                'UTBB',
                current_side,
                special_long_active=special_long_active,
                special_short_active=special_short_active,
                special_short_mode=special_short_mode
            ),
            'special_long_active': special_long_active,
            'special_short_active': special_short_active,
            'special_short_mode': special_short_mode
        }

        if current_side == 'long' and (
            ut_state == 'short'
            or bb_reentry_down
            or ((not special_long_active) and bb_mid_two_bearish_exit)
        ):
            result['raw_exit_long'] = True
            result['exit_note'] = self._build_utbb_long_exit_note(
                ut_signal=ut_signal,
                ut_state=ut_state,
                bb_reentry_down=bb_reentry_down
            )
            return result

        if current_side == 'short' and (
            ut_state == 'long'
            or ((not special_short_active) and bb_lower_breakout)
            or (special_short_mode == 'lower_reentry' and bb_reentry_up)
            or (special_short_mode == 'mid_rebound_fail' and bb_mid_reentry_up)
        ):
            result['raw_exit_short'] = True
            result['exit_note'] = self._build_utbb_short_exit_note(
                ut_signal=ut_signal,
                ut_state=ut_state,
                special_short_mode=special_short_mode
            )
            return result

        return result

    async def _handle_utbb_primary_strategy(self, symbol, k, pos, strategy_name, raw_strategy_sig, raw_state_sig, raw_hybrid_detail, sig):
        ut_signal = raw_hybrid_detail.get('ut_signal')
        ut_state = raw_hybrid_detail.get('ut_state')
        ut_long_fresh = bool(raw_hybrid_detail.get('ut_long_fresh'))
        bb_long_fresh = bool(raw_hybrid_detail.get('bb_long_fresh'))
        long_pair_source = raw_hybrid_detail.get('utbb_long_pair_source')
        bb_upper_breakout = bool(raw_hybrid_detail.get('bb_upper_breakout'))
        bb_reentry_down = bool(raw_hybrid_detail.get('bb_reentry_down'))
        bb_mid_two_bearish_exit = bool(raw_hybrid_detail.get('bb_mid_two_bearish_exit'))
        bb_reentry_up = bool(raw_hybrid_detail.get('bb_reentry_up'))
        bb_mid_reentry_up = bool(raw_hybrid_detail.get('bb_mid_reentry_up'))
        bb_lower_breakout = bool(raw_hybrid_detail.get('bb_lower_breakout'))
        short_setup_ready = bool(raw_hybrid_detail.get('bb_short_setup_ready'))
        short_mid_ready = bool(raw_hybrid_detail.get('bb_mid_short_ready'))
        short_rebound_fail_ready = bool(raw_hybrid_detail.get('bb_mid_rebound_fail_ready'))
        short_rebound_fail_bull_count = int(raw_hybrid_detail.get('bb_mid_rebound_fail_bull_count') or 0)
        special_short_candidate = bool(raw_hybrid_detail.get('bb_special_short_candidate'))
        entry_sig = sig
        special_long_active = self._is_utbb_special_long_active(symbol)
        special_short_active = self._is_utbb_special_short_active(symbol)
        special_short_mode = self._get_utbb_special_short_mode(symbol)

        if pos:
            exit_tf = self._get_exit_timeframe(symbol)
            exit_ctx = self._evaluate_utbb_exit_context(symbol, raw_hybrid_detail, pos['side'])
            if exit_ctx.get('raw_exit_long') or exit_ctx.get('raw_exit_short'):
                exit_note = exit_ctx.get('exit_note') or 'exit condition'
                self.last_entry_reason[symbol] = (
                    f"{strategy_name} {pos['side'].upper()} 청산 조건 감지 ({exit_note}), "
                    f"청산은 exit TF `{exit_tf}` 확인 후 실행"
                )
                self._update_stateful_diag(
                    symbol,
                    stage='exit_wait',
                    strategy=strategy_name,
                    raw_state=(raw_state_sig or 'none'),
                    raw_signal=(raw_strategy_sig or 'none'),
                    entry_sig=(entry_sig or 'none'),
                    pos_side=str(pos['side']).upper(),
                    note=f"primary exit condition detected | exit_tf={exit_tf} | {exit_note}"
                )
            else:
                hold_note = self._build_utbb_hold_note(
                    strategy_name,
                    pos['side'],
                    special_long_active=special_long_active,
                    special_short_active=special_short_active,
                    special_short_mode=special_short_mode
                )
                self.last_entry_reason[symbol] = (
                    f"포지션 보유 중 ({pos['side'].upper()}), {hold_note} | exit TF `{exit_tf}`"
                )
            return

        if not pos and entry_sig == 'long':
            self.last_entry_reason[symbol] = self._build_utbb_long_entry_reason(
                strategy_name,
                ut_signal=ut_signal,
                long_pair_source=long_pair_source,
                ut_long_fresh=ut_long_fresh,
                bb_long_fresh=bb_long_fresh,
                bb_upper_breakout=bb_upper_breakout
            )
            self._apply_utbb_long_entry_state(symbol, raw_hybrid_detail, bb_upper_breakout)
            self._clear_utbb_long_setup(symbol)
            self._clear_utbb_short_setup(symbol)
            self._clear_utbb_special_short_state(symbol)
            logger.info(f"[{strategy_name}] New LONG entry")
            await self.entry(symbol, 'long', float(k['c']))
            return

        if not pos and entry_sig == 'short':
            short_note = self._build_utbb_short_entry_note(
                ut_signal=ut_signal,
                short_mid_ready=short_mid_ready,
                short_rebound_fail_ready=short_rebound_fail_ready,
                short_rebound_fail_bull_count=short_rebound_fail_bull_count,
                special_short_candidate=special_short_candidate
            )
            self.last_entry_reason[symbol] = f"{strategy_name} {short_note} -> SHORT 진입"
            self._apply_utbb_short_entry_state(
                symbol,
                raw_hybrid_detail,
                special_short_candidate=special_short_candidate,
                short_rebound_fail_ready=short_rebound_fail_ready
            )
            self._clear_utbb_long_setup(symbol)
            self._clear_utbb_special_long_state(symbol)
            logger.info(f"[{strategy_name}] New SHORT entry")
            await self.entry(symbol, 'short', float(k['c']))
            if short_setup_ready:
                self._consume_utbb_short_setup(symbol)
            return

        if not pos and short_setup_ready:
            self._clear_utbb_special_long_state(symbol)
            self._clear_utbb_special_short_state(symbol)
            self.last_entry_reason[symbol] = f"{strategy_name} BB 숏 셋업 저장 중, UT 숏신호 대기"
            return

        if not pos:
            self._clear_utbb_special_long_state(symbol)
            self._clear_utbb_special_short_state(symbol)
            return

    async def process_primary_candle(self, symbol, k, force=False):
        strategy_params = self.get_runtime_strategy_params()
        active_strategy = strategy_params.get('active_strategy', 'utbot').lower()
        if active_strategy in UTBREAKOUT_STRATEGIES:
            symbol = self._canonicalize_utbreakout_symbol_for_use(
                symbol,
                source='process_primary_candle',
            )
        candle_time = k['t']

        # ?щ낵蹂??곹깭 珥덇린??
        if symbol not in self.last_candle_time:
            self.last_candle_time[symbol] = 0
            self.last_candle_success[symbol] = True

        # 以묐났 罹붾뱾 諛⑹? (媛숈? 罹붾뱾 ?ъ쿂由?李⑤떒) - ?깃났??寃쎌슦?먮쭔 ?ㅽ궢
        if (not force) and candle_time <= self.last_candle_time[symbol] and self.last_candle_success[symbol]:
            logger.debug(f"??Skipping duplicate candle: {candle_time} <= {self.last_candle_time[symbol]}")
            return

        processing_candle_time = candle_time
        self.last_signal_check = time.time()
        self.last_candle_success[symbol] = False

        logger.info(f"?빉截?[Signal] Processing candle: {symbol} close={k['c']}")
        if active_strategy in UTBREAKOUT_STRATEGIES:
            self._utbreakout_trace_event(
                symbol,
                'PROCESS_PRIMARY_START',
                'START',
                active_strategy=active_strategy,
                force=force,
                candle_close=k.get('c') if isinstance(k, dict) else None,
                candle_ts=k.get('t') if isinstance(k, dict) else None,
            )

        if await self.check_daily_loss_limit():
            logger.info("??Daily loss limit reached, skipping trade")
            if active_strategy in UTBREAKOUT_STRATEGIES:
                self.last_entry_reason[symbol] = "REJECTED_DAILY_LOSS_LIMIT"
            self.last_candle_time[symbol] = processing_candle_time
            self.last_candle_success[symbol] = True
            return

        try:
            await self.check_status(symbol, float(k['c']))

            # [MODIFIED] Prioritize entry_timeframe for fetching entry OHLCV
            common_cfg = self.get_runtime_common_settings()
            if active_strategy in UTBREAKOUT_STRATEGIES:
                filtered_cfg = self._get_utbot_filtered_breakout_config(strategy_params)
                tf = filtered_cfg.get('entry_timeframe', '15m')
            else:
                tf = common_cfg.get('entry_timeframe', common_cfg.get('timeframe', '15m'))
            ohlcv = await asyncio.to_thread(self.market_data_exchange.fetch_ohlcv, symbol, tf, limit=300)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

            # [CRITICAL] Ensure numeric types (Robust Loop)
            for col in ['open', 'high', 'low', 'close', 'volume']:
                df[col] = pd.to_numeric(df[col], errors='coerce')

            # ===== ?꾨왂 ?ㅼ젙 濡쒕뱶 =====
            strategy_context = self._collect_primary_strategy_context(symbol, df, strategy_params, active_strategy)
            raw_strategy_sig = strategy_context['raw_strategy_sig']
            raw_state_sig = strategy_context['raw_state_sig']
            raw_ut_detail = strategy_context['raw_ut_detail']
            raw_hybrid_detail = strategy_context['raw_hybrid_detail']

            # ?꾨왂蹂??좏샇 怨꾩궛
            sig, is_bullish, is_bearish, strategy_name, entry_mode, kalman_enabled = await self._calculate_strategy_signal(
                symbol,
                df,
                strategy_params,
                active_strategy,
                precomputed=strategy_context['precomputed'],
                force_utbreakout_reprocess=bool(
                    force and active_strategy in UTBREAKOUT_STRATEGIES
                ),
            )
            if active_strategy in UTBREAKOUT_STRATEGIES:
                signal_diag = self._utbreakout_diag_for_symbol(symbol)
                self._utbreakout_trace_event(
                    symbol,
                    'SIGNAL_CALCULATED',
                    'RESULT',
                    sig=sig,
                    reason=(
                        signal_diag.get('reason')
                        or self.last_entry_reason.get(symbol)
                        or ''
                    ),
                    accepted_side=signal_diag.get('accepted_side'),
                    reject_code=signal_diag.get('reject_code'),
                    decision_candle_ts=signal_diag.get('decision_candle_ts'),
                    force=force,
                )
            strategy_sig = sig
            utbot_entry_filter_eval = None
            utbot_rsi_momentum_entry_eval = None
            utbot_rsi_momentum_exit_eval = None
            if active_strategy == 'utbot':
                utbot_entry_filter_eval = await self._evaluate_utbot_filter_pack(
                    symbol,
                    df,
                    strategy_params,
                    tf,
                    'entry'
                )
                self._store_utbot_filter_pack_status(symbol, 'entry', utbot_entry_filter_eval, strategy_params)
                utbot_rsi_momentum_entry_eval = self._evaluate_utbot_rsi_momentum_filter(
                    df,
                    strategy_params,
                    'entry'
                )
                utbot_rsi_momentum_exit_eval = self._evaluate_utbot_rsi_momentum_filter(
                    df,
                    strategy_params,
                    'exit'
                )
                self._store_utbot_rsi_momentum_filter_status(
                    symbol,
                    'entry',
                    utbot_rsi_momentum_entry_eval,
                    strategy_params
                )

            # 留ㅻℓ 諛⑺뼢 ?꾪꽣
            d_mode = self.get_effective_trade_direction()
            if sig and not self.is_trade_direction_allowed(sig):
                logger.info(f"??Signal {sig} blocked by direction filter: {d_mode}")
                self.last_entry_reason[symbol] = self.format_trade_direction_block_reason(sig)
                if active_strategy in UTBREAKOUT_STRATEGIES:
                    self._clear_utbot_filtered_breakout_entry_plan(symbol)
                sig = None

            # ?ъ????뺤씤
            self.position_cache = None
            self.position_cache_time = 0
            pos = await self.get_server_position(symbol, use_cache=False)

            current_side = pos['side'] if pos else 'NONE'
            if not sig and not pos and active_strategy in UTBREAKOUT_STRATEGIES:
                try:
                    decision_ts = int(df.iloc[-2]['timestamp']) if len(df) >= 2 else None
                    recovered_side = self._recover_utbreakout_accepted_side(
                        symbol,
                        decision_ts,
                    )
                    if recovered_side:
                        sig = recovered_side
                        is_bullish = sig == 'long'
                        is_bearish = sig == 'short'
                        logger.warning(
                            "[UTBOT_FILTERED_BREAKOUT_V1] recovered accepted side from "
                            "diagnostic: symbol=%s side=%s",
                            symbol,
                            sig,
                        )
                except Exception:
                    logger.debug(
                        "UTBreakout diagnostic recovery skipped",
                        exc_info=True,
                    )
            logger.info(f"?뱧 Current position: {current_side}, Signal: {sig or 'NONE'}, Mode: {entry_mode}")
            if active_strategy in STATEFUL_UT_STRATEGIES:
                signal_ts = raw_ut_detail.get('signal_ts')
                timing_signal_ts = raw_hybrid_detail.get('timing_signal_ts')
                feed_last_ts = int(df.iloc[-1]['timestamp']) if len(df) >= 1 else None
                closed_row = df.iloc[-2] if len(df) >= 2 else None
                live_row = df.iloc[-1] if len(df) >= 1 else None
                if active_strategy == 'utsmc':
                    ut_pending = self._get_utsmc_pending_entry(symbol)
                    ut_pending_source = None
                    ut_pending_state = 'armed' if ut_pending else None
                else:
                    ut_pending = self._get_ut_pending_entry(symbol)
                    ut_pending_source = self._get_ut_pending_source(ut_pending) if ut_pending else None
                    ut_pending_state = ut_pending.get('pending_state') if ut_pending else None
                ut_pending_side = str(ut_pending.get('side') or '').upper() if ut_pending else None
                ut_pending_execute_ts = int(ut_pending.get('execute_ts') or 0) if ut_pending else 0
                ut_pending_expires_ts = (
                    int(ut_pending.get('expires_ts') or 0)
                    if ut_pending and active_strategy != 'utsmc' else 0
                )

                def _fmt_ohlc(row):
                    if row is None:
                        return None
                    ts = int(row['timestamp'])
                    return (
                        f"{datetime.fromtimestamp(ts / 1000).strftime('%m-%d %H:%M')} "
                        f"O{float(row['open']):.2f} H{float(row['high']):.2f} "
                        f"L{float(row['low']):.2f} C{float(row['close']):.2f}"
                    )

                self._update_stateful_diag(
                    symbol,
                    stage='evaluate',
                    strategy=active_strategy.upper(),
                    raw_state=(raw_state_sig or 'none'),
                    raw_signal=(raw_strategy_sig or 'none'),
                    entry_sig=(sig or 'none'),
                    pos_side=str(current_side).upper(),
                    ut_key=raw_ut_detail.get('key_value'),
                    ut_atr=raw_ut_detail.get('atr_period'),
                    ut_ha='ON' if raw_ut_detail.get('use_heikin_ashi') else 'OFF',
                    src=raw_ut_detail.get('curr_src'),
                    stop=raw_ut_detail.get('curr_stop'),
                    tf_used=tf,
                    signal_ts=signal_ts,
                    signal_ts_human=datetime.fromtimestamp(signal_ts / 1000).strftime('%m-%d %H:%M') if signal_ts else None,
                    signal_open=raw_ut_detail.get('signal_open'),
                    signal_high=raw_ut_detail.get('signal_high'),
                    signal_low=raw_ut_detail.get('signal_low'),
                    signal_close=raw_ut_detail.get('signal_close'),
                    timing_label=raw_hybrid_detail.get('timing_label'),
                    timing_signal=(raw_hybrid_detail.get('timing_signal') or 'none') if raw_hybrid_detail.get('timing_label') else None,
                    effective_timing_signal=(raw_hybrid_detail.get('effective_timing_signal') or 'none') if raw_hybrid_detail.get('timing_label') else None,
                    timing_match_source=raw_hybrid_detail.get('timing_match_source'),
                    timing_signal_ts=timing_signal_ts,
                    timing_signal_ts_human=datetime.fromtimestamp(timing_signal_ts / 1000).strftime('%m-%d %H:%M') if timing_signal_ts else None,
                    timing_reason=raw_hybrid_detail.get('timing_reason'),
                    latched_timing_signal=raw_hybrid_detail.get('latched_timing_signal'),
                    latched_timing_available=raw_hybrid_detail.get('latched_timing_available'),
                    latched_timing_signal_ts=raw_hybrid_detail.get('latched_timing_signal_ts'),
                    latched_timing_signal_ts_human=datetime.fromtimestamp(raw_hybrid_detail.get('latched_timing_signal_ts') / 1000).strftime('%m-%d %H:%M') if raw_hybrid_detail.get('latched_timing_signal_ts') else None,
                    utsmc_entry_bullish_ob_entry=(
                        raw_hybrid_detail.get('bullish_internal_ob_entry') if active_strategy == 'utsmc' else None
                    ),
                    utsmc_entry_bearish_ob_entry=(
                        raw_hybrid_detail.get('bearish_internal_ob_entry') if active_strategy == 'utsmc' else None
                    ),
                    utsmc_entry_ob_tags=(
                        ", ".join(raw_hybrid_detail.get('ob_tags') or [])
                        if active_strategy == 'utsmc' and raw_hybrid_detail.get('ob_tags')
                        else None
                    ),
                    utsmc_entry_bullish_ob_top=(
                        raw_hybrid_detail.get('bullish_ob_top') if active_strategy == 'utsmc' else None
                    ),
                    utsmc_entry_bullish_ob_bottom=(
                        raw_hybrid_detail.get('bullish_ob_bottom') if active_strategy == 'utsmc' else None
                    ),
                    utsmc_entry_bearish_ob_top=(
                        raw_hybrid_detail.get('bearish_ob_top') if active_strategy == 'utsmc' else None
                    ),
                    utsmc_entry_bearish_ob_bottom=(
                        raw_hybrid_detail.get('bearish_ob_bottom') if active_strategy == 'utsmc' else None
                    ),
                    utsmc_entry_smc_signal=(
                        raw_hybrid_detail.get('smc_signal') if active_strategy == 'utsmc' else None
                    ),
                    utsmc_entry_smc_reason=(
                        raw_hybrid_detail.get('smc_reason') if active_strategy == 'utsmc' else None
                    ),
                    utsmc_entry_tf=tf if active_strategy == 'utsmc' else None,
                    utsmc_candidate_filter_mode=(
                        raw_hybrid_detail.get('candidate_filter_mode_label') if active_strategy == 'utsmc' else None
                    ),
                    utsmc_candidate1_long_pass=(
                        raw_hybrid_detail.get('candidate1_long_pass') if active_strategy == 'utsmc' else None
                    ),
                    utsmc_candidate1_short_pass=(
                        raw_hybrid_detail.get('candidate1_short_pass') if active_strategy == 'utsmc' else None
                    ),
                    utsmc_candidate2_long_pass=(
                        raw_hybrid_detail.get('candidate2_long_pass') if active_strategy == 'utsmc' else None
                    ),
                    utsmc_candidate2_short_pass=(
                        raw_hybrid_detail.get('candidate2_short_pass') if active_strategy == 'utsmc' else None
                    ),
                    utsmc_candidate_final_long_pass=(
                        raw_hybrid_detail.get('candidate_final_long_pass') if active_strategy == 'utsmc' else None
                    ),
                    utsmc_candidate_final_short_pass=(
                        raw_hybrid_detail.get('candidate_final_short_pass') if active_strategy == 'utsmc' else None
                    ),
                    utsmc_candidate_reason_long=(
                        raw_hybrid_detail.get('candidate_reason_long') if active_strategy == 'utsmc' else None
                    ),
                    utsmc_candidate_reason_short=(
                        raw_hybrid_detail.get('candidate_reason_short') if active_strategy == 'utsmc' else None
                    ),
                    ut_pending_source=ut_pending_source,
                    ut_pending_side=ut_pending_side,
                    ut_pending_state=ut_pending_state,
                    ut_pending_execute_ts=(ut_pending_execute_ts or None),
                    ut_pending_execute_ts_human=(
                        datetime.fromtimestamp(ut_pending_execute_ts / 1000).strftime('%m-%d %H:%M')
                        if ut_pending_execute_ts else None
                    ),
                    ut_pending_expires_ts=(ut_pending_expires_ts or None),
                    ut_pending_expires_ts_human=(
                        datetime.fromtimestamp(ut_pending_expires_ts / 1000).strftime('%m-%d %H:%M')
                        if ut_pending_expires_ts else None
                    ),
                    ut_flip_target_side=(
                        str(ut_pending.get('side') or '').upper()
                        if ut_pending and ut_pending_source == 'flip' else None
                    ),
                    ut_flip_signal_ts=(
                        int(ut_pending.get('signal_ts') or 0) if ut_pending and ut_pending_source == 'flip' else None
                    ),
                    ut_flip_signal_ts_human=(
                        datetime.fromtimestamp(int(ut_pending.get('signal_ts') or 0) / 1000).strftime('%m-%d %H:%M')
                        if ut_pending and ut_pending_source == 'flip' and int(ut_pending.get('signal_ts') or 0) else None
                    ),
                    ut_flip_execute_ts=(
                        ut_pending_execute_ts if ut_pending and ut_pending_source == 'flip' and ut_pending_execute_ts else None
                    ),
                    ut_flip_execute_ts_human=(
                        datetime.fromtimestamp(ut_pending_execute_ts / 1000).strftime('%m-%d %H:%M')
                        if ut_pending and ut_pending_source == 'flip' and ut_pending_execute_ts else None
                    ),
                    ut_flip_expires_ts=(
                        ut_pending_expires_ts if ut_pending and ut_pending_source == 'flip' and ut_pending_expires_ts else None
                    ),
                    ut_flip_expires_ts_human=(
                        datetime.fromtimestamp(ut_pending_expires_ts / 1000).strftime('%m-%d %H:%M')
                        if ut_pending and ut_pending_source == 'flip' and ut_pending_expires_ts else None
                    ),
                    ut_flip_retry_count=(
                        int(ut_pending.get('retry_count') or 0) if ut_pending and ut_pending_source == 'flip' else None
                    ),
                    ut_flip_block_reason=(
                        ut_pending.get('block_reason') if ut_pending and ut_pending_source == 'flip' else None
                    ),
                    feed_last_ts=feed_last_ts,
                    feed_last_ts_human=datetime.fromtimestamp(feed_last_ts / 1000).strftime('%m-%d %H:%M') if feed_last_ts else None,
                    feed_last_close=float(df.iloc[-1]['close']) if len(df) >= 1 else None,
                    closed_ohlc_text=_fmt_ohlc(closed_row),
                    live_ohlc_text=_fmt_ohlc(live_row),
                    note=f"force={'Y' if force else 'N'} dir={d_mode}"
                )

            # 6.5 Pending Re-entry Check (吏??吏꾩엯)
            # ?댁쟾 罹붾뱾?먯꽌 泥?궛 ???덉빟??吏꾩엯???덈뒗吏 ?뺤씤
            if self.pending_reentry.get(symbol):
                reentry_data = self.pending_reentry[symbol]
                target_ts = reentry_data.get('target_time', 0)
                side = reentry_data.get('side')

                # ?寃?罹붾뱾 ?쒓컙?닿굅??洹??댄썑硫?吏꾩엯
                if candle_time >= target_ts:
                    logger.info(f"??Executing PENDING re-entry for {side.upper()} at {candle_time} (scheduled for {target_ts})")
                    self.pending_reentry.pop(symbol, None) # Reset first to avoid loop

                    if not pos: # ?ъ??섏씠 鍮꾩뼱?덉뼱??吏꾩엯
                        await self.entry(symbol, side, float(k['c']))
                    else:
                        logger.warning(f"?좑툘 Pending entry skipped: Position not empty ({pos['side']})")
                else:
                    logger.info(f"??Waiting for pending re-entry: target={target_ts}, current={candle_time}")
                    # ?꾩쭅 ?쒓컙 ???먯쑝硫??대쾲 ??醫낅즺 (?좉퇋 ?좏샇 臾댁떆)
                    return

            # ===== Kalman 諛⑺뼢 媛?몄삤湲?(李멸퀬?? =====
            kalman_direction = self.kalman_states.get(symbol, {}).get('direction')  # 'long' or 'short' or None

            # ?ㅼ쓬 罹붾뱾 ?쒓컙 怨꾩궛??
            next_candle_ts = candle_time + self._timeframe_to_ms(tf)

            # ===== entry_mode???곕Ⅸ 吏꾩엯 泥섎━ (Exit???쒖쇅) =====
            if active_strategy == 'utbot':
                await self._handle_utbot_primary_strategy(
                    symbol,
                    k,
                    pos,
                    strategy_name,
                    raw_strategy_sig,
                    raw_state_sig,
                    raw_ut_detail,
                    sig,
                    utbot_entry_filter_eval,
                    utbot_rsi_momentum_entry_eval,
                    utbot_rsi_momentum_exit_eval
                )

            elif active_strategy == 'utsmc':
                await self._handle_utsmc_primary_strategy(
                    symbol,
                    k,
                    pos,
                    strategy_name,
                    raw_strategy_sig,
                    raw_state_sig,
                    raw_hybrid_detail,
                    sig
                )

            elif active_strategy == 'utbb':
                await self._handle_utbb_primary_strategy(
                    symbol,
                    k,
                    pos,
                    strategy_name,
                    raw_strategy_sig,
                    raw_state_sig,
                    raw_hybrid_detail,
                    sig
                )

            elif active_strategy in UT_HYBRID_STRATEGIES:
                await self._handle_ut_hybrid_primary_strategy(
                    symbol,
                    k,
                    pos,
                    strategy_name,
                    active_strategy,
                    raw_strategy_sig,
                    raw_state_sig,
                    raw_hybrid_detail,
                    sig
                )

            elif active_strategy == 'rsibb':
                await self._handle_rsibb_primary_strategy(
                    symbol,
                    k,
                    pos,
                    strategy_name,
                    raw_strategy_sig,
                    sig
                )

            elif active_strategy in UTBREAKOUT_STRATEGIES:
                filtered_cfg = self._get_utbot_filtered_breakout_config(strategy_params)
                if pos:
                    trailing_state = self._get_utbreakout_trailing_state(symbol)
                    advanced_ladder = bool(
                        isinstance(trailing_state, dict)
                        and trailing_state.get('advanced_live_ladder_state')
                    )
                    if not advanced_ladder:
                        await self._manage_utbreakout_partial_trailing(symbol, pos, df, filtered_cfg)
                    pyramid_status = await self._maybe_apply_aggressive_growth_pyramiding(symbol, pos, df, filtered_cfg)
                    if isinstance(pyramid_status, dict) and pyramid_status.get('status') == 'ADDED':
                        self.position_cache = None
                        pos = await self.get_server_position(symbol, use_cache=False) or pos
                    if advanced_ladder:
                        await self._manage_live_ladder_exit_policy(symbol, pos, df, filtered_cfg)
                    self.last_entry_reason[symbol] = (
                        f"포지션 보유 중 ({pos['side'].upper()}), UTBOT_FILTERED_BREAKOUT_V1 신규 진입 대기"
                    )
                    self._clear_utbot_filtered_breakout_entry_plan(symbol)
                else:
                    flat_cleanup = await self._manage_live_ladder_exit_policy(
                        symbol,
                        None,
                        df,
                        filtered_cfg,
                    )
                    position_reappeared = (
                        isinstance(flat_cleanup, dict)
                        and flat_cleanup.get('status') == 'POSITION_ACTIVE'
                    )
                    cleanup_pending = (
                        isinstance(flat_cleanup, dict)
                        and flat_cleanup.get('status') == 'FLAT_CLEANUP_PENDING'
                    )
                    if position_reappeared or cleanup_pending:
                        self.last_entry_reason[symbol] = (
                            "포지션 재확인 또는 보호주문 정리 미확인: 신규 진입 보류"
                        )
                        self._clear_utbot_filtered_breakout_entry_plan(symbol)
                    elif sig:
                        accepted_plan = (
                            self._get_utbot_filtered_breakout_entry_plan(symbol, sig)
                            or {}
                        )
                        accepted_decision_ts = self._utbreakout_decision_timestamp(
                            accepted_plan
                        )
                        if self._utbreakout_decision_already_consumed(
                            symbol,
                            accepted_decision_ts,
                        ):
                            self.last_entry_reason[symbol] = (
                                "DECISION_ALREADY_CONSUMED: this closed-candle "
                                "decision already entered once"
                            )
                            self._utbreakout_trace_event(
                                symbol,
                                'ENTRY_BLOCKED',
                                'DECISION_ALREADY_CONSUMED',
                                side=sig,
                                decision_candle_ts=accepted_decision_ts,
                                source='process_primary_candle',
                            )
                            self._clear_utbot_filtered_breakout_entry_plan(symbol)
                            self.last_candle_time[symbol] = processing_candle_time
                            self.last_candle_success[symbol] = True
                            return
                        self._clear_utbreakout_trailing_state(symbol, finalize=True, reason='position closed before new UTBreak signal')
                        self._clear_aggressive_growth_position(symbol)
                        self.last_entry_reason[symbol] = f"ACCEPTED_ENTRY: {sig.upper()} filtered breakout -> 진입"
                        logger.info(f"[UTBOT_FILTERED_BREAKOUT_V1] New {sig.upper()} entry")
                        plan = self._get_utbot_filtered_breakout_entry_plan(symbol, sig) or {}
                        if not plan:
                            logger.warning(
                                "[UTBOT_FILTERED_BREAKOUT_V1] signal exists but entry plan missing before entry(): "
                                "%s %s keys=%s",
                                symbol,
                                sig,
                                list(getattr(self, 'utbot_filtered_breakout_entry_plans', {}).keys())[:20],
                            )
                        entry_ref_price = float(plan.get('entry_price') or k['c'])
                        self._utbreakout_trace_event(
                            symbol,
                            'ENTRY_CALL',
                            'CALLING',
                            side=sig,
                            entry_ref_price=entry_ref_price,
                            source='process_primary_candle',
                        )
                        await self.entry(symbol, sig, entry_ref_price)
                    else:
                        self._clear_utbot_filtered_breakout_entry_plan(symbol)

            elif (active_strategy in MA_STRATEGIES and entry_mode in ['cross', 'position']) or active_strategy == 'cameron':
                # Cross/Position 紐⑤뱶: Primary TF?먯꽌??"吏꾩엯(Entry)"留?泥섎━
                # 泥?궛(Exit)? Secondary TF candle (process_exit_candle)?먯꽌 泥섎━??

                if pos:
                    # ?대? ?ъ??섏씠 ?덉쑝硫?Entry 泥댄겕 ????(Wait for Exit TF signal)
                    # ?? Pending Re-entry???꾩뿉??泥섎━??
                    self.last_entry_reason[symbol] = f"포지션 보유 중 ({pos['side'].upper()}), 진입 대기"
                    logger.debug(f"ProcessPrimary: Position exists ({pos['side']}), waiting for Exit TF signal.")

                elif not pos and sig:
                    # ?ъ????놁쓬 + 吏꾩엯 ?좏샇 -> 吏꾩엯
                    if active_strategy == 'cameron':
                        strategy_label = strategy_name
                    else:
                        strategy_label = "Cross" if entry_mode == 'cross' else "Position"
                    self.last_entry_reason[symbol] = f"{strategy_label} 조건 충족 -> {sig.upper()} 진입"
                    logger.info(f"?? {strategy_label} (Primary): New entry {sig.upper()}")
                    await self.entry(symbol, sig, float(k['c']))

            elif entry_mode in ['hurst_fisher', 'microvbo']:
                # FractalFisher / MicroVBO 紐⑤뱶: 湲곗〈 濡쒖쭅 ?좎? (?⑥씪 TF)
                if not pos and sig:
                    logger.info(f"?? New entry ({entry_mode.upper()}): {sig.upper()}")
                    await self.entry(symbol, sig, float(k['c']))
                elif pos and sig:
                    # ?ъ????덇퀬 諛섎? ?좏샇 ???뚮┰
                    if (pos['side'] == 'long' and sig == 'short') or (pos['side'] == 'short' and sig == 'long'):
                        logger.info(f"?봽 {entry_mode.upper()}: Flip position {pos['side']} ??{sig}")
                        await self.exit_position(symbol, f"{strategy_name}_Flip")
                        await asyncio.sleep(1)
                        self.position_cache = None
                        check_pos = await self.get_server_position(symbol, use_cache=False)
                        if not check_pos:
                            await self.entry(symbol, sig, float(k['c']))

            else:
                logger.debug(f"No action: pos={current_side}, sig={sig}, entry_mode={entry_mode}")

            self.last_candle_time[symbol] = processing_candle_time
            self.last_candle_success[symbol] = True
            logger.debug(f"??Candle processing completed successfully")

        except Exception as e:
            logger.error(f"Signal process_candle error: {e}")
            import traceback
            traceback.print_exc()

    async def _post_ut_secondary_exit(self, symbol, active_strategy, previous_side):
        strategy_key = str(active_strategy or '').lower()
        previous_side = str(previous_side or '').lower()
        if self.is_upbit_mode():
            return
        if strategy_key not in ({'utbot'} | UT_HYBRID_STRATEGIES):
            return

        await asyncio.sleep(1)
        self.position_cache = None
        check_pos = await self.get_server_position(symbol, use_cache=False)
        if check_pos:
            logger.warning(
                f"[{STRATEGY_DISPLAY_NAMES.get(strategy_key, strategy_key.upper())}] "
                f"Secondary exit not yet confirmed for {symbol}: {check_pos['side']}"
            )
            self.last_stateful_retry_ts[symbol] = 0.0
            return

        pending = self._get_ut_pending_entry(symbol)
        pending_source = self._get_ut_pending_source(pending) if pending else None
        is_flip_pending = self._is_ut_flip_pending(pending) if pending else False
        if is_flip_pending:
            pending_side = str(pending.get('side') or '').upper() if pending else None
            execute_ts = int(pending.get('execute_ts') or 0) if pending else 0
            expires_ts = int(pending.get('expires_ts') or 0) if pending else 0
            signal_ts = int(pending.get('signal_ts') or 0) if pending else 0
            retry_count = int(pending.get('retry_count') or 0) if pending else 0
            self._touch_ut_pending_entry(symbol, pending_state='armed', block_reason=None)
            self._update_stateful_diag(
                symbol,
                stage='secondary_exit_confirmed',
                strategy=STRATEGY_DISPLAY_NAMES.get(strategy_key, strategy_key.upper()),
                pos_side='NONE',
                ut_pending_source=pending_source,
                ut_pending_side=pending_side,
                ut_pending_state='armed',
                ut_pending_execute_ts=execute_ts or None,
                ut_pending_execute_ts_human=(
                    datetime.fromtimestamp(execute_ts / 1000).strftime('%m-%d %H:%M')
                    if execute_ts else None
                ),
                ut_pending_expires_ts=expires_ts or None,
                ut_pending_expires_ts_human=(
                    datetime.fromtimestamp(expires_ts / 1000).strftime('%m-%d %H:%M')
                    if expires_ts else None
                ),
                ut_flip_target_side=pending_side,
                ut_flip_signal_ts=signal_ts or None,
                ut_flip_signal_ts_human=(
                    datetime.fromtimestamp(signal_ts / 1000).strftime('%m-%d %H:%M')
                    if signal_ts else None
                ),
                ut_flip_execute_ts=execute_ts or None,
                ut_flip_execute_ts_human=(
                    datetime.fromtimestamp(execute_ts / 1000).strftime('%m-%d %H:%M')
                    if execute_ts else None
                ),
                ut_flip_expires_ts=expires_ts or None,
                ut_flip_expires_ts_human=(
                    datetime.fromtimestamp(expires_ts / 1000).strftime('%m-%d %H:%M')
                    if expires_ts else None
                ),
                ut_flip_retry_count=retry_count,
                ut_flip_block_reason=None,
                note='secondary exit confirmed, flip pending preserved'
            )
        else:
            self._clear_ut_pending_entry(symbol)
        if strategy_key in UT_HYBRID_STRATEGIES:
            self._clear_ut_strategy_signal_state(symbol, strategy_key)
        if strategy_key == 'utbb':
            if previous_side == 'long':
                self._clear_utbb_special_long_state(symbol)
            elif previous_side == 'short':
                self._clear_utbb_special_short_state(symbol)
                self._clear_utbb_special_long_state(symbol)

        self.last_state_sync_candle_ts[symbol] = 0
        self.last_stateful_retry_ts[symbol] = 0.0

    async def process_exit_candle(self, symbol, tf, current_side):
        """[New] Process secondary timeframe candle for EXIT signals
           Applies EXIT filters (Kalman, R2, Chop) independently.
        """
        try:
            # Fetch history for Exit TF
            ohlcv = await asyncio.to_thread(self.market_data_exchange.fetch_ohlcv, symbol, tf, limit=300)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            # [CRITICAL] Ensure numeric types (Robust Loop)
            for col in ['open', 'high', 'low', 'close', 'volume']:
                df[col] = pd.to_numeric(df[col], errors='coerce')

            # ===== 1. Calculate Raw Exit Signal =====
            strategy_params = self.get_runtime_strategy_params()
            active_strategy = strategy_params.get('active_strategy', 'utbot').lower()
            raw_exit_long = False
            raw_exit_short = False
            bypass_exit_filters = False
            c_f = c_s = 0.0

            if active_strategy == 'utsmc':
                strategy_name = "UTSMC(Exit)"
                smc_sig, exit_reason, smc_detail = self._calculate_smc_internal_ob_signal(df, strategy_params)
                candidate_eval_df = df.iloc[:-1].copy() if len(df.index) > 2 else df.copy()
                candidate_detail = self._calculate_utsmc_candidate_filter_state(candidate_eval_df, strategy_params)
                utsmc_cfg = strategy_params.get('UTSMC', {}) or {}
                exit_candidate2_enabled = bool(utsmc_cfg.get('exit_candidate2_enabled', False))
                current_closed_row = df.iloc[-2]
                current_closed_ts = int(current_closed_row['timestamp'])
                current_closed_price = float(current_closed_row['close'])
                current_closed_open = float(current_closed_row['open'])
                current_closed_high = float(current_closed_row['high'])
                current_closed_low = float(current_closed_row['low'])
                exit_candle_ms = self._timeframe_to_ms(tf)
                current_closed_end_ts = (
                    current_closed_ts + exit_candle_ms
                    if exit_candle_ms > 0 else current_closed_ts
                )
                current_closed_end_human = datetime.fromtimestamp(
                    current_closed_end_ts / 1000
                ).strftime('%m-%d %H:%M') if current_closed_end_ts else None
                current_closed_ohlc_text = (
                    f"{datetime.fromtimestamp(current_closed_ts / 1000).strftime('%m-%d %H:%M')} "
                    f"O{current_closed_open:.2f} H{current_closed_high:.2f} "
                    f"L{current_closed_low:.2f} C{current_closed_price:.2f}"
                )
                invalidation = self._get_utsmc_entry_invalidation(symbol) or {}
                invalidation_side = str(invalidation.get('side') or '').lower()
                signal_high = invalidation.get('signal_high')
                signal_low = invalidation.get('signal_low')
                position_side = str(current_side or '').lower()
                entry_anchor_ts = int(self.utsmc_last_entry_signal_ts.get(symbol, 0) or 0)
                required_ob_side = 'bearish' if position_side == 'long' else 'bullish'
                fixed_exit_ob = self._get_utsmc_fixed_exit_ob(symbol)
                if fixed_exit_ob:
                    fixed_position_side = str(fixed_exit_ob.get('position_side') or '').lower()
                    fixed_ob_side = str(fixed_exit_ob.get('ob_side') or '').lower()
                    if fixed_position_side != position_side or fixed_ob_side != required_ob_side:
                        self._clear_utsmc_fixed_exit_ob(symbol)
                        fixed_exit_ob = None

                if not fixed_exit_ob and position_side in {'long', 'short'}:
                    candidate_obs = (
                        list(smc_detail.get('active_bearish_obs') or [])
                        if position_side == 'long'
                        else list(smc_detail.get('active_bullish_obs') or [])
                    )
                    eligible_obs = []
                    for order_block in candidate_obs:
                        created_ts = int(order_block.get('created_ts') or 0)
                        if entry_anchor_ts and created_ts < entry_anchor_ts:
                            continue
                        eligible_obs.append(order_block)
                    if eligible_obs:
                        fixed_candidate = min(
                            eligible_obs,
                            key=lambda ob: (
                                int(ob.get('created_ts') or 0),
                                int(ob.get('origin_ts') or 0)
                            )
                        )
                        self._set_utsmc_fixed_exit_ob(
                            symbol,
                            position_side,
                            required_ob_side,
                            fixed_candidate,
                            tf=tf
                        )
                        fixed_exit_ob = self._get_utsmc_fixed_exit_ob(symbol)

                fixed_ob_side = str((fixed_exit_ob or {}).get('ob_side') or '').lower()
                fixed_ob_top = (fixed_exit_ob or {}).get('top')
                fixed_ob_bottom = (fixed_exit_ob or {}).get('bottom')
                fixed_ob_origin_ts = int((fixed_exit_ob or {}).get('origin_ts') or 0)
                fixed_ob_created_ts = int((fixed_exit_ob or {}).get('created_ts') or 0)
                fixed_ob_origin_human = (
                    datetime.fromtimestamp(fixed_ob_origin_ts / 1000).strftime('%m-%d %H:%M')
                    if fixed_ob_origin_ts else None
                )
                fixed_ob_created_human = (
                    datetime.fromtimestamp(fixed_ob_created_ts / 1000).strftime('%m-%d %H:%M')
                    if fixed_ob_created_ts else None
                )
                fixed_bearish_ob_entry = (
                    position_side == 'long'
                    and fixed_ob_side == 'bearish'
                    and fixed_ob_bottom is not None
                    and fixed_ob_top is not None
                    and float(fixed_ob_bottom) <= current_closed_price <= float(fixed_ob_top)
                )
                fixed_bullish_ob_entry = (
                    position_side == 'short'
                    and fixed_ob_side == 'bullish'
                    and fixed_ob_bottom is not None
                    and fixed_ob_top is not None
                    and float(fixed_ob_bottom) <= current_closed_price <= float(fixed_ob_top)
                )
                ut_invalidation_long = (
                    current_side.lower() == 'long'
                    and invalidation_side in {'', 'long'}
                    and signal_low is not None
                    and current_closed_price <= float(signal_low)
                )
                ut_invalidation_short = (
                    current_side.lower() == 'short'
                    and invalidation_side in {'', 'short'}
                    and signal_high is not None
                    and current_closed_price >= float(signal_high)
                )
                exit_trigger_tags = []
                exit_reason_parts = []
                candidate2_long_exit = bool(
                    exit_candidate2_enabled and candidate_detail.get('candidate2_long_pass', False)
                )
                candidate2_short_exit = bool(
                    exit_candidate2_enabled and candidate_detail.get('candidate2_short_pass', False)
                )
                if current_side.lower() == 'long':
                    if fixed_bearish_ob_entry:
                        exit_trigger_tags.append('fixed_internal_bearish_ob_entry')
                        exit_reason_parts.append(
                            f"FIXED INTERNAL BEARISH OB ENTRY {float(fixed_ob_bottom or 0.0):.4f} ~ "
                            f"{float(fixed_ob_top or 0.0):.4f}"
                        )
                    if ut_invalidation_long:
                        exit_trigger_tags.append('ut_long_invalidation')
                        exit_reason_parts.append(
                            f"UT LONG INVALIDATION close {current_closed_price:.4f} <= signal low {float(signal_low):.4f}"
                        )
                    if candidate2_short_exit:
                        exit_trigger_tags.append('candidate2_short_exit')
                        exit_reason_parts.append(
                            f"OPPOSITE C2 SHORT {candidate_detail.get('candidate2_reason_short')}"
                        )
                    raw_exit_long = bool(exit_reason_parts)
                    raw_exit_short = False
                else:
                    if fixed_bullish_ob_entry:
                        exit_trigger_tags.append('fixed_internal_bullish_ob_entry')
                        exit_reason_parts.append(
                            f"FIXED INTERNAL BULLISH OB ENTRY {float(fixed_ob_bottom or 0.0):.4f} ~ "
                            f"{float(fixed_ob_top or 0.0):.4f}"
                        )
                    if ut_invalidation_short:
                        exit_trigger_tags.append('ut_short_invalidation')
                        exit_reason_parts.append(
                            f"UT SHORT INVALIDATION close {current_closed_price:.4f} >= signal high {float(signal_high):.4f}"
                        )
                    if candidate2_long_exit:
                        exit_trigger_tags.append('candidate2_long_exit')
                        exit_reason_parts.append(
                            f"OPPOSITE C2 LONG {candidate_detail.get('candidate2_reason_long')}"
                        )
                    raw_exit_short = bool(exit_reason_parts)
                    raw_exit_long = False
                if exit_reason_parts:
                    exit_reason = " | ".join(exit_reason_parts)
                elif current_side.lower() == 'long':
                    if fixed_ob_side == 'bearish' and fixed_ob_bottom is not None and fixed_ob_top is not None:
                        exit_reason = (
                            f"FIXED INTERNAL BEARISH OB WAIT {float(fixed_ob_bottom):.4f} ~ "
                            f"{float(fixed_ob_top):.4f}"
                        )
                    else:
                        exit_reason = (
                            f"FIXED INTERNAL BEARISH OB 대기: "
                            f"active_bear_ob={'Y' if smc_detail.get('active_bearish_ob_count', 0) else 'N'}"
                        )
                else:
                    if fixed_ob_side == 'bullish' and fixed_ob_bottom is not None and fixed_ob_top is not None:
                        exit_reason = (
                            f"FIXED INTERNAL BULLISH OB WAIT {float(fixed_ob_bottom):.4f} ~ "
                            f"{float(fixed_ob_top):.4f}"
                        )
                    else:
                        exit_reason = (
                            f"FIXED INTERNAL BULLISH OB 대기: "
                            f"active_bull_ob={'Y' if smc_detail.get('active_bullish_ob_count', 0) else 'N'}"
                        )
                bypass_exit_filters = True
                self._update_stateful_diag(
                    symbol,
                    smc_bullish_ob_entry=fixed_bullish_ob_entry,
                    smc_bearish_ob_entry=fixed_bearish_ob_entry,
                    smc_ob_tags=(
                        ", ".join(exit_trigger_tags)
                        if exit_trigger_tags else
                        (f"fixed_{fixed_ob_side}_ob_locked" if fixed_ob_side else None)
                    ),
                    smc_bullish_ob_top=(float(fixed_ob_top) if fixed_ob_side == 'bullish' and fixed_ob_top is not None else None),
                    smc_bullish_ob_bottom=(float(fixed_ob_bottom) if fixed_ob_side == 'bullish' and fixed_ob_bottom is not None else None),
                    smc_bearish_ob_top=(float(fixed_ob_top) if fixed_ob_side == 'bearish' and fixed_ob_top is not None else None),
                    smc_bearish_ob_bottom=(float(fixed_ob_bottom) if fixed_ob_side == 'bearish' and fixed_ob_bottom is not None else None),
                    smc_signal=smc_sig,
                    smc_reason=exit_reason,
                    smc_tf=tf,
                    utsmc_signal_high=signal_high,
                    utsmc_signal_low=signal_low,
                    utsmc_fixed_ob_side=(fixed_ob_side or None),
                    utsmc_fixed_ob_top=fixed_ob_top,
                    utsmc_fixed_ob_bottom=fixed_ob_bottom,
                    utsmc_fixed_ob_created_ts_human=fixed_ob_created_human,
                    utsmc_fixed_ob_origin_ts_human=fixed_ob_origin_human,
                    utsmc_exit_candidate2_enabled=exit_candidate2_enabled,
                    utsmc_exit_candidate2_long_pass=candidate_detail.get('candidate2_long_pass'),
                    utsmc_exit_candidate2_short_pass=candidate_detail.get('candidate2_short_pass'),
                    utsmc_exit_candidate2_reason_long=candidate_detail.get('candidate2_reason_long'),
                    utsmc_exit_candidate2_reason_short=candidate_detail.get('candidate2_reason_short'),
                    exit_reason_text=exit_reason,
                    exit_trigger_kind=", ".join(exit_trigger_tags) if exit_trigger_tags else None,
                    exit_tf=tf,
                    exit_closed_ts=current_closed_end_ts,
                    exit_closed_ts_human=current_closed_end_human,
                    exit_closed_open=current_closed_open,
                    exit_closed_high=current_closed_high,
                    exit_closed_low=current_closed_low,
                    exit_closed_close=current_closed_price,
                    exit_closed_ohlc_text=current_closed_ohlc_text
                )
                last_utsmc_entry_ts = int(self.utsmc_last_entry_signal_ts.get(symbol, 0) or 0)
                if last_utsmc_entry_ts and current_closed_end_ts <= last_utsmc_entry_ts:
                    raw_exit_long = False
                    raw_exit_short = False
                if raw_exit_long or raw_exit_short:
                    logger.info(f"[Exit Debug] {symbol} {strategy_name}: {exit_reason}")
            elif active_strategy == 'cameron':
                strategy_name = "CAMERON(Exit)"
                exit_sig, exit_reason, _ = self._calculate_cameron_signal(df, strategy_params)
                raw_exit_long = current_side.lower() == 'long' and exit_sig == 'short'
                raw_exit_short = current_side.lower() == 'short' and exit_sig == 'long'
                if raw_exit_long or raw_exit_short:
                    logger.info(f"[Exit Debug] {symbol} {strategy_name}: {exit_reason}")
            elif active_strategy == 'utbot':
                strategy_name = "UTBOT(Exit)"
                exit_sig, exit_reason, utbot_exit_detail = self._calculate_utbot_signal(df, strategy_params)
                exit_state = str(exit_sig or utbot_exit_detail.get('bias_side') or '').lower()
                base_exit_long = current_side.lower() == 'long' and exit_state == 'short'
                base_exit_short = current_side.lower() == 'short' and exit_state == 'long'
                utbot_exit_eval = await self._evaluate_utbot_filter_pack(
                    symbol,
                    df,
                    strategy_params,
                    tf,
                    'exit'
                )
                self._store_utbot_filter_pack_status(symbol, 'exit', utbot_exit_eval, strategy_params)
                utbot_rsi_exit_eval = self._evaluate_utbot_rsi_momentum_filter(
                    df,
                    strategy_params,
                    'exit'
                )
                self._store_utbot_rsi_momentum_filter_status(
                    symbol,
                    'exit',
                    utbot_rsi_exit_eval,
                    strategy_params
                )

                confirm_selected = utbot_exit_eval.get('confirm_selected', [])
                signal_selected = utbot_exit_eval.get('signal_selected', [])
                confirm_long_pass = bool(utbot_exit_eval.get('confirm_long_pass', True))
                confirm_short_pass = bool(utbot_exit_eval.get('confirm_short_pass', True))
                signal_long_trigger = bool(utbot_exit_eval.get('signal_long_trigger', False))
                signal_short_trigger = bool(utbot_exit_eval.get('signal_short_trigger', False))
                rsi_exit_long_pass = bool(utbot_rsi_exit_eval.get('long_pass', True))
                rsi_exit_short_pass = bool(utbot_rsi_exit_eval.get('short_pass', True))
                rsi_filter_enabled = bool(utbot_rsi_exit_eval.get('enabled', False))

                def _collect_utbot_exit_filter_text(filter_ids, side):
                    reason_key = 'reason_long' if side == 'long' else 'reason_short'
                    pass_key = 'long_pass' if side == 'long' else 'short_pass'
                    parts = []
                    for filter_id in filter_ids:
                        filter_detail = (utbot_exit_eval.get('filters') or {}).get(str(filter_id), {})
                        if filter_detail.get(pass_key, False):
                            parts.append(
                                f"{filter_id}:{filter_detail.get('label', get_utbot_filter_pack_label(filter_id))} "
                                f"({filter_detail.get(reason_key, '-')})"
                            )
                    return " | ".join(parts)

                if rsi_filter_enabled:
                    raw_exit_long = base_exit_long and confirm_long_pass and rsi_exit_long_pass
                    raw_exit_short = base_exit_short and confirm_short_pass and rsi_exit_short_pass
                else:
                    raw_exit_long = (base_exit_long and confirm_long_pass) or signal_long_trigger
                    raw_exit_short = (base_exit_short and confirm_short_pass) or signal_short_trigger

                if current_side.lower() == 'long':
                    exit_reason_parts = []
                    long_trigger_present = base_exit_long if rsi_filter_enabled else (base_exit_long or signal_long_trigger)
                    if base_exit_long:
                        if confirm_selected:
                            if confirm_long_pass:
                                confirm_text = _collect_utbot_exit_filter_text(confirm_selected, 'long')
                                exit_reason_parts.append(
                                    f"{exit_reason} | confirm {format_utbot_filter_pack_logic(utbot_exit_eval.get('logic', 'and'))} "
                                    f"{confirm_text or 'PASS'}"
                                )
                        else:
                            exit_reason_parts.append(exit_reason)
                    if signal_long_trigger and not rsi_filter_enabled:
                        signal_text = _collect_utbot_exit_filter_text(signal_selected, 'long')
                        exit_reason_parts.append(
                            f"UTBOT FILTER SIGNAL EXIT | {format_utbot_filter_pack_logic(utbot_exit_eval.get('logic', 'and'))} "
                            f"{signal_text or 'PASS'}"
                        )
                    if long_trigger_present and utbot_rsi_exit_eval.get('enabled', False) and rsi_exit_long_pass:
                        exit_reason_parts.append(
                            f"RSI Momentum Trend confirm ({utbot_rsi_exit_eval.get('reason_long', 'PASS')})"
                        )
                    if exit_reason_parts:
                        exit_reason = " | ".join(exit_reason_parts)
                elif current_side.lower() == 'short':
                    exit_reason_parts = []
                    short_trigger_present = base_exit_short if rsi_filter_enabled else (base_exit_short or signal_short_trigger)
                    if base_exit_short:
                        if confirm_selected:
                            if confirm_short_pass:
                                confirm_text = _collect_utbot_exit_filter_text(confirm_selected, 'short')
                                exit_reason_parts.append(
                                    f"{exit_reason} | confirm {format_utbot_filter_pack_logic(utbot_exit_eval.get('logic', 'and'))} "
                                    f"{confirm_text or 'PASS'}"
                                )
                        else:
                            exit_reason_parts.append(exit_reason)
                    if signal_short_trigger and not rsi_filter_enabled:
                        signal_text = _collect_utbot_exit_filter_text(signal_selected, 'short')
                        exit_reason_parts.append(
                            f"UTBOT FILTER SIGNAL EXIT | {format_utbot_filter_pack_logic(utbot_exit_eval.get('logic', 'and'))} "
                            f"{signal_text or 'PASS'}"
                        )
                    if short_trigger_present and utbot_rsi_exit_eval.get('enabled', False) and rsi_exit_short_pass:
                        exit_reason_parts.append(
                            f"RSI Momentum Trend confirm ({utbot_rsi_exit_eval.get('reason_short', 'PASS')})"
                        )
                    if exit_reason_parts:
                        exit_reason = " | ".join(exit_reason_parts)
                if raw_exit_long or raw_exit_short:
                    logger.info(f"[Exit Debug] {symbol} {strategy_name}: {exit_reason}")
            elif active_strategy in UTBREAKOUT_STRATEGIES:
                strategy_name = "UTBOT_FILTERED_BREAKOUT_V1(Exit)"
                fb_cfg = self._get_utbot_filtered_breakout_config(strategy_params)
                exit_reason_parts = []
                closed = df.iloc[:-1].copy().reset_index(drop=True)
                for col in ['open', 'high', 'low', 'close', 'volume']:
                    closed[col] = pd.to_numeric(closed[col], errors='coerce')
                closed = closed.dropna(subset=['open', 'high', 'low', 'close']).reset_index(drop=True)
                self._update_utbreakout_shadow_triple_barrier(symbol, closed, fb_cfg)
                trailing_state = self._get_utbreakout_trailing_state(symbol) or {}
                exit_gate_state = dict(trailing_state)
                exit_gate_state['entry_timeframe'] = tf
                post_entry_closed, post_entry_gate = (
                    self._utbreakout_post_entry_closed_bars(
                        closed,
                        exit_gate_state,
                        {**fb_cfg, 'timeframe': tf},
                    )
                )
                if post_entry_gate and (
                    post_entry_closed is None or post_entry_closed.empty
                ):
                    self.last_entry_reason[symbol] = (
                        'EXIT_WAIT: waiting for first full post-entry closed bar'
                    )
                    return True

                if bool(fb_cfg.get('opposite_signal_exit_enabled', False)):
                    exit_sig, ut_exit_reason, _ = self._calculate_utbot_signal(
                        df,
                        self._get_utbot_filtered_breakout_ut_params(fb_cfg)
                    )
                    if current_side.lower() == 'long' and exit_sig == 'short':
                        raw_exit_long = True
                        exit_reason_parts.append(f"UT opposite SELL ({ut_exit_reason})")
                    elif current_side.lower() == 'short' and exit_sig == 'long':
                        raw_exit_short = True
                        exit_reason_parts.append(f"UT opposite BUY ({ut_exit_reason})")

                opposite_set_exit = False
                opposite_set_reason = None
                if bool(fb_cfg.get('opposite_set_exit_enabled', False)):
                    position_fetch_ok, current_pos = await self._fetch_server_position_checked(symbol)
                    if position_fetch_ok:
                        opposite_set_exit, opposite_set_reason = (
                            await self._evaluate_utbreakout_opposite_set_exit(
                                symbol,
                                df,
                                fb_cfg,
                                current_side,
                                current_pos,
                            )
                        )
                    else:
                        opposite_set_reason = (
                            "Opposite set exit skipped: current position fetch failed"
                        )
                if opposite_set_exit:
                    if current_side.lower() == 'long':
                        raw_exit_long = True
                    elif current_side.lower() == 'short':
                        raw_exit_short = True
                    exit_reason_parts.append(opposite_set_reason)

                if bool(fb_cfg.get('ema_rsi_exit_enabled', False)) and len(closed) >= int(fb_cfg['ema_fast']) + int(fb_cfg['rsi_length']) + 2:
                    close_series = closed['close'].astype(float)
                    curr_close = float(close_series.iloc[-1])
                    ema50 = float(close_series.ewm(span=int(fb_cfg['ema_fast']), adjust=False).mean().iloc[-1])
                    rsi_series = self._calculate_wilder_rsi_series(close_series, int(fb_cfg['rsi_length']))
                    rsi_value = float(rsi_series.iloc[-1]) if self._is_valid_number(rsi_series.iloc[-1]) else np.nan
                    if current_side.lower() == 'long' and curr_close < ema50 and self._is_valid_number(rsi_value) and rsi_value < 50.0:
                        raw_exit_long = True
                        exit_reason_parts.append(f"EMA50/RSI exit close={curr_close:.4f} < EMA50={ema50:.4f}, RSI={rsi_value:.2f}")
                    elif current_side.lower() == 'short' and curr_close > ema50 and self._is_valid_number(rsi_value) and rsi_value > 50.0:
                        raw_exit_short = True
                        exit_reason_parts.append(f"EMA50/RSI exit close={curr_close:.4f} > EMA50={ema50:.4f}, RSI={rsi_value:.2f}")

                if bool(fb_cfg.get('adx_donchian_exit_enabled', False)) and len(closed) >= int(fb_cfg['donchian_length']) + int(fb_cfg['adx_length']) * 2 + 5:
                    curr_close = float(closed['close'].astype(float).iloc[-1])
                    adx_value, _, _, _ = self._calculate_utbot_filter_pack_adx_dmi(closed, int(fb_cfg['adx_length']))
                    don_window = closed.iloc[-int(fb_cfg['donchian_length']) - 1:-1]
                    don_high_prev = float(don_window['high'].astype(float).max())
                    don_low_prev = float(don_window['low'].astype(float).min())
                    back_inside = don_low_prev <= curr_close <= don_high_prev
                    adx_low = adx_value is not None and self._is_valid_number(adx_value) and float(adx_value) < float(fb_cfg['adx_threshold'])
                    if current_side.lower() == 'long' and back_inside and adx_low:
                        raw_exit_long = True
                        exit_reason_parts.append(f"ADX/Donchian re-entry exit ADX={float(adx_value):.2f}")
                    elif current_side.lower() == 'short' and back_inside and adx_low:
                        raw_exit_short = True
                        exit_reason_parts.append(f"ADX/Donchian re-entry exit ADX={float(adx_value):.2f}")

                exit_reason = " | ".join(exit_reason_parts) if exit_reason_parts else "SL/TP managed; optional exit filters OFF"
                if raw_exit_long or raw_exit_short:
                    logger.info(f"[Exit Debug] {symbol} {strategy_name}: {exit_reason}")
            elif active_strategy == 'utbb':
                strategy_name = "UTBB(Exit)"
                _, exit_reason, utbb_detail = self._calculate_utbb_signal(None, df, strategy_params)
                exit_ctx = self._evaluate_utbb_exit_context(symbol, utbb_detail, current_side)
                raw_exit_long = bool(exit_ctx.get('raw_exit_long'))
                raw_exit_short = bool(exit_ctx.get('raw_exit_short'))
                if raw_exit_long or raw_exit_short:
                    exit_note = exit_ctx.get('exit_note') or exit_reason
                    exit_reason = f"UTBB EXIT: {exit_note}"
                    logger.info(f"[Exit Debug] {symbol} {strategy_name}: {exit_reason}")
                else:
                    exit_reason = exit_ctx.get('hold_note') or exit_reason
            elif active_strategy in {'utrsi', 'utrsibb'}:
                strategy_name = f"{STRATEGY_DISPLAY_NAMES.get(active_strategy, active_strategy.upper())}(Exit)"
                hybrid_calc = {
                    'utrsibb': self._calculate_utrsibb_signal,
                    'utrsi': self._calculate_utrsi_signal
                }.get(active_strategy)
                _, hybrid_reason, hybrid_detail = hybrid_calc(None, df, strategy_params)
                regime_sig = str(hybrid_detail.get('ut_state') or '').lower()
                timing_label = hybrid_detail.get('timing_label') or 'Signal'
                timing_sig = hybrid_detail.get('effective_timing_signal') or hybrid_detail.get('timing_signal')
                raw_exit_long = current_side.lower() == 'long' and regime_sig == 'short'
                raw_exit_short = current_side.lower() == 'short' and regime_sig == 'long'
                if raw_exit_long or raw_exit_short:
                    exit_reason = f"{strategy_name} EXIT: UT {regime_sig.upper()} state on exit TF"
                    if timing_sig in {'long', 'short'}:
                        exit_reason += f" | {timing_label} {str(timing_sig).upper()}"
                    logger.info(f"[Exit Debug] {symbol} {strategy_name}: {exit_reason}")
                else:
                    exit_reason = hybrid_reason
            else:
                # Simple SMA/HMA Logic for Exit
                if active_strategy == 'hma':
                    p = strategy_params.get('HMA', {})
                    fast_period = p.get('fast_period', 9)
                    slow_period = p.get('slow_period', 21)
                    df['f'] = ta.hma(df['close'], length=fast_period)
                    df['s'] = ta.hma(df['close'], length=slow_period)
                    strategy_name = "HMA(Exit)"
                else:
                    p = strategy_params.get('Triple_SMA', {})
                    fast_period = p.get('fast_sma', 3)
                    slow_period = p.get('slow_sma', 10) # Fixed default to 10
                    df['f'] = ta.sma(df['close'], length=fast_period)
                    df['s'] = ta.sma(df['close'], length=slow_period)
                    strategy_name = "SMA(Exit)"

                c_f, c_s = df['f'].iloc[-2], df['s'].iloc[-2]
                p_f, p_s = df['f'].iloc[-3], df['s'].iloc[-3]

                # Check Cross (Standard Exit Trigger)
                # If Long -> Cross Down is exit signal
                if p_f > p_s and c_f < c_s:
                    raw_exit_long = True
                    logger.info(f"?뱣 [Exit Debug] {symbol} Dead Cross: {p_f:.2f}/{p_s:.2f} -> {c_f:.2f}/{c_s:.2f}")

                # If Short -> Cross Up is exit signal
                if p_f < p_s and c_f > c_s:
                    raw_exit_short = True
                    logger.info(f"?뱢 [Exit Debug] {symbol} Golden Cross: {p_f:.2f}/{p_s:.2f} -> {c_f:.2f}/{c_s:.2f}")

                if current_side.lower() == 'long':
                    if c_f < c_s: # Alignment becomes bearish
                        raw_exit_long = True
                        logger.info(f"?슟 [Exit Debug] {symbol} Bearish Alignment: {c_f:.2f} < {c_s:.2f}")
                    else:
                        logger.debug(f"?뵇 [Exit Debug] {symbol} Still Bullish: {c_f:.2f} > {c_s:.2f}")
                elif current_side.lower() == 'short':
                    if c_f > c_s: # Alignment becomes bullish
                        raw_exit_short = True
                        logger.info(f"??[Exit Debug] {symbol} Bullish Alignment: {c_f:.2f} > {c_s:.2f}")
                    else:
                        logger.debug(f"?뵇 [Exit Debug] {symbol} Still Bearish: {c_f:.2f} < {c_s:.2f}")

            # ?좏샇媛 ?녿뜑?쇰룄 ?꾪꽣 媛믪? 怨꾩궛?댁꽌 ??쒕낫?쒖뿉 ?낅뜲?댄듃 (??Pending 諛⑹?)
            await self._update_exit_filter_values(symbol, df, current_side)
            if not raw_exit_long and not raw_exit_short:
                return True

            if bypass_exit_filters:
                if current_side.lower() == 'long' and raw_exit_long:
                    if fixed_bearish_ob_entry and ut_invalidation_long:
                        exit_call_reason = f"{strategy_name}_MultiTriggerLongExit"
                    elif ut_invalidation_long:
                        exit_call_reason = f"{strategy_name}_UTLongInvalidation"
                    elif candidate2_short_exit:
                        exit_call_reason = f"{strategy_name}_Candidate2ShortExit"
                    else:
                        exit_call_reason = f"{strategy_name}_InternalBearishOBEntry"
                    logger.info(f"[Exit {tf}] LONG Exit Triggered: {exit_reason}")
                    await self.exit_position(symbol, exit_call_reason)
                elif current_side.lower() == 'short' and raw_exit_short:
                    if fixed_bullish_ob_entry and ut_invalidation_short:
                        exit_call_reason = f"{strategy_name}_MultiTriggerShortExit"
                    elif ut_invalidation_short:
                        exit_call_reason = f"{strategy_name}_UTShortInvalidation"
                    elif candidate2_long_exit:
                        exit_call_reason = f"{strategy_name}_Candidate2LongExit"
                    else:
                        exit_call_reason = f"{strategy_name}_InternalBullishOBEntry"
                    logger.info(f"[Exit {tf}] SHORT Exit Triggered: {exit_reason}")
                    await self.exit_position(symbol, exit_call_reason)
                return True

            # ===== 3. Exit Filter logic check =====
            can_exit = True
            block_reasons = []

            # Re-fetch the calculated values for checking
            st = self.last_exit_filter_status.get(symbol, {})
            kalman_vel = st.get('kalman_vel', 0.0)
            curr_r2 = st.get('r2_val', 0.0)
            curr_chop = st.get('chop_val', 50.0)

            kalman_cfg = strategy_params.get('kalman_filter', {})
            kalman_exit_enabled = kalman_cfg.get('exit_enabled', False)

            common_cfg = self.get_runtime_common_settings()
            r2_exit_enabled = common_cfg.get('r2_exit_enabled', True)
            r2_thresh = common_cfg.get('r2_threshold', 0.25)
            chop_exit_enabled = common_cfg.get('chop_exit_enabled', True)
            chop_thresh = common_cfg.get('chop_threshold', 50.0)

            # 1. Kalman Check
            if kalman_exit_enabled:
                if current_side.lower() == 'long':
                    # To Exit Long, Kalman must be Bearish (Velocity < 0)
                    if kalman_vel >= 0:
                        can_exit = False
                        block_reasons.append(f"Kalman(Vel={kalman_vel:.4f})>=0")
                elif current_side.lower() == 'short':
                    # To Exit Short, Kalman must be Bullish (Velocity > 0)
                    if kalman_vel <= 0:
                        can_exit = False
                        block_reasons.append(f"Kalman(Vel={kalman_vel:.4f})<=0")

            # 2. R2 Check (Trend Strength)
            if r2_exit_enabled:
                if curr_r2 < r2_thresh:
                    can_exit = False
                    block_reasons.append(f"R2({curr_r2:.2f})<{r2_thresh}")

            # 3. CHOP Check
            can_exit_by_chop = not chop_exit_enabled or (curr_chop <= chop_thresh)
            if chop_exit_enabled and not can_exit_by_chop:
                can_exit = False
                block_reasons.append(f"Chop({curr_chop:.1f})>{chop_thresh}")

            # 4. CC Check (Correlation)
            cc_exit_enabled = common_cfg.get('cc_exit_enabled', False)
            cc_thresh = common_cfg.get('cc_threshold', 0.70)
            curr_cc = st.get('cc_val', 0.0)
            can_exit_by_cc = (not cc_exit_enabled)
            if cc_exit_enabled:
                if current_side.lower() == 'long':
                    can_exit_by_cc = curr_cc < -cc_thresh
                elif current_side.lower() == 'short':
                    can_exit_by_cc = curr_cc > cc_thresh
                else:
                    can_exit_by_cc = False
                if not can_exit_by_cc:
                    can_exit = False
                    block_reasons.append(f"CC({curr_cc:.2f}) fail")

            # [New] Update Pass/Fail Status based on logic above
            st['kalman_pass'] = (not kalman_exit_enabled) or \
                              (current_side.lower() == 'long' and kalman_vel < 0) or \
                              (current_side.lower() == 'short' and kalman_vel > 0)
            st['r2_pass'] = (not r2_exit_enabled) or (curr_r2 >= r2_thresh)
            st['chop_pass'] = (not chop_exit_enabled) or (curr_chop <= chop_thresh)


            # ===== 5. Execute Exit =====
            # Enabled exit filters are all mandatory. If one fails, exit is blocked.

            if current_side.lower() == 'long':
                if raw_exit_long:
                    if can_exit:
                        logger.info(f"?뵒 [Exit {tf}] LONG Exit Triggered (Chop:{curr_chop:.1f}, CC:{curr_cc:.2f})")
                        await self.exit_position(symbol, f"{strategy_name}_Exit_L")
                        if (not self.is_upbit_mode()) and (active_strategy == 'utbot' or active_strategy in UT_HYBRID_STRATEGIES):
                            await self._post_ut_secondary_exit(symbol, active_strategy, current_side)
                    else:
                        why = ", ".join(block_reasons) if block_reasons else "Filter blocked"
                        logger.info(f"?썳截?[Exit {tf}] LONG Exit Blocked: {why} (Chop:{curr_chop:.1f}, CC:{curr_cc:.2f})")
                else:
                    logger.debug(f"??[Exit {tf}] LONG Exit Ignored: raw exit signal not confirmed")

            elif current_side.lower() == 'short':
                if raw_exit_short:
                    if can_exit:
                        logger.info(f"?뵒 [Exit {tf}] SHORT Exit Triggered (Chop:{curr_chop:.1f}, CC:{curr_cc:.2f})")
                        await self.exit_position(symbol, f"{strategy_name}_Exit_S")
                        if (not self.is_upbit_mode()) and (active_strategy == 'utbot' or active_strategy in UT_HYBRID_STRATEGIES):
                            await self._post_ut_secondary_exit(symbol, active_strategy, current_side)
                    else:
                        why = ", ".join(block_reasons) if block_reasons else "Filter blocked"
                        logger.info(f"?썳截?[Exit {tf}] SHORT Exit Blocked: {why} (Chop:{curr_chop:.1f}, CC:{curr_cc:.2f})")
                else:
                    logger.debug(f"??[Exit {tf}] SHORT Exit Ignored: raw exit signal not confirmed")
            return True

        except Exception as e:
            logger.error(f"Process exit candle error: {e}")
            import traceback
            traceback.print_exc()
            return False

    async def _calculate_strategy_signal(
        self,
        symbol,
        df,
        strategy_params,
        active_strategy,
        allow_utbot_stateful=True,
        precomputed=None,
        force_utbreakout_reprocess=False,
    ):
        """
        ?꾨왂蹂??좏샇 怨꾩궛

        Returns: (signal, is_bullish, is_bearish, strategy_name, entry_mode, kalman_enabled)
        """
        sig = None
        is_bullish = False
        is_bearish = False
        entry_reason = "신호 대기"

        precomputed = precomputed or {}

        # Init state dicts if needed
        self._ensure_runtime_state_containers((
            'vbo_states',
            'fisher_states',
            'cameron_states',
            'kalman_states'
        ))
        if symbol not in self.kalman_states:
            self.kalman_states[symbol] = {'velocity': 0.0, 'direction': None}

        active_strategy = str(active_strategy).lower()
        if active_strategy not in CORE_STRATEGIES:
            logger.warning(f"Unsupported active_strategy '{active_strategy}' in core mode. Using UTBOT.")
            active_strategy = 'utbot'

        entry_mode = active_strategy
        kalman_entry_enabled = False
        self.last_entry_filter_status[symbol] = {}
        self.kalman_states[symbol]['velocity'] = 0.0
        self.kalman_states[symbol]['direction'] = None

        strategy_name = STRATEGY_DISPLAY_NAMES.get(active_strategy, active_strategy.upper())

        if active_strategy == 'utbot':
            entry_mode = 'utbot'
            sig, entry_reason, utbot_detail = precomputed.get('utbot') or self._calculate_utbot_signal(df, strategy_params)
            if sig is None and allow_utbot_stateful:
                bias_sig = utbot_detail.get('bias_side')
                if bias_sig in {'long', 'short'}:
                    sig = bias_sig
            is_bullish = sig == 'long'
            is_bearish = sig == 'short'

        elif active_strategy == 'utsmc':
            entry_mode = 'utsmc'
            sig, entry_reason, utsmc_detail = precomputed.get('utsmc') or self._calculate_utsmc_signal(df, strategy_params)
            ut_state = utsmc_detail.get('ut_state')
            is_bullish = ut_state == 'long'
            is_bearish = ut_state == 'short'

        elif active_strategy == 'utbb':
            entry_mode = 'utbb'
            sig, entry_reason, hybrid_detail = precomputed.get('utbb') or self._calculate_utbb_signal(symbol, df, strategy_params)
            ut_state = hybrid_detail.get('ut_state')
            is_bullish = ut_state == 'long'
            is_bearish = ut_state == 'short'

        elif active_strategy in UT_HYBRID_STRATEGIES:
            entry_mode = active_strategy
            hybrid_calc = {
                'utrsibb': self._calculate_utrsibb_signal,
                'utrsi': self._calculate_utrsi_signal
            }.get(active_strategy)
            sig, entry_reason, hybrid_detail = precomputed.get(active_strategy) or hybrid_calc(symbol, df, strategy_params)
            ut_state = hybrid_detail.get('ut_state')
            is_bullish = ut_state == 'long'
            is_bearish = ut_state == 'short'
        elif active_strategy == 'rsibb':
            entry_mode = 'rsibb'
            guard_ok, guard_reason = self._rsibb_runtime_guard(strategy_params)
            if guard_ok:
                sig, entry_reason, _ = precomputed.get('rsibb') or self._calculate_rsibb_signal(df, strategy_params)
            else:
                sig = None
                entry_reason = guard_reason
            is_bullish = sig == 'long'
            is_bearish = sig == 'short'
        elif active_strategy == ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND:
            entry_mode = active_strategy
            sig, entry_reason, _ = await self._calculate_relative_strength_pullback_signal(
                symbol,
                df,
                strategy_params,
                force_reprocess=force_utbreakout_reprocess,
            )
            is_bullish = sig == 'long'
            is_bearish = sig == 'short'
        elif active_strategy == CROWDING_UNWIND_STRATEGY:
            entry_mode = active_strategy
            sig, entry_reason, _ = await self._calculate_crowding_unwind_signal(
                symbol,
                df,
                strategy_params,
                force_reprocess=force_utbreakout_reprocess,
            )
            is_bullish = sig == 'long'
            is_bearish = sig == 'short'
        elif active_strategy == LXR_STRATEGY:
            entry_mode = active_strategy
            sig, entry_reason, _ = await self._calculate_liquidation_exhaustion_reversal_signal(
                symbol,
                df,
                strategy_params,
                force_reprocess=force_utbreakout_reprocess,
            )
            is_bullish = sig == 'long'
            is_bearish = sig == 'short'
        elif active_strategy == QH_FLOW_STRATEGY:
            entry_mode = active_strategy
            sig, entry_reason, _ = await self._calculate_qh_flow_signal(
                symbol,
                df,
                strategy_params,
                force_reprocess=force_utbreakout_reprocess,
            )
            is_bullish = sig == 'long'
            is_bearish = sig == 'short'
        elif active_strategy == DUAL_ALPHA_STRATEGY:
            entry_mode = active_strategy
            sig, entry_reason, _ = await self._calculate_dual_alpha_signal(
                symbol,
                df,
                strategy_params,
                force_reprocess=force_utbreakout_reprocess,
            )
            is_bullish = sig == 'long'
            is_bearish = sig == 'short'
        elif active_strategy == TRIPLE_ALPHA_STRATEGY:
            entry_mode = active_strategy
            sig, entry_reason, _ = await self._calculate_triple_alpha_signal(
                symbol,
                df,
                strategy_params,
                force_reprocess=force_utbreakout_reprocess,
            )
            is_bullish = sig == 'long'
            is_bearish = sig == 'short'
        elif active_strategy == QUAD_ALPHA_STRATEGY:
            entry_mode = active_strategy
            sig, entry_reason, _ = await self._calculate_quad_alpha_signal(
                symbol,
                df,
                strategy_params,
                force_reprocess=force_utbreakout_reprocess,
            )
            is_bullish = sig == 'long'
            is_bearish = sig == 'short'
        elif active_strategy in UTBREAKOUT_STRATEGIES:
            entry_mode = active_strategy
            sig, entry_reason, _ = await self._calculate_utbot_filtered_breakout_signal(
                symbol,
                df,
                strategy_params,
                force_reprocess=force_utbreakout_reprocess,
            )
            is_bullish = sig == 'long'
            is_bearish = sig == 'short'

        self.last_entry_reason[symbol] = entry_reason
        return sig, is_bullish, is_bearish, strategy_name, entry_mode, kalman_entry_enabled

    async def _update_exit_filter_values(self, symbol, df, current_side):
        """[Helper] Calculate exit filter values and update status without executing exit logic"""
        try:
            strategy_params = self.get_runtime_strategy_params()
            common_cfg = self.get_runtime_common_settings()

            # A. Kalman Filter
            kalman_cfg = strategy_params.get('kalman_filter', {})
            kalman_exit_enabled = kalman_cfg.get('exit_enabled', False)
            kalman_vel = self._calculate_kalman_values(df, kalman_cfg)

            # B. R2 Filter
            r2_exit_enabled = common_cfg.get('r2_exit_enabled', True)
            r2_thresh = common_cfg.get('r2_threshold', 0.25)
            curr_r2 = 0.0
            if len(df) >= 14:
                df['idx_seq'] = np.arange(len(df))
                curr_r2 = (df['close'].rolling(14).corr(df['idx_seq']).iloc[-2]) ** 2
                if np.isnan(curr_r2): curr_r2 = 0.0

            # C. Chop Filter
            chop_exit_enabled = common_cfg.get('chop_exit_enabled', True)
            chop_thresh = common_cfg.get('chop_threshold', 50.0)
            curr_chop = 50.0
            if len(df) >= 14 and chop_exit_enabled:
                try:
                    chop = df.ta.chop(length=14)
                    if chop is not None:
                        curr_chop = chop.iloc[-2]
                        if np.isnan(curr_chop): curr_chop = 50.0
                    else:
                        logger.warning("?좑툘 Chop calculation returned None")
                except Exception as e:
                    logger.error(f"??Chop calculation error (Exit): {e}")

            # D. CC Filter (Correlation Coefficient)
            cc_exit_enabled = common_cfg.get('cc_exit_enabled', False)
            cc_thresh = common_cfg.get('cc_threshold', 0.70)
            cc_len = common_cfg.get('cc_length', 14)
            curr_cc = 0.0
            if len(df) >= cc_len:
                if 'idx_seq' not in df.columns:
                    df['idx_seq'] = np.arange(len(df))
                curr_cc = df['close'].rolling(cc_len).corr(df['idx_seq']).iloc[-2]
                if np.isnan(curr_cc): curr_cc = 0.0

            # Debug: CC Pass calculation
            cc_pass_result = (not cc_exit_enabled) or \
                           (current_side.lower() == 'long' and curr_cc < -cc_thresh) or \
                           (current_side.lower() == 'short' and curr_cc > cc_thresh)
            logger.debug(f"[CC Debug] enabled={cc_exit_enabled}, side={current_side}, cc={curr_cc:.3f}, thresh={cc_thresh}, pass={cc_pass_result}")

            # Update Status Data for Dashboard
            self.last_exit_filter_status[symbol] = {
                'r2_val': curr_r2,
                'chop_val': curr_chop,
                'kalman_vel': kalman_vel,
                'r2_pass': (not r2_exit_enabled) or (curr_r2 >= r2_thresh),
                'chop_pass': (not chop_exit_enabled) or (curr_chop <= chop_thresh),
                'cc_val': curr_cc,
                'cc_pass': (not cc_exit_enabled) or \
                           (current_side.lower() == 'long' and curr_cc < -cc_thresh) or \
                           (current_side.lower() == 'short' and curr_cc > cc_thresh),
                'kalman_pass': (not kalman_exit_enabled) or \
                              (current_side.lower() == 'long' and kalman_vel < 0) or \
                              (current_side.lower() == 'short' and kalman_vel > 0)
            }
        except Exception as e:
            logger.error(f"Error updating exit filter values for {symbol}: {e}")

    async def _confirm_entry_position(self, symbol, side, order, requested_price, requested_qty):
        attempts = max(1, int(getattr(self, 'ENTRY_POSITION_CONFIRM_ATTEMPTS', 5) or 5))
        delay = max(0.0, float(getattr(self, 'ENTRY_POSITION_CONFIRM_DELAY', 0.5) or 0.0))
        any_position_fetch_ok = False
        for attempt in range(attempts):
            self.position_cache = None
            self.position_cache_time = 0
            fetch_ok, pos = await self._fetch_server_position_checked(symbol)
            any_position_fetch_ok = any_position_fetch_ok or fetch_ok
            if fetch_ok and pos:
                pos_side = str(pos.get('side', '') or '').lower()
                contracts = abs(float(self._position_signed_contracts(pos) or pos.get('contracts', 0) or 0))
                if pos_side == str(side or '').lower() and contracts > 0:
                    return {
                        'confirmed': True,
                        'position_fetch_ok': True,
                        'position': pos,
                        'source': 'position',
                    }
            if attempt < attempts - 1 and delay > 0:
                await asyncio.sleep(delay)

        resolved_order = dict(order) if isinstance(order, dict) else {}
        order_id = resolved_order.get('id')
        fetch_order = getattr(self.exchange, 'fetch_order', None)
        if order_id and callable(fetch_order):
            try:
                fetched_order = await asyncio.to_thread(fetch_order, order_id, symbol)
                if isinstance(fetched_order, dict):
                    resolved_order.update(fetched_order)
            except Exception as exc:
                logger.warning(f"Entry order status lookup failed for {symbol}: {exc}")

        status = str(resolved_order.get('status') or '').lower()
        filled_qty = _safe_float_or_none(resolved_order.get('filled'))
        if filled_qty is None:
            info = resolved_order.get('info') if isinstance(resolved_order.get('info'), dict) else {}
            filled_qty = _safe_float_or_none(
                info.get('executedQty') or info.get('cumQty')
            )
        average_price = (
            _safe_float_or_none(resolved_order.get('average'))
            or _safe_float_or_none(resolved_order.get('price'))
        )
        if average_price is None:
            info = resolved_order.get('info') if isinstance(resolved_order.get('info'), dict) else {}
            average_price = _safe_float_or_none(info.get('avgPrice'))
        order_filled = (
            status in {'closed', 'filled'}
            or bool(filled_qty and filled_qty > 0)
        )
        if order_filled and filled_qty and filled_qty > 0:
            return {
                'confirmed': True,
                'position_fetch_ok': any_position_fetch_ok,
                'position': {
                    'symbol': symbol,
                    'side': str(side or '').lower(),
                    'contracts': float(filled_qty),
                    'entryPrice': float(average_price or requested_price),
                },
                'source': 'filled_order',
            }

        return {
            'confirmed': False,
            'position_fetch_ok': any_position_fetch_ok,
            'position': None,
            'source': 'position_missing' if any_position_fetch_ok else 'position_fetch_failed',
            'order_status': status or None,
            'requested_qty': float(requested_qty),
        }

    async def _rebuild_utbreakout_entry_plan_for_active_strategy(
        self,
        symbol,
        side,
        strategy_params,
        active_strategy,
    ):
        """Re-evaluate the configured strategy aggregate when an accepted plan was lost."""
        fb_cfg = self._get_utbot_filtered_breakout_config(strategy_params)
        entry_tf = fb_cfg.get('entry_timeframe', '15m')
        ohlcv = await asyncio.to_thread(
            self.market_data_exchange.fetch_ohlcv,
            symbol,
            entry_tf,
            limit=300,
        )
        df = pd.DataFrame(
            ohlcv,
            columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'],
        )
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = pd.to_numeric(df[col], errors='coerce')

        rebuilt_sig, _, _, _, _, _ = await self._calculate_strategy_signal(
            symbol,
            df,
            strategy_params,
            active_strategy,
            force_utbreakout_reprocess=True,
        )
        rebuilt_reason = self.last_entry_reason.get(symbol) or ''
        if rebuilt_sig != side:
            return None, rebuilt_sig, rebuilt_reason
        rebuilt_plan = self._get_utbot_filtered_breakout_entry_plan(symbol, side)
        return rebuilt_plan, rebuilt_sig, rebuilt_reason
