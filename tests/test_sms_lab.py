import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from flask import Flask

from sms_lab import create_sms_lab_blueprint, init_sms_lab_tables
from sms_lab import routes
from sms_lab.client import HeroSMSClient, HeroSMSError
from sms_lab.storage import amount_to_units, sale_units_for_cost


class FakeHeroSMS:
    def __init__(self):
        self.purchases = []
        self.cancelled = []
        self.finished = []
        self.replaced = []
        self.fail_purchase = False

    def get_services(self, country=None):
        return [
            {"code": "tg", "name": "Telegram"},
            {"code": "dr", "name": "OpenAI"},
            {"code": "md", "name": "Banks"},
            {"code": "zz", "name": "QA Demo"},
        ]

    def get_countries(self):
        return [
            {"id": 2, "chn": "哈萨克斯坦", "eng": "Kazakhstan", "visible": 1},
            {"id": 6, "chn": "印度尼西亚", "eng": "Indonesia", "visible": 1},
        ]

    def get_offers(self, country=None, service=None):
        service = service or "tg"
        return {"data": {service: {
            "2": {
                "prices": {"default": 0.2, "retail": 0.2, "min": 0.18},
                "counts": {"total": 5},
            },
            "6": {
                "prices": {"default": 0.4, "retail": 0.4, "min": 0.35},
                "counts": {"total": 3},
            },
        }}}

    def purchase(self, service, country, max_price, reseller_user_id=None):
        self.purchases.append((service, country, max_price, reseller_user_id))
        if self.fail_purchase:
            raise HeroSMSError("provider failed", 502, "provider_error")
        return {"data": [{
            "id": 123,
            "status": 4,
            "phone": "77001234567",
            "service": service,
            "country": country,
            "operator": "demo",
            "price": 0.2,
            "createdAt": "2026-09-13T12:00:00Z",
            "expiredAt": "2026-09-13T12:20:00Z",
            "otpList": [],
        }]}

    def list_activations(self):
        return {"data": [{
            "id": 123,
            "status": 4,
            "phone": "77001234567",
            "service": "tg",
            "country": 2,
            "operator": "demo",
            "price": 0.2,
            "otpList": [{
                "id": "otp-1",
                "smsCode": "482901",
                "smsText": "Your test code is 482901",
                "receivedAt": "2026-09-13T12:01:00Z",
                "phoneFrom": "TEST",
                "shouldNotLeak": "internal",
            }],
            "providerInternal": "must-not-leak",
        }, {
            "id": 999,
            "phone": "other-users-number",
            "service": "tg",
            "otpList": [],
        }]}

    def cancel(self, activation_id):
        self.cancelled.append(activation_id)

    def finish(self, activation_id):
        self.finished.append(activation_id)

    def replace(self, activation_id):
        self.replaced.append(activation_id)
        return {"data": [{
            "id": 124,
            "status": 4,
            "phone": "77007654321",
            "price": 0.2,
            "otpList": [],
        }]}


