import os
import glob
import gzip
import io
import json
import math
import posixpath
import re
import sqlite3
import secrets
import smtplib
import ssl
import threading
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from html import escape
from flask import Flask, abort, request, jsonify, send_from_directory, session
import sso_bridge
from werkzeug.exceptions import HTTPException
from werkzeug.security import generate_password_hash, check_password_hash
import pandas as pd
from maximize_credits import load_timetable, maximize_credits, fmt_meeting, parse_schedule
from crawler import fetch_timeline
from media_dl import media_dl_bp

# Database setup
DB_PATH = 'maxcourse.db'
APP_ROOT = os.path.dirname(os.path.abspath(__file__))

# page_views.created_at is stored in UTC (SQLite CURRENT_TIMESTAMP). The site
# serves a Beijing-time (UTC+8) audience, so every "today" / day / hour bucket
# must be shifted before it is grouped, or the day would roll over at 08:00 local.
BEIJING_SQL_OFFSET = '+8 hours'
SECRET_KEY_FILE = os.path.join(APP_ROOT, '.flask_secret_key')
SESSION_LIFETIME_DAYS = 36500
DAY_SEQUENCE = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
DAY_MAP = {day: index for index, day in enumerate(DAY_SEQUENCE)}
DAY_LABELS = {
    "Mon": "周一",
    "Tue": "周二",
    "Wed": "周三",
    "Thu": "周四",
    "Fri": "周五",
    "Sat": "周六",
    "Sun": "周日",
}
SCHOOL_DAY_END_MINUTES = 21 * 60 + 50
EXCLUDED_FREE_CLASSROOM_BUILDINGS = {'V22', 'V20', 'UC', 'SP'}
PRIORITY_BUILDING_ORDER = ['T8', 'T7', 'T6', 'T5', 'T4', 'T29']
CLASSROOM_INTENT_PURPOSES = ('study', 'discussion', 'practice', 'other')
CLASSROOM_INTENT_GRACE_MINUTES = 15
CLASSROOM_INTENT_MAX_DAYS_AHEAD = 14
CLASSROOM_INTENT_MAX_ACTIVE_PER_USER = 3
CLASSROOM_INTENT_MAX_PARTY_SIZE = 50
EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
EMAIL_REMINDER_CHOICES = [72, 24, 3, 1]
DEFAULT_EMAIL_REMINDER_HOURS = [24, 3]
DEFAULT_EMAIL_REMINDER_VALUE = ','.join(str(hour) for hour in DEFAULT_EMAIL_REMINDER_HOURS)
DEFAULT_PUBLIC_BASE_URL = 'https://www.bnbscheduler.top'
BEIJING_TZ = timezone(timedelta(hours=8))
EMAIL_MAX_DELIVERY_ATTEMPTS = 3


def load_or_create_secret_key():
    env_key = os.getenv('MAXCOURSE_SECRET_KEY')
    if env_key:
        return env_key

    try:
        with open(SECRET_KEY_FILE, 'r', encoding='utf-8') as file:
            secret_key = file.read().strip()
            if secret_key:
                return secret_key
    except FileNotFoundError:
        pass

    secret_key = secrets.token_hex(32)
    with open(SECRET_KEY_FILE, 'w', encoding='utf-8') as file:
        file.write(secret_key)

    try:
        os.chmod(SECRET_KEY_FILE, 0o600)
    except OSError:
        pass

    return secret_key


import mimetypes
# `.ps1` and `.command` carry Chinese text and are fetched via `irm | iex`
# (PowerShell) and `curl | bash` (macOS). Without an explicit charset, PowerShell
# 5.1 falls back to Windows-1252 → Chinese decodes to garbage. Register them
# as text/plain; charset=utf-8 so the response header tells clients the encoding.
mimetypes.add_type('text/plain; charset=utf-8', '.ps1')
mimetypes.add_type('text/plain; charset=utf-8', '.command')

app = Flask(__name__, static_folder='.', static_url_path='')
app.secret_key = load_or_create_secret_key()
app.config.update(
    PERMANENT_SESSION_LIFETIME=timedelta(days=SESSION_LIFETIME_DAYS),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_REFRESH_EACH_REQUEST=True,
    # The largest legitimate upload is a 12 MB transcript PDF. Reject larger
    # bodies before Flask parses multipart data or expensive handlers run.
    MAX_CONTENT_LENGTH=16 * 1024 * 1024,
)


GZIP_MIN_BYTES = 1024
GZIP_MIME_PREFIXES = ('text/', 'application/json', 'application/javascript', 'application/xml', 'image/svg+xml')
LONG_CACHE_PREFIXES = ('/vendor/', '/app.compiled.js', '/tailwind.static.css',
                       '/campus-map/map.webp')  # cache-busted via ?v= in map_data.json
API_BROWSER_CACHE_EXACT = {
    '/api/courses': 300,
    '/api/semesters': 3600,
    '/api/careers': 3600,
    '/api/programmes': 3600,
    '/api/programme-courses': 3600,
    '/api/campus-docs': 300,
    '/api/teachers': 60,
}
API_BROWSER_CACHE_PREFIXES = (
    ('/api/course/', 300),
    ('/api/classroom/', 300),
)
SENSITIVE_STATIC_PREFIXES = (
    '/.git', '/.hg', '/.svn', '/__pycache__', '/backups', '/instance',
    '/tests', '/venv', '/.venv', '/logs', '/deploy',
)
SENSITIVE_STATIC_SUFFIXES = (
    '.py', '.pyc', '.pyo', '.db', '.sqlite', '.sqlite3', '.env', '.pem',
    '.key', '.crt', '.conf', '.service', '.sh', '.log', '.bak', '.orig', '.swp',
    # Data/build formats nothing in the frontend fetches directly (verified):
    # blocking them stops one-GET bulk exfiltration of the curated datasets.
    '.npz', '.jsonl', '.md', '.xlsx', '.xls',
)

# Root-level data files that back /api/* endpoints. The repo root is the web
# root (static_folder='.'), so without this list a scraper can download every
# curated dataset in a single GET instead of going through the API. Exact
# paths, because /todolist.json (legacy page) and eatwhat CSVs must stay served.
BLOCKED_STATIC_FILES = (
    '/campus_docs.json', '/course_catalog.json', '/course_descriptions_extra.json',
    '/course_enrichment.json', '/course_equivalences.json', '/course_textbooks.json',
    '/programme_requirements.json', '/semesters_index.json',
    '/requirements.txt', '/precompile.js',
)
BLOCKED_STATIC_FILE_PREFIXES = ('/course_semester_', '/skillpath_')


@app.before_request
def block_sensitive_project_files():
    """Prevent Flask's root static handler from exposing source/data files.

    Match on the *normalized* path. The static handler resolves the target via
    posixpath.normpath, so a trailing slash, doubled slashes, a '/.' suffix, or
    '..' segments would let e.g. GET /maxcourse.db/ slip past a raw-path check
    and dump the file. Normalizing here the same way closes that whole class.
    """
    raw = '/' + (request.path or '').lstrip('/')
    norm = posixpath.normpath(raw)
    norm = re.sub(r'/{2,}', '/', norm)  # normpath keeps a leading '//'
    path_lower = norm.lower()
    segments = [segment for segment in path_lower.split('/') if segment]

    if any(segment.startswith('.') and segment != '.well-known' for segment in segments):
        abort(404)
    if path_lower.startswith(SENSITIVE_STATIC_PREFIXES) or path_lower.endswith(SENSITIVE_STATIC_SUFFIXES):
        abort(404)
    if path_lower in BLOCKED_STATIC_FILES or path_lower.startswith(BLOCKED_STATIC_FILE_PREFIXES):
        abort(404)


# ---------------------------------------------------------------------------
# Anti-scraping for /api/*: a cheap HTTP-library UA filter plus a generous
# per-client rate limit. Thresholds sit far above real human/SPA usage so a
# whole campus NAT egress never trips them; only bulk enumeration does.
# Static pages are untouched — this guards the data, not the site.
# ---------------------------------------------------------------------------
SCRAPER_UA_MARKERS = (
    'python-requests', 'python-urllib', 'python/', 'aiohttp', 'httpx',
    'scrapy', 'go-http-client', 'okhttp', 'apache-httpclient', 'libwww',
    'node-fetch', 'axios', 'guzzlehttp', 'java/', 'phantomjs',
)
# Every /api request is charged to a per-IP bucket (the hard ceiling that bounds
# any single egress IP, generous so a whole campus NAT never trips it) AND, for an
# established session, a tighter per-visitor bucket. Charging the IP bucket even on
# cookied requests is deliberate: it stops a scraper from minting endless fresh
# visitor cookies to multiply its quota. A request is blocked if EITHER bucket is
# over. 0 disables that bucket (emergency env knob, restart to apply).
RATE_LIMIT_IP_PER_MIN = int(os.getenv('MAXCOURSE_RL_IP_PER_MIN', '2000'))
RATE_LIMIT_VISITOR_PER_MIN = int(os.getenv('MAXCOURSE_RL_VISITOR_PER_MIN', '240'))
RATE_LIMIT_EXEMPT_PATHS = ('/api/notifications/dispatch',)  # cron heartbeat
_RATE_COUNTER_HARD_CAP = 20000  # fail open (stop tracking new keys) beyond this

_rate_lock = threading.Lock()
_rate_counters = {}  # bucket key -> [remaining_tokens, last_refill_timestamp]
antiscrape_stats = {"uaBlocked": 0, "rateLimited": 0}


def _client_ip():
    """Best available client identity for rate-limit bucketing.

    Behind the nginx reverse proxy Flask always sees 127.0.0.1, so only then
    trust X-Real-IP (set by nginx) or the last X-Forwarded-For hop. A direct
    hit on :5000 keeps its socket address — its forwarded headers would be
    attacker-controlled and could be spoofed to poison another IP's bucket.
    """
    addr = request.remote_addr or 'unknown'
    if addr in ('127.0.0.1', '::1'):
        real = (request.headers.get('X-Real-IP') or '').strip()
        if real:
            return real
        forwarded = (request.headers.get('X-Forwarded-For') or '').strip()
        if forwarded:
            return forwarded.split(',')[-1].strip()
    return addr


@app.before_request
def throttle_api_scrapers():
    path = request.path or ''
    if not path.startswith('/api/') or path in RATE_LIMIT_EXEMPT_PATHS:
        return None

    ua = (request.headers.get('User-Agent') or '').strip().lower()
    if not ua or any(marker in ua for marker in SCRAPER_UA_MARKERS):
        with _rate_lock:
            antiscrape_stats['uaBlocked'] += 1
        return jsonify({"error": "Automated clients are not allowed on this API"}), 403

    # Give every browser API client a signed, durable visitor identity on its
    # first request. Previously only /api/analytics/track minted this value, so
    # a scraper could skip that endpoint and receive the much looser IP-only
    # allowance indefinitely.
    if 'user_id' not in session and 'analytics_visitor_id' not in session:
        get_analytics_visitor_id()

    # Always charge the IP bucket; add the per-visitor/user bucket when there is a
    # session. Reject if either is exceeded.
    buckets = [(f"ip:{_client_ip()}", RATE_LIMIT_IP_PER_MIN)]
    if session.get('user_id'):
        buckets.append((f"u:{session['user_id']}", RATE_LIMIT_VISITOR_PER_MIN))
    elif session.get('analytics_visitor_id'):
        buckets.append((f"v:{session['analytics_visitor_id']}", RATE_LIMIT_VISITOR_PER_MIN))

    now = time.time()
    over = False
    retry_after = 1
    with _rate_lock:
        # Prune fully-refilled idle buckets. If a rotating-key flood still fills
        # the table, fail open for new keys rather than grow memory without bound.
        if len(_rate_counters) > 10000:
            for stale in [k for k, v in _rate_counters.items() if now - v[1] > 180]:
                _rate_counters.pop(stale, None)
        at_capacity = len(_rate_counters) >= _RATE_COUNTER_HARD_CAP
        for key, limit in buckets:
            if limit <= 0:
                continue
            entry = _rate_counters.get(key)
            if entry is None:
                if not at_capacity:
                    _rate_counters[key] = [max(0.0, float(limit) - 1.0), now]
                continue

            elapsed = max(0.0, now - entry[1])
            refill_per_second = float(limit) / 60.0
            tokens = min(float(limit), entry[0] + elapsed * refill_per_second)
            if tokens >= 1.0:
                tokens -= 1.0
            else:
                over = True
                retry_after = max(
                    retry_after,
                    math.ceil((1.0 - tokens) / refill_per_second),
                )
            entry[0] = tokens
            entry[1] = now
        if over:
            antiscrape_stats['rateLimited'] += 1

    if not over:
        return None
    response = jsonify({"error": "Too many requests, please slow down",
                        "retry_after": retry_after})
    response.status_code = 429
    response.headers['Retry-After'] = str(retry_after)
    return response


@app.after_request
def apply_response_optimizations(response):
    path = request.path or ''
    api_cache_seconds = None
    if request.method in ('GET', 'HEAD'):
        api_cache_seconds = API_BROWSER_CACHE_EXACT.get(path)
        if api_cache_seconds is None:
            for prefix, seconds in API_BROWSER_CACHE_PREFIXES:
                if path.startswith(prefix):
                    api_cache_seconds = seconds
                    break

    if path.startswith(LONG_CACHE_PREFIXES) and response.status_code == 200:
        response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
    elif path == '/' or path.endswith('.html'):
        response.headers['Cache-Control'] = 'no-cache'
    elif api_cache_seconds and response.status_code == 200:
        # These endpoints are user-independent, but keep the response private
        # because Flask may refresh a signed session cookie on the same response.
        # ETag revalidation avoids repeatedly transferring the 1.2 MB course list.
        response.headers['Cache-Control'] = (
            f'private, max-age={api_cache_seconds}, '
            f'stale-while-revalidate={max(300, api_cache_seconds)}'
        )
        if not response.direct_passthrough:
            if 'ETag' not in response.headers:
                response.add_etag()
            response.make_conditional(request)
    elif path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-store'

    if response.status_code < 200 or response.status_code >= 300:
        return response
    if 'Content-Encoding' in response.headers:
        return response

    accept_encoding = request.headers.get('Accept-Encoding', '')
    if 'gzip' not in accept_encoding.lower():
        return response

    mime = (response.mimetype or '').lower()
    if not any(mime.startswith(prefix) for prefix in GZIP_MIME_PREFIXES):
        return response

    if response.direct_passthrough:
        try:
            data = b''.join(response.iter_encoded())
        except Exception:
            return response
        response.direct_passthrough = False
        response.set_data(data)
    else:
        data = response.get_data()

    if len(data) < GZIP_MIN_BYTES:
        return response

    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode='wb', compresslevel=6, mtime=0) as gz:
        gz.write(data)
    compressed = buffer.getvalue()
    if len(compressed) >= len(data):
        return response

    response.set_data(compressed)
    response.headers['Content-Encoding'] = 'gzip'
    response.headers['Content-Length'] = str(len(compressed))
    vary = response.headers.get('Vary', '')
    if 'Accept-Encoding' not in vary:
        response.headers['Vary'] = (vary + ', Accept-Encoding').strip(', ') if vary else 'Accept-Encoding'
    return response


@app.errorhandler(Exception)
def handle_api_errors(error):
    if not request.path.startswith('/api/'):
        return error

    if isinstance(error, HTTPException):
        return jsonify({
            "error": error.description,
            "status": error.code,
            "path": request.path,
        }), error.code

    app.logger.exception("Unhandled API error")
    return jsonify({"error": "Internal server error"}), 500


