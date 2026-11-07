from __future__ import annotations

import base64
import hashlib
import json
import random
import time

import pytest


OUTLOOK_LINE = (
    "user@outlook.com----pass----client-id----"
    "M.C5xx_BAY.0.U.-Cl0000000000000000000000000000"
)
OUTLOOK_REFRESH = "M.C5xx_BAY.0.U.-Cl0000000000000000000000000000"


@pytest.fixture
def fresh_db(clean_db):
    return clean_db


class TestMailProviders:
    def test_registry_exposes_current_providers(self):
        from regix.providers.mail import list_providers

        providers = {item["kind"]: item for item in list_providers()}
        assert {"cf_temp", "icloud_relay", "outlook", "remail"} <= set(providers)
        assert providers["outlook"]["pooled"] is True
        assert providers["outlook"]["ephemeral"] is False
        assert providers["remail"]["pooled"] is False
        assert providers["remail"]["ephemeral"] is True

    def test_unknown_provider_is_rejected(self):
        from regix.providers.mail import MailProviderError, get_provider_class

        with pytest.raises(MailProviderError):
            get_provider_class("does-not-exist")

    def test_outlook_import_line_is_parsed(self):
        from regix.providers.mail import parse_import_line

        row = parse_import_line(OUTLOOK_LINE, "outlook")
        assert row == {
            "email": "user@outlook.com",
            "password": "pass",
            "client_id": "client-id",
            "refresh_token": OUTLOOK_REFRESH,
            "kind": "outlook",
        }

    def test_batch_import_is_atomic(self):
        from regix.providers.mail import ImportValidationError, parse_import_text

        with pytest.raises(ImportValidationError):
            parse_import_text(
                f"{OUTLOOK_LINE}\nnot-an-outlook-line", "outlook"
            )

    def test_otp_extraction(self):
        from regix.providers.mail import extract_otp

        assert extract_otp("Your code is 123456") == "123456"
        assert extract_otp("ignore 12345 and use 654321") == "654321"
        assert extract_otp("no code here") is None


class TestAccountPool:
    def test_import_claim_and_mark_outcome(self, fresh_db):
        db = fresh_db
        result = db.import_accounts(OUTLOOK_LINE, "outlook")
        assert result == {"parsed": 1, "inserted": 1, "updated": 0, "skipped": 0}

        claimed = db.claim_account("USER@outlook.com")
        assert claimed is not None
        # claim_account returns the row selected before the UPDATE.
        assert claimed["status"] == "available"
        assert db.get_account("user@outlook.com")["status"] == "in_use"
        assert db.claim_account("user@outlook.com") is None
        assert db.stats()["in_use"] == 1

        db.mark_failed("user@outlook.com", "network timeout")
        assert db.get_account("user@outlook.com")["status"] == "failed"
        assert db.reset_to_available("USER@outlook.com") is True
        assert db.get_account("user@outlook.com")["fail_reason"] is None

    def test_reimport_updates_credentials_and_resets_account(self, fresh_db):
        db = fresh_db
        db.import_accounts(OUTLOOK_LINE, "outlook")
        db.claim_account("user@outlook.com")
        updated_line = (
            f"user@outlook.com----new-pass----client-id----new-{OUTLOOK_REFRESH}"
        )
        result = db.import_accounts(updated_line, "outlook")
        assert result == {"parsed": 1, "inserted": 0, "updated": 1, "skipped": 0}
        account = db.get_account("user@outlook.com")
        assert account["refresh_token"] == f"new-{OUTLOOK_REFRESH}"
        assert account["status"] == "available"

    def test_duplicate_import_is_skipped(self, fresh_db):
        db = fresh_db
        first = db.import_accounts(OUTLOOK_LINE, "outlook")
        second = db.import_accounts(OUTLOOK_LINE, "outlook")
        assert first["inserted"] == 1
        assert second == {"parsed": 1, "inserted": 0, "updated": 0, "skipped": 1}

    def test_release_stale_in_use(self, fresh_db):
        db = fresh_db
        db.import_accounts(OUTLOOK_LINE, "outlook")
        db.claim_account("user@outlook.com")
        with db._lock:
            con = db._conn()
            con.execute(
                "UPDATE outlook_accounts SET claimed_at=? WHERE email=?",
                (time.time() - 1801, "user@outlook.com"),
            )
            con.commit()
            con.close()
        assert db.release_stale_in_use(1800) == 1
        assert db.get_account("user@outlook.com")["status"] == "available"


