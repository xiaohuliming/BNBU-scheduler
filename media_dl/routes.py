"""Flask blueprint exposing /api/media-dl/* endpoints."""

from __future__ import annotations

import logging
import json
import os
import re
import secrets
import shutil
import sqlite3
import time
from urllib.parse import quote, urlparse

from . import http as requests
from .http import UnsafeURLError, validate_url
from .transport import MediaDownloadError, headers_for
from flask import Blueprint, Response, jsonify, request, session, stream_with_context

from . import extractor
from .douyin import DouyinError
from .analytics import DB_PATH, host_of, log_event, platform_of_host


_BLOCKED_HOST_HINTS = {
    "x.com": "X (Twitter)",
    "api.x.com": "X (Twitter)",
    "twitter.com": "X (Twitter)",
    "youtube.com": "YouTube",
    "youtu.be": "YouTube",
    "googlevideo.com": "YouTube",
    "instagram.com": "Instagram",
    "facebook.com": "Facebook",
    "tiktok.com": "TikTok",
}


def _friendly_error(exc: Exception) -> str:
    """Translate cryptic network errors into actionable Chinese messages."""
    msg = str(exc)
    lowered = msg.lower()
    if isinstance(exc, (UnsafeURLError, DouyinError, MediaDownloadError)):
        return msg
    if 'fresh cookies' in lowered and 'douyin' in lowered:
        return '抖音暂时无法获取该视频，请重新复制完整分享链接后重试。若持续失败，需要更新服务器的抖音访问凭据。'
    if 'sign in' in lowered or 'login required' in lowered or 'not a bot' in lowered:
        return '源站要求登录或验证，当前无法下载该内容，请尝试其他公开视频。'
    if '403' in lowered or '410' in lowered:
        return '源站拒绝了下载请求，链接可能已失效。请重新解析后重试。'
    if "errno 101" in lowered or "network is unreachable" in lowered:
        for needle, label in _BLOCKED_HOST_HINTS.items():
            if needle in lowered:
                return (
                    f"{label} 在当前服务器无法直接访问（Network is unreachable）。"
                    "如果服务器有可用代理，请在启动 Flask 前设置 HTTPS_PROXY 环境变量后重启。"
                )
        return "上游服务器不可达（Network is unreachable）—— 该平台的源站可能在境外，需配置出站代理。"
    if "errno -2" in lowered or "name or service not known" in lowered or "nodename nor servname" in lowered:
        return "DNS 解析失败，请检查链接是否正确，或稍后重试。"
    if "timed out" in lowered or "timeout" in lowered:
        return "上游请求超时，请稍后再试。"
    if "ssl" in lowered and "verify" in lowered:
        return "上游 SSL 证书校验失败，可能是代理拦截或证书过期。"
    if '404' in lowered:
        return '媒体已删除或链接失效，请重新解析后重试。'
    if 'unsupported url' in lowered or 'no suitable extractor' in lowered:
        return '暂不支持该链接，请粘贴视频或图文详情页的完整分享链接。'
    return '暂时无法获取媒体，请检查分享链接或稍后重试。'

log = logging.getLogger(__name__)

media_dl_bp = Blueprint("media_dl", __name__, url_prefix="/api/media-dl")


# Hosts we trust to proxy on the user's behalf. Whitelist guards the proxy from
# being used as an open relay.
_ALLOWED_PROXY_HOSTS = (
    "bilivideo.com",
    "bilibili.com",
    "bilivideo.cn",
    "hdslb.com",
    "douyin.com",
    "iesdouyin.com",
    "douyinvod.com",
    "douyincdn.com",
    "douyinpic.com",
    "douyinstatic.com",
    "zjcdn.com",
    "snssdk.com",
    "xhscdn.com",
    "xiaohongshu.com",
    "googlevideo.com",
    "youtube.com",
    "ytimg.com",
    "twimg.com",
    "tiktokcdn.com",
    "tiktokcdn-us.com",
    "tiktokv.com",
    "sinaimg.cn",
    "weibocdn.com",
    "yximgs.com",
    "kwaicdn.com",
    "cdninstagram.com",
    "fbcdn.net",
    "vimeocdn.com",
)

