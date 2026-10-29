"""SMA3/SMA120 cross strategy, C-only sizing with a TradFi scope (/macross)."""

from .config import (
    CONFIG_KEY,
    FEATURES,
    FEATURE_FIELDS,
    FEATURE_LABELS,
    default_ma_cross_config,
    feature_mode,
    normalize_ma_cross_config,
)
from .service import MaCrossService
from .strategy import evaluate
from .tradfi import (
    TRADFI_MAINNET_ONLY_REASON,
    TRADFI_SCOPE,
    TRADFI_SESSION_CLOSED_REASON,
    TradFiScopeMixin,
    market_in_tradfi_scope,
    market_is_tradfi_gold,
    market_is_tradfi_us_equity,
)

__all__ = (
    "CONFIG_KEY",
    "FEATURES",
    "FEATURE_FIELDS",
    "FEATURE_LABELS",
    "MaCrossService",
    "TRADFI_MAINNET_ONLY_REASON",
    "TRADFI_SCOPE",
    "TRADFI_SESSION_CLOSED_REASON",
    "TradFiScopeMixin",
    "default_ma_cross_config",
    "evaluate",
    "feature_mode",
    "market_in_tradfi_scope",
    "market_is_tradfi_gold",
    "market_is_tradfi_us_equity",
    "normalize_ma_cross_config",
)
