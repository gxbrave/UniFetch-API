from __future__ import annotations

import html
import base64
import json
import logging
import os
import re
import shutil
import signal
import socket
import struct
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterable, Mapping, Optional
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import urlopen

import requests


UA_CHROME = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
UA_IPHONE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

_OPPO_EXIF_HEX = (
    "FFE100724578696600004D4D002A0000000800040100000400000001000005A001010004000000010000043C"
    "87690004000000010000003E011200030000000100000000000000000002928600020000000E0000005C"
    "920800040000000100000000000000006F706C75735F3833383836303800"
)


class Platform(str, Enum):
    DOUYIN = "douyin"
    XHS = "xiaohongshu"
    BILIBILI = "bilibili"
    X = "x"


class MediaKind(str, Enum):
    VIDEO = "video"
    IMAGE = "image"
    AUDIO = "audio"


@dataclass
class Media:
    url: str
    kind: MediaKind
    file_name: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    quality: str = ""
    size: int = -1
    motion_url: str = ""
    audio_url: str = ""
    audio_headers: dict[str, str] = field(default_factory=dict)


@dataclass
class ParseResult:
    platform: Platform
    title: str
    url: str
    author: str = ""
    text: str = ""
    cover: str = ""
    medias: list[Media] = field(default_factory=list)
    notice: str = ""
    canonical_url: str = ""
    _x_public_used: bool = field(default=False, repr=False, compare=False)

    @property
    def video_count(self) -> int:
        return sum(m.kind is MediaKind.VIDEO for m in self.medias)

    @property
    def image_count(self) -> int:
        return sum(m.kind is MediaKind.IMAGE for m in self.medias)


class ParseError(RuntimeError):
    """A user-facing parsing failure with a stable category."""

    def __init__(self, message: str, category: str = "unknown") -> None:
        super().__init__(message)
        self.category = category


@dataclass(frozen=True)
class DetectedLink:
    platform: Platform
    kind: str
    id: str
    url: str
    short: bool = False


_URL_RE = re.compile(r"https?://[^\s]+", re.I)
_TRAILING = "。，,；;)]}\"'、！？…"
_RULES: tuple[tuple[Platform, str, re.Pattern[str]], ...] = (
    (Platform.DOUYIN, "short", re.compile(r"(?:v|jx)\.douyin\.com/[A-Za-z0-9_-]+", re.I)),
    (Platform.DOUYIN, "video", re.compile(r"(?:www\.)?douyin\.com/(?:video|note)/(\d{10,})", re.I)),
    (Platform.DOUYIN, "video", re.compile(r"douyin\.com/[^\s]*[?&]modal_id=(\d{10,})", re.I)),
    (Platform.DOUYIN, "video", re.compile(r"iesdouyin\.com/share/(?:slides|video|note)/(\d{10,})", re.I)),
    (Platform.XHS, "short", re.compile(r"xhslink\.(?:com|cn)/[A-Za-z0-9._?%&+=/#@-]+", re.I)),
    (Platform.XHS, "note", re.compile(r"xiaohongshu\.com/(?:explore|discovery/item|discovery)/([0-9A-Za-z]{10,})", re.I)),
    (Platform.BILIBILI, "short", re.compile(r"(?:b23\.tv|bili2233\.cn)/[A-Za-z0-9]+", re.I)),
    (Platform.BILIBILI, "video", re.compile(r"bilibili\.com/video/(BV[0-9A-Za-z]{10}|av\d+)", re.I)),
    (Platform.X, "status", re.compile(r"(?:x|twitter)\.com/([A-Za-z0-9_]+)/status/(\d{8,})", re.I)),
)


def detect_link(text: str) -> DetectedLink:
    raw = _URL_RE.search(text.strip())
    if not raw:
        raise ParseError("输入文本里没有 http(s) 链接", "unsupported")
    url = raw.group(0).rstrip(_TRAILING)
    host = (urlsplit(url).hostname or "").lower()
    allowed_hosts = ("douyin.com", "iesdouyin.com", "xiaohongshu.com", "xhslink.com", "xhslink.cn", "bilibili.com", "b23.tv", "bili2233.cn", "x.com", "twitter.com")
    if not any(host == domain or host.endswith("." + domain) for domain in allowed_hosts):
        raise ParseError("暂不支持该链接域名", "unsupported")
    for platform, kind, pattern in _RULES:
        match = pattern.search(url)
        if not match:
            continue
        is_short = kind == "short"
        if platform is Platform.X:
            return DetectedLink(platform, kind, match.group(2), url, False)
        return DetectedLink(platform, kind, "" if is_short else match.group(1), url, is_short)
    raise ParseError("暂不支持该链接", "unsupported")


def _request(
    session: requests.Session,
    url: str,
    *,
    headers: Optional[Mapping[str, str]] = None,
    params: Optional[Mapping[str, object]] = None,
    timeout: float = 30,
    allow_redirects: bool = True,
    stream: bool = False,
) -> requests.Response:
    try:
        response = session.get(
            url,
            params=dict(params or {}),
            headers=dict(headers or {}),
            timeout=timeout,
            allow_redirects=allow_redirects,
            stream=stream,
        )
    except requests.RequestException as exc:
        raise ParseError(f"网络请求失败：{url}", "network") from exc
    if response.status_code in (403, 418, 429):
        raise ParseError(f"平台风控拦截了请求（HTTP {response.status_code}）", "blocked")
    return response


def _raise_http(response: requests.Response, what: str) -> None:
    if 200 <= response.status_code < 300:
        return
    if response.status_code in (404, 410):
        raise ParseError(f"{what}不存在或已删除（HTTP {response.status_code}）", "not_found")
    raise ParseError(f"{what}请求失败（HTTP {response.status_code}）", "unknown")


def _expand_short(session: requests.Session, link: DetectedLink) -> DetectedLink:
    if not link.short:
        return link
    current = link.url
    for _ in range(6):
        response = _request(session, current, headers={"User-Agent": UA_IPHONE}, allow_redirects=False)
        location = response.headers.get("Location")
        response.close()
        if not location:
            break
        current = urljoin(current, location)
        detected = detect_link(current)
        if detected.platform != link.platform:
            raise ParseError("短链跳转到了其他平台", "unsupported")
        if not detected.short:
            return detected
    raise ParseError("短链展开后无法识别作品地址", "unsupported")


def parse(
    text: str,
    *,
    cookie: str = "",
    browser: str | None = None,
    timeout: int = 90,
    quality: int | None = None,
) -> ParseResult:
    """Parse one platform URL or a full pasted sharing sentence."""

    session = requests.Session()
    link = _expand_short(session, detect_link(text))
    if link.platform is Platform.DOUYIN:
        return _parse_douyin(session, link, cookie=cookie, timeout=timeout, browser=browser)
    if link.platform is Platform.XHS:
        return _parse_xhs(session, link, cookie=cookie)
    if link.platform is Platform.X:
        return _parse_x(session, link, cookie=cookie)
    return _parse_bilibili(session, link, cookie=cookie, quality=quality)


def _normalise_url(value: str) -> str:
    value = html.unescape(value).replace("\\/", "/").replace("\\u0026", "&")
    if value.startswith("//"):
        return "https:" + value
    return value