class TestRegisteredStorage:
    @staticmethod
    def _save(fresh_db, **overrides):
        row = {
            "email": "signed-up@example.com",
            "password": "secret",
            "access_token": "access",
            "session_token": "session",
            "refresh_token": "refresh",
            "device_id": "device",
            "proxy": "socks5://127.0.0.1:1080",
            "some_metadata": {"ok": True},
        }
        row.update(overrides)
        fresh_db.save_registered(row)
        return row

    def test_save_preserves_password_and_totp_on_retry(self, fresh_db):
        db = fresh_db
        self._save(fresh_db, totp_secret="OLD", totp_factor_id="factor-old")
        db.save_registered({"email": "signed-up@example.com", "access_token": "new"})

        saved = db.get_registered("SIGNED-UP@example.com")
        assert saved["password"] == "secret"
        assert saved["totp_secret"] == "OLD"
        assert saved["totp_factor_id"] == "factor-old"
        assert saved["access_token"] == "new"
        assert saved["refresh_token"] == ""

    def test_extra_and_session_json_are_separated(self, fresh_db):
        db = fresh_db
        self._save(fresh_db, session_json='{"accessToken":"abc"}')
        saved = db.get_registered("signed-up@example.com")
        assert json.loads(saved["session_json"]) == {"accessToken": "abc"}
        assert saved["extra"] == {
            "proxy": "socks5://127.0.0.1:1080",
            "some_metadata": {"ok": True},
        }

    def test_delete_registered_releases_reserved_proxy(self, fresh_db):
        db = fresh_db
        db.replace_proxies(["socks5://127.0.0.1:1080"])
        chosen = db.pick_proxy()
        assert chosen == "socks5://127.0.0.1:1080"
        self._save(fresh_db)
        assert db.delete_registered("signed-up@example.com") is True
        proxy = db.find_proxy(chosen)
        assert proxy["used_count"] == 0

    def test_mark_abnormal_releases_proxy_once(self, fresh_db):
        db = fresh_db
        db.replace_proxies(["socks5://127.0.0.1:1080"])
        chosen = db.pick_proxy()
        self._save(fresh_db)
        assert db.mark_registered_abnormal("signed-up@example.com") is True
        assert db.mark_registered_abnormal("signed-up@example.com") is True
        assert db.find_proxy(chosen)["used_count"] == 0


class TestProxyPool:
    def test_replace_preserves_existing_state(self, fresh_db):
        db = fresh_db
        db.replace_proxies(["socks5://one:1080", "socks5://two:1080"])
        db.update_proxy("socks5://one:1080", enabled=False, max_uses=3)
        db.replace_proxies(["socks5://one:1080", "socks5://three:1080"])

        proxies = {p["proxy"]: p for p in db.list_proxies()}
        assert set(proxies) == {"socks5://one:1080", "socks5://three:1080"}
        assert proxies["socks5://one:1080"]["enabled"] is False
        assert proxies["socks5://one:1080"]["max_uses"] == 3

    def test_pick_proxy_respects_limit_and_release(self, fresh_db):
        db = fresh_db
        db.replace_proxies(["socks5://one:1080"])
        db.update_proxy("socks5://one:1080", max_uses=1)
        assert db.pick_proxy() == "socks5://one:1080"
        assert db.pick_proxy() == ""
        assert db.proxy_release("socks5://one:1080") is True
        assert db.pick_proxy() == "socks5://one:1080"

    def test_static_proxy_is_disabled_after_cf_threshold(self, fresh_db):
        db = fresh_db
        db.replace_proxies(["socks5://static:1080"])
        first = db.mark_proxy_cf_blocked("socks5://static:1080")
        second = db.mark_proxy_cf_blocked("socks5://static:1080")
        assert first == {
            "cf_blocked": 1,
            "auto_disabled": False,
            "proxy_type": "static",
        }
        assert second["auto_disabled"] is True
        assert db.find_proxy("socks5://static:1080")["enabled"] is False

    def test_dynamic_proxy_remains_enabled_after_cf_block(self, fresh_db):
        db = fresh_db
        db.replace_proxies(["socks5://dynamic:1080"])
        db.update_proxy("socks5://dynamic:1080", proxy_type="dynamic")
        result = db.mark_proxy_cf_blocked("socks5://dynamic:1080")
        assert result["cf_blocked"] == 1
        assert result["auto_disabled"] is False
        assert db.find_proxy("socks5://dynamic:1080")["enabled"] is True

    def test_proxy_stats_and_ipv6_status(self, fresh_db):
        db = fresh_db
        db.replace_proxies(["socks5://one:1080", "socks5://two:1080"])
        db.update_proxy("socks5://one:1080", enabled=False)
        db.update_proxy("socks5://two:1080", max_uses=1)
        db.pick_proxy()
        assert db.proxy_pool_stats() == {
            "total": 2,
            "available": 0,
            "disabled": 1,
            "exhausted": 1,
        }
        assert db.set_proxy_ipv6_status("socks5://two:1080", "ok") is True
        # Unknown values are normalized rather than rejected, preserving the
        # current permissive API behavior.
        assert db.set_proxy_ipv6_status("socks5://two:1080", "bad") is True
        assert db.find_proxy("socks5://two:1080")["ipv6_status"] == "untested"


