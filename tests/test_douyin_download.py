import importlib.util
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "douyin-transcribe" / "scripts" / "download_douyin_audio.py"
SPEC = importlib.util.spec_from_file_location("douyin_download", SCRIPT)
douyin_download = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = douyin_download
SPEC.loader.exec_module(douyin_download)

TRANSCRIBE_SCRIPT = ROOT / "douyin-transcribe" / "scripts" / "transcribe.py"
TRANSCRIBE_SPEC = importlib.util.spec_from_file_location("douyin_transcribe", TRANSCRIBE_SCRIPT)
transcribe = importlib.util.module_from_spec(TRANSCRIBE_SPEC)
sys.modules[TRANSCRIBE_SPEC.name] = transcribe
TRANSCRIBE_SPEC.loader.exec_module(transcribe)

AWEME_ID = "7709518080586596729"


def _detail(**overrides):
    item = {
        "desc": "正常标题",
        "author": {"nickname": "作者"},
        "create_time": 1795012057,
        "video": {"play_addr": {"url_list": ["https://v.example/video.mp4"]}},
    }
    item.update(overrides)
    return {"aweme_detail": item}


class CaptureTest(unittest.TestCase):
    def test_video_capture_downloads_and_extracts_audio(self):
        def fake_run(command, **kwargs):
            if "-o" in command:  # curl 视频
                Path(command[command.index("-o") + 1]).write_bytes(b"v")
            elif command[0] == "ffmpeg":
                Path(command[-1]).write_bytes(b"a")
            return subprocess.CompletedProcess(command, 0)

        with tempfile.TemporaryDirectory() as tmp, patch.object(
            douyin_download.douyin_cdp, "fetch_detail", return_value=_detail()
        ), patch.object(douyin_download.subprocess, "run", side_effect=fake_run), patch.object(
            douyin_download.deps, "ensure_ffmpeg"
        ) as ffmpeg:
            result = douyin_download.capture(AWEME_ID, tmp)

        self.assertEqual(result["kind"], "video")
        self.assertEqual(result["title"], "正常标题")
        self.assertTrue(result["audio"].endswith(".mp3"))
        ffmpeg.assert_called_once()

    def test_note_capture_returns_images_without_media_work(self):
        images = [{"url_list": [f"https://img.example/{i}.webp"]} for i in range(1, 10)]
        detail = _detail(desc="五羊邨\n第二行", images=images, video={})
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            douyin_download.douyin_cdp, "fetch_detail", return_value=detail
        ), patch.object(douyin_download.subprocess, "run") as run, patch.object(
            douyin_download.deps, "ensure_ffmpeg"
        ) as ffmpeg:
            result = douyin_download.capture(AWEME_ID, tmp)

        self.assertEqual(result["kind"], "note")
        self.assertEqual(result["title"], "五羊邨")
        self.assertEqual(len(result["images"]), 9)
        self.assertEqual(
            result["created"], datetime.fromtimestamp(1795012057).strftime("%Y-%m-%d")
        )
        run.assert_not_called()
        ffmpeg.assert_not_called()

    def test_missing_play_url_is_readable(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            douyin_download.douyin_cdp, "fetch_detail", return_value=_detail(video={})
        ), patch.object(douyin_download.deps, "ensure_ffmpeg"):
            with self.assertRaises(RuntimeError) as caught:
                douyin_download.capture(AWEME_ID, tmp)
        self.assertIn("没有可下载地址", str(caught.exception))


class NoteMarkdownTest(unittest.TestCase):
    def test_write_note_embeds_local_images(self):
        class Response:
            headers = {"Content-Type": "image/webp"}

            def read(self):
                return b"image"

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        note = {
            "kind": "note",
            "title": "五羊邨最爱的一人食",
            "desc": "正文",
            "author": "bbbiubiu",
            "created": "2026-11-18",
            "images": ["https://img.example/1.webp"],
        }
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            transcribe.urllib.request, "urlopen", return_value=Response()
        ):
            path = transcribe.write_note(note, "https://v.douyin.com/x/", tmp)
            text = Path(path).read_text(encoding="utf-8")
            asset = Path(tmp) / "五羊邨最爱的一人食.assets" / "img_1.webp"
            self.assertTrue(asset.is_file())

        self.assertIn("五羊邨最爱的一人食.assets/img_1.webp", text)
        self.assertIn('author: "bbbiubiu"', text)
        self.assertIn('note_type: "image"', text)


if __name__ == "__main__":
    unittest.main()
