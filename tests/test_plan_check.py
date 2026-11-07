"""Tests for post-registration plan and Plus trial eligibility checks."""

from __future__ import annotations

import base64
import json
import time
from datetime import datetime, timezone

import pytest

from regix.services.plan_check import (
    ACCOUNTS_CHECK_PATH,
    AccountPlanResult,
    PlanCheckError,
    check_account_plan,
    check_registered_plan,
    error_to_check_dict,
    normalize_access_token,
    parse_accounts_check,
    plan_request_headers,
    result_to_check_dict,
)


def _jwt(account_id: str, *, plan_type: str = "free", exp: int | None = None) -> str:
    payload = {
        "https://api.openai.com/auth": {
            "chatgpt_account_id": account_id,
            "chatgpt_plan_type": plan_type,
        }
    }
    if exp is not None:
        payload["exp"] = exp
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


def _account_item(
    *,
    plan: str,
    active: bool,
    plus_campaign: str | None = None,
    account_id: str = "target",
) -> dict:
    campaigns = {}
    if plus_campaign:
        campaigns["plus"] = {"id": plus_campaign}
    return {
        "account": {"account_id": account_id, "plan_type": plan},
        "entitlement": {
            "subscription_plan": f"chatgpt{plan}plan",
            "has_active_subscription": active,
        },
        "eligible_promo_campaigns": campaigns,
    }


def test_normalize_access_token_and_headers():
    assert normalize_access_token(" token ") == "token"
    assert normalize_access_token("Bearer token") == "token"
    assert normalize_access_token("Authorization: Bearer token") == "token"

    headers = plan_request_headers("Bearer token", device_id="device-1")
    assert headers["authorization"] == "Bearer token"
    assert headers["oai-device-id"] == "device-1"
    assert headers["x-openai-target-path"] == ACCOUNTS_CHECK_PATH
    assert headers["x-openai-target-route"] == ACCOUNTS_CHECK_PATH


@pytest.mark.parametrize(
    ("item", "status", "eligible"),
    [
        (_account_item(plan="free", active=False, plus_campaign="plus-1-month-free"),
         "plus_eligible", True),
        (_account_item(plan="free", active=False), "free", False),
        (_account_item(plan="plus", active=True, plus_campaign="ignored"),
         "plus_active", False),
        (_account_item(plan="pro", active=True), "plus_active", False),
    ],
)
def test_parse_selects_jwt_account_and_classifies_plan(item, status, eligible):
    token = _jwt("target")
    result = parse_accounts_check(
        {"accounts": {"other": _account_item(plan="plus", active=True, account_id="other"), "target": item}},
        access_token=token,
    )
    info = result_to_check_dict(result)

    assert result.account_id == "target"
    assert result.plus_trial_eligible is eligible
    assert info["status"] == status


def test_deactivated_account_is_banned():
    token = _jwt("target")
    with pytest.raises(PlanCheckError, match="account_deactivated"):
        parse_accounts_check(
            {
                "accounts": {
                    "target": {
                        "account": {"account_id": "target", "is_deactivated": True}
                    }
                }
            },
            access_token=token,
        )

    info = error_to_check_dict(PlanCheckError("account_deactivated", http_status=401))
    assert info["status"] == "banned"
    assert info["http_status"] == 401


class FakeResponse:
    def __init__(self, status: int, payload: dict | bytes | None = None):
        self.status_code = status
        self.content = (
            payload if isinstance(payload, bytes)
            else json.dumps(payload or {}).encode()
        )
        self.headers: dict[str, str] = {}


class FakeSession:
    def __init__(self, responses: list[FakeResponse]):
        self.responses = responses
        self.requests: list[tuple[str, dict]] = []
        self.closed = False

    def get(self, url: str, **kwargs):
        self.requests.append((url, kwargs))
        return self.responses.pop(0)

    def close(self):
        self.closed = True


def test_check_retries_transient_failure_and_returns_secret_free_result():
    sessions = [
        FakeSession([FakeResponse(503)]),
        FakeSession(
            [
                FakeResponse(
                    200,
                    {
                        "accounts": {
                            "default": _account_item(
                                plan="free",
                                active=False,
                                plus_campaign="trial",
                            )
                        }
                    },
                )
            ]
        ),
    ]
    result = check_account_plan(
        "TEST_ACCESS_TOKEN",
        proxy_url="http://user:PRIVATE_PASSWORD@proxy.test:1000",
        retry_delay_seconds=0,
        session_factory=lambda: sessions.pop(0),
    )
    info = result_to_check_dict(result)

    assert info["plus_trial_eligible"] is True
    assert info["status"] == "plus_eligible"
    assert "TEST_ACCESS_TOKEN" not in repr(result)
    assert "PRIVATE_PASSWORD" not in repr(result)


