from __future__ import annotations

from pathlib import Path

ROOT = Path('.')


def replace_once(path: Path, old: str, new: str, label: str) -> None:
    text = path.read_text(encoding='utf-8')
    if old not in text:
        raise SystemExit(f'{label}: block not found in {path}')
    path.write_text(text.replace(old, new, 1), encoding='utf-8')


def insert_before(path: Path, marker: str, content: str, label: str) -> None:
    text = path.read_text(encoding='utf-8')
    if marker not in text:
        raise SystemExit(f'{label}: marker not found in {path}')
    if content.strip() in text:
        return
    path.write_text(text.replace(marker, content + marker, 1), encoding='utf-8')


qh = ROOT / 'utbreakout/qh_flow.py'
replace_once(
    qh,
    'QH_FLOW_STRATEGY = "qh_flow_v1"\nTRIPLE_ALPHA_STRATEGY = "triple_alpha_v1"\n',
    'QH_FLOW_STRATEGY = "qh_flow_v1"\nTRIPLE_ALPHA_STRATEGY = "triple_alpha_v1"\nQUAD_ALPHA_STRATEGY = "quad_alpha_v1"\n',
    'quad strategy constant',
)
replace_once(
    qh,
    '        "triple_three_signal_multiplier": 1.00,\n        "triple_two_signal_multiplier": 0.85,\n        "triple_single_signal_multiplier": 0.55,\n',
    '        "triple_three_signal_multiplier": 1.00,\n        "triple_two_signal_multiplier": 0.85,\n        "triple_single_signal_multiplier": 0.55,\n        "quad_four_signal_multiplier": 1.00,\n        "quad_three_signal_multiplier": 0.90,\n        "quad_two_signal_multiplier": 0.75,\n        "quad_single_signal_multiplier": 0.45,\n',
    'quad default multipliers',
)

emas = ROOT / 'emas.py'
replace_once(
    emas,
    '    QH_FLOW_STRATEGY,\n    TRIPLE_ALPHA_STRATEGY,\n',
    '    QH_FLOW_STRATEGY,\n    TRIPLE_ALPHA_STRATEGY,\n    QUAD_ALPHA_STRATEGY,\n',
    'quad import',
)
replace_once(
    emas,
    '    "triple",\n    "triplet",\n    "triple_status",\n',
    '    "triple",\n    "triplet",\n    "triple_status",\n    "quad",\n    "quadalpha",\n    "quad_status",\n',
    'quad callback actions',
)
replace_once(
    emas,
    '    DUAL_ALPHA_STRATEGY,\n    TRIPLE_ALPHA_STRATEGY,\n}',
    '    DUAL_ALPHA_STRATEGY,\n    TRIPLE_ALPHA_STRATEGY,\n    QUAD_ALPHA_STRATEGY,\n}',
    'quad live strategy registry',
)
replace_once(
    emas,
    "    DUAL_ALPHA_STRATEGY: 'DUAL_ALPHA',\n    TRIPLE_ALPHA_STRATEGY: 'TRIPLE_ALPHA',\n}",
    "    DUAL_ALPHA_STRATEGY: 'DUAL_ALPHA',\n    TRIPLE_ALPHA_STRATEGY: 'TRIPLE_ALPHA',\n    QUAD_ALPHA_STRATEGY: 'QUAD_ALPHA',\n}",
    'quad display name',
)
replace_once(
    emas,
    "        'triple_alpha_three_signal_risk_multiplier': 1.00,\n        'triple_alpha_two_signal_risk_multiplier': 0.85,\n        'triple_alpha_single_signal_risk_multiplier': 0.55,\n",
    "        'triple_alpha_three_signal_risk_multiplier': 1.00,\n        'triple_alpha_two_signal_risk_multiplier': 0.85,\n        'triple_alpha_single_signal_risk_multiplier': 0.55,\n        'quad_alpha_four_signal_risk_multiplier': 1.00,\n        'quad_alpha_three_signal_risk_multiplier': 0.90,\n        'quad_alpha_two_signal_risk_multiplier': 0.75,\n        'quad_alpha_single_signal_risk_multiplier': 0.45,\n",
    'quad runtime multipliers',
)
replace_once(
    emas,
    '                active_strategy not in {DUAL_ALPHA_STRATEGY, TRIPLE_ALPHA_STRATEGY}\n',
    '                active_strategy not in {DUAL_ALPHA_STRATEGY, TRIPLE_ALPHA_STRATEGY, QUAD_ALPHA_STRATEGY}\n',
    'quad RSPT config exclusion',
)
replace_once(
    emas,
    '        self.triple_alpha_last_status = {}  # symbol -> latest triple strategy summary\n',
    '        self.triple_alpha_last_status = {}  # symbol -> latest triple strategy summary\n        self.quad_alpha_last_status = {}  # symbol -> latest four-strategy agreement summary\n',
    'quad runtime state init',
)
replace_once(
    emas,
    '        self.triple_alpha_last_status = {}\n        self.last_live_entry_snapshot = {}\n',
    '        self.triple_alpha_last_status = {}\n        self.quad_alpha_last_status = {}\n        self.last_live_entry_snapshot = {}\n',
    'quad runtime state reset',
)
replace_once(
    emas,
    "        if plan.get('triple_alpha_agreement_state') or plan.get('triple_alpha_selected_strategy'):\n            return TRIPLE_ALPHA_STRATEGY\n",
    "        if plan.get('quad_alpha_agreement_state') or plan.get('quad_alpha_selected_strategy'):\n            return QUAD_ALPHA_STRATEGY\n        if plan.get('triple_alpha_agreement_state') or plan.get('triple_alpha_selected_strategy'):\n            return TRIPLE_ALPHA_STRATEGY\n",
    'quad allocator strategy key',
)

