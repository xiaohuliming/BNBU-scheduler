"""Read-only BNBU MIS -> Tencent Exmail mailbox adapter.

The quickreadmail preview is deliberately used instead of the normal read view.
Never execute mail HTML/JavaScript or load remote images or attachments.
"""
import re
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit, parse_qs

import requests
from bs4 import BeautifulSoup

MIS = 'https://mis.bnbu.edu.cn'
MAIL = 'https://exmail.qq.com'
ALLOWED_HOSTS = {'mis.bnbu.edu.cn', 'exmail.qq.com', 'en.exmail.qq.com'}
MAIL_ID = re.compile(r'[A-Za-z0-9_~-]{1,160}')
MAX_HTML = 2_000_000


class MailError(Exception):
    def __init__(self, message, code='mail_unavailable', status=502):
        super().__init__(message)
        self.code, self.status = code, status


def soup(text):
    return BeautifulSoup(text, 'html.parser')


def parse_inbox(html, unread_only=True):
    doc = soup(html)
    if doc.select_one('input[type=password]') or not doc.select_one('body#list'):
        raise MailError('邮箱登录状态已过期，请重新连接。', 'mail_session_expired', 401)
    text = doc.get_text(' ', strip=True)
    count = re.search(r'有\s*([\d,]+)\s*封\s*未读邮件', text)
    if unread_only and not count:
        raise MailError('邮箱列表格式发生变化，暂时无法确认未读数量。')
    messages = []
    selector = 'input[type=checkbox][name=mailid]' + ('[unread=true]' if unread_only else '')
    for field in doc.select(selector):
        mid = field.get('value', '')
        row = field.find_parent('table')
        title = row.select_one('.gt u') if row else None
        if not MAIL_ID.fullmatch(mid) or not title:
            raise MailError('邮箱列表格式发生变化，请稍后重试。')
        snippet = row.select_one('.gt b')
        try:
            sent_at = datetime.fromtimestamp(int(field.get('totime', '0')) / 1000, timezone.utc).isoformat()
        except (ValueError, OverflowError, OSError):
            sent_at = ''
        messages.append({'id': mid, 'subject': title.get_text(' ', strip=True)[:500],
                         'sender': field.get('fn', '')[:200], 'sender_address': field.get('fa', '')[:254],
                         'received_at': sent_at, 'snippet': snippet.get_text(' ', strip=True)[:500] if snippet else ''})
    if not unread_only and not messages and not re.search(r'(?:没有|暂无).{0,8}邮件|收件箱.{0,8}空|共\s*0\s*封', text):
        raise MailError('邮箱列表格式发生变化，无法确认最近一周邮件。')
    total = int(count.group(1).replace(',', '')) if count else None
    if unread_only and total and not messages:
        raise MailError('未能读取未读邮件列表，请重新连接。')
    return {'total_unread': total, 'messages': messages, 'has_next': bool(doc.select_one('a#nextpage'))}


def extract_preview(html):
    doc = soup(html)
    if doc.select_one('input[type=password]') or 'session_timeout' in html[:4000]:
        raise MailError('邮箱登录状态已过期，请重新连接。', 'mail_session_expired', 401)
    # Preview responses contain body text directly, without a normal readmail shell.
    has_images = bool(doc.select('img'))
    for node in doc.select('script, style, meta, link, iframe, object, embed, head'):
        node.decompose()
    text = '\n'.join(line.strip() for line in doc.get_text('\n').splitlines() if line.strip())
    if not text and not has_images:
        raise MailError('未能提取这封邮件的正文，请在学校邮箱查看。', 'mail_body_unavailable')
    return {'body': text[:8000], 'body_truncated': len(text) > 8000, 'has_images': has_images}


