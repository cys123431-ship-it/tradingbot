#!/usr/bin/env python3
import argparse
import asyncio
import json
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from emas import (  # noqa: E402
    LIVE_REAL_CONFIRM_TEXT,
    LIVE_REAL_SMALL_CAP_DEFAULTS,
    MainController,
    enforce_activation_stage,
)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Run one tightly capped Binance Futures real-live trade decision.")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--confirm", required=True)
    parser.add_argument("--max-notional", type=float, default=15.0)
    parser.add_argument("--max-loss", type=float, default=3.0)
    parser.add_argument("--leverage", type=int, default=5)
    parser.add_argument("--side", choices=("LONG", "SHORT"), default=None)
    return parser.parse_args(argv)


def build_live_real_config(args, base_cfg=None):
    cfg = dict(base_cfg or {})
    cfg.update({
        "mode": "live",
        "live_activation_stage": "LIVE_REAL_SMALL_CAP",
        "real_live_confirm": args.confirm,
        "testnet": False,
        "live_trading": True,
        "real_order_enabled": True,
        "account_reference_equity_usdt": 62.0,
        "max_leverage": min(int(args.leverage), 5),
        "leverage": min(int(args.leverage), 5),
        "max_real_position_notional_pct_of_equity": LIVE_REAL_SMALL_CAP_DEFAULTS["max_real_position_notional_pct_of_equity"],
        "default_real_risk_pct": LIVE_REAL_SMALL_CAP_DEFAULTS["default_real_risk_pct"],
        "live_real_risk_pct_user": LIVE_REAL_SMALL_CAP_DEFAULTS["default_real_risk_pct"],
        "min_real_risk_pct": LIVE_REAL_SMALL_CAP_DEFAULTS["min_real_risk_pct"],
        "max_real_risk_pct": LIVE_REAL_SMALL_CAP_DEFAULTS["max_real_risk_pct"],
        "max_daily_real_loss_pct_of_equity": LIVE_REAL_SMALL_CAP_DEFAULTS["max_daily_real_loss_pct_of_equity"],
        "max_weekly_real_loss_pct_of_equity": LIVE_REAL_SMALL_CAP_DEFAULTS["max_weekly_real_loss_pct_of_equity"],
        "max_open_positions": 1,
        "max_same_direction_positions": 1,
    })
    if args.side:
        cfg["live_real_side_hint"] = args.side
    return enforce_activation_stage(cfg)


def build_bot():
    return MainController()


def _json_safe(value):
    if is_dataclass(value):
        return _json_safe(asdict(value))
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


async def run_once(args):
    cfg = build_live_real_config(args)
    bot = build_bot()
    return await bot.run_live_real_once(args.symbol, cfg)


def main(argv=None):
    args = parse_args(argv)
    if not args.once:
        raise SystemExit("Refusing to run: --once is required for LIVE_REAL_SMALL_CAP.")
    if args.confirm != LIVE_REAL_CONFIRM_TEXT:
        raise SystemExit("Refusing to run: exact real-live confirmation text is required.")
    result = asyncio.run(run_once(args))
    print(json.dumps(_json_safe(result), ensure_ascii=False, indent=2))
    return result


if __name__ == "__main__":
    main()
