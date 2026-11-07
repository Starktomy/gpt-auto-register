"""Protocol and fingerprint contracts that caused real registration failures."""

from __future__ import annotations

import re
from pathlib import Path

from curl_cffi.requests.utils import NATIVE_TARGET_NAMES


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")


def _flow_source() -> str:
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
    return "\n".join(_read(f"regix/openai/{name}") for name in names)


def test_fingerprint_impersonate_values_are_supported_by_curl_cffi():
    source = _read("regix/fingerprint/generator.py")
    configured = set(re.findall(r'"impersonate":\s*"([^"]+)"', source))
    assert configured <= set(NATIVE_TARGET_NAMES)
    assert "safari18_4_ios" not in configured
    assert "safari184_ios" in configured
    assert re.search(
        r'"impersonate":\s*"safari184_ios",\s*\n\s*"safari_ver":\s*"18\.4"',
        source,
    )


def test_signup_409_retry_refreshes_device_and_session_state():
    source = _flow_source()
    match = re.search(r"signup 撞 invalid_state", source)
    assert match
    block = source[match.start(): match.start() + 2500]
    assert 'self.result.device_id = ""' in block
    assert 'self._last_sentinel_so_token = ""' in block
    assert "self.get_csrf_token()" in block
    assert "self.get_auth_url(" in block
    assert "self.auth_oauth_init(" in block
    assert "self.warmup()" in block


def test_common_headers_prefer_refreshed_device_id():
    source = _flow_source()
    lines = source.splitlines()
    start = next(
        index
        for index, line in enumerate(lines)
        if line.strip().startswith("def _common_headers(")
    )
    definition = "\n".join(lines[start:start + 80])
    assert re.search(
        r'self\.result\.device_id\s+or\s+.{0,80}?'
        r'self\.session\.cookies\.get\(\s*"oai-did"',
        definition,
        re.S,
    )


def test_mailbox_is_created_after_warmup_and_before_authorization():
    source = _flow_source()
    match = re.search(r"def run_register", source)
    assert match
    body = source[match.start(): match.start() + 9000]
    csrf_index = body.find("self.get_csrf_token()")
    mailbox_index = body.find("mail_provider.create_mailbox()")
    auth_url_index = body.find("self.get_auth_url(")
    assert mailbox_index >= 0
    assert csrf_index >= 0
    assert auth_url_index >= 0
    assert mailbox_index > csrf_index
    assert mailbox_index < auth_url_index
    assert body.find("self.warmup()") < mailbox_index
    assert "email=email" not in body[auth_url_index:auth_url_index + 200]
