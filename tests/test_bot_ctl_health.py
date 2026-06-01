import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest


ROOT_DIR = Path(__file__).resolve().parents[1]


def _start_marker_process(marker):
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)", str(marker)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _bot_ctl_env(tmp_path, marker, heartbeat_file, max_age="30"):
    env = os.environ.copy()
    env.update({
        "BOT_ENTRY": str(marker),
        "HEARTBEAT_FILE": str(heartbeat_file),
        "HEARTBEAT_MAX_AGE_SEC": str(max_age),
        "PID_FILE": str(tmp_path / "emas.pid"),
        "LOG_FILE": str(tmp_path / "emas.log"),
        "PYTHON_BIN": sys.executable,
    })
    return env


@pytest.mark.skipif(shutil.which("pgrep") is None, reason="pgrep is required by bot_ctl.sh")
def test_bot_ctl_status_reports_fresh_heartbeat(tmp_path):
    marker = tmp_path / "fake_emas_entry.py"
    heartbeat_file = tmp_path / "heartbeat.json"
    heartbeat_file.write_text('{"epoch": 1}', encoding="utf-8")
    proc = _start_marker_process(marker)
    try:
        time.sleep(0.2)
        result = subprocess.run(
            ["bash", "scripts/bot_ctl.sh", "status"],
            cwd=ROOT_DIR,
            env=_bot_ctl_env(tmp_path, marker, heartbeat_file),
            capture_output=True,
            text=True,
            timeout=10,
        )
    finally:
        proc.terminate()
        proc.wait(timeout=5)

    assert result.returncode == 0
    assert "heartbeat healthy" in result.stdout


@pytest.mark.skipif(shutil.which("pgrep") is None, reason="pgrep is required by bot_ctl.sh")
def test_bot_ctl_status_fails_on_stale_heartbeat(tmp_path):
    marker = tmp_path / "fake_emas_entry.py"
    heartbeat_file = tmp_path / "heartbeat.json"
    heartbeat_file.write_text('{"epoch": 1}', encoding="utf-8")
    stale_time = time.time() - 120
    os.utime(heartbeat_file, (stale_time, stale_time))
    proc = _start_marker_process(marker)
    try:
        time.sleep(0.2)
        result = subprocess.run(
            ["bash", "scripts/bot_ctl.sh", "status"],
            cwd=ROOT_DIR,
            env=_bot_ctl_env(tmp_path, marker, heartbeat_file, max_age="1"),
            capture_output=True,
            text=True,
            timeout=10,
        )
    finally:
        proc.terminate()
        proc.wait(timeout=5)

    assert result.returncode == 1
    assert "heartbeat stale" in result.stdout
