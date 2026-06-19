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

import global_single_position_guard
import outbreakout_live_hardening_patch

global_single_position_guard.install()
utbreakout_live_hardening_patch.install()
runpy.run_path(str(ROOT / 'emas.py'), run_name='__main__')
