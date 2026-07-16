"""Dispatch a URL to the right extractor and return normalised media items.

Each extractor returns a dict shaped like:
    {
        "platform": "youtube" | "bilibili" | "xiaohongshu" | ...,
        "title": str,
        "thumbnail": str | None,
        "uploader": str | None,
        "items": [
            {
                "kind": "video" | "image" | "audio",
                "url": str,                # direct URL to fetch
                "ext": str,                # mp4 / jpg / m4a ...
                "width": int | None,
                "height": int | None,
                "filesize": int | None,
                "quality_label": str | None,
                "needs_proxy": bool,       # True if browser cannot fetch directly (Referer required, etc.)
                "referer": str | None,     # only set when needs_proxy
                "filename": str,           # suggested filename for download
            },
            ...
        ],
    }
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

from . import bilibili, xhs, ytdlp

log = logging.getLogger(__name__)

_YTDLP_HOST_RE = re.compile(
    r"(youtube\.com|youtu\.be|twitter\.com|x\.com|"
    r"twitch\.tv|tiktok\.com|douyin\.com|instagram\.com|facebook\.com|"
    r"vimeo\.com|weibo\.com|kuaishou\.com)$"
)

_BILI_HOST_RE = re.compile(r"(bilibili\.com|b23\.tv)$")
_XHS_HOST_RE = re.compile(r"(xiaohongshu\.com|xhslink\.com)$")


class UnsupportedURLError(ValueError):
    pass


# Share blurbs (douyin/xhs "复制打开" text) wrap the URL in CJK prose and
# full-width punctuation. Pull out the first http(s) URL, stopping at
# whitespace, CJK characters, or full-width punctuation.
_URL_IN_TEXT_RE = re.compile(
    r"https?://[^\s<>\"'　-〿一-鿿＀-￯]+",
    re.IGNORECASE,
)


def extract_url_from_text(text: str) -> str:
    """Return the first URL embedded in arbitrary share text, else the input."""
    text = (text or "").strip()
    match = _URL_IN_TEXT_RE.search(text)
    if not match:
        return text
    # Trailing ASCII punctuation that share text tends to glue onto the URL.
    return match.group(0).rstrip(".,;:!)]}>")


def _host_of(url: str) -> str:
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host.startswith("m."):
        host = host[2:]
    return host


def resolve(url: str) -> dict:
    url = extract_url_from_text(url)
    if not url:
        raise UnsupportedURLError("URL 为空")

    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    host = _host_of(url)
    if not host:
        raise UnsupportedURLError("无法解析主机名")

    if _XHS_HOST_RE.search(host):
        return xhs.extract(url)

    if _BILI_HOST_RE.search(host):
        # Native API path is more reliable — yt-dlp's HTML scrape often hits 412
        # on cloud server IPs. Fall back to yt-dlp only if the API fails.
        try:
            return bilibili.extract(url)
        except bilibili.BiliUserError as exc:
            # A user-input problem (e.g. ?p=N out of range) — yt-dlp would fail
            # the same way with a worse message, so surface ours directly.
            raise UnsupportedURLError(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            log.warning("native bilibili extractor failed, falling back to yt-dlp: %s", exc)
            return ytdlp.extract(url)

    if _YTDLP_HOST_RE.search(host):
        return ytdlp.extract(url)

    # Best-effort: still try yt-dlp because it supports 1000+ sites.
    return ytdlp.extract(url)
