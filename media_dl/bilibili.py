"""Native Bilibili extractor — bypasses yt-dlp because the HTML scrape often hits 412.

Strategy:
  1. Resolve b23.tv short links and pull BV id from any /video/BVxxx URL.
  2. GET /x/web-interface/view?bvid=... for title + cid (these are the IDs needed
     for the play-url API).
  3. GET /x/player/playurl with progressive fnval=1 first; fall back to DASH
     fnval=16 when the user needs >480p.
  4. Return the same normalised shape as the yt-dlp wrapper. needs_proxy=True
     because Bilibili CDN nodes require Referer.
"""

from __future__ import annotations

import logging
import re

import requests

from .ytdlp import _BROWSER_UA, _harvest_bili_cookies

log = logging.getLogger(__name__)

_BVID_RE = re.compile(r"(BV[A-Za-z0-9]{10})")
_AVID_RE = re.compile(r"/video/(?:av|AV)(\d+)")

_API_VIEW = "https://api.bilibili.com/x/web-interface/view"
_API_PLAYURL = "https://api.bilibili.com/x/player/playurl"

# Bilibili quality codes → human labels. Without login, qn=80 is the ceiling.
_QUALITY_LABELS = {
    6: "240p", 16: "360p", 32: "480p", 64: "720p", 74: "720p60",
    80: "1080p", 112: "1080p+", 116: "1080p60", 120: "4K",
    125: "HDR", 126: "杜比视界", 127: "8K",
}


def _quality_label(qn) -> str:
    try:
        qn_int = int(qn)
    except (TypeError, ValueError):
        return "原画"
    return _QUALITY_LABELS.get(qn_int, f"qn{qn_int}")

_HEADERS = {
    "User-Agent": _BROWSER_UA,
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://www.bilibili.com",
    "Origin": "https://www.bilibili.com",
}


class BiliError(RuntimeError):
    pass


def _safe_filename(title: str, ext: str, suffix: str = "") -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", title or "bilibili").strip()
    cleaned = cleaned[:80] or "bilibili"
    if suffix:
        cleaned = f"{cleaned}_{suffix}"
    return f"{cleaned}.{ext}"


def _resolve_short(url: str) -> str:
    if "b23.tv" not in url:
        return url
    try:
        resp = requests.get(url, headers=_HEADERS, allow_redirects=True, timeout=8)
        return resp.url or url
    except requests.RequestException as exc:
        log.warning("b23.tv resolve failed: %s", exc)
        return url


def _identify(url: str) -> tuple[str | None, int | None]:
    """Return (bvid, aid). Exactly one is populated."""
    bv_match = _BVID_RE.search(url)
    if bv_match:
        return bv_match.group(1), None
    av_match = _AVID_RE.search(url)
    if av_match:
        return None, int(av_match.group(1))
    return None, None


def _api_get(endpoint: str, params: dict, cookies: dict[str, str]) -> dict:
    resp = requests.get(endpoint, params=params, headers=_HEADERS, cookies=cookies, timeout=12)
    if resp.status_code != 200:
        snippet = resp.text[:200]
        raise BiliError(f"B站 API HTTP {resp.status_code}: {snippet}")
    try:
        body = resp.json()
    except ValueError as exc:
        raise BiliError(f"B站 API 返回非 JSON: {exc}") from exc
    if body.get("code") != 0:
        raise BiliError(f"B站 API 错误 code={body.get('code')}: {body.get('message')}")
    return body.get("data") or {}


def _build_items_progressive(play_data: dict, title: str) -> list[dict]:
    durl = play_data.get("durl") or []
    if not durl:
        return []
    quality_label = _quality_label(play_data.get("quality"))
    items: list[dict] = []
    multi = len(durl) > 1
    for idx, seg in enumerate(durl, start=1):
        url = seg.get("url") or (seg.get("backup_url") or [None])[0]
        if not url:
            continue
        suffix = f"part{idx}" if multi else ""
        label = f"{quality_label} · MP4"
        if multi:
            label += f" · 分段 {idx}/{len(durl)}"
        items.append(
            {
                "kind": "video",
                "url": url,
                "ext": "mp4",
                "width": None,
                "height": None,
                "filesize": seg.get("size"),
                "quality_label": label,
                "needs_proxy": True,
                "referer": "https://www.bilibili.com",
                "filename": _safe_filename(title, "mp4", suffix),
            }
        )
    return items