quad_methods = r'''
    def _quad_alpha_strategy_params(self, strategy_params, branch):
        branch = str(branch or '').lower()
        if branch != CROWDING_UNWIND_STRATEGY:
            return self._triple_alpha_strategy_params(strategy_params, branch)
        params = copy.deepcopy(strategy_params if isinstance(strategy_params, dict) else {})
        cfg = dict(params.get('UTBotFilteredBreakoutV1') or {})
        crowd_cfg = self._crowding_unwind_runtime_config(cfg)
        crowd_cfg['enabled'] = True
        crowd_cfg['live_enabled'] = True
        cfg['crowding_unwind'] = crowd_cfg
        cfg['crowding_unwind_live_enabled'] = True
        cfg['qh_flow_confirmation_enabled'] = False
        cfg['relative_strength_pullback_trend_live_enabled'] = False
        cfg['adaptive_timeframe_enabled'] = False
        cfg['entry_strategy'] = ENTRY_STRATEGY_UT_BREAKOUT
        params['active_strategy'] = CROWDING_UNWIND_STRATEGY
        params['UTBotFilteredBreakoutV1'] = cfg
        return params

    async def _calculate_quad_alpha_signal(
        self,
        symbol,
        df,
        strategy_params,
        *,
        force_reprocess=False,
    ):
        base_symbol = self._canonical_futures_symbol(symbol)
        self._clear_utbot_filtered_breakout_entry_plan(base_symbol)
        base_cfg = self._get_utbot_filtered_breakout_config(strategy_params)
        qh_cfg = self._qh_flow_runtime_config(base_cfg)
        multipliers = {
            4: max(0.0, min(1.0, float(base_cfg.get('quad_alpha_four_signal_risk_multiplier', qh_cfg.get('quad_four_signal_multiplier', 1.0)) or 1.0))),
            3: max(0.0, min(1.0, float(base_cfg.get('quad_alpha_three_signal_risk_multiplier', qh_cfg.get('quad_three_signal_multiplier', 0.90)) or 0.90))),
            2: max(0.0, min(1.0, float(base_cfg.get('quad_alpha_two_signal_risk_multiplier', qh_cfg.get('quad_two_signal_multiplier', 0.75)) or 0.75))),
            1: max(0.0, min(1.0, float(base_cfg.get('quad_alpha_single_signal_risk_multiplier', qh_cfg.get('quad_single_signal_multiplier', 0.45)) or 0.45))),
        }
        branch_results = []

        ut_params = self._quad_alpha_strategy_params(strategy_params, ENTRY_STRATEGY_UT_BREAKOUT)
        ut_sig, ut_reason, ut_status = await self._calculate_utbot_filtered_breakout_signal(
            base_symbol,
            df,
            ut_params,
            force_reprocess=force_reprocess,
        )
        ut_status = dict(ut_status or self._utbreakout_diag_for_symbol(base_symbol) or {})
        ut_plan = (
            dict(self._get_utbot_filtered_breakout_entry_plan(base_symbol, ut_sig) or {})
            if ut_sig in {'long', 'short'}
            else None
        )
        branch_results.append((ENTRY_STRATEGY_UT_BREAKOUT, 'UTBreakout', ut_sig, ut_reason, ut_status, ut_plan, 0))
        self._clear_utbot_filtered_breakout_entry_plan(base_symbol)

        rsp_params = self._quad_alpha_strategy_params(
            strategy_params,
            ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND,
        )
        rsp_sig, rsp_reason, rsp_status = await self._calculate_relative_strength_pullback_signal(
            base_symbol,
            df,
            rsp_params,
            force_reprocess=force_reprocess,
            forced_direction=None,
            direction_source='RSPT-v3 BTC/ETH/alt/vol residual strength',
            resolve_ut_direction=False,
        )
        rsp_status = dict(rsp_status or self._utbreakout_diag_for_symbol(base_symbol) or {})
        rsp_symbol = self._canonical_futures_symbol(
            rsp_status.get('plan_symbol') or rsp_status.get('symbol') or base_symbol
        )
        rsp_plan = (
            dict(self._get_utbot_filtered_breakout_entry_plan(rsp_symbol, rsp_sig) or {})
            if rsp_sig in {'long', 'short'}
            else None
        )
        branch_results.append((
            ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND,
            'RSPT-v3',
            rsp_sig,
            rsp_reason,
            rsp_status,
            rsp_plan,
            1,
        ))
        self._clear_utbot_filtered_breakout_entry_plan(rsp_symbol)

        qh_params = self._quad_alpha_strategy_params(strategy_params, QH_FLOW_STRATEGY)
        qh_sig, qh_reason, qh_status = await self._calculate_qh_flow_signal(
            base_symbol,
            df,
            qh_params,
            force_reprocess=force_reprocess,
        )
        qh_status = dict(qh_status or {})
        qh_symbol = self._canonical_futures_symbol(
            qh_status.get('plan_symbol') or qh_status.get('symbol') or base_symbol
        )
        qh_plan = (
            dict(self._get_utbot_filtered_breakout_entry_plan(qh_symbol, qh_sig) or {})
            if qh_sig in {'long', 'short'}
            else None
        )
        branch_results.append((QH_FLOW_STRATEGY, 'QH-Flow v2', qh_sig, qh_reason, qh_status, qh_plan, 2))
        self._clear_utbot_filtered_breakout_entry_plan(qh_symbol)

        crowd_params = self._quad_alpha_strategy_params(strategy_params, CROWDING_UNWIND_STRATEGY)
        crowd_sig, crowd_reason, crowd_status = await self._calculate_crowding_unwind_signal(
            base_symbol,
            df,
            crowd_params,
            force_reprocess=force_reprocess,
        )
        crowd_status = dict(crowd_status or {})
        crowd_symbol = self._canonical_futures_symbol(
            crowd_status.get('plan_symbol') or crowd_status.get('symbol') or base_symbol
        )
        crowd_plan = (
            dict(self._get_utbot_filtered_breakout_entry_plan(crowd_symbol, crowd_sig) or {})
            if crowd_sig in {'long', 'short'}
            else None
        )
        branch_results.append((
            CROWDING_UNWIND_STRATEGY,
            'Crowding Unwind',
            crowd_sig,
            crowd_reason,
            crowd_status,
            crowd_plan,
            3,
        ))
        self._clear_utbot_filtered_breakout_entry_plan(crowd_symbol)

        choices = []
        for key, label, side, reason, status, plan, priority in branch_results:
            if side not in {'long', 'short'} or not isinstance(plan, dict):
                continue
            choices.append({
                'key': key,
                'label': label,
                'side': side,
                'reason': reason,
                'status': status,
                'plan': plan,
                'score': self._dual_alpha_score(key, side, status, plan),
                'priority': priority,
            })

        unique_sides = {choice['side'] for choice in choices}
        selected = None
        agreement_state = 'none'
        agreement_multiplier = 0.0
        if len(unique_sides) > 1:
            agreement_state = 'conflict'
        elif choices:
            confirmation_count = len(choices)
            agreement_state = {1: 'single', 2: 'double', 3: 'triple', 4: 'quad'}.get(
                confirmation_count,
                'confirmed',
            )
            agreement_multiplier = multipliers.get(confirmation_count, multipliers[1])
            selected = sorted(
                choices,
                key=lambda item: (-float(item.get('score') or 0.0), int(item.get('priority') or 0)),
            )[0]

        statuses = {key: status for key, _, _, _, status, _, _ in branch_results}
        reasons = {key: reason for key, _, _, reason, _, _, _ in branch_results}
        final_status = dict(
            (selected or {}).get('status')
            or crowd_status
            or qh_status
            or rsp_status
            or ut_status
            or {}
        )
        if selected:
            selected_plan = self._dual_alpha_scale_plan(selected['plan'], agreement_multiplier)
            selected_plan.pop('dual_alpha_risk_multiplier', None)
            selected_plan.pop('triple_alpha_risk_multiplier', None)
            selected_plan.update({
                'strategy': selected['key'],
                'quad_alpha_selected_strategy': selected['key'],
                'quad_alpha_score': selected['score'],
                'quad_alpha_agreement_state': agreement_state,
                'quad_alpha_confirmation_count': len(choices),
                'quad_alpha_risk_multiplier': agreement_multiplier,
            })
            self._set_utbot_filtered_breakout_entry_plan(
                selected_plan.get('plan_symbol') or base_symbol,
                selected_plan,
            )

        summary = {
            'enabled': True,
            'utbreak': self._dual_alpha_light(statuses.get(ENTRY_STRATEGY_UT_BREAKOUT), 'UTBreakout'),
            'rspt': self._dual_alpha_light(statuses.get(ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND), 'RSPT-v3'),
            'qh_flow': self._dual_alpha_light(statuses.get(QH_FLOW_STRATEGY), 'QH-Flow v2'),
            'crowding_unwind': self._dual_alpha_light(statuses.get(CROWDING_UNWIND_STRATEGY), 'Crowding Unwind'),
            'agreement_state': agreement_state,
            'agreement_risk_multiplier': agreement_multiplier,
            'confirmation_count': len(choices),
            'selected': selected.get('key') if selected else None,
            'selected_label': selected.get('label') if selected else None,
            'selected_side': selected.get('side') if selected else None,
            'selection_score': selected.get('score') if selected else None,
            'scores': {choice['key']: choice['score'] for choice in choices},
        }
        final_status.update({
            'strategy': STRATEGY_DISPLAY_NAMES.get(QUAD_ALPHA_STRATEGY, 'QUAD_ALPHA'),
            'entry_strategy': QUAD_ALPHA_STRATEGY,
            'quad_alpha_enabled': True,
            'quad_selected_strategy': summary['selected'],
            'quad_alpha': summary,
        })
        if selected:
            final_status.update({
                'accepted_code': 'ACCEPTED_ENTRY',
                'accepted_side': selected['side'],
                'stage': 'entry_ready',
                'reason': (
                    f"QUAD_ALPHA {agreement_state} selected {selected['label']} "
                    f"{selected['side'].upper()} at {agreement_multiplier:.0%} risk: "
                    f"{selected['reason']}"
                ),
            })
        else:
            final_status['stage'] = 'waiting'
            if agreement_state == 'conflict':
                final_status['reject_code'] = 'REJECTED_QUAD_DIRECTION_CONFLICT'
            final_status['reason'] = (
                f"QUAD_ALPHA waiting ({agreement_state}): "
                f"UT={reasons.get(ENTRY_STRATEGY_UT_BREAKOUT)}; "
                f"RSPT={reasons.get(ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND)}; "
                f"QH={reasons.get(QH_FLOW_STRATEGY)}; "
                f"CROWD={reasons.get(CROWDING_UNWIND_STRATEGY)}"
            )

        canonical = self._canonical_futures_symbol(final_status.get('plan_symbol') or base_symbol)
        if not isinstance(getattr(self, 'quad_alpha_last_status', None), dict):
            self.quad_alpha_last_status = {}
        self.quad_alpha_last_status[canonical] = dict(final_status)
        self._store_utbot_filtered_breakout_status(canonical, final_status)
        self.last_entry_reason[canonical] = final_status.get('reason')
        if selected:
            return selected['side'], final_status['reason'], final_status
        return None, final_status['reason'], final_status

    async def build_quad_alpha_status_text(self, symbol=None):
        target = self._canonical_futures_symbol(
            symbol or self.current_utbreakout_candidate_symbol or 'BTC/USDT'
        )
        status = dict((getattr(self, 'quad_alpha_last_status', {}) or {}).get(target) or {})
        summary = status.get('quad_alpha') if isinstance(status.get('quad_alpha'), dict) else {}
        if not summary:
            return '\n'.join([
                '🧩 Quad 전략 상태',
                f'Symbol: {target}',
                '아직 Quad 평가 기록이 없습니다.',
            ])
        lines = [
            '🧩 Quad 전략 상태',
            f'Symbol: {target}',
            f"Agreement: {str(summary.get('agreement_state') or 'none').upper()} / confirmations={int(summary.get('confirmation_count') or 0)} / risk x{float(summary.get('agreement_risk_multiplier', 0.0) or 0.0):.2f}",
            f"Selected: {summary.get('selected_label') or 'NONE'} {str(summary.get('selected_side') or '').upper()}",
        ]
        for key, label in (
            ('utbreak', 'UTBreak'),
            ('rspt', 'RSPT-v3'),
            ('qh_flow', 'QH-Flow v2'),
            ('crowding_unwind', 'Crowding Unwind'),
        ):
            item = summary.get(key) if isinstance(summary.get(key), dict) else {}
            lines.append(
                f"{label}: {str(item.get('light') or 'gray').upper()} "
                f"{str(item.get('side') or 'NONE').upper()} - {item.get('reason') or '-'}"
            )
        lines.append(f"Reason: {status.get('reason') or '-'}")
        return '\n'.join(lines)

'''
insert_before(
    emas,
    '    def _resolve_dual_alpha_trading_mode(self, cfg, exchange_mode=None):\n',
    quad_methods,
    'quad methods',
)

