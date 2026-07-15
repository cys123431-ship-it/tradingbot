from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import math
from typing import Any, Iterable
from zoneinfo import ZoneInfo


KST = ZoneInfo("Asia/Seoul")


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KST)
    return parsed.astimezone(KST)


def previous_calendar_month(now: datetime | None = None) -> tuple[int, int]:
    current = (now or datetime.now(KST)).astimezone(KST)
    if current.month == 1:
        return current.year - 1, 12
    return current.year, current.month - 1


def trades_for_calendar_month(
    trades: Iterable[dict[str, Any]], year: int, month: int,
) -> list[dict[str, Any]]:
    selected = []
    for trade in trades or []:
        exit_at = _datetime(trade.get("exit_time"))
        if exit_at and exit_at.year == int(year) and exit_at.month == int(month):
            selected.append(dict(trade))
    selected.sort(key=lambda trade: str(trade.get("exit_time") or ""))
    return selected


def _primary_strategy(trade: dict[str, Any]) -> str:
    return str(
        trade.get("primary_strategy")
        or trade.get("selected_strategy")
        or trade.get("strategy")
        or trade.get("engine")
        or "UNKNOWN"
    )


def _confirmation_strategies(trade: dict[str, Any]) -> list[str]:
    values = trade.get("confirmation_strategies") or []
    if isinstance(values, str):
        values = [item.strip() for item in values.split(",") if item.strip()]
    return [str(value) for value in values if str(value).strip()]


def _profit_factor(values: Iterable[float]) -> float | None:
    numbers = list(values)
    profit = sum(value for value in numbers if value > 0)
    loss = abs(sum(value for value in numbers if value < 0))
    if loss > 0:
        return profit / loss
    return None if profit <= 0 else math.inf


