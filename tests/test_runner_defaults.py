import pathlib
import re


def test_runner_default_true_fallbacks_removed():
    paths = [pathlib.Path("emas.py")]
    paths.extend(pathlib.Path("utbreakout").glob("*.py"))

    patterns = [
        r'runner_exit_enabled["\']?\s*,\s*True',
        r'runner_chandelier_enabled["\']?\s*,\s*True',
        r'shadow_runner_exit_enabled["\']?\s*,\s*True',
        r'runner_exit_enabled\s*=\s*True',
        r'runner_chandelier_enabled\s*=\s*True',
        r'shadow_runner_exit_enabled\s*=\s*True',
    ]

    offenders = []

    for path in paths:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pattern in patterns:
            if re.search(pattern, text):
                offenders.append(f"{path}:{pattern}")

    assert not offenders, "Runner default True fallbacks must be removed: " + repr(offenders)
