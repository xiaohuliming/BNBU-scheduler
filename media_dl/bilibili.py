"""Native Bilibili extractor — bypasses yt-dlp because the HTML scrape often hits 412.

Strategy:
  1. Resolve b23.tv short links and pull BV id from any /video/BVxxx URL.
  2. GET /x/web-interface/view?bvid=... for title + cid (these are the IDs needed
     for the play-url API).
  3. GET /x/player/playurl with progressive fnval=1 first; fall back to DASH
     fnval=16 when the user needs >480p.
  4. If an API request 412s on a cloud IP, retry that same API call with the
     `facebookexternalhit/1.1` User-Agent. B站 currently allows its link-preview
     crawler through the anti-bot layer even when browser UAs are blocked.
     Keep the legacy HTML scraper as a last resort for pages that still embed
     `window.__playinfo__`.
  5. Return the same normalised shape as the yt-dlp wrapper. needs_proxy=True
     because Bilibili CDN nodes require Referer.
"""

from __future__ import annotations

import json
import logging
import re
from urllib.parse import parse_qs, urlparse

from . import http as requests

from .ytdlp import _BROWSER_UA, bili_cookie_dict

log = logging.getLogger(__name__)

_BVID_RE = re.compile(r"(BV[A-Za-z0-9]{10})")
_AVID_RE = re.compile(r"/video/(?:av|AV)(\d+)")


def _page_index(url: str) -> int:
    """1-based ?p= page number for multi-part videos (defaults to 1)."""
    try:
        p = parse_qs(urlparse(url).query).get("p", ["1"])[0]
        return max(1, int(p))
    except (TypeError, ValueError):
        return 1

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
_FB_UA = "facebookexternalhit/1.1"
_FB_API_HEADERS = {**_HEADERS, "User-Agent": _FB_UA}


class BiliError(RuntimeError):
    pass


class BiliUserError(BiliError):
    """A user-facing condition (e.g. bad ?p=N) where the yt-dlp fallback can't
    help either — the dispatcher surfaces it instead of silently falling back."""


# Official bilibili CDN host suffixes — all already on the proxy allowlist.
# The playurl API's primary `url`/`base_url` is usually a third-party PCDN edge
# (e.g. *.edge.mountaintoys.cn, *.szbdyd.com) that rejects datacenter IPs and
# isn't (safely) proxy-allowlistable; the `backup_url` list carries the official
# upos-*.bilivideo.com mirror. Prefer the official host so the server-side proxy
# both passes the allowlist and hits a CDN that serves our IP reliably.
# NOTE: these suffixes must stay a subset of what routes.py `_host_allowed`
# permits, or we'd prefer a URL the proxy then 403s. Bilibili's Akamai mirror is
# always `*-mirrorakam.akamaized.net`, matched precisely there.
_OFFICIAL_BILI_CDN = (".bilivideo.com", ".bilivideo.cn", ".hdslb.com", "mirrorakam.akamaized.net")


