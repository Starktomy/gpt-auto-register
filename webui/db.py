"""SQLite 号池 + 注册结果存储。

表结构：
  outlook_accounts: 接码号池（多种邮箱混放，kind 列区分 + 状态机）
  registered:       注册成功结果（凭证 JSON）

关于 outlook_accounts 这个表名：
    它现在装的不止 outlook（还有 gmail / icloud / qq ...），名字已经不准，
    但改表名要动迁移和一堆 SQL，收益只是好看一点，风险不值。
    真正区分类型的是 kind 列。

凭证字段用「并集列」而不是 extra_json：
    outlook/gmail 用 password+client_id+refresh_token，
    icloud 这类中转只用 relay_url，各自把不用的列留空。
    几种邮箱的规模下，并集列比 JSON 好 —— 能建索引、能加约束、
    SQL 里直接看得见。加新邮箱时如果要新字段，就再 ALTER 加一列。
"""
from __future__ import annotations

import base64
import json
import os
import random
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

DB_PATH = Path(
    os.environ.get("WEBUI_DB_PATH")
    or (Path(__file__).resolve().parent / "webui.db")
)
# ⚠️ DB_PATH 支持用环境变量 WEBUI_DB_PATH 覆盖 ——
#    Docker 部署时要把数据挂出来（容器重建不丢号池/凭证/订单），
#    默认写在代码目录里容器一删就没了。

# Remail 长效订单的**最大复用次数**。
# 一个已付费的长效邮箱可以被反复拿来注册，直到这个次数用完就不再复用
# （防止死循环卡在一个始终失败的邮箱上）。
REMAIL_MAX_ATTEMPTS = 10

_lock = threading.Lock()  # SQLite 写入串行化


def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init_db():
    con = _conn()
    con.executescript("""
        CREATE TABLE IF NOT EXISTS outlook_accounts (
            email           TEXT PRIMARY KEY,
            password        TEXT,
            client_id       TEXT,
            refresh_token   TEXT,
            relay_url       TEXT,       -- 中转取码 URL（icloud 类用，其余留空）
            kind            TEXT NOT NULL DEFAULT 'outlook',
                            -- 邮箱类型，对应 mail_providers 注册表的 kind
            status          TEXT NOT NULL DEFAULT 'available',
                            -- available / in_use / done / failed
            imported_at     REAL,
            claimed_at      REAL,
            finished_at     REAL,
            fail_reason     TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_outlook_status ON outlook_accounts(status);
        -- idx_outlook_kind 不在这里建：老库此刻还没有 kind 列，
        -- 建索引会当场报错。放到下面补完列之后再建。

        CREATE TABLE IF NOT EXISTS settings (
            key     TEXT PRIMARY KEY,
            value   TEXT
        );

        CREATE TABLE IF NOT EXISTS registered (
            email           TEXT PRIMARY KEY,
            password        TEXT,
            access_token    TEXT,
            session_token   TEXT,
            refresh_token   TEXT,
            id_token        TEXT,
            device_id       TEXT,
            csrf_token      TEXT,
            cookie_header   TEXT,
            totp_secret     TEXT,
            totp_factor_id  TEXT,
            -- /api/auth/session 的完整响应 JSON 原文（一行紧凑 JSON）。
            -- 里面有 accessToken / sessionToken / expires / user.* 等，
            -- 是这个号的凭证全集，比拆开的几列信息更全。
            session_json    TEXT,
            extra_json      TEXT,
            created_at      REAL
        );

        CREATE TABLE IF NOT EXISTS runs (
            run_id          TEXT PRIMARY KEY,
            email           TEXT,
            status          TEXT,        -- running / done / failed
            started_at      REAL,
            finished_at     REAL,
            log_path        TEXT,
            error           TEXT,
            error_category  TEXT         -- network / account / unknown
        );

        -- 代理池。原来代理只存在前端 localStorage（纯字符串数组），
        -- 后端完全不知道每个代理用了几次 → 没法限流，也没法在风控后自动换代理。
        -- 迁到后端后才有「单代理最多注册 N 个」「启用/停用」「用量持久化」这些能力。
        CREATE TABLE IF NOT EXISTS proxy_pool (
            proxy        TEXT PRIMARY KEY,
            enabled      INTEGER NOT NULL DEFAULT 1,   -- 0 = 停用，不参与注册
            max_uses     INTEGER NOT NULL DEFAULT 50,  -- 成功注册数上限，0 = 不限
            used_count   INTEGER NOT NULL DEFAULT 0,   -- 已成功注册数（占配额）
            failed_count INTEGER NOT NULL DEFAULT 0,   -- 失败次数（仅统计，不占配额）
            -- 代理类型：static = 固定出口 IP / dynamic = 每次请求换 IP
            --
            -- ⚠️ 为什么必须区分：被 Cloudflare 403 拦时，两者的正确处置**完全不同**。
            --    · static：出口 IP 固定 → 这个 IP 已经被 CF 拉黑，换多少次都一样
            --      → 直接停用，别再拿它试了
            --    · dynamic：代理本身是入口，每次连接可换出口 IP → 同一个代理
            --      换个 IP 可能就通过了 → 不停用，标记一下继续用
            proxy_type   TEXT NOT NULL DEFAULT 'static',
            -- 被 CF 403 拦截的次数（连续被拦到阈值 → 自动停用 static 代理）
            cf_blocked   INTEGER NOT NULL DEFAULT 0,
            -- IPv6 检测结果（'untested' / 'ok' / 'fail'），代理池页面展示
            ipv6_status  TEXT NOT NULL DEFAULT 'untested',
            ipv6_checked_at REAL,
            last_used_at REAL,
            note         TEXT,
            created_at   REAL
        );

        -- Remail 订单。Remail 不交付账号密码，只交付「邮箱地址 + serviceToken」
        -- 这一对取件凭证，所以本地必须自己记一份：后续取件、重复收件、
        -- 看邮箱类型全部读这里，不再回查 Remail 的 projects / orders 接口。
        CREATE TABLE IF NOT EXISTS remail_orders (
            order_no          TEXT PRIMARY KEY,
            email             TEXT,        -- deliveryEmail
            suffix            TEXT,        -- 下单时的 emailSuffix（邮箱类型）
            service_mode      TEXT,        -- purchase / code
            supply_policy     TEXT,
            project_id        INTEGER,
            order_status      TEXT,
            service_token     TEXT,        -- 取件凭证（明文，本机自用）
            pay_amount        TEXT,
            received_at       REAL,
            receive_started_at TEXT,
            after_sale_until  TEXT,
            used_email        TEXT,        -- 关联到本地注册成功的账号
            attempt_count     INTEGER NOT NULL DEFAULT 0,
                            -- 这个邮箱被拿去注册过几次。用来防止
                            -- 死循环复用同一个坏邮箱（每次都失败又每次都复用它）。
            last_error        TEXT,        -- 最近一次失败原因，便于人工判断
            disabled          INTEGER NOT NULL DEFAULT 0,
                            -- 本地手动禁用（1=禁用，不再被复用）
            created_at        REAL
        );
    """)
    con.commit()
    # 老 DB migrate：registered 后加的 session_json 列。
    # ⚠️ registered 是最老的表，存量库必须补这一列，否则 INSERT 报
    #    "table registered has no column named session_json"。
    cur = con.execute("PRAGMA table_info(registered)")
    reg_cols = {r[1] for r in cur.fetchall()}
    if "session_json" not in reg_cols:
        con.execute("ALTER TABLE registered ADD COLUMN session_json TEXT")
        con.commit()

    # 老 DB migrate：proxy_pool 后加的三列（proxy_type / cf_blocked / ipv6_*）
    cur = con.execute("PRAGMA table_info(proxy_pool)")
    pp_cols = {r[1] for r in cur.fetchall()}
    for col, ddl in (
        ("proxy_type", "ALTER TABLE proxy_pool ADD COLUMN proxy_type TEXT NOT NULL DEFAULT 'static'"),
        ("cf_blocked", "ALTER TABLE proxy_pool ADD COLUMN cf_blocked INTEGER NOT NULL DEFAULT 0"),
        ("ipv6_status", "ALTER TABLE proxy_pool ADD COLUMN ipv6_status TEXT NOT NULL DEFAULT 'untested'"),
        ("ipv6_checked_at", "ALTER TABLE proxy_pool ADD COLUMN ipv6_checked_at REAL"),
    ):
        if col not in pp_cols:
            con.execute(ddl)
            con.commit()

    # 老 DB migrate：remail_orders 是后加的表，存量库要补这几列
    cur = con.execute("PRAGMA table_info(remail_orders)")
    rc_cols = {r[1] for r in cur.fetchall()}
    for col, ddl in (
        ("attempt_count", "ALTER TABLE remail_orders ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0"),
        ("last_error", "ALTER TABLE remail_orders ADD COLUMN last_error TEXT"),
        ("disabled", "ALTER TABLE remail_orders ADD COLUMN disabled INTEGER NOT NULL DEFAULT 0"),
    ):
        if col not in rc_cols:
            con.execute(ddl)
            con.commit()
    # 老 DB migrate：error_category 在后期才加，对已建表补列
    cur = con.execute("PRAGMA table_info(runs)")
    cols = {r[1] for r in cur.fetchall()}
    if "error_category" not in cols:
        con.execute("ALTER TABLE runs ADD COLUMN error_category TEXT")
        con.commit()

    # 老 DB migrate：号池多邮箱混放（kind / relay_url 在后期才加）
    # 存量行全部是 outlook 时代导进去的，DEFAULT 'outlook' 正好把它们
    # 归位，不需要额外 UPDATE。重复执行无副作用。
    cur = con.execute("PRAGMA table_info(outlook_accounts)")
    acc_cols = {r[1] for r in cur.fetchall()}
    if "kind" not in acc_cols:
        con.execute(
            "ALTER TABLE outlook_accounts ADD COLUMN kind TEXT NOT NULL DEFAULT 'outlook'"
        )
        con.commit()
    if "relay_url" not in acc_cols:
        con.execute("ALTER TABLE outlook_accounts ADD COLUMN relay_url TEXT")
        con.commit()
    # 索引建在补列之后，否则老库上 CREATE INDEX 会因为没有 kind 列而失败
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_outlook_kind ON outlook_accounts(kind, status)"
    )
    con.commit()

    # 老 DB migrate：registered 的 2FA 两列（totp_secret / totp_factor_id）后期才加。
    # secret 一次性下发、服务端取不回，务必单独补列持久化。重复执行无副作用。
    cur = con.execute("PRAGMA table_info(registered)")
    reg_cols = {r[1] for r in cur.fetchall()}
    if "totp_secret" not in reg_cols:
        con.execute("ALTER TABLE registered ADD COLUMN totp_secret TEXT")
        con.commit()
    if "totp_factor_id" not in reg_cols:
        con.execute("ALTER TABLE registered ADD COLUMN totp_factor_id TEXT")
        con.commit()