def rollup_daily_page_stats(conn, recent_days=None):
    """Upsert Beijing-day page-view totals into daily_page_stats.

    recent_days=None recomputes the full history (used for the one-time backfill);
    an int N recomputes only the last N Beijing days (cheap incremental refresh —
    past days are immutable, so refreshing the tail keeps today/yesterday current).
    Recomputed from the raw page_views table, so it is always self-correcting.
    """
    where = ''
    if recent_days is not None:
        where = (
            f"WHERE date(created_at, '{BEIJING_SQL_OFFSET}') "
            f">= date('now', '{BEIJING_SQL_OFFSET}', '-{int(recent_days)} days')"
        )
    conn.execute(
        f'''
        INSERT OR REPLACE INTO daily_page_stats (day, views, visitors, updated_at)
        SELECT date(created_at, '{BEIJING_SQL_OFFSET}') AS day,
               COUNT(*) AS views,
               COUNT(DISTINCT visitor_id) AS visitors,
               CURRENT_TIMESTAMP
        FROM page_views
        {where}
        GROUP BY day
        '''
    )


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE,
                password_hash TEXT,
                ispace_username TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS todos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                ispace_id INTEGER,
                title TEXT,
                course TEXT,
                due_date INTEGER,
                url TEXT,
                description TEXT,
                is_completed BOOLEAN DEFAULT 0,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS teacher_ratings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                teacher_name TEXT,
                user_id INTEGER,
                rating INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS page_views (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                visitor_id TEXT,
                user_id INTEGER,
                view_name TEXT,
                path TEXT,
                referrer TEXT,
                user_agent TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        ''')
        # Check if description column exists, add if not (migration)
        try:
            c.execute('ALTER TABLE todos ADD COLUMN description TEXT')
        except sqlite3.OperationalError:
            pass # Column already exists
            
        # Migration for teacher_ratings
        try:
            c.execute('ALTER TABLE teacher_ratings ADD COLUMN comment TEXT')
        except sqlite3.OperationalError:
            pass
        try:
            c.execute('ALTER TABLE teacher_ratings ADD COLUMN is_anonymous BOOLEAN DEFAULT 0')
        except sqlite3.OperationalError:
            pass
        try:
            c.execute('ALTER TABLE teacher_ratings ADD COLUMN course_info TEXT')
        except sqlite3.OperationalError:
            pass
        try:
            c.execute('ALTER TABLE users ADD COLUMN display_name TEXT')
        except sqlite3.OperationalError:
            pass
        try:
            c.execute('ALTER TABLE users ADD COLUMN email TEXT')
        except sqlite3.OperationalError:
            pass
        try:
            c.execute('ALTER TABLE users ADD COLUMN email_notifications_enabled BOOLEAN DEFAULT 0')
        except sqlite3.OperationalError:
            pass
        try:
            c.execute(f"ALTER TABLE users ADD COLUMN email_reminder_hours TEXT DEFAULT '{DEFAULT_EMAIL_REMINDER_VALUE}'")
        except sqlite3.OperationalError:
            pass
        try:
            c.execute('ALTER TABLE users ADD COLUMN unsubscribe_token TEXT')
        except sqlite3.OperationalError:
            pass
        try:
            c.execute('ALTER TABLE users ADD COLUMN email_unsubscribed_at TIMESTAMP')
        except sqlite3.OperationalError:
            pass

        try:
            c.execute('ALTER TABLE todos ADD COLUMN is_stale BOOLEAN DEFAULT 0')
        except sqlite3.OperationalError:
            pass

        c.execute('''
            CREATE TABLE IF NOT EXISTS media_dl_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                visitor_id TEXT,
                user_id INTEGER,
                action TEXT,
                platform TEXT,
                host TEXT,
                success INTEGER DEFAULT 0,
                bytes INTEGER DEFAULT 0,
                elapsed_ms INTEGER,
                error TEXT,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        ''')

        c.execute('''
            CREATE TABLE IF NOT EXISTS email_notification_deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                todo_id INTEGER,
                reminder_hours INTEGER,
                sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                success BOOLEAN DEFAULT 0,
                error TEXT,
                FOREIGN KEY(user_id) REFERENCES users(id),
                FOREIGN KEY(todo_id) REFERENCES todos(id)
            )
        ''')

        c.execute('''
            CREATE TABLE IF NOT EXISTS classroom_intents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                room TEXT NOT NULL,
                use_date TEXT NOT NULL,
                start_min INTEGER NOT NULL,
                end_min INTEGER NOT NULL,
                purpose TEXT NOT NULL DEFAULT 'study',
                party_size INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'planned',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                checked_in_at TIMESTAMP,
                ended_at TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id),
                CHECK (end_min > start_min),
                CHECK (party_size BETWEEN 1 AND 50),
                CHECK (purpose IN ('study', 'discussion', 'practice', 'other')),
                CHECK (status IN ('planned', 'checked_in', 'cancelled', 'ended', 'expired'))
            )
        ''')

        c.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_users_unsubscribe_token ON users (unsubscribe_token) WHERE unsubscribe_token IS NOT NULL')
        c.execute('CREATE INDEX IF NOT EXISTS idx_todos_user_ispace_lookup ON todos (user_id, ispace_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_page_views_created_at ON page_views (created_at)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_page_views_view_name ON page_views (view_name)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_page_views_visitor_id ON page_views (visitor_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_media_dl_events_created_at ON media_dl_events (created_at)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_media_dl_events_action_platform ON media_dl_events (action, platform)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_email_notification_due_lookup ON email_notification_deliveries (user_id, todo_id, reminder_hours)')
        c.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_email_notification_unique_success ON email_notification_deliveries (user_id, todo_id, reminder_hours) WHERE success = 1')
        c.execute('CREATE INDEX IF NOT EXISTS idx_classroom_intents_room_time ON classroom_intents (use_date, room, status, start_min, end_min)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_classroom_intents_user_active ON classroom_intents (user_id, status, use_date)')
        try:
            c.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_todos_user_ispace_unique ON todos (user_id, ispace_id) WHERE ispace_id IS NOT NULL')
        except sqlite3.IntegrityError:
            pass

        # Durable per-day rollup of page views (Beijing calendar day). page_views
        # keeps every raw hit, but this table preserves the daily totals cheaply and
        # survives even if raw rows are ever pruned; it also makes long-range history
        # queryable without scanning the full events table.
        c.execute('''
            CREATE TABLE IF NOT EXISTS daily_page_stats (
                day TEXT PRIMARY KEY,
                views INTEGER NOT NULL DEFAULT 0,
                visitors INTEGER NOT NULL DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        conn.commit()

        # One-time backfill: if the rollup is empty but raw views exist, compute
        # every historical Beijing day once. Cheap (single GROUP BY) and idempotent.
        try:
            already = c.execute('SELECT COUNT(*) FROM daily_page_stats').fetchone()[0]
            has_views = c.execute('SELECT 1 FROM page_views LIMIT 1').fetchone()
            if not already and has_views:
                rollup_daily_page_stats(conn)  # full history
                conn.commit()
        except sqlite3.OperationalError:
            pass

init_db()

# Global cache for the dataframe
df_cache = None

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def env_bool(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return str(value).strip().lower() in {'1', 'true', 'yes', 'on'}


def get_public_base_url():
    return os.getenv('MAXCOURSE_PUBLIC_BASE_URL', DEFAULT_PUBLIC_BASE_URL).rstrip('/')


def get_smtp_config():
    use_ssl = env_bool('SMTP_USE_SSL', False)
    default_port = 465 if use_ssl else 587
    try:
        port = int(os.getenv('SMTP_PORT', default_port))
    except ValueError:
        port = default_port
    return {
        "host": os.getenv('SMTP_HOST', '').strip(),
        "port": port,
        "username": os.getenv('SMTP_USERNAME', '').strip(),
        "password": os.getenv('SMTP_PASSWORD', ''),
        "from_email": os.getenv('SMTP_FROM_EMAIL', '').strip(),
        "from_name": os.getenv('SMTP_FROM_NAME', 'MAXCOURSE DDL').strip(),
        "reply_to": os.getenv('SMTP_REPLY_TO', '').strip(),
        "use_tls": env_bool('SMTP_USE_TLS', not use_ssl),
        "use_ssl": use_ssl,
    }


def is_email_service_configured():
    config = get_smtp_config()
    return bool(config["host"] and config["from_email"])


def normalize_email(value):
    email = str(value or '').strip().lower()
    if not email:
        return ''
    if not EMAIL_RE.match(email):
        raise ValueError("Invalid email address")
    return email


def parse_reminder_hours(value, default_to_existing=True):
    if value is None:
        return DEFAULT_EMAIL_REMINDER_HOURS[:] if default_to_existing else []

    if isinstance(value, str):
        raw_values = [item.strip() for item in value.split(',')]
    elif isinstance(value, (list, tuple)):
        raw_values = value
    else:
        raw_values = [value]

    parsed = []
    allowed = set(EMAIL_REMINDER_CHOICES)
    for item in raw_values:
        try:
            hour = int(item)
        except (TypeError, ValueError):
            continue
        if hour in allowed and hour not in parsed:
            parsed.append(hour)

    if not parsed and default_to_existing:
        return DEFAULT_EMAIL_REMINDER_HOURS[:]
    return sorted(parsed, reverse=True)


def reminder_hours_to_db(value):
    return ','.join(str(hour) for hour in parse_reminder_hours(value, default_to_existing=False))


def format_due_time(timestamp):
    return datetime.fromtimestamp(int(timestamp), tz=BEIJING_TZ).strftime('%Y-%m-%d %H:%M')


def ensure_unsubscribe_token(conn, user_id):
    c = conn.cursor()
    c.execute('SELECT unsubscribe_token FROM users WHERE id = ?', (user_id,))
    row = c.fetchone()
    if row and row['unsubscribe_token']:
        return row['unsubscribe_token']

    for _ in range(5):
        token = secrets.token_urlsafe(32)
        try:
            c.execute('UPDATE users SET unsubscribe_token = ? WHERE id = ?', (token, user_id))
            conn.commit()
            return token
        except sqlite3.IntegrityError:
            continue
    raise RuntimeError("Failed to generate unique unsubscribe token")


def build_unsubscribe_url(token):
    return f"{get_public_base_url()}/api/notifications/unsubscribe?token={token}"


def send_email(to_email, subject, text_body, html_body=None, unsubscribe_url=None):
    config = get_smtp_config()
    if not is_email_service_configured():
        raise RuntimeError("Email service is not configured")

    message = EmailMessage()
    if config["from_name"]:
        message['From'] = f'{config["from_name"]} <{config["from_email"]}>'
    else:
        message['From'] = config["from_email"]
    message['To'] = to_email
    message['Subject'] = subject
    if config["reply_to"]:
        message['Reply-To'] = config["reply_to"]
    if unsubscribe_url:
        message['List-Unsubscribe'] = f'<{unsubscribe_url}>'
        message['List-Unsubscribe-Post'] = 'List-Unsubscribe=One-Click'

    message.set_content(text_body)
    if html_body:
        message.add_alternative(html_body, subtype='html')

    context = ssl.create_default_context()
    if config["use_ssl"]:
        server = smtplib.SMTP_SSL(config["host"], config["port"], timeout=20, context=context)
    else:
        server = smtplib.SMTP(config["host"], config["port"], timeout=20)

    with server:
        if config["use_tls"] and not config["use_ssl"]:
            server.starttls(context=context)
        if config["username"] or config["password"]:
            server.login(config["username"], config["password"])
        server.send_message(message)


def notification_settings_payload(user_row):
    keys = user_row.keys()
    reminder_hours = parse_reminder_hours(user_row['email_reminder_hours'] if 'email_reminder_hours' in keys else None)
    enabled = bool(user_row['email_notifications_enabled']) if 'email_notifications_enabled' in keys else False
    unsubscribed_at = user_row['email_unsubscribed_at'] if 'email_unsubscribed_at' in keys else None
    return {
        "email": user_row['email'] if 'email' in keys and user_row['email'] else "",
        "enabled": enabled,
        "reminder_hours": reminder_hours,
        "available_reminder_hours": EMAIL_REMINDER_CHOICES,
        "email_service_configured": is_email_service_configured(),
        "unsubscribed_via_link": bool(unsubscribed_at) and not enabled,
    }


def build_todo_reminder_email(user_row, todo_row, reminder_hours, unsubscribe_url=None):
    from email_reminders import render_todo_reminder_email

    due_time = format_due_time(todo_row['due_date'])
    display_name = user_row['display_name'] or user_row['ispace_username'] or user_row['username']
    title = todo_row['title']
    course = todo_row['course'] or '个人任务'
    task_url = todo_row['url'] or f"{get_public_base_url()}/"
    site_url = get_public_base_url()
    return render_todo_reminder_email(
        display_name=display_name,
        title=title,
        course=course,
        due_time=due_time,
        task_url=task_url,
        site_url=site_url,
        reminder_hours=reminder_hours,
        unsubscribe_url=unsubscribe_url,
    )


def set_authenticated_session(user_id, username, display_name):
    session.permanent = True
    session['user_id'] = user_id
    session['username'] = username
    session['display_name'] = display_name


def sync_ispace_todos_for_user(conn, user_id, items):
    c = conn.cursor()
    added = 0
    updated = 0
    seen_ids = []

    for item in items:
        ispace_id = item.get('id')
        if ispace_id is None:
            continue

        seen_ids.append(ispace_id)
        title = item.get('name')
        course = item.get('course')
        due_date = item.get('due_date')
        url = item.get('url')

        c.execute(
            '''
            SELECT id, title, course, due_date, url, COALESCE(is_stale, 0) AS is_stale
            FROM todos
            WHERE user_id = ? AND ispace_id = ?
            ORDER BY id ASC
            LIMIT 1
            ''',
            (user_id, ispace_id),
        )
        existing = c.fetchone()

        if existing:
            c.execute(
                '''
                UPDATE todos
                SET title = ?, course = ?, due_date = ?, url = ?, is_stale = 0
                WHERE user_id = ? AND ispace_id = ?
                ''',
                (title, course, due_date, url, user_id, ispace_id),
            )
            if (
                existing['title'] != title
                or existing['course'] != course
                or existing['due_date'] != due_date
                or existing['url'] != url
                or existing['is_stale']
            ):
                updated += 1
        else:
            c.execute(
                '''
                INSERT INTO todos (user_id, ispace_id, title, course, due_date, url, is_stale)
                VALUES (?, ?, ?, ?, ?, ?, 0)
                ''',
                (user_id, ispace_id, title, course, due_date, url),
            )
            added += 1

    if seen_ids:
        placeholders = ','.join('?' for _ in seen_ids)
        c.execute(
            f'''
            UPDATE todos
            SET is_stale = 1
            WHERE user_id = ?
              AND ispace_id IS NOT NULL
              AND ispace_id NOT IN ({placeholders})
              AND COALESCE(is_stale, 0) = 0
            ''',
            [user_id, *seen_ids],
        )
    else:
        c.execute(
            '''
            UPDATE todos
            SET is_stale = 1
            WHERE user_id = ?
              AND ispace_id IS NOT NULL
              AND COALESCE(is_stale, 0) = 0
            ''',
            (user_id,),
        )

    return {
        "added": added,
        "updated": updated,
        "stale": c.rowcount,
    }

def get_excel_file():
    files = glob.glob("*.xlsx") + glob.glob("*.xls")
    files = [f for f in files if not f.startswith("~$")]
    if not files:
        return None
    for f in files:
        if "Course List" in f:
            return f
    return files[0] if files else None

def get_df():
    global df_cache
    if df_cache is not None:
        return df_cache
    
    file_path = get_excel_file()
    if not file_path:
        raise FileNotFoundError("No Excel file found in directory")
    
    print(f"Loading data from {file_path}...")
    df_cache = load_timetable(file_path)
    return df_cache


def time_to_minutes(value):
    hours, minutes = str(value).strip().split(':')
    return int(hours) * 60 + int(minutes)


def minutes_to_time(value):
    return f"{value // 60:02d}:{value % 60:02d}"


def _classroom_now():
    return datetime.now(BEIJING_TZ)


def _parse_classroom_clock(value):
    raw = str(value or '').strip()
    if not re.fullmatch(r'\d{2}:\d{2}', raw):
        raise ValueError('Invalid time')
    hours, minutes = (int(part) for part in raw.split(':'))
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        raise ValueError('Invalid time')
    return hours * 60 + minutes


def _parse_classroom_date(value):
    raw = str(value or '').strip()
    try:
        parsed = datetime.strptime(raw, '%Y-%m-%d').date()
    except (TypeError, ValueError):
        raise ValueError('Invalid date')
    if parsed.isoformat() != raw:
        raise ValueError('Invalid date')
    return parsed


def _next_classroom_date(day, today=None):
    if day not in DAY_MAP:
        raise ValueError('Invalid day')
    today = today or _classroom_now().date()
    return today + timedelta(days=(DAY_MAP[day] - today.weekday()) % 7)


def _close_stale_classroom_intents(conn, now=None):
    now = now or _classroom_now()
    today = now.date().isoformat()
    now_min = now.hour * 60 + now.minute
    conn.execute(
        '''
        UPDATE classroom_intents
        SET status = 'expired', ended_at = CURRENT_TIMESTAMP
        WHERE status = 'planned'
          AND (use_date < ? OR (use_date = ? AND start_min + ? < ?))
        ''',
        (today, today, CLASSROOM_INTENT_GRACE_MINUTES, now_min),
    )
    conn.execute(
        '''
        UPDATE classroom_intents
        SET status = 'ended', ended_at = CURRENT_TIMESTAMP
        WHERE status = 'checked_in'
          AND (use_date < ? OR (use_date = ? AND end_min <= ?))
        ''',
        (today, today, now_min),
    )


def _serialize_classroom_intent(row, now=None):
    now = now or _classroom_now()
    now_min = now.hour * 60 + now.minute
    can_check_in = (
        row['status'] == 'planned'
        and row['use_date'] == now.date().isoformat()
        and row['start_min'] - CLASSROOM_INTENT_GRACE_MINUTES <= now_min
        and now_min <= row['start_min'] + CLASSROOM_INTENT_GRACE_MINUTES
    )
    return {
        'id': row['id'],
        'room': row['room'],
        'date': row['use_date'],
        'start': minutes_to_time(row['start_min']),
        'end': minutes_to_time(row['end_min']),
        'purpose': row['purpose'],
        'party_size': row['party_size'],
        'status': row['status'],
        'can_check_in': can_check_in,
    }


def _empty_classroom_intent_summary():
    return {
        'records': 0,
        'people': 0,
        'planned_people': 0,
        'checked_in_people': 0,
        'purposes': [],
        'my': None,
    }


def _classroom_intent_summaries(use_date, start_min, end_min, user_id=None, now=None):
    now = now or _classroom_now()
    today = now.date().isoformat()
    now_min = now.hour * 60 + now.minute
    conn = get_db()
    try:
        rows = conn.execute(
            '''
            SELECT id, user_id, room, use_date, start_min, end_min,
                   purpose, party_size, status
            FROM classroom_intents
            WHERE use_date = ?
              AND status IN ('planned', 'checked_in')
              AND start_min < ? AND end_min > ?
              AND (
                    use_date > ?
                    OR (
                        use_date = ?
                        AND (
                            (status = 'planned' AND start_min + ? >= ?)
                            OR (status = 'checked_in' AND end_min > ?)
                        )
                    )
              )
            ORDER BY start_min, id
            ''',
            (
                use_date, end_min, start_min, today, today,
                CLASSROOM_INTENT_GRACE_MINUTES, now_min, now_min,
            ),
        ).fetchall()
    finally:
        conn.close()

    summaries = {}
    for row in rows:
        summary = summaries.setdefault(row['room'], _empty_classroom_intent_summary())
        summary['records'] += 1
        summary['people'] += row['party_size']
        if row['status'] == 'checked_in':
            summary['checked_in_people'] += row['party_size']
        else:
            summary['planned_people'] += row['party_size']
        if row['purpose'] not in summary['purposes']:
            summary['purposes'].append(row['purpose'])
        if user_id is not None and row['user_id'] == user_id:
            summary['my'] = _serialize_classroom_intent(row, now)

    for summary in summaries.values():
        summary['purposes'].sort(
            key=lambda purpose: CLASSROOM_INTENT_PURPOSES.index(purpose)
        )
    return summaries


def extract_building(room):
    room = str(room).strip()
    if '-' not in room:
        return room.upper()
    return room.split('-', 1)[0].strip().upper()


def building_sort_key(building):
    building = str(building or '').strip().upper()

    if building in PRIORITY_BUILDING_ORDER:
        return (0, PRIORITY_BUILDING_ORDER.index(building), 0, '', 0, building)

    if building == 'CC':
        return (2, 0, 0, '', 0, building)

    match = re.match(r'^([A-Z]+)(\d+)$', building)
    if match:
        prefix, number = match.groups()
        return (1, 0, 0, prefix, int(number), building)

    return (1, 1, 1, building, 0, building)


def is_room_like(room):
    room = str(room).strip()
    return bool(room) and room.lower() != 'nil' and '-' in room and ' ' not in room


def normalize_room_tokens(raw_room):
    seen = set()
    rooms = []
    for part in str(raw_room or '').split('/'):
        room = part.strip()
        if is_room_like(room) and room not in seen:
            seen.add(room)
            rooms.append(room)
    return rooms


def serialize_room_event(event):
    return {
        "course_code": event["course_code"],
        "title": event["title"],
        "teacher": event["teacher"],
        "start": minutes_to_time(event["start_min"]),
        "end": minutes_to_time(event["end_min"]),
    }


# Memoized per DataFrame identity: df_cache is stable for the process
# lifetime in production, so this is a build-once cache; keying on the df
# object keeps it correct if the timetable is ever swapped (e.g. in tests).
_classroom_index_cache = None  # (df, rooms, room_entries)


def build_classroom_index():
    global _classroom_index_cache
    df = get_df()
    if _classroom_index_cache is not None and _classroom_index_cache[0] is df:
        return _classroom_index_cache[1], _classroom_index_cache[2]

    room_index = {}
    room_entries = {}

    for _, row in df.iterrows():
        meeting = parse_schedule(str(row.get('Class Schedule', '')).strip())
        if meeting is None:
            continue

        rooms = normalize_room_tokens(row.get('Classroom', ''))
        if not rooms:
            continue

        day_index, start_min, end_min = meeting
        event = {
            "day_index": day_index,
            "start_min": start_min,
            "end_min": end_min,
            "course_code": str(row.get('Course Code', '')).strip(),
            "title": str(row.get('Course Title & Session', '')).strip(),
            "teacher": str(row.get('Teachers', '')).strip(),
        }

        for room in rooms:
            room_entries.setdefault(room, []).append(event)
            room_index.setdefault(room, {"room": room, "building": extract_building(room)})

    for room, events in room_entries.items():
        events.sort(key=lambda item: (item["day_index"], item["start_min"], item["end_min"], item["course_code"]))

    rooms = [
        room_index[key]
        for key in sorted(
            room_index.keys(),
            key=lambda room: (building_sort_key(extract_building(room)), room)
        )
        if extract_building(key) not in EXCLUDED_FREE_CLASSROOM_BUILDINGS
    ]
    _classroom_index_cache = (df, rooms, room_entries)
    return rooms, room_entries

# --- Course catalog + AI enrichment (static JSON, regenerated per semester) ---
COURSE_CATALOG_PATH = os.path.join(APP_ROOT, 'course_catalog.json')
COURSE_ENRICHMENT_PATH = os.path.join(APP_ROOT, 'course_enrichment.json')
_catalog_cache = {"mtime": None, "data": None}
_enrichment_cache = {"mtime": None, "data": None}


def _load_json_cached(path, cache):
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        cache["mtime"], cache["data"] = None, {}
        return cache["data"]
    if cache["data"] is None or cache["mtime"] != mtime:
        try:
            with open(path, 'r', encoding='utf-8') as file:
                cache["data"] = json.load(file)
        except (OSError, ValueError):
            cache["data"] = {}
        cache["mtime"] = mtime
    return cache["data"]


def get_course_catalog():
    return _load_json_cached(COURSE_CATALOG_PATH, _catalog_cache)


def get_course_enrichment():
    return _load_json_cached(COURSE_ENRICHMENT_PATH, _enrichment_cache)


# Extra data extracted from the official ECM files (textbook list per semester,
# WPEC/GE course descriptions that the main PDF catalog is missing).
COURSE_TEXTBOOKS_PATH = os.path.join(APP_ROOT, 'course_textbooks.json')
COURSE_DESC_EXTRA_PATH = os.path.join(APP_ROOT, 'course_descriptions_extra.json')
_textbooks_cache = {"mtime": None, "data": None}
_desc_extra_cache = {"mtime": None, "data": None}


def get_course_textbooks():
    return _load_json_cached(COURSE_TEXTBOOKS_PATH, _textbooks_cache)


# Campus file center (工具箱 > 校园文件中心): curated high-frequency student
# documents. Hosted PDFs live under /docs/, external entries link to the
# official AR pages / the SSO-gated ECM station. Regenerate by hand-editing
# campus_docs.json; the loader picks up changes by mtime without a restart.
CAMPUS_DOCS_PATH = os.path.join(APP_ROOT, 'campus_docs.json')
_campus_docs_cache = {"mtime": None, "data": None}


def get_campus_docs():
    return _load_json_cached(CAMPUS_DOCS_PATH, _campus_docs_cache)


# Course equivalences derived from prereq exclusion/alternative texts
# (build_equivalences.py). Symmetric pairs, no transitive closure.
COURSE_EQUIV_PATH = os.path.join(APP_ROOT, 'course_equivalences.json')
_equiv_cache = {"mtime": None, "data": None}


def get_course_equivalences():
    return _load_json_cached(COURSE_EQUIV_PATH, _equiv_cache)


# Programme handbooks (four-year study plans) parsed by build_programmes.py
PROGRAMME_REQ_PATH = os.path.join(APP_ROOT, 'programme_requirements.json')
_programme_req_cache = {"mtime": None, "data": None}
GE_CODE_PREFIX = re.compile(r'^(GC|GT|GF)')


def get_programme_requirements():
    return _load_json_cached(PROGRAMME_REQ_PATH, _programme_req_cache)


_programme_course_index = {"mtime": None, "index": None}
_PROGRAMME_ROLE_ORDER = {"主修必修": 0, "BBA 核心": 1, "主修选修": 2, "大学核心": 3}


def _programme_role(title):
    t = title.lower()
    if "major" in t and "required" in t:
        return "主修必修"
    if "major" in t and "elective" in t:
        return "主修选修"
    if "university core" in t:
        return "大学核心"
    if "bba" in t:
        return "BBA 核心"
    return title


def get_programme_course_index():
    """code -> {(programme_key, role): set(cohorts)} across all handbooks."""
    reqs = get_programme_requirements()
    mtime = _programme_req_cache.get("mtime")
    if _programme_course_index["index"] is not None and _programme_course_index["mtime"] == mtime:
        return _programme_course_index["index"]
    index = {}
    for pkey, prog in reqs.items():
        for cohort, plan in prog.get("cohorts", {}).items():
            for section in plan.get("sections", []):
                role = _programme_role(section.get("title", ""))
                for lst in (section.get("courses", []), section.get("pool", [])):
                    for c in lst:
                        index.setdefault(c["code"], {}).setdefault((pkey, role), set()).add(cohort)
    _programme_course_index.update(mtime=mtime, index=index)
    return index


def course_programme_tags(code):
    """Aggregated 'which study plans include this course' tags for the modal."""
    entry = get_programme_course_index().get(code)
    if not entry:
        return []
    reqs = get_programme_requirements()
    by_role = {}
    for (pkey, role), cohorts in entry.items():
        if role not in _PROGRAMME_ROLE_ORDER:
            continue  # free-elective / GE suggestion lists carry no real signal
        by_role.setdefault(role, []).append((pkey, cohorts))
    out = []
    for role, items in by_role.items():
        # University-core rows appear in almost every programme — collapse.
        if role == "大学核心" and len(items) >= 20:
            all_cohorts = sorted(set().union(*[c for _, c in items]))
            out.append({"key": "ALL", "name": "各专业通用", "faculty": "", "role": role, "cohorts": all_cohorts})
            continue
        for pkey, cohorts in items:
            p = reqs.get(pkey, {})
            out.append({"key": pkey, "name": p.get("name", pkey), "faculty": p.get("faculty", ""),
                        "role": role, "cohorts": sorted(cohorts)})
    out.sort(key=lambda x: (_PROGRAMME_ROLE_ORDER.get(x["role"], 9), x["key"]))
    return out


def get_course_desc_extra():
    return _load_json_cached(COURSE_DESC_EXTRA_PATH, _desc_extra_cache)


def _resolve_course_refs(codes, catalog, enrichment):
    equiv_map = get_course_equivalences()
    resolved = []
    for code in codes:
        course = catalog.get(code)
        entry = {
            "code": code,
            "title": course["title"] if course else "",
            "offered": bool(course["offered"]) if course else False,
            "has_enrichment": code in enrichment,
        }
        if not course:
            # legacy code: point at its current-catalog equivalent if we know one
            for e in equiv_map.get(code, []):
                ref = catalog.get(e)
                if ref:
                    entry["equiv"] = {"code": e, "title": ref["title"], "offered": bool(ref["offered"])}
                    break
        resolved.append(entry)
    return resolved


app.register_blueprint(media_dl_bp)


@app.route('/')
def index():
    return send_from_directory('.', 'index.html')


@app.route('/api/course/<path:code>', methods=['GET'])
def get_course_detail(code):
    code = (code or '').strip().upper()
    catalog = get_course_catalog()
    course = catalog.get(code)
    if not course:
        return jsonify({"error": "Course not found"}), 404

    enrichment_map = get_course_enrichment()
    result = dict(course)
    result["prereqs"] = _resolve_course_refs(course.get("prereq_codes", []), catalog, enrichment_map)
    result["unlocks"] = _resolve_course_refs(course.get("unlocks", []), catalog, enrichment_map)

    # Textbooks (from the semester textbook list) + description fallback for
    # courses the main PDF catalog has no description for (WPEC/GE courses).
    result["textbooks"] = get_course_textbooks().get(code, [])
    if not (result.get("description") or "").strip():
        extra = get_course_desc_extra().get(code)
        if extra:
            if extra.get("description"):
                result["description"] = extra["description"]
            result["description_cn"] = extra.get("description_cn", "")
            result["description_source"] = extra.get("source", "")

    enrichment = enrichment_map.get(code)
    if enrichment:
        enrichment = dict(enrichment)
        resolved_similar = []
        for item in enrichment.get("similar", []):
            ref = catalog.get(item.get("code"))
            resolved_similar.append({
                "code": item.get("code"),
                "reason": item.get("reason", ""),
                "title": ref["title"] if ref else "",
                "offered": bool(ref["offered"]) if ref else False,
                "has_enrichment": item.get("code") in enrichment_map,
            })
        enrichment["similar"] = resolved_similar
    result["enrichment"] = enrichment

    # SkillPath layer: skills taught + matched careers (with US-market salary)
    skillpath_courses = get_skillpath_courses()
    result["skillpath"] = skillpath_courses.get(code, {"skills": [], "careers": []})

    # Which programme study plans (handbooks) include this course
    result["programmes"] = course_programme_tags(code)

    # Equivalent / mutually-exclusive courses (registry substitution relation)
    equiv_map = get_course_equivalences()
    result["equivalents"] = [
        {
            "code": e,
            "title": catalog[e]["title"] if e in catalog else "",
            "offered": bool(catalog[e]["offered"]) if e in catalog else False,
            "in_catalog": e in catalog,
        }
        for e in equiv_map.get(code, [])
    ]
    return jsonify(result)


# --- SkillPath: course skills, target careers, and a live PPR recommender ---
# Data + graph are precomputed by build_skillpath.py from the Big-Data project's
# LinkedIn + course-skill extraction. Salary/jobs reflect a 2023-24 US dataset.
SKILLPATH_COURSES_PATH = os.path.join(APP_ROOT, 'skillpath_courses.json')
SKILLPATH_CAREERS_PATH = os.path.join(APP_ROOT, 'skillpath_careers.json')
SKILLPATH_GRAPH_PATH = os.path.join(APP_ROOT, 'skillpath_graph.npz')
SKILLPATH_NODES_PATH = os.path.join(APP_ROOT, 'skillpath_nodes.json')
SKILLPATH_SALARY_CAVEAT = "岗位与薪资来自 2023-24 LinkedIn 公开数据集（以美国市场为主，USD/年），仅供参考，非大湾区本地行情。"
_skillpath_courses_cache = {"mtime": None, "data": None}
_skillpath_careers_cache = {"mtime": None, "data": None}
_skillpath_graph = {"loaded": False}


def get_skillpath_courses():
    return _load_json_cached(SKILLPATH_COURSES_PATH, _skillpath_courses_cache)


def get_skillpath_careers():
    return _load_json_cached(SKILLPATH_CAREERS_PATH, _skillpath_careers_cache)


def get_skillpath_graph():
    if _skillpath_graph["loaded"]:
        return _skillpath_graph
    _skillpath_graph["loaded"] = True
    try:
        import numpy as np
        from scipy import sparse
        matrix = sparse.load_npz(SKILLPATH_GRAPH_PATH).tocsr()
        nodes = json.load(open(SKILLPATH_NODES_PATH, encoding='utf-8'))
        n = matrix.shape[0]
        node_ids = nodes["node_ids"]
        dangling = np.zeros(n)
        for i in nodes.get("dangling", []):
            dangling[i] = 1.0
        _skillpath_graph.update(
            np=np, M=matrix, n=n,
            node_type=nodes["node_type"], node_label=nodes["node_label"], node_ids=node_ids,
            course_index=nodes["course_index"], career_index=nodes["career_index"],
            skill_index={nid.split("skill:", 1)[1]: i for i, nid in enumerate(node_ids) if nid.startswith("skill:")},
            course_rows=[i for i, t in enumerate(nodes["node_type"]) if t == "course"],
            dangling=dangling, ok=True,
        )
    except Exception:
        app.logger.exception("SkillPath graph load failed")
        _skillpath_graph["ok"] = False
    return _skillpath_graph


def _run_ppr(graph, career, completed_skill_names, beta=0.85, iters=60):
    np = graph["np"]
    n = graph["n"]
    careers = get_skillpath_careers()
    career_skills = careers.get(career, {}).get("skills", [])
    missing = [(s["name"], float(s["weight"])) for s in career_skills
               if s["name"].lower() not in completed_skill_names]

    teleport = np.zeros(n)
    career_node = graph["career_index"].get(career)
    if career_node is not None:
        teleport[career_node] = 0.5
    total_missing = sum(w for _, w in missing) or 1.0
    for name, weight in missing:
        si = graph["skill_index"].get(name)
        if si is not None:
            teleport[si] += 0.5 * (weight / total_missing)
    if teleport.sum() == 0:
        if career_node is not None:
            teleport[career_node] = 1.0
        else:
            return None
    teleport /= teleport.sum()

    rank = teleport.copy()
    matrix = graph["M"]
    dangling = graph["dangling"]
    for _ in range(iters):
        rank = beta * (matrix @ rank) + beta * float(dangling @ rank) * teleport + (1 - beta) * teleport
    return rank


# --- Transcript parsing: pull completed course codes from a BNBU/UIC PDF ---
TRANSCRIPT_CODE_RE = re.compile(r'\b([A-Z]{2,4})\s?(\d{4})\b')
TRANSCRIPT_PASS_GRADES = {'A+', 'A', 'A-', 'B+', 'B', 'B-', 'C+', 'C', 'C-', 'D+', 'D', 'D-', 'S', 'P'}
_TRANSCRIPT_GRADE_RE = re.compile(r'(?<![A-Za-z0-9.])(A\+|A-|A|B\+|B-|B|C\+|C-|C|D\+|D-|D|S|P|F|W|I)(?![A-Za-z])')
_TRANSCRIPT_GLUED_RE = re.compile(r'\d(A\+|A-|A|B\+|B-|B|C\+|C-|C|D\+|D-|D|S|P)(?![A-Za-z])')


def _transcript_has_pass(record):
    tokens = set(_TRANSCRIPT_GRADE_RE.findall(record)) | set(_TRANSCRIPT_GLUED_RE.findall(record))
    return any(t in TRANSCRIPT_PASS_GRADES for t in tokens)


def extract_completed_course_codes(text):
    """Return completed (passed) course codes from a transcript / graduation-audit PDF.

    Graduation-audit reports group courses under 'successfully completed' vs
    'failed/incomplete/to be taken' headers; plain transcripts list a grade per
    course. Handle both, and normalise 'AI 2023' -> 'AI2023'.
    """
    text = re.sub(r'[ \t]+', ' ', text or '')
    matches = list(TRANSCRIPT_CODE_RE.finditer(text))
    lower = text.lower()
    out, seen = [], set()

    if 'successfully completed' in lower:
        spans = []
        for header in re.finditer(r'successfully completed', lower):
            start = header.end()
            boundaries = [lower.find(h, start) for h in
                          ('currently taking', 'failed/incomplete', 'failed', 'to be taken',
                           'to be selected', 'successfully completed')]
            boundaries = [b for b in boundaries if b != -1 and b > start]
            spans.append((start, min(boundaries) if boundaries else len(text)))

        def in_completed(pos):
            return any(s <= pos < e for s, e in spans)

        for match in matches:
            if in_completed(match.start()):
                code = match.group(1) + match.group(2)
                if code not in seen:
                    seen.add(code)
                    out.append(code)
    else:
        for i, match in enumerate(matches):
            code = match.group(1) + match.group(2)
            start = match.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            if _transcript_has_pass(text[start:end]) and code not in seen:
                seen.add(code)
                out.append(code)
    return out


@app.route('/api/parse-transcript', methods=['POST'])
def parse_transcript():
    upload = request.files.get('file')
    if not upload or not upload.filename:
        return jsonify({"error": "请选择成绩单 PDF 文件"}), 400
    if not upload.filename.lower().endswith('.pdf'):
        return jsonify({"error": "仅支持 PDF 格式的成绩单/毕业审核报告"}), 400

    max_bytes = 12 * 1024 * 1024
    data = upload.read(max_bytes + 1)
    if len(data) > max_bytes:
        return jsonify({"error": "文件过大（上限 12MB）"}), 400

    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception as error:
        app.logger.exception("Transcript parse failed")
        return jsonify({"error": f"无法读取 PDF：{error}"}), 400

    codes = extract_completed_course_codes(text)
    catalog = get_course_catalog()
    recognized = [{"code": c, "title": catalog[c]["title"]} for c in codes if c in catalog]
    unrecognized = [c for c in codes if c not in catalog]
    return jsonify({
        "codes": codes,
        "courses": recognized,
        "unrecognized": unrecognized,
        "count": len(codes),
        "recognized_count": len(recognized),
    })


@app.route('/api/programmes', methods=['GET'])
def list_programmes():
    data = get_programme_requirements()
    out = []
    for key, p in data.items():
        out.append({
            "key": key,
            "name": p.get("name", key),
            "faculty": p.get("faculty", ""),
            "department": p.get("department", ""),
            "cohorts": sorted(p.get("cohorts", {}).keys()),
        })
    out.sort(key=lambda x: (x["faculty"], x["name"]))
    return jsonify({"programmes": out})


@app.route('/api/campus-docs', methods=['GET'])
def api_campus_docs():
    """Curated campus file center for the toolbox. Serves campus_docs.json as-is
    (hosted PDFs under /docs/ + external official links); empty categories on a
    missing/broken file so the view degrades gracefully."""
    data = get_campus_docs() or {}
    data.setdefault("categories", [])
    return jsonify(data)


@app.route('/api/programme-courses', methods=['GET'])
def programme_courses():
    """code -> role (主修必修/BBA 核心/主修选修) for one programme, union of cohorts.
    Used by the explorer's programme filter; university-core rows are excluded
    (they are common to every programme)."""
    key = (request.args.get('programme') or '').strip().upper()
    reqs = get_programme_requirements()
    if key not in reqs:
        return jsonify({"error": "Unknown programme"}), 400
    priority = {"主修必修": 0, "BBA 核心": 1, "主修选修": 2}
    out = {}
    for code, entry in get_programme_course_index().items():
        for (pkey, role), _cohorts in entry.items():
            if pkey != key or role not in priority:
                continue
            if code not in out or priority[role] < priority[out[code]]:
                out[code] = role
    return jsonify({"programme": key, "courses": out})


@app.route('/api/programme-map', methods=['POST'])
def programme_map():
    body = request.get_json(silent=True) or {}
    key = str(body.get('programme') or '').strip().upper()
    cohort = str(body.get('cohort') or '').strip()
    completed = {str(c).strip().upper() for c in (body.get('completed') or []) if str(c).strip()}

    data = get_programme_requirements()
    prog = data.get(key)
    if not prog:
        return jsonify({"error": "Unknown programme"}), 400
    plan = prog.get("cohorts", {}).get(cohort)
    if not plan:
        return jsonify({"error": "Unknown cohort"}), 400

    catalog = get_course_catalog()

    def course_units(code, fallback=3):
        c = catalog.get(code)
        if c and c.get("units"):
            return c["units"]
        return fallback

    def resolve(entry):
        code = entry["code"]
        ref = catalog.get(code)
        return {
            "code": code,
            "title": entry.get("title") or (ref["title"] if ref else ""),
            "units": entry.get("units") or course_units(code),
            "plan": entry.get("plan", []),
            "completed": code in completed,
            "via": None,
            "offered": bool(ref["offered"]) if ref else False,
            "in_catalog": ref is not None,
        }

    equiv_map = get_course_equivalences()
    matched = set()
    sections_out = []
    for section in plan.get("sections", []):
        courses = [resolve(c) for c in section.get("courses", [])]
        pool = [resolve(c) for c in section.get("pool", [])]
        for c in courses:
            if c["completed"]:
                matched.add(c["code"])
        for c in pool:
            if c["completed"]:
                matched.add(c["code"])
        sections_out.append({
            "numeral": section["numeral"],
            "title": section["title"],
            "units": section["units"],
            "courses": courses,
            "pool": pool,
            "gained": 0,
            "auto": bool(courses or pool),
            "estimated": False,
            "matched_extra": [],
        })

    # Equivalence pass: an uncompleted requirement counts as satisfied when the
    # student completed a registry-equivalent course (e.g. DS1013 for AI1003),
    # as long as that course isn't itself a listed requirement or already used.
    # Guard: mutual exclusion alone is overlap, not substitutability (the audit
    # does NOT count MATH1073 for MATH1123) — additionally require one course
    # title to contain the other ("Python Programming" ⊂ "Python Programming
    # for Beginners").
    def _norm_title(text):
        text = re.sub(r'\([^)]*\)', ' ', str(text or ''))
        return ' '.join(re.sub(r'[^a-z0-9]+', ' ', text.lower()).split())

    def _substitutable(req_title, cand_code):
        a = _norm_title(req_title)
        b = _norm_title(catalog.get(cand_code, {}).get('title', ''))
        return bool(a) and bool(b) and (a in b or b in a)

    all_listed = {c["code"] for s in sections_out for c in s["courses"]}
    for s in sections_out:
        for c in s["courses"]:
            if c["completed"]:
                continue
            via = next((e for e in equiv_map.get(c["code"], [])
                        if e in completed and e not in matched and e not in all_listed
                        and _substitutable(c["title"], e)), None)
            if via:
                c["completed"] = True
                c["via"] = via
                matched.add(via)

    # Unit tally per section (direct + via for listed courses; pool dedup'd)
    pool_counted = set()
    for s in sections_out:
        gained = sum(c["units"] for c in s["courses"] if c["completed"])
        for c in s["pool"]:
            if c["completed"] and c["code"] not in all_listed and c["code"] not in pool_counted:
                gained += c["units"]
                pool_counted.add(c["code"])
        s["gained"] = min(gained, s["units"]) if s["auto"] else 0

    # Heuristic for GE / Free-elective sections: distribute leftover completed
    # courses (GE by code prefix first, the rest to free electives). Estimates.
    leftovers = [c for c in sorted(completed - matched)]
    for section in sections_out:
        if section["auto"]:
            continue
        title = section["title"].lower()
        picked = []
        if "general education" in title:
            picked = [c for c in leftovers if GE_CODE_PREFIX.match(c)]
        elif "free elective" in title:
            picked = list(leftovers)
        if not picked:
            continue
        gained = 0
        used = []
        for code in picked:
            if gained >= section["units"]:
                break
            gained += course_units(code)
            used.append(code)
        leftovers = [c for c in leftovers if c not in used]
        section["gained"] = min(gained, section["units"])
        section["estimated"] = True
        section["matched_extra"] = [
            {"code": c, "title": catalog.get(c, {}).get("title", ""), "units": course_units(c)}
            for c in used
        ]

    # Browsable candidate pool for GE sections: the handbook lists none, but the
    # eligible set is identifiable from the catalog by GE code prefixes. Purely
    # informational — attached after unit tallying so it never affects progress.
    for s in sections_out:
        if "general education" in s["title"].lower() and not s["pool"] and not s["courses"]:
            ge_pool = []
            for ge_code in sorted(catalog):
                if not GE_CODE_PREFIX.match(ge_code):
                    continue
                ref = catalog[ge_code]
                ge_pool.append({
                    "code": ge_code,
                    "title": ref.get("title") or ge_code,
                    "units": ref.get("units") or 3,
                    "plan": [],
                    "completed": ge_code in completed,
                    "via": None,
                    "offered": bool(ref.get("offered")),
                    "in_catalog": True,
                })
            ge_pool.sort(key=lambda c: (not c["completed"], not c["offered"], c["code"]))
            s["pool"] = ge_pool
            s["pool_synthetic"] = True

    total = plan.get("total_units") or sum(s["units"] for s in sections_out)
    overall_gained = sum(s["gained"] for s in sections_out)
    return jsonify({
        "programme": {"key": key, "name": prog.get("name", key), "faculty": prog.get("faculty", ""),
                      "department": prog.get("department", ""), "cohort": cohort},
        "sections": sections_out,
        "overall": {"gained": overall_gained, "total": total},
        "unassigned": [
            {"code": c, "title": catalog.get(c, {}).get("title", "")} for c in leftovers
        ],
        "note": "主修/核心课按官方修读计划核对（含等价课自动认定，标 ≈ 号）；GE 与自由选修为按已修课程的估算，请以 MIS 毕业审核为准。",
    })


@app.route('/api/careers', methods=['GET'])
def list_careers():
    careers = get_skillpath_careers()
    out = []
    for label, c in careers.items():
        out.append({
            "label": label,
            "faculty": c.get("faculty", ""),
            "salary": c.get("salary", {}),
            "n_postings": c.get("n_postings", 0),
            "n_skill_jobs": c.get("n_skill_jobs", 0),
            "low_sample": bool(c.get("low_sample", False)),
            "top_skills": [s["name"] for s in c.get("skills", [])[:5]],
        })
    out.sort(key=lambda x: (x["faculty"], -x["n_postings"], x["label"]))
    return jsonify({"careers": out, "caveat": SKILLPATH_SALARY_CAVEAT})


@app.route('/api/recommend', methods=['POST'])
def recommend_courses():
    data = request.get_json(silent=True) or {}
    career = str(data.get('career') or '').strip()
    completed = [str(c).strip().upper() for c in (data.get('completed') or []) if str(c).strip()]
    offered_only = bool(data.get('offered_only', True))
    limit = min(int(data.get('limit', 12) or 12), 30)

    careers = get_skillpath_careers()
    if career not in careers:
        return jsonify({"error": "Unknown career goal"}), 400

    graph = get_skillpath_graph()
    if not graph.get("ok"):
        return jsonify({"error": "SkillPath recommender is unavailable"}), 503

    catalog = get_course_catalog()
    sp_courses = get_skillpath_courses()
    enrichment_map = get_course_enrichment()
    completed_set = set(completed)

    # student's skills = union of skills from completed courses
    student_skills = set()
    for code in completed:
        for s in sp_courses.get(code, {}).get("skills", []):
            student_skills.add(s["name"].lower())

    rank = _run_ppr(graph, career, student_skills)
    if rank is None:
        return jsonify({"error": "Could not build a recommendation for this goal"}), 500

    career_skill_names = {s["name"].lower(): s for s in careers[career].get("skills", [])}

    ranked_courses = sorted(
        ((graph["node_ids"][i].split("course:", 1)[1], float(rank[i])) for i in graph["course_rows"]),
        key=lambda x: -x[1],
    )

    recommendations = []
    why_not = []
    for code, score in ranked_courses:
        course = catalog.get(code)
        if not course:
            continue
        if code in completed_set:
            if len(why_not) < 6:
                why_not.append({"code": code, "title": course["title"], "reason": "已修过"})
            continue
        if offered_only and not course["offered"]:
            if len(why_not) < 6:
                why_not.append({"code": code, "title": course["title"], "reason": "本学期未开设"})
            continue

        course_skills = sp_courses.get(code, {}).get("skills", [])
        bridge = []
        for s in course_skills:
            low = s["name"].lower()
            if low in career_skill_names and low not in student_skills:
                bridge.append({"name": s["name"], "category": s["category"],
                               "career_weight": career_skill_names[low]["weight"]})
        bridge.sort(key=lambda x: -x["career_weight"])

        unmet_prereqs = [
            {"code": p, "title": catalog[p]["title"] if p in catalog else ""}
            for p in course.get("prereq_codes", []) if p not in completed_set
        ]

        recommendations.append({
            "code": code,
            "title": course["title"],
            "units": course["units"],
            "offered": course["offered"],
            "score": round(score, 6),
            "tagline": (enrichment_map.get(code) or {}).get("tagline", ""),
            "teaches": [{"name": s["name"], "category": s["category"]} for s in course_skills[:8]],
            "bridge_skills": bridge[:4],
            "unmet_prereqs": unmet_prereqs[:4],
        })
        if len(recommendations) >= limit:
            break

    # skill gap: which of the career's top skills the student already has
    skill_gap = [
        {"name": s["name"], "category": s["category"], "weight": s["weight"],
         "have": s["name"].lower() in student_skills}
        for s in careers[career].get("skills", [])[:12]
    ]

    return jsonify({
        "career": {
            "label": career,
            "faculty": careers[career].get("faculty", ""),
            "salary": careers[career].get("salary", {}),
            "n_postings": careers[career].get("n_postings", 0),
            "n_skill_jobs": careers[career].get("n_skill_jobs", 0),
            "low_sample": bool(careers[career].get("low_sample", False)),
        },
        "skill_gap": skill_gap,
        "student_skill_count": len(student_skills),
        "recommendations": recommendations,
        "why_not": why_not[:4],
        "caveat": SKILLPATH_SALARY_CAVEAT,
    })

@app.route('/ddl')
def ddl_page():
    return send_from_directory('.', 'ddl.html')

@app.route('/favicon.ico')
def favicon():
    return send_from_directory('.', 'favicon.png', mimetype='image/png')


@app.before_request
def sso_attach():
    # OmniChat / SlideCraft set a parent-domain sso_token on login; with no
    # local session, silently sign the shared user in here too.
    if 'user_id' in session:
        return
    shared = sso_bridge.shared_user_for_token(request.cookies.get('sso_token'))
    if not shared:
        return
    uname = shared['username']
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM users WHERE username = ?', (uname,))
    user = c.fetchone()
    if user is None:
        c.execute('INSERT INTO users (username, display_name) VALUES (?, ?)',
                  (uname, uname))
        conn.commit()
        c.execute('SELECT * FROM users WHERE username = ?', (uname,))
        user = c.fetchone()
    conn.close()
    display = user['display_name'] or user['ispace_username'] or user['username']
    set_authenticated_session(user['id'], user['username'], display)


@app.before_request
def refresh_logged_in_session():
    if 'user_id' in session:
        session.permanent = True


def get_analytics_visitor_id():
    if 'analytics_visitor_id' not in session:
        session['analytics_visitor_id'] = uuid.uuid4().hex
        session.permanent = True
    return session['analytics_visitor_id']

# --- Auth Endpoints ---

def _validated_credentials():
    data = request.get_json(silent=True) or {}
    username = data.get('username')
    password = data.get('password')
    if not username or not password:
        return None, None, "Username and password required"
    if not isinstance(username, str) or not isinstance(password, str):
        return None, None, "Username and password must be strings"
    if len(username) > 120 or len(password) > 512:
        return None, None, "Username or password is too long"
    return username, password, None


@app.route('/api/register', methods=['POST'])
def register():
    username, password, error = _validated_credentials()
    if error:
        return jsonify({"error": error}), 400
        
    conn = get_db()
    try:
        password_hash = generate_password_hash(password)
        c = conn.cursor()
        c.execute('INSERT INTO users (username, password_hash) VALUES (?, ?)', (username, password_hash))
        conn.commit()
        return jsonify({"success": True, "message": "Registered successfully"})
    except sqlite3.IntegrityError:
        return jsonify({"error": "Username already exists"}), 400
    finally:
        conn.close()

@app.route('/api/login', methods=['POST'])
def login():
    username, password, error = _validated_credentials()
    if error:
        return jsonify({"error": error}), 400
    
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM users WHERE username = ?', (username,))
    user = c.fetchone()
    conn.close()
    
    stored_password_hash = user['password_hash'] if user else None

    if user and stored_password_hash and check_password_hash(stored_password_hash, password):
        display_name = user['display_name'] if user['display_name'] else user['ispace_username'] or user['username']
        set_authenticated_session(user['id'], user['username'], display_name)
        resp = jsonify({"success": True, "user": {"id": user['id'], "username": user['username'], "ispace_username": user['ispace_username'], "display_name": session['display_name']}})
        sso_bridge.set_sso_cookie(resp, sso_bridge.issue_shared_token(user['username']))
        return resp
    
    return jsonify({"error": "Invalid credentials"}), 401

@app.route('/api/login/ispace', methods=['POST'])
def login_ispace():
    username, password, error = _validated_credentials()
    if error:
        return jsonify({"error": error}), 400
    
    # 1. Verify with iSpace
    result = fetch_timeline(username, password)
    if isinstance(result, dict) and "error" in result:
        return jsonify({"error": "iSpace login failed: " + result["error"]}), 401
        
    # 2. Login successful, get DDLs
    ddls = result
    
    # 3. Create or Update local user
    conn = get_db()
    c = conn.cursor()
    
    # Check if user exists by ispace_username (or just username if they registered with student ID)
    # Strategy: We treat iSpace login as a way to "bind" or "quick login".
    # If a user with this username exists, we log them in. If not, we create a shadow user.
    
    # Resolve by explicit binding first (accounts linked via
    # /api/user/bind/ispace or earlier verified logins), then by same name.
    c.execute('SELECT * FROM users WHERE ispace_username = ? ORDER BY id LIMIT 1', (username,))
    user = c.fetchone()
    if user is None:
        c.execute('SELECT * FROM users WHERE username = ?', (username,))
        user = c.fetchone()
        if user and user['password_hash']:
            # Same-named local PASSWORD account never verified via iSpace:
            # silently merging would hand this login to whoever set that
            # password (student-id squatting). Require the bind flow, which
            # proves both sides.
            conn.close()
            return jsonify({"error": "该学号已被一个本地密码账号占用。若那是你的账号:请先用密码登录,再绑定 iSpace;若不是,请联系管理员。"}), 409

    user_id = None
    display_name = username # Default display name is username (Student ID)

    if user:
        user_id = user['id']
        # Update ispace_username if not set (passwordless same-named account)
        if not user['ispace_username']:
            c.execute('UPDATE users SET ispace_username = ? WHERE id = ?', (username, user_id))
        
        # Use existing display name if set, otherwise use ispace username
        if user['display_name']:
            display_name = user['display_name']
        else:
            # If no display name, default to ispace username (Student ID)
            # We can try to fetch real name from ispace if possible, but for now use ID
            pass
            
    else:
        # Create new user
        # We don't have a local password for them, so we set a dummy hash or handle it.
        # For simplicity, we create a user with username=studentID and no password (so they can only login via iSpace)
        # or we ask them to set a password later.
        # Set display_name to username initially
        c.execute('INSERT INTO users (username, ispace_username, display_name) VALUES (?, ?, ?)', (username, username, username))
        user_id = c.lastrowid
        
    # 4. Sync DDLs to Todos
    sync_stats = sync_ispace_todos_for_user(conn, user_id, ddls)

    conn.commit()
    conn.close()
    
    # Bound accounts keep their own username; the shared SSO identity follows
    # the local account. Same-named (pure iSpace) identities stay ispace=True;
    # a bound self-chosen name is a password identity on the shared side.
    local_username = user['username'] if user else username
    set_authenticated_session(user_id, local_username, display_name)

    resp = jsonify({
        "success": True,
        "user": {"id": user_id, "username": local_username, "ispace_username": username, "display_name": display_name},
        "sync": sync_stats,
    })
    sso_bridge.set_sso_cookie(resp, sso_bridge.issue_shared_token(
        local_username, ispace=(local_username == username)))
    return resp

@app.route('/api/user/bind/ispace', methods=['POST'])
def bind_ispace():
    """Bind an iSpace (student id) identity onto the CURRENTLY logged-in
    account. Proves both sides: the session (local password login) and live
    iSpace credentials. Self-serve path for accounts registered under a
    self-chosen username, and for student-id-named password accounts."""
    if 'user_id' not in session:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.json or {}
    sid = (data.get('username') or '').strip()
    password = data.get('password')
    if not sid or not password:
        return jsonify({"error": "\u5b66\u53f7\u548c iSpace \u5bc6\u7801\u90fd\u9700\u8981\u586b\u5199"}), 400

    # 1. Verify with iSpace (same check as /api/login/ispace)
    result = fetch_timeline(sid, password)
    if isinstance(result, dict) and "error" in result:
        return jsonify({"error": "iSpace login failed: " + result["error"]}), 401
    ddls = result

    conn = get_db()
    c = conn.cursor()
    try:
        c.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],))
        me = c.fetchone()
        if me is None:
            return jsonify({"error": "Unauthorized"}), 401
        if me['ispace_username'] and me['ispace_username'] != sid:
            return jsonify({"error": "\u8be5\u8d26\u53f7\u5df2\u7ed1\u5b9a\u5b66\u53f7 " + me['ispace_username']}), 409
        # the student id must not already be linked to a different account
        c.execute('SELECT id FROM users WHERE ispace_username = ? AND id != ?', (sid, me['id']))
        if c.fetchone():
            return jsonify({"error": "\u8be5\u5b66\u53f7\u5df2\u7ed1\u5b9a\u5176\u4ed6\u8d26\u53f7,\u8bf7\u8054\u7cfb\u7ba1\u7406\u5458"}), 409

        c.execute('UPDATE users SET ispace_username = ? WHERE id = ?', (sid, me['id']))
        sync_stats = sync_ispace_todos_for_user(conn, me['id'], ddls)
        conn.commit()
    finally:
        conn.close()

    return jsonify({"success": True, "ispace_username": sid, "sync": sync_stats})

