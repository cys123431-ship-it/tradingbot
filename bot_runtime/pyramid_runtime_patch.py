"""Runtime installation of the Adaptive Trend pyramid stop postcondition."""

from __future__ import annotations

from .pyramid_protection_live import (
    best_live_exchange_stop,
    enforce_adaptive_pyramid_live_sl_guard,
    read_exchange_sl_snapshot,
)
from .pyramid_safe_rebuild import (
    activate_adaptive_pyramid_rebuild,
    cancel_preserving_pyramid_sl,
    place_pyramid_protection_preserving_sl,
    reset_adaptive_pyramid_rebuild,
)
from utbreakout.adaptive_breakout_trend import ADAPTIVE_BREAKOUT_TREND_STRATEGY


_POST_FILL_REPAIR_STATUSES = {"FILLED_UNPROTECTED", "ORDER_SENT_POSITION_MISSING"}


def _prepare_post_fill_guard_input(result):
    if not isinstance(result, dict):
        return result, None
    original_status = str(result.get("status") or "")
    if original_status not in _POST_FILL_REPAIR_STATUSES:
        return result, None
    guard_input = dict(result)
    guard_input["status"] = "ADDED"
    guard_input["pre_guard_status"] = original_status
    return guard_input, original_status


async def _position_increased_after_exception(engine, symbol, before_qty):
    fetcher = getattr(engine, "_fetch_server_position_checked", None)
    if not callable(fetcher):
        return False
    try:
        fetch_ok, live_pos = await fetcher(symbol)
    except Exception:
        return False
    if not fetch_ok or not isinstance(live_pos, dict):
        return False
    try:
        live_qty = abs(
            float(
                engine._position_signed_contracts(live_pos)
                or live_pos.get("contracts", 0.0)
                or 0.0
            )
        )
    except (AttributeError, TypeError, ValueError):
        return False
    return live_qty > float(before_qty or 0.0) + 1e-12


def install_adaptive_pyramid_stop_guard() -> None:
    """Install the Adaptive Trend post-fill guard and safe protection rebuild."""

    from .signal_engine import SignalEngine
    from .signal_exit import SignalExitMixin
    from .signal_protection import SignalProtectionMixin

    if bool(getattr(SignalEngine, "_adaptive_pyramid_stop_guard_installed", False)):
        return

    original = SignalExitMixin._maybe_apply_adaptive_trend_pyramiding
    original_place = SignalExitMixin._place_tp_sl_orders
    original_cancel = SignalProtectionMixin._cancel_protection_orders

    async def guarded_cancel(self, symbol, reason="protection cleanup", orders=None):
        return await cancel_preserving_pyramid_sl(
            self,
            original_cancel,
            symbol,
            reason=reason,
            orders=orders,
        )

    async def guarded_place(
        self,
        symbol,
        side,
        entry_price,
        qty,
        tp_distance=None,
        sl_distance=None,
        tp_qty_ratio=1.0,
        tp_targets=None,
        preserve_runner_qty=False,
    ):
        return await place_pyramid_protection_preserving_sl(
            self,
            original_place,
            symbol,
            side,
            entry_price,
            qty,
            tp_distance=tp_distance,
            sl_distance=sl_distance,
            tp_qty_ratio=tp_qty_ratio,
            tp_targets=tp_targets,
            preserve_runner_qty=preserve_runner_qty,
        )

    guarded_cancel.__name__ = original_cancel.__name__
    guarded_cancel.__qualname__ = f"SignalEngine.{original_cancel.__name__}"
    guarded_cancel.__doc__ = original_cancel.__doc__
    guarded_cancel.__runtime_original__ = original_cancel
    guarded_place.__name__ = original_place.__name__
    guarded_place.__qualname__ = f"SignalEngine.{original_place.__name__}"
    guarded_place.__doc__ = original_place.__doc__
    guarded_place.__runtime_original__ = original_place
    SignalEngine._cancel_protection_orders = guarded_cancel
    SignalEngine._place_tp_sl_orders = guarded_place

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
                side = str((pos or {}).get("side") or state.get("side") or "").lower()
                mark = None
                try:
                    mark = float((pos or {}).get("markPrice") or 0.0) or None
                except (TypeError, ValueError):
                    mark = None
                if side in {"long", "short"}:
                    try:
                        snapshot = await read_exchange_sl_snapshot(self, symbol, side)
                        if snapshot.get("fetch_ok"):
                            before_stop = best_live_exchange_stop(snapshot, side, mark)
                    except Exception:
                        before_stop = None

        token = activate_adaptive_pyramid_rebuild()
        original_error = None
        try:
            result = await original(self, symbol, pos, df, cfg)
        except Exception as exc:
            original_error = exc
            result = None
        finally:
            reset_adaptive_pyramid_rebuild(token)

        if original_error is not None:
            if not await _position_increased_after_exception(self, symbol, before_qty):
                raise original_error
            guarded_result = await enforce_adaptive_pyramid_live_sl_guard(
                self,
                symbol,
                before_qty=before_qty,
                before_stop=before_stop,
                before_add_count=before_add_count,
                result={
                    "status": "ADDED",
                    "reason": "adaptive trend post-fill exception routed through protection guard",
                    "post_fill_exception": (
                        f"{type(original_error).__name__}: {original_error}"
                    ),
                },
                cfg=cfg,
            )
            if (
                isinstance(guarded_result, dict)
                and guarded_result.get("post_add_stop_guard") == "OK"
            ):
                guarded_result = dict(guarded_result)
                guarded_result.update(
                    {
                        "status": "ADDED_PROTECTION_REPAIRED",
                        "reason": "adaptive trend pyramid SL recovered after post-fill exception",
                    }
                )
            return guarded_result

        guard_input, repair_status = _prepare_post_fill_guard_input(result)
        guarded_result = await enforce_adaptive_pyramid_live_sl_guard(
            self,
            symbol,
            before_qty=before_qty,
            before_stop=before_stop,
            before_add_count=before_add_count,
            result=guard_input,
            cfg=cfg,
        )
        if repair_status is not None:
            if guarded_result is guard_input:
                return result
            if (
                isinstance(guarded_result, dict)
                and guarded_result.get("post_add_stop_guard") == "OK"
            ):
                guarded_result = dict(guarded_result)
                guarded_result.update(
                    {
                        "status": "ADDED_PROTECTION_REPAIRED",
                        "reason": (
                            "adaptive trend pyramid exchange SL repaired after "
                            f"{repair_status.lower()}"
                        ),
                        "pre_guard_status": repair_status,
                    }
                )
            return guarded_result
        return guarded_result

    guarded.__name__ = original.__name__
    guarded.__qualname__ = f"SignalEngine.{original.__name__}"
    guarded.__doc__ = original.__doc__
    guarded.__runtime_original__ = original
    SignalEngine._maybe_apply_adaptive_trend_pyramiding = guarded
    SignalEngine._adaptive_pyramid_stop_guard_installed = True
    SignalEngine._adaptive_pyramid_safe_rebuild_installed = True


__all__ = (
    "_position_increased_after_exception",
    "_prepare_post_fill_guard_input",
    "install_adaptive_pyramid_stop_guard",
)
