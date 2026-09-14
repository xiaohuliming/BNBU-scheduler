"""Preserve Instagram photo products that yt-dlp normally treats as videos.

Networking and login/access checks remain in the pinned upstream extractor.
Only explicit media_type=1 records become images; thumbnails on failed videos
must never be returned as successful downloads.
"""
import re
from urllib.parse import urlparse

from .http import validate_url
from .transport import MediaDownloadError, remember_headers


def is_post(url):
    parsed = urlparse(url)
    host = (parsed.hostname or '').lower()
    return (host == 'instagram.com' or host.endswith('.instagram.com')) and bool(
        re.match(r'^/(?:p|reel|reels|tv)/[A-Za-z0-9_-]+(?:/|$)', parsed.path))


def _dimension(value):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _photo_format(media):
    candidates = [item for item in (media.get('image_versions2') or {}).get('candidates', [])
                  if isinstance(item, dict) and isinstance(item.get('url'), str)]
    if not candidates:
        raise MediaDownloadError('Instagram 未返回该照片的下载地址，请重新复制帖子链接后重试。')
    # Instagram's logged-out payload can omit candidate dimensions. Its list
    # is highest-quality first; never reorder unknown sizes or edit signed URLs.
    best = max(candidates, key=lambda c: _dimension(c.get('width')) * _dimension(c.get('height')))
    parsed = validate_url(best['url'])
    host = (parsed.hostname or '').lower()
    if not any(host == domain or host.endswith('.' + domain) for domain in ('cdninstagram.com', 'fbcdn.net')):
        raise MediaDownloadError('Instagram 返回了不支持的照片下载地址。')
    ext = parsed.path.rsplit('.', 1)[-1].lower()
    if ext not in ('jpg', 'jpeg', 'png', 'webp'):
        raise MediaDownloadError('Instagram 返回的照片格式暂不支持下载。')
    return {'url': best['url'], 'ext': 'jpg' if ext == 'jpeg' else ext,
            'width': _dimension(best.get('width')) or _dimension(media.get('original_width')) or None,
            'height': _dimension(best.get('height')) or _dimension(media.get('original_height')) or None,
            'vcodec': 'none', 'acodec': 'none'}


def photo_aware_extractor():
    # Lazy import: the app's other native extractors remain usable if yt-dlp
    # is unavailable. Retain Instagram's own cookie and access-check behavior.
    from yt_dlp.extractor.instagram import InstagramIE as UpstreamInstagramIE

    class InstagramIE(UpstreamInstagramIE):
        def _extract_product_media(self, product_media):
            result = super()._extract_product_media(product_media)
            if _dimension(product_media.get('media_type')) == 1:
                result['formats'] = [_photo_format(product_media)]
                result['_maxcourse_photo'] = True
            return result

    return InstagramIE()


def process_result(ydl, info):
    """Run yt-dlp's normal video processing without rejecting photo entries."""
    if info.get('_type') == 'playlist':
        entries = list(info.get('entries') or [])
        if not entries or len(entries) > 50 or not all(isinstance(e, dict) for e in entries):
            raise MediaDownloadError('Instagram 图集数据不完整或数量过多，请尝试单条帖子链接。')
        context = {key: info[key] for key in ('extractor', 'extractor_key', 'webpage_url', 'http_headers') if key in info}
        return {**info, 'entries': [process_result(ydl, {**context, **entry}) for entry in entries]}
    if info.get('_maxcourse_photo'):
        return info
    return ydl.process_ie_result(info, download=False)


def media_result(info, url, normalize_video):
    entries = info.get('entries') if info.get('_type') == 'playlist' else [info]
    title = (info.get('description') or info.get('title') or 'Instagram').strip().splitlines()[0][:100]
    items = []
    for index, entry in enumerate(entries or [], 1):
        if entry.get('_maxcourse_photo'):
            photo = entry['formats'][0]
            headers = {**(info.get('http_headers') or {}), **(entry.get('http_headers') or {})}
            remember_headers(photo['url'], headers)
            selected = [{
                'kind': 'image', 'url': photo['url'], 'preview_url': photo['url'],
                'ext': photo['ext'], 'width': photo['width'], 'height': photo['height'],
                'filesize': None, 'quality_label': f"{photo['width'] or '?'}×{photo['height'] or '?'}",
                'needs_proxy': True, 'referer': 'https://www.instagram.com/',
            }]
        else:
            selected = normalize_video(entry, url)['items']
        for item in selected:
            safe_title = re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f]+', '_', title).strip()[:70] or 'Instagram'
            suffix = ('_' + item['kind']) if len(selected) > 1 else ''
            items.append({**item, 'filename': f"{safe_title}_{index:02d}{suffix}.{item['ext']}"})
    if not items:
        raise MediaDownloadError('Instagram 未返回可下载的图片或视频，请检查帖子是否公开。')
    return {'platform': 'instagram', 'title': title,
            'thumbnail': items[0]['url'] if items[0]['kind'] == 'image' else info.get('thumbnail'),
            'uploader': info.get('uploader') or info.get('channel'), 'duration': info.get('duration'),
            'webpage_url': info.get('webpage_url') or url, 'items': items}
