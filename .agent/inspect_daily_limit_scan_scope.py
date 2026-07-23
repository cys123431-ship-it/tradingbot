from pathlib import Path

TERMS = (
    "get_daily_entry_count",
    "max_trade_count",
    "max_daily",
    "trade_count",
    "CoinSelector",
    "include_tradifi_universe",
    "custom_universe_enabled",
    "InlineKeyboardButton",
    "CallbackQueryHandler",
    "register_telegram",
)

for path in sorted(Path('.').rglob('*.py')):
    if any(part in {'.git', '.venv', 'venv', '__pycache__'} for part in path.parts):
        continue
    try:
        lines = path.read_text(encoding='utf-8').splitlines()
    except Exception:
        continue
    matches = []
    for index, line in enumerate(lines, 1):
        if any(term.lower() in line.lower() for term in TERMS):
            matches.append(index)
    if not matches:
        continue
    print(f'===== {path} =====')
    ranges = []
    for index in matches:
        start = max(1, index - 5)
        end = min(len(lines), index + 8)
        if ranges and start <= ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], max(ranges[-1][1], end))
        else:
            ranges.append((start, end))
    for start, end in ranges:
        print(f'--- lines {start}-{end} ---')
        for lineno in range(start, end + 1):
            print(f'{lineno:05d}: {lines[lineno - 1]}')
