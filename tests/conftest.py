import sys
import types
import importlib.util


class _DummyModule(types.ModuleType):
    def __getattr__(self, name):
        dummy = _DummyObject(name)
        setattr(self, name, dummy)
        return dummy


class _DummyObject:
    def __init__(self, name="Dummy"):
        self.__name__ = name

    def __call__(self, *args, **kwargs):
        return _DummyObject(self.__name__)

    def __getattr__(self, name):
        return _DummyObject(name)

    def __iter__(self):
        return iter(())

    def __bool__(self):
        return False


def _install_stub_module(name):
    if name in sys.modules:
        return

    try:
        if importlib.util.find_spec(name) is not None:
            return
    except Exception:
        pass

    module = _DummyModule(name)
    module.__dict__.update({
        "__all__": [],
        "__version__": "0.0-test-stub",
        "_codex_test_stub": True,
    })
    sys.modules[name] = module


# Optional/runtime dependencies that should not block structural tests.
for _name in [
    "ccxt",
    "pandas_ta",
    "pykalman",
    "telegram",
    "telegram.ext",
    "telegram.constants",
    "telegram.error",
    "telegram.request",
]:
    _install_stub_module(_name)


# ---------------------------------------------------------------------
# python-telegram-bot test stubs
# ---------------------------------------------------------------------
# These stubs are for test collection only. They keep structural tests
# executable in minimal sandboxes where python-telegram-bot is not installed.
# ---------------------------------------------------------------------

if getattr(sys.modules.get("telegram.constants"), "_codex_test_stub", False):
    telegram_constants = sys.modules["telegram.constants"]

    class ParseMode:
        HTML = "HTML"
        MARKDOWN = "MARKDOWN"
        MARKDOWN_V2 = "MARKDOWN_V2"

    telegram_constants.ParseMode = ParseMode


if getattr(sys.modules.get("telegram.error"), "_codex_test_stub", False):
    telegram_error = sys.modules["telegram.error"]

    class TelegramError(Exception):
        pass

    class NetworkError(TelegramError):
        pass

    class BadRequest(TelegramError):
        pass

    class TimedOut(NetworkError):
        pass

    class RetryAfter(TelegramError):
        def __init__(self, retry_after=0, *args, **kwargs):
            super().__init__(f"Retry after {retry_after}")
            self.retry_after = retry_after

    class Forbidden(TelegramError):
        pass

    class Unauthorized(TelegramError):
        pass

    telegram_error.TelegramError = TelegramError
    telegram_error.NetworkError = NetworkError
    telegram_error.BadRequest = BadRequest
    telegram_error.TimedOut = TimedOut
    telegram_error.RetryAfter = RetryAfter
    telegram_error.Forbidden = Forbidden
    telegram_error.Unauthorized = Unauthorized


if getattr(sys.modules.get("telegram.request"), "_codex_test_stub", False):
    telegram_request = sys.modules["telegram.request"]

    class HTTPXRequest:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    telegram_request.HTTPXRequest = HTTPXRequest


