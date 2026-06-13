from pathlib import Path
import runpy
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import global_single_position_guard
global_single_position_guard.install()
runpy.run_path(str(ROOT / 'emas.py'), run_name='__main__')
