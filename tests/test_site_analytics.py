import json
import sqlite3
import tempfile
from pathlib import Path
import unittest
from datetime import datetime
from unittest import mock
from flask import Flask
from site_analytics import BEIJING, build_dashboard, create_analytics_blueprint, window_from_args

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=BEIJING)


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.executescript('''
          CREATE TABLE page_views(visitor_id TEXT,user_id INTEGER,view_name TEXT,created_at TEXT,user_agent TEXT,referrer TEXT,referrer_known INTEGER DEFAULT 1);
          CREATE TABLE media_dl_events(action TEXT,platform TEXT,success INTEGER,bytes INTEGER,created_at TEXT,
            elapsed_ms INTEGER,error TEXT,host TEXT,visitor_id TEXT,user_id INTEGER);
          CREATE TABLE daily_page_stats(day TEXT,views INTEGER,visitors INTEGER);
          CREATE TABLE users(id INTEGER PRIMARY KEY,username TEXT,email TEXT,email_notifications_enabled INTEGER);
        ''')
        self.addCleanup(self.db.close)

    def visit(self, visitor='v', created='2026-09-07 01:00:00', ref='', ua='Chrome Desktop', view='home'):
        self.db.execute('INSERT INTO page_views(visitor_id,user_id,view_name,created_at,user_agent,referrer) VALUES (?,NULL,?,?,?,?)', (visitor, view, created, ua, ref))

    def media(self, action='resolve', ok=1, size=0, platform='youtube', error=None, elapsed=100):
        self.db.execute('INSERT INTO media_dl_events VALUES (?,?,?,?,?,?,?,?,NULL,NULL)',
                        (action, platform, ok, size, '2026-09-07 01:00:00', elapsed, error, 'cdn.example.com'))

    def result(self, **args):
        return build_dashboard(self.db, {'days': '1', **args}, NOW)

    def test_all_traffic_breakdowns_reconcile_and_rollup_is_not_used(self):
        self.visit('returning', '2026-09-01 01:00:00')
        self.visit('returning')
        self.visit('returning', ref='https://bnbscheduler.top/')
        self.visit('new', ref='https://search.example.com/?secret=do-not-return', ua='iPhone Mobile')
        self.db.execute("INSERT INTO daily_page_stats VALUES ('2026-09-07',999,999)")
        result = self.result();t=result['traffic']
        self.assertEqual((t['views'],t['visitors'],t['newVisitors'],t['returningVisitors']), (3,2,1,1))
        for key in ('pages','devices','sources','hourly'):
            self.assertEqual(sum(row['views'] for row in t[key]), t['views'])
        self.assertEqual(result['daily'][0]['views'], 3)
        self.assertNotIn('do-not-return', json.dumps(result))

    def test_period_uv_is_deduplicated_across_days(self):
        self.visit('same','2026-09-06 01:00:00');self.visit('same')
        result=self.result(days='2')
        self.assertEqual(result['traffic']['visitors'],1)
        self.assertEqual(sum(row['visitors'] or 0 for row in result['daily']),2)

    def test_email_opt_ins_count_current_accounts_independently_of_visit_filters(self):
        self.db.executemany('INSERT INTO users VALUES (?,?,?,?)', [
            (1, 'private-enabled', 'private-enabled@example.test', 1),
            (2, 'no-visits', 'no-visits@example.test', 1),
            (3, 'disabled', 'disabled@example.test', 0),
            (4, 'never-configured', None, None),
        ])
        self.visit(); self.visit(); self.visit('bot', ua='Googlebot')
        for args in ({}, {'days': '30'}, {'exclude_bots': '0'},
                     {'start': '2020-01-01', 'end': '2020-01-01'}):
            with self.subTest(args=args):
                result = self.result(**args)
                self.assertEqual(result['subscriptions'], {'ddlEmailEnabled': 2})
                self.assertNotIn('private-enabled', json.dumps(result))
                self.assertNotIn('@example.test', json.dumps(result))
        self.db.execute('UPDATE users SET email_notifications_enabled=0 WHERE id=1')
        self.assertEqual(self.result()['subscriptions']['ddlEmailEnabled'], 1)

    def test_email_opt_ins_are_zero_with_no_enabled_accounts(self):
        self.assertEqual(self.result()['subscriptions']['ddlEmailEnabled'], 0)

    def test_beijing_boundaries_and_equal_elapsed_comparison(self):
        self.visit('today','2026-09-06 16:00:00')
        self.visit('before','2026-09-06 15:59:59')
        self.visit('yesterday_morning','2026-09-06 01:00:00')
        self.visit('future','2026-09-07 05:00:00')
        result=self.result()
        self.assertEqual(result['traffic']['views'],1)
        self.assertEqual(result['traffic']['previous']['views'],1)
        self.assertEqual(result['traffic']['hourly'][0]['views'],1)

    def test_bot_filter_is_consistent(self):
        self.visit();self.visit('bot',ua='Googlebot')
        result=self.result()
        self.assertEqual(result['traffic']['views'],1)
        self.assertEqual(result['traffic']['excludedBots'],1)
        self.assertEqual(result['daily'][0]['views'],1)
        self.assertEqual(self.result(exclude_bots='0')['traffic']['views'],2)

    def test_all_download_modes_and_test_filter_reconcile(self):
        self.media();self.media(ok=0,error='HTTP 403 https://cdn.example.com/?secret=private')
        self.media(action='proxy',size=100);self.media(action='merge',size=200);self.media(action='batch',size=300)
        self.db.execute("INSERT INTO media_dl_events VALUES ('proxy','bilibili',1,3,'2026-09-07 01:00:00',1,NULL,'upos-sz-mirrorcosov.bilivideo.com','auto-assigned-test-visitor',NULL)")
        result=self.result();m=result['media']
        self.assertEqual((m['resolves'],m['resolveOk'],m['downloads'],m['bytes']), (2,1,3,600))
        self.assertEqual((m['singles'],m['merges'],m['batches'],m['excludedTests']), (1,1,1,1))
        self.assertEqual(m['resolveRate'],50)
        self.assertEqual(sum(r['bytes'] for r in m['platforms']), m['bytes'])
        self.assertEqual(result['daily'][0]['bytes'],600)
        self.assertNotIn('private', json.dumps(result))
        self.assertEqual(m['errors'][0]['name'],'blocked')

    def test_empty_rates_and_unrecorded_history_are_not_false_zeros(self):
        result=self.result(days='7')
        self.assertEqual(result['media']['resolves'],0)
        self.assertIsNone(result['media']['resolveRate'])
        self.assertIsNone(result['daily'][0]['views'])
        self.visit()
        result=self.result(days='7')
        self.assertIsNone(result['daily'][0]['views'])
        self.assertEqual(result['daily'][-1]['views'],1)

    def test_invalid_ranges_are_rejected(self):
        for args in ({'days':'0'}, {'days':'366'}, {'days':'abc'},
                     {'start':'2026-09-09','end':'2026-09-07'},
                     {'start':'2026-09-01'}, {'start':'2026-09-01','end':'2026-09-08'}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                window_from_args(args,NOW)

    def test_media_tests_do_not_write_to_application_database(self):
        from media_dl.analytics import log_event
        app=Flask(__name__);app.testing=True
        with app.app_context(),mock.patch('media_dl.analytics.sqlite3.connect') as connect:
            log_event(visitor_id=None,user_id=None,action='proxy',platform='bilibili',host='test',success=True,bytes_count=3)
        connect.assert_not_called()


    def test_delayed_test_stream_cannot_emit_after_testing_is_reset(self):
        from media_dl import routes
        from media_dl.analytics import log_event
        app=Flask(__name__);app.secret_key='temporary';app.testing=True
        app.register_blueprint(routes.media_dl_bp)
        upstream=mock.Mock(status_code=206,headers={'Content-Range':'bytes 0-2/3'})
        upstream.iter_content.return_value=iter([b'abc'])
        with mock.patch.object(routes,'_fetch_range',return_value=upstream), mock.patch('media_dl.analytics.sqlite3.connect') as connect:
            response=app.test_client().get('/api/media-dl/proxy?u=https://upos-sz-mirrorcosov.bilivideo.com/test.mp4',buffered=False)
            app.testing=False
            response.get_data()
            response.close()
        connect.assert_not_called()


    def test_http_dashboard_is_read_only_and_keeps_runtime_scope_separate(self):
        self.visit(created='2020-01-01 01:00:00')
        self.db.execute("INSERT INTO daily_page_stats VALUES ('2020-01-01',999,999)")
        self.db.commit()
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'test.db'
            target=sqlite3.connect(path)
            self.db.backup(target)
            target.close()
            before=path.read_bytes()
            app=Flask(__name__)
            app.register_blueprint(create_analytics_blueprint(lambda:path,lambda:{'uaBlocked':5,'rateLimited':0}))
            client=app.test_client()
            response=client.get('/api/analytics/dashboard?start=2020-01-01&end=2020-01-01')
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.headers['Cache-Control'],'no-store')
            result=response.get_json()
            self.assertEqual(result['traffic']['views'],1)
            self.assertEqual(result['runtime']['uaBlocked'],5)
            self.assertEqual(path.read_bytes(),before)
            self.assertEqual(client.get('/api/analytics/dashboard?days=0').status_code,400)

    def test_unavailable_source_returns_error_not_zero_metrics(self):
        app=Flask(__name__)
        app.register_blueprint(create_analytics_blueprint(lambda:'/nonexistent/stats-source.db'))
        response=app.test_client().get('/api/analytics/dashboard')
        self.assertEqual(response.status_code,503)
        self.assertNotIn('traffic',response.get_json())


    def test_legacy_sources_are_not_mislabeled_as_known_internal_or_direct(self):
        self.visit('known_direct',ref='')
        self.visit('known_internal',ref='https://bnbscheduler.top/')
        self.visit('legacy',ref='https://bnbscheduler.top/')
        self.db.execute("UPDATE page_views SET referrer_known=0 WHERE visitor_id='legacy'")
        sources={row['name']:row['views'] for row in self.result()['traffic']['sources']}
        self.assertEqual((sources['direct'],sources['internal'],sources['legacy']),(1,1,1))


