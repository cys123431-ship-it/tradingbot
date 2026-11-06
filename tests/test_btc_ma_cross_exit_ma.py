"""Custom exit MA (추가 청산 조건): SMA(fast) vs SMA(exit_ma_period).

Contract under test:
  * OFF (default): everything behaves exactly like the plain 3/200 cross
    (stop-and-reverse, trend entries, rotation).
  * ON: the exit line is an *extra* exit trigger -- a cross of SMA(fast) against
    SMA(exit) closes the position even when the SMA(fast)/SMA(slow) cross has
    not happened.  The opposite cross still closes it too.  Either way there is
    no reverse entry: the coin stays flat until the original SMA(fast)/SMA(slow)
    cross closes a fresh candle (long: close above, short: close below).
  * The two other exits (profit lock, emergency stop) are untouched.
"""
import asyncio

import pytest

from btc_ma_cross.config import (
    EXIT_MA_DEFAULT_PERIOD,
    EXIT_MA_ENABLED_DEFAULT,
    normalize_btc_ma_cross_config,
)
from btc_ma_cross.signals import evaluate_cross, sma
from tests.btc_pullback_fixtures import H1
from tests.test_btc_ma_cross import Harness as BtcHarness, _controller, _callbacks, bars, now_after
from tests.test_btc_ma_cross_scope import Harness as ScopeHarness, coin

ON = {"exit_ma_enabled": True, "exit_ma_period": 25}

# Walk up (SMA3 far above SMA25) then roll over.  On the 4th close SMA3 cuts
# SMA25 while SMA200 is still below, so the plain 3/200 cross does NOT fire on
# that candle -- only the optional exit line does.  The rest of the path keeps
# rolling until SMA3 reaches SMA200 (the ordinary opposite cross), where
# close-only mode must still not open a SHORT.
RISE_TO_EXIT_CROSS = (85_200.0, 85_400.0, 85_310.0, 85_010.0, 84_710.0, 84_410.0, 84_110.0, 83_810.0)
EXIT_LINE_CROSS_AT = 5  # how many of RISE_TO_EXIT_CROSS fire the exit line (no 3/200 cross)
# Same shape, then closes recover through SMA200 -> a fresh up cross (new LONG).
RECOVER_ABOVE_SLOW = (84_900.0, 85_400.0, 86_200.0, 87_000.0)
RECOVERY_CROSSES_AT = 3  # the 3rd of RECOVER_ABOVE_SLOW is a fresh 3/200 up cross
# Opposite cross: SMA3 straight through SMA200 (no reverse entry allowed when ON).
OPPOSITE_CROSS_DOWN = (85_000.0, 85_200.0, 85_400.0, 84_500.0, 84_000.0, 83_500.0, 83_000.0)


# =============================================================== config
def test_exit_ma_is_off_by_default_and_validated():
    cfg = normalize_btc_ma_cross_config({})
    assert cfg["exit_ma_enabled"] is EXIT_MA_ENABLED_DEFAULT is False
    assert cfg["exit_ma_period"] == EXIT_MA_DEFAULT_PERIOD == 25

    on = normalize_btc_ma_cross_config({"exit_ma_enabled": True, "exit_ma_period": 69})
    assert on["exit_ma_enabled"] is True and on["exit_ma_period"] == 69

    # Corrupted values fall back instead of raising (the bot must still start).
    assert normalize_btc_ma_cross_config({"exit_ma_enabled": True, "exit_ma_period": 1})["exit_ma_period"] == 25
    assert normalize_btc_ma_cross_config({"exit_ma_enabled": True, "exit_ma_period": "x"})["exit_ma_period"] == 25
    assert normalize_btc_ma_cross_config({"exit_ma_enabled": "off"})["exit_ma_enabled"] is False

    # The exit line only stays on when the fetched candles can actually cover it.
    small = normalize_btc_ma_cross_config({"exit_ma_enabled": True, "exit_ma_period": 400,
                                           "fast_period": 1, "slow_period": 3,
                                           "candle_history": 210})
    assert small["exit_ma_enabled"] is False and small["exit_ma_period"] == 400
    big = normalize_btc_ma_cross_config({"exit_ma_enabled": True, "exit_ma_period": 400,
                                         "fast_period": 1, "slow_period": 3,
                                         "candle_history": 600})
    assert big["exit_ma_enabled"] is True and big["exit_ma_period"] == 400


