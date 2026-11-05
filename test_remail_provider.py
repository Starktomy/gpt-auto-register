"""Remail provider 的离线联调脚本。

用一个假的 Remail 服务（本地 HTTP）验证：
  1. 下单请求的 URL / query / header / body 完全符合官方 OpenAPI
  2. 能从响应里正确取出 deliveryEmail + serviceToken
  3. 取件轮询能拿到平台直出的 verificationCode
  4. 平台没给码时，能兜底从 bodyPreview 抽 6 位码
  5. 时间窗过滤生效（issued_after 之前的旧码不认）
  6. 订单落库 + 关联本地账号

⚠️ **用独立的临时 DB**：这个测试会造订单，而 Remail provider 现在会**优先复用**
   已有订单（长效订单复用的功能）。如果不隔离：
     - 测试的假订单会写进生产库，污染真实的订单列表
     - 生产库里遗留的订单会被测试当成"可复用"而返回，断言就乱套了
   所以在 import webui.db **之前**把 WEBUI_DB_PATH 指到临时文件。

用法：.venv/Scripts/python.exe test_remail_provider.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

# ⚠️ 必须在 import webui.db 之前设好 —— db.py 在 import 时读这个环境变量
#    决定 DB_PATH，事后改没用。
_TEST_DB = Path(tempfile.gettempdir()) / "gpt_register_remail_test.db"
for _suffix in ("", "-shm", "-wal"):
    try:
        os.remove(str(_TEST_DB) + _suffix)
    except OSError:
        pass
os.environ["WEBUI_DB_PATH"] = str(_TEST_DB)
os.environ["WEBUI_LOG_DIR"] = str(Path(tempfile.gettempdir()) / "gpt_register_remail_test_logs")

STATE = {
    "orders": [],          # 已下单记录
    "mails": {},           # email -> [mail]
    "otp": "123456",
    "give_verification_code": True,
    "last_order_request": None,
    "last_pickup_request": None,
}


class FakeRemail(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)

        if u.path == "/v1/open/apikey/profile":
            return self._send(200, {"apiKey": {
                "id": 1, "name": "test", "enabled": True,
                "balance": "168.50", "quotaLimit": None, "quotaUsed": "0",
            }})

        if u.path == "/v1/pickup":
            STATE["last_pickup_request"] = {"path": u.path, "query": q}
            email = (q.get("email") or [""])[0]
            token = (q.get("token") or [""])[0]
            # serviceToken 必须和下单时给的一致，否则返回 401
            known = {o["deliveryEmail"]: o["serviceToken"] for o in STATE["orders"]}
            if known.get(email) != token:
                return self._send(401, {"message": "invalid service token"})
            mails = STATE["mails"].get(email, [])
            out = []
            for m in mails:
                item = {
                    "id": m["id"], "sender": m["sender"], "recipient": email,
                    "receivedAt": m["receivedAt"], "subject": m["subject"],
                    "bodyPreview": m["bodyPreview"],
                }
                if STATE["give_verification_code"]:
                    item["verificationCode"] = m.get("verificationCode", "")
                out.append(item)
            return self._send(200, {"items": out, "fetch": {
                "lastStatus": "normal", "nextFetchAllowedAt": None, "lastSafeError": "",
            }})

        return self._send(404, {"message": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode() if length else "{}"

        if u.path == "/v1/open/orders":
            # 校验官方要求的 Idempotency-Key
            idem = self.headers.get("Idempotency-Key")
            if not idem:
                return self._send(400, {"message": "Idempotency-Key required"})
            if self.headers.get("Authorization", "").startswith("Bearer rk-") is False:
                return self._send(401, {"message": "bad auth"})
            try:
                body = json.loads(raw)
            except Exception:
                body = {}
            STATE["last_order_request"] = {
                "query": q, "body": body, "idempotency": idem,
                "auth": self.headers.get("Authorization"),
            }
            suffix = body.get("emailSuffix") or "outlook"
            email = f"user{len(STATE['orders']) + 1}@{suffix}"
            order = {
                "orderNo": f"ORD{len(STATE['orders']) + 1:04d}",
                "deliveryEmail": email,
                "serviceToken": "svc-token-abc123",
                "status": "paid",
                "serviceMode": (q.get("serviceMode") or ["purchase"])[0],
                "supplyPolicy": (q.get("supply") or ["private_first"])[0],
                "payAmount": "0.80",
                "receiveStartedAt": "2026-11-04T10:00:00Z",
                "afterSaleUntil": "2026-11-05T10:00:00Z",
            }
            STATE["orders"].append(order)
            return self._send(201, order)

        return self._send(404, {"message": "not found"})


def main():
    server = HTTPServer(("127.0.0.1", 0), FakeRemail)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    print(f"[fake remail] 监听 {base}\n")

    from mail_providers.remail import RemailMailProvider
    from webui import db

    # 清掉临时库里的残留订单 —— 复用机制会优先挑"没关联账号的长效订单"，
    # 不清的话第 2 次下单拿到的不是新邮箱（而是上一轮留下的），
    # 断言 "返回 deliveryEmail user1@outlook" 就会误判为失败。
    con = db._conn()
    con.execute("DELETE FROM remail_orders")
    con.commit()

    ok = 0
    fail = 0

    def check(name, cond, extra=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"  ✅ {name}")
        else:
            fail += 1
            print(f"  ❌ {name} {extra}")

    # ── 1. self_test ─────────────────────────────
    print("【1】self_test —— Key 校验与余额")
    p = RemailMailProvider(api_base=base, api_key="rk-test", project_id=1001,
                           email_suffix="outlook")
    r = p.self_test()
    check("self_test 通过", r.get("ok"), r)
    check("返回余额", "168.50" in str(r.get("message")), r)

    # ── 2. 下单 ─────────────────────────────────
    print("\n【2】下单 —— 请求格式与响应解析")
    email = p.create_mailbox()
    req = STATE["last_order_request"]
    check("返回 deliveryEmail", email == "user1@outlook", email)
    check("带 Bearer 认证", req["auth"] == "Bearer rk-test", req["auth"])
    check("带 Idempotency-Key", bool(req["idempotency"]), req)
    check("query 带 serviceMode=purchase", req["query"].get("serviceMode") == ["purchase"], req["query"])
    check("query 带 supply=private_first", req["query"].get("supply") == ["private_first"], req["query"])
    check("body projectId 是 int", req["body"].get("projectId") == 1001, req["body"])
    check("body emailSuffix 正确", req["body"].get("emailSuffix") == "outlook", req["body"])
    check("订单落库", db.get_remail_order("ORD0001") is not None)

    order = db.get_remail_order("ORD0001", with_token=True)
    check("落库含 serviceToken", order.get("service_token") == "svc-token-abc123", order)
    check("落库含邮箱类型(后缀)", order.get("suffix") == "outlook", order)
    check("落库含付费金额", order.get("pay_amount") == "0.80", order)

    # ── 3. 取件（平台直出验证码）─────────────────
    print("\n【3】取件 —— 平台直出 verificationCode")
    now = time.time()
    STATE["mails"][email] = [{
        "id": 1, "sender": "noreply@openai.com",
        "receivedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "subject": "Your ChatGPT code",
        "bodyPreview": "Your code is 123456",
        "verificationCode": "123456",
    }]
    otp = p.wait_for_otp(email, timeout=20, issued_after=now - 5)
    check("拿到验证码", otp == "123456", otp)

    # ── 4. 兜底抽取（平台没给码）──────────────────
    print("\n【4】兜底 —— 平台没给 verificationCode 时自己抽")
    STATE["give_verification_code"] = False
    STATE["mails"][email] = [{
        "id": 2, "sender": "noreply@openai.com",
        "receivedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "subject": "OpenAI verification code 654321",
        "bodyPreview": "Your verification code is 654321",
    }]
    p2 = RemailMailProvider(api_base=base, api_key="rk-test", project_id=1001)
    email2 = p2.create_mailbox()
    STATE["mails"][email2] = STATE["mails"][email]
    otp2 = p2.wait_for_otp(email2, timeout=20, issued_after=now - 5)
    check("从正文抽到验证码", otp2 == "654321", otp2)
    STATE["give_verification_code"] = True

    # ── 5. 时间窗过滤 ────────────────────────────
    print("\n【5】时间窗 —— issued_after 之前的旧码不认")
    old = time.time() - 600
    STATE["mails"][email] = [{
        "id": 3, "sender": "noreply@openai.com",
        "receivedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(old)),
        "subject": "old code", "bodyPreview": "code 111111",
        "verificationCode": "111111",
    }]
    p3 = RemailMailProvider(api_base=base, api_key="rk-test", project_id=1001)
    email3 = p3.create_mailbox()
    STATE["mails"][email3] = STATE["mails"][email]
    try:
        p3.wait_for_otp(email3, timeout=6, issued_after=time.time() - 5)
        check("旧码被正确拒绝（应超时）", False, "竟然返回了旧码")
    except TimeoutError:
        check("旧码被正确拒绝（超时）", True)

    # ── 6. 订单关联账号 ──────────────────────────
    print("\n【6】订单关联本地账号")
    db.link_remail_order("ORD0001", "test@local")
    linked = db.get_remail_order("ORD0001")
    check("订单已关联账号", linked.get("used_email") == "test@local", linked)

    # ── 7. 重复收件 ──────────────────────────────
    print("\n【7】重复收件 —— 长效订单随时再取一次")
    mails = p.fetch_mails(email, "svc-token-abc123")
    check("能再次取件", isinstance(mails, list), mails)

    print(f"\n{'='*46}\n通过 {ok} / 失败 {fail}\n{'='*46}")
    server.shutdown()
    # 清掉临时 DB，别留垃圾
    for _suffix in ("", "-shm", "-wal"):
        try:
            os.remove(str(_TEST_DB) + _suffix)
        except OSError:
            pass
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
