"""Cross-app SSO bridge: accept / mint the parent-domain sso_token shared
with OmniChat (chat.bnbscheduler.top) and SlideCraft (ppt.bnbscheduler.top).

Tokens live in the shared auth DB (same one those apps use). In production
set SSO_COOKIE_DOMAIN=.bnbscheduler.top and SSO_COOKIE_SECURE=1 so one login
covers all three apps across subdomains.
"""
import hashlib
import os
import secrets
import sqlite3

SHARED_AUTH_DB = os.environ.get("SHARED_AUTH_DB", "/opt/shared/auth.db")
TOKEN_TTL_DAYS = 30
SIGNUP_CREDITS = 500
COOKIE_DOMAIN = os.environ.get("SSO_COOKIE_DOMAIN", "").strip() or None
COOKIE_SECURE = os.environ.get("SSO_COOKIE_SECURE", "").lower() in ("1", "true", "yes")


def _db():
    conn = sqlite3.connect(SHARED_AUTH_DB, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def shared_user_for_token(token):
    """Active shared-DB user for a live sso_token, else None. Never raises."""
    if not token:
        return None
    try:
        with _db() as db:
            row = db.execute(
                "SELECT u.* FROM tokens t JOIN users u ON u.id = t.user_id"
                " WHERE t.token = ? AND t.created_at >= datetime(\"now\", ?)",
                (token, "-%d days" % TOKEN_TTL_DAYS),
            ).fetchone()
        if row is not None and (row["status"] or "active") != "active":
            return None
        return row
    except sqlite3.Error:
        return None


def issue_shared_token(username, ispace=False):
    """Find-or-create the shared account for username and mint an sso token,
    mirroring OmniChat iSpace provisioning (random unusable password on
    create). Returns None for banned accounts or on DB errors.

    `ispace=True` marks the login as iSpace-verified (the caller MUST have
    checked iSpace credentials first). The shared `ispace` flag records each
    account's origin, and the token is refused when the login kind disagrees
    with it: an iSpace login will not bind onto a same-named local/password
    account (someone squatting a student id), and a local-password login will
    not seize a real iSpace student's shared account. On a conflict the
    MaxCourse session still stands; only the cross-app SSO token is withheld."""
    username = (username or "").strip()
    if not username:
        return None
    try:
        with _db() as db:
            row = db.execute(
                "SELECT id, status, ispace FROM users WHERE username = ?", (username,)
            ).fetchone()
            if row is None:
                salt = secrets.token_bytes(16)
                pw = hashlib.pbkdf2_hmac(
                    "sha256", secrets.token_urlsafe(24).encode(), salt, 200_000)
                # credits written explicitly (the live table's column DEFAULT
                # is stuck at its creation-time value), with a ledger row so
                # the OmniChat usage panel / margin report can replay it
                cur = db.execute(
                    "INSERT INTO users (username, pw_hash, salt, ispace, credits)"
                    " VALUES (?,?,?,?,?)",
                    (username, pw, salt, 1 if ispace else 0, SIGNUP_CREDITS))
                uid = cur.lastrowid
                db.execute(
                    "INSERT INTO credit_ledger (user_id, app, kind, delta,"
                    " balance_after, note) VALUES (?,?,?,?,?,?)",
                    (uid, "maxcourse", "grant", SIGNUP_CREDITS,
                     SIGNUP_CREDITS, "\u6ce8\u518c\u8d60\u9001"))
            elif (row["status"] or "active") != "active":
                return None
            elif ispace and not row["ispace"]:
                # 学号登录落到同名的非 iSpace（本地/密码）账号上 = 抢注，拒发令牌
                return None
            elif not ispace and row["ispace"]:
                # 本地密码登录不得接管真实 iSpace 学生的共享账号
                return None
            else:
                uid = row["id"]
            token = secrets.token_urlsafe(32)
            db.execute(
                "INSERT INTO tokens (token, user_id, created_at)"
                " VALUES (?,?,CURRENT_TIMESTAMP)", (token, uid))
        return token
    except sqlite3.Error:
        return None


def revoke_token(token):
    if not token:
        return
    try:
        with _db() as db:
            db.execute("DELETE FROM tokens WHERE token = ?", (token,))
    except sqlite3.Error:
        pass


def set_sso_cookie(resp, token):
    if not token:
        return
    resp.set_cookie(
        "sso_token", token, max_age=60 * 60 * 24 * TOKEN_TTL_DAYS,
        samesite="Lax", secure=COOKIE_SECURE, httponly=True,
        domain=COOKIE_DOMAIN, path="/")


def clear_sso_cookie(resp):
    resp.delete_cookie("sso_token", domain=COOKIE_DOMAIN, path="/")