# ============================================================== signals
def _series(closes, span=H1, start=1_790_000_000_000 - (1_790_000_000_000 % H1)):
    rows = []
    for i, close in enumerate(closes):
        open_ = closes[i - 1] if i else close
        high = max(open_, close) * 1.002
        low = min(open_, close) * 0.998
        rows.append([start + i * span, open_, high, low, close, 10.0])
    return rows


def test_exit_fields_absent_when_off():
    rows = bars(86_000.0)
    signal = evaluate_cross(rows, normalize_btc_ma_cross_config({}), now_after(rows))
    assert signal["exit_ma_enabled"] is False
    assert signal["exit_side"] is None and signal["exit_trend_side"] is None
    assert signal["exit_ma_period"] is None


def _long_series(start=100.0, step=1.0, n=200, tail=()):
    """A long flat-ish ramp (so SMA200 exists) followed by explicit closes."""
    return [start + step * i for i in range(n)] + list(tail)


def test_exit_line_cross_down_is_detected_while_the_slow_cross_is_not():
    # A long ramp, then a spike and a roll over: SMA3 cuts SMA25 down while SMA200
    # (far lower) is never crossed, so only the exit line fires.
    closes = _long_series(100.0, 0.2, 210, [142.0, 141.0, 130.0])
    rows = _series(closes)
    now = rows[-1][0] + H1 + 5_000
    off = evaluate_cross(rows, normalize_btc_ma_cross_config({}), now)
    on = evaluate_cross(rows, normalize_btc_ma_cross_config(ON), now)
    fast, exit_line = sma(closes, 3), sma(closes, 25)
    assert fast[-2] > exit_line[-2] and fast[-1] < exit_line[-1]  # fixture sanity
    assert on["exit_ma_enabled"] is True and on["exit_ma_period"] == 25
    assert on["exit_side"] == "SHORT" and on["exit_trend_side"] == "SHORT"
    # The exit line never creates an entry signal by itself.
    assert on["side"] is None and off["side"] is None


def test_exit_trend_side_reports_where_the_fast_line_sits():
    rising = _series(_long_series(100.0, 0.2, 210, [141.0, 141.5]))
    signal = evaluate_cross(rising, normalize_btc_ma_cross_config(ON), rising[-1][0] + H1 + 5_000)
    assert signal["exit_side"] is None  # no fresh cross on this bar
    assert signal["exit_trend_side"] == "LONG"


def test_exit_period_is_disabled_when_history_is_too_short():
    closes = [100.0 + i for i in range(30)]
    rows = _series(closes)
    cfg = normalize_btc_ma_cross_config({"exit_ma_enabled": True, "exit_ma_period": 500,
                                         "fast_period": 1, "slow_period": 3, "candle_history": 210})
    signal = evaluate_cross(rows, cfg, rows[-1][0] + H1 + 5_000)
    assert signal["exit_side"] is None and signal["exit_ma_period"] is None


# ===================================================== service: OFF path
def test_off_keeps_stop_and_reverse(tmp_path):
    h = BtcHarness(tmp_path)
    h.run()
    h.next_bar(83_000.0)
    result = h.run()
    assert result["action"] == "reversed"
    closed = h.service.ledger("testnet").trades(mode="LIVE", status="CLOSED")
    assert closed[-1]["exit_reason"] == "CROSS_REVERSE"
    assert h.state()["trade"]["side"] == "SHORT"


def test_off_never_exits_on_the_exit_line(tmp_path):
    # Config has a period stored but the feature is off: the line must be ignored.
    h = BtcHarness(tmp_path, exit_ma_enabled=False, exit_ma_period=25)
    h.run()
    trade = h.state()["trade"]
    assert trade["exit_ma_enabled"] is False and trade["exit_ma_period"] is None
    h.fake.mark = h.fake.mark * 0.99
    h.now_ms += 20_000
    assert h.run()["action"] == "managed"
    assert h.state()["trade"] is not None


