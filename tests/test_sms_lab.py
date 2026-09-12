import os
import unittest
from unittest import mock

from flask import Flask

from sms_lab import routes
from sms_lab.client import HeroSMSClient, HeroSMSError


class FakeHeroSMS:
    def __init__(self):
        self.purchases = []
        self.cancelled = []
        self.finished = []
        self.replaced = []

    def get_balance(self):
        return 12.5

    def get_countries(self):
        return [
            {"id": 2, "chn": "哈萨克斯坦", "eng": "Kazakhstan", "visible": 1},
            {"id": 6, "chn": "印度尼西亚", "eng": "Indonesia", "visible": 1},
        ]

    def get_services(self, country=None):
        return [{"code": "zz", "name": "QA Demo"}, {"code": "no", "name": "Not allowed"}]

    def get_offers(self, country=None, service=None):
        country_key = str(country) if country is not None else "2"
        return {"data": {"zz": {country_key: {
            "prices": {"default": 0.25, "retail": 0.25, "min": 0.2},
            "counts": {"total": 5},
        }}}}

    def purchase(self, service, country, max_price):
        self.purchases.append((service, country, max_price))
        return {"data": [{
            "id": 123,
            "status": 4,
            "phone": "77001234567",
            "service": service,
            "country": country,
            "operator": "demo",
            "price": 0.25,
            "otpList": [],
        }]}

    def list_activations(self):
        return {"data": [{
            "id": 123,
            "status": 4,
            "phone": "77001234567",
            "service": "zz",
            "country": 2,
            "operator": "demo",
            "price": 0.25,
            "otpList": [{
                "id": "otp-1",
                "smsCode": "482901",
                "smsText": "Your test code is 482901",
                "receivedAt": "2026-09-13T12:00:00Z",
                "phoneFrom": "TEST",
                "service": "zz",
                "shouldNotLeak": "internal",
            }],
            "providerInternal": "must-not-leak",
        }, {
            "id": 999,
            "phone": "secret-other-session-number",
            "service": "zz",
            "otpList": [],
        }]}

    def cancel(self, activation_id):
        self.cancelled.append(activation_id)

    def finish(self, activation_id):
        self.finished.append(activation_id)

    def replace(self, activation_id):
        self.replaced.append(activation_id)
        return self.purchase("zz", 2, 0.25)


