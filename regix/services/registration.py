"""注册 worker：调 auth_flow.run_register，并把日志/状态实时推到队列。

每个注册任务跑在独立线程；通过 `RunLogger` 把 `logging` 记录 + tail 状态推
到队列，前端用 SSE 实时收日志。
"""
from __future__ import annotations

import logging
import os
import queue
import threading
import traceback
import uuid
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[2]  # Regix/

from regix.config import Config  # noqa: E402
from regix.openai.auth_flow import AuthFlow  # noqa: E402
from regix.providers.mail import (  # noqa: E402
    MailProviderError,
    create_mail_provider,
    get_provider_class,
)
from regix.providers.sms.provider import PhoneCallbackController  # noqa: E402

from ..storage import facade as db  # noqa: E402

# run_id -> queue of log strings; sentinel = None 表示流结束
_run_queues: dict[str, queue.Queue] = {}
_lock = threading.Lock()

# 当前线程正在跑哪个 run。
# ⚠️ 为什么需要这个：QueueLogHandler 是挂在 **root logger** 上的，而 root logger
#    是进程全局的。auto_loop 并发时 N 个 run 各挂一个 handler，每条日志会被
#    广播进**所有** run 的文件和 SSE 流 —— 实测 2026-08-04 三 worker 并发，
#    一个号的记录同时出现在 3 个 .log 里，WebUI 上三个号的日志搅在一起，
#    而 "[4/10] 获取 Sentinel Token..." 这类行不带邮箱，根本分不清是谁的。
#
#    注册链路（auth_flow / mail_providers / sentinel）内部不开任何线程，
#    一个 run 的日志全在自己那条线程上产生，所以线程绑定就能干净切开。
_current_run = threading.local()

LOG_DIR = Path(
    os.environ.get("WEBUI_LOG_DIR")
    or (Path(__file__).resolve().parents[2] / "webui" / "logs")
)
LOG_DIR.mkdir(parents=True, exist_ok=True)
# ⚠️ LOG_DIR 支持用环境变量 WEBUI_LOG_DIR 覆盖 ——
#    Docker 部署时日志要挂出来，容器重建后还能翻旧记录查问题。


class QueueLogHandler(logging.Handler):
    """把 logging 记录扔进 run queue + 写 log 文件。

    只收**本 run 线程**产生的日志，见 emit 里的过滤。
    """

    def __init__(self, run_id: str, log_file: Path):
        super().__init__()
        self.run_id = run_id
        self._fh = open(log_file, "a", encoding="utf-8")
        self.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        ))

    def emit(self, record: logging.LogRecord):
        try:
            # emit 是在**打日志的那条线程**里同步跑的，所以这里读到的就是
            # 日志产生者的 run_id。别人 run 的日志直接丢掉。
            rid = getattr(_current_run, "run_id", None)
            if rid is not None and rid != self.run_id:
                return
            # rid is None = 不属于任何 run（webui 请求线程、启动期日志等）。
            # 这类照旧广播给所有 handler —— 宁可多收也不能丢，日志文件
            # 开头那句 "webui: [run] xxx -> email@..." 就是这么来的。
            msg = self.format(record)
            self._fh.write(msg + "\n")
            self._fh.flush()
            q = _run_queues.get(self.run_id)
            if q is not None:
                q.put(msg)
        except Exception:
            pass

    def close(self):
        try:
            self._fh.close()
        except Exception:
            pass
        super().close()


def _emit_status(run_id: str, kind: str, payload: dict | str = ""):
    """前端约定：以 `__EVENT__:` 开头的行被解析成 JSON 状态事件。"""
    import json as _json
    q = _run_queues.get(run_id)
    if q is None:
        return
    body = payload if isinstance(payload, dict) else {"message": str(payload)}
    body["kind"] = kind
    q.put("__EVENT__:" + _json.dumps(body, ensure_ascii=False))


# 网络/环境层错误特征：命中任一就把号放回 available（号本身没问题，是环境炸了）
_NETWORK_ERROR_PATTERNS = [
    "tls", "ssl", "sslerror", "connection", "connect error", "timeout", "timed out",
    "proxy", "socks", "dns", "name resolution", "name or service",
    "cloudflare", "just a moment", "403 forbidden",
    "csrf token 获取失败", "csrf token 失败",
    "/sentinel/req", "sentinel /req", "sentinel quickjs",
    "check_proxy 失败", "网络预检查",
    "curl: (35)", "curl: (28)", "curl: (6)", "curl: (7)",
    "remote disconnected", "connection reset", "connection aborted",
    "max retries exceeded",
    "invalid_state",
    "no module named",
]


# 邮箱侧的限制（不是网络问题，也不是账号本身的问题）。
#
# ⚠️ 必须单独归类，**不能归 network**：
#    network 类别会走 db.release_unused(email) → 号被放回 available 复用。
#    而短效（一次性）邮箱的 serviceToken 是一次性取件凭证，邮件已经取过，
#    复用必然再失败（白等一轮 + 白花钱）。
#    所以短效邮箱取件超时 = 这个邮箱已经消耗掉了（平台会自动退款），
#    号应该废弃，不复用。
_MAIL_LIMITED_PATTERNS = [
    "remail otp timeout",
    "短效邮箱",
]


