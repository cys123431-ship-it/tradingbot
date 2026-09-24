import asyncio
from datetime import datetime, timedelta, timezone
import inspect
from types import SimpleNamespace

import ccxt
import pandas as pd
import pytest
from telegram.ext import (
    ApplicationHandlerStop,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

import emas
from bot_runtime.controller_ema200_utbot_rsi import ControllerEMA200UTBotRSIMixin
from bot_runtime.database import DBManager
from bot_runtime.ema200_candidate_selector import (
    auxiliary_ut_score,
    build_ema200_candidate,
    rank_ema200_candidates,
)
from bot_runtime.ema200_profit_stop import ema200_profit_stop_target
from bot_runtime.ema200_utbot_rsi import (
    EMA200_CONSECUTIVE_LOSS_RESET_STATE_KEY,
    EMA200_DAILY_LOSS_RESET_STATE_KEY,
    EMA200_BINANCE_TOP10_BASES,
    EMA200_BINANCE_TOP10_SYMBOLS,
    EMA200_UTBOT_RSI_STRATEGY,
    apply_ema200_daily_loss_reset,
    build_ema200_utbot_rsi_risk_plan,
    calculate_ema200_utbot_rsi_emergency_stop_price,
    count_ema200_consecutive_losses,
    ema200_small_account_margin_percent,
    ema200_kst_date,
    evaluate_ema200_utbot_rsi_entry,
    evaluate_ema200_utbot_rsi_loss_gate,
    get_ema200_consecutive_losses,
    is_ema200_utbot_rsi_symbol_allowed,
    normalize_ema200_utbot_rsi_config,
)
from scripts.reset_ema200_daily_loss import reset_ema200_daily_loss
from scripts.reset_ema200_consecutive_losses import (
    reset_ema200_consecutive_losses,
)
from trading_safety.order_state import (
    DAILY_LOSS_ENTRY_LOCK_KEY,
    OrderRecord,
    OrderState,
    SQLiteTradingStateStore,
)


@pytest.mark.parametrize('raw,expected', [
    ({'exit_timeframe': '30m'}, '30m'),
    ({'exit_timeframe': '1h'}, '1h'),
    ({'exit_timeframe': '2h'}, '15m'),
    ({'exit_timeframe': None}, '15m'),
])
def test_ema200_exit_timeframe_normalizes_to_allowed_completed_bars(raw, expected):
    cfg = normalize_ema200_utbot_rsi_config(raw)
    assert cfg['timeframe'] == '2h'
    assert cfg['exit_timeframe'] == expected


def test_auxiliary_ut_scores_only_rank_already_eligible_candidates():
    assert auxiliary_ut_score('long', {'15m': 'long', '30m': 'short', '1h': 'long'}) == (
        6.0, {'15m': 4.0, '30m': -6.0, '1h': 8.0}
    )
    assert auxiliary_ut_score('short', {})[0] == 0.0
    base = {
        'symbol': 'BTC/USDT:USDT', 'side': 'long', 'ut_age_bars': 1.0,
        'rsi_momentum': 1.0, 'ema_slope_percent': 0.1,
        'quote_volume_24h': 100.0, 'extension_atr': 1.0,
    }
    ranked = rank_ema200_candidates([
        {**base, 'symbol': 'BTC/USDT:USDT', 'auxiliary_ut_biases': {'15m': 'short', '30m': 'short', '1h': 'short'}},
        {**base, 'symbol': 'ETH/USDT:USDT', 'auxiliary_ut_biases': {'15m': 'long', '30m': 'long', '1h': 'long'}},
    ])
    assert ranked[0]['symbol'] == 'ETH/USDT:USDT'
    assert ranked[0]['score_breakdown']['auxiliary_ut'] == 18.0
    assert len(ranked) == 2


@pytest.mark.parametrize('side,mark,roi_floor,stop', [
    ('long', 101.0, None, None),  # exactly +5% margin ROI stays inactive
    ('long', 101.2, 5.0, 101.0),
    ('long', 102.0, 5.0, 101.0),
    ('long', 102.2, 10.0, 102.0),
    ('long', 103.0, 10.0, 102.0),
    ('long', 103.2, 15.0, 103.0),
    ('short', 98.8, 5.0, 99.0),
    ('short', 98.0, 5.0, 99.0),
    ('short', 97.8, 10.0, 98.0),
    ('short', 97.0, 10.0, 98.0),
    ('short', 96.8, 15.0, 97.0),
])
def test_margin_roi_profit_stop_strict_steps(side, mark, roi_floor, stop):
    result = ema200_profit_stop_target(side, 100.0, mark, 5)
    if roi_floor is None:
        assert result is None
    else:
        assert result[1] == roi_floor
        assert result[2] == pytest.approx(stop)


def test_margin_roi_profit_stop_rejects_bad_exchange_position_data():
    for bad in (None, 0, float('nan'), float('inf')):
        assert ema200_profit_stop_target('long', 100, 102, bad) is None


def _ccxt_binance_v3_position_without_leverage():
    exchange = ccxt.binance({'options': {'defaultType': 'future'}})
    market = {
        'id': 'BTCUSDT',
        'symbol': 'BTC/USDT:USDT',
        'base': 'BTC',
        'quote': 'USDT',
        'settle': 'USDT',
        'baseId': 'BTC',
        'quoteId': 'USDT',
        'settleId': 'USDT',
        'type': 'swap',
        'spot': False,
        'margin': False,
        'swap': True,
        'future': False,
        'option': False,
        'active': True,
        'contract': True,
        'linear': True,
        'inverse': False,
        'contractSize': 1.0,
        'precision': {'amount': 0.001, 'price': 0.1, 'base': 0.001, 'quote': 0.1},
        'limits': {},
        'info': {'symbol': 'BTCUSDT'},
    }
    exchange.options.setdefault('leverageBrackets', {})[market['symbol']] = [
        ['0', '0.004'],
    ]
    raw = {
        'symbol': 'BTCUSDT',
        'positionSide': 'BOTH',
        'positionAmt': '1',
        'entryPrice': '100',
        'breakEvenPrice': '100',
        'markPrice': '101.2',
        'unRealizedProfit': '1.2',
        'liquidationPrice': '50',
        'isolatedMargin': '20.24',
        'notional': '101.2',
        'marginAsset': 'USDT',
        'isolatedWallet': '20.24',
        'initialMargin': '20.24',
        'maintMargin': '0.4048',
        'positionInitialMargin': '20.24',
        'openOrderInitialMargin': '0',
        'adl': 1,
        'bidNotional': '0',
        'askNotional': '0',
        'updateTime': 1_700_000_000_000,
    }
    parsed = exchange.parse_position_risk(raw, market)
    assert parsed['leverage'] is None
    assert parsed['entryPrice'] == pytest.approx(100.0)
    assert parsed['markPrice'] == pytest.approx(101.2)
    return exchange, parsed


def test_binance_v3_missing_leverage_is_resolved_from_symbol_config_before_profit_stop():
    exchange, parsed = _ccxt_binance_v3_position_without_leverage()
    exchange.fetch_positions = lambda symbols=None: [parsed]
    exchange.fapiPrivateGetSymbolConfig = lambda params: [{
        'symbol': 'BTCUSDT',
        'marginType': 'ISOLATED',
        'isAutoAddMargin': False,
        'leverage': 5,
        'maxNotionalValue': '1000000',
    }]

    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    engine.exchange = exchange
    engine.position_cache = {}
    engine.is_upbit_mode = lambda: False
    engine._position_entry_strategy = lambda _: EMA200_UTBOT_RSI_STRATEGY
    engine.safe_price = lambda _, price: price
    installed = []

    async def fetch_orders(_):
        return True, []

    async def replace(_symbol, _pos, price, reason):
        installed.append(price)
        return {'id': 'profit-stop-1'}

    async def audit(*args, **kwargs):
        return {'status': 'OK'}

    engine._collect_protection_orders_checked = fetch_orders
    engine._replace_stop_loss_order = replace
    engine._audit_protection_orders = audit

    asyncio.run(engine._ema200_apply_margin_profit_stop('BTC/USDT:USDT'))

    assert installed == [pytest.approx(101.0)]


def test_profit_stop_leverage_lookup_failure_does_not_create_order_and_records_reason():
    exchange, parsed = _ccxt_binance_v3_position_without_leverage()
    exchange.fetch_positions = lambda symbols=None: [parsed]

    def fail_symbol_config(params):
        raise RuntimeError('symbolConfig unavailable')

    exchange.fapiPrivateGetSymbolConfig = fail_symbol_config

    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    engine.exchange = exchange
    engine.position_cache = {}
    engine.is_upbit_mode = lambda: False
    engine._position_entry_strategy = lambda _: EMA200_UTBOT_RSI_STRATEGY
    engine.safe_price = lambda _, price: price
    engine.last_ema200_profit_stop_status = {}
    installed = []

    async def fetch_orders(_):
        return True, []

    async def replace(*args, **kwargs):
        installed.append(args)
        return {'id': 'must-not-exist'}

    engine._collect_protection_orders_checked = fetch_orders
    engine._replace_stop_loss_order = replace

    asyncio.run(engine._ema200_apply_margin_profit_stop('BTC/USDT:USDT'))

    assert installed == []
    status = engine.last_ema200_profit_stop_status['BTC/USDT:USDT']
    assert status['status'] == 'LEVERAGE_UNAVAILABLE'
    assert 'symbolConfig' in status['reason']


def test_profit_stop_rejects_conflicting_exchange_leverage_fields():
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    engine._position_entry_strategy = lambda _: EMA200_UTBOT_RSI_STRATEGY
    engine.last_ema200_profit_stop_status = {}
    pos = {
        'symbol': 'BTC/USDT:USDT',
        'side': 'long',
        'entryPrice': 100.0,
        'markPrice': 101.2,
        'leverage': 5,
        'contracts': 1.0,
        'info': {'leverage': '10'},
    }

    async def fetch_position(_):
        return True, pos

    engine._fetch_server_position_checked = fetch_position
    engine._replace_stop_loss_order = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError('conflicting leverage must not place a stop')
    )

    asyncio.run(engine._ema200_apply_margin_profit_stop('BTC/USDT:USDT'))

    status = engine.last_ema200_profit_stop_status['BTC/USDT:USDT']
    assert status['status'] == 'LEVERAGE_CONFLICT'


async def _run_ema_profit_stop_audit(engine, symbol, pos, order, records):
    cancelled = []

    engine.is_upbit_mode = lambda: False
    engine.exchange = SimpleNamespace(id='fixture')
    engine.ctrl = SimpleNamespace(format_symbol_for_display=lambda value: value)
    engine.last_protection_order_status = {}
    engine.protection_missing_candidates = {}
    engine.last_protection_alert_ts = {}
    engine._get_utbreakout_trailing_state = lambda _: None
    engine.get_runtime_strategy_params = lambda: {'active_strategy': 'utbot'}
    engine.trading_state_store = SimpleNamespace(active_for_symbol=lambda _: records)

    async def collect(_):
        return True, [order]

    async def cancel(_symbol, reason='test', orders=None):
        cancelled.extend(orders or [])
        return len(orders or [])

    engine._collect_protection_orders_checked = collect
    engine._cancel_protection_orders = cancel

    status = await engine._audit_protection_orders(
        symbol,
        pos=pos,
        expected_tp=False,
        expected_sl=True,
        alert=False,
    )
    return status, cancelled


def test_ema_profit_stop_without_ut_trailing_state_is_kept_by_real_audit():
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    symbol = 'BTC/USDT:USDT'
    pos = {
        'symbol': symbol, 'side': 'long', 'entryPrice': 100.0,
        'markPrice': 101.2, 'contracts': 1.0,
    }
    order = {
        'id': 'profit-stop-1',
        'clientOrderId': 'utbslsbtcprofit1',
        'symbol': symbol,
        'type': 'STOP_MARKET',
        'side': 'sell',
        'amount': 1.0,
        'stopPrice': 101.0,
        'reduceOnly': True,
    }
    record = SimpleNamespace(
        strategy=EMA200_UTBOT_RSI_STRATEGY,
        side='long',
        filled_qty=1.0,
        requested_qty=1.0,
        stop_order_id='profit-stop-1',
        metadata={},
    )

    status, cancelled = asyncio.run(
        _run_ema_profit_stop_audit(engine, symbol, pos, order, [record])
    )

    assert cancelled == []
    assert status['sl_present'] is True
    assert status['invalid_price_cancelled'] == 0


def test_manual_or_other_strategy_profit_side_stop_does_not_get_ema_managed_exception():
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    symbol = 'BTC/USDT:USDT'
    pos = {
        'symbol': symbol, 'side': 'long', 'entryPrice': 100.0,
        'markPrice': 101.2, 'contracts': 1.0,
    }
    order = {
        'id': 'manual-stop-1',
        'clientOrderId': 'manual-stop',
        'symbol': symbol,
        'type': 'STOP_MARKET',
        'side': 'sell',
        'amount': 1.0,
        'stopPrice': 101.0,
        'reduceOnly': True,
    }
    other = SimpleNamespace(
        strategy='utbot',
        side='long',
        filled_qty=1.0,
        requested_qty=1.0,
        stop_order_id='manual-stop-1',
        metadata={},
    )

    status, cancelled = asyncio.run(
        _run_ema_profit_stop_audit(engine, symbol, pos, order, [other])
    )

    assert cancelled == [order]
    assert status['invalid_price_cancelled'] == 1


def test_ema_profit_stop_ownership_survives_sqlite_restart(tmp_path):
    path = tmp_path / 'state.sqlite3'
    store = SQLiteTradingStateStore(path)
    store.upsert(OrderRecord(
        client_order_id='entry-1',
        symbol='BTC/USDT:USDT',
        side='long',
        strategy=EMA200_UTBOT_RSI_STRATEGY,
        signal_timestamp='1700000000',
        requested_qty=1.0,
        filled_qty=1.0,
        average_fill_price=100.0,
        order_state=OrderState.PROTECTED.value,
        stop_order_id='profit-stop-1',
    ))
    store.close()

    reopened = SQLiteTradingStateStore(path)
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    symbol = 'BTC/USDT:USDT'
    pos = {
        'symbol': symbol, 'side': 'long', 'entryPrice': 100.0,
        'markPrice': 101.2, 'contracts': 1.0,
    }
    order = {
        'id': 'profit-stop-1',
        'clientOrderId': 'utbslsbtcprofit1',
        'symbol': symbol,
        'type': 'STOP_MARKET',
        'side': 'sell',
        'amount': 1.0,
        'stopPrice': 101.0,
        'reduceOnly': True,
    }
    cancelled = []
    engine.is_upbit_mode = lambda: False
    engine.exchange = SimpleNamespace(id='fixture')
    engine.ctrl = SimpleNamespace(format_symbol_for_display=lambda value: value)
    engine.last_protection_order_status = {}
    engine.protection_missing_candidates = {}
    engine.last_protection_alert_ts = {}
    engine._get_utbreakout_trailing_state = lambda _: None
    engine.get_runtime_strategy_params = lambda: {'active_strategy': 'utbot'}
    engine.trading_state_store = reopened

    async def collect(_):
        return True, [order]

    async def cancel(_symbol, reason='test', orders=None):
        cancelled.extend(orders or [])
        return len(orders or [])

    engine._collect_protection_orders_checked = collect
    engine._cancel_protection_orders = cancel

    status = asyncio.run(engine._audit_protection_orders(
        symbol, pos=pos, expected_tp=False, expected_sl=True, alert=False
    ))

    assert cancelled == []
    assert status['sl_present'] is True
    reopened.close()


@pytest.mark.parametrize(
    ('record_stop_id', 'record_side', 'order_amount'),
    [
        ('different-stop', 'long', 1.0),
        ('profit-stop-1', 'short', 1.0),
        ('profit-stop-1', 'long', 0.5),
    ],
)
def test_ema_profit_stop_exception_requires_matching_order_side_and_quantity(
    record_stop_id,
    record_side,
    order_amount,
):
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    symbol = 'BTC/USDT:USDT'
    pos = {
        'symbol': symbol, 'side': 'long', 'entryPrice': 100.0,
        'markPrice': 101.2, 'contracts': 1.0,
    }
    order = {
        'id': 'profit-stop-1',
        'clientOrderId': 'utbslsbtcprofit1',
        'symbol': symbol,
        'type': 'STOP_MARKET',
        'side': 'sell',
        'amount': order_amount,
        'stopPrice': 101.0,
        'reduceOnly': True,
    }
    record = SimpleNamespace(
        strategy=EMA200_UTBOT_RSI_STRATEGY,
        side=record_side,
        filled_qty=1.0,
        requested_qty=1.0,
        stop_order_id=record_stop_id,
        metadata={},
    )

    status, cancelled = asyncio.run(
        _run_ema_profit_stop_audit(engine, symbol, pos, order, [record])
    )

    assert cancelled == [order]
    assert status['invalid_price_cancelled'] == 1


def test_ema_profit_stop_verified_client_id_restores_ownership_when_order_id_changes():
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    symbol = 'BTC/USDT:USDT'
    pos = {
        'symbol': symbol, 'side': 'short', 'entryPrice': 100.0,
        'markPrice': 98.8, 'contracts': 2.0,
    }
    order = {
        'id': 'new-algo-order-id',
        'clientOrderId': 'utbslsbtcstableclient',
        'symbol': symbol,
        'type': 'STOP_MARKET',
        'side': 'buy',
        'amount': 2.0,
        'stopPrice': 99.0,
        'reduceOnly': True,
    }
    record = SimpleNamespace(
        strategy=EMA200_UTBOT_RSI_STRATEGY,
        side='short',
        filled_qty=2.0,
        requested_qty=2.0,
        stop_order_id='old-algo-order-id',
        metadata={
            'ema200_profit_stop_client_order_id': 'utbslsbtcstableclient',
        },
    )

    status, cancelled = asyncio.run(
        _run_ema_profit_stop_audit(engine, symbol, pos, order, [record])
    )

    assert cancelled == []
    assert status['sl_present'] is True


