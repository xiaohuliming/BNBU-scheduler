"""Printing authentication must not subscribe users to unrelated data access."""
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

os.environ.setdefault('MAXCOURSE_SECRET_KEY', 'print-privacy-test-secret')
import app as application
import crawler


class PrintPrivacyTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_db = application.DB_PATH
        self.original_testing = application.app.config['TESTING']
        application.DB_PATH = os.path.join(self.tempdir.name, 'print-privacy.db')
        application.app.config['TESTING'] = True
        application.init_db()
        self.client = application.app.test_client()
        self.credentials = {'username': 't_privacy_fixture', 'password': 'synthetic-only', 'purpose': 'print'}
        self.addCleanup(self.tempdir.cleanup)
        self.addCleanup(setattr, application, 'DB_PATH', self.original_db)
        self.addCleanup(application.app.config.update, TESTING=self.original_testing)

    def authenticate(self, valid=True):
        return mock.patch.object(application, 'verify_credentials', return_value=valid)

    def test_print_login_creates_owner_without_calendar_mail_or_shared_sso(self):
        with self.authenticate() as verify, \
                mock.patch.object(application, 'fetch_timeline') as timeline, \
                mock.patch.object(application, 'sync_ispace_todos_for_user') as sync, \
                mock.patch.object(application.mail_brief_service, 'start') as mail, \
                mock.patch.object(application.sso_bridge, 'issue_shared_token') as sso:
            response = self.client.post('/api/login/ispace', json=self.credentials)
        self.assertEqual(response.status_code, 200)
        verify.assert_called_once_with('t_privacy_fixture', 'synthetic-only')
        for unrelated in (timeline, sync, mail, sso):
            unrelated.assert_not_called()
        with sqlite3.connect(application.DB_PATH) as db:
            user = db.execute('SELECT id, ispace_username, password_hash, ispace_password_encrypted FROM users').fetchone()
            self.assertEqual(db.execute('SELECT COUNT(*) FROM todos').fetchone()[0], 0)
        self.assertEqual(user[1], 't_privacy_fixture')
        self.assertFalse(user[2])
        self.assertFalse(user[3])
        with self.client.session_transaction() as session:
            self.assertEqual(session['user_id'], user[0])
            self.assertNotIn('synthetic-only', str(dict(session)))

    def test_campus_application_login_does_not_read_calendar_or_mail(self):
        credentials = {**self.credentials, 'purpose': 'campus-connect'}
        with self.authenticate() as verify, mock.patch.object(application, 'fetch_timeline') as timeline, mock.patch.object(application.mail_brief_service, 'start') as mail:
            response = self.client.post('/api/login/ispace', json=credentials)
        self.assertEqual(response.status_code, 200)
        verify.assert_called_once()
        timeline.assert_not_called()
        mail.assert_not_called()

    def test_failed_print_validation_cannot_create_or_login_an_owner(self):
        with self.authenticate(False), mock.patch.object(application.mail_brief_service, 'start') as mail:
            response = self.client.post('/api/login/ispace', json=self.credentials)
        self.assertEqual(response.status_code, 401)
        mail.assert_not_called()
        with sqlite3.connect(application.DB_PATH) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM users').fetchone()[0], 0)
        with self.client.session_transaction() as session:
            self.assertNotIn('user_id', session)

    def test_print_login_preserves_existing_binding_and_local_account_protection(self):
        with sqlite3.connect(application.DB_PATH) as db:
            owner = db.execute('INSERT INTO users (username, ispace_username) VALUES (?, ?)',
                               ('local-fixture', 't_privacy_fixture')).lastrowid
        with self.authenticate():
            response = self.client.post('/api/login/ispace', json=self.credentials)
        self.assertEqual(response.get_json()['user']['id'], owner)
        with sqlite3.connect(application.DB_PATH) as db:
            db.execute('DELETE FROM users')
            db.execute('INSERT INTO users (username, password_hash) VALUES (?, ?)',
                       ('t_privacy_fixture', 'local-password-hash-fixture'))
        with self.authenticate():
            response = self.client.post('/api/login/ispace', json=self.credentials)
        self.assertEqual(response.status_code, 409)

    def test_regular_ispace_login_keeps_its_existing_sync_behavior(self):
        with self.authenticate() as verify, \
                mock.patch.object(application, 'fetch_timeline', return_value=[]) as timeline, \
                mock.patch.object(application, 'sync_ispace_todos_for_user', return_value={}) as sync, \
                mock.patch.object(application.mail_brief_service, 'start') as mail, \
                mock.patch.object(application.sso_bridge, 'issue_shared_token', return_value='fixture') as sso, \
                mock.patch.object(application.sso_bridge, 'set_sso_cookie'):
            response = self.client.post('/api/login/ispace', json={key: value for key, value in self.credentials.items() if key != 'purpose'})
        self.assertEqual(response.status_code, 200)
        verify.assert_not_called()
        timeline.assert_called_once()
        sync.assert_called_once()
        mail.assert_called_once()
        sso.assert_called_once()

    def test_print_school_session_is_closed_without_reading_calendar(self):
        with mock.patch.object(crawler.requests, 'Session') as factory, mock.patch.object(crawler, 'login', return_value=True) as login:
            self.assertTrue(crawler.verify_credentials('t_privacy_fixture', 'synthetic-only'))
        login.assert_called_once_with(factory.return_value.__enter__.return_value, 't_privacy_fixture', 'synthetic-only')
        factory.return_value.__exit__.assert_called_once()