# ──────────────────────── outlook 号池 ────────────────────────


def parse_lines(text: str, kind: str = "") -> list[dict]:
    """解析导入文本，委托给 mail_providers 注册表。

    kind 指定 → 用该 provider 的格式解析（推荐）
    kind 为空 → 按段数猜（段数唯一时才行，Outlook/Gmail 都是 4 段会猜不出）

    非法行抛 ImportValidationError（带行号和原因），**不再静默跳过**。
    以前这里是 `if len(parts) != 4: continue`，用户看到"导入成功"
    但号少了几个，完全没法排查。
    """
    from mail_providers import parse_import_text

    return parse_import_text(text or "", kind)


def import_accounts(text: str, kind: str = "") -> dict:
    """批量入库。已存在的 email 仅在凭证变化时更新。

    解析阶段全对才写：有一行非法就整批拒绝（抛 ImportValidationError），
    不会出现"写进去一半"对不上账的情况。
    """
    rows = parse_lines(text, kind)
    now = time.time()
    inserted = updated = skipped = 0
    with _lock:
        con = _conn()
        for r in rows:
            row_kind = r.get("kind") or kind or "outlook"
            # 凭证并集：不同 provider 用不同子集，没有的留空字符串
            password = r.get("password", "") or ""
            client_id = r.get("client_id", "") or ""
            refresh = r.get("refresh_token", "") or ""
            relay = r.get("relay_url", "") or ""

            cur = con.execute(
                "SELECT refresh_token, relay_url, kind FROM outlook_accounts WHERE email=?",
                (r["email"],),
            )
            existing = cur.fetchone()
            if existing is None:
                con.execute(
                    "INSERT INTO outlook_accounts(email, password, client_id, refresh_token, "
                    "relay_url, kind, status, imported_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'available', ?)",
                    (r["email"], password, client_id, refresh, relay, row_kind, now),
                )
                inserted += 1
            elif (
                (existing["refresh_token"] or "") != refresh
                or (existing["relay_url"] or "") != relay
                or (existing["kind"] or "") != row_kind
            ):
                # 凭证或类型变了 → 覆盖并重置为可用
                con.execute(
                    "UPDATE outlook_accounts SET refresh_token=?, password=?, client_id=?, "
                    "relay_url=?, kind=?, status='available', imported_at=?, fail_reason=NULL "
                    "WHERE email=?",
                    (refresh, password, client_id, relay, row_kind, now, r["email"]),
                )
                updated += 1
            else:
                skipped += 1
        con.commit()
    return {"parsed": len(rows), "inserted": inserted, "updated": updated, "skipped": skipped}


def count_accounts(status: str = "", kind: str = "") -> int:
    con = _conn()
    sql = "SELECT COUNT(*) FROM outlook_accounts"
    where, args = [], []
    if status:
        where.append("status=?")
        args.append(status)
    if kind:
        where.append("kind=?")
        args.append(kind.strip().lower())
    if where:
        sql += " WHERE " + " AND ".join(where)
    return con.execute(sql, args).fetchone()[0]


