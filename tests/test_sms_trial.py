import sqlite3
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from tests import test_sms_lab as sms_fixture
from sms_lab import routes, init_sms_lab_tables


class SMSFreeTrialTests(unittest.TestCase):
    login = sms_fixture.SMSLabRouteTest.login
    wallet_balance = sms_fixture.SMSLabRouteTest.wallet_balance
    admin_credit = sms_fixture.SMSLabRouteTest.admin_credit

    def setUp(self):
        sms_fixture.SMSLabRouteTest.setUp(self)
        purchase = self.fake.purchase
        def unique_purchase(*args, **kwargs):
            result = purchase(*args, **kwargs)
            result['data'][0]['id'] = 122 + len(self.fake.purchases)
            return result
        self.fake.purchase = unique_purchase

    def trial_purchase(self, key='free_trial_001', country=2, client=None):
        return (client or self.client).post('/api/sms-lab/orders', json={
            'service': 'tg', 'country': country, 'idempotency_key': key,
            'use_free_trial': True,
        })

    def account(self):
        return self.client.get('/api/sms-lab/account').get_json()

    def test_new_sms_user_can_use_trial_without_changing_wallet(self):
        self.login()
        self.assertTrue(self.account()['free_trial']['available'])
        response = self.trial_purchase()
        self.assertEqual(response.status_code, 201, response.get_json())
        self.assertEqual(response.get_json()['order']['sale_price'], 0.3)
        self.assertEqual(response.get_json()['order']['charged_price'], 0)
        self.assertTrue(response.get_json()['order']['is_free_trial'])
        self.assertEqual(self.wallet_balance(), 0)
        self.assertEqual(self.account()['free_trial']['status'], 'reserved')

    def test_trial_checks_marked_up_price_and_never_uses_wallet_for_over_limit(self):
        self.login()
        self.admin_credit(amount=1)
        response = self.trial_purchase(country=6)  # cost .4, retail .6
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()['code'], 'trial_price_limit')
        self.assertEqual(self.wallet_balance(), 10000)
        self.assertEqual(self.fake.purchases, [])
        self.assertTrue(self.account()['free_trial']['available'])

    def test_exact_half_dollar_is_eligible(self):
        self.login()
        with mock.patch.object(routes, '_markup_percent', return_value=25), \
                mock.patch.object(routes, '_offer_for', return_value={'cost_units': 4000, 'stock': 1}):
            response = self.trial_purchase()
        self.assertEqual(response.status_code, 201, response.get_json())
        self.assertEqual(response.get_json()['order']['sale_price'], 0.5)
        self.assertEqual(response.get_json()['order']['charged_price'], 0)

    def test_duplicate_trial_purchase_is_idempotent(self):
        self.login()
        first = self.trial_purchase()
        again = self.trial_purchase()
        self.assertEqual((first.status_code, again.status_code), (201, 200))
        self.assertEqual(len(self.fake.purchases), 1)
        self.assertEqual(again.get_json()['wallet_balance'], 0)

    def test_cancel_restores_trial_without_minting_cash_or_releasing_new_claim(self):
        self.login()
        first = self.trial_purchase()
        self.assertEqual(first.status_code, 201)
        order_id = first.get_json()['order']['id']
        self.assertEqual(self.client.post(f'/api/sms-lab/orders/{order_id}/cancel').status_code, 200)
        self.assertTrue(self.account()['free_trial']['available'])
        self.assertEqual(self.wallet_balance(), 0)
        second = self.trial_purchase('free_trial_002')
        self.assertEqual(second.status_code, 201)
        routes._refund_order(lambda: self.db_path, order_id, 'cancelled', 'replay')
        self.assertEqual(self.account()['free_trial']['status'], 'reserved')
        self.assertEqual(self.wallet_balance(), 0)

    def test_provider_failure_restores_trial_without_cash_credit(self):
        self.login()
        self.fake.fail_purchase = True
        response = self.trial_purchase()
        self.assertEqual(response.status_code, 502)
        self.assertTrue(self.account()['free_trial']['available'])
        self.assertEqual(self.wallet_balance(), 0)

    def test_received_code_consumes_trial_even_if_later_order_is_cancelled(self):
        self.login()
        response = self.trial_purchase()
        self.assertEqual(response.status_code, 201)
        order_id = response.get_json()['order']['id']
        self.client.get('/api/sms-lab/orders')  # fixture receives a code
        self.assertEqual(self.account()['free_trial']['status'], 'used')
        routes._refund_order(lambda: self.db_path, order_id, 'cancelled', 'late refund')
        self.assertFalse(self.account()['free_trial']['available'])
        self.assertEqual(self.trial_purchase('second_success_attempt').status_code, 409)
        self.assertEqual(self.wallet_balance(), 0)

    def test_existing_successful_customer_does_not_get_trial(self):
        self.login()
        self.admin_credit()
        response = self.client.post('/api/sms-lab/orders', json={
            'service': 'tg', 'country': 2, 'idempotency_key': 'old_paid_purchase',
        })
        self.client.post(f'/api/sms-lab/orders/{response.get_json()["order"]["id"]}/finish')
        self.assertFalse(self.account()['free_trial']['available'])
        self.assertEqual(self.trial_purchase().status_code, 409)
        self.assertEqual(self.wallet_balance(), 7000)

    def test_parallel_trial_requests_reserve_only_one_upstream_order(self):
        first_reserved = threading.Event()
        release = threading.Event()
        original = self.fake.purchase
        def provider(*args, **kwargs):
            first_reserved.set()
            self.assertTrue(release.wait(3))
            return original(*args, **kwargs)
        def first():
            client = self.client.application.test_client()
            with client.session_transaction() as s:
                s['user_id'] = 1
                s['username'] = 'alice'
            return self.trial_purchase('parallel_trial_1', client=client)
        self.login()
        with mock.patch.object(self.fake, 'purchase', side_effect=provider), ThreadPoolExecutor(1) as pool:
            pending = pool.submit(first)
            try:
                self.assertTrue(first_reserved.wait(3))
                second = self.trial_purchase('parallel_trial_2')
                self.assertEqual(second.status_code, 409)
            finally:
                release.set()
            self.assertEqual(pending.result().status_code, 201)
        self.assertEqual(len(self.fake.purchases), 1)

    def test_free_orders_do_not_inflate_admin_revenue(self):
        self.login()
        self.assertEqual(self.trial_purchase().status_code, 201)
        summary = self.client.post('/api/sms-lab/admin/summary', json={
            'access_token': 'admin-access-token-at-least-20-chars',
        }).get_json()
        self.assertEqual(summary['revenue'], 0)
        self.assertEqual(summary['provider_cost'], 0.2)
        self.assertEqual(summary['gross_profit'], -0.2)

    def test_migration_preserves_existing_wallet_and_orders_on_repeat(self):
        self.admin_credit()
        self.login()
        order = self.client.post('/api/sms-lab/orders', json={
            'service': 'tg', 'country': 2, 'idempotency_key': 'legacy_paid_purchase',
        }).get_json()['order']
        self.client.post(f'/api/sms-lab/orders/{order["id"]}/finish')
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('DROP TABLE sms_trial_claims')
            conn.execute('ALTER TABLE sms_orders DROP COLUMN trial_discount_units')
            init_sms_lab_tables(conn.cursor())
            init_sms_lab_tables(conn.cursor())
            self.assertEqual(conn.execute('SELECT sale_price_units, trial_discount_units FROM sms_orders').fetchone(), (3000, 0))
        self.assertEqual(self.wallet_balance(), 7000)
        self.assertFalse(self.account()['free_trial']['available'])

    def test_immediate_code_on_replacement_consumes_trial(self):
        self.login()
        order_id = self.trial_purchase().get_json()['order']['id']
        replacement = self.fake.replace(123)
        replacement['data'][0]['otpList'] = [{'smsCode': '123456'}]
        with mock.patch.object(self.fake, 'replace', return_value=replacement):
            self.assertEqual(self.client.post(f'/api/sms-lab/orders/{order_id}/replace').status_code, 200)
        self.assertEqual(self.account()['free_trial']['status'], 'used')