class SchoolMailbox:
    def __init__(self):
        self.http = requests.Session()
        self.sid = None
        self.started = time.monotonic()
        self.cancelled = lambda: False

    def close(self):
        self.http.cookies.clear()
        self.http.close()
        self.sid = None

    def _request(self, method, url, **kwargs):
        # Follow each redirect ourselves so credentials and SSO tickets never
        # leave the exact school/Tencent host boundary.
        try:
            for _ in range(8):
                if self.cancelled():
                    raise MailError('Mailbox request cancelled', 'mail_cancelled')
                parsed = urlsplit(url)
                if (parsed.scheme != 'https' or parsed.hostname not in ALLOWED_HOSTS
                        or parsed.port not in (None, 443) or parsed.username or parsed.password):
                    raise MailError('学校邮箱跳转地址无法验证，请从 MIS 检查邮箱入口。')
                with self.http.request(method, url, timeout=(8, 20), allow_redirects=False,
                                       stream=True, **kwargs) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        if response.status_code in (307, 308) and method != 'GET':
                            raise MailError('学校登录方式发生变化，请稍后重试。')
                        url = urljoin(url, response.headers.get('Location', ''))
                        method, kwargs = 'GET', {}
                        continue
                    response.raise_for_status()
                    chunks, size = [], 0
                    for chunk in response.iter_content(32768):
                        size += len(chunk)
                        if size > MAX_HTML:
                            raise MailError('邮件内容过大，请在学校邮箱查看。', 'mail_too_large')
                        chunks.append(chunk)
                    raw = b''.join(chunks)
                    encoding = response.encoding if response.encoding and response.encoding.lower() != 'iso-8859-1' else 'utf-8'
                    return raw.decode(encoding, errors='replace'), response.url
            raise MailError('学校邮箱跳转次数过多，请重新连接。')
        except requests.RequestException:
            raise MailError('学校邮箱连接暂时失败，请稍后重试。') from None

    def login(self, username, password):
        if not re.fullmatch(r'[A-Za-z]\d{8,12}', username):
            raise MailError('请绑定有效的学校学生账号。', 'school_account_required', 400)
        html, url = self._request('GET', MIS + '/portal/student/index.do')
        form = soup(html).select_one('form')
        if not form or not form.select_one('input[name=password]'):
            raise MailError('MIS 登录页面发生变化，请稍后重试。')
        action = urljoin(url, form.get('action', ''))
        if urlsplit(action).hostname != 'mis.bnbu.edu.cn':
            raise MailError('无法验证 MIS 登录地址。')
        data = {x['name']: x.get('value', '') for x in form.select('input[name][type=hidden]')}
        data.update(uid=username, username=username + '@student', password=password)
        html, url = self._request('POST', action, data=data)
        if soup(html).select_one('input[type=password]') or urlsplit(url).path != '/portal/student/index.do':
            raise MailError('MIS 登录失败，请核对学校账号和密码。', 'school_login_failed', 401)
        html, url = self._request('GET', MIS + '/portal/student/email.do')
        match = re.search(r'frame_html\?sid=([A-Za-z0-9_,.-]+)', html)
        sid = (parse_qs(urlsplit(url).query).get('sid') or [None])[0]
        self.sid = match.group(1) if match else sid
        if not self.sid:
            raise MailError('MIS 未返回邮箱登录状态，请先在 MIS 中打开并激活学校邮箱。', 'mail_activation_required', 409)
        html, _ = self._request('GET', MAIL + '/cgi-bin/frame_html', params={'sid': self.sid})
        address = soup(html).select_one('#useraddrcontainer')
        expected = username.lower() + '@mail.bnbu.edu.cn'
        if not address or expected not in address.get_text(' ', strip=True).lower():
            raise MailError('邮箱账号与当前学校账号不一致，请重新连接。', 'mail_identity_mismatch', 403)

    def inbox(self, page=0):
        html, _ = self._request('GET', MAIL + '/cgi-bin/mail_list',
                                params={'sid': self.sid, 'folderid': 1, 'flag': 'new', 's': 'unread', 'page': page})
        return parse_inbox(html)

    def recent(self, now=None, max_messages=150):
        """Inspect both read and unread inbox mail from the last seven days."""
        now = now or time.time()
        cutoff, collected, seen = now - 7 * 86400, [], set()
        complete = True
        for page in range(20):
            html, _ = self._request('GET', MAIL + '/cgi-bin/mail_list', params={
                'sid': self.sid, 'folderid': 1, 's': 'inbox', 'page': page,
                'sorttype': 'time', 'sortasc': 0, 'topmails': 0})
            batch = parse_inbox(html, unread_only=False)
            dates = []
            for item in batch['messages']:
                try:
                    timestamp = datetime.fromisoformat(item['received_at']).timestamp()
                except (ValueError, TypeError):
                    complete = False
                    continue
                dates.append(timestamp)
                if cutoff <= timestamp <= now and item['id'] not in seen:
                    if len(collected) >= max_messages:
                        return {'messages': collected, 'complete': False, 'window_start': cutoff, 'window_end': now}
                    collected.append(item)
                    seen.add(item['id'])
            if not batch['has_next'] or (dates and max(dates) < cutoff):
                break
            if not batch['messages']:
                raise MailError('无法确认最近一周的邮件范围。')
        else:
            complete = False
        collected.sort(key=lambda item: item['received_at'], reverse=True)
        return {'messages': collected, 'complete': complete, 'window_start': cutoff, 'window_end': now}

    def preview(self, mail_id):
        if not MAIL_ID.fullmatch(mail_id):
            raise MailError('无效邮件。', 'invalid_mail', 400)
        html, _ = self._request('GET', MAIL + '/cgi-bin/readmail',
                                params={'sid': self.sid, 'folderid': 1, 't': 'quickreadmail',
                                        'mailid': mail_id, 'mode': 'preview'})
        return extract_preview(html)