def list_accounts(
    status: str = "", limit: int = 50, offset: int = 0, kind: str = ""
) -> list[dict]:
    con = _conn()
    sql = "SELECT * FROM outlook_accounts"
    where, args = [], []
    if status:
        where.append("status=?")
        args.append(status)
    if kind:
        where.append("kind=?")
        args.append(kind.strip().lower())
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY imported_at DESC LIMIT ? OFFSET ?"
    args += [limit, offset]
    return [dict(r) for r in con.execute(sql, args).fetchall()]


def stats_by_kind() -> dict:
    """按邮箱类型分组统计，给 WebUI 顶部展示"每种邮箱各有多少号"。"""
    con = _conn()
    cur = con.execute(
        "SELECT kind, status, COUNT(*) AS n FROM outlook_accounts GROUP BY kind, status"
    )
    out: dict[str, dict] = {}
    for r in cur.fetchall():
        k = r["kind"] or "outlook"
        slot = out.setdefault(
            k, {"available": 0, "in_use": 0, "done": 0, "failed": 0, "total": 0}
        )
        slot[r["status"]] = r["n"]
        slot["total"] += r["n"]
    return out


def get_account(email: str) -> Optional[dict]:
    con = _conn()
    cur = con.execute("SELECT * FROM outlook_accounts WHERE email=?", (email.lower(),))
    row = cur.fetchone()
    return dict(row) if row else None


def claim_account(email: str) -> Optional[dict]:
    """原子 claim 指定邮箱（available / failed -> in_use）。

    failed 也允许重试 claim：之前 OpenAI 风控误判 / 网络抖动等导致 fail 的号
    应允许用户手动重试，已 done 的号才禁止重 claim（防误覆盖凭证）。

    按 email 指定时不过滤 kind —— 用户点名要这个号，它是什么类型
    由记录自己的 kind 列说了算，调用方读 account["kind"] 即可。
    """
    email = (email or "").strip().lower()
    if not email:
        return None
    with _lock:
        con = _conn()
        cur = con.execute(
            "SELECT * FROM outlook_accounts WHERE email=? AND status IN ('available', 'failed')",
            (email,),
        )
        row = cur.fetchone()
        if not row:
            return None
        rc = con.execute(
            "UPDATE outlook_accounts SET status='in_use', claimed_at=?, fail_reason=NULL "
            "WHERE email=? AND status IN ('available', 'failed')",
            (time.time(), email),
        )
        con.commit()
        if rc.rowcount != 1:
            return None
        return dict(row)


def claim_next(kind: str = "") -> Optional[dict]:
    """原子 claim 任一 available 号。

    kind 指定 → 只从该类型里挑（"选了 gmail 就只跑 gmail 号"）
    kind 为空 → 全池子里挑最早导入的

    多类型混放的关键就在这里：号池里 outlook 和 gmail 并存，
    但当前配置选了哪种，就只 claim 哪种，不会串。
    """
    k = (kind or "").strip().lower()
    with _lock:
        con = _conn()
        for _ in range(50):  # 有限重试，避免并发抢号时无限递归爆栈
            if k:
                cur = con.execute(
                    "SELECT * FROM outlook_accounts WHERE status='available' AND kind=? "
                    "ORDER BY imported_at ASC LIMIT 1",
                    (k,),
                )
            else:
                cur = con.execute(
                    "SELECT * FROM outlook_accounts WHERE status='available' "
                    "ORDER BY imported_at ASC LIMIT 1"
                )
            row = cur.fetchone()
            if not row:
                return None
            rc = con.execute(
                "UPDATE outlook_accounts SET status='in_use', claimed_at=? "
                "WHERE email=? AND status='available'",
                (time.time(), row["email"]),
            )
            con.commit()
            if rc.rowcount == 1:
                return dict(row)
            # 被别的线程抢走了，换下一个再试
        return None


def mark_done(email: str) -> None:
    with _lock:
        con = _conn()
        con.execute(
            "UPDATE outlook_accounts SET status='done', finished_at=?, fail_reason=NULL WHERE email=?",
            (time.time(), email.lower()),
        )
        con.commit()


def mark_failed(email: str, reason: str = "") -> None:
    with _lock:
        con = _conn()
        con.execute(
            "UPDATE outlook_accounts SET status='failed', finished_at=?, fail_reason=? WHERE email=?",
            (time.time(), (reason or "")[:500], email.lower()),
        )
        con.commit()


def release_unused(email: str) -> None:
    """claim 后没真注册（异常 / 用户取消）→ 还回 available。"""
    with _lock:
        con = _conn()
        con.execute(
            "UPDATE outlook_accounts SET status='available', claimed_at=NULL "
            "WHERE email=? AND status='in_use'",
            (email.lower(),),
        )
        con.commit()


def reset_to_available(email: str) -> bool:
    """手动重置单个号：done / failed → available，清空时间戳和失败原因。

    场景：注册成功但 refresh_token 没拿到，主人想重新跑一遍这个号。
    """
    with _lock:
        con = _conn()
        rc = con.execute(
            "UPDATE outlook_accounts SET status='available', claimed_at=NULL, "
            "finished_at=NULL, fail_reason=NULL "
            "WHERE lower(email)=lower(?)",
            (email,),
        )
        con.commit()
        return rc.rowcount > 0


def bulk_reset_to_available(emails: list[str]) -> int:
    """批量重置多个号。返回实际被改的行数。"""
    if not emails:
        return 0
    with _lock:
        con = _conn()
        rc = con.execute(
            f"UPDATE outlook_accounts SET status='available', claimed_at=NULL, "
            f"finished_at=NULL, fail_reason=NULL "
            f"WHERE lower(email) IN ({','.join(['lower(?)'] * len(emails))})",
            emails,
        )
        con.commit()
        return rc.rowcount


def reset_failed_to_available() -> int:
    """把所有 failed 号一次性重置为 available（清掉 fail_reason）。返回受影响行数。

    场景：代理短暂抽风导致一波号被冤枉标 failed，主人想给它们一次机会。
    """
    with _lock:
        con = _conn()
        rc = con.execute(
            "UPDATE outlook_accounts SET status='available', fail_reason=NULL, "
            "finished_at=NULL WHERE status='failed'"
        )
        con.commit()
        return rc.rowcount


def release_stale_in_use(stale_seconds: float = 1800) -> int:
    """把 claimed_at 超过 N 秒还在 in_use 的号释放回 available。

    场景：上次 webui 强退/进程崩溃，号卡在 in_use 永远不释放。默认 30 分钟。
    """
    with _lock:
        con = _conn()
        cutoff = time.time() - stale_seconds
        rc = con.execute(
            "UPDATE outlook_accounts SET status='available', claimed_at=NULL "
            "WHERE status='in_use' AND (claimed_at IS NULL OR claimed_at < ?)",
            (cutoff,),
        )
        con.commit()
        return rc.rowcount


def delete_account(email: str) -> bool:
    with _lock:
        con = _conn()
        rc = con.execute("DELETE FROM outlook_accounts WHERE email=?", (email.lower(),))
        con.commit()
        return rc.rowcount > 0