def test_profit_stop_install_path_persists_identity_then_real_audit_keeps_it(tmp_path):
    symbol = 'BTC/USDT:USDT'
    path = tmp_path / 'state.sqlite3'
    store = SQLiteTradingStateStore(path)
    store.upsert(OrderRecord(
        client_order_id='entry-integrated',
        symbol=symbol,
        side='long',
        strategy=EMA200_UTBOT_RSI_STRATEGY,
        signal_timestamp='1700000000',
        requested_qty=1.0,
        filled_qty=1.0,
        average_fill_price=100.0,
        order_state=OrderState.PROTECTED.value,
    ))

    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    engine.trading_state_store = store
    engine.is_upbit_mode = lambda: False
    engine.ctrl = SimpleNamespace(format_symbol_for_display=lambda value: value)
    engine.last_protection_order_status = {}
    engine.protection_missing_candidates = {}
    engine.last_protection_alert_ts = {}
    engine._get_utbreakout_trailing_state = lambda _: None
    engine.get_runtime_strategy_params = lambda: {
        'active_strategy': EMA200_UTBOT_RSI_STRATEGY,
        'EMA200UTBotRSI2H': {'enabled': False},
    }
    engine.safe_amount = lambda _symbol, amount: float(amount)
    engine.safe_price = lambda _symbol, price: float(price)
    engine.PROTECTION_REPLACE_CONFIRM_DELAY = 0
    engine._persist_active_entry_protection_refs = lambda *args, **kwargs: None

    pos = {
        'symbol': symbol,
        'side': 'long',
        'entryPrice': 100.0,
        'markPrice': 101.2,
        'contracts': 1.0,
    }
    orders = []

    def create_order(_symbol, order_type, side, qty, price, params):
        order = {
            'id': 'profit-stop-integrated',
            'clientOrderId': params['newClientOrderId'],
            'symbol': _symbol,
            'type': str(order_type).upper(),
            'side': side,
            'amount': float(qty),
            'stopPrice': float(params['stopPrice']),
            'reduceOnly': bool(params.get('reduceOnly')),
        }
        orders.append(order)
        return order

    engine.exchange = SimpleNamespace(id='fixture', create_order=create_order)

    async def collect(_symbol):
        return True, list(orders)

    async def fetch_position(_symbol):
        return True, dict(pos)

    async def cancel(_symbol, reason='test', orders=None):
        return 0

    engine._collect_protection_orders_checked = collect
    engine._fetch_server_position_checked = fetch_position
    engine._cancel_protection_orders = cancel

    replacement = asyncio.run(engine._replace_stop_loss_order(
        symbol,
        pos,
        101.0,
        reason='EMA200 margin ROI 6.00% locks 5%',
    ))
    assert replacement is not None

    persisted = store.get('entry-integrated')
    assert persisted.stop_order_id == 'profit-stop-integrated'
    assert persisted.metadata['ema200_profit_stop_client_order_id'] == (
        replacement['clientOrderId']
    )

    audit = asyncio.run(engine._audit_protection_orders(
        symbol,
        pos=pos,
        expected_tp=False,
        expected_sl=True,
        alert=False,
    ))
    assert audit['sl_present'] is True
    assert audit['invalid_price_cancelled'] == 0
    store.close()


def test_first_profit_stop_install_exception_is_recorded_without_new_forced_close():
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    symbol = 'BTC/USDT:USDT'
    pos = {
        'symbol': symbol, 'side': 'long', 'entryPrice': 100.0,
        'markPrice': 101.2, 'leverage': 5, 'contracts': 1.0,
    }
    engine._position_entry_strategy = lambda _: EMA200_UTBOT_RSI_STRATEGY
    engine.safe_price = lambda _, price: price
    engine.last_ema200_profit_stop_status = {}

    async def fetch_position(_):
        return True, pos

    async def collect(_):
        return True, []

    async def replace(*args, **kwargs):
        raise TimeoutError('simulated lost response')

    async def forbidden_fail_close(*args, **kwargs):
        raise AssertionError('first profit stop failure must not add a new forced-close rule')

    engine._fetch_server_position_checked = fetch_position
    engine._collect_protection_orders_checked = collect
    engine._replace_stop_loss_order = replace
    engine._fail_closed_unprotected_position = forbidden_fail_close

    asyncio.run(engine._ema200_apply_margin_profit_stop(symbol))

    status = engine.last_ema200_profit_stop_status[symbol]
    assert status['status'] == 'UPDATE_FAILED'
    assert 'TimeoutError' in status['reason']


@pytest.mark.parametrize(
    ('side', 'mark'),
    [('long', 101.24), ('short', 98.82)],
)
def test_profit_stop_exchange_price_rounding_keeps_protective_direction(side, mark):
    entry = 100.03
    target = ema200_profit_stop_target(side, entry, mark, 5)
    assert target is not None
    rounded = round(target[2], 1)
    if side == 'long':
        assert entry < rounded < mark
    else:
        assert mark < rounded < entry



def _ema_replacement_race_fixture(initial_stop=None):
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    symbol = 'BTC/USDT:USDT'
    pos = {
        'symbol': symbol,
        'side': 'long',
        'entryPrice': 100.0,
        'markPrice': 103.0,
        'contracts': 1.0,
    }
    orders = []
    submissions = []
    cancellations = []
    if initial_stop is not None:
        orders.append({
            'id': 'initial-stop',
            'clientOrderId': 'utbsl-initial',
            'symbol': symbol,
            'type': 'STOP_MARKET',
            'side': 'sell',
            'amount': 1.0,
            'stopPrice': float(initial_stop),
            'reduceOnly': True,
            'timestamp': 1,
        })

    class Exchange:
        id = 'fixture'

        def create_order(self, _symbol, order_type, side, qty, price, params):
            order = {
                'id': f'created-{len(submissions) + 1}',
                'clientOrderId': params['newClientOrderId'],
                'symbol': _symbol,
                'type': str(order_type).upper(),
                'side': side,
                'amount': float(qty),
                'stopPrice': float(params['stopPrice']),
                'reduceOnly': bool(params.get('reduceOnly')),
                'timestamp': 100 + len(submissions),
            }
            submissions.append(order)
            orders.append(order)
            return order

    engine.exchange = Exchange()
    engine.is_upbit_mode = lambda: False
    engine.ctrl = SimpleNamespace(
        format_symbol_for_display=lambda value: value,
        notify=lambda *args, **kwargs: asyncio.sleep(0),
    )
    engine.safe_amount = lambda _symbol, amount: float(amount)
    engine.safe_price = lambda _symbol, price: float(price)
    engine.PROTECTION_REPLACE_CONFIRM_DELAY = 0
    engine.last_protection_order_status = {}
    engine.last_protection_alert_ts = {}
    engine._persist_active_entry_protection_refs = lambda *args, **kwargs: None

    async def collect(_symbol):
        return True, list(orders)

    async def cancel(_symbol, reason='test', orders=None):
        selected = list(orders or [])
        cancellations.extend(selected)
        for order in selected:
            if order in globals().get('_never_used', []):
                pass
        for order in selected:
            try:
                # list.remove is intentionally used here so the fake exchange
                # mirrors a confirmed cancel before the next snapshot.
                locals_orders.remove(order)
            except ValueError:
                pass
        return len(selected)

    # Keep the mutable list under a non-shadowed name for the cancellation fake.
    locals_orders = orders

    async def fetch_position(_symbol):
        return True, dict(pos)

    engine._collect_protection_orders_checked = collect
    engine._cancel_protection_orders = cancel
    engine._fetch_server_position_checked = fetch_position
    return engine, symbol, pos, orders, submissions, cancellations


def test_ema200_profit_stop_fails_closed_in_binance_hedge_mode():
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    symbol = 'BTC/USDT:USDT'
    pos = {
        'symbol': symbol,
        'side': 'long',
        'entryPrice': 100.0,
        'markPrice': 103.0,
        'contracts': 1.0,
        'liquidationPrice': 50.0,
    }
    locks = []
    mutations = []
    engine.exchange = SimpleNamespace(
        id='binance',
        fetch_position_mode=lambda _symbol=None: {'hedged': True},
    )
    engine.safe_amount = lambda _symbol, amount: float(amount)
    engine.safe_price = lambda _symbol, price: float(price)
    engine.last_protection_order_status = {}
    engine.last_ema200_profit_stop_status = {}
    engine._set_crypto_entry_lock = lambda reason: locks.append(reason)
    engine._fetch_position_with_liquidation = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError('hedge mode must fail before liquidation/order mutation')
    )
    engine._cancel_protection_orders = lambda *args, **kwargs: (
        mutations.append('cancel') or asyncio.sleep(0, result=1)
    )

    result = asyncio.run(engine._replace_stop_loss_order(
        symbol,
        pos,
        101.0,
        reason='EMA200 margin ROI 6.00% locks 5%',
    ))

    assert result is None
    assert mutations == []
    assert any('UNSUPPORTED_HEDGE_MODE' in value for value in locks)
    assert engine.last_ema200_profit_stop_status[symbol]['status'] == (
        'UNSUPPORTED_HEDGE_MODE'
    )


def test_ema200_profit_stop_fails_closed_when_position_mode_lookup_fails():
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    symbol = 'BTC/USDT:USDT'
    pos = {
        'symbol': symbol,
        'side': 'long',
        'entryPrice': 100.0,
        'markPrice': 103.0,
        'contracts': 1.0,
        'liquidationPrice': 50.0,
    }
    locks = []
    engine.exchange = SimpleNamespace(
        id='binance',
        fetch_position_mode=lambda _symbol=None: (_ for _ in ()).throw(
            TimeoutError('position mode timeout')
        ),
    )
    engine.safe_amount = lambda _symbol, amount: float(amount)
    engine.safe_price = lambda _symbol, price: float(price)
    engine.last_protection_order_status = {}
    engine.last_ema200_profit_stop_status = {}
    engine._set_crypto_entry_lock = lambda reason: locks.append(reason)
    engine._fetch_position_with_liquidation = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError('unknown position mode must fail before order mutation')
    )

    result = asyncio.run(engine._replace_stop_loss_order(
        symbol,
        pos,
        101.0,
        reason='EMA200 margin ROI 6.00% locks 5%',
    ))

    assert result is None
    assert any('POSITION_MODE_UNAVAILABLE' in value for value in locks)
    assert engine.last_ema200_profit_stop_status[symbol]['status'] == (
        'POSITION_MODE_UNAVAILABLE'
    )


def test_binance_one_way_mode_allows_ema200_profit_stop_replacement_path():
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    symbol = 'BTC/USDT:USDT'
    pos = {
        'symbol': symbol,
        'side': 'long',
        'entryPrice': 100.0,
        'markPrice': 103.0,
        'contracts': 1.0,
        'liquidationPrice': 50.0,
    }
    created = []
    engine.exchange = SimpleNamespace(
        id='binance',
        fetch_position_mode=lambda _symbol=None: {'hedged': False},
    )
    engine.safe_amount = lambda _symbol, amount: float(amount)
    engine.safe_price = lambda _symbol, price: float(price)
    engine.last_protection_order_status = {}
    engine.last_ema200_profit_stop_status = {}
    engine.last_protection_alert_ts = {}
    engine._set_crypto_entry_lock = lambda reason: None

    async def fetch_liquidation(_symbol, _pos):
        return True, dict(pos)

    engine._fetch_position_with_liquidation = fetch_liquidation
    engine._validate_position_stop_liquidation = lambda *args, **kwargs: SimpleNamespace(
        valid=True, reason='SAFE'
    )
    engine._collect_protection_orders_checked = lambda _symbol: asyncio.sleep(
        0, result=(True, [])
    )
    engine._fetch_server_position_checked = lambda _symbol: asyncio.sleep(
        0, result=(True, dict(pos))
    )
    engine._cancel_protection_orders = lambda *args, **kwargs: asyncio.sleep(
        0, result=0
    )

    async def create(*args, **kwargs):
        order = {
            'id': 'one-way-stop',
            'clientOrderId': 'utbsl-one-way',
            'symbol': symbol,
            'type': 'STOP_MARKET',
            'side': 'sell',
            'amount': 1.0,
            'stopPrice': 101.0,
            'reduceOnly': True,
        }
        created.append(order)
        return order

    engine._create_protection_order_with_retries = create
    engine._persist_active_entry_protection_refs = lambda *args, **kwargs: None
    engine._persist_ema200_profit_stop_identity = lambda *args, **kwargs: None

    result = asyncio.run(engine._replace_stop_loss_order(
        symbol,
        pos,
        101.0,
        reason='EMA200 margin ROI 6.00% locks 5%',
    ))

    assert result['id'] == 'one-way-stop'
    assert len(created) == 1


def test_ema200_entry_source_checks_one_way_mode_before_position_lookup():
    source = inspect.getsource(emas.SignalEngine.entry)
    mode_guard = source.index('_require_binance_one_way_mode')
    position_lookup = source.index('self.exchange.fetch_positions')
    assert mode_guard < position_lookup


def test_concurrent_profit_stop_replacements_keep_best_stop():
    engine, symbol, pos, orders, submissions, cancellations = (
        _ema_replacement_race_fixture(initial_stop=100.5)
    )

    async def scenario():
        return await asyncio.gather(
            engine._replace_stop_loss_order(
                symbol, pos, 101.0,
                reason='EMA200 margin ROI 6.00% locks 5%',
            ),
            engine._replace_stop_loss_order(
                symbol, pos, 102.0,
                reason='EMA200 margin ROI 11.00% locks 10%',
            ),
        )

    asyncio.run(scenario())

    open_stops = [
        float(order['stopPrice'])
        for order in orders
        if engine._classify_protection_order(order) == 'sl'
    ]
    assert open_stops == [pytest.approx(102.0)]
    assert max(float(order['stopPrice']) for order in cancellations) < 102.0
    assert len([o for o in submissions if float(o['stopPrice']) == 102.0]) == 1


def test_same_target_concurrent_profit_stop_replacement_submits_once():
    engine, symbol, pos, orders, submissions, _ = (
        _ema_replacement_race_fixture(initial_stop=100.5)
    )

    async def scenario():
        return await asyncio.gather(
            engine._replace_stop_loss_order(
                symbol, pos, 101.0,
                reason='EMA200 margin ROI 6.00% locks 5%',
            ),
            engine._replace_stop_loss_order(
                symbol, pos, 101.0,
                reason='EMA200 margin ROI 6.10% locks 5%',
            ),
        )

    asyncio.run(scenario())

    assert [float(order['stopPrice']) for order in orders] == [
        pytest.approx(101.0)
    ]
    assert len(submissions) == 1


def test_better_stop_appearing_during_cancel_confirmation_is_preserved():
    engine, symbol, pos, orders, submissions, cancellations = (
        _ema_replacement_race_fixture(initial_stop=100.5)
    )
    snapshots = 0
    original_collect = engine._collect_protection_orders_checked

    async def collect_with_external_winner(_symbol):
        nonlocal snapshots
        snapshots += 1
        if snapshots == 2:
            orders.append({
                'id': 'external-better-stop',
                'clientOrderId': 'utbsl-external-better',
                'symbol': symbol,
                'type': 'STOP_MARKET',
                'side': 'sell',
                'amount': 1.0,
                'stopPrice': 102.0,
                'reduceOnly': True,
                'timestamp': 999,
            })
        return await original_collect(_symbol)

    engine._collect_protection_orders_checked = collect_with_external_winner

    result = asyncio.run(engine._replace_stop_loss_order(
        symbol,
        pos,
        101.0,
        reason='EMA200 margin ROI 6.00% locks 5%',
    ))

    assert result['id'] == 'external-better-stop'
    assert any(order['id'] == 'external-better-stop' for order in orders)
    assert not any(
        order['id'] == 'external-better-stop' for order in cancellations
    )
    assert submissions == []


def test_pending_profit_stop_identity_survives_crash_and_restart_audit(tmp_path):
    symbol = 'BTC/USDT:USDT'
    path = tmp_path / 'state.sqlite3'
    store = SQLiteTradingStateStore(path)
    store.upsert(OrderRecord(
        client_order_id='entry-crash-window',
        symbol=symbol,
        side='long',
        strategy=EMA200_UTBOT_RSI_STRATEGY,
        signal_timestamp='1700000000',
        requested_qty=1.0,
        filled_qty=1.0,
        average_fill_price=100.0,
        order_state=OrderState.PROTECTED.value,
    ))

    engine, _, pos, orders, _, _ = _ema_replacement_race_fixture()
    engine.trading_state_store = store

    def crash_before_confirmed_persist(*args, **kwargs):
        raise RuntimeError('simulated process death after exchange accept')

    engine._persist_active_entry_protection_refs = crash_before_confirmed_persist

    with pytest.raises(RuntimeError, match='simulated process death'):
        asyncio.run(engine._replace_stop_loss_order(
            symbol,
            pos,
            101.0,
            reason='EMA200 margin ROI 6.00% locks 5%',
        ))

    persisted = store.get('entry-crash-window')
    pending_client_id = persisted.metadata[
        'ema200_profit_stop_pending_client_order_id'
    ]
    assert pending_client_id
    assert any(
        order['clientOrderId'] == pending_client_id
        for order in orders
    )
    store.close()

    reopened = SQLiteTradingStateStore(path)
    restarted = emas.SignalEngine.__new__(emas.SignalEngine)
    restarted.is_upbit_mode = lambda: False
    restarted.exchange = SimpleNamespace(id='fixture')
    restarted.ctrl = SimpleNamespace(format_symbol_for_display=lambda value: value)
    restarted.last_protection_order_status = {}
    restarted.protection_missing_candidates = {}
    restarted.last_protection_alert_ts = {}
    restarted._get_utbreakout_trailing_state = lambda _: None
    restarted.get_runtime_strategy_params = lambda: {'active_strategy': 'utbot'}
    restarted.trading_state_store = reopened
    cancelled = []

    async def collect(_symbol):
        return True, list(orders)

    async def cancel(_symbol, reason='test', orders=None):
        cancelled.extend(orders or [])
        return len(orders or [])

    restarted._collect_protection_orders_checked = collect
    restarted._cancel_protection_orders = cancel

    status = asyncio.run(restarted._audit_protection_orders(
        symbol,
        pos=pos,
        expected_tp=False,
        expected_sl=True,
        alert=False,
    ))

    assert cancelled == []
    assert status['sl_present'] is True
    reopened.close()


