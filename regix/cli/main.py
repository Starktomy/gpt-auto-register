"""Unified CLI entry point backed by the same services as the WebUI."""
from __future__ import annotations

import argparse
import base64
import json
import queue
import re
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Optional

from regix.providers.mail import MailProviderError, get_provider_class
from regix.services import registration as registrar
from regix.storage import facade as db


SENSITIVE_KEYS = {
    "password",
    "totp_secret",
    "access_token",
    "session_token",
    "refresh_token",
    "id_token",
    "cookie_header",
    "service_token",
    "api_key",
    "admin_token",
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "<set>" if str(key).lower() in SENSITIVE_KEYS and item not in ("", None)
            else _sanitize(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    return value


def _print_json(value: Any, *, show_secrets: bool = False) -> None:
    print(_json(value if show_secrets else _sanitize(value)))


def _redact_log(line: str, *, show_secrets: bool) -> str:
    if show_secrets:
        return line
    line = re.sub(r"(pw=)\S+", r"\1<set>", line)
    return re.sub(r"(/ )\S+$", r"\1<set>", line)


def _epoch(value: Any) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(value)))
    except (TypeError, ValueError):
        return ""


def _account_summary(row: dict) -> dict:
    return {
        "email": row.get("email", ""),
        "kind": row.get("kind", ""),
        "status": row.get("status", ""),
        "imported_at": _epoch(row.get("imported_at")),
        "claimed_at": _epoch(row.get("claimed_at")),
        "finished_at": _epoch(row.get("finished_at")),
        "fail_reason": row.get("fail_reason", ""),
    }


def _run_summary(row: dict) -> dict:
    return {
        "run_id": row.get("run_id", ""),
        "email": row.get("email", ""),
        "status": row.get("status", ""),
        "started_at": _epoch(row.get("started_at")),
        "finished_at": _epoch(row.get("finished_at")),
        "error": row.get("error", ""),
        "error_category": row.get("error_category", ""),
        "log_path": row.get("log_path", ""),
    }


def _claim_account(email: str) -> tuple[dict, str, bool]:
    mail_source = db.get_setting("mail_source", "outlook")
    try:
        provider = get_provider_class(mail_source)
    except MailProviderError as exc:
        raise ValueError(str(exc)) from exc

    if not provider.pooled:
        now = int(time.time())
        return (
            {
                "email": f"{mail_source}_placeholder_{now}@placeholder.local",
                "password": "",
                "client_id": "",
                "refresh_token": "",
                "relay_url": "",
                "kind": mail_source,
            },
            mail_source,
            False,
        )

    account = db.claim_account(email) if email else db.claim_next(kind=mail_source)
    if not account:
        raise ValueError(
            f"号池里没有可用的 {provider.display_name} 账号"
            + (f"：{email}" if email else "")
        )
    actual_kind = account.get("kind") or "outlook"
    if actual_kind != mail_source:
        db.release_unused(account["email"])
        raise ValueError(
            f"{account['email']} 是 {actual_kind} 号，当前邮箱来源是 {mail_source}"
        )
    return account, mail_source, True


def _select_proxy(args: argparse.Namespace) -> tuple[str, bool]:
    proxy = str(getattr(args, "proxy", "") or "").strip()
    if getattr(args, "pool_proxy", False):
        if proxy:
            raise ValueError("--pool-proxy 和 --proxy 只能选一个")
        picked = db.pick_proxy()
        if not picked:
            raise ValueError("代理池没有可用代理")
        return picked, True
    if proxy and db.find_proxy(proxy):
        if not db.reserve_proxy(proxy):
            raise ValueError(f"代理配额预占失败：{proxy}")
        return proxy, True
    return proxy, False


