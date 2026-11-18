"""Golden fixture generator for the tbot options-trading port.

Runs the Python ``options_trading`` pure functions (strategy, risk, config)
on deterministic pseudo-random inputs and records the results, so the Rust
port (``tbot/src/core/options_strategy.rs`` and
``tbot/src/strategy/options_config.rs``) can be compared case by case.

Usage: python scripts/tbot_golden_options.py [out.json]
"""

from __future__ import annotations

import json
import math
import random
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from options_trading import strategy as st  # noqa: E402
from options_trading import risk  # noqa: E402
from options_trading import config as oc  # noqa: E402

NOW_MS = 1_763_000_000_000  # fixed: never depend on the real date
DAY_MS = 86_400_000


def candles(rng, count, *, start=100.0, interval_ms=3_600_000, drift=0.0, vol=0.01, squeeze_tail=0):
    rows = []
    price = start
    for index in range(count):
        local_vol = vol
        if squeeze_tail and index >= count - squeeze_tail - 1 and index < count - 1:
            local_vol = vol * 0.2
        change = rng.gauss(drift, local_vol)
        if squeeze_tail and index == count - 1:
            change = abs(change) + vol * 3 * (1 if rng.random() < 0.5 else -1)
        open_ = price
        close = max(0.01, price * (1.0 + change))
        high = max(open_, close) * (1.0 + abs(rng.gauss(0, local_vol / 2)))
        low = min(open_, close) * (1.0 - abs(rng.gauss(0, local_vol / 2)))
        volume = 1000.0 * (1.0 + rng.random())
        if squeeze_tail and index == count - 1:
            volume *= 2.5
        rows.append([1_700_000_000_000 + index * interval_ms, open_, high, low, close, volume])
        price = close
    return rows


def trend_rows(count, *, start=100.0, slope=0.35, interval_ms=3_600_000):
    rows = []
    for index in range(count):
        close = start + slope * index + ((index % 5) - 2) * 0.04
        rows.append([1_700_000_000_000 + index * interval_ms, close - 0.1, close + 0.4, close - 0.4, close, 1000.0])
    return rows


def rand_cfg(rng):
    cfg = {}
    pool = {
        "min_abs_signal": [0.3, 0.46, 0.6, 0.2],
        "strong_signal": [0.5, 0.68, 0.8],
        "squeeze_min_score": [0.35, 0.58, 0.7],
        "squeeze_compression_ratio": [0.6, 0.78, 0.95],
        "squeeze_volume_multiplier": [1.0, 1.15, 2.0],
        "min_dte_days": [1.0, 2.0, 3.0],
        "max_dte_days": [10.0, 21.0, 35.0],
        "target_otm_pct": [0.0, 0.02, 0.05],
        "preferred_delta_min": [0.3, 0.35, 0.4],
        "preferred_delta_max": [0.5, 0.55, 0.6],
        "max_candidates_per_underlying": [3, 10, None, 0, "5"],
        "max_spread_pct": [0.04, 0.18, 0.4],
        "min_abs_delta": [0.1, 0.2, 0.3],
        "max_abs_delta": [0.6, 0.7, 0.9],
        "min_quote_volume_usdt": [0.0, 50.0, 500.0],
        "hard_max_iv_to_realized": [1.5, 2.4, 4.0],
        "max_iv_to_realized": [1.2, 1.6, 2.0],
        "squeeze_max_iv_to_realized": [1.0, 1.35],
        "min_flow_quote_usdt": [0.0, 25.0],
        "hard_negative_flow_score": [-0.9, -0.7, -0.2],
        "max_surface_iv_premium_pct": [0.05, 0.3, 1.0],
        "min_net_expected_edge_pct": [-1.0, 0.0, 0.04, 0.2],
        "min_ioc_net_expected_edge_pct": [0.0, 0.1, 0.5],
        "directional_move_scale": [0.2, 1.1, 2.0],
    }
    for key, values in pool.items():
        if rng.random() < 0.45:
            cfg[key] = rng.choice(values)
    return cfg