def _max_consecutive_losses(trades: list[dict[str, Any]]) -> int:
    longest = current = 0
    for trade in trades:
        if _number(trade.get("net_pnl_usdt", trade.get("gross_pnl_usdt"))) < 0:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def _duration(trade: dict[str, Any]) -> str:
    entry_at = _datetime(trade.get("entry_time"))
    exit_at = _datetime(trade.get("exit_time"))
    if not entry_at or not exit_at:
        return "N/A"
    minutes = max(0, int((exit_at - entry_at).total_seconds() // 60))
    return f"{minutes // 1440}d{(minutes % 1440) // 60:02d}h" if minutes >= 1440 else f"{minutes // 60}h{minutes % 60:02d}m"


def _exit_summary(trade: dict[str, Any]) -> str:
    legs = trade.get("exit_legs") or []
    if not isinstance(legs, list) or not legs:
        return str(trade.get("exit_reason") or "UNKNOWN")
    quantities: dict[str, float] = defaultdict(float)
    pnls: dict[str, float] = defaultdict(float)
    for leg in legs:
        if not isinstance(leg, dict):
            continue
        label = str(leg.get("label") or "EXIT").upper()
        quantities[label] += abs(_number(leg.get("qty")))
        pnls[label] += _number(leg.get("realized_pnl_usdt"))
    return ", ".join(
        f"{label} {quantities[label]:.6g}qty/{pnls[label]:+.2f}"
        for label in sorted(quantities)
    ) or str(trade.get("exit_reason") or "UNKNOWN")


def _metric_line(label: str, trades: list[dict[str, Any]]) -> str:
    net = [_number(item.get("net_pnl_usdt", item.get("gross_pnl_usdt"))) for item in trades]
    r_values = [_number(item.get("realized_r", item.get("r_multiple"))) for item in trades]
    wins = sum(1 for value in net if value > 0)
    pf = _profit_factor(net)
    pf_text = "INF" if pf == math.inf else f"{pf:.2f}" if pf is not None else "N/A"
    return (
        f"{label:<32} {len(trades):>4} {wins / len(trades) * 100 if trades else 0:>6.1f}% "
        f"{sum(net):>+11.2f} {sum(r_values) / len(r_values) if r_values else 0:>+7.2f} "
        f"{pf_text:>7} {_max_consecutive_losses(trades):>5}"
    )


def build_monthly_trade_report(
    trades: Iterable[dict[str, Any]],
    year: int,
    month: int,
    *,
    open_positions: Iterable[dict[str, Any]] | None = None,
    generated_at: datetime | None = None,
) -> str:
    monthly = trades_for_calendar_month(trades, year, month)
    generated = (generated_at or datetime.now(KST)).astimezone(KST)
    lines = [
        "LIVE TRADING MONTHLY REPORT",
        f"Period (KST): {int(year):04d}-{int(month):02d}-01 through month end",
        f"Generated (KST): {generated:%Y-%m-%d %H:%M:%S}",
        "Accounting rule: one realized position = one PnL record; confirmations are attribution only.",
        "",
        "[Overall]",
        "Name                             N    Win%     Net USDT   Avg R      PF MaxL",
        _metric_line("ALL REAL TRADES", monthly),
    ]

    if not monthly:
        lines.extend(["", "No real positions were closed during this calendar month."])
    else:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        sides: dict[str, list[dict[str, Any]]] = defaultdict(list)
        symbols: dict[str, list[dict[str, Any]]] = defaultdict(list)
        modes: dict[str, list[dict[str, Any]]] = defaultdict(list)
        participants: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for trade in monthly:
            primary = _primary_strategy(trade)
            grouped[primary].append(trade)
            sides[str(trade.get("side") or "UNKNOWN").upper()].append(trade)
            symbols[str(trade.get("symbol") or "UNKNOWN")].append(trade)
            confirmations = _confirmation_strategies(trade)
            modes["CONFIRMED" if len(confirmations) > 1 else "STANDALONE"].append(trade)
            for strategy in dict.fromkeys(confirmations or [primary]):
                participants[str(strategy)].append(trade)

        lines.extend(["", "[Primary strategy — no double counting]"])
        lines.extend(_metric_line(key[:32], grouped[key]) for key in sorted(grouped))
        lines.extend(["", "[Standalone vs confirmation]"])
        lines.extend(_metric_line(key, modes[key]) for key in sorted(modes))
        lines.extend(["", "[Direction]"])
        lines.extend(_metric_line(key, sides[key]) for key in sorted(sides))
        lines.extend(["", "[Symbol]"])
        lines.extend(_metric_line(key[:32], symbols[key]) for key in sorted(symbols))
        lines.extend([
            "",
            "[Signal participation — PnL shown for comparison, never added to total]",
        ])
        lines.extend(_metric_line(key[:32], participants[key]) for key in sorted(participants))

        exit_totals: dict[str, dict[str, float]] = defaultdict(lambda: {"qty": 0.0, "pnl": 0.0, "count": 0.0})
        for trade in monthly:
            for leg in trade.get("exit_legs") or []:
                if not isinstance(leg, dict):
                    continue
                label = str(leg.get("label") or "EXIT").upper()
                exit_totals[label]["qty"] += abs(_number(leg.get("qty")))
                exit_totals[label]["pnl"] += _number(leg.get("realized_pnl_usdt"))
                exit_totals[label]["count"] += 1
        lines.extend(["", "[Exit contribution]"])
        if exit_totals:
            for label in sorted(exit_totals):
                item = exit_totals[label]
                lines.append(f"{label:<12} fills={int(item['count']):>3} qty={item['qty']:.8g} pnl={item['pnl']:+.2f} USDT")
        else:
            lines.append("No exchange fill-leg breakdown was available; see exit reasons below.")

        lines.extend([
            "",
            "[Closed trade detail]",
            "Exit(KST)        Symbol              Side Primary                         Qty       Lev  Entry -> Exit             Net/R       Duration",
        ])
        for trade in monthly:
            exit_at = _datetime(trade.get("exit_time"))
            net = _number(trade.get("net_pnl_usdt", trade.get("gross_pnl_usdt")))
            r_value = _number(trade.get("realized_r", trade.get("r_multiple")))
            exit_text = f"{exit_at:%m-%d %H:%M}" if exit_at else "N/A"
            lines.append(
                f"{exit_text:<16}  "
                + f"{str(trade.get('symbol') or 'UNKNOWN'):<19} {str(trade.get('side') or '?').upper():<4} "
                + f"{_primary_strategy(trade)[:31]:<31} {abs(_number(trade.get('filled_qty'))):>9.6g} "
                + f"{int(_number(trade.get('leverage'), 0)):>3}x  "
                + f"{_number(trade.get('entry_price')):.8g} -> {_number(trade.get('exit_price')):.8g}  "
                + f"{net:+.2f}/{r_value:+.2f}R  {_duration(trade)}"
            )
            fees = abs(_number(trade.get("entry_fee_usdt"))) + abs(_number(trade.get("exit_fee_usdt")))
            lines.append(
                f"  gross={_number(trade.get('gross_pnl_usdt')):+.2f} fees={fees:.2f} "
                f"funding={_number(trade.get('funding_usdt')):+.2f} "
                f"status={'PROVISIONAL' if trade.get('provisional') else 'FINAL'}"
            )
            lines.append(f"  captured by: {_exit_summary(trade)}")
            confirmations = _confirmation_strategies(trade)
            if confirmations:
                lines.append(f"  signal participants: {', '.join(confirmations)}")

    positions = [dict(item) for item in open_positions or [] if isinstance(item, dict)]
    lines.extend(["", "[Open positions at month end]"])
    if positions:
        for position in positions:
            lines.append(
                f"{position.get('symbol') or 'UNKNOWN'} {str(position.get('side') or '?').upper()} "
                f"qty={abs(_number(position.get('contracts', position.get('qty')))):.8g} "
                f"entry={_number(position.get('entryPrice', position.get('entry_price'))):.8g} "
                f"mark={_number(position.get('markPrice', position.get('mark_price'))):.8g} "
                f"uPnL={_number(position.get('unrealizedPnl', position.get('unrealized_pnl'))):+.2f}"
            )
    else:
        lines.append("NONE")
    lines.append("")
    return "\n".join(lines)
