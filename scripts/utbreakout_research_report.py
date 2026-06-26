#!/usr/bin/env python
"""Print a UT Breakout research summary from diagnostic JSONL logs."""

import argparse
import glob
import json
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from utbreakout.research import (
    format_research_summary,
    load_diagnostic_events,
    summarize_diagnostic_events,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--log",
        action="append",
        dest="logs",
        help="Diagnostic JSONL path. Can be passed more than once.",
    )
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()

    paths = args.logs
    if not paths:
        paths = sorted(glob.glob("utbreakout_diagnostics.log*"))
    paths = [str(Path(path)) for path in paths]
    events = load_diagnostic_events(paths, days=args.days)
    if args.as_json:
        print(json.dumps(summarize_diagnostic_events(events), ensure_ascii=False, indent=2))
    else:
        print(format_research_summary(events, days=args.days))


if __name__ == "__main__":
    main()
