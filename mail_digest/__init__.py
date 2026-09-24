"""Account-scoped visit refresh; polling never starts another read or model call."""
import re
import secrets
from flask import Blueprint, jsonify, redirect, request, session
from ispace_credentials import decrypt_ispace_password, ISpaceCredentialError
from .summary import configured
from .weekly import WeeklyBriefService, init_tables


def clear_session():
    for key in ('mail_digest_key', 'mail_digest_csrf', 'mail_brief_csrf', 'mail_brief_visit'):
        session.pop(key, None)


def register_mail_brief(app, get_db):
    enabled = lambda: configured() and (not app.testing or app.config.get('MAIL_BRIEF_TEST_JOBS', False))
    service = WeeklyBriefService(get_db, app.secret_key, enabled)
    app.extensions['mail_brief'] = service
    bp = Blueprint('mail_brief', __name__)

    @bp.get('/mail-summary/')
    @bp.get('/mail-summary/index.html')
    def retired_tool():
        return redirect('/')

    def user():
        db = get_db()
        try:
            return db.execute('SELECT id,ispace_username,ispace_password_encrypted,mail_brief_enabled FROM users WHERE id=?', (session.get('user_id'),)).fetchone()
        finally:
            db.close()

    def result(row):
        if not row:
            return jsonify({'state': 'idle'}), 401
        token = session.setdefault('mail_brief_csrf', secrets.token_urlsafe(32))
        data = service.status(row['id'], row['ispace_username']) if row['ispace_username'] and row['mail_brief_enabled'] else {
            'state': 'idle', 'brief': None, 'updating': False, 'connection_ready': False, 'reauth_required': False}
        response = jsonify({**data, 'enabled': bool(row['mail_brief_enabled']), 'user_id': row['id'], 'csrf': token})
        response.headers['Cache-Control'] = 'no-store'
        return response

    @bp.get('/api/mail-brief')
    def status():
        return result(user())

    def csrf_valid():
        expected = session.get('mail_brief_csrf', '')
        supplied = request.headers.get('X-Mail-CSRF', '')
        return bool(expected and secrets.compare_digest(expected.encode(), supplied.encode()))

    @bp.put('/api/mail-brief/settings')
    def settings():
        row = user()
        if not row:
            return jsonify({'error': '请先登录。'}), 401
        if not csrf_valid():
            return jsonify({'error': '页面验证已过期，请刷新。'}), 403
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or type(body.get('enabled')) is not bool:
            return jsonify({'error': '请选择开启或关闭。'}), 400
        service.set_enabled(row['id'], body['enabled'])
        session.pop('mail_brief_visit', None)
        return result(user())

    @bp.post('/api/mail-brief/refresh')
    def refresh():
        row = user()
        if not row:
            return jsonify({'error': '请先登录。'}), 401
        if not csrf_valid():
            return jsonify({'error': '页面验证已过期，请刷新。'}), 403
        body = request.get_json(silent=True)
        visit = body.get('visit_id') if isinstance(body, dict) else None
        if not isinstance(visit, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,80}', visit):
            return jsonify({'error': '访问标识无效。'}), 400
        if row['mail_brief_enabled'] and session.get('mail_brief_visit') != visit and row['ispace_username']:
            password = None
            state = service.status(row['id'], row['ispace_username'])
            if not state['updating'] and row['ispace_password_encrypted']:
                try:
                    password = decrypt_ispace_password(row['ispace_password_encrypted'])
                except ISpaceCredentialError:
                    pass
            service.start(row['id'], row['ispace_username'], password)
            password = None
            session['mail_brief_visit'] = visit
        return result(user())

    app.register_blueprint(bp)
    return service
