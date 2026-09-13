"""Exercise the real OmniChat responses through the MAXCOURSE recharge routes.

Run with the MAXCOURSE Python and pass --omnichat-root and --omnichat-python.
The child uses only temporary SQLite databases and in-process FastAPI requests.
"""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from unittest import mock


OMNI_SCENARIO = r'''
import json
from urllib.parse import parse_qs, urlsplit
from unittest import mock
from app import db, main, payments
from starlette.testclient import TestClient

cfg = {
    'provider': 'xorpay', 'public_base_url': 'https://chat.fixture.test',
    'xorpay': {'aid': '10001', 'appsecret': 'cross-repo-fixture-secret', 'type': 'alipay'},
    'sms_market': {'usd_cny': '6.80', 'packages_usd': [1, 5, 10],
                   'return_url': 'https://www.bnbscheduler.top/sms-lab/'}
}
with db.get_auth_db() as conn:
    cur = conn.execute("INSERT INTO users(username,pw_hash,salt,credits) VALUES ('alice',X'00',X'00',321)")
    uid = cur.lastrowid
    conn.execute('INSERT INTO tokens(token,user_id) VALUES (?,?)', ('shared-secret-never-echo', uid))
headers = {'Authorization': 'Bearer shared-secret-never-echo'}
client = TestClient(main.app)
with mock.patch.object(payments, 'config', lambda: cfg):
    first = client.post('/api/recharge/sms-market/orders', headers=headers,
                         json={'package_usd': 1, 'request_id': 'cross-conflict-request'})
    assert first.status_code == 200, first.text
    conflict = client.post('/api/recharge/sms-market/orders', headers=headers,
                            json={'package_usd': 5, 'request_id': 'cross-conflict-request'})
    assert conflict.status_code == 409, conflict.text
    with mock.patch.object(main, 'RECHARGE_PENDING_CAP', 0):
        limited = client.post('/api/recharge/sms-market/orders', headers=headers,
                               json={'package_usd': 5, 'request_id': 'cross-limited-request'})
    assert limited.status_code == 429, limited.text
    checkout = client.get(first.json()['pay_url'], headers=headers, follow_redirects=False)
    generated_return = parse_qs(urlsplit(checkout.headers['location']).query)['return_url'][0]
    assert generated_return == 'https://www.bnbscheduler.top/sms-lab/?recharge_order=' + first.json()['order']['id']
    db.recharge_finish(first.json()['order']['id'], 'credited', external_id='cross-fixture-paid',
                       expected_channel='xorpay', expected_app='sms_market', expected_amount_fen=680)
    for index in range(100):
        db.recharge_create_once(f'S-cross-{index}', uid, 680, 10000, 0, 'xorpay',
                                request_id=f'cross-pending-{index}', pending_cap=101, app='sms_market')
    pages = []
    cursor = None
    while True:
        page = client.get('/api/recharge/sms-market/orders', headers=headers,
                           params={} if cursor is None else {'cursor': cursor})
        assert page.status_code == 200, page.text
        pages.append(page.json())
        cursor = page.json()['next_cursor']
        if cursor is None:
            break
    assert [len(page['orders']) for page in pages] == [100, 1]
    assert db.get_credits(uid) == 321
    print(json.dumps({'errors': [{'status': response.status_code, 'body': response.json()}
                                for response in (conflict, limited)], 'pages': pages,
                      'paid_order': first.json()['order']['id'], 'generated_return': generated_return}))
client.close()
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--omnichat-root', required=True, type=Path)
    parser.add_argument('--omnichat-python', required=True, type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(root / 'tests'))
    from test_sms_lab import SMSRechargeTests
    import sso_bridge

    with tempfile.TemporaryDirectory(prefix='sms-cross-repo-') as directory:
        auth_path = str(Path(directory) / 'auth.db')
        env = {**os.environ, 'SHARED_AUTH_DB': auth_path,
               'OMNICHAT_DB': str(Path(directory) / 'omni.db'), 'ACCESS_LOG_LEVEL': 'WARNING'}
        completed = subprocess.run(
            [str(args.omnichat_python), '-c', OMNI_SCENARIO], cwd=args.omnichat_root,
            env=env, check=True, capture_output=True, text=True, timeout=30)
        result = json.loads(completed.stdout)
        case = SMSRechargeTests()
        case.setUp()
        try:
            with mock.patch.object(sso_bridge, 'SHARED_AUTH_DB', auth_path):
                for error, code in zip(result['errors'], ['request_conflict', 'rate_limited']):
                    case.upstream(error['body'], error['status'])
                    response = case.client.post('/api/sms-lab/recharge/orders', json={
                        'package_usd': 5, 'request_id': 'cross-request-001'})
                    assert response.status_code == error['status'], response.get_json()
                    assert response.get_json() == {'error': error['body']['detail'], 'code': code}
                pages = []
                for page in result['pages']:
                    response = mock.Mock(status_code=200)
                    response.json.return_value = page
                    pages.append(response)
                case.http.side_effect = pages * 2
                for _ in range(2):
                    response = case.client.get('/api/sms-lab/recharge/orders')
                    assert response.status_code == 200, response.get_json()
                    assert response.get_json()['wallet_balance'] == 1.0
                    assert [order['id'] for order in response.get_json()['orders']] == [
                        f'S-cross-{index}' for index in range(99, 89, -1)]
                with sqlite3.connect(case.db_path) as conn:
                    assert conn.execute('SELECT reference FROM sms_wallet_ledger').fetchall() == [
                        ('online_recharge:' + result['paid_order'],)]
            print(json.dumps({'checks': 3, 'result': 'PASS', 'actual_fastapi_statuses': [409, 429],
                              'upstream_page_sizes': [100, 1], 'display_orders': 10,
                              'wallet_units': 10000, 'ledger_rows': 1,
                              'generated_return': result['generated_return']}))
        finally:
            case.doCleanups()


if __name__ == '__main__':
    main()