# ==================================================== service: ON path
def _append(h, closes):
    """Append closed 1h bars (as the harness would receive them)."""
    rows = [list(row) for row in h.rows]
    prev = rows[-1][4]
    for close in closes:
        close = float(close)
        rows.append([rows[-1][0] + H1, prev, max(prev, close) * 1.001, min(prev, close) * 0.999,
                     close, 10.0])
        prev = close
    h.rows = rows
    h.fake.bars["1h"] = rows
    h.now_ms = now_after(rows)


def _exit_cross_leg(h, closes=RISE_TO_EXIT_CROSS):
    """Append closes up to (not including) the candle where SMA3 cuts SMA25 down."""
    _append(h, closes[:EXIT_LINE_CROSS_AT])
    return h.rows[-1][4]




def _exit_line_cross_harness(tmp_path, **cfg):
    """LONG via 3/200 cross, ready for a bar where SMA3 cuts SMA25 down."""
    h = BtcHarness(tmp_path, **{**ON, **cfg})
    h.run()
    assert h.state()["trade"]["side"] == "LONG"
    trade = h.state()["trade"]
    assert trade["exit_ma_enabled"] is True and trade["exit_ma_period"] == 25
    return h


def test_on_exit_line_cross_closes_without_reverse_entry(tmp_path):
    h = _exit_line_cross_harness(tmp_path)
    _exit_cross_leg(h)
    result = h.run()
    closed = h.service.ledger("testnet").trades(mode="LIVE", status="CLOSED")
    assert closed and closed[-1]["exit_reason"] == "CUSTOM_MA_EXIT", (result, closed)
    # The ledger row is schema-narrow; the full trade (incl. the exit detail and
    # the PnL) comes back in the result.
    trade = result["trade"]
    assert trade["custom_exit_kind"] == "cross"
    assert trade["exit_ma_enabled"] is True and trade["exit_ma_period"] == 25
    assert result["action"] == "exited"
    assert h.state()["trade"] is None            # no opposite position opened
    assert h.fake.position is None               # exchange position flat


def test_on_exit_line_cross_blocks_reentry_until_the_next_cross(tmp_path):
    h = _exit_line_cross_harness(tmp_path)
    _exit_cross_leg(h)
    assert h.run()["action"] == "exited"
    block = h.state().get("reentry_block", {})
    assert "BTCUSDT" in block and block["BTCUSDT"]["side"] == "LONG"
    assert block["BTCUSDT"]["exit_reason"] == "CUSTOM_MA_EXIT"
    # The next bar is still on the trend side of SMA200 but has no fresh cross:
    # with ON, a trend-side entry must not fill the gap.
    _append(h, (85_500.0, 85_900.0))
    for _ in range(2):
        assert h.run()["action"] != "entered"
    assert h.state()["trade"] is None and not h.fake.position


def test_on_reentry_happens_only_after_a_fresh_up_cross(tmp_path):
    h = _exit_line_cross_harness(tmp_path)
    _exit_cross_leg(h)
    assert h.run()["action"] == "exited"
    assert h.state()["trade"] is None
    # Closes recover above SMA200 -> SMA3 crosses ABOVE SMA200 again: new LONG.
    _append(h, RECOVER_ABOVE_SLOW[:RECOVERY_CROSSES_AT])
    result = h.run()
    assert result["action"] == "entered", result
    assert h.state()["trade"]["side"] == "LONG"
    assert h.state()["trade"]["entry_type"] == "CROSS"
    assert "BTCUSDT" not in h.state().get("reentry_block", {})


def test_on_opposite_cross_still_closes_but_does_not_reverse(tmp_path):
    h = BtcHarness(tmp_path, **ON)
    h.run()
    _append(h, OPPOSITE_CROSS_DOWN)  # SMA3 under SMA200: the plain opposite cross
    result = h.run()
    assert result["action"] == "exited", result
    closed = h.service.ledger("testnet").trades(mode="LIVE", status="CLOSED")
    assert closed[-1]["exit_reason"] in ("CROSS_EXIT", "CUSTOM_MA_EXIT")
    assert h.state()["trade"] is None
    assert h.fake.position is None
    # OFF would stop-and-reverse here; ON must stay flat until the next cross.
    _append(h, (83_200.0, 83_400.0))
    assert h.run()["action"] != "entered"
    assert h.state()["trade"] is None and h.fake.position is None


