import os
import sqlite3
import tempfile
import unittest
from unittest import mock

os.environ.setdefault('MAXCOURSE_SECRET_KEY','test-secret-key')
import app as app_module


class PrivacyControlsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.old_db=app_module.DB_PATH;self.old_testing=app_module.app.testing
        app_module.DB_PATH=self.tmp.name+'/test.db';app_module.init_db();app_module.app.testing=True
        self.client=app_module.app.test_client()
        self.shared=mock.patch('app.sso_bridge.shared_user_for_token',return_value=None);self.shared.start()
        with sqlite3.connect(app_module.DB_PATH) as db:
            db.execute("INSERT INTO users(username,ispace_username,ispace_password_encrypted,ispace_auto_sync_enabled) VALUES ('privacy-one','s123456789','legacy-cipher',1),('privacy-two','s987654321','other-cipher',1)")
            for uid,sid in [(1,'s123456789'),(2,'s987654321')]:
                db.execute("INSERT INTO todos(user_id,title,ispace_id) VALUES (?,'school task',100)",(uid,))
                db.execute("INSERT INTO todos(user_id,title) VALUES (?,'manual task')",(uid,))
                db.execute('INSERT INTO mail_weekly_briefs(user_id,school_username,payload_encrypted) VALUES (?,?,?)',(uid,sid,'cipher'))
        with self.client.session_transaction() as sess:sess['user_id']=1
        self.csrf=self.client.get('/api/user').json['account_csrf']
    def tearDown(self):
        self.shared.stop();app_module.DB_PATH=self.old_db;app_module.app.testing=self.old_testing;self.tmp.cleanup()

    def test_unlink_requires_csrf_and_clears_only_callers_school_data(self):
        self.assertEqual(self.client.delete('/api/user/bind/ispace').status_code,403)
        with mock.patch.object(app_module.mail_brief_service,'revoke') as revoke:
            r=self.client.delete('/api/user/bind/ispace',headers={'X-Account-CSRF':self.csrf})
        self.assertEqual(r.status_code,200);revoke.assert_called_once_with(1)
        with sqlite3.connect(app_module.DB_PATH) as db:
            self.assertEqual(db.execute('SELECT ispace_username,ispace_password_encrypted,ispace_auto_sync_enabled,ispace_link_version FROM users WHERE id=1').fetchone(),(None,None,0,1))
            self.assertEqual(db.execute('SELECT title FROM todos WHERE user_id=1').fetchall(),[('manual task',)])
            self.assertEqual(db.execute('SELECT ispace_password_encrypted FROM users WHERE id=2').fetchone()[0],'other-cipher')
            self.assertEqual(db.execute('SELECT user_id FROM mail_weekly_briefs').fetchall(),[(2,)])
        self.assertIsNone(self.client.get('/api/user').json['user']['ispace_username'])

    def test_schema_update_preserves_opt_in_saved_credentials(self):
        app_module.init_db()
        with sqlite3.connect(app_module.DB_PATH) as db:self.assertEqual(db.execute('SELECT ispace_password_encrypted,ispace_auto_sync_enabled FROM users WHERE id=1').fetchone(),('legacy-cipher',1))

    def test_unlink_during_scheduled_sync_prevents_import_resurrection(self):
        def fetch(*args):
            response=self.client.delete('/api/user/bind/ispace',headers={'X-Account-CSRF':self.csrf})
            self.assertEqual(response.status_code,200)
            return [{'id':999,'name':'must not restore','course':'test','due_date':1800000000,'url':'https://ispace.bnbu.edu.cn/mod/assign/view.php?id=999'}]
        with mock.patch.dict(os.environ,{'MAXCOURSE_ISPACE_SYNC_SECRET':'fixture'}),mock.patch.object(app_module,'is_ispace_credential_encryption_configured',return_value=True),mock.patch.object(app_module,'decrypt_ispace_password',return_value='fixture'),mock.patch.object(app_module,'fetch_timeline',side_effect=fetch):
            self.client.post('/api/todos/auto-sync/dispatch',headers={'X-Auto-Sync-Secret':'fixture'})
        with sqlite3.connect(app_module.DB_PATH) as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM todos WHERE user_id=1 AND ispace_id IS NOT NULL').fetchone()[0],0)

    def test_public_disclosure_scope_and_contact_details(self):
        for url in ['/privacy/','/changelog/']:
            result=self.client.get(url);self.assertEqual(result.status_code,200)
            for required in ['ToDoHacambiado','https://f0xy.me/','t330025032','SIrus.','github.com/xiaohuliming/BNBU-scheduler']:
                self.assertIn(required,result.text)
        page=self.client.get('/privacy/').text
        self.assertIn('不是第三方独立安全审计',page)
        self.assertIn('服务器持有解密密钥',page)
        self.assertIn('历史数据库备份可能保留',page)
        self.assertNotIn('已清理在线业务库及本项目控制范围',page)

    def test_account_deletion_revokes_shared_cookie_and_brief(self):
        with mock.patch('app.sso_bridge.revoke_token') as revoke, mock.patch('app.sso_bridge.clear_sso_cookie') as clear:
            response=self.client.delete('/api/user/delete')
        self.assertEqual(response.status_code,200)
        revoke.assert_called_once();clear.assert_called_once()
        with sqlite3.connect(app_module.DB_PATH) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM mail_weekly_briefs WHERE user_id=1').fetchone()[0],0)

    def test_manual_sync_after_unlink_cannot_restore_school_tasks(self):
        def fetch(*args):
            self.client.delete('/api/user/bind/ispace',headers={'X-Account-CSRF':self.csrf})
            return [{'id':777,'name':'stale task','course':'test','due_date':1800000000,'url':'https://ispace.bnbu.edu.cn/mod/assign/view.php?id=777'}]
        with mock.patch.object(app_module,'fetch_timeline',side_effect=fetch):
            response=self.client.post('/api/todos/sync',json={'username':'s123456789','password':'one-use'})
        self.assertEqual(response.status_code,409)
        with sqlite3.connect(app_module.DB_PATH) as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM todos WHERE user_id=1 AND ispace_id IS NOT NULL').fetchone()[0],0)