@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    sso_bridge.revoke_token(request.cookies.get('sso_token'))
    resp = jsonify({"success": True})
    sso_bridge.clear_sso_cookie(resp)
    return resp

@app.route('/api/user', methods=['GET'])
def get_current_user():
    if 'user_id' not in session:
        return jsonify({"user": None})
    
    # Refresh user info from DB
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],))
    user = c.fetchone()
    conn.close()
    
    if not user:
        return jsonify({"user": None})
        
    display_name = user['display_name'] if user['display_name'] else user['ispace_username'] or user['username']
    session['display_name'] = display_name # Sync session
    
    return jsonify({"user": {"id": user['id'], "username": user['username'], "ispace_username": user['ispace_username'], "display_name": display_name}})

@app.route('/api/user/profile', methods=['PUT'])
def update_profile():
    if 'user_id' not in session:
        return jsonify({"error": "Unauthorized"}), 401
    
    data = request.json
    display_name = data.get('display_name')
    
    if not display_name:
        return jsonify({"error": "Display name required"}), 400
        
    conn = get_db()
    c = conn.cursor()
    try:
        c.execute('UPDATE users SET display_name = ? WHERE id = ?', (display_name, session['user_id']))
        conn.commit()
        session['display_name'] = display_name
        return jsonify({"success": True})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route('/api/user/notifications', methods=['GET'])
