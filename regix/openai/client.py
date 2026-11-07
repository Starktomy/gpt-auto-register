from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import random
import re
import secrets
import struct
import subprocess
import time
import uuid
from datetime import datetime
from typing import Any, Optional
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlparse, urlunparse

from regix.config import Config
from regix.fingerprint.generator import fingerprint_for_impersonate, generate_fingerprint, ua_for_impersonate
from regix.http.client import USER_AGENT, create_http_session
from regix.providers.mail import MailProvider

logger = logging.getLogger(__name__)


class ClientMixin:
    """Protocol methods for this domain."""

    def _build_chatgpt_cookie_header(self) -> str:
        """
        导出当前会话中的 chatgpt.com 相关 cookie。

        说明：
        - `/backend-api/payments/checkout` 的 modern/custom 入口不仅依赖
          `__Secure-next-auth.session-token`，还会校验若干同域 cookie
          （如 csrf / oai-sc / Cloudflare 相关 cookie 等）。
        - 因此这里不能只回传 session_token，需要尽量保留当前会话里已经拿到的
          `chatgpt.com` 域 cookie 集合。
        """
        cookie_pairs: list[tuple[str, str]] = []
        seen: set[str] = set()

        try:
            jar_iter = list(self.session.cookies)
        except Exception:
            jar_iter = []

        for cookie in jar_iter:
            try:
                name = (getattr(cookie, "name", "") or "").strip()
                value = getattr(cookie, "value", "") or ""
                domain = (getattr(cookie, "domain", "") or "").strip().lower()
            except Exception:
                continue
            if not name or not value:
                continue
            if domain and "chatgpt.com" not in domain:
                continue
            if name in seen:
                continue
            seen.add(name)
            cookie_pairs.append((name, value))

        # 兜底补齐关键 cookie，避免某些 cookiejar 迭代行为差异导致遗漏
        critical_names = [
            "__Secure-next-auth.session-token",
            "__Host-next-auth.csrf-token",
            "__Secure-next-auth.callback-url",
            "oai-did",
            "oai-sc",
            "cf_clearance",
            "__cf_bm",
            "_cfuvid",
            "__cflb",
            "__stripe_mid",
            "__stripe_sid",
            "oai-client-auth-info",
            "oai-gn",
            "oai-nav-state",
            "oai-hlib",
            "_account_is_fedramp",
            "oai_consent_analytics",
            "oai_consent_marketing",
            "oai-allow-ne",
            "_ga",
            "_ga_9SHBSK2D9J",
            "_gcl_au",
            "_fbp",
            "_puid",
            "_dd_s",
            "g_state",
        ]
        for name in critical_names:
            if name in seen:
                continue
            try:
                value = self.session.cookies.get(name, "")
            except Exception:
                value = ""
            if value:
                seen.add(name)
                cookie_pairs.append((name, value))

        return "; ".join(f"{name}={value}" for name, value in cookie_pairs if name and value)
        if self._trace_dump_enabled:
            try:
                os.makedirs("outputs", exist_ok=True)
                ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
                self._trace_dump_path = os.path.join("outputs", f"auth_trace_{ts}_{os.getpid()}.jsonl")
                logger.info(f"HTTP 明文抓包已启用: {self._trace_dump_path}")
            except Exception as e:
                logger.warning(f"初始化 HTTP 抓包文件失败: {e}")
                self._trace_dump_enabled = False

    def _trace_http(self, step: str, resp, extra_request: dict | None = None):
        """可选 HTTP 细粒度追踪（用于协议调试）"""
        if (not self._http_trace_enabled and not self._trace_dump_enabled) or resp is None:
            return
        try:
            req = getattr(resp, "request", None)
            method = getattr(req, "method", "") if req else ""
            req_url = getattr(req, "url", "") if req else ""
            req_body = ""
            req_headers = {}
            if req is not None:
                raw_req_body = getattr(req, "body", None)
                if raw_req_body is None:
                    raw_req_body = getattr(req, "content", None)
                if raw_req_body is None:
                    raw_req_body = getattr(req, "data", None)
                if isinstance(raw_req_body, bytes):
                    req_body = raw_req_body.decode("utf-8", errors="replace")
                elif raw_req_body is not None:
                    req_body = str(raw_req_body)
                try:
                    req_headers = dict(getattr(req, "headers", {}) or {})
                except Exception:
                    req_headers = {}

            # 手动补充请求信息（curl_cffi 某些场景 request.body/headers 为空）
            if isinstance(extra_request, dict):
                if not method:
                    method = str(extra_request.get("method", "") or "")
                if not req_url:
                    req_url = str(extra_request.get("url", "") or "")
                if not req_body:
                    maybe_body = extra_request.get("body", "")
                    if isinstance(maybe_body, bytes):
                        req_body = maybe_body.decode("utf-8", errors="replace")
                    else:
                        req_body = str(maybe_body or "")
                extra_headers = extra_request.get("headers", {})
                if isinstance(extra_headers, dict):
                    merged = dict(req_headers or {})
                    merged.update(extra_headers)
                    req_headers = merged

            status = getattr(resp, "status_code", "N/A")
            final_url = str(getattr(resp, "url", "") or "")
            req_cookie = (req_headers.get("Cookie", "") or "")
            location = (resp.headers.get("Location", "") or "")[:180]
            req_id = (resp.headers.get("x-request-id", "") or "")[:120]
            ctype = (resp.headers.get("Content-Type", "") or "")[:120]
            # 尽量保留完整 Set-Cookie（某些关键 cookie 可能在后续片段）
            set_cookie_list: list[str] = []
            try:
                get_list = getattr(resp.headers, "get_list", None) or getattr(resp.headers, "getlist", None)
                if callable(get_list):
                    vals = get_list("Set-Cookie")
                    if isinstance(vals, list):
                        set_cookie_list = [str(x) for x in vals if x]
            except Exception:
                set_cookie_list = []
            if not set_cookie_list:
                one = (resp.headers.get("Set-Cookie", "") or "")
                if one:
                    set_cookie_list = [one]
            set_cookie_raw = " || ".join(set_cookie_list)
            set_cookie = set_cookie_raw[:260]
            body = (resp.text or "").replace("\n", " ").replace("\r", " ")
            body = body[:260]
            req_headers_lc = {(str(k).lower()): v for k, v in (req_headers or {}).items()}

            if self._http_trace_enabled:
                logger.info(
                    "[HTTP TRACE] %s | %s %s -> %s | url=%s | location=%s | req_id=%s | ctype=%s | set_cookie=%s | body=%s",
                    step,
                    method,
                    req_url[:180],
                    status,
                    final_url[:180],
                    location,
                    req_id,
                    ctype,
                    set_cookie,
                    body,
                )
                if self._trace_include_cookie and req_cookie:
                    logger.info("[HTTP TRACE] %s | req_cookie=%s", step, req_cookie[:360])

            # 从多处信息中抓取 login_verifier/code_verifier
            self._sniff_login_verifier(req_url, f"{step}:req_url")
            self._sniff_login_verifier(req_body, f"{step}:req_body")
            self._sniff_login_verifier(final_url, f"{step}:final_url")
            self._sniff_login_verifier(location, f"{step}:location")
            raw_text = resp.text or ""
            self._sniff_login_verifier(raw_text, f"{step}:resp_body")

            # 明文 HTTP 抓包落盘（jsonl）
            if self._trace_dump_enabled and self._trace_dump_path:
                try:
                    include_req_cookie = self._env_flag("AUTH_TRACE_INCLUDE_REQ_COOKIE", "0")
                    record = {
                        "ts": datetime.utcnow().isoformat() + "Z",
                        "step": step,
                        "request": {
                            "method": method,
                            "url": req_url,
                            "body": req_body[:120000],
                            "headers": {
                                "Content-Type": (req_headers_lc.get("content-type", "") or "")[:240],
                                "Accept": (req_headers_lc.get("accept", "") or "")[:240],
                                "Referer": (req_headers_lc.get("referer", "") or "")[:500],
                                "Origin": (req_headers_lc.get("origin", "") or "")[:120],
                                **(
                                    {
                                        "Cookie": (req_headers_lc.get("cookie", "") or "")[:6000],
                                    }
                                    if include_req_cookie
                                    else {}
                                ),
                            },
                        },
                        "response": {
                            "status_code": status,
                            "url": final_url,
                            "location": resp.headers.get("Location", ""),
                            "x_request_id": resp.headers.get("x-request-id", ""),
                            "content_type": resp.headers.get("Content-Type", ""),
                            "set_cookie": set_cookie_raw,
                            "set_cookie_list": set_cookie_list,
                            "body": raw_text[:120000],
                        },
                        "captured_login_verifier": self._captured_login_verifier,
                    }
                    if self._trace_include_cookie and req_cookie:
                        record["request"]["headers"]["Cookie"] = req_cookie[:8000]
                    with open(self._trace_dump_path, "a", encoding="utf-8") as fw:
                        fw.write(json.dumps(record, ensure_ascii=False) + "\n")
                except Exception as e:
                    logger.debug(f"HTTP 抓包写入失败: {e}")
        except Exception as e:
            logger.debug(f"HTTP trace 输出失败: {e}")

    def _sniff_login_verifier(self, text: str, source: str = ""):
        """从任意文本中提取 login_verifier/code_verifier。"""
        if not text:
            return
        try:
            patterns = [
                r"(?:login_verifier|code_verifier|verifier)=([A-Za-z0-9._~-]{8,})",
                r'"(?:login_verifier|code_verifier|verifier)"\s*:\s*"([^"]{8,})"',
            ]
            for p in patterns:
                m = re.search(p, text)
                if not m:
                    continue
                v = (m.group(1) or "").strip()
                if not v:
                    continue
                if v != self._captured_login_verifier:
                    self._captured_login_verifier = v
                    logger.info("捕获 login_verifier 来源=%s len=%s", source or "unknown", len(v))
                return
        except Exception:
            return

    def _walk_collect_str_fields(obj: Any, wanted_keys: set[str], out: dict[str, str], depth: int = 0, max_depth: int = 6):
        """递归收集目标字段（仅字符串值）。"""
        if depth > max_depth or obj is None:
            return
        if isinstance(obj, dict):
            for k, v in obj.items():
                kk = (str(k) or "").strip().lower()
                if kk in wanted_keys and isinstance(v, str) and v.strip():
                    out[kk] = v.strip()
                AuthFlow._walk_collect_str_fields(v, wanted_keys, out, depth + 1, max_depth)
        elif isinstance(obj, list):
            for it in obj:
                AuthFlow._walk_collect_str_fields(it, wanted_keys, out, depth + 1, max_depth)

    def fetch_client_auth_session_dump(self, stage: str = "") -> dict:
        """
        尝试读取 auth.openai 的 client_auth_session_dump：
        - 可能包含 session_id / client_auth_session 的额外状态
        - 若出现 verifier/refresh 相关字段，自动注入当前流程
        """
        headers = self._common_headers("https://auth.openai.com/email-verification")
        headers["Accept"] = "application/json"
        try:
            resp = self.session.get(
                "https://auth.openai.com/api/accounts/client_auth_session_dump",
                headers=headers,
                timeout=30,
            )
            self._trace_http(f"client_auth_session_dump_{stage or 'default'}", resp)
        except Exception as e:
            logger.debug(f"client_auth_session_dump 请求异常({stage}): {e}")
            return {}

        if resp.status_code != 200:
            logger.info(
                "client_auth_session_dump(%s) 非 200: %s",
                stage or "default",
                resp.status_code,
            )
            return {}

        try:
            data = resp.json()
        except Exception:
            logger.warning(f"client_auth_session_dump({stage}) JSON 解析失败")
            return {}

        if not isinstance(data, dict):
            return {}

        self._client_auth_session_dump = data
        cas = data.get("client_auth_session", {}) if isinstance(data.get("client_auth_session"), dict) else {}

        sid = (data.get("session_id", "") or "").strip() or (cas.get("session_id", "") or "").strip()
        if sid:
            self._client_auth_session_id = sid

        # 同步 OAuth client_id（若 dump 给出更准确值）
        dump_client_id = (cas.get("openai_client_id", "") or data.get("openai_client_id", "") or "").strip()
        if dump_client_id:
            self._oauth_client_id = dump_client_id

        wanted = {
            "login_verifier", "code_verifier", "verifier", "pkce_verifier", "oauth_code_verifier",
            "refresh_token", "oauth_refresh_token", "access_token", "id_token",
        }
        found: dict[str, str] = {}
        self._walk_collect_str_fields(data, wanted, found)

        # verifier 候选
        for key in ("login_verifier", "code_verifier", "verifier", "pkce_verifier", "oauth_code_verifier"):
            v = (found.get(key, "") or "").strip()
            if v and len(v) >= 8:
                self._dump_login_verifier = v
                self._captured_login_verifier = v
                logger.info("client_auth_session_dump 捕获 verifier: key=%s len=%s", key, len(v))
                break

        # token 候选（极少见，但若有直接收下）
        refresh = (found.get("refresh_token", "") or found.get("oauth_refresh_token", "")).strip()
        if refresh:
            self.result.refresh_token = refresh
        acc = (found.get("access_token", "") or "").strip()
        if acc:
            self.result.access_token = acc
        idt = (found.get("id_token", "") or "").strip()
        if idt:
            self.result.id_token = idt

        logger.debug(
            "client_auth_session_dump(%s) 成功: top_keys=%s cas_keys=%s session_id=%s refresh=%s verifier=%s",
            stage or "default",
            list(data.keys())[:12],
            list(cas.keys())[:18] if isinstance(cas, dict) else [],
            (self._client_auth_session_id[:24] if self._client_auth_session_id else ""),
            "有" if self.result.refresh_token else "无",
            "有" if self._dump_login_verifier else "无",
        )
        return data

    def _is_tls_error(exc: Exception) -> bool:
        msg = str(exc).lower()
        markers = ["curl: (35)", "tls connect error", "openssl_internal", "sslerror"]
        return any(m in msg for m in markers)

    def _is_registration_disallowed_error(exc: Exception) -> bool:
        msg = str(exc).lower()
        return "registration_disallowed" in msg

    def _get_cookie_value_by_name(self, name: str) -> str:
        """按 cookie 名称获取值（忽略 domain 冲突）。"""
        try:
            jar = getattr(self.session.cookies, "jar", None)
            if jar is None:
                return ""
            target = (name or "").strip().lower()
            for c in jar:
                if (getattr(c, "name", "") or "").strip().lower() == target:
                    return (getattr(c, "value", "") or "").strip()
        except Exception:
            pass
        return ""

    def _extract_login_challenge_from_cookie(self) -> str:
        """
        从 login_session cookie 中提取 login_challenge。
        login_session 的第一段通常是 base64url(JSON)。
        """
        raw = self._get_cookie_value_by_name("login_session")
        if not raw:
            return ""
        try:
            p0 = raw.split(".")[0]
            p0 += "=" * (-len(p0) % 4)
            payload = json.loads(base64.urlsafe_b64decode(p0.encode("utf-8")).decode("utf-8"))
            return (payload.get("login_challenge", "") or "").strip()
        except Exception:
            return ""

    def _extract_query_first(url: str, keys: list[str]) -> str:
        if not url:
            return ""
        try:
            qs = parse_qs(urlparse(url).query)
        except Exception:
            return ""
        for k in keys:
            val = qs.get(k, [None])[0]
            if val:
                return val
        return ""

    def _extract_page_type(resp_json: dict | None) -> str:
        if not isinstance(resp_json, dict):
            return ""
        page = resp_json.get("page", {})
        if not isinstance(page, dict):
            return ""
        return (page.get("type", "") or "").strip()

    def _extract_continue_url_from_step(resp_json: dict | None) -> str:
        """
        从 auth step 响应提取 continue_url：
        - 顶层 continue_url
        - page.type=external_url 时 payload.url
        """
        if not isinstance(resp_json, dict):
            return ""
        continue_url = (resp_json.get("continue_url", "") or "").strip()
        if continue_url:
            return continue_url
        page = resp_json.get("page", {})
        if not isinstance(page, dict):
            return ""
        if (page.get("type", "") or "").strip() != "external_url":
            return ""
        payload = page.get("payload", {})
        if not isinstance(payload, dict):
            return ""
        return (payload.get("url", "") or "").strip()

    def _b64url_no_pad(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")

    def _rotate_impersonate_session(self) -> bool:
        """仅在 curl_cffi 指纹模式内切换 UA 指纹版本重试，同时联动更新 UA。

        ⚠️ 这里必须连 self._fingerprint 里的 client hints 一起换掉。
        旧版只更新了 self._ua 和 session —— 但 _common_headers / _navigation_headers
        的 sec-ch-ua* 全是从 self._fingerprint 取的，于是换完会变成
        「UA 说 Chrome/136、sec-ch-ua 说 v=146」，连 not_a_brand 都对不上
        （三个版本各不相同："Not.A/Brand";v="99" / "Not/A)Brand";v="8" /
        "Not?A_Brand";v="99"）—— 这正是上一轮刚消灭的「UA 与头自相矛盾」，
        是 CF 最容易抓的特征。之前没爆只因这条路几乎没走到过。

        fallback_impersonates 是**同家族**构造的（见 fingerprint.py 各 _gen_*），
        所以只会 chrome→chrome、safari→safari，不会跨族；但同族换版本一样要同步头。
        """
        if self._impersonate_idx >= len(self._impersonate_candidates) - 1:
            return False
        self._impersonate_idx += 1
        imp = self._impersonate_candidates[self._impersonate_idx]
        self._ua = ua_for_impersonate(imp, self._ua)
        # 让 client hints 跟上新版本，保持 UA 与头自洽
        try:
            self._fingerprint = fingerprint_for_impersonate(imp, self._fingerprint)
        except Exception as e:  # 兜底：宁可维持旧指纹也不要把流程搞崩
            logger.warning(f"client hints 同步失败（沿用旧指纹）: {e}")
        logger.warning(f"TLS 异常，切换指纹重试: impersonate={imp}, ua={self._ua[:60]}...")
        self.session = create_http_session(
            proxy=self.config.proxy, impersonate=imp, user_agent=self._ua,
        )
        return True

    def _datadog_trace_headers() -> dict:
        """生成 Datadog RUM 追踪头（对齐 gptfree-register 格式）。"""
        tid = f"{random.getrandbits(64):016x}"
        sid = str(random.getrandbits(63))
        pid = str(random.getrandbits(63))
        ts_hex = f"{int(time.time()):08x}"
        return {
            "traceparent": f"00-0000000000000000{tid}-{random.getrandbits(64):016x}-01",
            "x-datadog-trace-id": sid,
            "x-datadog-parent-id": pid,
            "x-datadog-sampling-priority": "1",
            "x-datadog-origin": "rum",
            "x-datadog-tags": f"_dd.p.id={tid},_dd.p.tid={ts_hex}00000000,_dd.b.sr=1",
        }

    def _common_headers(self, referer: str = "https://chatgpt.com/") -> dict:
        """
        构造通用请求头。

        关键点：
        - Origin 必须与 Referer 同源（尤其 auth.openai.com 的状态机接口），
          否则容易触发 invalid_state / 风控分支。
        - 在 auth.openai.com 域下，尽量补充 oai-device-id，提升状态机连续性。
        - 全请求注入 Datadog trace 头，避免 OTP silent-drop。
        """
        origin = "https://chatgpt.com"
        try:
            parsed = urlparse(referer or "")
            if parsed.scheme and parsed.netloc:
                origin = f"{parsed.scheme}://{parsed.netloc}"
        except Exception:
            pass

        fp = self._fingerprint
        headers = {
            "Accept": "application/json",
            "Referer": referer,
            "Origin": origin,
            "User-Agent": self._ua,
            "Accept-Language": fp["lang_full"],
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "priority": "u=1, i",
        }
        if fp.get("sec_ch_ua"):
            headers["sec-ch-ua"] = fp["sec_ch_ua"]
            headers["sec-ch-ua-mobile"] = fp.get("sec_ch_ua_mobile") or "?0"
            headers["sec-ch-ua-platform"] = fp["sec_ch_ua_platform"]
            # Client Hints 全套（仅 Chromium 有值，其他浏览器为空串不下发）
            if fp.get("sec_ch_ua_full_version_list"):
                headers["sec-ch-ua-full-version-list"] = fp["sec_ch_ua_full_version_list"]
            if fp.get("sec_ch_ua_arch"):
                headers["sec-ch-ua-arch"] = fp["sec_ch_ua_arch"]
            if fp.get("sec_ch_ua_bitness"):
                headers["sec-ch-ua-bitness"] = fp["sec_ch_ua_bitness"]
            if fp.get("sec_ch_ua_model"):
                headers["sec-ch-ua-model"] = fp["sec_ch_ua_model"]
            if fp.get("sec_ch_ua_platform_version"):
                headers["sec-ch-ua-platform-version"] = fp["sec_ch_ua_platform_version"]

        # auth.openai.com 侧请求补设备标识（若可得）
        try:
            host = (urlparse(origin).netloc or "").lower()
        except Exception:
            host = ""
        if "auth.openai.com" in host:
            device_id = (self.result.device_id or "").strip() or (self.session.cookies.get("oai-did", "") or "").strip()
            if device_id:
                headers["oai-device-id"] = device_id

        headers.update(self._datadog_trace_headers())
        return headers

    def _navigation_headers(self) -> dict:
        """文档导航请求（地址栏直达那种）的头，含 client hints。

        和 _common_headers 的区别只在 Sec-Fetch-* 那组：那边是 XHR（empty/cors/
        same-origin），这里是整页导航（document/navigate/none + user + UIR）。
        **client hints 两边必须一致**，都从 self._fingerprint 取：Chrome 指纹发
        全套，Safari/Firefox 指纹 sec_ch_ua 为空串、一个都不发——这正是真实浏览器
        的行为。旧 warmup 手搓头漏了这段，导致 Chrome UA 裸奔，实测 403 率 4/5，
        补齐后 5/5 通过（详见 warmup docstring）。
        """
        fp = self._fingerprint
        headers = {
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                      "image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": fp["lang_full"],
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "sec-fetch-dest": "document",
            "sec-fetch-mode": "navigate",
            "sec-fetch-site": "none",
            "sec-fetch-user": "?1",
            "upgrade-insecure-requests": "1",
            "priority": "u=0, i",
            "User-Agent": self._ua,
        }
        if fp.get("sec_ch_ua"):
            headers["sec-ch-ua"] = fp["sec_ch_ua"]
            headers["sec-ch-ua-mobile"] = fp.get("sec_ch_ua_mobile") or "?0"
            headers["sec-ch-ua-platform"] = fp["sec_ch_ua_platform"]
            for key, name in (
                ("sec_ch_ua_full_version_list", "sec-ch-ua-full-version-list"),
                ("sec_ch_ua_arch", "sec-ch-ua-arch"),
                ("sec_ch_ua_bitness", "sec-ch-ua-bitness"),
                ("sec_ch_ua_model", "sec-ch-ua-model"),
                ("sec_ch_ua_platform_version", "sec-ch-ua-platform-version"),
            ):
                if fp.get(key):
                    headers[name] = fp[key]
        return headers
