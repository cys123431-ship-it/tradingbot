"""Live candle, indicator, derivatives, and preflight context construction."""

from __future__ import annotations

def _live_timeframe_to_ms(tf):
    multipliers = {
        "m": 60 * 1000,
        "h": 60 * 60 * 1000,
        "d": 24 * 60 * 60 * 1000,
        "w": 7 * 24 * 60 * 60 * 1000,
    }
    try:
        text = str(tf or "").strip().lower()
        if not text:
            return 0
        if text.isdigit():
            return int(text) * multipliers["m"]
        return int(text[:-1]) * multipliers.get(text[-1], 0)
    except Exception:
        return 0


def _live_timestamp_to_ms(value):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    if parsed <= 0:
        return 0.0
    return parsed * 1000.0 if parsed < 10_000_000_000 else parsed


def _live_context_now_ms(cfg):
    cfg = cfg if isinstance(cfg, dict) else {}
    override = cfg.get("live_context_now_ms")
    if override is not None:
        parsed = _live_timestamp_to_ms(override)
        if parsed > 0:
            return parsed
    return time.time() * 1000.0


def _closed_live_ohlcv_rows(rows, cfg):
    cfg = cfg if isinstance(cfg, dict) else {}
    cleaned = [dict(row) for row in (rows or []) if isinstance(row, dict)]
    if not cleaned or not bool(cfg.get("exclude_incomplete_live_candle", True)):
        return cleaned

    tf = cfg.get("timeframe") or cfg.get("entry_timeframe") or "15m"
    tf_ms = _live_timeframe_to_ms(tf)
    if tf_ms <= 0:
        return cleaned

    last_ts = _live_timestamp_to_ms(cleaned[-1].get("timestamp"))
    if last_ts <= 0:
        return cleaned

    if last_ts + tf_ms > _live_context_now_ms(cfg):
        return cleaned[:-1]
    return cleaned


async def _fetch_live_ohlcv_rows_for_context(self, symbol, cfg):
    tf = cfg.get("timeframe", "15m")
    limit = max(120, int(cfg.get("live_context_ohlcv_limit", 200) or 200))
    raw = await asyncio.to_thread(self.exchange.fetch_ohlcv, symbol, tf, limit=limit)
    rows = []
    for i, item in enumerate(raw or []):
        if len(item) < 6:
            continue
        rows.append({
            "index": i,
            "timestamp": item[0],
            "open": float(item[1]),
            "high": float(item[2]),
            "low": float(item[3]),
            "close": float(item[4]),
            "volume": float(item[5]),
        })
    return rows

async def _build_live_indicator_values(self, symbol, side, rows, idx, cfg):
    values = {
        "symbol": symbol,
        "timeframe": cfg.get("timeframe", "15m"),
        "utbot_direction": str(side).upper(),
    }
    row = rows[idx]
    values.update({
        "open": row["open"],
        "high": row["high"],
        "low": row["low"],
        "close": row["close"],
        "volume": row["volume"],
    })

    try:
        adx_values = self._calculate_live_adx_dmi(rows, cfg)
        values.update(adx_values)
    except Exception as e:
        logger.warning("[Live Context] ADX/DMI calculation failed for %s: %s", symbol, e)
        values["indicator_data_missing"] = True

    try:
        htf_values = await self._calculate_live_htf_trend(symbol, cfg)
        values.update(htf_values)
    except Exception as e:
        logger.warning("[Live Context] HTF trend calculation failed for %s: %s", symbol, e)
        values["htf_data_missing"] = True

    values.setdefault("squeeze_percentile", None)

    try:
        deriv_values = await self._fetch_live_derivatives_values(symbol, cfg)
        values.update(deriv_values)
    except Exception as e:
        logger.warning("[Live Context] derivatives fetch failed for %s: %s", symbol, e)
        values["derivatives_data_missing"] = True

    try:
        liquidity_values = await self._fetch_live_liquidity_values(symbol, cfg)
        values.update(liquidity_values)
    except Exception as e:
        logger.warning("[Live Context] liquidity fetch failed for %s: %s", symbol, e)
        values["liquidity_data_missing"] = True

    return values

