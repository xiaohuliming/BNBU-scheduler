"""Guarded browser API for the standalone SMS testing console."""

import hashlib
import hmac
import os
import re
import threading
import time
from functools import wraps

from flask import Blueprint, jsonify, request, session

from .client import HeroSMSClient, HeroSMSError


sms_lab_bp = Blueprint("sms_lab", __name__, url_prefix="/api/sms-lab")

_SERVICE_RE = re.compile(r"^[a-z0-9]{2,4}$")
_AUTH_SESSION_KEYS = (
    "sms_lab_authorized_at",
    "sms_lab_token_fingerprint",
)
_catalog_cache = {}
_cache_lock = threading.Lock()


def _env_bool(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name, default, minimum, maximum):
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return min(maximum, max(minimum, value))


def _env_int(name, default, minimum, maximum):
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return min(maximum, max(minimum, value))


def _csv_values(name):
    return {
        value.strip().lower()
        for value in os.getenv(name, "").split(",")
        if value.strip()
    }


def _service_allowlist():
    return {value for value in _csv_values("SMS_LAB_ALLOWED_SERVICES") if _SERVICE_RE.fullmatch(value)}


def _service_allowlist_is_configured():
    values = _csv_values("SMS_LAB_ALLOWED_SERVICES")
    return "*" in values or bool(_service_allowlist())


def _service_allowed(service):
    values = _csv_values("SMS_LAB_ALLOWED_SERVICES")
    return "*" in values or service in _service_allowlist()


def _country_allowlist():
    allowed = set()
    for value in _csv_values("SMS_LAB_ALLOWED_COUNTRIES"):
        try:
            country = int(value)
        except ValueError:
            continue
        if 0 <= country <= 999:
            allowed.add(country)
    return allowed


def _country_allowlist_is_configured():
    return bool(os.getenv("SMS_LAB_ALLOWED_COUNTRIES", "").strip())


def _access_token():
    return os.getenv("SMS_LAB_ACCESS_TOKEN", "").strip()


def _token_fingerprint(token):
    return hashlib.sha256(("sms-lab:" + token).encode("utf-8")).hexdigest()[:32]


def _max_price():
    return _env_float("SMS_LAB_MAX_PRICE", 2.0, 0.0067, 1000.0)


def _session_ttl_seconds():
    return _env_int("SMS_LAB_SESSION_TTL_MINUTES", 120, 5, 1440) * 60


def _is_unlocked():
    token = _access_token()
    if len(token) < 20:
        return False
    authorized_at = session.get("sms_lab_authorized_at")
    fingerprint = session.get("sms_lab_token_fingerprint")
    if not isinstance(authorized_at, (int, float)):
        return False
    if time.time() - authorized_at > _session_ttl_seconds():
        _clear_authorization()
        return False
    return hmac.compare_digest(str(fingerprint or ""), _token_fingerprint(token))


def _clear_authorization():
    for key in _AUTH_SESSION_KEYS:
        session.pop(key, None)


def _status_payload():
    provider_configured = bool(os.getenv("HERO_SMS_API_KEY", "").strip())
    access_configured = len(_access_token()) >= 20
    allowed_services = sorted(_service_allowlist())
    allowed_countries = sorted(_country_allowlist())
    allow_all_services = "*" in _csv_values("SMS_LAB_ALLOWED_SERVICES")
    purchase_switch = _env_bool("SMS_LAB_PURCHASES_ENABLED")
    return {
        "configured": provider_configured and access_configured,
        "provider_configured": provider_configured,
        "access_configured": access_configured,
        "unlocked": _is_unlocked(),
        "purchase_enabled": bool(
            provider_configured
            and access_configured
            and purchase_switch
            and _service_allowlist_is_configured()
        ),
        "purchase_switch": purchase_switch,
        "service_allowlist_configured": _service_allowlist_is_configured(),
        "allow_all_services": allow_all_services,
        "country_allowlist_configured": _country_allowlist_is_configured(),
        "allowed_service_count": len(allowed_services),
        "allowed_country_count": len(allowed_countries),
        "max_price": _max_price(),
        "session_ttl_minutes": _session_ttl_seconds() // 60,
    }


