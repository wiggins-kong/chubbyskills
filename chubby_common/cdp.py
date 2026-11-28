"""Desktop Chromium CDP 核心：浏览器发现、Profile 锁、登录态流转与页面抓取。

仅在建立 WebSocket 连接时延迟 ``import websocket``，确保 ``python -I`` 零依赖导入成功。
启动 Chromium 时继承系统/环境变量代理（不传 ``--noproxy``）。
"""

import base64
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

from chubby_common import deps

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

BROWSERS = {
    "win32": [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
    ],
    "darwin": [
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ],
}.get(
    sys.platform,
    [
        "microsoft-edge",
        "microsoft-edge-stable",
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
    ],
)

_PROFILE_MARKERS = (
    ("Default", "Network", "Cookies"),
    ("Default", "Cookies"),
    ("Cookies",),
    ("Local State",),
    ("Default", "Preferences"),
)

__all__ = [
    "UA",
    "BROWSERS",
    "browser_bin",
    "profile_dir",
    "has_profile_session",
    "CDPSession",
    "ensure_session",
    "fetch_page_state",
    "format_cookies_netscape",
    "export_cookies_txt",
    "download_with_cookies",
]


def browser_bin() -> str:
    """定位本机 Chromium 系浏览器；BROWSER_BIN / EDGE_BIN 优先。"""
    for name in ("BROWSER_BIN", "EDGE_BIN"):
        env = os.environ.get(name, "").strip()
        if not env:
            continue
        found = env if os.path.isabs(env) else shutil.which(env)
        if not (found and os.path.exists(found)):
            raise RuntimeError(f"{name} 指向的浏览器不存在：{env}")
        return found
    for candidate in BROWSERS:
        expanded = os.path.expandvars(candidate)
        found = expanded if os.path.isabs(expanded) else shutil.which(expanded)
        if found and os.path.exists(found):
            return found
    raise RuntimeError(
        "找不到 Edge/Chrome/Chromium（需要 109+）。用 BROWSER_BIN 指定可执行文件，"
        "或运行 scripts/bootstrap.py 查看安装命令。"
    )


def _validate_site(site: str) -> str:
    if not isinstance(site, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", site):
        raise ValueError(f"非法的站点 profile 名称：{site!r}")
    return site


def profile_dir(site: str) -> str:
    """返回指定站点的持久化 Chromium user-data-dir 路径。"""
    site = _validate_site(site)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA", "").strip() or os.path.join(
            os.path.expanduser("~"), "AppData", "Local"
        )
    elif sys.platform == "darwin":
        base = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME", "").strip() or os.path.join(
            os.path.expanduser("~"), ".local", "share"
        )
    return os.path.join(base, "chubbyskills", "cdp-profiles", site)


def has_profile_session(site: str) -> bool:
    """判断指定站点是否已有落盘的持久化浏览器状态。"""
    root = profile_dir(site)
    if not os.path.isdir(root):
        return False
    return any(os.path.isfile(os.path.join(root, *parts)) for parts in _PROFILE_MARKERS)


class _ProfileLock:
    """基于文件锁保护同一站点的持久化 user-data-dir，避免并发损坏。"""

    def __init__(self, target_dir: str, timeout: float = 5.0):
        parent = os.path.dirname(target_dir)
        os.makedirs(parent, exist_ok=True)
        self.lock_path = os.path.join(parent, f"{os.path.basename(target_dir)}.lock")
        self._fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        if sys.platform == "win32" and os.fstat(self._fd).st_size == 0:
            os.write(self._fd, b"0")
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                self._try_lock()
                return
            except OSError:
                if time.monotonic() >= deadline:
                    os.close(self._fd)
                    self._fd = None
                    raise RuntimeError(
                        f"站点 profile 正在被另一个 CDP 进程占用：{self.lock_path}"
                    )
                time.sleep(0.1)

    def _try_lock(self) -> None:
        if sys.platform == "win32":
            import msvcrt

            os.lseek(self._fd, 0, os.SEEK_SET)
            msvcrt.locking(self._fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                os.lseek(self._fd, 0, os.SEEK_SET)
                msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            try:
                os.close(self._fd)
            finally:
                self._fd = None


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _wait_target(port: int, proc=None, timeout: int = 30) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            raise RuntimeError(f"浏览器启动后立即退出（exit={proc.poll()}）")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=3) as resp:
                tabs = json.load(resp)
            for tab in tabs:
                if tab.get("type") == "page" and tab.get("webSocketDebuggerUrl"):
                    return tab
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError("浏览器调试端口没起来：确认 Edge/Chrome/Chromium 是 109+ 且没有被安全策略拦住")