# Cloudflare 拦截的特征。
#
# ⚠️ 必须**单独**归类，不能混进 network：
#    network = "整个环境炸了" → auto_loop 会熔断整个批次、暂停自动注册
#    cf_blocked = "**这个出口 IP** 被 CF 拒了" → 只该换一个代理继续跑，
#                 没必要把整个批次停掉。
#
# 判据来自实测日志的 403 特征：只有 `__cf_bm` 没有 `cf_clearance`
# → 这是 WAF/BotManagement 的**直接拒绝**，根本没下发可解的 JS 挑战，
#    所以 FlareSolverr / 浏览器方案无效，只能换出口 IP。
_CF_BLOCKED_PATTERNS = [
    "未种到 oai-did",
    "__cf_bm",
    "warmup 失败",
    "cloudflare",
    "just a moment",
]


def classify_error(err: str, mail_source: str = "") -> str:
    """分类错误：
      'network'     —— 环境/代理整体问题（号无辜，会 release 复用 + 计入熔断）
      'account'     —— 号/邮箱本身有问题（这个号废了，换下一个）
      'mail_limited'—— 邮箱侧限制（如短效邮箱已消耗）
      'cf_blocked'  —— **这个出口 IP** 被 Cloudflare 拒了（换代理即可，不必熔断）
      'unknown'     —— 兜底

    mail_source 用来问 provider 要不要豁免某些模式 —— 比如 iCloud 中转号
    本来就是买的老号，"已有账号"是正常流程不是失败（见
    MailProvider.accepts_existing_account）。留空则按最严格的规则判。
    """
    s = (err or "").lower()

    # ⚠️ mail_limited 要在 network 之前判：短效邮箱的报错文案里含
    #    "timeout"（"Remail OTP timeout ..."），会被 network 特征命中。
    #    但它不是网络问题 —— 是邮箱一次性、已经消耗掉了。
    if any(p in s for p in _MAIL_LIMITED_PATTERNS):
        return "mail_limited"

    # ⚠️ cf_blocked 也要在 network 之前判：
    #    "warmup 失败：4 次重试均未拿到 ... oai-did cookie" 这句里
    #    含 "超时"/"失败" 类特征，而且 cloudflare 特征也在 network 列表里。
    #    但它的正确处置是「换代理」，不是「熔断整个批次」。
    if any(p in s for p in _CF_BLOCKED_PATTERNS):
        return "cf_blocked"

    account_patterns = [
        "wrong_email_otp_code", "invalid_grant", "imap xoauth2",
        "outlook imap account unusable", "user is authenticated but not connected",
        "outlook refresh failed", "authentication failed", "authenticate failed",
        "outlook otp timeout", "registration_disallowed",
        "已有账号", "账号被", "refresh_token 失效",
        # ⚠️ user_already_exists 是邮箱层面的拒绝，不是环境问题：
        #   · user_already_exists：这个邮箱地址在 OpenAI 已经有账号了
        #     （二手邮箱交易池，别人先注册过了）→ 这个号彻底废了，换下一个
        "user_already_exists",
    ]
    if mail_source:
        try:
            exempt = get_provider_class(mail_source).accepts_existing_account
        except MailProviderError:
            exempt = False  # 未知来源 —— 按默认最严格规则走
        # ⚠️ 用 if-in 而不是裸 remove()：上面的模式表将来被人改动/重排后，
        #    remove 抛的 ValueError 会跟 get_provider_class 的错混在同一个
        #    except 里被一起吞掉，豁免静默失效且没人看得出来。
        if exempt and "已有账号" in account_patterns:
            account_patterns.remove("已有账号")

    # 先匹配 account 特征（更具体），避免子串误命中（如 "outlook OTP timeout" 含 "timeout"）
    if any(p in s for p in account_patterns):
        return "account"
    if any(p in s for p in _NETWORK_ERROR_PATTERNS):
        return "network"
    return "unknown"


