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

from . import http as requests


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


def _xhs_image_token(image_url: str) -> str | None:
    """Pull the watermark-free image token (path after host) out of any XHS image URL."""
    if not image_url or "/" not in image_url:
        return None
    parts = image_url.split("/", 5)
    if len(parts) < 6:
        return None
    return parts[5].split("!", 1)[0].split("?", 1)[0]


def _strip_image_processing(image_url: str) -> str:
    """Rehost an XHS image URL onto the watermark-free CDN.

    Borrowed from JoeanAmier/XHS-Downloader: take the image *token* (the path
    segment after the 5th `/` and before any `!processing` marker) and rebuild
    it against `sns-img-bd.xhscdn.com`. Dodges per-image watermark markers
    instead of just trimming the suffix.
    """
    token = _xhs_image_token(image_url)
    if not token:
        return (image_url or "").split("!", 1)[0]
    return f"https://sns-img-bd.xhscdn.com/{token}"


def _jpeg_preview_url(image_url: str, quality: int | None = None) -> str:
    """Return a JPEG-transcoded URL safe for `<img>` preview in any browser.

    XHS now serves the `notes_uhdr/` path as HEIC/Ultra HDR — Safari renders it
    natively, Chrome shows a broken image because `<img>` requires a known
    raster format. `ci.xiaohongshu.com` accepts `?imageView2/format/jpg` and
    transcodes on the fly without sacrificing the source resolution.

    `quality` maps to Qiniu's `/q/<n>`; without it the CDN defaults to ~q75, so
    downloads pass quality=100 for a near-lossless grab while the preview keeps
    the lighter default to save bandwidth.
    """
    token = _xhs_image_token(image_url)
    if not token:
        return image_url
    suffix = f"/q/{quality}" if quality else ""
    return f"https://ci.xiaohongshu.com/{token}?imageView2/format/jpg{suffix}"


def _undefined_to_null(blob: str) -> str:
    """Replace bare `undefined` value tokens (xhs embeds them) with null.

    The INITIAL_STATE blob is minified, so a genuine `undefined` value always
    sits immediately after `:`, `,` or `[` and before `,`, `}` or `]` with no
    surrounding whitespace. Anchoring on those delimiters avoids rewriting the
    word inside quoted strings (e.g. a note titled 'undefined behavior 踩坑'),
    which the previous word-boundary regex corrupted.
    """
    return re.sub(r"(?<=[:,\[])undefined(?=[,}\]])", "null", blob)


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
            # `.get(key, default)` only applies the default when the KEY is
            # absent — a present-but-empty [] would raise IndexError, and null
            # would raise TypeError. `(x or [None])[0]` handles both.
            url = s.get("masterUrl") or (s.get("backupUrls") or [None])[0]
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
            # Cross-origin: Chrome ignores `<a download>` so we route via proxy.
            "needs_proxy": True,
            "referer": None,
            "filename": _safe_filename(title, "mp4"),
        }
    ]


def _image_items(note: dict, title: str) -> list[dict]:
    images = note.get("imageList") or []
    items: list[dict] = []
    for idx, img in enumerate(images, start=1):
        raw = img.get("urlDefault") or img.get("urlPre") or img.get("url")
        if not raw:
            continue
        # Use the JPEG-transcoded URL for *both* preview and download:
        #   - The `notes_uhdr/` path serves HEIC/Ultra HDR; a `.jpg` filename
        #     over HEIC bytes is broken for most viewers.
        #   - JPEG keeps the original resolution, just swaps the codec.
        # Original/UHDR URL is kept on the item as `original_url` so power
        # users can copy it via the "复制链接" button.
        preview_url = _jpeg_preview_url(raw)
        download_url = _jpeg_preview_url(raw, quality=100)
        original_url = _strip_image_processing(raw)
        items.append(
            {
                "kind": "image",
                "url": download_url,
                "preview_url": preview_url,
                "original_url": original_url,
                "ext": "jpg",
                "width": img.get("width"),
                "height": img.get("height"),
                "filesize": None,
                "quality_label": f"{img.get('width') or '?'}×{img.get('height') or '?'}",
                # Force the proxy path: Chrome ignores `<a download>` on cross-
                # origin links, so a direct href to xhscdn.com just opens the
                # image instead of saving it. Routing via our same-origin proxy
                # restores download behaviour through Content-Disposition.
                "needs_proxy": True,
                "referer": None,
                "filename": _safe_filename(title, "jpg", idx),
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
    raw_thumb = (note.get("imageList") or [{}])[0].get("urlDefault")
    return {
        "platform": "xiaohongshu",
        "title": title,
        "thumbnail": _jpeg_preview_url(raw_thumb) if raw_thumb else None,
        "uploader": user.get("nickname") or user.get("nickName"),
        "duration": ((note.get("video") or {}).get("capa") or {}).get("duration"),
        "webpage_url": real_url,
        "items": items,
    }