replace_once(
    emas,
    "        elif active_strategy == TRIPLE_ALPHA_STRATEGY:\n            entry_mode = active_strategy\n            sig, entry_reason, _ = await self._calculate_triple_alpha_signal(\n                symbol,\n                df,\n                strategy_params,\n                force_reprocess=force_utbreakout_reprocess,\n            )\n            is_bullish = sig == 'long'\n            is_bearish = sig == 'short'\n",
    "        elif active_strategy == TRIPLE_ALPHA_STRATEGY:\n            entry_mode = active_strategy\n            sig, entry_reason, _ = await self._calculate_triple_alpha_signal(\n                symbol,\n                df,\n                strategy_params,\n                force_reprocess=force_utbreakout_reprocess,\n            )\n            is_bullish = sig == 'long'\n            is_bearish = sig == 'short'\n        elif active_strategy == QUAD_ALPHA_STRATEGY:\n            entry_mode = active_strategy\n            sig, entry_reason, _ = await self._calculate_quad_alpha_signal(\n                symbol,\n                df,\n                strategy_params,\n                force_reprocess=force_utbreakout_reprocess,\n            )\n            is_bullish = sig == 'long'\n            is_bearish = sig == 'short'\n",
    'quad strategy dispatch',
)

quad_activation = r'''
        async def _activate_quad_alpha_strategy():
            await _ensure_signal_engine_active()
            self.is_paused = False
            current = self.cfg.get('signal_engine', {}).get('strategy_params', {}).get('UTBotFilteredBreakoutV1', {})
            rsp_cfg = default_relative_strength_pullback_config()
            if isinstance(current, dict) and isinstance(current.get('relative_strength_pullback_trend'), dict):
                rsp_cfg.update(current.get('relative_strength_pullback_trend'))
            rsp_cfg.update({
                'entry_strategy': ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND,
                'relative_strength_pullback_trend_shadow_enabled': True,
                'relative_strength_pullback_trend_live_enabled': True,
                'relative_strength_pullback_trend_paper_enabled': False,
                'rspt_v2_enabled': True,
                'rspt_v3_enabled': True,
                'independent_direction_enabled': True,
                'direction_source': 'RSPT-v3 BTC/ETH/alt/vol residual strength',
                'forced_direction': None,
            })
            qh_cfg = default_qh_flow_config()
            if isinstance(current, dict) and isinstance(current.get('qh_flow'), dict):
                qh_cfg.update(current.get('qh_flow'))
            qh_cfg['qh_flow_enabled'] = True
            qh_cfg['qh_flow_live_enabled'] = True
            crowd_cfg = default_crowding_unwind_config()
            if isinstance(current, dict) and isinstance(current.get('crowding_unwind'), dict):
                crowd_cfg.update(current.get('crowding_unwind'))
            crowd_cfg['enabled'] = True
            crowd_cfg['live_enabled'] = True
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'active_strategy'], QUAD_ALPHA_STRATEGY)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'entry_strategy'], ENTRY_STRATEGY_UT_BREAKOUT)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'relative_strength_pullback_trend'], rsp_cfg)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'relative_strength_pullback_trend_live_enabled'], True)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'qh_flow'], qh_cfg)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'qh_flow_live_enabled'], True)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'crowding_unwind'], crowd_cfg)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'crowding_unwind_live_enabled'], True)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'l2_gate_enabled'], True)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'adaptive_timeframe_enabled'], True)
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'selection_mode'], 'auto')
            await self.cfg.update_value(['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'auto_select_enabled'], True)
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'fixed_symbol_mode_enabled'], False)
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'fixed_symbol'], '')
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'custom_universe_enabled'], False)
            await self.cfg.update_value(['signal_engine', 'coin_selector', 'enabled'], True)
            await self.cfg.update_value(['signal_engine', 'common_settings', 'scanner_enabled'], True)
            await self.cfg.update_value(['signal_engine', 'micro_auto', 'enabled'], False)
            engine = self._reset_signal_engine_runtime_state(
                reset_entry_cache=True,
                reset_exit_cache=True,
                reset_stateful_strategy=True,
            )
            if engine:
                engine.qh_flow_signal_cache = {}
                engine.qh_flow_last_status = {}
                engine.crowding_unwind_last_status = {}
                engine.quad_alpha_last_status = {}
                engine.relative_strength_pullback_eval_cache = {}
                engine.l2_gate_cache = {}
                engine.l2_gate_history = {}
                if not engine.running:
                    engine.start()
            return (
                'QUAD Alpha ON. UTBreak + RSPT-v3 + QH-Flow v2 + Crowding Unwind are '
                'evaluated independently; direction conflicts block and 1/2/3/4 confirmations scale risk.'
            )

        async def _deactivate_quad_alpha_strategy():
            notice = await _activate_utbreak_strategy()
            await self.cfg.update_value(
                ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'crowding_unwind_live_enabled'],
                False,
            )
            engine = self.engines.get('signal')
            if engine:
                engine.quad_alpha_last_status = {}
                engine.qh_flow_signal_cache = {}
                engine.crowding_unwind_last_status = {}
            return f'QUAD Alpha OFF. {notice}'

'''
insert_before(
    emas,
    '        async def _activate_crowding_unwind_strategy():\n',
    quad_activation,
    'quad telegram activation',
)

