"""Lightweight event logger for media-dl. Best-effort: never raises into the request path."""

from __future__ import annotations

import logging
import sqlite3
from urllib.parse import urlparse

log = logging.getLogger(__name__)

# Same DB the rest of the Flask app uses. Kept as a module constant so this
# package stays decoupled from app.py's import-time side effects.
DB_PATH = "maxcourse.db"


def host_of(url: str) -> str:
    if not url:
        return ""
    host = (urlparse(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host.startswith("m."):
        host = host[2:]
    return host


def platform_of_host(host: str) -> str:
    """Coarse mapping from host → platform key. Mirrors the labels the resolver returns."""
    if not host:
        return "unknown"
    if "youtube.com" in host or "youtu.be" in host or "googlevideo.com" in host or "ytimg.com" in host:
        return "youtube"
    if "bilibili.com" in host or "b23.tv" in host or "hdslb.com" in host or "bilivideo" in host:
        return "bilibili"
    if "xiaohongshu.com" in host or "xhslink.com" in host or "xhscdn.com" in host:
        return "xiaohongshu"
    if "douyin" in host or "snssdk.com" in host:
        return "douyin"
    if "tiktok" in host:
        return "tiktok"
    if "twitter.com" in host or "x.com" in host or "twimg.com" in host:
        return "twitter"
    return host.split(".")[0] or "unknown"


def log_event(
    *,
    visitor_id: str | None,
    user_id: int | None,
    action: str,
    platform: str | None,
    host: str | None,
    success: bool,
    bytes_count: int = 0,
    elapsed_ms: int | None = None,
    error: str | None = None,
) -> None:
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                """
                INSERT INTO media_dl_events
                    (visitor_id, user_id, action, platform, host, success, bytes, elapsed_ms, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    visitor_id,
                    user_id,
                    action,
                    platform or "unknown",
                    host or "",
                    1 if success else 0,
                    int(bytes_count or 0),
                    int(elapsed_ms) if elapsed_ms is not None else None,
                    (error or None) and str(error)[:500],
                ),
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001 - analytics must never break a request
        log.warning("media-dl analytics insert failed: %s", exc)
