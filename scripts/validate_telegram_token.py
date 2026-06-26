#!/usr/bin/env python3
"""Validate the configured Telegram bot token without exposing the token."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.request


def _fail(message: str) -> int:
    print(f"Telegram token validation failed: {message}", file=sys.stderr)
    return 1


def main() -> int:
    config_path = Path(os.environ.get("TRADINGBOT_CONFIG", "config.json"))
    expected_username = os.environ.get("TELEGRAM_EXPECTED_USERNAME", "").lstrip("@")

    if not config_path.exists():
        return _fail(f"{config_path} not found")

    try:
        config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        return _fail(f"could not read {config_path}: {exc}")

    token = str((config.get("telegram") or {}).get("token") or "").strip()
    if not token:
        return _fail("telegram.token is empty")

    try:
        url = f"https://api.telegram.org/bot{token}/getMe"
        with urllib.request.urlopen(url, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8", errors="replace"))
            code = payload.get("error_code", exc.code)
            description = payload.get("description", "HTTP error")
        except Exception:
            code = exc.code
            description = "HTTP error"
        return _fail(f"Telegram API rejected token ({code}: {description})")
    except Exception as exc:
        return _fail(f"Telegram API check error: {type(exc).__name__}")

    if not payload.get("ok"):
        return _fail(str(payload.get("description") or "not ok"))

    result = payload.get("result") or {}
    username = str(result.get("username") or "")
    if expected_username and username.lower() != expected_username.lower():
        return _fail(f"token belongs to @{username or 'unknown'}, expected @{expected_username}")

    print(f"Telegram token OK: @{username or 'unknown'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