replace_once(
    emas,
    '            elif active_strategy == TRIPLE_ALPHA_STRATEGY:\n                active_label = "ON (TRIPLE ALPHA)"\n',
    '            elif active_strategy == TRIPLE_ALPHA_STRATEGY:\n                active_label = "ON (TRIPLE ALPHA)"\n            elif active_strategy == QUAD_ALPHA_STRATEGY:\n                active_label = "ON (QUAD ALPHA)"\n',
    'quad active menu label',
)
replace_once(
    emas,
    "            menu_title = (\n                'UTBreak 전략 메뉴 (Triple Alpha)'\n                if active_strategy == TRIPLE_ALPHA_STRATEGY\n",
    "            menu_title = (\n                'UTBreak 전략 메뉴 (Quad Alpha)'\n                if active_strategy == QUAD_ALPHA_STRATEGY\n                else 'UTBreak 전략 메뉴 (Triple Alpha)'\n                if active_strategy == TRIPLE_ALPHA_STRATEGY\n",
    'quad menu title',
)
replace_once(
    emas,
    '`/utbreak triple on|off|status` - UTBreakout + RSPT-v3 + QH-Flow v2 결합\n',
    '`/utbreak triple on|off|status` - UTBreakout + RSPT-v3 + QH-Flow v2 결합\n`/utbreak quad on|off|status` - UTBreakout + RSPT-v3 + QH-Flow v2 + Crowding 결합\n',
    'quad menu help',
)
replace_once(
    emas,
    '                [\n                    InlineKeyboardButton("TRIPLE ON", callback_data="utb:triple:on"),\n                    InlineKeyboardButton("TRIPLE OFF", callback_data="utb:triple:off"),\n                    InlineKeyboardButton("TRIPLE STATUS", callback_data="utb:triple_status")\n                ],\n                [\n                    InlineKeyboardButton("코인 감시 목록", callback_data="utb:watchlist")\n                ]\n',
    '                [\n                    InlineKeyboardButton("TRIPLE ON", callback_data="utb:triple:on"),\n                    InlineKeyboardButton("TRIPLE OFF", callback_data="utb:triple:off"),\n                    InlineKeyboardButton("TRIPLE STATUS", callback_data="utb:triple_status")\n                ],\n                [\n                    InlineKeyboardButton("QUAD ON", callback_data="utb:quad:on"),\n                    InlineKeyboardButton("QUAD OFF", callback_data="utb:quad:off"),\n                    InlineKeyboardButton("QUAD STATUS", callback_data="utb:quad_status")\n                ],\n                [\n                    InlineKeyboardButton("코인 감시 목록", callback_data="utb:watchlist")\n                ]\n',
    'quad keyboard row',
)