def _do_register(
    run_id: str,
    account: dict,
    options: dict,
    log_file: Path,
):
    """实际注册任务。

    options:
        want_access_token: bool
        want_session_token: bool
        want_refresh_token: bool
        proxy: Optional[str]
        otp_timeout: int
        allow_existing_login: bool
    """
    # 先认领本线程，再挂 handler —— 顺序不能反：中间要是有日志产生，
    # 没打标记的话会被广播到其他并发 run 的日志里去。
    _current_run.run_id = run_id

    handler = QueueLogHandler(run_id, log_file)
    handler.setLevel(logging.INFO)
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)
    # 第一次需要的话提到 INFO 级别
    if root_logger.level > logging.INFO or root_logger.level == 0:
        root_logger.setLevel(logging.INFO)

    email = account["email"]
    # 提前读取，避免在 try 块前异常时 except 引用未定义
    mail_source = db.get_setting("mail_source", "outlook")
    # 要不要操作号池（mark_done / mark_failed / release）由 provider 声明的
    # pooled 决定。未知 kind 时保守当池化处理 —— 号池里真有这行的话
    # 至少不会漏掉状态回写，把号永远卡在 in_use。
    try:
        is_pooled = get_provider_class(mail_source).pooled
    except MailProviderError:
        is_pooled = True

    try:
        # 本次注册专属的配置覆盖。
        # ⚠️ 以前是写 os.environ + finally 还原，但 auto_loop 并发跑多个 worker，
        #    os.environ 是**进程全局**的：A 设的 OTP_TIMEOUT/WEBUI_ALLOW_LOGIN 会被
        #    B 读到，B 跑完还原成 A 之前的值，A 后半程就用上别人的配置了。
        #    现在整个 dict 直接传给 AuthFlow，只挂在实例上，谁都污染不到谁。
        env_overrides = {}
        # outlook 接码邮箱常被 OpenAI 走 passwordless_signup 流程（新号收码而非设密码），
        # auth_flow 会误判为"已有账号"分支 → 不设 WEBUI_ALLOW_LOGIN 会 fast-fail。
        # 单号 WebUI 场景下 fast-fail 没意义（批量跑才需要"跳过被识别的号"），故强制 ON。
        env_overrides["WEBUI_ALLOW_LOGIN"] = "1"
        # OTP 等待下限 60 秒。
        # ⚠️ 原来完全跟着前端传的值走，而前端默认是 10 秒 —— 对 Remail 这类
        #    **聚合转发**邮箱根本不够：邮件要经平台中转再投到目标邮箱，
        #    实测 20~30 秒才到（发码 18:34:41 → 到达 18:35:05），10 秒必然超时，
        #    注册跑到最后一步全废。这里兜一个下限，用户仍然可以调大。
        try:
            _otp_timeout = int(options.get("otp_timeout") or 180)
        except (TypeError, ValueError):
            _otp_timeout = 180
        env_overrides["OTP_TIMEOUT"] = str(max(60, _otp_timeout))
        # ── 是否跑 Codex OAuth（换 refresh_token）──
        #
        # 两个条件都满足才跑：
        #   1. 用户要 rt（want_refresh_token）
        #   2. 接码真的可用 —— Codex 授权落到 /add-phone 时，只有配了 SMS
        #      controller 才推进得动（auth_flow._handle_add_phone_via_sms 的
        #      前置条件是 self._sms_callback is not None）。接码没配时，
        #      authorize 走不通 → rt=0 → 白耗 8~10 秒。
        #      更糟的是授权链可能回退到 /log-in **再发一封验证码**
        #      （auth_flow._codex_drive_login_from_log_in 的 need_otp 分支）——
        #      对短效邮箱（code 模式，一次性）这是致命的。
        #
        # ⚠️ OAUTH_SECONDARY_AUTHORIZE_EXCHANGE 以前漏了没关：那个"二次
        #    authorize"分支同样可能触发收件，一并关掉。
        want_rt = bool(options.get("want_refresh_token", False))
        sms_ready = _sms_ready()
        reglog = logging.getLogger("registrar")
        if not want_rt:
            env_overrides["SKIP_OAUTH_TOKEN_EXCHANGE"] = "1"
            env_overrides["OAUTH_CODEX_RT_EXCHANGE"] = "0"
            env_overrides["OAUTH_CODEX_RT_BEFORE_CALLBACK"] = "0"
            env_overrides["OAUTH_SECONDARY_AUTHORIZE_EXCHANGE"] = "0"
            reglog.info("[register] 不获取 refresh_token → 跳过 Codex OAuth（不触发二次收件）")
        elif not sms_ready:
            # 用户想要 rt，但接码没配 —— add-phone 分支推进不动，跑也是白跑。
            env_overrides["OAUTH_CODEX_RT_EXCHANGE"] = "0"
            env_overrides["OAUTH_CODEX_RT_BEFORE_CALLBACK"] = "0"
            env_overrides["OAUTH_SECONDARY_AUTHORIZE_EXCHANGE"] = "0"
            reglog.info(
                "[register] 未启用接码（无 API Key）→ 跳过 Codex OAuth："
                "add-phone 分支推进不动，且可能触发二次收件"
            )
        # PROXY 走 cfg.proxy，无需 env

        cfg = Config()
        cfg.proxy = (options.get("proxy") or "").strip() or None

        # ─ 邮箱来源路由 ─
        # 原来是 if cf_temp / else outlook 的写死分支，加一种邮箱就得回来改。
        # 现在交给注册表工厂：provider 自己从 settings + account 里取需要的字段。
        mail = create_mail_provider(mail_source, db.get_mail_settings(), account)
        logging.getLogger("registrar").info(
            f"[register] 邮箱来源: {mail_source} ({mail.display_name})"
        )

        # ─ 2FA 绑定钩子：插在「拿到 session」和「Codex 授权」之间 ─
        #   主人指定的顺序：注册完 → 绑 2FA → Codex 授权 → 接码。
        #   2FA 必须有 access_token 才能打 mfa/enroll，而 at 只能从 get_auth_session 拿，
        #   所以这是唯一「已有 at 且 Codex 还没跑」的位置（见 auth_flow.py 那处注释）。
        #   钩子里绑成了就把结果存进 _tfa_box，run_register 返回后直接取，不再重绑。
        _tfa_box: dict = {}

        def _bind_2fa_hook(_flow, at: str) -> None:
            # ⚠️ 这里**不查密码**。快路径 bind_totp_2fa_inline 只拿 access_token 打
            #    mfa_info / enroll / activate，全程不碰密码（two_factor.py:153）。
            #    以前拿 flow.result.password 当门禁，把**重跑的老号全挡在门外**：
            #    老号被 OpenAI 认成已有账号 → 本轮不走 register_password →
            #    内存里密码是空的（真密码在库里，靠下面那段回读补），于是 at 明明齐活
            #    也绑不上（实测一个重跑的老号：at 长度 1762 齐活，却被跳过）。
            from .two_factor import bind_totp_2fa_inline
            info = bind_totp_2fa_inline(_flow, at)
            if info and info.get("secret"):
                _tfa_box.update(info)
                # ★ 一拿到 secret 立刻落盘，别等后面 Codex 授权 + 接码那几分钟。
                #   接码太久用户一关进程，_tfa_box 内存里的 secret 就永久没了，
                #   而 secret 一次性下发、服务端取不回（跟 _save_password_early 同理）。
                #   ⚠️ 必须用【真正的注册邮箱】flow.result.email，绝不能用外层 email：
                #      非池化 provider（CF 等）外层 email 是占位符
                #      xxx_placeholder_N@placeholder.local，用它落盘会跟后面 save_registered
                #      的真实邮箱对不上 —— 库里凭空多出一条占位垃圾行（两行）。
                #      run_register 一开头就设了 result.email（auth_flow.py:3102），
                #      走到这个钩子时它必然已是真实邮箱；取不到再退回外层 email 兜底。
                #   这里绝不能拖垮注册，包一层 try：落盘失败也还有 _tfa_box 兜着。
                try:
                    real_email = getattr(getattr(_flow, "result", None), "email", "") or email
                    db.save_totp_early(real_email, info["secret"], info.get("factor_id", ""))
                    logging.getLogger("registrar").info(
                        f"[register] 2FA secret 已早落盘 email={real_email}"
                    )
                except Exception as e:
                    logging.getLogger("registrar").warning(
                        f"[register] 2FA secret 早落盘失败（内存仍保留）: {e}"
                    )

        def _account_callback_for_flow(email: str) -> dict:
            """从数据库加载账号凭证（密码和 totp_secret）供 AuthFlow 登录时使用。

            用于既有账号登录场景：当服务端返回 mfa-challenge 时，AuthFlow 需要
            totp_secret 来计算 6 位动态码完成 2FA 验证。
            """
            try:
                data = db.get_registered(email)
                if data:
                    return {
                        "password": data.get("password", ""),
                        "totp_secret": data.get("totp_secret", ""),
                    }
            except Exception as e:
                logging.getLogger("registrar").warning(f"[register] account_callback 异常: {e}")
            return {}

        flow = AuthFlow(
            cfg,
            sms_callback=_build_sms_callback(run_id),
            env_overrides=env_overrides,
            on_password=_save_password_early,
            on_session_ready=_bind_2fa_hook if options.get("want_2fa") else None,
            account_callback=_account_callback_for_flow,
        )
        _emit_status(run_id, "phase", {"phase": "starting", "email": email})
        # 把关键开关的实际生效值打进日志 —— 主人反馈过"关掉 2FA 好像没反应"，
        # 光看界面分不清是开关没传过来还是绑定失败，这里留个明确的凭据。
        logging.getLogger("registrar").info(
            f"[register] 开始: {email} | 2FA绑定={'开' if options.get('want_2fa') else '关'} "
            f"| OTP等待={env_overrides.get('OTP_TIMEOUT')}s "
            f"| 代理={'有' if str(options.get('proxy') or '').strip() else '直连'}"
        )

        partial = False
        d: dict
        try:
            result = flow.run_register(mail)
            d = result.to_dict()
        except RuntimeError as e:
            # 部分凭证也算成功（OTP 验证通过 + create_account 成功 → flow.result 有 token）
            d = flow.result.to_dict()
            need_access = options.get("want_access_token", True)
            need_session = options.get("want_session_token", True)
            need_refresh = options.get("want_refresh_token", True)
            # 用户勾选的凭证全拿到 → 算正常完成（不视为 partial）
            wanted_ok = (
                (not need_access or d.get("access_token"))
                and (not need_session or d.get("session_token"))
                and (not need_refresh or d.get("refresh_token"))
            )
            has_any = bool(
                d.get("access_token") or d.get("refresh_token") or d.get("session_token")
            )
            if wanted_ok and has_any:
                logging.getLogger("registrar").warning(
                    f"[register] 流程末段异常但用户勾选的凭证已齐: {e}"
                )
            elif has_any:
                partial = True
                logging.getLogger("registrar").warning(
                    f"[register] 部分凭证 (缺用户勾选的某项): {e}"
                )
            else:
                raise

        # ─ 用户选项过滤：未勾选的字段从结果里抹掉，DB 只存用户想要的
        full = d
        d = {
            "email": full.get("email", ""),
            "password": full.get("password", ""),
            # device_id 是 oai-did（设备标识）。不落库的话，下次用这个号
            # 重新登录/调接口时只能现生成一个，和当初注册时的设备对不上 ——
            # 早期那批账号的 registered.device_id 全是空的就是这个原因。
            "device_id": full.get("device_id", ""),
        }
        if options.get("want_access_token", True):
            d["access_token"] = full.get("access_token", "")
        if options.get("want_session_token", True):
            d["session_token"] = full.get("session_token", "")
            d["cookie_header"] = full.get("cookie_header", "")  # 同样是浏览器注入用
        if options.get("want_refresh_token", True):
            d["refresh_token"] = full.get("refresh_token", "")
            d["id_token"] = full.get("id_token", "")

        # ─ session 接口的完整响应 JSON 原文 ──
        # 上面那个 dict 是**白名单重建**的，没列出来的字段全被抹掉了，
        # 所以 session_json 必须显式带上，否则存不进库。
        #
        # 它跟 access_token / session_token 是同一份凭证的两个视角：
        # 拆开的那几列方便程序用，完整 JSON 保留 expires / user.* 等
        # 拆的时候丢掉的字段。两者都存，不冲突。
        _sess_json = str(full.get("session_json") or "").strip()
        if _sess_json:
            d["session_json"] = _sess_json

        # ─ 密码回读：必须在 2FA 之前 ─
        # ⚠️ d 是**本轮内存里**的结果，它不一定知道这个号有密码：
        #    重跑一个之前设过密码的邮箱时，OpenAI 会认成已有账号 → passwordless_login
        #    → register_password 根本不执行 → d["password"] 是空的，
        #    但上一轮 save_password_early 存的密码还在库里。
        #    两个下游都要它：① 2FA 慢路径要用密码重走 login 链；
        #    ② 前端 done 事件 `v-if="lastRunResult.password"` 判空会把密码行
        #       连同两个复制按钮一起藏掉，主人会以为密码丢了。
        #    以前这段在 2FA **之后**，于是老号在 2FA 眼里永远"无密码"→ 被跳过。
        #    只在 d 里密码为空时查一次，正常路径零额外开销。
        if not (d.get("password") or "").strip():
            try:
                _saved = db.get_registered(d.get("email") or "")
                _pw = ((_saved or {}).get("password") or "").strip()
                if _pw:
                    d["password"] = _pw
                    logging.getLogger("registrar").info(
                        "[register] 本轮未设密码，沿用库中已存密码（上一轮 register_password 留下的）"
                    )
            except Exception as e:
                logging.getLogger("registrar").warning(f"[register] 回读已存密码失败: {e}")

        # ─ 可选：绑定 TOTP 2FA（仅用户勾选 want_2fa 时才跑） ─
        #   正常情况上面的 on_session_ready 钩子已经在【Codex 授权之前】绑完了，
        #   这里只是兜底：钩子没跑到（run_register 中途抛异常走 partial 分支、
        #   或那时 access_token 还是空）时再补一次。
        #   兜底本身也是先快后慢两条路（见 two_factor.py 模块头）：
        #     快 bind_totp_2fa_inline —— 直接复用刚跑完注册的 flow + access_token，
        #        6.2s 搞定，零 PoW 零邮件（实测 2026-08-08 <测试号>@<自建域>
        #        四个请求全 200，mfa_enabled=true）。
        #     慢 bind_totp_2fa —— 新起 AuthFlow 重走 login 正式链，约 40s + 一次 PoW
        #        + 一封验证码邮件。只在快路径没成时兜底。
        #   失败仅告警、绝不废掉已注册成功的号；secret 一次性下发，成功即随 d 落库+推前端。
        #   ⚠️ 入口条件**不查密码**：快路径只要 access_token。密码只是慢路径
        #      （重走 login 链）的前提，所以判断挪到回落那一步再做。
        if options.get("want_2fa"):
            _emit_status(run_id, "phase", {"phase": "binding_2fa", "email": d.get("email")})
            try:
                from .two_factor import bind_totp_2fa, bind_totp_2fa_inline
                # 钩子（Codex 授权之前那次）已经绑好就直接用，别再打一遍 enroll
                tinfo = dict(_tfa_box) if _tfa_box.get("secret") else None
                if not tinfo:
                    tinfo = bind_totp_2fa_inline(flow, full.get("access_token", ""))
                if not (tinfo and tinfo.get("secret")):
                    # 慢路径要拿密码重登一次，没密码就只能到此为止
                    if (d.get("password") or "").strip():
                        logging.getLogger("registrar").info(
                            "[register] 2FA 快路径未成，回落重走登录链..."
                        )
                        tinfo = bind_totp_2fa(
                            cfg, d.get("email", ""), d.get("password", ""),
                            mail_provider=mail, env_overrides=env_overrides,
                        )
                    else:
                        logging.getLogger("registrar").warning(
                            "[register] 2FA 快路径未成，且该号无密码（库里也没有），"
                            "慢路径走不了，跳过绑定"
                        )
                if tinfo and tinfo.get("secret"):
                    d["totp_secret"] = tinfo["secret"]
                    d["totp_factor_id"] = tinfo.get("factor_id", "")
                    logging.getLogger("registrar").info(
                        f"[register] 2FA 绑定成功 email={d.get('email')}"
                    )
                    _emit_status(run_id, "phase", {"phase": "2fa_bound", "email": d.get("email")})
                else:
                    logging.getLogger("registrar").warning(
                        "[register] 2FA 绑定未成功（账号仍有效，仅未绑 2FA）"
                    )
            except Exception as e:
                logging.getLogger("registrar").warning(
                    f"[register] 2FA 绑定异常（账号仍有效）: {e}"
                )
        # 记下这个号用的代理。
        # 用途：① 导出「access_token----代理」给 chatgpt2api，让注册和使用同一个 IP；
        #      ② 账号被删除 / 标注异常时，把这个代理占用的配额还回去。
        run_proxy = str(options.get("proxy") or "").strip()
        if run_proxy:
            d["proxy"] = run_proxy
        # 落库（密码已在 2FA 之前回读补齐，这里 d 里该有的都有了）
        db.save_registered(d)
        # ⚠️ 这里**不再**给 used_count +1：自动跑号时 pick_proxy() 选中代理
        #    已经预占了一格，成功就是确认这格；失败由 auto_loop 把预占还回去。
        #    但「单次注册」页是用户手填的代理，没走过 pick_proxy()，
        #    所以那种情况要在这里补记一次（代理不在池里时 proxy_used 也不会命中，
        #    只是个记账，不会出错）。

        # ─ 注册后自动查询套餐和 Plus 试用资格（失败不影响账号） ─
        plan_info = _check_plan_after_registration(run_id, d)

        # Remail：把这笔订单关联到注册成功的账号，
        # 这样「邮箱服务」页能看出哪一单对应哪个号。
        order_no = str(getattr(mail, "_order_no", "") or "").strip()
        if order_no:
            try:
                db.link_remail_order(order_no, d.get("email") or email)
            except Exception as e:
                logging.getLogger("registrar").warning(
                    f"[register] Remail 订单关联失败 order={order_no}: {e}"
                )
        # 非池化 provider 的 email 是虚拟占位（xxx_placeholder_N@placeholder.local），
        # 号池里根本没这行，不能去 mark。判据用 provider 的 pooled，不写死 kind。
        if is_pooled:
            db.mark_done(email)

        # ─ 可选：导出到 CPA / SUB2API 面板（仅勾选启用时才执行） ─
        _try_export_to_panels(run_id, d)

        result_summary = {
            "email": d.get("email"),
            # 密码走明文推给前端：token 只给长度是因为太长且必须点按钮复制，
            # 但密码是随机 16 位、用户注册完第一件事就是拿去登录，
            # 藏在「查看凭证」弹窗里等于每次都要多点两下。
            # 这是本机自用工具，SSE 只发给本地浏览器，不外传。
            "password": d.get("password") or "",
            "access_token_len": len(d.get("access_token") or ""),
            "session_token_len": len(d.get("session_token") or ""),
            "refresh_token_len": len(d.get("refresh_token") or ""),
            # 2FA secret 一次性下发、服务端取不回，明文推前端让用户当场导入验证器
            # （理由同密码；本机自用工具，SSE 只发本地浏览器）。未绑则为空串。
            "totp_secret": d.get("totp_secret") or "",
            "plan": plan_info.get("current_plan_type") or "",
            "plan_label": plan_info.get("plan_label") or "",
            "plus_check": plan_info,
            "partial": partial,
        }
        _emit_status(run_id, "done", result_summary)
        logging.getLogger("registrar").info(
            f"[register] 完成 email={d.get('email')} "
            f"pw={d.get('password') or '(无)'} "
            f"at={result_summary['access_token_len']} "
            f"st={result_summary['session_token_len']} "
            f"rt={result_summary['refresh_token_len']}"
        )
        db.finish_run(run_id, "done")

    except Exception as e:
        err = str(e)
        category = classify_error(err, mail_source)
        logging.getLogger("registrar").error(f"[register] 失败 (category={category}): {err}")
        # ⚠️ 密码是在 register_password 里现生成的，只活在内存里。
        #    走到这里说明 save_registered 没执行过 —— 但 POST user/register 可能**已经成功**，
        #    OpenAI 那边账号连同这个密码已经建好了，只是后续步骤（发码/验证/建账户）挂了。
        #    不打出来的话这个号就成了谁也登不进去的孤儿。这里只写日志不落库，
        #    避免把没有任何 token 的半成品塞进「注册结果」表里。
        try:
            _pw = (flow.result.password or "").strip()
            if _pw:
                logging.getLogger("registrar").error(
                    f"[register] 该号已生成密码，请自行留存: {flow.result.email or email} / {_pw}"
                )
        except Exception:
            pass  # flow 还没建出来（异常发生在 AuthFlow 之前），没密码可救
        if category != "account":
            logging.getLogger("registrar").error(traceback.format_exc())
        # 非池化 provider 没有号池记录，不操作
        if is_pooled:
            if category == "network":
                db.release_unused(email)
                logging.getLogger("registrar").warning(
                    f"[register] {email} 判定为网络/环境错误，号已 release 回 available"
                )
            elif category == "mail_limited":
                # ⚠️ 邮箱侧的固有限制（如短效邮箱已消耗），**不能 release**。
                #    短效邮箱的 serviceToken 是一次性的，邮件已经取过 ——
                #    release 回 available 只会让同一个邮箱再被领一次、再失败一次。
                #    mark_failed 把它标成不可用，号池会自动跳过。
                db.mark_failed(email, f"[{category}] {err}")
                logging.getLogger("registrar").warning(
                    f"[register] {email} 判定为邮箱侧限制（短效邮箱已消耗），号废弃不复用"
                )
            else:
                db.mark_failed(email, f"[{category}] {err}")

        # 「单次注册」预占的代理配额在这里归还 —— 失败不占配额。
        # （自动跑号的预占由 auto_loop 归还，不走这里。）
        if options.get("proxy_reserved"):
            _p = str(options.get("proxy") or "").strip()
            if _p:
                db.proxy_release(_p)
                db.proxy_used(_p, ok=False)   # 只记失败次数

        # Remail：记一下这次失败原因。
        # attempt_count 在 provider 取邮箱时已经加过了，这里只补失败原因，
        # 方便在「邮箱服务」页看出这个邮箱为什么反复失败（是超时还是 409）。
        _order_no = str(getattr(mail, "_order_no", "") or "").strip()
        if _order_no:
            try:
                db.mark_remail_order_attempt(_order_no, ok=False, error=err[:200])
                logging.getLogger("registrar").warning(
                    f"[register] Remail 订单 {_order_no[:16]} 本次注册失败: {err[:120]}"
                )
            except Exception as e:
                logging.getLogger("registrar").warning(
                    f"[register] 记 Remail 订单失败原因出错: {e}"
                )

        db.finish_run(run_id, "failed", err, category=category)
        _emit_status(run_id, "error", {"message": err, "category": category})

    finally:
        # env 覆盖现在只挂在 AuthFlow 实例上，随实例一起回收，无需还原。
        # 关闭 handler
        try:
            root_logger.removeHandler(handler)
            handler.close()
        except Exception:
            pass
        q = _run_queues.get(run_id)
        if q is not None:
            q.put(None)  # sentinel: 流结束
        # 线程标记清掉。理论上线程跑完就回收了，但 threading.local 是绑在
        # 线程对象上的，万一以后换成线程池复用线程，残留的 run_id 会让下一个
        # 任务的日志全被投递到上一个 run 的（已关闭的）文件里去。
        _current_run.run_id = None