def _calculate_live_adx_dmi(self, rows, cfg):
    length = int(cfg.get("adx_length", 14) or 14)
    if len(rows) < length + 2:
        return {"indicator_data_missing": True}

    highs = [float(r["high"]) for r in rows]
    lows = [float(r["low"]) for r in rows]
    closes = [float(r["close"]) for r in rows]

    tr_list = []
    plus_dm_list = []
    minus_dm_list = []

    for i in range(1, len(rows)):
        high_diff = highs[i] - highs[i - 1]
        low_diff = lows[i - 1] - lows[i]

        plus_dm = high_diff if high_diff > low_diff and high_diff > 0 else 0.0
        minus_dm = low_diff if low_diff > high_diff and low_diff > 0 else 0.0

        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )

        tr_list.append(tr)
        plus_dm_list.append(plus_dm)
        minus_dm_list.append(minus_dm)

    if len(tr_list) < length:
        return {"indicator_data_missing": True}

    atr = sum(tr_list[-length:]) / length
    plus_dm_sum = sum(plus_dm_list[-length:])
    minus_dm_sum = sum(minus_dm_list[-length:])

    if atr <= 0:
        return {"indicator_data_missing": True}

    plus_di = 100.0 * plus_dm_sum / max(sum(tr_list[-length:]), 1e-12)
    minus_di = 100.0 * minus_dm_sum / max(sum(tr_list[-length:]), 1e-12)
    dx = 100.0 * abs(plus_di - minus_di) / max(plus_di + minus_di, 1e-12)
    adx = dx

    atr_values = []
    for j in range(length, len(rows)):
        window_tr = tr_list[max(0, j - length):j]
        if window_tr:
            atr_values.append(sum(window_tr) / len(window_tr))

    current_atr = atr
    if atr_values:
        below = sum(1 for v in atr_values if v <= current_atr)
        atr_percentile = (below / len(atr_values)) * 100.0
    else:
        atr_percentile = None

    return {
        "atr": current_atr,
        "adx": adx,
        "plus_di": plus_di,
        "minus_di": minus_di,
        "atr_percentile": atr_percentile,
    }

async def _calculate_live_htf_trend(self, symbol, cfg):
    htf = cfg.get("htf_timeframe", cfg.get("higher_timeframe", "1h"))
    limit = max(220, int(cfg.get("htf_ohlcv_limit", 250) or 250))
    raw = await asyncio.to_thread(self.exchange.fetch_ohlcv, symbol, htf, limit=limit)
    rows = []
    for item in raw or []:
        if len(item) < 6:
            continue
        rows.append({
            "timestamp": item[0],
            "open": float(item[1]),
            "high": float(item[2]),
            "low": float(item[3]),
            "close": float(item[4]),
            "volume": float(item[5]),
        })
    closes = [float(item["close"]) for item in _closed_live_ohlcv_rows(rows, {**cfg, "timeframe": htf})]
    if len(closes) < 200:
        return {
            "htf_trend": "FLAT",
            "supertrend_direction": "FLAT",
            "htf_data_missing": True,
        }

    ema50 = self._ema_last(closes, 50)
    ema200 = self._ema_last(closes, 200)

    if ema50 > ema200 and closes[-1] > ema50:
        trend = "UP"
    elif ema50 < ema200 and closes[-1] < ema50:
        trend = "DOWN"
    else:
        trend = "FLAT"

    return {
        "htf_trend": trend,
        "supertrend_direction": trend,
        "htf_ema50": ema50,
        "htf_ema200": ema200,
    }

def _ema_last(self, values, length):
    if not values:
        return 0.0
    alpha = 2.0 / (length + 1.0)
    ema = float(values[0])
    for value in values[1:]:
        ema = alpha * float(value) + (1.0 - alpha) * ema
    return ema

async def _fetch_live_derivatives_values(self, symbol, cfg):
    values = {}
    try:
        funding_info = await asyncio.to_thread(self.exchange.fetch_funding_rate, symbol)
        if funding_info and "fundingRate" in funding_info:
            values["funding_rate"] = float(funding_info["fundingRate"])
    except Exception as e:
        logger.debug("Failed to fetch funding rate: %s", e)

    values.setdefault("funding_rate", None)
    values.setdefault("oi_change_pct", None)
    values.setdefault("long_short_ratio", None)
    return values

async def _fetch_live_liquidity_values(self, symbol, cfg):
    try:
        ob = await asyncio.to_thread(self.exchange.fetch_order_book, symbol, 5)
        if ob and ob.get("bids") and ob.get("asks"):
            bid = float(ob["bids"][0][0])
            ask = float(ob["asks"][0][0])
            spread = ask - bid
            mid = (ask + bid) / 2.0
            if mid > 0:
                spread_bps = (spread / mid) * 10000.0
                return {"spread_bps": spread_bps}
    except Exception as e:
        logger.debug("Failed to fetch liquidity values: %s", e)
    return {"spread_bps": None}