def exchange_info(rng, spot, underlying="BTCUSDT"):
    rows = []
    expiries = [NOW_MS + int(d * DAY_MS) for d in (0.5, 1.5, 3.2, 5.1, 8.0, 14.0, 22.0, 40.0)]
    strikes = [round(spot * (0.85 + 0.025 * i)) for i in range(13)]
    for expiry in expiries:
        for strike in strikes:
            for side in ("CALL", "PUT"):
                if rng.random() < 0.15:
                    continue
                row = {
                    "symbol": f"BTC-{expiry}-{strike}-{side[0]}",
                    "underlying": underlying if rng.random() < 0.95 else "ETHUSDT",
                    "side": side if rng.random() < 0.97 else side.lower(),
                    "status": "TRADING" if rng.random() < 0.95 else "PAUSED",
                    "strikePrice": str(strike) if rng.random() < 0.5 else strike,
                    "expiryDate": expiry,
                    "unit": 1,
                    "minQty": "0.01",
                    "filters": [
                        {"filterType": "PRICE_FILTER", "tickSize": "5"},
                        {"filterType": "LOT_SIZE", "minQty": "0.01", "stepSize": "0.01"},
                    ],
                }
                if rng.random() < 0.05:
                    row["underlyingType"] = "INDEX"
                if rng.random() < 0.3:
                    row["openInterest"] = rng.random() * 50
                rows.append(row)
    return {"optionSymbols": rows}


def rand_signal(rng, spot):
    strategy = rng.choice(["ADAPTIVE_TREND", "LOW_IV_SQUEEZE", "adaptive_trend", None])
    signal = {
        "score": rng.uniform(-1, 1),
        "spot_price": spot,
        "realized_volatility": rng.choice([0.0, 0.3, 0.5, 0.8]),
        "forecast_volatility": rng.choice([None, 0.02, 0.4, 0.7]),
        "expected_return_24h": rng.uniform(-0.05, 0.05),
        "components": {
            "slow_timeframe": rng.uniform(-1, 1),
            "medium": rng.uniform(-1, 1),
            "breakout": rng.uniform(-1, 1),
        },
    }
    if strategy is not None:
        signal["strategy"] = strategy
    return signal


def rand_trades(rng):
    trades = []
    for _ in range(rng.randint(0, 12)):
        trade = {}
        if rng.random() < 0.6:
            trade["quoteQty"] = str(rng.uniform(0, 500))
        trade["price"] = rng.uniform(1, 200)
        if rng.random() < 0.5:
            trade["qty"] = rng.uniform(-3, 3)
        else:
            trade["quantity"] = str(rng.uniform(0, 3))
        trade["side"] = rng.choice(["BUY", "SELL", "b", " s ", 1, -1, "1", "-1", "+1", None, "X", 0, 2.5])
        trades.append(trade)
    if rng.random() < 0.1:
        trades.append("garbage")
    return trades


def rand_depth(rng, mid):
    bids, asks = [], []
    for i in range(rng.randint(0, 7)):
        bids.append([str(max(0.0, mid * (0.95 - 0.01 * i))), str(rng.uniform(0, 5))])
    for i in range(rng.randint(0, 7)):
        asks.append([str(mid * (1.05 + 0.01 * i)), str(rng.uniform(0, 5))])
    if rng.random() < 0.05 and bids:
        bids[0] = [bids[0][0]]
    return {"bids": bids, "asks": asks}


