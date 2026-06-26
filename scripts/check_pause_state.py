import json
import os
from pathlib import Path

PAUSE_STATE_FILE = Path("runtime/critical_pause_state.json")

def main():
    if not PAUSE_STATE_FILE.exists():
        print("NO_PAUSE_STATE")
        return

    try:
        with PAUSE_STATE_FILE.open("r", encoding="utf-8") as f:
            state = json.load(f)
        print(json.dumps(state, ensure_ascii=False, indent=2))
    except Exception as e:
        print(f"ERROR_READING_PAUSE_STATE: {e}")

if __name__ == "__main__":
    main()
