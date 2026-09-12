"""Small, server-side HeroSMS client.

The provider key must never leave the Flask process.  Only a fixed set of
HeroSMS endpoints is exposed through the application blueprint.
"""

import os
import threading
import time

import requests


API_BASE = "https://hero-sms.com/api/v1"
COMPAT_ENDPOINT = "https://hero-sms.com/stubs/handler_api.php"


class HeroSMSError(RuntimeError):
    def __init__(self, message, status_code=502, code="provider_error"):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


_request_lock = threading.Lock()
_last_request_at = 0.0


def _minimum_request_interval():
    try:
        value = float(os.getenv("HERO_SMS_MIN_REQUEST_INTERVAL", "0.15"))
    except (TypeError, ValueError):
        return 0.15
    return min(2.0, max(0.0, value))


def _throttle():
    """Keep this process well below HeroSMS's documented 50 RPS ceiling."""
    global _last_request_at
    with _request_lock:
        interval = _minimum_request_interval()
        wait_for = interval - (time.monotonic() - _last_request_at)
        if wait_for > 0:
            time.sleep(wait_for)
        _last_request_at = time.monotonic()


class HeroSMSClient:
    def __init__(self, api_key, session=None):
        if not api_key:
            raise HeroSMSError("HeroSMS API 尚未配置。", 503, "not_configured")
        self.api_key = api_key
        self.session = session or requests.Session()

    @staticmethod
    def _provider_error(status_code):
        if status_code == 400:
            return HeroSMSError("HeroSMS 无法处理该请求。", 422, "bad_request")
        if status_code == 401:
            return HeroSMSError("HeroSMS API Key 无效或已失效。", 503, "invalid_api_key")
        if status_code == 402:
            return HeroSMSError("HeroSMS 账户余额不足。", 402, "insufficient_balance")
        if status_code == 403:
            return HeroSMSError("HeroSMS 拒绝了本次操作。", 403, "provider_denied")
        if status_code == 404:
            return HeroSMSError("HeroSMS 未找到对应激活。", 404, "not_found")
        if status_code == 422:
            return HeroSMSError(
                "HeroSMS 拒绝了请求，请检查国家、服务和价格限制。",
                422,
                "provider_validation",
            )
        if status_code == 429:
            return HeroSMSError("HeroSMS 请求过于频繁，请稍后重试。", 429, "rate_limited")
        return HeroSMSError("HeroSMS 暂时不可用，请稍后重试。", 502, "provider_unavailable")

    def _send(self, method, url, *, headers=None, params=None, json=None):
        _throttle()
        try:
            response = self.session.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json,
                timeout=(5, 20),
                allow_redirects=False,
            )
        except requests.RequestException:
            # Never include the exception string.  Compatibility requests carry
            # api_key in the URL and requests may echo that URL in exceptions.
            raise HeroSMSError("无法连接 HeroSMS，请稍后重试。", 502, "provider_unavailable")
        if response.status_code < 200 or response.status_code >= 300:
            raise self._provider_error(response.status_code)
        return response

    def _rest(self, method, path, *, params=None, payload=None):
        response = self._send(
            method,
            API_BASE + path,
            headers={
                "Accept": "application/json",
                "Authorization": f"ApiKey {self.api_key}",
                "Content-Type": "application/json",
            },
            params=params,
            json=payload,
        )
        if response.status_code == 204:
            return {}
        try:
            data = response.json()
        except ValueError:
            raise HeroSMSError("HeroSMS 返回了无法识别的数据。", 502, "invalid_response")
        if not isinstance(data, dict):
            raise HeroSMSError("HeroSMS 返回了无法识别的数据。", 502, "invalid_response")
        return data

    def _compat(self, action, **params):
        query = {"action": action, "api_key": self.api_key, **params}
        response = self._send("GET", COMPAT_ENDPOINT, params=query)
        return response

    def get_balance(self):
        text = self._compat("getBalance").text.strip()
        if not text.startswith("ACCESS_BALANCE:"):
            raise HeroSMSError("HeroSMS 未返回可用余额。", 502, "invalid_response")
        try:
            return float(text.split(":", 1)[1].strip())
        except (IndexError, ValueError):
            raise HeroSMSError("HeroSMS 未返回可用余额。", 502, "invalid_response")

    def get_countries(self):
        response = self._compat("getCountries")
        try:
            data = response.json()
        except ValueError:
            raise HeroSMSError("HeroSMS 国家列表暂不可用。", 502, "invalid_response")
        if not isinstance(data, list):
            raise HeroSMSError("HeroSMS 国家列表暂不可用。", 502, "invalid_response")
        return data

    def get_services(self, country=None):
        params = {"lang": "cn"}
        if country is not None:
            params["country"] = country
        response = self._compat("getServicesList", **params)
        try:
            data = response.json()
        except ValueError:
            raise HeroSMSError("HeroSMS 服务列表暂不可用。", 502, "invalid_response")
        services = data.get("services") if isinstance(data, dict) else None
        if not isinstance(services, list):
            raise HeroSMSError("HeroSMS 服务列表暂不可用。", 502, "invalid_response")
        return services

    def get_offers(self, country=None, service=None):
        params = {}
        if country is not None:
            params["countries"] = str(country)
        if service:
            params["services"] = service
        return self._rest("GET", "/activations/offers/sms", params=params)

    def list_activations(self):
        return self._rest("GET", "/activations")

    def purchase(self, service, country, max_price):
        return self._rest(
            "POST",
            "/activations",
            payload={
                "service": service,
                "country": country,
                "amount": 1,
                "maxPrice": max_price,
                "fixedPrice": False,
                "verificationType": "sms",
            },
        )

    def cancel(self, activation_id):
        return self._rest("DELETE", f"/activations/{activation_id}")

    def finish(self, activation_id):
        return self._rest("POST", f"/activations/{activation_id}/finish")

    def replace(self, activation_id):
        return self._rest("POST", f"/activations/{activation_id}/replace")
