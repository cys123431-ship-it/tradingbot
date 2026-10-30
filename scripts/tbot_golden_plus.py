"""Generate golden vectors for the Rust port of the /macross pure helpers."""

from __future__ import annotations

import json
import math
import random
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from btc_ma_cross_plus import indicators as ind
from btc_ma_cross_plus import strategy as strat
from btc_ma_cross_plus.config import default_ma_cross_config

FEATURES = "ABCDE"


def make_candles(rng: random.Random, length: int) -> list:
    rows = []
    price = 20000.0 + rng.random() * 5000.0
    ts = 1_600_000_000_000
    step_ms = 4 * 3600 * 1000
    trend_dir = 1
    phase_left = rng.randint(20, 60)
    for _ in range(length):
        if phase_left <= 0:
            # Alternate up / down / sideways so both cross directions occur.
            trend_dir = rng.choice([1, -1, 0]) if trend_dir == 0 else rng.choice([-trend_dir, 0])
            phase_left = rng.randint(20, 60)
        phase_left -= 1
        drift = trend_dir * 0.0008 + rng.uniform(-0.004, 0.004)
        open_price = price
        close_price = price * (1.0 + drift)
        spread = abs(close_price - open_price) + price * rng.uniform(0.001, 0.006)
        high = max(open_price, close_price) + spread * rng.uniform(0.1, 0.5)
        low = min(open_price, close_price) - spread * rng.uniform(0.1, 0.5)
        if low <= 0:
            low = min(open_price, close_price) * 0.5
        volume = rng.uniform(10.0, 500.0) + 0.001
        rows.append([ts, open_price, high, low, close_price, volume])
        ts += step_ms
        price = close_price
    return rows


def encode(value):
    if isinstance(value, dict):
        return {str(k): encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [encode(v) for v in value]
    if isinstance(value, bool):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, float):
        if math.isnan(value):
            return "nan"
        if math.isinf(value):
            return "inf" if value > 0 else "-inf"
        return value
    if isinstance(value, int):
        return value
    return value


def clone_rows(rows):
    return [list(r) for r in rows]


