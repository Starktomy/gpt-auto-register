from __future__ import annotations

from regix.services import two_factor


class Response:
    status_code = 200
    text = ""

    def __init__(self, payload=None):
        self.payload = payload or {}

    def json(self):
        return self.payload


class Session:
    def __init__(self, mfa_enabled_after=1):
        self.mfa_enabled_after = mfa_enabled_after
        self.get_count = 0

    def get(self, *_args, **_kwargs):
        self.get_count += 1
        return Response({"mfa_enabled": self.get_count >= self.mfa_enabled_after})

    def post(self, *_args, **_kwargs):
        if getattr(self, "posts", 0) == 0:
            self.posts = 1
            return Response({"secret": "JBSWY3DPEHPK3PXP", "session_id": "sid"})
        self.posts = 2
        return Response({"success": True})


class Flow:
    def __init__(self, session):
        self.session = session

    def _common_headers(self, _referer):
        return {}


def test_mfa_info_is_checked_immediately_after_activation(monkeypatch):
    sleeps = []
    monkeypatch.setattr(two_factor.time, "sleep", lambda value: sleeps.append(value))
    flow = Flow(Session(mfa_enabled_after=2))

    result = two_factor._enroll_and_activate(flow, "access-token")

    assert result["secret"] == "JBSWY3DPEHPK3PXP"
    assert flow.session.get_count == 2  # pre-check + immediate post-activation check
    assert sleeps == []


def test_mfa_info_retries_briefly_for_propagation(monkeypatch):
    sleeps = []
    monkeypatch.setattr(two_factor.time, "sleep", lambda value: sleeps.append(value))
    flow = Flow(Session(mfa_enabled_after=3))

    result = two_factor._enroll_and_activate(flow, "access-token")

    assert result is not None
    assert sleeps == [0.4]
