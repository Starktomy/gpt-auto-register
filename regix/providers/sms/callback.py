"""Two-phase phone verification callback controller."""

from __future__ import annotations

import logging
from typing import Callable, Optional

from .base import BaseSmsProvider, SmsActivation
from .countries import SMS_COUNTRY_NAMES_CN
from .factory import create_sms_provider
from .smsbower import SMS_DEFAULT_COUNTRY, SmsBowerProvider, _SMS_VERIFY_LOCK

logger = logging.getLogger(__name__)


class PhoneCallbackController:
    """把 SMS provider 包装成两阶段回调，注入到 auth_flow.add_phone 流程。

    用法（在 auth_flow._handle_add_phone_verification 里）：
        controller = PhoneCallbackController(...)
        phone = controller.get_phone()         # 阶段1：租号
        flow._add_phone_send(phone)
        ...
        code = controller.get_code()           # 阶段2：等 SMS 验证码
        flow._phone_otp_validate(code)
        controller.report_success()            # 成功
        # 失败时 controller.cancel() / mark_code_failed()
    """

    def __init__(
        self,
        provider_key: str,
        config: dict,
        *,
        service: str = "openai",
        country: str = "",
        log_fn: Optional[Callable[[str], None]] = None,
        auto_select_country: bool = False,
    ):
        self.provider_key = provider_key
        self.config = dict(config or {})
        self.service = service
        self.country = country
        self.log = log_fn or logger.info
        self.auto_select_country = bool(auto_select_country)
        self.provider: Optional[BaseSmsProvider] = None
        self.activation: Optional[SmsActivation] = None
        self.completed = False
        self._verify_lock_acquired = False

    def _provider(self) -> BaseSmsProvider:
        if self.provider is None:
            self.provider = create_sms_provider(self.provider_key, self.config)
        return self.provider

    def get_phone(self) -> str:
        """阶段 1：租手机号（已带 +）。"""
        provider = self._provider()
        # 同号复用锁（SmsBower 系列才用，防止两个注册任务并发抢同一个 cache）
        if isinstance(provider, SmsBowerProvider) and not self._verify_lock_acquired:
            _SMS_VERIFY_LOCK.acquire()
            self._verify_lock_acquired = True

        # 收集候选国家列表：用户多选 > 自动选号选出的 best > 单一 country
        allowed_raw = str(self.config.get("sms_allowed_countries") or "").strip()
        allowed_list = [c.strip() for c in allowed_raw.replace(";", ",").split(",") if c.strip()]

        effective_country = self.country
        country_candidates: list[str] = []

        if self.auto_select_country and isinstance(provider, SmsBowerProvider):
            if allowed_list:
                self.log(f"🔍 自动选号: 从主人勾选的 {len(allowed_list)} 个国家依次尝试（按价格升序）")
                try:
                    rows = provider.get_top_countries(service=self.service)
                    # 按价格升序排，只保留在 allowed_list 中的
                    in_allow = [r for r in rows if str(r.get("country") or "") in allowed_list]
                    ordered_allowed = [str(r["country"]) for r in in_allow]
                    # 把 allowed 里没在排名中出现的也加在最后
                    appended = [c for c in allowed_list if c not in ordered_allowed]
                    country_candidates = ordered_allowed + appended
                    self.log(f"  候选顺序: {','.join(country_candidates)}")
                except Exception as e:
                    self.log(f"  排名查询失败({e})，按主人勾选的原始顺序尝试")
                    country_candidates = list(allowed_list)
            else:
                # 未多选时，单纯按价格选最便宜（默认非严格白名单）
                self.log("🔍 自动选号（未指定允许国家，按全平台价格+库存挑最优）...")
                try:
                    best = provider.get_best_country(
                        service=self.service,
                        min_stock=_safe_int(self.config.get("sms_auto_min_stock"), 20),
                        max_price=_safe_float(self.config.get("sms_auto_max_price"), 0),
                        strict_whitelist=_safe_bool(self.config.get("sms_strict_whitelist"), False),
                    )
                    if best:
                        name_cn = SMS_COUNTRY_NAMES_CN.get(best, "未知")
                        in_wl = best in OPENAI_SMS_COUNTRIES
                        wl_label = "✅ OpenAI SMS 白名单" if in_wl else "⚠️ 非白名单"
                        self.log(f"✅ 自动选择国家: {best} {name_cn}  [{wl_label}]")
                        country_candidates = [best]
                    else:
                        self.log("⚠️ 未找到满足条件的国家，使用默认 country")
                        country_candidates = [self.country] if self.country else []
                except Exception as e:
                    self.log(f"⚠️ 国家智能选择失败({e})，使用默认 country")
                    country_candidates = [self.country] if self.country else []
        else:
            # 没启用自动选号 → 强制用默认国家
            country_candidates = [self.country] if self.country else []

        if not country_candidates:
            country_candidates = [SMS_DEFAULT_COUNTRY]

        country_label_log = ",".join(
            f"{c}({SMS_COUNTRY_NAMES_CN.get(c, '?')})" for c in country_candidates[:5]
        )
        self.log(f"📱 准备租号: provider={self.provider_key} service={self.service} 候选={country_label_log}{' ...' if len(country_candidates) > 5 else ''}")
        try:
            self.activation = provider.get_number(
                service=self.service,
                country=country_candidates[0],
                country_candidates=country_candidates,
            )
        except Exception as exc:
            self._release_lock()
            raise

        reused = bool((self.activation.metadata or {}).get("reused"))
        used_country = self.activation.country or country_candidates[0]
        used_country_label = f"{used_country} {SMS_COUNTRY_NAMES_CN.get(used_country, '')}"
        self.log(f"✅ 已租到号码{'(复用)' if reused else ''}: {self.activation.phone_number} "
                 f"国家={used_country_label} (activation_id={self.activation.activation_id})")
        return self.activation.phone_number

    def get_code(self, timeout: int = 180) -> str:
        """阶段 2：等待 SMS 验证码。"""
        if not self.activation:
            raise RuntimeError("PhoneCallbackController: 未先 get_phone")
        provider = self._provider()
        self.log(f"⏳ 等待 SMS 验证码... (activation_id={self.activation.activation_id} timeout={timeout}s)")
        code = provider.get_code(self.activation.activation_id, timeout=timeout)
        if code:
            self.log(f"✅ 收到 SMS 验证码: {code}")
            if getattr(provider, "auto_report_success_on_code", True):
                self.report_success()
        else:
            self.log(f"⚠️ 未收到 SMS 验证码: activation_id={self.activation.activation_id}")
        return code

    def report_success(self) -> None:
        if self.activation and self.provider and not self.completed:
            try:
                self.provider.report_success(self.activation.activation_id)
            except Exception as e:
                logger.warning("report_success 失败: %s", e)
            self.completed = True
            self.log(f"🎉 已标记号码成功完成: activation_id={self.activation.activation_id}")
        self._release_lock()

    def mark_code_failed(self, reason: str = "") -> None:
        if self.activation and self.provider:
            try:
                self.provider.mark_code_failed(self.activation.activation_id, reason=reason)
            except Exception:
                pass

    def mark_send_succeeded(self) -> None:
        if self.activation and self.provider:
            try:
                self.provider.mark_send_succeeded(self.activation.activation_id)
            except Exception:
                pass

    def mark_send_failed(self, reason: str = "") -> None:
        if self.activation and self.provider:
            try:
                self.provider.mark_send_failed(self.activation.activation_id, reason=reason)
            except Exception:
                pass

    def set_resend_callback(self, callback: Optional[Callable[[], None]]) -> None:
        try:
            self._provider().set_resend_callback(callback)
        except Exception:
            pass

    def cleanup(self) -> None:
        """流程结束（成功或失败）调用：释放未完成的号、解锁。"""
        if self.activation and not self.completed and self.provider:
            try:
                self.provider.cancel(self.activation.activation_id)
                self.log(f"🗑️ 已释放未使用号码: activation_id={self.activation.activation_id}")
            except Exception:
                pass
        self._release_lock()

    def _release_lock(self) -> None:
        if self._verify_lock_acquired:
            try:
                _SMS_VERIFY_LOCK.release()
            except RuntimeError:
                pass
            self._verify_lock_acquired = False


# ---------------------------------------------------------------------------
# 简单 CLI 测试
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("Usage: python -m regix.providers.sms.callback <provider_key> <api_key> [country]")
        sys.exit(1)
    pk = sys.argv[1]
    key = sys.argv[2]
    cc = sys.argv[3] if len(sys.argv) > 3 else ""
    p = create_sms_provider(pk, {"sms_api_key": key, "sms_country": cc})
    print(f"余额: {p.get_balance()}")
