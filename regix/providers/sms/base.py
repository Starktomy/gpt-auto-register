"""SMS 接码 provider 抽象 + SmsBower 实现。

设计参考：asz798838958/GeniusFKoai 的 core/base_sms.py，但裁剪掉浏览器回调相关代码、
仅保留纯协议注册需要的两段流程：
    1) rent number    → provider.get_number(service=..., country=...)
    2) wait sms code  → provider.get_code(activation_id, timeout=...)
    3) 成功/失败       → provider.report_success / cancel / mark_code_failed

⚠️ 关键事实：OpenAI 自 2025 年起对大部分国家改用 WhatsApp 验证，**纯 SMS 路径目前只有
泰国（country_id=52）确认可用**。其它国家可能抽到 WhatsApp 号导致拿不到 SMS。
SmsBower 的 `auto_select_country=True` 会按价格 + 库存自动选号。
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 抽象基类
# ---------------------------------------------------------------------------


@dataclass
class SmsActivation:
    """一次手机号租用的句柄。"""
    activation_id: str
    phone_number: str          # E.164 格式，带 + 前缀
    country: str = ""
    metadata: dict = field(default_factory=dict)


class BaseSmsProvider(ABC):
    """接码 provider 抽象基类。"""

    auto_report_success_on_code = True  # True = 收到 code 即报成功；False = 等业务侧确认

    @abstractmethod
    def get_number(self, *, service: str, country: str = "",
                    country_candidates: Optional[list[str]] = None) -> SmsActivation:
        ...

    @abstractmethod
    def get_code(self, activation_id: str, *, timeout: int = 180) -> str:
        ...

    @abstractmethod
    def cancel(self, activation_id: str) -> bool:
        ...

    def get_balance(self) -> float:
        """查询余额（货币随平台）。"""
        raise NotImplementedError

    def report_success(self, activation_id: str) -> bool:
        """业务侧验证通过后调用，平台可能据此结算/允许复用。"""
        return True

    def mark_code_failed(self, activation_id: str, reason: str = "") -> None:
        """业务侧收到 code 但 validate 失败 → 请求 resend。"""
        return None

    def mark_send_failed(self, activation_id: str, reason: str = "") -> None:
        """业务侧拒绝该手机号（add-phone/send 返错）→ 停止复用。"""
        return None

    def mark_send_succeeded(self, activation_id: str) -> None:
        """业务侧已成功触发短信发送（add-phone/send 200）。"""
        return None

    def set_resend_callback(self, callback: Optional[Callable[[], None]]) -> None:
        """注册 resend 钩子（SmsBower 长等待时回调业务侧重新触发 OTP）。"""
        return None


# ---------------------------------------------------------------------------
# 国家 ID → 中文名映射（sms-activate.org 协议系，SmsBower 共用）
# ---------------------------------------------------------------------------

