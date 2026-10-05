"""BTC EMA pullback strategy: signals, rules, risk, execution safety, reconciliation."""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from btc_ema_pullback.config import (
    TRADING_MODE_DRY_RUN,
    TRADING_MODE_LIVE,
    effective_trading_mode,
    normalize_btc_pullback_config,
)
from btc_ema_pullback.ledger import compute_stats, daily_entry_block_reason
from btc_ema_pullback.rules import (
    ceil_to_step,
    floor_to_step,
    parse_trading_rules,
    protection_prices,
    round_price_to_tick,
    validate_and_normalize_order_quantity,
)
from btc_ema_pullback.service import BtcEmaPullbackService
from btc_ema_pullback.signals import count_crosses, ema, evaluate_trend, generate_signal
from tests.btc_pullback_fixtures import (
    H1,
    M1,
    FakeBinance,
    exchange_info,
    flat_1h,
    long_setup_15m,
    mirror,
    now_after,
    trend_1h,
)

CFG = normalize_btc_pullback_config({})
MAINNET_RULES = parse_trading_rules(exchange_info()["symbols"][0])
DEMO_RULES = parse_trading_rules(exchange_info(step="0.0001", min_qty="0.0001")["symbols"][0])


# --------------------------------------------------------------- 1-3 signals
def test_long_signal_true_and_false():
    h1, m15 = trend_1h(), long_setup_15m()
    signal = generate_signal(h1, m15, CFG, now_after(m15))
    assert signal["side"] == "LONG" and signal["entry_condition"] is True
    assert signal["signal_id"] == f"BTCUSDT:LONG:{m15[-1][0]}"
    for key in ("1h_close", "1h_ema20", "1h_ema50", "1h_ema20_slope", "15m_close", "15m_ema20",
                "15m_ema50", "previous_high_or_low", "atr"):
        assert key in signal
    # Without the pullback the uptrend alone is not an entry.
    no_pullback = trend_1h(n=220, start=80_000, step=0.0003)
    plain = generate_signal(h1, no_pullback, CFG, no_pullback[-1][0] + 900_000 + 5_000)
    assert plain["side"] is None and plain["skip_reason"]


def test_short_signal_true_and_false():
    h1, m15 = mirror(trend_1h()), mirror(long_setup_15m())
    signal = generate_signal(h1, m15, CFG, now_after(m15))
    assert signal["side"] == "SHORT"
    # SHORT setup against an uptrend 1h is rejected.
    against = generate_signal(trend_1h(), m15, CFG, now_after(m15))
    assert against["side"] is None


def test_in_progress_candle_is_ignored_and_stale_signal_skipped():
    h1, m15 = trend_1h(), long_setup_15m()
    # One second before the confirmation bar closes it is still in progress.
    early = generate_signal(h1, m15, CFG, m15[-1][0] + 900_000 - 1_000)
    assert early.get("15m_bar_open_ms") == m15[-2][0]
    late = generate_signal(h1, m15, CFG, now_after(m15, seconds=CFG["signal_max_age_seconds"] + 60))
    assert late["side"] is None and late["skip_reason"] == "SIGNAL_STALE"


def test_sideways_filters():
    m15 = long_setup_15m()
    flat = generate_signal(flat_1h(), m15, CFG, now_after(m15))
    assert flat["side"] is None and flat["skip_reason"] == "CHOP_EMA_TOO_CLOSE"
    flat_slope = evaluate_trend(trend_1h(step=0.0001), normalize_btc_pullback_config(
        {"ema_separation_threshold": 0.0, "ema_slope_threshold": 0.001}))
    assert flat_slope["skip_reason"] == "CHOP_EMA_FLAT"
    # A fresh EMA cross inside the lookback counts as chop.
    rows, price = [], 80_000.0
    for i in range(220):
        step = -0.001 if i < 170 else 0.004
        rows.append([i * H1, price, price * 1.001, price * 0.999, price * (1 + step), 1.0])
        price *= 1 + step
    crossed = evaluate_trend(rows, normalize_btc_pullback_config(
        {"recent_cross_lookback": 40, "max_recent_crosses": 0}))
    assert crossed["skip_reason"] == "CHOP_RECENT_CROSSES"
    assert count_crosses([1, 3, 1, 3], [2, 2, 2, 2], 4) == 3


def test_chasing_a_big_candle_is_refused():
    m15 = long_setup_15m(confirm_scale=6)
    signal = generate_signal(trend_1h(), m15, CFG, now_after(m15))
    assert signal["side"] is None and "NOT_CHASING" in signal["skip_reason"]


