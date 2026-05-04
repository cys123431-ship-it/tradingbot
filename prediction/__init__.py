"""Prediction Micro Auto pure helpers.

The package is paper-only by default. It does not place live orders.
"""

from .client import PredictAuthRequired, PredictClient
from .micro_risk import (
    DEFAULT_PREDICTION_MICRO_CONFIG,
    build_prediction_micro_plan,
    default_prediction_micro_config,
    normalize_prediction_micro_config,
)
from .models import normalize_market
from .orderbook import analyze_orderbook
from .paper_ledger import PaperLedger
from .probability import estimate_crypto_up_probability, evaluate_prediction_edge
from .report import format_prediction_report
from .strategies import PREDICTION_STRATEGY_CATALOG, score_prediction_candidate

__all__ = [
    "DEFAULT_PREDICTION_MICRO_CONFIG",
    "PREDICTION_STRATEGY_CATALOG",
    "PaperLedger",
    "PredictAuthRequired",
    "PredictClient",
    "analyze_orderbook",
    "build_prediction_micro_plan",
    "default_prediction_micro_config",
    "estimate_crypto_up_probability",
    "evaluate_prediction_edge",
    "format_prediction_report",
    "normalize_market",
    "normalize_prediction_micro_config",
    "score_prediction_candidate",
]