def build_cases(candle_sets):
    cases = []

    def add(fn, args, expect):
        cases.append({"fn": fn, "args": args, "expect": encode(expect)})

    # --- base rows for reference ---
    ref = 0
    rows = candle_sets[ref]

    # indicators: sma / true_range / atr / trend_efficiency / donchian / avg_volume / adx
    closes = [float(r[4]) for r in rows]

    add("sma", {"values": [r[4] for r in rows[:120]], "period": 3}, ind.sma([r[4] for r in rows[:120]], 3))
    add("sma", {"values": [r[4] for r in rows[:200]], "period": 120}, ind.sma([r[4] for r in rows[:200]], 120))
    add("sma", {"values": [r[4] for r in rows[:2]], "period": 3}, ind.sma([r[4] for r in rows[:2]], 3))
    add("sma", {"values": [1.0, 2.0, 3.0, 4.0, 5.0], "period": 2}, ind.sma([1.0, 2.0, 3.0, 4.0, 5.0], 2))

    add("true_range", {"rows_ref": 0, "rows_len": 200}, ind.true_range(rows[:200]))
    add("atr", {"rows_ref": 0, "rows_len": 200, "period": 14}, ind.atr(rows[:200], 14))
    add("atr", {"rows_ref": 0, "rows_len": 10, "period": 14}, ind.atr(rows[:10], 14))
    add("trend_efficiency", {"values": closes[:150], "period": 20}, ind.trend_efficiency(closes[:150], 20))
    add("trend_efficiency", {"values": [1.0, 1.0, 1.0, 1.0, 1.0, 1.0], "period": 2}, ind.trend_efficiency([1.0, 1.0, 1.0, 1.0, 1.0, 1.0], 2))

    du, dl = ind.donchian(rows[:120], 20)
    add("donchian", {"rows_ref": 0, "rows_len": 120, "period": 20}, [du, dl])
    au = ind.avg_volume(rows[:120], 20)
    add("avg_volume", {"rows_ref": 0, "rows_len": 120, "period": 20}, au)
    add("adx", {"rows_ref": 0, "rows_len": 120, "period": 14}, ind.adx(rows[:120], 14))
    add("adx", {"rows_ref": 0, "rows_len": 20, "period": 14}, ind.adx(rows[:20], 14))

    add("floor_to_step", {"value": 12.3456, "step": 0.01}, ind.floor_to_step(12.3456, 0.01))
    add("floor_to_step", {"value": 12.3456, "step": 0.0}, ind.floor_to_step(12.3456, 0.0))
    add("floor_to_step", {"value": 12.3456, "step": -1.0}, ind.floor_to_step(12.3456, -1.0))

    add("volatility_target_scale", {"atr_value": 600.0, "close": 20000.0, "target_pct": 0.03, "min_scale": 0.5, "max_scale": 1.5}, ind.volatility_target_scale(600.0, 20000.0, 0.03, 0.5, 1.5))
    add("volatility_target_scale", {"atr_value": -1.0, "close": 20000.0, "target_pct": 0.03, "min_scale": 0.5, "max_scale": 1.5}, ind.volatility_target_scale(-1.0, 20000.0, 0.03, 0.5, 1.5))
    add("volatility_target_scale", {"atr_value": None, "close": 20000.0, "target_pct": 0.03, "min_scale": 0.5, "max_scale": 1.5}, ind.volatility_target_scale(None, 20000.0, 0.03, 0.5, 1.5))

    add("chandelier_stop", {"side": "LONG", "highest_high": 21000.0, "lowest_low": 19000.0, "atr_value": 500.0, "multiplier": 3.0, "previous": None, "breakeven": None}, ind.chandelier_stop("LONG", 21000.0, 19000.0, 500.0, 3.0))
    add("chandelier_stop", {"side": "SHORT", "highest_high": 21000.0, "lowest_low": 19000.0, "atr_value": 500.0, "multiplier": 3.0, "previous": 20500.0, "breakeven": 20000.0}, ind.chandelier_stop("SHORT", 21000.0, 19000.0, 500.0, 3.0, previous=20500.0, breakeven=20000.0))
    add("chandelier_stop", {"side": "LONG", "highest_high": 21000.0, "lowest_low": 19000.0, "atr_value": None, "multiplier": 3.0, "previous": None, "breakeven": None}, ind.chandelier_stop("LONG", 21000.0, 19000.0, None, 3.0))

    # --- strategy helpers ---
    default_cfg = default_ma_cross_config()
    default_cfg["feature_c_vol_sizing"] = 1
    default_cfg["feature_b_atr_stop"] = 1

    frame_default = strat.indicator_frame(rows, default_cfg)
    add("indicator_frame", {"rows_ref": 0, "rows_len": len(rows), "cfg": default_cfg}, frame_default)
    add("cross_side", {"frame_ref_case": "frame_default", "index": 200}, strat.cross_side(frame_default, 200))

    add("trend_filter_ok", {"rows_ref": 0, "rows_len": len(rows), "cfg": default_cfg, "index": 200, "side": "LONG"},
        strat.trend_filter_ok(frame_default, 200, "LONG", default_cfg))
    add("initial_stop", {"side": "LONG", "entry_price": 20000.0, "atr_value": 500.0, "cfg": default_cfg}, strat.initial_stop("LONG", 20000.0, 500.0, default_cfg))
    add("initial_stop", {"side": "SHORT", "entry_price": 20000.0, "atr_value": 500.0, "cfg": default_cfg}, strat.initial_stop("SHORT", 20000.0, 500.0, default_cfg))
    add("size_multiplier", {"atr_value": 500.0, "close": 20000.0, "cfg": default_cfg}, strat.size_multiplier(500.0, 20000.0, default_cfg))

    trade = {"highest_price": "20000.0", "lowest_price": "20000.0", "entry_price": "19900.0", "trail_price": None}
    add("trailing_stop", {"side": "LONG", "trade": trade, "price": 20500.0, "atr_value": 500.0, "cfg": default_cfg},
        strat.trailing_stop("LONG", trade, 20500.0, 500.0, default_cfg))

    # --- evaluate with default cfg: scan slices for LONG/SHORT entries ---
    def find_crosses(rows, cfg):
        longs, shorts = [], []
        for i in range(1, len(rows)):
            r = strat.evaluate(rows[: i + 1], cfg)
            if r["side"] == "LONG" and r["trigger"]:
                longs.append(i)
            elif r["side"] == "SHORT" and r["trigger"]:
                shorts.append(i)
        return longs, shorts

    picked = []
    want = {"LONG": 3, "SHORT": 3}
    for set_ref, set_rows in enumerate(candle_sets):
        longs, shorts = find_crosses(set_rows, default_cfg)
        for side, found in (("LONG", longs), ("SHORT", shorts)):
            for i in found[: want[side]]:
                want[side] -= 1
                if set_ref == 0:
                    picked.append(i)
                add("evaluate", {"rows_ref": set_ref, "rows_len": len(set_rows), "cfg": default_cfg, "index": i},
                    strat.evaluate(set_rows, default_cfg, index=i))
    assert not any(want.values()), f"not enough golden entries: {want}"

    # feature toggles for evaluate
    for feat in FEATURES:
        cfg_on = dict(default_cfg)
        for name in FEATURES:
            cfg_on["feature_%s_%s" % (
                name.lower(),
                "trend_filter" if name == "A" else
                "atr_stop" if name == "B" else
                "vol_sizing" if name == "C" else
                "chandelier" if name == "D" else "donchian",
            )] = 1 if name == feat else 0
        # pick an index where signal exists under default to exercise path
        idx = picked[0] if picked else 100
        add("evaluate", {"rows_ref": 0, "rows_len": len(rows), "cfg": cfg_on, "index": idx},
            strat.evaluate(rows, cfg_on, index=idx))
        cfg_off = dict(default_cfg)
        for name in FEATURES:
            cfg_off["feature_%s_%s" % (
                name.lower(),
                "trend_filter" if name == "A" else
                "atr_stop" if name == "B" else
                "vol_sizing" if name == "C" else
                "chandelier" if name == "D" else "donchian",
            )] = 0
        add("evaluate", {"rows_ref": 0, "rows_len": len(rows), "cfg": cfg_off, "index": idx},
            strat.evaluate(rows, cfg_off, index=idx))

    return cases


def main() -> None:
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("tbot/tests/golden_plus.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rng = random.Random(20240501)
    candle_sets = []
    for _ in range(4):
        length = rng.randint(250, 400)
        candle_sets.append(make_candles(rng, length))
    cases = build_cases(candle_sets)
    payload = {"candle_sets": [[list(r) for r in s] for s in candle_sets], "cases": cases}
    out_path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")


if __name__ == "__main__":
    main()
