def test_telegram_test_stubs_are_available():
    from telegram.constants import ParseMode
    from telegram.error import BadRequest, TimedOut, RetryAfter
    from telegram.request import HTTPXRequest

    assert ParseMode.HTML == "HTML"
    assert issubclass(BadRequest, Exception)
    assert issubclass(TimedOut, Exception)

    err = RetryAfter(3)
    assert getattr(err, "retry_after", None) == 3

    req = HTTPXRequest()
    assert req is not None


def test_telegram_keyboard_stubs_preserve_keyboard_and_text():
    from telegram import (
        KeyboardButton,
        ReplyKeyboardMarkup,
        InlineKeyboardButton,
        InlineKeyboardMarkup,
    )

    button = KeyboardButton("Start")
    assert button.text == "Start"

    markup = ReplyKeyboardMarkup([[button]], resize_keyboard=True)
    assert markup.keyboard == [[button]]
    assert markup.keyboard[0][0].text == "Start"
    assert markup.resize_keyboard is True

    inline_button = InlineKeyboardButton("Open", callback_data="open")
    assert inline_button.text == "Open"
    assert inline_button.callback_data == "open"

    inline_markup = InlineKeyboardMarkup([[inline_button]])
    assert inline_markup.inline_keyboard == [[inline_button]]
    assert inline_markup.keyboard == [[inline_button]]
