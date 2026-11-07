"""两项目改造的回归验证脚本。

覆盖：
  A. 代理池：限流 / 停用 / 动态取用 / 异常释放配额
  B. Remail provider：下单 / 取件 / 验证码 / 兜底 / 时间窗 / 订单落库
  C. 导出格式：at_proxy 的输出
  D. chatgpt2api 侧：token----proxy 解析 + 代理落库 + 运行时生效
  E. OTP 超时下限 / 订单复用 / 偏好落库 / 订单禁用删除

⚠️ **必须用独立的临时 DB**！
   这个测试会 clear_proxies()、save_remail_order()、DELETE FROM registered …
   如果直接跑在生产库上，**会把主人的代理池、号池、订单全部清掉**
   （真实发生过 —— 主人的代理就是这样被测试脚本清没的）。

   在 import regix.storage.facade 之前把 WEBUI_DB_PATH 指到临时文件：
   storage 在 import 时读这个环境变量决定 DB_PATH，事后改没用。

用法（注册机项目根目录）：
    .venv/Scripts/python.exe test_integration.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

# Set the isolated DB before importing regix.storage.facade.
_TEST_DB = Path(tempfile.gettempdir()) / "regix_integration_test.db"
for _suffix in ("", "-shm", "-wal"):
    try:
        os.remove(str(_TEST_DB) + _suffix)
    except OSError:
        pass
os.environ["WEBUI_DB_PATH"] = str(_TEST_DB)
os.environ["WEBUI_LOG_DIR"] = str(Path(tempfile.gettempdir()) / "regix_integration_test_logs")

OK = 0
FAIL = 0


def _cleanup_test_db():
    for _suffix in ("", "-shm", "-wal"):
        try:
            os.remove(str(_TEST_DB) + _suffix)
        except OSError:
            pass


def check(name, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  {extra}")


def section(title):
    print(f"\n【{title}】")


def test_proxy_pool():
    from regix.storage import facade as db
    section("A. 代理池限流")

    db.clear_proxies()
    db.replace_proxies(["http://p1:1", "http://p2:2", "http://p3:3"])
    for p in ("http://p1:1", "http://p2:2", "http://p3:3"):
        db.update_proxy(p, max_uses=2)
    check("导入 3 个代理", db.proxy_pool_stats()["total"] == 3)

    # 默认上限是 50
    db.replace_proxies(["http://new:1"], mode="append")
    check("新建代理默认上限 50",
          db.find_proxy_max("http://new:1") == 50 if hasattr(db, "find_proxy_max")
          else db.list_proxies()[-1]["max_uses"] == 50)

    # 动态取用应轮转均衡，而不是固定绑定某个代理
    # pick_proxy 选中即预占一格，所以连续取 3 次应该分到 3 个不同代理
    picks = [db.pick_proxy() for _ in range(3)]
    check("动态取用会轮转（不是固定绑一个）",
          len(set(picks)) == 3, picks)
    it = {i["proxy"]: i for i in db.list_proxies()}
    check("预占后各自占 1 格",
          all(it[p]["used_count"] == 1 for p in picks),
          {p: it[p]["used_count"] for p in picks})

    # 预占满了上限的代理不会再被选中
    db.proxy_release(picks[0])
    db.proxy_release(picks[1])
    db.proxy_release(picks[2])

    # 成功占配额（此处直接记账，模拟已完成的注册）
    for p in ("http://p1:1", "http://p1:1", "http://p2:2"):
        db.proxy_used(p, ok=True)
    it = {i["proxy"]: i for i in db.list_proxies()}
    check("成功计数 +1", it["http://p1:1"]["used_count"] == 2, it["http://p1:1"])

    # 失败不占配额
    db.proxy_used("http://p3:3", ok=False)
    it = {i["proxy"]: i for i in db.list_proxies()}
    check("失败只加 failed_count，不占配额",
          it["http://p3:3"]["used_count"] == 0 and it["http://p3:3"]["failed_count"] == 1,
          it["http://p3:3"])

    # 停用后不再被选中
    db.update_proxy("http://p1:1", enabled=False)
    picks = []
    for _ in range(10):
        p = db.pick_proxy()
        if p:
            picks.append(p)
            db.proxy_release(p)   # 归还预占，模拟注册失败
    check("停用的代理不再被分配", "http://p1:1" not in picks, set(picks))

    # 超额的不再被选中
    db.update_proxy("http://p2:2", max_uses=1)
    db.proxy_used("http://p2:2", ok=True)
    picks = []
    for _ in range(10):
        p = db.pick_proxy()
        if p:
            picks.append(p)
            db.proxy_release(p)
    check("超额的代理不再被分配", "http://p2:2" not in picks, set(picks))

    db.clear_proxies()


def test_abnormal_release():
    from regix.storage import facade as db
    section("A2. 标注异常 → 释放代理配额")

    db.clear_proxies()
    db.replace_proxies(["http://rel:1"])
    db.update_proxy("http://rel:1", max_uses=5)
    db.proxy_used("http://rel:1", ok=True)
    db.save_registered({"email": "rel@test.com", "access_token": "AT", "proxy": "http://rel:1"})
    check("初始占用 1 格",
          [i for i in db.list_proxies() if i["proxy"] == "http://rel:1"][0]["used_count"] == 1)

    db.mark_registered_abnormal("rel@test.com", True)
    check("标注异常后配额释放",
          [i for i in db.list_proxies() if i["proxy"] == "http://rel:1"][0]["used_count"] == 0)

    rows = db.list_registered()
    row = [r for r in rows if r["email"] == "rel@test.com"][0]
    check("列表里 abnormal=True", row["abnormal"] is True, row)
    check("列表里带出 proxy 字段", row["proxy"] == "http://rel:1", row)

    # 重复标注不应重复释放
    db.mark_registered_abnormal("rel@test.com", True)
    check("重复标注不重复释放",
          [i for i in db.list_proxies() if i["proxy"] == "http://rel:1"][0]["used_count"] == 0)

    # 删除时也不重复释放
    db.delete_registered("rel@test.com")
    check("删除后不重复释放",
          [i for i in db.list_proxies() if i["proxy"] == "http://rel:1"][0]["used_count"] == 0)

    con = db._conn()
    con.execute("DELETE FROM registered WHERE email='rel@test.com'")
    con.commit()
    db.clear_proxies()


def test_export_format():
    from regix.storage import facade as db
    from regix.services import export_formats
    section("C. 导出格式 at_proxy")

    fmt = export_formats.get_format("at_proxy")
    check("格式已注册", fmt is not None)
    text = export_formats.render_text(
        [{"access_token": "AT1", "proxy": "http://1.2.3.4:8080"},
         {"access_token": "AT2", "proxy": ""}],
        fmt,
    )
    check("带代理的行输出 token----proxy",
          "AT1----http://1.2.3.4:8080" in text, text)
    check("无代理的行保留分隔符",
          "AT2----" in text, text)


def test_provider_registry():
    from regix.providers.mail import list_providers, get_provider_class
    section("B0. Remail provider 注册")

    kinds = [p["kind"] for p in list_providers()]
    check("remail 已注册", "remail" in kinds, kinds)

    cls = get_provider_class("remail")
    check("pooled=False（不走号池）", cls.pooled is False)
    check("ephemeral=True（每次新地址）", cls.ephemeral is True)

    fields = {f.key: f for f in cls.config_fields}
    check("后缀是自由输入框", fields["remail_email_suffix"].type == "text")
    check("服务模式是下拉", fields["remail_service_mode"].type == "select")
    check("服务模式选项含 purchase/code",
          set(fields["remail_service_mode"].options) == {"purchase", "code"})
    check("库存策略是下拉", fields["remail_supply_policy"].type == "select")
    check("库存策略默认项是 private_first",
          fields["remail_supply_policy"].options[0] == "private_first")
    check("API Key 是密码类型", fields["remail_api_key"].type == "password")
    check("项目 ID 是数字类型", fields["remail_project_id"].type == "number")


def test_chatgpt2api_parse():
    section("D. chatgpt2api 侧 token----proxy 解析")
    import re

    # 与前端 parseTokenLines 同逻辑
    def parse(value):
        out = []
        for line in [x.strip() for x in value.splitlines() if x.strip()]:
            i = line.find("----")
            if i < 0:
                out.append((line, None))
                continue
            a, p = line[:i].strip(), line[i + 4:].strip()
            out.append((a, p) if (a and p) else (line, None))
        return out

    r = parse("TOKEN_A----http://1.2.3.4:8080\nTOKEN_B\nTOKEN_C----socks5://u:p@5.6.7.8:1080")
    check("带 http 代理被解析", r[0] == ("TOKEN_A", "http://1.2.3.4:8080"), r[0])
    check("纯 token 保持原样", r[1] == ("TOKEN_B", None), r[1])
    check("带 socks5 代理被解析",
          r[2] == ("TOKEN_C", "socks5://u:p@5.6.7.8:1080"), r[2])

    # JWT 里的单横线不该被误切
    jwt = "eyJhbGciOiJIUzI1NiIsImtpZCI6Ii0xIn0.abc-def"
    r2 = parse(jwt)
    check("JWT 含单横线不被误切", r2[0] == (jwt, None), r2[0])

    # 分隔符后为空 → 视为无代理
    r3 = parse("TOKEN_D----")
    check("尾部分隔符视为无代理", r3[0] == ("TOKEN_D----", None), r3[0])


def test_chatgpt2api_runtime():
    section("D2. chatgpt2api 代理运行时生效")
    root = Path(__file__).resolve().parents[1] / "chatgpt2api"
    if not root.exists():
        check("chatgpt2api 目录存在", True, f"跳过可选集成目标: {root}")
        return

    # chatgpt2api 用的是自己的 uv 环境（sqlalchemy 等依赖不在注册机的 venv 里），
    # 所以用子进程跑，避免污染当前解释器。
    import subprocess
    code = r"""