def test_check_handles_401_without_leaking_response_body():
    session = FakeSession(
        [FakeResponse(401, b'{"error":"PRIVATE_RESPONSE_BODY"}')]
    )
    with pytest.raises(PlanCheckError) as exc_info:
        check_account_plan(
            "token",
            max_attempts=1,
            session_factory=lambda: session,
        )
    assert exc_info.value.code == "access_token_unauthorized"
    info = error_to_check_dict(exc_info.value)
    assert info["status"] == "token_invalid"
    assert "PRIVATE_RESPONSE_BODY" not in repr(info)
    assert session.closed is True


def test_expired_jwt_is_rejected_before_network_call():
    with pytest.raises(PlanCheckError, match="access_token_expired"):
        check_account_plan(_jwt("target", exp=int(time.time()) - 10))


def test_registered_plan_result_is_persisted_without_secrets(clean_db, monkeypatch):
    import regix.services.plan_check as module

    token = _jwt("target")
    clean_db.save_registered({"email": "plan@example.com", "access_token": token})
    result = AccountPlanResult(
        checked_at=datetime.now(timezone.utc),
        account_id="target",
        current_plan_type="free",
        subscription_plan="chatgptfreeplan",
        has_active_subscription=False,
        expires_at=None,
        renews_at=None,
        plus_trial_eligible=True,
        plus_trial_campaign_id="plus-1-month-free",
    )
    monkeypatch.setattr(module, "check_account_plan", lambda *args, **kwargs: result)

    info = check_registered_plan("PLAN@example.com", proxy_url="socks5h://proxy")
    saved = clean_db.get_registered("plan@example.com")
    serialized_check = json.dumps(saved["extra"]["plus_check"], ensure_ascii=False)

    assert info["status"] == "plus_eligible"
    assert info["plan_label"] == "Free"
    assert saved["extra"]["plus_check"]["plus_trial_eligible"] is True
    assert saved["extra"]["plus_check"]["plus_trial_campaign_id"] == "plus-1-month-free"
    assert token not in serialized_check
    assert "socks5h://proxy" not in serialized_check


def test_free_filter_does_not_include_plus_eligible(clean_db):
    clean_db.save_registered(
        {
            "email": "free@example.com",
            "access_token": "free",
            "plus_check": {"status": "free", "label": "Free"},
        }
    )
    clean_db.save_registered(
        {
            "email": "trial@example.com",
            "access_token": "trial",
            "plus_check": {"status": "plus_eligible", "label": "可领 Plus 试用"},
        }
    )
    assert [row["email"] for row in clean_db.list_registered(filter_rt="free")] == [
        "free@example.com"
    ]
    assert [row["email"] for row in clean_db.list_registered(filter_rt="plus")] == [
        "trial@example.com"
    ]


def test_manual_check_endpoint_uses_shared_plan_service(clean_db, monkeypatch):
    from regix.api.routers.exports import CheckPlusReq, api_check_plus
    import regix.services.plan_check as module

    clean_db.save_registered(
        {"email": "manual@example.com", "access_token": _jwt("manual")}
    )
    monkeypatch.setattr(
        module,
        "check_registered_plan",
        lambda email, **kwargs: {
            "status": "free",
            "label": "Free",
            "current_plan_type": "free",
            "plan_label": "Free",
            "plus_trial_eligible": False,
        },
    )
    response = api_check_plus(
        CheckPlusReq(emails=["manual@example.com"], proxy="")
    )
    assert response["ok"] is True
    assert response["results"]["manual@example.com"]["plan_label"] == "Free"


def test_registration_plan_helper_is_best_effort(monkeypatch):
    from regix.services import registration
    import regix.services.plan_check as module

    monkeypatch.setattr(
        module,
        "check_registered_plan",
        lambda email, **kwargs: {
            "status": "plus_active",
            "label": "Plus 生效中",
            "current_plan_type": "plus",
            "plan_label": "Plus",
            "plus_trial_eligible": False,
        },
    )
    info = registration._check_plan_after_registration(
        "run-plan",
        {"email": "active@example.com", "access_token": _jwt("active")},
    )
    assert info["status"] == "plus_active"
    assert info["plan_label"] == "Plus"

    assert registration._check_plan_after_registration(
        "run-no-at", {"email": "missing@example.com"}
    ) == {"status": "no_at", "label": "无AT"}
