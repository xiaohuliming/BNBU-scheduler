"""Per-visit weekly briefs using short-lived mailbox sessions, never saved passwords."""
import base64
import hashlib
import hmac
import json
import logging
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from cryptography.fernet import Fernet, InvalidToken
from .client import SchoolMailbox, MailError
from .summary import summarize_week

DISPLAY_SECONDS = 24 * 3600
SESSION_SECONDS = 8 * 3600
MAX_JOBS = 24
MAX_SESSIONS = 256
LOG = logging.getLogger(__name__)


def init_tables(cursor):
    for column in ('mail_brief_enabled INTEGER NOT NULL DEFAULT 1',
                   'mail_brief_version INTEGER NOT NULL DEFAULT 0'):
        try:
            cursor.execute(f'ALTER TABLE users ADD COLUMN {column}')
        except sqlite3.OperationalError:
            pass
    cursor.execute('''CREATE TABLE IF NOT EXISTS mail_weekly_briefs (
        user_id INTEGER PRIMARY KEY, school_username TEXT NOT NULL,
        payload_encrypted TEXT, fingerprint TEXT,
        generated_at INTEGER NOT NULL DEFAULT 0, checked_at INTEGER NOT NULL DEFAULT 0,
        last_attempt INTEGER NOT NULL DEFAULT 0, FOREIGN KEY(user_id) REFERENCES users(id)
    )''')


@dataclass
class MailboxSession:
    mailbox: object
    username: str
    version: tuple
    expires_at: float
    revoked: threading.Event = field(default_factory=threading.Event)
    busy: bool = False


