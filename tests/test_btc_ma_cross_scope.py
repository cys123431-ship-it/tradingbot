"""SMA3/200 strategy: alt-coin scan (top-50 by 24h volume) and trend-state entries."""
import asyncio

import pytest

from btc_ma_cross.service import BtcMaCrossService
from tests.btc_pullback_fixtures import H1, FakeBinance
from tests.test_btc_ma_cross import bars, now_after

UNKNOWN = 'binance {"code":-2013,"msg":"Order does not exist."}'


def _symbol_info(market, step="0.001", min_notional="5"):
    return {"symbol": market, "status": "TRADING", "contractType": "PERPETUAL", "pricePrecision": 4,
            "quantityPrecision": 3, "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.01", "minPrice": "0.01", "maxPrice": "1000000"},
                {"filterType": "LOT_SIZE", "minQty": step, "stepSize": step, "maxQty": "1000000"},
                {"filterType": "MARKET_LOT_SIZE", "minQty": step, "stepSize": step, "maxQty": "1000000"},
                {"filterType": "MIN_NOTIONAL", "notional": min_notional}]}


class MultiFake(FakeBinance):
    """Several USD-M markets: per-symbol candles, marks, positions."""

    def __init__(self, coins):
        super().__init__(wallet=5_000.0)
        self.coins = coins  # market -> dict(volume, rows, mark, contract_type, step, min_notional)
        self.positions = {}
        self.info = {"symbols": [_symbol_info(m, c.get("step", "0.001"), c.get("min_notional", "5"))
                                 for m, c in coins.items()]}
        self.markets = {
            self._ccxt(m): {"id": m, "symbol": self._ccxt(m), "base": m[:-4], "quote": "USDT", "settle": "USDT",
                            "active": True, "swap": True, "linear": True,
                            "info": {"contractType": c.get("contract_type", "PERPETUAL")}}
            for m, c in coins.items()
        }

    @staticmethod
    def _ccxt(market):
        return f"{market[:-4]}/USDT:USDT"

    @staticmethod
    def _market(symbol):
        return str(symbol).split(":")[0].replace("/", "")

    def load_markets(self):
        return self.markets

    def fetch_tickers(self):
        return {self._ccxt(m): {"symbol": self._ccxt(m), "quoteVolume": c["volume"]} for m, c in self.coins.items()}

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
        if timeframe == "1m":
            return self.m1
        return self.coins[self._market(symbol)]["rows"]

    def fapiPublicGetPremiumIndex(self, params=None):
        market = params["symbol"]
        return {"symbol": market, "markPrice": str(self.coins[market]["mark"]), "lastFundingRate": "0.0001",
                "nextFundingTime": "0"}

    def fapiPrivateV2GetPositionRisk(self, params=None):
        return [{"symbol": params["symbol"], "leverage": str(self.leverage), "marginType": self.margin_type}]

    def fetch_positions(self, symbols=None):
        rows = [dict(p, symbol=s) for s, p in self.positions.items()]
        if symbols:
            wanted = {self._market(s) for s in symbols}
            rows = [r for r in rows if self._market(r["symbol"]) in wanted]
        return rows

    def create_order(self, symbol, order_type, side, qty, price=None, params=None):
        params = params or {}
        cid = params.get("newClientOrderId")
        mark = self.coins[self._market(symbol)]["mark"]
        if params.get("reduceOnly"):
            pos = self.positions.pop(symbol, None)
            closed = pos["contracts"] if pos else 0
            order = {"id": str(len(self.orders) + 1), "clientOrderId": cid, "status": "closed", "filled": closed,
                     "amount": qty, "average": mark, "symbol": symbol}
            self._fill(side, closed, mark, 2)
        else:
            self.positions[symbol] = {"side": "long" if side == "buy" else "short", "contracts": qty, "entryPrice": mark}
            order = {"id": str(len(self.orders) + 1), "clientOrderId": cid, "status": "closed", "filled": qty,
                     "amount": qty, "average": mark, "symbol": symbol}
            self._fill(side, qty, mark, 1)
        self.orders.append(order)
        return order

    def fapiPrivatePostAlgoOrder(self, params):
        self.algo_attempts.append(params)
        order = {"algoId": len(self.algos) + 1, "clientAlgoId": params["clientAlgoId"], "symbol": params["symbol"],
                 "orderType": params["type"], "side": params["side"], "triggerPrice": params["triggerPrice"],
                 "workingType": params.get("workingType"), "algoStatus": "NEW"}
        self.algos[params["clientAlgoId"]] = order
        return order

    def fire_lock(self, market, price):
        symbol = self._ccxt(market)
        pos = self.positions.pop(symbol)
        for order in self.algos.values():
            if order["algoStatus"] == "NEW" and order["clientAlgoId"].split("-")[3] == "lock":
                order["algoStatus"] = "FINISHED"
        self._fill("SELL" if pos["side"] == "long" else "BUY", pos["contracts"], price, 3)


