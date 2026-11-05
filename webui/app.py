"""FastAPI 主程序：路由 + SSE 流式日志。

启动:
    python -m webui.app
或者:
    python start_webui.py
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from . import db, export_formats, registrar  # noqa: E402
from .auto_loop import CONTROLLER as AUTO_LOOP  # noqa: E402
from .exporter import _decode_jwt_payload, _get_auth  # noqa: E402
from mail_providers import (  # noqa: E402
    ImportValidationError,
    MailProviderError,
    create_mail_provider,
    get_provider_class,
    list_pooled_providers,
    list_providers,
)

# 启动时自动释放卡死的 in_use 号（上次进程崩溃 / 强退留下的）
try:
    _released = db.release_stale_in_use(stale_seconds=1800)
    if _released > 0:
        logging.getLogger("webui").info(f"[startup] 释放 {_released} 个卡死的 in_use 号")
except Exception as _e:
    logging.getLogger("webui").warning(f"[startup] release_stale 失败: {_e}")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("webui")

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="GPT Outlook Register WebUI", docs_url=None, redoc_url=None)


# ──────────────────────── Pydantic 模型 ────────────────────────


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


@app.get("/api/health")
def health():
    return {"ok": True, "stats": db.stats()}


@app.post("/api/import")
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


@app.get("/api/accounts")
def api_accounts(status: str = "", limit: int = 50, offset: int = 0, kind: str = ""):
    items = db.list_accounts(status=status, limit=limit, offset=offset, kind=kind)
    total = db.count_accounts(status=status, kind=kind)
    return {
        "ok": True,
        "items": items,
        "total": total,
        "by_kind": db.stats_by_kind(),
    }


@app.delete("/api/accounts/{email}")
def api_delete_account(email: str):
    ok = db.delete_account(email)
    if not ok:
        raise HTTPException(404, "not found")
    return {"ok": True}


class BulkDeleteReq(BaseModel):
    status: Optional[str] = Field(None, description="available/in_use/done/failed/all")
    emails: Optional[list[str]] = Field(None, description="按 email 列表删")


@app.post("/api/accounts/bulk_delete")
def api_bulk_delete(req: BulkDeleteReq):
    """按状态或 email 列表批量删除号池。两个参数二选一（status 优先）。"""
    if req.status:
        n = db.delete_accounts_by_status(req.status)
        return {"ok": True, "deleted": n, "by": "status", "stats": db.stats()}
    if req.emails:
        n = db.delete_accounts_by_emails(req.emails)
        return {"ok": True, "deleted": n, "by": "emails", "stats": db.stats()}
    raise HTTPException(400, "需要 status 或 emails")


@app.post("/api/accounts/reset_failed")
def api_reset_failed():
    n = db.reset_failed_to_available()
    return {"ok": True, "reset": n, "stats": db.stats()}


@app.post("/api/accounts/reset/{email}")
def api_reset_account(email: str):
    """重置单个号：done / failed → available。"""
    ok = db.reset_to_available(email)
    if not ok:
        raise HTTPException(404, f"邮箱 {email} 不存在")
    return {"ok": True, "email": email}


class BulkResetReq(BaseModel):
    emails: list[str]


@app.post("/api/accounts/bulk_reset")
def api_bulk_reset(req: BulkResetReq):
    """批量重置：done / failed → available。"""
    if not req.emails:
        raise HTTPException(400, "emails 不能为空")
    n = db.bulk_reset_to_available(req.emails)
    return {"ok": True, "reset": n, "stats": db.stats()}


@app.post("/api/accounts/release_stale")
def api_release_stale(stale_seconds: int = 1800):
    n = db.release_stale_in_use(stale_seconds=stale_seconds)
    return {"ok": True, "released": n, "stats": db.stats()}


@app.get("/api/stats")
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


@app.get("/api/settings/prefs")
def api_get_prefs():
    return {"ok": True, "prefs": db.get_prefs()}


@app.post("/api/settings/prefs")
def api_save_prefs(req: PrefsReq):
    data = req.model_dump(exclude_none=True)
    return {"ok": True, "prefs": db.save_prefs(data)}


# ──────────────────────── 代理连通性测试 ────────────────────────


class ProxyTestReq(BaseModel):
    proxies: list[str] = Field(..., description="要测试的代理列表")
    timeout: int = Field(8, description="每个代理超时秒数")
    test_url: str = Field("https://api.ipify.org?format=json",
                          description="测试目标 URL（默认返回出口 IP）")


@app.post("/api/proxy/test")
def api_proxy_test(req: ProxyTestReq):
    """并发测试代理连通性。复用真实注册流程的 create_http_session（含 socks5->socks5h
    标准化、trust_env=False），保证「测试正常」== 「跑号能用」。返回 ok / 延迟 / 出口 IP。

    协议说明：不写协议的 `ip:port` 被 curl 按 HTTP 代理处理；SOCKS5 需显式写 socks5://。
    """
    import sys as _sys
    ROOT_DIR = Path(__file__).resolve().parents[1]
    if str(ROOT_DIR) not in _sys.path:
        _sys.path.insert(0, str(ROOT_DIR))
    try:
        from http_client import create_http_session
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"加载 http_client 失败: {e}")

    import time as _t
    from concurrent.futures import ThreadPoolExecutor

    timeout = max(1, min(int(req.timeout or 8), 60))
    test_url = (req.test_url or "https://api.ipify.org?format=json").strip()

    proxies = [p.strip() for p in (req.proxies or []) if p and p.strip()]
    if not proxies:
        raise HTTPException(400, "proxies 不能为空")

    def _test_one(proxy: str):
        t0 = _t.perf_counter()
        try:
            sess = create_http_session(proxy=proxy)
            resp = sess.get(test_url, timeout=timeout)
            latency = int((_t.perf_counter() - t0) * 1000)
            if resp.status_code != 200:
                return {"ok": False, "latency_ms": latency, "error": f"HTTP {resp.status_code}"}
            ip = ""
            try:
                ip = resp.json().get("ip", "")
            except Exception:
                ip = (resp.text or "").strip()[:64]
            return {"ok": True, "latency_ms": latency, "ip": ip}
        except Exception as e:  # noqa: BLE001
            latency = int((_t.perf_counter() - t0) * 1000)
            return {"ok": False, "latency_ms": latency, "error": str(e)[:140]}

    results = {}
    with ThreadPoolExecutor(max_workers=min(20, len(proxies))) as ex:
        for proxy, res in zip(proxies, ex.map(_test_one, proxies)):
            results[proxy] = res
    return {"ok": True, "results": results}


# ──────────────────────── 代理池管理 ────────────────────────
#
# 代理池原来只在前端 localStorage（stores/proxy.js，纯字符串数组），
# 后端拿不到任何元数据 → 既没法限流，也没法在某个代理被风控后自动换走。
# 现在迁到 SQLite（proxy_pool 表），前端改成调这些接口。


class ProxyPoolImportReq(BaseModel):
    text: str = Field("", description="每行一个代理")
    mode: str = Field("replace", description="replace=覆盖（保留已有代理的用量）/ append=追加")


class ProxyPoolUpdateReq(BaseModel):
    proxy: str = Field(..., description="代理原文")
    enabled: Optional[bool] = None
    max_uses: Optional[int] = Field(None, description="成功注册数上限，0=不限")
    note: Optional[str] = None
    # 代理类型：static=固定出口 IP / dynamic=每次请求换 IP。
    # ⚠️ 被 Cloudflare 403 拦时两者的处置完全不同：
    #    static → 这个 IP 已被拉黑，连续被拦达阈值就自动停用
    #    dynamic → 换个出口 IP 可能就通过，不停用只记次数
    proxy_type: Optional[str] = Field(None, description="static / dynamic")


class ProxyPoolResetReq(BaseModel):
    proxy: Optional[str] = Field(None, description="留空=重置全部")


class ProxyIpv6TestReq(BaseModel):
    proxies: list[str] = Field(default_factory=list, description="要测的代理列表")
    timeout: int = Field(10, description="单代理超时秒数")


@app.get("/api/proxy/pool")
def api_proxy_pool():
    return {"ok": True, "items": db.list_proxies(), "stats": db.proxy_pool_stats()}


@app.post("/api/proxy/pool")
def api_proxy_pool_import(req: ProxyPoolImportReq):
    """批量导入代理。replace 只删掉不在新列表里的，已有代理的用量/启用状态保留。"""
    mode = "append" if str(req.mode or "").lower() == "append" else "replace"
    proxies = [p.strip() for p in (req.text or "").splitlines() if p.strip()]
    result = db.replace_proxies(proxies, mode=mode)
    return {"ok": True, **result, "stats": db.proxy_pool_stats()}


@app.delete("/api/proxy/pool")
def api_proxy_pool_clear():
    n = db.clear_proxies()
    return {"ok": True, "removed": n, "items": db.list_proxies(),
            "stats": db.proxy_pool_stats()}


@app.patch("/api/proxy/pool")
def api_proxy_pool_update(req: ProxyPoolUpdateReq):
    """改单个代理的启用状态 / 上限 / 备注 / 类型（静态 or 动态）。"""
    ok = db.update_proxy(
        req.proxy, enabled=req.enabled, max_uses=req.max_uses, note=req.note,
        proxy_type=req.proxy_type,
    )
    if not ok:
        raise HTTPException(404, "代理不存在")
    return {"ok": True, "items": db.list_proxies(), "stats": db.proxy_pool_stats()}


@app.post("/api/proxy/pool/cf_reset")
def api_proxy_pool_cf_reset(req: ProxyPoolResetReq):
    """清零 CF 拦截计数（代理换了 IP / 供应商修了之后手动点一下）。"""
    n = 1 if req.proxy else db.reset_all_proxy_cf_blocked()
    if req.proxy:
        db.reset_proxy_cf_blocked(req.proxy)
    return {"ok": True, "reset": n, "items": db.list_proxies(),
            "stats": db.proxy_pool_stats()}


@app.post("/api/proxy/pool/ipv6_test")
def api_proxy_pool_ipv6_test(req: ProxyIpv6TestReq):
    """检测代理池里哪些代理支持 IPv6 出口。

    为什么需要：IPv6 的地址空间比 IPv4 大几个数量级，同一个 /64 段里的
    "邻居" 少得多 → 被 Cloudflare 按 IP 段批量拉黑的概率低得多。
    实测日志里 7 次 warmup CF 403 全是 IPv4 出口。

    检测方式：走 cloudflare.com/cdn-cgi/trace，它会同时返回 ip 和 ipv6 两个字段
    —— ip 是客户端 IPv4，ipv6 是客户端 IPv6。有 ipv6 字段且非空 → 支持。
    """
    import sys as _sys
    ROOT_DIR = Path(__file__).resolve().parents[1]
    if str(ROOT_DIR) not in _sys.path:
        _sys.path.insert(0, str(ROOT_DIR))
    try:
        from http_client import create_http_session
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"加载 http_client 失败: {e}")

    import re as _re
    import time as _t
    from concurrent.futures import ThreadPoolExecutor

    timeout = max(3, min(int(req.timeout or 10), 60))
    proxies = [p.strip() for p in (req.proxies or []) if p and p.strip()]
    if not proxies:
        raise HTTPException(400, "proxies 不能为空")

    def _test_one(proxy: str):
        t0 = _t.perf_counter()
        try:
            sess = create_http_session(proxy=proxy)
            resp = sess.get("https://cloudflare.com/cdn-cgi/trace", timeout=timeout)
            latency = int((_t.perf_counter() - t0) * 1000)
            if resp.status_code != 200:
                return {
                    "ok": False, "ipv6": False, "latency_ms": latency,
                    "error": f"HTTP {resp.status_code}",
                }
            text = resp.text or ""
            ip4 = ""
            ip6 = ""
            m4 = _re.search(r"^ip=(.*)$", text, _re.M)
            m6 = _re.search(r"^ipv6=(.*)$", text, _re.M)
            if m4:
                ip4 = m4.group(1).strip()
            if m6:
                ip6 = m6.group(1).strip()
            # 有非空 ipv6 字段 → 该出口支持 IPv6
            has6 = bool(ip6)
            return {
                "ok": True, "ipv6": has6, "latency_ms": latency,
                "ip": ip4, "ipv6_addr": ip6,
            }
        except Exception as e:  # noqa: BLE001
            latency = int((_t.perf_counter() - t0) * 1000)
            return {
                "ok": False, "ipv6": False, "latency_ms": latency,
                "error": str(e)[:140],
            }

    results = {}
    with ThreadPoolExecutor(max_workers=min(10, len(proxies))) as ex:
        for proxy, res in zip(proxies, ex.map(_test_one, proxies)):
            results[proxy] = res
            # 结果落库，前端表格直接展示
            db.set_proxy_ipv6_status(proxy, "ok" if res.get("ipv6") else "fail")
    return {"ok": True, "results": results, "items": db.list_proxies(),
            "stats": db.proxy_pool_stats()}


@app.delete("/api/proxy/pool/{proxy:path}")
def api_proxy_pool_delete(proxy: str):
    ok = db.delete_proxy(proxy)
    if not ok:
        raise HTTPException(404, "代理不存在")
    return {"ok": True, "items": db.list_proxies(), "stats": db.proxy_pool_stats()}


@app.post("/api/proxy/pool/reset")
def api_proxy_pool_reset(req: ProxyPoolResetReq):
    """重置用量计数（不删代理）。"""
    n = db.reset_proxy_counts(req.proxy)
    return {"ok": True, "reset": n, "items": db.list_proxies(),
            "stats": db.proxy_pool_stats()}


@app.post("/api/register")
def api_register(req: RegisterReq):
    """启动注册任务，返回 run_id。前端拿 run_id 去 /api/runs/{run_id}/stream 订阅 SSE。"""
    mail_source = db.get_setting("mail_source", "outlook")
    try:
        provider_cls = get_provider_class(mail_source)
    except MailProviderError as e:
        raise HTTPException(400, str(e))

    # 要不要 claim 号池，由 provider 自己声明的 pooled 决定 ——
    # 原来写死 `mail_source == "cf_temp"`，加一种非池化邮箱就得改这里。
    if not provider_cls.pooled:
        # 非池化：地址由 provider 现造，用占位 account 走完后面的流程
        import time as _t
        account = {
            "email": f"{mail_source}_placeholder_{int(_t.time())}@placeholder.local",
            "password": "",
            "client_id": "",
            "refresh_token": "",
            "relay_url": "",
            "kind": mail_source,
        }
    elif req.email:
        account = db.claim_account(req.email)
        if not account:
            raise HTTPException(400, f"邮箱 {req.email} 不可用 (不存在 / 已 in_use / 已完成)")
        if (account.get("kind") or "outlook") != mail_source:
            # 号池里混放多种邮箱，点名的号必须和当前来源一致，
            # 否则会拿 Outlook 的凭证去初始化 Gmail provider
            db.release_unused(account["email"])
            raise HTTPException(
                400,
                f"{req.email} 是 {account.get('kind')} 的号，"
                f"当前邮箱来源是 {mail_source}，请先切换来源",
            )
    else:
        account = db.claim_next(kind=mail_source)
        if not account:
            raise HTTPException(
                400,
                f"号池里没有 available 的 {provider_cls.display_name} 账号；请先批量导入",
            )

    options = {
        "want_access_token": req.want_access_token,
        "want_session_token": req.want_session_token,
        "want_refresh_token": req.want_refresh_token,
        "proxy": req.proxy,
        "otp_timeout": int(req.otp_timeout),
        "allow_existing_login": req.allow_existing_login,
        "want_2fa": req.want_2fa,
    }

    # 单次注册用的代理是用户手填/手选的，同样要计入池子的配额口径。
    # 这里预占一格（自动跑号那边是 pick_proxy 预占的），失败时归还。
    single_proxy = str(req.proxy or "").strip()
    reserved = False
    if single_proxy and db.find_proxy(single_proxy):
        if db.reserve_proxy(single_proxy):
            reserved = True
            options["proxy_reserved"] = 1

    run_id = registrar.start_registration(account, options)
    logger.info(f"[run] {run_id} -> {account['email']} (mail_source={mail_source})")
    return {"ok": True, "run_id": run_id, "email": account["email"]}


@app.get("/api/runs/{run_id}/stream")
async def api_stream(run_id: str, request: Request):
    """SSE 实时推送日志 + 事件。"""
    q = registrar.get_run_queue(run_id)
    if q is None:
        raise HTTPException(404, "run_id not found or finished")

    async def event_gen():
        loop = asyncio.get_event_loop()
        try:
            while True:
                if await request.is_disconnected():
                    break
                # 从队列取消息（用 run_in_executor 避免阻塞 event loop）
                msg = await loop.run_in_executor(None, _safe_get, q)
                if msg is None:
                    # sentinel: 任务结束
                    yield "event: end\ndata: {}\n\n"
                    break
                if msg.startswith("__EVENT__:"):
                    yield f"event: status\ndata: {msg[len('__EVENT__:'):]}\n\n"
                else:
                    yield f"event: log\ndata: {json.dumps({'line': msg}, ensure_ascii=False)}\n\n"
        finally:
            registrar.remove_run_queue(run_id)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # 避免 nginx 缓冲
            "Connection": "keep-alive",
        },
    )


def _safe_get(q):
    try:
        return q.get(timeout=60)
    except Exception:
        return ""  # 心跳：返空串让 SSE 检查 disconnect


@app.get("/api/runs")
def api_runs(limit: int = 50):
    return {"ok": True, "items": db.list_runs(limit=limit)}


@app.get("/api/registered")
def api_registered(limit: int = 20, offset: int = 0, filter: str = "all"):
    items = db.list_registered(limit=limit, offset=offset, filter_rt=filter)
    total = db.count_registered(filter_rt=filter)
    return {"ok": True, "items": items, "total": total}


@app.get("/api/registered/{email}")
def api_registered_one(email: str):
    row = db.get_registered(email)
    if not row:
        raise HTTPException(404, "not found")
    return {"ok": True, "data": row}


@app.delete("/api/registered/{email}")
def api_delete_registered(email: str):
    ok = db.delete_registered(email)
    if not ok:
        raise HTTPException(404, "not found")
    return {"ok": True}


class BulkDeleteRegisteredReq(BaseModel):
    emails: Optional[list[str]] = Field(None, description="按 email 列表删；留空 + all=true 则删全部")
    all: bool = False


@app.post("/api/registered/bulk_delete")
def api_bulk_delete_registered(req: BulkDeleteRegisteredReq):
    if req.all:
        n = db.delete_all_registered()
        return {"ok": True, "deleted": n, "by": "all"}
    if req.emails:
        n = db.delete_registered_by_emails(req.emails)
        return {"ok": True, "deleted": n, "by": "emails"}
    raise HTTPException(400, "需要 emails 或 all=true")


class MarkAbnormalReq(BaseModel):
    emails: list[str] = Field(..., description="要标注的账号邮箱列表")
    abnormal: bool = Field(True, description="True=标注异常，False=取消标注")


@app.post("/api/registered/mark_abnormal")
def api_mark_abnormal(req: MarkAbnormalReq):
    """标注账号异常（被封 / 额度耗尽 / 不可用）。

    标注异常时会**自动释放该号占用的代理配额** —— 号已经废了，
    它的 IP 名额应该还回池子里给别的号用。
    """
    n = db.mark_registered_abnormal_by_emails(req.emails, req.abnormal)
    return {"ok": True, "updated": n, "items": db.list_registered(limit=200),
            "proxy_stats": db.proxy_pool_stats()}


# ──────────────────────── 批量导出（文本） ────────────────────────
# ⚠️ 路由顺序：
#   - formats 是 4 段路径，不会被 3 段的 GET /api/registered/{email} 吃掉；
#   - export 是 POST，而 {email} 那两条是 GET / DELETE，也不冲突。
# 要加新格式只改 webui/export_formats.py，这里和前端都不用动。


@app.get("/api/registered/export/formats")
def api_export_formats():
    """导出格式清单，前端下拉菜单据此渲染。"""
    return {"ok": True, "formats": export_formats.list_formats()}


class ExportRegisteredReq(BaseModel):
    format: str = Field(..., description="格式 id，见 GET /api/registered/export/formats")
    emails: Optional[list[str]] = Field(None, description="要导出的 email 列表")
    all: bool = Field(False, description="true = 导出全部（跨页），忽略 emails")


@app.post("/api/registered/export")
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

    # 不跳行：勾了几个号就几行 / 几个文件，字段为空也照样出。
    # 手动导出**不做 refresh_token 刷新、不因为缺 rt 拦截**，这是和自动推送的区别。
    base = {
        "ok": True,
        "count": len(rows),
        "filename": fmt.filename,
        "label": fmt.label,
        "mode": fmt.mode,
        "mime": fmt.mime,
        # 这一批导出的 email 原样带回去 —— 前端「下载并删除」照着它删，删得准。
        # ⚠️ 必须由后端给：`all=true` 时前端手里只有当前页那 20 行，
        #    自己凑列表会漏删；而用 all/status 那种"全清"接口去删号池，
        #    会把**还没跑过的号**一起清掉。所以这里回传精确列表。
        "emails": [(r.get("email") or "") for r in rows],
    }

    if fmt.mode == "download":
        # 二进制（zip / json 文件）走 base64，前端解出来直接存盘，不弹预览
        blob = export_formats.render_bytes(rows, fmt)
        return {**base, "b64": base64.b64encode(blob).decode("ascii"), "size": len(blob)}

    return {**base, "text": export_formats.render_text(rows, fmt)}


# ──────────────────────── 邮箱来源配置 ────────────────────────


@app.get("/api/mail/providers")
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


@app.get("/api/settings/mail")
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


@app.post("/api/settings/mail")
def api_save_mail_config(req: SaveMailConfigReq):
    try:
        db.save_mail_config(req.model_dump(exclude_none=True))
    except MailProviderError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "config": db.get_mail_config()}


@app.post("/api/settings/mail/test")
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


@app.get("/api/mail/orders")
def api_mail_orders(limit: int = 200, offset: int = 0):
    """本机记录的 Remail 订单列表。"""
    items = db.list_remail_orders(limit=limit, offset=offset)
    return {"ok": True, "items": items, "total": len(items)}


class RemailOrderReq(BaseModel):
    suffix: Optional[str] = Field(None, description="邮箱后缀，留空用配置里的默认值")
    project_id: Optional[int] = Field(None, description="项目 ID，留空用配置里的")
    service_mode: Optional[str] = Field(None, description="purchase / code")
    supply_policy: Optional[str] = Field(None, description="private_first / public_only")


@app.post("/api/mail/orders")
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


@app.get("/api/mail/orders/{order_no}/mails")
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


@app.post("/api/mail/orders/{order_no}/link")
def api_link_mail_order(order_no: str, req: LinkOrderReq):
    """把订单关联到本地注册成功的账号。"""
    ok = db.link_remail_order(order_no, req.email)
    if not ok:
        raise HTTPException(404, "订单不存在")
    return {"ok": True, "items": db.list_remail_orders()}


class OrderDisabledReq(BaseModel):
    disabled: bool = Field(True, description="True=禁用，False=启用")


@app.post("/api/mail/orders/{order_no}/disabled")
def api_mail_order_disabled(order_no: str, req: OrderDisabledReq):
    """本地禁用 / 启用一笔订单。

    禁用的订单不会再被自动复用（但仍然可以手动取件 —— 邮箱还在，钱已经花了）。
    只改本地标记，不动 Remail 服务端的状态。
    """
    ok = db.set_remail_order_disabled(order_no, req.disabled)
    if not ok:
        raise HTTPException(404, "订单不存在")
    return {"ok": True, "items": db.list_remail_orders()}


@app.delete("/api/mail/orders/{order_no}")
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


@app.post("/api/mail/orders/bulk_delete")
def api_bulk_delete_mail_orders(req: BulkDeleteOrdersReq):
    """批量删除本地订单记录。"""
    n = db.delete_remail_orders(req.order_nos)
    return {"ok": True, "deleted": n, "items": db.list_remail_orders()}


@app.get("/api/mail/wallet")
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


# ──────────────────────── SMS 接码配置 ────────────────────────


@app.get("/api/settings/sms")
def api_get_sms_config():
    return {"ok": True, "config": db.get_sms_config()}


class SaveSmsConfigReq(BaseModel):
    sms_enabled: Optional[str] = None              # "0" / "1"
    sms_provider: Optional[str] = None             # smsbower / herosms
    sms_api_key: Optional[str] = None              # 传 '***' 表示不修改
    sms_country: Optional[str] = None              # ID 或国家代码（'52' / 'th'）
    sms_service: Optional[str] = None              # OpenAI = 'dr'
    sms_max_price: Optional[str] = None
    sms_fixed_price: Optional[str] = None
    sms_reuse_phone: Optional[str] = None
    sms_phone_success_max: Optional[str] = None
    sms_auto_country: Optional[str] = None
    sms_strict_whitelist: Optional[str] = None
    sms_allowed_countries: Optional[str] = None    # 逗号分隔的 ID 列表，自动选号时只从这里挑
    sms_auto_min_stock: Optional[str] = None
    sms_auto_max_price: Optional[str] = None
    sms_max_phone_attempts: Optional[str] = None   # 空 = 用 provider 默认；>0 = 自定义
    sms_per_phone_timeout: Optional[str] = None    # 单号等待秒数（默认 80）


@app.post("/api/settings/sms")
def api_save_sms_config(req: SaveSmsConfigReq):
    db.save_sms_config(req.model_dump(exclude_none=True))
    return {"ok": True, "config": db.get_sms_config()}


@app.post("/api/settings/sms/test")
def api_test_sms():
    """测试 SMS provider 连通性：查询余额。"""
    cfg = db.get_sms_internal_config()
    if not cfg.get("sms_api_key"):
        raise HTTPException(400, "未配置 sms_api_key")

    import sys as _sys
    ROOT_DIR = Path(__file__).resolve().parents[1]
    if str(ROOT_DIR) not in _sys.path:
        _sys.path.insert(0, str(ROOT_DIR))
    from sms_provider import create_sms_provider
    try:
        provider = create_sms_provider(cfg["sms_provider"], cfg)
        balance = provider.get_balance()
        return {
            "ok": True,
            "provider": cfg["sms_provider"],
            "balance": balance,
            "message": f"连接成功，余额: {balance}",
        }
    except Exception as e:
        raise HTTPException(500, f"连接失败: {e}")


@app.get("/api/settings/sms/countries")
def api_sms_top_countries():
    """查询当前接码平台的国家排名（价格 + 库存）。"""
    cfg = db.get_sms_internal_config()
    if not cfg.get("sms_api_key"):
        raise HTTPException(400, "未配置 sms_api_key")

    import sys as _sys
    ROOT_DIR = Path(__file__).resolve().parents[1]
    if str(ROOT_DIR) not in _sys.path:
        _sys.path.insert(0, str(ROOT_DIR))
    from sms_provider import create_sms_provider, OPENAI_SMS_COUNTRIES, SMS_COUNTRY_NAMES_CN
    try:
        provider = create_sms_provider(cfg["sms_provider"], cfg)
        rows = provider.get_top_countries(service=cfg.get("sms_service") or "dr")
        for r in rows:
            cid = str(r.get("country"))
            r["openai_sms_safe"] = cid in OPENAI_SMS_COUNTRIES
            r["name_cn"] = SMS_COUNTRY_NAMES_CN.get(cid, "未知")
        return {"ok": True, "countries": rows[:30], "openai_sms_safe": list(OPENAI_SMS_COUNTRIES)}
    except Exception as e:
        raise HTTPException(500, f"查询失败: {e}")


@app.get("/api/settings/sms/all_countries")
def api_sms_all_countries(provider: str = ""):
    """返回当前平台实际有库存的国家（动态查询）；查询失败则 fallback 到静态字典。"""
    import sys as _sys
    ROOT_DIR = Path(__file__).resolve().parents[1]
    if str(ROOT_DIR) not in _sys.path:
        _sys.path.insert(0, str(ROOT_DIR))
    from sms_provider import SMS_COUNTRY_NAMES_CN, OPENAI_SMS_COUNTRIES, create_sms_provider

    cfg = db.get_sms_internal_config()
    if provider:
        cfg["sms_provider"] = provider

    # 尝试从平台 API 动态获取有库存的国家
    if cfg.get("sms_api_key"):
        try:
            p = create_sms_provider(cfg["sms_provider"], cfg)
            rows = p.get_top_countries(service=cfg.get("sms_service") or "dr")
            countries = []
            for r in rows:
                cid = str(r.get("country") or "")
                countries.append({
                    "id": cid,
                    "name_cn": SMS_COUNTRY_NAMES_CN.get(cid, f"国家{cid}"),
                    "openai_sms_safe": cid in OPENAI_SMS_COUNTRIES,
                    "price": r.get("price"),
                    "count": r.get("count"),
                })
            if countries:
                return {"ok": True, "countries": countries,
                        "openai_sms_safe": list(OPENAI_SMS_COUNTRIES), "source": "live"}
        except Exception:
            pass

    # fallback: 静态字典
    items = sorted(SMS_COUNTRY_NAMES_CN.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 9999)
    countries = [
        {"id": cid, "name_cn": name, "openai_sms_safe": cid in OPENAI_SMS_COUNTRIES}
        for cid, name in items
    ]
    return {"ok": True, "countries": countries,
            "openai_sms_safe": list(OPENAI_SMS_COUNTRIES), "source": "static"}


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


@app.get("/api/settings/export")
def api_get_export_config():
    return {"ok": True, "config": db.get_export_config()}


@app.post("/api/settings/export")
def api_save_export_config(req: SaveExportConfigReq):
    db.save_export_config(req.model_dump(exclude_none=True))
    return {"ok": True, "config": db.get_export_config()}


class TestExportReq(BaseModel):
    target: str = Field(..., description="cpa 或 sub2api")


@app.post("/api/settings/export/test")
def api_test_export(req: TestExportReq):
    """测试 CPA / SUB2API 连通性。"""
    from . import exporter
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


@app.post("/api/registered/export_to_panel")
def api_manual_export_to_panel(req: ManualExportReq):
    """对一个已注册账号手动触发到面板的导出。

    targets 里选 cpa / sub2api 之一或全部。即使总开关未启用，本接口也会执行
    （只要 URL/密钥 等基础配置已填）。
    """
    from . import exporter
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


@app.post("/api/registered/update_credentials")
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


# ──────────────────────── Plus 试用检查 ────────────────────────


class CheckPlusReq(BaseModel):
    emails: list[str] = Field(..., description="要检查的邮箱列表")
    proxy: str = Field("", description="查询代理，留空直连")


# 封号在 401/403 响应体里的措辞。OpenAI 不止一种写法，全部小写后子串匹配。
# 新措辞加在这里即可；日志会打出未匹配的 401/403 原文方便补充。
_DEACTIVATED_MARKERS = (
    "account_deactivated",
    "accountdeactivated",
    "deactivated",
    "has been deactivated",
    "disabled",
    "suspended",
    "banned",
    "violat",          # violating / violation of our policies
    "potential abuse",
    "terminated",
)


def _body_text(resp) -> str:
    """安全取响应体文本，任何异常都不许打断检测循环。"""
    try:
        return (resp.text or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _looks_deactivated(body: str) -> bool:
    return any(m in body.lower() for m in _DEACTIVATED_MARKERS)


@app.post("/api/registered/check_plus")
def api_check_plus(req: CheckPlusReq):
    """用 access_token 查询账号的 Plus 试用状态。"""
    from http_client import create_http_session

    log = logging.getLogger("webui")
    url = "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27"
    ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/145.0.0.0 Safari/537.36"
    )

    # 走和注册流程同一个 create_http_session，不再自己拼 proxies dict。
    # 它负责两件这里以前漏掉的事：
    #   1) socks5:// -> socks5h://，DNS 交给代理端解析。用本地 DNS 打
    #      chatgpt.com 经常握手失败，这是「填了 SOCKS5 就检测不出来」的真正原因。
    #   2) trust_env=False + 显式空代理，代理留空时是真直连，
    #      不会被系统 HTTP_PROXY/HTTPS_PROXY 悄悄接管。
    proxy = req.proxy.strip()
    try:
        sess = create_http_session(proxy=proxy or None, impersonate="chrome110")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"创建 HTTP 会话失败: {e}")

    note = ""

    def _check(access_token: str, account_id: str = "", device_id: str = ""):
        """打一次检测请求。

        ⚠️ 这里**不再自动降级直连**。原来的行为是：代理第一次报错就永久切直连，
        后面所有号都用主人的真实 IP 去打 chatgpt.com 的账号接口，而提示只是
        结果末尾一句小字。2026-08-10 实测踩到：主人改了代理池密码，这页却还在
        用 localStorage 里的旧代理 → curl:(97) 鉴权被拒 → 静默直连。
        检测失败重试一次就好，不值得拿真实 IP 换。

        请求头按 chatgpt.com 前端真实发的补齐（Origin/Referer/ChatGPT-Account-ID/
        OAI-Device-Id）。以前只发 Authorization，缺 Origin/Referer 属于典型的
        非浏览器特征，容易被风控挑出来；account_id 从 access_token 的 JWT 里解，
        不额外请求。
        """
        nonlocal note
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "User-Agent": ua,
            "Origin": "https://chatgpt.com",
            "Referer": "https://chatgpt.com/",
        }
        if account_id:
            headers["ChatGPT-Account-ID"] = account_id
        if device_id:
            headers["OAI-Device-Id"] = device_id
        try:
            return sess.get(url, headers=headers, timeout=15)
        except Exception as e:  # noqa: BLE001
            if proxy and not note:
                # 把 curl 的错误码带出来：(97)=SOCKS5 鉴权被拒，(7)=连不上，
                # 笼统一句「代理连不通」会让人以为是网络抖动，其实是密码/配额问题。
                msg = str(e)
                if "(97)" in msg or "rejected by the SOCKS5" in msg:
                    note = "代理认证被拒（SOCKS5 (97)）—— 检查代理账号密码/配额是否已变更"
                elif "(7)" in msg:
                    note = "代理连不上（curl (7)）—— 检查代理地址端口是否可达"
                else:
                    note = f"代理请求失败（{type(e).__name__}）—— 已保持代理，未改直连"
                log.warning(f"[check_plus] {note}: {msg[:140]}")
            raise

    results = {}
    for email in req.emails:
        cred = db.get_registered(email)
        if not cred:
            results[email] = {"status": "not_found", "label": "未找到"}
            continue
        at = (cred.get("access_token") or "").strip()
        if not at:
            results[email] = {"status": "no_at", "label": "无AT"}
            continue
        # account_id 直接从 AT 的 JWT payload 解（实测 12/12 都带），不发额外请求。
        auth_claims = _get_auth(_decode_jwt_payload(at))
        account_id = str(
            auth_claims.get("chatgpt_account_id") or auth_claims.get("account_id") or ""
        ).strip()
        # device_id 库里普遍是空的（注册时没落盘），按邮箱派生一个稳定 UUID：
        # 同一个号每次检测都是同一个 device，比每次随机更像正常客户端。
        device_id = (cred.get("device_id") or "").strip() or str(
            uuid.uuid5(uuid.NAMESPACE_DNS, f"dango-check-plus:{email}")
        )
        try:
            resp = _check(at, account_id, device_id)
        except Exception as e:  # noqa: BLE001
            results[email] = {"status": "error", "label": "网络失败"}
            log.warning(f"[check_plus] {email} 请求失败: {str(e)[:140]}")
            continue
        if resp.status_code in (401, 403):
            # 401/403 的**响应体必须看**。以前这里只看状态码就贴「凭证失效」，
            # 结果是封号号 100% 显示成凭证失效：账号被封时 access_token 会被一起
            # 吊销 → 请求在这里就 401 了 → 永远走不到下面 200 分支的 is_deactivated
            # 判据。2026-08-10 实测某个被封号：JWT exp 还有 239 小时、
            # 13:53 检测还是 plus_eligible，之后被封 → 同一个 token 直接 401。
            #
            # 未过期却失效 = 被吊销，而 OpenAI 会在响应体里写明原因，
            # 所以按响应体内容区分「封号」和「单纯的凭证过期/轮换」。
            body = _body_text(resp)
            if _looks_deactivated(body):
                results[email] = {"status": "banned", "label": "封号"}
                log.info(f"[check_plus] {email} 判定封号 (HTTP {resp.status_code}): {body[:200]}")
                continue
            if resp.status_code == 401:
                results[email] = {"status": "token_invalid", "label": "凭证失效"}
                # 日志留原文：万一是没覆盖到的封号措辞，主人看一眼就能告诉我补进去。
                log.info(f"[check_plus] {email} 401 响应体: {body[:200]}")
                continue
            results[email] = {"status": "error", "label": f"HTTP {resp.status_code}"}
            log.info(f"[check_plus] {email} 403 响应体: {body[:200]}")
            continue
        if resp.status_code != 200:
            results[email] = {"status": "error", "label": f"HTTP {resp.status_code}"}
            continue
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            results[email] = {"status": "error", "label": "响应非 JSON"}
            continue
        accts = data.get("accounts", {})
        if not accts:
            results[email] = {"status": "error", "label": "无账户数据"}
            continue
        info = next(iter(accts.values()))
        acct = info.get("account", {})
        ent = info.get("entitlement", {})
        promo = info.get("eligible_promo_campaigns", {})
        if acct.get("is_deactivated", False):
            results[email] = {"status": "banned", "label": "封号"}
            continue
        plan = acct.get("plan_type", "free")
        has_sub = ent.get("has_active_subscription", False)
        has_plus_promo = "plus" in promo and promo["plus"].get("id") == "plus-1-month-free"
        if plan == "plus" or has_sub:
            results[email] = {"status": "plus_active", "label": "Plus生效中"}
        elif has_plus_promo:
            results[email] = {"status": "plus_eligible", "label": "可领Plus试用"}
        else:
            results[email] = {"status": "free", "label": "Free"}

    try:
        sess.close()
    except Exception:  # noqa: BLE001
        pass

    checked_at = time.time()
    for email, info in results.items():
        # not_found / no_at / error 不写库：它们不是「检测结论」而是**没检测成**
        # （号不在库里、没凭证、代理挂了），写进去号就从 unchecked 过滤器里消失，
        # 看着像已经检测过。修好后重点一次即可。
        #
        # token_invalid **要写**（2026-08-10 改）。原先不写的理由是「凭证问题不是
        # 账号问题，换新凭证后该重查」，但实测下来：AT 没过期却 401 = 被吊销，
        # 大概率就是封号（2026-08-10 实测那个号即是）。不写库的实际后果是这号
        # 一直挂着上次的 plus_eligible，列表上显示「可领Plus试用」——比标成凭证
        # 失效误导得多。写库后 unchecked 过滤器会跳过它，正是想要的：它已经有结论了。
        if info["status"] not in ("not_found", "no_at", "error"):
            db.update_plus_check(email, {**info, "checked_at": checked_at})

    return {"ok": True, "results": results, "note": note}


# ──────────────────────── auto-loop ────────────────────────


class AutoLoopStartReq(BaseModel):
    """跟 RegisterReq 复用同样的字段，auto-loop 内部传给每个 run。"""
    want_access_token: bool = True
    want_session_token: bool = True
    # ⚠️ 默认 **False**：拿 refresh_token 要跑 Codex OAuth 授权链，而那条链
    #    可能**再发一封验证码**（auth_flow 的 need_otp 分支）—— 对短效邮箱
    #    （code 模式，一次性）是致命的。前端两个页面也默认关。
    #    真实默认值由前端的 wantRefreshToken / autoWantRefreshToken 决定；
    #    这里的 default 只是「调用方没传这个字段」时的兜底。
    want_refresh_token: bool = False
    proxy: str = ""              # 单代理（concurrency=1 + 无代理池时用）
    proxy_pool: str = ""         # 多代理池（每行一个）；优先于 proxy
    concurrency: int = 1         # 并发 worker 数（1-20）
    otp_timeout: int = 10
    allow_existing_login: bool = True
    cool_down_seconds: float = 3.0  # 每个 worker 跑完后冷却（防风控）
    target_count: int = 0        # 目标成功数（0=不限量，达标自动停止）
    # 批量页已放开关且**默认开**（主人要求每个号都绑）。
    # 这里的 default 仍保持 False —— 它只在「前端没传这个字段」时生效，
    # 是给旧前端缓存 / 直接打 API 的保守兜底：漏传时宁可不绑，也不要
    # 替调用方做一个不可逆的决定。真实默认值由 AutoLoop.vue 的 autoWant2fa 决定。
    want_2fa: bool = False


@app.post("/api/auto/start")
def api_auto_start(req: AutoLoopStartReq):
    res = AUTO_LOOP.start(req.model_dump())
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "启动失败"))
    return res


@app.post("/api/auto/pause")
def api_auto_pause():
    res = AUTO_LOOP.pause()
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "暂停失败"))
    return res


@app.post("/api/auto/resume")
def api_auto_resume():
    res = AUTO_LOOP.resume()
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "恢复失败"))
    return res


@app.post("/api/auto/stop")
def api_auto_stop():
    res = AUTO_LOOP.stop()
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "停止失败"))
    return res


@app.get("/api/auto/status")
def api_auto_status():
    return {"ok": True, **AUTO_LOOP.status()}


@app.get("/api/auto/stream")
async def api_auto_stream(request: Request):
    """SSE 推送 auto-loop 状态变化 + run_started / run_finished 事件。"""
    q = AUTO_LOOP.subscribe()

    async def gen():
        loop = asyncio.get_event_loop()
        try:
            while True:
                if await request.is_disconnected():
                    break
                # 阻塞拿消息，但每 30s 心跳
                try:
                    msg = await loop.run_in_executor(None, lambda: q.get(timeout=30))
                except Exception:
                    yield ": heartbeat\n\n"
                    continue
                if msg is None:
                    break
                kind = msg.get("kind", "state")
                data = msg.get("data", {})
                yield f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        finally:
            AUTO_LOOP.unsubscribe(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ──────────────────────── 静态资源 ────────────────────────


@app.get("/")
def root():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("webui.app:app", host="127.0.0.1", port=8765, reload=False)
