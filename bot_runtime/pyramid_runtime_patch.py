"""Runtime installation of the Adaptive Trend pyramid stop postcondition."""

from __future__ import annotations

from .pyramid_protection import enforce_adaptive_pyramid_stop_postcondition
from utbreakout.adaptive_breakout_trend import ADAPTIVE_BREAKOUT_TREND_STRATEGY


def install_adaptive_pyramid_stop_guard() -> None:
    """Wrap SignalEngine's pyramid add path without altering strategy selection logic."""

    from .signal_engine import SignalEngine
    from .signal_exit import SignalExitMixin

    if bool(getattr(SignalEngine, "_adaptive_pyramid_stop_guard_installed", False)):
        return

    original = SignalExitMixin._maybe_apply_adaptive_trend_pyramiding

    async def guarded(self, symbol, pos, df, cfg):
        before_qty = 0.0
        try:
            before_qty = abs(
                float(self._position_signed_contracts(pos) or pos.get("contracts", 0.0) or 0.0)
            )
        except (AttributeError, TypeError, ValueError):
            before_qty = 0.0

        before_stop = None
        before_add_count = 0
        try:
            state = self._get_utbreakout_trailing_state(symbol)
        except Exception:
            state = None
        if isinstance(state, dict):
            try:
                before_add_count = max(
                    0,
                    int(state.get("adaptive_trend_pyramid_add_count", 0) or 0),
                )
            except (TypeError, ValueError):
                before_add_count = 0
            if (
                bool(state.get("adaptive_trend_pyramid_enabled", False))
                and str(state.get("strategy") or "").lower()
                == ADAPTIVE_BREAKOUT_TREND_STRATEGY
            ):
                try:
                    before_stop = await self._current_stop_loss_price(symbol, state)
                except Exception:
                    before_stop = None

        result = await original(self, symbol, pos, df, cfg)
        return await enforce_adaptive_pyramid_stop_postcondition(
            self,
            symbol,
            before_qty=before_qty,
            before_stop=before_stop,
            before_add_count=before_add_count,
            result=result,
            cfg=cfg,
        )

    guarded.__name__ = original.__name__
    guarded.__qualname__ = f"SignalEngine.{original.__name__}"
    guarded.__doc__ = original.__doc__
    guarded.__runtime_original__ = original
    SignalEngine._maybe_apply_adaptive_trend_pyramiding = guarded
    SignalEngine._adaptive_pyramid_stop_guard_installed = True


__all__ = ("install_adaptive_pyramid_stop_guard",)