class WeeklyBriefService:
    def __init__(self, get_db, secret_key, enabled, executor=None):
        self.get_db = get_db
        raw = secret_key.encode() if isinstance(secret_key, str) else secret_key
        key = hmac.new(raw, b'maxcourse-weekly-mail-brief-v1', hashlib.sha256).digest()
        self.cipher = Fernet(base64.urlsafe_b64encode(key))
        self.enabled = enabled
        self.executor = executor or ThreadPoolExecutor(max_workers=3, thread_name_prefix='mail-brief')
        self.lock = threading.RLock()
        self.jobs = {}
        self.slots = threading.BoundedSemaphore(MAX_JOBS)
        self.sessions = {}
        if executor is None and enabled():
            threading.Thread(target=self._housekeeping, daemon=True, name='mail-session-expiry').start()

    def _housekeeping(self):
        while True:
            time.sleep(60)
            with self.lock:
                self._prune()

    def _prune(self):
        now = time.monotonic()
        for uid, lease in list(self.sessions.items()):
            if lease.expires_at <= now or lease.revoked.is_set():
                self.sessions.pop(uid, None)
                lease.revoked.set()
                if not lease.busy:
                    lease.mailbox.close()

    def revoke(self, user_id):
        with self.lock:
            for key, cancelled in list(self.jobs.items()):
                if key[0] == user_id:
                    cancelled.set()
                    self.jobs.pop(key, None)
            lease = self.sessions.pop(user_id, None)
            if lease:
                lease.revoked.set()
                if not lease.busy:
                    lease.mailbox.close()

    def _identity(self, user_id, username):
        db = self.get_db()
        try:
            row = db.execute('''SELECT ispace_username,COALESCE(ispace_link_version,0) AS version,
                             mail_brief_enabled,mail_brief_version FROM users WHERE id=?''',
                             (user_id,)).fetchone()
            if row and row['ispace_username'] == username and row['mail_brief_enabled']:
                return int(row['version']), int(row['mail_brief_version'])
            return None
        finally:
            db.close()

    def set_enabled(self, user_id, enabled):
        # A separate generation prevents stale work surviving off/on without
        # invalidating the user's independent iSpace/DDL binding generation.
        with self.lock:
            db = self.get_db()
            try:
                db.execute('BEGIN IMMEDIATE')
                changed = db.execute('''UPDATE users SET mail_brief_enabled=?,
                    mail_brief_version=mail_brief_version+1
                    WHERE id=? AND mail_brief_enabled<>?''', (int(enabled), user_id, int(enabled))).rowcount
                if not enabled:
                    db.execute('DELETE FROM mail_weekly_briefs WHERE user_id=?', (user_id,))
                db.commit()
            finally:
                db.close()
            if changed or not enabled:
                self.revoke(user_id)

    def _owns(self, user_id, username, version):
        return self._identity(user_id, username) == version

    def _row(self, user_id, username):
        db = self.get_db()
        try:
            return db.execute('SELECT * FROM mail_weekly_briefs WHERE user_id=? AND school_username=?',
                              (user_id, username)).fetchone()
        finally:
            db.close()

    def _payload(self, row):
        if not row or not row['payload_encrypted'] or time.time() - row['generated_at'] > DISPLAY_SECONDS:
            return None
        try:
            payload = json.loads(self.cipher.decrypt(row['payload_encrypted'].encode()).decode())
            if payload.pop('owner', None) != [row['user_id'], row['school_username']]:
                return None
            payload['checked_at'] = row['checked_at']
            return payload
        except (InvalidToken, ValueError, TypeError, AttributeError):
            return None

    def status(self, user_id, username):
        with self.lock:
            version = self._identity(user_id, username)
            if version is None:
                return {'state': 'idle', 'brief': None, 'updating': False,
                        'connection_ready': False, 'reauth_required': False}
            self._prune()
            lease = self.sessions.get(user_id)
            available = bool(lease and lease.username == username and lease.version == version)
            working = (user_id, username, version) in self.jobs
            payload = self._payload(self._row(user_id, username))
        return {'state': 'ready' if payload else ('working' if working else 'idle'),
                'brief': payload, 'updating': working, 'connection_ready': available,
                'reauth_required': not available and not working}

    def start(self, user_id, username, password=None):
        """Login supplies a one-off password; later visits reuse only the mailbox session."""
        try:
            return self._start(user_id, username, password)
        except Exception:
            LOG.warning('mail_brief_schedule_failed user_id=%s', user_id)
            return False

    def _start(self, user_id, username, password):
        if not self.enabled():
            return False
        version = self._identity(user_id, username)
        if version is None:
            return False
        key = (user_id, username, version)
        with self.lock:
            self._prune()
            if key in self.jobs or len(self.jobs) >= MAX_JOBS:
                return False
            lease = self.sessions.get(user_id)
            if lease and (lease.username != username or lease.version != version):
                self.revoke(user_id)
                lease = None
            if lease and isinstance(password, str) and password:
                # An explicit login or opted-in saved password can renew the
                # provider session immediately instead of discovering expiry
                # midway through a visit and requiring another page refresh.
                self.revoke(user_id)
                lease = None
            if not lease and (not isinstance(password, str) or not password):
                return False
            if not self.slots.acquire(blocking=False):
                return False
            cancelled = threading.Event()
            self.jobs[key] = cancelled
        try:
            now = int(time.time())
            db = self.get_db()
            try:
                result = db.execute('''INSERT INTO mail_weekly_briefs (user_id,school_username,last_attempt)
                    SELECT ?,?,? WHERE EXISTS (SELECT 1 FROM users WHERE id=? AND ispace_username=?
                    AND COALESCE(ispace_link_version,0)=? AND mail_brief_version=? AND mail_brief_enabled=1)
                    ON CONFLICT(user_id) DO UPDATE SET
                    payload_encrypted=CASE WHEN school_username=excluded.school_username THEN payload_encrypted ELSE NULL END,
                    fingerprint=CASE WHEN school_username=excluded.school_username THEN fingerprint ELSE NULL END,
                    checked_at=CASE WHEN school_username=excluded.school_username THEN checked_at ELSE 0 END,
                    generated_at=CASE WHEN school_username=excluded.school_username THEN generated_at ELSE 0 END,
                    school_username=excluded.school_username,last_attempt=excluded.last_attempt''', (user_id, username, now, user_id, username, *version))
                if not result.rowcount:
                    raise MailError('Account binding changed', 'mail_cancelled')
                db.commit()
            finally:
                db.close()
            sealed = self.cipher.encrypt(password.encode()) if not lease else None
            self.executor.submit(self._run, user_id, username, version, lease, sealed, now, cancelled)
            return True
        except Exception:
            with self.lock:
                if self.jobs.get(key) is cancelled:
                    self.jobs.pop(key, None)
            self.slots.release()
            raise

    def _save(self, user_id, username, version, payload, fingerprint, generated_at, checked_at):
        sealed = self.cipher.encrypt(json.dumps({'owner': [user_id, username], **payload}, ensure_ascii=False).encode()).decode()
        db = self.get_db()
        try:
            db.execute('''UPDATE mail_weekly_briefs SET payload_encrypted=?,fingerprint=?,generated_at=?,checked_at=?
                WHERE user_id=? AND school_username=? AND EXISTS
                (SELECT 1 FROM users WHERE id=? AND ispace_username=? AND COALESCE(ispace_link_version,0)=?
                AND mail_brief_version=? AND mail_brief_enabled=1)''',
                       (sealed, fingerprint, generated_at, checked_at, user_id, username, user_id, username, *version))
            db.commit()
        finally:
            db.close()

    def _run(self, user_id, username, version, lease, sealed_password, queued_at, cancellation):
        key = (user_id, username, version)
        mailbox = None
        try:
            if cancellation.is_set() or time.time() - queued_at > 600 or not self._owns(user_id, username, version):
                return
            if lease is None:
                mailbox = SchoolMailbox()
                mailbox.cancelled = lambda: cancellation.is_set() or not self._owns(user_id, username, version)
                password = self.cipher.decrypt(sealed_password).decode()
                mailbox.login(username, password)
                password = sealed_password = None
                lease = MailboxSession(mailbox, username, version, time.monotonic() + SESSION_SECONDS, busy=True)
                with self.lock:
                    self._prune()
                    if len(self.sessions) >= MAX_SESSIONS:
                        raise MailError('Mailbox capacity reached', 'mail_busy')
                    if cancellation.is_set():
                        lease.revoked.set()
                        return
                    self.sessions[user_id] = lease
            lease.busy = True
            mailbox = lease.mailbox
            def cancelled():
                return cancellation.is_set() or lease.revoked.is_set() or time.monotonic() >= lease.expires_at or not self._owns(user_id, username, version)
            mailbox.cancelled = cancelled
            now = int(time.time())
            recent = mailbox.recent(now=now)
            metadata = recent['messages']
            fingerprint = hashlib.sha256(json.dumps(metadata, sort_keys=True).encode()).hexdigest()
            messages, unreadable = [], 0
            limit = min(4000, 180000 // max(1, len(metadata)))
            for item in metadata:
                if cancelled():
                    raise MailError('Mailbox connection was removed', 'mail_cancelled')
                if time.time() - now > 360:
                    raise MailError('Mail read timed out', 'mail_read_timeout')
                try:
                    preview = mailbox.preview(item['id'])
                except MailError as exc:
                    if exc.code in ('mail_session_expired', 'mail_identity_mismatch', 'mail_cancelled'):
                        raise
                    unreadable += 1
                    preview = {'body': item['snippet'], 'body_truncated': True, 'has_images': False}
                body = preview['body']
                truncated = len(body) > limit
                if truncated:
                    head = limit * 3 // 4
                    body = body[:head] + '\n[中间内容省略]\n' + body[-(limit - head):]
                messages.append({**{k: v for k, v in item.items() if k != 'snippet'},
                                 **preview, 'body': body, 'body_truncated': preview['body_truncated'] or truncated})
            if cancelled():
                return
            # A distinct visit gets a fresh date-aware summary, even if the mail IDs are unchanged.
            highlights = summarize_week(messages, recent['window_start'], recent['window_end']) if messages else []
            indexed = {item['id']: item for item in metadata}
            items = [{'text': line['text'], 'sources': [
                {k: indexed[mid][k] for k in ('subject', 'sender', 'received_at')}
                for mid in line['source_ids']]} for line in highlights]
            payload = {'items': items, 'mail_count': len(metadata), 'complete': recent['complete'],
                       'limited_content': unreadable > 0 or any(m['body_truncated'] for m in messages),
                       'window_start': int(recent['window_start']), 'window_end': now, 'generated_at': int(time.time())}
            if not cancelled():
                self._save(user_id, username, version, payload, fingerprint, payload['generated_at'], now)
        except Exception as exc:
            code = exc.code if isinstance(exc, MailError) else 'unexpected_error'
            if lease and code in ('mail_session_expired', 'mail_identity_mismatch', 'mail_cancelled'):
                lease.revoked.set()
            LOG.warning('mail_brief_failed user_id=%s code=%s', user_id, code)
        finally:
            with self.lock:
                if self.jobs.get(key) is cancellation:
                    self.jobs.pop(key, None)
                if lease:
                    lease.busy = False
                if lease and (cancellation.is_set() or lease.revoked.is_set() or lease.expires_at <= time.monotonic()
                              or not self._owns(user_id, username, version)):
                    if self.sessions.get(user_id) is lease:
                        self.sessions.pop(user_id, None)
                    lease.mailbox.close()
                elif mailbox and (not lease or self.sessions.get(user_id) is not lease):
                    mailbox.close()
            self.slots.release()
