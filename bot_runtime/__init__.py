"""Runtime composition components for the trading bot."""

from .pyramid_runtime_patch import install_adaptive_pyramid_stop_guard

install_adaptive_pyramid_stop_guard()

__all__ = ("install_adaptive_pyramid_stop_guard",)