def _validate_live_context_or_block(self, symbol, context, cfg):
    cfg = cfg if isinstance(cfg, dict) else {}
    reasons = []
    quality = getattr(context, "quality", None)
    require_derivatives = bool(cfg.get("require_derivatives_data_for_advanced_alpha", False))
    if quality is not None:
        for item in list(getattr(quality, "reasons", ()) or ()):
            if item == "MISSING_DERIVATIVES_DATA" and not require_derivatives:
                continue
            reasons.append(item)

    if bool(cfg.get("require_live_dmi_for_advanced_alpha", True)):
        if float(getattr(context, "plus_di", 0.0) or 0.0) <= 0 and float(getattr(context, "minus_di", 0.0) or 0.0) <= 0:
            reasons.append("MISSING_DMI")

    if bool(cfg.get("require_live_htf_for_advanced_alpha", True)):
        if str(getattr(context, "htf_trend", "FLAT")).upper() == "FLAT":
            reasons.append("MISSING_HTF_TREND")

    derivatives_missing = (
        getattr(context, "funding_rate", None) is None
        or getattr(context, "oi_change_pct", None) is None
        or getattr(context, "long_short_ratio", None) is None
    )
    if require_derivatives:
        if derivatives_missing:
            reasons.append("MISSING_DERIVATIVES_DATA")
    elif derivatives_missing:
        logger.info(
            "[Live Parity] derivatives data incomplete for %s; keeping signal eligible with reduced confidence/size overlays",
            symbol,
        )

    if getattr(context, "spread_bps", None) is None:
        reasons.append("MISSING_SPREAD")

    if float(getattr(context, "spread_bps", 999.0) or 999.0) > float(cfg.get("max_spread_bps", 10.0) or 10.0):
        reasons.append("BAD_LIQUIDITY")

    if reasons:
        logger.warning("[Live Parity] context blocked for %s: %s", symbol, reasons)
        return None
    return context


def _infer_live_utbot_direction_from_rows(rows, cfg):
    hint = str(
        (cfg or {}).get("live_real_side_hint")
        or (cfg or {}).get("live_signal_side")
        or (cfg or {}).get("utbot_direction")
        or ""
    ).upper()
    if hint in {"LONG", "SHORT"}:
        return hint

    cfg = cfg or {}
    key_value = float(cfg.get("utbot_key_value", cfg.get("key_value", 2.5)) or 2.5)
    atr_period = max(1, int(cfg.get("utbot_atr_period", cfg.get("atr_period", 14)) or 14))
    if not rows or len(rows) < max(atr_period + 3, 20):
        return ""
    try:
        closed_rows = _closed_live_ohlcv_rows(rows, cfg)
        closed = pd.DataFrame(closed_rows).copy().reset_index(drop=True)
        if len(closed) < max(atr_period + 3, 20):
            closed = pd.DataFrame(rows).copy().reset_index(drop=True)
        prev_close = closed["close"].shift(1)
        true_range = pd.concat([
            (closed["high"] - closed["low"]).abs(),
            (closed["high"] - prev_close).abs(),
            (closed["low"] - prev_close).abs(),
        ], axis=1).max(axis=1)
        atr_series = pd.Series(np.nan, index=closed.index, dtype=float)
        if len(true_range) >= atr_period:
            atr_series.iloc[atr_period - 1] = true_range.iloc[:atr_period].mean()
            for idx in range(atr_period, len(true_range)):
                atr_series.iloc[idx] = ((atr_series.iloc[idx - 1] * (atr_period - 1)) + true_range.iloc[idx]) / atr_period
        valid = pd.DataFrame({
            "src": closed["close"].astype(float),
            "atr": atr_series.astype(float),
        }).dropna().reset_index(drop=True)
        if len(valid) < 3:
            return ""
        valid["nloss"] = valid["atr"] * key_value
        trail = []
        for idx, row in valid.iterrows():
            src_val = float(row["src"])
            nloss_val = float(row["nloss"])
            if idx == 0:
                trail.append(src_val - nloss_val)
                continue
            prev_stop = float(trail[-1])
            prev_src = float(valid.iloc[idx - 1]["src"])
            if src_val > prev_stop and prev_src > prev_stop:
                trail.append(max(prev_stop, src_val - nloss_val))
            elif src_val < prev_stop and prev_src < prev_stop:
                trail.append(min(prev_stop, src_val + nloss_val))
            elif src_val > prev_stop:
                trail.append(src_val - nloss_val)
            else:
                trail.append(src_val + nloss_val)
        curr_src = float(valid.iloc[-1]["src"])
        curr_stop = float(trail[-1])
        if curr_src > curr_stop:
            return "LONG"
        if curr_src < curr_stop:
            return "SHORT"
    except Exception as exc:
        logger.warning("[Live Real] UT direction inference failed: %s", exc)
    return ""


