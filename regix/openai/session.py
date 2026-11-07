from __future__ import annotations

import json
import logging
import os
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlparse, urlunparse


logger = logging.getLogger(__name__)


class SessionMixin:
    """Protocol methods for this domain."""

    def _reauthorize_for_session(self, original_auth_url: str) -> str | None:
        """已有账号 OTP 验证后，重新发起 authorize 获取 callback URL"""
        logger.info("[9.5/10] 重新 authorize 获取 session ...")
        try:
            # 去掉 prompt=login 参数，利用已有的 auth session cookie
            parsed = urlparse(original_auth_url)
            params = parse_qs(parsed.query, keep_blank_values=True)
            params.pop("prompt", None)
            # 重新构建 URL
            new_query = urlencode({k: v[0] for k, v in params.items()})
            authorize_url = urlunparse(parsed._replace(query=new_query))

            resp = self.session.get(
                authorize_url,
                allow_redirects=False,
                timeout=15,
            )
            self._trace_http("reauthorize_start", resp)
            logger.info(f"reauthorize status={resp.status_code}")

            # 跟随 redirect chain 找到 callback URL
            current_url = resp.headers.get("Location", "")
            logger.info(f"reauthorize Location: {current_url[:150]}")
            if resp.status_code in (301, 302, 303, 307, 308) and current_url:
                for hop in range(10):
                    logger.debug(f"reauthorize redirect hop {hop+1}: {current_url[:100]}")
                    if "code=" in current_url and "state=" in current_url:
                        logger.info("reauthorize: 找到 callback URL")
                        return current_url
                    try:
                        hop_resp = self.session.get(
                            current_url,
                            allow_redirects=False,
                            timeout=15,
                        )
                        self._trace_http(f"reauthorize_hop_{hop+1}", hop_resp)
                        next_loc = hop_resp.headers.get("Location", "")
                        if hop_resp.status_code not in (301, 302, 303, 307, 308) or not next_loc:
                            # 检查最终 URL
                            final_url = str(getattr(hop_resp, 'url', current_url))
                            if "code=" in final_url:
                                return final_url
                            break
                        current_url = next_loc
                        if not current_url.startswith("http"):
                            from urllib.parse import urljoin
                            current_url = urljoin(authorize_url, current_url)
                    except Exception:
                        break
            logger.warning("reauthorize: 未能获取 callback URL")
            return None
        except Exception as e:
            logger.warning(f"reauthorize 失败: {e}")
            return None

    def _extract_session_cookie(self) -> str:
        """多路兜底提取 __Secure-next-auth.session-token cookie。

        curl_cffi 在某些情况下按 domain 隔离 cookie，session.cookies.get(name) 拿不到，
        所以这里把所有 cookie 都遍历一遍，按名字精确匹配。
        """
        target = "__Secure-next-auth.session-token"
        # 路径1：直接 get
        try:
            v = self.session.cookies.get(target, "")
            if v:
                return v
        except Exception:
            pass
        # 路径2：遍历 jar
        try:
            for c in self.session.cookies:
                name = getattr(c, "name", "") if hasattr(c, "name") else str(c)
                if name == target:
                    val = getattr(c, "value", "") or ""
                    if val:
                        return val
        except Exception:
            pass
        # 路径3：用 _get_cookie_value_by_name（不挑 domain）
        try:
            return self._get_cookie_value_by_name(target)
        except Exception:
            return ""

    def get_auth_session(self) -> tuple[str, str]:
        """获取 session_token 和 access_token。

        session_token 三路兜底（按优先级）：
          1. cookie `__Secure-next-auth.session-token`（NextAuth 数据库 session 策略）
          2. JSON 响应里的 `sessionToken` 字段（NextAuth JWT session 策略，某些路径）
          3. 兼容大小写 / 下划线变体
        access_token 取 JSON 响应里的 `accessToken`。
        """
        first_call = not getattr(self, "_auth_session_fetched", False)
        self._auth_session_fetched = True
        if first_call:
            logger.info("[10/10] 获取认证 Session...")
        headers = self._common_headers("https://chatgpt.com/")
        resp = self.session.get(
            "https://chatgpt.com/api/auth/session",
            headers=headers,
            timeout=30,
        )
        self._trace_http("chatgpt_auth_session", resp)
        resp.raise_for_status()

        try:
            sess_json = resp.json() if resp is not None else {}
        except Exception:
            sess_json = {}
        if not isinstance(sess_json, dict):
            sess_json = {}

        cookie_st = self._extract_session_cookie()
        json_st = (
            sess_json.get("sessionToken", "")
            or sess_json.get("session_token", "")
            or ""
        )
        session_token = cookie_st or json_st
        access_token = sess_json.get("accessToken", "") or sess_json.get("access_token", "") or ""

        # 保存 session 接口的完整响应 JSON 原文（紧凑格式，一行）。
        #
        # ⚠️ 只在有内容时保存 —— 空 dict 存 "{}" 没意义，还会污染导出文件。
        # ⚠️ 这里含 accessToken / sessionToken —— 凭证全集，别泄露。
        if sess_json:
            try:
                self.result.session_json = json.dumps(
                    sess_json, ensure_ascii=False, separators=(",", ":")
                )
            except Exception:  # noqa: BLE001
                # 序列化失败不能拖垮注册（token 已经拿到了）
                pass

        if session_token:
            self.result.session_token = session_token
        if access_token:
            self.result.access_token = access_token
        self.result.cookie_header = self._build_chatgpt_cookie_header()

        _log = logger.info if first_call else logger.debug
        _log(f"session: st={'有' if session_token else '无'} at={'有' if access_token else '无'}")
        return session_token, access_token

    def _consume_callback_for_session(self, callback_url: str) -> bool:
        """主动 GET callback URL 让 chatgpt.com NextAuth 设 session cookie。

        协议层 follow_redirect_chain 故意不消费 callback（为后续 OAuth token exchange 留 code），
        但这导致 NextAuth 永远不会写 __Secure-next-auth.session-token cookie。
        在拿不到 session_token 时主动消费一次 callback：跟随到 chatgpt.com 主页，
        服务器会 Set-Cookie session-token。
        """
        if not callback_url or "code=" not in callback_url:
            return False
        try:
            current = callback_url
            for hop in range(8):
                resp = self.session.get(
                    current,
                    headers={
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Referer": "https://auth.openai.com/",
                        "User-Agent": self._ua,
                    },
                    timeout=30,
                    allow_redirects=False,
                )
                self._trace_http(f"consume_callback_hop_{hop+1}", resp)
                if resp.status_code not in (301, 302, 303, 307, 308):
                    break
                loc = (resp.headers.get("Location", "") or "").strip()
                if not loc:
                    break
                if loc.startswith("/"):
                    loc = urljoin(current, loc)
                current = loc
                # 已到 chatgpt.com 主页就够
                parsed = urlparse(current)
                if "chatgpt.com" in (parsed.netloc or "") and "/api/auth/callback" not in current:
                    # 再 GET 一下主页，让 cookie 全部落地
                    try:
                        self.session.get(current, timeout=20, allow_redirects=True)
                    except Exception:
                        pass
                    break
            token = self.session.cookies.get("__Secure-next-auth.session-token", "")
            if not token:
                # 排障用：只打 host/path 和状态码，不打 query（里面有 code）
                final = urlparse(current)
                logger.warning(
                    "消费 callback 未拿到 session-token: final=%s%s cookies=%s",
                    final.netloc,
                    final.path,
                    sorted({c.split("=")[0] for c in str(self.session.cookies).split(";") if "=" in c})[:8],
                )
            return bool(token)
        except Exception as e:
            logger.warning(f"消费 callback 失败: {e}")
            return False

    def oauth_token_exchange(self, callback_url: str, continue_url: str) -> bool:
        """
        交换 OAuth token（尽力模式）：
        1) 尝试多来源 code_verifier（query/cookie/dump/hydra）
        2) 回退无 verifier
        """
        auth_code = self._extract_query_first(callback_url, ["code"]) or self._extract_query_first(continue_url, ["code"])

        if not auth_code:
            logger.info("缺少 auth_code，跳过 token 交换")
            return False

        verifier_candidates = self._collect_code_verifier_candidates(callback_url, continue_url)
        if not verifier_candidates:
            logger.info("当前未获取到可用 code_verifier，将先尝试无 verifier 交换")
        else:
            show = ", ".join([f"{src}:{len(v)}" for src, v in verifier_candidates[:8]])
            logger.info("code_verifier 候选数=%s 示例=%s", len(verifier_candidates), show)

        logger.info("执行 OAuth Token 交换...")
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "Origin": "https://auth.openai.com",
            "Referer": "https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
        }
        base_form = {
            "grant_type": "authorization_code",
            "client_id": self._oauth_client_id or "YOUR_OPENAI_WEB_CLIENT_ID",
            "code": auth_code,
            "redirect_uri": self._oauth_redirect_uri or "https://chatgpt.com/api/auth/callback/openai",
        }
        logger.info(
            "Token 交换参数: client_id=%s redirect_uri=%s",
            base_form["client_id"],
            base_form["redirect_uri"],
        )

        candidates: list[tuple[str, dict]] = []
        if self._oauth_client_secret:
            d = dict(base_form)
            d["client_secret"] = self._oauth_client_secret
            candidates.append(("with_client_secret", d))

        try:
            max_verifier_try = max(1, int(os.getenv("OAUTH_MAX_VERIFIER_TRY", "18")))
        except Exception:
            max_verifier_try = 18

        for src, verifier in verifier_candidates[:max_verifier_try]:
            d = dict(base_form)
            d["code_verifier"] = verifier
            candidates.append((f"with_verifier_{src}", d))
            if self._oauth_client_secret:
                d2 = dict(d)
                d2["client_secret"] = self._oauth_client_secret
                candidates.append((f"with_verifier_{src}_and_client_secret", d2))

        # 一些服务端可能要求额外参数（实验候选）
        audience = self._extract_query_first(self._oauth_auth_url, ["audience"])
        if audience:
            d = dict(base_form)
            d["audience"] = audience
            candidates.append(("without_verifier_with_audience", d))
        if self._oauth_scope:
            d = dict(base_form)
            d["scope"] = self._oauth_scope
            candidates.append(("without_verifier_with_scope", d))

        candidates.append(("without_verifier", dict(base_form)))

        seen_fingerprints: set[str] = set()
        for mode, form in candidates:
            fp = json.dumps(form, sort_keys=True, ensure_ascii=False)
            if fp in seen_fingerprints:
                continue
            seen_fingerprints.add(fp)
            try:
                self._sniff_login_verifier(urlencode(form), f"oauth_token_exchange_{mode}:form")
            except Exception:
                pass
            encoded_form = urlencode(form)
            extra_request = {
                "method": "POST",
                "url": "https://auth.openai.com/oauth/token",
                "body": encoded_form,
                "headers": headers,
            }

            resp = self.session.post(
                "https://auth.openai.com/oauth/token",
                headers=headers,
                data=encoded_form,
                timeout=30,
            )
            self._trace_http(f"oauth_token_exchange_{mode}", resp, extra_request=extra_request)
            if resp.status_code == 200:
                data = resp.json()
                self.result.id_token = data.get("id_token", "")
                self.result.access_token = data.get("access_token", self.result.access_token)
                self.result.refresh_token = data.get("refresh_token", "")
                logger.info(
                    "Token 交换成功(mode=%s): refresh_token=%s",
                    mode,
                    "有" if self.result.refresh_token else "无",
                )
                return True

            body = (resp.text or "")[:240]
            logger.warning("Token 交换失败(mode=%s): status=%s body=%s", mode, resp.status_code, body)

        return False

    def oauth_secondary_authorize_exchange(self) -> bool:
        """
        二次授权实验：
        - 在当前已登录会话上，重新发起一条带 PKCE 的 authorize
        - 仅提取 callback code，不消费 callback
        - 再走 oauth/token 交换
        """
        logger.info("尝试二次 authorize + PKCE 换 refresh_token ...")
        try:
            csrf = self.get_csrf_token()
            auth_url = self.get_auth_url(csrf)
        except Exception as e:
            logger.warning(f"二次 authorize 初始化失败: {e}")
            return False

        try:
            verifier, challenge = self._build_pkce_pair()
            parsed = urlparse(auth_url)
            params = dict(parse_qsl(parsed.query, keep_blank_values=True))
            params["code_challenge"] = challenge
            params["code_challenge_method"] = "S256"
            if not params.get("state"):
                params["state"] = self._b64url_no_pad(os.urandom(16))
            sec_url = urlunparse(parsed._replace(query=urlencode(params)))

            self._manual_login_verifier = verifier
            self._captured_login_verifier = verifier
            self._remember_oauth_params(sec_url)

            current = sec_url
            callback_url = ""
            max_hops = 10
            for i in range(max_hops):
                resp = self.session.get(
                    current,
                    headers={
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Referer": "https://chatgpt.com/",
                        "User-Agent": self._ua,
                    },
                    timeout=30,
                    allow_redirects=False,
                )
                self._trace_http(f"secondary_authorize_hop_{i+1}", resp)

                loc = (resp.headers.get("Location", "") or "").strip()
                if loc and loc.startswith("/"):
                    loc = urljoin(current, loc)

                if loc and "/api/auth/callback/openai" in loc and "code=" in loc:
                    callback_url = loc
                    break
                if resp.status_code not in (301, 302, 303, 307, 308) or not loc:
                    break
                current = loc

            if not callback_url:
                logger.warning("二次 authorize 未捕获 callback code")
                return False

            ok = self.oauth_token_exchange(callback_url, callback_url)
            logger.info("二次 authorize 交换结果: %s", "成功" if ok else "失败")
            return ok
        except Exception as e:
            logger.warning(f"二次 authorize 交换异常: {e}")
            return False