def delete_accounts_by_status(status: str) -> int:
    """按状态批量删除。status 必须是 available/in_use/done/failed 之一；
    传 'all' 删全部。返回受影响行数。"""
    valid = {"available", "in_use", "done", "failed", "all"}
    s = (status or "").strip().lower()
    if s not in valid:
        return 0
    with _lock:
        con = _conn()
        if s == "all":
            rc = con.execute("DELETE FROM outlook_accounts")
        else:
            rc = con.execute("DELETE FROM outlook_accounts WHERE status=?", (s,))
        con.commit()
        return rc.rowcount


def delete_accounts_by_emails(emails: list[str]) -> int:
    """按 email 列表批量删除。返回受影响行数。"""
    cleaned = [e.strip().lower() for e in (emails or []) if e and e.strip()]
    if not cleaned:
        return 0
    with _lock:
        con = _conn()
        placeholders = ",".join("?" * len(cleaned))
        rc = con.execute(
            f"DELETE FROM outlook_accounts WHERE email IN ({placeholders})",
            cleaned,
        )
        con.commit()
        return rc.rowcount


def stats() -> dict:
    con = _conn()
    cur = con.execute(
        "SELECT status, COUNT(*) AS n FROM outlook_accounts GROUP BY status"
    )
    out = {"available": 0, "in_use": 0, "done": 0, "failed": 0, "total": 0}
    for r in cur.fetchall():
        out[r["status"]] = r["n"]
        out["total"] += r["n"]
    return out


# ──────────────────────── 注册结果存储 ────────────────────────


def save_registered(d: dict) -> None:
    """保存注册成功（或部分成功）的凭证。覆盖同邮箱旧记录。

    凭证三件套（access_token / session_token / refresh_token）单独存列；
    其余字段（如 device_id / cookie_header / id_token / 自定义元数据）打包进 extra_json。
    """
    email = (d.get("email") or "").lower()
    if not email:
        return
    password = d.get("password", "") or ""
    extra = {k: v for k, v in d.items() if k not in {
        "email", "password", "access_token", "session_token", "refresh_token",
        "id_token", "device_id", "csrf_token", "cookie_header",
        "totp_secret", "totp_factor_id", "session_json",
    }}
    with _lock:
        con = _conn()
        # ⚠️ INSERT OR REPLACE 是**整行替换**，不是按字段合并 —— 没写的列会被清空。
        #    重跑同一个邮箱时这会咬人：第一轮 register_password 设了密码但 OTP 超时，
        #    save_password_early 把密码存下了；第二轮 OpenAI 已经认识这个邮箱了，
        #    走 passwordless_login 分支根本不调 register_password，
        #    这一轮的 d["password"] 是空的 —— 直接 REPLACE 就把上一轮的密码冲没了。
        #    密码是 OpenAI 侧的**持久状态**，"这一轮没设" ≠ "这个号没有密码"，
        #    所以空值不覆盖非空旧值。
        #    token 三件套正相反：每轮跑都是全新的，旧的可能已失效，照常整列覆盖。
        # totp_secret 和密码同理，甚至更严：secret【一次性下发、服务端取不回】，
        #    丢了 = 该号 2FA 永久锁死。重跑同邮箱（已绑过 2FA）时这一轮不会再绑，
        #    d 里没有 secret —— 绝不能拿空值把库里已存的 secret 冲没。
        #    与密码合成一次 SELECT，顺带把两列旧值一起兜住。
        totp_secret = (d.get("totp_secret") or "").strip()
        totp_factor_id = (d.get("totp_factor_id") or "").strip()
        if not password or not totp_secret:
            row = con.execute(
                "SELECT password, totp_secret, totp_factor_id FROM registered WHERE email=?",
                (email,),
            ).fetchone()
            if row:
                if not password and (row["password"] or "").strip():
                    password = row["password"]
                if not totp_secret and (row["totp_secret"] or "").strip():
                    totp_secret = row["totp_secret"]
                    # factor_id 跟着 secret 走：本轮没绑就沿用旧的
                    totp_factor_id = totp_factor_id or (row["totp_factor_id"] or "")
        con.execute(
            "INSERT OR REPLACE INTO registered "
            "(email, password, access_token, session_token, refresh_token, "
            "id_token, device_id, csrf_token, cookie_header, "
            "totp_secret, totp_factor_id, session_json, extra_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                email,
                password,
                d.get("access_token", ""),
                d.get("session_token", ""),
                d.get("refresh_token", ""),
                d.get("id_token", ""),
                d.get("device_id", ""),
                d.get("csrf_token", ""),
                d.get("cookie_header", ""),
                totp_secret,
                totp_factor_id,
                # session 接口的完整响应 JSON 原文（空则留 None）
                str(d.get("session_json") or "").strip() or None,
                json.dumps(extra, ensure_ascii=False) if extra else None,
                time.time(),
            ),
        )
        con.commit()


def save_password_early(email: str, password: str) -> None:
    """密码一在 OpenAI 侧生效就落盘，不等整个注册流程跑完。

    由 AuthFlow 的 on_password 回调触发（register_password 里 POST 200 之后）。
    此刻账号+密码在 OpenAI 那边已经建好，但本地还要过发码/验证/建账户三关，
    挂在任何一关都走不到 save_registered ——
    密码只活在内存里，进程一退号就成了谁也登不进去的孤儿。

    只写 email + password；token 三件套留空，等流程跑通后 save_registered
    用同一个 email 主键覆盖同一行补上。extra_json 打 pending 标记，
    方便一眼认出"有密码没凭证"的半成品行（跑通后会被 save_registered 清掉）。

    ⚠️ 行已存在时**只 UPDATE password**，绝不动已有的 token：
       重跑一个之前跑通过的邮箱时，不能把人家的凭证清空。
    """
    email = (email or "").strip().lower()
    password = (password or "").strip()
    if not email or not password:
        return
    with _lock:
        con = _conn()
        con.execute(
            "INSERT INTO registered "
            "(email, password, access_token, session_token, refresh_token, "
            "id_token, device_id, csrf_token, cookie_header, extra_json, created_at) "
            "VALUES (?, ?, '', '', '', '', '', '', '', ?, ?) "
            "ON CONFLICT(email) DO UPDATE SET password=excluded.password",
            (
                email,
                password,
                json.dumps({"pending": True}, ensure_ascii=False),
                time.time(),
            ),
        )
        con.commit()