async def build_live_context_for_symbol(self, symbol, cfg):
    cfg = cfg if isinstance(cfg, dict) else {}
    raw_rows = await self._fetch_live_ohlcv_rows_for_context(symbol, cfg)
    rows = _closed_live_ohlcv_rows(raw_rows, cfg)
    if not rows or len(rows) < int(cfg.get("live_context_min_bars", 80) or 80):
        raise TradingSafetyError(f"insufficient live OHLCV context for {symbol}")

    idx = len(rows) - 1
    side = _infer_live_utbot_direction_from_rows(rows, cfg)
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
        raise TradingSafetyError(f"invalid live context for {symbol}")
    try:
        object.__setattr__(context, "current_candle", rows[idx])
    except Exception:
        pass
    cfg["last_price"] = float(getattr(context, "close", 0.0) or 0.0)
    cfg["last_context_close"] = cfg["last_price"]
    cfg["last_context_atr"] = float(getattr(context, "atr", 0.0) or 0.0)
    return context


def get_runtime_models(self=None, cfg=None):
    return getattr(self, "models", None) or {}


def get_runtime_stats(self=None, symbol=None, cfg=None):
    return ENGINE_PERFORMANCE_STATS


def _live_real_position_contracts(self, position):
    try:
        return abs(float(self._position_signed_contracts(position) or 0.0))
    except Exception:
        pass
    if not isinstance(position, dict):
        return 0.0
    info = position.get("info", {}) if isinstance(position.get("info"), dict) else {}
    for source in (position, info):
        for key in ("contracts", "contract", "amount", "positionAmt", "position_amt", "size"):
            try:
                value = float(source.get(key, 0.0) or 0.0)
                if value:
                    return abs(value)
            except Exception:
                continue
    return 0.0


