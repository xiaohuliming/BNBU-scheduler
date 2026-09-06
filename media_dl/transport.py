"""Keep extractor HTTP headers server-side for subsequent media transfers."""
from collections import OrderedDict
import threading
import time

_lock = threading.Lock()
_headers = OrderedDict()
_TTL = 2 * 60 * 60
_LIMIT = 1024
_ALLOWED = {'user-agent', 'referer', 'origin', 'accept', 'accept-language', 'cookie'}


class MediaDownloadError(RuntimeError):
    """An actionable, non-sensitive message that may be shown to the user."""


def remember_headers(url, headers):
    safe = {str(k): str(v) for k, v in headers.items()
            if str(k).lower() in _ALLOWED and len(str(v)) <= 8192
            and '\r' not in str(v) and '\n' not in str(v)}
    if not safe:
        return
    with _lock:
        _headers[url] = (time.monotonic() + _TTL, safe)
        _headers.move_to_end(url)
        while len(_headers) > _LIMIT:
            _headers.popitem(last=False)


def headers_for(url):
    with _lock:
        entry = _headers.get(url)
        if not entry:
            return {}
        if entry[0] < time.monotonic():
            del _headers[url]
            return {}
        return dict(entry[1])