def test_ema_is_linear_and_seeded_with_sma():
    assert ema([1, 2, 3, 4], 2)[1] == pytest.approx(1.5)
    assert ema([1, 2], 3) == []


# --------------------------------------------------------- 4-7 prices/steps
def test_stop_and_take_profit_from_actual_fill_rounded_towards_entry():
    sl, tp = protection_prices("LONG", "85000.05", CFG, MAINNET_RULES)
    assert sl == Decimal("84320.1") and tp == Decimal("86360.0")
    assert sl > Decimal("85000.05") * Decimal("0.992")
    sl, tp = protection_prices("SHORT", "85000.05", CFG, MAINNET_RULES)
    assert sl == Decimal("85680.0") and tp == Decimal("83640.1")


def test_tick_and_step_rounding():
    assert round_price_to_tick("85000.07", "0.10", "down") == Decimal("85000.0")
    assert round_price_to_tick("85000.01", "0.10", "up") == Decimal("85000.1")
    assert round_price_to_tick("85000.05", "0.10") in {Decimal("85000.0"), Decimal("85000.1")}
    assert str(round_price_to_tick("84320.0", "0.10")) == "84320.0"
    assert str(round_price_to_tick("84321", "10", "down")) == "84320"
    assert floor_to_step("0.0019", "0.001") == Decimal("0.001")
    assert ceil_to_step("0.0011", "0.001") == Decimal("0.002")
    assert floor_to_step("0.03087", "0.0001") == Decimal("0.0308")


# ------------------------------------------------ 8-10 quantity validation
def _validate(rules=MAINNET_RULES, price=85_000, wallet=210, cfg=None, sizing="fixed"):
    return validate_and_normalize_order_quantity(
        rules=rules, price=price, wallet_balance=wallet, available_balance=wallet,
        cfg=cfg or CFG, taker_fee_rate="0.0005", sizing_mode=sizing,
    )


def test_base_quantity_accepted_when_inside_limits():
    decision = _validate()
    assert decision["accepted"] is True
    assert decision["quantity"] == Decimal("0.001")
    assert decision["estimated_total_risk"] < decision["risk_limit"]
    assert decision["leverage"] == Decimal(3)


def test_min_qty_is_enforced_and_never_silently_raised():
    rules = parse_trading_rules(exchange_info(min_qty="0.002", step="0.001")["symbols"][0])
    decision = _validate(rules=rules, wallet=10_000)
    assert decision["accepted"] is False
    assert decision["skip_reason"].startswith("BELOW_EXCHANGE_MINIMUM")
    raised = _validate(rules=rules, wallet=10_000, cfg=normalize_btc_pullback_config(
        {"allow_auto_raise_to_exchange_minimum": True}))
    assert raised["accepted"] is True and raised["quantity"] == Decimal("0.002")


def test_min_notional_is_enforced():
    # 0.001 BTC @ 40,000 = 40 USDT < 50 USDT minimum notional.
    decision = _validate(price=40_000, wallet=10_000)
    assert decision["skip_reason"].startswith("BELOW_EXCHANGE_MINIMUM")
    assert decision["exchange_minimum_quantity"] == Decimal("0.002")
    allow = normalize_btc_pullback_config({"allow_auto_raise_to_exchange_minimum": True})
    too_risky = _validate(price=40_000, wallet=50, cfg=allow)
    assert too_risky["skip_reason"].startswith("EXCHANGE_MINIMUM_EXCEEDS_RISK_LIMIT")


def test_risk_limit_skips_instead_of_raising_leverage():
    decision = _validate(wallet=100)  # 0.5% = 0.50 USDT < ~0.84 USDT risk
    assert decision["accepted"] is False and decision["skip_reason"].startswith("RISK_LIMIT")
    assert decision["leverage"] == Decimal(3)
    assert normalize_btc_pullback_config({"leverage": 10})["leverage"] == 3


def test_testnet_risk_based_size_uses_the_whole_risk_budget():
    decision = _validate(rules=DEMO_RULES, wallet=5_000, sizing="risk_based")
    assert decision["accepted"] is True
    assert decision["quantity"] > Decimal("0.01")
    assert decision["estimated_total_risk"] <= decision["risk_limit"]
    assert decision["risk_limit"] == Decimal("25.000")


