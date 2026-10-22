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
    # BTC is left out of the alt scope.
    assert signal["evaluated"] == 2 and set(signal["crosses"]) == {"ETHUSDT:LONG", "SOLUSDT:SHORT"}


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
    h = Harness(tmp_path, _trend_coins(), scan_scope="btc", entry_mode="trend", reentry_wait_bars=0)
    rows = h.fake.coins["BTCUSDT"]["rows"]
    h.now_ms = now_after(rows)
    h.run()
    h.fake.coins["BTCUSDT"]["mark"] = 85_000 * 1.021   # +10.5% -> lock +9%
    h.now_ms += 20_000
    h.run()
    h.fake.fire_lock("BTCUSDT", 86_530.0)
    h.now_ms += 20_000
    assert h.run()["trade"]["exit_reason"] == "PROFIT_LOCK"
    assert h.state()["reentry_block"]["BTCUSDT"]["side"] == "LONG"
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
    assert "종목: 알트 전용" in status and "BTC 제외" in status and "진입: 방향 유지" in status
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert "✅ 🌐 알트 상위50" in labels and "✅ 📈 방향 유지 중 진입" in labels


def test_testnet_universe_is_ranked_by_mainnet_volume(tmp_path):
    demo = MultiFake({
        "FXSUSDT": coin(99_999, 85_000),     # fake demo volume, not listed on mainnet
        "BTCUSDT": coin(1, 85_000),
        "ETHUSDT": coin(2, 85_000),
    })
    mainnet = MultiFake({
        "BTCUSDT": coin(900, 85_000),
        "ETHUSDT": coin(800, 85_000),
        "SOLUSDT": coin(700, 85_000),        # not listed on demo
    })

    async def no_sleep(_):
        return None

    cfg = {"enabled": True, "trading_mode": "DRY_RUN", "scan_scope": "alts"}
    service = BtcMaCrossService(
        config_getter=lambda: cfg, credentials_getter=lambda n: {},
        exchange_factory=lambda n, c: mainnet if n == "mainnet" else demo,
        runtime_dir=tmp_path, notifier=lambda t: None, main_network_getter=lambda: "testnet",
        sleep=no_sleep, environ={},
    )
    universe = asyncio.run(service.scan_universe("testnet", service.config()))
    assert [m for _, m, _ in universe] == ["ETHUSDT"]  # FXS: not on mainnet, SOL: not on demo, BTC: alts only
    cfg["rank_testnet_by_mainnet_volume"] = False
    service._universe_cache.clear()
    universe = asyncio.run(service.scan_universe("testnet", service.config()))
    assert [m for _, m, _ in universe][0] == "FXSUSDT"


def _exit_by_profit_lock(h):
    rows = h.fake.coins["BTCUSDT"]["rows"]
    h.now_ms = now_after(rows)
    h.run()
    h.fake.coins["BTCUSDT"]["mark"] = 85_000 * 1.021
    h.now_ms += 20_000
    h.run()
    h.fake.fire_lock("BTCUSDT", 86_530.0)
    h.now_ms += 20_000
    assert h.run()["trade"]["exit_reason"] == "PROFIT_LOCK"
    return rows


def _add_up_bar(rows, close):
    rows.append([rows[-1][0] + H1, rows[-1][4], close * 1.001, rows[-1][4] * 0.999, close, 1.0])


def test_trend_mode_reenters_the_same_direction_after_three_bars(tmp_path):
    h = Harness(tmp_path, _trend_coins(), scan_scope="btc", entry_mode="trend")  # default: 3 bars
    rows = _exit_by_profit_lock(h)
    orders = len(h.fake.orders)
    for i, close in enumerate((86_600.0, 86_700.0), start=1):
        _add_up_bar(rows, close)
        h.now_ms = now_after(rows)
        h.run()
        assert len(h.fake.orders) == orders, f"re-entered after {i} bar(s)"
        last = h.service.ledger("testnet").recent_events(1, kinds={"SIGNAL"})[0]
        assert last["skip_reason"].startswith("REENTRY_WAIT: BTCUSDT LONG")
    _add_up_bar(rows, 86_800.0)
    h.now_ms = now_after(rows)
    h.fake.coins["BTCUSDT"]["mark"] = 86_800.0
    result = h.run()
    assert result["action"] == "entered"
    assert h.state()["trade"]["side"] == "LONG" and h.state()["trade"]["entry_type"] == "TREND"


def test_legacy_block_without_exit_time_is_already_over(tmp_path):
    h = Harness(tmp_path, _trend_coins(), scan_scope="btc", entry_mode="trend")
    state = h.state()
    state["reentry_block"] = {"BTCUSDT": "LONG"}   # stored by the previous version
    h.service.save_state("testnet", "LIVE", state)
    h.now_ms = now_after(h.fake.coins["BTCUSDT"]["rows"])
    assert h.run()["action"] == "entered"