def test_on_realign_does_not_reverse(tmp_path):
    h = BtcHarness(tmp_path, **ON)
    h.run()
    # Fresh appending bar keeps SMA3 below the slow line without a *new* cross:
    # the "already on the other side" realign path must close only.
    rows = h.fake.bars["1h"]
    prev = rows[-1][4]
    rows.append([rows[-1][0] + H1, prev, prev * 1.001, 82_000, 82_500.0, 10.0])
    h.now_ms = now_after(rows)
    result = h.run()
    assert h.state()["trade"] is None and h.fake.position is None
    closed = h.service.ledger("testnet").trades(mode="LIVE", status="CLOSED")
    assert closed[-1]["exit_reason"] in ("CROSS_EXIT", "CUSTOM_MA_EXIT")
    # Exit reason + the line that fired are recorded on the trade for the exit text.
    assert result["trade"]["exit_ma_enabled"] is True
    assert result["trade"]["custom_exit_kind"] in ("", "cross", "realign")


def test_exit_text_names_the_custom_line(tmp_path):
    h = _exit_line_cross_harness(tmp_path)
    _exit_cross_leg(h)
    trade = h.run()["trade"]
    text = h.service._exit_text(trade, "LIVE")
    assert "CUSTOM_MA_EXIT" in text
    assert "SMA3 / SMA25" in text and "cross" in text
    assert "재진입은 다음 크로스 마감까지 대기" in text

    # The plain opposite cross (ON) is labelled separately.
    cross_trade = dict(trade, exit_reason="CROSS_EXIT", custom_exit_kind="")
    assert "CROSS_EXIT" in h.service._exit_text(cross_trade, "LIVE")
    assert "SMA3 / SMA200 반대 크로스" in h.service._exit_text(cross_trade, "LIVE")


def test_on_keeps_profit_lock_and_emergency_stop(tmp_path):
    h = BtcHarness(tmp_path, **ON)
    h.run()
    trade = h.state()["trade"]
    assert trade["stop_price"]                     # emergency stop still attached
    stops = [a for a in h.fake.algo_attempts if a["closePosition"] == "true"]
    assert stops and stops[0]["triggerPrice"] == "79900.0"
    h.fake.mark = 85_000 * 1.011                   # +5.5% margin ROI
    h.now_ms += 20_000
    h.run()
    assert h.state()["trade"]["lock_roi"] == "4.0"  # profit lock still ratchets


def test_on_trend_mode_does_not_enter_on_the_trend_side(tmp_path):
    h = BtcHarness(tmp_path, entry_mode="trend", **ON)
    assert h.run()["action"] == "entered"
    _exit_cross_leg(h)
    assert h.run()["action"] == "exited"
    # Trend mode keeps a position simply because SMA3 is above SMA200.  With the
    # exit line ON it must not re-enter without a fresh cross.
    _append(h, (86_200.0, 86_400.0, 86_600.0))
    for _ in range(3):
        assert h.run()["action"] != "entered"
    assert h.state()["trade"] is None


def test_on_rotation_after_exit_is_disabled(tmp_path):
    coins = {"BTCUSDT": coin(900, 86_000), "ETHUSDT": coin(800, 86_000)}
    h = ScopeHarness(tmp_path, coins, scan_scope="alts", entry_mode="trend",
                     rotate_after_exit=True, **ON)
    h.run()
    latest = max(c["rows"][-1][0] for c in h.fake.coins.values())
    for c in h.fake.coins.values():
        prev = c["rows"][-1][4]
        c["rows"].append([c["rows"][-1][0] + H1, prev, max(prev, 83_000) * 1.001,
                          min(prev, 83_000) * 0.999, 83_000.0, 1.0])
    h.now_ms = latest + H1 + 5_000
    h.run()
    assert "rotation_pending" not in h.state()


