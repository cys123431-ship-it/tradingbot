import json
from pathlib import Path

import pytest
import emas


def test_manual_resume_archives_pause_file(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    runtime.mkdir()

    pause_file = runtime / "critical_pause_state.json"
    pause_file.write_text(json.dumps({
        "status": "CRITICAL_PAUSED",
        "symbol": "BTC/USDT:USDT",
        "reason": "TEST",
        "manual_resume_required": True,
    }), encoding="utf-8")

    monkeypatch.setattr(emas, "PAUSE_STATE_FILE", pause_file)

    result = emas.manual_resume_trading(
        "BTC/USDT:USDT",
        "I_CONFIRM_MANUAL_RISK_CHECK_DONE",
    )

    assert result["status"] == "RESUMED"
    assert not pause_file.exists()
    assert Path(result["archived"]).exists()


def test_manual_resume_wrong_confirm_does_not_remove_pause_file(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    runtime.mkdir()

    pause_file = runtime / "critical_pause_state.json"
    pause_file.write_text(json.dumps({
        "status": "CRITICAL_PAUSED",
        "symbol": "BTC/USDT:USDT",
    }), encoding="utf-8")

    monkeypatch.setattr(emas, "PAUSE_STATE_FILE", pause_file)

    with pytest.raises(Exception):
        emas.manual_resume_trading("BTC/USDT:USDT", "WRONG")

    assert pause_file.exists()
