"""Offline contract tests for the Remail HTTP provider."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from regix.providers.mail.remail import RemailMailProvider


class FakeRemail(BaseHTTPRequestHandler):
    state: dict

    def log_message(self, *args):
        pass

    def _send(self, status: int, body: dict) -> None:
        encoded = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path == "/v1/open/apikey/profile":
            self._send(
                200,
                {
                    "apiKey": {
                        "id": 1,
                        "name": "test",
                        "enabled": True,
                        "balance": "168.50",
                        "quotaLimit": None,
                        "quotaUsed": "0",
                    }
                },
            )
            return

        if parsed.path == "/v1/pickup":
            self.state["last_pickup_request"] = {"path": parsed.path, "query": query}
            email = (query.get("email") or [""])[0]
            token = (query.get("token") or [""])[0]
            known = {
                item["deliveryEmail"]: item["serviceToken"]
                for item in self.state["orders"]
            }
            if known.get(email) != token:
                self._send(401, {"message": "invalid service token"})
                return

            items = []
            for mail in self.state["mails"].get(email, []):
                item = {
                    "id": mail["id"],
                    "sender": mail["sender"],
                    "recipient": email,
                    "receivedAt": mail["receivedAt"],
                    "subject": mail["subject"],
                    "bodyPreview": mail["bodyPreview"],
                }
                if self.state["give_verification_code"]:
                    item["verificationCode"] = mail.get("verificationCode", "")
                items.append(item)
            self._send(
                200,
                {
                    "items": items,
                    "fetch": {
                        "lastStatus": "normal",
                        "nextFetchAllowedAt": None,
                        "lastSafeError": "",
                    },
                },
            )
            return

        self._send(404, {"message": "not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode() if length else "{}"
        if parsed.path != "/v1/open/orders":
            self._send(404, {"message": "not found"})
            return

        idempotency_key = self.headers.get("Idempotency-Key")
        if not idempotency_key:
            self._send(400, {"message": "Idempotency-Key required"})
            return
        if not self.headers.get("Authorization", "").startswith("Bearer rk-"):
            self._send(401, {"message": "bad auth"})
            return

        body = json.loads(raw) if raw else {}
        self.state["last_order_request"] = {
            "query": query,
            "body": body,
            "idempotency": idempotency_key,
            "auth": self.headers.get("Authorization"),
        }
        suffix = body.get("emailSuffix") or "outlook"
        count = len(self.state["orders"]) + 1
        order = {
            "orderNo": f"ORD{count:04d}",
            "deliveryEmail": f"user{count}@{suffix}",
            "serviceToken": "svc-token-abc123",
            "status": "paid",
            "serviceMode": (query.get("serviceMode") or ["purchase"])[0],
            "supplyPolicy": (query.get("supply") or ["private_first"])[0],
            "payAmount": "0.80",
            "receiveStartedAt": "2026-11-04T10:00:00Z",
            "afterSaleUntil": "2026-11-05T10:00:00Z",
        }
        self.state["orders"].append(order)
        self._send(201, order)


@pytest.fixture
def fake_remail(clean_db):
    state = {
        "orders": [],
        "mails": {},
        "otp": "123456",
        "give_verification_code": True,
        "last_order_request": None,
        "last_pickup_request": None,
    }
    FakeRemail.state = state
    server = HTTPServer(("127.0.0.1", 0), FakeRemail)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture
def remail(fake_remail):
    base, _ = fake_remail
    return RemailMailProvider(
        api_base=base,
        api_key="rk-test",
        project_id=1001,
        email_suffix="outlook",
    )


def utc_now(offset: float = 0) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + offset))


def test_self_test_reports_balance_and_order_request_contract(
    remail, fake_remail, clean_db
):
    base, state = fake_remail
    result = remail.self_test()
    email = remail.create_mailbox()
    request = state["last_order_request"]
    saved = clean_db.get_remail_order("ORD0001", with_token=True)

    assert result["ok"] is True
    assert "168.50" in result["message"]
    assert email == "user1@outlook"
    assert request["auth"] == "Bearer rk-test"
    assert request["idempotency"]
    assert request["query"]["serviceMode"] == ["purchase"]
    assert request["query"]["supply"] == ["private_first"]
    assert request["body"] == {
        "projectId": 1001,
        "emailSuffix": "outlook",
    }
    assert saved["service_token"] == "svc-token-abc123"
    assert saved["suffix"] == "outlook"
    assert saved["pay_amount"] == "0.80"


def test_accepts_platform_verification_code(remail, fake_remail):
    _, state = fake_remail
    email = remail.create_mailbox()
    state["mails"][email] = [
        {
            "id": 1,
            "sender": "noreply@openai.com",
            "receivedAt": utc_now(),
            "subject": "Your ChatGPT code",
            "bodyPreview": "Your code is 123456",
            "verificationCode": "123456",
        }
    ]

    assert remail.wait_for_otp(email, timeout=20, issued_after=time.time() - 5) == "123456"


def test_extracts_verification_code_from_body(remail, fake_remail):
    _, state = fake_remail
    state["give_verification_code"] = False
    email = remail.create_mailbox()
    state["mails"][email] = [
        {
            "id": 2,
            "sender": "noreply@openai.com",
            "receivedAt": utc_now(),
            "subject": "OpenAI verification code 654321",
            "bodyPreview": "Your verification code is 654321",
        }
    ]

    assert remail.wait_for_otp(email, timeout=20, issued_after=time.time() - 5) == "654321"


def test_ignores_mail_received_before_time_window(remail, fake_remail):
    _, state = fake_remail
    email = remail.create_mailbox()
    state["mails"][email] = [
        {
            "id": 3,
            "sender": "noreply@openai.com",
            "receivedAt": utc_now(-600),
            "subject": "old code",
            "bodyPreview": "code 111111",
            "verificationCode": "111111",
        }
    ]

    with pytest.raises(TimeoutError):
        remail.wait_for_otp(email, timeout=6, issued_after=time.time() - 5)


def test_order_link_and_repeated_pickup(remail, fake_remail, clean_db):
    _, state = fake_remail
    email = remail.create_mailbox()
    state["mails"][email] = []
    assert clean_db.link_remail_order("ORD0001", "test@local") is True
    assert clean_db.get_remail_order("ORD0001")["used_email"] == "test@local"
    assert isinstance(remail.fetch_mails(email, "svc-token-abc123"), list)
