"""TEMA, Shannon, Dual Thrust, and fractal engine implementations."""

from __future__ import annotations

from .base_engine import BaseEngine

class TemaEngine(BaseEngine):
    def __init__(self, controller):
        super().__init__(controller)
        self.last_candle_time = 0
        self.consecutive_errors = 0

        # 湲곕낯 湲곗닠??吏??罹먯떆
        self.ema1 = None
        self.ema2 = None
        self.ema3 = None

    def start(self):
        super().start()
        # ?ъ떆?????곹깭 珥덇린?뷀븯??利됱떆 遺꾩꽍 媛?ν븯寃???
        self.last_candle_time = 0
        self.ema1 = None
        self.ema2 = None
        self.ema3 = None
        logger.info(f"?? [TEMA] Engine started")

    async def poll_tick(self):
        if not self.running: return

        try:
            # 1. ?ㅼ젙 濡쒕뱶 (怨듯넻 ?ㅼ젙 ?ъ슜)
            cfg = self.cfg.get('tema_engine', {})
            common_cfg = self.cfg.get('signal_engine', {}).get('common_settings', {})

            symbol = cfg.get('target_symbol', 'BTC/USDT')
            tf = cfg.get('timeframe', '5m')

            # 2. 罹붾뱾 ?곗씠??議고쉶
            ohlcv = await asyncio.to_thread(self.exchange.fetch_ohlcv, symbol, tf, limit=100)
            if not ohlcv or len(ohlcv) < 50:
                return

            last_closed = ohlcv[-2]
            current_ts = int(last_closed[0])
            current_close = float(last_closed[4])

            # 3. ?덈줈??罹붾뱾 留덇컧 ??遺꾩꽍
            if current_ts > self.last_candle_time:
                logger.info(f"?빉截?[TEMA {tf}] {symbol} New Candle: close={current_close}")
                self.last_candle_time = current_ts

                df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

                # 吏??怨꾩궛
                df = self._calculate_indicators(df, cfg)

                # ?좏샇 ?뺤씤
                signal, reason = self._check_entry_conditions(df, cfg)

                # ?ъ???議고쉶
                pos = await self.get_server_position(symbol, use_cache=False)
                if pos and abs(float(pos.get('contracts', 0) or 0)) > 0:
                    p_side = str(pos.get('side', '')).lower()
                    if p_side == 'long':
                        pos_side = 'long'
                    elif p_side == 'short':
                        pos_side = 'short'
                    else:
                        pos_side = 'none'
                else:
                    pos_side = 'none'

                # 吏꾩엯 媛먯?
                if signal and pos_side == 'none':
                    logger.info(f"?? TEMA Signal Detected: {signal.upper()} ({reason})")
                    current_price = float(ohlcv[-1][4])
                    await self.entry(symbol, signal, current_price, common_cfg)

                # 泥?궛 媛먯? (?ъ??섏씠 ?덉쓣 ?뚮쭔)
                elif pos_side != 'none':
                     exit_signal, exit_reason = self._check_exit_conditions(df, pos_side, cfg)
                     if exit_signal:
                         logger.info(f"?몝 TEMA Exit Signal: {exit_reason}")
                         await self.exit_position(symbol, exit_reason)

        except Exception as e:
            self.consecutive_errors += 1
            if self.consecutive_errors % 10 == 0:
                logger.error(f"TemaEngine poll error: {e}")

    def _calculate_indicators(self, df, cfg):
        try:
            rsi_period = cfg.get('rsi_period', 14)
            tema_period = cfg.get('tema_period', 9)
            bb_window = cfg.get('bollinger_window', 20)

            # RSI
            df['rsi'] = ta.rsi(df['close'], length=rsi_period)

            # TEMA Calculation
            # TEMA = (3 * EMA1) - (3 * EMA2) + EMA3
            ema1 = ta.ema(df['close'], length=tema_period)
            ema2 = ta.ema(ema1, length=tema_period)
            ema3 = ta.ema(ema2, length=tema_period)
            df['tema'] = (3 * ema1) - (3 * ema2) + ema3

            # Bollinger Bands
            bb = ta.bbands(df['close'], length=bb_window, std=2.0)
            # pandas_ta bbands returns multiple columns. We need standard names.
            # Assuming default names: BBL_20_2.0, BBM_20_2.0, BBU_20_2.0
            # We map them to simpler names
            cols = bb.columns
            df['bb_lower'] = bb[cols[0]]
            df['bb_mid'] = bb[cols[1]]
            df['bb_upper'] = bb[cols[2]]

            return df
        except Exception as e:
            logger.error(f"Indicator calculation error: {e}")
            return df

    def _check_entry_conditions(self, df, cfg):
        # ?꾨왂: SampleStrategy.py 濡쒖쭅 援ы쁽
        # Long: RSI > 30 & TEMA < BB_Mid & TEMA Rising
        # Short: RSI > 70 & TEMA > BB_Mid & TEMA Falling

        try:
            last = df.iloc[-2] # 吏곸쟾 ?뺤젙 遊?
            prev = df.iloc[-3] # 洹???遊?(異붿꽭 ?뺤씤??

            # TEMA Rising/Falling check
            tema_rising = last['tema'] > prev['tema']
            tema_falling = last['tema'] < prev['tema']

            # Conditions
            # 1. Long
            if (last['rsi'] > 30 and
                last['tema'] <= last['bb_mid'] and
                tema_rising):
                return 'long', f"RSI({last['rsi']:.1f})>30 & TEMA<Mid & Rising"

            # 2. Short
            if (last['rsi'] > 70 and
                last['tema'] >= last['bb_mid'] and
                tema_falling):
                return 'short', f"RSI({last['rsi']:.1f})>70 & TEMA>Mid & Falling"

            return None, None
        except Exception:
            return None, None

    def _check_exit_conditions(self, df, pos_side, cfg):
        # ?꾨왂: SampleStrategy.py 濡쒖쭅 援ы쁽
        # Exit Long: RSI > 70 & TEMA > BB_Mid & TEMA Falling (怨쇰ℓ??+ 爰얠엫)
        # Exit Short: RSI < 30 & TEMA < BB_Mid & TEMA Rising (怨쇰ℓ??+ 諛섎벑)

        try:
            last = df.iloc[-2]
            prev = df.iloc[-3]

            tema_rising = last['tema'] > prev['tema']
            tema_falling = last['tema'] < prev['tema']

            if pos_side == 'long':
                if (last['rsi'] > 70 and
                    last['tema'] > last['bb_mid'] and
                    tema_falling):
                    return True, f"Long Exit: RSI({last['rsi']:.1f})>70 & TEMA>Mid & Falling"

            elif pos_side == 'short':
                if (last['rsi'] < 30 and
                    last['tema'] < last['bb_mid'] and
                    tema_rising):
                    return True, f"Short Exit: RSI({last['rsi']:.1f})<30 & TEMA<Mid & Rising"

            return False, None
        except Exception:
            return False, None

    async def entry(self, symbol, side, price, common_cfg):
        try:
            # === [Single Position Enforcement] ===
            try:
                all_positions = await asyncio.to_thread(self.exchange.fetch_positions)
                for p in all_positions:
                    normalized_pos = self._normalize_server_position(p)
                    if normalized_pos:
                        active_sym = normalize_futures_market_id(normalized_pos.get('symbol'))
                        target_sym = normalize_futures_market_id(symbol)

                        if active_sym != target_sym:
                            logger.warning(f"?슟 [Single Limit] Entry blocked: Already holding {normalized_pos['symbol']}")
                            await self.ctrl.notify(f"⚠️ **진입 차단**: 단일 포지션 제한 (보유 중: {normalized_pos['symbol']})")
                            return
            except Exception as e:
                logger.error(f"Single position check failed: {e}")
                return

            # 1. ?먯궛 ?뺤씤
            total, free, _ = await self.get_balance_info()
            if total <= 0: return

            # 2. ?ъ옄 鍮꾩쨷 (Risk %) - 怨듯넻 ?ㅼ젙 ?ъ슜
            risk_pct = normalize_risk_percent(common_cfg)
            leverage = common_cfg.get('leverage', 5)

            # USDT ?ъ엯 湲덉븸 怨꾩궛
            invest_amount = (total * (risk_pct / 100.0)) * leverage

            # ?섎웾 怨꾩궛
            quantity = invest_amount / price
            amount_str = self.safe_amount(symbol, quantity)
            price_str = self.safe_price(symbol, price) # Limit 二쇰Ц??(?꾩옱媛)

            logger.info(f"?뮥 TEMA Entry: {side.upper()} {symbol} Qty={amount_str} Price={price_str} (Lev {leverage}x)")

            # 3. 二쇰Ц ?꾩넚
            # [Enforce] Market Settings
            await self.ensure_market_settings(symbol, leverage=leverage)

            params = {'leverage': leverage}

            signal_timestamp = _resolve_crypto_signal_timestamp(
                self,
                symbol,
                {'entry_price': price, 'timeframe': common_cfg.get('timeframe', '1m')},
            )
            submission = await self.submit_futures_entry(
                strategy='TEMA',
                symbol=symbol,
                side=side,
                signal_timestamp=signal_timestamp,
                qty=float(amount_str),
                params=params,
            )
            if not submission.accepted:
                raise RuntimeError(
                    f"TEMA safe entry rejected: {submission.state} {submission.error}"
                )
            order = submission.order or {}
            position = submission.position or await self.get_server_position(
                symbol,
                use_cache=False,
            )

            await self.ctrl.notify(f"✅ **TEMA 진입**: {symbol} {side.upper()}\n가격: {price}\n수량: {amount_str}")

            # 4. TP/SL ?ㅼ젙 (怨듯넻 ?ㅼ젙 ?ъ슜)
            tp_master_enabled = bool(common_cfg.get('tp_sl_enabled', False))
            tp_enabled = tp_master_enabled and bool(common_cfg.get('take_profit_enabled', True))
            sl_enabled = tp_master_enabled and bool(common_cfg.get('stop_loss_enabled', True))
            if tp_enabled or sl_enabled:
                roe_target = common_cfg.get('target_roe_pct', 20.0) / 100.0
                stop_loss = common_cfg.get('stop_loss_pct', 10.0) / 100.0

                # 二쇰Ц 泥닿껐媛 湲곗? TP/SL 怨꾩궛
                entry_price = float(order['average']) if order.get('average') else price

                if side == 'long':
                    tp_price = entry_price * (1 + roe_target/leverage)
                    sl_price = entry_price * (1 - stop_loss/leverage)
                else:
                    tp_price = entry_price * (1 - roe_target/leverage)
                    sl_price = entry_price * (1 + stop_loss/leverage)

                # 諛붿씠?몄뒪 湲곗? TP/SL 二쇰Ц (STOP_MARKET / TAKE_PROFIT_MARKET)
                try:
                    if tp_enabled:
                        params_tp = {
                            'stopPrice': self.safe_price(symbol, tp_price),
                            'reduceOnly': True
                        }
                        await self.place_futures_protection(
                            'tp',
                            symbol=symbol,
                            side='sell' if side == 'long' else 'buy',
                            qty=float(amount_str),
                            trigger_price=params_tp['stopPrice'],
                            order_type='take_profit_market',
                            label='TP',
                            position=position,
                            entry_client_order_id=submission.client_order_id,
                            config=common_cfg,
                        )

                    if sl_enabled:
                        params_sl = {
                            'stopPrice': self.safe_price(symbol, sl_price),
                            'reduceOnly': True
                        }
                        await self.place_futures_protection(
                            'sl',
                            symbol=symbol,
                            side='sell' if side == 'long' else 'buy',
                            qty=float(amount_str),
                            trigger_price=params_sl['stopPrice'],
                            order_type='stop_market',
                            label='SL',
                            position=position,
                            entry_client_order_id=submission.client_order_id,
                            config=common_cfg,
                        )

                    logger.info(f"??Protective orders placed: TP={'ON' if tp_enabled else 'OFF'}, SL={'ON' if sl_enabled else 'OFF'}")
                except Exception as e:
                    logger.error(f"Failed to place TP/SL order: {e}")
                    await self.ctrl.notify(f"⚠️ TP/SL 주문 실패: {e}")
                    signal_engine = getattr(self.ctrl, 'engines', {}).get('signal')
                    if signal_engine is not None:
                        await signal_engine._emergency_close_position_without_stop_loss(
                            symbol,
                            reason=f'TEMA protection placement failed: {e}',
                        )

        except Exception as e:
            logger.error(f"TEMA entry failed: {e}")
            await self.ctrl.notify(f"❌ 진입 실패: {e}")

    async def _resolve_closed_trade_accounting(self, symbol, open_trade, *, exit_price=None, state=None):
        return await resolve_closed_trade_accounting(
            self,
            symbol,
            open_trade,
            exit_price=exit_price,
            state=state,
        )

    async def _record_closed_trade_accounting(self, symbol, reason, *, exit_price=None, state=None):
        return await record_closed_trade_accounting(
            self,
            symbol,
            reason,
            exit_price=exit_price,
            state=state,
            record_realized_pnl=record_bot_realized_pnl,
            persist_live_trade=record_live_trade_result,
            ensure_trading_runtime=_ensure_trading_safety_runtime,
        )

    async def exit_position(self, symbol, reason):
        try:
            pos = await self.get_server_position(symbol, use_cache=False)
            if not pos: return

            amount = abs(float(pos.get('contracts', 0) or 0))
            pos_side = str(pos.get('side', '')).lower()

            if amount > 0:
                await self.submit_futures_close(
                    strategy='TEMA',
                    symbol=symbol,
                    position_side=pos_side,
                    position_signature=pos.get('timestamp') or pos.get('entryPrice'),
                    qty=amount,
                    reason=reason,
                    order_purpose=f'close_{str(reason).lower().replace(" ", "_")}',
                )
                await self.ctrl.notify(f"🔄 **TEMA 청산**: {symbol} ({reason})")
        except Exception as e:
            logger.error(f"TEMA exit failed: {e}")

