from __future__ import annotations

import threading
import time

import pytest

from regix.providers.mail import outlook


def _provider() -> outlook.OutlookMailProvider:
    return outlook.OutlookMailProvider(
        email="user@outlook.com",
        password="password",
        client_id="client-id",
        refresh_token="refresh-token",
    )


def test_graph_failure_does_not_delay_imap(monkeypatch):
    def graph(_email, _refresh, _client_id, **_kwargs):
        raise outlook.FatalOutlookMailError("Graph permission denied")

    def imap(_email, _refresh, _client_id, **_kwargs):
        time.sleep(0.05)
        return "654321"

    monkeypatch.setattr(outlook, "fetch_otp_via_graph", graph)
    monkeypatch.setattr(outlook, "fetch_otp_via_imap", imap)

    assert _provider().wait_for_otp("user@outlook.com", timeout=1) == "654321"


def test_graph_winner_stops_imap_channel(monkeypatch):
    imap_stopped = threading.Event()

    def graph(_email, _refresh, _client_id, **_kwargs):
        return "123456"

    def imap(_email, _refresh, _client_id, *, stop_event=None, **_kwargs):
        stop_event.wait(timeout=0.5)
        imap_stopped.set()
        return "654321"

    monkeypatch.setattr(outlook, "fetch_otp_via_graph", graph)
    monkeypatch.setattr(outlook, "fetch_otp_via_imap", imap)
    started = time.monotonic()

    result = _provider().wait_for_otp("user@outlook.com", timeout=1)

    assert result == "123456"
    assert time.monotonic() - started < 0.3
    assert imap_stopped.wait(timeout=0.5)


def test_both_mail_channel_failures_raise_imap_error(monkeypatch):
    def graph(_email, _refresh, _client_id, **_kwargs):
        raise RuntimeError("graph unavailable")

    def imap(_email, _refresh, _client_id, **_kwargs):
        raise TimeoutError("imap timeout")

    monkeypatch.setattr(outlook, "fetch_otp_via_graph", graph)
    monkeypatch.setattr(outlook, "fetch_otp_via_imap", imap)

    with pytest.raises(TimeoutError, match="imap timeout"):
        _provider().wait_for_otp("user@outlook.com", timeout=1)
