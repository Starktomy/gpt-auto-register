"""Codex client access-token exchange.

The checkout endpoint of ChatGPT only accepts an access token that was
minted by the **Codex client** (``client_id=app_EMoamEEZ73f0CkXaXp7hrann``).
The token stored in ``registered.access_token`` is the one the web session
endpoint hands out (``client_id=app_X8zY6vW2pQ9tR3dE7nK1jL5gH``,
``amr=otp``) and the checkout endpoint rejects it with

    HTTP 400 {"detail": "Our systems have detected unusual activity. Please try again later."}

which looks exactly like an account-level fraud block but is not one: the
account is fine, only the token flavour is wrong. Measured 2026-11-07 on four
accounts: web-session token -> 400, Codex token -> 200 (checkout session
created).

So: a stored ``refresh_token`` is exchanged here for a Codex access token
before talking to the payments API.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

OPENAI_TOKEN_ENDPOINT = "https://auth.openai.com/oauth/token"
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_SCOPE = "openid email profile offline_access"
DEFAULT_TIMEOUT = 30


def _import_cffi():
    try:
        from curl_cffi import requests as cffi_requests
        return cffi_requests
    except ImportError as exc:  # pragma: no cover - curl_cffi is a hard dependency
        raise RuntimeError(f"curl_cffi 未安装，无法刷新 Codex token: {exc}") from exc


def refresh_codex_token(
    refresh_token: str,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    proxy: str = "",
) -> dict[str, Any]:
    """Exchange a refresh token for a fresh Codex access token.

    Returns OpenAI's raw response dict (``access_token`` / ``refresh_token`` /
    ``id_token`` / ``expires_in``). Raises ``RuntimeError`` on failure.
    """
    rt = str(refresh_token or "").strip()
    if not rt:
        raise RuntimeError("缺少 refresh_token，无法刷新 Codex access_token")

    cffi = _import_cffi()
    body = {
        "grant_type": "refresh_token",
        "client_id": CODEX_CLIENT_ID,
        "refresh_token": rt,
        "scope": CODEX_SCOPE,
    }
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "Origin": "https://auth.openai.com",
        "Referer": "https://auth.openai.com/",
    }
    kwargs: dict[str, Any] = {
        "headers": headers,
        "data": body,
        "verify": False,
        "timeout": timeout,
        "impersonate": "chrome110",
    }
    if proxy:
        kwargs["proxies"] = {"https": proxy, "http": proxy}
    else:
        kwargs["proxies"] = None

    resp = cffi.post(OPENAI_TOKEN_ENDPOINT, **kwargs)
    if resp.status_code != 200:
        text = ""
        try:
            text = (resp.text or "")[:300]
        except Exception:
            pass
        raise RuntimeError(f"Codex token 刷新失败 HTTP {resp.status_code}: {text}")
    try:
        data = resp.json()
    except Exception as exc:
        raise RuntimeError(f"Codex token 刷新响应不是 JSON: {exc}") from exc
    if not isinstance(data, dict) or not data.get("access_token"):
        raise RuntimeError("Codex token 刷新响应里没有 access_token")
    return data