# CDNs unreachable from a mainland server without an outbound proxy. When
# MAXCOURSE_PROXY / HTTPS_PROXY is set (same vars ytdlp.py honours for
# resolving), route /proxy fetches for these hosts through it too — otherwise
# resolve succeeds via the proxy but the actual download then fails.
_FOREIGN_CDN_HOSTS = (
    "googlevideo.com",
    "youtube.com",
    "ytimg.com",
    "twimg.com",
    "tiktokcdn.com",
    "tiktokcdn-us.com",
    "tiktokv.com",
    "cdninstagram.com",
    "fbcdn.net",
    "vimeocdn.com",
)


def _match_host(url: str, suffixes: tuple[str, ...]) -> bool:
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    return any(host == h or host.endswith("." + h) for h in suffixes)


def _host_allowed(url: str) -> bool:
    try:
        validate_url(url)
    except UnsafeURLError:
        return False
    if _match_host(url, _ALLOWED_PROXY_HOSTS):
        return True
    # Bilibili's overseas Akamai mirror is `upos-<region>-mirrorakam.akamaized.net`
    # (note the `-`, not a `.`, before `mirrorakam`). Match just that suffix
    # instead of allowlisting all of *.akamaized.net, which would turn /proxy
    # into an open relay for every unrelated Akamai customer.
    host = (urlparse(url).hostname or "").lower()
    return bool(re.fullmatch(r'upos-[a-z0-9-]+-mirrorakam\.akamaized\.net', host))


def _outbound_proxies_for(url: str) -> dict[str, str] | None:
    proxy = (
        os.environ.get("MAXCOURSE_PROXY")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
    )
    if not proxy or not _match_host(url, _FOREIGN_CDN_HOSTS):
        return None
    return {"http": proxy, "https": proxy}


def _visitor_id() -> str | None:
    return session.get("analytics_visitor_id")


def _user_id() -> int | None:
    return session.get("user_id")


@media_dl_bp.post("/resolve")
def resolve():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not isinstance(payload.get('url', ''), str):
        return jsonify({'error': '请提供字符串类型的 url 参数。'}), 400
    url = payload.get('url', '').strip()
    if not url:
        return jsonify({"error": "缺少 url 参数"}), 400
    if len(url) > 16384:
        return jsonify({'error': '分享链接或文案过长。'}), 400

    url = extractor.extract_url_from_text(url)
    if '://' not in url:
        url = 'https://' + url
    try:
        validate_url(url)
    except UnsafeURLError as exc:
        return jsonify({'error': str(exc)}), 400
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
        log.warning("media-dl resolve failed for host=%s: %s", host, type(exc).__name__)
        log_event(
            visitor_id=_visitor_id(), user_id=_user_id(),
            action="resolve", platform=platform, host=host,
            success=False, elapsed_ms=int((time.monotonic() - started) * 1000),
            error=str(exc),
        )
        return jsonify({"error": _friendly_error(exc)}), 502

    elapsed = int((time.monotonic() - started) * 1000)
    result["elapsed_ms"] = elapsed
    # Tell the UI whether the server can mux DASH split streams (画面+音频) into
    # one file — only true when ffmpeg is installed; otherwise it shows the
    # manual merge hint instead.
    result["can_merge"] = bool(_ffmpeg_path())
    log_event(
        visitor_id=_visitor_id(), user_id=_user_id(),
        action="resolve",
        platform=result.get("platform") or platform,
        host=host_of(result.get("webpage_url") or url),
        success=True, elapsed_ms=elapsed,
    )
    return jsonify(result)


def _download_error(message: str, status: int = 502):
    """Let a hidden download frame report failures without leaving the tool."""
    feedback = request.args.get('feedback', '')
    if re.fullmatch(r'[a-zA-Z0-9-]{16,80}', feedback):
        data = json.dumps({'type': 'media-dl-error', 'id': feedback, 'error': message}, ensure_ascii=True)
        data = data.replace('<', '\\u003c')
        html = '<!doctype html><meta charset="utf-8"><script>parent.postMessage(' + data + ',location.origin);</script>'
        return Response(html, status=status, headers={
            'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store',
            'Content-Security-Policy': "default-src 'none'; script-src 'unsafe-inline'; frame-ancestors 'self'",
            'X-Content-Type-Options': 'nosniff',
        })
    return jsonify({'error': message}), status