def _event(line: str) -> Optional[dict]:
    if not line.startswith("__EVENT__:"):
        return None
    try:
        value = json.loads(line[len("__EVENT__:"):])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def cmd_register(args: argparse.Namespace) -> int:
    prefs = db.get_prefs()
    want_2fa = args.totp if args.totp is not None else bool(prefs.get("want2fa"))
    want_refresh = (
        args.refresh_token
        if args.refresh_token is not None
        else bool(prefs.get("wantRefreshToken"))
    )
    otp_timeout = args.otp_timeout or int(prefs.get("otpTimeout") or 180)

    account, mail_source, pooled = _claim_account(args.email or "")
    try:
        proxy, proxy_reserved = _select_proxy(args)
    except Exception:
        if pooled:
            db.release_unused(account["email"])
        raise
    options = {
        "want_access_token": args.access_token,
        "want_session_token": args.session_token,
        "want_refresh_token": want_refresh,
        "proxy": proxy,
        "otp_timeout": max(60, int(otp_timeout)),
        "allow_existing_login": args.allow_existing_login,
        "want_2fa": want_2fa,
    }
    if proxy_reserved:
        options["proxy_reserved"] = 1

    try:
        run_id = registrar.start_registration(account, options)
    except Exception:
        if pooled:
            db.release_unused(account["email"])
        raise

    started = {
        "ok": True,
        "run_id": run_id,
        "email": account["email"],
        "mail_source": mail_source,
        "want_2fa": want_2fa,
        "proxy": proxy,
    }
    if args.json:
        _print_json(started, show_secrets=args.show_secrets)
    else:
        print(
            f"run_id={run_id} email={account['email']} "
            f"totp={'on' if want_2fa else 'off'} proxy={proxy or 'direct'}",
            file=sys.stderr,
        )

    q = registrar.get_run_queue(run_id)
    if q is None:
        raise RuntimeError("注册任务已结束，无法读取实时日志")

    result: dict = {}
    failed: dict = {}
    try:
        while True:
            try:
                line = q.get(timeout=60)
            except queue.Empty:
                continue
            if line is None:
                break
            if not line:
                continue
            event = _event(line)
            if event is not None:
                kind = str(event.get("kind") or "")
                if kind == "done":
                    result = event
                elif kind == "error":
                    failed = event
                elif not args.json and not args.quiet:
                    print(f"[status] {_json(event)}", file=sys.stderr)
                continue
            if not args.json and not args.quiet:
                print(_redact_log(line, show_secrets=args.show_secrets), file=sys.stderr)
    finally:
        registrar.remove_run_queue(run_id)

    payload = dict(failed or result or {"kind": "unknown_end"})
    payload["run_id"] = run_id
    payload["email"] = account["email"]
    if args.json or not args.quiet:
        _print_json(payload, show_secrets=args.show_secrets)
    return 1 if failed else 0


def cmd_status(args: argparse.Namespace) -> int:
    from regix.services.auto_loop import CONTROLLER

    payload = {
        "mail_source": db.get_setting("mail_source", "outlook"),
        "pool": db.stats(),
        "pool_by_kind": db.stats_by_kind(),
        "proxies": db.proxy_pool_stats(),
        "prefs": db.get_prefs(),
        "auto": CONTROLLER.status(),
        "active_runs": [
            _run_summary(row)
            for row in db.list_runs(limit=100)
            if row.get("status") == "running"
        ],
    }
    _print_json(payload, show_secrets=args.show_secrets)
    return 0


def cmd_accounts(args: argparse.Namespace) -> int:
    rows = db.list_accounts(
        status=args.status, kind=args.kind, limit=args.limit, offset=args.offset
    )
    _print_json(
        {"total": len(rows), "items": [_account_summary(row) for row in rows]},
        show_secrets=args.show_secrets,
    )
    return 0


def cmd_runs(args: argparse.Namespace) -> int:
    rows = db.list_runs(limit=args.limit)
    _print_json({"total": len(rows), "items": [_run_summary(row) for row in rows]})
    return 0


def cmd_registered(args: argparse.Namespace) -> int:
    if args.email:
        row = db.get_registered(args.email)
        if not row:
            print(f"not found: {args.email}", file=sys.stderr)
            return 1
        rows = [row]
    else:
        rows = db.list_registered(limit=args.limit, offset=args.offset, filter_rt=args.filter)
    _print_json({"total": len(rows), "items": rows}, show_secrets=args.show_secrets)
    return 0


def cmd_codex_login(args: argparse.Namespace) -> int:
    """Re-login an account so it gets a Codex refresh token."""
    from regix.services.codex_login import CodexLoginError, mint_codex_refresh_token

    try:
        info = mint_codex_refresh_token(
            args.email,
            proxy=args.proxy,
            otp_timeout=args.otp_timeout,
        )
    except CodexLoginError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    _print_json(info, show_secrets=args.show_secrets)
    return 0


def cmd_bind_2fa(args: argparse.Namespace) -> int:
    """Bind TOTP 2FA to a registered account that does not have one."""
    from regix.services.two_factor import add_totp_2fa

    info = add_totp_2fa(args.email, proxy=args.proxy, otp_timeout=args.otp_timeout)
    _print_json(info, show_secrets=args.show_secrets)
    return 0 if info.get("ok") else 1


def cmd_set_proxy(args: argparse.Namespace) -> int:
    """Set (or clear) the proxy stored for one registered account."""
    ok = db.update_registered_proxy(args.email, args.proxy or "")
    if not ok:
        print(f"not found: {args.email}", file=sys.stderr)
        return 1
    _print_json(
        {"ok": True, "email": args.email.strip().lower(), "proxy": (args.proxy or "").strip()},
        show_secrets=args.show_secrets,
    )
    return 0


