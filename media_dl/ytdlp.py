"""yt-dlp wrapper. Handles YouTube, Bilibili, Twitter/X, TikTok, Douyin, etc."""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
import threading
import time
from typing import Any
from urllib.parse import urlparse

from . import http as requests
from .transport import remember_headers

try:
    import yt_dlp  # type: ignore
except ImportError:  # pragma: no cover - dep is required at runtime
    yt_dlp = None


log = logging.getLogger(__name__)

_BILI_HOST_RE = re.compile(r"bilibili\.com|b23\.tv")
_DOUYIN_HOST_RE = re.compile(r"douyin\.com")
# Overseas platforms: the server can only reach them through an outbound proxy,
# and browsers ignore `<a download>` on cross-origin CDN URLs — so their items
# must be routed through /api/media-dl/proxy (server-side fetch, same-origin
# Content-Disposition) whenever we can reach them at all.
_FOREIGN_HOST_RE = re.compile(
    r"youtube\.com|youtu\.be|googlevideo\.com|"
    r"twitter\.com|x\.com|twimg\.com|"
    r"tiktok\.com|instagram\.com|facebook\.com|vimeo\.com"
)
_WEIBO_HOST_RE = re.compile(r"weibo\.com|weibocdn\.com|sinaimg\.cn")
_KUAISHOU_HOST_RE = re.compile(r"kuaishou\.com|kwaicdn\.com|yximgs\.com")


def _server_proxy() -> str | None:
    return (
        os.environ.get("MAXCOURSE_PROXY")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
    )

_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
)

# Bilibili rejects requests without `buvid3` (and friends) with HTTP 412.
# We bootstrap a Netscape-format cookie file once, then point yt-dlp at it.
_BILI_COOKIE_TTL = 30 * 60
_bili_cookie_cache: dict[str, Any] = {"path": "", "expires": 0.0, "cookies": None}
_bili_cookie_lock = threading.Lock()


def _write_netscape_cookies(path: str, cookies: dict[str, str]) -> None:
    """Write cookies to a Netscape-format cookies file that yt-dlp can read."""
    lines = ["# Netscape HTTP Cookie File\n"]
    expiry = int(time.time()) + _BILI_COOKIE_TTL
    for name, value in cookies.items():
        if not value:
            continue
        # domain  flag  path  secure  expiration  name  value
        lines.append(f".bilibili.com\tTRUE\t/\tFALSE\t{expiry}\t{name}\t{value}\n")
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)


def _harvest_bili_cookies() -> dict[str, str]:
    """Hit Bilibili's public endpoints and return a dict of cookie name → value."""
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": _BROWSER_UA,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": "https://www.bilibili.com",
    })

    # Step 1: homepage Set-Cookie sets some of buvid3/b_nut/_uuid (sometimes empty in CN IDC).
    try:
        sess.get("https://www.bilibili.com/", timeout=8)
    except requests.RequestException as exc:
        log.warning("bili homepage GET failed: %s", exc)

    cookies: dict[str, str] = {c.name: c.value for c in sess.cookies if c.value}

    # Step 2: SPI fingerprint endpoint returns the canonical buvid3/buvid4 values
    # in the JSON body, regardless of Set-Cookie. This is the path that actually
    # works on cloud servers where the homepage refuses to set cookies.
    try:
        spi = sess.get("https://api.bilibili.com/x/frontend/finger/spi", timeout=8).json()
        data = spi.get("data") or {}
        if data.get("b_3"):
            cookies["buvid3"] = data["b_3"]
        if data.get("b_4"):
            cookies["buvid4"] = data["b_4"]
    except Exception as exc:  # noqa: BLE001
        log.warning("bili SPI fetch failed: %s", exc)

    # Step 3: ExClimbWuzhiQuanZhi reportedly stabilises buvid usage; harmless if it 404s.
    try:
        sess.post(
            "https://api.bilibili.com/x/internal/gaia-gateway/ExClimbWuzhi",
            json={"payload": "{\"3064\":1}"},
            timeout=6,
        )
        for c in sess.cookies:
            if c.value and c.name not in cookies:
                cookies[c.name] = c.value
    except requests.RequestException:
        pass

    # Optional operator-provided login cookie. With a valid SESSDATA the playurl
    # API stops downgrading anonymous requests, so qn=80/112 (1080p) becomes
    # reachable. Mirrors the MAXCOURSE_PROXY opt-in knob — unset by default.
    sessdata = os.environ.get("BILIBILI_SESSDATA")
    if sessdata:
        cookies["SESSDATA"] = sessdata

    return cookies


