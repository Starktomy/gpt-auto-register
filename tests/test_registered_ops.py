"""Per-account proxy editing and one-click TOTP binding."""

from __future__ import annotations

import base64
import json

import pytest


def _jwt(account_id: str = "acct-1") -> str:
    payload = {
        "https://api.openai.com/auth": {
            "chatgpt_account_id": account_id,
            "chatgpt_plan_type": "free",
        }
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


def _used(db, proxy: str) -> int:
    for row in db.list_proxies():
        if row.get("proxy") == proxy:
            return int(row.get("used_count") or 0)
    raise AssertionError(f"proxy not in pool: {proxy}")


# ─────────────────────── per-account proxy ───────────────────────


def test_update_registered_proxy_reserves_and_releases_quota(clean_db):
    db = clean_db
    db.replace_proxies(["socks5://a:1080", "socks5://b:1080"])
    db.save_registered({"email": "proxy@example.com", "access_token": _jwt()})

    assert db.update_registered_proxy("proxy@example.com", "socks5://a:1080") is True
    assert _used(db, "socks5://a:1080") == 1
    assert db.update_registered_proxy("proxy@example.com", "socks5://b:1080") is True
    assert _used(db, "socks5://a:1080") == 0
    assert _used(db, "socks5://b:1080") == 1

    row = db.get_registered("proxy@example.com")
    assert row["extra"]["proxy"] == "socks5://b:1080"

    # 清空 = 直连，并把配额还回去
    assert db.update_registered_proxy("proxy@example.com", "") is True
    assert _used(db, "socks5://b:1080") == 0
    assert "proxy" not in db.get_registered("proxy@example.com")["extra"]


def test_update_registered_proxy_same_value_does_not_double_count(clean_db):
    db = clean_db
    db.replace_proxies(["socks5://a:1080"])
    db.save_registered({"email": "same@example.com", "access_token": _jwt()})
    db.update_registered_proxy("same@example.com", "socks5://a:1080")
    db.update_registered_proxy("same@example.com", "socks5://a:1080")
    assert _used(db, "socks5://a:1080") == 1


def test_update_registered_proxy_unknown_email(clean_db):
    assert clean_db.update_registered_proxy("nobody@example.com", "socks5://a:1080") is False


def test_update_proxy_api(clean_db, monkeypatch):
    import regix.api.app as app_module
    from fastapi.testclient import TestClient

    clean_db.save_registered({"email": "api@example.com", "access_token": _jwt()})
    monkeypatch.setattr(app_module, "ADMIN_PASSWORD", "")
    app_module._AUTH_FAILED_TIMES.clear()
    client = TestClient(app_module.app)
    resp = client.post(
        "/api/registered/update_proxy",
        json={"email": "api@example.com", "proxy": "socks5://manual:1080"},
    )
    assert resp.status_code == 200
    assert resp.json()["proxy"] == "socks5://manual:1080"
    assert clean_db.get_registered("api@example.com")["extra"]["proxy"] == "socks5://manual:1080"


# ─────────────────────── one-click TOTP binding ───────────────────────


def test_add_totp_refuses_to_overwrite_existing_secret(clean_db):
    from regix.services.two_factor import add_totp_2fa

    clean_db.save_registered(
        {
            "email": "has2fa@example.com",
            "access_token": _jwt(),
            "password": "secret-password",
            "totp_secret": "JBSWY3DPEHPK3PXP",
        }
    )
    info = add_totp_2fa("has2fa@example.com")
    assert info["ok"] is False
    assert info["reason"] == "already_bound"
    # the stored secret must survive untouched
    assert clean_db.get_registered("has2fa@example.com")["totp_secret"] == "JBSWY3DPEHPK3PXP"


def test_add_totp_reports_server_side_mfa_without_local_secret(monkeypatch, clean_db):
    """Login stops at mfa-challenge -> tell the user the server has a factor
    we cannot answer, instead of a generic failure."""
    import regix.services.two_factor as tf
    import regix.services.codex_login as cl

    clean_db.save_registered(
        {"email": "lost@example.com", "access_token": _jwt(), "password": "pw"}
    )

    def _boom(*args, **kwargs):
        raise cl.CodexLoginError("协议登录完成，但未拿到有效 session/access token")

    monkeypatch.setattr(cl, "login_and_exchange", _boom)
    monkeypatch.setattr(tf, "_server_mfa_enabled", lambda row, proxy="": True)
    info = tf.add_totp_2fa("lost@example.com")
    assert info["ok"] is False
    assert info["reason"] == "already_bound_unknown_secret"


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = ""

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self):
        self.calls = []
        self.info_calls = 0

    def get(self, url, headers=None, timeout=None):
        self.calls.append(("GET", url))
        self.info_calls += 1
        # 第一次查询 = 尚未绑定；激活后复核 = 已启用
        return _Resp(200, {"mfa_enabled": self.info_calls > 1})

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append(("POST", url))
        if url.endswith("/mfa/enroll"):
            return _Resp(200, {"secret": "NEWSECRETBASE32", "session_id": "sid-1",
                               "factor": {"id": "factor-9"}})
        if url.endswith("/activate_enrollment"):
            return _Resp(200, {})
        return _Resp(404, {})


