"""Proxy connectivity and proxy pool API routes."""


from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ...storage import facade as db
from ...http.client import create_http_session

router = APIRouter()


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