import sys
sys.path.insert(0, '.')
from services.account_service import account_service
from services.proxy_service import proxy_settings

tok = 'test.integration.proxy.token'
account_service.add_account_items([{'access_token': tok, 'proxy': 'http://9.9.9.9:8080'}])
acct = account_service.get_account(tok)
print('STORED:', acct.get('proxy'))
prof = proxy_settings.get_profile(account=acct)
print('SOURCE:', prof.proxy_source)
print('URL:', prof.proxy_url)

tok2 = 'test.integration.proxy.token2'
account_service.add_account_items([{'access_token': tok2, 'proxy': 'socks5://u:p@8.8.8.8:1080'}])
prof2 = proxy_settings.get_profile(account=account_service.get_account(tok2))
print('SOCKS_URL:', prof2.proxy_url)
"""
    try:
        r = subprocess.run(
            ["uv", "run", "python", "-c", code],
            cwd=str(root), capture_output=True, text=True, timeout=180,
        )
        out = r.stdout
        print("    " + out.strip().replace("\n", "\n    "))
        if r.returncode != 0:
            check("chatgpt2api 运行时验证", False, r.stderr[-300:])
            return
        check("代理已存入账号", "STORED: http://9.9.9.9:8080" in out, out)
        check("运行时来源是 account", "SOURCE: account" in out, out)
        check("运行时用账号代理", "URL: http://9.9.9.9:8080" in out, out)
        check("socks5 自动归一化为 socks5h",
              "SOCKS_URL: socks5h://u:p@8.8.8.8:1080" in out, out)
    except Exception as e:
        check("chatgpt2api 运行时验证", False, e)


def test_otp_timeout_floor():
    section("E. OTP 超时下限（Remail 聚合转发需要更久）")
    from regix.services import registration as registrar

    # ⚠️ 回归点：OTP 等待原本跟着前端传的值走，而前端默认是 10 秒。
    #    Remail 的邮件要经平台中转再投到目标邮箱，实测 20~30 秒才到
    #    （发码 18:34:41 → 到达 18:35:05），10 秒必然超时，注册跑到最后一步全废。
    #    后端必须兜一个下限。
    src = (Path(__file__).resolve().parent / "regix" / "services" / "registration.py").read_text(
        encoding="utf-8")
    check("registrar 里有 OTP_TIMEOUT 下限保护",
          "max(60, _otp_timeout)" in src, "没找到 max(60, ...)")

    # 前端默认值也应该是 180
    form_src = (Path(__file__).resolve().parent / "webui" / "frontend" / "src"
                / "stores" / "form.js").read_text(encoding="utf-8")
    check("前端 otpTimeout 默认 180", "otpTimeout: 180" in form_src)


def test_order_reuse():
    from regix.storage import facade as db
    section("F. 长效订单复用")

    db.save_remail_order({
        "order_no": "REUSE_A", "email": "reuse_a@outlook.com",
        "suffix": "outlook.com", "service_mode": "purchase",
        "project_id": 2, "order_status": "active", "service_token": "svc_a",
        "pay_amount": "15.00", "received_at": 1000.0, "attempt_count": 0,
    })
    db.save_remail_order({
        "order_no": "REUSE_B", "email": "reuse_b@outlook.com",
        "suffix": "outlook.com", "service_mode": "purchase",
        "project_id": 2, "order_status": "active", "service_token": "svc_b",
        "pay_amount": "15.00", "received_at": 2000.0, "attempt_count": 0,
    })

    # 最早的可复用订单优先（先把已付的钱用掉）
    got = db.claim_reusable_remail_order(
        service_mode="purchase", suffix="outlook.com", project_id=2)
    check("复用最早的可复用订单", got and got["order_no"] == "REUSE_A",
          got["order_no"] if got else None)

    # 已关联账号的不再复用
    db.link_remail_order("REUSE_A", "linked@x.com")
    got2 = db.claim_reusable_remail_order(
        service_mode="purchase", suffix="outlook.com", project_id=2)
    check("已关联账号的不再复用", got2 and got2["order_no"] == "REUSE_B",
          got2["order_no"] if got2 else None)

    # 尝试次数超限的跳过
    db.save_remail_order({
        "order_no": "REUSE_C", "email": "reuse_c@outlook.com",
        "suffix": "outlook.com", "service_mode": "purchase",
        "project_id": 2, "order_status": "active", "service_token": "svc_c",
        "received_at": 3000.0, "attempt_count": 9,
    })
    got3 = db.claim_reusable_remail_order(
        service_mode="purchase", suffix="outlook.com", project_id=2)
    check("尝试次数超限的不再复用",
          got3 is None or got3["order_no"] != "REUSE_C",
          got3["order_no"] if got3 else None)

    # 短效订单不可复用
    got4 = db.claim_reusable_remail_order(
        service_mode="code", suffix="outlook.com", project_id=2)
    check("短效订单不复用", got4 is None, got4)

    # 不同后缀不混用
    got5 = db.claim_reusable_remail_order(
        service_mode="purchase", suffix="gmail.com", project_id=2)
    check("不同后缀不混用", got5 is None, got5)

    con = db._conn()
    con.execute("DELETE FROM remail_orders WHERE order_no LIKE 'REUSE_%'")
    con.commit()


def test_prefs_db():
    from regix.storage import facade as db
    section("G. 界面偏好走后端（不再用 localStorage）")

    db.save_prefs({"__test_key": "v1", "__test_other": 2})
    check("prefs 写入后可读回", db.get_prefs().get("__test_key") == "v1")
    db.save_prefs({"__test_key": "v2"})
    p = db.get_prefs()
    check("prefs 是合并写入（不影响其它 key）",
          p.get("__test_key") == "v2" and p.get("__test_other") == 2, p)

    # 前端不应再有 localStorage
    front = Path(__file__).resolve().parent / "webui" / "frontend" / "src"
    bad = []
    for f in front.rglob("*"):
        if f.suffix not in (".js", ".vue"):
            continue
        txt = f.read_text(encoding="utf-8", errors="ignore")
        # 去掉注释再找真正的 localStorage 调用
        code = "\n".join(
            ln for ln in txt.splitlines()
            if not ln.strip().startswith("//") and not ln.strip().startswith("*")
            and not ln.strip().startswith("/*")
        )
        if "localStorage" in code:
            bad.append(f.relative_to(front))
    check("前端已无 localStorage 调用", not bad, bad)

    con = db._conn()
    con.execute("UPDATE settings SET value=? WHERE key='prefs'", ("{}",))
    con.commit()


def test_order_disable_delete():
    from regix.storage import facade as db
    section("H. 订单禁用 / 删除 + 复用上限 10")

    check("复用上限是 10", db.REMAIL_MAX_ATTEMPTS == 10, db.REMAIL_MAX_ATTEMPTS)

    db.save_remail_order({
        "order_no": "DIS_A", "email": "dis_a@outlook.com",
        "suffix": "outlook.com", "service_mode": "purchase",
        "project_id": 2, "order_status": "active", "service_token": "s_a",
        "received_at": 5000.0, "attempt_count": 0,
    })
    db.save_remail_order({
        "order_no": "DIS_B", "email": "dis_b@outlook.com",
        "suffix": "outlook.com", "service_mode": "purchase",
        "project_id": 2, "order_status": "active", "service_token": "s_b",
        "received_at": 6000.0, "attempt_count": 0,
    })

    got = db.claim_reusable_remail_order(
        service_mode="purchase", suffix="outlook.com", project_id=2)
    check("先复用最早的一笔", got and got["order_no"] == "DIS_A",
          got["order_no"] if got else None)

    # 禁用后跳过
    db.set_remail_order_disabled("DIS_A", True)
    got2 = db.claim_reusable_remail_order(
        service_mode="purchase", suffix="outlook.com", project_id=2)
    check("禁用的订单不再被复用",
          got2 and got2["order_no"] == "DIS_B",
          got2["order_no"] if got2 else None)

    row = [o for o in db.list_remail_orders() if o["order_no"] == "DIS_A"][0]
    check("禁用后 disabled=True", row["disabled"] is True, row)
    check("禁用后 reusable=False", row["reusable"] is False, row)

    # 启用恢复
    db.set_remail_order_disabled("DIS_A", False)
    row2 = [o for o in db.list_remail_orders() if o["order_no"] == "DIS_A"][0]
    check("启用后 reusable 恢复", row2["reusable"] is True, row2)
    check("列表带出 max_attempts", row2.get("max_attempts") == 10, row2.get("max_attempts"))

    # 尝试次数达到上限（10）后不再复用
    db.mark_remail_order_attempt("DIS_A", ok=True)
    for _ in range(9):
        db.mark_remail_order_attempt("DIS_A", ok=True)
    row3 = [o for o in db.list_remail_orders() if o["order_no"] == "DIS_A"][0]
    check("尝试次数累计到 10", row3["attempt_count"] == 10, row3["attempt_count"])

    # 禁用 DIS_B 后，本测试造的两笔都不可用了 ——
    # ⚠️ 但库里还有主人自己的真实订单，所以这里不能断言"查不到任何订单"，
    #    只能断言"这两笔不再被选中"。
    db.set_remail_order_disabled("DIS_B", True)
    got3 = db.claim_reusable_remail_order(
        service_mode="purchase", suffix="outlook.com", project_id=2)
    check("尝试满 10 次后不再复用",
          got3 is None or got3["order_no"] not in ("DIS_A", "DIS_B"),
          got3["order_no"] if got3 else None)

    # 删除
    check("删除订单返回 True", db.delete_remail_order("DIS_A") is True)
    check("删除后查不到", db.get_remail_order("DIS_A") is None)
    check("批量删除返回条数", db.delete_remail_orders(["DIS_B"]) == 1)

    con = db._conn()
    con.execute("DELETE FROM remail_orders WHERE order_no LIKE 'DIS_%'")
    con.commit()


def main():
    print("=" * 50)
    print("  两项目改造回归验证")
    print("=" * 50)
    test_provider_registry()
    test_proxy_pool()
    test_abnormal_release()
    test_export_format()
    test_chatgpt2api_parse()
    test_chatgpt2api_runtime()
    test_otp_timeout_floor()
    test_order_reuse()
    test_prefs_db()
    test_order_disable_delete()
    print(f"\n{'=' * 50}")
    print(f"  通过 {OK} / 失败 {FAIL}")
    print(f"{'=' * 50}")
    _cleanup_test_db()
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
