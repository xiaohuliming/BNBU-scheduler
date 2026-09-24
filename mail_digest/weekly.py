"""Automatic, account-scoped weekly mail briefs with encrypted result caching."""
import base64
import hashlib
import hmac
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from cryptography.fernet import Fernet, InvalidToken
from .client import SchoolMailbox, MailError
from .summary import summarize_week

REFRESH_SECONDS = 4 * 3600
RETRY_SECONDS = 3600
DISPLAY_SECONDS = 24 * 3600
MAX_JOBS = 24
LOG = logging.getLogger(__name__)


def init_tables(cursor):
    cursor.execute('''CREATE TABLE IF NOT EXISTS mail_weekly_briefs (
        user_id INTEGER PRIMARY KEY,
        school_username TEXT NOT NULL,
        payload_encrypted TEXT,
        fingerprint TEXT,
        generated_at INTEGER NOT NULL DEFAULT 0,
        checked_at INTEGER NOT NULL DEFAULT 0,
        last_attempt INTEGER NOT NULL DEFAULT 0,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )''')


class WeeklyBriefService:
    def __init__(self, get_db, secret_key, enabled, executor=None):
        self.get_db = get_db
        raw = secret_key.encode() if isinstance(secret_key, str) else secret_key
        key = hmac.new(raw, b'maxcourse-weekly-mail-brief-v1', hashlib.sha256).digest()
        self.cipher = Fernet(base64.urlsafe_b64encode(key))
        self.enabled = enabled
        self.executor = executor or ThreadPoolExecutor(max_workers=3, thread_name_prefix='mail-brief')
        self.lock = threading.Lock()
        self.jobs = set()

    def _row(self, user_id, username):
        db = self.get_db()
        try:
            return db.execute('SELECT * FROM mail_weekly_briefs WHERE user_id=? AND school_username=?',
                              (user_id, username)).fetchone()
        finally:
            db.close()

    def _owns(self, user_id, username):
        db = self.get_db()
        try:
            row = db.execute('SELECT ispace_username FROM users WHERE id=?', (user_id,)).fetchone()
            return bool(row and row['ispace_username'] == username)
        finally:
            db.close()

    def _payload(self, row):
        if not row or not row['payload_encrypted'] or time.time() - row['generated_at'] > DISPLAY_SECONDS:
            return None
        try:
            payload = json.loads(self.cipher.decrypt(row['payload_encrypted'].encode()).decode())
            if payload.pop('owner', None) != [row['user_id'], row['school_username']]:
                return None
            return payload
        except (InvalidToken, ValueError, TypeError, AttributeError):
            return None

    def status(self, user_id, username):
        with self.lock:
            working = (user_id, username) in self.jobs
        row = self._row(user_id, username)
        payload = self._payload(row)
        if payload:
            return {'state': 'ready', 'brief': payload, 'updating': working}
        return {'state': 'working' if working else 'idle'}

    def should_refresh(self, user_id, username):
        if not self.enabled():
            return False
        row = self._row(user_id, username)
        now = time.time()
        return not row or (now - row['checked_at'] >= REFRESH_SECONDS and now - row['last_attempt'] >= RETRY_SECONDS)

    def start(self, user_id, username, password):
        try:
            return self._start(user_id, username, password)
        except Exception:
            LOG.warning('mail_brief_schedule_failed user_id=%s', user_id)
            return False

    def _start(self, user_id, username, password):
        """Return immediately. Passwords live only as encrypted, bounded job data."""
        if not isinstance(password, str) or not password or not self.should_refresh(user_id, username):
            return False
        key = (user_id, username)
        with self.lock:
            if key in self.jobs or len(self.jobs) >= MAX_JOBS:
                return False
            self.jobs.add(key)
        try:
            if not self._owns(user_id, username):
                with self.lock:
                    self.jobs.discard(key)
                return False
            now = int(time.time())
            db = self.get_db()
            try:
                db.execute('''INSERT INTO mail_weekly_briefs (user_id,school_username,last_attempt)
                    VALUES (?,?,?) ON CONFLICT(user_id) DO UPDATE SET
                    payload_encrypted=CASE WHEN school_username=excluded.school_username THEN payload_encrypted ELSE NULL END,
                    fingerprint=CASE WHEN school_username=excluded.school_username THEN fingerprint ELSE NULL END,
                    checked_at=CASE WHEN school_username=excluded.school_username THEN checked_at ELSE 0 END,
                    generated_at=CASE WHEN school_username=excluded.school_username THEN generated_at ELSE 0 END,
                    school_username=excluded.school_username,last_attempt=excluded.last_attempt''', (user_id, username, now))
                db.commit()
            finally:
                db.close()
            sealed = self.cipher.encrypt(password.encode())
            self.executor.submit(self._run, user_id, username, sealed, now)
            return True
        except Exception:
            with self.lock:
                self.jobs.discard(key)
            LOG.warning('mail_brief_schedule_failed user_id=%s', user_id)
            return False

    def _save(self, user_id, username, payload, fingerprint, generated_at, checked_at):
        # Authenticate the cached record's owner inside the ciphertext too.
        sealed = self.cipher.encrypt(json.dumps({'owner': [user_id, username], **payload}, ensure_ascii=False).encode()).decode()
        db = self.get_db()
        try:
            db.execute('''UPDATE mail_weekly_briefs SET payload_encrypted=?,fingerprint=?,generated_at=?,checked_at=?
                WHERE user_id=? AND school_username=? AND EXISTS
                (SELECT 1 FROM users WHERE id=? AND ispace_username=?)''',
                       (sealed, fingerprint, generated_at, checked_at, user_id, username, user_id, username))
            db.commit()
        finally:
            db.close()

    def _run(self, user_id, username, sealed_password, queued_at):
        mailbox = None
        try:
            if time.time() - queued_at > 600 or not self._owns(user_id, username):
                return
            mailbox = SchoolMailbox()
            password = self.cipher.decrypt(sealed_password).decode()
            mailbox.login(username, password)
            password = None
            sealed_password = None
            now = int(time.time())
            recent = mailbox.recent(now=now)
            metadata = recent['messages']
            fingerprint = hashlib.sha256(json.dumps(metadata, sort_keys=True).encode()).hexdigest()
            old = self._row(user_id, username)
            old_payload = self._payload(old)
            # Even unchanged mail needs a fresh date interpretation each day.
            if old_payload and old['fingerprint'] == fingerprint and now - old['generated_at'] < 12 * 3600:
                self._save(user_id, username, old_payload, fingerprint, old['generated_at'], now)
                return
            messages, unreadable = [], 0
            limit = min(4000, 180000 // max(1, len(metadata)))
            for item in metadata:
                if time.time() - now > 360:
                    raise MailError('邮件读取超时。', 'mail_read_timeout')
                try:
                    preview = mailbox.preview(item['id'])
                except MailError as exc:
                    if exc.code in ('mail_session_expired', 'mail_identity_mismatch'):
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
            mailbox.close()
            mailbox = None
            if not self._owns(user_id, username):
                return
            highlights = summarize_week(messages, recent['window_start'], recent['window_end']) if messages else []
            indexed = {item['id']: item for item in metadata}
            items = [{'text': line['text'], 'sources': [
                {k: indexed[mid][k] for k in ('subject', 'sender', 'received_at')}
                for mid in line['source_ids']]} for line in highlights]
            payload = {'items': items, 'mail_count': len(metadata), 'complete': recent['complete'],
                       'limited_content': unreadable > 0 or any(m['body_truncated'] for m in messages),
                       'window_start': int(recent['window_start']), 'window_end': now, 'generated_at': int(time.time())}
            self._save(user_id, username, payload, fingerprint, payload['generated_at'], now)
        except Exception as exc:
            code = exc.code if isinstance(exc, MailError) else 'unexpected_error'
            LOG.warning('mail_brief_failed user_id=%s code=%s', user_id, code)
        finally:
            if mailbox:
                mailbox.close()
            with self.lock:
                self.jobs.discard((user_id, username))
