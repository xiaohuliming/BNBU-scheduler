"""HTTP transport for untrusted media URLs. Pin each request to a public IP.

The original hostname is kept for Host, TLS SNI and certificate verification.
Redirects run through the adapter again, including redirects followed by yt-dlp.
No process-wide DNS or socket monkeypatches are used.
"""
from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlsplit, urlunsplit

import requests as _requests

RequestException = _requests.RequestException
Response = _requests.Response


class UnsafeURLError(ValueError):
    pass


def _public_ip(value: str) -> bool:
    address = ipaddress.ip_address(value)
    if not address.is_global or address.is_multicast or address.is_reserved:
        return False
    if isinstance(address, ipaddress.IPv4Address) and address in ipaddress.ip_network('192.0.0.0/24'):
        return False
    if isinstance(address, ipaddress.IPv6Address):
        if address in ipaddress.ip_network('64:ff9b::/96') or address in ipaddress.ip_network('64:ff9b:1::/48'):
            return False
        if address.ipv4_mapped and not _public_ip(str(address.ipv4_mapped)):
            return False
        if address.sixtofour and not _public_ip(str(address.sixtofour)):
            return False
        if address.teredo:
            return False
    return True


def validate_url(url: str):
    if not isinstance(url, str) or len(url) > 16384:
        raise UnsafeURLError('链接格式无效或过长。')
    if re.search(r'[\x00-\x20\x7f\\]', url):
        raise UnsafeURLError('链接中包含无效字符。')
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or '').rstrip('.').lower()
        port = parsed.port
    except ValueError as exc:
        raise UnsafeURLError('链接格式无效。') from exc
    if parsed.scheme not in ('http', 'https') or not host:
        raise UnsafeURLError('请提供有效的 http 或 https 网页链接。')
    if parsed.username is not None or parsed.password is not None or '%' in host:
        raise UnsafeURLError('不支持包含登录信息的链接。')
    if port is not None and port not in (80, 443):
        raise UnsafeURLError('不支持该链接端口。')
    if host == 'localhost' or host.endswith(('.localhost', '.local', '.internal', '.home', '.lan')):
        raise UnsafeURLError('不允许访问本机或内网地址。')
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        # Reject alternate integer/octal/hex IPv4 syntax, single-label names,
        # and malformed names before handing a URL to any extractor.
        if '.' not in host or re.fullmatch(r'(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+))*', host):
            raise UnsafeURLError('请提供有效的公网域名。')
        try:
            ascii_host = host.encode('idna').decode('ascii')
        except UnicodeError as exc:
            raise UnsafeURLError('域名格式无效。') from exc
        if not re.fullmatch(r'[a-z0-9-]+(?:\.[a-z0-9-]+)+', ascii_host):
            raise UnsafeURLError('域名格式无效。')
    else:
        if not _public_ip(str(literal)):
            raise UnsafeURLError('不允许访问本机或内网地址。')
    return parsed


def public_addresses(host: str, port: int) -> list[str]:
    addresses = list(dict.fromkeys(
        entry[4][0] for entry in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    ))
    if not addresses or any(not _public_ip(ip) for ip in addresses):
        raise UnsafeURLError('不允许访问本机、内网或保留地址。')
    # Prefer IPv4 because several production hosts have no IPv6 route.
    return sorted(addresses, key=lambda ip: ':' in ip)


class PublicHTTPAdapter(_requests.adapters.HTTPAdapter):
    def build_connection_pool_key_attributes(self, request, verify, cert=None):
        host_params, pool_kwargs = super().build_connection_pool_key_attributes(request, verify, cert)
        if host_params['scheme'] == 'https':
            hostname = request._media_hostname
            pool_kwargs.update(assert_hostname=hostname, server_hostname=hostname)
        return host_params, pool_kwargs

    def send(self, request, **kwargs):
        parsed = validate_url(request.url)
        host = parsed.hostname.rstrip('.').encode('idna').decode('ascii')
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        address = public_addresses(host, port)[0]
        pinned = request.copy()
        literal = f'[{address}]' if ':' in address else address
        pinned.url = urlunsplit((parsed.scheme, f'{literal}:{port}', parsed.path, parsed.query, ''))
        pinned._media_hostname = host
        authority = f'[{host}]' if ':' in host else host
        if parsed.port:
            authority += f':{parsed.port}'
        pinned.headers['Host'] = authority
        # Transport callers cannot disable certificate validation.
        kwargs['verify'] = kwargs.get('verify') or True
        response = super().send(pinned, **kwargs)
        # Cookie scope and relative redirects must use the original hostname.
        response.url = request.url
        response.request = request
        return response


class Session(_requests.Session):
    def __init__(self):
        super().__init__()
        self.trust_env = False
        self.max_redirects = 8
        adapter = PublicHTTPAdapter()
        self.mount('https://', adapter)
        self.mount('http://', adapter)


def request(method: str, url: str, **kwargs):
    session = Session()
    try:
        response = session.request(method, url, **kwargs)
    except BaseException:
        session.close()
        raise
    if not kwargs.get('stream'):
        session.close()
    else:
        close = response.close

        def close_all():
            try:
                close()
            finally:
                session.close()

        response.close = close_all
    return response


def get(url: str, **kwargs):
    return request('GET', url, **kwargs)


def post(url: str, **kwargs):
    return request('POST', url, **kwargs)
