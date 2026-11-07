"""Mail provider, mail settings, and Remail order API routes."""


from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ...providers.mail import (
    MailProviderError,
    create_mail_provider,
    get_provider_class,
    list_pooled_providers,
    list_providers,
)
from ...storage import facade as db

router = APIRouter()


# ──────────────────────── 邮箱来源配置 ────────────────────────


@router.get("/api/mail/providers")
def api_mail_providers(pooled_only: bool = False):
    """列出所有已注册的邮箱 provider 及其能力 / 配置项声明。

    前端据此渲染「邮箱来源」单选和对应的动态表单 ——
    以后加邮箱，前端一行都不用改。

        pooled_only=true  只返回能导入号池的（导入页用）
    """
    return {
        "ok": True,
        "providers": list_pooled_providers() if pooled_only else list_providers(),
        "current": db.get_setting("mail_source", "outlook"),
    }


@router.get("/api/settings/mail")
def api_get_mail_config():
    return {"ok": True, "config": db.get_mail_config()}


class SaveMailConfigReq(BaseModel):
    """字段不再写死。

    mail_source 之外的配置项由各 provider 的 config_fields 声明，
    前端原样回传，db.save_mail_config 按声明逐项存 ——
    加 provider 时这个模型不用动。
    """

    model_config = {"extra": "allow"}

    mail_source: Optional[str] = None


@router.post("/api/settings/mail")
def api_save_mail_config(req: SaveMailConfigReq):
    try:
        db.save_mail_config(req.model_dump(exclude_none=True))
    except MailProviderError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "config": db.get_mail_config()}


@router.post("/api/settings/mail/test")
def api_test_mail():
    """测试当前邮箱来源的连通性，具体怎么测由 provider 的 self_test() 决定。

    原来这里写死了 CF 的 api_url/domain/token 三个字段，
    换成让 provider 自检 —— 加邮箱不用回来改这个路由。
    """
    mail_source = db.get_setting("mail_source", "outlook")
    try:
        provider_cls = get_provider_class(mail_source)
    except MailProviderError as e:
        raise HTTPException(400, str(e))

    # 池化 provider 的连通性绑定在具体某个号上，没号可测 ——
    # 它的"测试"就是导入时的格式校验 + 跑一次注册。
    if provider_cls.pooled:
        raise HTTPException(
            400,
            f"{provider_cls.display_name} 是号池类型，不需要单独测试；"
            f"导入时会校验格式",
        )

    try:
        provider = create_mail_provider(mail_source, db.get_mail_settings())
    except MailProviderError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(400, f"构造 {provider_cls.display_name} 失败: {e}")

    try:
        result = provider.self_test()
    except Exception as e:
        raise HTTPException(500, f"连接失败: {e}")
    if not result.get("ok"):
        raise HTTPException(500, result.get("message") or "连接失败")
    return {"ok": True, "message": result.get("message", "连接成功")}


# ──────────────────────── Remail 订单（邮箱服务） ────────────────────────
#
# Remail 不交付账号密码，只交付「邮箱地址 + serviceToken」这一对取件凭证。
# 下单时全部写进本地 remail_orders 表，这里所有操作都只读本地 —— 不回查
# Remail 的 projects / orders 接口。


@router.get("/api/mail/orders")
def api_mail_orders(limit: int = 200, offset: int = 0):
    """本机记录的 Remail 订单列表。"""
    items = db.list_remail_orders(limit=limit, offset=offset)
    return {"ok": True, "items": items, "total": len(items)}


class RemailOrderReq(BaseModel):
    suffix: Optional[str] = Field(None, description="邮箱后缀，留空用配置里的默认值")
    project_id: Optional[int] = Field(None, description="项目 ID，留空用配置里的")
    service_mode: Optional[str] = Field(None, description="purchase / code")
    supply_policy: Optional[str] = Field(None, description="private_first / public_only")


@router.post("/api/mail/orders")
def api_create_mail_order(req: RemailOrderReq):
    """手动下一单（测试用 / 补单）。

    走的是和注册流程完全一样的 provider.create_mailbox()，
    所以订单会落在同一张表里，行为一致。
    """
    try:
        provider = create_mail_provider("remail", db.get_mail_settings())
    except Exception as e:
        raise HTTPException(400, f"构造 Remail provider 失败: {e}")

    # 允许这一单覆盖配置里的后缀 / 项目 / 模式
    for attr, value in (
        ("email_suffix", req.suffix),
        ("project_id", req.project_id),
        ("service_mode", req.service_mode),
        ("supply_policy", req.supply_policy),
    ):
        if value not in (None, ""):
            setattr(provider, attr, value)

    try:
        email = provider.create_mailbox()
    except Exception as e:
        raise HTTPException(400, f"下单失败: {e}")
    return {"ok": True, "email": email, "items": db.list_remail_orders()}