def test_flat_not_found_active_pending_profit_stop_identity_is_preserved(tmp_path):
    symbol = 'BTC/USDT:USDT'
    store = SQLiteTradingStateStore(tmp_path / 'state.sqlite3')
    store.upsert(OrderRecord(
        client_order_id='entry-pending-not-found',
        symbol=symbol,
        side='long',
        strategy=EMA200_UTBOT_RSI_STRATEGY,
        signal_timestamp='1700000000',
        requested_qty=1.0,
        filled_qty=1.0,
        average_fill_price=100.0,
        order_state=OrderState.PROTECTED.value,
        metadata={
            'ema200_profit_stop_pending_client_order_id': 'pending-not-found',
            'ema200_profit_stop_pending_side': 'long',
            'ema200_profit_stop_pending_qty': 1.0,
            'ema200_profit_stop_pending_trigger_price': 101.0,
        },
    ))

    class Exchange:
        id = 'binance'
        def market(self, _symbol):
            return {'id': 'BTCUSDT'}
        def fapiPrivateGetAlgoOrder(self, params):
            raise RuntimeError('-2013 Order does not exist.')

    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    engine.exchange = Exchange()
    engine.trading_state_store = store
    engine._set_crypto_entry_lock = lambda reason: None

    result = asyncio.run(engine._reconcile_ema200_profit_stop_pending_identity(
        symbol,
        pos=None,
        protection_orders=[],
    ))

    assert result['status'] == 'NOT_FOUND_PRESERVED'
    record = store.get('entry-pending-not-found')
    assert record.metadata['ema200_profit_stop_pending_client_order_id'] == (
        'pending-not-found'
    )
    store.close()


def test_unknown_pending_profit_stop_lookup_is_preserved_and_locks_entries(tmp_path):
    symbol = 'BTC/USDT:USDT'
    store = SQLiteTradingStateStore(tmp_path / 'state.sqlite3')
    store.upsert(OrderRecord(
        client_order_id='entry-pending-unknown',
        symbol=symbol,
        side='long',
        strategy=EMA200_UTBOT_RSI_STRATEGY,
        signal_timestamp='1700000000',
        requested_qty=1.0,
        filled_qty=1.0,
        average_fill_price=100.0,
        order_state=OrderState.PROTECTED.value,
        metadata={
            'ema200_profit_stop_pending_client_order_id': 'pending-unknown',
            'ema200_profit_stop_pending_side': 'long',
            'ema200_profit_stop_pending_qty': 1.0,
            'ema200_profit_stop_pending_trigger_price': 101.0,
        },
    ))

    class Exchange:
        id = 'binance'
        def market(self, _symbol):
            return {'id': 'BTCUSDT'}
        def fapiPrivateGetAlgoOrder(self, params):
            raise TimeoutError('lookup timeout')

    locks = []
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    engine.exchange = Exchange()
    engine.trading_state_store = store
    engine._set_crypto_entry_lock = lambda reason: locks.append(reason)

    result = asyncio.run(engine._reconcile_ema200_profit_stop_pending_identity(
        symbol,
        pos=None,
        protection_orders=[],
    ))

    assert result['status'] == 'UNKNOWN'
    record = store.get('entry-pending-unknown')
    assert record.metadata['ema200_profit_stop_pending_client_order_id'] == (
        'pending-unknown'
    )
    assert any('PENDING_PROTECTION_LOOKUP_UNKNOWN' in value for value in locks)
    store.close()


def test_partial_position_reduction_never_keeps_oversized_profit_stop_as_managed():
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    symbol = 'BTC/USDT:USDT'
    pos = {
        'symbol': symbol,
        'side': 'long',
        'entryPrice': 100.0,
        'markPrice': 101.2,
        'contracts': 1.0,
    }
    order = {
        'id': 'oversized-profit-stop',
        'clientOrderId': 'utbsl-oversized',
        'symbol': symbol,
        'type': 'STOP_MARKET',
        'side': 'sell',
        'amount': 2.0,
        'stopPrice': 101.0,
        'reduceOnly': True,
    }
    record = SimpleNamespace(
        strategy=EMA200_UTBOT_RSI_STRATEGY,
        side='long',
        filled_qty=2.0,
        requested_qty=2.0,
        stop_order_id='oversized-profit-stop',
        metadata={},
    )

    status, cancelled = asyncio.run(
        _run_ema_profit_stop_audit(engine, symbol, pos, order, [record])
    )

    assert cancelled == [order]
    assert status['sl_present'] is False



def test_profit_stop_apply_preserves_hedge_mode_failure_status():
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    symbol = 'BTC/USDT:USDT'
    pos = {
        'symbol': symbol,
        'side': 'long',
        'entryPrice': 100.0,
        'markPrice': 101.2,
        'leverage': 5,
        'contracts': 1.0,
        'liquidationPrice': 50.0,
    }
    locks = []
    engine.exchange = SimpleNamespace(
        id='binance',
        fetch_position_mode=lambda _symbol=None: {'hedged': True},
    )
    engine.last_ema200_profit_stop_status = {}
    engine._position_entry_strategy = lambda _: EMA200_UTBOT_RSI_STRATEGY
    engine._fetch_server_position_checked = lambda _symbol: asyncio.sleep(
        0, result=(True, dict(pos))
    )
    engine._collect_protection_orders_checked = lambda _symbol: asyncio.sleep(
        0, result=(True, [])
    )
    engine.safe_price = lambda _symbol, price: float(price)
    engine.safe_amount = lambda _symbol, amount: float(amount)
    engine._set_crypto_entry_lock = lambda reason: locks.append(reason)

    asyncio.run(engine._ema200_apply_margin_profit_stop(symbol))

    assert engine.last_ema200_profit_stop_status[symbol]['status'] == (
        'UNSUPPORTED_HEDGE_MODE'
    )
    assert any('UNSUPPORTED_HEDGE_MODE' in value for value in locks)


def test_pending_unknown_blocks_new_profit_stop_client_id_and_submit(tmp_path):
    symbol = 'BTC/USDT:USDT'
    store = SQLiteTradingStateStore(tmp_path / 'state.sqlite3')
    store.upsert(OrderRecord(
        client_order_id='entry-pending-submit-block',
        symbol=symbol,
        side='long',
        strategy=EMA200_UTBOT_RSI_STRATEGY,
        signal_timestamp='1700000000',
        requested_qty=1.0,
        filled_qty=1.0,
        average_fill_price=100.0,
        order_state=OrderState.PROTECTED.value,
        metadata={
            'ema200_profit_stop_pending_client_order_id': 'pending-old-client',
            'ema200_profit_stop_pending_side': 'long',
            'ema200_profit_stop_pending_qty': 1.0,
            'ema200_profit_stop_pending_trigger_price': 101.0,
        },
    ))

    submissions = []

    class Exchange:
        id = 'binance'

        def fetch_position_mode(self, _symbol=None):
            return {'hedged': False}

        def market(self, _symbol):
            return {'id': 'BTCUSDT'}

        def fapiPrivateGetAlgoOrder(self, params):
            raise TimeoutError('pending lookup timeout')

        def fapiPrivatePostAlgoOrder(self, params):
            submissions.append(dict(params))
            raise AssertionError('pending UNKNOWN must block a new submission')

    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    engine.exchange = Exchange()
    engine.trading_state_store = store
    engine.last_protection_order_status = {}
    engine.last_ema200_profit_stop_status = {}
    engine.last_protection_alert_ts = {}
    engine.safe_amount = lambda _symbol, amount: float(amount)
    engine.safe_price = lambda _symbol, price: float(price)
    engine._fetch_position_with_liquidation = lambda _symbol, pos: asyncio.sleep(
        0, result=(True, dict(pos))
    )
    engine._validate_position_stop_liquidation = lambda *args, **kwargs: SimpleNamespace(
        valid=True, reason='SAFE'
    )
    engine._collect_protection_orders_checked = lambda _symbol: asyncio.sleep(
        0, result=(True, [])
    )
    locks = []
    engine._set_crypto_entry_lock = lambda reason: locks.append(reason)

    pos = {
        'symbol': symbol,
        'side': 'long',
        'entryPrice': 100.0,
        'markPrice': 103.0,
        'contracts': 1.0,
        'liquidationPrice': 50.0,
    }
    result = asyncio.run(engine._replace_stop_loss_order(
        symbol,
        pos,
        102.0,
        reason='EMA200 margin ROI 11.00% locks 10%',
    ))

    assert result is None
    assert submissions == []
    record = store.get('entry-pending-submit-block')
    assert record.metadata['ema200_profit_stop_pending_client_order_id'] == (
        'pending-old-client'
    )
    assert any('PENDING_PROTECTION' in value for value in locks)
    store.close()



def _binance_audit_mode_fixture(*, hedged=None, mode_error=None):
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    symbol = 'BTC/USDT:USDT'
    pos = {
        'symbol': symbol,
        'side': 'long',
        'entryPrice': 100.0,
        'markPrice': 103.0,
        'liquidationPrice': 50.0,
        'contracts': 1.0,
    }
    order = {
        'id': 'hedge-stop-1',
        'clientOrderId': 'hedge-stop-client',
        'symbol': symbol,
        'type': 'STOP_MARKET',
        'side': 'sell',
        'amount': 1.0,
        'stopPrice': 95.0,
        'reduceOnly': False,
        'positionSide': 'LONG',
    }
    cancelled = []
    locks = []

    class Exchange:
        id = 'binance'

        def fetch_position_mode(self, _symbol=None):
            if mode_error is not None:
                raise mode_error
            return {'hedged': hedged}

        def market(self, _symbol):
            return {
                'id': 'BTCUSDT',
                'precision': {'price': 0.1},
                'info': {
                    'filters': [{
                        'filterType': 'PRICE_FILTER',
                        'tickSize': '0.1',
                    }],
                },
            }

    engine.exchange = Exchange()
    engine.is_upbit_mode = lambda: False
    engine.ctrl = SimpleNamespace(format_symbol_for_display=lambda value: value)
    engine.last_protection_order_status = {}
    engine.protection_missing_candidates = {}
    engine.last_protection_alert_ts = {}
    engine._get_utbreakout_trailing_state = lambda _: None
    engine.get_runtime_strategy_params = lambda: {'active_strategy': 'utbot'}
    engine.trading_state_store = SimpleNamespace(active_for_symbol=lambda _: [])
    engine._set_crypto_entry_lock = lambda reason: locks.append(reason)

    async def collect(_symbol):
        return True, [order]

    async def cancel(_symbol, reason='test', orders=None):
        cancelled.extend(list(orders or []))
        return len(list(orders or []))

    engine._collect_protection_orders_checked = collect
    engine._cancel_protection_orders = cancel
    return engine, symbol, pos, order, cancelled, locks


def test_audit_does_not_cancel_existing_stop_in_binance_hedge_mode():
    engine, symbol, pos, _, cancelled, locks = _binance_audit_mode_fixture(
        hedged=True
    )

    status = asyncio.run(engine._audit_protection_orders(
        symbol,
        pos=pos,
        expected_tp=False,
        expected_sl=True,
        alert=False,
    ))

    assert cancelled == []
    assert status['status'] == 'UNSUPPORTED_HEDGE_MODE'
    assert any('UNSUPPORTED_HEDGE_MODE' in reason for reason in locks)


def test_audit_does_not_cancel_existing_stop_when_position_mode_unknown():
    engine, symbol, pos, _, cancelled, locks = _binance_audit_mode_fixture(
        mode_error=TimeoutError('position mode timeout')
    )

    status = asyncio.run(engine._audit_protection_orders(
        symbol,
        pos=pos,
        expected_tp=False,
        expected_sl=True,
        alert=False,
    ))

    assert cancelled == []
    assert status['status'] == 'POSITION_MODE_UNAVAILABLE'
    assert any('POSITION_MODE_UNAVAILABLE' in reason for reason in locks)


@pytest.mark.parametrize(
    ('side', 'entry_price', 'mark_price', 'stops', 'expected_keep', 'expected_cancel'),
    [
        ('long', 105.0, 103.0, [(102.0, 1), (101.0, 2)], 102.0, 101.0),
        ('short', 95.0, 97.0, [(98.0, 1), (99.0, 2)], 98.0, 99.0),
    ],
)
def test_audit_duplicate_stops_keeps_most_protective_not_newest(
    side,
    entry_price,
    mark_price,
    stops,
    expected_keep,
    expected_cancel,
):
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    symbol = 'BTC/USDT:USDT'
    pos = {
        'symbol': symbol,
        'side': side,
        'entryPrice': entry_price,
        'markPrice': mark_price,
        'contracts': 1.0,
    }
    close_side = 'sell' if side == 'long' else 'buy'
    orders = [
        {
            'id': f'stop-{index}',
            'clientOrderId': f'client-{index}',
            'symbol': symbol,
            'type': 'STOP_MARKET',
            'side': close_side,
            'amount': 1.0,
            'stopPrice': price,
            'reduceOnly': True,
            'timestamp': timestamp,
        }
        for index, (price, timestamp) in enumerate(stops, start=1)
    ]
    cancelled = []
    engine.is_upbit_mode = lambda: False
    engine.exchange = SimpleNamespace(id='fixture')
    engine.ctrl = SimpleNamespace(format_symbol_for_display=lambda value: value)
    engine.last_protection_order_status = {}
    engine.protection_missing_candidates = {}
    engine.last_protection_alert_ts = {}
    engine._get_utbreakout_trailing_state = lambda _: None
    engine.get_runtime_strategy_params = lambda: {'active_strategy': 'utbot'}
    engine.trading_state_store = SimpleNamespace(active_for_symbol=lambda _: [])

    async def collect(_symbol):
        return True, list(orders)

    async def cancel(_symbol, reason='test', orders=None):
        cancelled.extend(list(orders or []))
        return len(list(orders or []))

    engine._collect_protection_orders_checked = collect
    engine._cancel_protection_orders = cancel

    status = asyncio.run(engine._audit_protection_orders(
        symbol,
        pos=pos,
        expected_tp=False,
        expected_sl=True,
        alert=False,
    ))

    assert status['duplicate_cancelled'] == 1
    assert [float(o['stopPrice']) for o in cancelled] == [
        pytest.approx(expected_cancel)
    ]
    kept = [o for o in orders if o not in cancelled]
    assert [float(o['stopPrice']) for o in kept] == [pytest.approx(expected_keep)]


def test_partial_close_oversized_stop_is_not_accepted_as_better_existing_stop():
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    symbol = 'BTC/USDT:USDT'
    pos = {
        'symbol': symbol,
        'side': 'long',
        'entryPrice': 100.0,
        'markPrice': 101.2,
        'leverage': 5,
        'contracts': 1.0,
    }
    oversized = {
        'id': 'oversized-profit-stop',
        'clientOrderId': 'oversized-client',
        'symbol': symbol,
        'type': 'STOP_MARKET',
        'side': 'sell',
        'amount': 2.0,
        'stopPrice': 101.0,
        'reduceOnly': True,
    }
    replacements = []
    engine._position_entry_strategy = lambda _: EMA200_UTBOT_RSI_STRATEGY
    engine._fetch_server_position_checked = lambda _symbol: asyncio.sleep(
        0, result=(True, dict(pos))
    )
    engine._collect_protection_orders_checked = lambda _symbol: asyncio.sleep(
        0, result=(True, [oversized])
    )
    engine.safe_price = lambda _symbol, price: float(price)
    engine.last_ema200_profit_stop_status = {}

    async def replace(_symbol, _pos, price, reason):
        replacements.append((float(price), float(_pos['contracts'])))
        return {
            'id': 'replacement',
            'clientOrderId': 'replacement-client',
            'symbol': symbol,
            'type': 'STOP_MARKET',
            'side': 'sell',
            'amount': 1.0,
            'stopPrice': float(price),
            'reduceOnly': True,
        }

    async def audit(*args, **kwargs):
        return {'status': 'OK', 'sl_present': True}

    engine._replace_stop_loss_order = replace
    engine._audit_protection_orders = audit

    asyncio.run(engine._ema200_apply_margin_profit_stop(symbol))

    assert replacements == [(pytest.approx(101.0), 1.0)]
    assert engine.last_ema200_profit_stop_status[symbol]['status'] == 'PROTECTED'


def test_partial_close_rebuilds_profit_stop_using_current_position_qty(tmp_path):
    symbol = 'BTC/USDT:USDT'
    store = SQLiteTradingStateStore(tmp_path / 'state.sqlite3')
    store.upsert(OrderRecord(
        client_order_id='entry-partial',
        symbol=symbol,
        side='long',
        strategy=EMA200_UTBOT_RSI_STRATEGY,
        signal_timestamp='1700000000',
        requested_qty=2.0,
        filled_qty=2.0,
        average_fill_price=100.0,
        order_state=OrderState.PROTECTED.value,
        stop_order_id='oversized-stop',
    ))
    pos = {
        'symbol': symbol,
        'side': 'long',
        'entryPrice': 100.0,
        'markPrice': 103.0,
        'contracts': 1.0,
    }
    orders = [{
        'id': 'oversized-stop',
        'clientOrderId': 'oversized-client',
        'symbol': symbol,
        'type': 'STOP_MARKET',
        'side': 'sell',
        'amount': 2.0,
        'stopPrice': 101.0,
        'reduceOnly': True,
    }]
    submissions = []
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    engine.exchange = SimpleNamespace(
        id='fixture',
        create_order=lambda _symbol, order_type, side, qty, price, params: (
            submissions.append({
                'id': 'replacement-qty1',
                'clientOrderId': params['newClientOrderId'],
                'symbol': _symbol,
                'type': str(order_type).upper(),
                'side': side,
                'amount': float(qty),
                'stopPrice': float(params['stopPrice']),
                'reduceOnly': bool(params.get('reduceOnly')),
            })
            or submissions[-1]
        ),
    )
    engine.trading_state_store = store
    engine.is_upbit_mode = lambda: False
    engine.ctrl = SimpleNamespace(
        format_symbol_for_display=lambda value: value,
        notify=lambda *args, **kwargs: asyncio.sleep(0),
    )
    engine.safe_amount = lambda _symbol, amount: float(amount)
    engine.safe_price = lambda _symbol, price: float(price)
    engine.PROTECTION_REPLACE_CONFIRM_DELAY = 0
    engine.last_protection_order_status = {}
    engine.last_protection_alert_ts = {}
    engine._persist_active_entry_protection_refs = lambda *args, **kwargs: None

    async def collect(_symbol):
        return True, list(orders)

    async def cancel(_symbol, reason='test', orders=None):
        for order in list(orders or []):
            if order in globals().get('_never_used', []):
                pass
            try:
                locals_orders.remove(order)
            except ValueError:
                pass
        return len(list(orders or []))

    locals_orders = orders
    engine._collect_protection_orders_checked = collect
    engine._cancel_protection_orders = cancel
    engine._fetch_server_position_checked = lambda _symbol: asyncio.sleep(
        0, result=(True, dict(pos))
    )

    replacement = asyncio.run(engine._replace_stop_loss_order(
        symbol,
        pos,
        101.0,
        reason='EMA200 margin ROI 6.00% locks 5%',
    ))

    assert replacement is not None
    assert replacement['amount'] == pytest.approx(1.0)
    assert len(submissions) == 1
    persisted = store.get('entry-partial')
    assert persisted.metadata['ema200_profit_stop_qty'] == pytest.approx(1.0)
    store.close()


