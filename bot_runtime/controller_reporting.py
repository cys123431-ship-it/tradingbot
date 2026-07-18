"""Periodic reports, polling loops, dashboard, and resume processing."""

from __future__ import annotations


class ControllerReportingMixin:
    async def setup_r2_select(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text
        if "1" in text:
            curr = self.cfg.get('signal_engine', {}).get('common_settings', {}).get('r2_entry_enabled', True)
            await self.cfg.update_value(['signal_engine', 'common_settings', 'r2_entry_enabled'], not curr)
            await update.message.reply_text(f"✅ R2 Entry: {'ON' if not curr else 'OFF'}")
        elif "2" in text:
            curr = self.cfg.get('signal_engine', {}).get('common_settings', {}).get('r2_exit_enabled', True)
            await self.cfg.update_value(['signal_engine', 'common_settings', 'r2_exit_enabled'], not curr)
            await update.message.reply_text(f"✅ R2 Exit: {'ON' if not curr else 'OFF'}")

        await self._restore_main_keyboard(update)
        await self.show_setup_menu(update)
        return SELECT

    async def setup_chop_select(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text
        if "1" in text:
            curr = self.cfg.get('signal_engine', {}).get('common_settings', {}).get('chop_entry_enabled', True)
            await self.cfg.update_value(['signal_engine', 'common_settings', 'chop_entry_enabled'], not curr)
            await update.message.reply_text(f"✅ CHOP Entry: {'ON' if not curr else 'OFF'}")
        elif "2" in text:
            curr = self.cfg.get('signal_engine', {}).get('common_settings', {}).get('chop_exit_enabled', True)
            await self.cfg.update_value(['signal_engine', 'common_settings', 'chop_exit_enabled'], not curr)
            await update.message.reply_text(f"✅ CHOP Exit: {'ON' if not curr else 'OFF'}")

        await self._restore_main_keyboard(update)
        await self.show_setup_menu(update)
        return SELECT

    async def setup_kalman_select(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text
        if "1" in text:
            curr = self.cfg.get('signal_engine', {}).get('strategy_params', {}).get('kalman_filter', {}).get('entry_enabled', False)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'kalman_filter', 'entry_enabled'], not curr)
            await update.message.reply_text(f"✅ Kalman Entry: {'ON' if not curr else 'OFF'}")
        elif "2" in text:
            curr = self.cfg.get('signal_engine', {}).get('strategy_params', {}).get('kalman_filter', {}).get('exit_enabled', False)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'kalman_filter', 'exit_enabled'], not curr)
            await update.message.reply_text(f"✅ Kalman Exit: {'ON' if not curr else 'OFF'}")

        await self._restore_main_keyboard(update)
        await self.show_setup_menu(update)
        return SELECT

    async def _capture_month_end_open_positions(self):
        engine = self.engines.get(CORE_ENGINE) or self.active_engine
        if engine is None:
            return []
        symbols = await engine.get_active_position_symbols(use_cache=False)
        positions = []
        for symbol in sorted(symbols or []):
            position = await engine.get_server_position(symbol, use_cache=False)
            if not position:
                continue
            positions.append({
                'symbol': position.get('symbol') or symbol,
                'side': position.get('side'),
                'contracts': position.get('contracts'),
                'entryPrice': position.get('entryPrice'),
                'markPrice': position.get('markPrice'),
                'unrealizedPnl': position.get('unrealizedPnl'),
                'percentage': position.get('percentage'),
            })
        return positions

    async def _send_monthly_trade_report(self, year, month, store, open_positions):
        engine = self.engines.get(CORE_ENGINE) or self.active_engine
        if engine is not None:
            try:
                finalizer = TradeAccountingFinalizer(engine.exchange, store)
                await finalizer.finalize_pending(limit=100)
            except Exception:
                logger.exception(
                    'Monthly trade accounting finalization failed; provisional rows will be reported'
                )
        text = build_monthly_trade_report(
            store.load_trade_results(),
            year,
            month,
            open_positions=open_positions,
            generated_at=datetime.now(MONTHLY_REPORT_KST),
        )
        cid = self.cfg.get_chat_id()
        if not cid or not self.tg_app:
            raise RuntimeError('Telegram chat/application unavailable for monthly report')
        filename = f'monthly_trade_report_{int(year):04d}-{int(month):02d}.txt'
        bio = io.BytesIO(text.encode('utf-8'))
        bio.name = filename
        await self.tg_app.bot.send_document(
            chat_id=cid,
            document=bio,
            filename=filename,
            caption=(
                f'{int(year):04d}-{int(month):02d} 실전 자동매매 월말 리포트입니다. '
                '손익은 실제 포지션별로 한 번만 집계했습니다.'
            ),
        )

    async def _monthly_trade_report_loop(self):
        await asyncio.sleep(5)
        while True:
            try:
                reporting = self._telegram_reporting_cfg()
                if not bool(reporting.get('monthly_trade_report_enabled', True)):
                    await asyncio.sleep(60)
                    continue
                now = datetime.now(MONTHLY_REPORT_KST)
                if now.day != 1:
                    await asyncio.sleep(60)
                    continue
                engine = self.engines.get(CORE_ENGINE) or self.active_engine
                if engine is None:
                    await asyncio.sleep(60)
                    continue
                _ensure_trading_safety_runtime(engine)
                store = getattr(engine, 'trading_state_store', None)
                if store is None:
                    raise RuntimeError('trading state store unavailable')
                report_year, report_month = previous_calendar_month(now)
                report_key = f'{int(report_year):04d}-{int(report_month):02d}'
                snapshot_key = f'monthly_trade_report_open_positions:{report_key}'
                snapshot = store.get_runtime_state(snapshot_key)
                if not isinstance(snapshot, dict):
                    snapshot = {
                        'captured_at': now.isoformat(),
                        'positions': await self._capture_month_end_open_positions(),
                    }
                    store.set_runtime_state(snapshot_key, snapshot)

                hour = max(
                    0,
                    min(23, int(reporting.get('monthly_trade_report_hour', 9) or 9)),
                )
                if now.hour < hour:
                    await asyncio.sleep(60)
                    continue
                sent_key = f'monthly_trade_report_sent:{report_key}'
                if store.get_runtime_state(sent_key):
                    await asyncio.sleep(60)
                    continue
                await self._send_monthly_trade_report(
                    report_year,
                    report_month,
                    store,
                    list(snapshot.get('positions') or []),
                )
                store.set_runtime_state(sent_key, {
                    'sent_at': datetime.now(MONTHLY_REPORT_KST).isoformat(),
                    'period': report_key,
                })
                logger.info('Monthly live trade report sent for %s', report_key)
                await asyncio.sleep(60)
            except Exception as exc:
                logger.error('Monthly trade report error: %s', exc, exc_info=True)
                await asyncio.sleep(60)

    async def _hourly_report_loop(self):
        """시간별 리포트."""
        await asyncio.sleep(60)  # ?쒖옉 ?湲?

        while True:
            try:
                reporting = self.cfg.get('telegram', {}).get('reporting', {})
                if reporting.get('periodic_reports_enabled', False) and reporting.get('hourly_report_enabled', False):
                    now = datetime.now()
                    if now.minute == 0 and time.time() - self.last_hourly_report > 3500:
                        self.last_hourly_report = time.time()

                        daily_count, daily_pnl = self.db.get_daily_stats()
                        d = {}
                        if isinstance(self.status_data, dict) and self.status_data:
                            first_key = next(iter(self.status_data))
                            first_val = self.status_data.get(first_key)
                            if isinstance(first_val, dict):
                                d = first_val

                        quote_currency = d.get('quote_currency', 'KRW' if self.is_upbit_mode() else 'USDT')

                        def fmt_report_money(value, signed=False):
                            amount = float(value or 0)
                            if str(quote_currency).upper() == 'KRW':
                                prefix = '+' if signed and amount >= 0 else ''
                                return f"{prefix}₩{amount:,.0f}"
                            prefix = '+' if signed and amount >= 0 else ''
                            return f"{prefix}${amount:.2f}"

                        msg = f"""
⏱ **시간별 리포트** [{now.strftime('%H:%M')}]

💰 자산: {fmt_report_money(d.get('total_equity', 0))}
📈 일일 손익: {fmt_report_money(daily_pnl, signed=True)} ({daily_count}건)
🛡 MMR: {d.get('mmr', 0):.2f}%
"""
                        await self.notify(msg.strip())

                await asyncio.sleep(30)
            except Exception as e:
                logger.error(f"Hourly report error: {e}")
                await asyncio.sleep(60)

    async def process_manual_resume_request(self):
        request = load_manual_resume_request()
        if request is None:
            return {"status": "NO_RESUME_REQUEST"}
        pause = load_critical_pause_state()
        issues = []
        reconciled_at = None
        approved = False
        status = "RESUME_REJECTED_RECONCILIATION_FAILED"
        try:
            if pause is None:
                issues.append("critical_pause_state_missing")
                status = "RESUME_REJECTED_NO_PAUSE_STATE"
            elif request.symbol not in {str(pause.get('symbol') or ''), '*'}:
                issues.append("resume_symbol_mismatch")
                status = "RESUME_REJECTED_SYMBOL_MISMATCH"
            else:
                engine = getattr(self, 'engines', {}).get('signal') or getattr(self, 'active_engine', None)
                if engine is None or not hasattr(engine, '_reconcile_crypto_exchange_state'):
                    issues.append("signal_engine_unavailable")
                else:
                    _ensure_trading_safety_runtime(engine)
                    stream_state = engine.trading_state_store.get_runtime_state(
                        'user_data_stream',
                        {},
                    ) or {}
                    stream_ready = bool(
                        stream_state.get('connected')
                        and stream_state.get('reason') == 'connected_and_reconciled'
                    )
                    if not stream_ready:
                        issues.append("user_stream_not_ready")
                        status = "RESUME_REJECTED_USER_STREAM_NOT_READY"
                    else:
                        reconciliation = await engine._reconcile_crypto_exchange_state(
                            user_stream_ready=True,
                            require_user_stream=True,
                        )
                        reconciled_at = reconciliation.reconciled_at
                        blocking = engine.trading_state_store.list_by_states(
                            ENTRY_BLOCKING_STATES
                        )
                        unresolved = [
                            record.client_order_id
                            for record in blocking
                            if record.order_state != OrderState.PROTECTED.value
                        ]
                        issues.extend(reconciliation.issues)
                        issues.extend(reconciliation.unresolved_records)
                        if unresolved:
                            issues.extend(f"active_order:{item}" for item in unresolved)
                            status = "RESUME_REJECTED_UNRESOLVED_ORDER"
                        elif reconciliation.positions and any(
                            record.order_state == OrderState.FILLED_UNPROTECTED.value
                            for record in blocking
                        ):
                            issues.append("unprotected_position")
                            status = "RESUME_REJECTED_UNPROTECTED_POSITION"
                        elif not reconciliation.algo_orders_ok:
                            issues.append("algo_snapshot_unavailable")
                            status = "RESUME_REJECTED_ALGO_SNAPSHOT_UNAVAILABLE"
                        elif reconciliation.safe_to_trade:
                            archive_critical_pause(PAUSE_STATE_FILE, request)
                            approved = True
                            status = "RESUME_APPROVED"
                            engine._set_crypto_entry_lock(None)
                            self.is_paused = False
                            if getattr(engine, 'ctrl', None) is not None:
                                engine.ctrl.is_paused = False
            result = ManualResumeResult(
                request_id=request.request_id,
                approved=approved,
                status=status,
                issues=tuple(issues),
                reconciled_at=reconciled_at,
            )
            write_manual_resume_result(result)
            archive_processed_request(request)
            notifier = getattr(self, 'notify', None)
            if callable(notifier):
                try:
                    await notifier(
                        f"Manual resume {status}: {request.symbol}"
                        + (f" / {', '.join(issues[:5])}" if issues else "")
                    )
                except Exception:
                    logger.exception("Manual resume Telegram notification failed")
            return {
                "request_id": request.request_id,
                "approved": approved,
                "status": status,
                "issues": issues,
                "reconciled_at": reconciled_at,
            }
        except Exception as exc:
            logger.exception("Manual resume reconciliation failed")
            result = ManualResumeResult(
                request_id=request.request_id,
                approved=False,
                status="RESUME_REJECTED_RECONCILIATION_FAILED",
                issues=(f"{type(exc).__name__}:{exc}",),
                reconciled_at=reconciled_at,
            )
            write_manual_resume_result(result)
            archive_processed_request(request)
            return {
                "request_id": request.request_id,
                "approved": False,
                "status": result.status,
                "issues": list(result.issues),
                "reconciled_at": reconciled_at,
            }

    async def _heartbeat_loop(self):
        """런타임 heartbeat 파일 갱신."""
        await asyncio.sleep(5)

        while True:
            try:
                await self.process_manual_resume_request()
                service = getattr(self, 'crypto_execution_service', None)
                accounting = getattr(service, 'accounting_service', None)
                if accounting is not None:
                    try:
                        await accounting.finalize_pending(limit=5)
                        ENGINE_PERFORMANCE_STATS.clear()
                        ENGINE_PERFORMANCE_STATS.update(
                            rebuild_engine_performance_stats(
                                service.state_store.load_trade_results()
                            )
                        )
                    except Exception:
                        logger.exception("Provisional trade accounting finalization failed")
                self._write_heartbeat()
                await asyncio.sleep(60)
            except Exception as e:
                logger.error(f"Heartbeat loop error: {e}")
                await asyncio.sleep(30)

    async def _main_polling_loop(self):
        """
        ?쒖닔 ?대쭅 硫붿씤 猷⑦봽 - WebSocket ?놁씠 紐⑤뱺 寃껋쓣 泥섎━
        - Signal ?붿쭊: 媛寃?紐⑤땲?곕쭅 + 罹붾뱾 ?좏샇 泥댄겕
        - Shannon ?붿쭊: 媛寃?紐⑤땲?곕쭅 + 由щ갭?곗떛 泥댄겕
        """
        logger.info("?봽 [Polling] Main polling loop started (Pure Polling Mode)")
        await asyncio.sleep(3)  # ?쒖옉 ?湲?

        while True:
            try:
                eng = self.cfg.get('system_settings', {}).get('active_engine', CORE_ENGINE)

                if eng == 'signal' and self.active_engine and self.active_engine.running:
                    # Signal ?붿쭊 ?대쭅
                    signal_engine = self.engines.get('signal')
                    if signal_engine and hasattr(signal_engine, 'poll_tick'):
                        await signal_engine.poll_tick()

                elif eng == 'shannon' and self.active_engine and self.active_engine.running:
                    # Shannon ?붿쭊 ?대쭅
                    shannon_engine = self.engines.get('shannon')
                    if shannon_engine and hasattr(shannon_engine, 'poll_tick'):
                        await shannon_engine.poll_tick()

                elif eng == 'dualthrust' and self.active_engine and self.active_engine.running:
                    # Dual Thrust ?붿쭊 ?대쭅
                    dt_engine = self.engines.get('dualthrust')
                    if dt_engine and hasattr(dt_engine, 'poll_tick'):
                        await dt_engine.poll_tick()

                elif eng == 'dualmode' and self.active_engine and self.active_engine.running:
                    # Dual Mode ?붿쭊 ?대쭅
                    dm_engine = self.engines.get('dualmode')
                    if dm_engine and hasattr(dm_engine, 'poll_tick'):
                        await dm_engine.poll_tick()

                elif eng == 'tema' and self.active_engine and self.active_engine.running:
                    # TEMA ?붿쭊 ?대쭅
                    tema_engine = self.engines.get('tema')
                    if tema_engine and hasattr(tema_engine, 'poll_tick'):
                        await tema_engine.poll_tick()

                # [MODIFIED] Prioritize entry_timeframe for polling interval
                sys_cfg = self.get_active_common_settings()
                tf = sys_cfg.get('entry_timeframe', sys_cfg.get('timeframe', '15m'))
                monitoring_interval = self.cfg.get('system_settings', {}).get(
                    'monitoring_interval_seconds'
                )
                poll_interval = self._get_poll_interval(
                    tf,
                    monitoring_interval_seconds=monitoring_interval,
                )
                await asyncio.sleep(poll_interval)

            except Exception as e:
                logger.error(f"Main polling loop error: {e}")
                await asyncio.sleep(30)

    def _get_poll_interval(self, tf, monitoring_interval_seconds=None):
        """??꾪봽?덉엫???곕Ⅸ ?대쭅 媛꾧꺽 怨꾩궛"""
        tf_seconds = {
            '1m': 60, '3m': 180, '5m': 300, '15m': 900,
            '30m': 1800, '1h': 3600, '2h': 7200, '4h': 14400,
            '6h': 21600, '8h': 28800, '12h': 43200, '1d': 86400
        }
        candle_seconds = tf_seconds.get(tf, 900)  # 湲곕낯 15遺?
        # 罹붾뱾 ?쒓컙??1/6 媛꾧꺽?쇰줈 ?대쭅 (理쒖냼 10珥? 理쒕? 60珥?
        # 2H = 7200珥???1200珥?20遺?... 理쒕? 60珥덈줈 ?쒗븳
        interval = max(10, min(60, candle_seconds // 6))
        if monitoring_interval_seconds is None:
            return interval
        try:
            configured_interval = float(monitoring_interval_seconds)
        except (TypeError, ValueError):
            return interval
        if configured_interval <= 0:
            return interval
        configured_interval = max(10, min(60, configured_interval))
        return min(interval, configured_interval)

    async def _dashboard_loop(self):
        logger.info("Legacy Telegram dashboard loop disabled; manual /status only.")
        return

    async def _refresh_dashboard(self, force=False):
        logger.info(
            f"Legacy Telegram dashboard refresh ignored (force={force}); manual /status only."
        )
        return False

    def _record_status_snapshot(self, snapshot_key, snapshot_text):
        if not snapshot_key or snapshot_key == self.last_status_snapshot_key:
            return
        self.db.log_status_snapshot(snapshot_key, snapshot_text)
        self.last_status_snapshot_key = snapshot_key

    async def _upsert_dashboard_message(self, text):
        if self._should_suppress_telegram_notice(text):
            logger.info("Telegram dashboard message suppressed by event-only alert mode.")
            return False
        cid = self.cfg.get_chat_id()
        if not cid or not self.tg_app:
            logger.error("Invalid chat_id - dashboard refresh skipped")
            return False

        plain_text = text.replace('**', '').replace('`', '')

        if self.dashboard_msg_id:
            try:
                if self.dashboard_plain_text_mode:
                    await self.tg_app.bot.edit_message_text(
                        chat_id=cid,
                        message_id=self.dashboard_msg_id,
                        text=plain_text
                    )
                else:
                    await self.tg_app.bot.edit_message_text(
                        chat_id=cid,
                        message_id=self.dashboard_msg_id,
                        text=text,
                        parse_mode=ParseMode.MARKDOWN
                    )
                return True
            except RetryAfter as e:
                logger.warning(f"Flood Wait: Sleeping {e.retry_after}s")
                await asyncio.sleep(e.retry_after)
                try:
                    if self.dashboard_plain_text_mode:
                        await self.tg_app.bot.edit_message_text(
                            chat_id=cid,
                            message_id=self.dashboard_msg_id,
                            text=plain_text
                        )
                    else:
                        await self.tg_app.bot.edit_message_text(
                            chat_id=cid,
                            message_id=self.dashboard_msg_id,
                            text=text,
                            parse_mode=ParseMode.MARKDOWN
                        )
                    return True
                except Exception as retry_error:
                    logger.warning(f"Dashboard retry edit error: {retry_error}")
                    self.dashboard_msg_id = None
            except TimedOut as e:
                logger.warning(f"Dashboard edit timeout: {e}")
            except BadRequest as e:
                if "Message is not modified" in str(e):
                    return True
                if "can't parse entities" in str(e).lower():
                    logger.warning("Dashboard markdown parse failed on edit; switching to plain text mode")
                    self.dashboard_plain_text_mode = True
                    try:
                        await self.tg_app.bot.edit_message_text(
                            chat_id=cid,
                            message_id=self.dashboard_msg_id,
                            text=plain_text
                        )
                        return True
                    except Exception as plain_error:
                        logger.warning(f"Dashboard plain text edit error: {plain_error}")
                        self.dashboard_msg_id = None
                        return False
                logger.warning(f"Dashboard edit error: {e}")
                if "message to edit not found" in str(e).lower():
                    self.dashboard_msg_id = None
            except Exception as e:
                logger.error(f"Dashboard error: {e}")

        try:
            if self.dashboard_plain_text_mode:
                m = await self.tg_app.bot.send_message(
                    chat_id=cid,
                    text=plain_text
                )
            else:
                m = await self.tg_app.bot.send_message(
                    chat_id=cid,
                    text=text,
                    parse_mode=ParseMode.MARKDOWN
                )
            self.dashboard_msg_id = m.message_id
            return True
        except RetryAfter as e:
            logger.warning(f"Send Flood Wait: {e.retry_after}s")
            await asyncio.sleep(min(e.retry_after, 60))
        except TimedOut as e:
            logger.warning(f"Dashboard send timeout: {e}")
        except BadRequest as e:
            if "can't parse entities" in str(e).lower():
                logger.warning("Dashboard markdown parse failed on send; switching to plain text mode")
                self.dashboard_plain_text_mode = True
                try:
                    m = await self.tg_app.bot.send_message(chat_id=cid, text=plain_text)
                    self.dashboard_msg_id = m.message_id
                    return True
                except Exception as plain_error:
                    logger.error(f"Dashboard plain text send error: {plain_error}")
                    return False
            logger.error(f"Dashboard send error: {e}")
        except Exception as e:
            logger.error(f"Dashboard send error: {e}")
        return False

    def _build_dashboard_messages(self, all_data, blink, pause_indicator):
        eng = self.cfg.get('system_settings', {}).get('active_engine', 'unknown').upper()
        now = datetime.now()
        body = self._format_dashboard_body(all_data)
        live_text = f"{blink} **[{eng}] 대시보드**{pause_indicator} [{now.strftime('%H:%M:%S')}]\n\n{body}"
        history_text = f"**[{eng}] 상태 이력**{pause_indicator} [{now.strftime('%Y-%m-%d %H:%M:%S')}]\n\n{body}"
        snapshot_key = f"{eng}|{pause_indicator}|{body}"
        return live_text, history_text, snapshot_key

    def _format_dashboard_body(self, all_data):
        """텔레그램 대시보드 본문 생성."""
        try:
            if not all_data:
                return "데이터 수집 대기 중..."

            def fmt_money(value, quote_currency):
                amount = float(value or 0)
                if str(quote_currency).upper() == 'KRW':
                    return f"₩{amount:,.0f}"
                return f"${amount:.2f}"

            def fmt_signed_money(value, quote_currency):
                amount = float(value or 0)
                prefix = '+' if amount >= 0 else ''
                if str(quote_currency).upper() == 'KRW':
                    return f"{prefix}₩{amount:,.0f}"
                return f"{prefix}${amount:.2f}"

            def fmt_price(value, quote_currency):
                amount = float(value or 0)
                if str(quote_currency).upper() == 'KRW':
                    return f"{amount:,.0f}"
                return f"{amount:.2f}"

            msg = ""
            summary_candidates = [v for v in all_data.values() if isinstance(v, dict)]
            d_first = max(
                summary_candidates,
                key=lambda row: float(row.get('total_equity', 0) or 0),
                default=next(iter(all_data.values()))
            )
            first_quote = d_first.get('quote_currency', 'KRW' if self.is_upbit_mode() else 'USDT')

            msg += "💰 **자산 요약**\n"
            msg += (
                f"총자산: `{fmt_money(d_first.get('total_equity', 0), first_quote)}` | "
                f"가용자산: `{fmt_money(d_first.get('free_usdt', 0), first_quote)}`\n"
            )
            msg += f"MMR: `{d_first.get('mmr', 0):.2f}%` | 일일 PnL: `{fmt_signed_money(d_first.get('daily_pnl', 0), first_quote)}`\n"
            runtime_diag = d_first.get('runtime_diag', {})
            if runtime_diag:
                msg += (
                    f"♻️ 런타임: `{runtime_diag.get('uptime_human', '-')}` | "
                    f"시작 `{runtime_diag.get('launch_reason', '-')}` | "
                    f"메모리 `{runtime_diag.get('rss_mb', '-')}`MB\n"
                )
                restart_bits = []
                if runtime_diag.get('last_heartbeat_age') is not None:
                    restart_bits.append(f"이전 heartbeat `{runtime_diag.get('last_heartbeat_age_human', '-')}`")
                if runtime_diag.get('last_pid_before_start'):
                    restart_bits.append(f"이전 PID `{runtime_diag.get('last_pid_before_start')}`")
                if restart_bits:
                    msg += f"🧷 최근 재시작: {' | '.join(restart_bits)}\n"
                if runtime_diag.get('last_log_line'):
                    msg += f"🪵 직전 로그: `{runtime_diag.get('last_log_line')}`\n"
                if runtime_diag.get('last_exit_reason'):
                    exit_bits = [
                        f"사유 `{runtime_diag.get('last_exit_reason')}`",
                        f"시각 `{runtime_diag.get('last_exit_ts', '-')}`"
                    ]
                    if runtime_diag.get('last_exit_uptime_human'):
                        exit_bits.append(f"uptime `{runtime_diag.get('last_exit_uptime_human')}`")
                    if runtime_diag.get('last_exit_rss_mb') is not None:
                        exit_bits.append(f"mem `{runtime_diag.get('last_exit_rss_mb')}`MB")
                    msg += f"🧯 직전 종료: {' | '.join(exit_bits)}\n"
                if runtime_diag.get('last_exit_detail'):
                    msg += f"🧾 종료상세: `{runtime_diag.get('last_exit_detail')}`\n"
            msg += "━━━━━━━━━━\n"

            for symbol, d in all_data.items():
                cur_price = d.get('price', 0)
                pos_side = d.get('pos_side', 'NONE')
                lev = d.get('leverage', '?')
                mm = d.get('margin_mode', 'ISO')
                quote_currency = d.get('quote_currency', first_quote)
                symbol_label = self.format_symbol_for_display(symbol)
                mode_str = "(SPOT)" if d.get('is_spot') else (f"({mm} {lev}x)" if 'leverage' in d else "")

                pos_icon = "🟢" if pos_side == 'LONG' else "🔴" if pos_side == 'SHORT' else "⚪"
                msg += f"{pos_icon} **{symbol_label}** {mode_str} | `{pos_side}`\n"

                if pos_side != 'NONE':
                    pnl_pct = d.get('pnl_pct', 0)
                    pnl_icon = "📈" if pnl_pct >= 0 else "📉"
                    msg += f"{pnl_icon} PnL: `{fmt_signed_money(d.get('pnl_usdt', 0), quote_currency)}` (`{pnl_pct:+.2f}%`)\n"
                    msg += (
                        f"진입가: `{fmt_price(d.get('entry_price', 0), quote_currency)}` | "
                        f"현재가: `{fmt_price(cur_price, quote_currency)}`\n"
                    )
                else:
                    msg += f"현재가: `{fmt_price(cur_price, quote_currency)}`\n"

                d_eng = d.get('engine', '').upper()
                if d_eng == 'SIGNAL':
                    e_tf = d.get('entry_tf', '?')
                    x_tf = d.get('exit_tf', '?')
                    entry_reason = d.get('entry_reason', '대기')
                    protection_cfg = d.get('protection_config', {})
                    tp_expected = bool(protection_cfg.get('tp_expected', protection_cfg.get('tp_enabled', False)))
                    sl_expected = bool(protection_cfg.get('sl_expected', protection_cfg.get('sl_enabled', False)))
                    tp_present = bool(protection_cfg.get('tp_present', False))
                    sl_present = bool(protection_cfg.get('sl_present', False))
                    tp_text = "OK" if tp_expected and tp_present else "누락" if tp_expected else "OFF"
                    sl_text = "OK" if sl_expected and sl_present else "누락" if sl_expected else "OFF"
                    audit_status = protection_cfg.get('audit_status', '-')
                    network = d.get('network', '?')
                    exchange_id = d.get('exchange_id', '?')
                    market_data_exchange_id = d.get('market_data_exchange_id', '?')
                    market_data_source = d.get('market_data_source', 'BINANCE FUTURES MAINNET PUBLIC')
                    msg += f"⏱ TF: 진입 `{e_tf}` / 청산 `{x_tf}`\n"
                    msg += f"🌐 거래소: `{exchange_id}` | 네트워크 `{network}`\n"
                    msg += f"📡 신호데이터: `{market_data_exchange_id}` | `{market_data_source}`\n"
                    msg += f"🛡 보호주문: TP `{tp_text}`({protection_cfg.get('tp_count', 0)}) | SL `{sl_text}`({protection_cfg.get('sl_count', 0)}) | audit `{audit_status}`\n"
                    msg += f"📝 진입판정: `{entry_reason}`\n"
                    stateful_diag = d.get('stateful_diag', {})
                    if stateful_diag:
                        msg += (
                            f"🧪 상태진단: stage `{stateful_diag.get('stage', '-')}` | "
                            f"raw `{stateful_diag.get('raw_state', '-')}` | "
                            f"entry `{stateful_diag.get('entry_sig', '-')}` | "
                            f"pos `{stateful_diag.get('pos_side', '-')}`\n"
                        )
                        if stateful_diag.get('timing_label'):
                            timing_line = (
                                f"타이밍: `{stateful_diag.get('timing_label')}` | "
                                f"raw `{stateful_diag.get('timing_signal', '-')}`"
                            )
                            if stateful_diag.get('timing_signal_ts_human'):
                                timing_line += f" | signal `{stateful_diag.get('timing_signal_ts_human')}`"
                            if stateful_diag.get('effective_timing_signal') not in (None, 'none'):
                                timing_line += f" | effective `{stateful_diag.get('effective_timing_signal')}`"
                            if stateful_diag.get('timing_match_source'):
                                timing_line += f" | source `{stateful_diag.get('timing_match_source')}`"
                            msg += timing_line + "\n"
                        if stateful_diag.get('latched_timing_signal'):
                            latched_line = (
                                f"저장신호: `{stateful_diag.get('latched_timing_signal')}` | "
                                f"usable `{stateful_diag.get('latched_timing_available')}`"
                            )
                            if stateful_diag.get('latched_timing_signal_ts_human'):
                                latched_line += f" | ts `{stateful_diag.get('latched_timing_signal_ts_human')}`"
                            msg += latched_line + "\n"
                        if stateful_diag.get('timing_reason'):
                            msg += f"타이밍판정: `{stateful_diag.get('timing_reason')}`\n"
                        if stateful_diag.get('ut_key') is not None:
                            msg += (
                                f"UT 설정: K `{stateful_diag.get('ut_key')}` | "
                                f"ATR `{stateful_diag.get('ut_atr')}` | "
                                f"HA `{stateful_diag.get('ut_ha', '-')}`\n"
                            )
                        if stateful_diag.get('src') is not None and stateful_diag.get('stop') is not None:
                            msg += (
                                f"UT 값: src `{float(stateful_diag.get('src')):.2f}` | "
                                f"stop `{float(stateful_diag.get('stop')):.2f}`\n"
                            )
                        if stateful_diag.get('signal_ts_human') or stateful_diag.get('feed_last_ts_human'):
                            msg += (
                                f"UT 캔들: tf `{stateful_diag.get('tf_used', '?')}` | "
                                f"signal `{stateful_diag.get('signal_ts_human', '?')}` | "
                                f"feed_last `{stateful_diag.get('feed_last_ts_human', '?')}`\n"
                            )
                        if stateful_diag.get('closed_ohlc_text'):
                            msg += f"UT 확정봉: `{stateful_diag.get('closed_ohlc_text')}`\n"
                        if stateful_diag.get('live_ohlc_text'):
                            msg += f"UT 진행봉: `{stateful_diag.get('live_ohlc_text')}`\n"
                        pending_visible = any([
                            stateful_diag.get('ut_pending_source'),
                            stateful_diag.get('ut_pending_side'),
                            stateful_diag.get('ut_pending_state'),
                            stateful_diag.get('ut_pending_execute_ts_human')
                        ])
                        if pending_visible:
                            pending_line = "UT pending: "
                            if stateful_diag.get('ut_pending_source'):
                                pending_line += f"source `{stateful_diag.get('ut_pending_source')}` | "
                            pending_line += (
                                f"side `{stateful_diag.get('ut_pending_side', '-')}` | "
                                f"state `{stateful_diag.get('ut_pending_state', '-')}` | "
                                f"execute `{stateful_diag.get('ut_pending_execute_ts_human', '-')}`"
                            )
                            if stateful_diag.get('ut_pending_expires_ts_human'):
                                pending_line += f" | expires `{stateful_diag.get('ut_pending_expires_ts_human')}`"
                            msg += pending_line + "\n"
                        if stateful_diag.get('ut_flip_target_side'):
                            flip_line = (
                                f"UTBOT flip: target `{stateful_diag.get('ut_flip_target_side')}` | "
                                f"signal `{stateful_diag.get('ut_flip_signal_ts_human', '-')}`"
                            )
                            if stateful_diag.get('ut_flip_execute_ts_human'):
                                flip_line += f" | execute `{stateful_diag.get('ut_flip_execute_ts_human')}`"
                            if stateful_diag.get('ut_flip_expires_ts_human'):
                                flip_line += f" | expires `{stateful_diag.get('ut_flip_expires_ts_human')}`"
                            if stateful_diag.get('ut_flip_retry_count') is not None:
                                flip_line += f" | retry `{stateful_diag.get('ut_flip_retry_count')}`"
                            msg += flip_line + "\n"
                        if stateful_diag.get('ut_flip_block_reason'):
                            msg += f"UTBOT flip 차단사유: `{stateful_diag.get('ut_flip_block_reason')}`\n"
                        if (
                            stateful_diag.get('utsmc_entry_bullish_ob_entry') is not None
                            or stateful_diag.get('utsmc_entry_bearish_ob_entry') is not None
                            or stateful_diag.get('utsmc_entry_ob_tags')
                        ):
                            smc_entry_bull = 'Y' if stateful_diag.get('utsmc_entry_bullish_ob_entry') else 'N'
                            smc_entry_bear = 'Y' if stateful_diag.get('utsmc_entry_bearish_ob_entry') else 'N'
                            smc_entry_tf = stateful_diag.get('utsmc_entry_tf')
                            smc_entry_line = f"UTSMC entry OB{f'[{smc_entry_tf}]' if smc_entry_tf else ''}: bull `{smc_entry_bull}` | bear `{smc_entry_bear}`"
                            if stateful_diag.get('utsmc_entry_ob_tags'):
                                smc_entry_line += f" | tags `{stateful_diag.get('utsmc_entry_ob_tags')}`"
                            msg += smc_entry_line + "\n"
                        if (
                            stateful_diag.get('utsmc_entry_bullish_ob_bottom') is not None
                            or stateful_diag.get('utsmc_entry_bullish_ob_top') is not None
                            or stateful_diag.get('utsmc_entry_bearish_ob_bottom') is not None
                            or stateful_diag.get('utsmc_entry_bearish_ob_top') is not None
                        ):
                            msg += (
                                f"UTSMC entry OB range: "
                                f"bull `{float(stateful_diag.get('utsmc_entry_bullish_ob_bottom') or 0.0):.2f}` ~ "
                                f"`{float(stateful_diag.get('utsmc_entry_bullish_ob_top') or 0.0):.2f}` | "
                                f"bear `{float(stateful_diag.get('utsmc_entry_bearish_ob_bottom') or 0.0):.2f}` ~ "
                                f"`{float(stateful_diag.get('utsmc_entry_bearish_ob_top') or 0.0):.2f}`\n"
                            )
                        if stateful_diag.get('utsmc_entry_smc_reason'):
                            msg += f"UTSMC entry 판정: `{stateful_diag.get('utsmc_entry_smc_reason')}`\n"
                        if stateful_diag.get('utsmc_candidate_filter_mode'):
                            c1_long = 'PASS' if stateful_diag.get('utsmc_candidate1_long_pass') else 'FAIL'
                            c2_long = 'PASS' if stateful_diag.get('utsmc_candidate2_long_pass') else 'FAIL'
                            final_long = 'PASS' if stateful_diag.get('utsmc_candidate_final_long_pass') else 'FAIL'
                            c1_short = 'PASS' if stateful_diag.get('utsmc_candidate1_short_pass') else 'FAIL'
                            c2_short = 'PASS' if stateful_diag.get('utsmc_candidate2_short_pass') else 'FAIL'
                            final_short = 'PASS' if stateful_diag.get('utsmc_candidate_final_short_pass') else 'FAIL'
                            msg += (
                                f"UTSMC 후보필터: mode `{stateful_diag.get('utsmc_candidate_filter_mode')}` | "
                                f"LONG `C1 {c1_long} / C2 {c2_long} / FINAL {final_long}` | "
                                f"SHORT `C1 {c1_short} / C2 {c2_short} / FINAL {final_short}`\n"
                            )
                        if stateful_diag.get('utsmc_signal_high') is not None or stateful_diag.get('utsmc_signal_low') is not None:
                            msg += (
                                f"UTSMC invalidation: high `{float(stateful_diag.get('utsmc_signal_high') or 0.0):.2f}` | "
                                f"low `{float(stateful_diag.get('utsmc_signal_low') or 0.0):.2f}`\n"
                            )
                        if (
                            stateful_diag.get('smc_bullish_ob_entry') is not None
                            or stateful_diag.get('smc_bearish_ob_entry') is not None
                            or stateful_diag.get('smc_ob_tags')
                        ):
                            smc_bull = 'Y' if stateful_diag.get('smc_bullish_ob_entry') else 'N'
                            smc_bear = 'Y' if stateful_diag.get('smc_bearish_ob_entry') else 'N'
                            smc_tf = stateful_diag.get('smc_tf')
                            smc_line = f"UTSMC exit OB{f'[{smc_tf}]' if smc_tf else ''}: bull `{smc_bull}` | bear `{smc_bear}`"
                            if stateful_diag.get('smc_ob_tags'):
                                smc_line += f" | tags `{stateful_diag.get('smc_ob_tags')}`"
                            msg += smc_line + "\n"
                        if (
                            stateful_diag.get('smc_bullish_ob_bottom') is not None
                            or stateful_diag.get('smc_bullish_ob_top') is not None
                            or stateful_diag.get('smc_bearish_ob_bottom') is not None
                            or stateful_diag.get('smc_bearish_ob_top') is not None
                        ):
                            msg += (
                                f"UTSMC exit OB range: "
                                f"bull `{float(stateful_diag.get('smc_bullish_ob_bottom') or 0.0):.2f}` ~ "
                                f"`{float(stateful_diag.get('smc_bullish_ob_top') or 0.0):.2f}` | "
                                f"bear `{float(stateful_diag.get('smc_bearish_ob_bottom') or 0.0):.2f}` ~ "
                                f"`{float(stateful_diag.get('smc_bearish_ob_top') or 0.0):.2f}`\n"
                            )
                        if stateful_diag.get('smc_reason'):
                            msg += f"UTSMC exit 판정: `{stateful_diag.get('smc_reason')}`\n"
                        if stateful_diag.get('smc_reason_ts_human'):
                            msg += f"UTSMC exit 판정시각: `{stateful_diag.get('smc_reason_ts_human')}`\n"
                        diag_note = stateful_diag.get('note')
                        if diag_note:
                            msg += f"진단메모: `{diag_note}`\n"

                    active_strat = d.get('active_strategy', '')
                    utbot_filter_pack = d.get('utbot_filter_pack', {}) or {}
                    utbot_filter_cfg = utbot_filter_pack.get('config', {}) if isinstance(utbot_filter_pack, dict) else {}
                    utbot_rsi_momentum_filter = d.get('utbot_rsi_momentum_filter', {}) or {}
                    utbot_rsi_cfg = utbot_rsi_momentum_filter.get('config', {}) if isinstance(utbot_rsi_momentum_filter, dict) else {}
                    if active_strat == 'UTBOT' and utbot_filter_cfg:
                        utbot_entry_cfg = utbot_filter_cfg.get('entry', {})
                        utbot_exit_cfg = utbot_filter_cfg.get('exit', {})
                        utbot_entry_eval = utbot_filter_pack.get('entry', {}) if isinstance(utbot_filter_pack.get('entry', {}), dict) else {}
                        utbot_exit_eval = utbot_filter_pack.get('exit', {}) if isinstance(utbot_filter_pack.get('exit', {}), dict) else {}
                        entry_selected = normalize_utbot_filter_pack_selected(utbot_entry_cfg.get('selected', []))
                        exit_selected = normalize_utbot_filter_pack_selected(utbot_exit_cfg.get('selected', []))
                        msg += (
                            f"🧪 UTBot 필터(진입): `{format_utbot_filter_pack_selected(entry_selected)}` "
                            f"`{format_utbot_filter_pack_logic(utbot_entry_cfg.get('logic', 'and'))}`\n"
                        )
                        msg += (
                            f"🧪 UTBot 필터(청산): `{format_utbot_filter_pack_selected(exit_selected)}` "
                            f"`{format_utbot_filter_pack_logic(utbot_exit_cfg.get('logic', 'and'))}` | "
                            f"`{format_utbot_filter_pack_mode_map(utbot_exit_cfg.get('mode_by_filter', {}), exit_selected)}`\n"
                        )

                        for filter_id in entry_selected:
                            filter_detail = (utbot_entry_eval.get('filters') or {}).get(str(filter_id), {})
                            label = filter_detail.get('label', get_utbot_filter_pack_label(filter_id))
                            long_status = 'PASS' if filter_detail.get('long_pass') else 'FAIL'
                            short_status = 'PASS' if filter_detail.get('short_pass') else 'FAIL'
                            msg += (
                                f"UTBot 진입 {filter_id} `{label}`: "
                                f"LONG `{long_status}` ({filter_detail.get('reason_long', '-')}) | "
                                f"SHORT `{short_status}` ({filter_detail.get('reason_short', '-')})\n"
                            )

                        exit_mode_map = utbot_exit_cfg.get('mode_by_filter', {})
                        for filter_id in exit_selected:
                            filter_detail = (utbot_exit_eval.get('filters') or {}).get(str(filter_id), {})
                            label = filter_detail.get('label', get_utbot_filter_pack_label(filter_id))
                            mode_label = normalize_utbot_filter_pack_exit_mode(
                                exit_mode_map.get(str(filter_id), exit_mode_map.get(filter_id, 'confirm'))
                            )
                            long_status = 'PASS' if filter_detail.get('long_pass') else 'FAIL'
                            short_status = 'PASS' if filter_detail.get('short_pass') else 'FAIL'
                            msg += (
                                f"UTBot 청산 {filter_id} `{label}`[{mode_label.upper()}]: "
                                f"LONG `{long_status}` ({filter_detail.get('reason_long', '-')}) | "
                                f"SHORT `{short_status}` ({filter_detail.get('reason_short', '-')})\n"
                            )

                    if active_strat == 'UTBOT' and utbot_rsi_cfg.get('enabled'):
                        rsi_entry_eval = utbot_rsi_momentum_filter.get('entry', {}) if isinstance(utbot_rsi_momentum_filter.get('entry', {}), dict) else {}
                        rsi_exit_eval = utbot_rsi_momentum_filter.get('exit', {}) if isinstance(utbot_rsi_momentum_filter.get('exit', {}), dict) else {}
                        msg += (
                            f"🧪 RSI Momentum Trend: `ON` | "
                            f"`RSI {utbot_rsi_cfg.get('rsi_length', 14)}` / "
                            f"`P>{float(utbot_rsi_cfg.get('positive_above', 65) or 65):.1f}` / "
                            f"`N<{float(utbot_rsi_cfg.get('negative_below', 32) or 32):.1f}` / "
                            f"`EMA {utbot_rsi_cfg.get('ema_period', 5)}`\n"
                        )
                        if rsi_entry_eval:
                            long_status = 'PASS' if rsi_entry_eval.get('long_pass') else 'FAIL'
                            short_status = 'PASS' if rsi_entry_eval.get('short_pass') else 'FAIL'
                            msg += (
                                f"RMT 진입: LONG `{long_status}` ({rsi_entry_eval.get('reason_long', '-')}) | "
                                f"SHORT `{short_status}` ({rsi_entry_eval.get('reason_short', '-')})\n"
                            )
                        if rsi_exit_eval:
                            long_status = 'PASS' if rsi_exit_eval.get('long_pass') else 'FAIL'
                            short_status = 'PASS' if rsi_exit_eval.get('short_pass') else 'FAIL'
                            msg += (
                                f"RMT 청산확인: LONG `{long_status}` ({rsi_exit_eval.get('reason_long', '-')}) | "
                                f"SHORT `{short_status}` ({rsi_exit_eval.get('reason_short', '-')})\n"
                            )

                    f_cfg = d.get('filter_config', {})
                    if f_cfg:
                        entry_st = d.get('entry_filters', {})
                        exit_st = d.get('exit_filters', {})

                        def get_st_text(st_dict, cfg_key, val_key, pass_key, is_entry=True):
                            en_key = 'en_entry' if is_entry else 'en_exit'
                            if not f_cfg.get(cfg_key, {}).get(en_key, False):
                                return "-"
                            val = st_dict.get(val_key, 0.0)
                            if val == 0.0 and not is_entry:
                                return "~"
                            return "PASS" if st_dict.get(pass_key, False) else "FAIL"

                        e_r2 = get_st_text(entry_st, 'r2', 'r2_val', 'r2_pass', True)
                        e_c = get_st_text(entry_st, 'chop', 'chop_val', 'chop_pass', True)

                        x_r2 = get_st_text(exit_st, 'r2', 'r2_val', 'r2_pass', False)
                        x_c = get_st_text(exit_st, 'chop', 'chop_val', 'chop_pass', False)
                        x_cc = get_st_text(exit_st, 'cc', 'cc_val', 'cc_pass', False)
                        cc_val = exit_st.get('cc_val', 0.0)

                        msg += f"🧪 필터(진입): R2 {e_r2} | CHOP {e_c}\n"
                        msg += f"🧪 필터(청산): R2 {x_r2} | CHOP {x_c} | CC {x_cc}({cc_val:.2f})\n"

                    cand_mode = d.get('utsmc_candidate_filter_mode', 'OFF')
                    cand_status = d.get('utsmc_candidate_filter', {}) or {}
                    if active_strat == 'UTSMC' and cand_mode != 'OFF':
                        c1_final = 'PASS' if cand_status.get('candidate1_long_pass') or cand_status.get('candidate1_short_pass') else 'FAIL'
                        c2_final = 'PASS' if cand_status.get('candidate2_long_pass') or cand_status.get('candidate2_short_pass') else 'FAIL'
                        final_long = cand_status.get('candidate_final_long_pass')
                        final_short = cand_status.get('candidate_final_short_pass')
                        final_text = 'PASS' if final_long or final_short else 'FAIL'
                        msg += f"🧪 UTSMC 후보필터: {cand_mode} | C1 {c1_final} | C2 {c2_final} | FINAL {final_text}\n"

                    if active_strat == 'MICROVBO':
                        vbo = d.get('vbo_breakout_level', {})
                        if vbo:
                            msg += f"VBO: `L:{vbo.get('long', 0):.1f} / S:{vbo.get('short', 0):.1f}`\n"
                    elif active_strat == 'FRACTALFISHER':
                        msg += f"FF: `H:{d.get('fisher_hurst', 0):.2f} / F:{d.get('fisher_value', 0):.2f}`\n"
                        if d.get('fisher_trailing_stop') and pos_side != 'NONE':
                            msg += f"TS: `{d.get('fisher_trailing_stop', 0):.2f}`\n"

                elif d_eng == 'SHANNON':
                    msg += f"추세: `{d.get('trend', 'N/A')}` | EMA200: `{d.get('ema_200', 0):.1f}`\n"
                    msg += f"그리드: `{d.get('grid_orders', 0)}` | Diff: `{d.get('diff_pct', 0):.1f}%`\n"

                elif d_eng == 'DUALTHRUST':
                    msg += f"트리거: `L:{d.get('long_trigger', 0):.1f} / S:{d.get('short_trigger', 0):.1f}`\n"

                elif d_eng == 'DUALMODE':
                    mode_raw = str(d.get('dm_mode', 'N/A')).upper()
                    mode_name = {'STANDARD': '스탠다드', 'SCALPING': '스캘핑'}.get(mode_raw, mode_raw)
                    msg += f"듀얼모드: `{mode_name}` | TF: `{d.get('dm_tf')}`\n"

                msg += "\n"

            return msg.rstrip()
        except Exception as e:
            logger.error(f"Dashboard format error: {e}")
            return "❌ 대시보드 메시지 생성 오류"

    def _format_dashboard_message(self, all_data, blink, pause_indicator):
        """텔레그램 대시보드 메시지 생성."""
        live_text, _, _ = self._build_dashboard_messages(all_data, blink, pause_indicator)
        return live_text
