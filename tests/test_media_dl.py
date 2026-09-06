"""Regression checks for the public media downloader, without a real database."""
import io
import json
import socket
import unittest
from unittest import mock

import requests

from flask import Flask
from media_dl import extractor, routes, ytdlp


class MediaDownloaderTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.secret_key = 'media-tests-only'
        self.app.register_blueprint(routes.media_dl_bp)
        self.client = self.app.test_client()
        self.logger = mock.patch.object(routes, 'log_event').start()
        self.addCleanup(mock.patch.stopall)

    def test_invalid_payload_types_are_client_errors(self):
        for payload in ([1], 'text', 7, {'url': 123}, {'url': []}, {'url': {}}):
            with self.subTest(payload=payload):
                response = self.client.post('/api/media-dl/resolve', json=payload)
                self.assertEqual(response.status_code, 400)

    def test_private_urls_never_reach_an_extractor(self):
        for url in ('http://127.0.0.1/a', 'http://[::1]/a',
                    'http://169.254.169.254/a', 'http://localhost/a',
                    'http://2130706433/a', 'http://user:pass@example.com/a',
                    'file:///etc/passwd', 'http://example.com:5000/a'):
            with self.subTest(url=url), mock.patch.object(ytdlp, 'extract') as run:
                response = self.client.post('/api/media-dl/resolve', json={'url': url})
                self.assertEqual(response.status_code, 400)
                run.assert_not_called()

    def test_proxy_failure_returns_an_error_without_a_fake_download(self):
        with mock.patch.object(routes, '_fetch_range', side_effect=RuntimeError('HTTP 403')):
            response = self.client.get('/api/media-dl/proxy', query_string={
                'u': 'https://video.bilivideo.com/video.mp4',
            })
        self.assertEqual(response.status_code, 502)
        self.assertIn('error', response.get_json())
        self.assertNotIn('Content-Disposition', response.headers)

    def test_proxy_rejects_an_initial_range_that_starts_mid_file(self):
        upstream = mock.Mock(status_code=206, headers={'Content-Range': 'bytes 5-7/8'})
        with mock.patch.object(routes, '_fetch_range', return_value=upstream):
            response = self.client.get('/api/media-dl/proxy', query_string={
                'u': 'https://video.bilivideo.com/video.mp4'})
        self.assertEqual(response.status_code, 502)
        upstream.close.assert_called_once()
        upstream.iter_content.assert_not_called()

    def test_proxy_does_not_accept_early_416_as_completed_download(self):
        upstream = mock.Mock(status_code=206, headers={'Content-Range': 'bytes 0-2/6'})
        upstream.iter_content.return_value = iter([b'abc'])
        with mock.patch.object(routes, '_fetch_range', side_effect=[upstream, routes._RangeNotSatisfiable()]), \
                self.assertRaises(RuntimeError):
            response = self.client.get('/api/media-dl/proxy', query_string={
                'u': 'https://video.bilivideo.com/video.mp4'}, buffered=False)
            response.get_data()

    def test_foreign_media_use_same_origin_download_without_an_outbound_proxy(self):
        with mock.patch.object(ytdlp, '_server_proxy', return_value=None):
            for url in ('https://www.youtube.com/watch?v=sample', 'https://x.com/a/status/1'):
                self.assertTrue(ytdlp._needs_proxy(url, url)[0])

    def test_douyin_cookie_failure_is_actionable_chinese(self):
        error = RuntimeError('ERROR: [Douyin] 123: Fresh cookies (not necessarily logged in) are needed')
        message = routes._friendly_error(error)
        self.assertNotIn('Fresh cookies', message)
        self.assertIn('抖音', message)

    def test_audio_only_formats_are_not_discarded(self):
        info = {'title': 'Audio', 'webpage_url': 'https://example.com/audio', 'formats': [{
            'url': 'https://example.com/audio.m4a', 'protocol': 'https',
            'ext': 'm4a', 'vcodec': 'none', 'acodec': 'mp4a.40.2', 'abr': 128,
        }]}
        with mock.patch.object(ytdlp, '_extract_info', return_value=info):
            result = ytdlp.extract('https://example.com/audio')
        self.assertEqual(result['items'][0]['kind'], 'audio')

    def test_private_dns_answer_is_rejected_before_connection(self):
        from media_dl import http
        for address in ('127.0.0.1', '10.1.2.3', '169.254.169.254', '100.64.0.1',
                        '::1', '192.0.0.8', '64:ff9b::7f00:1', '::ffff:127.0.0.1'):
            with self.subTest(address=address), \
                    mock.patch.object(http.socket, 'getaddrinfo', return_value=[
                        (socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 443))]), \
                    mock.patch.object(requests.adapters.HTTPAdapter, 'send') as send:
                with self.assertRaises(http.UnsafeURLError):
                    http.get('https://media.example.com/file.mp4')
                send.assert_not_called()

    def test_redirect_to_private_address_is_rejected_before_second_request(self):
        from media_dl import http
        response = requests.Response()
        response.status_code = 302
        response.headers['Location'] = 'http://127.0.0.1/private'
        response._content = b''
        with mock.patch.object(http.socket, 'getaddrinfo', return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.215.14', 443))]), \
                mock.patch.object(requests.adapters.HTTPAdapter, 'send', return_value=response) as send:
            with self.assertRaises(http.UnsafeURLError):
                http.get('https://media.example.com/redirect')
            self.assertEqual(send.call_count, 1)

    def test_connection_is_pinned_and_keeps_tls_hostname(self):
        from media_dl import http
        response = requests.Response()
        response.status_code = 200
        response._content = b'video'
        with mock.patch.object(http.socket, 'getaddrinfo', side_effect=[[
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.215.14', 443))], [
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))]]) as dns, \
                mock.patch.object(requests.adapters.HTTPAdapter, 'send', return_value=response) as send:
            result = http.get('https://media.example.com/file.mp4')
            pinned = send.call_args.args[0]
            self.assertEqual(pinned.url, 'https://93.184.215.14:443/file.mp4')
            self.assertEqual(pinned.headers['Host'], 'media.example.com')
            self.assertEqual(result.url, 'https://media.example.com/file.mp4')
            self.assertEqual(dns.call_count, 1)
            _, tls = http.PublicHTTPAdapter().build_connection_pool_key_attributes(pinned, True)
            self.assertEqual(tls['server_hostname'], 'media.example.com')
            self.assertEqual(tls['assert_hostname'], 'media.example.com')

    def test_download_frame_reports_failure_with_non_success_status(self):
        with mock.patch.object(routes, '_fetch_range', side_effect=RuntimeError('HTTP 403')):
            response = self.client.get('/api/media-dl/proxy', query_string={
                'u': 'https://video.bilivideo.com/video.mp4', 'feedback': 'a' * 32,
            })
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.mimetype, 'text/html')
        self.assertIn('parent.postMessage', response.get_data(as_text=True))
        self.assertNotIn('Content-Disposition', response.headers)

    def test_douyin_share_text_uses_the_current_page_token_protocol(self):
        from media_dl import douyin
        video_id = '7638586788907223488'
        context = {'loaderData': {'video_(id)/page': {
            'itemId': video_id, 'webId': '1234567890123456789',
        }}}
        page = ('<div id="douyin_reflow_token" xsstoken="public-share-token"></div>'
                '<script>window._ROUTER_DATA = ' + json.dumps(context) + '</script>')
        redirect = mock.Mock(url='https://www.douyin.com/video/' + video_id)
        html = mock.Mock(text=page)
        detail = mock.Mock(headers={})
        detail.json.return_value = {'status_code': 0, 'item_list': [{
            'aweme_id': video_id, 'desc': 'Public video', 'author': {'nickname': 'Creator'},
            'video': {'height': 720, 'width': 1280, 'duration': 9000,
                      'play_addr': {'url_list': ['https://v.example.douyinvod.com/video.mp4']}},
        }]}
        with mock.patch.object(douyin.http, 'Session') as factory, \
                mock.patch.object(ytdlp, 'extract') as old:
            session = factory.return_value.__enter__.return_value
            session.get.side_effect = [redirect, html, detail]
            result = extractor.resolve('复制打开抖音 https://v.douyin.com/example/ 快来看')
            params = session.get.call_args.kwargs['params']
        self.assertTrue(params['reflow_id'])
        self.assertEqual(params['web_id'], '1234567890123456789')
        self.assertEqual(result['duration'], 9)
        self.assertTrue(result['items'][0]['needs_proxy'])
        self.assertNotIn('reflow_id', json.dumps(result))
        old.assert_not_called()

    def test_merge_never_passes_remote_urls_to_ffmpeg(self):
        from media_dl import mux
        response = mock.Mock(headers={})
        response.iter_content.return_value = iter(())
        process = mock.Mock(stdout=io.BytesIO(b'muxed bytes'), returncode=0)
        process.poll.return_value = 0
        with mock.patch.object(mux.http, 'get', return_value=response), \
                mock.patch.object(mux.subprocess, 'Popen', return_value=process) as popen:
            data = b''.join(mux.merge_chunks('/ffmpeg',
                'https://video.bilivideo.com/video', 'https://video.bilivideo.com/audio',
                referer=None, proxies_for=lambda url: None, user_agent='test'))
        self.assertEqual(data, b'muxed bytes')
        command = popen.call_args.args[0]
        self.assertFalse(any('https://' in arg for arg in command))
        self.assertEqual(command.count('-protocol_whitelist'), 2)
        self.assertEqual(len(popen.call_args.kwargs['pass_fds']), 2)

    def test_douyin_does_not_download_the_watermarked_share_preview(self):
        from media_dl import douyin
        result = douyin._result({'video': {'play_addr': {'url_list': [
            'https://aweme.snssdk.com/aweme/v1/playwm/?video_id=public-source-id&ratio=1080p',
        ]}}}, 'https://www.iesdouyin.com/share/video/123/')
        self.assertEqual(result['items'][0]['url'],
                         'https://aweme.snssdk.com/aweme/v1/play/?video_id=public-source-id&ratio=1080p')

    def test_merge_upstream_failure_is_not_a_successful_partial_file(self):
        from media_dl import mux
        response = mock.Mock(headers={})
        response.iter_content.side_effect = requests.ConnectionError('connection dropped')
        process = mock.Mock(stdout=io.BytesIO(b'partial'), returncode=0)
        process.poll.return_value = 0
        with mock.patch.object(mux.http, 'get', return_value=response), \
                mock.patch.object(mux.subprocess, 'Popen', return_value=process):
            chunks = mux.merge_chunks('/ffmpeg',
                'https://video.bilivideo.com/video', 'https://video.bilivideo.com/audio',
                referer=None, proxies_for=lambda url: None, user_agent='test')
            with self.assertRaises(requests.ConnectionError):
                b''.join(chunks)


if __name__ == '__main__':
    unittest.main()
