import os
import sqlite3
import tempfile
import unittest
from unittest import mock

os.environ.setdefault('MAXCOURSE_SECRET_KEY', 'test-secret-key')

import app as app_module


class AppTestCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_db_path = app_module.DB_PATH
        self.original_testing = app_module.app.config.get('TESTING', False)

        app_module.DB_PATH = os.path.join(self.tempdir.name, 'test.db')
        app_module.df_cache = None
        app_module.init_db()
        app_module.app.config.update(TESTING=True)
        self.client = app_module.app.test_client()

    def tearDown(self):
        app_module.DB_PATH = self.original_db_path
        app_module.df_cache = None
        app_module.app.config.update(TESTING=self.original_testing)
        self.tempdir.cleanup()

    def insert_user(self, username, password_hash=None, display_name=None, ispace_username=None):
        with sqlite3.connect(app_module.DB_PATH) as conn:
            conn.execute(
                '''
                INSERT INTO users (username, password_hash, display_name, ispace_username)
                VALUES (?, ?, ?, ?)
                ''',
                (username, password_hash, display_name, ispace_username),
            )
            conn.commit()

    def test_password_login_returns_401_for_ispace_only_account(self):
        self.insert_user('shadow-user', password_hash=None)

        response = self.client.post(
            '/api/login',
            json={'username': 'shadow-user', 'password': 'anything'},
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()['error'], 'Invalid credentials')

    def test_unknown_api_route_returns_json_error(self):
        response = self.client.get('/api/not-a-real-endpoint')

        self.assertEqual(response.status_code, 404)
        self.assertTrue(response.is_json)
        self.assertEqual(response.get_json()['status'], 404)

    def test_analytics_tracks_views_and_reports_summary(self):
        first = self.client.post('/api/analytics/track', json={'view': 'home', 'path': '/'})
        second = self.client.post('/api/analytics/track', json={'view': 'classrooms', 'path': '/#classrooms'})
        summary = self.client.get('/api/analytics/summary')

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(summary.status_code, 200)

        data = summary.get_json()
        self.assertEqual(data['totalViews'], 2)
        self.assertEqual(data['uniqueVisitors'], 1)
        self.assertGreaterEqual(data['todayViews'], 2)

        views_by_name = {item['view_name']: item for item in data['byView']}
        self.assertEqual(views_by_name['home']['views'], 1)
        self.assertEqual(views_by_name['classrooms']['views'], 1)
        self.assertEqual(views_by_name['home']['visitors'], 1)

    def test_login_creates_long_lived_session_cookie(self):
        self.insert_user(
            'regular-user',
            password_hash=app_module.generate_password_hash('s3cret'),
            display_name='Regular User',
        )

        with self.client as client:
            response = client.post(
                '/api/login',
                json={'username': 'regular-user', 'password': 's3cret'},
            )

            self.assertEqual(response.status_code, 200)
            self.assertIn('Expires=', response.headers.get('Set-Cookie', ''))
            with client.session_transaction() as flask_session:
                self.assertTrue(flask_session.permanent)
                self.assertEqual(flask_session['user_id'], 1)

    def test_sync_updates_existing_todos_and_hides_stale_items(self):
        self.insert_user(
            'sync-user',
            password_hash=app_module.generate_password_hash('pw'),
            ispace_username='sync-user',
        )

        with sqlite3.connect(app_module.DB_PATH) as conn:
            conn.execute(
                '''
                INSERT INTO todos (user_id, ispace_id, title, course, due_date, url, is_stale)
                VALUES (?, ?, ?, ?, ?, ?, 0)
                ''',
                (1, 101, 'Old title', 'Old course', 111, 'https://old.example/task'),
            )
            conn.execute(
                '''
                INSERT INTO todos (user_id, ispace_id, title, course, due_date, url, is_stale)
                VALUES (?, ?, ?, ?, ?, ?, 0)
                ''',
                (1, 202, 'Should become stale', 'Legacy', 222, 'https://old.example/stale'),
            )
            conn.commit()

        with self.client.session_transaction() as flask_session:
            flask_session['user_id'] = 1
            flask_session['username'] = 'sync-user'
            flask_session['display_name'] = 'sync-user'
            flask_session.permanent = True

        payload = [
            {
                'id': 101,
                'name': 'Updated title',
                'course': 'Updated course',
                'due_date': 999,
                'url': 'https://new.example/task',
            }
        ]

        with mock.patch.object(app_module, 'fetch_timeline', return_value=payload):
            response = self.client.post(
                '/api/todos/sync',
                json={'username': 'sync-user', 'password': 'pw'},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['updated'], 1)
        self.assertEqual(response.get_json()['stale'], 1)

        with sqlite3.connect(app_module.DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            active_row = conn.execute(
                'SELECT title, course, due_date, url, is_stale FROM todos WHERE user_id = ? AND ispace_id = ?',
                (1, 101),
            ).fetchone()
            stale_row = conn.execute(
                'SELECT is_stale FROM todos WHERE user_id = ? AND ispace_id = ?',
                (1, 202),
            ).fetchone()

        self.assertEqual(active_row['title'], 'Updated title')
        self.assertEqual(active_row['course'], 'Updated course')
        self.assertEqual(active_row['due_date'], 999)
        self.assertEqual(active_row['url'], 'https://new.example/task')
        self.assertEqual(active_row['is_stale'], 0)
        self.assertEqual(stale_row['is_stale'], 1)

        todos_response = self.client.get('/api/todos')
        self.assertEqual(todos_response.status_code, 200)
        todos = todos_response.get_json()
        self.assertEqual(len(todos), 1)
        self.assertEqual(todos[0]['ispace_id'], 101)

    def test_sync_rejects_mismatched_ispace_account(self):
        self.insert_user(
            'sync-user',
            password_hash=app_module.generate_password_hash('pw'),
            ispace_username='sync-user',
        )

        with self.client.session_transaction() as flask_session:
            flask_session['user_id'] = 1
            flask_session['username'] = 'sync-user'
            flask_session['display_name'] = 'sync-user'
            flask_session.permanent = True

        with mock.patch.object(app_module, 'fetch_timeline') as mocked_fetch:
            response = self.client.post(
                '/api/todos/sync',
                json={'username': 'other-ispace-user', 'password': 'pw'},
            )

        self.assertEqual(response.status_code, 403)
        self.assertIn('mismatch', response.get_json()['error'])
        mocked_fetch.assert_not_called()

        with sqlite3.connect(app_module.DB_PATH) as conn:
            todo_count = conn.execute('SELECT COUNT(*) FROM todos WHERE user_id = ?', (1,)).fetchone()[0]

        self.assertEqual(todo_count, 0)

    def test_user_can_save_email_notification_settings(self):
        self.insert_user(
            'notify-user',
            password_hash=app_module.generate_password_hash('pw'),
            display_name='Notify User',
        )

        with self.client.session_transaction() as flask_session:
            flask_session['user_id'] = 1
            flask_session['username'] = 'notify-user'
            flask_session['display_name'] = 'Notify User'
            flask_session.permanent = True

        response = self.client.put(
            '/api/user/notifications',
            json={
                'email': 'Student@Example.COM',
                'enabled': True,
                'reminder_hours': [24, 3, 999],
            },
        )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()['settings']
        self.assertEqual(data['email'], 'student@example.com')
        self.assertTrue(data['enabled'])
        self.assertEqual(data['reminder_hours'], [24, 3])

        get_response = self.client.get('/api/user/notifications')
        self.assertEqual(get_response.status_code, 200)
        self.assertEqual(get_response.get_json()['email'], 'student@example.com')

    def test_email_notification_requires_valid_email_when_enabled(self):
        self.insert_user('notify-user', password_hash=app_module.generate_password_hash('pw'))

        with self.client.session_transaction() as flask_session:
            flask_session['user_id'] = 1
            flask_session['username'] = 'notify-user'
            flask_session['display_name'] = 'notify-user'
            flask_session.permanent = True

        response = self.client.put(
            '/api/user/notifications',
            json={'email': 'not-an-email', 'enabled': True, 'reminder_hours': [24]},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('Invalid email', response.get_json()['error'])

    def test_dispatch_due_email_notifications_sends_closest_window_once(self):
        self.insert_user(
            'notify-user',
            password_hash=app_module.generate_password_hash('pw'),
            display_name='Notify User',
        )
        due_date = int(app_module.time.time()) + (2 * 60 * 60)

        with sqlite3.connect(app_module.DB_PATH) as conn:
            conn.execute(
                '''
                UPDATE users
                SET email = ?, email_notifications_enabled = 1, email_reminder_hours = ?
                WHERE id = 1
                ''',
                ('student@example.com', '24,3,1'),
            )
            conn.execute(
                '''
                INSERT INTO todos (user_id, title, course, due_date, url, is_completed, is_stale)
                VALUES (1, 'Submit essay', 'WRIT1001', ?, 'https://ispace.example/task', 0, 0)
                ''',
                (due_date,),
            )
            conn.commit()

        env = {
            'MAXCOURSE_NOTIFICATION_SECRET': 'dispatch-secret',
            'SMTP_HOST': 'smtp.example.com',
            'SMTP_FROM_EMAIL': 'notify@bnbscheduler.top',
        }
        with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(app_module, 'send_email') as mocked_send:
            first_response = self.client.post(
                '/api/notifications/dispatch',
                headers={'X-Notification-Secret': 'dispatch-secret'},
                json={},
            )
            second_response = self.client.post(
                '/api/notifications/dispatch',
                headers={'X-Notification-Secret': 'dispatch-secret'},
                json={},
            )

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(first_response.get_json()['sent'], 1)
        self.assertEqual(second_response.status_code, 200)
        self.assertEqual(second_response.get_json()['sent'], 0)
        mocked_send.assert_called_once()

        with sqlite3.connect(app_module.DB_PATH) as conn:
            row = conn.execute(
                '''
                SELECT reminder_hours, success
                FROM email_notification_deliveries
                WHERE user_id = 1 AND todo_id = 1
                '''
            ).fetchone()

        self.assertEqual(row[0], 3)
        self.assertEqual(row[1], 1)

    def test_format_due_time_uses_beijing_timezone(self):
        # 1700000000 = 2023-11-14 22:13:20 UTC = 2023-11-15 06:13 Beijing
        formatted = app_module.format_due_time(1700000000)
        self.assertEqual(formatted, '2023-11-15 06:13')

    def test_dispatch_includes_unsubscribe_url_and_token_persists(self):
        self.insert_user(
            'notify-user',
            password_hash=app_module.generate_password_hash('pw'),
            display_name='Notify User',
        )
        due_date = int(app_module.time.time()) + (2 * 60 * 60)

        with sqlite3.connect(app_module.DB_PATH) as conn:
            conn.execute(
                '''
                UPDATE users
                SET email = ?, email_notifications_enabled = 1, email_reminder_hours = ?
                WHERE id = 1
                ''',
                ('student@example.com', '24,3,1'),
            )
            conn.execute(
                '''
                INSERT INTO todos (user_id, title, course, due_date, url, is_completed, is_stale)
                VALUES (1, 'Submit essay', 'WRIT1001', ?, 'https://ispace.example/task', 0, 0)
                ''',
                (due_date,),
            )
            conn.commit()

        env = {
            'MAXCOURSE_NOTIFICATION_SECRET': 'dispatch-secret',
            'SMTP_HOST': 'smtp.example.com',
            'SMTP_FROM_EMAIL': 'notify@bnbscheduler.top',
        }
        with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(app_module, 'send_email') as mocked_send:
            response = self.client.post(
                '/api/notifications/dispatch',
                headers={'X-Notification-Secret': 'dispatch-secret'},
                json={},
            )

        self.assertEqual(response.status_code, 200)
        mocked_send.assert_called_once()
        call_kwargs = mocked_send.call_args.kwargs
        self.assertIn('unsubscribe_url', call_kwargs)
        unsubscribe_url = call_kwargs['unsubscribe_url']
        self.assertIn('/api/notifications/unsubscribe?token=', unsubscribe_url)
        text_body = mocked_send.call_args.args[2]
        self.assertIn('Beijing Time', text_body)
        self.assertIn('Unsubscribe', text_body)

        with sqlite3.connect(app_module.DB_PATH) as conn:
            token_row = conn.execute('SELECT unsubscribe_token FROM users WHERE id = 1').fetchone()
        self.assertTrue(token_row[0])
        self.assertIn(token_row[0], unsubscribe_url)

    def test_unsubscribe_endpoint_disables_notifications(self):
        self.insert_user(
            'notify-user',
            password_hash=app_module.generate_password_hash('pw'),
        )
        with sqlite3.connect(app_module.DB_PATH) as conn:
            conn.execute(
                '''
                UPDATE users
                SET email = ?, email_notifications_enabled = 1, unsubscribe_token = ?
                WHERE id = 1
                ''',
                ('student@example.com', 'unsub-token-abc'),
            )
            conn.commit()

        bad = self.client.get('/api/notifications/unsubscribe?token=does-not-exist')
        self.assertEqual(bad.status_code, 404)

        ok = self.client.get('/api/notifications/unsubscribe?token=unsub-token-abc')
        self.assertEqual(ok.status_code, 200)
        self.assertIn(b"unsubscribed", ok.data.lower())

        with sqlite3.connect(app_module.DB_PATH) as conn:
            enabled = conn.execute(
                'SELECT email_notifications_enabled FROM users WHERE id = 1'
            ).fetchone()[0]
        self.assertEqual(enabled, 0)

        post_response = self.client.post(
            '/api/notifications/unsubscribe',
            data={'token': 'unsub-token-abc'},
        )
        self.assertEqual(post_response.status_code, 200)
        self.assertTrue(post_response.is_json)
        self.assertTrue(post_response.get_json()['success'])

    def test_unsubscribe_via_link_flag_set_and_cleared_on_save(self):
        self.insert_user(
            'notify-user',
            password_hash=app_module.generate_password_hash('pw'),
        )
        with sqlite3.connect(app_module.DB_PATH) as conn:
            conn.execute(
                '''
                UPDATE users
                SET email = ?, email_notifications_enabled = 1, unsubscribe_token = ?
                WHERE id = 1
                ''',
                ('student@example.com', 'unsub-flag-token'),
            )
            conn.commit()

        self.client.get('/api/notifications/unsubscribe?token=unsub-flag-token')

        with self.client.session_transaction() as flask_session:
            flask_session['user_id'] = 1
            flask_session['username'] = 'notify-user'
            flask_session['display_name'] = 'notify-user'
            flask_session.permanent = True

        before = self.client.get('/api/user/notifications').get_json()
        self.assertFalse(before['enabled'])
        self.assertTrue(before['unsubscribed_via_link'])

        save = self.client.put(
            '/api/user/notifications',
            json={'email': 'student@example.com', 'enabled': True, 'reminder_hours': [24]},
        )
        self.assertEqual(save.status_code, 200)
        self.assertFalse(save.get_json()['settings']['unsubscribed_via_link'])

        after = self.client.get('/api/user/notifications').get_json()
        self.assertTrue(after['enabled'])
        self.assertFalse(after['unsubscribed_via_link'])

    def test_dispatch_stops_after_three_failures(self):
        self.insert_user(
            'notify-user',
            password_hash=app_module.generate_password_hash('pw'),
        )
        due_date = int(app_module.time.time()) + (2 * 60 * 60)

        with sqlite3.connect(app_module.DB_PATH) as conn:
            conn.execute(
                '''
                UPDATE users
                SET email = ?, email_notifications_enabled = 1, email_reminder_hours = ?
                WHERE id = 1
                ''',
                ('student@example.com', '24,3,1'),
            )
            conn.execute(
                '''
                INSERT INTO todos (user_id, title, course, due_date, url, is_completed, is_stale)
                VALUES (1, 'Submit essay', 'WRIT1001', ?, 'https://ispace.example/task', 0, 0)
                ''',
                (due_date,),
            )
            conn.commit()

        env = {
            'MAXCOURSE_NOTIFICATION_SECRET': 'dispatch-secret',
            'SMTP_HOST': 'smtp.example.com',
            'SMTP_FROM_EMAIL': 'notify@bnbscheduler.top',
        }

        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(app_module, 'send_email', side_effect=RuntimeError('smtp down')) as mocked_send:
            for _ in range(3):
                self.client.post(
                    '/api/notifications/dispatch',
                    headers={'X-Notification-Secret': 'dispatch-secret'},
                    json={},
                )
            self.assertEqual(mocked_send.call_count, 3)

            fourth = self.client.post(
                '/api/notifications/dispatch',
                headers={'X-Notification-Secret': 'dispatch-secret'},
                json={},
            )
            self.assertEqual(mocked_send.call_count, 3)

        self.assertEqual(fourth.status_code, 200)
        data = fourth.get_json()
        self.assertEqual(data['sent'], 0)
        self.assertEqual(data['failed'], 0)
        self.assertEqual(data['skipped'], 1)

        with sqlite3.connect(app_module.DB_PATH) as conn:
            failure_count = conn.execute(
                '''
                SELECT COUNT(*)
                FROM email_notification_deliveries
                WHERE user_id = 1 AND todo_id = 1 AND success = 0
                '''
            ).fetchone()[0]
        self.assertEqual(failure_count, 3)

    def test_optimize_returns_real_course_units(self):
        mocked_result = {
            'best_units': 3,
            'missing': [],
            'solutions': [
                [
                    {
                        'course_code': 'COMP1001',
                        'title': 'Intro to Testing (1001)',
                        'teacher': 'Dr. Test',
                        'session': '1001',
                        'units': 3,
                        'meetings': [(0, 540, 600)],
                    }
                ]
            ],
        }

        with mock.patch.object(app_module, 'get_df', return_value=object()), mock.patch.object(
            app_module,
            'maximize_credits',
            return_value=mocked_result,
        ):
            response = self.client.post('/api/optimize', json={'codes': ['COMP1001']})

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data['found'])
        self.assertEqual(data['courses'][0]['units'], 3)
        self.assertEqual(data['totalUnits'], 3)

    def test_free_classrooms_returns_only_rooms_free_for_whole_window(self):
        sample_df = app_module.pd.DataFrame(
            [
                {
                    'Course Code': 'COMP1001',
                    'Course Title & Session': 'Intro Programming (1001)',
                    'Teachers': 'Dr. One',
                    'Class Schedule': 'Mon 08:00-08:50',
                    'Classroom': 'T4-101',
                },
                {
                    'Course Code': 'COMP1002',
                    'Course Title & Session': 'Discrete Math (1001)',
                    'Teachers': 'Dr. Two',
                    'Class Schedule': 'Mon 09:00-09:50',
                    'Classroom': 'T4-102/T4-103',
                },
                {
                    'Course Code': 'COMP1003',
                    'Course Title & Session': 'Data Structures (1001)',
                    'Teachers': 'Dr. Three',
                    'Class Schedule': 'Mon 10:00-10:50',
                    'Classroom': 'Nil',
                },
                {
                    'Course Code': 'COMP1004',
                    'Course Title & Session': 'Outdoor Activity (1001)',
                    'Teachers': 'Coach',
                    'Class Schedule': 'Mon 10:00-10:50',
                    'Classroom': 'Central Lake (gathering spot: front of CC-128)',
                },
            ]
        )

        with mock.patch.object(app_module, 'get_df', return_value=sample_df):
            morning = self.client.get('/api/free-classrooms?day=Mon&start=08:00&end=08:50')
            later = self.client.get('/api/free-classrooms?day=Mon&start=09:00&end=09:50')

        self.assertEqual(morning.status_code, 200)
        morning_data = morning.get_json()
        morning_rooms = {room['room']: room for room in morning_data['rooms']}
        self.assertNotIn('T4-101', morning_rooms)
        self.assertIn('T4-102', morning_rooms)
        self.assertIn('T4-103', morning_rooms)
        self.assertEqual(morning_rooms['T4-102']['next_busy']['start'], '09:00')
        self.assertEqual(morning_data['summary']['total_rooms'], 3)

        self.assertEqual(later.status_code, 200)
        later_data = later.get_json()
        later_rooms = {room['room'] for room in later_data['rooms']}
        self.assertIn('T4-101', later_rooms)
        self.assertNotIn('T4-102', later_rooms)
        self.assertNotIn('T4-103', later_rooms)

    def test_free_classrooms_buildings_follow_custom_display_order(self):
        sample_df = app_module.pd.DataFrame(
            [
                {
                    'Course Code': 'COMP2001',
                    'Course Title & Session': 'Algo (1001)',
                    'Teachers': 'Dr. T',
                    'Class Schedule': 'Mon 10:00-10:50',
                    'Classroom': 'T8-201',
                },
                {
                    'Course Code': 'COMP2002',
                    'Course Title & Session': 'Algo (1002)',
                    'Teachers': 'Dr. T',
                    'Class Schedule': 'Mon 10:00-10:50',
                    'Classroom': 'T6-301',
                },
                {
                    'Course Code': 'COMP2003',
                    'Course Title & Session': 'Algo (1003)',
                    'Teachers': 'Dr. T',
                    'Class Schedule': 'Mon 10:00-10:50',
                    'Classroom': 'T4-401',
                },
                {
                    'Course Code': 'COMP2004',
                    'Course Title & Session': 'Algo (1004)',
                    'Teachers': 'Dr. T',
                    'Class Schedule': 'Mon 10:00-10:50',
                    'Classroom': 'T29-101',
                },
                {
                    'Course Code': 'COMP2005',
                    'Course Title & Session': 'Algo (1005)',
                    'Teachers': 'Dr. T',
                    'Class Schedule': 'Mon 10:00-10:50',
                    'Classroom': 'T11-101',
                },
                {
                    'Course Code': 'COMP2006',
                    'Course Title & Session': 'Algo (1006)',
                    'Teachers': 'Dr. T',
                    'Class Schedule': 'Mon 10:00-10:50',
                    'Classroom': 'A3-201',
                },
                {
                    'Course Code': 'COMP2007',
                    'Course Title & Session': 'Algo (1007)',
                    'Teachers': 'Dr. T',
                    'Class Schedule': 'Mon 10:00-10:50',
                    'Classroom': 'CC-128',
                },
                {
                    'Course Code': 'COMP2008',
                    'Course Title & Session': 'Algo (1008)',
                    'Teachers': 'Dr. T',
                    'Class Schedule': 'Mon 10:00-10:50',
                    'Classroom': 'V20-101/UC-201/SP-301/V22-101',
                },
            ]
        )

        with mock.patch.object(app_module, 'get_df', return_value=sample_df):
            response = self.client.get('/api/free-classrooms?day=Mon&start=08:00&end=08:50')

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(
            [item['building'] for item in data['buildings']],
            ['T8', 'T6', 'T4', 'T29', 'A3', 'T11', 'CC'],
        )
        self.assertEqual(
            [room['building'] for room in data['rooms']],
            ['T8', 'T6', 'T4', 'T29', 'A3', 'T11', 'CC'],
        )
        self.assertNotIn('V20', [item['building'] for item in data['buildings']])
        self.assertNotIn('UC', [item['building'] for item in data['buildings']])
        self.assertNotIn('SP', [item['building'] for item in data['buildings']])
        self.assertNotIn('V22', [item['building'] for item in data['buildings']])


if __name__ == '__main__':
    unittest.main()
