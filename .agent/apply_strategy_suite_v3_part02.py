PART = ' if tier == "low" else 0.0)\n    if score < score_min:\n        return QHFlowDecision(side=side, score=score, reason="score_below_threshold", metrics=metrics)\n    tier_multiplier = float(cfg.get(f"liquidity_{tier}_risk_multiplier", 0.50) or 0.50)\n    risk_multiplier = min(\n        1.0,\n        max(float(cfg["risk_multiplier_floor"]), score / 100.0),\n        max(0.0, float((l2_gate or {}).get("risk_multiplier", 0.0) or 0.0)),\n        crowding_multiplier,\n        float(benchmark_result["multiplier"]),\n        tier_multiplier,\n    )\n    metrics.update({"crowding_multiplier": crowding_multiplier, "score": score, "strategy_version": QH_FLOW_VERSION})\n    return QHFlowDecision(\n        side=side,\n        allowed=True,\n        score=score,\n        risk_multiplier=risk_multiplier,\n        reason=(\n            f"QH-v2 {side} score={score:.1f} imbalance={imbalance:+.3f} "\n            f"z={signed_z:.2f} volume={notional_ratio:.2f}x "\n            f"bench={benchmark_result[\'confirmations\']} L2={l2_gate.get(\'state\')} tier={tier}"\n        ),\n        metrics=metrics,\n    )\n\',
)

# RSPT-v3: retain the public strategy ID and execution model, swap only the
# residual factor engine and make the v3 factors explicit in defaults/status.
rsp = ROOT / \'utbreakout/relative_strength_pullback.py\'
replace_once(
    rsp,
    \'\'\'from .rspt_v2 import (
    evaluate_pullback_setup as evaluate_rspt_v2_pullback_setup,
    residual_strength_percentiles,
    volatility_risk_multiplier as rspt_v2_volatility_risk_multiplier,
)\'\'\',
    \'\'\'from .rspt_v3 import (
    evaluate_pullback_setup as evaluate_rspt_v2_pullback_setup,
    residual_strength_percentiles,
    volatility_risk_multiplier as rspt_v2_volatility_risk_multiplier,
)\'\'\',
    \'RSPT-v3 import\',
)
replace_once(rsp, \'"strategy_version": "v2",\', \'"strategy_version": "v3",\', \'RSPT version\')
replace_once(
    rsp,
    \'\'\'        "rspt_v2_enabled": True,
        "independent_direction_enabled": True,\'\'\',
    \'\'\'        "rspt_v2_enabled": True,
        "rspt_v3_enabled": True,
        "alt_common_factor_enabled": True,
        "market_volatility_factor_enabled": True,
        "alt_common_factor_min_symbols": 4,
        "independent_direction_enabled": True,\'\'\',
    \'RSPT-v3 defaults\',
)
replace_once(
    rsp,
    \'"direction_source": "RSPT-v2 residual strength",\',
    \'"direction_source": "RSPT-v3 BTC/ETH/alt/vol residual strength",\',
    \'RSPT direction source\',
)

emas = ROOT / \'emas.py\'
# Imports.
replace_once(
    emas,
    \'\'\'from utbreakout.qh_flow import (
    QH_FLOW_STRATEGY,
    TRIPLE_ALPHA_STRATEGY,
    boundary_phase as qh_boundary_phase,
    default_qh_flow_config,
    evaluate_l2_gate,
    evaluate_qh_flow,
    quarter_hour_boundary_ms,
    summarize_agg_trades,
)\'\'\',
    \'\'\'from utbreakout.qh_flow import (
    QH_FLOW_STRATEGY,
    TRIPLE_ALPHA_STRATEGY,
    boundary_phase as qh_boundary_phase,
    default_qh_flow_config,
    evaluate_l2_gate,
    evaluate_qh_flow,
    quarter_hour_boundary_ms,
    summarize_agg_trades,
)
from utbreakout.crowding_unwind import (
    CROWDING_UNWIND_STRATEGY,
    default_crowding_unwind_config,
    evaluate_crowding_unwind,
)
from utbreakout.strategy_allocator import (
    default_strategy_allocator_config,
    evaluate_strategy_allocation,
    scale_plan_risk,
    summarize_strategy_trades,
)\'\'\',
    \'strategy suite imports\',
)

