import os
import sqlite3
import tempfile
import unittest
from unittest import mock

os.environ.setdefault('MAXCOURSE_SECRET_KEY', 'test-secret-key')
import app as app_module
import mail_digest
from mail_digest.client import MailError, SchoolMailbox, parse_inbox, extract_preview
from mail_digest.summary import summarize

LIST = '''<body id="list"><div>在"收件箱"中有 2 封 未读邮件</div>
<table><tr><td><input type="checkbox" name="mailid" value="M~1" unread="true" fn="Registry" fa="registry@example.edu" totime="1790204400000"></td>
<td class="gt"><u>Course registration</u><b>Register by Friday</b></td></tr></table>
<table><tr><td><input type="checkbox" name="mailid" value="M-2" unread="true" fn="Library" fa="library@example.edu" totime="1790204300000"></td>
<td class="gt"><u>Holiday hours</u></td></tr></table><a id="nextpage" href="?page=1">下一页</a></body>'''


class MailParserTest(unittest.TestCase):
    def test_unread_only_and_metadata(self):
        data = parse_inbox(LIST.replace('value="M-2" unread="true"', 'value="M-2" unread="false"'))
        self.assertEqual(data['total_unread'], 2)
        self.assertEqual([m['id'] for m in data['messages']], ['M~1'])
        self.assertTrue(data['has_next'])
        self.assertEqual(data['messages'][0]['subject'], 'Course registration')

    def test_login_and_changed_layout_are_not_empty_inbox(self):
        for html in ['<input type=password>', '<body>changed</body>', '<body id=list>unknown</body>']:
            with self.assertRaises(MailError):
                parse_inbox(html)

    def test_empty_inbox(self):
        self.assertEqual(parse_inbox('<body id="list">在收件箱中有 0 封 未读邮件</body>')['messages'], [])

    def test_preview_removes_scripts_and_does_not_load_images(self):
        body = extract_preview('<div>Deadline Friday</div><script>STEAL()</script><img src="https://tracker.invalid"><iframe>hidden</iframe>')
        self.assertEqual(body['body'], 'Deadline Friday')
        self.assertTrue(body['has_images'])
        self.assertNotIn('STEAL', body['body'])
        self.assertTrue(extract_preview('x' * 9000)['body_truncated'])

    def test_preview_uses_non_marking_endpoint(self):
        client = SchoolMailbox()
        client.sid = 'secret-session'
        with mock.patch.object(client, '_request', return_value=('<p>Text</p>', '')) as req:
            client.preview('M-1')
        self.assertEqual(req.call_args.kwargs['params']['mode'], 'preview')
        self.assertEqual(req.call_args.kwargs['params']['t'], 'quickreadmail')
        self.assertEqual(req.call_args.kwargs['params']['folderid'], 1)
        client.close()

    def test_untrusted_redirect_destination_rejected_before_http(self):
        client = SchoolMailbox()
        with mock.patch.object(client.http, 'request') as req:
            for url in ['http://mis.bnbu.edu.cn/', 'https://evil.test/', 'https://exmail.qq.com@evil.test/']:
                with self.assertRaises(MailError): client._request('GET', url)
            req.assert_not_called()
        client.close()

    def test_summary_requires_exact_source_coverage(self):
        import json
        good = {'overview': 'Two notices', 'items': [{'id': 'M-1', 'summary': 'Register', 'priority': 'action', 'action': 'Register', 'deadline': 'Friday'}]}
        payload = {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(good)}}], 'usage': {'credits': 1}}
        with mock.patch('mail_digest.summary.omni_request', return_value=payload):
            self.assertEqual(summarize('token', [{'id':'M-1'}])['credits'], 1)
            with self.assertRaises(MailError): summarize('token', [{'id':'another-id'}])


class MailRoutesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = app_module.DB_PATH
        app_module.DB_PATH = os.path.join(self.tmp.name, 'test.db')
        app_module.init_db()
        self.old_testing = app_module.app.testing
        app_module.app.testing = True
        self.client = app_module.app.test_client()
        with sqlite3.connect(app_module.DB_PATH) as db:
            db.execute("INSERT INTO users (username,ispace_username) VALUES ('student-local','s123456789')")
        self.sign_in()
        self.client.set_cookie('sso_token', 'test-shared-token')
        self.csrf = self.client.get('/api/mail-digest/status').json['csrf']
        self.fake_mail = mock.Mock()
        self.fake_mail.inbox.return_value = parse_inbox(LIST)
        self.fake_mail.preview.return_value = {'body':'Read me', 'has_images':False, 'body_truncated':False}
        self.mail_patch = mock.patch('mail_digest.SchoolMailbox', return_value=self.fake_mail)
        self.mail_patch.start()
        self.shared_patch = mock.patch('mail_digest.sso_bridge.shared_user_for_token', side_effect=lambda token: {'username':'student-local'} if token == 'test-shared-token' else None)
        self.shared_patch.start()

    def tearDown(self):
        self.mail_patch.stop(); self.shared_patch.stop()
        for entry in mail_digest._sessions.values(): entry['mailbox'].close()
        mail_digest._sessions.clear()
        app_module.DB_PATH = self.old_db
        app_module.app.testing = self.old_testing
        self.tmp.cleanup()

    def sign_in(self, uid=1):
        with self.client.session_transaction() as sess: sess['user_id'] = uid

    def post(self, path, payload):
        response = self.client.post('/api/mail-digest/' + path, json=payload, headers={'X-Mail-CSRF':self.csrf})
        if response.is_json and response.json.get('csrf'): self.csrf = response.json['csrf']
        return response

    def connect(self):
        r = self.post('connect', {'password':'test-only-password', 'username':'someone-else'})
        self.assertEqual(r.status_code, 200, r.json)
        return r

    def test_connect_scopes_identity_and_never_returns_or_saves_credentials(self):
        response = self.connect()
        self.fake_mail.login.assert_called_once_with('s123456789', 'test-only-password')
        self.assertNotIn('password', response.text)
        self.assertNotIn('sid', response.text)
        with sqlite3.connect(app_module.DB_PATH) as db:
            self.assertIsNone(db.execute('SELECT ispace_password_encrypted FROM users').fetchone()[0])
        with self.client.session_transaction() as sess:
            self.assertNotIn('test-only-password', str(dict(sess)))

    def test_connect_rejects_missing_csrf_and_logout_clears_cache(self):
        r=self.client.post('/api/mail-digest/connect',json={'password':'p'})
        self.assertEqual(r.status_code,403)
        self.connect()
        self.client.post('/api/logout')
        self.assertEqual(len(mail_digest._sessions),0)
        self.assertEqual(self.post('connect',{'password':'p'}).status_code,401)

    def test_account_switch_cannot_read_old_mail(self):
        self.connect(); self.sign_in(999)
        self.assertEqual(self.post('inbox',{'page':0}).status_code,401)

    def test_other_browser_of_same_user_has_no_mail_session(self):
        self.connect(); other=app_module.app.test_client()
        with other.session_transaction() as sess: sess['user_id']=1
        status=other.get('/api/mail-digest/status').json
        self.assertFalse(status['connected'])
        r=other.post('/api/mail-digest/inbox',json={'page':0},headers={'X-Mail-CSRF':status['csrf']})
        self.assertEqual(r.status_code,401)

    def test_expiry_and_disconnect(self):
        self.connect()
        for entry in mail_digest._sessions.values(): entry['expires']=0
        self.assertEqual(self.post('inbox',{'page':0}).status_code,401)
        self.assertTrue(self.fake_mail.close.called)

    def test_summary_uses_selected_owned_mail_and_cached_result(self):
        self.connect()
        digest={'overview':'News','items':[], 'credits':2}
        with mock.patch('mail_digest.omni_request',return_value={'model':'test'}), mock.patch('mail_digest.summarize',return_value=digest) as summarize_call:
            self.assertEqual(self.post('summarize',{'ids':['M-private']}).status_code,400)
            self.assertEqual(self.post('summarize',{'ids':['M~1','M~1']}).status_code,400)
            a=self.post('summarize',{'ids':['M~1']}); b=self.post('summarize',{'ids':['M~1']})
            self.assertEqual(a.status_code,200,a.json)
            self.assertEqual(a.json['summarized_count'],1)
            self.assertTrue(b.json['cached'])
            summarize_call.assert_called_once()
            self.fake_mail.preview.assert_called_once_with('M~1')
            self.assertEqual(a.headers['Cache-Control'],'no-store')

    def test_mismatched_omnichat_account_rejected_before_body_read(self):
        self.connect()
        with mock.patch('mail_digest.sso_bridge.shared_user_for_token',return_value={'username':'other'}):
            self.assertEqual(self.post('summarize',{'ids':['M~1']}).status_code,401)
        self.fake_mail.preview.assert_not_called()

    def test_service_failure_never_reports_success(self):
        self.connect()
        with mock.patch('mail_digest.omni_request',side_effect=MailError('Not ready','summary_not_configured',503)):
            self.assertEqual(self.post('summarize',{'ids':['M~1']}).status_code,503)
        self.fake_mail.preview.assert_not_called()

    def test_source_files_are_blocked_and_page_served(self):
        for path in ['/mail_digest/client.py','/mail_digest/__init__.py','/tests/test_mail_digest.py']:
            self.assertEqual(self.client.get(path).status_code,404)
        self.assertEqual(self.client.get('/mail-summary/').status_code,200)
