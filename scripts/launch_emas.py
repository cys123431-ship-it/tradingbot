"""Launch emas.py with runtime safety hooks enabled."""
from __future__ import annotations

import runpy

import global_single_position_guard

global_single_position_guard.install()
runpy.run_path("emas.py", run_name="__main__")
