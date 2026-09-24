"""User-scoped, short-lived mail sessions; no mailbox data in SQLite."""
import secrets
import threading
import time
from contextlib import contextmanager

from flask import Blueprint, jsonify, request, session, send_from_directory

import sso_bridge
from ispace_credentials import decrypt_ispace_password, ISpaceCredentialError, is_ispace_credential_encryption_configured
from .client import SchoolMailbox, MailError
from .summary import omni_request, summarize

TTL = 600
_sessions = {}
_lock = threading.Lock()
_connecting = set()


def _prune():
    now = time.monotonic()
    for key, entry in list(_sessions.items()):
        if entry['expires'] < now and entry['lock'].acquire(blocking=False):
            try:
                _sessions.pop(key, None)
                entry['mailbox'].close()
            finally:
                entry['lock'].release()


def clear_session():
    key = session.pop('mail_digest_key', None)
    with _lock:
        entry = _sessions.get(key)
        if entry:
            entry['expires'] = 0
        _prune()
    session.pop('mail_digest_csrf', None)


@contextmanager
def _entry(user):
    with _lock:
        _prune()
        entry = _sessions.get(session.get('mail_digest_key'))
        if not entry or entry['user_id'] != user['id'] or entry['username'] != user['ispace_username']:
            raise MailError('邮箱连接已过期，请重新连接。', 'mail_session_expired', 401)
        if not entry['lock'].acquire(blocking=False):
            raise MailError('上一个邮件请求仍在处理中，请稍候。', 'mail_busy', 409)
    try:
        yield entry
    finally:
        entry['lock'].release()
        with _lock:
            _prune()