def test_on_dry_run_exits_on_the_exit_line(tmp_path):
    h = BtcHarness(tmp_path, live=False, **ON)
    assert h.run()["action"] == "entered"
    assert h.state("DRY_RUN")["trade"] is not None
    _exit_cross_leg(h)
    result = h.run()
    trades = h.service.ledger("testnet").trades(mode="DRY_RUN", status="CLOSED")
    assert trades and trades[-1]["exit_reason"] == "CUSTOM_MA_EXIT", (result, trades)
    assert h.state("DRY_RUN")["trade"] is None
    assert h.fake.orders == [] and h.fake.algo_attempts == []


# =========================================================== Telegram
def test_telegram_exit_ma_buttons_and_command(tmp_path):
    controller = _controller(tmp_path)
    press = lambda a: asyncio.run(controller._handle_btc_ma_cross_action(a))  # noqa: E731

    text, markup = press("status")
    callbacks = _callbacks(markup)
    assert "bm:xma:on" in callbacks
    assert "bm:xmap:25" in callbacks and "bm:xmap:69" in callbacks
    assert "청산 MA: OFF" in text

    # ON via the header button keeps the default period.
    press("xma:on")
    cfg = controller.btc_ma_cross_service.config()
    assert cfg["exit_ma_enabled"] is True and cfg["exit_ma_period"] == 25

    # Period preset.
    press("xmap:69")
    assert controller.btc_ma_cross_service.config()["exit_ma_period"] == 69
    assert "SMA69" in press("status")[0]

    # OFF again: enabled flag only.
    press("xma:off")
    assert controller.btc_ma_cross_service.config()["exit_ma_enabled"] is False
    assert "청산 MA: OFF" in press("status")[0]


def test_telegram_bad_exit_period_is_rejected(tmp_path):
    controller = _controller(tmp_path)
    press = lambda a: asyncio.run(controller._handle_btc_ma_cross_action(a))  # noqa: E731
    press("xma:on")
    before = controller.btc_ma_cross_service.config()["exit_ma_period"]
    text, _ = press("xmap:1")
    assert "올바르지" in text
    assert controller.btc_ma_cross_service.config()["exit_ma_period"] == before


def test_telegram_exit_column_in_status_and_rules(tmp_path):
    controller = _controller(tmp_path)
    press = lambda a: asyncio.run(controller._handle_btc_ma_cross_action(a))  # noqa: E731
    press("xma:on")
    status = press("status")[0]
    rules = press("rules")[0]
    assert "청산 MA: ON · SMA3가 SMA25" in status
    assert "반대 진입 없이" in status
    assert "추가 청산 MA: ON" in rules
    assert "반대 진입 없음" in rules


def test_telegram_parse_exit_ma_command():
    from bot_runtime.controller_btc_ma_cross import parse_exit_ma_command
    from trading_safety.sma_periods import PeriodError

    assert parse_exit_ma_command(["exit", "25"]) == (True, 25)
    assert parse_exit_ma_command(["off"]) == (False, EXIT_MA_DEFAULT_PERIOD)
    assert parse_exit_ma_command(["on"]) == (True, EXIT_MA_DEFAULT_PERIOD)
    assert parse_exit_ma_command(["on", "69"]) == (True, 69)
    with pytest.raises(PeriodError):
        parse_exit_ma_command(["exit", "1"])
    with pytest.raises(PeriodError):
        parse_exit_ma_command(["exit", "abc"])
    with pytest.raises(PeriodError):
        parse_exit_ma_command([])


def test_telegram_help_and_entry_text_reflect_the_flag(tmp_path):
    controller = _controller(tmp_path)
    press = lambda a: asyncio.run(controller._handle_btc_ma_cross_action(a))  # noqa: E731
    off_entry = press("status")[0]
    assert "돌파 시 진입" in off_entry
    press("xma:on")
    on_entry = press("status")[0]
    assert "크로스 마감 진입만" in on_entry
    assert "청산 MA 직접 입력" in press("xma_help")[0]
