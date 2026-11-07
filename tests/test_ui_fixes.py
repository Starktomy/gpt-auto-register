"""Regression contracts for frontend state handling and registration options."""

from __future__ import annotations

import json
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_SRC = PROJECT_ROOT / "webui" / "frontend" / "src"


def read_frontend(relative_path: str) -> str:
    return (FRONTEND_SRC / relative_path).read_text(encoding="utf-8")


def read_flow_source() -> str:
    root = PROJECT_ROOT / "regix" / "openai"
    names = (
        "auth_flow.py",
        "models.py",
        "client.py",
        "oauth.py",
        "challenges.py",
        "token.py",
        "protocol.py",
        "session.py",
        "registration.py",
    )
    return "\n".join((root / name).read_text(encoding="utf-8") for name in names)


def code_only(source: str) -> str:
    """Strip comments so migration notes cannot satisfy a regression check."""
    output: list[str] = []
    in_block = False
    for line in source.splitlines():
        stripped = line.strip()
        if in_block:
            if "*/" in stripped:
                in_block = False
            continue
        if stripped.startswith("/*"):
            if "*/" not in stripped:
                in_block = True
            continue
        if stripped.startswith("//") or stripped.startswith("*"):
            continue
        output.append(line)
    return "\n".join(output)


def test_sms_switch_does_not_overwrite_in_flight_changes():
    source = read_frontend("views/SmsConfig.vue")
    assert "const touched = new Set()" in source
    assert "markTouched('enabled')" in source
    assert "touched.has('enabled')" in source
    assert source.index("enabled.value = config.sms_enabled") < source.index(
        "await loadCountries(provider.value)"
    )
    assert "setTimeout(load" not in code_only(source)


def test_2fa_switch_is_passed_to_worker_and_hook():
    form = read_frontend("stores/form.js")
    auto_view = read_frontend("views/AutoLoop.vue")
    registration = (
        PROJECT_ROOT / "regix" / "services" / "registration.py"
    ).read_text(encoding="utf-8")

    assert "const dirty = new Set()" in form
    assert "dirty.has(key)" in form
    assert "_applying" in form
    assert "await nextTick()" in form
    assert "if (!_applying) dirty.add(key)" in form
    assert "want_2fa: form.value.autoWant2fa" in auto_view
    assert 'on_session_ready=_bind_2fa_hook if options.get("want_2fa") else None' in registration
    assert 'if options.get("want_2fa"):' in registration
    assert "2FA绑定=" in registration


def test_refresh_token_defaults_are_safe_without_sms():
    form = read_frontend("stores/form.js")
    register_view = read_frontend("views/Register.vue")
    auto_view = read_frontend("views/AutoLoop.vue")
    routers = PROJECT_ROOT / "regix" / "api" / "routers"
    router_source = "\n".join(
        (routers / name).read_text(encoding="utf-8")
        for name in ("registration.py", "auto_loop.py")
    )
    registration = (
        PROJECT_ROOT / "regix" / "services" / "registration.py"
    ).read_text(encoding="utf-8")
    flow = read_flow_source()

    assert "wantRefreshToken: false" in form
    assert "autoWantRefreshToken: false" in form
    assert "want_refresh_token: form.value.wantRefreshToken" in register_view
    assert "want_refresh_token: form.value.autoWantRefreshToken" in auto_view
    assert router_source.count("want_refresh_token: bool = False") == 2
    assert "def _sms_ready()" in registration
    assert "elif not sms_ready:" in registration
    assert registration.count(
        'env_overrides["OAUTH_SECONDARY_AUTHORIZE_EXCHANGE"] = "0"'
    ) == 2
    assert "Codex 登录推进需要 OTP，但已禁用 Codex OAuth" in flow
    assert 'logger.debug("Codex OAuth 未捕获 callback code' not in flow