def bili_cookie_dict() -> dict[str, str]:
    """Return the cached harvested Bilibili cookie dict (TTL + lock guarded).

    Shared by the native extractor (bilibili.py) and the yt-dlp cookie file so a
    single resolve doesn't fire the 3-request harvest more than once per TTL.
    """
    now = time.time()
    cached = _bili_cookie_cache.get("cookies")
    if cached is not None and now < _bili_cookie_cache.get("expires", 0):
        return cached
    with _bili_cookie_lock:
        now = time.time()
        cached = _bili_cookie_cache.get("cookies")
        if cached is not None and now < _bili_cookie_cache.get("expires", 0):
            return cached
        cookies = _harvest_bili_cookies()
        usable = bool(cookies.get("buvid3") or cookies.get("SESSDATA"))
        if not usable:
            # Don't let a failed harvest (blocked IP, transient SPI error) stick
            # for the full TTL — a bad jar 412s every request until it expires.
            # Cache briefly so we retry soon without hammering B站's endpoints.
            log.warning("bilibili cookie harvest produced no buvid3/SESSDATA — retrying soon")
        _bili_cookie_cache["cookies"] = cookies
        _bili_cookie_cache["expires"] = time.time() + (_BILI_COOKIE_TTL if usable else 60)
        log.info("bilibili cookies refreshed (%d entries, usable=%s)", len(cookies), usable)
        return cookies


def flush_bili_cookies() -> None:
    """Invalidate the cached cookies + file so the next access re-harvests."""
    with _bili_cookie_lock:
        _bili_cookie_cache["cookies"] = None
        _bili_cookie_cache["expires"] = 0.0


def _bili_cookie_file() -> str:
    """Return the path to a Netscape cookies file with fresh Bilibili cookies."""
    cookies = bili_cookie_dict()
    cached_path = _bili_cookie_cache.get("path", "")
    # Freshness is keyed to the identity of the cookie dict, not the shared TTL:
    # bili_cookie_dict() hands back a NEW dict object on each re-harvest, so we
    # rewrite the file exactly when the cookies actually changed (and never let
    # the on-disk jar silently lag a refreshed dict).
    if (cached_path and os.path.exists(cached_path)
            and _bili_cookie_cache.get("file_for") is cookies):
        return cached_path
    try:
        tmpdir = tempfile.gettempdir()
        path = os.path.join(tmpdir, "maxcourse_bili_cookies.txt")
        # Write to a temp file and atomically replace, so a concurrent yt-dlp
        # read never sees a half-truncated cookie jar (→ spurious 412).
        fd, tmp_path = tempfile.mkstemp(dir=tmpdir, prefix=".bili_cookies_", suffix=".txt")
        os.close(fd)
        _write_netscape_cookies(tmp_path, cookies)
        os.replace(tmp_path, path)
        with _bili_cookie_lock:
            _bili_cookie_cache["path"] = path
            _bili_cookie_cache["file_for"] = cookies
        return path
    except Exception as exc:  # noqa: BLE001
        log.warning("bilibili cookie bootstrap failed: %s", exc)
        return ""