def _browser_executable(explicit: str | None) -> str | None:
    candidates = [explicit] if explicit else []
    candidates.extend(("google-chrome", "chromium", "chromium-browser"))
    for candidate in candidates:
        if candidate and shutil.which(candidate):
            return candidate
    return None


def _browser_dump(url: str, *, browser: str | None, timeout: int) -> str:
    executable = _browser_executable(browser)
    if not executable:
        raise ParseError(
            "抖音需要可执行的 Chrome/Chromium 来运行页面挑战，请安装浏览器或用 --browser 指定路径",
            "unsupported",
        )
    profile = tempfile.mkdtemp(prefix="yunx-chrome-")
    command = [
        executable,
        "--headless=new",
        "--no-sandbox",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--no-first-run",
        "--no-default-browser-check",
        "--virtual-time-budget=60000",
        "--run-all-compositor-stages-before-draw",
        f"--user-data-dir={profile}",
        "--dump-dom",
        url,
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            timeout=max(timeout, 60),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ParseError("抖音页面渲染超时", "network") from exc
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    body = completed.stdout.decode("utf-8", "ignore")
    if completed.returncode != 0 or len(body) < 1000:
        detail = completed.stderr.decode("utf-8", "ignore")[-300:].strip()
        raise ParseError(f"抖音页面渲染失败{': ' + detail if detail else ''}", "unknown")
    return body


class _CdpSocket:
    """Small dependency-free WebSocket client for Chrome DevTools Protocol."""

    def __init__(self, websocket_url: str) -> None:
        parts = urlsplit(websocket_url)
        if parts.hostname is None or parts.port is None:
            raise OSError("invalid DevTools websocket URL")
        self.socket = socket.create_connection((parts.hostname, parts.port), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {parts.path or '/'} HTTP/1.1\r\n"
            f"Host: {parts.hostname}:{parts.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii")
        self.socket.sendall(request)
        response = self._read_until(b"\r\n\r\n")
        if b" 101 " not in response.split(b"\r\n", 1)[0]:
            self.close()
            raise OSError("DevTools websocket handshake failed")

    def _read_until(self, marker: bytes) -> bytes:
        data = bytearray()
        while marker not in data:
            chunk = self.socket.recv(4096)
            if not chunk:
                raise OSError("DevTools websocket closed")
            data.extend(chunk)
        return bytes(data)

    def _read_exact(self, size: int) -> bytes:
        data = bytearray()
        while len(data) < size:
            chunk = self.socket.recv(size - len(data))
            if not chunk:
                raise OSError("DevTools websocket closed")
            data.extend(chunk)
        return bytes(data)

    def send(self, value: dict) -> None:
        payload = json.dumps(value, separators=(",", ":")).encode("utf-8")
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        length = len(masked)
        if length < 126:
            header = bytes([0x81, 0x80 | length])
        elif length <= 0xFFFF:
            header = bytes([0x81, 0x80 | 126]) + struct.pack("!H", length)
        else:
            header = bytes([0x81, 0x80 | 127]) + struct.pack("!Q", length)
        self.socket.sendall(header + mask + masked)

    def receive(self, timeout: float = 1.0) -> dict | None:
        self.socket.settimeout(timeout)
        try:
            first = self._read_exact(2)
        except socket.timeout:
            return None
        opcode = first[0] & 0x0F
        masked = bool(first[1] & 0x80)
        length = first[1] & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exact(8))[0]
        mask = self._read_exact(4) if masked else b""
        payload = self._read_exact(length)
        if masked:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        if opcode == 9:
            self._send_control(10, payload)
            return None
        if opcode in (8,):
            return None
        if opcode != 1:
            return None
        return json.loads(payload.decode("utf-8", "ignore"))

    def _send_control(self, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.socket.sendall(bytes([0x80 | opcode, 0x80 | len(masked)]) + mask + masked)

    def close(self) -> None:
        try:
            self.socket.close()
        except Exception:
            pass


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _cookie_pairs(cookie: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for part in cookie.split(";"):
        if "=" not in part:
            continue
        name, value = part.strip().split("=", 1)
        if name and value:
            pairs.append((name, value))
    return pairs


def _browser_capture(
    url: str,
    *,
    cookie: str,
    browser: str | None,
    timeout: int,
) -> tuple[str, dict | None, str]:
    """Render a Douyin page and capture its detail JSON before DOM fallback."""

    executable = _browser_executable(browser)
    if not executable:
        raise ParseError(
            "抖音需要可执行的 Chrome/Chromium 来运行页面挑战，请安装浏览器或用 --browser 指定路径",
            "unsupported",
        )
    profile = tempfile.mkdtemp(prefix="yunx-chrome-cdp-")
    port = _free_port()
    process = subprocess.Popen(
        [
            executable,
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--no-default-browser-check",
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    client: _CdpSocket | None = None
    try:
        endpoint: dict | None = None
        wait_until = time.monotonic() + 12
        while time.monotonic() < wait_until and endpoint is None:
            try:
                with urlopen(f"http://127.0.0.1:{port}/json", timeout=1) as response:
                    targets = json.loads(response.read().decode("utf-8"))
                endpoint = next((target for target in targets if target.get("type") == "page"), None)
            except Exception:
                time.sleep(0.1)
        if not endpoint or not endpoint.get("webSocketDebuggerUrl"):
            raise OSError("Chrome DevTools target did not start")
        client = _CdpSocket(endpoint["webSocketDebuggerUrl"])
        next_id = 0

        def command(method: str, params: dict | None = None) -> int:
            nonlocal next_id
            next_id += 1
            client.send({"id": next_id, "method": method, "params": params or {}})
            return next_id

        command("Network.enable")
        command("Page.enable")
        command("Runtime.enable")
        pairs = _cookie_pairs(cookie)
        if pairs:
            command(
                "Network.setCookies",
                {
                    "cookies": [
                        {"name": name, "value": value, "domain": ".douyin.com", "path": "/"}
                        for name, value in pairs
                    ]
                    + [
                        {"name": name, "value": value, "domain": ".douyinvod.com", "path": "/"}
                        for name, value in pairs
                    ]
                },
            )
        command("Page.navigate", {"url": url})
        detail_request_ids: set[str] = set()
        body_command: dict[int, str] = {}
        detail: dict | None = None
        html_body = ""
        deadline = time.monotonic() + min(max(timeout, 30), 45)
        while time.monotonic() < deadline:
            message = client.receive(1.0)
            if not message:
                continue
            method = message.get("method")
            params = message.get("params") or {}
            if method == "Network.responseReceived":
                response = params.get("response") or {}
                response_url = str(response.get("url") or "")
                if "/aweme/v1/web/aweme/detail" in response_url:
                    detail_request_ids.add(str(params.get("requestId")))
            elif method == "Network.loadingFinished":
                request_id = str(params.get("requestId"))
                if request_id in detail_request_ids:
                    body_id = command("Network.getResponseBody", {"requestId": request_id})
                    body_command[body_id] = request_id
            elif "id" in message and message["id"] in body_command:
                body_result = message.get("result") or {}
                body = body_result.get("body", "")
                if body_result.get("base64Encoded"):
                    try:
                        body = base64.b64decode(body).decode("utf-8", "ignore")
                    except Exception:
                        body = ""
                try:
                    decoded = json.loads(body)
                    detail = decoded.get("aweme_detail") or decoded.get("awemeDetail")
                    if detail:
                        break
                except (TypeError, ValueError):
                    pass
        html_command = command(
            "Runtime.evaluate",
            {"expression": "document.documentElement.outerHTML", "returnByValue": True},
        )
        html_deadline = time.monotonic() + 5
        while time.monotonic() < html_deadline:
            message = client.receive(1.0)
            if message and message.get("id") == html_command:
                html_body = str(((message.get("result") or {}).get("result") or {}).get("value") or "")
                break
        return html_body, detail, cookie
    finally:
        if client:
            client.close()
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                process.kill()
        shutil.rmtree(profile, ignore_errors=True)


def _unique_urls(body: str, domain: str) -> list[str]:
    # dump-dom contains both HTML attributes and escaped JSON. Keep the first URL
    # for each resource path so v11/v26 CDN mirrors do not create duplicates.
    pattern = re.compile(r"https?://[^\"'<>\s]+" + re.escape(domain) + r"[^\"'<>\s]*", re.I)
    seen: set[tuple[str, str]] = set()
    result: list[str] = []
    for raw in pattern.findall(body):
        url = _normalise_url(raw).rstrip("\\\"',);}")
        parts = urlsplit(url)
        path = parts.path
        key = (domain.lower(), path)
        if not path or key in seen:
            continue
        seen.add(key)
        result.append(url)
    return result


def _title_from_html(body: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", body, re.I | re.S)
    title = html.unescape(re.sub(r"\s+", " ", match.group(1))).strip() if match else ""
    return re.sub(r"\s*[-|｜]\s*抖音\s*$", "", title).strip()


def _douyin_image_urls(body: str) -> list[str]:
    urls = _unique_urls(body, "douyinpic.com")
    result: list[str] = []
    seen_assets: set[str] = set()
    for url in urls:
        if "biz_tag=aweme_images" not in url and "tplv-dy-aweme-images" not in url:
            continue
        # The hydrated page places thumbnail, 1440px and original variants
        # of each image in the DOM. They share the source object before the
        # `~tplv-...` transform suffix. Keep the first signed rendition.
        asset_key = urlsplit(url).path.split("~tplv", 1)[0]
        if asset_key in seen_assets:
            continue
        seen_assets.add(asset_key)
        result.append(url)
    return result


def _douyin_video_urls(body: str) -> list[str]:
    urls = _unique_urls(body, "douyinvod.com")
    # CDN mirrors with a higher bt/br are usually the best rendition. The
    # browser page already selected a playable rendition, so this only chooses
    # between duplicate quality entries when a page exposes several.
    return sorted(
        urls,
        key=lambda value: int(parse_qs(urlsplit(value).query).get("bt", ["0"])[0] or 0),
        reverse=True,
    )


def _nested_media_url(value: object) -> str:
    if isinstance(value, str) and value:
        return _normalise_url(value)
    if isinstance(value, dict):
        for key in ("url_list", "urlList", "url", "src"):
            found = _nested_media_url(value.get(key))
            if found:
                return found
    if isinstance(value, list):
        for item in value:
            found = _nested_media_url(item)
            if found:
                return found
    return ""


def _douyin_quality_label(item: dict, play_addr: object, fallback: str) -> str:
    width = int(item.get("width") or (play_addr.get("width") if isinstance(play_addr, dict) else 0) or 0)
    height = int(item.get("height") or (play_addr.get("height") if isinstance(play_addr, dict) else 0) or 0)
    long_edge = max(width, height)
    base = next(
        (
            label
            for edge, label in (
                (3840, "4K"),
                (2560, "2K"),
                (1920, "1080P"),
                (1280, "720P"),
                (854, "540P"),
            )
            if long_edge >= edge
        ),
        fallback,
    )
    if item.get("is_h265") or item.get("is_bytevc1"):
        return f"{base} HEVC"
    return base


def _douyin_best_video(detail: dict) -> tuple[str, str]:
    video = detail.get("video") or {}
    candidates: list[tuple[tuple[int, int, int], str, str]] = []
    bit_rates = video.get("bit_rate") or video.get("bitRate") or []
    if isinstance(bit_rates, list):
        for item in bit_rates:
            if not isinstance(item, dict):
                continue
            url = _nested_media_url(item.get("play_addr") or item.get("playAddr"))
            if not url:
                continue
            url = url.replace("/playwm/", "/play/")
            play_addr = item.get("play_addr") or item.get("playAddr") or {}
            width = int(item.get("width") or (play_addr.get("width") if isinstance(play_addr, dict) else 0) or 0)
            height = int(item.get("height") or (play_addr.get("height") if isinstance(play_addr, dict) else 0) or 0)
            size = int(
                item.get("data_size")
                or item.get("dataSize")
                or item.get("file_size")
                or (play_addr.get("data_size") if isinstance(play_addr, dict) else 0)
                or 0
            )
            bitrate = int(item.get("bit_rate") or item.get("bitRate") or 0)
            label = _douyin_quality_label(
                item,
                play_addr,
                str(item.get("gear_name") or item.get("gearName") or ""),
            )
            candidates.append(((width * height, size, bitrate), url, label))
    fallback = _nested_media_url(video.get("play_addr") or video.get("playAddr"))
    if fallback:
        candidates.append(((0, int(video.get("width") or 0) * int(video.get("height") or 0), 0), fallback.replace("/playwm/", "/play/"), ""))
    if not candidates:
        return "", ""
    _, url, label = max(candidates, key=lambda item: item[0])
    return url, label


def _cover_from_html(body: str) -> str:
    urls = _unique_urls(body, "douyinpic.com")
    return next((u for u in urls if "biz_tag=pcweb_cover" in u), urls[0] if urls else "")


def _image_extension(url: str) -> str:
    path = urlsplit(url).path.lower()
    if ".png" in path:
        return ".png"
    if ".jpeg" in path or ".jpg" in path:
        return ".jpg"
    if ".gif" in path:
        return ".gif"
    return ".webp"


def _douyin_page_detail(body: str, expected_id: str) -> dict | None:
    """Read SSR or React Flight data and select this work's structured object."""
    def find(value: object) -> dict | None:
        if isinstance(value, dict):
            identity = str(value.get("awemeId") or value.get("aweme_id") or "")
            if identity == expected_id and (value.get("images") or value.get("video")):
                return value
            for child in value.values():
                found = find(child)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = find(child)
                if found:
                    return found
        return None

    render = re.search(r'<script[^>]*id="RENDER_DATA"[^>]*>(.*?)</script>', body, re.S)
    if render:
        try:
            found = find(json.loads(unquote(render.group(1))))
            if found:
                return found
        except ValueError:
            pass
    chunks = []
    for match in re.finditer(r'self\.__pace_f\.push\((.*?)\)</script>', body, re.S):
        try:
            value = json.loads(match.group(1))
            if value[0] == 1 and isinstance(value[1], str):
                chunks.append(value[1])
        except (ValueError, IndexError, TypeError):
            continue
    for line in "".join(chunks).splitlines():
        record = re.match(r'[0-9a-f]+:(.*)', line)
        if record:
            try:
                found = find(json.loads(record.group(1)))
                if found:
                    return found
            except ValueError:
                continue
    return None


def _parse_douyin(
    session: requests.Session,
    link: DetectedLink,
    *,
    cookie: str,
    timeout: int,
    browser: str | None,
) -> ParseResult:
    # The share page now returns an acrawler challenge to HTTP clients. The
    # Chromium dump executes that challenge and serialises the hydrated page.
    page_kind = "note" if "/note/" in link.url or "/slides/" in link.url else "video"
    page_url = f"https://www.douyin.com/{page_kind}/{link.id}"
    detail: dict | None = None
    try:
        if page_kind == "video":
            body, detail, _ = _browser_capture(
                page_url,
                cookie=cookie,
                browser=browser,
                timeout=min(timeout, 45),
            )
        else:
            body = _browser_dump(page_url, browser=browser, timeout=timeout)
    except Exception:
        body = _browser_dump(page_url, browser=browser, timeout=timeout)
    page_detail = _douyin_page_detail(body, link.id)
    image_detail = page_detail or detail or {}
    title = str((detail or page_detail or {}).get("desc") or "").strip() or _title_from_html(body) or f"douyin_{link.id}"
    images = _douyin_image_urls(body)
    videos = _douyin_video_urls(body)
    headers = {"User-Agent": UA_CHROME, "Referer": "https://www.douyin.com/"}
    if cookie:
        headers["Cookie"] = cookie
    cover = _cover_from_html(body)
    medias: list[Media] = []

    structured_images = image_detail.get("images") or (image_detail.get("image_post_info") or {}).get("images") or []
    if structured_images:
        for image in structured_images:
            image_url = _nested_media_url(image)
            motion = _nested_media_url((image.get("video") or {}).get("playAddr") or (image.get("video") or {}).get("play_addr"))
            if image_url:
                medias.append(Media(url=image_url, kind=MediaKind.IMAGE, quality="动图" if motion else "原图", headers=headers, motion_url=motion))
        cover = cover or (medias[0].url if medias else "")
    elif images:
        medias = [Media(url=image, kind=MediaKind.IMAGE, quality="平台图片", headers=headers) for image in images]
        cover = cover or images[0]
    elif detail:
        best_url, quality = _douyin_best_video(detail)
        if best_url:
            video = detail.get("video") or {}
            cover = cover or _nested_media_url(video.get("cover") or video.get("origin_cover"))
            medias.append(Media(url=best_url, kind=MediaKind.VIDEO, quality=quality, headers=headers))
    if not medias:
        if videos:
            medias.append(Media(url=videos[0], kind=MediaKind.VIDEO, headers=headers))
    if not medias:
        raise ParseError("抖音页面没有找到可下载的媒体，可能仍被风控拦截", "blocked")

    return ParseResult(
        platform=Platform.DOUYIN,
        title=title,
        url=link.url,
        canonical_url=page_url,
        cover=cover,
        medias=medias,
        notice="图集页面缺少结构化配对数据，动态部分未获取" if images and not structured_images else "",
    )


def _inline_json(body: str, key: str) -> object:
    pattern = re.compile(re.escape(key) + r"\s*=\s*(.*?)\s*</script>", re.I | re.S)
    match = pattern.search(body)
    if not match:
        raise ParseError(f"页面缺少 {key} 数据", "unknown")
    raw = match.group(1).strip()
    raw = raw.rstrip(";").replace("undefined", "null")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        try:
            return json.loads(unquote(raw))
        except json.JSONDecodeError as exc:
            raise ParseError(f"{key} 数据格式错误", "unknown") from exc


def _xhs_headers(cookie: str, *, discovery: bool = False) -> dict[str, str]:
    headers = {
        "User-Agent": UA_IPHONE if discovery else UA_CHROME,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Referer": "https://www.xiaohongshu.com/",
    }
    if discovery:
        headers.update({"Origin": "https://www.xiaohongshu.com", "X-Requested-With": "XMLHttpRequest"})
    if cookie:
        headers["Cookie"] = cookie
    return headers


def _xhs_note_from_state(state: dict, note_id: str) -> tuple[dict, dict]:
    try:
        note = state["note"]["noteDetailMap"][note_id]["note"]
        return note, state
    except (KeyError, TypeError):
        pass
    try:
        root = state["noteData"]
        return root["data"]["noteData"], root
    except (KeyError, TypeError) as exc:
        raise ParseError("小红书页面里没有笔记数据，可能需要登录或内容已删除", "unsupported") from exc


def _xhs_raw_image(item: dict) -> str:
    return str(item.get("urlDefault") or item.get("url") or "")


def _xhs_original_url(raw: str) -> str:
    # XHS's processed URL is host/<bucket>/<hash>/<image-id>!<transform>.
    # The original CDN expects everything after the first two path segments.
    parts = urlsplit(raw).path.lstrip("/").split("/")
    image_id = "/".join(parts[2:]).split("!", 1)[0] if len(parts) > 2 else ""
    if not image_id:
        return raw
    return f"https://sns-img-bd.xhscdn.com/{image_id}"


def _xhs_video(note: dict) -> tuple[str, str]:
    stream = ((note.get("video") or {}).get("media") or {}).get("stream") or {}
    for key, label in (("h265", "H.265"), ("h264", "H.264"), ("av1", "AV1"), ("h266", "H.266")):
        values = stream.get(key) or []
        if values:
            item = values[0] or {}
            url = item.get("masterUrl") or item.get("master_url") or ""
            if url:
                return _normalise_url(url), label
    return "", ""


def _parse_xhs(session: requests.Session, link: DetectedLink, *, cookie: str) -> ParseResult:
    query = urlsplit(link.url).query
    paths = [f"https://www.xiaohongshu.com/explore/{link.id}", f"https://www.xiaohongshu.com/discovery/item/{link.id}"]
    last_error: Exception | None = None
    for base in paths:
        url = base + (f"?{query}" if query else "")
        try:
            response = _request(session, url, headers=_xhs_headers(cookie, discovery="discovery" in base))
            _raise_http(response, "小红书笔记")
            if any(marker in response.url for marker in ("/login", "captcha", "website-login")):
                raise ParseError("小红书要求登录后才能查看笔记，请通过 --cookie 提供登录 Cookie", "unsupported")
            if "/404" in response.url:
                raise ParseError("小红书笔记不存在或已删除", "not_found")
            state = _inline_json(response.text, "window.__INITIAL_STATE__")
            if not isinstance(state, dict):
                raise ParseError("小红书页面数据不是对象", "unknown")
            note, root = _xhs_note_from_state(state, link.id)
            title = str(note.get("title") or link.id)
            headers = _xhs_headers(cookie)
            video, quality = _xhs_video(note)
            medias: list[Media] = []
            notice = ""
            if video:
                medias.append(Media(url=video, kind=MediaKind.VIDEO, quality=quality, headers=headers))
            elif note.get("video") is not None:
                raise ParseError("小红书视频笔记没有取到视频直链", "unknown")
            else:
                raw_images = [_xhs_raw_image(item) for item in (note.get("imageList") or []) if isinstance(item, dict)]
                fallback = 0
                for raw in raw_images:
                    original = _xhs_original_url(raw)
                    try:
                        probe = _request(session, original, headers=headers, timeout=15, stream=True)
                        missing = probe.status_code in (404, 410)
                        probe.close()
                        if missing:
                            raise requests.RequestException("missing")
                        chosen = original
                    except (ParseError, requests.RequestException):
                        chosen = raw
                        fallback += 1
                    medias.append(Media(url=chosen, kind=MediaKind.IMAGE, quality="原图", headers=headers))
                if fallback:
                    notice = f"{fallback} 张图片没有可用的去水印原图，已改用平台地址"
            if not medias:
                raise ParseError("小红书笔记没有可下载媒体", "not_found")
            user = note.get("user") or {}
            preload = (root.get("normalNotePreloadData") or {}).get("imagesList") or []
            cover = _xhs_raw_image(preload[0]) if preload and isinstance(preload[0], dict) else medias[0].url
            return ParseResult(
                platform=Platform.XHS,
                title=title,
                author=str(user.get("nickname") or user.get("nickName") or ""),
                text=str(note.get("desc") or ""),
                cover=cover,
                url=link.url,
                canonical_url=response.url,
                medias=medias,
                notice=notice,
            )
        except ParseError as exc:
            last_error = exc
            if exc.category == "unsupported":
                raise
    raise last_error if isinstance(last_error, ParseError) else ParseError("小红书解析失败")


# X guest request and media normalization adapted from FxEmbed/FxEmbed (MIT),
# commit 2e9097877863050749f7ab555ce0c77a8caa70b4. See vendor/fxembed/UPSTREAM.md.
_X_API_ROOT = "https://api.x.com"
_X_GUEST_BEARER = (
    "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D"
    "1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)
_X_TWEET_QUERY_ID = "f2sagi1jweVHFkTUIHzmMQ"
_X_TWEET_FEATURES = {
    "rweb_video_screen_enabled": False,
    "profile_label_improvements_pcf_label_in_post_enabled": True,
    "responsive_web_profile_redirect_enabled": False,
    "rweb_tipjar_consumption_enabled": False,
    "verified_phone_label_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "premium_content_api_read_enabled": False,
    "communities_web_enable_tweet_community_results_fetch": True,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "responsive_web_grok_analyze_button_fetch_trends_enabled": False,
    "responsive_web_grok_analyze_post_followups_enabled": True,
    "responsive_web_jetfuel_frame": True,
    "responsive_web_grok_share_attachment_enabled": True,
    "responsive_web_grok_annotations_enabled": True,
    "articles_preview_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "content_disclosure_indicator_enabled": True,
    "content_disclosure_ai_generated_indicator_enabled": True,
    "responsive_web_grok_show_grok_translated_post": True,
    "responsive_web_grok_analysis_button_from_backend": True,
    "post_ctas_fetch_enabled": True,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "responsive_web_grok_image_annotation_enabled": True,
    "responsive_web_grok_imagine_annotation_enabled": True,
    "responsive_web_grok_community_note_auto_translation_is_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
    "tweet_awards_web_tipping_enabled": False,
}


def _x_quality_label(width: int, height: int, bitrate: int) -> str:
    edge = max(width, height)
    label = next(
        (name for threshold, name in ((3840, "4K"), (2560, "2K"), (1920, "1080P"), (1280, "720P"), (854, "540P")) if edge >= threshold),
        f"{edge}P" if edge else "X 视频",
    )
    return f"{label} · {bitrate // 1000} kbps" if bitrate else label


def _x_video_variant(item: dict) -> tuple[str, str, int, int, int]:
    """Select the highest bitrate MP4; never use a thumbnail or HLS playlist."""
    candidates = []
    for variant in item.get("variants") or []:
        if not isinstance(variant, dict):
            continue
        url = _normalise_url(str(variant.get("url") or ""))
        content_type = str(variant.get("content_type") or "").lower()
        path = urlsplit(url).path.lower()
        if ".m3u8" in path or (content_type != "video/mp4" and not path.endswith(".mp4")):
            continue
        if not url or (content_type and content_type != "video/mp4"):
            continue
        bitrate = int(variant.get("bitrate") or 0)
        match = re.search(r"/(\d+)x(\d+)/", url)
        width = int(match.group(1)) if match else int(variant.get("width") or item.get("width") or 0)
        height = int(match.group(2)) if match else int(variant.get("height") or item.get("height") or 0)
        candidates.append(((bitrate, width * height), url, width, height, bitrate))
    if not candidates:
        return "", "", 0, 0, 0
    _, url, width, height, bitrate = max(candidates, key=lambda entry: entry[0])
    return url, _x_quality_label(width, height, bitrate), width, height, bitrate


def _x_json(response: requests.Response, what: str) -> dict:
    if response.status_code == 401:
        raise ParseError("X 会话已失效或需要登录，请检查服务端 X_COOKIE", "unsupported")
    _raise_http(response, what)
    try:
        payload = response.json()
    except ValueError as exc:
        raise ParseError(f"{what}返回的不是 JSON", "unknown") from exc
    if not isinstance(payload, dict):
        raise ParseError(f"{what}返回的数据格式无效", "unknown")
    errors = payload.get("errors") or []
    if errors:
        codes = {error.get("code") for error in errors if isinstance(error, dict)}
        category = "blocked" if 88 in codes else "unsupported" if codes & {32, 89, 215} else "not_found" if 144 in codes else "unknown"
        # Upstream messages can contain session details; keep the error stable.
        raise ParseError("X 拒绝了当前会话的解析请求", category)
    return payload


def _x_graphql_payload(session: requests.Session, tweet_id: str, *, cookie: str = "") -> dict:
    """Call X directly in Python, using FxEmbed's token and GraphQL flow."""
    headers = {
        "Authorization": _X_GUEST_BEARER,
        "User-Agent": UA_CHROME,
        "Accept": "*/*",
        "Content-Type": "application/json",
        "Origin": "https://x.com",
        "Referer": "https://x.com/home",
        "x-twitter-active-user": "yes",
        "x-twitter-client-language": "en",
    }
    cookie_values = dict(_cookie_pairs(cookie))
    if cookie_values.get("auth_token"):
        if not cookie_values.get("ct0"):
            raise ParseError("X_COOKIE 需要同时包含 auth_token 和 ct0", "unsupported")
        headers.update({
            "Cookie": cookie,
            "x-csrf-token": cookie_values["ct0"],
            "x-twitter-auth-type": "OAuth2Session",
        })
    else:
        try:
            activate = session.post(
                f"{_X_API_ROOT}/1.1/guest/activate.json",
                headers=headers, data="", timeout=30, allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise ParseError("X guest 会话请求失败", "network") from exc
        if activate.status_code in (403, 418, 429):
            raise ParseError(f"平台风控拦截了请求（HTTP {activate.status_code}）", "blocked")
        guest_token = str(_x_json(activate, "X guest 会话").get("guest_token") or "")
        if not guest_token:
            raise ParseError("X guest 会话未返回 token", "blocked")
        csrf = uuid.uuid4().hex
        headers.update({
            "Cookie": "; ".join((
                f"guest_id_ads=v1%3A{guest_token}", f"guest_id_marketing=v1%3A{guest_token}",
                f"guest_id=v1%3A{guest_token}", f"ct0={csrf}",
            )),
            "x-csrf-token": csrf,
            "x-guest-token": guest_token,
        })
    response = _request(
        session,
        f"{_X_API_ROOT}/graphql/{_X_TWEET_QUERY_ID}/TweetResultByRestId",
        params={
            "variables": json.dumps({
                "tweetId": tweet_id, "withCommunity": False,
                "includePromotedContent": False, "withVoice": False,
            }, separators=(",", ":")),
            "features": json.dumps(_X_TWEET_FEATURES, separators=(",", ":")),
            "fieldToggles": json.dumps({
                "withArticleRichContentState": True, "withArticlePlainText": False,
                "withGrokAnalyze": False, "withDisallowedReplyControls": False,
            }, separators=(",", ":")),
        },
        headers=headers, timeout=30, allow_redirects=False,
    )
    return _x_json(response, "X GraphQL")


def _parse_x_graphql_payload(payload: dict, link: DetectedLink) -> ParseResult:
    """Read the requested tweet's media, preserving order and original photos."""
    result = ((payload.get("data") or {}).get("tweetResult") or {}).get("result")
    # Visibility wrappers and reposts carry the original Tweet as an inner node.
    for _ in range(4):
        if not isinstance(result, dict):
            break
        if isinstance(result.get("tweet"), dict):
            result = result["tweet"]
            continue
        legacy = result.get("legacy") or {}
        repost = legacy.get("retweeted_status_result") or legacy.get("retweeted_status_results") or {}
        if isinstance(repost.get("result"), dict):
            result = repost["result"]
            continue
        break
    if not isinstance(result, dict):
        raise ParseError("X 作品不存在或当前会话不可见", "not_found")
    if result.get("__typename") in ("TweetUnavailable", "TweetTombstone"):
        # A tombstone does not prove deletion: X also uses it for guest restrictions.
        raise ParseError("X 作品对当前会话不可见，可能需要登录", "access_restricted")
    legacy = result.get("legacy")
    if not isinstance(legacy, dict):
        raise ParseError("X 未返回有效的作品数据", "not_found")
    media_items = ((legacy.get("extended_entities") or {}).get("media")
                   or (legacy.get("entities") or {}).get("media") or [])
    headers = {"User-Agent": UA_CHROME, "Referer": "https://x.com/"}
    medias: list[Media] = []
    cover = ""
    for item in media_items:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "").lower()
        preview = _normalise_url(str(item.get("media_url_https") or ""))
        if not cover:
            cover = preview
        if kind == "photo" and preview:
            parsed = urlsplit(preview)
            params = parse_qs(parsed.query, keep_blank_values=True)
            params["name"] = ["orig"]
            original_url = urlunsplit(parsed._replace(
                path=re.sub(r":\w+$", "", parsed.path), query=urlencode(params, doseq=True),
            ))
            medias.append(Media(url=original_url, kind=MediaKind.IMAGE, quality="原图", headers=headers))
        elif kind in ("video", "animated_gif"):
            original = item.get("original_info") or {}
            url, quality, _, _, _ = _x_video_variant({
                "variants": (item.get("video_info") or {}).get("variants") or [],
                "width": original.get("width"), "height": original.get("height"),
            })
            if not url:
                raise ParseError("X 视频没有可下载的 MP4 格式", "unsupported")
            medias.append(Media(url=url, kind=MediaKind.VIDEO, quality=quality, headers=headers))
    if not medias:
        raise ParseError("X 作品没有可下载的图片或视频", "not_found")
    core = result.get("core") or {}
    user = (core.get("user_results") or core.get("user_result") or {}).get("result") or {}
    user_core, user_legacy = user.get("core") or {}, user.get("legacy") or {}
    screen_name = str(user_core.get("screen_name") or user_legacy.get("screen_name") or "i")
    note = ((result.get("note_tweet") or {}).get("note_tweet_results") or {}).get("result") or {}
    text = str(note.get("text") or legacy.get("full_text") or "")
    for item in media_items:
        if isinstance(item, dict) and item.get("url"):
            text = text.replace(str(item["url"]), "")
    entities = note.get("entity_set") or legacy.get("entities") or {}
    for entity in entities.get("urls") or []:
        if entity.get("url") and entity.get("expanded_url"):
            text = text.replace(str(entity["url"]), str(entity["expanded_url"]))
    text = html.unescape(text).strip() or f"x_{link.id}"
    return ParseResult(
        platform=Platform.X, title=text, text=text, url=link.url, cover=cover,
        author=str(user_core.get("name") or user_legacy.get("name") or screen_name),
        canonical_url=f"https://x.com/{screen_name}/status/{result.get('rest_id') or link.id}",
        medias=medias,
    )


def _parse_x(session: requests.Session, link: DetectedLink, *, cookie: str = "") -> ParseResult:
    try:
        return _parse_x_graphql_payload(_x_graphql_payload(session, link.id, cookie=cookie), link)
    except ParseError as error:
        logging.getLogger(__name__).info("X built-in parsing failed (%s); using public API", error.category)
        return _parse_x_public(session, link)


def _parse_x_public(session: requests.Session, link: DetectedLink) -> ParseResult:
    """Use the free FxTwitter endpoint once, without forwarding X credentials."""
    response = _request(session, f"https://api.fxtwitter.com/status/{link.id}",
                        headers={"User-Agent": "YunX-Media-API/0.1", "Accept": "application/json"}, timeout=30)
    payload = _x_json(response, "X 备用接口")
    tweet = payload.get("tweet")
    if payload.get("code") not in (None, 200) or not isinstance(tweet, dict):
        category = "not_found" if payload.get("code") in (404, 144) else "unsupported"
        raise ParseError("X 备用接口未返回可下载的作品", category)
    block = tweet.get("media") or {}
    entries = block.get("all") or list(block.get("photos") or []) + list(block.get("videos") or [])
    media_items = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "mosaic_photo":
            continue
        media = {"type": "animated_gif" if kind == "gif" else kind,
                 "media_url_https": item.get("url") if kind == "photo" else item.get("thumbnail_url"),
                 "original_info": {"width": item.get("width"), "height": item.get("height")}}
        if kind in ("video", "gif"):
            variants = list(item.get("formats") or item.get("variants") or [])
            # Some public responses expose only the chosen MP4 URL.
            if urlsplit(str(item.get("url") or "")).path.lower().endswith(".mp4"):
                variants.append({"url": item["url"], "content_type": "video/mp4"})
            media["video_info"] = {"variants": variants}
        media_items.append(media)
    result = _parse_x_graphql_payload({"data": {"tweetResult": {"result": {
        "__typename": "Tweet", "rest_id": str(tweet.get("id") or link.id),
        "core": {"user_results": {"result": {"core": tweet.get("author") or {}}}},
        "legacy": {"full_text": str(tweet.get("text") or ""), "extended_entities": {"media": media_items}},
    }}}}, link)
    result._x_public_used = True
    return result


def _x_public_retry(result: ParseResult) -> ParseResult:
    """Refresh all media via the public endpoint after an internal download fails."""
    with requests.Session() as session:
        return _parse_x_public(session, detect_link(result.url))


def _bili_headers(cookie: str) -> dict[str, str]:
    headers = {"User-Agent": UA_CHROME, "Referer": "https://www.bilibili.com/"}
    if cookie:
        headers["Cookie"] = cookie
    return headers


def _json_get(session: requests.Session, url: str, params: Mapping[str, object], headers: Mapping[str, str], what: str) -> dict:
    response = _request(session, url, params=params, headers=headers)
    _raise_http(response, what)
    try:
        data = response.json()
    except ValueError as exc:
        raise ParseError(f"{what}返回的不是 JSON", "unknown") from exc
    if data.get("code", 0) != 0:
        code = data.get("code")
        category = "not_found" if code in (-404, -400) else "unsupported"
        raise ParseError(f"{what}失败：{data.get('message', code)}", category)
    return data


def _quality_label(qn: int) -> str:
    return {6: "240P", 7: "240P", 16: "360P", 32: "480P", 64: "720P", 74: "720P60", 80: "1080P", 112: "1080P+", 116: "1080P60", 120: "4K"}.get(qn, f"画质 {qn}" if qn else "")


def _https_url(url: str) -> str:
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("http://"):
        return "https://" + url.removeprefix("http://")
    return url


def _dash_url(stream: Mapping[str, object]) -> str:
    values: list[str] = []
    for key in ("baseUrl", "base_url"):
        value = stream.get(key)
        if isinstance(value, str) and value:
            values.append(_https_url(value))
    for key in ("backupUrl", "backup_url"):
        backup = stream.get(key)
        if isinstance(backup, list):
            values.extend(_https_url(str(item)) for item in backup if item)
    # bilivideo's mcdn nodes are valid but frequently rate limited. Prefer an
    # official upos mirror when the API provided one.
    return next((url for url in values if "mcdn" not in url.lower()), values[0] if values else "")


def _dash_media(
    session: requests.Session,
    bvid: str,
    cid: int,
    headers: Mapping[str, str],
    requested_quality: int | None,
) -> tuple[Media, int] | None:
    params = {"bvid": bvid, "cid": cid, "qn": requested_quality or 120, "fnval": 4048, "fnver": 0, "fourk": 1}
    try:
        payload = _json_get(session, "https://api.bilibili.com/x/player/playurl", params, headers, "B站取 DASH 直链")
    except ParseError:
        return None
    data = payload.get("data") or {}
    dash = data.get("dash") or {}
    videos = dash.get("video") or []
    audios = dash.get("audio") or []
    if not videos or not audios:
        return None
    avc = [item for item in videos if int(item.get("codecid") or 0) == 7]
    pool = avc or videos
    if requested_quality:
        exact = [item for item in pool if int(item.get("id") or 0) == requested_quality]
        lower = [item for item in pool if int(item.get("id") or 0) <= requested_quality]
        chosen_video = (exact or sorted(lower, key=lambda item: int(item.get("id") or 0), reverse=True) or pool)[0]
    else:
        chosen_video = max(pool, key=lambda item: int(item.get("id") or 0))
    chosen_audio = max(audios, key=lambda item: int(item.get("id") or 0))
    video_url = _dash_url(chosen_video)
    audio_url = _dash_url(chosen_audio)
    if not video_url or not audio_url:
        return None
    quality = int(chosen_video.get("id") or 0)
    return (
        Media(
            url=video_url,
            kind=MediaKind.VIDEO,
            quality=_quality_label(quality),
            headers=dict(headers),
            audio_url=audio_url,
            audio_headers=dict(headers),
        ),
        quality,
    )


def _parse_bilibili(session: requests.Session, link: DetectedLink, *, cookie: str, quality: int | None = None) -> ParseResult:
    headers = _bili_headers(cookie)
    view_params = {"bvid": link.id} if link.id.upper().startswith("BV") else {"aid": link.id.lower().removeprefix("av")}
    view = _json_get(session, "https://api.bilibili.com/x/web-interface/view", view_params, headers, "B站视频信息")
    data = view.get("data") or {}
    bvid = data.get("bvid") or link.id
    pages = data.get("pages") or []
    cid = (pages[0] if pages else {}).get("cid") or data.get("cid")
    if not cid:
        raise ParseError("B站视频信息缺少 cid", "unknown")
    dash = _dash_media(session, bvid, int(cid), headers, quality) if cookie else None
    if dash:
        media, _ = dash
        return ParseResult(
            platform=Platform.BILIBILI,
            title=str(data.get("title") or bvid),
            author=str((data.get("owner") or {}).get("name") or ""),
            text=str(data.get("desc") or ""),
            cover=_https_url(str(data.get("pic") or "")),
            url=f"https://www.bilibili.com/video/{bvid}",
            canonical_url=f"https://www.bilibili.com/video/{bvid}",
            medias=[media],
        )
    play_params = {"bvid": bvid, "cid": cid, "qn": quality or 80, "fnval": 1, "fnver": 0, "fourk": 1, "platform": "html5", "high_quality": 1}
    play = _json_get(session, "https://api.bilibili.com/x/player/playurl", play_params, headers, "B站取直链")
    play_data = play.get("data") or {}
    durl = play_data.get("durl") or []
    if not durl or not durl[0].get("url"):
        raise ParseError("B站未返回可下载直链，可能需要登录或属于番剧/付费内容", "unsupported")
    first = durl[0]
    media = Media(
        url=_https_url(first["url"]),
        kind=MediaKind.VIDEO,
        quality=_quality_label(int(play_data.get("quality") or 0)),
        size=int(first.get("size") or -1),
        headers=headers,
    )
    return ParseResult(
        platform=Platform.BILIBILI,
        title=str(data.get("title") or bvid),
        author=str((data.get("owner") or {}).get("name") or ""),
        text=str(data.get("desc") or ""),
        cover=_https_url(str(data.get("pic") or "")),
        url=f"https://www.bilibili.com/video/{bvid}",
        canonical_url=f"https://www.bilibili.com/video/{bvid}",
        medias=[media],
    )


def _safe_name(value: str, fallback: str) -> str:
    value = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "_", value).strip(" .")
    return (value or fallback)[:120]


def _extension(media: Media) -> str:
    if media.audio_url:
        return ".mp4"
    if media.motion_url:
        return ".jpg"
    suffix = Path(urlsplit(media.url).path).suffix.lower()
    allowed = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".mov", ".m4s", ".ts", ".mp3", ".m4a", ".aac"}
    return suffix if suffix in allowed else (".jpg" if media.kind is MediaKind.IMAGE else ".mp4")


def _download_file(session: requests.Session, url: str, destination: Path, headers: Mapping[str, str], *, timeout: int) -> int:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".part")
    try:
        with _request(session, url, headers=headers, timeout=timeout, stream=True) as response:
            _raise_http(response, "媒体下载")
            with temporary.open("wb") as output:
                for chunk in response.iter_content(1024 * 256):
                    if chunk:
                        output.write(chunk)
        temporary.replace(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return destination.stat().st_size


def _jpeg_still(source: Path, destination: Path) -> Path | None:
    if source.read_bytes()[:2] == b"\xff\xd8":
        return source
    try:
        from PIL import Image

        image = Image.open(source).convert("RGB")
        converted = destination.with_name(destination.stem + ".still.jpg")
        image.save(converted, format="JPEG", quality=95)
        return converted
    except Exception:
        return None


def _oppo_exif(width: int, height: int) -> bytes:
    value = bytearray.fromhex(_OPPO_EXIF_HEX)
    if width > 0 and height > 0:
        value[28:32] = int(width).to_bytes(4, "big")
        value[40:44] = int(height).to_bytes(4, "big")
    return bytes(value)


def _motion_xmp(video_length: int) -> bytes:
    xml = f'''<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:GCamera="http://ns.google.com/photos/1.0/camera/"
    xmlns:OpCamera="http://ns.oplus.com/photos/1.0/camera/"
    xmlns:Container="http://ns.google.com/photos/1.0/container/"
    xmlns:Item="http://ns.google.com/photos/1.0/container/item/"
   GCamera:MotionPhoto="1"
   GCamera:MotionPhotoVersion="1"
   GCamera:MotionPhotoPresentationTimestampUs="0"
   OpCamera:MotionPhotoPrimaryPresentationTimestampUs="0"
   OpCamera:MotionPhotoOwner="oplus"
   OpCamera:OLivePhotoVersion="2"
   OpCamera:VideoLength="{video_length}">
   <Container:Directory>
    <rdf:Seq>
     <rdf:li rdf:parseType="Resource">
      <Container:Item Item:Mime="image/jpeg" Item:Semantic="Primary" Item:Length="0" Item:Padding="0"/>
     </rdf:li>
     <rdf:li rdf:parseType="Resource">
      <Container:Item Item:Mime="video/mp4" Item:Semantic="MotionPhoto" Item:Length="{video_length}"/>
     </rdf:li>
    </rdf:Seq>
   </Container:Directory>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>'''
    return xml.encode("utf-8")


def _app1_xmp(xmp: bytes) -> bytes:
    identifier = b"http://ns.adobe.com/xap/1.0/\x00"
    payload = identifier + xmp
    length = len(payload) + 2
    return b"\xff\xe1" + length.to_bytes(2, "big") + payload


def _write_motion_photo(still: Path, motion: Path, output: Path) -> bool:
    jpeg = _jpeg_still(still, output)
    if jpeg is None:
        return False
    # Google Motion Photo readers locate the appended MP4 from the XMP item
    # length. Keep the metadata small and avoid re-encoding either resource.
    video_length = motion.stat().st_size
    try:
        from PIL import Image

        with Image.open(jpeg) as image:
            width, height = image.size
    except Exception:
        width, height = 0, 0
    xmp_segment = _app1_xmp(_motion_xmp(video_length))
    exif_segment = _oppo_exif(width, height)
    temporary_output = output.with_name(output.name + ".motion.tmp")
    with temporary_output.open("wb") as target, jpeg.open("rb") as source:
        head = source.read(2)
        if head != b"\xff\xd8":
            temporary_output.unlink(missing_ok=True)
            return False
        target.write(head)
        # Keep a JFIF APP0 immediately after SOI. Some Android galleries only
        # recognise the Motion Photo XMP when it follows the complete APP0
        # segment, matching the writer used by YunX-Desktop.
        probe = source.read(4)
        source.seek(2)
        if probe[:2] == b"\xff\xe0" and len(probe) == 4:
            app0_length = int.from_bytes(probe[2:4], "big")
            app0 = source.read(2 + app0_length)
            target.write(app0)
        target.write(xmp_segment)
        target.write(exif_segment)
        shutil.copyfileobj(source, target)
        with motion.open("rb") as video:
            shutil.copyfileobj(video, target)
    temporary_output.replace(output)
    if jpeg != still:
        jpeg.unlink(missing_ok=True)
    return output.stat().st_size > video_length


def save_result(result: ParseResult, directory: str | os.PathLike[str], *, cookie: str = "", timeout: int = 120,
                x_fallback: bool = True) -> list[Path]:
    """Download media, retrying X through its public parser at most once."""
    try:
        return _save_result(result, directory, cookie=cookie, timeout=timeout)
    except (ParseError, requests.RequestException, subprocess.SubprocessError):
        if result.platform is not Platform.X or result._x_public_used or not x_fallback:
            raise
        refreshed = _x_public_retry(result)
        refreshed.title = result.title
        return _save_result(refreshed, directory, timeout=timeout)


def _save_result(result: ParseResult, directory: str | os.PathLike[str], *, cookie: str = "", timeout: int = 120) -> list[Path]:
    """Download all parsed media and return the resulting paths."""

    target_dir = Path(directory)
    target_dir.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    base = _safe_name(result.title, result.platform.value)
    output: list[Path] = []
    total = len(result.medias)
    for index, media in enumerate(result.medias, start=1):
        order = f"_{index}" if total > 1 else ""
        path = target_dir / f"{base}{order}{_extension(media)}"
        if media.audio_url:
            video_path = path.with_name(path.name + ".video.m4s")
            audio_path = path.with_name(path.name + ".audio.m4s")
            _download_file(session, media.url, video_path, media.headers, timeout=timeout)
            _download_file(session, media.audio_url, audio_path, media.audio_headers or media.headers, timeout=timeout)
            ffmpeg = shutil.which("ffmpeg")
            if not ffmpeg:
                # A video-only DASH track is still a valid downloaded stream
                # and is preferable to discarding data when ffmpeg is absent.
                video_path.replace(path.with_suffix(".m4s"))
                path = path.with_suffix(".m4s")
            else:
                try:
                    completed = subprocess.run(
                        [ffmpeg, "-y", "-loglevel", "error", "-i", str(video_path), "-i", str(audio_path), "-c", "copy", str(path)],
                        capture_output=True,
                        timeout=timeout,
                        check=False,
                    )
                    if completed.returncode != 0 or not path.exists():
                        detail = completed.stderr.decode("utf-8", "ignore").strip()
                        raise ParseError(f"B站音视频合轨失败{': ' + detail if detail else ''}", "unknown")
                finally:
                    video_path.unlink(missing_ok=True)
                    audio_path.unlink(missing_ok=True)
        else:
            _download_file(session, media.url, path, media.headers, timeout=timeout)
        if media.motion_url:
            motion_path = path.with_name(path.stem + ".motion.mp4")
            _download_file(session, media.motion_url, motion_path, media.headers, timeout=timeout)
            if not _write_motion_photo(path, motion_path, path):
                # Preserve both files if a platform returns a non-decodable still.
                path = path.with_suffix(_extension(media))
            # Keep the motion stream beside the Motion Photo container. This
            # gives desktop viewers and users without Motion Photo support a
            # directly playable copy of the animated half.
            output.append(motion_path)
        output.append(path)
    return output


__all__ = [
    "DetectedLink",
    "Media",
    "MediaKind",
    "ParseError",
    "ParseResult",
    "Platform",
    "detect_link",
    "parse",
    "save_result",
]
