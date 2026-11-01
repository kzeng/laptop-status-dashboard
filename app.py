"""FastAPI application for the refresh-driven laptop status dashboard."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from collector import collect_snapshot


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DASHBOARD_DATA_DIR", str(BASE_DIR / "data")))
CACHE_FILE = DATA_DIR / "snapshot.json"
MIN_REFRESH_SECONDS = int(os.getenv("DASHBOARD_MIN_REFRESH_SECONDS", "10"))
SESSION_TTL_SECONDS = int(os.getenv("DASHBOARD_SESSION_TTL_SECONDS", str(7 * 86400)))
COOKIE_SECURE = os.getenv("DASHBOARD_COOKIE_SECURE", "1") != "0"
SESSION_COOKIE = "laptop_status_session"
PASSWORD_HASH = os.getenv("DASHBOARD_PASSWORD_HASH", "")
SESSION_SECRET = os.getenv("DASHBOARD_SESSION_SECRET", "")


def make_password_hash(password: str, iterations: int = 310_000) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(
        iterations,
        base64.urlsafe_b64encode(salt).decode().rstrip("="),
        base64.urlsafe_b64encode(digest).decode().rstrip("="),
    )


def _decode_b64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iterations_raw, salt_raw, digest_raw = stored.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        expected = _decode_b64(digest_raw)
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), _decode_b64(salt_raw), int(iterations_raw)
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def _sign_session(expires: int) -> str:
    payload = f"v1.{expires}"
    signature = hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).digest()
    encoded = base64.urlsafe_b64encode(signature).decode().rstrip("=")
    return f"{payload}.{encoded}"


def _valid_session(token: str | None) -> bool:
    if not token or not SESSION_SECRET:
        return False
    try:
        version, expires_raw, signature = token.split(".", 2)
        expires = int(expires_raw)
    except (ValueError, TypeError):
        return False
    if version != "v1" or expires < int(time.time()):
        return False
    expected = _sign_session(expires)
    return hmac.compare_digest(token, expected)


def _safe_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")


class SnapshotStore:
    def __init__(
        self,
        cache_file: Path,
        collector: Callable[[], dict[str, Any]],
        min_refresh_seconds: int,
    ) -> None:
        self.cache_file = cache_file
        self.collector = collector
        self.min_refresh_seconds = min_refresh_seconds
        self._lock = threading.Lock()

    def load(self) -> dict[str, Any] | None:
        try:
            data = json.loads(self.cache_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except (OSError, json.JSONDecodeError):
            return None

    @staticmethod
    def _age_seconds(snapshot: dict[str, Any] | None) -> float | None:
        if not snapshot or not snapshot.get("collected_at"):
            return None
        try:
            collected = datetime.fromisoformat(snapshot["collected_at"])
            now = datetime.now().astimezone()
            return max(0.0, (now - collected).total_seconds())
        except (ValueError, TypeError):
            return None

    def refresh(self) -> tuple[dict[str, Any], str]:
        with self._lock:
            cached = self.load()
            age = self._age_seconds(cached)
            if cached is not None and age is not None and age < self.min_refresh_seconds:
                return cached, "recent-cache"
            snapshot = self.collector()
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.cache_file.with_suffix(".tmp")
            temporary.write_text(_safe_json(snapshot), encoding="utf-8")
            os.replace(temporary, self.cache_file)
            return snapshot, "fresh"


store = SnapshotStore(CACHE_FILE, collect_snapshot, MIN_REFRESH_SECONDS)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app = FastAPI(title="T470s Status Dashboard", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.middleware("http")
async def response_headers(request: Request, call_next: Callable[..., Any]) -> Response:
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'"
    )
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    elif request.url.path != "/healthz":
        response.headers["Cache-Control"] = "no-store"
    return response


def _authenticated(request: Request) -> bool:
    return _valid_session(request.cookies.get(SESSION_COOKIE))


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> Response:
    if _authenticated(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": False})


@app.post("/login", response_class=HTMLResponse)
async def login(request: Request) -> Response:
    body = (await request.body()).decode("utf-8", errors="replace")
    password = parse_qs(body).get("password", [""])[0]
    if not PASSWORD_HASH or not verify_password(password, PASSWORD_HASH):
        await run_in_threadpool(time.sleep, 0.35)
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": True},
            status_code=401,
        )
    expires = int(time.time()) + SESSION_TTL_SECONDS
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        _sign_session(expires),
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="strict",
        path="/",
    )
    return response


@app.post("/logout")
async def logout() -> Response:
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> Response:
    if not _authenticated(request):
        return RedirectResponse("/login", status_code=303)
    cached = store.load()
    return templates.TemplateResponse(
        request,
        "index.html",
        {"snapshot_json": _safe_json(cached), "has_snapshot": cached is not None},
    )


@app.post("/api/refresh")
async def refresh(request: Request) -> Response:
    if not _authenticated(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        snapshot, source = await run_in_threadpool(store.refresh)
    except Exception as exc:
        cached = store.load()
        if cached is None:
            return JSONResponse(
                {"error": "collection_failed", "detail": type(exc).__name__},
                status_code=503,
            )
        return JSONResponse(
            {
                "snapshot": cached,
                "source": "stale-fallback",
                "warning": "最新采集失败，继续显示上次快照",
            }
        )
    return JSONResponse({"snapshot": snapshot, "source": source})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="127.0.0.1", port=8765)

