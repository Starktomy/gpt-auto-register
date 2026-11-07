"""Proxy pool persistence and selection."""

from __future__ import annotations

import random
import time
from typing import Optional

from .database import _conn, _lock


# ──────────────────────── 代理池 ────────────────────────
#
# 代理池原来只存在前端 localStorage（stores/proxy.js，纯字符串数组），
# 后端拿不到任何元数据 → 既没法限流，也没法在某个代理被风控后自动换走。
# 现在迁到 SQLite，所有状态（启用/上限/用量）都由后端持有，前端只做展示。


def _proxy_row_to_dict(row) -> dict:
    d = dict(row)
    d["enabled"] = bool(d.get("enabled"))
    d["max_uses"] = int(d.get("max_uses") or 0)
    d["used_count"] = int(d.get("used_count") or 0)
    d["failed_count"] = int(d.get("failed_count") or 0)
    d["cf_blocked"] = int(d.get("cf_blocked") or 0)
    # 代理类型：static（固定出口 IP）/ dynamic（每次请求换 IP）。
    # 被 CF 403 拦时两者的处置完全不同，见表结构注释。
    pt = str(d.get("proxy_type") or "static").strip().lower()
    d["proxy_type"] = pt if pt in ("static", "dynamic") else "static"
    d["ipv6_status"] = str(d.get("ipv6_status") or "untested")
    d["exhausted"] = bool(d["max_uses"] and d["used_count"] >= d["max_uses"])
    d["status"] = (
        "disabled" if not d["enabled"]
        else ("exhausted" if d["exhausted"] else "available")
    )
    return d


def list_proxies() -> list[dict]:
    """列出全部代理及其用量状态。"""
    con = _conn()
    cur = con.execute(
        "SELECT * FROM proxy_pool ORDER BY rowid ASC"
    )
    return [_proxy_row_to_dict(r) for r in cur.fetchall()]


def replace_proxies(proxies: list[str], *, mode: str = "replace") -> dict:
    """批量导入代理。

    mode="replace"：清空后写入（保留已有代理的 enabled/上限/用量？
                  不保留 —— 「覆盖保存」的语义就是重来，但用量清零会让
                  已经注册成功的号对应的代理计数丢失。
                  这里改为：replace 只清掉**不在新列表里**的代理，
                  新列表里已存在的代理保留其状态和用量。）
    mode="append"：只追加新代理，已存在的保持不变。
    """
    cleaned: list[str] = []
    seen = set()
    for p in proxies:
        s = str(p or "").strip()
        if not s or s in seen:
            continue
        seen.add(s)
        cleaned.append(s)

    with _lock:
        con = _conn()
        now = time.time()
        if mode == "replace":
            # 删掉不在新列表里的
            if cleaned:
                placeholders = ",".join("?" for _ in cleaned)
                con.execute(
                    f"DELETE FROM proxy_pool WHERE proxy NOT IN ({placeholders})",
                    cleaned,
                )
            else:
                con.execute("DELETE FROM proxy_pool")
        # 新增（已存在的跳过，保留状态和用量）
        for s in cleaned:
            con.execute(
                "INSERT OR IGNORE INTO proxy_pool(proxy, created_at) VALUES (?, ?)",
                (s, now),
            )
        con.commit()

    items = list_proxies()
    return {"total": len(cleaned), "items": items}


def update_proxy(proxy: str, *, enabled: Optional[bool] = None,
                 max_uses: Optional[int] = None, note: Optional[str] = None,
                 proxy_type: Optional[str] = None) -> bool:
    """改单个代理的启用状态 / 上限 / 备注 / 类型。返回是否命中。"""
    proxy = str(proxy or "").strip()
    if not proxy:
        return False
    sets, vals = [], []
    if enabled is not None:
        sets.append("enabled=?")
        vals.append(1 if enabled else 0)
    if max_uses is not None:
        sets.append("max_uses=?")
        vals.append(max(0, int(max_uses)))
    if note is not None:
        sets.append("note=?")
        vals.append(str(note))
    if proxy_type is not None:
        pt = str(proxy_type or "").strip().lower()
        if pt not in ("static", "dynamic"):
            return False
        sets.append("proxy_type=?")
        vals.append(pt)
    if not sets:
        return False
    with _lock:
        con = _conn()
        cur = con.execute(
            f"UPDATE proxy_pool SET {', '.join(sets)} WHERE proxy=?",
            [*vals, proxy],
        )
        con.commit()
        return cur.rowcount > 0


def delete_proxy(proxy: str) -> bool:
    proxy = str(proxy or "").strip()
    if not proxy:
        return False
    with _lock:
        con = _conn()
        cur = con.execute("DELETE FROM proxy_pool WHERE proxy=?", (proxy,))
        con.commit()
        return cur.rowcount > 0


def clear_proxies() -> int:
    with _lock:
        con = _conn()
        cur = con.execute("DELETE FROM proxy_pool")
        con.commit()
        return cur.rowcount


