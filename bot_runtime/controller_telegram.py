"""Telegram authorization, setup flows, and message helpers."""

from __future__ import annotations


class ControllerTelegramMixin:
    async def global_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._require_authorized_telegram_update(update):
            return ConversationHandler.END

        text = update.message.text if update.message else ""
        action = self._extract_emergency_action(text)

        if action == "STOP":
            result = await self.emergency_stop()
            await update.message.reply_text(self._format_emergency_stop_reply(result))
            return ConversationHandler.END
        elif action == "PAUSE":
            self.is_paused = True
            await update.message.reply_text("⏸ 일시정지 (매매 중단, 모니터링 유지)")
            return ConversationHandler.END
        elif action == "RESUME":
            critical_pause = load_critical_pause_state()
            if critical_pause:
                write_manual_resume_request(
                    critical_pause.get("symbol") or "*",
                    "I_CONFIRM_MANUAL_RISK_CHECK_DONE",
                    requested_by="telegram",
                )
                result = await self.process_manual_resume_request()
                await update.message.reply_text(
                    f"Resume safety check: {result.get('status')}"
                )
                return ConversationHandler.END
            self.is_paused = False
            # Restart engine if it is not running
            if self.active_engine and not self.active_engine.running:
                self.active_engine.start()
                await update.message.reply_text("▶ 매매 재개 (엔진 재시작)")
            else:
                await update.message.reply_text("▶ 매매 재개")
            return ConversationHandler.END

        return None

    async def _reply_markdown_safe(self, message, text, reply_markup=None):
        if message is None:
            return
        try:
            await message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)
        except BadRequest as md_err:
            if self._is_telegram_message_too_long_error(md_err):
                await self._reply_long_text_with_document(
                    message,
                    str(text),
                    reply_markup=reply_markup,
                    filename="telegram_message.txt",
                    caption="텔레그램 메시지 전체 내용입니다.",
                )
            elif "can't parse entities" in str(md_err).lower():
                plain_text = str(text).replace("**", "").replace("`", "")
                try:
                    await message.reply_text(plain_text, reply_markup=reply_markup)
                except BadRequest as plain_err:
                    if not self._is_telegram_message_too_long_error(plain_err):
                        raise
                    await self._reply_long_text_with_document(
                        message,
                        plain_text,
                        reply_markup=reply_markup,
                        filename="telegram_message.txt",
                        caption="텔레그램 메시지 전체 내용입니다.",
                    )
            else:
                raise

    async def _edit_markdown_safe(
        self,
        query,
        text,
        *,
        reply_markup=None,
        filename="telegram_message.txt",
        caption="텔레그램 메시지 전체 내용입니다.",
        preview_suffix="상세 내용은 파일로 보냈습니다.",
    ):
        if query is None:
            return
        try:
            await query.edit_message_text(
                text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=reply_markup,
            )
        except BadRequest as md_err:
            if self._is_telegram_message_not_modified_error(md_err):
                return
            plain_text = str(text).replace("**", "").replace("`", "")
            if self._is_telegram_message_too_long_error(md_err):
                await self._edit_long_text_with_document(
                    query,
                    plain_text,
                    reply_markup=reply_markup,
                    filename=filename,
                    caption=caption,
                    preview_suffix=preview_suffix,
                )
                return
            if "can't parse entities" in str(md_err).lower():
                try:
                    await query.edit_message_text(plain_text, reply_markup=reply_markup)
                except BadRequest as plain_err:
                    if self._is_telegram_message_not_modified_error(plain_err):
                        return
                    if not self._is_telegram_message_too_long_error(plain_err):
                        raise
                    await self._edit_long_text_with_document(
                        query,
                        plain_text,
                        reply_markup=reply_markup,
                        filename=filename,
                        caption=caption,
                        preview_suffix=preview_suffix,
                    )
                return
            raise

    @staticmethod
    def _is_telegram_message_not_modified_error(error):
        return "message is not modified" in str(error).lower()

    @staticmethod
    def _is_telegram_message_too_long_error(error):
        message = str(error).lower()
        return "message is too long" in message or "message_too_long" in message

    @staticmethod
    def _build_telegram_long_text_preview(text, max_chars=3600, max_lines=55, suffix="상세 내용은 파일로 보냈습니다."):
        raw = str(text or "").strip()
        if not raw:
            return suffix

        lines = raw.splitlines()
        preview_lines = []
        used_chars = 0
        for line in lines:
            add_chars = len(line) + (1 if preview_lines else 0)
            if preview_lines and (len(preview_lines) >= max_lines or used_chars + add_chars > max_chars):
                break
            if not preview_lines and add_chars > max_chars:
                preview_lines.append(line[:max_chars])
                break
            if len(preview_lines) >= max_lines or used_chars + add_chars > max_chars:
                break
            preview_lines.append(line)
            used_chars += add_chars

        preview = "\n".join(preview_lines).strip() or raw[:max_chars].strip()
        return f"{preview}\n\n{suffix}".strip()

    async def _send_telegram_text_document(self, message, text, filename, caption):
        if message is None:
            return
        bio = io.BytesIO(str(text or "").encode("utf-8"))
        bio.name = filename
        await message.reply_document(
            document=bio,
            filename=filename,
            caption=caption,
        )

    async def _send_utbreakout_trace_document(self, message, text, *, full=False, symbol=None):
        if message is None:
            return
        normalized_symbol = str(symbol or "all").upper().replace(":USDT", "")
        symbol_part = re.sub(
            r"[^A-Za-z0-9]+",
            "",
            normalized_symbol,
        ) or "ALL"
        report_name = "tracefull" if full else "trace"
        await self._send_telegram_text_document(
            message,
            text,
            f"utbreakout_{report_name}_{symbol_part}.txt",
            (
                "UT Breakout 전체 진단 보고서입니다."
                if full else
                "UT Breakout 진입 경로 요약입니다."
            ),
        )

    async def _reply_long_text_with_document(
        self,
        message,
        text,
        *,
        reply_markup=None,
        filename="telegram_message.txt",
        caption="전체 내용입니다.",
        preview_suffix="상세 내용은 파일로 보냈습니다.",
        text_limit=3900,
    ):
        if message is None:
            return
        raw = str(text or "")
        if len(raw) <= text_limit:
            try:
                await message.reply_text(raw, reply_markup=reply_markup)
                return
            except BadRequest as md_err:
                if not self._is_telegram_message_too_long_error(md_err):
                    raise

        preview = self._build_telegram_long_text_preview(raw, suffix=preview_suffix)
        await message.reply_text(preview, reply_markup=reply_markup)
        await self._send_telegram_text_document(message, raw, filename, caption)

    async def _edit_long_text_with_document(
        self,
        query,
        text,
        *,
        reply_markup=None,
        filename="telegram_message.txt",
        caption="전체 내용입니다.",
        preview_suffix="상세 내용은 파일로 보냈습니다.",
        text_limit=3900,
    ):
        if query is None:
            return
        raw = str(text or "")
        if len(raw) <= text_limit:
            try:
                await query.edit_message_text(raw, reply_markup=reply_markup)
                return
            except BadRequest as md_err:
                if self._is_telegram_message_not_modified_error(md_err):
                    return
                if not self._is_telegram_message_too_long_error(md_err):
                    raise

        preview = self._build_telegram_long_text_preview(raw, suffix=preview_suffix)
        try:
            await query.edit_message_text(preview, reply_markup=reply_markup)
        except BadRequest as md_err:
            if not self._is_telegram_message_not_modified_error(md_err):
                raise

        await self._send_telegram_text_document(
            getattr(query, "message", None),
            raw,
            filename,
            caption,
        )

    def _extract_telegram_command(self, text):
        normalized = str(text or "").strip()
        if not normalized.startswith('/'):
            return None
        first_token = normalized.split(None, 1)[0]
        return first_token.split('@', 1)[0].lower()

    def _telegram_chat_id_from_update(self, update):
        chat = getattr(update, "effective_chat", None)
        if chat is not None and getattr(chat, "id", None) is not None:
            try:
                return int(chat.id)
            except (TypeError, ValueError):
                return 0
        return 0

    def _is_authorized_telegram_update(self, update):
        try:
            configured_chat_id = int(self.cfg.get_chat_id())
        except Exception:
            configured_chat_id = 0
        incoming_chat_id = self._telegram_chat_id_from_update(update)
        return bool(configured_chat_id and incoming_chat_id == configured_chat_id)

    async def _reject_unauthorized_telegram_update(self, update):
        incoming_chat_id = self._telegram_chat_id_from_update(update)
        logger.warning(f"Unauthorized Telegram update rejected: chat_id={incoming_chat_id}")
        query = getattr(update, "callback_query", None)
        if query is not None:
            try:
                await query.answer("권한이 없습니다.", show_alert=True)
                return
            except Exception:
                logger.exception("Unauthorized Telegram callback reject failed")
        message = getattr(update, "effective_message", None) or getattr(update, "message", None)
        if message is not None:
            try:
                await message.reply_text("⛔ 권한이 없습니다.")
            except Exception:
                logger.exception("Unauthorized Telegram reply failed")

    async def _require_authorized_telegram_update(self, update):
        if self._is_authorized_telegram_update(update):
            return True
        await self._reject_unauthorized_telegram_update(update)
        return False

    def _telegram_owner_only(self, handler):
        async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
            if not await self._require_authorized_telegram_update(update):
                return ConversationHandler.END
            return await handler(update, context)
        return wrapped

    def _extract_emergency_action(self, text):
        normalized = str(text or "").strip()
        if not normalized:
            return None
        command = self._extract_telegram_command(normalized)
        if command in {"/stop", "/pause", "/resume"}:
            return command[1:].upper()
        normalized = re.sub(r"^[^\w/]+", "", normalized).strip()
        upper = normalized.upper()
        if upper in {"STOP", "PAUSE", "RESUME"}:
            return upper
        return None

    def _build_manual_status_payload(self):
        all_data = self.status_data if isinstance(self.status_data, dict) else {}
        return self._build_dashboard_messages(
            all_data,
            "●",
            " [PAUSED]" if self.is_paused else ""
        )

    def _get_utbot_filter_pack(self, strategy_params=None):
        params = strategy_params
        if not isinstance(params, dict):
            params = self.cfg.get('signal_engine', {}).get('strategy_params', {})
        utbot_cfg = params.get('UTBot', {}) if isinstance(params, dict) else {}
        return normalize_utbot_filter_pack_config(utbot_cfg.get('filter_pack', {}))

    def _format_utbot_filter_pack_selected(self, selected):
        return format_utbot_filter_pack_selected(selected)

    def _format_utbot_filter_pack_logic(self, logic):
        return format_utbot_filter_pack_logic(logic)

    def _format_utbot_filter_pack_mode_map(self, mode_by_filter, selected=None):
        return format_utbot_filter_pack_mode_map(mode_by_filter, selected)

    def _get_utbot_filter_label(self, filter_id):
        return get_utbot_filter_pack_label(filter_id)

    def _normalize_utsmc_candidate_filter_mode(self, mode):
        mode_raw = str(mode or 'off').strip().lower()
        aliases = {
            '0': 'off',
            'off': 'off',
            'none': 'off',
            '1': 'candidate1',
            'c1': 'candidate1',
            'candidate1': 'candidate1',
            '2': 'candidate2',
            'c2': 'candidate2',
            'candidate2': 'candidate2',
            '3': 'candidate12',
            '12': 'candidate12',
            'c12': 'candidate12',
            'candidate12': 'candidate12',
            'both': 'candidate12'
        }
        return aliases.get(mode_raw, 'off')

    def _format_utsmc_candidate_filter_mode(self, mode):
        normalized = self._normalize_utsmc_candidate_filter_mode(mode)
        return {
            'off': 'OFF',
            'candidate1': 'C1',
            'candidate2': 'C2',
            'candidate12': 'C1+C2'
        }.get(normalized, 'OFF')

    async def _sync_signal_protection_orders(self):
        """현재 보유 포지션의 보호주문(TP/SL)을 최신 설정으로 동기화한다."""
        try:
            if self.is_upbit_mode():
                logger.info("Protection sync skipped: Upbit spot mode does not use futures TP/SL orders.")
                return

            strategy_params = self.get_active_strategy_params()
            active_strategy = str(strategy_params.get('active_strategy', '') or '').lower()
            common_cfg = self.get_active_common_settings()
            tp_master_enabled = bool(common_cfg.get('tp_sl_enabled', True))
            tp_enabled = tp_master_enabled and bool(common_cfg.get('take_profit_enabled', True))
            sl_enabled = tp_master_enabled and bool(common_cfg.get('stop_loss_enabled', True))

            lev = max(1.0, float(common_cfg.get('leverage', 10) or 10))
            tp_pct = float(common_cfg.get('target_roe_pct', 20.0) or 0.0) / 100.0 / lev
            sl_pct = float(common_cfg.get('stop_loss_pct', 10.0) or 0.0) / 100.0 / lev

            positions = await asyncio.to_thread(self.exchange.fetch_positions)
            refreshed = 0
            cancelled_only = 0

            for p in positions:
                contracts = abs(float(p.get('contracts', 0) or 0))
                if contracts <= 0:
                    continue

                raw_symbol = str(p.get('symbol', ''))
                symbol = raw_symbol.replace(':USDT', '')
                side = str(p.get('side', '')).lower()
                if side not in ('long', 'short'):
                    continue

                if active_strategy in UTBREAKOUT_STRATEGIES:
                    await self._audit_protection_orders(
                        symbol,
                        pos=p,
                        expected_tp=True,
                        expected_sl=True,
                        alert=True
                    )
                    refreshed += 1
                    logger.info(
                        f"Protection sync audited UTBreak position without overwriting partial TP/trailing SL: {symbol}"
                    )
                    continue

                try:
                    await asyncio.to_thread(self.exchange.cancel_all_orders, symbol)
                except Exception as cancel_e:
                    logger.warning(f"Protection sync: cancel_all_orders failed for {symbol}: {cancel_e}")

                if not (tp_enabled or sl_enabled):
                    cancelled_only += 1
                    continue

                entry_price = float(p.get('entryPrice') or p.get('markPrice') or 0.0)
                if entry_price <= 0:
                    logger.warning(f"Protection sync skipped for {symbol}: invalid entry price")
                    continue

                close_side = 'sell' if side == 'long' else 'buy'
                try:
                    qty = self.exchange.amount_to_precision(symbol, contracts)
                except Exception:
                    qty = str(round(contracts, 6))

                tp_distance = (entry_price * tp_pct) if (tp_enabled and tp_pct > 0) else None
                sl_distance = (entry_price * sl_pct) if (sl_enabled and sl_pct > 0) else None
                if tp_distance is not None or sl_distance is not None:
                    await self._place_tp_sl_orders(
                        symbol,
                        side,
                        entry_price,
                        qty,
                        tp_distance=tp_distance,
                        sl_distance=sl_distance
                    )
                refreshed += 1

            logger.info(
                f"Protection sync done: refreshed={refreshed}, "
                f"cancelled_only={cancelled_only}, tp={tp_enabled}, sl={sl_enabled}"
            )
        except Exception as e:
            logger.error(f"Protection order sync error: {e}")

    def _build_setup_keyboard(self):
        return ReplyKeyboardMarkup(
            [
                [KeyboardButton("거래소/네트워크 전환")],
                [KeyboardButton("나가기")],
            ],
            one_time_keyboard=True,
            resize_keyboard=True
        )

    def _build_setup_network_keyboard(self):
        return ReplyKeyboardMarkup(
            [
                [KeyboardButton("1. 바이낸스 테스트넷")],
                [KeyboardButton("2. 바이낸스 메인넷")],
                [KeyboardButton("3. 업비트 KRW 현물")],
                [KeyboardButton("나가기")],
            ],
            one_time_keyboard=True,
            resize_keyboard=True
        )

    def _normalize_setup_choice_text(self, text):
        normalized = str(text or '').strip().lower()
        if normalized in {'0', '나가기', '종료', '취소', 'cancel', 'exit', 'quit'}:
            return '0'
        if normalized in {'22', '거래소/네트워크 전환', '거래소 네트워크 전환', '거래소 전환', '네트워크 전환'}:
            return '22'
        return normalized

    def _normalize_setup_network_choice(self, text):
        normalized = str(text or '').strip().lower()
        compact = normalized.replace(' ', '')
        if compact.startswith('1') or '테스트넷' in compact or 'testnet' in compact:
            return '1'
        if compact.startswith('2') or '메인넷' in compact or 'mainnet' in compact:
            return '2'
        if compact.startswith('3') or '업비트' in compact or 'upbit' in compact:
            return '3'
        return normalized

    async def show_setup_menu(self, update: Update):
        sys_cfg = self.cfg.get('system_settings', {})
        sig = self.cfg.get('signal_engine', {})
        up_cfg = self.cfg.get('upbit', {})
        sha = self.cfg.get('shannon_engine', {})
        dt = self.cfg.get('dual_thrust_engine', {})
        dm = self.cfg.get('dual_mode_engine', {})
        eng = sys_cfg.get('active_engine', CORE_ENGINE)
        request_text = ""
        if update and update.message and update.message.text:
            request_text = update.message.text.strip()
        logger.info(
            f"Telegram setup menu rendering: chat_id={update.effective_chat.id if update and update.effective_chat else 'unknown'} "
            f"text={request_text!r} engine={eng} exchange={self.get_exchange_mode()}"
        )
        network_status = self.get_network_status_label()
        msg = f"""
🔧 **설정 메뉴** (번호 입력)

/setup은 이제 거래소/네트워크 전환만 남겼습니다.

22. 거래소/네트워크 전환 (`{network_status}`)
0. 나가기
"""
        await self._reply_markdown_safe(
            update.message,
            msg.strip(),
            reply_markup=self._build_setup_keyboard()
        )
        return
        direction = self.get_effective_trade_direction()
        watchlist = sig.get('watchlist', ['BTC/USDT'])
        if not isinstance(watchlist, list) or not watchlist:
            watchlist = ['BTC/USDT']

        if eng == 'shannon':
            lev = sha.get('leverage', 5)
            symbol = sha.get('target_symbol', 'BTC/USDT')
        elif eng == 'dualthrust':
            lev = dt.get('leverage', 5)
            symbol = dt.get('target_symbol', 'BTC/USDT')
        elif eng == 'dualmode':
            lev = dm.get('leverage', 5)
            symbol = dm.get('target_symbol', 'BTC/USDT')
        else:
            lev = sig.get('common_settings', {}).get('leverage', 20)
            symbol = watchlist[0] if watchlist else 'BTC/USDT'

        status = "🔴 OFF" if self.is_paused else "🟢 ON"
        direction_str = {'both': '양방향', 'long': '롱만', 'short': '숏만'}.get(direction, 'both')

        if self.is_upbit_mode():
            up_watchlist = up_cfg.get('watchlist', ['BTC/KRW'])
            if not isinstance(up_watchlist, list) or not up_watchlist:
                up_watchlist = ['BTC/KRW']
            up_common = up_cfg.get('common_settings', {})
            up_strategy = up_cfg.get('strategy_params', {})
            up_utbot = up_strategy.get('UTBot', {})
            up_symbol = self.format_symbol_for_display(up_watchlist[0])
            network_status = self.get_network_status_label()
            reporting_cfg = self.cfg.get('telegram', {}).get('reporting', {})
            hourly_report_status = "ON" if (
                reporting_cfg.get('periodic_reports_enabled', False)
                and reporting_cfg.get('hourly_report_enabled', False)
            ) else "OFF"

            msg = f"""
🔧 **설정 메뉴** (번호 입력)

**현재 상태**: `{eng.upper()}` | `{up_symbol}` | `UPBIT`

**거래소**
22. 거래소/네트워크 전환 (`{network_status}`)
7. 매매 방향 (`롱만 고정`)

**Upbit**
43. 업비트 코인 (`{up_symbol}`)
44. 업비트 UT Bot (`K={float(up_utbot.get('key_value', 1.0) or 1.0):.2f}` / `ATR={int(up_utbot.get('atr_period', 10) or 10)}` / `HA={'ON' if up_utbot.get('use_heikin_ashi', False) else 'OFF'}`)
45. 업비트 진입 비율 (`{up_common.get('risk_per_trade_pct', DEFAULT_RISK_PER_TRADE_PERCENT)}%`)
46. 업비트 진입 TF (`{up_common.get('entry_timeframe', up_common.get('timeframe', '1h'))}`)
47. 업비트 청산 TF (`{up_common.get('exit_timeframe', '1h')}`)
48. 업비트 일일 손실 제한 (`₩{float(up_common.get('daily_loss_limit', 50000) or 50000):,.0f}`)

**운영**
42. 시간별 리포트 (`{hourly_report_status}`)
9. 매매 시작/중지 (`{status}`)
0. 나가기
"""
            await self._reply_markdown_safe(update.message, msg.strip())
            return

        # ?덉쟾???ㅼ젙 ?묎렐
        sig_common = sig.get('common_settings', {})

        # SMA ?ㅼ젙 媛?몄삤湲?
        sma_params = sig.get('strategy_params', {}).get('Triple_SMA', {})
        fast_sma = sma_params.get('fast_sma', 2)
        slow_sma = sma_params.get('slow_sma', 10)

        # Shannon ?ㅼ젙
        shannon_ratio = int(sha.get('asset_allocation', {}).get('target_ratio', 0.5) * 100)
        grid_enabled = "ON" if sha.get('grid_trading', {}).get('enabled', False) else "OFF"
        grid_size = sha.get('grid_trading', {}).get('order_size_usdt', 200)

        # Dual Thrust ?ㅼ젙
        dt_n = dt.get('n_days', 4)
        dt_k1 = dt.get('k1', 0.5)
        dt_k2 = dt.get('k2', 0.5)

        # Dual Mode ?ㅼ젙
        dm_mode = dm.get('mode', 'standard').upper()
        dm_tf = dm.get('scalping_tf', '5m') if dm_mode == 'SCALPING' else dm.get('standard_tf', '4h')

        # TP/SL ?곹깭
        tp_enabled = bool(sig_common.get('take_profit_enabled', sig_common.get('tp_sl_enabled', True)))
        sl_enabled = bool(sig_common.get('stop_loss_enabled', sig_common.get('tp_sl_enabled', True)))
        tp_sl_status = "ON" if (tp_enabled or sl_enabled) else "OFF"
        tp_status = "ON" if tp_enabled else "OFF"
        sl_status = "ON" if sl_enabled else "OFF"

        # Signal ?꾨왂 ?ㅼ젙 (異붽?)
        strategy_params = sig.get('strategy_params', {})
        active_strategy = strategy_params.get('active_strategy', 'utbot').upper()
        entry_mode = active_strategy
        ut_entry_timing_mode = str(strategy_params.get('ut_entry_timing_mode', 'next_candle') or 'next_candle').lower()
        ut_entry_timing_label = "다음봉 진입" if ut_entry_timing_mode == 'next_candle' else "신호유지 진입"
        utbot_params = strategy_params.get('UTBot', {})
        utbot_filter_pack = self._get_utbot_filter_pack(strategy_params)
        utbot_entry_filters_text = self._format_utbot_filter_pack_selected(
            utbot_filter_pack.get('entry', {}).get('selected', [])
        )
        utbot_entry_logic_text = self._format_utbot_filter_pack_logic(
            utbot_filter_pack.get('entry', {}).get('logic', 'and')
        )
        utbot_exit_filters_text = self._format_utbot_filter_pack_selected(
            utbot_filter_pack.get('exit', {}).get('selected', [])
        )
        utbot_exit_logic_text = self._format_utbot_filter_pack_logic(
            utbot_filter_pack.get('exit', {}).get('logic', 'and')
        )
        utbot_exit_mode_text = self._format_utbot_filter_pack_mode_map(
            utbot_filter_pack.get('exit', {}).get('mode_by_filter', {}),
            utbot_filter_pack.get('exit', {}).get('selected', [])
        )
        utbot_key = float(utbot_params.get('key_value', 1.0) or 1.0)
        utbot_atr = int(utbot_params.get('atr_period', 10) or 10)
        utbot_ha = "ON" if utbot_params.get('use_heikin_ashi', False) else "OFF"
        utbot_rsi_momentum_enabled = "ON" if bool(utbot_params.get('rsi_momentum_filter_enabled', False)) else "OFF"
        utsmc_cfg = strategy_params.get('UTSMC', {}) or {}
        utsmc_filter_cfg = utsmc_cfg.get('candidate_filter', {})
        utsmc_filter_mode = self._format_utsmc_candidate_filter_mode(utsmc_filter_cfg.get('mode', 'off'))
        utsmc_c2_exit_status = "ON" if bool(utsmc_cfg.get('exit_candidate2_enabled', False)) else "OFF"
        rsibb_params = strategy_params.get('RSIBB', {})
        rsibb_rsi = int(rsibb_params.get('rsi_length', 6) or 6)
        rsibb_bb = int(rsibb_params.get('bb_length', 200) or 200)
        rsibb_mult = float(rsibb_params.get('bb_mult', 2.0) or 2.0)
        rsi_momentum_params = strategy_params.get('RSIMomentumTrend', {}) or {}
        rsi_momentum_rsi = int(rsi_momentum_params.get('rsi_length', 14) or 14)
        rsi_momentum_pos = float(rsi_momentum_params.get('positive_above', 65) or 65)
        rsi_momentum_neg = float(rsi_momentum_params.get('negative_below', 32) or 32)
        rsi_momentum_ema = int(rsi_momentum_params.get('ema_period', 5) or 5)
        watchlist_preview = ", ".join(watchlist[:4]) if isinstance(watchlist, list) and watchlist else symbol
        if isinstance(watchlist, list) and len(watchlist) > 4:
            watchlist_preview += " ..."

        # Scanner ?곹깭
        scanner_enabled = sig_common.get('scanner_enabled', True)
        scanner_status = "ON 📡" if scanner_enabled else "OFF"
        scanner_tf = sig_common.get('scanner_timeframe', '15m')
        scanner_exit_tf = sig_common.get('scanner_exit_timeframe', '1h')

        # Hourly Report Status
        reporting_cfg = self.cfg.get('telegram', {}).get('reporting', {})
        hourly_report_status = "ON" if (
            reporting_cfg.get('periodic_reports_enabled', False)
            and reporting_cfg.get('hourly_report_enabled', False)
        ) else "OFF"
        alt_trend_settings = self._get_alt_trend_alert_settings()
        alt_trend_alert_status = "ON 🔔" if alt_trend_settings.get('enabled', False) else "OFF"
        alt_trend_tf_text = format_alt_trend_timeframes(alt_trend_settings.get('timeframes', []))

        # Network status
        network_status = self.get_network_status_label()

        msg = f"""
🔧 **설정 메뉴** (번호 입력)

**현재 상태**: `{eng.upper()}` | `{symbol}`

**공통**
1. 레버리지 (`{lev}x`)
2. 목표 ROE (`{sig_common.get('target_roe_pct', 20)}%` / `{tp_status}`)
3. 손절 (`{sig_common.get('stop_loss_pct', 10)}%` / `{sl_status}`)
4. 진입 타임프레임 (`{sig_common.get('timeframe', '15m')}`)
41. 청산 타임프레임 (`{sig_common.get('exit_timeframe', '4h')}`)
5. 일일 손실 제한 (`${sig_common.get('daily_loss_limit', 5000)}`)
6. 진입 비율 (`{sig_common.get('risk_per_trade_pct', DEFAULT_RISK_PER_TRADE_PERCENT)}%`)
7. 매매 방향 (`{direction_str}`)
8. 심볼 변경 (`{symbol}`)
38. 감시 심볼 추가 (`{watchlist_preview}`)

**Signal**
16. 전략 (`{active_strategy}`)
19. UT Bot (`K={utbot_key:.2f}` / `ATR={utbot_atr}` / `HA={utbot_ha}`)
20. RSI+BB 보조설정 (`RSI={rsibb_rsi}` / `BB={rsibb_bb}` / `x{rsibb_mult:.2f}`)
57. RSI Momentum Trend (`RSI={rsi_momentum_rsi}` / `P>{rsi_momentum_pos:.0f}` / `N<{rsi_momentum_neg:.0f}` / `EMA={rsi_momentum_ema}`)
58. UTBot + RSI Momentum (`{utbot_rsi_momentum_enabled}`)
21. UT 전략 안내 (`UTBOT / UTRSI / UTBB / UTRSIBB / UTSMC / RSIBB`)
51. UTBot 진입 필터 (`{utbot_entry_filters_text}`)
52. UTBot 진입 결합모드 (`{utbot_entry_logic_text}`)
53. UTBot 청산 필터 (`{utbot_exit_filters_text}`)
54. UTBot 청산 결합모드 (`{utbot_exit_logic_text}`)
55. UTBot 청산 필터 타입 (`{utbot_exit_mode_text}`)
56. UTBot 필터 안내
26. UT 진입 방식 (`{ut_entry_timing_label}`)
49. UTSMC 후보 필터 (`{utsmc_filter_mode}`)
50. UTSMC C2 청산 (`{utsmc_c2_exit_status}`)
13. TP/SL 자동청산 (`{tp_sl_status}`)
36. ROE 자동청산 (`{tp_status}`)
37. 손절 자동청산 (`{sl_status}`)
23. 거래량 급등 채굴 (`{scanner_status}` / `{scanner_tf}`)
24. 채굴 진입 TF
25. 채굴 청산 TF (`{scanner_exit_tf}`)
59. 알트 상승추세 알림 (`{alt_trend_alert_status}`)
60. 알트 상승추세 알림 TF (`{alt_trend_tf_text}`)
61. 알트 상승추세 알림 안내

**기타**
22. 거래소/네트워크 전환 (`{network_status}`)
42. 시간별 리포트 (`{hourly_report_status}`)
00. 엔진 교체 (현재: `{eng.upper()}`)
9. 매매 시작/중지 (`{status}`)
0. 나가기
"""
        await self._reply_markdown_safe(update.message, msg.strip())

    async def setup_entry(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._require_authorized_telegram_update(update):
            return ConversationHandler.END

        request_text = ""
        if update and update.message and update.message.text:
            request_text = update.message.text.strip()
        chat_id = update.effective_chat.id if update and update.effective_chat else 'unknown'
        logger.info(f"Telegram setup menu requested: chat_id={chat_id} text={request_text!r}")
        try:
            await self.show_setup_menu(update)
            return SELECT
        except Exception as e:
            logger.exception(f"Telegram setup menu render failed: chat_id={chat_id} text={request_text!r}")
            if update and update.message:
                await update.message.reply_text(f"❌ 설정 메뉴 표시 실패: {e}")
            return ConversationHandler.END

    async def setup_network_entry(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Conversation 상태가 끊겨도 거래소/네트워크 전환 버튼을 바로 처리한다."""
        if not await self._require_authorized_telegram_update(update):
            return ConversationHandler.END

        context.user_data['setup_choice'] = '22'
        await update.message.reply_text(
            "📝 **거래소/네트워크 선택**\n"
            "1=바이낸스 테스트넷\n"
            "2=바이낸스 메인넷\n"
            "3=업비트 KRW 현물",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=self._build_setup_network_keyboard()
        )
        return INPUT

    async def setup_select(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        raw_text = update.message.text.strip()
        text = self._normalize_setup_choice_text(raw_text)

        if text == '0':
            await update.message.reply_text("✅ 설정 종료", reply_markup=ReplyKeyboardRemove())
            await self._restore_main_keyboard(update)
            return ConversationHandler.END

        if text != '22':
            await update.message.reply_text("ℹ️ 현재 /setup은 거래소/네트워크 전환(22)만 지원합니다.")
            await self.show_setup_menu(update)
            return SELECT

        context.user_data['setup_choice'] = text

        prompts = {
            '1': "📝 **레버리지** 값을 입력하세요 (1~20, 예: 5)",
            '2': "📝 **목표 ROE 설정**: 값(%) 또는 ON/OFF 입력 (예: 20, on, off)",
            '3': "📝 **손절 설정**: 값(%) 또는 ON/OFF 입력 (예: 5, on, off)",
            '4': "📝 **진입 타임프레임** 입력 (예: 15m)\n1m,2m,3m,5m,15m,30m | 1h,2h,4h | 1d",
            '41': "📝 **청산 타임프레임** 입력 (예: 1h)\n1m,2m,3m,5m,15m,30m | 1h,2h,4h | 1d",
            '5': "📝 **일일 손실 제한($)** 입력 (예: 1000)",
            '6': f"📝 **진입 비율(%)** 입력 ({DEFAULT_MIN_RISK_PER_TRADE_PERCENT:.2f}~{DEFAULT_MAX_RISK_PER_TRADE_PERCENT:.2f}, 예: {DEFAULT_RISK_PER_TRADE_PERCENT:.2f})",
            '7': "↕️ **매매 방향** 선택 (양방향/롱만/숏만)",
            '8': "💱 **심볼** 입력 또는 선택\n1: BTC  2: ETH  3: SOL\n또는 직접 입력 (예: DOGE, XRP, PEPE)",
            '38': "➕ **감시 심볼 추가**\n1: BTC  2: ETH  3: SOL\n또는 직접 입력 (예: DOGE, XRP, PEPE)",
            '9': "▶️ 매매 시작/중지를 바꾸려면 1 또는 0 입력",
            '11': "📝 **자산 비율(%)** 입력 (예: 50)",
            '12': "📝 **Grid 설정** 입력 (예: on,200 또는 off)",
            '14': "📝 **N Days** 입력 (예: 4)",
            '15': "📝 **K1/K2** 입력 (예: 0.5,0.5)",
            '16': "📝 **전략 선택** (1=UTBOT, 2=UTRSIBB, 3=UTRSI, 4=UTBB, 5=UTSMC, 6=RSIBB)\nRSIBB는 순수 RSI Bollinger mean-reversion이며 기본 안전장치로 비활성/paper-only입니다.",
            '19': "📝 **UT Bot 설정** 입력 (형식: key,atr,on/off 예: 1,10,off)",
            '20': "RSI/BB 보조설정 입력\n형식: RSI길이,BB길이,BB배수\n예: 6,200,2",
            '21': "ℹ️ **UT 전략 안내**: UTRSI/UTRSIBB는 조합형, UTBB는 비대칭 롱/숏 규칙, UTSMC는 26번 UT 진입 방식 + internal OB/UT 신호봉 무효화 청산을 사용합니다.\n- RSIBB: 순수 RSI Bollinger mean-reversion입니다. 기본값은 rsibb_enabled=False / paper_only=True라 안전장치 해제 전 진입하지 않습니다.\n- 57번: RSI Momentum Trend 파라미터 설정\n- 58번: UTBot에 RSI Momentum Trend 보조필터 ON/OFF\n- 49번: UTSMC 진입용 후보 필터(C1/C2)\n- 50번: 필요 시 exit TF 기준 반대 방향 C2를 보조 청산으로 OR 추가",
            '51': "📝 **UTBot 진입 필터** 입력\n`0/off` = OFF\n또는 `1,4,5` 형식으로 번호 조합 입력\n1=CHOP, 2=ADX+DMI, 3=VWAP, 4=HTF Supertrend, 5=BOS/CHoCH",
            '52': "📝 **UTBot 진입 결합모드** 입력\n`and` 또는 `or`",
            '53': "📝 **UTBot 청산 필터** 입력\n`0/off` = OFF\n또는 `2,3` 형식으로 번호 조합 입력\n1=CHOP, 2=ADX+DMI, 3=VWAP, 4=HTF Supertrend, 5=BOS/CHoCH",
            '54': "📝 **UTBot 청산 결합모드** 입력\n`and` 또는 `or`",
            '55': "📝 **UTBot 청산 필터 타입** 입력\n형식: `2:c,3:s,5:c`\n`c=confirm`, `s=signal`",
            '56': "ℹ️ **UTBot 필터 안내**: 1=CHOP, 2=ADX+DMI, 3=VWAP, 4=HTF Supertrend, 5=BOS/CHoCH\n- 진입: 선택한 필터를 52번 AND/OR로 결합\n- 청산: 55번에서 각 필터를 `confirm`(기본 UT 반대신호 확인용) 또는 `signal`(단독 청산 트리거)로 지정\n- 58번 RSI Momentum Trend는 일반 필터팩과 별도로, UTBot 진입/청산을 직접 확인하는 전용 보조필터\n- 예시: 진입 `1,4,5` / 청산 `2,3` / 타입 `2:c,3:s`",
            '57': "📝 **RSI Momentum Trend 설정** 입력\n형식: RSI길이,PositiveAbove,NegativeBelow,EMA길이\n예: 14,65,32,5",
            '58': "📝 **UTBot + RSI Momentum Trend 보조필터** 입력\n`on/off` 또는 `1/0` (`true/false`, `yes/no` 지원)\n- on: UTBot 진입은 둘 다 같은 방향일 때만, 청산/반전은 둘 다 반대 방향일 때만 실행\n- off: 순수 UTBot만 사용",
            '59': "📝 **알트 상승추세 알림** 입력\n`on/off` 또는 `1/0` (`true/false`, `yes/no` 지원)",
            '60': "📝 **알트 상승추세 알림 TF** 입력\n쉼표로 여러 개 선택 가능\n예: `5m,15m,30m,1h,2h,4h,6h,8h,1d`\n허용 TF: 5m, 15m, 30m, 1h, 2h, 4h, 6h, 8h, 1d",
            '61': "ℹ️ **알트 상승추세 알림 안내**\n- 선택한 시간프레임만 스캔합니다.\n- 여러 TF를 선택하면 텔레그램 한 메시지 안에 TF별 섹션으로 묶어 보냅니다.\n- 준비(setup)와 확정(confirm)을 함께 보여줍니다.\n- Binance OI/CVD가 둘 다 양수일 때만 최종 알림에 포함됩니다.",
            '26': "📝 **UT 진입 방식** 입력\n`next` 또는 `1` = 시그널 마감봉 바로 다음봉에만 진입\n`persistent` 또는 `2` = fresh signal이 아니어도 현재 유지 중인 시그널이면 즉시 진입 대기/진입 (기준봉은 마지막 시그널 확정봉)",
            '49': "📝 **UTSMC 후보 필터** 입력\n`0/off` = OFF\n`1/c1` = 후보1 (Squeeze)\n`2/c2` = 후보2 (Breakout)\n`3/c12` = 후보1+2",
            '50': "📝 **UTSMC C2 청산** 입력\n`on/off` 또는 `1/0` (`true/false`, `yes/no` 지원)",
            '22': "📝 **거래소/네트워크 선택** (1=바이낸스 테스트넷, 2=바이낸스 메인넷, 3=업비트 KRW 현물)",
            '23': "📝 **거래량 급등 채굴 기능** (1=ON, 0=OFF)",
            '24': "📝 **채굴 진입 타임프레임** 입력 (예: 5m)\n1m, 5m, 15m, 30m, 1h",
            '25': "📝 **채굴 청산 타임프레임** 입력 (예: 1h)\n1m, 5m, 15m, 30m, 1h, 4h",
            '35': "📝 **듀얼모드 변경** (1=스탠다드, 2=스캘핑)",
            '36': "📝 **ROE 자동청산** ON/OFF 토글",
            '37': "📝 **손절 자동청산** ON/OFF 토글",
            '43': "📝 **업비트 코인** 입력\n예: BTC, XRP, KRW-BTC, BTC/KRW",
            '44': "📝 **업비트 UT Bot 설정** 입력 (형식: key,atr,on/off 예: 1,10,off)",
            '45': f"📝 **업비트 진입 비율(%)** 입력 ({DEFAULT_MIN_RISK_PER_TRADE_PERCENT:.2f}~{DEFAULT_MAX_RISK_PER_TRADE_PERCENT:.2f}, 예: {DEFAULT_RISK_PER_TRADE_PERCENT:.2f})",
            '46': "📝 **업비트 진입 타임프레임** 입력 (예: 1h)\n1m,3m,5m,15m,30m | 1h,4h | 1d",
            '47': "📝 **업비트 청산 타임프레임** 입력 (예: 1h)\n1m,3m,5m,15m,30m | 1h,4h | 1d",
            '48': "📝 **업비트 일일 손실 제한(KRW)** 입력 (예: 50000)",
        }
        if text == '22':
            await update.message.reply_text(
                prompts['22'],
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=self._build_setup_network_keyboard()
            )
            return INPUT
        if self.is_upbit_mode():
            blocked_choices = {
                '1', '2', '3', '4', '5', '6', '8', '10', '11', '12', '13', '14', '15',
                '16', '19', '20', '21', '23', '24', '25', '26', '35', '36', '37', '38', '41', '49', '50', '51', '52', '53', '54', '55', '56', '57', '58', '59', '60', '61', '00'
            }
            if text in blocked_choices:
                await update.message.reply_text("ℹ️ 업비트 모드에서는 업비트 전용 메뉴(22, 43~48)만 사용합니다.")
                await self.show_setup_menu(update)
                return SELECT
        elif text in {'43', '44', '45', '46', '47', '48'}:
            await update.message.reply_text("ℹ️ 업비트 전용 메뉴입니다. 먼저 22번에서 업비트 KRW 현물로 전환하세요.")
            await self.show_setup_menu(update)
            return SELECT
        if text == '7':
            if self.is_upbit_mode():
                await update.message.reply_text("ℹ️ 업비트 현물은 숏/레버리지가 없어 `롱만`으로 고정됩니다.")
                await self.show_setup_menu(update)
                return SELECT
            keyboard = [
                [KeyboardButton("양방향 (Long+Short)")],
                [KeyboardButton("롱만 (Long Only)")],
                [KeyboardButton("숏만 (Short Only)")]
            ]
            await update.message.reply_text(
                "📝 **매매 방향** 선택:",
                reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True),
                parse_mode=ParseMode.MARKDOWN
            )
            return DIRECTION_SELECT
        elif text == '00':
            msg = """
🔀 **엔진 교체**

현재 코어 모드에서는 아래만 사용합니다.

1. **Signal Engine**
"""
            await update.message.reply_text(msg.strip(), parse_mode=ParseMode.MARKDOWN)
            return ENGINE_SELECT
        elif text == '56':
            await update.message.reply_text(prompts['56'], parse_mode=ParseMode.MARKDOWN)
            await self.show_setup_menu(update)
            return SELECT
        elif text == '61':
            await update.message.reply_text(prompts['61'], parse_mode=ParseMode.MARKDOWN)
            await self.show_setup_menu(update)
            return SELECT

        elif text == '9':
            self.is_paused = not self.is_paused
            await self.show_setup_menu(update)
            return SELECT
        elif text == '13':
            # TP/SL ?좉?
            common_cfg = self.cfg.get('signal_engine', {}).get('common_settings', {})
            current = bool(common_cfg.get('tp_sl_enabled', True))
            new_val = not current
            await self.cfg.update_value(['signal_engine', 'common_settings', 'tp_sl_enabled'], new_val)
            await self.cfg.update_value(['signal_engine', 'common_settings', 'take_profit_enabled'], new_val)
            await self.cfg.update_value(['signal_engine', 'common_settings', 'stop_loss_enabled'], new_val)
            await self._sync_signal_protection_orders()
            status = "ON" if new_val else "OFF"
            await update.message.reply_text(f"✅ TP/SL 자동청산(전체): {status}")
            await self.show_setup_menu(update)
            return SELECT
        elif text == '36':
            common_cfg = self.cfg.get('signal_engine', {}).get('common_settings', {})
            current_tp = bool(common_cfg.get('take_profit_enabled', common_cfg.get('tp_sl_enabled', True)))
            new_tp = not current_tp
            curr_sl = bool(common_cfg.get('stop_loss_enabled', common_cfg.get('tp_sl_enabled', True)))
            await self.cfg.update_value(['signal_engine', 'common_settings', 'take_profit_enabled'], new_tp)
            await self.cfg.update_value(['signal_engine', 'common_settings', 'tp_sl_enabled'], bool(new_tp or curr_sl))
            await self._sync_signal_protection_orders()
            await update.message.reply_text(f"✅ ROE 자동청산: {'ON' if new_tp else 'OFF'}")
            await self.show_setup_menu(update)
            return SELECT
        elif text == '37':
            common_cfg = self.cfg.get('signal_engine', {}).get('common_settings', {})
            current_sl = bool(common_cfg.get('stop_loss_enabled', common_cfg.get('tp_sl_enabled', True)))
            new_sl = not current_sl
            curr_tp = bool(common_cfg.get('take_profit_enabled', common_cfg.get('tp_sl_enabled', True)))
            await self.cfg.update_value(['signal_engine', 'common_settings', 'stop_loss_enabled'], new_sl)
            await self.cfg.update_value(['signal_engine', 'common_settings', 'tp_sl_enabled'], bool(curr_tp or new_sl))
            await self._sync_signal_protection_orders()
            await update.message.reply_text(f"✅ 손절 자동청산: {'ON' if new_sl else 'OFF'}")
            await self.show_setup_menu(update)
            return SELECT
        elif text == '42':
            # Hourly Report Toggle
            reporting = self.cfg.get('telegram', {}).get('reporting', {})
            curr = bool(
                reporting.get('periodic_reports_enabled', False)
                and reporting.get('hourly_report_enabled', False)
            )
            new_val = not curr
            await self.cfg.update_value(['telegram', 'reporting', 'hourly_report_enabled'], new_val)
            await self.cfg.update_value(
                ['telegram', 'reporting', 'periodic_reports_enabled'],
                bool(new_val or reporting.get('alt_trend_alert_enabled', False))
            )
            status = "ON" if new_val else "OFF"
            await update.message.reply_text(f"⚙️ 시간별 리포트: {status}")
            await self.show_setup_menu(update)
            return SELECT
        elif text in ('28', '29'):
            await update.message.reply_text("ℹ️ Hurst 필터는 코드에서 제거되었습니다.")
            return SELECT

        elif text == '21':
            await update.message.reply_text(
                "ℹ️ UT Hybrid 전략 안내\n"
                "- 26번 UT 진입 방식: `다음봉 진입`은 시그널 마감 직후 다음 진행봉에서만 진입, `신호유지 진입`은 fresh signal이 아니어도 현재 유지 중인 시그널이면 즉시 진입 대기/진입하고 기준봉은 마지막 시그널 확정봉\n"
                "- UTRSI: UT 방향 + RSI 타이밍 조합\n"
                "- UTRSIBB: UT 방향 + RSI/BB 동시 타이밍 조합\n"
                "- RSIBB: UT 없는 순수 RSI Bollinger mean-reversion입니다. 기본 안전장치(rsibb_enabled=False, paper_only=True) 해제 전에는 진입하지 않습니다.\n"
                "- 57번 RSI Momentum Trend: Positive/Negative 상태 계산 파라미터 설정\n"
                "- 58번 UTBot + RSI Momentum: UTBot 진입은 RSI Momentum Trend와 같은 방향일 때만, 청산/반전은 둘 다 반대 방향일 때만 실행\n"
                "- UTBB LONG: UT 롱신호와 BB 롱신호가 순서 무관 3봉 내 일치하면 진입, 중간에 UT 숏 또는 BB 숏이 나오면 저장 무효\n"
                "- UTBB LONG 청산: 일반롱은 UT 숏상태 또는 BB 상단 하향 돌파 또는 중간선 위 음봉 2개, 특수롱은 BB 상단 하향 돌파 또는 UT 숏상태\n"
                "- UTBB 특수 LONG: UT 롱상태에서 BB 상단 상향 돌파가 나오면 진입\n"
                "- UTBB SHORT: BB 상단 재진입 셋업 후 UT 숏신호, BB 중간선 하락 돌파 + UT 숏상태, 또는 중간선 아래 반등 실패 + UT 숏상태로 진입\n"
                "- UTBB 특수 SHORT(하단돌파형): 진입봉이 BB 중간선 하향 + BB 하단선 하향 돌파면 BB 하단선 상향 돌파 또는 UT 롱상태로 청산\n"
                "- UTBB 특수 SHORT(반등실패형): 중간선 아래 양봉 1~2개 뒤 저점 이탈 음봉이면 진입, BB 중간선 상향 돌파 또는 UT 롱상태로 청산\n"
                "- UTBB SHORT 청산: 일반은 UT 롱상태 또는 BB 하단선 하향 돌파\n"
                "- UTSMC: 선택한 진입 방식대로 진입, `신호유지 진입`이면 현재 유지 중인 UT 상태에서도 바로 진입 가능하며 기준봉은 마지막 UT 시그널 확정봉, 청산은 exit TF internal OB 또는 UT 신호봉 무효화 마감 기준\n"
                "- UTSMC LONG: UT 롱신호 확정 후 LONG 진입, internal bearish OB 진입 마감 또는 UT 신호봉 저가 이탈 마감 청산\n"
                "- UTSMC SHORT: UT 숏신호 확정 후 SHORT 진입, internal bullish OB 진입 마감 또는 UT 신호봉 고가 돌파 마감 청산\n"
                "- 모든 판단은 확정봉 기준"
            )
            await self.show_setup_menu(update)
            return SELECT

        elif text == '24':
            await update.message.reply_text(prompts['24'])
            return INPUT

        elif text in prompts:
            if text == '20':
                await update.message.reply_text(prompts[text])
            else:
                await update.message.reply_text(prompts[text], parse_mode=ParseMode.MARKDOWN)
            if text in {'8', '38', '43'}:
                return SYMBOL_INPUT
            return INPUT
        else:
            await update.message.reply_text("❌ 잘못된 번호입니다.")
            return SELECT

    async def handle_manual_symbol_input(self, update: Update, symbol: str):
        """Telegram manual symbol input handler."""
        try:
            mode = self.get_exchange_mode()
            markets = await self._load_trade_markets_for_exchange_mode(mode)
            symbol = self._resolve_watch_symbol_for_exchange_mode(symbol, markets, exchange_mode=mode)

            # ?щ낵 ?좏슚??寃??(Exchange check)
            # SignalEngine???쒖꽦?붾릺???덉뼱????
            eng_type = self.cfg.get('system_settings', {}).get('active_engine', CORE_ENGINE)
            if eng_type != 'signal':
                await update.message.reply_text("⚠️ 현재 Signal 엔진이 활성화되어 있지 않습니다. (`/strat 1`)")
                return

            signal_engine = self.engines.get('signal')
            if not signal_engine:
                await update.message.reply_text("❌ Signal 엔진을 찾을 수 없습니다.")
                return

            # ?щ낵 寃利?(exchange load_markets ?꾩슂?????덉쓬, ?ш린??try fetch ticker濡??泥?
            try:
                await asyncio.to_thread(self.exchange.fetch_ticker, symbol)
            except Exception:
                await update.message.reply_text(f"❌ 유효하지 않은 심볼입니다: `{symbol}`", parse_mode=ParseMode.MARKDOWN)
                return

            display_symbol = self.format_symbol_for_display(symbol)
            if self.is_upbit_mode():
                await self._update_config_value(['upbit', 'watchlist'], [symbol])
                await self._update_config_value(['exchange_watchlists', UPBIT_MODE], [symbol])
                signal_engine.active_symbols.clear()
                signal_engine.active_symbols.add(symbol)
                self._reset_signal_engine_runtime_state(
                    reset_entry_cache=True,
                    reset_exit_cache=True,
                    reset_stateful_strategy=True
                )
                await signal_engine.prime_symbol_to_next_closed_candle(symbol)
                await update.message.reply_text(f"✅ 업비트 코인 변경: `{display_symbol}`", parse_mode=ParseMode.MARKDOWN)
                logger.info(f"Upbit manual symbol set: {symbol}")
            else:
                # Active Symbols??異붽?
                if symbol not in signal_engine.active_symbols:
                    watchlist = self.get_active_watchlist()
                    if symbol not in watchlist:
                        watchlist = watchlist + [symbol]
                        await self._update_config_value(['signal_engine', 'watchlist'], watchlist)
                        await self._update_config_value(['exchange_watchlists', mode], watchlist)
                    signal_engine.active_symbols.add(symbol)
                    await signal_engine.prime_symbol_to_next_closed_candle(symbol)
                    await update.message.reply_text(f"✅ **{display_symbol}** 감시 시작 (수동 추가)", parse_mode=ParseMode.MARKDOWN)
                    logger.info(f"Manual symbol added: {symbol}")
                else:
                    await update.message.reply_text(f"ℹ️ 이미 감시 중인 심볼입니다: `{display_symbol}`", parse_mode=ParseMode.MARKDOWN)

        except Exception as e:
            logger.error(f"Manual input error: {e}")
            await update.message.reply_text(f"❌ 처리 실패: {e}")

    async def setup_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        choice = context.user_data.get('setup_choice')
        val = update.message.text

        try:
            removed_strategy_filter_choices = {'10', '17', '18', '27', '30', '31', '32', '33', '34'}
            if choice in removed_strategy_filter_choices:
                await update.message.reply_text("ℹ️ 해당 전략/필터 항목은 현재 코어 UT 설정에서 제거되었습니다.")
                await self.show_setup_menu(update)
                return SELECT

            if choice == '1':
                v = int(val)
                # ?덈쾭由ъ? 理쒕? 20諛??쒗븳 (?ъ슜???붿껌: 5 -> 20)
                if v < 1 or v > 20:
                    await update.message.reply_text("❌ 레버리지는 1~20 사이 값만 가능합니다.")
                    return SELECT
                await self.cfg.update_value(['signal_engine', 'common_settings', 'leverage'], v)
                await self.cfg.update_value(['shannon_engine', 'leverage'], v)
                await self.cfg.update_value(['dual_thrust_engine', 'leverage'], v)
                await self.cfg.update_value(['dual_mode_engine', 'leverage'], v)
                # TEMA??common_settings瑜?李몄“?섎?濡?蹂꾨룄 ?낅뜲?댄듃 遺덊븘?뷀븯吏留?
                # ?쒖꽦 ?붿쭊??TEMA??寃쎌슦 market settings 利됱떆 ?곸슜 ?꾩슂
                if self.active_engine:
                    sym = self._get_current_symbol()
                    await self.active_engine.ensure_market_settings(sym)
                await update.message.reply_text(f"✅ 레버리지 변경: {v}x")
            elif choice == '2':
                v_low = str(val).strip().lower()
                common_cfg = self.cfg.get('signal_engine', {}).get('common_settings', {})
                if v_low in ('on', '1', 'true'):
                    curr_sl = bool(common_cfg.get('stop_loss_enabled', common_cfg.get('tp_sl_enabled', True)))
                    await self.cfg.update_value(['signal_engine', 'common_settings', 'take_profit_enabled'], True)
                    await self.cfg.update_value(['signal_engine', 'common_settings', 'tp_sl_enabled'], bool(True or curr_sl))
                    await self._sync_signal_protection_orders()
                    await update.message.reply_text("✅ 목표 ROE 자동청산: ON")
                elif v_low in ('off', '0', 'false'):
                    curr_sl = bool(common_cfg.get('stop_loss_enabled', common_cfg.get('tp_sl_enabled', True)))
                    await self.cfg.update_value(['signal_engine', 'common_settings', 'take_profit_enabled'], False)
                    await self.cfg.update_value(['signal_engine', 'common_settings', 'tp_sl_enabled'], bool(False or curr_sl))
                    await self._sync_signal_protection_orders()
                    await update.message.reply_text("✅ 목표 ROE 자동청산: OFF")
                else:
                    v = float(val)
                    if v < 0:
                        await update.message.reply_text("❌ 목표 ROE는 0 이상으로 입력하세요.")
                        return SELECT
                    await self.cfg.update_value(['signal_engine', 'common_settings', 'target_roe_pct'], v)
                    await self.cfg.update_value(['dual_mode_engine', 'target_roe_pct'], v)
                    await self._sync_signal_protection_orders()
                    await update.message.reply_text(f"✅ 목표 ROE 변경: {v}%")
            elif choice == '3':
                v_low = str(val).strip().lower()
                common_cfg = self.cfg.get('signal_engine', {}).get('common_settings', {})
                if v_low in ('on', '1', 'true'):
                    curr_tp = bool(common_cfg.get('take_profit_enabled', common_cfg.get('tp_sl_enabled', True)))
                    await self.cfg.update_value(['signal_engine', 'common_settings', 'stop_loss_enabled'], True)
                    await self.cfg.update_value(['signal_engine', 'common_settings', 'tp_sl_enabled'], bool(curr_tp or True))
                    await self._sync_signal_protection_orders()
                    await update.message.reply_text("✅ 손절 자동청산: ON")
                elif v_low in ('off', '0', 'false'):
                    curr_tp = bool(common_cfg.get('take_profit_enabled', common_cfg.get('tp_sl_enabled', True)))
                    await self.cfg.update_value(['signal_engine', 'common_settings', 'stop_loss_enabled'], False)
                    await self.cfg.update_value(['signal_engine', 'common_settings', 'tp_sl_enabled'], bool(curr_tp or False))
                    await self._sync_signal_protection_orders()
                    await update.message.reply_text("✅ 손절 자동청산: OFF")
                else:
                    v = float(val)
                    if v < 0:
                        await update.message.reply_text("❌ 손절 비율은 0 이상으로 입력하세요.")
                        return SELECT
                    await self.cfg.update_value(['signal_engine', 'common_settings', 'stop_loss_pct'], v)
                    await self.cfg.update_value(['dual_thrust_engine', 'stop_loss_pct'], v)
                    await self.cfg.update_value(['dual_mode_engine', 'stop_loss_pct'], v)
                    await self._sync_signal_protection_orders()
                    await update.message.reply_text(f"✅ 손절 비율 변경: {v}%")
            elif choice == '4':
                # 타임프레임 유효성 검사
                valid_tf = ['1m', '2m', '3m', '4m', '5m', '15m', '30m', '1h', '2h', '4h', '6h', '8h', '12h', '1d', '3d', '1w', '1M']
                if val not in valid_tf:
                    await update.message.reply_text(f"❌ 유효하지 않은 타임프레임입니다.\n사용 가능: {', '.join(valid_tf)}")
                    return SELECT
                # 타임프레임 업데이트 (Common, Signal, Shannon 동기화)
                await self.cfg.update_value(['signal_engine', 'common_settings', 'timeframe'], val)
                await self.cfg.update_value(['signal_engine', 'common_settings', 'entry_timeframe'], val) # Sync Entry TF
                await self.cfg.update_value(['shannon_engine', 'timeframe'], val)

                # DualMode 타임프레임도 모드에 따라 동기화
                dm_mode = self.cfg.get('dual_mode_engine', {}).get('mode', 'standard')
                if dm_mode == 'scalping':
                    await self.cfg.update_value(['dual_mode_engine', 'scalping_tf'], val)
                else:
                    await self.cfg.update_value(['dual_mode_engine', 'standard_tf'], val)

                # Shannon 엔진 200 EMA 캐시 초기화 (변경 TF 즉시 반영)
                if self.active_engine and hasattr(self.active_engine, 'ema_200'):
                    self.active_engine.ema_200 = None
                    self.active_engine.trend_direction = None
                    self.active_engine.last_indicator_update = 0
                # Signal 엔진 캐시 초기화
                self._reset_signal_engine_runtime_state(
                    reset_entry_cache=True,
                    reset_stateful_strategy=True
                )
                await update.message.reply_text(f"✅ 진입 타임프레임 변경: {val}")

                # DualMode 엔진 캐시 초기화
                dm_engine = self.engines.get('dualmode')
                if dm_engine:
                    dm_engine.last_candle_ts = 0

            elif choice == '5':
                await self.cfg.update_value(['shannon_engine', 'daily_loss_limit'], float(val))
                await self.cfg.update_value(['signal_engine', 'common_settings', 'daily_loss_limit'], float(val))
                await self.cfg.update_value(['dual_thrust_engine', 'daily_loss_limit'], float(val))
                await self.cfg.update_value(['dual_mode_engine', 'daily_loss_limit'], float(val))
            elif choice == '6':
                v = float(val)
                common_settings = self.cfg.get('signal_engine', {}).get('common_settings', {})
                min_risk = float(common_settings.get('min_risk_per_trade_pct', DEFAULT_MIN_RISK_PER_TRADE_PERCENT) or 0.0)
                max_risk = float(common_settings.get('max_risk_per_trade_pct', DEFAULT_MAX_RISK_PER_TRADE_PERCENT) or DEFAULT_MAX_RISK_PER_TRADE_PERCENT)
                min_risk = max(0.0, min_risk)
                max_risk = max(min_risk, min(DEFAULT_MAX_RISK_PER_TRADE_PERCENT, max_risk))
                if v < min_risk or v > max_risk:
                    await update.message.reply_text(f"❌ {min_risk:.2f}~{max_risk:.2f}% 사이 값을 입력하세요.")
                    return SELECT
                await self.cfg.update_value(['signal_engine', 'common_settings', 'risk_per_trade_pct'], v)
                await self.cfg.update_value(['dual_thrust_engine', 'risk_per_trade_pct'], v)
                await self.cfg.update_value(['dual_mode_engine', 'risk_per_trade_pct'], v)
            elif choice == '45':
                v = float(val)
                upbit_common = self.cfg.get('upbit', {}).get('common_settings', {})
                min_risk = float(upbit_common.get('min_risk_per_trade_pct', DEFAULT_MIN_RISK_PER_TRADE_PERCENT) or 0.0)
                max_risk = float(upbit_common.get('max_risk_per_trade_pct', DEFAULT_MAX_RISK_PER_TRADE_PERCENT) or DEFAULT_MAX_RISK_PER_TRADE_PERCENT)
                min_risk = max(0.0, min_risk)
                max_risk = max(min_risk, min(DEFAULT_MAX_RISK_PER_TRADE_PERCENT, max_risk))
                if v < min_risk or v > max_risk:
                    await update.message.reply_text(f"❌ 업비트 진입 비율은 {min_risk:.2f}~{max_risk:.2f}% 사이여야 합니다.")
                    return SELECT
                await self.cfg.update_value(['upbit', 'common_settings', 'risk_per_trade_pct'], v)
                await update.message.reply_text(f"✅ 업비트 진입 비율 변경: {v}%")

            elif choice == '24':
                # Scanner Entry Timeframe
                valid_tf = ['1m', '2m', '3m', '5m', '15m', '30m', '1h', '4h']
                if val not in valid_tf:
                    await update.message.reply_text("❌ 유효하지 않은 타임프레임입니다.\n추천: 1m, 5m, 15m")
                    return SELECT
                await self.cfg.update_value(['signal_engine', 'common_settings', 'scanner_timeframe'], val)
                await update.message.reply_text(f"✅ 채굴 진입 타임프레임 변경: {val}")

            elif choice == '25':
                # Scanner Exit Timeframe
                valid_tf = ['1m', '2m', '3m', '5m', '15m', '30m', '1h', '4h']
                if val not in valid_tf:
                    await update.message.reply_text("❌ 유효하지 않은 타임프레임입니다.\n추천: 1m, 5m, 15m, 1h")
                    return SELECT
                await self.cfg.update_value(['signal_engine', 'common_settings', 'scanner_exit_timeframe'], val)
                await update.message.reply_text(f"✅ 채굴 청산 타임프레임 변경: {val}")

            # ======== Signal (SMA) ?꾩슜 ========
            elif choice == '10':
                # SMA 湲곌컙 蹂寃?(?뺤떇: "2,10" ?먮뒗 "5,25")
                parts = val.replace(' ', '').split(',')
                if len(parts) != 2:
                    await update.message.reply_text("❌ 형식: fast,slow (예: 2,10)")
                    return SELECT
                fast, slow = int(parts[0]), int(parts[1])
                if fast >= slow:
                    await update.message.reply_text("❌ fast SMA는 slow SMA보다 작아야 합니다.")
                    return SELECT
                await self.cfg.update_value(['signal_engine', 'strategy_params', 'Triple_SMA', 'fast_sma'], fast)
                await self.cfg.update_value(['signal_engine', 'strategy_params', 'Triple_SMA', 'slow_sma'], slow)
                await update.message.reply_text(f"✅ SMA 기간 변경: {fast}/{slow}")

            # ======== Shannon ?꾩슜 ========
            elif choice == '11':
                # ?먯궛 鍮꾩쑉 蹂寃?(?뺤떇: "50" = 50%)
                v = float(val)
                if v < 10 or v > 90:
                    await update.message.reply_text("❌ 10~90 사이 값을 입력하세요.")
                    return SELECT
                ratio = v / 100.0
                await self.cfg.update_value(['shannon_engine', 'asset_allocation', 'target_ratio'], ratio)
                # Shannon ?붿쭊??利됱떆 ?곸슜
                shannon_engine = self.engines.get('shannon')
                if shannon_engine:
                    shannon_engine.ratio = ratio
                await update.message.reply_text(f"✅ Shannon 자산 비율 변경: {int(v)}%")

            elif choice == '12':
                # Grid ?ㅼ젙 (?뺤떇: "on,200" ?먮뒗 "off")
                val_lower = val.lower().strip()
                if val_lower == 'off':
                    await self.cfg.update_value(['shannon_engine', 'grid_trading', 'enabled'], False)
                    await update.message.reply_text("✅ Grid Trading: OFF")
                else:
                    parts = val_lower.replace(' ', '').split(',')
                    if parts[0] == 'on' and len(parts) >= 2:
                        size = float(parts[1])
                        await self.cfg.update_value(['shannon_engine', 'grid_trading', 'enabled'], True)
                        await self.cfg.update_value(['shannon_engine', 'grid_trading', 'order_size_usdt'], size)
                        await update.message.reply_text(f"✅ Grid Trading: ON, ${size}")
                    else:
                        await update.message.reply_text("❌ 형식: on,금액 또는 off (예: on,200)")
                        return SELECT

            # ======== Dual Thrust ?꾩슜 ========
            elif choice == '14':
                # N Days 蹂寃?
                v = int(val)
                if v < 1 or v > 30:
                    await update.message.reply_text("❌ 1~30 사이 값을 입력하세요.")
                    return SELECT
                await self.cfg.update_value(['dual_thrust_engine', 'n_days'], v)
                # ?붿쭊 罹먯떆 珥덇린??(??N?쇰줈 ?몃━嫄??ш퀎??
                dt_engine = self.engines.get('dualthrust')
                if dt_engine:
                    dt_engine.trigger_date = None
                await update.message.reply_text(f"✅ Dual Thrust N Days 변경: {v}")

            elif choice == '15':
                # K1/K2 蹂寃?(?뺤떇: "0.5,0.5" ?먮뒗 "0.4,0.6")
                parts = val.replace(' ', '').split(',')
                if len(parts) != 2:
                    await update.message.reply_text("❌ 형식: k1,k2 (예: 0.5,0.5)")
                    return SELECT
                k1, k2 = float(parts[0]), float(parts[1])
                if k1 <= 0 or k1 > 1 or k2 <= 0 or k2 > 1:
                    await update.message.reply_text("❌ K1, K2는 0~1 사이 값이어야 합니다.")
                    return SELECT
                await self.cfg.update_value(['dual_thrust_engine', 'k1'], k1)
                await self.cfg.update_value(['dual_thrust_engine', 'k2'], k2)
                # ?붿쭊 罹먯떆 珥덇린??(??K濡??몃━嫄??ш퀎??
                dt_engine = self.engines.get('dualthrust')
                if dt_engine:
                    dt_engine.trigger_date = None
                await update.message.reply_text(f"✅ Dual Thrust K1/K2 변경: {k1}/{k2}")
            # ======== Signal ?좉퇋 ?듭뀡 ========
            elif choice == '16':
                # ?꾨왂 蹂寃?(踰덊샇 ?먮뒗 ?대쫫?쇰줈 ?좏깮)
                strategy_map = {
                    '1': 'utbot',
                    '2': 'utrsibb',
                    '3': 'utrsi',
                    '4': 'utbb',
                    '5': 'utsmc',
                    '6': 'rsibb'
                }
                val_lower = val.lower().strip()

                # 踰덊샇 ?낅젰 ??蹂??
                if val_lower in strategy_map:
                    val_lower = strategy_map[val_lower]

                if val_lower in UT_ONLY_STRATEGIES:
                    await self.cfg.update_value(['signal_engine', 'strategy_params', 'active_strategy'], val_lower)
                    signal_engine = self.engines.get('signal')
                    if signal_engine:
                        self._reset_signal_engine_runtime_state(
                            reset_entry_cache=True,
                            reset_exit_cache=True,
                            reset_stateful_strategy=True
                        )
                    suffix = "\n⚠️ RSIBB는 기본 rsibb_enabled=False / paper_only=True 안전장치가 적용됩니다." if val_lower == 'rsibb' else ""
                    await update.message.reply_text(f"✅ 전략 변경: {val_lower.upper()}{suffix}")
                else:
                    await update.message.reply_text(
                        "❌ 1~6 또는 utbot/utrsibb/utrsi/utbb/utsmc/rsibb를 입력하세요.\n"
                        "1=UTBOT, 2=UTRSIBB, 3=UTRSI, 4=UTBB, 5=UTSMC, 6=RSIBB"
                    )
                    return SELECT

            elif choice == '20':
                parts = val.replace(' ', '').split(',')
                if len(parts) != 3:
                    await update.message.reply_text("❌ 형식: rsi_length,bb_length,bb_mult (예: 6,200,2)")
                    return SELECT

                rsi_length = int(parts[0])
                bb_length = int(parts[1])
                bb_mult = float(parts[2])
                if rsi_length < 1 or bb_length < 2 or bb_mult <= 0:
                    await update.message.reply_text("❌ RSI 기간은 1 이상, BB 기간은 2 이상, 배수는 0보다 커야 합니다.")
                    return SELECT

                await self.cfg.update_value(['signal_engine', 'strategy_params', 'RSIBB', 'rsi_length'], rsi_length)
                await self.cfg.update_value(['signal_engine', 'strategy_params', 'RSIBB', 'bb_length'], bb_length)
                await self.cfg.update_value(['signal_engine', 'strategy_params', 'RSIBB', 'bb_mult'], bb_mult)
                self._reset_signal_engine_runtime_state(
                    reset_entry_cache=True,
                    reset_stateful_strategy=True
                )
                await update.message.reply_text(
                    f"✅ RSI+BB 설정 변경: RSI={rsi_length}, BB={bb_length}, x{bb_mult:.2f}"
                )

            elif choice == '57':
                parts = val.replace(' ', '').split(',')
                if len(parts) != 4:
                    await update.message.reply_text("❌ 형식: rsi_length,positive_above,negative_below,ema_period (예: 14,65,32,5)")
                    return SELECT

                rsi_length = int(parts[0])
                positive_above = float(parts[1])
                negative_below = float(parts[2])
                ema_period = int(parts[3])
                if rsi_length < 1 or ema_period < 1:
                    await update.message.reply_text("❌ RSI 기간과 EMA 기간은 1 이상이어야 합니다.")
                    return SELECT
                if not (0 <= negative_below < positive_above <= 100):
                    await update.message.reply_text("❌ 기준값은 `0 <= NegativeBelow < PositiveAbove <= 100` 이어야 합니다.")
                    return SELECT

                await self.cfg.update_value(['signal_engine', 'strategy_params', 'RSIMomentumTrend', 'rsi_length'], rsi_length)
                await self.cfg.update_value(['signal_engine', 'strategy_params', 'RSIMomentumTrend', 'positive_above'], positive_above)
                await self.cfg.update_value(['signal_engine', 'strategy_params', 'RSIMomentumTrend', 'negative_below'], negative_below)
                await self.cfg.update_value(['signal_engine', 'strategy_params', 'RSIMomentumTrend', 'ema_period'], ema_period)
                self._reset_signal_engine_runtime_state(
                    reset_entry_cache=True,
                    reset_exit_cache=True,
                    reset_stateful_strategy=True
                )
                await update.message.reply_text(
                    f"✅ RSI Momentum Trend 설정 변경: RSI={rsi_length}, P>{positive_above:.1f}, N<{negative_below:.1f}, EMA={ema_period}"
                )

            elif choice == '58':
                toggle_raw = str(val or '').strip().lower()
                if toggle_raw in {'1', 'on', 'true', 'yes'}:
                    enabled = True
                elif toggle_raw in {'0', 'off', 'false', 'no'}:
                    enabled = False
                else:
                    await update.message.reply_text("❌ `on/off`, `1/0`, `true/false`, `yes/no` 중 하나를 입력하세요.")
                    return SELECT

                await self.cfg.update_value(
                    ['signal_engine', 'strategy_params', 'UTBot', 'rsi_momentum_filter_enabled'],
                    enabled
                )
                self._reset_signal_engine_runtime_state(
                    reset_entry_cache=True,
                    reset_exit_cache=True,
                    reset_stateful_strategy=True
                )
                await update.message.reply_text(
                    f"✅ UTBot + RSI Momentum Trend: {'ON' if enabled else 'OFF'}"
                )

            elif choice == '59':
                toggle_raw = str(val or '').strip().lower()
                if toggle_raw in {'1', 'on', 'true', 'yes'}:
                    enabled = True
                elif toggle_raw in {'0', 'off', 'false', 'no'}:
                    enabled = False
                else:
                    await update.message.reply_text("❌ `on/off`, `1/0`, `true/false`, `yes/no` 중 하나를 입력하세요.")
                    return SELECT

                await self.cfg.update_value(
                    ['telegram', 'reporting', 'alt_trend_alert_enabled'],
                    enabled
                )
                reporting = self.cfg.get('telegram', {}).get('reporting', {})
                await self.cfg.update_value(
                    ['telegram', 'reporting', 'periodic_reports_enabled'],
                    bool(enabled or reporting.get('hourly_report_enabled', False))
                )
                self.last_alt_trend_scan_candle_ts_by_tf = {}
                self.last_alt_trend_alert_sent = {}
                self.last_alt_trend_scan_summary = {}
                await update.message.reply_text(
                    f"✅ 알트 상승추세 알림: {'ON' if enabled else 'OFF'}"
                )

            elif choice == '60':
                selected_timeframes = normalize_alt_trend_timeframes(val)
                if not selected_timeframes:
                    await update.message.reply_text(
                        "❌ 최소 1개 이상 입력하세요.\n허용 TF: 5m, 15m, 30m, 1h, 2h, 4h, 6h, 8h, 1d"
                    )
                    return SELECT

                await self.cfg.update_value(
                    ['telegram', 'reporting', 'alt_trend_alert_timeframes'],
                    selected_timeframes
                )
                self.last_alt_trend_scan_candle_ts_by_tf = {}
                self.last_alt_trend_alert_sent = {}
                self.last_alt_trend_scan_summary = {}
                await update.message.reply_text(
                    f"✅ 알트 상승추세 알림 TF: {format_alt_trend_timeframes(selected_timeframes)}"
                )

            elif choice in {'51', '53'}:
                raw_val = str(val or '').strip().lower()
                if raw_val in {'0', 'off', 'none'}:
                    selected_filters = []
                else:
                    tokens = [token.strip() for token in str(val or '').split(',') if token.strip()]
                    if not tokens:
                        await update.message.reply_text("❌ `0/off` 또는 `1,4,5` 형식으로 입력하세요.")
                        return SELECT
                    selected_filters = []
                    seen_filters = set()
                    for token in tokens:
                        if not re.fullmatch(r'\d+', token):
                            await update.message.reply_text("❌ 필터 번호는 `1,2,3,4,5` 중에서 입력하세요.")
                            return SELECT
                        filter_id = int(token)
                        if filter_id not in UTBOT_FILTER_PACK_ID_SET:
                            await update.message.reply_text("❌ 사용 가능한 필터 번호는 1,2,3,4,5 입니다.")
                            return SELECT
                        if filter_id in seen_filters:
                            await update.message.reply_text("❌ 같은 필터 번호를 중복해서 입력할 수 없습니다.")
                            return SELECT
                        seen_filters.add(filter_id)
                        selected_filters.append(filter_id)
                    selected_filters = sorted(selected_filters)

                filter_pack = self._get_utbot_filter_pack()
                if choice == '51':
                    filter_pack['entry']['selected'] = selected_filters
                    await self.cfg.update_value(
                        ['signal_engine', 'strategy_params', 'UTBot', 'filter_pack'],
                        filter_pack
                    )
                    self._reset_signal_engine_runtime_state(
                        reset_entry_cache=True,
                        reset_exit_cache=True,
                        reset_stateful_strategy=True
                    )
                    await update.message.reply_text(
                        f"✅ UTBot 진입 필터: {self._format_utbot_filter_pack_selected(selected_filters)}"
                    )
                else:
                    filter_pack['exit']['selected'] = selected_filters
                    filter_pack['exit']['mode_by_filter'] = {
                        str(filter_id): normalize_utbot_filter_pack_exit_mode(
                            filter_pack.get('exit', {}).get('mode_by_filter', {}).get(str(filter_id), 'confirm')
                        )
                        for filter_id in selected_filters
                    }
                    await self.cfg.update_value(
                        ['signal_engine', 'strategy_params', 'UTBot', 'filter_pack'],
                        filter_pack
                    )
                    self._reset_signal_engine_runtime_state(
                        reset_entry_cache=True,
                        reset_exit_cache=True,
                        reset_stateful_strategy=True
                    )
                    await update.message.reply_text(
                        f"✅ UTBot 청산 필터: {self._format_utbot_filter_pack_selected(selected_filters)}"
                    )

            elif choice in {'52', '54'}:
                logic_raw = str(val or '').strip().lower()
                if logic_raw not in {'and', 'or'}:
                    await update.message.reply_text("❌ `and` 또는 `or`만 입력하세요.")
                    return SELECT
                filter_pack = self._get_utbot_filter_pack()
                if choice == '52':
                    filter_pack['entry']['logic'] = logic_raw
                    label = '진입'
                else:
                    filter_pack['exit']['logic'] = logic_raw
                    label = '청산'
                await self.cfg.update_value(
                    ['signal_engine', 'strategy_params', 'UTBot', 'filter_pack'],
                    filter_pack
                )
                self._reset_signal_engine_runtime_state(
                    reset_entry_cache=True,
                    reset_exit_cache=True,
                    reset_stateful_strategy=True
                )
                await update.message.reply_text(
                    f"✅ UTBot {label} 결합모드: {self._format_utbot_filter_pack_logic(logic_raw)}"
                )

            elif choice == '55':
                filter_pack = self._get_utbot_filter_pack()
                exit_selected = normalize_utbot_filter_pack_selected(
                    filter_pack.get('exit', {}).get('selected', [])
                )
                if not exit_selected:
                    await update.message.reply_text("❌ 먼저 53번에서 UTBot 청산 필터를 선택하세요.")
                    return SELECT

                raw_val = str(val or '').strip().lower()
                if raw_val in {'0', 'off', 'none'}:
                    mode_by_filter = {str(filter_id): 'confirm' for filter_id in exit_selected}
                else:
                    tokens = [token.strip() for token in str(val or '').split(',') if token.strip()]
                    if not tokens:
                        await update.message.reply_text("❌ 형식: `2:c,3:s,5:c`")
                        return SELECT
                    mode_by_filter = {str(filter_id): 'confirm' for filter_id in exit_selected}
                    seen_mode_filters = set()
                    for token in tokens:
                        if ':' not in token:
                            await update.message.reply_text("❌ 형식: `2:c,3:s,5:c`")
                            return SELECT
                        filter_token, mode_token = token.split(':', 1)
                        filter_token = filter_token.strip()
                        mode_token = mode_token.strip().lower()
                        if not re.fullmatch(r'\d+', filter_token):
                            await update.message.reply_text("❌ 필터 번호는 숫자로 입력하세요.")
                            return SELECT
                        filter_id = int(filter_token)
                        if filter_id not in exit_selected:
                            await update.message.reply_text(
                                "❌ 55번에서는 53번에서 선택한 청산 필터만 지정할 수 있습니다."
                            )
                            return SELECT
                        if filter_id in seen_mode_filters:
                            await update.message.reply_text("❌ 같은 필터 타입을 중복 지정할 수 없습니다.")
                            return SELECT
                        if mode_token not in {'c', 'confirm', 's', 'signal'}:
                            await update.message.reply_text("❌ 필터 타입은 `c(confirm)` 또는 `s(signal)`만 가능합니다.")
                            return SELECT
                        seen_mode_filters.add(filter_id)
                        mode_by_filter[str(filter_id)] = normalize_utbot_filter_pack_exit_mode(mode_token)

                filter_pack['exit']['mode_by_filter'] = mode_by_filter
                await self.cfg.update_value(
                    ['signal_engine', 'strategy_params', 'UTBot', 'filter_pack'],
                    filter_pack
                )
                self._reset_signal_engine_runtime_state(
                    reset_entry_cache=True,
                    reset_exit_cache=True,
                    reset_stateful_strategy=True
                )
                await update.message.reply_text(
                    f"✅ UTBot 청산 필터 타입: {self._format_utbot_filter_pack_mode_map(mode_by_filter, exit_selected)}"
                )

            elif choice == '17':
                # HMA 湲곌컙 蹂寃?(?뺤떇: "9,21")
                parts = val.replace(' ', '').split(',')
                if len(parts) != 2:
                    await update.message.reply_text("❌ 형식: fast,slow (예: 9,21)")
                    return SELECT
                fast, slow = int(parts[0]), int(parts[1])
                if fast >= slow:
                    await update.message.reply_text("❌ fast HMA는 slow HMA보다 작아야 합니다.")
                    return SELECT
                await self.cfg.update_value(['signal_engine', 'strategy_params', 'HMA', 'fast_period'], fast)
                await self.cfg.update_value(['signal_engine', 'strategy_params', 'HMA', 'slow_period'], slow)
                await update.message.reply_text(f"✅ HMA 기간 변경: {fast}/{slow}")

            elif choice == '19':
                parts = val.replace(' ', '').split(',')
                if len(parts) not in (2, 3):
                    await update.message.reply_text("❌ 형식: key,atr,on/off (예: 1,10,off)")
                    return SELECT

                key_value = float(parts[0])
                atr_period = int(parts[1])
                if key_value <= 0 or atr_period < 1:
                    await update.message.reply_text("❌ key는 0보다 커야 하고 ATR 기간은 1 이상이어야 합니다.")
                    return SELECT

                use_ha = self.cfg.get('signal_engine', {}).get('strategy_params', {}).get('UTBot', {}).get('use_heikin_ashi', False)
                if len(parts) == 3:
                    ha_raw = parts[2].lower()
                    if ha_raw in ('on', 'true', '1', 'yes'):
                        use_ha = True
                    elif ha_raw in ('off', 'false', '0', 'no'):
                        use_ha = False
                    else:
                        await update.message.reply_text("❌ HA 옵션은 on/off 로 입력하세요.")
                        return SELECT

                await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBot', 'key_value'], key_value)
                await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBot', 'atr_period'], atr_period)
                await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBot', 'use_heikin_ashi'], use_ha)
                self._reset_signal_engine_runtime_state(
                    reset_entry_cache=True,
                    reset_stateful_strategy=True
                )
                await update.message.reply_text(
                    f"✅ UT Bot 설정 변경: key={key_value:.2f}, ATR={atr_period}, HA={'ON' if use_ha else 'OFF'}"
                )

            elif choice == '26':
                mode_raw = str(val or '').strip().lower()
                if mode_raw in {'1', 'next', 'next_candle', 'next-candle'}:
                    timing_mode = 'next_candle'
                    timing_label = '다음봉 진입'
                elif mode_raw in {'2', 'persistent', 'hold', 'maintain'}:
                    timing_mode = 'persistent'
                    timing_label = '신호유지 진입'
                else:
                    await update.message.reply_text("❌ `next`(1) 또는 `persistent`(2)만 입력하세요.")
                    return SELECT

                await self.cfg.update_value(['signal_engine', 'strategy_params', 'ut_entry_timing_mode'], timing_mode)
                self._reset_signal_engine_runtime_state(
                    reset_entry_cache=True,
                    reset_stateful_strategy=True
                )
                await update.message.reply_text(f"✅ UT 진입 방식 변경: {timing_label}")

            elif choice == '49':
                filter_mode = self._normalize_utsmc_candidate_filter_mode(val)
                if filter_mode == 'off' and str(val or '').strip().lower() not in {'0', 'off', 'none'}:
                    await update.message.reply_text("❌ `0/off`, `1/c1`, `2/c2`, `3/c12` 중 하나를 입력하세요.")
                    return SELECT

                await self.cfg.update_value(
                    ['signal_engine', 'strategy_params', 'UTSMC', 'candidate_filter', 'mode'],
                    filter_mode
                )
                self._reset_signal_engine_runtime_state(
                    reset_entry_cache=True,
                    reset_stateful_strategy=True
                )
                await update.message.reply_text(
                    f"✅ UTSMC 후보 필터 변경: {self._format_utsmc_candidate_filter_mode(filter_mode)}"
                )

            elif choice == '50':
                toggle_raw = str(val or '').strip().lower()
                if toggle_raw in {'1', 'on', 'true', 'yes'}:
                    exit_candidate2_enabled = True
                elif toggle_raw in {'0', 'off', 'false', 'no'}:
                    exit_candidate2_enabled = False
                else:
                    await update.message.reply_text("❌ `on/off`, `1/0`, `true/false`, `yes/no` 중 하나를 입력하세요.")
                    return SELECT

                await self.cfg.update_value(
                    ['signal_engine', 'strategy_params', 'UTSMC', 'exit_candidate2_enabled'],
                    exit_candidate2_enabled
                )
                self._reset_signal_engine_runtime_state(reset_exit_cache=True)
                await update.message.reply_text(
                    f"✅ UTSMC C2 청산: {'ON' if exit_candidate2_enabled else 'OFF'}"
                )

            elif choice == '18':
                # 吏꾩엯紐⑤뱶 蹂寃?(1=cross, 2=position)
                if val == '1':
                    val_lower = 'cross'
                elif val == '2':
                    val_lower = 'position'
                else:
                    await update.message.reply_text("❌ 1(Cross) 또는 2(Position)를 입력하세요.")
                    return SELECT

                await self.cfg.update_value(['signal_engine', 'strategy_params', 'entry_mode'], val_lower)
                await update.message.reply_text(f"✅ 진입모드 변경: {val_lower.upper()}")

            elif choice == '22':
                mode_map = {
                    '1': BINANCE_TESTNET,
                    '2': BINANCE_MAINNET,
                    '3': UPBIT_MODE
                }
                network_choice = self._normalize_setup_network_choice(val)
                if network_choice in {'0', '나가기', '종료', '취소', 'cancel', 'exit', 'quit'}:
                    await update.message.reply_text("✅ 설정 종료", reply_markup=ReplyKeyboardRemove())
                    await self._restore_main_keyboard(update)
                    return ConversationHandler.END
                if network_choice not in mode_map:
                    await update.message.reply_text(
                        "❌ 버튼에서 선택하거나 1, 2, 3 중 하나를 입력하세요.\n"
                        "1=바이낸스 테스트넷, 2=바이낸스 메인넷, 3=업비트 KRW 현물",
                        reply_markup=self._build_setup_network_keyboard()
                    )
                    return INPUT

                target_mode = mode_map[network_choice]
                current_mode = self.get_exchange_mode()

                if target_mode == current_mode:
                    await update.message.reply_text(f"ℹ️ 이미 {self.get_network_status_label(target_mode)} 사용 중입니다.")
                else:
                    target_name = self.get_network_status_label(target_mode)
                    await update.message.reply_text(f"🔄 {target_name}으로 전환 중...")

                    success, result = await self.reinit_exchange(target_mode)

                    if success:
                        if target_mode == UPBIT_MODE:
                            await self.cfg.update_value(['system_settings', 'trade_direction'], 'long')
                        await update.message.reply_text(self._format_exchange_reinit_result(result))
                    else:
                        await update.message.reply_text(f"❌ 거래소 전환 실패: {result}")

            elif choice == '23':
                # Scanner Toggle
                if val in ['1', 'on', 'ON']:
                    new_val = True
                elif val in ['0', 'off', 'OFF']:
                    new_val = False
                else:
                    await update.message.reply_text("❌ 1(ON) 또는 0(OFF)를 입력하세요.")
                    return SELECT

                await self.cfg.update_value(['signal_engine', 'common_settings', 'scanner_enabled'], new_val)
                status = "ON 📡" if new_val else "OFF"
                await update.message.reply_text(f"✅ 거래량 급등 채굴: {status}")

            elif choice == '27':
                # R2 Threshold
                v = float(val)
                if v < 0.01 or v > 0.9:
                    await update.message.reply_text("❌ 0.01 ~ 0.9 사이 값을 입력하세요.")
                    return SELECT
                await self.cfg.update_value(['signal_engine', 'common_settings', 'r2_threshold'], v)
                await update.message.reply_text(f"✅ R2 기준값 변경: {v}")

            elif choice == '31':
                # CHOP Threshold
                v = float(val)
                if v < 0 or v > 100:
                    await update.message.reply_text("❌ 0 ~ 100 사이 값을 입력하세요.")
                    return SELECT
                await self.cfg.update_value(['signal_engine', 'common_settings', 'chop_threshold'], v)
                await update.message.reply_text(f"✅ CHOP 기준값 변경: {v}")

            elif choice == '34':
                # CC Threshold & Length
                parts = val.replace(' ', '').split(',')
                if len(parts) == 1:
                    v = float(parts[0])
                    if v < 0.1 or v > 1.0:
                        await update.message.reply_text("❌ 임계값은 0.1 ~ 1.0 사이여야 합니다.")
                        return SELECT
                    await self.cfg.update_value(['signal_engine', 'common_settings', 'cc_threshold'], v)
                    await update.message.reply_text(f"✅ CC 임계값 변경: {v}")
                elif len(parts) == 2:
                    v = float(parts[0])
                    l = int(parts[1])
                    if v < 0.1 or v > 1.0 or l < 5 or l > 100:
                        await update.message.reply_text("❌ 임계값(0.1~1.0), 기간(5~100)을 확인하세요.")
                        return SELECT
                    await self.cfg.update_value(['signal_engine', 'common_settings', 'cc_threshold'], v)
                    await self.cfg.update_value(['signal_engine', 'common_settings', 'cc_length'], l)
                    await update.message.reply_text(f"✅ CC 설정: 임계값={v}, 기간={l}")
                else:
                    await update.message.reply_text("❌ 형식: 임계값 또는 임계값,기간")
                    return SELECT

            elif choice == '35':
                # 듀얼모드 변경
                if val == '1':
                    new_mode = 'standard'
                    mode_label = '스탠다드'
                elif val == '2':
                    new_mode = 'scalping'
                    mode_label = '스캘핑'
                else:
                    await update.message.reply_text("❌ 1(스탠다드) 또는 2(스캘핑)를 입력하세요.")
                    return SELECT

                await self.cfg.update_value(['dual_mode_engine', 'mode'], new_mode)
                await update.message.reply_text(f"✅ 듀얼모드 변경: {mode_label}")

                # 利됱떆 ?ъ큹湲고솕 ?몃━嫄?(poll_tick?먯꽌 媛먯??섏?留?紐낆떆??由ъ뀑)
                dm_engine = self.engines.get('dualmode')
                if dm_engine:
                    dm_engine._init_strategy()

            elif choice == '41':
                # 청산 타임프레임 변경
                valid_tf = ['1m', '2m', '3m', '4m', '5m', '15m', '30m', '1h', '2h', '4h', '6h', '8h', '12h', '1d', '3d', '1w', '1M']
                if val not in valid_tf:
                    await update.message.reply_text(f"❌ 유효하지 않은 타임프레임입니다.\n사용 가능: {', '.join(valid_tf)}")
                    return SELECT
                await self.cfg.update_value(['signal_engine', 'common_settings', 'exit_timeframe'], val)
                # Signal 엔진 캐시 초기화
                self._reset_signal_engine_runtime_state(reset_exit_cache=True)
                await update.message.reply_text(f"✅ 청산 타임프레임 변경: {val}")

            elif choice == '44':
                parts = val.replace(' ', '').split(',')
                if len(parts) not in (2, 3):
                    await update.message.reply_text("❌ 형식: key,atr,on/off (예: 1,10,off)")
                    return SELECT

                key_value = float(parts[0])
                atr_period = int(parts[1])
                if key_value <= 0 or atr_period < 1:
                    await update.message.reply_text("❌ key는 0보다 커야 하고 ATR 기간은 1 이상이어야 합니다.")
                    return SELECT

                use_ha = self.cfg.get('upbit', {}).get('strategy_params', {}).get('UTBot', {}).get('use_heikin_ashi', False)
                if len(parts) == 3:
                    ha_raw = parts[2].lower()
                    if ha_raw in ('on', 'true', '1', 'yes'):
                        use_ha = True
                    elif ha_raw in ('off', 'false', '0', 'no'):
                        use_ha = False
                    else:
                        await update.message.reply_text("❌ HA 옵션은 on/off 로 입력하세요.")
                        return SELECT

                await self.cfg.update_value(['upbit', 'strategy_params', 'UTBot', 'key_value'], key_value)
                await self.cfg.update_value(['upbit', 'strategy_params', 'UTBot', 'atr_period'], atr_period)
                await self.cfg.update_value(['upbit', 'strategy_params', 'UTBot', 'use_heikin_ashi'], use_ha)
                self._reset_signal_engine_runtime_state(
                    reset_entry_cache=True,
                    reset_stateful_strategy=True
                )
                await update.message.reply_text(
                    f"✅ 업비트 UT Bot 설정 변경: key={key_value:.2f}, ATR={atr_period}, HA={'ON' if use_ha else 'OFF'}"
                )

            elif choice == '46':
                valid_tf = ['1m', '3m', '5m', '15m', '30m', '1h', '4h', '1d']
                if val not in valid_tf:
                    await update.message.reply_text(f"❌ 유효하지 않은 타임프레임입니다.\n사용 가능: {', '.join(valid_tf)}")
                    return SELECT
                await self.cfg.update_value(['upbit', 'common_settings', 'timeframe'], val)
                await self.cfg.update_value(['upbit', 'common_settings', 'entry_timeframe'], val)
                self._reset_signal_engine_runtime_state(
                    reset_entry_cache=True,
                    reset_stateful_strategy=True
                )
                await update.message.reply_text(f"✅ 업비트 진입 타임프레임 변경: {val}")

            elif choice == '47':
                valid_tf = ['1m', '3m', '5m', '15m', '30m', '1h', '4h', '1d']
                if val not in valid_tf:
                    await update.message.reply_text(f"❌ 유효하지 않은 타임프레임입니다.\n사용 가능: {', '.join(valid_tf)}")
                    return SELECT
                await self.cfg.update_value(['upbit', 'common_settings', 'exit_timeframe'], val)
                self._reset_signal_engine_runtime_state(reset_exit_cache=True)
                await update.message.reply_text(f"✅ 업비트 청산 타임프레임 변경: {val}")

            elif choice == '48':
                limit_krw = float(val)
                if limit_krw <= 0:
                    await update.message.reply_text("❌ 업비트 일일 손실 제한은 0보다 커야 합니다.")
                    return SELECT
                await self.cfg.update_value(['upbit', 'common_settings', 'daily_loss_limit'], limit_krw)
                await update.message.reply_text(f"✅ 업비트 일일 손실 제한 변경: ₩{limit_krw:,.0f}")

            # 10~41 success message handled
            if choice not in ['2', '3', '10', '11', '12', '14', '15', '16', '17', '18', '19', '20', '21', '22', '23', '26', '27', '28', '30', '31', '33', '34', '35', '41', '44', '45', '46', '47', '48', '49', '50', '51', '52', '53', '54', '55']:
                await update.message.reply_text(f"✅ 설정 완료: {val}")
            await self._restore_main_keyboard(update)
            await self.show_setup_menu(update)
            return SELECT

        except ValueError:
            await update.message.reply_text("❌ 올바른 숫자를 입력하세요.")
            return SELECT
        except Exception as e:
            logger.error(f"Setup input error: {e}")
            await update.message.reply_text(f"❌ 오류: {e}")
            return SELECT

    async def setup_symbol_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """?щ낵 蹂寃?泥섎━ - 1/2/3 ?⑥텞???먮뒗 吏곸젒 ?낅젰"""
        choice = update.message.text.strip().upper()
        setup_choice = context.user_data.get('setup_choice')
        target_mode = UPBIT_MODE if setup_choice == '43' else self.get_exchange_mode()
        new_watchlist = self.get_active_watchlist() if target_mode != UPBIT_MODE else []
        if not isinstance(new_watchlist, list):
            new_watchlist = self.get_default_watchlist_for_exchange_mode(target_mode)
        upbit_watchlist = self.cfg.get('upbit', {}).get('watchlist', ['BTC/KRW'])
        if not isinstance(upbit_watchlist, list):
            upbit_watchlist = self.get_default_watchlist_for_exchange_mode(UPBIT_MODE)

        # 1/2/3 踰덊샇濡??щ낵 留ㅽ븨 (?⑥텞??
        symbol_map = (
            {'1': 'BTC/KRW', '2': 'ETH/KRW', '3': 'SOL/KRW'}
            if target_mode == UPBIT_MODE
            else {'1': 'BTC/USDT', '2': 'ETH/USDT', '3': 'SOL/USDT'}
        )

        # ?⑥텞???먮뒗 吏곸젒 ?낅젰 ?ъ슜
        raw_symbol = symbol_map.get(choice, choice)
        try:
            markets = await self._load_trade_markets_for_exchange_mode(target_mode)
            symbol = self._resolve_watch_symbol_for_exchange_mode(
                raw_symbol,
                markets,
                exchange_mode=target_mode,
            )
        except ValueError as ve:
            await update.message.reply_text(f"❌ {ve}")
            return SELECT
        except Exception as exc:
            await update.message.reply_text(f"❌ market 검증 실패: {exc}")
            return SELECT

        # ?좏슚??寃??(媛꾨떒??Ticker 議고쉶)
        try:
            await asyncio.to_thread(self.exchange.fetch_ticker, symbol)
        except Exception:
            await update.message.reply_text(f"❌ 유효하지 않은 심볼 또는 거래쌍입니다: {symbol}")
            return SELECT

        try:
            eng = self.cfg.get('system_settings', {}).get('active_engine', CORE_ENGINE)
            display_symbol = self.format_symbol_for_display(symbol, target_mode)
            if eng == 'shannon':
                await self.cfg.update_value(['shannon_engine', 'target_symbol'], symbol)
            elif eng == 'dualthrust':
                await self.cfg.update_value(['dual_thrust_engine', 'target_symbol'], symbol)
            elif eng == 'dualmode':
                await self.cfg.update_value(['dual_mode_engine', 'target_symbol'], symbol)
            elif eng == 'tema':
                await self.cfg.update_value(['tema_engine', 'target_symbol'], symbol)
            elif setup_choice == '43':
                await self._update_config_value(['upbit', 'watchlist'], [symbol])
                await self._update_config_value(['exchange_watchlists', UPBIT_MODE], [symbol])
                upbit_watchlist = [symbol]
                await update.message.reply_text(f"✅ 업비트 코인 변경: {display_symbol}")
            else:
                if setup_choice == '38':
                    if symbol not in new_watchlist:
                        new_watchlist = new_watchlist + [symbol]
                        await self._update_config_value(['signal_engine', 'watchlist'], new_watchlist)
                        await self._update_config_value(['exchange_watchlists', target_mode], new_watchlist)
                        await update.message.reply_text(f"✅ 감시 심볼 추가: {display_symbol}")
                    else:
                        await update.message.reply_text(f"ℹ️ 이미 감시 목록에 있습니다: {display_symbol}")
                else:
                    # Signal ?붿쭊: 硫붾돱?먯꽌 蹂寃???Watchlist瑜??대떦 ?щ낵濡?**?泥?* (湲곗〈 ?숈옉 ?좎?)
                    await self._update_config_value(['signal_engine', 'watchlist'], [symbol])
                    await self._update_config_value(['exchange_watchlists', target_mode], [symbol])
                    new_watchlist = [symbol]
                    await update.message.reply_text("ℹ️ Signal 엔진 감시 목록이 해당 심볼로 초기화되었습니다.")

            # 留덉폆 ?ㅼ젙 ?곸슜
            # 留덉폆 ?ㅼ젙 ?곸슜
            if self.active_engine:
                await self.active_engine.ensure_market_settings(symbol)

            # Shannon ?붿쭊 罹먯떆 珥덇린??(?щ낵 蹂寃????꾩닔!)
            shannon_engine = self.engines.get('shannon')
            if shannon_engine:
                shannon_engine.ema_200 = None
                shannon_engine.atr_value = None
                shannon_engine.trend_direction = None
                shannon_engine.last_indicator_update = 0
                shannon_engine.position_cache = None
                shannon_engine.grid_orders = []
                logger.info(f"?봽 Shannon engine cache cleared for new symbol: {symbol}")

            # Signal ?붿쭊 罹먯떆??珥덇린??
            signal_engine = self.engines.get('signal')
            if signal_engine:
                signal_engine.position_cache = None
                if setup_choice == '38':
                    signal_engine.active_symbols.add(symbol)
                    await signal_engine.prime_symbol_to_next_closed_candle(symbol)
                elif setup_choice == '43':
                    self._reset_signal_engine_runtime_state(
                        reset_entry_cache=True,
                        reset_exit_cache=True,
                        reset_stateful_strategy=True
                    )
                    signal_engine.active_symbols.clear()
                    signal_engine.active_symbols.add(symbol)
                    await signal_engine.prime_symbol_to_next_closed_candle(symbol)
                else:
                    self._reset_signal_engine_runtime_state(
                        reset_entry_cache=True,
                        reset_exit_cache=True,
                        reset_stateful_strategy=True
                    )
                    signal_engine.active_symbols.clear() # 湲곗〈 ?섎룞 紐⑸줉??珥덇린??(紐낇솗?깆쓣 ?꾪빐)
                    signal_engine.active_symbols.add(symbol)
                    await signal_engine.prime_symbol_to_next_closed_candle(symbol)

            # Dual Thrust ?붿쭊 罹먯떆??珥덇린??
            dt_engine = self.engines.get('dualthrust')
            if dt_engine:
                dt_engine.position_cache = None
                dt_engine.trigger_date = None  # ?몃━嫄??ш퀎??
                logger.info(f"?봽 DualThrust engine cache cleared for new symbol: {symbol}")

            # TEMA ?붿쭊 罹먯떆 珥덇린??
            tema_engine = self.engines.get('tema')
            if tema_engine:
                tema_engine.last_candle_time = 0
                tema_engine.ema1 = None
                tema_engine.ema2 = None
                tema_engine.ema3 = None
                logger.info(f"?봽 TEMA engine cache cleared for new symbol: {symbol}")

            if setup_choice == '38':
                watchlist_text = ", ".join(new_watchlist)
                await update.message.reply_text(f"✅ 감시 목록: {watchlist_text}")
            elif setup_choice == '43':
                watchlist_text = ", ".join(self.format_symbol_for_display(s) for s in upbit_watchlist)
                await update.message.reply_text(f"✅ 업비트 감시 코인: {watchlist_text}")
            else:
                await update.message.reply_text(f"✅ 심볼 변경 완료: {display_symbol}")
            await self._restore_main_keyboard(update)
            await self.show_setup_menu(update)
            return SELECT

        except Exception as e:
            logger.error(f"Symbol change error: {e}")
            await update.message.reply_text(f"❌ 심볼 변경 실패: {e}")
            return SELECT

    async def setup_direction_select(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """留ㅻℓ 諛⑺뼢 ?좏깮 泥섎━"""
        text = update.message.text

        if self.is_upbit_mode():
            await self.cfg.update_value(['system_settings', 'trade_direction'], 'long')
            await update.message.reply_text("ℹ️ 업비트 KRW 현물은 숏이 없어 매매 방향이 `롱만`으로 고정됩니다.", parse_mode=ParseMode.MARKDOWN)
            await self._restore_main_keyboard(update)
            await self.show_setup_menu(update)
            return SELECT

        direction_map = {
            '양방향': 'both',
            'Long+Short': 'both',
            '롱만': 'long',
            'Long Only': 'long',
            '숏만': 'short',
            'Short Only': 'short'
        }

        direction = None
        for key, val in direction_map.items():
            if key in text:
                direction = val
                break

        if direction:
            previous_direction = self.get_effective_trade_direction()
            await self.cfg.update_value(['system_settings', 'trade_direction'], direction)
            if direction != previous_direction:
                self._reset_signal_engine_runtime_state(
                    reset_entry_cache=True,
                    reset_exit_cache=True,
                    reset_stateful_strategy=True
                )
            direction_label = {'both': '양방향', 'long': '롱만', 'short': '숏만'}.get(direction, direction)
            await update.message.reply_text(f"✅ 매매 방향 변경: {direction_label}")
        else:
            await update.message.reply_text("❌ 유효하지 않은 선택입니다.")

        await self._restore_main_keyboard(update)
        await self.show_setup_menu(update)
        return SELECT

    async def setup_engine_select(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """?붿쭊 援먯껜 泥섎━"""
        text = update.message.text.strip()

        mode_map = {'1': CORE_ENGINE}

        if text in mode_map:
            mode = mode_map[text]
            if mode == 'dualmode' and not DUAL_MODE_AVAILABLE:
                await update.message.reply_text("❌ DualMode 관련 모듈이 없어 사용할 수 없습니다.")
            else:
                await self.cfg.update_value(['system_settings', 'active_engine'], mode)
                await self._switch_engine(mode)
                await update.message.reply_text(f"✅ 엔진 변경 완료: {mode.upper()}")
        else:
            await update.message.reply_text("ℹ️ 코어 모드에서는 `1 (Signal)`만 사용 가능합니다.", parse_mode=ParseMode.MARKDOWN)

        await self._restore_main_keyboard(update)
        await self.show_setup_menu(update)
        return SELECT

    def _build_main_keyboard(self):
        kb = [
            [KeyboardButton("🚨 STOP"), KeyboardButton("⏸ PAUSE"), KeyboardButton("▶ RESUME")],
            [KeyboardButton("/utbreak")],
            [KeyboardButton("/setup"), KeyboardButton("/prediction")],
            [KeyboardButton("/customentry")],
            [KeyboardButton("/status"), KeyboardButton("/history"), KeyboardButton("/stats")],
            [KeyboardButton("/log"), KeyboardButton("/help")]
        ]
        return ReplyKeyboardMarkup(kb, resize_keyboard=True)

    async def _restore_main_keyboard(self, update: Update):
        """Restore the main keyboard."""
        await update.message.reply_text("📱 메인 메뉴", reply_markup=self._build_main_keyboard())