def _try_export_to_panels(run_id: str, cred: dict) -> None:
    """注册完成后可选地把凭证导出到 CPA / SUB2API 面板。

    - 任一目标的"启用"开关关闭时,该目标跳过(不发请求);两者都未启用时整段 no-op。
    - 任何异常都不抛,只 emit 日志/状态(不影响注册主流程)。
    """
    try:
        cfg = db.get_export_internal_config()
    except Exception as e:
        logging.getLogger("registrar").warning(f"[export] 读取配置失败: {e}")
        return

    cpa_enabled = bool(cfg.get("cpa", {}).get("enabled"))
    sub2api_enabled = bool(cfg.get("sub2api", {}).get("enabled"))
    if not (cpa_enabled or sub2api_enabled):
        return  # 用户没勾选任何目标 → 完全不执行

    from ..services import exporter  # lazy import avoids panel dependencies at startup

    explog = logging.getLogger("registrar")

    def _log(msg: str, level: str = "info") -> None:
        if level == "error":
            explog.error(f"[export] {msg}")
        elif level == "warn":
            explog.warning(f"[export] {msg}")
        else:
            explog.info(f"[export] {msg}")
        try:
            _emit_status(run_id, "phase", {"phase": "export", "message": msg, "level": level})
        except Exception:
            pass

    try:
        results = exporter.run_exports(
            cred,
            cpa_cfg=cfg.get("cpa") if cpa_enabled else None,
            sub2api_cfg=cfg.get("sub2api") if sub2api_enabled else None,
            log_fn=_log,
        )
    except Exception as e:
        _log(f"导出整体异常: {e}", "error")
        return

    # 汇总成一个事件给前端
    summary = {}
    if results.get("cpa") is not None:
        summary["cpa"] = {"ok": bool(results["cpa"].get("ok")),
                          "message": results["cpa"].get("message") or results["cpa"].get("error") or ""}
    if results.get("sub2api") is not None:
        summary["sub2api"] = {"ok": bool(results["sub2api"].get("ok")),
                              "message": results["sub2api"].get("message") or results["sub2api"].get("error") or ""}
    try:
        _emit_status(run_id, "phase", {"phase": "export_done", "summary": summary})
    except Exception:
        pass


