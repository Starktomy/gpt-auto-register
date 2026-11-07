#!/usr/bin/env python3
"""Regix WebUI launcher.

Usage:
    regix-webui                             # default 127.0.0.1:8765
    regix-webui --port 9000
    regix-webui --host 0.0.0.0 --port 8765
"""
from __future__ import annotations

import argparse
import os
import sys
import webbrowser
from pathlib import Path

# Windows 控制台 GBK 编码兼容：强制 UTF-8 输出
if sys.platform.startswith("win"):
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parents[2]


def load_env_file(path: Path) -> None:
    """加载简单 KEY=VALUE 配置；不展开变量，避免把值意外交给 shell。"""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key:
            os.environ.setdefault(key, value)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1", help="监听地址 (默认 127.0.0.1)")
    ap.add_argument("--port", type=int, default=8765, help="监听端口 (默认 8765)")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    ap.add_argument("--reload", action="store_true", help="开发模式 (代码改动自动重启)")
    args = ap.parse_args()

    # 确保依赖装了
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
    except ImportError:
        print("[!] 缺少依赖，正在安装 fastapi / uvicorn ...")
        import subprocess
        subprocess.check_call([
            sys.executable, "-m", "pip", "install",
            "fastapi", "uvicorn[standard]", "pydantic>=2",
        ])
        import uvicorn  # noqa: F401

    load_env_file(ROOT / ".env")
    public_host = args.host not in {"127.0.0.1", "localhost", "::1"}
    if public_host and not os.environ.get("WEBUI_ADMIN_PASSWORD", "").strip():
        print("[!] 公网监听必须先设置 WEBUI_ADMIN_PASSWORD（建议放 .env）", file=sys.stderr)
        sys.exit(2)

    url = f"http://{args.host if args.host != '0.0.0.0' else '127.0.0.1'}:{args.port}/"
    print("\nRegix WebUI starting...")
    print(f"   访问: {url}\n")

    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    uvicorn.run(
        "regix.api.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
