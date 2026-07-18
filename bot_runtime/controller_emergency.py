"""Emergency liquidation controls and outbound notification helpers."""

from __future__ import annotations


class ControllerEmergencyMixin:
    async def emergency_stop(self):
        """湲닿툒 ?뺤? - 紐⑤뱺 ?ㅽ뵂 ?ъ???泥?궛"""
        logger.warning("Emergency stop triggered")

        engine = self.active_engine or self.engines.get(CORE_ENGINE)
        if self.active_engine:
            self.active_engine.stop()

        self.is_paused = True
        result = {
            'status': 'started',
            'position_count': 0,
            'closed': 0,
            'failed': 0,
            'cancelled_orders': 0,
            'closed_positions': [],
            'failed_positions': [],
        }

        try:
            if self.is_upbit_mode():
                upbit_engine = engine or self.engines.get(CORE_ENGINE)
                open_symbols = set()
                if upbit_engine:
                    open_symbols = await upbit_engine.get_active_position_symbols(use_cache=False)

                if not open_symbols:
                    cancelled_orders = 0
                    for sym in self.get_active_watchlist():
                        try:
                            open_orders = await asyncio.to_thread(self.exchange.fetch_open_orders, sym)
                        except Exception as order_fetch_error:
                            logger.warning(f"Open order fetch error for {sym}: {order_fetch_error}")
                            continue

                        for order in open_orders or []:
                            order_id = order.get('id')
                            if not order_id:
                                continue
                            try:
                                await asyncio.to_thread(self.exchange.cancel_order, order_id, sym)
                                cancelled_orders += 1
                            except Exception as cancel_error:
                                logger.warning(f"Cancel order error for {sym} / {order_id}: {cancel_error}")

                    result.update({
                        'status': 'no_position',
                        'cancelled_orders': cancelled_orders,
                    })
                    if cancelled_orders:
                        await self.notify(f"ℹ️ 업비트 보유 코인은 없어서 미체결 주문 `{cancelled_orders}`건만 취소했습니다.")
                    else:
                        await self.notify("ℹ️ 청산할 업비트 보유 코인이 없습니다.")
                    return result

                symbols_text = ", ".join(self.format_symbol_for_display(sym) for sym in sorted(open_symbols))
                result['position_count'] = len(open_symbols)
                await self.notify(
                    f"🚨 **긴급 정지 실행**\n업비트 보유 코인 `{len(open_symbols)}`개를 즉시 매도합니다.\n대상: `{symbols_text}`"
                )

                for sym in sorted(open_symbols):
                    try:
                        await upbit_engine.exit_position(sym, "EmergencyStop")
                        result['closed'] += 1
                        result['closed_positions'].append({'symbol': sym})
                    except Exception as e:
                        result['failed'] += 1
                        result['failed_positions'].append({'symbol': sym, 'error': str(e)})
                        logger.error(f"Failed to close Upbit position {sym}: {e}")
                        await self.notify(f"❌ {self.format_symbol_for_display(sym)} 청산 실패: {e}")

                result['status'] = 'failed' if result['failed'] and not result['closed'] else ('partial_failed' if result['failed'] else 'closed')
                await self.notify("🧯 긴급 정지 처리 완료")
                return result

            # 1. ?ㅽ뵂??紐⑤뱺 ?ъ???議고쉶
            signal_engine = engine if hasattr(engine, '_cancel_protection_orders') else self.engines.get(CORE_ENGINE)
            emergency_service = None
            if signal_engine is not None:
                try:
                    emergency_service = getattr(signal_engine, 'crypto_execution', None)
                except Exception:
                    logger.exception("Emergency stop could not resolve the engine execution service")
            if emergency_service is None:
                store = getattr(self, 'trading_state_store', None)
                if store is None:
                    store = SQLiteTradingStateStore(':memory:')
                    self.trading_state_store = store

                async def _emergency_position_fetcher(target_symbol):
                    return await self._fetch_emergency_position_by_symbol(target_symbol)

                gateway = getattr(self, 'idempotent_order_gateway', None)
                if gateway is None or gateway.exchange is not self.exchange:
                    gateway = IdempotentOrderGateway(
                        self.exchange,
                        store,
                        position_fetcher=_emergency_position_fetcher,
                    )
                    self.idempotent_order_gateway = gateway
                emergency_service = CryptoExecutionService(
                    self.exchange,
                    store,
                    gateway,
                    BinanceAlgoOrderGateway(self.exchange),
                    accounting_service=TradeAccountingFinalizer(self.exchange, store),
                )
                self.crypto_execution_service = emergency_service
            positions = await asyncio.to_thread(self.exchange.fetch_positions)
            open_positions = []
            for p in positions:
                normalized_pos = self._normalize_futures_position_for_emergency(p)
                if normalized_pos:
                    open_positions.append(normalized_pos)
            result['position_count'] = len(open_positions)

            if not open_positions:
                # ?ъ??섏씠 ?녿떎硫? ?뱀떆 紐⑤Ⅴ???꾩옱 ?ㅼ젙???щ낵??誘몄껜寃?二쇰Ц留?痍⑥냼 ?쒕룄
                symbols_to_clean = {self._get_current_symbol(), *self.get_active_watchlist()}
                if signal_engine:
                    symbols_to_clean.update(getattr(signal_engine, 'active_symbols', set()) or set())
                    symbols_to_clean.update(getattr(signal_engine, 'last_protection_order_status', {}).keys())
                    if getattr(signal_engine, 'scanner_active_symbol', None):
                        symbols_to_clean.add(signal_engine.scanner_active_symbol)
                for sym in sorted(symbol for symbol in symbols_to_clean if symbol):
                    try:
                        if signal_engine:
                            await signal_engine._cancel_all_orders_variants(sym, reason='emergency stop without position')
                            await signal_engine._reconcile_closed_position_protection(
                                sym,
                                reason='emergency stop without position',
                                alert=True,
                                attempts=2
                            )
                        else:
                            await asyncio.to_thread(self.exchange.cancel_all_orders, sym)
                            logger.info(f"??All orders cancelled for {sym}")
                    except Exception as cleanup_error:
                        logger.warning(f"Emergency orphan cleanup failed for {sym}: {cleanup_error}")
                result['status'] = 'no_position'
                await self.notify("ℹ️ 청산할 오픈 포지션이 없습니다. (미체결 주문만 취소)")
                return result

            await self.notify(f"🚨 **긴급 정지 실행**\n발견된 포지션 {len(open_positions)}개를 즉시 청산합니다.")

            # 2. 紐⑤뱺 ?ㅽ뵂 ?ъ????쒖감 泥?궛
            for pos in open_positions:
                sym = pos['symbol']

                # 二쇰Ц 痍⑥냼
                try:
                    if signal_engine:
                        await signal_engine._cancel_all_orders_variants(sym, reason='before emergency close')
                        await signal_engine._cancel_protection_orders(sym, reason='before emergency close')
                    else:
                        await asyncio.to_thread(self.exchange.cancel_all_orders, sym)
                except Exception as e:
                    logger.error(f"Cancel orders error for {sym}: {e}")

                # 泥?궛 二쇰Ц
                try:
                    pnl = float(pos.get('pnl', 0.0) or 0.0)
                    order = None
                    last_error = None
                    current_pos = await self._fetch_emergency_position_by_symbol(sym) or pos
                    max_close_attempts = 5
                    for attempt in range(1, max_close_attempts + 1):
                        if not current_pos:
                            break
                        side = 'sell' if current_pos['side'] == 'long' else 'buy'
                        qty = self._safe_emergency_amount(sym, current_pos['contracts'])
                        if float(qty) <= 0:
                            raise ValueError(f"invalid emergency close qty: {qty}")
                        params = self._close_order_params_for_position(current_pos)
                        try:
                            close_submission = await emergency_service.submit_reduce_only_close(
                                strategy='MAIN_CONTROLLER_EMERGENCY_STOP',
                                symbol=sym,
                                position_side=current_pos['side'],
                                position_signature=(
                                    current_pos.get('timestamp')
                                    or current_pos.get('entryPrice')
                                    or f"{current_pos['side']}:{qty}"
                                ),
                                qty=float(qty),
                                reason='close_emergency_stop',
                                params=params,
                                leg=f'attempt_{attempt}',
                            )
                            order = close_submission.order or {}
                            if close_submission.state == OrderState.SUBMITTED_UNKNOWN.value:
                                raise RuntimeError(
                                    'emergency close submission remains unresolved: '
                                    f'{close_submission.client_order_id}'
                                )
                        except Exception as close_error:
                            last_error = close_error
                            logger.error(
                                f"Emergency close attempt {attempt}/{max_close_attempts} failed for {sym}: {close_error}"
                            )
                            await asyncio.sleep(1.0)
                            current_pos = await self._fetch_emergency_position_by_symbol(sym)
                            if not current_pos:
                                break
                            if attempt < max_close_attempts:
                                await self.notify(f"⚠️ {sym} 청산 재시도 중... ({attempt + 1}/{max_close_attempts})")
                            continue

                        await asyncio.sleep(0.9)
                        current_pos = await self._fetch_emergency_position_by_symbol(sym)
                        if not current_pos:
                            break

                        remaining_qty = abs(float(current_pos.get('contracts', 0) or 0))
                        logger.warning(
                            f"Emergency close order accepted but position still open: "
                            f"{sym} {current_pos.get('side')} {remaining_qty}"
                        )
                        if signal_engine:
                            await signal_engine._cancel_all_orders_variants(sym, reason=f'emergency close remaining retry {attempt}')
                            await signal_engine._cancel_protection_orders(sym, reason=f'emergency close remaining retry {attempt}')
                        else:
                            try:
                                await asyncio.to_thread(self.exchange.cancel_all_orders, sym)
                            except Exception as retry_cancel_error:
                                logger.warning(f"Emergency retry cancel failed for {sym}: {retry_cancel_error}")
                        if attempt < max_close_attempts:
                            await self.notify(
                                f"⚠️ {sym} 청산 주문 후 잔여 포지션 확인: "
                                f"{current_pos.get('side', '').upper()} `{remaining_qty}`. 재시도합니다."
                            )

                    if current_pos:
                        remaining_qty = abs(float(current_pos.get('contracts', 0) or 0))
                        raise RuntimeError(
                            f"position still open after emergency close attempts: "
                            f"{current_pos.get('side')} {remaining_qty}"
                        )
                    if not order and last_error:
                        logger.warning(
                            f"Emergency close order errored for {sym}, but position is now flat: {last_error}"
                        )

                    logger.info(f"??Emergency Close: {sym} {side} {qty}")
                    if signal_engine:
                        await asyncio.sleep(0.5)
                        trailing_state = None
                        if hasattr(signal_engine, '_get_utbreakout_trailing_state'):
                            trailing_state = (
                                signal_engine._get_utbreakout_trailing_state(sym)
                            )
                        emergency_exit_price = (
                            order.get('average')
                            if isinstance(order, dict) and order.get('average')
                            else pos.get('mark_price') or pos.get('markPrice')
                        )
                        if hasattr(signal_engine, '_record_closed_trade_accounting'):
                            accounting = await signal_engine._record_closed_trade_accounting(
                                sym,
                                'EmergencyStop',
                                exit_price=emergency_exit_price,
                                state=trailing_state,
                            )
                            logger.info(
                                "Emergency close accounting %s: %s",
                                sym,
                                accounting.get('status') if isinstance(accounting, dict) else accounting,
                            )
                        if hasattr(signal_engine, '_clear_utbreakout_trailing_state'):
                            signal_engine._clear_utbreakout_trailing_state(
                                sym,
                                finalize=True,
                                reason='EmergencyStop',
                                exit_price=emergency_exit_price,
                            )
                        await signal_engine._cancel_all_orders_variants(sym, reason='after emergency close')
                        await signal_engine._reconcile_closed_position_protection(
                            sym,
                            reason='after emergency close',
                            alert=True,
                            attempts=3
                        )
                    result['closed'] += 1
                    result['closed_positions'].append({'symbol': sym, 'qty': qty, 'pnl': pnl})
                    await self.notify(f"✅ **{sym}** 청산 완료\nPnL: ${pnl:+.2f}")

                except Exception as e:
                    result['failed'] += 1
                    result['failed_positions'].append({'symbol': sym, 'error': str(e)})
                    logger.error(f"Failed to close {sym}: {e}")
                    await self.notify(f"❌ {sym} 청산 실패: {e}")

            result['status'] = 'failed' if result['failed'] and not result['closed'] else ('partial_failed' if result['failed'] else 'closed')
            await self.notify("🧯 긴급 정지 처리 완료")
            return result

        except Exception as e:
            logger.error(f"Emergency stop error: {e}")
            await self.notify(f"❌ 긴급 정지 중 오류: {e}")
            result.update({
                'status': 'error',
                'error': str(e),
            })
            return result

    async def notify(self, text):
        """?뚮┝ ?꾩넚"""
        try:
            if self._should_suppress_telegram_notice(text):
                first_line = str(text or '').strip().splitlines()[0][:120] if str(text or '').strip() else ''
                logger.info(f"Telegram notice suppressed by event-only alert mode: {first_line}")
                return
            cid = self.cfg.get_chat_id()
            if not cid or not self.tg_app:
                return

            try:
                await self.tg_app.bot.send_message(
                    chat_id=cid,
                    text=text,
                    parse_mode=ParseMode.MARKDOWN
                )
            except BadRequest as md_err:
                # Dynamic symbols/text can break Markdown entities; retry as plain text.
                if "can't parse entities" in str(md_err).lower():
                    await self.tg_app.bot.send_message(chat_id=cid, text=text)
                else:
                    raise
        except Exception as e:
            logger.error(f"Notify error: {e}")

    async def notify_plain(self, text):
        """Send diagnostics without Telegram Markdown consuming underscores."""
        try:
            if self._should_suppress_telegram_notice(text):
                first_line = str(text or '').strip().splitlines()[0][:120] if str(text or '').strip() else ''
                logger.info(f"Telegram plain notice suppressed by event-only alert mode: {first_line}")
                return
            cid = self.cfg.get_chat_id()
            if not cid or not self.tg_app:
                return
            await self.tg_app.bot.send_message(chat_id=cid, text=text)
        except Exception as e:
            logger.error(f"Plain notify error: {e}")
