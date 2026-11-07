from __future__ import annotations

import base64
import hashlib
import hmac
import struct
import time


# ── RFC 6238 TOTP 实现（用于 mfa-challenge 计算动态码）────────────
def _hotp(secret_b32: str, counter: int, digits: int = 6) -> str:
    """HOTP 算法（RFC 4226）"""
    key = base64.b32decode(secret_b32 + "=" * (-len(secret_b32) % 8))
    msg = struct.pack(">Q", counter)
    h = hmac.new(key, msg, hashlib.sha1).digest()
    o = h[-1] & 0x0F
    code = (struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def _totp_now(secret_b32: str) -> str:
    """当前 30 秒窗口的 6 位 TOTP 码"""
    return _hotp(secret_b32, int(time.time()) // 30)


class AuthResult:
    """认证结果"""

    def __init__(self):
        self.email: str = ""
        self.password: str = ""
        self.session_token: str = ""
        self.access_token: str = ""
        self.device_id: str = ""
        self.csrf_token: str = ""
        self.id_token: str = ""
        self.refresh_token: str = ""
        self.cookie_header: str = ""
        self.totp_secret: str = ""
        # /api/auth/session 的**完整响应 JSON 原文**。
        #
        # 为什么存整份而不只存 accessToken / sessionToken：
        #   · expires —— access_token 的到期时间。丢了就没法提前判断
        #     "这个号什么时候得重新注册"（尤其在关掉 refresh_token 之后）。
        #   · user.id —— OpenAI 侧账号的唯一标识，比 email 更稳定。
        #   · user.groups / idp / picture 等 —— 排查账号状态时用得上。
        # 之前只提取了两个字段就丢弃了完整响应，这里补上。
        self.session_json: str = ""

    def is_valid(self) -> bool:
        return bool(self.session_token and self.access_token)

    def to_dict(self) -> dict:
        return {
            "email": self.email,
            "password": self.password,
            "session_token": self.session_token,
            "access_token": self.access_token,
            "device_id": self.device_id,
            "csrf_token": self.csrf_token,
            "id_token": self.id_token,
            "refresh_token": self.refresh_token,
            "cookie_header": self.cookie_header,
            "totp_secret": self.totp_secret,
            "session_json": self.session_json,
        }
