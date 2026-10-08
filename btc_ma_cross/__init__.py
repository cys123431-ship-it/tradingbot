"""BTCUSDT SMA3/SMA200 cross strategy (stop-and-reverse, DRY_RUN by default)."""

from .config import default_btc_ma_cross_config, normalize_btc_ma_cross_config
from .service import BtcMaCrossService

__all__ = ("BtcMaCrossService", "default_btc_ma_cross_config", "normalize_btc_ma_cross_config")
