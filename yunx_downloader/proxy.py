"""Recover the public origin from common reverse proxy headers."""

import re
from urllib.parse import unquote, urlsplit

from fastapi import HTTPException, Request


def _first(value: str) -> str:
    return value.split(",", 1)[0].strip()


def _forwarded(value: str) -> dict[str, str]:
    # A quoted RFC 7239 value may itself contain a comma or semicolon.
    first = re.match(r'((?:[^",]|"(?:\\.|[^"\\])*")*)', value).group(1)
    fields = {}
    for match in re.finditer(r'(?:^|;)\s*(host|proto)\s*=\s*("(?:\\.|[^"\\])*"|[^;\s,]+)\s*(?=;|$)', first, re.I):
        raw = match.group(2)
        if raw.startswith('"'):
            raw = re.sub(r'\\(.)', r'\1', raw[1:-1])
        fields[match.group(1).lower()] = raw
    return fields


def _authority(value: str) -> tuple[str, int | None]:
    if not re.fullmatch(r'(?:\[[0-9a-fA-F:.]+\]|[a-zA-Z0-9.-]+)(?::[0-9]+)?', value):
        raise HTTPException(400, "反代 Host 格式无效")
    try:
        parsed = urlsplit("//" + value)
        port = parsed.port
        if not parsed.hostname or (port is not None and port < 1):
            raise ValueError("invalid host or port")
    except ValueError as error:
        raise HTTPException(400, "反代 Host 格式无效") from error
    return value, port


def public_request_url(request: Request, trust_proxy_headers: bool = True) -> str:
    """Use the outermost proxy's origin and external mount prefix.

    Forwarded takes precedence over X-Forwarded-Host/Proto. A proxy should
    overwrite these headers or retain the original external entry first.
    """
    headers = request.headers
    forwarded = _forwarded(headers.get("forwarded", "")) if trust_proxy_headers else {}
    scheme = request.scope.get("scheme", "http")
    host = headers.get("host", "")
    prefix = request.scope.get("root_path", "").rstrip("/")
    if trust_proxy_headers:
        scheme = forwarded.get("proto") or _first(headers.get("x-forwarded-proto", "")) or scheme
        host = forwarded.get("host") or _first(headers.get("x-forwarded-host", "")) or host
        if "x-forwarded-prefix" in headers:
            prefix = _first(headers["x-forwarded-prefix"]).rstrip("/")
    scheme = scheme.lower()
    if scheme not in ("http", "https"):
        raise HTTPException(400, "反代协议必须为 http 或 https")
    host, host_port = _authority(host)
    forwarded_port = _first(headers.get("x-forwarded-port", "")) if trust_proxy_headers else ""
    # Explicit Forwarded/Host ports already describe the public authority.
    if forwarded_port and not forwarded.get("host"):
        forwarded_host = _first(headers.get("x-forwarded-host", ""))
        if not forwarded_host or host_port is None:
            if not forwarded_port.isascii() or not forwarded_port.isdigit() or not 1 <= int(forwarded_port) <= 65535:
                raise HTTPException(400, "反代端口格式无效")
            hostname = urlsplit("//" + host).hostname
            host = f"[{hostname}]" if ":" in hostname else hostname
            if int(forwarded_port) != (443 if scheme == "https" else 80):
                host += f":{int(forwarded_port)}"
    if prefix:
        if not prefix.startswith("/") or prefix.startswith("//") or re.search(r'[\x00-\x20\x7f\\?#]', prefix):
            raise HTTPException(400, "反代路径前缀格式无效")
        decoded = unquote(prefix)
        if "\\" in decoded or any(part in (".", "..") for part in decoded.split("/")):
            raise HTTPException(400, "反代路径前缀格式无效")
    return f"{scheme}://{host}{prefix}"