if getattr(sys.modules.get("telegram"), "_codex_test_stub", False):
    telegram = sys.modules["telegram"]

    class Bot:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        async def send_message(self, *args, **kwargs):
            return {"ok": True, "method": "send_message", "args": args, "kwargs": kwargs}

        async def send_photo(self, *args, **kwargs):
            return {"ok": True, "method": "send_photo", "args": args, "kwargs": kwargs}

        async def send_document(self, *args, **kwargs):
            return {"ok": True, "method": "send_document", "args": args, "kwargs": kwargs}

    class Update:
        pass

    class _TelegramObject:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class KeyboardButton(_TelegramObject):
        def __init__(self, text=None, *args, **kwargs):
            super().__init__(text, *args, **kwargs)
            self.text = text

        def __repr__(self):
            return f"KeyboardButton(text={self.text!r})"

    class ReplyKeyboardMarkup(_TelegramObject):
        def __init__(self, keyboard=None, *args, **kwargs):
            super().__init__(keyboard, *args, **kwargs)
            self.keyboard = keyboard or []
            self.resize_keyboard = kwargs.get("resize_keyboard")
            self.one_time_keyboard = kwargs.get("one_time_keyboard")
            self.selective = kwargs.get("selective")
            self.input_field_placeholder = kwargs.get("input_field_placeholder")
            self.is_persistent = kwargs.get("is_persistent")

        def __repr__(self):
            return f"ReplyKeyboardMarkup(keyboard={self.keyboard!r})"

    class InlineKeyboardButton(_TelegramObject):
        def __init__(self, text=None, *args, **kwargs):
            super().__init__(text, *args, **kwargs)
            self.text = text
            self.callback_data = kwargs.get("callback_data")
            self.url = kwargs.get("url")
            self.login_url = kwargs.get("login_url")
            self.web_app = kwargs.get("web_app")
            self.switch_inline_query = kwargs.get("switch_inline_query")
            self.switch_inline_query_current_chat = kwargs.get("switch_inline_query_current_chat")
            self.callback_game = kwargs.get("callback_game")
            self.pay = kwargs.get("pay")

        def __repr__(self):
            return (
                "InlineKeyboardButton("
                f"text={self.text!r}, "
                f"callback_data={self.callback_data!r}, "
                f"url={self.url!r})"
            )

    class InlineKeyboardMarkup(_TelegramObject):
        def __init__(self, inline_keyboard=None, *args, **kwargs):
            super().__init__(inline_keyboard, *args, **kwargs)
            self.inline_keyboard = inline_keyboard or []
            self.keyboard = self.inline_keyboard

        def __repr__(self):
            return f"InlineKeyboardMarkup(inline_keyboard={self.inline_keyboard!r})"

    telegram.Bot = Bot
    telegram.Update = Update
    telegram.KeyboardButton = KeyboardButton
    telegram.ReplyKeyboardMarkup = ReplyKeyboardMarkup
    telegram.InlineKeyboardButton = InlineKeyboardButton
    telegram.InlineKeyboardMarkup = InlineKeyboardMarkup

    for _submodule_name in ("telegram.constants", "telegram.error", "telegram.request"):
        if _submodule_name in sys.modules:
            setattr(telegram, _submodule_name.rsplit(".", 1)[-1], sys.modules[_submodule_name])


try:
    import telegram as _telegram_module
except Exception:
    _telegram_module = sys.modules.get("telegram")

if _telegram_module is not None:
    class _TelegramObject:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class KeyboardButton(_TelegramObject):
        def __init__(self, text=None, *args, **kwargs):
            super().__init__(text, *args, **kwargs)
            self.text = text

        def __repr__(self):
            return f"KeyboardButton(text={self.text!r})"

    class ReplyKeyboardMarkup(_TelegramObject):
        def __init__(self, keyboard=None, *args, **kwargs):
            super().__init__(keyboard, *args, **kwargs)
            self.keyboard = keyboard or []
            self.resize_keyboard = kwargs.get("resize_keyboard")
            self.one_time_keyboard = kwargs.get("one_time_keyboard")
            self.selective = kwargs.get("selective")
            self.input_field_placeholder = kwargs.get("input_field_placeholder")
            self.is_persistent = kwargs.get("is_persistent")

        def __repr__(self):
            return f"ReplyKeyboardMarkup(keyboard={self.keyboard!r})"

    class InlineKeyboardButton(_TelegramObject):
        def __init__(self, text=None, *args, **kwargs):
            super().__init__(text, *args, **kwargs)
            self.text = text
            self.callback_data = kwargs.get("callback_data")
            self.url = kwargs.get("url")
            self.login_url = kwargs.get("login_url")
            self.web_app = kwargs.get("web_app")
            self.switch_inline_query = kwargs.get("switch_inline_query")
            self.switch_inline_query_current_chat = kwargs.get("switch_inline_query_current_chat")
            self.callback_game = kwargs.get("callback_game")
            self.pay = kwargs.get("pay")

        def __repr__(self):
            return (
                "InlineKeyboardButton("
                f"text={self.text!r}, "
                f"callback_data={self.callback_data!r}, "
                f"url={self.url!r})"
            )

    class InlineKeyboardMarkup(_TelegramObject):
        def __init__(self, inline_keyboard=None, *args, **kwargs):
            super().__init__(inline_keyboard, *args, **kwargs)
            self.inline_keyboard = inline_keyboard or []
            self.keyboard = self.inline_keyboard

        def __repr__(self):
            return f"InlineKeyboardMarkup(inline_keyboard={self.inline_keyboard!r})"

    _telegram_module.KeyboardButton = KeyboardButton
    _telegram_module.ReplyKeyboardMarkup = ReplyKeyboardMarkup
    _telegram_module.InlineKeyboardButton = InlineKeyboardButton
    _telegram_module.InlineKeyboardMarkup = InlineKeyboardMarkup


