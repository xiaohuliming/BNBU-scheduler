"""Automatic weekly mail brief embedded in the signed-in homepage."""
from flask import Blueprint, jsonify, redirect, session
from ispace_credentials import decrypt_ispace_password, ISpaceCredentialError
from .summary import configured
from .weekly import WeeklyBriefService, init_tables


def clear_session():
    # Remove legacy browser-session references without retaining mailbox state.
    session.pop('mail_digest_key', None)
    session.pop('mail_digest_csrf', None)


def register_mail_brief(app, get_db):
    enabled = lambda: configured() and (not app.testing or app.config.get('MAIL_BRIEF_TEST_JOBS', False))
    service = WeeklyBriefService(get_db, app.secret_key, enabled)
    app.extensions['mail_brief'] = service
    bp = Blueprint('mail_brief', __name__)

    @bp.get('/mail-summary/')
    @bp.get('/mail-summary/index.html')
    def retired_tool():
        return redirect('/')

    @bp.get('/api/mail-brief')
    def brief():
        user_id = session.get('user_id')
        if not user_id:
            return jsonify({'state': 'idle'}), 401
        db = get_db()
        try:
            user = db.execute('SELECT id,ispace_username,ispace_password_encrypted FROM users WHERE id=?',
                              (user_id,)).fetchone()
        finally:
            db.close()
        if not user or not user['ispace_username']:
            return jsonify({'state': 'idle', 'user_id': user_id})
        username = user['ispace_username']
        # Existing opt-in encrypted sync credentials let returning users refresh
        # silently. Otherwise the next successful school login supplies a one-off
        # password; no additional login form or password persistence is added.
        if user['ispace_password_encrypted'] and service.should_refresh(user_id, username):
            try:
                password = decrypt_ispace_password(user['ispace_password_encrypted'])
                service.start(user_id, username, password)
                password = None
            except ISpaceCredentialError:
                pass
        response = jsonify({**service.status(user_id, username), 'user_id': user_id})
        response.headers['Cache-Control'] = 'no-store'
        return response

    app.register_blueprint(bp)
    return service