def _prefer_official_url(primary: str | None, backups) -> str | None:
    """Pick the first official-CDN URL among primary+backups, else the primary."""
    candidates = [primary, *(backups or [])]
    candidates = [c for c in candidates if c]
    for url in candidates:
        host = (urlparse(url).hostname or "").lower()
        if any(host.endswith(sfx) for sfx in _OFFICIAL_BILI_CDN):
            return url
    return candidates[0] if candidates else None


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
    if resp.status_code == 412:
        log.info("bilibili API 412 — retrying with Facebook crawler UA")
        resp = requests.get(
            endpoint,
            params=params,
            headers=_FB_API_HEADERS,
            cookies=cookies,
            timeout=12,
        )
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
        url = _prefer_official_url(seg.get("url"), seg.get("backup_url"))
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
                # DASH segments are fragmented MP4 and play standalone under a
                # .mp4/.m4a name (VLC/QuickTime); '.m4s' has no player
                # association. The two files still need muxing for a combined
                # A/V file — the UI flags that.
                "url": _prefer_official_url(
                    v.get("base_url") or v.get("baseUrl"),
                    v.get("backup_url") or v.get("backupUrl"),
                ),
                "ext": "mp4",
                "width": v.get("width"),
                "height": v.get("height"),
                "filesize": None,
                "quality_label": f"{v.get('height') or '?'}p · DASH · 仅画面",
                "needs_proxy": True,
                "referer": "https://www.bilibili.com",
                "needs_merge": True,
                "filename": _safe_filename(title, "mp4", "video"),
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
                "url": _prefer_official_url(
                    a.get("base_url") or a.get("baseUrl"),
                    a.get("backup_url") or a.get("backupUrl"),
                ),
                "ext": "m4a",
                "width": None,
                "height": None,
                "filesize": None,
                "quality_label": f"DASH · 仅音频 · {int((a.get('bandwidth') or 0) / 1000)}kbps",
                "needs_proxy": True,
                "referer": "https://www.bilibili.com",
                "needs_merge": True,
                "filename": _safe_filename(title, "m4a", "audio"),
            }
        )

    return [it for it in items if it.get("url")]


# Cobalt-style fallback: B站 whitelists the Facebook link-preview crawler UA,
# so this path skips WBI signing and most IP-level anti-bot checks.
_PLAYINFO_RE = re.compile(r"window\.__playinfo__\s*=\s*(\{.+?\})\s*</script>", re.DOTALL)
_INITIAL_STATE_BILI_RE = re.compile(
    r"window\.__INITIAL_STATE__\s*=\s*(\{.+?\});\(function", re.DOTALL
)
_HTML_TITLE_RE = re.compile(r"<title>([^<]+)</title>")


