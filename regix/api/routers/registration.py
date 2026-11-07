"""Single-run registration and registered account API routes."""

from __future__ import annotations

import queue

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ...services.auto_loop import CONTROLLER as AUTO_LOOP
from ...services import registration as registrar
from ...storage import facade as db

router = APIRouter()


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


# ──────────────────────── 代理连通性测试 ────────────────────────


class ProxyTestReq(BaseModel):
    proxies: list[str] = Field(..., description="要测试的代理列表")
    timeout: int = Field(8, description="每个代理超时秒数")
    test_url: str = Field("https://api.ipify.org?format=json",
                          description="测试目标 URL（默认返回出口 IP）")


@router.post("/api/proxy/test")
def api_proxy_test(req: ProxyTestReq):
    """并发测试代理连通性。复用真实注册流程的 create_http_session（含 socks5->socks5h
    标准化、trust_env=False），保证「测试正常」== 「跑号能用」。返回 ok / 延迟 / 出口 IP。

    协议说明：不写协议的 `ip:port` 被 curl 按 HTTP 代理处理；SOCKS5 需显式写 socks5://。
    """
    import sys as _sys
    ROOT_DIR = Path(__file__).resolve().parents[2]
    if str(ROOT_DIR) not in _sys.path:
        _sys.path.insert(0, str(ROOT_DIR))
    try:
        from regix.http.client import create_http_session
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


@router.get("/api/proxy/pool")
def api_proxy_pool():
    return {"ok": True, "items": db.list_proxies(), "stats": db.proxy_pool_stats()}


@router.post("/api/proxy/pool")
def api_proxy_pool_import(req: ProxyPoolImportReq):
    """批量导入代理。replace 只删掉不在新列表里的，已有代理的用量/启用状态保留。"""
    mode = "append" if str(req.mode or "").lower() == "append" else "replace"
    proxies = [p.strip() for p in (req.text or "").splitlines() if p.strip()]
    result = db.replace_proxies(proxies, mode=mode)
    return {"ok": True, **result, "stats": db.proxy_pool_stats()}


@router.delete("/api/proxy/pool")
def api_proxy_pool_clear():
    n = db.clear_proxies()
    return {"ok": True, "removed": n, "items": db.list_proxies(),
            "stats": db.proxy_pool_stats()}


@router.patch("/api/proxy/pool")
def api_proxy_pool_update(req: ProxyPoolUpdateReq):
    """改单个代理的启用状态 / 上限 / 备注 / 类型（静态 or 动态）。"""
    ok = db.update_proxy(
        req.proxy, enabled=req.enabled, max_uses=req.max_uses, note=req.note,
        proxy_type=req.proxy_type,
    )
    if not ok:
        raise HTTPException(404, "代理不存在")
    return {"ok": True, "items": db.list_proxies(), "stats": db.proxy_pool_stats()}


@router.post("/api/proxy/pool/cf_reset")
def api_proxy_pool_cf_reset(req: ProxyPoolResetReq):
    """清零 CF 拦截计数（代理换了 IP / 供应商修了之后手动点一下）。"""
    n = 1 if req.proxy else db.reset_all_proxy_cf_blocked()
    if req.proxy:
        db.reset_proxy_cf_blocked(req.proxy)
    return {"ok": True, "reset": n, "items": db.list_proxies(),
            "stats": db.proxy_pool_stats()}


@router.post("/api/proxy/pool/ipv6_test")
def api_proxy_pool_ipv6_test(req: ProxyIpv6TestReq):
    """检测代理池里哪些代理支持 IPv6 出口。

    为什么需要：IPv6 的地址空间比 IPv4 大几个数量级，同一个 /64 段里的
    "邻居" 少得多 → 被 Cloudflare 按 IP 段批量拉黑的概率低得多。
    实测日志里 7 次 warmup CF 403 全是 IPv4 出口。

    检测方式：走 cloudflare.com/cdn-cgi/trace，它会同时返回 ip 和 ipv6 两个字段
    —— ip 是客户端 IPv4，ipv6 是客户端 IPv6。有 ipv6 字段且非空 → 支持。
    """
    import sys as _sys
    ROOT_DIR = Path(__file__).resolve().parents[2]
    if str(ROOT_DIR) not in _sys.path:
        _sys.path.insert(0, str(ROOT_DIR))
    try:
        from regix.http.client import create_http_session
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


@router.delete("/api/proxy/pool/{proxy:path}")
def api_proxy_pool_delete(proxy: str):
    ok = db.delete_proxy(proxy)
    if not ok:
        raise HTTPException(404, "代理不存在")
    return {"ok": True, "items": db.list_proxies(), "stats": db.proxy_pool_stats()}


@router.post("/api/proxy/pool/reset")
def api_proxy_pool_reset(req: ProxyPoolResetReq):
    """重置用量计数（不删代理）。"""
    n = db.reset_proxy_counts(req.proxy)
    return {"ok": True, "reset": n, "items": db.list_proxies(),
            "stats": db.proxy_pool_stats()}


@router.post("/api/register")
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


@router.get("/api/runs/{run_id}/stream")
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


@router.get("/api/runs")
def api_runs(limit: int = 50):
    return {"ok": True, "items": db.list_runs(limit=limit)}


@router.get("/api/registered")
def api_registered(limit: int = 20, offset: int = 0, filter: str = "all"):
    items = db.list_registered(limit=limit, offset=offset, filter_rt=filter)
    total = db.count_registered(filter_rt=filter)
    return {"ok": True, "items": items, "total": total}


@router.get("/api/registered/{email}")
def api_registered_one(email: str):
    row = db.get_registered(email)
    if not row:
        raise HTTPException(404, "not found")
    return {"ok": True, "data": row}


@router.delete("/api/registered/{email}")
def api_delete_registered(email: str):
    ok = db.delete_registered(email)
    if not ok:
        raise HTTPException(404, "not found")
    return {"ok": True}


class BulkDeleteRegisteredReq(BaseModel):
    emails: Optional[list[str]] = Field(None, description="按 email 列表删；留空 + all=true 则删全部")
    all: bool = False


@router.post("/api/registered/bulk_delete")
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


@router.post("/api/registered/mark_abnormal")
def api_mark_abnormal(req: MarkAbnormalReq):
    """标注账号异常（被封 / 额度耗尽 / 不可用）。

    标注异常时会**自动释放该号占用的代理配额** —— 号已经废了，
    它的 IP 名额应该还回池子里给别的号用。
    """
    n = db.mark_registered_abnormal_by_emails(req.emails, req.abnormal)
    return {"ok": True, "updated": n, "items": db.list_registered(limit=200),
            "proxy_stats": db.proxy_pool_stats()}


