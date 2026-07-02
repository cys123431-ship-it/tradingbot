"""Independent direction decision engine for UTBreakout entries.

This module is pure scoring logic. It does not select scanner candidates,
size positions, or place orders.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
from typing import Any, Mapping


@dataclass(frozen=True)
class DirectionDecision:
    allowed: bool
    side: str
    score: float
    min_score: float
    risk_multiplier: float = 1.0
    opposite_regime: bool = False
    reasons: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    components: Mapping[str, float] = field(default_factory=dict)

    @property
    def summary(self) -> str:
        state = "ALLOW" if self.allowed else "BLOCK"
        return f"{state} direction score {self.score:.1f}/{self.min_score:.1f} risk x{self.risk_multiplier:.2f}"


def default_direction_engine_config() -> dict[str, Any]:
    return {
        "direction_engine_min_score": 62.0,
        "direction_engine_opposite_regime_min_score": 68.0,
    }


def _finite(value: Any, default: float | None = None) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if isfinite(parsed) else default


def _clamp(value: float, low: float, high: float) -> float:
    return max(float(low), min(float(high), float(value)))


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _is_aligned_direction(side: str, value: Any) -> bool:
    text = str(value or "").strip().lower()
    if str(side or "").lower() == "long":
        return text in {"long", "bull", "bullish", "up"}
    return text in {"short", "bear", "bearish", "down"}


def _is_opposite_direction(side: str, value: Any) -> bool:
    text = str(value or "").strip().lower()
    if str(side or "").lower() == "long":
        return text in {"short", "bear", "bearish", "down"}
    return text in {"long", "bull", "bullish", "up"}


def evaluate_direction_engine(
    *,
    side: str,
    values: Mapping[str, Any] | None = None,
    trend: float,
    relative: float,
    regime: float,
    derivatives: float,
    squeeze: float,
    ev_decision: Any = None,
    config: Mapping[str, Any] | None = None,
    opposite_regime: bool = False,
) -> DirectionDecision:
    cfg = default_direction_engine_config()
    if isinstance(config, Mapping):
        cfg.update(dict(config))
    values = values if isinstance(values, Mapping) else {}
    side = str(side or "").lower()

    ev_score = _finite(_attr(ev_decision, "score", None), None)
    mtf = str(_attr(ev_decision, "mtf_alignment", values.get("mtf_alignment", "")) or "")
    momentum = str(_attr(ev_decision, "momentum_alignment", values.get("momentum_alignment", "")) or "")

    score = (
        float(trend) * 0.30
        + float(relative) * 0.18
        + float(regime) * 0.24
        + float(derivatives) * 0.22
        + float(squeeze) * 0.06
    )
    if ev_score is not None:
        score = score * 0.82 + float(ev_score) * 0.18

    reasons = [
        f"direction trend {float(trend):.1f}",
        f"regime {float(regime):.1f}",
        f"flow {float(derivatives):.1f}",
    ]
    blockers: list[str] = []
    if mtf and mtf not in {"n/a", "None"}:
        reasons.append(f"MTF {mtf}")
        try:
            aligned, total = mtf.split("/", 1)
            if int(aligned) <= 1 and int(total) >= 3:
                score -= 5.0
                blockers.append(f"weak MTF alignment {mtf}")
        except Exception:
            pass
    if momentum:
        if _is_aligned_direction(side, momentum):
            score += 3.0
            reasons.append("momentum aligned")
        elif _is_opposite_direction(side, momentum):
            score -= 4.0
            blockers.append("momentum against")

    score = _clamp(score, 0.0, 100.0)
    min_score_key = (
        "direction_engine_opposite_regime_min_score"
        if opposite_regime
        else "direction_engine_min_score"
    )
    min_score = float(cfg.get(min_score_key, 62.0 if not opposite_regime else 68.0) or 0.0)
    if score < min_score:
        blockers.append(f"direction engine {score:.1f}<{min_score:.1f}")

    risk_multiplier = 1.0
    if score < min_score + 4.0:
        risk_multiplier = 0.72
    if blockers:
        risk_multiplier = min(risk_multiplier, 0.50)

    components = {
        "trend": float(trend),
        "relative": float(relative),
        "regime": float(regime),
        "derivatives": float(derivatives),
        "squeeze": float(squeeze),
        "ev_score": float(ev_score or 0.0),
    }
    return DirectionDecision(
        allowed=not blockers,
        side=side,
        score=score,
        min_score=min_score,
        risk_multiplier=_clamp(risk_multiplier, 0.0, 1.0),
        opposite_regime=bool(opposite_regime),
        reasons=tuple(reasons),
        blockers=tuple(dict.fromkeys(str(item) for item in blockers if item)),
        components=components,
    )
