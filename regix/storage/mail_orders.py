"""Remail order persistence and reuse rules."""

from __future__ import annotations

import time
from typing import Optional

from .database import _conn, _lock, REMAIL_MAX_ATTEMPTS


def _remail_order_to_dict(row) -> dict:
    d = dict(row)
    # service_token 是取件凭证，列表页不返回明文（详情/取件时才用）。
    d["has_token"] = bool(d.get("service_token"))
    d.pop("service_token", None)
    d["attempt_count"] = int(d.get("attempt_count") or 0)
    d["disabled"] = bool(d.get("disabled"))
    # 最大复用次数，前端显示「已用 N / 上限 M」用，别在前端硬编码。
    d["max_attempts"] = REMAIL_MAX_ATTEMPTS
    # 可复用 = 长效 + 没关联账号 + 状态有效 + 有 token + 未禁用 + 尝试次数没超限。
    # 前端据此显示"可复用"标签，并提示下一个注册任务会直接用它（不再扣费）。
    d["reusable"] = bool(
        str(d.get("service_mode") or "").lower() == "purchase"
        and not (d.get("used_email") or "").strip()
        and d.get("has_token")
        and str(d.get("order_status") or "") in ("paid", "active", "completed")
        and not d["disabled"]
        and d["attempt_count"] < REMAIL_MAX_ATTEMPTS
    )
    return d


def save_remail_order(d: dict) -> None:
    """记录一笔 Remail 订单。

    Remail 不交付账号密码，只交付「邮箱地址 + serviceToken」。
    后续所有取件、重复收件、查看邮箱类型都读这张表，不再回查 Remail 接口。
    """
    order_no = str(d.get("order_no") or "").strip()
    if not order_no:
        return
    with _lock:
        con = _conn()
        # ⚠️ used_email / disabled / attempt_count 不能被覆盖成默认值：
        #    这张表用 INSERT OR REPLACE（整行替换），不显式带上就会冲掉
        #    —— used_email 被冲空 → 那个邮箱会被重复注册；
        #    disabled 被冲掉 → 手动禁用的订单又会被复用。
        #    所以先读旧值，没传就沿用。
        prev_used = ""
        prev_attempt = 0
        prev_disabled = 0
        row = con.execute(
            "SELECT used_email, attempt_count, disabled FROM remail_orders WHERE order_no=?",
            (order_no,),
        ).fetchone()
        if row:
            prev_used = (row["used_email"] or "").strip()
            prev_attempt = int(row["attempt_count"] or 0)
            prev_disabled = int(row["disabled"] or 0)

        con.execute(
            "INSERT OR REPLACE INTO remail_orders "
            "(order_no, email, suffix, service_mode, supply_policy, project_id, "
            " order_status, service_token, pay_amount, received_at, "
            " receive_started_at, after_sale_until, used_email, attempt_count, "
            " last_error, disabled, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                order_no,
                str(d.get("email") or "").strip(),
                str(d.get("suffix") or "").strip(),
                str(d.get("service_mode") or "").strip(),
                str(d.get("supply_policy") or "").strip(),
                d.get("project_id"),
                str(d.get("order_status") or "").strip(),
                str(d.get("service_token") or "").strip(),
                str(d.get("pay_amount") or "").strip(),
                d.get("received_at") or time.time(),
                str(d.get("receive_started_at") or "").strip(),
                str(d.get("after_sale_until") or "").strip(),
                str(d.get("used_email") or "").strip() or prev_used,
                int(d.get("attempt_count") if d.get("attempt_count") is not None else prev_attempt),
                d.get("last_error"),
                int(d.get("disabled") if d.get("disabled") is not None else prev_disabled),
                d.get("created_at") or time.time(),
            ),
        )
        con.commit()


def list_remail_orders(limit: int = 200, offset: int = 0) -> list[dict]:
    con = _conn()
    cur = con.execute(
        "SELECT * FROM remail_orders ORDER BY received_at DESC LIMIT ? OFFSET ?",
        (limit, offset),
    )
    return [_remail_order_to_dict(r) for r in cur.fetchall()]


