"""Polling, coin selection, volume validation, and market-context scanning."""

from __future__ import annotations


class SignalScannerMixin:
    def _get_primary_poll_timeframe(self, symbol=None, trade_cfg=None):
        cfg = trade_cfg if isinstance(trade_cfg, dict) else self.get_runtime_trade_config()
        common_cfg = cfg.get('common_settings', {}) if isinstance(cfg.get('common_settings', {}), dict) else {}
        strategy_params = cfg.get('strategy_params', {}) if isinstance(cfg.get('strategy_params', {}), dict) else {}
        active_strategy = str(strategy_params.get('active_strategy', 'utbot') or 'utbot').lower()
        if active_strategy in UTBREAKOUT_STRATEGIES:
            fb_cfg = self._get_utbot_filtered_breakout_config(strategy_params)
            if bool(fb_cfg.get('adaptive_timeframe_enabled', False)):
                raw_timeframes = fb_cfg.get('adaptive_timeframes') or fb_cfg.get('auto_timeframes') or [fb_cfg.get('entry_timeframe', '15m')]
                candidates = []
                for tf in raw_timeframes:
                    tf_text = str(tf or '').strip().lower()
                    tf_ms = self._timeframe_to_ms(tf_text)
                    if tf_text and tf_ms > 0:
                        candidates.append((tf_ms, tf_text))
                if candidates:
                    return min(candidates, key=lambda item: item[0])[1]
            return str(fb_cfg.get('entry_timeframe', '15m') or '15m').strip().lower() or '15m'
        return common_cfg.get('entry_timeframe', common_cfg.get('timeframe', '15m'))

    async def poll_tick(self):
        """
        [?쒖닔 ?대쭅] 硫붿씤 ?대쭅 ?⑥닔 (Multi-Symbol)
        Mode 1: Scanner ON -> Serial Hunter (One at a time, ignoring Watchlist)
        Mode 2: Scanner OFF -> Watchlist + Manual Added
        """
        if not self.running:
            return

        try:
            self.last_activity = time.time()
            cfg = self.get_runtime_trade_config()
            common_cfg = self.get_runtime_common_settings()
            entry_tf = self._get_primary_poll_timeframe(trade_cfg=cfg)

            active_position_symbols = set()
            # Common: Fetch current positions (best effort).
            # If this call fails (e.g. permission/auth issue), do not stop status updates.
            try:
                active_position_symbols = await self.get_active_position_symbols(use_cache=True)
            except Exception as e:
                logger.warning(
                    f"Signal poll_tick: fetch_positions failed, continuing without server positions ({e})"
                )

            try:
                orphan_cleanup = await self._cleanup_orphan_protection_orders(
                    reason='poll tick orphan protection sweep',
                    alert=True
                )
                if orphan_cleanup.get('cancelled', 0):
                    logger.warning(
                        f"Protection orphan sweep cancelled {orphan_cleanup.get('cancelled')} "
                        f"orders across {len(orphan_cleanup.get('symbols', {}))} symbols"
                    )
            except Exception as cleanup_error:
                logger.warning(f"Protection orphan sweep failed: {cleanup_error}")

            # Check Scanner Setting
            scanner_enabled = common_cfg.get('scanner_enabled', True)

            target_symbols = set()

            if scanner_enabled:
                # === [Mode 1: Serial Scanner] ===
                # 0. Add Existing Positions to Targets (Safety Net)
                for sym in active_position_symbols:
                    target_symbols.add(sym)

                # 1. 留뚯빟 ?대? ?↔퀬 ?덈뒗 ?ㅼ틦??肄붿씤???덈떎硫? -> 洹멸쾬留?愿由?
                if self.scanner_active_symbol:
                    # ?ъ??섏씠 ?꾩쭅 ?댁븘?덈뒗吏 ?뺤씤
                    pos = await self.get_server_position(self.scanner_active_symbol, use_cache=False)
                    if pos:
                        # ?ъ????좎? 以?-> ?대냸留?吏묒쨷 耳??(?ㅼ틪 以묒?)
                        target_symbols.add(self.scanner_active_symbol)
                    else:
                        # ?ъ????놁쓬 (泥?궛?? -> ?ㅼ틦??蹂??珥덇린??& ?ㅼ떆 ?ㅼ틪 紐⑤뱶 吏꾩엯
                        logger.info(f"?삼툘 Scanner trade completed for {self.scanner_active_symbol}. Resuming scan.")
                        await self._cancel_protection_orders(
                            self.scanner_active_symbol,
                            reason='scanner position completed'
                        )
                        await self._reconcile_closed_position_protection(
                            self.scanner_active_symbol,
                            reason='scanner position completed',
                            alert=True,
                            attempts=2
                        )
                        self.scanner_active_symbol = None
                        # 諛붾줈 ?ㅼ틪 濡쒖쭅?쇰줈 ?섏뼱媛?

                # 2. ?↔퀬 ?덈뒗寃??녿떎硫? -> ?ㅼ틪 ?ㅽ뻾
                if not self.scanner_active_symbol:
                    # ?ㅼ틪 寃곌낵? 蹂꾧컻濡?湲곗〈 ?ъ??섏? ?대? target_symbols??異붽???

                    # 荑⑦???濡쒖쭅: ?ъ???泥?궛 吏곹썑?쇰㈃ 諛붾줈 ?ㅼ틪?댁빞 ??
                    scanner_scan_interval = self._get_scanner_scan_interval_seconds(common_cfg)
                    if time.time() - self.last_volume_scan > scanner_scan_interval:
                        await self.scan_and_trade_high_volume()
                        self.last_volume_scan = time.time()

                    # ?ㅼ틪 寃곌낵 吏꾩엯?덉쑝硫?target_symbols??異붽???
                    if self.scanner_active_symbol:
                        target_symbols.add(self.scanner_active_symbol)
            else:
                # === [Mode 2: Manual / Watchlist] ===
                # Config Watchlist
                config_symbols = set(self.get_runtime_watchlist())

                # Merge: Config + Chat Manual + Positions
                target_symbols = self.active_symbols | config_symbols | active_position_symbols

            strategy_params = cfg.get('strategy_params', {})
            active_strategy = str(
                strategy_params.get('active_strategy', 'utbot') or 'utbot'
            ).lower()
            if active_strategy in UTBREAKOUT_STRATEGIES and not self.is_upbit_mode():
                target_symbols = {
                    self._canonicalize_utbreakout_symbol_for_use(
                        target_symbol,
                        source='poll_tick_target',
                    )
                    for target_symbol in target_symbols
                    if target_symbol
                }
                if self.scanner_active_symbol:
                    self.scanner_active_symbol = (
                        self._canonicalize_utbreakout_symbol_for_use(
                            self.scanner_active_symbol,
                            source='poll_tick_scanner_active',
                        )
                    )

            if self.ctrl.is_paused:
                total, free, mmr = await self.get_balance_info()
                count, daily_pnl = self.db.get_daily_stats()
                paused_symbol = sorted(target_symbols)[0] if target_symbols else 'PAUSED'
                self.ctrl.status_data = {
                    'PAUSED': {
                        'engine': 'SIGNAL',
                        'symbol': paused_symbol,
                        'price': 0,
                        'pos_side': 'NONE',
                        'total_equity': total,
                        'free_usdt': free,
                        'mmr': mmr,
                        'daily_count': count,
                        'daily_pnl': daily_pnl,
                        'leverage': common_cfg.get('leverage', 20),
                        'margin_mode': 'SPOT' if self.is_upbit_mode() else 'ISOLATED',
                        'entry_tf': entry_tf,
                        'exit_tf': common_cfg.get('exit_timeframe', '4h'),
                        'entry_reason': '일시정지(PAUSE) 상태',
                        'protection_config': {'tp_enabled': False, 'sl_enabled': False},
                    }
                }
                return

            if 'PAUSED' in self.ctrl.status_data:
                del self.ctrl.status_data['PAUSED']

            if not target_symbols:
                if scanner_enabled:
                    # [Fix] Provide status feedback during scanning (Target empty)
                    total, free, mmr = await self.get_balance_info()
                    count, daily_pnl = self.db.get_daily_stats()

                    self.ctrl.status_data['SCANNER'] = {
                        'engine': 'SIGNAL',
                        'symbol': 'Scanning... ?뱻',
                        'price': 0,
                        'pos_side': 'NONE',
                        'total_equity': total, 'free_usdt': free, 'mmr': mmr,
                        'daily_count': count, 'daily_pnl': daily_pnl,
                        'leverage': common_cfg.get('leverage', 20),
                        'margin_mode': 'SPOT' if self.is_upbit_mode() else 'ISOLATED',
                        'entry_tf': entry_tf,
                        'exit_tf': common_cfg.get('exit_timeframe', '4h')
                    }
                return

            # If targets exist, remove SCANNER placeholder to avoid duplicate display
            if 'SCANNER' in self.ctrl.status_data:
                del self.ctrl.status_data['SCANNER']

            active_status_keys = set(target_symbols) | {'PAUSED', 'SCANNER'}
            stale_keys = [
                key for key in list(self.ctrl.status_data.keys())
                if key not in active_status_keys
            ]
            for stale_key in stale_keys:
                self.ctrl.status_data.pop(stale_key, None)

            # Parallel Execution: Poll all symbols concurrently
            tasks = []
            for symbol in target_symbols:
                if self.ctrl.is_paused: break
                tasks.append(self.poll_symbol(symbol, entry_tf, cfg))

            if tasks:
                await asyncio.gather(*tasks)

        except Exception as e:
            self.consecutive_errors += 1
            logger.error(f"Signal poll_tick error ({self.consecutive_errors}x): {e}")
            if self.consecutive_errors > 10:
                self.consecutive_errors = 0

    async def poll_symbol(self, symbol, primary_tf, cfg):
        """媛쒕퀎 ?щ낵 ?대쭅 濡쒖쭅"""
        self._ensure_runtime_state_containers((
            'last_processed_candle_ts',
            'last_state_sync_candle_ts',
            'last_stateful_retry_ts',
            'last_utbreakout_no_position_retry_ts',
            'last_processed_exit_candle_ts',
            'last_candle_time',
            'utbreakout_adaptive_tf_state',
            'utbreakout_adaptive_last_decision_ts',
            'utbreakout_last_selected_set_ids',
            'fisher_states',
            'vbo_states',
            'cameron_states'
        ))

        try:
            strategy_params = cfg.get('strategy_params', {})
            entry_mode = strategy_params.get('entry_mode', 'cross').lower()
            active_strategy = strategy_params.get('active_strategy', 'utbot').lower()
            if active_strategy in UTBREAKOUT_STRATEGIES:
                symbol = self._canonicalize_utbreakout_symbol_for_use(
                    symbol,
                    source='poll_symbol',
                )
            primary_tf = self._get_primary_poll_timeframe(symbol, cfg)
            # 1. OHLCV (Primary) - Basic monitoring
            ohlcv_p = await asyncio.to_thread(self.market_data_exchange.fetch_ohlcv, symbol, primary_tf, limit=5)
            if not ohlcv_p or len(ohlcv_p) < 3:
                return

            current_price = float(ohlcv_p[-1][4])

            # [Fix] Update Status and get local pos_side to avoid race condition
            pos_side = await self.check_status(symbol, current_price)

            # 2. Check Primary TF (Entry Logic)
            last_closed_p = ohlcv_p[-2]
            ts_p = last_closed_p[0]
            k_p = {
                't': ts_p, 'o': str(last_closed_p[1]), 'h': str(last_closed_p[2]),
                'l': str(last_closed_p[3]), 'c': str(last_closed_p[4]), 'v': str(last_closed_p[5])
            }

            # ?щ낵蹂??곹깭 珥덇린??
            if symbol not in self.last_processed_candle_ts:
                self.last_processed_candle_ts[symbol] = 0
            if symbol not in self.last_state_sync_candle_ts:
                self.last_state_sync_candle_ts[symbol] = 0
            if symbol not in self.last_stateful_retry_ts:
                self.last_stateful_retry_ts[symbol] = 0.0

            uses_stateful_primary_sync = active_strategy in STATEFUL_UT_STRATEGIES
            processed_primary_this_tick = False
            if active_strategy in UTBREAKOUT_STRATEGIES:
                self._utbreakout_trace_event(
                    symbol,
                    'POLL_TICK',
                    'SEEN',
                    active_strategy=active_strategy,
                    primary_tf=primary_tf,
                    pos_side=pos_side,
                    current_price=current_price,
                    candle_ts=ts_p,
                )

            if ts_p > self.last_processed_candle_ts[symbol]:
                logger.info(f"?빉截?[Primary {primary_tf}] {symbol} New Candle: {ts_p} close={last_closed_p[4]}")
                await self.process_primary_candle(symbol, k_p)
                processed_primary_this_tick = True
                if self.last_candle_success.get(symbol, False):
                    self.last_processed_candle_ts[symbol] = ts_p
                    if uses_stateful_primary_sync:
                        self.last_state_sync_candle_ts[symbol] = ts_p
                    pos_side = await self.check_status(symbol, current_price)
                else:
                    logger.warning(f"Primary candle processing failed, will retry: {symbol} {ts_p}")
            elif uses_stateful_primary_sync and self.last_state_sync_candle_ts[symbol] < ts_p:
                logger.info(f"?봽 [Primary {primary_tf}] {symbol} State sync on closed candle: {ts_p} close={last_closed_p[4]}")
                await self.process_primary_candle(symbol, k_p, force=True)
                processed_primary_this_tick = True
                if self.last_candle_success.get(symbol, False):
                    self.last_state_sync_candle_ts[symbol] = ts_p
                    pos_side = await self.check_status(symbol, current_price)
            elif uses_stateful_primary_sync and pos_side != 'NONE':
                retry_interval = 15.0
                now_ts = time.time()
                if (not processed_primary_this_tick) and (now_ts - float(self.last_stateful_retry_ts.get(symbol, 0.0) or 0.0) >= retry_interval):
                    logger.info(
                        f"?봽 [Primary {primary_tf}] {symbol} Stateful reconcile retry: "
                        f"candle={ts_p} pos={pos_side} close={last_closed_p[4]}"
                    )
                    self.last_stateful_retry_ts[symbol] = now_ts
                    await self.process_primary_candle(symbol, k_p, force=True)
                    if self.last_candle_success.get(symbol, False):
                        self.last_state_sync_candle_ts[symbol] = ts_p
                    pos_side = await self.check_status(symbol, current_price)

            if (
                active_strategy in UTBREAKOUT_STRATEGIES
                and not processed_primary_this_tick
                and ts_p <= self.last_processed_candle_ts[symbol]
            ):
                self._utbreakout_trace_event(
                    symbol,
                    'POLL_ALREADY_PROCESSED',
                    'WAIT',
                    primary_tf=primary_tf,
                    candle_ts=ts_p,
                    last_processed=self.last_processed_candle_ts.get(symbol),
                    pos_side=pos_side,
                )

            # UTBreakout no-position retry:
            # If status can be entry-ready but the current candle was already processed,
            # retry the primary entry path periodically while no position exists.
            if (
                active_strategy in UTBREAKOUT_STRATEGIES
                and pos_side == 'NONE'
                and not processed_primary_this_tick
            ):
                filtered_cfg = self._get_utbot_filtered_breakout_config(strategy_params)
                retry_interval = float(
                    filtered_cfg.get('utbreakout_no_position_retry_interval_sec', 20.0)
                    or 20.0
                )
                now_ts = time.time()
                retry_key = f"{symbol}:{primary_tf}:{ts_p}"
                last_retry = float(
                    self.last_utbreakout_no_position_retry_ts.get(retry_key, 0.0)
                    or 0.0
                )
                if now_ts - last_retry >= retry_interval:
                    self.last_utbreakout_no_position_retry_ts[retry_key] = now_ts
                    if self._utbreakout_decision_already_consumed(symbol, ts_p):
                        self._utbreakout_trace_event(
                            symbol,
                            'NO_POSITION_RETRY',
                            'DECISION_ALREADY_CONSUMED',
                            primary_tf=primary_tf,
                            candle_ts=ts_p,
                            retry_interval=retry_interval,
                        )
                        self._clear_utbot_filtered_breakout_entry_plan(symbol)
                    else:
                        self._utbreakout_trace_event(
                            symbol,
                            'NO_POSITION_RETRY',
                            'FORCE_PROCESS_PRIMARY',
                            primary_tf=primary_tf,
                            candle_ts=ts_p,
                            retry_interval=retry_interval,
                        )
                        logger.warning(
                            "[UTBOT_FILTERED_BREAKOUT_V1] no-position entry retry: "
                            "symbol=%s tf=%s candle=%s",
                            symbol,
                            primary_tf,
                            ts_p,
                        )
                        await self.process_primary_candle(symbol, k_p, force=True)
                        processed_primary_this_tick = True
                        pos_side = await self.check_status(symbol, current_price)

            if active_strategy in UTBREAKOUT_STRATEGIES:
                await self._utbreakout_entry_watchdog_check(
                    symbol,
                    source='poll_symbol',
                )

            if active_strategy == 'utsmc':
                live_candle_ts = int(ohlcv_p[-1][0])
                pos_side = await self._maybe_execute_utsmc_live_entry(
                    symbol,
                    live_candle_ts,
                    current_price,
                    pos_side,
                    strategy_name='UTSMC'
                )
            elif (not self.is_upbit_mode()) and (active_strategy == 'utbot' or active_strategy in UT_HYBRID_STRATEGIES):
                live_candle_ts = int(ohlcv_p[-1][0])
                pos_side = await self._maybe_execute_ut_live_entry(
                    symbol,
                    live_candle_ts,
                    current_price,
                    pos_side
                )

            # 3. Check Exit TF (Exit Logic)
            # Cross/Position 紐⑤뱶?먯꽌留?Secondary TF 泥?궛 濡쒖쭅 ?ъ슜
            uses_secondary_exit = (
                pos_side != 'NONE'
                and (
                    (active_strategy in MA_STRATEGIES and entry_mode in ['cross', 'position'])
                    or active_strategy == 'cameron'
                    or active_strategy == 'utsmc'
                    or active_strategy == 'utbot'
                    or active_strategy in UTBREAKOUT_STRATEGIES
                    or active_strategy in UT_HYBRID_STRATEGIES
                )
            )
            if uses_secondary_exit:
                exit_tf = self._get_exit_timeframe(symbol)

                ohlcv_e = await asyncio.to_thread(self.market_data_exchange.fetch_ohlcv, symbol, exit_tf, limit=5)
                if ohlcv_e and len(ohlcv_e) >= 3:
                    last_closed_e = ohlcv_e[-2]
                    ts_e = last_closed_e[0]

                    if symbol not in self.last_processed_exit_candle_ts:
                        self.last_processed_exit_candle_ts[symbol] = 0

                    # [Initial Sync] 留뚯빟 遊??ъ떆???깆쑝濡??꾩쭅 怨꾩궛 湲곕줉???녾퀬 ?ъ??섏씠 ?덈떎硫?利됱떆 1??怨꾩궛
                    is_first_sync = (self.last_processed_exit_candle_ts[symbol] == 0)

                    if ts_e > self.last_processed_exit_candle_ts[symbol] or is_first_sync:
                        if is_first_sync:
                            logger.info(f"?봽 [Initial Sync] {symbol} Position detected on restart, processing filters...")
                        else:
                            logger.info(f"?빉截?[Exit {exit_tf}] {symbol} New Candle: {ts_e} close={last_closed_e[4]}")

                        exit_ok = await self.process_exit_candle(symbol, exit_tf, pos_side)
                        if exit_ok:
                            self.last_processed_exit_candle_ts[symbol] = ts_e
                        else:
                            logger.warning(f"Exit candle processing failed, will retry: {symbol} {ts_e}")

        except Exception as e:
            logger.error(f"Poll symbol {symbol} error: {e}")
            import traceback
            traceback.print_exc()

    def _get_coin_selector_config(self):
        raw = self.get_runtime_trade_config().get('coin_selector', {})
        cfg = default_coin_selector_config()
        if isinstance(raw, dict):
            cfg.update(raw)
        def _bool_value(value, default=False):
            if isinstance(value, bool):
                return value
            text = str(value).strip().lower()
            if text in {'1', 'true', 'yes', 'on', 'enable', 'enabled'}:
                return True
            if text in {'0', 'false', 'no', 'off', 'disable', 'disabled'}:
                return False
            return default
        for key in ('enabled', 'auto_apply_watchlist', 'custom_universe_enabled', 'custom_relax_discovery', 'candidate_cooldown_enabled', 'selection_quality_enabled', 'include_tradifi_universe'):
            cfg[key] = _bool_value(cfg.get(key), bool(default_coin_selector_config().get(key, False)))
        for key in ('excluded_sectors', 'blacklist'):
            value = cfg.get(key, [])
            if isinstance(value, str):
                cfg[key] = [item.strip() for item in value.split(',') if item.strip()]
            elif isinstance(value, (list, tuple, set)):
                cfg[key] = [str(item).strip() for item in value if str(item).strip()]
            else:
                cfg[key] = []
        cfg['custom_symbols'] = normalize_coin_selector_custom_symbols(cfg.get('custom_symbols'))
        if not isinstance(cfg.get('sector_overrides'), dict):
            cfg['sector_overrides'] = {}
        for key, default in {
            'min_quote_volume_usdt': 100_000_000.0,
            'ideal_quote_volume_usdt': 1_000_000_000.0,
            'max_spread_pct': 0.08,
            'max_abs_price_change_pct': 18.0,
            'min_final_score': 55.0,
            'refresh_interval_seconds': 300.0,
            'rate_limit_backoff_seconds': 120.0,
            'candidate_cooldown_seconds': 1800.0,
            'selection_target_realized_vol_pct': 0.65,
            'selection_max_drawdown_pct': 18.0,
            'selection_max_rebound_pct': 18.0,
            'selection_max_dispersion_pct': 9.0,
        }.items():
            try:
                cfg[key] = max(0.0, float(cfg.get(key, default)))
            except (TypeError, ValueError):
                cfg[key] = float(default)
        cfg['min_quote_volume_usdt'] = max(
            COIN_SELECTOR_HARD_MIN_QUOTE_VOLUME_USDT,
            float(
                cfg.get(
                    'min_quote_volume_usdt',
                    COIN_SELECTOR_HARD_MIN_QUOTE_VOLUME_USDT,
                )
                or COIN_SELECTOR_HARD_MIN_QUOTE_VOLUME_USDT
            ),
        )
        cfg['ideal_quote_volume_usdt'] = max(
            cfg['min_quote_volume_usdt'],
            float(
                cfg.get('ideal_quote_volume_usdt', 1_000_000_000.0)
                or 1_000_000_000.0
            ),
        )
        for key, default in {
            'min_trade_count': 20_000,
            'ideal_trade_count': 500_000,
            'analysis_limit': 20,
            'analysis_batch_size': 12,
            'top_n': 10,
            'max_strategy_evaluations_per_cycle': 3,
            'candidate_cooldown_misses': 3,
            'selection_return_lookback_bars': 96,
            'tradifi_universe_max_candidates': 20,
        }.items():
            try:
                cfg[key] = max(1, int(float(cfg.get(key, default))))
            except (TypeError, ValueError):
                cfg[key] = int(default)
        return cfg

    async def _validate_mainnet_entry_quote_volume(self, symbol):
        ctrl = getattr(self, 'ctrl', None)
        exchange_mode = None
        if ctrl is not None and hasattr(ctrl, 'get_exchange_mode'):
            try:
                exchange_mode = ctrl.get_exchange_mode()
            except Exception:
                exchange_mode = None
        if exchange_mode != BINANCE_MAINNET:
            return {
                'allowed': True,
                'status': 'SKIPPED_NON_MAINNET',
                'symbol': symbol,
            }

        selector_cfg = self._get_coin_selector_config()
        min_quote_volume = max(
            COIN_SELECTOR_HARD_MIN_QUOTE_VOLUME_USDT,
            float(
                selector_cfg.get(
                    'min_quote_volume_usdt',
                    COIN_SELECTOR_HARD_MIN_QUOTE_VOLUME_USDT,
                )
                or COIN_SELECTOR_HARD_MIN_QUOTE_VOLUME_USDT
            ),
        )
        market_exchange = (
            getattr(self, 'market_data_exchange', None)
            or getattr(self, 'exchange', None)
        )
        if market_exchange is None or not hasattr(
            market_exchange,
            'fetch_ticker',
        ):
            return {
                'allowed': False,
                'status': 'REJECTED_QUOTE_VOLUME_UNAVAILABLE',
                'symbol': symbol,
                'quote_volume': None,
                'min_quote_volume': min_quote_volume,
                'reason': '24h quoteVolume endpoint unavailable',
            }
        try:
            ticker = await asyncio.to_thread(
                market_exchange.fetch_ticker,
                symbol,
            )
        except Exception as exc:
            return {
                'allowed': False,
                'status': 'REJECTED_QUOTE_VOLUME_UNAVAILABLE',
                'symbol': symbol,
                'quote_volume': None,
                'min_quote_volume': min_quote_volume,
                'reason': (
                    '24h quoteVolume lookup failed: '
                    f'{type(exc).__name__}: {exc}'
                ),
            }
        ticker = ticker if isinstance(ticker, dict) else {}
        info = ticker.get('info') if isinstance(ticker.get('info'), dict) else {}
        quote_volume = _safe_float_or_none(
            ticker.get('quoteVolume')
            or info.get('quoteVolume')
        )
        if quote_volume is None or quote_volume <= 0:
            return {
                'allowed': False,
                'status': 'REJECTED_QUOTE_VOLUME_UNAVAILABLE',
                'symbol': symbol,
                'quote_volume': quote_volume,
                'min_quote_volume': min_quote_volume,
                'reason': '24h quoteVolume is missing or zero',
            }
        allowed = float(quote_volume) >= min_quote_volume
        return {
            'allowed': allowed,
            'status': (
                'PASS'
                if allowed
                else 'REJECTED_LOW_QUOTE_VOLUME'
            ),
            'symbol': symbol,
            'quote_volume': float(quote_volume),
            'min_quote_volume': min_quote_volume,
            'reason': (
                f'24h quoteVolume {float(quote_volume):.2f} '
                f'>= {min_quote_volume:.2f} USDT'
                if allowed
                else f'24h quoteVolume {float(quote_volume):.2f} '
                f'< {min_quote_volume:.2f} USDT'
            ),
        }

    @staticmethod
    def _is_exchange_rate_limit_error(value):
        """Recognize Binance/HTTP throttling even when buried in a diagnostic payload."""
        if isinstance(value, dict):
            return any(
                SignalEngine._is_exchange_rate_limit_error(item)
                for item in value.values()
            )
        if isinstance(value, (list, tuple, set)):
            return any(
                SignalEngine._is_exchange_rate_limit_error(item)
                for item in value
            )
        text = str(value or '').strip().lower()
        return any(marker in text for marker in (
            '429 too many requests',
            'http 429',
            'status code 429',
            'code=-1003',
            'code -1003',
            '"code":-1003',
            "'code': -1003",
            'too many requests',
            'request weight limit',
            'way too much request weight',
            'ip banned until',
        ))

    def _coin_selector_rate_limit_backoff_remaining(self, *, now=None):
        current = time.time() if now is None else float(now)
        until = float(
            getattr(self, 'coin_selector_rate_limit_backoff_until', 0.0) or 0.0
        )
        return max(0.0, until - current)

    def _activate_coin_selector_rate_limit_backoff(self, cfg, reason):
        try:
            duration = float((cfg or {}).get('rate_limit_backoff_seconds', 120.0) or 120.0)
        except (TypeError, ValueError):
            duration = 120.0
        duration = max(60.0, min(900.0, duration))
        now = time.time()
        current_until = float(
            getattr(self, 'coin_selector_rate_limit_backoff_until', 0.0) or 0.0
        )
        self.coin_selector_rate_limit_backoff_until = max(current_until, now + duration)
        logger.warning(
            "CoinSelector exchange rate limit detected; scanner backing off %.0fs: %s",
            duration,
            str(reason or 'rate limited')[:300],
        )
        return duration

    def _rotating_coin_selector_batch(self, items, limit, *, cursor_attr):
        items = list(items or [])
        if not items:
            setattr(self, cursor_attr, 0)
            return []
        try:
            batch_limit = max(1, int(limit))
        except (TypeError, ValueError):
            batch_limit = len(items)
        if batch_limit >= len(items):
            setattr(self, cursor_attr, 0)
            return items
        start = int(getattr(self, cursor_attr, 0) or 0) % len(items)
        batch = [items[(start + offset) % len(items)] for offset in range(batch_limit)]
        setattr(self, cursor_attr, (start + batch_limit) % len(items))
        return batch

    def _get_micro_auto_config(self):
        raw = self.get_runtime_trade_config().get('micro_auto', {})
        return normalize_micro_auto_config(raw if isinstance(raw, dict) else {})

    def _micro_auto_enabled(self):
        cfg = self._get_micro_auto_config()
        return bool(cfg.get('enabled', False)) and not self.is_upbit_mode()

    def _get_scanner_scan_interval_seconds(self, common_cfg=None):
        if common_cfg is None:
            try:
                common_cfg = self.get_runtime_common_settings()
            except Exception:
                common_cfg = {}
        if not isinstance(common_cfg, dict):
            common_cfg = {}
        try:
            interval = float(common_cfg.get('scanner_scan_interval_seconds', 60.0) or 60.0)
        except (TypeError, ValueError):
            interval = 60.0
        return max(10.0, min(300.0, interval))

    def _extract_market_min_notional(self, market):
        def _safe_positive(value):
            try:
                parsed = float(value)
                return parsed if parsed > 0 and np.isfinite(parsed) else None
            except (TypeError, ValueError):
                return None

        if not isinstance(market, dict):
            return 0.0
        limits = market.get('limits', {}) if isinstance(market.get('limits', {}), dict) else {}
        info = market.get('info', {}) if isinstance(market.get('info', {}), dict) else {}
        candidates = []
        cost_limits = limits.get('cost', {}) if isinstance(limits.get('cost', {}), dict) else {}
        candidates.extend([cost_limits.get('min'), info.get('notional'), info.get('minNotional')])
        filters = info.get('filters', [])
        if isinstance(filters, list):
            for item in filters:
                if not isinstance(item, dict):
                    continue
                if item.get('filterType') in {'MIN_NOTIONAL', 'NOTIONAL'}:
                    candidates.extend([item.get('notional'), item.get('minNotional')])
        values = [value for value in (_safe_positive(item) for item in candidates) if value is not None]
        return min(values) if values else 0.0

    async def _get_symbol_min_notional(self, symbol):
        try:
            markets = await asyncio.to_thread(self.market_data_exchange.load_markets)
            market = self._coin_selector_market_for_symbol(symbol, markets)
            min_notional = self._extract_market_min_notional(market)
            if min_notional > 0:
                return min_notional
        except Exception as exc:
            logger.debug(f"Micro Auto market data minNotional lookup failed for {symbol}: {exc}")
        try:
            markets = await asyncio.to_thread(self.exchange.load_markets)
            market = self._coin_selector_market_for_symbol(symbol, markets)
            return self._extract_market_min_notional(market)
        except Exception as exc:
            logger.debug(f"Micro Auto private exchange minNotional lookup failed for {symbol}: {exc}")
            return 0.0

    async def evaluate_micro_auto_candidates(self, *, force=False):
        cfg = self._get_micro_auto_config()
        now = time.time()
        cached = self.micro_auto_last_scan if isinstance(self.micro_auto_last_scan, dict) else {}
        if (not force) and cached and (now - float(cached.get('generated_at_ts', 0) or 0)) < 60:
            return cached

        total, free, _ = await self.get_balance_info()
        if self.is_upbit_mode():
            report = {
                'generated_at_ts': now,
                'criteria': cfg,
                'selected': [],
                'rejected': [],
                'note': 'Micro Auto V1은 Binance Futures 전용입니다.',
            }
            self.micro_auto_last_scan = report
            return report

        coin_report = await self.evaluate_coin_selector(force=force)
        selected = [
            item for item in list((coin_report or {}).get('selected') or [])
            if item.get('selection_state') == 'SELECTED'
        ]
        markets = await self._load_coin_selector_markets()
        feasible = []
        rejected = []
        for item in selected:
            symbol = item.get('normalized_symbol') or item.get('exchange_symbol') or item.get('symbol')
            exchange_symbol = item.get('exchange_symbol') or symbol
            market = self._coin_selector_market_for_symbol(exchange_symbol, markets)
            min_notional = self._extract_market_min_notional(market)
            if min_notional <= 0:
                min_notional = await self._get_symbol_min_notional(symbol)
            decision = assess_micro_market_feasibility(
                symbol=symbol,
                total_equity_usdt=total,
                free_usdt=free,
                min_notional_usdt=min_notional,
                cfg=cfg,
                assumed_stop_distance_pct=1.5,
            )
            row = dict(item)
            row.update({
                'micro_auto': decision,
                'micro_min_notional': min_notional,
                'micro_accepted': bool(decision.get('accepted')),
                'micro_reject_code': decision.get('reject_code'),
            })
            if decision.get('accepted'):
                feasible.append(row)
            else:
                rejected.append(row)

        report = {
            'generated_at_ts': now,
            'generated_at': datetime.now(timezone.utc).isoformat(),
            'criteria': cfg,
            'coin_selector': coin_report,
            'total_equity_usdt': total,
            'free_usdt': free,
            'equity_for_micro': min(max(0.0, total if total > 0 else free), float(cfg.get('equity_cap_usdt', 10.0) or 10.0)),
            'selected': feasible,
            'rejected': rejected,
            'total_checked': len(selected),
        }
        self.micro_auto_last_scan = report
        return report

    async def _apply_micro_auto_to_utbreakout_plan(
        self,
        *,
        symbol,
        side,
        base_plan,
        selected_set,
        auto_analysis,
        cfg,
        entry_price,
        atr_value,
        ut_stop,
        total_balance,
        free_balance,
        market_quality_risk_multiplier=1.0,
    ):
        if not self._micro_auto_enabled():
            return base_plan, None

        micro_cfg = self._get_micro_auto_config()
        try:
            quality_multiplier = min(1.0, max(0.0, float(market_quality_risk_multiplier or 1.0)))
        except (TypeError, ValueError):
            quality_multiplier = 1.0
        if quality_multiplier < 0.999:
            micro_cfg = dict(micro_cfg)
            micro_cfg['risk_per_trade_pct'] = float(micro_cfg.get('risk_per_trade_pct', 0.0) or 0.0) * quality_multiplier
            micro_cfg['max_risk_usdt'] = float(micro_cfg.get('max_risk_usdt', 0.0) or 0.0) * quality_multiplier
        min_notional = await self._get_symbol_min_notional(symbol)
        auto_scores = (auto_analysis or {}).get('scores') if isinstance(auto_analysis, dict) else {}
        micro_plan = build_micro_entry_plan(
            side=side,
            entry_price=entry_price,
            atr_value=atr_value,
            ut_stop=ut_stop,
            base_plan=base_plan,
            cfg=micro_cfg,
            selected_set=selected_set,
            auto_scores=auto_scores,
            selected_timeframe=cfg.get('entry_timeframe', '15m'),
            total_equity_usdt=total_balance,
            free_usdt=free_balance,
            min_notional_usdt=min_notional,
            max_symbol_leverage=micro_cfg.get('max_leverage', 10),
        )
        if not micro_plan.get('accepted'):
            payload = dict(micro_plan)
            payload.update({
                'symbol': symbol,
                'side': side,
                'min_notional_usdt': min_notional,
                'cfg': micro_cfg,
            })
            self.micro_auto_last_rejects[symbol] = payload
            self.micro_auto_last_plan.pop(symbol, None)
            return None, payload

        merged = dict(base_plan or {})
        merged.update(micro_plan)
        merged.update({
            'strategy': base_plan.get('strategy'),
            'entry_timeframe': base_plan.get('entry_timeframe') or cfg.get('entry_timeframe', '15m'),
            'exit_timeframe': base_plan.get('exit_timeframe') or cfg.get('exit_timeframe', cfg.get('entry_timeframe', '15m')),
            'htf_timeframe': base_plan.get('htf_timeframe') or cfg.get('htf_timeframe', '1h'),
            'adaptive_timeframe_enabled': bool(cfg.get('adaptive_timeframe_enabled', False)),
            'atr': atr_value,
            'atr_pct': base_plan.get('atr_pct'),
            'decision_candle_ts': base_plan.get('decision_candle_ts'),
        })
        self.micro_auto_last_plan[symbol] = dict(merged)
        self.micro_auto_last_rejects.pop(symbol, None)
        return merged, None

    def _prediction_context_for_futures_symbol(self, symbol):
        scan = getattr(self.ctrl, 'prediction_micro_last_scan', None)
        if not isinstance(scan, dict):
            return {}
        base = str(symbol or '').upper().replace(':USDT', '').split('/', 1)[0].replace('USDT', '')
        base_aliases = {
            'BTC': ['BTC', 'BITCOIN'],
            'ETH': ['ETH', 'ETHEREUM'],
            'SOL': ['SOL', 'SOLANA'],
            'XRP': ['XRP', 'RIPPLE'],
            'BNB': ['BNB', 'BINANCE COIN'],
            'DOGE': ['DOGE', 'DOGECOIN'],
        }.get(base, [base])
        rows = list(scan.get('candidates') or []) + list(scan.get('rejects') or [])
        for row in rows:
            title = str(row.get('title') or row.get('market_title') or '').upper()
            if not title:
                continue
            if not any(alias in title for alias in base_aliases):
                continue
            if 'UP' not in title and 'DOWN' not in title:
                continue
            probability = _safe_float_or_none(row.get('fair_probability'))
            market_price = _safe_float_or_none(row.get('market_price'))
            edge = _safe_float_or_none(row.get('edge'))
            if probability is None and market_price is None:
                continue
            return {
                'prediction_title': row.get('title') or row.get('market_title'),
                'prediction_up_probability': probability,
                'prediction_market_price': market_price,
                'prediction_edge': edge,
                'prediction_score': _safe_float_or_none(row.get('score')),
            }
        return {}

    def _score_prediction_futures_context(self, context):
        context = dict(context or {})

        def _score(value, low, high):
            if value is None or high <= low:
                return 0.0
            try:
                return max(0.0, min(100.0, (float(value) - float(low)) / (float(high) - float(low)) * 100.0))
            except (TypeError, ValueError):
                return 0.0

        imbalance = _safe_float_or_none(context.get('orderbook_imbalance_pct'))
        taker_ratio = _safe_float_or_none(context.get('taker_buy_sell_ratio'))
        spread = _safe_float_or_none(context.get('futures_spread_pct'))
        depth = min(
            _safe_float_or_none(context.get('bid_depth_usdt')) or 0.0,
            _safe_float_or_none(context.get('ask_depth_usdt')) or 0.0,
        )
        funding = _safe_float_or_none(context.get('funding_rate'))
        long_short = _safe_float_or_none(context.get('long_short_ratio'))
        oi_delta = _safe_float_or_none(context.get('open_interest_delta_pct'))
        basis = _safe_float_or_none(context.get('basis_pct'))
        prediction_prob = _safe_float_or_none(context.get('prediction_up_probability'))
        prediction_edge = _safe_float_or_none(context.get('prediction_edge'))
        vol_score = _safe_float_or_none(context.get('volatility_forecast_score'))

        orderflow_score = 0.0
        if imbalance is not None and taker_ratio is not None:
            orderflow_score = min(100.0, abs(imbalance) * 5.0 + abs(taker_ratio - 1.0) * 130.0)
            if spread is not None and spread > 0.05:
                orderflow_score *= 0.45
        depth_score = _score(depth, 10000.0, 150000.0)
        funding_crowding_score = 55.0
        if funding is not None:
            funding_crowding_score = max(0.0, 100.0 - min(100.0, abs(funding) / 0.0012 * 100.0))
        if long_short is not None:
            funding_crowding_score = min(funding_crowding_score, max(0.0, 100.0 - min(100.0, abs(long_short - 1.0) / 1.2 * 100.0)))
        liquidation_proxy_score = 0.0
        if oi_delta is not None and taker_ratio is not None:
            liquidation_proxy_score = min(100.0, max(0.0, -oi_delta) * 22.0 + abs(taker_ratio - 1.0) * 120.0)
        prediction_score = 0.0
        if prediction_prob is not None:
            prediction_score = min(100.0, abs(prediction_prob - 0.5) * 240.0)
            if prediction_edge is not None:
                prediction_score = max(prediction_score, min(100.0, abs(prediction_edge) * 600.0))
        event_guard_score = 70.0
        next_funding_time = context.get('next_funding_time')
        if next_funding_time:
            minutes_to_funding = (int(next_funding_time) / 1000.0 - time.time()) / 60.0
            if 0.0 <= minutes_to_funding <= 20.0 and funding is not None and abs(funding) >= 0.0007:
                event_guard_score = 20.0
        if vol_score is None:
            vol_score = 50.0
        basis_score = 55.0
        if basis is not None:
            basis_score = max(0.0, 100.0 - min(100.0, abs(basis) / 0.18 * 100.0))
        cross_market_score = (
            orderflow_score * 0.28
            + funding_crowding_score * 0.18
            + prediction_score * 0.28
            + basis_score * 0.16
            + depth_score * 0.10
        )
        rolling_imbalance = _safe_float_or_none(context.get('rolling_orderbook_imbalance_pct'))
        rolling_delta = _safe_float_or_none(context.get('rolling_orderbook_imbalance_delta'))
        rolling_samples = int(context.get('rolling_ofi_samples') or 0)
        rolling_orderflow_score = 0.0
        if rolling_imbalance is not None and taker_ratio is not None and rolling_samples >= 3:
            rolling_orderflow_score = min(
                100.0,
                abs(rolling_imbalance) * 7.0
                + abs(taker_ratio - 1.0) * 130.0
                + (abs(rolling_delta or 0.0) * 3.0)
            )
            if spread is not None and spread > 0.05:
                rolling_orderflow_score *= 0.45
        oi_z = _safe_float_or_none(context.get('open_interest_delta_z'))
        oi_acc = _safe_float_or_none(context.get('open_interest_acceleration'))
        oi_funding_squeeze_score = funding_crowding_score
        if oi_z is not None:
            oi_base = min(100.0, max(0.0, oi_z / 2.0 * 100.0))
            acc_bonus = min(20.0, max(0.0, (oi_acc or 0.0) * 80.0))
            oi_funding_squeeze_score = min(100.0, (oi_base * 0.65) + (funding_crowding_score * 0.30) + acc_bonus)
        return {
            'prediction_orderflow_score': round(orderflow_score, 2),
            'rolling_orderflow_score': round(rolling_orderflow_score, 2),
            'prediction_depth_score': round(depth_score, 2),
            'prediction_oi_funding_score': round(funding_crowding_score, 2),
            'oi_funding_squeeze_score': round(oi_funding_squeeze_score, 2),
            'prediction_liquidation_score': round(liquidation_proxy_score, 2),
            'prediction_odds_score': round(prediction_score, 2),
            'prediction_event_guard_score': round(event_guard_score, 2),
            'prediction_volatility_score': round(float(vol_score), 2),
            'prediction_basis_score': round(basis_score, 2),
            'prediction_cross_market_score': round(cross_market_score, 2),
        }

    def _build_utbreakout_orderflow_snapshot(self, depth, timestamp=None):
        bids = depth.get('bids') if isinstance(depth, dict) else []
        asks = depth.get('asks') if isinstance(depth, dict) else []

        def _levels(rows, *, reverse=False):
            levels = []
            for row in rows or []:
                if not isinstance(row, (list, tuple)) or len(row) < 2:
                    continue
                price = _safe_float_or_none(row[0])
                qty = _safe_float_or_none(row[1])
                if price is None or qty is None or price <= 0 or qty < 0:
                    continue
                levels.append((float(price), float(qty)))
            return sorted(levels, key=lambda item: item[0], reverse=reverse)

        bid_levels = _levels(bids, reverse=True)
        ask_levels = _levels(asks, reverse=False)
        if not bid_levels or not ask_levels:
            return {}

        best_bid = bid_levels[0][0]
        best_ask = ask_levels[0][0]
        mid = (best_bid + best_ask) / 2.0
        bid_depth = sum(price * qty for price, qty in bid_levels)
        ask_depth = sum(price * qty for price, qty in ask_levels)
        if bid_depth < 0 or ask_depth < 0 or mid <= 0:
            return {}
        return {
            'timestamp': float(timestamp if timestamp is not None else time.time()),
            'bid_depth_usdt': float(bid_depth),
            'ask_depth_usdt': float(ask_depth),
            'orderbook_imbalance_pct': (bid_depth - ask_depth) / max(bid_depth + ask_depth, 1e-9) * 100.0,
            'futures_spread_pct': (best_ask - best_bid) / max(abs(mid), 1e-9) * 100.0,
            'best_bid': float(best_bid),
            'best_ask': float(best_ask),
        }

    def _update_utbreakout_orderflow_snapshots(self, symbol, snapshot=None, *, window=5, max_samples=20):
        snapshots = self._ensure_runtime_state_container('utbreakout_orderflow_snapshots')
        symbol_key = str(symbol or '')
        rows = list(snapshots.get(symbol_key) or [])
        if isinstance(snapshot, dict) and snapshot:
            clean = {}
            for key in (
                'timestamp',
                'bid_depth_usdt',
                'ask_depth_usdt',
                'orderbook_imbalance_pct',
                'futures_spread_pct',
                'best_bid',
                'best_ask',
            ):
                value = _safe_float_or_none(snapshot.get(key))
                if value is not None:
                    clean[key] = value
            if clean.get('timestamp') is not None:
                rows.append(clean)
        rows = rows[-max(1, int(max_samples or 20)):]
        snapshots[symbol_key] = rows

        try:
            window = max(1, int(window or 5))
        except (TypeError, ValueError):
            window = 5
        recent = [
            row for row in rows[-window:]
            if _safe_float_or_none((row or {}).get('orderbook_imbalance_pct')) is not None
        ]
        latest = rows[-1] if rows else {}
        context = {}
        if latest:
            context.update({
                'futures_best_bid': latest.get('best_bid'),
                'futures_best_ask': latest.get('best_ask'),
                'best_bid': latest.get('best_bid'),
                'best_ask': latest.get('best_ask'),
                'futures_spread_pct': latest.get('futures_spread_pct'),
                'bid_depth_usdt': latest.get('bid_depth_usdt'),
                'ask_depth_usdt': latest.get('ask_depth_usdt'),
                'orderbook_imbalance_pct': latest.get('orderbook_imbalance_pct'),
                'orderflow_snapshot_ts': latest.get('timestamp'),
            })
        if recent:
            imbalances = [float(row.get('orderbook_imbalance_pct')) for row in recent]
            rolling = sum(imbalances) / len(imbalances)
            delta = imbalances[-1] - imbalances[0] if len(imbalances) >= 2 else 0.0
            context.update({
                'rolling_orderbook_imbalance_pct': rolling,
                'rolling_orderbook_imbalance_delta': delta,
                'rolling_ofi_score': rolling + (delta * 0.5),
                'rolling_ofi_samples': len(imbalances),
            })
        else:
            context.update({
                'rolling_orderbook_imbalance_pct': None,
                'rolling_orderbook_imbalance_delta': None,
                'rolling_ofi_score': None,
                'rolling_ofi_samples': 0,
            })
        return {key: value for key, value in context.items() if value is not None}

    def _calculate_utbreakout_open_interest_stats(self, oi_hist):
        if not isinstance(oi_hist, list) or len(oi_hist) < 2:
            return {
                'open_interest_delta_pct': None,
                'open_interest_change_1h': None,
                'open_interest_change_4h': None,
                'open_interest_delta_z': None,
                'open_interest_acceleration': None,
                'open_interest_hist_samples': len(oi_hist) if isinstance(oi_hist, list) else 0,
            }
        rows = sorted(
            [row for row in oi_hist if isinstance(row, dict)],
            key=lambda row: int(row.get('timestamp', 0) or 0),
        )
        values = []
        for row in rows:
            value = _safe_float_or_none(row.get('sumOpenInterestValue') or row.get('sumOpenInterest'))
            if value is not None and value > 0:
                values.append(float(value))
        if len(values) < 2:
            return {
                'open_interest_delta_pct': None,
                'open_interest_change_1h': None,
                'open_interest_change_4h': None,
                'open_interest_delta_z': None,
                'open_interest_acceleration': None,
                'open_interest_hist_samples': len(values),
            }

        changes = []
        for prev, curr in zip(values, values[1:]):
            if prev == 0.0:
                continue
            pct = (curr - prev) / max(abs(prev), 1e-9) * 100.0
            if np.isfinite(pct):
                changes.append(float(pct))
        if not changes:
            return {
                'open_interest_delta_pct': None,
                'open_interest_change_1h': None,
                'open_interest_change_4h': None,
                'open_interest_delta_z': None,
                'open_interest_acceleration': None,
                'open_interest_hist_samples': len(values),
            }

        latest = changes[-1]
        change_1h = None
        if len(values) >= 5 and values[-5] > 0:
            change_1h = (values[-1] - values[-5]) / max(abs(values[-5]), 1e-9) * 100.0
        change_4h = None
        if len(values) >= 17 and values[-17] > 0:
            change_4h = (values[-1] - values[-17]) / max(abs(values[-17]), 1e-9) * 100.0
        z_score = None
        if len(changes) >= 3:
            mean = sum(changes) / len(changes)
            variance = sum((item - mean) ** 2 for item in changes) / len(changes)
            std = variance ** 0.5
            if std > 0 and np.isfinite(std):
                z_score = (latest - mean) / std
        acceleration = changes[-1] - changes[-2] if len(changes) >= 2 else None
        return {
            'open_interest_delta_pct': latest,
            'open_interest_change_1h': change_1h,
            'open_interest_change_4h': change_4h,
            'open_interest_delta_z': z_score,
            'open_interest_acceleration': acceleration,
            'open_interest_hist_samples': len(values),
        }

    def _calculate_utbreakout_funding_percentiles(self, funding_rows, current_funding):
        rates = []
        if isinstance(funding_rows, list):
            for row in funding_rows:
                if not isinstance(row, dict):
                    continue
                value = _safe_float_or_none(row.get('fundingRate'))
                if value is not None:
                    rates.append(float(value))
        current = _safe_float_or_none(current_funding)
        if current is None and rates:
            current = rates[-1]
        if current is None or not rates:
            return {}

        current_abs = abs(float(current))

        def _rank(window):
            samples = [abs(item) for item in rates[-window:] if np.isfinite(item)]
            if not samples:
                return None
            return sum(1 for item in samples if item <= current_abs) / len(samples) * 100.0

        result = {'funding_hist_samples': len(rates)}
        percentile_7d = _rank(21)
        percentile_30d = _rank(90)
        if percentile_7d is not None:
            result['funding_percentile_7d'] = percentile_7d
        if percentile_30d is not None:
            result['funding_percentile_30d'] = percentile_30d
        return result

    def _calculate_utbreakout_liquidation_proxy(self, context):
        context = dict(context or {})
        oi_change = _safe_float_or_none(
            context.get('open_interest_change_1h', context.get('open_interest_delta_pct'))
        )
        taker_ratio = _safe_float_or_none(context.get('taker_buy_sell_ratio'))
        if oi_change is None or taker_ratio is None:
            return {}
        oi_drop = max(0.0, -float(oi_change))
        taker_bias = float(taker_ratio) - 1.0
        if oi_drop < 0.15 or abs(taker_bias) < 0.02:
            return {'liquidation_imbalance': 0.0}
        imbalance = max(-1.5, min(1.5, (oi_drop / 1.20) * (taker_bias / 0.12)))
        return {'liquidation_imbalance': imbalance}

    def _calculate_utbreakout_feature_score(self, side, cfg, values):
        side = str(side or '').lower()
        values = dict(values or {})

        def _f(key):
            return _safe_float_or_none(values.get(key))

        def _score_between(value, low, high):
            if value is None or high <= low:
                return 50.0
            return max(0.0, min(100.0, (float(value) - float(low)) / (float(high) - float(low)) * 100.0))

        trend_parts = []
        adx = _f('adx')
        if adx is not None:
            trend_parts.append(_score_between(adx, 15.0, 32.0))
        htf_close = _f('htf_close')
        htf_fast = _f('htf_ema_fast')
        htf_slow = _f('htf_ema_slow')
        if htf_close is not None and htf_fast is not None and htf_slow is not None:
            aligned = (htf_close > htf_slow and htf_fast > htf_slow) if side == 'long' else (htf_close < htf_slow and htf_fast < htf_slow)
            trend_parts.append(85.0 if aligned else 25.0)
        macd_hist = _f('macd_hist')
        if macd_hist is not None:
            trend_parts.append(70.0 if (macd_hist > 0 if side == 'long' else macd_hist < 0) else 35.0)
        trend = sum(trend_parts) / len(trend_parts) if trend_parts else 50.0

        atr_pct = _f('atr_pct')
        range_ratio = _f('range_expansion_ratio')
        bb_percentile = _f('bb_width_percentile')
        volatility_parts = []
        if atr_pct is not None:
            volatility_parts.append(85.0 if 0.12 <= atr_pct <= 1.20 else 40.0 if atr_pct < 0.12 else 55.0)
        if range_ratio is not None:
            volatility_parts.append(_score_between(range_ratio, 0.85, 1.70))
        if bb_percentile is not None:
            volatility_parts.append(max(0.0, min(100.0, 100.0 - abs(bb_percentile - 35.0))))
        volatility = sum(volatility_parts) / len(volatility_parts) if volatility_parts else 50.0

        rolling_ofi = _f('rolling_orderbook_imbalance_pct')
        taker_ratio = _f('taker_buy_sell_ratio')
        orderflow_parts = []
        if rolling_ofi is not None:
            signed = rolling_ofi if side == 'long' else -rolling_ofi
            orderflow_parts.append(_score_between(signed, -3.0, 8.0))
        if taker_ratio is not None:
            signed_ratio = (taker_ratio - 1.0) if side == 'long' else (1.0 - taker_ratio)
            orderflow_parts.append(_score_between(signed_ratio, -0.04, 0.09))
        orderflow = sum(orderflow_parts) / len(orderflow_parts) if orderflow_parts else 50.0

        oi_z = _f('open_interest_delta_z')
        oi_acc = _f('open_interest_acceleration')
        funding = _f('funding_rate')
        long_short = _f('long_short_ratio')
        basis = _f('basis_pct')
        oi_parts = []
        if oi_z is not None:
            oi_parts.append(_score_between(oi_z, -0.25, 1.75))
        if oi_acc is not None:
            oi_parts.append(_score_between(oi_acc, -0.20, 0.45))
        if funding is not None:
            adverse = funding if side == 'long' else -funding
            oi_parts.append(max(0.0, 100.0 - _score_between(adverse, 0.0002, 0.0012)))
        if long_short is not None:
            adverse_ratio = long_short if side == 'long' else (1.0 / max(long_short, 1e-9))
            oi_parts.append(max(0.0, 100.0 - _score_between(adverse_ratio, 1.20, 2.20)))
        if basis is not None:
            adverse_basis = basis if side == 'long' else -basis
            oi_parts.append(max(0.0, 100.0 - _score_between(adverse_basis, 0.05, 0.35)))
        oi_funding = sum(oi_parts) / len(oi_parts) if oi_parts else 50.0

        spread = _f('futures_spread_pct')
        bid_depth = _f('bid_depth_usdt') or 0.0
        ask_depth = _f('ask_depth_usdt') or 0.0
        min_depth = min(bid_depth, ask_depth)
        liquidity_parts = []
        if spread is not None:
            liquidity_parts.append(max(0.0, 100.0 - _score_between(spread, 0.02, 0.12)))
        if min_depth > 0:
            liquidity_parts.append(_score_between(min_depth, 10000.0, 150000.0))
        liquidity_cost = sum(liquidity_parts) / len(liquidity_parts) if liquidity_parts else 50.0

        components = {
            'trend': round(trend, 2),
            'volatility': round(volatility, 2),
            'orderflow': round(orderflow, 2),
            'oi_funding': round(oi_funding, 2),
            'liquidity_cost': round(liquidity_cost, 2),
        }
        score = (
            trend * 0.24
            + volatility * 0.18
            + orderflow * 0.22
            + oi_funding * 0.20
            + liquidity_cost * 0.16
        )
        score = max(0.0, min(100.0, float(score)))
        return {
            'score': round(score, 2),
            'components': components,
            'reason': (
                f"trend {components['trend']:.1f}, vol {components['volatility']:.1f}, "
                f"OFI {components['orderflow']:.1f}, OI {components['oi_funding']:.1f}, "
                f"liq {components['liquidity_cost']:.1f}"
            ),
        }

    async def _fetch_utbreakout_futures_context(self, symbol):
        if self.is_upbit_mode():
            return {}
        now = time.time()
        cached = self.utbreakout_futures_context_cache.get(symbol)
        cached_at = float(cached.get('cached_at', 0) or 0) if isinstance(cached, dict) else 0.0
        cache_fresh = isinstance(cached, dict) and (now - cached_at) < 300
        context = dict((cached or {}).get('data') or {}) if cache_fresh else {}
        rest_symbol = ''
        try:
            rest_symbol = self.ctrl._build_binance_futures_rest_symbol(symbol)
        except Exception:
            rest_symbol = ''
        if not rest_symbol:
            return {}

        if not cache_fresh:
            context = {}
            try:
                premium = await self.ctrl._fetch_binance_public_json('/fapi/v1/premiumIndex', {'symbol': rest_symbol})
                if isinstance(premium, dict):
                    context.update({
                        'funding_rate': _safe_float_or_none(premium.get('lastFundingRate')),
                        'next_funding_time': int(premium.get('nextFundingTime') or 0) or None,
                        'mark_price': _safe_float_or_none(premium.get('markPrice')),
                        'index_price': _safe_float_or_none(premium.get('indexPrice')),
                    })
            except Exception as exc:
                context['futures_context_error'] = f"premiumIndex: {exc}"

            try:
                oi = await self.ctrl._fetch_binance_public_json('/fapi/v1/openInterest', {'symbol': rest_symbol})
                if isinstance(oi, dict):
                    context['open_interest'] = _safe_float_or_none(oi.get('openInterest'))
                    context['open_interest_ts'] = int(oi.get('time') or 0) or None
            except Exception as exc:
                if 'futures_context_error' not in context:
                    context['futures_context_error'] = f"openInterest: {exc}"

            if context.get('open_interest') is not None and context.get('mark_price') is not None:
                try:
                    context['open_interest_usdt'] = float(context['open_interest']) * float(context['mark_price'])
                except (TypeError, ValueError):
                    pass

            try:
                oi_hist = await self.ctrl._fetch_binance_public_json('/futures/data/openInterestHist', {'symbol': rest_symbol, 'period': '15m', 'limit': 20})
                context.update({
                    key: value for key, value in self._calculate_utbreakout_open_interest_stats(oi_hist).items()
                    if value is not None
                })
            except Exception as exc:
                context.setdefault('futures_context_error', f"openInterestHist: {exc}")

            try:
                funding_rows = await self.ctrl._fetch_binance_public_json(
                    '/fapi/v1/fundingRate',
                    {'symbol': rest_symbol, 'limit': 120},
                )
                context.update(
                    self._calculate_utbreakout_funding_percentiles(
                        funding_rows,
                        context.get('funding_rate'),
                    )
                )
            except Exception as exc:
                context.setdefault('futures_context_error', f"fundingRate: {exc}")

            try:
                ratio_rows = await self.ctrl._fetch_binance_public_json('/futures/data/globalLongShortAccountRatio', {'symbol': rest_symbol, 'period': '15m', 'limit': 1})
                if isinstance(ratio_rows, list) and ratio_rows:
                    latest = ratio_rows[-1]
                    context.update({
                        'long_short_ratio': _safe_float_or_none(latest.get('longShortRatio')),
                        'long_account': _safe_float_or_none(latest.get('longAccount')),
                        'short_account': _safe_float_or_none(latest.get('shortAccount')),
                    })
            except Exception as exc:
                context.setdefault('futures_context_error', f"globalLongShortAccountRatio: {exc}")

            if context.get('mark_price') is not None and context.get('index_price') is not None:
                try:
                    context['basis_pct'] = (
                        (float(context['mark_price']) - float(context['index_price']))
                        / max(abs(float(context['index_price'])), 1e-9)
                        * 100.0
                    )
                except (TypeError, ValueError):
                    pass
            context.update(self._prediction_context_for_futures_symbol(symbol))

        orderflow_cache_ttl = 30.0
        snapshots = getattr(self, 'utbreakout_orderflow_snapshots', {})
        latest_snapshot = None
        if isinstance(snapshots, dict):
            rows = snapshots.get(symbol)
            latest_snapshot = rows[-1] if isinstance(rows, list) and rows else None
        latest_snapshot_ts = float((latest_snapshot or {}).get('timestamp', 0) or 0)
        orderflow_due = not latest_snapshot_ts or (now - latest_snapshot_ts) >= orderflow_cache_ttl
        try:
            if orderflow_due:
                depth = await self.ctrl._fetch_binance_public_json('/fapi/v1/depth', {'symbol': rest_symbol, 'limit': 20})
                snapshot = self._build_utbreakout_orderflow_snapshot(depth, timestamp=now)
                context.update(
                    self._update_utbreakout_orderflow_snapshots(
                        symbol,
                        snapshot,
                        window=int(context.get('rolling_ofi_window') or 5),
                    )
                )
            else:
                context.update(
                    self._update_utbreakout_orderflow_snapshots(
                        symbol,
                        None,
                        window=int(context.get('rolling_ofi_window') or 5),
                    )
                )
        except Exception as exc:
            context.setdefault('futures_context_error', f"depth: {exc}")
            context.update(
                self._update_utbreakout_orderflow_snapshots(
                    symbol,
                    None,
                    window=int(context.get('rolling_ofi_window') or 5),
                )
            )

        if orderflow_due or not cache_fresh:
            try:
                taker_rows = await self.ctrl._fetch_binance_public_json('/futures/data/takerlongshortRatio', {'symbol': rest_symbol, 'period': '15m', 'limit': 1})
                if isinstance(taker_rows, list) and taker_rows:
                    latest = taker_rows[-1]
                    context.update({
                        'taker_buy_sell_ratio': _safe_float_or_none(latest.get('buySellRatio')),
                        'taker_buy_vol': _safe_float_or_none(latest.get('buyVol')),
                        'taker_sell_vol': _safe_float_or_none(latest.get('sellVol')),
                    })
            except Exception as exc:
                context.setdefault('futures_context_error', f"takerlongshortRatio: {exc}")
        context.update(self._calculate_utbreakout_liquidation_proxy(context))
        clean_context = {key: value for key, value in context.items() if value is not None}
        self.utbreakout_futures_context_cache[symbol] = {
            'cached_at': cached_at if cache_fresh else now,
            'data': clean_context,
        }
        return clean_context

    async def _fetch_utbreakout_market_regime_context(self, cfg):
        if self.is_upbit_mode() or not bool(cfg.get('market_quality_regime_enabled', True)):
            return {}
        raw_symbols = cfg.get('market_quality_regime_symbols') or ['BTC/USDT', 'ETH/USDT']
        if isinstance(raw_symbols, str):
            symbols = [part.strip() for part in raw_symbols.split(',') if part.strip()]
        else:
            symbols = [str(part or '').strip() for part in raw_symbols if str(part or '').strip()]
        normalized_symbols = []
        for symbol in symbols:
            clean = symbol.upper().replace(':USDT', '')
            if '/' not in clean and clean.endswith('USDT'):
                clean = f"{clean[:-4]}/USDT"
            elif '/' not in clean:
                clean = f"{clean}/USDT"
            if clean not in normalized_symbols:
                normalized_symbols.append(clean)
        normalized_symbols = normalized_symbols[:4] or ['BTC/USDT', 'ETH/USDT']

        timeframe = str(cfg.get('market_quality_regime_timeframe', '4h') or '4h')
        cache_key = f"{timeframe}:{','.join(normalized_symbols)}"
        now = time.time()
        cached = self.utbreakout_market_regime_cache.get(cache_key)
        if isinstance(cached, dict) and (now - float(cached.get('cached_at', 0) or 0)) < 300:
            return dict(cached.get('data') or {})

        ema_fast_len = int(cfg.get('ema_fast', 50) or 50)
        ema_slow_len = int(cfg.get('ema_slow', 200) or 200)
        limit = max(ema_slow_len + 10, 80)
        items = {}
        for symbol in normalized_symbols:
            ohlcv = None
            candidates = [symbol]
            futures_symbol = f"{symbol}:USDT"
            if futures_symbol not in candidates:
                candidates.append(futures_symbol)
            last_error = None
            for candidate in candidates:
                try:
                    ohlcv = await asyncio.to_thread(
                        self.market_data_exchange.fetch_ohlcv,
                        candidate,
                        timeframe,
                        limit=limit
                    )
                    if ohlcv:
                        break
                except Exception as exc:
                    last_error = exc
                    ohlcv = None
            if not ohlcv:
                items[symbol] = {'direction': 'unknown', 'error': str(last_error or 'no ohlcv'), 'timeframe': timeframe}
                continue

            try:
                df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                for col in ['open', 'high', 'low', 'close', 'volume']:
                    df[col] = pd.to_numeric(df[col], errors='coerce')
                closed = df.iloc[:-1].dropna(subset=['open', 'high', 'low', 'close']).reset_index(drop=True)
                if len(closed) < ema_slow_len + 2:
                    items[symbol] = {
                        'direction': 'unknown',
                        'error': f"data {len(closed)}/{ema_slow_len + 2}",
                        'timeframe': timeframe,
                    }
                    continue
                close_series = closed['close'].astype(float)
                close = float(close_series.iloc[-1])
                ema_fast = float(close_series.ewm(span=ema_fast_len, adjust=False).mean().iloc[-1])
                ema_slow = float(close_series.ewm(span=ema_slow_len, adjust=False).mean().iloc[-1])
                prev_idx = -4 if len(close_series) >= 4 else 0
                prev_close = float(close_series.iloc[prev_idx])
                return_lookback_pct = (close - prev_close) / max(abs(prev_close), 1e-9) * 100.0
                if close > ema_slow and ema_fast > ema_slow:
                    direction = 'long'
                elif close < ema_slow and ema_fast < ema_slow:
                    direction = 'short'
                else:
                    direction = 'neutral'
                items[symbol] = {
                    'direction': direction,
                    'close': close,
                    'ema_fast': ema_fast,
                    'ema_slow': ema_slow,
                    'return_lookback_pct': return_lookback_pct,
                    'timeframe': timeframe,
                }
            except Exception as exc:
                items[symbol] = {'direction': 'unknown', 'error': str(exc), 'timeframe': timeframe}

        summary_parts = []
        for symbol, item in items.items():
            direction = str(item.get('direction') or 'unknown').upper()
            ret_pct = item.get('return_lookback_pct')
            ret_text = f" {float(ret_pct):+.2f}%" if self._is_valid_number(ret_pct) else ""
            summary_parts.append(f"{symbol} {direction}{ret_text}")
        data = {
            'items': items,
            'summary': ', '.join(summary_parts) if summary_parts else 'market regime data unavailable',
            'timeframe': timeframe,
        }
        self.utbreakout_market_regime_cache[cache_key] = {'cached_at': now, 'data': data}
        return data

    async def _load_coin_selector_markets(self):
        try:
            markets = await asyncio.to_thread(self.market_data_exchange.load_markets)
            return markets if isinstance(markets, dict) else {}
        except Exception as exc:
            logger.warning(f"CoinSelector markets load failed: {exc}")
            return {}

    def _coin_selector_market_for_symbol(self, symbol, markets):
        if not isinstance(markets, dict):
            return None
        normalized_items = normalize_coin_selector_custom_symbols([symbol])
        normalized = normalized_items[0] if normalized_items else str(symbol or '').replace(':USDT', '').strip().upper()
        base = normalized.split('/', 1)[0] if '/' in normalized else normalized.replace('USDT', '')
        quote = normalized.split('/', 1)[1] if '/' in normalized else 'USDT'
        keys = []

        def _add_key(key):
            key = str(key or '').strip()
            if key and key not in keys:
                keys.append(key)

        if quote == 'USDT':
            _add_key(f"{base}/USDT:USDT")
            _add_key(f"{normalized}:USDT")
        _add_key(symbol)
        _add_key(normalized)
        if quote == 'USDT':
            _add_key(f"{base}/USDT")

        for key in keys:
            market = markets.get(key)
            if not isinstance(market, dict):
                continue
            market_symbol = market.get('symbol') or key
            if coin_selector_market_is_usdt_perpetual(market_symbol, market):
                return market
        return None

    def _coin_selector_should_include_tradifi_universe(self, cfg, custom_enabled):
        if custom_enabled or self.is_upbit_mode():
            return False
        if not bool(cfg.get('include_tradifi_universe', True)):
            return False
        ctrl = getattr(self, 'ctrl', None)
        if ctrl is None or not hasattr(ctrl, 'get_exchange_mode'):
            return False
        if ctrl.get_exchange_mode() != BINANCE_MAINNET:
            return False
        return bool(self._coin_selector_tradifi_regular_session_status().get('open'))

    def _coin_selector_tradifi_regular_session_status(self):
        return us_equity_regular_session_status()

    def _coin_selector_tradifi_closed_reject_reason(self, status):
        status = status if isinstance(status, dict) else {}
        reason = status.get('reason') or 'closed'
        local_time = status.get('local_time') or 'unknown'
        return (
            "REJECTED_TRADFI_REGULAR_SESSION_CLOSED: "
            f"US equity regular session is closed ({reason}, {local_time})"
        )

    def _coin_selector_is_tradifi_market(self, symbol, market):
        return coin_selector_market_is_tradifi_perpetual(symbol, market)

    def _coin_selector_exchange_symbol_for_custom(self, symbol, markets):
        normalized = normalize_coin_selector_custom_symbols([symbol])
        normalized = normalized[0] if normalized else str(symbol or '').strip().upper()
        market = self._coin_selector_market_for_symbol(normalized, markets)
        if isinstance(market, dict) and market.get('symbol'):
            return market.get('symbol')
        if normalized.endswith('/USDT'):
            return f"{normalized}:USDT"
        return normalized

    def _calculate_coin_selector_selection_metrics(self, df, cfg, selector_context=None):
        try:
            if df is None or len(df) < 20:
                return {}
            closed = df.iloc[:-1].copy().reset_index(drop=True)
            if len(closed) < 20:
                closed = df.copy().reset_index(drop=True)
            close = pd.to_numeric(closed['close'], errors='coerce').dropna().astype(float)
            if len(close) < 20:
                return {}
            lookback = max(12, int(cfg.get('selection_return_lookback_bars', 96) or 96))
            lookback = min(lookback, len(close) - 1)
            window = close.tail(lookback + 1)
            returns = window.pct_change().dropna()
            if returns.empty:
                return {}
            start = float(window.iloc[0])
            end = float(window.iloc[-1])
            return_lookback_pct = (end / max(start, 1e-9) - 1.0) * 100.0
            realized_vol_pct = float(returns.std(ddof=0) * 100.0)
            mean_return = float(returns.mean())
            rolling_sharpe = 0.0
            if realized_vol_pct > 0:
                rolling_sharpe = float(mean_return / max(float(returns.std(ddof=0)), 1e-12) * np.sqrt(len(returns)))
            direction = 1 if return_lookback_pct >= 0 else -1
            momentum_consistency = float(((returns * direction) > 0).mean())
            path = float(window.diff().abs().dropna().sum())
            directional_efficiency = abs(end - start) / max(path, 1e-9)
            running_peak = window.cummax()
            drawdowns = (running_peak - window) / running_peak.replace(0, np.nan) * 100.0
            running_trough = window.cummin()
            rebounds = (window - running_trough) / running_trough.replace(0, np.nan) * 100.0
            bar_returns_pct = returns * 100.0
            metrics = {
                'return_lookback_pct': round(float(return_lookback_pct), 4),
                'realized_vol_pct': round(float(realized_vol_pct), 4),
                'rolling_sharpe': round(float(rolling_sharpe), 4),
                'momentum_consistency': round(float(momentum_consistency), 4),
                'directional_efficiency': round(float(directional_efficiency), 4),
                'max_drawdown_pct': round(float(drawdowns.max(skipna=True) or 0.0), 4),
                'max_rebound_pct': round(float(rebounds.max(skipna=True) or 0.0), 4),
                'tail_bar_move_pct': round(float(bar_returns_pct.abs().max() or 0.0), 4),
            }
            context = selector_context if isinstance(selector_context, dict) else {}
            dispersion = context.get('cross_sectional_dispersion_pct')
            if self._is_valid_number(dispersion):
                metrics['cross_sectional_dispersion_pct'] = round(float(dispersion), 4)
            return metrics
        except Exception as exc:
            logger.debug(f"CoinSelector selection quality metrics failed: {exc}")
            return {}

    def _build_utbreakout_selector_quality(self, symbol):
        item = None
        scores = getattr(self, 'coin_selector_symbol_scores', {})
        if isinstance(scores, dict):
            candidates = [
                symbol,
                str(symbol or '').replace(':USDT', ''),
                str(symbol or '').upper(),
            ]
            for key in candidates:
                if key in scores and isinstance(scores.get(key), dict):
                    item = scores.get(key)
                    break
        if not isinstance(item, dict):
            return {
                'score': None,
                'risk_multiplier': 1.0,
                'summary': 'selector quality neutral: no recent CoinSelector score',
            }
        cfg = self._get_coin_selector_config()
        try:
            score = float(item.get('score', 0.0) or 0.0)
            min_score = float(cfg.get('min_final_score', 55.0) or 55.0)
        except (TypeError, ValueError):
            score = 0.0
            min_score = 55.0
        if score >= 78.0:
            multiplier = 1.0
            label = 'strong'
        elif score >= 68.0:
            multiplier = 0.90
            label = 'good'
        elif score >= min_score:
            multiplier = 0.75
            label = 'marginal'
        else:
            multiplier = 0.50
            label = 'weak'
        return {
            'score': round(score, 2),
            'risk_multiplier': multiplier,
            'summary': (
                f"selector quality {label} {score:.1f}/100 x{multiplier:.2f} "
                f"(Sharpe {item.get('rolling_sharpe', 'n/a')}, "
                f"vol {item.get('realized_vol_pct', 'n/a')}%, "
                f"ret {item.get('return_lookback_pct', 'n/a')}%)"
            ),
            'candidate': item,
        }

    async def _score_coin_selector_candidate(self, base_candidate, cfg, strategy_params, selector_context=None):
        symbol = base_candidate.get('exchange_symbol') or base_candidate.get('symbol')
        try:
            ut_cfg = self._get_utbot_filtered_breakout_config(strategy_params)
            entry_tf = str(ut_cfg.get('entry_timeframe', '15m') or '15m')
            ohlcv = await asyncio.to_thread(self.market_data_exchange.fetch_ohlcv, symbol, entry_tf, limit=300)
            if not ohlcv:
                raise ValueError("empty OHLCV")
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            for col in ['open', 'high', 'low', 'close', 'volume']:
                df[col] = pd.to_numeric(df[col], errors='coerce')
            selection_metrics = self._calculate_coin_selector_selection_metrics(df, cfg, selector_context=selector_context)
            analysis = await self._build_utbreakout_auto_analysis(symbol, df, ut_cfg)
            selected_set_id, auto_reason = self._select_utbreakout_auto_set(analysis, ut_cfg)
            selected_set_info = self._get_utbreakout_set_info(ut_cfg, selected_set_id)
            decision = select_adaptive_timeframe(
                analysis.get('timeframes', {}),
                ut_cfg,
                state=self.utbreakout_adaptive_tf_state.get(symbol, {}),
                position_side='none',
            )
            futures_context = await self._fetch_utbreakout_futures_context(symbol)
            result = finalize_coin_selector_candidate(
                base_candidate,
                auto_analysis=analysis,
                selected_set_id=selected_set_id,
                selected_set_info=selected_set_info,
                adaptive_decision=decision,
                futures_context=futures_context,
                selection_metrics=selection_metrics,
                cfg=cfg,
            )
            result['auto_selection_reason'] = auto_reason
            if bool(ut_cfg.get('ev_adaptive_enabled', False)):
                scores = analysis.get('scores') if isinstance(analysis.get('scores'), dict) else {}
                selected_tf = str(
                    decision.get('selected_tf')
                    or decision.get('timeframe')
                    or entry_tf
                )
                tf_values = (
                    analysis.get('timeframes', {}).get(selected_tf, {})
                    if isinstance(analysis.get('timeframes'), dict)
                    else {}
                )
                ev_values = dict(tf_values or {})
                ev_values.update(selection_metrics or {})
                ev_values.update(futures_context or {})
                ev_values.setdefault(
                    'entry_price',
                    ev_values.get('close') or float(df.iloc[-2]['close']),
                )
                ev_values.setdefault(
                    'recent_rebound_pct',
                    selection_metrics.get('max_rebound_pct')
                    if isinstance(selection_metrics, dict)
                    else None,
                )
                side = str(scores.get('dominant_side') or '').lower()
                ev_candidate = evaluate_ev_adaptive_entry(
                    side=side,
                    candidate_type='fresh_signal',
                    values=ev_values,
                    config=_ev_adaptive_runtime_config(ut_cfg),
                )
                atr_pct_value = max(0.0, float(ev_values.get('atr_pct', 0.0) or 0.0))
                stop_atr = max(0.1, float(ut_cfg.get('stop_atr_multiplier', 1.5) or 1.5))
                risk_distance_pct = atr_pct_value * stop_atr
                notional_per_risk = (
                    100.0 / risk_distance_pct
                    if risk_distance_pct > 0
                    else 1_000_000_000.0
                )
                ev_net = evaluate_net_edge(
                    risk_usdt=1.0,
                    planned_notional=notional_per_risk,
                    win_probability=ev_candidate.win_probability,
                    gross_win_r=ev_candidate.gross_win_r,
                    config=_ev_adaptive_runtime_config(ut_cfg),
                )
                result.update({
                    'ev_allowed': bool(ev_candidate.allowed and ev_net.allowed),
                    'ev_mode': ev_candidate.mode,
                    'ev_score': ev_candidate.score,
                    'ev_win_probability': ev_candidate.win_probability,
                    'ev_net_edge_r': ev_net.expected_net_r,
                    'ev_cost_r': ev_net.cost_r,
                    'ev_reason': (
                        ev_net.reason
                        if ev_candidate.allowed
                        else '; '.join(ev_candidate.blockers)
                    ),
                })
                result.setdefault('scanner_warnings', []).append("EV diagnostic only at scanner stage")
                if not result['ev_allowed']:
                    result.setdefault('soft_warnings', []).append('EV_EDGE_NOT_ACTIONABLE')
            return result
        except Exception as exc:
            result = dict(base_candidate)
            result['accepted'] = False
            result.setdefault('reject_reasons', []).append('REJECTED_ANALYSIS_ERROR')
            result['analysis_error'] = str(exc)
            result['score'] = 0.0
            result['selection_state'] = 'REJECTED'
            return result

    async def evaluate_coin_selector(self, *, force=False, custom_universe_override=False):
        cfg = self._get_coin_selector_config()
        strategy_params = self.get_runtime_strategy_params()
        active_strategy = str(
            strategy_params.get('active_strategy', 'utbot') or 'utbot'
        ).lower()
        custom_symbols = normalize_coin_selector_custom_symbols(cfg.get('custom_symbols'))
        custom_enabled = bool(cfg.get('custom_universe_enabled', False)) or bool(custom_universe_override)
        now = time.time()
        tradifi_session_status = self._coin_selector_tradifi_regular_session_status()
        tradifi_session_open = bool(tradifi_session_status.get('open'))
        cached = self.coin_selector_last_result if isinstance(self.coin_selector_last_result, dict) else {}
        refresh_interval = float(cfg.get('refresh_interval_seconds', 300.0) or 300.0)
        backoff_remaining = self._coin_selector_rate_limit_backoff_remaining(now=now)
        if backoff_remaining > 0 and cached:
            cached['rate_limit_backoff_remaining_seconds'] = round(backoff_remaining, 1)
            return cached
        if (not force) and cached and (now - float(cached.get('generated_at_ts', 0) or 0)) < refresh_interval:
            cached_cfg = cached.get('criteria') if isinstance(cached.get('criteria'), dict) else {}
            cached_custom = bool(cached_cfg.get('custom_universe_enabled', False))
            cached_symbols = normalize_coin_selector_custom_symbols(cached_cfg.get('custom_symbols'))
            cached_tradifi_open = cached.get('tradifi_regular_session_open')
            if (
                cached_custom == custom_enabled
                and (not custom_enabled or cached_symbols == custom_symbols)
                and float(
                    cached_cfg.get('min_quote_volume_usdt', 0.0)
                    or 0.0
                ) == float(cfg.get('min_quote_volume_usdt', 0.0) or 0.0)
                and int(
                    cached_cfg.get('min_trade_count', 0)
                    or 0
                ) == int(cfg.get('min_trade_count', 0) or 0)
                and float(
                    cached_cfg.get('max_spread_pct', 0.0)
                    or 0.0
                ) == float(cfg.get('max_spread_pct', 0.0) or 0.0)
                and cached_tradifi_open is not None
                and bool(cached_tradifi_open) == tradifi_session_open
            ):
                return cached

        if self.is_upbit_mode():
            report = {
                'generated_at_ts': now,
                'selected': [],
                'reject_counts': {'REJECTED_UPBIT_MODE': 1},
                'total_scored': 0,
                'total_rejected': 0,
                'criteria': cfg,
                'note': 'CoinSelector V2는 Binance Futures 전용입니다.',
            }
            self.coin_selector_last_result = report
            self.coin_selector_symbol_scores = {}
            return report

        markets = await self._load_coin_selector_markets()
        if custom_enabled and not custom_symbols:
            report = {
                'generated_at_ts': now,
                'generated_at': datetime.now(timezone.utc).isoformat(),
                'selected': [],
                'reject_counts': {'REJECTED_CUSTOM_SYMBOLS_EMPTY': 1},
                'total_scored': 0,
                'total_rejected': 0,
                'criteria': dict(cfg, custom_universe_enabled=True, custom_symbols=[]),
                'analysis_limit': 0,
                'total_base_candidates': 0,
                'total_unanalyzed': 0,
                'note': '커스텀 코인 목록이 비어 있습니다.',
            }
            self.coin_selector_last_result = report
            self.coin_selector_symbol_scores = {}
            return report

        ticker_items = []
        custom_resolution_rejected = []
        if custom_enabled:
            validation_markets = markets
            try:
                loaded_validation_markets = await asyncio.to_thread(self.exchange.load_markets)
                if isinstance(loaded_validation_markets, dict):
                    validation_markets = loaded_validation_markets
            except Exception as exc:
                logger.warning(f"CoinSelector custom validation markets load failed: {exc}")
            ctrl = getattr(self, 'ctrl', None)
            mode = ctrl.get_exchange_mode() if ctrl and hasattr(ctrl, 'get_exchange_mode') else None
            for requested_symbol in custom_symbols:
                if active_strategy in UTBREAKOUT_STRATEGIES:
                    ok_market, normalized, invalid_reason = (
                        self._ensure_valid_utbreakout_market_symbol(
                            requested_symbol,
                            source='coin_selector_custom_universe',
                        )
                    )
                    if not ok_market:
                        reject_code = (
                            'REJECTED_REGION_RESTRICTED_SYMBOL'
                            if invalid_reason.startswith(
                                'REJECTED_REGION_RESTRICTED_SYMBOL:'
                            )
                            else 'REJECTED_INVALID_MARKET_SYMBOL'
                        )
                        custom_resolution_rejected.append({
                            'symbol': normalized,
                            'exchange_symbol': normalized,
                            'normalized_symbol': normalized,
                            'accepted': False,
                            'reject_reasons': [reject_code],
                            'analysis_error': invalid_reason,
                            'selection_state': 'REJECTED',
                            'custom_universe': True,
                        })
                        continue
                try:
                    if ctrl and hasattr(ctrl, '_resolve_futures_watch_symbol_from_markets'):
                        exchange_symbol = ctrl._resolve_futures_watch_symbol_from_markets(
                            requested_symbol,
                            validation_markets,
                            exchange_mode=mode,
                        )
                    else:
                        exchange_symbol = self._coin_selector_exchange_symbol_for_custom(requested_symbol, validation_markets)
                except Exception as exc:
                    normalized = normalize_coin_selector_custom_symbols([requested_symbol])
                    normalized = normalized[0] if normalized else str(requested_symbol or '').strip().upper()
                    custom_resolution_rejected.append({
                        'symbol': normalized,
                        'exchange_symbol': normalized,
                        'normalized_symbol': normalized,
                        'accepted': False,
                        'reject_reasons': ['REJECTED_EXCHANGE_MODE_SYMBOL'],
                        'analysis_error': str(exc),
                        'selection_state': 'REJECTED',
                        'custom_universe': True,
                    })
                    continue
                try:
                    ticker = await asyncio.to_thread(self.market_data_exchange.fetch_ticker, exchange_symbol)
                    ticker_items.append((exchange_symbol, ticker))
                except Exception as exc:
                    ticker_items.append((
                        exchange_symbol,
                        {
                            'coin_selector_fetch_error': str(exc),
                            'quoteVolume': 0.0,
                            'percentage': 0.0,
                            'count': 0,
                        }
                    ))
        else:
            tickers = await asyncio.to_thread(self.market_data_exchange.fetch_tickers)
            ticker_items = list((tickers or {}).items())

        accepted_base = []
        rejected = list(custom_resolution_rejected)
        include_tradifi_universe = self._coin_selector_should_include_tradifi_universe(cfg, custom_enabled)
        for symbol, ticker in ticker_items:
            if active_strategy in UTBREAKOUT_STRATEGIES:
                ok_market, canonical, invalid_reason = (
                    self._ensure_valid_utbreakout_market_symbol(
                        symbol,
                        source='coin_selector_candidate_filter',
                    )
                )
                if not ok_market:
                    reject_code = (
                        'REJECTED_REGION_RESTRICTED_SYMBOL'
                        if invalid_reason.startswith(
                            'REJECTED_REGION_RESTRICTED_SYMBOL:'
                        )
                        else 'REJECTED_INVALID_MARKET_SYMBOL'
                    )
                    rejected.append({
                        'symbol': canonical,
                        'exchange_symbol': canonical,
                        'normalized_symbol': canonical,
                        'accepted': False,
                        'reject_reasons': [reject_code],
                        'analysis_error': invalid_reason,
                        'selection_state': 'REJECTED',
                    })
                    continue
            market = self._coin_selector_market_for_symbol(symbol, markets)
            tags = coin_selector_sector_tags_for_symbol(symbol, cfg.get('sector_overrides'))
            candidate_cfg = cfg
            if custom_enabled and bool(cfg.get('custom_relax_discovery', True)):
                candidate_cfg = dict(cfg)
                candidate_cfg['min_trade_count'] = 0
            candidate = build_coin_selector_base_candidate(symbol, ticker, market, candidate_cfg, tags)
            if self._coin_selector_is_tradifi_market(symbol, market):
                candidate['tradifi_perpetual'] = True
                candidate['tradifi_regular_session'] = dict(tradifi_session_status)
                if not tradifi_session_open:
                    candidate['accepted'] = False
                    candidate['selection_state'] = 'REJECTED'
                    candidate.setdefault('reject_reasons', []).append(
                        'REJECTED_TRADFI_REGULAR_SESSION_CLOSED'
                    )
                    candidate['analysis_error'] = self._coin_selector_tradifi_closed_reject_reason(
                        tradifi_session_status
                    )
            if custom_enabled:
                candidate['custom_universe'] = True
                candidate['custom_discovery_relaxed'] = bool(cfg.get('custom_relax_discovery', True))
            if isinstance(ticker, dict) and ticker.get('coin_selector_fetch_error'):
                candidate['accepted'] = False
                candidate.setdefault('reject_reasons', []).append('REJECTED_TICKER_UNAVAILABLE')
                candidate['analysis_error'] = ticker.get('coin_selector_fetch_error')
            if candidate.get('accepted'):
                accepted_base.append(candidate)
            else:
                rejected.append(candidate)

        if not custom_enabled:
            accepted_base.sort(
                key=lambda item: float(item.get('quote_volume', 0.0) or 0.0),
                reverse=True
            )
        dispersion_values = [
            float(item.get('percentage', 0.0) or 0.0)
            for item in accepted_base
            if self._is_valid_number(item.get('percentage', 0.0))
        ]
        selector_context = {
            'cross_sectional_dispersion_pct': float(np.std(dispersion_values)) if len(dispersion_values) >= 2 else 0.0,
        }
        analysis_limit = len(accepted_base) if custom_enabled else int(cfg.get('analysis_limit', 20) or 20)
        analysis_universe = list(accepted_base[:analysis_limit])
        tradifi_candidates_considered = 0
        if include_tradifi_universe:
            tradifi_limit = max(1, int(cfg.get('tradifi_universe_max_candidates', 20) or 20))
            seen_symbols = {
                str(item.get('normalized_symbol') or item.get('exchange_symbol') or item.get('symbol') or '').upper()
                for item in analysis_universe
            }
            tradifi_candidates = [
                item for item in accepted_base
                if item.get('tradifi_perpetual')
            ]
            tradifi_candidates.sort(
                key=lambda item: float(item.get('quote_volume', 0.0) or 0.0),
                reverse=True
            )
            for item in tradifi_candidates[:tradifi_limit]:
                key = str(item.get('normalized_symbol') or item.get('exchange_symbol') or item.get('symbol') or '').upper()
                if key in seen_symbols:
                    continue
                analysis_universe.append(item)
                seen_symbols.add(key)
            tradifi_candidates_considered = len(tradifi_candidates[:tradifi_limit])
        analysis_candidates = self._rotating_coin_selector_batch(
            analysis_universe,
            cfg.get('analysis_batch_size', 12),
            cursor_attr='coin_selector_analysis_cursor',
        )
        scored = []
        analysis_errors = []
        rate_limited = False
        analysis_attempted = 0
        for candidate in analysis_candidates:
            analysis_attempted += 1
            scored_candidate = await self._score_coin_selector_candidate(
                candidate,
                cfg,
                strategy_params,
                selector_context=selector_context,
            )
            if scored_candidate.get('accepted'):
                scored.append(scored_candidate)
            else:
                analysis_errors.append(scored_candidate)
                if self._is_exchange_rate_limit_error(scored_candidate):
                    self._activate_coin_selector_rate_limit_backoff(
                        cfg,
                        scored_candidate.get('analysis_error') or scored_candidate,
                    )
                    rate_limited = True
                    break
        if rate_limited and analysis_attempted < len(analysis_candidates) and analysis_universe:
            deferred_count = len(analysis_candidates) - analysis_attempted
            self.coin_selector_analysis_cursor = (
                int(getattr(self, 'coin_selector_analysis_cursor', 0) or 0) - deferred_count
            ) % len(analysis_universe)

        top_n = int(cfg.get('top_n', 10) or 10)
        report = build_coin_selector_report(scored, rejected + analysis_errors, top_n=top_n)
        reject_samples = list((rejected + analysis_errors)[:10])
        report.update({
            'generated_at_ts': now,
            'generated_at': datetime.now(timezone.utc).isoformat(),
            'criteria': dict(cfg, custom_universe_enabled=custom_enabled, custom_symbols=custom_symbols),
            'analysis_limit': len(analysis_candidates),
            'analysis_universe_size': len(analysis_universe),
            'analysis_batch_size': len(analysis_candidates),
            'analysis_attempted': analysis_attempted,
            'total_base_candidates': len(accepted_base),
            'total_unanalyzed': max(0, len(analysis_universe) - analysis_attempted),
            'rate_limited': rate_limited,
            'custom_universe_enabled': custom_enabled,
            'custom_symbols': custom_symbols if custom_enabled else [],
            'tradifi_universe_included': include_tradifi_universe,
            'tradifi_regular_session_open': tradifi_session_open,
            'tradifi_regular_session': dict(tradifi_session_status),
            'tradifi_candidates_considered': tradifi_candidates_considered,
            'reject_samples': reject_samples,
        })
        selected = report.get('selected', [])
        selected_count = len(selected)
        for index, item in enumerate(selected):
            if not isinstance(item, dict):
                continue
            item['cross_sectional_rank'] = index + 1
            item['cross_sectional_rank_pct'] = round(
                100.0 * (selected_count - index) / max(selected_count, 1),
                2,
            )
        self.coin_selector_symbol_scores = {}
        score_items = list(selected) + list(report.get('watch_only') or [])
        for item in score_items:
            for key in (item.get('normalized_symbol'), item.get('exchange_symbol')):
                if key:
                    self.coin_selector_symbol_scores[key] = item
        self.coin_selector_last_result = report
        self.coin_selector_last_run_ts = now
        warning = report.get('concentration_warning')
        if warning:
            logger.warning(
                f"CoinSelector concentration warning: {warning.get('key')}={warning.get('value')} "
                f"{warning.get('share_pct')}%"
            )
        return report

    def get_coin_selector_symbol_summary(self, symbol):
        if not isinstance(self.coin_selector_symbol_scores, dict):
            return "CoinSelector: 아직 스캔 기록 없음"
        item = (
            self.coin_selector_symbol_scores.get(symbol)
            or self.coin_selector_symbol_scores.get(str(symbol or '').replace(':USDT', ''))
        )
        if not isinstance(item, dict):
            return "CoinSelector: 현재 Top 후보 아님"
        return (
            f"CoinSelector: {float(item.get('score', 0.0) or 0.0):.1f}점 / "
            f"Set{item.get('auto_set_id') or '?'} / TF {item.get('adaptive_tf') or 'n/a'} / "
            f"{item.get('selection_state', 'WATCH')}"
        )

    def _format_coin_selector_no_actionable_summary(self, report, limit=3):
        if not isinstance(report, dict):
            return "CoinSelector scanner: no actionable candidates (no report)."
        selected_count = len(report.get('selected') or [])
        watch_only = list(report.get('watch_only') or [])
        actionability = report.get('actionability_counts') or {}
        reason_counts = report.get('watch_only_reason_counts') or {}
        reason_text = ", ".join(
            f"{reason}={count}"
            for reason, count in sorted(
                reason_counts.items(),
                key=lambda item: item[1],
                reverse=True,
            )[:limit]
        ) or "n/a"
        top_parts = []
        for item in watch_only[:limit]:
            symbol = (
                item.get('normalized_symbol')
                or item.get('exchange_symbol')
                or item.get('symbol')
            )
            score = float(item.get('score', 0.0) or 0.0)
            state = item.get('selection_state') or 'WATCH_ONLY'
            ev_score = item.get('ev_score')
            ev_score_text = (
                f"{float(ev_score):.1f}"
                if self._is_valid_number(ev_score)
                else "n/a"
            )
            ev_reason = str(item.get('ev_reason') or '').replace('\n', ' ')
            if len(ev_reason) > 80:
                ev_reason = ev_reason[:77] + "..."
            top_parts.append(
                f"{symbol} score={score:.1f} state={state} "
                f"ev={ev_score_text} reason={ev_reason or 'n/a'}"
            )
        top_text = " | ".join(top_parts) or "none"
        return (
            "CoinSelector scanner: no actionable candidates "
            f"(selected={selected_count}, watch_only={len(watch_only)}, "
            f"states={actionability}, reasons={reason_text}; top={top_text})"
        )

    async def _scan_and_trade_coin_selector(self):
        cfg = self._get_coin_selector_config()
        backoff_remaining = self._coin_selector_rate_limit_backoff_remaining()
        if backoff_remaining > 0:
            logger.warning(
                "CoinSelector scan deferred for %.0fs after exchange rate limiting",
                backoff_remaining,
            )
            return
        scanner_strategy_params = self.get_runtime_strategy_params()
        scanner_active_strategy = str(
            scanner_strategy_params.get('active_strategy', 'utbot') or 'utbot'
        ).lower()
        scan_started_at = time.time()
        try:
            if self._micro_auto_enabled():
                report = await self.evaluate_micro_auto_candidates(force=False)
                candidates = list(report.get('selected', []))
            else:
                report = await self.evaluate_coin_selector(force=False)
                candidates = [
                    item for item in report.get('selected', [])
                    if item.get('scanner_accepted', True)
                ]
        except Exception as exc:
            if self._is_exchange_rate_limit_error(exc):
                self._activate_coin_selector_rate_limit_backoff(cfg, exc)
                return
            raise
        if scanner_active_strategy in UTBREAKOUT_STRATEGIES:
            allowed_candidates = []
            for item in candidates:
                candidate_symbol = (
                    item.get('exchange_symbol')
                    or item.get('normalized_symbol')
                    or item.get('symbol')
                )
                ok_market, canonical_symbol, invalid_reason = (
                    self._ensure_valid_utbreakout_market_symbol(
                        candidate_symbol,
                        source='coin_selector_selected_filter',
                    )
                )
                if not ok_market:
                    self._record_coin_selector_candidate_outcome(
                        canonical_symbol,
                        reason=invalid_reason,
                        cfg=cfg,
                        decision_key=(
                            "invalid_market:"
                            f"{self._utbreakout_trace_key(canonical_symbol)}"
                        ),
                    )
                    continue
                if item.get('exchange_symbol'):
                    item['exchange_symbol'] = canonical_symbol
                allowed_candidates.append(item)
            candidates = allowed_candidates
        min_quote_volume = max(
            COIN_SELECTOR_HARD_MIN_QUOTE_VOLUME_USDT,
            float(
                cfg.get(
                    'min_quote_volume_usdt',
                    COIN_SELECTOR_HARD_MIN_QUOTE_VOLUME_USDT,
                )
                or COIN_SELECTOR_HARD_MIN_QUOTE_VOLUME_USDT
            ),
        )
        volume_eligible_candidates = []
        for item in candidates:
            quote_volume = _safe_float_or_none(item.get('quote_volume'))
            if quote_volume is not None and quote_volume >= min_quote_volume:
                volume_eligible_candidates.append(item)
                continue
            candidate_symbol = (
                item.get('exchange_symbol')
                or item.get('normalized_symbol')
                or item.get('symbol')
                or 'UNKNOWN'
            )
            reason = (
                f"REJECTED_LOW_QUOTE_VOLUME: 24h quoteVolume "
                f"{float(quote_volume or 0.0):.2f} < "
                f"{min_quote_volume:.2f} USDT"
            )
            self._record_coin_selector_candidate_outcome(
                candidate_symbol,
                reason=reason,
                cfg=cfg,
                decision_key=(
                    f"low_quote_volume:{int(scan_started_at // 60)}"
                ),
            )
            self._utbreakout_trace_event(
                candidate_symbol,
                'COIN_SELECTED',
                'REJECTED_LOW_QUOTE_VOLUME',
                reason=reason,
                quote_volume=quote_volume,
                min_quote_volume=min_quote_volume,
            )
        candidates = volume_eligible_candidates
        candidates = self._rotating_coin_selector_batch(
            candidates,
            cfg.get('max_strategy_evaluations_per_cycle', 3),
            cursor_attr='coin_selector_strategy_cursor',
        )
        if not candidates:
            logger.info(self._format_coin_selector_no_actionable_summary(report))
            return

        log_lines = ["🧭 [CoinSelector] Top candidates:"]
        for idx, item in enumerate(candidates[:int(cfg.get('top_n', 10) or 10)], 1):
            micro_note = ""
            if item.get('micro_accepted'):
                decision = item.get('micro_auto') or {}
                micro_note = (
                    f", micro minNotional={float(item.get('micro_min_notional', 0) or 0):.2f}, "
                    f"{int(decision.get('leverage', 0) or 0)}x"
                )
            log_lines.append(
                f"  {idx}. {item.get('normalized_symbol')}: score={float(item.get('score', 0) or 0):.1f}, "
                f"vol={float(item.get('quote_volume', 0) or 0)/1_000_000:.1f}M, "
                f"Set{item.get('auto_set_id') or '?'}, TF={item.get('adaptive_tf')}{micro_note}"
            )
        logger.info("\n".join(log_lines))

        for target_coin in candidates:
            symbol = target_coin.get('exchange_symbol') or target_coin.get('normalized_symbol')
            symbol = self._canonicalize_utbreakout_symbol_for_use(
                symbol,
                source='coin_selector_scanner',
            )
            if scanner_active_strategy in UTBREAKOUT_STRATEGIES:
                ok_market, symbol, invalid_reason = (
                    self._ensure_valid_utbreakout_market_symbol(
                        symbol,
                        source='coin_selector_candidate_guard',
                    )
                )
                if not ok_market:
                    self._record_coin_selector_candidate_outcome(
                        symbol,
                        reason=invalid_reason,
                        cfg=cfg,
                        decision_key=(
                            "invalid_market:"
                            f"{self._utbreakout_trace_key(symbol)}"
                        ),
                    )
                    continue
            self._utbreakout_trace_event(
                symbol,
                'COIN_SELECTED',
                'SELECTED',
                score=target_coin.get('score'),
                set_name=target_coin.get('auto_set_name') or target_coin.get('auto_set_id'),
                timeframe=target_coin.get('adaptive_tf'),
                reason=target_coin.get('auto_selection_reason') or target_coin.get('adaptive_reason'),
            )
            cooldown_remaining, cooldown_state = self._coin_selector_cooldown_remaining(
                symbol,
                cfg,
                now=scan_started_at
            )
            target_coin["cooldown_remaining"] = cooldown_remaining
            target_coin.setdefault("scanner_warnings", []).append(f"cooldown remaining {cooldown_remaining:.0f}s")
            if cooldown_remaining > 0:
                cooldown_reason = ''
                if isinstance(cooldown_state, dict):
                    cooldown_reason = cooldown_state.get('last_reason') or ''
                logger.info(
                    f"CoinSelector cooldown noted for {symbol}: "
                    f"{cooldown_remaining / 60.0:.1f}m remaining ({cooldown_reason}) - not blocked"
                )
            logger.info(
                f"CoinSelector evaluating {symbol}: score={float(target_coin.get('score', 0) or 0):.1f}, "
                f"Set{target_coin.get('auto_set_id')}, TF={target_coin.get('adaptive_tf')}"
            )
            if self.ctrl.is_paused:
                return
            try:
                trade_cfg = self.get_runtime_trade_config()
                common_cfg = self.get_runtime_common_settings()
                scan_tf = common_cfg.get('scanner_timeframe', '15m')
                strategy_params = trade_cfg.get('strategy_params', {})
                scan_params = strategy_params.copy()
                active_strategy = scan_params.get('active_strategy', 'utbot').lower()
                if active_strategy not in CORE_STRATEGIES:
                    active_strategy = 'utbot'
                if active_strategy in UTBREAKOUT_STRATEGIES:
                    scan_tf = self._get_utbot_filtered_breakout_config(scan_params).get('entry_timeframe', '15m')
                    if (
                        active_strategy != ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND
                        and target_coin.get('adaptive_tf')
                        and target_coin.get('adaptive_tf') != 'NO_TRADE'
                    ):
                        scan_tf = target_coin.get('adaptive_tf')
                    self._utbreakout_trace_event(
                        symbol,
                        'SCANNER_SEEN',
                        'EVALUATING',
                        active_strategy=active_strategy,
                        scan_tf=scan_tf,
                        score=target_coin.get('score'),
                        selected_set=target_coin.get('auto_set_id'),
                    )

                ohlcv = await asyncio.to_thread(self.market_data_exchange.fetch_ohlcv, symbol, scan_tf, limit=300)
                if not ohlcv:
                    self._record_coin_selector_candidate_outcome(
                        symbol,
                        reason=f"{active_strategy}: no OHLCV on {scan_tf}",
                        cfg=cfg,
                        decision_key=f"no_ohlcv:{scan_tf}:{int(scan_started_at // 60)}"
                    )
                    continue
                df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                decision_key = f"{active_strategy}:{scan_tf}:{int(ohlcv[-1][0])}"
                strategy_context = self._collect_primary_strategy_context(symbol, df, scan_params, active_strategy)
                sig, _, _, _, _, _ = await self._calculate_strategy_signal(
                    symbol,
                    df,
                    scan_params,
                    active_strategy,
                    allow_utbot_stateful=False,
                    precomputed=strategy_context.get('precomputed'),
                    force_utbreakout_reprocess=(
                        active_strategy in UTBREAKOUT_STRATEGIES
                    ),
                )
                if active_strategy in UTBREAKOUT_STRATEGIES:
                    signal_diag = self._utbreakout_diag_for_symbol(symbol)
                    last_entry_reason = (
                        self.last_entry_reason.get(symbol)
                        if isinstance(getattr(self, 'last_entry_reason', None), dict)
                        else None
                    )
                    if self._is_exchange_rate_limit_error((
                        signal_diag,
                        last_entry_reason,
                    )):
                        self._activate_coin_selector_rate_limit_backoff(
                            cfg,
                            signal_diag.get('reason') or last_entry_reason,
                        )
                        break
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
                        decision_candle_ts=signal_diag.get(
                            'decision_candle_ts'
                        ),
                        source='coin_selector_scanner',
                        force=True,
                    )
                    self._record_utbreakout_live_ready_from_diag(
                        symbol,
                        source='scanner_seen',
                        scan_tf=scan_tf,
                    )
                    bridge_called = (
                        await self._maybe_run_utbreakout_auto_entry_bridge(
                            symbol,
                            source='scanner_seen',
                        )
                    )
                    if bridge_called:
                        bridge_pos = await self.get_server_position(
                            symbol,
                            use_cache=False,
                        )
                        if bridge_pos:
                            self._record_coin_selector_candidate_outcome(
                                symbol,
                                accepted=True,
                                cfg=cfg,
                            )
                            self.scanner_active_symbol = symbol
                            self._utbreakout_trace_event(
                                symbol,
                                'POSITION_CONFIRMED',
                                'OK',
                                side=bridge_pos.get('side'),
                                contracts=bridge_pos.get('contracts'),
                                source='auto_entry_bridge_post_check',
                            )
                            break
                        entry_reason = (
                            self.last_entry_reason.get(symbol)
                            if isinstance(
                                getattr(self, 'last_entry_reason', None),
                                dict,
                            )
                            else ''
                        )
                        self._record_coin_selector_candidate_outcome(
                            symbol,
                            reason=(
                                entry_reason
                                or 'auto entry bridge did not open position'
                            ),
                            cfg=cfg,
                            decision_key=(
                                f"bridge_no_position:{int(scan_started_at // 60)}"
                            ),
                        )
                        self._utbreakout_trace_event(
                            symbol,
                            'NO_POSITION_AFTER_ENTRY',
                            'WARN',
                            reason=entry_reason,
                            source='auto_entry_bridge_post_check',
                        )
                        continue
                    if active_strategy == ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND:
                        diag = self._utbreakout_diag_for_symbol(symbol)
                        if (
                            isinstance(diag, dict)
                            and str(diag.get('stage') or '').lower() == 'waiting'
                            and not diag.get('reject_code')
                        ):
                            self._utbreakout_trace_event(
                                symbol,
                                'RSPT_WAITING',
                                'NO_CANDIDATE_COOLDOWN',
                                reason=diag.get('reason') or self.last_entry_reason.get(symbol) or '',
                                source='coin_selector_scanner',
                            )
                            continue
                    self._record_coin_selector_candidate_outcome(
                        symbol,
                        reason='auto entry bridge blocked before direct entry',
                        cfg=cfg,
                        decision_key=(
                            f"bridge_blocked:{int(scan_started_at // 60)}"
                        ),
                    )
                    continue

                if not sig and active_strategy in UTBREAKOUT_STRATEGIES:
                    diag = self._utbreakout_diag_for_symbol(symbol)
                    accepted_side = str((diag or {}).get('accepted_side') or '').lower()
                    if accepted_side in {'long', 'short'}:
                        sig = accepted_side
                        logger.warning(
                            "CoinSelector recovered UTBreakout accepted side from diagnostic: %s %s",
                            symbol,
                            sig,
                        )

                if not sig:
                    last_reason = ''
                    if isinstance(getattr(self, 'last_entry_reason', None), dict):
                        last_reason = self.last_entry_reason.get(symbol) or ''
                    self._record_coin_selector_candidate_outcome(
                        symbol,
                        reason=last_reason or f"{active_strategy}: no entry signal on {scan_tf}",
                        cfg=cfg,
                        decision_key=decision_key
                    )
                    continue
                if active_strategy == 'utbot':
                    utbot_entry_filter_eval = await self._evaluate_utbot_filter_pack(
                        symbol,
                        df,
                        scan_params,
                        scan_tf,
                        'entry'
                    )
                    allowed, filter_reason = self._utbot_filter_pack_allows_entry(utbot_entry_filter_eval, sig)
                    if not allowed:
                        logger.info(f"CoinSelector skip {symbol}: UTBot filter pack blocked ({filter_reason})")
                        self._record_coin_selector_candidate_outcome(
                            symbol,
                            reason=f"utbot filter blocked: {filter_reason}",
                            cfg=cfg,
                            decision_key=decision_key
                        )
                        continue
                    utbot_rsi_momentum_entry_eval = self._evaluate_utbot_rsi_momentum_filter(df, scan_params, 'entry')
                    allowed, filter_reason = self._utbot_rsi_momentum_filter_allows(utbot_rsi_momentum_entry_eval, sig)
                    if not allowed:
                        logger.info(f"CoinSelector skip {symbol}: RSI Momentum filter blocked ({filter_reason})")
                        self._record_coin_selector_candidate_outcome(
                            symbol,
                            reason=f"rsi momentum filter blocked: {filter_reason}",
                            cfg=cfg,
                            decision_key=decision_key
                        )
                        continue
                elif active_strategy == 'utsmc':
                    utsmc_detail = strategy_context.get('raw_hybrid_detail', {}) or {}
                    allowed, candidate_reason = self._utsmc_candidate_filter_allows(utsmc_detail, sig, is_persistent=False)
                    if not allowed:
                        logger.info(f"CoinSelector skip {symbol}: UTSMC candidate filter blocked ({candidate_reason})")
                        self._record_coin_selector_candidate_outcome(
                            symbol,
                            reason=f"utsmc candidate filter blocked: {candidate_reason}",
                            cfg=cfg,
                            decision_key=decision_key
                        )
                        continue

                pos = await self.get_server_position(symbol, use_cache=False)
                if not pos:
                    logger.info(f"CoinSelector locking in: {symbol} [{sig.upper()}]")
                    current_price = float(ohlcv[-1][4])
                    if active_strategy in UTBREAKOUT_STRATEGIES:
                        self._utbreakout_trace_event(
                            symbol,
                            'ENTRY_CALL',
                            'CALLING',
                            side=sig,
                            entry_ref_price=current_price,
                            source='coin_selector_scanner',
                        )
                    await self.entry(symbol, sig, current_price)
                    post_pos = await self.get_server_position(symbol, use_cache=False)
                    if not post_pos:
                        entry_reason = ''
                        if isinstance(getattr(self, 'last_entry_reason', None), dict):
                            entry_reason = self.last_entry_reason.get(symbol) or ''
                        self._record_coin_selector_candidate_outcome(
                            symbol,
                            reason=entry_reason or "entry call did not open position",
                            cfg=cfg,
                            decision_key=decision_key
                        )
                        logger.warning(
                            "CoinSelector scanner entry call did not open position: %s %s reason=%s",
                            symbol,
                            sig,
                            entry_reason,
                        )
                        self._utbreakout_trace_event(
                            symbol,
                            'NO_POSITION_AFTER_ENTRY',
                            'WARN',
                            side=sig,
                            reason=entry_reason,
                            source='scanner_post_entry_check',
                        )
                        try:
                            await self.ctrl.notify(
                                "⚠️ UTBreakout 조건 통과 후 포지션 미생성\n"
                                f"symbol={symbol}\n"
                                f"side={sig.upper()}\n"
                                f"reason={entry_reason or 'unknown'}\n"
                                "자세한 분석: /utbreak tracefull"
                            )
                        except Exception:
                            logger.debug("entry failure notify skipped", exc_info=True)
                        continue
                    self._record_coin_selector_candidate_outcome(symbol, accepted=True, cfg=cfg)
                    self.scanner_active_symbol = symbol
                    current_ts = int(ohlcv[-1][0])
                    self.last_processed_candle_ts[symbol] = current_ts
                    self.last_candle_time[symbol] = current_ts
                    self.last_candle_success[symbol] = True
                    self._utbreakout_trace_event(
                        symbol,
                        'POSITION_CONFIRMED',
                        'OK',
                        side=post_pos.get('side'),
                        contracts=post_pos.get('contracts'),
                        source='scanner_post_entry_check',
                    )
                    break
                self._record_coin_selector_candidate_outcome(symbol, accepted=True, cfg=cfg)
                logger.info(f"CoinSelector checked {symbol}: position exists ({pos.get('side')})")
            except Exception as exc:
                if self._is_exchange_rate_limit_error(exc):
                    self._activate_coin_selector_rate_limit_backoff(cfg, exc)
                    break
                self._record_coin_selector_candidate_outcome(
                    symbol,
                    reason=f"strategy check error: {type(exc).__name__}",
                    cfg=cfg,
                    decision_key=f"strategy_error:{int(time.time() // 60)}"
                )
                logger.error(f"CoinSelector strategy check failed for {symbol}: {exc}")
                continue

    async def scan_and_trade_high_volume(self):
        """[New] High Volume Scanner Logic (Refined)
        Rule: 200M+ Vol -> Top 5 Risers -> Select Max Vol from Top 5 -> Cross Strategy
        Serial Mode: Finds ONE target -> Enters -> Sets self.scanner_active_symbol
        """
        try:
            if self.is_upbit_mode():
                logger.info("Scanner skipped: Upbit mode uses dedicated KRW UTBOT watchlist only.")
                return

            scan_started_at = time.time()
            coin_selector_cfg = self._get_coin_selector_config()
            if bool(coin_selector_cfg.get('enabled', True)):
                await self._scan_and_trade_coin_selector()
                return

            logger.info("?뱻 Scanning high volume markets (>200M USDT)...")
            tickers = await asyncio.to_thread(self.market_data_exchange.fetch_tickers)

            # 1. 1李??꾪꽣: 嫄곕옒湲덉븸 200M ?댁긽
            candidates = []
            for symbol, data in tickers.items():
                # [Fix] 諛붿씠?몄뒪 ?좊Ъ ?щ낵留??꾪꽣 (?ㅽ뙚 ?쒖쇅: BTC/USDT ??X, BTC/USDT:USDT ??O)
                if '/USDT:USDT' in symbol:
                    quote_vol = float(data.get('quoteVolume', 0) or 0)
                    percentage = float(data.get('percentage', 0) or 0)
                    if quote_vol >= 200_000_000:
                        candidates.append({'symbol': symbol, 'vol': quote_vol, 'pct': percentage})

            if not candidates:
                logger.info("scanner: No coins > 200M vol found.")
                return

            # 2. 2李??꾪꽣: ?곸듅瑜??곸쐞 5媛?
            candidates.sort(key=lambda x: x['pct'], reverse=True)
            top_5_risers = candidates[:5]

            # Debug Log: Top 5 Risers
            log_msg = "?뱤 [Scanner Debug] Top 5 Risers:\n"
            for idx, c in enumerate(top_5_risers):
                log_msg += f"  {idx+1}. {c['symbol']}: Rise={c['pct']:.2f}%, Vol={c['vol']/1_000_000:.1f}M\n"
            logger.info(log_msg.strip())

            if not top_5_risers:
                 logger.info("scanner: No candidates after filtering.")
                 return

            # 3. 3李??꾪꽣: 洹?以?嫄곕옒?湲??쒖쑝濡??뺣젹?섏뿬 ?쒖감?곸쑝濡?泥댄겕
            top_5_risers.sort(key=lambda x: x['vol'], reverse=True)

            for target_coin in top_5_risers:
                symbol = target_coin['symbol']
                cooldown_remaining, cooldown_state = self._coin_selector_cooldown_remaining(
                    symbol,
                    coin_selector_cfg,
                    now=scan_started_at
                )
                if cooldown_remaining > 0:
                    cooldown_reason = ''
                    if isinstance(cooldown_state, dict):
                        cooldown_reason = cooldown_state.get('last_reason') or ''
                    logger.info(
                        f"Scanner Skip {symbol}: candidate cooldown "
                        f"{cooldown_remaining / 60.0:.1f}m remaining ({cooldown_reason})"
                    )
                    continue
                logger.info(f"?렞 Scanner Evaluating: {symbol} (Vol: {target_coin['vol']/1_000_000:.1f}M, Rise: {target_coin['pct']:.2f}%)")

                # 4. ?꾨왂 ?ㅽ뻾
                if self.ctrl.is_paused: return

                try:
                    cfg = self.get_runtime_trade_config()
                    common_cfg = self.get_runtime_common_settings()
                    scan_tf = common_cfg.get('scanner_timeframe', '15m')
                    min_rise_pct = float(common_cfg.get('scanner_min_rise_pct', 0.5) or 0.0)
                    max_rise_pct = float(common_cfg.get('scanner_max_rise_pct', 8.0) or 8.0)
                    strategy_params = cfg.get('strategy_params', {})

                    rise_pct = float(target_coin.get('pct', 0) or 0)
                    if rise_pct < min_rise_pct:
                        logger.info(f"?? Scanner Skip {symbol}: rise too weak ({rise_pct:.2f}% < {min_rise_pct:.2f}%)")
                        self._record_coin_selector_candidate_outcome(
                            symbol,
                            reason=f"rise too weak: {rise_pct:.2f}% < {min_rise_pct:.2f}%",
                            cfg=coin_selector_cfg,
                            decision_key=f"rise_min:{int(scan_started_at // 300)}"
                        )
                        continue
                    if rise_pct > max_rise_pct:
                        logger.info(f"?? Scanner Skip {symbol}: rise too extended ({rise_pct:.2f}% > {max_rise_pct:.2f}%)")
                        self._record_coin_selector_candidate_outcome(
                            symbol,
                            reason=f"rise too extended: {rise_pct:.2f}% > {max_rise_pct:.2f}%",
                            cfg=coin_selector_cfg,
                            decision_key=f"rise_max:{int(scan_started_at // 300)}"
                        )
                        continue

                    # [MODIFIED] Always use entry_timeframe if scanner is evaluating for a position-like entry
                    scan_params = strategy_params.copy()
                    active_strategy = scan_params.get('active_strategy', 'utbot').lower()
                    if active_strategy not in CORE_STRATEGIES:
                        active_strategy = 'utbot'
                    if active_strategy in UTBREAKOUT_STRATEGIES:
                        scan_tf = self._get_utbot_filtered_breakout_config(scan_params).get('entry_timeframe', '15m')

                    # Use scanner_timeframe if set, but ensure we are thinking about consistency
                    ohlcv = await asyncio.to_thread(self.market_data_exchange.fetch_ohlcv, symbol, scan_tf, limit=300)
                    if not ohlcv:
                        self._record_coin_selector_candidate_outcome(
                            symbol,
                            reason=f"{active_strategy}: no OHLCV on {scan_tf}",
                            cfg=coin_selector_cfg,
                            decision_key=f"no_ohlcv:{scan_tf}:{int(scan_started_at // 60)}"
                        )
                        continue

                    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    decision_key = f"{active_strategy}:{scan_tf}:{int(ohlcv[-1][0])}"

                    strategy_context = self._collect_primary_strategy_context(symbol, df, scan_params, active_strategy)
                    sig, _, _, _, _, _ = await self._calculate_strategy_signal(
                        symbol, df, scan_params, active_strategy, allow_utbot_stateful=False,
                        precomputed=strategy_context.get('precomputed')
                    )

                    if not sig:
                        last_reason = ''
                        if isinstance(getattr(self, 'last_entry_reason', None), dict):
                            last_reason = self.last_entry_reason.get(symbol) or ''
                        self._record_coin_selector_candidate_outcome(
                            symbol,
                            reason=last_reason or f"{active_strategy}: no entry signal on {scan_tf}",
                            cfg=coin_selector_cfg,
                            decision_key=decision_key
                        )
                        continue

                    if sig:
                        if active_strategy == 'utbot':
                            utbot_entry_filter_eval = await self._evaluate_utbot_filter_pack(
                                symbol,
                                df,
                                scan_params,
                                scan_tf,
                                'entry'
                            )
                            allowed, filter_reason = self._utbot_filter_pack_allows_entry(utbot_entry_filter_eval, sig)
                            if not allowed:
                                logger.info(
                                    f"🚫 Scanner Skip {symbol}: UTBot filter pack blocked "
                                    f"({filter_reason})"
                                )
                                self._record_coin_selector_candidate_outcome(
                                    symbol,
                                    reason=f"utbot filter blocked: {filter_reason}",
                                    cfg=coin_selector_cfg,
                                    decision_key=decision_key
                                )
                                continue
                            utbot_rsi_momentum_entry_eval = self._evaluate_utbot_rsi_momentum_filter(
                                df,
                                scan_params,
                                'entry'
                            )
                            allowed, filter_reason = self._utbot_rsi_momentum_filter_allows(
                                utbot_rsi_momentum_entry_eval,
                                sig
                            )
                            if not allowed:
                                logger.info(
                                    f"🚫 Scanner Skip {symbol}: RSI Momentum Trend filter blocked "
                                    f"({filter_reason})"
                                )
                                self._record_coin_selector_candidate_outcome(
                                    symbol,
                                    reason=f"rsi momentum filter blocked: {filter_reason}",
                                    cfg=coin_selector_cfg,
                                    decision_key=decision_key
                                )
                                continue
                        elif active_strategy == 'utsmc':
                            utsmc_detail = strategy_context.get('raw_hybrid_detail', {}) or {}
                            allowed, candidate_reason = self._utsmc_candidate_filter_allows(utsmc_detail, sig, is_persistent=False)
                            if not allowed:
                                logger.info(
                                    f"?? Scanner Skip {symbol}: UTSMC candidate filter blocked "
                                    f"({utsmc_detail.get('candidate_filter_mode_label', 'OFF')} | {candidate_reason})"
                                )
                                self._record_coin_selector_candidate_outcome(
                                    symbol,
                                    reason=f"utsmc candidate filter blocked: {candidate_reason}",
                                    cfg=coin_selector_cfg,
                                    decision_key=decision_key
                                )
                                continue
                        # ?ъ????뺤씤 (?쒕쾭)
                        pos = await self.get_server_position(symbol, use_cache=False)

                        if not pos:
                            logger.info(f"?? Scanner Locking In: {symbol} [{sig.upper()}] detected!")
                            current_price = float(ohlcv[-1][4])
                            await self.entry(symbol, sig, current_price)
                            post_pos = await self.get_server_position(symbol, use_cache=False)
                            if not post_pos:
                                entry_reason = ''
                                if isinstance(getattr(self, 'last_entry_reason', None), dict):
                                    entry_reason = self.last_entry_reason.get(symbol) or ''
                                self._record_coin_selector_candidate_outcome(
                                    symbol,
                                    reason=entry_reason or "entry call did not open position",
                                    cfg=coin_selector_cfg,
                                    decision_key=decision_key
                                )
                                logger.info(f"Scanner did not open {symbol}; continuing candidates.")
                                continue
                            self._record_coin_selector_candidate_outcome(symbol, accepted=True, cfg=coin_selector_cfg)
                            self.scanner_active_symbol = symbol
                            current_ts = int(ohlcv[-1][0])
                            self.last_processed_candle_ts[symbol] = current_ts
                            self.last_candle_time[symbol] = current_ts
                            self.last_candle_success[symbol] = True
                            break # Found a winner, exit loop
                        else:
                            self._record_coin_selector_candidate_outcome(symbol, accepted=True, cfg=coin_selector_cfg)
                            logger.info(f"?? Scanner Checked {symbol}: Position exists ({pos['side']})")

                except Exception as e:
                    self._record_coin_selector_candidate_outcome(
                        symbol,
                        reason=f"strategy check error: {type(e).__name__}",
                        cfg=coin_selector_cfg,
                        decision_key=f"strategy_error:{int(time.time() // 60)}"
                    )
                    logger.error(f"Scanner strategy check failed for {symbol}: {e}")
                    continue

        except Exception as e:
            logger.error(f"Volume scanner error: {e}")

    def _timeframe_to_ms(self, tf):
        """??꾪봽?덉엫??諛由ъ큹濡?蹂??"""
        multipliers = {
            'm': 60 * 1000,
            'h': 60 * 60 * 1000,
            'd': 24 * 60 * 60 * 1000,
            'w': 7 * 24 * 60 * 60 * 1000
        }
        try:
            tf = str(tf or '').strip().lower()
            if not tf:
                return 0
            if tf.isdigit():
                return int(tf) * multipliers['m']
            unit = tf[-1]
            value = int(tf[:-1])
            return value * multipliers.get(unit, 0)
        except (TypeError, ValueError, IndexError):
            return 0

    def _timeframes_equivalent(self, tf_a, tf_b):
        tf_a = str(tf_a or '').strip().lower()
        tf_b = str(tf_b or '').strip().lower()
        if not tf_a or not tf_b:
            return False
        ms_a = self._timeframe_to_ms(tf_a)
        ms_b = self._timeframe_to_ms(tf_b)
        if ms_a > 0 and ms_b > 0:
            return ms_a == ms_b
        return tf_a == tf_b