class TrackingSourceTests(unittest.TestCase):
    def setUp(self):
        import app as module
        self.module=module
        self.temp=tempfile.TemporaryDirectory()
        self.original_path=module.DB_PATH
        self.original_testing=module.app.testing
        module.DB_PATH=str(Path(self.temp.name)/'events.db')
        module.app.testing=True
        module.init_db()
        self.client=module.app.test_client()

    def tearDown(self):
        self.module.DB_PATH=self.original_path
        self.module.app.testing=self.original_testing
        self.temp.cleanup()

    def test_explicit_direct_referrer_is_not_replaced_by_fetch_page(self):
        response=self.client.post('/api/analytics/track',json={'view':'home','path':'/','referrer':''},
                                  headers={'User-Agent':'Browser','Referer':'https://bnbscheduler.top/'})
        self.assertEqual(response.status_code,200)
        with sqlite3.connect(self.module.DB_PATH) as conn:
            self.assertEqual(conn.execute('SELECT referrer,referrer_known FROM page_views').fetchone(),('',1))

    def test_missing_referrer_stays_unknown(self):
        response=self.client.post('/api/analytics/track',json={'view':'home'},
                                  headers={'User-Agent':'Browser','Referer':'https://bnbscheduler.top/'})
        self.assertEqual(response.status_code,200)
        with sqlite3.connect(self.module.DB_PATH) as conn:
            self.assertEqual(conn.execute('SELECT referrer,referrer_known FROM page_views').fetchone(),(None,0))


if __name__ == '__main__':
    unittest.main()