def _check_plan_after_registration(run_id: str, cred: dict) -> dict:
    """Query plan/trial after saving credentials without affecting registration."""
    email = str(cred.get("email") or "").strip().lower()
    access_token = str(cred.get("access_token") or "").strip()
    if not email or not access_token:
        return {"status": "no_at", "label": "无AT"}

    _emit_status(
        run_id,
        "phase",
        {"phase": "checking_plan", "email": email},
    )
    from ..services.plan_check import check_registered_plan

    try:
        info = check_registered_plan(
            email,
            cred=cred,
            proxy_url=str(cred.get("proxy") or ""),
            device_id=str(cred.get("device_id") or ""),
        )
    except Exception as exc:
        logging.getLogger("registrar").warning(
            "[register] 套餐查询异常（账号仍有效）: %s", exc
        )
        info = {"status": "error", "label": "查询失败"}
    _emit_status(
        run_id,
        "phase",
        {
            "phase": "plan_checked",
            "email": email,
            "plan": info.get("current_plan_type") or "",
            "plan_label": info.get("plan_label") or "",
            "status": info.get("status"),
            "label": info.get("label"),
        },
    )
    logging.getLogger("registrar").info(
        "[register] 套餐查询 email=%s plan=%s plus_trial=%s status=%s",
        email,
        info.get("current_plan_type") or "unknown",
        info.get("plus_trial_eligible"),
        info.get("status"),
    )
    return info


