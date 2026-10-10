"""BTC SMA3/SMA200 cross: signal, profit lock, sizing, stop-and-reverse, Telegram, wiring."""
import asyncio
from decimal import Decimal

import pytest

from btc_ma_cross.config import normalize_btc_ma_cross_config
from btc_ma_cross.service import BtcMaCrossService
from btc_ma_cross.signals import emergency_stop_price, evaluate_cross, profit_lock_target, sma
from tests.btc_pullback_fixtures import H1, M1, FakeBinance, exchange_info

CFG = normalize_btc_ma_cross_config({})
T0 = 1_790_000_000_000 - (1_790_000_000_000 % H1)


def bars(last_close, n=205, base=85_000.0, start_ms=T0, span=H1):
    """n-1 flat closes then one final close: a cross happens on the last bar."""
    rows = []
    for i in range(n - 1):
        rows.append([start_ms + i * span, base, base * 1.001, base * 0.999, base, 10.0])
    o = base
    rows.append([start_ms + (n - 1) * span, o, max(o, last_close) * 1.001, min(o, last_close) * 0.999, last_close, 10.0])
    return rows


def now_after(rows, span=H1, seconds=5):
    return rows[-1][0] + span + seconds * 1000


# ---------------------------------------------------------------- signals
def test_sma_and_cross_on_closed_candles():
    assert sma([1, 2, 3, 4], 3) == [None, None, 2.0, 3.0]
    up = bars(86_000.0)
    signal = evaluate_cross(up, CFG, now_after(up))
    assert signal["side"] == "LONG" and signal["signal_id"].endswith(f"LONG:{up[-1][0]}")
    down = bars(84_000.0)
    assert evaluate_cross(down, CFG, now_after(down))["side"] == "SHORT"
    # The in-progress candle is ignored: one second before it closes, no cross yet.
    assert evaluate_cross(up, CFG, up[-1][0] + H1 - 1_000)["side"] is None
    stale = evaluate_cross(up, CFG, now_after(up, seconds=600))
    assert stale["side"] is None and stale["skip_reason"] == "SIGNAL_STALE"
    flat = bars(85_000.0)
    assert evaluate_cross(flat, CFG, now_after(flat))["side"] is None


def test_timeframe_choices():
    cfg = normalize_btc_ma_cross_config({"timeframe": "4h"})
    rows = bars(86_000.0, span=4 * H1)
    assert evaluate_cross(rows, cfg, now_after(rows, span=4 * H1))["side"] == "LONG"
    assert normalize_btc_ma_cross_config({"timeframe": "3h"})["timeframe"] == "1h"
    assert normalize_btc_ma_cross_config({"leverage": 20})["leverage"] == 5


@pytest.mark.parametrize("roi,locked", [(4.9, None), (5, 4), (9.99, 4), (10, 9), (14.9, 9), (15, 14), (20, 19), (27, 24)])
def test_profit_lock_table_matches_the_agreed_staircase(roi, locked):
    entry = 100_000.0
    best = entry * (1 + roi / 500)  # 5x leverage: margin ROI = price move x 5
    target = profit_lock_target("LONG", entry, best, 5, 5, 5, 1)
    assert (target[1] if target else None) == (None if locked is None else pytest.approx(locked))
    short = profit_lock_target("SHORT", entry, entry * (1 - roi / 500), 5, 5, 5, 1)
    assert (short[1] if short else None) == (None if locked is None else pytest.approx(locked))


def test_lock_and_emergency_prices():
    assert profit_lock_target("LONG", 100_000, 101_000, 5, 5, 5, 1)[2] == pytest.approx(100_800)
    assert profit_lock_target("SHORT", 100_000, 99_000, 5, 5, 5, 1)[2] == pytest.approx(99_200)
    assert emergency_stop_price("LONG", 100_000, 5, 30) == pytest.approx(94_000)
    assert emergency_stop_price("SHORT", 100_000, 5, 30) == pytest.approx(106_000)
    assert emergency_stop_price("LONG", 100_000, 5, 0) is None


# ---------------------------------------------------------------- service
class Fake(FakeBinance):
    def __init__(self, rows, **kw):
        super().__init__(**kw)
        self.bars = {"1h": rows}

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
        if self.raise_klines:
            raise ConnectionError("disconnected")
        if timeframe == "1m":
            return self.m1
        return self.bars[timeframe]

    def fire(self, client_prefix, price):
        """Binance fires one specific trigger (e.g. only the profit lock)."""
        side = "SELL" if self.position["side"] == "long" else "BUY"
        qty = self.position["contracts"]
        for order in self.algos.values():
            if order["algoStatus"] == "NEW" and order["clientAlgoId"].startswith(client_prefix):
                order["algoStatus"] = "FINISHED"
        self.position = None
        self._fill(side, qty, price, 3)