def _build_items_dash(play_data: dict, title: str) -> list[dict]:
    dash = play_data.get("dash") or {}
    videos = dash.get("video") or []
    audios = dash.get("audio") or []
    items: list[dict] = []

    if videos:
        videos_sorted = sorted(
            videos,
            key=lambda v: (v.get("id") or 0, v.get("bandwidth") or 0),
            reverse=True,
        )
        v = videos_sorted[0]
        items.append(
            {
                "kind": "video",
                "url": v.get("base_url") or v.get("baseUrl"),
                "ext": "m4s",
                "width": v.get("width"),
                "height": v.get("height"),
                "filesize": None,
                "quality_label": f"{v.get('height') or '?'}p · DASH · 仅画面",
                "needs_proxy": True,
                "referer": "https://www.bilibili.com",
                "filename": _safe_filename(title, "m4s", "video"),
            }
        )

    if audios:
        audios_sorted = sorted(
            audios,
            key=lambda a: (a.get("id") or 0, a.get("bandwidth") or 0),
            reverse=True,
        )
        a = audios_sorted[0]
        items.append(
            {
                "kind": "audio",
                "url": a.get("base_url") or a.get("baseUrl"),
                "ext": "m4s",
                "width": None,
                "height": None,
                "filesize": None,
                "quality_label": f"DASH · 仅音频 · {int((a.get('bandwidth') or 0) / 1000)}kbps",
                "needs_proxy": True,
                "referer": "https://www.bilibili.com",
                "filename": _safe_filename(title, "m4s", "audio"),
            }
        )

    return [it for it in items if it.get("url")]


def extract(url: str) -> dict:
    real_url = _resolve_short(url)
    bvid, aid = _identify(real_url)
    if not bvid and not aid:
        raise BiliError("无法识别 B站 视频 ID（既不是 BV 号也不是 av 号）")

    cookies = _harvest_bili_cookies()
    if not cookies.get("buvid3"):
        log.warning("native bilibili extractor: no buvid3 — request may still be blocked")

    view_params: dict = {}
    if bvid:
        view_params["bvid"] = bvid
    else:
        view_params["aid"] = aid

    view = _api_get(_API_VIEW, view_params, cookies)

    title = view.get("title") or "bilibili"
    pic = view.get("pic")
    owner = (view.get("owner") or {}).get("name")
    duration = view.get("duration")
    cid = view.get("cid")
    bvid = view.get("bvid") or bvid
    if not cid:
        # Multi-page: pick the first part.
        pages = view.get("pages") or []
        if pages:
            cid = pages[0].get("cid")
    if not cid:
        raise BiliError("未获取到 cid，无法请求播放地址")

    play_params: dict = {
        "cid": cid,
        "qn": 80,           # 1080p ceiling for unauthenticated access
        "fnval": 1,         # progressive MP4 first
        "fnver": 0,
        "fourk": 1,
    }
    if bvid:
        play_params["bvid"] = bvid
    elif aid:
        play_params["avid"] = aid

    play_data = _api_get(_API_PLAYURL, play_params, cookies)
    items = _build_items_progressive(play_data, title)

    if not items:
        play_params["fnval"] = 16  # DASH fallback for higher resolutions
        play_data = _api_get(_API_PLAYURL, play_params, cookies)
        items = _build_items_dash(play_data, title)

    if not items:
        raise BiliError("未能从该视频解析出可用的播放流。")

    canonical = (
        f"https://www.bilibili.com/video/{bvid}/" if bvid else f"https://www.bilibili.com/video/av{aid}/"
    )

    return {
        "platform": "bilibili",
        "title": title,
        "thumbnail": pic,
        "uploader": owner,
        "duration": duration,
        "webpage_url": canonical,
        "items": items,
    }