class ShannonEngine(BaseEngine):
    def __init__(self, controller):
        super().__init__(controller)
        self.last_logic_time = 0
        self.last_indicator_update = 0
        self.ratio = controller.cfg.get('shannon_engine', {}).get('asset_allocation', {}).get('target_ratio', 0.5)
        self.grid_orders = []

        # 吏??罹먯떆
        self.ema_200 = None
        self.atr_value = None
        self.trend_direction = None  # 'long', 'short', or None
        self.INDICATOR_UPDATE_INTERVAL = 10  # 10珥덈쭏??吏??媛깆떊

    def start(self):
        super().start()
        # ?ъ떆????吏??罹먯떆 珥덇린??
        self.last_logic_time = 0
        self.last_indicator_update = 0
        self.ema_200 = None
        self.atr_value = None
        self.trend_direction = None
        logger.info(f"?? [Shannon] Engine started and cache cleared")

    async def poll_tick(self):
        """
        [?쒖닔 ?대쭅] Shannon ?붿쭊 硫붿씤 ?대쭅 ?⑥닔
        - ?꾩옱 媛寃?議고쉶
        - 由щ갭?곗떛 濡쒖쭅 ?ㅽ뻾
        """
        if not self.running:
            return

        try:
            symbol = self._get_target_symbol()

            # ?꾩옱 媛寃?議고쉶 (ticker ?ъ슜)
            ticker = await asyncio.to_thread(self.exchange.fetch_ticker, symbol)
            price = float(ticker['last'])

            # 由щ갭?곗떛 濡쒖쭅 ?ㅽ뻾 (1珥?媛꾧꺽 ?쒗븳)
            if time.time() - self.last_logic_time > 1.0:
                await self.logic(symbol, price)
                self.last_logic_time = time.time()

        except Exception as e:
            logger.error(f"Shannon poll_tick error: {e}")

    def _get_target_symbol(self):
        """config?먯꽌 target_symbol 媛?몄삤湲?"""
        return self.cfg.get('shannon_engine', {}).get('target_symbol', 'BTC/USDT')

    async def update_indicators(self, symbol):
        """200 EMA? ATR ?낅뜲?댄듃"""
        now = time.time()

        # 泥??ㅽ뻾?닿굅??罹먯떆 留뚮즺 ???낅뜲?댄듃
        # trend_direction??None?대㈃ 媛뺤젣 ?낅뜲?댄듃 (泥?吏꾩엯 ?꾪빐)
        if self.trend_direction is not None and now - self.last_indicator_update < self.INDICATOR_UPDATE_INTERVAL:
            return  # 罹먯떆 ?ъ슜

        try:
            cfg = self.cfg.get('shannon_engine', {})
            # Shannon??timeframe ?ㅼ젙???놁쑝硫?signal_engine ?ㅼ젙 ?ъ슜
            tf = cfg.get('timeframe') or self.cfg.get('signal_engine', {}).get('common_settings', {}).get('timeframe', '15m')

            # OHLCV ?곗씠??媛?몄삤湲?(200 EMA 怨꾩궛???꾪빐 理쒖냼 250媛?
            ohlcv = await asyncio.to_thread(
                self.exchange.fetch_ohlcv, symbol, tf, limit=250
            )
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

            # 200 EMA 怨꾩궛
            trend_cfg = cfg.get('trend_filter', {})
            if trend_cfg.get('enabled', True):
                ema_period = trend_cfg.get('ema_period', 200)
                df['ema'] = ta.ema(df['close'], length=ema_period)
                self.ema_200 = df['ema'].iloc[-1]
                # trend_direction? logic()?먯꽌 ?ㅼ떆媛?媛寃⑹쑝濡?寃곗젙
                logger.info(f"?뱤 {tf} 200 EMA ?낅뜲?댄듃: {self.ema_200:.2f}")

            # ATR 怨꾩궛
            atr_cfg = cfg.get('atr_settings', {})
            if atr_cfg.get('enabled', True):
                atr_period = atr_cfg.get('period', 14)
                df['atr'] = ta.atr(df['high'], df['low'], df['close'], length=atr_period)
                self.atr_value = df['atr'].iloc[-1]
                logger.info(f"?뱤 ATR({atr_period}): {self.atr_value:.2f}")

            self.last_indicator_update = now

        except Exception as e:
            logger.error(f"Indicator update error: {e}")

    def get_drawdown_multiplier(self, total_equity, daily_pnl):
        """?쒕줈?곕떎??湲곕컲 由ъ뒪???뱀닔 怨꾩궛"""
        cfg = self.cfg.get('shannon_engine', {}).get('drawdown_protection', {})

        if not cfg.get('enabled', True):
            return 1.0

        threshold_pct = cfg.get('threshold_pct', 3.0)
        reduction_factor = cfg.get('reduction_factor', 0.5)

        # ?쇱씪 ?먯떎瑜?怨꾩궛
        if total_equity > 0:
            daily_loss_pct = abs(min(0, daily_pnl)) / total_equity * 100
        else:
            daily_loss_pct = 0

        if daily_loss_pct >= threshold_pct:
            logger.warning(f"?좑툘 Drawdown protection: {daily_loss_pct:.2f}% loss ??{reduction_factor*100:.0f}% size")
            return reduction_factor

        return 1.0

    async def logic(self, symbol, price):
        try:
            # 吏???낅뜲?댄듃
            await self.update_indicators(symbol)

            total, free, mmr = await self.get_balance_info()
            count, daily_pnl = self.db.get_daily_stats()
            pos = await self.get_server_position(symbol)

            # ?쒕줈?곕떎???뱀닔 怨꾩궛
            dd_multiplier = self.get_drawdown_multiplier(total, daily_pnl)

            coin_amt = float(pos['contracts']) if pos else 0.0
            coin_val = abs(coin_amt * price)
            diff = coin_val - (total * self.ratio)
            diff_pct = (diff / total * 100) if total > 0 else 0

            # ?곹깭 ?곗씠???낅뜲?댄듃 (Symbol Keyed)
            self.ctrl.status_data[symbol] = {
                'engine': 'Shannon', 'symbol': symbol, 'price': price,
                'total_equity': total, 'free_usdt': free, 'mmr': mmr,
                'daily_count': count, 'daily_pnl': daily_pnl,
                'ratio': self.ratio, 'diff_pct': diff_pct, 'coin_val': coin_val,
                'pos_side': pos['side'].upper() if pos else 'NONE',
                'entry_price': float(pos['entryPrice']) if pos else 0.0,
                'coin_amt': coin_amt,
                'pnl_pct': float(pos['percentage']) if pos else 0.0,
                'pnl_usdt': float(pos['unrealizedPnl']) if pos else 0.0,
                'grid_orders': len(self.grid_orders),
                'ema_200': self.ema_200,
                'atr': self.atr_value,
                'trend': self.trend_direction,
                'dd_multiplier': dd_multiplier
            }

            # MMR 寃쎄퀬 泥댄겕
            await self.check_mmr_alert(mmr)

            if self.ctrl.is_paused:
                return

            # ?쇱씪 ?먯떎 ?쒕룄 泥댄겕
            if await self.check_daily_loss_limit():
                return

            cfg = self.cfg.get('shannon_engine', {})
            trend_cfg = cfg.get('trend_filter', {})

            # ============ ?ㅼ떆媛?媛寃?湲곗? 異붿꽭 諛⑺뼢 寃곗젙 ============
            if trend_cfg.get('enabled', True) and self.ema_200 is not None:
                if price > self.ema_200:
                    self.trend_direction = 'long'
                else:
                    self.trend_direction = 'short'
                logger.info(f"?뱤 ?꾩옱媛 {price:.2f} vs 200 EMA {self.ema_200:.2f} ??{self.trend_direction.upper()}")

            # ============ 珥덇린 吏꾩엯 濡쒖쭅 (200 EMA 湲곕컲) ============
            # ?ъ??섏씠 ?놁쓣 ??200 EMA 諛⑺뼢?쇰줈 珥덇린 吏꾩엯
            if not pos and trend_cfg.get('enabled', True) and self.trend_direction:
                # 留ㅻℓ 諛⑺뼢 ?꾪꽣 ?곸슜
                d_mode = self.cfg.get('system_settings', {}).get('trade_direction', 'both')
                if (d_mode == 'long' and self.trend_direction == 'short') or \
                   (d_mode == 'short' and self.trend_direction == 'long'):
                    logger.info(f"??[Shannon] Entry blocked by direction filter: {d_mode}")
                    return  # ?꾪꽣???섑빐 吏꾩엯 李⑤떒

                target_value = total * self.ratio * dd_multiplier  # 紐⑺몴 50%
                entry_qty = self.safe_amount(symbol, target_value / price)

                if float(entry_qty) > 0:
                    # ?덈쾭由ъ? ?ㅼ젙 ?곸슜
                    lev = cfg.get('leverage', 5)
                    await asyncio.to_thread(self.exchange.set_leverage, lev, symbol)

                    if self.trend_direction == 'long':
                        # 200 EMA ????濡?吏꾩엯
                        order = await self.submit_futures_entry(
                            strategy='SHANNON',
                            symbol=symbol,
                            side='long',
                            signal_timestamp=_resolve_crypto_signal_timestamp(
                                self, symbol, {'entry_price': price}
                            ),
                            qty=float(entry_qty),
                        )
                        self.position_cache = None
                        self.position_cache_time = 0
                        await self.ctrl.notify(f"✅ [Shannon] LONG 진입 {entry_qty} @ {price:.2f} (200 EMA 상향)")
                        self.db.log_shannon(total, "ENTRY_LONG", price, float(entry_qty), total)
                        logger.info(f"Shannon initial LONG entry: {order}")
                        return

                    elif self.trend_direction == 'short':
                        # 200 EMA ?꾨옒 ????吏꾩엯
                        order = await self.submit_futures_entry(
                            strategy='SHANNON',
                            symbol=symbol,
                            side='short',
                            signal_timestamp=_resolve_crypto_signal_timestamp(
                                self, symbol, {'entry_price': price}
                            ),
                            qty=float(entry_qty),
                        )
                        self.position_cache = None
                        self.position_cache_time = 0
                        await self.ctrl.notify(f"✅ [Shannon] SHORT 진입 {entry_qty} @ {price:.2f} (200 EMA 하향)")
                        self.db.log_shannon(total, "ENTRY_SHORT", price, float(entry_qty), total)
                        logger.info(f"Shannon initial SHORT entry: {order}")
                        return

            # ============ 異붿꽭 諛섏쟾 媛먯?: ?ъ???泥?궛 ???ъ쭊??============
            reversed_position = False
            if pos and trend_cfg.get('enabled', True) and self.trend_direction:
                current_side = pos['side']
                # 濡??ъ??섏씤??異붿꽭媛 ?섎씫?쇰줈 ?꾪솚
                if current_side == 'long' and self.trend_direction == 'short':
                    await self._close_and_reverse(symbol, pos, price, 'short', total, dd_multiplier)
                    reversed_position = True
                    # ?ъ???蹂寃????ㅼ떆 議고쉶
                    pos = await self.get_server_position(symbol, use_cache=False)
                # ???ъ??섏씤??異붿꽭媛 ?곸듅?쇰줈 ?꾪솚
                elif current_side == 'short' and self.trend_direction == 'long':
                    await self._close_and_reverse(symbol, pos, price, 'long', total, dd_multiplier)
                    reversed_position = True
                    pos = await self.get_server_position(symbol, use_cache=False)

            # ============ Grid Trading (ATR 湲곕컲 媛꾧꺽) ============
            grid_cfg = cfg.get('grid_trading', {})
            if grid_cfg.get('enabled', False):
                await self.manage_grid_orders(symbol, price, grid_cfg, dd_multiplier)

            # ============ Rebalance (鍮꾩쑉 ?좎?) ============
            threshold = cfg.get('asset_allocation', {}).get('allowed_deviation_pct', 2.0)
            if abs(diff_pct) > threshold and pos:
                current_side = pos['side']
                contracts = abs(float(pos['contracts']))
                target_contracts = (total * self.ratio) / price  # 紐⑺몴 怨꾩빟 ??

                if current_side == 'long':
                    # 濡??ъ???由щ갭?곗떛
                    if contracts > target_contracts:
                        # ?ъ???怨쇰떎 ???쇰? 泥?궛 (留ㅻ룄)
                        reduce_qty = self.safe_amount(symbol, (contracts - target_contracts) * dd_multiplier)
                        if float(reduce_qty) > 0:
                            order = await self.submit_futures_close(
                                strategy='SHANNON_REBALANCE',
                                symbol=symbol,
                                position_side='long',
                                position_signature=pos.get('timestamp') or pos.get('entryPrice'),
                                qty=float(reduce_qty),
                                reason='rebalance_reduce',
                            )
                            self.position_cache = None
                            self.position_cache_time = 0
                            await self.ctrl.notify(f"?뽳툘 [Long] 異뺤냼: SELL {reduce_qty}")
                            logger.info(f"Rebalance SELL: {order}")
                    else:
                        # ?ъ???遺議???異붽? 留ㅼ닔 (異붿꽭 ?꾪꽣: 濡?異붿꽭???뚮쭔)
                        if self.trend_direction == 'long':
                            add_qty = self.safe_amount(symbol, (target_contracts - contracts) * dd_multiplier)
                            if float(add_qty) > 0:
                                order = await self.crypto_execution.submit_position_add(
                                    strategy='SHANNON_REBALANCE',
                                    symbol=symbol,
                                    side='long',
                                    signal_timestamp=_resolve_crypto_signal_timestamp(
                                        self, symbol, {'entry_price': price}
                                    ),
                                    qty=float(add_qty),
                                    stage='rebalance_long',
                                )
                                self.position_cache = None
                                self.position_cache_time = 0
                                await self.ctrl.notify(f"?뽳툘 [Long] ?뺣?: BUY {add_qty}")
                                logger.info(f"Rebalance BUY: {order}")
                        else:
                            logger.info(f"??[Long] ?뺣? 李⑤떒: 異붿꽭媛 {self.trend_direction}")
                else:
                    # ???ъ???由щ갭?곗떛
                    if contracts > target_contracts:
                        # ??怨쇰떎 ???쇰? 泥?궛 (留ㅼ닔濡?而ㅻ쾭)
                        reduce_qty = self.safe_amount(symbol, (contracts - target_contracts) * dd_multiplier)
                        if float(reduce_qty) > 0:
                            order = await self.submit_futures_close(
                                strategy='SHANNON_REBALANCE',
                                symbol=symbol,
                                position_side='short',
                                position_signature=pos.get('timestamp') or pos.get('entryPrice'),
                                qty=float(reduce_qty),
                                reason='rebalance_reduce',
                            )
                            self.position_cache = None
                            self.position_cache_time = 0
                            await self.ctrl.notify(f"?뽳툘 [Short] 異뺤냼: BUY {reduce_qty}")
                            logger.info(f"Rebalance BUY (cover): {order}")
                    else:
                        # ??遺議???異붽? 留ㅻ룄 (異붿꽭 ?꾪꽣: ??異붿꽭???뚮쭔)
                        if self.trend_direction == 'short':
                            add_qty = self.safe_amount(symbol, (target_contracts - contracts) * dd_multiplier)
                            if float(add_qty) > 0:
                                order = await self.crypto_execution.submit_position_add(
                                    strategy='SHANNON_REBALANCE',
                                    symbol=symbol,
                                    side='short',
                                    signal_timestamp=_resolve_crypto_signal_timestamp(
                                        self, symbol, {'entry_price': price}
                                    ),
                                    qty=float(add_qty),
                                    stage='rebalance_short',
                                )
                                self.position_cache = None
                                self.position_cache_time = 0
                                await self.ctrl.notify(f"?뽳툘 [Short] ?뺣?: SELL {add_qty}")
                                logger.info(f"Rebalance SELL (add short): {order}")
                        else:
                            logger.info(f"??[Short] ?뺣? 李⑤떒: 異붿꽭媛 {self.trend_direction}")

                self.db.log_shannon(total, "REBAL", price, coin_amt, total)

        except Exception as e:
            logger.error(f"Shannon logic error: {e}")

    async def _close_and_reverse(self, symbol, pos, price, new_direction, total, dd_multiplier):
        """異붿꽭 諛섏쟾 ???ъ???泥?궛 ??諛섎? 諛⑺뼢 吏꾩엯"""
        try:
            # 留ㅻℓ 諛⑺뼢 ?꾪꽣 泥댄겕
            d_mode = self.cfg.get('system_settings', {}).get('trade_direction', 'both')
            if (d_mode == 'long' and new_direction == 'short') or \
               (d_mode == 'short' and new_direction == 'long'):
                # 諛⑺뼢 ?꾪꽣???섑빐 ?ъ쭊??遺덇? ??泥?궛留?
                close_qty = self.safe_amount(symbol, abs(float(pos['contracts'])))
                close_result = await self.submit_futures_close(
                    strategy='SHANNON_REVERSE',
                    symbol=symbol,
                    position_side=pos['side'],
                    position_signature=pos.get('timestamp') or pos.get('entryPrice'),
                    qty=float(close_qty),
                    reason='direction_filter_close',
                )
                if close_result.state != OrderState.CLOSED.value:
                    logger.warning("Shannon close not confirmed; reverse entry blocked")
                    return
                pnl = float(pos.get('unrealizedPnl', 0))
                await self.ctrl.notify(f"🔄 [Shannon] {pos['side'].upper()} 청산 (방향 필터) PnL: {pnl:+.2f}")
                self.position_cache = None
                self.position_cache_time = 0
                return

            # 湲곗〈 ?ъ???泥?궛
            close_qty = self.safe_amount(symbol, abs(float(pos['contracts'])))

            close_result = await self.submit_futures_close(
                strategy='SHANNON_REVERSE',
                symbol=symbol,
                position_side=pos['side'],
                position_signature=pos.get('timestamp') or pos.get('entryPrice'),
                qty=float(close_qty),
                reason='trend_reverse_close',
            )
            if close_result.state != OrderState.CLOSED.value:
                logger.warning("Shannon reverse blocked until close is confirmed CLOSED")
                return

            pnl = float(pos.get('unrealizedPnl', 0))
            await self.ctrl.notify(f"🔄 [Shannon] {pos['side'].upper()} 청산 (추세 반전) PnL: {pnl:+.2f}")

            # 泥?궛 ?꾨즺 ?湲?
            await asyncio.sleep(2.0)  # 2珥??湲?

            # 諛섎? 諛⑺뼢 ???ъ???吏꾩엯
            target_value = total * self.ratio * dd_multiplier
            entry_qty = self.safe_amount(symbol, target_value / price)

            if float(entry_qty) > 0:
                await self.submit_futures_entry(
                    strategy='SHANNON_REVERSE',
                    symbol=symbol,
                    side=new_direction,
                    signal_timestamp=_resolve_crypto_signal_timestamp(
                        self, symbol, {'entry_price': price}
                    ),
                    qty=float(entry_qty),
                )
                self.position_cache = None
                self.position_cache_time = 0
                await self.ctrl.notify(f"✅ [Shannon] {new_direction.upper()} 진입 {entry_qty} @ {price:.2f}")
                self.db.log_shannon(total, f"REVERSE_{new_direction.upper()}", price, float(entry_qty), total)

        except Exception as e:
            logger.error(f"Shannon close and reverse error: {e}")

    async def manage_grid_orders(self, symbol, price, grid_cfg, dd_multiplier=1.0):
        """Grid Trading 濡쒖쭅 - ATR 湲곕컲 ?숈쟻 媛꾧꺽"""
        try:
            levels = grid_cfg.get('grid_levels', 5)
            base_order_size = grid_cfg.get('order_size_usdt', 20)

            # ?쒕줈?곕떎??蹂댄샇 ?곸슜
            order_size = base_order_size * dd_multiplier

            # ?덈쾭由ъ? ?ㅼ젙 ?뺤씤 (Grid 二쇰Ц?먮룄 ?숈씪?섍쾶 ?곸슜)
            lev = self.cfg.get('shannon_engine', {}).get('leverage', 5)
            try:
                await asyncio.to_thread(self.exchange.set_leverage, lev, symbol)
            except Exception:
                pass  # ?대? ?ㅼ젙??寃쎌슦 臾댁떆

            # ATR 湲곕컲 洹몃━??媛꾧꺽 怨꾩궛
            atr_cfg = self.cfg.get('shannon_engine', {}).get('atr_settings', {})
            if atr_cfg.get('enabled', True) and self.atr_value:
                atr_multiplier = atr_cfg.get('grid_multiplier', 0.5)
                # ATR 湲곕컲 媛꾧꺽 (媛寃??鍮?鍮꾩쑉)
                step_pct = (self.atr_value * atr_multiplier) / price
                logger.debug(f"ATR Grid Step: {step_pct*100:.3f}%")
            else:
                # ATR ?놁쑝硫?湲곕낯媛??ъ슜
                step_pct = 0.005  # 0.5%

            # 200 EMA 諛⑺뼢 ?꾪꽣 ?곸슜
            trend_cfg = self.cfg.get('shannon_engine', {}).get('trend_filter', {})
            allow_buy = True
            allow_sell = True

            if trend_cfg.get('enabled', True) and self.trend_direction:
                if self.trend_direction == 'short':
                    allow_buy = False  # ?섎씫 異붿꽭: 留ㅼ닔 二쇰Ц 湲덉?
                elif self.trend_direction == 'long':
                    allow_sell = False  # ?곸듅 異붿꽭: 留ㅻ룄 二쇰Ц 湲덉?

            # 湲곗〈 洹몃━??二쇰Ц ?곹깭 ?뺤씤
            try:
                open_orders = await asyncio.to_thread(self.exchange.fetch_open_orders, symbol)
            except Exception as e:
                logger.error(f"Fetch open orders error: {e}")
                return

            # #5 Fix: ?꾩옱 媛寃⑹뿉???덈Т 硫?댁쭊 二쇰Ц 痍⑥냼 (媛寃⑹쓽 5% ?댁긽)
            max_distance_pct = 0.05
            for order in open_orders:
                try:
                    order_price = float(order['price'])
                    distance = abs(order_price - price) / price
                    if distance > max_distance_pct:
                        await asyncio.to_thread(self.exchange.cancel_order, order['id'], symbol)
                        logger.info(f"Grid order cancelled (too far): {order['side']} @ {order_price:.2f} ({distance*100:.1f}% away)")
                except Exception as e:
                    logger.error(f"Cancel distant order error: {e}")

            # ?꾩옱 ?ъ????뺤씤 (#10 Fix)
            pos = await self.get_server_position(symbol, use_cache=True)
            has_long_position = pos and pos['side'] == 'long' and float(pos['contracts']) > 0
            has_short_position = pos and pos['side'] == 'short' and abs(float(pos['contracts'])) > 0

            # 洹몃━??二쇰Ц 諛⑺뼢 寃곗젙 (?ъ???湲곕컲)
            # - 濡??ъ??? 留ㅻ룄 二쇰Ц ?덉슜 (?듭젅/泥?궛??, 留ㅼ닔 二쇰Ц???덉슜 (異붽? 吏꾩엯)
            # - ???ъ??? 留ㅼ닔 二쇰Ц ?덉슜 (?듭젅/泥?궛??, 留ㅻ룄 二쇰Ц???덉슜 (異붽? 吏꾩엯)
            # - ?ъ????놁쓬: 異붿꽭 諛⑺뼢?쇰줈留?二쇰Ц
            if has_long_position:
                allow_sell = True  # 濡??ъ???泥?궛??留ㅻ룄 ?덉슜
                # 濡??ъ??섏뿉??異붽? 留ㅼ닔??異붿꽭 ?꾪꽣 ?곕쫫
            elif has_short_position:
                allow_buy = True  # ???ъ???泥?궛??留ㅼ닔 ?덉슜
                # ???ъ??섏뿉??異붽? 留ㅼ닔??異붿꽭 ?꾪꽣 ?곕쫫
            else:
                # ?ъ????놁쓣 ?? 異붿꽭 諛⑺뼢??諛섑븯??二쇰Ц 湲덉?
                if self.trend_direction == 'short':
                    allow_buy = False  # ??異붿꽭: 留ㅼ닔 湲덉?
                elif self.trend_direction == 'long':
                    allow_sell = False  # 濡?異붿꽭: 留ㅻ룄 湲덉?

            # 洹몃━??二쇰Ц??遺議깊븯硫??덈줈 ?앹꽦
            if len(open_orders) < levels * 2:
                for i in range(1, levels + 1):
                    # 留ㅼ닔 二쇰Ц (?꾩옱媛 ?꾨옒)
                    buy_price = price * (1 - step_pct * i)
                    buy_qty = self.safe_amount(symbol, order_size / buy_price)

                    # 留ㅻ룄 二쇰Ц (?꾩옱媛 ??
                    sell_price = price * (1 + step_pct * i)
                    sell_qty = self.safe_amount(symbol, order_size / sell_price)

                    # 以묐났 泥댄겕
                    buy_exists = any(
                        abs(float(o['price']) - buy_price) / buy_price < 0.002
                        for o in open_orders if o['side'] == 'buy'
                    )
                    sell_exists = any(
                        abs(float(o['price']) - sell_price) / sell_price < 0.002
                        for o in open_orders if o['side'] == 'sell'
                    )

                    # 留ㅼ닔 二쇰Ц (異붿꽭 ?꾪꽣 ?곸슜)
                    if allow_buy and not buy_exists and float(buy_qty) > 0:
                        try:
                            if has_short_position:
                                await self.submit_futures_close(
                                    strategy='SHANNON_GRID',
                                    symbol=symbol,
                                    position_side='short',
                                    position_signature=pos.get('timestamp') or pos.get('entryPrice'),
                                    qty=float(buy_qty),
                                    reason=f'grid_exit_{i}',
                                    order_intent='GRID_EXIT',
                                    order_type='limit',
                                    price=float(self.safe_price(symbol, buy_price)),
                                    leg=f'buy_{i:02d}',
                                )
                            else:
                                await self.crypto_execution.submit_limit_entry(
                                    strategy='SHANNON_GRID',
                                    symbol=symbol,
                                    side='long',
                                    signal_timestamp=_resolve_crypto_signal_timestamp(
                                        self, symbol, {'entry_price': price}
                                    ),
                                    grid_leg=f'buy_{i:02d}',
                                    qty=float(buy_qty),
                                    price=float(self.safe_price(symbol, buy_price)),
                                    position_add=bool(has_long_position),
                                )
                            logger.info(f"Grid BUY: {buy_qty} @ {buy_price:.2f} (ATR step: {step_pct*100:.2f}%)")
                        except Exception as e:
                            logger.error(f"Grid buy order error: {e}")

                    # 留ㅻ룄 二쇰Ц (異붿꽭 ?꾪꽣 + ?ъ???泥댄겕 ?곸슜)
                    if allow_sell and not sell_exists and float(sell_qty) > 0:
                        try:
                            if has_long_position:
                                await self.submit_futures_close(
                                    strategy='SHANNON_GRID',
                                    symbol=symbol,
                                    position_side='long',
                                    position_signature=pos.get('timestamp') or pos.get('entryPrice'),
                                    qty=float(sell_qty),
                                    reason=f'grid_exit_{i}',
                                    order_intent='GRID_EXIT',
                                    order_type='limit',
                                    price=float(self.safe_price(symbol, sell_price)),
                                    leg=f'sell_{i:02d}',
                                )
                            else:
                                await self.crypto_execution.submit_limit_entry(
                                    strategy='SHANNON_GRID',
                                    symbol=symbol,
                                    side='short',
                                    signal_timestamp=_resolve_crypto_signal_timestamp(
                                        self, symbol, {'entry_price': price}
                                    ),
                                    grid_leg=f'sell_{i:02d}',
                                    qty=float(sell_qty),
                                    price=float(self.safe_price(symbol, sell_price)),
                                    position_add=bool(has_short_position),
                                )
                            logger.info(f"Grid SELL: {sell_qty} @ {sell_price:.2f} (ATR step: {step_pct*100:.2f}%)")
                        except Exception as e:
                            logger.error(f"Grid sell order error: {e}")

            self.grid_orders = open_orders

        except Exception as e:
            logger.error(f"Grid trading error: {e}")