def get_notification_settings():
    if 'user_id' not in session:
        return jsonify({"error": "Unauthorized"}), 401

    conn = get_db()
    c = conn.cursor()
    c.execute(
        '''
        SELECT username, ispace_username, display_name, email, email_notifications_enabled, email_reminder_hours, email_unsubscribed_at
        FROM users
        WHERE id = ?
        ''',
        (session['user_id'],),
    )
    user = c.fetchone()
    conn.close()

    if not user:
        return jsonify({"error": "User not found"}), 404

    return jsonify(notification_settings_payload(user))


@app.route('/api/user/notifications', methods=['PUT'])
def update_notification_settings():
    if 'user_id' not in session:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    enabled = bool(data.get('enabled'))

    try:
        email = normalize_email(data.get('email'))
    except ValueError as error:
        return jsonify({"error": str(error)}), 400

    if enabled and not email:
        return jsonify({"error": "Email is required when notifications are enabled"}), 400

    reminder_hours = parse_reminder_hours(data.get('reminder_hours'), default_to_existing=False)
    if enabled and not reminder_hours:
        return jsonify({"error": "Choose at least one reminder time"}), 400

    conn = get_db()
    c = conn.cursor()
    c.execute(
        '''
        UPDATE users
        SET email = ?, email_notifications_enabled = ?, email_reminder_hours = ?, email_unsubscribed_at = NULL
        WHERE id = ?
        ''',
        (email or None, 1 if enabled else 0, reminder_hours_to_db(reminder_hours), session['user_id']),
    )
    conn.commit()
    c.execute(
        '''
        SELECT username, ispace_username, display_name, email, email_notifications_enabled, email_reminder_hours, email_unsubscribed_at
        FROM users
        WHERE id = ?
        ''',
        (session['user_id'],),
    )
    user = c.fetchone()
    conn.close()

    return jsonify({"success": True, "settings": notification_settings_payload(user)})