class CDPSession:
    """单浏览器实例的 CDP 会话，支持临时或持久化 profile 及无头/有头切换。"""

    def __init__(
        self,
        site: str | None = None,
        *,
        ephemeral: bool = False,
        headless: bool = True,
        lock_timeout: float = 5.0,
        browser: str | None = None,
        target_timeout: int = 30,
    ):
        if not ephemeral and not site:
            raise ValueError("持久化 CDP 会话必须提供 site")
        self.site = _validate_site(site) if site else None
        self.ephemeral = ephemeral
        self.headless = headless
        self.browser = browser
        self.target_timeout = target_timeout
        self.process = None
        self.ws = None
        self._seq = 0
        self._events = []
        self._lock = None

        if ephemeral:
            prefix = f"cdp-{self.site}-" if self.site else "cdp-"
            self.user_data_dir = tempfile.mkdtemp(prefix=prefix)
        else:
            path = profile_dir(self.site)
            self._lock = _ProfileLock(path, timeout=lock_timeout)
            os.makedirs(path, exist_ok=True)
            self.user_data_dir = path

    def __enter__(self) -> "CDPSession":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def is_alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self, headless: bool | None = None) -> "CDPSession":
        """启动（或按新模式重启）Chromium 并连接 CDP WebSocket。"""
        if headless is not None:
            requested = headless
        else:
            requested = self.headless

        if self.is_alive() and self.ws is not None:
            if requested == self.headless:
                return self
            self.stop_browser()

        deps.check_python_module(
            "websocket",
            "python -m pip install websocket-client（或运行 scripts/bootstrap.py --install）",
        )
        import websocket

        self.headless = requested
        exe = self.browser or browser_bin()
        port = _free_port()
        cmd = [exe]
        if self.headless:
            cmd.extend(["--headless=new", "--disable-gpu"])
        # 不传 --noproxy/--no-proxy-server，继承系统或环境代理
        cmd.extend(
            [
                "--mute-audio",
                "--no-first-run",
                "--no-default-browser-check",
                f"--remote-debugging-port={port}",
                f"--user-data-dir={self.user_data_dir}",
                "about:blank",
            ]
        )
        self.process = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            target = _wait_target(port, proc=self.process, timeout=self.target_timeout)
            self.ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=20)
            self._events.clear()
            self.call("Network.enable")
            self.call("Page.enable")
        except Exception:
            self.stop_browser()
            raise
        return self

    def stop_browser(self) -> None:
        """发送 Browser.close 并等待进程退出，确保 cookie 落盘且释放 profile 锁。"""
        if self.ws is not None:
            try:
                self.call("Browser.close", timeout=5)
            except Exception:
                pass
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None
        if self.process is not None:
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
            self.process = None
        self._events.clear()

    def close(self) -> None:
        """关闭浏览器、释放持久化锁或清理临时 profile。"""
        try:
            self.stop_browser()
        finally:
            if self._lock is not None:
                self._lock.release()
                self._lock = None
            if self.ephemeral and self.user_data_dir:
                shutil.rmtree(self.user_data_dir, ignore_errors=True)

    def call(self, method: str, params: dict | None = None, timeout: float = 20.0) -> dict:
        if self.ws is None:
            raise RuntimeError("CDP 会话尚未启动")
        self._seq += 1
        req_id = self._seq
        self.ws.settimeout(timeout)
        self.ws.send(json.dumps({"id": req_id, "method": method, "params": params or {}}))
        while True:
            message = json.loads(self.ws.recv())
            if message.get("id") == req_id:
                return message
            self._events.append(message)

    def navigate(self, url: str) -> dict:
        return self.call("Page.navigate", {"url": url})

    def eval_js(self, expression: str, await_promise: bool = True):
        res = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": await_promise,
            },
        )
        if "error" in res:
            raise RuntimeError(f"CDP 执行失败：{res['error']}")
        result = res.get("result") or {}
        if "exceptionDetails" in result:
            detail = result["exceptionDetails"].get("text") or "JS execution error"
            raise RuntimeError(f"页面 JS 异常：{detail}")
        return (result.get("result") or {}).get("value")

    def html(self) -> str:
        return self.eval_js(
            "document.documentElement ? document.documentElement.outerHTML : ''",
            await_promise=False,
        ) or ""

    def cookies(self, urls: list[str] | tuple[str, ...] | None = None) -> list[dict]:
        if urls:
            res = self.call("Network.getCookies", {"urls": list(urls)})
        else:
            res = self.call("Network.getAllCookies")
            if "error" in res or "cookies" not in (res.get("result") or {}):
                res = self.call("Network.getCookies")
        return (res.get("result") or {}).get("cookies") or []

    def response_body(self, request_id: str) -> str:
        res = self.call("Network.getResponseBody", {"requestId": request_id})
        result = res.get("result") or {}
        body = result.get("body") or ""
        if body and result.get("base64Encoded"):
            return base64.b64decode(body).decode("utf-8", errors="replace")
        return body

    def _next_event(self, timeout: float = 5.0) -> dict | None:
        if self._events:
            return self._events.pop(0)
        if self.ws is None:
            return None
        self.ws.settimeout(timeout)
        try:
            return json.loads(self.ws.recv())
        except Exception as exc:
            if isinstance(exc, (TimeoutError, socket.timeout)) or exc.__class__.__name__.endswith(
                "TimeoutException"
            ):
                return None
            raise

    def capture_xhr(
        self,
        url_match: str | None = None,
        *,
        timeout: float = 30.0,
        predicate=None,
        stop_on_match: bool = True,
    ) -> list[dict]:
        """监听并捕获匹配的网络响应体。"""
        matches = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process is not None and self.process.poll() is not None:
                break
            remaining = max(0.05, min(5.0, deadline - time.monotonic()))
            try:
                message = self._next_event(timeout=remaining)
            except Exception:
                if self.process is not None and self.process.poll() is not None:
                    break
                raise
            if not message or message.get("method") != "Network.responseReceived":
                continue
            params = message.get("params") or {}
            resp = params.get("response") or {}
            resp_url = resp.get("url") or ""
            if url_match and url_match not in resp_url:
                continue
            body = self.response_body(params["requestId"])
            entry = {"url": resp_url, "status": resp.get("status"), "body": body}
            if predicate is None or predicate(entry):
                matches.append(entry)
                if stop_on_match:
                    return matches
        return matches


