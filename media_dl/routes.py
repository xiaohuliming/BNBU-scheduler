"""Flask blueprint exposing /api/media-dl/* endpoints."""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from urllib.parse import quote, urlparse

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


# Cobalt-style chunked proxy: instead of holding one giant streaming GET open
# (where a single CDN hiccup at byte 200MB nukes the entire 300MB transfer),
# we issue successive small `Range:` requests so a disconnect costs at most
# one chunk.
_PROXY_CHUNK = 8 * 1024 * 1024              # 8 MB per upstream request
_PROXY_RETRIES_PER_CHUNK = 3                # transient errors → retry the same range
_PROXY_TIMEOUT = (10, 60)                   # (connect, read) — read is between chunks
_RANGE_RE = re.compile(r"bytes=(\d+)-(\d*)")
_CONTENT_RANGE_RE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", re.IGNORECASE)
_PROXY_PASSTHROUGH_HEADERS = ("Content-Type", "ETag", "Last-Modified")


def _content_disposition_header(filename: str) -> str:
    """Build an RFC 5987 Content-Disposition header safe for WSGI latin-1."""
    cleaned = re.sub(r'[\\/\r\n]+', '_', filename).strip() or 'download.bin'
    ext = ''
    if '.' in cleaned:
        candidate = cleaned.rsplit('.', 1)[1].lower()
        if re.fullmatch(r'[a-z0-9]{1,8}', candidate):
            ext = f'.{candidate}'
    fallback = f'download{ext or ".bin"}'
    encoded = quote(cleaned, safe='')
    return f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{encoded}'


def _fetch_range(url: str, base_headers: dict, start: int, end: int) -> requests.Response:
    """GET a byte range with bounded retries. Returns an unread streaming Response."""
    last_exc: Exception | None = None
    last_status: int | None = None
    last_body: str = ""
    headers = {**base_headers, "Range": f"bytes={start}-{end}"}
    for attempt in range(_PROXY_RETRIES_PER_CHUNK):
        try:
            r = requests.get(url, headers=headers, stream=True, timeout=_PROXY_TIMEOUT)
            if r.status_code in (200, 206):
                return r
            last_status = r.status_code
            try:
                last_body = r.content[:512].decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                last_body = ""
            r.close()
            last_exc = RuntimeError(f"upstream HTTP {r.status_code}")
        except requests.RequestException as exc:
            last_exc = exc
        # Linear backoff: 0.4s, 0.8s.
        time.sleep(0.4 * (attempt + 1))
    detail = f"HTTP {last_status}: {last_body[:200]}" if last_status else str(last_exc or "")
    raise RuntimeError(f"range fetch failed [{detail}]") from last_exc


