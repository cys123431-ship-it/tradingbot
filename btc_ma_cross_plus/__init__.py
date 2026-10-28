"""SMA3/SMA120 cross strategy with selectable A~E feature blocks (/macross)."""

from .config import (
    CONFIG_KEY,
    FEATURES,
    FEATURE_FIELDS,
    FEATURE_LABELS,
    default_ma_cross_config,
    feature_mode,
    normalize_ma_cross_config,
)
from .strategy import evaluate

__all__ = (
    "CONFIG_KEY",
    "FEATURES",
    "FEATURE_FIELDS",
    "FEATURE_LABELS",
    "default_ma_cross_config",
    "evaluate",
    "feature_mode",
    "normalize_ma_cross_config",
)