def _client():
    return HeroSMSClient(os.getenv("HERO_SMS_API_KEY", "").strip())


def _provider_cache_key(suffix):
    api_key = os.getenv("HERO_SMS_API_KEY", "").strip()
    digest = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12]
    return f"{digest}:{suffix}"


def _cached(key, ttl_seconds, loader):
    now = time.monotonic()
    with _cache_lock:
        cached = _catalog_cache.get(key)
        if cached and now - cached[0] < ttl_seconds:
            return cached[1]
    value = loader()
    with _cache_lock:
        _catalog_cache[key] = (now, value)
        if len(_catalog_cache) > 128:
            oldest = sorted(_catalog_cache, key=lambda item: _catalog_cache[item][0])[:32]
            for stale in oldest:
                _catalog_cache.pop(stale, None)
    return value


def _error_response(error):
    response = jsonify({"error": str(error), "code": error.code})
    response.status_code = error.status_code
    if error.status_code == 429:
        response.headers["Retry-After"] = "10"
    return response


def _authorized(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        status = _status_payload()
        if not status["configured"]:
            return jsonify({"error": "SMS Lab 尚未完成服务端配置。", "code": "not_configured"}), 503
        if not status["unlocked"]:
            return jsonify({"error": "请先输入访问码解锁。", "code": "locked"}), 401
        try:
            return view(*args, **kwargs)
        except HeroSMSError as error:
            return _error_response(error)

    return wrapped


def _owned_map():
    raw = session.get("sms_lab_owned_activations")
    if not isinstance(raw, dict):
        return {}
    cutoff = time.time() - (2 * 60 * 60)
    owned = {}
    for activation_id, created_at in raw.items():
        if not str(activation_id).isdigit():
            continue
        if isinstance(created_at, (int, float)) and created_at >= cutoff:
            owned[str(activation_id)] = created_at
    if owned != raw:
        session["sms_lab_owned_activations"] = owned
    return owned


def _remember_activations(items):
    owned = _owned_map()
    now = time.time()
    for item in items:
        activation_id = item.get("id") if isinstance(item, dict) else None
        if str(activation_id).isdigit():
            owned[str(activation_id)] = now
    if len(owned) > 10:
        owned = dict(sorted(owned.items(), key=lambda pair: pair[1], reverse=True)[:10])
    session["sms_lab_owned_activations"] = owned


def _forget_activation(activation_id):
    owned = _owned_map()
    owned.pop(str(activation_id), None)
    session["sms_lab_owned_activations"] = owned


def _require_owned(activation_id):
    if str(activation_id) not in _owned_map():
        return jsonify({"error": "该激活不属于当前浏览器会话。", "code": "not_owned"}), 404
    return None


def _sanitize_otp(item):
    if not isinstance(item, dict):
        return None
    return {
        "id": str(item.get("id", "")),
        "smsCode": str(item.get("smsCode", "")),
        "smsText": str(item.get("smsText", "")),
        "receivedAt": item.get("receivedAt"),
        "phoneFrom": str(item.get("phoneFrom", "")),
        "service": str(item.get("service", "")),
    }


def _sanitize_activation(item):
    if not isinstance(item, dict):
        return None
    try:
        activation_id = int(item.get("id"))
    except (TypeError, ValueError):
        return None
    otp_list = item.get("otpList") if isinstance(item.get("otpList"), list) else []
    return {
        "id": activation_id,
        "status": item.get("status"),
        "phone": str(item.get("phone", "")),
        "service": str(item.get("service", "")),
        "country": item.get("country"),
        "countryPhoneCode": item.get("countryPhoneCode"),
        "operator": str(item.get("operator", "")),
        "price": item.get("price"),
        "createdAt": item.get("createdAt"),
        "expiredAt": item.get("expiredAt"),
        "verificationType": item.get("verificationType"),
        "otpList": [otp for otp in (_sanitize_otp(value) for value in otp_list) if otp],
    }


@sms_lab_bp.get("/status")
def status():
    return jsonify(_status_payload())


@sms_lab_bp.post("/session")
def unlock():
    token = _access_token()
    if len(token) < 20 or not os.getenv("HERO_SMS_API_KEY", "").strip():
        return jsonify({"error": "SMS Lab 尚未完成服务端配置。", "code": "not_configured"}), 503
    data = request.get_json(silent=True)
    supplied = data.get("access_token") if isinstance(data, dict) else None
    if not isinstance(supplied, str) or len(supplied) > 512:
        return jsonify({"error": "访问码无效。", "code": "invalid_access_token"}), 401
    if not hmac.compare_digest(supplied, token):
        return jsonify({"error": "访问码无效。", "code": "invalid_access_token"}), 401
    session["sms_lab_authorized_at"] = time.time()
    session["sms_lab_token_fingerprint"] = _token_fingerprint(token)
    session.setdefault("sms_lab_owned_activations", {})
    return jsonify(_status_payload())


@sms_lab_bp.delete("/session")
def lock():
    # Keep activation ownership in the signed session so a user who locks and
    # reopens the console can still finish or cancel a number they purchased.
    _clear_authorization()
    return jsonify({"success": True})


@sms_lab_bp.get("/balance")
@_authorized
def balance():
    return jsonify({"balance": _client().get_balance(), "currency": "USD"})


@sms_lab_bp.get("/countries")
@_authorized
def countries():
    service = str(request.args.get("service", "")).strip().lower()
    if not _SERVICE_RE.fullmatch(service):
        return jsonify({"error": "请选择有效服务。", "code": "invalid_service"}), 400

    api_client = _client()
    values = _cached(
        _provider_cache_key("countries"),
        1800,
        lambda: api_client.get_countries(),
    )
    offers = _cached(
        _provider_cache_key(f"offers:{service}"),
        20,
        lambda: api_client.get_offers(service=service),
    )
    offer_data = offers.get("data") if isinstance(offers, dict) else {}
    if not isinstance(offer_data, dict):
        offer_data = {}
    service_offers = offer_data.get(service, {})
    if not isinstance(service_offers, dict):
        service_offers = {}
    allowed = _country_allowlist()
    result = []
    for item in values:
        if not isinstance(item, dict):
            continue
        try:
            country_id = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        if not item.get("visible", 1):
            continue
        country_offer = service_offers.get(str(country_id), {})
        prices = country_offer.get("prices") if isinstance(country_offer, dict) else {}
        counts = country_offer.get("counts") if isinstance(country_offer, dict) else {}
        prices = prices if isinstance(prices, dict) else {}
        counts = counts if isinstance(counts, dict) else {}
        raw_price = prices.get("retail", prices.get("default"))
        try:
            price = float(raw_price)
        except (TypeError, ValueError):
            price = None
        try:
            stock = int(counts.get("total", 0) or 0)
        except (TypeError, ValueError):
            stock = 0
        country_allowed = not _country_allowlist_is_configured() or country_id in allowed
        result.append({
            "id": country_id,
            "name": str(item.get("chn") or item.get("eng") or country_id),
            "name_en": str(item.get("eng") or ""),
            "allowed": _service_allowed(service) and country_allowed,
            "available": stock > 0 and price is not None,
            "stock": stock,
            "price": price,
            "min_price": prices.get("min"),
        })
    result.sort(key=lambda item: (
        not item["allowed"],
        not item["available"],
        -(item["stock"] or 0),
        item["name"],
    ))
    return jsonify({"countries": result, "service": service})


@sms_lab_bp.get("/services")
@_authorized
def services():
    api_client = _client()
    service_values = _cached(
        _provider_cache_key("services:all"),
        1800,
        lambda: api_client.get_services(),
    )
    result = []
    for item in service_values:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code", "")).lower()
        if not _SERVICE_RE.fullmatch(code):
            continue
        result.append({
            "code": code,
            "name": str(item.get("name") or code.upper()),
            "allowed": _service_allowed(code),
        })
    result.sort(key=lambda item: (
        not item["allowed"],
        item["name"].lower(),
    ))
    return jsonify({"services": result})


@sms_lab_bp.get("/activations")
@_authorized
def activations():
    owned = _owned_map()
    if not owned:
        return jsonify({"activations": []})
    payload = _client().list_activations()
    values = payload.get("data") if isinstance(payload, dict) else []
    result = []
    for item in values if isinstance(values, list) else []:
        if not isinstance(item, dict) or str(item.get("id")) not in owned:
            continue
        clean = _sanitize_activation(item)
        if clean:
            result.append(clean)
    return jsonify({"activations": result})


@sms_lab_bp.post("/activations")
@_authorized
def purchase_activation():
    status_payload = _status_payload()
    if not status_payload["purchase_enabled"]:
        return jsonify({
            "error": "购买功能尚未开放，请配置服务白名单并启用购买开关。",
            "code": "purchase_disabled",
        }), 503
    if len(_owned_map()) >= 3:
        return jsonify({"error": "当前会话最多同时保留 3 个激活。", "code": "active_limit"}), 409

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "请求格式无效。", "code": "invalid_payload"}), 400
    service = data.get("service")
    country = data.get("country")
    if not isinstance(service, str) or not _SERVICE_RE.fullmatch(service.lower()):
        return jsonify({"error": "请选择有效服务。", "code": "invalid_service"}), 400
    service = service.lower()
    if isinstance(country, bool) or not isinstance(country, int) or not 0 <= country <= 999:
        return jsonify({"error": "请选择有效国家。", "code": "invalid_country"}), 400
    if not _service_allowed(service):
        return jsonify({"error": "该服务不在管理员白名单中。", "code": "service_not_allowed"}), 403
    allowed_countries = _country_allowlist()
    if _country_allowlist_is_configured() and country not in allowed_countries:
        return jsonify({"error": "该国家不在管理员白名单中。", "code": "country_not_allowed"}), 403

    requested_price = data.get("max_price", _max_price())
    if isinstance(requested_price, bool) or not isinstance(requested_price, (int, float)):
        return jsonify({"error": "最高价格无效。", "code": "invalid_price"}), 400
    if requested_price < 0.0067 or requested_price > _max_price():
        return jsonify({
            "error": f"最高价格必须在 0.0067 到 {_max_price():.4f} USD 之间。",
            "code": "invalid_price",
        }), 400

    payload = _client().purchase(service, country, round(float(requested_price), 4))
    values = payload.get("data") if isinstance(payload, dict) else []
    if isinstance(values, dict):
        values = [values]
    clean = []
    for item in values if isinstance(values, list) else []:
        sanitized = _sanitize_activation(item)
        if sanitized:
            clean.append(sanitized)
    if not clean:
        raise HeroSMSError("HeroSMS 未返回新激活。", 502, "invalid_response")
    _remember_activations(clean)
    return jsonify({"activations": clean}), 201