def save_totp_early(email: str, secret: str, factor_id: str = "") -> None:
    """2FA secret 一从 enroll 响应拿到就落盘，不等整个注册流程跑完。

    由 registrar 的 _bind_2fa_hook 触发（钩子在「拿到 session」和「Codex 授权 /
    绑手机号接码」之间调 bind_totp_2fa_inline，成功即拿到 secret）。

    ⚠️ 早落盘的理由和 save_password_early 一模一样、甚至更急：
       secret 绑成之后，流程还要走 Codex 授权 + add-phone 接码（可能好几分钟），
       这段时间 secret 只活在 registrar 内存的 _tfa_box 里。接码太久用户一关进程，
       secret 就永久蒸发 —— 而它【一次性下发、服务端取不回】，丢了该号 2FA 锁死。
       所以一拿到手就先写库，后面接码怎么中断都不怕。

    只写 totp 两列；token / 密码留给后续 save_registered 用同一 email 主键补齐。
    ⚠️ 行已存在时**只 UPDATE totp 两列**，绝不动已有的密码 / token
       —— 重跑老号时不能把人家已存的凭证清空。
    """
    email = (email or "").strip().lower()
    secret = (secret or "").strip()
    if not email or not secret:
        return
    factor_id = (factor_id or "").strip()
    with _lock:
        con = _conn()
        con.execute(
            "INSERT INTO registered "
            "(email, password, access_token, session_token, refresh_token, "
            "id_token, device_id, csrf_token, cookie_header, "
            "totp_secret, totp_factor_id, extra_json, created_at) "
            "VALUES (?, '', '', '', '', '', '', '', '', ?, ?, ?, ?) "
            "ON CONFLICT(email) DO UPDATE SET "
            "totp_secret=excluded.totp_secret, "
            "totp_factor_id=excluded.totp_factor_id",
            (
                email,
                secret,
                factor_id,
                json.dumps({"pending": True}, ensure_ascii=False),
                time.time(),
            ),
        )
        con.commit()


def normalize_totp_secret(raw: str) -> str:
    """把用户手填的 TOTP secret 规范化成可用的 base32，非法值抛 ValueError。

    登录侧（auth_flow._totp_now）拿到 secret 直接 b32decode，**不做任何校验** ——
    脏值存进去要等到真登录时才炸，那时只看到一句 base32 解码异常，
    根本看不出是手填填错了。所以校验必须挡在写库这一关。

    接受的输入：
      - 裸 base32:  JBSWY3DPEHPK3PXP / jbswy3dp ehpk 3pxp / JBSW-Y3DP-EHPK
      - otpauth URI: otpauth://totp/ChatGPT:a@b.com?secret=JBSWY3DP&issuer=...
        （从手机 App 导出/二维码解码出来的就是这个格式，直接粘进来很常见）
    """
    s = (raw or "").strip()
    if not s:
        return ""
    # otpauth:// URI 抽 secret 参数
    if s.lower().startswith("otpauth://"):
        try:
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(s).query)
            s = (qs.get("secret") or [""])[0]
        except Exception:
            raise ValueError("otpauth 链接解析失败，请直接填 secret")
        if not s:
            raise ValueError("otpauth 链接里没有 secret 参数")
    # 去掉分隔符（手机 App 展示时常带空格/连字符）并统一大写
    s = s.replace(" ", "").replace("-", "").replace("_", "").upper()
    # base32 只有 A-Z 和 2-7，先挡掉明显非法字符再解码，报错更好懂
    if not s or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567=" for c in s):
        raise ValueError("TOTP secret 含非法字符（base32 只允许 A-Z 和 2-7）")
    try:
        # 补 padding 后试解，解得开才算合法。auth_flow 那边也是这么补的。
        decoded = base64.b32decode(s + "=" * (-len(s) % 8))
    except Exception:
        raise ValueError("TOTP secret 不是合法的 base32")
    if len(decoded) < 10:
        raise ValueError(f"TOTP secret 太短（解出 {len(decoded)} 字节，通常应为 20 字节）")
    return s


def update_registered_manual(email: str, password: Optional[str] = None,
                             totp_secret: Optional[str] = None) -> bool:
    """手动修正某个已注册账号的密码 / TOTP secret。

    ⚠️ 只改**本地库**，不会同步到 OpenAI —— 这里改密码不等于改了账号密码。
       用途是把外部已知的凭证补进来，或修正记录错误。

    传 None = 该字段不动（不是清空）。用 None 而不是空串做"不修改"的标记，
    是为了留出"主人真想清空某字段"的余地（传空串即清空）。

    totp_secret 会先过 normalize_totp_secret 校验，非法直接抛 ValueError；
    宁可这里报错，也不能让脏值躺进库里等登录时才炸。

    返回 False 表示该邮箱不存在（不会凭空插入新行 —— 手填是"修正已有记录"，
    真要新增外部账号是另一件事，走单独的导入功能）。
    """
    email = (email or "").strip().lower()
    if not email:
        return False
    sets, vals = [], []
    if password is not None:
        sets.append("password=?")
        vals.append(password)
    if totp_secret is not None:
        # 空串 = 主人主动清空；非空则必须过校验
        sets.append("totp_secret=?")
        vals.append(normalize_totp_secret(totp_secret) if totp_secret.strip() else "")
    if not sets:
        return False
    with _lock:
        con = _conn()
        row = con.execute("SELECT email FROM registered WHERE email=?", (email,)).fetchone()
        if not row:
            return False
        vals.append(email)
        con.execute(f"UPDATE registered SET {', '.join(sets)} WHERE email=?", vals)
        con.commit()
        return True


def update_plus_check(email: str, plus_info: dict) -> None:
    """把 Plus 检查结果写入 extra_json.plus_check。"""
    email = email.lower()
    con = _conn()
    cur = con.execute("SELECT extra_json FROM registered WHERE email=?", (email,))
    row = cur.fetchone()
    if not row:
        return
    extra = {}
    if row["extra_json"]:
        try:
            extra = json.loads(row["extra_json"])
        except Exception:
            extra = {}
    extra["plus_check"] = plus_info
    with _lock:
        con.execute(
            "UPDATE registered SET extra_json=? WHERE email=?",
            (json.dumps(extra, ensure_ascii=False), email),
        )
        con.commit()


def _registered_where(filt: str) -> str:
    if filt == "has_rt":
        return "WHERE length(refresh_token) > 0"
    if filt == "no_rt":
        return "WHERE coalesce(length(refresh_token),0) = 0"
    if filt == "unchecked":
        return "WHERE (extra_json IS NULL OR extra_json NOT LIKE '%\"plus_check\"%')"
    if filt == "free":
        return "WHERE extra_json LIKE '%\"free\"%'"
    if filt == "plus":
        return "WHERE (extra_json LIKE '%\"plus_eligible\"%' OR extra_json LIKE '%\"plus_active\"%')"
    if filt == "banned":
        return "WHERE extra_json LIKE '%\"banned\"%'"
    if filt == "token_invalid":
        # token_invalid 从 2026-08-10 起会写库，得能筛出来，否则等于埋了：
        # 它既不在 unchecked 里（已有结论），又不在 free/plus/banned 里。
        return "WHERE extra_json LIKE '%\"token_invalid\"%'"
    return ""


def count_registered(filter_rt: str = "all") -> int:
    con = _conn()
    cur = con.execute(f"SELECT COUNT(*) FROM registered {_registered_where(filter_rt)}")
    return cur.fetchone()[0]


