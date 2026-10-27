"""SMA3/200 strategy: one designated coin and the 3m / 5m candles."""
import asyncio

import pytest

from btc_ma_cross.config import normalize_btc_ma_cross_config
from btc_ma_cross.signals import evaluate_cross, market_is_flat
from tests.test_btc_ma_cross import _callbacks, _controller, bars, now_after
from tests.test_btc_ma_cross_scope import Harness, coin

M3, M5 = 180_000, 300_000


def _coins():
    return {
        "BTCUSDT": coin(900, 86_000),     # LONG cross, highest volume
        "ETHUSDT": coin(800, 86_000),     # LONG cross
        "SOLUSDT": coin(700, 84_000),     # SHORT cross
        "1000PEPEUSDT": coin(600, 85_000),
        "USDCUSDT": coin(500, 85_000),
        "AAPLUSDT": coin(400, 85_000, contract_type="TRADIFI_PERPETUAL"),
    }


# ------------------------------------------------------------- single coin
def test_single_scope_trades_only_the_chosen_coin(tmp_path):
    h = Harness(tmp_path, _coins(), scan_scope="single", single_symbol="SOLUSDT")
    universe = asyncio.run(h.service.scan_universe("testnet", h.service.config()))
    assert universe == [("SOL/USDT:USDT", "SOLUSDT", None)]
    assert h.run()["action"] == "entered"
    trade = h.state()["trade"]
    assert trade["market_id"] == "SOLUSDT" and trade["side"] == "SHORT"
    assert list(h.fake.positions) == ["SOL/USDT:USDT"]


def test_single_scope_waits_when_the_chosen_coin_has_no_signal(tmp_path):
    h = Harness(tmp_path, _coins(), scan_scope="single", single_symbol="1000PEPEUSDT")
    assert h.run()["reason"] == "NO_SIGNAL"
    assert h.state()["trade"] is None and not h.fake.positions


@pytest.mark.parametrize("text,expected", [
    ("sol", "SOLUSDT"), ("SOLUSDT", "SOLUSDT"), ("SOL/USDT", "SOLUSDT"), ("SOL/USDT:USDT", "SOLUSDT"),
    ("btc", "BTCUSDT"), ("pepe", "1000PEPEUSDT"),
])
def test_coin_names_resolve(tmp_path, text, expected):
    h = Harness(tmp_path, _coins())
    assert asyncio.run(h.service.resolve_coin("testnet", text))[1] == expected


@pytest.mark.parametrize("text,reason", [
    ("DOGE", "SYMBOL_NOT_FOUND"), ("USDC", "STABLECOIN"), ("AAPL", "TRADFI"), ("", "SYMBOL_EMPTY"),
])
def test_bad_coins_are_rejected(tmp_path, text, reason):
    h = Harness(tmp_path, _coins())
    with pytest.raises(ValueError, match=reason):
        asyncio.run(h.service.resolve_coin("testnet", text))


def test_delisted_coin_is_rejected(tmp_path):
    h = Harness(tmp_path, _coins())
    h.fake.markets["SOL/USDT:USDT"]["info"]["status"] = "SETTLING"
    with pytest.raises(ValueError, match="NOT_TRADING"):
        asyncio.run(h.service.resolve_coin("testnet", "SOL"))


def test_single_scope_never_rotates_to_another_coin(tmp_path):
    h = Harness(tmp_path, _coins(), scan_scope="single", single_symbol="SOLUSDT",
                entry_mode="trend", rotate_after_exit=True)
    state = h.state()
    h.service._on_trade_closed("testnet", "LIVE", state, {"market_id": "SOLUSDT", "side": "SHORT",
                                                            "exit_reason": "PROFIT_LOCK"})
    assert not state.get("rotation_pending")


def test_config_keeps_a_clean_market_id():
    assert normalize_btc_ma_cross_config({"single_symbol": "sol/usdt"})["single_symbol"] == "SOLUSDT"
    assert normalize_btc_ma_cross_config({"single_symbol": "SOL"})["single_symbol"] == "BTCUSDT"
    assert normalize_btc_ma_cross_config({"scan_scope": "single"})["scan_scope"] == "single"