@media_dl_bp.get("/proxy")
def proxy():
    """Stream a remote resource through the server in 8 MB Range chunks.

    Used for hosts that require Referer (e.g. Bilibili). Per-chunk retries mean
    a transient CDN error at byte 200 MB only loses 8 MB, not the whole download.
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

    base_headers: dict[str, str] = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
        ),
    }
    if referer:
        base_headers["Referer"] = referer

    # Honour any Range the browser sent. Most browsers send `bytes=0-` for downloads.
    user_range = request.headers.get("Range")
    user_start = 0
    user_end: int | None = None
    if user_range:
        m = _RANGE_RE.match(user_range)
        if m:
            user_start = int(m.group(1))
            user_end = int(m.group(2)) if m.group(2) else None

    visitor = _visitor_id()
    user = _user_id()

    def _log(success: bool, sent: int, error: str | None) -> None:
        log_event(
            visitor_id=visitor, user_id=user,
            action="proxy", platform=target_platform, host=target_host,
            success=success, bytes_count=sent,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            error=error,
        )

    # First range fetch: needed to discover total size from Content-Range and
    # to surface upstream auth / 4xx errors as a real HTTP error to the client
    # instead of dying mid-stream.
    first_end = user_start + _PROXY_CHUNK - 1
    if user_end is not None:
        first_end = min(first_end, user_end)
    try:
        first = _fetch_range(target, base_headers, user_start, first_end)
    except Exception as exc:  # noqa: BLE001
        _log(False, 0, f"first range failed: {exc}")
        # Return a downloadable .txt with the diagnosis so the user can see
        # *why* the download failed instead of just Chrome's generic error.
        body = (
            "下载未能开始。\n\n"
            f"目标 URL: {target}\n"
            f"Referer:  {referer or '(未设置)'}\n"
            f"上游错误: {exc}\n\n"
            "可能原因：\n"
            "  1. 解析结果已超过 2 小时（B站签名 URL 失效）→ 请回到工具页重新解析\n"
            "  2. 服务器 IP 被 CDN 风控 → 短时间内多次失败可换时段重试\n"
            "  3. 链接本身需要登录态（如付费内容）→ 当前仅支持公开视频\n"
        ).encode("utf-8")
        return Response(
            body,
            status=200,
            headers={
                "Content-Type": "text/plain; charset=utf-8",
                "Content-Disposition": _content_disposition_header("download_error.txt"),
                "Cache-Control": "no-store",
            },
        )

    total: int | None = None
    cr_match = _CONTENT_RANGE_RE.match(first.headers.get("Content-Range", ""))
    if cr_match and cr_match.group(3) != "*":
        total = int(cr_match.group(3))
    elif first.headers.get("Content-Length") and first.status_code == 200:
        # Server doesn't honour Range — treat the single response as the whole body.
        total = int(first.headers["Content-Length"])

    effective_end = user_end if user_end is not None else (total - 1 if total else None)

    forwarded: dict[str, str] = {
        "Cache-Control": "no-store",
        "Content-Disposition": _content_disposition_header(filename),
        "Accept-Ranges": "bytes",
    }
    for h in _PROXY_PASSTHROUGH_HEADERS:
        if h in first.headers:
            forwarded[h] = first.headers[h]
    if effective_end is not None:
        forwarded["Content-Length"] = str(effective_end - user_start + 1)
    if total is not None and effective_end is not None:
        forwarded["Content-Range"] = f"bytes {user_start}-{effective_end}/{total}"

    # Always 206 when we know the total; otherwise mirror the upstream status.
    status = 206 if (total is not None) else first.status_code

    def generate():
        sent = 0
        cursor = user_start
        cur: requests.Response | None = first
        failures = 0
        MAX_FAILURES = 5  # consecutive (refetch + iter_content) failures before giving up

        def _close(r: requests.Response | None) -> None:
            if r is None:
                return
            try:
                r.close()
            except Exception:  # noqa: BLE001
                pass

        try:
            while effective_end is None or cursor <= effective_end:
                # 1. Make sure we hold an open response to drain.
                if cur is None:
                    next_end = (
                        min(cursor + _PROXY_CHUNK - 1, effective_end)
                        if effective_end is not None
                        else cursor + _PROXY_CHUNK - 1
                    )
                    try:
                        cur = _fetch_range(target, base_headers, cursor, next_end)
                    except Exception as exc:  # noqa: BLE001
                        failures += 1
                        log.warning(
                            "[proxy] refetch failed at byte %s (%d/%d): %s",
                            cursor, failures, MAX_FAILURES, exc,
                        )
                        if failures >= MAX_FAILURES:
                            raise
                        time.sleep(0.5 * failures)
                        continue

                # 2. Drain the response. If iter_content blows up mid-chunk,
                #    keep `cursor` accurate and refetch from where we are.
                try:
                    for piece in cur.iter_content(chunk_size=64 * 1024):
                        if piece:
                            sent += len(piece)
                            cursor += len(piece)
                            yield piece
                    _close(cur)
                    cur = None
                    failures = 0  # full chunk drained
                except Exception as exc:  # noqa: BLE001
                    _close(cur)
                    cur = None
                    failures += 1
                    log.warning(
                        "[proxy] stream broke at byte %s (%d/%d): %s — refetching from cursor",
                        cursor, failures, MAX_FAILURES, exc,
                    )
                    if failures >= MAX_FAILURES:
                        raise
                    # Next loop iteration will refetch starting at `cursor`.

            _log(True, sent, None)
        except Exception as exc:  # noqa: BLE001
            log.warning("chunked proxy interrupted at byte %s/%s: %s", cursor, effective_end, exc)
            _log(False, sent, f"interrupted at byte {cursor}: {exc}")
        finally:
            _close(cur)

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
