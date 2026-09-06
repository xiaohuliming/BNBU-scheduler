"""Extract public Douyin videos using the current mobile share-page protocol.

The page supplies a per-visit token. Its own client encrypts that token with
the public web ID before requesting iteminfo. No personal browser cookies or
account credentials are needed. Verification challenges are surfaced to users.
"""
from __future__ import annotations

import base64
import json
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from . import http
from .transport import remember_headers

_UA = ('Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) '
       'AppleWebKit/605.1.15 Version/17.0 Mobile/15E148 Safari/604.1')
_VIDEO_ID = re.compile(r'/(?:share/)?(?:video|note|slides)/(\d+)')


class DouyinError(RuntimeError):
    pass


def _share_context(html: str) -> tuple[dict, str]:
    marker = re.search(r'window\._ROUTER_DATA\s*=\s*', html)
    if not marker:
        raise DouyinError('抖音分享页暂时无法读取，请重新复制完整分享链接后重试。')
    try:
        state, _ = json.JSONDecoder().raw_decode(html[marker.end():].lstrip())
        context = next(value for value in state.get('loaderData', {}).values()
                       if isinstance(value, dict) and value.get('itemId'))
        web_id = str(context['webId'])
        if len(web_id) < 16 or not web_id.isdigit():
            raise ValueError('invalid web ID')
        soup = BeautifulSoup(html, 'html.parser')
        config_tag = soup.find(id='douyin_reflow_tcc')
        config = json.loads(config_tag.get('tccconfig', '{}')) if config_tag else {}
        settings = config.get('token_encry_cooperation') or {}
        if not isinstance(settings, dict):
            settings = {}
        use_new = int(web_id[-3:]) < float(settings.get('new_fe_key_ratio') or 0)
        token_id = settings.get('new_fe_key' if use_new else 'fe_key') or 'douyin_reflow_token'
        token_tag = soup.find(id=token_id)
        token = token_tag.get('xsstoken') if token_tag else None
        if not token:
            raise ValueError('missing share token')
        key = web_id[:16].encode('ascii')
        padder = PKCS7(128).padder()
        padded = padder.update(token.encode('utf-8')) + padder.finalize()
        encryptor = Cipher(algorithms.AES(key), modes.CBC(key)).encryptor()
        encoded = base64.b64encode(encryptor.update(padded) + encryptor.finalize()).decode('ascii')
        return context, encoded
    except (ValueError, KeyError, TypeError, StopIteration) as exc:
        raise DouyinError('抖音分享页访问状态已变化，请稍后重新解析。') from exc


def _result(item: dict, webpage: str) -> dict:
    video = item.get('video') or {}
    sources = [video, *(entry for entry in video.get('bit_rate') or [] if isinstance(entry, dict))]
    candidates = []
    for source in sources:
        address = source.get('play_addr') or {}
        urls = address.get('url_list') or []
        if urls:
            candidates.append((int(source.get('bit_rate') or 0), source, address, urls[0]))
    if not candidates:
        raise DouyinError('该抖音内容没有可下载的视频，暂不支持此类图文或已失效的内容。')
    _, source, address, url = max(candidates, key=lambda entry: entry[0])
    parsed = urlparse(url)
    if parsed.hostname == 'aweme.snssdk.com' and parsed.path == '/aweme/v1/playwm/':
        # The share payload names the watermarked preview endpoint. The public
        # play endpoint accepts the same source video ID and returns the source.
        url = parsed._replace(path='/aweme/v1/play/').geturl()
    http.validate_url(url)
    title = (item.get('desc') or '抖音视频').strip()
    filename = re.sub(r'[\\/:*?"<>|\r\n\t]+', '_', title)[:80] + '.mp4'
    remember_headers(url, {'User-Agent': _UA, 'Referer': webpage})
    # Top-level dimensions describe the uploaded source, while play may serve
    # a lower rendition. Only label dimensions explicitly attached to a stream.
    width = address.get('width')
    height = address.get('height')
    covers = (video.get('cover') or {}).get('url_list') or []
    return {
        'platform': 'douyin', 'title': title, 'webpage_url': webpage,
        'uploader': (item.get('author') or {}).get('nickname'),
        'thumbnail': covers[0] if covers else None,
        'duration': (video.get('duration') or 0) / 1000,
        'items': [{
            'kind': 'video', 'url': url, 'ext': 'mp4', 'filename': filename,
            'width': width, 'height': height, 'filesize': address.get('data_size'),
            'quality_label': f'{height}p · MP4' if height else 'MP4',
            'needs_proxy': True, 'referer': webpage,
        }],
    }


def extract(url: str) -> dict:
    http.validate_url(url)
    with http.Session() as session:
        session.headers.update({'User-Agent': _UA, 'Accept-Language': 'zh-CN,zh;q=0.9'})
        match = _VIDEO_ID.search(urlparse(url).path)
        if not match:
            # Expand short share links through the same guarded transport.
            response = session.get(url, timeout=(10, 15))
            response.raise_for_status()
            match = _VIDEO_ID.search(urlparse(response.url).path)
        if not match:
            raise DouyinError('请粘贴抖音视频的完整分享链接。')
        video_id = match.group(1)
        webpage = f'https://www.iesdouyin.com/share/video/{video_id}/'
        response = session.get(webpage, timeout=(10, 15))
        response.raise_for_status()
        context, token = _share_context(response.text)
        response = session.get('https://www.iesdouyin.com/web/api/v2/aweme/iteminfo/',
            params={'item_ids': video_id, 'reflow_id': token, 'web_id': context['webId'],
                    'device_id': context['webId'], 'reflow_source': 'reflow_page',
                    'use_new_select_scope': 0},
            headers={'Agw-Js-Conv': 'str', 'Referer': webpage}, timeout=(10, 15))
        response.raise_for_status()
        if response.headers.get('bdturing-verify'):
            raise DouyinError('抖音要求人工验证，当前无法下载该视频，请稍后重试。')
        try:
            payload = response.json()
        except ValueError as exc:
            raise DouyinError('抖音暂时没有返回视频数据，请稍后重试。') from exc
        items = payload.get('item_list') or []
        item = next((entry for entry in items if str(entry.get('aweme_id')) == video_id), None)
        if payload.get('status_code') != 0 or not item:
            raise DouyinError('抖音暂时无法访问该视频，可能链接已失效或源站限制访问。')
        return _result(item, webpage)
