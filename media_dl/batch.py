"""Stream selected media as one ZIP without browser multiple-download prompts."""
import io
import re
import secrets
import threading
import time
import zipfile

from . import http
from .transport import MediaDownloadError, headers_for

_jobs = {}
_lock = threading.Lock()
_slots = threading.BoundedSemaphore(3)
_TTL = 300


def prepare(items, owner):
    now = time.monotonic()
    with _lock:
        for key in list(_jobs):
            if _jobs[key][0] < now:
                del _jobs[key]
        if len(_jobs) >= 64:
            raise MediaDownloadError('打包任务较多，请稍后重试。')
        token = secrets.token_urlsafe(24)
        _jobs[token] = (now + _TTL, owner, items)
        return token


def take(token, owner):
    with _lock:
        entry = _jobs.get(token)
        if not entry or entry[0] < time.monotonic() or entry[1] != owner:
            return None
        del _jobs[token]
        return entry[2]


class _Sink:
    def __init__(self):
        self.offset = 0
        self.pending = bytearray()

    def tell(self):
        return self.offset

    def seek(self, *args):
        raise io.UnsupportedOperation('streaming ZIP')

    def write(self, data):
        self.pending.extend(data)
        self.offset += len(data)
        return len(data)

    def flush(self):
        pass

    def drain(self):
        data = bytes(self.pending)
        self.pending.clear()
        return data


def _filename(value, index, used):
    name = re.sub(r'[\x00-\x1f\x7f\\/:]+', '_', value).strip('. ')[:140] or f'media-{index}.bin'
    original = name
    suffix = 2
    while name.casefold() in used:
        stem, dot, ext = original.rpartition('.')
        name = f'{stem} ({suffix}).{ext}' if dot else f'{original} ({suffix})'
        suffix += 1
    used.add(name.casefold())
    return name


def archive(items, proxies_for, user_agent):
    if not _slots.acquire(blocking=False):
        raise MediaDownloadError('打包任务较多，请稍后重试。')
    started = time.monotonic()
    sink, names, total = _Sink(), set(), 0
    try:
        with zipfile.ZipFile(sink, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as bundle:
            for index, item in enumerate(items, 1):
                url = item['url']
                headers = {'User-Agent': user_agent, **headers_for(url), 'Accept-Encoding': 'identity'}
                if item.get('referer') and 'Referer' not in headers:
                    headers['Referer'] = item['referer']
                with http.get(url, headers=headers, proxies=proxies_for(url),
                              stream=True, timeout=(10, 30)) as response:
                    response.raise_for_status()
                    name = _filename(item['filename'], index, names)
                    with bundle.open(name, 'w', force_zip64=True) as destination:
                        for chunk in response.iter_content(64 * 1024):
                            total += len(chunk)
                            if total > 2 * 1024 ** 3 or time.monotonic() - started > 900:
                                raise MediaDownloadError('选中素材过大，请减少选择后分批下载。')
                            destination.write(chunk)
                            yield sink.drain()
                    yield sink.drain()
        yield sink.drain()
    finally:
        _slots.release()