def coin(volume, last_close, base=100.0, **extra):
    rows = bars(last_close * base / 85_000.0, base=base)
    return {"volume": volume, "rows": rows, "mark": base, **extra}


class Harness:
    def __init__(self, tmp_path, coins, *, live=True, **cfg):
        self.fake = MultiFake(coins)
        any_rows = next(iter(coins.values()))["rows"]
        self.now_ms = now_after(any_rows)
        self.cfg = {"enabled": True, "trading_mode": "LIVE" if live else "DRY_RUN", "scan_scope": "alts", **cfg}
        self.tmp_path = tmp_path
        self.service = self.build()

    def build(self):
        async def no_sleep(_):
            return None

        return BtcMaCrossService(
            config_getter=lambda: self.cfg, credentials_getter=lambda n: {"api_key": "k", "secret_key": "s"},
            exchange_factory=lambda n, c: self.fake, runtime_dir=self.tmp_path, notifier=lambda t: None,
            main_network_getter=lambda: "testnet", clock=lambda: self.now_ms / 1000, sleep=no_sleep, environ={},
        )

    def run(self):
        return asyncio.run(self.service.run_cycle())

    def state(self, mode="LIVE"):
        return self.service.load_state("testnet", mode)

    def add_bar(self, market, last_close):
        rows = self.fake.coins[market]["rows"]
        prev = rows[-1][4]
        rows.append([rows[-1][0] + H1, prev, max(prev, last_close) * 1.001, min(prev, last_close) * 0.999, last_close, 1.0])

    def next_hour(self):
        for c in self.fake.coins.values():
            if len(c["rows"]) and c["rows"][-1][0] + H1 > self.now_ms - 5000:
                continue
        self.now_ms += H1


# --------------------------------------------------------------- universe
def test_universe_is_top_n_by_volume_without_stables_or_tradfi(tmp_path):
    coins = {f"C{i:02d}USDT": coin(1000 - i, 85_000) for i in range(60)}
    coins["USDCUSDT"] = coin(5000, 85_000)
    coins["AAPLUSDT"] = coin(4000, 85_000, contract_type="TRADIFI_PERPETUAL")
    h = Harness(tmp_path, coins)
    universe = asyncio.run(h.service.scan_universe("testnet", h.service.config()))
    markets = [m for _, m, _ in universe]
    assert len(markets) == 50
    assert markets[0] == "C00USDT" and markets[-1] == "C49USDT"
    assert "USDCUSDT" not in markets and "AAPLUSDT" not in markets


def test_btc_scope_ignores_alts(tmp_path):
    h = Harness(tmp_path, {"BTCUSDT": coin(1, 85_000), "ETHUSDT": coin(9, 86_000)}, scan_scope="btc")
    universe = asyncio.run(h.service.scan_universe("testnet", h.service.config()))
    assert [m for _, m, _ in universe] == ["BTCUSDT"]


# ------------------------------------------------------------ alt entries
def test_highest_volume_coin_with_a_signal_wins(tmp_path):
    coins = {
        "BTCUSDT": coin(900, 85_000),            # no cross
        "ETHUSDT": coin(800, 86_000),            # LONG cross
        "SOLUSDT": coin(700, 84_000),            # SHORT cross
    }
    h = Harness(tmp_path, coins)
    assert h.run()["action"] == "entered"
    trade = h.state()["trade"]
    assert trade["market_id"] == "ETHUSDT" and trade["side"] == "LONG"
    assert list(h.fake.positions) == ["ETH/USDT:USDT"]
    assert all(a["symbol"] == "ETHUSDT" for a in h.fake.algo_attempts)
    assert h.service.owned_position_keys() == {"ETHUSDT"}
    signal = h.service.ledger("testnet").recent_events(1, kinds={"SIGNAL"})[0]
    assert signal["evaluated"] == 3 and set(signal["crosses"]) == {"ETHUSDT:LONG", "SOLUSDT:SHORT"}


def test_unusable_coin_falls_through_to_the_next(tmp_path):
    coins = {
        "ETHUSDT": coin(800, 86_000, min_notional="100000"),   # cannot meet the minimum
        "SOLUSDT": coin(700, 84_000),
    }
    h = Harness(tmp_path, coins)
    assert h.run()["action"] == "entered"
    assert h.state()["trade"]["market_id"] == "SOLUSDT" and h.state()["trade"]["side"] == "SHORT"