def cmd_payment_link(args: argparse.Namespace) -> int:
    """Generate a Plus/Team checkout link for a registered account."""
    from regix.services.payment import PaymentLinkError, generate_payment_link_for_account

    try:
        link = generate_payment_link_for_account(
            args.email,
            plan=args.plan,
            profile=args.profile,
            country=args.country,
            currency=args.currency,
            locale=args.locale,
            workspace_name=args.workspace_name,
            interval=args.interval,
            seats=args.seats,
            promo_code=args.promo_code,
            promo_campaign_id=args.promo_campaign,
            proxy=args.proxy,
            timeout_seconds=args.timeout,
        )
    except PaymentLinkError as exc:
        print(f"error: {exc.code}", file=sys.stderr)
        return 1
    _print_json(link.to_dict(), show_secrets=args.show_secrets)
    return 0


def cmd_payment_profiles(args: argparse.Namespace) -> int:
    from regix.services.payment import PAYMENT_PROFILES

    _print_json(
        {
            "items": [
                {
                    "id": item.id,
                    "label": item.label,
                    "country": item.country,
                    "currency": item.currency,
                    "locale": item.locale,
                    "note": item.note,
                }
                for item in PAYMENT_PROFILES
            ]
        },
        show_secrets=args.show_secrets,
    )
    return 0


def _http_request(args: argparse.Namespace) -> int:
    url = args.base_url.rstrip("/") + "/" + args.path.lstrip("/")
    data = None
    if args.data is not None:
        data = args.data.encode("utf-8")
    elif args.input:
        data = sys.stdin.buffer.read()

    req = urllib.request.Request(url, data=data, method=args.method.upper())
    req.add_header("accept", "application/json")
    if data is not None:
        req.add_header("content-type", "application/json")
    username = args.username if args.username is not None else os.environ.get("WEBUI_ADMIN_USERNAME")
    password = args.password if args.password is not None else os.environ.get("WEBUI_ADMIN_PASSWORD")
    if username is not None or password is not None:
        token = base64.b64encode(f"{username or ''}:{password or ''}".encode()).decode()
        req.add_header("authorization", f"Basic {token}")

    try:
        with urllib.request.urlopen(req, timeout=args.timeout) as resp:
            body = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:
        body = exc.read()
        status = exc.code
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        parsed = body.decode("utf-8", errors="replace")
    _print_json({"status": status, "data": parsed}, show_secrets=args.show_secrets)
    return 0 if status < 400 else 1