def get_remail_order(order_no: str, *, with_token: bool = False) -> Optional[dict]:
    con = _conn()
    cur = con.execute(
        "SELECT * FROM remail_orders WHERE order_no=?", (str(order_no or "").strip(),)
    )
    row = cur.fetchone()
    if not row:
        return None
    d = dict(row)
    if not with_token:
        d["has_token"] = bool(d.get("service_token"))
        d.pop("service_token", None)
    return d


def link_remail_order(order_no: str, email: str) -> bool:
    """把订单关联到本地注册成功的账号。

    顺带：短效（code）订单关联成功后即视为「已消耗」——
    它的取件凭证是一次性的，用完就该标 consumed，而不是继续挂在 active。
    长效（purchase）订单保持 active（还能重复收件）。
    """
    order_no = str(order_no or "").strip()
    with _lock:
        con = _conn()
        cur = con.execute(
            "UPDATE remail_orders SET used_email=? WHERE order_no=?",
            (str(email or "").strip().lower(), order_no),
        )
        con.commit()
        rc = cur.rowcount > 0
    if rc:
        _consume_short_lived_order(order_no)
    return rc


def update_remail_order_status(order_no: str, status: str) -> bool:
    with _lock:
        con = _conn()
        cur = con.execute(
            "UPDATE remail_orders SET order_status=? WHERE order_no=?",
            (str(status or "").strip(), str(order_no or "").strip()),
        )
        con.commit()
        return cur.rowcount > 0


def set_remail_order_disabled(order_no: str, disabled: bool = True) -> bool:
    """本地禁用 / 启用一笔 Remail 订单。

    ⚠️ 用**独立的 disabled 列**，不改 order_status ——
    order_status 是 Remail 服务端返回的状态（paid/active/completed…），
    我们不该把本地的"我不想再用这个"混进去，否则就分不清
    "服务端说这单废了"和"我手动不用了"。

    禁用的订单不会被复用（claim_reusable_remail_order 会跳过）。
    """
    order_no = str(order_no or "").strip()
    if not order_no:
        return False
    with _lock:
        con = _conn()
        cur = con.execute(
            "UPDATE remail_orders SET disabled=? WHERE order_no=?",
            (1 if disabled else 0, order_no),
        )
        con.commit()
        return cur.rowcount > 0


def delete_remail_order(order_no: str) -> bool:
    """从本地删除一笔 Remail 订单记录。

    ⚠️ 只删**本地记录**，不会去 Remail 退单/退款 —— 那个要走服务端的售后流程
    （Order.afterSaleUntil）。这里只是让主人不再看到它、不再复用它。
    """
    order_no = str(order_no or "").strip()
    if not order_no:
        return False
    with _lock:
        con = _conn()
        cur = con.execute(
            "DELETE FROM remail_orders WHERE order_no=?", (order_no,)
        )
        con.commit()
        return cur.rowcount > 0


def delete_remail_orders(order_nos: list[str]) -> int:
    """批量删除本地 Remail 订单记录。"""
    cleaned = [str(o or "").strip() for o in (order_nos or []) if str(o or "").strip()]
    if not cleaned:
        return 0
    with _lock:
        con = _conn()
        placeholders = ",".join("?" * len(cleaned))
        cur = con.execute(
            f"DELETE FROM remail_orders WHERE order_no IN ({placeholders})", cleaned
        )
        con.commit()
        return cur.rowcount


def find_remail_order_by_email(email: str) -> Optional[dict]:
    con = _conn()
    cur = con.execute(
        "SELECT * FROM remail_orders WHERE email=? ORDER BY received_at DESC LIMIT 1",
        (str(email or "").strip().lower(),),
    )
    row = cur.fetchone()
    if not row:
        return None
    d = dict(row)
    d["has_token"] = bool(d.get("service_token"))
    d.pop("service_token", None)
    return d