def test_opposite_cross_reverses_on_the_same_alt(tmp_path):
    h = Harness(tmp_path, {"ETHUSDT": coin(800, 86_000), "SOLUSDT": coin(700, 85_000)})
    h.run()
    h.add_bar("ETHUSDT", 96.0)   # ETH SMA3 drops under SMA200
    h.add_bar("SOLUSDT", 100.0)
    h.now_ms += H1
    assert h.run()["action"] == "reversed"
    assert h.state()["trade"]["market_id"] == "ETHUSDT" and h.state()["trade"]["side"] == "SHORT"


# ------------------------------------------------------------- trend mode
def _trend_coins():
    rows = bars(86_000.0)                      # cross on the last bar
    rows.append([rows[-1][0] + H1, 86_000.0, 86_600.0, 85_900.0, 86_500.0, 1.0])  # still above, no cross
    return {"BTCUSDT": {"volume": 1, "rows": rows, "mark": 85_000.0}}


def test_cross_mode_does_not_enter_without_a_cross(tmp_path):
    h = Harness(tmp_path, _trend_coins(), scan_scope="btc")
    h.now_ms = now_after(h.fake.coins["BTCUSDT"]["rows"])
    assert h.run()["reason"] == "NO_SIGNAL"
    assert h.fake.orders == []


def test_trend_mode_enters_when_sma3_is_already_above(tmp_path):
    h = Harness(tmp_path, _trend_coins(), scan_scope="btc", entry_mode="trend")
    h.now_ms = now_after(h.fake.coins["BTCUSDT"]["rows"])
    assert h.run()["action"] == "entered"
    trade = h.state()["trade"]
    assert trade["side"] == "LONG" and trade["entry_type"] == "TREND"


def test_trend_mode_waits_for_the_next_cross_after_a_profit_lock_exit(tmp_path):
    h = Harness(tmp_path, _trend_coins(), scan_scope="btc", entry_mode="trend")
    rows = h.fake.coins["BTCUSDT"]["rows"]
    h.now_ms = now_after(rows)
    h.run()
    h.fake.coins["BTCUSDT"]["mark"] = 85_000 * 1.021   # +10.5% -> lock +9%
    h.now_ms += 20_000
    h.run()
    h.fake.fire_lock("BTCUSDT", 86_530.0)
    h.now_ms += 20_000
    assert h.run()["trade"]["exit_reason"] == "PROFIT_LOCK"
    assert h.state()["reentry_block"] == {"BTCUSDT": "LONG"}
    # Next bar: SMA3 still above SMA200 -> no new LONG (wait for a cross).
    rows.append([rows[-1][0] + H1, 86_500.0, 86_700.0, 86_400.0, 86_600.0, 1.0])
    h.now_ms = now_after(rows)
    assert h.run()["reason"] == "NO_SIGNAL"
    # A cross down releases the block and (trend mode) enters SHORT on it.
    rows.append([rows[-1][0] + H1, 86_600.0, 86_600.0, 70_000.0, 70_100.0, 1.0])
    h.now_ms = now_after(rows)
    h.fake.coins["BTCUSDT"]["mark"] = 70_100.0
    assert h.run()["action"] == "entered"
    assert h.state()["trade"]["side"] == "SHORT" and h.state()["reentry_block"] == {}


# ---------------------------------------------------------------- Telegram
def test_scope_and_entry_mode_buttons(tmp_path):
    from tests.test_btc_ma_cross import _callbacks, _controller

    controller = _controller(tmp_path)
    press = lambda a: asyncio.run(controller._handle_btc_ma_cross_action(a))  # noqa: E731
    _, markup = press("status")
    callbacks = _callbacks(markup)
    for data in ("bm:scope:btc", "bm:scope:alts", "bm:entry:cross", "bm:entry:trend"):
        assert data in callbacks
    text, _ = press("scope:alts")
    assert controller.cfg.data["btc_ma_cross"]["scan_scope"] == "alts" and "상위 50" in text
    text, _ = press("entry:trend")
    assert controller.cfg.data["btc_ma_cross"]["entry_mode"] == "trend" and "방향 유지" in text
    status, markup = press("status")
    assert "종목: 알트 포함" in status and "진입: 방향 유지" in status
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert "✅ 🌐 알트 상위50" in labels and "✅ 📈 방향 유지 중 진입" in labels