def test_partial_close_restart_preserves_ema_attribution_but_not_oversized_stop(tmp_path):
    symbol = 'BTC/USDT:USDT'
    path = tmp_path / 'state.sqlite3'
    store = SQLiteTradingStateStore(path)
    store.upsert(OrderRecord(
        client_order_id='entry-partial-restart',
        symbol=symbol,
        side='long',
        strategy=EMA200_UTBOT_RSI_STRATEGY,
        signal_timestamp='1700000000',
        requested_qty=2.0,
        filled_qty=2.0,
        average_fill_price=100.0,
        order_state=OrderState.PROTECTED.value,
        stop_order_id='correct-qty-stop',
    ))
    store.close()
    reopened = SQLiteTradingStateStore(path)
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    pos = {
        'symbol': symbol,
        'side': 'long',
        'entryPrice': 100.0,
        'markPrice': 103.0,
        'contracts': 1.0,
    }
    correct = {
        'id': 'correct-qty-stop',
        'clientOrderId': 'correct-qty-client',
        'symbol': symbol,
        'type': 'STOP_MARKET',
        'side': 'sell',
        'amount': 1.0,
        'stopPrice': 101.0,
        'reduceOnly': True,
    }
    cancelled = []
    engine.is_upbit_mode = lambda: False
    engine.exchange = SimpleNamespace(id='fixture')
    engine.ctrl = SimpleNamespace(format_symbol_for_display=lambda value: value)
    engine.last_protection_order_status = {}
    engine.protection_missing_candidates = {}
    engine.last_protection_alert_ts = {}
    engine._get_utbreakout_trailing_state = lambda _: None
    engine.get_runtime_strategy_params = lambda: {'active_strategy': 'utbot'}
    engine.trading_state_store = reopened
    engine._collect_protection_orders_checked = lambda _symbol: asyncio.sleep(
        0, result=(True, [correct])
    )

    async def cancel(_symbol, reason='test', orders=None):
        cancelled.extend(list(orders or []))
        return len(list(orders or []))

    engine._cancel_protection_orders = cancel

    status = asyncio.run(engine._audit_protection_orders(
        symbol,
        pos=pos,
        expected_tp=False,
        expected_sl=True,
        alert=False,
    ))

    assert cancelled == []
    assert status['sl_present'] is True
    reopened.close()


def _cancel_confirmation_fixture(*, lookup_behavior):
    symbol = 'BTC/USDT:USDT'
    pos = {
        'symbol': symbol,
        'side': 'long',
        'entryPrice': 100.0,
        'markPrice': 103.0,
        'liquidationPrice': 50.0,
        'contracts': 1.0,
    }
    existing = {
        'id': '9001',
        'algoId': '9001',
        'clientOrderId': 'existing-client',
        'clientAlgoId': 'existing-client',
        'symbol': symbol,
        'type': 'STOP_MARKET',
        'side': 'sell',
        'amount': 1.0,
        'stopPrice': 100.5,
        'reduceOnly': True,
        '_protection_source': 'binance_algo',
    }
    submissions = []
    locks = []
    snapshot_calls = 0

    class Exchange:
        id = 'binance'

        def fetch_position_mode(self, _symbol=None):
            return {'hedged': False}

        def market(self, _symbol):
            return {'id': 'BTCUSDT'}

        def fapiPrivateGetAlgoOrder(self, params):
            if lookup_behavior == 'unknown':
                raise TimeoutError('lookup timeout')
            if lookup_behavior == 'open':
                return {
                    'algoId': '9001',
                    'clientAlgoId': 'existing-client',
                    'symbol': 'BTCUSDT',
                    'orderType': 'STOP_MARKET',
                    'side': 'SELL',
                    'quantity': '1',
                    'triggerPrice': '100.5',
                    'reduceOnly': 'true',
                    'algoStatus': 'NEW',
                }
            if lookup_behavior in {'terminal', 'filled'}:
                return {
                    'algoId': '9001',
                    'clientAlgoId': 'existing-client',
                    'symbol': 'BTCUSDT',
                    'orderType': 'STOP_MARKET',
                    'side': 'SELL',
                    'quantity': '1',
                    'triggerPrice': '100.5',
                    'reduceOnly': 'true',
                    'algoStatus': 'FILLED' if lookup_behavior == 'filled' else 'CANCELED',
                }
            raise AssertionError(lookup_behavior)

    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    engine.exchange = Exchange()
    engine.is_upbit_mode = lambda: False
    engine.ctrl = SimpleNamespace(
        format_symbol_for_display=lambda value: value,
        notify=lambda *args, **kwargs: asyncio.sleep(0),
    )
    engine.safe_amount = lambda _symbol, amount: float(amount)
    engine.safe_price = lambda _symbol, price: float(price)
    engine.PROTECTION_REPLACE_CONFIRM_ATTEMPTS = 1
    engine.PROTECTION_REPLACE_CONFIRM_DELAY = 0
    engine.last_protection_order_status = {}
    engine.last_ema200_profit_stop_status = {}
    engine.last_protection_alert_ts = {}
    engine._set_crypto_entry_lock = lambda reason: locks.append(reason)
    engine._fetch_position_with_liquidation = lambda _symbol, _pos: asyncio.sleep(
        0, result=(True, dict(pos))
    )
    engine._validate_position_stop_liquidation = lambda *args, **kwargs: SimpleNamespace(
        valid=True, reason='SAFE'
    )
    engine._fetch_server_position_checked = lambda _symbol: asyncio.sleep(
        0, result=(True, dict(pos))
    )
    engine._persist_active_entry_protection_refs = lambda *args, **kwargs: None

    async def collect(_symbol):
        nonlocal snapshot_calls
        snapshot_calls += 1
        if snapshot_calls == 1:
            return True, [existing]
        return False, []

    engine._collect_protection_orders_checked = collect
    engine._cancel_protection_orders = lambda *args, **kwargs: asyncio.sleep(
        0, result=1
    )

    async def create(*args, **kwargs):
        order = {
            'id': 'replacement-after-terminal',
            'clientOrderId': 'replacement-client',
            'symbol': symbol,
            'type': 'STOP_MARKET',
            'side': 'sell',
            'amount': 1.0,
            'stopPrice': 101.0,
            'reduceOnly': True,
        }
        submissions.append(order)
        return order

    engine._create_protection_order_with_retries = create
    return engine, symbol, pos, submissions, locks


@pytest.mark.parametrize('lookup_behavior', ['open', 'unknown'])
def test_cancel_confirmation_uncertainty_blocks_replacement(lookup_behavior):
    engine, symbol, pos, submissions, locks = _cancel_confirmation_fixture(
        lookup_behavior=lookup_behavior
    )

    result = asyncio.run(engine._replace_stop_loss_order(
        symbol,
        pos,
        101.0,
        reason='EMA200 margin ROI 6.00% locks 5%',
    ))

    assert result is None
    assert submissions == []
    assert any('PENDING_PROTECTION_RECONCILIATION' in value for value in locks)


def test_cancel_confirmation_terminal_cancel_allows_replacement():
    engine, symbol, pos, submissions, locks = _cancel_confirmation_fixture(
        lookup_behavior='terminal'
    )

    result = asyncio.run(engine._replace_stop_loss_order(
        symbol,
        pos,
        101.0,
        reason='EMA200 margin ROI 6.00% locks 5%',
    ))

    assert result is not None
    assert len(submissions) == 1
    assert locks == []


def test_pending_not_found_active_lifecycle_is_preserved(tmp_path):
    symbol = 'BTC/USDT:USDT'
    store = SQLiteTradingStateStore(tmp_path / 'state.sqlite3')
    store.upsert(OrderRecord(
        client_order_id='entry-active-not-found',
        symbol=symbol,
        side='long',
        strategy=EMA200_UTBOT_RSI_STRATEGY,
        signal_timestamp='1700000000',
        requested_qty=1.0,
        filled_qty=1.0,
        average_fill_price=100.0,
        order_state=OrderState.PROTECTED.value,
        metadata={
            'ema200_profit_stop_pending_client_order_id': 'active-not-found',
            'ema200_profit_stop_pending_side': 'long',
            'ema200_profit_stop_pending_qty': 1.0,
            'ema200_profit_stop_pending_trigger_price': 101.0,
        },
    ))

    class Exchange:
        id = 'binance'
        def market(self, _symbol):
            return {'id': 'BTCUSDT'}
        def fapiPrivateGetAlgoOrder(self, params):
            raise RuntimeError('-2013 Order does not exist.')

    locks = []
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    engine.exchange = Exchange()
    engine.trading_state_store = store
    engine._set_crypto_entry_lock = lambda reason: locks.append(reason)

    result = asyncio.run(engine._reconcile_ema200_profit_stop_pending_identity(
        symbol,
        pos=None,
        protection_orders=[],
    ))

    assert result['status'] == 'NOT_FOUND_PRESERVED'
    record = store.get('entry-active-not-found')
    assert record.metadata['ema200_profit_stop_pending_client_order_id'] == (
        'active-not-found'
    )
    assert any('PENDING_PROTECTION_RECONCILIATION' in reason for reason in locks)
    store.close()


def test_pending_not_found_closed_lifecycle_is_cleaned(tmp_path):
    symbol = 'BTC/USDT:USDT'
    store = SQLiteTradingStateStore(tmp_path / 'state.sqlite3')
    store.upsert(OrderRecord(
        client_order_id='entry-closed-not-found',
        symbol=symbol,
        side='long',
        strategy=EMA200_UTBOT_RSI_STRATEGY,
        signal_timestamp='1700000000',
        requested_qty=1.0,
        filled_qty=1.0,
        average_fill_price=100.0,
        order_state=OrderState.CLOSED.value,
        metadata={
            'ema200_profit_stop_pending_client_order_id': 'closed-not-found',
            'ema200_profit_stop_pending_side': 'long',
            'ema200_profit_stop_pending_qty': 1.0,
            'ema200_profit_stop_pending_trigger_price': 101.0,
        },
    ))

    class Exchange:
        id = 'binance'
        def market(self, _symbol):
            return {'id': 'BTCUSDT'}
        def fapiPrivateGetAlgoOrder(self, params):
            raise RuntimeError('-2013 Order does not exist.')

    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    engine.exchange = Exchange()
    engine.trading_state_store = store
    engine._set_crypto_entry_lock = lambda reason: None

    result = asyncio.run(engine._reconcile_ema200_profit_stop_pending_identity(
        symbol,
        pos=None,
        protection_orders=[],
    ))

    assert result['status'] == 'NOT_FOUND_CLEANED'
    record = store.get('entry-closed-not-found')
    assert 'ema200_profit_stop_pending_client_order_id' not in record.metadata
    store.close()



def test_audit_duplicate_long_stops_keeps_most_protective_not_newest():
    test_audit_duplicate_stops_keeps_most_protective_not_newest(
        'long',
        105.0,
        103.0,
        [(102.0, 1), (101.0, 2)],
        102.0,
        101.0,
    )


def test_audit_duplicate_short_stops_keeps_most_protective_not_newest():
    test_audit_duplicate_stops_keeps_most_protective_not_newest(
        'short',
        95.0,
        97.0,
        [(98.0, 1), (99.0, 2)],
        98.0,
        99.0,
    )


def test_cancel_confirmation_snapshot_failure_found_open_blocks_replacement():
    test_cancel_confirmation_uncertainty_blocks_replacement('open')


def test_cancel_confirmation_unknown_blocks_replacement():
    test_cancel_confirmation_uncertainty_blocks_replacement('unknown')


def test_audit_after_replacement_keeps_best_long_stop():
    test_audit_duplicate_stops_keeps_most_protective_not_newest(
        'long',
        105.0,
        103.0,
        [(102.0, 1), (101.0, 2)],
        102.0,
        101.0,
    )


def test_audit_after_replacement_keeps_best_short_stop():
    test_audit_duplicate_stops_keeps_most_protective_not_newest(
        'short',
        95.0,
        97.0,
        [(98.0, 1), (99.0, 2)],
        98.0,
        99.0,
    )


def test_terminal_stale_pending_does_not_block_new_live_position(tmp_path):
    symbol = 'BTC/USDT:USDT'
    store = SQLiteTradingStateStore(tmp_path / 'state.sqlite3')
    store.upsert(OrderRecord(
        client_order_id='old-closed-pending',
        symbol=symbol,
        side='long',
        strategy=EMA200_UTBOT_RSI_STRATEGY,
        signal_timestamp='1600000000',
        requested_qty=1.0,
        filled_qty=1.0,
        average_fill_price=90.0,
        order_state=OrderState.CLOSED.value,
        metadata={
            'ema200_profit_stop_pending_client_order_id': 'old-pending',
            'ema200_profit_stop_pending_side': 'long',
            'ema200_profit_stop_pending_qty': 1.0,
            'ema200_profit_stop_pending_trigger_price': 91.0,
        },
    ))

    class Exchange:
        id = 'binance'
        def market(self, _symbol):
            return {'id': 'BTCUSDT'}
        def fapiPrivateGetAlgoOrder(self, params):
            raise RuntimeError('-2013 Order does not exist.')

    locks = []
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    engine.exchange = Exchange()
    engine.trading_state_store = store
    engine._set_crypto_entry_lock = lambda reason: locks.append(reason)
    new_pos = {
        'symbol': symbol,
        'side': 'long',
        'entryPrice': 100.0,
        'markPrice': 103.0,
        'contracts': 1.0,
    }

    result = asyncio.run(engine._reconcile_ema200_profit_stop_pending_identity(
        symbol,
        pos=new_pos,
        protection_orders=[],
    ))

    assert result['status'] == 'NOT_FOUND_CLEANED'
    assert 'ema200_profit_stop_pending_client_order_id' not in (
        store.get('old-closed-pending').metadata
    )
    assert locks == []
    store.close()


def test_exit_flat_after_stop_cancel_never_submits_replacement():
    engine, symbol, pos, orders, submissions, _ = (
        _ema_replacement_race_fixture(initial_stop=100.5)
    )
    fetch_calls = 0

    async def fetch_position(_symbol):
        nonlocal fetch_calls
        fetch_calls += 1
        # The post-cancel reconciliation sees the UT/manual exit already flat.
        return True, None

    engine._fetch_server_position_checked = fetch_position

    result = asyncio.run(engine._replace_stop_loss_order(
        symbol,
        pos,
        101.0,
        reason='EMA200 margin ROI 6.00% locks 5%',
    ))

    assert result is None
    assert submissions == []
    assert orders == []
    assert fetch_calls == 1


def test_first_stage_expects_exchange_stop_after_profit_stop_was_installed():
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    engine.is_upbit_mode = lambda: False
    engine.get_runtime_strategy_params = lambda: {
        'active_strategy': EMA200_UTBOT_RSI_STRATEGY,
    }
    record = SimpleNamespace(
        strategy=EMA200_UTBOT_RSI_STRATEGY,
        metadata={'strategy_managed_no_stop': True},
        stop_order_id='profit-stop-on-exchange',
    )
    engine.trading_state_store = SimpleNamespace(active_for_symbol=lambda _: [record])
    assert engine._protection_expected_from_config('BTC/USDT:USDT', {
        'side': 'long', 'contracts': 1.0,
    }) == (False, True)


def test_profit_stop_raises_exchange_stop_without_lowering_existing_floor():
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    symbol = 'BTC/USDT:USDT'
    pos = {'side': 'long', 'entryPrice': 100.0, 'markPrice': 101.2,
           'leverage': 5, 'contracts': 1}
    installed = []
    stop = {'price': None}
    engine._position_entry_strategy = lambda _: EMA200_UTBOT_RSI_STRATEGY

    async def fetch_position(_):
        return True, pos

    async def fetch_orders(_):
        return True, ([{'type': 'STOP_MARKET', 'stopPrice': stop['price']}]
                      if stop['price'] is not None else [])

    async def replace(_symbol, _pos, price, reason):
        stop['price'] = price
        installed.append(price)
        return {'id': str(len(installed))}

    async def audit(*args, **kwargs):
        return {'status': 'OK'}

    engine._fetch_server_position_checked = fetch_position
    engine._collect_protection_orders_checked = fetch_orders
    engine._classify_protection_order = lambda _: 'sl'
    engine._protection_trigger_price = lambda order: order['stopPrice']
    engine.safe_price = lambda _, price: price
    engine._replace_stop_loss_order = replace
    engine._audit_protection_orders = audit
    asyncio.run(engine._ema200_apply_margin_profit_stop(symbol))
    assert installed == [pytest.approx(101.0)]
    pos['markPrice'] = 100.8
    asyncio.run(engine._ema200_apply_margin_profit_stop(symbol))
    assert len(installed) == 1
    pos['markPrice'] = 102.2
    asyncio.run(engine._ema200_apply_margin_profit_stop(symbol))
    assert installed[-1] == pytest.approx(102.0)
    pos['markPrice'] = 101.5
    asyncio.run(engine._ema200_apply_margin_profit_stop(symbol))
    assert len(installed) == 2


def test_profit_stop_never_changes_other_strategy_position():
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    engine._position_entry_strategy = lambda _: 'utbot'
    engine._fetch_server_position_checked = lambda _: (_ for _ in ()).throw(
        AssertionError('other strategy position must not be touched')
    )
    assert asyncio.run(engine._ema200_apply_margin_profit_stop('BTC/USDT:USDT')) is None


def test_auxiliary_ut_reader_uses_completed_bars_and_own_ut_settings():
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    seen = []
    def fetch(symbol, timeframe, limit):
        seen.append((timeframe, limit))
        return [[index, 1, 2, 0.5, 1.5, 10] for index in range(30)]
    engine.market_data_exchange = SimpleNamespace(fetch_ohlcv=fetch)
    engine._calculate_utbot_signal = lambda frame, params: (
        None, 'maintained', {'bias_side': 'long' if int(frame.iloc[-2]['timestamp']) == 28 else 'short'}
    )
    result = asyncio.run(engine._ema200_auxiliary_ut_biases('BTC/USDT:USDT', {
        'active_strategy': EMA200_UTBOT_RSI_STRATEGY,
    }))
    assert seen == [('15m', 250), ('30m', 250), ('1h', 250)]
    assert result == {'15m': 'long', '30m': 'long', '1h': 'long'}