def _save_password_early(email: str, password: str) -> None:
    """AuthFlow 的 on_password 回调：密码在 OpenAI 侧一生效就落盘。

    以前密码只在流程**全部**跑通后才随 save_registered 一起入库，
    中间任何一步失败（实测最常见的是 OTP 超时）密码就只剩一行 ERROR 日志兜底 ——
    换台机器、日志轮转、或者干脆没人去翻，号就废了。

    这里存的是"有密码、无凭证"的半成品行，跑通后 save_registered 会用
    同一个 email 主键覆盖补全，不会多出一行对不上的记录。
    """
    log = logging.getLogger("registrar")
    try:
        db.save_password_early(email, password)
        log.info(f"[register] 密码已落盘: {email}（凭证待补）")
    except Exception as e:
        # 落盘失败不能影响注册；下面 except 里那行 ERROR 日志仍然是兜底
        log.warning(f"[register] 密码落盘失败，仅剩日志兜底: {e}")


def _build_sms_callback(run_id: str) -> Optional[PhoneCallbackController]:
    """根据 webui 配置创建 SMS 接码 controller。

    未启用接码或未配置 API key 时返回 None，flow 会回退到环境变量路径。
    log_fn 把租号/等码的状态推到 SSE 流，前端可见。
    """
    cfg = db.get_sms_internal_config()
    if not cfg.get("sms_enabled"):
        return None
    api_key = (cfg.get("sms_api_key") or "").strip()
    if not api_key:
        logging.getLogger("registrar").warning("[sms] 已启用接码但未配置 sms_api_key，跳过")
        return None

    smslog = logging.getLogger("registrar")

    def _log(msg: str) -> None:
        # 既写日志、又通过 _emit_status 推 phase 事件给前端
        smslog.info(f"[sms] {msg}")
        try:
            _emit_status(run_id, "phase", {"phase": "sms", "message": msg})
        except Exception:
            pass

    try:
        return PhoneCallbackController(
            provider_key=cfg["sms_provider"],
            config=cfg,
            service=cfg.get("sms_service") or "openai",
            country=cfg.get("sms_country") or "52",
            log_fn=_log,
            auto_select_country=bool(cfg.get("sms_auto_country")),
        )
    except Exception as e:
        smslog.warning(f"[sms] 创建接码 controller 失败: {e}")
        return None


