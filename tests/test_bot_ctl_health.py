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


def _start_shell_process_with_legacy_text():
    return subprocess.Popen(
        ["bash", "-c", "sleep 60", "python emas.py"],
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
        # These tests drive the Python bot itself, so they run in a world
        # where that bot is the enabled one. The real ops/BOT_DISABLED gate
        # is covered separately by the tests at the end of this file.
        "PYTHON_GATE_FILE": str(tmp_path / "python_enabled_here"),
    })
    return env


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required by bot_ctl.sh")
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


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required by bot_ctl.sh")
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


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required by bot_ctl.sh")
def test_bot_ctl_status_rejects_legacy_direct_emas_process(tmp_path):
    marker = tmp_path / "fake_launcher.py"
    legacy_marker = tmp_path / "emas.py"
    heartbeat_file = tmp_path / "heartbeat.json"
    heartbeat_file.write_text('{"epoch": 1}', encoding="utf-8")
    launcher = _start_marker_process(marker)
    legacy = _start_marker_process(legacy_marker)
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
        launcher.terminate()
        legacy.terminate()
        launcher.wait(timeout=5)
        legacy.wait(timeout=5)

    assert result.returncode == 1
    assert "legacy direct emas.py process detected" in result.stdout


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required by bot_ctl.sh")
def test_bot_ctl_does_not_treat_shell_script_text_as_legacy_python_process(tmp_path):
    marker = tmp_path / "fake_launcher.py"
    heartbeat_file = tmp_path / "heartbeat.json"
    heartbeat_file.write_text('{"epoch": 1}', encoding="utf-8")
    launcher = _start_marker_process(marker)
    shell = _start_shell_process_with_legacy_text()
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
        launcher.terminate()
        shell.terminate()
        launcher.wait(timeout=5)
        shell.wait(timeout=5)

    assert result.returncode == 0
    assert "heartbeat healthy" in result.stdout


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required by bot_ctl.sh")
def test_bot_ctl_ensure_compacts_oversized_log_without_restart(tmp_path):
    marker = tmp_path / "fake_emas_entry.py"
    heartbeat_file = tmp_path / "heartbeat.json"
    heartbeat_file.write_text('{"epoch": 1}', encoding="utf-8")
    env = _bot_ctl_env(tmp_path, marker, heartbeat_file)
    env.update({"LOG_MAX_BYTES": "2048", "LOG_RETAIN_BYTES": "1024"})
    log_file = Path(env["LOG_FILE"])
    log_file.write_bytes(b"A" * 5000 + b"TAIL_MARKER")
    proc = _start_marker_process(marker)
    try:
        time.sleep(0.2)
        result = subprocess.run(
            ["bash", "scripts/bot_ctl.sh", "ensure"],
            cwd=ROOT_DIR,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
    finally:
        proc.terminate()
        proc.wait(timeout=5)

    assert result.returncode == 0
    assert "compacted bot log" in result.stdout
    assert log_file.stat().st_size <= 1024
    assert log_file.read_bytes().endswith(b"TAIL_MARKER")


def _run_bot_ctl(tmp_path, marker, heartbeat_file, action, gate_file):
    env = _bot_ctl_env(tmp_path, marker, heartbeat_file)
    env["PYTHON_GATE_FILE"] = str(gate_file)
    return subprocess.run(
        ["bash", "scripts/bot_ctl.sh", action],
        cwd=ROOT_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required by bot_ctl.sh")
@pytest.mark.parametrize("action", ["start", "restart", "ensure"])
def test_bot_ctl_refuses_to_start_python_bot_when_gate_present(tmp_path, action):
    """The Python bot is retired: with the gate present nothing may start it.

    This is what makes the @reboot line and the */5 ensure cron harmless.
    """
    marker = tmp_path / "fake_emas_entry.py"
    marker.write_text("import time; time.sleep(60)", encoding="utf-8")
    heartbeat_file = tmp_path / "heartbeat.json"
    gate_file = tmp_path / "BOT_DISABLED"
    gate_file.write_text("retired", encoding="utf-8")

    result = _run_bot_ctl(tmp_path, marker, heartbeat_file, action, gate_file)

    assert result.returncode != 0
    assert "disabled" in (result.stdout + result.stderr)
    assert not (tmp_path / "emas.pid").exists()


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required by bot_ctl.sh")
def test_bot_ctl_stop_is_allowed_when_gate_present(tmp_path):
    """Stopping must stay possible: the gate blocks starts, not cleanup."""
    marker = tmp_path / "fake_emas_entry.py"
    heartbeat_file = tmp_path / "heartbeat.json"
    gate_file = tmp_path / "BOT_DISABLED"
    gate_file.write_text("retired", encoding="utf-8")

    result = _run_bot_ctl(tmp_path, marker, heartbeat_file, "stop", gate_file)

    assert result.returncode == 0
    assert "disabled" not in (result.stdout + result.stderr)
