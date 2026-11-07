"""FastAPI application assembly and admin authentication."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
import os
import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .routers import (
    accounts,
    auto_loop,
    exports,
    mail,
    payment,
    proxies,
    registration,
    sms,
)

ROOT = Path(__file__).resolve().parents[2]
STATIC_DIR = ROOT / "webui" / "static"

app = FastAPI(title="Regix WebUI", docs_url=None, redoc_url=None)


# Admin auth covers API, SSE, and static files. Basic Auth encodes but does not
# encrypt credentials; public deployments still need an HTTPS reverse proxy.
ADMIN_USERNAME = os.environ.get("WEBUI_ADMIN_USERNAME", "admin").strip() or "admin"
ADMIN_PASSWORD = os.environ.get("WEBUI_ADMIN_PASSWORD", "")
_AUTH_FAILED_TIMES: dict[str, deque[float]] = defaultdict(deque)
_AUTH_FAILURE_WINDOW = 300.0
_AUTH_FAILURE_LIMIT = 10


def _auth_digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


def _unauthorized(realm: str = "WebUI Admin") -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={"detail": "需要管理员登录"},
        headers={"WWW-Authenticate": f'Basic realm="{realm}", charset="UTF-8"'},
    )


@app.middleware("http")
async def admin_basic_auth(request: Request, call_next):
    if not ADMIN_PASSWORD:
        return await call_next(request)

    client_ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    failures = _AUTH_FAILED_TIMES[client_ip]
    while failures and now - failures[0] >= _AUTH_FAILURE_WINDOW:
        failures.popleft()
    if len(failures) >= _AUTH_FAILURE_LIMIT:
        return JSONResponse(
            status_code=429,
            content={"detail": "登录尝试过多，请稍后再试"},
            headers={"Retry-After": str(int(_AUTH_FAILURE_WINDOW))},
        )

    header = request.headers.get("Authorization", "")
    scheme, _, encoded = header.partition(" ")
    credentials_ok = False
    if scheme.lower() == "basic" and encoded.strip():
        try:
            decoded = base64.b64decode(encoded.strip(), validate=True).decode("utf-8")
            username, _, password = decoded.partition(":")
            credentials_ok = hmac.compare_digest(
                _auth_digest(username), _auth_digest(ADMIN_USERNAME)
            ) and hmac.compare_digest(
                _auth_digest(password), _auth_digest(ADMIN_PASSWORD)
            )
        except (UnicodeDecodeError, binascii.Error, ValueError):
            credentials_ok = False

    if not credentials_ok:
        failures.append(now)
        return _unauthorized()
    return await call_next(request)


app.include_router(accounts.router)
app.include_router(proxies.router)
app.include_router(registration.router)
app.include_router(mail.router)
app.include_router(sms.router)
app.include_router(exports.router)
app.include_router(auto_loop.router)
app.include_router(payment.router)


# 首屏主要是那两坨 bundle（JS ~400KB / CSS ~358KB）。文本压缩比很高：
#   JS 400KB -> ~147KB，CSS 358KB -> ~50KB，响应体积直接砍到 1/4。
# minimum_size 给 1024，小 JSON / SSE 心跳不压缩（SSE 不能缓冲）。
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)


@app.middleware("http")
async def cache_headers(request: Request, call_next):
    """Hashed bundles can be cached forever; the shell and API cannot.

    Vite puts a content hash in every bundle name, so a cached copy can never
    be stale — a new build produces a new URL. ``index.html`` must revalidate
    so a deploy is picked up immediately, and JSON API answers must not be
    cached by the browser.
    """
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/static/assets/"):
        response.headers["cache-control"] = "public, max-age=31536000, immutable"
    elif path.startswith("/static/"):
        response.headers["cache-control"] = "public, max-age=86400"
    elif path == "/" or path.endswith(".html"):
        response.headers["cache-control"] = "no-cache"
    elif path.startswith("/api/") and path != "/api/auto/stream":
        response.headers.setdefault("cache-control", "no-store")
    return response


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/")
def root():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Import side effects release stale claims and initialize logging only after the
# storage facade is wired. Keeping it here avoids a hidden import-order contract.
_released = 0
try:
    from ..storage import facade as db

    _released = db.release_stale_in_use(stale_seconds=1800)
    if _released > 0:
        logging.getLogger("regix.api").info(
            "[startup] released %s stale account claims", _released
        )
except Exception as exc:
    logging.getLogger("regix.api").warning(
        "[startup] release_stale failed: %s", exc
    )