def claim_reusable_remail_order(
    *, service_mode: str = "purchase", suffix: str = "", project_id=None,
    max_attempts: int = REMAIL_MAX_ATTEMPTS,
) -> Optional[dict]:
    """找一笔可以复用的 Remail 订单，返回**含 service_token** 的完整记录。

    复用条件（全部满足）：
      1. service_mode = purchase（**长效**订单才可重复收件；code 短效是一次性的）
      2. 没有关联账号（used_email 为空）—— 已注册成功的号不能再拿来注册
      3. 订单还有 service_token（没它就取不到件）
      4. 订单状态是有效的（paid / active / completed 都算；
         refunded / failed / closed 的不能再用）
      5. suffix / project_id 匹配（不同后缀的邮箱不能混用）
      6. 尝试次数没超限（attempt_count < max_attempts）——
         同一个邮箱反复失败就不要再复用了，否则会卡死在一个坏邮箱上

    为什么要复用：长效订单是**已经付过钱**的邮箱，注册失败（OTP 超时、
    409、风控）之后那个邮箱本身还是好的 —— 换个号重新注册完全可行，
    不该再花一次钱买新的。这正是主人说的"没关联账号的就要复用"。

    挑哪一笔：最早下的一笔（received_at 升序）—— 优先把已经付过钱的用掉，
    避免囤着一堆闲置订单还继续买新的。
    """
    mode = str(service_mode or "purchase").strip().lower()
    if mode != "purchase":
        # 短效订单一次性，用完就没了，不存在"复用"
        return None
    sf = str(suffix or "").strip()
    pid = project_id

    con = _conn()
    sql = (
        "SELECT * FROM remail_orders "
        "WHERE service_mode='purchase' "
        "AND (used_email IS NULL OR used_email='') "
        "AND service_token IS NOT NULL AND service_token!='' "
        "AND order_status IN ('paid','active','completed') "
        "AND COALESCE(disabled, 0)=0 "
        "AND COALESCE(attempt_count, 0) < ?"
    )
    params: list = [max(1, int(max_attempts))]
    if sf:
        sql += " AND suffix=?"
        params.append(sf)
    if pid is not None:
        sql += " AND project_id=?"
        params.append(int(pid))
    sql += " ORDER BY received_at ASC LIMIT 1"

    cur = con.execute(sql, params)
    row = cur.fetchone()
    if not row:
        return None
    return dict(row)


def mark_remail_order_attempt(order_no: str, ok: bool, error: str = "") -> bool:
    """记录一次注册尝试的结果。

    失败 → attempt_count +1（超过上限就不再被复用，避免死循环用一个坏邮箱）
    成功 → 由 registrar 走 link_remail_order 关联账号，不再计数
    """
    order_no = str(order_no or "").strip()
    if not order_no:
        return False
    with _lock:
        con = _conn()
        if ok:
            cur = con.execute(
                "UPDATE remail_orders SET attempt_count=COALESCE(attempt_count,0)+1, "
                "last_error=NULL WHERE order_no=?",
                (order_no,),
            )
        else:
            cur = con.execute(
                "UPDATE remail_orders SET attempt_count=COALESCE(attempt_count,0)+1, "
                "last_error=? WHERE order_no=?",
                (str(error or "")[:200], order_no),
            )
        con.commit()
        rc = cur.rowcount > 0

    # 短效订单进入终态 → 状态置为「已消耗」
    # ⚠️ 只在短效（code）订单上做。长效（purchase）能重复收件，
    #    它的 active 是**真实状态**，不能改成 consumed。
    if rc:
        _consume_short_lived_order(order_no)
    return rc


def _consume_short_lived_order(order_no: str) -> bool:
    """短效（code）订单用尽后，把状态置为 consumed（已消耗）。

    背景：短效订单的 serviceToken 是**一次性凭证**，实测取件返回
    `401 Credential is invalid or expired` —— 凭证早已失效，
    但订单状态还写着 'active'，是**假状态**（主人反馈过这个现象）。
    """
    order_no = str(order_no or "").strip()
    if not order_no:
        return False
    with _lock:
        con = _conn()
        cur = con.execute(
            "UPDATE remail_orders SET order_status='consumed' "
            "WHERE order_no=? AND service_mode='code' "
            "AND (order_status IS NULL OR order_status NOT IN ('consumed','refunded'))",
            (order_no,),
        )
        con.commit()
        return cur.rowcount > 0