@pytest.mark.parametrize('exit_timeframe', ['15m', '30m', '1h'])
def test_short_mechanical_exit_on_selected_completed_ut_buy(exit_timeframe):
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    timeframe_calls = []
    engine.market_data_exchange = SimpleNamespace(fetch_ohlcv=lambda symbol, tf, limit: (
        timeframe_calls.append(tf) or [[1, 1, 2, 0.5, 1.5, 10],
        [900001, 1, 2, 0.5, 1.5, 10], [1800001, 1, 2, 0.5, 1.5, 10]]
    ))
    engine.db = SimpleNamespace(get_latest_open_trade=lambda _: {
        'strategy': EMA200_UTBOT_RSI_STRATEGY,
        'entry_time': '1970-01-01T00:00:01+00:00',
    })
    engine.get_runtime_strategy_params = lambda: {
        'active_strategy': EMA200_UTBOT_RSI_STRATEGY,
        'EMA200UTBotRSI2H': {'exit_timeframe': exit_timeframe, 'enabled': False},
    }
    engine._calculate_utbot_signal = lambda df, params: ('long', 'buy', {'bias_side': 'long'})
    engine._update_stateful_diag = lambda *args, **kwargs: None
    engine.last_entry_reason = {}
    exits = []

    async def exit_position(symbol, reason):
        exits.append(reason)

    async def fetch_position(symbol):
        return True, None

    engine.exit_position = exit_position
    engine._fetch_server_position_checked = fetch_position
    assert asyncio.run(
        engine.process_exit_candle('BTC/USDT:USDT', exit_timeframe, 'short')
    )
    assert timeframe_calls == [exit_timeframe]
    assert exits == ['EMA200_UTBOT_RSI_UT_BUY']


def test_stale_ut_signal_before_entry_never_closes_new_position():
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    engine.market_data_exchange = SimpleNamespace(fetch_ohlcv=lambda *a, **k: [
        [1, 1, 2, 0.5, 1.5, 10], [900001, 1, 2, 0.5, 1.5, 10],
        [1800001, 1, 2, 0.5, 1.5, 10],
    ])
    engine.db = SimpleNamespace(get_latest_open_trade=lambda _: {
        'strategy': EMA200_UTBOT_RSI_STRATEGY,
        'entry_time': '1970-01-01T00:30:01+00:00',
    })
    engine.get_runtime_strategy_params = lambda: {'active_strategy': EMA200_UTBOT_RSI_STRATEGY}
    engine._calculate_utbot_signal = lambda df, params: ('short', 'sell', {'bias_side': 'short'})
    engine.last_entry_reason = {}
    engine.exit_position = lambda *args: (_ for _ in ()).throw(AssertionError('stale exit'))
    assert asyncio.run(engine.process_exit_candle('BTC/USDT:USDT', '15m', 'long'))
from bot_runtime.strategy_registry import CORE_STRATEGIES


def test_strategy_registered_in_core_strategies():
    assert EMA200_UTBOT_RSI_STRATEGY in CORE_STRATEGIES


def test_long_requires_ordered_ut_then_rsi_above_50_and_rising():
    signal, _, detail = evaluate_ema200_utbot_rsi_entry(
        close_price=110.0,
        ema200=100.0,
        ut_state="long",
        ut_last_signal_side="long",
        ut_last_signal_ts=1_000,
        prev_rsi=49.0,
        curr_rsi=51.0,
        rsi_signal_ts=2_000,
    )
    assert signal == "long"
    assert detail["ut_precedes_rsi"] is True
    assert detail["rsi_above_and_rising"] is True


def test_long_rejects_current_rsi_condition_when_ut_buy_did_not_happen_first():
    signal, reason, detail = evaluate_ema200_utbot_rsi_entry(
        close_price=110.0,
        ema200=100.0,
        ut_state="long",
        ut_last_signal_side="long",
        ut_last_signal_ts=3_000,
        prev_rsi=49.0,
        curr_rsi=51.0,
        rsi_signal_ts=2_000,
    )
    assert signal is None
    assert detail["ut_precedes_rsi"] is False
    assert "먼저 확정되지 않음" in reason


def test_same_completed_candle_ut_and_rsi_condition_is_rejected_as_unknown_order():
    signal, reason, detail = evaluate_ema200_utbot_rsi_entry(
        close_price=110.0,
        ema200=100.0,
        ut_state="long",
        ut_last_signal_side="long",
        ut_last_signal_ts=2_000,
        prev_rsi=49.0,
        curr_rsi=51.0,
        rsi_signal_ts=2_000,
    )
    assert signal is None
    assert detail["ut_precedes_rsi"] is False
    assert "먼저 확정되지 않음" in reason


def test_long_accepts_rsi_already_above_50_when_it_is_still_rising():
    signal, _, detail = evaluate_ema200_utbot_rsi_entry(
        close_price=110.0,
        ema200=100.0,
        ut_state="long",
        ut_last_signal_side="long",
        ut_last_signal_ts=1_000,
        prev_rsi=55.0,
        curr_rsi=57.0,
        rsi_signal_ts=2_000,
    )
    assert signal == "long"
    assert detail["rsi_cross_up"] is False
    assert detail["rsi_above_and_rising"] is True


@pytest.mark.parametrize(
    ("ut_state", "previous", "current"),
    [
        ("long", 55.0, 54.0),
        ("long", 55.0, 55.0),
        ("long", 49.0, 50.0),
        ("short", 45.0, 46.0),
        ("short", 45.0, 45.0),
        ("short", 51.0, 50.0),
    ],
)
def test_rsi_must_be_strictly_on_correct_side_and_move_in_entry_direction(
    ut_state,
    previous,
    current,
):
    signal, _, _ = evaluate_ema200_utbot_rsi_entry(
        close_price=110.0 if ut_state == "long" else 90.0,
        ema200=100.0,
        ut_state=ut_state,
        ut_last_signal_side=ut_state,
        ut_last_signal_ts=1_000,
        prev_rsi=previous,
        curr_rsi=current,
        rsi_signal_ts=2_000,
    )
    assert signal is None


def test_short_is_exact_inverse():
    signal, _, detail = evaluate_ema200_utbot_rsi_entry(
        close_price=90.0,
        ema200=100.0,
        ut_state="short",
        ut_last_signal_side="short",
        ut_last_signal_ts=1_000,
        prev_rsi=48.0,
        curr_rsi=47.0,
        rsi_signal_ts=2_000,
    )
    assert signal == "short"
    assert detail["ema_short_ok"] is True
    assert detail["rsi_cross_down"] is False
    assert detail["rsi_below_and_falling"] is True


def test_fixed_universe_is_exactly_ten_non_stable_binance_perpetual_assets():
    assert EMA200_BINANCE_TOP10_BASES == (
        "BTC",
        "ETH",
        "BNB",
        "XRP",
        "SOL",
        "TRX",
        "ZEC",
        "HYPE",
        "DOGE",
        "XMR",
    )
    assert len(EMA200_BINANCE_TOP10_SYMBOLS) == 10
    assert not {"USDT", "USDC", "DAI"} & set(EMA200_BINANCE_TOP10_BASES)
    assert all(symbol.endswith("/USDT:USDT") for symbol in EMA200_BINANCE_TOP10_SYMBOLS)


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("BTC/USDT:USDT", True),
        ("btcusdt", True),
        ("HYPE/USDT", True),
        ("ADA/USDT:USDT", False),
        ("BTC/USDC:USDC", False),
        ("BTC", False),
        ("USDT/USDT:USDT", False),
        (None, False),
    ],
)
def test_fixed_universe_guard_normalizes_common_symbol_forms(symbol, expected):
    assert is_ema200_utbot_rsi_symbol_allowed(symbol) is expected


def test_wrong_ema_side_blocks_signal():
    signal, _, _ = evaluate_ema200_utbot_rsi_entry(
        close_price=99.0,
        ema200=100.0,
        ut_state="long",
        ut_last_signal_side="long",
        ut_last_signal_ts=1_000,
        prev_rsi=49.0,
        curr_rsi=51.0,
        rsi_signal_ts=2_000,
    )
    assert signal is None


def test_above_1000_risk_plan_sizes_from_loss_budget_and_emergency_distance():
    plan = build_ema200_utbot_rsi_risk_plan(
        account_equity=2000.0,
        free_balance=2000.0,
        entry_price=100.0,
        config={
            "risk_per_trade_percent": 0.5,
            "emergency_exit_percent": 5.0,
            "leverage": 5,
        },
    )
    assert plan["small_account_mode"] is False
    assert plan["risk_budget_usdt"] == 10.0
    assert plan["planned_notional"] == 200.0
    assert plan["planned_margin"] == 40.0
    assert plan["planned_emergency_loss_usdt"] == 10.0
    assert plan["planned_qty"] == 2.0


def test_small_account_first_stage_uses_half_equity_at_5x_without_stop():
    plan = build_ema200_utbot_rsi_risk_plan(
        account_equity=200.0,
        free_balance=200.0,
        entry_price=100.0,
        config={"emergency_exit_percent": 5.0, "leverage": 9},
        consecutive_losses=0,
    )

    assert plan["small_account_mode"] is True
    assert plan["margin_percent"] == 50.0
    assert plan["planned_margin"] == 100.0
    assert plan["leverage"] == 5
    assert plan["planned_notional"] == 500.0
    assert plan["planned_qty"] == 5.0
    assert plan["strategy_exit_only"] is True
    assert plan["emergency_stop_required"] is False
    assert plan["planned_emergency_loss_usdt"] is None


@pytest.mark.parametrize(
    ("loss_streak", "expected_margin_percent"),
    [(1, 35.0), (2, 25.0), (3, 15.0), (4, 10.0), (12, 10.0)],
)
def test_small_account_loss_ladder_reduces_next_position_and_enables_stop(
    loss_streak,
    expected_margin_percent,
):
    plan = build_ema200_utbot_rsi_risk_plan(
        account_equity=200.0,
        free_balance=200.0,
        entry_price=100.0,
        config={"emergency_exit_percent": 5.0},
        consecutive_losses=loss_streak,
    )

    expected_margin = 200.0 * expected_margin_percent / 100.0
    assert plan["margin_percent"] == expected_margin_percent
    assert plan["planned_margin"] == expected_margin
    assert plan["planned_notional"] == expected_margin * 5
    assert plan["emergency_stop_required"] is True
    assert plan["strategy_exit_only"] is False
    assert plan["planned_emergency_loss_usdt"] == pytest.approx(
        expected_margin * 5 * 0.05
    )


def test_small_account_boundary_includes_exactly_1000_usdt():
    plan = build_ema200_utbot_rsi_risk_plan(
        account_equity=1000.0,
        free_balance=1000.0,
        entry_price=100.0,
        consecutive_losses=0,
    )
    assert plan["small_account_mode"] is True
    assert plan["planned_margin"] == 500.0
    assert plan["planned_notional"] == 2500.0


def test_consecutive_loss_helpers_reset_on_profit_or_break_even():
    assert count_ema200_consecutive_losses([-1.0, -2.0, 3.0, -4.0]) == 2
    assert count_ema200_consecutive_losses([0.0, -2.0]) == 0
    assert ema200_small_account_margin_percent(0) == 50.0
    assert ema200_small_account_margin_percent(99) == 10.0


def test_database_loss_streak_is_strategy_specific_and_restart_durable(tmp_path):
    db_path = tmp_path / "trades.sqlite3"
    db = DBManager(db_path)

    def close_trade(symbol, pnl, strategy):
        db.log_trade_entry(symbol, "long", 100.0, 1.0, strategy=strategy)
        assert db.get_latest_open_trade(symbol)["strategy"] == strategy
        assert db.log_trade_close(symbol, pnl, pnl, 100.0 + pnl, "test")

    close_trade("BTC/USDT", 4.0, EMA200_UTBOT_RSI_STRATEGY)
    close_trade("ETH/USDT", -8.0, "utbot")
    close_trade("SOL/USDT", -3.0, EMA200_UTBOT_RSI_STRATEGY)
    close_trade("XRP/USDT", -2.0, EMA200_UTBOT_RSI_STRATEGY)
    assert db.get_consecutive_strategy_losses(EMA200_UTBOT_RSI_STRATEGY) == 2
    db.conn.close()

    reopened = DBManager(db_path)
    assert reopened.get_consecutive_strategy_losses(EMA200_UTBOT_RSI_STRATEGY) == 2
    close_trade_db = reopened
    close_trade_db.log_trade_entry(
        "ADA/USDT", "long", 100.0, 1.0, strategy=EMA200_UTBOT_RSI_STRATEGY
    )
    assert close_trade_db.log_trade_close(
        "ADA/USDT", 1.0, 1.0, 101.0, "profit reset"
    )
    assert reopened.get_consecutive_strategy_losses(EMA200_UTBOT_RSI_STRATEGY) == 0
    reopened.conn.close()


def test_consecutive_loss_reset_preserves_history_and_counts_new_closes(tmp_path):
    db = DBManager(tmp_path / "trades.sqlite3")
    store = SQLiteTradingStateStore(tmp_path / "state.sqlite3")
    old_exit = datetime.now(timezone.utc) - timedelta(hours=1)

    def close_trade(symbol, pnl, exit_time):
        db.log_trade_entry(
            symbol,
            "long",
            100.0,
            1.0,
            strategy=EMA200_UTBOT_RSI_STRATEGY,
        )
        assert db.log_trade_close(
            symbol,
            pnl,
            pnl,
            100.0 + pnl,
            "test",
            exit_time=exit_time.isoformat(),
        )

    close_trade("BTC/USDT", -2.0, old_exit)
    close_trade("ETH/USDT", -3.0, old_exit + timedelta(seconds=1))
    payload = reset_ema200_consecutive_losses(db, store, reason="test")

    assert payload["raw_consecutive_losses_at_reset"] == 2
    assert store.get_runtime_state(
        EMA200_CONSECUTIVE_LOSS_RESET_STATE_KEY
    ) == payload
    assert get_ema200_consecutive_losses(db, payload) == (0, True)
    assert db.conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 2

    reset_at = datetime.fromisoformat(payload["reset_at"].replace("Z", "+00:00"))
    close_trade("SOL/USDT", -1.0, reset_at + timedelta(seconds=1))
    assert get_ema200_consecutive_losses(db, payload) == (1, True)
    close_trade("XRP/USDT", 1.0, reset_at + timedelta(seconds=2))
    assert get_ema200_consecutive_losses(db, payload) == (0, True)

    db.conn.close()
    store.close()


def test_risk_plan_caps_position_to_available_margin_without_increasing_risk():
    plan = build_ema200_utbot_rsi_risk_plan(
        account_equity=2000.0,
        free_balance=5.0,
        entry_price=100.0,
        config={
            "risk_per_trade_percent": 0.5,
            "emergency_exit_percent": 5.0,
            "leverage": 5,
        },
    )
    assert plan["margin_cap_applied"] is True
    assert plan["planned_notional"] < 200.0
    assert plan["planned_emergency_loss_usdt"] < plan["risk_budget_usdt"]


def test_daily_and_weekly_limits_block_new_entries_only_via_gate_result():
    gate = evaluate_ema200_utbot_rsi_loss_gate(
        account_equity=1000.0,
        daily_realized_pnl=-21.0,
        weekly_realized_pnl=-21.0,
        config={
            "daily_loss_limit_percent": 2.0,
            "weekly_loss_limit_percent": 5.0,
        },
    )
    assert gate["allowed"] is False
    assert gate["daily_blocked"] is True
    assert gate["weekly_blocked"] is False


def test_weekly_limit_is_never_normalized_below_daily_limit():
    cfg = normalize_ema200_utbot_rsi_config(
        {
            "daily_loss_limit_percent": 6.0,
            "weekly_loss_limit_percent": 4.0,
        }
    )
    assert cfg["weekly_loss_limit_percent"] >= cfg["daily_loss_limit_percent"]


def test_config_normalization_keeps_fixed_safety_ranges_and_rejects_nonfinite_values():
    cfg = normalize_ema200_utbot_rsi_config(
        {
            "min_risk_per_trade_percent": 0.001,
            "max_risk_per_trade_percent": 99.0,
            "enabled": "false",
            "risk_per_trade_percent": float("nan"),
            "rsi_length": float("inf"),
            "min_leverage": 0,
            "max_leverage": 99,
            "leverage": float("inf"),
            "max_emergency_exit_percent": 99.0,
            "emergency_exit_percent": float("inf"),
            "max_daily_loss_limit_percent": 99.0,
            "daily_loss_limit_percent": 99.0,
            "max_weekly_loss_limit_percent": 99.0,
            "weekly_loss_limit_percent": 99.0,
            "utbot_key_value": 9.0,
            "utbot_atr_period": 99,
            "utbot_use_heikin_ashi": True,
            "best_candidate_selection_enabled": "off",
        }
    )
    assert cfg["min_risk_per_trade_percent"] == 0.10
    assert cfg["enabled"] is False
    assert cfg["max_risk_per_trade_percent"] == 5.00
    assert cfg["risk_per_trade_percent"] == 0.50
    assert cfg["min_leverage"] == 1
    assert cfg["max_leverage"] == 10
    assert cfg["rsi_length"] == 14
    assert cfg["leverage"] == 5
    assert cfg["emergency_exit_percent"] == 5.0
    assert cfg["max_emergency_exit_percent"] == 30.0
    assert cfg["daily_loss_limit_percent"] == 20.0
    assert cfg["weekly_loss_limit_percent"] == 40.0
    assert cfg["utbot_key_value"] == 1.0
    assert cfg["utbot_atr_period"] == 10
    assert cfg["utbot_use_heikin_ashi"] is False
    assert cfg["best_candidate_selection_enabled"] is False


def test_best_candidate_selection_defaults_on_for_fresh_and_migrated_config():
    assert normalize_ema200_utbot_rsi_config()[
        "best_candidate_selection_enabled"
    ] is True
    assert normalize_ema200_utbot_rsi_config({
        "best_candidate_selection_enabled": "true",
    })["best_candidate_selection_enabled"] is True


