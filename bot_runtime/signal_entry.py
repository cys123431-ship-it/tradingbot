"""Futures and spot position-entry orchestration."""

from __future__ import annotations


class SignalEntryMixin:
    async def entry(self, symbol, side, price):
        try:
            if (
                hasattr(self, 'is_user_custom_entry_mode_enabled')
                and self.is_user_custom_entry_mode_enabled()
            ):
                reason = (
                    'USER_CUSTOM_MODE_ACTIVE: automatic strategy entry is suspended'
                )
                if not isinstance(getattr(self, 'last_entry_reason', None), dict):
                    self.last_entry_reason = {}
                self.last_entry_reason[str(symbol)] = reason
                logger.info(
                    'Automatic strategy entry skipped while USER_CUSTOM mode is active: '
                    'symbol=%s side=%s',
                    symbol,
                    side,
                )
                return
            trace_strategy_params = self.get_runtime_strategy_params()
            trace_active_strategy = str(
                trace_strategy_params.get('active_strategy', '') or ''
            ).lower()
            trace_utbreakout = trace_active_strategy in UTBREAKOUT_STRATEGIES
            if trace_utbreakout:
                symbol = self._canonicalize_utbreakout_symbol_for_use(
                    symbol,
                    source='entry',
                )
            raw_symbol = symbol
            if trace_utbreakout:
                ok_market, symbol, validation_reason = (
                    self._ensure_valid_utbreakout_market_symbol(
                        symbol,
                        source='entry_guard',
                    )
                )
            else:
                ok_market = True
                validation_reason = ''
            if not ok_market:
                restricted_symbol = validation_reason.startswith(
                    'REJECTED_REGION_RESTRICTED_SYMBOL:'
                )
                self._utbreakout_trace_event(
                    symbol,
                    'ENTRY_BLOCKED',
                    'BLOCK' if restricted_symbol else 'INVALID_MARKET',
                    side=side,
                    reason=validation_reason,
                )
                self.last_entry_reason[symbol] = validation_reason
                try:
                    if restricted_symbol:
                        await self.ctrl.notify(
                            "🔴 UTBreakout 진입 차단: 한국계정 제한 티커\n"
                            f"symbol={symbol}\n"
                            f"reason={validation_reason}"
                        )
                    else:
                        await self.ctrl.notify(
                            "🔴 UTBreakout 진입 차단: 유효하지 않은 "
                            "Binance Futures 심볼\n"
                            f"symbol={symbol}\n"
                            f"reason={validation_reason}"
                    )
                except Exception:
                    logger.debug(
                        "invalid UTBreakout symbol notify skipped",
                        exc_info=True,
                    )
                return
            if trace_utbreakout:
                daily_sl_locked, daily_sl_reason = self._is_utbreakout_daily_sl_locked(symbol)
                if daily_sl_locked:
                    self._utbreakout_trace_event(
                        symbol,
                        'ENTRY_BLOCKED',
                        'DAILY_SL_LOCKOUT',
                        side=side,
                        reason=daily_sl_reason,
                    )
                    self.last_entry_reason[symbol] = (
                        daily_sl_reason or "daily SL lockout active"
                    )
                    try:
                        await self.ctrl.notify(
                            f"UTBreakout entry blocked: {symbol} {side.upper()} "
                            f"/ {daily_sl_reason}"
                        )
                    except Exception:
                        logger.debug(
                            "daily SL lockout entry notify skipped",
                            exc_info=True,
                        )
                    return
            raw_symbol = symbol
            if trace_utbreakout:
                self._utbreakout_trace_event(
                    raw_symbol,
                    'ENTRY_ENTERED',
                    'START',
                    side=side,
                    price=price,
                    active_strategy=trace_active_strategy,
                )
            cfg = self.get_runtime_common_settings()
            cfg = apply_runtime_safety_defaults(cfg)
            try:
                cfg = enforce_activation_stage(cfg)
            except Exception as stage_err:
                if trace_utbreakout:
                    self._utbreakout_trace_event(
                        raw_symbol,
                        'ENTRY_BLOCKED',
                        'ACTIVATION_STAGE',
                        side=side,
                        reason=str(stage_err),
                    )
                logger.error(f"Error enforcing activation stage: {stage_err}")
                self.last_entry_reason[symbol] = f"Activation stage blocked: {stage_err}"
                await self.ctrl.notify(f"⚠️ **진입 차단**: activation stage 오류 ({stage_err})")
                return

            try:
                symbol = await self.ctrl._assert_symbol_tradeable_in_current_exchange_mode(symbol)
                if hasattr(self, '_assert_automatic_entry_scan_scope'):
                    symbol = await self._assert_automatic_entry_scan_scope(symbol)
                if trace_utbreakout:
                    symbol = self._canonicalize_utbreakout_symbol_for_use(
                        symbol,
                        source='entry_preflight',
                    )
            except Exception as symbol_err:
                if trace_utbreakout:
                    self._utbreakout_trace_event(
                        raw_symbol,
                        'ENTRY_BLOCKED',
                        'SYMBOL_PREFLIGHT',
                        side=side,
                        reason=str(symbol_err),
                    )
                logger.error(f"Exchange-mode symbol preflight blocked entry: {raw_symbol} ({symbol_err})")
                self.last_entry_reason[raw_symbol] = f"Exchange-mode symbol blocked: {symbol_err}"
                await self.ctrl.notify(
                    f"⛔ 주문 차단: 현재 거래소 모드에서 사용할 수 없는 심볼입니다. "
                    f"{raw_symbol} / {symbol_err}"
                )
                return

            automatic_daily_limit = (
                int(self.get_effective_automatic_daily_trade_limit())
                if hasattr(self, 'get_effective_automatic_daily_trade_limit')
                else 5
            )
            try:
                automatic_daily_entries = int(
                    self.get_automatic_daily_entry_count()
                    if hasattr(self, 'get_automatic_daily_entry_count')
                    else 0
                )
            except Exception as daily_count_error:
                logger.error(
                    "Automatic daily entry count check failed: %s",
                    daily_count_error,
                )
                self.last_entry_reason[symbol] = (
                    "AUTOMATIC_DAILY_TRADE_COUNT_UNAVAILABLE: "
                    f"{daily_count_error}"
                )
                return
            if (
                automatic_daily_limit > 0
                and automatic_daily_entries >= automatic_daily_limit
            ):
                reason = (
                    "REJECTED_DAILY_TRADE_LIMIT: automatic daily trade count "
                    f"{automatic_daily_entries} >= {automatic_daily_limit}"
                )
                self.last_entry_reason[symbol] = reason
                if raw_symbol != symbol:
                    self.last_entry_reason[raw_symbol] = reason
                if trace_utbreakout:
                    self._utbreakout_trace_event(
                        symbol,
                        'ENTRY_BLOCKED',
                        'REJECTED_DAILY_TRADE_LIMIT',
                        side=side,
                        reason=reason,
                        daily_entries=automatic_daily_entries,
                        daily_limit=automatic_daily_limit,
                    )
                await self.ctrl.notify(
                    f"⛔ 자동매매 일일 진입 한도: "
                    f"{automatic_daily_entries}/{automatic_daily_limit}회"
                )
                return

            if trace_utbreakout:
                liquidity_gate = (
                    await self._validate_mainnet_entry_quote_volume(symbol)
                )
                if not liquidity_gate.get('allowed'):
                    gate_code = str(
                        liquidity_gate.get('status')
                        or 'REJECTED_LOW_QUOTE_VOLUME'
                    )
                    gate_reason = str(
                        liquidity_gate.get('reason')
                        or '24h quoteVolume liquidity gate failed'
                    )
                    self.last_entry_reason[symbol] = (
                        f'{gate_code}: {gate_reason}'
                    )
                    if raw_symbol != symbol:
                        self.last_entry_reason[raw_symbol] = (
                            self.last_entry_reason[symbol]
                        )
                    self._utbreakout_trace_event(
                        symbol,
                        'ENTRY_BLOCKED',
                        gate_code,
                        side=side,
                        reason=gate_reason,
                        quote_volume=liquidity_gate.get('quote_volume'),
                        min_quote_volume=liquidity_gate.get(
                            'min_quote_volume'
                        ),
                    )
                    self._clear_utbot_filtered_breakout_entry_plan(symbol)
                    try:
                        await self.ctrl.notify(
                            f"🚫 UTBreakout 진입 차단: {symbol} "
                            f"{str(side or '').upper()}\n"
                            f"{gate_reason}"
                        )
                    except Exception:
                        logger.debug(
                            "quote-volume entry block notify skipped",
                            exc_info=True,
                        )
                    return

            pause_state = load_critical_pause_state()
            pause_decision = evaluate_critical_pause_block(state=pause_state, requested_symbol=symbol)
            if pause_decision.blocked:
                await _handle_critical_pause_entry_block(
                    self,
                    symbol=symbol,
                    raw_symbol=raw_symbol,
                    side=side,
                    decision=pause_decision,
                    qty=None,
                    phase="ENTRY_PREFLIGHT",
                )
                return

            try:
                assert_trading_allowed(symbol, cfg, include_critical_pause=False)
            except Exception as pause_err:
                if trace_utbreakout:
                    self._utbreakout_trace_event(
                        symbol,
                        'ENTRY_BLOCKED',
                        'TRADING_PAUSED',
                        side=side,
                        reason=str(pause_err),
                    )
                logger.error(f"Trading blocked by pause state: {pause_err}")
                self.last_entry_reason[symbol] = f"Trading Blocked: {pause_err}"
                await self.ctrl.notify(f"⚠️ **진입 차단**: {symbol} 거래 정지 상태 ({pause_err})")
                return

            if _normalize_live_real_stage(cfg.get("live_activation_stage")) == "LIVE_REAL_SMALL_CAP":
                try:
                    await self.preflight_live_real_check(symbol, cfg)
                except Exception as safety_err:
                    if trace_utbreakout:
                        self._utbreakout_trace_event(
                            symbol,
                            'ENTRY_BLOCKED',
                            'LIVE_PREFLIGHT',
                            side=side,
                            reason=str(safety_err),
                        )
                    logger.error(f"Live real preflight blocked entry: {safety_err}")
                    self.last_entry_reason[symbol] = f"Live real preflight blocked: {safety_err}"
                    await self.ctrl.notify(f"⚠️ **실거래 진입 차단**: {symbol} ({safety_err})")
                    return

            if cfg.get("live_parity_signal_enabled", False):
                logger.info(f"[Live Parity] Attempting {side.upper()} final trade decision for {symbol} @ {price}")

                rows = await self._fetch_live_ohlcv_rows_for_context(symbol, cfg)
                if not rows or len(rows) < int(cfg.get("live_context_min_bars", 80)):
                    logger.warning("[Live Parity] insufficient OHLCV rows for context: %s", symbol)
                    self.last_entry_reason[symbol] = "NO_TRADE: insufficient live OHLCV context"
                    return

                idx = len(rows) - 1

                indicator_values = await self._build_live_indicator_values(
                    symbol=symbol,
                    side=side,
                    rows=rows,
                    idx=idx,
                    cfg=cfg,
                )

                context = build_market_context(
                    rows=rows,
                    idx=idx,
                    symbol=symbol,
                    timeframe=cfg.get("timeframe", "15m"),
                    values=indicator_values,
                    config=cfg,
                )

                context = self._validate_live_context_or_block(symbol, context, cfg)
                if context is None:
                    self.last_entry_reason[symbol] = "NO_TRADE: invalid live context"
                    return

                models = getattr(self, "models", None) or {}
                stats = ENGINE_PERFORMANCE_STATS

                decision = evaluate_final_trade_decision(
                    context=context,
                    config=cfg,
                    models=models,
                    stats=stats
                )

                if not decision.valid:
                    logger.info(f"[Live Parity] Decision invalid: {decision.reasons}")
                    self.last_entry_reason[symbol] = f"Decision Invalid: {decision.reasons}"
                    await self.ctrl.notify(f"⚠️ **진입 판정 거부**: {symbol} Parity 거절 ({decision.reasons})")
                    return

                if str(decision.side).upper() != str(side).upper():
                    logger.warning(f"[Live Parity] Side conflict: side={side}, decision={decision.side}")
                    return

                try:
                    account_equity = await self._fetch_futures_account_equity(cfg)
                    plan = build_live_order_plan_from_decision(
                        symbol,
                        decision,
                        context,
                        cfg,
                        account_equity=account_equity,
                    )
                except Exception as err:
                    logger.error(f"[Live Parity] Order plan error: {err}")
                    await self.ctrl.notify(f"⚠️ **주문 계획 생성 오류**: {err}")
                    return

                result = await self.execute_live_order_plan(plan, cfg)
                return result
            if not self.is_trade_direction_allowed(side):
                block_reason = self.format_trade_direction_block_reason(side)
                if trace_utbreakout:
                    self._utbreakout_trace_event(
                        symbol,
                        'ENTRY_BLOCKED',
                        'DIRECTION_FILTER',
                        side=side,
                        reason=block_reason,
                    )
                display_symbol = self.ctrl.format_symbol_for_display(symbol)
                logger.info(
                    f"[Signal] Entry blocked by direction filter: "
                    f"{display_symbol} {str(side or '').upper()} vs {self.get_effective_trade_direction().upper()}"
                )
                self.last_entry_reason[symbol] = block_reason
                await self.ctrl.notify(f"⚠️ {display_symbol} {block_reason}")
                return

            if self.is_upbit_mode():
                await self._entry_upbit_spot(symbol, side, price)
                return

            # === [Single Position Enforcement] ===
            # ?대? ?ㅻⅨ ?ъ??섏씠 ?덈뒗吏 ?뺤씤 (?꾩껜 ?щ낵 ?ㅼ틪)
            # Volume Scanner ???대뼡 湲곕뒫???곕뜑?쇰룄 ?대? ?ъ??섏씠 ?덉쑝硫?異붽? 吏꾩엯 李⑤떒
            try:
                all_positions = await asyncio.to_thread(self.exchange.fetch_positions)
                for p in all_positions:
                    if float(p.get('contracts', 0)) > 0:
                        active_sym = normalize_futures_market_id(p.get('symbol'))
                        target_sym = normalize_futures_market_id(symbol)

                        if active_sym == target_sym:
                            if trace_utbreakout:
                                self._utbreakout_trace_event(
                                    symbol,
                                    'ENTRY_BLOCKED',
                                    'SAME_SYMBOL_POSITION_EXISTS',
                                    side=side,
                                    reason=f"already holding {p.get('symbol')}",
                                )
                            logger.warning(
                                "[Single Limit] Entry blocked: already holding same symbol %s",
                                p.get('symbol'),
                            )
                            self.last_entry_reason[symbol] = (
                                f"SAME_SYMBOL_POSITION_EXISTS: already holding {p.get('symbol')}"
                            )
                            entry_label = "UTBreakout" if trace_utbreakout else "Entry"
                            await self.ctrl.notify(
                                f"{entry_label} blocked: same-symbol position already exists "
                                f"({p.get('symbol')})"
                            )
                            return

                        if active_sym != target_sym:
                            if trace_utbreakout:
                                self._utbreakout_trace_event(
                                    symbol,
                                    'ENTRY_BLOCKED',
                                    'SINGLE_POSITION_LIMIT',
                                    side=side,
                                    reason=f"holding {p.get('symbol')}",
                                )
                            logger.warning(f"?슟 [Single Limit] Entry blocked: Already holding {p['symbol']}")
                            await self.ctrl.notify(f"⚠️ **진입 차단**: 단일 포지션 제한 (보유 중: {p['symbol']})")
                            return
            except Exception as e:
                if trace_utbreakout:
                    self._utbreakout_trace_event(
                        symbol,
                        'ENTRY_BLOCKED',
                        'POSITION_CHECK_ERROR',
                        side=side,
                        reason=str(e),
                    )
                logger.error(f"Single position check failed: {e}")
                return # ?덉쟾???꾪빐 ?뺤씤 ?ㅽ뙣 ??吏꾩엯 以묐떒

            logger.info(f"?뱿 [Signal] Attempting {side.upper()} entry @ {price}")

            cfg = self.get_runtime_common_settings()
            strategy_params = trace_strategy_params
            active_strategy = trace_active_strategy
            filtered_breakout_plan = None
            filtered_breakout_decision_ts = None
            if active_strategy in UTBREAKOUT_STRATEGIES:
                filtered_breakout_plan = (
                    self._get_utbot_filtered_breakout_entry_plan(symbol, side)
                    or self._get_utbot_filtered_breakout_entry_plan(raw_symbol, side)
                )
                self._utbreakout_trace_event(
                    symbol,
                    'PLAN_LOOKUP',
                    'FOUND' if filtered_breakout_plan else 'MISSING',
                    side=side,
                    raw_symbol=raw_symbol,
                    normalized_symbol=symbol,
                    plan_symbol=(
                        filtered_breakout_plan.get('plan_symbol')
                        if filtered_breakout_plan else None
                    ),
                    available_plan_keys=list(
                        getattr(self, 'utbot_filtered_breakout_entry_plans', {}).keys()
                    )[:20],
                )

            def _record_filtered_breakout_entry_block(code, reason, extra=None):
                if active_strategy not in UTBREAKOUT_STRATEGIES:
                    return
                entry_reason = f"{code}: {reason}"
                self.last_entry_reason[symbol] = entry_reason
                if raw_symbol != symbol:
                    self.last_entry_reason[raw_symbol] = entry_reason
                fb_cfg = self._get_utbot_filtered_breakout_config(strategy_params)
                status = {
                    'candidate_side': side,
                    'entry_timeframe': fb_cfg.get('entry_timeframe', '15m'),
                    'htf_timeframe': fb_cfg.get('htf_timeframe', '1h'),
                    'reason': reason,
                    'entry_price': price,
                    'entry_plan': dict(filtered_breakout_plan or {})
                }
                payload_extra = {'code': code}
                if isinstance(extra, dict):
                    payload_extra.update(extra)
                self._record_utbreakout_diagnostic_event(
                    symbol,
                    status,
                    event='entry_blocked',
                    extra=payload_extra
                )
                trace_payload = dict(payload_extra)
                for reserved_key in ('symbol', 'stage', 'status'):
                    trace_payload.pop(reserved_key, None)
                trace_payload.setdefault('side', side)
                trace_payload.setdefault('reason', reason)
                self._utbreakout_trace_event(
                    symbol,
                    'ENTRY_BLOCKED',
                    code,
                    **trace_payload,
                )

            lev_default = 5 if active_strategy in UTBREAKOUT_STRATEGIES else 10
            lev = int(max(1.0, float(cfg.get('leverage', lev_default) or lev_default)))
            if active_strategy in UTBREAKOUT_STRATEGIES and filtered_breakout_plan and filtered_breakout_plan.get('micro_auto'):
                try:
                    lev = int(max(1.0, float(filtered_breakout_plan.get('leverage', lev) or lev)))
                except (TypeError, ValueError):
                    pass
            req_risk_pct = float(cfg.get('risk_per_trade_pct', DEFAULT_RISK_PER_TRADE_PERCENT) or DEFAULT_RISK_PER_TRADE_PERCENT)
            bounded_risk_pct = normalize_risk_percent(cfg)
            risk_pct = bounded_risk_pct / 100.0

            _, free, _ = await self.get_balance_info()
            if active_strategy in UTBREAKOUT_STRATEGIES:
                self._utbreakout_trace_event(
                    symbol,
                    'BALANCE_CHECK',
                    'OK' if free > 0 else 'INSUFFICIENT',
                    side=side,
                    free_balance=free,
                )

            if free <= 0:
                _record_filtered_breakout_entry_block(
                    'ENTRY_BLOCKED_BALANCE',
                    f'Insufficient balance: {free}',
                    {'free_balance': free},
                )
                logger.warning(f"Insufficient balance: {free}")
                await self.ctrl.notify(f"⚠️ 잔고 부족: ${free:.2f}")
                return

            def _safe_float(v):
                try:
                    if v is None or v == '':
                        return None
                    f = float(v)
                    return f if f > 0 else None
                except (TypeError, ValueError):
                    return None

            def _pick_min_positive(values):
                parsed = [x for x in (_safe_float(v) for v in values) if x is not None]
                return min(parsed) if parsed else 0.0

            safety_buffer = 0.98
            if active_strategy in UTBREAKOUT_STRATEGIES:
                if not filtered_breakout_plan:
                    # Rebuild the currently configured strategy/aggregate once
                    # before giving up. Rebuilding UTBreak alone here could
                    # discard a valid RSPT/QH/Crowding/LXR selection.
                    try:
                        (
                            filtered_breakout_plan,
                            rebuilt_sig,
                            rebuilt_reason,
                        ) = await self._rebuild_utbreakout_entry_plan_for_active_strategy(
                            symbol,
                            side,
                            strategy_params,
                            active_strategy,
                        )
                        logger.warning(
                            "[UTBOT_FILTERED_BREAKOUT_V1] rebuilt missing entry plan "
                            "through active strategy: symbol=%s raw=%s side=%s "
                            "active=%s sig=%s reason=%s",
                            symbol,
                            raw_symbol,
                            side,
                            active_strategy,
                            rebuilt_sig,
                            rebuilt_reason,
                        )
                    except Exception as rebuild_exc:
                        logger.exception(
                            "UTBreakout missing-plan rebuild failed for %s: %s",
                            symbol,
                            rebuild_exc,
                        )

                if not filtered_breakout_plan:
                    try:
                        aliases = (
                            self._utbreakout_plan_symbol_keys(symbol)
                            + self._utbreakout_plan_symbol_keys(raw_symbol)
                        )
                    except Exception:
                        aliases = [str(symbol), str(raw_symbol)]
                    _record_filtered_breakout_entry_block(
                        'ENTRY_BLOCKED_MISSING_PLAN',
                        'UTBOT_FILTERED_BREAKOUT_V1 entry blocked: missing risk plan',
                        {
                            'symbol': symbol,
                            'raw_symbol': raw_symbol,
                            'side': side,
                            'plan_aliases': aliases,
                            'available_plan_keys': list(
                                getattr(self, 'utbot_filtered_breakout_entry_plans', {}).keys()
                            )[:20],
                        },
                    )
                    await self.ctrl.notify(
                        "⚠️ UTBreakout 진입 계획 누락으로 주문 중단: "
                        f"{raw_symbol}->{symbol} {side}. aliases={aliases[:4]}"
                    )
                    return
                filtered_breakout_decision_ts = self._utbreakout_decision_timestamp(
                    filtered_breakout_plan
                )
                if self._utbreakout_decision_already_consumed(
                    symbol,
                    filtered_breakout_decision_ts,
                ):
                    _record_filtered_breakout_entry_block(
                        'ENTRY_BLOCKED_DECISION_ALREADY_CONSUMED',
                        'this closed-candle decision already entered once',
                        {
                            'decision_candle_ts': filtered_breakout_decision_ts,
                        },
                    )
                    self._clear_utbot_filtered_breakout_entry_plan(symbol)
                    return
                if filtered_breakout_plan.get('micro_auto'):
                    try:
                        lev = int(max(1.0, float(filtered_breakout_plan.get('leverage', lev) or lev)))
                    except (TypeError, ValueError):
                        pass
                planned_qty = float(filtered_breakout_plan.get('qty', 0.0) or 0.0)
                target_notional = planned_qty * float(price)
                margin_to_use = target_notional / max(float(lev), 1e-9)
            else:
                # Position sizing (user-friendly):
                # 1) Use configured % of current free USDT as margin.
                # 2) Apply leverage to that margin to get position notional.
                # 3) Keep a small safety buffer to avoid -2019 by fees/slippage.
                margin_to_use = free * risk_pct
                target_notional = margin_to_use * lev * safety_buffer

            # Exchange min notional check (prevents Binance -4164).
            min_notional = 0.0
            try:
                markets = await asyncio.to_thread(self.exchange.load_markets)
                market = markets.get(symbol) or markets.get(f"{symbol}:USDT") or {}
                limits = market.get('limits', {}) if isinstance(market, dict) else {}
                info = market.get('info', {}) if isinstance(market, dict) else {}
                filters = info.get('filters', []) if isinstance(info, dict) else []
                filter_values = []
                if isinstance(filters, list):
                    for f in filters:
                        if not isinstance(f, dict):
                            continue
                        if f.get('filterType') in ('MIN_NOTIONAL', 'NOTIONAL'):
                            filter_values.extend([f.get('notional'), f.get('minNotional')])
                min_notional = _pick_min_positive([
                    limits.get('cost', {}).get('min') if isinstance(limits.get('cost', {}), dict) else None,
                    info.get('notional'),
                    info.get('minNotional'),
                    *filter_values,
                ])
            except Exception as e:
                logger.debug(f"Entry min notional lookup failed for {symbol}: {e}")

            if (
                active_strategy in UTBREAKOUT_STRATEGIES
                and filtered_breakout_plan
                and filtered_breakout_plan.get('micro_auto')
                and float(filtered_breakout_plan.get('min_notional_usdt', 0.0) or 0.0) > 0
            ):
                min_notional = float(filtered_breakout_plan.get('min_notional_usdt', 0.0) or 0.0)

            # Binance futures fallback (some responses omit min notional filter fields).
            if min_notional <= 0 and getattr(self.exchange, 'id', '') == 'binance':
                default_type = str(getattr(self.exchange, 'options', {}).get('defaultType', '')).lower()
                if default_type in ('future', 'futures'):
                    min_notional = 100.0

            max_notional = free * lev * safety_buffer
            if min_notional > 0 and target_notional < min_notional:
                if active_strategy in UTBREAKOUT_STRATEGIES:
                    logger.warning(
                        f"[UTBOT_FILTERED_BREAKOUT_V1] Entry blocked by min notional: "
                        f"target={target_notional:.2f}, min={min_notional:.2f}, risk_plan={filtered_breakout_plan}"
                    )
                    _record_filtered_breakout_entry_block(
                        'ENTRY_BLOCKED_MIN_NOTIONAL',
                        'UTBOT_FILTERED_BREAKOUT_V1 entry blocked by min notional',
                        {
                            'target_notional': target_notional,
                            'min_notional': min_notional,
                            'free_balance': free,
                            'leverage': lev
                        }
                    )
                    await self.ctrl.notify(
                        f"⚠️ UTBOT_FILTERED_BREAKOUT_V1 최소 주문금액 미달: "
                        f"계획 {target_notional:.2f} < 필요 {min_notional:.2f} USDT. "
                        "리스크 기반 수량을 임의 증액하지 않고 진입을 차단합니다."
                    )
                    self._clear_utbot_filtered_breakout_entry_plan(symbol)
                    return
                # If balance/leverage can support exchange minimum, auto-bump notional.
                if max_notional >= min_notional:
                    target_notional = min_notional * 1.001  # small buffer for precision truncation
                    margin_to_use = target_notional / max(lev * safety_buffer, 1e-9)
                    implied_risk_pct = (margin_to_use / max(free, 1e-9)) * 100.0
                    await self.ctrl.notify(
                        f"ℹ️ 최소 주문금액 반영: {target_notional:.2f} USDT "
                        f"(요구 {min_notional:.2f}, 계산 리스크 {implied_risk_pct:.1f}%)"
                    )
                else:
                    required_risk_pct = (min_notional / max(max_notional, 1e-9)) * 100.0
                    logger.warning(
                        f"Entry blocked by min notional: target={target_notional:.2f}, "
                        f"min={min_notional:.2f}, max={max_notional:.2f}, free={free:.2f}, lev={lev}"
                    )
                    await self.ctrl.notify(
                        f"⚠️ 최소 주문금액 미달: 필요 {min_notional:.2f} USDT, 가능 {max_notional:.2f} USDT. "
                        f"잔고를 늘리거나 레버리지를 높이세요(필요 리스크 약 {required_risk_pct:.1f}%)."
                    )
                    return

            if active_strategy in UTBREAKOUT_STRATEGIES and target_notional > max_notional:
                logger.warning(
                    f"[UTBOT_FILTERED_BREAKOUT_V1] Entry resized by margin cap: "
                    f"target={target_notional:.2f}, max={max_notional:.2f}, free={free:.2f}, lev={lev}"
                )
                original_target_notional = target_notional
                target_notional = max_notional
                margin_to_use = target_notional / max(float(lev), 1e-9)
                capped_qty = target_notional / max(float(price), 1e-9)
                risk_distance = float(filtered_breakout_plan.get('risk_distance', 0.0) or 0.0)
                filtered_breakout_plan.update({
                    'qty': capped_qty,
                    'planned_notional': target_notional,
                    'planned_margin': margin_to_use,
                    'risk_usdt': capped_qty * risk_distance,
                    'position_cap_applied': True,
                    'position_cap_reason': 'available_margin_changed_before_order',
                    'position_cap_original_notional': original_target_notional,
                    'position_cap_max_notional': max_notional,
                })
                self._utbreakout_trace_event(
                    symbol,
                    'POSITION_SIZE_CAP',
                    'RESIZED',
                    side=side,
                    original_notional=original_target_notional,
                    capped_notional=target_notional,
                    free_balance=free,
                    leverage=lev,
                )
                await self.ctrl.notify(
                    f"UTBreakout 수량 축소: 계획 {original_target_notional:.2f} -> "
                    f"가능 {target_notional:.2f} USDT (증거금/레버리지 상한)"
                )

            qty = self.safe_amount(symbol, target_notional / price)
            try:
                qty_notional = float(qty) * float(price)
            except (TypeError, ValueError):
                qty_notional = 0.0
            if active_strategy in UTBREAKOUT_STRATEGIES and filtered_breakout_plan:
                filtered_breakout_plan = reconcile_utbreakout_risk_plan_to_order_qty(
                    filtered_breakout_plan,
                    qty=qty,
                    entry_price=price,
                    leverage=lev,
                    reason='exchange_amount_precision',
                )
                target_notional = qty_notional
                margin_to_use = target_notional / max(float(lev), 1e-9)
            if min_notional > 0 and qty_notional < min_notional:
                logger.warning(
                    f"Entry quantity below min notional after precision: qty={qty}, "
                    f"notional={qty_notional:.4f}, min={min_notional:.4f}"
                )
                if active_strategy in UTBREAKOUT_STRATEGIES:
                    _record_filtered_breakout_entry_block(
                        'ENTRY_BLOCKED_PRECISION_MIN_NOTIONAL',
                        'UTBOT_FILTERED_BREAKOUT_V1 entry blocked after precision min notional check',
                        {
                            'qty_notional': qty_notional,
                            'min_notional': min_notional,
                            'qty': qty
                        }
                    )
                    await self.ctrl.notify(
                        f"⚠️ UTBOT_FILTERED_BREAKOUT_V1 수량 정밀도 후 최소 금액 미달"
                        f"({qty_notional:.2f} < {min_notional:.2f}). 리스크 기반 진입 차단."
                    )
                    self._clear_utbot_filtered_breakout_entry_plan(symbol)
                else:
                    await self.ctrl.notify(
                        f"⚠️ 주문 수량 정밀도 때문에 최소 금액 미달({qty_notional:.2f} < {min_notional:.2f}). "
                        "레버리지/진입비율을 높여주세요."
                    )
                return

            if float(qty) <= 0:
                _record_filtered_breakout_entry_block(
                    'ENTRY_BLOCKED_INVALID_QTY',
                    f'Invalid quantity: {qty}',
                    {
                        'free_balance': free,
                        'target_notional': target_notional,
                        'price': price,
                    },
                )
                logger.warning(f"Invalid quantity: {qty} (free={free}, risk={risk_pct}, lev={lev}, price={price}, target_notional={target_notional})")
                await self.ctrl.notify(f"⚠️ 주문 수량 계산 오류: {qty} (잔고: {free:.2f})")
                return

            if active_strategy not in UTBREAKOUT_STRATEGIES and bounded_risk_pct != req_risk_pct:
                await self.ctrl.notify(f"⚠️ 리스크 상한 적용: {req_risk_pct:.2f}% -> {bounded_risk_pct:.2f}%")
            if active_strategy in UTBREAKOUT_STRATEGIES:
                self._utbreakout_trace_event(
                    symbol,
                    'BALANCE_CHECK',
                    'READY',
                    side=side,
                    free_balance=free,
                    qty=qty,
                    target_notional=target_notional,
                    margin_to_use=margin_to_use,
                    min_notional=min_notional,
                    leverage=lev,
                )
                logger.info(
                    f"[UTBOT_FILTERED_BREAKOUT_V1] Entry params: qty={qty}, lev={lev}x, "
                    f"risk_usdt={float(filtered_breakout_plan.get('risk_usdt', 0) or 0):.4f}, "
                    f"risk_distance={float(filtered_breakout_plan.get('risk_distance', 0) or 0):.4f}, "
                    f"target_notional={target_notional:.2f}, margin_to_use={margin_to_use:.2f}"
                )
            else:
                logger.info(
                    f"Entry params: qty={qty}, lev={lev}x, risk={bounded_risk_pct:.2f}% "
                    f"(free={free:.2f}, margin_to_use={margin_to_use:.2f}, target_notional={target_notional:.2f}, safety={safety_buffer:.2f})"
                )

            if (
                active_strategy in UTBREAKOUT_STRATEGIES
                and filtered_breakout_plan
                and filtered_breakout_plan.get('micro_auto')
                and (filtered_breakout_plan.get('dry_run', True) or not filtered_breakout_plan.get('live_enabled', False))
            ):
                logger.info(
                    f"[Micro Auto V1] Dry-run/live-lock entry skipped: {symbol} {side} "
                    f"qty={qty}, notional={target_notional:.2f}, margin={margin_to_use:.2f}, lev={lev}x"
                )
                self.micro_auto_last_plan[symbol] = dict(filtered_breakout_plan)
                self.micro_auto_last_plan[symbol].update({
                    'dry_run_blocked_at': datetime.now(timezone.utc).isoformat(),
                    'dry_run_target_notional': target_notional,
                    'dry_run_margin': margin_to_use,
                    'dry_run_qty': float(qty),
                })
                _record_filtered_breakout_entry_block(
                    'MICRO_AUTO_DRY_RUN_READY',
                    'Micro Auto V1 dry-run/live-lock: order not sent',
                    {
                        'target_notional': target_notional,
                        'planned_margin': margin_to_use,
                        'leverage': lev,
                        'micro_auto': True,
                        'dry_run': filtered_breakout_plan.get('dry_run', True),
                        'live_enabled': filtered_breakout_plan.get('live_enabled', False),
                    }
                )
                self._clear_utbot_filtered_breakout_entry_plan(symbol)
                return

            if active_strategy in UTBREAKOUT_STRATEGIES and filtered_breakout_plan:
                try:
                    fb_cfg = self._get_utbot_filtered_breakout_config(strategy_params)
                    live_ladder_cfg = dict(fb_cfg or {})
                    live_ladder_cfg.update(dict(cfg or {}))
                    live_ladder_cfg.update(dict(filtered_breakout_plan or {}))
                    split_policy = str(
                        live_ladder_cfg.get("tp_split_failure_policy", "single_tp2_with_warning")
                        or "single_tp2_with_warning"
                    ).lower()
                    live_ladder_enabled = (
                        _normalize_live_real_stage(cfg.get("live_activation_stage")) == "LIVE_REAL_SMALL_CAP"
                        and str(live_ladder_cfg.get("live_tp_ladder_mode", "tp1_tp2_full_exit") or "").lower()
                        == "tp1_tp2_full_exit"
                    )
                    risk_distance = _safe_float_or_none(filtered_breakout_plan.get("risk_distance"))
                    sl_price_for_preview = _safe_float_or_none(
                        filtered_breakout_plan.get("stop_loss")
                        or filtered_breakout_plan.get("stop_loss_price")
                        or filtered_breakout_plan.get("sl_price")
                        or filtered_breakout_plan.get("initial_sl_price")
                    )
                    if sl_price_for_preview is None and risk_distance is not None:
                        sl_price_for_preview = (
                            float(price) - float(risk_distance)
                            if str(side).lower() == "long"
                            else float(price) + float(risk_distance)
                        )
                    if live_ladder_enabled and split_policy == "block_entry" and sl_price_for_preview:
                        ladder_preview = self._build_tp_ladder_orders(
                            symbol,
                            side,
                            price,
                            sl_price_for_preview,
                            qty,
                            live_ladder_cfg,
                        )
                        filtered_breakout_plan["tp_ladder_preflight"] = dict(ladder_preview)
                        if not ladder_preview.get("ok"):
                            reason = ladder_preview.get("reason") or "tp_ladder_not_possible"
                            _record_filtered_breakout_entry_block(
                                'ENTRY_BLOCKED_TP_LADDER',
                                f'TP ladder not possible: {reason}',
                                {
                                    'tp_ladder_mode': ladder_preview.get('mode'),
                                    'tp_ladder_reason': reason,
                                    'tp_split_failure_policy': split_policy,
                                },
                            )
                            await self.ctrl.notify(
                                f"UTBreakout entry blocked: {symbol} {side.upper()} / "
                                f"TP ladder not possible ({reason})"
                            )
                            self._clear_utbot_filtered_breakout_entry_plan(symbol)
                            return
                except Exception as ladder_preflight_exc:
                    logger.warning(
                        "[UTBOT_FILTERED_BREAKOUT_V1] TP ladder preflight skipped for %s: %s",
                        symbol,
                        ladder_preflight_exc,
                    )

            # [Enforce] Market Settings (Isolated + Leverage)
            await self.ensure_market_settings(symbol, leverage=lev)

            liquidation_payload = dict(cfg or {})
            if isinstance(filtered_breakout_plan, dict):
                liquidation_payload.update(filtered_breakout_plan)
            liquidation_stop = self._extract_liquidation_stop_price(
                side,
                price,
                liquidation_payload,
                lev,
            )
            liquidation_preflight = await self._preflight_liquidation_safety(
                symbol,
                side,
                price,
                liquidation_stop,
                lev,
                qty,
                liquidation_payload,
            )
            if not liquidation_preflight.get('valid'):
                reason = liquidation_preflight.get('reason') or liquidation_preflight.get('status')
                self.last_entry_reason[symbol] = f"ENTRY_REJECTED_LIQUIDATION_CONFLICT:{reason}"
                if active_strategy in UTBREAKOUT_STRATEGIES:
                    self._utbreakout_trace_event(
                        symbol,
                        'ENTRY_BLOCKED',
                        'ENTRY_REJECTED_LIQUIDATION_CONFLICT',
                        side=side,
                        stop_price=liquidation_stop,
                        liquidation_price=liquidation_preflight.get('liquidation_price'),
                        reason=reason,
                    )
                    self._clear_utbot_filtered_breakout_entry_plan(symbol)
                await self.ctrl.notify(
                    "[진입 거부] 전략 스탑이 청산가보다 늦게 발동합니다.\n"
                    f"심볼: {self.ctrl.format_symbol_for_display(symbol)}\n"
                    f"방향: {side.upper()}\n스탑: {liquidation_stop}\n"
                    f"예상 청산가: {liquidation_preflight.get('liquidation_price')}\n"
                    f"이유: {reason}"
                )
                return
            selected_leverage = int(liquidation_preflight.get('selected_leverage') or lev)
            if selected_leverage < lev:
                lev = selected_leverage
                max_notional = free * lev * safety_buffer
                leverage_qty_reduced = False
                if target_notional > max_notional:
                    original_target_notional = target_notional
                    target_notional = max_notional
                    qty = self.safe_amount(symbol, target_notional / max(float(price), 1e-9))
                    target_notional = float(qty) * float(price)
                    margin_to_use = target_notional / max(float(lev), 1e-9)
                    leverage_qty_reduced = True
                    if isinstance(filtered_breakout_plan, dict):
                        filtered_breakout_plan.update({
                            'position_cap_applied': True,
                            'position_cap_reason': 'liquidation_safe_leverage_margin_cap',
                            'position_cap_original_notional': original_target_notional,
                            'position_cap_max_notional': max_notional,
                        })
                if isinstance(filtered_breakout_plan, dict):
                    filtered_breakout_plan = reconcile_utbreakout_risk_plan_to_order_qty(
                        filtered_breakout_plan,
                        qty=qty,
                        entry_price=price,
                        leverage=lev,
                        reason='liquidation_safe_leverage',
                    )
                if float(qty) <= 0 or (
                    min_notional > 0 and target_notional < min_notional
                ):
                    reason = (
                        'leverage-safe quantity rounded to zero'
                        if float(qty) <= 0
                        else (
                            f'leverage-safe quantity below minimum notional: '
                            f'{target_notional:.4f} < {min_notional:.4f}'
                        )
                    )
                    _record_filtered_breakout_entry_block(
                        'ENTRY_BLOCKED_LEVERAGE_MIN_NOTIONAL',
                        reason,
                        {
                            'qty': qty,
                            'target_notional': target_notional,
                            'min_notional': min_notional,
                            'selected_leverage': lev,
                        },
                    )
                    entry_label = (
                        'UTBreakout'
                        if active_strategy in UTBREAKOUT_STRATEGIES
                        else 'Entry'
                    )
                    await self.ctrl.notify(
                        f"{entry_label} blocked after safe leverage sizing: "
                        f"{symbol} / {reason}"
                    )
                    if active_strategy in UTBREAKOUT_STRATEGIES:
                        self._clear_utbot_filtered_breakout_entry_plan(symbol)
                    return
                if (
                    leverage_qty_reduced
                    and active_strategy in UTBREAKOUT_STRATEGIES
                    and isinstance(filtered_breakout_plan, dict)
                ):
                    try:
                        fb_cfg = self._get_utbot_filtered_breakout_config(strategy_params)
                        live_ladder_cfg = dict(fb_cfg or {})
                        live_ladder_cfg.update(dict(cfg or {}))
                        live_ladder_cfg.update(filtered_breakout_plan)
                        split_policy = str(
                            live_ladder_cfg.get(
                                'tp_split_failure_policy',
                                'single_tp2_with_warning',
                            )
                            or 'single_tp2_with_warning'
                        ).lower()
                        live_ladder_enabled = (
                            _normalize_live_real_stage(cfg.get('live_activation_stage'))
                            == 'LIVE_REAL_SMALL_CAP'
                            and str(
                                live_ladder_cfg.get(
                                    'live_tp_ladder_mode',
                                    'tp1_tp2_full_exit',
                                )
                                or ''
                            ).lower() == 'tp1_tp2_full_exit'
                        )
                        risk_distance = _safe_float_or_none(
                            filtered_breakout_plan.get('risk_distance')
                        )
                        stop_price = _safe_float_or_none(
                            filtered_breakout_plan.get('stop_loss')
                            or filtered_breakout_plan.get('stop_loss_price')
                            or filtered_breakout_plan.get('sl_price')
                            or filtered_breakout_plan.get('initial_sl_price')
                        )
                        if stop_price is None and risk_distance is not None:
                            stop_price = (
                                float(price) - risk_distance
                                if side == 'long'
                                else float(price) + risk_distance
                            )
                        if live_ladder_enabled and split_policy == 'block_entry' and stop_price:
                            ladder_preview = self._build_tp_ladder_orders(
                                symbol,
                                side,
                                price,
                                stop_price,
                                qty,
                                live_ladder_cfg,
                            )
                            filtered_breakout_plan['tp_ladder_preflight'] = dict(
                                ladder_preview
                            )
                            if not ladder_preview.get('ok'):
                                reason = (
                                    ladder_preview.get('reason')
                                    or 'tp_ladder_not_possible_after_leverage_resize'
                                )
                                _record_filtered_breakout_entry_block(
                                    'ENTRY_BLOCKED_TP_LADDER_AFTER_RESIZE',
                                    f'TP ladder not possible after safe leverage resize: {reason}',
                                    {
                                        'qty': qty,
                                        'selected_leverage': lev,
                                        'tp_ladder_reason': reason,
                                    },
                                )
                                await self.ctrl.notify(
                                    f"UTBreakout entry blocked after safe leverage resize: "
                                    f"TP ladder unavailable ({reason})"
                                )
                                self._clear_utbot_filtered_breakout_entry_plan(symbol)
                                return
                    except Exception as ladder_preflight_exc:
                        logger.warning(
                            'TP ladder resize preflight skipped for %s: %s',
                            symbol,
                            ladder_preflight_exc,
                        )
                await self.ensure_market_settings(symbol, leverage=lev)
                liquidation_preflight = await self._preflight_liquidation_safety(
                    symbol,
                    side,
                    price,
                    liquidation_stop,
                    lev,
                    qty,
                    liquidation_payload,
                )
                if not liquidation_preflight.get('valid'):
                    await self.ctrl.notify(
                        f"[진입 거부] 자동 레버리지 감소 후 청산가 검증 실패: {symbol} / "
                        f"{liquidation_preflight.get('reason')}"
                    )
                    return

            logger.info(
                "[ORDER_PLAN_READY] symbol=%s side=%s qty=%s price=%s notional=%.4f "
                "margin=%.4f free=%.4f strategy=%s",
                symbol,
                side,
                qty,
                price,
                target_notional,
                margin_to_use,
                free,
                active_strategy,
            )

            entry_client_order_id = None
            try:
                outcome = await _submit_idempotent_crypto_entry(
                    self,
                    symbol,
                    side,
                    qty,
                    active_strategy or 'SIGNAL_ENGINE',
                    filtered_breakout_plan or cfg,
                )
                if outcome.critical_pause_block is not None:
                    await _handle_critical_pause_entry_block(
                        self,
                        symbol=symbol,
                        raw_symbol=raw_symbol,
                        side=side,
                        decision=outcome.critical_pause_block,
                        qty=qty,
                        phase="ENTRY_LATE_GATE",
                    )
                    return

                if outcome.entry_block_reason is not None:
                    await _handle_noncritical_entry_block(
                        self,
                        symbol=symbol,
                        raw_symbol=raw_symbol,
                        side=side,
                        reason=outcome.entry_block_reason,
                        qty=qty,
                        phase="ENTRY_LATE_GATE",
                    )
                    return

                if outcome.duplicate_protected:
                    entry_client_order_id = outcome.client_order_id
                    self.last_entry_reason[symbol] = "DUPLICATE:duplicate signal already protected"
                    if raw_symbol != symbol:
                        self.last_entry_reason[raw_symbol] = "DUPLICATE:duplicate signal already protected"
                    return

                if outcome.submission_error is not None:
                    raw_sub = outcome.submission
                    raw_state = getattr(raw_sub, "state", None)
                    state_value = getattr(raw_state, "value", raw_state)
                    state_name = str(state_value or "").strip().upper() or "UNKNOWN"
                    raise RuntimeError(
                        "entry submission not accepted: "
                        f"{state_name} / {outcome.submission_error}"
                    )

                submission = outcome.submission
                order = submission.order or {}
                entry_client_order_id = outcome.client_order_id
            except Exception as order_exc:
                if active_strategy in UTBREAKOUT_STRATEGIES:
                    self._utbreakout_trace_event(
                        symbol,
                        'ORDER_FAILED',
                        type(order_exc).__name__,
                        side=side,
                        qty=qty,
                        target_notional=target_notional,
                        margin_to_use=margin_to_use,
                        error=str(order_exc),
                    )
                logger.exception(
                    "[ORDER_FAILED] symbol=%s side=%s qty=%s notional=%.4f "
                    "margin=%.4f free=%.4f error=%s",
                    symbol,
                    side,
                    qty,
                    target_notional,
                    margin_to_use,
                    free,
                    order_exc,
                )
                if active_strategy in UTBREAKOUT_STRATEGIES:
                    _record_filtered_breakout_entry_block(
                        'ENTRY_ORDER_FAILED',
                        f'UTBOT_FILTERED_BREAKOUT_V1 order failed: {order_exc}',
                        {
                            'symbol': symbol,
                            'side': side,
                            'qty': str(qty),
                            'target_notional': target_notional,
                            'margin_to_use': margin_to_use,
                            'free_balance': free,
                            'error_type': type(order_exc).__name__,
                            'error': str(order_exc),
                        },
                    )
                order_failure_reason = (
                    f"ORDER_FAILED: {type(order_exc).__name__}: {order_exc}"
                )
                self.last_entry_reason[symbol] = order_failure_reason
                if raw_symbol != symbol:
                    self.last_entry_reason[raw_symbol] = order_failure_reason
                if active_strategy in UTBREAKOUT_STRATEGIES:
                    await self.ctrl.notify(
                        "❌ UTBreakout 주문 실패: "
                        f"{symbol} {side.upper()} qty={qty} / "
                        f"{type(order_exc).__name__}: {order_exc}"
                    )
                else:
                    await self.ctrl.notify(
                        f"❌ 주문 실패: {symbol} {side.upper()} qty={qty} / "
                        f"{type(order_exc).__name__}: {order_exc}"
                    )
                return

            if active_strategy in UTBREAKOUT_STRATEGIES:
                self._record_utbreakout_consumed_decision(
                    symbol,
                    filtered_breakout_decision_ts,
                )
                self._utbreakout_trace_event(
                    symbol,
                    'ORDER_SUCCESS',
                    'ACCEPTED',
                    side=side,
                    qty=qty,
                    order_id=order.get('id') if isinstance(order, dict) else None,
                )

            # 罹먯떆 ?꾩쟾 臾댄슚??
            self.position_cache = None
            self.position_cache_time = 0

            confirmation = await self._confirm_entry_position(
                symbol,
                side,
                order,
                price,
                qty,
            )
            verify_pos = confirmation.get('position')
            if not confirmation.get('confirmed') or not verify_pos:
                failure_code = (
                    'ENTRY_POSITION_FETCH_FAILED'
                    if not confirmation.get('position_fetch_ok')
                    else 'NO_POSITION_AFTER_ENTRY'
                )
                failure_reason = (
                    f"{failure_code}: order accepted but position was not confirmed "
                    f"(order_id={order.get('id') if isinstance(order, dict) else None})"
                )
                self.last_entry_reason[symbol] = failure_reason
                _mark_crypto_entry_state(
                    self,
                    entry_client_order_id,
                    OrderState.SUBMITTED_UNKNOWN,
                    last_error=failure_reason,
                )
                self.crypto_entry_lock_reason = f"SUBMITTED_UNKNOWN:{entry_client_order_id}"
                self.trading_state_store.set_runtime_state(
                    'entry_lock_reason',
                    self.crypto_entry_lock_reason,
                )
                if raw_symbol != symbol:
                    self.last_entry_reason[raw_symbol] = failure_reason
                if not confirmation.get('position_fetch_ok'):
                    if getattr(self, 'ctrl', None) is not None and hasattr(self.ctrl, 'is_paused'):
                        self.ctrl.is_paused = True
                if active_strategy in UTBREAKOUT_STRATEGIES:
                    self._utbreakout_trace_event(
                        symbol,
                        'NO_POSITION_AFTER_ENTRY',
                        failure_code,
                        side=side,
                        qty=qty,
                        order_id=order.get('id') if isinstance(order, dict) else None,
                    )
                    self._clear_utbot_filtered_breakout_entry_plan(symbol)
                await self.ctrl.notify(
                    f"⚠️ {self.ctrl.format_symbol_for_display(symbol)} 진입 주문 접수 후 "
                    "포지션을 확인하지 못했습니다. DB 기록과 TP/SL 생성을 중단했으며 수동 확인이 필요합니다."
                )
                return

            actual_entry_price = float(verify_pos['entryPrice'])
            actual_qty = abs(float(
                self._position_signed_contracts(verify_pos)
                or verify_pos.get('contracts', 0)
                or 0
            ))
            if actual_qty <= 0:
                logger.error(f"Confirmed entry has invalid quantity for {symbol}: {verify_pos}")
                _mark_crypto_entry_state(
                    self,
                    entry_client_order_id,
                    OrderState.SUBMITTED_UNKNOWN,
                    last_error='confirmed entry quantity was invalid',
                )
                self.crypto_entry_lock_reason = f"SUBMITTED_UNKNOWN:{entry_client_order_id}"
                return
            qty = self.safe_amount(symbol, actual_qty)
            entry_plan_attribution = dict(filtered_breakout_plan or {})
            primary_strategy = str(
                entry_plan_attribution.get('strategy')
                or active_strategy
                or 'SIGNAL_ENGINE'
            )
            confirmation_strategies = (
                entry_plan_attribution.get('quad_alpha_confirmation_strategies')
                or entry_plan_attribution.get('triple_alpha_confirmation_strategies')
                or entry_plan_attribution.get('dual_alpha_confirmation_strategies')
                or [primary_strategy]
            )
            confirmation_strategies = list(dict.fromkeys(
                str(value) for value in confirmation_strategies if str(value).strip()
            ))
            _mark_crypto_entry_state(
                self,
                entry_client_order_id,
                OrderState.FILLED_UNVERIFIED_LIQUIDATION,
                exchange_order_id=(order.get('id') if isinstance(order, dict) else None),
                filled_qty=actual_qty,
                average_fill_price=actual_entry_price,
                primary_strategy=primary_strategy,
                confirmation_strategies=confirmation_strategies,
                aggregate_strategy=(
                    active_strategy
                    if active_strategy in {DUAL_ALPHA_STRATEGY, TRIPLE_ALPHA_STRATEGY, QUAD_ALPHA_STRATEGY}
                    else None
                ),
                leverage=lev,
                entry_plan_summary={
                    key: entry_plan_attribution.get(key)
                    for key in (
                        'strategy',
                        'plan_symbol',
                        'stop_loss',
                        'risk_distance',
                        'rr_multiple',
                        'entry_timeframe',
                        'exit_timeframe',
                        'htf_timeframe',
                        'partial_take_profit_r_multiple',
                        'partial_take_profit_ratio',
                        'second_take_profit_r_multiple',
                        'second_take_profit_ratio',
                        'runner_pct',
                        'atr_trailing_enabled',
                        'atr_trailing_multiplier',
                        'atr_trailing_activation_r',
                        'runner_exit_enabled',
                        'runner_chandelier_enabled',
                        'tp1_breakeven_enabled',
                        'tp1_breakeven_trigger_r',
                        'tp1_breakeven_offset_r',
                        'tp1_breakeven_wait_for_partial',
                        'tp1_breakeven_qty_tolerance',
                    )
                    if entry_plan_attribution.get(key) is not None
                },
            )
            actual_liquidation_payload = dict(cfg or {})
            if isinstance(filtered_breakout_plan, dict):
                actual_liquidation_payload.update(filtered_breakout_plan)
            actual_stop_price = self._extract_liquidation_stop_price(
                side,
                actual_entry_price,
                actual_liquidation_payload,
                lev,
            )
            actual_liquidation_check = await self._verify_actual_liquidation_safety(
                symbol,
                side,
                actual_stop_price,
                verify_pos,
                actual_liquidation_payload,
                entry_client_order_id,
            )
            if not actual_liquidation_check.get('valid'):
                self.last_entry_reason[symbol] = str(actual_liquidation_check.get('status'))
                if active_strategy in UTBREAKOUT_STRATEGIES:
                    self._clear_utbot_filtered_breakout_entry_plan(symbol)
                return
            verify_pos = actual_liquidation_check.get('position') or verify_pos
            self.db.log_trade_entry(
                symbol,
                side,
                actual_entry_price,
                float(qty),
                strategy=primary_strategy,
            )
            logger.info(
                f"Entry confirmed: order={order.get('id', 'N/A')} "
                f"source={confirmation.get('source')} qty={qty} price={actual_entry_price}"
            )
            if active_strategy in UTBREAKOUT_STRATEGIES:
                self._utbreakout_trace_event(
                    symbol,
                    'POSITION_CONFIRMED',
                    'FOUND',
                    side=side,
                    qty=qty,
                    actual_entry_price=actual_entry_price,
                    order_id=order.get('id') if isinstance(order, dict) else None,
                )
            execution_snapshot = None
            if active_strategy in UTBREAKOUT_STRATEGIES:
                execution_snapshot = self._build_entry_execution_snapshot(
                    symbol,
                    side,
                    requested_price=price,
                    actual_entry_price=actual_entry_price,
                    entry_plan=filtered_breakout_plan,
                    planned_qty=(filtered_breakout_plan or {}).get('qty') if isinstance(filtered_breakout_plan, dict) else None,
                    final_order_qty=qty,
                    filled_qty=qty,
                    target_notional=target_notional,
                    margin_to_use=margin_to_use,
                    leverage=lev,
                    strategy=STRATEGY_DISPLAY_NAMES.get(active_strategy, active_strategy),
                    order=order,
                )
                try:
                    risk_distance = _safe_float_or_none((filtered_breakout_plan or {}).get("risk_distance"))
                    if risk_distance is not None and risk_distance > 0:
                        sl_price_for_ladder = (
                            float(actual_entry_price) - float(risk_distance)
                            if str(side).lower() == "long"
                            else float(actual_entry_price) + float(risk_distance)
                        )
                        sl_price_for_ladder = float(self.safe_price(symbol, sl_price_for_ladder))
                        fb_cfg = self._get_utbot_filtered_breakout_config(strategy_params)
                        live_ladder_cfg = dict(fb_cfg or {})
                        live_ladder_cfg.update(dict(cfg or {}))
                        live_ladder_cfg.update(dict(filtered_breakout_plan or {}))
                        ladder_snapshot = self._build_tp_ladder_orders(
                            symbol,
                            side,
                            actual_entry_price,
                            sl_price_for_ladder,
                            qty,
                            live_ladder_cfg,
                        )
                        execution_snapshot["tp_ladder"] = ladder_snapshot
                        if isinstance(ladder_snapshot, dict) and ladder_snapshot.get("sl_distance") is not None:
                            execution_snapshot["sl_price"] = ladder_snapshot.get("sl_price")
                            execution_snapshot["risk_distance"] = ladder_snapshot.get("sl_distance")
                            filled_qty_value = _safe_float_or_none(execution_snapshot.get("filled_qty"))
                            if filled_qty_value is not None:
                                execution_snapshot["actual_risk_estimate"] = (
                                    float(filled_qty_value)
                                    * float(ladder_snapshot.get("sl_distance") or 0.0)
                                )
                except Exception as ladder_snapshot_exc:
                    logger.debug(
                        "entry TP ladder snapshot skipped for %s: %s",
                        symbol,
                        ladder_snapshot_exc,
                    )
                self._record_last_live_entry_snapshot(execution_snapshot)
            await self.ctrl.notify(
                self._build_signal_entry_notice(
                    symbol,
                    side,
                    qty,
                    price,
                    actual_entry_price,
                    entry_plan=filtered_breakout_plan if active_strategy in UTBREAKOUT_STRATEGIES else None,
                    leverage=lev,
                    target_notional=target_notional,
                    margin_to_use=margin_to_use,
                    execution_snapshot=execution_snapshot,
                )
            )

            # ===== ?꾨왂蹂?TP/SL ?ㅼ젙 =====
            if active_strategy == 'microvbo':
                # MicroVBO: ATR 湲곕컲 TP/SL 二쇰Ц
                if self.vbo_breakout_level:
                    self.vbo_entry_price = actual_entry_price
                    self.vbo_entry_atr = self.vbo_breakout_level.get('atr', 0)
                    vbo_cfg = strategy_params.get('MicroVBO', {})
                    tp_mult = vbo_cfg.get('tp_atr_multiplier', 1.0)
                    sl_mult = vbo_cfg.get('sl_atr_multiplier', 0.5)

                    await self._place_tp_sl_orders(symbol, side, actual_entry_price, qty,
                                                   self.vbo_entry_atr * tp_mult,
                                                   self.vbo_entry_atr * sl_mult)
                    logger.info(f"?뮶 [MicroVBO] Entry state saved: price={actual_entry_price:.2f}, ATR={self.vbo_entry_atr:.2f}")

            elif active_strategy == 'fractalfisher':
                # FractalFisher: Trailing Stop (?뚰봽?몄썾??湲곕컲 ?좎? - ?숈쟻?대?濡?
                if self.fisher_entry_atr:
                    self.fisher_entry_price = actual_entry_price
                    ff_cfg = strategy_params.get('FractalFisher', {})
                    trailing_mult = ff_cfg.get('atr_trailing_multiplier', 2.0)
                    if side == 'long':
                        self.fisher_trailing_stop = actual_entry_price - (self.fisher_entry_atr * trailing_mult)
                    else:
                        self.fisher_trailing_stop = actual_entry_price + (self.fisher_entry_atr * trailing_mult)
                    logger.info(f"?뮶 [FractalFisher] Entry state: price={actual_entry_price:.2f}, TrailingStop={self.fisher_trailing_stop:.2f}")
                    await self.ctrl.notify(f"🧭 Trailing Stop 설정: {self.fisher_trailing_stop:.2f}")

            elif active_strategy == 'cameron':
                cam_state = self.cameron_states.get(symbol, {})
                cam_cfg = strategy_params.get('Cameron', {})
                rr_ratio = float(cam_cfg.get('risk_reward_ratio', 2.0) or 2.0)
                stop_price = float(cam_state.get('stop_price', 0.0) or 0.0)
                rr_applied = False

                if side == 'long' and stop_price > 0:
                    risk_distance = actual_entry_price - stop_price
                elif side == 'short' and stop_price > 0:
                    risk_distance = stop_price - actual_entry_price
                else:
                    risk_distance = 0.0

                if risk_distance > 0:
                    tp_distance = risk_distance * rr_ratio
                    await self._place_tp_sl_orders(
                        symbol,
                        side,
                        actual_entry_price,
                        qty,
                        tp_distance=tp_distance,
                        sl_distance=risk_distance
                    )
                    rr_applied = True
                    await self.ctrl.notify(
                        f"🎯 CAMERON RR {rr_ratio:.1f}:1 적용 "
                        f"(SL 기준 {risk_distance / max(actual_entry_price, 1e-9) * 100:.2f}%)"
                    )
                    logger.info(
                        f"[CAMERON] RR protection set: entry={actual_entry_price:.4f}, "
                        f"stop={stop_price:.4f}, risk={risk_distance:.4f}, rr={rr_ratio:.2f}"
                    )

                if not rr_applied:
                    logger.warning(f"[CAMERON] Invalid stop anchor for {symbol}. Falling back to default TP/SL.")
                    tp_master_enabled = bool(cfg.get('tp_sl_enabled', True))
                    tp_enabled = tp_master_enabled and bool(cfg.get('take_profit_enabled', True))
                    sl_enabled = tp_master_enabled and bool(cfg.get('stop_loss_enabled', True))
                    if tp_enabled or sl_enabled:
                        tp_pct = cfg.get('target_roe_pct', 20.0) / 100.0 / lev
                        sl_pct = cfg.get('stop_loss_pct', 10.0) / 100.0 / lev

                        tp_distance = (actual_entry_price * tp_pct) if (tp_enabled and tp_pct > 0) else None
                        sl_distance = (actual_entry_price * sl_pct) if (sl_enabled and sl_pct > 0) else None

                        if tp_distance is not None or sl_distance is not None:
                            await self._place_tp_sl_orders(
                                symbol,
                                side,
                                actual_entry_price,
                                qty,
                                tp_distance=tp_distance,
                                sl_distance=sl_distance
                            )

            elif active_strategy in UTBREAKOUT_STRATEGIES:
                plan = filtered_breakout_plan or self._get_utbot_filtered_breakout_entry_plan(symbol, side)
                if not plan:
                    logger.error("[UTBOT_FILTERED_BREAKOUT_V1] Missing risk plan after entry; flattening unprotected position.")
                    fail_status = await self._fail_closed_unprotected_position(
                        symbol,
                        reason='UTBreakout risk plan missing after entry fill',
                        status_code='MISSING_RISK_PLAN_AFTER_FILL',
                        expected_tp=True,
                    )
                    if (fail_status.get('emergency_close') or {}).get('closed'):
                        verify_pos = None
                    await self.ctrl.notify(
                        "🚨 UTBOT_FILTERED_BREAKOUT_V1 리스크 계획 누락: "
                        "무방비 포지션 긴급 청산을 실행했습니다."
                    )
                else:
                    risk_distance = float(plan.get('risk_distance', 0.0) or 0.0)
                    rr_multiple = float(plan.get('rr_multiple', 3.50) or 3.50)
                    if risk_distance > 0 and rr_multiple > 0:
                        fb_cfg = self._get_utbot_filtered_breakout_config(strategy_params)
                        effective_fb_cfg = apply_profit_opportunity_effective_overrides(dict(fb_cfg))
                        for key in (
                            'fixed_take_profit_enabled',
                            'partial_take_profit_enabled',
                            'partial_take_profit_r_multiple',
                            'partial_take_profit_ratio',
                            'second_take_profit_enabled',
                            'second_take_profit_r_multiple',
                            'second_take_profit_ratio',
                            'atr_trailing_enabled',
                            'atr_trailing_multiplier',
                            'atr_trailing_activation_r',
                            'atr_trailing_breakeven_enabled',
                            'dynamic_take_profit_enabled',
                            'tp1_breakeven_enabled',
                            'tp1_breakeven_trigger_r',
                            'tp1_breakeven_offset_r',
                            'tp1_breakeven_wait_for_partial',
                            'tp1_breakeven_qty_tolerance',
                            'runner_pct',
                            'runner_exit_enabled',
                            'runner_chandelier_enabled',
                            'ev_exit_profile_name',
                            'ev_exit_mode',
                            'ev_time_stop_enabled',
                            'ev_time_stop_bars',
                            'ev_time_stop_min_mfe_r',
                            'take_profit_front_run_atr',
                            'take_profit_front_run_pct',
                            'tp_front_run_min_r_multiple',
                            'structure_stop_buffer_atr',
                            'soft_stop_enabled',
                            'soft_stop_confirm_bars',
                            'near_miss_tp_enabled',
                            'near_miss_tp_arm_ratio',
                            'near_miss_tp_lock_r',
                            'near_miss_tp_rejection_atr',
                            'profit_alpha_stall_exit_max_current_r',
                            'ev_time_stop_max_current_r',
                            'time_stop_max_current_r',
                        ):
                            if key in plan:
                                effective_fb_cfg[key] = plan[key]
                        effective_fb_cfg['take_profit_r_multiple'] = float(
                            plan.get(
                                'rr_multiple',
                                effective_fb_cfg.get(
                                    'second_take_profit_r_multiple',
                                    effective_fb_cfg.get('partial_take_profit_r_multiple', rr_multiple),
                                ),
                            )
                            or rr_multiple
                        )
                        partial_enabled = bool(effective_fb_cfg.get('partial_take_profit_enabled', True))
                        partial_ratio = (
                            min(1.0, max(0.0, float(effective_fb_cfg.get('partial_take_profit_ratio', 0.20) or 0.20)))
                            if partial_enabled else 1.0
                        )
                        partial_r = (
                            float(effective_fb_cfg.get('partial_take_profit_r_multiple', 1.00) or 1.00)
                            if partial_enabled else rr_multiple
                        )
                        second_enabled = bool(effective_fb_cfg.get('second_take_profit_enabled', True))
                        second_r = float(effective_fb_cfg.get('second_take_profit_r_multiple', rr_multiple) or rr_multiple)
                        second_ratio = (
                            min(
                                max(0.0, 1.0 - partial_ratio),
                                max(0.0, float(effective_fb_cfg.get('second_take_profit_ratio', 0.40) or 0.40))
                            )
                            if second_enabled else 0.0
                        )
                        atr_for_front_run = plan.get('atr')
                        partial_distance = self._utbreakout_tp_distance_after_front_run(
                            risk_distance,
                            partial_r,
                            effective_fb_cfg,
                            atr_for_front_run,
                        ) or (risk_distance * partial_r)
                        second_distance = self._utbreakout_tp_distance_after_front_run(
                            risk_distance,
                            second_r,
                            effective_fb_cfg,
                            atr_for_front_run,
                        ) or (risk_distance * second_r)
                        tp_targets = []
                        if partial_enabled and partial_ratio > 0:
                            tp_targets.append({
                                'label': 'TP1',
                                'kind': 'tp1',
                                'distance': partial_distance,
                                'qty_ratio': partial_ratio,
                                'target_r': partial_r,
                                'effective_r': partial_distance / max(risk_distance, 1e-9),
                            })
                        if second_enabled and second_ratio > 0 and second_r > 0:
                            tp_targets.append({
                                'label': 'TP2',
                                'kind': 'tp2',
                                'distance': second_distance,
                                'qty_ratio': second_ratio,
                                'target_r': second_r,
                                'effective_r': second_distance / max(risk_distance, 1e-9),
                            })
                        if not tp_targets:
                            fallback_distance = self._utbreakout_tp_distance_after_front_run(
                                risk_distance,
                                rr_multiple,
                                effective_fb_cfg,
                                atr_for_front_run,
                            ) or (risk_distance * rr_multiple)
                            tp_targets.append({
                                'label': 'TP',
                                'kind': 'tp',
                                'distance': fallback_distance,
                                'qty_ratio': 1.0,
                                'target_r': rr_multiple,
                                'effective_r': fallback_distance / max(risk_distance, 1e-9),
                            })
                        await self._place_tp_sl_orders(
                            symbol,
                            side,
                            actual_entry_price,
                            qty,
                            sl_distance=risk_distance,
                            tp_targets=tp_targets,
                            preserve_runner_qty=bool(
                                float(plan.get('runner_pct', 0.0) or 0.0) > 0
                            ),
                        )
                        if (
                            bool(effective_fb_cfg.get('atr_trailing_enabled', False))
                            or bool(effective_fb_cfg.get('tp1_breakeven_enabled', True))
                        ):
                            self._register_utbreakout_trailing_state(
                                symbol,
                                side,
                                actual_entry_price,
                                qty,
                                plan,
                                effective_fb_cfg
                            )
                        else:
                            self._clear_utbreakout_trailing_state(symbol)
                        logger.info(
                            f"[UTBOT_FILTERED_BREAKOUT_V1] RR protection set: "
                            f"entry={actual_entry_price:.4f}, risk={risk_distance:.4f}, "
                            f"tp1={partial_ratio:.2f}@{partial_r:.2f}R, "
                            f"tp2={second_ratio:.2f}@{second_r:.2f}R, "
                            f"runner={'ON' if effective_fb_cfg.get('atr_trailing_enabled', False) else 'OFF'}, "
                            f"tp1_be={'ON' if effective_fb_cfg.get('tp1_breakeven_enabled', True) else 'OFF'}"
                        )
                        if bool(plan.get('aggressive_growth_overlay')):
                            position_fetch_ok, fresh_pos = await self._fetch_server_position_checked(symbol)
                            runner_state = self._get_utbreakout_trailing_state(symbol)
                            audit_status = (
                                await self._audit_protection_orders(
                                    symbol,
                                    pos=fresh_pos,
                                    expected_tp=True,
                                    expected_sl=True,
                                    planned_tp_orders=self._planned_tp_orders_from_state(symbol, runner_state),
                                    alert=True
                                )
                                if position_fetch_ok
                                else {'status': 'POSITION_FETCH_FAILED', 'sl_present': False}
                            )
                            if fresh_pos and audit_status.get('sl_present'):
                                self._record_aggressive_growth_position(
                                    symbol,
                                    fresh_pos,
                                    plan,
                                    qty=abs(float(fresh_pos.get('contracts', qty) or qty)),
                                    current_price=actual_entry_price
                                )
                                split_mode = plan.get('aggressive_growth_exit_split_mode', 'growth')
                                await self.ctrl.notify(
                                    f"🚀 Aggressive Growth 진입: {self.ctrl.format_symbol_for_display(symbol)} "
                                    f"score `{float(plan.get('aggressive_growth_score', 0) or 0):.1f}` / "
                                    f"risk `{float(plan.get('aggressive_growth_risk_pct', 0) or 0) * 100:.2f}%` / "
                                    f"split `{float(plan.get('partial_take_profit_ratio', 0) or 0):.0%}/"
                                    f"{float(plan.get('second_take_profit_ratio', 0) or 0):.0%}/"
                                    f"{float(plan.get('runner_pct', 0) or 0):.0%}` ({split_mode})"
                                )
                                reduced_reasons = []
                                vol_reason = str(plan.get('aggressive_growth_vol_target_reason') or '')
                                if vol_reason not in {'', 'atr_normal', 'vol_target_disabled'}:
                                    reduced_reasons.append(vol_reason)
                                try:
                                    kelly_mult = float(plan.get('aggressive_growth_kelly_multiplier', 1.0) or 1.0)
                                except (TypeError, ValueError):
                                    kelly_mult = 1.0
                                if kelly_mult < 0.999:
                                    reduced_reasons.append(str(plan.get('aggressive_growth_kelly_reason') or 'kelly_reduced'))
                                if reduced_reasons:
                                    await self.ctrl.notify(
                                        f"ℹ️ Aggressive Growth 축소 적용: "
                                        f"{self.ctrl.format_symbol_for_display(symbol)} {', '.join(reduced_reasons)}"
                                    )
                            else:
                                self._clear_aggressive_growth_position(symbol)
                                logger.warning(
                                    f"Aggressive growth tracking skipped for {symbol}: "
                                    f"protection audit={audit_status}"
                                )
                    else:
                        fail_status = await self._fail_closed_unprotected_position(
                            symbol,
                            reason='UTBreakout protection distance invalid after entry fill',
                            status_code='INVALID_RISK_DISTANCE_AFTER_FILL',
                            expected_tp=True,
                        )
                        if (fail_status.get('emergency_close') or {}).get('closed'):
                            verify_pos = None
                        await self.ctrl.notify(
                            "🚨 UTBOT_FILTERED_BREAKOUT_V1 보호 주문 거리 계산 오류: "
                            "무방비 포지션 긴급 청산을 실행했습니다."
                        )
                if plan and not plan.get('aggressive_growth_overlay'):
                    self._clear_aggressive_growth_position(symbol)
                if plan:
                    try:
                        requested = float(price)
                        actual = float(actual_entry_price)
                        signed_slippage_pct = (actual - requested) / max(abs(requested), 1e-9) * 100.0
                        adverse_slippage_pct = (
                            signed_slippage_pct if side == 'long' else -signed_slippage_pct
                        )
                    except (TypeError, ValueError):
                        signed_slippage_pct = None
                        adverse_slippage_pct = None
                    self._record_utbreakout_diagnostic_event(
                        symbol,
                        {
                            'candidate_side': side,
                            'entry_timeframe': plan.get('entry_timeframe') or self._get_utbot_filtered_breakout_config(strategy_params).get('entry_timeframe', '15m'),
                            'htf_timeframe': plan.get('htf_timeframe') or self._get_utbot_filtered_breakout_config(strategy_params).get('htf_timeframe', '1h'),
                            'adaptive_timeframe_enabled': plan.get('adaptive_timeframe_enabled'),
                            'reason': f"{STRATEGY_DISPLAY_NAMES.get(active_strategy, 'UTBOT_FILTERED_BREAKOUT_V1')} entry filled",
                            'entry_price': actual_entry_price,
                            'entry_plan': dict(plan)
                        },
                        event='entry_filled',
                        extra={
                            'code': 'ENTRY_FILLED',
                            'order_id': order.get('id') if isinstance(order, dict) else None,
                            'requested_price': price,
                            'actual_entry_price': actual_entry_price,
                            'entry_slippage_pct': adverse_slippage_pct,
                            'entry_signed_slippage_pct': signed_slippage_pct,
                            'target_notional': target_notional,
                            'planned_margin': margin_to_use,
                            'leverage': lev
                        }
                    )
                self._clear_utbot_filtered_breakout_entry_plan(symbol)

            else:
                # SMA/HMA: ?쇱꽱??湲곕컲 TP/SL 二쇰Ц
                tp_master_enabled = bool(cfg.get('tp_sl_enabled', True))
                tp_enabled = tp_master_enabled and bool(cfg.get('take_profit_enabled', True))
                sl_enabled = tp_master_enabled and bool(cfg.get('stop_loss_enabled', True))
                if tp_enabled or sl_enabled:
                    tp_pct = cfg.get('target_roe_pct', 20.0) / 100.0 / lev  # ROE瑜?媛寃?蹂?숇쪧濡?蹂??
                    sl_pct = cfg.get('stop_loss_pct', 10.0) / 100.0 / lev

                    tp_distance = (actual_entry_price * tp_pct) if (tp_enabled and tp_pct > 0) else None
                    sl_distance = (actual_entry_price * sl_pct) if (sl_enabled and sl_pct > 0) else None

                    if tp_distance is not None or sl_distance is not None:
                        await self._place_tp_sl_orders(
                            symbol,
                            side,
                            actual_entry_price,
                            qty,
                            tp_distance=tp_distance,
                            sl_distance=sl_distance
                        )

            if verify_pos:
                protection_audit = await self._audit_protection_orders(
                    symbol,
                    pos=verify_pos,
                    expected_tp=None,
                    expected_sl=True,
                    alert=True,
                )
                if (
                    protection_audit.get('fetch_ok')
                    and protection_audit.get('sl_present')
                    and not protection_audit.get('sl_qty_mismatch')
                ):
                    protection_orders = await self._collect_protection_orders(symbol)
                    stop_order = next(
                        (
                            item for item in (protection_orders or [])
                            if self._classify_protection_order(item) == 'sl'
                        ),
                        None,
                    )
                    trailing_state = self._get_utbreakout_trailing_state(symbol)
                    if isinstance(trailing_state, dict):
                        trailing_state = (
                            self._sync_utbreakout_state_with_protection_orders(
                                symbol,
                                trailing_state,
                                protection_orders,
                            )
                        )
                    planned_tp_orders = self._planned_tp_orders_from_state(
                        symbol,
                        trailing_state,
                    )
                    take_profit_order_ids = []
                    take_profit_order_labels = {}
                    for item in protection_orders or []:
                        if self._classify_protection_order(item) != 'tp':
                            continue
                        order_id = self._protection_order_id(item)
                        if not order_id:
                            continue
                        label = self._protection_tp_label(
                            item,
                            planned_tp_orders,
                        )
                        take_profit_order_ids.append(str(order_id))
                        if label:
                            take_profit_order_labels[str(order_id)] = str(
                                label
                            ).upper()
                    _mark_crypto_entry_state(
                        self,
                        entry_client_order_id,
                        OrderState.PROTECTED,
                        stop_order_id=(
                            self._protection_order_id(stop_order)
                            if stop_order is not None
                            else None
                        ),
                        take_profit_order_ids=take_profit_order_ids,
                        take_profit_order_labels=take_profit_order_labels,
                    )
                    if str(getattr(self, 'crypto_entry_lock_reason', '') or '').startswith('FILLED_'):
                        self._set_crypto_entry_lock(None)
                else:
                    self.crypto_entry_lock_reason = f"FILLED_UNPROTECTED:{entry_client_order_id}"
                    self.trading_state_store.set_runtime_state(
                        'entry_lock_reason',
                        self.crypto_entry_lock_reason,
                    )
                    await self.ctrl.notify(
                        f"CRITICAL: {self.ctrl.format_symbol_for_display(symbol)} is not verified "
                        "as stop-loss protected. New entries are locked."
                    )
                logger.info(f"??Position verified: {verify_pos['side']} {verify_pos['contracts']}")
            else:
                logger.warning("?좑툘 Position not found after entry (may take time to update)")

        except Exception as e:
            logger.error(f"Signal entry error: {e}")
            import traceback
            traceback.print_exc()
            if locals().get('active_strategy') in UTBREAKOUT_STRATEGIES:
                self._utbreakout_trace_event(
                    locals().get('symbol', locals().get('raw_symbol', 'UNKNOWN')),
                    'ENTRY_BLOCKED',
                    'UNHANDLED_EXCEPTION',
                    side=locals().get('side'),
                    reason=str(e),
                    error_type=type(e).__name__,
                )
                self._clear_utbot_filtered_breakout_entry_plan(symbol)
            await self.ctrl.notify(f"❌ 진입 실패: {e}")

    async def _entry_upbit_spot(self, symbol, side, price):
        if side != 'long':
            logger.info(f"Upbit spot entry ignored: side={side}")
            return

        try:
            cfg = self.get_runtime_common_settings()
            min_order_krw = max(5000.0, float(cfg.get('min_order_krw', 5000.0) or 5000.0))
            active_symbols = await self.get_active_position_symbols(use_cache=False)
            for active_symbol in sorted(active_symbols):
                if active_symbol != symbol:
                    active_pos = await self.get_server_position(active_symbol, use_cache=False)
                    active_qty = abs(float((active_pos or {}).get('contracts', 0) or 0))
                    active_price = float((active_pos or {}).get('markPrice', 0) or (active_pos or {}).get('entryPrice', 0) or 0)
                    active_notional = active_qty * active_price
                    if active_notional < min_order_krw:
                        logger.info(
                            f"[Upbit] Dust holding ignored for entry block: {active_symbol} value={active_notional:,.0f} KRW"
                        )
                        continue
                    display_active = self.ctrl.format_symbol_for_display(active_symbol)
                    await self.ctrl.notify(f"⚠️ **진입 차단**: 업비트 단일 보유 제한 (보유 중: {display_active})")
                    return

            req_risk_pct = float(cfg.get('risk_per_trade_pct', DEFAULT_RISK_PER_TRADE_PERCENT) or DEFAULT_RISK_PER_TRADE_PERCENT)
            bounded_risk_pct = normalize_risk_percent(cfg)
            risk_pct = bounded_risk_pct / 100.0

            _, free_krw, _ = await self.get_balance_info()
            if free_krw <= 0:
                await self.ctrl.notify(f"⚠️ 업비트 KRW 잔고 부족: {free_krw:,.0f} KRW")
                return

            safety_buffer = 0.997
            target_notional = free_krw * risk_pct * safety_buffer
            if target_notional < min_order_krw:
                if free_krw >= min_order_krw:
                    target_notional = min_order_krw
                else:
                    await self.ctrl.notify(
                        f"⚠️ 업비트 최소 주문금액 미달: 필요 {min_order_krw:,.0f} KRW, 가용 {free_krw:,.0f} KRW"
                    )
                    return

            qty = self.safe_amount(symbol, target_notional / max(float(price), 1e-9))
            if float(qty) <= 0:
                await self.ctrl.notify(f"⚠️ 업비트 주문 수량 계산 오류: {qty}")
                return

            if bounded_risk_pct != req_risk_pct:
                await self.ctrl.notify(f"⚠️ 업비트 리스크 상한 적용: {req_risk_pct:.2f}% -> {bounded_risk_pct:.2f}%")

            await self.ensure_market_settings(symbol, leverage=1)
            order = await asyncio.to_thread(
                self.exchange.create_order,
                symbol,
                'market',
                'buy',
                float(qty),
                float(price)
            )

            self.position_cache = None
            self.position_cache_time = 0
            self.db.log_trade_entry(symbol, 'long', price, float(qty))

            display_symbol = self.ctrl.format_symbol_for_display(symbol)
            await self.ctrl.notify(
                f"✅ [Upbit] BUY {display_symbol} {qty}\n예상 체결가: {price:,.0f} KRW | 주문금액 약 {target_notional:,.0f} KRW"
            )

            await asyncio.sleep(1)
            self.position_cache = None
            verify_pos = await self.get_server_position(symbol, use_cache=False)
            if verify_pos:
                logger.info(
                    f"[Upbit] Position verified: {display_symbol} qty={verify_pos['contracts']} avg={verify_pos['entryPrice']}"
                )
            else:
                logger.warning(f"[Upbit] Position not found after buy: {display_symbol}, order={order}")
        except Exception as e:
            logger.error(f"Upbit spot entry error: {e}")
            await self.ctrl.notify(f"❌ 업비트 매수 실패: {e}")
