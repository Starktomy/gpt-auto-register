"""ChatGPT Plus / Team payment-link generation.

Mirrors the three-step flow of the reference toolbox
(``1837620622/chatgpt-specimen-toolbox``):

  1. ``POST https://chatgpt.com/backend-api/payments/checkout``
     -> ``checkout_session_id`` + ``client_secret`` + ``publishable_key``
  2. ``POST https://api.stripe.com/v1/payment_pages/{cs_id}/init``
     -> ``stripe_hosted_url`` (carries the authoritative ``#fid`` fragment)
  3. rewrite the host ``checkout.stripe.com`` -> ``pay.openai.com``

Step 2 is the part that matters: in ``hosted`` mode OpenAI frequently returns a
URL without the ``#fid`` fragment, and a link built from ``client_secret``
alone then refuses to open. Asking Stripe directly is the only reliable way to
get the full long link.

The result is a *standalone* Stripe link that can be opened in a clean browser
(it does not depend on the ChatGPT session), plus the ChatGPT-internal wrapper
link for the account that owns the session.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from regix.http.client import create_http_session
from regix.openai.codex_token import refresh_codex_token
from regix.services.plan_check import normalize_access_token, token_claims

logger = logging.getLogger(__name__)

CHECKOUT_PATH = "/backend-api/payments/checkout"
CHECKOUT_URL = f"https://chatgpt.com{CHECKOUT_PATH}"
STRIPE_INIT_BASE = "https://api.stripe.com/v1/payment_pages/"

# Public Stripe live key that OpenAI ships inside its checkout bundle. Only used
# when the checkout response does not carry a publishable key of its own.
DEFAULT_STRIPE_PK = (
    "pk_live_51HOrSwC6h1nxGoI3lTAgRjYVrz4dU3fVOabyCcKR3pbEJguCVAlqCxdxCUvoRh1XWw"
    "RacViovU3kLKvpkjh7IqkW00iXQsjo3n"
)
# Must match the _stripe_version ChatGPT's own checkout sends, otherwise Stripe
# answers with an older client shape (and the hosted URL loses its fragment).
STRIPE_API_VERSION = (
    "2025-03-31.basil; checkout_server_update_beta=v1; checkout_manual_approval_preview=v1"
)
CANCEL_URL = "https://chatgpt.com/#pricing"

PLAN_PLUS = "chatgptplusplan"
PLAN_TEAM = "chatgptteamplan"

_STRIPE_HOST = "checkout.stripe.com"
_OPENAI_HOST = "pay.openai.com"

# OpenAI validates billing_details.country against the country the request
# exits from, so a preset that does not match the exit IP is rejected with
# "Billing country must match request country". Knowing the exit country lets
# the UI warn before the request instead of after the failure.
_EXIT_COUNTRY_URL = "https://ipinfo.io/json"
_EXIT_COUNTRY_TTL = 600.0
_exit_country_cache: dict[str, tuple[float, str]] = {}

_SESSION_ID_RE = re.compile(r"(cs_(?:live|test)_[A-Za-z0-9]+)")
_ENTITY_RE = re.compile(r"/checkout/([^/]+)/cs_(?:live|test)_")

# Merchant entity used by OpenAI per billing country. The checkout response
# normally carries it, this is only the fallback for a response without it.
_IE_COUNTRIES = {
    "DE", "FR", "IT", "ES", "NL", "BE", "AT", "PT", "IE", "LU",
    "FI", "GR", "CY", "EE", "LV", "LT", "MT", "SK", "SI", "GB",
}


class PaymentLinkError(RuntimeError):
    """Payment-link generation failed.

    ``code`` is a stable machine-readable reason; ``http_status`` carries the
    upstream status when there was one.
    """

    def __init__(
        self,
        code: str,
        *,
        http_status: int | None = None,
        detail: str = "",
        exit_country: str = "",
    ) -> None:
        super().__init__(code)
        self.code = code
        self.http_status = http_status
        self.detail = detail
        # Filled in for checkout_country_mismatch: the country the request
        # actually exits from, which is what billing_details.country must match.
        self.exit_country = exit_country


@dataclass(frozen=True)
class PaymentProfile:
    """A country/currency pair that shapes the payment page."""

    id: str
    label: str
    country: str
    currency: str
    locale: str = "en"
    note: str = ""


# Payment-page presets. country/currency decide the locale, the currency and
# the payment methods Stripe offers on the page; the trial entitlement itself is
# decided by the exit IP of the request, not by these fields.
PAYMENT_PROFILES: tuple[PaymentProfile, ...] = (
    PaymentProfile("paypal_de", "PayPal · 德国", "DE", "EUR", "en", "欧元区常用 · 可能显示 PayPal"),
    PaymentProfile("paypal_fr", "PayPal · 法国", "FR", "EUR", "en", "欧元区备选 · 德区拒卡时可换"),
    PaymentProfile("paypal_it", "PayPal · 意大利", "IT", "EUR", "en", "欧元区备选"),
    PaymentProfile("paypal_es", "PayPal · 西班牙", "ES", "EUR", "en", "欧元区备选"),
    PaymentProfile("paypal_nl", "PayPal · 荷兰", "NL", "EUR", "en", "欧元区备选 · 可能含 iDEAL"),
    PaymentProfile("paypal_be", "PayPal · 比利时", "BE", "EUR", "en", "欧元区备选 · 可能含 Bancontact"),
    PaymentProfile("paypal_at", "PayPal · 奥地利", "AT", "EUR", "en", "欧元区备选 · 可能含 EPS"),
    PaymentProfile("paypal_pt", "PayPal · 葡萄牙", "PT", "EUR", "en", "欧元区备选 · 使用较少"),
    PaymentProfile("paypal_ie", "PayPal · 爱尔兰", "IE", "EUR", "en", "欧元区备选 · 英语界面较多"),
    PaymentProfile("direct_jp", "日区直绑 · JPY", "JP", "JPY", "ja", "日卡 / Wise 直绑（非 PayPal）"),
    PaymentProfile("gopay_id", "GoPay · 印尼", "ID", "IDR", "en", "印尼区 GoPay · 风控较严"),
    PaymentProfile("upi_in", "UPI · 印度", "IN", "INR", "en", "印度 UPI · INR · 需本地银行能力"),
    PaymentProfile("pix_br", "PIX · 巴西", "BR", "BRL", "pt-BR", "巴西 PIX · BRL · 需本地支付能力"),
    PaymentProfile("paypal_gb", "PayPal · 英国", "GB", "GBP", "en", "英镑区 · 有时显示 PayPal"),
    PaymentProfile("us_default", "美区 · USD", "US", "USD", "en", "OpenAI 默认区域 · 多为卡直付"),
)

_DEFAULT_PROFILE = PAYMENT_PROFILES[-1]


@dataclass(frozen=True)
class PaymentLink:
    """One generated checkout session and every link derived from it."""

    url: str
    urls: dict[str, str]
    plan: str
    plan_name: str
    country: str
    currency: str
    locale: str
    checkout_session_id: str = ""
    processor_entity: str = ""
    stripe_hosted: bool = False
    elapsed_ms: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "urls": dict(self.urls),
            "plan": self.plan,
            "plan_name": self.plan_name,
            "country": self.country,
            "currency": self.currency,
            "locale": self.locale,
            "checkout_session_id": self.checkout_session_id,
            "processor_entity": self.processor_entity,
            "stripe_hosted": self.stripe_hosted,
            "elapsed_ms": self.elapsed_ms,
            "warnings": list(self.warnings),
        }


# ─────────────────────────── pure helpers ───────────────────────────


def normalize_plan(plan: str) -> str:
    """Map a user-facing plan name onto ``plus`` / ``team``."""
    value = str(plan or "").strip().lower().replace("-", "_").replace(" ", "_")
    if value in {"team", "business", "chatgptteamplan", "team_plan"}:
        return "team"
    return "plus"


def resolve_profile(
    *,
    profile: str = "",
    country: str = "",
    currency: str = "",
    locale: str = "",
) -> PaymentProfile:
    """Pick the country/currency pair for the payment page.

    An explicit ``country`` wins over a profile id; a profile id fills in
    whatever the caller did not specify. Everything unknown falls back to the
    US default, which is OpenAI's own default region.
    """
    selected: PaymentProfile | None = None
    wanted = str(profile or "").strip().lower()
    if wanted:
        for item in PAYMENT_PROFILES:
            if item.id == wanted:
                selected = item
                break
        if selected is None:
            raise PaymentLinkError("payment_profile_unknown", detail=wanted)
    country_value = str(country or "").strip().upper()
    currency_value = str(currency or "").strip().upper()
    if country_value or currency_value:
        base = selected or _DEFAULT_PROFILE
        return PaymentProfile(
            id=base.id,
            label=base.label,
            country=country_value or base.country,
            currency=currency_value or base.currency,
            locale=(str(locale).strip() or base.locale),
            note=base.note,
        )
    base = selected or _DEFAULT_PROFILE
    if locale:
        return PaymentProfile(
            id=base.id,
            label=base.label,
            country=base.country,
            currency=base.currency,
            locale=str(locale).strip(),
            note=base.note,
        )
    return base


def build_checkout_body(
    plan: str,
    country: str,
    currency: str,
    *,
    workspace_name: str = "",
    interval: str = "month",
    seats: int = 2,
    promo_code: str = "",
    promo_campaign_id: str = "",
) -> dict:
    """Build the ``/backend-api/payments/checkout`` request body.

    Field list is exact on purpose. ``success_url``, ``entry_point``,
    ``promo_campaign``, ``locale`` and ``check_card_proxy`` must NOT be sent:
    the first three make the Stripe session behave oddly (the long link stops
    opening), ``locale`` follows the browser, and ``check_card_proxy`` is a
    retired API field. Letting ChatGPT decide the success URL keeps the
    PayPal return path inside chatgpt.com.
    """
    plan_key = normalize_plan(plan)
    body: dict[str, Any] = {
        "plan_name": PLAN_TEAM if plan_key == "team" else PLAN_PLUS,
        "checkout_ui_mode": "hosted",
        "billing_details": {
            "country": str(country or "").strip().upper(),
            "currency": str(currency or "").strip().upper(),
        },
        "cancel_url": CANCEL_URL,
    }
    if plan_key == "team":
        body["team_plan_data"] = {
            "workspace_name": str(workspace_name or "").strip() or "我的工作区",
            "price_interval": "year" if str(interval).strip().lower() == "year" else "month",
            "seat_quantity": max(1, int(seats or 2)),
        }
    # 两种优惠是**不同字段**：
    #   promo_code      自定义优惠码（用户手输，比如 SAVE20）
    #   promo_campaign  官方试用活动 id（例如 plus-1-month-free），走对象结构
    # 参考实现 openai-plus-vxt 用的就是后者；两者可以并存，互不覆盖。
    campaign = str(promo_campaign_id or "").strip()
    if campaign:
        body["promo_campaign"] = {
            "promo_campaign_id": campaign,
            "is_coupon_from_query_param": False,
        }
    promo = str(promo_code or "").strip()
    if promo:
        body["promo_code"] = promo
    return body


def build_stripe_init_body(
    publishable_key: str,
    locale: str = "en",
    timezone_name: str = "Asia/Shanghai",
    stripe_js_id: str = "",
) -> str:
    """Build the ``payment_pages/{id}/init`` form body.

    These are the fields ChatGPT's own checkout sends to Stripe; Stripe rejects
    the init call (or answers without a hosted URL) when the betas /
    elements options are missing.
    """
    params = {
        "browser_locale": "en-US",
        "browser_timezone": timezone_name or "Asia/Shanghai",
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[stripe_js_id]": stripe_js_id or str(uuid4()),
        "elements_session_client[locale]": str(locale or "en").strip() or "en",
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_options_client[saved_payment_method][enable_save]": "auto",
        "elements_options_client[saved_payment_method][enable_redisplay]": "auto",
        "key": str(publishable_key or ""),
        "_stripe_version": STRIPE_API_VERSION,
    }
    return urlencode(params)


def fragment_from_client_secret(client_secret: str, session_id: str) -> str:
    """Extract the ``#fragment`` that belongs to a checkout session.

    The client secret embeds the fragment after ``{session_id}_secret_``.
    """
    if not client_secret or not session_id:
        return ""
    marker = f"{session_id}_secret_"
    index = client_secret.find(marker)
    if index < 0:
        return ""
    fragment = client_secret[index + len(marker):]
    return f"#{fragment}" if fragment else ""


def extract_session_id(data: Any) -> str:
    """Read the checkout session id out of a checkout response."""
    if not isinstance(data, dict):
        return ""
    session_id = str(data.get("checkout_session_id") or "").strip()
    if session_id:
        return session_id
    for key in ("checkout_url", "url", "openai_checkout_url"):
        value = data.get(key)
        if isinstance(value, str) and value:
            match = _SESSION_ID_RE.search(value)
            if match:
                return match.group(1)
    return ""


def extract_entity_from_url(url: str) -> str:
    """Read ``openai_ie`` / ``openai_llc`` out of a checkout URL."""
    if not url:
        return ""
    match = _ENTITY_RE.search(str(url))
    return match.group(1) if match else ""


def infer_merchant_path(country: str) -> str:
    """Fallback merchant entity for a billing country."""
    code = str(country or "").strip().upper()
    return "openai_ie" if code in _IE_COUNTRIES else "openai_llc"


def rewrite_host(url: str, host: str) -> str:
    """Swap the host of a URL, keeping path, query and fragment intact.

    The fragment (``#fid=...``) is the part that makes the long link work, so
    it must survive the rewrite untouched.
    """
    if not url or not host:
        return url or ""
    try:
        parts = urlsplit(str(url))
    except ValueError:
        return str(url)
    if not parts.scheme or not parts.netloc:
        return str(url)
    netloc = parts.netloc.rsplit("@", 1)[-1]
    port = ""
    if ":" in netloc:
        netloc, _, port_value = netloc.partition(":")
        port = f":{port_value}"
    return urlunsplit((parts.scheme, f"{host}{port}", parts.path, parts.query, parts.fragment))


def build_payment_urls(
    data: Any,
    country: str = "US",
    hosted_url: str = "",
) -> dict:
    """Derive every usable link from a checkout (and optional Stripe) response.

    ``hosted_url`` is the URL Stripe returned from ``payment_pages/init``. It
    wins over the URL inside the OpenAI response because it carries the
    authoritative ``#fid`` fragment.
    """
    payload = data if isinstance(data, dict) else {}
    session_id = extract_session_id(payload)
    entity = (
        str(payload.get("processor_entity") or "").strip()
        or extract_entity_from_url(str(payload.get("url") or ""))
        or extract_entity_from_url(str(payload.get("checkout_url") or ""))
        or infer_merchant_path(country)
    )

    source = str(hosted_url or "").strip()
    if not source:
        for key in ("url", "checkout_url", "openai_checkout_url"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                source = value.strip()
                break

    external = ""
    if source:
        external = rewrite_host(source, _OPENAI_HOST)
    if not external and session_id:
        fragment = fragment_from_client_secret(str(payload.get("client_secret") or ""), session_id)
        if fragment:
            external = rewrite_host(f"https://{_STRIPE_HOST}/c/pay/{session_id}{fragment}", _OPENAI_HOST)

    stripe_mirror = rewrite_host(external, _STRIPE_HOST) if external else ""
    internal = ""
    if session_id:
        internal = f"https://chatgpt.com/checkout/{entity}/{session_id}" if entity else (
            f"https://chatgpt.com/checkout/{session_id}"
        )

    return {
        "openai": external,
        "stripe": stripe_mirror,
        "external": external,
        "internal": internal,
        "checkout_session_id": session_id,
        "processor_entity": entity,
    }


# ─────────────────────────── HTTP layer ───────────────────────────


def checkout_headers(
    access_token: str,
    *,
    cookie_header: str = "",
    device_id: str = "",
    accept_language: str = "zh-CN,zh;q=0.9",
) -> dict[str, str]:
    """Headers for the chatgpt.com checkout call."""
    token = normalize_access_token(access_token)
    claims = token_claims(token)
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "authorization": f"Bearer {token}",
        "accept-language": accept_language or "zh-CN,zh;q=0.9",
        "oai-device-id": str(device_id or uuid4()),
        "origin": "https://chatgpt.com",
        "referer": "https://chatgpt.com/",
        "x-openai-target-path": CHECKOUT_PATH,
        "x-openai-target-route": CHECKOUT_PATH,
    }
    if claims.account_id:
        headers["chatgpt-account-id"] = claims.account_id
    cookies = str(cookie_header or "").strip()
    if cookies:
        headers["cookie"] = cookies
    return headers


def stripe_init_headers(publishable_key: str, user_agent: str = "") -> dict[str, str]:
    """Headers for the Stripe ``payment_pages/init`` call.

    No ``Origin`` header: Stripe treats a browser origin it cannot match as
    cross-site and rejects the init call.
    """
    headers = {
        "accept": "application/json",
        "authorization": f"Bearer {publishable_key}",
        "content-type": "application/x-www-form-urlencoded",
    }
    if user_agent:
        headers["user-agent"] = user_agent
    return headers


def _decode_json(content: bytes | str) -> Any:
    try:
        return json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return None


def _classify_checkout_error(detail: str, status: int) -> str:
    """Turn OpenAI's checkout error text into a stable error code.

    Two of the failures are worth telling apart because they need different
    fixes: a risk block is about the account/exit IP, a country mismatch is
    about the chosen payment preset.
    """
    lowered = str(detail or "").lower()
    if "billing country must match" in lowered:
        return "checkout_country_mismatch"
    if "unusual activity" in lowered:
        return "checkout_risk_blocked"
    if status == 429:
        return "checkout_rate_limited"
    return "checkout_http_failed"


def _looks_like_proxy_failure(exc: Exception) -> bool:
    """True when the request never reached OpenAI because the proxy is gone.

    Short-lived sticky proxies expire; the old entry stays in
    ``registered.extra.proxy`` and every later request dies with
    ``curl: (97) cannot complete SOCKS5 connection``. That is a proxy problem,
    not an account problem, so it gets its own error code.
    """
    text = f"{type(exc).__name__}: {exc}".lower()
    markers = (
        "curl: (97)", "cannot complete socks", "proxyerror",
        "proxy connect", "failed to perform",
    )
    return any(marker in text for marker in markers)


def detect_exit_country(proxy: str = "", timeout_seconds: float = 8.0) -> str:
    """Best-effort country of the IP this host exits from.

    Empty string when the lookup fails - it is only a hint, never a hard
    requirement. The result is cached per proxy for ten minutes so opening
    the dialog repeatedly does not hammer the geo endpoint.
    """
    key = str(proxy or "")
    now = time.monotonic()
    cached = _exit_country_cache.get(key)
    if cached and now - cached[0] < _EXIT_COUNTRY_TTL:
        return cached[1]

    country = ""
    session = None
    try:
        session = create_http_session(proxy=proxy or None, impersonate="chrome110")
        response = session.get(_EXIT_COUNTRY_URL, timeout=timeout_seconds)
        if 200 <= int(response.status_code) < 300:
            payload = json.loads(bytes(response.content or b"").decode("utf-8", "ignore"))
            if isinstance(payload, dict):
                country = str(payload.get("country") or "").strip().upper()
    except Exception:
        country = ""
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                pass

    _exit_country_cache[key] = (now, country)
    return country


def _post_checkout(
    session: Any,
    body: dict,
    headers: dict[str, str],
    timeout_seconds: float,
) -> dict:
    response = session.post(
        CHECKOUT_URL,
        headers=headers,
        data=json.dumps(body, ensure_ascii=False),
        timeout=timeout_seconds,
        allow_redirects=False,
    )
    status = int(response.status_code)
    content = bytes(response.content or b"")
    if status in {401, 403}:
        raise PaymentLinkError(
            "checkout_unauthorized" if status == 401 else "checkout_forbidden",
            http_status=status,
        )
    if not 200 <= status < 300:
        detail = content.decode("utf-8", errors="ignore")[:200]
        raise PaymentLinkError(
            _classify_checkout_error(detail, status),
            http_status=status,
            detail=detail,
        )
    payload = _decode_json(content)
    if not isinstance(payload, dict):
        raise PaymentLinkError("checkout_response_invalid", http_status=status)
    return payload


def _post_stripe_init(
    session: Any,
    session_id: str,
    publishable_key: str,
    locale: str,
    timeout_seconds: float,
) -> dict:
    url = f"{STRIPE_INIT_BASE}{quote(session_id, safe='')}/init"
    body = build_stripe_init_body(publishable_key, locale=locale)
    response = session.post(
        url,
        headers=stripe_init_headers(publishable_key),
        data=body,
        timeout=timeout_seconds,
        allow_redirects=False,
    )
    status = int(response.status_code)
    content = bytes(response.content or b"")
    if not 200 <= status < 300:
        raise PaymentLinkError(
            "stripe_init_http_failed",
            http_status=status,
            detail=content.decode("utf-8", errors="ignore")[:200],
        )
    payload = _decode_json(content)
    if not isinstance(payload, dict):
        raise PaymentLinkError("stripe_init_response_invalid", http_status=status)
    return payload


def generate_payment_link(
    access_token: str,
    *,
    cookie_header: str = "",
    device_id: str = "",
    proxy: str = "",
    plan: str = "plus",
    profile: str = "",
    country: str = "",
    currency: str = "",
    locale: str = "",
    workspace_name: str = "",
    interval: str = "month",
    seats: int = 2,
    promo_code: str = "",
    promo_campaign_id: str = "",
    timeout_seconds: float = 20.0,
    session_factory: Callable[[], Any] | None = None,
) -> PaymentLink:
    """Create a checkout session and return every link that belongs to it.

    Raises :class:`PaymentLinkError` when the checkout itself fails. A failing
    Stripe init call is *not* fatal: the link degrades to the URL OpenAI
    returned (or one rebuilt from the client secret) and the reason is reported
    through ``warnings``.
    """
    token = normalize_access_token(access_token)
    if not token:
        raise PaymentLinkError("access_token_missing")

    resolved = resolve_profile(profile=profile, country=country, currency=currency, locale=locale)
    body = build_checkout_body(
        plan,
        resolved.country,
        resolved.currency,
        workspace_name=workspace_name,
        interval=interval,
        seats=seats,
        promo_code=promo_code,
        promo_campaign_id=promo_campaign_id,
    )
    plan_key = normalize_plan(plan)

    started = time.monotonic()
    session = None
    warnings: list[str] = []
    try:
        session = (
            session_factory()
            if session_factory is not None
            else create_http_session(proxy=proxy or None, impersonate="chrome110")
        )
        try:
            payload = _post_checkout(
                session,
                body,
                checkout_headers(
                    token,
                    cookie_header=cookie_header,
                    device_id=device_id,
                ),
                timeout_seconds,
            )
        except PaymentLinkError:
            raise
        except Exception as exc:  # noqa: BLE001
            if _looks_like_proxy_failure(exc):
                raise PaymentLinkError("proxy_unavailable", detail=str(exc)[:200]) from exc
            raise

        session_id = extract_session_id(payload)
        if not session_id:
            raise PaymentLinkError("checkout_no_session")

        publishable_key = str(payload.get("publishable_key") or "").strip() or DEFAULT_STRIPE_PK
        hosted_url = ""
        try:
            stripe_payload = _post_stripe_init(
                session,
                session_id,
                publishable_key,
                resolved.locale,
                timeout_seconds,
            )
            for key in ("stripe_hosted_url", "hosted_url", "url"):
                value = stripe_payload.get(key)
                if isinstance(value, str) and value.strip():
                    hosted_url = value.strip()
                    break
            if not hosted_url:
                warnings.append("stripe_init_no_hosted_url")
        except PaymentLinkError as exc:
            warnings.append(f"stripe_init_failed:{exc.code}")
            logger.info("[payment] stripe init failed code=%s http=%s", exc.code, exc.http_status)

        urls = build_payment_urls(payload, resolved.country, hosted_url=hosted_url)
        url = urls.get("openai") or urls.get("internal") or ""
        if not url:
            raise PaymentLinkError("checkout_no_url")
        return PaymentLink(
            url=url,
            urls=urls,
            plan=plan_key,
            plan_name=body["plan_name"],
            country=resolved.country,
            currency=resolved.currency,
            locale=resolved.locale,
            checkout_session_id=session_id,
            processor_entity=urls.get("processor_entity", ""),
            stripe_hosted=bool(hosted_url),
            elapsed_ms=int((time.monotonic() - started) * 1000),
            warnings=warnings,
        )
    except PaymentLinkError as exc:
        if exc.code == "checkout_country_mismatch" and not exc.exit_country:
            exc.exit_country = detect_exit_country(proxy)
        raise
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                pass


def resolve_checkout_token(row: dict, *, proxy: str = "") -> tuple[str, str]:
    """Pick the access token the checkout endpoint actually accepts.

    The payments endpoint only accepts an access token minted by the **Codex
    client**. The token in ``registered.access_token`` comes from the web
    session endpoint and is rejected with

        HTTP 400 {"detail": "Our systems have detected unusual activity. ..."}

    which reads like an account-level fraud block but is only a token-flavour
    problem. Measured 2026-11-07: web-session token -> 400, Codex token ->
    200, on the *same* account.

    Returns ``(access_token, source)`` where source is ``codex_refresh``,
    ``session`` or ``codex_refresh_failed``.
    """
    stored = str((row or {}).get("access_token") or "").strip()
    refresh = str((row or {}).get("refresh_token") or "").strip()
    if refresh:
        try:
            fresh = refresh_codex_token(refresh, proxy=proxy)
            token = str(fresh.get("access_token") or "").strip()
            if token:
                # The grant rotates the refresh token. Persist it right away:
                # keeping the old one makes the next refresh fail with
                # "refresh_token_reused" and the account loses the ability to
                # mint Codex tokens entirely.
                new_refresh = str(fresh.get("refresh_token") or "").strip()
                new_id = str(fresh.get("id_token") or "").strip()
                try:
                    from regix.storage import facade as db

                    db.update_registered_tokens(
                        str((row or {}).get("email") or ""),
                        access_token=token,
                        refresh_token=new_refresh or None,
                        id_token=new_id or None,
                    )
                except Exception as exc:  # noqa: BLE001 - never block the link
                    logger.warning("[payment] 新 token 落库失败: %s", exc)
                return token, "codex_refresh"
            logger.warning("[payment] Codex token 刷新返回里没有 access_token")
        except Exception as exc:  # noqa: BLE001 - fall back to the stored token
            logger.warning("[payment] Codex token 刷新失败，改用库里的 access_token: %s", exc)
            return stored, "codex_refresh_failed"
    return stored, "session"


def generate_payment_link_for_account(
    email: str,
    *,
    plan: str = "plus",
    profile: str = "",
    country: str = "",
    currency: str = "",
    locale: str = "",
    workspace_name: str = "",
    interval: str = "month",
    seats: int = 2,
    promo_code: str = "",
    promo_campaign_id: str = "",
    proxy: str = "",
    timeout_seconds: float = 20.0,
) -> PaymentLink:
    """Generate a payment link for an account stored in the local database.

    The access token, session cookies and device id come from the stored row;
    the proxy falls back to the proxy the account was registered with. The
    token never travels through the API layer.
    """
    from regix.storage import facade as db

    normalized = str(email or "").strip().lower()
    if not normalized:
        raise PaymentLinkError("email_missing")
    row = db.get_registered(normalized)
    if not row:
        raise PaymentLinkError("account_not_found")

    extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
    effective_proxy = str(proxy or "").strip() or str(extra.get("proxy") or "").strip()

    token, token_source = resolve_checkout_token(row, proxy=effective_proxy)
    if not token:
        raise PaymentLinkError("access_token_missing")

    try:
        link = generate_payment_link(
            token,
            cookie_header=str(row.get("cookie_header") or ""),
            device_id=str(row.get("device_id") or ""),
            proxy=effective_proxy,
            plan=plan,
            profile=profile,
            country=country,
            currency=currency,
            locale=locale,
            workspace_name=workspace_name,
            interval=interval,
            seats=seats,
            promo_code=promo_code,
            promo_campaign_id=promo_campaign_id,
            timeout_seconds=timeout_seconds,
        )
    except PaymentLinkError as exc:
        # 把「token 用错了」从「账号被风控」里拆出来，否则用户看到的是
        # "unusual activity"，会以为是号废了。
        if exc.code == "checkout_risk_blocked" and token_source == "session":
            raise PaymentLinkError(
                "codex_token_missing",
                http_status=exc.http_status,
                detail=exc.detail,
            ) from exc
        if exc.code == "checkout_risk_blocked" and token_source == "codex_refresh_failed":
            raise PaymentLinkError(
                "codex_token_refresh_failed",
                http_status=exc.http_status,
                detail=exc.detail,
            ) from exc
        raise
    warnings = getattr(link, "warnings", None)
    if isinstance(warnings, list):
        warnings.append(f"token_source:{token_source}")
    return link