def test_status_shows_the_reentry_wait_and_the_button(tmp_path):
    from tests.test_btc_ma_cross import _callbacks, _controller

    controller = _controller(tmp_path)
    press = lambda a: asyncio.run(controller._handle_btc_ma_cross_action(a))  # noqa: E731
    _, markup = press("status")
    for data in ("bm:rw:1", "bm:rw:3", "bm:rw:6", "bm:rw:0"):
        assert data in _callbacks(markup)
    press("rw:6")
    assert controller.cfg.data["btc_ma_cross"]["reentry_wait_bars"] == 6
    service = controller.btc_ma_cross_service
    state = service.load_state("testnet", "DRY_RUN")
    state["reentry_block"] = {"BTCUSDT": {"side": "LONG", "exit_ms": service.now_ms()}}
    service.save_state("testnet", "DRY_RUN", state)
    status, _ = press("status")
    assert "⏳ BTCUSDT 롱 재진입 대기:" in status and "/6봉" in status


# ------------------------------------------------ post-exit coin rotation
def _trend_rows(last_close, base):
    """Cross up on the previous bar, still above on the last closed bar."""
    rows = bars(last_close * base / 85_000.0, base=base)
    rows.append([rows[-1][0] + H1, rows[-1][4], rows[-1][4] * 1.002, rows[-1][4] * 0.998, rows[-1][4] * 1.001, 1.0])
    return rows


def _down_rows(base):
    rows = bars(84_000.0 * base / 85_000.0, base=base)
    rows.append([rows[-1][0] + H1, rows[-1][4], rows[-1][4] * 1.002, rows[-1][4] * 0.998, rows[-1][4] * 0.999, 1.0])
    return rows


def _rotation_harness(tmp_path, **cfg):
    coins = {
        "BTCUSDT": {"volume": 1000, "rows": _trend_rows(86_000.0, 85_000.0), "mark": 85_000.0},
        "ETHUSDT": {"volume": 800, "rows": _trend_rows(86_000.0, 3_000.0), "mark": 3_000.0},
        "SOLUSDT": {"volume": 700, "rows": _down_rows(150.0), "mark": 150.0},
    }
    h = Harness(tmp_path, coins, **{"entry_mode": "trend", "rotate_after_exit": True, **cfg})
    h.now_ms = now_after(coins["BTCUSDT"]["rows"])
    return h


def _lock_exit(h, market, mark_up):
    h.fake.coins[market]["mark"] *= mark_up
    h.now_ms += 20_000
    h.run()
    h.fake.fire_lock(market, h.fake.coins[market]["mark"])
    h.now_ms += 20_000
    return h.run()


def test_after_an_exit_the_next_coin_is_entered_at_once(tmp_path):
    h = _rotation_harness(tmp_path)                 # alts scope: ETH (#1 alt), SOL
    assert h.run()["action"] == "entered"
    assert h.state()["trade"]["market_id"] == "ETHUSDT"
    assert _lock_exit(h, "ETHUSDT", 1.021)["trade"]["exit_reason"] == "PROFIT_LOCK"
    assert h.state()["rotation_pending"]["exclude"] == "ETHUSDT"
    h.now_ms += 10_000                               # same candle, no close waited for
    result = h.run()
    assert result["action"] == "entered", result
    trade = h.state()["trade"]
    assert trade["market_id"] == "SOLUSDT" and trade["side"] == "SHORT" and trade["entry_type"] == "ROTATE"
    assert h.state()["rotation_pending"] is None


def test_rotation_off_waits_for_the_next_candle(tmp_path):
    h = _rotation_harness(tmp_path, rotate_after_exit=False)
    h.run()
    _lock_exit(h, "ETHUSDT", 1.021)
    h.now_ms += 10_000
    assert h.run()["reason"] == "WAITING_NEXT_CLOSE"
    assert h.state()["trade"] is None


def test_btc_only_scope_rotates_to_the_top_alt(tmp_path):
    h = _rotation_harness(tmp_path, scan_scope="btc")
    assert h.run()["action"] == "entered"
    assert h.state()["trade"]["market_id"] == "BTCUSDT"
    _lock_exit(h, "BTCUSDT", 1.021)
    h.now_ms += 10_000
    assert h.run()["action"] == "entered"
    assert h.state()["trade"]["market_id"] == "ETHUSDT"   # #1 by volume is the coin just closed -> #2


def test_cross_reverse_does_not_trigger_rotation(tmp_path):
    h = _rotation_harness(tmp_path)
    h.run()
    state = h.state()
    from btc_ma_cross.service import BtcMaCrossService as S  # noqa: F401
    h.service._on_trade_closed("testnet", "LIVE", state, {"market_id": "ETHUSDT", "side": "LONG",
                                                            "exit_reason": "CROSS_REVERSE"})
    assert not state.get("rotation_pending")


def test_rotation_button(tmp_path):
    from tests.test_btc_ma_cross import _callbacks, _controller

    controller = _controller(tmp_path)
    press = lambda a: asyncio.run(controller._handle_btc_ma_cross_action(a))  # noqa: E731
    _, markup = press("status")
    assert "bm:rot:on" in _callbacks(markup)
    text, markup = press("rot:on")
    assert controller.cfg.data["btc_ma_cross"]["rotate_after_exit"] is True
    assert "bm:rot:off" in _callbacks(markup) and "방향 유지 중 진입 모드에서만" in text
    press("entry:trend")
    assert "익절·손절 직후" in press("status")[0]