@media_dl_bp.post('/batch')
def prepare_batch():
    from . import batch
    payload = request.get_json(silent=True)
    items = payload.get('items') if isinstance(payload, dict) else None
    if not isinstance(items, list) or not 1 <= len(items) <= 50:
        return jsonify({'error': '请选择 1 至 50 个文件。'}), 400
    cleaned = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get('url'), str):
            return jsonify({'error': '下载列表格式无效。'}), 400
        if not _host_allowed(item['url']):
            return jsonify({'error': '选中的文件不支持打包下载。'}), 403
        filename, referer = item.get('filename', 'media.bin'), item.get('referer')
        if not isinstance(filename, str) or len(filename) > 500:
            return jsonify({'error': '文件名格式无效。'}), 400
        if referer is not None and (not isinstance(referer, str) or len(referer) > 16384
                                    or '\r' in referer or '\n' in referer):
            return jsonify({'error': '下载来源格式无效。'}), 400
        cleaned.append({'url': item['url'], 'filename': filename, 'referer': referer})
    owner = session.setdefault('media_dl_owner', secrets.token_urlsafe(24))
    try:
        token = batch.prepare(cleaned, owner)
    except MediaDownloadError as exc:
        return jsonify({'error': str(exc)}), 429
    return jsonify({'url': '/api/media-dl/batch/' + token})


@media_dl_bp.get('/batch/<token>')
def download_batch(token):
    from . import batch
    items = batch.take(token, session.get('media_dl_owner'))
    if not items:
        return _download_error('打包链接已失效，请重新选择下载。', 404)
    started = time.monotonic()
    visitor, user = _visitor_id(), _user_id()
    host = host_of(items[0]['url'])

    def record(success, sent, error=None):
        # One archive is one browser download; include its streamed bytes in
        # the existing proxy bandwidth metrics.
        log_event(visitor_id=visitor, user_id=user, action='proxy',
                  platform=platform_of_host(host), host=host, success=success,
                  bytes_count=sent, error=error,
                  elapsed_ms=int((time.monotonic() - started) * 1000))

    chunks = batch.archive(items, _outbound_proxies_for, _MERGE_UA)
    try:
        first = next(chunks)
    except Exception as exc:
        chunks.close()
        record(False, 0, type(exc).__name__)
        return _download_error(_friendly_error(exc))

    def generate():
        sent = 0
        try:
            sent += len(first)
            yield first
            for chunk in chunks:
                sent += len(chunk)
                yield chunk
            record(True, sent)
        except BaseException as exc:
            record(False, sent, type(exc).__name__)
            raise
        finally:
            chunks.close()

    response = Response(stream_with_context(generate()), headers={
        'Content-Type': 'application/zip', 'Cache-Control': 'no-store',
        'Content-Disposition': _content_disposition_header('MAXCOURSE-素材.zip'),
    })
    response.call_on_close(chunks.close)
    return response


# Cobalt-style chunked proxy: instead of holding one giant streaming GET open
# (where a single CDN hiccup at byte 200MB nukes the entire 300MB transfer),
# we issue successive small `Range:` requests so a disconnect costs at most
# one chunk.
_PROXY_CHUNK = 8 * 1024 * 1024              # 8 MB per upstream request
_PROXY_RETRIES_PER_CHUNK = 3                # transient errors → retry the same range
_PROXY_TIMEOUT = (10, 60)                   # (connect, read) — read is between chunks
_CONTENT_RANGE_RE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", re.IGNORECASE)
# Only forward Content-Type. ETag/Last-Modified are deliberately dropped: with
# them a browser may attempt a byte-range resume, which we can't answer (see the
# Range-handling note in proxy()).
_PROXY_PASSTHROUGH_HEADERS = ("Content-Type",)
_PROXY_MAX_FAILURES = 5                      # consecutive chunk failures before aborting


class _RangeNotSatisfiable(Exception):
    """Upstream answered 416 — the requested range is past EOF (stream done)."""


class _UpstreamClientError(Exception):
    """Upstream returned a permanent 4xx — retrying is pointless."""


