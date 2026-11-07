from __future__ import annotations

import base64
import json
import logging
import os
import random
import re
import time
import uuid
from urllib.parse import urlencode, urljoin, urlparse

from regix.fingerprint.generator import generate_fingerprint
from regix.http.client import create_http_session

logger = logging.getLogger(__name__)


class ProtocolMixin:
    """Protocol methods for this domain."""

    def warmup(self) -> bool:
        """GET chatgpt.com 种全套 cookie（含 oai-did），成功返回 True。

        为什么这步不能失败（2026-08-10 实测 26 轮，跨 40+ 出口 IP）：
        `POST /api/auth/signin/openai` 依据 chatgpt.com 的 cookie 决定返回什么——
        有 oai-did 就返 auth.openai.com/authorize URL，没有就返 NextAuth 页，
        后者到 authorize/continue 必然 409 invalid_state。
        实测：无 oai-did 的 5 轮 **5/5 全 409**；有 oai-did 的 17 轮只有 3 次 409。

        旧实现两个问题，实测各占一半失败：
        1. 单次无重试 + timeout=15。实测种 cookie 失败率 19%，形态有三种：
           TLS curl(35) 断连、15s 超时、CF 403。成功轮实际耗时 3.4~10.9s，
           15s 卡边缘，40s 才有富余。
        2. **返回值和实际结果对不上**：只 catch 异常，不看 status_code——
           403 照样 return True（实测 3 轮 True 但没 cookie），
           而超时前 cookie 其实已经种上了却 return False（实测 1 轮）。
           所以判据改成直接查 cookie jar，这是唯一可信的信号。

        3. **没发 client hints，自称 Chrome 却不带 sec-ch-ua —— CF 一眼假。**
           这是 403 的真因。真 Chrome 每个导航请求必带 sec-ch-ua/-mobile/-platform，
           而旧 warmup 是手搓 headers、一个都没带（_common_headers 带了，只有这里漏）。
           2026-08-10 实测，同一 impersonate 各打 5 次（每次新 IP）：

               impersonate   裸头(旧)   补 CH 全套
               chrome146      1/5        5/5
               chrome136      1/5        5/5
               chrome142      4/5        4/5   ← 唯一失败是 SSL 断连，不是 403

           补齐后 403 **全部消失**。此前"chrome 族被 CF 拦"的结论是误判：
           safari/firefox 当时 4/4 不是因为它们更干净，而是**它们本来就不该发
           client hints**，裸头对它们恰好是正确的头。所以修法是把头补齐，
           不是换成 safari —— 换指纹只是绕开症状，且会让 self._fingerprint 与
           self._ua 不一致（后续 _common_headers 会拿旧家族的 CH 配新 UA，更假）。

        重试只换出口 IP，不换指纹（指纹本来就没问题，见上）：代理池按会话分配
        出口，新 session ≈ 新 IP，绕开连不上的坏 IP。cookie 跟着 session 一起
        清掉是对的：失败轮本来就没种到有用的东西。

        注：URL 保持首页 `/`。实测对比过 `/auth/login`（16 轮 vs 10 轮），
        失败率 18.75% vs 20%，无差异，不值得换。

        【最终验证 2026-08-10】本处 + auth_oauth_init + _follow_redirects 三处
        统一走 _navigation_headers 后，用真实 CF 域名跑完整 run_register
        **3/3 全成功**（各约 100s，password + access_token 齐全），409 = 0。
        """
        headers = self._navigation_headers()

        for attempt in range(4):
            if attempt:
                # 只换出口 IP（新 session = 新出口），指纹保持不变：
                # 403 是缺 client hints 导致的，已在头里修好，不是指纹的锅。
                time.sleep(3 + attempt * 2)
                self.session = create_http_session(
                    proxy=self.config.proxy,
                    impersonate=self._impersonate_candidates[self._impersonate_idx],
                    user_agent=self._ua,
                )
            try:
                resp = self.session.get(
                    "https://chatgpt.com", headers=headers, timeout=40,
                )
                status = resp.status_code
            except Exception as e:
                status = None
                logger.warning(f"warmup 第 {attempt + 1}/4 次请求失败: {e}")

            # 唯一判据：cookie 到底种上没有。HTTP 200 不代表拿到 oai-did（CF 403 只给
            # __cf_bm），请求抛异常也不代表没拿到（超时前可能已经种上了）。
            try:
                cookies = self.session.cookies.get_dict()
            except Exception:
                cookies = {}
            if "oai-did" in cookies:
                logger.info(
                    f"chatgpt.com warmup 完成（第 {attempt + 1} 次，oai-did 已种，"
                    f"共 {len(cookies)} 个 cookie）"
                )
                return True

            logger.warning(
                f"warmup 第 {attempt + 1}/4 次未种到 oai-did"
                + (f"（HTTP {status}）" if status is not None else "")
                + (f"，已有 cookie: {sorted(cookies)}" if cookies else "，无任何 cookie")
            )

        logger.error("warmup 4 次均未种到 oai-did cookie —— 此时继续走注册链必然 409 invalid_state")
        return False

    def check_proxy(self) -> bool:
        logger.info("检查网络连通性...")
        try:
            resp = self.session.get("https://cloudflare.com/cdn-cgi/trace", timeout=15)
            if resp.status_code == 200:
                loc = re.search(r"loc=(\w+)", resp.text)
                ip = re.search(r"ip=([^\n]+)", resp.text)
                country_code = loc.group(1) if loc else ""
                logger.info(f"网络正常 - IP: {ip.group(1) if ip else 'N/A'}, "
                            f"地区: {country_code or 'N/A'}")

                # IP 地理联动：检测到国家码后，重新生成指纹（带时区/语言联动）
                if country_code and country_code != self._country_code:
                    self._country_code = country_code
                    import random
                    session_seed = id(self.session) % (2**32)
                    rng = random.Random(session_seed)
                    self._fingerprint = generate_fingerprint(rng=rng, country_code=country_code)
                    self._ua = self._fingerprint["user_agent"]
                    new_imp = self._fingerprint["impersonate"]
                    self._impersonate_candidates = self._fingerprint.get(
                        "fallback_impersonates",
                        [new_imp],
                    )
                    self._impersonate_idx = 0
                    self.session = create_http_session(
                        proxy=self.config.proxy,
                        impersonate=new_imp,
                        user_agent=self._ua,
                    )
                    logger.info(
                        f"指纹(IP联动): type={self._fingerprint['browser_type']} "
                        f"impersonate={new_imp} "
                        f"lang={self._fingerprint['lang']} "
                        f"tz={self._fingerprint.get('timezone', '')} "
                        f"ua={self._ua[:80]}"
                    )
            else:
                logger.warning(f"网络探测异常: cloudflare trace {resp.status_code}")

            return True
        except Exception as e:
            logger.error(f"网络检查失败: {e}")
        return False

    def get_csrf_token(self) -> str:
        logger.info("[1/10] 获取 CSRF Token...")
        headers = self._common_headers("https://chatgpt.com/auth/login")

        # Cloudflare 可能在短时间内多次请求后返回 403，重试 3 次
        for attempt in range(3):
            try:
                resp = self.session.get(
                    "https://chatgpt.com/api/auth/csrf",
                    headers=headers,
                    timeout=30,
                )
            except Exception as e:
                if self._is_tls_error(e) and self._rotate_impersonate_session():
                    continue
                if self._is_tls_error(e):
                    raise RuntimeError(
                        "chatgpt.com TLS 握手失败，当前网络无法建立到 /api/auth/csrf 的 HTTPS 连接。"
                        "请切换可直连 chatgpt.com 的网络或在界面中配置可用代理后重试。"
                    ) from e
                raise
            if resp.status_code == 403 and attempt < 2:
                wait = (attempt + 1) * 5
                logger.warning(f"Cloudflare 403, {wait}s 后重试 ({attempt + 1}/3)...")
                import time
                time.sleep(wait)
                continue
            resp.raise_for_status()
            break

        self._trace_http("chatgpt_csrf", resp)
        csrf = resp.json().get("csrfToken", "")
        if not csrf:
            raise RuntimeError("CSRF Token 获取失败")
        self.result.csrf_token = csrf
        logger.debug(f"CSRF Token: {csrf[:20]}...")
        return csrf

    def get_auth_url(self, csrf_token: str, email: str = "") -> str:
        logger.info("[2/10] 获取 OpenAI 授权地址...")
        headers = self._common_headers("https://chatgpt.com/auth/login")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        if not self.result.device_id:
            self.result.device_id = str(uuid.uuid4())
        query_params: dict[str, str] = {
            "prompt": "login",
            "screen_hint": "login_or_signup",
            "ext-oai-did": self.result.device_id,
            "auth_session_logging_id": str(uuid.uuid4()),
            "ext-passkey-client-capabilities": "1111",
        }
        if email:
            query_params["login_hint"] = email
        signin_url = f"https://chatgpt.com/api/auth/signin/openai?{urlencode(query_params)}"
        resp = self.session.post(
            signin_url,
            headers=headers,
            data={
                "csrfToken": csrf_token,
                "callbackUrl": "https://chatgpt.com/",
                "json": "true",
            },
            timeout=30,
        )
        resp.raise_for_status()
        self._trace_http("chatgpt_signin_openai", resp)
        auth_url = resp.json().get("url", "")
        if not auth_url:
            raise RuntimeError("Auth URL 获取失败")
        # 记住 OAuth 参数，并根据开关可选注入 PKCE
        self._remember_oauth_params(auth_url)
        auth_url = self._inject_pkce_into_auth_url(auth_url)
        self._remember_oauth_params(auth_url)
        logger.debug(f"Auth URL: {auth_url[:80]}...")
        return auth_url

    def auth_oauth_init(self, auth_url: str) -> str:
        """跟随 authorize 链，落 authorize 会话状态并取回 oai-did。

        这一步**建立的就是后面 authorize/continue 要用的那个 state**，头不像真
        浏览器就拿不到有效状态，下一步必 409 invalid_state。

        旧实现只发 Accept/Referer/UA，缺 client hints、**整组 Sec-Fetch-* 也没有**
        （真浏览器跳转必带 document/navigate/cross-site）。2026-08-10 实测 A/B
        对照各 6 轮（400 invalid_username 视为会话正常，只是 .test 域名被拒）：

            A 现状裸头        会话正常 2/6，**409 = 3**
            B 补齐 CH+SecFetch 会话正常 5/6，**409 = 0**

        和 warmup 那处是同一个病（详见 warmup docstring），当时只修了 warmup，
        漏了这里，所以主人实跑仍 409。头统一从 _navigation_headers 派生，
        保证 client hints 与 self._fingerprint / self._ua 同族。
        """
        logger.info("[3/10] OAuth 初始化...")

        def _nav_headers() -> dict:
            h = self._navigation_headers()
            h["Referer"] = "https://chatgpt.com/"
            # chatgpt.com -> auth.openai.com 是跨站跳转，不是首次直达
            h["sec-fetch-site"] = "cross-site"
            # 302 自动跟随不是用户手动点击，真浏览器此时不发 sec-fetch-user
            h.pop("sec-fetch-user", None)
            return h

        headers = _nav_headers()
        resp = self.session.get(auth_url, headers=headers, timeout=30, allow_redirects=True)
        self._trace_http("auth_oauth_init", resp)

        # 🔴 Cloudflare 403 在这里是**指纹级**的，不是 IP 级（2026-11-07 实测）：
        #    同一出口 IP、同一 URL、只换 TLS 指纹 →
        #        safari260 = 403，chrome110/136/146/150、safari18_0、firefox144 = 200
        #    撞上被拦的指纹时，authorize/continue 必然 409 invalid_state，
        #    而下面那次「重试」换的只是 device_id，**指纹没换** → 3 次撞同一堵墙。
        #    这里就地换同族回退指纹重试：不换邮箱、不换 device_id、不换出口 IP。
        for _ in range(2):
            if resp.status_code != 403:
                break
            if not self._rotate_impersonate_session():
                break
            headers = _nav_headers()
            resp = self.session.get(auth_url, headers=headers, timeout=30, allow_redirects=True)
            self._trace_http("auth_oauth_init", resp)

        final_url = str(resp.url)
        self._oauth_init_final_url = final_url
        logger.info(
            "[3/10] OAuth 初始化完成 status=%s final=%s",
            resp.status_code,
            urlparse(final_url)._replace(query="").geturl(),
        )

        # 从 cookie 获取 oai-did
        device_id = ""
        for cookie in self.session.cookies:
            if hasattr(cookie, "name"):
                if cookie.name == "oai-did":
                    device_id = cookie.value
                    break
            elif isinstance(cookie, str) and cookie == "oai-did":
                device_id = self.session.cookies.get("oai-did", "")
                break

        # curl_cffi cookies 访问方式
        if not device_id:
            try:
                device_id = self.session.cookies.get("oai-did", "")
            except Exception:
                pass

        # fallback: 从 HTML 提取
        if not device_id:
            m = re.search(r'oai-did["\s:=]+([a-f0-9-]{36})', resp.text)
            if m:
                device_id = m.group(1)

        if not device_id:
            device_id = str(uuid.uuid4())
            logger.warning(f"未从响应中获取 device_id，使用生成值: {device_id}")

        self.result.device_id = device_id
        logger.debug(f"Device ID: {device_id}")
        return device_id

    def _sentinel_fp_kwargs(self) -> dict:
        """从 self._fingerprint 抽出 sentinel 需要的指纹/硬件字段。

        保证 4 处 sentinel 调用（authorize_continue / username_password_create /
        create_account 等）用的是同一套一致画像——UA↔platform↔vendor↔硬件全程不变。
        """
        fp = self._fingerprint or {}
        return {
            "user_agent": self._ua,
            "sec_ch_ua": fp.get("sec_ch_ua", ""),
            "sec_ch_ua_platform": fp.get("sec_ch_ua_platform", ""),
            "sec_ch_ua_mobile": fp.get("sec_ch_ua_mobile", ""),
            # Client Hints 全套（仅 Chromium 有值）
            "sec_ch_ua_full_version_list": fp.get("sec_ch_ua_full_version_list", ""),
            "sec_ch_ua_arch": fp.get("sec_ch_ua_arch", ""),
            "sec_ch_ua_bitness": fp.get("sec_ch_ua_bitness", ""),
            "sec_ch_ua_model": fp.get("sec_ch_ua_model", ""),
            "sec_ch_ua_platform_version": fp.get("sec_ch_ua_platform_version", ""),
            "screen": fp.get("screen", ""),
            "lang": fp.get("lang", ""),
            "lang_full": fp.get("lang_full", ""),
            "browser_type": fp.get("browser_type", ""),
            "navigator_platform": fp.get("navigator_platform", ""),
            "navigator_vendor": fp.get("navigator_vendor"),
            "hardware_concurrency": fp.get("hardware_concurrency", 0),
            "device_memory": fp.get("device_memory"),
            "max_touch_points": fp.get("max_touch_points", 0),
            "device_pixel_ratio": fp.get("device_pixel_ratio", 0.0),
            "timezone": fp.get("timezone", ""),  # IP 联动时区
        }

    def get_sentinel_token(self, device_id: str) -> str:
        logger.info("[4/10] 获取 Sentinel Token (PoW)...")
        from regix.openai.sentinel import get_sentinel_token
        result = get_sentinel_token(
            self.session,
            device_id=device_id,
            flow="authorize_continue",
            **self._sentinel_fp_kwargs(),
        )
        token, so_token = result
        self._last_sentinel_token = token or ""
        self._last_sentinel_so_token = so_token or ""
        logger.debug("Sentinel Token 获取成功")
        return token

    def authorize_continue(
        self,
        email: str,
        sentinel_token: str,
        screen_hint: str = "signup",
        referer: str = "https://auth.openai.com/create-account",
        trace_step: str = "",
    ) -> dict:
        """调用 /api/accounts/authorize/continue，返回 JSON。"""
        headers = self._common_headers(referer)
        headers["Content-Type"] = "application/json"
        if sentinel_token:
            headers["openai-sentinel-token"] = sentinel_token
        if getattr(self, "_last_sentinel_so_token", ""):
            headers["openai-sentinel-so-token"] = self._last_sentinel_so_token
        payload = {
            "username": {"value": email, "kind": "email"},
            "screen_hint": screen_hint,
        }
        resp = self.session.post(
            "https://auth.openai.com/api/accounts/authorize/continue",
            headers=headers,
            json=payload,
            timeout=30,
        )
        self._trace_http(trace_step or f"authorize_continue_{screen_hint}", resp)
        if resp.status_code != 200:
            body = (resp.text or "")[:360]
            # 额外打日志：headers/req_id 帮排查是不是 IP 风控
            req_id = (resp.headers.get("x-request-id", "") or "")[:80]
            ct = (resp.headers.get("Content-Type", "") or "")[:60]
            logger.error(
                "authorize/continue 非 200: status=%s screen_hint=%s req_id=%s content_type=%s body=%r",
                resp.status_code, screen_hint, req_id, ct, body,
            )
            raise RuntimeError(
                f"authorize/continue 失败(screen_hint={screen_hint}): "
                f"HTTP {resp.status_code} req_id={req_id} body={body}"
            )
        try:
            return resp.json() if resp is not None else {}
        except Exception:
            return {}

    def signup(self, email: str, sentinel_token: str) -> bool:
        """提交注册邮箱。返回 True 表示走新注册流程，False 表示已有账号走 OTP 登录流程"""
        logger.info("[5/10] 提交注册邮箱...")
        data = self.authorize_continue(
            email=email,
            sentinel_token=sentinel_token,
            screen_hint="signup",
            referer="https://auth.openai.com/create-account",
            trace_step="authorize_continue_signup",
        )

        # 检测 page_type/continue_url，区分新账号与已有账号
        try:
            page = (data.get("page") or {}) if isinstance(data, dict) else {}
            page_type = (page.get("type") or "").strip()
            payload = (page.get("payload") or {}) if isinstance(page, dict) else {}
            continue_url = (data.get("continue_url") or "").strip()

            # 新账号标准分支
            if page_type == "create_account_password" or "/create-account/password" in continue_url:
                self._is_existing_account = False
                self._existing_email_verification_mode = ""
                self._existing_page_type = page_type
                logger.info("注册邮箱已提交")
                return True

            # OTP 验证分支（passwordless 新注册 或 已有账号登录）
            if page_type == "email_otp_verification":
                mode = (payload.get("email_verification_mode", "") or "").strip()
                self._existing_email_verification_mode = mode
                self._existing_page_type = page_type
                if mode == "passwordless_signup":
                    logger.info("服务端选择 passwordless 注册流程（新账号，无密码），等待 OTP")
                    self._is_existing_account = False
                else:
                    logger.info("检测到已有账号，切换到 OTP 登录流程")
                    self._is_existing_account = True
                return False

            # 未知 page_type：通常是社交登录/风控分支，按已有账号处理，避免误进 register_password 导致 invalid_state
            self._existing_email_verification_mode = (payload.get("email_verification_mode", "") or "").strip()
            self._existing_page_type = page_type
            self._is_existing_account = True
            logger.warning(
                "authorize/continue 返回非标准注册页面: page_type=%s continue_url=%s，按已有账号流程处理",
                page_type or "(empty)",
                continue_url[:180] or "(empty)",
            )
            return False
        except Exception:
            # JSON 解析失败时保守按新注册处理
            self._is_existing_account = False
            self._existing_email_verification_mode = ""
            self._existing_page_type = ""
            logger.info("注册邮箱已提交")
            return True

    def register_password(self, email: str) -> bool:
        logger.info("[5.5/10] 注册密码...")
        password = self._random_password()
        self.result.password = password

        # 先访问 create-account/password 页面（HAR 确认需要此步建立服务端状态）
        try:
            pw_page = self.session.get(
                "https://auth.openai.com/create-account/password",
                headers=self._common_headers("https://auth.openai.com/create-account"),
                timeout=15,
            )
            logger.info(f"create-account/password 页面: {pw_page.status_code}")
        except Exception as e:
            logger.warning(f"访问 create-account/password 页面失败: {e}")

        # 注册前需要刷新 sentinel token，且 flow 必须为 username_password_create
        #
        # ⚠️ SO token 只在**本次请求**范围内决定带不带，绝不回写实例上的
        #    _last_sentinel_so_token。原因：send_otp / verify_otp 这些后续步骤
        #    自己不刷 sentinel，直接复用实例字段。本 flow 服务端不要求 SO token
        #    （so_token 为空），要是把空值写回实例，等于顺手把后续所有请求的
        #    SO 头也一起摘掉了 —— 那几步的 flow 服务端是要 SO 的。
        so_token_for_request = getattr(self, "_last_sentinel_so_token", "")
        if self.result.device_id:
            try:
                from regix.openai.sentinel import get_sentinel_token as _get_st
                token, so_token = _get_st(self.session, device_id=self.result.device_id,
                                flow="username_password_create",
                                **self._sentinel_fp_kwargs())
                self._last_sentinel_token = token or ""
                so_token_for_request = so_token or ""
                if so_token:
                    self._last_sentinel_so_token = so_token
                logger.debug("Sentinel Token 获取成功")
            except Exception as e:
                # 注：username_password_create 这个 flow 服务端**不下发 so 块**
                #    （实测 2026-08-06，见 sentinel_quickjs.py 里的说明），
                #    所以 so_token 为空是正常的，已在 sentinel_quickjs 按服务端要求判定，
                #    不再走到这个 except。这里只兜网络/子进程一类的真异常。
                #    走到这里说明用的是上一步的 token，flow 对不上是风控特征，
                #    但比当场崩掉（POST 根本发不出去）强，故降级继续。
                logger.warning(
                    f"注册前刷新 sentinel 失败，将改用 flow 不匹配的现有 sentinel token 提交: {e}"
                )

        headers = self._common_headers("https://auth.openai.com/create-account/password")
        headers["Content-Type"] = "application/json"
        if self._last_sentinel_token:
            headers["openai-sentinel-token"] = self._last_sentinel_token
        # 服务端对该 flow 没下发 so 块时 so_token_for_request 为空 → 不带这个头，
        # 与真实浏览器一致；不要退回实例字段拿别的 flow 的 SO token 来凑。
        if so_token_for_request:
            headers["openai-sentinel-so-token"] = so_token_for_request
        resp = self.session.post(
            "https://auth.openai.com/api/accounts/user/register",
            headers=headers,
            json={"password": password, "username": email},
            timeout=30,
        )
        self._trace_http("register_password", resp)
        if resp.status_code != 200:
            logger.warning(f"密码注册返回 {resp.status_code}: {resp.text[:200]}")
            return False
        logger.info("密码注册成功")
        # ⚠️ 走到这里 = OpenAI 侧账号连同这个密码**已经建好了**，但注册流程后面还有
        #    发码 → 验证 OTP → create_account 三步，任何一步挂掉都到不了 save_registered。
        #    密码是这个方法现生成的、只活在内存里，进程一退就永久没了 ——
        #    号还在 OpenAI 那边好好的，却谁也登不进去（实测 2026-08-07 被 OTP 超时坑过一次）。
        #    所以在这里立刻回调落盘。
        #    位置刻意选在 POST 200 **之后**而不是生成密码时：POST 失败的密码
        #    OpenAI 侧根本没生效，写进库里反而误导人以为能用。
        if self._on_password is not None:
            try:
                self._on_password(email, password)
            except Exception as e:
                logger.warning(f"密码落盘回调失败（不影响注册，日志里还有兜底）: {e}")
        return True

    def send_otp(self, referer: str = "https://auth.openai.com/create-account/password"):
        logger.info(f"[6/10] 发送 OTP (referer={referer.split('/')[-1]})...")
        headers = self._common_headers(referer)
        if self._last_sentinel_token:
            headers["openai-sentinel-token"] = self._last_sentinel_token
        if getattr(self, "_last_sentinel_so_token", ""):
            headers["openai-sentinel-so-token"] = self._last_sentinel_so_token
        # zhuce6 用 GET /api/accounts/email-otp/send
        resp = self.session.get(
            "https://auth.openai.com/api/accounts/email-otp/send",
            headers=headers,
            timeout=30,
        )
        self._trace_http("send_email_otp", resp)
        if resp.status_code != 200:
            raise RuntimeError(f"发送 OTP 失败: {resp.status_code} - {resp.text[:200]}")
        logger.info("OTP 已发送到邮箱")

    def send_passwordless_otp(self, referer: str = "https://auth.openai.com/create-account/password") -> bool:
        """
        走 passwordless 发码（create-account/password 页面可触发该路径）。
        """
        headers = self._common_headers(referer)
        headers["Content-Type"] = "application/json"
        if self._last_sentinel_token:
            headers["openai-sentinel-token"] = self._last_sentinel_token
        if getattr(self, "_last_sentinel_so_token", ""):
            headers["openai-sentinel-so-token"] = self._last_sentinel_so_token
        resp = self.session.post(
            "https://auth.openai.com/api/accounts/passwordless/send-otp",
            headers=headers,
            timeout=30,
        )
        self._trace_http("send_passwordless_otp", resp)
        if resp.status_code == 200:
            logger.info("passwordless OTP 已发送")
            return True
        logger.warning(f"passwordless 发码失败: {resp.status_code} - {(resp.text or '')[:220]}")
        return False

    def resend_otp(self, referer: str = "https://auth.openai.com/email-verification") -> bool:
        """
        重发 OTP（适用于已有账号 passwordless/login_challenge）。
        返回 True 代表请求成功。
        """
        headers = self._common_headers(referer)
        headers["Content-Type"] = "application/json"
        if self._last_sentinel_token:
            headers["openai-sentinel-token"] = self._last_sentinel_token
        if getattr(self, "_last_sentinel_so_token", ""):
            headers["openai-sentinel-so-token"] = self._last_sentinel_so_token
        resp = self.session.post(
            "https://auth.openai.com/api/accounts/email-otp/resend",
            headers=headers,
            timeout=30,
        )
        self._trace_http("resend_email_otp", resp)
        if resp.status_code == 200:
            logger.info("OTP 已重发")
            return True
        logger.warning(f"重发 OTP 失败: {resp.status_code} - {(resp.text or '')[:200]}")
        return False

    def kickoff_otp_delivery(self, mode: str = "") -> bool:
        """
        统一发码策略, 根据 mode hint 区分"新注册" vs "已有账号" referer:

        - 新注册 (create-account/password 页面 state): passwordless/send-otp → email-otp/send
        - 已有账号 / passwordless_login / existing_*: send_otp(referer=email-verification) → resend_otp
          (绕开 passwordless/send-otp 在已有账号场景的 409 invalid_state)
        """
        mode_lc = (mode or "").strip().lower()
        is_existing = (
            "existing" in mode_lc
            or "passwordless_login" in mode_lc
            or "passwordless_signup" in mode_lc  # OpenAI 把 outlook 接码池都打这个 mode
            or self._is_existing_account
        )

        if is_existing:
            # 已有账号 passwordless_signup / passwordless_login: authorize/continue 已经在
            # OpenAI server 端 trigger 了发码 (state S, OTP X, 邮件 X 已在投递). 这里**只能 resend**
            # (复用同 challenge state, 复用同 OTP X 或派生新码但 state 不变). 不能调 send_otp,
            # 它会新建 challenge token 让 state 跳到 Y, 旧邮件 X 在 server 端立即失效 → IMAP 抓到 X
            # verify 时 wrong_email_otp_code.
            if self.resend_otp("https://auth.openai.com/email-verification"):
                return True
            # resend 失败兜底: send_otp 新建 challenge (旧 state 已坏, 不得不重启)
            logger.warning(f"已有账号 resend 失败, 兜底 send_otp 新建 challenge (邮件 X 将失效)")
            try:
                self.send_otp(referer="https://auth.openai.com/email-verification")
                return True
            except Exception as e:
                logger.warning(f"已有账号发码全 fail: {e}")
                return False

        # 新注册 (原顺序)
        if self.send_passwordless_otp("https://auth.openai.com/create-account/password"):
            return True
        if self.resend_otp("https://auth.openai.com/email-verification"):
            return True
        try:
            self.send_otp()
            return True
        except Exception as e:
            logger.warning(f"send_otp 兜底失败(mode={mode_lc or 'unknown'}): {e}")
            return False

    @staticmethod
    def _default_password_from_email(email: str) -> str:
        """⚠️ 这是**猜**出来的密码，不是这个号真的密码。

        只在实在拿不到真密码时用来碰一下运气（碰上早期用这个规则建的号）。
        调用方必须走 _resolve_login_password，别直接调这个 —— 见那边的注释。
        """
        pwd = (email or "").replace("@", "")
        if len(pwd) < 8:
            pwd = f"{pwd}2026OpenAI"
        return pwd

    def _resolve_login_password(self, email: str) -> tuple[str, bool]:
        """找出登录这个号该用的密码。返回 (密码, 是否为真密码)。

        真密码三个来源，按优先级：
            ① self.result.password —— 本轮 register_password 刚设的
            ② LOGIN_PASSWORD 环境变量 —— 主人手动指定
            ③ account_callback —— 数据库里存的（**重跑老号全靠这条**）
        三条都空才退到 _default_password_from_email 猜一个，此时第二个返回值 False。

        ⚠️ 猜出来的密码**绝不能**写回 self.result.password。那个字段有两个下游：
             · to_dict() → registrar 落库，会把假密码存成这个号的密码；
             · registrar 异常兜底那行「该号已生成密码，请自行留存」。
           实测：一个 passwordless 老号（从没设过密码）被打成「邮箱去掉 @」，
           照着存等于存了个死密码。所以赋值一律由调用方按 is_real 决定。
        """
        pwd = (self.result.password or "").strip()
        if pwd:
            return pwd, True
        pwd = (os.getenv("LOGIN_PASSWORD", "") or "").strip()
        if pwd:
            return pwd, True
        if self._account_callback:
            try:
                cred = self._account_callback(email) or {}
                pwd = (cred.get("password") or "").strip()
                if pwd:
                    logger.info("已从数据库加载密码")
                    return pwd, True
            except Exception as e:
                logger.warning(f"account_callback 加载密码异常: {e}")
        return self._default_password_from_email(email), False

    @staticmethod
    def _random_password(length: int = 16) -> str:
        import string
        upper = string.ascii_uppercase
        lower = string.ascii_lowercase
        digits = string.digits
        special = "!@#$%^&*"
        must = [
            random.choice(upper),
            random.choice(lower),
            random.choice(digits),
            random.choice(special),
        ]
        all_chars = upper + lower + digits + special
        rest = random.choices(all_chars, k=length - len(must))
        pwd_list = must + rest
        random.shuffle(pwd_list)
        return "".join(pwd_list)

    def login_password_verify(self, password: str) -> dict:
        """已有账号密码登录一步（/password/verify）。"""
        headers = self._common_headers("https://auth.openai.com/log-in/password")
        headers["Content-Type"] = "application/json"
        if self._last_sentinel_token:
            headers["openai-sentinel-token"] = self._last_sentinel_token
        if getattr(self, "_last_sentinel_so_token", ""):
            headers["openai-sentinel-so-token"] = self._last_sentinel_so_token
        resp = self.session.post(
            "https://auth.openai.com/api/accounts/password/verify",
            headers=headers,
            json={"password": password},
            timeout=30,
        )
        self._trace_http("login_password_verify", resp)
        if resp.status_code != 200:
            body = (resp.text or "")[:260]
            raise RuntimeError(f"密码登录失败: {resp.status_code} - {body}")
        try:
            return resp.json()
        except Exception:
            return {}

    def submit_mfa_totp(self, totp_code: str, challenge_id: str) -> dict:
        """提交 TOTP 2FA 验证码（已有账号登录时，密码验证后进入 mfa-challenge 状态）。

        Args:
            totp_code: 6 位 TOTP 动态码
            challenge_id: 从 continue_url 提取的 challenge ID（如 /mfa-challenge/6a76f2e8...）

        Returns:
            服务端响应 dict，包含 continue_url 指向 callback
        """
        headers = self._common_headers("https://auth.openai.com/mfa-challenge")
        headers["Content-Type"] = "application/json"
        if self._last_sentinel_token:
            headers["openai-sentinel-token"] = self._last_sentinel_token
        if getattr(self, "_last_sentinel_so_token", ""):
            headers["openai-sentinel-so-token"] = self._last_sentinel_so_token

        resp = self.session.post(
            "https://auth.openai.com/api/accounts/mfa/verify",
            headers=headers,
            json={"code": totp_code, "type": "totp", "id": challenge_id},
            timeout=30,
        )
        self._trace_http("submit_mfa_totp", resp)
        if resp.status_code != 200:
            body = (resp.text or "")[:260]
            raise RuntimeError(f"TOTP 验证失败: {resp.status_code} - {body}")
        try:
            return resp.json()
        except Exception:
            return {}

    def verify_otp(self, otp_code: str) -> dict:
        logger.info("[7/10] 验证 OTP...")
        headers = self._common_headers("https://auth.openai.com/email-verification")
        headers["Content-Type"] = "application/json"
        resp = self.session.post(
            "https://auth.openai.com/api/accounts/email-otp/validate",
            headers=headers,
            json={"code": otp_code},
            timeout=30,
        )
        self._trace_http("validate_email_otp", resp)
        if resp.status_code != 200:
            body = (resp.text or "")
            logger.warning(f"verify_otp FULL body ({resp.status_code}): {body[:2000]}")
            raise RuntimeError(f"OTP 验证失败: {resp.status_code} - {body[:260]}")
        logger.info("OTP 验证成功")
        try:
            return resp.json()
        except Exception:
            return {}

    def create_account(self) -> str:
        logger.info("[8/10] 创建账户...")
        # 创建账户前刷新 sentinel token，flow 为 create_account
        if self.result.device_id:
            try:
                from regix.openai.sentinel import get_sentinel_token as _get_st
                token, so_token = _get_st(self.session, device_id=self.result.device_id,
                                flow="oauth_create_account",
                                **self._sentinel_fp_kwargs())
                self._last_sentinel_token = token or ""
                self._last_sentinel_so_token = so_token or ""
                logger.debug("Sentinel Token 获取成功")
            except RuntimeError:
                raise
            except Exception as e:
                logger.warning(f"创建账户前刷新 sentinel 失败: {e}")
        headers = self._common_headers("https://auth.openai.com/about-you")
        headers["Content-Type"] = "application/json"
        if self._last_sentinel_token:
            headers["openai-sentinel-token"] = self._last_sentinel_token
        if getattr(self, "_last_sentinel_so_token", ""):
            headers["openai-sentinel-so-token"] = self._last_sentinel_so_token
        _FIRST = ["James", "John", "Robert", "Michael", "William", "David", "Richard",
                  "Joseph", "Thomas", "Charles", "Mary", "Patricia", "Jennifer", "Linda",
                  "Elizabeth", "Barbara", "Susan", "Jessica", "Sarah", "Karen"]
        _LAST = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
                 "Davis", "Rodriguez", "Martinez", "Wilson", "Anderson", "Taylor", "Thomas"]
        name = f"{random.choice(_FIRST)} {random.choice(_LAST)}"
        birthdate = f"{random.randint(1985, 2000)}-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}"
        resp = self.session.post(
            "https://auth.openai.com/api/accounts/create_account",
            headers=headers,
            json={"name": name, "birthdate": birthdate},
            timeout=30,
        )
        self._trace_http("create_account", resp)
        if resp.status_code != 200:
            body = (resp.text or "")[:500]
            logger.error("创建账户失败: http=%s body=%s", resp.status_code, body)
            raise RuntimeError(f"创建账户失败: {resp.status_code} - {body[:260]}")
        data = resp.json()
        continue_url = data.get("continue_url", "")
        self._sniff_login_verifier(continue_url, "create_account_continue_url")

        # 尝试 workspace select
        if not continue_url:
            workspace_id = self._extract_workspace_id()
            if workspace_id:
                continue_url = self._workspace_select(workspace_id)

        if not continue_url:
            raise RuntimeError("创建账户后未获取到 continue_url")

        logger.info("账户创建成功")
        return continue_url

    def _extract_workspace_id(self) -> str:
        """从 cookie 中提取 workspace_id"""
        try:
            auth_session = self.session.cookies.get("oai-client-auth-session", "")
            if auth_session:
                parts = auth_session.split(".")
                # 兼容不同 cookie 形态：workspace_id 可能在第 1 段/第 2 段，也可能在 workspaces[0].id
                for idx in range(min(2, len(parts))):
                    segment = (parts[idx] or "").strip()
                    if not segment:
                        continue
                    payload_b64 = segment + "=" * (-len(segment) % 4)
                    decoded = json.loads(base64.urlsafe_b64decode(payload_b64.encode("utf-8")).decode("utf-8"))
                    if not isinstance(decoded, dict):
                        continue
                    wid = (decoded.get("workspace_id", "") or "").strip()
                    if wid:
                        return wid
                    workspaces = decoded.get("workspaces", [])
                    if isinstance(workspaces, list):
                        for it in workspaces:
                            if isinstance(it, dict):
                                wid = (it.get("id", "") or "").strip()
                                if wid:
                                    return wid
        except Exception:
            pass
        return ""

    def _workspace_select(self, workspace_id: str) -> str:
        logger.info("执行 workspace 选择...")
        headers = self._common_headers("https://auth.openai.com/sign-in-with-chatgpt/codex/consent")
        headers["Content-Type"] = "application/json"
        resp = self.session.post(
            "https://auth.openai.com/api/accounts/workspace/select",
            headers=headers,
            json={"workspace_id": workspace_id},
            timeout=30,
        )
        self._trace_http("workspace_select", resp)
        return resp.json().get("continue_url", "") if resp.status_code == 200 else ""

    def _choose_account_select(self, html_text: str, current_url: str) -> str:
        """处理 /choose-an-account 多账号选择页（react-router SSR）。

        HTML 里 streamController.enqueue 注入 `unified_sessions[].id` (us_*) 和
        `session_id` (authsess_*)。这里 regex 抽 us_*，按 react-router action 惯例
        POST 回 /choose-an-account，并 fallback 试几个候选 JSON endpoint。
        返回 next continue_url 或空串。
        """
        m = re.search(r"us_[A-Za-z0-9]{16,}", html_text or "")
        if not m:
            logger.warning("/choose-an-account HTML 里没找到 us_* session id, 跳过")
            return ""
        session_id = m.group(0)
        logger.debug(f"/choose-an-account 选 session_id={session_id}")
        headers = self._common_headers("https://auth.openai.com/choose-an-account")
        headers["Origin"] = "https://auth.openai.com"

        # 真实 endpoint 从 nextStepHandler-*.js 反编译解出：
        #   const {path, method} = r.data.intent === "select"
        #     ? {path: "/session/select", method: "POST"}
        #     : {path: "/session/remove", method: "DELETE"};
        #   fetch(`${authapi_base}/session/select`, {method, body: JSON.stringify({session_id})})
        # 即 POST https://auth.openai.com/api/accounts/session/select JSON {session_id}
        # （intent 决定 path 不进 body；body 只有 session_id 一个字段）
        # 之前直接 POST /choose-an-account 会先经过 react-router action loader 再被
        # nextStepHandler 转发，但 server-side 那一段似乎对 CT/form 字段强敏感，500。
        # 直接命中底层 /api/accounts/session/select 绕开 react-router 层。
        candidates = [
            ("POST", "https://auth.openai.com/api/accounts/session/select",
             {"session_id": session_id}, "json"),
            # 兜底：万一上面被风控，回退到 react-router 路径 + zod schema 字段
            ("POST", "https://auth.openai.com/choose-an-account",
             {"intent": "select", "session_id": session_id}, "form"),
        ]
        for method, url, body, kind in candidates:
            try:
                h = dict(headers)
                if kind == "json":
                    h["Content-Type"] = "application/json"
                    h["Accept"] = "application/json"
                    resp = self.session.post(url, headers=h, json=body, timeout=30)
                else:
                    h["Content-Type"] = "application/x-www-form-urlencoded"
                    h["Accept"] = "application/json, text/html;q=0.9"
                    body_str = "&".join(f"{k}={v}" for k, v in body.items())
                    resp = self.session.post(url, headers=h, data=body_str, timeout=30)
                self._trace_http(f"choose_account_try_{kind}_{url.rsplit('/', 1)[-1][:30]}", resp)
                status = getattr(resp, "status_code", 0)
                snippet = (getattr(resp, "text", "") or "")[:240].replace("\n", " ")
                loc = (getattr(resp, "headers", {}) or {}).get("Location", "") or \
                      (getattr(resp, "headers", {}) or {}).get("location", "") or ""
                # print 到 stdout 让 webui SSE 能看到每个候选的具体结果
                print(
                    f"[choose-an-account] {method} {url} [{kind}] -> "
                    f"status={status} loc={loc[:120]} body={snippet}",
                    flush=True,
                )
                if status in (200, 201, 302, 303):
                    next_url = ""
                    try:
                        j = resp.json() if resp is not None else {}
                        next_url = j.get("continue_url", "") if isinstance(j, dict) else ""
                    except Exception:
                        pass
                    if not next_url and loc:
                        next_url = loc
                    if next_url:
                        logger.debug(f"choose-an-account 选号成功 endpoint={url} next={next_url[:120]}")
                        return next_url
                    # 200 但没 continue_url：可能 set 了 cookie，直接让 caller 重 GET authorize
                    if status == 200:
                        logger.debug(f"choose-an-account POST {url} 200 OK 无 continue_url，假定 cookie 已 set")
                        return current_url  # 让外层重 GET 一次，cookie 已被 server set
            except Exception as e:
                print(f"[choose-an-account] {method} {url} [{kind}] -> EXC {e}", flush=True)
                continue
        logger.warning("/choose-an-account 全部候选 endpoint 都失败")
        return ""

    def _normalize_continue_url(self, continue_url: str) -> str:
        """
        标准化 continue_url：
        1) 相对路径 -> 绝对路径
        2) workspace 页面 -> 调用 workspace/select 取下一跳
        """
        if not continue_url:
            return ""
        out = continue_url.strip()
        if out.startswith("/"):
            out = urljoin("https://auth.openai.com", out)
        if "/workspace" in out:
            workspace_id = self._extract_workspace_id() or self._extract_query_first(out, ["workspace_id", "id"])
            if workspace_id:
                logger.info("检测到 workspace 页面，尝试 workspace/select: workspace_id=%s", workspace_id)
                next_url = self._workspace_select(workspace_id)
                if next_url:
                    out = next_url
        return out

    @staticmethod
    def _extract_workspace_id_from_html(html_text: str) -> str:
        """从 workspace 页面 HTML 文本中提取 workspace_id（兜底）。"""
        if not html_text:
            return ""
        try:
            # 先把转义引号还原，便于正则匹配
            text = html_text.replace('\\"', '"')
            patterns = [
                r'workspaces".{0,1600}?"id","([0-9a-fA-F-]{36})"',
                r'"workspace_id"\s*:\s*"([0-9a-fA-F-]{36})"',
                r'"workspaceId"\s*:\s*"([0-9a-fA-F-]{36})"',
            ]
            for p in patterns:
                m = re.search(p, text, flags=re.DOTALL | re.IGNORECASE)
                if m:
                    return (m.group(1) or "").strip()
        except Exception:
            return ""
        return ""

    def follow_redirect_chain(self, start_url: str) -> tuple[str, str]:
        """手动跟踪重定向，返回 (callback_url, final_url)"""
        logger.info("[9/10] 跟踪重定向链...")
        current_url = start_url
        callback_url = ""
        max_hops = 12
        referer = "https://auth.openai.com/"

        for i in range(max_hops):
            # 逐跳整页导航，头必须像浏览器：同 auth_oauth_init，旧版只发
            # Accept/Referer/UA，缺 client hints 和 Sec-Fetch-*（实测那正是
            # 409 invalid_state 的来源，见 auth_oauth_init docstring）。
            headers = self._navigation_headers()
            headers["Referer"] = referer
            headers.pop("sec-fetch-user", None)   # 302 跟随非用户点击
            # 跨站跳转（chatgpt.com <-> auth.openai.com）标 cross-site，同站标 same-origin
            try:
                headers["sec-fetch-site"] = (
                    "same-origin"
                    if urlparse(current_url).netloc == urlparse(referer).netloc
                    else "cross-site"
                )
            except Exception:
                headers["sec-fetch-site"] = "cross-site"
            resp = self.session.get(
                current_url, headers=headers, timeout=30, allow_redirects=False
            )
            self._trace_http(f"redirect_hop_{i+1}", resp)
            referer = current_url

            if "/api/auth/callback/openai" in current_url:
                callback_url = current_url
                self._sniff_login_verifier(current_url, f"redirect_hop_{i+1}_callback_url")

            # workspace 页面常见为 200，需要主动调 workspace/select 获取下一跳
            if "/workspace" in current_url and resp.status_code == 200:
                workspace_id = self._extract_workspace_id() or self._extract_workspace_id_from_html(resp.text or "")
                if workspace_id:
                    logger.info("workspace 页面提取到 workspace_id=%s，尝试继续授权", workspace_id)
                    next_url = self._workspace_select(workspace_id)
                    if next_url:
                        if next_url.startswith("/"):
                            next_url = urljoin("https://auth.openai.com", next_url)
                        current_url = next_url
                        continue

            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location", "")
                if not location:
                    break
                if location.startswith("/"):
                    parsed = urlparse(current_url)
                    location = f"{parsed.scheme}://{parsed.netloc}{location}"
                # 关键：不要主动 GET callback，避免 code 被服务端回调消费
                if "/api/auth/callback/openai" in location and "code=" in location:
                    callback_url = location
                    current_url = location
                    self._sniff_login_verifier(location, f"redirect_hop_{i+1}_location_callback")
                    logger.info("捕获 callback URL（未消费）")
                    break
                current_url = location
                logger.debug(f"  重定向 {i + 1}: {current_url[:80]}...")
            else:
                break

        # 补一跳首页
        if (not callback_url) and (not current_url.rstrip("/").endswith("chatgpt.com")):
            self.session.get(
                "https://chatgpt.com/",
                headers={"Referer": current_url},
                timeout=30,
            )

        logger.info(f"重定向链完成, callback: {'有' if callback_url else '无'}")
        return callback_url, current_url