def test_ema200_signal_uses_dedicated_ut_settings_not_shared_utbot_config():
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    rows = []
    for index in range(205):
        close = 100.0 + index
        rows.append([index, close - 1.0, close + 1.0, close - 2.0, close, 10.0])
    frame = pd.DataFrame(
        rows,
        columns=["timestamp", "open", "high", "low", "close", "volume"],
    )
    observed = {}

    def calculate_utbot(df, params):
        observed.update(params["UTBot"])
        return None, "state", {
            "bias_side": "long",
            "signal_side": "long",
            "signal_ts": 100,
        }

    engine._calculate_utbot_signal = calculate_utbot
    engine._calculate_ema200_utbot_rsi_signal(
        frame,
        {
            "UTBot": {
                "key_value": 5.0,
                "atr_period": 50,
                "use_heikin_ashi": True,
            },
            "EMA200UTBotRSI2H": {
                "utbot_key_value": 8.0,
                "utbot_atr_period": 80,
                "utbot_use_heikin_ashi": True,
            },
        },
    )

    assert observed == {
        "key_value": 1.0,
        "atr_period": 10,
        "use_heikin_ashi": False,
    }


def test_daily_loss_reset_preserves_history_and_resets_only_same_day_baseline(tmp_path):
    db = DBManager(str(tmp_path / "trades.db"))
    store = SQLiteTradingStateStore(tmp_path / "state.sqlite3")
    db.get_daily_stats = lambda: (2, -7.5)
    store.set_runtime_state(
        DAILY_LOSS_ENTRY_LOCK_KEY,
        {"date": "2099-01-01", "reason": "DAILY_LOSS_LIMIT"},
    )

    payload = reset_ema200_daily_loss(db, store, reason="test")
    effective, baseline, active = apply_ema200_daily_loss_reset(
        -7.5,
        store.get_runtime_state(EMA200_DAILY_LOSS_RESET_STATE_KEY),
        current_date=payload["date"],
    )

    assert effective == pytest.approx(0.0)
    assert baseline == pytest.approx(-7.5)
    assert active is True
    assert payload["trade_count_at_reset"] == 2
    assert payload["already_reset"] is False
    assert store.get_runtime_state(DAILY_LOSS_ENTRY_LOCK_KEY) is None
    db.get_daily_stats = lambda: (3, -9.0)
    repeated = reset_ema200_daily_loss(db, store, reason="repeat")
    assert repeated["already_reset"] is True
    assert repeated["baseline_realized_pnl"] == pytest.approx(-7.5)
    assert apply_ema200_daily_loss_reset(
        -7.5,
        payload,
        current_date="2099-01-02",
    ) == (-7.5, 0.0, False)
    db.conn.close()
    store.close()


def test_entry_gate_uses_reset_daily_baseline_but_keeps_weekly_pnl(tmp_path):
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    engine.db = SimpleNamespace(
        get_daily_stats=lambda: (2, -30.0),
        get_weekly_stats=lambda: (4, -30.0),
    )
    engine.trading_state_store = SQLiteTradingStateStore(
        tmp_path / "state.sqlite3"
    )
    engine.trading_state_store.set_runtime_state(
        EMA200_DAILY_LOSS_RESET_STATE_KEY,
        {
            "date": ema200_kst_date(),
            "baseline_realized_pnl": -25.0,
        },
    )

    async def balance_info():
        return 1000.0, 1000.0, {}

    engine.get_balance_info = balance_info
    gate = asyncio.run(
        engine._ema200_utbot_rsi_new_entry_gate(
            "BTC/USDT:USDT",
            {
                "EMA200UTBotRSI2H": {
                    "daily_loss_limit_percent": 2.0,
                    "weekly_loss_limit_percent": 5.0,
                }
            },
        )
    )

    assert gate["allowed"] is True
    assert gate["daily_realized_pnl_raw"] == pytest.approx(-30.0)
    assert gate["daily_realized_pnl"] == pytest.approx(-5.0)
    assert gate["weekly_realized_pnl"] == pytest.approx(-30.0)
    assert gate["daily_reset_active"] is True
    engine.trading_state_store.close()


def test_emergency_stop_price_is_long_short_symmetric():
    config = {"emergency_exit_percent": 5.0}
    assert calculate_ema200_utbot_rsi_emergency_stop_price(
        side="long", entry_price=100.0, config=config
    ) == pytest.approx(95.0)
    assert calculate_ema200_utbot_rsi_emergency_stop_price(
        side="short", entry_price=100.0, config=config
    ) == pytest.approx(105.0)


def test_signal_uses_last_completed_candle_and_ignores_current_candle():
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    rows = []
    for index in range(205):
        close = 100.0 + index
        rows.append([index, close - 1.0, close + 1.0, close - 2.0, close, 10.0])
    rows[-1][4] = 10_000.0
    frame = pd.DataFrame(
        rows,
        columns=["timestamp", "open", "high", "low", "close", "volume"],
    )
    engine._calculate_utbot_signal = lambda df, params: (
        None,
        "no fresh signal",
        {"bias_side": "long", "signal_side": "long", "signal_ts": 100},
    )

    _, _, detail = engine._calculate_ema200_utbot_rsi_signal(
        frame,
        {"EMA200UTBotRSI2H": {"enabled": True}},
    )

    assert detail["closed_candle_ts"] == rows[-2][0]
    assert detail["closed_candle_close"] == rows[-2][4]
    assert detail["closed_candle_close"] != rows[-1][4]


def test_rsi_uses_wilder_sma_seed_then_recursive_smoothing():
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    rsi = engine._calculate_wilder_rsi_for_ema200_strategy(
        [10.0, 11.0, 10.0, 12.0, 11.0],
        3,
    )

    assert rsi.iloc[:3].isna().all()
    assert rsi.iloc[3] == pytest.approx(75.0)
    assert rsi.iloc[4] == pytest.approx(54.5454545455)


def test_primary_polling_is_fixed_to_2h_and_exit_is_user_selectable():
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    params = {
        "active_strategy": EMA200_UTBOT_RSI_STRATEGY,
        "EMA200UTBotRSI2H": {"timeframe": "4h"},
    }
    engine.get_runtime_trade_config = lambda: {
        "common_settings": {"entry_timeframe": "15m", "exit_timeframe": "4h"},
        "strategy_params": params,
    }
    engine.get_runtime_common_settings = lambda: {
        "entry_timeframe": "15m",
        "exit_timeframe": "4h",
    }
    engine.get_runtime_strategy_params = lambda: params

    assert engine._get_primary_poll_timeframe() == "2h"
    assert engine._get_exit_timeframe("BTC/USDT") == "15m"
    params["EMA200UTBotRSI2H"]["exit_timeframe"] = "30m"
    assert engine._get_exit_timeframe("BTC/USDT") == "30m"

    fixed_scanner_source = inspect.getsource(
        emas.SignalEngine._scan_and_trade_ema200_binance_top10
    )
    high_volume_source = inspect.getsource(emas.SignalEngine.scan_and_trade_high_volume)
    poll_tick_source = inspect.getsource(emas.SignalEngine.poll_tick)
    assert "'2h'" in fixed_scanner_source
    assert "EMA200_BINANCE_TOP10_SYMBOLS" in fixed_scanner_source
    assert "_scan_and_trade_ema200_binance_top10" in high_volume_source
    assert "configured_active_strategy == EMA200_UTBOT_RSI_STRATEGY" in poll_tick_source
    assert "scanner_enabled = True" in poll_tick_source


def test_open_ema200_position_keeps_selected_exit_after_strategy_changes():
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    engine.db = SimpleNamespace(
        get_latest_open_trade=lambda symbol: {
            "strategy": EMA200_UTBOT_RSI_STRATEGY,
        }
    )
    engine.get_runtime_strategy_params = lambda: {
        "active_strategy": "utbot",
        "EMA200UTBotRSI2H": {},
    }
    engine.get_runtime_common_settings = lambda: {
        "exit_timeframe": "4h",
    }

    assert engine._position_entry_strategy("DOGE/USDT:USDT") == (
        EMA200_UTBOT_RSI_STRATEGY
    )
    assert engine._get_exit_timeframe("DOGE/USDT:USDT") == "15m"


def test_known_non_ema_position_is_not_reassigned_to_new_ema_config():
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    engine.db = SimpleNamespace(
        get_latest_open_trade=lambda symbol: {"strategy": "utbot"}
    )
    engine.scanner_active_symbol = None
    engine.get_runtime_strategy_params = lambda: {
        "active_strategy": EMA200_UTBOT_RSI_STRATEGY,
    }
    engine.get_runtime_common_settings = lambda: {
        "exit_timeframe": "4h",
    }

    assert engine._get_exit_timeframe("DOGE/USDT:USDT") == "4h"


def test_fixed_top10_scanner_checks_every_symbol_on_2h_without_volume_selection():
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    calls = []

    def fetch_ohlcv(symbol, timeframe, limit):
        calls.append((symbol, timeframe, limit))
        return [
            [1, 1.0, 2.0, 0.5, 1.5, 10.0],
            [2, 1.5, 2.0, 0.5, 1.4, 10.0],
            [3, 1.4, 2.0, 0.5, 1.3, 10.0],
        ]

    engine.market_data_exchange = SimpleNamespace(fetch_ohlcv=fetch_ohlcv)
    engine.ctrl = SimpleNamespace(is_paused=False)
    engine.scanner_active_symbol = None
    engine.ema200_top10_scan_cursor = 0
    engine.last_entry_reason = {}
    engine.get_runtime_strategy_params = lambda: {
        "active_strategy": EMA200_UTBOT_RSI_STRATEGY,
    }
    engine._collect_primary_strategy_context = lambda *args, **kwargs: {
        "precomputed": {},
    }

    async def no_signal(*args, **kwargs):
        return None, None, None, None, None, None

    engine._calculate_strategy_signal = no_signal

    asyncio.run(engine._scan_and_trade_ema200_binance_top10())

    assert calls == [
        (symbol, "2h", 300) for symbol in EMA200_BINANCE_TOP10_SYMBOLS
    ]

    calls.clear()
    asyncio.run(engine._scan_and_trade_ema200_binance_top10())
    rotated = EMA200_BINANCE_TOP10_SYMBOLS[1:] + EMA200_BINANCE_TOP10_SYMBOLS[:1]
    assert calls == [(symbol, "2h", 300) for symbol in rotated]


def _ema200_ranking_ohlcv(*, close=100.0, volume=100.0, candle_ts=None):
    step = 2 * 60 * 60 * 1000
    final_closed_ts = candle_ts or (220 * step)
    first_ts = final_closed_ts - (219 * step)
    rows = []
    for index in range(220):
        price = close - 2.0 + (index / 220.0) * 2.0
        rows.append([
            first_ts + index * step,
            price - 0.2,
            price + 0.5,
            price - 0.5,
            price,
            volume,
        ])
    rows.append([
        final_closed_ts + step,
        close,
        close + 0.3,
        close - 0.3,
        close,
        volume,
    ])
    return rows


def test_candidate_ranker_prefers_fresher_stronger_signal_over_scan_order():
    step = 2 * 60 * 60 * 1000
    candle_ts = 220 * step
    btc = build_ema200_candidate(
        symbol="BTC/USDT:USDT",
        side="long",
        detail={
            "closed_candle_ts": candle_ts,
            "closed_candle_close": 100.0,
            "ema200": 99.0,
            "ema200_previous": 99.1,
            "prev_rsi": 55.0,
            "curr_rsi": 55.1,
            "ut_last_signal_ts": candle_ts - 10 * step,
        },
        ohlcv=_ema200_ranking_ohlcv(
            close=100.0,
            volume=10_000.0,
            candle_ts=candle_ts,
        ),
    )
    eth = build_ema200_candidate(
        symbol="ETH/USDT:USDT",
        side="long",
        detail={
            "closed_candle_ts": candle_ts,
            "closed_candle_close": 100.0,
            "ema200": 99.0,
            "ema200_previous": 98.8,
            "prev_rsi": 52.0,
            "curr_rsi": 55.0,
            "ut_last_signal_ts": candle_ts - step,
        },
        ohlcv=_ema200_ranking_ohlcv(
            close=100.0,
            volume=100.0,
            candle_ts=candle_ts,
        ),
    )

    ranked = rank_ema200_candidates([btc, eth])

    assert [item["symbol"] for item in ranked] == [
        "ETH/USDT:USDT",
        "BTC/USDT:USDT",
    ]
    assert ranked[0]["score"] > ranked[1]["score"]
    assert ranked[0]["score_breakdown"]["ut_recency"] == 40.0
    assert ranked[1]["score_breakdown"]["liquidity"] == 15.0


def test_best_candidate_scanner_evaluates_all_ten_then_enters_highest_rank():
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    step = 2 * 60 * 60 * 1000
    candle_ts = 220 * step
    events = []
    opened = set()
    details = {
        symbol: {
            "closed_candle_ts": candle_ts,
            "closed_candle_close": 100.0,
            "ema200": 99.0,
            "ema200_previous": 99.0,
            "prev_rsi": 49.0,
            "curr_rsi": 49.0,
            "ut_last_signal_ts": candle_ts - step,
        }
        for symbol in EMA200_BINANCE_TOP10_SYMBOLS
    }
    details["BTC/USDT:USDT"].update({
        "prev_rsi": 55.0,
        "curr_rsi": 55.1,
        "ema200_previous": 99.1,
        "ut_last_signal_ts": candle_ts - 10 * step,
    })
    details["ETH/USDT:USDT"].update({
        "prev_rsi": 52.0,
        "curr_rsi": 55.0,
        "ema200_previous": 98.8,
        "ut_last_signal_ts": candle_ts - step,
    })

    def fetch_ohlcv(symbol, timeframe, limit):
        events.append(("fetch", symbol))
        volume = 10_000.0 if symbol == "BTC/USDT:USDT" else 100.0
        return _ema200_ranking_ohlcv(
            close=100.0,
            volume=volume,
            candle_ts=candle_ts,
        )

    engine.market_data_exchange = SimpleNamespace(fetch_ohlcv=fetch_ohlcv)
    engine.ctrl = SimpleNamespace(is_paused=False)
    engine.scanner_active_symbol = None
    engine.ema200_top10_scan_cursor = 0
    engine.last_entry_reason = {}
    engine.last_processed_candle_ts = {}
    engine.last_candle_time = {}
    engine.last_candle_success = {}
    engine.get_runtime_strategy_params = lambda: {
        "active_strategy": EMA200_UTBOT_RSI_STRATEGY,
        "EMA200UTBotRSI2H": {"best_candidate_selection_enabled": True},
    }

    def collect_context(symbol, *args, **kwargs):
        return {
            "precomputed": {"symbol": symbol},
            "raw_hybrid_detail": dict(details[symbol]),
        }

    async def calculate_signal(*args, precomputed=None, **kwargs):
        symbol = precomputed["symbol"]
        signal = "long" if symbol in {"BTC/USDT:USDT", "ETH/USDT:USDT"} else None
        return signal, None, None, None, None, None

    async def get_position(symbol, use_cache=False):
        if symbol in opened:
            return {"symbol": symbol, "side": "long", "contracts": 1.0}
        return None

    async def entry(symbol, side, price):
        events.append(("entry", symbol))
        opened.add(symbol)

    engine._collect_primary_strategy_context = collect_context
    engine._calculate_strategy_signal = calculate_signal
    engine.get_server_position = get_position
    engine.entry = entry

    asyncio.run(engine._scan_and_trade_ema200_binance_top10())

    assert [event for event in events if event[0] == "fetch"] == [
        ("fetch", symbol) for symbol in EMA200_BINANCE_TOP10_SYMBOLS
        for _ in range(1 + (3 if symbol in {"BTC/USDT:USDT", "ETH/USDT:USDT"} else 0))
    ]
    assert [event for event in events if event[0] == "entry"] == [
        ("entry", "ETH/USDT:USDT")
    ]
    assert engine.scanner_active_symbol == "ETH/USDT:USDT"
    assert engine.last_ema200_candidate_selection["selected"]["symbol"] == (
        "ETH/USDT:USDT"
    )
    assert engine.last_processed_candle_ts["ETH/USDT:USDT"] == candle_ts


def test_best_candidate_off_preserves_first_valid_signal_behavior():
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    step = 2 * 60 * 60 * 1000
    candle_ts = 220 * step
    fetched = []
    opened = set()

    def fetch_ohlcv(symbol, timeframe, limit):
        fetched.append(symbol)
        return _ema200_ranking_ohlcv(candle_ts=candle_ts)

    engine.market_data_exchange = SimpleNamespace(fetch_ohlcv=fetch_ohlcv)
    engine.ctrl = SimpleNamespace(is_paused=False)
    engine.scanner_active_symbol = None
    engine.ema200_top10_scan_cursor = 0
    engine.last_entry_reason = {}
    engine.last_processed_candle_ts = {}
    engine.last_candle_time = {}
    engine.last_candle_success = {}
    engine.get_runtime_strategy_params = lambda: {
        "active_strategy": EMA200_UTBOT_RSI_STRATEGY,
        "EMA200UTBotRSI2H": {"best_candidate_selection_enabled": False},
    }
    engine._collect_primary_strategy_context = lambda symbol, *args, **kwargs: {
        "precomputed": {"symbol": symbol},
        "raw_hybrid_detail": {
            "closed_candle_ts": candle_ts,
            "closed_candle_close": 100.0,
            "ema200": 99.0,
            "ema200_previous": 98.9,
            "prev_rsi": 52.0,
            "curr_rsi": 53.0,
            "ut_last_signal_ts": candle_ts - step,
        },
    }

    async def signal(*args, **kwargs):
        return "long", None, None, None, None, None

    async def get_position(symbol, use_cache=False):
        return (
            {"symbol": symbol, "side": "long", "contracts": 1.0}
            if symbol in opened
            else None
        )

    async def entry(symbol, side, price):
        opened.add(symbol)

    engine._calculate_strategy_signal = signal
    engine.get_server_position = get_position
    engine.entry = entry

    asyncio.run(engine._scan_and_trade_ema200_binance_top10())

    assert fetched == [EMA200_BINANCE_TOP10_SYMBOLS[0]]
    assert engine.scanner_active_symbol == EMA200_BINANCE_TOP10_SYMBOLS[0]
    assert engine.last_ema200_candidate_selection["enabled"] is False


