"""Paper ledger for prediction market plans."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone


class PaperLedger:
    def __init__(self, positions=None):
        self.positions = list(positions or [])

    @classmethod
    def load(cls, path):
        try:
            with open(path, "r", encoding="utf-8") as fp:
                payload = json.load(fp)
        except FileNotFoundError:
            return cls()
        except Exception:
            return cls()
        if isinstance(payload, dict):
            return cls(payload.get("positions") or [])
        if isinstance(payload, list):
            return cls(payload)
        return cls()

    def to_dict(self):
        return {"version": 1, "positions": list(self.positions)}

    def save(self, path):
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as fp:
            json.dump(self.to_dict(), fp, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp_path, path)

    def open_positions(self):
        return [p for p in self.positions if p.get("status") == "OPEN"]

    def open_position(self, plan, *, fair_probability=None, opened_at=None):
        opened_at = opened_at or datetime.now(timezone.utc).isoformat()
        position_id = f"paper-{len(self.positions) + 1}"
        position = {
            "id": position_id,
            "status": "OPEN",
            "opened_at": opened_at,
            "market_id": plan.get("market_id"),
            "market_title": plan.get("market_title"),
            "market_type": plan.get("market_type"),
            "side": plan.get("side", "YES"),
            "stake_usdt": float(plan.get("stake_usdt") or 0.0),
            "entry_price": float(plan.get("entry_price") or 0.0),
            "shares": float(plan.get("shares") or 0.0),
            "fair_probability": float(fair_probability if fair_probability is not None else plan.get("fair_probability", 0.5)),
        }
        self.positions.append(position)
        return position

    def close_position(self, position_id, *, exit_price, closed_at=None):
        position = self._get_open(position_id)
        closed_at = closed_at or datetime.now(timezone.utc).isoformat()
        exit_price = float(exit_price)
        pnl = (position["shares"] * exit_price) - position["stake_usdt"]
        position.update({
            "status": "CLOSED",
            "closed_at": closed_at,
            "exit_price": exit_price,
            "pnl_usdt": pnl,
            "closing_line_value": exit_price - position["entry_price"],
        })
        return position

    def settle_position(self, position_id, *, outcome_won, settled_at=None, closing_price=None):
        position = self._get_open(position_id)
        settled_at = settled_at or datetime.now(timezone.utc).isoformat()
        payout = position["shares"] * (1.0 if outcome_won else 0.0)
        pnl = payout - position["stake_usdt"]
        actual = 1.0 if outcome_won else 0.0
        brier = (position["fair_probability"] - actual) ** 2
        position.update({
            "status": "SETTLED",
            "settled_at": settled_at,
            "outcome_won": bool(outcome_won),
            "payout_usdt": payout,
            "pnl_usdt": pnl,
            "brier_score": brier,
            "closing_line_value": None if closing_price is None else float(closing_price) - position["entry_price"],
        })
        return position

    def recent_positions(self, days=7, now=None):
        now_dt = _parse_dt(now) or datetime.now(timezone.utc)
        cutoff = now_dt.timestamp() - (max(1, int(days or 7)) * 86400)
        recent = []
        for position in self.positions:
            ts = (
                _parse_dt(position.get("closed_at"))
                or _parse_dt(position.get("settled_at"))
                or _parse_dt(position.get("opened_at"))
            )
            if ts and ts.timestamp() >= cutoff:
                recent.append(position)
        return recent

    def opened_count_since(self, hours=24, now=None):
        now_dt = _parse_dt(now) or datetime.now(timezone.utc)
        cutoff = now_dt.timestamp() - (max(1, float(hours or 24)) * 3600)
        count = 0
        for position in self.positions:
            opened_at = _parse_dt(position.get("opened_at"))
            if opened_at and opened_at.timestamp() >= cutoff:
                count += 1
        return count

    def summary(self, days=None):
        positions = self.recent_positions(days=days) if days else list(self.positions)
        closed = [p for p in positions if p.get("status") in {"CLOSED", "SETTLED"}]
        pnl = sum(float(p.get("pnl_usdt") or 0.0) for p in closed)
        briers = [float(p.get("brier_score")) for p in closed if p.get("brier_score") is not None]
        clvs = [float(p.get("closing_line_value")) for p in closed if p.get("closing_line_value") is not None]
        by_category = {}
        for p in closed:
            key = p.get("market_type") or "unknown"
            bucket = by_category.setdefault(key, {"count": 0, "pnl_usdt": 0.0})
            bucket["count"] += 1
            bucket["pnl_usdt"] += float(p.get("pnl_usdt") or 0.0)
        return {
            "total_positions": len(positions),
            "open_positions": len(self.open_positions()),
            "closed_positions": len(closed),
            "realized_pnl_usdt": pnl,
            "avg_brier_score": (sum(briers) / len(briers)) if briers else None,
            "avg_closing_line_value": (sum(clvs) / len(clvs)) if clvs else None,
            "by_category": by_category,
        }

    def _get_open(self, position_id):
        for position in self.positions:
            if position.get("id") == position_id and position.get("status") == "OPEN":
                return position
        raise KeyError(f"open position not found: {position_id}")


def _parse_dt(value):
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
