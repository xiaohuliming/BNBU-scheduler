import hashlib
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault('MAXCOURSE_SECRET_KEY', 'test-secret-key')

import app as app_module
import campus_agent
from build_campus_knowledge import write_index
from campus_knowledge import INDEX_NAME, KnowledgeIndex, query_tokens, sha256_file


class CampusKnowledgeTest(unittest.TestCase):
    UA = {'User-Agent': 'Mozilla/5.0 CampusKnowledgeTest'}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original_db = app_module.DB_PATH
        self.original_index = app_module.app.extensions['campus_knowledge']
        self.original_testing = app_module.app.testing
        app_module.DB_PATH = str(Path(self.temp.name) / 'users.db')
        app_module.init_db()
        app_module.app.testing = True
        campus_agent._ip_buckets.clear()
        self.client = app_module.app.test_client()
        with app_module.get_db() as conn:
            self.uid = conn.execute("INSERT INTO users(username) VALUES ('knowledge-test')").lastrowid
            self.other_uid = conn.execute("INSERT INTO users(username) VALUES ('knowledge-other')").lastrowid
        with self.client.session_transaction() as session:
            session['user_id'] = self.uid
        records = [
            {'id': 'office:AI', 'title': '计算机科学系办公室 AI CST', 'kind': 'office', 'programmes': ['AI', 'CST'], 'chunks': [(None, '计算机科学系 DCS 办公室 T3-602-R12')], 'source_url': 'https://fst.bnbu.edu.cn/en/about_us/contact_us.htm'},
            {'id': 'handbook:AI:2025', 'title': 'AI Artificial Intelligence 2025 培养手册', 'kind': 'handbook', 'cohort': '2025', 'programmes': ['AI'], 'chunks': [(1, 'AI1003 Python Programming 3 units'), (2, 'Major electives 21 units'), (2, 'Check original table')], 'source_url': 'https://ar.bnbu.edu.cn/example.pdf'},
            {'id': 'handbook:AI:2026', 'title': 'AI Artificial Intelligence 2026 培养手册', 'kind': 'handbook', 'cohort': '2026', 'programmes': ['AI'], 'chunks': [(1, 'Major Required Courses AI3013 Machine Learning')], 'source_url': 'https://ar.bnbu.edu.cn/example2026.pdf'},
            {'id': 'handbook:AIM:2026', 'title': 'AIM Animation 2026', 'kind': 'handbook', 'cohort': '2026', 'programmes': ['AIM'], 'chunks': [(1, 'Animation and Interactive Media')], 'source_url': 'https://ar.bnbu.edu.cn/aim.pdf'},
            {'id': 'course:2627S1:AI3013', 'title': 'AI3013 Machine Learning', 'kind': 'course', 'semester': '2627S1', 'programmes': ['AI'], 'meeting_rows': 2, 'chunks': [(None, 'Machine Learning 机器学习'), (None, 'Session 1001 Mon 08:00-09:50 T8-303 teacher A'), (None, 'Session 1001 Fri 08:00-08:50 T8-305 teacher A')], 'source_url': '/docs/course.pdf'},
            {'id': 'course:2627S1:AI4004', 'title': 'AI4004 Final Year Project', 'kind': 'course', 'semester': '2627S1', 'programmes': ['AI'], 'chunks': [(None, 'Prerequisite AI3013 AI2003')], 'source_url': '/docs/course.pdf'},
        ]
        for record in records:
            record.setdefault('cohort', '')
            record.setdefault('semester', '')
        path = Path(self.temp.name) / 'fixture.sqlite'
        write_index(path, records, {'built_at': '2026-09-04', 'retrieval': 'BM25'})
        self.index = KnowledgeIndex(path)
        app_module.app.extensions['campus_knowledge'] = self.index

    def tearDown(self):
        app_module.DB_PATH = self.original_db
        app_module.app.extensions['campus_knowledge'] = self.original_index
        app_module.app.testing = self.original_testing
        campus_agent._ip_buckets.clear()
        self.temp.cleanup()

    def key(self):
        response = self.client.post('/api/knowledge/tokens', json={'name': 'test agent'}, headers=self.UA)
        self.assertEqual(response.status_code, 201)
        data = response.get_json()
        return data, {'User-Agent': 'python-httpx/0.28', 'Authorization': 'Bearer ' + data['token']}

    def mcp(self, headers, method, params=None, message_id=1):
        return self.client.post('/mcp', json={'jsonrpc': '2.0', 'id': message_id, 'method': method, 'params': params or {}},
                                headers=dict(headers, Accept='application/json, text/event-stream'))

    def test_public_info_contains_counts_not_credentials(self):
        response = self.client.get('/api/knowledge/info', headers={'User-Agent': 'httpx'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['counts']['course'], 2)
        self.assertNotIn('local_sources', response.get_json())
        self.assertEqual(response.headers['Cache-Control'], 'no-store')

    def test_keys_are_hashed_scoped_and_only_disclosed_once(self):
        key, headers = self.key()
        response = self.client.get('/api/knowledge/tokens', headers=self.UA)
        self.assertNotIn(key['token'], response.get_data(as_text=True))
        with app_module.get_db() as conn:
            row = conn.execute('SELECT * FROM campus_agent_keys WHERE id=?', (key['id'],)).fetchone()
            self.assertEqual(row['token_hash'], hashlib.sha256(key['token'].encode()).hexdigest())
            self.assertNotIn(key['token'], str(tuple(row)))
        self.assertEqual(self.client.post('/api/knowledge/search', json={'query': 'AI3013'}, headers=headers).status_code, 200)
        self.assertEqual(self.client.get('/api/courses', headers=headers).status_code, 403)

    def test_browser_session_does_not_grant_agent_access(self):
        self.assertEqual(self.client.post('/api/knowledge/search', json={'query': 'AI'}, headers=self.UA).status_code, 401)
        self.assertEqual(self.client.post('/mcp', json={}, headers=self.UA).status_code, 401)

    def test_bearer_cannot_create_keys(self):
        _, headers = self.key()
        self.client = app_module.app.test_client()
        headers['User-Agent'] = self.UA['User-Agent']
        response = self.client.post('/api/knowledge/tokens', json={'name': 'unauthorized'}, headers=headers)
        self.assertEqual(response.status_code, 401)

    def test_key_revocation_is_immediate(self):
        key, headers = self.key()
        response = self.client.delete('/api/knowledge/tokens/' + key['id'], headers=self.UA)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.post('/api/knowledge/search', json={'query': 'AI'}, headers=headers).status_code, 401)

    def test_cannot_revoke_another_users_key(self):
        key, _ = self.key()
        with self.client.session_transaction() as session:
            session['user_id'] = self.other_uid
        self.assertEqual(self.client.delete('/api/knowledge/tokens/' + key['id'], headers=self.UA).status_code, 404)

    def test_expired_key_is_rejected(self):
        key, headers = self.key()
        with app_module.get_db() as conn:
            conn.execute('UPDATE campus_agent_keys SET expires_at=0 WHERE id=?', (key['id'],))
        self.assertEqual(self.client.post('/api/knowledge/search', json={'query': 'AI'}, headers=headers).status_code, 401)

    def test_key_count_cap_and_origin_check(self):
        for _ in range(3): self.key()
        self.assertEqual(self.client.post('/api/knowledge/tokens', json={'name': 'fourth'}, headers=self.UA).status_code, 409)
        self.assertEqual(self.client.post('/api/knowledge/tokens', json={'name': 'attack'}, headers=dict(self.UA, Origin='https://evil.example')).status_code, 403)

    def test_valid_key_still_rejects_untrusted_origin(self):
        _, headers = self.key()
        headers['Origin'] = 'https://evil.example'
        response = self.client.post('/api/knowledge/search', json={'query': 'AI'}, headers=headers)
        self.assertEqual(response.status_code, 403)
        self.assertNotIn('Access-Control-Allow-Origin', response.headers)

    def test_cors_preflight_is_origin_scoped_and_does_not_grant_read_access(self):
        headers = {'Origin': 'https://www.bnbscheduler.top', 'Access-Control-Request-Method': 'POST',
                   'Access-Control-Request-Headers': 'authorization,content-type'}
        response = self.client.options('/mcp', headers=headers)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.headers['Access-Control-Allow-Origin'], headers['Origin'])
        self.assertNotIn('Access-Control-Allow-Credentials', response.headers)
        self.assertEqual(self.client.post('/mcp', json={}, headers=headers).status_code, 401)
        headers['Origin'] = 'https://evil.example'
        self.assertEqual(self.client.options('/mcp', headers=headers).status_code, 403)

    def test_quotas_are_shared_by_all_keys_for_account(self):
        _, first = self.key()
        _, second = self.key()
        now = int(time.time())
        with app_module.get_db() as conn:
            conn.execute('INSERT INTO campus_agent_usage VALUES (?, ?, ?, ?, ?)',
                         (self.uid, now // 60, campus_agent.READ_PER_MINUTE, now // 86400, 0))
        for headers in (first, second):
            response = self.client.post('/api/knowledge/search', json={'query': 'AI'}, headers=headers)
            self.assertEqual(response.status_code, 429)
            self.assertIn('Retry-After', response.headers)

    def test_daily_quota_persists_independently_of_minute(self):
        _, headers = self.key()
        now = int(time.time())
        with app_module.get_db() as conn:
            conn.execute('INSERT INTO campus_agent_usage VALUES (?, ?, ?, ?, ?)',
                         (self.uid, now // 60 - 1, 0, now // 86400, campus_agent.READ_PER_DAY))
        self.assertEqual(self.client.post('/api/knowledge/search', json={'query': 'AI'}, headers=headers).status_code, 429)

    def test_programme_cohort_filters_and_exact_code(self):
        data = self.index.search('AI 培养手册', kind='handbook', programme='AI', cohort='2026')
        self.assertEqual([row['id'] for row in data['results']], ['handbook:AI:2026'])
        data = self.index.search('AI 2025级培养手册', kind='handbook')
        self.assertEqual([row['id'] for row in data['results']], ['handbook:AI:2025'])
        data = self.index.search('AI3013 上课时间', kind='course')
        self.assertEqual([row['id'] for row in data['results']], ['course:2627S1:AI3013'])
        self.assertEqual(self.index.search('AI2003上课时间', kind='course')['results'], [])
        self.assertIn('machine', query_tokens('机器学习'))

    def test_pagination_uses_global_chunk_ordinals_even_with_page_filter(self):
        data = self.index.read('handbook:AI:2025', page=2, limit=1)
        self.assertEqual(data['chunks'][0]['ordinal'], 1)
        self.assertEqual(data['next_offset'], 2)
        self.assertEqual(self.index.read('handbook:AI:2025', page=2, offset=2)['chunks'][0]['text'], 'Check original table')

    def test_listing_is_exhaustive_and_does_not_mix_ai_with_aim(self):
        first = self.index.list_documents(kind='handbook', programme='AI', limit=1)
        second = self.index.list_documents(kind='handbook', programme='AI', limit=1, offset=first['next_offset'])
        self.assertEqual(first['total'], 2)
        self.assertIsNone(second['next_offset'])
        self.assertNotEqual(first['documents'][0]['id'], second['documents'][0]['id'])

    def test_unsafe_or_oversized_queries_never_execute_paths_or_sql(self):
        _, headers = self.key()
        for body in ({'query': []}, {'query': 'x' * 301}, {'query': 'AI', 'limit': True}, {'query': 'AI', 'programme': "AI' OR 1=1"}, {'query': 'AI', 'url': 'http://127.0.0.1'}):
            self.assertEqual(self.client.post('/api/knowledge/search', json=body, headers=headers).status_code, 400)
        self.assertEqual(self.client.post('/api/knowledge/read', json={'document_id': '../../maxcourse.db'}, headers=headers).status_code, 400)
        self.assertEqual(self.client.post('/api/knowledge/search', data='not json', content_type='application/json', headers=headers).status_code, 400)
        self.assertEqual(self.client.post('/api/knowledge/search', data='x' * 33000, content_type='application/json', headers=headers).status_code, 400)

    def test_citations_are_absolute_and_resolve_pages(self):
        _, headers = self.key()
        result = self.client.post('/api/knowledge/read', json={'document_id': 'course:2627S1:AI3013'}, headers=headers).get_json()
        self.assertTrue(result['source_url'].startswith('https://www.bnbscheduler.top/'))
        result = self.client.post('/api/knowledge/read', json={'document_id': 'handbook:AI:2025', 'page': 2}, headers=headers).get_json()
        self.assertTrue(result['chunks'][0]['citation_url'].endswith('#page=2'))

    def test_mcp_handshake_tools_call_and_notification(self):
        _, headers = self.key()
        response = self.mcp(headers, 'initialize', {'protocolVersion': '2025-11-25', 'clientInfo': {'name': 'test', 'version': '1'}, 'capabilities': {}})
        self.assertEqual(response.get_json()['result']['protocolVersion'], '2025-11-25')
        response = self.mcp(headers, 'tools/list')
        self.assertEqual(len(response.get_json()['result']['tools']), 3)
        response = self.mcp(headers, 'tools/call', {'name': 'search_campus', 'arguments': {'query': 'AI3013'}})
        self.assertFalse(response.get_json()['result']['isError'])
        self.assertTrue(response.get_json()['result']['structuredContent']['results'])
        response = self.client.post('/mcp', json={'jsonrpc': '2.0', 'method': 'notifications/initialized'}, headers=dict(headers, Accept='application/json, text/event-stream'))
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data, b'')

    def test_mcp_error_contract(self):
        _, headers = self.key()
        self.assertEqual(self.mcp(headers, 'unknown').get_json()['error']['code'], -32601)
        data = self.mcp(headers, 'tools/call', {'name': 'read_document', 'arguments': {'document_id': 'unknown'}}).get_json()
        self.assertTrue(data['result']['isError'])
        self.assertEqual(self.client.get('/mcp', headers=headers).status_code, 405)
        self.assertEqual(self.client.post('/mcp', json={}, headers=headers).status_code, 406)
        response = self.client.post('/mcp', json=[], headers=dict(headers, Accept='application/json, text/event-stream'))
        self.assertEqual(response.status_code, 400)

    def test_openapi_contract_and_static_source_protection(self):
        response = self.client.get('/api/knowledge/openapi.json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.get_json()['paths']), 3)
        for path in ('/campus_knowledge.sqlite', '/campus_knowledge.building.sqlite', '/campus_agent.py', '/build_campus_knowledge.py'):
            self.assertEqual(self.client.get(path).status_code, 404)


class CampusCorpusTest(unittest.TestCase):
    def test_index_tracks_all_current_courses_and_exact_local_sources(self):
        root = Path(app_module.APP_ROOT)
        index = KnowledgeIndex(root / INDEX_NAME)
        metadata = index.metadata()
        from maximize_credits import load_timetable
        path = next(root.glob('*Course List*.xlsx'))
        frame = load_timetable(str(path))
        self.assertEqual(metadata['counts']['course'], frame['Course Code'].nunique())
        self.assertEqual(metadata['meeting_rows'], len(frame))
        for name, digest in metadata['local_sources'].items():
            self.assertEqual(sha256_file(root / name), digest, 'Rebuild campus knowledge after changing ' + name)
        self.assertEqual(metadata['cohorts'], ['2023', '2024', '2025', '2026'])
        self.assertEqual(metadata['semesters'], [f'{app_module.CURRENT_SEMESTER_AY_START % 100:02d}{(app_module.CURRENT_SEMESTER_AY_START + 1) % 100:02d}S{app_module.CURRENT_SEMESTER_NO}'])
        self.assertGreaterEqual(metadata['counts']['handbook'], 120)
        conn = index.connect()
        try:
            codes = {json.loads(row['metadata'])['course_code'] for row in conn.execute("SELECT metadata FROM documents WHERE kind='course'")}
            self.assertEqual(codes, set(frame['Course Code']))
            for row in conn.execute("SELECT metadata FROM documents WHERE kind='handbook'"):
                self.assertTrue(json.loads(row['metadata'])['source_url'].startswith('https://ar.bnbu.edu.cn/'))
        finally:
            conn.close()


if __name__ == '__main__':
    unittest.main()