def _sms_ready() -> bool:
    """接码是否**真的可用**（不只是开关开了，还得有 API Key）。

    为什么单独抽这个函数：`_build_sms_callback` 在开关开但没配 Key 时
    也会返回 None。判断 Codex OAuth 值不值得跑，必须跟"controller 能否
    真正构建"对齐，否则会出现"以为能跑、实际拿不到"的白跑。

    跟 Codex 授权链的关系：授权落到 /add-phone 时，只有配了 SMS controller
    才推进得动（auth_flow._handle_add_phone_via_sms 的前置条件）。
    """
    try:
        cfg = db.get_sms_internal_config()
    except Exception:
        return False
    if not cfg.get("sms_enabled"):
        return False
    if not (cfg.get("sms_api_key") or "").strip():
        return False
    return True


def start_registration(account: dict, options: dict) -> str:
    """启动一次注册任务，返回 run_id。"""
    run_id = uuid.uuid4().hex[:12]
    log_file = LOG_DIR / f"{run_id}.log"
    db.create_run(run_id, account["email"], str(log_file))

    q: queue.Queue = queue.Queue()
    with _lock:
        _run_queues[run_id] = q

    th = threading.Thread(
        target=_do_register,
        args=(run_id, account, options, log_file),
        daemon=True,
        name=f"register-{run_id}",
    )
    th.start()
    return run_id


def get_run_queue(run_id: str) -> Optional[queue.Queue]:
    return _run_queues.get(run_id)


def remove_run_queue(run_id: str) -> None:
    with _lock:
        _run_queues.pop(run_id, None)