def reset_proxy_counts(proxy: Optional[str] = None) -> int:
    """重置用量计数。proxy 为空则重置全部。"""
    with _lock:
        con = _conn()
        if proxy:
            cur = con.execute(
                "UPDATE proxy_pool SET used_count=0, failed_count=0 WHERE proxy=?",
                (str(proxy).strip(),),
            )
        else:
            cur = con.execute(
                "UPDATE proxy_pool SET used_count=0, failed_count=0"
            )
        con.commit()
        return cur.rowcount


def proxy_used(proxy: str, *, ok: bool) -> bool:
    """登记一次注册结果。

    成功 → used_count += 1（占用配额）
    失败 → 只 failed_count += 1，**不占配额**（按需求：失败不计入上限）
    """
    proxy = str(proxy or "").strip()
    if not proxy:
        return False
    with _lock:
        con = _conn()
        if ok:
            con.execute(
                "UPDATE proxy_pool SET used_count=used_count+1, last_used_at=? "
                "WHERE proxy=?",
                (time.time(), proxy),
            )
        else:
            con.execute(
                "UPDATE proxy_pool SET failed_count=failed_count+1, last_used_at=? "
                "WHERE proxy=?",
                (time.time(), proxy),
            )
        con.commit()
    return True


def proxy_release(proxy: str) -> bool:
    """释放一个代理的配额（账号被删除 / 标注异常时调用）。

    used_count 不会减到负数 —— 重复释放是安全的。
    """
    proxy = str(proxy or "").strip()
    if not proxy:
        return False
    with _lock:
        con = _conn()
        cur = con.execute(
            "UPDATE proxy_pool SET used_count=MAX(0, used_count-1) WHERE proxy=?",
            (proxy,),
        )
        con.commit()
        return cur.rowcount > 0


def find_proxy(proxy: str) -> Optional[dict]:
    """查单个代理的详情；不在池里返回 None。"""
    proxy = str(proxy or "").strip()
    if not proxy:
        return None
    con = _conn()
    cur = con.execute("SELECT * FROM proxy_pool WHERE proxy=?", (proxy,))
    row = cur.fetchone()
    return _proxy_row_to_dict(row) if row else None


def reserve_proxy(proxy: str) -> bool:
    """给指定代理预占一格配额。成功返回 True。

    用于「单次注册」——用户手选的代理也要计入池子的配额口径，
    否则同一个代理可以靠单次注册无限注册，限流就形同虚设。
    代理不在池里（或已满/停用）时返回 False，不占用。
    """
    proxy = str(proxy or "").strip()
    if not proxy:
        return False
    with _lock:
        con = _conn()
        rc = con.execute(
            "UPDATE proxy_pool SET used_count=used_count+1, last_used_at=? "
            "WHERE proxy=? AND enabled=1 AND (max_uses=0 OR used_count < max_uses)",
            (time.time(), proxy),
        )
        con.commit()
        return rc.rowcount > 0


def pick_proxy() -> str:
    """挑一个可用代理：启用中 + 未超配额 + 用得少的优先。

    ⚠️ 为什么不能沿用 auto_loop 里 worker_id % len(pool) 的取模：
    取模是**静态绑定** —— worker-0 永远拿第 1 个代理、worker-1 永远拿第 2 个。
    某个代理被 OpenAI 风控后，所有 worker 会一直死磕那个 IP，永不换走，
    这正是"同一 IP 账号太多 → 全挂"的根因。

    现在改成**每次动态挑**，且优先挑用得少的（天然均衡负载）。

    ★ 关键：**选中即预占一格**（used_count +1）。
    不预占的话，两个 worker 会在同一瞬间读到同一个 used_count，
    都挑中同一个代理 —— 并发时等于没做均衡。预占后下一个 worker 就会
    自动避开它，去用另一个还没人用的代理。

    预占的结算：
      - 注册成功 → 什么都不做（预占的那格就是这次的配额）
      - 注册失败 → 调用 proxy_release 把这格还回去（失败不占配额）

    池里没有可用代理时返回空串（调用方负责停下并提示）。
    """
    con = _conn()
    cur = con.execute(
        "SELECT * FROM proxy_pool "
        "WHERE enabled=1 AND (max_uses=0 OR used_count < max_uses) "
        "ORDER BY used_count ASC, failed_count ASC, rowid ASC"
    )
    rows = cur.fetchall()
    if not rows:
        return ""
    # 同分（used_count 相同）的候选里随机挑一个，避免多个 worker 同时挤同一个。
    best = min(int(r["used_count"] or 0) for r in rows)
    candidates = [str(r["proxy"]) for r in rows if int(r["used_count"] or 0) == best]
    if len(candidates) == 1:
        chosen = candidates[0]
    else:
        chosen = str(random.choice(candidates))

    # 预占一格：并发 worker 不会同时挑中同一个代理。
    # 用 UPDATE ... WHERE used_count < max_uses 保证不会超预，
    # 条件不满足说明刚被别人抢满 → 这次放弃，让调用方下一轮再取。
    with _lock:
        con = _conn()
        rc = con.execute(
            "UPDATE proxy_pool SET used_count=used_count+1, last_used_at=? "
            "WHERE proxy=? AND enabled=1 AND (max_uses=0 OR used_count < max_uses)",
            (time.time(), chosen),
        )
        con.commit()
        if rc.rowcount == 0:
            return ""
    return chosen


