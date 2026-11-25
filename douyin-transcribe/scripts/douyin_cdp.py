#!/usr/bin/env python3
"""抖音 aweme detail 抓取：独立 profile 的无头 Chromium + CDP。

与正在运行的浏览器并存，不读 cookie、不关浏览器。用法：
    python douyin_cdp.py "<链接 / aweme_id / 整段分享文案>" [-o 输出目录]

浏览器不在默认路径时，用 BROWSER_BIN（兼容旧名 EDGE_BIN）指定可执行文件。
抖音分享页现在只返回反爬空壳，前端仍会请求 aweme/detail，这里监听的就是它。
"""

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = SKILL_ROOT if os.path.isdir(os.path.join(SKILL_ROOT, "chubby_common")) else os.path.dirname(SKILL_ROOT)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from chubby_common import deps

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

BROWSERS = {
    "win32": [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ],
    "darwin": [
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ],
}.get(sys.platform, [
    "microsoft-edge",
    "microsoft-edge-stable",
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
])


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
        found = candidate if os.path.isabs(candidate) else shutil.which(candidate)
        if found and os.path.exists(found):
            return found
    raise RuntimeError(
        "找不到 Edge/Chrome/Chromium（需要 109+）。用 BROWSER_BIN 指定可执行文件，"
        "或运行 scripts/bootstrap.py 查看安装命令。"
    )


def _effective_url(url: str) -> str:
    """跟随短链跳转，返回最终地址。HEAD 被拒时退回 GET。"""
    for method in ("HEAD", "GET"):
        try:
            request = urllib.request.Request(url, method=method, headers={"User-Agent": UA})
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.geturl()
        except Exception:
            continue
    return ""


def aweme_id(text: str) -> str:
    """从链接、aweme_id 或整段分享文案里取作品 ID。"""
    match = re.search(r"https?://[^\s]+", str(text))
    value = (match.group(0) if match else str(text)).strip().rstrip(",，。！？;；)】》\"'")
    if re.fullmatch(r"\d{15,25}", value):
        return value
    match = re.search(r"/(?:video|note|slides)/(\d{15,25})", value)
    if match:
        return match.group(1)
    final = _effective_url(value)
    match = re.search(r"/(?:video|note|slides)/(\d{15,25})", final)
    if not match:
        raise ValueError(f"取不到 aweme_id，最终地址是：{(final or value)[:120]}")
    return match.group(1)


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _wait_target(port: int, timeout: int = 30) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=3))
            for tab in tabs:
                if tab.get("type") == "page" and tab.get("webSocketDebuggerUrl"):
                    return tab
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError("浏览器调试端口没起来：确认 Edge/Chrome/Chromium 是 109+ 且没有被安全策略拦住")


def fetch_detail(video_id: str, attempts=(45, 30)) -> dict:
    """打开分享页，返回前端收回的 aweme detail 原始响应（含 aweme_detail）。"""
    deps.check_python_module(
        "websocket",
        "python -m pip install websocket-client（或运行 scripts/bootstrap.py --install）",
    )
    import websocket

    exe = browser_bin()
    port = _free_port()
    profile = tempfile.mkdtemp(prefix="dyprof-")
    process = subprocess.Popen(
        [exe, "--headless=new", "--disable-gpu", "--mute-audio", "--no-first-run",
         f"--remote-debugging-port={port}", f"--user-data-dir={profile}", "about:blank"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    connection = None
    try:
        connection = websocket.create_connection(_wait_target(port)["webSocketDebuggerUrl"], timeout=20)
        sequence = [0]

        def call(method, params=None):
            sequence[0] += 1
            connection.settimeout(20)
            connection.send(json.dumps({"id": sequence[0], "method": method, "params": params or {}}))
            while True:
                message = json.loads(connection.recv())
                if message.get("id") == sequence[0]:
                    return message

        call("Network.enable")
        call("Page.enable")

        def capture(seconds):
            deadline = time.time() + seconds
            connection.settimeout(5)
            while time.time() < deadline:
                try:
                    message = json.loads(connection.recv())
                except websocket.WebSocketTimeoutException:
                    continue
                if message.get("method") != "Network.responseReceived":
                    continue
                # shortcut: 依赖分享页前端继续请求 aweme/detail；抖音改接口时这里要跟着换
                if "aweme/detail" not in message["params"]["response"]["url"]:
                    continue
                body = call("Network.getResponseBody", {"requestId": message["params"]["requestId"]})
                text = body.get("result", {}).get("body", "")
                if text:
                    data = json.loads(text)
                    if data.get("aweme_detail"):
                        return data
            return None

        share = f"https://www.iesdouyin.com/share/video/{video_id}/"
        for seconds in attempts:  # 抖音偶尔限流，第二次用同一 profile 重试
            call("Page.navigate", {"url": share})
            detail = capture(seconds)
            if detail:
                return detail
            time.sleep(8)
        raise RuntimeError(
            "没抓到 aweme/detail 响应：先确认链接有效、网络能访问抖音，或隔几分钟再试。"
            "环境检查用 scripts/bootstrap.py。"
        )
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        shutil.rmtree(profile, ignore_errors=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="抖音 aweme detail 抓取（CDP）")
    parser.add_argument("source", help="链接、aweme_id 或整段分享文案")
    parser.add_argument("-o", "--output", help="JSON 输出目录（默认临时目录）")
    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):  # Windows 控制台默认 GBK，中文摘要会炸掉输出
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    try:
        video_id = aweme_id(args.source)
        data = fetch_detail(video_id)
    except (RuntimeError, ValueError) as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1

    item = data["aweme_detail"]
    outdir = os.path.expanduser(args.output) if args.output else tempfile.gettempdir()
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, f"douyin_{video_id}.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False)

    video = item.get("video") or {}
    summary = {
        "json": path,
        "aweme_id": item.get("aweme_id"),
        "type": "图文" if item.get("images") else "视频",
        "images": len(item.get("images") or []),
        "duration_ms": video.get("duration"),
        "size": [video.get("width"), video.get("height")],
        "author": (item.get("author") or {}).get("nickname"),
        "create_time": item.get("create_time"),
        "desc": (item.get("desc") or "")[:60],
    }
    print(json.dumps(summary, ensure_ascii=False), file=sys.stderr)
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