def test_best_candidate_scan_fails_closed_when_one_of_ten_cannot_be_evaluated():
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    step = 2 * 60 * 60 * 1000
    candle_ts = 220 * step
    entries = []

    def fetch_ohlcv(symbol, timeframe, limit):
        if symbol == EMA200_BINANCE_TOP10_SYMBOLS[-1]:
            raise TimeoutError("simulated market-data timeout")
        return _ema200_ranking_ohlcv(candle_ts=candle_ts)

    engine.market_data_exchange = SimpleNamespace(fetch_ohlcv=fetch_ohlcv)
    engine.ctrl = SimpleNamespace(is_paused=False)
    engine.scanner_active_symbol = None
    engine.ema200_top10_scan_cursor = 0
    engine.last_entry_reason = {}
    engine.last_processed_candle_ts = {}
    engine.last_candle_time = {}
    engine.last_candle_success = {}
    engine.get_runtime_strategy_params = lambda: {
        "active_strategy": EMA200_UTBOT_RSI_STRATEGY,
        "EMA200UTBotRSI2H": {"best_candidate_selection_enabled": True},
    }
    engine._collect_primary_strategy_context = lambda symbol, *args, **kwargs: {
        "precomputed": {"symbol": symbol},
        "raw_hybrid_detail": {
            "closed_candle_ts": candle_ts,
            "closed_candle_close": 100.0,
            "ema200": 99.0,
            "ema200_previous": 98.9,
            "prev_rsi": 52.0,
            "curr_rsi": 53.0,
            "ut_last_signal_ts": candle_ts - step,
        },
    }

    async def signal(*args, precomputed=None, **kwargs):
        return (
            "long" if precomputed["symbol"] == EMA200_BINANCE_TOP10_SYMBOLS[0] else None,
            None,
            None,
            None,
            None,
            None,
        )

    async def entry(symbol, side, price):
        entries.append((symbol, side, price))

    async def no_position(symbol, use_cache=False):
        return None

    engine._calculate_strategy_signal = signal
    engine.entry = entry
    engine.get_server_position = no_position

    asyncio.run(engine._scan_and_trade_ema200_binance_top10())

    assert entries == []
    assert "10개 중 9개 평가" in engine.last_ema200_candidate_selection["reason"]
    assert "TimeoutError" in engine.last_ema200_candidate_selection["reason"]


def test_high_volume_scanner_bypasses_coin_selector_for_ema200_strategy():
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    calls = []
    engine.is_upbit_mode = lambda: False
    engine.get_runtime_strategy_params = lambda: {
        "active_strategy": EMA200_UTBOT_RSI_STRATEGY,
    }

    async def fixed_scan():
        calls.append("fixed")

    engine._scan_and_trade_ema200_binance_top10 = fixed_scan
    engine._get_coin_selector_config = lambda: (_ for _ in ()).throw(
        AssertionError("CoinSelector must not run for the fixed EMA200 universe")
    )

    asyncio.run(engine.scan_and_trade_high_volume())

    assert calls == ["fixed"]


def test_ema200_entry_guard_blocks_every_symbol_outside_fixed_top10():
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    notices = []

    async def notify(message):
        notices.append(message)

    engine.ctrl = SimpleNamespace(notify=notify)
    engine.last_entry_reason = {}
    engine.is_user_custom_entry_mode_enabled = lambda: False
    engine.get_runtime_strategy_params = lambda: {
        "active_strategy": EMA200_UTBOT_RSI_STRATEGY,
    }

    asyncio.run(engine.entry("ADA/USDT:USDT", "long", 1.0))

    assert "EMA200_FIXED_TOP10_ONLY" in engine.last_entry_reason["ADA/USDT:USDT"]
    assert notices and "진입 차단" in notices[0]


def test_latest_strategy_evaluation_is_saved_for_telegram_status():
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    engine.last_ema200_utbot_rsi_status = {}
    expected_detail = {
        "closed_candle_ts": 123,
        "closed_candle_close": 101.0,
        "ema200": 100.0,
        "ut_state": "long",
        "prev_rsi": 49.0,
        "curr_rsi": 51.0,
    }
    engine._calculate_ema200_utbot_rsi_signal = lambda df, params: (
        "long",
        "ready",
        dict(expected_detail),
    )

    context = engine._collect_primary_strategy_context(
        "BTC/USDT",
        pd.DataFrame(),
        {"active_strategy": EMA200_UTBOT_RSI_STRATEGY},
        EMA200_UTBOT_RSI_STRATEGY,
    )

    assert context["precomputed"][EMA200_UTBOT_RSI_STRATEGY][0] == "long"
    saved = engine.last_ema200_utbot_rsi_status["BTC/USDT"]
    assert all(saved[key] == value for key, value in expected_detail.items())
    assert int(saved["evaluated_at_ns"]) > 0


@pytest.mark.parametrize(
    ("current_side", "ut_signal", "exit_label", "enabled"),
    [
        ("long", "short", "EMA200_UTBOT_RSI_UT_SELL", False),
        ("short", "long", "EMA200_UTBOT_RSI_UT_BUY", True),
    ],
)
def test_fresh_opposite_ut_signal_mechanically_exits_before_optional_filters(
    current_side,
    ut_signal,
    exit_label,
    enabled,
):
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    engine.market_data_exchange = SimpleNamespace(
        fetch_ohlcv=lambda *args, **kwargs: [
            [1, 1, 2, 0.5, 1.5, 10],
            [2, 1.5, 2, 0.5, 1.4, 10],
            [3, 1.4, 2, 0.5, 1.3, 10],
        ]
    )
    engine.get_runtime_strategy_params = lambda: {
        "active_strategy": EMA200_UTBOT_RSI_STRATEGY,
        "EMA200UTBotRSI2H": {"enabled": enabled},
        "UTBot": {
            "key_value": 5.0,
            "atr_period": 50,
            "use_heikin_ashi": True,
        },
    }
    engine.get_runtime_common_settings = lambda: (_ for _ in ()).throw(
        AssertionError("optional exit filters must not run")
    )
    observed_ut = {}

    def calculate_utbot(df, params):
        observed_ut.update(params["UTBot"])
        return ut_signal, "fresh opposite", {"bias_side": ut_signal}

    engine._calculate_utbot_signal = calculate_utbot
    engine._update_stateful_diag = lambda *args, **kwargs: None
    engine.last_entry_reason = {}
    exit_calls = []

    async def exit_position(symbol, reason):
        exit_calls.append((symbol, reason))

    async def fetch_position(symbol):
        return True, None

    engine.exit_position = exit_position
    engine._fetch_server_position_checked = fetch_position

    processed = asyncio.run(
        engine.process_exit_candle("BTC/USDT", "2h", current_side)
    )

    assert processed is True
    assert exit_calls == [("BTC/USDT", exit_label)]
    assert observed_ut == {
        "key_value": 1.0,
        "atr_period": 10,
        "use_heikin_ashi": False,
    }


def test_mechanical_exit_retries_same_candle_when_position_remains_open():
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    engine.market_data_exchange = SimpleNamespace(
        fetch_ohlcv=lambda *args, **kwargs: [
            [1, 1, 2, 0.5, 1.5, 10],
            [2, 1.5, 2, 0.5, 1.4, 10],
            [3, 1.4, 2, 0.5, 1.3, 10],
        ]
    )
    engine.get_runtime_strategy_params = lambda: {
        "active_strategy": EMA200_UTBOT_RSI_STRATEGY,
    }
    engine._calculate_utbot_signal = lambda df, params: (
        "short",
        "fresh sell",
        {"bias_side": "short"},
    )
    engine._update_stateful_diag = lambda *args, **kwargs: None
    engine.last_entry_reason = {}

    async def exit_position(symbol, reason):
        return None

    async def fetch_position(symbol):
        return True, {"side": "long", "contracts": 1.0}

    engine.exit_position = exit_position
    engine._fetch_server_position_checked = fetch_position

    assert asyncio.run(engine.process_exit_candle("BTC/USDT", "2h", "long")) is False
    assert "재시도" in engine.last_entry_reason["BTC/USDT"]


def test_ema200_position_owner_keeps_mechanical_exit_after_config_switch():
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    engine.market_data_exchange = SimpleNamespace(
        fetch_ohlcv=lambda *args, **kwargs: [
            [1, 1, 2, 0.5, 1.5, 10],
            [2, 1.5, 2, 0.5, 1.4, 10],
            [3, 1.4, 2, 0.5, 1.3, 10],
        ]
    )
    engine.db = SimpleNamespace(
        get_latest_open_trade=lambda symbol: {
            "strategy": EMA200_UTBOT_RSI_STRATEGY,
        }
    )
    engine.get_runtime_strategy_params = lambda: {
        "active_strategy": "utbot",
    }
    engine.get_runtime_common_settings = lambda: (_ for _ in ()).throw(
        AssertionError("new strategy filters must not manage the EMA200 position")
    )
    engine._calculate_utbot_signal = lambda df, params: (
        "short",
        "fresh sell",
        {"bias_side": "short"},
    )
    engine._update_stateful_diag = lambda *args, **kwargs: None
    engine.last_entry_reason = {}
    exits = []

    async def exit_position(symbol, reason):
        exits.append((symbol, reason))

    async def fetch_position(symbol):
        return True, None

    engine.exit_position = exit_position
    engine._fetch_server_position_checked = fetch_position

    assert asyncio.run(
        engine.process_exit_candle("DOGE/USDT:USDT", "2h", "long")
    ) is True
    assert exits == [
        ("DOGE/USDT:USDT", "EMA200_UTBOT_RSI_UT_SELL")
    ]


def test_later_loss_stage_requires_stop_and_never_fixed_tp_for_strategy():
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    engine.is_upbit_mode = lambda: False
    engine.get_runtime_strategy_params = lambda: {
        "active_strategy": EMA200_UTBOT_RSI_STRATEGY,
    }
    engine.get_runtime_common_settings = lambda: {
        "tp_sl_enabled": False,
        "take_profit_enabled": True,
        "stop_loss_enabled": False,
    }

    expected = engine._protection_expected_from_config(
        "BTC/USDT",
        {"side": "long", "contracts": 1.0},
    )

    assert expected == (False, True)


def test_first_small_account_stage_is_durably_recognized_as_strategy_managed():
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    engine.is_upbit_mode = lambda: False
    engine.get_runtime_strategy_params = lambda: {
        "active_strategy": "utbot",
    }
    record = SimpleNamespace(
        strategy=EMA200_UTBOT_RSI_STRATEGY,
        order_intent="ENTRY",
        order_purpose="entry",
        metadata={"strategy_managed_no_stop": True},
    )
    engine.trading_state_store = SimpleNamespace(
        active_for_symbol=lambda symbol: [record]
    )

    assert engine._protection_expected_from_config(
        "BTC/USDT",
        {"side": "long", "contracts": 1.0},
    ) == (False, False)


def test_entry_branch_conditionally_places_emergency_stop_after_first_loss():
    source = inspect.getsource(emas.SignalEngine.entry)
    protection_branch = source.rsplit(
        "elif active_strategy == EMA200_UTBOT_RSI_STRATEGY:", 1
    )[1].split("elif active_strategy in UTBREAKOUT_STRATEGIES:", 1)[0]
    assert "emergency_stop_required" in protection_branch
    assert "tp_distance=None" in protection_branch
    assert "sl_distance=emergency_distance" in protection_branch
    assert "notify_after_place=False" in protection_branch
    assert "거래소 Stop 없음" in protection_branch

    finalization_source = source[source.index("ema200_strategy_only_no_stop = bool("):]
    assert "strategy_managed_no_stop=True" in finalization_source
    assert "STRATEGY_MANAGED_NO_STOP" in finalization_source


def test_ema200_entry_notice_receives_fixed_2h_plan_instead_of_common_timeframe():
    source = inspect.getsource(emas.SignalEngine.entry)
    notice_start = source.index("# Build the user-facing message")
    notice_setup = source[
        notice_start:
        source.index("# =====", notice_start)
    ]
    assert "entry_notice_plan = ema200_entry_plan" in notice_setup
    assert "active_strategy == EMA200_UTBOT_RSI_STRATEGY" in notice_setup
    assert "entry_plan=entry_notice_plan" in notice_setup

    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    engine.ctrl = SimpleNamespace(
        format_symbol_for_display=lambda symbol: symbol,
        get_network_status_label=lambda: "테스트넷(데모)",
        get_exchange_display_name=lambda: "BINANCE FUTURES",
    )
    engine.last_stateful_diag = {}
    engine.last_entry_reason = {"DOGE/USDT:USDT": "EMA200 2h entry"}
    engine.get_runtime_strategy_params = lambda: {
        "active_strategy": EMA200_UTBOT_RSI_STRATEGY,
    }
    engine.get_runtime_common_settings = lambda: {"entry_timeframe": "4h"}
    engine._get_exit_timeframe = lambda symbol=None: "2h"

    notice = engine._build_signal_entry_notice(
        "DOGE/USDT:USDT",
        "long",
        5896,
        0.09,
        0.09,
        entry_plan={"entry_timeframe": "2h", "timeframe": "2h"},
        leverage=5,
    )

    assert "TF: 진입 `2h` / 청산 `2h`" in notice
    assert "진입 `4h`" not in notice


def test_ema200_entry_notice_labels_stop_by_actual_risk_mode():
    source = inspect.getsource(emas.SignalEngine.entry)
    protection_branch = source.rsplit(
        "elif active_strategy == EMA200_UTBOT_RSI_STRATEGY:", 1
    )[1].split("elif active_strategy in UTBREAKOUT_STRATEGIES:", 1)[0]

    assert "소액계좌 연속손실 단계 보호" in protection_branch
    assert "위험예산 기반 최후 안전선" in protection_branch
    assert "🛟 비상 손절 가격거리" in protection_branch
    assert "(연속손실 보호)" not in protection_branch


def test_minimum_notional_branch_blocks_instead_of_auto_increasing_strategy_size():
    source = inspect.getsource(emas.SignalEngine.entry)
    start = source.index("if min_notional > 0 and target_notional < min_notional:")
    auto_bump = source.index("# If balance/leverage can support exchange minimum", start)
    strategy_block = source[start:auto_bump]
    assert "active_strategy == EMA200_UTBOT_RSI_STRATEGY" in strategy_block
    assert "return" in strategy_block
    assert "target_notional = min_notional" not in strategy_block


def test_common_daily_breaker_is_bypassed_without_touching_mandatory_safety_paths():
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    engine.get_runtime_strategy_params = lambda: {
        "active_strategy": EMA200_UTBOT_RSI_STRATEGY,
    }
    engine._fetch_active_position_symbols_checked = lambda: (_ for _ in ()).throw(
        AssertionError("common forced-close path must not run")
    )

    assert asyncio.run(engine.check_daily_loss_limit()) is False
    entry_source = inspect.getsource(emas.SignalEngine.entry)
    assert "_submit_idempotent_crypto_entry" in entry_source
    assert "_preflight_liquidation_safety" in entry_source
    assert "_verify_actual_liquidation_safety" in entry_source


class _TelegramConfig(dict):
    def __init__(self):
        super().__init__({"binance_futures": {"strategy_params": {}}})
        self.updates = []

    async def update_value(self, path, value):
        self.updates.append((list(path), value))


class _TelegramApp:
    def __init__(self):
        self.handlers = []

    def add_handler(self, handler, group=0):
        self.handlers.append((handler, group))


class _TelegramMessage:
    def __init__(self, text):
        self.text = text
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


class _TelegramQuery:
    def __init__(self, data):
        self.data = data
        self.edits = []

    async def answer(self):
        return None

    async def edit_message_text(self, text, **kwargs):
        self.edits.append(text)


def _registered_telegram_controller():
    controller = ControllerEMA200UTBotRSIMixin()
    controller.tg_app = _TelegramApp()
    controller.cfg = _TelegramConfig()
    controller.get_active_trade_section = lambda: "binance_futures"
    controller.is_upbit_mode = lambda: False
    controller._register_ema200_utbot_rsi_handlers(
        lambda callback: callback,
        filters.TEXT & ~filters.COMMAND,
    )
    return controller


def test_telegram_status_uses_real_evaluation_order_when_2h_timestamps_tie():
    controller = _registered_telegram_controller()
    controller.db = SimpleNamespace(
        get_daily_stats=lambda: (0, 0.0),
        get_weekly_stats=lambda: (0, 0.0),
        get_consecutive_strategy_losses=lambda strategy: 1,
    )
    base_detail = {
        "closed_candle_ts": 1_000,
        "closed_candle_close": 100.0,
        "ema200": 99.0,
        "ut_state": "long",
        "ut_last_signal_side": "long",
        "prev_rsi": 49.0,
        "curr_rsi": 49.5,
    }
    controller.engines = {
        "signal": SimpleNamespace(
            last_ema200_utbot_rsi_status={
                "BTC/USDT": {**base_detail, "evaluated_at_ns": 10},
                "ETH/USDT": {**base_detail, "evaluated_at_ns": 20},
            },
            last_entry_reason={"ETH/USDT": "latest evaluation"},
            last_ema200_candidate_selection={
                "reason": "전체 유효 후보 중 최고 순위 진입 완료",
                "selected": {
                    "symbol": "ETH/USDT:USDT",
                    "side": "long",
                    "score": 88.5,
                },
                "candidates": [{
                    "rank": 1,
                    "symbol": "ETH/USDT:USDT",
                    "side": "long",
                    "score": 88.5,
                    "ut_age_bars": 1.0,
                    "rsi_momentum": 2.5,
                }],
            },
        )
    }

    status = asyncio.run(controller._ema200_utbot_rsi_status_text())

    assert "최근 조건 (ETH/USDT)" in status
    assert "최근 평가 종목(2개 기록): ETH/USDT, BTC/USDT" in status
    assert "스캔 종목: BTC, ETH, BNB, XRP, SOL, TRX, ZEC, HYPE, DOGE, XMR" in status
    assert "소액계좌 다음 단계(해당 시): 증거금 35% / 5x" in status
    assert "UT 반대 신호 + 비상 Stop" in status
    assert "최적 후보 선택: ON" in status
    assert "선택: ETH/USDT:USDT LONG / 점수 88.50" in status


def test_telegram_status_shows_actual_large_account_entry_amounts_and_ratios():
    controller = _registered_telegram_controller()
    controller.db = SimpleNamespace(
        get_daily_stats=lambda: (0, 0.0),
        get_weekly_stats=lambda: (0, 0.0),
        get_consecutive_strategy_losses=lambda strategy: 0,
    )

    async def get_balance_info():
        return 5000.0, 5000.0, 0.0

    controller.engines = {
        "signal": SimpleNamespace(
            get_balance_info=get_balance_info,
            last_ema200_utbot_rsi_status={},
            last_entry_reason={},
        )
    }

    status = asyncio.run(controller._ema200_utbot_rsi_status_text())

    assert "현재 계좌: 5000.00 USDT → 위험예산 방식" in status
    assert "1회 허용손실: 25.00 USDT (계좌의 0.50%)" in status
    assert "비상 손절 가격거리: 진입가 대비 5.00%" in status
    assert "예상 명목 포지션: 500.00 USDT (계좌의 10.00%)" in status
    assert "예상 사용 증거금: 100.00 USDT (계좌의 2.00%, 5x)" in status