# 被 CF 403 拦截时，静态代理连续被拦多少次就自动停用。
#
# 阈值定 2 的理由：
#   · 1 次就停太激进 —— 偶尔一次 403 可能只是瞬时抖动
#   · 连续 2 次基本可确定：这个出口 IP 稳定地被 CF 拒
# 动态代理不受此限制（换 IP 可能就通过了），见 mark_proxy_cf_blocked。
PROXY_CF_BLOCK_THRESHOLD = 2


def mark_proxy_cf_blocked(proxy: str) -> dict:
    """记录一次 Cloudflare 403 拦截，按代理类型决定后续动作。

    返回 {"cf_blocked": n, "auto_disabled": bool, "proxy_type": str}

    ⚠️ 核心逻辑（主人要求）：
      · **static（固定出口 IP）**：这个 IP 已经被 CF 拉黑，换多少次都一样
        → 连续被拦达阈值就**自动停用**，别再拿它试了
      · **dynamic（每次请求换 IP）**：代理只是入口，换个出口可能就通过
        → **不停用**，只记次数（供主人判断这个代理池质量）

    这是静态/动态两种代理在「被 CF 拦」时必须区别对待的根本原因：
    对 static 而言，"再试一次"是纯浪费；对 dynamic 而言，"再试一次"有可能成功。
    """
    proxy = str(proxy or "").strip()
    if not proxy:
        return {"cf_blocked": 0, "auto_disabled": False, "proxy_type": "static"}
    with _lock:
        con = _conn()
        cur = con.execute(
            "UPDATE proxy_pool SET cf_blocked=cf_blocked+1 WHERE proxy=?", (proxy,)
        )
        con.commit()
        row = con.execute(
            "SELECT cf_blocked, proxy_type FROM proxy_pool WHERE proxy=?", (proxy,)
        ).fetchone()
    if not row:
        return {"cf_blocked": 0, "auto_disabled": False, "proxy_type": "static"}
    n = int(row["cf_blocked"] or 0)
    ptype = str(row["proxy_type"] or "static")
    auto_disabled = False
    if ptype == "static" and n >= PROXY_CF_BLOCK_THRESHOLD:
        with _lock:
            con = _conn()
            con.execute(
                "UPDATE proxy_pool SET enabled=0 WHERE proxy=? AND enabled=1", (proxy,)
            )
            con.commit()
        auto_disabled = True
    return {"cf_blocked": n, "auto_disabled": auto_disabled, "proxy_type": ptype}


def reset_proxy_cf_blocked(proxy: str) -> bool:
    """清零某个代理的 CF 拦截计数（主人手动点「重置」时用）。"""
    proxy = str(proxy or "").strip()
    if not proxy:
        return False
    with _lock:
        con = _conn()
        cur = con.execute(
            "UPDATE proxy_pool SET cf_blocked=0 WHERE proxy=?", (proxy,)
        )
        con.commit()
        return cur.rowcount > 0


def reset_all_proxy_cf_blocked() -> int:
    """清零全部代理的 CF 拦截计数。"""
    with _lock:
        con = _conn()
        cur = con.execute("UPDATE proxy_pool SET cf_blocked=0 WHERE cf_blocked>0")
        con.commit()
        return cur.rowcount


def set_proxy_ipv6_status(proxy: str, status: str) -> bool:
    """记录代理的 IPv6 检测结果（ok / fail / untested）。"""
    proxy = str(proxy or "").strip()
    if not proxy:
        return False
    st = str(status or "untested").strip().lower()
    if st not in ("ok", "fail", "untested"):
        st = "untested"
    with _lock:
        con = _conn()
        cur = con.execute(
            "UPDATE proxy_pool SET ipv6_status=?, ipv6_checked_at=? WHERE proxy=?",
            (st, time.time(), proxy),
        )
        con.commit()
        return cur.rowcount > 0


def proxy_pool_stats() -> dict:
    """代理池概览，供前端展示「可用 N / 停用 M / 已满 K」。"""
    items = list_proxies()
    return {
        "total": len(items),
        "available": sum(1 for i in items if i["status"] == "available"),
        "disabled": sum(1 for i in items if i["status"] == "disabled"),
        "exhausted": sum(1 for i in items if i["status"] == "exhausted"),
    }


# ──────────────────────── Remail 订单 ────────────────────────
