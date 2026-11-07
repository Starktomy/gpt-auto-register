"""Unique integration contracts not covered by the behavior-lock suite."""

from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_remail_provider_configuration():
    from regix.providers.mail import get_provider_class

    provider = get_provider_class("remail")
    assert provider.pooled is False
    assert provider.ephemeral is True

    fields = {field.key: field for field in provider.config_fields}
    assert fields["remail_email_suffix"].type == "text"
    assert fields["remail_service_mode"].type == "select"
    assert set(fields["remail_service_mode"].options) == {"purchase", "code"}
    assert fields["remail_supply_policy"].type == "select"
    assert fields["remail_supply_policy"].options[0] == "private_first"
    assert fields["remail_api_key"].type == "password"
    assert fields["remail_project_id"].type == "number"


def test_otp_timeout_floor():
    # Remail delivery can take 20-30 seconds, so a short frontend value must
    # not turn into an OTP timeout before the message arrives.
    registration = (
        PROJECT_ROOT / "regix" / "services" / "registration.py"
    ).read_text(encoding="utf-8")
    form = (
        PROJECT_ROOT
        / "webui"
        / "frontend"
        / "src"
        / "stores"
        / "form.js"
    ).read_text(encoding="utf-8")

    assert "max(60, _otp_timeout)" in registration
    assert "otpTimeout: 180" in form
