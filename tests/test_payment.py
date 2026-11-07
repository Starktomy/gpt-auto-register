"""Tests for the Plus/Team payment-link generation flow."""

from __future__ import annotations

import base64
import json

import pytest

from regix.services.payment import (
    CHECKOUT_URL,
    DEFAULT_STRIPE_PK,
    PaymentLinkError,
    STRIPE_INIT_BASE,
    build_checkout_body,
    build_payment_urls,
    build_stripe_init_body,
    checkout_headers,
    extract_entity_from_url,
    extract_session_id,
    fragment_from_client_secret,
    generate_payment_link,
    generate_payment_link_for_account,
    infer_merchant_path,
    normalize_plan,
    resolve_profile,
    rewrite_host,
)


def _jwt(account_id: str = "acct-1") -> str:
    payload = {
        "https://api.openai.com/auth": {
            "chatgpt_account_id": account_id,
            "chatgpt_plan_type": "free",
        }
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


class _Response:
    def __init__(self, status_code: int = 200, payload=None, body: bytes = b""):
        self.status_code = status_code
        if payload is not None:
            self.content = json.dumps(payload).encode()
        else:
            self.content = body


class _Session:
    """Minimal curl_cffi-like session that records the calls it received."""

    def __init__(self, checkout_payload=None, stripe_payload=None, stripe_status=200):
        self.checkout_payload = checkout_payload if checkout_payload is not None else {}
        self.stripe_payload = stripe_payload if stripe_payload is not None else {}
        self.stripe_status = stripe_status
        self.calls: list[dict] = []
        self.closed = False

    def post(self, url, headers=None, data=None, timeout=None, allow_redirects=None):
        self.calls.append({"url": url, "headers": headers or {}, "data": data})
        if url == CHECKOUT_URL:
            return _Response(200, self.checkout_payload)
        if url.startswith(STRIPE_INIT_BASE):
            return _Response(self.stripe_status, self.stripe_payload)
        raise AssertionError(f"unexpected url: {url}")

    def close(self):
        self.closed = True


# ─────────────────────── pure helpers ───────────────────────


def test_normalize_plan_accepts_aliases():
    assert normalize_plan("") == "plus"
    assert normalize_plan("Plus") == "plus"
    assert normalize_plan("team") == "team"
    assert normalize_plan("chatgptteamplan") == "team"
    assert normalize_plan("Business") == "team"


def test_resolve_profile_precedence():
    assert resolve_profile(profile="paypal_de").country == "DE"
    # explicit country wins over the profile
    picked = resolve_profile(profile="paypal_de", country="US")
    assert (picked.country, picked.currency) == ("US", "EUR")
    # no preset at all falls back to the OpenAI default region
    assert resolve_profile().country == "US"
    with pytest.raises(PaymentLinkError):
        resolve_profile(profile="does-not-exist")


def test_fragment_from_client_secret():
    secret = "pi_1_secret/cs_live_abc_secret_fidkdWx1"
    assert fragment_from_client_secret(secret, "cs_live_abc") == "#fidkdWx1"
    assert fragment_from_client_secret("nothing", "cs_live_abc") == ""
    assert fragment_from_client_secret(secret, "cs_live_other") == ""


def test_extract_session_id_and_entity():
    assert extract_session_id({"checkout_session_id": "cs_live_1"}) == "cs_live_1"
    assert extract_session_id({"url": "https://x/c/pay/cs_test_9#fid=1"}) == "cs_test_9"
    assert extract_session_id({}) == ""
    assert extract_entity_from_url("https://chatgpt.com/checkout/openai_ie/cs_live_1") == "openai_ie"
    assert extract_entity_from_url("nope") == ""


def test_infer_merchant_path():
    assert infer_merchant_path("DE") == "openai_ie"
    assert infer_merchant_path("gb") == "openai_ie"
    assert infer_merchant_path("US") == "openai_llc"
    assert infer_merchant_path("ID") == "openai_llc"


def test_rewrite_host_keeps_path_and_fragment():
    url = "https://checkout.stripe.com/c/pay/cs_live_1#fidkdWx1"
    assert rewrite_host(url, "pay.openai.com") == "https://pay.openai.com/c/pay/cs_live_1#fidkdWx1"
    assert rewrite_host(url, "checkout.stripe.com") == url
    assert rewrite_host("not-a-url", "pay.openai.com") == "not-a-url"


def test_build_checkout_body_plus_has_no_team_fields():
    body = build_checkout_body("plus", "DE", "EUR")
    assert body["plan_name"] == "chatgptplusplan"
    assert body["checkout_ui_mode"] == "hosted"
    assert body["billing_details"] == {"country": "DE", "currency": "EUR"}
    assert body["cancel_url"] == "https://chatgpt.com/#pricing"
    assert "team_plan_data" not in body
    # blacklisted fields must never be sent
    for banned in ("success_url", "entry_point", "promo_campaign", "locale", "check_card_proxy"):
        assert banned not in body


def test_build_checkout_body_team():
    body = build_checkout_body(
        "team", "US", "USD", workspace_name="Lab", interval="year", seats=5, promo_code="SAVE"
    )
    assert body["plan_name"] == "chatgptteamplan"
    assert body["team_plan_data"] == {
        "workspace_name": "Lab",
        "price_interval": "year",
        "seat_quantity": 5,
    }
    assert body["promo_code"] == "SAVE"


def test_build_stripe_init_body_has_required_fields():
    body = build_stripe_init_body("pk_live_x", locale="pt-BR", stripe_js_id="js-1")
    assert "elements_session_client%5Bclient_betas%5D%5B0%5D=custom_checkout_server_updates_1" in body
    assert "elements_session_client%5Bclient_betas%5D%5B1%5D=custom_checkout_manual_approval_1" in body
    assert "elements_session_client%5Belements_init_source%5D=custom_checkout" in body
    assert "elements_session_client%5Breferrer_host%5D=chatgpt.com" in body
    assert "elements_session_client%5Blocale%5D=pt-BR" in body
    assert "key=pk_live_x" in body
    assert "_stripe_version=2025-03-31.basil" in body


def test_checkout_headers_carry_token_and_cookie():
    headers = checkout_headers(
        f"Bearer {_jwt()}", cookie_header="a=1; b=2", device_id="dev-1"
    )
    assert headers["authorization"] == f"Bearer {_jwt()}"
    assert headers["cookie"] == "a=1; b=2"
    assert headers["oai-device-id"] == "dev-1"
    assert headers["chatgpt-account-id"] == "acct-1"
    assert headers["content-type"] == "application/json"


def test_build_payment_urls_uses_stripe_hosted_url():
    payload = {
        "checkout_session_id": "cs_live_1",
        "processor_entity": "openai_ie",
        "url": "https://pay.openai.com/c/pay/cs_live_1#old",
    }
    urls = build_payment_urls(payload, "DE", hosted_url="https://checkout.stripe.com/c/pay/cs_live_1#fidkdWx1")
    assert urls["openai"] == "https://pay.openai.com/c/pay/cs_live_1#fidkdWx1"
    assert urls["stripe"] == "https://checkout.stripe.com/c/pay/cs_live_1#fidkdWx1"
    assert urls["internal"] == "https://chatgpt.com/checkout/openai_ie/cs_live_1"
    assert urls["checkout_session_id"] == "cs_live_1"


def test_build_payment_urls_falls_back_to_client_secret():
    payload = {
        "checkout_session_id": "cs_live_2",
        "client_secret": "pi_1_secret/cs_live_2_secret_fidXYZ",
    }
    urls = build_payment_urls(payload, "US")
    assert urls["openai"] == "https://pay.openai.com/c/pay/cs_live_2#fidXYZ"
    # no entity in the response -> inferred from the country
    assert urls["internal"] == "https://chatgpt.com/checkout/openai_llc/cs_live_2"


# ─────────────────────── end-to-end (mocked HTTP) ───────────────────────


def test_generate_payment_link_happy_path():
    session = _Session(
        checkout_payload={
            "checkout_session_id": "cs_live_1",
            "client_secret": "pi_1_secret/cs_live_1_secret_fidZZZ",
            "publishable_key": "pk_live_from_openai",
            "url": "https://pay.openai.com/c/pay/cs_live_1",
            "processor_entity": "openai_ie",
        },
        stripe_payload={"stripe_hosted_url": "https://checkout.stripe.com/c/pay/cs_live_1#fidZZZ"},
    )
    link = generate_payment_link(
        _jwt(),
        cookie_header="__Secure-next-auth.session-token=abc",
        device_id="dev-9",
        plan="plus",
        profile="paypal_de",
        session_factory=lambda: session,
    )
    assert link.url == "https://pay.openai.com/c/pay/cs_live_1#fidZZZ"
    assert link.plan == "plus"
    assert link.country == "DE"
    assert link.currency == "EUR"
    assert link.checkout_session_id == "cs_live_1"
    assert link.stripe_hosted is True
    assert link.warnings == []

    # two calls: chatgpt checkout + stripe init
    assert len(session.calls) == 2
    checkout_call, stripe_call = session.calls
    assert checkout_call["url"] == CHECKOUT_URL
    body = json.loads(checkout_call["data"])
    assert body["plan_name"] == "chatgptplusplan"
    assert body["billing_details"] == {"country": "DE", "currency": "EUR"}
    assert checkout_call["headers"]["authorization"].startswith("Bearer ")
    assert checkout_call["headers"]["cookie"] == "__Secure-next-auth.session-token=abc"

    assert stripe_call["url"] == f"{STRIPE_INIT_BASE}cs_live_1/init"
    assert stripe_call["headers"]["authorization"] == "Bearer pk_live_from_openai"
    assert "key=pk_live_from_openai" in stripe_call["data"]


def test_generate_payment_link_team_plan_sends_seats():
    session = _Session(
        checkout_payload={"checkout_session_id": "cs_live_3"},
        stripe_payload={"stripe_hosted_url": "https://checkout.stripe.com/c/pay/cs_live_3#f"},
    )
    generate_payment_link(
        _jwt(),
        plan="team",
        country="US",
        currency="USD",
        seats=3,
        interval="year",
        workspace_name="Lab",
        session_factory=lambda: session,
    )
    body = json.loads(session.calls[0]["data"])
    assert body["plan_name"] == "chatgptteamplan"
    assert body["team_plan_data"]["seat_quantity"] == 3
    assert body["team_plan_data"]["price_interval"] == "year"


def test_generate_payment_link_survives_stripe_failure():
    session = _Session(
        checkout_payload={
            "checkout_session_id": "cs_live_4",
            "url": "https://pay.openai.com/c/pay/cs_live_4",
        },
        stripe_status=500,
    )
    link = generate_payment_link(_jwt(), session_factory=lambda: session)
    assert link.url == "https://pay.openai.com/c/pay/cs_live_4"
    assert link.stripe_hosted is False
    assert any(w.startswith("stripe_init_failed") for w in link.warnings)


def test_generate_payment_link_uses_default_publishable_key():
    session = _Session(
        checkout_payload={"checkout_session_id": "cs_live_5"},
        stripe_payload={"stripe_hosted_url": "https://checkout.stripe.com/c/pay/cs_live_5#f"},
    )
    generate_payment_link(_jwt(), session_factory=lambda: session)
    assert session.calls[1]["headers"]["authorization"] == f"Bearer {DEFAULT_STRIPE_PK}"


def test_generate_payment_link_requires_token():
    with pytest.raises(PaymentLinkError) as excinfo:
        generate_payment_link("", session_factory=lambda: _Session())
    assert excinfo.value.code == "access_token_missing"


def test_generate_payment_link_reports_checkout_failure():
    class _Failing(_Session):
        def post(self, url, headers=None, data=None, timeout=None, allow_redirects=None):
            if url == CHECKOUT_URL:
                return _Response(401, body=b'{"detail":"unauthorized"}')
            return super().post(url, headers=headers, data=data, timeout=timeout, allow_redirects=allow_redirects)

    with pytest.raises(PaymentLinkError) as excinfo:
        generate_payment_link(_jwt(), session_factory=_Failing)
    assert excinfo.value.code == "checkout_unauthorized"
    assert excinfo.value.http_status == 401


def test_generate_payment_link_for_account_uses_stored_credentials(clean_db, monkeypatch):
    clean_db.save_registered(
        {
            "email": "demo@example.com",
            "access_token": _jwt("acct-7"),
            "cookie_header": "cf_clearance=zzz",
            "device_id": "dev-42",
        }
    )
    seen: dict = {}

    def _fake_link(token, **kwargs):
        seen["token"] = token
        seen.update(kwargs)
        return "https://pay.openai.com/c/pay/cs_live_9#f"

    class _Link:
        def to_dict(self):
            return {"url": "https://pay.openai.com/c/pay/cs_live_9#f"}

    def _fake(*args, **kwargs):
        seen["args"] = args
        seen.update(kwargs)
        return _Link()

    monkeypatch.setattr("regix.services.payment.generate_payment_link", _fake)
    link = generate_payment_link_for_account("demo@example.com", plan="plus")
    assert link.to_dict()["url"].endswith("#f")
    assert seen["args"][0] == _jwt("acct-7")
    assert seen["cookie_header"] == "cf_clearance=zzz"
    assert seen["device_id"] == "dev-42"


def test_generate_payment_link_for_account_missing(clean_db):
    with pytest.raises(PaymentLinkError) as excinfo:
        generate_payment_link_for_account("nobody@example.com")
    assert excinfo.value.code == "account_not_found"


# ─────────────────────── error classification ───────────────────────


def test_classify_checkout_error():
    from regix.services.payment import _classify_checkout_error

    assert _classify_checkout_error("Billing country must match request country.", 400) == (
        "checkout_country_mismatch"
    )
    assert _classify_checkout_error("Our systems have detected unusual activity.", 400) == (
        "checkout_risk_blocked"
    )
    assert _classify_checkout_error("slow down", 429) == "checkout_rate_limited"
    assert _classify_checkout_error("boom", 500) == "checkout_http_failed"


def test_generate_payment_link_reports_country_mismatch(monkeypatch):
    monkeypatch.setattr("regix.services.payment.detect_exit_country", lambda *a, **k: "US")

    class _Mismatch(_Session):
        def post(self, url, headers=None, data=None, timeout=None, allow_redirects=None):
            if url == CHECKOUT_URL:
                return _Response(400, body=b'{"detail":"Billing country must match request country."}')
            return super().post(url, headers=headers, data=data, timeout=timeout, allow_redirects=allow_redirects)

    with pytest.raises(PaymentLinkError) as excinfo:
        generate_payment_link(_jwt(), session_factory=_Mismatch)
    assert excinfo.value.code == "checkout_country_mismatch"
    assert excinfo.value.exit_country == "US"


def test_generate_payment_link_reports_risk_block():
    class _Blocked(_Session):
        def post(self, url, headers=None, data=None, timeout=None, allow_redirects=None):
            if url == CHECKOUT_URL:
                return _Response(400, body=b'{"detail":"Our systems have detected unusual activity. Please try again later."}')
            return super().post(url, headers=headers, data=data, timeout=timeout, allow_redirects=allow_redirects)

    with pytest.raises(PaymentLinkError) as excinfo:
        generate_payment_link(_jwt(), session_factory=_Blocked)
    assert excinfo.value.code == "checkout_risk_blocked"


def test_detect_exit_country_reads_the_geo_response(monkeypatch):
    import regix.services.payment as payment

    class _Geo(_Session):
        def get(self, url, timeout=None, allow_redirects=None):
            return _Response(200, {"country": "de"})

    monkeypatch.setattr(payment, "create_http_session", lambda **kwargs: _Geo())
    monkeypatch.setattr(payment, "_exit_country_cache", {})
    assert payment.detect_exit_country() == "DE"


def test_detect_exit_country_swallows_failures(monkeypatch):
    import regix.services.payment as payment

    class _Broken(_Session):
        def get(self, url, timeout=None, allow_redirects=None):
            raise OSError("no network")

    monkeypatch.setattr(payment, "create_http_session", lambda **kwargs: _Broken())
    monkeypatch.setattr(payment, "_exit_country_cache", {})
    assert payment.detect_exit_country() == ""


# ─────────────────────── API layer ───────────────────────


def test_payment_api_exposes_profiles_and_link(monkeypatch, clean_db):
    import regix.api.app as app_module
    from fastapi.testclient import TestClient

    monkeypatch.setattr(app_module, "ADMIN_PASSWORD", "")
    app_module._AUTH_FAILED_TIMES.clear()
    client = TestClient(app_module.app)

    profiles = client.get("/api/payment/profiles")
    assert profiles.status_code == 200
    payload = profiles.json()
    assert payload["ok"] is True
    assert any(item["id"] == "paypal_de" for item in payload["items"])

    clean_db.save_registered({"email": "demo@example.com", "access_token": _jwt()})
    captured: dict = {}

    class _Link:
        def to_dict(self):
            return {
                "url": "https://pay.openai.com/c/pay/cs_live_1#f",
                "urls": {"openai": "https://pay.openai.com/c/pay/cs_live_1#f"},
                "plan": "plus",
            }

    def _fake(*args, **kwargs):
        captured["email"] = args[0] if args else kwargs.get("email", "")
        captured.update(kwargs)
        return _Link()

    monkeypatch.setattr("regix.api.routers.payment.generate_payment_link_for_account", _fake)
    response = client.post("/api/payment/link", json={"email": "demo@example.com", "plan": "plus"})
    assert response.status_code == 200
    assert response.json()["data"]["url"].startswith("https://pay.openai.com/")
    assert captured["email"] == "demo@example.com"


def test_payment_api_reports_errors_as_400(monkeypatch, clean_db):
    import regix.api.app as app_module
    from fastapi.testclient import TestClient

    monkeypatch.setattr(app_module, "ADMIN_PASSWORD", "")
    app_module._AUTH_FAILED_TIMES.clear()
    client = TestClient(app_module.app)

    def _fake(*args, **kwargs):
        raise PaymentLinkError("checkout_risk_blocked")

    monkeypatch.setattr("regix.api.routers.payment.generate_payment_link_for_account", _fake)
    response = client.post("/api/payment/link", json={"email": "demo@example.com"})
    assert response.status_code == 400
    assert "风控" in response.json()["detail"]


def test_phone_rejection_patterns_cover_human_readable_text():
    """OpenAI rejects a recycled number with plain English, not just a code."""
    from regix.openai.challenges import is_phone_rejected

    # the exact body OpenAI returned for a recycled HeroSMS number
    assert is_phone_rejected("Phone number already in use. Please use a different phone number.")
    assert is_phone_rejected("phone_number_already_in_use")
    assert is_phone_rejected("This phone number is disallowed_phone")
    assert not is_phone_rejected("too many phone verification")
    assert not is_phone_rejected("")


# ─────────────────────── promo + Codex token resolution ───────────────────────


def test_build_checkout_body_supports_promo_campaign():
    body = build_checkout_body(
        "plus", "US", "USD",
        promo_code="SAVE20",
        promo_campaign_id="plus-1-month-free",
    )
    assert body["promo_code"] == "SAVE20"
    assert body["promo_campaign"] == {
        "promo_campaign_id": "plus-1-month-free",
        "is_coupon_from_query_param": False,
    }


def test_build_checkout_body_omits_empty_promos():
    body = build_checkout_body("plus", "US", "USD")
    assert "promo_code" not in body
    assert "promo_campaign" not in body


def test_resolve_checkout_token_prefers_codex_refresh(monkeypatch):
    from regix.services import payment as payment_module

    monkeypatch.setattr(
        payment_module, "refresh_codex_token",
        lambda rt, proxy="", timeout=30: {"access_token": "codex-at"},
    )
    token, source = payment_module.resolve_checkout_token(
        {"access_token": "web-at", "refresh_token": "rt"}
    )
    assert (token, source) == ("codex-at", "codex_refresh")


def test_resolve_checkout_token_falls_back_when_refresh_fails(monkeypatch):
    from regix.services import payment as payment_module

    def _boom(rt, proxy="", timeout=30):
        raise RuntimeError("expired")

    monkeypatch.setattr(payment_module, "refresh_codex_token", _boom)
    token, source = payment_module.resolve_checkout_token(
        {"access_token": "web-at", "refresh_token": "rt"}
    )
    assert (token, source) == ("web-at", "codex_refresh_failed")


def test_resolve_checkout_token_uses_session_when_no_refresh_token():
    from regix.services.payment import resolve_checkout_token

    assert resolve_checkout_token({"access_token": "web-at"}) == ("web-at", "session")


def test_risk_block_without_refresh_token_is_reported_as_missing_codex_token(monkeypatch, clean_db):
    """The payments endpoint only accepts Codex tokens; a web-session token
    produces the same "unusual activity" body as a real fraud block. The
    service must tell those apart so the UI can offer a re-login."""
    import regix.services.payment as payment_module

    clean_db.save_registered(
        {"email": "no-rt@example.com", "access_token": _jwt(), "refresh_token": ""}
    )

    def _risk_block(*args, **kwargs):
        raise PaymentLinkError("checkout_risk_blocked", http_status=400)

    monkeypatch.setattr(payment_module, "generate_payment_link", _risk_block)
    with pytest.raises(PaymentLinkError) as excinfo:
        payment_module.generate_payment_link_for_account("no-rt@example.com")
    assert excinfo.value.code == "codex_token_missing"


def test_check_plus_uses_registration_proxy(monkeypatch, clean_db):
    """A check without an explicit proxy must stay on the account's own IP."""
    import regix.api.app as app_module
    from fastapi.testclient import TestClient

    clean_db.save_registered(
        {
            "email": "proxy@example.com",
            "access_token": _jwt(),
            "refresh_token": "rt",
            "proxy": "socks5h://user:pass@proxy.example:1080",
        }
    )
    seen: dict = {}

    def _fake_check(email, **kwargs):
        seen.update(kwargs)
        return {"status": "free", "label": "Free"}

    monkeypatch.setattr("regix.services.plan_check.check_registered_plan", _fake_check)
    monkeypatch.setattr(app_module, "ADMIN_PASSWORD", "")
    app_module._AUTH_FAILED_TIMES.clear()
    client = TestClient(app_module.app)
    resp = client.post("/api/registered/check_plus", json={"emails": ["proxy@example.com"]})
    assert resp.status_code == 200
    assert seen["proxy_url"] == "socks5h://user:pass@proxy.example:1080"
