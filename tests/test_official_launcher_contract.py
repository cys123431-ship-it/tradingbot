from pathlib import Path


def test_readme_uses_only_official_launcher_entrypoint():
    readme = Path("README.md").read_text(encoding="utf-8")
    assert "python3 scripts/launch_emas.py" in readme
    assert "python3 emas.py" not in readme


def test_launcher_marks_official_execution_and_emas_blocks_direct_run():
    launcher = Path("scripts/launch_emas.py").read_text(encoding="utf-8")
    emas_source = Path("emas.py").read_text(encoding="utf-8")

    assert "TRADINGBOT_OFFICIAL_LAUNCHER" in launcher
    assert "TRADINGBOT_OFFICIAL_LAUNCHER" in emas_source
    assert "Direct execution is disabled" in emas_source
    assert "python3 scripts/launch_emas.py" in emas_source
