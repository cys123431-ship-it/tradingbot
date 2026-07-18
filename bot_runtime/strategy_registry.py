"""Strategy identifiers, five-strategy selection, and exchange-mode policy."""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from utbreakout.crowding_unwind import CROWDING_UNWIND_STRATEGY
from utbreakout.liquidation_exhaustion_reversal import LXR_STRATEGY
from utbreakout.qh_flow import QH_FLOW_STRATEGY, QUAD_ALPHA_STRATEGY, TRIPLE_ALPHA_STRATEGY
from utbreakout.relative_strength_pullback import ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND, ENTRY_STRATEGY_UT_BREAKOUT

CORE_ENGINE = 'signal'
UTBOT_FILTERED_BREAKOUT_STRATEGY = 'utbot_filtered_breakout_v1'
UTBOT_ADAPTIVE_TIMEFRAME_STRATEGY = 'utbot_adaptive_timeframe_v1'
DUAL_ALPHA_STRATEGY = 'dual_alpha_v1'
UTBREAKOUT_STRATEGIES = {
    UTBOT_FILTERED_BREAKOUT_STRATEGY,
    UTBOT_ADAPTIVE_TIMEFRAME_STRATEGY,
    ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND,
    QH_FLOW_STRATEGY,
    CROWDING_UNWIND_STRATEGY,
    LXR_STRATEGY,
    DUAL_ALPHA_STRATEGY,
    TRIPLE_ALPHA_STRATEGY,
    QUAD_ALPHA_STRATEGY,
}
UTBREAKOUT_RECENT_LOSS_COOLDOWN_SECONDS = 7200.0
UTBREAKOUT_RECENT_LOSS_COOLDOWN_MIN_LOSS_USDT = 2.0
UTBREAKOUT_RECENT_LOSS_LEGACY_COOLDOWN_SECONDS = 21600.0
UTBREAKOUT_RECENT_LOSS_LEGACY_MIN_LOSS_USDT = 0.0
UT_ONLY_STRATEGIES = {'utbot', 'rsibb', 'utrsibb', 'utrsi', 'utbb', 'utsmc'}
MA_STRATEGIES = set()
PATTERN_STRATEGIES = set(UT_ONLY_STRATEGIES)
CORE_STRATEGIES = set(UT_ONLY_STRATEGIES) | UTBREAKOUT_STRATEGIES
UT_HYBRID_STRATEGIES = {'utrsibb', 'utrsi', 'utbb'}
STATEFUL_UT_STRATEGIES = (
    {'utbot', 'utsmc'}
    | UT_HYBRID_STRATEGIES
    | UTBREAKOUT_STRATEGIES
)
STRATEGY_DISPLAY_NAMES = {
    'sma': 'SMA',
    'hma': 'HMA',
    'cameron': 'CAMERON',
    'utbot': 'UTBOT',
    'utsmc': 'UTSMC',
    'rsibb': 'RSIBB',
    'utrsibb': 'UTRSIBB',
    'utrsi': 'UTRSI',
    'utbb': 'UTBB',
    UTBOT_FILTERED_BREAKOUT_STRATEGY: 'UTBOT_FILTERED_BREAKOUT_V1',
    UTBOT_ADAPTIVE_TIMEFRAME_STRATEGY: 'UTBOT_ADAPTIVE_TIMEFRAME_V1',
    ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND: 'RELATIVE_STRENGTH_PULLBACK_TREND',
    QH_FLOW_STRATEGY: 'QH_FLOW',
    CROWDING_UNWIND_STRATEGY: 'FUNDING_OI_CROWDING_UNWIND',
    LXR_STRATEGY: 'LXR',
    DUAL_ALPHA_STRATEGY: 'DUAL_ALPHA',
    TRIPLE_ALPHA_STRATEGY: 'TRIPLE_ALPHA',
    QUAD_ALPHA_STRATEGY: 'QUAD_ALPHA',
}

QUAD_ALPHA_BRANCH_ORDER = (
    ENTRY_STRATEGY_UT_BREAKOUT,
    ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND,
    QH_FLOW_STRATEGY,
    CROWDING_UNWIND_STRATEGY,
    LXR_STRATEGY,
)
QUAD_ALPHA_BRANCH_LABELS = {
    ENTRY_STRATEGY_UT_BREAKOUT: 'UTBreak',
    ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND: 'RSPT-v3',
    QH_FLOW_STRATEGY: 'QH-Flow v2',
    CROWDING_UNWIND_STRATEGY: 'Crowding Unwind',
    LXR_STRATEGY: 'LXR Reversal',
}
QUAD_ALPHA_SELECTOR_KEYS = {
    'ut': ENTRY_STRATEGY_UT_BREAKOUT,
    'rsp': ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND,
    'qh': QH_FLOW_STRATEGY,
    'crowd': CROWDING_UNWIND_STRATEGY,
    'lxr': LXR_STRATEGY,
}


def normalize_quad_alpha_enabled_strategies(value):
    """Return a stable, de-duplicated subset of the five live alpha branches."""
    if value is None:
        return list(QUAD_ALPHA_BRANCH_ORDER)
    if isinstance(value, str):
        value = [part.strip() for part in value.split(',') if part.strip()]
    if not isinstance(value, (list, tuple, set)):
        return list(QUAD_ALPHA_BRANCH_ORDER)

    aliases = {
        **QUAD_ALPHA_SELECTOR_KEYS,
        'utbreak': ENTRY_STRATEGY_UT_BREAKOUT,
        'utbreakout': ENTRY_STRATEGY_UT_BREAKOUT,
        'rspt': ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND,
        'qhflow': QH_FLOW_STRATEGY,
        'crowding': CROWDING_UNWIND_STRATEGY,
        'liquidation_reversal': LXR_STRATEGY,
        'liquidation-exhaustion-reversal': LXR_STRATEGY,
    }
    requested = set()
    for raw_key in value:
        key = str(raw_key or '').strip().lower()
        canonical = aliases.get(key, key)
        if canonical in QUAD_ALPHA_BRANCH_ORDER:
            requested.add(canonical)
    return [key for key in QUAD_ALPHA_BRANCH_ORDER if key in requested]


