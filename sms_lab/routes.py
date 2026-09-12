"""Public SMS reseller API backed by one guarded HeroSMS provider account."""

import hashlib
import hmac
import os
import re
import sqlite3
import threading
import time
from functools import wraps

from flask import Blueprint, jsonify, request, session

from .client import HeroSMSClient, HeroSMSError
from .storage import ACTIVE_ORDER_STATUSES, amount_to_units, sale_units_for_cost, units_to_amount


_SERVICE_RE = re.compile(r"^[a-z0-9]{2,4}$")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9_-]{8,100}$")
_catalog_cache = {}
_cache_lock = threading.Lock()

_POPULAR_SERVICE_CODES = (
    "tg", "wa", "go", "fb", "ig", "lf", "tw", "ds", "dr", "am", "wx", "tn",
)

# HeroSMS forbids disposable-number use in banking and paid subscriptions. A
# public reseller needs a stricter default than the private operator console.
_BLOCKED_SERVICE_TERMS = (
    "bank", "banco", "banka", "банк", "银行", "kredi", "loan", "credit",
    "paypal", "venmo", "revolut", "wise", "cash app", "cashapp", "wallet",
    "binance", "coinbase", "crypto", "bybit", "kucoin", "okx", "bitget",
    "netflix", "spotify", "subscription", "onlyfans",
)


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


def _markup_percent():
    return _env_int("SMS_RESELLER_MARKUP_PERCENT", 50, 0, 500)


def _logo_url(code):
    return f"https://cdn.hero-sms.com/assets/img/service/{code}0.webp"


def _service_resellable(code, name):
    code = str(code or "").lower()
    label = str(name or "").lower()
    if code in _csv_values("SMS_RESELLER_BLOCKED_SERVICE_CODES"):
        return False
    extra_terms = tuple(_csv_values("SMS_RESELLER_BLOCKED_SERVICE_TERMS"))
    if any(term in label for term in _BLOCKED_SERVICE_TERMS + extra_terms):
        return False
    allowlist = _csv_values("SMS_RESELLER_ALLOWED_SERVICES")
    return not allowlist or "*" in allowlist or code in allowlist


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


def _provider_error_response(error):
    response = jsonify({"error": str(error), "code": error.code})
    response.status_code = error.status_code
    if error.status_code == 429:
        response.headers["Retry-After"] = "10"
    return response