def main() -> None:
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO_ROOT / "tbot/tests/golden_options.json"
    rng = random.Random(20261118)
    cases = []

    def add(fn, args, result):
        cases.append({"fn": fn, "args": args, "result": result})

    # forecast / realized volatility
    for i in range(40):
        n = rng.choice([0, 5, 13, 20, 25, 30, 80, 200, 240])
        closes = [c[4] for c in candles(rng, n, vol=rng.choice([0.002, 0.01, 0.03]))]
        if n and rng.random() < 0.2:
            closes[rng.randrange(n)] = 0.0
        add("forecast_realized_volatility", {"closes": closes}, st.forecast_realized_volatility(closes))
        size = rng.choice([12, 18, 24, 72])
        add("realized_volatility", {"closes": closes, "sample_size": size},
            st._realized_volatility(closes, 24 * 365, sample_size=size))

    # expected edge
    for i in range(80):
        args = {
            "side": rng.choice(["CALL", "PUT", "call", "", "X"]),
            "spot_price": rng.choice([0.0, 100.0, 95000.0]),
            "strike_price": rng.choice([0.0, 90.0, 105.0, 98000.0, 91000.0]),
            "dte_days": rng.choice([0.1, 1.0, 3.5, 9.0, 30.0]),
            "option_price": rng.choice([0.0, 0.5, 3.0, 1200.0, 2500.0]),
            "forecast_volatility": rng.choice([0.0, 0.01, 0.5, 5.0]),
            "signal_score": rng.uniform(-1, 1),
            "expected_return_24h": rng.uniform(-0.1, 0.1),
            "spread": rng.uniform(0, 50),
            "directional_move_scale": rng.choice([0.2, 1.1, 2.0]),
        }
        add("expected_option_net_edge", args, st.expected_option_net_edge(**args))

    # flow
    for i in range(60):
        trades = rand_trades(rng)
        depth = rand_depth(rng, rng.uniform(1, 100))
        add("evaluate_option_flow", {"recent_trades": trades, "depth_payload": depth},
            st.evaluate_option_flow(trades, depth))

    # underlying signals
    for i in range(60):
        n_fast = rng.choice([50, 95, 109, 120, 239])
        n_slow = rng.choice([25, 40, 60, 159])
        drift = rng.choice([-0.004, -0.001, 0.0, 0.001, 0.004])
        squeeze = rng.choice([0, 0, 20])
        fast = candles(rng, n_fast, drift=drift, vol=rng.choice([0.004, 0.01]), squeeze_tail=squeeze)
        slow = candles(rng, n_slow, interval_ms=14_400_000, drift=drift * 3, vol=0.02)
        cfg = rand_cfg(rng)
        args = {"fast_rows": fast, "slow_rows": slow, "cfg": cfg}
        add("evaluate_underlying_trend", args, st.evaluate_underlying_trend(fast, slow, cfg))
        add("evaluate_low_iv_squeeze", args, st.evaluate_low_iv_squeeze(fast, slow, cfg))
        add("choose_underlying_signal", args, st.choose_underlying_signal(fast, slow, cfg))
    fast = trend_rows(180)
    slow = trend_rows(100, slope=1.1, interval_ms=14_400_000)
    args = {"fast_rows": fast, "slow_rows": slow, "cfg": {"min_abs_signal": 0.45}}
    add("evaluate_underlying_trend", args, st.evaluate_underlying_trend(fast, slow, args["cfg"]))
    add("choose_underlying_signal", args, st.choose_underlying_signal(fast, slow, args["cfg"]))

    # targets
    for i in range(40):
        signal = rand_signal(rng, 100.0)
        signal["score"] = rng.choice([0.3, 0.56, 0.6, 0.68, 0.7, 0.78, 0.9, -0.95])
        cfg = rand_cfg(rng)
        add("derive_dynamic_contract_targets", {"signal": signal, "cfg": cfg},
            st.derive_dynamic_contract_targets(signal, cfg))

    # shortlist + peers
    for i in range(25):
        spot = rng.choice([95000.0, 100000.0])
        info = exchange_info(rng, spot)
        signal = rand_signal(rng, spot)
        cfg = rand_cfg(rng)
        direction = rng.choice(["CALL", "PUT", "call"])
        rows, diag = st.shortlist_option_contracts_with_diagnostics(
            info, "BTCUSDT", direction, spot, signal=signal, cfg=cfg, now_ms=NOW_MS
        )
        add("shortlist_option_contracts_with_diagnostics",
            {"exchange_info": info, "underlying": "BTCUSDT", "direction": direction,
             "spot_price": spot, "signal": signal, "cfg": cfg, "now_ms": NOW_MS},
            {"rows": rows, "diagnostics": diag})
        for contract in info["optionSymbols"][:: max(1, len(info["optionSymbols"]) // 6)]:
            add("find_skew_peer", {"exchange_info": info, "contract": contract},
                st.find_skew_peer(info, contract))
            peers_n = rng.choice([0, 1, 4])
            add("find_surface_peers", {"exchange_info": info, "contract": contract, "max_peers": peers_n},
                st.find_surface_peers(info, contract, max_peers=peers_n))

    # contract scoring
    for i in range(160):
        spot = 100000.0
        strike = rng.choice([95000.0, 100000.0, 104000.0])
        side = rng.choice(["CALL", "PUT"])
        contract = {
            "symbol": "BTC-X",
            "side": side,
            "strikePrice": str(strike),
            "min_qty": rng.choice([0.01, 0.1]),
            "tick_size": rng.choice([0.0, 5.0]),
            "dte_days": rng.choice([2.0, 5.0, 9.0, 20.0]),
        }
        if rng.random() < 0.5:
            contract["target_delta"] = rng.choice([0.4, 0.5])
            contract["target_dte_days"] = rng.choice([5.0, 9.0])
        if rng.random() < 0.3:
            contract["openInterest"] = rng.uniform(0, 100)
        mid = rng.choice([300.0, 1500.0, 3000.0])
        mark_row = {
            "markPrice": mid,
            "delta": rng.choice([0.05, 0.3, 0.45, -0.45, 0.8]),
            "markIV": rng.choice([0.0, 0.3, 0.5, 0.9, 2.0]),
        }
        greek_case = rng.choice(["lower", "upper", "none"])
        if greek_case == "lower":
            mark_row.update({"gamma": rng.uniform(0, 0.001), "theta": -rng.uniform(0, 300), "vega": rng.uniform(0, 100)})
        elif greek_case == "upper":
            mark_row.update({"GAMMA": rng.uniform(0, 0.001), "THETA": -rng.uniform(0, 300), "VEGA": rng.uniform(0, 100)})
        mark = rng.choice([[mark_row], mark_row, []])
        ticker_row = {
            "bidPrice": str(mid * 0.97),
            "askPrice": str(mid * 1.03),
            "amount": rng.choice([0.0, 40.0, 600.0, 50000.0]),
        }
        if rng.random() < 0.4:
            ticker_row["openInterest"] = rng.uniform(0, 200)
        ticker = rng.choice([[ticker_row], ticker_row])
        depth = rand_depth(rng, mid) if rng.random() < 0.8 else {}
        if rng.random() < 0.3:
            depth = {"bids": [[str(mid * 0.99), "2"]], "asks": [[str(mid * 1.01), "3"]]}
        signal = rand_signal(rng, spot)
        signal["score"] = (1 if side == "CALL" else -1) * rng.uniform(0.3, 1.0) * (1 if rng.random() < 0.85 else -1)
        cfg = rand_cfg(rng)
        skew = rng.choice([None, [{"markIV": rng.uniform(0.2, 1.0)}], {"markIV": 0}])
        surface = [rng.choice([[{"markIV": rng.uniform(0.2, 1.2)}], {"markIV": rng.uniform(0.2, 1.2)}, {}]) for _ in range(rng.randint(0, 4))]
        trades = rand_trades(rng)
        result = st.score_option_contract(
            contract, mark, ticker, depth, signal, cfg,
            skew_mark_payload=skew, surface_mark_payloads=surface, recent_trades=trades,
        )
        add("score_option_contract",
            {"contract": contract, "mark_payload": mark, "ticker_payload": ticker, "depth_payload": depth,
             "signal": signal, "cfg": cfg, "skew_mark_payload": skew, "surface_mark_payloads": surface,
             "recent_trades": trades},
            result)

    # contract scoring, mostly-passing inputs (exercise the soft-score branch)
    for i in range(80):
        spot = 100000.0
        side = rng.choice(["CALL", "PUT"])
        strike = rng.choice([98000.0, 100000.0, 102000.0])
        mid = rng.choice([800.0, 1500.0, 2600.0])
        contract = {
            "symbol": "BTC-Y", "side": side, "strikePrice": strike,
            "min_qty": 0.01, "tick_size": rng.choice([0.0, 5.0]), "dte_days": rng.choice([3.0, 7.0, 12.0]),
        }
        mark_row = {"markPrice": mid, "delta": rng.choice([0.35, 0.45, -0.5, 0.6]),
                    "markIV": rng.choice([0.35, 0.5, 0.7, 0.9]),
                    "gamma": rng.uniform(0, 0.001), "theta": rng.choice([0, -rng.uniform(0, 300)]),
                    "vega": rng.uniform(0, 100)}
        ticker = [{"bidPrice": str(mid * 0.99), "askPrice": str(mid * 1.01), "amount": rng.choice([800.0, 50000.0]),
                   "openInterest": rng.choice([0, 15.0])}]
        depth = {"bids": [[str(mid * 0.99), str(rng.uniform(0.5, 9))], [str(mid * 0.98), "1"]],
                 "asks": [[str(mid * 1.01), str(rng.uniform(0.5, 9))]]}
        signal = rand_signal(rng, spot)
        signal["realized_volatility"] = rng.choice([0.4, 0.5, 0.6])
        signal["score"] = (1 if side == "CALL" else -1) * rng.uniform(0.4, 1.0)
        cfg = {"min_net_expected_edge_pct": rng.choice([-1.0, -0.5, 0.0, 0.04]),
               "min_ioc_net_expected_edge_pct": rng.choice([-1.0, 0.1])}
        if rng.random() < 0.5:
            cfg["strong_signal"] = 0.6
        skew = rng.choice([None, [{"markIV": rng.uniform(0.3, 0.9)}]])
        surface = [[{"markIV": rng.uniform(0.4, 0.9)}] for _ in range(rng.randint(0, 4))]
        trades = rand_trades(rng)
        result = st.score_option_contract(contract, [mark_row], ticker, depth, signal, cfg,
                                          skew_mark_payload=skew, surface_mark_payloads=surface, recent_trades=trades)
        add("score_option_contract",
            {"contract": contract, "mark_payload": [mark_row], "ticker_payload": ticker, "depth_payload": depth,
             "signal": signal, "cfg": cfg, "skew_mark_payload": skew, "surface_mark_payloads": surface,
             "recent_trades": trades},
            result)

    # risk
    for i in range(80):
        args = {
            "option_price": rng.choice([0.0, 0.5, 12.0, 1500.0]),
            "index_price": rng.choice([0.0, 2000.0, 100000.0]),
            "quantity": rng.choice([0.0, 0.01, 1.0, 2.5]),
            "unit": rng.choice([1.0, 0.1]),
        }
        add("estimate_option_fee", args, risk.estimate_option_fee(
            args["option_price"], args["index_price"], args["quantity"], unit=args["unit"]))
    for i in range(120):
        args = {
            "ask_price": rng.choice([0.0, 5.0, 12.0, 100.0, 1505.0, 2995.0]),
            "index_price": rng.choice([0.0, 2000.0, 100000.0]),
            "unit": rng.choice([1.0, 0.1]),
            "min_qty": rng.choice([0.0, 0.01, 0.1, 1.0]),
            "step_size": rng.choice([0.0, 0.01, 0.1, 1.0]),
            "cash_bankroll_usdt": rng.choice([0.0, 20.0, 37.5, 250.0, 5000.0]),
            "entry_fraction": rng.choice([0.05, 0.7, 0.85, 1.0, 1.5]),
            "capital_limit_usdt": rng.choice([0.0, 10.0, 100.0]),
        }
        add("build_long_option_entry_plan", args, risk.build_long_option_entry_plan(**args))
    for value, step in [(1.234, 0.01), (5.0, 0.0), (-1.0, 0.1), (0.999, 0.25), (17.0, 3.0)]:
        add("floor_to_step", {"value": value, "step": step}, float(risk.floor_to_step(value, step)))

    # config
    defaults = oc.default_options_config()
    add("default_options_config", {}, defaults)
    options = [None, True, False, "on", "off", "yes", 0, 1, -5, 0.05, 0.5, 3, 7.5, 50, 999, 10_000_000,
               "abc", "12", [], {}, ["BTCUSDT", "ETH/USDT", "sol", "", None, "BTCUSDT", "XRPUSD"],
               ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"], "weekly", "monthly", "standard", "MONTHLY", "bad",
               0.04, 0.06, 0.08, 0.1, 0.8, 0.45, 0.35, 0.25]
    keys = list(defaults.keys()) + ["adaptive_convexity_v2_migration_complete", "wallet_budget_migration_complete"]
    for i in range(150):
        raw = {}
        for _ in range(rng.randint(0, 14)):
            key = rng.choice(keys)
            if key == "underlyings":
                # Python crashes on a non-iterable here; the Rust port falls back to defaults.
                raw[key] = rng.choice([None, [], {}, "BTCUSDT", options[20], options[21]])
            else:
                raw[key] = rng.choice(options)
        if rng.random() < 0.3:
            raw["btc_only"] = rng.choice([True, "true", 1, False])
        if rng.random() < 0.2:
            raw.update({"take_profit_pct": 0.80, "stop_loss_pct": 0.45, "trail_activation_pct": 0.35, "trail_drawdown_pct": 0.25})
        add("normalize_options_config", {"raw": raw}, oc.normalize_options_config(raw))
    add("normalize_options_config", {"raw": None}, oc.normalize_options_config(None))
    for value in [None, "x", 0.0, 0.05, 0.07, 0.08, 0.2, "0.04", 1]:
        add("normalize_btc_spread_choice", {"value": value}, oc.normalize_btc_spread_choice(value))
    for cap, avail in [(0, 50), (20, 50), (100, 50), (None, None), ("x", 3), (-5, 7)]:
        cfg = {"capital_limit_usdt": cap}
        add("options_spend_limit", {"cfg": cfg, "available": avail}, oc.options_spend_limit(cfg, avail))
        add("options_budget_text", {"cfg": cfg}, oc.options_budget_text(cfg))
    add("multi_underlying_restore_values", {}, oc.multi_underlying_restore_values())

    def clean(value):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("non-finite float in golden output")
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [clean(v) for v in value]
        return value

    out_path.write_text(json.dumps(clean(cases), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {len(cases)} cases to {out_path}")


if __name__ == "__main__":
    main()
