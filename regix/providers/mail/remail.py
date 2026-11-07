"""Remail 聚合邮箱 provider。

Remail（remail.aishop6.com）是一个多邮箱账户聚合平台：一个 API 就能切换
outlook / gmail / icloud / proto / 域名邮箱等不同来源。用法是「下单 → 拿邮箱
→ 接码」，每个注册任务下一个订单。

官方 OpenAPI：https://remail.aishop6.com/openapi.json（"ReMail 开放 API" v1.0.0）

本 provider 用到的 3 个端点：

  1) POST /v1/open/orders   统一下单
       query : serviceMode = purchase(长效可重复收件) | code(短效接码)
               supply     = private_first(优先自有库存) | public_only(仅公共库存)
       header: Authorization: Bearer rk-xxxx
               Idempotency-Key: <必填>
       body  : {"projectId": <int>, "emailSuffix": "<后缀>"}
       返回  : Order{deliveryEmail, serviceToken, orderNo, payAmount, ...}

  2) GET  /v1/pickup        取件读邮件
       query : email = 订单交付的完整地址, token = 订单返回的 serviceToken
       返回  : {items: [{verificationCode, bodyPreview, subject, receivedAt, ...}],
                fetch: {lastStatus, lastSafeError, nextFetchAllowedAt}}

  3) GET  /v1/open/apikey/profile   查 Key 状态与余额（self_test 用）

设计取舍：
  - **不查 projects / orders 接口**。下单时把邮箱类型、后缀、serviceToken 等
    全部写进本地 remail_orders 表（webui/db.py），后续取件、重复收件、看邮箱
    类型全读本地 —— 少一次网络往返，也避免服务端接口变动影响本地流程。
  - serviceToken 是**取件凭证**，官方要求不得记日志，这里只存内存 + 本地表，
    任何日志都不打它的值。
  - ⚠️ Remail **不交付账号密码**（官方原文：「购买与接码均不交付账号；资源密码、
    2FA、应用密码和上游令牌对外只写不读」）。如果 OpenAI 把这个地址判定为
    「已有账号」，会要求密码登录 → 必然失败且无解。所以后缀选什么直接决定
    成功率：自有域名 > gmail 变种 > outlook（Remail 本身是二手邮箱交易平台，
    OpenAI 命中率最高）。

能力声明：
  pooled     = False  运行时下单，不走本地号池
  ephemeral  = True   每次下单都是新地址 → OpenAI 视为新号
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Optional

from .base import ConfigField, MailProvider, extract_otp, register

logger = logging.getLogger("mail.remail")

# 取件轮询间隔（秒）。Remail 侧建议不要过于频繁，FetchState.nextFetchAllowedAt
# 会给出下次可取的时间。
POLL_INTERVAL = 4

# 单次取件返回的邮件条数上限
PICKUP_LIMIT = 20


@register
class RemailMailProvider(MailProvider):
    """Remail 聚合邮箱 provider。

    用法：
        mail = RemailMailProvider(
            api_base="https://remail.aishop6.com",
            api_key="rk-xxxx",
            project_id=1001,
            email_suffix="outlook",
            service_mode="purchase",
            supply_policy="private_first",
        )
        auth_flow.run_register(mail)
    """

    kind = "remail"
    display_name = "Remail 聚合邮箱"
    pooled = False         # 运行时下单，不走号池
    ephemeral = True       # 每次新地址 → OpenAI 永远当新号

    line_segments = 0      # 不支持手动导入（地址是下单现造的）
    import_hint = ""
    import_placeholder = ""

    config_fields = [
        ConfigField(
            "remail_api_base", "API 地址",
            placeholder="https://remail.aishop6.com",
            help="Remail 开放 API 的地址，末尾不带斜杠",
        ),
        ConfigField(
            "remail_api_key", "API Key（oauth2 key）", type="password",
            help="Remail 控制台生成的 rk- 开头密钥，等同于 Bearer 认证",
        ),
        ConfigField(
            "remail_project_id", "项目 ID", type="number",
            help="Remail「项目」列表中用于 OpenAI 验证码的 projectId",
        ),
        ConfigField(
            "remail_email_suffix", "邮箱后缀",
            placeholder="outlook / outlook.com / gmail_variant / domain",
            help="填 outlook 则在微软白名单后缀中按库存加权随机；"
                 "填具体后缀（如 outlook.com）则精确匹配该库存。"
                 "指定后缀缺货时不会自动切换，会直接失败。",
        ),
        ConfigField(
            "remail_service_mode", "服务模式", type="select",
            options=["purchase", "code"],
            help="purchase = 长效购买（可重复收件）；code = 短效接码（一次性）",
        ),
        ConfigField(
            "remail_supply_policy", "库存策略", type="select",
            options=["private_first", "public_only"],
            help="private_first = 优先用自有库存，没有才用公共库存；public_only = 只用公共库存",
        ),
    ]

    def __init__(
        self,
        api_base: str,
        api_key: str,
        project_id,
        email_suffix: str = "outlook",
        service_mode: str = "purchase",
        supply_policy: str = "private_first",
        session=None,
    ):
        if not api_base:
            raise ValueError("api_base 不能为空")
        if not api_key:
            raise ValueError("api_key 不能为空")
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key.strip()
        self.project_id = project_id
        self.email_suffix = (email_suffix or "outlook").strip()
        self.service_mode = (service_mode or "purchase").strip().lower()
        self.supply_policy = (supply_policy or "private_first").strip().lower()

        # 短效（code）订单 = 一次性邮箱。
        #
        # ⚠️ 为什么短效要区别对待（详见 base.MailProvider.ephemeral_mailbox）：
        #    短效订单的 serviceToken 是一次性取件凭证，平台**只保留一封**邮件；
        #    而 OpenAI 注册流程在「设密码」后会切页面，服务端自动发的那封 OTP
        #    立即失效 → 必须补发第 2 封 → 短效收不到 → 死锁。
        #    所以短效模式要跳过设密码（第 1 封保持有效），且取件时间窗要放宽。
        self.ephemeral_mailbox = (self.service_mode == "code")

        # 当前这一单的信息（一次注册任务 = 一个 provider 实例）
        self._email: str = ""
        self._service_token: str = ""
        self._order_no: str = ""
        self._seen_mail_ids: set = set()

        self._session = session

    # ──────────────────────── 构造入口 ────────────────────────

    @classmethod
    def from_config(cls, settings: dict, account: Optional[dict] = None):
        api_base = (settings.get("remail_api_base") or "").strip()
        api_key = (settings.get("remail_api_key") or "").strip()
        project_id = (settings.get("remail_project_id") or "").strip()
        suffix = (settings.get("remail_email_suffix") or "outlook").strip()
        mode = (settings.get("remail_service_mode") or "purchase").strip()
        supply = (settings.get("remail_supply_policy") or "private_first").strip()

        missing = []
        if not api_base:
            missing.append("API 地址")
        if not api_key:
            missing.append("API Key")
        if not project_id:
            missing.append("项目 ID")
        if missing:
            raise RuntimeError(
                f"Remail 未配置完整（缺 {'、'.join(missing)}），"
                f"请去「邮箱配置」页填写"
            )
        try:
            pid = int(str(project_id).strip())
        except (TypeError, ValueError):
            raise RuntimeError(f"Remail 项目 ID 必须是整数，当前是 {project_id!r}")

        return cls(
            api_base=api_base,
            api_key=api_key,
            project_id=pid,
            email_suffix=suffix,
            service_mode=mode,
            supply_policy=supply,
        )

    # ──────────────────────── HTTP ────────────────────────

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, *, params=None, json_body=None,
                 timeout: int = 20, extra_headers=None):
        """统一请求。curl_cffi 优先（复用项目已有的会话），urllib 兜底。"""
        url = f"{self.api_base}{path}"
        headers = self._headers()
        if extra_headers:
            headers.update(extra_headers)
        m = method.upper()

        sess = self._session
        if sess is None:
            try:
                from curl_cffi.requests import Session as CffiSession
                sess = CffiSession(impersonate="chrome136")
                sess.trust_env = False
                self._session = sess
            except ImportError:
                sess = None

        if sess is not None:
            try:
                if m == "GET":
                    return sess.get(url, headers=headers, params=params, timeout=timeout)
                return sess.post(
                    url, headers=headers,
                    data=json.dumps(json_body, separators=(",", ":")) if json_body is not None else None,
                    params=params, timeout=timeout,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[remail] curl_cffi 请求异常，回退 urllib: {e}")

        # urllib 兜底
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        body = json.dumps(json_body).encode() if json_body is not None else None
        req = urllib.request.Request(url, data=body, headers=headers, method=m)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                text = r.read().decode("utf-8", errors="replace")
                r.status_code = r.status
                r.text = text
                r.json = lambda: json.loads(text)
                return r
        except urllib.error.HTTPError as e:
            text = ""
            try:
                text = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            e.status_code = e.code
            e.text = text
            e.json = lambda: json.loads(text or "{}")
            return e

    @staticmethod
    def _parse_json(resp) -> dict:
        try:
            fn = getattr(resp, "json", None)
            data = fn() if callable(fn) else json.loads(resp.text)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _error_text(resp) -> str:
        """把 Remail 的错误响应压成一行可读文本。"""
        data = RemailMailProvider._parse_json(resp)
        for key in ("message", "error", "detail", "code"):
            val = data.get(key)
            if val:
                return str(val)[:200]
        return (getattr(resp, "text", "") or "")[:200]

    # ──────────────────────── 下单 ────────────────────────

    def _try_reuse(self) -> Optional[str]:
        """尝试复用一个已经付过钱、但还没关联账号的长效订单。

        为什么要有这一步：长效（purchase）订单是**已经付过钱**的邮箱。
        上一次注册失败（OTP 超时 / 409 / 风控）之后，那个邮箱本身还是好的 ——
        换个号重新注册完全可以，不该再花一次钱买新的。

        返回复用的邮箱地址；没有可复用的返回 None（走正常下单）。
        """
        try:
            from regix.storage import facade as db
            order = db.claim_reusable_remail_order(
                service_mode=self.service_mode,
                suffix=self.email_suffix,
                project_id=self.project_id,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[remail] 查可复用订单失败（改走新下单）: {e}")
            return None

        if not order:
            return None

        email = str(order.get("email") or "").strip()
        token = str(order.get("service_token") or "").strip()
        if not email or not token:
            return None

        self._email = email
        self._service_token = token
        self._order_no = str(order.get("order_no") or "")
        self._seen_mail_ids = set()
        # 这次尝试先记上：万一这单又失败了，attempt_count 涨上去，
        # 超过上限就不再复用它（否则会死循环卡在一个坏邮箱上）。
        try:
            from regix.storage import facade as db
            db.mark_remail_order_attempt(self._order_no, ok=True)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[remail] 记尝试次数失败（不影响复用）: {e}")

        logger.info(
            f"[remail] ♻️ 复用已购长效邮箱 email={email} "
            f"order={self._order_no[:16] or '-'}（已付费，不重复下单）"
        )
        return email

    def create_mailbox(self) -> str:
        """拿一个邮箱：优先复用已购未用的长效订单，没有才下单。

        Idempotency-Key 是**必填**的（官方 OpenAPI 标注 required），
        用 uuid4 保证同一单不会因重试被重复扣费。
        """
        reused = self._try_reuse()
        if reused:
            return reused

        body = {
            "projectId": int(self.project_id),
            "emailSuffix": self.email_suffix,
        }
        params = {
            "serviceMode": self.service_mode,
            "supply": self.supply_policy,
        }
        headers = {"Idempotency-Key": uuid.uuid4().hex}

        resp = self._request(
            "POST", "/v1/open/orders",
            params=params, json_body=body, timeout=25, extra_headers=headers,
        )
        status = getattr(resp, "status_code", 0)
        if status not in (200, 201):
            raise RuntimeError(
                f"Remail 下单失败: status={status} "
                f"{self._error_text(resp)} "
                f"(projectId={self.project_id} suffix={self.email_suffix})"
            )

        data = self._parse_json(resp)
        email = str(data.get("deliveryEmail") or "").strip()
        token = str(data.get("serviceToken") or "").strip()
        order_no = str(data.get("orderNo") or "").strip()

        if not email:
            raise RuntimeError(f"Remail 下单响应缺 deliveryEmail: {data}")
        if not token:
            # 没有 token 就取不到件，必须当场失败，别等到等码时才发现
            raise RuntimeError(f"Remail 下单响应缺 serviceToken（订单 {order_no}）")

        self._email = email
        self._service_token = token
        self._order_no = order_no
        self._seen_mail_ids = set()

        # 记到本地订单表 —— 后续取件 / 重复收件 / 看邮箱类型都读这里
        try:
            from regix.storage import facade as db
            db.save_remail_order({
                "order_no": order_no,
                "email": email,
                "suffix": self.email_suffix,
                "service_mode": self.service_mode,
                "supply_policy": self.supply_policy,
                "project_id": int(self.project_id),
                "order_status": str(data.get("status") or ""),
                "service_token": token,
                "pay_amount": str(data.get("payAmount") or ""),
                "received_at": time.time(),
                "receive_started_at": str(data.get("receiveStartedAt") or ""),
                "after_sale_until": str(data.get("afterSaleUntil") or ""),
                # 新订单马上要拿去注册 —— 先记一次尝试。
                # 万一这单又失败了，attempt_count 涨上去，超过上限就不再复用它。
                "attempt_count": 1,
            })
        except Exception as e:  # noqa: BLE001
            # 落库失败不能拖垮注册（内存里还有 token，本轮照样能收件）
            logger.warning(f"[remail] 订单落库失败（不影响本轮注册）: {e}")

        logger.info(
            f"[remail] 下单成功 order={order_no or '-'} email={email} "
            f"后缀={self.email_suffix} 模式={self.service_mode} "
            f"库存={self.supply_policy} 付费={data.get('payAmount') or '-'}"
        )
        return email

    # ──────────────────────── 取件 ────────────────────────

    def _pickup(self, email_addr: str, token: str) -> dict:
        """拉一批评件。返回 {items: [...], fetch: {...}}。"""
        resp = self._request(
            "GET", "/v1/pickup",
            params={"email": email_addr, "token": token},
            timeout=20,
        )
        status = getattr(resp, "status_code", 0)
        if status != 200:
            raise RuntimeError(
                f"Remail 取件失败: status={status} {self._error_text(resp)}"
            )
        data = self._parse_json(resp)
        items = data.get("items")
        if not isinstance(items, list):
            items = []
        fetch = data.get("fetch") if isinstance(data.get("fetch"), dict) else {}
        return {"items": items, "fetch": fetch}

    @staticmethod
    def _mail_epoch(mail: dict) -> Optional[float]:
        """把邮件时间解析成 epoch 秒；解析不出来返回 None。

        Remail 的 receivedAt 是 ISO8601（带时区），按 RFC3339 解析。
        """
        raw = str(mail.get("receivedAt") or mail.get("received_at") or "").strip()
        if not raw:
            return None
        try:
            from datetime import datetime
            txt = raw.replace("Z", "+00:00")
            dt = datetime.fromisoformat(txt)
            if dt.tzinfo is None:
                from datetime import timezone
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except Exception:
            return None

    @staticmethod
    def _otp_of(mail: dict) -> Optional[str]:
        """从一封邮件里取 6 位验证码。

        优先用平台直出的 verificationCode（Remail 已经解析好了），
        拿不到再退回自己从 bodyPreview/subject 里抽。
        """
        vc = str(mail.get("verificationCode") or "").strip()
        if len(vc) == 6 and vc.isdigit():
            return vc
        raw = " ".join([
            str(mail.get("subject") or ""),
            str(mail.get("bodyPreview") or ""),
        ])
        return extract_otp(raw)

    def peek_otp(
        self,
        email_addr: str,
        issued_after: Optional[float] = None,
        wait: float = 0.0,
    ) -> Optional[str]:
        """非破坏性预读：收件箱里已经有本轮的码就直接返回。

        语义见 base.MailProvider.peek_otp —— 不碰 _seen_mail_ids，
        issued_after 之前的信一律不认，探不到就安静返回 None。
        """
        email_addr = email_addr or self._email
        if not email_addr or not self._service_token:
            return None
        deadline = time.time() + max(0.0, float(wait))
        while True:
            try:
                for mail in self._pickup(email_addr, self._service_token)["items"]:
                    mid = str(mail.get("id") or "")
                    if mid and mid in self._seen_mail_ids:
                        continue
                    if issued_after is not None:
                        ts = self._mail_epoch(mail)
                        if ts is None or ts < issued_after - 2:
                            continue
                    otp = self._otp_of(mail)
                    if otp:
                        logger.info(f"[remail] 👀 预读命中 OTP（省掉一次发码）")
                        return otp
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[remail] peek 异常（当作没探到）: {e}")
            if time.time() >= deadline:
                return None
            time.sleep(1)

    def wait_for_otp(
        self,
        email_addr: str,
        timeout: int = 120,
        issued_after: Optional[float] = None,
        earliest: Optional[float] = None,
    ) -> str:
        """轮询取件直到拿到 6 位验证码。

        失败分三类，报错要能分清（决定用户下一步是"换个后缀"还是"再等等"）：
          - 平台明确说异常（fetch.lastStatus=abnormal）→ 立刻抛，不空等
          - HTTP 报错 → 立刻抛（凭证错 / 订单失效）
          - 单纯没收到 → 等到超时才抛

        issued_after：主动发码时刻，做防串号时间窗的下界。
        earliest：本次注册流程开始的时刻。

        ⚠️ **短效（code）订单必须用 earliest 而不是 issued_after**：
        短效只保留一封邮件，而那封是服务端在「提交邮箱」时自动发的 ——
        它比 issued_after（主动补发时刻）早十几秒到达（中间隔着设密码的 PoW，
        实测 16~21 秒）。用 issued_after 会把**唯一那封**也滤掉 → 死等超时。
        长效（purchase）会收到 2 封，必须严格用 issued_after，
        否则可能取到已失效的第 1 封 → 409 invalid_state。
        """
        email_addr = email_addr or self._email
        if not email_addr or not self._service_token:
            raise RuntimeError("Remail 未下单，无法取件")

        # 短效模式：下界用「注册开始」；普通模式：下界用「主动发码时刻」。
        if self.ephemeral_mailbox and earliest is not None:
            lower_bound = float(earliest)
        elif issued_after is not None:
            lower_bound = float(issued_after)
        else:
            lower_bound = None

        deadline = time.time() + max(10, int(timeout))
        last_error = ""

        while time.time() < deadline:
            try:
                result = self._pickup(email_addr, self._service_token)
            except Exception as e:  # noqa: BLE001
                last_error = str(e)
                # 取件本身失败（凭证错 / 网络）→ 重试几次，持续失败就抛
                if "status=4" in last_error or "status=5" in last_error:
                    raise RuntimeError(f"Remail 取件失败: {last_error}") from e
                time.sleep(POLL_INTERVAL)
                continue

            fetch = result.get("fetch") or {}
            if fetch.get("lastStatus") == "abnormal":
                safe_err = str(fetch.get("lastSafeError") or "").strip()
                raise RuntimeError(
                    f"Remail 取件异常: {safe_error or '平台未给出原因'}"
                    f"（邮箱 {email_addr}）"
                )

            # ⚠️ 同一封验证码可能被 OpenAI 发**两封**（不同发件人，同一个码）：
            #    · otp@tm1.openai.com    ← authorize/continue 时服务端自动发的那封
            #    · noreply@tm.openai.com  ← 注册密码后主动 send_otp 发的那封
            #    两封内容完全一样（同一个 OTP X）。这不是重复发码，是服务端
            #    在流程切换时补发了一次。
            #
            #    长效模式：时间窗（lower_bound = 主动发码时刻）会把早于本次
            #    发码的那封排除掉，所以这里只可能取到"本次"那封。
            #    短效模式：只保留 1 封（且是服务端自动发的那封），
            #    时间窗下界放宽到「注册开始」，才能把它取到。
            #
            #    但仍然记录一下窗口内有几封、选的是哪封，便于排查。
            _window_mails: list[dict] = []
            for mail in result["items"]:
                mid = str(mail.get("id") or "")
                if mid and mid in self._seen_mail_ids:
                    continue
                if lower_bound is not None:
                    ts = self._mail_epoch(mail)
                    if ts is None or ts < lower_bound - 2:
                        continue
                otp = self._otp_of(mail)
                if otp:
                    if mid:
                        self._seen_mail_ids.add(mid)
                    _window_mails.append(mail)
                    logger.info(
                        f"[remail] 收到验证码（{email_addr}）"
                        f"｜本轮窗口内共 {len(_window_mails)} 封"
                        f"｜发件人 {mail.get('sender') or '(未知)'}"
                        + ("｜短效模式(放宽时间窗)" if self.ephemeral_mailbox else "")
                    )
                    return otp

            time.sleep(POLL_INTERVAL)

        raise TimeoutError(
            f"Remail OTP timeout for {email_addr}"
            + (f"（最后一次错误: {last_error}）" if last_error else "")
        )

    # ──────────────────────── 自检 ────────────────────────

    def self_test(self) -> dict:
        """连通性自检：Key 是否有效、余额够不够。

        故意**不查 projects 接口** —— 项目和库存信息以下单记录为准，
        这里只需要确认"能不能下单"：Key 有效 + 余额不为 0。
        """
        resp = self._request("GET", "/v1/open/apikey/profile", timeout=15)
        status = getattr(resp, "status_code", 0)
        if status != 200:
            return {
                "ok": False,
                "message": f"API Key 校验失败: status={status} {self._error_text(resp)}",
            }
        data = self._parse_json(resp)
        key = data.get("apiKey") if isinstance(data.get("apiKey"), dict) else data
        balance = str(key.get("balance") or "0").strip()
        enabled = key.get("enabled")
        if enabled is False:
            return {"ok": False, "message": "该 API Key 已被禁用"}
        try:
            bal = float(balance)
        except (TypeError, ValueError):
            bal = 0.0
        if bal <= 0:
            return {
                "ok": False,
                "message": f"API Key 有效，但余额为 {balance}，请先充值再下单",
            }

        return {
            "ok": True,
            "message": (
                f"Remail 连通正常 · 余额 {balance} · "
                f"后缀 {self.email_suffix} · 模式 {self.service_mode} · "
                f"库存策略 {self.supply_policy}"
            ),
        }

    # ──────────────────────── 号池语义 ────────────────────────

    @property
    def exhausted(self) -> bool:
        return False

    def mark_dead(self, reason: str = "") -> None:
        # 非池化 provider：没有"废号换下一个"的概念，只记日志
        logger.warning(f"[remail] mark_dead: {reason}")

    # ──────────────────────── 给「邮箱服务」页用 ────────────────────────

    def fetch_mails(self, email_addr: str, service_token: str) -> list[dict]:
        """按邮箱地址 + serviceToken 拉一批评件（重复收件用）。"""
        return self._pickup(email_addr, service_token)["items"]
