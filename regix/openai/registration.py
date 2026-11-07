from __future__ import annotations

import base64
import json
import logging
import os
import time
import uuid

from regix.http.client import create_http_session
from regix.providers.mail import MailProvider

from .models import AuthResult, _totp_now

logger = logging.getLogger(__name__)


class RegistrationMixin:
    """Protocol methods for this domain."""

    def run_register(self, mail_provider: MailProvider) -> AuthResult:
        """执行完整注册流程"""
        # 本次注册流程开始的时刻 —— 取件时间窗的**下界兜底**。
        #
        # ⚠️ 为什么要单独记：一次性邮箱（短效 code 模式）只保留**一封**邮件，
        #    而那封是服务端在「提交邮箱」时自动发的 —— 它比"主动补发时刻"
        #    （otp_sent_at）早十几秒到达（中间隔着设密码的 PoW，实测 16~21 秒）。
        #    若用 otp_sent_at 做下界，唯一那封会被时间窗滤掉 → 死等超时。
        #    所以短效模式下界要用这个更早的时刻。
        self._register_started_at = time.time()

        # 检查网络
        if not self.check_proxy():
            logger.warning("网络预检查未通过，继续尝试注册链路以获取精确错误...")
        # warmup 失败 = 没拿到 oai-did = 后面 authorize/continue 必 409（实测 5/5）。
        # 必须在 create_mailbox 之前拦掉：邮箱是花钱的，不能为一个注定 409 的轮次浪费。
        if not self.warmup():
            raise RuntimeError(
                "warmup 失败：4 次重试均未拿到 chatgpt.com 的 oai-did cookie，"
                "继续注册必然 409 invalid_state（多为代理出口 IP 不通或被 CF 拦），"
                "请检查代理后重试"
            )

        # 登录/注册链路
        csrf_token = self.get_csrf_token()

        # ── 下单邮箱 ──
        # ⚠️ 下单放在**第 1 步之后、第 2 步之前**：
        #    第 2 步 get_auth_url 要用 email 拼 login_hint（"预填邮箱"提示），
        #    所以最晚只能晚到第 2 步前。往前挪到 warmup 之后（第 1 步之前）
        #    只会让「第 1 步 CSRF 失败」也白下一个邮箱 —— 没意义。
        email = mail_provider.create_mailbox()
        self.result.email = email

        # 不要在这里传 login_hint。OpenAI 当前会因 login_hint 预先推进授权状态，
        # 直接落到 email-verification；随后的 authorize_continue 提交邮箱会报
        # invalid_state。先停在账号选择态，让 signup() 正常提交邮箱。
        auth_url = self.get_auth_url(csrf_token)
        device_id = self.auth_oauth_init(auth_url)
        if "/email-verification" in getattr(self, "_oauth_init_final_url", ""):
            raise RuntimeError(
                "OAuth 初始化异常提前进入 email-verification（invalid_state）"
            )

        # ── signup 链可重试 ──
        #
        # ⚠️ 为什么可重试：409 invalid_state 的服务端原文是
        #    "Your sign-in session is no longer valid. Please start over to continue."
        #    —— 它本来就是要你**从头再来**。
        #
        # 🔴 为什么旧实现救不回来（2026-11-06 实测 10 连续注册，6 次 409 全废）：
        #    旧实现只做了「重建 session + 重新 warmup」，但**没刷新 device_id**。
        #    device_id 在第 3 步 auth_oauth_init 时写进 self.result.device_id，
        #    而 _common_headers 里取 device_id 的逻辑是：
        #        self.result.device_id or self.session.cookies.get("oai-did")
        #    —— `or` 短路，self.result.device_id 一旦有值就永远用它。
        #    于是重建 session 后拿到的是**新的 oai-did cookie**，但请求头发出去的
        #    还是**旧的 oai-device-id** → 服务端认定设备不一致 → invalid_state。
        #    实测印证：warmup 第一次就成功、409 重试后 warmup 也第一次就成功，
        #    照旧 409 —— 换的只是 session，从来没换过 device_id。
        #
        # ✅ 现在用方案 B：重试时把 **第 1~3 步整个重跑**（csrf → auth_url →
        #    oauth_init → 拿新的 device_id），再 warmup → PoW → submit。
        #    这才真正符合服务端 "start over to continue" 的语义。
        #    代价是每次重试多花 ~10 秒（csrf + auth_url + oauth_init），
        #    但换来的是从"必然 409"变成"可能救回"。
        #
        # ⚠️ 邮箱不换：重试复用同一个邮箱（邮箱是花钱买的，重试不额外花钱）。
        # ⚠️ 只对 invalid_state 重试。user_already_exists 这类业务拒绝
        #    重试多少次都没用（邮箱本身废了），不该在这里空转。
        max_attempts = 3  # 首次 + 2 次重试
        is_new = False
        last_err = ""
        for attempt in range(1, max_attempts + 1):
            try:
                # ── 第 4 步：PoW ──
                sentinel = self.get_sentinel_token(device_id)

                # ── 第 5 步：提交注册邮箱 ──
                # ⚠️ email 在循环外就已创建（重试复用同一个，不重复下单）
                is_new = self.signup(email, sentinel)
                break
            except Exception as e:  # noqa: BLE001
                last_err = str(e)
                if "invalid_state" not in last_err.lower():
                    raise
                if attempt >= max_attempts:
                    logger.error(
                        f"signup 链连续 {attempt} 次 invalid_state，放弃"
                        f"（邮箱已付费，本轮浪费）: {last_err[:200]}"
                    )
                    raise
                logger.warning(
                    f"signup 撞 invalid_state（第 {attempt}/{max_attempts} 次），"
                    f"从头再来（重建会话 + 刷新 device_id）—— 邮箱复用，不额外花钱"
                )
                # ── 从头再来：重建 session（新的 oai-did）──
                time.sleep(3 + attempt * 2)
                # 🔴 必须连指纹一起换：CF 403 是指纹级的（safari260 必 403，
                #    同 IP 同 URL 换 chrome146/safari18_0 都是 200）。
                #    原来这里只重建 session、指纹不变 → 3 次重试全撞同一堵墙，
                #    实测 3 次全是同一个 403 → invalid_state。
                if not self._rotate_impersonate_session():
                    self.session = create_http_session(
                        proxy=self.config.proxy,
                        impersonate=self._impersonate_candidates[self._impersonate_idx],
                        user_agent=self._ua,
                    )
                # 🔴 清掉上一轮的会话身份，否则 _common_headers 会一直用旧值
                #    （`self.result.device_id or cookies["oai-did"]`，or 短路）。
                #    不清 → 新 cookie + 旧 device-id → 必然又 409（实测 6/6）。
                self.result.device_id = ""
                self._last_sentinel_so_token = ""

                if not self.warmup():
                    raise RuntimeError(
                        "warmup 失败：4 次重试均未拿到 chatgpt.com 的 oai-did cookie，"
                        "继续注册必然 409 invalid_state（多为代理出口 IP 不通或被 CF 拦），"
                        "请检查代理后重试"
                    )

                # ── 重跑第 1~3 步：拿新的 csrf / auth_url / device_id ──
                # ⚠️ device_id 必须刷新：authorize/continue 的 oai-device-id 头
                #    要和当前 session 的 oai-did 一致，否则服务端判设备不一致。
                #    auth_url 也得重拿 —— 它是 auth.openai.com/authorize 的入口，
                #    里面的 state 跟 device_id 绑定。
                csrf_token = self.get_csrf_token()
                auth_url = self.get_auth_url(csrf_token)
                device_id = self.auth_oauth_init(auth_url)
                logger.info(
                    f"[重试] 会话已刷新 device_id={device_id[:16]}...，"
                    f"继续第 4 步（PoW）"
                )

        # 号池邮箱被 OpenAI 标"已有账号" 处理策略:
        #   WEBUI_ALLOW_LOGIN=1 (promo-link 等需要拿 access_token 的模式) → 走 OTP login 拿凭证
        #   WEBUI_ALLOW_LOGIN 未设 (register-only 模式) → fast-fail mark dead 换下一个号
        # 这样 register-only 不被 honeypot 拖死, promo-link 又能复用已存在账号.
        #
        # ⚠️ 这个分支对**所有号池型 provider** 生效（outlook / icloud_relay / 以后新增的），
        #    不是 outlook 专属 —— 日志前缀用 provider 自己的 kind，别写死。
        pool_tag = getattr(mail_provider, "kind", "pool")
        # passwordless_signup 也是新号。signup() 的 False 只表示没有走
        # create_account_password 页面，不能据此把它当作已有账号 fast-fail。
        is_pooled_existing = (
            not is_new
            and getattr(mail_provider, "pooled", False)
            and self._existing_email_verification_mode != "passwordless_signup"
        )
        if is_pooled_existing:
            _allow_login = self._get_env("WEBUI_ALLOW_LOGIN", "").strip() in (
                "1", "true", "yes",
            )
            if _allow_login:
                logger.info(
                    f"[{pool_tag}] '已有账号' 分支但 WEBUI_ALLOW_LOGIN=1 → 走 OTP login 拿凭证 ({email})"
                )
            else:
                logger.warning(
                    f"[{pool_tag}] '已有账号' 分支检测到号池邮箱 ({email}) → fast-fail mark dead, "
                    f"让外层 register() 自动 claim 下一个号 (设 WEBUI_ALLOW_LOGIN=1 改走 OTP login)"
                )
                try:
                    mail_provider.mark_dead(
                        "OpenAI 识别为已有账号 (接码池二手 / honeypot, 协议层 fast-fail)"
                    )
                except Exception:
                    pass
                raise RuntimeError(
                    f"OpenAI 静默拒绝发 OTP (识别 {email} 为已有账号, {pool_tag} 池 fast-fail)"
                )

        # ⚠️ passwordless_signup **也是新账号**，只是服务端选择了"不设密码直接发码"的注册流程。
        #    signup() 用一个 bool 表达三种服务端状态，把它和"已有账号"压成了同一个 False，
        #    结果新号全部走进下面的 else 分支 —— register_password 从没被调用过，
        #    注册出来的号全是无密码号（只能靠临时邮箱收码登录，域名一失效就永久丢失）。
        #    实测 2026-08-06: 这类号照样能走 POST user/register 设密码并成功。
        if is_new or self._existing_email_verification_mode == "passwordless_signup":
            # ── 短效（一次性）邮箱：跳过设密码 ──
            #
            # ⚠️ 为什么必须跳（详见 base.MailProvider.ephemeral_mailbox）：
            #    设密码（POST user/register）成功后，服务端切到 email_otp_send 页，
            #    **提交邮箱时自动发的那封 OTP 立即失效** → 必须主动补发第 2 封。
            #    而短效邮箱只保留一封邮件，**收不到第 2 封** → 死锁：
            #      · 不补发 → 拿失效的第 1 封 verify → 409 invalid_state
            #      · 补发   → 邮件收不到 → 等到超时
            #    跳过设密码就不触发流程切换，第 1 封保持有效 → 直接 verify 即可，
            #    全程只要 1 封邮件。
            #
            #    代价：注册出来是无密码号（只能靠邮箱收码登录）。
            #    短效邮箱本来就一次性，邮箱失效后无密码号等于永久丢失 ——
            #    但 access_token 照样能拿到（verify_otp / create_account /
            #    get_auth_session 三处都不需要密码），导出给 chatgpt2api 照常用。
            skip_password = bool(getattr(mail_provider, "ephemeral_mailbox", False))
            if skip_password:
                logger.info(
                    "[register] 短效邮箱模式（一次性）：跳过设密码，"
                    "只收 1 封验证码（服务端自动发的那封，此时仍有效）"
                )
                # 时间窗下界用「注册开始」—— 那封邮件比"主动发码时刻"早十几秒到。
                otp_sent_at = self._register_started_at
            else:
                # 新账号：注册密码 → 主动重新发码 → 验证 OTP → 创建账户
                password_registered = self.register_password(email)
                if password_registered:
                    # ⚠️ POST user/register 成功后服务端**切换流程**到 email_otp_send 页
                    # （实测响应 continue_url=/api/accounts/email-otp/send, page.type=email_otp_send），
                    # signup 阶段自动发的那封 OTP 立即失效，拿它 verify 会 409 invalid_state。
                    # 所以必须主动重新发码，且以本次发码时间为准，不能再用 -8 偏移。
                    # 时间戳在发码**之前**取：邮件是服务端在这次请求里发出的，
                    # 先发再取时间会让 otp_sent_at 晚于邮件时间戳，被 issued_after 过滤掉。
                    otp_sent_at = time.time()
                    try:
                        # ⚠️ 这里会收到**两封**验证码邮件，验证码相同、发件人不同：
                        #    · otp@tm1.openai.com    ← authorize/continue 时服务端自动发的
                        #    · noreply@tm.openai.com  ← 这次 send_otp 发的
                        # 原因：passwordless_signup 流程下服务端在提交邮箱那步
                        # 已经自动发过一封；POST user/register 成功后流程切到
                        # email_otp_send 页，必须**主动重新发码**（旧的那封
                        # 立即失效，verify 会 409 invalid_state）。
                        # 所以"只请求一次发码、却收到两封邮件"是正常的，
                        # 取件侧靠 issued_after 时间窗只取新的那封。
                        self.send_otp()
                    except Exception as e:
                        # 429 等发码失败时别让整个注册崩掉，退回 resend 兜底
                        logger.warning(f"密码注册后主动发码失败，回退 resend: {e}")
                        self.kickoff_otp_delivery("post_register_password_send_failed")
                else:
                    logger.warning("注册密码失败，回退到已有账号 OTP 路径")
                    self.fetch_client_auth_session_dump("post_register_password_failed_new")
                    # 密码注册失败时 fallback 主动发码
                    if not self.kickoff_otp_delivery("register_password_failed_fallback"):
                        self.send_otp()
                    otp_sent_at = time.time()

            try:
                otp_timeout = max(10, int(self._get_env("OTP_TIMEOUT", "60")))
            except Exception:
                otp_timeout = 180
            otp_code = mail_provider.wait_for_otp(
                email,
                timeout=otp_timeout,
                issued_after=otp_sent_at,
                # 短效邮箱的下界兜底：只有 1 封邮件，且那封早于 otp_sent_at。
                # 普通 provider 不认识这个参数 → base 里默认忽略。
                earliest=self._register_started_at,
            )
            try:
                self.verify_otp(otp_code)
                self.fetch_client_auth_session_dump("post_verify_otp_new")
            except RuntimeError as e:
                # 偶发 401 错码，补发一次 OTP 并重试
                if "401" in str(e):
                    logger.warning(f"OTP 首次验证失败，补发重试: {e}")
                    otp_sent_at = time.time()
                    if not self.kickoff_otp_delivery("verify_otp_retry_new"):
                        self.send_otp()
                    otp_code = mail_provider.wait_for_otp(
                        email,
                        timeout=otp_timeout,
                        issued_after=otp_sent_at,
                        earliest=self._register_started_at,
                    )
                    self.verify_otp(otp_code)
                    self.fetch_client_auth_session_dump("post_verify_otp_retry_new")
                else:
                    raise

            try:
                continue_url = self.create_account()
            except Exception as e:
                # registration_disallowed 时尝试 reauthorize 兜底，若仍不可用再抛出
                if self._is_registration_disallowed_error(e):
                    logger.warning("create_account 被拒绝，尝试 reauthorize 兜底获取 session ...")
                    continue_url = self._reauthorize_for_session(auth_url) or ""
                    if not continue_url:
                        raise
                else:
                    raise
        else:
            # 已有账号：直接发 OTP → 验证 → 获取 session
            mode = (self._existing_email_verification_mode or "").lower()
            page_type = (self._existing_page_type or "").lower()
            continue_url = ""

            try:
                otp_timeout = max(10, int(self._get_env("OTP_TIMEOUT", "60")))
            except Exception:
                otp_timeout = 180

            if page_type == "login_password":
                logger.info("已有账号进入 login_password 分支，先走密码校验再 OTP")
                login_password, pw_is_real = self._resolve_login_password(email)
                if pw_is_real:
                    self.result.password = login_password
                else:
                    logger.info("该号无已知密码，用默认规则猜一个试试（多半 401）")
                login_resp = self.login_password_verify(login_password)
                login_page_type = self._extract_page_type(login_resp)
                continue_url = self._normalize_continue_url(
                    (login_resp or {}).get("continue_url", "") if isinstance(login_resp, dict) else ""
                )

                # mfa-challenge 分支（密码验证后需要 TOTP 2FA）
                if self._is_mfa_challenge_state(login_page_type, continue_url):
                    totp_secret = (self.result.totp_secret or "").strip()
                    if not totp_secret and self._account_callback:
                        # 从数据库加载凭证
                        try:
                            cred = self._account_callback(email)
                            if cred and cred.get("totp_secret"):
                                totp_secret = cred["totp_secret"]
                                self.result.totp_secret = totp_secret
                                logger.info("已从数据库加载 totp_secret")
                        except Exception as e:
                            logger.warning(f"account_callback 异常: {e}")
                    if not totp_secret:
                        logger.warning("进入 mfa-challenge 但没有 totp_secret，无法继续")
                    else:
                        challenge_id = continue_url.split("/")[-1] if "/mfa-challenge/" in continue_url else ""
                        if challenge_id:
                            totp_code = _totp_now(totp_secret)
                            logger.info(f"提交 TOTP 码进行 2FA 验证（challenge_id={challenge_id[:16]}...）")
                            mfa_resp = self.submit_mfa_totp(totp_code, challenge_id)
                            continue_url = self._normalize_continue_url(
                                (mfa_resp or {}).get("continue_url", "") if isinstance(mfa_resp, dict) else ""
                            )
                        else:
                            logger.warning("无法从 continue_url 提取 challenge_id")

                # 部分账号密码校验后仍需 email otp（二次校验）
                elif not continue_url or "/email-verification" in continue_url:
                    # password/verify 后推荐使用 resend，而不是 /email-otp/send
                    otp_sent_at = time.time()
                    self.kickoff_otp_delivery("existing_login_password")
                    otp_code = mail_provider.wait_for_otp(
                        email,
                        timeout=otp_timeout,
                        issued_after=otp_sent_at,
                    )
                    otp_resp = self.verify_otp(otp_code)
                    continue_url = self._normalize_continue_url(
                        (otp_resp or {}).get("continue_url", "") if isinstance(otp_resp, dict) else ""
                    )
            else:
                need_send_otp = mode not in ("passwordless_signup", "passwordless_login")
                if need_send_otp:
                    otp_sent_at = time.time()
                    self.send_otp()
                else:
                    # 某些模式在 /authorize/continue 已触发发码，不要重复 /email-otp/send 以免破坏 state
                    # 默认先尝试 /email-otp/resend 获取新码，失败再回看短窗口
                    forced_resend = self._env_flag("OTP_FORCE_RESEND", "0")
                    if forced_resend and self.kickoff_otp_delivery("existing_forced_resend"):
                        otp_sent_at = time.time()
                        logger.debug(f"已有账号验证码模式={mode}，已主动 resend OTP")
                    else:
                        # 回看短窗口，避免误读上一轮旧验证码
                        otp_sent_at = time.time() - 8
                        logger.info(f"已有账号验证码模式={mode}，跳过额外 send_otp，直接等邮件")

                try:
                    otp_code = mail_provider.wait_for_otp(
                        email,
                        timeout=otp_timeout,
                        issued_after=otp_sent_at,
                    )
                except TimeoutError:
                    # provider 判定自己已无可用收件链路时会设 exhausted=True 并 mark_dead，
                    # 不 retry 直接 raise，避免再次等待无效收件链路。
                    # （outlook 是 IMAP-only 纯协议失败；别的 provider 有自己的判据。）
                    if getattr(mail_provider, "exhausted", False):
                        logger.warning(
                            f"[{getattr(mail_provider, 'kind', 'mail')}] "
                            f"收码链路已失效并 mark dead, 跳过 retry resend"
                        )
                        raise
                    # 否则 (非号池场景, 如 catch_all CF KV) 给一次 resend retry
                    logger.warning("未等到已有账号 OTP，先重发后重试等待")
                    otp_sent_at = time.time()
                    if not self.kickoff_otp_delivery("existing_timeout_retry"):
                        self.send_otp()
                    try:
                        otp_code = mail_provider.wait_for_otp(
                            email,
                            timeout=otp_timeout,
                            issued_after=otp_sent_at,
                        )
                    except TimeoutError:
                        # 号池 + "已有账号" 分支 + 两次 timeout = OpenAI 反欺诈
                        # 静默拒绝（页面声称已注册但不真发邮件）→ mark dead 该邮箱
                        # 让池下次跳过，user 重新点 ▶ 自动 claim 下一个 available。
                        # （非号池 provider 如 CF 临时邮箱 mark_dead 是空操作，不用额外判断。）
                        if getattr(mail_provider, "pooled", False):
                            try:
                                mail_provider.mark_dead(
                                    "OpenAI 静默拒绝发 OTP（'已有账号'但 INBOX 无邮件）"
                                )
                            except Exception:
                                pass
                        raise
                try:
                    otp_resp = self.verify_otp(otp_code)
                    self.fetch_client_auth_session_dump("post_verify_otp_existing")
                except RuntimeError as e:
                    if any(code in str(e) for code in ("401", "409")):
                        logger.warning(f"OTP 首次验证失败，重发重试: {e}")
                        otp_sent_at = time.time()
                        if not self.kickoff_otp_delivery("existing_verify_retry"):
                            self.send_otp()
                        otp_code = mail_provider.wait_for_otp(
                            email,
                            timeout=otp_timeout,
                            issued_after=otp_sent_at,
                        )
                        otp_resp = self.verify_otp(otp_code)
                        self.fetch_client_auth_session_dump("post_verify_otp_retry_existing")
                    else:
                        raise
                continue_url = (otp_resp or {}).get("continue_url", "") if isinstance(otp_resp, dict) else ""
                continue_url = self._normalize_continue_url(continue_url)
                if self._is_add_phone_state(page_type=self._extract_page_type(otp_resp), continue_url=continue_url):
                    continue_url = self._normalize_continue_url(
                        self._handle_add_phone_verification(continue_url=continue_url)
                    )

            # 某些已有账号在 OTP 后会进入 about-you，需要补一次 create_account
            if continue_url and "/about-you" in continue_url:
                try:
                    continue_url = self.create_account()
                except Exception as e:
                    if self._is_registration_disallowed_error(e):
                        logger.warning("about-you create_account 被拒绝，尝试 reauthorize 兜底获取 session ...")
                        continue_url = self._reauthorize_for_session(auth_url) or ""
                        if continue_url:
                            logger.info("reauthorize 兜底成功，继续后续 session 获取")
                            # 下游会走 follow_redirect_chain + get_auth_session
                            pass
                        else:
                            raise
                    else:
                        logger.warning(f"已有账号 about-you 创建信息失败，回退 reauthorize: {e}")
                        continue_url = ""

            # 若 otp 响应未给可用 continue_url，则回退到 reauthorize
            if not continue_url:
                # auth.openai.com 的 session cookie 已设置，直接拿 code
                continue_url = self._reauthorize_for_session(auth_url)

        if continue_url:
            continue_url = self._normalize_continue_url(continue_url)
            # 关键尝试：在 chatgpt callback 被消费前，先走一次 Codex OAuth（有助于保留 auth.openai 登录态）
            # ⚠️ 挂了 on_session_ready 钩子时**跳过这次抢跑**：钩子（绑 2FA）要等
            #    get_auth_session 拿到 access_token 才能跑，而那步在下面 :callback 之后；
            #    这次抢跑成功的话 Codex 就跑到钩子前面去了，指定的顺序等于没改。
            #    跳过后 Codex 落到后面那个调用点（get_auth_session 之后），顺序才是
            #    创建账户 → 重定向链 → 拿 session → 绑 2FA → Codex 授权 → 接码。
            if (
                (not self.result.refresh_token)
                and self._on_session_ready is None
                and self._env_flag("OAUTH_CODEX_RT_BEFORE_CALLBACK", "1")
            ):
                self.oauth_codex_rt_exchange(mail_provider=mail_provider)
            # 可选：在 callback 被消费前尝试 token 交换（可能影响后续 callback，默认关闭）
            refresh_only_mode = self._env_flag("OAUTH_REFRESH_ONLY", "0")
            pre_exchange_default = "1" if refresh_only_mode else "0"
            pre_exchange = self._env_flag("OAUTH_EXCHANGE_BEFORE_CALLBACK", pre_exchange_default)
            if pre_exchange and not self._env_flag("SKIP_OAUTH_TOKEN_EXCHANGE", "0"):
                self.oauth_token_exchange(continue_url, continue_url)
            callback_url, final_url = self.follow_redirect_chain(continue_url)
            if (not callback_url) and final_url and ("/workspace" in final_url):
                normalized = self._normalize_continue_url(final_url)
                if normalized and normalized != final_url:
                    callback_url, final_url = self.follow_redirect_chain(normalized)
        else:
            callback_url, final_url = None, None

        refresh_only_mode = self._env_flag("OAUTH_REFRESH_ONLY", "0")

        # ─ 关键顺序修复 ─
        # 旧版顺序：get_auth_session → oauth_token_exchange → _consume_callback (兜底)
        # 问题：oauth_token_exchange 直接 POST /oauth/token 用掉了 callback 的 code，
        #       后面 _consume_callback 再 GET callback URL 时 code 已失效 →
        #       NextAuth 拒绝 set __Secure-next-auth.session-token cookie。
        # 新版：先 _consume_callback 让 chatgpt.com NextAuth 自己消费 code 并 set 全套
        #       cookie（含 session-token），然后再 get_auth_session 拿 access_token；
        #       Codex RT exchange 用独立 authorize 链路，跟 chatgpt callback 不冲突。
        if (not refresh_only_mode) and callback_url:
            logger.debug("消费 callback 触发 NextAuth Set-Cookie (session-token)")
            self._consume_callback_for_session(callback_url)

        if not refresh_only_mode:
            self.get_auth_session()

        # ── 钩子：session 到手、Codex 授权之前 ──
        # 主人指定的顺序是「注册完 → 绑 2FA → Codex 授权 → 接码」。这里是唯一同时满足
        # 「已经有 access_token」和「Codex 还没跑」的位置，所以插在这。
        # 失败绝不能拖垮已注册成功的号 —— 异常吞掉，继续往下走 Codex。
        if self._on_session_ready is not None and self.result.access_token:
            try:
                self._on_session_ready(self, self.result.access_token)
            except Exception as e:
                logger.warning(f"session_ready 回调失败（不影响注册）: {e}")

        # Codex OAuth refresh_token 交换（独立 authorize 链路，不依赖上面 callback 的 code）
        if callback_url or continue_url:
            self.fetch_client_auth_session_dump("pre_oauth_exchange_register")
            # 注意：oauth_token_exchange(callback_url) 会和 NextAuth 抢同一个 code，
            # 默认禁用避免冲突；只有用户显式 SET OAUTH_TOKEN_EXCHANGE_FROM_CALLBACK=1
            # 才尝试（极少需要，access_token 已通过 NextAuth callback 拿到）。
            if self._env_flag("OAUTH_TOKEN_EXCHANGE_FROM_CALLBACK", "0") \
                    and not self._env_flag("SKIP_OAUTH_TOKEN_EXCHANGE", "0"):
                self.oauth_token_exchange(callback_url or "", continue_url or "")
            if (not self.result.refresh_token) and self._env_flag("OAUTH_CODEX_RT_EXCHANGE", "1"):
                self.oauth_codex_rt_exchange(mail_provider=mail_provider)
            if (not self.result.refresh_token) and self._env_flag("OAUTH_SECONDARY_AUTHORIZE_EXCHANGE", "0"):
                self.oauth_secondary_authorize_exchange()
            # 最终再拉一次 session（Codex 流程可能更新 cookie/access_token）
            if not refresh_only_mode:
                self.get_auth_session()

        if refresh_only_mode:
            if not (self.result.refresh_token or self.result.access_token):
                raise RuntimeError("流程完成但未获取 refresh_token/access_token")
        elif not self.result.is_valid():
            raise RuntimeError("注册完成但未获取有效凭证")

        logger.info("注册流程完成!")
        return self.result

    def run_protocol_login(self, mail_provider: MailProvider, email: str, password: str = "") -> AuthResult:
        """
        纯协议登录（不创建随机邮箱）：
        - 适配 passwordless / login_password 两类已有账号入口
        - 可配合 OAUTH_EXCHANGE_BEFORE_CALLBACK / OAUTH_REFRESH_ONLY 尝试优先拿 refresh_token
        """
        if not (email or "").strip():
            raise RuntimeError("run_protocol_login 缺少邮箱")

        if not self.check_proxy():
            logger.warning("网络预检查未通过，继续尝试登录链路以获取精确错误...")
        # 同 run_register：没 oai-did 就走不通 authorize 链，早失败早换 IP。
        # 这里不花钱建邮箱，但报错说清原因，省得当成"密码错"排查。
        if not self.warmup():
            raise RuntimeError(
                "warmup 失败：4 次重试均未拿到 chatgpt.com 的 oai-did cookie，"
                "继续登录必然 409 invalid_state（多为代理出口 IP 不通或被 CF 拦），"
                "请检查代理后重试"
            )

        # run_protocol_login 的语义即"登录已有账号"（docstring 明写）。kickoff_otp_delivery
        # 依据 _is_existing_account 选 resend vs send_passwordless_otp 分支；落到 send
        # 分支会把 server-side state 弄坏 → 之后 IMAP 抓到的 OTP X 已失效 → verify 401
        # wrong_email_otp_code。这里入口统一 set True，覆盖 passwordless 这类 page_type
        # 不在 ("login_password","email_otp_verification") 集合的情况；signup() 回退
        # 路径会基于 OpenAI 真实响应再次覆盖（True/False），无副作用。
        self._is_existing_account = True

        email = email.strip()
        self.result.email = email
        login_password = (password or "").strip()
        if login_password:
            self.result.password = login_password
        else:
            login_password, pw_is_real = self._resolve_login_password(email)
            if pw_is_real:
                self.result.password = login_password
            else:
                logger.info("协议登录：调用方没给密码、库里也没有，用默认规则猜一个试试")

        csrf_token = self.get_csrf_token()
        # 与 run_register 相同：邮箱只应通过 authorize_continue 提交一次。
        auth_url = self.get_auth_url(csrf_token)
        device_id = self.auth_oauth_init(auth_url)
        sentinel = self.get_sentinel_token(device_id)

        continue_url = ""
        try:
            otp_timeout = max(10, int(self._get_env("OTP_TIMEOUT", "60")))
        except Exception:
            otp_timeout = 180

        page_type = ""
        mode = ""
        prefer_login_screen_first = str(
            os.getenv("LOCALAUTH_EXISTING_LOGIN_USE_LOGIN_HINT", "1")
        ).lower() in ("1", "true", "yes", "on")

        if prefer_login_screen_first:
            try:
                logger.info("已有账号协议登录：优先走 login screen_hint 探测 password/otp 分支")
                login_step = self.authorize_continue(
                    email=email,
                    sentinel_token=sentinel,
                    screen_hint="login",
                    referer="https://auth.openai.com/log-in",
                    trace_step="authorize_continue_login_protocol",
                )
                page_type = (self._extract_page_type(login_step) or "").lower()
                continue_url = self._normalize_continue_url(
                    self._extract_continue_url_from_step(login_step)
                )
                page = (login_step.get("page") or {}) if isinstance(login_step, dict) else {}
                payload = (page.get("payload") or {}) if isinstance(page, dict) else {}
                mode = (payload.get("email_verification_mode", "") or "").lower()
                self._existing_page_type = page_type
                self._existing_email_verification_mode = mode

                if page_type == "login_password" or "/log-in/password" in (continue_url or ""):
                    logger.info("登录分支: login_password -> password/verify")
                    # 命中已有账号 password 路径：标记之，让 kickoff_otp_delivery 走 resend
                    # 分支（避免 send_passwordless_otp 把 state 弄坏 → wrong_email_otp_code）
                    self._is_existing_account = True
                    login_resp = self.login_password_verify(login_password)
                    page_type = (self._extract_page_type(login_resp) or "").lower()
                    continue_url = self._normalize_continue_url(
                        self._extract_continue_url_from_step(login_resp)
                    )

                    # mfa-challenge 分支（密码验证后需要 TOTP 2FA）
                    if self._is_mfa_challenge_state(page_type, continue_url):
                        totp_secret = (self.result.totp_secret or "").strip()
                        if not totp_secret and self._account_callback:
                            # 从数据库加载凭证
                            try:
                                cred = self._account_callback(email)
                                if cred and cred.get("totp_secret"):
                                    totp_secret = cred["totp_secret"]
                                    self.result.totp_secret = totp_secret
                                    logger.info("已从数据库加载 totp_secret")
                            except Exception as e:
                                logger.warning(f"account_callback 异常: {e}")
                        if not totp_secret:
                            logger.warning("进入 mfa-challenge 但没有 totp_secret，无法继续")
                        else:
                            challenge_id = continue_url.split("/")[-1] if "/mfa-challenge/" in continue_url else ""
                            if challenge_id:
                                totp_code = _totp_now(totp_secret)
                                logger.info(f"提交 TOTP 码进行 2FA 验证（challenge_id={challenge_id[:16]}...）")
                                mfa_resp = self.submit_mfa_totp(totp_code, challenge_id)
                                page_type = (self._extract_page_type(mfa_resp) or "").lower()
                                continue_url = self._normalize_continue_url(
                                    self._extract_continue_url_from_step(mfa_resp)
                                )
                            else:
                                logger.warning("无法从 continue_url 提取 challenge_id")

                elif page_type == "email_otp_verification" or "/email-verification" in (continue_url or ""):
                    logger.info("登录分支: email_otp_verification")
                    # 同上：authorize/continue 已 trigger 发码，kickoff_otp_delivery 必须只 resend。
                    self._is_existing_account = True
                else:
                    logger.info(
                        "login screen_hint 未直接命中已有账号完成态: page_type=%s continue_url=%s",
                        page_type or "(empty)",
                        (continue_url or "")[:180] or "(empty)",
                    )
            except Exception as e:
                logger.warning(f"login screen_hint 探测失败，回退 signup 探测: {e}")
                continue_url = ""
                page_type = ""
                mode = ""

        if not continue_url and page_type not in ("login_password", "email_otp_verification"):
            is_new = self.signup(email, sentinel)
            if is_new:
                logger.warning("目标邮箱未命中已有账号分支，回退到注册链路")
                self.register_password(email)
                otp_sent_at = time.time()
                self.send_otp()
                otp_code = mail_provider.wait_for_otp(
                    email,
                    timeout=otp_timeout,
                    issued_after=otp_sent_at,
                )
                self.verify_otp(otp_code)
                continue_url = self.create_account()
            else:
                page_type = (self._existing_page_type or "").lower()
                mode = (self._existing_email_verification_mode or "").lower()
        else:
            page_type = (page_type or self._existing_page_type or "").lower()
            mode = (mode or self._existing_email_verification_mode or "").lower()

        if not continue_url or "/email-verification" in continue_url:
            # 仍需 OTP：优先 resend 获取新码
            otp_sent_at = time.time()
            resend_ok = self.kickoff_otp_delivery("protocol_need_otp")
            if not resend_ok and mode not in ("passwordless_signup", "passwordless_login"):
                self.send_otp()
                otp_sent_at = time.time()

            otp_code = mail_provider.wait_for_otp(
                email,
                timeout=otp_timeout,
                issued_after=otp_sent_at,
            )
            try:
                otp_resp = self.verify_otp(otp_code)
                self.fetch_client_auth_session_dump("post_verify_otp_protocol")
            except RuntimeError as e:
                if any(code in str(e) for code in ("401", "409")):
                    logger.warning(f"OTP 首次验证失败，重发重试: {e}")
                    otp_sent_at = time.time()
                    if not self.kickoff_otp_delivery("protocol_verify_retry"):
                        self.send_otp()
                    otp_code = mail_provider.wait_for_otp(
                        email,
                        timeout=otp_timeout,
                        issued_after=otp_sent_at,
                    )
                    otp_resp = self.verify_otp(otp_code)
                    self.fetch_client_auth_session_dump("post_verify_otp_retry_protocol")
                else:
                    raise
            continue_url = self._extract_continue_url_from_step(otp_resp)
            continue_url = self._normalize_continue_url(continue_url)
            if self._is_add_phone_state(page_type=self._extract_page_type(otp_resp), continue_url=continue_url):
                continue_url = self._normalize_continue_url(
                    self._handle_add_phone_verification(continue_url=continue_url)
                )

        continue_url = self._normalize_continue_url(continue_url)
        # 某些边缘态 OTP 后未返回 callback，回退 reauthorize
        if not continue_url:
            continue_url = self._reauthorize_for_session(auth_url) or ""

        refresh_only_mode = self._env_flag("OAUTH_REFRESH_ONLY", "0")
        callback_url = ""
        if continue_url:
            continue_url = self._normalize_continue_url(continue_url)
            if (not self.result.refresh_token) and self._env_flag("OAUTH_CODEX_RT_BEFORE_CALLBACK", "1"):
                self.oauth_codex_rt_exchange(mail_provider=mail_provider)
            pre_exchange_default = "1" if refresh_only_mode else "0"
            pre_exchange = self._env_flag("OAUTH_EXCHANGE_BEFORE_CALLBACK", pre_exchange_default)
            if pre_exchange:
                self.oauth_token_exchange(continue_url, continue_url)
            callback_url, final_url = self.follow_redirect_chain(continue_url)
            if (not callback_url) and final_url and ("/workspace" in final_url):
                normalized = self._normalize_continue_url(final_url)
                if normalized and normalized != final_url:
                    callback_url, final_url = self.follow_redirect_chain(normalized)

        if not refresh_only_mode:
            self.get_auth_session()

        if callback_url or continue_url:
            self.fetch_client_auth_session_dump("pre_oauth_exchange_protocol")
            self.oauth_token_exchange(callback_url or "", continue_url or "")
            if (not self.result.refresh_token) and self._env_flag("OAUTH_CODEX_RT_EXCHANGE", "1"):
                self.oauth_codex_rt_exchange(mail_provider=mail_provider)
            if (not self.result.refresh_token) and self._env_flag("OAUTH_SECONDARY_AUTHORIZE_EXCHANGE", "0"):
                self.oauth_secondary_authorize_exchange()
            if not refresh_only_mode:
                self.get_auth_session()

        if refresh_only_mode:
            if not (self.result.refresh_token or self.result.access_token):
                raise RuntimeError("协议登录完成，但未拿到 refresh_token/access_token")
        elif not self.result.is_valid():
            raise RuntimeError("协议登录完成，但未拿到有效 session/access token")

        logger.info("纯协议登录流程完成")
        return self.result

    def from_existing_credentials(
        self, session_token: str, access_token: str, device_id: str
    ) -> AuthResult:
        """使用已有凭证（跳过注册）"""
        self.result.device_id = device_id or str(uuid.uuid4())
        self.session.cookies.set("oai-did", self.result.device_id, domain=".chatgpt.com")
        detected_email = ""

        # 如果有 session_token, 用它刷新 access_token (旧 access_token 可能已过期)
        if session_token:
            self.session.cookies.set(
                "__Secure-next-auth.session-token",
                session_token,
                domain=".chatgpt.com",
            )
            logger.info("使用 session_token 刷新 access_token...")
            try:
                headers = self._common_headers("https://chatgpt.com/")
                resp = self.session.get(
                    "https://chatgpt.com/api/auth/session",
                    headers=headers,
                    timeout=30,
                )
                session_data = resp.json() if resp is not None else {}
                new_access_token = session_data.get("accessToken", "")
                user_obj = session_data.get("user", {}) if isinstance(session_data, dict) else {}
                if isinstance(user_obj, dict):
                    detected_email = detected_email or (user_obj.get("email", "") or "")
                new_session_token = self.session.cookies.get("__Secure-next-auth.session-token", "")
                if new_access_token:
                    access_token = new_access_token
                    logger.info("access_token 刷新成功")
                else:
                    logger.warning(f"access_token 刷新失败 (status={resp.status_code}), 使用原 token")
                if new_session_token:
                    session_token = new_session_token
            except Exception as e:
                logger.warning(f"刷新 access_token 失败: {e}, 使用原 token")
        elif access_token:
            # 没有 session_token, 尝试通过 access_token 获取
            logger.info("未提供 session_token, 尝试通过 access_token 获取...")
            try:
                headers = self._common_headers("https://chatgpt.com/")
                headers["Authorization"] = f"Bearer {access_token}"
                resp = self.session.get(
                    "https://chatgpt.com/api/auth/session",
                    headers=headers,
                    timeout=30,
                )
                session_data = resp.json() if resp is not None else {}
                user_obj = session_data.get("user", {}) if isinstance(session_data, dict) else {}
                if isinstance(user_obj, dict):
                    detected_email = detected_email or (user_obj.get("email", "") or "")
                session_token = self.session.cookies.get("__Secure-next-auth.session-token", "")
                if session_token:
                    logger.info("通过 access_token 获取 session_token 成功")
                else:
                    logger.warning("未能获取 session_token, 可能需要手动提供")
            except Exception as e:
                logger.warning(f"获取 session_token 失败: {e}")

        self.result.access_token = access_token
        self.result.session_token = session_token
        if session_token:
            self.session.cookies.set(
                "__Secure-next-auth.session-token",
                session_token,
                domain=".chatgpt.com",
            )
        self.result.cookie_header = self._build_chatgpt_cookie_header()

        # 回填 email（skip-register 模式下常用于账单 email）
        if not detected_email and access_token and access_token.count(".") >= 2:
            try:
                payload_b64 = access_token.split(".")[1]
                payload_b64 += "=" * (-len(payload_b64) % 4)
                payload = json.loads(base64.urlsafe_b64decode(payload_b64.encode("utf-8")).decode("utf-8"))
                prof = payload.get("https://api.openai.com/profile", {}) if isinstance(payload, dict) else {}
                if isinstance(prof, dict):
                    detected_email = detected_email or (prof.get("email", "") or "")
            except Exception:
                pass
        self.result.email = detected_email or ""
        logger.info("使用已有凭证初始化完成")
        return self.result
