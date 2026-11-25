#!/usr/bin/env python3
"""抖音抓取环境自检与部署：新机器上先跑这个。

用法：
    python bootstrap.py                    # 检查，缺什么就打印安装命令
    python bootstrap.py --install          # 安装 websocket-client
    python bootstrap.py --probe "<链接>"   # 用真实链接抓一次，确认能跑通

抓取需要 Python 3.9+、websocket-client 和 Edge/Chrome/Chromium 109+；
视频转文字另需 ffmpeg 与 funasr（体积较大，只存图文不需要）。
退出码：0 表示抓取就绪，1 表示还缺东西。
"""

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = SKILL_ROOT if os.path.isdir(os.path.join(SKILL_ROOT, "chubby_common")) else os.path.dirname(SKILL_ROOT)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import douyin_cdp


def _has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def browser_hint() -> str:
    if sys.platform == "win32":
        return "安装：winget install --id Microsoft.Edge（或 Google.Chrome）"
    if sys.platform == "darwin":
        return "安装：brew install --cask google-chrome（或 microsoft-edge）"
    return "安装：sudo apt install chromium（或 dnf / pacman 的 chromium 包）"


def collect() -> list:
    """逐项检查，返回 [{label, ok, hint, capture}]；capture 表示抓取必需。"""
    version = ".".join(str(part) for part in sys.version_info[:3])
    try:
        browser = douyin_cdp.browser_bin()
        browser_hint_text = ""
    except RuntimeError as exc:
        browser = ""
        browser_hint_text = str(exc)

    return [
        {"label": f"Python {version}", "ok": sys.version_info >= (3, 9),
         "hint": "需要 Python 3.9+", "capture": True},
        {"label": "websocket-client", "ok": _has_module("websocket"),
         "hint": "安装：python -m pip install websocket-client", "capture": True},
        {"label": f"浏览器：{browser}" if browser else "浏览器（Edge/Chrome/Chromium 109+）",
         "ok": bool(browser),
         "hint": f"{browser_hint_text}；{browser_hint()}", "capture": True},
        {"label": "ffmpeg（从视频抽音轨）", "ok": shutil.which("ffmpeg") is not None,
         "hint": "安装：macOS brew install ffmpeg | Ubuntu sudo apt install ffmpeg | Windows winget install Gyan.FFmpeg",
         "capture": True},
        {"label": "funasr（视频转文字）", "ok": _has_module("funasr"),
         "hint": "安装：python -m pip install funasr modelscope torch torchaudio（体积较大；只存图文不需要）",
         "capture": False},
    ]


def install_websocket() -> int:
    if _has_module("websocket"):
        print("websocket-client 已安装", file=sys.stderr)
        return 0
    commands = [[sys.executable, "-m", "pip", "install", "websocket-client"]]
    uv = shutil.which("uv")
    if uv:
        commands.append([uv, "pip", "install", "--python", sys.executable, "websocket-client"])
    for command in commands:
        print("▶ " + " ".join(command), file=sys.stderr)
        if subprocess.run(command).returncode == 0 and _has_module("websocket"):
            return 0
    print("❌ 安装失败：请手动运行 python -m pip install websocket-client", file=sys.stderr)
    return 1


def probe(source: str) -> int:
    try:
        video_id = douyin_cdp.aweme_id(source)
        data = douyin_cdp.fetch_detail(video_id)
    except (RuntimeError, ValueError) as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1
    item = data.get("aweme_detail") or {}
    images = item.get("images") or []
    video = item.get("video") or {}
    author = (item.get("author") or {}).get("nickname") or "未知"
    if images:
        print(f"✅ 抓到图文：{len(images)} 张图 | 作者 {author} | aweme {video_id}")
    else:
        seconds = (video.get("duration") or 0) / 1000
        print(f"✅ 抓到视频：{seconds:.0f} 秒 | 作者 {author} | aweme {video_id}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="抖音抓取环境自检与部署")
    parser.add_argument("--install", action="store_true", help="安装 websocket-client")
    parser.add_argument("--probe", metavar="SOURCE", help="用真实链接抓一次，确认跑通")
    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    if args.probe:
        return probe(args.probe)

    if args.install and install_websocket() != 0:
        return 1

    checks = collect()
    print("抖音抓取环境自检")
    for item in checks:
        print(f"  {'✅' if item['ok'] else '⚠️ '} {item['label']}")
        if not item["ok"]:
            print(f"      {item['hint']}")

    capture_ready = all(item["ok"] for item in checks if item["capture"])
    if capture_ready:
        print('\n抓取已就绪。下一步：python scripts/bootstrap.py --probe "<真实链接>"')
    else:
        print("\n抓取未就绪：先按上面的命令补齐。")
    if not _has_module("funasr"):
        print("视频转文字还缺 funasr；只存图文不受影响。")
    return 0 if capture_ready else 1


if __name__ == "__main__":
    sys.exit(main())
