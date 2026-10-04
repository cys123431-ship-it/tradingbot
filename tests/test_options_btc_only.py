"""BTC-only options profile, bid-based profit exits and the /btcoptions menu."""
import asyncio
import time

import pytest

from bot_runtime.controller_options import ControllerOptionsMixin
from options_trading import OptionsTradingService
from options_trading import runtime as base_runtime
from options_trading.config import (
    BTC_MIN_QUOTE_VOLUME_USDT,
    default_options_config,
    multi_underlying_restore_values,
    normalize_options_config,
)
from tests.test_options_trading import _FakeMarket, _FakeOptionsClient

SYMBOL = "BTC-TEST-60000-C"


def test_btc_only_profile_overrides_universe_dte_spread_and_exit_basis():
    cfg = normalize_options_config({"btc_only": True, "btc_dte_preset": "monthly"})
    assert cfg["underlyings"] == ["BTCUSDT"]
    assert (cfg["min_dte_days"], cfg["target_dte_days"], cfg["max_dte_days"]) == (7.0, 14.0, 35.0)
    assert cfg["max_spread_pct"] == pytest.approx(0.06)
    assert cfg["min_quote_volume_usdt"] >= BTC_MIN_QUOTE_VOLUME_USDT
    assert cfg["profit_trigger_on_bid"] is True
    # Normalising again (as startup persistence does) is stable.
    assert normalize_options_config(cfg) == cfg


def test_btc_only_rejects_bad_values_and_default_is_unchanged():
    cfg = normalize_options_config(
        {"btc_only": "on", "btc_dte_preset": "daily", "btc_max_spread_pct": 0.5}
    )
    assert cfg["btc_dte_preset"] == "weekly"
    assert cfg["btc_max_spread_pct"] == pytest.approx(0.10)
    assert cfg["max_spread_pct"] == pytest.approx(0.10)

    legacy = normalize_options_config({})
    assert legacy["btc_only"] is False
    assert legacy["profit_trigger_on_bid"] is False
    assert legacy["underlyings"] == default_options_config()["underlyings"]
    assert legacy["max_spread_pct"] == pytest.approx(0.18)


@pytest.mark.parametrize(
    "enabled,mark,bid,expected",
    [
        (True, 21.0, 15.0, 15.0),
        (True, 12.0, 13.0, 12.0),   # never above the mark
        (True, 12.0, 0.0, 12.0),    # empty/zero bid falls back to mark
        (False, 21.0, 15.0, 21.0),
    ],
)
def test_profit_reference_price(enabled, mark, bid, expected):
    bids = [[str(bid), "1"]] if bid else []
    price = base_runtime._profit_reference_price(
        {"profit_trigger_on_bid": enabled}, mark, bids
    )
    assert price == pytest.approx(expected)


class _BookClient(_FakeOptionsClient):
    mark = 10.0
    bid = 10.0

    def positions(self, symbol=None):
        return [{"symbol": SYMBOL, "quantity": "1"}]

    def mark_price(self, symbol=None):
        return [{"symbol": SYMBOL, "markPrice": str(self.mark), "markIV": "0.5", "delta": "0.5"}]

    def depth(self, symbol, limit=20):
        return {"bids": [[str(self.bid), "5"]], "asks": [[str(self.mark + 1), "5"]]}


def _service(tmp_path, cfg, mark, bid):
    clients = []

    def factory(**kwargs):
        client = _BookClient(**kwargs)
        client.mark, client.bid = mark, bid
        clients.append(client)
        return client

    service = OptionsTradingService(
        config_getter=lambda: cfg,
        credentials_getter=lambda: {"api_key": "key", "secret_key": "secret"},
        market_data_exchange=_FakeMarket(),
        state_path=tmp_path / "options_state.json",
        client_factory=factory,
    )
    now = int(time.time() * 1000)
    service.state["cash_bankroll_usdt"] = 0.0
    service.state["active_position"] = {
        "symbol": SYMBOL,
        "side": "CALL",
        "quantity": 1.0,
        "original_quantity": 1.0,
        "entry_price": 10.0,
        "entry_total_usdt": 10.0,
        "entry_time_ms": now,
        "expiry_date_ms": now + 30 * 86_400_000,
        "unit": 1.0,
        "tick_size": 0.1,
        "peak_mark": 10.0,
    }
    service._save_state()
    return service, clients