def _content_range_bounds(resp: requests.Response) -> tuple[int | None, int | None]:
    """Parse (start, end) inclusive from a response's Content-Range, or (None, None)."""
    m = _CONTENT_RANGE_RE.match(resp.headers.get("Content-Range", ""))
    if not m:
        return None, None
    return int(m.group(1)), int(m.group(2))


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
    """GET a byte range with bounded retries. Returns an unread streaming Response.

    Raises `_RangeNotSatisfiable` on a 416 (past EOF) and `_UpstreamClientError`
    on a permanent 4xx (except 429), both without burning retries — those are
    deterministic and should fail fast rather than sleeping through 3 attempts.
    """
    last_exc: Exception | None = None
    last_status: int | None = None
    last_body: str = ""
    headers = {**base_headers, "Range": f"bytes={start}-{end}"}
    proxies = _outbound_proxies_for(url)
    for attempt in range(_PROXY_RETRIES_PER_CHUNK):
        try:
            r = requests.get(
                url, headers=headers, stream=True, timeout=_PROXY_TIMEOUT, proxies=proxies,
            )
            if r.status_code in (200, 206):
                return r
            status = r.status_code
            try:
                last_body = next(r.iter_content(512), b'').decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                last_body = ""
            r.close()
            if status == 416:
                raise _RangeNotSatisfiable(f"HTTP 416 at bytes={start}-{end}")
            if 400 <= status < 500 and status != 429:
                raise _UpstreamClientError(f"upstream HTTP {status}: {last_body[:200]}")
            last_status = status
            last_exc = RuntimeError(f"upstream HTTP {status}")
        except (_RangeNotSatisfiable, _UpstreamClientError):
            raise
        except requests.RequestException as exc:
            last_exc = exc
        # Linear backoff: 0.4s, 0.8s — but never after the final attempt.
        if attempt < _PROXY_RETRIES_PER_CHUNK - 1:
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
        return _download_error('缺少 u 参数', 400)
    if not target.startswith(("http://", "https://")):
        return _download_error('仅允许 http/https 链接', 400)
    if not _host_allowed(target):
        return _download_error('目标域名不在允许列表中', 403)

    target_host = host_of(target)
    target_platform = platform_of_host(target_host)
    started = time.monotonic()

    base_headers: dict[str, str] = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
        ),
        # Pin identity so our per-chunk byte arithmetic operates on the raw
        # stream. If an upstream ever gzipped, iter_content would yield decoded
        # bytes while `cursor` indexes the encoded stream — desyncing the Range
        # offsets and corrupting the reassembly.
        "Accept-Encoding": "identity",
    }
    if referer:
        base_headers["Referer"] = referer
    base_headers.update(headers_for(target))
    base_headers['Accept-Encoding'] = 'identity'

    # Deliberately ignore any Range the browser sent and always stream the
    # full body with a 200. We can't answer a mid-file Range correctly: a 206
    # needs an exact Content-Range, but our retry loop may deliver fewer bytes
    # (see the Content-Length note below). Worse, the old behaviour answered
    # `bytes=N-` with a 200 whose body *started at byte N* — a resuming
    # browser treats a 200 as the whole file and saves a truncated download.
    # `Accept-Ranges: none` below tells browsers not to try resuming at all.
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
    try:
        first = _fetch_range(target, base_headers, 0, _PROXY_CHUNK - 1)
    except Exception as exc:  # noqa: BLE001
        _log(False, 0, f"first range failed: {exc}")
        return _download_error(_friendly_error(exc), 400 if isinstance(exc, UnsafeURLError) else 502)

    first_start, first_end_incl = _content_range_bounds(first)
    if first.status_code == 206 and (first_start != 0 or first_end_incl is None):
        first.close()
        _log(False, 0, 'invalid initial content range')
        return _download_error('源站返回的文件范围不完整，请重新解析后重试。')
    server_honors_ranges = first.status_code == 206 and first_start == 0

    total: int | None = None
    if first_end_incl is not None:
        m = _CONTENT_RANGE_RE.match(first.headers.get("Content-Range", ""))
        if m and m.group(3) != "*":
            total = int(m.group(3))
    if total is None and first.headers.get("Content-Length") and first.status_code == 200:
        # Server doesn't honour Range — treat the single response as the whole body.
        total = int(first.headers["Content-Length"])

    forwarded: dict[str, str] = {
        "Cache-Control": "no-store",
        "Accept-Ranges": "none",
        "Content-Disposition": _content_disposition_header(filename),
    }
    for h in _PROXY_PASSTHROUGH_HEADERS:
        if h in first.headers:
            forwarded[h] = first.headers[h]

    # Deliberately omit Content-Length / Content-Range. Our chunked retry loop
    # may yield slightly fewer bytes than `total` if a chunk permanently fails
    # — Safari is lenient and saves whatever it got, but Chrome strictly
    # enforces Content-Length and shows "无法从网站上提取文件" on any mismatch.
    # By skipping Content-Length, Flask falls back to chunked transfer-encoding.
    # A mid-stream failure re-raises out of the generator instead of returning
    # cleanly, so the socket aborts and the browser flags the download as failed
    # rather than saving a truncated file it believes is complete.

    def _close(r: requests.Response | None) -> None:
        if r is None:
            return
        try:
            r.close()
        except Exception:  # noqa: BLE001
            pass

    def generate():
        sent = 0
        cursor = 0
        cur: requests.Response | None = first
        try:
            # --- Upstream ignores Range: single full-body response, no resume. ---
            if not server_honors_ranges:
                for piece in first.iter_content(chunk_size=64 * 1024):
                    if piece:
                        sent += len(piece)
                        yield piece
                _close(cur)
                cur = None
                _log(True, sent, None)
                return

            # --- Range-capable upstream: fetch in chunks with per-chunk retry. ---
            chunk_end = first_end_incl          # inclusive end of the held response
            failures = 0
            while total is None or cursor <= total - 1:
                if cur is None:
                    next_end = cursor + _PROXY_CHUNK - 1
                    if total is not None:
                        next_end = min(next_end, total - 1)
                    try:
                        cur = _fetch_range(target, base_headers, cursor, next_end)
                    except _RangeNotSatisfiable:
                        if total is not None and cursor < total:
                            raise RuntimeError('upstream ended before the declared file size')
                        break  # past EOF (unknown-total case) → clean end
                    except _UpstreamClientError:
                        raise  # permanent (e.g. expired signed URL) → abort
                    except Exception as exc:  # noqa: BLE001
                        failures += 1
                        log.warning("[proxy] refetch failed at byte %s (%d/%d): %s",
                                    cursor, failures, _PROXY_MAX_FAILURES, exc)
                        if failures >= _PROXY_MAX_FAILURES:
                            raise
                        time.sleep(0.5 * failures)
                        continue
                    # A refetch (cursor>0) MUST resume exactly at cursor. If the
                    # upstream ignored Range and returned a full 200, appending it
                    # would duplicate bytes 0..cursor — abort instead.
                    r_start, r_end = _content_range_bounds(cur)
                    r_status = cur.status_code
                    if r_status != 206 or r_start != cursor:
                        _close(cur)
                        cur = None
                        failures += 1
                        log.warning("[proxy] refetch did not resume at %s (status=%s start=%s)",
                                    cursor, r_status, r_start)
                        if failures >= _PROXY_MAX_FAILURES:
                            raise RuntimeError("upstream stopped honouring Range on refetch")
                        time.sleep(0.5 * failures)
                        continue
                    chunk_end = r_end

                chunk_start = cursor
                try:
                    for piece in cur.iter_content(chunk_size=64 * 1024):
                        if piece:
                            n = len(piece)
                            sent += n
                            cursor += n
                            yield piece
                    _close(cur)
                    cur = None
                    if cursor == chunk_start:
                        raise RuntimeError('upstream returned an empty range before EOF')
                    failures = 0
                except Exception as exc:  # noqa: BLE001
                    _close(cur)
                    cur = None
                    failures += 1
                    log.warning("[proxy] stream broke at byte %s (%d/%d): %s — refetching",
                                cursor, failures, _PROXY_MAX_FAILURES, exc)
                    if failures >= _PROXY_MAX_FAILURES:
                        raise
                    continue

                # Unknown totals finish only when the next range returns 416.
                # A body shorter than Content-Range promised must be resumed,
                # never treated as a successfully completed file.

            _log(True, sent, None)
        except GeneratorExit:
            # Browser cancelled the download. Log the partial transfer (the bytes
            # were genuinely served) so /stats doesn't undercount, then re-raise.
            _log(False, sent, "client disconnected")
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("chunked proxy aborted at byte %s (total=%s): %s", cursor, total, exc)
            _log(False, sent, f"aborted at byte {cursor}: {exc}")
            raise  # abort the socket so the browser marks the download failed
        finally:
            _close(cur)

    response = Response(stream_with_context(generate()), status=200, headers=forwarded)
    response.call_on_close(lambda: _close(first))
    return response


