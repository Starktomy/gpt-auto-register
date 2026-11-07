"""Text export, panel export, credentials, and Plus check routes."""


import base64
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ...services import export_formats
from ...storage import facade as db

router = APIRouter()
logger = logging.getLogger("webui")


@router.get("/api/registered/export/formats")
def api_export_formats():
    return {"ok": True, "formats": export_formats.list_formats()}


class ExportRegisteredReq(BaseModel):
    format: str = Field(..., description="Format id from /api/registered/export/formats")
    emails: Optional[list[str]] = Field(None, description="Emails to export")
    all: bool = Field(False, description="Export every account, ignoring emails")


@router.post("/api/registered/export")
def api_export_registered(req: ExportRegisteredReq):
    fmt = export_formats.get_format(req.format)
    if fmt is None:
        raise HTTPException(400, f"未知导出格式: {req.format}")

    if req.all:
        rows = db.list_registered_full(limit=100000)
    elif req.emails:
        rows = db.list_registered_by_emails(req.emails)
    else:
        raise HTTPException(400, "需要 emails 或 all=true")

    base = {
        "ok": True,
        "count": len(rows),
        "filename": fmt.filename,
        "label": fmt.label,
        "mode": fmt.mode,
        "mime": fmt.mime,
        "emails": [(row.get("email") or "") for row in rows],
    }
    if fmt.mode == "download":
        blob = export_formats.render_bytes(rows, fmt)
        return {
            **base,
            "b64": base64.b64encode(blob).decode("ascii"),
            "size": len(blob),
        }
    return {**base, "text": export_formats.render_text(rows, fmt)}


# ──────────────────────── 自动导出 (CPA / SUB2API) ────────────────────────


class SaveExportConfigReq(BaseModel):
    # CPA
    cpa_enabled: Optional[str] = None       # "0" / "1"
    cpa_url: Optional[str] = None
    cpa_mgmt_key: Optional[str] = None      # 传 '***' 表示不修改
    cpa_timeout: Optional[str] = None
    # SUB2API
    sub2api_enabled: Optional[str] = None
    sub2api_url: Optional[str] = None
    sub2api_api_key: Optional[str] = None   # '***' 不修改
    sub2api_group_ids: Optional[str] = None  # 逗号分隔，例 "2" 或 "1,2,3"
    sub2api_timeout: Optional[str] = None


@router.get("/api/settings/export")
def api_get_export_config():
    return {"ok": True, "config": db.get_export_config()}


@router.post("/api/settings/export")
def api_save_export_config(req: SaveExportConfigReq):
    db.save_export_config(req.model_dump(exclude_none=True))
    return {"ok": True, "config": db.get_export_config()}


class TestExportReq(BaseModel):
    target: str = Field(..., description="cpa 或 sub2api")


