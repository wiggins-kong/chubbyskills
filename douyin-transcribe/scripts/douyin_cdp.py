#!/usr/bin/env python3
"""抖音 aweme detail 抓取：独立临时 profile 的无头 Chromium + CDP。

与正在运行的浏览器并存，不读 cookie、不关浏览器。用法：
    python douyin_cdp.py "<链接 / aweme_id / 整段分享文案>" [-o 输出目录]

浏览器不在默认路径时，用 BROWSER_BIN（兼容旧名 EDGE_BIN）指定可执行文件。
抖音分享页现在只返回反爬空壳，前端仍会请求 aweme/detail，这里监听的就是它。
"""

import argparse
import json
import os
import re
import sys
import tempfile
import time
import urllib.request

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = SKILL_ROOT if os.path.isdir(os.path.join(SKILL_ROOT, "chubby_common")) else os.path.dirname(SKILL_ROOT)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from chubby_common import cdp

UA = cdp.UA
browser_bin = cdp.browser_bin


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


def _extract_aweme_detail(entry: dict):
    text = entry.get("body") or ""
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if data.get("aweme_detail") else None


def fetch_detail(video_id: str, attempts=(45, 30)) -> dict:
    """打开分享页，返回前端收回的 aweme detail 原始响应（含 aweme_detail）。"""
    share = f"https://www.iesdouyin.com/share/video/{video_id}/"
    with cdp.CDPSession(site="douyin", ephemeral=True, headless=True) as session:
        session.start(headless=True)
        for index, seconds in enumerate(attempts):  # 抖音偶尔限流，第二次用同一 profile 重试
            session._events.clear()
            session.navigate(share)
            captured = session.capture_xhr(
                # shortcut: 依赖分享页前端继续请求 aweme/detail；抖音改接口时这里要跟着换
                "aweme/detail",
                timeout=seconds,
                predicate=lambda entry: _extract_aweme_detail(entry) is not None,
                stop_on_match=True,
            )
            if captured:
                return json.loads(captured[0]["body"])
            if index + 1 < len(attempts):
                time.sleep(8)
    raise RuntimeError(
        "没抓到 aweme/detail 响应：先确认链接有效、网络能访问抖音，或隔几分钟再试。"
        "环境检查用 scripts/bootstrap.py。"
    )


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
