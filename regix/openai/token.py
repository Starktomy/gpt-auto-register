from __future__ import annotations

import base64
import json
import logging
import os
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from regix.providers.mail import MailProvider

logger = logging.getLogger(__name__)


class TokenMixin:
    """Protocol methods for this domain."""

    def oauth_codex_rt_exchange(self, mail_provider: Optional[MailProvider] = None) -> bool:
        """
        纯协议方式获取 RT（参考 any-regix）：
        - 使用独立 Codex OAuth 参数重新授权（可控 PKCE）
        - 捕获 callback code（不消费）
        - 直接调 /oauth/token 交换 access_token + refresh_token
        """
        allow_retry = self._env_flag("OAUTH_CODEX_RT_ALLOW_RETRY", "0")
        if self._codex_rt_attempted and (not allow_retry):
            logger.debug("Codex RT 本轮已尝试过，跳过重复尝试")
            return False
        self._codex_rt_attempted = True

        logger.info("尝试 Codex OAuth 直连换取 refresh_token ...")
        try:
            auth_url, state, verifier, redirect_uri, client_id = self._build_codex_authorize()
            self._oauth_auth_url = auth_url
            self._oauth_client_id = client_id
            self._oauth_redirect_uri = redirect_uri
            self._oauth_state = state
            self._manual_login_verifier = verifier
            self._captured_login_verifier = verifier
            callback_url, final_url = self._follow_authorize_for_callback(
                auth_url, redirect_uri, "codex_authorize"
            )

            login_attempted = False

            def _drive_login_once() -> None:
                nonlocal callback_url, final_url, login_attempted
                if login_attempted:
                    return
                login_attempted = True
                logger.info("Codex 授权回落到 /log-in，尝试协议推进登录状态...")
                try:
                    continue_url = self._codex_drive_login_from_log_in(mail_provider=mail_provider)
                except Exception as e:
                    logger.warning(f"Codex 登录推进失败，改走 no-prompt 兜底: {e}")
                    return
                if continue_url:
                    if self._is_add_phone_state(page_type="", continue_url=continue_url) and self._env_flag(
                        "OAUTH_CODEX_ADD_PHONE_REFRESH_RETRY", "1"
                    ):
                        try:
                            retry_count = max(1, int(os.getenv("OAUTH_CODEX_ADD_PHONE_REFRESH_RETRY_COUNT", "3")))
                        except Exception:
                            retry_count = 3
                        try:
                            retry_sleep = max(0.0, float(os.getenv("OAUTH_CODEX_ADD_PHONE_REFRESH_SLEEP", "1.2")))
                        except Exception:
                            retry_sleep = 1.2
                        logger.info("命中 add-phone，执行 authorize 刷新重试: count=%s sleep=%.1fs", retry_count, retry_sleep)
                        callback_url, final_url = self._codex_refresh_retry_after_add_phone(
                            auth_url=auth_url,
                            redirect_uri=redirect_uri,
                            attempts=retry_count,
                            sleep_seconds=retry_sleep,
                        )
                    else:
                        callback_url, final_url = self._follow_authorize_for_callback(
                            continue_url,
                            redirect_uri,
                            "codex_post_login",
                        )

            # The initial authorize can redirect to /log-in. Some auth sessions
            # only reveal that after removing prompt=login, so this is checked
            # again after the no-prompt fallback below.
            if (not callback_url) and "/log-in" in (final_url or ""):
                _drive_login_once()

            # Codex authorize 直接被打到 /add-phone（不经过 /log-in）：
            # 如果配了 SMS 接码 controller，先把手机号绑了再重新 authorize
            if (not callback_url) and self._is_add_phone_state(page_type="", continue_url=final_url or "") \
                    and self._sms_callback is not None:
                logger.info("Codex 授权直接落到 /add-phone，尝试 SMS 接码绑号 ...")
                try:
                    self._handle_add_phone_via_sms(continue_url=final_url)
                    # 绑号成功后重新 authorize 拿 callback code
                    callback_url, final_url = self._follow_authorize_for_callback(
                        auth_url, redirect_uri, "codex_authorize_after_add_phone"
                    )
                    if not callback_url:
                        no_prompt_url = self._drop_query_keys(auth_url, {"prompt"})
                        if no_prompt_url and no_prompt_url != auth_url:
                            callback_url, final_url = self._follow_authorize_for_callback(
                                no_prompt_url,
                                redirect_uri,
                                "codex_authorize_noprompt_after_add_phone",
                            )
                except Exception as e:
                    logger.warning(f"SMS 接码绑号失败: {e}")

            # 兜底：去掉 prompt=login 再发起一次授权
            if not callback_url:
                no_prompt_url = self._drop_query_keys(auth_url, {"prompt"})
                if no_prompt_url and no_prompt_url != auth_url:
                    callback_url, final_url = self._follow_authorize_for_callback(
                        no_prompt_url,
                        redirect_uri,
                        "codex_authorize_noprompt",
                    )

            if (not callback_url) and "/log-in" in (final_url or ""):
                _drive_login_once()

            if not callback_url:
                logger.warning("Codex OAuth 未捕获 callback code, final=%s", (final_url or "")[:180])
                return False
            return self._exchange_codex_callback_code(
                callback_url=callback_url,
                expected_state=state,
                verifier=verifier,
                redirect_uri=redirect_uri,
                client_id=client_id,
            )
        except Exception as e:
            logger.warning(f"Codex OAuth 交换异常: {e}")
            return False

    def _inject_pkce_into_auth_url(self, auth_url: str) -> str:
        """为 authorize URL 注入 PKCE 参数（可选）。"""
        if not auth_url:
            return auth_url
        if not self._env_flag("OAUTH_SECONDARY_PKCE", "0"):
            return auth_url

        try:
            parsed = urlparse(auth_url)
            params = dict(parse_qsl(parsed.query, keep_blank_values=True))
            if params.get("code_challenge") and params.get("code_challenge_method"):
                return auth_url

            verifier, challenge = self._build_pkce_pair()
            params["code_challenge"] = challenge
            params["code_challenge_method"] = "S256"
            new_url = urlunparse(parsed._replace(query=urlencode(params)))
            # 若用户未手动指定 verifier，则自动注入本轮 verifier
            if not self._manual_login_verifier:
                self._manual_login_verifier = verifier
            logger.info(
                "已启用二次 PKCE 注入: verifier_len=%s challenge=%s...",
                len(verifier),
                challenge[:16],
            )
            return new_url
        except Exception as e:
            logger.warning(f"注入 PKCE 参数失败，回退原始 auth_url: {e}")
            return auth_url

    @staticmethod
    def _safe_b64url_decode_text(data: str) -> str:
        if not data:
            return ""
        try:
            s = data + "=" * (-len(data) % 4)
            return base64.urlsafe_b64decode(s.encode("utf-8")).decode("utf-8", errors="replace")
        except Exception:
            return ""

    def _extract_hydra_redirect_values(self) -> list[str]:
        """从 hydra_redirect cookie 中提取可能的会话值。"""
        raw = self._get_cookie_value_by_name("hydra_redirect")
        if not raw:
            return []
        out: list[str] = []
        try:
            p0 = (raw.split(".", 1)[0] or "").strip()
            text = self._safe_b64url_decode_text(p0)
            if text:
                obj = json.loads(text)
                if isinstance(obj, dict):
                    for v in obj.values():
                        if isinstance(v, str) and v.strip():
                            vv = v.strip()
                            out.append(vv)
                            if "|" in vv:
                                out.extend([x for x in vv.split("|") if isinstance(x, str) and x.strip()])
        except Exception:
            return out
        return out

    def _collect_code_verifier_candidates(self, callback_url: str, continue_url: str) -> list[tuple[str, str]]:
        """收集 code_verifier 候选（来源 + 值）。"""
        raw_candidates: list[tuple[str, str]] = [
            ("query", self._extract_query_first(continue_url, ["login_verifier", "code_verifier", "verifier"])),
            ("query_callback", self._extract_query_first(callback_url, ["login_verifier", "code_verifier", "verifier"])),
            ("dump", self._dump_login_verifier),
            ("captured", self._captured_login_verifier),
            ("manual", self._manual_login_verifier),
            ("cookie_login_verifier", self._get_cookie_value_by_name("login_verifier")),
            ("cookie_code_verifier", self._get_cookie_value_by_name("code_verifier")),
            ("cookie_login_challenge", self._extract_login_challenge_from_cookie()),
            ("cookie_nextauth_state", self._get_cookie_value_by_name("__Secure-next-auth.state")),
        ]

        # hydra_redirect 中可能包含编码后的 csrf/session 串，作为实验候选
        for i, hv in enumerate(self._extract_hydra_redirect_values()):
            raw_candidates.append((f"hydra_{i}", hv))

        out: list[tuple[str, str]] = []
        seen: set[str] = set()

        max_len = max(128, int(os.getenv("OAUTH_MAX_VERIFIER_LEN", "4096")))
        for src, val in raw_candidates:
            v = (val or "").strip()
            if not v:
                continue
            if len(v) > max_len:
                v = v[:max_len]
            if v not in seen:
                seen.add(v)
                out.append((src, v))
            # PKCE 标准长度 43~128；对超长候选补一个截断版本
            if len(v) > 128:
                v128 = v[:128]
                if v128 not in seen:
                    seen.add(v128)
                    out.append((f"{src}_trunc128", v128))

        return out
