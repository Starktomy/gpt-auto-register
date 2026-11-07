"""Cloudflare 403 at the OAuth-init step is fingerprint-level, not IP-level.

Measured 2026-11-07 on one exit IP, same URL, only the TLS impersonation
changed::

    chrome110/136/146/150 -> 200    safari18_0 -> 200    firefox144 -> 200
    safari260              -> 403

A 403 there used to doom the whole registration: the `invalid_state` retry
rebuilt the session but kept the same impersonation, so all three attempts hit
the same wall. The protocol now rotates to the next same-family impersonate
and retries the OAuth init in place.
"""

from __future__ import annotations

import types

from regix.openai.protocol import ProtocolMixin


class _Resp:
    def __init__(self, status_code: int, url: str):
        self.status_code = status_code
        self.url = url
        self.text = ""
        self.content = b""


class _Session:
    """Answers 403 for the first N requests, then 200."""

    def __init__(self, state: dict):
        self.state = state
        self.cookies = {"oai-did": "device-from-cookie"}

    def get(self, url, headers=None, timeout=None, allow_redirects=None):
        self.state["gets"] += 1
        self.state["headers"].append(dict(headers or {}))
        if self.state["gets"] <= self.state["fail_first"]:
            return _Resp(403, "https://auth.openai.com/oauth/authorize")
        return _Resp(200, "https://auth.openai.com/log-in-or-create-account")


class _Subject(ProtocolMixin):
    """Minimal stand-in for the registration flow (only what auth_oauth_init touches)."""

    def __init__(self, fail_first: int, can_rotate: bool = True):
        self.state = {"gets": 0, "fail_first": fail_first, "headers": []}
        self.session = _Session(self.state)
        self.result = types.SimpleNamespace(device_id="")
        self.rotations = 0
        self._can_rotate = can_rotate
        # stands in for the TLS impersonation; the headers carry the client
        # hints of that fingerprint, so they change when the fingerprint rotates
        self.impersonate = "safari260"

    def _navigation_headers(self):
        return {"sec-fetch-site": "same-origin", "sec-fetch-user": "?1", "impersonate": self.impersonate}

    def _trace_http(self, *args, **kwargs):
        pass

    def _rotate_impersonate_session(self):
        if not self._can_rotate:
            return False
        self.rotations += 1
        self.impersonate = "safari18_0"
        self.session = _Session(self.state)
        return True


def test_oauth_init_rotates_fingerprint_after_403():
    subject = _Subject(fail_first=1)
    device_id = subject.auth_oauth_init("https://auth.openai.com/oauth/authorize")

    assert subject.rotations == 1, "403 must trigger an impersonate rotation"
    assert subject.state["gets"] == 2
    assert device_id == "device-from-cookie"
    # the retry must rebuild the navigation headers (client hints follow the fingerprint)
    assert subject.state["headers"][0] != subject.state["headers"][1]


def test_oauth_init_gives_up_when_no_fingerprint_is_left():
    subject = _Subject(fail_first=5, can_rotate=False)
    subject.auth_oauth_init("https://auth.openai.com/oauth/authorize")

    assert subject.rotations == 0
    assert subject.state["gets"] == 1, "no rotation available -> do not hammer the endpoint"


def test_oauth_init_keeps_going_after_a_200():
    subject = _Subject(fail_first=0)
    device_id = subject.auth_oauth_init("https://auth.openai.com/oauth/authorize")

    assert subject.rotations == 0
    assert subject.state["gets"] == 1
    assert device_id == "device-from-cookie"