@router.post("/api/settings/export/test")
def api_test_export(req: TestExportReq):
    """测试 CPA / SUB2API 连通性。"""
    from ..services import exporter
    cfg = db.get_export_internal_config()
    target = (req.target or "").strip().lower()
    try:
        if target == "cpa":
            return exporter.test_cpa(cfg["cpa"])
        if target == "sub2api":
            return exporter.test_sub2api(cfg["sub2api"])
        raise HTTPException(400, f"未知 target: {target}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"测试失败: {e}")


class ManualExportReq(BaseModel):
    email: str = Field(..., description="要导出的已注册账号邮箱")
    targets: list[str] = Field(default_factory=lambda: ["cpa", "sub2api"],
                                description="选择导出目标：cpa / sub2api")


@router.post("/api/registered/export_to_panel")
def api_manual_export_to_panel(req: ManualExportReq):
    """对一个已注册账号手动触发到面板的导出。

    targets 里选 cpa / sub2api 之一或全部。即使总开关未启用，本接口也会执行
    （只要 URL/密钥 等基础配置已填）。
    """
    from ..services import exporter
    cred = db.get_registered(req.email)
    if not cred:
        raise HTTPException(404, f"未找到已注册账号: {req.email}")

    cfg = db.get_export_internal_config()
    out = {"email": req.email, "cpa": None, "sub2api": None}
    targets = {t.strip().lower() for t in (req.targets or []) if t}

    if "cpa" in targets:
        cpa_cfg = dict(cfg["cpa"])
        cpa_cfg["enabled"] = True  # 手动触发：强制启用
        try:
            out["cpa"] = exporter.export_to_cpa(cred, cpa_cfg)
        except Exception as e:
            out["cpa"] = {"ok": False, "error": str(e)}
    if "sub2api" in targets:
        sub2api_cfg = dict(cfg["sub2api"])
        sub2api_cfg["enabled"] = True
        try:
            out["sub2api"] = exporter.export_to_sub2api(cred, sub2api_cfg)
        except Exception as e:
            out["sub2api"] = {"ok": False, "error": str(e)}

    return {"ok": True, **out}


class UpdateCredReq(BaseModel):
    email: str = Field(..., description="要修改的已注册账号邮箱")
    # None = 该字段不动；空串 = 主动清空。前端不填的字段就别传。
    password: Optional[str] = Field(None, description="新密码，None=不修改")
    totp_secret: Optional[str] = Field(None, description="新 TOTP secret，None=不修改")


@router.post("/api/registered/update_credentials")
def api_update_credentials(req: UpdateCredReq):
    """手动修正已注册账号的密码 / TOTP secret。

    ⚠️ 只改本地库，不会同步到 OpenAI。用途是把外部已知凭证补进来或修正记录。

    改完的值会被登录流程直接用上（registrar 的 account_callback 走
    db.get_registered，不区分数据来源），所以 totp_secret 必须过 base32
    校验 —— 脏值存进去要等真登录时才炸，那时根本看不出是手填填错的。
    """
    email = (req.email or "").strip().lower()
    if not email:
        raise HTTPException(400, "email 不能为空")
    if req.password is None and req.totp_secret is None:
        raise HTTPException(400, "没有要修改的字段")
    try:
        ok = db.update_registered_manual(
            email, password=req.password, totp_secret=req.totp_secret
        )
    except ValueError as e:
        # 校验失败：把具体原因带给前端，别让用户猜哪里填错了
        raise HTTPException(400, str(e))
    if not ok:
        raise HTTPException(404, f"未找到已注册账号: {email}")

    changed = [n for n, v in (("密码", req.password), ("TOTP secret", req.totp_secret))
               if v is not None]
    logger.info(f"[registered] 手动修改凭证 email={email} 字段={'+'.join(changed)}")
    return {"ok": True, "email": email, "changed": changed}


class UpdateProxyReq(BaseModel):
    email: str = Field(..., description="已注册账号邮箱")
    proxy: str = Field("", description="新代理；空串=改为直连")


class Bind2FAReq(BaseModel):
    email: str = Field(..., description="已注册账号邮箱")
    proxy: str = Field("", description="代理；留空则用该号当前的代理")
    otp_timeout: int = Field(180, description="等待邮件验证码的秒数")


@router.post("/api/registered/update_proxy")
def api_update_proxy(req: UpdateProxyReq):
    """Set (or clear) the proxy of one registered account.

    The stored value is what later checks and payment-link requests fall back
    to, so an account keeps leaving from the same IP it was registered on.
    Pool quota is moved with it: the old proxy is released, the new one is
    reserved when it exists in the pool.
    """
    email = (req.email or "").strip().lower()
    if not email:
        raise HTTPException(400, "email 不能为空")
    ok = db.update_registered_proxy(email, req.proxy)
    if not ok:
        raise HTTPException(404, f"未找到已注册账号: {email}")
    logger.info(f"[registered] 修改代理 email={email} -> {'直连' if not req.proxy.strip() else '已设置'}")
    return {"ok": True, "email": email, "proxy": (req.proxy or "").strip()}


@router.post("/api/registered/bind_2fa")
def api_bind_2fa(req: Bind2FAReq):
    """Bind TOTP 2FA to an account that does not have one yet.

    Runs the existing slow login chain (``bind_totp_2fa``) and stores the new
    secret immediately. Accounts that already have a secret are refused: the
    secret cannot be fetched again, and replacing a live factor needs the old
    code plus a disable step.
    """
    from ...services.two_factor import add_totp_2fa

    email = (req.email or "").strip().lower()
    if not email:
        raise HTTPException(400, "email 不能为空")
    row = db.get_registered(email)
    if not row:
        raise HTTPException(404, f"未找到已注册账号: {email}")
    extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
    effective_proxy = (req.proxy or "").strip() or str(extra.get("proxy") or "").strip()

    info = add_totp_2fa(email, proxy=effective_proxy, otp_timeout=int(req.otp_timeout or 180))
    if not info.get("ok"):
        reason = str(info.get("reason") or "unknown")
        messages = {
            "already_bound": "该号已有 2FA secret；当前页面先支持添加，不覆盖已有 secret",
            "already_bound_unknown_secret": (
                "该账号在 OpenAI 侧已经启用 2FA，但本地没有 secret，无法自动应答登录挑战。"
                "请先在 ChatGPT 网页端关闭 2FA，再重新点「绑定2FA」"
            ),
            "password_missing": "该号没有密码，无法程序化补绑 2FA",
            "mail_credentials_missing": "号池里找不到这个号的邮箱凭证，收不到验证码",
            "bind_failed": "补绑失败：登录/接码/激活有一条没通过，详见运行日志",
            "not_found": "未找到该账号",
        }
        raise HTTPException(400, messages.get(reason, f"补绑失败: {reason}"))
    return {
        "ok": True,
        "email": email,
        "totp_secret": info.get("totp_secret", ""),
        "factor_id": info.get("factor_id", ""),
    }


# ──────────────────────── 套餐 / Plus 试用资格检查 ────────────────────────


class CheckPlusReq(BaseModel):
    emails: list[str] = Field(..., description="要检查的邮箱列表")
    proxy: str = Field("", description="查询代理，留空直连")


@router.post("/api/registered/check_plus")
def api_check_plus(req: CheckPlusReq):
    """Query plan and Plus trial eligibility for registered accounts."""
    from ...services.plan_check import check_registered_plan

    from ...storage import facade as db

    results = {}
    for email in dict.fromkeys(req.emails):
        normalized = str(email or "").strip().lower()
        if not normalized:
            continue
        # 没显式指定代理时，逐号用它**注册时**的出口 IP —— 账号在哪个 IP 上
        # 注册的，就还在哪个 IP 上检测，别让同一个号今天走 A 明天走 B。
        row = db.get_registered(normalized) or {}
        extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
        effective_proxy = req.proxy.strip() or str(extra.get("proxy") or "").strip()
        results[normalized] = check_registered_plan(
            normalized,
            cred=row or None,
            proxy_url=effective_proxy,
        )
    return {"ok": True, "results": results}
