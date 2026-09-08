import time
import unittest
from unittest import mock

import app as module
from human_verification import CLIENT_COOKIE, CLEARANCE_COOKIE, CLEARANCE_SECONDS


class HumanVerificationTests(unittest.TestCase):
    UA = 'python-requests/2.32'

    def setUp(self):
        self.client = module.app.test_client()
        self.human = module.human_verification
        module._rate_counters.clear()
        self.human._buckets.clear()
        self.headers = {'User-Agent': self.UA, 'Origin': 'http://localhost'}

    def get(self, path, **kwargs):
        return self.client.get(path, headers=self.headers, **kwargs)

    def post(self, path, **kwargs):
        return self.client.post(path, headers=self.headers, **kwargs)

    def establish(self):
        response = self.get('/api/semesters')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json['code'], 'human_verification_required')
        self.assertEqual(response.headers['X-Maxcourse-Challenge'], 'required')
        with mock.patch.object(self.human, 'bridge', return_value={'challenge': {}, 'token': 't', 'expires': 123}):
            self.assertEqual(self.post('/api/human/challenge').status_code, 200)

    def grant(self):
        self.establish()
        with mock.patch.object(self.human, 'bridge', return_value={'success': True, 'token': 'verified-by-cap'}):
            response = self.post('/api/human/redeem', json={'token': 'challenge-token', 'solutions': [1]})
        self.assertEqual(response.status_code, 200)
        self.assertIn('HttpOnly', response.headers['Set-Cookie'])
        return response

    def test_successful_redemption_resumes_suspicious_client_and_keeps_auth(self):
        self.grant()
        self.assertEqual(self.get('/api/semesters').status_code, 200)
        self.assertTrue(self.get('/api/human/status').json['verified'])
        self.assertEqual(self.get('/api/user/notifications').status_code, 401)
        for path in ('/maxcourse.db', '/app.py', '/cap_service/server.mjs', '/cap_service/package-lock.json'):
            self.assertEqual(self.get(path).status_code, 404)

    def test_clearance_is_bound_to_browser_identity_ip_and_user_agent(self):
        self.grant()
        self.assertEqual(self.get('/api/semesters', environ_overrides={'REMOTE_ADDR': '203.0.113.12'}).status_code, 403)
        other_ua = self.client.get('/api/semesters', headers={'User-Agent': 'httpx/1.0'})
        self.assertEqual(other_ua.status_code, 403)
        other = module.app.test_client()
        other.set_cookie(CLEARANCE_COOKIE, self.client.get_cookie(CLEARANCE_COOKIE).value)
        self.assertEqual(other.get('/api/semesters', headers=self.headers).status_code, 403)

    def test_expired_or_forged_clearance_does_not_bypass(self):
        self.grant()
        with mock.patch('time.time', return_value=time.time() + CLEARANCE_SECONDS + 5):
            self.assertEqual(self.get('/api/semesters').status_code, 403)
        self.client.set_cookie(CLEARANCE_COOKIE, 'forged')
        self.assertFalse(self.get('/api/human/status').json['verified'])

    def test_failed_or_unavailable_verifier_never_grants_clearance(self):
        self.establish()
        proof = {'token': 'invalid', 'solutions': [0]}
        with mock.patch.object(self.human, 'bridge', return_value={'success': False}):
            self.assertEqual(self.post('/api/human/redeem', json=proof).status_code, 400)
        with mock.patch.object(self.human, 'bridge', side_effect=RuntimeError):
            self.assertEqual(self.post('/api/human/redeem', json=proof).status_code, 503)
        self.assertIsNone(self.client.get_cookie(CLEARANCE_COOKIE))
        self.assertEqual(self.get('/api/semesters').status_code, 403)

    def test_widget_empty_post_challenge_and_json_redemption_contract(self):
        with mock.patch.object(self.human, 'bridge', return_value={'challenge': {'c': 50}, 'token': 'signed', 'expires': 123}) as bridge:
            response = self.post('/api/human/challenge')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(bridge.call_args.args[1]['scope']), 64)
        self.assertEqual(self.post('/api/human/redeem', data='token=forged').status_code, 400)

    def test_cross_site_challenge_and_redemption_are_rejected_before_verifier(self):
        with mock.patch.object(self.human, 'bridge') as bridge:
            for path in ('/api/human/challenge', '/api/human/redeem'):
                response = self.client.post(path, json={}, headers={'Origin': 'https://attacker.example', 'User-Agent': self.UA})
                self.assertEqual(response.status_code, 403)
            bridge.assert_not_called()

    def test_proof_payload_limits_and_invalid_numbers(self):
        self.establish()
        with mock.patch.object(self.human, 'bridge') as bridge:
            for proof in ({'token': 'x', 'solutions': [True]}, {'token': 'x', 'solutions': [-1]},
                          {'token': 'x', 'solutions': [1.5]}, {'token': 'x' * 9000, 'solutions': [0]},
                          {'token': 'x', 'solutions': [0] * 101}):
                self.assertEqual(self.post('/api/human/redeem', json=proof).status_code, 400)
            bridge.assert_not_called()

    def test_rate_challenge_remains_accessible_and_does_not_reset_shared_ip(self):
        self.headers['User-Agent'] = 'Mozilla/5.0'
        with mock.patch.object(module, 'RATE_LIMIT_VISITOR_PER_MIN', 1), mock.patch.object(module, 'RATE_LIMIT_IP_PER_MIN', 1000):
            self.assertEqual(self.get('/api/semesters').status_code, 200)
            limited = self.get('/api/semesters')
            self.assertEqual(limited.status_code, 429)
            self.assertEqual(limited.json['code'], 'human_verification_required')
            self.assertEqual(self.get('/api/human/status').status_code, 200)
            with mock.patch.object(self.human, 'bridge', return_value={'challenge': {}, 'token': 't', 'expires': 123}):
                self.assertEqual(self.post('/api/human/challenge').status_code, 200)
            before = module._rate_counters['ip:127.0.0.1'][0]
            with mock.patch.object(self.human, 'bridge', return_value={'success': True, 'token': 'valid'}):
                self.assertEqual(self.post('/api/human/redeem', json={'token': 't', 'solutions': [1]}).status_code, 200)
            self.assertEqual(module._rate_counters['ip:127.0.0.1'][0], before)
            self.assertEqual(self.get('/api/semesters').status_code, 200)
            again = self.get('/api/semesters')
            self.assertEqual(again.status_code, 429)
            self.assertNotIn('X-Maxcourse-Challenge', again.headers)

    def test_challenge_minting_has_its_own_limit(self):
        with mock.patch.object(self.human, 'bridge', return_value={'challenge': {}, 'token': 't', 'expires': 123}):
            responses = [self.post('/api/human/challenge') for _ in range(11)]
        self.assertEqual(responses[-1].status_code, 429)
        self.assertNotIn('X-Maxcourse-Challenge', responses[-1].headers)

    def test_verification_quotas_allow_different_browsers_on_a_shared_campus_ip(self):
        other = module.app.test_client()
        with mock.patch.object(self.human, 'bridge', return_value={'challenge': {}, 'token': 't', 'expires': 123}):
            for _ in range(10):
                self.assertEqual(self.post('/api/human/challenge').status_code, 200)
            self.assertEqual(self.post('/api/human/challenge').status_code, 429)
            self.assertEqual(other.post('/api/human/challenge', headers=self.headers).status_code, 200)

    def test_parallel_api_session_responses_cannot_overwrite_clearance_identity(self):
        self.grant()
        identity = self.client.get_cookie(CLIENT_COOKIE).value
        # A delayed response from another initial API request may replace the
        # ordinary Flask session. It must not invalidate the Cap browser cookie.
        self.client.delete_cookie('session')
        self.assertTrue(self.get('/api/human/status').json['verified'])
        self.assertEqual(self.client.get_cookie(CLIENT_COOKIE).value, identity)
        self.assertEqual(self.get('/api/semesters').status_code, 200)

    def test_navigation_challenge_and_shared_html_bootstrap(self):
        response = self.client.get('/api/semesters', headers={'User-Agent': self.UA, 'Accept': 'text/html'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.mimetype, 'text/html')
        self.assertIn(b'MAXCOURSE_VERIFY_NEXT="/api/semesters"', response.data)
        frame = self.get('/api/media-dl/proxy?feedback=12345678-1234-1234-1234-123456789012')
        self.assertEqual(frame.mimetype, 'text/html')
        self.assertIn(b'MAXCOURSE_VERIFY_NEXT', frame.data)
        for path in ('/', '/media-dl/index.html', '/stats/index.html', '/todolist.html'):
            response = self.get(path, environ_overrides={'HTTP_IF_MODIFIED_SINCE': 'Wed, 31 Dec 2099 23:59:59 GMT'})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data.count(b'/human-check/client.js'), 1)

    def test_verified_download_rate_error_reaches_the_parent_tool(self):
        self.grant()
        with mock.patch.object(module, 'RATE_LIMIT_IP_PER_MIN', 1):
            self.assertEqual(self.get('/api/semesters').status_code, 200)
            response = self.get('/api/media-dl/proxy?feedback=12345678-1234-1234-1234-123456789012')
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.mimetype, 'text/html')
        self.assertIn(b'media-dl-error', response.data)
        self.assertNotIn('X-Maxcourse-Challenge', response.headers)

    def test_return_url_cannot_redirect_to_an_external_site_or_inject_markup(self):
        for target in ('//attacker.test/', '/\\attacker.test/', 'https://attacker.test'):
            response = self.get('/human-check/', query_string={'next': target})
            self.assertIn(b'MAXCOURSE_VERIFY_NEXT="/"', response.data)
        response = self.get('/human-check/', query_string={'next': '/?x=</script><script>alert(1)</script>'})
        self.assertNotIn(b'</script><script>alert(1)', response.data)


if __name__ == '__main__':
    unittest.main()