def test_margin_must_fit_available_balance():
    decision = validate_and_normalize_order_quantity(
        rules=DEMO_RULES, price=85_000, wallet_balance=5_000, available_balance=100,
        cfg=CFG, taker_fee_rate="0.0005", sizing_mode="risk_based",
    )
    assert decision["skip_reason"].startswith("INSUFFICIENT_MARGIN")


# --------------------------------------------------------- 11-12 daily rules
def _trades(*pnls, open_last=False):
    rows = [{"status": "CLOSED", "net_pnl": str(p)} for p in pnls]
    if open_last:
        rows.append({"status": "OPEN"})
    return rows


def test_three_trades_per_day_limit():
    assert daily_entry_block_reason(_trades(1, 1), CFG, day_start_equity=100, current_equity=100) == ""
    assert daily_entry_block_reason(_trades(1, 1, 1), CFG, day_start_equity=100, current_equity=100).startswith("DAILY_TRADE_LIMIT")


def test_three_consecutive_losses_and_daily_loss_limit():
    cfg = normalize_btc_pullback_config({"max_trades_per_day": 10})
    assert daily_entry_block_reason(_trades(-1, -1, -1), cfg, day_start_equity=1000, current_equity=999).startswith("CONSECUTIVE_LOSS_LIMIT")
    assert daily_entry_block_reason(_trades(-1, 2, -1, -1), cfg, day_start_equity=1000, current_equity=999) == ""
    assert daily_entry_block_reason([], cfg, day_start_equity=1000, current_equity=984).startswith("DAILY_LOSS_LIMIT")


def test_prohibited_behaviours_cannot_be_enabled_and_dry_run_is_default():
    cfg = normalize_btc_pullback_config({"allow_martingale": True, "allow_averaging": True, "allow_pyramiding": True})
    assert not (cfg["allow_martingale"] or cfg["allow_averaging"] or cfg["allow_pyramiding"])
    defaults = normalize_btc_pullback_config({})
    assert defaults["enabled"] is False and defaults["trading_mode"] == TRADING_MODE_DRY_RUN
    live = normalize_btc_pullback_config({"trading_mode": "LIVE"})
    assert effective_trading_mode(live, {}) == TRADING_MODE_LIVE
    assert effective_trading_mode(live, {"BTC_PULLBACK_TRADING_MODE": "DRY_RUN"}) == TRADING_MODE_DRY_RUN


# ------------------------------------------------------- service harness
class Harness:
    def __init__(self, tmp_path, *, live=True, network="testnet", creds=True, main_network="mainnet", **cfg):
        self.h1, self.m15 = trend_1h(), long_setup_15m()
        self.now_ms = now_after(self.m15)
        self.fake = FakeBinance(h1=self.h1, m15=self.m15)
        if network == "testnet":
            self.fake.info = exchange_info(step="0.0001", min_qty="0.0001")
        self.cfg = {"enabled": True, "trading_mode": "LIVE" if live else "DRY_RUN", "network": network, **cfg}
        self.notes = []
        self.creds = creds
        self.main_network = main_network
        self.tmp_path = tmp_path
        self.service = self.build()

    def build(self):
        async def no_sleep(_):
            return None

        return BtcEmaPullbackService(
            config_getter=lambda: self.cfg,
            credentials_getter=lambda n: {"api_key": "k", "secret_key": "s"} if self.creds else {},
            exchange_factory=lambda n, c: self.fake,
            runtime_dir=self.tmp_path,
            notifier=self.notes.append,
            main_network_getter=lambda: self.main_network,
            clock=lambda: self.now_ms / 1000,
            sleep=no_sleep,
            environ={},
        )

    def run(self):
        return asyncio.run(self.service.run_cycle())

    def state(self, mode="LIVE"):
        return self.service.load_state(self.cfg["network"], mode)

    def entries(self):
        return [o for o in self.fake.orders if not str(o["clientOrderId"]).startswith("btcpb-btcusdt-l-close")
                and "close" not in str(o["clientOrderId"])]