def quad_alpha_branch_live_flags(enabled_strategies):
    enabled = set(normalize_quad_alpha_enabled_strategies(enabled_strategies))
    return {key: key in enabled for key in QUAD_ALPHA_BRANCH_ORDER}


def quad_alpha_selection_text(enabled_strategies):
    enabled = set(normalize_quad_alpha_enabled_strategies(enabled_strategies))
    lines = [
        '5-Strategy Alpha selection',
        '',
        'Choose one or more strategies, then press APPLY.',
        'Changes affect new entries only; open-position exits and protection stay active.',
        '',
    ]
    for index, key in enumerate(QUAD_ALPHA_BRANCH_ORDER, start=1):
        marker = 'ON ' if key in enabled else 'OFF'
        lines.append(f'{index}. [{marker}] {QUAD_ALPHA_BRANCH_LABELS[key]}')
    lines.extend([
        '',
        f'Selected: {len(enabled)}/5',
        'At least one strategy must remain selected.',
    ])
    return '\n'.join(lines)


def build_quad_alpha_selection_keyboard(enabled_strategies):
    enabled = set(normalize_quad_alpha_enabled_strategies(enabled_strategies))
    callback_by_strategy = {value: key for key, value in QUAD_ALPHA_SELECTOR_KEYS.items()}
    rows = []
    for index, key in enumerate(QUAD_ALPHA_BRANCH_ORDER, start=1):
        marker = '✅' if key in enabled else '⬜'
        rows.append([
            InlineKeyboardButton(
                f'{marker} {index}. {QUAD_ALPHA_BRANCH_LABELS[key]}',
                callback_data=f'utb:qsel:{callback_by_strategy[key]}',
            )
        ])
    rows.append([
        InlineKeyboardButton('APPLY', callback_data='utb:qsel:apply'),
        InlineKeyboardButton('CANCEL', callback_data='utb:qsel:cancel'),
    ])
    return InlineKeyboardMarkup(rows)

UT_HYBRID_TIMING_LABELS = {
    'utrsibb': 'RSI+BB',
    'utrsi': 'RSI',
    'utbb': 'BB'
}
BINANCE_TESTNET = 'binance_testnet'
BINANCE_MAINNET = 'binance_mainnet'
UPBIT_MODE = 'upbit'
SUPPORTED_EXCHANGE_MODES = {BINANCE_TESTNET, BINANCE_MAINNET, UPBIT_MODE}
EXCHANGE_MODE_DEFAULT_WATCHLISTS = {
    BINANCE_TESTNET: ["BTC/USDT", "ETH/USDT", "SOL/USDT"],
    BINANCE_MAINNET: ["BTC/USDT", "ETH/USDT", "SOL/USDT"],
    UPBIT_MODE: ["BTC/KRW", "ETH/KRW", "SOL/KRW"],
}
EXCHANGE_MODE_SYMBOL_POLICY = {
    BINANCE_TESTNET: {
        "quote": "USDT",
        "market_type": "usdt_perpetual",
        "allow_tradifi": False,
        "allow_upbit_krw": False,
    },
    BINANCE_MAINNET: {
        "quote": "USDT",
        "market_type": "usdt_perpetual",
        "allow_tradifi": True,
        "allow_upbit_krw": False,
    },
    UPBIT_MODE: {
        "quote": "KRW",
        "market_type": "spot",
        "allow_tradifi": False,
        "allow_upbit_krw": True,
    },
}

__all__ = (
    'CORE_ENGINE',
    'UTBOT_FILTERED_BREAKOUT_STRATEGY',
    'UTBOT_ADAPTIVE_TIMEFRAME_STRATEGY',
    'DUAL_ALPHA_STRATEGY',
    'UTBREAKOUT_STRATEGIES',
    'UTBREAKOUT_RECENT_LOSS_COOLDOWN_SECONDS',
    'UTBREAKOUT_RECENT_LOSS_COOLDOWN_MIN_LOSS_USDT',
    'UTBREAKOUT_RECENT_LOSS_LEGACY_COOLDOWN_SECONDS',
    'UTBREAKOUT_RECENT_LOSS_LEGACY_MIN_LOSS_USDT',
    'UT_ONLY_STRATEGIES',
    'MA_STRATEGIES',
    'PATTERN_STRATEGIES',
    'CORE_STRATEGIES',
    'UT_HYBRID_STRATEGIES',
    'STATEFUL_UT_STRATEGIES',
    'STRATEGY_DISPLAY_NAMES',
    'QUAD_ALPHA_BRANCH_ORDER',
    'QUAD_ALPHA_BRANCH_LABELS',
    'QUAD_ALPHA_SELECTOR_KEYS',
    'normalize_quad_alpha_enabled_strategies',
    'quad_alpha_branch_live_flags',
    'quad_alpha_selection_text',
    'build_quad_alpha_selection_keyboard',
    'UT_HYBRID_TIMING_LABELS',
    'BINANCE_TESTNET',
    'BINANCE_MAINNET',
    'UPBIT_MODE',
    'SUPPORTED_EXCHANGE_MODES',
    'EXCHANGE_MODE_DEFAULT_WATCHLISTS',
    'EXCHANGE_MODE_SYMBOL_POLICY',
)
