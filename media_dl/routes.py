"""Flask blueprint exposing /api/media-dl/* endpoints."""

from __future__ import annotations

import logging
import sqlite3
import time
from urllib.parse import urlparse

import requests
from flask import Blueprint, Response, jsonify, request, session, stream_with_context

from . import extractor
from .analytics import DB_PATH, host_of, log_event, platform_of_host

log = logging.getLogger(__name__)

media_dl_bp = Blueprint("media_dl", __name__, url_prefix="/api/media-dl")


# Hosts we trust to proxy on the user's behalf. Whitelist guards the proxy from
# being used as an open relay.
_ALLOWED_PROXY_HOSTS = (
    "bilivideo.com",
    "akamaized.net",
    "bilibili.com",
    "bilivideo.cn",
    "hdslb.com",
    "douyinvod.com",
    "douyincdn.com",
    "douyinpic.com",
    "snssdk.com",
    "xhscdn.com",
    "xiaohongshu.com",
    "googlevideo.com",
    "youtube.com",
    "ytimg.com",
    "twimg.com",
    "tiktokcdn.com",
    "tiktokv.com",
)


def _host_allowed(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    return any(host == h or host.endswith("." + h) for h in _ALLOWED_PROXY_HOSTS)


def _visitor_id() -> str | None:
    return session.get("analytics_visitor_id")


def _user_id() -> int | None:
    return session.get("user_id")


@media_dl_bp.post("/resolve")
def resolve():
    payload = request.get_json(silent=True) or {}
    url = (payload.get("url") or "").strip()
    if not url:
        return jsonify({"error": "缺少 url 参数"}), 400

    host = host_of(url)
    platform = platform_of_host(host)

    started = time.monotonic()
    try:
        result = extractor.resolve(url)
    except extractor.UnsupportedURLError as exc:
        log_event(
            visitor_id=_visitor_id(), user_id=_user_id(),
            action="resolve", platform=platform, host=host,
            success=False, elapsed_ms=int((time.monotonic() - started) * 1000),
            error=str(exc),
        )
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        log.exception("media-dl resolve failed for %s", url)
        log_event(
            visitor_id=_visitor_id(), user_id=_user_id(),
            action="resolve", platform=platform, host=host,
            success=False, elapsed_ms=int((time.monotonic() - started) * 1000),
            error=str(exc),
        )
        return jsonify({"error": f"解析失败: {exc}"}), 502

    elapsed = int((time.monotonic() - started) * 1000)
    result["elapsed_ms"] = elapsed
    log_event(
        visitor_id=_visitor_id(), user_id=_user_id(),
        action="resolve",
        platform=result.get("platform") or platform,
        host=host_of(result.get("webpage_url") or url),
        success=True, elapsed_ms=elapsed,
    )
    return jsonify(result)


@media_dl_bp.get("/proxy")
def proxy():
    """Stream a remote resource through the server.

    Used for hosts that require a Referer header (e.g. Bilibili) so the browser's
    direct download would otherwise hit 403.
    """
    target = request.args.get("u", "").strip()
    referer = request.args.get("r", "").strip() or None
    filename = request.args.get("name", "").strip() or "download.bin"

    if not target:
        return jsonify({"error": "缺少 u 参数"}), 400
    if not target.startswith(("http://", "https://")):
        return jsonify({"error": "仅允许 http/https 链接"}), 400
    if not _host_allowed(target):
        return jsonify({"error": "目标域名不在允许列表中"}), 403

    target_host = host_of(target)
    target_platform = platform_of_host(target_host)
    started = time.monotonic()

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
        ),
    }
    if referer:
        headers["Referer"] = referer
    range_header = request.headers.get("Range")
    if range_header:
        headers["Range"] = range_header

    try:
        # (connect, read). The read timeout applies *between* chunks while
        # streaming, so it must be generous enough for slow CDN segments.
        upstream = requests.get(target, headers=headers, stream=True, timeout=(10, 120))
    except requests.RequestException as exc:
        log_event(
            visitor_id=_visitor_id(), user_id=_user_id(),
            action="proxy", platform=target_platform, host=target_host,
            success=False, elapsed_ms=int((time.monotonic() - started) * 1000),
            error=f"upstream request failed: {exc}",
        )
        return jsonify({"error": f"上游请求失败: {exc}"}), 502

    if upstream.status_code >= 400:
        body = upstream.content[:512]
        upstream.close()
        log_event(
            visitor_id=_visitor_id(), user_id=_user_id(),
            action="proxy", platform=target_platform, host=target_host,
            success=False, elapsed_ms=int((time.monotonic() - started) * 1000),
            error=f"upstream HTTP {upstream.status_code}",
        )
        return (
            jsonify({"error": f"上游返回 HTTP {upstream.status_code}", "snippet": body.decode("utf-8", "replace")}),
            502,
        )

    pass_through = ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges", "ETag", "Last-Modified")
    forwarded = {k: upstream.headers[k] for k in pass_through if k in upstream.headers}
    forwarded["Cache-Control"] = "no-store"
    forwarded["Content-Disposition"] = f'attachment; filename="{filename}"'

    visitor = _visitor_id()
    user = _user_id()

    def generate():
        sent = 0
        errored = False
        try:
            for chunk in upstream.iter_content(chunk_size=64 * 1024):
                if chunk:
                    sent += len(chunk)
                    yield chunk
        except Exception as exc:  # noqa: BLE001
            errored = True
            log.warning("proxy stream interrupted: %s", exc)
        finally:
            upstream.close()
            log_event(
                visitor_id=visitor, user_id=user,
                action="proxy", platform=target_platform, host=target_host,
                success=not errored, bytes_count=sent,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                error="stream interrupted" if errored else None,
            )

    status = upstream.status_code
    return Response(stream_with_context(generate()), status=status, headers=forwarded)