def _gate_passed(
    session: CDPSession,
    url: str,
    *,
    gate=None,
    auth_cookies=(),
    blocked_selectors=(),
    require_login: bool = False,
) -> bool:
    if blocked_selectors:
        found = session.eval_js(
            f"({json.dumps(list(blocked_selectors))}).some(s => document.querySelector(s) !== null)",
            await_promise=False,
        )
        if found:
            return False

    if auth_cookies:
        present = {c.get("name") for c in session.cookies() if c.get("value")}
        if not all(name in present for name in auth_cookies):
            return False
    elif require_login and gate is None:
        if not any(c.get("value") for c in session.cookies([url])):
            return False

    if gate is not None and not gate(session):
        return False
    return True


def ensure_session(
    url: str,
    site: str | None = None,
    *,
    session: CDPSession | None = None,
    login_url: str | None = None,
    gate=None,
    auth_cookies=(),
    blocked_selectors=(),
    require_login: bool = False,
    timeout: int = 180,
    poll_interval: float = 1.0,
    settle_seconds: float = 1.0,
    ephemeral: bool = False,
    lock_timeout: float = 5.0,
) -> CDPSession:
    """先无头检查门禁；未通过时关闭无头再以同 profile 切有头等待登录或验证。"""
    owns_session = session is None
    if session is None:
        session = CDPSession(
            site=site,
            ephemeral=ephemeral,
            headless=True,
            lock_timeout=lock_timeout,
        )

    try:
        if not session.is_alive() or session.ws is None:
            session.start(headless=True)
        session.navigate(url)
        if settle_seconds > 0:
            time.sleep(settle_seconds)

        if _gate_passed(
            session,
            url,
            gate=gate,
            auth_cookies=auth_cookies,
            blocked_selectors=blocked_selectors,
            require_login=require_login,
        ):
            return session

        # 关闭无头实例释放目录锁，再在同一个 user-data-dir 上启动有头窗口
        session.stop_browser()
        session.start(headless=False)
        session.navigate(login_url or url)

        deadline = time.monotonic() + timeout
        while True:
            if not session.is_alive():
                raise RuntimeError("浏览器窗口已关闭，登录或验证未完成")
            try:
                if _gate_passed(
                    session,
                    url,
                    gate=gate,
                    auth_cookies=auth_cookies,
                    blocked_selectors=blocked_selectors,
                    require_login=require_login,
                ):
                    return session
            except Exception:
                if not session.is_alive():
                    raise RuntimeError("浏览器窗口已关闭，登录或验证未完成")
            if time.monotonic() >= deadline:
                raise RuntimeError(f"等待登录或验证超时（{timeout} 秒）")
            time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))
    except Exception:
        if owns_session:
            session.close()
        else:
            session.stop_browser()
        raise