quad_status_helpers = r'''
        async def _get_quad_alpha_status_text():
            engine = self.engines.get('signal')
            if not engine:
                return 'QUAD Alpha status\n\nSignal engine not found.'
            symbol = await _get_utbreakout_status_symbol_async()
            return await engine.build_quad_alpha_status_text(symbol)

        async def _send_quad_alpha_status(message):
            if message is None:
                return False
            try:
                text = await _get_quad_alpha_status_text()
                await self._reply_long_text_with_document(
                    message,
                    text,
                    reply_markup=_build_utbreakout_keyboard(),
                    filename='quad_alpha_status.txt',
                    caption='QUAD Alpha status',
                    preview_suffix='QUAD Alpha status was sent as a file.',
                )
                return True
            except Exception as exc:
                logger.exception('QUAD Alpha status send failed')
                await message.reply_text(
                    f'QUAD Alpha status failed: {type(exc).__name__}: {exc}',
                    reply_markup=_build_utbreakout_keyboard(),
                )
                return False

        async def _send_quad_alpha_status_from_callback(query):
            await _show_utbreakout_callback_progress(query, 'QUAD Alpha status 조회 중입니다.')
            await _send_quad_alpha_status(getattr(query, 'message', None))

'''
insert_before(
    emas,
    '        async def _get_crowding_unwind_status_text():\n',
    quad_status_helpers,
    'quad status helpers',
)

