"""Live Predict.fun order helpers.

This module keeps live order creation behind explicit credentials and runtime
guards. It does not read config files and never stores secrets.
"""

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass

from .orderbook import normalize_orderbook_levels


WEI = 10**18


class PredictionLiveOrderError(RuntimeError):
    pass


@dataclass
class PredictionLiveCredentials:
    api_key: str = ""
    jwt_token: str = ""
    private_key: str = ""
    predict_account: str = ""
    chain_id: int = 56
    env_live_enabled: bool = False

    @classmethod
    def from_env(cls, env=None):
        env = env or os.environ
        return cls(
            api_key=str(env.get("PREDICTION_API_KEY") or "").strip(),
            jwt_token=str(env.get("PREDICTION_JWT") or "").strip(),
            private_key=str(env.get("PREDICTION_PRIVATE_KEY") or "").strip(),
            predict_account=str(env.get("PREDICTION_PREDICT_ACCOUNT") or "").strip(),
            chain_id=int(float(env.get("PREDICTION_CHAIN_ID") or 56)),
            env_live_enabled=str(env.get("PREDICTION_LIVE_TRADING_ENABLED") or "").strip().lower()
            in {"1", "true", "yes", "on", "enabled", "unlocked"},
        )

    def missing_reasons(self):
        reasons = []
        if not self.env_live_enabled:
            reasons.append("PREDICTION_LIVE_ENV_LOCKED")
        if not self.api_key:
            reasons.append("PREDICTION_API_KEY_MISSING")
        if not self.jwt_token:
            reasons.append("PREDICTION_JWT_MISSING")
        if not self.private_key:
            reasons.append("PREDICTION_PRIVATE_KEY_MISSING")
        return reasons


def yes_token_id_from_market(market):
    raw = (market or {}).get("raw") if isinstance(market, dict) else market
    raw = raw or {}
    for outcome in raw.get("outcomes") or []:
        if not isinstance(outcome, dict):
            continue
        if str(outcome.get("name") or "").strip().lower() == "yes":
            token_id = outcome.get("onChainId") or outcome.get("tokenId") or outcome.get("token_id")
            if token_id:
                return str(token_id)
    token_id = raw.get("yesTokenId") or raw.get("yes_token_id") or raw.get("tokenId")
    return str(token_id) if token_id else ""


def _bool_raw(raw, *keys, default=False):
    for key in keys:
        if key in raw:
            return bool(raw.get(key))
    return default


def _sdk_chain_id(sdk, chain_id):
    chain = getattr(sdk, "ChainId")
    if int(chain_id) == 97:
        return chain.BNB_TESTNET
    return chain.BNB_MAINNET


def _signed_order_dict(signed_order, order_hash=None):
    if dataclasses.is_dataclass(signed_order):
        payload = dataclasses.asdict(signed_order)
    elif isinstance(signed_order, dict):
        payload = dict(signed_order)
    else:
        payload = dict(getattr(signed_order, "__dict__", {}) or {})
    if order_hash:
        payload["hash"] = order_hash
    for key, value in list(payload.items()):
        if hasattr(value, "value"):
            payload[key] = value.value
    if "token_id" in payload:
        payload["tokenId"] = str(payload.pop("token_id"))
    if "maker_amount" in payload:
        payload["makerAmount"] = str(payload.pop("maker_amount"))
    if "taker_amount" in payload:
        payload["takerAmount"] = str(payload.pop("taker_amount"))
    if "fee_rate_bps" in payload:
        payload["feeRateBps"] = str(payload.pop("fee_rate_bps"))
    if "signature_type" in payload:
        payload["signatureType"] = int(payload.pop("signature_type"))
    if "side" in payload:
        payload["side"] = int(payload["side"])
    for key in ("salt", "maker", "signer", "taker", "expiration", "nonce", "signature", "hash"):
        if key in payload and payload[key] is not None:
            payload[key] = str(payload[key])
    return payload


