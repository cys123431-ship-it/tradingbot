"""BTCUSDT 1h EMA trend + 15m EMA20 pullback strategy (DRY_RUN by default)."""

from .config import default_btc_pullback_config, normalize_btc_pullback_config
from .service import BtcEmaPullbackService

__all__ = ("BtcEmaPullbackService", "default_btc_pullback_config", "normalize_btc_pullback_config")