class SMSLabRoutesTests(unittest.TestCase):
    def setUp(self):
        routes._catalog_cache.clear()
        self.fake = FakeHeroSMS()
        self.env = mock.patch.dict(os.environ, {
            "HERO_SMS_API_KEY": "provider-secret-that-must-not-leak",
            "SMS_LAB_ACCESS_TOKEN": "test-access-token-at-least-20-chars",
            "SMS_LAB_ALLOWED_SERVICES": "zz",
            "SMS_LAB_ALLOWED_COUNTRIES": "2",
            "SMS_LAB_MAX_PRICE": "1.00",
            "SMS_LAB_PURCHASES_ENABLED": "1",
            "HERO_SMS_MIN_REQUEST_INTERVAL": "0",
        }, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.client_patch = mock.patch.object(routes, "_client", return_value=self.fake)
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)
        app = Flask(__name__)
        app.secret_key = "sms-lab-tests"
        app.testing = True
        app.register_blueprint(routes.sms_lab_bp)
        self.client = app.test_client()

    def unlock(self):
        return self.client.post('/api/sms-lab/session', json={
            'access_token': 'test-access-token-at-least-20-chars',
        })

    def purchase(self):
        return self.client.post('/api/sms-lab/activations', json={
            'service': 'zz', 'country': 2, 'max_price': 0.3,
        })

    def test_status_never_exposes_provider_or_access_secrets(self):
        response = self.client.get('/api/sms-lab/status')
        text = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('provider-secret', text)
        self.assertNotIn('test-access-token', text)
        self.assertFalse(response.get_json()['unlocked'])

    def test_wrong_access_code_is_rejected_and_routes_remain_locked(self):
        response = self.client.post('/api/sms-lab/session', json={'access_token': 'wrong'})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.client.get('/api/sms-lab/balance').status_code, 401)

    def test_unlock_catalog_and_balance_flow(self):
        self.assertEqual(self.unlock().status_code, 200)
        balance = self.client.get('/api/sms-lab/balance').get_json()
        self.assertEqual(balance, {'balance': 12.5, 'currency': 'USD'})

        countries = self.client.get('/api/sms-lab/countries?service=zz').get_json()['countries']
        self.assertTrue(countries[0]['allowed'])
        self.assertEqual(countries[0]['stock'], 5)
        self.assertEqual(countries[0]['price'], 0.25)
        self.assertFalse(countries[1]['allowed'])

        services = self.client.get('/api/sms-lab/services').get_json()['services']
        by_code = {item['code']: item for item in services}
        self.assertTrue(by_code['zz']['allowed'])
        self.assertFalse(by_code['no']['allowed'])

    def test_country_catalog_requires_service_first(self):
        self.unlock()
        response = self.client.get('/api/sms-lab/countries')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['code'], 'invalid_service')

    def test_purchase_is_single_item_price_capped_and_session_owned(self):
        self.unlock()
        response = self.purchase()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.fake.purchases, [('zz', 2, 0.3)])

        active_response = self.client.get('/api/sms-lab/activations')
        active = active_response.get_json()['activations']
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]['id'], 123)
        self.assertEqual(active[0]['otpList'][0]['smsCode'], '482901')
        text = active_response.get_data(as_text=True)
        self.assertNotIn('secret-other-session-number', text)
        self.assertNotIn('providerInternal', text)
        self.assertNotIn('shouldNotLeak', text)

    def test_unowned_activation_cannot_be_mutated(self):
        self.unlock()
        response = self.client.delete('/api/sms-lab/activations/999')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.fake.cancelled, [])

    def test_finish_removes_activation_from_browser_session(self):
        self.unlock()
        self.purchase()
        response = self.client.post('/api/sms-lab/activations/123/finish')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.fake.finished, [123])
        self.assertEqual(
            self.client.get('/api/sms-lab/activations').get_json()['activations'],
            [],
        )

    def test_lock_keeps_activation_ownership_for_next_unlock(self):
        self.unlock()
        self.purchase()
        self.assertEqual(self.client.delete('/api/sms-lab/session').status_code, 200)
        self.assertEqual(self.client.get('/api/sms-lab/activations').status_code, 401)
        self.unlock()
        active = self.client.get('/api/sms-lab/activations').get_json()['activations']
        self.assertEqual([item['id'] for item in active], [123])

    def test_invalid_nonempty_country_allowlist_denies_every_country(self):
        self.unlock()
        with mock.patch.dict(os.environ, {'SMS_LAB_ALLOWED_COUNTRIES': 'not-a-country'}, clear=False):
            response = self.purchase()
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['code'], 'country_not_allowed')

    def test_purchase_requires_explicit_service_allowlist(self):
        self.unlock()
        with mock.patch.dict(os.environ, {'SMS_LAB_ALLOWED_SERVICES': ''}, clear=False):
            response = self.purchase()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()['code'], 'purchase_disabled')

    def test_wildcard_service_policy_allows_provider_catalog_services(self):
        self.unlock()
        with mock.patch.dict(os.environ, {'SMS_LAB_ALLOWED_SERVICES': '*'}, clear=False):
            status = self.client.get('/api/sms-lab/status').get_json()
            response = self.client.post('/api/sms-lab/activations', json={
                'service': 'no', 'country': 2, 'max_price': 0.3,
            })
        self.assertTrue(status['allow_all_services'])
        self.assertEqual(response.status_code, 201)

    def test_price_above_server_cap_is_rejected_before_provider_call(self):
        self.unlock()
        response = self.client.post('/api/sms-lab/activations', json={
            'service': 'zz', 'country': 2, 'max_price': 5.0,
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.fake.purchases, [])


class HeroSMSClientTests(unittest.TestCase):
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
        self.assertFalse(kwargs['allow_redirects'])

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
