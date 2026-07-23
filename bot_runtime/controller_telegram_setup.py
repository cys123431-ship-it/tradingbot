"""Telegram command and callback registration for MainController."""

from __future__ import annotations


class TelegramSetupMixin:
    async def _setup_telegram(self):
        markup = self._build_main_keyboard()
        owner_only = self._telegram_owner_only
        text_filter = filters.TEXT & ~filters.COMMAND
        setup_trigger_pattern = r"^/setup(?:@[A-Za-z0-9_]+)?$"
        menu_trigger_pattern = TELEGRAM_MENU_COMMAND_PATTERN
        setup_text_filter = text_filter & ~filters.Regex(r"^/")
        emergency_pattern = TELEGRAM_EMERGENCY_PATTERN

        async def start_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
            await u.message.reply_text("🤖 봇 준비 완료", reply_markup=markup)

        async def strat_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
            if not c.args:
                await u.message.reply_text("사용법: /strat 번호\n1: Signal")
                return
            mode_map = {'1': CORE_ENGINE}
            arg = c.args[0]
            if arg not in mode_map:
                await u.message.reply_text("잘못된 입력입니다. 현재는 `1 (Signal)`만 사용 가능합니다.", parse_mode=ParseMode.MARKDOWN)
                return
            mode = mode_map[arg]
            await self.cfg.update_value(['system_settings', 'active_engine'], mode)
            await self._switch_engine(mode)
            await u.message.reply_text(f"✅ 전략 변경: {mode.upper()}")

        async def log_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
            logs = list(log_buffer)[-15:]
            if logs:
                await u.message.reply_text("\n".join(logs))
            else:
                await u.message.reply_text("📝 최근 로그가 없습니다.")

        async def status_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
            status_text, history_text, snapshot_key = self._build_manual_status_payload()
            self._record_status_snapshot(snapshot_key, history_text)
            await self._reply_markdown_safe(u.message, status_text)

        async def history_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
            limit = 3
            if c.args:
                try:
                    limit = int(c.args[0])
                except ValueError:
                    await u.message.reply_text("사용법: /history 또는 /history 3")
                    return
            limit = max(1, min(limit, 5))
            rows = self.db.get_recent_status_history(limit=limit + 1)
            prev_rows = rows[1:] if len(rows) > 1 else []
            if not prev_rows:
                await u.message.reply_text("📝 지난 상태 이력이 없습니다.")
                return

            await u.message.reply_text(
                f"🕘 최근 상태 이력 {len(prev_rows)}건을 표시합니다. (`/history {limit}`)",
                parse_mode=ParseMode.MARKDOWN
            )
            for created_at, snapshot_text in prev_rows:
                await u.message.reply_text(
                    f"`{created_at}`\n{snapshot_text}",
                    parse_mode=ParseMode.MARKDOWN
                )

        async def close_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
            result = await self.emergency_stop()
            await u.message.reply_text(self._format_emergency_stop_reply(result))

        async def stop_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
            result = await self.emergency_stop()
            await u.message.reply_text(self._format_emergency_stop_reply(result))

        async def stats_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
            daily_count, daily_pnl = self.db.get_daily_stats()
            weekly_count, weekly_pnl = self.db.get_weekly_stats()

            quote_currency = 'KRW' if self.is_upbit_mode() else 'USDT'

            def fmt_stats_pnl(value):
                amount = float(value or 0)
                prefix = '+' if amount >= 0 else ''
                if quote_currency == 'KRW':
                    return f"{prefix}₩{amount:,.0f}"
                return f"{prefix}${amount:.2f}"

            msg = f"""
📊 **매매 통계**

**오늘**
- 거래: {daily_count}건
- 손익: {fmt_stats_pnl(daily_pnl)}

**7일**
- 거래: {weekly_count}건
- 손익: {fmt_stats_pnl(weekly_pnl)}
"""
            await u.message.reply_text(msg.strip(), parse_mode=ParseMode.MARKDOWN)

        def _live_real_risk_cfg():
            return build_live_real_risk_config(self.cfg.get('signal_engine', {}) or {})

        async def _live_real_risk_limits(cfg_for_risk):
            account_equity = None
            equity_source = "reference"
            engine = self.engines.get('signal')
            if engine is not None and hasattr(engine, 'get_balance_info'):
                try:
                    total_balance, free_balance, _ = await engine.get_balance_info()
                    candidate_equity = total_balance if total_balance and total_balance > 0 else free_balance
                    if candidate_equity and candidate_equity > 0:
                        account_equity = float(candidate_equity)
                        equity_source = "actual"
                except Exception as exc:
                    logger.debug("LIVE_REAL risk equity lookup fallback to reference: %s", exc)
            return resolve_live_small_cap_limits(cfg_for_risk, account_equity), equity_source

        async def _set_live_real_risk_pct_from_user(requested_fraction, *, source):
            cfg_for_risk = _live_real_risk_cfg()
            current_fraction = _live_real_risk_fraction_from_cfg(cfg_for_risk)
            try:
                state = load_live_real_risk_state()
            except LiveRealRiskStateUnreadable as exc:
                return False, (
                    "🔴 실거래 리스크 상태 파일을 신뢰할 수 없어 신규 거래와 리스크 변경을 차단했습니다.\n"
                    "상태 파일을 복구하거나 운영자가 확인한 뒤 다시 시도하세요.\n"
                    f"오류: {exc}"
                )
            change_cfg = dict(cfg_for_risk)
            change_cfg["_risk_change_source"] = source
            change = apply_live_real_risk_pct_change(
                state,
                current_fraction,
                requested_fraction,
                change_cfg,
            )
            if not change.get("allowed"):
                limits, equity_source = await _live_real_risk_limits(cfg_for_risk)
                text = render_live_real_risk_status_text(
                    cfg_for_risk,
                    state,
                    limits,
                    equity_source=equity_source,
                    notice=(
                        "리스크 상향이 차단되었습니다. KST 기준 하루 상향 변경 한도에 도달했습니다. "
                        "리스크를 낮추는 변경은 계속 허용됩니다."
                    ),
                )
                return False, text

            changed_state = change.get("state") or state
            save_live_real_risk_state(changed_state)
            risk_percent = requested_fraction * 100.0
            max_percent = _live_real_min_max_risk_fraction(cfg_for_risk)[1] * 100.0
            next_cfg = {**cfg_for_risk, "live_real_risk_pct_user": requested_fraction}
            limits, equity_source = await _live_real_risk_limits(next_cfg)
            update_paths = [
                (['signal_engine', 'common_settings', 'live_real_risk_pct_user'], requested_fraction),
                (['signal_engine', 'common_settings', 'risk_per_trade_pct'], risk_percent),
                (['signal_engine', 'common_settings', 'max_risk_per_trade_pct'], max_percent),
                (['signal_engine', 'common_settings', 'max_real_position_notional_usdt'], limits['max_position_notional_usdt']),
                (['signal_engine', 'common_settings', 'max_real_loss_per_trade_usdt'], limits['max_loss_per_trade_usdt']),
                (['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'live_real_risk_pct_user'], requested_fraction),
                (['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'risk_per_trade_percent'], risk_percent),
                (['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'max_risk_per_trade_percent'], max_percent),
                (['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'max_risk_per_trade_usdt'], limits['max_loss_per_trade_usdt']),
                (['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'risk_budget_tracks_account_equity'], True),
            ]
            for path, value in update_paths:
                await self.cfg.update_value(path, value)
            engine = self._reset_signal_engine_runtime_state(reset_entry_cache=True)
            if engine and not getattr(engine, "running", False):
                engine.start()
            text = render_live_real_risk_status_text(
                next_cfg,
                changed_state,
                limits,
                equity_source=equity_source,
                notice=f"1회 리스크가 {risk_percent:.2f}%로 변경되었습니다. ({change.get('reason')})",
            )
            return True, text

        async def _live_real_risk_status_text(*, menu=False, notice=None):
            cfg_for_risk = _live_real_risk_cfg()
            try:
                state = load_live_real_risk_state()
            except LiveRealRiskStateUnreadable as exc:
                return (
                    "🔴 실거래 리스크 상태: 거래 차단\n"
                    "누적 손실 상태 파일을 신뢰할 수 없어 fail-closed로 신규 거래를 막았습니다.\n"
                    "상태 파일을 복구하거나 운영자가 확인해야 합니다.\n"
                    f"오류: {exc}"
                )
            limits, equity_source = await _live_real_risk_limits(cfg_for_risk)
            return render_live_real_risk_status_text(
                cfg_for_risk,
                state,
                limits,
                equity_source=equity_source,
                menu=menu,
                notice=notice,
            )

        async def risk_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
            args = list(getattr(c, "args", []) or [])
            cfg_for_risk = _live_real_risk_cfg()
            if not args:
                await u.message.reply_text(await _live_real_risk_status_text())
                return
            try:
                requested_fraction = parse_live_real_risk_pct_input(args[0], cfg_for_risk)
            except ValueError as exc:
                await u.message.reply_text(f"Invalid /risk value: {exc}\nUsage: /risk 0.5, /risk 1, /risk 10, /risk safe")
                return
            _, text = await _set_live_real_risk_pct_from_user(
                requested_fraction,
                source="command",
            )
            await u.message.reply_text(text)

        def _parse_bool_mode(value):
            text = str(value or '').strip().lower()
            if text in {'on', '1', 'true', 'yes', 'enable', 'enabled', 'start'}:
                return True
            if text in {'off', '0', 'false', 'no', 'disable', 'disabled', 'stop'}:
                return False
            return None

        def _format_warning_block(strategy_key):
            sig_cfg = self.cfg.get('signal_engine', {}) or {}
            common_cfg = sig_cfg.get('common_settings', {}) or {}
            strategy_params = sig_cfg.get('strategy_params', {}) or {}
            active_strategy = str(strategy_params.get('active_strategy', 'utbot') or 'utbot').lower()
            coin_cfg = sig_cfg.get('coin_selector', {}) or {}
            micro_cfg = normalize_micro_auto_config(sig_cfg.get('micro_auto', {}) if isinstance(sig_cfg.get('micro_auto', {}), dict) else {})
            watchlist = self.get_active_watchlist()
            engine = self.engines.get('signal')
            active_symbols = set(engine.active_symbols or set()) if engine else set()
            cached_positions = getattr(engine, 'all_positions_cache', None) if engine else None
            direction = self.get_effective_trade_direction()
            scanner_on = bool(common_cfg.get('scanner_enabled', False))
            selector_on = bool(coin_cfg.get('enabled', False))
            custom_on = bool(coin_cfg.get('custom_universe_enabled', False))
            warnings = []

            if self.cfg.get('system_settings', {}).get('active_engine', CORE_ENGINE) != CORE_ENGINE:
                warnings.append("active engine이 Signal이 아닙니다.")
            if self.is_paused:
                warnings.append("봇이 PAUSE 상태입니다.")
            if str(direction or 'both').lower() != 'both':
                warnings.append(f"방향필터 {str(direction).upper()}: 반대 방향 신호는 차단됩니다.")
            if strategy_key == 'utbot' and active_strategy != 'utbot':
                warnings.append(f"현재 전략은 {active_strategy.upper()}입니다. UTBot ON을 눌러 전환하세요.")
            if strategy_key == 'utbreak' and active_strategy not in UTBREAKOUT_STRATEGIES:
                warnings.append(f"현재 전략은 {active_strategy.upper()}입니다. UTBreak ON을 눌러 전환하세요.")
            if strategy_key == 'utbreak' and scanner_on and selector_on and not custom_on:
                warnings.append("코인 자동 선택 ON: Binance Futures 전체 후보 중 lock-in 모드입니다.")
            elif strategy_key == 'utbreak' and scanner_on and selector_on and custom_on:
                warnings.append("AUTO 후보 스캔 ON: 입력한 후보군 안에서 lock-in 모드입니다.")
            elif strategy_key in {'utbot', 'utbreak'} and scanner_on:
                warnings.append("scanner ON: watchlist 직접 진입이 아니라 후보 lock-in 방식입니다.")
            if strategy_key in {'utbot', 'utbreak'} and selector_on and not scanner_on:
                warnings.append("CoinSelector ON: 전략 메뉴의 단일코인/직접감시와 충돌할 수 있습니다.")
            if strategy_key in {'utbot', 'utbreak'} and custom_on and not scanner_on:
                warnings.append("Custom universe ON: 지정 코인 후보 스캔 모드입니다.")
            if strategy_key == 'utbreak' and micro_cfg.get('enabled') and (micro_cfg.get('dry_run') or not micro_cfg.get('live_enabled')):
                warnings.append("Micro Auto dry-run/live-lock: 조건이 맞아도 주문을 보내지 않습니다.")
            if not watchlist:
                warnings.append("watchlist가 비어 있습니다.")
            elif engine and not bool(common_cfg.get('scanner_enabled', False)):
                missing = [symbol for symbol in watchlist if symbol not in active_symbols]
                if missing and active_symbols:
                    warnings.append(f"watchlist와 active_symbols가 다릅니다: {', '.join(missing[:3])}")
            if isinstance(cached_positions, set) and cached_positions and watchlist:
                other_positions = sorted(set(cached_positions) - set(watchlist))
                if other_positions:
                    warnings.append(f"다른 포지션 보유 감지: {', '.join(other_positions[:3])}")

            if not warnings:
                return "경고: 없음"
            return "경고:\n" + "\n".join(f"- {item}" for item in warnings[:8])

        def _format_common_strategy_summary(strategy_key):
            sig_cfg = self.cfg.get('signal_engine', {}) or {}
            common_cfg = sig_cfg.get('common_settings', {}) or {}
            strategy_params = sig_cfg.get('strategy_params', {}) or {}
            active_strategy = str(strategy_params.get('active_strategy', 'utbot') or 'utbot').lower()
            watchlist = self.get_active_watchlist()
            coin_cfg = sig_cfg.get('coin_selector', {}) or {}
            micro_cfg = normalize_micro_auto_config(sig_cfg.get('micro_auto', {}) if isinstance(sig_cfg.get('micro_auto', {}), dict) else {})
            scanner_on = bool(common_cfg.get('scanner_enabled', False))
            selector_on = bool(coin_cfg.get('enabled', False))
            custom_on = bool(coin_cfg.get('custom_universe_enabled', False))
            if scanner_on and selector_on and not custom_on:
                coin_mode = '코인 자동 선택'
                if self.get_exchange_mode() == BINANCE_MAINNET and bool(coin_cfg.get('include_tradifi_universe', True)):
                    coin_mode += ' + TradFi'
            elif scanner_on and selector_on and custom_on:
                coin_mode = 'AUTO 후보'
            elif scanner_on:
                coin_mode = 'scanner'
            else:
                coin_mode = '직접감시'
            return "\n".join([
                f"전략: `{active_strategy.upper()}`",
                f"매매: `{'PAUSE' if self.is_paused else 'RUNNING'}` | 방향 `{self.get_effective_trade_direction().upper()}`",
                f"코인: `{', '.join(watchlist) if watchlist else '없음'}`",
                f"코인선택: `{coin_mode}`",
                f"레버리지: `{int(float(common_cfg.get('leverage', 10) or 10))}x`",
                f"TF: 진입 `{common_cfg.get('entry_timeframe', common_cfg.get('timeframe', '15m'))}` / 청산 `{common_cfg.get('exit_timeframe', '4h')}`",
                f"TP/SL: TP `{'ON' if common_cfg.get('take_profit_enabled', True) else 'OFF'} {float(common_cfg.get('target_roe_pct', 20) or 20):.1f}%` / SL `{'ON' if common_cfg.get('stop_loss_enabled', True) else 'OFF'} {float(common_cfg.get('stop_loss_pct', 10) or 10):.1f}%`",
                f"Scanner/CoinSelector: `{'ON' if scanner_on else 'OFF'}` / `{'ON' if selector_on else 'OFF'}`",
                f"Micro Auto: `{'ON' if micro_cfg.get('enabled') else 'OFF'}`",
                _format_warning_block(strategy_key),
            ])

        async def _ensure_signal_engine_active():
            if self.cfg.get('system_settings', {}).get('active_engine', CORE_ENGINE) != CORE_ENGINE:
                await self.cfg.update_value(['system_settings', 'active_engine'], CORE_ENGINE)
                await self._switch_engine(CORE_ENGINE)

        async def _activate_utbot_strategy():
            await _ensure_signal_engine_active()
            await self._return_signal_engine_to_utbot()
            return "✅ UTBot 전략 ON. UTBreak/scanner/CoinSelector/Micro Auto를 OFF로 정리했습니다."

        async def _activate_utbreak_strategy():
            await _ensure_signal_engine_active()
            self.is_paused = True
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'active_strategy'], UTBOT_ADAPTIVE_TIMEFRAME_STRATEGY)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'entry_strategy'], ENTRY_STRATEGY_UT_BREAKOUT)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'relative_strength_pullback_trend_live_enabled'], False)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'selection_mode'], 'auto')
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'auto_select_enabled'], True)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'adaptive_timeframe_enabled'], True)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'fixed_take_profit_enabled'], True)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'partial_take_profit_enabled'], True)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'partial_take_profit_r_multiple'], 1.0)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'partial_take_profit_ratio'], 0.20)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'second_take_profit_enabled'], True)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'second_take_profit_r_multiple'], 3.50)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'second_take_profit_ratio'], 0.40)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'atr_trailing_enabled'], True)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'shadow_runner_exit_enabled'], True)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'runner_exit_enabled'], True)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'runner_chandelier_enabled'], True)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'quality_score_v2_enabled'], True)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'dynamic_take_profit_enabled'], True)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'tp1_breakeven_enabled'], True)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'tp1_breakeven_wait_for_partial'], True)
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'fixed_symbol_mode_enabled'], False)
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'fixed_symbol'], '')
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'custom_universe_enabled'], False)
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'enabled'], True)
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'selection_quality_enabled'], True)
            await self.cfg.update_value(['signal_engine', 'common_settings', 'scanner_enabled'], True)
            await self.cfg.update_value(['signal_engine', 'micro_auto', 'enabled'], False)
            engine = self._reset_signal_engine_runtime_state(
                reset_entry_cache=True,
                reset_exit_cache=True,
                reset_stateful_strategy=True
            )
            if engine:
                engine.scanner_active_symbol = None
                engine.active_symbols.clear()
                if not engine.running:
                    engine.start()
            self.is_paused = False
            return "✅ UTBreak ON. CoinSelector + scanner + Set AUTO + Adaptive TF 자동 운용을 한 번에 켰습니다."

        async def _activate_relative_strength_pullback_strategy():
            await _ensure_signal_engine_active()
            self.is_paused = False
            rsp_cfg = default_relative_strength_pullback_config()
            rsp_cfg['entry_strategy'] = ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND
            rsp_cfg['relative_strength_pullback_trend_shadow_enabled'] = True
            rsp_cfg['relative_strength_pullback_trend_live_enabled'] = True
            rsp_cfg['relative_strength_pullback_trend_paper_enabled'] = False
            rsp_cfg['forced_direction'] = None
            rsp_cfg['direction_source'] = 'RSPT-v3 BTC/ETH/alt/vol residual strength'
            rsp_cfg['require_internal_trend_confirmation'] = True
            rsp_cfg['rspt_v2_enabled'] = True
            rsp_cfg['independent_direction_enabled'] = True
            await self.cfg.update_value(
                ['signal_engine', 'strategy_params', 'active_strategy'],
                ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND,
            )
            await self.cfg.update_value(
                ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'entry_strategy'],
                ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND,
            )
            await self.cfg.update_value(
                ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'relative_strength_pullback_trend'],
                rsp_cfg,
            )
            await self.cfg.update_value(
                ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'relative_strength_pullback_trend_shadow_enabled'],
                True,
            )
            await self.cfg.update_value(
                ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'relative_strength_pullback_trend_live_enabled'],
                True,
            )
            await self.cfg.update_value(
                ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'relative_strength_pullback_trend_paper_enabled'],
                False,
            )
            await self.cfg.update_value(
                ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'adaptive_timeframe_enabled'],
                False,
            )
            await self.cfg.update_value(
                ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'selection_mode'],
                'auto',
            )
            await self.cfg.update_value(
                ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'auto_select_enabled'],
                True,
            )
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'fixed_symbol_mode_enabled'], False)
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'fixed_symbol'], '')
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'custom_universe_enabled'], False)
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'enabled'], True)
            await self.cfg.update_value(['signal_engine', 'common_settings', 'scanner_enabled'], True)
            await self.cfg.update_value(['signal_engine', 'micro_auto', 'enabled'], False)
            engine = self._reset_signal_engine_runtime_state(
                reset_entry_cache=True,
                reset_exit_cache=True,
                reset_stateful_strategy=True,
            )
            if engine:
                engine.scanner_active_symbol = None
                engine.active_symbols.clear()
                engine.relative_strength_pullback_states = {}
                engine.relative_strength_pullback_last_decisions = {}
                engine.relative_strength_pullback_eval_cache = {}
                if not engine.running:
                    engine.start()
            return (
                "RSPT-v2 ON. BTC/ETH residual strength, independent LONG/SHORT direction, "
                "prior impulse + pullback + reclaim, shared L2 gate and QH confirmation are enabled."
            )

        async def _deactivate_relative_strength_pullback_strategy():
            await _ensure_signal_engine_active()
            await self.cfg.update_value(
                ['signal_engine', 'strategy_params', 'active_strategy'],
                UTBOT_FILTERED_BREAKOUT_STRATEGY,
            )
            await self.cfg.update_value(
                ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'entry_strategy'],
                ENTRY_STRATEGY_UT_BREAKOUT,
            )
            await self.cfg.update_value(
                ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'relative_strength_pullback_trend_live_enabled'],
                False,
            )
            engine = self._reset_signal_engine_runtime_state(
                reset_entry_cache=True,
                reset_exit_cache=True,
                reset_stateful_strategy=True,
            )
            if engine:
                engine.relative_strength_pullback_states = {}
                engine.relative_strength_pullback_last_decisions = {}
                engine.relative_strength_pullback_eval_cache = {}
            return "RelativeStrengthPullbackTrend OFF. active strategy returned to UTBreakout default."

        async def _activate_dual_alpha_strategy():
            await _ensure_signal_engine_active()
            self.is_paused = False
            rsp_cfg = default_relative_strength_pullback_config()
            rsp_cfg['entry_strategy'] = ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND
            rsp_cfg['relative_strength_pullback_trend_shadow_enabled'] = True
            rsp_cfg['relative_strength_pullback_trend_live_enabled'] = True
            rsp_cfg['relative_strength_pullback_trend_paper_enabled'] = False
            await self.cfg.update_value(
                ['signal_engine', 'strategy_params', 'active_strategy'],
                DUAL_ALPHA_STRATEGY,
            )
            await self.cfg.update_value(
                ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'entry_strategy'],
                ENTRY_STRATEGY_UT_BREAKOUT,
            )
            await self.cfg.update_value(
                ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'relative_strength_pullback_trend'],
                rsp_cfg,
            )
            await self.cfg.update_value(
                ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'relative_strength_pullback_trend_shadow_enabled'],
                True,
            )
            await self.cfg.update_value(
                ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'relative_strength_pullback_trend_live_enabled'],
                True,
            )
            await self.cfg.update_value(
                ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'relative_strength_pullback_trend_paper_enabled'],
                False,
            )
            await self.cfg.update_value(
                ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'adaptive_timeframe_enabled'],
                True,
            )
            await self.cfg.update_value(
                ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'selection_mode'],
                'auto',
            )
            await self.cfg.update_value(
                ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'auto_select_enabled'],
                True,
            )
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'fixed_symbol_mode_enabled'], False)
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'fixed_symbol'], '')
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'custom_universe_enabled'], False)
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'enabled'], True)
            await self.cfg.update_value(['signal_engine', 'common_settings', 'scanner_enabled'], True)
            await self.cfg.update_value(['signal_engine', 'micro_auto', 'enabled'], False)
            engine = self._reset_signal_engine_runtime_state(
                reset_entry_cache=True,
                reset_exit_cache=True,
                reset_stateful_strategy=True,
            )
            if engine:
                engine.scanner_active_symbol = None
                engine.active_symbols.clear()
                engine.relative_strength_pullback_states = {}
                engine.relative_strength_pullback_last_decisions = {}
                engine.relative_strength_pullback_eval_cache = {}
                engine.dual_alpha_last_status = {}
                if not engine.running:
                    engine.start()
            return (
                "DUAL Alpha ON. UTBreakout + RSPT are both watched; "
                "4h signal + 1d HTF UT direction is used as the DUAL direction filter. "
                "one selected plan uses the existing risk/TP/SL/order path with no extra small-cap limit."
            )

        async def _deactivate_dual_alpha_strategy():
            notice = await _activate_utbreak_strategy()
            engine = self.engines.get('signal')
            if engine:
                engine.relative_strength_pullback_eval_cache = {}
                engine.dual_alpha_last_status = {}
            return f"DUAL Alpha OFF. {notice}"

        async def _activate_qh_flow_strategy():
            await _ensure_signal_engine_active()
            self.is_paused = False
            current = self.cfg.get('signal_engine', {}).get('strategy_params', {}).get('UTBotFilteredBreakoutV1', {})
            qh_cfg = default_qh_flow_config()
            if isinstance(current, dict) and isinstance(current.get('qh_flow'), dict):
                qh_cfg.update(current.get('qh_flow'))
            qh_cfg['qh_flow_enabled'] = True
            qh_cfg['qh_flow_live_enabled'] = True
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'active_strategy'], QH_FLOW_STRATEGY)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'qh_flow'], qh_cfg)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'qh_flow_live_enabled'], True)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'l2_gate_enabled'], True)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'relative_strength_pullback_trend_live_enabled'], False)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'adaptive_timeframe_enabled'], False)
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'fixed_symbol_mode_enabled'], False)
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'fixed_symbol'], '')
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'custom_universe_enabled'], False)
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'enabled'], True)
            await self.cfg.update_value(['signal_engine', 'common_settings', 'scanner_enabled'], True)
            await self.cfg.update_value(['signal_engine', 'micro_auto', 'enabled'], False)
            engine = self._reset_signal_engine_runtime_state(reset_entry_cache=True, reset_exit_cache=True, reset_stateful_strategy=True)
            if engine:
                engine.qh_flow_signal_cache = {}
                engine.qh_flow_last_status = {}
                engine.l2_gate_cache = {}
                if not engine.running:
                    engine.start()
            return 'QH-Flow ON. Quarter-hour first-10-second taker flow, L2 gate, funding/basis crowding and live order path are enabled.'

        async def _deactivate_qh_flow_strategy():
            notice = await _activate_utbreak_strategy()
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'qh_flow_live_enabled'], False)
            engine = self.engines.get('signal')
            if engine:
                engine.qh_flow_signal_cache = {}
                engine.qh_flow_last_status = {}
            return f'QH-Flow OFF. {notice}'

        async def _activate_triple_alpha_strategy():
            await _ensure_signal_engine_active()
            self.is_paused = False
            rsp_cfg = default_relative_strength_pullback_config()
            rsp_cfg.update({
                'entry_strategy': ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND,
                'relative_strength_pullback_trend_shadow_enabled': True,
                'relative_strength_pullback_trend_live_enabled': True,
                'relative_strength_pullback_trend_paper_enabled': False,
                'rspt_v2_enabled': True,
                'independent_direction_enabled': True,
                'direction_source': 'RSPT-v3 BTC/ETH/alt/vol residual strength',
                'forced_direction': None,
            })
            qh_cfg = default_qh_flow_config()
            qh_cfg['qh_flow_enabled'] = True
            qh_cfg['qh_flow_live_enabled'] = True
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'active_strategy'], TRIPLE_ALPHA_STRATEGY)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'entry_strategy'], ENTRY_STRATEGY_UT_BREAKOUT)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'relative_strength_pullback_trend'], rsp_cfg)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'relative_strength_pullback_trend_live_enabled'], True)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'qh_flow'], qh_cfg)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'qh_flow_live_enabled'], True)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'l2_gate_enabled'], True)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'adaptive_timeframe_enabled'], True)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'selection_mode'], 'auto')
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'auto_select_enabled'], True)
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'fixed_symbol_mode_enabled'], False)
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'fixed_symbol'], '')
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'custom_universe_enabled'], False)
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'enabled'], True)
            await self.cfg.update_value(['signal_engine', 'common_settings', 'scanner_enabled'], True)
            await self.cfg.update_value(['signal_engine', 'micro_auto', 'enabled'], False)
            engine = self._reset_signal_engine_runtime_state(reset_entry_cache=True, reset_exit_cache=True, reset_stateful_strategy=True)
            if engine:
                engine.qh_flow_signal_cache = {}
                engine.qh_flow_last_status = {}
                engine.triple_alpha_last_status = {}
                engine.relative_strength_pullback_eval_cache = {}
                if not engine.running:
                    engine.start()
            return 'TRIPLE Alpha ON. UTBreak + RSPT-v3 + QH-Flow v2 are evaluated independently; conflicts block and 1/2/3 confirmations scale risk.'

        async def _deactivate_triple_alpha_strategy():
            notice = await _activate_utbreak_strategy()
            engine = self.engines.get('signal')
            if engine:
                engine.triple_alpha_last_status = {}
                engine.qh_flow_signal_cache = {}
            return f'TRIPLE Alpha OFF. {notice}'

        async def _activate_liquidation_exhaustion_reversal_strategy():
            await _ensure_signal_engine_active()
            self.is_paused = False
            current = self.cfg.get('signal_engine', {}).get('strategy_params', {}).get('UTBotFilteredBreakoutV1', {})
            lxr_cfg = default_liquidation_exhaustion_reversal_config()
            if isinstance(current, dict) and isinstance(current.get('liquidation_exhaustion_reversal'), dict):
                lxr_cfg.update(current.get('liquidation_exhaustion_reversal'))
            lxr_cfg['enabled'] = True
            lxr_cfg['live_enabled'] = True
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'active_strategy'], LXR_STRATEGY)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'liquidation_exhaustion_reversal'], lxr_cfg)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'liquidation_exhaustion_reversal_live_enabled'], True)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'l2_gate_enabled'], True)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'relative_strength_pullback_trend_live_enabled'], False)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'adaptive_timeframe_enabled'], False)
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'fixed_symbol_mode_enabled'], False)
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'fixed_symbol'], '')
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'custom_universe_enabled'], False)
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'enabled'], True)
            await self.cfg.update_value(['signal_engine', 'common_settings', 'scanner_enabled'], True)
            await self.cfg.update_value(['signal_engine', 'micro_auto', 'enabled'], False)
            engine = self._reset_signal_engine_runtime_state(
                reset_entry_cache=True,
                reset_exit_cache=True,
                reset_stateful_strategy=True,
            )
            if engine:
                engine.liquidation_exhaustion_reversal_last_status = {}
                engine.l2_gate_cache = {}
                engine.l2_gate_history = {}
                if not engine.running:
                    engine.start()
            return (
                'LXR ON. A completed high-volume price shock with falling open interest must '
                'reclaim structure before it can enter through the existing live order and protection path.'
            )

        async def _deactivate_liquidation_exhaustion_reversal_strategy():
            notice = await _activate_utbreak_strategy()
            await self.cfg.update_value(
                ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'liquidation_exhaustion_reversal_live_enabled'],
                False,
            )
            engine = self.engines.get('signal')
            if engine:
                engine.liquidation_exhaustion_reversal_last_status = {}
            return f'LXR OFF. {notice}'



        def _configured_quad_alpha_strategies():
            current = self.cfg.get('signal_engine', {}).get('strategy_params', {}).get(
                'UTBotFilteredBreakoutV1', {}
            )
            raw_value = current.get('quad_alpha_enabled_strategies') if isinstance(current, dict) else None
            return normalize_quad_alpha_enabled_strategies(raw_value)

        async def _activate_quad_alpha_strategy(enabled_strategies=None):
            await _ensure_signal_engine_active()
            self.is_paused = True
            enabled_strategies = normalize_quad_alpha_enabled_strategies(
                list(QUAD_ALPHA_BRANCH_ORDER) if enabled_strategies is None else enabled_strategies
            )
            if not enabled_strategies:
                raise ValueError('At least one strategy must remain selected.')
            branch_live = quad_alpha_branch_live_flags(enabled_strategies)
            current = self.cfg.get('signal_engine', {}).get('strategy_params', {}).get('UTBotFilteredBreakoutV1', {})
            rsp_cfg = default_relative_strength_pullback_config()
            if isinstance(current, dict) and isinstance(current.get('relative_strength_pullback_trend'), dict):
                rsp_cfg.update(current.get('relative_strength_pullback_trend'))
            rsp_cfg.update({
                'entry_strategy': ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND,
                'relative_strength_pullback_trend_shadow_enabled': True,
                'relative_strength_pullback_trend_live_enabled': branch_live[
                    ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND
                ],
                'relative_strength_pullback_trend_paper_enabled': False,
                'rspt_v2_enabled': True,
                'rspt_v3_enabled': True,
                'independent_direction_enabled': True,
                'direction_source': 'RSPT-v3 BTC/ETH/alt/vol residual strength',
                'forced_direction': None,
            })
            qh_cfg = default_qh_flow_config()
            if isinstance(current, dict) and isinstance(current.get('qh_flow'), dict):
                qh_cfg.update(current.get('qh_flow'))
            qh_cfg['qh_flow_enabled'] = branch_live[QH_FLOW_STRATEGY]
            qh_cfg['qh_flow_live_enabled'] = branch_live[QH_FLOW_STRATEGY]
            crowd_cfg = default_crowding_unwind_config()
            if isinstance(current, dict) and isinstance(current.get('crowding_unwind'), dict):
                crowd_cfg.update(current.get('crowding_unwind'))
            crowd_cfg['enabled'] = branch_live[CROWDING_UNWIND_STRATEGY]
            crowd_cfg['live_enabled'] = branch_live[CROWDING_UNWIND_STRATEGY]
            lxr_cfg = default_liquidation_exhaustion_reversal_config()
            if isinstance(current, dict) and isinstance(current.get('liquidation_exhaustion_reversal'), dict):
                lxr_cfg.update(current.get('liquidation_exhaustion_reversal'))
            lxr_cfg['enabled'] = branch_live[LXR_STRATEGY]
            lxr_cfg['live_enabled'] = branch_live[LXR_STRATEGY]
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'active_strategy'], QUAD_ALPHA_STRATEGY)
            await self.cfg.update_value(
                ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'quad_alpha_enabled_strategies'],
                list(enabled_strategies),
            )
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'entry_strategy'], ENTRY_STRATEGY_UT_BREAKOUT)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'relative_strength_pullback_trend'], rsp_cfg)
            await self.cfg.update_value(
                ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'relative_strength_pullback_trend_live_enabled'],
                branch_live[ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND],
            )
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'qh_flow'], qh_cfg)
            await self.cfg.update_value(
                ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'qh_flow_live_enabled'],
                branch_live[QH_FLOW_STRATEGY],
            )
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'crowding_unwind'], crowd_cfg)
            await self.cfg.update_value(
                ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'crowding_unwind_live_enabled'],
                branch_live[CROWDING_UNWIND_STRATEGY],
            )
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'liquidation_exhaustion_reversal'], lxr_cfg)
            await self.cfg.update_value(
                ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'liquidation_exhaustion_reversal_live_enabled'],
                branch_live[LXR_STRATEGY],
            )
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'l2_gate_enabled'], True)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'adaptive_timeframe_enabled'], True)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'selection_mode'], 'auto')
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'auto_select_enabled'], True)
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'fixed_symbol_mode_enabled'], False)
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'fixed_symbol'], '')
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'custom_universe_enabled'], False)
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'enabled'], True)
            await self.cfg.update_value(['signal_engine', 'common_settings', 'scanner_enabled'], True)
            await self.cfg.update_value(['signal_engine', 'micro_auto', 'enabled'], False)
            engine = self._reset_signal_engine_runtime_state(
                reset_entry_cache=True,
                reset_exit_cache=False,
                reset_stateful_strategy=False,
            )
            if engine:
                engine.qh_flow_signal_cache = {}
                engine.qh_flow_last_status = {}
                engine.crowding_unwind_last_status = {}
                engine.liquidation_exhaustion_reversal_last_status = {}
                engine.quad_alpha_last_status = {}
                engine.relative_strength_pullback_eval_cache = {}
                engine.l2_gate_cache = {}
                engine.l2_gate_history = {}
                if not engine.running:
                    engine.start()
            self.is_paused = False
            enabled_labels = ', '.join(
                QUAD_ALPHA_BRANCH_LABELS[key] for key in enabled_strategies
            )
            return (
                f'5-Strategy Alpha ON ({len(enabled_strategies)}/5): {enabled_labels}. '
                'Selected strategies are evaluated independently; one valid signal can enter, '
                'direction conflicts block, and confirmations scale risk. Open-position exits '
                'and protection are unchanged.'
            )

        async def _set_quad_alpha_branch_enabled(strategy_key, enabled):
            selected = set(_configured_quad_alpha_strategies())
            if enabled:
                selected.add(strategy_key)
            else:
                selected.discard(strategy_key)
            ordered = [key for key in QUAD_ALPHA_BRANCH_ORDER if key in selected]
            if not ordered:
                return 'At least one strategy must remain selected. Use 5-ALL OFF to stop new entries.'
            return await _activate_quad_alpha_strategy(ordered)

        async def _deactivate_quad_alpha_strategy():
            notice = await _stop_utbreak_trading()
            for key in (
                'relative_strength_pullback_trend_live_enabled',
                'qh_flow_live_enabled',
                'crowding_unwind_live_enabled',
                'liquidation_exhaustion_reversal_live_enabled',
            ):
                await self.cfg.update_value(
                    ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', key],
                    False,
                )
            engine = self.engines.get('signal')
            if engine:
                engine.quad_alpha_last_status = {}
                engine.qh_flow_signal_cache = {}
                engine.crowding_unwind_last_status = {}
                engine.liquidation_exhaustion_reversal_last_status = {}
            return f'5-Strategy Alpha ALL OFF. {notice}'

        async def _activate_crowding_unwind_strategy():
            await _ensure_signal_engine_active()
            self.is_paused = False
            current = self.cfg.get('signal_engine', {}).get('strategy_params', {}).get('UTBotFilteredBreakoutV1', {})
            crowd_cfg = default_crowding_unwind_config()
            if isinstance(current, dict) and isinstance(current.get('crowding_unwind'), dict):
                crowd_cfg.update(current.get('crowding_unwind'))
            crowd_cfg['enabled'] = True
            crowd_cfg['live_enabled'] = True
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'active_strategy'], CROWDING_UNWIND_STRATEGY)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'crowding_unwind'], crowd_cfg)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'crowding_unwind_live_enabled'], True)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'l2_gate_enabled'], True)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'relative_strength_pullback_trend_live_enabled'], False)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'adaptive_timeframe_enabled'], False)
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'fixed_symbol_mode_enabled'], False)
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'fixed_symbol'], '')
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'custom_universe_enabled'], False)
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'enabled'], True)
            await self.cfg.update_value(['signal_engine', 'common_settings', 'scanner_enabled'], True)
            await self.cfg.update_value(['signal_engine', 'micro_auto', 'enabled'], False)
            engine = self._reset_signal_engine_runtime_state(
                reset_entry_cache=True,
                reset_exit_cache=True,
                reset_stateful_strategy=True,
            )
            if engine:
                engine.crowding_unwind_last_status = {}
                engine.l2_gate_cache = {}
                engine.l2_gate_history = {}
                if not engine.running:
                    engine.start()
            return (
                'Funding-OI Crowding Unwind ON. Extreme funding/OI crowding, '
                'price failure, absorption, structure break and dynamic L2 reversal support are required.'
            )

        async def _deactivate_crowding_unwind_strategy():
            notice = await _activate_utbreak_strategy()
            await self.cfg.update_value(
                ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'crowding_unwind_live_enabled'],
                False,
            )
            engine = self.engines.get('signal')
            if engine:
                engine.crowding_unwind_last_status = {}
            return f'Funding-OI Crowding Unwind OFF. {notice}'

        async def _stop_utbreak_trading():
            await _ensure_signal_engine_active()
            self.is_paused = True
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'active_strategy'], UTBOT_FILTERED_BREAKOUT_STRATEGY)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'entry_strategy'], ENTRY_STRATEGY_UT_BREAKOUT)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'relative_strength_pullback_trend_live_enabled'], False)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'selection_mode'], 'manual')
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'auto_select_enabled'], False)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'adaptive_timeframe_enabled'], False)
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'enabled'], False)
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'custom_universe_enabled'], False)
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'fixed_symbol_mode_enabled'], False)
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'fixed_symbol'], '')
            await self.cfg.update_value(['signal_engine', 'common_settings', 'scanner_enabled'], False)
            await self.cfg.update_value(['signal_engine', 'micro_auto', 'enabled'], False)
            engine = self._reset_signal_engine_runtime_state(
                reset_entry_cache=True,
                reset_exit_cache=True,
                reset_stateful_strategy=True
            )
            if engine:
                engine.scanner_active_symbol = None
                engine.active_symbols.clear()
            return "⛔ UTBreak OFF. 자동 코인선택/scanner/Set AUTO/Adaptive TF/Micro Auto를 끄고 매매를 PAUSE 상태로 전환했습니다. 기존 포지션 강제청산은 /stop 입니다."

        async def _set_strategy_coin(symbol_text):
            mode = self.get_exchange_mode()
            if mode == UPBIT_MODE:
                raise ValueError("Upbit 모드에서는 Binance Futures 전략 코인을 설정할 수 없습니다.")
            markets = await asyncio.to_thread(self.exchange.load_markets)
            symbol = self._resolve_futures_watch_symbol_from_markets(
                symbol_text,
                markets,
                exchange_mode=mode,
            )
            try:
                await asyncio.to_thread(self.exchange.fetch_ticker, symbol)
            except Exception:
                raise ValueError(f"유효하지 않은 심볼입니다: {symbol}")
            await self.cfg.update_value(['signal_engine', 'watchlist'], [symbol])
            await self._update_config_value(['exchange_watchlists', mode], [symbol])
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'enabled'], False)
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'custom_universe_enabled'], False)
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'fixed_symbol_mode_enabled'], False)
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'fixed_symbol'], '')
            await self.cfg.update_value(['signal_engine', 'common_settings', 'scanner_enabled'], False)
            await self.cfg.update_value(['signal_engine', 'micro_auto', 'enabled'], False)
            engine = self._reset_signal_engine_runtime_state(
                reset_entry_cache=True,
                reset_exit_cache=True,
                reset_stateful_strategy=True
            )
            if engine:
                engine.active_symbols.clear()
                engine.active_symbols.add(symbol)
                engine.scanner_active_symbol = None
                await engine.prime_symbol_to_next_closed_candle(symbol)
            return symbol

        async def _set_common_leverage(value):
            lev = int(float(value))
            if lev < 1 or lev > 20:
                raise ValueError("레버리지는 1~20 사이 값만 가능합니다.")
            await self.cfg.update_value(['signal_engine', 'common_settings', 'leverage'], lev)
            if self.active_engine:
                await self.active_engine.ensure_market_settings(self._get_current_symbol())
            return lev

        async def _set_common_entry_tf(value):
            valid_tf = ['1m', '2m', '3m', '4m', '5m', '15m', '30m', '1h', '2h', '4h', '6h', '8h', '12h', '1d', '3d', '1w', '1M']
            tf = str(value or '').strip()
            if tf not in valid_tf:
                raise ValueError(f"유효하지 않은 타임프레임입니다. 사용 가능: {', '.join(valid_tf)}")
            await self.cfg.update_value(['signal_engine', 'common_settings', 'timeframe'], tf)
            await self.cfg.update_value(['signal_engine', 'common_settings', 'entry_timeframe'], tf)
            strategy_params = self.cfg.get('signal_engine', {}).get('strategy_params', {})
            active_strategy = str(strategy_params.get('active_strategy', 'utbot') or 'utbot').lower()
            if active_strategy in UTBREAKOUT_STRATEGIES:
                await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'entry_timeframe'], tf)
            self._reset_signal_engine_runtime_state(reset_entry_cache=True, reset_stateful_strategy=True)
            return tf

        async def _set_common_exit_tf(value):
            valid_tf = ['1m', '2m', '3m', '4m', '5m', '15m', '30m', '1h', '2h', '4h', '6h', '8h', '12h', '1d', '3d', '1w', '1M']
            tf = str(value or '').strip()
            if tf not in valid_tf:
                raise ValueError(f"유효하지 않은 타임프레임입니다. 사용 가능: {', '.join(valid_tf)}")
            await self.cfg.update_value(['signal_engine', 'common_settings', 'exit_timeframe'], tf)
            strategy_params = self.cfg.get('signal_engine', {}).get('strategy_params', {})
            active_strategy = str(strategy_params.get('active_strategy', 'utbot') or 'utbot').lower()
            if active_strategy in UTBREAKOUT_STRATEGIES:
                await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'exit_timeframe'], tf)
            self._reset_signal_engine_runtime_state(reset_exit_cache=True)
            return tf

        async def _set_take_profit(value):
            mode = _parse_bool_mode(value)
            common_cfg = self.cfg.get('signal_engine', {}).get('common_settings', {})
            if mode is not None:
                curr_sl = bool(common_cfg.get('stop_loss_enabled', common_cfg.get('tp_sl_enabled', True)))
                await self.cfg.update_value(['signal_engine', 'common_settings', 'take_profit_enabled'], mode)
                await self.cfg.update_value(['signal_engine', 'common_settings', 'tp_sl_enabled'], bool(mode or curr_sl))
                await self._sync_signal_protection_orders()
                return f"목표 ROE 자동청산 {'ON' if mode else 'OFF'}"
            roe = float(value)
            if roe < 0:
                raise ValueError("목표 ROE는 0 이상으로 입력하세요.")
            await self.cfg.update_value(['signal_engine', 'common_settings', 'target_roe_pct'], roe)
            await self._sync_signal_protection_orders()
            return f"목표 ROE {roe:.1f}%"

        async def _set_stop_loss(value):
            mode = _parse_bool_mode(value)
            common_cfg = self.cfg.get('signal_engine', {}).get('common_settings', {})
            if mode is not None:
                curr_tp = bool(common_cfg.get('take_profit_enabled', common_cfg.get('tp_sl_enabled', True)))
                await self.cfg.update_value(['signal_engine', 'common_settings', 'stop_loss_enabled'], mode)
                await self.cfg.update_value(['signal_engine', 'common_settings', 'tp_sl_enabled'], bool(curr_tp or mode))
                await self._sync_signal_protection_orders()
                return f"손절 자동청산 {'ON' if mode else 'OFF'}"
            sl = float(value)
            if sl < 0:
                raise ValueError("손절 비율은 0 이상으로 입력하세요.")
            await self.cfg.update_value(['signal_engine', 'common_settings', 'stop_loss_pct'], sl)
            await self._sync_signal_protection_orders()
            return f"손절 {sl:.1f}%"

        async def _set_utbot_params(raw_text):
            parts = str(raw_text or '').replace(' ', '').split(',')
            if len(parts) not in (2, 3):
                raise ValueError("형식: key,atr,on/off (예: 1,10,off)")
            key_value = float(parts[0])
            atr_period = int(parts[1])
            if key_value <= 0 or atr_period < 1:
                raise ValueError("key는 0보다 커야 하고 ATR 기간은 1 이상이어야 합니다.")
            use_ha = self.cfg.get('signal_engine', {}).get('strategy_params', {}).get('UTBot', {}).get('use_heikin_ashi', False)
            if len(parts) == 3:
                parsed = _parse_bool_mode(parts[2])
                if parsed is None:
                    raise ValueError("HA 옵션은 on/off 로 입력하세요.")
                use_ha = parsed
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBot', 'key_value'], key_value)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBot', 'atr_period'], atr_period)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBot', 'use_heikin_ashi'], use_ha)
            self._reset_signal_engine_runtime_state(reset_entry_cache=True, reset_stateful_strategy=True)
            return f"UTBot key={key_value:.2f}, ATR={atr_period}, HA={'ON' if use_ha else 'OFF'}"

        async def _set_utbot_rsi_momentum(value):
            mode = _parse_bool_mode(value)
            if mode is None:
                raise ValueError("on/off 로 입력하세요.")
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBot', 'rsi_momentum_filter_enabled'], mode)
            self._reset_signal_engine_runtime_state(reset_stateful_strategy=True)
            return f"RSI Momentum 보조필터 {'ON' if mode else 'OFF'}"

        async def _set_strategy_pause(paused):
            self.is_paused = bool(paused)
            if not self.is_paused and self.active_engine and not self.active_engine.running:
                self.active_engine.start()
            return "⏸ 매매 일시정지" if self.is_paused else "▶ 매매 재개"

        def _format_utbot_menu_text():
            strategy_params = self.cfg.get('signal_engine', {}).get('strategy_params', {}) or {}
            ut_cfg = strategy_params.get('UTBot', {}) if isinstance(strategy_params.get('UTBot', {}), dict) else {}
            filter_pack = ut_cfg.get('filter_pack', {}) if isinstance(ut_cfg.get('filter_pack', {}), dict) else {}
            entry_pack = filter_pack.get('entry', {}) if isinstance(filter_pack.get('entry', {}), dict) else {}
            exit_pack = filter_pack.get('exit', {}) if isinstance(filter_pack.get('exit', {}), dict) else {}
            return f"""
🧭 **UTBOT 전략 메뉴**

{_format_common_strategy_summary('utbot')}

UTBot:
- key `{float(ut_cfg.get('key_value', 1.0) or 1.0):.2f}` / ATR `{int(ut_cfg.get('atr_period', 10) or 10)}` / HA `{'ON' if ut_cfg.get('use_heikin_ashi') else 'OFF'}`
- RSI Momentum 보조필터 `{'ON' if ut_cfg.get('rsi_momentum_filter_enabled') else 'OFF'}`
- 진입 필터 `{format_utbot_filter_pack_selected(normalize_utbot_filter_pack_selected(entry_pack.get('selected', [])))}` / `{format_utbot_filter_pack_logic(entry_pack.get('logic', 'and'))}`
- 청산 필터 `{format_utbot_filter_pack_selected(normalize_utbot_filter_pack_selected(exit_pack.get('selected', [])))}` / `{format_utbot_filter_pack_logic(exit_pack.get('logic', 'and'))}`

명령:
`/utbot on`, `/utbot coin EWY`, `/utbot lev 10`, `/utbot tf 15m`, `/utbot exit_tf 1h`
`/utbot target 20`, `/utbot target off`, `/utbot stop 10`, `/utbot ut 1,10,off`, `/utbot rsi on`
""".strip()

        def _build_utbot_keyboard():
            return InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("UTBot ON", callback_data="utbot:on"),
                    InlineKeyboardButton("일시정지", callback_data="utbot:pause"),
                    InlineKeyboardButton("재개", callback_data="utbot:resume")
                ],
                [
                    InlineKeyboardButton("코인 선택", callback_data="utbot:coin"),
                    InlineKeyboardButton("Lev 5x", callback_data="utbot:lev:5"),
                    InlineKeyboardButton("Lev 10x", callback_data="utbot:lev:10")
                ],
                [
                    InlineKeyboardButton("진입 15m", callback_data="utbot:tf:15m"),
                    InlineKeyboardButton("진입 1h", callback_data="utbot:tf:1h"),
                    InlineKeyboardButton("청산 1h", callback_data="utbot:exit_tf:1h")
                ],
                [
                    InlineKeyboardButton("TP ON", callback_data="utbot:target:on"),
                    InlineKeyboardButton("TP OFF", callback_data="utbot:target:off"),
                    InlineKeyboardButton("SL ON", callback_data="utbot:stop:on"),
                    InlineKeyboardButton("SL OFF", callback_data="utbot:stop:off")
                ],
                [
                    InlineKeyboardButton("UT 설정", callback_data="utbot:ut"),
                    InlineKeyboardButton("RSI보조 ON", callback_data="utbot:rsi:on"),
                    InlineKeyboardButton("RSI보조 OFF", callback_data="utbot:rsi:off")
                ],
                [
                    InlineKeyboardButton("상태", callback_data="utbot:status"),
                    InlineKeyboardButton("새로고침", callback_data="utbot:menu")
                ]
            ])

        async def _edit_utbot_menu(query, notice=None):
            text = _format_utbot_menu_text()
            if notice:
                text = f"{notice}\n\n{text}"
            try:
                await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=_build_utbot_keyboard())
            except BadRequest as md_err:
                if "message is not modified" in str(md_err).lower():
                    return
                await query.edit_message_text(str(text).replace("`", "").replace("**", ""), reply_markup=_build_utbot_keyboard())

        async def _send_utbot_menu(message, notice=None):
            text = _format_utbot_menu_text()
            if notice:
                text = f"{notice}\n\n{text}"
            await self._reply_markdown_safe(message, text, reply_markup=_build_utbot_keyboard())

        async def utbot_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
            args = list(getattr(c, 'args', []) or [])
            if not args and u and u.message and u.message.text:
                parts = u.message.text.strip().split()
                args = parts[1:]
            action = str(args[0]).strip().lower() if args else ''
            try:
                if action in {'on', 'enable', 'start'}:
                    await _send_utbot_menu(u.message, await _activate_utbot_strategy())
                    return
                if action == 'pause':
                    await _send_utbot_menu(u.message, await _set_strategy_pause(True))
                    return
                if action in {'resume', 'run'}:
                    await _send_utbot_menu(u.message, await _set_strategy_pause(False))
                    return
                if action in {'coin', 'symbol'} and len(args) > 1:
                    symbol = await _set_strategy_coin(args[1])
                    await _send_utbot_menu(u.message, f"✅ UTBot 코인 선택: `{symbol}`")
                    return
                if action in {'lev', 'leverage'} and len(args) > 1:
                    lev = await _set_common_leverage(args[1])
                    await _send_utbot_menu(u.message, f"✅ 레버리지: {lev}x")
                    return
                if action in {'tf', 'entry_tf', 'timeframe'} and len(args) > 1:
                    tf = await _set_common_entry_tf(args[1])
                    await _send_utbot_menu(u.message, f"✅ 진입 TF: {tf}")
                    return
                if action in {'exit_tf', 'exit'} and len(args) > 1:
                    tf = await _set_common_exit_tf(args[1])
                    await _send_utbot_menu(u.message, f"✅ 청산 TF: {tf}")
                    return
                if action in {'target', 'tp', 'roe'} and len(args) > 1:
                    await _send_utbot_menu(u.message, f"✅ {await _set_take_profit(args[1])}")
                    return
                if action in {'stop', 'sl'} and len(args) > 1:
                    await _send_utbot_menu(u.message, f"✅ {await _set_stop_loss(args[1])}")
                    return
                if action == 'ut' and len(args) > 1:
                    await _send_utbot_menu(u.message, f"✅ {await _set_utbot_params(args[1])}")
                    return
                if action == 'rsi' and len(args) > 1:
                    await _send_utbot_menu(u.message, f"✅ {await _set_utbot_rsi_momentum(args[1])}")
                    return
                if action == 'status':
                    await status_cmd(u, c)
                    return
                await _send_utbot_menu(u.message)
            except Exception as exc:
                await _send_utbot_menu(u.message, f"❌ {exc}")

        async def utbot_callback(u: Update, c: ContextTypes.DEFAULT_TYPE):
            query = u.callback_query
            if not query:
                return
            await query.answer()
            data = str(query.data or '')
            if not data.startswith('utbot:'):
                return
            parts = data.split(':')
            action = parts[1] if len(parts) > 1 else 'menu'
            value = parts[2] if len(parts) > 2 else None
            try:
                if action == 'on':
                    await _edit_utbot_menu(query, await _activate_utbot_strategy())
                    return
                if action == 'pause':
                    await _edit_utbot_menu(query, await _set_strategy_pause(True))
                    return
                if action == 'resume':
                    await _edit_utbot_menu(query, await _set_strategy_pause(False))
                    return
                if action == 'coin':
                    if c and c.user_data is not None:
                        c.user_data['utbot_coin_waiting_for_symbol'] = True
                    await query.edit_message_text("UTBot에서 거래할 코인 1개를 입력하세요. 예: `EWY`", parse_mode=ParseMode.MARKDOWN)
                    return
                if action == 'lev':
                    await _edit_utbot_menu(query, f"✅ 레버리지: {await _set_common_leverage(value)}x")
                    return
                if action == 'tf':
                    await _edit_utbot_menu(query, f"✅ 진입 TF: {await _set_common_entry_tf(value)}")
                    return
                if action == 'exit_tf':
                    await _edit_utbot_menu(query, f"✅ 청산 TF: {await _set_common_exit_tf(value)}")
                    return
                if action == 'target':
                    await _edit_utbot_menu(query, f"✅ {await _set_take_profit(value)}")
                    return
                if action == 'stop':
                    await _edit_utbot_menu(query, f"✅ {await _set_stop_loss(value)}")
                    return
                if action == 'ut':
                    if c and c.user_data is not None:
                        c.user_data['utbot_params_waiting'] = True
                    await query.edit_message_text("UTBot 설정을 입력하세요. 형식: `key,atr,on/off` 예: `1,10,off`", parse_mode=ParseMode.MARKDOWN)
                    return
                if action == 'rsi':
                    await _edit_utbot_menu(query, f"✅ {await _set_utbot_rsi_momentum(value)}")
                    return
                if action == 'status':
                    await query.message.reply_text(_format_utbot_menu_text(), parse_mode=ParseMode.MARKDOWN, reply_markup=_build_utbot_keyboard())
                    return
                await _edit_utbot_menu(query)
            except Exception as exc:
                await _edit_utbot_menu(query, f"❌ {exc}")

        async def legacy_utbot_callback(u: Update, c: ContextTypes.DEFAULT_TYPE):
            query = u.callback_query
            if not query:
                return
            await query.answer()
            data = str(query.data or '')
            if not data.startswith('utbot:'):
                return
            parts = data.split(':')
            action = parts[1] if len(parts) > 1 else 'menu'
            if action == 'status':
                await _send_utbreakout_condition_status_from_callback(query)
                return
            await _edit_integrated_utbreak_notice(query, "UTBot")

        def _format_utbreakout_menu_text():
            sig_cfg = self.cfg.get('signal_engine', {})
            strategy_params = sig_cfg.get('strategy_params', {})
            active_strategy = str(strategy_params.get('active_strategy', 'utbot') or 'utbot').lower()
            cfg = build_default_utbot_filtered_breakout_config()
            raw_cfg = strategy_params.get('UTBotFilteredBreakoutV1', {})
            if isinstance(raw_cfg, dict):
                cfg.update(raw_cfg)
            cfg = apply_stable_utbreak_final_overrides(cfg)
            quad_enabled = normalize_quad_alpha_enabled_strategies(
                cfg.get('quad_alpha_enabled_strategies')
            )
            quad_enabled_labels = ', '.join(
                QUAD_ALPHA_BRANCH_LABELS[key] for key in quad_enabled
            ) or 'NONE'
            coin_cfg = sig_cfg.get('coin_selector', {}) if isinstance(sig_cfg.get('coin_selector', {}), dict) else {}
            common_cfg = sig_cfg.get('common_settings', {}) if isinstance(sig_cfg.get('common_settings', {}), dict) else {}
            watchlist = self.get_active_watchlist()
            first_symbol = watchlist[0] if watchlist else 'BTC/USDT'
            engine = self.engines.get('signal')
            diag = {}
            if engine:
                diag = engine.last_utbot_filtered_breakout_status.get(first_symbol, {}) or {}
            active_label = "ON" if active_strategy in UTBREAKOUT_STRATEGIES else f"OFF ({active_strategy.upper()})"
            if active_strategy == UTBOT_ADAPTIVE_TIMEFRAME_STRATEGY:
                active_label = "ON (ADAPTIVE TF)"
            elif active_strategy == QH_FLOW_STRATEGY:
                active_label = "ON (QH FLOW V2)"
            elif active_strategy == CROWDING_UNWIND_STRATEGY:
                active_label = "ON (CROWDING UNWIND)"
            elif active_strategy == DUAL_ALPHA_STRATEGY:
                active_label = "ON (DUAL ALPHA)"
            elif active_strategy == TRIPLE_ALPHA_STRATEGY:
                active_label = "ON (TRIPLE ALPHA)"
            elif active_strategy == QUAD_ALPHA_STRATEGY:
                active_label = "ON (QUAD ALPHA)"
            last_reason = diag.get('reject_code') or diag.get('accepted_code') or diag.get('reason') or '대기'
            diag_summary = format_utbreakout_diagnostic_summary()
            set_id = normalize_utbreakout_set_id(cfg.get('active_set_id') or cfg.get('profile'), UTBREAKOUT_DEFAULT_SET_ID)
            set_info = get_utbreakout_set_definition(set_id)
            mode_label = 'AUTO' if bool(cfg.get('auto_select_enabled', False)) or str(cfg.get('selection_mode', '')).lower() == 'auto' else 'MANUAL'
            auto_set = diag.get('auto_selected_set_id')
            auto_name = diag.get('auto_selected_set_name')
            auto_reason = diag.get('auto_selection_reason') or '아직 AUTO 분석 기록 없음'
            adaptive_summary = diag.get('adaptive_timeframe_summary') or '아직 Adaptive TF 분석 기록 없음'
            strategy_adaptation_summary = diag.get('strategy_adaptation_summary') or '아직 전략 적응 기록 없음'
            menu_title = (
                'UTBreak 전략 메뉴 (Quad Alpha)'
                if active_strategy == QUAD_ALPHA_STRATEGY
                else 'UTBreak 전략 메뉴 (Triple Alpha)'
                if active_strategy == TRIPLE_ALPHA_STRATEGY
                else 'UTBreak 전략 메뉴 (Crowding Unwind)'
                if active_strategy == CROWDING_UNWIND_STRATEGY
                else 'UTBreak 전략 메뉴 (QH-Flow v2)'
                if active_strategy == QH_FLOW_STRATEGY
                else 'UTBreak 전략 메뉴 (Dual Alpha)'
                if active_strategy == DUAL_ALPHA_STRATEGY
                else 'UTBreak 전략 메뉴 (Adaptive TF)'
                if active_strategy == UTBOT_ADAPTIVE_TIMEFRAME_STRATEGY
                else 'UTBreak 전략 메뉴'
            )
            daily_trade_limit = int(float(cfg.get('max_daily_trades', 5) if cfg.get('max_daily_trades', 5) is not None else 5))
            daily_trade_limit_text = "OFF" if daily_trade_limit <= 0 else f"{daily_trade_limit}회"
            adaptive_on = bool(cfg.get('adaptive_timeframe_enabled')) or active_strategy == UTBOT_ADAPTIVE_TIMEFRAME_STRATEGY
            auto_set_on = bool(cfg.get('auto_select_enabled', False)) or str(cfg.get('selection_mode', '')).lower() == 'auto'
            scanner_on = bool(common_cfg.get('scanner_enabled', False))
            coin_auto_on = bool(coin_cfg.get('enabled', False)) and scanner_on and not bool(coin_cfg.get('fixed_symbol_mode_enabled', False))
            auto_bundle_on = adaptive_on and auto_set_on and coin_auto_on
            bridge_on = bool(
                getattr(
                    engine,
                    'utbreakout_auto_entry_bridge_enabled',
                    True,
                )
            ) if engine else True
            fixed_symbol = coin_cfg.get('fixed_symbol') or ''
            custom_symbols = coin_cfg.get('custom_symbols') or []
            if coin_cfg.get('fixed_symbol_mode_enabled') and fixed_symbol:
                coin_mode = f"단일 `{fixed_symbol}`"
            elif coin_cfg.get('custom_universe_enabled') and custom_symbols:
                coin_mode = f"후보 `{', '.join(custom_symbols[:6])}`"
            elif coin_auto_on:
                scan_scope = str(coin_cfg.get('scan_scope', 'all_allowed') or 'all_allowed').lower()
                scope_label = {
                    'tradfi_only': 'TradFi ONLY',
                    'crypto_only': '순수 코인 ONLY',
                    'all_allowed': '전체 허용',
                }.get(scan_scope, '전체 허용')
                coin_mode = (
                    f"AUTO {scope_label} Top "
                    f"{int(float(coin_cfg.get('top_n', 10) or 10))}"
                )
            else:
                coin_mode = f"Watchlist `{', '.join(watchlist[:6]) if watchlist else '없음'}`"
            partial_enabled = bool(cfg.get('partial_take_profit_enabled', True))
            partial_text = (
                f"{float(cfg.get('partial_take_profit_ratio', 0.20) or 0.20) * 100:.0f}% @ "
                f"{float(cfg.get('partial_take_profit_r_multiple', 1.00) or 1.00):.1f}R"
                if partial_enabled else
                "OFF"
            )
            second_enabled = bool(cfg.get('second_take_profit_enabled', True))
            second_text = (
                f"{float(cfg.get('second_take_profit_ratio', 0.40) or 0.40) * 100:.0f}% @ "
                f"{float(cfg.get('second_take_profit_r_multiple', cfg.get('take_profit_r_multiple', 3.50)) or 3.50):.1f}R"
                if second_enabled else
                "OFF"
            )
            take_profit_text = (
                f"TP1 {partial_text} / TP2 {second_text}"
                if cfg.get('fixed_take_profit_enabled', True) else
                f"부분익절 {partial_text}"
            )
            if cfg.get('dynamic_take_profit_enabled', True):
                take_profit_text += (
                    f" / Dynamic TP2 ON "
                    f"({float(cfg.get('dynamic_tp2_base_r_multiple', 3.20) or 3.20):.1f}~"
                    f"{float(cfg.get('dynamic_tp2_elite_r_multiple', 7.00) or 7.00):.1f}R)"
                )
            if cfg.get('tp1_breakeven_enabled', True):
                take_profit_text += (
                    f" / TP1 BE+{float(cfg.get('tp1_breakeven_offset_r', 0.03) or 0.03):.2f}R"
                )
            trailing_text = (
                f"{float(cfg.get('atr_trailing_multiplier', 3.50) or 3.50):.1f}ATR + Chandelier / "
                f"{float(cfg.get('atr_trailing_activation_r', 1.60) or 1.60):.1f}R부터"
                if cfg.get('atr_trailing_enabled', True) else
                "OFF"
            )
            short_text = (
                f"ON / 리스크 x{float(cfg.get('short_risk_multiplier', 0.5) or 0.5):.2f}, "
                f"ADX≥{float(cfg.get('short_adx_threshold', 25.0) or 25.0):.1f}, "
                "HTF ST/EMA/모멘텀 확인"
                if cfg.get('short_conservative_enabled', True) else
                "OFF"
            )
            adaptive_stack_text = (
                f"Shadow {'ON' if cfg.get('shadow_triple_barrier_enabled', True) else 'OFF'} / "
                f"Runner {'ON' if cfg.get('runner_exit_enabled', False) else 'OFF'} / "
                f"TrendHealth {'ON' if cfg.get('trend_health_enabled', True) else 'OFF'} / "
                f"QualityV2 {'ON' if cfg.get('strategy_quality_enabled', True) else 'OFF'} / "
                f"QScore {'ON' if cfg.get('quality_score_v2_enabled', True) else 'OFF'} / "
                f"Exit {'ON' if cfg.get('adaptive_exit_enabled', True) else 'OFF'} / "
                f"VolTarget {'ON' if cfg.get('volatility_targeting_enabled', True) else 'OFF'} / "
                f"MetaSizing {'ON' if cfg.get('meta_labeling_enabled', True) else 'OFF'} / "
                f"ShortAsym {'ON' if cfg.get('short_asymmetry_enabled', True) else 'OFF'}"
            )
            return f"""
🧭 **{menu_title}**

{_format_common_strategy_summary('utbreak')}

상태: `{active_label}`
5전략 선택: `{len(quad_enabled)}/5` (`{quad_enabled_labels}`)
코인: {coin_mode}
AUTO 묶음: `{'ON' if auto_bundle_on else 'OFF'}` (코인 자동선택 + Set 자동 + Adaptive TF)
Auto Entry Bridge: `{'ON' if bridge_on else 'OFF'}`
제외 티커: `SKHYNIX, HYUNDAI, SAMSUNG` (SKHY ADR perpetual은 허용)
Set: `{mode_label}` / 수동 `Set{set_id} {set_info.get('name')}` / 최근 AUTO `{('Set' + str(auto_set) + ' ' + str(auto_name)) if auto_set else '대기'}`
시간봉: 진입 `{cfg.get('entry_timeframe', '15m')}` / 청산 `{cfg.get('exit_timeframe', cfg.get('entry_timeframe', '15m'))}` / HTF `{cfg.get('htf_timeframe', '1h')}`
Adaptive 최근: `{adaptive_summary}`
리스크: `SL {float(cfg.get('stop_atr_multiplier', 1.5) or 1.5):.1f}ATR` | 익절 `{take_profit_text}` | ATR 트레일 `{trailing_text}`
숏 가드: `{short_text}`
고도화: `{adaptive_stack_text}`
한도: `1회 ${float(cfg.get('max_risk_per_trade_usdt', 1.0) or 1.0):.2f}` / `일손실 ${float(cfg.get('daily_max_loss_usdt', 3.0) or 3.0):.2f}` / `일거래 {daily_trade_limit_text}`

AUTO 최근 선택 이유:
`{auto_reason}`

최근 전략 적응:
`{strategy_adaptation_summary}`

[Trend Continuation]
진입여부: `{diag.get('trend_continuation_entry')}` | 승인: `{diag.get('trend_continuation_accepted')}`
배율: `x{float(diag.get('trend_continuation_risk_multiplier', 1.0) or 1.0):.2f}`
요약: `{diag.get('trend_continuation_summary') or 'n/a'}`

[Set Filter]
배율: `x{float(diag.get('set_filter_risk_multiplier', 1.0) or 1.0):.2f}` (소프트 실패: `{diag.get('set_filter_soft_fail_count', 0)}`개)
실패 요약: `{diag.get('set_filter_soft_fail_summary') or 'n/a'}`

[Direction (Directional Bias)]
판단: `{diag.get('direction_decision') or 'n/a'}`
BTC 4h: `{diag.get('direction_btc_4h_symbol') or 'n/a'}` | BTC 1d: `{diag.get('direction_btc_1d_symbol') or 'n/a'}`
심볼 1h: `{diag.get('direction_symbol_1h_symbol') or 'n/a'}`

최근 진단({first_symbol}): `{last_reason}`
진단 요약:
```
{diag_summary}
```

명령:
`/utbreak on`, `/utbreak watch BTC ETH AAPL`, `/utbreak coin EWY`, `/utbreak autoscan on BTC ETH`
`/utbreak auto on` / `auto off` - 코인선택 포함 AUTO 묶음 ON/OFF
`/utbreak set 57`, `/utbreak risk 5`, `/utbreak dailytrades 10`
`/utbreak sets`, `/utbreak why`, `/utbreak status`, `/utbreak forceentry`, `/utbreak analyze [EWY]`, `/utbreak research`, `/utbreak config`, `/utbreak configdiff`, `/utbreak log`
`/utbreak trace [SYMBOL]` - 최근 진입 경로 요약 진단 보고서
`/utbreak tracefull [SYMBOL]` - 최근 진입 경로 전체 진단 보고서
`/utbreak watchdog on|off` - 진입 가능 후 주문 미시도 진단 기록 ON/OFF (텔레그램 자동 발송 없음)
`/utbreak bridge on|off` - ready plan을 live scanner의 entry()로 연결
`SELECT 전략` - 5개 전략을 복수 선택하고 APPLY (신규 진입에만 적용)
`/utbreak quad on|off|status` - 5전략 전체 ON/OFF/상태
""".strip()

        def _format_utbreakout_config_text(diff=False):
            sig_cfg = self.cfg.get('signal_engine', {}) or {}
            strategy_params = sig_cfg.get('strategy_params', {}) if isinstance(sig_cfg.get('strategy_params', {}), dict) else {}
            raw_cfg = strategy_params.get('UTBotFilteredBreakoutV1', {})
            raw_cfg = dict(raw_cfg or {})

            engine = self.engines.get('signal')
            if engine and hasattr(engine, '_get_utbot_filtered_breakout_config'):
                effective_cfg = engine._get_utbot_filtered_breakout_config(strategy_params)
            else:
                effective_cfg = build_default_utbot_filtered_breakout_config()
                effective_cfg.update(raw_cfg)
                effective_cfg = apply_stable_utbreak_final_overrides(effective_cfg)

            if diff:
                return build_utbreakout_effective_config_diff_text(raw_cfg, effective_cfg)

            keys = [
                'effective_profile_version',
                'runtime_profile',
                'ev_adaptive_enabled',
                'selection_mode',
                'auto_select_enabled',
                'active_set_id',
                'safe_live_default_set_id',
                'live_safety_guard_enabled',
                'live_auto_set_whitelist_enabled',
                'live_auto_set_whitelist',
                'auto_min_score_margin_live',
                'auto_min_adjusted_score_live',
                'auto_block_on_weak_margin_live',
                'auto_multiple_testing_penalty_enabled',
                'selected_set_core_filter_hard_block_enabled',
                'set32_min_relative_volume',
                'set32_require_direction_candle',
                'set32_require_ema50_side',
                'set32_require_orderflow_confirmation',
                'set32_orderflow_min_samples',
                'market_quality_long_hard_block_on_multi_adverse_enabled',
                'market_quality_long_multi_adverse_min_reasons',
                'market_quality_long_multi_adverse_max_multiplier',
                'ev_min_entry_score',
                'ev_min_net_expectancy_r',
                'ev_no_edge_relief_enabled',
                'ev_no_edge_relief_min_score',
                'ev_no_edge_relief_min_adx',
                'ev_no_edge_relief_min_volume_ratio',
                'ev_no_edge_relief_min_efficiency',
                'ev_no_edge_relief_min_range_expansion',
                'ev_short_min_entry_score',
                'ev_short_trend_min_adx',
                'ev_short_trend_min_volume_ratio',
                'ev_short_no_edge_relief_min_score',
                'ev_short_no_edge_relief_min_adx',
                'ev_short_no_edge_relief_min_volume_ratio',
                'ev_short_no_edge_relief_min_efficiency',
                'ev_short_no_edge_relief_min_range_expansion',
                'ev_short_conditional_relief_risk_cap',
                'ev_short_relaxed_signal_risk_cap',
                'entry_quality_gate_enabled',
                'entry_quality_gate_min_final_risk_multiplier',
                'entry_quality_gate_long_min_final_risk_multiplier',
                'entry_quality_gate_short_min_final_risk_multiplier',
                'entry_quality_gate_min_ev_score',
                'entry_quality_gate_min_ev_probability',
                'entry_quality_gate_min_ev_net_expectancy_r',
                'entry_quality_gate_min_ev_mtf_votes',
                'entry_edge_enabled',
                'utbreak_entry_relaxation_mode',
                'entry_edge_min_score',
                'entry_edge_long_min_score',
                'entry_edge_short_min_score',
                'entry_edge_min_probability',
                'entry_edge_long_min_probability',
                'entry_edge_short_min_probability',
                'entry_edge_min_net_expectancy_r',
                'trend_continuation_entry_enabled',
                'trend_continuation_base_risk_multiplier',
                'trend_continuation_min_risk_multiplier',
                'entry_timeframe',
                'exit_timeframe',
                'htf_timeframe',
                'partial_take_profit_r_multiple',
                'partial_take_profit_ratio',
                'second_take_profit_r_multiple',
                'second_take_profit_ratio',
                'utbreakout_auto_entry_bridge_cooldown_sec',
                'utbreakout_auto_entry_bridge_max_ready_age_sec',
                'utbreakout_require_scanner_candidate_for_auto_entry',
            ]
            lines = [
                '🧾 UT Breakout Effective Config',
                '',
            ]
            for key in keys:
                lines.append(f"- {key}: {effective_cfg.get(key)!r}")
            lines.append("- restricted_symbols: ['SKHYNIX', 'HYUNDAI', 'SAMSUNG']")
            return '\n'.join(lines)

        def _build_utbreakout_keyboard():
            rows = [
                [
                    InlineKeyboardButton("5-ALL ON", callback_data="utb:quad:on"),
                    InlineKeyboardButton("5-ALL OFF", callback_data="utb:quad:off"),
                    InlineKeyboardButton("5-ALL STATUS", callback_data="utb:quad_status")
                ],
                [
                    InlineKeyboardButton("SELECT 전략", callback_data="utb:qselect")
                ],
                [
                    InlineKeyboardButton("코인 감시 목록", callback_data="utb:watchlist")
                ],
                [
                    InlineKeyboardButton("⚙️ 자동매매 설정", callback_data="atc:status")
                ]
            ]
            return InlineKeyboardMarkup(append_live_real_risk_buttons_to_utbreakout_rows(rows))

        async def _edit_utbreakout_menu(query, notice=None):
            text = _format_utbreakout_menu_text()
            if notice:
                text = f"{notice}\n\n{text}"
            await self._edit_markdown_safe(
                query,
                text,
                reply_markup=_build_utbreakout_keyboard(),
                filename='utbreakout_menu.txt',
                caption='UT Breakout 메뉴 전체 내용입니다.',
                preview_suffix='상세 UTBreak 메뉴는 파일로 보냈습니다.',
            )

        async def risk_callback(u: Update, c: ContextTypes.DEFAULT_TYPE):
            query = getattr(u, 'callback_query', None)
            if query is None:
                return
            try:
                await query.answer()
            except Exception:
                logger.debug("risk callback answer skipped", exc_info=True)

            data = str(getattr(query, 'data', '') or '')
            parts = data.split(':')
            try:
                if data == 'risk:menu':
                    await self._edit_markdown_safe(
                        query,
                        await _live_real_risk_status_text(menu=True),
                        reply_markup=build_live_real_risk_menu_keyboard(),
                        filename='risk_menu.txt',
                        caption='LIVE_REAL_SMALL_CAP 리스크 설정 메뉴',
                    )
                    return
                if data == 'risk:status':
                    await self._edit_markdown_safe(
                        query,
                        await _live_real_risk_status_text(menu=False),
                        reply_markup=build_live_real_risk_status_keyboard(),
                        filename='risk_status.txt',
                        caption='LIVE_REAL_SMALL_CAP 리스크 상태',
                    )
                    return
                if data == 'risk:manual':
                    await self._edit_markdown_safe(
                        query,
                        render_live_real_risk_manual_text(),
                        reply_markup=build_live_real_risk_status_keyboard(),
                        filename='risk_manual.txt',
                        caption='LIVE_REAL_SMALL_CAP 리스크 직접 입력 안내',
                    )
                    return
                if data == 'risk:safe':
                    cfg_for_risk = _live_real_risk_cfg()
                    requested_fraction = _live_real_min_max_risk_fraction(cfg_for_risk)[0]
                    _, text = await _set_live_real_risk_pct_from_user(
                        requested_fraction,
                        source="button",
                    )
                    await self._edit_markdown_safe(
                        query,
                        text,
                        reply_markup=build_live_real_risk_status_keyboard(),
                        filename='risk_status.txt',
                        caption='LIVE_REAL_SMALL_CAP 리스크 상태',
                    )
                    return
                if data == 'risk:back:utbreak':
                    await _edit_utbreakout_menu(query)
                    return
                if data == 'risk:cancel':
                    await self._edit_markdown_safe(
                        query,
                        await _live_real_risk_status_text(menu=True, notice="변경을 취소했습니다."),
                        reply_markup=build_live_real_risk_menu_keyboard(),
                        filename='risk_menu.txt',
                        caption='LIVE_REAL_SMALL_CAP 리스크 설정 메뉴',
                    )
                    return
                if len(parts) >= 3 and parts[0] == 'risk' and parts[1] == 'set':
                    cfg_for_risk = _live_real_risk_cfg()
                    requested_fraction = parse_live_real_risk_pct_input(str(float(parts[2]) * 100.0), cfg_for_risk)
                    if requested_fraction >= 0.05 - 1e-12:
                        await self._edit_markdown_safe(
                            query,
                            "\n".join([
                                "⚠️ 5% 리스크는 매우 공격적인 설정입니다.",
                                "1회 거래에서 계좌의 최대 5% 손실을 허용합니다.",
                                "그래도 변경하시겠습니까?",
                            ]),
                            reply_markup=build_live_real_risk_confirm_keyboard(requested_fraction),
                            filename='risk_confirm.txt',
                            caption='LIVE_REAL_SMALL_CAP 리스크 확인',
                        )
                        return
                    _, text = await _set_live_real_risk_pct_from_user(
                        requested_fraction,
                        source="button",
                    )
                    await self._edit_markdown_safe(
                        query,
                        text,
                        reply_markup=build_live_real_risk_status_keyboard(),
                        filename='risk_status.txt',
                        caption='LIVE_REAL_SMALL_CAP 리스크 상태',
                    )
                    return
                if len(parts) >= 4 and parts[0] == 'risk' and parts[1] == 'confirm' and parts[2] == 'set':
                    cfg_for_risk = _live_real_risk_cfg()
                    requested_fraction = parse_live_real_risk_pct_input(str(float(parts[3]) * 100.0), cfg_for_risk)
                    _, text = await _set_live_real_risk_pct_from_user(
                        requested_fraction,
                        source="button_confirm",
                    )
                    await self._edit_markdown_safe(
                        query,
                        text,
                        reply_markup=build_live_real_risk_status_keyboard(),
                        filename='risk_status.txt',
                        caption='LIVE_REAL_SMALL_CAP 리스크 상태',
                    )
                    return
                await self._edit_markdown_safe(
                    query,
                    await _live_real_risk_status_text(menu=True, notice="처리할 수 없는 리스크 버튼입니다."),
                    reply_markup=build_live_real_risk_menu_keyboard(),
                )
            except ValueError as exc:
                await self._edit_markdown_safe(
                    query,
                    await _live_real_risk_status_text(menu=True, notice=f"리스크 값 오류: {exc}"),
                    reply_markup=build_live_real_risk_menu_keyboard(),
                )
            except BadRequest as exc:
                if not self._is_telegram_message_not_modified_error(exc):
                    raise

        async def _reply_utbreakout_interaction(message_or_query, text, *, parse_mode=ParseMode.MARKDOWN):
            target = getattr(message_or_query, 'message', None)
            if target is not None and hasattr(target, 'reply_text'):
                await target.reply_text(
                    text,
                    parse_mode=parse_mode,
                    reply_markup=_build_utbreakout_keyboard()
                )
                return
            if hasattr(message_or_query, 'reply_text'):
                await message_or_query.reply_text(
                    text,
                    parse_mode=parse_mode,
                    reply_markup=_build_utbreakout_keyboard()
                )
                return
            if hasattr(message_or_query, 'edit_message_text'):
                try:
                    await message_or_query.edit_message_text(
                        text,
                        parse_mode=parse_mode,
                        reply_markup=_build_utbreakout_keyboard()
                    )
                except BadRequest as md_err:
                    if not self._is_telegram_message_not_modified_error(md_err):
                        raise

        async def _show_utbreakout_callback_progress(query, text="⏳ 처리 중입니다. 잠시만 기다려 주세요."):
            try:
                await query.edit_message_text(
                    text,
                    reply_markup=_build_utbreakout_keyboard()
                )
            except BadRequest as md_err:
                if self._is_telegram_message_not_modified_error(md_err):
                    return
                message = getattr(query, 'message', None)
                if message is not None:
                    try:
                        await message.reply_text(text, reply_markup=_build_utbreakout_keyboard())
                    except Exception as exc:
                        logger.debug(f"UTBreak callback progress fallback failed: {exc}")
            except Exception as exc:
                logger.debug(f"UTBreak callback progress message failed: {exc}")

        async def _edit_integrated_utbreak_notice(query, source_label):
            await _edit_utbreakout_menu(
                query,
                f"ℹ️ {source_label} 버튼은 이제 `/utbreak`의 UTBreak ON/OFF/조건 스테이터스로 통합되었습니다."
            )

        async def _send_utbreakout_log_document(message):
            if message is None:
                return
            path = UTBREAKOUT_DIAGNOSTIC_LOG_FILE
            try:
                if not os.path.exists(path) or os.path.getsize(path) <= 0:
                    await message.reply_text("다운로드할 UT Breakout 진단 로그가 아직 없습니다.")
                    return
                with open(path, 'rb') as fp:
                    await message.reply_document(
                        document=fp,
                        filename=os.path.basename(path),
                        caption="UT Breakout 진단 로그입니다. 후보/거절/승인/진입차단 이벤트만 기록합니다."
                    )
            except Exception as e:
                logger.error(f"UT breakout diagnostic log download failed: {e}")
                await message.reply_text(f"진단 로그 다운로드 실패: {e}")

        def _format_utbreakout_research_text():
            engine = self.engines.get('signal')
            protection_status = engine.last_protection_order_status if engine else {}
            text = format_utbreakout_research_summary(
                protection_status=protection_status,
                days=7
            )
            return "\n".join([
                "📈 UT Breakout 리서치 요약",
                "실거래 조건을 바꾸는 화면이 아니라, 최근 판단 로그로 쏠림/거절/리스크를 보는 화면입니다.",
                "",
                text,
                "",
                "다운로드: /utbreak research download",
            ])

        async def _send_utbreakout_research_document(message):
            if message is None:
                return
            try:
                report = _format_utbreakout_research_text()
                bio = io.BytesIO(report.encode('utf-8'))
                bio.name = 'utbreakout_research_report.txt'
                await message.reply_document(
                    document=bio,
                    filename='utbreakout_research_report.txt',
                    caption='UT Breakout 최근 7일 리서치 요약입니다.'
                )
            except Exception as e:
                logger.error(f"UT breakout research report download failed: {e}")
                await message.reply_text(f"리서치 리포트 다운로드 실패: {e}")

        def _get_utbreakout_status_symbol():
            watchlist = self.get_active_watchlist()
            return watchlist[0] if watchlist else 'BTC/USDT'

        async def _get_utbreakout_status_symbol_async():
            return await self._resolve_utbreakout_status_symbol()

        def _get_utbreakout_analysis_symbol(raw_symbol=None):
            raw = str(raw_symbol or '').strip()
            if raw:
                try:
                    return self.normalize_symbol_for_exchange(raw)
                except Exception:
                    return raw.upper()

            sig_cfg = self.cfg.get('signal_engine', {}) or {}
            coin_cfg = sig_cfg.get('coin_selector', {}) or {}
            custom_symbols = normalize_coin_selector_custom_symbols(coin_cfg.get('custom_symbols'))
            if len(custom_symbols) == 1:
                return custom_symbols[0]

            engine = self.engines.get('signal')
            if engine and engine.scanner_active_symbol:
                return engine.scanner_active_symbol
            if engine and len(engine.active_symbols or set()) == 1:
                return next(iter(engine.active_symbols))

            watchlist = self.get_active_watchlist()
            return watchlist[0] if watchlist else 'BTC/USDT'

        async def _get_utbreakout_condition_status_text():
            engine = self.engines.get('signal')
            if not engine:
                return "🚦 UT Breakout 조건 스테이터스\n\nSignal 엔진을 찾을 수 없습니다."
            symbol = await _get_utbreakout_status_symbol_async()
            if not symbol:
                text = await engine.build_utbreakout_no_live_candidate_status_text()
            else:
                text = await engine.build_utbreakout_condition_status_text(symbol)
            cfg = engine._get_utbot_filtered_breakout_config(engine.get_runtime_strategy_params())
            daily_entries = engine.db.get_daily_entry_count()
            return enforce_utbreakout_effective_status_contract(
                text,
                cfg,
                daily_entries,
            )

        async def _send_utbreakout_condition_status(message):
            if message is None:
                return False
            try:
                text = await _get_utbreakout_condition_status_text()
                await self._reply_long_text_with_document(
                    message,
                    text,
                    reply_markup=_build_utbreakout_keyboard(),
                    filename='utbreakout_condition_status.txt',
                    caption=f'UT Breakout 조건 스테이터스 · {UTBREAKOUT_EFFECTIVE_PROFILE_VERSION}',
                    preview_suffix='상세 조건 스테이터스는 파일로 보냈습니다.',
                )
                return True
            except Exception as exc:
                logger.exception("UTBreakout condition status send failed")
                try:
                    await message.reply_text(
                        f"⚠️ UTBreak 조건 스테이터스 전송 실패: {exc}",
                        reply_markup=_build_utbreakout_keyboard(),
                    )
                except Exception:
                    logger.exception("UTBreakout condition status failure reply failed")
                return False

        async def _send_utbreakout_condition_status_from_callback(query):
            await _show_utbreakout_callback_progress(
                query,
                "⏳ UTBreak 조건 스테이터스 조회 중입니다. 완료되면 새 메시지/파일로 보냅니다."
            )
            ok = await _send_utbreakout_condition_status(getattr(query, 'message', None))
            if not ok:
                try:
                    await query.answer("UTBreak 조건 스테이터스 전송 실패", show_alert=True)
                except Exception:
                    logger.debug("UTBreak callback failure alert failed", exc_info=True)

        async def _get_relative_strength_pullback_status_text():
            engine = self.engines.get('signal')
            if not engine:
                return "RelativeStrengthPullbackTrend status\n\nSignal engine not found."
            symbol = await _get_utbreakout_status_symbol_async()
            return await engine.build_relative_strength_pullback_status_text(symbol)

        async def _send_relative_strength_pullback_status(message):
            if message is None:
                return False
            try:
                text = await _get_relative_strength_pullback_status_text()
                await self._reply_long_text_with_document(
                    message,
                    text,
                    reply_markup=_build_utbreakout_keyboard(),
                    filename='relative_strength_pullback_status.txt',
                    caption='RelativeStrengthPullbackTrend status',
                    preview_suffix='RSPT detailed status was sent as a file.',
                )
                return True
            except Exception as exc:
                logger.exception("RSPT status send failed")
                try:
                    await message.reply_text(
                        f"RSPT status failed: {type(exc).__name__}: {exc}",
                        reply_markup=_build_utbreakout_keyboard(),
                    )
                except Exception:
                    logger.exception("RSPT status failure reply failed")
                return False

        async def _send_relative_strength_pullback_status_from_callback(query):
            await _show_utbreakout_callback_progress(
                query,
                "RSPT status 조회 중입니다. 완료되면 새 메시지/파일로 보냅니다.",
            )
            ok = await _send_relative_strength_pullback_status(getattr(query, 'message', None))
            if not ok:
                try:
                    await query.answer("RSPT status 전송 실패", show_alert=True)
                except Exception:
                    logger.debug("RSPT callback failure alert failed", exc_info=True)

        async def _get_dual_alpha_status_text():
            engine = self.engines.get('signal')
            if not engine:
                return "DUAL Alpha status\n\nSignal engine not found."
            symbol = await _get_utbreakout_status_symbol_async()
            return await engine.build_dual_alpha_status_text(symbol)

        async def _send_dual_alpha_status(message):
            if message is None:
                return False
            try:
                text = await _get_dual_alpha_status_text()
                await self._reply_long_text_with_document(
                    message,
                    text,
                    reply_markup=_build_utbreakout_keyboard(),
                    filename='dual_alpha_status.txt',
                    caption='DUAL Alpha status',
                    preview_suffix='DUAL Alpha status was sent as a file.',
                )
                return True
            except Exception as exc:
                logger.exception("DUAL Alpha status send failed")
                try:
                    await message.reply_text(
                        f"DUAL Alpha status failed: {type(exc).__name__}: {exc}",
                        reply_markup=_build_utbreakout_keyboard(),
                    )
                except Exception:
                    logger.exception("DUAL Alpha status failure reply failed")
                return False

        async def _send_dual_alpha_status_from_callback(query):
            await _show_utbreakout_callback_progress(
                query,
                "DUAL Alpha status 조회 중입니다. 완료되면 새 메시지/파일로 보냅니다.",
            )
            ok = await _send_dual_alpha_status(getattr(query, 'message', None))
            if not ok:
                try:
                    await query.answer("DUAL Alpha status 전송 실패", show_alert=True)
                except Exception:
                    logger.debug("DUAL Alpha callback failure alert failed", exc_info=True)

        async def _get_qh_flow_status_text():
            engine = self.engines.get('signal')
            if not engine:
                return "QH-Flow status\n\nSignal engine not found."
            symbol = await _get_utbreakout_status_symbol_async()
            return await engine.build_qh_flow_status_text(symbol)

        async def _send_qh_flow_status(message):
            if message is None:
                return False
            try:
                text = await _get_qh_flow_status_text()
                await self._reply_long_text_with_document(
                    message,
                    text,
                    reply_markup=_build_utbreakout_keyboard(),
                    filename='qh_flow_status.txt',
                    caption='QH-Flow status',
                    preview_suffix='QH-Flow status was sent as a file.',
                )
                return True
            except Exception as exc:
                logger.exception('QH-Flow status send failed')
                await message.reply_text(
                    f"QH-Flow status failed: {type(exc).__name__}: {exc}",
                    reply_markup=_build_utbreakout_keyboard(),
                )
                return False

        async def _send_qh_flow_status_from_callback(query):
            await _show_utbreakout_callback_progress(query, 'QH-Flow status 조회 중입니다.')
            await _send_qh_flow_status(getattr(query, 'message', None))

        async def _get_triple_alpha_status_text():
            engine = self.engines.get('signal')
            if not engine:
                return "TRIPLE Alpha status\n\nSignal engine not found."
            symbol = await _get_utbreakout_status_symbol_async()
            return await engine.build_triple_alpha_status_text(symbol)

        async def _send_triple_alpha_status(message):
            if message is None:
                return False
            try:
                text = await _get_triple_alpha_status_text()
                await self._reply_long_text_with_document(
                    message,
                    text,
                    reply_markup=_build_utbreakout_keyboard(),
                    filename='triple_alpha_status.txt',
                    caption='TRIPLE Alpha status',
                    preview_suffix='TRIPLE Alpha status was sent as a file.',
                )
                return True
            except Exception as exc:
                logger.exception('TRIPLE Alpha status send failed')
                await message.reply_text(
                    f"TRIPLE Alpha status failed: {type(exc).__name__}: {exc}",
                    reply_markup=_build_utbreakout_keyboard(),
                )
                return False

        async def _send_triple_alpha_status_from_callback(query):
            await _show_utbreakout_callback_progress(query, 'TRIPLE Alpha status 조회 중입니다.')
            await _send_triple_alpha_status(getattr(query, 'message', None))



        async def _get_quad_alpha_status_text():
            engine = self.engines.get('signal')
            if not engine:
                return '5-Strategy Alpha status\n\nSignal engine not found.'
            symbol = await _get_utbreakout_status_symbol_async()
            return await engine.build_quad_alpha_status_text(symbol)

        async def _send_quad_alpha_status(message):
            if message is None:
                return False
            try:
                text = await _get_quad_alpha_status_text()
                await self._reply_long_text_with_document(
                    message,
                    text,
                    reply_markup=_build_utbreakout_keyboard(),
                    filename='quad_alpha_status.txt',
                    caption='5-Strategy Alpha status',
                    preview_suffix='5-Strategy Alpha status was sent as a file.',
                )
                return True
            except Exception as exc:
                logger.exception('5-Strategy Alpha status send failed')
                await message.reply_text(
                    f'5-Strategy Alpha status failed: {type(exc).__name__}: {exc}',
                    reply_markup=_build_utbreakout_keyboard(),
                )
                return False

        async def _send_quad_alpha_status_from_callback(query):
            await _show_utbreakout_callback_progress(query, '5-Strategy Alpha status 조회 중입니다.')
            await _send_quad_alpha_status(getattr(query, 'message', None))

        async def _get_crowding_unwind_status_text():
            engine = self.engines.get('signal')
            if not engine:
                return 'Crowding Unwind status\n\nSignal engine not found.'
            symbol = await _get_utbreakout_status_symbol_async()
            return await engine.build_crowding_unwind_status_text(symbol)

        async def _send_crowding_unwind_status(message):
            if message is None:
                return False
            try:
                text = await _get_crowding_unwind_status_text()
                await self._reply_long_text_with_document(
                    message,
                    text,
                    reply_markup=_build_utbreakout_keyboard(),
                    filename='crowding_unwind_status.txt',
                    caption='Funding-OI Crowding Unwind status',
                    preview_suffix='Crowding Unwind status was sent as a file.',
                )
                return True
            except Exception as exc:
                logger.exception('Crowding Unwind status send failed')
                await message.reply_text(
                    f'Crowding Unwind status failed: {type(exc).__name__}: {exc}',
                    reply_markup=_build_utbreakout_keyboard(),
                )
                return False

        async def _send_crowding_unwind_status_from_callback(query):
            await _show_utbreakout_callback_progress(query, 'Crowding Unwind status 조회 중입니다.')
            await _send_crowding_unwind_status(getattr(query, 'message', None))

        async def _get_liquidation_exhaustion_reversal_status_text():
            engine = self.engines.get('signal')
            if not engine:
                return 'LXR status\n\nSignal engine not found.'
            symbol = await _get_utbreakout_status_symbol_async()
            return await engine.build_liquidation_exhaustion_reversal_status_text(symbol)

        async def _send_liquidation_exhaustion_reversal_status(message):
            if message is None:
                return False
            try:
                text = await _get_liquidation_exhaustion_reversal_status_text()
                await self._reply_long_text_with_document(
                    message,
                    text,
                    reply_markup=_build_utbreakout_keyboard(),
                    filename='lxr_status.txt',
                    caption='LXR strategy status',
                    preview_suffix='LXR status was sent as a file.',
                )
                return True
            except Exception as exc:
                logger.exception('LXR status send failed')
                await message.reply_text(
                    f'LXR status failed: {type(exc).__name__}: {exc}',
                    reply_markup=_build_utbreakout_keyboard(),
                )
                return False

        async def _send_liquidation_exhaustion_reversal_status_from_callback(query):
            await _show_utbreakout_callback_progress(query, 'LXR status 조회 중입니다.')
            await _send_liquidation_exhaustion_reversal_status(getattr(query, 'message', None))

        async def _edit_utbreakout_condition_status(query):
            text = await _get_utbreakout_condition_status_text()
            await self._edit_long_text_with_document(
                query,
                text,
                reply_markup=_build_utbreakout_keyboard(),
                filename='utbreakout_condition_status.txt',
                caption=f'UT Breakout 조건 스테이터스 · {UTBREAKOUT_EFFECTIVE_PROFILE_VERSION}',
                preview_suffix='상세 조건 스테이터스는 파일로 보냈습니다.',
            )

        async def _get_utbreakout_entry_analysis_text(raw_symbol=None):
            engine = self.engines.get('signal')
            if not engine:
                return "UT Breakout 진입 분석\n\nSignal 엔진을 찾을 수 없습니다."
            symbol = _get_utbreakout_analysis_symbol(raw_symbol)
            return await engine.build_utbreakout_entry_analysis_text(symbol)

        async def _send_utbreakout_entry_analysis(message, raw_symbol=None):
            if message is None:
                return
            text = await _get_utbreakout_entry_analysis_text(raw_symbol)
            if len(text) <= 3900:
                await message.reply_text(text, reply_markup=_build_utbreakout_keyboard())
                return
            preview = "\n".join(text.splitlines()[:45])
            await message.reply_text(
                f"{preview}\n\n상세 분석은 파일로 보냈습니다.",
                reply_markup=_build_utbreakout_keyboard()
            )
            bio = io.BytesIO(text.encode('utf-8'))
            bio.name = 'utbreakout_entry_analysis.txt'
            await message.reply_document(
                document=bio,
                filename='utbreakout_entry_analysis.txt',
                caption='UT Breakout 진입 분석 전체 로그입니다. 그대로 공유하면 원인 추적에 사용할 수 있습니다.'
            )

        async def _edit_utbreakout_entry_analysis(query, raw_symbol=None):
            text = await _get_utbreakout_entry_analysis_text(raw_symbol)
            if len(text) <= 3900:
                try:
                    await query.edit_message_text(text, reply_markup=_build_utbreakout_keyboard())
                except BadRequest as md_err:
                    if "message is not modified" in str(md_err).lower():
                        return
                    raise
                return
            preview = "\n".join(text.splitlines()[:45])
            try:
                await query.edit_message_text(
                    f"{preview}\n\n상세 분석은 파일로 보냈습니다.",
                    reply_markup=_build_utbreakout_keyboard()
                )
            except BadRequest as md_err:
                if "message is not modified" not in str(md_err).lower():
                    raise
            bio = io.BytesIO(text.encode('utf-8'))
            bio.name = 'utbreakout_entry_analysis.txt'
            await query.message.reply_document(
                document=bio,
                filename='utbreakout_entry_analysis.txt',
                caption='UT Breakout 진입 분석 전체 로그입니다. 그대로 공유하면 원인 추적에 사용할 수 있습니다.'
            )

        async def _send_utbreakout_entry_analysis_from_callback(query):
            await _show_utbreakout_callback_progress(
                query,
                "⏳ UTBreak 진입 분석 조회 중입니다. 완료되면 새 메시지/파일로 보냅니다."
            )
            await _send_utbreakout_entry_analysis(getattr(query, 'message', None))

        async def _send_utbreakout_trace(message, raw_symbol=None, *, full=False):
            engine = self.engines.get('signal')
            if not engine:
                await message.reply_text("⚠️ trace 실패: Signal 엔진을 찾을 수 없습니다.")
                return
            raw = str(raw_symbol or '').strip()
            show_all = raw.lower() in {'all', '*'}
            explicit_symbol = (
                _get_utbreakout_analysis_symbol(raw)
                if raw and not show_all else raw
            )
            symbol = engine._resolve_utbreakout_trace_symbol(explicit_symbol)
            if not show_all and not symbol:
                symbol = await _get_utbreakout_status_symbol_async()

            invalid_market = False
            if not show_all and symbol:
                ok_market, symbol, _ = (
                    engine._ensure_valid_utbreakout_market_symbol(
                        symbol,
                        source='trace_command',
                    )
                )
                invalid_market = not ok_market

            if (
                not show_all
                and not invalid_market
                and not engine._utbreakout_recent_trace_events(symbol, limit=1)
            ):
                try:
                    await engine.build_utbreakout_condition_status_text(symbol)
                except Exception:
                    logger.debug(
                        "UTBreakout pre-trace status build skipped for %s",
                        symbol,
                        exc_info=True,
                    )
                symbol = engine._resolve_utbreakout_trace_symbol(
                    explicit_symbol
                ) or symbol

            report = engine._format_utbreakout_trace_report(symbol, full=full)
            if full:
                await self._send_utbreakout_trace_document(
                    message,
                    report,
                    full=True,
                    symbol=symbol if not show_all else "all",
                )
            else:
                await self._reply_markdown_safe(message, report)

        async def _force_utbreakout_entry(message):
            engine = self.engines.get('signal')
            if not engine:
                await message.reply_text("⚠️ forceentry 실패: Signal 엔진을 찾을 수 없습니다.")
                return
            symbol = await _get_utbreakout_status_symbol_async()
            await message.reply_text(
                f"🟡 UTBreakout forceentry 재검증 시작: {symbol}\n"
                "현재 조건을 다시 계산한 뒤 기존 entry() 안전검사를 그대로 통과시킵니다."
            )
            try:
                result = await engine.force_utbreakout_entry_from_status(symbol)
            except Exception as exc:
                logger.exception("UTBreakout forceentry failed for %s: %s", symbol, exc)
                await message.reply_text(
                    f"❌ UTBreakout forceentry 오류: {symbol} / "
                    f"{type(exc).__name__}: {exc}"
                )
                return
            if result.get('status') == 'POSITION_OPENED':
                await message.reply_text(
                    f"✅ UTBreakout forceentry 포지션 확인: "
                    f"{symbol} {str(result.get('side') or '').upper()}"
                )

        def _format_utbreakout_sets_text(page=1):
            try:
                page = int(page or 1)
            except (TypeError, ValueError):
                page = 1
            page = min(6, max(1, page))
            start_id = ((page - 1) * 10) + 1
            end_id = min(60, start_id + 9)
            lines = [
                f"📚 UT Breakout 60-Set 카탈로그 ({page}/6)",
                "Set 1~60은 AUTO 후보/수동 선택/실거래 판단에 연결되어 있습니다.",
                "Set 51~60은 Futures public data + Prediction scan 데이터가 없으면 진입을 차단합니다.",
                "",
            ]
            for set_id in range(start_id, end_id + 1):
                info = get_utbreakout_set_definition(set_id)
                status = "실거래" if info.get('status') == 'active' else "연구전용"
                filters = ", ".join(info.get('entry_filters') or ['UT only'])
                lines.extend([
                    f"Set{set_id} {info.get('name')} [{status}]",
                    f"장세: {info.get('regime')}",
                    f"조건: {filters} | 빈도: {info.get('frequency_impact')}",
                    f"설명: {info.get('description')}",
                    "",
                ])
            lines.append("다른 페이지: /utbreak sets 1~6")
            return "\n".join(lines).strip()

        def _format_utbreakout_why_text():
            symbol = _get_utbreakout_status_symbol()
            engine = self.engines.get('signal')
            diag = {}
            if engine:
                diag = engine.last_utbot_filtered_breakout_status.get(symbol, {}) or {}
            cfg = _current_utbreakout_cfg()
            set_id = diag.get('auto_selected_set_id') or cfg.get('active_set_id') or cfg.get('profile') or UTBREAKOUT_DEFAULT_SET_ID
            info = get_utbreakout_set_definition(set_id)
            scores = diag.get('auto_scores') or {}
            if scores:
                score_lines = [
                    f"trend: {scores.get('trend_score', 0)}",
                    f"chop: {scores.get('chop_score', 0)}",
                    f"volatility: {scores.get('volatility_score', 0)}",
                    f"breakout: {scores.get('breakout_score', 0)}",
                    f"momentum: {scores.get('momentum_score', 0)}",
                    f"flow: {scores.get('flow_score', 0)}",
                    f"alignment: {scores.get('alignment_score', 0)}",
                    f"dominant_side: {scores.get('dominant_side', 'n/a')}",
                ]
                adjusted_scores = scores.get('auto_candidate_scores_adjusted')
                if isinstance(adjusted_scores, dict) and adjusted_scores:
                    adjusted_preview = ", ".join(
                        f"{key}:{value}"
                        for key, value in list(adjusted_scores.items())[:8]
                    )
                else:
                    adjusted_preview = 'n/a'
                safety_lines = [
                    "AUTO 안전장치:",
                    f"- confidence: {scores.get('auto_confidence', 'n/a')}",
                    f"- margin: {scores.get('auto_score_margin', 'n/a')}",
                    f"- live whitelist: {scores.get('auto_live_whitelist', 'n/a')}",
                    f"- multiple-testing penalty: {scores.get('auto_multiple_testing_penalty_points', 0)}점",
                    f"- blocked: {bool(scores.get('auto_blocked_by_margin'))}",
                    f"- block reason: {scores.get('auto_block_reason') or 'n/a'}",
                    f"- adjusted scores: {adjusted_preview}",
                ]
            else:
                score_lines = ["AUTO 점수 기록 없음. /utbreak status 또는 다음 판단봉 이후 확인하세요."]
                safety_lines = []
            return "\n".join([
                "🧠 UT Breakout AUTO 선택 이유",
                f"심볼: {symbol}",
                f"선택: Set{info.get('id')} {info.get('name')} ({'실거래' if info.get('status') == 'active' else 'planned'})",
                f"이유: {diag.get('auto_selection_reason') or '수동 선택 또는 아직 분석 기록 없음'}",
                f"실제 진입 조건: {', '.join(info.get('entry_filters') or ['UT only'])}",
                "",
                "점수:",
                *score_lines,
                "",
                *safety_lines,
            ])

        def _current_utbreakout_cfg():
            raw = self.cfg.get('signal_engine', {}).get('strategy_params', {}).get('UTBotFilteredBreakoutV1', {})
            return dict(raw) if isinstance(raw, dict) else {}

        def _merge_utbreakout_profile_with_preserved_settings(profile):
            profile_cfg = build_utbot_filtered_breakout_profile(profile)
            current_cfg = _current_utbreakout_cfg()
            preserve_keys = {
                'entry_timeframe',
                'exit_timeframe',
                'htf_timeframe',
                'auto_timeframes',
                'adaptive_timeframe_enabled',
                'adaptive_timeframes',
                'adaptive_timeframe_min_score',
                'adaptive_timeframe_switch_margin',
                'adaptive_timeframe_min_hold_candles',
                'quad_alpha_enabled_strategies',
                'risk_per_trade_percent',
                'max_risk_per_trade_usdt',
                'daily_max_loss_usdt',
                'max_daily_trades',
                'daily_profit_target_enabled',
                'daily_profit_target_usdt',
                'opposite_signal_exit_enabled',
                'opposite_set_exit_enabled',
                'opposite_set_exit_min_hold_candles',
                'opposite_set_exit_min_pnl_enabled',
                'opposite_set_exit_min_pnl_usdt',
                'ema_rsi_exit_enabled',
                'adx_donchian_exit_enabled',
                'exclude_rsi_extreme'
            }
            for key in preserve_keys:
                if key in current_cfg:
                    profile_cfg[key] = current_cfg[key]
            return profile_cfg

        async def _resolve_utbreak_direct_watchlist(symbols, *, max_symbols=5):
            mode = self.get_exchange_mode()
            if mode == UPBIT_MODE:
                raise ValueError("Upbit 모드에서는 UTBreakout Futures watchlist를 사용할 수 없습니다. 업비트 코인 변경 메뉴를 사용하세요.")
            normalized = normalize_coin_selector_custom_symbols(symbols)
            if not normalized:
                raise ValueError("감시할 코인을 1개 이상 입력하세요. 예: `/utbreak watch BTC ETH AAPL`")
            if len(normalized) > max_symbols:
                raise ValueError(f"감시 목록은 최대 {max_symbols}개까지만 가능합니다.")
            markets = await asyncio.to_thread(self.exchange.load_markets)
            resolved = []
            seen = set()
            for raw_symbol in normalized:
                symbol = self._resolve_futures_watch_symbol_from_markets(
                    raw_symbol,
                    markets,
                    exchange_mode=mode,
                )
                key = self._futures_symbol_key(symbol)
                if key and key not in seen:
                    resolved.append(symbol)
                    seen.add(key)
            if not resolved:
                raise ValueError("감시할 코인을 인식하지 못했습니다.")
            return resolved

        async def _enable_utbreak_direct_watchlist(symbols):
            watch_symbols = await _resolve_utbreak_direct_watchlist(symbols)
            await _ensure_signal_engine_active()
            await self.cfg.update_value(['signal_engine', 'watchlist'], watch_symbols)
            await self._update_config_value(['exchange_watchlists', self.get_exchange_mode()], watch_symbols)
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'fixed_symbol_mode_enabled'], False)
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'fixed_symbol'], '')
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'custom_symbols'], watch_symbols)
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'custom_universe_enabled'], False)
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'enabled'], False)
            await self.cfg.update_value(['signal_engine', 'common_settings', 'scanner_enabled'], False)
            await self.cfg.update_value(['signal_engine', 'micro_auto', 'enabled'], False)

            strategy_params = self.cfg.get('signal_engine', {}).get('strategy_params', {}) or {}
            active_strategy = str(strategy_params.get('active_strategy', '') or '').lower()
            if active_strategy not in UTBREAKOUT_STRATEGIES:
                await self.cfg.update_value(['signal_engine', 'strategy_params', 'active_strategy'], UTBOT_FILTERED_BREAKOUT_STRATEGY)

            engine = self._reset_signal_engine_runtime_state(
                reset_entry_cache=True,
                reset_exit_cache=True,
                reset_stateful_strategy=True
            )
            if engine:
                engine.scanner_active_symbol = None
                engine.active_symbols.clear()
                for symbol in watch_symbols:
                    engine.active_symbols.add(symbol)
                    await engine.prime_symbol_to_next_closed_candle(symbol)
            return (
                f"✅ UTBreak 직접 감시 목록: `{', '.join(watch_symbols)}`\n"
                "scanner/CoinSelector를 끄고 지정한 1~5개 심볼만 감시합니다."
            )

        async def _prompt_utbreak_watchlist_input(message_or_query, context):
            if context and context.user_data is not None:
                context.user_data['utbreak_watchlist_waiting_for_symbols'] = True
            text = "UTBreak에서 직접 감시할 코인 1~5개를 입력하세요. 예: `BTC ETH AAPL EWY`"
            await _reply_utbreakout_interaction(message_or_query, text)

        async def _prompt_utbreak_single_coin_input(message_or_query, context):
            if context and context.user_data is not None:
                context.user_data['utbreak_coin_waiting_for_symbol'] = True
            await _reply_utbreakout_interaction(
                message_or_query,
                "UTBreak에서 직접 감시할 코인 1개를 입력하세요. 예: `EWY`"
            )

        async def _prompt_utbreak_autoscan_input(message_or_query, context):
            if context and context.user_data is not None:
                context.user_data['utbreak_autoscan_waiting_for_symbols'] = True
            await _reply_utbreakout_interaction(
                message_or_query,
                "AUTO 후보 스캔에 사용할 코인을 입력하세요. 예: `BTC ETH SOL`"
            )

        async def utbreakout_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
            args = list(getattr(c, 'args', []) or [])
            if not args and u and u.message and u.message.text:
                parts = u.message.text.strip().split()
                args = parts[1:]
            action = str(args[0]).strip().lower() if args else ''

            def _parse_positive_arg(label, minimum=0.0, maximum=None):
                if len(args) < 2:
                    raise ValueError(f"{label} 값을 입력하세요.")
                try:
                    value = float(str(args[1]).replace('$', '').replace(',', '').strip())
                except (TypeError, ValueError):
                    raise ValueError(f"{label} 값은 숫자로 입력하세요.")
                if value <= minimum:
                    raise ValueError(f"{label} 값은 {minimum}보다 커야 합니다.")
                if maximum is not None and value > maximum:
                    raise ValueError(f"{label} 값은 {maximum} 이하로 입력하세요.")
                return value

            def _parse_float_arg(label, minimum=None, maximum=None):
                if len(args) < 2:
                    raise ValueError(f"{label} 값을 입력하세요.")
                try:
                    value = float(str(args[1]).replace('$', '').replace(',', '').strip())
                except (TypeError, ValueError):
                    raise ValueError(f"{label} 값은 숫자로 입력하세요.")
                if minimum is not None and value < minimum:
                    raise ValueError(f"{label} 값은 {minimum} 이상이어야 합니다.")
                if maximum is not None and value > maximum:
                    raise ValueError(f"{label} 값은 {maximum} 이하로 입력하세요.")
                return value

            if action in {'on', 'enable', 'activate', 'start'}:
                await u.message.reply_text(await _activate_utbreak_strategy())
            elif action in {'off', 'disable', 'stop_trade', 'stoptrading'}:
                await u.message.reply_text(await _stop_utbreak_trading())
            elif action in {'pause', 'paused', 'stop_trade', 'stoptrading'}:
                await u.message.reply_text(await _set_strategy_pause(True))
            elif action in {'resume', 'run', 'restart'}:
                await u.message.reply_text(await _set_strategy_pause(False))
            elif action in {'coin', 'symbol', 'fixed', 'single', 'pin'}:
                if len(args) > 1 and str(args[1]).strip().lower() in {'off', 'disable', 'stop', '0'}:
                    await u.message.reply_text(await _disable_single_fixed_coin(), parse_mode=ParseMode.MARKDOWN)
                elif len(args) > 1:
                    ok, notice = await _enable_single_fixed_coin(args[1:])
                    await u.message.reply_text(notice, parse_mode=ParseMode.MARKDOWN)
                else:
                    await _prompt_utbreak_single_coin_input(u.message, c)
                    return
            elif action in {'watch', 'watchlist', 'coins'}:
                if len(args) <= 1:
                    await _prompt_utbreak_watchlist_input(u.message, c)
                    return
                try:
                    notice = await _enable_utbreak_direct_watchlist(args[1:])
                except ValueError as e:
                    await u.message.reply_text(f"❌ {e}", parse_mode=ParseMode.MARKDOWN)
                    return
                await u.message.reply_text(
                    f"{notice}\n\n{_format_utbreakout_menu_text()}",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=_build_utbreakout_keyboard()
                )
            elif action in {'autoscan', 'auto_scan', 'candidates', 'candidate', 'scanmode'}:
                mode = str(args[1]).strip().lower() if len(args) > 1 else ''
                if mode in {'off', 'disable', 'stop', '0'}:
                    await u.message.reply_text(await _disable_customcoins(), parse_mode=ParseMode.MARKDOWN)
                elif mode in {'on', 'enable', 'start', '1'}:
                    symbols = args[2:]
                    if not symbols:
                        await _prompt_utbreak_autoscan_input(u.message, c)
                        return
                    _, notice = await _enable_customcoins(symbols)
                    await u.message.reply_text(notice, parse_mode=ParseMode.MARKDOWN)
                else:
                    await u.message.reply_text("❌ 예: `/utbreak autoscan on BTC ETH SOL` 또는 `/utbreak autoscan off`", parse_mode=ParseMode.MARKDOWN)
                    return
            elif action in {'coinauto', 'coin_auto', 'coinselect', 'coin_select', 'autocoin', 'auto_coin'}:
                mode = str(args[1]).strip().lower() if len(args) > 1 else ''
                if mode in {'on', 'enable', 'start', '1'}:
                    await u.message.reply_text(await _enable_coin_auto_selection(), parse_mode=ParseMode.MARKDOWN)
                elif mode in {'off', 'disable', 'stop', '0'}:
                    await u.message.reply_text(await _disable_coin_auto_selection(), parse_mode=ParseMode.MARKDOWN)
                else:
                    await u.message.reply_text("❌ 예: `/utbreak coinauto on` 또는 `/utbreak coinauto off`", parse_mode=ParseMode.MARKDOWN)
                    return
            elif action in {'lev', 'leverage'}:
                try:
                    value = args[1]
                    await u.message.reply_text(f"✅ 레버리지: {await _set_common_leverage(value)}x")
                except (IndexError, ValueError) as e:
                    await u.message.reply_text(f"❌ {e}\n예: `/utbreak lev 10`", parse_mode=ParseMode.MARKDOWN)
                    return
            elif action in {'tf', 'entry_tf', 'entrytf'}:
                try:
                    value = args[1]
                    await u.message.reply_text(f"✅ 진입 TF: {await _set_common_entry_tf(value)}")
                except (IndexError, ValueError) as e:
                    await u.message.reply_text(f"❌ {e}\n예: `/utbreak tf 15m`", parse_mode=ParseMode.MARKDOWN)
                    return
            elif action in {'exit_tf', 'exittf', 'exit'}:
                try:
                    value = args[1]
                    await u.message.reply_text(f"✅ 청산 TF: {await _set_common_exit_tf(value)}")
                except (IndexError, ValueError) as e:
                    await u.message.reply_text(f"❌ {e}\n예: `/utbreak exit_tf 1h`", parse_mode=ParseMode.MARKDOWN)
                    return
            elif action in {'target', 'tp', 'roe'}:
                try:
                    value = args[1]
                    await u.message.reply_text(f"✅ {await _set_take_profit(value)}")
                except (IndexError, ValueError) as e:
                    await u.message.reply_text(f"❌ {e}\n예: `/utbreak target 20` 또는 `/utbreak target off`", parse_mode=ParseMode.MARKDOWN)
                    return
            elif action in {'stop', 'sl', 'stoploss', 'stop_loss'}:
                try:
                    value = args[1]
                    await u.message.reply_text(f"✅ {await _set_stop_loss(value)}")
                except (IndexError, ValueError) as e:
                    await u.message.reply_text(f"❌ {e}\n예: `/utbreak stop 10` 또는 `/utbreak stop off`", parse_mode=ParseMode.MARKDOWN)
                    return
            elif action in {'micro', 'microauto'}:
                mode = str(args[1]).strip().lower() if len(args) > 1 else ''
                if mode in {'on', 'dry', 'dryrun', 'dry-run'}:
                    await _enable_microauto(dry_run=True)
                    await u.message.reply_text("✅ Micro Auto: ON / DRY-RUN ON")
                elif mode in {'live', 'live_on'}:
                    await _enable_microauto(dry_run=False)
                    await u.message.reply_text("✅ Micro Auto: LIVE ON")
                elif mode in {'off', 'disable', 'stop'}:
                    await self.cfg.update_value(['signal_engine', 'micro_auto', 'enabled'], False)
                    self._reset_signal_engine_runtime_state(reset_stateful_strategy=True)
                    await u.message.reply_text("✅ Micro Auto: OFF")
                else:
                    await u.message.reply_text("❌ 예: `/utbreak micro on`, `/utbreak micro live`, `/utbreak micro off`", parse_mode=ParseMode.MARKDOWN)
                    return
            elif action in {'auto', 'autoset', 'auto_select', 'bundle', 'autobundle', 'auto_bundle'}:
                mode = str(args[1]).strip().lower() if len(args) > 1 else ''
                if mode not in {'on', 'off', 'enable', 'disable'}:
                    await u.message.reply_text("❌ 예: `/utbreak auto on` 또는 `/utbreak auto off`", parse_mode=ParseMode.MARKDOWN)
                    return
                notice = (
                    await _enable_utbreak_auto_bundle()
                    if mode in {'on', 'enable'} else
                    await _disable_utbreak_auto_bundle()
                )
                await u.message.reply_text(notice, parse_mode=ParseMode.MARKDOWN)
            elif action in {'adaptive', 'tfauto', 'timeframe', 'timeframe_auto'}:
                mode = str(args[1]).strip().lower() if len(args) > 1 else ''
                if mode not in {'on', 'off', 'enable', 'disable'}:
                    await u.message.reply_text("❌ 예: `/utbreak adaptive on` 또는 `/utbreak adaptive off`", parse_mode=ParseMode.MARKDOWN)
                    return
                enabled = mode in {'on', 'enable'}
                await self.cfg.update_value(
                    ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'adaptive_timeframe_enabled'],
                    enabled
                )
                await self.cfg.update_value(
                    ['signal_engine', 'strategy_params', 'active_strategy'],
                    UTBOT_ADAPTIVE_TIMEFRAME_STRATEGY if enabled else UTBOT_FILTERED_BREAKOUT_STRATEGY
                )
                self._reset_signal_engine_runtime_state(
                    reset_entry_cache=True,
                    reset_exit_cache=True,
                    reset_stateful_strategy=True
                )
                await u.message.reply_text(
                    f"✅ Adaptive 시간봉 전략: {'ON' if enabled else 'OFF'} "
                    f"({'UTBOT_ADAPTIVE_TIMEFRAME_V1' if enabled else 'UTBOT_FILTERED_BREAKOUT_V1'})"
                )
            elif action in {'rsp', 'rspt', 'relative', 'relative_strength', 'pullback'}:
                mode = str(args[1]).strip().lower() if len(args) > 1 else 'status'
                if mode in {'on', 'enable', 'start', 'live'}:
                    await u.message.reply_text(
                        await _activate_relative_strength_pullback_strategy(),
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=_build_utbreakout_keyboard(),
                    )
                elif mode in {'off', 'disable', 'stop'}:
                    await u.message.reply_text(
                        await _deactivate_relative_strength_pullback_strategy(),
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=_build_utbreakout_keyboard(),
                    )
                elif mode in {'status', 'stat', 'menu', ''}:
                    await _send_relative_strength_pullback_status(u.message)
                else:
                    await u.message.reply_text(
                        "Usage: `/utbreak rsp on`, `/utbreak rsp off`, `/utbreak rsp status`",
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=_build_utbreakout_keyboard(),
                    )
                    return
            elif action in {'crowding', 'crowd', 'fundingoi', 'funding_oi'}:
                mode = str(args[1]).strip().lower() if len(args) > 1 else 'status'
                if mode in {'on', 'enable', 'start', 'live'}:
                    await u.message.reply_text(
                        await _activate_crowding_unwind_strategy(),
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=_build_utbreakout_keyboard(),
                    )
                elif mode in {'off', 'disable', 'stop'}:
                    await u.message.reply_text(
                        await _deactivate_crowding_unwind_strategy(),
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=_build_utbreakout_keyboard(),
                    )
                elif mode in {'status', 'stat', 'menu', ''}:
                    await _send_crowding_unwind_status(u.message)
                else:
                    await u.message.reply_text(
                        'Usage: `/utbreak crowding on`, `/utbreak crowding off`, `/utbreak crowding status`',
                        parse_mode=ParseMode.MARKDOWN,
                    )
                    return
            elif action in {'lxr', 'liquidation_reversal'}:
                mode = str(args[1]).strip().lower() if len(args) > 1 else 'status'
                if mode in {'on', 'enable', 'start', 'live'}:
                    await u.message.reply_text(
                        await _activate_liquidation_exhaustion_reversal_strategy(),
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=_build_utbreakout_keyboard(),
                    )
                elif mode in {'off', 'disable', 'stop'}:
                    await u.message.reply_text(
                        await _deactivate_liquidation_exhaustion_reversal_strategy(),
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=_build_utbreakout_keyboard(),
                    )
                elif mode in {'status', 'stat', 'menu', ''}:
                    await _send_liquidation_exhaustion_reversal_status(u.message)
                else:
                    await u.message.reply_text(
                        'Usage: `/utbreak lxr on`, `/utbreak lxr off`, `/utbreak lxr status`',
                        parse_mode=ParseMode.MARKDOWN,
                    )
                    return
            elif action in {'qh', 'qhflow', 'qh_flow'}:
                mode = str(args[1]).strip().lower() if len(args) > 1 else 'status'
                if mode in {'on', 'enable', 'start', 'live'}:
                    await u.message.reply_text(
                        await _activate_qh_flow_strategy(),
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=_build_utbreakout_keyboard(),
                    )
                elif mode in {'off', 'disable', 'stop'}:
                    await u.message.reply_text(
                        await _deactivate_qh_flow_strategy(),
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=_build_utbreakout_keyboard(),
                    )
                elif mode in {'status', 'stat', 'menu', ''}:
                    await _send_qh_flow_status(u.message)
                else:
                    await u.message.reply_text('Usage: `/utbreak qh on`, `/utbreak qh off`, `/utbreak qh status`', parse_mode=ParseMode.MARKDOWN)
                    return
            elif action in {'triple', 'triplealpha', 'triple_alpha'}:
                mode = str(args[1]).strip().lower() if len(args) > 1 else 'status'
                if mode in {'on', 'enable', 'start', 'live'}:
                    await u.message.reply_text(
                        await _activate_triple_alpha_strategy(),
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=_build_utbreakout_keyboard(),
                    )
                elif mode in {'off', 'disable', 'stop'}:
                    await u.message.reply_text(
                        await _deactivate_triple_alpha_strategy(),
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=_build_utbreakout_keyboard(),
                    )
                elif mode in {'status', 'stat', 'menu', ''}:
                    await _send_triple_alpha_status(u.message)
                else:
                    await u.message.reply_text('Usage: `/utbreak triple on`, `/utbreak triple off`, `/utbreak triple status`', parse_mode=ParseMode.MARKDOWN)
                    return
            elif action in {'quad', 'quadalpha', 'quad_alpha', 'five', 'all5'}:
                mode = str(args[1]).strip().lower() if len(args) > 1 else 'status'
                if mode in {'on', 'enable', 'start', 'live'}:
                    await u.message.reply_text(
                        await _activate_quad_alpha_strategy(),
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=_build_utbreakout_keyboard(),
                    )
                elif mode in {'off', 'disable', 'stop'}:
                    await u.message.reply_text(
                        await _deactivate_quad_alpha_strategy(),
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=_build_utbreakout_keyboard(),
                    )
                elif mode in {'status', 'stat', 'menu', ''}:
                    await _send_quad_alpha_status(u.message)
                else:
                    await u.message.reply_text(
                        'Usage: `/utbreak quad on`, `/utbreak quad off`, `/utbreak quad status`',
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=_build_utbreakout_keyboard(),
                    )
                    return
            elif action in {'dual', 'dualt', 'dualalpha', 'dual_alpha'}:
                mode = str(args[1]).strip().lower() if len(args) > 1 else 'status'
                if mode in {'on', 'enable', 'start', 'live'}:
                    await u.message.reply_text(
                        await _activate_dual_alpha_strategy(),
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=_build_utbreakout_keyboard(),
                    )
                elif mode in {'off', 'disable', 'stop'}:
                    await u.message.reply_text(
                        await _deactivate_dual_alpha_strategy(),
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=_build_utbreakout_keyboard(),
                    )
                elif mode in {'status', 'stat', 'menu', ''}:
                    await _send_dual_alpha_status(u.message)
                else:
                    await u.message.reply_text(
                        "Usage: `/utbreak dual on`, `/utbreak dual off`, `/utbreak dual status`",
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=_build_utbreakout_keyboard(),
                    )
                    return
            elif action == 'set' or re.fullmatch(r'set\d+', action or '') or action in {'1', '2', '3', '4', '5', '6', '7', '8', '9', '10', 'aggressive', 'conservative'}:
                set_arg = args[1] if action == 'set' and len(args) > 1 else action
                set_text = str(set_arg or '').strip().lower()
                set_id = 1 if set_text == 'aggressive' else 7 if set_text == 'conservative' else normalize_utbreakout_set_id(set_arg, UTBREAKOUT_DEFAULT_SET_ID)
                if set_id > UTBREAKOUT_ACTIVE_SET_MAX:
                    await u.message.reply_text("❌ 현재 선택 가능한 범위는 Set 1~60입니다.")
                    return
                profile_cfg = _merge_utbreakout_profile_with_preserved_settings(f"set{set_id}")
                await self.cfg.update_value(
                    ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1'],
                    profile_cfg
                )
                self._reset_signal_engine_runtime_state(reset_stateful_strategy=True)
                info = get_utbreakout_set_definition(set_id)
                await u.message.reply_text(f"✅ Set{set_id} 적용: {info.get('name')} (AUTO OFF, 수동 선택)")
            elif action in {'risk', 'maxrisk', 'loss', 'max_loss'}:
                try:
                    value = _parse_positive_arg('1회 최대 손실 USDT', minimum=0.0, maximum=100000.0)
                except ValueError as e:
                    await u.message.reply_text(f"❌ {e}\n예: `/utbreak risk 5`", parse_mode=ParseMode.MARKDOWN)
                    return
                await self.cfg.update_value(
                    ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'max_risk_per_trade_usdt'],
                    value
                )
                self._reset_signal_engine_runtime_state(reset_stateful_strategy=True)
                await u.message.reply_text(f"✅ 1회 최대 손실 설정: ${value:.2f}")
            elif action in {'riskpct', 'risk_pct', 'riskpercent', 'risk_percent'}:
                try:
                    value = _parse_positive_arg(
                        '잔고 대비 손실 기준(%)',
                        minimum=DEFAULT_MIN_RISK_PER_TRADE_PERCENT,
                        maximum=DEFAULT_MAX_RISK_PER_TRADE_PERCENT
                    )
                except ValueError as e:
                    await u.message.reply_text(f"❌ {e}\n예: `/utbreak riskpct 0.5`", parse_mode=ParseMode.MARKDOWN)
                    return
                await self.cfg.update_value(
                    ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'risk_per_trade_percent'],
                    value
                )
                self._reset_signal_engine_runtime_state(reset_stateful_strategy=True)
                await u.message.reply_text(f"✅ 잔고 대비 손실 기준 설정: {value:.2f}%")
            elif action in {'dailyloss', 'daily_loss', 'dayloss', 'day_loss'}:
                try:
                    value = _parse_positive_arg('하루 최대 손실 USDT', minimum=0.0, maximum=1000000.0)
                except ValueError as e:
                    await u.message.reply_text(f"❌ {e}\n예: `/utbreak dailyloss 30`", parse_mode=ParseMode.MARKDOWN)
                    return
                await self.cfg.update_value(
                    ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'daily_max_loss_usdt'],
                    value
                )
                self._reset_signal_engine_runtime_state(reset_stateful_strategy=True)
                await u.message.reply_text(f"✅ 하루 최대 손실 설정: ${value:.2f}")
            elif action in {'dailytrades', 'daily_trades', 'daytrades', 'day_trades', 'maxtrades', 'max_trades'}:
                requested = str(args[1]).strip().lower() if len(args) > 1 else 'status'
                if requested in {'status', 'stat', 'menu', ''}:
                    await self._send_automatic_trading_controls(u.message)
                    return
                if requested != '10':
                    await u.message.reply_text(
                        "❌ 자동매매 기본 한도는 5회입니다. 하루 한 번만 "
                        "`/utbreak dailytrades 10`으로 당일 한도를 확장할 수 있습니다.",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                    return
                changed, _ = await self.extend_automatic_daily_trade_limit_for_today()
                notice = (
                    "✅ 오늘 자동매매 한도를 10회로 확장했습니다."
                    if changed
                    else "⛔ 오늘은 이미 10회 확장을 사용했습니다."
                )
                await self._send_automatic_trading_controls(u.message, notice)
            elif action in {'toggle_opposite_set', 'opposite_set', 'setexit', 'set_exit'}:
                raw = _current_utbreakout_cfg()
                current = bool(raw.get('opposite_set_exit_enabled', False))
                await self.cfg.update_value(
                    ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'opposite_set_exit_enabled'],
                    not current
                )
                if not current:
                    await self.cfg.update_value(
                        ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'opposite_signal_exit_enabled'],
                        False
                    )
                self._reset_signal_engine_runtime_state(reset_stateful_strategy=True)
                await u.message.reply_text(
                    f"✅ 반대Set청산: {'ON' if not current else 'OFF'}\n"
                    "조건: 반대 UT 신규신호 + 반대 방향 선택 Set 조건 통과 + 최소 보유 조건. PnL 조건은 별도 옵션이며 기본 OFF입니다. 청산만 하고 반대진입은 하지 않습니다."
                )
            elif action in {'opphold', 'opposite_hold', 'sethold', 'set_hold'}:
                try:
                    raw_value = _parse_float_arg('반대Set청산 최소 보유 캔들', minimum=0.0, maximum=1000.0)
                    if raw_value != int(raw_value):
                        raise ValueError("반대Set청산 최소 보유 캔들은 정수로 입력하세요.")
                    value = int(raw_value)
                except ValueError as e:
                    await u.message.reply_text(f"❌ {e}\n예: `/utbreak opphold 3`", parse_mode=ParseMode.MARKDOWN)
                    return
                await self.cfg.update_value(
                    ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'opposite_set_exit_min_hold_candles'],
                    value
                )
                self._reset_signal_engine_runtime_state(reset_stateful_strategy=True)
                await u.message.reply_text(f"✅ 반대Set청산 최소 보유: {value}캔들")
            elif action in {'opppnl', 'opposite_pnl', 'setpnl', 'set_pnl'}:
                mode = str(args[1]).strip().lower() if len(args) > 1 else ''
                if mode in {'off', 'disable', 'false'}:
                    await self.cfg.update_value(
                        ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'opposite_set_exit_min_pnl_enabled'],
                        False
                    )
                    self._reset_signal_engine_runtime_state(reset_stateful_strategy=True)
                    await u.message.reply_text("✅ 반대Set청산 PnL 조건: OFF")
                    return
                if mode in {'on', 'enable', 'true'}:
                    raw = _current_utbreakout_cfg()
                    value = float(raw.get('opposite_set_exit_min_pnl_usdt', 0.0) or 0.0)
                    await self.cfg.update_value(
                        ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'opposite_set_exit_min_pnl_enabled'],
                        True
                    )
                    self._reset_signal_engine_runtime_state(reset_stateful_strategy=True)
                    await u.message.reply_text(f"✅ 반대Set청산 PnL 조건: ON / 최소 ${value:.2f}")
                    return
                try:
                    value = _parse_float_arg('반대Set청산 최소 미실현손익 USDT', minimum=-1000000.0, maximum=1000000.0)
                except ValueError as e:
                    await u.message.reply_text(f"❌ {e}\n예: `/utbreak opppnl off`, `/utbreak opppnl 0`, `/utbreak opppnl -25`", parse_mode=ParseMode.MARKDOWN)
                    return
                await self.cfg.update_value(
                    ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'opposite_set_exit_min_pnl_enabled'],
                    True
                )
                await self.cfg.update_value(
                    ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'opposite_set_exit_min_pnl_usdt'],
                    value
                )
                self._reset_signal_engine_runtime_state(reset_stateful_strategy=True)
                await u.message.reply_text(f"✅ 반대Set청산 PnL 조건: ON / 최소 ${value:.2f}")
            elif action in {'toggle_opposite', 'opposite'}:
                await self.cfg.update_value(
                    ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'opposite_signal_exit_enabled'],
                    False
                )
                self._reset_signal_engine_runtime_state(reset_stateful_strategy=True)
                await u.message.reply_text("✅ 단순 반대UT청산(A)은 기각 옵션이라 OFF로 유지합니다. 반대Set청산(B)은 `/utbreak toggle_opposite_set`으로 켜세요.", parse_mode=ParseMode.MARKDOWN)
            elif action in {'toggle_ema', 'ema'}:
                raw = _current_utbreakout_cfg()
                current = bool(raw.get('ema_rsi_exit_enabled', False))
                await self.cfg.update_value(
                    ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'ema_rsi_exit_enabled'],
                    not current
                )
                await u.message.reply_text(f"✅ EMA50/RSI 청산: {'ON' if not current else 'OFF'}")
            elif action in {'toggle_extreme', 'extreme'}:
                raw = _current_utbreakout_cfg()
                current = bool(raw.get('exclude_rsi_extreme', False))
                await self.cfg.update_value(
                    ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'exclude_rsi_extreme'],
                    not current
                )
                await u.message.reply_text(f"✅ RSI 과열 제외 옵션: {'ON' if not current else 'OFF'}")
            elif action in {'config', 'cfg'}:
                await self._reply_markdown_safe(
                    u.message,
                    _format_utbreakout_config_text(diff=False),
                    reply_markup=_build_utbreakout_keyboard(),
                    filename='utbreakout_effective_config.txt',
                    caption='UT Breakout effective config입니다.',
                    preview_suffix='상세 effective config는 파일로 보냈습니다.',
                )
                return
            elif action in {'configdiff', 'cfgdiff', 'diff'}:
                await self._reply_markdown_safe(
                    u.message,
                    _format_utbreakout_config_text(diff=True),
                    reply_markup=_build_utbreakout_keyboard(),
                    filename='utbreakout_config_diff.txt',
                    caption='UT Breakout raw/effective config diff입니다.',
                    preview_suffix='상세 config diff는 파일로 보냈습니다.',
                )
                return
            elif action in {'log', 'logs', 'download'}:
                await _send_utbreakout_log_document(u.message)
                return
            elif action in {'sets', 'catalog', 'list'}:
                page = args[1] if len(args) > 1 else 1
                await u.message.reply_text(_format_utbreakout_sets_text(page), reply_markup=_build_utbreakout_keyboard())
                return
            elif action in {'why', 'reason', 'auto_reason'}:
                await u.message.reply_text(_format_utbreakout_why_text(), reply_markup=_build_utbreakout_keyboard())
                return
            elif action in {'research', 'report'}:
                if len(args) > 1 and str(args[1]).strip().lower() in {'download', 'file', 'log'}:
                    await _send_utbreakout_research_document(u.message)
                else:
                    await u.message.reply_text(_format_utbreakout_research_text(), reply_markup=_build_utbreakout_keyboard())
                return
            elif action in {'status', 'conditions', 'condition_status'}:
                await _send_utbreakout_condition_status(u.message)
                return
            elif action in {'trace'}:
                symbol_arg = args[1] if len(args) > 1 else None
                await _send_utbreakout_trace(u.message, symbol_arg, full=False)
                return
            elif action in {'tracefull', 'trace_full', 'fulltrace'}:
                symbol_arg = args[1] if len(args) > 1 else None
                await _send_utbreakout_trace(u.message, symbol_arg, full=True)
                return
            elif action in {'watchdog', 'tracewatchdog', 'trace_watchdog'}:
                engine = self.engines.get('signal')
                if not engine:
                    await u.message.reply_text("⚠️ watchdog 설정 실패: Signal 엔진을 찾을 수 없습니다.")
                    return
                mode = str(args[1]).strip().lower() if len(args) > 1 else ''
                if mode in {'on', 'enable', '1', 'true'}:
                    engine.utbreakout_trace_watchdog_enabled = True
                elif mode in {'off', 'disable', '0', 'false'}:
                    engine.utbreakout_trace_watchdog_enabled = False
                elif mode:
                    await u.message.reply_text(
                        "❌ 예: `/utbreak watchdog on` 또는 `/utbreak watchdog off`",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                    return
                state = bool(
                    getattr(engine, 'utbreakout_trace_watchdog_enabled', False)
                )
                await u.message.reply_text(
                    f"UTBreakout trace watchdog 진단 기록: {'ON' if state else 'OFF'} "
                    "(텔레그램 자동 발송 없음)"
                )
                return
            elif action in {'bridge', 'autoentrybridge', 'auto_entry_bridge'}:
                engine = self.engines.get('signal')
                if not engine:
                    await u.message.reply_text(
                        "⚠️ bridge 설정 실패: Signal 엔진을 찾을 수 없습니다."
                    )
                    return
                engine._ensure_utbreakout_auto_entry_bridge_state()
                mode = str(args[1]).strip().lower() if len(args) > 1 else ''
                old_state = bool(engine.utbreakout_auto_entry_bridge_enabled)
                if mode in {'on', 'enable', '1', 'true'}:
                    engine.utbreakout_auto_entry_bridge_enabled = True
                elif mode in {'off', 'disable', '0', 'false'}:
                    engine.utbreakout_auto_entry_bridge_enabled = False
                elif mode:
                    await u.message.reply_text(
                        "❌ 예: `/utbreak bridge on` 또는 `/utbreak bridge off`",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                    return
                state = bool(engine.utbreakout_auto_entry_bridge_enabled)
                logger.info(
                    "UTBREAK_BRIDGE_STATE changed old=%s new=%s source=telegram_command persisted=%s",
                    old_state,
                    state,
                    'runtime',
                )
                await u.message.reply_text(
                    "UTBreakout Auto Entry Bridge: "
                    + ("ON" if state else "OFF")
                    + "\n사용법: /utbreak bridge on 또는 /utbreak bridge off"
                )
                return
            elif action in {'forceentry', 'force_entry', 'retryentry', 'retry_entry'}:
                await _force_utbreakout_entry(u.message)
                return
            elif action in {'analyze', 'analysis', 'entry_analyze', 'why_entry', 'debug'}:
                symbol_arg = args[1] if len(args) > 1 else None
                await _send_utbreakout_entry_analysis(u.message, symbol_arg)
                return
            elif action in {'menu', ''}:
                pass
            else:
                await u.message.reply_text("❌ 알 수 없는 UT Breakout 명령입니다. `/utbreak`로 메뉴를 확인하세요.", parse_mode=ParseMode.MARKDOWN)
                return

            await self._reply_markdown_safe(
                u.message,
                _format_utbreakout_menu_text(),
                reply_markup=_build_utbreakout_keyboard()
            )

        async def utbreakout_callback(u: Update, c: ContextTypes.DEFAULT_TYPE):
            query = u.callback_query
            if not query:
                return
            await query.answer("처리 중입니다.")
            data = str(query.data or '')
            if not data.startswith('utb:'):
                return
            parts = data.split(':')
            action = parts[1] if len(parts) > 1 else 'status'
            value = parts[2] if len(parts) > 2 else None
            if action not in UTBREAKOUT_CALLBACK_ACTIONS and not re.fullmatch(r'set\d+', action or ''):
                await _edit_utbreakout_menu(
                    query,
                    "ℹ️ 알 수 없는 이전 버전 UTBreak 버튼입니다. `/utbreak`로 최신 메뉴를 다시 열어주세요."
                )
                return

            if action == 'qselect':
                draft = _configured_quad_alpha_strategies()
                if c is not None and c.user_data is not None:
                    c.user_data['quad_alpha_selection_draft'] = list(draft)
                await query.edit_message_text(
                    quad_alpha_selection_text(draft),
                    reply_markup=build_quad_alpha_selection_keyboard(draft),
                )
                return

            if action == 'qsel':
                user_data = c.user_data if c is not None and c.user_data is not None else {}
                draft = normalize_quad_alpha_enabled_strategies(
                    user_data.get('quad_alpha_selection_draft', _configured_quad_alpha_strategies())
                )
                selection_action = str(value or '').lower()
                if selection_action == 'cancel':
                    user_data.pop('quad_alpha_selection_draft', None)
                    await _edit_utbreakout_menu(query, 'Strategy selection cancelled.')
                    return
                if selection_action == 'apply':
                    if not draft:
                        await query.edit_message_text(
                            'Select at least one strategy before APPLY. Use 5-ALL OFF to stop new entries.\n\n'
                            + quad_alpha_selection_text(draft),
                            reply_markup=build_quad_alpha_selection_keyboard(draft),
                        )
                        return
                    notice = await _activate_quad_alpha_strategy(draft)
                    user_data.pop('quad_alpha_selection_draft', None)
                    await _edit_utbreakout_menu(query, notice)
                    return
                strategy_key = QUAD_ALPHA_SELECTOR_KEYS.get(selection_action)
                if strategy_key is None:
                    await _edit_utbreakout_menu(query, 'Unknown strategy selection.')
                    return
                selected = set(draft)
                if strategy_key in selected:
                    selected.remove(strategy_key)
                else:
                    selected.add(strategy_key)
                draft = [key for key in QUAD_ALPHA_BRANCH_ORDER if key in selected]
                user_data['quad_alpha_selection_draft'] = list(draft)
                await query.edit_message_text(
                    quad_alpha_selection_text(draft),
                    reply_markup=build_quad_alpha_selection_keyboard(draft),
                )
                return

            if action == 'on':
                await _edit_utbreakout_menu(
                    query,
                    await _set_quad_alpha_branch_enabled(ENTRY_STRATEGY_UT_BREAKOUT, True),
                )
                return

            if action == 'off':
                await _edit_utbreakout_menu(
                    query,
                    await _set_quad_alpha_branch_enabled(ENTRY_STRATEGY_UT_BREAKOUT, False),
                )
                return

            if action == 'pause':
                await _edit_utbreakout_menu(query, await _set_strategy_pause(True))
                return

            if action == 'resume':
                await _edit_utbreakout_menu(query, await _set_strategy_pause(False))
                return

            if action == 'watchlist':
                await _prompt_utbreak_watchlist_input(query, c)
                return

            if action == 'fixed':
                await _prompt_utbreak_single_coin_input(query, c)
                return

            if action == 'fixed_off':
                await _edit_utbreakout_menu(query, await _disable_single_fixed_coin())
                return

            if action == 'auto_scan':
                await _prompt_utbreak_autoscan_input(query, c)
                return

            if action == 'auto_scan_off':
                await _edit_utbreakout_menu(query, await _disable_customcoins())
                return

            if action == 'coin_auto':
                mode = str(value or '').lower()
                if mode in {'on', 'enable', '1', 'true'}:
                    await _edit_utbreakout_menu(query, await _enable_coin_auto_selection())
                elif mode in {'off', 'disable', '0', 'false'}:
                    await _edit_utbreakout_menu(query, await _disable_coin_auto_selection())
                else:
                    await _edit_utbreakout_menu(query, "❌ 코인 자동 선택 버튼 값 처리 실패")
                return

            if action == 'lev':
                try:
                    await _edit_utbreakout_menu(query, f"✅ 레버리지: {await _set_common_leverage(value)}x")
                except Exception as e:
                    await _edit_utbreakout_menu(query, f"❌ {e}")
                return

            if action == 'tf':
                try:
                    await _edit_utbreakout_menu(query, f"✅ 진입 TF: {await _set_common_entry_tf(value)}")
                except Exception as e:
                    await _edit_utbreakout_menu(query, f"❌ {e}")
                return

            if action == 'exit_tf':
                try:
                    await _edit_utbreakout_menu(query, f"✅ 청산 TF: {await _set_common_exit_tf(value)}")
                except Exception as e:
                    await _edit_utbreakout_menu(query, f"❌ {e}")
                return

            if action == 'target':
                try:
                    await _edit_utbreakout_menu(query, f"✅ {await _set_take_profit(value)}")
                except Exception as e:
                    await _edit_utbreakout_menu(query, f"❌ {e}")
                return

            if action == 'stop':
                try:
                    await _edit_utbreakout_menu(query, f"✅ {await _set_stop_loss(value)}")
                except Exception as e:
                    await _edit_utbreakout_menu(query, f"❌ {e}")
                return

            if action == 'micro':
                mode = str(value or '').lower()
                if mode in {'on', 'dry', 'dryrun'}:
                    await _enable_microauto(dry_run=True)
                    await _edit_utbreakout_menu(query, "✅ Micro Auto: ON / DRY-RUN ON")
                elif mode in {'live', 'live_on'}:
                    await _enable_microauto(dry_run=False)
                    await _edit_utbreakout_menu(query, "✅ Micro Auto: LIVE ON")
                elif mode in {'off', 'disable', 'stop'}:
                    await self.cfg.update_value(['signal_engine', 'micro_auto', 'enabled'], False)
                    self._reset_signal_engine_runtime_state(reset_stateful_strategy=True)
                    await _edit_utbreakout_menu(query, "✅ Micro Auto: OFF")
                else:
                    await _edit_utbreakout_menu(query, "❌ Micro Auto 버튼 값 처리 실패")
                return

            if action in {'auto', 'auto_bundle'}:
                enabled = str(value or '').lower() in {'on', 'enable', '1', 'true'}
                notice = (
                    await _enable_utbreak_auto_bundle()
                    if enabled else
                    await _disable_utbreak_auto_bundle()
                )
                await _edit_utbreakout_menu(query, notice)
                return

            if action == 'adaptive':
                enabled = str(value or '').lower() in {'on', 'enable', '1', 'true'}
                await self.cfg.update_value(
                    ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'adaptive_timeframe_enabled'],
                    enabled
                )
                await self.cfg.update_value(
                    ['signal_engine', 'strategy_params', 'active_strategy'],
                    UTBOT_ADAPTIVE_TIMEFRAME_STRATEGY if enabled else UTBOT_FILTERED_BREAKOUT_STRATEGY
                )
                self._reset_signal_engine_runtime_state(
                    reset_entry_cache=True,
                    reset_exit_cache=True,
                    reset_stateful_strategy=True
                )
                await _edit_utbreakout_menu(
                    query,
                    f"✅ Adaptive 시간봉 전략: {'ON' if enabled else 'OFF'}"
                )
                return

            if action in {'rsp', 'rspt'}:
                mode = str(value or '').lower()
                if mode in {'on', 'enable', '1', 'true', 'live'}:
                    await _edit_utbreakout_menu(
                        query,
                        await _set_quad_alpha_branch_enabled(
                            ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND,
                            True,
                        ),
                    )
                elif mode in {'off', 'disable', '0', 'false', 'stop'}:
                    await _edit_utbreakout_menu(
                        query,
                        await _set_quad_alpha_branch_enabled(
                            ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND,
                            False,
                        ),
                    )
                else:
                    await _send_relative_strength_pullback_status_from_callback(query)
                return

            if action in {'crowding', 'crowd'}:
                mode = str(value or '').lower()
                if mode in {'on', 'enable', '1', 'true', 'live'}:
                    await _edit_utbreakout_menu(
                        query,
                        await _set_quad_alpha_branch_enabled(CROWDING_UNWIND_STRATEGY, True),
                    )
                elif mode in {'off', 'disable', '0', 'false', 'stop'}:
                    await _edit_utbreakout_menu(
                        query,
                        await _set_quad_alpha_branch_enabled(CROWDING_UNWIND_STRATEGY, False),
                    )
                else:
                    await _send_crowding_unwind_status_from_callback(query)
                return

            if action in {'lxr', 'liquidation_reversal'}:
                mode = str(value or '').lower()
                if mode in {'on', 'enable', '1', 'true', 'live'}:
                    await _edit_utbreakout_menu(
                        query,
                        await _set_quad_alpha_branch_enabled(LXR_STRATEGY, True),
                    )
                elif mode in {'off', 'disable', '0', 'false', 'stop'}:
                    await _edit_utbreakout_menu(
                        query,
                        await _set_quad_alpha_branch_enabled(LXR_STRATEGY, False),
                    )
                else:
                    await _send_liquidation_exhaustion_reversal_status_from_callback(query)
                return

            if action in {'qh', 'qhflow'}:
                mode = str(value or '').lower()
                if mode in {'on', 'enable', '1', 'true', 'live'}:
                    await _edit_utbreakout_menu(
                        query,
                        await _set_quad_alpha_branch_enabled(QH_FLOW_STRATEGY, True),
                    )
                elif mode in {'off', 'disable', '0', 'false', 'stop'}:
                    await _edit_utbreakout_menu(
                        query,
                        await _set_quad_alpha_branch_enabled(QH_FLOW_STRATEGY, False),
                    )
                else:
                    await _send_qh_flow_status_from_callback(query)
                return

            if action in {'triple', 'triplet'}:
                mode = str(value or '').lower()
                if mode in {'on', 'enable', '1', 'true', 'live'}:
                    await _edit_utbreakout_menu(query, await _activate_triple_alpha_strategy())
                elif mode in {'off', 'disable', '0', 'false', 'stop'}:
                    await _edit_utbreakout_menu(query, await _deactivate_triple_alpha_strategy())
                else:
                    await _send_triple_alpha_status_from_callback(query)
                return

            if action in {'quad', 'quadalpha'}:
                mode = str(value or '').lower()
                if mode in {'on', 'enable', '1', 'true', 'live'}:
                    await _edit_utbreakout_menu(query, await _activate_quad_alpha_strategy())
                elif mode in {'off', 'disable', '0', 'false', 'stop'}:
                    await _edit_utbreakout_menu(query, await _deactivate_quad_alpha_strategy())
                else:
                    await _send_quad_alpha_status_from_callback(query)
                return

            if action in {'dual', 'dualt'}:
                mode = str(value or '').lower()
                if mode in {'on', 'enable', '1', 'true', 'live'}:
                    await _edit_utbreakout_menu(query, await _activate_dual_alpha_strategy())
                elif mode in {'off', 'disable', '0', 'false', 'stop'}:
                    await _edit_utbreakout_menu(query, await _deactivate_dual_alpha_strategy())
                else:
                    await _send_dual_alpha_status_from_callback(query)
                return

            if action == 'rsp_status':
                await _send_relative_strength_pullback_status_from_callback(query)
                return

            if action == 'qh_status':
                await _send_qh_flow_status_from_callback(query)
                return

            if action == 'crowding_status':
                await _send_crowding_unwind_status_from_callback(query)
                return

            if action == 'lxr_status':
                await _send_liquidation_exhaustion_reversal_status_from_callback(query)
                return

            if action == 'dual_status':
                await _send_dual_alpha_status_from_callback(query)
                return

            if action == 'triple_status':
                await _send_triple_alpha_status_from_callback(query)
                return

            if action == 'quad_status':
                await _send_quad_alpha_status_from_callback(query)
                return

            if action == 'set' or re.fullmatch(r'set\d+', action or ''):
                set_arg = value if action == 'set' else action
                set_id = normalize_utbreakout_set_id(set_arg, UTBREAKOUT_DEFAULT_SET_ID)
                if set_id > UTBREAKOUT_ACTIVE_SET_MAX:
                    await _edit_utbreakout_menu(query, "❌ 현재 선택 가능한 범위는 Set 1~60입니다.")
                    return
                profile_cfg = _merge_utbreakout_profile_with_preserved_settings(f"set{set_id}")
                await self.cfg.update_value(
                    ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1'],
                    profile_cfg
                )
                self._reset_signal_engine_runtime_state(reset_stateful_strategy=True)
                info = get_utbreakout_set_definition(set_id)
                await _edit_utbreakout_menu(query, f"✅ Set{set_id} 적용: {info.get('name')} (AUTO OFF)")
                return

            if action == 'sets':
                try:
                    await query.edit_message_text(
                        _format_utbreakout_sets_text(value or 1),
                        reply_markup=_build_utbreakout_keyboard()
                    )
                except BadRequest as md_err:
                    if "message is not modified" in str(md_err).lower():
                        return
                    raise
                return

            if action == 'why':
                try:
                    await query.edit_message_text(
                        _format_utbreakout_why_text(),
                        reply_markup=_build_utbreakout_keyboard()
                    )
                except BadRequest as md_err:
                    if "message is not modified" in str(md_err).lower():
                        return
                    raise
                return

            if action == 'dailytrades':
                if str(value or '').strip() != '10':
                    await _edit_utbreakout_menu(
                        query,
                        "❌ 자동매매 기본 한도는 5회이며 당일 10회 확장만 허용됩니다.",
                    )
                    return
                changed, _ = await self.extend_automatic_daily_trade_limit_for_today()
                await _edit_utbreakout_menu(
                    query,
                    (
                        "✅ 오늘 자동매매 한도를 10회로 확장했습니다."
                        if changed
                        else "⛔ 오늘은 이미 10회 확장을 사용했습니다."
                    ),
                )
                return

            if action in {'risk', 'riskpct', 'dailyloss'}:
                try:
                    numeric_value = float(str(value or '').replace('$', '').replace(',', '').strip())
                except (TypeError, ValueError):
                    await _edit_utbreakout_menu(query, "❌ 버튼 값 처리 실패")
                    return
                if numeric_value <= 0:
                    await _edit_utbreakout_menu(query, "❌ 손실 설정값은 0보다 커야 합니다.")
                    return
                if (
                    action == 'riskpct'
                    and (
                        numeric_value < DEFAULT_MIN_RISK_PER_TRADE_PERCENT
                        or numeric_value > DEFAULT_MAX_RISK_PER_TRADE_PERCENT
                    )
                ):
                    await _edit_utbreakout_menu(
                        query,
                        f"❌ 잔고 대비 손실 기준은 "
                        f"{DEFAULT_MIN_RISK_PER_TRADE_PERCENT:.2f}~{DEFAULT_MAX_RISK_PER_TRADE_PERCENT:.2f}% 사이여야 합니다."
                    )
                    return
                if action == 'risk':
                    await self.cfg.update_value(
                        ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'max_risk_per_trade_usdt'],
                        numeric_value
                    )
                    notice = f"✅ 1회 최대 손실 설정: ${numeric_value:.2f}"
                elif action == 'riskpct':
                    await self.cfg.update_value(
                        ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'risk_per_trade_percent'],
                        numeric_value
                    )
                    notice = f"✅ 잔고 대비 손실 기준 설정: {numeric_value:.2f}%"
                else:
                    await self.cfg.update_value(
                        ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'daily_max_loss_usdt'],
                        numeric_value
                    )
                    notice = f"✅ 하루 최대 손실 설정: ${numeric_value:.2f}"
                self._reset_signal_engine_runtime_state(reset_stateful_strategy=True)
                await _edit_utbreakout_menu(query, notice)
                return

            if action in {'opphold', 'opppnl'}:
                if action == 'opppnl' and str(value or '').strip().lower() in {'off', 'disable', 'false'}:
                    await self.cfg.update_value(
                        ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'opposite_set_exit_min_pnl_enabled'],
                        False
                    )
                    self._reset_signal_engine_runtime_state(reset_stateful_strategy=True)
                    await _edit_utbreakout_menu(query, "✅ 반대Set청산 PnL 조건: OFF")
                    return
                try:
                    numeric_value = float(str(value or '').replace('$', '').replace(',', '').strip())
                except (TypeError, ValueError):
                    await _edit_utbreakout_menu(query, "❌ 버튼 값 처리 실패")
                    return
                if action == 'opphold':
                    if numeric_value < 0 or numeric_value != int(numeric_value):
                        await _edit_utbreakout_menu(query, "❌ 최소 보유 캔들은 0 이상의 정수여야 합니다.")
                        return
                    hold_value = int(numeric_value)
                    await self.cfg.update_value(
                        ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'opposite_set_exit_min_hold_candles'],
                        hold_value
                    )
                    notice = f"✅ 반대Set청산 최소 보유: {hold_value}캔들"
                else:
                    await self.cfg.update_value(
                        ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'opposite_set_exit_min_pnl_enabled'],
                        True
                    )
                    await self.cfg.update_value(
                        ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'opposite_set_exit_min_pnl_usdt'],
                        numeric_value
                    )
                    notice = f"✅ 반대Set청산 PnL 조건: ON / 최소 ${numeric_value:.2f}"
                self._reset_signal_engine_runtime_state(reset_stateful_strategy=True)
                await _edit_utbreakout_menu(query, notice)
                return

            if action == 'toggle_opposite':
                await self.cfg.update_value(
                    ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'opposite_signal_exit_enabled'],
                    False
                )
                self._reset_signal_engine_runtime_state(reset_stateful_strategy=True)
                await _edit_utbreakout_menu(query, "✅ 단순 반대UT청산(A)은 기각 옵션이라 OFF로 유지합니다.")
                return

            if action in {'toggle_opposite_set', 'toggle_ema', 'toggle_extreme'}:
                key_map = {
                    'toggle_opposite_set': ('opposite_set_exit_enabled', '반대Set청산'),
                    'toggle_ema': ('ema_rsi_exit_enabled', 'EMA50/RSI 청산'),
                    'toggle_extreme': ('exclude_rsi_extreme', 'RSI 과열 제외 옵션')
                }
                key, label = key_map[action]
                current = bool(_current_utbreakout_cfg().get(key, False))
                await self.cfg.update_value(
                    ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', key],
                    not current
                )
                if action == 'toggle_opposite_set' and not current:
                    await self.cfg.update_value(
                        ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'opposite_signal_exit_enabled'],
                        False
                    )
                self._reset_signal_engine_runtime_state(reset_stateful_strategy=True)
                await _edit_utbreakout_menu(query, f"✅ {label}: {'ON' if not current else 'OFF'}")
                return

            if action == 'download':
                await _send_utbreakout_log_document(query.message)
                return

            if action == 'research_download':
                await _send_utbreakout_research_document(query.message)
                return

            if action == 'research':
                try:
                    await query.edit_message_text(
                        _format_utbreakout_research_text(),
                        reply_markup=_build_utbreakout_keyboard()
                    )
                except BadRequest as md_err:
                    if "message is not modified" in str(md_err).lower():
                        return
                    raise
                return

            if action in {'condition_status', 'conditions', 'status'}:
                await _send_utbreakout_condition_status_from_callback(query)
                return

            if action == 'entry_analyze':
                await _send_utbreakout_entry_analysis_from_callback(query)
                return

            await _edit_utbreakout_menu(query)

        def _format_volume_usdt(value):
            try:
                amount = float(value or 0.0)
            except (TypeError, ValueError):
                amount = 0.0
            if amount >= 1_000_000_000:
                return f"{amount / 1_000_000_000:.2f}B"
            if amount >= 1_000_000:
                return f"{amount / 1_000_000:.1f}M"
            return f"{amount:.0f}"

        def _coinscan_cfg():
            raw = self.cfg.get('signal_engine', {}).get('coin_selector', {})
            cfg = default_coin_selector_config()
            if isinstance(raw, dict):
                cfg.update(raw)
            return cfg

        def _build_coinscan_keyboard():
            return InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("자동선택 ON", callback_data="cs:on"),
                    InlineKeyboardButton("자동선택 OFF", callback_data="cs:off")
                ],
                [
                    InlineKeyboardButton("Top 10 후보", callback_data="cs:top"),
                    InlineKeyboardButton("watchlist 적용", callback_data="cs:apply")
                ],
                [
                    InlineKeyboardButton("제외 사유", callback_data="cs:rejects"),
                    InlineKeyboardButton("현재 기준", callback_data="cs:criteria")
                ],
                [
                    InlineKeyboardButton("섹터 제외", callback_data="cs:sectors"),
                    InlineKeyboardButton("리포트 다운로드", callback_data="cs:download")
                ],
                [
                    InlineKeyboardButton("새로고침", callback_data="cs:status")
                ]
            ])

        async def _run_coinscan(force=False):
            engine = self.engines.get('signal')
            if not engine:
                return {
                    'selected': [],
                    'reject_counts': {'NO_SIGNAL_ENGINE': 1},
                    'criteria': _coinscan_cfg(),
                    'note': 'Signal 엔진을 찾을 수 없습니다.',
                }
            return await engine.evaluate_coin_selector(force=force)

        def _format_coinscan_top_text(report):
            cfg = report.get('criteria') or _coinscan_cfg()
            selected = list(report.get('selected') or [])
            lines = [
                "🧭 CoinSelector V2 Top 후보",
                "코인 선택은 감시 대상 선정이고, 최종 진입은 기존 전략이 다시 판단합니다.",
                f"상태: {'ON' if cfg.get('enabled', True) else 'OFF'} | minVol {_format_volume_usdt(cfg.get('min_quote_volume_usdt'))} | top {int(cfg.get('top_n', 10) or 10)}",
                f"분석: base {report.get('total_base_candidates', 0)}개 / scored {report.get('total_scored', 0)}개 / rejected {report.get('total_rejected', 0)}개",
                "",
            ]
            if not selected:
                lines.append("아직 후보가 없습니다. `/coinscan top`으로 새로 스캔하거나 기준을 확인하세요.")
            for idx, item in enumerate(selected, 1):
                components = item.get('component_scores') or {}
                warnings = ", ".join(item.get('soft_warnings') or [])
                lines.extend([
                    f"{idx}. {item.get('normalized_symbol') or item.get('symbol')}",
                    (
                        f"점수 {float(item.get('score', 0) or 0):.1f} / 상태 {item.get('selection_state')} / "
                        f"거래대금 {_format_volume_usdt(item.get('quote_volume'))} / 변동 {float(item.get('percentage', 0) or 0):.2f}%"
                    ),
                    (
                        f"Set{item.get('auto_set_id') or '?'} {item.get('auto_set_name') or ''} / "
                        f"Adaptive TF {item.get('adaptive_tf')} / margin {float(item.get('auto_score_margin', 0) or 0):.1f}"
                    ),
                    (
                        "구성점수 "
                        f"liq {components.get('liquidity_cost', 0):.1f}, "
                        f"ut {components.get('utbreakout_regime', 0):.1f}, "
                        f"set {components.get('auto_set', 0):.1f}, "
                        f"tf {components.get('adaptive_tf', 0):.1f}, "
                        f"fut {components.get('futures_health', 0):.1f}, "
                        f"qual {components.get('selection_quality', 0):.1f}"
                    ),
                    (
                        f"품질 Sharpe {item.get('rolling_sharpe', 'n/a')} / "
                        f"ret {item.get('return_lookback_pct', 'n/a')}% / "
                        f"vol {item.get('realized_vol_pct', 'n/a')}% / "
                        f"cons {item.get('momentum_consistency', 'n/a')}"
                    ),
                ])
                if warnings:
                    lines.append(f"주의: {warnings}")
                lines.append("")
            warning = report.get('concentration_warning')
            if warning:
                lines.append(
                    f"쏠림 경고: {warning.get('key')}={warning.get('value')} "
                    f"{warning.get('share_pct')}% ({warning.get('count')}/{warning.get('total')})"
                )
            lines.append("명령: `/coinscan apply`로 현재 Top 후보를 watchlist에 반영")
            return "\n".join(lines).strip()

        def _format_coinscan_reject_text(report):
            counts = report.get('reject_counts') or {}
            lines = [
                "🚫 CoinSelector 제외 사유",
                f"총 제외: {report.get('total_rejected', 0)}개",
                "",
            ]
            if not counts:
                lines.append("제외 사유 기록이 없습니다.")
            else:
                for reason, count in sorted(counts.items(), key=lambda item: item[1], reverse=True):
                    lines.append(f"- {reason}: {count}")
            return "\n".join(lines).strip()

        def _format_coinscan_criteria_text():
            cfg = _coinscan_cfg()
            excluded = ", ".join(cfg.get('excluded_sectors') or []) or "없음"
            blacklist = ", ".join(cfg.get('blacklist') or []) or "없음"
            return "\n".join([
                "⚙️ CoinSelector V2 현재 기준",
                f"자동선택: {'ON' if cfg.get('enabled', True) else 'OFF'}",
                f"거래대금: 24h quoteVolume >= {_format_volume_usdt(cfg.get('min_quote_volume_usdt'))} USDT",
                f"스프레드: <= {float(cfg.get('max_spread_pct', 0.08) or 0.08):.3f}%",
                f"24h 변동 과열 제외: abs(change) <= {float(cfg.get('max_abs_price_change_pct', 18.0) or 18.0):.1f}%",
                f"최소 체결수: {int(float(cfg.get('min_trade_count', 20000) or 20000)):,}",
                f"분석 후보 수: {int(float(cfg.get('analysis_limit', 20) or 20))} / 표시 Top {int(float(cfg.get('top_n', 10) or 10))}",
                f"최소 최종 점수: {float(cfg.get('min_final_score', 55.0) or 55.0):.1f}",
                f"새로고침 간격: {int(float(cfg.get('refresh_interval_seconds', 300) or 300))}초",
                f"제외 섹터: {excluded}",
                f"블랙리스트: {blacklist}",
                "",
                "점수 구성: 유동성/비용 23 + UTBreakout 장세 27 + AUTO Set 17 + Adaptive TF 8 + 선물시장 8 + 선택품질 12 + 섹터 5",
                "선택품질: rolling Sharpe, 모멘텀 일관성, 방향 효율, 적정 변동성, drawdown/rebound, 시장 분산을 점수화합니다.",
                "주의: CoinSelector는 감시 후보만 고르고, 실제 진입은 전략 로직이 다시 확인합니다.",
            ])

        async def _send_coinscan_report_document(message, report=None):
            if message is None:
                return
            try:
                report = report or await _run_coinscan(force=False)
                text = "\n\n".join([
                    _format_coinscan_top_text(report),
                    _format_coinscan_reject_text(report),
                    _format_coinscan_criteria_text(),
                ])
                bio = io.BytesIO(text.encode('utf-8'))
                bio.name = 'coinselector_v2_report.txt'
                await message.reply_document(
                    document=bio,
                    filename='coinselector_v2_report.txt',
                    caption='CoinSelector V2 리포트입니다.'
                )
            except Exception as e:
                logger.error(f"CoinSelector report download failed: {e}")
                await message.reply_text(f"CoinSelector 리포트 다운로드 실패: {e}")

        async def _apply_coinscan_watchlist(message, report=None):
            mode = self.get_exchange_mode()
            if mode == UPBIT_MODE:
                await message.reply_text("❌ CoinSelector apply는 Binance Futures 모드에서만 사용할 수 있습니다.")
                return
            report = report or await _run_coinscan(force=False)
            selected = [
                item for item in report.get('selected', [])
                if item.get('selection_state') == 'SELECTED'
            ]
            raw_symbols = []
            for item in selected:
                symbol = item.get('normalized_symbol') or str(item.get('exchange_symbol') or '').replace(':USDT', '')
                if symbol and symbol not in raw_symbols:
                    raw_symbols.append(symbol)
            if not raw_symbols:
                await message.reply_text("❌ 적용할 CoinSelector 후보가 없습니다. `/coinscan top`으로 먼저 확인하세요.", parse_mode=ParseMode.MARKDOWN)
                return
            markets = await asyncio.to_thread(self.exchange.load_markets)
            symbols, removed = self._filter_futures_symbols_for_exchange_mode(
                raw_symbols,
                markets,
                exchange_mode=mode,
            )
            if not symbols:
                await message.reply_text("❌ 현재 거래소 모드에 적용 가능한 CoinSelector 후보가 없습니다.")
                return
            await self.cfg.update_value(['signal_engine', 'watchlist'], symbols)
            await self._update_config_value(['exchange_watchlists', mode], symbols)
            engine = self.engines.get('signal')
            if engine:
                engine.active_symbols = set(symbols)
                engine.scanner_active_symbol = None
            suffix = ""
            if removed:
                suffix = "\n제외: " + ", ".join(
                    f"{item.get('symbol')}({item.get('reason')})" for item in removed[:5]
                )
            await message.reply_text(f"✅ CoinSelector 후보를 watchlist에 적용했습니다: {', '.join(symbols)}{suffix}")

        def _format_coinscan_menu_text():
            cfg = _coinscan_cfg()
            engine = self.engines.get('signal')
            report = engine.coin_selector_last_result if engine else {}
            selected = list((report or {}).get('selected') or [])
            preview = ", ".join(
                f"{item.get('normalized_symbol')}({float(item.get('score', 0) or 0):.0f})"
                for item in selected[:5]
            ) or "아직 스캔 기록 없음"
            return "\n".join([
                "🧭 CoinSelector V2",
                "",
                f"상태: {'ON' if cfg.get('enabled', True) else 'OFF'}",
                f"기준: minVol {_format_volume_usdt(cfg.get('min_quote_volume_usdt'))}, spread <= {float(cfg.get('max_spread_pct', 0.08) or 0.08):.3f}%, Top {int(float(cfg.get('top_n', 10) or 10))}",
                f"최근 후보: {preview}",
                "",
                "역할: Binance USDT perpetual 중 감시할 코인을 고릅니다. 주문/진입은 기존 전략이 담당합니다.",
                "",
                "명령:",
                "`/coinscan top` - Top 후보 새로 조회",
                "`/coinscan apply` - Top 후보를 watchlist에 적용",
                "`/coinscan minvol 100` - 최소 24h 거래대금 100M 설정",
                "`/coinscan blacklist DOGE` / `unblacklist DOGE`",
                "`/coinscan criteria` - 현재 기준",
                "`/coinscan rejects` - 제외 사유",
                "`/coinscan download` - 리포트 다운로드",
            ])

        async def _edit_coinscan_menu(query, notice=None):
            text = _format_coinscan_menu_text()
            if notice:
                text = f"{notice}\n\n{text}"
            try:
                await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=_build_coinscan_keyboard())
            except BadRequest as md_err:
                if "message is not modified" in str(md_err).lower():
                    return
                plain = str(text).replace("`", "").replace("**", "")
                await query.edit_message_text(plain, reply_markup=_build_coinscan_keyboard())

        async def coinscan_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
            args = list(getattr(c, 'args', []) or [])
            if not args and u and u.message and u.message.text:
                parts = u.message.text.strip().split()
                args = parts[1:]
            action = str(args[0]).strip().lower() if args else ''

            if action in {'on', 'enable', 'start'}:
                await self.cfg.update_value(['signal_engine', 'coin_selector', 'enabled'], True)
                await u.message.reply_text("✅ CoinSelector V2 자동선택: ON")
                return
            if action in {'off', 'disable', 'stop'}:
                await self.cfg.update_value(['signal_engine', 'coin_selector', 'enabled'], False)
                await u.message.reply_text("✅ CoinSelector V2 자동선택: OFF. 기존 거래량 스캐너로 되돌아갑니다.")
                return
            if action in {'top', 'scan', 'refresh'}:
                report = await _run_coinscan(force=True)
                await u.message.reply_text(_format_coinscan_top_text(report), reply_markup=_build_coinscan_keyboard())
                return
            if action in {'apply', 'watchlist'}:
                report = await _run_coinscan(force=False)
                await _apply_coinscan_watchlist(u.message, report)
                return
            if action in {'criteria', 'rule', 'rules'}:
                await u.message.reply_text(_format_coinscan_criteria_text(), reply_markup=_build_coinscan_keyboard())
                return
            if action in {'rejects', 'exclude', 'excluded'}:
                report = await _run_coinscan(force=False)
                await u.message.reply_text(_format_coinscan_reject_text(report), reply_markup=_build_coinscan_keyboard())
                return
            if action in {'sectors', 'sector'}:
                cfg = _coinscan_cfg()
                await u.message.reply_text(
                    "제외 섹터: " + (", ".join(cfg.get('excluded_sectors') or []) or "없음"),
                    reply_markup=_build_coinscan_keyboard()
                )
                return
            if action in {'download', 'report', 'log'}:
                await _send_coinscan_report_document(u.message)
                return
            if action in {'minvol', 'volume', 'min_volume'}:
                if len(args) < 2:
                    await u.message.reply_text("❌ 예: `/coinscan minvol 100` (100M USDT)", parse_mode=ParseMode.MARKDOWN)
                    return
                try:
                    value_m = float(str(args[1]).replace('m', '').replace('M', '').replace(',', '').strip())
                except (TypeError, ValueError):
                    await u.message.reply_text("❌ 거래대금은 숫자로 입력하세요. 예: `/coinscan minvol 100`", parse_mode=ParseMode.MARKDOWN)
                    return
                value = max(100.0, value_m) * 1_000_000.0
                await self.cfg.update_value(['signal_engine', 'coin_selector', 'min_quote_volume_usdt'], value)
                await u.message.reply_text(
                    f"✅ CoinSelector 최소 거래대금: "
                    f"{_format_volume_usdt(value)} USDT "
                    "(메인넷 하한 100M)"
                )
                return
            if action in {'blacklist', 'ban'}:
                if len(args) < 2:
                    await u.message.reply_text("❌ 예: `/coinscan blacklist DOGE`", parse_mode=ParseMode.MARKDOWN)
                    return
                try:
                    symbol = self.normalize_symbol_for_exchange(args[1], BINANCE_MAINNET)
                except Exception:
                    symbol = str(args[1]).upper()
                cfg = _coinscan_cfg()
                blacklist = list(cfg.get('blacklist') or [])
                if symbol not in blacklist:
                    blacklist.append(symbol)
                await self.cfg.update_value(['signal_engine', 'coin_selector', 'blacklist'], blacklist)
                await u.message.reply_text(f"✅ 블랙리스트 추가: {symbol}")
                return
            if action in {'unblacklist', 'allow', 'unban'}:
                if len(args) < 2:
                    await u.message.reply_text("❌ 예: `/coinscan unblacklist DOGE`", parse_mode=ParseMode.MARKDOWN)
                    return
                try:
                    symbol = self.normalize_symbol_for_exchange(args[1], BINANCE_MAINNET)
                except Exception:
                    symbol = str(args[1]).upper()
                cfg = _coinscan_cfg()
                blacklist = [item for item in list(cfg.get('blacklist') or []) if item != symbol and item.split('/')[0] != symbol]
                await self.cfg.update_value(['signal_engine', 'coin_selector', 'blacklist'], blacklist)
                await u.message.reply_text(f"✅ 블랙리스트 해제: {symbol}")
                return

            await self._reply_markdown_safe(u.message, _format_coinscan_menu_text(), reply_markup=_build_coinscan_keyboard())

        async def coinscan_callback(u: Update, c: ContextTypes.DEFAULT_TYPE):
            query = u.callback_query
            if not query:
                return
            await query.answer()
            data = str(query.data or '')
            if not data.startswith('cs:'):
                return
            await _edit_integrated_utbreak_notice(query, "CoinSelector")
            return
            action = data.split(':', 1)[1]
            if action == 'on':
                await self.cfg.update_value(['signal_engine', 'coin_selector', 'enabled'], True)
                await _edit_coinscan_menu(query, "✅ CoinSelector V2 자동선택: ON")
                return
            if action == 'off':
                await self.cfg.update_value(['signal_engine', 'coin_selector', 'enabled'], False)
                await _edit_coinscan_menu(query, "✅ CoinSelector V2 자동선택: OFF")
                return
            if action == 'top':
                report = await _run_coinscan(force=True)
                await query.edit_message_text(_format_coinscan_top_text(report), reply_markup=_build_coinscan_keyboard())
                return
            if action == 'apply':
                report = await _run_coinscan(force=False)
                await _apply_coinscan_watchlist(query.message, report)
                return
            if action == 'criteria':
                await query.edit_message_text(_format_coinscan_criteria_text(), reply_markup=_build_coinscan_keyboard())
                return
            if action == 'rejects':
                report = await _run_coinscan(force=False)
                await query.edit_message_text(_format_coinscan_reject_text(report), reply_markup=_build_coinscan_keyboard())
                return
            if action == 'sectors':
                cfg = _coinscan_cfg()
                await query.edit_message_text(
                    "제외 섹터: " + (", ".join(cfg.get('excluded_sectors') or []) or "없음"),
                    reply_markup=_build_coinscan_keyboard()
                )
                return
            if action == 'download':
                await _send_coinscan_report_document(query.message)
                return
            await _edit_coinscan_menu(query)

        def _customcoins_cfg():
            cfg = _coinscan_cfg()
            cfg['custom_symbols'] = normalize_coin_selector_custom_symbols(cfg.get('custom_symbols'))
            fixed_symbols = normalize_coin_selector_custom_symbols([cfg.get('fixed_symbol')])
            cfg['fixed_symbol'] = fixed_symbols[0] if fixed_symbols else ''
            cfg['fixed_symbol_mode_enabled'] = bool(cfg.get('fixed_symbol_mode_enabled', False))
            return cfg

        def _build_customcoins_keyboard():
            return InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("AUTO 후보 ON", callback_data="cc:on"),
                    InlineKeyboardButton("AUTO 후보 OFF", callback_data="cc:off")
                ],
                [
                    InlineKeyboardButton("단일코인 설정", callback_data="cc:fixed"),
                    InlineKeyboardButton("단일코인 해제", callback_data="cc:fixed_off")
                ],
                [
                    InlineKeyboardButton("SCAN", callback_data="cc:scan"),
                    InlineKeyboardButton("CLEAR", callback_data="cc:clear")
                ],
                [
                    InlineKeyboardButton("새로고침", callback_data="cc:status")
                ]
            ])

        def _format_customcoins_status(report=None):
            cfg = _customcoins_cfg()
            symbols = normalize_coin_selector_custom_symbols(cfg.get('custom_symbols'))
            fixed_enabled = bool(cfg.get('fixed_symbol_mode_enabled'))
            fixed_symbol = cfg.get('fixed_symbol')
            selected = list((report or {}).get('selected') or [])
            reject_counts = (report or {}).get('reject_counts') or {}
            reject_samples = list((report or {}).get('reject_samples') or [])
            lines = [
                "🎯 CustomCoins AUTO",
                "",
                f"AUTO 후보 스캔: {'ON' if cfg.get('custom_universe_enabled') else 'OFF'}",
                f"단일코인 고정: {'ON' if fixed_enabled else 'OFF'}{(' / ' + fixed_symbol) if fixed_enabled and fixed_symbol else ''}",
                f"지정 코인: {', '.join(symbols) if symbols else '없음'}",
                "AUTO 후보 모드: 지정 코인만 후보 풀로 사용 + 60-set AUTO + Adaptive TF + scanner",
                "단일코인 고정 모드: scanner/CoinSelector OFF + watchlist 1개 + 현재 UT Breakout 설정 우선",
                "완화: AUTO 후보 모드에서만 거래대금/체결수 발견 기준을 완화하고, 진입/리스크 기준은 유지",
            ]
            if report:
                lines.extend([
                    "",
                    f"스캔: base {report.get('total_base_candidates', 0)}개 / scored {report.get('total_scored', 0)}개 / rejected {report.get('total_rejected', 0)}개",
                ])
                if selected:
                    lines.append("후보:")
                    for idx, item in enumerate(selected[:8], 1):
                        lines.append(
                            f"{idx}. {item.get('normalized_symbol') or item.get('symbol')} "
                            f"score {float(item.get('score', 0) or 0):.1f} / "
                            f"state `{item.get('selection_state')}` / "
                            f"Set{item.get('auto_set_id') or '?'} / TF {item.get('adaptive_tf')}"
                        )
                else:
                    lines.append("후보: 없음")
                if reject_counts:
                    lines.append(f"제외 사유: {', '.join(f'{k}:{v}' for k, v in reject_counts.items())}")
                if reject_samples:
                    lines.append("거절:")
                    for item in reject_samples[:5]:
                        reasons = ", ".join(item.get('reject_reasons') or [item.get('selection_state') or 'UNKNOWN'])
                        lines.append(
                            f"- {item.get('normalized_symbol') or item.get('symbol')}: `{reasons}`"
                        )
            lines.extend([
                "",
                "명령: `/utbreak coin EWY`, `/utbreak coin off`, `/utbreak autoscan on BTC ETH SOL`, `/utbreak auto on`, `/utbreak auto off`",
            ])
            return "\n".join(lines).strip()

        def _clear_coin_selector_runtime_cache():
            engine = self.engines.get('signal')
            if not engine:
                return
            engine.coin_selector_last_result = {}
            engine.coin_selector_symbol_scores = {}
            engine.coin_selector_last_run_ts = 0.0
            engine.micro_auto_last_scan = {}

        async def _enable_utbreak_auto_bundle():
            await _ensure_signal_engine_active()
            self.is_paused = False
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'active_strategy'], UTBOT_ADAPTIVE_TIMEFRAME_STRATEGY)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'selection_mode'], 'auto')
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'auto_select_enabled'], True)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'adaptive_timeframe_enabled'], True)
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'fixed_symbol_mode_enabled'], False)
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'fixed_symbol'], '')
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'custom_universe_enabled'], False)
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'enabled'], True)
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'selection_quality_enabled'], True)
            await self.cfg.update_value(['signal_engine', 'common_settings', 'scanner_enabled'], True)
            await self.cfg.update_value(['signal_engine', 'micro_auto', 'enabled'], False)
            engine = self._reset_signal_engine_runtime_state(
                reset_entry_cache=True,
                reset_exit_cache=True,
                reset_stateful_strategy=True
            )
            if engine:
                engine.scanner_active_symbol = None
                engine.active_symbols.clear()
            return "✅ FULL AUTO ON: CoinSelector V2 + scanner + Set AUTO + Adaptive TF를 한 번에 켰습니다."

        async def _disable_utbreak_auto_bundle():
            await _ensure_signal_engine_active()
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'active_strategy'], UTBOT_FILTERED_BREAKOUT_STRATEGY)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'selection_mode'], 'manual')
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'auto_select_enabled'], False)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'adaptive_timeframe_enabled'], False)
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'enabled'], False)
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'custom_universe_enabled'], False)
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'fixed_symbol_mode_enabled'], False)
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'fixed_symbol'], '')
            await self.cfg.update_value(['signal_engine', 'common_settings', 'scanner_enabled'], False)
            await self.cfg.update_value(['signal_engine', 'micro_auto', 'enabled'], False)
            engine = self._reset_signal_engine_runtime_state(
                reset_entry_cache=True,
                reset_exit_cache=True,
                reset_stateful_strategy=True
            )
            if engine:
                engine.scanner_active_symbol = None
                engine.active_symbols.clear()
                for item in self.get_active_watchlist():
                    engine.active_symbols.add(item)
            return "✅ FULL AUTO OFF: 코인 자동선택/scanner/Set AUTO/Adaptive TF를 끄고 watchlist 직접감시로 돌렸습니다."

        async def _enable_customcoins(symbols):
            normalized = normalize_coin_selector_custom_symbols(symbols)
            if not normalized:
                return False, "❌ 코인을 입력하세요. 예: `BTC ETH SOL`"
            if self.get_exchange_mode() == UPBIT_MODE:
                return False, "❌ CustomCoins는 Binance Futures 모드에서만 사용할 수 있습니다."
            markets = await asyncio.to_thread(self.exchange.load_markets)
            resolved_symbols = []
            removed_symbols = []
            seen = set()
            for raw_symbol in normalized:
                try:
                    resolved = self._resolve_futures_watch_symbol_from_markets(
                        raw_symbol,
                        markets,
                        exchange_mode=self.get_exchange_mode(),
                    )
                    key = self._futures_symbol_key(resolved)
                    if key and key not in seen:
                        resolved_symbols.append(resolved)
                        seen.add(key)
                except Exception as exc:
                    removed_symbols.append(f"{raw_symbol}({exc})")
            if not resolved_symbols:
                return False, "❌ 현재 거래소 모드에서 사용할 수 있는 CustomCoins 심볼이 없습니다."
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'fixed_symbol_mode_enabled'], False)
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'custom_symbols'], resolved_symbols)
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'custom_universe_enabled'], True)
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'custom_relax_discovery'], True)
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'enabled'], True)
            await self.cfg.update_value(['signal_engine', 'common_settings', 'scanner_enabled'], True)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'active_strategy'], UTBOT_ADAPTIVE_TIMEFRAME_STRATEGY)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'selection_mode'], 'auto')
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'auto_select_enabled'], True)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'adaptive_timeframe_enabled'], True)
            _clear_coin_selector_runtime_cache()
            self._reset_signal_engine_runtime_state(reset_stateful_strategy=True)
            notice = f"✅ CustomCoins AUTO ON: {', '.join(resolved_symbols)}"
            if removed_symbols:
                notice += "\n제외: " + ", ".join(removed_symbols[:5])
            return True, notice

        async def _enable_coin_auto_selection():
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'fixed_symbol_mode_enabled'], False)
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'fixed_symbol'], '')
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'custom_universe_enabled'], False)
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'enabled'], True)
            await self.cfg.update_value(['signal_engine', 'common_settings', 'scanner_enabled'], True)
            await self.cfg.update_value(['signal_engine', 'micro_auto', 'enabled'], False)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'active_strategy'], UTBOT_ADAPTIVE_TIMEFRAME_STRATEGY)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'selection_mode'], 'auto')
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'auto_select_enabled'], True)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'adaptive_timeframe_enabled'], True)
            _clear_coin_selector_runtime_cache()
            engine = self._reset_signal_engine_runtime_state(
                reset_entry_cache=True,
                reset_exit_cache=True,
                reset_stateful_strategy=True
            )
            if engine:
                engine.scanner_active_symbol = None
                engine.active_symbols.clear()
            return "✅ 코인 자동 선택 ON. Binance Futures 전체 후보에서 CoinSelector가 고르고 UTBreak AUTO/Adaptive로 진입합니다."

        async def _disable_coin_auto_selection():
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'enabled'], False)
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'custom_universe_enabled'], False)
            await self.cfg.update_value(['signal_engine', 'common_settings', 'scanner_enabled'], False)
            _clear_coin_selector_runtime_cache()
            engine = self._reset_signal_engine_runtime_state(
                reset_entry_cache=True,
                reset_exit_cache=True,
                reset_stateful_strategy=True
            )
            if engine:
                engine.scanner_active_symbol = None
                engine.active_symbols.clear()
                for item in self.get_active_watchlist():
                    engine.active_symbols.add(item)
            return "✅ 코인 자동 선택 OFF. scanner/CoinSelector를 끄고 watchlist 직접감시로 돌아갑니다."

        async def _disable_customcoins():
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'custom_universe_enabled'], False)
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'enabled'], False)
            await self.cfg.update_value(['signal_engine', 'common_settings', 'scanner_enabled'], False)
            _clear_coin_selector_runtime_cache()
            engine = self._reset_signal_engine_runtime_state(reset_stateful_strategy=True)
            if engine:
                engine.scanner_active_symbol = None
            return "✅ CustomCoins AUTO OFF. scanner/CoinSelector도 OFF로 전환했고 코인 목록은 보존했습니다."

        async def _enable_single_fixed_coin(symbols):
            normalized = normalize_coin_selector_custom_symbols(symbols)
            if len(normalized) != 1:
                return False, "❌ 단일코인 고정은 코인 1개만 입력하세요. 예: `/utbreak coin EWY`"
            try:
                symbol = (await _resolve_utbreak_direct_watchlist(normalized, max_symbols=1))[0]
            except Exception as exc:
                return False, f"❌ {exc}"

            current = _customcoins_cfg()
            if not current.get('fixed_symbol_mode_enabled'):
                current_watchlist = self.cfg.get('signal_engine', {}).get('watchlist', [])
                if isinstance(current_watchlist, list):
                    await self.cfg.update_value(['signal_engine', 'coin_selector', 'fixed_symbol_previous_watchlist'], list(current_watchlist))

            await self.cfg.update_value(['signal_engine', 'watchlist'], [symbol])
            await self._update_config_value(['exchange_watchlists', self.get_exchange_mode()], [symbol])
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'custom_symbols'], [symbol])
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'fixed_symbol'], symbol)
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'fixed_symbol_mode_enabled'], True)
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'custom_universe_enabled'], False)
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'enabled'], False)
            await self.cfg.update_value(['signal_engine', 'common_settings', 'scanner_enabled'], False)

            strategy_params = self.cfg.get('signal_engine', {}).get('strategy_params', {}) or {}
            active_strategy = str(strategy_params.get('active_strategy', '') or '').lower()
            if active_strategy not in UTBREAKOUT_STRATEGIES:
                await self.cfg.update_value(['signal_engine', 'strategy_params', 'active_strategy'], UTBOT_FILTERED_BREAKOUT_STRATEGY)

            _clear_coin_selector_runtime_cache()
            engine = self._reset_signal_engine_runtime_state(
                reset_entry_cache=True,
                reset_exit_cache=True,
                reset_stateful_strategy=True
            )
            if engine:
                engine.scanner_active_symbol = None
                engine.active_symbols.clear()
                engine.active_symbols.add(symbol)
                await engine.prime_symbol_to_next_closed_candle(symbol)
            return True, (
                f"✅ 단일코인 고정 ON: `{symbol}`\n"
                "scanner/CoinSelector를 끄고 현재 UT Breakout 설정으로 이 코인만 직접 감시합니다."
            )

        async def _disable_single_fixed_coin():
            cfg = _customcoins_cfg()
            previous_watchlist = self.cfg.get('signal_engine', {}).get('coin_selector', {}).get('fixed_symbol_previous_watchlist')
            if isinstance(previous_watchlist, list) and previous_watchlist:
                await self.cfg.update_value(['signal_engine', 'watchlist'], list(previous_watchlist))
                await self._update_config_value(['exchange_watchlists', self.get_exchange_mode()], list(previous_watchlist))
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'fixed_symbol_mode_enabled'], False)
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'fixed_symbol'], '')
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'custom_universe_enabled'], False)
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'enabled'], False)
            await self.cfg.update_value(['signal_engine', 'common_settings', 'scanner_enabled'], False)
            _clear_coin_selector_runtime_cache()
            engine = self._reset_signal_engine_runtime_state(
                reset_entry_cache=True,
                reset_exit_cache=True,
                reset_stateful_strategy=True
            )
            if engine:
                engine.scanner_active_symbol = None
                engine.active_symbols.clear()
                for item in self.get_active_watchlist():
                    engine.active_symbols.add(item)
            restored = ", ".join(self.get_active_watchlist())
            suffix = f" 감시 목록: {restored}" if restored else ""
            if not cfg.get('fixed_symbol_mode_enabled'):
                return "ℹ️ 단일코인 고정은 이미 OFF입니다. UT Breakout 설정 우선으로 유지합니다." + suffix
            return "✅ 단일코인 고정 OFF. scanner/CoinSelector OFF, UT Breakout 설정 우선으로 전환했습니다." + suffix

        async def _clear_customcoins():
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'fixed_symbol_mode_enabled'], False)
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'fixed_symbol'], '')
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'custom_universe_enabled'], False)
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'enabled'], False)
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'custom_symbols'], [])
            await self.cfg.update_value(['signal_engine', 'common_settings', 'scanner_enabled'], False)
            _clear_coin_selector_runtime_cache()
            engine = self._reset_signal_engine_runtime_state(reset_stateful_strategy=True)
            if engine:
                engine.scanner_active_symbol = None
            return "✅ CustomCoins 목록을 비우고 scanner/CoinSelector도 OFF로 전환했습니다."

        async def _run_customcoins_scan(force=True):
            engine = self.engines.get('signal')
            if not engine:
                return {
                    'selected': [],
                    'reject_counts': {'NO_SIGNAL_ENGINE': 1},
                    'criteria': _customcoins_cfg(),
                    'note': 'Signal 엔진을 찾을 수 없습니다.',
                }
            return await engine.evaluate_coin_selector(force=force, custom_universe_override=True)

        async def _prompt_customcoins_input(message_or_query, context):
            if context and context.user_data is not None:
                context.user_data['customcoins_waiting_for_symbols'] = True
            text = "AUTO 후보로 쓸 코인을 입력하세요. 예: `BTC ETH SOL`"
            if hasattr(message_or_query, 'edit_message_text'):
                await message_or_query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)
            else:
                await message_or_query.reply_text(text, parse_mode=ParseMode.MARKDOWN)

        async def _prompt_fixed_coin_input(message_or_query, context):
            if context and context.user_data is not None:
                context.user_data['fixedcoin_waiting_for_symbol'] = True
            text = "단일 고정 감시할 코인 1개를 입력하세요. 예: `EWY`"
            if hasattr(message_or_query, 'edit_message_text'):
                await message_or_query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)
            else:
                await message_or_query.reply_text(text, parse_mode=ParseMode.MARKDOWN)

        async def _handle_customcoins_symbol_text(message, context, text):
            symbols = normalize_coin_selector_custom_symbols(text)
            if not symbols:
                if context and context.user_data is not None:
                    context.user_data['customcoins_waiting_for_symbols'] = True
                await message.reply_text("❌ 코인을 인식하지 못했습니다. 예: `BTC ETH SOL`", parse_mode=ParseMode.MARKDOWN)
                return
            _, notice = await _enable_customcoins(symbols)
            await message.reply_text(
                f"{notice}\n\n{_format_customcoins_status()}",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=_build_customcoins_keyboard()
            )

        async def _handle_fixed_coin_symbol_text(message, context, text):
            ok, notice = await _enable_single_fixed_coin([text])
            if not ok and context and context.user_data is not None:
                context.user_data['fixedcoin_waiting_for_symbol'] = True
            await message.reply_text(
                f"{notice}\n\n{_format_customcoins_status()}",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=_build_customcoins_keyboard()
            )

        async def customcoins_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
            args = list(getattr(c, 'args', []) or [])
            if not args and u and u.message and u.message.text:
                parts = u.message.text.strip().split()
                args = parts[1:]
            action = str(args[0]).strip().lower() if args else ''

            if action in {'on', 'enable', 'start'}:
                if len(args) > 1:
                    _, notice = await _enable_customcoins(args[1:])
                    await u.message.reply_text(
                        f"{notice}\n\n{_format_customcoins_status()}",
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=_build_customcoins_keyboard()
                    )
                    return
                await _prompt_customcoins_input(u.message, c)
                return
            if action in {'off', 'disable', 'stop'}:
                notice = await _disable_customcoins()
                await u.message.reply_text(
                    f"{notice}\n\n{_format_customcoins_status()}",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=_build_customcoins_keyboard()
                )
                return
            if action in {'fixed', 'fix', 'single', 'pin'}:
                if len(args) > 1 and str(args[1]).strip().lower() in {'off', 'disable', 'stop', '0'}:
                    notice = await _disable_single_fixed_coin()
                    await u.message.reply_text(
                        f"{notice}\n\n{_format_customcoins_status()}",
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=_build_customcoins_keyboard()
                    )
                    return
                if len(args) > 1:
                    ok, notice = await _enable_single_fixed_coin(args[1:])
                    await u.message.reply_text(
                        f"{notice}\n\n{_format_customcoins_status()}",
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=_build_customcoins_keyboard()
                    )
                    return
                await _prompt_fixed_coin_input(u.message, c)
                return
            if action in {'clear', 'reset'}:
                notice = await _clear_customcoins()
                await u.message.reply_text(
                    f"{notice}\n\n{_format_customcoins_status()}",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=_build_customcoins_keyboard()
                )
                return
            if action in {'scan', 'top', 'refresh'}:
                report = await _run_customcoins_scan(force=True)
                await u.message.reply_text(
                    _format_customcoins_status(report),
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=_build_customcoins_keyboard()
                )
                return
            await self._reply_markdown_safe(u.message, _format_customcoins_status(), reply_markup=_build_customcoins_keyboard())

        async def customcoins_callback(u: Update, c: ContextTypes.DEFAULT_TYPE):
            query = u.callback_query
            if not query:
                return
            await query.answer()
            data = str(query.data or '')
            if not data.startswith('cc:'):
                return
            await _edit_integrated_utbreak_notice(query, "CustomCoins AUTO")
            return
            action = data.split(':', 1)[1]
            if action == 'on':
                await _prompt_customcoins_input(query, c)
                return
            if action == 'off':
                notice = await _disable_customcoins()
                await query.edit_message_text(
                    f"{notice}\n\n{_format_customcoins_status()}",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=_build_customcoins_keyboard()
                )
                return
            if action == 'fixed':
                await _prompt_fixed_coin_input(query, c)
                return
            if action == 'fixed_off':
                notice = await _disable_single_fixed_coin()
                await query.edit_message_text(
                    f"{notice}\n\n{_format_customcoins_status()}",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=_build_customcoins_keyboard()
                )
                return
            if action == 'clear':
                notice = await _clear_customcoins()
                await query.edit_message_text(
                    f"{notice}\n\n{_format_customcoins_status()}",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=_build_customcoins_keyboard()
                )
                return
            if action == 'scan':
                report = await _run_customcoins_scan(force=True)
                await query.edit_message_text(
                    _format_customcoins_status(report),
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=_build_customcoins_keyboard()
                )
                return
            await query.edit_message_text(
                _format_customcoins_status(),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=_build_customcoins_keyboard()
            )

        def _micro_cfg():
            raw = self.cfg.get('signal_engine', {}).get('micro_auto', {})
            return normalize_micro_auto_config(raw if isinstance(raw, dict) else {})

        def _build_micro_keyboard():
            return InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("ON", callback_data="ma:on"),
                    InlineKeyboardButton("OFF", callback_data="ma:off")
                ],
                [
                    InlineKeyboardButton("DRY-RUN ON", callback_data="ma:dry_on"),
                    InlineKeyboardButton("LIVE LOCK 해제", callback_data="ma:live_on")
                ],
                [
                    InlineKeyboardButton("현재 후보", callback_data="ma:scan"),
                    InlineKeyboardButton("자동계획", callback_data="ma:status")
                ],
                [
                    InlineKeyboardButton("리스크", callback_data="ma:risk"),
                    InlineKeyboardButton("리포트 다운로드", callback_data="ma:download")
                ],
                [
                    InlineKeyboardButton("새로고침", callback_data="ma:menu")
                ]
            ])

        async def _enable_microauto(dry_run=True):
            await self.cfg.update_value(['signal_engine', 'micro_auto', 'enabled'], True)
            await self.cfg.update_value(['signal_engine', 'micro_auto', 'dry_run'], bool(dry_run))
            await self.cfg.update_value(['signal_engine', 'micro_auto', 'live_enabled'], not bool(dry_run))
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'enabled'], True)
            await self.cfg.update_value(['signal_engine', 'common_settings', 'scanner_enabled'], True)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'active_strategy'], UTBOT_ADAPTIVE_TIMEFRAME_STRATEGY)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'selection_mode'], 'auto')
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'auto_select_enabled'], True)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'adaptive_timeframe_enabled'], True)
            self._reset_signal_engine_runtime_state(reset_stateful_strategy=True)

        async def _run_micro_scan(force=False):
            engine = self.engines.get('signal')
            if not engine:
                return {
                    'criteria': _micro_cfg(),
                    'selected': [],
                    'rejected': [],
                    'note': 'Signal 엔진을 찾을 수 없습니다.',
                }
            return await engine.evaluate_micro_auto_candidates(force=force)

        def _format_micro_risk_text():
            cfg = _micro_cfg()
            return "\n".join([
                "🧪 Micro Auto V1 리스크 기준",
                f"계좌 한도: min(total equity, {float(cfg.get('equity_cap_usdt', 10.0) or 10.0):.2f} USDT)",
                f"최소 거래 equity: {float(cfg.get('min_equity_to_trade_usdt', 6.0) or 6.0):.2f} USDT",
                f"레버리지 자동범위: {int(cfg.get('min_leverage', 3) or 3)}x~{int(cfg.get('max_leverage', 10) or 10)}x",
                f"증거금 상한: micro equity의 {float(cfg.get('max_margin_usage_pct', 45.0) or 45.0):.1f}%",
                f"1회 손실: {float(cfg.get('risk_per_trade_pct', DEFAULT_RISK_PER_TRADE_PERCENT) or DEFAULT_RISK_PER_TRADE_PERCENT):.2f}% 또는 최대 {float(cfg.get('max_risk_usdt', 0.15) or 0.15):.2f} USDT",
                f"일일 손실/거래 제한: -{float(cfg.get('daily_loss_limit_usdt', 0.45) or 0.45):.2f} USDT / {int(cfg.get('max_daily_trades', 3) or 3)}회 / 연속손절 {int(cfg.get('max_consecutive_losses', 2) or 2)}회",
                f"익절: 최소 {float(cfg.get('min_take_profit_r', 2.2) or 2.2):.1f}R, 수수료 부담 크면 {float(cfg.get('high_fee_take_profit_r', 2.5) or 2.5):.1f}R",
                "원칙: 최소주문 때문에 수량을 키웠을 때 손절 손실이 예산을 넘으면 진입하지 않습니다.",
            ])

        def _format_micro_status_text(report=None):
            cfg = _micro_cfg()
            engine = self.engines.get('signal')
            report = report or (engine.micro_auto_last_scan if engine else {}) or {}
            last_plan = {}
            last_reject = {}
            if engine:
                if engine.micro_auto_last_plan:
                    last_plan = next(reversed(engine.micro_auto_last_plan.values()))
                if engine.micro_auto_last_rejects:
                    last_reject = next(reversed(engine.micro_auto_last_rejects.values()))
            selected = list(report.get('selected') or [])
            rejected = list(report.get('rejected') or [])
            lines = [
                "🧪 MICRO_AUTO_V1",
                "",
                f"상태: {'ON' if cfg.get('enabled') else 'OFF'} | DRY-RUN {'ON' if cfg.get('dry_run') else 'OFF'} | LIVE {'ON' if cfg.get('live_enabled') else 'LOCKED'}",
                f"전략: `{UTBOT_ADAPTIVE_TIMEFRAME_STRATEGY}` + CoinSelector V2 + 50-set AUTO + Adaptive TF",
                f"계좌: total {float(report.get('total_equity_usdt', 0) or 0):.2f} / free {float(report.get('free_usdt', 0) or 0):.2f} / micro {float(report.get('equity_for_micro', 0) or 0):.2f} USDT",
                "",
            ]
            if last_plan:
                lines.extend([
                    "최근 자동계획:",
                    (
                        f"{last_plan.get('side', '').upper()} {last_plan.get('selected_timeframe') or last_plan.get('entry_timeframe')} / "
                        f"Set{last_plan.get('selected_set_id') or '?'} {last_plan.get('selected_set_name') or ''}"
                    ),
                    (
                        f"레버리지 {int(float(last_plan.get('leverage', 0) or 0))}x / "
                        f"증거금 {float(last_plan.get('planned_margin', 0) or 0):.2f} / "
                        f"포지션 {float(last_plan.get('planned_notional', 0) or 0):.2f} USDT / "
                        f"수량 {float(last_plan.get('qty', 0) or 0):.8f}"
                    ),
                    (
                        f"SL {float(last_plan.get('stop_loss', 0) or 0):.6f} / "
                        f"TP {float(last_plan.get('take_profit', 0) or 0):.6f} / "
                        f"손실 {float(last_plan.get('risk_usdt', 0) or 0):.4f} / "
                        f"RR {float(last_plan.get('rr_multiple', 0) or 0):.1f}R"
                    ),
                    (
                        f"minNotional {float(last_plan.get('min_notional_usdt', 0) or 0):.2f} / "
                        f"수수료부담 {float(last_plan.get('fee_burden_pct', 0) or 0):.1f}%"
                    ),
                    "",
                ])
            elif last_reject:
                lines.extend([
                    "최근 거절:",
                    f"{last_reject.get('symbol', '')} {last_reject.get('reject_code', '')}: {last_reject.get('reason', '')}",
                    "",
                ])
            else:
                lines.append("최근 자동계획: 아직 없음. `/utbreak micro on` 후 후보를 확인하세요.\n")

            lines.append("현재 후보:")
            if not selected:
                lines.append("가능 후보 없음 또는 아직 스캔 전입니다.")
            for idx, item in enumerate(selected[:8], 1):
                decision = item.get('micro_auto') or {}
                lines.append(
                    f"{idx}. {item.get('normalized_symbol') or item.get('symbol')} "
                    f"score {float(item.get('score', 0) or 0):.1f} / "
                    f"minNotional {float(item.get('micro_min_notional', 0) or 0):.2f} / "
                    f"{int(decision.get('leverage', 0) or 0)}x / margin {float(decision.get('required_margin', 0) or 0):.2f}"
                )
            if rejected:
                lines.append("")
                lines.append(f"제외 후보: {len(rejected)}개 (예: {rejected[0].get('normalized_symbol') or rejected[0].get('symbol')} {rejected[0].get('micro_reject_code')})")
            lines.extend([
                "",
                "명령: `/utbreak micro on`, `/utbreak micro off`, `/utbreak micro live`",
            ])
            return "\n".join(lines).strip()

        async def _send_micro_report_document(message, report=None):
            if message is None:
                return
            try:
                report = report or await _run_micro_scan(force=False)
                text = "\n\n".join([
                    _format_micro_status_text(report),
                    _format_micro_risk_text(),
                ])
                bio = io.BytesIO(text.encode('utf-8'))
                bio.name = 'micro_auto_v1_report.txt'
                await message.reply_document(
                    document=bio,
                    filename='micro_auto_v1_report.txt',
                    caption='Micro Auto V1 리포트입니다.'
                )
            except Exception as e:
                logger.error(f"Micro Auto report download failed: {e}")
                await message.reply_text(f"Micro Auto 리포트 다운로드 실패: {e}")

        async def _edit_micro_menu(query, notice=None, report=None):
            text = _format_micro_status_text(report)
            if notice:
                text = f"{notice}\n\n{text}"
            try:
                await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=_build_micro_keyboard())
            except BadRequest as md_err:
                if "message is not modified" in str(md_err).lower():
                    return
                plain = str(text).replace("`", "").replace("**", "")
                await query.edit_message_text(plain, reply_markup=_build_micro_keyboard())

        async def microauto_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
            args = list(getattr(c, 'args', []) or [])
            if not args and u and u.message and u.message.text:
                args = u.message.text.strip().split()[1:]
            action = str(args[0]).strip().lower() if args else ''
            value = str(args[1]).strip().lower() if len(args) >= 2 else ''

            if action in {'on', 'enable', 'start'}:
                await _enable_microauto(dry_run=True)
                await u.message.reply_text("✅ Micro Auto V1: ON / DRY-RUN ON. 실주문은 `/utbreak micro live` 전까지 차단됩니다.", parse_mode=ParseMode.MARKDOWN)
                return
            if action in {'off', 'disable', 'stop'}:
                await self.cfg.update_value(['signal_engine', 'micro_auto', 'enabled'], False)
                self._reset_signal_engine_runtime_state(reset_stateful_strategy=True)
                await u.message.reply_text("✅ Micro Auto V1: OFF")
                return
            if action in {'dryrun', 'dry-run'}:
                on = value not in {'off', 'false', '0', 'disable'}
                await self.cfg.update_value(['signal_engine', 'micro_auto', 'dry_run'], on)
                if on:
                    await self.cfg.update_value(['signal_engine', 'micro_auto', 'live_enabled'], False)
                await u.message.reply_text(f"✅ Micro Auto DRY-RUN: {'ON' if on else 'OFF'}")
                return
            if action == 'live' and value == 'on':
                await _enable_microauto(dry_run=False)
                await u.message.reply_text("✅ Micro Auto LIVE: ON. 소액 모드 조건을 통과한 주문만 실행됩니다.")
                return
            if action in {'scan', 'candidates', 'top'}:
                report = await _run_micro_scan(force=True)
                await u.message.reply_text(_format_micro_status_text(report), parse_mode=ParseMode.MARKDOWN, reply_markup=_build_micro_keyboard())
                return
            if action in {'risk', 'rules'}:
                await u.message.reply_text(_format_micro_risk_text(), reply_markup=_build_micro_keyboard())
                return
            if action in {'download', 'report', 'log'}:
                await _send_micro_report_document(u.message)
                return
            await self._reply_markdown_safe(u.message, _format_micro_status_text(), reply_markup=_build_micro_keyboard())

        async def microauto_callback(u: Update, c: ContextTypes.DEFAULT_TYPE):
            query = u.callback_query
            if not query:
                return
            await query.answer()
            data = str(query.data or '')
            if not data.startswith('ma:'):
                return
            await _edit_integrated_utbreak_notice(query, "Micro Auto")
            return
            action = data.split(':', 1)[1]
            if action == 'on':
                await _enable_microauto(dry_run=True)
                await _edit_micro_menu(query, "✅ Micro Auto V1: ON / DRY-RUN ON")
                return
            if action == 'off':
                await self.cfg.update_value(['signal_engine', 'micro_auto', 'enabled'], False)
                self._reset_signal_engine_runtime_state(reset_stateful_strategy=True)
                await _edit_micro_menu(query, "✅ Micro Auto V1: OFF")
                return
            if action == 'dry_on':
                await self.cfg.update_value(['signal_engine', 'micro_auto', 'dry_run'], True)
                await self.cfg.update_value(['signal_engine', 'micro_auto', 'live_enabled'], False)
                await _edit_micro_menu(query, "✅ DRY-RUN ON")
                return
            if action == 'live_on':
                await _enable_microauto(dry_run=False)
                await _edit_micro_menu(query, "✅ LIVE ON")
                return
            if action == 'scan':
                report = await _run_micro_scan(force=True)
                await _edit_micro_menu(query, report=report)
                return
            if action == 'risk':
                await query.edit_message_text(_format_micro_risk_text(), reply_markup=_build_micro_keyboard())
                return
            if action == 'download':
                await _send_micro_report_document(query.message)
                return
            await _edit_micro_menu(query)

        def _prediction_cfg():
            raw = self.cfg.get('prediction_micro_auto', {})
            if not isinstance(raw, dict):
                raw = {}
            return normalize_prediction_micro_config(raw)

        def _prediction_ledger():
            ledger = getattr(self, 'prediction_paper_ledger', None)
            if ledger is None:
                ledger = PaperLedger.load(PREDICTION_PAPER_LEDGER_FILE)
                self.prediction_paper_ledger = ledger
            return ledger

        def _save_prediction_ledger():
            try:
                _prediction_ledger().save(PREDICTION_PAPER_LEDGER_FILE)
            except Exception as e:
                logger.warning(f"Prediction paper ledger save failed: {e}")

        def _build_prediction_keyboard():
            return InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("ON", callback_data="pr:on"),
                    InlineKeyboardButton("OFF", callback_data="pr:off"),
                    InlineKeyboardButton("Paper Only", callback_data="pr:paper"),
                ],
                [
                    InlineKeyboardButton("LIVE UNLOCK", callback_data="pr:live_on"),
                    InlineKeyboardButton("LIVE LOCK", callback_data="pr:live_off"),
                    InlineKeyboardButton("LIVE STATUS", callback_data="pr:live_status"),
                ],
                [
                    InlineKeyboardButton("시장 스캔", callback_data="pr:scan"),
                    InlineKeyboardButton("전략 점수", callback_data="pr:strategies"),
                ],
                [
                    InlineKeyboardButton("페이퍼 포지션", callback_data="pr:positions"),
                    InlineKeyboardButton("7일 리포트", callback_data="pr:research"),
                ],
                [
                    InlineKeyboardButton("Futures 연구 Set", callback_data="pr:futures"),
                    InlineKeyboardButton("리포트 다운로드", callback_data="pr:download"),
                ],
            ])

        def _prediction_extract_markets(payload):
            if isinstance(payload, list):
                return payload
            if not isinstance(payload, dict):
                return []
            data = payload.get('data')
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                for key in ('markets', 'items', 'results', 'data'):
                    if isinstance(data.get(key), list):
                        return data.get(key)
            for key in ('markets', 'items', 'results'):
                if isinstance(payload.get(key), list):
                    return payload.get(key)
            return []

        def _prediction_open_allocated_usdt(ledger):
            return sum(float(p.get('stake_usdt') or 0.0) for p in ledger.open_positions())

        def _prediction_daily_trade_count(ledger):
            return ledger.opened_count_since(hours=24)

        def _prediction_market_price(orderbook):
            for key in ('buy_yes_avg_price', 'best_yes_ask', 'yes_mid', 'best_yes_bid'):
                value = _safe_float_or_none((orderbook or {}).get(key))
                if value is not None and 0.0 < value < 1.0:
                    return value
            return 0.5

        def _prediction_fair_probability(market, orderbook):
            title = str((market or {}).get('title') or '').lower()
            market_type = str((market or {}).get('market_type') or '')
            if market_type == 'crypto' and ('up or down' in title or 'up/down' in title):
                return estimate_crypto_up_probability(
                    open_price=100.0,
                    current_price=100.4,
                    minutes_remaining=15,
                    realized_vol_pct=0.8,
                    drift_pct=0.05,
                )
            if market_type == 'crypto':
                return 0.56
            if market_type == 'macro':
                return 0.55
            return 0.50

        def _prediction_live_credentials():
            return PredictionLiveCredentials.from_env()

        def _prediction_live_ready_text():
            creds = _prediction_live_credentials()
            missing = creds.missing_reasons()
            if missing:
                return "LIVE blocked: " + ", ".join(missing)
            return "LIVE ready: env unlock + API key + JWT + private key + approvals confirmed"

        def _prediction_value(payload, *keys):
            if not isinstance(payload, dict):
                return None
            scopes = [payload]
            if isinstance(payload.get('data'), dict):
                scopes.append(payload.get('data'))
            for scope in scopes:
                for key in keys:
                    if key in scope and scope.get(key) is not None:
                        return scope.get(key)
            return None

        async def _sync_prediction_live_orders():
            cfg = _prediction_cfg()
            ledger = _prediction_ledger()
            creds = _prediction_live_credentials()
            missing = [reason for reason in creds.missing_reasons() if reason not in {'PREDICTION_APPROVALS_NOT_CONFIRMED'}]
            if missing:
                return {'synced': [], 'errors': [{'error': ','.join(missing)}]}
            client = PredictClient.mainnet(api_key=creds.api_key, jwt_token=creds.jwt_token, timeout=10)
            synced = []
            errors = []
            changed = False
            for pos in list(ledger.open_positions()):
                if pos.get('execution_mode') != 'live':
                    continue
                order_ref = pos.get('order_hash') or pos.get('order_id')
                if not order_ref:
                    errors.append({'position_id': pos.get('id'), 'error': 'PREDICTION_LIVE_ORDER_REF_MISSING'})
                    continue
                try:
                    payload = await asyncio.to_thread(client.get_order, order_ref)
                    status = _prediction_value(payload, 'status', 'state', 'orderStatus') or 'UNKNOWN'
                    filled = _prediction_value(payload, 'filledAmount', 'filled_amount', 'filled', 'matchedAmount')
                    remaining = _prediction_value(payload, 'remainingAmount', 'remaining_amount', 'remaining')
                    pos['live_order_status'] = str(status)
                    pos['live_order_last_sync'] = datetime.now(timezone.utc).isoformat()
                    if filled is not None:
                        pos['live_filled_amount'] = str(filled)
                    if remaining is not None:
                        pos['live_remaining_amount'] = str(remaining)
                    synced.append({
                        'position_id': pos.get('id'),
                        'market_id': pos.get('market_id'),
                        'order_ref': order_ref,
                        'status': str(status),
                        'filled': filled,
                        'remaining': remaining,
                    })
                    changed = True
                    log_prediction_diagnostic_event({
                        'event': 'live_order_sync',
                        'position_id': pos.get('id'),
                        'market_id': pos.get('market_id'),
                        'order_ref': order_ref,
                        'status': str(status),
                    })
                except Exception as e:
                    errors.append({'position_id': pos.get('id'), 'order_ref': order_ref, 'error': str(e)})
                    log_prediction_diagnostic_event({
                        'event': 'live_order_sync_error',
                        'position_id': pos.get('id'),
                        'market_id': pos.get('market_id'),
                        'order_ref': order_ref,
                        'error': str(e),
                    })
            if changed:
                _save_prediction_ledger()
            return {'synced': synced, 'errors': errors, 'provider': provider_label(cfg.get('provider'))}

        async def _format_prediction_live_status_text(sync=True):
            cfg = _prediction_cfg()
            ledger = _prediction_ledger()
            creds = _prediction_live_credentials()
            sync_result = await _sync_prediction_live_orders() if sync else {'synced': [], 'errors': []}
            missing = creds.missing_reasons()
            lines = [
                "Prediction LIVE Status",
                f"Provider: {provider_label(cfg.get('provider'))}",
                "Binance 앱 Prediction은 공식 문서상 Predict.fun 제3자 프로토콜 통합으로 취급합니다.",
                f"Bot: {'ON' if cfg.get('enabled') else 'OFF'} | LIVE {'UNLOCKED' if cfg.get('live_enabled') else 'LOCKED'} | Mainnet {'ON' if cfg.get('use_mainnet') else 'OFF'}",
                f"10USDT cap: {float(cfg.get('equity_cap_usdt', 10.0) or 10.0):.2f} / stake max {float(cfg.get('max_stake_usdt', 1.0) or 1.0):.2f}",
                f"Env/approval: {'READY' if not missing else 'BLOCKED'}",
            ]
            if missing:
                lines.append("Missing: " + ", ".join(missing))
            lines.extend([
                "",
                "Funding/withdraw check:",
                "Binance 앱은 Assets > Prediction > Transfer에서 USDT를 옮기는 구조입니다.",
                "Predict.fun 직접 사용은 스마트월렛 입금/출금 구조라, live 전 1~2 USDT 입금-주문-회수 테스트가 필요합니다.",
            ])
            live_positions = [p for p in ledger.open_positions() if p.get('execution_mode') == 'live']
            lines.append("")
            lines.append("Live positions:")
            if not live_positions:
                lines.append("none")
            for pos in live_positions:
                lines.append(
                    f"{pos.get('id')} | {pos.get('market_title')} | stake {float(pos.get('stake_usdt', 0) or 0):.2f} | "
                    f"order {pos.get('order_id') or pos.get('order_hash') or 'n/a'} | status {pos.get('live_order_status') or 'UNKNOWN'}"
                )
            if sync_result.get('errors'):
                lines.append("")
                lines.append("Sync errors:")
                for item in sync_result.get('errors')[:5]:
                    lines.append(f"{item.get('position_id', 'n/a')}: {item.get('error')}")
            return "\n".join(lines)

        def _prediction_event_summary(days=7):
            events = read_prediction_diagnostic_events(days=days)
            reject_counts = Counter()
            strategy_counts = Counter()
            for event in events:
                if event.get('event') == 'scan':
                    for reason, count in (event.get('reject_counts') or {}).items():
                        reject_counts[str(reason)] += int(count or 0)
                    for strategy_id, count in (event.get('strategy_counts') or {}).items():
                        strategy_counts[str(strategy_id)] += int(count or 0)
                elif event.get('event') in {'paper_entry', 'paper_close', 'paper_settle'}:
                    for strategy_id in event.get('strategy_ids') or []:
                        strategy_counts[str(strategy_id)] += 1
            return {
                'events': events,
                'reject_counts': dict(reject_counts),
                'strategy_counts': dict(strategy_counts),
            }

        def _log_prediction_scan_result(result):
            reject_counts = prediction_reject_counts(result)
            strategy_counts = Counter()
            for item in list((result or {}).get('candidates') or []):
                for strategy_id in item.get('strategy_ids') or []:
                    strategy_counts[str(strategy_id)] += 1
            log_prediction_diagnostic_event({
                'event': 'scan',
                'source': (result or {}).get('source'),
                'candidate_count': len((result or {}).get('candidates') or []),
                'reject_count': len((result or {}).get('rejects') or []),
                'reject_counts': reject_counts,
                'strategy_counts': dict(strategy_counts),
                'top_market_id': ((result or {}).get('candidates') or [{}])[0].get('market_id') if (result or {}).get('candidates') else None,
                'futures_research_sets': list(range(51, 61)),
            })

        async def _reconcile_prediction_paper_positions(client, raw_market_by_id, cfg, ledger):
            events = []
            for position in list(ledger.open_positions()):
                if position.get('execution_mode') == 'live':
                    continue
                market_id = str(position.get('market_id') or '')
                raw_market = raw_market_by_id.get(market_id, {})
                orderbook = {}
                try:
                    orderbook_payload = await asyncio.to_thread(client.get_orderbook, position.get('market_id'))
                    orderbook = analyze_orderbook(orderbook_payload, spend_usdt=float(position.get('stake_usdt') or 1.0))
                except Exception as e:
                    orderbook = {'accepted': False, 'reject_code': f"PREDICTION_ORDERBOOK_ERROR: {e}"}
                market_price = _safe_float_or_none(orderbook.get('yes_mid'))
                if market_price is None:
                    market_price = _safe_float_or_none(orderbook.get('best_yes_bid')) or float(position.get('entry_price') or 0.5)
                edge_result = evaluate_prediction_edge(
                    fair_probability=position.get('fair_probability', 0.5),
                    market_price=market_price,
                    fee_rate_bps=200,
                    spread_decimal=orderbook.get('yes_spread') or 0.0,
                    safety_margin=cfg.get('min_edge_probability', 0.03),
                )
                decision = evaluate_paper_position_exit(
                    position,
                    raw_market=raw_market,
                    orderbook=orderbook,
                    edge_result=edge_result,
                    cfg=cfg,
                )
                if decision.get('action') == 'settle':
                    updated = ledger.settle_position(
                        position.get('id'),
                        outcome_won=bool(decision.get('outcome_won')),
                        closing_price=decision.get('closing_price'),
                    )
                    event = {
                        'event': 'paper_settle',
                        'position_id': updated.get('id'),
                        'market_id': updated.get('market_id'),
                        'market_title': updated.get('market_title'),
                        'reason': decision.get('reason'),
                        'pnl_usdt': updated.get('pnl_usdt'),
                        'strategy_ids': position.get('strategy_ids') or [],
                    }
                    events.append(event)
                    log_prediction_diagnostic_event(event)
                elif decision.get('action') == 'close':
                    updated = ledger.close_position(
                        position.get('id'),
                        exit_price=float(decision.get('exit_price') or position.get('entry_price') or 0.0),
                    )
                    event = {
                        'event': 'paper_close',
                        'position_id': updated.get('id'),
                        'market_id': updated.get('market_id'),
                        'market_title': updated.get('market_title'),
                        'reason': decision.get('reason'),
                        'exit_price': updated.get('exit_price'),
                        'pnl_usdt': updated.get('pnl_usdt'),
                        'strategy_ids': position.get('strategy_ids') or [],
                    }
                    events.append(event)
                    log_prediction_diagnostic_event(event)
            if events:
                _save_prediction_ledger()
            return events

        async def _run_prediction_scan(force=False, auto=False):
            cfg = _prediction_cfg()
            ledger = _prediction_ledger()
            result = {
                'candidates': [],
                'rejects': [],
                'note': '',
                'paper_entry': None,
                'live_order': None,
                'live_error': None,
                'live_preflight': None,
                'live_sync': None,
                'provider': provider_label(cfg.get('provider')),
                'source': 'mainnet' if cfg.get('use_mainnet') else 'testnet',
            }
            try:
                if cfg.get('live_enabled') or cfg.get('use_mainnet'):
                    creds = _prediction_live_credentials()
                    client = PredictClient.mainnet(api_key=creds.api_key, jwt_token=creds.jwt_token, timeout=12)
                    result['source'] = 'mainnet'
                else:
                    client = PredictClient.testnet(timeout=12)
                payload = await asyncio.to_thread(client.get_markets, first=int(cfg.get('scan_limit', 30) or 30))
                raw_markets = _prediction_extract_markets(payload)
            except PredictAuthRequired as e:
                result['note'] = str(e)
                self.prediction_micro_last_scan = result
                return result
            except Exception as e:
                result['note'] = f"PREDICTION_SCAN_ERROR: {e}"
                self.prediction_micro_last_scan = result
                logger.warning(f"Prediction scan failed: {e}")
                return result

            raw_market_by_id = {str(raw.get('id') or raw.get('marketId') or raw.get('market_id')): raw for raw in raw_markets}
            result['paper_exits'] = await _reconcile_prediction_paper_positions(client, raw_market_by_id, cfg, ledger)
            if any(p.get('execution_mode') == 'live' for p in ledger.open_positions()):
                result['live_sync'] = await _sync_prediction_live_orders()
            open_ids = {str(p.get('market_id')) for p in ledger.open_positions()}
            summary = ledger.summary()
            for raw in raw_markets:
                market = normalize_market(raw)
                if not market.get('accepted'):
                    result['rejects'].append({
                        'market_id': market.get('id'),
                        'title': market.get('title'),
                        'reject_reasons': market.get('reject_reasons'),
                    })
                    continue

                orderbook = {}
                orderbook_payload = {}
                try:
                    orderbook_payload = await asyncio.to_thread(client.get_orderbook, market.get('id'))
                    orderbook = analyze_orderbook(orderbook_payload, spend_usdt=float(cfg.get('min_stake_usdt', 1.0) or 1.0))
                except Exception as e:
                    orderbook = {'accepted': False, 'reject_code': f"PREDICTION_ORDERBOOK_ERROR: {e}"}

                market_price = _prediction_market_price(orderbook)
                fair_probability = _prediction_fair_probability(market, orderbook)
                edge_result = evaluate_prediction_edge(
                    fair_probability=fair_probability,
                    market_price=market_price,
                    fee_rate_bps=market.get('fee_rate_bps'),
                    spread_decimal=orderbook.get('yes_spread') or 0.0,
                    safety_margin=cfg.get('min_edge_probability', 0.03),
                )
                plan = build_prediction_micro_plan(
                    market=market,
                    side='YES',
                    market_price=market_price,
                    edge=edge_result.get('edge'),
                    total_allocated_usdt=_prediction_open_allocated_usdt(ledger),
                    daily_realized_pnl_usdt=summary.get('realized_pnl_usdt', 0.0),
                    daily_trade_count=_prediction_daily_trade_count(ledger),
                    open_position_count=len(ledger.open_positions()),
                    requested_stake_usdt=cfg.get('max_stake_usdt', 1.0),
                    cfg=cfg,
                )
                score = score_prediction_candidate(market, orderbook, edge_result, plan)
                item = {
                    'market_id': market.get('id'),
                    'title': market.get('title'),
                    'market_type': market.get('market_type'),
                    'score': score.get('score'),
                    'accepted': bool(score.get('accepted')),
                    'strategy_ids': score.get('selected_strategy_ids') or [],
                    'component_scores': score.get('component_scores') or {},
                    'fair_probability': edge_result.get('fair_probability'),
                    'market_price': edge_result.get('market_price'),
                    'edge': edge_result.get('edge'),
                    'stake_usdt': plan.get('stake_usdt'),
                    'reject_code': plan.get('reject_code') or edge_result.get('reject_code') or orderbook.get('reject_code'),
                    'micro_plan': plan,
                    '_market': market,
                    '_orderbook_payload': orderbook_payload,
                }
                if item['accepted'] and str(item['market_id']) not in open_ids:
                    result['candidates'].append(item)
                else:
                    result['rejects'].append(item)

            result['candidates'].sort(key=lambda item: float(item.get('score') or 0.0), reverse=True)
            if cfg.get('enabled') and cfg.get('live_enabled') and result['candidates'] and not ledger.open_positions():
                top = result['candidates'][0]
                if float(top.get('score') or 0.0) < float(cfg.get('live_min_score', 60.0) or 60.0):
                    result['live_error'] = 'PREDICTION_LIVE_SCORE_LOW'
                else:
                    try:
                        creds = _prediction_live_credentials()
                        live_preflight = await asyncio.to_thread(
                            check_live_preflight,
                            market=top.get('_market') or {},
                            orderbook_payload=top.get('_orderbook_payload') or {},
                            plan=top.get('micro_plan') or {},
                            cfg=cfg,
                            credentials=creds,
                            open_positions=ledger.open_positions(),
                        )
                        result['live_preflight'] = live_preflight
                        if not live_preflight.get('accepted'):
                            result['live_error'] = ",".join(live_preflight.get('reject_reasons') or ['PREDICTION_LIVE_PREFLIGHT_REJECTED'])
                            log_prediction_diagnostic_event({
                                'event': 'live_order_preflight_reject',
                                'market_id': top.get('market_id'),
                                'market_title': top.get('title'),
                                'reject_reasons': live_preflight.get('reject_reasons') or [],
                                'stake_usdt': live_preflight.get('stake_usdt'),
                                'balance_usdt': live_preflight.get('balance_usdt'),
                            })
                            raise PredictionLiveOrderError(result['live_error'])
                        live_result = await asyncio.to_thread(
                            submit_live_market_order,
                            client=PredictClient.mainnet(api_key=creds.api_key, jwt_token=creds.jwt_token, timeout=15),
                            market=top.get('_market') or {},
                            orderbook_payload=top.get('_orderbook_payload') or {},
                            plan=top.get('micro_plan') or {},
                            cfg=cfg,
                            credentials=creds,
                            open_positions=ledger.open_positions(),
                        )
                        result['live_order'] = {
                            'market_id': top.get('market_id'),
                            'market_title': top.get('title'),
                            'stake_usdt': top.get('stake_usdt'),
                            'score': top.get('score'),
                            'order_id': live_result.get('order_id'),
                            'order_hash': live_result.get('order_hash'),
                            'balance_usdt': (live_result.get('preflight') or {}).get('balance_usdt'),
                        }
                        position = ledger.open_position(
                            top.get('micro_plan') or {},
                            fair_probability=top.get('fair_probability'),
                        )
                        position['execution_mode'] = 'live'
                        position['status'] = 'OPEN'
                        position['strategy_ids'] = top.get('strategy_ids') or []
                        position['score'] = top.get('score')
                        position['order_id'] = live_result.get('order_id')
                        position['order_hash'] = live_result.get('order_hash')
                        position['live_order_status'] = 'SUBMITTED'
                        position['provider'] = provider_label(cfg.get('provider'))
                        _save_prediction_ledger()
                        log_prediction_diagnostic_event({
                            'event': 'live_order',
                            'position_id': position.get('id'),
                            'market_id': position.get('market_id'),
                            'market_title': position.get('market_title'),
                            'stake_usdt': position.get('stake_usdt'),
                            'entry_price': position.get('entry_price'),
                            'order_id': live_result.get('order_id'),
                            'order_hash': live_result.get('order_hash'),
                            'strategy_ids': position.get('strategy_ids') or [],
                            'score': position.get('score'),
                        })
                    except (PredictionLiveOrderError, PredictAuthRequired, Exception) as e:
                        result['live_error'] = str(e)
                        log_prediction_diagnostic_event({
                            'event': 'live_order_error',
                            'market_id': top.get('market_id'),
                            'market_title': top.get('title'),
                            'error': str(e),
                        })
            elif cfg.get('enabled') and cfg.get('auto_paper_entry') and result['candidates'] and not ledger.open_positions():
                top = result['candidates'][0]
                position = ledger.open_position(
                    top.get('micro_plan') or {},
                    fair_probability=top.get('fair_probability'),
                )
                position['strategy_ids'] = top.get('strategy_ids') or []
                position['score'] = top.get('score')
                result['paper_entry'] = position
                _save_prediction_ledger()
                log_prediction_diagnostic_event({
                    'event': 'paper_entry',
                    'position_id': position.get('id'),
                    'market_id': position.get('market_id'),
                    'market_title': position.get('market_title'),
                    'market_type': position.get('market_type'),
                    'stake_usdt': position.get('stake_usdt'),
                    'entry_price': position.get('entry_price'),
                    'fair_probability': position.get('fair_probability'),
                    'strategy_ids': position.get('strategy_ids') or [],
                    'score': position.get('score'),
                })
                logger.info(
                    "Prediction Micro Auto paper entry: %s stake=%.2f price=%.4f",
                    position.get('market_title'),
                    float(position.get('stake_usdt') or 0.0),
                    float(position.get('entry_price') or 0.0),
                )
            for item in result.get('candidates') or []:
                item.pop('_market', None)
                item.pop('_orderbook_payload', None)
            self.prediction_micro_last_scan = result
            _log_prediction_scan_result(result)
            return result

        def _format_prediction_menu_text(report=None):
            cfg = _prediction_cfg()
            ledger = _prediction_ledger()
            report = report or getattr(self, 'prediction_micro_last_scan', None) or {}
            text = format_prediction_report(report, cfg=cfg, ledger_summary=ledger.summary())
            entry = report.get('paper_entry') if isinstance(report, dict) else None
            if entry:
                text += (
                    "\n\nPaper entry opened:\n"
                    f"{entry.get('market_title')} / stake {float(entry.get('stake_usdt', 0) or 0):.2f} USDT / "
                    f"price {float(entry.get('entry_price', 0) or 0):.4f}"
                )
            exits = report.get('paper_exits') if isinstance(report, dict) else []
            if exits:
                text += "\n\nPaper lifecycle:"
                for event in exits[:3]:
                    text += (
                        f"\n{event.get('event')} {event.get('market_title')} "
                        f"{event.get('reason')} PnL {float(event.get('pnl_usdt', 0) or 0):.2f}"
                    )
            live_order = report.get('live_order') if isinstance(report, dict) else None
            if live_order:
                text += (
                    "\n\nLIVE order submitted:\n"
                    f"{live_order.get('market_title')} / stake {float(live_order.get('stake_usdt', 0) or 0):.2f} USDT / "
                    f"order {live_order.get('order_id') or live_order.get('order_hash')}"
                )
            live_preflight = report.get('live_preflight') if isinstance(report, dict) else None
            if live_preflight:
                state = 'PASS' if live_preflight.get('accepted') else 'BLOCKED'
                text += (
                    f"\n\nLIVE preflight: {state} / stake {float(live_preflight.get('stake_usdt', 0) or 0):.2f} / "
                    f"balance {live_preflight.get('balance_usdt') if live_preflight.get('balance_usdt') is not None else 'n/a'}"
                )
            if isinstance(report, dict) and report.get('live_error'):
                text += f"\n\nLIVE error: {report.get('live_error')}"
            if isinstance(report, dict) and report.get('note'):
                text += f"\n\nNote: {report.get('note')}"
            text += f"\n\n{_prediction_live_ready_text()}"
            text += "\n\nCommands: /prediction on, /prediction off, /prediction scan, /prediction positions, /prediction live status, /prediction strategies, /prediction research"
            return text

        def _format_prediction_positions_text():
            ledger = _prediction_ledger()
            lines = ["Prediction Paper Positions"]
            open_positions = ledger.open_positions()
            if not open_positions:
                lines.append("Open: none")
            for pos in open_positions:
                lines.append(
                    f"OPEN {pos.get('id')} | {pos.get('market_title')} | "
                    f"{str(pos.get('execution_mode') or 'paper').upper()} | "
                    f"stake {float(pos.get('stake_usdt', 0) or 0):.2f} | "
                    f"entry {float(pos.get('entry_price', 0) or 0):.4f} | "
                    f"fair {float(pos.get('fair_probability', 0) or 0):.3f}"
                )
            recent_closed = [
                p for p in list(getattr(ledger, 'positions', []) or [])
                if p.get('status') in {'CLOSED', 'SETTLED'}
            ][-5:]
            if recent_closed:
                lines.append("")
                lines.append("Recent closed/settled:")
                for pos in recent_closed:
                    lines.append(
                        f"{pos.get('status')} {pos.get('id')} | {pos.get('market_title')} | "
                        f"PnL {float(pos.get('pnl_usdt', 0) or 0):.2f} | "
                        f"CLV {pos.get('closing_line_value') if pos.get('closing_line_value') is not None else 'n/a'}"
                    )
            summary = ledger.summary()
            lines.append(
                f"Summary: total {summary.get('total_positions', 0)} / open {summary.get('open_positions', 0)} / "
                f"closed {summary.get('closed_positions', 0)} / PnL {float(summary.get('realized_pnl_usdt', 0) or 0):.2f} USDT"
            )
            return "\n".join(lines)

        def _format_prediction_strategies_text(page=1):
            try:
                page = int(page or 1)
            except (TypeError, ValueError):
                page = 1
            page = min(10, max(1, page))
            start = (page - 1) * 10
            rows = PREDICTION_STRATEGY_CATALOG[start:start + 10]
            lines = [
                f"Prediction Strategy Catalog ({page}/10)",
                "All 100 items are scoring/research rules. Live orders are gated separately by preflight, cap, balance, and approval checks.",
                "",
            ]
            for item in rows:
                lines.append(
                    f"{item.get('id')}. {item.get('name')} [{item.get('family')}] "
                    f"{item.get('status')}"
                )
            lines.append("Other pages: /prediction strategies 1~10")
            return "\n".join(lines)

        def _format_prediction_futures_sets_text():
            lines = [
                "Futures Prediction Research Sets",
                "Set51~60 are now active UT Breakout futures sets. They require Binance Futures public data and, for odds-based sets, recent Prediction scan data.",
                "",
            ]
            for set_id in range(51, 61):
                info = get_utbreakout_set_definition(set_id)
                lines.append(f"Set{set_id} {info.get('name')} - {info.get('description')}")
            return "\n".join(lines)

        def _format_prediction_research_text(days=7):
            ledger = _prediction_ledger()
            event_summary = _prediction_event_summary(days=days)
            return format_prediction_research_report(
                ledger.summary(days=days),
                reject_counts=event_summary.get('reject_counts'),
                strategy_counts=event_summary.get('strategy_counts'),
                days=days,
            )

        async def _send_prediction_report_document(message, report=None):
            if message is None:
                return
            try:
                report = report or getattr(self, 'prediction_micro_last_scan', None) or await _run_prediction_scan(force=False)
                text = "\n\n".join([
                    _format_prediction_menu_text(report),
                    _format_prediction_positions_text(),
                    _format_prediction_research_text(),
                    _format_prediction_strategies_text(1),
                    _format_prediction_futures_sets_text(),
                ])
                bio = io.BytesIO(text.encode('utf-8'))
                bio.name = 'prediction_micro_auto_report.txt'
                await message.reply_document(
                    document=bio,
                    filename='prediction_micro_auto_report.txt',
                    caption='Prediction Micro Auto paper report'
                )
            except Exception as e:
                logger.error(f"Prediction report download failed: {e}")
                await message.reply_text(f"Prediction report download failed: {e}")

        async def _edit_prediction_menu(query, notice=None, report=None):
            text = _format_prediction_menu_text(report)
            if notice:
                text = f"{notice}\n\n{text}"
            try:
                await query.edit_message_text(text, reply_markup=_build_prediction_keyboard())
            except BadRequest as md_err:
                if "message is not modified" in str(md_err).lower():
                    return
                await query.edit_message_text(str(text).replace("`", ""), reply_markup=_build_prediction_keyboard())

        async def prediction_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
            args = list(getattr(c, 'args', []) or [])
            if not args and u and u.message and u.message.text:
                args = u.message.text.strip().split()[1:]
            action = str(args[0]).strip().lower() if args else ''
            if action in {'on', 'enable', 'start'}:
                await self.cfg.update_value(['prediction_micro_auto', 'enabled'], True)
                await u.message.reply_text("Prediction Micro Auto: ON. 현재 모드는 설정에 따라 PAPER/LIVE로 동작합니다.")
                return
            if action in {'off', 'disable', 'stop'}:
                await self.cfg.update_value(['prediction_micro_auto', 'enabled'], False)
                await u.message.reply_text("Prediction Micro Auto: OFF")
                return
            if action == 'live' and len(args) > 1 and str(args[1]).lower() == 'unlock':
                await self.cfg.update_value(['prediction_micro_auto', 'enabled'], True)
                await self.cfg.update_value(['prediction_micro_auto', 'live_enabled'], True)
                await self.cfg.update_value(['prediction_micro_auto', 'use_mainnet'], True)
                await u.message.reply_text("Prediction LIVE: UNLOCKED. 단, 환경변수 키, 승인 확인, PREDICTION_LIVE_TRADING_ENABLED=1 없으면 주문은 차단됩니다.")
                return
            if action == 'live' and len(args) > 1 and str(args[1]).lower() in {'lock', 'off'}:
                await self.cfg.update_value(['prediction_micro_auto', 'live_enabled'], False)
                await self.cfg.update_value(['prediction_micro_auto', 'use_mainnet'], False)
                await u.message.reply_text("Prediction LIVE: LOCKED. Paper mode로 돌아갑니다.")
                return
            if action == 'live' and len(args) > 1 and str(args[1]).lower() == 'status':
                await u.message.reply_text(await _format_prediction_live_status_text(), reply_markup=_build_prediction_keyboard())
                return
            if action in {'status', 'menu', ''}:
                await u.message.reply_text(_format_prediction_menu_text(), reply_markup=_build_prediction_keyboard())
                return
            if action in {'scan', 'markets', 'top'}:
                report = await _run_prediction_scan(force=True)
                await u.message.reply_text(_format_prediction_menu_text(report), reply_markup=_build_prediction_keyboard())
                return
            if action in {'positions', 'paper', 'ledger'}:
                await u.message.reply_text(_format_prediction_positions_text(), reply_markup=_build_prediction_keyboard())
                return
            if action in {'strategies', 'strategy', 'score'}:
                page = args[1] if len(args) > 1 else 1
                await u.message.reply_text(_format_prediction_strategies_text(page), reply_markup=_build_prediction_keyboard())
                return
            if action in {'futures', 'sets'}:
                await u.message.reply_text(_format_prediction_futures_sets_text(), reply_markup=_build_prediction_keyboard())
                return
            if action in {'research', 'summary', '7d'}:
                await u.message.reply_text(_format_prediction_research_text(), reply_markup=_build_prediction_keyboard())
                return
            if action in {'download', 'report', 'log'}:
                await _send_prediction_report_document(u.message)
                return
            await u.message.reply_text(_format_prediction_menu_text(), reply_markup=_build_prediction_keyboard())

        async def prediction_callback(u: Update, c: ContextTypes.DEFAULT_TYPE):
            query = u.callback_query
            if not query:
                return
            await query.answer()
            data = str(query.data or '')
            if not data.startswith('pr:'):
                return
            action = data.split(':', 1)[1]
            if action == 'on':
                await self.cfg.update_value(['prediction_micro_auto', 'enabled'], True)
                await _edit_prediction_menu(query, "Prediction Micro Auto: ON")
                return
            if action == 'off':
                await self.cfg.update_value(['prediction_micro_auto', 'enabled'], False)
                await _edit_prediction_menu(query, "Prediction Micro Auto: OFF")
                return
            if action == 'paper':
                await self.cfg.update_value(['prediction_micro_auto', 'live_enabled'], False)
                await self.cfg.update_value(['prediction_micro_auto', 'use_mainnet'], False)
                await self.cfg.update_value(['prediction_micro_auto', 'paper_only'], True)
                await _edit_prediction_menu(query, "Paper Only: ON / LIVE LOCKED")
                return
            if action == 'live_on':
                await self.cfg.update_value(['prediction_micro_auto', 'enabled'], True)
                await self.cfg.update_value(['prediction_micro_auto', 'live_enabled'], True)
                await self.cfg.update_value(['prediction_micro_auto', 'use_mainnet'], True)
                await _edit_prediction_menu(query, "Prediction LIVE: UNLOCKED. 환경변수 키/승인 확인이 없으면 주문은 차단됩니다.")
                return
            if action == 'live_off':
                await self.cfg.update_value(['prediction_micro_auto', 'live_enabled'], False)
                await self.cfg.update_value(['prediction_micro_auto', 'use_mainnet'], False)
                await _edit_prediction_menu(query, "Prediction LIVE: LOCKED")
                return
            if action == 'live_status':
                await query.edit_message_text(await _format_prediction_live_status_text(), reply_markup=_build_prediction_keyboard())
                return
            if action == 'scan':
                report = await _run_prediction_scan(force=True)
                await _edit_prediction_menu(query, report=report)
                return
            if action == 'positions':
                await query.edit_message_text(_format_prediction_positions_text(), reply_markup=_build_prediction_keyboard())
                return
            if action == 'strategies':
                await query.edit_message_text(_format_prediction_strategies_text(1), reply_markup=_build_prediction_keyboard())
                return
            if action == 'futures':
                await query.edit_message_text(_format_prediction_futures_sets_text(), reply_markup=_build_prediction_keyboard())
                return
            if action == 'research':
                await query.edit_message_text(_format_prediction_research_text(), reply_markup=_build_prediction_keyboard())
                return
            if action == 'download':
                await _send_prediction_report_document(query.message)
                return
            await _edit_prediction_menu(query)

        async def prediction_auto_scan_job(context):
            try:
                cfg = _prediction_cfg()
                if not cfg.get('enabled'):
                    return
                await _run_prediction_scan(force=True, auto=True)
            except Exception as e:
                logger.warning(f"Prediction auto scan job failed: {e}")

        async def help_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
            msg = """
📚 **명령어**

/start - 메인 메뉴 표시
/status - 현재 상태 조회
/history - 지난 상태 조회
/stats - 통계
/utbreak - UTBreak 전략 메뉴
/utbreakout - /utbreak alias
/setup - 거래소/네트워크 전환
/customentry - 사용자 지정 종목·방향 진입 모드
/autotrade - 자동매매 거래횟수·스캔범위 설정
/prediction - Prediction Micro Auto / Binance Wallet Prediction(Predict.fun) 메뉴
/log - 최근 로그
/close - 긴급 청산
/stop - 긴급 정지 및 포지션 청산

🚨 STOP - 긴급 정지
⏸ PAUSE - 일시정지
▶ RESUME - 재개
"""
            await u.message.reply_text(msg.strip(), parse_mode=ParseMode.MARKDOWN)

        self.tg_app.add_handler(CommandHandler("stop", owner_only(stop_cmd)), group=-1)
        emergency_handler = MessageHandler(filters.Regex(emergency_pattern), owner_only(self.global_handler))
        self.tg_app.add_handler(emergency_handler, group=-1)

        self.tg_app.add_handler(CommandHandler("start", owner_only(start_cmd)))
        self.tg_app.add_handler(CommandHandler("strat", owner_only(strat_cmd)))
        self.tg_app.add_handler(CommandHandler("status", owner_only(status_cmd)))
        self.tg_app.add_handler(CommandHandler("history", owner_only(history_cmd)))
        self.tg_app.add_handler(CommandHandler("log", owner_only(log_cmd)))
        self.tg_app.add_handler(CommandHandler("close", owner_only(close_cmd)))
        self.tg_app.add_handler(CommandHandler("stats", owner_only(stats_cmd)))
        self.tg_app.add_handler(CommandHandler("risk", owner_only(risk_cmd)))
        self.tg_app.add_handler(CommandHandler("utbreak", owner_only(utbreakout_cmd)))
        self.tg_app.add_handler(CommandHandler("utbreakout", owner_only(utbreakout_cmd)))
        self.tg_app.add_handler(CallbackQueryHandler(owner_only(legacy_utbot_callback), pattern=r"^utbot:"))
        self.tg_app.add_handler(CallbackQueryHandler(owner_only(utbreakout_callback), pattern=r"^utb:"))
        self.tg_app.add_handler(CallbackQueryHandler(owner_only(risk_callback), pattern=r"^risk:"))
        self.tg_app.add_handler(CallbackQueryHandler(owner_only(coinscan_callback), pattern=r"^cs:"))
        self.tg_app.add_handler(CallbackQueryHandler(owner_only(customcoins_callback), pattern=r"^cc:"))
        self.tg_app.add_handler(CallbackQueryHandler(owner_only(microauto_callback), pattern=r"^ma:"))
        self.tg_app.add_handler(CommandHandler("prediction", owner_only(prediction_cmd)))
        self.tg_app.add_handler(CallbackQueryHandler(owner_only(prediction_callback), pattern=r"^pr:"))
        self.tg_app.add_handler(CommandHandler("help", owner_only(help_cmd)))
        self._register_automatic_trading_control_handlers(owner_only)
        self._register_user_custom_entry_handlers(owner_only, text_filter)

        setup_command_handler = CommandHandler('setup', owner_only(self.setup_entry))
        setup_text_handler = MessageHandler(filters.Regex(setup_trigger_pattern), owner_only(self.setup_entry))
        setup_network_direct_pattern = r"^(거래소/네트워크 전환|거래소 네트워크 전환|거래소 전환|네트워크 전환|22)$"
        setup_network_direct_handler = MessageHandler(
            filters.Regex(setup_network_direct_pattern),
            owner_only(self.setup_network_entry)
        )

        conv = ConversationHandler(
            entry_points=[
                setup_command_handler,
                setup_text_handler,
                setup_network_direct_handler
            ],
            allow_reentry=True,
            states={
                SELECT: [MessageHandler(setup_text_filter, owner_only(self.setup_select))],
                INPUT: [MessageHandler(setup_text_filter, owner_only(self.setup_input))],
                SYMBOL_INPUT: [MessageHandler(setup_text_filter, owner_only(self.setup_symbol_input))],
                DIRECTION_SELECT: [MessageHandler(setup_text_filter, owner_only(self.setup_direction_select))],
                ENGINE_SELECT: [MessageHandler(setup_text_filter, owner_only(self.setup_engine_select))]
            },
            fallbacks=[
                setup_command_handler,
                setup_text_handler,
                setup_network_direct_handler,
                emergency_handler
            ]
        )

        self.tg_app.add_handler(conv)

        async def menu_button_handler(u: Update, c: ContextTypes.DEFAULT_TYPE):
            command = self._extract_telegram_command(u.message.text if u and u.message else "")
            if command == "/status":
                return await status_cmd(u, c)
            if command == "/history":
                return await history_cmd(u, c)
            if command == "/log":
                return await log_cmd(u, c)
            if command == "/help":
                return await help_cmd(u, c)
            if command == "/stats":
                return await stats_cmd(u, c)
            if command == "/risk":
                return await risk_cmd(u, c)
            if command == "/close":
                return await close_cmd(u, c)
            if command in {"/utbreak", "/utbreakout"}:
                return await utbreakout_cmd(u, c)
            if command in TELEGRAM_UTBREAK_INTEGRATED_COMMANDS:
                await self._reply_markdown_safe(
                    u.message,
                    "ℹ️ 해당 기능은 이제 `/utbreak` 메뉴 안으로 통합되었습니다.\n\n" + _format_utbreakout_menu_text(),
                    reply_markup=_build_utbreakout_keyboard()
                )
                return
            if command == "/prediction":
                return await prediction_cmd(u, c)
            return None

        self.tg_app.add_handler(
            MessageHandler(
                filters.Regex(menu_trigger_pattern),
                owner_only(menu_button_handler)
            )
        )

        try:
            job_queue = getattr(self.tg_app, 'job_queue', None)
            if job_queue:
                for job in job_queue.get_jobs_by_name('prediction_micro_auto_scan'):
                    job.schedule_removal()
                interval = int(_prediction_cfg().get('scan_interval_seconds', 300) or 300)
                job_queue.run_repeating(
                    prediction_auto_scan_job,
                    interval=max(60, interval),
                    first=60,
                    name='prediction_micro_auto_scan'
                )
        except Exception as e:
            logger.warning(f"Prediction auto scan scheduler setup failed: {e}")

        async def manual_symbol_handler(u: Update, c: ContextTypes.DEFAULT_TYPE):
            raw_text = u.message.text.strip()
            if c and c.user_data is not None and c.user_data.pop('utbot_coin_waiting_for_symbol', False):
                try:
                    symbol = await _set_strategy_coin(raw_text)
                    await self._reply_markdown_safe(
                        u.message,
                        f"✅ UTBot 단일코인 설정: `{symbol}`\n\n{_format_utbot_menu_text()}",
                        reply_markup=_build_utbot_keyboard()
                    )
                except Exception as e:
                    c.user_data['utbot_coin_waiting_for_symbol'] = True
                    await u.message.reply_text(f"❌ {e}\n다시 입력하세요. 예: `EWY`", parse_mode=ParseMode.MARKDOWN)
                return
            if c and c.user_data is not None and c.user_data.pop('utbot_params_waiting', False):
                try:
                    notice = await _set_utbot_params(raw_text)
                    await self._reply_markdown_safe(
                        u.message,
                        f"✅ {notice}\n\n{_format_utbot_menu_text()}",
                        reply_markup=_build_utbot_keyboard()
                    )
                except Exception as e:
                    c.user_data['utbot_params_waiting'] = True
                    await u.message.reply_text(f"❌ {e}\n다시 입력하세요. 형식: `key,atr,on/off`", parse_mode=ParseMode.MARKDOWN)
                return
            if c and c.user_data is not None and c.user_data.pop('utbreak_coin_waiting_for_symbol', False):
                ok, notice = await _enable_single_fixed_coin([raw_text])
                if not ok:
                    c.user_data['utbreak_coin_waiting_for_symbol'] = True
                    await u.message.reply_text(f"{notice}\n다시 입력하세요. 예: `EWY`", parse_mode=ParseMode.MARKDOWN)
                    return
                await self._reply_markdown_safe(
                    u.message,
                    f"{notice}\n\n{_format_utbreakout_menu_text()}",
                    reply_markup=_build_utbreakout_keyboard()
                )
                return
            if c and c.user_data is not None and c.user_data.pop('utbreak_watchlist_waiting_for_symbols', False):
                try:
                    notice = await _enable_utbreak_direct_watchlist(raw_text.split())
                except ValueError as e:
                    c.user_data['utbreak_watchlist_waiting_for_symbols'] = True
                    await u.message.reply_text(f"❌ {e}\n다시 입력하세요. 예: `BTC ETH AAPL EWY`", parse_mode=ParseMode.MARKDOWN)
                    return
                await self._reply_markdown_safe(
                    u.message,
                    f"{notice}\n\n{_format_utbreakout_menu_text()}",
                    reply_markup=_build_utbreakout_keyboard()
                )
                return
            if c and c.user_data is not None and c.user_data.pop('utbreak_autoscan_waiting_for_symbols', False):
                _, notice = await _enable_customcoins(raw_text.split())
                await self._reply_markdown_safe(
                    u.message,
                    f"{notice}\n\n{_format_utbreakout_menu_text()}",
                    reply_markup=_build_utbreakout_keyboard()
                )
                return
            if c and c.user_data is not None and c.user_data.pop('customcoins_waiting_for_symbols', False):
                await _handle_customcoins_symbol_text(u.message, c, raw_text)
                return
            if c and c.user_data is not None and c.user_data.pop('fixedcoin_waiting_for_symbol', False):
                await _handle_fixed_coin_symbol_text(u.message, c, raw_text)
                return
            text = raw_text.upper()
            if re.match(r'^[A-Z0-9]{2,15}([/-][A-Z0-9]{2,15})?(:[A-Z0-9]+)?$', text):
                if text.startswith('/'):
                    return
                await self.handle_manual_symbol_input(u, text)

        self.tg_app.add_handler(MessageHandler(text_filter, owner_only(manual_symbol_handler)))
