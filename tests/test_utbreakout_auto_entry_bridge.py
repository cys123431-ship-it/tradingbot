import asyncio

import emas


def _build_engine():
    engine = object.__new__(emas.SignalEngine)
    engine.utbreakout_entry_trace = {}
    engine.utbreakout_last_ready_ts = {}
    engine.utbreakout_last_ready_side = {}
    engine.utbreakout_last_order_attempt_ts = {}
    engine.utbreakout_last_watchdog_report_ts = {}
    engine.utbreakout_trace_watchdog_enabled = True
    engine.utbot_filtered_breakout_entry_plans = {}
    engine.last_utbot_filtered_breakout_status = {}
    engine.get_runtime_strategy_params = lambda: {
        "active_strategy": emas.UTBOT_FILTERED_BREAKOUT_STRATEGY,
    }
    engine._get_utbot_filtered_breakout_config = lambda params: {
        "effective_profile_version": emas.UTBREAKOUT_EFFECTIVE_PROFILE_VERSION,
        "utbreakout_auto_entry_bridge_cooldown_sec": 60.0,
        "utbreakout_auto_entry_bridge_max_ready_age_sec": 180.0,
    }
    return engine


def test_auto_entry_bridge_state_helpers_exist():
    engine = _build_engine()

    engine._ensure_utbreakout_auto_entry_bridge_state()

    assert engine.utbreakout_auto_entry_bridge_last_attempt_ts == {}
    assert engine.utbreakout_auto_entry_bridge_enabled is True


def test_auto_entry_bridge_calls_entry_for_recent_ready_plan():
    notifications = []
    entry_calls = []

    class Controller:
        async def notify(self, text):
            notifications.append(text)

    engine = _build_engine()
    engine.ctrl = Controller()
    symbol = "SOL/USDT:USDT"

    async def get_server_position(requested_symbol, use_cache=False):
        assert requested_symbol == symbol
        return None

    async def entry(requested_symbol, side, price):
        entry_calls.append((requested_symbol, side, price))

    async def mock_evaluate_eligibility(requested_symbol, **kwargs):
        return {
            'ok_market': True,
            'symbol': requested_symbol,
            'long_ok': True,
            'short_ok': True,
            'long_eligibility': {'can_attempt': True, 'blockers': []},
            'short_eligibility': {'can_attempt': True, 'blockers': []},
            'long_lines': [],
            'short_lines': [],
            'ut_reason': 'mocked',
        }

    engine.get_server_position = get_server_position
    engine.entry = entry
    engine._evaluate_utbreakout_eligibility_context = mock_evaluate_eligibility
    engine._set_utbot_filtered_breakout_entry_plan(
        symbol,
        {
            "side": "long",
            "entry_price": 101.25,
            "qty": 0.5,
            "planned_notional": 50.625,
            "planned_margin": 10.125,
            "risk_usdt": 1.0,
        },
    )
    engine._utbreakout_trace_event(
        symbol,
        "STATUS_READY",
        "READY",
        side="long",
        entry_price=101.25,
    )

    called = asyncio.run(
        engine._maybe_run_utbreakout_auto_entry_bridge(
            symbol,
            source="scanner_seen",
        )
    )
    assert called is True
    assert entry_calls == [(symbol, "long", 101.25)]
    assert any("Auto Entry Bridge" in text for text in notifications)
    stages = [
        event["stage"]
        for event in engine._utbreakout_recent_trace_events(symbol, limit=20)
    ]
    assert "AUTO_ENTRY_BRIDGE" in stages
    assert "ENTRY_CALL" in stages