def _single_controller(tmp_path):
    controller = _controller(tmp_path)
    h = Harness(tmp_path, _coins(), live=False)
    h.cfg = controller.cfg.data["btc_ma_cross"]
    controller.btc_ma_cross_service = h.build()
    return controller


def test_coin_pick_buttons(tmp_path):
    controller = _single_controller(tmp_path)
    press = lambda a: asyncio.run(controller._handle_btc_ma_cross_action(a))  # noqa: E731
    assert "bm:pick" in _callbacks(press("status")[1])
    text, markup = press("pick")
    picks = _callbacks(markup)
    assert "/macoin" in text
    assert picks[0] == "bm:coin:BTCUSDT" and "bm:coin:SOLUSDT" in picks
    assert "bm:coin:USDCUSDT" not in picks and "bm:coin:AAPLUSDT" not in picks
    text, markup = press("coin:SOLUSDT")
    cfg = controller.cfg.data["btc_ma_cross"]
    assert cfg["scan_scope"] == "single" and cfg["single_symbol"] == "SOLUSDT"
    assert "종목: 지정 코인 SOLUSDT만" in text
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert "✅ 🎯 SOLUSDT" in labels and "🪙 BTC만" in labels
    text, _ = press("coin:AAPLUSDT")
    assert "TradFi" in text and cfg["single_symbol"] == "SOLUSDT"
    press("scope:alts")
    assert cfg["scan_scope"] == "alts"


def test_macoin_command(tmp_path):
    controller = _single_controller(tmp_path)
    replies = []

    class Message:
        async def reply_text(self, text, reply_markup=None):
            replies.append((text, reply_markup))

    class Update:
        message = Message()

    class Context:
        args = ["pepe"]

    handlers = []

    class App:
        def add_handler(self, handler):
            handlers.append(handler)

    controller.tg_app = App()
    controller._register_btc_ma_cross_handlers(lambda fn: fn)
    macoin = next(h for h in handlers if getattr(h, "commands", None) == frozenset({"macoin"}))
    asyncio.run(macoin.callback(Update(), Context()))
    assert controller.cfg.data["btc_ma_cross"]["single_symbol"] == "1000PEPEUSDT"
    assert "1000PEPEUSDT만 거래" in replies[-1][0]
    Context.args = []
    asyncio.run(macoin.callback(Update(), Context()))
    assert "bm:coin:BTCUSDT" in _callbacks(replies[-1][1])


# --------------------------------------------------------------- 3m / 5m
@pytest.mark.parametrize("tf,span", [("3m", M3), ("5m", M5)])
def test_short_timeframes_cross_and_go_stale_within_half_a_candle(tf, span):
    cfg = normalize_btc_ma_cross_config({"timeframe": tf})
    assert cfg["timeframe"] == tf
    rows = bars(85_200.0, span=span)
    assert evaluate_cross(rows, cfg, now_after(rows, span=span))["side"] == "LONG"
    late = evaluate_cross(rows, cfg, now_after(rows, span=span, seconds=span / 2000 + 5))
    assert late["side"] is None and late["skip_reason"] == "SIGNAL_STALE"


def test_quiet_short_candles_are_not_flat_markets():
    cfg = normalize_btc_ma_cross_config({"timeframe": "3m"})
    # BTC 3m candles with a 0.03% range are normal, not a frozen market.
    rows = [[i * M3, 85_000.0, 85_013.0, 84_987.0, 85_000.0, 1.0] for i in range(205)]
    assert market_is_flat(rows, cfg) is False
    assert market_is_flat(rows, normalize_btc_ma_cross_config({"timeframe": "1h"})) is True


def test_timeframe_buttons_include_3m_and_5m(tmp_path):
    controller = _controller(tmp_path)
    press = lambda a: asyncio.run(controller._handle_btc_ma_cross_action(a))  # noqa: E731
    callbacks = _callbacks(press("status")[1])
    for tf in ("3m", "5m", "15m", "30m", "1h", "2h", "4h"):
        assert f"bm:tf:{tf}" in callbacks
    text, _ = press("tf:3m")
    assert controller.cfg.data["btc_ma_cross"]["timeframe"] == "3m" and "봉: 3분" in text
    assert "3분~4시간" in press("rules")[0]
