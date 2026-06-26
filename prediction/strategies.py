"""Prediction strategy catalog and scoring helpers."""

from __future__ import annotations


_FIRST_50 = [
    "BTC/ETH UpDown probability model",
    "Brownian bridge directional model",
    "Late-window drift continuation",
    "Opening-noise delay filter",
    "Final minutes momentum",
    "Final spread veto",
    "Spot/futures divergence",
    "Funding overheating fade",
    "Open interest breakout",
    "Liquidation burst signal",
    "VWAP directional filter",
    "Taker imbalance",
    "ATR no-trade band",
    "High-vol market-order veto",
    "Mid-price mean reversion",
    "YES/NO parity check",
    "Predict/Polymarket divergence",
    "Predict/Kalshi divergence",
    "Sportsbook odds cross-check",
    "News delay reaction",
    "Maker spread capture",
    "Orderbook imbalance",
    "Thin book veto",
    "Fee-adjusted market making",
    "Stale quote cancel",
    "Depth vacuum breakout",
    "Whale trade follow",
    "Volume acceleration",
    "Volume without price fade",
    "Probability spike fade",
    "Resolution-rule parser",
    "Ambiguous source veto",
    "High-dispute category veto",
    "Lineup/news sports research",
    "Macro release window",
    "Crypto price target option model",
    "Yield-adjusted long market",
    "Fee burden veto",
    "High-prob tail risk guard",
    "Low-prob lottery veto",
    "Quarter Kelly sizing",
    "Market max loss cap",
    "Category exposure cap",
    "Daily loss stop",
    "Consecutive loss stop",
    "Pre-resolution exit",
    "Auto redeem",
    "API stale stop",
    "Testnet replay gate",
    "Live rollout gate",
]

_SECOND_50 = [
    "Fee-adjusted EV",
    "Brownian bridge UpDown probability",
    "Distance-to-open z-score",
    "Realized volatility adjusted edge",
    "Intraperiod drift from short EMA log returns",
    "Binance taker buy/sell imbalance",
    "CVD slope continuation",
    "Orderbook imbalance",
    "Spread and market impact veto",
    "Stale quote and latency veto",
    "Open interest acceleration",
    "Funding crowding fade",
    "Liquidation cluster continuation/fade",
    "Perp basis premium/discount",
    "Spot-futures divergence",
    "Volume shock confirmation",
    "Volatility expansion entry",
    "Volatility crush no-trade",
    "Jump/news candle detector",
    "Macro event cooling window",
    "Predict.fun vs Polymarket/Kalshi divergence",
    "Macro consensus divergence",
    "Options-implied probability comparison",
    "Closing-line value tracking",
    "Brier score by category",
    "Favorite-longshot bias adjustment",
    "High-probability favorite filter",
    "Low-probability lottery veto",
    "Category liquidity whitelist",
    "Resolution-rule ambiguity score",
    "Oracle/dispute risk score",
    "Market age filter",
    "Expiry window filter",
    "Late-stage sell-to-lock profit",
    "Trailing probability stop",
    "Maker midpoint quote",
    "Taker only when edge exceeds friction",
    "Maker order timeout",
    "Queue age/partial-fill handling",
    "Correlated event exposure cap",
    "Contradictory contract checker",
    "YES/NO parity checker",
    "Same-event bundle no-arb checker",
    "Event-source confidence score",
    "Liquidity threshold by stake size",
    "Sudden probability gap alert",
    "Whale/public large trade alert",
    "Macro lead-lag to BTC volatility",
    "Paper-only calibration retraining gate",
    "Futures bridge signal candidate",
]


def _make_catalog():
    rows = []
    for idx, name in enumerate(_FIRST_50 + _SECOND_50, 1):
        family = "Crypto" if idx <= 20 or idx in {52, 53, 54, 55, 56, 57, 61, 62, 63, 64, 65} else "Market Structure"
        if idx in {35, 70, 72, 98}:
            family = "Macro"
        if idx in {16, 17, 18, 19, 71, 73, 91, 92, 93}:
            family = "Arbitrage"
        if idx in {31, 32, 33, 80, 81, 94}:
            family = "Resolution Risk"
        rows.append({
            "id": idx,
            "name": name,
            "family": family,
            "status": "paper_research",
            "paper_only": True,
        })
    return rows


PREDICTION_STRATEGY_CATALOG = _make_catalog()


def score_prediction_candidate(market, orderbook, edge_result, micro_plan):
    market = market or {}
    orderbook = orderbook or {}
    edge_result = edge_result or {}
    micro_plan = micro_plan or {}
    liquidity = 25.0 if orderbook.get("accepted") else 0.0
    if orderbook.get("yes_spread_pct") is not None:
        liquidity = max(0.0, 25.0 - float(orderbook["yes_spread_pct"]) * 2.0)
    edge_score = max(0.0, min(35.0, float(edge_result.get("edge") or 0.0) / 0.12 * 35.0))
    category_score = 20.0 if market.get("market_type") in {"crypto", "macro"} else 0.0
    risk_score = 20.0 if micro_plan.get("accepted") else 0.0
    total = round(liquidity + edge_score + category_score + risk_score, 2)
    return {
        "score": total,
        "component_scores": {
            "liquidity": round(liquidity, 2),
            "edge": round(edge_score, 2),
            "category": category_score,
            "micro_risk": risk_score,
        },
        "selected_strategy_ids": [51, 52, 56, 59] if market.get("market_type") == "crypto" else [70, 72, 80, 98],
        "accepted": total >= 60.0 and micro_plan.get("accepted") is True,
    }
