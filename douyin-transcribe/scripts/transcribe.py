#!/usr/bin/env python3
"""
抖音一键采集：视频转文字稿，图文存图 + 原文。

用法：
    python transcribe.py "https://v.douyin.com/xxxxx"
    python transcribe.py "https://www.douyin.com/video/1234567890" ./output
    python transcribe.py "https://www.douyin.com/note/1234567890" ./output
"""

import os
import shutil
import sys
import tempfile
import urllib.request

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = SKILL_ROOT if os.path.isdir(os.path.join(SKILL_ROOT, "chubby_common")) else os.path.dirname(SKILL_ROOT)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from chubby_common import funasr, markdown

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from download_douyin_audio import MOBILE_UA, capture


def clean_title(title: str) -> str:
    """清理标题：去掉重复的《》后缀。"""
    clean = title.split("《")[0] if "《" in title and title.count("《") > 1 else title
    if clean.count("《") > 1:
        first_end = clean.find("》")
        if first_end > 0:
            clean = clean[: first_end + 1]
    return clean


def transcribe(audio_path: str, output_path: str, title: str, source: str = ""):
    """Transcribe audio using SenseVoice-Small and save as Markdown."""
    clean = clean_title(title)
    text, elapsed = funasr.transcribe(audio_path, language="zh")

    body = markdown.note_markdown(
        clean,
        f"> 转录引擎：SenseVoice-Small | 耗时：{elapsed:.0f}秒\n\n{text}",
        {
            "type": "note",
            "platform": "douyin",
            "tags": ["抖音"],
            "source": source,
            "author": "",
            "transcriber": "SenseVoice-Small",
        },
    )

    # Save to file
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(body)

    return elapsed, len(text)


def _download_images(urls, output_dir: str, base: str) -> list:
    """把图片存到 <base>.assets/，返回引用列表；单张失败时保留原链接。"""
    refs = []
    asset_dir = os.path.join(output_dir, f"{base}.assets")
    for index, url in enumerate(urls, 1):
        try:
            os.makedirs(asset_dir, exist_ok=True)
            request = urllib.request.Request(
                url,
                headers={"User-Agent": MOBILE_UA, "Referer": "https://www.douyin.com/"},
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                blob = response.read()
                content_type = response.headers.get("Content-Type", "")
            suffix = ".webp" if "webp" in content_type else ".png" if "png" in content_type else ".jpg"
            name = f"img_{index}{suffix}"
            with open(os.path.join(asset_dir, name), "wb") as handle:
                handle.write(blob)
            print(f"  🖼️  图片 {index}/{len(urls)}（{len(blob) // 1024} KB）", file=sys.stderr)
            refs.append(f"{base}.assets/{name}")
        except Exception as exc:
            print(f"  ⚠️  图片 {index} 下载失败，保留链接：{exc}", file=sys.stderr)
            refs.append(url)
    return refs


def write_note(capture_result: dict, source: str, output_dir: str) -> str:
    """图文笔记：原文 + 本地图片，按统一 frontmatter 输出。"""
    title = capture_result["title"]
    base = markdown.sanitize_filename(title, "抖音图文")
    refs = _download_images(capture_result["images"], output_dir, base)

    body = [capture_result["desc"].strip() or "（正文为空）"]
    if refs:
        body += ["", "## 图片", ""]
        body += [f"![]({ref})" if not ref.startswith("http") else f"- {ref}" for ref in refs]

    text = markdown.note_markdown(
        title,
        "\n".join(body),
        {
            "type": "note",
            "platform": "douyin",
            "note_type": "image",
            "tags": ["抖音", "图文"],
            "source": source,
            "author": capture_result["author"],
        },
        created=capture_result.get("created"),
    )

    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, f"{base}.md")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(text)
    return output_path


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <douyin_url> [output_dir]")
        print("\nExample:")
        print(f'  {sys.argv[0]} "https://v.douyin.com/xxxxx"')
        print(f'  {sys.argv[0]} "https://v.douyin.com/xxxxx" ./output')
        sys.exit(1)

    url = sys.argv[1]
    output_dir = sys.argv[2] if len(sys.argv) > 2 else "."

    # Step 1: CDP 抓 aweme detail，视频抽音轨，图文取正文和图片
    print("=" * 50, file=sys.stderr)
    print("Step 1: Fetching...", file=sys.stderr)
    print("=" * 50, file=sys.stderr)

    tmpdir = None
    try:
        tmpdir = tempfile.mkdtemp(prefix="dyt-")
        result = capture(url, tmpdir)

        if result["kind"] == "note":
            output_path = write_note(result, url, output_dir)
            print("\n" + "=" * 50, file=sys.stderr)
            print("✅ Done!", file=sys.stderr)
            print("=" * 50, file=sys.stderr)
            print(f"  Title: {result['title']}", file=sys.stderr)
            print(f"  Images: {len(result['images'])}", file=sys.stderr)
            print(f"  Output: {output_path}", file=sys.stderr)
            print(output_path)
            return

        # Step 2: Transcribe
        print("\n" + "=" * 50, file=sys.stderr)
        print("Step 2: Transcribing...", file=sys.stderr)
        print("=" * 50, file=sys.stderr)

        title = result["title"]
        safe_title = markdown.sanitize_filename(title, "抖音视频")
        output_path = os.path.join(output_dir, f"{safe_title}.md")

        elapsed, chars = transcribe(result["audio"], output_path, title, url)

        print("\n" + "=" * 50, file=sys.stderr)
        print("✅ Done!", file=sys.stderr)
        print("=" * 50, file=sys.stderr)
        print(f"  Title: {title}", file=sys.stderr)
        print(f"  Time: {elapsed:.0f}s", file=sys.stderr)
        print(f"  Chars: {chars}", file=sys.stderr)
        print(f"  Output: {output_path}", file=sys.stderr)

        # Print output path to stdout for scripting
        print(output_path)

    except (RuntimeError, ValueError) as e:
        print(f"\n❌ {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        # Cleanup
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    main()