@router.get("/api/mail/orders/{order_no}/mails")
def api_mail_order_mails(order_no: str):
    """对某个订单取件 —— 这就是「长效订单可以重复收件」的入口。

    订单里的 serviceToken 是取件凭证，这里从本地表读，不回查服务端。
    """
    order = db.get_remail_order(order_no, with_token=True)
    if not order:
        raise HTTPException(404, "订单不存在")
    token = order.get("service_token") or ""
    if not token:
        raise HTTPException(400, "该订单没有 serviceToken，无法取件")

    try:
        provider = create_mail_provider("remail", db.get_mail_settings())
        mails = provider.fetch_mails(order.get("email") or "", token)
    except Exception as e:
        raise HTTPException(500, f"取件失败: {e}")

    # 只回必要字段。邮件正文可能很长，前端只展示摘要 + 验证码。
    items = []
    for m in mails:
        items.append({
            "id": m.get("id"),
            "sender": m.get("sender") or "",
            "subject": m.get("subject") or "",
            "receivedAt": m.get("receivedAt") or "",
            "verificationCode": m.get("verificationCode") or "",
            "bodyPreview": (m.get("bodyPreview") or "")[:300],
        })
    return {"ok": True, "email": order.get("email") or "", "items": items}


class LinkOrderReq(BaseModel):
    email: str = Field(..., description="关联到的本地账号邮箱")


@router.post("/api/mail/orders/{order_no}/link")
def api_link_mail_order(order_no: str, req: LinkOrderReq):
    """把订单关联到本地注册成功的账号。"""
    ok = db.link_remail_order(order_no, req.email)
    if not ok:
        raise HTTPException(404, "订单不存在")
    return {"ok": True, "items": db.list_remail_orders()}


class OrderDisabledReq(BaseModel):
    disabled: bool = Field(True, description="True=禁用，False=启用")


@router.post("/api/mail/orders/{order_no}/disabled")
def api_mail_order_disabled(order_no: str, req: OrderDisabledReq):
    """本地禁用 / 启用一笔订单。

    禁用的订单不会再被自动复用（但仍然可以手动取件 —— 邮箱还在，钱已经花了）。
    只改本地标记，不动 Remail 服务端的状态。
    """
    ok = db.set_remail_order_disabled(order_no, req.disabled)
    if not ok:
        raise HTTPException(404, "订单不存在")
    return {"ok": True, "items": db.list_remail_orders()}


@router.delete("/api/mail/orders/{order_no}")
def api_delete_mail_order(order_no: str):
    """删除本地订单记录。

    ⚠️ 只删本地记录，不会去 Remail 退单 —— 退款要走服务端的售后流程。
    """
    ok = db.delete_remail_order(order_no)
    if not ok:
        raise HTTPException(404, "订单不存在")
    return {"ok": True, "items": db.list_remail_orders()}


class BulkDeleteOrdersReq(BaseModel):
    order_nos: list[str] = Field(..., description="要删除的订单号列表")


@router.post("/api/mail/orders/bulk_delete")
def api_bulk_delete_mail_orders(req: BulkDeleteOrdersReq):
    """批量删除本地订单记录。"""
    n = db.delete_remail_orders(req.order_nos)
    return {"ok": True, "deleted": n, "items": db.list_remail_orders()}


@router.get("/api/mail/wallet")
def api_mail_wallet():
    """查 Remail 余额（透传 apikey/profile）。"""
    try:
        provider = create_mail_provider("remail", db.get_mail_settings())
    except Exception as e:
        raise HTTPException(400, f"构造 Remail provider 失败: {e}")
    try:
        resp = provider._request("GET", "/v1/open/apikey/profile", timeout=15)
    except Exception as e:
        raise HTTPException(500, f"查询余额失败: {e}")
    status = getattr(resp, "status_code", 0)
    if status != 200:
        raise HTTPException(500, f"查询余额失败: status={status}")
    data = provider._parse_json(resp)
    key = data.get("apiKey") if isinstance(data.get("apiKey"), dict) else data
    return {"ok": True, "balance": str(key.get("balance") or "0"),
            "enabled": key.get("enabled")}