def _sells(clients):
    return [order for client in clients for order in client.orders if order["side"] == "SELL"]


def _base_manage(service):
    return asyncio.run(base_runtime.OptionsTradingService._manage_active_position(service))


def test_mark_target_without_bid_does_not_fire_in_btc_mode(tmp_path):
    # Mark says +110% but the bid only pays +50%: no target, no sell.
    cfg = {"btc_only": True, "take_profit_pct": 1.0, "trail_activation_pct": 3.0}
    service, clients = _service(tmp_path, cfg, mark=21.0, bid=15.0)
    result = _base_manage(service)
    assert result["action"] == "managed"
    assert _sells(clients) == []
    position = service.state["active_position"]
    assert position["last_bid_pnl_pct"] == pytest.approx(0.5)
    assert position["peak_mark"] == pytest.approx(15.0)


def test_bid_target_fires_and_sells_at_the_bid(tmp_path):
    cfg = {"btc_only": True, "take_profit_pct": 1.0, "trail_activation_pct": 3.0}
    service, clients = _service(tmp_path, cfg, mark=21.0, bid=20.5)
    _base_manage(service)
    sells = _sells(clients)
    assert len(sells) == 1
    assert float(sells[0]["price"]) == pytest.approx(20.4)
    assert sells[0]["reduce_only"] is True


def test_legacy_mode_still_uses_mark_for_target(tmp_path):
    cfg = {"take_profit_pct": 1.0, "trail_activation_pct": 3.0}
    service, clients = _service(tmp_path, cfg, mark=21.0, bid=15.0)
    _base_manage(service)
    assert len(_sells(clients)) == 1


def test_stop_loss_still_uses_mark_in_btc_mode(tmp_path):
    # A wide book (bid far below mark) must not trigger a stop by itself.
    cfg = {"btc_only": True, "stop_loss_pct": 0.55}
    service, clients = _service(tmp_path, cfg, mark=9.0, bid=4.0)
    result = _base_manage(service)
    assert result["action"] == "managed"
    assert _sells(clients) == []


def test_adaptive_runtime_uses_the_bid_too(tmp_path, monkeypatch):
    async def no_signal(self, position, cfg, now_ms):
        return None

    monkeypatch.setattr(OptionsTradingService, "_refresh_exit_signal", no_signal)
    cfg = {"btc_only": True, "take_profit_pct": 1.0}
    service, clients = _service(tmp_path, cfg, mark=21.0, bid=15.0)
    result = asyncio.run(service._manage_active_position())
    assert result["action"] == "managed"
    assert _sells(clients) == []

    service2, clients2 = _service(tmp_path / "b", cfg, mark=21.0, bid=20.5)
    asyncio.run(service2._manage_active_position())
    assert len(_sells(clients2)) == 1


# ----- Telegram menu -----


class _Cfg:
    def __init__(self, options):
        self.data = {"options_trading": dict(options)}

    def get(self, key, default=None):
        return self.data.get(key, default)

    async def update_value(self, path, value):
        node = self.data
        for key in path[:-1]:
            node = node.setdefault(key, {})
        node[path[-1]] = value


class _FakeService:
    def __init__(self, cfg):
        self.cfg = cfg
        self.state = {"active_position": None}
        self.cycles = []
        self.preflight_result = {"ok": True, "can_trade": True}

    def config(self):
        return normalize_options_config(self.cfg.get("options_trading"))

    async def preflight(self):
        return self.preflight_result

    async def run_cycle(self, *, force_scan=False, force_exit=False):
        self.cycles.append((force_scan, force_exit))
        return {"action": "waiting", "reason": "테스트 판단"}

    async def status_snapshot(self, refresh=True):
        cfg = self.config()
        return {"enabled": cfg["enabled"], "api_ok": True, "can_trade": True}


def _controller(options=None):
    controller = ControllerOptionsMixin()
    controller.cfg = _Cfg(options or {})
    controller.options_trading_service = _FakeService(controller.cfg)
    controller.market_data_exchange = None
    return controller