class Harness:
    def __init__(self, tmp_path, *, live=True, network="testnet", last_close=86_000.0, **cfg):
        self.rows = bars(last_close)
        self.now_ms = now_after(self.rows)
        self.fake = Fake(self.rows, mark=85_000.0, wallet=5_000.0,
                         info=exchange_info(step="0.0001", min_qty="0.0001") if network == "testnet" else None)
        self.cfg = {"enabled": True, "trading_mode": "LIVE" if live else "DRY_RUN", **cfg}
        self.network = network
        self.notes = []
        self.tmp_path = tmp_path
        self.service = self.build()

    def build(self):
        async def no_sleep(_):
            return None

        return BtcMaCrossService(
            config_getter=lambda: self.cfg,
            credentials_getter=lambda n: {"api_key": "k", "secret_key": "s"},
            exchange_factory=lambda n, c: self.fake,
            runtime_dir=self.tmp_path, notifier=self.notes.append,
            main_network_getter=lambda: self.network, clock=lambda: self.now_ms / 1000,
            sleep=no_sleep, environ={},
        )

    def run(self):
        return asyncio.run(self.service.run_cycle())

    def state(self, mode="LIVE"):
        return self.service.load_state(self.network, mode)

    def next_bar(self, last_close):
        """Append a new closed bar that produces a cross the other way."""
        rows = self.rows[:-1] + [self.rows[-1]]
        prev_close = rows[-1][4]
        rows.append([rows[-1][0] + H1, prev_close, max(prev_close, last_close) * 1.001,
                     min(prev_close, last_close) * 0.999, last_close, 10.0])
        self.rows = rows
        self.fake.bars["1h"] = rows
        self.now_ms = now_after(rows)


def test_entry_uses_half_the_margin_at_5x_with_emergency_stop(tmp_path):
    h = Harness(tmp_path)
    assert h.run()["action"] == "entered"
    trade = h.state()["trade"]
    # 5,000 USDT x 50% x 5 / 85,000 = 0.1470 BTC (step 0.0001)
    assert trade["side"] == "LONG" and Decimal(trade["quantity"]) == Decimal("0.147")
    assert h.fake.leverage == 5 and h.fake.margin_type == "isolated"
    stops = [a for a in h.fake.algo_attempts if a["type"] == "STOP_MARKET"]
    assert len(stops) == 1 and stops[0]["closePosition"] == "true"
    assert stops[0]["triggerPrice"] == "79900.0"  # -30% margin ROI = -6% price
    assert stops[0]["workingType"] == "MARK_PRICE"
    assert not [a for a in h.fake.algo_attempts if a["type"] == "TAKE_PROFIT_MARKET"]


def test_emergency_stop_off_places_no_stop(tmp_path):
    h = Harness(tmp_path, emergency_stop_roi_percent=0)
    h.run()
    assert h.state()["trade"]["stop_price"] == ""
    assert h.fake.algo_attempts == []


def test_profit_lock_ratchets_and_replaces_the_previous_lock(tmp_path):
    h = Harness(tmp_path)
    h.run()
    h.fake.mark = 85_000 * 1.011  # +5.5% margin ROI
    h.now_ms += 20_000
    h.run()
    trade = h.state()["trade"]
    assert trade["lock_roi"] == "4.0" and trade["lock_price"] == "85680.0"
    lock = [a for a in h.fake.algo_attempts if a.get("reduceOnly") == "true"]
    assert lock and lock[0]["quantity"] == trade["quantity"] and lock[0]["workingType"] == "CONTRACT_PRICE"
    first_lock = trade["lock_client_algo_id"]
    h.fake.mark = 85_000 * 1.021  # +10.5%
    h.now_ms += 20_000
    h.run()
    trade = h.state()["trade"]
    assert trade["lock_roi"] == "9.0" and trade["lock_price"] == "86530.0"
    assert h.fake.algos[first_lock]["algoStatus"] == "CANCELED"
    # The lock never moves down.
    h.fake.mark = 85_000 * 1.012
    h.now_ms += 20_000
    h.run()
    assert h.state()["trade"]["lock_roi"] == "9.0"


