"""Same-origin Cap proof verification and short-lived browser clearance.

The official Cap verifier runs on loopback. Only successful proof redemption
can issue clearance; widget events or client-provided redeem tokens cannot.
"""
import hashlib
import json
import os
import re
import secrets
import threading
import time
from urllib.parse import urlsplit

import requests
from flask import Blueprint, g, jsonify, make_response, request
from itsdangerous import BadSignature, URLSafeTimedSerializer

HUMAN_PATHS = frozenset(('/api/human/challenge', '/api/human/redeem', '/api/human/status'))
CLEARANCE_COOKIE = 'maxcourse_human'
CLIENT_COOKIE = 'maxcourse_cap_client'
CLEARANCE_SECONDS = 900


class HumanVerification:
    def __init__(self, app, client_ip, on_verified=None):
        self.app = app
        self.client_ip = client_ip
        self.on_verified = on_verified
        self.signer = URLSafeTimedSerializer(app.secret_key, salt='maxcourse-cap-v1')
        self.client_signer = URLSafeTimedSerializer(app.secret_key, salt='maxcourse-cap-client-v1')
        self._lock = threading.Lock()
        self._buckets = {}
        self.stats = {'challenges': 0, 'verified': 0, 'failed': 0}
        self.blueprint = Blueprint('human_verification', __name__)
        self.blueprint.add_url_rule('/api/human/challenge', 'challenge', self.challenge, methods=['POST'])
        self.blueprint.add_url_rule('/api/human/redeem', 'redeem', self.redeem, methods=['POST'])
        self.blueprint.add_url_rule('/api/human/status', 'status', self.status, methods=['GET'])
        self.blueprint.add_url_rule('/human-check/', 'page', self.page, methods=['GET'])
        app.register_blueprint(self.blueprint)

    def binding(self, create=False):
        identity = getattr(g, 'cap_client_identity', None)
        if identity is None:
            try:
                identity = self.client_signer.loads(request.cookies.get(CLIENT_COOKIE, ''), max_age=86400)
                if not isinstance(identity, str) or len(identity) != 32:
                    identity = None
            except (BadSignature, TypeError, ValueError):
                identity = None
        if not identity and create:
            identity = secrets.token_urlsafe(24)
            g.cap_client_fresh = True
        g.cap_client_identity = identity
        if not identity:
            return None
        payload = json.dumps([identity, self.client_ip(), request.headers.get('User-Agent', '')], separators=(',', ':'))
        return hashlib.sha256(payload.encode()).hexdigest()

    def verified(self):
        cookie = request.cookies.get(CLEARANCE_COOKIE, '')
        if not cookie or len(cookie) > 2048:
            return False
        try:
            grant = self.signer.loads(cookie, max_age=CLEARANCE_SECONDS)
            return isinstance(grant, dict) and grant.get('binding') == self.binding() and self.binding() is not None
        except (BadSignature, TypeError, ValueError):
            return False

    def local_host(self):
        return urlsplit('//' + request.host).hostname in ('localhost', '127.0.0.1', '::1')

    def valid_origin(self):
        try:
            origin = urlsplit(request.headers.get('Origin', ''))
            return (origin.netloc.lower() == request.host.lower()
                    and origin.scheme in (('http', 'https') if self.local_host() else ('https',))
                    and not origin.path and not origin.query and not origin.fragment
                    and request.headers.get('Sec-Fetch-Site') != 'cross-site')
        except ValueError:
            return False

    def limited(self, kind, limit):
        now = time.monotonic()
        binding = self.binding(create=kind == 'challenge')
        buckets = [(('ip', self.client_ip(), kind), limit * 10)]
        if binding:
            buckets.append((('browser', binding, kind), limit))
        with self._lock:
            if len(self._buckets) >= 10000:
                self._buckets = {k: v for k, v in self._buckets.items() if now - v[1] < 120}
            over = False
            for key, capacity in buckets:
                entry = self._buckets.get(key)
                if entry is None:
                    if len(self._buckets) >= 10000:
                        return True
                    self._buckets[key] = [capacity - 1.0, now]
                    continue
                tokens = min(capacity, entry[0] + max(0, now - entry[1]) * capacity / 60)
                allowed = tokens >= 1
                entry[:] = [tokens - 1 if allowed else tokens, now]
                over = over or not allowed
            return over

    def response(self, body, status=200):
        response = jsonify(body)
        response.status_code = status
        response.headers['Cache-Control'] = 'no-store'
        if getattr(g, 'cap_client_fresh', False):
            response.set_cookie(CLIENT_COOKIE, self.client_signer.dumps(g.cap_client_identity),
                                max_age=86400, httponly=True,
                                secure=not self.local_host() or request.is_secure, samesite='Lax', path='/')
        return response

    def preflight(self, kind, limit):
        if not self.valid_origin():
            return self.response({'success': False, 'error': '验证请求来源不正确，请从本站页面重试。'}, 403)
        if self.limited(kind, limit):
            response = self.response({'success': False, 'error': '验证请求较多，请稍后再试。'}, 429)
            response.headers['Retry-After'] = '10'
            return response
        if ((request.content_length or 0) > 16384
                or kind == 'redeem' and (not request.is_json or request.content_length is None)):
            return self.response({'success': False, 'error': '验证数据格式不正确。'}, 400)
        return None

    def bridge(self, endpoint, payload):
        secret = self.app.config.get('MAXCOURSE_CAP_SECRET') or os.getenv('MAXCOURSE_CAP_SECRET', '')
        address = self.app.config.get('MAXCOURSE_CAP_URL') or os.getenv('MAXCOURSE_CAP_URL', 'http://127.0.0.1:5068')
        # Configuration must never turn this internal verifier into an outbound
        # URL proxy or transmit its bridge credential to a third-party host.
        parsed = urlsplit(address)
        if len(secret) < 32 or parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or parsed.username or parsed.path not in ('', '/'):
            raise RuntimeError('Cap service is not configured')
        with requests.Session() as transport:
            transport.trust_env = False
            response = transport.post(address.rstrip('/') + endpoint, json=payload,
                headers={'X-Cap-Service-Secret': secret}, timeout=(2, 8), allow_redirects=False)
        if response.status_code not in (200, 400):
            raise RuntimeError('Cap service unavailable')
        result = response.json()
        if not isinstance(result, dict):
            raise RuntimeError('Invalid Cap service response')
        return result

    def challenge(self):
        rejected = self.preflight('challenge', 10)
        if rejected is not None:
            return rejected
        try:
            result = self.bridge('/challenge', {'scope': self.binding(create=True)})
            if not all(key in result for key in ('challenge', 'token', 'expires')):
                raise RuntimeError('Invalid challenge')
        except (requests.RequestException, ValueError, RuntimeError):
            return self.response({'success': False, 'error': '验证服务暂时不可用，请稍后重试。'}, 503)
        with self._lock:
            self.stats['challenges'] += 1
        return self.response(result)

    def redeem(self):
        rejected = self.preflight('redeem', 20)
        if rejected is not None:
            return rejected
        proof = request.get_json(silent=True)
        if (not self.binding() or not isinstance(proof, dict)
                or not isinstance(proof.get('token'), str) or len(proof['token']) > 8192
                or not isinstance(proof.get('solutions'), list) or len(proof['solutions']) > 100
                or not all(type(value) is int and 0 <= value <= 2**53 - 1 for value in proof['solutions'])):
            return self.response({'success': False, 'error': '验证已失效，请重新验证。'}, 400)
        try:
            result = self.bridge('/redeem', {'scope': self.binding(), 'proof': proof})
        except (requests.RequestException, ValueError, RuntimeError):
            return self.response({'success': False, 'error': '验证服务暂时不可用，请稍后重试。'}, 503)
        if result.get('success') is not True or not isinstance(result.get('token'), str):
            with self._lock:
                self.stats['failed'] += 1
            return self.response({'success': False, 'error': '验证未通过或已过期，请重新验证。'}, 400)
        with self._lock:
            self.stats['verified'] += 1
        if self.on_verified:
            self.on_verified()
        response = self.response({'success': True, 'token': result['token'],
                                  'expires': int(time.time() * 1000) + CLEARANCE_SECONDS * 1000})
        response.set_cookie(CLEARANCE_COOKIE, self.signer.dumps({'binding': self.binding()}),
                            max_age=CLEARANCE_SECONDS, httponly=True,
                            secure=not self.local_host() or request.is_secure, samesite='Lax', path='/')
        return response

    def status(self):
        if self.limited('status', 60):
            return self.response({'verified': False, 'error': '请稍后再试。'}, 429)
        return self.response({'verified': self.verified()})

    def required(self, reason, status=403, retry_after=None):
        download_frame = request.path.startswith('/api/media-dl/') and re.fullmatch(
            r'[a-zA-Z0-9-]{16,80}', request.args.get('feedback', ''))
        navigation = (request.accept_mimetypes.best == 'text/html'
                      or request.headers.get('Sec-Fetch-Mode') == 'navigate'
                      or request.headers.get('Sec-Fetch-Dest') in ('document', 'iframe'))
        if request.method in ('GET', 'HEAD') and (navigation or download_frame):
            response = self.page(next_path=request.full_path.rstrip('?'))
            response.status_code = status
        else:
            response = self.response({
                'error': '请完成人机验证后继续访问。',
                'code': 'human_verification_required', 'reason': reason,
                'challenge_url': '/human-check/', 'retry_after': retry_after,
            }, status)
        response.headers['X-Maxcourse-Challenge'] = 'required'
        response.headers['Cache-Control'] = 'no-store'
        if retry_after:
            response.headers['Retry-After'] = str(retry_after)
        return response

    def page(self, next_path=None):
        target = next_path or request.args.get('next', '/')
        if not target.startswith('/') or target.startswith('//') or '\\' in target or any(ord(c) < 32 for c in target):
            target = '/'
        serialized = json.dumps(target).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
        html = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>访问验证 · MAXCOURSE</title>