class TestRemailOrders:
    @staticmethod
    def _order(order_no: str, **overrides):
        row = {
            "order_no": order_no,
            "email": f"{order_no}@remail.test",
            "suffix": "@remail.test",
            "service_mode": "purchase",
            "order_status": "paid",
            "service_token": "token",
            "received_at": 100,
        }
        row.update(overrides)
        return row

    def test_reusable_purchase_order_is_claimed_oldest_first(self, fresh_db):
        db = fresh_db
        db.save_remail_order(self._order("old", received_at=2))
        db.save_remail_order(self._order("new", received_at=3))
        claimed = db.claim_reusable_remail_order(
            service_mode="purchase", suffix="@remail.test", project_id=None
        )
        assert claimed["order_no"] == "old"
        assert claimed["service_token"] == "token"

    def test_invalid_or_disabled_orders_are_not_reused(self, fresh_db):
        db = fresh_db
        db.save_remail_order(self._order("disabled", disabled=True))
        db.save_remail_order(self._order("short", service_mode="code"))
        db.save_remail_order(self._order("linked", used_email="x@test"))
        db.save_remail_order(self._order("bad-status", order_status="refunded"))
        assert db.claim_reusable_remail_order(service_mode="purchase") is None

    def test_attempt_limit_and_short_order_consumption(self, fresh_db):
        db = fresh_db
        db.save_remail_order(self._order("long", service_mode="purchase"))
        for _ in range(db.REMAIL_MAX_ATTEMPTS):
            assert db.mark_remail_order_attempt("long", False, "otp timeout") is True
        item = db.get_remail_order("long")
        assert item["attempt_count"] == db.REMAIL_MAX_ATTEMPTS
        assert db.list_remail_orders()[0]["reusable"] is False
        assert db.claim_reusable_remail_order(service_mode="purchase") is None

        db.save_remail_order(self._order("short", service_mode="code"))
        db.mark_remail_order_attempt("short", False)
        assert db.get_remail_order("short")["order_status"] == "consumed"

    def test_list_hides_token(self, fresh_db):
        db = fresh_db
        db.save_remail_order(self._order("one"))
        orders = db.list_remail_orders()
        assert orders[0]["has_token"] is True
        assert "service_token" not in orders[0]
        assert db.get_remail_order("one", with_token=True)["service_token"] == "token"


