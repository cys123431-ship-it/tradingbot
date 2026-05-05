"""Telegram text helpers for Prediction Micro Auto."""

from __future__ import annotations

from collections import Counter


def prediction_reject_counts(scan_result):
    counts = Counter()
    for item in list((scan_result or {}).get("rejects") or []):
        reasons = item.get("reject_reasons")
        if isinstance(reasons, list) and reasons:
            for reason in reasons:
                counts[str(reason)] += 1
        else:
            counts[str(item.get("reject_code") or "UNKNOWN_REJECT")] += 1
    return dict(counts)


def format_prediction_report(scan_result, cfg=None, ledger_summary=None):
    cfg = cfg or {}
    ledger_summary = ledger_summary or {}
    candidates = list((scan_result or {}).get("candidates") or [])
    rejects = list((scan_result or {}).get("rejects") or [])
    provider = "Binance Wallet Prediction (Predict.fun API)"
    if str(cfg.get("provider") or "").strip().lower() == "predict_fun":
        provider = "Predict.fun direct"
    lines = [
        "Prediction Micro Auto",
        f"Provider: {provider}",
        "State: {} | Paper Only: {} | Cap {:.2f} USDT".format(
            "ON" if cfg.get("enabled") else "OFF",
            "OFF" if cfg.get("live_enabled") else "ON",
            float(cfg.get("equity_cap_usdt", 10.0) or 10.0),
        ),
        "Live: {} | Order: BUY YES MARKET | stake <= {:.2f} USDT".format(
            "UNLOCKED" if cfg.get("live_enabled") else "LOCKED",
            float(cfg.get("max_stake_usdt", 1.0) or 1.0),
        ),
        f"Markets: Crypto + Macro | candidates {len(candidates)} / rejects {len(rejects)}",
        "Binance app Prediction is treated as a Predict.fun protocol integration, not a separate Binance order API.",
        "",
    ]
    if candidates:
        lines.append("Top candidates:")
        for idx, item in enumerate(candidates[:8], 1):
            components = item.get("component_scores") or {}
            lines.append(
                f"{idx}. {item.get('title') or item.get('market_id')} "
                f"score {float(item.get('score', 0) or 0):.1f} / "
                f"edge {float(item.get('edge', 0) or 0):.3f} / "
                f"stake {float(item.get('stake_usdt', 0) or 0):.2f} / "
                f"strats {','.join(str(s) for s in item.get('strategy_ids', [])[:4])}"
            )
            if components:
                lines.append(
                    "   components: "
                    + ", ".join(f"{k} {float(v):.1f}" for k, v in components.items())
                )
    else:
        lines.append("Top candidates: none yet. Run /prediction scan.")
    lines.extend([
        "",
        "Paper ledger:",
        (
            f"open {ledger_summary.get('open_positions', 0)} / "
            f"closed {ledger_summary.get('closed_positions', 0)} / "
            f"PnL {float(ledger_summary.get('realized_pnl_usdt', 0) or 0):.2f} USDT"
        ),
    ])
    if rejects:
        counts = prediction_reject_counts(scan_result)
        top = sorted(counts.items(), key=lambda item: item[1], reverse=True)[:5]
        lines.append("Top rejects: " + ", ".join(f"{k}={v}" for k, v in top))
    return "\n".join(lines).strip()


def format_prediction_research_report(ledger_summary, reject_counts=None, strategy_counts=None, days=7):
    ledger_summary = ledger_summary or {}
    reject_counts = reject_counts or {}
    strategy_counts = strategy_counts or {}
    by_category = ledger_summary.get("by_category") or {}
    lines = [
        f"Prediction Micro Auto Research Report ({int(days or 7)}d)",
        (
            f"PnL {float(ledger_summary.get('realized_pnl_usdt', 0) or 0):.2f} USDT / "
            f"closed {ledger_summary.get('closed_positions', 0)} / "
            f"open {ledger_summary.get('open_positions', 0)}"
        ),
        (
            f"Avg Brier {ledger_summary.get('avg_brier_score') if ledger_summary.get('avg_brier_score') is not None else 'n/a'} / "
            f"Avg CLV {ledger_summary.get('avg_closing_line_value') if ledger_summary.get('avg_closing_line_value') is not None else 'n/a'}"
        ),
        "",
        "Category PnL:",
    ]
    if not by_category:
        lines.append("none")
    for category, item in sorted(by_category.items()):
        lines.append(f"- {category}: {item.get('count', 0)} trades / {float(item.get('pnl_usdt', 0) or 0):.2f} USDT")
    lines.append("")
    lines.append("Top rejects:")
    if not reject_counts:
        lines.append("none")
    for reason, count in sorted(reject_counts.items(), key=lambda item: item[1], reverse=True)[:10]:
        lines.append(f"- {reason}: {count}")
    lines.append("")
    lines.append("Strategy usage:")
    if not strategy_counts:
        lines.append("none")
    for strategy_id, count in sorted(strategy_counts.items(), key=lambda item: item[1], reverse=True)[:10]:
        lines.append(f"- Strategy {strategy_id}: {count}")
    return "\n".join(lines).strip()
