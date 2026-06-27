import asyncio
import ast
from pathlib import Path

import emas


def _build_trace_engine():
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
        "utbreakout_trace_watchdog_wait_sec": 1.0,
        "utbreakout_trace_watchdog_cooldown_sec": 180.0,
    }
    return engine


def test_utbreakout_trace_report_detects_ready_without_entry():
    engine = _build_trace_engine()

    engine._utbreakout_trace_event(
        "SOL/USDT:USDT",
        "STATUS_READY",
        "READY",
        side="long",
        qty=0.6,
    )

    report = engine._format_utbreakout_trace_report(
        "SOL/USDT:USDT",
        full=True,
    )

    assert "UTBreakout Entry Trace Report" in report
    assert "Effective Profile" in report
    assert "STATUS_READY" in report
    assert "ENTRY_CALL" in report
    assert "scanner_seen 여부: False" in report
    assert "order_attempt 여부: False" in report
    assert "suspected_break_stage" in report
    assert "STATUS_READY 이후 AUTO_ENTRY_BRIDGE/ENTRY_CALL 전" in report


def test_no_duplicate_utbreakout_status_builder_definition():
    tree = ast.parse(Path("emas.py").read_text(encoding="utf-8"))
    names = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
    ]

    assert names.count("build_utbreakout_condition_status_text") == 1


def test_manual_status_uses_diagnostic_ready_not_status_ready():
    tree = ast.parse(Path("emas.py").read_text(encoding="utf-8"))
    status_fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "build_utbreakout_condition_status_text"
    )
    emitted_stages = []
    for node in ast.walk(status_fn):
        if not isinstance(node, ast.Call):
            continue
        if not (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "_utbreakout_trace_event"
        ):
            continue
        if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
            emitted_stages.append(node.args[1].value)

    assert "STATUS_DIAGNOSTIC_READY" in emitted_stages
    assert "STATUS_READY" not in emitted_stages


def test_utbreakout_trace_is_bounded_and_symbol_aliases_share_state():
    engine = _build_trace_engine()

    for index in range(205):
        engine._utbreakout_trace_event(
            "SOL/USDT:USDT",
            "POLL_TICK",
            "SEEN",
            index=index,
        )

    events = engine._utbreakout_recent_trace_events("SOLUSDT", limit=300)

    assert len(events) == 200
    assert events[0]["data"]["index"] == 5
    assert events[-1]["data"]["index"] == 204


def test_utbreakout_watchdog_records_without_sending_telegram_after_ready_without_order():
    notifications = []

    class Controller:
        async def notify(self, text):
            notifications.append(text)

    engine = _build_trace_engine()
    engine.ctrl = Controller()
    symbol = "SOL/USDT:USDT"
    engine._utbreakout_trace_event(
        symbol,
        "STATUS_READY",
        "READY",
        side="long",
    )
    key = engine._utbreakout_trace_key(symbol)
    engine.utbreakout_last_ready_ts[key] -= 2.0

    sent = asyncio.run(
        engine._utbreakout_entry_watchdog_check(
            symbol,
            ready_side="long",
            source="test",
        )
    )

    assert sent is True
    assert notifications == []
    events = engine._utbreakout_recent_trace_events(symbol, limit=10)
    assert events[-1]["stage"] == "WATCHDOG"
    assert events[-1]["status"] == "READY_BUT_NO_ORDER_ATTEMPT"


def test_utbreakout_watchdog_does_not_report_after_order_attempt():
    notifications = []

    class Controller:
        async def notify(self, text):
            notifications.append(text)

    engine = _build_trace_engine()
    engine.ctrl = Controller()
    symbol = "SOL/USDT:USDT"
    engine._utbreakout_trace_event(
        symbol,
        "STATUS_READY",
        "READY",
        side="long",
    )
    key = engine._utbreakout_trace_key(symbol)
    engine.utbreakout_last_ready_ts[key] -= 2.0
    engine._utbreakout_trace_event(
        symbol,
        "ORDER_ATTEMPT",
        "SENT",
        side="long",
    )

    sent = asyncio.run(
        engine._utbreakout_entry_watchdog_check(
            symbol,
            ready_side="long",
            source="test",
        )
    )

    assert sent is False
    assert notifications == []


