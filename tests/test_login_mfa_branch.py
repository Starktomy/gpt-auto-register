"""The OTP login branch must answer an mfa-challenge before the redirect chain.

Regression: accounts with 2FA that log in through the email-code branch used to
stop on ``/mfa-challenge/<id>``. The redirect chain then produced no callback
code and the whole login failed with "no session/access token" even though the
email code was correct.
"""

from __future__ import annotations

import types

from regix.openai.registration import RegistrationMixin


class _Stub(RegistrationMixin):
    def __init__(self, secret: str = "JBSWY3DPEHPK3PXP"):
        self.result = types.SimpleNamespace(totp_secret=secret)
        self._account_callback = None
        self.submitted: list[tuple[str, str]] = []

    def _extract_page_type(self, step):
        return (step or {}).get("page_type", "")

    def _normalize_continue_url(self, url):
        return url or ""

    def _is_mfa_challenge_state(self, page_type="", continue_url=""):
        return page_type == "mfa_challenge" or "/mfa-challenge/" in (continue_url or "")

    def _extract_continue_url_from_step(self, step):
        return (step or {}).get("continue_url", "")

    def submit_mfa_totp(self, code, challenge_id):
        self.submitted.append((code, challenge_id))
        return {
            "page_type": "external_url",
            "continue_url": "https://chatgpt.com/api/auth/callback/openai?code=abc&state=xyz",
        }


def test_otp_branch_solves_mfa_challenge():
    stub = _Stub()
    page_type, continue_url = stub._solve_mfa_challenge_step(
        {"page_type": "mfa_challenge"},
        "https://auth.openai.com/mfa-challenge/chal-1",
        "a@example.com",
    )
    assert len(stub.submitted) == 1
    code, challenge_id = stub.submitted[0]
    assert len(code) == 6 and challenge_id == "chal-1"
    assert page_type == "external_url"
    assert "code=abc" in continue_url


def test_non_mfa_step_is_left_alone():
    stub = _Stub()
    page_type, continue_url = stub._solve_mfa_challenge_step(
        {"page_type": "external_url"},
        "https://chatgpt.com/api/auth/callback/openai?code=xyz",
        "a@example.com",
    )
    assert stub.submitted == []
    assert page_type == "external_url"
    assert "code=xyz" in continue_url


def test_missing_secret_does_not_try_to_submit():
    stub = _Stub(secret="")
    page_type, continue_url = stub._solve_mfa_challenge_step(
        {"page_type": "mfa_challenge"},
        "https://auth.openai.com/mfa-challenge/chal-2",
        "a@example.com",
    )
    assert stub.submitted == []
    assert page_type == "mfa_challenge"
