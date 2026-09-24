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
            return db.execute('SELECT id,ispace_username,ispace_password_encrypted FROM users WHERE id=?', (session.get('user_id'),)).fetchone()
        finally:
            db.close()

    def result(row):
        if not row:
            return jsonify({'state': 'idle'}), 401
        token = session.setdefault('mail_brief_csrf', secrets.token_urlsafe(32))
        data = service.status(row['id'], row['ispace_username']) if row['ispace_username'] else {'state': 'idle'}
        response = jsonify({**data, 'user_id': row['id'], 'csrf': token})
        response.headers['Cache-Control'] = 'no-store'
        return response

    @bp.get('/api/mail-brief')
    def status():
        return result(user())

    @bp.post('/api/mail-brief/refresh')
    def refresh():
        row = user()
        if not row:
            return jsonify({'error': '请先登录。'}), 401
        expected = session.get('mail_brief_csrf', '')
        supplied = request.headers.get('X-Mail-CSRF', '')
        if not expected or not secrets.compare_digest(expected.encode(), supplied.encode()):
            return jsonify({'error': '页面验证已过期，请刷新。'}), 403
        body = request.get_json(silent=True)
        visit = body.get('visit_id') if isinstance(body, dict) else None
        if not isinstance(visit, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,80}', visit):
            return jsonify({'error': '访问标识无效。'}), 400
        if session.get('mail_brief_visit') != visit and row['ispace_username']:
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
        return result(row)

    app.register_blueprint(bp)
    return service
