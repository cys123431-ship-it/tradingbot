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

output = []
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
    joined = "\n".join(lines).lower()
    if rel not in TARGET_FILES and not any(
        term.lower() in joined
        for term in ("get_daily_entry_count", "max_trade_count", "daily_entry_count")
    ):
        continue
    output.append(f'===== {rel} =====')
    ranges = []
    for index in matches:
        start = max(1, index - 5)
        end = min(len(lines), index + 8)
        if ranges and start <= ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], max(ranges[-1][1], end))
        else:
            ranges.append((start, end))
    for start, end in ranges:
        output.append(f'--- lines {start}-{end} ---')
        for lineno in range(start, end + 1):
            output.append(f'{lineno:05d}: {lines[lineno - 1]}')

Path('.agent/inspection.txt').write_text('\n'.join(output) + '\n', encoding='utf-8')
print(f'wrote {len(output)} inspection lines')
