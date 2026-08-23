"""Runtime composition components for the trading bot."""

from .adaptive_research_patch import install_adaptive_research_overlay

install_adaptive_research_overlay()

__all__ = ("install_adaptive_research_overlay",)
