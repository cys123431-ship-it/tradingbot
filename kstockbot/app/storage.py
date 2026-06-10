import json
import os
import tempfile
from typing import Any
from .settings import Settings
from .logger import get_logger

logger = get_logger("storage")

class JsonStorage:
    def __init__(self, filename: str, default: Any):
        os.makedirs(Settings.RUNTIME_DIR, exist_ok=True)
        self.path = os.path.join(Settings.RUNTIME_DIR, filename)
        self.default = default

    def load(self):
        if not os.path.exists(self.path):
            return self.default.copy() if isinstance(self.default, dict) or isinstance(self.default, list) else self.default
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load {self.path}: {e}")
            return self.default.copy() if isinstance(self.default, dict) or isinstance(self.default, list) else self.default

    def save(self, data):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(self.path), prefix=".tmp_", text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self.path)
        except Exception as e:
            logger.error(f"Failed to save {self.path}: {e}")
            try:
                os.remove(tmp_path)
            except Exception:
                pass
            raise

state_storage = JsonStorage("state.json", {})
orders_storage = JsonStorage("orders.json", [])
approvals_storage = JsonStorage("approvals.json", {})
signals_storage = JsonStorage("signals.json", {})
webhook_events_storage = JsonStorage("webhook_events.json", [])
