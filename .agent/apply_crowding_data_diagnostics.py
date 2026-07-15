from __future__ import annotations

import re
from pathlib import Path

ROOT = Path('.')


def replace_once(path: Path, old: str, new: str, label: str) -> None:
    text = path.read_text(encoding='utf-8')
    if old not in text:
        raise SystemExit(f'{label}: block not found in {path}')
    path.write_text(text.replace(old, new, 1), encoding='utf-8')


def regex_once(path: Path, pattern: str, replacement: str, label: str) -> None:
    text = path.read_text(encoding='utf-8')
    updated, count = re.subn(pattern, lambda _: replacement, text, count=1, flags=re.S)
    if count != 1:
        raise SystemExit(f'{label}: expected 1 match, got {count} in {path}')
    path.write_text(updated, encoding='utf-8')


crowding = ROOT / 'utbreakout/crowding_unwind.py'
regex_once(
    crowding,
    r'def _crowding_side\(.*?\n\n\ndef evaluate_crowding_unwind\(',
    '''def _crowding_side(context: Mapping[str, Any], cfg: Mapping[str, Any]) -> tuple[str | None, dict[str, Any]]:
    funding_raw = context.get("funding_rate")
    funding = _finite(funding_raw)
    percentile_raw = context.get("funding_percentile_30d", context.get("funding_percentile_7d"))
    percentile = _finite(percentile_raw)
    oi_z_raw = context.get("open_interest_delta_z")
    oi_z = _finite(oi_z_raw)
    oi_4h_raw = context.get("open_interest_change_4h")
    oi_4h = _finite(oi_4h_raw)
    long_short_raw = context.get("long_short_ratio")
    long_short = _finite(long_short_raw)

    missing_fields: list[str] = []
    if funding is None:
        missing_fields.append("funding_rate")
    if oi_z is None and oi_4h is None:
        missing_fields.append("open_interest_delta_z|open_interest_change_4h")
    if long_short is None:
        missing_fields.append("long_short_ratio")

    metrics = {
        "funding_rate": funding,
        "funding_percentile": percentile,
        "oi_z": oi_z,
        "oi_change_4h_pct": oi_4h,
        "long_short_ratio": long_short,
        "funding_rate_present": funding is not None,
        "funding_percentile_present": percentile is not None,
        "oi_z_present": oi_z is not None,
        "oi_change_4h_present": oi_4h is not None,
        "long_short_ratio_present": long_short is not None,
        "derivatives_data_ready": not missing_fields,
        "missing_derivatives_fields": missing_fields,
        "oi_extreme": False,
        "long_crowded": False,
        "short_crowded": False,
    }
    if missing_fields:
        return None, metrics

    oi_extreme = (
        (oi_z is not None and oi_z >= float(cfg["oi_z_min"]))
        or (oi_4h is not None and oi_4h >= float(cfg["oi_change_4h_min_pct"]))
    )
    percentile_value = float(percentile or 0.0)
    positive_funding = float(funding) >= float(cfg["funding_abs_min"]) or (
        float(funding) > 0 and percentile_value >= float(cfg["funding_percentile_min"])
    )
    negative_funding = float(funding) <= -float(cfg["funding_abs_min"]) or (
        float(funding) < 0 and percentile_value >= float(cfg["funding_percentile_min"])
    )
    long_crowded = oi_extreme and positive_funding and float(long_short) >= float(cfg["long_short_ratio_high"])
    short_crowded = oi_extreme and negative_funding and float(long_short) <= float(cfg["long_short_ratio_low"])
    side = "short" if long_crowded else "long" if short_crowded else None
    metrics.update({
        "oi_extreme": oi_extreme,
        "long_crowded": long_crowded,
        "short_crowded": short_crowded,
    })
    return side, metrics


def evaluate_crowding_unwind(''',
    'crowding side data readiness',
)
replace_once(
    crowding,
    '''    side, metrics = _crowding_side(context, cfg)
    metrics["l2_gate"] = dict(l2_gate or {})
    if side is None:
        return CrowdingUnwindDecision(reason="crowding_not_extreme", metrics=metrics)
''',
    '''    side, metrics = _crowding_side(context, cfg)
    metrics["l2_gate"] = dict(l2_gate or {})
    if not bool(metrics.get("derivatives_data_ready", False)):
        return CrowdingUnwindDecision(
            reason="crowding_derivatives_data_missing",
            metrics=metrics,
        )
    if side is None:
        return CrowdingUnwindDecision(reason="crowding_not_extreme", metrics=metrics)
''',
    'crowding missing-data decision',
)

