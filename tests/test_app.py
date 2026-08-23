import io
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
            cursor = conn.execute(
                '''
                INSERT INTO users (username, password_hash, display_name, ispace_username)
                VALUES (?, ?, ?, ?)
                ''',
                (username, password_hash, display_name, ispace_username),
            )
            conn.commit()
            return cursor.lastrowid

    def test_password_login_returns_401_for_ispace_only_account(self):
        self.insert_user('shadow-user', password_hash=None)

        response = self.client.post(
            '/api/login',
            json={'username': 'shadow-user', 'password': 'anything'},
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()['error'], 'Invalid credentials')

    def test_registration_rejects_oversized_credentials(self):
        response = self.client.post(
            '/api/register',
            json={'username': 'u' * 121, 'password': 'valid-password'},
            headers={'User-Agent': self.BROWSER_UA},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('too long', response.get_json()['error'].lower())

    def test_login_rejects_oversized_credentials(self):
        response = self.client.post(
            '/api/login',
            json={'username': 'u' * 121, 'password': 'guess'},
            headers={'User-Agent': self.BROWSER_UA},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('too long', response.get_json()['error'].lower())

    def test_ispace_login_rejects_oversized_credentials_before_remote_auth(self):
        with mock.patch.object(
            app_module,
            'fetch_timeline',
            return_value={'error': 'remote auth should not run'},
        ):
            response = self.client.post(
                '/api/login/ispace',
                json={'username': '2' * 121, 'password': 'guess'},
                headers={'User-Agent': self.BROWSER_UA},
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn('too long', response.get_json()['error'].lower())

    def test_unknown_api_route_returns_json_error(self):
        response = self.client.get('/api/not-a-real-endpoint')

        self.assertEqual(response.status_code, 404)
        self.assertTrue(response.is_json)
        self.assertEqual(response.get_json()['status'], 404)

    def test_sensitive_project_files_are_not_public_static_assets(self):
        for path in ('/.git/HEAD', '/app.py', '/maxcourse.db'):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 404)

    def test_oversized_api_request_is_rejected_before_handler_work(self):
        response = self.client.post(
            '/api/parse-transcript',
            data={'file': (io.BytesIO(b'x' * (17 * 1024 * 1024)), 'large.pdf')},
            headers={'User-Agent': self.BROWSER_UA},
            content_type='multipart/form-data',
        )

        self.assertEqual(response.status_code, 413)
        self.assertTrue(response.is_json)

    def test_optimize_rejects_unreasonable_course_count(self):
        response = self.client.post(
            '/api/optimize',
            json={'codes': [f'ZZ{i:04d}' for i in range(101)]},
            headers={'User-Agent': self.BROWSER_UA},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('too many', response.get_json()['error'].lower())

    def test_campus_map_page_and_data_are_served(self):
        page = self.client.get('/campus-map/index.html')
        self.assertEqual(page.status_code, 200)
        self.assertIn('校园手绘地图'.encode('utf-8'), page.data)

        data_resp = self.client.get('/campus-map/map_data.json')
        self.assertEqual(data_resp.status_code, 200)
        data = data_resp.get_json()

        node_ids = set((data.get('nodes') or {}).keys())
        building_ids = [b['id'] for b in (data.get('buildings') or [])]
        self.assertEqual(len(building_ids), len(set(building_ids)), 'duplicate building ids')
        for edge in (data.get('edges') or []):
            # [a, b], [a, b, meters], or [a, b, meters, 'path'] (meters is a
            # real-world override for zones the hand-drawn map does not draw
            # to scale; the 'path' tag marks small footpaths for the route UI)
            self.assertIn(len(edge), (2, 3, 4), f'malformed edge {edge}')
            a, b = edge[0], edge[1]
            self.assertIn(a, node_ids)
            self.assertIn(b, node_ids)
            if len(edge) >= 3:
                self.assertIsInstance(edge[2], (int, float))
                self.assertGreater(edge[2], 0)
            if len(edge) == 4:
                self.assertEqual(edge[3], 'path')
        for bld in (data.get('buildings') or []):
            for node_id in (bld.get('nodes') or []):
                self.assertIn(node_id, node_ids,
                              f"building {bld['id']} references unknown node {node_id}")

        # DATA_FORMAT.md is repo-only documentation, never public (.md is blocked)
        self.assertEqual(self.client.get('/campus-map/DATA_FORMAT.md').status_code, 404)

    def test_media_proxy_content_disposition_is_ascii_safe_for_unicode_filename(self):
        import media_dl.routes as media_routes

        class FakeUpstreamResponse:
            status_code = 206
            headers = {
                'Content-Range': 'bytes 0-2/3',
                'Content-Type': 'video/mp4',
            }

            def iter_content(self, chunk_size=65536):
                yield b'abc'

            def close(self):
                pass

        with mock.patch.object(media_routes, '_fetch_range', return_value=FakeUpstreamResponse()):
            response = self.client.get(
                '/api/media-dl/proxy'
                '?u=https://upos-sz-mirrorcosov.bilivideo.com/video.mp4'
                '&name=我把AI扔进了测试.mp4'
                '&r=https://www.bilibili.com',
                buffered=False,
            )

        disposition = response.headers['Content-Disposition']
        disposition.encode('latin-1')
        self.assertIn('filename="download.mp4"', disposition)
        self.assertIn("filename*=UTF-8''", disposition)

    def test_extract_url_from_text_handles_share_blurbs_and_scheme_less(self):
        from media_dl.extractor import extract_url_from_text

        cases = {
            '8.63 复制打开抖音，看看【xxx】 https://v.douyin.com/abc/ 快来看!':
                'https://v.douyin.com/abc/',
            'www.bilibili.com/video/BV1xx411c7mD':
                'www.bilibili.com/video/BV1xx411c7mD',
            '看这个 https://www.xiaohongshu.com/explore/abc123?xsec_token=AB，很好':
                'https://www.xiaohongshu.com/explore/abc123?xsec_token=AB',
            '   https://youtu.be/dQw4w9WgXcQ   ':
                'https://youtu.be/dQw4w9WgXcQ',
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(extract_url_from_text(text), expected)

    def test_media_proxy_rejects_non_allowlisted_and_pcdn_hosts(self):
        for url in (
            'https://evil.example.com/x.mp4',
            'https://wfm.edge.mountaintoys.cn:4483/x.mp4',   # bili PCDN, not allowlisted
            'https://evil.akamaized.net/x.mp4',              # unrelated Akamai customer
        ):
            with self.subTest(url=url):
                resp = self.client.get('/api/media-dl/proxy?u=' + url)
                self.assertEqual(resp.status_code, 403)

    def test_media_proxy_allowlist_covers_new_platforms(self):
        from media_dl.routes import _host_allowed

        for url in (
            'https://upos-hz-mirrorakam.akamaized.net/x.mp4',  # bili overseas mirror
            'https://xy1.mcdn.bilivideo.cn/x.m4s',
            'https://v16.tiktokcdn-us.com/x.mp4',
            'https://www.douyin.com/aweme/v1/play/?video_id=1',
            'https://aweme.iesdouyin.com/aweme/v1/play/',
            'https://f.video.weibocdn.com/x.mp4',
        ):
            with self.subTest(url=url):
                self.assertTrue(_host_allowed(url))
        self.assertFalse(_host_allowed('https://other.akamaized.net/x'))

    def test_media_proxy_ignores_client_range_and_disables_resume(self):
        import media_dl.routes as media_routes

        calls = []

        class FakeUpstreamResponse:
            status_code = 206
            headers = {'Content-Range': 'bytes 0-2/3', 'Content-Type': 'video/mp4'}

            def iter_content(self, chunk_size=65536):
                yield b'abc'

            def close(self):
                pass

        def fake_fetch(url, base_headers, start, end):
            calls.append((start, end))
            return FakeUpstreamResponse()

        with mock.patch.object(media_routes, '_fetch_range', side_effect=fake_fetch):
            response = self.client.get(
                '/api/media-dl/proxy'
                '?u=https://upos-sz-mirrorcosov.bilivideo.com/video.mp4'
                '&name=v.mp4&r=https://www.bilibili.com',
                headers={'Range': 'bytes=100-'},   # resuming client
                buffered=False,
            )
            body = response.get_data()

        # Full body from byte 0 despite the client asking for bytes=100-.
        self.assertEqual(calls[0][0], 0)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get('Accept-Ranges'), 'none')
        self.assertEqual(body, b'abc')

    def test_bilibili_prefers_official_cdn_over_pcdn_backup(self):
        from media_dl.bilibili import _prefer_official_url

        self.assertEqual(
            _prefer_official_url(
                'https://wfm.edge.mountaintoys.cn/a',
                ['https://upos-sz-estgoss.bilivideo.com/a'],
            ),
            'https://upos-sz-estgoss.bilivideo.com/a',
        )
        # No official backup → keep the primary rather than dropping the item.
        self.assertEqual(
            _prefer_official_url('https://only.pcdn.example/a', []),
            'https://only.pcdn.example/a',
        )

    def test_xhs_video_items_tolerate_empty_and_null_backup_urls(self):
        from media_dl.xhs import _video_items

        note = {
            'video': {'media': {'stream': {'h264': [
                {'masterUrl': '', 'backupUrls': []},            # would IndexError before
                {'masterUrl': None, 'backupUrls': None},        # would TypeError before
                {'masterUrl': 'https://sns-video.xhscdn.com/ok.mp4',
                 'height': 1080, 'width': 1920},
            ]}}}
        }
        items = _video_items(note, 'title')
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['url'], 'https://sns-video.xhscdn.com/ok.mp4')

    def test_xhs_undefined_substitution_preserves_quoted_prose(self):
        from media_dl.xhs import _undefined_to_null

        blob = '{"a":undefined,"title":"C++ undefined behavior","b":[undefined,1]}'
        cleaned = _undefined_to_null(blob)
        self.assertIn('"a":null', cleaned)
        self.assertIn('[null,1]', cleaned)
        self.assertIn('C++ undefined behavior', cleaned)  # untouched inside the string

    def test_ytdlp_rejects_hls_and_fragmented_formats(self):
        from media_dl.ytdlp import _is_directly_downloadable

        self.assertTrue(_is_directly_downloadable(
            {'url': 'https://cdn/v.mp4', 'protocol': 'https'}))
        self.assertTrue(_is_directly_downloadable({'url': 'https://cdn/v.mp4'}))  # protocol absent
        self.assertFalse(_is_directly_downloadable(
            {'url': 'https://cdn/playlist.m3u8', 'protocol': 'm3u8_native'}))
        self.assertFalse(_is_directly_downloadable(
            {'url': 'https://cdn/seg', 'protocol': 'http_dash_segments'}))
        self.assertFalse(_is_directly_downloadable(
            {'url': 'https://cdn/v.mp4', 'fragments': [{'url': 'a'}]}))
        self.assertFalse(_is_directly_downloadable({'url': ''}))

    def test_media_merge_validates_params_and_hosts(self):
        # Missing a param.
        r = self.client.get('/api/media-dl/merge?v=https://x.bilivideo.com/v.m4s')
        self.assertEqual(r.status_code, 400)
        # Non-allowlisted hosts.
        r = self.client.get('/api/media-dl/merge?v=https://evil.com/v&a=https://evil.com/a')
        self.assertEqual(r.status_code, 403)

    def test_media_merge_returns_501_when_ffmpeg_absent(self):
        import media_dl.routes as media_routes

        with mock.patch.object(media_routes, '_ffmpeg_path', return_value=''):
            r = self.client.get(
                '/api/media-dl/merge'
                '?v=https://x.bilivideo.com/v.m4s'
                '&a=https://x.bilivideo.com/a.m4s'
                '&r=https://www.bilibili.com'
            )
        self.assertEqual(r.status_code, 501)
        self.assertIn('ffmpeg', r.get_json()['error'])

    def test_bilibili_preferred_official_url_always_passes_proxy_allowlist(self):
        # Regression guard: the CDN suffixes bilibili._prefer_official_url picks
        # from must stay a subset of what routes._host_allowed permits.
        from media_dl.bilibili import _prefer_official_url
        from media_dl.routes import _host_allowed

        pcdn = 'https://wfm.edge.mountaintoys.cn/a'
        for official in (
            'https://upos-sz-estgoss.bilivideo.com/a',
            'https://x.mcdn.bilivideo.cn/a',
            'https://x.hdslb.com/a',
            'https://upos-hz-mirrorakam.akamaized.net/a',
        ):
            with self.subTest(official=official):
                picked = _prefer_official_url(pcdn, [official])
                self.assertEqual(picked, official)
                self.assertTrue(_host_allowed(picked))

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

    def test_classroom_intent_requires_login(self):
        response = self.client.post(
            '/api/classroom-intents',
            json={
                'room': 'T4-101',
                'date': '2026-08-24',
                'start': '10:00',
                'end': '10:50',
                'purpose': 'study',
                'party_size': 1,
            },
            headers={'User-Agent': self.BROWSER_UA},
        )

        self.assertEqual(response.status_code, 401)

    def test_classroom_intent_ui_uses_concise_non_authoritative_copy(self):
        source = self.client.get('/').get_data(as_text=True)

        self.assertIn('const ClassroomIntentModal', source)
        self.assertIn('仅作协调，不代表预约。', source)
        self.assertIn("NOTICE_VERSION = '2026S1b'", source)
        self.assertNotIn('预约成功', source)

    def test_classroom_intent_is_aggregated_without_exposing_identity(self):
        sample_df = app_module.pd.DataFrame([
            {
                'Course Code': 'COMP3001',
                'Course Title & Session': 'Later Class (1001)',
                'Teachers': 'Dr. Later',
                'Class Schedule': 'Mon 15:00-15:50',
                'Classroom': 'T4-101',
            },
        ])
        fixed_now = app_module.datetime(2026, 8, 24, 9, 0, tzinfo=app_module.BEIJING_TZ)
        user_id = self.insert_user('intent-owner', display_name='Private Name')
        second_user_id = self.insert_user('second-owner', display_name='Second Private Name')
        with self.client.session_transaction() as login_session:
            login_session['user_id'] = user_id
        second_client = app_module.app.test_client()
        with second_client.session_transaction() as login_session:
            login_session['user_id'] = second_user_id

        payload = {
            'room': 'T4-101',
            'date': '2026-08-24',
            'start': '10:00',
            'end': '11:50',
            'purpose': 'discussion',
            'party_size': 3,
        }
        with mock.patch.object(app_module, 'get_df', return_value=sample_df), \
             mock.patch.object(app_module, '_classroom_now', return_value=fixed_now):
            created = self.client.post(
                '/api/classroom-intents',
                json=payload,
                headers={'User-Agent': self.BROWSER_UA},
            )
            second_created = second_client.post(
                '/api/classroom-intents',
                json={**payload, 'purpose': 'study', 'party_size': 2},
                headers={'User-Agent': self.BROWSER_UA},
            )
            owner_view = self.client.get(
                '/api/free-classrooms?day=Mon&date=2026-08-24&start=10:00&end=11:50',
                headers={'User-Agent': self.BROWSER_UA},
            )
            public_view = app_module.app.test_client().get(
                '/api/free-classrooms?day=Mon&date=2026-08-24&start=10:00&end=11:50',
                headers={'User-Agent': self.BROWSER_UA},
            )

        self.assertEqual(created.status_code, 201)
        self.assertEqual(second_created.status_code, 201)
        created_intent = created.get_json()['intent']
        self.assertEqual(created_intent['status'], 'planned')

        owner_room = owner_view.get_json()['rooms'][0]
        self.assertEqual(owner_room['intent']['records'], 2)
        self.assertEqual(owner_room['intent']['planned_people'], 5)
        self.assertEqual(owner_room['intent']['checked_in_people'], 0)
        self.assertEqual(owner_room['intent']['my']['id'], created_intent['id'])
        self.assertEqual(owner_view.headers.get('Cache-Control'), 'no-store')

        public_intent = public_view.get_json()['rooms'][0]['intent']
        self.assertEqual(public_intent['people'], 5)
        self.assertIsNone(public_intent['my'])
        self.assertNotIn('Private Name', public_view.get_data(as_text=True))
        self.assertNotIn('intent-owner', public_view.get_data(as_text=True))
        self.assertNotIn('Second Private Name', public_view.get_data(as_text=True))
        self.assertNotIn('second-owner', public_view.get_data(as_text=True))

    def test_classroom_intent_rejects_timetable_conflict(self):
        sample_df = app_module.pd.DataFrame([
            {
                'Course Code': 'COMP3002',
                'Course Title & Session': 'Scheduled Class (1001)',
                'Teachers': 'Dr. Busy',
                'Class Schedule': 'Mon 10:00-10:50',
                'Classroom': 'T4-101',
            },
        ])
        fixed_now = app_module.datetime(2026, 8, 24, 9, 0, tzinfo=app_module.BEIJING_TZ)
        user_id = self.insert_user('conflict-user')
        with self.client.session_transaction() as login_session:
            login_session['user_id'] = user_id

        with mock.patch.object(app_module, 'get_df', return_value=sample_df), \
             mock.patch.object(app_module, '_classroom_now', return_value=fixed_now):
            response = self.client.post(
                '/api/classroom-intents',
                json={
                    'room': 'T4-101',
                    'date': '2026-08-24',
                    'start': '10:00',
                    'end': '10:50',
                    'purpose': 'study',
                    'party_size': 1,
                },
                headers={'User-Agent': self.BROWSER_UA},
            )

        self.assertEqual(response.status_code, 409)
        self.assertIn('scheduled class', response.get_json()['error'].lower())

    def test_classroom_intent_check_in_and_end_flow(self):
        sample_df = app_module.pd.DataFrame([
            {
                'Course Code': 'COMP3003',
                'Course Title & Session': 'Later Class (1001)',
                'Teachers': 'Dr. Later',
                'Class Schedule': 'Mon 15:00-15:50',
                'Classroom': 'T4-101',
            },
        ])
        user_id = self.insert_user('check-in-user')
        with self.client.session_transaction() as login_session:
            login_session['user_id'] = user_id

        create_now = app_module.datetime(2026, 8, 24, 9, 0, tzinfo=app_module.BEIJING_TZ)
        with mock.patch.object(app_module, 'get_df', return_value=sample_df), \
             mock.patch.object(app_module, '_classroom_now', return_value=create_now):
            created = self.client.post(
                '/api/classroom-intents',
                json={
                    'room': 'T4-101',
                    'date': '2026-08-24',
                    'start': '10:00',
                    'end': '10:50',
                    'purpose': 'practice',
                    'party_size': 2,
                },
                headers={'User-Agent': self.BROWSER_UA},
            )
        intent_id = created.get_json()['intent']['id']

        arrival_now = app_module.datetime(2026, 8, 24, 9, 55, tzinfo=app_module.BEIJING_TZ)
        with mock.patch.object(app_module, 'get_df', return_value=sample_df), \
             mock.patch.object(app_module, '_classroom_now', return_value=arrival_now):
            checked_in = self.client.post(
                f'/api/classroom-intents/{intent_id}/check-in',
                headers={'User-Agent': self.BROWSER_UA},
            )
            occupied = self.client.get(
                '/api/free-classrooms?day=Mon&date=2026-08-24&start=10:00&end=10:50',
                headers={'User-Agent': self.BROWSER_UA},
            )
            ended = self.client.delete(
                f'/api/classroom-intents/{intent_id}',
                headers={'User-Agent': self.BROWSER_UA},
            )
            cleared = self.client.get(
                '/api/free-classrooms?day=Mon&date=2026-08-24&start=10:00&end=10:50',
                headers={'User-Agent': self.BROWSER_UA},
            )

        self.assertEqual(checked_in.status_code, 200)
        self.assertEqual(checked_in.get_json()['intent']['status'], 'checked_in')
        self.assertEqual(occupied.get_json()['rooms'][0]['intent']['checked_in_people'], 2)
        self.assertEqual(ended.status_code, 200)
        self.assertEqual(ended.get_json()['status'], 'ended')
        self.assertEqual(cleared.get_json()['rooms'][0]['intent']['people'], 0)

    def test_unconfirmed_classroom_intent_expires_after_grace_period(self):
        sample_df = app_module.pd.DataFrame([
            {
                'Course Code': 'COMP3004',
                'Course Title & Session': 'Later Class (1001)',
                'Teachers': 'Dr. Later',
                'Class Schedule': 'Mon 15:00-15:50',
                'Classroom': 'T4-101',
            },
        ])
        user_id = self.insert_user('no-show-user')
        with sqlite3.connect(app_module.DB_PATH) as conn:
            conn.execute(
                '''
                INSERT INTO classroom_intents
                    (user_id, room, use_date, start_min, end_min, purpose, party_size)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ''',
                (user_id, 'T4-101', '2026-08-24', 600, 650, 'study', 1),
            )
            conn.commit()

        late_now = app_module.datetime(2026, 8, 24, 10, 16, tzinfo=app_module.BEIJING_TZ)
        with mock.patch.object(app_module, 'get_df', return_value=sample_df), \
             mock.patch.object(app_module, '_classroom_now', return_value=late_now):
            response = self.client.get(
                '/api/free-classrooms?day=Mon&date=2026-08-24&start=10:00&end=10:50',
                headers={'User-Agent': self.BROWSER_UA},
            )

        self.assertEqual(response.get_json()['rooms'][0]['intent']['people'], 0)

    # ------------------------------------------------------------------
    # Analytics summary (/api/analytics/summary)
    # ------------------------------------------------------------------
    def _insert_page_view(self, visitor_id, view_name='home', user_id=None,
                          user_agent='Mozilla/5.0 (Macintosh)', referrer='', created_at=None):
        with sqlite3.connect(app_module.DB_PATH) as conn:
            if created_at is None:
                conn.execute(
                    'INSERT INTO page_views (visitor_id, user_id, view_name, referrer, user_agent) '
                    'VALUES (?, ?, ?, ?, ?)',
                    (visitor_id, user_id, view_name, referrer, user_agent),
                )
            else:
                conn.execute(
                    'INSERT INTO page_views (visitor_id, user_id, view_name, referrer, user_agent, created_at) '
                    'VALUES (?, ?, ?, ?, ?, ?)',
                    (visitor_id, user_id, view_name, referrer, user_agent, created_at),
                )
            conn.commit()

    def test_analytics_summary_empty_db_is_safe(self):
        response = self.client.get('/api/analytics/summary')

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        # Back-compat top-level keys older cached pages read.
        self.assertEqual(data['totalViews'], 0)
        self.assertEqual(data['uniqueVisitors'], 0)
        self.assertEqual(data['todayViews'], 0)
        self.assertEqual(data['byView'], [])
        # Enriched payload degrades to zeros / empties, never crashes.
        self.assertEqual(data['totals']['views'], 0)
        self.assertEqual(data['totals']['registeredVisitors'], 0)
        self.assertEqual(data['daily'], [])
        self.assertEqual(data['devices'], [])
        self.assertEqual(data['referrers'], [])
        self.assertEqual(len(data['hourly']), 24)  # always a full 0-23 axis
        self.assertEqual(data['timezone'], 'UTC+8')

    def test_analytics_summary_shape_devices_and_referrers(self):
        # 3 desktop hits from visitor a (one carrying a user_id = registered),
        # 2 Android-tablet hits from b, 1 Android-phone hit from c.
        self._insert_page_view('a', view_name='home', user_id=7)
        self._insert_page_view('a', view_name='explorer')
        self._insert_page_view('a', view_name='home', referrer='https://www.google.com/search?q=x')
        tablet_ua = 'Mozilla/5.0 (Linux; Android 13; SM-X710) AppleWebKit/537.36 Chrome/120 Safari/537.36'
        self._insert_page_view('b', view_name='home', user_agent=tablet_ua)
        self._insert_page_view('b', view_name='home', user_agent=tablet_ua, referrer='http://localhost/')
        phone_ua = 'Mozilla/5.0 (Linux; Android 13; Pixel 7) Chrome/120 Mobile Safari/537.36'
        self._insert_page_view('c', view_name='home', user_agent=phone_ua)

        data = self.client.get('/api/analytics/summary').get_json()

        self.assertEqual(data['totalViews'], 6)
        self.assertEqual(data['uniqueVisitors'], 3)
        self.assertEqual(data['totals']['registeredVisitors'], 1)

        # Device split: Android tablet must NOT be lumped into mobile.
        devices = {d['device']: d['views'] for d in data['devices']}
        self.assertEqual(devices.get('desktop'), 3)
        self.assertEqual(devices.get('tablet'), 2)
        self.assertEqual(devices.get('mobile'), 1)

        # Referrers: external host aggregated, internal (localhost) excluded.
        ext_hosts = {r['host']: r['count'] for r in data['referrers']}
        self.assertEqual(ext_hosts.get('google.com'), 1)
        self.assertNotIn('localhost', ext_hosts)
        self.assertGreaterEqual(data['internalReferrals'], 1)

        # Per-view breakdown carries the 7-day trend column.
        home = next(v for v in data['byView'] if v['view_name'] == 'home')
        self.assertEqual(home['views'], 5)  # a×2 + b×2 + c×1
        self.assertEqual(home['visitors'], 3)
        self.assertIn('views_7d', home)

    def test_analytics_summary_buckets_days_in_beijing_time(self):
        # 15:00 UTC = 23:00 Beijing same day; 17:00 UTC = 01:00 Beijing next day.
        # Both must land on their Beijing calendar day, not the UTC one.
        self._insert_page_view('x', created_at='2026-05-10 15:00:00')
        self._insert_page_view('y', created_at='2026-05-10 17:00:00')

        with sqlite3.connect(app_module.DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT visitor_id, date(created_at, '+8 hours') AS day, "
                "CAST(strftime('%H', datetime(created_at, '+8 hours')) AS INTEGER) AS hour "
                "FROM page_views ORDER BY created_at"
            ).fetchall()
        by_visitor = {r['visitor_id']: (r['day'], r['hour']) for r in rows}
        self.assertEqual(by_visitor['x'], ('2026-05-10', 23))
        self.assertEqual(by_visitor['y'], ('2026-05-11', 1))

    def test_daily_rollup_backfills_and_matches_raw(self):
        # Three Beijing days; note the 20:00-UTC hit belongs to the *next* Beijing day.
        self._insert_page_view('a', created_at='2026-03-01 02:00:00')  # 10:00 BJ 03-01
        self._insert_page_view('b', created_at='2026-03-01 03:00:00')  # 11:00 BJ 03-01
        self._insert_page_view('a', created_at='2026-03-02 20:00:00')  # 04:00 BJ 03-03
        self._insert_page_view('c', created_at='2026-03-05 06:00:00')  # 14:00 BJ 03-05

        with sqlite3.connect(app_module.DB_PATH) as conn:
            app_module.rollup_daily_page_stats(conn)  # full backfill (deploy-time path)
            conn.commit()
            conn.row_factory = sqlite3.Row
            roll = {r['day']: (r['views'], r['visitors'])
                    for r in conn.execute('SELECT day, views, visitors FROM daily_page_stats').fetchall()}

        self.assertEqual(roll['2026-03-01'], (2, 2))
        self.assertEqual(roll['2026-03-03'], (1, 1))
        self.assertEqual(roll['2026-03-05'], (1, 1))
        self.assertNotIn('2026-03-02', roll)  # 20:00 UTC row rolls into Beijing 03-03

        # Endpoint surfaces the persisted history + meta via ?days=.
        data = self.client.get('/api/analytics/summary?days=365').get_json()
        days = {d['day']: d['views'] for d in data['daily']}
        self.assertEqual(days.get('2026-03-01'), 2)
        self.assertEqual(data['dailyRecordedDays'], 3)
        self.assertEqual(data['dailyFirstDay'], '2026-03-01')

    def test_daily_rollup_refreshes_on_summary_read(self):
        def today_views():
            with sqlite3.connect(app_module.DB_PATH) as conn:
                row = conn.execute(
                    "SELECT views FROM daily_page_stats WHERE day = date('now', '+8 hours')"
                ).fetchone()
                return row[0] if row else None

        self._insert_page_view('v1')          # created_at defaults to now (Beijing today)
        self.client.get('/api/analytics/summary')
        self.assertEqual(today_views(), 1)

        self._insert_page_view('v2')          # a later hit the same day
        self.client.get('/api/analytics/summary')
        self.assertEqual(today_views(), 2)    # freshen-on-read picked it up

    # ------------------------------------------------------------------
    # Anti-scraping (static blocklist, UA filter, rate limit)
    # ------------------------------------------------------------------
    BROWSER_UA = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Safari/604.1'

    def test_data_files_are_not_public_static_assets(self):
        for path in (
            '/course_catalog.json', '/course_enrichment.json', '/campus_docs.json',
            '/course_equivalences.json', '/course_textbooks.json',
            '/course_semester_2526S1.json', '/semesters_index.json',
            '/programme_requirements.json', '/skillpath_nodes.json',
            '/skillpath_graph.npz', '/CLAUDE.md', '/README.md',
            '/requirements.txt', '/precompile.js',
            '/deploy/nginx/maxcourse-antibot-zones.conf',
            '/Course%20List%20and%20Timetable_Semester%201%20of%20AY2026-27_20260709.xlsx',
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

    def test_static_guard_resists_path_normalization_tricks(self):
        # A trailing slash / '/.' / doubled slash must not let the static handler
        # serve a blocked file (regression: GET /maxcourse.db/ dumped the live DB).
        for path in (
            '/maxcourse.db/', '/app.py/', '/app.py/.', '/crawler.py/',
            '/CLAUDE.md/', '/course_catalog.json/',
            '/./app.py', '/.flask_secret_key/', '/sso_bridge.py/',
        ):
            with self.subTest(path=path):
                r = self.client.get(path)
                self.assertEqual(r.status_code, 404)
                self.assertNotIn(b'SQLite format', r.data)

    def test_legacy_paths_survive_static_blocklist(self):
        # todolist.html still fetches this root JSON directly.
        self.assertEqual(self.client.get('/todolist.json').status_code, 200)
        # robots.txt must stay served (and steer polite crawlers off the API).
        robots = self.client.get('/robots.txt')
        self.assertEqual(robots.status_code, 200)
        self.assertIn(b'Disallow: /api/', robots.data)
        # Blocked campus_docs.json is still served through its API endpoint.
        r = self.client.get('/api/campus-docs', headers={'User-Agent': self.BROWSER_UA})
        self.assertEqual(r.status_code, 200)

    def test_public_read_api_supports_private_browser_revalidation(self):
        first = self.client.get(
            '/api/semesters',
            headers={'User-Agent': self.BROWSER_UA},
        )
        etag = first.headers.get('ETag')

        self.assertEqual(first.status_code, 200)
        self.assertTrue(etag)
        self.assertIn('private', first.headers.get('Cache-Control', ''))
        self.assertIn('max-age=', first.headers.get('Cache-Control', ''))

        second = self.client.get(
            '/api/semesters',
            headers={'User-Agent': self.BROWSER_UA, 'If-None-Match': etag},
        )
        self.assertEqual(second.status_code, 304)
        self.assertEqual(second.data, b'')

    def test_scraper_user_agents_are_rejected_on_api_only(self):
        for ua in ('python-requests/2.31.0', 'Scrapy/2.11 (+https://scrapy.org)',
                   'node-fetch/1.0', 'Go-http-client/2.0', ''):
            with self.subTest(ua=ua):
                r = self.client.get('/api/semesters', headers={'User-Agent': ua})
                self.assertEqual(r.status_code, 403)
        # Browsers pass.
        ok = self.client.get('/api/semesters', headers={'User-Agent': self.BROWSER_UA})
        self.assertEqual(ok.status_code, 200)
        # Static pages stay open to any client (only the data API is guarded).
        page = self.client.get('/', headers={'User-Agent': 'python-requests/2.31.0'})
        self.assertEqual(page.status_code, 200)

    def test_api_rate_limit_returns_429_with_retry_after(self):
        app_module._rate_counters.clear()
        try:
            # Freeze time so all requests share one 60s window (no boundary flake).
            with mock.patch('time.time', return_value=1_000_000.0), \
                 mock.patch.object(app_module, 'RATE_LIMIT_IP_PER_MIN', 3):
                responses = [
                    self.client.get('/api/semesters',
                                    headers={'User-Agent': self.BROWSER_UA},
                                    environ_overrides={'REMOTE_ADDR': '203.0.113.9'})
                    for _ in range(5)
                ]
            codes = [r.status_code for r in responses]
            self.assertEqual(codes[:3], [200, 200, 200])
            self.assertEqual(codes[3], 429)
            self.assertEqual(codes[4], 429)
            limited = responses[4]
            self.assertTrue(limited.headers.get('Retry-After'))
            self.assertIn('error', limited.get_json())
        finally:
            app_module._rate_counters.clear()

    def test_first_browser_api_request_is_covered_by_visitor_limit(self):
        """A bot cannot skip /analytics/track to escape the visitor bucket."""
        app_module._rate_counters.clear()
        try:
            now = 1_800_000_000.0
            with mock.patch('time.time', return_value=now), \
                 mock.patch.object(app_module, 'RATE_LIMIT_IP_PER_MIN', 100), \
                 mock.patch.object(app_module, 'RATE_LIMIT_VISITOR_PER_MIN', 2):
                client = app_module.app.test_client()
                kwargs = {
                    'headers': {'User-Agent': self.BROWSER_UA},
                    'environ_overrides': {'REMOTE_ADDR': '203.0.113.56'},
                }
                responses = [
                    client.get('/api/semesters', **kwargs)
                    for _ in range(3)
                ]

            self.assertEqual([r.status_code for r in responses], [200, 200, 429])
            self.assertIn('session=', responses[0].headers.get('Set-Cookie', ''))
        finally:
            app_module._rate_counters.clear()

    def test_rate_limit_does_not_reset_at_minute_boundary(self):
        """Crossing :00 must not grant a second full burst immediately."""
        app_module._rate_counters.clear()
        try:
            now = 1_800_000_000.0
            minute = int(now // 60) * 60
            client = app_module.app.test_client()
            kwargs = {
                'headers': {'User-Agent': self.BROWSER_UA},
                'environ_overrides': {'REMOTE_ADDR': '203.0.113.57'},
            }
            with mock.patch.object(app_module, 'RATE_LIMIT_IP_PER_MIN', 100), \
                 mock.patch.object(app_module, 'RATE_LIMIT_VISITOR_PER_MIN', 2):
                with mock.patch('time.time', return_value=minute + 59.9):
                    first = client.get('/api/semesters', **kwargs)
                    second = client.get('/api/semesters', **kwargs)
                with mock.patch('time.time', return_value=minute + 60.1):
                    third = client.get('/api/semesters', **kwargs)

            self.assertEqual((first.status_code, second.status_code), (200, 200))
            self.assertEqual(third.status_code, 429)
        finally:
            app_module._rate_counters.clear()

    def test_rate_limit_charges_ip_bucket_even_with_session_cookie(self):
        # A scraper cannot dodge the per-IP ceiling by minting fresh visitor
        # cookies: cookied requests are still charged to the IP bucket.
        app_module._rate_counters.clear()
        try:
            with mock.patch('time.time', return_value=1_000_000.0), \
                 mock.patch.object(app_module, 'RATE_LIMIT_IP_PER_MIN', 2), \
                 mock.patch.object(app_module, 'RATE_LIMIT_VISITOR_PER_MIN', 100):
                ovr = {'REMOTE_ADDR': '203.0.113.55'}
                hdr = {'User-Agent': self.BROWSER_UA}
                # 1: mints a session cookie (cookieless at throttle time) -> ip=1
                r1 = self.client.post('/api/analytics/track', json={'view': 'home'},
                                      headers=hdr, environ_overrides=ovr)
                # 2: now carries the cookie -> charges ip(=2) AND visitor(=1)
                r2 = self.client.get('/api/semesters', headers=hdr, environ_overrides=ovr)
                # 3: ip bucket (=3) exceeds 2 despite an unexhausted visitor bucket
                r3 = self.client.get('/api/semesters', headers=hdr, environ_overrides=ovr)
            self.assertEqual((r1.status_code, r2.status_code), (200, 200))
            self.assertEqual(r3.status_code, 429)
        finally:
            app_module._rate_counters.clear()

    def test_notification_dispatch_is_exempt_from_scraper_filter(self):
        # The cron heartbeat must never be UA-filtered or rate-limited; its own
        # secret check is the gate (503 unconfigured / 401 bad secret, never 403/429).
        r = self.client.post('/api/notifications/dispatch',
                             headers={'User-Agent': 'python-requests/2.31.0'})
        self.assertNotIn(r.status_code, (403, 429))


if __name__ == '__main__':
    unittest.main()
