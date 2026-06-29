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


def _sample_long_short_status_lines():
    long_lines = [
        "LONG: 대기",
        "필수 게이트",
        "🔴 불만족 1. UTBot 방향: 현재 SHORT / bias SHORT",
        "🟢 만족 2. 방향 필터: EV Adaptive 통합 방향 판정 사용",
        "🟢 만족 3. 선택 Set 필터: 선택 Set 추가 필터 없음",
        "🟡 축소 4. 시장 품질: REDUCE x0.25",
        "🔴 불만족 5. EV Adaptive 기대값: NO_TRADE score 33.4: stale continuation 112>10, MTF 1/3, score 33.4<60",
        "🟢 만족 6. 일일 리스크: PnL 0.00 / trades 0/7",
        "🟡 대기 7. ATR 손절/RR/수량: 리스크 예산 없음",
        "참고/감액",
        "🟢 만족 - Feature Score: 33.4 / sample",
    ]
    short_lines = [
        "SHORT: 조건통과 / 주문 안함",
        "필수 게이트",
        "🟢 만족 1. UTBot 방향: SHORT bias_state",
        "🟢 만족 2. 방향 필터: EV Adaptive 통합 방향 판정 사용",
        "🟢 만족 3. 선택 Set 필터: 선택 Set 추가 필터 없음",
        "🟡 축소 4. 시장 품질: REDUCE x0.25",
        "🟢 만족 5. EV Adaptive 기대값: GO score 66.2 net=0.410R",
        "🟢 만족 6. 일일 리스크: PnL 0.00 / trades 0/7",
        "🟢 만족 7. ATR 손절/RR/수량: qty 12.0 risk 1.10",
        "참고/감액",
        "🟢 만족 - Feature Score: 66.2 / sample",
    ]
    return long_lines, short_lines


def test_utbreakout_status_shows_active_short_required_gates_in_visible_body():
    engine = _build_trace_engine()
    long_lines, short_lines = _sample_long_short_status_lines()
    compact_long = engine._compact_side_gate_summary("long", False, long_lines)
    compact_short = engine._compact_side_gate_summary("short", True, short_lines)

    visible_lines = [
        "실행 게이트",
        "LONG: 차단 - UTBot 방향 불일치: 현재 SHORT / bias SHORT",
        "SHORT: 차단 - 상태조회 진단용: 이 심볼은 현재 live 후보 아님",
        "",
        "요약 신호등",
        compact_long,
        compact_short,
        "",
        *engine._build_utbreakout_active_side_preview_lines(
            "short",
            long_lines,
            short_lines,
            compact_long,
            compact_short,
        ),
    ]
    visible_body = "\n".join(visible_lines)

    assert visible_body.index("요약 신호등") < visible_body.index("활성 후보 상세: SHORT")
    inactive_index = visible_body.index("비활성 방향 요약")
    assert visible_body.index("SHORT: 조건통과 / 주문 안함") < inactive_index
    assert inactive_index < visible_body.index("LONG: 🔴", inactive_index)
    for index in range(1, 8):
        assert f"{index}. " in visible_body
    assert "참고/감액" not in visible_body.split("비활성 방향 요약", 1)[0]


def test_utbreakout_status_shows_active_long_required_gates_in_visible_body():
    engine = _build_trace_engine()
    long_lines, short_lines = _sample_long_short_status_lines()
    compact_long = engine._compact_side_gate_summary("long", False, long_lines)
    compact_short = engine._compact_side_gate_summary("short", True, short_lines)

    visible_body = "\n".join(
        engine._build_utbreakout_active_side_preview_lines(
            "long",
            long_lines,
            short_lines,
            compact_long,
            compact_short,
        )
    )

    assert visible_body.startswith("활성 후보 상세: LONG\nLONG: 대기")
    assert visible_body.index("LONG: 대기") < visible_body.index("SHORT: 🟢")
    for index in range(1, 8):
        assert f"{index}. " in visible_body