def _callbacks(markup):
    return [button.callback_data for row in markup.inline_keyboard for button in row]


def _press(controller, action):
    return asyncio.run(controller._handle_btc_options_action(action))


def test_btc_menu_on_requires_confirmation_then_enables_btc_only():
    controller = _controller()
    text, markup = _press(controller, "on")
    assert "bo:confirm_on" in _callbacks(markup)
    assert controller.cfg.data["options_trading"].get("enabled") is not True

    text, _ = _press(controller, "confirm_on")
    stored = controller.cfg.data["options_trading"]
    assert stored["enabled"] is True and stored["btc_only"] is True
    assert controller.options_trading_service.cycles == [(True, False)]
    assert "BTC 옵션 자동매매 ON" in text
    assert "BTC 전용 모드: ON" in text
    assert "BTCUSDT 전용" in text


def test_btc_menu_preflight_failure_does_not_enable():
    controller = _controller()
    controller.options_trading_service.preflight_result = {"ok": False, "error": "권한 없음"}
    text, markup = _press(controller, "on")
    assert "사전점검 실패" in text
    assert "bo:confirm_on" not in _callbacks(markup)


def test_btc_menu_dte_and_spread_buttons_update_config():
    controller = _controller({"btc_only": True})
    text, markup = _press(controller, "dte:monthly")
    assert controller.cfg.data["options_trading"]["btc_dte_preset"] == "monthly"
    assert "월간" in text
    assert "bo:dte:weekly" in _callbacks(markup)

    _press(controller, "spr:0.1")
    assert controller.cfg.data["options_trading"]["btc_max_spread_pct"] == pytest.approx(0.10)
    cfg = controller.options_trading_service.config()
    assert cfg["max_spread_pct"] == pytest.approx(0.10)
    labels = [b.text for row in controller._build_btc_options_keyboard(cfg).inline_keyboard for b in row]
    assert "✅ 스프레드 10%" in labels


def test_btc_menu_multi_restore_turns_entries_off():
    controller = _controller({"btc_only": True, "enabled": True})
    _, markup = _press(controller, "multi")
    assert "bo:confirm_multi" in _callbacks(markup)
    _press(controller, "confirm_multi")
    stored = controller.cfg.data["options_trading"]
    assert stored["enabled"] is False and stored["btc_only"] is False
    for key, value in multi_underlying_restore_values().items():
        assert stored[key] == value


def test_btc_menu_close_requires_confirmation():
    controller = _controller({"btc_only": True})
    _, markup = _press(controller, "close")
    assert "bo:confirm_close" in _callbacks(markup)
    assert controller.options_trading_service.cycles == []
    _press(controller, "confirm_close")
    assert controller.options_trading_service.cycles == [(False, True)]


def test_btc_menu_warns_about_a_legacy_alt_position():
    controller = _controller({"btc_only": True})
    controller.options_trading_service.state["active_position"] = {"symbol": "SOL-251010-150-C"}
    text, _ = _press(controller, "status")
    assert "SOL-251010-150-C 포지션은 청산될 때까지" in text


def test_main_keyboard_has_btcoptions_button():
    import emas

    controller = emas.MainController.__new__(emas.MainController)
    labels = [b.text for row in controller._build_main_keyboard().keyboard for b in row]
    assert "/btcoptions" in labels


# ----- stale candidate / scan history after switching universe -----


def test_switching_to_btc_only_clears_old_sol_candidate_and_scan_stats(tmp_path):
    cfg = {"underlyings": ["SOLUSDT"]}
    service, _ = _service(tmp_path, cfg, mark=10.0, bid=10.0)
    service.state["active_position"] = None
    service._sync_scan_universe()
    service.state["last_candidate"] = {"symbol": "SOL-260828-88-C", "underlying": "SOLUSDT"}
    service.state["recent_scan_outcomes"] = ["ORDERABLE_CANDIDATE"] * 3
    service._save_state()

    assert service._sync_scan_universe() is False  # same universe keeps history

    cfg["btc_only"] = True
    status = asyncio.run(service.status_snapshot(refresh=False))
    assert status["last_candidate"] is None
    assert status["scan_outcomes_window"] == 0
    assert service.state["scan_universe"] == ["BTCUSDT"]