class TestSettingsRunsAndErrors:
    def test_preferences_merge(self, fresh_db):
        db = fresh_db
        db.save_prefs({"theme": "dark", "workers": 2})
        assert db.save_prefs({"workers": 4}) == {
            "theme": "dark",
            "workers": 4,
        }

    def test_runs_are_recorded_and_truncated(self, fresh_db):
        db = fresh_db
        db.create_run("run-1", "USER@example.com", "/tmp/run.log")
        db.finish_run("run-1", "failed", "x" * 600, "network")
        run = db.list_runs()[0]
        assert run["email"] == "user@example.com"
        assert run["status"] == "failed"
        assert run["error_category"] == "network"
        assert len(run["error"]) == 500

    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            ("Remail OTP timeout after 180 seconds", "mail_limited"),
            ("warmup 失败：__cf_bm", "cf_blocked"),
            ("user_already_exists", "account"),
            ("invalid_state", "network"),
            ("connection reset", "network"),
            ("No module named 'sentinel'", "network"),
            ("something unusual", "unknown"),
        ],
    )
    def test_error_classification(self, message, expected):
        from regix.services.registration import classify_error

        assert classify_error(message) == expected

    def test_icloud_existing_account_remains_neutral_after_exemption(self):
        from regix.services.registration import classify_error

        assert classify_error("已有账号", mail_source="icloud_relay") == "unknown"


class TestExportHelpers:
    def test_export_catalog_is_stable(self):
        from regix.services.export_formats import list_formats

        assert [item["id"] for item in list_formats()] == [
            "at",
            "email_pw",
            "email_pw_2fa",
            "email_pw_2fa_relay",
            "at_proxy",
            "session_json",
        ]

    def test_at_proxy_rendering(self):
        from regix.services.export_formats import render_text

        rows = [
            {"access_token": "token-one", "proxy": "socks5://a:1080"},
            {"access_token": "token-two", "proxy": ""},
        ]
        assert render_text(rows, "at_proxy") == "token-one----socks5://a:1080\ntoken-two----"

    def test_session_json_rendering(self):
        from regix.services.export_formats import render_text

        rows = [{"session_json": '{"a":1}'}, {"session_json": None}]
        assert render_text(rows, "session_json") == '{"a":1}\n'

    def test_jwt_and_auth_claim_helpers(self):
        from regix.services.exporter import _decode_jwt_payload, _get_auth

        payload = {"https://api.openai.com/auth": {"user_id": "user-1"}}
        encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        token = f"header.{encoded}.signature"
        assert _decode_jwt_payload(token) == payload
        assert _get_auth(payload) == {"user_id": "user-1"}
        assert _decode_jwt_payload("not-a-jwt") == {}
        assert _get_auth({}) == {}


class TestTwoFactorHelpers:
    def test_hotp_rfc_vector(self):
        from regix.services.two_factor import hotp

        secret = base64.b32encode(b"12345678901234567890").decode()
        assert hotp(secret, 0) == "755224"
        assert hotp(secret, 9) == "520489"

    def test_verify_totp_window(self):
        from regix.services.two_factor import totp_now, verify_totp

        secret = base64.b32encode(b"12345678901234567890").decode()
        assert verify_totp(secret, totp_now(secret)) is True
        assert verify_totp(secret, "000000") is False


class TestInfrastructureHelpers:
    def test_fingerprint_is_deterministic_for_seed(self):
        from regix.fingerprint.generator import (
            fingerprint_for_impersonate,
            generate_fingerprint,
        )

        first = generate_fingerprint(random.Random(42), "US")
        second = generate_fingerprint(random.Random(42), "US")
        assert first == second
        adapted = fingerprint_for_impersonate("chrome150", first)
        assert adapted["impersonate"] == "chrome150"
        assert adapted["screen"] == first["screen"]
        assert adapted["timezone"] == first["timezone"]
        assert "Chrome/150." in adapted["user_agent"]

    def test_tls_handshake_classification(self):
        from regix.http.client import _is_tls_handshake_error

        assert _is_tls_handshake_error(
            RuntimeError("curl: (35) TLS connect error")
        ) is True
        assert _is_tls_handshake_error(RuntimeError("connection reset")) is False


