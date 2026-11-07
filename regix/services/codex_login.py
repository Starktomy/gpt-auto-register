"""Mint a Codex refresh token for an account that does not have one.

Why this exists
---------------
The ChatGPT checkout endpoint only accepts an access token that was minted by
the **Codex client**. An account registered without the Codex chain has no
``refresh_token``, so the only token in the database is the web-session one,
and the payments API answers::

    HTTP 400 {"detail": "Our systems have detected unusual activity. ..."}

which looks like an account-level fraud block but is only a token-flavour
problem. Measured 2026-11-07 on the same account: web-session token -> 400,
Codex token -> 200 (checkout session created).

So for those accounts the fix is to log the account back in and run the Codex
authorization once, which mints a refresh token. The flow reuses the existing
login chain (``run_protocol_login``) plus the Codex OAuth exchange
(``oauth_codex_rt_exchange``); the mailbox is still readable through the pool
row, so a verification mail can be fetched when the login asks for one.
"""

from __future__ import annotations

import logging
from typing import Optional

from regix.config import Config
from regix.openai.auth_flow import AuthFlow
from regix.providers.sms.provider import PhoneCallbackController
from regix.storage import facade as db

logger = logging.getLogger(__name__)


class CodexLoginError(RuntimeError):
    """Re-login failed; the account row was left untouched."""


def _build_mail_provider(email: str):
    row = db.get_account(email)
    if not row:
        raise CodexLoginError(f"号池里没有 {email}，取不到验证码用的邮箱凭证")
    kind = (row.get("kind") or "outlook").strip().lower()
    if kind != "outlook":
        raise CodexLoginError(f"暂只支持 outlook 号池重登（当前是 {kind}）")
    from regix.providers.mail.outlook import OutlookMailProvider

    return OutlookMailProvider(
        email=email,
        password=row.get("password") or "",
        client_id=row.get("client_id") or "",
        refresh_token=row.get("refresh_token") or "",
    )


def _build_sms_callback() -> Optional[PhoneCallbackController]:
    cfg = db.get_sms_internal_config()
    if not cfg.get("sms_enabled"):
        return None
    api_key = (cfg.get("sms_api_key") or "").strip()
    if not api_key:
        return None
    try:
        return PhoneCallbackController(
            provider_key=cfg["sms_provider"],
            config=cfg,
            service=cfg.get("sms_service") or "openai",
            country=cfg.get("sms_country") or "52",
            log_fn=lambda msg: logger.info("[sms] %s", msg),
            auto_select_country=bool(cfg.get("sms_auto_country")),
        )
    except Exception as exc:  # noqa: BLE001 - SMS is optional here
        logger.warning("[codex-login] 创建接码 controller 失败: %s", exc)
        return None


def mint_codex_refresh_token(
    email: str,
    *,
    proxy: str = "",
    otp_timeout: int = 180,
    save: bool = True,
) -> dict:
    """Log an existing account back in and mint a Codex refresh token.

    Returns a dict with ``ok``, ``has_refresh_token`` and the token lengths.
    Raises :class:`CodexLoginError` when the login or the exchange fails.
    """
    normalized = str(email or "").strip().lower()
    row = db.get_registered(normalized)
    if not row:
        raise CodexLoginError(f"注册结果里没有 {normalized}")
    password = str(row.get("password") or "").strip()
    if not password:
        raise CodexLoginError(f"{normalized} 没有密码，无法程序化重登")

    mail = _build_mail_provider(normalized)

    cfg = Config()
    cfg.proxy = str(proxy or "").strip() or None
    env_overrides = {
        # 走「已有账号」登录链，不新建账号
        "WEBUI_ALLOW_LOGIN": "1",
        "OTP_TIMEOUT": str(max(60, int(otp_timeout or 180))),
        # 只要 Codex RT，不要二次授权
        "OAUTH_CODEX_RT_EXCHANGE": "1",
        "OAUTH_CODEX_RT_BEFORE_CALLBACK": "0",
        "OAUTH_SECONDARY_AUTHORIZE_EXCHANGE": "0",
    }

    def _account_callback(_email: str) -> dict:
        return {
            "password": str(row.get("password") or ""),
            "totp_secret": str(row.get("totp_secret") or ""),
        }

    flow = AuthFlow(
        cfg,
        sms_callback=_build_sms_callback(),
        env_overrides=env_overrides,
        account_callback=_account_callback,
    )
    try:
        flow.run_protocol_login(mail, normalized, password)
        flow.oauth_codex_rt_exchange(mail_provider=mail)
    except Exception as exc:  # noqa: BLE001
        raise CodexLoginError(f"重登失败: {exc}") from exc

    result = flow.result.to_dict()
    refresh_token = str(result.get("refresh_token") or "").strip()
    if not refresh_token:
        raise CodexLoginError("重登成功但没有拿到 refresh_token")

    if save:
        # save_registered 是整行替换：空值不覆盖旧的密码 / 2FA secret，
        # 但 token 三件套会照常换成新的（这里正是我们要的）。
        db.save_registered(
            {
                "email": normalized,
                "password": password,
                "access_token": str(result.get("access_token") or row.get("access_token") or ""),
                "session_token": str(result.get("session_token") or row.get("session_token") or ""),
                "refresh_token": refresh_token,
                "id_token": str(result.get("id_token") or row.get("id_token") or ""),
                "cookie_header": str(row.get("cookie_header") or ""),
                "device_id": str(row.get("device_id") or ""),
                "totp_secret": str(row.get("totp_secret") or ""),
                "totp_factor_id": str(row.get("totp_factor_id") or ""),
            }
        )
        logger.info("[codex-login] %s 已写入新的 refresh_token", normalized)

    return {
        "ok": True,
        "email": normalized,
        "has_refresh_token": True,
        "refresh_token_len": len(refresh_token),
        "access_token_len": len(str(result.get("access_token") or "")),
    }