def test_profit_lock_exit_then_wait_for_the_next_cross(tmp_path):
    h = Harness(tmp_path)
    h.run()
    h.fake.mark = 85_000 * 1.021
    h.now_ms += 20_000
    h.run()
    lock_id = h.state()["trade"]["lock_client_algo_id"]
    h.fake.fire(lock_id, 86_530.0)
    h.now_ms += 20_000
    result = h.run()
    assert result["trade"]["exit_reason"] == "PROFIT_LOCK"
    assert float(result["trade"]["net_pnl"]) > 0
    orders = len(h.fake.orders)
    h.now_ms += 20_000
    assert h.run()["reason"] == "WAITING_NEXT_CLOSE"  # same bar: no re-entry
    assert len(h.fake.orders) == orders
    assert all(o["algoStatus"] != "NEW" for o in h.fake.algos.values())


def test_opposite_cross_closes_and_reverses_immediately(tmp_path):
    h = Harness(tmp_path)
    h.run()
    h.next_bar(83_000.0)  # SMA3 drops below SMA200 on the next closed bar
    result = h.run()
    assert result["action"] == "reversed", result
    closed = h.service.ledger("testnet").trades(mode="LIVE", status="CLOSED")
    assert closed[-1]["side"] == "LONG" and closed[-1]["exit_reason"] == "CROSS_REVERSE"
    trade = h.state()["trade"]
    assert trade["side"] == "SHORT" and h.fake.position["side"] == "short"
    assert any("close" in o["clientOrderId"] for o in h.fake.orders)


def test_emergency_stop_failure_triggers_failsafe(tmp_path):
    h = Harness(tmp_path)
    h.fake.fail_sl_creates = 99
    result = h.run()
    assert result["action"] == "exited" and result["trade"]["exit_reason"] == "FAILSAFE"
    assert h.fake.position is None


def test_below_exchange_minimum_is_skipped(tmp_path):
    h = Harness(tmp_path, network="mainnet")
    h.fake.wallet = 10.0  # 10 x 50% x 5 = 25 USDT notional < 50 USDT minimum
    result = h.run()
    assert result["reason"].startswith("BELOW_EXCHANGE_MINIMUM")
    assert h.fake.orders == []


def test_any_account_position_blocks_entry(tmp_path):
    h = Harness(tmp_path)
    h.fake.other_positions = [{"symbol": "ETH/USDT:USDT", "side": "long", "contracts": 1.0, "entryPrice": 3000}]
    assert h.run()["reason"].startswith("ACCOUNT_POSITION_EXISTS")


def test_dry_run_simulates_lock_exit_without_orders(tmp_path):
    h = Harness(tmp_path, live=False)
    assert h.run()["action"] == "entered"
    trade = h.state("DRY_RUN")["trade"]
    entry = float(trade["entry_price"])
    t1 = trade["entry_ms"] + M1
    h.fake.m1 = [
        [t1, entry, entry * 1.021, entry, entry * 1.02, 1.0],               # +10.5% ROI peak -> lock +9%
        [t1 + M1, entry * 1.02, entry * 1.02, entry * 1.015, entry * 1.016, 1.0],  # falls through the lock
    ]
    h.now_ms += 5 * M1
    result = h.run()
    assert result["action"] == "exited" and result["trade"]["exit_reason"] == "PROFIT_LOCK"
    assert h.fake.orders == [] and h.fake.algo_attempts == []
    assert float(result["trade"]["net_pnl"]) > 0


def test_dry_run_reverses_on_opposite_cross(tmp_path):
    h = Harness(tmp_path, live=False)
    h.run()
    h.next_bar(83_000.0)
    assert h.run()["action"] == "reversed"
    assert h.state("DRY_RUN")["trade"]["side"] == "SHORT"
    assert h.fake.orders == []


def test_owned_keys_and_client_prefix(tmp_path):
    h = Harness(tmp_path)
    assert h.service.owned_position_keys() == set()
    h.run()
    assert h.service.owned_position_keys() == {"BTCUSDT"}
    assert all(a["clientAlgoId"].startswith("btcma-") for a in h.fake.algo_attempts)
    assert all(o["clientOrderId"].startswith("btcma-") for o in h.fake.orders)


# ---------------------------------------------------------------- Telegram
class _Cfg:
    def __init__(self, data):
        self.data = data

    def get(self, key, default=None):
        return self.data.get(key, default)

    async def update_value(self, path, value):
        node = self.data
        for key in path[:-1]:
            node = node.setdefault(key, {})
        node[path[-1]] = value