def _safe_filename(title: str, ext: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", title or "video").strip()
    cleaned = cleaned[:80] or "video"
    return f"{cleaned}.{ext}"


def _is_directly_downloadable(f: dict) -> bool:
    """True only for a single-file http(s) URL the browser/proxy can GET as-is.

    Excludes HLS/DASH manifest formats: yt-dlp reports them with ext='mp4' but a
    `.m3u8`/`.mpd` playlist URL (protocol m3u8_native / http_dash_segments) or a
    `fragments` list — handing one to the user yields a tiny unplayable text file.
    """
    if not f.get("url"):
        return False
    if f.get("fragments"):
        return False
    proto = (f.get("protocol") or "").lower()
    return proto in ("https", "http", "")


def _pick_best_combined(formats: list[dict]) -> dict | None:
    """Pick the best progressive (audio+video) MP4 we can get without ffmpeg."""
    candidates = [
        f
        for f in formats
        if f.get("vcodec") not in (None, "none")
        and f.get("acodec") not in (None, "none")
        and (f.get("ext") in ("mp4", "webm"))
        and _is_directly_downloadable(f)
    ]
    if not candidates:
        return None

    def key(f: dict) -> tuple:
        height = f.get("height") or 0
        is_mp4 = f.get("ext") == "mp4"
        tbr = f.get("tbr") or 0
        return (height, is_mp4, tbr)

    return max(candidates, key=key)


def _pick_best_video_only(formats: list[dict]) -> dict | None:
    candidates = [
        f
        for f in formats
        if f.get("vcodec") not in (None, "none")
        and f.get("acodec") in (None, "none")
        and _is_directly_downloadable(f)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda f: (f.get("height") or 0, f.get("tbr") or 0))


def _pick_best_audio_only(formats: list[dict]) -> dict | None:
    candidates = [
        f
        for f in formats
        if f.get("acodec") not in (None, "none")
        and f.get("vcodec") in (None, "none")
        and _is_directly_downloadable(f)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda f: f.get("abr") or 0)


def _quality_label(fmt: dict) -> str:
    parts = []
    if fmt.get("height"):
        parts.append(f"{fmt['height']}p")
    if fmt.get("fps") and fmt["fps"] >= 60:
        parts.append(f"{int(fmt['fps'])}fps")
    if fmt.get("ext"):
        parts.append(fmt["ext"].upper())
    if fmt.get("tbr"):
        parts.append(f"{int(fmt['tbr'])}kbps")
    return " · ".join(parts) or "原始"


def _needs_proxy(url: str, host: str) -> tuple[bool, str | None]:
    """Whether an item must be routed through /api/media-dl/proxy, and its Referer.

    Two independent reasons to proxy: (1) the CDN needs a Referer the browser
    can't send (bilibili/douyin/weibo hotlink checks); (2) the host is only
    reachable from the server via an outbound proxy and/or the cross-origin
    `<a download>` limitation would otherwise open the media in a tab instead of
    saving it (all overseas platforms).
    """
    if _BILI_HOST_RE.search(host):
        return True, "https://www.bilibili.com"
    if _DOUYIN_HOST_RE.search(host):
        return True, "https://www.douyin.com"
    if _WEIBO_HOST_RE.search(host):
        return True, "https://weibo.com"
    if _KUAISHOU_HOST_RE.search(host):
        return True, "https://www.kuaishou.com"
    if _FOREIGN_HOST_RE.search(host):
        return True, None
    return False, None


def _platform_of(host: str) -> str:
    if "youtube.com" in host or "youtu.be" in host:
        return "youtube"
    if "bilibili.com" in host or "b23.tv" in host:
        return "bilibili"
    if "douyin.com" in host:
        return "douyin"
    if "tiktok.com" in host:
        return "tiktok"
    if "twitter.com" in host or "x.com" in host:
        return "twitter"
    if "weibo" in host or "sinaimg" in host:
        return "weibo"
    if "kuaishou" in host or "kwai" in host or "yximgs" in host:
        return "kuaishou"
    if "instagram" in host:
        return "instagram"
    return host.split(".")[0] or "unknown"


def _extract_info(url: str) -> dict:
    if yt_dlp is None:
        raise RuntimeError("yt-dlp 未安装。请在服务器虚拟环境中执行 pip install yt-dlp。")

    http_headers: dict[str, str] = {
        "User-Agent": _BROWSER_UA,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }

    is_bili = bool(_BILI_HOST_RE.search(url))
    cookiefile: str | None = None
    if is_bili:
        http_headers["Referer"] = "https://www.bilibili.com"
        cookiefile = _bili_cookie_file() or None

    runtimes = {name: {'path': path} for name in ('deno', 'node')
                if (path := shutil.which(name))}
    media_node = os.environ.get('MEDIA_DL_NODE') or '/opt/maxcourse-media/node/bin/node'
    if os.path.isfile(media_node) and os.access(media_node, os.X_OK):
        runtimes['node'] = {'path': media_node}

    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "extract_flat": False,
        "http_headers": http_headers,
        "socket_timeout": 15,
        "retries": 2,
        "cachedir": False,
        "js_runtimes": runtimes,
    }
    if cookiefile:
        opts["cookiefile"] = cookiefile

    # Outbound proxy for platforms unreachable from CN servers (X/Twitter,
    # YouTube, Instagram). Set MAXCOURSE_PROXY (preferred) or HTTPS_PROXY in
    # the environment, e.g. `http://127.0.0.1:7890` or `socks5://...`.
    proxy = (
        os.environ.get("MAXCOURSE_PROXY")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
    )
    opts["proxy"] = proxy or ''

    cookie_path = os.environ.get('MEDIA_DL_COOKIE_FILE', '').strip()
    if cookie_path and not is_bili:
        opts['cookiefile'] = cookie_path

    def _run() -> dict[str, Any]:
        from yt_dlp.networking._requests import RequestsRH

        class PublicRequestsRH(RequestsRH):
            def _create_instance(self, cookiejar, legacy_ssl_support=None):
                session = requests.Session()
                session.headers.clear()
                session.cookies = cookiejar
                return session

        with yt_dlp.YoutubeDL(opts) as ydl:
            # No unguarded urllib/curl fallback may bypass IP pinning.
            ydl._request_director.close()
            ydl._request_director = ydl.build_request_director([PublicRequestsRH])
            return ydl.extract_info(url, download=False)

    try:
        info: dict[str, Any] = _run()
    except yt_dlp.utils.DownloadError as exc:
        msg = str(exc)
        if is_bili and "412" in msg:
            log.info("bilibili 412 — flushing cookie cache and retrying once")
            flush_bili_cookies()
            cookiefile = _bili_cookie_file() or None
            if cookiefile:
                opts["cookiefile"] = cookiefile
            info = _run()
        else:
            raise

    return info