def list_registered(limit: int = 20, offset: int = 0, filter_rt: str = "all") -> list[dict]:
    con = _conn()
    where = _registered_where(filter_rt)
    cur = con.execute(
        f"SELECT email, password, totp_secret, "
        f"length(access_token) AS at_len, length(session_token) AS st_len, "
        f"length(refresh_token) AS rt_len, extra_json, created_at FROM registered "
        f"{where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (limit, offset),
    )
    rows = []
    for r in cur.fetchall():
        d = dict(r)
        plus = None
        proxy = ""
        abnormal = False
        if d.get("extra_json"):
            try:
                extra = json.loads(d["extra_json"])
                plus = extra.get("plus_check")
                proxy = str(extra.get("proxy") or "").strip()
                abnormal = bool(extra.get("abnormal"))
            except Exception:
                pass
        d["plus_check"] = plus
        # 该号注册时用的代理 + 是否已标注异常。
        # 代理列是导出「access_token----代理」和配额释放的依据。
        d["proxy"] = proxy
        d["abnormal"] = abnormal
        d.pop("extra_json", None)
        rows.append(d)
    return rows


def list_registered_full(limit: int = 5000) -> list[dict]:
    """返回完整凭证（用于批量导出）。每行同 get_registered 的格式，外加 relay_url。

    ⚠️ relay_url（中转取件链接）**不在 registered 表里**，它跟着号池那一行走
       （outlook_accounts.relay_url，icloud_relay 这类号一号一条 token）。
       导出格式「邮箱----密码----2FA----取件url」要用它，所以这里 LEFT JOIN 带出来。
       用 JOIN 而不是给 registered 加列的原因：不用迁移、**已经注册完的老号也能导**
       （只要号池那行还在）；号池行被删掉就是空串，照约定留空、分隔符保留。
    """
    con = _conn()
    cur = con.execute(
        "SELECT r.*, a.relay_url AS relay_url "
        "FROM registered r LEFT JOIN outlook_accounts a ON a.email = r.email "
        "ORDER BY r.created_at DESC LIMIT ?",
        (limit,),
    )
    out = []
    for row in cur.fetchall():
        d = dict(row)
        if d.get("extra_json"):
            try:
                d["extra"] = json.loads(d["extra_json"])
            except Exception:
                d["extra"] = {}
        else:
            d["extra"] = {}
        # 把 extra 里的 proxy / abnormal 平铺到顶层，导出格式直接取 d["proxy"]
        d["proxy"] = str(d["extra"].get("proxy") or "").strip()
        d["abnormal"] = bool(d["extra"].get("abnormal"))
        d.pop("extra_json", None)
        out.append(d)
    return out


def list_registered_by_emails(emails: list[str]) -> list[dict]:
    """按 email 列表返回完整凭证（批量导出勾选的号用）。

    - 行序 = created_at 倒序，和「注册结果」表格里看到的一致，方便核对。
    - 查不到的 email 直接不出现（号已被删掉的情况），不报错。
    - SQLite 单条语句变量数有上限（默认 999），所以分批查。
    - relay_url 从号池表 LEFT JOIN 带出（原因见 list_registered_full）。
    """
    cleaned = [e.strip().lower() for e in (emails or []) if e and e.strip()]
    if not cleaned:
        return []

    con = _conn()
    out = []
    CHUNK = 500
    for i in range(0, len(cleaned), CHUNK):
        part = cleaned[i:i + CHUNK]
        placeholders = ",".join("?" * len(part))
        cur = con.execute(
            f"SELECT r.*, a.relay_url AS relay_url "
            f"FROM registered r LEFT JOIN outlook_accounts a ON a.email = r.email "
            f"WHERE r.email IN ({placeholders})",
            part,
        )
        for row in cur.fetchall():
            d = dict(row)
            if d.get("extra_json"):
                try:
                    d["extra"] = json.loads(d["extra_json"])
                except Exception:
                    d["extra"] = {}
            else:
                d["extra"] = {}
            d["proxy"] = str(d["extra"].get("proxy") or "").strip()
            d["abnormal"] = bool(d["extra"].get("abnormal"))
            d.pop("extra_json", None)
            out.append(d)

    out.sort(key=lambda d: d.get("created_at") or 0, reverse=True)
    return out


def get_registered(email: str) -> Optional[dict]:
    con = _conn()
    cur = con.execute("SELECT * FROM registered WHERE email=?", (email.lower(),))
    row = cur.fetchone()
    if not row:
        return None
    out = dict(row)
    if out.get("extra_json"):
        try:
            out["extra"] = json.loads(out["extra_json"])
        except Exception:
            out["extra"] = {}
    out.pop("extra_json", None)
    return out


def _release_proxy_for_emails(emails: list[str]) -> int:
    """把这些号占用的代理配额还回去。返回释放了几个。

    只对**真正占用过配额**的号释放 —— 用 extra_json.proxy_released 标记位
    防重复释放（同一个号先标异常再删除，不能减两次）。
    """
    cleaned = [e.strip().lower() for e in (emails or []) if e and e.strip()]
    if not cleaned:
        return 0
    released = 0
    with _lock:
        con = _conn()
        placeholders = ",".join("?" * len(cleaned))
        rows = con.execute(
            f"SELECT email, extra_json FROM registered WHERE email IN ({placeholders})",
            cleaned,
        ).fetchall()
        for row in rows:
            extra = {}
            if row["extra_json"]:
                try:
                    extra = json.loads(row["extra_json"]) or {}
                except Exception:
                    extra = {}
            if extra.get("proxy_released"):
                continue
            proxy = str(extra.get("proxy") or "").strip()
            if not proxy:
                continue
            con.execute(
                "UPDATE proxy_pool SET used_count=MAX(0, used_count-1) WHERE proxy=?",
                (proxy,),
            )
            extra["proxy_released"] = True
            con.execute(
                "UPDATE registered SET extra_json=? WHERE email=?",
                (json.dumps(extra, ensure_ascii=False), row["email"]),
            )
            released += 1
        con.commit()
    return released


def delete_registered(email: str) -> bool:
    # 删除前把该号占用的代理配额还回去（账号没了，配额也不该占着）
    _release_proxy_for_emails([email])
    with _lock:
        con = _conn()
        rc = con.execute("DELETE FROM registered WHERE email=?", (email.lower(),))
        con.commit()
        return rc.rowcount > 0


def delete_registered_by_emails(emails: list[str]) -> int:
    cleaned = [e.strip().lower() for e in (emails or []) if e and e.strip()]
    if not cleaned:
        return 0
    _release_proxy_for_emails(cleaned)
    with _lock:
        con = _conn()
        placeholders = ",".join("?" * len(cleaned))
        rc = con.execute(
            f"DELETE FROM registered WHERE email IN ({placeholders})",
            cleaned,
        )
        con.commit()
        return rc.rowcount


def get_prefs() -> dict:
    """读界面偏好设置（表单默认值、主题等）。

    这些以前散在各处 localStorage 里（stores/form.js / stores/theme.js /
    stores/proxy.js）。localStorage 的问题是：
      - 只跟着**这一个浏览器**走，换台机器/清缓存就没了
      - 后端完全看不到，前端和后端各记一份，容易对不上
    现在统一收到后端 SQLite（settings 表的 prefs key），前后端同一份。
    """
    raw = get_setting("prefs", "")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_prefs(patch: dict) -> dict:
    """合并写入偏好设置（只覆盖传进来的 key），返回合并后的完整值。"""
    data = get_prefs()
    if not isinstance(patch, dict):
        patch = {}
    for k, v in patch.items():
        if not isinstance(k, str) or not k:
            continue
        data[k] = v
    set_setting("prefs", json.dumps(data, ensure_ascii=False))
    return data


