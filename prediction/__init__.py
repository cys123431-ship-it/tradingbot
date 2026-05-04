"""Prediction Micro Auto pure helpers.

The package is paper-only by default. It does not place live orders.
"""

from .client import PredictAuthRequired, PredictClient
from .lifecycle import best_paper_exit_price, evaluate_paper_position_exit
from .live_order import (
    PredictionLiveCredentials,
    PredictionLiveOrderError,
    build_live_market_order_payload,
    submit_live_market_order,
    yes_token_id_from_market,
)
from .micro_risk import (
    DEFAULT_PREDICTION_MICRO_CONFIG,
    build_prediction_micro_plan,
    default_prediction_micro_config,
    normalize_prediction_micro_config,
)
from .models import extract_market_resolution, normalize_market
from .orderbook import analyze_orderbook, normalize_orderbook_levels
from .paper_ledger import PaperLedger
from .probability import estimate_crypto_up_probability, evaluate_prediction_edge
from .report import format_prediction_report, format_prediction_research_report, prediction_reject_counts
from .strategies import PREDICTION_STRATEGY_CATALOG, score_prediction_candidate

__all__ = [
    "DEFAULT_PREDICTION_MICRO_CONFIG",
    "PREDICTION_STRATEGY_CATALOG",
    "PaperLedger",
    "PredictAuthRequired",
    "PredictClient",
    "PredictionLiveCredentials",
    "PredictionLiveOrderError",
    "analyze_orderbook",
    "build_live_market_order_payload",
    "build_prediction_micro_plan",
    "default_prediction_micro_config",
    "best_paper_exit_price",
    "evaluate_paper_position_exit",
    "extract_market_resolution",
    "estimate_crypto_up_probability",
    "evaluate_prediction_edge",
    "format_prediction_report",
    "format_prediction_research_report",
    "normalize_market",
    "normalize_prediction_micro_config",
    "normalize_orderbook_levels",
    "prediction_reject_counts",
    "score_prediction_candidate",
    "submit_live_market_order",
    "yes_token_id_from_market",
]