class DualThrustEngine(BaseEngine):
    """????몃윭?ㅽ듃 蹂?숈꽦 ?뚰뙆 ?꾨왂"""
    def __init__(self, controller):
        super().__init__(controller)
        self.last_heartbeat = 0
        self.last_trigger_update = 0
        self.consecutive_errors = 0

        # ?몃━嫄?罹먯떆
        self.today_open = None
        self.range_value = None
        self.long_trigger = None
        self.short_trigger = None
        self.trigger_date = None  # ?몃━嫄?怨꾩궛 ?좎쭨 (?ㅻ쾭?섏엲 由ъ뀑??

        self.TRIGGER_UPDATE_INTERVAL = 60  # 60珥덈쭏??泥댄겕 (??蹂寃??뺤씤)

    def start(self):
        super().start()
        # ?ъ떆?????몃━嫄??뺣낫 珥덇린??
        self.last_heartbeat = 0
        self.last_trigger_update = 0
        self.trigger_date = None
        self.long_trigger = None
        self.short_trigger = None
        logger.info(f"?? [DualThrust] Engine started and triggers reset")

    def _get_target_symbol(self):
        return self.cfg.get('dual_thrust_engine', {}).get('target_symbol', 'BTC/USDT')

    async def poll_tick(self):
        """[?쒖닔 ?대쭅] Dual Thrust 硫붿씤 ?대쭅"""
        if not self.running:
            return

        try:
            symbol = self._get_target_symbol()

            # ?꾩옱 媛寃?議고쉶
            ticker = await asyncio.to_thread(self.exchange.fetch_ticker, symbol)
            price = float(ticker['last'])

            # ?몃━嫄??낅뜲?댄듃 (?섎（ 蹂寃???媛깆떊)
            await self._update_triggers(symbol)

            # ?섑듃鍮꾪듃 (30珥덈쭏??
            now = time.time()
            if now - self.last_heartbeat > 30:
                self.last_heartbeat = now
                pos_side = self.ctrl.status_data.get('pos_side', 'UNKNOWN') if self.ctrl.status_data else 'UNKNOWN'
                logger.info(f"?뮄 [DualThrust] Heartbeat: running={self.running}, paused={self.ctrl.is_paused}, pos={pos_side}, price={price:.2f}")
                if self.long_trigger and self.short_trigger:
                    logger.info(f"?뱤 [DualThrust] Triggers: Long={self.long_trigger:.2f}, Short={self.short_trigger:.2f}, Range={self.range_value:.2f}")

            # ?곹깭 ?낅뜲?댄듃 + 留ㅻℓ 濡쒖쭅
            await self._logic(symbol, price)
            self.consecutive_errors = 0

        except Exception as e:
            self.consecutive_errors += 1
            logger.error(f"DualThrust poll_tick error ({self.consecutive_errors}x): {e}")
            if self.consecutive_errors > 10:
                self.position_cache = None
                self.position_cache_time = 0
                self.consecutive_errors = 0

    async def _update_triggers(self, symbol):
        """N???쇰큺?쇰줈 Range 怨꾩궛 諛??몃━嫄??낅뜲?댄듃"""
        now = datetime.now(timezone.utc)
        today_str = now.strftime('%Y-%m-%d')

        # ?대? ?ㅻ뒛 怨꾩궛?덉쑝硫??ㅽ궢
        if self.trigger_date == today_str and self.long_trigger and self.short_trigger:
            return

        try:
            cfg = self.cfg.get('dual_thrust_engine', {})
            n_days = cfg.get('n_days', 4)
            k1 = cfg.get('k1', 0.5)
            k2 = cfg.get('k2', 0.5)

            # ?쇰큺 ?곗씠??媛?몄삤湲?(N+1媛? 怨쇨굅 N??+ ?ㅻ뒛)
            ohlcv = await asyncio.to_thread(
                self.exchange.fetch_ohlcv, symbol, '1d', limit=n_days + 1
            )

            if len(ohlcv) < n_days + 1:
                logger.warning(f"[DualThrust] Insufficient daily data: {len(ohlcv)} < {n_days + 1}")
                return

            # 怨쇨굅 N???곗씠??(?ㅻ뒛 ?쒖쇅)
            past_n = ohlcv[:-1][-n_days:]

            # HH, HC, LC, LL 怨꾩궛
            highs = [candle[2] for candle in past_n]
            lows = [candle[3] for candle in past_n]
            closes = [candle[4] for candle in past_n]

            hh = max(highs)  # Highest High
            hc = max(closes)  # Highest Close
            lc = min(closes)  # Lowest Close
            ll = min(lows)  # Lowest Low

            # Range = max(HH - LC, HC - LL)
            self.range_value = max(hh - lc, hc - ll)

            # ?뱀씪 ?쒓? (?ㅻ뒛 罹붾뱾)
            self.today_open = ohlcv[-1][1]

            # ?몃━嫄?怨꾩궛
            self.long_trigger = self.today_open + (self.range_value * k1)
            self.short_trigger = self.today_open - (self.range_value * k2)
            self.trigger_date = today_str

            logger.info(f"?뱢 [DualThrust] Triggers Updated: Open={self.today_open:.2f}, Range={self.range_value:.2f}")
            logger.info(f"   Long Trigger: {self.long_trigger:.2f}, Short Trigger: {self.short_trigger:.2f}")

        except Exception as e:
            logger.error(f"DualThrust trigger update error: {e}")

    async def _logic(self, symbol, price):
        """留ㅻℓ 濡쒖쭅"""
        try:
            total, free, mmr = await self.get_balance_info()
            count, daily_pnl = self.db.get_daily_stats()
            pos = await self.get_server_position(symbol)

            # ?곹깭 ?곗씠???낅뜲?댄듃 (Symbol Keyed)
            self.ctrl.status_data[symbol] = {
                'engine': 'DualThrust', 'symbol': symbol, 'price': price,
                'total_equity': total, 'free_usdt': free, 'mmr': mmr,
                'daily_count': count, 'daily_pnl': daily_pnl,
                'pos_side': pos['side'].upper() if pos else 'NONE',
                'entry_price': float(pos['entryPrice']) if pos else 0.0,
                'coin_amt': float(pos['contracts']) if pos else 0.0,
                'pnl_pct': float(pos['percentage']) if pos else 0.0,
                'pnl_usdt': float(pos['unrealizedPnl']) if pos else 0.0,
                # Dual Thrust ?꾩슜
                'long_trigger': self.long_trigger,
                'short_trigger': self.short_trigger,
                'range': self.range_value,
                'today_open': self.today_open
            }

            # MMR 寃쎄퀬
            await self.check_mmr_alert(mmr)

            if self.ctrl.is_paused:
                return

            # ?쇱씪 ?먯떎 ?쒕룄
            if await self.check_daily_loss_limit():
                return

            # ?몃━嫄곌? ?놁쑝硫??湲?
            if not self.long_trigger or not self.short_trigger:
                logger.debug("[DualThrust] Waiting for trigger calculation...")
                return

            # 留ㅻℓ 諛⑺뼢 ?꾪꽣
            d_mode = self.cfg.get('system_settings', {}).get('trade_direction', 'both')

            current_side = pos['side'] if pos else None

            # 濡??몃━嫄??뚰뙆
            if price > self.long_trigger:
                if d_mode != 'short':  # ???꾩슜 ?꾨땲硫?
                    if current_side == 'short':
                        # ??泥?궛 ??濡??ㅼ쐞移?
                        await self._close_and_switch(symbol, pos, price, 'long', total)
                    elif not pos:
                        # ?ъ????놁쑝硫?濡?吏꾩엯
                        await self._entry(symbol, 'long', price, total)

            # ???몃━嫄??댄깉
            elif price < self.short_trigger:
                if d_mode != 'long':  # 濡??꾩슜 ?꾨땲硫?
                    if current_side == 'long':
                        # 濡?泥?궛 ?????ㅼ쐞移?
                        await self._close_and_switch(symbol, pos, price, 'short', total)
                    elif not pos:
                        # ?ъ????놁쑝硫???吏꾩엯
                        await self._entry(symbol, 'short', price, total)

        except Exception as e:
            logger.error(f"DualThrust logic error: {e}")

    async def _entry(self, symbol, side, price, total_equity):
        """?ъ???吏꾩엯"""
        try:
            cfg = self.cfg.get('dual_thrust_engine', {})
            lev = cfg.get('leverage', 5)
            risk_pct = normalize_risk_percent(cfg) / 100.0

            # ?덈쾭由ъ? ?ㅼ젙 & 寃⑸━ 紐⑤뱶 媛뺤젣
            await self.ensure_market_settings(symbol, leverage=lev)
            # await asyncio.to_thread(self.exchange.set_leverage, lev, symbol)

            # ?섎웾 怨꾩궛
            _, free, _ = await self.get_balance_info()
            qty = self.safe_amount(symbol, (free * risk_pct * lev) / price)

            if float(qty) <= 0:
                logger.warning(f"[DualThrust] Invalid qty: {qty}")
                return

            order = await self.submit_futures_entry(
                strategy='DUAL_THRUST',
                symbol=symbol,
                side=side,
                signal_timestamp=_resolve_crypto_signal_timestamp(
                    self, symbol, {'entry_price': price}
                ),
                qty=float(qty),
            )

            self.position_cache = None
            self.position_cache_time = 0

            self.db.log_trade_entry(symbol, side, price, float(qty))
            await self.ctrl.notify(f"✅ [DualThrust] {side.upper()} {qty} @ {price:.2f}")
            logger.info(f"??[DualThrust] Entry: {side} {qty} @ {price}")

        except Exception as e:
            logger.error(f"DualThrust entry error: {e}")
            await self.ctrl.notify(f"❌ [DualThrust] 진입 실패: {e}")

    async def _close_and_switch(self, symbol, pos, price, new_side, total_equity):
        """?ъ???泥?궛 ??諛섎? 諛⑺뼢 吏꾩엯"""
        try:
            # 留ㅻℓ 諛⑺뼢 ?꾪꽣 泥댄겕
            d_mode = self.cfg.get('system_settings', {}).get('trade_direction', 'both')
            if (d_mode == 'long' and new_side == 'short') or (d_mode == 'short' and new_side == 'long'):
                # 泥?궛留??섍퀬 ?ъ쭊???덊븿
                await self.exit_position(symbol, "DirectionFilter")
                return

            # 湲곗〈 ?ъ???泥?궛
            close_qty = self.safe_amount(symbol, abs(float(pos['contracts'])))

            pnl = float(pos.get('unrealizedPnl', 0))

            close_result = await self.submit_futures_close(
                strategy='DUAL_THRUST',
                symbol=symbol,
                position_side=pos['side'],
                position_signature=pos.get('timestamp') or pos.get('entryPrice'),
                qty=float(close_qty),
                reason='switch_close',
            )
            if close_result.state != OrderState.CLOSED.value:
                logger.warning("DualThrust switch blocked until prior position is CLOSED")
                return

            self.db.log_trade_close(symbol, pnl, float(pos.get('percentage', 0)), price, "Switch")
            await self.ctrl.notify(f"🔄 [DualThrust] {pos['side'].upper()} 청산 -> {new_side.upper()} 전환 | PnL: {pnl:+.2f}")

            self.position_cache = None
            self.position_cache_time = 0

            # ?좎떆 ?湲???諛섎? 諛⑺뼢 吏꾩엯
            await asyncio.sleep(1.0)
            await self._entry(symbol, new_side, price, total_equity)

        except Exception as e:
            logger.error(f"DualThrust switch error: {e}")

    async def exit_position(self, symbol, reason):
        """?ъ???泥?궛"""
        try:
            pos = await self.get_server_position(symbol, use_cache=False)
            if not pos:
                return

            contracts = abs(float(pos['contracts']))
            if contracts <= 0:
                return

            qty = self.safe_amount(symbol, contracts)

            order = await self.submit_futures_close(
                strategy='DUAL_THRUST',
                symbol=symbol,
                position_side=pos['side'],
                position_signature=pos.get('timestamp') or pos.get('entryPrice'),
                qty=float(qty),
                reason=reason,
            )

            pnl = float(pos.get('unrealizedPnl', 0))
            pnl_pct = float(pos.get('percentage', 0))
            exit_price = float(order.get('average', 0)) or float(pos.get('markPrice', 0))

            self.position_cache = None
            self.position_cache_time = 0

            self.db.log_trade_close(symbol, pnl, pnl_pct, exit_price, reason)
            await self.ctrl.notify(f"📊 [DualThrust] [{reason}] PnL: {pnl:+.2f} ({pnl_pct:+.2f}%)")

        except Exception as e:
            logger.error(f"DualThrust exit error: {e}")