class _FakeFlow:
    def __init__(self):
        self.session = _FakeSession()

    def _common_headers(self, url):
        return {}


def test_add_totp_binds_and_saves_secret(monkeypatch, clean_db):
    import regix.services.two_factor as tf
    import regix.services.codex_login as cl

    clean_db.save_registered(
        {"email": "new2fa@example.com", "access_token": _jwt(), "password": "pw"}
    )
    fake_flow = _FakeFlow()
    monkeypatch.setattr(
        cl, "login_and_exchange",
        lambda email, proxy="", otp_timeout=180, save=True: (
            fake_flow, {"access_token": "fresh-at", "refresh_token": "rt", "email": email}, {},
        ),
    )
    info = tf.add_totp_2fa("new2fa@example.com")
    assert info["ok"] is True
    assert info["totp_secret"] == "NEWSECRETBASE32"
    row = clean_db.get_registered("new2fa@example.com")
    assert row["totp_secret"] == "NEWSECRETBASE32"
    assert row["totp_factor_id"] == "factor-9"


def test_add_totp_saves_secret_before_activation(monkeypatch, clean_db):
    """If activation fails, the secret must already be in the database."""
    import regix.services.two_factor as tf
    import regix.services.codex_login as cl

    clean_db.save_registered(
        {"email": "crash@example.com", "access_token": _jwt(), "password": "pw"}
    )
    fake_flow = _FakeFlow()
    original_post = fake_flow.session.post

    def _post(url, headers=None, json=None, timeout=None):
        if url.endswith("/activate_enrollment"):
            return _Resp(500, {})
        return original_post(url, headers=headers, json=json, timeout=timeout)

    fake_flow.session.post = _post
    monkeypatch.setattr(
        cl, "login_and_exchange",
        lambda email, proxy="", otp_timeout=180, save=True: (
            fake_flow, {"access_token": "fresh-at", "refresh_token": "rt", "email": email}, {},
        ),
    )
    info = tf.add_totp_2fa("crash@example.com")
    assert info["ok"] is False
    assert info["reason"] == "activate_failed"
    assert clean_db.get_registered("crash@example.com")["totp_secret"] == "NEWSECRETBASE32"


def test_bind_2fa_api_reports_already_bound(clean_db, monkeypatch):
    import regix.api.app as app_module
    from fastapi.testclient import TestClient

    clean_db.save_registered(
        {
            "email": "bound@example.com",
            "access_token": _jwt(),
            "password": "pw",
            "totp_secret": "JBSWY3DPEHPK3PXP",
        }
    )
    monkeypatch.setattr(app_module, "ADMIN_PASSWORD", "")
    app_module._AUTH_FAILED_TIMES.clear()
    client = TestClient(app_module.app)
    resp = client.post("/api/registered/bind_2fa", json={"email": "bound@example.com"})
    assert resp.status_code == 400
    assert "已有 2FA" in resp.json()["detail"]


def test_bind_2fa_api_returns_new_secret(clean_db, monkeypatch):
    import regix.api.app as app_module
    import regix.services.two_factor as tf
    from fastapi.testclient import TestClient

    clean_db.save_registered(
        {"email": "fresh@example.com", "access_token": _jwt(), "password": "pw"}
    )
    monkeypatch.setattr(
        tf, "add_totp_2fa",
        lambda email, proxy="", otp_timeout=180: {
            "ok": True, "email": email, "totp_secret": "ABC234", "factor_id": "f1",
        },
    )
    monkeypatch.setattr(app_module, "ADMIN_PASSWORD", "")
    app_module._AUTH_FAILED_TIMES.clear()
    client = TestClient(app_module.app)
    resp = client.post("/api/registered/bind_2fa", json={"email": "fresh@example.com"})
    assert resp.status_code == 200
    assert resp.json()["totp_secret"] == "ABC234"
