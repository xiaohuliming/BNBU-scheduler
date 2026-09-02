import os
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

from cryptography.fernet import Fernet

os.environ.setdefault('MAXCOURSE_SECRET_KEY', 'test-secret-key')

import app as app_module


class ISpaceAutoSyncTestCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_db_path = app_module.DB_PATH
        self.original_testing = app_module.app.config.get('TESTING', False)
        self.credential_key = Fernet.generate_key().decode('ascii')

        app_module.DB_PATH = os.path.join(self.tempdir.name, 'test.db')
        app_module.init_db()
        app_module.app.config.update(TESTING=True)
        self.client = app_module.app.test_client()

        with sqlite3.connect(app_module.DB_PATH) as conn:
            conn.execute(
                '''
                INSERT INTO users (username, ispace_username, display_name)
                VALUES (?, ?, ?)
                ''',
                ('student-local', 's123456789', 'Student'),
            )
            conn.commit()

        with self.client.session_transaction() as flask_session:
            flask_session['user_id'] = 1
            flask_session['username'] = 'student-local'
            flask_session['display_name'] = 'Student'
            flask_session.permanent = True

    def tearDown(self):
        app_module.DB_PATH = self.original_db_path
        app_module.app.config.update(TESTING=self.original_testing)
        self.tempdir.cleanup()

    @property
    def encryption_env(self):
        return {'MAXCOURSE_ISPACE_CREDENTIAL_KEY': self.credential_key}

    @staticmethod
    def timeline_payload(event_id=501):
        return [{
            'id': event_id,
            'name': 'Submit report',
            'course': 'COMP1001',
            'due_date': int(time.time()) + 86400,
            'url': 'https://ispace.bnbu.edu.cn/mod/assign/view.php?id=501',
        }]

    def test_enable_auto_sync_verifies_encrypts_and_syncs_immediately(self):
        password = 'real-looking-password'
        with mock.patch.dict(os.environ, self.encryption_env, clear=False), \
                mock.patch.object(
                    app_module,
                    'fetch_timeline',
                    return_value=self.timeline_payload(),
                ) as fetch:
            response = self.client.put('/api/user/ispace-auto-sync', json={
                'enabled': True,
                'password': password,
            })

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload['settings']['enabled'])
        self.assertTrue(payload['settings']['credential_saved'])
        self.assertEqual(payload['sync']['added'], 1)
        self.assertNotIn(password, response.get_data(as_text=True))
        fetch.assert_called_once_with('s123456789', password)

        with sqlite3.connect(app_module.DB_PATH) as conn:
            encrypted = conn.execute(
                'SELECT ispace_password_encrypted FROM users WHERE id = 1'
            ).fetchone()[0]
        self.assertTrue(encrypted)
        self.assertNotEqual(encrypted, password)
        self.assertNotIn(password, encrypted)

        with mock.patch.dict(os.environ, self.encryption_env, clear=False):
            self.assertEqual(app_module.decrypt_ispace_password(encrypted), password)

    def test_settings_never_return_encrypted_credential(self):
        with mock.patch.dict(os.environ, self.encryption_env, clear=False), \
                mock.patch.object(app_module, 'fetch_timeline', return_value=self.timeline_payload()):
            self.client.put('/api/user/ispace-auto-sync', json={
                'enabled': True,
                'password': 'secret-password',
            })
            response = self.client.get('/api/user/ispace-auto-sync')

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload['credential_saved'])
        self.assertNotIn('encrypted', response.get_data(as_text=True).lower())
        self.assertNotIn('secret-password', response.get_data(as_text=True))

    def test_manual_sync_does_not_store_submitted_password(self):
        with mock.patch.object(app_module, 'fetch_timeline', return_value=self.timeline_payload()):
            response = self.client.post('/api/todos/sync', json={
                'username': 's123456789',
                'password': 'one-time-password',
            })

        self.assertEqual(response.status_code, 200)
        with sqlite3.connect(app_module.DB_PATH) as conn:
            row = conn.execute(
                'SELECT ispace_password_encrypted, ispace_auto_sync_enabled FROM users WHERE id = 1'
            ).fetchone()
        self.assertIsNone(row[0])
        self.assertEqual(row[1], 0)

    def test_local_account_can_bind_ispace_without_saving_password(self):
        with sqlite3.connect(app_module.DB_PATH) as conn:
            conn.execute('UPDATE users SET ispace_username = NULL WHERE id = 1')
            conn.commit()

        with mock.patch.object(app_module, 'fetch_timeline', return_value=self.timeline_payload()):
            response = self.client.post('/api/user/bind/ispace', json={
                'username': 's987654321',
                'password': 'binding-password',
            })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['ispace_username'], 's987654321')
        with sqlite3.connect(app_module.DB_PATH) as conn:
            row = conn.execute(
                '''
                SELECT ispace_username, ispace_password_encrypted,
                       ispace_auto_sync_enabled
                FROM users WHERE id = 1
                '''
            ).fetchone()
        self.assertEqual(row[0], 's987654321')
        self.assertIsNone(row[1])
        self.assertEqual(row[2], 0)

    def test_disabling_auto_sync_deletes_saved_password(self):
        with mock.patch.dict(os.environ, self.encryption_env, clear=False), \
                mock.patch.object(app_module, 'fetch_timeline', return_value=self.timeline_payload()):
            self.client.put('/api/user/ispace-auto-sync', json={
                'enabled': True,
                'password': 'password-to-delete',
            })
            response = self.client.put('/api/user/ispace-auto-sync', json={
                'enabled': False,
                'clear_credential': True,
            })

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()['settings']['enabled'])
        self.assertFalse(response.get_json()['settings']['credential_saved'])
        with sqlite3.connect(app_module.DB_PATH) as conn:
            row = conn.execute(
                'SELECT ispace_password_encrypted, ispace_auto_sync_enabled FROM users WHERE id = 1'
            ).fetchone()
        self.assertIsNone(row[0])
        self.assertEqual(row[1], 0)

    def test_enable_auto_sync_requires_server_encryption_key(self):
        with mock.patch.dict(
                os.environ,
                {'MAXCOURSE_ISPACE_CREDENTIAL_KEY': ''},
                clear=False,
        ), mock.patch.object(app_module, 'fetch_timeline') as fetch:
            response = self.client.put('/api/user/ispace-auto-sync', json={
                'enabled': True,
                'password': 'password',
            })

        self.assertEqual(response.status_code, 503)
        self.assertIn('not configured', response.get_json()['error'].lower())
        fetch.assert_not_called()

    def test_protected_dispatch_decrypts_and_syncs_enabled_users(self):
        with mock.patch.dict(os.environ, self.encryption_env, clear=False):
            encrypted = app_module.encrypt_ispace_password('stored-password')

        with sqlite3.connect(app_module.DB_PATH) as conn:
            conn.execute(
                '''
                UPDATE users
                SET ispace_password_encrypted = ?, ispace_auto_sync_enabled = 1
                WHERE id = 1
                ''',
                (encrypted,),
            )
            conn.commit()

        env = {
            **self.encryption_env,
            'MAXCOURSE_ISPACE_SYNC_SECRET': 'auto-sync-secret',
        }
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(
                    app_module,
                    'fetch_timeline',
                    return_value=self.timeline_payload(event_id=777),
                ) as fetch:
            unauthorized = self.client.post('/api/todos/auto-sync/dispatch')
            response = self.client.post(
                '/api/todos/auto-sync/dispatch',
                headers={'X-Auto-Sync-Secret': 'auto-sync-secret'},
            )

        self.assertEqual(unauthorized.status_code, 401)
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload['candidates'], 1)
        self.assertEqual(payload['synced'], 1)
        self.assertEqual(payload['failed'], 0)
        fetch.assert_called_once_with('s123456789', 'stored-password')

        with sqlite3.connect(app_module.DB_PATH) as conn:
            user = conn.execute(
                '''
                SELECT ispace_auto_sync_last_status, ispace_auto_sync_failure_count
                FROM users WHERE id = 1
                '''
            ).fetchone()
            todo_count = conn.execute(
                'SELECT COUNT(*) FROM todos WHERE user_id = 1 AND ispace_id = 777'
            ).fetchone()[0]
        self.assertEqual(user[0], 'success')
        self.assertEqual(user[1], 0)
        self.assertEqual(todo_count, 1)

    def test_three_auth_failures_disable_and_delete_saved_credential(self):
        with mock.patch.dict(os.environ, self.encryption_env, clear=False):
            encrypted = app_module.encrypt_ispace_password('expired-password')

        with sqlite3.connect(app_module.DB_PATH) as conn:
            conn.execute(
                '''
                UPDATE users
                SET ispace_password_encrypted = ?, ispace_auto_sync_enabled = 1
                WHERE id = 1
                ''',
                (encrypted,),
            )
            conn.commit()

        env = {
            **self.encryption_env,
            'MAXCOURSE_ISPACE_SYNC_SECRET': 'auto-sync-secret',
        }
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(
                    app_module,
                    'fetch_timeline',
                    return_value={'error': 'Login failed'},
                ) as fetch:
            responses = [
                self.client.post(
                    '/api/todos/auto-sync/dispatch',
                    headers={'X-Auto-Sync-Secret': 'auto-sync-secret'},
                )
                for _ in range(3)
            ]

        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(responses[-1].get_json()['disabled'], 1)
        with sqlite3.connect(app_module.DB_PATH) as conn:
            row = conn.execute(
                '''
                SELECT ispace_password_encrypted, ispace_auto_sync_enabled,
                       ispace_auto_sync_failure_count
                FROM users WHERE id = 1
                '''
            ).fetchone()
        self.assertIsNone(row[0])
        self.assertEqual(row[1], 0)
        self.assertEqual(row[2], 3)

    def test_ddl_page_exposes_manual_and_daily_auto_sync_choices(self):
        source = self.client.get('/').get_data(as_text=True)
        self.assertIn('/api/user/ispace-auto-sync', source)
        self.assertIn('手动同步', source)
        self.assertIn('每日自动同步', source)
        self.assertIn('密码会使用服务器密钥加密保存', source)
        self.assertIn('绑定 iSpace 账号', source)
        self.assertIn('绑定时只验证一次，不会保存密码', source)

    def test_auto_sync_source_and_setup_files_are_not_public(self):
        self.assertEqual(self.client.get('/ispace_credentials.py').status_code, 404)
        self.assertEqual(self.client.get('/ISPACE_AUTO_SYNC_SETUP.md').status_code, 404)


if __name__ == '__main__':
    unittest.main()