@app.route('/api/user/notifications/test', methods=['POST'])
def send_notification_test_email():
    if 'user_id' not in session:
        return jsonify({"error": "Unauthorized"}), 401

    conn = get_db()
    c = conn.cursor()
    c.execute(
        '''
        SELECT username, ispace_username, display_name, email, email_notifications_enabled, email_reminder_hours, email_unsubscribed_at
        FROM users
        WHERE id = ?
        ''',
        (session['user_id'],),
    )
    user = c.fetchone()
    conn.close()

    if not user:
        return jsonify({"error": "User not found"}), 404
    if not user['email']:
        return jsonify({"error": "Save an email address before sending a test"}), 400
    if not is_email_service_configured():
        return jsonify({"error": "Email service is not configured on the server"}), 503

    display_name = user['display_name'] or user['ispace_username'] or user['username']
    subject = "MAXCOURSE DDL email notification test"
    site_url = get_public_base_url()

    conn = get_db()
    try:
        token = ensure_unsubscribe_token(conn, session['user_id'])
    finally:
        conn.close()
    unsubscribe_url = build_unsubscribe_url(token)

    text_body = (
        f"Hi {display_name},\n\n"
        "Your MAXCOURSE DDL email notification is ready.\n"
        "Reminder times shown in Beijing Time (UTC+8).\n\n"
        f"Open MAXCOURSE: {site_url}\n"
        f"\nUnsubscribe from these reminders: {unsubscribe_url}\n"
    )
    html_body = f"""
        <div style="font-family: system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; color: #101820; line-height: 1.55;">
            <h2>MAXCOURSE DDL notification test</h2>
            <p>Hi {escape(str(display_name))}, your email notification is ready.</p>
            <p style="color: #6b7280; font-size: 13px;">Reminder times are shown in Beijing Time (UTC+8).</p>
            <p><a href="{escape(site_url, quote=True)}" style="display: inline-block; padding: 10px 14px; background: #101820; color: #ffffff; border-radius: 999px; text-decoration: none; font-weight: 700;">Open MAXCOURSE</a></p>
            <p style="margin-top: 24px; font-size: 12px; color: #6b7280;">
                Don't want these reminders?
                <a href="{escape(unsubscribe_url, quote=True)}" style="color: #6b7280; text-decoration: underline;">Unsubscribe with one click</a>.
            </p>
        </div>
    """

    try:
        send_email(user['email'], subject, text_body, html_body, unsubscribe_url=unsubscribe_url)
    except Exception as error:
        app.logger.exception("Failed to send notification test email")
        return jsonify({"error": str(error)}), 502

    return jsonify({"success": True})


