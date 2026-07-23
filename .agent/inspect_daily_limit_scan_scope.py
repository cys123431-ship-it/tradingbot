from pathlib import Path

TERMS = (
    "get_daily_entry_count",
    "daily_entry_count",
    "max_trade_count",
    "max_trades",
    "trade_count",
    "include_tradifi_universe",
    "tradifi_perpetual",
    "custom_universe_enabled",
    "evaluate_coin_selector",
    "_register_user_custom_entry_handlers",
    "add_handler(",
)
TARGET_FILES = {
    "emas.py",
    "config.py",
    "database.py",
    "bot_runtime/controller.py",
    "bot_runtime/controller_telegram.py",
    "bot_runtime/controller_telegram_setup.py",
    "bot_runtime/controller_custom_entry.py",
    "bot_runtime/signal_entry.py",
    "bot_runtime/signal_scanner.py",
    "utbreakout/coinselector.py",
}

for path in sorted(Path('.').rglob('*.py')):
    if any(part in {'.git', '.venv', 'venv', '__pycache__'} for part in path.parts):
        continue
    rel = path.as_posix()
    try:
        lines = path.read_text(encoding='utf-8').splitlines()
    except Exception:
        continue
    matches = [
        index
        for index, line in enumerate(lines, 1)
        if any(term.lower() in line.lower() for term in TERMS)
    ]
    if not matches:
        continue
    if rel not in TARGET_FILES and not any(
        term.lower() in "\n".join(lines).lower()
        for term in ("get_daily_entry_count", "max_trade_count", "daily_entry_count")
    ):
        continue
    print(f'===== {rel} =====')
    printed = set()
    for index in matches:
        start = max(1, index - 3)
        end = min(len(lines), index + 5)
        key = (start, end)
        if key in printed:
            continue
        printed.add(key)
        print(f'--- lines {start}-{end} ---')
        for lineno in range(start, end + 1):
            print(f'{lineno:05d}: {lines[lineno - 1]}')