# Callback allow-list.
replace_once(
    emas,
    \'\'\'    "adaptive",
    "dual",\'\'\',
    \'\'\'    "adaptive",
    "crowd",
    "crowding",
    "crowding_status",
    "dual",\'\'\',
    \'crowding callback allow-list\',
)

# Live strategy registry and labels.
replace_once(
    emas,
    \'\'\'    ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND,
    QH_FLOW_STRATEGY,
    DUAL_ALPHA_STRATEGY,\'\'\',
    \'\'\'    ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND,
    QH_FLOW_STRATEGY,
    CROWDING_UNWIND_STRATEGY,
    DUAL_ALPHA_STRATEGY,\'\'\',
    \'crowding strategy registry\',
)
replace_once(
    emas,
    \'\'\'    QH_FLOW_STRATEGY: \'QH_FLOW\',
    DUAL_ALPHA_STRATEGY: \'DUAL_ALPHA\',\'\'\',
    \'\'\'    QH_FLOW_STRATEGY: \'QH_FLOW_V2\',
    CROWDING_UNWIND_STRATEGY: \'FUNDING_OI_CROWDING_UNWIND\',
    DUAL_ALPHA_STRATEGY: \'DUAL_ALPHA\',\'\'\',
    \'crowding strategy display label\',
)

# Default config.
replace_once(
    emas,
    \'\'\'        \'qh_flow_confirmation_enabled\': True,
        \'l2_gate_enabled\': True,
        \'entry_strategy\': ENTRY_STRATEGY_UT_BREAKOUT,\'\'\',
    \'\'\'        \'qh_flow_confirmation_enabled\': True,
        \'l2_gate_enabled\': True,
        \'crowding_unwind\': default_crowding_unwind_config(),
        \'crowding_unwind_live_enabled\': False,
        \'strategy_allocator\': default_strategy_allocator_config(),
        \'strategy_allocator_enabled\': True,
        \'entry_strategy\': ENTRY_STRATEGY_UT_BREAKOUT,\'\'\',
    \'strategy suite default config\',
)

# Runtime state containers.
replace_once(
    emas,
    \'\'\'        self.qh_flow_last_status = {}  # symbol -> latest QH-Flow status
        self.l2_gate_cache = {}  # symbol -> short-lived shared L2 state
        self.triple_alpha_last_status = {}  # symbol -> latest triple strategy summary\'\'\',
    \'\'\'        self.qh_flow_last_status = {}  # symbol -> latest QH-Flow status
        self.l2_gate_cache = {}  # symbol -> short-lived shared L2 state
        self.l2_gate_history = {}  # symbol -> dynamic L2 replenishment/depletion samples
        self.crowding_unwind_last_status = {}  # symbol -> latest funding/OI unwind status
        self.strategy_allocator_last_status = {}  # strategy -> adaptive risk summary
        self.strategy_allocator_cache = {}  # short-lived finalized trade summaries
        self.triple_alpha_last_status = {}  # symbol -> latest triple strategy summary\'\'\',
    \'strategy suite init state\',
)
replace_once(
    emas,
    \'\'\'        self.qh_flow_last_status = {}
        self.l2_gate_cache = {}
        self.triple_alpha_last_status = {}\'\'\',
    \'\'\'        self.qh_flow_last_status = {}
        self.l2_gate_cache = {}
        self.l2_gate_history = {}
        self.crowding_unwind_last_status = {}
        self.strategy_allocator_last_status = {}
        self.strategy_allocator_cache = {}
        self.triple_alpha_last_status = {}\'\'\',
    \'strategy suite reset state\',
)