_ffmpeg_cached: str | None = None


def _ffmpeg_path() -> str:
    """Path to ffmpeg, or '' if not installed. Cached (install state is fixed
    for the process lifetime)."""
    global _ffmpeg_cached
    if _ffmpeg_cached is None:
        _ffmpeg_cached = shutil.which("ffmpeg") or ""
    return _ffmpeg_cached


_MERGE_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
)


@media_dl_bp.get("/merge")
def merge():
    """Remux a separate video + audio stream into one MP4 via `ffmpeg -c copy`.

    For Bilibili/YouTube high-quality DASH, the video and audio come as two
    files; this streams a single muxed MP4 back so the user gets one playable
    file with sound. `-c copy` means no re-encode (cheap CPU), and
    `frag_keyframe+empty_moov` makes the output streamable without seeking.
    Both source hosts must pass the same allowlist as /proxy.
    """
    video = request.args.get("v", "").strip()
    audio = request.args.get("a", "").strip()
    referer = request.args.get("r", "").strip() or None
    filename = request.args.get("name", "").strip() or "merged.mp4"

    # Strip CR/LF: referer is injected into ffmpeg's `-headers` value, where a
    # newline would let a crafted `r` param append extra outbound HTTP headers.
    if referer:
        referer = re.sub(r"[\r\n]+", "", referer)

    if not video or not audio:
        return _download_error('缺少 v 或 a 参数', 400)
    for u in (video, audio):
        if not u.startswith(("http://", "https://")):
            return _download_error('仅允许 http/https 链接', 400)
        if not _host_allowed(u):
            return _download_error('目标域名不在允许列表中', 403)

    ffmpeg = _ffmpeg_path()
    if not ffmpeg:
        return _download_error('服务器未安装 ffmpeg，无法在线合并。请分别下载画面与音频后在本地合并。', 501)

    from .mux import MergeBusyError, merge_chunks

    started = time.monotonic()
    visitor, user = _visitor_id(), _user_id()
    target_host = host_of(video)
    chunks = merge_chunks(ffmpeg, video, audio, referer=referer,
                          proxies_for=_outbound_proxies_for, user_agent=_MERGE_UA)

    def record(success, sent, error=None):
        log_event(visitor_id=visitor, user_id=user, action='merge',
                  platform=platform_of_host(target_host), host=target_host,
                  success=success, bytes_count=sent,
                  elapsed_ms=int((time.monotonic() - started) * 1000), error=error)

    try:
        first = next(chunks)
    except Exception as exc:
        chunks.close()
        record(False, 0, type(exc).__name__)
        status = 429 if isinstance(exc, MergeBusyError) else 502
        return _download_error(_friendly_error(exc), status)

    def generate():
        sent = 0
        try:
            sent += len(first)
            yield first
            for chunk in chunks:
                sent += len(chunk)
                yield chunk
            record(True, sent)
        except BaseException as exc:
            record(False, sent, type(exc).__name__)
            raise
        finally:
            chunks.close()

    response = Response(stream_with_context(generate()), status=200, headers={
        'Cache-Control': 'no-store', 'Accept-Ranges': 'none',
        'Content-Type': 'video/mp4',
        'Content-Disposition': _content_disposition_header(filename),
    })
    response.call_on_close(chunks.close)
    return response


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
                WHERE date(created_at, '+8 hours') = date('now', '+8 hours')
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
                    date(created_at, '+8 hours') AS day,
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
