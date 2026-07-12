from pathlib import Path
import logging.handlers
import os
import runpy
import sys

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

OriginalRotatingFileHandler = logging.handlers.RotatingFileHandler

class SafeRotatingFileHandler(OriginalRotatingFileHandler):
    def __init__(self, filename, *args, **kwargs):
        try:
            super().__init__(filename, *args, **kwargs)
        except PermissionError:
            fallback = ROOT / 'runtime' / Path(str(filename)).name
            fallback.parent.mkdir(parents=True, exist_ok=True)
            super().__init__(str(fallback), *args, **kwargs)

logging.handlers.RotatingFileHandler = SafeRotatingFileHandler

import global_single_position_guard  # noqa: E402
import utbreakout_live_hardening_patch  # noqa: E402
from trading_safety.process_lock import ProcessLock, ProcessLockError  # noqa: E402
from trading_safety.liquidation_guard import resolve_liquidation_safety_config  # noqa: E402

overlap = set(global_single_position_guard.OPPORTUNITY_OVERRIDES) & set(
    utbreakout_live_hardening_patch.PROFIT_MAX_OVERRIDES
)
conflicts = {
    key: (
        global_single_position_guard.OPPORTUNITY_OVERRIDES[key],
        utbreakout_live_hardening_patch.PROFIT_MAX_OVERRIDES[key],
    )
    for key in overlap
    if global_single_position_guard.OPPORTUNITY_OVERRIDES[key]
    != utbreakout_live_hardening_patch.PROFIT_MAX_OVERRIDES[key]
}
if conflicts:
    raise RuntimeError(f"conflicting runtime trading settings: {conflicts}")
logging.info(
    "TRADING_CONFIG_SOURCE near_miss_tp_arm_ratio=%s source=canonical_runtime_overrides",
    global_single_position_guard.OPPORTUNITY_OVERRIDES.get("near_miss_tp_arm_ratio"),
)
liquidation_safety = resolve_liquidation_safety_config()
logging.info(
    "TRADING_CONFIG_SOURCE minimum_liquidation_buffer_pct=%s "
    "minimum_liquidation_buffer_ticks=%s stop_working_type=%s priceProtect=%s "
    "auto_reduce_leverage=%s source=trading_safety.liquidation_guard",
    liquidation_safety.minimum_buffer_pct,
    liquidation_safety.minimum_buffer_ticks,
    liquidation_safety.stop_working_type,
    liquidation_safety.stop_price_protect,
    liquidation_safety.auto_reduce_leverage,
)

process_lock = ProcessLock(ROOT / 'runtime' / 'crypto_trading_bot.lock')
try:
    process_lock.acquire()
except ProcessLockError as exc:
    logging.critical("DUPLICATE_PROCESS_DETECTED: %s", exc)
    raise SystemExit(73) from exc

try:
    os.environ['TRADINGBOT_PROCESS_LOCK_HELD'] = '1'
    os.environ['TRADINGBOT_OFFICIAL_LAUNCHER'] = '1'
    global_single_position_guard.install()
    utbreakout_live_hardening_patch.install()
    runpy.run_path(str(ROOT / 'emas.py'), run_name='__main__')
finally:
    os.environ.pop('TRADINGBOT_OFFICIAL_LAUNCHER', None)
    os.environ.pop('TRADINGBOT_PROCESS_LOCK_HELD', None)
    process_lock.release()
