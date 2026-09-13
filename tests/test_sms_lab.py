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


class SMSResellerRoutesTests(unittest.TestCase):
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
