"""Configuration, exchange, market-data, and startup orchestration."""

from __future__ import annotations

from utbreakout.coinselector import market_is_blocked_tradifi_commodity


class ControllerExchangeMixin:
    def _telegram_reporting_cfg(self):
        return self.cfg.get('telegram', {}).get('reporting', {}) or {}

    def _telegram_event_alerts_only(self):
        return bool(self._telegram_reporting_cfg().get('event_alerts_only', True))

    def _startup_notice_enabled(self):
        reporting = self._telegram_reporting_cfg()
        return (not self._telegram_event_alerts_only()) and bool(reporting.get('startup_notice_enabled', False))

    def _startup_keyboard_enabled(self):
        reporting = self._telegram_reporting_cfg()
        return (not self._telegram_event_alerts_only()) and bool(reporting.get('startup_keyboard_enabled', False))

    def _should_suppress_telegram_notice(self, text):
        if not self._telegram_event_alerts_only():
            return False

        body = str(text or '').strip()
        if not body:
            return True

        quiet_prefixes = (
            "🧪 UT 진단",
            "⏱ **시간별 리포트**",
            "⏱ 시간별 리포트",
            "▶ **봇 시작됨",
            "⏸ **봇 시작됨",
            "📱 메인 메뉴",
            "🚦 UT Breakout 조건 스테이터스",
            "UT Breakout Research Summary",
        )
        if body.startswith(quiet_prefixes):
            return True
        if "대시보드" in body and body.startswith(("🟢", "🔴", "🟡", "⚪", "**[")):
            return True

        noisy_markers = (
            "진입 차단",
            "잔고 부족",
            "최소 주문금액 미달",
            "최소 금액 미달",
            "최소금액 미달",
            "증거금 부족",
            "리스크 상한",
            "수량 정밀도",
            "진입 계획이 없어",
            "리스크 계획 누락",
            "주문 수량 계산 오류",
            "포지션 없음",
            "청산할 오픈 포지션이 없습니다",
            "청산할 업비트 보유 코인이 없습니다",
            "최소 주문금액 반영",
        )
        if any(marker in body for marker in noisy_markers):
            return True

        entry_markers = (
            "[Signal Entry]",
            "TEMA 진입",
            "LONG 진입",
            "SHORT 진입",
            "매수 성공",
            "매도 성공",
        )
        exit_markers = (
            "[Signal Exit]",
            "TEMA 청산",
            "청산 완료",
            "청산 성공",
            "강제 청산",
            "청산 실패",
            "청산 재시도",
            "긴급 정지",
            "익절",
            "손절",
            "PnL",
        )
        protection_markers = (
            "TP:",
            "SL:",
            "TP/SL",
            "TP 주문",
            "SL 주문",
            "보호",
        )
        risk_markers = (
            "일일 손실",
            "MMR",
        )
        error_markers = (
            "진입 실패",
            "실패",
            "오류",
        )
        event_markers = entry_markers + exit_markers + protection_markers + risk_markers + error_markers
        return not any(marker in body for marker in event_markers)

    def get_exchange_mode(self):
        api_cfg = self.cfg.get('api', {})
        mode = str(api_cfg.get('exchange_mode', '')).lower()
        if mode not in SUPPORTED_EXCHANGE_MODES:
            mode = BINANCE_TESTNET if api_cfg.get('use_testnet', True) else BINANCE_MAINNET
        return mode

    def is_upbit_mode(self):
        return self.get_exchange_mode() == UPBIT_MODE

    def get_effective_trade_direction(self):
        if self.is_upbit_mode():
            return 'long'
        return self.cfg.get('system_settings', {}).get('trade_direction', 'both')

    def get_exchange_mode_label(self, exchange_mode=None):
        mode = exchange_mode or self.get_exchange_mode()
        labels = {
            BINANCE_TESTNET: 'binance testnet',
            BINANCE_MAINNET: 'binance mainnet',
            UPBIT_MODE: 'upbit krw spot'
        }
        return labels.get(mode, mode)

    def get_network_status_label(self, exchange_mode=None):
        mode = exchange_mode or self.get_exchange_mode()
        labels = {
            BINANCE_TESTNET: '테스트넷(데모) 🧪',
            BINANCE_MAINNET: '메인넷 💰',
            UPBIT_MODE: '업비트 KRW 현물'
        }
        return labels.get(mode, mode)

    def get_exchange_display_name(self, exchange_mode=None):
        mode = exchange_mode or self.get_exchange_mode()
        names = {
            BINANCE_TESTNET: 'BINANCE FUTURES',
            BINANCE_MAINNET: 'BINANCE FUTURES',
            UPBIT_MODE: 'UPBIT SPOT'
        }
        return names.get(mode, mode.upper())

    def _get_market_data_exchange_mode(self, exchange_mode=None):
        mode = exchange_mode or self.get_exchange_mode()
        if mode == BINANCE_TESTNET:
            return BINANCE_MAINNET
        return mode

    def _get_market_data_source_label(self, exchange_mode=None):
        mode = self._get_market_data_exchange_mode(exchange_mode)
        labels = {
            BINANCE_MAINNET: 'BINANCE FUTURES MAINNET PUBLIC',
            UPBIT_MODE: 'UPBIT KRW SPOT PUBLIC'
        }
        return labels.get(mode, mode.upper())

    def _get_exchange_credentials(self, exchange_mode=None):
        mode = exchange_mode or self.get_exchange_mode()
        api = self.cfg.get('api', {})
        if mode == UPBIT_MODE:
            return api.get('upbit', {})
        if mode == BINANCE_TESTNET:
            return api.get('testnet', {})
        return api.get('mainnet', {})

    def get_active_trade_section(self):
        return 'upbit' if self.is_upbit_mode() else 'signal_engine'

    def get_active_trade_config(self):
        return self.cfg.get(self.get_active_trade_section(), {})

    def get_active_common_settings(self):
        return self.get_active_trade_config().get('common_settings', {})

    def get_active_strategy_params(self):
        strategy = dict(self.get_active_trade_config().get('strategy_params', {}))
        if self.is_upbit_mode():
            strategy['active_strategy'] = 'utbot'
            strategy['entry_mode'] = 'position'
        elif str(strategy.get('active_strategy', 'utbot')).lower() not in CORE_STRATEGIES:
            strategy['active_strategy'] = 'utbot'
        return strategy

    def _config_root(self):
        if isinstance(getattr(self.cfg, 'config', None), dict):
            return self.cfg.config
        if isinstance(getattr(self.cfg, 'values', None), dict):
            return self.cfg.values
        if isinstance(self.cfg, dict):
            return self.cfg
        return None

    async def _update_config_value(self, path, value):
        if hasattr(self.cfg, 'update_value'):
            await self.cfg.update_value(path, value)
            return
        root = self._config_root()
        if not isinstance(root, dict):
            raise RuntimeError("Config object does not support updates")
        node = root
        for key in path[:-1]:
            node = node.setdefault(key, {})
        node[path[-1]] = value

    def get_default_watchlist_for_exchange_mode(self, exchange_mode=None):
        mode = exchange_mode or self.get_exchange_mode()
        return list(EXCHANGE_MODE_DEFAULT_WATCHLISTS.get(mode, ["BTC/USDT"]))

    def get_active_watchlist(self):
        mode = self.get_exchange_mode()
        watchlists = self.cfg.get('exchange_watchlists', {}) or {}
        mode_watchlist = watchlists.get(mode) if isinstance(watchlists, dict) else None
        if isinstance(mode_watchlist, list) and mode_watchlist:
            return list(mode_watchlist)

        if mode == UPBIT_MODE:
            legacy = self.cfg.get('upbit', {}).get('watchlist', [])
        else:
            legacy = self.cfg.get('signal_engine', {}).get('watchlist', [])
        if isinstance(legacy, list) and legacy:
            return list(legacy)

        return self.get_default_watchlist_for_exchange_mode(mode)

    def _reset_signal_engine_runtime_state(self, *, reset_entry_cache=False, reset_exit_cache=False, reset_stateful_strategy=False):
        signal_engine = self.engines.get('signal')
        if not signal_engine:
            return None

        signal_engine.reset_signal_runtime_state(
            reset_entry_cache=reset_entry_cache,
            reset_exit_cache=reset_exit_cache,
            reset_stateful_strategy=reset_stateful_strategy
        )
        return signal_engine

    async def _return_signal_engine_to_utbot(self):
        await self.cfg.update_value(['signal_engine', 'strategy_params', 'active_strategy'], 'utbot')
        await self.cfg.update_value(
            ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'adaptive_timeframe_enabled'],
            False
        )
        await self.cfg.update_value(
            ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'auto_select_enabled'],
            False
        )
        await self.cfg.update_value(
            ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'selection_mode'],
            'manual'
        )
        await self.cfg.update_value(['signal_engine', 'coin_selector', 'enabled'], False)
        await self.cfg.update_value(['signal_engine', 'coin_selector', 'custom_universe_enabled'], False)
        await self.cfg.update_value(['signal_engine', 'common_settings', 'scanner_enabled'], False)
        await self.cfg.update_value(['signal_engine', 'micro_auto', 'enabled'], False)
        signal_engine = self._reset_signal_engine_runtime_state(
            reset_entry_cache=True,
            reset_exit_cache=True,
            reset_stateful_strategy=True
        )
        if signal_engine:
            signal_engine.scanner_active_symbol = None
        return signal_engine

    def normalize_symbol_for_exchange(self, raw_symbol, exchange_mode=None):
        mode = exchange_mode or self.get_exchange_mode()
        text = str(raw_symbol or '').strip().upper()
        if not text:
            raise ValueError("빈 심볼은 사용할 수 없습니다.")

        if mode == UPBIT_MODE:
            text = text.replace(' ', '')
            if text.startswith('KRW-'):
                base = text.split('-', 1)[1]
            elif '/' in text:
                left, right = text.split('/', 1)
                if left == 'KRW':
                    base = right
                elif right == 'KRW':
                    base = left
                else:
                    raise ValueError("업비트는 KRW 마켓만 지원합니다.")
            else:
                base = text
            if not re.fullmatch(r'[A-Z0-9]{2,15}', base or ''):
                raise ValueError("업비트 코인은 영문/숫자 심볼로 입력하세요. 예: BTC, XRP, KRW-BTC")
            return f"{base}/KRW"

        if '/' in text:
            return text
        return f"{text}/USDT"

    def _futures_market_for_symbol(self, symbol, markets):
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

    def _upbit_market_for_symbol(self, symbol, markets):
        if not isinstance(markets, dict):
            return None
        normalized = self.normalize_symbol_for_exchange(symbol, exchange_mode=UPBIT_MODE)
        base = normalized.split('/', 1)[0]
        keys = []

        def _add_key(key):
            key = str(key or '').strip()
            if key and key not in keys:
                keys.append(key)

        _add_key(normalized)
        _add_key(f"KRW-{base}")
        _add_key(f"{base}/KRW")

        for key in keys:
            market = markets.get(key)
            if not isinstance(market, dict):
                continue
            info = market.get('info', {}) if isinstance(market.get('info', {}), dict) else {}
            market_symbol = str(market.get('symbol') or key).upper()
            market_id = str(market.get('id') or info.get('market') or '').upper()
            quote = str(market.get('quote') or info.get('quote') or '').upper()
            market_type = str(market.get('type') or '').lower()
            active = market.get('active', True)
            spot_like = bool(market.get('spot', market_type in {'', 'spot'})) and not bool(market.get('swap', False))
            krw_pair = (
                quote == 'KRW'
                or market_symbol.endswith('/KRW')
                or market_id.startswith('KRW-')
            )
            if krw_pair and spot_like and active is not False:
                return market
        return None

    def _resolve_futures_watch_symbol_from_markets(self, raw_symbol, markets, *, exchange_mode=None):
        mode = exchange_mode or self.get_exchange_mode()
        normalized_items = normalize_coin_selector_custom_symbols([raw_symbol])
        if not normalized_items:
            raise ValueError("빈 심볼은 사용할 수 없습니다.")
        normalized = normalized_items[0]
        market = self._futures_market_for_symbol(normalized, markets)
        if not isinstance(market, dict):
            raise ValueError(f"유효하지 않은 Binance Futures USDT perpetual 심볼입니다: {normalized}")
        market_symbol = market.get('symbol') or normalized
        is_tradifi = coin_selector_market_is_tradifi_perpetual(market_symbol, market)
        if is_tradifi and market_is_blocked_tradifi_commodity(market_symbol, market):
            raise ValueError(
                f"REJECTED_TRADIFI_COMMODITY: 에너지·귀금속·광물 원자재 TradFi 신규 진입은 차단됩니다: {normalized}"
            )
        if is_tradifi and mode != BINANCE_MAINNET:
            raise ValueError(f"TradFi perpetual은 Binance Futures 메인넷에서만 직접 감시합니다: {normalized}")
        return market_symbol if is_tradifi else normalized

    def _get_tradifi_symbols_from_markets(self, markets):
        if not isinstance(markets, dict):
            return []
        result = []
        seen = set()
        for key, market in markets.items():
            if not isinstance(market, dict):
                continue
            symbol = market.get('symbol') or key
            if not coin_selector_market_is_tradifi_perpetual(symbol, market):
                continue
            if market_is_blocked_tradifi_commodity(symbol, market):
                continue
            normalized = normalize_coin_selector_custom_symbols([symbol])
            normalized = normalized[0] if normalized else str(symbol or '').replace(':USDT', '')
            if normalized not in seen:
                result.append(symbol)
                seen.add(normalized)
        return sorted(result)

    async def _load_trade_markets_for_exchange_mode(self, exchange_mode=None):
        exchange = getattr(self, 'exchange', None) or getattr(self, 'market_data_exchange', None)
        if exchange is None or not hasattr(exchange, 'load_markets'):
            raise RuntimeError("현재 거래소 market 정보를 불러올 수 없습니다.")
        markets = await asyncio.to_thread(exchange.load_markets)
        return markets if isinstance(markets, dict) else {}

    def _resolve_watch_symbol_for_exchange_mode(self, raw_symbol, markets, *, exchange_mode=None):
        mode = exchange_mode or self.get_exchange_mode()
        if mode == UPBIT_MODE:
            symbol = self.normalize_symbol_for_exchange(raw_symbol, exchange_mode=UPBIT_MODE)
            if not symbol.endswith('/KRW'):
                raise ValueError("Upbit 모드는 KRW 현물만 허용합니다.")
            if self._upbit_market_for_symbol(symbol, markets) is None:
                raise ValueError(f"유효하지 않은 Upbit KRW 현물 심볼입니다: {symbol}")
            return symbol
        return self._resolve_futures_watch_symbol_from_markets(
            raw_symbol,
            markets,
            exchange_mode=mode,
        )

    def _dedupe_watch_symbols(self, symbols):
        deduped = []
        seen = set()
        for symbol in symbols or []:
            key = self._utbreakout_status_symbol_key(symbol)
            if key and key not in seen:
                deduped.append(symbol)
                seen.add(key)
        return deduped

    def _filter_futures_symbols_for_exchange_mode(self, symbols, markets, *, exchange_mode=None):
        mode = exchange_mode or self.get_exchange_mode()
        valid = []
        removed = []
        seen = set()
        for raw_symbol in normalize_coin_selector_custom_symbols(symbols):
            try:
                resolved = self._resolve_futures_watch_symbol_from_markets(
                    raw_symbol,
                    markets,
                    exchange_mode=mode,
                )
                key = self._futures_symbol_key(resolved)
                if key and key not in seen:
                    valid.append(resolved)
                    seen.add(key)
            except Exception as exc:
                removed.append({'symbol': raw_symbol, 'reason': str(exc)})
        return valid, removed

    def _sync_signal_active_symbols(self, symbols):
        synced = False
        seen_engines = set()
        for key in (CORE_ENGINE, 'signal'):
            engine = self.engines.get(key) if isinstance(getattr(self, 'engines', None), dict) else None
            if not engine or id(engine) in seen_engines:
                continue
            seen_engines.add(id(engine))
            if hasattr(engine, 'active_symbols'):
                engine.active_symbols = set(symbols or [])
                synced = True
            if hasattr(engine, 'scanner_active_symbol'):
                engine.scanner_active_symbol = None
        return synced

    async def _sanitize_coin_selector_universe_for_exchange_mode(self, exchange_mode=None, markets=None, *, persist=True):
        mode = exchange_mode or self.get_exchange_mode()
        signal_cfg = self.cfg.get('signal_engine', {}) or {}
        coin_cfg = signal_cfg.get('coin_selector', {}) if isinstance(signal_cfg.get('coin_selector', {}), dict) else {}
        custom_symbols = normalize_coin_selector_custom_symbols(coin_cfg.get('custom_symbols'))
        removed = []

        if mode == UPBIT_MODE:
            if persist:
                await self._update_config_value(['signal_engine', 'coin_selector', 'enabled'], False)
                await self._update_config_value(['signal_engine', 'coin_selector', 'custom_universe_enabled'], False)
                await self._update_config_value(['signal_engine', 'coin_selector', 'custom_symbols'], [])
                await self._update_config_value(['signal_engine', 'coin_selector', 'include_tradifi_universe'], False)
            return {
                'mode': mode,
                'valid': [],
                'removed': [{'symbol': symbol, 'reason': 'Upbit mode does not use Binance Futures CoinSelector'} for symbol in custom_symbols],
            }

        if markets is None:
            markets = await self._load_trade_markets_for_exchange_mode(mode)

        valid = []
        seen = set()
        for item in custom_symbols:
            try:
                resolved = self._resolve_futures_watch_symbol_from_markets(
                    item,
                    markets,
                    exchange_mode=mode,
                )
                key = self._futures_symbol_key(resolved)
                if key and key not in seen:
                    valid.append(resolved)
                    seen.add(key)
            except Exception as exc:
                removed.append({'symbol': item, 'reason': str(exc)})

        if persist:
            await self._update_config_value(['signal_engine', 'coin_selector', 'custom_symbols'], valid)
            if mode != BINANCE_MAINNET:
                await self._update_config_value(['signal_engine', 'coin_selector', 'include_tradifi_universe'], False)
            elif 'include_tradifi_universe' not in coin_cfg:
                await self._update_config_value(['signal_engine', 'coin_selector', 'include_tradifi_universe'], True)

        return {'mode': mode, 'valid': valid, 'removed': removed}

    async def _sanitize_watchlist_for_exchange_mode(
        self,
        exchange_mode=None,
        *,
        markets=None,
        persist=True,
        reason="exchange mode changed",
    ):
        mode = exchange_mode or self.get_exchange_mode()
        watchlists = self.cfg.get('exchange_watchlists', {}) or {}
        mode_watchlist = watchlists.get(mode) if isinstance(watchlists, dict) else None

        if isinstance(mode_watchlist, list) and mode_watchlist:
            raw_watchlist = list(mode_watchlist)
        elif mode == UPBIT_MODE:
            raw_watchlist = self.cfg.get('upbit', {}).get('watchlist', [])
        else:
            raw_watchlist = self.cfg.get('signal_engine', {}).get('watchlist', [])

        if not isinstance(raw_watchlist, list):
            raw_watchlist = []

        legacy_shadow_watchlist = []
        raw_keys = {self._utbreakout_status_symbol_key(item) for item in raw_watchlist}
        if isinstance(mode_watchlist, list) and mode_watchlist:
            if mode == UPBIT_MODE:
                legacy_shadow_watchlist = self.cfg.get('signal_engine', {}).get('watchlist', [])
            else:
                legacy_shadow_watchlist = self.cfg.get('signal_engine', {}).get('watchlist', [])
            if not isinstance(legacy_shadow_watchlist, list):
                legacy_shadow_watchlist = []

        if markets is None:
            markets = await self._load_trade_markets_for_exchange_mode(mode)

        valid = []
        removed = []
        for raw_symbol in raw_watchlist:
            try:
                valid.append(
                    self._resolve_watch_symbol_for_exchange_mode(
                        raw_symbol,
                        markets,
                        exchange_mode=mode,
                    )
                )
            except Exception as exc:
                removed.append({'symbol': raw_symbol, 'reason': str(exc)})

        for legacy_symbol in legacy_shadow_watchlist:
            if self._utbreakout_status_symbol_key(legacy_symbol) in raw_keys:
                continue
            try:
                self._resolve_watch_symbol_for_exchange_mode(
                    legacy_symbol,
                    markets,
                    exchange_mode=mode,
                )
            except Exception as exc:
                removed.append({'symbol': legacy_symbol, 'reason': str(exc)})

        deduped = self._dedupe_watch_symbols(valid)
        if not deduped:
            fallback_removed = []
            for fallback in self.get_default_watchlist_for_exchange_mode(mode):
                try:
                    deduped.append(
                        self._resolve_watch_symbol_for_exchange_mode(
                            fallback,
                            markets,
                            exchange_mode=mode,
                        )
                    )
                except Exception as exc:
                    fallback_removed.append({'symbol': fallback, 'reason': str(exc)})
            deduped = self._dedupe_watch_symbols(deduped)
            if fallback_removed:
                removed.extend(fallback_removed)

        if not deduped:
            raise RuntimeError(f"No valid watchlist symbols for exchange mode: {mode}")

        coin_selector_result = None
        if persist:
            await self._update_config_value(['exchange_watchlists', mode], deduped)
            if mode == UPBIT_MODE:
                await self._update_config_value(['upbit', 'watchlist'], deduped)
            else:
                await self._update_config_value(['signal_engine', 'watchlist'], deduped)
            coin_selector_result = await self._sanitize_coin_selector_universe_for_exchange_mode(
                mode,
                markets=markets,
                persist=True,
            )

        self._sync_signal_active_symbols(deduped)

        if removed:
            logger.warning(
                "Watchlist sanitized for %s (%s). kept=%s removed=%s",
                mode,
                reason,
                deduped,
                removed,
            )
        else:
            logger.info("Watchlist validated for %s (%s): %s", mode, reason, deduped)

        return {
            'mode': mode,
            'watchlist': deduped,
            'removed': removed,
            'reason': reason,
            'coin_selector': coin_selector_result,
        }

    async def _assert_symbol_tradeable_in_current_exchange_mode(self, symbol):
        mode = self.get_exchange_mode()
        markets = await self._load_trade_markets_for_exchange_mode(mode)
        resolved = self._resolve_watch_symbol_for_exchange_mode(symbol, markets, exchange_mode=mode)

        if mode == UPBIT_MODE:
            return resolved

        market = self._futures_market_for_symbol(resolved, markets)
        if not isinstance(market, dict):
            raise ValueError(f"현재 Binance Futures market에서 찾을 수 없는 심볼입니다: {symbol}")
        market_symbol = market.get('symbol') or resolved
        is_tradifi = coin_selector_market_is_tradifi_perpetual(market_symbol, market)
        if is_tradifi and market_is_blocked_tradifi_commodity(market_symbol, market):
            raise ValueError(
                f"REJECTED_TRADIFI_COMMODITY: 에너지·귀금속·광물 원자재 TradFi 신규 진입은 차단됩니다: {symbol}"
            )
        if is_tradifi and mode == BINANCE_TESTNET:
            raise ValueError(f"Binance Testnet에서는 TradeFi/tokenized stock 심볼을 주문할 수 없습니다: {symbol}")
        if is_tradifi and mode != BINANCE_MAINNET:
            raise ValueError(f"TradeFi/tokenized stock은 Binance Futures Mainnet에서만 주문할 수 있습니다: {symbol}")
        return resolved

    def _format_watchlist_sanitize_result(self, sanitize_result):
        if not isinstance(sanitize_result, dict):
            return ""
        mode = sanitize_result.get('mode') or self.get_exchange_mode()
        watchlist = sanitize_result.get('watchlist') or []
        removed = sanitize_result.get('removed') or []

        lines = ["감시목록이 현재 거래소 기준으로 정리되었습니다."]
        if removed:
            removed_symbols = [str(item.get('symbol')) for item in removed if isinstance(item, dict)]
            if removed_symbols:
                lines.append(f"제거: {', '.join(removed_symbols[:12])}")
            reasons = []
            for item in removed:
                reason = str((item or {}).get('reason') or '').strip()
                if reason and reason not in reasons:
                    reasons.append(reason)
            if reasons:
                lines.append(f"제거 사유: {'; '.join(reasons[:3])}")

        lines.append(f"현재 감시목록: {', '.join(watchlist)}")
        if mode == BINANCE_TESTNET:
            lines.append("TradeFi/tokenized stock 심볼은 테스트넷에서 차단됩니다.")
        elif mode == BINANCE_MAINNET:
            lines.append("TradeFi 주식·ETF·지수 심볼은 메인넷에서 허용되며, 에너지·귀금속·광물 원자재는 신규 진입이 차단됩니다.")
        elif mode == UPBIT_MODE:
            lines.append("Upbit 모드는 KRW 현물 마켓만 감시합니다.")
        return "\n".join(lines)

    def _format_exchange_reinit_result(self, reinit_result):
        if not isinstance(reinit_result, dict):
            return f"✅ 거래소 전환 완료: {reinit_result}"
        mode = reinit_result.get('mode') or self.get_exchange_mode()
        network_name = reinit_result.get('network_name') or self.get_network_status_label(mode)
        lines = [f"✅ 거래소 전환 완료: {network_name}", ""]
        summary = self._format_watchlist_sanitize_result(reinit_result.get('sanitize'))
        if summary:
            lines.append(summary)
        return "\n".join(lines).strip()

    def format_symbol_for_display(self, symbol, exchange_mode=None):
        mode = exchange_mode or self.get_exchange_mode()
        raw = str(symbol or '')
        if mode == UPBIT_MODE and '/' in raw:
            base, quote = raw.split('/', 1)
            if quote.upper() == 'KRW':
                return f"KRW-{base.upper()}"
        return raw

    def _futures_symbol_for_order(self, raw_symbol):
        text = str(raw_symbol or '').strip().upper()
        if not text:
            return ''
        for suffix in (':USDT', ':USDC', ':BUSD'):
            text = text.replace(suffix, '')
        if '/' in text:
            return text
        for quote in ('USDT', 'USDC', 'BUSD'):
            if text.endswith(quote) and len(text) > len(quote):
                return f"{text[:-len(quote)]}/{quote}"
        return self.normalize_symbol_for_exchange(text)

    def _futures_symbol_key(self, value):
        text = str(value or '').strip().upper()
        for suffix in (':USDT', ':USDC', ':BUSD'):
            text = text.replace(suffix, '')
        return (
            text
            .replace('/', '')
            .replace('-', '')
            .replace('_', '')
            .replace(':', '')
        )

    def _position_numeric_value(self, position, keys):
        if not isinstance(position, dict):
            return None
        info = position.get('info', {}) if isinstance(position.get('info'), dict) else {}
        for source in (position, info):
            for key in keys:
                if key not in source:
                    continue
                value = source.get(key)
                if value in (None, ''):
                    continue
                try:
                    return float(value)
                except (TypeError, ValueError):
                    continue
        return None

    def _position_signed_contracts(self, position):
        if not isinstance(position, dict):
            return 0.0
        signed_value = self._position_numeric_value(
            position,
            ('positionAmt', 'position_amt', 'pa')
        )
        if signed_value is not None:
            return signed_value

        amount = self._position_numeric_value(
            position,
            ('contracts', 'contract', 'amount', 'size', 'qty', 'position')
        )
        if amount is None:
            return 0.0
        if amount < 0:
            return amount

        side = str(position.get('side', '') or '').lower()
        info = position.get('info', {}) if isinstance(position.get('info'), dict) else {}
        position_side = str(info.get('positionSide', '') or info.get('side', '') or '').lower()
        if side == 'short' or position_side == 'short':
            return -abs(amount)
        return abs(amount)

    def _position_side_for_close(self, position, signed_contracts=None):
        if signed_contracts is None:
            signed_contracts = self._position_signed_contracts(position)
        side = str((position or {}).get('side', '') or '').lower()
        if side in {'long', 'short'}:
            return side
        info = (position or {}).get('info', {}) if isinstance((position or {}).get('info'), dict) else {}
        raw_position_side = str(info.get('positionSide', '') or info.get('side', '') or '').lower()
        if raw_position_side in {'long', 'short'}:
            return raw_position_side
        if signed_contracts > 0:
            return 'long'
        if signed_contracts < 0:
            return 'short'
        return ''

    def _normalize_futures_position_for_emergency(self, position):
        if not isinstance(position, dict):
            return None
        info = position.get('info', {}) if isinstance(position.get('info'), dict) else {}
        raw_symbol = (
            position.get('symbol')
            or info.get('symbol')
            or info.get('pair')
            or ''
        )
        symbol = self._futures_symbol_for_order(raw_symbol)
        signed_contracts = self._position_signed_contracts(position)
        contracts = abs(float(signed_contracts or 0.0))
        if not symbol or contracts <= 0:
            return None
        side = self._position_side_for_close(position, signed_contracts)
        if side not in {'long', 'short'}:
            return None
        return {
            'symbol': symbol,
            'side': side,
            'contracts': contracts,
            'raw_position': position,
            'pnl': _safe_float_or_none(position.get('unrealizedPnl') or info.get('unRealizedProfit')) or 0.0,
            'mark_price': _safe_float_or_none(position.get('markPrice') or info.get('markPrice')) or 0.0,
            'positionSide': str(info.get('positionSide') or position.get('positionSide') or '').upper(),
        }

    def _emergency_position_matches_symbol(self, position, symbol):
        normalized_pos = self._normalize_futures_position_for_emergency(position)
        if not normalized_pos:
            return False
        return self._futures_symbol_key(normalized_pos.get('symbol')) == self._futures_symbol_key(symbol)

    async def _fetch_emergency_position_by_symbol(self, symbol):
        positions = await asyncio.to_thread(self.exchange.fetch_positions)
        target_key = self._futures_symbol_key(symbol)
        for p in positions or []:
            normalized_pos = self._normalize_futures_position_for_emergency(p)
            if not normalized_pos:
                continue
            if self._futures_symbol_key(normalized_pos.get('symbol')) == target_key:
                return normalized_pos
        return None

    def _close_order_params_for_position(self, position):
        params = {'reduceOnly': True}
        raw_position = position.get('raw_position') if isinstance(position, dict) else None
        raw_position = raw_position if isinstance(raw_position, dict) else position
        info = raw_position.get('info', {}) if isinstance(raw_position, dict) and isinstance(raw_position.get('info'), dict) else {}
        position_side = str(
            (position or {}).get('positionSide')
            or info.get('positionSide')
            or (raw_position or {}).get('positionSide')
            or ''
        ).upper()
        if position_side in {'LONG', 'SHORT'}:
            params['positionSide'] = position_side
        return params

    def _safe_emergency_amount(self, symbol, contracts):
        try:
            return self.exchange.amount_to_precision(symbol, contracts)
        except Exception as exc:
            logger.warning(f"Emergency amount precision fallback for {symbol}: {exc}")
            return str(round(float(contracts or 0.0), 8))

    def _format_emergency_stop_reply(self, result):
        if not isinstance(result, dict):
            return "🚨 긴급 정지 완료"
        status = result.get('status')
        if status == 'error':
            return f"❌ 긴급 정지 중 오류: {result.get('error', 'unknown error')}"
        closed = int(result.get('closed', 0) or 0)
        failed = int(result.get('failed', 0) or 0)
        total = int(result.get('position_count', closed + failed) or 0)
        if failed:
            failed_symbols = ", ".join(str(item.get('symbol')) for item in result.get('failed_positions', [])[:5])
            return (
                f"⚠️ 긴급 정지 일부 실패\n"
                f"청산 성공 {closed}/{max(total, closed + failed)}개, 실패 {failed}개"
                + (f"\n실패: {failed_symbols}" if failed_symbols else "")
            )
        if closed:
            return f"✅ 긴급 정지 완료\n청산 성공 {closed}개"
        if status == 'no_position':
            cancelled = int(result.get('cancelled_orders', 0) or 0)
            return f"ℹ️ 긴급 정지 완료\n청산할 오픈 포지션 없음. 미체결 주문 정리 {cancelled}건"
        return "🧯 긴급 정지 처리 완료"

    def _build_exchange(self, creds, exchange_mode=None):
        mode = exchange_mode or self.get_exchange_mode()
        if mode == UPBIT_MODE:
            return ccxt.upbit({
                'apiKey': creds.get('api_key', ''),
                'secret': creds.get('secret_key', ''),
                'enableRateLimit': True
            })
        return ccxt.binance({
            'apiKey': creds.get('api_key', ''),
            'secret': creds.get('secret_key', ''),
            'options': {
                'defaultType': 'future',
                'warnOnFetchOpenOrdersWithoutSymbol': False,
            },
            'enableRateLimit': True
        })

    def _build_public_market_data_exchange(self, exchange_mode=None):
        mode = exchange_mode or self.get_exchange_mode()
        if mode == UPBIT_MODE:
            return ccxt.upbit({
                'enableRateLimit': True
            })
        return ccxt.binance({
            'options': {
                'defaultType': 'future',
                'warnOnFetchOpenOrdersWithoutSymbol': False,
            },
            'enableRateLimit': True
        })

    def _configure_exchange_network(self, exchange, exchange_mode=None):
        mode = exchange_mode or self.get_exchange_mode()
        if mode == UPBIT_MODE:
            return "업비트 KRW 현물"

        if mode != BINANCE_TESTNET:
            return "메인넷 💰"

        try:
            if hasattr(exchange, 'enable_demo_trading'):
                exchange.enable_demo_trading(True)
                logger.info("Exchange network configured: Binance Demo Trading")
                return "테스트넷(데모) 🧪"
            if hasattr(exchange, 'enableDemoTrading'):
                exchange.enableDemoTrading(True)
                logger.info("Exchange network configured: Binance Demo Trading")
                return "테스트넷(데모) 🧪"
        except Exception as e:
            logger.warning(f"Failed to enable demo trading, fallback to sandbox mode: {e}")

        try:
            exchange.set_sandbox_mode(True)
            logger.warning("Using legacy sandbox mode. Consider updating ccxt >= 4.5.6.")
            return "테스트넷(샌드박스) 🧪"
        except Exception as e:
            logger.error(f"Failed to configure testnet mode: {e}")
            raise

    def _format_duration(self, seconds):
        if seconds is None:
            return "-"
        seconds = max(0, int(seconds))
        hours, rem = divmod(seconds, 3600)
        minutes, secs = divmod(rem, 60)
        if hours >= 24:
            days, hours = divmod(hours, 24)
            return f"{days}d {hours:02d}:{minutes:02d}:{secs:02d}"
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"

    def _read_last_exit_info(self):
        try:
            if not os.path.exists(self.exit_file):
                return {}
            with open(self.exit_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.warning(f"Exit marker read error: {e}")
            return {}

    def _get_rss_mb(self):
        try:
            with open('/proc/self/status', 'r', encoding='utf-8') as f:
                for line in f:
                    if line.startswith('VmRSS:'):
                        parts = line.split()
                        if len(parts) >= 2:
                            return round(int(parts[1]) / 1024.0, 1)
        except Exception:
            return None
        return None

    def _get_effective_last_exit_info(self):
        info = self.last_exit_info if isinstance(self.last_exit_info, dict) else {}
        if not info:
            return {}
        prev_pid = str(self.last_pid_before_start or "").strip()
        exit_pid = str(info.get('pid', '')).strip()
        if prev_pid and exit_pid and prev_pid != exit_pid:
            return {}
        return info

    def _write_heartbeat(self):
        payload = {
            'ts': datetime.now().astimezone().isoformat(timespec='seconds'),
            'epoch': int(time.time()),
            'pid': os.getpid(),
            'launch_reason': self.launch_reason,
            'paused': self.is_paused,
            'engine': self.cfg.get('system_settings', {}).get('active_engine', CORE_ENGINE),
            'status_count': len(self.status_data) if isinstance(self.status_data, dict) else 0,
            'rss_mb': self._get_rss_mb()
        }
        try:
            atomic_write_json(self.heartbeat_file, payload, ensure_ascii=False, indent=None)
        except Exception as e:
            logger.error(f"Heartbeat write error: {e}")

    def record_exit_marker(self, reason, detail=""):
        if self._exit_recorded:
            return
        self._exit_recorded = True
        payload = {
            'ts': datetime.now().astimezone().isoformat(timespec='seconds'),
            'pid': os.getpid(),
            'reason': str(reason),
            'detail': str(detail or "")[:500],
            'uptime_sec': int(max(0, time.time() - self.process_start_ts)),
            'rss_mb': self._get_rss_mb()
        }
        try:
            atomic_write_json(self.exit_file, payload, ensure_ascii=False, indent=None)
            logger.info(f"Exit marker recorded: {payload}")
        except Exception as e:
            logger.error(f"Exit marker write error: {e}")

    def get_runtime_diag(self):
        last_exit = self._get_effective_last_exit_info()
        return {
            'launch_reason': self.launch_reason,
            'launch_started_at': self.launch_started_at,
            'uptime_human': self._format_duration(time.time() - self.process_start_ts),
            'last_heartbeat_age': self.last_heartbeat_age,
            'last_heartbeat_age_human': self._format_duration(self.last_heartbeat_age),
            'last_pid_before_start': self.last_pid_before_start,
            'last_log_line': self.last_log_line,
            'rss_mb': self._get_rss_mb(),
            'last_exit_reason': last_exit.get('reason'),
            'last_exit_ts': last_exit.get('ts'),
            'last_exit_detail': last_exit.get('detail'),
            'last_exit_uptime_human': self._format_duration(last_exit.get('uptime_sec')),
            'last_exit_rss_mb': last_exit.get('rss_mb')
        }

    def _build_startup_notice(self):
        header = "⏸ **봇 시작됨 (일시정지 상태)**" if self.is_paused else "▶ **봇 시작됨 (자동 재개 상태)**"
        lines = [header, ""]
        lines.append(f"거래모드: `{self.get_exchange_display_name()}` / `{self.get_network_status_label()}`")
        lines.append(f"시작사유: `{self.launch_reason}`")
        lines.append(f"시작시각: `{self.launch_started_at}`")
        if self.last_heartbeat_age is not None:
            lines.append(f"이전 heartbeat age: `{self._format_duration(self.last_heartbeat_age)}`")
        if self.last_pid_before_start:
            lines.append(f"이전 PID: `{self.last_pid_before_start}`")
        if self.prev_paused_state is not None:
            lines.append(f"이전 일시정지 상태: `{'ON' if self.prev_paused_state else 'OFF'}`")
        if self.last_log_line:
            lines.append(f"직전 로그: `{self.last_log_line}`")
        last_exit = self._get_effective_last_exit_info()
        if last_exit:
            lines.append(
                f"직전 종료: `{last_exit.get('reason', '-')}` "
                f"@ `{last_exit.get('ts', '-')}`"
            )
        if self.is_paused:
            lines.extend(["", "설정 확인 후 `▶ RESUME`을 눌러주세요."])
        else:
            lines.extend(["", "이전 실행 상태를 유지해 자동으로 재개되었습니다."])
        return "\n".join(lines)

    def _get_alt_trend_alert_settings(self):
        reporting = self.cfg.get('telegram', {}).get('reporting', {}) or {}
        timeframes = normalize_alt_trend_timeframes(
            reporting.get('alt_trend_alert_timeframes', ['1d'])
        ) or ['1d']
        periodic_enabled = bool(reporting.get('periodic_reports_enabled', False))
        return {
            'enabled': periodic_enabled and bool(reporting.get('alt_trend_alert_enabled', False)),
            'timeframes': timeframes,
            'scope': 'binance_futures_all',
            'stage_mode': 'setup_and_confirm',
            'profile': 'conservative',
            'oi_cvd_mode': 'required'
        }

    def _format_alt_trend_alert_item(self, item):
        symbol_label = self.format_symbol_for_display(item.get('symbol', ''))
        cvd_label = "▲" if float(item.get('cvd_delta', 0.0) or 0.0) > 0 else "▼"
        return (
            f"- `{symbol_label}` score `{float(item.get('score', 0.0) or 0.0):.1f}` | "
            f"RSI `{float(item.get('curr_rsi', 0.0) or 0.0):.1f}` | "
            f"Vol `x{float(item.get('volume_ratio', 0.0) or 0.0):.2f}` | "
            f"OI `{float(item.get('oi_delta_pct', 0.0) or 0.0):+.2f}%` | "
            f"CVD `{cvd_label}`"
        )

    def _build_alt_trend_alert_chunks(self, results_by_tf, scanned_timeframes=None):
        if not results_by_tf:
            return []

        scanned_tf_text = format_alt_trend_timeframes(scanned_timeframes or results_by_tf.keys())
        header_lines = [
            "🚀 **알트 급등 알림**",
            f"시각: `{datetime.now().strftime('%m-%d %H:%M:%S')}`",
            f"스캔 TF: `{scanned_tf_text}`"
        ]
        header = "\n".join(header_lines)
        sections = []

        for timeframe in sorted(results_by_tf.keys(), key=lambda tf: ALT_TREND_TIMEFRAME_ORDER.get(tf, 999)):
            tf_result = results_by_tf.get(timeframe, {}) or {}
            confirms = tf_result.get('confirm', []) or []
            setups = tf_result.get('setup', []) or []
            if not confirms and not setups:
                continue

            section_lines = [f"**{timeframe}**"]
            if confirms:
                section_lines.append("🟢 확정")
                section_lines.extend(self._format_alt_trend_alert_item(item) for item in confirms)
            if setups:
                section_lines.append("🟡 준비")
                section_lines.extend(self._format_alt_trend_alert_item(item) for item in setups)
            sections.append("\n".join(section_lines))

        if not sections:
            return []

        chunks = []
        current = header
        for section in sections:
            candidate = f"{current}\n\n{section}".strip()
            if len(candidate) > 3800 and current != header:
                chunks.append(current.strip())
                current = f"{header}\n\n{section}".strip()
            else:
                current = candidate
        if current:
            chunks.append(current.strip())

        if len(chunks) <= 1:
            return chunks

        numbered_chunks = []
        total = len(chunks)
        for idx, chunk in enumerate(chunks, start=1):
            numbered_chunks.append(chunk.replace("🚀 **알트 급등 알림**", f"🚀 **알트 급등 알림** ({idx}/{total})", 1))
        return numbered_chunks

    def _build_binance_futures_rest_symbol(self, symbol):
        text = str(symbol or '').strip().upper()
        if not text:
            return ''
        if '/' in text:
            base, quote = text.split('/', 1)
            quote = quote.split(':', 1)[0]
            return f"{base}{quote}"
        return text.replace(':', '')

    def _fetch_binance_public_json_sync(self, path, params):
        query = urllib.parse.urlencode(params)
        url = f"{BINANCE_FAPI_PUBLIC_BASE_URL}{path}?{query}"
        request = urllib.request.Request(
            url,
            headers={
                'User-Agent': 'Mozilla/5.0',
                'Accept': 'application/json'
            }
        )
        with urllib.request.urlopen(request, timeout=12) as response:
            payload = response.read().decode('utf-8')
        return json.loads(payload)

    async def _fetch_binance_public_json(self, path, params):
        return await asyncio.to_thread(self._fetch_binance_public_json_sync, path, params)

    async def _fetch_utbreakout_futures_context(self, symbol):
        """Fetch lightweight futures context for controller-side reports.

        The SignalEngine implementation is the source of truth for entry
        evaluation. This controller helper delegates there when available so
        status/report paths keep the same field contract, then falls back to a
        small None-safe public-data fetch for isolated helper use.
        """
        if self.is_upbit_mode():
            return {}
        signal_engine = getattr(self, 'engines', {}).get('signal') if isinstance(getattr(self, 'engines', None), dict) else None
        if signal_engine is not None and hasattr(signal_engine, '_fetch_utbreakout_futures_context'):
            try:
                context = await signal_engine._fetch_utbreakout_futures_context(symbol)
                return dict(context or {})
            except Exception as exc:
                delegate_error = f"signalEngineContext: {exc}"
        else:
            delegate_error = None

        if not hasattr(self, 'utbreakout_futures_context_cache') or not isinstance(self.utbreakout_futures_context_cache, dict):
            self.utbreakout_futures_context_cache = {}
        if not hasattr(self, 'utbreakout_orderflow_snapshots') or not isinstance(self.utbreakout_orderflow_snapshots, dict):
            self.utbreakout_orderflow_snapshots = {}

        def _open_interest_stats(oi_hist):
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

        def _funding_percentiles(funding_rows, current_funding):
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

        def _liquidation_proxy(context):
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

        def _build_orderflow_snapshot(depth, timestamp=None):
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

        def _update_orderflow_snapshots(snapshot=None, *, window=5, max_samples=20):
            symbol_key = str(symbol or '')
            rows = list(self.utbreakout_orderflow_snapshots.get(symbol_key) or [])
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
            self.utbreakout_orderflow_snapshots[symbol_key] = rows
            try:
                window = max(1, int(window or 5))
            except (TypeError, ValueError):
                window = 5
            recent = [
                row for row in rows[-window:]
                if _safe_float_or_none((row or {}).get('orderbook_imbalance_pct')) is not None
            ]
            latest = rows[-1] if rows else {}
            orderflow_context = {}
            if latest:
                orderflow_context.update({
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
                orderflow_context.update({
                    'rolling_orderbook_imbalance_pct': rolling,
                    'rolling_orderbook_imbalance_delta': delta,
                    'rolling_ofi_score': rolling + (delta * 0.5),
                    'rolling_ofi_samples': len(imbalances),
                })
            else:
                orderflow_context['rolling_ofi_samples'] = 0
            return {key: value for key, value in orderflow_context.items() if value is not None}

        now = time.time()
        cached = self.utbreakout_futures_context_cache.get(symbol)
        cached_at = float(cached.get('cached_at', 0) or 0) if isinstance(cached, dict) else 0.0
        cache_fresh = isinstance(cached, dict) and (now - cached_at) < 300
        context = dict((cached or {}).get('data') or {}) if cache_fresh else {}
        if delegate_error:
            context.setdefault('futures_context_error', delegate_error)

        rest_symbol = self._build_binance_futures_rest_symbol(symbol)
        if not rest_symbol:
            return {}

        if not cache_fresh:
            context = {'futures_context_error': delegate_error} if delegate_error else {}
            try:
                premium = await self._fetch_binance_public_json(
                    '/fapi/v1/premiumIndex',
                    {'symbol': rest_symbol}
                )
                if isinstance(premium, dict):
                    context.update({
                        'funding_rate': _safe_float_or_none(premium.get('lastFundingRate')),
                        'next_funding_time': int(premium.get('nextFundingTime') or 0) or None,
                        'mark_price': _safe_float_or_none(premium.get('markPrice')),
                        'index_price': _safe_float_or_none(premium.get('indexPrice')),
                    })
            except Exception as e:
                context['futures_context_error'] = f"premiumIndex: {e}"

            try:
                oi = await self._fetch_binance_public_json(
                    '/fapi/v1/openInterest',
                    {'symbol': rest_symbol}
                )
                if isinstance(oi, dict):
                    context['open_interest'] = _safe_float_or_none(oi.get('openInterest'))
                    context['open_interest_ts'] = int(oi.get('time') or 0) or None
            except Exception as e:
                if 'futures_context_error' not in context:
                    context['futures_context_error'] = f"openInterest: {e}"

            if context.get('open_interest') is not None and context.get('mark_price') is not None:
                try:
                    context['open_interest_usdt'] = float(context['open_interest']) * float(context['mark_price'])
                except (TypeError, ValueError):
                    pass

            try:
                oi_hist = await self._fetch_binance_public_json(
                    '/futures/data/openInterestHist',
                    {'symbol': rest_symbol, 'period': '15m', 'limit': 20}
                )
                context.update({
                    key: value for key, value in _open_interest_stats(oi_hist).items()
                    if value is not None
                })
            except Exception as e:
                context.setdefault('futures_context_error', f"openInterestHist: {e}")

            try:
                funding_rows = await self._fetch_binance_public_json(
                    '/fapi/v1/fundingRate',
                    {'symbol': rest_symbol, 'limit': 120}
                )
                context.update(_funding_percentiles(funding_rows, context.get('funding_rate')))
            except Exception as e:
                context.setdefault('futures_context_error', f"fundingRate: {e}")

            try:
                ratio_rows = await self._fetch_binance_public_json(
                    '/futures/data/globalLongShortAccountRatio',
                    {'symbol': rest_symbol, 'period': '15m', 'limit': 1}
                )
                if isinstance(ratio_rows, list) and ratio_rows:
                    latest = ratio_rows[-1]
                    context.update({
                        'long_short_ratio': _safe_float_or_none(latest.get('longShortRatio')),
                        'long_account': _safe_float_or_none(latest.get('longAccount')),
                        'short_account': _safe_float_or_none(latest.get('shortAccount')),
                    })
            except Exception as e:
                context.setdefault('futures_context_error', f"globalLongShortAccountRatio: {e}")

            if context.get('mark_price') is not None and context.get('index_price') is not None:
                try:
                    context['basis_pct'] = (
                        (float(context['mark_price']) - float(context['index_price']))
                        / max(abs(float(context['index_price'])), 1e-9)
                        * 100.0
                    )
                except (TypeError, ValueError):
                    pass

        orderflow_cache_ttl = 30.0
        rows = self.utbreakout_orderflow_snapshots.get(str(symbol or '')) or []
        latest_snapshot = rows[-1] if isinstance(rows, list) and rows else None
        latest_snapshot_ts = float((latest_snapshot or {}).get('timestamp', 0) or 0)
        orderflow_due = not latest_snapshot_ts or (now - latest_snapshot_ts) >= orderflow_cache_ttl
        try:
            if orderflow_due:
                depth = await self._fetch_binance_public_json(
                    '/fapi/v1/depth',
                    {'symbol': rest_symbol, 'limit': 20}
                )
                context.update(_update_orderflow_snapshots(_build_orderflow_snapshot(depth, timestamp=now), window=5))
            else:
                context.update(_update_orderflow_snapshots(None, window=5))
        except Exception as e:
            context.setdefault('futures_context_error', f"depth: {e}")
            context.update(_update_orderflow_snapshots(None, window=5))

        if orderflow_due or not cache_fresh:
            try:
                taker_rows = await self._fetch_binance_public_json(
                    '/futures/data/takerlongshortRatio',
                    {'symbol': rest_symbol, 'period': '15m', 'limit': 1}
                )
                if isinstance(taker_rows, list) and taker_rows:
                    latest = taker_rows[-1]
                    context.update({
                        'taker_buy_sell_ratio': _safe_float_or_none(latest.get('buySellRatio')),
                        'taker_buy_vol': _safe_float_or_none(latest.get('buyVol')),
                        'taker_sell_vol': _safe_float_or_none(latest.get('sellVol')),
                    })
            except Exception as e:
                context.setdefault('futures_context_error', f"takerlongshortRatio: {e}")

        context.update(_liquidation_proxy(context))
        clean_context = {k: v for k, v in context.items() if v is not None}
        self.utbreakout_futures_context_cache[symbol] = {
            'cached_at': cached_at if cache_fresh else now,
            'data': clean_context
        }
        return clean_context

    async def _fetch_alt_trend_oi_cvd_metrics(self, symbol, timeframe):
        rest_symbol = self._build_binance_futures_rest_symbol(symbol)
        if not rest_symbol:
            return None

        try:
            if timeframe in {'5m', '15m', '30m'}:
                oi_rows = await self._fetch_binance_public_json(
                    '/futures/data/openInterestHist',
                    {'symbol': rest_symbol, 'period': timeframe, 'limit': 2}
                )
                cvd_rows = await self._fetch_binance_public_json(
                    '/futures/data/takerlongshortRatio',
                    {'symbol': rest_symbol, 'period': timeframe, 'limit': 2}
                )
                if not isinstance(oi_rows, list) or len(oi_rows) < 2:
                    return None
                if not isinstance(cvd_rows, list) or len(cvd_rows) < 1:
                    return None

                oi_rows = sorted(oi_rows, key=lambda row: int(row.get('timestamp', 0) or 0))
                cvd_rows = sorted(cvd_rows, key=lambda row: int(row.get('timestamp', 0) or 0))
                start_row = oi_rows[-2]
                end_row = oi_rows[-1]
                cvd_window = cvd_rows[-1:]
            else:
                bars_by_timeframe = {
                    '1h': 1,
                    '2h': 2,
                    '4h': 4,
                    '6h': 6,
                    '8h': 8,
                    '1d': 24
                }
                bars = bars_by_timeframe.get(timeframe)
                if not bars:
                    return None

                oi_rows = await self._fetch_binance_public_json(
                    '/futures/data/openInterestHist',
                    {'symbol': rest_symbol, 'period': '1h', 'limit': bars + 1}
                )
                cvd_rows = await self._fetch_binance_public_json(
                    '/futures/data/takerlongshortRatio',
                    {'symbol': rest_symbol, 'period': '1h', 'limit': bars}
                )
                if not isinstance(oi_rows, list) or len(oi_rows) < (bars + 1):
                    return None
                if not isinstance(cvd_rows, list) or len(cvd_rows) < bars:
                    return None

                oi_rows = sorted(oi_rows, key=lambda row: int(row.get('timestamp', 0) or 0))
                cvd_rows = sorted(cvd_rows, key=lambda row: int(row.get('timestamp', 0) or 0))
                oi_window = oi_rows[-(bars + 1):]
                cvd_window = cvd_rows[-bars:]
                start_row = oi_window[0]
                end_row = oi_window[-1]

            start_oi = float(
                start_row.get('sumOpenInterestValue')
                or start_row.get('sumOpenInterest')
                or 0.0
            )
            end_oi = float(
                end_row.get('sumOpenInterestValue')
                or end_row.get('sumOpenInterest')
                or 0.0
            )
            oi_delta_pct = ((end_oi - start_oi) / start_oi * 100.0) if start_oi > 0 else 0.0
            cvd_delta = 0.0
            latest_ts = int(end_row.get('timestamp', 0) or 0)
            for row in cvd_window:
                buy_vol = float(row.get('buyVol') or 0.0)
                sell_vol = float(row.get('sellVol') or 0.0)
                cvd_delta += (buy_vol - sell_vol)
                latest_ts = max(latest_ts, int(row.get('timestamp', 0) or 0))

            return {
                'oi_delta_pct': float(oi_delta_pct),
                'cvd_delta': float(cvd_delta),
                'oi_cvd_pass': bool(oi_delta_pct > 0 and cvd_delta > 0),
                'oi_cvd_ts': latest_ts
            }
        except Exception as e:
            logger.warning(f"Alt trend OI/CVD fetch failed for {symbol} {timeframe}: {e}")
            return None

    async def _get_last_closed_candle_ts_for_alert(self, timeframe):
        try:
            ohlcv = await asyncio.to_thread(
                self.market_data_exchange.fetch_ohlcv,
                'BTC/USDT:USDT',
                timeframe,
                limit=3
            )
            if not ohlcv or len(ohlcv) < 2:
                return 0
            return int(ohlcv[-2][0] or 0)
        except Exception as e:
            logger.warning(f"Alt trend candle sync failed for {timeframe}: {e}")
            return 0

    async def _scan_alt_trend_timeframe(self, timeframe, expected_candle_ts, ranked_symbols):
        signal_engine = self.engines.get('signal')
        if not signal_engine:
            return {'confirm': [], 'setup': []}

        strategy_params = self.get_active_strategy_params()
        price_semaphore = asyncio.Semaphore(8)
        oi_cvd_semaphore = asyncio.Semaphore(4)

        async def _evaluate_price(symbol_row):
            symbol = symbol_row.get('symbol')
            quote_volume = float(symbol_row.get('quote_volume', 0.0) or 0.0)
            try:
                async with price_semaphore:
                    ohlcv = await asyncio.to_thread(
                        self.market_data_exchange.fetch_ohlcv,
                        symbol,
                        timeframe,
                        limit=300
                    )
                if not ohlcv or len(ohlcv) < 60:
                    return None

                last_closed_ts = int(ohlcv[-2][0] or 0) if len(ohlcv) >= 2 else 0
                if expected_candle_ts and last_closed_ts != expected_candle_ts:
                    return None

                df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                result = signal_engine._calculate_alt_trend_price_signal(symbol, df, strategy_params)
                if not result:
                    return None
                if expected_candle_ts and int(result.get('candle_ts', 0) or 0) != expected_candle_ts:
                    return None
                result['quote_volume'] = quote_volume
                result['timeframe'] = timeframe
                return result
            except Exception as e:
                logger.warning(f"Alt trend price scan failed for {symbol} {timeframe}: {e}")
                return None

        price_tasks = [_evaluate_price(symbol_row) for symbol_row in ranked_symbols]
        price_candidates = [item for item in await asyncio.gather(*price_tasks) if item]
        price_candidates.sort(
            key=lambda item: (1 if item.get('stage') == 'confirm' else 0, float(item.get('score', 0.0) or 0.0)),
            reverse=True
        )
        shortlist = price_candidates[:12]
        if not shortlist:
            return {'confirm': [], 'setup': []}

        async def _attach_oi_cvd(item):
            try:
                async with oi_cvd_semaphore:
                    metrics = await self._fetch_alt_trend_oi_cvd_metrics(item.get('symbol'), timeframe)
                if not metrics or not metrics.get('oi_cvd_pass', False):
                    return None
                enriched = dict(item)
                enriched.update(metrics)
                return enriched
            except Exception as e:
                logger.warning(f"Alt trend OI/CVD attach failed for {item.get('symbol')} {timeframe}: {e}")
                return None

        final_candidates = [item for item in await asyncio.gather(*[_attach_oi_cvd(item) for item in shortlist]) if item]
        final_candidates.sort(
            key=lambda item: (1 if item.get('stage') == 'confirm' else 0, float(item.get('score', 0.0) or 0.0)),
            reverse=True
        )

        confirms = []
        setups = []
        for item in final_candidates:
            key = (timeframe, item.get('symbol'), item.get('stage'))
            candle_ts = int(item.get('candle_ts', 0) or 0)
            if self.last_alt_trend_alert_sent.get(key) == candle_ts:
                continue
            if item.get('stage') == 'confirm':
                confirms.append(item)
            else:
                setups.append(item)

        confirms = confirms[:5]
        remaining_slots = max(0, 5 - len(confirms))
        setups = setups[:remaining_slots]

        return {
            'confirm': confirms,
            'setup': setups
        }

    async def _alt_trend_alert_loop(self):
        await asyncio.sleep(20)
        while True:
            try:
                settings = self._get_alt_trend_alert_settings()
                if self.is_upbit_mode() or not settings.get('enabled', False):
                    await asyncio.sleep(60)
                    continue

                due_timeframes = {}
                for timeframe in settings.get('timeframes', []):
                    closed_ts = await self._get_last_closed_candle_ts_for_alert(timeframe)
                    if closed_ts <= 0:
                        continue
                    if int(self.last_alt_trend_scan_candle_ts_by_tf.get(timeframe, 0) or 0) >= closed_ts:
                        continue
                    due_timeframes[timeframe] = closed_ts

                if not due_timeframes:
                    await asyncio.sleep(60)
                    continue

                try:
                    tickers = await asyncio.to_thread(self.market_data_exchange.fetch_tickers)
                except Exception as e:
                    logger.warning(f"Alt trend ticker scan failed: {e}")
                    await asyncio.sleep(60)
                    continue

                ranked_symbols = []
                for symbol, data in (tickers or {}).items():
                    if '/USDT:USDT' not in symbol:
                        continue
                    quote_volume = float(data.get('quoteVolume', 0.0) or 0.0)
                    if quote_volume < 15_000_000:
                        continue
                    ranked_symbols.append({
                        'symbol': symbol,
                        'quote_volume': quote_volume
                    })
                ranked_symbols.sort(key=lambda item: item.get('quote_volume', 0.0), reverse=True)
                ranked_symbols = ranked_symbols[:60]

                results_by_tf = {}
                summary = {}
                for timeframe in sorted(due_timeframes.keys(), key=lambda tf: ALT_TREND_TIMEFRAME_ORDER.get(tf, 999)):
                    closed_ts = due_timeframes[timeframe]
                    tf_result = await self._scan_alt_trend_timeframe(timeframe, closed_ts, ranked_symbols)
                    confirm_count = len(tf_result.get('confirm', []) or [])
                    setup_count = len(tf_result.get('setup', []) or [])
                    summary[timeframe] = {
                        'candle_ts': closed_ts,
                        'confirm_count': confirm_count,
                        'setup_count': setup_count,
                        'scanned_symbols': len(ranked_symbols),
                        'scanned_at': int(time.time())
                    }
                    self.last_alt_trend_scan_candle_ts_by_tf[timeframe] = closed_ts
                    if confirm_count or setup_count:
                        results_by_tf[timeframe] = tf_result

                self.last_alt_trend_scan_summary = summary

                if results_by_tf:
                    for chunk in self._build_alt_trend_alert_chunks(results_by_tf, due_timeframes.keys()):
                        await self.notify(chunk)
                    for timeframe, tf_result in results_by_tf.items():
                        for stage in ('confirm', 'setup'):
                            for item in tf_result.get(stage, []) or []:
                                self.last_alt_trend_alert_sent[(timeframe, item.get('symbol'), stage)] = int(item.get('candle_ts', 0) or 0)

                await asyncio.sleep(60)
            except Exception as e:
                logger.error(f"Alt trend alert loop error: {e}")
                await asyncio.sleep(60)

    async def run(self):
        logger.info("Bot starting... (Pure Polling Mode)")

        if self.cfg.get('logging', {}).get('debug_mode', False):
            logger.setLevel(logging.DEBUG)
            logger.info("Debug mode enabled")

        token = self.cfg.get('telegram', {}).get('token', '')
        if not token:
            logger.error("??Telegram token is missing!")
            return

        self.tg_app = ApplicationBuilder().token(token).build()
        await self._setup_telegram()

        await self._switch_engine(self.cfg.get('system_settings', {}).get('active_engine', CORE_ENGINE))

        await self.tg_app.initialize()
        await self.tg_app.start()
        await self.tg_app.updater.start_polling()
        self._write_heartbeat()

        # Startup notice + keyboard
        if self._startup_notice_enabled():
            await self.notify(self._build_startup_notice())
        else:
            logger.info("Telegram startup notice suppressed by event-only alert mode.")
        cid = self.cfg.get_chat_id()
        if cid and self._startup_keyboard_enabled():
            try:
                await self.tg_app.bot.send_message(
                    chat_id=cid,
                    text="📱 메인 메뉴를 띄웠습니다.",
                    reply_markup=self._build_main_keyboard()
                )
            except Exception as e:
                logger.warning(f"Failed to send main menu keyboard: {e}")
        else:
            logger.info("Telegram startup keyboard suppressed by event-only alert mode.")

        await asyncio.gather(
            self._main_polling_loop(),  # [?대쭅 ?꾩슜] 硫붿씤 ?대쭅 猷⑦봽
            self._hourly_report_loop(),
            self._monthly_trade_report_loop(),
            self._alt_trend_alert_loop(),
            self._heartbeat_loop()
        )

    async def _switch_engine(self, name):
        requested = (name or CORE_ENGINE).lower()
        if requested != CORE_ENGINE:
            logger.warning(f"Legacy engine request ignored in core mode: {requested} -> {CORE_ENGINE}")
            requested = CORE_ENGINE
            await self.cfg.update_value(['system_settings', 'active_engine'], CORE_ENGINE)

        if CORE_ENGINE not in self.engines:
            logger.error(f"Required engine not available: {CORE_ENGINE}")
            return

        if self.active_engine:
            shutdown_safety = getattr(self.active_engine, '_shutdown_crypto_safety_runtime', None)
            if callable(shutdown_safety):
                await shutdown_safety()
            self.active_engine.stop()

        # ?붿쭊 ?꾪솚 ???곹깭 ?곗씠??珥덇린??
        self.status_data = {}

        self.active_engine = self.engines[CORE_ENGINE]
        self.active_engine.start()

        sym = self._get_current_symbol()

        await self.active_engine.ensure_market_settings(sym)
        logger.info(f"Active engine: {CORE_ENGINE.upper()}")

    def _get_current_symbol(self):
        """?꾩옱 ?쒖꽦 ?붿쭊???щ낵 諛섑솚"""
        watchlist = self.get_active_watchlist()
        return watchlist[0] if watchlist else ('BTC/KRW' if self.is_upbit_mode() else 'BTC/USDT')

    def _utbreakout_status_symbol_key(self, symbol):
        return normalize_futures_market_id(symbol)

    async def _resolve_utbreakout_status_symbol(self):
        """Pick the symbol operators expect to inspect.

        Priority:
        1. Real live position symbol
        2. Current scanner_active_symbol
        3. Next selected CoinSelector candidate
        4. Non-stale status_data position symbol
        5. None when no live candidate exists
        """
        engine = self.engines.get(CORE_ENGINE)

        def _mark_status_symbol_source(source, detail=None):
            if engine:
                try:
                    engine.utbreakout_status_symbol_source = source
                    engine.utbreakout_status_symbol_detail = detail
                except Exception:
                    pass

        position_symbols = set()
        if engine and hasattr(engine, 'get_active_position_symbols'):
            try:
                position_symbols = await engine.get_active_position_symbols(use_cache=False)
            except Exception as exc:
                logger.warning(f"UTBreak status position symbol lookup failed: {exc}")

        if position_symbols:
            symbol = sorted(position_symbols, key=self._utbreakout_status_symbol_key)[0]
            _mark_status_symbol_source('position', 'active exchange position')
            return symbol

        scanner_symbol = getattr(engine, 'scanner_active_symbol', None) if engine else None
        if scanner_symbol:
            _mark_status_symbol_source('scanner_lock', 'current scanner_active_symbol')
            return scanner_symbol

        next_symbol = None
        try:
            if engine and hasattr(engine, "_resolve_next_utbreakout_scan_candidate"):
                next_symbol, _ = await engine._resolve_next_utbreakout_scan_candidate(
                    excluded_symbols=position_symbols
                )
            elif hasattr(self, "_resolve_next_utbreakout_scan_candidate"):
                next_symbol, _ = await self._resolve_next_utbreakout_scan_candidate(
                    excluded_symbols=position_symbols
                )
        except Exception as exc:
            logger.warning(f"UTBreak next candidate status lookup failed: {exc}")

        if next_symbol:
            _mark_status_symbol_source('live_candidate', 'selected CoinSelector candidate')
            return next_symbol

        if isinstance(self.status_data, dict):
            status_symbols = []
            now_ts = time.time()
            for key, row in self.status_data.items():
                if not isinstance(row, dict):
                    continue

                symbol = row.get('symbol') or key
                if not symbol:
                    continue

                updated_at = row.get('updated_at') or row.get('ts') or row.get('timestamp')
                try:
                    updated_float = float(updated_at or 0)
                except (TypeError, ValueError):
                    updated_float = 0.0

                if updated_float > 10_000_000_000:
                    updated_float = updated_float / 1000.0

                if updated_float and now_ts - updated_float > 300:
                    continue

                pos_side = str(row.get('pos_side') or '').upper()
                if pos_side not in {'', 'NONE', 'NO_POSITION'}:
                    status_symbols.append(symbol)

            if status_symbols:
                symbol = sorted(status_symbols, key=self._utbreakout_status_symbol_key)[0]
                _mark_status_symbol_source('status_data_position', 'recent non-flat status_data')
                return symbol

        _mark_status_symbol_source(
            'no_live_candidate',
            'no position/scanner lock/CoinSelector candidate',
        )
        return None

    async def reinit_exchange(self, target_mode):
        """거래소 연결 재초기화. 중복 전환 방지 + 실패 시 이전 상태 롤백."""
        if not hasattr(self, 'exchange_switch_lock') or self.exchange_switch_lock is None:
            self.exchange_switch_lock = asyncio.Lock()

        if self.exchange_switch_lock.locked():
            return False, "이미 거래소/네트워크 전환 중입니다. 완료될 때까지 기다려주세요."

        async with self.exchange_switch_lock:
            try:
                if isinstance(target_mode, bool):
                    exchange_mode = BINANCE_TESTNET if target_mode else BINANCE_MAINNET
                else:
                    exchange_mode = str(target_mode or '').lower()

                if exchange_mode not in SUPPORTED_EXCHANGE_MODES:
                    return False, f"지원하지 않는 거래모드: {target_mode}"

                cooldown = float(getattr(self, 'exchange_switch_cooldown_seconds', 30) or 30)
                now = time.time()
                last_ts = float(getattr(self, 'last_exchange_switch_ts', 0.0) or 0.0)
                if last_ts and (now - last_ts) < cooldown:
                    remain = int(max(1, cooldown - (now - last_ts)))
                    return False, f"거래소 전환은 너무 자주 실행할 수 없습니다. {remain}초 후 다시 시도하세요."

                self.last_exchange_switch_ts = now

                old_mode = self.get_exchange_mode()
                old_use_testnet = bool(self.cfg.get('api', {}).get('use_testnet', old_mode == BINANCE_TESTNET))
                old_exchange_mode_attr = getattr(self, 'exchange_mode', old_mode)
                old_exchange = getattr(self, 'exchange', None)
                old_market_data_exchange = getattr(self, 'market_data_exchange', None)
                old_market_data_source_label = getattr(self, 'market_data_source_label', None)
                old_active_engine = getattr(self, 'active_engine', None)
                old_status_data = dict(getattr(self, 'status_data', {}) or {})

                old_engine_state = {}
                for key, engine in (getattr(self, 'engines', {}) or {}).items():
                    state = {
                        'exchange': getattr(engine, 'exchange', None),
                        'market_data_exchange': getattr(engine, 'market_data_exchange', None),
                        'position_cache': getattr(engine, 'position_cache', None),
                        'position_cache_time': getattr(engine, 'position_cache_time', 0),
                        'all_positions_cache': getattr(engine, 'all_positions_cache', None),
                        'all_positions_cache_time': getattr(engine, 'all_positions_cache_time', 0),
                    }
                    if hasattr(engine, 'active_symbols'):
                        state['active_symbols'] = set(getattr(engine, 'active_symbols') or set())
                    if hasattr(engine, 'scanner_active_symbol'):
                        state['scanner_active_symbol'] = getattr(engine, 'scanner_active_symbol', None)
                    if hasattr(engine, 'utbreakout_daily_sl_symbol_lockouts'):
                        state['utbreakout_daily_sl_symbol_lockouts'] = dict(getattr(engine, 'utbreakout_daily_sl_symbol_lockouts', {}) or {})
                    old_engine_state[key] = state

                try:
                    if self.active_engine:
                        shutdown_safety = getattr(
                            self.active_engine,
                            '_shutdown_crypto_safety_runtime',
                            None,
                        )
                        if callable(shutdown_safety):
                            await shutdown_safety()
                        self.active_engine.stop()
                        logger.info("Engine stopped for exchange reinit")

                    await self.cfg.update_value(['api', 'exchange_mode'], exchange_mode)
                    await self.cfg.update_value(['api', 'use_testnet'], exchange_mode == BINANCE_TESTNET)

                    self.exchange_mode = exchange_mode
                    creds = self._get_exchange_credentials(exchange_mode)

                    market_data_mode = self._get_market_data_exchange_mode(exchange_mode)
                    self.exchange = self._build_exchange(creds, exchange_mode)
                    network_name = self._configure_exchange_network(self.exchange, exchange_mode)
                    self.market_data_exchange = self._build_public_market_data_exchange(market_data_mode)
                    self._configure_exchange_network(self.market_data_exchange, market_data_mode)
                    self.market_data_source_label = self._get_market_data_source_label(exchange_mode)

                    markets = await asyncio.to_thread(self.exchange.load_markets)
                    markets = markets if isinstance(markets, dict) else {}

                    sanitize_result = await self._sanitize_watchlist_for_exchange_mode(
                        exchange_mode,
                        markets=markets,
                        persist=True,
                        reason="exchange reinit",
                    )

                    for engine in self.engines.values():
                        engine.exchange = self.exchange
                        engine.market_data_exchange = self.market_data_exchange
                        engine.position_cache = None
                        engine.position_cache_time = 0
                        engine.all_positions_cache = None
                        engine.all_positions_cache_time = 0
                        if hasattr(engine, 'active_symbols'):
                            engine.active_symbols = set(sanitize_result.get('watchlist') or [])
                        if hasattr(engine, 'scanner_active_symbol'):
                            engine.scanner_active_symbol = None

                    eng_name = self.cfg.get('system_settings', {}).get('active_engine', CORE_ENGINE)
                    await self._switch_engine(eng_name)

                    logger.info(f"Exchange reinitialized: {network_name}")
                    return True, {
                        'mode': exchange_mode,
                        'network_name': network_name,
                        'sanitize': sanitize_result,
                    }

                except Exception as e:
                    logger.exception(f"Exchange reinit failed, rolling back to previous mode: {e}")

                    try:
                        await self.cfg.update_value(['api', 'exchange_mode'], old_mode)
                        await self.cfg.update_value(['api', 'use_testnet'], old_use_testnet)
                    except Exception as cfg_rollback_error:
                        logger.error(f"Exchange reinit config rollback failed: {cfg_rollback_error}")

                    self.exchange_mode = old_exchange_mode_attr
                    self.exchange = old_exchange
                    self.market_data_exchange = old_market_data_exchange
                    self.market_data_source_label = old_market_data_source_label
                    self.active_engine = old_active_engine
                    self.status_data = old_status_data

                    for key, state in old_engine_state.items():
                        engine = self.engines.get(key)
                        if not engine:
                            continue
                        engine.exchange = state.get('exchange')
                        engine.market_data_exchange = state.get('market_data_exchange')
                        engine.position_cache = state.get('position_cache')
                        engine.position_cache_time = state.get('position_cache_time', 0)
                        engine.all_positions_cache = state.get('all_positions_cache')
                        engine.all_positions_cache_time = state.get('all_positions_cache_time', 0)
                        if hasattr(engine, 'active_symbols') and 'active_symbols' in state:
                            engine.active_symbols = set(state.get('active_symbols') or set())
                        if hasattr(engine, 'scanner_active_symbol') and 'scanner_active_symbol' in state:
                            engine.scanner_active_symbol = state.get('scanner_active_symbol')
                        if hasattr(engine, 'utbreakout_daily_sl_symbol_lockouts') and 'utbreakout_daily_sl_symbol_lockouts' in state:
                            engine.utbreakout_daily_sl_symbol_lockouts = dict(state.get('utbreakout_daily_sl_symbol_lockouts', {}) or {})

                    try:
                        if self.active_engine:
                            shutdown_safety = getattr(
                                self.active_engine,
                                '_shutdown_crypto_safety_runtime',
                                None,
                            )
                            if callable(shutdown_safety):
                                await shutdown_safety()
                            if getattr(self.active_engine, 'running', False):
                                self.active_engine.stop()
                            self.active_engine.start()
                            logger.info("Previous engine restarted after exchange reinit rollback")
                    except Exception as engine_rollback_error:
                        logger.error(f"Previous engine restart after rollback failed: {engine_rollback_error}")

                    return False, f"{e} (이전 거래소 설정으로 롤백했습니다)"

            except Exception as outer_error:
                logger.exception(f"Exchange reinit outer error: {outer_error}")
                return False, str(outer_error)