def _extract_via_html(bvid: str | None, aid: int | None, page: int = 1) -> dict:
    """Scrape the public video page using the Facebook crawler UA."""
    page_path = f"video/{bvid}/" if bvid else f"video/av{aid}/"
    page_url = f"https://www.bilibili.com/{page_path}"
    if page > 1:
        page_url += f"?p={page}"
    headers = {
        "User-Agent": _FB_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    resp = requests.get(page_url, headers=headers, timeout=12)
    if resp.status_code != 200:
        raise BiliError(f"HTML 抓取失败 HTTP {resp.status_code}")

    html = resp.text
    play_match = _PLAYINFO_RE.search(html)
    if not play_match:
        raise BiliError("HTML 中未找到 __playinfo__ 字段（页面可能是登录墙或专栏）")

    try:
        play_blob = json.loads(play_match.group(1))
    except json.JSONDecodeError as exc:
        raise BiliError(f"__playinfo__ 解析失败: {exc}") from exc
    play_data = play_blob.get("data") or {}

    # Title + uploader from INITIAL_STATE if available, fall back to <title>.
    title = ""
    uploader = None
    pic = None
    duration = None
    state_match = _INITIAL_STATE_BILI_RE.search(html)
    if state_match:
        try:
            state = json.loads(state_match.group(1))
            v = state.get("videoData") or {}
            title = v.get("title") or title
            pic = v.get("pic")
            duration = v.get("duration")
            uploader = (v.get("owner") or {}).get("name")
        except json.JSONDecodeError:
            pass
    if not title:
        t = _HTML_TITLE_RE.search(html)
        if t:
            title = t.group(1).replace("_哔哩哔哩_bilibili", "").strip()
    title = title or "bilibili"

    items = _build_items_progressive(play_data, title) or _build_items_dash(play_data, title)
    if not items:
        raise BiliError("HTML 中找到了 __playinfo__ 但没有可用的播放流。")

    return {
        "platform": "bilibili",
        "title": title,
        "thumbnail": pic,
        "uploader": uploader,
        "duration": duration,
        "webpage_url": page_url,
        "items": items,
    }


def _extract_via_html_after_api_failure(
    bvid: str | None,
    aid: int | None,
    page: int,
    api_error: BiliError,
) -> dict:
    """Try the legacy page fallback without discarding the API diagnosis."""
    try:
        return _extract_via_html(bvid, aid, page)
    except BiliError as html_error:
        raise BiliError(
            f"{api_error}; HTML 兜底失败: {html_error}"
        ) from html_error


def extract(url: str) -> dict:
    real_url = _resolve_short(url)
    bvid, aid = _identify(real_url)
    if not bvid and not aid:
        raise BiliError("无法识别 B站 视频 ID（既不是 BV 号也不是 av 号）")

    page = _page_index(real_url)
    cookies = bili_cookie_dict()
    if not cookies.get("buvid3"):
        log.warning("native bilibili extractor: no buvid3 — request may still be blocked")

    view_params: dict = {}
    if bvid:
        view_params["bvid"] = bvid
    else:
        view_params["aid"] = aid

    try:
        view = _api_get(_API_VIEW, view_params, cookies)
    except BiliError as exc:
        log.warning("bilibili API view failed (%s) — trying HTML fallback", exc)
        return _extract_via_html_after_api_failure(bvid, aid, page, exc)

    title = view.get("title") or "bilibili"
    pic = view.get("pic")
    owner = (view.get("owner") or {}).get("name")
    duration = view.get("duration")
    bvid = view.get("bvid") or bvid

    # Multi-part videos: select the requested ?p=N part (1-based). The top-level
    # `cid`/`title` describe part 1, so for p>1 we override both from `pages`.
    pages = view.get("pages") or []
    cid = view.get("cid")
    if pages:
        if page > len(pages):
            raise BiliUserError(f"该视频只有 {len(pages)} 个分P，但链接请求的是第 {page} P")
        part = pages[page - 1]
        cid = part.get("cid") or cid
        part_title = part.get("part") or ""
        if len(pages) > 1:
            duration = part.get("duration") or duration
            if part_title and part_title != title:
                title = f"{title}_P{page}_{part_title}"
            else:
                title = f"{title}_P{page}"
    if not cid:
        raise BiliError("未获取到 cid，无法请求播放地址")

    # qn is the *requested* quality ceiling; the playurl API silently downgrades
    # anonymous requests to ~480p regardless. 1080p+ needs a login cookie —
    # set BILIBILI_SESSDATA in the server env (merged into `cookies`) to unlock.
    play_params: dict = {
        "cid": cid,
        "qn": 112 if cookies.get("SESSDATA") else 80,
        "fnval": 1,         # progressive MP4 first
        "fnver": 0,
        "fourk": 1,
    }
    if bvid:
        play_params["bvid"] = bvid
    elif aid:
        play_params["avid"] = aid

    try:
        play_data = _api_get(_API_PLAYURL, play_params, cookies)
        items = _build_items_progressive(play_data, title)

        if not items:
            play_params["fnval"] = 16  # DASH fallback for higher resolutions
            play_data = _api_get(_API_PLAYURL, play_params, cookies)
            items = _build_items_dash(play_data, title)
    except BiliError as exc:
        log.warning("bilibili playurl API failed (%s) — trying HTML fallback", exc)
        return _extract_via_html_after_api_failure(bvid, aid, page, exc)

    if not items:
        log.warning("bilibili playurl returned no streams — trying HTML fallback")
        return _extract_via_html(bvid, aid, page)

    base = (
        f"https://www.bilibili.com/video/{bvid}/" if bvid else f"https://www.bilibili.com/video/av{aid}/"
    )
    canonical = f"{base}?p={page}" if page > 1 else base

    return {
        "platform": "bilibili",
        "title": title,
        "thumbnail": pic,
        "uploader": owner,
        "duration": duration,
        "webpage_url": canonical,
        "items": items,
    }
