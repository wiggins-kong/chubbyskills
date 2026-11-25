#!/usr/bin/env python3
"""抖音抓取：CDP 取 aweme detail，视频抽音轨，图文返回图片与原文。

抖音分享页现在只返回反爬空壳，所以这里不再尝试解析页面，也不回退 yt-dlp。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = SKILL_ROOT if os.path.isdir(os.path.join(SKILL_ROOT, "chubby_common")) else os.path.dirname(SKILL_ROOT)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import douyin_cdp
from chubby_common import deps

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) EdgiOS/121.0.2277.107 "
    "Version/17.0 Mobile/15E148 Safari/604.1"
)


def _play_url(item: dict) -> str:
    """优先用 play_addr；为空时退到清晰度列表。playwm → play 去水印。"""
    video = item.get("video") or {}
    urls = (video.get("play_addr") or {}).get("url_list") or []
    if not urls:
        for rate in video.get("bit_rate") or []:
            urls = (rate.get("play_addr") or {}).get("url_list") or []
            if urls:
                break
    if not urls:
        raise RuntimeError("aweme detail 里没有可下载地址：链接可能已失效或内容受限")
    return urls[0].replace("playwm", "play")


def _download_video(video_id: str, url: str, output_dir: str) -> str:
    video_path = os.path.join(output_dir, f"{video_id}.mp4")
    subprocess.run(
        ["curl", "-s", "-L", "-o", video_path,
         "-H", f"User-Agent: {MOBILE_UA}",
         "-H", "Referer: https://www.douyin.com/", url],
        timeout=300, check=True,
    )
    print(f"  Video: {os.path.getsize(video_path) / (1024 * 1024):.1f} MB", file=sys.stderr)

    audio_path = os.path.join(output_dir, f"{video_id}.mp3")
    print("Extracting audio...", file=sys.stderr)
    subprocess.run(
        ["ffmpeg", "-y", "-i", video_path,
         "-vn", "-acodec", "libmp3lame", "-ab", "128k", audio_path],
        capture_output=True, timeout=60, check=True,
    )
    os.remove(video_path)
    print(f"  Audio: {os.path.getsize(audio_path) / (1024 * 1024):.1f} MB", file=sys.stderr)
    return audio_path


def _note_capture(item: dict) -> dict:
    desc = item.get("desc") or ""
    title = next((line.strip() for line in desc.splitlines() if line.strip()), "抖音图文")
    images = []
    for image in item.get("images") or []:
        urls = image.get("url_list") or []
        if urls:
            images.append(urls[0])
    created = None
    if item.get("create_time"):
        created = datetime.fromtimestamp(int(item["create_time"])).strftime("%Y-%m-%d")
    return {
        "kind": "note",
        "title": title,
        "desc": desc,
        "author": (item.get("author") or {}).get("nickname") or "",
        "created": created,
        "images": images,
    }


def capture(source: str, output_dir: str = None) -> dict:
    """抖音链接 / aweme_id / 分享文案 → 抓取结果。

    {"kind": "video", "title", "audio"} 或
    {"kind": "note", "title", "desc", "author", "created", "images"}。
    """
    if output_dir is None:
        output_dir = tempfile.mkdtemp(prefix="dyt-")
    video_id = douyin_cdp.aweme_id(source)
    detail = douyin_cdp.fetch_detail(video_id)
    item = detail.get("aweme_detail") or {}
    if not item:
        raise RuntimeError("aweme detail 响应里没有内容，链接可能已失效")

    if item.get("images"):
        return _note_capture(item)

    deps.ensure_ffmpeg()
    title = item.get("desc") or "抖音视频"
    print(f"Downloading: {title[:60]}...", file=sys.stderr)
    audio_path = _download_video(video_id, _play_url(item), output_dir)
    return {"kind": "video", "title": title, "audio": audio_path}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <抖音链接 / aweme_id / 分享文案>")
        sys.exit(1)

    tmpdir = tempfile.mkdtemp(prefix="dyt-")
    try:
        result = capture(sys.argv[1], tmpdir)
        summary = {key: value for key, value in result.items() if key != "desc"}
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    except (RuntimeError, ValueError) as exc:
        print(f"❌ {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