quad_text_command = r'''            elif action in {'quad', 'quadalpha', 'quad_alpha'}:
                mode = str(args[1]).strip().lower() if len(args) > 1 else 'status'
                if mode in {'on', 'enable', 'start', 'live'}:
                    await u.message.reply_text(
                        await _activate_quad_alpha_strategy(),
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=_build_utbreakout_keyboard(),
                    )
                elif mode in {'off', 'disable', 'stop'}:
                    await u.message.reply_text(
                        await _deactivate_quad_alpha_strategy(),
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=_build_utbreakout_keyboard(),
                    )
                elif mode in {'status', 'stat', 'menu', ''}:
                    await _send_quad_alpha_status(u.message)
                else:
                    await u.message.reply_text(
                        'Usage: `/utbreak quad on`, `/utbreak quad off`, `/utbreak quad status`',
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=_build_utbreakout_keyboard(),
                    )
                    return
'''
insert_before(
    emas,
    "            elif action in {'dual', 'dualt', 'dualalpha', 'dual_alpha'}:\n",
    quad_text_command,
    'quad text command',
)

quad_callback = r'''            if action in {'quad', 'quadalpha'}:
                mode = str(value or '').lower()
                if mode in {'on', 'enable', '1', 'true', 'live'}:
                    await _edit_utbreakout_menu(query, await _activate_quad_alpha_strategy())
                elif mode in {'off', 'disable', '0', 'false', 'stop'}:
                    await _edit_utbreakout_menu(query, await _deactivate_quad_alpha_strategy())
                else:
                    await _send_quad_alpha_status_from_callback(query)
                return

'''
insert_before(
    emas,
    "            if action in {'dual', 'dualt'}:\n",
    quad_callback,
    'quad callback',
)
replace_once(
    emas,
    "            if action == 'triple_status':\n                await _send_triple_alpha_status_from_callback(query)\n                return\n",
    "            if action == 'triple_status':\n                await _send_triple_alpha_status_from_callback(query)\n                return\n\n            if action == 'quad_status':\n                await _send_quad_alpha_status_from_callback(query)\n                return\n",
    'quad status callback',
)

