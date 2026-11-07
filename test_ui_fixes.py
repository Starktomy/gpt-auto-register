"""三个 UI 交互问题的回归验证（对应主人 2026-11-04 的反馈）。

  1. 接码配置的"启用/禁用"开关显示反转
  2. 代理池重启后丢失（根因：测试脚本污染生产库）
  3. 全自动批量的 2FA 开关关不掉

根因都是同一个：**异步 load() 把用户的在途操作覆盖掉**。
load() 要等后端返回（几百毫秒），这期间用户点一下开关，
load() 回来后无条件 `form[key] = 后端值` → 把用户的选择冲掉。

这些断言检查**源码里是否有防护**，而不是跑浏览器（没有 headless 环境）。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

_FRONT = Path(__file__).resolve().parent / "webui" / "frontend" / "src"

OK = 0
FAIL = 0


def check(name, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  {extra}")


def read(rel: str) -> str:
    return (_FRONT / rel).read_text(encoding="utf-8")


def read_flow_source() -> str:
    """Read every OpenAI protocol module after the AuthFlow mixin split."""
    root = Path(__file__).resolve().parent / "regix" / "openai"
    names = (
        "auth_flow.py", "models.py", "client.py", "oauth.py",
        "challenges.py", "token.py", "protocol.py", "session.py",
        "registration.py",
    )
    return "\n".join((root / name).read_text(encoding="utf-8") for name in names)


def _code_only(src: str) -> str:
    """去掉注释，只留真正的代码 —— 判断"某调用是否存在"时要用。

    否则注释里写着 "原来这里有 setTimeout(load, 300)" 也会被误判成代码还在。
    """
    out = []
    in_block = False
    for line in src.splitlines():
        stripped = line.strip()
        if in_block:
            if "*/" in stripped:
                in_block = False
            continue
        if stripped.startswith("/*"):
            if "*/" not in stripped:
                in_block = True
            continue
        if stripped.startswith("//") or stripped.startswith("*"):
            continue
        out.append(line)
    return "\n".join(out)


def test_sms_switch():
    print("\n【1】接码配置开关反转")
    src = read("views/SmsConfig.vue")

    check("有 touched 集合（记录用户动过的字段）", "const touched = new Set()" in src)
    check("开关的 @change 会记 touched",
          "markTouched('enabled')" in src, "checkbox 上没绑 @change")
    check("load() 不覆盖用户动过的 enabled",
          "touched.has('enabled')" in src)
    check("enabled 的赋值在 loadCountries 之前（缩短竞态窗口）",
          src.index("enabled.value = config.sms_enabled") < src.index("await loadCountries(provider.value)"),
          "赋值排在 await loadCountries 之后 → 窗口太长")
    check("保存后不再 setTimeout(load) 重读",
          "setTimeout(load" not in _code_only(src),
          "保存后又 load 一次，会把用户当前状态重置")


def test_proxy_pool_persistence():
    print("\n【2】代理池丢失（测试脚本污染生产库）")
    for name in ("test_integration.py", "test_remail_provider.py"):
        src = Path(__file__).resolve().parent / name
        text = src.read_text(encoding="utf-8")
        check(f"{name} 用了独立的临时 DB",
              "WEBUI_DB_PATH" in text,
              "没设 WEBUI_DB_PATH → 直接操作生产库")
        check(f"{name} 在 import regix.storage 前设环境变量",
              text.index("WEBUI_DB_PATH") < text.index("from regix.storage")
              if "regix.storage" in text else True,
              "设得太晚，db.py 已经在 import 时读过了")
        check(f"{name} 结束时清理临时 DB",
              "_cleanup_test_db" in text or "os.remove" in text)

    # 生产库里不能有测试残留
    from regix.storage import facade as db
    bad = [o["email"] for o in db.list_remail_orders()
           if o["order_no"].startswith(("ORD0", "REUSE", "DIS_"))]
    check("生产库没有测试残留订单", not bad, bad)


def test_2fa_switch():
    print("\n【3】2FA 开关关不掉")
    form_src = read("stores/form.js")

    check("form store 有 dirty 集合", "const dirty = new Set()" in form_src)
    check("load() 跳过用户动过的字段", "dirty.has(key)" in form_src)
    check("有 _applying 标志区分程序赋值/用户操作",
          "_applying" in form_src,
          "没有区分 → load() 内部的赋值也会被当成用户修改")
    check("灌值后 await nextTick 再复位标志",
          "await nextTick()" in form_src,
          "watch 是 pre-flush 的，不等它跑完就复位 → 标志失效")
    check("watch 里只在非程序赋值时标 dirty",
          "if (!_applying) dirty.add(key)" in form_src)

    # AutoLoop 确实把开关传给后端
    auto_src = read("views/AutoLoop.vue")
    check("AutoLoop 传 want_2fa=form.autoWant2fa",
          "want_2fa: form.value.autoWant2fa" in auto_src)

    # 后端两处判断都在
    reg_src = Path(__file__).resolve().parent / "regix" / "services" / "registration.py"
    reg = reg_src.read_text(encoding="utf-8")
    check("registrar 的钩子判断 want_2fa",
          'on_session_ready=_bind_2fa_hook if options.get("want_2fa") else None' in reg)
    check("registrar 的兜底绑定判断 want_2fa",
          'if options.get("want_2fa"):' in reg)
    check("registrar 日志打印 2FA 开关实际生效值",
          "2FA绑定=" in reg,
          "没有日志 → 分不清是开关没传过来还是绑定失败")


def test_refresh_token_switch():
    """refresh_token（Codex OAuth）与接码开关的联动。

    要保证的三件事：
      1. 默认**不**获取 refresh_token（短效邮箱不能被偷走第二次收件）
      2. 接码未启用时自动跳过 Codex（跑了也是白跑）
      3. auth_flow 有兜底防线：Codex 链不许触发第二次收件
    """
    print("\n【I】refresh_token 开关与接码联动")

    form_src = read("stores/form.js")
    check("form store 默认 wantRefreshToken=false",
          "wantRefreshToken: false" in form_src,
          "默认必须是关的 —— 否则短效邮箱会被偷走第二次收件")
    check("form store 默认 autoWantRefreshToken=false",
          "autoWantRefreshToken: false" in form_src)

    reg_src = read("views/Register.vue")
    check("Register 传 want_refresh_token=form.wantRefreshToken",
          "want_refresh_token: form.value.wantRefreshToken" in reg_src)
    auto_src = read("views/AutoLoop.vue")
    check("AutoLoop 传 want_refresh_token=form.autoWantRefreshToken",
          "want_refresh_token: form.value.autoWantRefreshToken" in auto_src)

    # 后端：默认值也应该是 False
    api_root = Path(__file__).resolve().parent / "regix" / "api" / "routers"
    app = "\n".join(
        (api_root / name).read_text(encoding="utf-8")
        for name in ("registration.py", "auto_loop.py")
    )
    check("后端 RegisterReq/AutoLoopStartReq 默认 want_refresh_token=False",
          app.count("want_refresh_token: bool = False") == 2,
          f"实际匹配 {app.count('want_refresh_token: bool = False')} 处")

    # 后端：接码未启用时跳过 Codex
    reg = (Path(__file__).resolve().parent / "regix" / "services" / "registration.py").read_text(encoding="utf-8")
    check("registrar 有 _sms_ready 判定函数",
          "def _sms_ready()" in reg)
    check("registrar 在接码不可用时跳过 Codex",
          "elif not sms_ready:" in reg)
    check("registrar 补齐了 OAUTH_SECONDARY_AUTHORIZE_EXCHANGE",
          reg.count('env_overrides["OAUTH_SECONDARY_AUTHORIZE_EXCHANGE"] = "0"') == 2,
          f"实际 {reg.count('env_overrides[\"OAUTH_SECONDARY_AUTHORIZE_EXCHANGE\"] = \"0\"')} 处（两个分支各一处）")

    # 后端：auth_flow 的兜底防线
    flow = read_flow_source()
    check("auth_flow 的 Codex need_otp 分支有兜底防线",
          "Codex 登录推进需要 OTP，但已禁用 Codex OAuth" in flow,
          "没有兜底 → env 开关失效时仍会发第二封验证码")
    check("Codex 未捕获 callback 的日志不是 debug",
          "logger.debug(\"Codex OAuth 未捕获 callback code" not in flow,
          "debug 级别会被过滤 → 只看到「尝试」没有下文")


def test_shortmail_mode():
    """短效邮箱（code 模式）的注册适配。

    背景：短效订单只保留**一封**邮件，而 OpenAI 注册流程在「设密码」后
    会让服务端自动发的那封 OTP 立即失效 → 必须补发第 2 封 → 短效收不到
    → 死锁（补发收不到 / 不补发 409）。而且那唯一一封还早于
    otp_sent_at 十几秒，会被严格时间窗滤掉 → 死等 180 秒。

    解法：短效模式跳过设密码（第 1 封保持有效）+ 取件时间窗放宽到
    「注册开始时刻」。
    """
    print("\n【J】短效邮箱（code 模式）适配")

    base = (Path(__file__).resolve().parent / "regix" / "providers" / "mail" / "base.py").read_text(encoding="utf-8")
    check("base.MailProvider 有 ephemeral_mailbox 属性",
          "ephemeral_mailbox: bool = False" in base)

    remail = (Path(__file__).resolve().parent / "regix" / "providers" / "mail" / "remail.py").read_text(encoding="utf-8")
    check("Remail 按 service_mode 置 ephemeral_mailbox",
          'self.ephemeral_mailbox = (self.service_mode == "code")' in remail)
    check("wait_for_otp 支持 earliest 参数",
          "earliest: Optional[float] = None" in remail)
    check("短效模式用 earliest 做时间窗下界",
          "if self.ephemeral_mailbox and earliest is not None:" in remail)
    check("短效模式不再用 issued_after 严格过滤",
          "elif issued_after is not None:" in remail)

    flow = read_flow_source()
    check("auth_flow 记录注册开始时刻",
          "self._register_started_at = time.time()" in flow)
    check("auth_flow 短效模式跳过设密码",
          'skip_password = bool(getattr(mail_provider, "ephemeral_mailbox", False))' in flow)
    check("短效模式的时间窗下界用注册开始时刻",
          "otp_sent_at = self._register_started_at" in flow)
    check("取件时传入 earliest",
          "earliest=self._register_started_at" in flow)

    reg = (Path(__file__).resolve().parent / "regix" / "services" / "registration.py").read_text(encoding="utf-8")
    check("registrar 有 mail_limited 错误类别",
          "_MAIL_LIMITED_PATTERNS" in reg)
    check("mail_limited 在 network 之前判定",
          reg.index("_MAIL_LIMITED_PATTERNS") < reg.index('if any(p in s for p in _NETWORK_ERROR_PATTERNS)'))
    check("mail_limited 不 release 复用",
          'elif category == "mail_limited":' in reg)
    check("mail_limited 会 mark_failed 废弃",
          'db.mark_failed(email, f"[{category}] {err}")' in reg)

    # 实际行为验证
    from regix.services.registration import classify_error
    check("Remail OTP timeout 归类为 mail_limited（不是 network）",
          classify_error("Remail OTP timeout for x@outlook.com", "remail") == "mail_limited")
    check("真正的网络错误仍归 network",
          classify_error("TLS 瞬断 timeout", "remail") == "network")

    from regix.providers.mail.remail import RemailMailProvider
    p_code = RemailMailProvider(api_base="https://x", api_key="rk-x", project_id=2, service_mode="code")
    p_pur = RemailMailProvider(api_base="https://x", api_key="rk-x", project_id=2, service_mode="purchase")
    check("code 模式 provider 标记为 ephemeral_mailbox",
          p_code.ephemeral_mailbox is True)
    check("purchase 模式 provider 不标记",
          p_pur.ephemeral_mailbox is False)


def test_cf_blocked_and_shortmail_state():
    """v13：CF 拦截分类 + 静态/动态代理差异处置 + 短效订单 consumed 状态。"""
    print("\n【K. CF拦截分类 + 代理类型 + 短效订单状态】")
    from regix.storage import facade as db
    from regix.services.registration import classify_error

    # ── 错误分类 ──
    cases = [
        ("创建账户失败: 400 - {\"error\": {\"code\": \"user_already_exists\"}}",
         "account", "user_already_exists 归 account（邮箱废了换下一个）"),
        ("authorize/continue 失败(screen_hint=signup): HTTP 409 ... invalid_state",
         "network", "invalid_state 归 network（会话失效可重建后重试）"),
        ("warmup 失败：4 次重试均未拿到 chatgpt.com 的 oai-did cookie，继续注册必然 409 invalid_state",
         "cf_blocked", "warmup CF403 归 cf_blocked（换代理，不熔断）"),
        ("Remail OTP timeout for x@outlook.com",
         "mail_limited", "短效 OTP 超时仍是 mail_limited"),
        ("TLS 瞬断 timeout", "network", "真网络故障仍是 network"),
    ]
    for err, expect, desc in cases:
        got = classify_error(err, "remail")
        check(desc, got == expect, f"得到 {got}，期望 {expect}")

    # ── 静态 vs 动态代理的 CF 处置差异 ──
    db.clear_proxies()
    db.replace_proxies(["http://s1:1", "http://d1:2"])
    db.update_proxy("http://s1:1", proxy_type="static")
    db.update_proxy("http://d1:2", proxy_type="dynamic")

    r1 = db.mark_proxy_cf_blocked("http://s1:1")
    check("静态代理被拦 1 次不停用", r1["auto_disabled"] is False, r1)
    r2 = db.mark_proxy_cf_blocked("http://s1:1")
    check("静态代理被拦 2 次自动停用", r2["auto_disabled"] is True, r2)
    s1 = db.find_proxy("http://s1:1")
    check("停用后 enabled=False", s1["enabled"] is False, s1)

    for _ in range(3):
        rd = db.mark_proxy_cf_blocked("http://d1:2")
    check("动态代理被拦 3 次仍不停用", rd["auto_disabled"] is False, rd)
    d1 = db.find_proxy("http://d1:2")
    check("动态代理 enabled 保持 True", d1["enabled"] is True, d1)
    check("动态代理记录了拦截次数", d1["cf_blocked"] == 3, d1)

    db.reset_all_proxy_cf_blocked()
    check("CF 计数可全部重置", db.find_proxy("http://d1:2")["cf_blocked"] == 0)

    # ── 短效订单终态 → consumed ──
    import time as _t
    db.save_remail_order({
        "order_no": "K_CODE", "email": "kc@outlook.com", "suffix": "outlook.com",
        "service_mode": "code", "project_id": 2, "order_status": "active",
        "service_token": "s", "received_at": _t.time(), "attempt_count": 0,
    })
    db.save_remail_order({
        "order_no": "K_LONG", "email": "kl@outlook.com", "suffix": "outlook.com",
        "service_mode": "purchase", "project_id": 2, "order_status": "active",
        "service_token": "s", "received_at": _t.time(), "attempt_count": 0,
    })
    db.mark_remail_order_attempt("K_CODE", ok=False, error="测试")
    db.mark_remail_order_attempt("K_LONG", ok=False, error="测试")
    items = {o["order_no"]: o for o in db.list_remail_orders()}
    check("短效订单失败后状态=consumed",
          items.get("K_CODE", {}).get("order_status") == "consumed",
          items.get("K_CODE", {}).get("order_status"))
    check("长效订单状态不变（仍 active）",
          items.get("K_LONG", {}).get("order_status") == "active",
          items.get("K_LONG", {}).get("order_status"))

    db.link_remail_order("K_CODE", "acc@x.com")
    items = {o["order_no"]: o for o in db.list_remail_orders()}
    check("短效订单关联后仍是 consumed",
          items.get("K_CODE", {}).get("order_status") == "consumed",
          items.get("K_CODE", {}).get("order_status"))

    con = db._conn()
    con.execute("DELETE FROM remail_orders WHERE order_no LIKE 'K_%'")
    con.commit()
    db.clear_proxies()

    # ── 前端：类型/CF/IPv6 列与按钮 ──
    pp = read("views/ProxyPool.vue")
    check("代理池有「类型」列", 'label="类型"' in pp)
    check("类型下拉有静态选项", 'value="static"' in pp)
    check("类型下拉有动态选项", 'value="dynamic"' in pp)
    check("代理池有「CF拦截」列", 'label="CF拦截"' in pp)
    check("代理池有「IPv6」列", 'label="IPv6"' in pp)
    check("有检测 IPv6 按钮", "检测 IPv6" in pp)
    check("有清CF 操作", "清CF" in pp)

    ms = read("views/MailService.vue")
    check("Remail 页有 consumed 状态映射", "consumed:" in ms)

    # ── auto_loop：cf_blocked 不熔断 ──
    al = (Path(__file__).resolve().parent / "regix" / "services" / "auto_loop.py").read_text(encoding="utf-8")
    check("auto_loop 处理 cf_blocked 分类",
          'category == "cf_blocked"' in al)
    check("auto_loop 调 mark_proxy_cf_blocked",
          "db.mark_proxy_cf_blocked(proxy)" in al)
    check("cf_blocked 不计入熔断计数",
          'if category == "network":\n                    self._consecutive_network_fails += 1' in al)

    # ── auth_flow：409 可重试 ──
    flow = read_flow_source()
    check("auth_flow 有 invalid_state 重试",
          "invalid_state" in flow and "max_attempts = 3" in flow)
    check("重试时重建 session 换 IP",
          "self.session = create_http_session(" in flow)


def test_session_json_capture():
    """v14：session 接口的完整响应 JSON 原文落库 + 导出。"""
    print("\n【L. session_json 原始响应保存与导出】")
    import json as _json
    from regix.storage import facade as db
    from regix.services import export_formats

    # ── 表结构 ──
    con = db._conn()
    cols = [r[1] for r in con.execute("PRAGMA table_info(registered)").fetchall()]
    check("registered 表有 session_json 列", "session_json" in cols, cols)

    # ── 落库 ──
    fake = _json.dumps(
        {
            "user": {"id": "u-1", "email": "s1@x.com", "groups": ["g"]},
            "accessToken": "AT1",
            "sessionToken": "ST1",
            "expires": "2026-12-05T00:00:00Z",
        },
        ensure_ascii=False, separators=(",", ":"),
    )
    db.save_registered({
        "email": "L_SESS@x.com", "access_token": "AT1",
        "session_token": "ST1", "session_json": fake,
    })
    d = db.get_registered("L_SESS@x.com")
    check("session_json 已落库", (d.get("session_json") or "") == fake, d.get("session_json"))
    check("session_json 不重复进 extra",
          "session_json" not in (d.get("extra") or {}), d.get("extra"))
    check("session_json 是紧凑单行", "\n" not in (d.get("session_json") or ""))

    # ── 导出格式 ──
    fmt = export_formats.get_format("session_json")
    check("导出格式 session_json 已注册", fmt is not None)
    if fmt:
        ids = [f["id"] for f in export_formats.list_formats()]
        check("格式清单含 session_json", "session_json" in ids, ids)
        rows = db.list_registered_full()
        text = export_formats.render_text(rows, fmt)
        lines = [l for l in text.split("\n") if l.strip()]
        check("每行一个 JSON", len(lines) >= 1, len(lines))
        ok = True
        for l in lines:
            try:
                _json.loads(l)
            except Exception:
                ok = False
        check("每行都是合法 JSON", ok, lines[:1])

    # ── 前端 ──
    rv = read("views/Registered.vue")
    check("前端有复制原始 session_json 按钮", "复制原始 session_json" in rv)
    check("前端有 copySessionJson 函数", "copySessionJson" in rv)
    check("CRED_KEYS 含 session_json", "'session_json'" in rv)

    # ── auth_flow ──
    flow = read_flow_source()
    check("AuthResult 有 session_json 字段", "self.session_json" in flow)
    check("get_auth_session 保存完整 JSON", "self.result.session_json = json.dumps(" in flow)

    # ── registrar ──
    reg = (Path(__file__).resolve().parent / "regix" / "services" / "registration.py").read_text(encoding="utf-8")
    check("registrar 显式带上 session_json", 'd["session_json"]' in reg)

    con = db._conn()
    con.execute("DELETE FROM registered WHERE email LIKE 'L_SESS@%'")
    con.commit()



def main():
    print("=" * 52)
    print("  UI 交互问题回归验证")
    print("=" * 52)
    test_sms_switch()
    test_proxy_pool_persistence()
    test_2fa_switch()
    test_refresh_token_switch()
    test_shortmail_mode()
    test_cf_blocked_and_shortmail_state()
    test_session_json_capture()
    print(f"\n{'=' * 52}")
    print(f"  通过 {OK} / 失败 {FAIL}")
    print(f"{'=' * 52}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
