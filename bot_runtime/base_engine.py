"""Shared exchange, position, balance, and execution engine services."""

from __future__ import annotations

class BaseEngine:
    def __init__(self, controller):
        self.ctrl = controller
        self.cfg = controller.cfg
        self.db = controller.db
        self.exchange = controller.exchange
        self.market_data_exchange = getattr(controller, 'market_data_exchange', self.exchange)
        self.running = False
        self.position_cache = None
        self.position_cache_time = 0
        self.POSITION_CACHE_TTL = 2.0  # 2珥?罹먯떆
        self.all_positions_cache = None
        self.all_positions_cache_time = 0
        self.ALL_POSITIONS_CACHE_TTL = 15.0

    def start(self):
        self.running = True
        logger.info(f"?? {self.__class__.__name__} started")

    def stop(self):
        self.running = False
        logger.info(f"??{self.__class__.__name__} stopped")

    @property
    def crypto_execution(self):
        service = getattr(self.ctrl, 'crypto_execution_service', None)
        if service is None:
            gateway = _ensure_trading_safety_runtime(self)
            service = getattr(self.ctrl, 'crypto_execution_service', None)
            if service is None:
                service = CryptoExecutionService(
                    self.exchange,
                    gateway.store,
                    gateway,
                    BinanceAlgoOrderGateway(self.exchange),
                    accounting_service=TradeAccountingFinalizer(self.exchange, gateway.store),
                )
                self.ctrl.crypto_execution_service = service
        return service

    async def submit_futures_entry(self, **kwargs):
        if self.is_upbit_mode():
            raise RuntimeError("Futures execution service cannot route Upbit spot orders")
        return await self.crypto_execution.submit_entry(**kwargs)

    async def submit_futures_close(self, **kwargs):
        if self.is_upbit_mode():
            raise RuntimeError("Futures execution service cannot route Upbit spot orders")
        return await self.crypto_execution.submit_reduce_only_close(**kwargs)

    async def place_futures_protection(self, kind, **kwargs):
        if self.is_upbit_mode():
            raise RuntimeError("Futures execution service cannot route Upbit spot protection")
        target = self
        if not hasattr(target, '_create_protection_order_with_retries'):
            target = getattr(self.ctrl, 'engines', {}).get('signal')
        if target is None or not hasattr(target, '_create_protection_order_with_retries'):
            raise RuntimeError("CRYPTO_PROTECTION_MANAGER_UNAVAILABLE")
        service = self.crypto_execution

        async def protection_manager(protection_kind, **payload):
            symbol = payload['symbol']
            side = payload['side']
            qty = payload['qty']
            trigger_price = payload.get('trigger_price')
            order_type = payload.get('order_type') or (
                'stop_market' if protection_kind == 'sl' else 'take_profit_market'
            )
            params = dict(payload.get('params') or {})
            params['reduceOnly'] = True
            if trigger_price is not None:
                params['stopPrice'] = trigger_price
            pos = payload.get('position')
            if protection_kind == 'sl' and hasattr(target, '_verify_actual_liquidation_safety'):
                position_side = 'long' if str(side).lower() == 'sell' else 'short'
                safety = await target._verify_actual_liquidation_safety(
                    symbol,
                    position_side,
                    trigger_price,
                    pos,
                    payload.get('config') or {},
                    payload.get('entry_client_order_id'),
                )
                if not safety.get('valid'):
                    raise RuntimeError(
                        f"SL liquidation safety failed: {safety.get('status')}"
                    )
                pos = safety.get('position') or pos
            if 'newClientOrderId' not in params and hasattr(target, '_build_protection_client_order_id'):
                params['newClientOrderId'] = target._build_protection_client_order_id(
                    symbol,
                    'long' if str(side).lower() == 'sell' else 'short',
                    protection_kind,
                    pos,
                    trigger_price=trigger_price or payload.get('price'),
                    quantity=qty,
                    leg=payload.get('label') or protection_kind,
                    position_identity=payload.get('entry_client_order_id'),
                )
            return await target._create_protection_order_with_retries(
                symbol,
                order_type,
                side,
                qty,
                payload.get('price'),
                params,
                payload.get('label') or protection_kind.upper(),
            )

        service.protection_manager = protection_manager
        if str(kind).lower() == 'sl':
            return await service.place_stop_loss(**kwargs)
        if str(kind).lower() == 'tp':
            return await service.place_take_profit(**kwargs)
        return await service.cancel_protection(**kwargs)

    async def on_tick(self, data_type, data):
        pass

    def is_upbit_mode(self):
        try:
            return self.ctrl.is_upbit_mode()
        except AttributeError:
            return getattr(self, 'upbit_mode', False)

    def get_runtime_trade_config(self):
        try:
            return self.ctrl.get_active_trade_config()
        except AttributeError:
            return getattr(self, 'trade_config', {})

    def get_runtime_common_settings(self):
        try:
            return self.ctrl.get_active_common_settings()
        except AttributeError:
            return getattr(self, 'common_settings', {})

    def get_runtime_strategy_params(self):
        try:
            return self.ctrl.get_active_strategy_params()
        except AttributeError:
            return getattr(self, 'strategy_params', {})

    def get_runtime_watchlist(self):
        try:
            return self.ctrl.get_active_watchlist()
        except AttributeError:
            return getattr(self, 'watchlist', [])

    def get_quote_currency(self):
        return 'KRW' if self.is_upbit_mode() else 'USDT'

    def get_effective_trade_direction(self):
        try:
            return self.ctrl.get_effective_trade_direction()
        except AttributeError:
            return getattr(self, 'trade_direction', 'both')

    def is_trade_direction_allowed(self, side):
        side = str(side or '').strip().lower()
        if side not in {'long', 'short'}:
            return True
        direction = self.get_effective_trade_direction()
        return not (
            (direction == 'long' and side == 'short') or
            (direction == 'short' and side == 'long')
        )

    def format_trade_direction_block_reason(self, side):
        return f"방향 필터 차단 ({str(side or '').upper()} vs {self.get_effective_trade_direction().upper()})"

    def _to_float_safe(self, value):
        try:
            if value in (None, ''):
                return 0.0
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def _get_upbit_min_order_krw(self):
        cfg = self.get_runtime_common_settings()
        return max(5000.0, float(cfg.get('min_order_krw', 5000.0) or 5000.0))

    def _extract_upbit_assets(self, balance_payload):
        assets = {}
        if not isinstance(balance_payload, dict):
            return assets

        info_rows = balance_payload.get('info', [])
        if isinstance(info_rows, list):
            for row in info_rows:
                if not isinstance(row, dict):
                    continue
                code = str(row.get('currency', '')).upper().strip()
                if not code:
                    continue
                free_amt = self._to_float_safe(row.get('balance'))
                locked_amt = self._to_float_safe(row.get('locked'))
                assets[code] = {
                    'currency': code,
                    'free': free_amt,
                    'locked': locked_amt,
                    'total': free_amt + locked_amt,
                    'avg_buy_price': self._to_float_safe(row.get('avg_buy_price')),
                    'unit_currency': str(row.get('unit_currency', '')).upper().strip(),
                }

        if assets:
            return assets

        total_map = balance_payload.get('total', {})
        free_map = balance_payload.get('free', {})
        used_map = balance_payload.get('used', {})
        if isinstance(total_map, dict):
            for code_raw, total_val in total_map.items():
                code = str(code_raw or '').upper().strip()
                if not code:
                    continue
                free_amt = self._to_float_safe(free_map.get(code))
                locked_amt = self._to_float_safe(used_map.get(code))
                total_amt = self._to_float_safe(total_val)
                if total_amt <= 0:
                    total_amt = free_amt + locked_amt
                assets[code] = {
                    'currency': code,
                    'free': free_amt,
                    'locked': locked_amt,
                    'total': total_amt,
                    'avg_buy_price': 0.0,
                    'unit_currency': 'KRW',
                }

        return assets

    def safe_amount(self, symbol, amount):
        try:
            return self.exchange.amount_to_precision(symbol, amount)
        except Exception as e:
            logger.error(f"Amount precision error: {e}")
            return str(round(amount, 6))

    def safe_price(self, symbol, price):
        try:
            return self.exchange.price_to_precision(symbol, price)
        except Exception as e:
            logger.error(f"Price precision error: {e}")
            return str(round(price, 2))

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
        return text

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

    def _position_symbol_for_match(self, position):
        if not isinstance(position, dict):
            return ''
        info = position.get('info', {}) if isinstance(position.get('info'), dict) else {}
        return str(position.get('symbol') or info.get('symbol') or info.get('pair') or '').strip()

    def _position_matches_symbol(self, position, symbol):
        position_key = self._futures_symbol_key(self._position_symbol_for_match(position))
        target_key = self._futures_symbol_key(symbol)
        target_order_key = self._futures_symbol_key(self._futures_symbol_for_order(symbol))
        return bool(position_key and position_key in {target_key, target_order_key})

    def _normalize_server_position(self, position, symbol=None):
        if not isinstance(position, dict):
            return None
        if symbol and not self._position_matches_symbol(position, symbol):
            return None
        signed_contracts = self._position_signed_contracts(position)
        contracts = abs(float(signed_contracts or 0.0))
        if contracts <= 0:
            return None
        side = self._position_side_for_close(position, signed_contracts)
        if side not in {'long', 'short'}:
            return None

        info = position.get('info', {}) if isinstance(position.get('info'), dict) else {}
        normalized = dict(position)
        normalized['symbol'] = (
            self._futures_symbol_for_order(self._position_symbol_for_match(position))
            or self._futures_symbol_for_order(symbol)
            or position.get('symbol')
        )
        normalized['side'] = side
        normalized['contracts'] = contracts
        for field, keys in {
            'entryPrice': ('entryPrice', 'entry_price', 'entry_price', 'entryPrice'),
            'markPrice': ('markPrice', 'mark_price', 'markPrice'),
            'liquidationPrice': ('liquidationPrice', 'liquidation_price', 'liquidationPrice'),
            'leverage': ('leverage',),
            'isolatedMargin': ('isolatedMargin', 'isolated_margin'),
            'unrealizedPnl': ('unrealizedPnl', 'unRealizedProfit', 'unrealizedProfit'),
            'percentage': ('percentage', 'unrealizedPnlPercent'),
        }.items():
            if normalized.get(field) not in (None, ''):
                continue
            value = self._position_numeric_value(position, keys)
            if value is not None:
                normalized[field] = value
        normalized['raw_position'] = position
        normalized['positionSide'] = str(info.get('positionSide') or position.get('positionSide') or '').upper()
        normalized['marginType'] = str(
            position.get('marginType')
            or position.get('marginMode')
            or info.get('marginType')
            or info.get('marginMode')
            or ''
        ).lower()
        return normalized

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

    async def ensure_market_settings(self, symbol, leverage=None):
        """留덉폆 ?ㅼ젙 媛뺤젣 ?곸슜 (寃⑸━ 紐⑤뱶 + ?덈쾭由ъ?)"""
        if self.is_upbit_mode():
            logger.info(f"{symbol} Settings: UPBIT KRW spot / leverage fixed at 1x")
            return

        # CCXT's symbol-scoped Binance setting methods require market metadata.
        # Startup reaches this method before the scanner has loaded markets, so
        # preload them once instead of producing a false settings failure.
        if not getattr(self.exchange, 'markets', None):
            load_markets = getattr(self.exchange, 'load_markets', None)
            if callable(load_markets):
                try:
                    await asyncio.to_thread(load_markets)
                except Exception as e:
                    logger.warning("Market metadata preload failed for %s: %s", symbol, e)

        # 1. Position Mode: One-way (Hedge Mode OFF)
        try:
            await asyncio.to_thread(self.exchange.set_position_mode, hedged=False, symbol=symbol)
        except Exception as e:
            logger.warning("Position mode verification/setup failed for %s: %s", symbol, e)

        # 2. Margin Mode: ISOLATED (媛뺤젣)
        try:
            await asyncio.to_thread(self.exchange.set_margin_mode, 'ISOLATED', symbol)
        except Exception as e:
            logger.warning("Isolated margin setup failed for %s: %s", symbol, e)

            # 3. Leverage Setting
        try:
            # ?몄옄濡??꾨떖???덈쾭由ъ?媛 ?놁쑝硫??ㅼ젙?먯꽌 議고쉶
            if leverage is None:
                eng = self.cfg.get('system_settings', {}).get('active_engine', CORE_ENGINE)
                if eng == 'shannon':
                    leverage = self.cfg.get('shannon_engine', {}).get('leverage', 5)
                elif eng == 'dualthrust':
                    leverage = self.cfg.get('dual_thrust_engine', {}).get('leverage', 5)
                elif eng == 'dualmode':
                    leverage = self.cfg.get('dual_mode_engine', {}).get('leverage', 5)
                else:
                    leverage = self.get_runtime_common_settings().get('leverage', 20)

            await asyncio.to_thread(self.exchange.set_leverage, leverage, symbol)
            logger.info(f"??{symbol} Settings: ISOLATED / {leverage}x")
        except Exception as e:
            logger.error(f"Leverage setting error: {e}")

    async def _fetch_server_position_checked(self, symbol):
        """Return (fetch_ok, position) without treating an API error as flat."""
        now = time.time()
        if not isinstance(self.position_cache, dict):
            self.position_cache = {}

        try:
            if self.is_upbit_mode():
                bal = await asyncio.to_thread(self.exchange.fetch_balance)
                assets = self._extract_upbit_assets(bal)
                base = str(symbol).split('/')[0].upper()
                asset_info = assets.get(base, {})
                contracts = float(asset_info.get('total', 0) or 0)
                if contracts <= 0:
                    self.position_cache[symbol] = (None, now)
                    return True, None

                entry_price = float(asset_info.get('avg_buy_price') or 0.0)
                current_price = 0.0
                try:
                    ticker = await asyncio.to_thread(self.market_data_exchange.fetch_ticker, symbol)
                    current_price = float(ticker.get('last') or ticker.get('close') or 0.0)
                except Exception as ticker_e:
                    logger.warning(f"Upbit ticker fetch error for {symbol}: {ticker_e}")

                pnl = 0.0
                pnl_pct = 0.0
                if entry_price > 0 and current_price > 0:
                    pnl = (current_price - entry_price) * contracts
                    pnl_pct = ((current_price / entry_price) - 1.0) * 100.0
                found_pos = {
                    'symbol': symbol,
                    'side': 'long',
                    'contracts': contracts,
                    'entryPrice': entry_price,
                    'markPrice': current_price,
                    'unrealizedPnl': pnl,
                    'percentage': pnl_pct
                }
                self.position_cache[symbol] = (found_pos, now)
                return True, found_pos

            try:
                positions = await asyncio.to_thread(self.exchange.fetch_positions, [symbol])
            except Exception as scoped_error:
                logger.debug(f"Scoped position fetch failed for {symbol}: {scoped_error}")
                positions = await asyncio.to_thread(self.exchange.fetch_positions)

            found_pos = None
            for position in positions:
                normalized_pos = self._normalize_server_position(position, symbol)
                if normalized_pos:
                    found_pos = normalized_pos
                    break
            self.position_cache[symbol] = (found_pos, now)
            return True, found_pos
        except Exception as exc:
            logger.error(f"Position fetch error for {symbol}: {exc}")
            return False, None

    async def get_server_position(self, symbol, use_cache=True):
        """?ъ???議고쉶 (?щ낵蹂?罹먯떆 ?곸슜)"""
        now = time.time()

        # [Fix] Lazy Init for Dictionary Cache (Backward Compatibility)
        if not isinstance(self.position_cache, dict):
            self.position_cache = {}

        # Check Cache
        if use_cache:
            cache_entry = self.position_cache.get(symbol)
            if cache_entry:
                cached_pos, cached_ts = cache_entry
                if (now - cached_ts) < self.POSITION_CACHE_TTL:
                    return cached_pos

        try:
            if self.is_upbit_mode():
                bal = await asyncio.to_thread(self.exchange.fetch_balance)
                assets = self._extract_upbit_assets(bal)
                base = str(symbol).split('/')[0].upper()
                asset_info = assets.get(base, {})
                contracts = float(asset_info.get('total', 0) or 0)
                if contracts <= 0:
                    self.position_cache[symbol] = (None, now)
                    return None

                entry_price = float(asset_info.get('avg_buy_price') or 0.0)
                current_price = 0.0
                try:
                    ticker = await asyncio.to_thread(self.market_data_exchange.fetch_ticker, symbol)
                    current_price = float(ticker.get('last') or ticker.get('close') or 0.0)
                except Exception as ticker_e:
                    logger.warning(f"Upbit ticker fetch error for {symbol}: {ticker_e}")

                pnl = 0.0
                pnl_pct = 0.0
                if entry_price > 0 and current_price > 0:
                    pnl = (current_price - entry_price) * contracts
                    pnl_pct = ((current_price / entry_price) - 1.0) * 100.0

                found_pos = {
                    'symbol': symbol,
                    'side': 'long',
                    'contracts': contracts,
                    'entryPrice': entry_price,
                    'markPrice': current_price,
                    'unrealizedPnl': pnl,
                    'percentage': pnl_pct
                }
                self.position_cache[symbol] = (found_pos, now)
                return found_pos

            try:
                positions = await asyncio.to_thread(self.exchange.fetch_positions, [symbol])
            except Exception as scoped_error:
                logger.debug(f"Scoped position fetch failed for {symbol}: {scoped_error}")
                positions = await asyncio.to_thread(self.exchange.fetch_positions)

            found_pos = None
            for p in positions:
                normalized_pos = self._normalize_server_position(p, symbol)
                if normalized_pos:
                    found_pos = normalized_pos
                    logger.debug(
                        f"Position found: {normalized_pos['symbol']} "
                        f"side={normalized_pos['side']} contracts={normalized_pos['contracts']}"
                    )
                    break

            # Update Cache (Key by Symbol)
            self.position_cache[symbol] = (found_pos, now)
            return found_pos

        except Exception as e:
            logger.error(f"Position fetch error: {e}")
            # ?먮윭 ??罹먯떆媛 ?덉쑝硫?諛섑솚, ?놁쑝硫?None
            cache_entry = self.position_cache.get(symbol)
            if cache_entry:
                return cache_entry[0]
            return None

    async def get_active_position_symbols(self, use_cache=True):
        now = time.time()

        if use_cache and isinstance(self.all_positions_cache, set):
            if (now - self.all_positions_cache_time) < self.ALL_POSITIONS_CACHE_TTL:
                return set(self.all_positions_cache)

        try:
            if self.is_upbit_mode():
                bal = await asyncio.to_thread(self.exchange.fetch_balance)
                assets = self._extract_upbit_assets(bal)
                active_symbols = set()
                min_order_krw = self._get_upbit_min_order_krw()
                for code, asset in assets.items():
                    if code == 'KRW':
                        continue
                    qty = float(asset.get('total', 0) or 0)
                    if qty <= 0:
                        continue
                    est_value = qty * float(asset.get('avg_buy_price', 0) or 0)
                    if est_value > 0 and est_value < min_order_krw:
                        continue
                    active_symbols.add(f"{code}/KRW")
                self.all_positions_cache = set(active_symbols)
                self.all_positions_cache_time = now
                return active_symbols

            positions = await asyncio.to_thread(self.exchange.fetch_positions)
            active_symbols = set()
            for p in positions:
                normalized_pos = self._normalize_server_position(p)
                if normalized_pos:
                    sym = str(normalized_pos.get('symbol', '')).split(':', 1)[0]
                    if sym:
                        active_symbols.add(sym)
            self.all_positions_cache = set(active_symbols)
            self.all_positions_cache_time = now
            return active_symbols
        except Exception as e:
            logger.warning(f"Active positions fetch error: {e}")
            if isinstance(self.all_positions_cache, set):
                return set(self.all_positions_cache)
            return set()

    async def get_balance_info(self):
        try:
            if self.is_upbit_mode():
                bal = await asyncio.to_thread(self.exchange.fetch_balance)
                assets = self._extract_upbit_assets(bal)
                krw_asset = assets.get('KRW', {})
                total_krw = self._to_float_safe(krw_asset.get('total'))
                free_krw = self._to_float_safe(krw_asset.get('free'))

                total_equity = total_krw
                for code, asset in assets.items():
                    if code == 'KRW':
                        continue
                    qty = self._to_float_safe(asset.get('total'))
                    if qty <= 0:
                        continue
                    try:
                        ticker = await asyncio.to_thread(self.market_data_exchange.fetch_ticker, f"{code}/KRW")
                        last_price = self._to_float_safe(ticker.get('last') or ticker.get('close'))
                        if last_price > 0:
                            total_equity += qty * last_price
                            continue
                    except Exception as ticker_e:
                        logger.debug(f"Upbit balance valuation skipped for {code}/KRW: {ticker_e}")

                    fallback_price = self._to_float_safe(asset.get('avg_buy_price'))
                    if fallback_price > 0:
                        total_equity += qty * fallback_price

                if total_equity <= 0 and free_krw > 0:
                    total_equity = free_krw

                return float(total_equity), float(free_krw), 0.0

            # Binance futures balance schemas vary by account mode (single/multi asset, PM, etc).
            # Parse multiple candidate fields to avoid showing 0 when funds exist.
            def parse_futures_balance_payload(balance_payload):
                info = balance_payload.get('info', {}) if isinstance(balance_payload, dict) else {}
                usdt_bucket = balance_payload.get('USDT', {}) if isinstance(balance_payload, dict) else {}
                total_map = balance_payload.get('total', {}) if isinstance(balance_payload, dict) else {}
                free_map = balance_payload.get('free', {}) if isinstance(balance_payload, dict) else {}

                usdt_asset = {}
                assets = info.get('assets', []) if isinstance(info, dict) else []
                if isinstance(assets, list):
                    for asset in assets:
                        if str(asset.get('asset', '')).upper() == 'USDT':
                            usdt_asset = asset
                            break

                def to_float_or_none(v):
                    try:
                        if v is None or v == '':
                            return None
                        return float(v)
                    except (TypeError, ValueError):
                        return None

                def pick_value(candidates):
                    parsed = [to_float_or_none(v) for v in candidates]
                    for v in parsed:
                        if v is not None and v > 0:
                            return v
                    for v in parsed:
                        if v is not None:
                            return v
                    return 0.0

                total_value = pick_value([
                    info.get('totalMarginBalance'),
                    info.get('totalWalletBalance'),
                    info.get('totalCrossWalletBalance'),
                    usdt_asset.get('marginBalance'),
                    usdt_asset.get('walletBalance'),
                    usdt_bucket.get('total'),
                    total_map.get('USDT'),
                    info.get('availableBalance'),
                    usdt_asset.get('availableBalance'),
                    usdt_bucket.get('free'),
                    free_map.get('USDT'),
                ])
                free_value = pick_value([
                    info.get('availableBalance'),
                    usdt_asset.get('availableBalance'),
                    usdt_bucket.get('free'),
                    free_map.get('USDT'),
                    usdt_bucket.get('total'),
                    total_map.get('USDT'),
                ])
                if total_value <= 0 and free_value > 0:
                    total_value = free_value
                maint_margin = pick_value([
                    info.get('totalMaintMargin'),
                    usdt_asset.get('maintMargin'),
                ])
                mmr_value = (maint_margin / total_value * 100) if total_value > 0 else 0.0
                return float(total_value), float(free_value), float(mmr_value)

            async def fetch_futures_balance_payload():
                try:
                    return await asyncio.to_thread(self.exchange.fetch_balance, {'type': 'future'})
                except Exception as primary_exc:
                    logger.warning(f"Futures balance fetch with type=future failed: {primary_exc}")
                    return await asyncio.to_thread(self.exchange.fetch_balance)

            bal = await fetch_futures_balance_payload()
            total, free, mmr = parse_futures_balance_payload(bal)
            if total <= 0 and free <= 0:
                try:
                    mode = self.get_exchange_mode()
                    creds = self._get_exchange_credentials(mode)
                    logger.warning("Futures balance parsed as zero; rebuilding exchange and retrying balance fetch")
                    self.exchange = self._build_exchange(creds, mode)
                    self._configure_exchange_network(self.exchange, mode)
                    bal = await fetch_futures_balance_payload()
                    total, free, mmr = parse_futures_balance_payload(bal)
                except Exception as retry_e:
                    logger.error(f"Futures balance retry after exchange refresh failed: {retry_e}")

            return float(total), float(free), float(mmr)
        except Exception as e:
            logger.error(f"Balance fetch error: {e}")
            return 0.0, 0.0, 0.0

    async def check_daily_loss_limit(self):
        """?쇱씪 ?먯떎 ?쒕룄 泥댄겕 (誘몄떎???먯씡 ?ы븿)"""
        _, daily_pnl = self.db.get_daily_stats()
        eng = self.cfg.get('system_settings', {}).get('active_engine', CORE_ENGINE)

        # 誘몄떎???먯씡???ы븿 (硫???щ낵 ?곹깭 ?곗씠??吏??
        unrealized_pnl = 0.0
        open_symbols = []
        status_data = self.ctrl.status_data if isinstance(self.ctrl.status_data, dict) else {}

        status_rows = []
        if status_data.get('symbol') and status_data.get('pos_side') is not None:
            # Legacy single-symbol format
            status_rows = [status_data]
        else:
            status_rows = [v for v in status_data.values() if isinstance(v, dict)]

        active_symbols_on_exchange = set()
        try:
            active_symbols_on_exchange = await self.get_active_position_symbols(use_cache=True)
        except Exception as e:
            logger.warning(f"Daily loss check: fetch_positions failed, using status cache only ({e})")

        for row in status_rows:
            pos_side = str(row.get('pos_side', 'NONE')).upper()
            symbol = row.get('symbol')
            if symbol and pos_side != 'NONE':
                norm_symbol = str(symbol).split(':', 1)[0]
                if active_symbols_on_exchange and norm_symbol not in active_symbols_on_exchange:
                    continue
                unrealized_pnl += float(row.get('pnl_usdt', 0) or 0)
                open_symbols.append(symbol)

        total_daily_pnl = daily_pnl + unrealized_pnl
        total_equity = 0.0
        if status_rows:
            equities = [float(r.get('total_equity', 0) or 0) for r in status_rows]
            total_equity = max(equities) if equities else 0.0
        if total_equity <= 0:
            total_equity, _, _ = await self.get_balance_info()

        if eng == 'shannon':
            sh_cfg = self.cfg.get('shannon_engine', {})
            limit_abs = float(sh_cfg.get('daily_loss_limit', 5000) or 5000)
            limit_pct = float(sh_cfg.get('daily_loss_limit_pct', 0) or 0)
        else:
            sig_cfg = self.get_runtime_common_settings()
            limit_abs = float(sig_cfg.get('daily_loss_limit', 5000) or 5000)
            limit_pct = float(sig_cfg.get('daily_loss_limit_pct', 0) or 0)

        # Telegram setup currently exposes absolute daily limit, so prioritize absolute limit.
        if limit_abs > 0:
            effective_limit = limit_abs
        elif limit_pct > 0 and total_equity > 0:
            effective_limit = total_equity * (limit_pct / 100.0)
        else:
            effective_limit = 5000.0

        if total_daily_pnl < -effective_limit:
            logger.warning(
                f"?좑툘 Daily loss limit reached: {total_daily_pnl:.2f} "
                f"(realized: {daily_pnl:.2f}, unrealized: {unrealized_pnl:.2f}) / "
                f"Limit: -{effective_limit:.2f} (abs={limit_abs:.2f}, pct={limit_pct:.2f}%)"
            )
            # ?ъ??섏씠 ?덉쑝硫?泥?궛
            if open_symbols and hasattr(self, 'exit_position'):
                await self.ctrl.notify("⚠️ 일일 손실 한도 도달! 보유 포지션 정리를 시작합니다.")
                for symbol in sorted(set(open_symbols)):
                    try:
                        await self.exit_position(symbol, "DailyLossLimit")
                    except Exception as e:
                        logger.error(f"Daily loss limit forced exit failed for {symbol}: {e}")
            return True
        return False

    async def check_mmr_alert(self, mmr):
        """MMR 寃쎄퀬 泥댄겕 (荑⑤떎???곸슜)"""
        max_mmr = self.cfg.get('shannon_engine', {}).get('risk_monitor', {}).get('max_mmr_alert_pct', 25.0)

        # 荑⑤떎?? 5遺??숈븞 以묐났 ?뚮┝ 諛⑹?
        now = time.time()
        if not hasattr(self, '_last_mmr_alert_time'):
            self._last_mmr_alert_time = 0

        if mmr >= max_mmr:
            if now - self._last_mmr_alert_time > 300:  # 5遺?
                self._last_mmr_alert_time = now
                await self.ctrl.notify(f"⚠️ **MMR 경고!** 현재 {mmr:.2f}% (한도: {max_mmr}%)")
            return True
        return False

__all__ = (
    'BaseEngine',
)