class DualModeFractalEngine(BaseEngine):
    """
    DualModeFractalStrategy瑜??ъ슜?섎뒗 ?낅┰ ?붿쭊
    - Mode: Scalping (5m~15m) / Standard (1h~4h)
    """
    def __init__(self, controller):
        super().__init__(controller)
        self.strategy = None
        self.current_mode = None
        self.last_candle_ts = 0

    def _get_target_symbol(self):
        # 怨듯넻 ?ㅼ젙??Watchlist 泥?踰덉㎏ 肄붿씤???寃잛쑝濡??ъ슜
        watchlist = self.cfg.get('signal_engine', {}).get('watchlist', [])
        return watchlist[0] if watchlist else 'BTC/USDT'

    def start(self):
        super().start()
        self.last_candle_ts = 0  # ??꾩뒪?ы봽 珥덇린??異붽?
        if not self._init_strategy():
            self.running = False

    def _init_strategy(self):
        if not DUAL_MODE_AVAILABLE:
            logger.error("DualMode strategy module is not available.")
            return False
        cfg = self.cfg.get('dual_mode_engine', {})
        mode = cfg.get('mode', 'standard')
        self.strategy = DualModeFractalStrategy(mode=mode)
        self.current_mode = mode
        logger.info(f"?쏉툘 [DualMode] Strategy initialized: {mode.upper()}")
        return True

    async def poll_tick(self):
        if not self.running:
            return
        if not self.strategy:
            return

        try:
            symbol = self._get_target_symbol()
            cfg = self.cfg.get('dual_mode_engine', {})

            # 紐⑤뱶 蹂寃?媛먯? 諛??ъ큹湲고솕
            if cfg.get('mode') != self.current_mode:
                if not self._init_strategy():
                    return

            # ??꾪봽?덉엫 寃곗젙
            if self.current_mode == 'scalping':
                tf = cfg.get('scalping_tf', '5m')
            else:
                tf = cfg.get('standard_tf', '4h')

            # OHLCV 媛?몄삤湲?(Limit reduced to 300 for optimization)
            ohlcv = await asyncio.to_thread(self.exchange.fetch_ohlcv, symbol, tf, limit=300)
            if not ohlcv: return

            # 留덉?留??뺤젙 罹붾뱾 湲곗? (?꾩옱 吏꾪뻾 以묒씤 罹붾뱾? ?쒖쇅?섍굅???ы븿 ?щ? 寃곗젙)
            # ?꾨왂 濡쒖쭅??'close' ?곗씠?곕? ?곕?濡??뺤젙??罹붾뱾???덉쟾??
            last_closed = ohlcv[-2]
            ts = last_closed[0]
            price = float(ohlcv[-1][4]) # ?꾩옱媛???ㅼ떆媛?

            # ??罹붾뱾 媛깆떊 ?쒖뿉留??쒓렇??怨꾩궛 (?대쭅 遺??媛먯냼)
            if ts > self.last_candle_ts:
                self.last_candle_ts = ts

                df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

                # ?꾨왂 ?ㅽ뻾
                res = self.strategy.generate_signals(df)
                last_row = res.iloc[-2] # ?뺤젙 罹붾뱾 湲곗? ?쒓렇??

                sig = int(last_row['signal'])
                chop = float(last_row['chop_idx'])
                kalman = float(last_row['kalman_val'])
                exit_price = float(last_row['exit_price'])

                logger.info(f"?쏉툘 [DualMode] {tf} Candle Close: Chop={chop:.1f}, Kalman={kalman:.1f}, Signal={sig}")

                # 留ㅻℓ 濡쒖쭅
                await self._execute_signal(symbol, sig, price)

            # ?곹깭 ?낅뜲?댄듃 (??쒕낫?쒖슜)
            await self._update_status(symbol, price, tf)

        except Exception as e:
            logger.error(f"DualMode poll error: {e}")

    async def _execute_signal(self, symbol, sig, price):
        pos = await self.get_server_position(symbol, use_cache=False)
        total, free, _ = await self.get_balance_info()

        # 1. Long Entry (Signal=1)
        if sig == 1:
            if not pos:
                await self._entry(symbol, 'long', price, free)
            elif pos['side'] == 'short':
                # Close Short & Flip Long
                await self.exit_position(symbol, "Signal_Flip_Long")
                await asyncio.sleep(1)
                await self._entry(symbol, 'long', price, free)

        # 2. Short Entry (Signal=-1)
        elif sig == -1:
            if not pos:
                await self._entry(symbol, 'short', price, free)
            elif pos['side'] == 'long':
                # Close Long & Flip Short
                await self.exit_position(symbol, "Signal_Flip_Short")
                await asyncio.sleep(1)
                await self._entry(symbol, 'short', price, free)

        # 3. Exit Long (Signal=2)
        elif sig == 2:
            if pos and pos['side'] == 'long':
                await self.exit_position(symbol, "Signal_Exit_Long")

        # 4. Exit Short (Signal=-2)
        elif sig == -2:
            if pos and pos['side'] == 'short':
                await self.exit_position(symbol, "Signal_Exit_Short")

    async def _entry(self, symbol, side, price, free_usdt):
        # 怨듯넻 ?ㅼ젙?먯꽌 由ъ뒪??諛?TP/SL 媛?몄삤湲?
        common_cfg = self.cfg.get('signal_engine', {}).get('common_settings', {})
        risk = normalize_risk_percent(common_cfg) / 100.0

        # ?덈쾭由ъ?????쇰え???ㅼ젙???곕Ⅴ嫄곕굹 怨듯넻 ?ㅼ젙???곕쫫 (?ъ슜???붿껌: ?먯궛/肄붿씤/TP/SL)
        # 臾몃㎘???덈쾭由ъ???怨듯넻???곕Ⅴ??寃껋씠 ?덉쟾?섎굹, 紐낆떆???붿껌? ?놁뿀??
        # ?섏?留??ㅻⅨ ?ㅼ젙?ㅼ씠 怨듯넻???곕Ⅴ誘濡??덈쾭由ъ???怨듯넻???쎈뒗 寃껋씠 ?쇨??곸엫.
        lev = common_cfg.get('leverage', 5)

        # ?덈쾭由ъ? ?ㅼ젙 & 寃⑸━ 紐⑤뱶 媛뺤젣
        await self.ensure_market_settings(symbol, leverage=lev)
        # await asyncio.to_thread(self.exchange.set_leverage, lev, symbol)

        cost = free_usdt * risk
        qty = self.safe_amount(symbol, (cost * lev) / price)

        if float(qty) > 0:
            # 1. 吏꾩엯 二쇰Ц
            submission = await self.submit_futures_entry(
                strategy='DUAL_MODE_FRACTAL',
                symbol=symbol,
                side=side,
                signal_timestamp=_resolve_crypto_signal_timestamp(
                    self, symbol, {'entry_price': price}
                ),
                qty=float(qty),
            )
            if not submission.accepted:
                raise RuntimeError(
                    f"DualMode safe entry rejected: {submission.state} {submission.error}"
                )
            order = submission.order or {}
            position = submission.position or await self.get_server_position(
                symbol,
                use_cache=False,
            )
            entry_price = float(order.get('average') or price)
            self.position_cache = None
            self.db.log_trade_entry(symbol, side, entry_price, float(qty))

            # 2. TP/SL ?ㅼ젙 (怨듯넻 ?ㅼ젙 媛??ъ슜)
            tp_master_enabled = bool(common_cfg.get('tp_sl_enabled', True))
            tp_enabled = tp_master_enabled and bool(common_cfg.get('take_profit_enabled', True))
            sl_enabled = tp_master_enabled and bool(common_cfg.get('stop_loss_enabled', True))
            tp_pct = common_cfg.get('target_roe_pct', 0.0) if tp_enabled else 0.0
            sl_pct = common_cfg.get('stop_loss_pct', 0.0) if sl_enabled else 0.0

            msg = f"✅ [DualMode] {side.upper()} 진입: {qty} @ {entry_price}"

            # TP/SL 二쇰Ц 諛곗튂 (鍮꾨룞湲??ㅻ쪟 諛⑹?瑜??꾪빐 try-except)
            if tp_pct > 0 or sl_pct > 0:
                try:
                    # 湲곗〈 ?ㅽ뵂 ?ㅻ뜑 痍⑥냼
                    await asyncio.to_thread(self.exchange.cancel_all_orders, symbol)

                    if side == 'long':
                        if tp_pct > 0:
                            tp_price = entry_price * (1 + tp_pct / 100 / lev)
                            await self.place_futures_protection(
                                'tp', symbol=symbol, side='sell', qty=float(qty),
                                price=tp_price, order_type='limit', label='TP',
                                position=position,
                                entry_client_order_id=submission.client_order_id,
                                config=common_cfg,
                            )
                            msg += f" | TP: {tp_price:.2f}"
                        if sl_pct > 0:
                            sl_price = entry_price * (1 - sl_pct / 100 / lev)
                            await self.place_futures_protection(
                                'sl', symbol=symbol, side='sell', qty=float(qty),
                                trigger_price=sl_price, order_type='stop_market', label='SL',
                                position=position,
                                entry_client_order_id=submission.client_order_id,
                                config=common_cfg,
                            )
                            msg += f" | SL: {sl_price:.2f}"
                    else: # short
                        if tp_pct > 0:
                            tp_price = entry_price * (1 - tp_pct / 100 / lev)
                            await self.place_futures_protection(
                                'tp', symbol=symbol, side='buy', qty=float(qty),
                                price=tp_price, order_type='limit', label='TP',
                                position=position,
                                entry_client_order_id=submission.client_order_id,
                                config=common_cfg,
                            )
                            msg += f" | TP: {tp_price:.2f}"
                        if sl_pct > 0:
                            sl_price = entry_price * (1 + sl_pct / 100 / lev)
                            await self.place_futures_protection(
                                'sl', symbol=symbol, side='buy', qty=float(qty),
                                trigger_price=sl_price, order_type='stop_market', label='SL',
                                position=position,
                                entry_client_order_id=submission.client_order_id,
                                config=common_cfg,
                            )
                            msg += f" | SL: {sl_price:.2f}"
                except Exception as e:
                    logger.error(f"TP/SL Order Failed: {e}")
                    msg += " | ⚠️ TP/SL 오류"
                    safety_engine = self.ctrl.engines.get(CORE_ENGINE)
                    if safety_engine and hasattr(
                        safety_engine,
                        '_emergency_close_position_without_stop_loss',
                    ):
                        await safety_engine._emergency_close_position_without_stop_loss(
                            symbol,
                            reason=f'DualMode TP/SL protection placement failed: {e}',
                            max_attempts=5,
                            lockout_reason='TAKE_PROFIT_PROTECTION_FAILED_FORCE_CLOSED',
                            protection_label='TP/SL',
                            critical_pause_reason_code='TP_SL_FAILED_AND_EMERGENCY_CLOSE_FAILED',
                        )
                    else:
                        raise RuntimeError(
                            'DualMode protection failed and emergency close service is unavailable'
                        ) from e

            await self.ctrl.notify(msg)

    async def exit_position(self, symbol, reason):
        pos = await self.get_server_position(symbol)
        if pos:
            qty = self.safe_amount(symbol, abs(float(pos['contracts'])))
            close_result = await self.submit_futures_close(
                strategy='DUAL_MODE_FRACTAL',
                symbol=symbol,
                position_side=pos['side'],
                position_signature=(
                    pos.get('timestamp')
                    or pos.get('entryPrice')
                    or f"{pos['side']}:{qty}"
                ),
                qty=float(qty),
                reason=reason,
            )
            if close_result.state != OrderState.CLOSED.value:
                raise RuntimeError(
                    f"DualMode close not confirmed: {close_result.state} {close_result.error}"
                )
            pnl = float(pos.get('unrealizedPnl', 0))
            self.db.log_trade_close(symbol, pnl, 0, 0, reason)
            await self.ctrl.notify(f"📊 [DualMode] 청산 [{reason}]: PnL {pnl:.2f}")
            self.position_cache = None
            return close_result
        return None

    async def _update_status(self, symbol, price, tf):
        total, free, mmr = await self.get_balance_info()
        count, daily_pnl = self.db.get_daily_stats()
        pos = await self.get_server_position(symbol)

        self.ctrl.status_data[symbol] = {
            'engine': 'DualMode', 'symbol': symbol, 'price': price,
            'total_equity': total, 'free_usdt': free, 'mmr': mmr,
            'daily_count': count, 'daily_pnl': daily_pnl,
            'pos_side': pos['side'].upper() if pos else 'NONE',
            'entry_price': float(pos['entryPrice']) if pos else 0.0,
            'pnl_usdt': float(pos['unrealizedPnl']) if pos else 0.0,
            'pnl_pct': float(pos['percentage']) if pos else 0.0,
            # DualMode ?꾩슜
            'dm_mode': self.current_mode.upper(),
            'dm_tf': tf
        }

__all__ = (
    'TemaEngine',
    'ShannonEngine',
    'DualThrustEngine',
    'DualModeFractalEngine',
)
