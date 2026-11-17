"""Import already-registered accounts (credentials) into the registered table.

This is the counterpart of the mailbox-pool import: the pool import creates
addresses to *register*, while this import stores accounts that are already
registered elsewhere so the payment-link / Plus-check features can use them.

Accepted line formats:

1. JSON, one object per line (or one JSON array):

       {"email": "...", "password": "...", "totp_secret": "...",
        "access_token": "...", "refresh_token": "...", "session_token": "..."}

   Camel-case keys from a session dump (``accessToken`` etc.) and a nested
   ``tokens`` object are accepted too.

2. ``----`` separated, in this order (any trailing part may be omitted):

       email----password----totp_secret----access_token----refresh_token----session_token----id_token

An account is useful for payment links only when it has a refresh token (or at
least an access token), so lines without any token are rejected.
"""

from __future__ import annotations

import json
from typing import Any

# fields that are stored as columns (everything else is ignored on import)
COLUMNS = (
    "password",
    "totp_secret",
    "totp_factor_id",
    "access_token",
    "refresh_token",
    "session_token",
    "id_token",
    "device_id",
    "csrf_token",
    "cookie_header",
    "session_json",
)

_ALIASES = {
    "email": "email",
    "mail": "email",
    "username": "email",
    "password": "password",
    "pw": "password",
    "totp_secret": "totp_secret",
    "totp": "totp_secret",
    "two_factor_secret": "totp_secret",
    "totp_factor_id": "totp_factor_id",
    "factor_id": "totp_factor_id",
    "access_token": "access_token",
    "accesstoken": "access_token",
    "at": "access_token",
    "refresh_token": "refresh_token",
    "refreshtoken": "refresh_token",
    "rt": "refresh_token",
    "session_token": "session_token",
    "sessiontoken": "session_token",
    "st": "session_token",
    "id_token": "id_token",
    "idtoken": "id_token",
    "device_id": "device_id",
    "deviceid": "device_id",
    "oai_did": "device_id",
    "csrf_token": "csrf_token",
    "cookie_header": "cookie_header",
    "cookie": "cookie_header",
    "cookies": "cookie_header",
    "session_json": "session_json",
}

# order of the `----` format
DASH_ORDER = ("email", "password", "totp_secret", "access_token",
              "refresh_token", "session_token", "id_token")


class RegisteredImportError(ValueError):
    def __init__(self, message: str, errors: list[dict] | None = None) -> None:
        super().__init__(message)
        self.errors = errors or []


def _normalize_dict(raw: dict, line: int) -> dict:
    obj: dict[str, Any] = {}
    # nested "tokens" / "auth" objects are common in exported JSON
    for nested_key in ("tokens", "auth"):
        nested = raw.get(nested_key)
        if isinstance(nested, dict):
            obj.update(nested)
    obj.update(raw)

    row: dict[str, Any] = {}
    for key, value in obj.items():
        name = _ALIASES.get(str(key).strip().lower())
        if not name or value is None:
            continue
        if name == "session_json" and isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        row[name] = str(value).strip()

    email = row.get("email", "").strip().lower()
    if "@" not in email:
        raise ValueError(f"第 {line} 行缺少合法邮箱")
    row["email"] = email
    if not (row.get("access_token") or row.get("refresh_token")):
        raise ValueError(f"第 {line} 行没有任何 token（至少需要 access_token 或 refresh_token）")
    return row


def parse_registered_import(text: str) -> list[dict]:
    """Parse import text into rows. Raises RegisteredImportError with line details."""
    content = (text or "").strip()
    if not content:
        raise RegisteredImportError("导入内容为空")

    rows: list[dict] = []
    errors: list[dict] = []

    if content.startswith("["):
        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            raise RegisteredImportError(f"JSON 解析失败: {exc}") from exc
        if not isinstance(data, list):
            raise RegisteredImportError("JSON 顶层必须是数组或每行一个对象")
        for index, item in enumerate(data, start=1):
            if not isinstance(item, dict):
                errors.append({"line": index, "error": "不是 JSON 对象"})
                continue
            try:
                rows.append(_normalize_dict(item, index))
            except ValueError as exc:
                errors.append({"line": index, "error": str(exc)})
    else:
        for index, raw_line in enumerate(content.splitlines(), start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("{"):
                try:
                    item = json.loads(line)
                except json.JSONDecodeError as exc:
                    errors.append({"line": index, "error": f"JSON 解析失败: {exc}"})
                    continue
                if not isinstance(item, dict):
                    errors.append({"line": index, "error": "不是 JSON 对象"})
                    continue
                try:
                    rows.append(_normalize_dict(item, index))
                except ValueError as exc:
                    errors.append({"line": index, "error": str(exc)})
                continue

            parts = [p.strip() for p in line.split("----")]
            if len(parts) < 2:
                errors.append({"line": index, "error": "至少需要 邮箱----(密码或 token)"})
                continue
            if len(parts) == 2:
                # 两段既可能是 邮箱----密码，也可能是 邮箱----AT。
                # 按 JWT 形状认：AT 是 eyJ... 的长串；否则当密码。
                second = parts[1]
                row = {name: "" for name in DASH_ORDER}
                row["email"] = parts[0]
                if second.startswith("eyJ") or len(second) > 120:
                    row["access_token"] = second
                else:
                    row["password"] = second
            else:
                row = {name: parts[i] if i < len(parts) else "" for i, name in enumerate(DASH_ORDER)}
            try:
                rows.append(_normalize_dict(row, index))
            except ValueError as exc:
                errors.append({"line": index, "error": str(exc)})

    if errors:
        raise RegisteredImportError(
            f"有 {len(errors)} 行不合法，整批未导入", errors
        )
    if not rows:
        raise RegisteredImportError("没有解析出任何账号")
    return rows
