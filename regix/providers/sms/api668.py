"""HTTP provider for the api668.com number-token service."""

from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import requests

from .base import BaseSmsProvider, SmsActivation

logger = logging.getLogger(__name__)

_CODE_PATTERN = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_CONTEXT_CODE_PATTERN = re.compile(
    r"(?:code|otp|passcode|verification|验证码)[^\d]{0,80}"
    r"(?<!\d)(\d{6})(?!\d)",
    re.IGNORECASE,
)


class Api668Error(RuntimeError):
    pass


class Api668Provider(BaseSmsProvider):
    """api668 order-based SMS provider.

    The upstream API creates an order first and exposes phone tokens under that
    order. We keep ``order_id`` in ``SmsActivation.activation_id`` and poll the
    token list until a phone and verification code are available.
    """

    DEFAULT_BASE_URL = "https://api668.com"
    auto_report_success_on_code = False

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "",
        default_service: str = "",
        default_country: str = "",
        prefix: str = "",
        auth_scheme: str = "raw",
        proxy: Optional[str] = None,
        timeout: int = 30,
    ):
        self.api_key = str(api_key or "").strip()
        if not self.api_key:
            raise ValueError("api668 API key is required")
        self.base_url = str(base_url or self.DEFAULT_BASE_URL).rstrip("/")
        self.default_service = str(default_service or "").strip()
        self.default_country = str(default_country or "").strip()
        self.prefix = str(prefix or "").strip()
        self.auth_scheme = str(auth_scheme or "raw").strip().lower()
        if self.auth_scheme not in {"raw", "bearer"}:
            self.auth_scheme = "raw"
        self._proxy = str(proxy or "").strip() or None
        self._proxies = (
            {"http": self._proxy, "https": self._proxy} if self._proxy else None
        )
        self.timeout = max(1, int(timeout or 30))
        self._auth_scheme_locked = False
        self._resend_callback: Optional[Callable[[], None]] = None
        self.current_activation: Optional[SmsActivation] = None

    @staticmethod
    def _payload(data: Any) -> dict:
        if not isinstance(data, dict):
            raise Api668Error("API668 returned a non-object response")
        if data.get("code") != 0:
            raise Api668Error(str(data.get("msg") or f"API668 error {data.get('code')}"))
        payload = data.get("data")
        if not isinstance(payload, dict):
            raise Api668Error("API668 response has no data object")
        return payload

    def _headers(self, scheme: str) -> dict[str, str]:
        token = self.api_key
        return {
            "Authorization": token if scheme == "raw" else f"Bearer {token}",
            "accept": "application/json",
            "accept-language": "zh",
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        body: Optional[dict] = None,
        timeout: Optional[int] = None,
    ) -> dict:
        schemes = [self.auth_scheme]
        if not self._auth_scheme_locked:
            schemes = ["bearer", "raw"] if self.auth_scheme == "bearer" else ["raw", "bearer"]

        last_error: Exception | None = None
        for scheme in schemes:
            try:
                response = requests.request(
                    method,
                    f"{self.base_url}{path}",
                    params=params,
                    json=body,
                    headers=self._headers(scheme),
                    timeout=timeout or self.timeout,
                    proxies=self._proxies,
                )
                response.raise_for_status()
                payload = self._payload(response.json())
                self.auth_scheme = scheme
                self._auth_scheme_locked = True
                return payload
            except Api668Error:
                # Authentication and business errors are distinct only by the
                # upstream code, so try the alternate auth format on 401-style
                # messages, then surface the original message if it persists.
                raise
            except Exception as exc:
                last_error = exc
                continue
        raise Api668Error(f"API668 request failed: {last_error}")

    # ---- catalog ----

    def get_country_list(self) -> list[dict]:
        return list(self._request("GET", "/api/country/").get("country_list") or [])

    def get_platform_list(self, country_id: int | str, name: str = "") -> list[dict]:
        params: dict[str, Any] = {"country_id": int(country_id or 0)}
        if name:
            params["name"] = name
        return list(self._request("GET", "/api/platform/", params=params).get("platform_list") or [])

    def get_prefix_list(self, country_id: int | str) -> list[dict]:
        payload = self._request(
            "GET",
            "/api/phone_number/prefix/",
            params={"country_id": int(country_id or 0)},
        )
        return list(payload.get("prefix_list") or [])

    def get_inventory(self, platform_id: int | str, prefix: str = "") -> int:
        params: dict[str, Any] = {"platform_id": int(platform_id or 0)}
        if prefix:
            params["prefix"] = prefix
        return int(self._request("GET", "/api/token/inventory", params=params).get("inventory") or 0)

    def create_order(self, platform_id: int | str, quantity: int = 1, prefix: str = "") -> dict:
        payload = self._request(
            "POST",
            "/api/order",
            body={
                "platform_id": int(platform_id or 0),
                "quantity": max(1, int(quantity or 1)),
                "prefix": str(prefix or ""),
            },
        )
        return {
            "order_id": payload.get("order_id"),
            "quantity": payload.get("quantity", 1),
        }

    def list_orders(self, *, platform_name: str = "", page: int = 1, size: int = 20) -> dict:
        params: dict[str, Any] = {"page": max(1, int(page or 1)), "size": max(1, int(size or 20))}
        if platform_name:
            params["platform_name"] = platform_name
        return dict(self._request("GET", "/api/order/", params=params))

    def list_tokens(self, order_id: int | str, *, page: int = 1, size: int = 20) -> list[dict]:
        payload = self._request(
            "GET",
            "/api/token/",
            params={
                "order_id": int(order_id or 0),
                "page": max(1, int(page or 1)),
                "size": max(1, int(size or 20)),
            },
        )
        return list(payload.get("items") or [])

    def get_balance(self) -> float:
        # The documented public API has no balance endpoint. health_check()
        # is used by the WebUI connection test instead.
        raise NotImplementedError("api668 does not expose a balance endpoint")

    def health_check(self) -> dict:
        countries = self.get_country_list()
        return {"ok": True, "country_count": len(countries)}

    def _resolve_platform(self, country_id: str, service: str = "") -> tuple[int, dict]:
        wanted = str(service or self.default_service or "").strip()
        platforms = self.get_platform_list(country_id)
        if not platforms:
            raise Api668Error(f"api668 country {country_id} has no platforms")
        if wanted.isdigit():
            platform_id = int(wanted)
            exact = next((item for item in platforms if int(item.get("id") or 0) == platform_id), None)
            if exact:
                return platform_id, exact
            raise Api668Error(f"platform_id {platform_id} is not available in country {country_id}")
        if wanted:
            lowered = wanted.casefold()
            exact = next(
                (item for item in platforms if str(item.get("name") or "").casefold() == lowered),
                None,
            )
            if exact:
                return int(exact.get("id") or 0), exact
            partial = next(
                (item for item in platforms if lowered in str(item.get("name") or "").casefold()),
                None,
            )
            if partial:
                return int(partial.get("id") or 0), partial
            raise Api668Error(f"platform '{wanted}' is not available in country {country_id}")
        return int(platforms[0].get("id") or 0), platforms[0]

    def _resolve_prefix(self, country_id: str) -> str:
        if self.prefix:
            return self.prefix
        prefixes = self.get_prefix_list(country_id)
        if prefixes:
            return str(prefixes[0].get("prefix") or "")
        countries = self.get_country_list()
        country = next((item for item in countries if str(item.get("id")) == country_id), None)
        return str(country.get("phone_area_code") or "") if country else ""

    def get_top_countries(self, service: Optional[str] = None) -> list[dict]:
        wanted = str(service or self.default_service or "").strip()
        countries = self.get_country_list()

        def load_country(country: dict) -> Optional[dict]:
            country_id = str(country.get("id") or "")
            if not country_id:
                return None
            try:
                platform_id, platform = self._resolve_platform(country_id, wanted)
                prefix = self._resolve_prefix(country_id)
                count = self.get_inventory(platform_id, prefix)
                return {
                    "country": country_id,
                    "count": count,
                    "price": platform.get("price"),
                    "name_cn": country.get("cn_name") or country.get("en_name") or f"国家{country_id}",
                    "platform_id": platform_id,
                    "platform_name": platform.get("name"),
                    "prefix": prefix,
                }
            except Exception as exc:
                logger.debug("api668 country %s unavailable: %s", country_id, exc)
                return None

        workers = min(4, max(1, len(countries)))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            rows = [row for row in executor.map(load_country, countries) if row]
        rows.sort(key=lambda row: (row.get("price") if row.get("price") is not None else 10**9, -int(row.get("count") or 0)))
        return rows

    def get_best_country(
        self,
        service: Optional[str] = None,
        *,
        min_stock: int = 20,
        max_price: float = 0,
        strict_whitelist: bool = False,
        allowed_countries: Optional[list[str]] = None,
    ) -> Optional[str]:
        # api668 country IDs are independent from the sms-activate whitelist.
        rows = self.get_top_countries(service)
        if allowed_countries:
            allowed = {str(item) for item in allowed_countries}
            rows = [row for row in rows if str(row.get("country")) in allowed]
        rows = [row for row in rows if int(row.get("count") or 0) >= max(1, int(min_stock or 1))]
        if max_price and max_price > 0:
            rows = [
                row for row in rows
                if row.get("price") is not None and float(row["price"]) <= float(max_price)
            ]
        return str(rows[0]["country"]) if rows else None

    @staticmethod
    def _normalize_phone(value: Any) -> str:
        phone = re.sub(r"[^\d+]", "", str(value or ""))
        if phone and not phone.startswith("+"):
            phone = f"+{phone}"
        return phone

    @staticmethod
    def _extract_code(value: Any) -> str:
        text = str(value or "")
        match = _CONTEXT_CODE_PATTERN.search(text) or _CODE_PATTERN.search(text)
        return match.group(1) if match else ""

    def _code_from_token(self, token: dict) -> str:
        for key in ("sms_code", "code", "verification_code"):
            code = self._extract_code(token.get(key))
            if code:
                return code

        token_url = str(token.get("token_url") or "").strip()
        if not token_url:
            return ""
        try:
            parsed = urlparse(token_url)
            if parsed.scheme in {"http", "https"}:
                # Some deployments return JSON; others return a small HTML/text
                # page containing the one-time code.
                response = requests.get(
                    token_url,
                    headers={"Authorization": self.api_key, "accept": "application/json, text/plain"},
                    timeout=self.timeout,
                    proxies=self._proxies,
                )
                response.raise_for_status()
                content_type = str(response.headers.get("content-type") or "").lower()
                if "json" in content_type:
                    try:
                        payload = response.json()
                    except Exception:
                        payload = None
                    if isinstance(payload, dict):
                        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
                        fields = data.get("fields") if isinstance(data.get("fields"), dict) else {}
                        candidates = (
                            fields.get("content"),
                            fields.get("description"),
                            data.get("description"),
                            data.get("message"),
                        )
                        for candidate in candidates:
                            code = self._extract_code(candidate)
                            if code:
                                return code
                        return ""
                return self._extract_code(response.text)
        except Exception as exc:
            logger.debug("api668 token_url fetch failed: %s", exc)
        return ""

    def _wait_for_phone(self, order_id: str, timeout: int) -> Optional[dict]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                tokens = self.list_tokens(order_id, size=100)
            except Exception as exc:
                logger.debug("api668 token poll failed: %s", exc)
                tokens = []
            for token in tokens:
                phone = self._normalize_phone(token.get("full_number"))
                if phone:
                    return {"token": token, "phone": phone}
            time.sleep(min(3, max(0, deadline - time.time())))
        return None

    def get_number(
        self,
        *,
        service: str,
        country: str = "",
        country_candidates: Optional[list[str]] = None,
    ) -> SmsActivation:
        candidates = [item for item in (country_candidates or [country]) if str(item or "").strip()]
        if self.default_country and self.default_country not in candidates:
            candidates.append(self.default_country)
        if not candidates:
            raise Api668Error("api668 country is required")

        errors: list[str] = []
        for country_id in candidates:
            try:
                platform_id, _ = self._resolve_platform(str(country_id), service)
                prefix = self._resolve_prefix(str(country_id))
                order = self.create_order(platform_id, quantity=1, prefix=prefix)
                order_id = str(order.get("order_id") or "")
                if not order_id:
                    raise Api668Error("create order response has no order_id")
                phone_result = self._wait_for_phone(order_id, timeout=45)
                if not phone_result:
                    raise Api668Error(f"order {order_id} did not return a phone token")
                activation = SmsActivation(
                    activation_id=order_id,
                    phone_number=phone_result["phone"],
                    country=str(country_id),
                    metadata={
                        "provider": "api668",
                        "platform_id": platform_id,
                        "prefix": prefix,
                        "token": phone_result["token"],
                    },
                )
                self.current_activation = activation
                return activation
            except Exception as exc:
                errors.append(f"country {country_id}: {exc}")
        raise Api668Error("; ".join(errors) or "api668 could not create an order")

    def _wait_for_code(self, order_id: str, timeout: int, poll: int = 5) -> str:
        deadline = time.time() + max(5, int(timeout or 5))
        last_resend_at = time.time()
        resend_count = 0
        while time.time() < deadline:
            try:
                tokens = self.list_tokens(order_id, size=100)
            except Exception as exc:
                logger.debug("api668 code poll failed: %s", exc)
                tokens = []
            for token in tokens:
                code = self._code_from_token(token)
                if code:
                    return code

            # OpenAI can expire the pending phone challenge; retry sending the
            # SMS at the cadence chosen by the registration flow.
            now = time.time()
            if self._resend_callback and resend_count < 3 and now - last_resend_at >= 20:
                try:
                    self._resend_callback()
                    resend_count += 1
                    last_resend_at = now
                except Exception as exc:
                    logger.debug("api668 resend callback failed: %s", exc)
            time.sleep(min(poll, max(0, deadline - time.time())))
        return ""

    def get_code(self, activation_id: str, *, timeout: int = 180) -> str:
        return self._wait_for_code(str(activation_id), timeout=timeout)

    def cancel(self, activation_id: str) -> bool:
        # The documented API has no cancel/refund endpoint.
        return False

    def report_success(self, activation_id: str) -> bool:
        # Orders are prepaid; token expiry is managed upstream.
        return True

    def set_resend_callback(self, callback: Optional[Callable[[], None]]) -> None:
        self._resend_callback = callback
