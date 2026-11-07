"""Account import, pool, result, and preference API routes."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ...providers.mail import (
    ImportValidationError,
    MailProviderError,
    get_provider_class,
)
from ...storage import facade as db

router = APIRouter()


class ImportReq(BaseModel):
    text: str = Field(..., description="每行一个号，格式由 kind 决定")
    kind: str = Field(
        "",
        description="邮箱来源（outlook / ...）。留空则按段数猜，"
                    "但 Outlook 和 Gmail 都是 4 段，猜不出来，建议前端必填",
    )


class RegisterReq(BaseModel):
    email: Optional[str] = Field(None, description="留空 = 自动 claim 下一个 available")
    want_access_token: bool = True
    want_session_token: bool = True
    # ⚠️ 默认 **False**：拿 refresh_token 要跑 Codex OAuth 授权链，而那条链
    #    可能**再发一封验证码**（auth_flow 的 need_otp 分支）—— 对短效邮箱
    #    （code 模式，一次性）是致命的。前端两个页面也默认关。
    #    真实默认值由前端的 wantRefreshToken / autoWantRefreshToken 决定；
    #    这里的 default 只是「调用方没传这个字段」时的兜底。
    want_refresh_token: bool = False
    proxy: str = ""
    otp_timeout: int = 10
    allow_existing_login: bool = True
    # 注册成功后自动绑定 TOTP 2FA。前端两个页面都**默认开**（主人要求每个号都绑）。
    # 这里的 default 保持 False —— 它只在「调用方没传这个字段」时生效，是给旧前端
    # 缓存 / 直接打 API 的保守兜底：漏传时宁可不绑，也不替调用方做一个不可逆的决定。
    # 真实默认值由前端 form store 的 want2fa / autoWant2fa 决定。
    want_2fa: bool = False


# ──────────────────────── API ────────────────────────


@router.get("/api/health")
def health():
    return {"ok": True, "stats": db.stats()}


@router.post("/api/import")
def api_import(req: ImportReq):
    """批量导入号池。**有一行不合法就整批拒绝**，一个都不写库。

    非法时返回 422，body 里带每一行的行号和原因，前端直接展示即可：

        {"ok": false, "message": "...", "errors": [{"line": 3, "error": "..."}]}
    """
    try:
        result = db.import_accounts(req.text, kind=req.kind)
    except ImportValidationError as e:
        return JSONResponse(
            status_code=422,
            content={"ok": False, "message": str(e), "errors": e.errors},
        )
    except MailProviderError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, **result, "stats": db.stats()}


@router.get("/api/accounts")
def api_accounts(status: str = "", limit: int = 50, offset: int = 0, kind: str = ""):
    items = db.list_accounts(status=status, limit=limit, offset=offset, kind=kind)
    total = db.count_accounts(status=status, kind=kind)
    return {
        "ok": True,
        "items": items,
        "total": total,
        "by_kind": db.stats_by_kind(),
    }


@router.delete("/api/accounts/{email}")
def api_delete_account(email: str):
    ok = db.delete_account(email)
    if not ok:
        raise HTTPException(404, "not found")
    return {"ok": True}


class BulkDeleteReq(BaseModel):
    status: Optional[str] = Field(None, description="available/in_use/done/failed/all")
    emails: Optional[list[str]] = Field(None, description="按 email 列表删")


@router.post("/api/accounts/bulk_delete")
def api_bulk_delete(req: BulkDeleteReq):
    """按状态或 email 列表批量删除号池。两个参数二选一（status 优先）。"""
    if req.status:
        n = db.delete_accounts_by_status(req.status)
        return {"ok": True, "deleted": n, "by": "status", "stats": db.stats()}
    if req.emails:
        n = db.delete_accounts_by_emails(req.emails)
        return {"ok": True, "deleted": n, "by": "emails", "stats": db.stats()}
    raise HTTPException(400, "需要 status 或 emails")


@router.post("/api/accounts/reset_failed")
def api_reset_failed():
    n = db.reset_failed_to_available()
    return {"ok": True, "reset": n, "stats": db.stats()}


@router.post("/api/accounts/reset/{email}")
def api_reset_account(email: str):
    """重置单个号：done / failed → available。"""
    ok = db.reset_to_available(email)
    if not ok:
        raise HTTPException(404, f"邮箱 {email} 不存在")
    return {"ok": True, "email": email}


class BulkResetReq(BaseModel):
    emails: list[str]


@router.post("/api/accounts/bulk_reset")
def api_bulk_reset(req: BulkResetReq):
    """批量重置：done / failed → available。"""
    if not req.emails:
        raise HTTPException(400, "emails 不能为空")
    n = db.bulk_reset_to_available(req.emails)
    return {"ok": True, "reset": n, "stats": db.stats()}


@router.post("/api/accounts/release_stale")
def api_release_stale(stale_seconds: int = 1800):
    n = db.release_stale_in_use(stale_seconds=stale_seconds)
    return {"ok": True, "released": n, "stats": db.stats()}


@router.get("/api/stats")
def api_stats():
    return {"ok": True, "stats": db.stats()}


# ──────────────────────── 界面偏好 ────────────────────────
#
# 表单默认值（并发数、OTP 等待、2FA 开关…）和主题以前存在各页面的
# localStorage 里 —— 只跟着这一个浏览器走，换机器/清缓存就没了，后端也看不到。
# 现在统一收到后端 SQLite，前端启动时拉一次。


class PrefsReq(BaseModel):
    model_config = {"extra": "allow"}

    # 不写死字段：任意 key 都存（前端加新偏好不用改这个模型）


@router.get("/api/settings/prefs")
def api_get_prefs():
    return {"ok": True, "prefs": db.get_prefs()}


@router.post("/api/settings/prefs")
def api_save_prefs(req: PrefsReq):
    data = req.model_dump(exclude_none=True)
    return {"ok": True, "prefs": db.save_prefs(data)}