def build_live_market_order_payload(
    *,
    market,
    orderbook_payload,
    stake_usdt,
    slippage_bps=50,
    credentials=None,
    sdk_module=None,
):
    credentials = credentials or PredictionLiveCredentials.from_env()
    missing = credentials.missing_reasons()
    if missing:
        raise PredictionLiveOrderError(",".join(missing))
    token_id = yes_token_id_from_market(market)
    if not token_id:
        raise PredictionLiveOrderError("PREDICTION_YES_TOKEN_ID_MISSING")

    try:
        sdk = sdk_module or __import__("predict_sdk")
    except Exception as exc:
        raise PredictionLiveOrderError("PREDICTION_SDK_NOT_INSTALLED") from exc

    levels = normalize_orderbook_levels(orderbook_payload)
    if not levels["asks"]:
        raise PredictionLiveOrderError("PREDICTION_ORDERBOOK_ASKS_EMPTY")

    raw = (market or {}).get("raw") or {}
    stake_wei = int(round(float(stake_usdt) * WEI))
    builder_options = sdk.OrderBuilderOptions(
        predict_account=credentials.predict_account or None,
    )
    builder = sdk.OrderBuilder.make(
        _sdk_chain_id(sdk, credentials.chain_id),
        credentials.private_key,
        builder_options,
    )
    book = sdk.Book(
        market_id=int(float(levels["market_id"] or (market or {}).get("id") or 0)),
        update_timestamp_ms=int(levels["update_timestamp_ms"] or 0),
        asks=list(levels["asks"]),
        bids=list(levels["bids"]),
    )
    amounts = builder.get_market_order_amounts(
        sdk.MarketHelperValueInput(
            side=sdk.Side.BUY,
            value_wei=stake_wei,
            slippage_bps=int(slippage_bps or 0),
            is_min_amount_out=True,
        ),
        book,
    )
    order = builder.build_order(
        "MARKET",
        sdk.BuildOrderInput(
            side=sdk.Side.BUY,
            token_id=token_id,
            maker_amount=str(amounts.maker_amount),
            taker_amount=str(amounts.taker_amount),
            fee_rate_bps=int(float((market or {}).get("fee_rate_bps") or raw.get("feeRateBps") or 200)),
        ),
    )
    typed_data = builder.build_typed_data(
        order,
        is_neg_risk=_bool_raw(raw, "isNegRisk", "is_neg_risk", default=False),
        is_yield_bearing=_bool_raw(raw, "isYieldBearing", "is_yield_bearing", default=False),
    )
    signed_order = builder.sign_typed_data_order(typed_data)
    order_hash = builder.build_typed_data_hash(typed_data)
    order_payload = _signed_order_dict(signed_order, order_hash=order_hash)
    return {
        "data": {
            "pricePerShare": str(amounts.price_per_share),
            "strategy": "MARKET",
            "slippageBps": str(int(slippage_bps or 0)),
            "isFillOrKill": True,
            "isPostOnly": False,
            "reservedBalancePolicy": "REJECT_MARKET_ORDER",
            "isMinAmountOut": True,
            "selfTradePrevention": "CANCEL_MAKER",
            "order": order_payload,
        }
    }


def submit_live_market_order(*, client, market, orderbook_payload, plan, cfg, credentials=None, sdk_module=None):
    payload = build_live_market_order_payload(
        market=market,
        orderbook_payload=orderbook_payload,
        stake_usdt=float((plan or {}).get("stake_usdt") or 0.0),
        slippage_bps=int((cfg or {}).get("live_slippage_bps", 50) or 50),
        credentials=credentials,
        sdk_module=sdk_module,
    )
    response = client.create_order(payload)
    return {
        "accepted": True,
        "payload": payload,
        "response": response,
        "order_id": ((response or {}).get("data") or {}).get("orderId"),
        "order_hash": ((response or {}).get("data") or {}).get("orderHash")
        or (((payload or {}).get("data") or {}).get("order") or {}).get("hash"),
    }