def delete_all_registered() -> int:
    with _lock:
        con = _conn()
        rows = con.execute("SELECT email FROM registered").fetchall()
        rc = con.execute("DELETE FROM registered")
        con.commit()
        emails = [r["email"] for r in rows]
    # 清空后把所有号占用的配额还回去
    _release_proxy_for_emails(emails)
    return rc.rowcount


def mark_registered_abnormal(email: str, abnormal: bool = True) -> bool:
    """标注账号异常（被封 / 额度耗尽 / 不可用）。

    标注为异常时**自动释放该号占用的代理配额** —— 号已经废了，
    它的 IP 名额应该还回池子里给别的号用。
    """
    email = str(email or "").strip().lower()
    if not email:
        return False
    if abnormal:
        _release_proxy_for_emails([email])
    with _lock:
        con = _conn()
        row = con.execute(
            "SELECT extra_json FROM registered WHERE email=?", (email,)
        ).fetchone()
        if not row:
            return False
        extra = {}
        if row["extra_json"]:
            try:
                extra = json.loads(row["extra_json"]) or {}
            except Exception:
                extra = {}
        extra["abnormal"] = bool(abnormal)
        con.execute(
            "UPDATE registered SET extra_json=? WHERE email=?",
            (json.dumps(extra, ensure_ascii=False), email),
        )
        con.commit()
    return True


def mark_registered_abnormal_by_emails(emails: list[str], abnormal: bool = True) -> int:
    cleaned = [e.strip().lower() for e in (emails or []) if e and e.strip()]
    if not cleaned:
        return 0
    count = 0
    for e in cleaned:
        if mark_registered_abnormal(e, abnormal):
            count += 1
    return count


# ──────────────────────── 运行记录 ────────────────────────


def create_run(run_id: str, email: str, log_path: str) -> None:
    with _lock:
        con = _conn()
        con.execute(
            "INSERT INTO runs(run_id, email, status, started_at, log_path) "
            "VALUES (?, ?, 'running', ?, ?)",
            (run_id, email.lower(), time.time(), log_path),
        )
        con.commit()


def finish_run(run_id: str, status: str, error: str = "", category: str = "") -> None:
    with _lock:
        con = _conn()
        con.execute(
            "UPDATE runs SET status=?, finished_at=?, error=?, error_category=? WHERE run_id=?",
            (status, time.time(), (error or "")[:500], category or None, run_id),
        )
        con.commit()


def list_runs(limit: int = 50) -> list[dict]:
    con = _conn()
    cur = con.execute(
        "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,),
    )
    return [dict(r) for r in cur.fetchall()]


# ──────────────────────── settings (KV) ────────────────────────


def get_setting(key: str, default: str = "") -> str:
    con = _conn()
    cur = con.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = cur.fetchone()
    return row["value"] if row else default


def set_setting(key: str, value) -> None:
    with _lock:
        con = _conn()
        con.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        con.commit()


# ──────────────────────── 邮箱来源配置 ────────────────────────


def get_mail_config() -> dict:
    """返回邮箱来源配置（密码类字段隐藏明文）。

    provider 声明的配置项自动带出来 —— 加新邮箱时这里不用改，
    新 provider 的 config_fields 会自动出现在返回值里。
    """
    from mail_providers import list_providers

    out = {"mail_source": get_setting("mail_source", "outlook")}
    for p in list_providers():
        for f in p["config_fields"]:
            key = f["key"]
            if f.get("type") == "password":
                out[key] = "***" if get_setting(key) else ""
            else:
                out[key] = get_setting(key, "")
    return out


def save_mail_config(data: dict) -> None:
    """保存邮箱配置。password 类字段传 '***' 表示不修改。

    mail_source 校验改成查 mail_providers 注册表：
        以前是写死的白名单 ("outlook", "cf_temp")，选了别的会被
        **静默改回 outlook** —— 用户看到的是"保存成功但选择没生效"。
        现在未知来源直接抛错，问题当场暴露。
    """
    from mail_providers import get_provider_class, list_providers

    if "mail_source" in data:
        src = str(data["mail_source"]).strip().lower()
        get_provider_class(src)  # 未注册的 kind 会抛 MailProviderError
        set_setting("mail_source", src)

    # 按 provider 声明的字段保存，加新邮箱时这里零改动
    for p in list_providers():
        for f in p["config_fields"]:
            key = f["key"]
            if key not in data:
                continue
            val = data[key]
            if f.get("type") == "password":
                if not val or val == "***":
                    continue  # 没填 / 是掩码 → 保持原值
            set_setting(key, str(val).strip())


def get_secret_setting(key: str) -> str:
    """内部用：拿密码类配置的明文。"""
    return get_setting(key, "")


def get_mail_settings() -> dict:
    """内部用：给 create_mail_provider 的 settings（含明文密钥）。

    跟 get_mail_config 的区别：这个不打码，只在服务端构造 provider 时用，
    绝不能直接返回给前端。
    """
    from mail_providers import list_providers

    out = {"mail_source": get_setting("mail_source", "outlook")}
    for p in list_providers():
        for f in p["config_fields"]:
            out[f["key"]] = get_setting(f["key"], "")
    return out


def get_cf_admin_token() -> str:
    """内部用：拿明文 admin_token。"""
    return get_setting("cf_admin_token", "")


# ──────────────────────── SMS 接码配置 ────────────────────────


def get_sms_config() -> dict:
    """返回 SMS 接码配置（api_key 隐藏明文）。

    sms_enabled:        '0'/'1' 是否启用接码（命中 add-phone 时才会用）
    sms_provider:       smsbower
    sms_country:        国家代码或 ID（推荐 '52' = Thailand，OpenAI 走 SMS 的唯一稳定国家）
    sms_service:        服务代码（OpenAI = 'dr'）
    sms_max_price:      号码最高单价（SmsBower / SmsBower 用，单位平台货币；空 / -1 = 不限）
    sms_reuse_phone:    '0'/'1' 同号复用（SmsBower / SmsBower 支持，省钱）
    sms_phone_success_max: 同号最多复用几次（默认 3）
    sms_auto_country:   '0'/'1' 自动选最优国家（按价格 + 库存）
    sms_auto_min_stock: 自动选国家最低库存（默认 20）
    sms_auto_max_price: 自动选国家最高单价（默认 0 = 不限）
    """
    return {
        "sms_enabled":             get_setting("sms_enabled", "0"),
        "sms_provider":            get_setting("sms_provider", "smsbower"),
        "sms_api_key":             "***" if get_setting("sms_api_key") else "",
        "sms_country":             get_setting("sms_country", "52"),
        "sms_service":             get_setting("sms_service", "dr"),
        "sms_max_price":           get_setting("sms_max_price", ""),
        "sms_fixed_price":         get_setting("sms_fixed_price", ""),
        "sms_reuse_phone":         get_setting("sms_reuse_phone", "0"),
        "sms_phone_success_max":   get_setting("sms_phone_success_max", "3"),
        "sms_auto_country":        get_setting("sms_auto_country", "0"),
        "sms_strict_whitelist":    get_setting("sms_strict_whitelist", "0"),
        "sms_allowed_countries":   get_setting("sms_allowed_countries", ""),
        "sms_auto_min_stock":      get_setting("sms_auto_min_stock", "20"),
        "sms_auto_max_price":      get_setting("sms_auto_max_price", ""),
        "sms_max_phone_attempts":  get_setting("sms_max_phone_attempts", ""),
        "sms_per_phone_timeout":   get_setting("sms_per_phone_timeout", "80"),
    }