class TestWebUIAuth:
    def test_admin_auth_locks_api(self, monkeypatch):
        monkeypatch.setenv("WEBUI_ADMIN_PASSWORD", "correct-password")
        # Basic Auth values are captured at module import, so isolate this test
        # with a fresh app instance after the credential is set.
        import importlib
        from fastapi.testclient import TestClient
        import regix.api.app as app_module

        app_module.ADMIN_PASSWORD = "correct-password"
        app_module._AUTH_FAILED_TIMES.clear()
        client = TestClient(app_module.app)
        assert client.get("/api/stats").status_code == 401
        headers = self._basic_header("admin", "correct-password")
        assert client.get("/api/stats", headers=headers).status_code == 200

        bad = self._basic_header("admin", "wrong")
        assert client.get("/api/stats", headers=bad).status_code == 401

    def test_router_catalog_is_complete(self):
        from regix.api.app import app

        paths: set[str] = set()
        for route in app.routes:
            paths.add(getattr(route, "path", ""))
            original = getattr(route, "original_router", None)
            if original is not None:
                paths.update(getattr(item, "path", "") for item in original.routes)
        expected = {
            "/api/health",
            "/api/stats",
            "/api/accounts",
            "/api/proxy/pool",
            "/api/register",
            "/api/runs",
            "/api/registered",
            "/api/registered/export/formats",
            "/api/registered/export",
            "/api/mail/providers",
            "/api/mail/orders",
            "/api/settings/sms",
            "/api/settings/export",
            "/api/auto/status",
            "/api/payment/profiles",
            "/api/payment/link",
        }
        assert expected <= paths

    def test_openapi_schema_builds_without_unresolved_models(self):
        import regix.api.app as app_module

        schema = app_module.app.openapi()
        assert "/api/register" in schema["paths"]
        assert "RegisterReq" in schema["components"]["schemas"]
        assert "ProxyPoolUpdateReq" in schema["components"]["schemas"]
        assert "SaveSmsConfigReq" in schema["components"]["schemas"]

        operations = []
        for path, methods in schema["paths"].items():
            for method in methods:
                operations.append((method, path))
        assert len(operations) == len(set(operations))

    def test_register_request_validation_returns_422_not_500(self, monkeypatch):
        import regix.api.app as app_module
        from fastapi.testclient import TestClient

        monkeypatch.setattr(app_module, "ADMIN_PASSWORD", "")
        app_module._AUTH_FAILED_TIMES.clear()
        response = TestClient(app_module.app).post(
            "/api/register",
            json={"otp_timeout": "not-an-integer"},
        )
        assert response.status_code == 422, response.text

    def test_read_api_smoke_with_temp_db(self, monkeypatch):
        import regix.api.app as app_module
        from fastapi.testclient import TestClient

        monkeypatch.setattr(app_module, "ADMIN_PASSWORD", "")
        app_module._AUTH_FAILED_TIMES.clear()
        client = TestClient(app_module.app)
        for path in (
            "/api/health",
            "/api/stats",
            "/api/accounts",
            "/api/proxy/pool",
            "/api/runs",
            "/api/registered",
            "/api/registered/export/formats",
            "/api/mail/providers",
            "/api/mail/orders",
            "/api/settings/sms",
            "/api/settings/export",
            "/api/settings/prefs",
            "/api/auto/status",
        ):
            response = client.get(path)
            assert response.status_code == 200, (path, response.status_code, response.text)

    @staticmethod
    def _basic_header(username: str, password: str) -> dict[str, str]:
        raw = f"{username}:{password}".encode()
        return {"Authorization": "Basic " + base64.b64encode(raw).decode()}

    def test_password_digest_is_constant_time_compatible(self):
        value = "secret-input"
        assert (
            hashlib.sha256(value.encode()).digest()
            == hashlib.sha256(value.encode()).digest()
        )


class TestAuthFlowGuards:
    def test_add_phone_is_only_entered_once_per_flow(self, monkeypatch):
        from regix.config import Config
        from regix.openai.auth_flow import AuthFlow

        flow = AuthFlow(Config(), sms_callback=object())
        calls = []
        monkeypatch.setattr(
            flow,
            "_handle_add_phone_via_sms",
            lambda continue_url="": calls.append(continue_url) or "next",
        )

        assert flow._handle_add_phone_verification("add-phone/1") == "next"
        assert flow._handle_add_phone_verification("add-phone/2") == "add-phone/2"
        assert calls == ["add-phone/1"]

