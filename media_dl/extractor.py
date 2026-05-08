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

import re
from urllib.parse import urlparse

from . import xhs, ytdlp


_YTDLP_HOST_RE = re.compile(
    r"(youtube\.com|youtu\.be|bilibili\.com|b23\.tv|twitter\.com|x\.com|"
    r"twitch\.tv|tiktok\.com|douyin\.com|instagram\.com|facebook\.com|"
    r"vimeo\.com|weibo\.com|kuaishou\.com)$"
)

_XHS_HOST_RE = re.compile(r"(xiaohongshu\.com|xhslink\.com)$")


class UnsupportedURLError(ValueError):
    pass


def _host_of(url: str) -> str:
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host.startswith("m."):
        host = host[2:]
    return host


def resolve(url: str) -> dict:
    url = (url or "").strip()
    if not url:
        raise UnsupportedURLError("URL 为空")

    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    host = _host_of(url)
    if not host:
        raise UnsupportedURLError("无法解析主机名")

    if _XHS_HOST_RE.search(host):
        return xhs.extract(url)

    if _YTDLP_HOST_RE.search(host):
        return ytdlp.extract(url)

    # Best-effort: still try yt-dlp because it supports 1000+ sites.
    return ytdlp.extract(url)