def _provider_guard(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not os.getenv("HERO_SMS_API_KEY", "").strip():
            return jsonify({"error": "号码服务尚未配置。", "code": "not_configured"}), 503
        try:
            return view(*args, **kwargs)
        except HeroSMSError as error:
            return _provider_error_response(error)

    return wrapped


def _require_user(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user_id = session.get("user_id")
        if not isinstance(user_id, int):
            return jsonify({"error": "请先登录后继续。", "code": "login_required"}), 401
        return view(user_id, *args, **kwargs)

    return wrapped


def _open_db(db_path_getter):
    conn = sqlite3.connect(db_path_getter(), timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def _service_catalog():
    values = _cached(
        _provider_cache_key("services:all"),
        1800,
        lambda: _client().get_services(),
    )
    result = []
    for item in values:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code", "")).lower()
        name = str(item.get("name") or code.upper()).strip()
        if not _SERVICE_RE.fullmatch(code) or not _service_resellable(code, name):
            continue
        result.append({"code": code, "name": name, "logo_url": _logo_url(code)})
    popular = {code: index for index, code in enumerate(_POPULAR_SERVICE_CODES)}
    result.sort(key=lambda item: (
        item["code"] not in popular,
        popular.get(item["code"], 999),
        item["name"].lower(),
    ))
    return result


def _country_catalog():
    values = _cached(
        _provider_cache_key("countries"),
        1800,
        lambda: _client().get_countries(),
    )
    result = []
    for item in values:
        if not isinstance(item, dict) or not item.get("visible", 1):
            continue
        try:
            country_id = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        result.append({
            "id": country_id,
            "name": str(item.get("chn") or item.get("eng") or country_id),
            "name_en": str(item.get("eng") or ""),
        })
    return result


def _find_service(code):
    return next((item for item in _service_catalog() if item["code"] == code), None)


def _offer_for(service, country, *, fresh=False):
    if fresh:
        offers = _client().get_offers(service=service)
    else:
        offers = _cached(
            _provider_cache_key(f"offers:{service}"),
            15,
            lambda: _client().get_offers(service=service),
        )
    data = offers.get("data") if isinstance(offers, dict) else {}
    service_offers = data.get(service, {}) if isinstance(data, dict) else {}
    offer = service_offers.get(str(country), {}) if isinstance(service_offers, dict) else {}
    prices = offer.get("prices") if isinstance(offer, dict) else {}
    counts = offer.get("counts") if isinstance(offer, dict) else {}
    prices = prices if isinstance(prices, dict) else {}
    counts = counts if isinstance(counts, dict) else {}
    raw_price = prices.get("retail", prices.get("default"))
    try:
        cost_units = amount_to_units(raw_price)
        stock = int(counts.get("total", 0) or 0)
    except (TypeError, ValueError):
        return None
    if cost_units <= 0 or stock <= 0:
        return None
    return {"cost_units": cost_units, "stock": stock}


def _sanitize_otp(item):
    if not isinstance(item, dict):
        return None
    return {
        "id": str(item.get("id", "")),
        "smsCode": str(item.get("smsCode", "")),
        "smsText": str(item.get("smsText", "")),
        "receivedAt": item.get("receivedAt"),
        "phoneFrom": str(item.get("phoneFrom", "")),
    }


def _provider_activation(item):
    if not isinstance(item, dict):
        return None
    try:
        activation_id = str(int(item.get("id")))
    except (TypeError, ValueError):
        return None
    otp_values = item.get("otpList") if isinstance(item.get("otpList"), list) else []
    return {
        "id": activation_id,
        "status": item.get("status"),
        "phone": str(item.get("phone", "")),
        "price": item.get("price"),
        "createdAt": item.get("createdAt"),
        "expiredAt": item.get("expiredAt"),
        "operator": str(item.get("operator", "")),
        "otpList": [otp for otp in (_sanitize_otp(value) for value in otp_values) if otp],
    }


def _row_time(value):
    if not value:
        return None
    text = str(value)
    return text.replace(" ", "T") + ("Z" if "T" in text and not text.endswith("Z") else "")


def _order_payload(row, provider=None):
    status = row["status"]
    otp_list = provider.get("otpList", []) if provider else []
    if otp_list and status in ACTIVE_ORDER_STATUSES:
        status = "code_received"
    return {
        "id": row["id"],
        "service": {
            "code": row["service_code"],
            "name": row["service_name"],
            "logo_url": _logo_url(row["service_code"]),
        },
        "country": {"id": row["country_id"], "name": row["country_name"]},
        "phone": provider.get("phone") if provider else (row["phone"] or ""),
        "operator": provider.get("operator") if provider else "",
        "sale_price": units_to_amount(row["sale_price_units"]),
        "refunded": units_to_amount(row["refunded_units"]),
        "status": status,
        "provider_status": provider.get("status") if provider else row["provider_status"],
        "createdAt": provider.get("createdAt") if provider else _row_time(row["created_at"]),
        "expiredAt": provider.get("expiredAt") if provider else row["expires_at"],
        "otpList": otp_list,
        "can_cancel": status == "active" and not otp_list,
        "can_finish": status == "code_received",
        "can_replace": status == "active" and not otp_list,
    }


def _refund_order(db_path_getter, order_id, status, note):
    conn = _open_db(db_path_getter)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM sms_orders WHERE id = ?", (order_id,)).fetchone()
        if not row:
            conn.rollback()
            return None
        if row["refunded_units"]:
            conn.execute(
                "UPDATE sms_orders SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (status, order_id),
            )
            conn.commit()
            return row["refunded_units"]
        amount = row["sale_price_units"]
        conn.execute(
            "UPDATE users SET sms_wallet_units = sms_wallet_units + ? WHERE id = ?",
            (amount, row["user_id"]),
        )
        balance = conn.execute(
            "SELECT sms_wallet_units FROM users WHERE id = ?", (row["user_id"],)
        ).fetchone()[0]
        conn.execute(
            """
            UPDATE sms_orders
            SET status = ?, refunded_units = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (status, amount, order_id),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO sms_wallet_ledger
            (user_id, order_id, amount_units, balance_after_units, kind, reference, note)
            VALUES (?, ?, ?, ?, 'refund', ?, ?)
            """,
            (row["user_id"], order_id, amount, balance, f"order:{order_id}:refund", note),
        )
        conn.commit()
        return amount
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _admin_token_valid(value):
    expected = os.getenv("SMS_LAB_ACCESS_TOKEN", "").strip()
    return len(expected) >= 20 and isinstance(value, str) and hmac.compare_digest(value, expected)


def create_sms_lab_blueprint(db_path_getter):
    bp = Blueprint("sms_lab", __name__, url_prefix="/api/sms-lab")

    @bp.get("/status")
    def status():
        return jsonify({
            "configured": bool(os.getenv("HERO_SMS_API_KEY", "").strip()),
            "reseller_mode": True,
            "currency": "USD",
            "manual_recharge": True,
        })

    @bp.get("/services")
    @_provider_guard
    def services():
        values = _service_catalog()
        return jsonify({"services": values, "count": len(values)})

    @bp.get("/countries")
    @_provider_guard
    def countries():
        service = str(request.args.get("service", "")).strip().lower()
        service_item = _find_service(service) if _SERVICE_RE.fullmatch(service) else None
        if not service_item:
            return jsonify({"error": "请选择可转售的服务。", "code": "invalid_service"}), 400
        offers = _cached(
            _provider_cache_key(f"offers:{service}"),
            15,
            lambda: _client().get_offers(service=service),
        )
        data = offers.get("data") if isinstance(offers, dict) else {}
        service_offers = data.get(service, {}) if isinstance(data, dict) else {}
        service_offers = service_offers if isinstance(service_offers, dict) else {}
        markup = _markup_percent()
        result = []
        for country in _country_catalog():
            raw = service_offers.get(str(country["id"]), {})
            prices = raw.get("prices") if isinstance(raw, dict) else {}
            counts = raw.get("counts") if isinstance(raw, dict) else {}
            prices = prices if isinstance(prices, dict) else {}
            counts = counts if isinstance(counts, dict) else {}
            try:
                cost_units = amount_to_units(prices.get("retail", prices.get("default")))
                stock = int(counts.get("total", 0) or 0)
            except (TypeError, ValueError):
                continue
            if cost_units <= 0 or stock <= 0:
                continue
            result.append({
                **country,
                "stock": stock,
                "price": units_to_amount(sale_units_for_cost(cost_units, markup)),
            })
        result.sort(key=lambda item: (item["price"], -item["stock"], item["name"]))
        return jsonify({
            "service": service_item,
            "countries": result,
            "currency": "USD",
        })

    @bp.get("/account")
    def account():
        user_id = session.get("user_id")
        if not isinstance(user_id, int):
            return jsonify({"authenticated": False, "currency": "USD"})
        conn = _open_db(db_path_getter)
        try:
            user = conn.execute(
                "SELECT id, username, display_name, sms_wallet_units FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
            if not user:
                return jsonify({"authenticated": False, "currency": "USD"})
            ledger = conn.execute(
                """
                SELECT amount_units, balance_after_units, kind, reference, note, created_at
                FROM sms_wallet_ledger WHERE user_id = ? ORDER BY id DESC LIMIT 12
                """,
                (user_id,),
            ).fetchall()
            return jsonify({
                "authenticated": True,
                "user": {
                    "id": user["id"],
                    "username": user["username"],
                    "display_name": user["display_name"] or user["username"],
                },
                "wallet": {
                    "balance": units_to_amount(user["sms_wallet_units"]),
                    "currency": "USD",
                },
                "ledger": [{
                    "amount": units_to_amount(row["amount_units"]),
                    "balance_after": units_to_amount(row["balance_after_units"]),
                    "kind": row["kind"],
                    "reference": row["reference"],
                    "note": row["note"] or "",
                    "createdAt": _row_time(row["created_at"]),
                } for row in ledger],
            })
        finally:
            conn.close()

    @bp.get("/orders")
    @_require_user
    @_provider_guard
    def orders(user_id):
        conn = _open_db(db_path_getter)
        try:
            rows = conn.execute(
                "SELECT * FROM sms_orders WHERE user_id = ? ORDER BY id DESC LIMIT 50",
                (user_id,),
            ).fetchall()
        finally:
            conn.close()

        active_ids = {
            row["provider_activation_id"]
            for row in rows
            if row["status"] in ACTIVE_ORDER_STATUSES and row["provider_activation_id"]
        }
        provider_map = {}
        if active_ids:
            payload = _client().list_activations()
            values = payload.get("data") if isinstance(payload, dict) else []
            for value in values if isinstance(values, list) else []:
                activation = _provider_activation(value)
                if activation and activation["id"] in active_ids:
                    provider_map[activation["id"]] = activation

        if provider_map:
            conn = _open_db(db_path_getter)
            try:
                for row in rows:
                    provider = provider_map.get(row["provider_activation_id"])
                    if not provider:
                        continue
                    new_status = "code_received" if provider["otpList"] else "active"
                    conn.execute(
                        """
                        UPDATE sms_orders
                        SET phone = ?, provider_status = ?, status = ?, expires_at = ?,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = ? AND user_id = ?
                        """,
                        (
                            provider["phone"], provider["status"], new_status,
                            provider["expiredAt"], row["id"], user_id,
                        ),
                    )
                conn.commit()
                rows = conn.execute(
                    "SELECT * FROM sms_orders WHERE user_id = ? ORDER BY id DESC LIMIT 50",
                    (user_id,),
                ).fetchall()
            finally:
                conn.close()

        return jsonify({
            "orders": [
                _order_payload(row, provider_map.get(row["provider_activation_id"]))
                for row in rows
            ]
        })

    @bp.post("/orders")
    @_require_user
    @_provider_guard
    def purchase_order(user_id):
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "请求格式无效。", "code": "invalid_payload"}), 400
        service = str(data.get("service", "")).strip().lower()
        country = data.get("country")
        idempotency_key = str(data.get("idempotency_key", "")).strip()
        if not _SERVICE_RE.fullmatch(service) or not _find_service(service):
            return jsonify({"error": "请选择可转售的服务。", "code": "invalid_service"}), 400
        if isinstance(country, bool) or not isinstance(country, int) or not 0 <= country <= 999:
            return jsonify({"error": "请选择有效国家。", "code": "invalid_country"}), 400
        if not _IDEMPOTENCY_RE.fullmatch(idempotency_key):
            return jsonify({"error": "订单标识无效。", "code": "invalid_idempotency_key"}), 400

        conn = _open_db(db_path_getter)
        try:
            existing = conn.execute(
                "SELECT * FROM sms_orders WHERE user_id = ? AND idempotency_key = ?",
                (user_id, idempotency_key),
            ).fetchone()
            if existing:
                return jsonify({"order": _order_payload(existing), "idempotent": True})
        finally:
            conn.close()

        service_item = _find_service(service)
        country_item = next((item for item in _country_catalog() if item["id"] == country), None)
        quote = _offer_for(service, country, fresh=True)
        if not country_item or not quote:
            return jsonify({"error": "该国家当前没有可用号码。", "code": "out_of_stock"}), 409
        cost_units = quote["cost_units"]
        sale_units = sale_units_for_cost(cost_units, _markup_percent())

        conn = _open_db(db_path_getter)
        try:
            conn.execute("BEGIN IMMEDIATE")
            active_count = conn.execute(
                """
                SELECT COUNT(*) FROM sms_orders
                WHERE user_id = ? AND status IN ('purchasing', 'active', 'code_received')
                """,
                (user_id,),
            ).fetchone()[0]
            if active_count >= 3:
                conn.rollback()
                return jsonify({"error": "最多同时保留 3 个进行中订单。", "code": "active_limit"}), 409
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO sms_orders
                    (user_id, idempotency_key, service_code, service_name, country_id,
                     country_name, provider_cost_units, sale_price_units, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'purchasing')
                    """,
                    (
                        user_id, idempotency_key, service, service_item["name"], country,
                        country_item["name"], cost_units, sale_units,
                    ),
                )
            except sqlite3.IntegrityError:
                conn.rollback()
                return jsonify({"error": "订单标识已被使用。", "code": "duplicate_order"}), 409
            order_id = cursor.lastrowid
            debit = conn.execute(
                """
                UPDATE users SET sms_wallet_units = sms_wallet_units - ?
                WHERE id = ? AND sms_wallet_units >= ?
                """,
                (sale_units, user_id, sale_units),
            )
            if debit.rowcount != 1:
                conn.rollback()
                return jsonify({
                    "error": "钱包余额不足，请联系管理员充值。",
                    "code": "insufficient_wallet_balance",
                }), 402
            balance = conn.execute(
                "SELECT sms_wallet_units FROM users WHERE id = ?", (user_id,)
            ).fetchone()[0]
            conn.execute(
                """
                INSERT INTO sms_wallet_ledger
                (user_id, order_id, amount_units, balance_after_units, kind, reference, note)
                VALUES (?, ?, ?, ?, 'purchase', ?, ?)
                """,
                (
                    user_id, order_id, -sale_units, balance,
                    f"order:{order_id}:purchase", f"{service_item['name']} · {country_item['name']}",
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        try:
            payload = _client().purchase(
                service,
                country,
                units_to_amount(cost_units),
                reseller_user_id=str(user_id),
            )
            values = payload.get("data") if isinstance(payload, dict) else []
            if isinstance(values, dict):
                values = [values]
            provider = next(
                (item for item in (_provider_activation(value) for value in values or []) if item),
                None,
            )
            if not provider:
                raise HeroSMSError("号码供应商未返回新订单。", 502, "invalid_response")
        except HeroSMSError:
            _refund_order(db_path_getter, order_id, "failed", "上游购买失败自动退款")
            raise

        try:
            actual_cost_units = amount_to_units(provider.get("price"))
        except ValueError:
            actual_cost_units = cost_units
        conn = _open_db(db_path_getter)
        try:
            conn.execute(
                """
                UPDATE sms_orders
                SET provider_activation_id = ?, phone = ?, provider_cost_units = ?,
                    provider_status = ?, status = ?, expires_at = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND user_id = ?
                """,
                (
                    provider["id"], provider["phone"], actual_cost_units,
                    provider["status"], "code_received" if provider["otpList"] else "active",
                    provider["expiredAt"], order_id, user_id,
                ),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM sms_orders WHERE id = ?", (order_id,)).fetchone()
            wallet = conn.execute(
                "SELECT sms_wallet_units FROM users WHERE id = ?", (user_id,)
            ).fetchone()[0]
        finally:
            conn.close()
        return jsonify({
            "order": _order_payload(row, provider),
            "wallet_balance": units_to_amount(wallet),
        }), 201

    def owned_order(user_id, order_id):
        conn = _open_db(db_path_getter)
        try:
            return conn.execute(
                "SELECT * FROM sms_orders WHERE id = ? AND user_id = ?",
                (order_id, user_id),
            ).fetchone()
        finally:
            conn.close()

    @bp.post("/orders/<int:order_id>/cancel")
    @_require_user
    @_provider_guard
    def cancel_order(user_id, order_id):
        row = owned_order(user_id, order_id)
        if not row or row["status"] not in ACTIVE_ORDER_STATUSES or not row["provider_activation_id"]:
            return jsonify({"error": "订单不可取消。", "code": "order_not_cancellable"}), 409
        _client().cancel(int(row["provider_activation_id"]))
        _refund_order(db_path_getter, order_id, "cancelled", "号码取消退款")
        return account()

    @bp.post("/orders/<int:order_id>/finish")
    @_require_user
    @_provider_guard
    def finish_order(user_id, order_id):
        row = owned_order(user_id, order_id)
        if not row or row["status"] not in ACTIVE_ORDER_STATUSES or not row["provider_activation_id"]:
            return jsonify({"error": "订单不可完成。", "code": "order_not_finishable"}), 409
        _client().finish(int(row["provider_activation_id"]))
        conn = _open_db(db_path_getter)
        try:
            conn.execute(
                "UPDATE sms_orders SET status = 'completed', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (order_id,),
            )
            conn.commit()
        finally:
            conn.close()
        return jsonify({"success": True})

    @bp.post("/orders/<int:order_id>/replace")
    @_require_user
    @_provider_guard
    def replace_order(user_id, order_id):
        row = owned_order(user_id, order_id)
        if not row or row["status"] != "active" or not row["provider_activation_id"]:
            return jsonify({"error": "订单不可换号。", "code": "order_not_replaceable"}), 409
        payload = _client().replace(int(row["provider_activation_id"]))
        values = payload.get("data") if isinstance(payload, dict) else []
        if isinstance(values, dict):
            values = [values]
        provider = next(
            (item for item in (_provider_activation(value) for value in values or []) if item),
            None,
        )
        if not provider:
            raise HeroSMSError("供应商未返回替换后的号码。", 502, "invalid_response")
        conn = _open_db(db_path_getter)
        try:
            conn.execute(
                """
                UPDATE sms_orders
                SET provider_activation_id = ?, phone = ?, provider_status = ?, status = 'active',
                    expires_at = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND user_id = ?
                """,
                (
                    provider["id"], provider["phone"], provider["status"],
                    provider["expiredAt"], order_id, user_id,
                ),
            )
            conn.commit()
            updated = conn.execute("SELECT * FROM sms_orders WHERE id = ?", (order_id,)).fetchone()
        finally:
            conn.close()
        return jsonify({"order": _order_payload(updated, provider)})

    @bp.post("/admin/credit")
    def admin_credit():
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or not _admin_token_valid(data.get("access_token")):
            return jsonify({"error": "管理员凭证无效。", "code": "admin_unauthorized"}), 401
        username = data.get("username")
        reference = str(data.get("reference", "")).strip()
        note = str(data.get("note", "手动充值")).strip()[:200]
        if not isinstance(username, str) or not username.strip() or len(username) > 120:
            return jsonify({"error": "用户名无效。", "code": "invalid_username"}), 400
        if not _IDEMPOTENCY_RE.fullmatch(reference):
            return jsonify({"error": "充值流水号无效。", "code": "invalid_reference"}), 400
        try:
            amount_units = amount_to_units(data.get("amount"))
        except ValueError:
            return jsonify({"error": "充值金额无效。", "code": "invalid_amount"}), 400
        if amount_units < 100 or amount_units > amount_to_units(10000):
            return jsonify({"error": "单次充值必须在 0.01 到 10000 USD 之间。", "code": "invalid_amount"}), 400

        conn = _open_db(db_path_getter)
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT user_id, balance_after_units FROM sms_wallet_ledger WHERE reference = ?",
                (reference,),
            ).fetchone()
            if existing:
                conn.rollback()
                return jsonify({
                    "success": True,
                    "idempotent": True,
                    "balance": units_to_amount(existing["balance_after_units"]),
                })
            user = conn.execute(
                "SELECT id FROM users WHERE username = ?", (username.strip(),)
            ).fetchone()
            if not user:
                conn.rollback()
                return jsonify({"error": "用户不存在。", "code": "user_not_found"}), 404
            conn.execute(
                "UPDATE users SET sms_wallet_units = sms_wallet_units + ? WHERE id = ?",
                (amount_units, user["id"]),
            )
            balance = conn.execute(
                "SELECT sms_wallet_units FROM users WHERE id = ?", (user["id"],)
            ).fetchone()[0]
            conn.execute(
                """
                INSERT INTO sms_wallet_ledger
                (user_id, amount_units, balance_after_units, kind, reference, note)
                VALUES (?, ?, ?, 'manual_credit', ?, ?)
                """,
                (user["id"], amount_units, balance, reference, note),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return jsonify({
            "success": True,
            "username": username.strip(),
            "credited": units_to_amount(amount_units),
            "balance": units_to_amount(balance),
            "currency": "USD",
        })

    @bp.post("/admin/summary")
    def admin_summary():
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or not _admin_token_valid(data.get("access_token")):
            return jsonify({"error": "管理员凭证无效。", "code": "admin_unauthorized"}), 401
        conn = _open_db(db_path_getter)
        try:
            totals = conn.execute(
                """
                SELECT COUNT(*) AS orders,
                       COALESCE(SUM(sale_price_units - refunded_units), 0) AS revenue,
                       COALESCE(SUM(CASE WHEN status NOT IN ('failed', 'cancelled')
                                   THEN provider_cost_units ELSE 0 END), 0) AS cost
                FROM sms_orders
                """
            ).fetchone()
            wallet_total = conn.execute(
                "SELECT COALESCE(SUM(sms_wallet_units), 0) FROM users"
            ).fetchone()[0]
            users = conn.execute(
                "SELECT COUNT(*) FROM users WHERE sms_wallet_units > 0"
            ).fetchone()[0]
        finally:
            conn.close()
        return jsonify({
            "orders": totals["orders"],
            "funded_users": users,
            "customer_wallet_total": units_to_amount(wallet_total),
            "revenue": units_to_amount(totals["revenue"]),
            "provider_cost": units_to_amount(totals["cost"]),
            "gross_profit": units_to_amount(totals["revenue"] - totals["cost"]),
            "markup_percent": _markup_percent(),
            "currency": "USD",
        })

    return bp
