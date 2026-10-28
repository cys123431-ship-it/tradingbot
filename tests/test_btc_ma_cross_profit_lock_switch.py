"""3/200 SMA: 익절(PROFIT_LOCK) 3회 → 다음 진입부터 '돌파 시 진입' 강제 전환."""
import asyncio

from btc_ma_cross.config import PROFIT_LOCK_SWITCH_CHOICES, normalize_btc_ma_cross_config
from tests.test_btc_ma_cross import Harness, _callbacks, _controller


def _close(service, state, reason="PROFIT_LOCK", **extra):
    service._on_trade_closed("testnet", "LIVE", state,
                             {"market_id": "BTCUSDT", "side": "LONG", "exit_reason": reason,
                              "trade_id": "t", **extra})


def _press(controller, action):
    return asyncio.run(controller._handle_btc_ma_cross_action(action))


class Writer:
    """Stand-in for the Telegram controller's async config writer."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.writes = []

    async def set_entry_mode(self, entry_mode):
        self.writes.append(entry_mode)
        self.cfg["entry_mode"] = entry_mode
        return True


# ------------------------------------------------------------------ config
def test_config_default_and_choices():
    assert normalize_btc_ma_cross_config({})["profit_lock_switch_count"] == 3
    assert normalize_btc_ma_cross_config({"profit_lock_switch_count": 0})["profit_lock_switch_count"] == 0
    assert normalize_btc_ma_cross_config({"profit_lock_switch_count": "5"})["profit_lock_switch_count"] == 5
    assert normalize_btc_ma_cross_config({"profit_lock_switch_count": 99})["profit_lock_switch_count"] == 3
    assert normalize_btc_ma_cross_config({"profit_lock_switch_count": None})["profit_lock_switch_count"] == 3
    assert PROFIT_LOCK_SWITCH_CHOICES == (0, 1, 2, 3, 4, 5)


# ------------------------------------------------------------- 카운트/전환
def test_counts_only_profit_lock_exits(tmp_path):
    h = Harness(tmp_path, entry_mode="trend")
    state = h.state()
    _close(h.service, state, "PROFIT_LOCK")
    assert state["profit_lock_exit_count"] == 1
    for reason in ("CROSS_REVERSE", "EMERGENCY_STOP", "MANUAL", "FAILSAFE"):
        _close(h.service, state, reason)
    assert state["profit_lock_exit_count"] == 1
    assert state.get("pending_entry_mode_switch") is None
    assert not state.get("entry_mode_forced_cross")


def test_third_profit_lock_queues_the_forced_cross_switch(tmp_path):
    h = Harness(tmp_path, entry_mode="trend")
    state = h.state()
    _close(h.service, state, "PROFIT_LOCK")
    _close(h.service, state, "PROFIT_LOCK")
    assert state.get("pending_entry_mode_switch") is None
    _close(h.service, state, "PROFIT_LOCK")
    switch = state["pending_entry_mode_switch"]
    assert switch["entry_mode"] == "cross"
    assert switch["from_mode"] == "trend"
    assert switch["exit_count"] == 3
    assert state["entry_mode_forced_cross"] is True
    assert state["profit_lock_exit_count"] == 0          # 다시 0부터 센다


def test_switch_off_never_forces(tmp_path):
    h = Harness(tmp_path, entry_mode="trend", profit_lock_switch_count=0)
    state = h.state()
    for _ in range(6):
        _close(h.service, state, "PROFIT_LOCK")
    assert state["profit_lock_exit_count"] == 6
    assert state.get("pending_entry_mode_switch") is None
    assert not state.get("entry_mode_forced_cross")


# ------------------------------------------------------ 실제 진입 사이클 적용
def test_forced_switch_is_applied_before_the_next_entry(tmp_path):
    h = Harness(tmp_path, entry_mode="trend", last_close=86_000.0)
    writer = Writer(h.cfg)
    h.service.config_writer = writer
    state = h.state()
    for _ in range(3):
        _close(h.service, state, "PROFIT_LOCK")
    h.service.save_state("testnet", "LIVE", state)
    h.next_bar(83_000.0)                 # 반대 크로스 봉 → 이번 사이클에서 진입
    result = h.run()
    assert writer.writes == ["cross"]
    assert h.cfg["entry_mode"] == "cross"
    assert result["action"] == "entered" and h.state()["trade"]["side"] == "SHORT"
    saved = h.state()
    assert saved["pending_entry_mode_switch"] is None
    assert saved["last_entry_mode_switch"]["applied"] is True
    assert saved["entry_mode_forced_cross"] is True


def test_forced_cross_does_not_reenter_without_a_new_cross(tmp_path):
    from btc_ma_cross.service import BtcMaCrossService
    from tests.btc_pullback_fixtures import FakeBinance
    from tests.test_btc_ma_cross import now_after
    from tests.test_btc_ma_cross_scope import _trend_rows

    # 크로스는 이미 두 봉 전에 끝났고(SMA3 > SMA200 유지) 마지막 봉엔 새 돌파가 없다.
    rows = _trend_rows(86_000.0, 85_000.0)
    fake = FakeBinance(mark=85_000.0, wallet=5_000.0)
    fake.fetch_ohlcv = lambda symbol, timeframe, since=None, limit=None: (
        fake.m1 if timeframe == "1m" else rows
    )
    cfg = {"enabled": True, "trading_mode": "LIVE", "entry_mode": "trend"}
    now_ms = now_after(rows)

    async def no_sleep(_):
        return None

    service = BtcMaCrossService(
        config_getter=lambda: cfg,
        credentials_getter=lambda n: {"api_key": "k", "secret_key": "s"},
        exchange_factory=lambda n, c: fake,
        runtime_dir=tmp_path, notifier=lambda t: None,
        main_network_getter=lambda: "testnet",
        clock=lambda: now_ms / 1000, sleep=no_sleep, environ={},
        config_writer=Writer(cfg),
    )
    state = service.load_state("testnet", "LIVE")
    for _ in range(3):
        _close(service, state, "PROFIT_LOCK")
    service.save_state("testnet", "LIVE", state)
    result = asyncio.run(service.run_cycle())
    assert cfg["entry_mode"] == "cross"
    # 돌파가 없으므로 진입하지 않는다(trend 모드였다면 방향 유지 재진입했을 상황).
    assert result["action"] == "skip" and result["reason"] == "NO_SIGNAL"
    assert service.load_state("testnet", "LIVE")["trade"] is None
    assert fake.orders == []


# ------------------------------------------------------------- Telegram 버튼
def test_manual_trend_choice_releases_the_forced_mode(tmp_path):
    controller = _controller(tmp_path)
    service = controller.btc_ma_cross_service
    state = service.load_state("testnet", "DRY_RUN")
    for _ in range(3):
        _close(service, state, "PROFIT_LOCK")
    service.save_state("testnet", "DRY_RUN", state)
    assert state["entry_mode_forced_cross"] is True
    _press(controller, "entry:trend")
    saved = service.load_state("testnet", "DRY_RUN")
    assert saved["entry_mode_forced_cross"] is False
    assert saved["profit_lock_exit_count"] == 0
    assert saved["pending_entry_mode_switch"] is None
    assert controller.cfg.data["btc_ma_cross"]["entry_mode"] == "trend"


def _labeled(markup):
    """(button text, callback data) pairs of an inline keyboard."""
    return [(button.text, button.callback_data) for row in markup.inline_keyboard for button in row]


def test_telegram_switch_button_toggles_and_shows_the_count(tmp_path):
    controller = _controller(tmp_path)
    text, markup = _press(controller, "status")
    buttons = _labeled(markup)
    assert any("bm:pls:off" == data and label.startswith("🔁") and "ON (0/3)" in label
               for label, data in buttons)                      # 기본 ON → 끌 수 있다
    assert "익절 자동 전환" in text
    text, markup = _press(controller, "pls:off")
    assert controller.cfg.data["btc_ma_cross"]["profit_lock_switch_count"] == 0
    assert any(data == "bm:pls:on" and "OFF" in label for label, data in _labeled(markup))
    text, markup = _press(controller, "pls:on")
    assert controller.cfg.data["btc_ma_cross"]["profit_lock_switch_count"] == 3
    assert any(data == "bm:pls:off" and "ON (0/3)" in label for label, data in _labeled(markup))
    assert "익절 자동 전환: 수익 확보 익절 3회마다" in _press(controller, "rules")[0]


def test_status_line_shows_the_switch_progress(tmp_path):
    controller = _controller(tmp_path)
    service = controller.btc_ma_cross_service
    state = service.load_state("testnet", "DRY_RUN")
    _close(service, state, "PROFIT_LOCK")
    service.save_state("testnet", "DRY_RUN", state)
    status = asyncio.run(controller._format_btc_ma_cross_status())
    assert "익절 자동 전환: 수익 확보 익절 3회마다" in status
    assert "(현재 1/3)" in status
