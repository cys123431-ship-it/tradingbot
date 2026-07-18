"""UT live-entry timing and specialized setup state."""

from __future__ import annotations


class SignalUtEntryMixin:
    async def _maybe_execute_ut_live_entry(self, symbol, live_candle_ts, live_price, pos_side):
        pending = self._get_ut_pending_entry(symbol)
        pending_source = self._get_ut_pending_source(pending) if pending else None
        is_flip_pending = self._is_ut_flip_pending(pending) if pending else False
        pending_side = str((pending or {}).get('side') or '').strip().lower()
        if pos_side != 'NONE':
            if pending:
                if is_flip_pending:
                    current_pos_side = str(pos_side).strip().lower()
                    if pending_side in {'long', 'short'} and current_pos_side == pending_side:
                        execute_ts = int(pending.get('execute_ts') or 0)
                        expires_ts = int(pending.get('expires_ts') or 0)
                        signal_ts = int(pending.get('signal_ts') or 0)
                        retry_count = int(pending.get('retry_count') or 0)
                        strategy_name = str(pending.get('strategy_name') or 'UT')
                        self._clear_ut_pending_entry(symbol)
                        self.last_entry_reason[symbol] = (
                            f"{strategy_name} flip pending stale cleared ({pending_side.upper()} already open)"
                        )
                        self._update_stateful_diag(
                            symbol,
                            stage='flip_stale_cleared',
                            strategy=strategy_name,
                            entry_sig=(pending_side or 'none'),
                            pos_side=current_pos_side.upper(),
                            ut_pending_source='flip',
                            ut_pending_side=(pending_side.upper() if pending_side in {'long', 'short'} else None),
                            ut_pending_state='stale_cleared',
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
                            ut_flip_target_side=(pending_side.upper() if pending_side in {'long', 'short'} else None),
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
                            ut_flip_block_reason='same_side_position',
                            note='flip pending stale cleared | matching live position already exists'
                        )
                    else:
                        self._touch_ut_pending_entry(symbol, pending_state='waiting_flat')
                else:
                    self._clear_ut_pending_entry(symbol)
            return pos_side
        if not pending:
            return pos_side

        execute_ts = int(pending.get('execute_ts') or 0)
        if live_candle_ts < execute_ts:
            return pos_side

        timing_mode = self._get_ut_entry_timing_mode()
        expires_ts = int(pending.get('expires_ts') or 0)
        if timing_mode == 'next_candle':
            if is_flip_pending:
                if expires_ts > 0 and live_candle_ts > expires_ts:
                    entry_sig = str(pending.get('side') or '').lower()
                    strategy_name = str(pending.get('strategy_name') or 'UT')
                    retry_count = int(pending.get('retry_count') or 0)
                    self._clear_ut_pending_entry(symbol)
                    self.last_entry_reason[symbol] = (
                        f"{strategy_name} flip {entry_sig.upper()} 대기 만료"
                        if entry_sig in {'long', 'short'}
                        else f"{strategy_name} flip 대기 만료"
                    )
                    self._update_stateful_diag(
                        symbol,
                        stage='flip_expired',
                        strategy=strategy_name,
                        entry_sig=(entry_sig or 'none'),
                        pos_side='NONE',
                        ut_pending_source=pending_source,
                        ut_pending_side=(entry_sig.upper() if entry_sig in {'long', 'short'} else None),
                        ut_pending_state='expired',
                        ut_pending_execute_ts=execute_ts,
                        ut_pending_execute_ts_human=datetime.fromtimestamp(execute_ts / 1000).strftime('%m-%d %H:%M') if execute_ts else None,
                        ut_pending_expires_ts=expires_ts,
                        ut_pending_expires_ts_human=datetime.fromtimestamp(expires_ts / 1000).strftime('%m-%d %H:%M') if expires_ts else None,
                        ut_flip_target_side=(entry_sig.upper() if entry_sig in {'long', 'short'} else None),
                        ut_flip_signal_ts=int(pending.get('signal_ts') or 0) or None,
                        ut_flip_signal_ts_human=(
                            datetime.fromtimestamp(int(pending.get('signal_ts') or 0) / 1000).strftime('%m-%d %H:%M')
                            if int(pending.get('signal_ts') or 0) else None
                        ),
                        ut_flip_execute_ts=execute_ts,
                        ut_flip_execute_ts_human=datetime.fromtimestamp(execute_ts / 1000).strftime('%m-%d %H:%M') if execute_ts else None,
                        ut_flip_expires_ts=expires_ts,
                        ut_flip_expires_ts_human=datetime.fromtimestamp(expires_ts / 1000).strftime('%m-%d %H:%M') if expires_ts else None,
                        ut_flip_retry_count=retry_count,
                        ut_flip_block_reason=pending.get('block_reason'),
                        note=(
                            f"flip pending expired | live_ts={live_candle_ts} | "
                            f"execute_ts={execute_ts} | expires_ts={expires_ts}"
                        )
                    )
                    await self._notify_stateful_diag(symbol)
                    return pos_side
            elif live_candle_ts > execute_ts:
                self._clear_ut_pending_entry(symbol)
                return pos_side

        entry_sig = str(pending.get('side') or '').lower()
        if entry_sig not in {'long', 'short'}:
            self._clear_ut_pending_entry(symbol)
            return pos_side

        strategy_name = str(pending.get('strategy_name') or 'UT')
        timing_label = self._get_ut_entry_timing_label(timing_mode)
        if not self.is_trade_direction_allowed(entry_sig):
            block_reason = self.format_trade_direction_block_reason(entry_sig)
            self._clear_ut_pending_entry(symbol)
            self.last_entry_reason[symbol] = f"{strategy_name} {block_reason}"
            self._update_stateful_diag(
                symbol,
                stage='entry_blocked',
                strategy=strategy_name,
                entry_sig=entry_sig,
                pos_side='NONE',
                ut_pending_source=pending_source,
                ut_pending_side=entry_sig.upper(),
                ut_pending_state='blocked',
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
                ut_flip_target_side=(entry_sig.upper() if is_flip_pending else None),
                note=f"live {timing_mode} entry blocked by direction filter"
            )
            return pos_side

        self._clear_ut_pending_entry(symbol)
        self.last_entry_reason[symbol] = (
            f"{strategy_name} flip 유지 확인 -> {entry_sig.upper()} 진입"
            if is_flip_pending else
            f"{strategy_name} {timing_label} 기준 {entry_sig.upper()} 진입"
        )
        self._update_stateful_diag(
            symbol,
            stage='flip_reentered' if is_flip_pending else 'entry_submitted',
            strategy=strategy_name,
            entry_sig=entry_sig,
            pos_side=entry_sig.upper(),
            ut_pending_source=pending_source,
            ut_pending_side=entry_sig.upper(),
            ut_pending_state='executed',
            ut_pending_execute_ts=execute_ts,
            ut_pending_execute_ts_human=datetime.fromtimestamp(execute_ts / 1000).strftime('%m-%d %H:%M') if execute_ts else None,
            ut_pending_expires_ts=expires_ts,
            ut_pending_expires_ts_human=datetime.fromtimestamp(expires_ts / 1000).strftime('%m-%d %H:%M') if expires_ts else None,
            ut_flip_target_side=(entry_sig.upper() if is_flip_pending else None),
            ut_flip_signal_ts=int(pending.get('signal_ts') or 0) or None,
            ut_flip_signal_ts_human=(
                datetime.fromtimestamp(int(pending.get('signal_ts') or 0) / 1000).strftime('%m-%d %H:%M')
                if int(pending.get('signal_ts') or 0) else None
            ),
            ut_flip_execute_ts=execute_ts if is_flip_pending else None,
            ut_flip_execute_ts_human=(
                datetime.fromtimestamp(execute_ts / 1000).strftime('%m-%d %H:%M')
                if is_flip_pending and execute_ts else None
            ),
            ut_flip_expires_ts=expires_ts if is_flip_pending else None,
            ut_flip_expires_ts_human=(
                datetime.fromtimestamp(expires_ts / 1000).strftime('%m-%d %H:%M')
                if is_flip_pending and expires_ts else None
            ),
            ut_flip_retry_count=int(pending.get('retry_count') or 0) if is_flip_pending else None,
            ut_flip_block_reason=pending.get('block_reason') if is_flip_pending else None,
            note=(
                f"live flip entry | live_ts={live_candle_ts} | armed_ts={execute_ts} | expires_ts={expires_ts}"
                if is_flip_pending else
                f"live {timing_mode} entry | live_ts={live_candle_ts} | armed_ts={execute_ts}"
            )
        )
        logger.info(f"[{strategy_name}] live {timing_mode} entry {entry_sig.upper()} @ {live_candle_ts}")
        await self.entry(symbol, entry_sig, float(live_price))

        hybrid_strategy = str(pending.get('hybrid_strategy') or '').lower()
        if hybrid_strategy:
            self._consume_ut_hybrid_timing_latch(symbol, hybrid_strategy, entry_sig)
        return entry_sig.upper()

    def _expire_utbb_long_setup(self, symbol, current_ts, candle_ms, max_bars=3):
        current_ts = int(current_ts or 0)
        candle_ms = int(candle_ms or 0)
        max_bars = max(1, int(max_bars or 1))
        if current_ts <= 0 or candle_ms <= 0:
            return
        expiry_window = candle_ms * max_bars
        for source in ('ut', 'bb'):
            setup = self._get_utbb_long_setup(symbol, source)
            if not setup:
                continue
            setup_ts = int(setup.get('signal_ts') or 0)
            if setup_ts <= 0 or (current_ts - setup_ts) > expiry_window:
                self._clear_utbb_long_setup(symbol, source)

    def _is_utbb_long_setup_ready(self, symbol, candle_ms, max_bars=3):
        ut_setup = self._get_utbb_long_setup(symbol, 'ut')
        bb_setup = self._get_utbb_long_setup(symbol, 'bb')
        if not ut_setup or not bb_setup:
            return False
        ut_ts = int(ut_setup.get('signal_ts') or 0)
        bb_ts = int(bb_setup.get('signal_ts') or 0)
        candle_ms = int(candle_ms or 0)
        max_bars = max(1, int(max_bars or 1))
        if ut_ts <= 0 or bb_ts <= 0 or candle_ms <= 0:
            return False
        return abs(ut_ts - bb_ts) <= (candle_ms * max_bars)

    def _set_utbb_special_long_state(self, symbol, signal_ts=None):
        self.utbb_special_long_state[symbol] = {
            'active': True,
            'signal_ts': int(signal_ts or 0)
        }

    def _clear_utbb_special_long_state(self, symbol):
        self.utbb_special_long_state.pop(symbol, None)

    def _get_utbb_special_long_state(self, symbol):
        return self.utbb_special_long_state.get(symbol)

    def _is_utbb_special_long_active(self, symbol):
        state = self._get_utbb_special_long_state(symbol)
        return bool(state and state.get('active'))

    def _set_utbb_special_short_state(self, symbol, signal_ts=None, mode='lower_reentry'):
        self.utbb_special_short_state[symbol] = {
            'active': True,
            'signal_ts': int(signal_ts or 0),
            'mode': str(mode or 'lower_reentry')
        }

    def _clear_utbb_special_short_state(self, symbol):
        self.utbb_special_short_state.pop(symbol, None)

    def _get_utbb_special_short_state(self, symbol):
        return self.utbb_special_short_state.get(symbol)

    def _is_utbb_special_short_active(self, symbol):
        state = self._get_utbb_special_short_state(symbol)
        return bool(state and state.get('active'))

    def _get_utbb_special_short_mode(self, symbol):
        state = self._get_utbb_special_short_state(symbol)
        if not state or not state.get('active'):
            return None
        return str(state.get('mode') or 'lower_reentry')

    def _apply_utbb_long_entry_state(self, symbol, raw_hybrid_detail, bb_upper_breakout):
        if bb_upper_breakout:
            self._set_utbb_special_long_state(symbol, raw_hybrid_detail.get('bb_upper_signal_ts'))
            return
        self._clear_utbb_special_long_state(symbol)

    def _apply_utbb_short_entry_state(self, symbol, raw_hybrid_detail, *, special_short_candidate=False, short_rebound_fail_ready=False):
        if special_short_candidate:
            self._set_utbb_special_short_state(symbol, raw_hybrid_detail.get('bb_lower_signal_ts'), mode='lower_reentry')
            return
        if short_rebound_fail_ready:
            self._set_utbb_special_short_state(
                symbol,
                raw_hybrid_detail.get('bb_mid_rebound_fail_signal_ts'),
                mode='mid_rebound_fail'
            )
            return
        self._clear_utbb_special_short_state(symbol)

    def _build_utbb_long_entry_reason(self, strategy_name, *, ut_signal=None, long_pair_source=None, ut_long_fresh=False, bb_long_fresh=False, bb_upper_breakout=False, reentry=False):
        if bb_upper_breakout:
            if reentry:
                return (
                    f"{strategy_name} 숏 청산 후 특수 LONG 재진입"
                    if ut_signal == 'long'
                    else f"{strategy_name} 숏 청산 후 UT 롱상태로 특수 LONG 재진입"
                )
            return (
                f"{strategy_name} UT 롱신호 + BB 상단돌파 -> 특수 LONG 진입"
                if ut_signal == 'long'
                else f"{strategy_name} UT 롱상태 유지 + BB 상단돌파 -> 특수 LONG 진입"
            )

        if long_pair_source == 'same_candle':
            return (
                f"{strategy_name} 숏 청산 후 UT/BB 롱 동시 확인 -> LONG 재진입"
                if reentry else f"{strategy_name} UT/BB 롱 동시 확인 -> LONG 진입"
            )
        if long_pair_source == 'bb_first':
            return (
                f"{strategy_name} 숏 청산 후 저장된 BB 롱 + UT 롱신호 -> LONG 재진입"
                if reentry else f"{strategy_name} 저장된 BB 롱 + UT 롱신호 -> LONG 진입"
            )
        if long_pair_source == 'ut_first':
            return (
                f"{strategy_name} 숏 청산 후 저장된 UT 롱 + BB 롱신호 -> LONG 재진입"
                if reentry else f"{strategy_name} 저장된 UT 롱 + BB 롱신호 -> LONG 진입"
            )
        if ut_long_fresh:
            return (
                f"{strategy_name} 숏 청산 후 UT 롱신호 -> LONG 재진입"
                if reentry else f"{strategy_name} UT 롱신호 -> LONG 진입"
            )
        if bb_long_fresh:
            return f"{strategy_name} BB 롱신호 -> LONG 진입"
        return (
            f"{strategy_name} 숏 청산 후 LONG 재진입"
            if reentry else f"{strategy_name} LONG 진입"
        )

    def _build_utbb_short_entry_note(self, *, ut_signal=None, short_mid_ready=False, short_rebound_fail_ready=False, short_rebound_fail_bull_count=0, special_short_candidate=False):
        if special_short_candidate:
            return (
                "BB 중간선 하락 돌파 + BB 하단 하향 돌파 + UT 숏신호"
                if ut_signal == 'short'
                else "BB 중간선 하락 돌파 + BB 하단 하향 돌파 + UT 숏상태"
            )
        if short_rebound_fail_ready:
            return (
                f"BB 중간선 아래 양봉 {short_rebound_fail_bull_count}개 후 반등 실패 + UT 숏신호"
                if ut_signal == 'short'
                else f"BB 중간선 아래 양봉 {short_rebound_fail_bull_count}개 후 반등 실패 + UT 숏상태"
            )
        if short_mid_ready:
            return (
                "BB 중간선 하락 돌파 + UT 숏신호"
                if ut_signal == 'short'
                else "BB 중간선 하락 돌파 + UT 숏상태"
            )
        return "BB 숏 셋업 + UT 숏신호"
