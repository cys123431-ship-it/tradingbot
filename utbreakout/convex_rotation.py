"""Opportunity-cost exit logic for the standalone trend portfolio."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ConvexRotationExitDecision:
    bad_observation: bool
    bad_count: int
    should_exit: bool
    reason: str


def evaluate_convex_rotation_exit(
    *,
    enabled: bool,
    bars_held: int,
    min_bars: int,
    max_bars: int,
    mfe_r: float,
    max_mfe_r: float,
    current_r: float,
    max_current_r: float,
    entry_percentile: float | None,
    current_percentile: float | None,
    percentile_floor: float,
    reaccelerating: bool,
    prior_bad_count: int,
    required_confirmations: int,
) -> ConvexRotationExitDecision:
    """Require weak progress, persistent rank decay and no re-acceleration."""

    bars_held = max(0, int(bars_held or 0))
    min_bars = max(1, int(min_bars or 1))
    max_bars = max(min_bars, int(max_bars or min_bars))
    prior_bad_count = max(0, int(prior_bad_count or 0))
    required_confirmations = max(1, int(required_confirmations or 1))
    if not enabled:
        return ConvexRotationExitDecision(False, prior_bad_count, False, "disabled")
    if bars_held < min_bars:
        return ConvexRotationExitDecision(
            False,
            prior_bad_count,
            False,
            f"waiting {bars_held}/{min_bars} bars",
        )
    if entry_percentile is None or current_percentile is None:
        return ConvexRotationExitDecision(
            False,
            0,
            False,
            "cross-sectional rank unavailable",
        )

    rank_deteriorated = bool(
        float(current_percentile) < float(percentile_floor)
        and float(current_percentile) <= float(entry_percentile) - 20.0
    )
    no_progress = bool(
        float(mfe_r) < float(max_mfe_r)
        and float(current_r) <= float(max_current_r)
    )
    bad_observation = bool(rank_deteriorated and no_progress and not reaccelerating)
    bad_count = prior_bad_count + 1 if bad_observation else 0
    severe_late_decay = bool(
        bars_held >= max_bars
        and float(current_percentile) < float(percentile_floor) * 0.5
    )
    should_exit = bool(
        bad_count >= required_confirmations
        or (severe_late_decay and bad_count >= 1)
    )
    reason = (
        f"bars={bars_held}, MFE={float(mfe_r):.2f}R, current={float(current_r):.2f}R, "
        f"rank={float(current_percentile):.1f} from {float(entry_percentile):.1f}, "
        f"decay={bad_count}/{required_confirmations}, reacceleration={bool(reaccelerating)}"
    )
    return ConvexRotationExitDecision(
        bad_observation,
        bad_count,
        should_exit,
        reason,
    )


__all__ = ("ConvexRotationExitDecision", "evaluate_convex_rotation_exit")