def test_status_hides_expired_candidate_and_shows_found_time():
    from bot_runtime.controller_options import (
        _candidate_expired,
        _candidate_found_text,
    )

    old = {"symbol": "SOL-260828-88-C"}
    assert _candidate_expired(old) is True
    fresh = {
        "symbol": "BTC-991231-60000-C",
        "expiry_date_ms": 4_102_444_800_000,
        "found_at_ms": 1_791_000_000_000,
    }
    assert _candidate_expired(fresh) is False
    assert _candidate_found_text(fresh).endswith("KST 발견")
    assert _candidate_found_text({}) == "발견 시각 미기록"


def test_format_status_drops_expired_candidate():
    controller = _controller({"btc_only": True})

    async def snapshot(refresh=True):
        return {
            "enabled": True,
            "last_candidate": {"symbol": "SOL-260828-88-C", "signal_score": 0.81},
        }

    controller.options_trading_service.status_snapshot = snapshot
    text = asyncio.run(controller._format_options_status(refresh=True))
    assert "SOL-260828-88-C" not in text
    assert "최근 후보" not in text


# ----- responsiveness / clock-skew fixes -----


def test_setting_buttons_do_not_touch_the_exchange():
    controller = _controller({"btc_only": True, "btc_max_spread_pct": 0.1})

    async def boom(refresh=True):
        raise AssertionError("setting buttons must not call the API")

    controller.options_trading_service.status_snapshot = boom
    text, markup = _press(controller, "spr:0.06")
    assert controller.cfg.data["options_trading"]["btc_max_spread_pct"] == pytest.approx(0.06)
    assert "최대 스프레드: 6%" in text and "저장됨" in text
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert "✅ 스프레드 6%" in labels
    text, _ = _press(controller, "dte:standard")
    assert "표준" in text


def test_stale_callback_answer_does_not_abort_the_press():
    from telegram.error import BadRequest

    from bot_runtime.controller_options import _answer_quietly

    class Query:
        async def answer(self):
            raise BadRequest("Query is too old and response timeout expired")

    asyncio.run(_answer_quietly(Query()))  # no exception


def test_timestamp_error_resyncs_and_retries_signed_requests_once():
    from options_trading.client import BinanceOptionsApiError, BinanceOptionsClient

    client = BinanceOptionsClient(api_key="k", secret_key="s")
    calls = []

    def once(method, path, params=None, *, signed=False):
        calls.append(path)
        if path == "/eapi/v1/time":
            return {"serverTime": int(time.time() * 1000)}
        if calls.count(path) == 1:
            raise BinanceOptionsApiError("Timestamp outside recvWindow", code=-1021, status=400)
        return {"ok": True}

    client._request_once = once
    assert client._request("GET", "/eapi/v1/position", signed=True) == {"ok": True}
    assert calls == ["/eapi/v1/position", "/eapi/v1/time", "/eapi/v1/position"]

    calls.clear()

    def other_error(method, path, params=None, *, signed=False):
        calls.append(path)
        raise BinanceOptionsApiError("bad", code=-2010, status=400)

    client._request_once = other_error
    with pytest.raises(BinanceOptionsApiError):
        client._request("GET", "/eapi/v1/position", signed=True)
    assert calls == ["/eapi/v1/position"]


def test_sync_time_keeps_timestamps_behind_the_server():
    from options_trading.client import BinanceOptionsClient

    client = BinanceOptionsClient()
    server_now = int(time.time() * 1000) + 3_000  # local clock 3s slow
    client._request_once = lambda *a, **k: {"serverTime": server_now}
    client.sync_time()
    assert 2_000 < client._server_offset_ms < 3_000


def test_direction_wait_reason_shows_scores():
    from options_trading.adaptive_runtime import _direction_wait_text

    text = _direction_wait_text(
        [{"underlying": "BTCUSDT", "trend_score": 0.052, "squeeze_score": -0.524}],
        normalize_options_config({"btc_only": True}),
    )
    assert text == "방향 신호 대기 — BTC 추세 +0.05 (기준 ±0.46) · 압축돌파 0.52 (기준 0.58)"