emas = ROOT / 'emas.py'
replace_once(
    emas,
    '''        summary = {
            'enabled': True,
            'utbreak': self._dual_alpha_light(statuses.get(ENTRY_STRATEGY_UT_BREAKOUT), 'UTBreakout'),
            'rspt': self._dual_alpha_light(statuses.get(ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND), 'RSPT-v3'),
            'qh_flow': self._dual_alpha_light(statuses.get(QH_FLOW_STRATEGY), 'QH-Flow v2'),
            'crowding_unwind': self._dual_alpha_light(statuses.get(CROWDING_UNWIND_STRATEGY), 'Crowding Unwind'),
''',
    '''        crowding_light = self._dual_alpha_light(
            statuses.get(CROWDING_UNWIND_STRATEGY),
            'Crowding Unwind',
        )
        crowding_status_metrics = (
            statuses.get(CROWDING_UNWIND_STRATEGY, {}).get('metrics')
            if isinstance(statuses.get(CROWDING_UNWIND_STRATEGY), dict)
            else None
        )
        crowding_light['metrics'] = dict(crowding_status_metrics or {})
        summary = {
            'enabled': True,
            'utbreak': self._dual_alpha_light(statuses.get(ENTRY_STRATEGY_UT_BREAKOUT), 'UTBreakout'),
            'rspt': self._dual_alpha_light(statuses.get(ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND), 'RSPT-v3'),
            'qh_flow': self._dual_alpha_light(statuses.get(QH_FLOW_STRATEGY), 'QH-Flow v2'),
            'crowding_unwind': crowding_light,
''',
    'quad crowding metrics propagation',
)
replace_once(
    emas,
    '''            reason = str(item.get('reason') or '-').strip()
            if light == 'green':
''',
    '''            reason = str(item.get('reason') or '-').strip()
            metrics = dict(item.get('metrics') or {}) if isinstance(item, dict) else {}
            if 'crowding_derivatives_data_missing' in reason:
                icon = '⚪'
                light = 'gray'
                state = '파생데이터 누락'
                meaning = '과밀 여부를 계산할 수 없어 confirmations 제외'
            elif light == 'green':
''',
    'quad traffic missing-data state',
)
replace_once(
    emas,
    '''                'side': side,
                'light': light,
            }
''',
    '''                'side': side,
                'light': light,
                'metrics': metrics,
            }
''',
    'quad traffic metrics payload',
)
replace_once(
    emas,
    '''        for key, label in strategy_rows:
            item = traffic[key]
            lines.extend([
                f"{item['icon']} {label} — {item['state']}",
                f"  사유: {item['reason']}",
                f"  판정: {item['meaning']}",
            ])
''',
    '''        def _metric_text(value, *, digits=2, signed=False):
            try:
                number = float(value)
            except (TypeError, ValueError):
                return 'N/A'
            pattern = f"{{:{'+' if signed else ''}.{digits}f}}"
            return pattern.format(number)

        for key, label in strategy_rows:
            item = traffic[key]
            lines.extend([
                f"{item['icon']} {label} — {item['state']}",
                f"  사유: {item['reason']}",
                f"  판정: {item['meaning']}",
            ])
            if key == 'crowding_unwind':
                metrics = item.get('metrics') if isinstance(item.get('metrics'), dict) else {}
                lines.append(
                    '  파생데이터: '
                    f"funding={_metric_text(metrics.get('funding_rate'), digits=6, signed=True)} | "
                    f"funding pct={_metric_text(metrics.get('funding_percentile'), digits=1)} | "
                    f"OI z={_metric_text(metrics.get('oi_z'), digits=2, signed=True)} | "
                    f"OI 4h={_metric_text(metrics.get('oi_change_4h_pct'), digits=2, signed=True)}% | "
                    f"L/S={_metric_text(metrics.get('long_short_ratio'), digits=2)}"
                )
                missing = list(metrics.get('missing_derivatives_fields') or [])
                if missing:
                    lines.append(f"  누락 필드: {', '.join(str(value) for value in missing)}")
''',
    'quad crowding detailed metrics',
)

regex_once(
    emas,
    r'''    async def build_crowding_unwind_status_text\(self, symbol=None\):\n.*?        return '\\n'\.join\(\[.*?\n        \]\)\n''',
    '''    async def build_crowding_unwind_status_text(self, symbol=None):
        target = self._canonical_futures_symbol(
            symbol or self.current_utbreakout_candidate_symbol or 'BTC/USDT'
        )
        status = dict((getattr(self, 'crowding_unwind_last_status', {}) or {}).get(target) or {})
        if not status:
            return '\n'.join([
                '🧨 Funding-OI Crowding Unwind 상태',
                f'Symbol: {target}',
                '아직 전략 평가 기록이 없습니다.',
            ])
        metrics = status.get('metrics') if isinstance(status.get('metrics'), dict) else {}
        l2 = status.get('l2_gate') if isinstance(status.get('l2_gate'), dict) else {}

        def _fmt(value, digits=2, signed=False, suffix=''):
            try:
                number = float(value)
            except (TypeError, ValueError):
                return 'N/A'
            sign = '+' if signed else ''
            return f"{number:{sign}.{digits}f}{suffix}"

        missing = list(metrics.get('missing_derivatives_fields') or [])
        lines = [
            '🧨 Funding-OI Crowding Unwind 상태',
            f'Symbol: {target}',
            f"Signal: {str(status.get('side') or 'NONE').upper()} / allowed={bool(status.get('allowed'))}",
            f"Score: {float(status.get('score', 0.0) or 0.0):.1f} / risk x{float(status.get('risk_multiplier', 0.0) or 0.0):.2f}",
            f"Funding: {_fmt(metrics.get('funding_rate'), 6, True)} / percentile {_fmt(metrics.get('funding_percentile'), 1)}",
            f"OI: z {_fmt(metrics.get('oi_z'), 2, True)} / 4h {_fmt(metrics.get('oi_change_4h_pct'), 2, True, '%')}",
            f"Long/Short ratio: {_fmt(metrics.get('long_short_ratio'), 2)}",
            f"Derivatives data: {'READY' if metrics.get('derivatives_data_ready') else 'MISSING'}",
            f"Confirmations: {int(metrics.get('confirmations', 0) or 0)} / L2 {str(l2.get('state') or 'unknown').upper()}",
            f"Reason: {status.get('reason') or '-'}",
        ]
        if missing:
            lines.append(f"Missing fields: {', '.join(str(value) for value in missing)}")
        return '\n'.join(lines)
''',
    'standalone crowding status diagnostics',
)

