"""Local-only backtest runner for /macross (NOT deployed to Azure).

Runs every feature combination (1..5 of A~E) on BTCUSDT 4h candles and prints
one comparison table, so single features and 2/3/4/5-feature combinations can
be compared before anything is traded.

Kline source, in order:
  1. ``--csv``    a local OHLCV CSV (open_time,open,high,low,close,volume)
  2. ``--cache``  a JSON file written by an earlier run (``--csv-out``)
  3. Binance public klines (--network mainnet|testnet) unless ``--offline``

Examples
--------
    python scripts/macross_backtest.py --offline --csv btc_4h.csv
    python scripts/macross_backtest.py --months 24 --csv-out btc_4h.json
    python scripts/macross_backtest.py --modes A,B,ABD --leverage 3
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from btc_ma_cross_plus.backtest import (  # noqa: E402
    all_feature_modes,
    cfg_for_mode,
    compare_modes,
    run_backtest,
)
from btc_ma_cross_plus.config import FEATURES, normalize_ma_cross_config  # noqa: E402

TF_MS = {"15m": 900_000, "30m": 1_800_000, "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000}
BAR_MS = TF_MS["4h"]  # default; --timeframe overrides it


def load_csv(path):
    rows = []
    with open(path, newline="", encoding="utf-8") as handle:
        for record in csv.reader(handle):
            if not record or len(record) < 6:
                continue
            try:
                rows.append([int(float(record[0])), float(record[1]), float(record[2]),
                             float(record[3]), float(record[4]), float(record[5])])
            except ValueError:
                continue  # header row
    return rows


def load_json_cache(path):
    with open(path, encoding="utf-8") as handle:
        raw = json.load(handle)
    return [[int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])] for r in raw]


def fetch_klines(network, symbol, months, timeframe="4h"):
    """Public klines from Binance (works for the demo endpoint too)."""
    import ccxt

    bar_ms = TF_MS[timeframe]
    exchange = ccxt.binanceusdm({"enableRateLimit": True})
    if network == "testnet":
        exchange.set_sandbox_mode(True)
    count = int(months * 30 * 24 * 3_600_000 / bar_ms) + 200
    rows = []
    since = exchange.milliseconds() - count * bar_ms
    while len(rows) < count:
        batch = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=1000)
        if not batch:
            break
        rows.extend([[int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])] for r in batch])
        since = rows[-1][0] + bar_ms
        if len(batch) < 2:
            break
    return rows[-count:]


def format_table(results, limit=None):
    header = f"{'기능':>5} | {'거래':>4} | {'승률':>6} | {'수익률':>9} | {'PF':>6} | {'MDD':>7} | {'평균손익':>10}"
    lines = [header, "-" * len(header)]
    for item in (results[:limit] if limit else results):
        pf = item["profit_factor"]
        pf_text = "inf" if pf == float("inf") else f"{pf:.2f}"
        lines.append(
            f"{item['mode']:>5} | {item['trades']:>4} | {item['win_rate']:>5.1f}% | "
            f"{item['return_pct']:>8.2f}% | {pf_text:>6} | {item['max_drawdown_pct']:>6.2f}% | "
            f"{item['avg_trade']:>10.2f}"
        )
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local-only /macross A~E comparison backtest")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--network", default="mainnet", choices=("mainnet", "testnet"))
    parser.add_argument("--timeframe", default="4h", choices=tuple(TF_MS), help="candle interval")
    parser.add_argument("--months", type=float, default=24.0)
    parser.add_argument("--equity", type=float, default=10_000.0)
    parser.add_argument("--leverage", type=int, default=5)
    parser.add_argument("--fee", type=float, default=0.0005)
    parser.add_argument("--slippage", type=float, default=0.0005)
    parser.add_argument("--funding", type=float, default=0.0001, help="funding rate per 8h")
    parser.add_argument("--modes", default="", help="subset such as A,AB,ABCDE")
    parser.add_argument("--lock", default="on", choices=("on", "off"),
                        help="profit lock (+5%%->+4%% ratchet); 'off' keeps only the exit you choose")
    parser.add_argument("--csv", default="", help="local OHLCV csv to read")
    parser.add_argument("--cache", default="", help="cached json from a previous run")
    parser.add_argument("--csv-out", default="", help="write the used klines to this json path")
    parser.add_argument("--offline", action="store_true", help="never call the exchange")
    parser.add_argument("--top", type=int, default=0, help="show only the top N rows")
    parser.add_argument("--dump-trades", default="", help="write the best mode's trades to this csv")
    args = parser.parse_args(argv)
    bar_ms = TF_MS[args.timeframe]

    if args.csv:
        rows, source = load_csv(args.csv), f"csv:{args.csv}"
    elif args.cache:
        rows, source = load_json_cache(args.cache), f"cache:{args.cache}"
    elif args.offline:
        parser.error("--offline needs --csv or --cache (no exchange access allowed)")
        return 2
    else:
        rows = fetch_klines(args.network, args.symbol, args.months, args.timeframe)
        source = f"binance:{args.network}:{args.symbol}:{args.timeframe}"

    if len(rows) < 300:
        parser.error(f"not enough candles: {len(rows)} (need 300+)")
        return 2

    base = normalize_ma_cross_config({"timeframe": args.timeframe, "scan_scope": "btc",
                                      "leverage": args.leverage})
    if args.lock == "off":
        base["locking"] = False
        base["_locking_pinned"] = True
    modes = [m.strip().upper() for m in args.modes.split(",") if m.strip()] or all_feature_modes()
    unknown = [m for m in modes if not m or any(ch not in FEATURES for ch in m)]
    if unknown:
        parser.error(f"unknown feature mode(s): {', '.join(unknown)} (use A~E)")
        return 2

    print(f"# /macross local backtest — source={source} candles={len(rows)} "
          f"({len(rows) * bar_ms / 86_400_000:.0f}일, {args.timeframe}, {args.symbol})")
    print(f"# fee={args.fee:.4%} slippage={args.slippage:.4%} funding={args.funding:.4%}/8h "
          f"leverage={args.leverage}x equity={args.equity:.0f}")

    if args.csv_out:
        with open(args.csv_out, "w", encoding="utf-8") as handle:
            json.dump(rows, handle)
        print(f"# klines saved: {args.csv_out}")

    kwargs = dict(initial_equity=args.equity, fee_rate=args.fee, slippage=args.slippage,
                  funding_rate_per_8h=args.funding, bar_ms=bar_ms)
    if modes == all_feature_modes():
        results = compare_modes(rows, base, **kwargs)
    else:
        results = []
        for mode in modes:
            summary = run_backtest(rows, cfg_for_mode(mode, base), **kwargs)
            summary["mode"] = mode
            results.append(summary)
        results.sort(key=lambda item: item["return_pct"], reverse=True)

    print()
    print(format_table(results, args.top or None))
    print()
    print(f"# 조합 수: {len(results)} · 최고: {results[0]['mode']} ({results[0]['return_pct']:.2f}%) "
          f"· 최저: {results[-1]['mode']} ({results[-1]['return_pct']:.2f}%)")

    if args.dump_trades:
        best = results[0]
        with open(args.dump_trades, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["mode", "side", "entry_ms", "exit_ms", "entry", "exit", "qty", "net", "reason"])
            for trade in best["trade_log"]:
                writer.writerow([best["mode"], trade["side"], trade["entry_ms"], trade["exit_ms"],
                                 f"{trade['entry']:.4f}", f"{trade['exit']:.4f}",
                                 f"{trade['qty']:.8f}", f"{trade['net']:.4f}", trade["reason"]])
        print(f"# best mode trades: {args.dump_trades}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
