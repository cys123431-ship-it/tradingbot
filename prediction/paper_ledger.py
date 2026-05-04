"""In-memory paper ledger for prediction market plans."""

from __future__ import annotations

from datetime import datetime, timezone


class PaperLedger:
    def __init__(self, positions=None):
        self.positions = list(positions or [])

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

    def summary(self):
        closed = [p for p in self.positions if p.get("status") in {"CLOSED", "SETTLED"}]
        pnl = sum(float(p.get("pnl_usdt") or 0.0) for p in closed)
        briers = [float(p.get("brier_score")) for p in closed if p.get("brier_score") is not None]
        return {
            "total_positions": len(self.positions),
            "open_positions": len(self.open_positions()),
            "closed_positions": len(closed),
            "realized_pnl_usdt": pnl,
            "avg_brier_score": (sum(briers) / len(briers)) if briers else None,
        }

    def _get_open(self, position_id):
        for position in self.positions:
            if position.get("id") == position_id and position.get("status") == "OPEN":
                return position
        raise KeyError(f"open position not found: {position_id}")
