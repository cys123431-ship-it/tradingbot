"""Golden fixture generator for tbot strategy config / session / tradfi helpers."""

from __future__ import annotations

import json
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _to_jsonable(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "isoformat") and not isinstance(value, str):
        try:
            return value.isoformat()
        except Exception:
            return str(value)
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    return value


def _dt(text):
    return datetime.fromisoformat(text)


def _randomize(base, rng, options):
    cfg = dict(base)
    keys = list(cfg.keys())
    rng.shuffle(keys)
    for _ in range(rng.randint(3, 12)):
        key = rng.choice(keys)
        cfg[key] = rng.choice(options)
    return cfg


def main() -> None:
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("tbot/tests/golden_config.json")

    from btc_ema_pullback.config import (
        default_btc_pullback_config,
        normalize_btc_pullback_config,
        effective_trading_mode,
        sizing_mode_for,
        TRADING_MODE_ENV,
    )
    from btc_ma_cross.config import (
        default_btc_ma_cross_config,
        normalize_btc_ma_cross_config,
        TIMEFRAMES as BTC_MA_TIMEFRAMES,
        SCAN_SCOPES as BTC_MA_SCAN_SCOPES,
        ENTRY_MODES as BTC_MA_ENTRY_MODES,
        EMERGENCY_STOP_CHOICES as BTC_MA_ESTOP,
        REENTRY_WAIT_CHOICES as BTC_MA_REENTRY,
        PROFIT_LOCK_SWITCH_CHOICES as BTC_MA_PLSW,
        LEVERAGE_CHOICES as BTC_MA_LEV,
    )
    from btc_ma_cross_plus.config import (
        default_ma_cross_config,
        normalize_ma_cross_config,
        TIMEFRAMES as PLUS_TIMEFRAMES,
        SCAN_SCOPES as PLUS_SCAN_SCOPES,
        ENTRY_MODES as PLUS_ENTRY_MODES,
        FEATURES as PLUS_FEATURES,
        FEATURE_FIELDS as PLUS_FEATURE_FIELDS,
        REENTRY_WAIT_CHOICES as PLUS_REENTRY,
        LEVERAGE_CHOICES as PLUS_LEV,
    )
    from trading_safety.market_session import (
        us_equity_regular_session_status,
        tradfi_primary_session_status,
    )
    from utbreakout.coinselector import (
        normalize_symbol,
        symbol_base,
        market_is_usdt_perpetual,
        market_is_tradifi_perpetual,
        market_tradifi_underlying_type,
    )
    from btc_ma_cross_plus.tradfi import (
        market_is_tradfi_gold,
        market_is_tradfi_us_equity,
        market_in_tradfi_scope,
    )
    normalizers = {
        "normalize_btc_pullback_config": normalize_btc_pullback_config,
        "normalize_btc_ma_cross_config": normalize_btc_ma_cross_config,
        "normalize_ma_cross_config": normalize_ma_cross_config,
    }

    cases = []

    def add(fn, args, result):
        cases.append({"fn": fn, "args": args, "expect": _to_jsonable(result)})

    # ---------- configs ----------
    raw_inputs = [
        None,
        {},
        default_btc_pullback_config(),
        default_btc_ma_cross_config(),
        default_ma_cross_config(),
    ]
    out_of_range = [
        -1,
        -1000000,
        10**15,
        "3",
        "abc",
        None,
        True,
        False,
        0,
        "",
    ]
    for norm_name, default_fn, in [
        ("normalize_btc_pullback_config", default_btc_pullback_config),
        ("normalize_btc_ma_cross_config", default_btc_ma_cross_config),
        ("normalize_ma_cross_config", default_ma_cross_config),
    ]:
        base = default_fn()
        rng = random.Random(42)
        configs = []
        configs.append(None)
        configs.append({})
        configs.append(dict(base))
        configs.append({"unknown_key": 1, "timeframe": "7m", "scan_scope": "foo"})
        for value in out_of_range:
            entry = dict(base)
            for key in list(entry.keys())[:5]:
                entry[key] = value
            configs.append(entry)
        # Each choice value.
        for v in BTC_MA_TIMEFRAMES + PLUS_TIMEFRAMES:
            configs.append({"timeframe": v})
        for v in BTC_MA_SCAN_SCOPES + PLUS_SCAN_SCOPES:
            configs.append({"scan_scope": v})
        for v in BTC_MA_ENTRY_MODES + PLUS_ENTRY_MODES:
            configs.append({"entry_mode": v})
        for v in BTC_MA_ESTOP:
            configs.append({"emergency_stop_roi_percent": v})
        for v in BTC_MA_REENTRY + PLUS_REENTRY:
            configs.append({"reentry_wait_bars": v})
        for v in BTC_MA_PLSW:
            configs.append({"profit_lock_switch_count": v})
        for v in BTC_MA_LEV + PLUS_LEV:
            configs.append({"leverage": v})
        for name, field in PLUS_FEATURE_FIELDS.items():
            configs.append({field: 1})
        for v in ("btc", "alts", "single", "tradfi", "unknown"):
            configs.append({"scan_scope": v})
        # 40+ random permutations.
        for _ in range(40):
            cfg = _randomize(base, rng, out_of_range)
            configs.append(cfg)
        for raw in configs:
            add(norm_name, {"raw": raw}, normalizers[norm_name](raw))

    # None explicit case.
    for norm_name, default_fn in [
        ("normalize_btc_pullback_config", default_btc_pullback_config),
        ("normalize_btc_ma_cross_config", default_btc_ma_cross_config),
        ("normalize_ma_cross_config", default_ma_cross_config),
    ]:
        add(norm_name, {"raw": None}, normalizers[norm_name](None))

    # effective_trading_mode
    base_live = {"trading_mode": "LIVE"}
    base_dry = {"trading_mode": "DRY_RUN"}
    env_cases = [
        (base_live, {}),
        (base_live, {TRADING_MODE_ENV: "DRY_RUN"}),
        (base_live, {TRADING_MODE_ENV: "LIVE"}),
        (base_live, {TRADING_MODE_ENV: "  dry_run  "}),
        (base_dry, {}),
        (base_dry, {TRADING_MODE_ENV: "LIVE"}),
        (None, {}),
        (None, {TRADING_MODE_ENV: "DRY_RUN"}),
        ({}, {}),
    ]
    for cfg, env in env_cases:
        res = effective_trading_mode(cfg, env)
        add("effective_trading_mode", {"cfg": cfg, "environ": env}, res)

    # sizing_mode_for
    sm_cases = [
        {"network": "testnet", "sizing_mode_testnet": "risk_based", "sizing_mode_mainnet": "fixed"},
        {"network": "mainnet", "sizing_mode_testnet": "risk_based", "sizing_mode_mainnet": "fixed"},
        {"network": "testnet", "sizing_mode_testnet": None, "sizing_mode_mainnet": "fixed"},
        {"network": "mainnet", "sizing_mode_testnet": "risk_based", "sizing_mode_mainnet": None},
        {"network": "mainnet"},
    ]
    for cfg in sm_cases:
        add("sizing_mode_for", {"cfg": cfg}, sizing_mode_for(cfg))

    # ---------- market session ----------
    def utc(y, m, d, h, mi, s=0):
        return datetime(y, m, d, h, mi, s, tzinfo=timezone.utc)

    times = [
        utc(2024, 1, 1, 14, 30),      # New Year
        utc(2024, 7, 4, 15, 0),       # Independence Day
        utc(2024, 11, 28, 14, 0),     # Thanksgiving holiday
        utc(2024, 11, 29, 18, 30),    # Day after Thanksgiving early close
        utc(2024, 12, 24, 18, 30),    # Christmas Eve early close
        utc(2025, 3, 9, 7, 0),        # DST start
        utc(2025, 11, 2, 6, 0),       # DST end
        utc(2025, 7, 4, 15, 0),       # 2025 Independence
        utc(2025, 12, 25, 15, 0),     # 2025 Christmas
        utc(2026, 7, 3, 15, 0),       # 2026 Independence observed (Jul 4 is Sat)
        utc(2026, 10, 30, 13, 25),    # session open
        utc(2026, 10, 30, 13, 29),    # just before open
        utc(2026, 10, 30, 20, 0),     # close boundary
        utc(2026, 10, 30, 19, 59),    # just before close
        utc(2026, 10, 31, 15, 0),     # weekend (Sat)
        utc(2026, 11, 1, 15, 0),      # weekend (Sun)
        utc(2027, 1, 1, 15, 0),       # 2027 New Year
        utc(2027, 11, 25, 15, 0),     # 2027 Thanksgiving
        utc(2027, 12, 24, 18, 30),    # 2027 Christmas Eve early close
    ]
    for t in times:
        add("us_equity_regular_session_status",
            {"now": t.isoformat()}, us_equity_regular_session_status(t))

    underlying_types = ["EQUITY", "COMMODITY", "", "KR_EQUITY", "HK_EQUITY", "CN_EQUITY", "BOND", None, "unknown"]
    for t in times[:8] + times[10:]:
        for ut in underlying_types:
            add("tradfi_primary_session_status",
                {"underlying_type": ut, "now": t.isoformat()},
                tradfi_primary_session_status(ut, t))

    # ---------- coinselect / tradfi with binance public sample ----------
    try:
        import ccxt
        fx_path = REPO_ROOT / "tbot" / "tests" / "fixtures" / "binance_public_sample.json"
        with open(fx_path, "r", encoding="utf-8") as fh:
            fx = json.load(fh)
        exchange = ccxt.binance({"options": {"defaultType": "future"}})
        symbols_raw = fx.get("symbols") or fx.get("exchangeInfo", {}).get("symbols") or []
        parsed_markets = []
        for sym in symbols_raw:
            try:
                market = exchange.parse_market(sym)
            except Exception:
                market = None
            parsed_markets.append((sym.get("symbol"), market))
    except Exception:
        parsed_markets = []

    for sym, market in parsed_markets:
        if sym is None:
            continue
        add("normalize_symbol", {"symbol": sym}, normalize_symbol(sym))
        add("symbol_base", {"symbol": sym}, symbol_base(sym))
        add("market_is_usdt_perpetual", {"symbol": sym, "market": market},
            market_is_usdt_perpetual(sym, market))
        add("market_is_tradifi_perpetual", {"symbol": sym, "market": market},
            market_is_tradifi_perpetual(sym, market))
        add("market_tradifi_underlying_type", {"market": market},
            market_tradifi_underlying_type(market))
        add("market_is_tradfi_gold", {"symbol": sym, "market": market},
            market_is_tradfi_gold(sym, market))
        add("market_is_tradfi_us_equity", {"symbol": sym, "market": market},
            market_is_tradfi_us_equity(sym, market))
        add("market_in_tradfi_scope", {"symbol": sym, "market": market},
            market_in_tradfi_scope(sym, market))

    edge_cases = [
        ("BTCUSDT", None),
        ("ETHUSDT", {}),
        ("XAUUSDT", {"info": {"contractType": "TRADIFI_PERPETUAL", "underlyingType": "COMMODITY", "underlyingSubType": "GOLD", "status": "TRADING"}, "swap": True, "type": "swap", "quote": "USDT", "settle": "USDT", "active": True}),
        ("AAPLUSDT", {"info": {"contractType": "TRADIFI_PERPETUAL", "underlyingType": "EQUITY", "status": "TRADING"}, "swap": True, "type": "swap", "quote": "USDT", "settle": "USDT", "active": True}),
        ("SILVERUSDT", {"info": {"contractType": "TRADIFI_PERPETUAL", "underlyingType": "COMMODITY", "underlyingSubType": "SILVER", "status": "TRADING"}, "swap": True, "type": "swap", "quote": "USDT", "settle": "USDT", "active": True}),
        ("OILUSDT", {"info": {"contractType": "TRADIFI_PERPETUAL", "underlyingType": "COMMODITY", "underlyingSubType": "OIL", "status": "TRADING"}, "swap": True, "type": "swap", "quote": "USDT", "settle": "USDT", "active": True}),
        ("AAPLUSDT", {"info": {"contractType": "TRADIFI_PERPETUAL", "underlyingType": "EQUITY", "status": "SETTLING"}, "swap": True, "type": "swap", "quote": "USDT", "settle": "USDT", "active": True}),
        ("AAPLUSDT", {"info": {"contractType": "TRADIFI_PERPETUAL", "underlyingType": "EQUITY", "status": "TRADING"}, "swap": True, "type": "swap", "quote": "USDC", "settle": "USDT", "active": True}),
        ("AAPLUSDT", {"info": {"contractType": "TRADIFI_PERPETUAL", "underlyingType": "EQUITY", "status": "TRADING"}, "swap": True, "type": "swap", "quote": "USDT", "settle": "USDC", "active": True}),
        ("xau/usdt", {"info": {"contractType": "TRADIFI_PERPETUAL", "underlyingType": "COMMODITY", "underlyingSubType": "GOLD", "status": "TRADING"}, "swap": True, "type": "swap", "quote": "USDT", "settle": "USDT", "active": True}),
        ("BTC/USDT", {"info": {"contractType": "PERPETUAL", "status": "TRADING"}, "swap": True, "type": "swap", "quote": "USDT", "settle": "USDT", "active": True}),
        ("BTC:USDT", None),
        ("btcusdt", {"info": {"contractType": "PERPETUAL", "status": "TRADING"}, "swap": True, "type": "swap", "quote": "USDT", "settle": "USDT", "active": False}),
    ]
    for sym, market in edge_cases:
        add("normalize_symbol", {"symbol": sym}, normalize_symbol(sym))
        add("symbol_base", {"symbol": sym}, symbol_base(sym))
        add("market_is_usdt_perpetual", {"symbol": sym, "market": market},
            market_is_usdt_perpetual(sym, market))
        add("market_is_tradifi_perpetual", {"symbol": sym, "market": market},
            market_is_tradifi_perpetual(sym, market))
        add("market_tradifi_underlying_type", {"market": market},
            market_tradifi_underlying_type(market))
        add("market_is_tradfi_gold", {"symbol": sym, "market": market},
            market_is_tradfi_gold(sym, market))
        add("market_is_tradfi_us_equity", {"symbol": sym, "market": market},
            market_is_tradfi_us_equity(sym, market))
        add("market_in_tradfi_scope", {"symbol": sym, "market": market},
            market_in_tradfi_scope(sym, market))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump({"cases": cases}, fh, ensure_ascii=False, allow_nan=False, indent=2)
    print(f"wrote {len(cases)} cases to {out_path}")


if __name__ == "__main__":
    main()
