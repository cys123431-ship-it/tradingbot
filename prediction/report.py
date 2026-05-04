"""Telegram text helpers for Prediction Micro Auto."""

from __future__ import annotations


def format_prediction_report(scan_result, cfg=None, ledger_summary=None):
    cfg = cfg or {}
    ledger_summary = ledger_summary or {}
    candidates = list((scan_result or {}).get("candidates") or [])
    rejects = list((scan_result or {}).get("rejects") or [])
    lines = [
        "Prediction Micro Auto",
        "State: {} | Paper Only: ON | Cap {:.2f} USDT".format(
            "ON" if cfg.get("enabled") else "OFF",
            float(cfg.get("equity_cap_usdt", 10.0) or 10.0),
        ),
        f"Markets: Crypto + Macro | candidates {len(candidates)} / rejects {len(rejects)}",
        "Live orders disabled. Predict.fun mainnet orders/JWT/private keys are not used.",
        "",
    ]
    if candidates:
        lines.append("Top candidates:")
        for idx, item in enumerate(candidates[:8], 1):
            lines.append(
                f"{idx}. {item.get('title') or item.get('market_id')} "
                f"score {float(item.get('score', 0) or 0):.1f} / "
                f"edge {float(item.get('edge', 0) or 0):.3f} / "
                f"stake {float(item.get('stake_usdt', 0) or 0):.2f}"
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
        first = rejects[0]
        lines.append(
            "Recent reject: "
            f"{first.get('title') or first.get('market_id')} "
            f"{first.get('reject_code') or first.get('reject_reasons')}"
        )
    return "\n".join(lines).strip()
