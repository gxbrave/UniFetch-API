"""Token authenticated, persistent job API for Shortcuts and other clients."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
from html import escape
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
import requests

from .api_media import prepare_media
from .core import MediaKind, ParseError, Platform, _x_public_retry, detect_link, parse
from .proxy import public_request_url
from .observability import (RequestLogMiddleware, bind_context, configure_console_logging,
                            event, request_id, step)

LOG = logging.getLogger("yunx.api")


@dataclass
class Settings:
    token: str
    data_dir: Path
    public_base_url: str = ""
    retention_hours: int = 24
    max_pending: int = 10
    trust_proxy_headers: bool = True

    @classmethod
    def from_env(cls):
        token = os.environ.get("API_TOKEN", "")
        if len(token) < 24:
            raise RuntimeError("API_TOKEN 必须至少包含 24 个字符")
        return cls(token, Path(os.environ.get("DATA_DIR") or "/data"),
                   os.environ.get("PUBLIC_BASE_URL", "").rstrip("/"),
                   max(1, int(os.environ.get("RETENTION_HOURS") or "24")),
                   max(1, int(os.environ.get("MAX_PENDING_JOBS") or "10")),
                   (os.environ.get("TRUST_PROXY_HEADERS") or "true").lower() in ("1", "true", "yes"))


class JobInput(BaseModel):
    text: str = Field(min_length=1, max_length=16000)
    token: str | None = Field(default=None, max_length=512, exclude=True)


class JobStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.database = settings.data_dir / "jobs.sqlite3"
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="media-job")
        self.lock = threading.Lock()
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, status TEXT, created REAL, updated REAL, result TEXT, error TEXT)")
            recovered = db.execute("UPDATE jobs SET status='failed', error=?, updated=? WHERE status IN ('queued','running')",
                                   (json.dumps({"code": "restarted", "message": "服务重启中断了任务，请重新提交"}, ensure_ascii=False), time.time())).rowcount
        if recovered:
            event("恢复中断任务", count=recovered, status="failed")

    def connect(self):
        db = sqlite3.connect(self.database, timeout=20)
        db.row_factory = sqlite3.Row
        return db

    def submit(self, text: str) -> str:
        try:
            link = detect_link(text)
        except ParseError as error:
            event("任务拒绝", level=logging.WARNING, code=error.category)
            raise
        with self.lock:
            with self.connect() as db:
                pending = db.execute("SELECT status FROM jobs WHERE status IN ('queued','running')").fetchall()
                count = len(pending)
                if count >= self.settings.max_pending:
                    event("队列已满", level=logging.WARNING, pending=count, limit=self.settings.max_pending)
                    raise HTTPException(429, "任务队列已满，请稍后重试", headers={"Retry-After": "10"})
                identity = uuid.uuid4().hex
                now = time.time()
                db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?)", (identity, "queued", now, now, None, None))
            running = sum(row["status"] == "running" for row in pending)
            event("任务入队", job=identity, platform=link.platform.value, source_id=link.id or "short_link",
                  queue_position=count + 1, ahead=count, running=running, waiting=count - running + 1)
            # Keep scheduling inside the lock so executor order matches queue order.
            self.executor.submit(self.run, identity, link.url, request=request_id())
        return identity

    def _log_queue(self):
        """Called with self.lock held, after committing a queue state change."""
        with self.connect() as db:
            rows = db.execute("SELECT id, status FROM jobs WHERE status IN ('queued','running') ORDER BY rowid").fetchall()
        running = sum(row["status"] == "running" for row in rows)
        waiting = [row for row in rows if row["status"] == "queued"]
        for index, row in enumerate(waiting, 1):
            event("排队位置更新", context=False, job=row["id"], queue_position=running + index,
                  ahead=running + index - 1, running=running, waiting=len(waiting))
        if not rows:
            event("队列空闲", context=False, running=0, waiting=0)

    def update(self, identity: str, status: str, *, result=None, error=None):
        with self.connect() as db:
            db.execute("UPDATE jobs SET status=?, updated=?, result=?, error=? WHERE id=?",
                       (status, time.time(), json.dumps(result, ensure_ascii=False) if result else None,
                        json.dumps(error, ensure_ascii=False) if error else None, identity))

    def run(self, identity: str, text: str, *, request: str = ""):
        with bind_context(job=identity, request=request, step=""):
            self._run(identity, text)

    def _run(self, identity: str, text: str):
        directory = self.settings.data_dir / identity
        started = time.monotonic()
        try:
            with self.lock:
                row = self.get(identity)
                self.update(identity, "running")
                event("任务开始", queue_position=1, waited_ms=round((time.time() - row["created"]) * 1000, 1))
                self._log_queue()
            link = detect_link(text)
            platform = link.platform.value.upper()
            cookie = os.environ.get(f"{platform}_COOKIE", "")
            step("解析作品", platform=link.platform.value)
            parsed = parse(text, cookie=cookie, browser=os.environ.get("BROWSER_PATH") or None)
            event("解析完成", platform=parsed.platform.value, media_count=len(parsed.medias),
                  videos=parsed.video_count,
                  images=sum(media.kind is MediaKind.IMAGE and not media.motion_url for media in parsed.medias),
                  live_photos=sum(bool(media.motion_url) for media in parsed.medias),
                  qualities=list(dict.fromkeys(media.quality for media in parsed.medias if media.quality)))
            if parsed.platform is Platform.DOUYIN and any(media.kind is MediaKind.VIDEO and not media.quality for media in parsed.medias):
                raise ParseError("未取得最高画质列表，请重新提交", "quality_unavailable")
            step("准备媒体文件", media_count=len(parsed.medias))
            try:
                items = prepare_media(parsed, directory)
            except (ParseError, requests.RequestException, subprocess.SubprocessError):
                if parsed.platform is not Platform.X or parsed._x_public_used:
                    raise
                event("切换备用解析", level=logging.WARNING, reason="media_fetch_failed", attempt=1)
                step("X 免费备用 API")
                # Each directory belongs to one job. Discard the partial attempt
                # before retrying all media so clients see a consistent result.
                shutil.rmtree(directory, ignore_errors=True)
                parsed = _x_public_retry(parsed)
                items = prepare_media(parsed, directory)
            self.update(identity, "succeeded", result={"platform": parsed.platform.value, "title": parsed.title,
                                                       "author": parsed.author, "notice": parsed.notice, "media": items})
            event("任务完成", status="succeeded", media_count=len(items),
                  videos=sum(item["kind"] == "video" for item in items),
                  images=sum(item["kind"] == "image" for item in items),
                  live_photos=sum(item["kind"] == "live_photo" for item in items),
                  qualities=list(dict.fromkeys(item["quality"] for item in items if item.get("quality"))),
                  stored_bytes=sum(path.stat().st_size for item in items
                                   for key in ("file", "motion_file", "motion_photo_file")
                                   if key in item and (path := directory / item[key]).is_file()),
                  elapsed_ms=round((time.monotonic() - started) * 1000, 1))
        except Exception as error:
            # Keep platform URLs and cookies out of API responses and log messages.
            code = error.category if isinstance(error, ParseError) else "processing_failed"
            messages = {"blocked": "平台拦截了此次请求，请稍后重试", "network": "平台网络请求失败，请稍后重试",
                        "access_restricted": "平台未向当前会话开放此作品，可能需要在服务端配置 Cookie",
                        "not_found": "作品不存在或不可见", "unsupported": "链接不受支持或需要在服务端配置平台 Cookie",
                        "quality_unavailable": "未取得最高画质列表，请重新提交"}
            event("任务失败", level=logging.WARNING, status="failed", code=code,
                  error_type=type(error).__name__, message=messages.get(code, "媒体处理失败，请重新提交"),
                  elapsed_ms=round((time.monotonic() - started) * 1000, 1))
            self.update(identity, "failed", error={"code": code, "message": messages.get(code, "媒体处理失败，请重新提交")})
            shutil.rmtree(directory, ignore_errors=True)
        finally:
            with self.lock:
                self._log_queue()

    def get(self, identity: str) -> dict:
        if len(identity) != 32 or any(c not in "0123456789abcdef" for c in identity):
            raise HTTPException(404, "任务不存在")
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise HTTPException(404, "任务不存在或已过期")
        return dict(row)

    def cleanup(self):
        cutoff = time.time() - self.settings.retention_hours * 3600
        with self.lock, self.connect() as db:
            rows = db.execute("SELECT id FROM jobs WHERE updated < ? AND status IN ('succeeded','failed')", (cutoff,)).fetchall()
            for row in rows:
                shutil.rmtree(self.settings.data_dir / row["id"], ignore_errors=True)
                db.execute("DELETE FROM jobs WHERE id=?", (row["id"],))
        if rows:
            event("清理过期任务", count=len(rows))

    def close(self):
        self.executor.shutdown(wait=True, cancel_futures=False)


def create_app(settings: Settings | None = None) -> FastAPI:
    configure_console_logging()
    settings = settings or Settings.from_env()
    store: JobStore | None = None

    @asynccontextmanager
    async def lifespan(app):
        nonlocal store
        store = JobStore(settings)
        store.cleanup()
        event("服务就绪", workers=1, max_pending=settings.max_pending, retention_hours=settings.retention_hours)

        async def sweep():
            while True:
                await asyncio.sleep(600)
                await asyncio.to_thread(store.cleanup)

        task = asyncio.create_task(sweep())
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            await asyncio.to_thread(store.close)
            event("服务停止")

    app = FastAPI(title="UniFetch-API", version="1.0.0", lifespan=lifespan,
                  description="提交分享文本，轮询任务状态，再下载媒体。对应源码可从 /source.tar.gz 获取。")

    @app.middleware("http")
    async def no_cache(request: Request, call_next):
        # Apply the public origin to ASGI too, so docs and slash redirects use
        # the same scheme, authority and mount prefix as job responses.
        if settings.trust_proxy_headers:
            try:
                base = public_request_url(request)
            except HTTPException as error:
                return JSONResponse({"detail": error.detail}, status_code=error.status_code)
            origin = urlsplit(base)
            request.scope["scheme"] = origin.scheme
            request.scope["headers"] = [(name, value) for name, value in request.scope["headers"] if name.lower() != b"host"]
            request.scope["headers"].append((b"host", origin.netloc.encode("ascii")))
            request.scope["root_path"] = origin.path
            path = request.scope["path"]
            if origin.path and path != origin.path and not path.startswith(origin.path + "/"):
                request.scope["path"] = origin.path + path
                if "raw_path" in request.scope:
                    request.scope["raw_path"] = origin.path.encode("ascii") + request.scope["raw_path"]
        if int(request.headers.get("content-length", "0") or "0") > 32768:
            return JSONResponse({"detail": "请求体过大"}, status_code=413)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    def valid_token(value: str) -> bool:
        return secrets.compare_digest(value.encode(), settings.token.encode())

    bearer = HTTPBearer(auto_error=False)

    async def authenticate(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        token = credentials.credentials if credentials else request.headers.get("X-API-Token", "")
        if not token and request.method == "POST":
            try:
                token = (await request.json()).get("token", "")
            except (ValueError, AttributeError):
                pass
        if not isinstance(token, str) or not valid_token(token):
            raise HTTPException(401, "Token 无效", headers={"WWW-Authenticate": "Bearer"})

    def signature(identity: str, name: str, expires: int) -> str:
        message = f"{identity}/{name}/{expires}".encode()
        return hmac.new(settings.token.encode(), message, hashlib.sha256).hexdigest()

    def describe(identity: str, request: Request):
        row = store.get(identity)
        base = settings.public_base_url or public_request_url(request, settings.trust_proxy_headers)
        result = {"id": identity, "status": row["status"], "poll_url": f"{base}/v1/jobs/{identity}",
                  "created_at": row["created"], "source_url": f"{base}/source.tar.gz"}
        if row["error"]:
            result["error"] = json.loads(row["error"])
        if row["result"]:
            expires = int(row["updated"] + settings.retention_hours * 3600)
            payload = json.loads(row["result"])
            for item in payload["media"]:
                for field, url_field in (("file", "url"), ("motion_file", "motion_url"), ("motion_photo_file", "motion_photo_url")):
                    if field in item:
                        name = item.pop(field)
                        item[url_field] = f"{base}/v1/files/{identity}/{name}?expires={expires}&sig={signature(identity, name, expires)}"
                # Drop legacy file/package metadata from persisted rows while
                # keeping the current public media fields stable.
                for obsolete in tuple(item):
                    if obsolete.endswith(("_file", "_bytes", "_asset_id", "_verified")) or obsolete == "ios_import":
                        item.pop(obsolete, None)
            result.update(payload)
            result["expires_at"] = expires
        return result

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        base = escape(settings.public_base_url or public_request_url(request, settings.trust_proxy_headers), quote=True)
        return f'<html lang="zh"><meta charset="utf-8"><title>UniFetch-API</title><h1>UniFetch-API</h1><p>服务已启动。</p><p><a href="{base}/docs">接口文档</a> · <a href="{base}/shortcuts">快捷指令配置</a> · <a href="{base}/source.tar.gz">AGPL-3.0 对应源码</a></p></html>'

    @app.get("/shortcuts")
    def shortcut_guide():
        return FileResponse(Path(__file__).parent.parent / "docs" / "SHORTCUTS.md", media_type="text/plain; charset=utf-8")

    @app.get("/source.tar.gz")
    def source():
        path = Path(os.environ.get("SOURCE_ARCHIVE", "/app/source.tar.gz"))
        if not path.is_file():
            raise HTTPException(404, "源码归档未生成")
        return FileResponse(path, media_type="application/gzip", filename="unifetch-api-source.tar.gz")

    @app.post("/v1/jobs", status_code=202, dependencies=[Depends(authenticate)])
    def submit(payload: JobInput, request: Request):
        try:
            identity = store.submit(payload.text)
        except ParseError as error:
            raise HTTPException(422, str(error)) from error
        return describe(identity, request)

    @app.get("/v1/jobs/{identity}", dependencies=[Depends(authenticate)])
    def job(identity: str, request: Request):
        result = describe(identity, request)
        event("查询任务", job=identity, status=result["status"])
        return result

    @app.get("/v1/files/{identity}/{name}")
    def download(identity: str, name: str, request: Request, expires: int = 0, sig: str = ""):
        now = time.time()
        logged_identity = identity if re.fullmatch(r"[0-9a-f]{32}", identity) else "unknown"
        logged_name = name if re.fullmatch(r"\d{3}(?:\.motion)?\.(?:mp4|mov|jpg|jpeg|png|webp)", name) else "unknown"
        received_signature_length = len(sig)
        suffix = Path(name).suffix.lower()
        normalized_extension = ""
        # Shortcuts may append the requested file extension to a URL when
        # renaming it before download. The full 64-character HMAC still has
        # to match after removing exactly that known media extension.
        if suffix in (".mp4", ".mov", ".jpg", ".jpeg", ".png", ".webp", ".zip") and len(sig) == 64 + len(suffix) and sig.endswith(suffix):
            sig = sig[:-len(suffix)]
            normalized_extension = suffix
        reason = ""
        message = ""
        if not expires or not sig:
            reason = "missing_parameters"
            message = "下载链接缺少 expires 或 sig 参数，请使用 API 返回的完整媒体 url"
        elif expires < now:
            reason = "expired"
            message = "下载链接已过期，请重新获取任务结果"
        elif not hmac.compare_digest(sig.encode(), signature(identity, name, expires).encode()):
            reason = "signature_mismatch"
            message = "下载链接签名不匹配，请使用同一媒体 url 中的任务 ID、expires 和 sig"
        if reason:
            # Record why validation failed without logging a usable signature,
            # token, full URL or arbitrary query parameter values.
            parameter_flags = {
                "expires": "expires" in request.query_params,
                "sig": "sig" in request.query_params,
                "amp_sig": "amp;sig" in request.query_params,
            }
            LOG.warning(
                "download_rejected job=%s file=%s reason=%s expires=%d remaining_seconds=%d signature_length=%d parameters=%s",
                logged_identity, logged_name, reason, expires, int(expires - now), received_signature_length, parameter_flags,
            )
            raise HTTPException(401, message, headers={"X-Download-Error": reason})
        row = store.get(identity)
        if row["status"] != "succeeded":
            raise HTTPException(404, "媒体不存在")
        allowed = {item[field] for item in json.loads(row["result"])["media"]
                   for field in ("file", "motion_file", "motion_photo_file") if field in item}
        if name not in allowed or Path(name).name != name:
            raise HTTPException(404, "媒体不存在")
        path = settings.data_dir / identity / name
        if not path.is_file():
            raise HTTPException(404, "媒体已过期")
        headers = {}
        if normalized_extension:
            LOG.warning("download_signature_extension_normalized job=%s extension=%s", identity, normalized_extension)
            headers["X-Download-Signature-Normalized"] = "file-extension"
        event("发送媒体文件", job=identity, file=name, bytes=path.stat().st_size,
              range_request="range" in request.headers)
        return FileResponse(path, filename=name, headers=headers)

    app.add_middleware(RequestLogMiddleware)
    return app