def fetch_page_state(
    url: str,
    *,
    site: str | None = None,
    session: CDPSession | None = None,
    ephemeral: bool = False,
    js: str | None = None,
    xhr_match: str | None = None,
    xhr_predicate=None,
    stop_on_xhr: bool = True,
    wait_seconds: float = 10.0,
    settle_seconds: float = 1.0,
    gate=None,
    auth_cookies=(),
    blocked_selectors=(),
    require_login: bool = False,
    login_url: str | None = None,
    timeout: int = 180,
    close_session: bool | None = None,
) -> dict:
    """导航到目标页，提取渲染后 HTML、JS 结果、匹配 XHR 响应与 cookies。"""
    owns_session = session is None
    if close_session is None:
        close_session = owns_session

    has_gate = bool(gate or auth_cookies or blocked_selectors or require_login)
    if session is None or has_gate:
        session = ensure_session(
            url,
            site=site,
            session=session,
            login_url=login_url,
            gate=gate,
            auth_cookies=auth_cookies,
            blocked_selectors=blocked_selectors,
            require_login=require_login,
            timeout=timeout,
            settle_seconds=settle_seconds,
            ephemeral=ephemeral,
        )
    elif not session.is_alive() or session.ws is None:
        session.start(headless=session.headless)

    try:
        session._events.clear()
        session.navigate(url)
        if xhr_match is not None or xhr_predicate is not None:
            xhr = session.capture_xhr(
                xhr_match,
                timeout=wait_seconds,
                predicate=xhr_predicate,
                stop_on_match=stop_on_xhr,
            )
        else:
            xhr = []
            if settle_seconds > 0:
                time.sleep(settle_seconds)

        rendered_html = session.html()
        js_value = session.eval_js(js) if js is not None else None
        cookie_list = session.cookies()
        return {
            "url": url,
            "html": rendered_html,
            "js": js_value,
            "xhr": xhr,
            "cookies": cookie_list,
        }
    finally:
        if close_session:
            session.close()


