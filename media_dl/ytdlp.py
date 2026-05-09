"""yt-dlp wrapper. Handles YouTube, Bilibili, Twitter/X, TikTok, Douyin, etc."""

from __future__ import annotations

import logging
import os
import re
import tempfile
import threading
import time
from typing import Any

import requests

try:
    import yt_dlp  # type: ignore
except ImportError:  # pragma: no cover - dep is required at runtime
    yt_dlp = None


log = logging.getLogger(__name__)

_BILI_HOST_RE = re.compile(r"bilibili\.com|b23\.tv")
_DOUYIN_HOST_RE = re.compile(r"douyin\.com")

_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
)

# Bilibili rejects requests without `buvid3` (and friends) with HTTP 412.
# We bootstrap a Netscape-format cookie file once, then point yt-dlp at it.
_BILI_COOKIE_TTL = 30 * 60
_bili_cookie_cache: dict[str, Any] = {"path": "", "expires": 0.0}
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

    return cookies


def _bili_cookie_file() -> str:
    """Return the path to a Netscape cookies file with fresh Bilibili cookies."""
    now = time.time()
    cached_path = _bili_cookie_cache.get("path", "")
    if cached_path and os.path.exists(cached_path) and now < _bili_cookie_cache.get("expires", 0):
        return cached_path

    with _bili_cookie_lock:
        now = time.time()
        cached_path = _bili_cookie_cache.get("path", "")
        if cached_path and os.path.exists(cached_path) and now < _bili_cookie_cache.get("expires", 0):
            return cached_path
        try:
            cookies = _harvest_bili_cookies()
            if not cookies.get("buvid3"):
                log.warning("bilibili cookie harvest produced no buvid3 — yt-dlp may still 412")
                # Continue anyway; yt-dlp can still try.
            tmpdir = tempfile.gettempdir()
            path = os.path.join(tmpdir, "maxcourse_bili_cookies.txt")
            _write_netscape_cookies(path, cookies)
            _bili_cookie_cache["path"] = path
            _bili_cookie_cache["expires"] = time.time() + _BILI_COOKIE_TTL
            log.info("bilibili cookies refreshed → %s (%d entries)", path, len(cookies))
            return path
        except Exception as exc:  # noqa: BLE001
            log.warning("bilibili cookie bootstrap failed: %s", exc)
            return ""


def _safe_filename(title: str, ext: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", title or "video").strip()
    cleaned = cleaned[:80] or "video"
    return f"{cleaned}.{ext}"


def _pick_best_combined(formats: list[dict]) -> dict | None:
    """Pick the best progressive (audio+video) MP4 we can get without ffmpeg."""
    candidates = [
        f
        for f in formats
        if f.get("vcodec") not in (None, "none")
        and f.get("acodec") not in (None, "none")
        and (f.get("ext") in ("mp4", "webm"))
        and f.get("url")
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
        and f.get("url")
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
        and f.get("url")
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
    """Some hosts return 403 unless a Referer is set. Browser direct-download fails there."""
    if _BILI_HOST_RE.search(host):
        return True, "https://www.bilibili.com"
    if _DOUYIN_HOST_RE.search(host):
        return True, "https://www.douyin.com"
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
    return host.split(".")[0] or "unknown"


def extract(url: str) -> dict:
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

    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "extract_flat": False,
        "http_headers": http_headers,
        "socket_timeout": 15,
        "retries": 2,
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
    if proxy:
        opts["proxy"] = proxy

    def _run() -> dict[str, Any]:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    try:
        info: dict[str, Any] = _run()
    except yt_dlp.utils.DownloadError as exc:
        msg = str(exc)
        if is_bili and "412" in msg:
            log.info("bilibili 412 — flushing cookie cache and retrying once")
            _bili_cookie_cache["expires"] = 0.0
            cookiefile = _bili_cookie_file() or None
            if cookiefile:
                opts["cookiefile"] = cookiefile
            info = _run()
        else:
            raise

    if info.get("_type") == "playlist" and info.get("entries"):
        info = next((e for e in info["entries"] if e), info)

    formats = info.get("formats") or []
    title = info.get("title") or "video"
    webpage_url = info.get("webpage_url") or url
    host = (info.get("extractor") or "").lower() or webpage_url
    needs_proxy_flag, referer = _needs_proxy(webpage_url, webpage_url)

    items: list[dict] = []

    combined = _pick_best_combined(formats)
    if combined:
        ext = combined.get("ext") or "mp4"
        items.append(
            {
                "kind": "video",
                "url": combined["url"],
                "ext": ext,
                "width": combined.get("width"),
                "height": combined.get("height"),
                "filesize": combined.get("filesize") or combined.get("filesize_approx"),
                "quality_label": _quality_label(combined),
                "needs_proxy": needs_proxy_flag,
                "referer": referer if needs_proxy_flag else None,
                "filename": _safe_filename(title, ext),
            }
        )

    video_only = _pick_best_video_only(formats)
    audio_only = _pick_best_audio_only(formats)
    if video_only and audio_only and not combined:
        v_ext = video_only.get("ext") or "mp4"
        a_ext = audio_only.get("ext") or "m4a"
        items.append(
            {
                "kind": "video",
                "url": video_only["url"],
                "ext": v_ext,
                "width": video_only.get("width"),
                "height": video_only.get("height"),
                "filesize": video_only.get("filesize") or video_only.get("filesize_approx"),
                "quality_label": _quality_label(video_only) + " · 仅画面",
                "needs_proxy": needs_proxy_flag,
                "referer": referer if needs_proxy_flag else None,
                "filename": _safe_filename(title + "-video", v_ext),
            }
        )
        items.append(
            {
                "kind": "audio",
                "url": audio_only["url"],
                "ext": a_ext,
                "width": None,
                "height": None,
                "filesize": audio_only.get("filesize") or audio_only.get("filesize_approx"),
                "quality_label": _quality_label(audio_only) + " · 仅音频",
                "needs_proxy": needs_proxy_flag,
                "referer": referer if needs_proxy_flag else None,
                "filename": _safe_filename(title + "-audio", a_ext),
            }
        )

    if audio_only and combined:
        a_ext = audio_only.get("ext") or "m4a"
        items.append(
            {
                "kind": "audio",
                "url": audio_only["url"],
                "ext": a_ext,
                "width": None,
                "height": None,
                "filesize": audio_only.get("filesize") or audio_only.get("filesize_approx"),
                "quality_label": _quality_label(audio_only) + " · 仅音频",
                "needs_proxy": needs_proxy_flag,
                "referer": referer if needs_proxy_flag else None,
                "filename": _safe_filename(title + "-audio", a_ext),
            }
        )

    if not items and info.get("url"):
        ext = info.get("ext") or "mp4"
        items.append(
            {
                "kind": "video",
                "url": info["url"],
                "ext": ext,
                "width": info.get("width"),
                "height": info.get("height"),
                "filesize": info.get("filesize"),
                "quality_label": _quality_label(info),
                "needs_proxy": needs_proxy_flag,
                "referer": referer if needs_proxy_flag else None,
                "filename": _safe_filename(title, ext),
            }
        )

    if not items:
        raise RuntimeError("未能从该链接提取到可下载的媒体流。")

    return {
        "platform": _platform_of((info.get("extractor_key") or info.get("extractor") or "").lower()),
        "title": title,
        "thumbnail": info.get("thumbnail"),
        "uploader": info.get("uploader") or info.get("channel"),
        "duration": info.get("duration"),
        "webpage_url": webpage_url,
        "items": items,
    }
