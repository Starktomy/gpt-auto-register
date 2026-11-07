"""OpenAI registration and login flow composition."""

import logging
import os
from typing import Any, Optional

from regix.config import Config
from regix.fingerprint.generator import generate_fingerprint
from regix.http.client import create_http_session

from .models import AuthResult
from .client import ClientMixin
from .oauth import OAuthMixin
from .challenges import ChallengeMixin
from .token import TokenMixin
from .protocol import ProtocolMixin
from .session import SessionMixin
from .registration import RegistrationMixin

logger = logging.getLogger(__name__)


class AuthFlow(
    ClientMixin,
    OAuthMixin,
    ChallengeMixin,
    TokenMixin,
    ProtocolMixin,
    SessionMixin,
    RegistrationMixin,
):
    """Compose protocol-domain mixins without changing the public flow API."""

    def __init__(
        self,
        config: Config,
        sms_callback: Optional[Any] = None,
        env_overrides: Optional[dict] = None,
        on_password: Optional[Any] = None,
        on_session_ready: Optional[Any] = None,
        account_callback: Optional[Any] = None,
    ):
        # 本次流程专属的配置覆盖（WEBUI_ALLOW_LOGIN / OTP_TIMEOUT / OAuth 开关等）。
        # ⚠️ 以前 registrar 是直接写 os.environ 再在 finally 里还原的，
        #    但 auto_loop 会并发跑多个 worker —— A 写的 OTP_TIMEOUT 会被 B 看见，
        #    B 跑完还原成 A 之前的值，A 后半程就读到别人的配置了。
        #    现在覆盖值只挂在实例上，进程全局环境一个字节都不动。
        self._env_overrides = dict(env_overrides or {})
        self.config = config
        self._country_code = ""  # IP 地理国家码，check_proxy() 时填充
        self._fingerprint = generate_fingerprint()  # 先生成默认指纹
        self._ua = self._fingerprint["user_agent"]
        self._impersonate_candidates = self._fingerprint.get(
            "fallback_impersonates",
            [self._fingerprint["impersonate"], "safari17_0", "safari15_5"],
        )
        self._impersonate_idx = 0
        self.session = create_http_session(
            proxy=config.proxy,
            impersonate=self._impersonate_candidates[self._impersonate_idx],
            user_agent=self._ua,
        )
        self.result = AuthResult()
        # 可选 SMS 接码控制器（sms_provider.PhoneCallbackController 实例）
        # 命中 add-phone 时自动租手机号 + 接 SMS 验证码，否则回退到环境变量路径
        self._sms_callback = sms_callback
        # 密码一在 OpenAI 侧生效就回调出去，调用方负责立刻落盘。
        # 签名 (email: str, password: str) -> None，异常由 register_password 吞掉。
        # ⚠️ 协议层不认识 webui.db，所以只给回调，"存哪"留给调用方决定，
        #    auth_flow 单独当 CLI 用时不传就是了，行为和以前一模一样。
        self._on_password = on_password
        # 拿到 session（access_token）之后、Codex 授权之前的钩子。
        # 签名 (flow: AuthFlow, access_token: str) -> None，异常由调用点吞掉。
        # 为的是把 2FA 绑定插进主人指定的顺序：
        #     创建账户 → 重定向链 → 拿 session → ★绑 2FA★ → Codex 授权 → 接码
        # ⚠️ 传了这个钩子会**顺带关掉** run_register 里 callback 前那次 Codex 抢跑
        #    （:3051 OAUTH_CODEX_RT_BEFORE_CALLBACK），否则 Codex 会跑在钩子前面，
        #    顺序就白调了。不传则一个字节都不变，老行为。
        self._on_session_ready = on_session_ready
        # 账号凭证回调：已有账号登录时从数据库加载密码和 totp_secret。
        # 签名 (email: str) -> dict，返回 {"password": "...", "totp_secret": "..."}。
        # 用于 mfa-challenge 路径：密码验证后需要 TOTP 码，从库里读 secret。
        self._account_callback = account_callback
        self._http_trace_enabled = str(os.getenv("AUTH_HTTP_TRACE", "0")).lower() in ("1", "true", "yes", "on")
        # signup() 会在分支里 set；run_protocol_login 命中已有账号路径会跳过 signup，
        # 导致 kickoff_otp_delivery 读未初始化属性 AttributeError。这里给个默认值。
        self._is_existing_account = False
        self._existing_email_verification_mode = ""
        self._existing_page_type = ""
        self._manual_login_verifier = (os.getenv("LOGIN_VERIFIER", "") or "").strip()
        self._captured_login_verifier = ""
        self._oauth_client_secret = (os.getenv("OAUTH_CLIENT_SECRET", "") or "").strip()
        self._oauth_client_id = "YOUR_OPENAI_WEB_CLIENT_ID"
        self._oauth_redirect_uri = "https://chatgpt.com/api/auth/callback/openai"
        self._oauth_scope = ""
        self._oauth_state = ""
        self._oauth_auth_url = ""
        self._client_auth_session_dump: dict[str, Any] = {}
        self._client_auth_session_id: str = ""
        self._dump_login_verifier: str = ""
        self._codex_rt_attempted: bool = False
        self._sms_phone_attempted: bool = False
        self._trace_dump_enabled = str(os.getenv("AUTH_TRACE_DUMP", "0")).lower() in ("1", "true", "yes", "on")
        self._trace_include_cookie = str(os.getenv("AUTH_TRACE_INCLUDE_COOKIE", "0")).lower() in (
            "1", "true", "yes", "on"
        )
        self._trace_dump_path = ""
        logger.info(
            f"指纹: type={self._fingerprint['browser_type']} "
            f"impersonate={self._fingerprint['impersonate']} "
            f"screen={self._fingerprint['screen']} lang={self._fingerprint['lang']} "
            f"tz={self._fingerprint.get('timezone', '')} "
            f"ua={self._ua[:80]}"
        )