def test_utbreakout_status_full_file_still_contains_both_long_and_short_details():
    engine = _build_trace_engine()
    long_lines, short_lines = _sample_long_short_status_lines()

    short_first_full_text = "\n".join(
        engine._ordered_utbreakout_side_detail_lines("short", long_lines, short_lines)
    )
    long_first_full_text = "\n".join(
        engine._ordered_utbreakout_side_detail_lines("long", long_lines, short_lines)
    )
    default_full_text = "\n".join(
        engine._ordered_utbreakout_side_detail_lines(None, long_lines, short_lines)
    )

    assert "LONG: 대기" in short_first_full_text
    assert "SHORT: 조건통과 / 주문 안함" in short_first_full_text
    assert short_first_full_text.index("SHORT: 조건통과 / 주문 안함") < short_first_full_text.index("LONG: 대기")
    assert long_first_full_text.index("LONG: 대기") < long_first_full_text.index("SHORT: 조건통과 / 주문 안함")
    assert default_full_text.index("LONG: 대기") < default_full_text.index("SHORT: 조건통과 / 주문 안함")


def test_execution_gate_display_prioritizes_root_blockers_over_downstream_noise():
    engine = _build_trace_engine()
    long_lines, short_lines = _sample_long_short_status_lines()

    ev_blockers = engine._format_utbreakout_execution_blockers_for_display(
        "long",
        long_lines,
        {
            "can_attempt": False,
            "blockers": [
                "side condition failed",
                "risk plan blocked",
                "planned quantity zero",
                "planned risk zero",
                "ready entry plan missing",
            ],
        },
    )
    direction_blockers = engine._format_utbreakout_execution_blockers_for_display(
        "short",
        [
            "SHORT: 대기",
            "필수 게이트",
            "🔴 불만족 1. UTBot 방향: 현재 LONG",
            *short_lines[3:],
        ],
        {
            "can_attempt": False,
            "blockers": [
                "direction mismatch",
                "side condition failed",
                "planned quantity zero",
                "planned risk zero",
                "ready entry plan missing",
            ],
        },
    )

    assert ev_blockers == [
        "UTBot 방향 불일치: 현재 SHORT / bias SHORT",
        "EV Adaptive NO_TRADE: score 33.4: stale continuation 112>10, MTF 1/3, score 33.4<60",
        "risk plan blocked",
    ]
    assert direction_blockers == ["UTBot 방향 불일치: 현재 LONG"]

    trade_direction_blockers = engine._format_utbreakout_execution_blockers_for_display(
        "short",
        short_lines,
        {
            "can_attempt": False,
            "blockers": [
                "방향 필터 차단 (SHORT vs LONG)",
                "ready entry plan missing",
            ],
        },
    )

    assert "방향 필터 차단 (SHORT vs LONG)" in trade_direction_blockers

    fallback_blockers = engine._format_utbreakout_execution_blockers_for_display(
        "short",
        short_lines,
        {
            "can_attempt": False,
            "blockers": [
                "status screen only; no live scanner candidate for this symbol",
            ],
        },
    )
    live_status_blockers = engine._format_utbreakout_execution_blockers_for_display(
        "short",
        short_lines,
        {
            "can_attempt": False,
            "blockers": [
                "status screen only; live scanner candidate, scanner loop must emit STATUS_READY",
            ],
        },
    )

    assert fallback_blockers == ["상태조회 진단용: 이 심볼은 현재 live 후보 아님"]
    assert live_status_blockers == ["상태조회 진단용: 실제 주문은 live scanner 루프가 시도"]


def test_position_scan_context_identifies_watchlist_fallback_reference_symbol():
    engine = _build_trace_engine()
    engine.utbreakout_status_symbol_source = "watchlist_fallback_no_live_candidate"

    async def get_active_position_symbols(use_cache=False):
        return set()

    async def resolve_next_utbreakout_scan_candidate(excluded_symbols=None):
        return None, None

    engine.get_active_position_symbols = get_active_position_symbols
    engine._resolve_next_utbreakout_scan_candidate = resolve_next_utbreakout_scan_candidate
    engine._get_coin_selector_config = lambda: {"enabled": True}

    lines = asyncio.run(
        engine._build_utbreakout_position_scan_context_lines("DOGE/USDT:USDT")
    )

    assert "현재 포지션: 없음" in lines
    assert "현재 live 후보: 없음" in lines
    assert any(
        "상태 화면 참고 심볼: DOGE/USDT:USDT" in line
        and "watchlist 첫 항목" in line
        for line in lines
    )
