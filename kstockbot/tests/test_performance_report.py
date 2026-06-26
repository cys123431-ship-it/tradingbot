import pytest
from unittest.mock import AsyncMock

from kstockbot.app.performance_report import parse_balance, build_report, pnl_text
from kstockbot.app.settings import Settings


def test_parse_balance_summary():
    data = {'output1': [{'prdt_name': '삼성전자', 'pdno': '005930', 'hldg_qty': '2', 'evlu_pfls_amt': '1200', 'evlu_pfls_rt': '1.20'}], 'output2': [{'tot_evlu_amt': '100000', 'dnca_tot_amt': '50000', 'evlu_pfls_smtl_amt': '1200', 'asst_icdc_erng_rt': '1.20'}]}
    parsed = parse_balance(data)
    assert parsed['summary']['total'] == '100000'
    assert parsed['summary']['pnl'] == '1200'
    assert parsed['holdings'][0]['symbol'] == '005930'


def test_pnl_text_without_setup():
    report = {'mode': 'paper', 'mock': True, 'ready': False, 'error': 'empty', 'balance': parse_balance(None), 'today': {'total': 0}}
    text = pnl_text(report)
    assert 'KIS app/account setup is empty' in text


@pytest.mark.asyncio
async def test_build_report_skips_api_without_setup():
    old = (Settings.KIS_APP_KEY, Settings.KIS_APP_SECRET, Settings.KIS_ACCOUNT_NO)
    Settings.KIS_APP_KEY = ''
    Settings.KIS_APP_SECRET = ''
    Settings.KIS_ACCOUNT_NO = ''
    try:
        client = AsyncMock()
        report = await build_report(client)
        assert report['ready'] is False
        client.get_balance.assert_not_called()
    finally:
        Settings.KIS_APP_KEY, Settings.KIS_APP_SECRET, Settings.KIS_ACCOUNT_NO = old