def extract(url: str) -> dict:
    info = _extract_info(url)
    if info.get("_type") == "playlist" and info.get("entries"):
        info = next((e for e in info["entries"] if e), info)

    formats = info.get("formats") or []
    title = info.get("title") or "video"
    webpage_url = info.get("webpage_url") or url
    needs_proxy_flag, referer = _needs_proxy(webpage_url, webpage_url)

    def _item(kind: str, fmt: dict, ext: str, label: str, name: str,
              needs_merge: bool = False) -> dict:
        remember_headers(fmt['url'], {**(info.get('http_headers') or {}), **(fmt.get('http_headers') or {})})
        return {
            "kind": kind,
            "url": fmt["url"],
            "ext": ext,
            "width": fmt.get("width"),
            "height": fmt.get("height"),
            "filesize": fmt.get("filesize") or fmt.get("filesize_approx"),
            "quality_label": label,
            "needs_proxy": needs_proxy_flag,
            "referer": referer if needs_proxy_flag else None,
            "needs_merge": needs_merge,
            "filename": _safe_filename(name, ext),
        }

    items: list[dict] = []

    combined = _pick_best_combined(formats)
    video_only = _pick_best_video_only(formats)
    audio_only = _pick_best_audio_only(formats)

    # 1. Convenience: best progressive stream (audio+video in one file).
    if combined:
        ext = combined.get("ext") or "mp4"
        items.append(_item("video", combined, ext, _quality_label(combined), title))

    # 2. Higher-quality split streams (需自行合并). Offer when there is no
    #    progressive at all, or when the best video-only is meaningfully
    #    higher-res than the progressive — YouTube caps progressive at 360p but
    #    exposes 1080p+ as video-only, so the convenience MP4 alone hides it.
    combined_h = (combined or {}).get("height") or 0
    vo_h = (video_only or {}).get("height") or 0
    offer_split = bool(video_only and audio_only) and (not combined or vo_h > combined_h)

    if offer_split:
        v_ext = video_only.get("ext") or "mp4"
        a_ext = audio_only.get("ext") or "m4a"
        # Split A/V: mark needs_merge so the UI shows the ffmpeg/merge banner —
        # otherwise a user grabs "1080p · 仅画面" and gets a silent video.
        items.append(_item("video", video_only, v_ext,
                           _quality_label(video_only) + " · 仅画面", title + "-video",
                           needs_merge=True))
        items.append(_item("audio", audio_only, a_ext,
                           _quality_label(audio_only) + " · 仅音频", title + "-audio",
                           needs_merge=True))
    elif audio_only:
        # Progressive already covers video; still expose a standalone audio grab.
        a_ext = audio_only.get("ext") or "m4a"
        items.append(_item("audio", audio_only, a_ext,
                           _quality_label(audio_only) + " · 仅音频", title + "-audio"))

    if not items and info.get("url") and _is_directly_downloadable(info):
        ext = info.get("ext") or "mp4"
        items.append(_item("video", info, ext, _quality_label(info), title))

    if not items:
        raise RuntimeError("未能从该链接提取到可下载的媒体流。")

    webpage_host = (urlparse(webpage_url).hostname or "").lower()
    return {
        "platform": _platform_of(webpage_host or (info.get("extractor_key") or "").lower()),
        "title": title,
        "thumbnail": info.get("thumbnail"),
        "uploader": info.get("uploader") or info.get("channel"),
        "duration": info.get("duration"),
        "webpage_url": webpage_url,
        "items": items,
    }
