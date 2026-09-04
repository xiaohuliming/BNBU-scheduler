import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

os.environ.setdefault('MAXCOURSE_SECRET_KEY', 'test-secret-key')

import app as app_module
import campus_agent


class CampusClassroomTest(unittest.TestCase):
    UA = {'User-Agent': 'Mozilla/5.0 CampusClassroomTest'}
    FREE = '/api/knowledge/classrooms/free'
    SCHEDULE = '/api/knowledge/classrooms/schedule'

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original_db = app_module.DB_PATH
        self.original_testing = app_module.app.testing
        self.original_cache = app_module._classroom_index_cache
        app_module.DB_PATH = str(Path(self.temp.name) / 'users.db')
        app_module.init_db()
        app_module.app.testing = True
        campus_agent._ip_buckets.clear()
        self.client = app_module.app.test_client()
        with app_module.get_db() as conn:
            self.uid = conn.execute("INSERT INTO users(username) VALUES ('private-owner')").lastrowid
            self.other_uid = conn.execute("INSERT INTO users(username) VALUES ('private-other')").lastrowid
        with self.client.session_transaction() as session:
            session['user_id'] = self.uid
        response = self.client.post('/api/knowledge/tokens', json={'name': 'classroom-qa'}, headers=self.UA)
        self.key = response.get_json()
        self.headers = {'User-Agent': 'python-httpx/0.28', 'Authorization': 'Bearer ' + self.key['token']}
        rows = [
            ('T8-101', 'Mon 09:00-09:50'), ('T8-101', 'Fri 09:00-10:00'),
            ('T8-101', 'Fri 12:00-13:00'), ('T8-102', 'Fri 10:00-11:00'),
            ('T8-103', 'Fri 11:05-12:00'), ('T4-201', 'Fri 14:00-15:00'),
            ('V22-101', 'Fri 15:00-16:00'),
        ]
        frame = app_module.pd.DataFrame([
            {'Course Code': 'TEST1003', 'Course Title & Session': 'Test (1001)',
             'Teachers': 'Teacher', 'Classroom': room, 'Class Schedule': schedule}
            for room, schedule in rows
        ])
        self.df_patch = mock.patch.object(app_module, 'get_df', return_value=frame)
        self.now_patch = mock.patch.object(app_module, '_classroom_now',
            return_value=datetime(2026, 9, 4, 10, 5, tzinfo=app_module.BEIJING_TZ))
        self.df_patch.start()
        self.clock = self.now_patch.start()

    def tearDown(self):
        self.df_patch.stop()
        self.now_patch.stop()
        app_module.DB_PATH = self.original_db
        app_module.app.testing = self.original_testing
        app_module._classroom_index_cache = self.original_cache
        campus_agent._ip_buckets.clear()
        self.temp.cleanup()

    def query(self, body=None, path=None):
        return self.client.post(path or self.FREE, json={} if body is None else body, headers=self.headers)

    def intent(self, room='T8-101', status='planned', start=600, end=660, size=2):
        with app_module.get_db() as conn:
            return conn.execute('''INSERT INTO classroom_intents
                (user_id,room,use_date,start_min,end_min,purpose,party_size,status)
                VALUES (?,?,?,?,?,?,?,?)''',
                (self.uid, room, '2026-09-04', start, end, 'study', size, status)).lastrowid

    def test_now_defaults_and_source_caveats(self):
        response = self.query()
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual((data['query']['date'], data['query']['day']), ('2026-09-04', 'Fri'))
        self.assertEqual((data['query']['start'], data['query']['end']), ('10:05', '11:05'))
        self.assertEqual(data['query']['state'], 'ongoing')
        self.assertEqual(data['timezone'], 'Asia/Shanghai')
        self.assertEqual(data['as_of'], '2026-09-04T10:05:00+08:00')
        self.assertEqual(data['physical_occupancy'], 'unknown')
        self.assertIn('临时调课', data['notice'])
        self.assertEqual([r['room'] for r in data['rooms']], ['T8-101', 'T8-103', 'T4-201'])
        self.assertEqual(data['rooms'][0]['free_until'], '12:00')
        self.assertNotIn('registration_open', data['query'])
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertIn('Authorization', response.headers['Vary'])

    def test_utc_day_rollover_and_midnight_end(self):
        self.clock.return_value = datetime(2026, 9, 4, 16, 1, tzinfo=timezone.utc)
        data = self.query().get_json()
        self.assertEqual((data['query']['date'], data['query']['day']), ('2026-09-05', 'Sat'))
        self.assertEqual(data['query']['start'], '00:01')
        self.clock.return_value = datetime(2026, 9, 4, 23, 59, 59, tzinfo=app_module.BEIJING_TZ)
        data = self.query().get_json()
        self.assertEqual(data['query']['date'], '2026-09-04')
        self.assertEqual(data['query']['end'], '24:00')
        self.assertIsNone(data['rooms'][0]['free_until'])
        self.assertTrue(data['rooms'][0]['no_later_class_in_timetable'])

    def test_future_weekday_is_derived_from_date_and_no_four_hour_limit(self):
        data = self.query({'date': '2026-09-11', 'start': '08:00', 'end': '13:50'}).get_json()
        self.assertEqual(data['query']['day'], 'Fri')
        self.assertEqual(data['query']['state'], 'future')
        self.assertEqual([r['room'] for r in data['rooms']], ['T4-201'])
        self.assertEqual(self.query({'date': '2026-09-18', 'start': '08:00'}).status_code, 200)
        self.assertEqual(self.query({'date': '2026-09-19', 'start': '08:00'}).status_code, 400)

    def test_exact_room_filter_and_exclusive_end_boundary(self):
        data = self.query({'room': 't8-103'}).get_json()
        self.assertEqual(data['total'], 1)  # Class begins exactly at query end.
        self.assertEqual(self.query({'room': 'T8-102'}).get_json()['total'], 0)
        data = self.query({'room': 'T8-102', 'start': '11:00', 'end': '11:05'}).get_json()
        self.assertEqual(data['total'], 1)  # Previous class ends at query start.
        self.assertEqual(self.query({'room': 'T8-999'}).status_code, 404)
        self.assertEqual(self.query({'building': 'T999'}).status_code, 404)
        self.assertEqual(self.query({'room': 'V22-101'}).status_code, 404)
        self.assertEqual(self.query({'room': 'T8-101', 'building': 'T4'}).status_code, 400)

    def test_building_filters_and_pagination_are_explicit(self):
        first = self.query({'building': 't8', 'limit': 1}).get_json()
        second = self.query({'building': 'T8', 'limit': 1, 'offset': first['next_offset']}).get_json()
        self.assertEqual(first['total'], 2)
        self.assertEqual(first['returned'], 1)
        self.assertNotEqual(first['rooms'][0]['room'], second['rooms'][0]['room'])
        self.assertIsNone(second['next_offset'])
        empty = self.query({'offset': 99}).get_json()
        self.assertEqual(empty['rooms'], [])
        self.assertEqual(empty['total'], 3)

    def test_live_intent_changes_are_fresh_anonymous_and_never_write_records(self):
        key = self.intent()
        data = self.query({'room': 'T8-101'}).get_json()
        self.assertEqual(data['rooms'][0]['intent']['planned_people'], 2)
        self.assertEqual(set(data['rooms'][0]['intent']),
                         {'records', 'people', 'planned_people', 'checked_in_people'})
        self.assertNotIn('private-owner', json.dumps(data))
        self.assertNotIn('user_id', json.dumps(data))
        self.assertNotIn('"my"', json.dumps(data))
        self.assertEqual(self.query({'exclude_intents': True, 'room': 'T8-101'}).get_json()['total'], 0)
        with app_module.get_db() as conn:
            conn.execute("UPDATE classroom_intents SET status='checked_in' WHERE id=?", (key,))
        updated = self.query({'room': 'T8-101'}).get_json()
        self.assertEqual(updated['rooms'][0]['intent']['checked_in_people'], 2)
        self.assertEqual(updated['rooms'][0]['intent']['planned_people'], 0)
        with app_module.get_db() as conn:
            conn.execute("UPDATE classroom_intents SET status='ended' WHERE id=?", (key,))
            before = [tuple(row) for row in conn.execute('SELECT * FROM classroom_intents')]
        self.assertEqual(self.query({'room': 'T8-101'}).get_json()['rooms'][0]['intent']['people'], 0)
        with app_module.get_db() as conn:
            self.assertEqual([tuple(row) for row in conn.execute('SELECT * FROM classroom_intents')], before)

    def test_stale_intents_expire_in_read_without_mutation(self):
        self.intent(start=585)  # 09:45 plan is over the 15-minute grace.
        self.intent(status='checked_in', end=605)  # Ends exactly at now.
        self.intent(status='cancelled')
        self.assertEqual(self.query({'room': 'T8-101'}).get_json()['rooms'][0]['intent']['people'], 0)
        with app_module.get_db() as conn:
            states = [row[0] for row in conn.execute('SELECT status FROM classroom_intents ORDER BY id')]
        self.assertEqual(states, ['planned', 'checked_in', 'cancelled'])

    def test_existing_browser_results_are_preserved(self):
        self.intent()
        agent = self.query().get_json()
        web = self.client.get('/api/free-classrooms?date=2026-09-04&day=Fri&start=10:05&end=11:05',
                              headers=self.UA).get_json()
        self.assertEqual([r['room'] for r in agent['rooms']], [r['room'] for r in web['rooms']])
        self.assertIsNotNone(web['rooms'][0]['intent']['my'])
        for actual, expected in zip(agent['rooms'], web['rooms']):
            self.assertEqual(actual['next_busy'], expected['next_busy'])
            self.assertEqual(actual['intent']['people'], expected['intent']['people'])

    def test_weekly_schedule_matches_website_and_labels_snapshot(self):
        response = self.query({'room': 't8-101'}, self.SCHEDULE)
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        web = self.client.get('/api/classroom/T8-101/schedule', headers=self.UA).get_json()
        self.assertEqual(data['days'], web['days'])
        self.assertEqual(len(data['days']), 7)
        self.assertEqual(data['timetable_basis'], 'current_semester_weekly_snapshot')
        self.assertEqual(data['intent_basis'], 'not_included')
        self.assertEqual(self.query({'room': 'T8-999'}, self.SCHEDULE).status_code, 404)

    def test_invalid_arguments_and_elapsed_windows_are_rejected(self):
        invalid = [
            {'date': '2026-09-03'}, {'date': '2026-9-4'}, {'date': 'not-a-date'},
            {'date': '2026-09-05'}, {'start': '9:00'}, {'start': '24:00'}, {'end': '24:01'},
            {'start': '12:00', 'end': '11:00'}, {'start': '09:00', 'end': '10:00'},
            {'start': None}, {'date': 20260904}, {'limit': True}, {'offset': -1},
            {'limit': 51}, {'exclude_intents': 'false'}, {'room': '../../maxcourse.db'},
            {'building': "T8' OR 1=1"}, {'room': []}, {'url': 'http://127.0.0.1'},
            {'day': 'Mon'}, {'user_id': self.uid},
        ]
        for body in invalid:
            with self.subTest(body=body):
                self.assertEqual(self.query(body).status_code, 400)
        self.assertEqual(self.query({}, self.SCHEDULE).status_code, 400)

    def test_auth_revocation_quota_and_original_write_boundaries(self):
        agent = app_module.app.test_client()
        for path in (self.FREE, self.SCHEDULE):
            self.assertEqual(self.client.post(path, json={}, headers=self.UA).status_code, 401)
        self.assertEqual(agent.get('/api/free-classrooms', headers=self.headers).status_code, 403)
        self.assertEqual(agent.post('/api/classroom-intents', json={}, headers=self.headers).status_code, 403)
        browser_ua_token = dict(self.headers, **self.UA)
        self.assertEqual(agent.post('/api/classroom-intents', json={}, headers=browser_ua_token).status_code, 401)
        self.assertEqual(agent.post('/api/classroom-intents/1/check-in', headers=browser_ua_token).status_code, 401)
        self.assertEqual(agent.delete('/api/classroom-intents/1', headers=browser_ua_token).status_code, 401)
        self.assertEqual(agent.post(self.FREE, json={}, headers=dict(self.headers, Origin='https://evil.example')).status_code, 403)
        self.assertEqual(agent.post(self.FREE, json={}, headers=self.headers).status_code, 200)
        with app_module.get_db() as conn:
            now = int(time.time())
            conn.execute('UPDATE campus_agent_usage SET minute=?, minute_count=? WHERE user_id=?',
                         (now // 60, campus_agent.READ_PER_MINUTE, self.uid))
        self.assertEqual(self.query().status_code, 429)
        self.client.delete('/api/knowledge/tokens/' + self.key['id'], headers=self.UA)
        self.assertEqual(self.query().status_code, 401)

    def test_missing_document_index_does_not_block_live_queries(self):
        with mock.patch.object(app_module.app.extensions['campus_knowledge'], 'search',
                               side_effect=AssertionError('Live queries must not search indexed snapshots')):
            self.assertEqual(self.query().status_code, 200)

    def test_concurrency_limit_is_retained(self):
        for _ in range(3):
            self.assertTrue(campus_agent._search_slots.acquire(blocking=False))
        try:
            self.assertEqual(self.query().status_code, 503)
        finally:
            for _ in range(3):
                campus_agent._search_slots.release()

    def test_mcp_and_openapi_expose_only_read_tools_with_live_metadata(self):
        headers = dict(self.headers, Accept='application/json, text/event-stream')
        body = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                'params': {'name': 'find_free_classrooms', 'arguments': {'room': 'T8-101'}}}
        result = self.client.post('/mcp', json=body, headers=headers).get_json()['result']
        self.assertFalse(result['isError'])
        self.assertEqual(json.loads(result['content'][0]['text']), result['structuredContent'])
        self.assertEqual(result['structuredContent']['rooms'][0]['room'], 'T8-101')
        body['params'] = {'name': 'get_classroom_schedule', 'arguments': {'room': 'T8-101'}}
        self.assertFalse(self.client.post('/mcp', json=body, headers=headers).get_json()['result']['isError'])
        body['params'] = {'name': 'create_classroom_intent', 'arguments': {}}
        self.assertEqual(self.client.post('/mcp', json=body, headers=headers).get_json()['error']['code'], -32602)
        spec = self.client.get('/api/knowledge/openapi.json').get_json()
        self.assertEqual(spec['paths'][self.FREE]['post']['operationId'], 'find_free_classrooms')
        self.assertIn({'campusToken': []}, spec['paths'][self.SCHEDULE]['post']['security'])
        info = self.client.get('/api/knowledge/info').get_json()
        self.assertIn('find_free_classrooms', info['live_tools'])


if __name__ == '__main__':
    unittest.main()