def test_auto_entry_bridge_restricted_symbol_is_blocked():
    entry_calls = []

    class Controller:
        async def notify(self, text):
            pass

    engine = _build_engine()
    engine.ctrl = Controller()

    async def entry(*args):
        entry_calls.append(args)

    engine.entry = entry

    called = asyncio.run(
        engine._maybe_run_utbreakout_auto_entry_bridge(
            "SAMSUNGUSDT",
            source="scanner_seen",
        )
    )

    assert called is False
    assert entry_calls == []
    events = engine._utbreakout_recent_trace_events(
        "SAMSUNG/USDT:USDT",
        limit=20,
    )
    assert any(
        event["stage"] == "SYMBOL_BLOCKED_REGION_RESTRICTED"
        for event in events
    )
    assert any(
        event["stage"] == "AUTO_ENTRY_BRIDGE_BLOCKED"
        for event in events
    )


def test_bridge_stage_names_and_diagnosis_are_in_report():
    engine = _build_engine()
    symbol = "SOL/USDT:USDT"
    engine._utbreakout_trace_event(
        symbol,
        "STATUS_READY",
        "READY",
        side="long",
    )
    engine._utbreakout_trace_event(
        symbol,
        "AUTO_ENTRY_BRIDGE",
        "CALL_ENTRY",
        side="long",
    )

    report = engine._format_utbreakout_trace_report(symbol, full=True)

    assert "AUTO_ENTRY_BRIDGE" in report
    assert "AUTO_ENTRY_BRIDGE_BLOCKED" in report
    assert "AUTO_ENTRY_BRIDGE 이후 entry() 호출 전" in report


def test_coin_selector_scanner_invokes_bridge_after_scanner_seen():
    bridge_calls = []

    class Controller:
        is_paused = False

    class MarketDataExchange:
        def fetch_ohlcv(self, symbol, timeframe, limit=300):
            return [
                [index * 900_000, 100.0, 101.0, 99.0, 100.5, 1000.0]
                for index in range(300)
            ]

    engine = _build_engine()
    engine.ctrl = Controller()
    engine.market_data_exchange = MarketDataExchange()
    engine.coin_selector_candidate_cooldowns = {}
    engine.last_entry_reason = {}
    engine.scanner_active_symbol = None
    engine._get_coin_selector_config = lambda: {
        "top_n": 10,
        "candidate_cooldown_enabled": False,
    }
    engine.get_runtime_trade_config = lambda: {
        "strategy_params": engine.get_runtime_strategy_params(),
    }
    engine.get_runtime_common_settings = lambda: {
        "scanner_timeframe": "15m",
    }
    engine._micro_auto_enabled = lambda: False

    async def evaluate_coin_selector(force=False):
        return {
            "selected": [{
                "exchange_symbol": "SOL/USDT:USDT",
                "normalized_symbol": "SOL/USDT",
                "selection_state": "SELECTED",
                "score": 90.0,
                "quote_volume": 1_000_000.0,
                "auto_set_id": 22,
                "adaptive_tf": "15m",
            }]
        }

    async def bridge(symbol, source="scanner"):
        bridge_calls.append((symbol, source))
        return True

    async def calculate_strategy_signal(*args, **kwargs):
        assert kwargs["force_utbreakout_reprocess"] is True
        engine._set_utbot_filtered_breakout_entry_plan(
            "SOL/USDT:USDT",
            {
                "side": "long",
                "entry_price": 100.5,
                "qty": 0.5,
                "planned_notional": 50.25,
                "planned_margin": 10.05,
                "risk_usdt": 1.0,
            },
        )
        return "long", True, False, "UTBreakout", "utbreakout", False

    async def get_server_position(symbol, use_cache=False):
        return {"side": "long", "contracts": 0.5}

    engine.evaluate_coin_selector = evaluate_coin_selector
    engine._collect_primary_strategy_context = lambda *args, **kwargs: {
        "precomputed": {},
    }
    engine._calculate_strategy_signal = calculate_strategy_signal
    engine._utbreakout_diag_for_symbol = lambda symbol: {
        "accepted_side": "long",
        "reason": "accepted",
    }
    engine._maybe_run_utbreakout_auto_entry_bridge = bridge
    engine.get_server_position = get_server_position

    asyncio.run(engine._scan_and_trade_coin_selector())

    assert bridge_calls == [("SOL/USDT:USDT", "scanner_seen")]
    assert engine.scanner_active_symbol == "SOL/USDT:USDT"
    stages = [
        event["stage"]
        for event in engine._utbreakout_recent_trace_events(
            "SOL/USDT:USDT",
            limit=20,
        )
    ]
    assert "SCANNER_SEEN" in stages
    assert "STATUS_READY" in stages
    assert "POSITION_CONFIRMED" in stages


