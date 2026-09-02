import unittest
from unittest import mock

import crawler


def make_response(text='', url='', json_data=None):
    response = mock.Mock()
    response.text = text
    response.url = url
    response.status_code = 200
    response.history = []
    response.headers = {'Content-Type': 'application/json; charset=utf-8'}
    response.raise_for_status.return_value = None
    response.json.return_value = json_data
    return response


class CrawlerTestCase(unittest.TestCase):
    def test_default_ispace_endpoints_use_bnbu_domain(self):
        self.assertEqual(crawler.BASE_URL, 'https://ispace.bnbu.edu.cn')
        self.assertEqual(crawler.LOGIN_URL, 'https://ispace.bnbu.edu.cn/login/index.php')
        self.assertEqual(crawler.SERVICE_URL, 'https://ispace.bnbu.edu.cn/lib/ajax/service.php')

    def test_login_rejects_notloggedin_page_that_contains_my_courses(self):
        login_page = make_response(
            '<input name="logintoken" value="token-123">',
            'https://ispace.bnbu.edu.cn/login/index.php',
        )
        rejected_page = make_response(
            '<body class="notloggedin">My courses<div id="loginerrormessage">Invalid login, please try again</div></body>',
            'https://ispace.bnbu.edu.cn/login/index.php',
        )
        session = mock.Mock()
        session.get.return_value = login_page
        session.post.return_value = rejected_page

        self.assertFalse(crawler.login(session, 'invalid-user', 'invalid-password'))

    def test_login_accepts_authenticated_moodle_marker(self):
        login_page = make_response(
            '<input name="logintoken" value="token-123">',
            'https://ispace.bnbu.edu.cn/login/index.php',
        )
        dashboard = make_response(
            '<body class="path-my loggedin"><a href="/login/logout.php?sesskey=abc123">Log out</a></body>',
            'https://ispace.bnbu.edu.cn/my/',
        )
        session = mock.Mock()
        session.get.return_value = login_page
        session.post.return_value = dashboard

        self.assertTrue(crawler.login(session, 'valid-user', 'valid-password'))

    def test_fetch_timeline_surfaces_object_shaped_moodle_error(self):
        api_response = make_response(
            url='https://ispace.bnbu.edu.cn/lib/ajax/service.php',
            json_data={
                'error': 'Invalid session key',
                'errorcode': 'invalidsesskey',
            },
        )
        session = mock.Mock()
        session.post.return_value = api_response

        with mock.patch.object(crawler.requests, 'Session', return_value=session), \
                mock.patch.object(crawler, 'login', return_value=True), \
                mock.patch.object(crawler, 'get_sesskey', return_value='abc123'):
            result = crawler.fetch_timeline('user', 'password')

        self.assertEqual(result, {'error': 'API Error: Invalid session key'})


if __name__ == '__main__':
    unittest.main()