readme = ROOT / 'README.md'
replace_once(
    readme,
    '현재 실운영 핵심은 **UTBreak Set64(EV Adaptive)**, **RSPT-v3**, **QH-Flow v2**, 두 전략을 묶는 **Dual**, 세 전략을 독립적으로 결합하는 **Triple**입니다.\n',
    '현재 실운영 핵심은 **UTBreak Set64(EV Adaptive)**, **RSPT-v3**, **QH-Flow v2**, **Funding-OI Crowding Unwind**, 두 전략을 묶는 **Dual**, 세 전략을 결합하는 **Triple**, 네 전략을 결합하는 **Quad**입니다.\n',
    'README opening quad',
)
quad_readme = '''### 6. Quad — UTBreak + RSPT-v3 + QH-Flow v2 + Crowding Unwind

Quad는 네 전략을 각각 독립적으로 계산합니다. Crowding Unwind가 다른 전략과 반대 방향이면 과밀 해소 신호를 무시하지 않고 **방향 충돌로 신규 진입을 차단**합니다.

| 유효한 동일 방향 신호 | 위험 배율 |
|---|---:|
| 4개 | 100% |
| 3개 | 90% |
| 2개 | 75% |
| 1개 | 45% |
| LONG·SHORT 혼재 | 거래 차단 |

최종 주문은 네 전략 중 점수가 가장 높은 기존 진입 계획을 사용하며, 진입가·손절가·익절가는 유지하고 수량과 위험금액만 합의 배율에 맞춰 줄입니다. 단독 Crowding 전략과 기존 Dual·Triple도 계속 별도로 사용할 수 있습니다.

'''
insert_before(readme, '### 6. 레거시 전략\n', quad_readme, 'README quad section')
replace_once(readme, '### 6. 레거시 전략\n', '### 7. 레거시 전략\n', 'README legacy renumber')
replace_once(
    readme,
    '- **공통 L2 Gate**: UTBreak, RSPT-v3, QH-Flow v2, Dual, Triple 모두 상위 20호가의 스프레드·깊이·불균형을 확인합니다.\n',
    '- **공통 L2 Gate**: UTBreak, RSPT-v3, QH-Flow v2, Crowding, Dual, Triple, Quad 모두 상위 20호가의 스프레드·깊이·불균형을 확인합니다.\n',
    'README L2 quad',
)
replace_once(
    readme,
    '| `/utbreak` | UTBreak/Set64/RSPT/QH-Flow v2/Dual/Triple 메뉴 |\n',
    '| `/utbreak` | UTBreak/Set64/RSPT/QH-Flow v2/Crowding/Dual/Triple/Quad 메뉴 |\n',
    'README command table quad',
)
replace_once(
    readme,
    '/utbreak triple on|off|status\n',
    '/utbreak triple on|off|status\n/utbreak quad on|off|status\n',
    'README quad command',
)

test = ROOT / 'tests/test_qh_flow_integration.py'
replace_once(
    test,
    '    assert emas.TRIPLE_ALPHA_STRATEGY in emas.UTBREAKOUT_STRATEGIES\n',
    '    assert emas.TRIPLE_ALPHA_STRATEGY in emas.UTBREAKOUT_STRATEGIES\n    assert emas.QUAD_ALPHA_STRATEGY in emas.UTBREAKOUT_STRATEGIES\n',
    'quad selectable test',
)
replace_once(
    test,
    '    assert emas.STRATEGY_DISPLAY_NAMES[emas.TRIPLE_ALPHA_STRATEGY] == "TRIPLE_ALPHA"\n',
    '    assert emas.STRATEGY_DISPLAY_NAMES[emas.TRIPLE_ALPHA_STRATEGY] == "TRIPLE_ALPHA"\n    assert emas.STRATEGY_DISPLAY_NAMES[emas.QUAD_ALPHA_STRATEGY] == "QUAD_ALPHA"\n',
    'quad display test',
)
replace_once(
    test,
    '        "triple_status",\n    } <= emas.UTBREAKOUT_CALLBACK_ACTIONS\n',
    '        "triple_status",\n        "quad",\n        "quadalpha",\n        "quad_status",\n    } <= emas.UTBREAKOUT_CALLBACK_ACTIONS\n',
    'quad callback test',
)