def test_live_scanner_records_status_ready_from_accepted_diag_and_plan():
    engine = _build_engine()
    symbol = "SOL/USDT:USDT"
    engine.last_utbot_filtered_breakout_status = {
        symbol: {
            "accepted_side": "short",
            "candidate_type": "fresh_signal",
            "decision_candle_ts": 1234567890,
            "entry_timeframe": "15m",
            "effective_profile_version": "ev_adaptive_v2",
            "auto_selected_set_id": 64,
        }
    }
    engine._set_utbot_filtered_breakout_entry_plan(
        symbol,
        {
            "side": "short",
            "entry_price": 98.5,
            "qty": 0.4,
            "planned_notional": 39.4,
            "planned_margin": 7.88,
            "risk_usdt": 1.2,
        },
    )

    ready_side = engine._record_utbreakout_live_ready_from_diag(
        symbol,
        source="scanner_seen",
        scan_tf="15m",
    )

    assert ready_side == "short"
    events = engine._utbreakout_recent_trace_events(symbol, limit=20)
    ready_events = [event for event in events if event["stage"] == "STATUS_READY"]
    assert ready_events
    assert ready_events[-1]["data"]["side"] == "short"
    assert ready_events[-1]["data"]["source"] == "scanner_seen"


def test_auto_entry_bridge_does_not_require_manual_status_when_diag_plan_ready():
    entry_calls = []

    class Controller:
        async def notify(self, text):
            pass

    engine = _build_engine()
    engine.ctrl = Controller()
    symbol = "SOL/USDT:USDT"
    engine.last_utbot_filtered_breakout_status = {
        symbol: {
            "accepted_side": "short",
            "candidate_type": "fresh_signal",
            "decision_candle_ts": 1234567890,
        }
    }
    engine._set_utbot_filtered_breakout_entry_plan(
        symbol,
        {
            "side": "short",
            "entry_price": 98.5,
            "qty": 0.4,
            "planned_notional": 39.4,
            "planned_margin": 7.88,
            "risk_usdt": 1.2,
        },
    )

    async def mock_evaluate_eligibility(requested_symbol, **kwargs):
        assert kwargs["is_live_scanner_context"] is True
        return {
            "ok_market": True,
            "symbol": requested_symbol,
            "long_eligibility": {"can_attempt": False, "blockers": ["direction mismatch"]},
            "short_eligibility": {"can_attempt": True, "blockers": []},
        }

    async def entry(requested_symbol, side, price):
        entry_calls.append((requested_symbol, side, price))

    engine._evaluate_utbreakout_eligibility_context = mock_evaluate_eligibility
    engine.entry = entry

    called = asyncio.run(
        engine._maybe_run_utbreakout_auto_entry_bridge(
            symbol,
            source="scanner_seen",
        )
    )

    assert called is True
    assert entry_calls == [(symbol, "short", 98.5)]
    stages = [
        event["stage"]
        for event in engine._utbreakout_recent_trace_events(symbol, limit=20)
    ]
    assert "STATUS_READY" in stages
    assert "AUTO_ENTRY_BRIDGE" in stages
    assert "ENTRY_CALL" in stages


