"""Offline contract tests for the api668 SMS provider."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from regix.providers.sms.api668 import Api668Provider
from regix.providers.sms.base import SmsActivation
from regix.providers.sms.callback import PhoneCallbackController
from regix.providers.sms.factory import create_sms_provider


class FakeApi668(BaseHTTPRequestHandler):
    state: dict

    def log_message(self, *args):
        pass

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _ok(self, data: dict) -> None:
        self._send_json(200, {"code": 0, "msg": "ok", "data": data})

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        authorization = self.headers.get("Authorization")
        if self.state.get("required_authorization") and authorization != self.state["required_authorization"]:
            self._send_json(401, {"code": 101105, "msg": "unauthorized", "data": {}})
            return
        self.state["requests"].append(
            {
                "method": "GET",
                "path": parsed.path,
                "query": query,
                "authorization": authorization,
            }
        )
        if parsed.path == "/api/country/":
            self._ok(
                {
                    "country_list": [
                        {
                            "id": 1,
                            "cn_name": "美国",
                            "en_name": "United States",
                            "iso2": "US",
                            "phone_area_code": "1",
                        }
                    ]
                }
            )
            return
        if parsed.path == "/api/platform/":
            self._ok(
                {
                    "platform_list": [
                        {"id": 42, "name": "OpenAI", "price": 0.25}
                    ]
                }
            )
            return
        if parsed.path == "/api/phone_number/prefix/":
            self._ok(
                {
                    "prefix_list": [
                        {"id": 1, "prefix": "212", "phone_area_code": "1"}
                    ]
                }
            )
            return
        if parsed.path == "/api/token/inventory":
            self._ok({"inventory": 8})
            return
        if parsed.path == "/api/token/":
            assert query["order_id"] == ["100"]
            self._ok(
                {
                    "total": 1,
                    "page": 1,
                    "size": 20,
                    "items": [
                        {
                            "id": 7,
                            "token_url": f"{self.state['base_url']}/sms-token/7",
                            "full_number": "12125550123",
                            "expiration_time": "2026-11-07T20:00:00Z",
                            "platform_id": 42,
                            "platform_name": "OpenAI",
                        }
                    ],
                }
            )
            return
        if parsed.path == "/sms-token/7":
            body = b"OpenAI verification code: 654321"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._send_json(404, {"code": 404, "msg": "not found", "data": {}})

    def do_POST(self):
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        self.state["requests"].append(
            {
                "method": "POST",
                "path": parsed.path,
                "body": body,
                "authorization": self.headers.get("Authorization"),
            }
        )
        if parsed.path == "/api/order":
            assert body == {"platform_id": 42, "quantity": 1, "prefix": "212"}
            self._ok({"order_id": 100, "quantity": 1})
            return
        self._send_json(404, {"code": 404, "msg": "not found", "data": {}})


@pytest.fixture
def api668_server():
    server = HTTPServer(("127.0.0.1", 0), FakeApi668)
    state = {"requests": []}
    state["base_url"] = f"http://127.0.0.1:{server.server_address[1]}"
    FakeApi668.state = state
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state["base_url"], state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_api668_factory_and_catalog(api668_server):
    base_url, state = api668_server
    provider = create_sms_provider(
        "api668",
        {
            "sms_api_key": "secret-token",
            "sms668_base_url": base_url,
            "sms_service": "OpenAI",
            "sms_country": "1",
            "sms668_prefix": "212",
        },
    )

    assert isinstance(provider, Api668Provider)
    assert provider.health_check() == {"ok": True, "country_count": 1}
    assert provider.get_country_list()[0]["cn_name"] == "美国"
    assert provider.get_platform_list(1) == [
        {"id": 42, "name": "OpenAI", "price": 0.25}
    ]
    assert provider.get_prefix_list(1)[0]["prefix"] == "212"
    assert provider.get_inventory(42, "212") == 8
    assert all(item["authorization"] == "secret-token" for item in state["requests"])


def test_api668_retries_bearer_authorization(api668_server):
    base_url, state = api668_server
    state["required_authorization"] = "Bearer secret-token"
    provider = Api668Provider("secret-token", base_url=base_url, auth_scheme="raw")
    assert provider.get_country_list()[0]["id"] == 1
    assert provider.auth_scheme == "bearer"


def test_api668_rejects_unknown_platform_instead_of_buying_another(api668_server):
    base_url, _ = api668_server
    provider = Api668Provider("secret-token", base_url=base_url)
    with pytest.raises(Exception, match="not available"):
        provider.get_number(service="Not-OpenAI", country="1")


def test_api668_country_ranking(api668_server):
    base_url, _ = api668_server
    provider = Api668Provider(
        "secret-token",
        base_url=base_url,
        default_service="OpenAI",
        default_country="1",
        prefix="212",
    )

    rows = provider.get_top_countries("OpenAI")
    assert rows == [
        {
            "country": "1",
            "count": 8,
            "price": 0.25,
            "name_cn": "美国",
            "platform_id": 42,
            "platform_name": "OpenAI",
            "prefix": "212",
        }
    ]
    assert provider.get_best_country("OpenAI", min_stock=8, max_price=0.5) == "1"


def test_api668_order_phone_and_code(api668_server):
    base_url, state = api668_server
    provider = Api668Provider(
        "secret-token",
        base_url=base_url,
        default_service="OpenAI",
        default_country="1",
        prefix="212",
    )

    activation = provider.get_number(
        service="OpenAI", country="1", country_candidates=["1"]
    )
    assert activation.activation_id == "100"
    assert activation.phone_number == "+12125550123"
    assert activation.country == "1"
    assert activation.metadata["platform_id"] == 42
    assert activation.metadata["prefix"] == "212"
    assert provider.get_code("100", timeout=5) == "654321"

    order_request = next(
        item for item in state["requests"]
        if item["method"] == "POST" and item["path"] == "/api/order"
    )
    assert order_request["body"] == {
        "platform_id": 42,
        "quantity": 1,
        "prefix": "212",
    }


def test_api668_settings_are_persisted(clean_db):
    clean_db.save_sms_config(
        {
            "sms_provider": "api668",
            "sms_api_key": "secret-token",
            "sms668_prefix": "66",
            "sms668_base_url": "https://example.test",
            "sms668_auth_scheme": "bearer",
        }
    )
    config = clean_db.get_sms_internal_config()
    assert config["sms_provider"] == "api668"
    assert config["sms_api_key"] == "secret-token"
    assert config["sms668_prefix"] == "66"
    assert config["sms668_base_url"] == "https://example.test"
    assert config["sms668_auth_scheme"] == "bearer"


def test_preloaded_activation_is_used_once_and_never_buys_a_replacement(api668_server):
    base_url, state = api668_server
    provider = Api668Provider(
        "secret-token",
        base_url=base_url,
        default_service="OpenAI",
        default_country="1",
        prefix="212",
    )
    controller = PhoneCallbackController(
        "api668",
        {"sms_api_key": "secret-token", "sms668_base_url": base_url},
        service="OpenAI",
        country="1",
    )
    controller.provider = provider
    controller.adopt_activation(
        SmsActivation(
            activation_id="100",
            phone_number="+12125550123",
            country="1",
        )
    )
    assert controller.get_phone() == "+12125550123"
    controller.cleanup()
    with pytest.raises(RuntimeError, match="locked"):
        controller.get_phone()
    assert not any(
        item.get("method") == "POST" and item.get("path") == "/api/order"
        for item in state["requests"]
    )


def test_token_url_json_ignores_expiration_year_and_reads_message_content(monkeypatch):
    class JsonResponse:
        headers = {"content-type": "application/json"}
        text = '{"code":0}'

        def __init__(self, content: str):
            self._content = content

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "code": 0,
                "data": {
                    "message_type": "SMS",
                    "description": "",
                    "fields": {"content": self._content},
                    "key_expiration_time": "2026-11-07T12:06:58Z",
                },
            }

    provider = Api668Provider("secret-token")
    monkeypatch.setattr(
        "regix.providers.sms.api668.requests.get",
        lambda *args, **kwargs: JsonResponse(""),
    )
    assert provider._code_from_token({"token_url": "https://example.test/token"}) == ""

    monkeypatch.setattr(
        "regix.providers.sms.api668.requests.get",
        lambda *args, **kwargs: JsonResponse("OpenAI verification code: 654321"),
    )
    assert provider._code_from_token({"token_url": "https://example.test/token"}) == "654321"
