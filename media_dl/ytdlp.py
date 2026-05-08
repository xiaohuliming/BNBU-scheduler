"""yt-dlp wrapper. Handles YouTube, Bilibili, Twitter/X, TikTok, Douyin, etc."""

from __future__ import annotations

import re
from typing import Any

try:
    import yt_dlp  # type: ignore
except ImportError:  # pragma: no cover - dep is required at runtime
    yt_dlp = None


_BILI_HOST_RE = re.compile(r"bilibili\.com|b23\.tv")
_DOUYIN_HOST_RE = re.compile(r"douyin\.com")


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

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "extract_flat": False,
    }

    with yt_dlp.YoutubeDL(opts) as ydl:
        info: dict[str, Any] = ydl.extract_info(url, download=False)

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
