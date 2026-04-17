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

    def insert_user(self, username, password_hash=None, display_name=None):
        with sqlite3.connect(app_module.DB_PATH) as conn:
            conn.execute(
                '''
                INSERT INTO users (username, password_hash, display_name)
                VALUES (?, ?, ?)
                ''',
                (username, password_hash, display_name),
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
        self.insert_user('sync-user', password_hash=app_module.generate_password_hash('pw'))

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
