"""Create configured SMS providers."""

from __future__ import annotations

from .base import BaseSmsProvider
from .api668 import Api668Provider
from .smsbower import (
    SMS_DEFAULT_COUNTRY,
    _safe_bool,
    _safe_float,
    _safe_int,
    SmsBowerProvider,
)


def create_sms_provider(provider_key: str, config: dict) -> BaseSmsProvider:
    """从配置创建 provider 实例。

    provider_key: smsbower / herosms / api668
    config 字段：sms_api_key / sms_country / sms_service / sms_max_price /
                sms_reuse_phone / sms_phone_success_max
    """
    pk = (provider_key or "").lower().strip()
    api_key = str(config.get("sms_api_key") or "").strip()
    if not api_key:
        raise RuntimeError(f"{pk} 未配置 API Key")
    country = str(config.get("sms_country") or "").strip()
    service = str(config.get("sms_service") or "").strip() or "dr"
    # 接码 API 请求走的代理：复用全局 proxy（registrar 注入注册流程的代理），
    # 也允许调用方显式传 sms_proxy 覆盖（保留扩展点，目前 WebUI 不暴露）。
    proxy = (str(config.get("sms_proxy") or config.get("proxy") or "")).strip() or None
    max_price = _safe_float(config.get("sms_max_price"), -1)
    fixed_price = _safe_float(config.get("sms_fixed_price"), -1)
    reuse = _safe_bool(config.get("sms_reuse_phone"), False)
    succ_max = max(0, _safe_int(config.get("sms_phone_success_max"), 3))

    if pk in ("smsbower", "sms_bower"):
        return SmsBowerProvider(api_key=api_key,
                                default_service=service,
                                default_country=country or SMS_DEFAULT_COUNTRY,
                                max_price=max_price,
                                fixed_price=fixed_price,
                                proxy=proxy,
                                reuse_phone_to_max=reuse,
                                phone_success_max=succ_max)
    if pk in ("herosms", "hero_sms"):
        return SmsBowerProvider(api_key=api_key,
                                base_url="https://hero-sms.com/stubs/handler_api.php",
                                default_service=service,
                                default_country=country or SMS_DEFAULT_COUNTRY,
                                max_price=max_price,
                                fixed_price=fixed_price,
                                proxy=proxy,
                                reuse_phone_to_max=reuse,
                                phone_success_max=succ_max)
    if pk in ("api668", "api_668", "668"):
        return Api668Provider(
            api_key=api_key,
            base_url=str(config.get("sms668_base_url") or ""),
            default_service=service,
            default_country=country,
            prefix=str(config.get("sms668_prefix") or "").strip(),
            auth_scheme=str(config.get("sms668_auth_scheme") or "raw"),
            proxy=proxy,
        )
    raise RuntimeError(f"未知接码服务: {provider_key}")


