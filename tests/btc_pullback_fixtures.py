"""Synthetic candles and a fake Binance USD-M exchange for BTC pullback tests."""
from __future__ import annotations

H1, M15, M1 = 3_600_000, 900_000, 60_000
T0 = 1_790_000_000_000 - (1_790_000_000_000 % H1)
UNKNOWN = 'binance {"code":-2013,"msg":"Order does not exist."}'


def trend_1h(n=220, start=80_000.0, step=0.001):
    rows, price = [], start
    for i in range(n):
        o, c = price, price * (1 + step)
        rows.append([T0 + i * H1, o, max(o, c) * 1.0005, min(o, c) * 0.9995, c, 100.0])
        price = c
    return rows


def flat_1h(n=220, start=80_000.0):
    rows = []
    for i in range(n):
        close = start * (1.0004 if i % 2 else 0.9996)
        rows.append([T0 + i * H1, start, start * 1.0008, start * 0.9992, close, 100.0])
    return rows


def long_setup_15m(n=220, start=80_000.0, step=0.0003, confirm_scale=1.0):
    """Uptrend, 3-bar pullback into EMA20, then a bullish breakout bar."""
    rows, price = [], start
    end_ms = T0 + 220 * H1  # same close time as trend_1h()
    first = end_ms - n * M15
    for i in range(n - 4):
        o, c = price, price * (1 + step)
        rows.append([first + i * M15, o, c * 1.0010, o * 0.9990, c, 50.0])
        price = c
    for k in range(3):
        o, c = price, price * (1 - 0.0011)
        rows.append([first + (n - 4 + k) * M15, o, o * 1.0002, c * 0.9996, c, 50.0])
        price = c
    prev_high = rows[-1][2]
    o = price
    c = max(prev_high * 1.0004, o * (1 + 0.0016 * confirm_scale))
    rows.append([first + (n - 1) * M15, o, c * 1.0002, o * 0.9997, c, 80.0])
    return rows


def mirror(rows, center=160_000.0):
    return [[r[0], center - r[1], center - r[3], center - r[2], center - r[4], r[5]] for r in rows]


def now_after(rows15, seconds=5):
    return rows15[-1][0] + M15 + seconds * 1000


def exchange_info(step="0.001", min_qty="0.001", min_notional="50", tick="0.10", status="TRADING"):
    return {"symbols": [{
        "symbol": "BTCUSDT", "status": status, "contractType": "PERPETUAL",
        "pricePrecision": 2, "quantityPrecision": 3,
        "filters": [
            {"filterType": "PRICE_FILTER", "tickSize": tick, "minPrice": "556.80", "maxPrice": "4529764"},
            {"filterType": "LOT_SIZE", "minQty": min_qty, "stepSize": step, "maxQty": "1000"},
            {"filterType": "MARKET_LOT_SIZE", "minQty": min_qty, "stepSize": step, "maxQty": "120"},
            {"filterType": "MIN_NOTIONAL", "notional": min_notional},
        ],
    }]}