@media_dl_bp.get("/stats")
def stats():
    """Aggregate counts and proxy bandwidth over the last 30 days."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            c = conn.cursor()

            totals = c.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN action = 'resolve' THEN 1 ELSE 0 END) AS resolves,
                    SUM(CASE WHEN action = 'resolve' AND success = 1 THEN 1 ELSE 0 END) AS resolve_ok,
                    SUM(CASE WHEN action = 'proxy' THEN 1 ELSE 0 END) AS proxy_calls,
                    SUM(CASE WHEN action = 'proxy' AND success = 1 THEN 1 ELSE 0 END) AS proxy_ok,
                    COALESCE(SUM(CASE WHEN action = 'proxy' THEN bytes ELSE 0 END), 0) AS proxy_bytes
                FROM media_dl_events
                """
            ).fetchone()

            today = c.execute(
                """
                SELECT
                    SUM(CASE WHEN action = 'resolve' THEN 1 ELSE 0 END) AS resolves,
                    SUM(CASE WHEN action = 'proxy' THEN 1 ELSE 0 END) AS proxy_calls,
                    COALESCE(SUM(CASE WHEN action = 'proxy' THEN bytes ELSE 0 END), 0) AS proxy_bytes
                FROM media_dl_events
                WHERE date(created_at) = date('now')
                """
            ).fetchone()

            week = c.execute(
                """
                SELECT
                    SUM(CASE WHEN action = 'resolve' THEN 1 ELSE 0 END) AS resolves,
                    SUM(CASE WHEN action = 'proxy' THEN 1 ELSE 0 END) AS proxy_calls,
                    COALESCE(SUM(CASE WHEN action = 'proxy' THEN bytes ELSE 0 END), 0) AS proxy_bytes
                FROM media_dl_events
                WHERE created_at >= datetime('now', '-7 days')
                """
            ).fetchone()

            by_platform = c.execute(
                """
                SELECT
                    platform,
                    SUM(CASE WHEN action = 'resolve' THEN 1 ELSE 0 END) AS resolves,
                    SUM(CASE WHEN action = 'resolve' AND success = 1 THEN 1 ELSE 0 END) AS resolve_ok,
                    SUM(CASE WHEN action = 'proxy' THEN 1 ELSE 0 END) AS proxy_calls,
                    COALESCE(SUM(CASE WHEN action = 'proxy' THEN bytes ELSE 0 END), 0) AS proxy_bytes
                FROM media_dl_events
                WHERE created_at >= datetime('now', '-30 days')
                GROUP BY platform
                ORDER BY resolves DESC, proxy_bytes DESC
                """
            ).fetchall()

            daily = c.execute(
                """
                SELECT
                    date(created_at) AS day,
                    SUM(CASE WHEN action = 'resolve' THEN 1 ELSE 0 END) AS resolves,
                    SUM(CASE WHEN action = 'proxy' THEN 1 ELSE 0 END) AS proxy_calls,
                    COALESCE(SUM(CASE WHEN action = 'proxy' THEN bytes ELSE 0 END), 0) AS proxy_bytes
                FROM media_dl_events
                WHERE created_at >= datetime('now', '-30 days')
                GROUP BY day
                ORDER BY day ASC
                """
            ).fetchall()
    except Exception as exc:  # noqa: BLE001
        log.exception("media-dl stats failed")
        return jsonify({"error": f"统计读取失败: {exc}"}), 500

    return jsonify({
        "totals": dict(totals) if totals else {},
        "today": dict(today) if today else {},
        "last7d": dict(week) if week else {},
        "byPlatform": [dict(row) for row in by_platform],
        "daily": [dict(row) for row in daily],
    })