def _add_common_output(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--show-secrets",
        action="store_true",
        help="输出密码/TOTP/token 等敏感字段",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="regix", description="Regix WebUI 同链路 CLI")
    sub = parser.add_subparsers(dest="command")

    reg = sub.add_parser("register", help="从当前邮箱号池跑一次注册")
    reg.add_argument("email", nargs="?", help="可选：指定号池邮箱；留空自动领取")
    reg.add_argument(
        "--totp",
        action=argparse.BooleanOptionalAction,
        help="注册后绑定 TOTP；默认读 WebUI 偏好",
    )
    reg.add_argument("--access-token", action=argparse.BooleanOptionalAction, default=True)
    reg.add_argument("--session-token", action=argparse.BooleanOptionalAction, default=True)
    reg.add_argument("--refresh-token", action=argparse.BooleanOptionalAction)
    reg.add_argument("--proxy", help="指定代理；若在代理池中会预占配额")
    reg.add_argument("--pool-proxy", action="store_true", help="从代理池动态选择并预占配额")
    reg.add_argument("--otp-timeout", type=int, help="OTP 等待秒数；默认读 WebUI 偏好")
    reg.add_argument("--allow-existing-login", action=argparse.BooleanOptionalAction, default=True)
    reg.add_argument("--json", action="store_true", help="只输出 JSON，注册日志走 stderr")
    reg.add_argument("--quiet", action="store_true", help="不打印注册日志和最终 JSON")
    _add_common_output(reg)
    reg.set_defaults(func=cmd_register)

    status = sub.add_parser("status", help="查看号池、代理、偏好和运行状态")
    _add_common_output(status)
    status.set_defaults(func=cmd_status)

    accounts = sub.add_parser("accounts", help="查看邮箱号池")
    accounts.add_argument("--status", choices=("available", "in_use", "done", "failed"))
    accounts.add_argument("--kind")
    accounts.add_argument("--limit", type=int, default=50)
    accounts.add_argument("--offset", type=int, default=0)
    _add_common_output(accounts)
    accounts.set_defaults(func=cmd_accounts)

    runs = sub.add_parser("runs", help="查看注册运行记录")
    runs.add_argument("--limit", type=int, default=20)
    runs.set_defaults(func=cmd_runs)

    registered = sub.add_parser("registered", help="查看注册结果")
    registered.add_argument("email", nargs="?")
    registered.add_argument(
        "--filter",
        choices=("all", "free", "plus", "banned", "token_invalid"),
        default="all",
    )
    registered.add_argument("--limit", type=int, default=20)
    registered.add_argument("--offset", type=int, default=0)
    _add_common_output(registered)
    registered.set_defaults(func=cmd_registered)

    codex_login = sub.add_parser(
        "codex-login", help="给已注册账号补跑一次 Codex OAuth，拿回 refresh_token"
    )
    codex_login.add_argument("email", help="已注册账号的邮箱")
    codex_login.add_argument("--proxy", default="", help="代理；留空则用该号注册时的代理")
    codex_login.add_argument("--otp-timeout", type=int, default=180)
    _add_common_output(codex_login)
    codex_login.set_defaults(func=cmd_codex_login)

    bind_2fa = sub.add_parser("bind-2fa", help="给已注册账号补绑 TOTP 2FA（已有 secret 不覆盖）")
    bind_2fa.add_argument("email", help="已注册账号的邮箱")
    bind_2fa.add_argument("--proxy", default="", help="代理；留空则用该号当前的代理")
    bind_2fa.add_argument("--otp-timeout", type=int, default=180)
    _add_common_output(bind_2fa)
    bind_2fa.set_defaults(func=cmd_bind_2fa)

    set_proxy = sub.add_parser("set-proxy", help="设置某个已注册账号的代理（留空=直连）")
    set_proxy.add_argument("email", help="已注册账号的邮箱")
    set_proxy.add_argument("proxy", nargs="?", default="", help="代理 URL；留空则清除")
    _add_common_output(set_proxy)
    set_proxy.set_defaults(func=cmd_set_proxy)

    payment_link = sub.add_parser("payment-link", help="为已注册账号生成 Plus/Team 支付链接")
    payment_link.add_argument("email", help="已注册账号的邮箱")
    payment_link.add_argument("--plan", choices=("plus", "team"), default="plus")
    payment_link.add_argument("--profile", default="", help="支付区域预设 id，例如 paypal_de")
    payment_link.add_argument("--country", default="", help="账单国家，优先于 profile")
    payment_link.add_argument("--currency", default="", help="账单币种，优先于 profile")
    payment_link.add_argument("--locale", default="", help="支付页语言标签")
    payment_link.add_argument("--workspace-name", default="", help="Team 工作区名称")
    payment_link.add_argument("--interval", choices=("month", "year"), default="month")
    payment_link.add_argument("--seats", type=int, default=2, help="Team 席位数")
    payment_link.add_argument("--promo-code", default="", help="自定义优惠码")
    payment_link.add_argument(
        "--promo-campaign", default="", help="官方试用活动 id，例如 plus-1-month-free"
    )
    payment_link.add_argument("--proxy", default="", help="代理；留空则用该号注册时的代理")
    payment_link.add_argument("--timeout", type=float, default=20.0)
    _add_common_output(payment_link)
    payment_link.set_defaults(func=cmd_payment_link)

    payment_profiles = sub.add_parser("payment-profiles", help="列出支付区域预设")
    _add_common_output(payment_profiles)
    payment_profiles.set_defaults(func=cmd_payment_profiles)

    api = sub.add_parser("api", help="调用 WebUI API（覆盖未单独封装的功能）")
    api.add_argument("method", choices=("get", "post", "patch", "delete"))
    api.add_argument("path", help="API path，例如 /api/settings/prefs")
    api.add_argument("--data", help="JSON request body")
    api.add_argument("--input", action="store_true", help="从 stdin 读取 request body")
    api.add_argument("--base-url", default="http://127.0.0.1:8765")
    api.add_argument("--username")
    api.add_argument("--password")
    api.add_argument("--timeout", type=float, default=30.0)
    _add_common_output(api)
    api.set_defaults(func=_http_request)

    legacy = sub.add_parser("legacy", add_help=False)
    legacy.add_argument("legacy_args", nargs=argparse.REMAINDER)
    legacy.set_defaults(legacy=True)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "legacy", False):
        from regix.cli.register_outlook import main as legacy_main
        legacy_argv = args.legacy_args
        return int(legacy_main(legacy_argv) or 0)
    if not args.command:
        parser.print_help()
        return 2
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except (ValueError, RuntimeError, MailProviderError, urllib.error.URLError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
