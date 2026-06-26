import os
from dotenv import load_dotenv

PACKAGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(PACKAGE_DIR, ".env")
load_dotenv(dotenv_path=ENV_PATH)

def parse_bool(value: str, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in ("true", "1", "yes", "y", "on")

def parse_int(value: str, default: int, min_value: int | None = None, max_value: int | None = None) -> int:
    try:
        result = int(str(value).strip())
    except Exception:
        return default

    if min_value is not None and result < min_value:
        return default
    if max_value is not None and result > max_value:
        return default
    return result

class Settings:
    MODE = os.getenv("KSTOCK_MODE", "analysis").lower().strip()
    BROKER = os.getenv("KSTOCK_BROKER", "KIS")
    
    KIS_APP_KEY = os.getenv("KSTOCK_KIS_APP_KEY", "")
    KIS_APP_SECRET = os.getenv("KSTOCK_KIS_APP_SECRET", "")
    KIS_ACCOUNT_NO = os.getenv("KSTOCK_KIS_ACCOUNT_NO", "")
    KIS_ACCOUNT_PRODUCT_CODE = os.getenv("KSTOCK_KIS_ACCOUNT_PRODUCT_CODE", "01")
    KIS_IS_MOCK = parse_bool(os.getenv("KSTOCK_KIS_IS_MOCK"), True)
    
    TELEGRAM_BOT_TOKEN = os.getenv("KSTOCK_TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_OWNER_ID = os.getenv("KSTOCK_TELEGRAM_OWNER_ID", "")
    
    WEBHOOK_SECRET = os.getenv("KSTOCK_WEBHOOK_SECRET", "")
    WEBHOOK_FAST_ACK = parse_bool(os.getenv("KSTOCK_WEBHOOK_FAST_ACK"), True)
    WEBHOOK_BACKGROUND_MAX_CONCURRENCY = parse_int(
        os.getenv("KSTOCK_WEBHOOK_BACKGROUND_MAX_CONCURRENCY", "1"),
        1,
        min_value=1,
        max_value=5,
    )
    
    HOST = os.getenv("KSTOCK_HOST", "0.0.0.0")
    PORT = parse_int(os.getenv("KSTOCK_PORT", "8090"), 8090, min_value=1, max_value=65535)
    TIMEZONE = os.getenv("KSTOCK_TIMEZONE", "Asia/Seoul")
    
    MAX_SCAN_SYMBOLS = parse_int(os.getenv("KSTOCK_MAX_SCAN_SYMBOLS", "20"), 20, min_value=1, max_value=200)
    
    CONFIRM_LIVE_TRADING = os.getenv("KSTOCK_CONFIRM_LIVE_TRADING", "no").lower().strip()
    
    BASE_DIR = PACKAGE_DIR
    CONFIG_DIR = os.path.join(BASE_DIR, "config")
    RUNTIME_DIR = os.path.join(BASE_DIR, "runtime")
    LOG_DIR = os.path.join(BASE_DIR, "logs")

    @classmethod
    def is_live_like_mode(cls) -> bool:
        return cls.MODE in ["manual_live", "live"]

    @classmethod
    def is_order_capable_mode(cls) -> bool:
        return cls.MODE in ["paper", "manual_live", "live"]

    @classmethod
    def is_paper_mode(cls) -> bool:
        return cls.MODE == "paper"

    @classmethod
    def validate(cls):
        if cls.MODE not in ["analysis", "paper", "manual_live", "live"]:
            print(f"Warning: Invalid KSTOCK_MODE '{cls.MODE}'. Defaulting to 'analysis'.")
            cls.MODE = "analysis"
            
        if cls.MODE == "live":
            if cls.CONFIRM_LIVE_TRADING != "yes-i-understand-real-money-risk":
                raise RuntimeError("Live trading requires KSTOCK_CONFIRM_LIVE_TRADING='yes-i-understand-real-money-risk'")
            if cls.KIS_IS_MOCK:
                raise RuntimeError("Live mode cannot run with KSTOCK_KIS_IS_MOCK=true")
                
        if cls.MODE in ["manual_live", "live"]:
            if not cls.TELEGRAM_BOT_TOKEN or not cls.TELEGRAM_OWNER_ID:
                raise RuntimeError("Live/Manual_live modes require Telegram Token and Owner ID for safety")
                
        # Webhook secret check handled gracefully in processor, but good to warn
        if not cls.WEBHOOK_SECRET or cls.WEBHOOK_SECRET == "change-me":
            print("Warning: WEBHOOK_SECRET is empty or default. Webhook processing will be rejected.")

Settings.validate()
