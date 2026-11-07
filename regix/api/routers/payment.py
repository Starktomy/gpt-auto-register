"""Payment-link API routes.

The client only sends the account email plus the payment options; the access
token, session cookies and device id are read from the local database so no
credential ever travels through the browser.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ...storage import facade as db
from ...services.payment import (
    PAYMENT_PROFILES,
    PaymentLinkError,
    generate_payment_link_for_account,
)

router = APIRouter()
logger = logging.getLogger("webui")

# Machine-readable reason -> message the WebUI can show verbatim.
_ERROR_MESSAGES = {
    "email_missing": "缺少邮箱",
    "account_not_found": "未找到该账号",
    "access_token_missing": "该账号没有 access_token，无法生成支付链接",
    "payment_profile_unknown": "未知的支付区域预设",
    "checkout_unauthorized": "access_token 已失效（HTTP 401）",
    "checkout_forbidden": "OpenAI 拒绝了该请求（HTTP 403），与账号状态或出口 IP 有关",
    "checkout_response_invalid": "checkout 响应不是合法 JSON",
    "checkout_no_session": "checkout 响应里没有 checkout_session_id",
    "checkout_no_url": "未能生成可用的支付链接",
    "stripe_init_http_failed": "Stripe 初始化失败（HTTP {http_status}）",
    "stripe_init_response_invalid": "Stripe 初始化响应不是合法 JSON",
    "checkout_risk_blocked": "OpenAI 风控拦截：该账号被判定为异常活动，换个出口 IP 或稍后再试",
    "codex_token_missing": "该号没有 Codex refresh_token，支付接口只认 Codex 签发的 AT；先点「重登换 Codex token」",
    "codex_token_refresh_failed": "Codex refresh_token 刷新失败，需要重登一次",
    "checkout_country_mismatch": "账单国家必须与请求出口 IP 的国家一致（当前出口 {exit_country}），改用对应区域或先切换代理",
    "checkout_rate_limited": "OpenAI 限流了，稍后再试",
}


class PaymentPrepareReq(BaseModel):
    email: str = Field(..., description="已注册账号的邮箱")
    proxy: str = Field("", description="代理；留空则用该号注册时的代理")
    otp_timeout: int = Field(180, description="等待邮件 OTP 的秒数")


class PaymentLinkReq(BaseModel):
    email: str = Field(..., description="已注册账号的邮箱")
    plan: str = Field("plus", description="plus / team")
    profile: str = Field("", description="支付区域预设 id，例如 paypal_de")
    country: str = Field("", description="账单国家，优先于 profile")
    currency: str = Field("", description="账单币种，优先于 profile")
    locale: str = Field("", description="支付页语言标签")
    workspace_name: str = Field("", description="Team 工作区名称")
    interval: str = Field("month", description="Team 周期：month / year")
    seats: int = Field(2, description="Team 席位数")
    promo_code: str = Field("", description="自定义优惠码（手输）")
    promo_campaign_id: str = Field("", description="官方试用活动 id，例如 plus-1-month-free")
    proxy: str = Field("", description="代理；留空则用该号注册时的代理")


@router.get("/api/payment/account/{email}")
def api_payment_account(email: str):
    """Non-secret metadata the payment dialog needs for one account.

    Returns the registration proxy (so later requests can leave from the same
    IP) and the trial campaign id the account is eligible for (so the promo
    field can be filled from the account instead of typed by hand).
    """
    normalized = str(email or "").strip().lower()
    row = db.get_registered(normalized)
    if not row:
        raise HTTPException(404, "未找到该账号")
    extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
    plus = extra.get("plus_check") if isinstance(extra.get("plus_check"), dict) else {}
    return {
        "ok": True,
        "email": normalized,
        "proxy": str(extra.get("proxy") or "").strip(),
        "promo_campaign_id": str(plus.get("plus_trial_campaign_id") or "").strip(),
        "plus_trial_eligible": bool(plus.get("plus_trial_eligible")),
        "plus_status": str(plus.get("status") or ""),
        "has_refresh_token": bool(str(row.get("refresh_token") or "").strip()),
        "has_access_token": bool(str(row.get("access_token") or "").strip()),
    }


@router.post("/api/payment/prepare")
def api_payment_prepare(req: PaymentPrepareReq):
    """Re-login an account so it has a Codex refresh token.

    The payments endpoint only accepts an access token minted by the Codex
    client; accounts registered without the Codex chain have none. This runs
    the login chain once and stores the refresh token.
    """
    from ...services.codex_login import CodexLoginError, mint_codex_refresh_token

    normalized = str(req.email or "").strip().lower()
    row = db.get_registered(normalized)
    if not row:
        raise HTTPException(400, "未找到该账号")
    extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
    effective_proxy = str(req.proxy or "").strip() or str(extra.get("proxy") or "").strip()
    try:
        info = mint_codex_refresh_token(
            normalized,
            proxy=effective_proxy,
            otp_timeout=int(req.otp_timeout or 180),
        )
    except CodexLoginError as exc:
        logger.info("[payment] prepare failed %s: %s", normalized, exc)
        raise HTTPException(400, f"重登失败: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        logger.warning("[payment] prepare unexpected error %s: %s", normalized, exc)
        raise HTTPException(500, f"重登失败: {exc}") from exc
    return {"ok": True, **info}


@router.get("/api/payment/profiles")
def api_payment_profiles(proxy: str = ""):
    """List the country/currency presets offered by the payment page.

    Also reports the country the request would exit from: OpenAI rejects a
    preset whose country differs from the exit IP's country.
    """
    from ...services.payment import detect_exit_country

    return {
        "ok": True,
        "exit_country": detect_exit_country(proxy),
        "items": [
            {
                "id": item.id,
                "label": item.label,
                "country": item.country,
                "currency": item.currency,
                "locale": item.locale,
                "note": item.note,
            }
            for item in PAYMENT_PROFILES
        ],
    }


@router.post("/api/payment/link")
def api_payment_link(req: PaymentLinkReq):
    """Create a checkout session for one account and return its links."""
    try:
        link = generate_payment_link_for_account(
            req.email,
            plan=req.plan,
            profile=req.profile,
            country=req.country,
            currency=req.currency,
            locale=req.locale,
            workspace_name=req.workspace_name,
            interval=req.interval,
            seats=req.seats,
            promo_code=req.promo_code,
            promo_campaign_id=req.promo_campaign_id,
            proxy=req.proxy,
        )
    except PaymentLinkError as exc:
        message = _ERROR_MESSAGES.get(exc.code, exc.code)
        if "{http_status}" in message:
            message = message.format(http_status=exc.http_status)
        if "{exit_country}" in message:
            message = message.format(exit_country=exc.exit_country or "未知")
        logger.info("[payment] %s failed code=%s http=%s", req.email, exc.code, exc.http_status)
        # 带上机读 code，前端据此决定要不要提示「重登换 Codex token」
        return JSONResponse(status_code=400, content={"detail": message, "code": exc.code})
    except Exception as exc:  # noqa: BLE001 - surface the failure to the UI
        logger.warning("[payment] %s unexpected error: %s", req.email, exc)
        raise HTTPException(500, f"生成支付链接失败: {exc}") from exc

    data = link.to_dict()
    return {"ok": True, "email": str(req.email or "").strip().lower(), "data": data}
