from __future__ import annotations

import re
from pathlib import Path

EMAS = Path("emas.py")
TEST = Path("tests/test_qh_flow_integration.py")

text = EMAS.read_text(encoding="utf-8")
pattern = r'''    async def build_quad_alpha_status_text\(self, symbol=None\):\n.*?        return '\\n'\.join\(lines\)\n'''
replacement = r'''    async def build_quad_alpha_status_text(self, symbol=None):
        target = self._canonical_futures_symbol(
            symbol or self.current_utbreakout_candidate_symbol or 'BTC/USDT'
        )
        status = dict((getattr(self, 'quad_alpha_last_status', {}) or {}).get(target) or {})
        summary = status.get('quad_alpha') if isinstance(status.get('quad_alpha'), dict) else {}
        if not summary:
            return '\n'.join([
                '🧩 Quad 전략 상태',
                f'Symbol: {target}',
                '',
                '🚦 전략 신호등',
                'UTBreak          ⚪ 미평가',
                'RSPT-v3          ⚪ 미평가',
                'QH-Flow v2       ⚪ 미평가',
                'Crowding Unwind  ⚪ 미평가',
                '',
                '아직 Quad 평가 기록이 없습니다.',
            ])

        def _traffic_view(item):
            item = item if isinstance(item, dict) else {}
            light = str(item.get('light') or 'gray').strip().lower()
            side = str(item.get('side') or '').strip().upper()
            reason = str(item.get('reason') or '-').strip()
            if light == 'green':
                icon = '🟢'
                state = f'유효 {side} 신호' if side else '유효 진입 신호'
                meaning = 'Quad confirmations에 포함'
            elif light == 'red':
                icon = '🔴'
                state = f'{side} 후보 거절' if side else '진입 거절'
                meaning = '안전·품질 필터에서 차단되어 confirmations 제외'
            elif light == 'yellow':
                icon = '🟡'
                state = f'{side} 조건 대기' if side else '조건 대기'
                meaning = '유효 신호가 아직 없어 confirmations 제외'
            else:
                icon = '⚪'
                state = '미평가 또는 데이터 없음'
                meaning = '전략 평가가 완료되지 않아 confirmations 제외'
            return {
                'icon': icon,
                'state': state,
                'meaning': meaning,
                'reason': reason,
                'side': side,
                'light': light,
            }

        strategy_rows = (
            ('utbreak', 'UTBreak'),
            ('rspt', 'RSPT-v3'),
            ('qh_flow', 'QH-Flow v2'),
            ('crowding_unwind', 'Crowding Unwind'),
        )
        traffic = {
            key: _traffic_view(summary.get(key))
            for key, _ in strategy_rows
        }
        green_count = sum(
            1 for item in traffic.values() if item['light'] == 'green'
        )

        lines = [
            '🧩 Quad 전략 상태',
            f'Symbol: {target}',
            '',
            '🚦 전략 신호등',
        ]
        label_width = max(len(label) for _, label in strategy_rows)
        for key, label in strategy_rows:
            item = traffic[key]
            lines.append(f"{label.ljust(label_width)}  {item['icon']} {item['state']}")
        lines.extend([
            f'🟢 유효 신호: {green_count}/4 — 초록불만 confirmations에 포함',
            '',
            f"Agreement: {str(summary.get('agreement_state') or 'none').upper()} / confirmations={int(summary.get('confirmation_count') or 0)} / risk x{float(summary.get('agreement_risk_multiplier', 0.0) or 0.0):.2f}",
            f"Selected: {summary.get('selected_label') or 'NONE'} {str(summary.get('selected_side') or '').upper()}",
            '',
            '📋 전략별 상세 설명',
        ])
        for key, label in strategy_rows:
            item = traffic[key]
            lines.extend([
                f"{item['icon']} {label} — {item['state']}",
                f"  사유: {item['reason']}",
                f"  판정: {item['meaning']}",
            ])
        lines.extend([
            '',
            f"최종 사유: {status.get('reason') or '-'}",
            '',
            '범례: 🟢 유효 신호 | 🟡 조건 대기 | 🔴 후보 거절·충돌 | ⚪ 미평가·데이터 없음',
        ])
        return '\n'.join(lines)
'''
updated, count = re.subn(pattern, lambda match: replacement, text, count=1, flags=re.S)
if count != 1:
    raise SystemExit(f"Quad status function replacement failed: matches={count}")
EMAS.write_text(updated, encoding="utf-8")

test_text = TEST.read_text(encoding="utf-8")
name = "test_quad_status_text_shows_four_traffic_lights_and_details"
if name not in test_text:
    test_text += r'''


def test_quad_status_text_shows_four_traffic_lights_and_details():
    emas = _emas_module()
    engine = object.__new__(emas.SignalEngine)
    symbol = "ALLO/USDT:USDT"
    engine.current_utbreakout_candidate_symbol = symbol
    engine._canonical_futures_symbol = lambda value: value
    engine.quad_alpha_last_status = {
        symbol: {
            "reason": "QUAD_ALPHA waiting (none)",
            "quad_alpha": {
                "agreement_state": "none",
                "agreement_risk_multiplier": 0.0,
                "confirmation_count": 0,
                "selected_label": None,
                "selected_side": None,
                "utbreak": {
                    "light": "red",
                    "side": "short",
                    "reason": "REJECTED_L2_STRESSED",
                },
                "rspt": {
                    "light": "yellow",
                    "side": None,
                    "reason": "trend_filter_failed",
                },
                "qh_flow": {
                    "light": "yellow",
                    "side": None,
                    "reason": "QH signal window expired",
                },
                "crowding_unwind": {
                    "light": "yellow",
                    "side": None,
                    "reason": "crowding_not_extreme",
                },
            },
        }
    }

    text = asyncio.run(engine.build_quad_alpha_status_text(symbol))

    assert "🚦 전략 신호등" in text
    assert "UTBreak" in text and "🔴 SHORT 후보 거절" in text
    assert "RSPT-v3" in text and "🟡 조건 대기" in text
    assert "QH-Flow v2" in text
    assert "Crowding Unwind" in text
    assert "🟢 유효 신호: 0/4" in text
    assert "📋 전략별 상세 설명" in text
    assert "초록불만 confirmations에 포함" in text
    assert "범례: 🟢 유효 신호" in text
'''
    TEST.write_text(test_text, encoding="utf-8")
