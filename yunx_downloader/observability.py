"""Console events with request/job context and no raw URLs or credentials."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
import json
import logging
import re
import sys
import time
import uuid


LOG = logging.getLogger("yunx.api")


@dataclass
class Context:
    request: str = ""
    job: str = ""
    step: str = ""
    media_index: int = 0
    media_total: int = 0


_context: ContextVar[Context | None] = ContextVar("unifetch_log_context", default=None)


def configure_console_logging() -> None:
    logger = logging.getLogger("yunx")
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


@contextmanager
def bind_context(**fields):
    current = _context.get() or Context()
    token = _context.set(replace(current, **fields))
    try:
        yield
    finally:
        _context.reset(token)


def request_id() -> str:
    current = _context.get()
    return current.request if current else ""


def event(name: str, *, level: int = logging.INFO, context: bool = True, **fields) -> None:
    values = {}
    current = _context.get()
    if context and current:
        values.update(request=current.request, job=current.job, step=current.step)
        if current.media_index:
            values.update(media_index=current.media_index, media_total=current.media_total)
    values.update(fields)
    details = " ".join(
        f"{key}={json.dumps(value, ensure_ascii=False, separators=(',', ':'))}"
        for key, value in values.items() if value not in ("", None)
    )
    LOG.log(level, "[%s] %s", name, details)


def step(message: str, **fields) -> None:
    current = _context.get()
    if current:
        current.step = message
        for key in ("media_index", "media_total"):
            if key in fields:
                setattr(current, key, fields[key])
    event("执行步骤", step=message, **fields)


def _request_target(path: str) -> tuple[str, str]:
    # Log route templates, never arbitrary path segments or query strings.
    match = re.search(r"/v1/jobs/([0-9a-f]{32})/?$", path)
    if match:
        return "/v1/jobs/{job}", match[1]
    match = re.search(r"/v1/files/([0-9a-f]{32})/[^/]+/?$", path)
    if match:
        return "/v1/files/{job}/{file}", match[1]
    for known in ("/v1/jobs", "/health", "/docs", "/openapi.json", "/shortcuts", "/source.tar.gz"):
        if path.rstrip("/").endswith(known):
            return known, ""
    return ("/" if path == "/" else "unknown"), ""


class RequestLogMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        target, job = _request_target(scope.get("path", ""))
        if target == "/health":
            return await self.app(scope, receive, send)
        started = time.monotonic()
        status = 500
        sent_bytes = 0

        async def tracked_send(message):
            nonlocal status, sent_bytes
            if message["type"] == "http.response.start":
                status = message["status"]
            elif message["type"] == "http.response.body":
                sent_bytes += len(message.get("body", b""))
            await send(message)

        with bind_context(request=uuid.uuid4().hex[:12], job=job, step=""):
            event("收到请求", method=scope["method"], path=target)
            try:
                await self.app(scope, receive, tracked_send)
            except Exception as error:
                event("请求异常", level=logging.ERROR, error_type=type(error).__name__)
                raise
            finally:
                event("请求完成", level=logging.WARNING if status >= 400 else logging.INFO,
                      method=scope["method"], path=target, status=status,
                      elapsed_ms=round((time.monotonic() - started) * 1000, 1), response_bytes=sent_bytes)