def create_blueprint(get_db):
    bp = Blueprint('mail_digest', __name__)

    def current_user():
        conn = get_db()
        try:
            row = conn.execute('SELECT id, username, ispace_username, ispace_password_encrypted FROM users WHERE id = ?',
                               (session.get('user_id'),)).fetchone()
        finally:
            conn.close()
        if not row:
            raise MailError('请先登录 MAXCOURSE。', 'login_required', 401)
        if not row['ispace_username']:
            raise MailError('请先在账号设置中绑定 iSpace 账号。', 'school_account_required', 409)
        return row

    def shared_token(user):
        token = request.cookies.get('sso_token')
        shared = sso_bridge.shared_user_for_token(token)
        if not shared or shared['username'] != user['username']:
            raise MailError('请重新登录 MAXCOURSE，以连接同一 OmniChat 账号。', 'shared_login_required', 401)
        return token

    @bp.before_request
    def protect():
        if request.path.startswith('/api/mail-digest/') and request.method == 'POST':
            if not session.get('user_id'):
                raise MailError('请先登录 MAXCOURSE。', 'login_required', 401)
            expected = session.get('mail_digest_csrf')
            actual = request.headers.get('X-Mail-CSRF', '')
            if not expected or not secrets.compare_digest(expected, actual):
                raise MailError('页面验证已过期，请刷新后重试。', 'csrf_failed', 403)
            if not request.is_json:
                raise MailError('请使用 JSON 请求。', 'invalid_request', 400)

    @bp.errorhandler(MailError)
    def error(exc):
        return jsonify({'error': str(exc), 'code': exc.code}), exc.status

    @bp.get('/mail-summary/')
    def page():
        return send_from_directory('mail-summary', 'index.html')

    @bp.get('/api/mail-digest/status')
    def status():
        user = current_user()
        if 'mail_digest_csrf' not in session:
            session['mail_digest_csrf'] = secrets.token_urlsafe(32)
        with _lock:
            _prune()
            entry = _sessions.get(session.get('mail_digest_key'))
            connected = bool(entry and entry['user_id'] == user['id'] and entry['username'] == user['ispace_username'])
        return jsonify({'username': user['ispace_username'], 'connected': connected,
                        'credential_saved': bool(user['ispace_password_encrypted']) and is_ispace_credential_encryption_configured(),
                        'csrf': session['mail_digest_csrf'], 'max_messages': 10, 'session_minutes': TTL // 60})

    @bp.get('/api/mail-digest/ai-status')
    def ai_status():
        return jsonify(omni_request(shared_token(current_user())))

    def payload():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise MailError('请求格式无效。', 'invalid_request', 400)
        return data

    @bp.post('/api/mail-digest/connect')
    def connect():
        user, data = current_user(), payload()
        password = data.get('password', '')
        if data.get('use_saved_password') is True:
            try:
                password = decrypt_ispace_password(user['ispace_password_encrypted'])
            except ISpaceCredentialError:
                raise MailError('已保存密码无法使用，请手动输入学校密码。', 'password_required', 400) from None
        if not isinstance(password, str) or not password or len(password) > 512:
            raise MailError('请输入学校密码。', 'password_required', 400)
        with _lock:
            _prune()
            if user['id'] in _connecting or len(_sessions) + len(_connecting) >= 64:
                raise MailError('邮箱连接繁忙，请稍后重试。', 'mail_busy', 429)
            _connecting.add(user['id'])
        mailbox = SchoolMailbox()
        try:
            mailbox.login(user['ispace_username'], password)
            inbox = mailbox.inbox()
            clear_session()
            session['mail_digest_csrf'] = secrets.token_urlsafe(32)
            key = secrets.token_urlsafe(32)
            with _lock:
                _sessions[key] = {'user_id': user['id'], 'username': user['ispace_username'],
                                  'mailbox': mailbox, 'inbox': inbox, 'page': 0, 'bodies': {}, 'digests': {},
                                  'lock': threading.Lock(), 'expires': time.monotonic() + TTL}
            session['mail_digest_key'] = key
            response = jsonify({**inbox, 'page': 0, 'csrf': session['mail_digest_csrf']})
            # A long-lived MAXCOURSE session may outlive the shared SSO token.
            # MIS just reverified this bound school identity, so refresh the
            # same shared account using the existing login identity rules.
            sso_bridge.set_sso_cookie(response, sso_bridge.issue_shared_token(
                user['username'], ispace=(user['username'] == user['ispace_username'])))
            return response
        except Exception:
            mailbox.close()
            raise
        finally:
            password = None
            with _lock:
                _connecting.discard(user['id'])

    @bp.post('/api/mail-digest/inbox')
    def inbox():
        user, data = current_user(), payload()
        page_number = data.get('page', 0)
        if type(page_number) is not int or page_number < 0 or page_number > 10000:
            raise MailError('邮件页码无效。', 'invalid_page', 400)
        with _entry(user) as entry:
            result = entry['mailbox'].inbox(page_number)
            entry.update(inbox=result, page=page_number, bodies={}, digests={})
            return jsonify({**result, 'page': page_number})

    @bp.post('/api/mail-digest/summarize')
    def summary():
        user, data = current_user(), payload()
        token = shared_token(user)
        ids = data.get('ids')
        if (not isinstance(ids, list) or not 1 <= len(ids) <= 10
                or not all(isinstance(mid, str) for mid in ids) or len(set(ids)) != len(ids)):
            raise MailError('每次请选择 1 至 10 封邮件。', 'invalid_selection', 400)
        with _entry(user) as entry:
            cache_key = tuple(sorted(ids))
            if cache_key in entry['digests']:
                return jsonify({**entry['digests'][cache_key], 'cached': True})
            indexed = {m['id']: m for m in entry['inbox']['messages']}
            if not set(ids).issubset(indexed):
                raise MailError('只能总结当前批次的邮件，请重新选择。', 'invalid_selection', 400)
            # Preflight auth/config before fetching body text or starting a charge.
            omni_request(token)
            messages = []
            for mid in ids:
                if mid not in entry['bodies']:
                    entry['bodies'][mid] = entry['mailbox'].preview(mid)
                messages.append({**{k: v for k, v in indexed[mid].items() if k != 'snippet'}, **entry['bodies'][mid]})
            result = {'digest': summarize(token, messages), 'messages': messages,
                      'summarized_count': len(messages), 'total_unread': entry['inbox']['total_unread'],
                      'generated_at': int(time.time()), 'page': entry['page']}
            entry['digests'][cache_key] = result
            return jsonify({**result, 'cached': False})

    @bp.post('/api/mail-digest/disconnect')
    def disconnect():
        clear_session()
        return jsonify({'success': True})

    return bp
