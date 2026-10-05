"""Binance trading-rule parsing, Decimal rounding and pre-trade quantity/risk validation."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, Decimal, InvalidOperation

from .config import SIZING_FIXED, SIZING_RISK_BASED


class TradingRulesError(ValueError):
    """Exchange rules are missing or malformed: trading must not proceed."""


def D(value, default=None) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        if default is None:
            raise TradingRulesError(f"not a number: {value!r}")
        return Decimal(str(default))
    if not result.is_finite():
        if default is None:
            raise TradingRulesError(f"not a finite number: {value!r}")
        return Decimal(str(default))
    return result


@dataclass(frozen=True)
class TradingRules:
    symbol: str
    tick_size: Decimal
    min_price: Decimal
    max_price: Decimal
    min_qty: Decimal
    step_size: Decimal
    max_qty: Decimal
    market_min_qty: Decimal
    market_step_size: Decimal
    market_max_qty: Decimal
    min_notional: Decimal
    quantity_precision: int
    price_precision: int
    status: str = "TRADING"
    contract_type: str = "PERPETUAL"

    @property
    def effective_min_qty(self) -> Decimal:
        return max(self.min_qty, self.market_min_qty)

    @property
    def effective_step(self) -> Decimal:
        return max(self.step_size, self.market_step_size)

    @property
    def effective_max_qty(self) -> Decimal:
        return min(self.max_qty, self.market_max_qty)

    def as_log(self) -> dict:
        return {key: str(value) for key, value in asdict(self).items()}


def parse_trading_rules(symbol_info: dict) -> TradingRules:
    """Parse one ``exchangeInfo.symbols[]`` entry (raw Binance JSON)."""
    if not isinstance(symbol_info, dict) or not symbol_info.get("symbol"):
        raise TradingRulesError("exchangeInfo symbol entry missing")
    filters = {}
    for row in symbol_info.get("filters") or []:
        if isinstance(row, dict) and row.get("filterType"):
            filters[str(row["filterType"])] = row
    price = filters.get("PRICE_FILTER")
    lot = filters.get("LOT_SIZE")
    if not price or not lot:
        raise TradingRulesError("PRICE_FILTER/LOT_SIZE filters missing")
    market_lot = filters.get("MARKET_LOT_SIZE") or lot
    notional = filters.get("MIN_NOTIONAL") or filters.get("NOTIONAL") or {}
    rules = TradingRules(
        symbol=str(symbol_info["symbol"]),
        tick_size=D(price.get("tickSize")),
        min_price=D(price.get("minPrice"), 0),
        max_price=D(price.get("maxPrice"), 0),
        min_qty=D(lot.get("minQty")),
        step_size=D(lot.get("stepSize")),
        max_qty=D(lot.get("maxQty"), "1e18"),
        market_min_qty=D(market_lot.get("minQty"), lot.get("minQty")),
        market_step_size=D(market_lot.get("stepSize"), lot.get("stepSize")),
        market_max_qty=D(market_lot.get("maxQty"), lot.get("maxQty")),
        min_notional=D(notional.get("notional", notional.get("minNotional", 0)), 0),
        quantity_precision=int(symbol_info.get("quantityPrecision") or 0),
        price_precision=int(symbol_info.get("pricePrecision") or 0),
        status=str(symbol_info.get("status") or ""),
        contract_type=str(symbol_info.get("contractType") or ""),
    )
    if rules.tick_size <= 0 or rules.step_size <= 0 or rules.min_qty <= 0:
        raise TradingRulesError("non-positive tickSize/stepSize/minQty")
    return rules


def rules_from_exchange_info(payload: dict, symbol: str) -> TradingRules:
    for row in (payload or {}).get("symbols") or []:
        if isinstance(row, dict) and str(row.get("symbol")) == symbol:
            return parse_trading_rules(row)
    raise TradingRulesError(f"{symbol} not found in exchangeInfo")


def floor_to_step(value, step) -> Decimal:
    value, step = D(value), D(step)
    if step <= 0:
        raise TradingRulesError("step must be positive")
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def ceil_to_step(value, step) -> Decimal:
    value, step = D(value), D(step)
    if step <= 0:
        raise TradingRulesError("step must be positive")
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step


def round_price_to_tick(price, tick, direction="nearest") -> Decimal:
    price, tick = D(price), D(tick)
    if tick <= 0:
        raise TradingRulesError("tickSize must be positive")
    units = price / tick
    if direction == "down":
        units = units.to_integral_value(rounding=ROUND_FLOOR)
    elif direction == "up":
        units = units.to_integral_value(rounding=ROUND_CEILING)
    else:
        units = units.quantize(Decimal(1))
    # Keep only the tick's real decimals (tick "0.10" -> 1 place, "10" -> 0).
    places = min(tick.normalize().as_tuple().exponent, 0)
    return (units * tick).quantize(Decimal(1).scaleb(places))


def protection_prices(side, avg_entry_price, cfg, rules: TradingRules):
    """SL/TP from the *actual* average fill, rounded to tickSize.

    Rounding always moves the trigger towards the entry: the stop never ends
    up further away than ``stop_loss_pct`` and the target is never further
    than ``take_profit_pct``.
    """
    entry = D(avg_entry_price)
    if entry <= 0:
        raise TradingRulesError("average entry price must be positive")
    sl_pct, tp_pct = D(cfg["stop_loss_pct"]), D(cfg["take_profit_pct"])
    if str(side).upper() == "LONG":
        stop = round_price_to_tick(entry * (1 - sl_pct), rules.tick_size, "up")
        take = round_price_to_tick(entry * (1 + tp_pct), rules.tick_size, "down")
    else:
        stop = round_price_to_tick(entry * (1 + sl_pct), rules.tick_size, "down")
        take = round_price_to_tick(entry * (1 - tp_pct), rules.tick_size, "up")
    return stop, take


def estimate_trade_risk(price, quantity, cfg, taker_fee_rate) -> dict:
    """Loss if the stop fills, including taker fees on both legs and slippage."""
    price, qty = D(price), D(quantity)
    sl_pct = D(cfg["stop_loss_pct"])
    fee_rate = D(taker_fee_rate)
    slippage = D(cfg["slippage_buffer_pct"])
    notional = price * qty
    # The stop distance is symmetric for LONG/SHORT; the exit fee uses the
    # higher (SHORT-side) stop price so it is never underestimated.
    price_risk = price * sl_pct * qty
    entry_fee = notional * fee_rate
    exit_fee = price * (1 + sl_pct) * qty * fee_rate
    slippage_cost = notional * slippage
    return {
        "notional": notional,
        "price_risk": price_risk,
        "estimated_entry_fee": entry_fee,
        "estimated_exit_fee": exit_fee,
        "slippage_buffer": slippage_cost,
        "estimated_total_risk": price_risk + entry_fee + exit_fee + slippage_cost,
    }


def _risk_per_unit(price, cfg, taker_fee_rate) -> Decimal:
    return estimate_trade_risk(price, 1, cfg, taker_fee_rate)["estimated_total_risk"]


def validate_and_normalize_order_quantity(
    *,
    rules: TradingRules,
    price,
    wallet_balance,
    available_balance,
    cfg,
    taker_fee_rate,
    sizing_mode=SIZING_FIXED,
) -> dict:
    """Decide the order quantity or SKIP with a precise reason.

    1. base quantity (fixed) or risk-budget quantity (risk_based)
    2. minQty, 3. stepSize, 4. minNotional, 5. current price,
    6. estimated total risk, 7. max_risk_per_trade_pct, plus margin.
    Never raises the leverage and never silently raises the quantity.
    """
    price = D(price)
    wallet = D(wallet_balance, 0)
    available = D(available_balance, 0)
    leverage = D(cfg["leverage"])
    risk_limit = wallet * D(cfg["max_risk_per_trade_pct"])
    step = rules.effective_step
    minimum_qty = rules.effective_min_qty
    decision = {
        "accepted": False,
        "sizing_mode": sizing_mode,
        "price": price,
        "wallet_balance": wallet,
        "available_balance": available,
        "risk_limit": risk_limit,
        "leverage": leverage,
        "skip_reason": "",
    }
    if rules.status and rules.status != "TRADING":
        decision["skip_reason"] = f"SYMBOL_NOT_TRADING:{rules.status}"
        return decision
    if price <= 0:
        decision["skip_reason"] = "INVALID_PRICE"
        return decision
    if wallet <= 0:
        decision["skip_reason"] = "WALLET_BALANCE_UNAVAILABLE"
        return decision

    if sizing_mode == SIZING_RISK_BASED:
        requested = risk_limit / _risk_per_unit(price, cfg, taker_fee_rate)
    else:
        requested = D(cfg["base_quantity"])
    quantity = floor_to_step(requested, step)
    decision["requested_quantity"] = requested

    required_min = max(minimum_qty, ceil_to_step(rules.min_notional / price, step))
    decision["exchange_minimum_quantity"] = required_min
    if quantity < required_min:
        risk_at_min = estimate_trade_risk(price, required_min, cfg, taker_fee_rate)
        decision["risk_at_exchange_minimum"] = risk_at_min["estimated_total_risk"]
        if not cfg.get("allow_auto_raise_to_exchange_minimum"):
            decision["skip_reason"] = (
                f"BELOW_EXCHANGE_MINIMUM: quantity {quantity} < required {required_min} "
                f"(minQty {minimum_qty}, minNotional {rules.min_notional} USDT @ {price}); "
                f"risk at required {risk_at_min['estimated_total_risk']:.4f} vs limit {risk_limit:.4f} USDT; "
                "auto raise disabled"
            )
            return decision
        if risk_at_min["estimated_total_risk"] > risk_limit:
            decision["skip_reason"] = (
                f"EXCHANGE_MINIMUM_EXCEEDS_RISK_LIMIT: required {required_min} risk "
                f"{risk_at_min['estimated_total_risk']:.4f} > limit {risk_limit:.4f} USDT"
            )
            return decision
        quantity = required_min
        decision["raised_to_exchange_minimum"] = True

    if quantity > rules.effective_max_qty:
        decision["skip_reason"] = f"ABOVE_MAX_QTY: {quantity} > {rules.effective_max_qty}"
        return decision

    risk = estimate_trade_risk(price, quantity, cfg, taker_fee_rate)
    decision.update(risk)
    decision["quantity"] = quantity
    decision["estimated_margin"] = risk["notional"] / leverage
    if risk["estimated_total_risk"] > risk_limit:
        decision["skip_reason"] = (
            f"RISK_LIMIT: estimated total risk {risk['estimated_total_risk']:.4f} > "
            f"{D(cfg['max_risk_per_trade_pct']) * 100}% of wallet = {risk_limit:.4f} USDT"
        )
        return decision
    margin_cap = available * D(cfg["max_margin_usage_pct"])
    if decision["estimated_margin"] > margin_cap:
        decision["skip_reason"] = (
            f"INSUFFICIENT_MARGIN: needs {decision['estimated_margin']:.4f} > "
            f"usable {margin_cap:.4f} USDT"
        )
        return decision
    decision["accepted"] = True
    return decision


__all__ = (
    "D",
    "TradingRules",
    "TradingRulesError",
    "ceil_to_step",
    "estimate_trade_risk",
    "floor_to_step",
    "parse_trading_rules",
    "protection_prices",
    "round_price_to_tick",
    "rules_from_exchange_info",
    "validate_and_normalize_order_quantity",
)
