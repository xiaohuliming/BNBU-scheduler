import json
import os
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timezone
from unittest import mock

os.environ.setdefault('MAXCOURSE_SECRET_KEY', 'test-secret-key')
import app as app_module
from mail_digest.client import MailError, SchoolMailbox, parse_inbox, extract_preview
from mail_digest.weekly import WeeklyBriefService, init_tables
from mail_digest.summary import summarize_week


def mail(mid='M~1', timestamp=None):
    return {'id':mid, 'subject':'申请截止提醒', 'sender':'教务处', 'sender_address':'office@example.edu',
            'received_at':datetime.fromtimestamp(timestamp or time.time(),timezone.utc).isoformat(), 'snippet':'请周五前申请'}


def html_row(mid, timestamp, unread=True):
    return f'''<table><tr><td><input type="checkbox" name="mailid" value="{mid}" unread="{str(unread).lower()}"
        fn="Registry" fa="office@example.edu" totime="{int(timestamp*1000)}"></td><td class="gt"><u>Registration</u><b>Apply Friday</b></td></tr></table>'''


class ParserTests(unittest.TestCase):
    def test_week_includes_read_and_unread_and_excludes_old_mail(self):
        now=1800000000
        html='<body id="list">'+html_row('read-mail',now-100,False)+html_row('boundary',now-7*86400)+html_row('old',now-7*86400-1)+'</body>'
        client=SchoolMailbox();client.sid='fixture-sid'
        with mock.patch.object(client,'_request',return_value=(html,'')) as request:
            result=client.recent(now=now)
        self.assertEqual([m['id'] for m in result['messages']],['read-mail','boundary'])
        self.assertTrue(result['complete'])
        self.assertNotIn('flag',request.call_args.kwargs['params'])
        self.assertEqual(request.call_args.kwargs['params']['folderid'],1)
        client.close()

    def test_week_size_cap_is_explicit(self):
        now=1800000000;client=SchoolMailbox()
        html='<body id="list">'+html_row('a',now-1)+html_row('b',now-2)+'</body>'
        with mock.patch.object(client,'_request',return_value=(html,'')):
            result=client.recent(now=now,max_messages=1)
        self.assertFalse(result['complete']);self.assertEqual(len(result['messages']),1);client.close()

    def test_bad_dates_do_not_claim_complete_coverage(self):
        client=SchoolMailbox()
        html='<body id="list">'+html_row('a',1).replace('totime="1000"','totime="bad"')+'</body>'
        with mock.patch.object(client,'_request',return_value=(html,'')):
            self.assertFalse(client.recent()['complete'])
        client.close()

    def test_changed_layout_fails_closed(self):
        for html in ['<input type=password>','<body>changed</body>','<body id="list">changed</body>']:
            with self.assertRaises(MailError):parse_inbox(html,unread_only=False)

    def test_preview_is_text_only_and_preserves_read_state(self):
        data=extract_preview('<p>Deadline Friday</p><script>steal()</script><img src="https://tracker.invalid"><iframe>hidden</iframe>')
        self.assertEqual(data['body'],'Deadline Friday');self.assertTrue(data['has_images'])
        client=SchoolMailbox();client.sid='secret'
        with mock.patch.object(client,'_request',return_value=('<p>Hello</p>','')) as req:
            client.preview('M~1')
        self.assertEqual(req.call_args.kwargs['params']['mode'],'preview')
        self.assertEqual(req.call_args.kwargs['params']['t'],'quickreadmail');client.close()

    def test_untrusted_redirect_never_receives_credentials(self):
        client=SchoolMailbox()
        with mock.patch.object(client.http,'request') as req:
            for url in ['http://mis.bnbu.edu.cn/','https://evil.test/','https://exmail.qq.com@evil.test/']:
                with self.assertRaises(MailError):client._request('GET',url)
            req.assert_not_called()
        client.close()


class DeferredExecutor:
    def __init__(self):self.calls=[]
    def submit(self,fn,*args):self.calls.append((fn,args))
    def run(self):
        fn,args=self.calls.pop(0);fn(*args)


class WeeklyServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db_path=self.tmp.name+'/mail.db'
        with self.db() as db:
            db.execute('CREATE TABLE users (id INTEGER PRIMARY KEY,ispace_username TEXT,ispace_link_version INTEGER DEFAULT 0)')
            db.execute("INSERT INTO users(id,ispace_username) VALUES (1,'s123456789'),(2,'s987654321')")
            init_tables(db.cursor())
        self.executor=DeferredExecutor()
        self.service=WeeklyBriefService(self.db,'test-secret',lambda:True,executor=self.executor)
        self.box=mock.Mock();self.now=int(time.time())
        self.metadata=[mail(timestamp=self.now-100)]
        self.box.recent.return_value={'messages':self.metadata,'complete':True,'window_start':self.now-7*86400,'window_end':self.now}
        self.box.preview.return_value={'body':'Only-in-transient-body','body_truncated':False,'has_images':False}
        self.client_patch=mock.patch('mail_digest.weekly.SchoolMailbox',return_value=self.box);self.client_patch.start()
        self.summary_patch=mock.patch('mail_digest.weekly.summarize_week',return_value=[{'text':'需要调整课程的话，请于周五前提交申请。','source_ids':['M~1']}]);self.summarizer=self.summary_patch.start()
    def db(self):
        db=sqlite3.connect(self.db_path);db.row_factory=sqlite3.Row;return db
    def tearDown(self):
        self.client_patch.stop();self.summary_patch.stop();self.tmp.cleanup()
    def start(self):return self.service.start(1,'s123456789','transient-school-password')

    def test_start_is_nonblocking_and_persists_only_encrypted_brief(self):
        self.assertTrue(self.start());self.box.login.assert_not_called()
        self.assertNotIn('transient-school-password',str(self.executor.calls))
        self.assertEqual(self.service.status(1,'s123456789')['state'],'working')
        self.executor.run();self.box.login.assert_called_once_with('s123456789','transient-school-password')
        result=self.service.status(1,'s123456789')['brief'];self.assertEqual(result['mail_count'],1)
        self.assertNotIn('body',json.dumps(result));self.assertNotIn('Only-in-transient-body',json.dumps(result))
        with self.db() as db:raw=str([tuple(r) for r in db.execute('SELECT * FROM mail_weekly_briefs')])
        for private in ['transient-school-password','Only-in-transient-body','需要调整课程','申请截止提醒']:
            self.assertNotIn(private,raw)
        self.assertTrue(self.service.status(1,'s123456789')['connection_ready'])
        self.assertEqual(self.summarizer.call_count,1)

    def test_duplicate_jobs_and_cross_account_cache_are_blocked(self):
        self.assertTrue(self.start());self.assertFalse(self.start());self.executor.run()
        self.assertEqual(self.service.status(2,'s987654321')['state'],'idle')
        with self.db() as db:
            db.execute('''INSERT INTO mail_weekly_briefs(user_id,school_username,payload_encrypted,generated_at)
                SELECT 2,'s987654321',payload_encrypted,generated_at FROM mail_weekly_briefs WHERE user_id=1''')
        self.assertEqual(self.service.status(2,'s987654321')['state'],'idle')

    def test_account_binding_change_cancels_queued_read(self):
        self.start()
        with self.db() as db:db.execute("UPDATE users SET ispace_username='another' WHERE id=1")
        self.executor.run();self.box.login.assert_not_called()

    def test_each_new_visit_rereads_and_regenerates_using_only_mail_session(self):
        self.start();self.executor.run()
        self.assertTrue(self.service.start(1,'s123456789'))
        self.executor.run()
        self.assertEqual(self.summarizer.call_count,2)
        self.assertEqual(self.box.recent.call_count,2)
        self.box.login.assert_called_once_with('s123456789','transient-school-password')

    def test_expired_session_requires_manual_or_opt_in_saved_password(self):
        self.start();self.executor.run()
        self.service.sessions[1].expires_at=0
        self.assertFalse(self.service.start(1,'s123456789'))
        self.assertTrue(self.service.status(1,'s123456789')['reauth_required'])
        self.box.close.assert_called()

    def test_logout_cancels_queued_job_and_relogin_can_start_another(self):
        self.start();self.service.revoke(1)
        self.assertTrue(self.start())
        self.executor.run();self.box.login.assert_not_called()
        self.executor.run();self.box.login.assert_called_once()

    def test_unlink_during_body_read_prevents_ai_and_cache_restore(self):
        def preview(mid):
            with self.db() as db:
                db.execute('UPDATE users SET ispace_username=NULL,ispace_link_version=1 WHERE id=1')
                db.execute('DELETE FROM mail_weekly_briefs WHERE user_id=1')
            self.service.revoke(1)
            return {'body':'unused','body_truncated':False,'has_images':False}
        self.box.preview.side_effect=preview
        self.start();self.executor.run();self.summarizer.assert_not_called()
        with self.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM mail_weekly_briefs').fetchone()[0],0)
        self.box.close.assert_called()

    def test_unlink_after_ai_submission_does_not_restore_cached_output(self):
        def finish(*args):
            with self.db() as db:
                db.execute('UPDATE users SET ispace_username=NULL,ispace_link_version=1 WHERE id=1')
                db.execute('DELETE FROM mail_weekly_briefs WHERE user_id=1')
            self.service.revoke(1)
            return [{'text':'stale result','source_ids':['M~1']}]
        self.summarizer.side_effect=finish
        self.start();self.executor.run()
        with self.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM mail_weekly_briefs').fetchone()[0],0)

    def test_atomic_job_reservation_rejects_changed_binding(self):
        with self.db() as db:db.execute('UPDATE users SET ispace_link_version=1 WHERE id=1')
        with mock.patch.object(self.service,'_identity',return_value=0):
            self.assertFalse(self.start())
        self.assertEqual(len(self.executor.calls),0)
        with self.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM mail_weekly_briefs').fetchone()[0],0)

    def test_provider_failure_is_quiet_and_next_visit_may_retry(self):
        self.summarizer.side_effect=MailError('fixture upstream failure')
        self.start();self.executor.run()
        self.assertEqual(self.service.status(1,'s123456789')['state'],'idle')
        self.assertTrue(self.service.start(1,'s123456789'))
        self.executor.run()

    def test_empty_week_uses_no_model(self):
        self.box.recent.return_value['messages']=[]
        self.start();self.executor.run();self.summarizer.assert_not_called()
        self.assertEqual(self.service.status(1,'s123456789')['brief']['items'],[])

    def test_scheduling_failure_never_breaks_login(self):
        with mock.patch.object(self.service,'_identity',side_effect=RuntimeError('DB unavailable')):
            self.assertFalse(self.start())


class BriefRoutesTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.old_db=app_module.DB_PATH;self.old_testing=app_module.app.testing
        app_module.DB_PATH=self.tmp.name+'/test.db';app_module.init_db();app_module.app.testing=True
        self.client=app_module.app.test_client()
        self.bridge=mock.patch('app.sso_bridge.issue_shared_token',return_value=None);self.bridge.start()
        self.shared=mock.patch('app.sso_bridge.shared_user_for_token',return_value=None);self.shared.start()
        with sqlite3.connect(app_module.DB_PATH) as db:db.execute("INSERT INTO users(username,ispace_username) VALUES ('student-local','s123456789')")
    def tearDown(self):
        self.bridge.stop();self.shared.stop();app_module.DB_PATH=self.old_db;app_module.app.testing=self.old_testing;self.tmp.cleanup()
    def sign_in(self):
        with self.client.session_transaction() as sess:sess['user_id']=1

    def test_successful_ispace_login_triggers_background_job_for_bound_account(self):
        with mock.patch('app.fetch_timeline',return_value=[]),mock.patch.object(app_module.mail_brief_service,'start') as start:
            response=self.client.post('/api/login/ispace',json={'username':'s123456789','password':'one-off-password'})
        self.assertEqual(response.status_code,200);start.assert_called_once_with(1,'s123456789','one-off-password')
        with sqlite3.connect(app_module.DB_PATH) as db:self.assertIsNone(db.execute('SELECT ispace_password_encrypted FROM users').fetchone()[0])

    def test_failed_login_never_reads_mail(self):
        with mock.patch('app.fetch_timeline',return_value={'error':'Login failed'}),mock.patch.object(app_module.mail_brief_service,'start') as start:
            self.assertEqual(self.client.post('/api/login/ispace',json={'username':'s123456789','password':'bad'}).status_code,401)
            start.assert_not_called()

    def test_cache_is_authenticated_private_and_not_query_selectable(self):
        self.assertEqual(self.client.get('/api/mail-brief').status_code,401)
        self.sign_in();result=self.client.get('/api/mail-brief?user_id=2')
        self.assertEqual(result.json['state'],'idle');self.assertEqual(result.json['user_id'],1)
        self.assertIn('csrf',result.json)
        self.assertEqual(result.headers['Cache-Control'],'no-store')

    def test_readonly_polling_does_not_schedule_but_new_visits_do(self):
        self.sign_in()
        data=self.client.get('/api/mail-brief').json
        with mock.patch.object(app_module.mail_brief_service,'start') as start:
            for _ in range(3):self.client.get('/api/mail-brief')
            start.assert_not_called()
            self.assertEqual(self.client.post('/api/mail-brief/refresh',json={'visit_id':'a'*20}).status_code,403)
            for visit in ['a'*20,'a'*20,'b'*20]:
                response=self.client.post('/api/mail-brief/refresh',json={'visit_id':visit},headers={'X-Mail-CSRF':data['csrf']})
                self.assertEqual(response.status_code,200)
            self.assertEqual(start.call_count,2)

    def test_separate_tool_is_retired(self):
        for path in ['/mail-summary/','/mail-summary/index.html']:
            r=self.client.get(path);self.assertEqual(r.status_code,302);self.assertEqual(r.headers['Location'],'/')
        self.assertIn(self.client.post('/api/mail-digest/connect',json={}).status_code,(404,405))
        self.assertEqual(self.client.get('/mail_digest/weekly.py').status_code,404)


class SummaryBoundaryTests(unittest.TestCase):
    def test_service_auth_instead_of_user_billing_and_validated_sources(self):
        response=mock.Mock(status_code=200)
        response.json.return_value={'items':[{'text':'周五前申请','source_ids':['M~1']}]}
        http=mock.MagicMock();http.__enter__.return_value=http;http.post.return_value=response
        with mock.patch.dict(os.environ,{'MAXCOURSE_MAIL_BRIEF_TOKEN':'test-service-secret'}),mock.patch('mail_digest.summary.requests.Session',return_value=http):
            self.assertEqual(len(summarize_week([mail()],1,2)),1)
            headers=http.post.call_args.kwargs['headers'];self.assertNotIn('Authorization',headers)
            self.assertEqual(headers['X-Mail-Brief-Token'],'test-service-secret')
            self.assertFalse(http.post.call_args.kwargs['allow_redirects'])
            response.json.return_value={'items':[{'text':'Invented','source_ids':['wrong']}]}
            with self.assertRaises(MailError):summarize_week([mail()],1,2)