class FakeBinance:
    """Just enough of ccxt.binance (USD-M) for the service under test."""

    def __init__(self, *, h1=None, m15=None, m1=None, mark=85_000.0, wallet=5_000.0, info=None):
        self.h1, self.m15, self.m1 = h1 or [], m15 or [], m1 or []
        self.mark = mark
        self.wallet = wallet
        self.info = info or exchange_info()
        self.position = None
        self.other_positions = []  # positions on other symbols (main bot / manual)
        self.orders = []
        self.algos = {}
        self.algo_attempts = []
        self.fail_sl_creates = 0
        self.fail_tp_creates = 0
        self.breach_sl = False
        self.fill_ratio = 1.0
        self.raise_positions = False
        self.raise_klines = False
        self.dual = False
        self.leverage = 3
        self.margin_type = "isolated"
        self.user_trades = []
        self.income = []

    # --- public
    def load_markets(self):
        return {}

    def fapiPublicGetExchangeInfo(self, params=None):
        return self.info

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
        if self.raise_klines:
            raise ConnectionError("websocket/REST disconnected")
        return {"1h": self.h1, "15m": self.m15, "1m": self.m1}[timeframe]

    def fapiPublicGetPremiumIndex(self, params=None):
        return {"symbol": "BTCUSDT", "markPrice": str(self.mark), "lastFundingRate": "0.0001", "nextFundingTime": "0"}

    # --- account
    def fapiPrivateGetCommissionRate(self, params=None):
        return {"symbol": "BTCUSDT", "takerCommissionRate": "0.0004", "makerCommissionRate": "0.0002"}

    def fetch_balance(self):
        w = str(self.wallet)
        return {"info": {"totalWalletBalance": w, "availableBalance": w, "totalMarginBalance": w}}

    def fapiPrivateGetPositionSideDual(self, params=None):
        return {"dualSidePosition": self.dual}

    def set_margin_mode(self, mode, symbol):
        self.margin_type = mode

    def set_leverage(self, leverage, symbol):
        self.leverage = leverage

    def fapiPrivateV2GetPositionRisk(self, params=None):
        return [{"symbol": "BTCUSDT", "leverage": str(self.leverage), "marginType": self.margin_type}]

    def fetch_positions(self, symbols=None):
        if self.raise_positions:
            raise TimeoutError("positions timeout")
        rows = [dict(self.position, symbol="BTC/USDT:USDT")] if self.position else []
        if symbols is None:
            rows += [dict(p) for p in self.other_positions]
        return rows

    # --- regular orders
    def fapiPrivateGetOrder(self, params):
        cid = params.get("origClientOrderId")
        for order in self.orders:
            if order["clientOrderId"] == cid:
                return order
        raise Exception(UNKNOWN)

    def fetch_open_orders(self, symbol=None):
        return []

    def cancel_order(self, order_id, symbol=None):
        return {"id": order_id, "status": "canceled"}

    def _fill(self, side, qty, price, time_ms):
        self.user_trades.append({"side": side.upper(), "qty": str(qty), "price": str(price),
                                 "commission": str(price * qty * 0.0004), "commissionAsset": "USDT",
                                 "time": time_ms})

    def create_order(self, symbol, order_type, side, qty, price=None, params=None):
        params = params or {}
        cid = params.get("newClientOrderId")
        if params.get("reduceOnly"):
            closed = self.position["contracts"] if self.position else 0
            self.position = None
            order = {"id": str(len(self.orders) + 1), "clientOrderId": cid, "status": "closed",
                     "filled": closed, "amount": qty, "average": self.mark}
            self._fill(side, closed, self.mark, 2)
        else:
            filled = round(qty * self.fill_ratio, 6)
            self.position = {"side": "long" if side == "buy" else "short", "contracts": filled,
                             "entryPrice": self.mark}
            status = "closed" if self.fill_ratio >= 1 else "open"
            order = {"id": str(len(self.orders) + 1), "clientOrderId": cid, "status": status,
                     "filled": filled, "amount": qty, "average": self.mark}
            self._fill(side, filled, self.mark, 1)
        self.orders.append(order)
        return order

    # --- algo (conditional) orders
    def fapiPrivatePostAlgoOrder(self, params):
        self.algo_attempts.append(params)
        kind = params["type"]
        if kind == "STOP_MARKET":
            if self.breach_sl:
                raise Exception('binance {"code":-2021,"msg":"Order would immediately trigger."}')
            if self.fail_sl_creates > 0:
                self.fail_sl_creates -= 1
                raise TimeoutError("algo order timeout")
        if kind == "TAKE_PROFIT_MARKET" and self.fail_tp_creates > 0:
            self.fail_tp_creates -= 1
            raise TimeoutError("algo order timeout")
        order = {"algoId": len(self.algos) + 1, "clientAlgoId": params["clientAlgoId"], "symbol": "BTCUSDT",
                 "orderType": kind, "side": params["side"], "triggerPrice": params["triggerPrice"],
                 "closePosition": params.get("closePosition"), "workingType": params.get("workingType"),
                 "algoStatus": "NEW"}
        self.algos[params["clientAlgoId"]] = order
        return order

    def fapiPrivateGetAlgoOrder(self, params):
        order = self.algos.get(params["clientAlgoId"])
        if order is None:
            raise Exception(UNKNOWN)
        return order

    def fapiPrivateGetOpenAlgoOrders(self, params=None):
        return [o for o in self.algos.values() if o["algoStatus"] == "NEW"]

    def fapiPrivateDeleteAlgoOrder(self, params):
        order = self.algos.get(params["clientAlgoId"])
        if order is None:
            raise Exception('binance {"code":-2011,"msg":"Unknown order sent."}')
        order["algoStatus"] = "CANCELED"
        return order

    def trigger(self, kind, price):
        """Simulate Binance firing the SL or TP trigger."""
        side = "SELL" if self.position["side"] == "long" else "BUY"
        qty = self.position["contracts"]
        for order in self.algos.values():
            if order["algoStatus"] == "NEW" and order["orderType"] == kind:
                order["algoStatus"] = "FINISHED"
        self.position = None
        self._fill(side, qty, price, 3)

    def fapiPrivateGetUserTrades(self, params):
        return list(self.user_trades)

    def fapiPrivateGetIncome(self, params):
        return list(self.income)