def test_live_entry_places_closeposition_sl_and_tp_from_actual_fill(tmp_path):
    h = Harness(tmp_path)
    result = h.run()
    assert result["action"] == "entered", result
    trade = h.state()["trade"]
    assert h.state()["phase"] == "POSITION_OPEN"
    assert trade["entry_price"] == "85000.0"
    assert Decimal(trade["quantity"]) > Decimal("0.01")  # testnet: risk-based size
    kinds = {a["type"]: a for a in h.fake.algo_attempts}
    assert kinds["STOP_MARKET"]["closePosition"] == "true" and "quantity" not in kinds["STOP_MARKET"]
    assert kinds["STOP_MARKET"]["workingType"] == "MARK_PRICE"
    assert kinds["STOP_MARKET"]["side"] == "SELL" and kinds["TAKE_PROFIT_MARKET"]["side"] == "SELL"
    assert kinds["STOP_MARKET"]["triggerPrice"] == "84320.0"
    assert kinds["TAKE_PROFIT_MARKET"]["triggerPrice"] == "86360.0"
    assert h.fake.leverage == 3 and h.fake.margin_type == "isolated"
    events = [e["event"] for e in h.service.ledger("testnet").recent_events(50)]
    assert {"SIGNAL", "ORDER", "PROTECTION", "ACCOUNT_SETUP"} <= set(events)


def test_mainnet_uses_fixed_base_quantity(tmp_path):
    h = Harness(tmp_path, network="mainnet", main_network="testnet")
    h.fake.wallet = 300.0
    assert h.run()["action"] == "entered"
    assert h.state()["trade"]["quantity"] == "0.001"


def test_dry_run_never_sends_orders_and_simulates_exit(tmp_path):
    h = Harness(tmp_path, live=False)
    assert h.run()["action"] == "entered"
    assert h.fake.orders == [] and h.fake.algo_attempts == []
    trade = h.state("DRY_RUN")["trade"]
    entry = float(trade["entry_price"])
    tp = float(trade["take_profit_price"])
    h.now_ms += 5 * M1
    h.fake.m1 = [[trade["entry_ms"] + M1, entry, tp + 10, entry - 1, tp, 1.0]]
    result = h.run()
    assert result["action"] == "exited" and result["trade"]["exit_reason"] == "TP"
    assert float(result["trade"]["net_pnl"]) < float(result["trade"]["gross_pnl"])  # fees
    assert h.fake.orders == []


def test_dry_run_same_candle_hits_assume_stop_first(tmp_path):
    h = Harness(tmp_path, live=False)
    h.run()
    trade = h.state("DRY_RUN")["trade"]
    h.now_ms += 5 * M1
    h.fake.m1 = [[trade["entry_ms"] + M1, 85_000, 99_000, 70_000, 85_000, 1.0]]
    assert h.run()["trade"]["exit_reason"] == "SL"


def test_existing_position_blocks_new_entry(tmp_path):
    h = Harness(tmp_path)
    h.cfg["enabled"] = False
    h.run()  # reconcile with a flat account
    h.cfg["enabled"] = True
    h.fake.position = {"side": "long", "contracts": 0.002, "entryPrice": 84_000}
    h.service.load_state("testnet", "LIVE")
    result = asyncio.run(h.service.maybe_enter("testnet", h.service.config(), "LIVE", h.state()))
    assert result["reason"].startswith("POSITION_EXISTS")
    assert h.fake.orders == []


def test_same_candle_never_orders_twice_even_after_restart(tmp_path):
    h = Harness(tmp_path)
    h.run()
    assert len(h.fake.orders) == 1
    h.fake.position = None  # pretend it closed instantly
    for algo in h.fake.algos.values():
        algo["algoStatus"] = "CANCELED"
    h.run()  # finalize
    assert h.run()["reason"] == "WAITING_NEXT_15M_CLOSE"
    h.service = h.build()  # restart
    state = h.state()
    state["last_evaluated_bar"] = 0  # even if the bar cursor is lost
    h.service.save_state("testnet", "LIVE", state)
    assert h.run()["reason"] == "DUPLICATE_SIGNAL"
    assert len(h.fake.orders) == 1


def test_partial_fill_protects_the_actual_quantity(tmp_path):
    h = Harness(tmp_path)
    h.fake.fill_ratio = 0.5
    assert h.run()["action"] == "entered"
    trade = h.state()["trade"]
    assert "PARTIAL_FILL" in trade["rule_violation"]
    assert float(trade["quantity"]) == pytest.approx(h.fake.position["contracts"])
    assert any(a["type"] == "STOP_MARKET" for a in h.fake.algo_attempts)


def test_sl_registration_failure_triggers_failsafe_market_close(tmp_path):
    h = Harness(tmp_path)
    h.fake.fail_sl_creates = 99
    result = h.run()
    assert result["action"] == "exited"
    assert result["trade"]["exit_reason"] == "FAILSAFE"
    assert h.fake.position is None
    closes = [o for o in h.fake.orders if "close" in o["clientOrderId"]]
    assert len(closes) == 1
    assert any("🚨" in n for n in h.notes)
    assert h.state()["phase"] == "IDLE"