suite_test = ROOT / 'tests/test_strategy_suite_v3.py'
text = suite_test.read_text(encoding='utf-8')
if 'test_crowding_missing_derivatives_is_not_reported_as_not_extreme' not in text:
    text = text.rstrip() + '''


def test_crowding_missing_derivatives_is_not_reported_as_not_extreme():
    decision = evaluate_crowding_unwind(
        _crowding_rows("short"),
        {
            "funding_rate": None,
            "open_interest_delta_z": None,
            "open_interest_change_4h": None,
            "long_short_ratio": None,
        },
        {"allowed": True, "state": "deep_balanced", "risk_multiplier": 1.0},
    )
    assert decision.allowed is False
    assert decision.reason == "crowding_derivatives_data_missing"
    assert decision.metrics["derivatives_data_ready"] is False
    assert "funding_rate" in decision.metrics["missing_derivatives_fields"]
    assert "long_short_ratio" in decision.metrics["missing_derivatives_fields"]


def test_crowding_not_extreme_requires_complete_derivatives_data():
    decision = evaluate_crowding_unwind(
        _crowding_rows("short"),
        {
            "funding_rate": 0.0001,
            "funding_percentile_30d": 50.0,
            "open_interest_delta_z": 0.2,
            "open_interest_change_4h": 0.5,
            "long_short_ratio": 1.1,
        },
        {"allowed": True, "state": "deep_balanced", "risk_multiplier": 1.0},
    )
    assert decision.reason == "crowding_not_extreme"
    assert decision.metrics["derivatives_data_ready"] is True
    assert decision.metrics["missing_derivatives_fields"] == []
''' + '\n'
    suite_test.write_text(text, encoding='utf-8')

integration_test = ROOT / 'tests/test_qh_flow_integration.py'
text = integration_test.read_text(encoding='utf-8')
if 'test_quad_status_distinguishes_crowding_data_missing_from_not_extreme' not in text:
    text = text.rstrip() + r'''


def test_quad_status_distinguishes_crowding_data_missing_from_not_extreme():
    emas = _emas_module()
    engine = object.__new__(emas.SignalEngine)
    symbol = "PUMP/USDT:USDT"
    engine.current_utbreakout_candidate_symbol = symbol
    engine._canonical_futures_symbol = lambda value: value
    engine.quad_alpha_last_status = {
        symbol: {
            "reason": "QUAD_ALPHA waiting",
            "quad_alpha": {
                "agreement_state": "none",
                "agreement_risk_multiplier": 0.0,
                "confirmation_count": 0,
                "selected_label": None,
                "selected_side": None,
                "utbreak": {"light": "yellow", "side": None, "reason": "waiting"},
                "rspt": {"light": "yellow", "side": None, "reason": "waiting"},
                "qh_flow": {"light": "yellow", "side": None, "reason": "waiting"},
                "crowding_unwind": {
                    "light": "yellow",
                    "side": None,
                    "reason": "Crowding waiting: crowding_derivatives_data_missing",
                    "metrics": {
                        "funding_rate": None,
                        "funding_percentile": None,
                        "oi_z": None,
                        "oi_change_4h_pct": None,
                        "long_short_ratio": None,
                        "derivatives_data_ready": False,
                        "missing_derivatives_fields": [
                            "funding_rate",
                            "open_interest_delta_z|open_interest_change_4h",
                            "long_short_ratio",
                        ],
                    },
                },
            },
        }
    }

    report = asyncio.run(engine.build_quad_alpha_status_text(symbol))

    assert "Crowding Unwind  ⚪ 파생데이터 누락" in report
    assert "funding=N/A" in report
    assert "OI z=N/A" in report
    assert "L/S=N/A" in report
    assert "누락 필드: funding_rate" in report
'''
    integration_test.write_text(text, encoding='utf-8')
