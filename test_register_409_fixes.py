"""2026-11-06 单并发连续注册 10 个账号的失败归因回归测试。

背景（来自 log.txt 实测）：
  29 次尝试 → 10 成功 / 19 失败；17 次下单 → 7 次白花（41%）。

两个真凶：

  1. **safari18_4_ios 指纹不存在**
     curl_cffi 的 iOS Safari 命名是「无下划线版本号」：safari184_ios。
     写成 safari18_4_ios 时 curl_cffi 直接抛
     "Impersonating safari18_4_ios is not supported" → 该指纹 100% 失败。
     实测 4/29 次运行全栽在这（run #5/#7/#10/#25），每次白跑 4 次 warmup。

  2. **409 重试没刷新 device_id**
     device_id 在第 3 步 auth_oauth_init 写进 self.result.device_id，
     而 _common_headers 取值是
         self.result.device_id or self.session.cookies.get("oai-did")
     —— `or` 短路，result.device_id 一旦有值就永远用它。
     旧实现 409 重试只重建 session + 重新 warmup（拿到新 oai-did），
     但请求头发的还是旧 oai-device-id → 服务端判设备不一致 → 必然又 409。
     实测印证：warmup 第一次成功、409 重试后 warmup 也第一次成功，照旧 409。

这些断言检查**源码结构与依赖可用性**，不发起真实注册（要花钱）。
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

_ROOT = Path(__file__).resolve().parent

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
    return (_ROOT / rel).read_text(encoding="utf-8")


def section(title: str):
    print(f"\n【{title}】")


# ---------------------------------------------------------------------------
section("1. 指纹 impersonate 值必须真实存在于 curl_cffi")
# ---------------------------------------------------------------------------

fp_src = read("fingerprint.py")
impersonates = sorted(set(re.findall(r'"impersonate":\s*"([^"]+)"', fp_src)))

try:
    from curl_cffi.requests.utils import NATIVE_TARGET_NAMES

    supported = set(NATIVE_TARGET_NAMES)
except Exception as e:  # pragma: no cover - 环境缺依赖时降级
    supported = None
    print(f"  ⚠️ 无法导入 curl_cffi（{e}），跳过可用性校验")

if supported is not None:
    check(
        "curl_cffi 可用（依赖装好了）",
        True,
    )
    bad = [v for v in impersonates if v not in supported]
    check(
        f"指纹表所有 impersonate 值都受支持（共 {len(impersonates)} 个）",
        not bad,
        f"不受支持: {bad}",
    )
    check(
        "不存在已知的坏值 safari18_4_ios",
        "safari18_4_ios" not in impersonates,
    )
    check(
        "存在正确的 safari184_ios",
        "safari184_ios" in impersonates,
    )
    check(
        "iOS Safari 18.4 条目的 version 号与 impersonate 一致",
        re.search(
            r'"impersonate":\s*"safari184_ios",\s*\n\s*"safari_ver":\s*"18\.4"',
            fp_src,
        )
        is not None,
    )


# ---------------------------------------------------------------------------
section("2. 409 重试必须刷新 device_id（不能只重建 session）")
# ---------------------------------------------------------------------------

af_src = read("auth_flow.py")

# 找到 409 重试块（从 "signup 撞 invalid_state" 往后 2500 字符）
m = re.search(r"signup 撞 invalid_state", af_src)
check("源码里存在 409 重试块", m is not None)
if m:
    block = af_src[m.start(): m.start() + 2500]

    check(
        "重试时清空了 self.result.device_id",
        'self.result.device_id = ""' in block,
    )
    check(
        "重试时清空了 self._last_sentinel_so_token",
        'self._last_sentinel_so_token = ""' in block,
    )
    check(
        "重试时重跑了 get_csrf_token（第 1 步）",
        "self.get_csrf_token()" in block,
    )
    check(
        "重试时重跑了 get_auth_url（第 2 步）",
        "self.get_auth_url(" in block,
    )
    check(
        "重试时重跑了 auth_oauth_init（第 3 步，刷新 device_id）",
        "self.auth_oauth_init(" in block,
    )
    check(
        "重试时重新 warmup 了",
        "self.warmup()" in block,
    )

# _common_headers 必须走 `or` 回退（这是 device_id 能刷新的前提）
# ⚠️ 用行号定位而不是 re.search("def _common_headers") —— 源码里更早的位置
#    就有 "self._common_headers(...)" 这类调用字符串，会被误当成定义。
af_lines = af_src.splitlines()
def_line = next(
    (i for i, l in enumerate(af_lines)
     if l.strip().startswith("def _common_headers(")),
    None,
)
check("存在 _common_headers 定义", def_line is not None)
if def_line is not None:
    hdr = "\n".join(af_lines[def_line: def_line + 80])
    check(
        "_common_headers 里 device_id 走 `result.device_id or cookie` 回退",
        re.search(
            r'self\.result\.device_id\s+or\s+.{0,80}?self\.session\.cookies\.get\(\s*"oai-did"',
            hdr,
            re.S,
        )
        is not None,
        "回退链断了 → 清空 device_id 也拿不到新值",
    )


# ---------------------------------------------------------------------------
section("3. 下单邮箱的位置")
# ---------------------------------------------------------------------------

m3 = re.search(r"def run_register", af_src)
check("存在 run_register", m3 is not None)
if m3:
    body = af_src[m3.start(): m3.start() + 9000]

    i_csrf = body.find("self.get_csrf_token()")
    i_order = body.find("mail_provider.create_mailbox()")
    i_authurl = body.find("self.get_auth_url(")

    check(
        "下单调用存在",
        i_order != -1,
    )
    check(
        "CSRF 调用存在",
        i_csrf != -1,
    )
    check(
        "get_auth_url 调用存在",
        i_authurl != -1,
    )
    if -1 not in (i_csrf, i_order, i_authurl):
        check(
            "下单在 CSRF 之后（第 1 步后）",
            i_order > i_csrf,
            f"csrf@{i_csrf} order@{i_order}",
        )
        check(
            "下单在 get_auth_url 之前（第 2 步前）",
            i_order < i_authurl,
            f"order@{i_order} authurl@{i_authurl}",
        )
        check(
            "下单在 warmup 之后（warmup 失败不白下单）",
            body.find("self.warmup()") < i_order,
        )

    # 第 2 步必须传 email（login_hint 要用）
    seg = body[i_authurl: i_authurl + 200]
    check(
        "get_auth_url 仍然传了 email（login_hint 依赖它）",
        "email=email" in seg,
        seg[:80],
    )


# ---------------------------------------------------------------------------
section("4. 语法完整性")
# ---------------------------------------------------------------------------

for rel in ("auth_flow.py", "fingerprint.py"):
    try:
        ast.parse(read(rel))
        check(f"{rel} 语法正确", True)
    except SyntaxError as e:
        check(f"{rel} 语法正确", False, str(e))


# ---------------------------------------------------------------------------
print("\n" + "=" * 60)
print(f"  通过 {OK} / 失败 {FAIL}")
print("=" * 60)
sys.exit(1 if FAIL else 0)
