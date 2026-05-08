"""Best-effort Xiaohongshu (小红书) extractor.

Strategy:
  1. Follow xhslink.com short links to the real explore URL.
  2. Fetch the page HTML with a browser-like User-Agent.
  3. Locate the `window.__INITIAL_STATE__ = {...}` blob and parse out
     `noteDetailMap[noteId].note` — that holds title, imageList, video info.
  4. Pick non-watermarked URLs:
       - For images: imageList[*].urlDefault works if we strip trailing `!...` style
         processing markers; we also try imageList[*].urlPre for a larger raw.
       - For videos: video.media.stream.h264[*].masterUrl gives a clean direct mp4.

XHS rotates anti-bot heuristics often; if the page returns a login wall, we surface
a clear error so the caller can show it.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse

import requests


_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

_INITIAL_STATE_RE = re.compile(
    r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*</script>",
    re.DOTALL,
)


class XhsError(RuntimeError):
    pass


def _safe_filename(title: str, ext: str, idx: int | None = None) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", title or "xhs").strip()
    cleaned = cleaned[:60] or "xhs"
    if idx is not None:
        cleaned = f"{cleaned}_{idx:02d}"
    return f"{cleaned}.{ext}"


def _resolve_real_url(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if "xhslink.com" not in host:
        return url
    resp = requests.get(url, headers=_HEADERS, allow_redirects=True, timeout=12)
    if resp.url and "xiaohongshu.com" in resp.url:
        return resp.url
    return url


def _strip_image_processing(image_url: str) -> str:
    """xhs images often end with `!nd_dft_wgth_webp_3` style markers — strip to original."""
    if not image_url:
        return image_url
    if "!" in image_url:
        return image_url.split("!", 1)[0]
    return image_url


def _undefined_to_null(blob: str) -> str:
    """Replace bare `undefined` tokens (xhs sometimes embeds them) with null."""
    return re.sub(r"(?<![A-Za-z0-9_])undefined(?![A-Za-z0-9_])", "null", blob)


def _parse_initial_state(html: str) -> dict:
    match = _INITIAL_STATE_RE.search(html)
    if not match:
        raise XhsError("未在页面中找到笔记数据，可能链接已失效或需要登录。")
    raw = _undefined_to_null(match.group(1))
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise XhsError(f"笔记数据解析失败: {exc}") from exc


def _extract_note(state: dict, fallback_id: str | None) -> tuple[str, dict]:
    note_map = (
        state.get("note", {}).get("noteDetailMap")
        or state.get("noteDetailMap")
        or {}
    )
    if not note_map:
        raise XhsError("笔记数据为空，可能需要登录。")

    if fallback_id and fallback_id in note_map:
        node = note_map[fallback_id]
    else:
        node = next(iter(note_map.values()))

    note = node.get("note") or node
    note_id = note.get("noteId") or fallback_id or ""
    return note_id, note


def _video_items(note: dict, title: str) -> list[dict]:
    video = note.get("video") or {}
    streams = ((video.get("media") or {}).get("stream")) or {}
    candidates: list[dict] = []
    for codec_key in ("h264", "h265", "av1"):
        for s in streams.get(codec_key) or []:
            url = s.get("masterUrl") or s.get("backupUrls", [None])[0]
            if not url:
                continue
            candidates.append(
                {
                    "url": url,
                    "width": s.get("width"),
                    "height": s.get("height"),
                    "filesize": s.get("size"),
                    "quality_label": f"{s.get('height') or '?'}p · {codec_key.upper()}",
                }
            )
    if not candidates:
        return []
    best = max(candidates, key=lambda c: (c["height"] or 0, c["filesize"] or 0))
    return [
        {
            "kind": "video",
            "url": best["url"],
            "ext": "mp4",
            "width": best["width"],
            "height": best["height"],
            "filesize": best["filesize"],
            "quality_label": best["quality_label"],
            "needs_proxy": False,
            "referer": None,
            "filename": _safe_filename(title, "mp4"),
        }
    ]


def _image_items(note: dict, title: str) -> list[dict]:
    images = note.get("imageList") or []
    items: list[dict] = []
    for idx, img in enumerate(images, start=1):
        url = img.get("urlDefault") or img.get("urlPre") or img.get("url")
        if not url:
            continue
        url = _strip_image_processing(url)
        ext = "jpg"
        if ".webp" in url.lower():
            ext = "webp"
        elif ".png" in url.lower():
            ext = "png"
        items.append(
            {
                "kind": "image",
                "url": url,
                "ext": ext,
                "width": img.get("width"),
                "height": img.get("height"),
                "filesize": None,
                "quality_label": f"{img.get('width') or '?'}×{img.get('height') or '?'}",
                "needs_proxy": False,
                "referer": None,
                "filename": _safe_filename(title, ext, idx),
            }
        )
    return items


def extract(url: str) -> dict:
    real_url = _resolve_real_url(url)
    parsed = urlparse(real_url)
    note_id_match = re.search(r"/(?:explore|discovery/item)/([0-9a-f]+)", parsed.path)
    fallback_id = note_id_match.group(1) if note_id_match else None

    resp = requests.get(real_url, headers=_HEADERS, timeout=12)
    if resp.status_code != 200:
        raise XhsError(f"抓取页面失败 (HTTP {resp.status_code})")

    state = _parse_initial_state(resp.text)
    note_id, note = _extract_note(state, fallback_id)
    title = note.get("title") or note.get("desc") or f"xhs_{note_id or 'note'}"
    title = title.strip().splitlines()[0] if title else "xhs"

    items = _video_items(note, title) or _image_items(note, title)
    if not items:
        raise XhsError("未能从该笔记中解析出图片或视频。")

    user = (note.get("user") or {})
    return {
        "platform": "xiaohongshu",
        "title": title,
        "thumbnail": (note.get("imageList") or [{}])[0].get("urlDefault"),
        "uploader": user.get("nickname") or user.get("nickName"),
        "duration": (note.get("video") or {}).get("capa", {}).get("duration"),
        "webpage_url": real_url,
        "items": items,
    }