def save_sms_config(data: dict) -> None:
    """保存 SMS 配置。sms_api_key 传 '***' 表示不修改。"""
    # 校验 provider
    valid_providers = {"smsbower", "herosms"}
    if "sms_provider" in data:
        p = str(data["sms_provider"]).strip().lower()
        if p not in valid_providers:
            p = "smsbower"
        set_setting("sms_provider", p)
    # 字符串字段直接落
    for key in (
        "sms_country", "sms_service", "sms_max_price", "sms_fixed_price",
        "sms_phone_success_max", "sms_auto_min_stock", "sms_auto_max_price",
        "sms_max_phone_attempts", "sms_per_phone_timeout",
        "sms_allowed_countries",
    ):
        if key in data:
            set_setting(key, str(data[key]).strip())
    # 布尔字段（前端传 '0'/'1' 或 bool）
    for key in ("sms_enabled", "sms_reuse_phone", "sms_auto_country", "sms_strict_whitelist"):
        if key in data:
            v = data[key]
            if isinstance(v, bool):
                set_setting(key, "1" if v else "0")
            else:
                s = str(v).strip().lower()
                set_setting(key, "1" if s in ("1", "true", "yes", "on") else "0")
    # API key（'***' 不修改）
    if data.get("sms_api_key") and data["sms_api_key"] != "***":
        set_setting("sms_api_key", str(data["sms_api_key"]).strip())


def get_sms_internal_config() -> dict:
    """内部用：拿明文 sms_api_key,供 sms_provider 实例化使用。"""
    return {
        "sms_enabled":             get_setting("sms_enabled", "0") in ("1", "true"),
        "sms_provider":            get_setting("sms_provider", "smsbower"),
        "sms_api_key":             get_setting("sms_api_key", ""),
        "sms_country":             get_setting("sms_country", "52"),
        "sms_service":             get_setting("sms_service", "dr"),
        "sms_max_price":           get_setting("sms_max_price", ""),
        "sms_fixed_price":         get_setting("sms_fixed_price", ""),
        "sms_reuse_phone":         get_setting("sms_reuse_phone", "0") in ("1", "true"),
        "sms_phone_success_max":   get_setting("sms_phone_success_max", "3"),
        "sms_auto_country":        get_setting("sms_auto_country", "0") in ("1", "true"),
        "sms_strict_whitelist":    get_setting("sms_strict_whitelist", "0") in ("1", "true"),
        "sms_allowed_countries":   get_setting("sms_allowed_countries", ""),
        "sms_auto_min_stock":      get_setting("sms_auto_min_stock", "20"),
        "sms_auto_max_price":      get_setting("sms_auto_max_price", ""),
        "sms_max_phone_attempts":  get_setting("sms_max_phone_attempts", ""),
        "sms_per_phone_timeout":   get_setting("sms_per_phone_timeout", "80"),
    }


# ──────────────────────── 自动导出配置 (CPA / SUB2API) ────────────────────────


def get_export_config() -> dict:
    """返回导出配置（敏感字段做明文/'***' 占位）。

    给前端展示用：
      cpa_mgmt_key / sub2api_api_key 已设置时返回 '***'，未设置返回 ''。
      保存时传 '***' 代表不修改。
    """
    return {
        # CPA
        "cpa_enabled":     get_setting("export_cpa_enabled", "0"),
        "cpa_url":         get_setting("export_cpa_url", ""),
        "cpa_mgmt_key":    "***" if get_setting("export_cpa_mgmt_key") else "",
        "cpa_timeout":     get_setting("export_cpa_timeout", "30"),
        # SUB2API
        "sub2api_enabled":    get_setting("export_sub2api_enabled", "0"),
        "sub2api_url":        get_setting("export_sub2api_url", ""),
        "sub2api_api_key":    "***" if get_setting("export_sub2api_api_key") else "",
        "sub2api_group_ids":  get_setting("export_sub2api_group_ids", "2"),
        "sub2api_timeout":    get_setting("export_sub2api_timeout", "30"),
    }


def save_export_config(data: dict) -> None:
    """保存导出配置。密文字段传 '***' 表示不修改。"""
    # 布尔开关
    for key_in, key_out in (
        ("cpa_enabled",     "export_cpa_enabled"),
        ("sub2api_enabled", "export_sub2api_enabled"),
    ):
        if key_in in data:
            v = data[key_in]
            if isinstance(v, bool):
                set_setting(key_out, "1" if v else "0")
            else:
                s = str(v).strip().lower()
                set_setting(key_out, "1" if s in ("1", "true", "yes", "on") else "0")
    # 字符串字段（明文）
    for key_in, key_out in (
        ("cpa_url",            "export_cpa_url"),
        ("cpa_timeout",        "export_cpa_timeout"),
        ("sub2api_url",        "export_sub2api_url"),
        ("sub2api_group_ids",  "export_sub2api_group_ids"),
        ("sub2api_timeout",    "export_sub2api_timeout"),
    ):
        if key_in in data:
            set_setting(key_out, str(data[key_in] or "").strip())
    # 密文字段（'***' 不修改）
    if data.get("cpa_mgmt_key") and data["cpa_mgmt_key"] != "***":
        set_setting("export_cpa_mgmt_key", str(data["cpa_mgmt_key"]).strip())
    if data.get("sub2api_api_key") and data["sub2api_api_key"] != "***":
        set_setting("export_sub2api_api_key", str(data["sub2api_api_key"]).strip())


def get_export_internal_config() -> dict:
    """内部用：拿明文密钥 + 解析后的 enabled 布尔。供 registrar / app.test 调用。

    返回两个子配置 dict，可分别传给 exporter.export_to_cpa / export_to_sub2api。
    """
    cpa = {
        "enabled":      get_setting("export_cpa_enabled", "0") in ("1", "true"),
        "cpa_url":      get_setting("export_cpa_url", ""),
        "cpa_mgmt_key": get_setting("export_cpa_mgmt_key", ""),
        "cpa_timeout":  get_setting("export_cpa_timeout", "30"),
    }
    sub2api = {
        "enabled":            get_setting("export_sub2api_enabled", "0") in ("1", "true"),
        "sub2api_url":        get_setting("export_sub2api_url", ""),
        "sub2api_api_key":    get_setting("export_sub2api_api_key", ""),
        "sub2api_group_ids":  get_setting("export_sub2api_group_ids", "2"),
        "sub2api_timeout":    get_setting("export_sub2api_timeout", "30"),
    }
    return {"cpa": cpa, "sub2api": sub2api}


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


# 模块加载时自动建表
init_db()