@sms_lab_bp.delete("/activations/<int:activation_id>")
@_authorized
def cancel_activation(activation_id):
    denied = _require_owned(activation_id)
    if denied:
        return denied
    _client().cancel(activation_id)
    _forget_activation(activation_id)
    return jsonify({"success": True})


@sms_lab_bp.post("/activations/<int:activation_id>/finish")
@_authorized
def finish_activation(activation_id):
    denied = _require_owned(activation_id)
    if denied:
        return denied
    _client().finish(activation_id)
    _forget_activation(activation_id)
    return jsonify({"success": True})


@sms_lab_bp.post("/activations/<int:activation_id>/replace")
@_authorized
def replace_activation(activation_id):
    denied = _require_owned(activation_id)
    if denied:
        return denied
    payload = _client().replace(activation_id)
    values = payload.get("data") if isinstance(payload, dict) else []
    if isinstance(values, dict):
        values = [values]
    clean = []
    for item in values if isinstance(values, list) else []:
        sanitized = _sanitize_activation(item)
        if sanitized:
            clean.append(sanitized)
    if not clean:
        raise HeroSMSError("HeroSMS 未返回替换后的激活。", 502, "invalid_response")
    _forget_activation(activation_id)
    _remember_activations(clean)
    return jsonify({"activations": clean})