def test_short_lived_mail_registration_contract():
    base = (
        PROJECT_ROOT / "regix" / "providers" / "mail" / "base.py"
    ).read_text(encoding="utf-8")
    remail = (
        PROJECT_ROOT / "regix" / "providers" / "mail" / "remail.py"
    ).read_text(encoding="utf-8")
    registration = (
        PROJECT_ROOT / "regix" / "services" / "registration.py"
    ).read_text(encoding="utf-8")
    flow = read_flow_source()

    assert "ephemeral_mailbox: bool = False" in base
    assert 'self.ephemeral_mailbox = (self.service_mode == "code")' in remail
    assert "earliest: Optional[float] = None" in remail
    assert "if self.ephemeral_mailbox and earliest is not None:" in remail
    assert "elif issued_after is not None:" in remail
    assert "self._register_started_at = time.time()" in flow
    assert 'skip_password = bool(getattr(mail_provider, "ephemeral_mailbox", False))' in flow
    assert "otp_sent_at = self._register_started_at" in flow
    assert "earliest=self._register_started_at" in flow
    assert "_MAIL_LIMITED_PATTERNS" in registration
    assert registration.index("_MAIL_LIMITED_PATTERNS") < registration.index(
        'if any(p in s for p in _NETWORK_ERROR_PATTERNS)'
    )
    assert 'elif category == "mail_limited":' in registration
    assert 'db.mark_failed(email, f"[{category}] {err}")' in registration


def test_proxy_and_mail_views_expose_operational_state():
    proxy_view = read_frontend("views/ProxyPool.vue")
    mail_view = read_frontend("views/MailService.vue")

    assert 'label="类型"' in proxy_view
    assert 'value="static"' in proxy_view
    assert 'value="dynamic"' in proxy_view
    assert 'label="CF拦截"' in proxy_view
    assert 'label="IPv6"' in proxy_view
    assert "检测 IPv6" in proxy_view
    assert "清CF" in proxy_view
    assert "consumed:" in mail_view


def test_auto_loop_treats_cf_blocked_as_proxy_failure():
    source = (
        PROJECT_ROOT / "regix" / "services" / "auto_loop.py"
    ).read_text(encoding="utf-8")
    flow = read_flow_source()

    assert 'category == "cf_blocked"' in source
    assert "db.mark_proxy_cf_blocked(proxy)" in source
    assert (
        'if category == "network":\n                    self._consecutive_network_fails += 1'
        in source
    )
    assert "invalid_state" in flow
    assert "max_attempts = 3" in flow
    assert "self.session = create_http_session(" in flow


def test_session_json_is_persisted_and_exported(clean_db):
    db = clean_db
    from regix.services import export_formats

    columns = [
        row[1]
        for row in db._conn().execute("PRAGMA table_info(registered)").fetchall()
    ]
    assert "session_json" in columns

    session = json.dumps(
        {
            "user": {"id": "u-1", "email": "s1@x.com", "groups": ["g"]},
            "accessToken": "AT1",
            "sessionToken": "ST1",
            "expires": "2026-12-05T00:00:00Z",
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    db.save_registered(
        {
            "email": "L_SESS@x.com",
            "access_token": "AT1",
            "session_token": "ST1",
            "session_json": session,
        }
    )
    saved = db.get_registered("L_SESS@x.com")
    assert saved["session_json"] == session
    assert "session_json" not in (saved.get("extra") or {})
    assert "\n" not in saved["session_json"]

    assert export_formats.get_format("session_json") is not None
    assert "session_json" in {
        item["id"] for item in export_formats.list_formats()
    }
    rendered = export_formats.render_text(
        db.list_registered_full(), "session_json"
    )
    for line in (line for line in rendered.splitlines() if line):
        assert json.loads(line)

    registered_view = read_frontend("views/Registered.vue")
    assert "复制原始 session_json" in registered_view
    assert "copySessionJson" in registered_view
    assert "'session_json'" in registered_view
    assert "self.session_json" in read_flow_source()
    assert "self.result.session_json = json.dumps(" in read_flow_source()
    assert 'd["session_json"]' in (
        PROJECT_ROOT / "regix" / "services" / "registration.py"
    ).read_text(encoding="utf-8")