def test_breached_stop_is_closed_immediately(tmp_path):
    h = Harness(tmp_path)
    h.fake.breach_sl = True
    assert h.run()["trade"]["exit_reason"] == "FAILSAFE"


def test_tp_failure_keeps_stop_and_retries_tp(tmp_path):
    h = Harness(tmp_path)
    h.fake.fail_tp_creates = 3
    h.run()
    trade = h.state()["trade"]
    assert trade["tp_missing"] is True and h.state()["phase"] == "POSITION_OPEN"
    h.now_ms += 70_000
    h.run()
    assert h.state()["trade"]["tp_missing"] is False
    assert any(o["orderType"] == "TAKE_PROFIT_MARKET" and o["algoStatus"] == "NEW" for o in h.fake.algos.values())


def test_tp_exit_cancels_the_remaining_stop_and_records_net_pnl(tmp_path):
    h = Harness(tmp_path)
    h.run()
    trade = h.state()["trade"]
    h.fake.income = [{"income": "-0.25"}]
    h.fake.trigger("TAKE_PROFIT_MARKET", float(trade["take_profit_price"]))
    h.now_ms += 60_000
    result = h.run()
    closed = result["trade"]
    assert closed["exit_reason"] == "TP"
    qty, entry, exit_ = float(closed["quantity"]), float(closed["entry_price"]), float(closed["exit_price"])
    gross = (exit_ - entry) * qty
    commission = (entry + exit_) * qty * 0.0004
    assert float(closed["gross_pnl"]) == pytest.approx(gross)
    assert float(closed["commission"]) == pytest.approx(commission)
    assert float(closed["funding"]) == pytest.approx(-0.25)
    assert float(closed["net_pnl"]) == pytest.approx(gross - commission - 0.25)
    assert float(closed["r_multiple"]) == pytest.approx(float(closed["net_pnl"]) / float(trade["initial_risk"]))
    assert all(o["algoStatus"] != "NEW" for o in h.fake.algos.values())
    assert compute_stats(h.service.ledger("testnet").trades(mode="LIVE"))["wins"] == 1


def test_restart_adopts_unprotected_position_and_protects_it(tmp_path):
    h = Harness(tmp_path)
    h.cfg["enabled"] = False
    h.fake.position = {"side": "short", "contracts": 0.01, "entryPrice": 85_000}
    h.run()
    trade = h.state()["trade"]
    assert trade["side"] == "SHORT" and trade["rule_violation"] == "ADOPTED_UNTRACKED_POSITION"
    stops = [o for o in h.fake.algos.values() if o["orderType"] == "STOP_MARKET"]
    assert stops and stops[0]["side"] == "BUY" and stops[0]["triggerPrice"] == "85680.0"


def test_restart_finalizes_a_trade_closed_while_offline(tmp_path):
    h = Harness(tmp_path)
    h.run()
    h.fake.trigger("STOP_MARKET", float(h.state()["trade"]["stop_price"]))
    h.service = h.build()  # restart: state says POSITION_OPEN, exchange is flat
    state = h.state()
    state["reconciled"] = False
    h.service.save_state("testnet", "LIVE", state)
    h.now_ms += 60_000
    h.run()
    closed = h.service.ledger("testnet").trades(mode="LIVE", status="CLOSED")
    assert closed and closed[-1]["exit_reason"] == "SL" and float(closed[-1]["net_pnl"]) < 0
    assert all(o["algoStatus"] != "NEW" for o in h.fake.algos.values())


def test_invalid_api_responses_skip_safely(tmp_path):
    h = Harness(tmp_path)
    h.fake.info = {"symbols": [{"symbol": "BTCUSDT", "filters": []}]}
    result = h.run()
    assert result["reason"].startswith("TRADING_RULES_UNAVAILABLE")
    assert h.fake.orders == []
    h2 = Harness(tmp_path / "b")
    h2.fake.fetch_positions = lambda symbols=None: {"not": "a list"}
    assert h2.run()["reason"] == "RECONCILIATION_PENDING"
    assert h2.fake.orders == []
    events = [e["event"] for e in h2.service.ledger("testnet").recent_events(20)]
    assert "RECONCILE_FAILED" in events