def test_auto_entry_bridge_does_not_synthesize_ready_for_manual_source():
    entry_calls = []

    class Controller:
        async def notify(self, text):
            pass

    engine = _build_engine()
    engine.ctrl = Controller()
    symbol = "SOL/USDT:USDT"
    engine.last_utbot_filtered_breakout_status = {
        symbol: {"accepted_side": "long", "candidate_type": "fresh_signal"}
    }
    engine._set_utbot_filtered_breakout_entry_plan(
        symbol,
        {
            "side": "long",
            "entry_price": 101.25,
            "qty": 0.5,
            "planned_notional": 50.625,
            "planned_margin": 10.125,
            "risk_usdt": 1.0,
        },
    )

    async def entry(*args):
        entry_calls.append(args)

    engine.entry = entry

    called = asyncio.run(
        engine._maybe_run_utbreakout_auto_entry_bridge(
            symbol,
            source="manual_status",
        )
    )

    assert called is False
    assert entry_calls == []
    events = engine._utbreakout_recent_trace_events(symbol, limit=20)
    assert not any(event["stage"] == "STATUS_READY" for event in events)
    assert any(
        event["stage"] == "AUTO_ENTRY_BRIDGE_BLOCKED"
        and event["status"] == "NO_STATUS_READY"
        for event in events
    )


def test_execution_gate_reports_not_current_scanner_candidate():
    class Controller:
        is_paused = False

    engine = _build_engine()
    engine.ctrl = Controller()
    eligibility = engine._build_utbreakout_execution_eligibility(
        symbol="SOL/USDT:USDT",
        side="long",
        candidate_side="long",
        candidate_type="fresh_signal",
        side_condition_ok=True,
        risk_ok=True,
        planned_qty=1.0,
        risk_usdt=1.0,
        entry_plan_detail="ok",
        cooldown_reasons=[],
        has_open_position=False,
        has_other_position=False,
        auto_entry_enabled=True,
        daily_risk_ok=True,
        plan_lookup_ready=True,
        cfg={"utbreakout_require_scanner_candidate_for_auto_entry": True},
        scanner_source="scanner_seen",
        is_live_scanner_context=True,
        is_current_scanner_candidate=False,
        next_scan_symbol="ETH/USDT:USDT",
        evaluated_symbol="SOL/USDT:USDT",
    )

    assert eligibility["can_attempt"] is False
    assert "not current scanner candidate" in eligibility["blockers"]


def test_execution_eligibility_covers_long_and_short_manual_vs_live():
    class Controller:
        is_paused = False

    engine = _build_engine()
    engine.ctrl = Controller()

    for side in ("long", "short"):
        live = engine._build_utbreakout_execution_eligibility(
            symbol="SOL/USDT:USDT",
            side=side,
            candidate_side=side,
            candidate_type="fresh_signal",
            side_condition_ok=True,
            risk_ok=True,
            planned_qty=1.0,
            risk_usdt=1.0,
            entry_plan_detail="ok",
            cooldown_reasons=[],
            has_open_position=False,
            has_other_position=False,
            auto_entry_enabled=True,
            daily_risk_ok=True,
            plan_lookup_ready=True,
            cfg={"utbreakout_require_scanner_candidate_for_auto_entry": True},
            scanner_source="scanner_seen",
            is_live_scanner_context=True,
            is_current_scanner_candidate=True,
            is_coinselector_top_candidate=True,
            next_scan_symbol="SOL/USDT:USDT",
            evaluated_symbol="SOL/USDT:USDT",
        )
        manual = engine._build_utbreakout_execution_eligibility(
            symbol="SOL/USDT:USDT",
            side=side,
            candidate_side=side,
            candidate_type="fresh_signal",
            side_condition_ok=True,
            risk_ok=True,
            planned_qty=1.0,
            risk_usdt=1.0,
            entry_plan_detail="ok",
            cooldown_reasons=[],
            has_open_position=False,
            has_other_position=False,
            auto_entry_enabled=True,
            daily_risk_ok=True,
            plan_lookup_ready=True,
            cfg={"utbreakout_require_scanner_candidate_for_auto_entry": True},
            scanner_source="manual_status",
            manual_status_only=True,
        )

        assert live["can_attempt"] is True
        assert manual["can_attempt"] is False
        assert "manual status only; live scanner has not selected this symbol" in manual["blockers"]
