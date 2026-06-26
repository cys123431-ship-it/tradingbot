import json
import os
from .settings import Settings
from .logger import get_logger

logger = get_logger("universe")

WATCHLIST_FILE = os.path.join(Settings.CONFIG_DIR, "watchlist.json")
EXAMPLE_WATCHLIST_FILE = os.path.join(Settings.CONFIG_DIR, "watchlist.example.json")

class Universe:
    def __init__(self):
        self.watchlist = []
        self._load_watchlist()

    def _load_watchlist(self):
        target_file = WATCHLIST_FILE if os.path.exists(WATCHLIST_FILE) else EXAMPLE_WATCHLIST_FILE
        
        try:
            with open(target_file, "r", encoding="utf-8") as f:
                raw_data = json.load(f)
            
            valid_watchlist = []
            for item in raw_data:
                symbol = item.get("symbol", "")
                if isinstance(symbol, str) and symbol.isdigit() and len(symbol) == 6:
                    valid_watchlist.append(item)
                else:
                    logger.warning(f"Invalid symbol found in watchlist and ignored: {symbol}")
                    
            self.watchlist = valid_watchlist
            logger.info(f"Loaded {len(self.watchlist)} items from {os.path.basename(target_file)}")
        except Exception as e:
            logger.error(f"Failed to load watchlist: {e}")
            self.watchlist = []

    def reload(self):
        self._load_watchlist()
        logger.info("Watchlist reloaded manually.")

    def get_symbols(self) -> list:
        return [item["symbol"] for item in self.watchlist if "symbol" in item]

    def get_watchlist(self) -> list:
        return self.watchlist

universe = Universe()