def test_market_data_disconnect_blocks_entry_but_keeps_protection(tmp_path):
    h = Harness(tmp_path)
    h.fake.raise_klines = True
    result = h.run()
    assert result["reason"].startswith("MARKET_DATA_UNAVAILABLE") and h.fake.orders == []
    h.fake.raise_klines = False
    h.run()
    h.fake.raise_positions = True
    result = h.run()
    assert result["reason"] == "POSITION_UNKNOWN"
    assert h.state()["trade"] is not None  # not finalized on an outage


def test_live_on_main_bot_account_is_refused(tmp_path):
    h = Harness(tmp_path, main_network="testnet")
    result = h.run()
    assert result["reason"].startswith("SHARED_ACCOUNT_WITH_MAIN_BOT")
    assert h.fake.orders == []


def test_live_without_api_keys_is_blocked(tmp_path):
    h = Harness(tmp_path, creds=False)
    assert h.run()["reason"] == "API_KEYS_MISSING"


def test_hedge_mode_account_is_refused(tmp_path):
    h = Harness(tmp_path)
    h.fake.dual = True
    assert h.run()["reason"].startswith("POSITION_MODE_HEDGE")


def test_daily_limit_blocks_in_service(tmp_path):
    h = Harness(tmp_path, live=False)
    ledger = h.service.ledger("testnet")
    now = datetime.fromtimestamp(h.now_ms / 1000, timezone.utc)
    for i in range(3):
        ledger.upsert_trade({"trade_id": f"t{i}", "mode": "DRY_RUN", "network": "testnet", "status": "CLOSED",
                             "entry_time": (now - timedelta(minutes=10 + i)).isoformat(), "net_pnl": "1"})
    result = h.run()
    assert result["reason"].startswith("DAILY_TRADE_LIMIT")


def test_manual_close_and_stats(tmp_path):
    h = Harness(tmp_path)
    h.run()
    result = asyncio.run(h.service.close_position())
    assert result["action"] == "exited" and result["trade"]["exit_reason"] == "MANUAL"
    stats = compute_stats(h.service.ledger("testnet").trades(mode="LIVE"))
    for key in ("win_rate", "profit_factor", "expectancy", "average_r", "max_drawdown", "max_consecutive_losses"):
        assert key in stats


# ----------------------------------------------------------------- Telegram
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


def _controller(tmp_path, options=None):
    from bot_runtime.controller_btc_pullback import ControllerBtcPullbackMixin

    controller = ControllerBtcPullbackMixin()
    controller.cfg = _Cfg({"btc_ema_pullback": dict(options or {})})
    h = Harness(tmp_path, live=False)
    h.cfg = controller.cfg.data["btc_ema_pullback"]
    controller.btc_pullback_service = h.build()
    return controller, h


def _callbacks(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def test_telegram_menu_confirmations(tmp_path):
    controller, _ = _controller(tmp_path)
    press = lambda a: asyncio.run(controller._handle_btc_pullback_action(a))  # noqa: E731
    text, markup = press("status")
    assert "BTC EMA 눌림목" in text and "모드: DRY_RUN" in text
    _, markup = press("on")
    assert "bp:confirm_on" in _callbacks(markup)
    assert controller.cfg.data["btc_ema_pullback"].get("enabled") is not True
    press("confirm_on")
    assert controller.cfg.data["btc_ema_pullback"]["enabled"] is True
    _, markup = press("mode:live")
    assert "bp:confirm_live" in _callbacks(markup)
    assert controller.cfg.data["btc_ema_pullback"].get("trading_mode") != "LIVE"
    press("confirm_live")
    assert controller.cfg.data["btc_ema_pullback"]["trading_mode"] == "LIVE"
    _, markup = press("net:mainnet")
    assert "bp:confirm_mainnet" in _callbacks(markup)
    text, _ = press("rules")
    assert "보장" in text and "guaranteed" not in text.lower()
    text, _ = press("stats")
    assert "기대값" in text


def test_main_keyboard_has_btcpullback_button():
    import emas

    controller = emas.MainController.__new__(emas.MainController)
    labels = [b.text for row in controller._build_main_keyboard().keyboard for b in row]
    assert "/btcpullback" in labels


def test_config_defaults_are_registered():
    import emas

    config = emas.TradingConfig.__new__(emas.TradingConfig)
    config.config = {}
    config.config_file = "unused.json"
    config.save_config_sync = lambda: None
    config._ensure_defaults()
    assert config.config["btc_ema_pullback"]["trading_mode"] == "DRY_RUN"
    assert config.config["btc_ema_pullback"]["enabled"] is False
