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

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

DB_PATH = Path(
    os.environ.get("WEBUI_DB_PATH")
    or (Path(__file__).resolve().parents[2] / "webui" / "webui.db")
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