<link rel="stylesheet" href="/vendor/fonts.css"><script>window.MAXCOURSE_VERIFY_NEXT={serialized};</script>
<script src="/human-check/client.js?v=1" defer></script></head>
<body style="margin:0;padding:32px;background:#f4efe6;color:#101820;font-family:system-ui">
<h1>MAXCOURSE</h1><p>完成验证后即可继续刚才的访问。</p>
<noscript>请启用 JavaScript 后刷新页面以完成验证。</noscript>
<button onclick="window.MAXCOURSE_HUMAN_CHECK?.()" style="padding:12px">开始验证</button>
</body></html>'''
        response = make_response(html)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Content-Security-Policy'] = "frame-ancestors 'self'"
        return response


def inject_client(response):
    """Load before page scripts on all Flask-served HTML, including tool pages."""
    if response.status_code != 200 or response.mimetype != 'text/html' or request.method != 'GET':
        return response
    response.direct_passthrough = False
    html = response.get_data(as_text=True)
    if '</head>' not in html or '/human-check/client.js' in html:
        return response
    # Early synchronous loading ensures inline tracking and initial data fetches
    # use the same challenge-aware fetch as later user actions.
    html = html.replace('<head>', '<head><script src="/human-check/client.js?v=1"></script>', 1)
    response.set_data(html)
    response.headers.pop('ETag', None)
    return response
