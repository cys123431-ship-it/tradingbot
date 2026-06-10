import pytest
from unittest.mock import AsyncMock, Mock, patch

from kstockbot.app.telegram_bot import telegram_bot
from kstockbot.app.settings import Settings


def make_update():
    update = Mock()
    update.effective_user = Mock()
    update.effective_user.id = int(Settings.TELEGRAM_OWNER_ID or "123456")
    update.message = Mock()
    update.message.reply_text = AsyncMock()
    telegram_bot.owner_id = str(update.effective_user.id)
    return update


def make_context(args=None):
    context = Mock()
    context.args = args or []
    return context


@pytest.mark.asyncio
@patch("kstockbot.app.kis_client.kis_client.auth.ensure_token_valid", new_callable=AsyncMock)
async def test_kischeck_no_secret_leak(mock_token):
    mock_token.return_value = "dummy-token-value-that-must-not-appear"
    update = make_update()
    context = make_context()

    await telegram_bot.cmd_kischeck(update, context)

    update.message.reply_text.assert_called_once()
    text = update.message.reply_text.call_args.args[0]
    assert "Token OK" in text
    assert "dummy-token" not in text


@pytest.mark.asyncio
@patch("kstockbot.app.kis_client.kis_client.get_price", new_callable=AsyncMock)
async def test_price_command(mock_price):
    mock_price.return_value = {
        "output": {
            "stck_prpr": "80000",
            "prdy_ctrt": "1.23",
            "acml_vol": "1000000"
        }
    }
    update = make_update()
    context = make_context(["005930"])

    await telegram_bot.cmd_price(update, context)

    update.message.reply_text.assert_called_once()
    text = update.message.reply_text.call_args.args[0]
    assert "005930" in text
    assert "80000" in text


@pytest.mark.asyncio
async def test_price_invalid_symbol_rejected():
    update = make_update()
    context = make_context(["ABC"])

    await telegram_bot.cmd_price(update, context)

    update.message.reply_text.assert_called_once()
    text = update.message.reply_text.call_args.args[0]
    assert "6-digit" in text


@pytest.mark.asyncio
@patch("kstockbot.app.kis_client.kis_client.get_balance", new_callable=AsyncMock)
async def test_balance_command(mock_balance):
    mock_balance.return_value = {
        "output1": [
            {"prdt_name": "삼성전자", "pdno": "005930", "hldg_qty": "1"}
        ],
        "output2": [
            {"tot_evlu_amt": "100000", "dnca_tot_amt": "50000"}
        ]
    }
    update = make_update()
    context = make_context()

    await telegram_bot.cmd_balance(update, context)

    update.message.reply_text.assert_called_once()
    text = update.message.reply_text.call_args.args[0]
    assert "KIS Balance Summary" in text
    assert "005930" in text


@pytest.mark.asyncio
@patch("kstockbot.app.kis_client.kis_client.get_buyable_cash", new_callable=AsyncMock)
async def test_cash_command(mock_cash):
    mock_cash.return_value = {
        "output": {
            "ord_psbl_cash": "1000000",
            "ord_psbl_qty": "10"
        }
    }
    update = make_update()
    context = make_context(["005930", "80000"])

    await telegram_bot.cmd_cash(update, context)

    update.message.reply_text.assert_called_once()
    text = update.message.reply_text.call_args.args[0]
    assert "Buyable Cash Check" in text
    assert "ord_psbl_cash" in text


@pytest.mark.asyncio
@patch("kstockbot.app.order_manager.order_manager.submit_live_order_after_approval", new_callable=AsyncMock)
async def test_approve_command_success(mock_submit):
    mock_submit.return_value = {"order_no": "999"}
    update = make_update()
    context = make_context(["app-123"])
    
    with patch("kstockbot.app.telegram_bot.Settings.MODE", "manual_live"):
        await telegram_bot.cmd_approve(update, context)
        
    update.message.reply_text.assert_called_once()
    text = update.message.reply_text.call_args.args[0]
    assert "Approved and Submitted" in text
    assert "999" in text


@pytest.mark.asyncio
@patch("kstockbot.app.order_manager.order_manager.reject_order")
async def test_reject_command_success(mock_reject):
    mock_reject.return_value = (True, "Rejected successfully")
    update = make_update()
    context = make_context(["app-123"])
    
    await telegram_bot.cmd_reject(update, context)
    
    update.message.reply_text.assert_called_once()
    text = update.message.reply_text.call_args.args[0]
    assert "manually rejected" in text


@pytest.mark.asyncio
@patch("kstockbot.app.kis_client.kis_client.place_order", new_callable=AsyncMock)
@patch("kstockbot.app.kis_client.kis_client.get_buyable_cash", new_callable=AsyncMock)
@patch("kstockbot.app.kis_client.kis_client.get_balance", new_callable=AsyncMock)
@patch("kstockbot.app.kis_client.kis_client.get_price", new_callable=AsyncMock)
@patch("kstockbot.app.kis_client.kis_client.auth.ensure_token_valid", new_callable=AsyncMock)
async def test_dryrun_command_no_secret_leak(mock_token, mock_price, mock_balance, mock_cash, mock_place_order):
    mock_token.return_value = "dummy-token-value-that-must-not-appear"
    mock_price.return_value = {"output": {"stck_prpr": "80000"}}
    mock_balance.return_value = {"output1": [], "output2": []}
    mock_cash.return_value = {"output": {"ord_psbl_cash": "1000000"}}

    update = make_update()
    context = make_context()

    await telegram_bot.cmd_dryrun(update, context)

    # dryrun sends a start message and a result message
    assert update.message.reply_text.call_count == 2

    result_text = update.message.reply_text.call_args.args[0]
    assert "KIS Dry Run Result" in result_text
    assert "dummy-token" not in result_text
    assert "Order Execution: NO" in result_text
    assert "Sample Price 005930" in result_text
    mock_place_order.assert_not_called()


@pytest.mark.asyncio
@patch("kstockbot.app.webhook_events.list_recent_events")
async def test_webhooks_command(mock_events):
    mock_events.return_value = [
        {
            "event_id": "abc123",
            "status": "ok",
            "action": "BUY",
            "symbol": "005930",
            "updated_at": "2026-06-10T10:00:00+09:00",
            "message": "Paper order submitted",
        }
    ]

    update = make_update()
    context = make_context(["1"])

    await telegram_bot.cmd_webhooks(update, context)

    update.message.reply_text.assert_called_once()
    text = update.message.reply_text.call_args.args[0]
    assert "Recent Webhook Events" in text
    assert "abc123" in text
    assert "005930" in text