async def preflight_live_real_check(self, symbol, cfg):
    cfg = enforce_activation_stage(cfg if isinstance(cfg, dict) else {})
    if _normalize_live_real_stage(cfg.get("live_activation_stage")) != "LIVE_REAL_SMALL_CAP":
        raise TradingSafetyError("preflight_live_real_check requires LIVE_REAL_SMALL_CAP")
    if getattr(self, "is_upbit_mode", lambda: False)():
        raise TradingSafetyError("LIVE_REAL_SMALL_CAP is Binance Futures only")
    if bool(cfg.get("testnet", False)):
        raise TradingSafetyError("LIVE_REAL_SMALL_CAP must not use testnet")
    if not bool(cfg.get("real_order_enabled", False)) or not bool(cfg.get("live_trading", False)):
        raise TradingSafetyError("LIVE_REAL_SMALL_CAP real order flags are not enabled")

    assert_symbol_allowed_for_live_real(symbol, cfg)
    configure_binance_futures_environment(self.exchange, cfg)
    if hasattr(self.exchange, "load_markets"):
        await asyncio.to_thread(self.exchange.load_markets)
    account_equity = await self._fetch_futures_account_equity(cfg)
    if float(account_equity) <= 0:
        raise TradingSafetyError("real futures account equity must be positive")
    limits = apply_live_small_cap_limits_to_config(cfg, account_equity)
    logger.warning(
        "LIVE_REAL_EFFECTIVE_LIMITS symbol=%s equity=%.4f risk_pct=%.4f "
        "max_notional=%.4f max_loss_per_trade=%.4f daily_loss_limit=%.4f "
        "weekly_loss_limit=%.4f timezone=%s requested_abs_notional=%s "
        "requested_abs_loss=%s",
        symbol,
        float(limits.get('account_equity_usdt', 0.0) or 0.0),
        float(limits.get('risk_pct', 0.0) or 0.0),
        float(limits.get('max_position_notional_usdt', 0.0) or 0.0),
        float(limits.get('max_loss_per_trade_usdt', 0.0) or 0.0),
        float(limits.get('max_daily_loss_usdt', 0.0) or 0.0),
        float(limits.get('max_weekly_loss_usdt', 0.0) or 0.0),
        limits.get('loss_period_timezone'),
        limits.get('requested_absolute_max_notional_usdt'),
        limits.get('requested_absolute_max_loss_usdt'),
    )
    assert_live_real_loss_limits_not_exceeded(cfg)
    state = load_critical_pause_state()
    if state and state.get("status") == "CRITICAL_PAUSED":
        raise TradingPausedError(f"TRADING_CRITICAL_PAUSED: {state.get('reason')}")

    await self.assert_binance_futures_one_way_mode(cfg)

    active_positions = []
    if hasattr(self.exchange, "fetch_positions"):
        try:
            positions = await asyncio.to_thread(self.exchange.fetch_positions)
        except Exception as exc:
            raise TradingSafetyError(f"could not verify open futures positions: {exc}") from exc
        for pos in positions or []:
            if _live_real_position_contracts(self, pos) > 0:
                active_positions.append(pos)
    elif hasattr(self, "get_server_position"):
        pos = await self.get_server_position(symbol, use_cache=False)
        if pos and _live_real_position_contracts(self, pos) > 0:
            active_positions.append(pos)
    else:
        raise TradingSafetyError("could not verify open futures positions")
    if active_positions:
        raise TradingSafetyError("LIVE_REAL_SMALL_CAP blocks new entries while any futures position is open")

    open_orders = []
    if not hasattr(self.exchange, "fetch_open_orders"):
        raise TradingSafetyError("could not verify open futures orders")
    for args in ((symbol,), tuple()):
        try:
            fetched = await asyncio.to_thread(self.exchange.fetch_open_orders, *args)
        except Exception as exc:
            raise TradingSafetyError(f"could not verify open futures orders: {exc}") from exc
        open_orders.extend(fetched or [])
    if open_orders:
        raise TradingSafetyError("LIVE_REAL_SMALL_CAP blocks new entries while open orders exist")

    return {
        "status": "OK",
        "symbol": symbol,
        "account_equity": float(account_equity),
        "live_real_limits": dict(limits),
        "open_positions": 0,
        "open_orders": 0,
    }


async def run_live_real_once(self, symbol, cfg):
    cfg = enforce_activation_stage(cfg if isinstance(cfg, dict) else {})
    if _normalize_live_real_stage(cfg.get("live_activation_stage")) != "LIVE_REAL_SMALL_CAP":
        raise TradingSafetyError("run_live_real_once requires LIVE_REAL_SMALL_CAP")
    preflight = await self.preflight_live_real_check(symbol, cfg)
    context = await self.build_live_context_for_symbol(symbol, cfg)
    decision = evaluate_final_trade_decision(
        candle=getattr(context, "current_candle", None),
        context=context,
        config=cfg,
        models=self.get_runtime_models(cfg),
        stats=self.get_runtime_stats(symbol, cfg),
    )
    if not decision.valid:
        return {
            "status": "NO_TRADE",
            "symbol": symbol,
            "reason": list(decision.reasons or []),
        }

    account_equity = float(preflight.get("account_equity") or await self._fetch_futures_account_equity(cfg))
    plan = build_live_order_plan_from_decision(
        symbol,
        decision,
        context,
        cfg,
        account_equity=account_equity,
    )
    plan = enforce_live_real_order_caps(plan, context, cfg)
    cfg["last_price"] = float(getattr(context, "close", 0.0) or cfg.get("last_price") or 0.0)
    return await self.execute_live_order_plan(plan, cfg)

__all__ = (
    '_live_timeframe_to_ms',
    '_live_timestamp_to_ms',
    '_live_context_now_ms',
    '_closed_live_ohlcv_rows',
    '_fetch_live_ohlcv_rows_for_context',
    '_build_live_indicator_values',
    '_calculate_live_adx_dmi',
    '_calculate_live_htf_trend',
    '_ema_last',
    '_fetch_live_derivatives_values',
    '_fetch_live_liquidity_values',
    '_validate_live_context_or_block',
    '_infer_live_utbot_direction_from_rows',
    'build_live_context_for_symbol',
    'get_runtime_models',
    'get_runtime_stats',
    '_live_real_position_contracts',
    'preflight_live_real_check',
    'run_live_real_once',
)
