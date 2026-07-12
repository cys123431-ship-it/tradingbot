from pathlib import Path

path = Path("emas.py")
text = path.read_text(encoding="utf-8")

replacements = [
    (
        """                        (\n                            f\"   {direction_reason} | 4h {side_4h} / 1d {side_1d} | \"\n                            '주문 없음 | 코드: no_ut_direction'\n                        ),\n""",
        """                        (\n                            f\"   {direction_reason} | 4h {side_4h} / 1d {side_1d} | \"\n                            'UT 방향 확인 후 RSPT 조건 검사 | 주문 없음 | 코드: no_ut_direction'\n                        ),\n""",
    ),
    (
        "def _format_live_real_risk_history(state, *, limit=5):\n",
        "def _format_live_real_risk_history(state, cfg=None, *, limit=5):\n",
    ),
    ('    tz = ZoneInfo("Asia/Seoul")\n', '    _, tz = _live_real_timezone(cfg)\n'),
    (
        'lines.extend(["", "최근 변경 기록:", *_format_live_real_risk_history(state)])',
        'lines.extend(["", "최근 변경 기록:", *_format_live_real_risk_history(state, cfg)])',
    ),
    (
        """        async def _set_live_real_risk_pct_from_user(requested_fraction, *, source):\n            cfg_for_risk = _live_real_risk_cfg()\n            current_fraction = _live_real_risk_fraction_from_cfg(cfg_for_risk)\n            state = load_live_real_risk_state()\n            change_cfg = dict(cfg_for_risk)\n""",
        """        async def _set_live_real_risk_pct_from_user(requested_fraction, *, source):\n            cfg_for_risk = _live_real_risk_cfg()\n            current_fraction = _live_real_risk_fraction_from_cfg(cfg_for_risk)\n            try:\n                state = load_live_real_risk_state()\n            except LiveRealRiskStateUnreadable as exc:\n                return False, (\n                    \"🔴 실거래 리스크 상태 파일을 신뢰할 수 없어 신규 거래와 리스크 변경을 차단했습니다.\\n\"\n                    \"상태 파일을 복구하거나 운영자가 확인한 뒤 다시 시도하세요.\\n\"\n                    f\"오류: {exc}\"\n                )\n            change_cfg = dict(cfg_for_risk)\n""",
    ),
    (
        """        async def _live_real_risk_status_text(*, menu=False, notice=None):\n            cfg_for_risk = _live_real_risk_cfg()\n            state = load_live_real_risk_state()\n            limits, equity_source = await _live_real_risk_limits(cfg_for_risk)\n""",
        """        async def _live_real_risk_status_text(*, menu=False, notice=None):\n            cfg_for_risk = _live_real_risk_cfg()\n            try:\n                state = load_live_real_risk_state()\n            except LiveRealRiskStateUnreadable as exc:\n                return (\n                    \"🔴 실거래 리스크 상태: 거래 차단\\n\"\n                    \"누적 손실 상태 파일을 신뢰할 수 없어 fail-closed로 신규 거래를 막았습니다.\\n\"\n                    \"상태 파일을 복구하거나 운영자가 확인해야 합니다.\\n\"\n                    f\"오류: {exc}\"\n                )\n            limits, equity_source = await _live_real_risk_limits(cfg_for_risk)\n""",
    ),
]

for old, new in replacements:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"correction target mismatch ({count}): {old[:80]!r}")
    text = text.replace(old, new, 1)

path.write_text(text, encoding="utf-8")
