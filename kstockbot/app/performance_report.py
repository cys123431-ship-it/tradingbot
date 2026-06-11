from collections import Counter
from datetime import datetime, timedelta
from html import escape

from .market_calendar import now_kst
from .settings import Settings
from .storage import orders_storage


def n(v):
    try:
        return float(str(v).replace(',', '').replace('%', '').strip())
    except Exception:
        return None


def krw(v):
    x = n(v)
    return 'N/A' if x is None else f'{int(round(x)):,}원'


def pct(v):
    x = n(v)
    return 'N/A' if x is None else f'{x:.2f}%'


def pick(d, keys):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ''):
            return d.get(k)
    return ''


def parse_balance(data):
    if not isinstance(data, dict):
        data = {}
    rows = data.get('output1') if isinstance(data.get('output1'), list) else []
    out2 = data.get('output2')
    raw = out2[0] if isinstance(out2, list) and out2 and isinstance(out2[0], dict) else (out2 if isinstance(out2, dict) else {})
    holdings = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        h = {
            'name': pick(r, ['prdt_name', 'hts_kor_isnm']),
            'symbol': pick(r, ['pdno', 'symbol']),
            'qty': pick(r, ['hldg_qty', 'qty']),
            'pnl': pick(r, ['evlu_pfls_amt', 'pfls_amt']),
            'rate': pick(r, ['evlu_pfls_rt', 'pfls_rt']),
        }
        h['_sort'] = abs(n(h['pnl']) or 0)
        holdings.append(h)
    holdings.sort(key=lambda x: x['_sort'], reverse=True)
    return {'summary': {'total': pick(raw, ['tot_evlu_amt', 'nass_amt']), 'cash': pick(raw, ['dnca_tot_amt', 'ord_psbl_cash']), 'pnl': pick(raw, ['evlu_pfls_smtl_amt', 'tot_evlu_pfls_amt', 'evlu_pfls_amt']), 'rate': pick(raw, ['asst_icdc_erng_rt', 'evlu_pfls_rt', 'tot_evlu_pfls_rt'])}, 'holdings': holdings}


def trades(days=None, limit=10):
    data = orders_storage.load()
    if not isinstance(data, list):
        data = []
    now = now_kst()
    items = []
    for o in data:
        if not isinstance(o, dict):
            continue
        if days is not None:
            try:
                ts = datetime.fromisoformat(str(o.get('timestamp') or o.get('request_time')))
            except Exception:
                continue
            if now - ts > timedelta(days=days):
                continue
        items.append(o)
    return {'total': len(items), 'statuses': dict(Counter(str(o.get('status', 'unknown')) for o in items)), 'recent': list(reversed(items[-limit:]))}


async def build_report(kis_client):
    ready = bool(Settings.KIS_APP_KEY and Settings.KIS_APP_SECRET and Settings.KIS_ACCOUNT_NO)
    report = {'mode': Settings.MODE, 'mock': Settings.KIS_IS_MOCK, 'ready': ready, 'error': '', 'balance': parse_balance(None), 'today': trades(1), 'all': trades(None)}
    if not ready:
        report['error'] = 'KIS setup is empty.'
        return report
    try:
        report['balance'] = parse_balance(await kis_client.get_balance())
    except Exception as e:
        report['error'] = str(e)[:250]
    return report


def pnl_text(report):
    lines = ['📈 <b>KStockBot Mock PnL</b>', f"- Mode: {escape(str(report.get('mode')))}", f"- Mock: {escape(str(report.get('mock')))}"]
    if not report.get('ready'):
        lines += ['KIS app/account setup is empty.', 'Set private values only in server env or repository Secrets.']
        return '\n'.join(lines)
    if report.get('error'):
        lines += [f"Balance Error: {escape(str(report.get('error')))}"]
        return '\n'.join(lines)
    s = report['balance']['summary']
    lines += ['<b>Summary</b>', f"- Total: {escape(krw(s.get('total')))}", f"- Cash: {escape(krw(s.get('cash')))}", f"- PnL: {escape(krw(s.get('pnl')))} ({escape(pct(s.get('rate')))})", f"- Today ledger: {report.get('today', {}).get('total', 0)}"]
    hs = report['balance']['holdings']
    if hs:
        lines.append('<b>Holdings</b>')
        for h in hs[:5]:
            lines.append(f"- {escape(str(h.get('name')))} <code>{escape(str(h.get('symbol')))}</code> {escape(str(h.get('qty')))}주 {escape(krw(h.get('pnl')))} ({escape(pct(h.get('rate')))})")
    return '\n'.join(lines)


def trades_text(summary, title='Recent Trades'):
    lines = [f'🧾 <b>{escape(title)}</b>', f"- Count: {summary.get('total', 0)}", f"- Statuses: {escape(str(summary.get('statuses', {})))}"]
    for o in summary.get('recent', [])[:10]:
        lines.append(f"- {escape(str(o.get('timestamp') or o.get('request_time') or ''))} {escape(str(o.get('side','')))} <code>{escape(str(o.get('symbol','')))}</code> {escape(str(o.get('qty','')))}주 @ {escape(krw(o.get('price')))} {escape(str(o.get('status','')))}")
    return '\n'.join(lines)