class SMSLabRouteTest(unittest.TestCase):
    def setUp(self):
        routes._catalog_cache.clear()
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.db_path = os.path.join(self.tempdir.name, "sms-reseller.db")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE,
                    password_hash TEXT,
                    display_name TEXT
                )
                """
            )
            init_sms_lab_tables(conn.cursor())
            conn.execute(
                "INSERT INTO users (username, display_name) VALUES ('alice', 'Alice')"
            )
            conn.execute(
                "INSERT INTO users (username, display_name) VALUES ('bob', 'Bob')"
            )
            conn.commit()

        self.fake = FakeHeroSMS()
        self.env = mock.patch.dict(os.environ, {
            "HERO_SMS_API_KEY": "provider-secret-that-must-not-leak",
            "SMS_LAB_ACCESS_TOKEN": "admin-access-token-at-least-20-chars",
            "SMS_RESELLER_MARKUP_PERCENT": "50",
            "HERO_SMS_MIN_REQUEST_INTERVAL": "0",
            "SMS_RESELLER_ALLOWED_SERVICES": "",
            "SMS_RESELLER_BLOCKED_SERVICE_CODES": "",
            "SMS_RESELLER_BLOCKED_SERVICE_TERMS": "",
        }, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.client_patch = mock.patch.object(routes, "_client", return_value=self.fake)
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)

        app = Flask(__name__)
        app.secret_key = "sms-reseller-tests"
        app.testing = True
        app.register_blueprint(create_sms_lab_blueprint(lambda: self.db_path))
        self.client = app.test_client()

    def login(self, user_id=1, username="alice"):
        with self.client.session_transaction() as browser_session:
            browser_session["user_id"] = user_id
            browser_session["username"] = username

    def admin_credit(self, username="alice", amount=1.0, reference="credit_test_001"):
        return self.client.post('/api/sms-lab/admin/credit', json={
            'access_token': 'admin-access-token-at-least-20-chars',
            'username': username,
            'amount': amount,
            'reference': reference,
            'note': 'test credit',
        })

    def purchase(self, key="purchase_test_001", service="tg", country=2):
        return self.client.post('/api/sms-lab/orders', json={
            'service': service,
            'country': country,
            'idempotency_key': key,
        })

    def wallet_balance(self, username="alice"):
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute(
                "SELECT sms_wallet_units FROM users WHERE username = ?", (username,)
            ).fetchone()[0]

    def test_public_status_reports_reseller_mode_without_margin_or_secrets(self):
        response = self.client.get('/api/sms-lab/status')
        data = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(data['reseller_mode'])
        self.assertNotIn('markup_percent', data)
        text = response.get_data(as_text=True)
        self.assertNotIn('provider-secret', text)
        self.assertNotIn('admin-access-token', text)

    def test_service_catalog_includes_logo_and_blocks_banking(self):
        response = self.client.get('/api/sms-lab/services')
        services = {item['code']: item for item in response.get_json()['services']}
        self.assertIn('tg', services)
        self.assertNotIn('md', services)
        self.assertEqual(
            services['tg']['logo_url'],
            'https://cdn.hero-sms.com/assets/img/service/tg0.webp',
        )

    def test_openai_service_includes_search_aliases(self):
        services = self.client.get('/api/sms-lab/services').get_json()['services']
        openai = next(item for item in services if item['code'] == 'dr')
        self.assertTrue({'chatgpt', 'gpt', 'open ai'}.issubset(set(openai['aliases'])))

    def test_country_prices_are_marked_up_50_percent_without_cost_leak(self):
        response = self.client.get('/api/sms-lab/countries?service=tg')
        self.assertEqual(response.status_code, 200)
        countries = response.get_json()['countries']
        self.assertEqual(countries[0]['price'], 0.3)
        self.assertEqual(countries[1]['price'], 0.6)
        self.assertNotIn('cost', response.get_data(as_text=True).lower())

    def test_account_requires_login_and_starts_with_zero_wallet(self):
        self.assertFalse(self.client.get('/api/sms-lab/account').get_json()['authenticated'])
        self.login()
        data = self.client.get('/api/sms-lab/account').get_json()
        self.assertTrue(data['authenticated'])
        self.assertEqual(data['wallet']['balance'], 0.0)

    def test_admin_credit_is_token_guarded_and_idempotent(self):
        denied = self.client.post('/api/sms-lab/admin/credit', json={
            'access_token': 'wrong', 'username': 'alice', 'amount': 1,
            'reference': 'credit_test_001',
        })
        self.assertEqual(denied.status_code, 401)
        first = self.admin_credit()
        second = self.admin_credit()
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.get_json()['balance'], 1.0)
        self.assertTrue(second.get_json()['idempotent'])
        self.assertEqual(self.wallet_balance(), 10000)

    def test_purchase_requires_login(self):
        response = self.purchase()
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()['code'], 'login_required')

    def test_successful_purchase_deducts_sale_price_and_sets_reseller_user(self):
        self.admin_credit()
        self.login()
        response = self.purchase()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()['order']['sale_price'], 0.3)
        self.assertEqual(response.get_json()['wallet_balance'], 0.7)
        self.assertEqual(self.fake.purchases, [('tg', 2, 0.2, '1')])
        self.assertEqual(self.wallet_balance(), 7000)

    def test_purchase_idempotency_prevents_double_charge_and_provider_call(self):
        self.admin_credit()
        self.login()
        first = self.purchase()
        second = self.purchase()
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.get_json()['idempotent'])
        self.assertEqual(len(self.fake.purchases), 1)
        self.assertEqual(self.wallet_balance(), 7000)

    def test_insufficient_wallet_never_calls_provider(self):
        self.login()
        response = self.purchase()
        self.assertEqual(response.status_code, 402)
        self.assertEqual(response.get_json()['code'], 'insufficient_wallet_balance')
        self.assertEqual(self.fake.purchases, [])

    def test_provider_failure_refunds_wallet_and_marks_order_failed(self):
        self.admin_credit()
        self.login()
        self.fake.fail_purchase = True
        response = self.purchase()
        self.assertEqual(response.status_code, 502)
        self.assertEqual(self.wallet_balance(), 10000)
        with sqlite3.connect(self.db_path) as conn:
            status, refunded = conn.execute(
                "SELECT status, refunded_units FROM sms_orders"
            ).fetchone()
        self.assertEqual(status, 'failed')
        self.assertEqual(refunded, 3000)

    def test_orders_are_isolated_and_provider_private_fields_are_removed(self):
        self.admin_credit()
        self.login()
        self.purchase()
        orders = self.client.get('/api/sms-lab/orders')
        text = orders.get_data(as_text=True)
        self.assertEqual(orders.get_json()['orders'][0]['otpList'][0]['smsCode'], '482901')
        self.assertNotIn('providerInternal', text)
        self.assertNotIn('shouldNotLeak', text)
        self.login(2, 'bob')
        self.assertEqual(self.client.get('/api/sms-lab/orders').get_json()['orders'], [])

    def test_cancel_refunds_customer_sale_price_only_once(self):
        self.admin_credit()
        self.login()
        order_id = self.purchase().get_json()['order']['id']
        response = self.client.post(f'/api/sms-lab/orders/{order_id}/cancel')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.wallet_balance(), 10000)
        again = self.client.post(f'/api/sms-lab/orders/{order_id}/cancel')
        self.assertEqual(again.status_code, 409)
        self.assertEqual(self.wallet_balance(), 10000)
        self.assertEqual(self.fake.cancelled, [123])

    def test_admin_summary_reports_revenue_cost_and_profit(self):
        self.admin_credit()
        self.login()
        self.purchase()
        response = self.client.post('/api/sms-lab/admin/summary', json={
            'access_token': 'admin-access-token-at-least-20-chars',
        })
        data = response.get_json()
        self.assertEqual(data['revenue'], 0.3)
        self.assertEqual(data['provider_cost'], 0.2)
        self.assertEqual(data['gross_profit'], 0.1)


class SMSMoneyTests(unittest.TestCase):
    def test_money_uses_integer_units_and_rounds_markup_up(self):
        self.assertEqual(amount_to_units('0.078'), 780)
        self.assertEqual(sale_units_for_cost(1, 50), 2)


def recharge_order(**changes):
    order = {
        'id': 'S-paid-1', 'app': 'sms_market', 'amount_fen': 3500,
        'wallet_units': 50000, 'wallet_amount': '5.0000', 'status': 'credited',
        'channel': 'xorpay', 'created_at': '2026-09-13T12:00:00Z',
        'finished_at': '2026-09-13T12:01:00Z', 'request_id': 'recharge_test_001',
    }
    order.update(changes)
    return order


class SMSRechargeTests(unittest.TestCase):
    login = SMSLabRouteTest.login
    wallet_balance = SMSLabRouteTest.wallet_balance

    def setUp(self):
        SMSLabRouteTest.setUp(self)
        self.login()
        self.client.set_cookie('sso_token', 'shared-secret-never-echo')
        self.http_patch = mock.patch('requests.Session.request')
        self.http = self.http_patch.start()
        self.addCleanup(self.http_patch.stop)

    def upstream(self, payload, status=200):
        response = mock.Mock(status_code=status)
        response.json.return_value = payload
        self.http.return_value = response

    def settle(self, order, user_id=1):
        from sms_lab.storage import settle_paid_sms_recharge
        with sqlite3.connect(self.db_path) as conn:
            return settle_paid_sms_recharge(conn, user_id, order)

    def test_paid_recharge_settles_wallet_exactly_once(self):
        self.assertEqual(self.settle(recharge_order()), (True, 50000))
        self.assertEqual(self.settle(recharge_order()), (False, 50000))
        self.assertEqual(self.wallet_balance(), 50000)
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                'SELECT kind, reference, amount_units FROM sms_wallet_ledger'
            ).fetchall()
        self.assertEqual(rows, [('online_recharge', 'online_recharge:S-paid-1', 50000)])

    def test_settlement_replay_returns_current_wallet_after_spend(self):
        self.settle(recharge_order())
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('UPDATE users SET sms_wallet_units = 47000 WHERE id = 1')
        self.assertEqual(self.settle(recharge_order()), (False, 47000))

    def test_concurrent_paid_order_replays_credit_exactly_once(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        start = Barrier(4)

        def settle_at_once():
            start.wait(timeout=5)
            return self.settle(recharge_order())

        with ThreadPoolExecutor(max_workers=4) as workers:
            results = list(workers.map(lambda _: settle_at_once(), range(4)))
        self.assertEqual(sum(applied for applied, _ in results), 1)
        self.assertEqual([balance for _, balance in results], [50000] * 4)
        self.assertEqual(self.wallet_balance(), 50000)

    def test_settlement_rejects_order_claimed_by_another_local_user(self):
        from sms_lab.recharge import OmniRechargeError
        self.settle(recharge_order())
        with self.assertRaises(OmniRechargeError) as raised:
            self.settle(recharge_order(), user_id=2)
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(self.wallet_balance(), 50000)
        self.assertEqual(self.wallet_balance('bob'), 0)

    def test_settlement_rejects_invalid_money_order_and_app_without_writes(self):
        from sms_lab.recharge import OmniRechargeError
        for changes in (
            {'app': 'omnichat'}, {'status': 'pending'}, {'id': '../wrong'},
            {'id': ''}, {'id': 'S\n'}, {'wallet_units': True},
            {'wallet_units': '50000'}, {'wallet_units': 1.5},
            {'wallet_units': 0}, {'wallet_units': -1}, {'wallet_units': 100001},
        ):
            with self.subTest(changes=changes), self.assertRaises(OmniRechargeError):
                self.settle(recharge_order(**changes))
        self.assertEqual(self.wallet_balance(), 0)

    def test_settlement_rolls_back_balance_if_ledger_insert_fails(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("CREATE TRIGGER fail_ledger BEFORE INSERT ON sms_wallet_ledger "
                         "BEGIN SELECT RAISE(ABORT, 'ledger unavailable'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.settle(recharge_order())
        self.assertEqual(self.wallet_balance(), 0)

    def test_recharge_requires_local_login(self):
        with self.client.session_transaction() as browser_session:
            browser_session.clear()
        response = self.client.get('/api/sms-lab/recharge/orders')
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()['code'], 'login_required')
        self.http.assert_not_called()

    def test_recharge_requires_cookie_even_when_body_contains_token(self):
        self.client.delete_cookie('sso_token')
        response = self.client.post('/api/sms-lab/recharge/orders', json={
            'package_usd': 5, 'request_id': 'recharge_test_001',
            'sso_token': 'body-secret',
        })
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()['code'], 'shared_login_required')
        self.http.assert_not_called()

    def test_recharge_create_forwards_only_fixed_package_and_request_id(self):
        self.upstream({'order': recharge_order(status='pending', finished_at=None),
                       'pay_url': '/api/recharge/sms-market/orders/S-paid-1/checkout', 'reused': False}, 201)
        response = self.client.post('/api/sms-lab/recharge/orders', json={
            'package_usd': 5, 'request_id': 'recharge_test_001',
            'user_id': 2, 'username': 'bob', 'sso_token': 'body-secret',
            'return_url': 'https://evil.example/',
        })
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()['checkout_url'], 'https://chat.bnbscheduler.top/api/recharge/sms-market/orders/S-paid-1/checkout')
        self.assertEqual(self.http.call_args.args[:2], ('POST', 'https://chat.bnbscheduler.top/api/recharge/sms-market/orders'))
        sent = self.http.call_args.kwargs
        self.assertEqual(sent['json'], {'package_usd': 5, 'request_id': 'recharge_test_001'})
        self.assertEqual(sent['headers']['Authorization'], 'Bearer shared-secret-never-echo')
        self.assertEqual(sent['timeout'], 10)
        self.assertFalse(sent['allow_redirects'])
        self.assertEqual(self.wallet_balance(), 0)

    def test_recharge_create_rejects_invalid_package_and_request_id(self):
        for package, request_id in ((2, 'recharge_test_001'), (True, 'recharge_test_001'),
                                    (5, '../wrong'), (5, ''), (5, None)):
            with self.subTest(package=package, request_id=request_id):
                response = self.client.post('/api/sms-lab/recharge/orders', json={
                    'package_usd': package, 'request_id': request_id,
                })
                self.assertEqual(response.status_code, 400)
        self.http.assert_not_called()

    def test_recharge_list_and_detail_settle_paid_orders_and_replay(self):
        self.upstream({'orders': [recharge_order(), recharge_order(id='S-pending-2', status='pending', finished_at=None)]})
        first = self.client.get('/api/sms-lab/recharge/orders?user_id=2&username=bob')
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.get_json()['wallet_balance'], 5.0)
        self.assertEqual(self.http.call_args.args[:2], ('GET', 'https://chat.bnbscheduler.top/api/recharge/sms-market/orders'))
        self.assertNotIn('params', self.http.call_args.kwargs)
        self.upstream({'order': recharge_order()})
        second = self.client.get('/api/sms-lab/recharge/orders/S-paid-1')
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.get_json()['wallet_balance'], 5.0)
        self.assertEqual(self.wallet_balance(), 50000)
        self.assertEqual(self.wallet_balance('bob'), 0)

    def test_recharge_detail_cannot_read_another_upstream_users_order(self):
        self.upstream({'error': '订单不存在'}, 404)
        response = self.client.get('/api/sms-lab/recharge/orders/S-other-user')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()['code'], 'order_not_found')
        self.assertEqual(self.http.call_args.args[:2], ('GET', 'https://chat.bnbscheduler.top/api/recharge/sms-market/orders/S-other-user'))
        self.assertEqual(self.http.call_args.kwargs['headers']['Authorization'], 'Bearer shared-secret-never-echo')
        self.assertEqual(self.wallet_balance(), 0)

    def test_recharge_detail_conflicts_on_other_local_users_settled_order(self):
        self.settle(recharge_order(), user_id=2)
        self.upstream({'order': recharge_order()})
        response = self.client.get('/api/sms-lab/recharge/orders/S-paid-1')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.wallet_balance(), 0)
        self.assertEqual(self.wallet_balance('bob'), 50000)

    def test_recharge_list_rolls_back_all_new_orders_on_later_user_conflict(self):
        self.settle(recharge_order(id='S-bob'), user_id=2)
        self.upstream({'orders': [recharge_order(id='S-alice-new'), recharge_order(id='S-bob')]})
        response = self.client.get('/api/sms-lab/recharge/orders')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()['code'], 'recharge_order_conflict')
        self.assertEqual(self.wallet_balance(), 0)
        self.assertEqual(self.wallet_balance('bob'), 50000)
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute('SELECT user_id, reference FROM sms_wallet_ledger').fetchall()
        self.assertEqual(rows, [(2, 'online_recharge:S-bob')])

    def test_recharge_list_rolls_back_earlier_orders_on_later_ledger_failure(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("CREATE TRIGGER fail_second_ledger BEFORE INSERT ON sms_wallet_ledger "
                         "WHEN NEW.reference = 'online_recharge:S-second' "
                         "BEGIN SELECT RAISE(ABORT, 'ledger unavailable'); END")
        self.upstream({'orders': [recharge_order(id='S-first'), recharge_order(id='S-second')]})
        with self.assertRaises(sqlite3.IntegrityError):
            self.client.get('/api/sms-lab/recharge/orders')
        self.assertEqual(self.wallet_balance(), 0)
        with sqlite3.connect(self.db_path) as conn:
            count = conn.execute('SELECT COUNT(*) FROM sms_wallet_ledger').fetchone()[0]
        self.assertEqual(count, 0)

    def test_recharge_list_settles_distinct_orders_and_deduplicates_within_batch(self):
        self.upstream({'orders': [recharge_order(id='S-first'), recharge_order(id='S-first'),
                                 recharge_order(id='S-second')]})
        first = self.client.get('/api/sms-lab/recharge/orders')
        replay = self.client.get('/api/sms-lab/recharge/orders')
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.get_json()['wallet_balance'], 10.0)
        self.assertEqual(replay.get_json()['wallet_balance'], 10.0)
        self.assertEqual(self.wallet_balance(), 100000)
        with sqlite3.connect(self.db_path) as conn:
            balances = conn.execute(
                'SELECT balance_after_units FROM sms_wallet_ledger ORDER BY id'
            ).fetchall()
        self.assertEqual(balances, [(50000,), (100000,)])

    def test_upstream_timeout_and_malformed_orders_fail_without_secret_or_credit(self):
        import requests
        self.http.side_effect = requests.Timeout('shared-secret-never-echo')
        response = self.client.get('/api/sms-lab/recharge/orders')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()['code'], 'recharge_service_unavailable')
        self.assertNotIn('shared-secret', response.get_data(as_text=True))
        self.http.side_effect = None
        for payload in ([], {'orders': 'wrong'}, {'orders': [{'id': 'S-incomplete'}]},
                        {'orders': [recharge_order(), recharge_order(id='S-bad', app='omnichat')]},
                        {'orders': [recharge_order(wallet_amount='1.0000')]},
                        {'orders': [recharge_order(wallet_units=True)]}):
            self.upstream(payload)
            response = self.client.get('/api/sms-lab/recharge/orders')
            self.assertEqual(response.status_code, 503)
        self.http.return_value.json.side_effect = ValueError('shared-secret-never-echo')
        response = self.client.get('/api/sms-lab/recharge/orders')
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('shared-secret', response.get_data(as_text=True))
        self.assertEqual(self.wallet_balance(), 0)

    def test_upstream_auth_conflict_and_rate_limit_errors_are_safe(self):
        for status, message, code in (
            (401, 'expired', 'shared_login_required'),
            (409, '订单请求冲突', 'request_conflict'),
            (429, '请求过于频繁', 'rate_limited'),
        ):
            self.upstream({'error': message, 'code': code}, status)
            response = self.client.get('/api/sms-lab/recharge/orders')
            self.assertEqual(response.status_code, status)
            self.assertEqual(response.get_json()['code'], code)
            if status != 401:
                self.assertEqual(response.get_json()['error'], message)

    def test_recharge_rejects_external_checkout_and_mismatched_detail_id(self):
        self.upstream({'order': recharge_order(status='pending'),
                       'pay_url': 'https://evil.example/', 'reused': False}, 201)
        response = self.client.post('/api/sms-lab/recharge/orders', json={
            'package_usd': 5, 'request_id': 'recharge_test_001',
        })
        self.assertEqual(response.status_code, 503)
        self.upstream({'order': recharge_order(id='S-wrong')})
        response = self.client.get('/api/sms-lab/recharge/orders/S-paid-1')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.wallet_balance(), 0)

    def test_recharge_config_uses_configured_base_and_bearer_cookie(self):
        payload = {'usd_cny': '6.80', 'packages': [
            {'usd_units': 1, 'usd': '1.00', 'fen': 680},
            {'usd_units': 5, 'usd': '5.00', 'fen': 3400},
            {'usd_units': 10, 'usd': '10.00', 'fen': 6800},
        ]}
        self.upstream(payload)
        with mock.patch.dict(os.environ, {'OMNICHAT_RECHARGE_API_BASE': 'https://chat.bnbscheduler.top:443'}):
            response = self.client.get('/api/sms-lab/recharge/config')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), payload)
        self.assertEqual(self.http.call_args.args[:2], ('GET', 'https://chat.bnbscheduler.top:443/api/recharge/sms-market/config'))
        self.assertEqual(self.http.call_args.kwargs['headers']['Authorization'], 'Bearer shared-secret-never-echo')

    def test_recharge_rejects_untrusted_hosts_and_ports_before_http(self):
        self.upstream({'orders': [recharge_order()]})
        for base in ('https://evil.test', 'https://chat.bnbscheduler.top:444',
                     'https://chat.bnbscheduler.top.evil.test', 'http://chat.bnbscheduler.top'):
            with self.subTest(base=base), mock.patch.dict(os.environ, {'OMNICHAT_RECHARGE_API_BASE': base}):
                response = self.client.get('/api/sms-lab/recharge/orders')
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.get_json()['code'], 'recharge_service_unavailable')
                self.http.assert_not_called()
                self.assertEqual(self.wallet_balance(), 0)


class OmniRechargeClientTests(unittest.TestCase):
    def test_fake_upstream_host_requires_explicit_injected_allowlist(self):
        from sms_lab.recharge import OmniRechargeClient, OmniRechargeError
        response = mock.Mock(status_code=200)
        response.json.return_value = {'orders': []}
        with mock.patch('requests.Session.request', return_value=response) as http:
            with self.assertRaises(OmniRechargeError):
                OmniRechargeClient('https://payments.example.test', 'shared-secret')
            http.assert_not_called()
            client = OmniRechargeClient('https://payments.example.test', 'shared-secret',
                                        allowed_hosts=('payments.example.test',))
            self.assertEqual(client.list_orders(), {'orders': []})
            self.assertEqual(http.call_args.args[:2], ('GET', 'https://payments.example.test/api/recharge/sms-market/orders'))

    def test_client_requires_https_outside_tests(self):
        from sms_lab.recharge import OmniRechargeClient, OmniRechargeError
        for base in ('http://localhost:3000', 'file:///tmp/recharge',
                     'https://user:password@example.com', 'https://example.com/?token=x'):
            with self.subTest(base=base), self.assertRaises(OmniRechargeError):
                OmniRechargeClient(base, 'shared-secret')


class SMSMarketFrontendTests(unittest.TestCase):
    def test_auth_dialog_offers_maxcourse_and_ispace_login(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, 'sms-lab', 'index.html'), encoding='utf-8') as file:
            html = file.read()
        with open(os.path.join(root, 'sms-lab', 'reseller.js'), encoding='utf-8') as file:
            script = file.read()

        self.assertIn('id="auth-local-tab"', html)
        self.assertIn('id="auth-ispace-tab"', html)
        self.assertIn('MAXCOURSE 账号', html)
        self.assertIn('iSpace 登录', html)
        self.assertIn('id="auth-provider-hint"', html)
        self.assertIn("'/api/login/ispace'", script)
        self.assertIn("authProvider: 'local'", script)
        self.assertIn('iSpace 登录成功，DDL 已同步', script)


class HeroSMSClientTests(unittest.TestCase):
    def test_country_catalog_accepts_current_keyed_object_shape(self):
        response = mock.Mock(status_code=200)
        response.json.return_value = {
            '2': {'chn': '哈萨克斯坦', 'eng': 'Kazakhstan', 'visible': 1},
            '6': {'id': 6, 'chn': '印度尼西亚', 'eng': 'Indonesia', 'visible': 1},
        }
        request_session = mock.Mock()
        request_session.request.return_value = response
        client = HeroSMSClient('server-only-secret', session=request_session)
        with mock.patch.dict(os.environ, {'HERO_SMS_MIN_REQUEST_INTERVAL': '0'}, clear=False):
            countries = client.get_countries()
        self.assertEqual(countries[0]['id'], '2')
        self.assertEqual(countries[1]['id'], 6)

    def test_purchase_sends_reseller_user_id_in_server_side_body(self):
        response = mock.Mock(status_code=200)
        response.json.return_value = {'data': []}
        request_session = mock.Mock()
        request_session.request.return_value = response
        client = HeroSMSClient('server-only-secret', session=request_session)
        with mock.patch.dict(os.environ, {'HERO_SMS_MIN_REQUEST_INTERVAL': '0'}, clear=False):
            client.purchase('tg', 2, 0.2, reseller_user_id='42')
        payload = request_session.request.call_args.kwargs['json']
        self.assertEqual(payload['resellerUserId'], '42')
        self.assertEqual(payload['amount'], 1)

    def test_modern_api_uses_authorization_header_not_query_key(self):
        response = mock.Mock(status_code=200)
        response.json.return_value = {'data': []}
        request_session = mock.Mock()
        request_session.request.return_value = response
        client = HeroSMSClient('server-only-secret', session=request_session)
        with mock.patch.dict(os.environ, {'HERO_SMS_MIN_REQUEST_INTERVAL': '0'}, clear=False):
            client.list_activations()
        kwargs = request_session.request.call_args.kwargs
        self.assertEqual(kwargs['headers']['Authorization'], 'ApiKey server-only-secret')
        self.assertNotIn('server-only-secret', request_session.request.call_args.args[1])
        self.assertIsNone(kwargs['params'])

    def test_network_error_does_not_echo_secret_url(self):
        request_session = mock.Mock()
        request_session.request.side_effect = __import__('requests').RequestException(
            'https://hero-sms.com/?api_key=server-only-secret'
        )
        client = HeroSMSClient('server-only-secret', session=request_session)
        with mock.patch.dict(os.environ, {'HERO_SMS_MIN_REQUEST_INTERVAL': '0'}, clear=False):
            with self.assertRaises(HeroSMSError) as raised:
                client.get_balance()
        self.assertNotIn('server-only-secret', str(raised.exception))


if __name__ == '__main__':
    unittest.main()