def _controller(tmp_path):
    from bot_runtime.controller_btc_ma_cross import ControllerBtcMaCrossMixin
    from bot_runtime.controller_btc_pullback import ControllerBtcPullbackMixin
    from bot_runtime.controller_options import ControllerOptionsMixin

    class Controller(ControllerBtcMaCrossMixin, ControllerBtcPullbackMixin, ControllerOptionsMixin):
        pass

    controller = Controller()
    controller.cfg = _Cfg({"btc_ma_cross": {}, "btc_ema_pullback": {"enabled": True}, "options_trading": {"enabled": False}})
    controller.is_paused = False
    controller.notes = []

    async def notify_plain(text, event_type=None):
        controller.notes.append(text)

    controller.notify_plain = notify_plain
    h = Harness(tmp_path, live=False)
    h.cfg = controller.cfg.data["btc_ma_cross"]
    controller.btc_ma_cross_service = h.build()
    return controller


def _callbacks(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def test_telegram_menu_timeframe_emergency_stop_and_exclusivity(tmp_path):
    controller = _controller(tmp_path)
    press = lambda a: asyncio.run(controller._handle_btc_ma_cross_action(a))  # noqa: E731
    text, markup = press("status")
    assert "3/200 SMA" in text and "/setup" in text
    callbacks = _callbacks(markup)
    for tf in ("15m", "30m", "1h", "2h", "4h"):
        assert f"bm:tf:{tf}" in callbacks
    for choice in (0, 10, 20, 30, 50):
        assert f"bm:es:{choice}" in callbacks
    press("tf:4h")
    assert controller.cfg.data["btc_ma_cross"]["timeframe"] == "4h"
    press("es:0")
    assert controller.cfg.data["btc_ma_cross"]["emergency_stop_roi_percent"] == 0
    assert "비상손절: OFF" in press("status")[0]
    _, markup = press("on")
    assert "bm:confirm_on" in _callbacks(markup)
    press("confirm_on")
    assert controller.cfg.data["btc_ma_cross"]["enabled"] is True
    assert controller.is_paused is True
    assert controller.cfg.data["btc_ema_pullback"]["enabled"] is False  # only one standalone at a time
    assert "+5%→+4%" in press("rules")[0]


def test_pullback_on_turns_ma_cross_off(tmp_path):
    controller = _controller(tmp_path)
    controller.cfg.data["btc_ma_cross"]["enabled"] = True
    asyncio.run(controller._turn_off_standalone_futures_strategies(except_key="btc_ema_pullback"))
    assert controller.cfg.data["btc_ma_cross"]["enabled"] is False
    assert controller.cfg.data["btc_ema_pullback"]["enabled"] is True


def test_wiring_keyboard_config_stop_and_report():
    from pathlib import Path

    import emas
    from bot_runtime.daily_analysis_report import STANDALONE_REPORT_KEYS

    controller = emas.MainController.__new__(emas.MainController)
    labels = [b.text for row in controller._build_main_keyboard().keyboard for b in row]
    assert "/btcmacross" in labels and "/btcpullback" in labels
    config = emas.TradingConfig.__new__(emas.TradingConfig)
    config.config = {}
    config.config_file = "unused.json"
    config.save_config_sync = lambda: None
    config._ensure_defaults()
    assert config.config["btc_ma_cross"]["trading_mode"] == "DRY_RUN"
    assert config.config["btc_ma_cross"]["enabled"] is False
    options = (Path(__file__).parents[1] / "bot_runtime" / "controller_options.py").read_text(encoding="utf-8")
    assert "_stop_btc_ma_cross_for_emergency" in options
    assert "btc_ma_cross" in {key for key, *_ in STANDALONE_REPORT_KEYS}


def test_daily_report_has_ma_cross_section():
    from datetime import datetime, timedelta, timezone

    from bot_runtime.daily_analysis_report import build_daily_analysis_report, standalone_summary

    start = datetime(2026, 10, 9, 0, 0, tzinfo=timezone.utc)
    trade = {"trade_id": "x", "status": "CLOSED", "side": "LONG", "quantity": "0.147", "entry_price": "85000",
             "stop_price": "79900.0", "lock_price": "86530.0", "exit_price": "86530", "exit_reason": "PROFIT_LOCK",
             "entry_time": (start + timedelta(hours=1)).isoformat(), "exit_time": (start + timedelta(hours=5)).isoformat(),
             "net_pnl": "212.3", "commission": "12.5", "funding": "0", "r_multiple": "0.085"}
    inputs = {"window_start": start.isoformat(), "window_end": (start + timedelta(days=1)).isoformat(),
              "generated_at": start.isoformat(), "trades": [], "journal": [], "positions": [], "positions_known": True,
              "btc_ma_cross": {"network": "testnet", "mode": "LIVE", "enabled": True, "trades": [trade], "signals": 1,
                               "skip_counts": {"NO_CROSS_ABOVE": 20}, "failsafe_events": [], "protection_failures": [],
                               "owned_keys": [], "profit_locks": 2}}
    assert standalone_summary(inputs, "btc_ma_cross") == (1, 1, pytest.approx(212.3))
    text = build_daily_analysis_report(inputs)
    assert "3/200 SMA 전략 (/btcmacross" in text and "PROFIT_LOCK" in text and "수익 확보 손절 상향 2회" in text


# ------------------------------------------- reversal cancels old protection
def _open_algos(fake):
    return [o for o in fake.algos.values() if o["algoStatus"] == "NEW"]


def test_reversal_cancels_the_old_emergency_stop_and_places_a_new_one(tmp_path):
    h = Harness(tmp_path)
    h.run()
    old_stop = h.state()["trade"]["sl_client_algo_id"]
    assert h.fake.algos[old_stop]["side"] == "SELL"  # LONG's stop
    h.next_bar(83_000.0)
    assert h.run()["action"] == "reversed"
    assert h.fake.algos[old_stop]["algoStatus"] == "CANCELED"
    open_orders = _open_algos(h.fake)
    assert len(open_orders) == 1  # only the new SHORT's emergency stop
    new_stop = open_orders[0]
    assert new_stop["clientAlgoId"] == h.state()["trade"]["sl_client_algo_id"] != old_stop
    assert new_stop["side"] == "BUY" and new_stop["orderType"] == "STOP_MARKET"
    assert float(new_stop["triggerPrice"]) > float(h.state()["trade"]["entry_price"])  # above a short entry


def test_reversal_also_cancels_an_active_profit_lock(tmp_path):
    h = Harness(tmp_path)
    h.run()
    h.fake.mark = 85_000 * 1.021  # lock +9% active
    h.now_ms += 20_000
    h.run()
    trade = h.state()["trade"]
    old_ids = {trade["sl_client_algo_id"], trade["lock_client_algo_id"]}
    assert len(_open_algos(h.fake)) == 2
    h.next_bar(83_000.0)
    assert h.run()["action"] == "reversed"
    assert all(h.fake.algos[i]["algoStatus"] == "CANCELED" for i in old_ids)
    remaining = _open_algos(h.fake)
    assert len(remaining) == 1 and remaining[0]["side"] == "BUY"
    assert h.state()["trade"]["lock_price"] == ""  # fresh trade: no lock yet


def test_reversal_never_enters_while_an_old_order_could_not_be_cancelled(tmp_path):
    h = Harness(tmp_path)
    h.run()
    old_stop = h.state()["trade"]["sl_client_algo_id"]

    def failing_cancel(params):
        raise Exception("binance {\"code\":-1001,\"msg\":\"Internal error\"}")

    real_cancel = h.fake.fapiPrivateDeleteAlgoOrder
    h.fake.fapiPrivateDeleteAlgoOrder = failing_cancel
    h.next_bar(83_000.0)
    result = h.run()
    assert "반대 진입 보류" in result["reason"]
    assert h.fake.position is None  # the LONG was closed
    assert h.fake.algos[old_stop]["algoStatus"] == "NEW"  # cancel failed
    entries = [o for o in h.fake.orders if "entry" in o["clientOrderId"]]
    assert len(entries) == 1  # no SHORT entry while the old stop is still open
    assert h.state()["pending_reverse_entry"]["side"] == "SHORT"
    h.now_ms += 20_000
    assert h.run()["reason"].startswith("REVERSE_ENTRY_RETRY: OPEN_ORDERS_CANCEL_FAILED")
    # The exchange recovers: the old stop is cleaned up and the SHORT is taken.
    h.fake.fapiPrivateDeleteAlgoOrder = real_cancel
    h.now_ms += 20_000
    assert h.run()["action"] == "entered"
    assert h.fake.algos[old_stop]["algoStatus"] == "CANCELED"
    assert h.fake.position["side"] == "short" and h.state()["trade"]["side"] == "SHORT"
    assert h.state()["pending_reverse_entry"] is None
    assert len(_open_algos(h.fake)) == 1


def test_reverse_retry_gives_up_after_the_window(tmp_path):
    h = Harness(tmp_path)
    h.run()

    def failing_cancel(params):
        raise Exception("binance {\"code\":-1001,\"msg\":\"Internal error\"}")

    h.fake.fapiPrivateDeleteAlgoOrder = failing_cancel
    h.next_bar(83_000.0)
    h.run()
    h.now_ms += 11 * 60_000  # past the 10-minute retry window
    assert h.run()["reason"] == "REVERSE_ENTRY_EXPIRED"
    assert h.state()["pending_reverse_entry"] is None
    assert any("포기" in n for n in h.notes)
    h.now_ms += 20_000
    assert h.run()["reason"] == "WAITING_NEXT_CLOSE"  # waits for the next cross
