"""Authenticated, narrowly scoped OmniChat SMS recharge HTTP boundary."""

import re
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

import requests


ORDER_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}")
REQUEST_ID_RE = re.compile(r"[A-Za-z0-9_-]{8,100}")
_API = "/api/recharge/sms-market"
_ORDER_FIELDS = (
    "id", "app", "amount_fen", "wallet_units", "wallet_amount", "status",
    "channel", "created_at", "finished_at", "request_id",
)


class OmniRechargeError(Exception):
    def __init__(self, message="充值服务暂时不可用，请稍后重试。", status_code=503,
                 code="recharge_service_unavailable"):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def validate_paid_recharge(order):
    if (not isinstance(order, dict) or order.get("app") != "sms_market"
            or order.get("status") != "credited"
            or not isinstance(order.get("id"), str)
            or not ORDER_ID_RE.fullmatch(order["id"])
            or type(order.get("wallet_units")) is not int
            or not 0 < order["wallet_units"] <= 100_000):
        raise OmniRechargeError()


def _order(payload):
    if (not isinstance(payload, dict)
            or any(field not in payload for field in _ORDER_FIELDS)
            or not isinstance(payload["id"], str)
            or not ORDER_ID_RE.fullmatch(payload["id"])
            or payload["app"] != "sms_market"
            or type(payload["wallet_units"]) is not int
            or not 0 < payload["wallet_units"] <= 100_000
            or type(payload["amount_fen"]) is not int or payload["amount_fen"] <= 0
            or not isinstance(payload["wallet_amount"], str)
            or not re.fullmatch(r"[0-9]+\.[0-9]{4}", payload["wallet_amount"])
            or Decimal(payload["wallet_amount"]) * 10_000 != payload["wallet_units"]
            or not isinstance(payload["status"], str) or not payload["status"]
            or not isinstance(payload["channel"], str)
            or not isinstance(payload["created_at"], str)
            or not isinstance(payload["finished_at"], (str, type(None)))
            or not isinstance(payload["request_id"], str)):
        raise OmniRechargeError()
    return {field: payload[field] for field in _ORDER_FIELDS}


class OmniRechargeClient:
    def __init__(self, base_url, token, *, allowed_hosts=None):
        # Test destinations require explicit injection; environment and Flask
        # testing flags never expand the production destination allowlist.
        if allowed_hosts is None:
            allowed_hosts = ("chat.bnbscheduler.top",)
        try:
            parsed = urlsplit(base_url)
            valid_base = (parsed.scheme == "https" and parsed.hostname in allowed_hosts
                          and parsed.port in (None, 443) and not parsed.username
                          and not parsed.password and not parsed.query and not parsed.fragment
                          and parsed.path in ("", "/"))
        except (TypeError, ValueError):
            valid_base = False
        if not valid_base:
            raise OmniRechargeError()
        if not isinstance(token, str) or not token or "\r" in token or "\n" in token:
            raise OmniRechargeError("请先使用共享账号登录。", 401, "shared_login_required")
        self.base_url = base_url.rstrip("/")
        self._token = token

    def _request(self, method, path, **kwargs):
        try:
            with requests.Session() as http:
                response = http.request(
                    method, self.base_url + _API + path, timeout=10,
                    headers={"Authorization": f"Bearer {self._token}",
                             "Accept": "application/json", "Content-Type": "application/json"},
                    allow_redirects=False, **kwargs,
                )
            if response.status_code == 401:
                raise OmniRechargeError("请重新使用共享账号登录。", 401, "shared_login_required")
            if response.status_code == 404:
                raise OmniRechargeError("充值订单不存在。", 404, "order_not_found")
            payload = response.json()
            if not isinstance(payload, dict):
                raise OmniRechargeError()
            if response.status_code in (409, 429):
                message = payload.get("detail", payload.get("error"))
                code = payload.get("code")
                if (not isinstance(message, str) or not message.strip() or len(message) > 300
                        or self._token in message or any(ord(ch) < 32 for ch in message)):
                    raise OmniRechargeError()
                if (not isinstance(code, str) or not re.fullmatch(r"[a-z_]{1,80}", code)
                        or self._token in code):
                    code = "request_conflict" if response.status_code == 409 else "rate_limited"
                raise OmniRechargeError(message, response.status_code, code)
            if response.status_code not in (200, 201):
                raise OmniRechargeError()
            return payload
        except (requests.RequestException, ValueError, TypeError):
            raise OmniRechargeError() from None

    def config(self):
        payload = self._request("GET", "/config")
        try:
            rate = Decimal(payload["usd_cny"])
            packages = payload["packages"]
            valid = (isinstance(payload["usd_cny"], str) and rate.is_finite() and rate > 0
                     and isinstance(packages, list) and len(packages) == 3
                     and sorted(item["usd_units"] for item in packages) == [1, 5, 10]
                     and all(type(item["usd_units"]) is int
                             and isinstance(item["usd"], str)
                             and Decimal(item["usd"]) == item["usd_units"]
                             and type(item["fen"]) is int and item["fen"] > 0
                             for item in packages))
        except (KeyError, TypeError, ValueError, InvalidOperation):
            valid = False
        if not valid:
            raise OmniRechargeError()
        return {"usd_cny": payload["usd_cny"], "packages": [
            {field: item[field] for field in ("usd_units", "usd", "fen")}
            for item in packages
        ]}

    def create_order(self, package_usd, request_id):
        if type(package_usd) is not int or package_usd not in (1, 5, 10):
            raise OmniRechargeError("请选择有效的充值套餐。", 400, "invalid_package")
        if not isinstance(request_id, str) or not REQUEST_ID_RE.fullmatch(request_id):
            raise OmniRechargeError("充值请求标识无效。", 400, "invalid_request_id")
        payload = self._request("POST", "/orders", json={
            "package_usd": package_usd, "request_id": request_id,
        })
        order = _order(payload.get("order"))
        path = f"{_API}/orders/{order['id']}/checkout"
        if (payload.get("pay_url") != path or type(payload.get("reused")) is not bool
                or order["request_id"] != request_id
                or order["wallet_units"] != package_usd * 10_000):
            raise OmniRechargeError()
        return {"order": order, "checkout_url": self.base_url + path,
                "reused": payload["reused"]}

    def list_orders(self):
        orders = []
        cursor = None
        seen_cursors = set()
        while True:
            options = {"params": {"cursor": cursor}} if cursor is not None else {}
            payload = self._request("GET", "/orders", **options)
            page = payload.get("orders")
            if (not isinstance(page, list) or len(page) > 100
                    or "next_cursor" not in payload):
                raise OmniRechargeError()
            next_cursor = payload["next_cursor"]
            if next_cursor is not None and (
                type(next_cursor) is not int or not 0 < next_cursor <= 9223372036854775807
                or not page or next_cursor in seen_cursors
                or (cursor is not None and next_cursor >= cursor)
            ):
                raise OmniRechargeError()
            orders.extend(_order(order) for order in page)
            if next_cursor is None:
                return {"orders": orders}
            seen_cursors.add(next_cursor)
            cursor = next_cursor

    def get_order(self, order_id):
        if not isinstance(order_id, str) or not ORDER_ID_RE.fullmatch(order_id):
            raise OmniRechargeError("充值订单不存在。", 404, "order_not_found")
        order = _order(self._request("GET", f"/orders/{order_id}").get("order"))
        if order["id"] != order_id:
            raise OmniRechargeError()
        return {"order": order}