if getattr(sys.modules.get("telegram.ext"), "_codex_test_stub", False):
    telegram_ext = sys.modules["telegram.ext"]

    class Application:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs
            self.handlers = []

        @classmethod
        def builder(cls):
            return ApplicationBuilder()

        def add_handler(self, *args, **kwargs):
            self.handlers.append((args, kwargs))

        async def run_polling(self, *args, **kwargs):
            return None

    class ApplicationBuilder:
        def token(self, *args, **kwargs):
            return self

        def request(self, *args, **kwargs):
            return self

        def build(self):
            return Application()

    class _Handler:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class CommandHandler(_Handler):
        pass

    class MessageHandler(_Handler):
        pass

    class CallbackQueryHandler(_Handler):
        pass

    class ConversationHandler(_Handler):
        END = -1

    class ContextTypes:
        DEFAULT_TYPE = object

    class _Filter:
        def __init__(self, name="filter"):
            self.name = name

        def __and__(self, other):
            return _Filter(f"({self.name}&{getattr(other, 'name', other)})")

        def __or__(self, other):
            return _Filter(f"({self.name}|{getattr(other, 'name', other)})")

        def __invert__(self):
            return _Filter(f"~{self.name}")

    class filters:
        TEXT = _Filter("TEXT")
        COMMAND = _Filter("COMMAND")

        @staticmethod
        def Regex(*args, **kwargs):
            return _Filter("Regex")

    telegram_ext.Application = Application
    telegram_ext.ApplicationBuilder = ApplicationBuilder
    telegram_ext.CommandHandler = CommandHandler
    telegram_ext.MessageHandler = MessageHandler
    telegram_ext.CallbackQueryHandler = CallbackQueryHandler
    telegram_ext.ConversationHandler = ConversationHandler
    telegram_ext.ContextTypes = ContextTypes
    telegram_ext.filters = filters


# Provide a slightly more useful ccxt stub when ccxt is unavailable.
if "ccxt" in sys.modules:
    ccxt = sys.modules["ccxt"]

    class BaseError(Exception):
        pass

    class ExchangeError(BaseError):
        pass

    class NetworkError(BaseError):
        pass

    class RequestTimeout(NetworkError):
        pass

    class DDoSProtection(NetworkError):
        pass

    class RateLimitExceeded(NetworkError):
        pass

    class AuthenticationError(ExchangeError):
        pass

    class InsufficientFunds(ExchangeError):
        pass

    class InvalidOrder(ExchangeError):
        pass

    class OrderNotFound(ExchangeError):
        pass

    class binanceusdm:
        def __init__(self, *args, **kwargs):
            self.options = {}
            self.markets = {}

        def load_markets(self, *args, **kwargs):
            return {}

        def market(self, symbol):
            return {
                "symbol": symbol,
                "limits": {
                    "cost": {"min": 5.0},
                    "amount": {"min": 0.001},
                },
                "precision": {
                    "amount": 3,
                    "price": 2,
                },
            }

        def amount_to_precision(self, symbol, amount):
            return str(amount)

        def price_to_precision(self, symbol, price):
            return str(price)

        def create_order(self, *args, **kwargs):
            return {"id": "stub-order", "args": args, "kwargs": kwargs}

        def fetch_balance(self, *args, **kwargs):
            return {"USDT": {"total": 100.0, "free": 100.0}, "info": {"totalWalletBalance": "100"}}

        def fetch_ohlcv(self, *args, **kwargs):
            rows = []
            for i in range(250):
                price = 100.0 + i * 0.01
                rows.append([i, price, price + 1.0, price - 1.0, price, 1000.0])
            return rows

    ccxt.BaseError = BaseError
    ccxt.ExchangeError = ExchangeError
    ccxt.NetworkError = NetworkError
    ccxt.RequestTimeout = RequestTimeout
    ccxt.DDoSProtection = DDoSProtection
    ccxt.RateLimitExceeded = RateLimitExceeded
    ccxt.AuthenticationError = AuthenticationError
    ccxt.InsufficientFunds = InsufficientFunds
    ccxt.InvalidOrder = InvalidOrder
    ccxt.OrderNotFound = OrderNotFound
    ccxt.binanceusdm = binanceusdm