def format_cookies_netscape(cookies: list[dict]) -> str:
    """将 CDP cookie 列表格式化为 Netscape cookies.txt 文本。"""
    lines = [
        "# Netscape HTTP Cookie File",
        "# https://curl.se/docs/http-cookies.html",
        "# This file was generated by chubby_common.cdp",
    ]
    for cookie in cookies:
        domain = str(cookie.get("domain") or "").strip()
        name = str(cookie.get("name") or "")
        value = str(cookie.get("value") or "")
        if not domain or not name or "\n" in name or "\r" in name or "\n" in value or "\r" in value:
            continue
        include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"
        path = str(cookie.get("path") or "/")
        secure = "TRUE" if cookie.get("secure") else "FALSE"
        raw_expires = cookie.get("expires")
        try:
            expires = max(0, int(float(raw_expires))) if raw_expires is not None else 0
        except (TypeError, ValueError):
            expires = 0
        lines.append("\t".join((domain, include_subdomains, path, secure, str(expires), name, value)))
    return "\n".join(lines) + "\n"


def export_cookies_txt(
    source,
    path: str | None = None,
    urls: list[str] | tuple[str, ...] | None = None,
) -> str:
    """导出 CDP cookies 为 Netscape cookies.txt 文件，返回文件路径。"""
    if isinstance(source, CDPSession):
        cookies = source.cookies(urls=urls)
    elif isinstance(source, str):
        with CDPSession(site=source, headless=True) as session:
            session.start(headless=True)
            cookies = session.cookies(urls=urls)
    else:
        cookies = list(source)

    content = format_cookies_netscape(cookies)
    if path is None:
        handle = tempfile.NamedTemporaryFile(
            mode="w", prefix="cdp-cookies-", suffix=".txt", encoding="utf-8", delete=False
        )
        with handle:
            handle.write(content)
        target = handle.name
    else:
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
        target = path
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    return target


def _cookie_matches_url(cookie: dict, url: str) -> bool:
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").lower()
    req_path = parsed.path or "/"
    c_domain = str(cookie.get("domain") or "").lower()
    if not host or not c_domain:
        return False
    if c_domain.startswith("."):
        bare = c_domain[1:]
        if host != bare and not host.endswith(c_domain):
            return False
    elif host != c_domain:
        return False
    c_path = cookie.get("path") or "/"
    if not req_path.startswith(c_path):
        return False
    if cookie.get("secure") and parsed.scheme.lower() != "https":
        return False
    return True


def download_with_cookies(
    url: str,
    dest: str,
    *,
    cookies=None,
    referer: str = "",
    user_agent: str = UA,
    headers: dict | None = None,
    timeout: int = 60,
) -> str:
    """携带匹配域名的 CDP cookies 与 Referer 下载资源到指定路径。"""
    if isinstance(cookies, CDPSession):
        cookie_list = cookies.cookies()
    elif cookies:
        cookie_list = list(cookies)
    else:
        cookie_list = []

    req_headers = {"User-Agent": user_agent}
    if referer:
        req_headers["Referer"] = referer
    if headers:
        req_headers.update(headers)
    matched = [
        f"{c['name']}={c.get('value', '')}"
        for c in cookie_list
        if c.get("name") and _cookie_matches_url(c, url)
    ]
    if matched and "Cookie" not in req_headers:
        req_headers["Cookie"] = "; ".join(matched)

    parent = os.path.dirname(os.path.abspath(dest))
    if parent:
        os.makedirs(parent, exist_ok=True)
    request = urllib.request.Request(url, headers=req_headers)
    with urllib.request.urlopen(request, timeout=timeout) as response, open(dest, "wb") as out:
        shutil.copyfileobj(response, out)
    return dest