def test_telegram_sizing_preview_recalculates_entry_ratio_for_wider_stop():
    controller = _registered_telegram_controller()

    async def get_balance_info():
        return 5000.0, 5000.0, 0.0

    controller.engines = {
        "signal": SimpleNamespace(get_balance_info=get_balance_info)
    }
    cfg = normalize_ema200_utbot_rsi_config(
        {
            "risk_per_trade_percent": 0.5,
            "emergency_exit_percent": 25.0,
            "leverage": 5,
        }
    )

    preview = asyncio.run(
        controller._ema200_utbot_rsi_sizing_preview(cfg, loss_streak=0)
    )

    assert "비상 손절 가격거리: 진입가 대비 25.00%" in preview
    assert "예상 명목 포지션: 100.00 USDT (계좌의 2.00%)" in preview
    assert "예상 사용 증거금: 20.00 USDT (계좌의 0.40%, 5x)" in preview


def test_telegram_emergency_help_explains_distance_and_inverse_position_sizing():
    help_text = ControllerEMA200UTBotRSIMixin._ema200_utbot_rsi_help_text(
        "emergency"
    )

    assert "포지션에 넣는 비율이 아니라" in help_text
    assert "손절거리 5%" in help_text
    assert "명목 포지션 약 500 USDT" in help_text
    assert "증거금 약 100 USDT(계좌의 2%)" in help_text
    assert "손절거리 25%" in help_text
    assert "명목 포지션 약 100 USDT" in help_text
    assert "진입금액은 작아집니다" in help_text
    assert "청산가보다 늦어질 수 있어 진입 자체가 차단" in help_text


def test_telegram_keyboard_labels_emergency_percent_as_stop_distance():
    controller = _registered_telegram_controller()
    labels = [
        button.text
        for row in controller._build_ema200_utbot_rsi_keyboard().inline_keyboard
        for button in row
    ]

    assert "손절거리 5%" in labels
    assert "✍️ 손절거리 직접입력" in labels
    assert all("비상탈출" not in label for label in labels)


def test_telegram_keyboard_exposes_best_candidate_toggle_and_help():
    controller = _registered_telegram_controller()
    buttons = [
        button
        for row in controller._build_ema200_utbot_rsi_keyboard().inline_keyboard
        for button in row
    ]

    assert any(
        button.text == "🏆 최적후보: ON"
        and button.callback_data == "e2h:candidate_toggle"
        for button in buttons
    )
    help_text = controller._ema200_utbot_rsi_help_text("candidate")
    assert "10개 종목을 동일한 완료 2시간봉" in help_text
    assert "점수는 후보의 순서만 정하며" in help_text
    assert "기존 포지션" in help_text


def test_telegram_exit_timeframe_buttons_update_dedicated_strategy_setting():
    controller = _registered_telegram_controller()
    buttons = [
        button for row in controller._build_ema200_utbot_rsi_keyboard().inline_keyboard
        for button in row
    ]
    assert {button.callback_data for button in buttons if button.callback_data.startswith('e2h:exit_tf:')} == {
        'e2h:exit_tf:15m', 'e2h:exit_tf:30m', 'e2h:exit_tf:1h',
    }
    handler = next(handler for handler, _ in controller.tg_app.handlers
                   if isinstance(handler, CallbackQueryHandler))
    query = _TelegramQuery('e2h:exit_tf:1h')
    asyncio.run(handler.callback(SimpleNamespace(callback_query=query), None))
    assert controller.cfg.updates[-1] == (
        ['binance_futures', 'strategy_params', 'EMA200UTBotRSI2H', 'exit_timeframe'], '1h'
    )
    assert '현재 포지션' in query.edits[-1]


def test_telegram_best_candidate_toggle_updates_only_strategy_selector_flag():
    controller = _registered_telegram_controller()
    handler = next(
        handler
        for handler, _ in controller.tg_app.handlers
        if isinstance(handler, CallbackQueryHandler)
    )
    query = _TelegramQuery("e2h:candidate_toggle")

    asyncio.run(
        handler.callback(
            SimpleNamespace(callback_query=query),
            SimpleNamespace(user_data={}),
        )
    )

    assert controller.cfg.updates == [
        (
            [
                "binance_futures",
                "strategy_params",
                "EMA200UTBotRSI2H",
                "best_candidate_selection_enabled",
            ],
            False,
        )
    ]
    assert query.edits and "최적 후보 선택 OFF" in query.edits[-1]


@pytest.mark.parametrize("raw_value", ["9", "nan", "inf"])
def test_telegram_direct_risk_input_rejects_out_of_range_and_nonfinite(raw_value):
    controller = _registered_telegram_controller()
    handler, group = next(
        (handler, group)
        for handler, group in controller.tg_app.handlers
        if isinstance(handler, MessageHandler)
    )
    message = _TelegramMessage(raw_value)
    context = SimpleNamespace(user_data={"ema200_utbot_rsi_custom": "risk"})

    with pytest.raises(ApplicationHandlerStop):
        asyncio.run(handler.callback(SimpleNamespace(message=message), context))

    assert group == -2
    assert controller.cfg.updates == []
    assert message.replies


def test_telegram_custom_handler_without_state_returns_without_stopping_other_groups():
    controller = _registered_telegram_controller()
    handler, group = next(
        (handler, group)
        for handler, group in controller.tg_app.handlers
        if isinstance(handler, MessageHandler)
    )
    message = _TelegramMessage("ordinary text")

    result = asyncio.run(
        handler.callback(
            SimpleNamespace(message=message),
            SimpleNamespace(user_data={}),
        )
    )

    assert group == -2
    assert result is None
    assert message.replies == []


def test_telegram_activation_is_blocked_when_position_is_open():
    controller = _registered_telegram_controller()

    async def has_open_position():
        return True, "BTC/USDT"

    controller._ema200_utbot_rsi_has_open_position = has_open_position
    handler = next(
        handler
        for handler, _ in controller.tg_app.handlers
        if isinstance(handler, CallbackQueryHandler)
    )
    query = _TelegramQuery("e2h:activate")

    asyncio.run(
        handler.callback(
            SimpleNamespace(callback_query=query),
            SimpleNamespace(user_data={}),
        )
    )

    assert controller.cfg.updates == []
    assert query.edits
    assert "열린 포지션" in query.edits[-1]


def test_telegram_activation_fails_closed_when_position_lookup_fails():
    controller = _registered_telegram_controller()

    async def position_lookup_failed():
        return None, "포지션 조회 실패: timeout"

    controller._ema200_utbot_rsi_has_open_position = position_lookup_failed
    handler = next(
        handler
        for handler, _ in controller.tg_app.handlers
        if isinstance(handler, CallbackQueryHandler)
    )
    query = _TelegramQuery("e2h:activate")

    asyncio.run(
        handler.callback(
            SimpleNamespace(callback_query=query),
            SimpleNamespace(user_data={}),
        )
    )

    assert controller.cfg.updates == []
    assert query.edits
    assert "전략 변경을 중단" in query.edits[-1]


def test_telegram_quick_leverage_button_updates_strategy_leverage():
    controller = _registered_telegram_controller()
    handler = next(
        handler
        for handler, _ in controller.tg_app.handlers
        if isinstance(handler, CallbackQueryHandler)
    )
    query = _TelegramQuery("e2h:leverage:5")

    asyncio.run(
        handler.callback(
            SimpleNamespace(callback_query=query),
            SimpleNamespace(user_data={}),
        )
    )

    assert controller.cfg.updates == [
        (
            [
                "binance_futures",
                "strategy_params",
                "EMA200UTBotRSI2H",
                "leverage",
            ],
            5,
        )
    ]
    assert query.edits


def test_telegram_daily_input_raises_weekly_limit_to_preserve_valid_ordering():
    controller = _registered_telegram_controller()
    handler, _ = next(
        (handler, group)
        for handler, group in controller.tg_app.handlers
        if isinstance(handler, MessageHandler)
    )
    message = _TelegramMessage("6")
    context = SimpleNamespace(user_data={"ema200_utbot_rsi_custom": "daily"})

    with pytest.raises(ApplicationHandlerStop):
        asyncio.run(handler.callback(SimpleNamespace(message=message), context))

    assert controller.cfg.updates == [
        (
            [
                "binance_futures",
                "strategy_params",
                "EMA200UTBotRSI2H",
                "daily_loss_limit_percent",
            ],
            6.0,
        ),
        (
            [
                "binance_futures",
                "strategy_params",
                "EMA200UTBotRSI2H",
                "weekly_loss_limit_percent",
            ],
            6.0,
        ),
    ]


def _ema_existing_stop_scanner_fixture(
    *,
    side,
    existing_stop,
    reduce_only,
    order_side=None,
    order_qty=1.0,
):
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    symbol = 'BTC/USDT:USDT'
    pos = {
        'symbol': symbol,
        'side': side,
        'entryPrice': 100.0,
        'markPrice': 101.2 if side == 'long' else 98.8,
        'contracts': 1.0,
        'leverage': 5.0,
        'info': {},
    }
    order = {
        'id': 'existing-manual-stop',
        'clientOrderId': 'manual-stop',
        'symbol': symbol,
        'type': 'STOP_MARKET',
        'side': order_side or ('sell' if side == 'long' else 'buy'),
        'amount': order_qty,
        'stopPrice': existing_stop,
        'reduceOnly': reduce_only,
    }
    replacements = []
    statuses = []

    engine._position_entry_strategy = lambda _symbol: EMA200_UTBOT_RSI_STRATEGY
    engine._fetch_server_position_checked = lambda _symbol: asyncio.sleep(
        0, result=(True, dict(pos))
    )
    engine._ema200_resolve_position_leverage = lambda _symbol, _pos: asyncio.sleep(
        0, result=(5.0, 'fixture', None, None)
    )
    engine._collect_protection_orders_checked = lambda _symbol: asyncio.sleep(
        0, result=(True, [dict(order)])
    )
    engine.safe_price = lambda _symbol, price: float(price)
    engine._set_ema200_profit_stop_status = (
        lambda _symbol, status, **kwargs: statuses.append(status)
    )

    async def replace(_symbol, _pos, stop_price, reason=''):
        replacement = {
            'id': 'ema-managed-replacement',
            'clientOrderId': 'ema-managed-replacement-client',
            'symbol': _symbol,
            'type': 'STOP_MARKET',
            'side': 'sell' if side == 'long' else 'buy',
            'amount': 1.0,
            'stopPrice': float(stop_price),
            'reduceOnly': True,
        }
        replacements.append(replacement)
        return replacement

    engine._replace_stop_loss_order = replace
    engine._audit_protection_orders = lambda *args, **kwargs: asyncio.sleep(
        0, result={'status': 'OK', 'sl_present': True}
    )
    return engine, symbol, pos, order, replacements, statuses


def test_non_reduce_only_manual_stop_does_not_block_ema_profit_stop():
    engine, symbol, _, _, replacements, statuses = (
        _ema_existing_stop_scanner_fixture(
            side='long',
            existing_stop=102.0,
            reduce_only=False,
        )
    )

    asyncio.run(engine._ema200_apply_margin_profit_stop(symbol))

    assert len(replacements) == 1
    assert 'UNCHANGED_BETTER_OR_EQUAL_STOP' not in statuses


def test_non_reduce_only_manual_short_stop_does_not_block_ema_profit_stop():
    engine, symbol, _, _, replacements, statuses = (
        _ema_existing_stop_scanner_fixture(
            side='short',
            existing_stop=98.0,
            reduce_only=False,
        )
    )

    asyncio.run(engine._ema200_apply_margin_profit_stop(symbol))

    assert len(replacements) == 1
    assert 'UNCHANGED_BETTER_OR_EQUAL_STOP' not in statuses


def test_reduce_only_external_better_stop_preserves_floor():
    engine, symbol, _, _, replacements, statuses = (
        _ema_existing_stop_scanner_fixture(
            side='long',
            existing_stop=102.0,
            reduce_only=True,
        )
    )

    asyncio.run(engine._ema200_apply_margin_profit_stop(symbol))

    assert replacements == []
    assert 'UNCHANGED_BETTER_OR_EQUAL_STOP' in statuses


def test_wrong_qty_external_stop_is_not_protective_winner():
    engine, symbol, _, _, replacements, _ = (
        _ema_existing_stop_scanner_fixture(
            side='long',
            existing_stop=102.0,
            reduce_only=True,
            order_qty=2.0,
        )
    )

    asyncio.run(engine._ema200_apply_margin_profit_stop(symbol))

    assert len(replacements) == 1


def test_wrong_side_external_stop_is_not_protective_winner():
    engine, symbol, _, _, replacements, _ = (
        _ema_existing_stop_scanner_fixture(
            side='long',
            existing_stop=102.0,
            reduce_only=True,
            order_side='buy',
        )
    )

    asyncio.run(engine._ema200_apply_margin_profit_stop(symbol))

    assert len(replacements) == 1


def test_filled_cancelled_stop_stale_live_position_does_not_submit_replacement():
    engine, symbol, pos, submissions, locks = _cancel_confirmation_fixture(
        lookup_behavior='filled'
    )
    engine._fetch_server_position_checked = lambda _symbol: asyncio.sleep(
        0, result=(True, dict(pos))
    )

    result = asyncio.run(engine._replace_stop_loss_order(
        symbol,
        pos,
        101.0,
        reason='EMA200 margin ROI 6.00% locks 5%',
    ))

    assert result is None
    assert submissions == []
    assert any(
        'PENDING_PROTECTION_RECONCILIATION' in str(value)
        for value in locks
    )


def test_audit_malformed_position_mode_response_never_mutates_protection():
    engine, symbol, pos, _, cancelled, locks = _binance_audit_mode_fixture(
        hedged=None
    )

    status = asyncio.run(engine._audit_protection_orders(
        symbol,
        pos=pos,
        expected_tp=False,
        expected_sl=True,
        alert=False,
    ))

    assert cancelled == []
    assert status['status'] == 'POSITION_MODE_UNAVAILABLE'
    assert any('POSITION_MODE_UNAVAILABLE' in reason for reason in locks)


def test_filled_cancelled_stop_flat_position_does_not_submit_replacement():
    engine, symbol, pos, submissions, locks = _cancel_confirmation_fixture(
        lookup_behavior='filled'
    )
    engine._fetch_server_position_checked = lambda _symbol: asyncio.sleep(
        0, result=(True, None)
    )

    result = asyncio.run(engine._replace_stop_loss_order(
        symbol,
        pos,
        101.0,
        reason='EMA200 margin ROI 6.00% locks 5%',
    ))

    assert result is None
    assert submissions == []
    assert any(
        'PENDING_PROTECTION_RECONCILIATION' in str(value)
        for value in locks
    )


def test_binanceusdm_algo_stop_submission_uses_one_way_guard_and_algo_gateway():
    symbol = 'BTC/USDT:USDT'
    submitted = []

    class Exchange:
        id = 'binanceusdm'

        def fetch_position_mode(self, _symbol=None):
            return {'hedged': False}

        def market(self, _symbol):
            return {'id': 'BTCUSDT'}

        def fetch_open_orders(self, _symbol=None):
            return []

        def fapiPrivateGetOpenAlgoOrders(self, params):
            return []

        def fapiPrivateGetAlgoOrder(self, params):
            raise RuntimeError('-2013 Order does not exist.')

        def fapiPrivatePostAlgoOrder(self, params):
            submitted.append(dict(params))
            return {
                'algoId': 'algo-usdm-1',
                'clientAlgoId': params['clientAlgoId'],
                'symbol': params['symbol'],
                'orderType': params['type'],
                'side': params['side'],
                'quantity': str(params['quantity']),
                'triggerPrice': str(params['triggerPrice']),
                'reduceOnly': params['reduceOnly'],
                'workingType': params['workingType'],
                'algoStatus': 'NEW',
            }

    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    engine.exchange = Exchange()
    engine.is_upbit_mode = lambda: False
    engine.last_protection_order_status = {}
    engine.last_protection_alert_ts = {}
    engine._set_crypto_entry_lock = lambda reason: None

    order = asyncio.run(engine._create_protection_order_with_retries(
        symbol,
        'STOP_MARKET',
        'sell',
        1.0,
        None,
        {
            'newClientOrderId': 'ema-usdm-stop',
            'stopPrice': 101.0,
            'reduceOnly': True,
        },
        'EMA200 SL',
        max_attempts=1,
        retry_delay_sec=0,
    ))

    assert order is not None
    assert len(submitted) == 1
    assert submitted[0]['symbol'] == 'BTCUSDT'
    assert submitted[0]['clientAlgoId'] == 'ema-usdm-stop'
    assert submitted[0]['reduceOnly'] == 'true'


def test_confirmed_profit_stop_turns_off_current_no_stop_exception(tmp_path):
    symbol = 'BTC/USDT:USDT'
    store = SQLiteTradingStateStore(tmp_path / 'state.sqlite3')
    store.upsert(OrderRecord(
        client_order_id='ema-first-stage-confirm',
        symbol=symbol,
        side='long',
        strategy=EMA200_UTBOT_RSI_STRATEGY,
        signal_timestamp='1700000000',
        requested_qty=1.0,
        filled_qty=1.0,
        average_fill_price=100.0,
        order_state=OrderState.PROTECTED.value,
        metadata={'strategy_managed_no_stop': True},
    ))
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    engine.trading_state_store = store
    engine._position_signed_contracts = lambda pos: float(pos['contracts'])
    pos = {
        'symbol': symbol,
        'side': 'long',
        'entryPrice': 100.0,
        'contracts': 1.0,
    }
    order = {
        'id': 'profit-stop-confirmed',
        'clientOrderId': 'profit-stop-client',
        'symbol': symbol,
        'type': 'STOP_MARKET',
        'side': 'sell',
        'amount': 1.0,
        'stopPrice': 101.0,
        'reduceOnly': True,
    }

    assert engine._persist_ema200_profit_stop_identity(symbol, pos, order) == 1

    record = store.get('ema-first-stage-confirm')
    assert record.stop_order_id == 'profit-stop-confirmed'
    assert record.metadata['strategy_managed_no_stop'] is False
    assert record.metadata['ema200_first_stage_entry'] is True
    store.close()