append = r'''

def test_quad_branch_params_enable_crowding_without_cross_branch_confirmation():
    emas = _emas_module()
    engine = object.__new__(emas.SignalEngine)
    params = {
        "active_strategy": emas.QUAD_ALPHA_STRATEGY,
        "UTBotFilteredBreakoutV1": {
            "qh_flow_confirmation_enabled": True,
            "crowding_unwind_live_enabled": False,
            "crowding_unwind": {"enabled": False, "live_enabled": False},
        },
    }
    engine._crowding_unwind_runtime_config = lambda cfg=None: {
        "enabled": False,
        "live_enabled": False,
    }

    crowd = engine._quad_alpha_strategy_params(params, emas.CROWDING_UNWIND_STRATEGY)

    assert crowd["active_strategy"] == emas.CROWDING_UNWIND_STRATEGY
    cfg = crowd["UTBotFilteredBreakoutV1"]
    assert cfg["crowding_unwind_live_enabled"] is True
    assert cfg["crowding_unwind"]["enabled"] is True
    assert cfg["crowding_unwind"]["live_enabled"] is True
    assert cfg["qh_flow_confirmation_enabled"] is False


def test_quad_four_way_agreement_selects_full_risk_plan():
    emas = _emas_module()
    engine = object.__new__(emas.SignalEngine)
    engine.quad_alpha_last_status = {}
    engine.last_entry_reason = {}
    selected_plans = []
    plans = {
        emas.ENTRY_STRATEGY_UT_BREAKOUT: {"strategy": emas.ENTRY_STRATEGY_UT_BREAKOUT, "plan_symbol": "BTC/USDT:USDT", "qty": 1.0, "risk_usdt": 1.0},
        emas.ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND: {"strategy": emas.ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND, "plan_symbol": "BTC/USDT:USDT", "qty": 1.0, "risk_usdt": 1.0},
        emas.QH_FLOW_STRATEGY: {"strategy": emas.QH_FLOW_STRATEGY, "plan_symbol": "BTC/USDT:USDT", "qty": 1.0, "risk_usdt": 1.0},
        emas.CROWDING_UNWIND_STRATEGY: {"strategy": emas.CROWDING_UNWIND_STRATEGY, "plan_symbol": "BTC/USDT:USDT", "qty": 1.0, "risk_usdt": 1.0},
    }
    current = {"key": None}

    engine._canonical_futures_symbol = lambda symbol: symbol
    engine._clear_utbot_filtered_breakout_entry_plan = lambda symbol: None
    engine._get_utbot_filtered_breakout_config = lambda params=None: {}
    engine._qh_flow_runtime_config = lambda cfg=None: {}
    engine._quad_alpha_strategy_params = lambda params, branch: {"branch": branch}
    engine._utbreakout_diag_for_symbol = lambda symbol: {}
    engine._get_utbot_filtered_breakout_entry_plan = lambda symbol, side=None: plans[current["key"]]
    engine._dual_alpha_score = lambda key, side, status, plan: {
        emas.ENTRY_STRATEGY_UT_BREAKOUT: 90,
        emas.ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND: 80,
        emas.QH_FLOW_STRATEGY: 70,
        emas.CROWDING_UNWIND_STRATEGY: 60,
    }[key]
    engine._dual_alpha_scale_plan = lambda plan, multiplier: {
        **plan,
        "qty": plan["qty"] * multiplier,
        "risk_usdt": plan["risk_usdt"] * multiplier,
    }
    engine._dual_alpha_light = lambda status, label: {
        "light": "green",
        "side": status.get("accepted_side"),
        "reason": status.get("reason"),
    }
    engine._set_utbot_filtered_breakout_entry_plan = lambda symbol, plan: selected_plans.append((symbol, dict(plan)))
    engine._store_utbot_filtered_breakout_status = lambda symbol, status: None

    async def ut(*args, **kwargs):
        current["key"] = emas.ENTRY_STRATEGY_UT_BREAKOUT
        return "long", "ut long", {"accepted_side": "long", "reason": "ut long"}

    async def rsp(*args, **kwargs):
        current["key"] = emas.ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND
        return "long", "rsp long", {"accepted_side": "long", "reason": "rsp long"}

    async def qh(*args, **kwargs):
        current["key"] = emas.QH_FLOW_STRATEGY
        return "long", "qh long", {"accepted_side": "long", "reason": "qh long"}

    async def crowd(*args, **kwargs):
        current["key"] = emas.CROWDING_UNWIND_STRATEGY
        return "long", "crowd long", {"accepted_side": "long", "reason": "crowd long"}

    engine._calculate_utbot_filtered_breakout_signal = ut
    engine._calculate_relative_strength_pullback_signal = rsp
    engine._calculate_qh_flow_signal = qh
    engine._calculate_crowding_unwind_signal = crowd

    side, _, status = asyncio.run(
        engine._calculate_quad_alpha_signal(
            "BTC/USDT:USDT",
            None,
            {"active_strategy": emas.QUAD_ALPHA_STRATEGY},
        )
    )

    assert side == "long"
    assert status["quad_alpha"]["confirmation_count"] == 4
    assert status["quad_alpha"]["agreement_state"] == "quad"
    assert status["quad_alpha"]["agreement_risk_multiplier"] == pytest.approx(1.0)
    assert selected_plans[-1][1]["quad_alpha_confirmation_count"] == 4
    assert selected_plans[-1][1]["qty"] == pytest.approx(1.0)
'''
text = test.read_text(encoding='utf-8')
if 'test_quad_four_way_agreement_selects_full_risk_plan' not in text:
    test.write_text(text.rstrip() + append + '\n', encoding='utf-8')