# Adaptive allocator is applied once at the common plan store boundary so all
# standalone/Dual/Triple branches share the same final sizing control.
replace_once(
    emas,
    \'\'\'        stored = dict(plan)
        canonical = self._canonical_futures_symbol(\'\'\',
    \'\'\'        stored = dict(plan)
        if not bool(stored.get(\'strategy_allocator_applied\')):
            stored = self._apply_strategy_allocator_to_plan(stored)
        canonical = self._canonical_futures_symbol(\'\'\',
    \'strategy allocator plan hook\',
)

# Add enhanced QH-v2, dynamic L2, allocator and Crowding methods later in the
# class so they override the original v1 methods without deleting stable code.
MARKER = "    def _dual_alpha_strategy_params(self, strategy_params, branch):\\n"
text = emas.read_text(encoding=\'utf-8\')
if MARKER not in text:
    raise SystemExit(\'SignalEngine insertion marker missing\')
METHODS = r\'\'\'
    def _strategy_allocator_runtime_config(self, cfg=None):
        source = dict(cfg or {})
        base = default_strategy_allocator_config()
        nested = source.get(\'strategy_allocator\')
        if isinstance(nested, dict):
            base.update(nested)
        if \'strategy_allocator_enabled\' in source:
            base[\'enabled\'] = bool(source.get(\'strategy_allocator_enabled\'))
        return base

    def _strategy_allocator_key_for_plan(self, plan):
        plan = dict(plan or {})
        if plan.get(\'triple_alpha_agreement_state\') or plan.get(\'triple_alpha_selected_strategy\'):
            return TRIPLE_ALPHA_STRATEGY
        if plan.get(\'dual_alpha_agreement_state\') or plan.get(\'dual_alpha_selected_strategy\'):
            return DUAL_ALPHA_STRATEGY
        return str(plan.get(\'strategy\') or plan.get(\'entry_strategy\') or \'unknown\').strip().lower()

    def _load_strategy_allocator_trades(self):
        store = getattr(self, \'trading_state_store\', None)
        if store is None:
            store = getattr(getattr(self, \'ctrl\', None), \'trading_state_store\', None)
        loader = getattr(store, \'load_trade_results\', None)
        if not callable(loader):
            return []
        try:
            rows = loader()
        except TypeError:
            rows = loader(limit=500)
        except Exception:
            logger.debug(\'strategy allocator trade load failed\', exc_info=True)
            return []
        return list(rows or [])

    def _apply_strategy_allocator_to_plan(self, plan):
        stored = dict(plan or {})
        if stored.get(\'strategy_allocator_applied\'):
            return stored
        try:
            cfg = self._get_utbot_filtered_breakout_config()
        except Exception:
            cfg = {}
        allocator_cfg = self._strategy_allocator_runtime_config(cfg)
        strategy_key = self._strategy_allocator_key_for_plan(stored)
        metrics = summarize_strategy_trades(
            self._load_strategy_allocator_trades(),
            strategy_key,
            allocator_cfg,
        )
        allocation = evaluate_strategy_allocation(metrics, allocator_cfg)
        scaled = scale_plan_risk(stored, allocation.multiplier)
        scaled.update({
            \'strategy_allocator_applied\': True,
            \'strategy_allocator_key\': strategy_key,
            \'strategy_allocator_multiplier\': float(allocation.multiplier),
            \'strategy_allocator_reason\': allocation.reason,
            \'strategy_allocator_metrics\': dict(allocation.metrics),
        })
        if not isinstance(getattr(self, \'strategy_allocator_last_status\', None), dict):
            self.strategy_allocator_last_status = {}
        self.strategy_allocator_last_status[strategy_key] = {
            \'multiplier\': float(allocation.multiplier),
            \'reason\': allocation.reason,
            \'metrics\': dict'