@app.route('/api/notifications/unsubscribe', methods=['GET', 'POST'])
def unsubscribe_email_notifications():
    token = (request.args.get('token') or request.form.get('token') or '').strip()
    if not token:
        return jsonify({"error": "Missing token"}), 400

    conn = get_db()
    try:
        c = conn.cursor()
        c.execute('SELECT id, email FROM users WHERE unsubscribe_token = ?', (token,))
        user = c.fetchone()
        if not user:
            return jsonify({"error": "Invalid or expired unsubscribe token"}), 404

        c.execute(
            'UPDATE users SET email_notifications_enabled = 0, email_unsubscribed_at = CURRENT_TIMESTAMP WHERE id = ?',
            (user['id'],),
        )
        conn.commit()
    finally:
        conn.close()

    if request.method == 'POST':
        return jsonify({"success": True})

    site_url = escape(get_public_base_url(), quote=True)
    email_html = escape(user['email'] or '')
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Unsubscribed</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body {{ font-family: system-ui, -apple-system, sans-serif; background: #fbf7ef; color: #101820; padding: 48px 24px; }}
.card {{ max-width: 480px; margin: 0 auto; background: #fff; border: 1px solid #e5e7eb; border-radius: 16px; padding: 32px; box-shadow: 0 4px 12px rgba(0,0,0,.05); }}
h1 {{ margin: 0 0 12px; font-size: 22px; }}
p {{ margin: 0 0 12px; line-height: 1.6; }}
.btn {{ display: inline-block; margin-top: 16px; padding: 10px 16px; background: #101820; color: #fff; border-radius: 999px; text-decoration: none; font-weight: 700; }}
</style></head>
<body><div class="card">
<h1>You're unsubscribed</h1>
<p>DDL reminder emails to <strong>{email_html}</strong> have been turned off.</p>
<p>You can re-enable them anytime in MAXCOURSE notification settings.</p>
<a class="btn" href="{site_url}">Open MAXCOURSE</a>
</div></body></html>"""
    return page, 200, {'Content-Type': 'text/html; charset=utf-8'}


@app.route('/api/teachers', methods=['GET'])
def get_all_teachers():
    try:
        df = get_df()
        # Extract unique teachers and calculate basic stats
        # We need a list of teachers with: name, course_count, avg_rating
        
        # 1. Get all teachers from dataframe
        # Explode the 'Teachers' column if it contains multiple teachers? 
        # The current logic assumes 'Teachers' column is a string. 
        # Let's clean and split if necessary, but current get_courses just lists them.
        # Let's iterate unique values in 'Teachers' column.
        
        all_teachers = set()
        teacher_course_counts = {}
        
        # Safe iteration
        if 'Teachers' in df.columns:
            for teachers_str in df['Teachers'].dropna().astype(str):
                # Split by comma and newline to handle multiple teachers
                parts = teachers_str.replace('\n', ',').split(',')
                for part in parts:
                    t_name = part.strip()
                    if t_name and t_name.lower() != 'nan':
                        all_teachers.add(t_name)
                        teacher_course_counts[t_name] = teacher_course_counts.get(t_name, 0) + 1

        teacher_list = []
        conn = get_db()
        c = conn.cursor()
        
        for name in all_teachers:
            # Get average rating
            c.execute('SELECT rating FROM teacher_ratings WHERE teacher_name = ?', (name,))
            ratings = [r[0] for r in c.fetchall()]
            avg_rating = sum(ratings) / len(ratings) if ratings else 0
            rating_count = len(ratings)
            
            teacher_list.append({
                "name": name,
                "course_count": teacher_course_counts.get(name, 0),
                "average_rating": avg_rating,
                "rating_count": rating_count
            })
            
        conn.close()
        
        # Sort by rating count (popular) then name
        teacher_list.sort(key=lambda x: (-x['rating_count'], x['name']))
        
        return jsonify(teacher_list)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/user/delete', methods=['DELETE'])
def delete_user_data():
    if 'user_id' not in session:
        return jsonify({"error": "Unauthorized"}), 401
        
    user_id = session['user_id']
    conn = get_db()
    c = conn.cursor()
    
    try:
        # 1. Delete all todos
        c.execute('DELETE FROM todos WHERE user_id = ?', (user_id,))

        # Classroom intents are coordination records, not public history.
        c.execute('DELETE FROM classroom_intents WHERE user_id = ?', (user_id,))
        
        # 2. Unlink teacher ratings (set user_id to NULL to preserve rating but remove link)
        # Note: We need to check if schema allows NULL.
        # Schema: FOREIGN KEY(user_id) REFERENCES users(id)
        # It doesn't explicitly say NOT NULL, so it should allow NULL.
        c.execute('UPDATE teacher_ratings SET user_id = NULL, is_anonymous = 1 WHERE user_id = ?', (user_id,))
        
        # 3. Delete user
        c.execute('DELETE FROM users WHERE id = ?', (user_id,))
        
        conn.commit()
        session.clear()
        return jsonify({"success": True})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/teachers/<path:name>', methods=['GET'])
def get_teacher_profile(name):
    try:
        df = get_df()
        # Find courses where 'Teachers' column contains the name or equals it
        # For simplicity and performance, we'll iterate through cached data structure if possible, 
        # but here we use pandas.
        # Note: The name comes from the frontend which took it from 'Teachers' column.
        # We will look for rows where the 'Teachers' column value matches exactly what was sent,
        # OR contains it if we want to be more flexible. Given the frontend logic, exact match on the cell value is safest
        # to replicate the 'grouping' seen in ExplorerView, but ideally we want "All courses by Dr. X".
        # Let's do a contains search to be more helpful.
        
        # Safe string search handling NaN
        teacher_courses_df = df[df['Teachers'].astype(str).str.contains(name, regex=False, case=False, na=False)]
        
        courses_list = []
        if not teacher_courses_df.empty:
            for _, row in teacher_courses_df.iterrows():
                row_data = row.where(pd.notnull(row), "").to_dict()
                # Clean up keys for frontend
                clean_row = {
                    "code": row_data.get('Course Code', ''),
                    "title": row_data.get('Course Title & Session', ''),
                    "units": row_data.get('Units', ''),
                    "schedule": row_data.get('Class Schedule', ''),
                    "classroom": row_data.get('Classroom', ''),
                    "teachers": row_data.get('Teachers', '')
                }
                courses_list.append(clean_row)
        
        # Get ratings
        conn = get_db()
        c = conn.cursor()
        
        # Get ratings and comments
        c.execute('SELECT rating, comment, is_anonymous, user_id, created_at, course_info FROM teacher_ratings WHERE teacher_name = ? ORDER BY created_at DESC', (name,))
        rows = c.fetchall()
        
        ratings = [r['rating'] for r in rows]
        comments = []
        
        for r in rows:
            if r['comment']:
                username = "Anonymous"
                if not r['is_anonymous']:
                    # Fetch username and display_name
                    c2 = conn.cursor()
                    c2.execute('SELECT username, display_name FROM users WHERE id = ?', (r['user_id'],))
                    u = c2.fetchone()
                    if u:
                        username = u['display_name'] if u['display_name'] else u['username']
                
                comments.append({
                    "rating": r['rating'],
                    "comment": r['comment'],
                    "user": username,
                    "date": r['created_at'],
                    "course": r['course_info']
                })
        
        avg_rating = sum(ratings) / len(ratings) if ratings else 0
        
        # Check if current user has rated
        user_rating = None
        user_comment = ""
        user_is_anonymous = False
        user_course = ""
        
        if 'user_id' in session:
            c.execute('SELECT rating, comment, is_anonymous, course_info FROM teacher_ratings WHERE teacher_name = ? AND user_id = ?', (name, session['user_id']))
            row = c.fetchone()
            if row:
                user_rating = row['rating']
                user_comment = row['comment']
                user_is_anonymous = bool(row['is_anonymous'])
                user_course = row['course_info']
        
        conn.close()

        return jsonify({
            "name": name,
            "courses": courses_list,
            "average_rating": avg_rating,
            "rating_count": len(ratings),
            "user_rating": user_rating,
            "user_comment": user_comment,
            "user_is_anonymous": user_is_anonymous,
            "user_course": user_course,
            "comments": comments
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/teachers/<path:name>/rate', methods=['POST'])
def rate_teacher(name):
    if 'user_id' not in session:
        return jsonify({"error": "Unauthorized"}), 401
    
    data = request.json
    rating = data.get('rating')
    comment = data.get('comment', '')
    is_anonymous = data.get('is_anonymous', False)
    course_info = data.get('course', '')
    
    if not isinstance(rating, int) or not (1 <= rating <= 5):
        return jsonify({"error": "Invalid rating (1-5)"}), 400
        
    conn = get_db()
    c = conn.cursor()
    
    # Upsert rating
    c.execute('SELECT id FROM teacher_ratings WHERE teacher_name = ? AND user_id = ?', (name, session['user_id']))
    exists = c.fetchone()
    
    if exists:
        c.execute('UPDATE teacher_ratings SET rating = ?, comment = ?, is_anonymous = ?, course_info = ?, created_at = CURRENT_TIMESTAMP WHERE id = ?', (rating, comment, is_anonymous, course_info, exists[0]))
    else:
        c.execute('INSERT INTO teacher_ratings (teacher_name, user_id, rating, comment, is_anonymous, course_info) VALUES (?, ?, ?, ?, ?, ?)', (name, session['user_id'], rating, comment, is_anonymous, course_info))
        
    conn.commit()
    conn.close()
    
    return jsonify({"success": True})

# --- Todo Endpoints ---

@app.route('/api/todos', methods=['GET'])
def get_todos():
    if 'user_id' not in session:
        return jsonify({"error": "Unauthorized"}), 401
        
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM todos WHERE user_id = ? AND COALESCE(is_stale, 0) = 0 ORDER BY due_date ASC, id ASC', (session['user_id'],))
    todos = [dict(row) for row in c.fetchall()]
    conn.close()
    
    return jsonify(todos)

@app.route('/api/todos/sync', methods=['POST'])
def sync_todos():
    if 'user_id' not in session:
        return jsonify({"error": "Unauthorized"}), 401
        
    data = request.json
    ispace_user = data.get('username')
    ispace_pass = data.get('password')
    
    if not ispace_user or not ispace_pass:
         return jsonify({"error": "Credentials required"}), 400

    user_id = session['user_id']
    submitted_ispace_user = str(ispace_user).strip()

    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT ispace_username FROM users WHERE id = ?', (user_id,))
    user = c.fetchone()
    conn.close()

    linked_ispace_user = str(user['ispace_username']).strip() if user and user['ispace_username'] else ''
    if not linked_ispace_user or linked_ispace_user != submitted_ispace_user:
        return jsonify({
            "error": "iSpace account mismatch. Please log in with the matching iSpace account before syncing."
        }), 403

    result = fetch_timeline(ispace_user, ispace_pass)
    if isinstance(result, dict) and "error" in result:
        return jsonify(result), 400
        
    conn = get_db()
    c = conn.cursor()
    
    sync_stats = sync_ispace_todos_for_user(conn, user_id, result)

    conn.commit()
    conn.close()
    
    return jsonify({"success": True, **sync_stats})

@app.route('/api/todos/add', methods=['POST'])
def add_todo():
    if 'user_id' not in session:
        return jsonify({"error": "Unauthorized"}), 401
    
    data = request.json
    title = data.get('title')
    course = data.get('course')
    description = data.get('description')
    due_date = data.get('due_date') # Unix timestamp or ISO string
    
    if not title:
        return jsonify({"error": "Title required"}), 400
        
    # Convert ISO date string to timestamp if necessary
    if isinstance(due_date, str):
        try:
            dt = datetime.fromisoformat(due_date.replace('Z', '+00:00'))
            due_date = int(dt.timestamp())
        except ValueError:
            pass # Assume it's already int or handle error
            
    conn = get_db()
    c = conn.cursor()
    c.execute('''
        INSERT INTO todos (user_id, title, course, description, due_date, is_completed)
        VALUES (?, ?, ?, ?, ?, 0)
    ''', (session['user_id'], title, course, description, due_date))
    conn.commit()
    new_id = c.lastrowid
    conn.close()
    
    return jsonify({"success": True, "id": new_id})

@app.route('/api/todos/<int:todo_id>', methods=['PUT'])
def update_todo(todo_id):
    if 'user_id' not in session:
        return jsonify({"error": "Unauthorized"}), 401
    
    data = request.json
    description = data.get('description')
    
    conn = get_db()
    c = conn.cursor()
    c.execute('UPDATE todos SET description = ? WHERE id = ? AND user_id = ?', (description, todo_id, session['user_id']))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

@app.route('/api/todos/<int:todo_id>/complete', methods=['POST'])
def complete_todo(todo_id):
    if 'user_id' not in session:
        return jsonify({"error": "Unauthorized"}), 401
        
    conn = get_db()
    c = conn.cursor()
    c.execute('UPDATE todos SET is_completed = 1 WHERE id = ? AND user_id = ?', (todo_id, session['user_id']))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

@app.route('/api/todos/<int:todo_id>/incomplete', methods=['POST'])
def incomplete_todo(todo_id):
    if 'user_id' not in session:
        return jsonify({"error": "Unauthorized"}), 401
        
    conn = get_db()
    c = conn.cursor()
    c.execute('UPDATE todos SET is_completed = 0 WHERE id = ? AND user_id = ?', (todo_id, session['user_id']))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

@app.route('/api/todos/<int:todo_id>', methods=['DELETE'])
def delete_todo(todo_id):
    if 'user_id' not in session:
        return jsonify({"error": "Unauthorized"}), 401
        
    conn = get_db()
    c = conn.cursor()
    c.execute('DELETE FROM todos WHERE id = ? AND user_id = ?', (todo_id, session['user_id']))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route('/api/notifications/dispatch', methods=['POST'])
def dispatch_due_email_notifications():
    expected_secret = os.getenv('MAXCOURSE_NOTIFICATION_SECRET', '').strip()
    if not expected_secret:
        return jsonify({"error": "Notification dispatch secret is not configured"}), 503

    supplied_secret = request.headers.get('X-Notification-Secret', '').strip()
    if not supplied_secret or not secrets.compare_digest(supplied_secret, expected_secret):
        return jsonify({"error": "Unauthorized"}), 401

    # Piggyback the daily page-view rollup on the cron heartbeat so daily totals are
    # persisted even on days nobody opens the dashboard. Best-effort; never blocks email.
    try:
        _rconn = get_db()
        rollup_daily_page_stats(_rconn, recent_days=2)
        _rconn.commit()
        _rconn.close()
    except Exception:
        app.logger.warning('daily_page_stats rollup (cron) failed', exc_info=True)

    if not is_email_service_configured():
        return jsonify({"error": "Email service is not configured on the server"}), 503

    data = request.get_json(silent=True) or {}
    dry_run = bool(data.get('dry_run'))
    now_ts = int(time.time())
    max_window_seconds = max(EMAIL_REMINDER_CHOICES) * 60 * 60

    conn = get_db()
    c = conn.cursor()
    c.execute(
        '''
        SELECT
            todos.id AS todo_id,
            todos.title,
            todos.course,
            todos.due_date,
            todos.url,
            users.id AS user_id,
            users.username,
            users.ispace_username,
            users.display_name,
            users.email,
            users.email_reminder_hours
        FROM todos
        JOIN users ON users.id = todos.user_id
        WHERE COALESCE(users.email_notifications_enabled, 0) = 1
          AND users.email IS NOT NULL
          AND users.email != ''
          AND COALESCE(todos.is_completed, 0) = 0
          AND COALESCE(todos.is_stale, 0) = 0
          AND todos.due_date > ?
          AND todos.due_date <= ?
        ORDER BY todos.due_date ASC
        ''',
        (now_ts, now_ts + max_window_seconds),
    )
    candidates = c.fetchall()

    checked = 0
    sent = 0
    skipped = 0
    failed = 0
    errors = []

    for row in candidates:
        reminder_hours = parse_reminder_hours(row['email_reminder_hours'])
        remaining_seconds = int(row['due_date']) - now_ts
        eligible = [hour for hour in sorted(reminder_hours) if remaining_seconds <= hour * 60 * 60]
        if not eligible:
            skipped += 1
            continue

        reminder_hour = eligible[0]
        checked += 1
        c.execute(
            '''
            SELECT
                SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) AS success_count,
                SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) AS failure_count
            FROM email_notification_deliveries
            WHERE user_id = ? AND todo_id = ? AND reminder_hours = ?
            ''',
            (row['user_id'], row['todo_id'], reminder_hour),
        )
        delivery_stats = c.fetchone()
        success_count = (delivery_stats['success_count'] if delivery_stats else 0) or 0
        failure_count = (delivery_stats['failure_count'] if delivery_stats else 0) or 0
        if success_count > 0:
            skipped += 1
            continue
        if failure_count >= EMAIL_MAX_DELIVERY_ATTEMPTS:
            skipped += 1
            continue

        if dry_run:
            continue

        try:
            unsubscribe_token = ensure_unsubscribe_token(conn, row['user_id'])
            unsubscribe_url = build_unsubscribe_url(unsubscribe_token)
            subject, text_body, html_body = build_todo_reminder_email(row, row, reminder_hour, unsubscribe_url=unsubscribe_url)
            send_email(row['email'], subject, text_body, html_body, unsubscribe_url=unsubscribe_url)
            c.execute(
                '''
                INSERT INTO email_notification_deliveries (user_id, todo_id, reminder_hours, success, error)
                VALUES (?, ?, ?, 1, NULL)
                ''',
                (row['user_id'], row['todo_id'], reminder_hour),
            )
            conn.commit()
            sent += 1
        except Exception as error:
            failed += 1
            message = str(error)[:500]
            errors.append({"todo_id": row['todo_id'], "user_id": row['user_id'], "error": message})
            c.execute(
                '''
                INSERT INTO email_notification_deliveries (user_id, todo_id, reminder_hours, success, error)
                VALUES (?, ?, ?, 0, ?)
                ''',
                (row['user_id'], row['todo_id'], reminder_hour, message),
            )
            conn.commit()
            app.logger.exception("Failed to send DDL reminder email")

    conn.close()

    return jsonify({
        "success": failed == 0,
        "dry_run": dry_run,
        "candidates": len(candidates),
        "checked": checked,
        "sent": sent,
        "skipped": skipped,
        "failed": failed,
        "errors": errors[:10],
    })

# --- Existing API Endpoints ---

@app.route('/api/ddl', methods=['POST'])
def get_ddl():
    # Keep this for backward compatibility or direct checking
    data = request.json
    username = data.get('username')
    password = data.get('password')
    
    if not username or not password:
        return jsonify({"error": "Username and password required"}), 400
        
    result = fetch_timeline(username, password)
    
    if isinstance(result, dict) and "error" in result:
        return jsonify(result), 400
        
    return jsonify(result)


@app.route('/api/analytics/track', methods=['POST'])
def track_page_view():
    data = request.get_json(silent=True) or {}
    view_name = str(data.get('view') or 'unknown').strip()[:80]
    path = str(data.get('path') or request.referrer or '').strip()[:300]
    referrer = str(data.get('referrer') or request.referrer or '').strip()[:300]
    user_agent = str(request.headers.get('User-Agent') or '').strip()[:300]
    visitor_id = get_analytics_visitor_id()
    user_id = session.get('user_id')

    conn = get_db()
    c = conn.cursor()
    c.execute(
        '''
        INSERT INTO page_views (visitor_id, user_id, view_name, path, referrer, user_agent)
        VALUES (?, ?, ?, ?, ?, ?)
        ''',
        (visitor_id, user_id, view_name, path, referrer, user_agent),
    )
    conn.commit()
    conn.close()

    return jsonify({"success": True})


def _classify_referrer_host(referrer, self_host):
    """Return (host, is_external) for a stored referrer URL, or None to drop it."""
    if not referrer:
        return None
    from urllib.parse import urlparse
    try:
        host = (urlparse(referrer).hostname or '').lower()
    except Exception:
        host = ''
    if not host:
        return None
    if host.startswith('www.'):
        host = host[4:]
    internal = (
        host == self_host
        or host in ('localhost', '127.0.0.1', '0.0.0.0')
        or host.endswith('.bnbscheduler.top')
        or host == 'bnbscheduler.top'
    )
    return (host, not internal)


@app.route('/api/analytics/summary', methods=['GET'])
def get_analytics_summary():
    conn = get_db()
    c = conn.cursor()
    off = BEIJING_SQL_OFFSET

    # ---- Headline totals (all-time) ----
    total_views = c.execute('SELECT COUNT(*) FROM page_views').fetchone()[0]
    unique_visitors = c.execute('SELECT COUNT(DISTINCT visitor_id) FROM page_views').fetchone()[0]
    registered_visitors = c.execute(
        'SELECT COUNT(DISTINCT user_id) FROM page_views WHERE user_id IS NOT NULL'
    ).fetchone()[0]

    # ---- Windowed totals (Beijing day boundaries) ----
    def window(views_sql):
        row = c.execute(views_sql).fetchone()
        return {"views": row[0] or 0, "visitors": row[1] or 0}

    today = window(
        f"SELECT COUNT(*), COUNT(DISTINCT visitor_id) FROM page_views "
        f"WHERE date(created_at, '{off}') = date('now', '{off}')"
    )
    last7d = window(
        f"SELECT COUNT(*), COUNT(DISTINCT visitor_id) FROM page_views "
        f"WHERE date(created_at, '{off}') >= date('now', '{off}', '-6 days')"
    )
    last30d = window(
        f"SELECT COUNT(*), COUNT(DISTINCT visitor_id) FROM page_views "
        f"WHERE date(created_at, '{off}') >= date('now', '{off}', '-29 days')"
    )

    # Visitors whose very first pageview landed today (net-new audience).
    new_visitors_today = c.execute(
        f'''
        SELECT COUNT(*) FROM (
            SELECT visitor_id, MIN(created_at) AS first_seen
            FROM page_views GROUP BY visitor_id
        ) WHERE date(first_seen, '{off}') = date('now', '{off}')
        '''
    ).fetchone()[0]

    # ---- Per-view breakdown, with a 7-day trend column ----
    c.execute(
        f'''
        SELECT view_name,
               COUNT(*) AS views,
               COUNT(DISTINCT visitor_id) AS visitors,
               SUM(CASE WHEN date(created_at, '{off}') >= date('now', '{off}', '-6 days')
                        THEN 1 ELSE 0 END) AS views_7d
        FROM page_views
        GROUP BY view_name
        ORDER BY views DESC, view_name ASC
        '''
    )
    by_view = [dict(row) for row in c.fetchall()]

    # ---- Daily trend, served from the durable rollup (self-correcting) ----
    try:
        days = int(request.args.get('days', 30))
    except (TypeError, ValueError):
        days = 30
    days = max(1, min(days, 365))
    # Refresh the tail (today/yesterday) from raw so the newest hits show, then read
    # the persisted rollup. Past days are immutable, so refreshing 2 days is enough.
    try:
        rollup_daily_page_stats(conn, recent_days=2)
        conn.commit()
    except sqlite3.OperationalError:
        pass
    c.execute(
        f'''
        SELECT day, views, visitors FROM daily_page_stats
        WHERE day >= date('now', '{off}', '-{days - 1} days')
        ORDER BY day ASC
        '''
    )
    daily = [dict(row) for row in c.fetchall()]
    _dmeta = c.execute('SELECT COUNT(*) AS n, MIN(day) AS first FROM daily_page_stats').fetchone()
    daily_recorded_days = _dmeta['n'] or 0
    daily_first_day = _dmeta['first']

    # ---- Hour-of-day distribution (last 30 Beijing days) ----
    c.execute(
        f'''
        SELECT CAST(strftime('%H', datetime(created_at, '{off}')) AS INTEGER) AS hour,
               COUNT(*) AS views
        FROM page_views
        WHERE date(created_at, '{off}') >= date('now', '{off}', '-29 days')
        GROUP BY hour ORDER BY hour ASC
        '''
    )
    hourly_map = {int(row['hour']): row['views'] for row in c.fetchall()}
    hourly = [{"hour": h, "views": hourly_map.get(h, 0)} for h in range(24)]

    # ---- Device split from user-agent (last 30 Beijing days) ----
    c.execute(
        f'''
        SELECT
            CASE
                WHEN user_agent IS NULL OR user_agent = '' THEN 'unknown'
                WHEN user_agent LIKE '%bot%' OR user_agent LIKE '%spider%'
                     OR user_agent LIKE '%crawl%' OR user_agent LIKE '%slurp%'
                     OR user_agent LIKE '%HeadlessChrome%' OR user_agent LIKE '%bingpreview%' THEN 'bot'
                WHEN user_agent LIKE '%iPad%' OR user_agent LIKE '%Tablet%'
                     OR (user_agent LIKE '%Android%' AND user_agent NOT LIKE '%Mobile%') THEN 'tablet'
                WHEN user_agent LIKE '%Mobile%' OR user_agent LIKE '%Android%'
                     OR user_agent LIKE '%iPhone%' OR user_agent LIKE '%iPod%' THEN 'mobile'
                ELSE 'desktop'
            END AS device,
            COUNT(*) AS views,
            COUNT(DISTINCT visitor_id) AS visitors
        FROM page_views
        WHERE date(created_at, '{off}') >= date('now', '{off}', '-29 days')
        GROUP BY device
        '''
    )
    devices = [dict(row) for row in c.fetchall()]
    devices.sort(key=lambda d: d['views'], reverse=True)

    # ---- Referrers (last 30 Beijing days), host-aggregated, external vs internal ----
    c.execute(
        f'''
        SELECT referrer, COUNT(*) AS n
        FROM page_views
        WHERE referrer IS NOT NULL AND referrer <> ''
          AND date(created_at, '{off}') >= date('now', '{off}', '-29 days')
        GROUP BY referrer
        '''
    )
    self_host = (request.host or '').split(':')[0].lower()
    if self_host.startswith('www.'):
        self_host = self_host[4:]
    ext_hosts = Counter()
    internal_hits = 0
    for row in c.fetchall():
        cls = _classify_referrer_host(row['referrer'], self_host)
        if cls is None:
            continue
        host, is_external = cls
        if is_external:
            ext_hosts[host] += row['n']
        else:
            internal_hits += row['n']
    referrers = [{"host": h, "count": n} for h, n in ext_hosts.most_common(12)]

    conn.close()

    now_bj = datetime.now(timezone(timedelta(hours=8)))

    return jsonify({
        # Back-compat top-level keys (older cached stats page reads these).
        "totalViews": total_views,
        "uniqueVisitors": unique_visitors,
        "todayViews": today["views"],
        "byView": by_view,
        # Enriched payload.
        "generatedAt": now_bj.isoformat(timespec='seconds'),
        "timezone": "UTC+8",
        "totals": {
            "views": total_views,
            "visitors": unique_visitors,
            "registeredVisitors": registered_visitors,
            "viewsToday": today["views"],
            "visitorsToday": today["visitors"],
            "newVisitorsToday": new_visitors_today,
            "views7d": last7d["views"],
            "visitors7d": last7d["visitors"],
            "views30d": last30d["views"],
            "visitors30d": last30d["visitors"],
        },
        "daily": daily,
        "dailyRecordedDays": daily_recorded_days,
        "dailyFirstDay": daily_first_day,
        "hourly": hourly,
        "devices": devices,
        "referrers": referrers,
        "internalReferrals": internal_hits,
        # In-memory since last restart; enough to confirm the guards are firing.
        "antiScrape": dict(antiscrape_stats),
    })


@app.route('/api/classroom-intents', methods=['POST'])
def create_classroom_intent():
    if 'user_id' not in session:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    room = str(data.get('room') or '').strip().upper()
    purpose = str(data.get('purpose') or '').strip()
    party_size = data.get('party_size')
    now = _classroom_now()

    try:
        use_date = _parse_classroom_date(data.get('date'))
        start_min = _parse_classroom_clock(data.get('start'))
        end_min = _parse_classroom_clock(data.get('end'))
    except ValueError:
        return jsonify({"error": "Invalid date or time"}), 400

    if not room or len(room) > 40:
        return jsonify({"error": "Invalid room"}), 400
    if purpose not in CLASSROOM_INTENT_PURPOSES:
        return jsonify({"error": "Invalid purpose"}), 400
    if isinstance(party_size, bool) or not isinstance(party_size, int):
        return jsonify({"error": "Invalid party size"}), 400
    if not 1 <= party_size <= CLASSROOM_INTENT_MAX_PARTY_SIZE:
        return jsonify({"error": "Invalid party size"}), 400
    if end_min <= start_min:
        return jsonify({"error": "End time must be later than start time"}), 400
    if use_date < now.date() or use_date > now.date() + timedelta(days=CLASSROOM_INTENT_MAX_DAYS_AHEAD):
        return jsonify({"error": "Date is outside the available range"}), 400
    now_min = now.hour * 60 + now.minute
    if use_date == now.date() and start_min + CLASSROOM_INTENT_GRACE_MINUTES < now_min:
        return jsonify({"error": "This time slot has already started"}), 400

    rooms, room_entries = build_classroom_index()
    valid_rooms = {item['room'] for item in rooms}
    if room not in valid_rooms:
        return jsonify({"error": "Room not found"}), 404

    day_index = use_date.weekday()
    has_scheduled_class = any(
        entry['day_index'] == day_index
        and start_min < entry['end_min']
        and entry['start_min'] < end_min
        for entry in room_entries.get(room, [])
    )
    if has_scheduled_class:
        return jsonify({"error": "Room has a scheduled class"}), 409

    conn = get_db()
    try:
        conn.execute('BEGIN IMMEDIATE')
        _close_stale_classroom_intents(conn, now)
        active_count = conn.execute(
            '''
            SELECT COUNT(*) FROM classroom_intents
            WHERE user_id = ? AND status IN ('planned', 'checked_in')
            ''',
            (session['user_id'],),
        ).fetchone()[0]
        if active_count >= CLASSROOM_INTENT_MAX_ACTIVE_PER_USER:
            conn.rollback()
            return jsonify({"error": "Too many active classroom intents"}), 409

        overlap = conn.execute(
            '''
            SELECT 1 FROM classroom_intents
            WHERE user_id = ? AND use_date = ?
              AND status IN ('planned', 'checked_in')
              AND start_min < ? AND end_min > ?
            LIMIT 1
            ''',
            (session['user_id'], use_date.isoformat(), end_min, start_min),
        ).fetchone()
        if overlap:
            conn.rollback()
            return jsonify({"error": "You already have an overlapping classroom intent"}), 409

        cursor = conn.execute(
            '''
            INSERT INTO classroom_intents
                (user_id, room, use_date, start_min, end_min, purpose, party_size)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                session['user_id'], room, use_date.isoformat(), start_min,
                end_min, purpose, party_size,
            ),
        )
        row = conn.execute(
            '''
            SELECT id, room, use_date, start_min, end_min, purpose, party_size, status
            FROM classroom_intents WHERE id = ?
            ''',
            (cursor.lastrowid,),
        ).fetchone()
        conn.commit()
        return jsonify({"intent": _serialize_classroom_intent(row, now)}), 201
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.route('/api/classroom-intents/<int:intent_id>/check-in', methods=['POST'])
def check_in_classroom_intent(intent_id):
    if 'user_id' not in session:
        return jsonify({"error": "Unauthorized"}), 401

    now = _classroom_now()
    now_min = now.hour * 60 + now.minute
    conn = get_db()
    try:
        conn.execute('BEGIN IMMEDIATE')
        _close_stale_classroom_intents(conn, now)
        row = conn.execute(
            '''
            SELECT id, room, use_date, start_min, end_min, purpose, party_size, status
            FROM classroom_intents WHERE id = ? AND user_id = ?
            ''',
            (intent_id, session['user_id']),
        ).fetchone()
        if row is None:
            conn.rollback()
            return jsonify({"error": "Intent not found"}), 404
        if row['status'] != 'planned':
            conn.rollback()
            return jsonify({"error": "Intent cannot be checked in"}), 409
        if (
            row['use_date'] != now.date().isoformat()
            or now_min < row['start_min'] - CLASSROOM_INTENT_GRACE_MINUTES
            or now_min > row['start_min'] + CLASSROOM_INTENT_GRACE_MINUTES
        ):
            conn.rollback()
            return jsonify({"error": "Check-in is not available yet"}), 409

        conn.execute(
            '''
            UPDATE classroom_intents
            SET status = 'checked_in', checked_in_at = CURRENT_TIMESTAMP
            WHERE id = ?
            ''',
            (intent_id,),
        )
        row = conn.execute(
            '''
            SELECT id, room, use_date, start_min, end_min, purpose, party_size, status
            FROM classroom_intents WHERE id = ?
            ''',
            (intent_id,),
        ).fetchone()
        conn.commit()
        return jsonify({"intent": _serialize_classroom_intent(row, now)})
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.route('/api/classroom-intents/<int:intent_id>', methods=['DELETE'])
def end_classroom_intent(intent_id):
    if 'user_id' not in session:
        return jsonify({"error": "Unauthorized"}), 401

    now = _classroom_now()
    conn = get_db()
    try:
        conn.execute('BEGIN IMMEDIATE')
        _close_stale_classroom_intents(conn, now)
        row = conn.execute(
            '''
            SELECT status FROM classroom_intents WHERE id = ? AND user_id = ?
            ''',
            (intent_id, session['user_id']),
        ).fetchone()
        if row is None:
            conn.rollback()
            return jsonify({"error": "Intent not found"}), 404
        if row['status'] not in ('planned', 'checked_in'):
            conn.rollback()
            return jsonify({"error": "Intent is no longer active"}), 409

        final_status = 'ended' if row['status'] == 'checked_in' else 'cancelled'
        conn.execute(
            '''
            UPDATE classroom_intents SET status = ?, ended_at = CURRENT_TIMESTAMP
            WHERE id = ?
            ''',
            (final_status, intent_id),
        )
        conn.commit()
        return jsonify({"success": True, "status": final_status})
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.route('/api/free-classrooms', methods=['GET'])
def get_free_classrooms():
    now = _classroom_now()
    day = str(request.args.get('day', 'Mon')).strip()
    start = str(request.args.get('start', '08:00')).strip()
    end = str(request.args.get('end', '08:50')).strip()

    if day not in DAY_MAP:
        return jsonify({"error": "Invalid day"}), 400

    try:
        start_min = _parse_classroom_clock(start)
        end_min = _parse_classroom_clock(end)
    except ValueError:
        return jsonify({"error": "Invalid time format, expected HH:MM"}), 400
    try:
        date_arg = request.args.get('date')
        use_date = _parse_classroom_date(date_arg) if date_arg else _next_classroom_date(day, now.date())
    except ValueError:
        return jsonify({"error": "Invalid date format, expected YYYY-MM-DD"}), 400

    if end_min <= start_min:
        return jsonify({"error": "End time must be later than start time"}), 400
    if use_date.weekday() != DAY_MAP[day]:
        return jsonify({"error": "Date does not match day"}), 400
    if use_date < now.date() or use_date > now.date() + timedelta(days=CLASSROOM_INTENT_MAX_DAYS_AHEAD):
        return jsonify({"error": "Date is outside the available range"}), 400

    rooms, room_entries = build_classroom_index()
    day_index = DAY_MAP[day]
    intent_summaries = _classroom_intent_summaries(
        use_date.isoformat(),
        start_min,
        end_min,
        user_id=session.get('user_id'),
        now=now,
    )
    now_min = now.hour * 60 + now.minute
    if use_date > now.date() or start_min + CLASSROOM_INTENT_GRACE_MINUTES >= now_min:
        registration_state = "open"
    elif end_min > now_min:
        registration_state = "started"
    else:
        registration_state = "past"
    registration_open = registration_state == "open"

    free_rooms = []
    building_totals = Counter()
    free_buildings = Counter()

    for room_info in rooms:
        room = room_info["room"]
        building = room_info["building"]
        building_totals[building] += 1

        entries = [entry for entry in room_entries.get(room, []) if entry["day_index"] == day_index]
        has_conflict = any(start_min < entry["end_min"] and entry["start_min"] < end_min for entry in entries)
        if has_conflict:
            continue

        previous_busy = None
        next_busy = None

        for entry in entries:
            if entry["end_min"] <= start_min:
                if previous_busy is None or entry["end_min"] > previous_busy["end_min"]:
                    previous_busy = entry
            if entry["start_min"] >= end_min:
                if next_busy is None or entry["start_min"] < next_busy["start_min"]:
                    next_busy = entry

        free_buildings[building] += 1
        free_rooms.append({
            "room": room,
            "building": building,
            "free_until": minutes_to_time(next_busy["start_min"]) if next_busy else minutes_to_time(SCHOOL_DAY_END_MINUTES),
            "previous_busy": serialize_room_event(previous_busy) if previous_busy else None,
            "next_busy": serialize_room_event(next_busy) if next_busy else None,
            "intent": intent_summaries.get(room, _empty_classroom_intent_summary()),
        })

    free_rooms.sort(key=lambda item: (building_sort_key(item["building"]), item["room"]))
    total_rooms = len(rooms)

    building_summary = []
    for building in sorted(building_totals.keys(), key=building_sort_key):
        total = building_totals[building]
        free = free_buildings.get(building, 0)
        building_summary.append({
            "building": building,
            "total_rooms": total,
            "free_rooms": free,
            "occupied_rooms": total - free,
        })

    return jsonify({
        "query": {
            "day": day,
            "day_label": DAY_LABELS.get(day, day),
            "date": use_date.isoformat(),
            "start": minutes_to_time(start_min),
            "end": minutes_to_time(end_min),
            "registration_open": registration_open,
            "registration_state": registration_state,
        },
        "summary": {
            "total_rooms": total_rooms,
            "free_rooms": len(free_rooms),
            "occupied_rooms": total_rooms - len(free_rooms),
        },
        "buildings": building_summary,
        "rooms": free_rooms,
    })


@app.route('/api/classroom/<path:room>/schedule', methods=['GET'])
def get_classroom_schedule(room):
    room_clean = (room or '').strip()
    if not room_clean:
        return jsonify({"error": "Invalid room"}), 400

    rooms, room_entries = build_classroom_index()
    if room_clean not in room_entries:
        room_meta = next((r for r in rooms if r["room"] == room_clean), None)
        if room_meta is None:
            return jsonify({"error": "Room not found"}), 404
        building = room_meta["building"]
    else:
        room_meta = next((r for r in rooms if r["room"] == room_clean), None)
        building = room_meta["building"] if room_meta else extract_building(room_clean)

    entries = room_entries.get(room_clean, [])
    days = []
    for day in DAY_SEQUENCE:
        day_idx = DAY_MAP[day]
        day_events = [
            {
                "start": minutes_to_time(e["start_min"]),
                "end": minutes_to_time(e["end_min"]),
                "start_min": e["start_min"],
                "end_min": e["end_min"],
                "course_code": e["course_code"],
                "title": e["title"],
                "teacher": e["teacher"],
            }
            for e in entries if e["day_index"] == day_idx
        ]
        days.append({
            "day": day,
            "day_label": DAY_LABELS.get(day, day),
            "events": day_events,
        })

    return jsonify({
        "room": room_clean,
        "building": building,
        "total_events": len(entries),
        "days": days,
    })

# Historical semester offerings baked by build_semesters.py; the current
# semester always comes live from the timetable xlsx (get_df).
SEMESTERS_INDEX_PATH = os.path.join(APP_ROOT, 'semesters_index.json')
CURRENT_SEMESTER_LABEL = '26-27 第一学期'
# Academic-year start + semester number of the current timetable; used by the
# frontend to map an admission cohort to its current study year (Y = ay_start -
# cohort + 1). Update together with CURRENT_SEMESTER_LABEL each semester swap.
CURRENT_SEMESTER_AY_START = 2026
CURRENT_SEMESTER_NO = 1
_semesters_index_cache = {"mtime": None, "data": None}
_semester_caches = {}


def get_semesters_index():
    data = _load_json_cached(SEMESTERS_INDEX_PATH, _semesters_index_cache)
    return data if isinstance(data, list) else []


def get_semester_courses(key):
    cache = _semester_caches.setdefault(key, {"mtime": None, "data": None})
    path = os.path.join(APP_ROOT, f'course_semester_{key}.json')
    data = _load_json_cached(path, cache)
    return data if isinstance(data, list) else []


def _group_df_courses():
    df = get_df()
    courses = []
    for code, group in df.groupby('Course Code'):
        title_full = str(group['Course Title & Session'].iloc[0])
        title = title_full.split('(')[0].strip()
        teachers = [str(t) for t in group['Teachers'].unique().tolist() if pd.notna(t)]
        details = []
        for _, row in group.iterrows():
            details.append(row.where(pd.notnull(row), "").to_dict())
        courses.append({"code": code, "name": title, "teachers": teachers, "details": details})
    return courses


@app.route('/api/semesters', methods=['GET'])
def list_semesters():
    return jsonify({
        "current": {"key": "current", "label": CURRENT_SEMESTER_LABEL,
                    "ay_start": CURRENT_SEMESTER_AY_START, "sem": CURRENT_SEMESTER_NO},
        "semesters": [
            {"key": s.get("key"), "label": s.get("label")}
            for s in get_semesters_index() if s.get("key")
        ],
    })


@app.route('/api/courses', methods=['GET'])
def get_courses():
    try:
        semester = (request.args.get('semester') or 'current').strip()

        if semester in ('', 'current'):
            return jsonify(_group_df_courses())

        if semester == 'all':
            # current offerings + one synthesized card per catalog course that
            # is not offered this semester (so the whole catalog is searchable)
            courses = _group_df_courses()
            seen = {c["code"] for c in courses}
            catalog = get_course_catalog()
            for code in sorted(catalog):
                if code in seen:
                    continue
                c = catalog[code]
                courses.append({
                    "code": code,
                    "name": c.get("title") or code,
                    "teachers": [],
                    "details": [{
                        "Course Code": code,
                        "Course Title & Session": c.get("title") or code,
                        "Offering Unit": " / ".join(c.get("offering_units") or []),
                        "Offering Programme": " / ".join(c.get("offering_programmes") or []),
                        "Units": c.get("units") or "",
                        "Curriculum Type": " / ".join(c.get("curriculum_types") or []),
                        "Elective Type": " / ".join(c.get("elective_types") or []),
                        "Teachers": "",
                        "Class Schedule": "",
                        "Hours": "",
                        "Classroom": "",
                        "Requirements": c.get("prereq_text_zh") or "",
                        "Remarks": "",
                    }],
                })
            return jsonify(courses)

        # whitelist against the baked index (no path injection)
        valid_keys = {s.get("key") for s in get_semesters_index()}
        if semester not in valid_keys:
            return jsonify({"error": "Unknown semester"}), 400
        return jsonify(get_semester_courses(semester))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/optimize', methods=['POST'])
def optimize():
    try:
        data = request.get_json(silent=True) or {}
        target_codes = data.get('codes', [])
        if not isinstance(target_codes, list):
            return jsonify({"error": "Course codes must be a list"}), 400
        if len(target_codes) > 100:
            return jsonify({"error": "Too many course codes (maximum 100)"}), 400
        start_time_str = data.get('startTime')
        end_time_str = data.get('endTime')
        
        time_range = None
        if start_time_str and end_time_str:
            try:
                def parse_min(t):
                    h, m = map(int, t.split(':'))
                    return h * 60 + m
                min_t = parse_min(start_time_str)
                max_t = parse_min(end_time_str)
                time_range = (min_t, max_t)
            except Exception:
                pass

        blocked_raw = data.get('blocked', [])
        blocked_slots = []
        DAY_MAP = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}
        
        for b in blocked_raw:
            try:
                day_str = b.get('day')
                s_str = b.get('start')
                e_str = b.get('end')
                
                if day_str in DAY_MAP and s_str and e_str:
                    day_idx = DAY_MAP[day_str]
                    def parse_min(t):
                        h, m = map(int, t.split(':'))
                        return h * 60 + m
                    s_min = parse_min(s_str)
                    e_min = parse_min(e_str)
                    blocked_slots.append((day_idx, s_min, e_min))
            except Exception:
                continue

        if not target_codes:
            return jsonify({"error": "No course codes provided"}), 400
            
        teacher_constraints = data.get('teachers', {})

        df = get_df()
        result = maximize_credits(
            df, 
            target_codes, 
            time_range=time_range, 
            blocked_slots=blocked_slots,
            teacher_constraints=teacher_constraints
        )
        
        if not result['solutions']:
            return jsonify({
                "found": False,
                "best_units": result['best_units'],
                "missing": result.get("missing", [])
            })

        sol = result['solutions'][0]
        
        # Format for frontend
        REV_DAY = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri", 5: "Sat", 6: "Sun"}
        
        formatted_courses = []
        for c in sol:
            schedules = []
            meeting_rooms = c.get('meeting_rooms') or []
            for i, (day_idx, start_min, end_min) in enumerate(c['meetings']):
                schedules.append({
                    "day": REV_DAY[day_idx],
                    "start": f"{start_min//60:02d}:{start_min%60:02d}",
                    "end": f"{end_min//60:02d}:{end_min%60:02d}",
                    # per-meeting classroom: a session can move rooms by day
                    "room": meeting_rooms[i] if i < len(meeting_rooms) else ""
                })
                
            formatted_courses.append({
                "code": c['course_code'],
                "name": c['title'],
                "teacher": c['teacher'],
                "session": c['session'],
                "units": c.get('units', 0),
                "room": c.get('room', ''),
                "schedules": schedules,
                "id": f"{c['course_code']}-{c['session']}"
            })
            
        return jsonify({
            "found": True,
            "courses": formatted_courses,
            "totalUnits": result['best_units']
        })
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    # Nginx runs on the same host and proxies to loopback. Binding publicly
    # would expose Werkzeug directly and bypass every edge/WAF rule.
    app.run(
        host=os.getenv('MAXCOURSE_BIND_HOST', '127.0.0.1'),
        port=int(os.getenv('PORT', '5000')),
    )