def test_utbreakout_status_includes_compact_long_short_traffic_lights():
    engine = _build_trace_engine()
    
    long_lines = [
        "LONG: 대기",
        "필수 게이트",
        "🟢 만족 1. UTBot 방향: LONG bias_state",
        "🟢 만족 2. 방향 필터: EV Adaptive 통합 방향 판정 사용",
        "🟢 만족 3. 선택 Set 필터: 선택 Set 추가 필터 없음",
        "🟡 축소 4. 시장 품질: REDUCE x0.25",
        "🔴 불만족 5. EV Adaptive 기대값: NO_TRADE score 41.5: MTF alignment 0/3; score 41.5<60.0",
        "🟢 만족 6. 일일 리스크: PnL 0.00 / trades 0/7",
        "🟡 대기 7. ATR 손절/RR/수량: 리스크 예산 없음",
    ]
    short_lines = [
        "SHORT: 대기",
        "필수 게이트",
        "🔴 불만족 1. UTBot 방향: 현재 LONG / bias LONG",
        "🟢 만족 2. 방향 필터: EV Adaptive 통합 방향 판정 사용",
        "🟢 만족 3. 선택 Set 필터: 선택 Set 추가 필터 없음",
        "🟡 축소 4. 시장 품질: REDUCE x0.25",
        "🔴 불만족 5. EV Adaptive 기대값: NO_TRADE score 38.2: no trend or squeeze edge",
        "🟢 만족 6. 일일 리스크: PnL 0.00 / trades 0/7",
        "🟡 대기 7. ATR 손절/RR/수량: 리스크 예산 없음",
    ]
    
    compact_long = engine._compact_side_gate_summary("long", False, long_lines)
    compact_short = engine._compact_side_gate_summary("short", False, short_lines)
    
    assert "LONG: 🟢🟢🟢🟡🔴🟢🟡 | 점수 41.5 | 진입 안함 | 이유: EV Adaptive 기대값: NO_TRADE score 41.5: MTF alignment 0/3; score 41.5<60.0" in compact_long
    assert "SHORT: 🔴🟢🟢🟡🔴🟢🟡 | 점수 38.2 | 진입 안함 | 이유: UTBot 방향: 현재 LONG / bias LONG" in compact_short


def test_compact_side_summary_preserves_short_visibility_before_preview_cutoff():
    engine = _build_trace_engine()
    
    long_lines = [
        "LONG: 대기",
        "필수 게이트",
        "🟢 만족 1. UTBot 방향: LONG bias_state",
        "🟡 축소 2. 시장 품질: REDUCE x0.25",
        "🔴 불만족 3. EV Adaptive 기대값: NO_TRADE score 41.5: MTF alignment 0/3",
    ]
    short_lines = [
        "SHORT: 대기",
        "필수 게이트",
        "🔴 불만족 1. UTBot 방향: 현재 LONG",
        "🟡 축소 2. 시장 품질: REDUCE x0.25",
        "🔴 불만족 3. EV Adaptive 기대값: NO_TRADE score 38.2",
    ]
    
    text = "\n".join([
        "🚦 UT Breakout 조건 스테이터스",
        "최종: LONG 대기 / SHORT 대기",
        "",
        "요약 신호등",
        engine._compact_side_gate_summary("long", False, long_lines),
        engine._compact_side_gate_summary("short", False, short_lines),
        "",
        *long_lines,
        "",
        *short_lines
    ])
    
    preview = emas.MainController._build_telegram_long_text_preview(text, max_lines=55)
    
    assert "요약 신호등" in preview
    assert "LONG: 🟢🟡🔴⚪⚪⚪⚪ | 점수 41.5" in preview
    assert "SHORT: 🔴🟡🔴⚪⚪⚪⚪ | 점수 38.2" in preview
