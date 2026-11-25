import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "douyin-transcribe" / "scripts" / "douyin_cdp.py"
SPEC = importlib.util.spec_from_file_location("douyin_cdp", SCRIPT)
douyin_cdp = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = douyin_cdp
SPEC.loader.exec_module(douyin_cdp)


class AwemeIdTest(unittest.TestCase):
    def test_reads_a_bare_id(self):
        self.assertEqual(douyin_cdp.aweme_id("7709518080586596729"), "7709518080586596729")

    def test_reads_video_note_and_slides_paths(self):
        for kind in ("video", "note", "slides"):
            with self.subTest(kind=kind):
                self.assertEqual(
                    douyin_cdp.aweme_id(f"https://www.douyin.com/{kind}/7709518080586596729?x=1"),
                    "7709518080586596729",
                )

    def test_reads_a_whole_share_blurb(self):
        blurb = "4.61 复制打开抖音 https://v.douyin.com/xeHW_5KeR-E/ 看看"
        with patch.object(
            douyin_cdp,
            "_effective_url",
            return_value="https://www.iesdouyin.com/share/note/7709518080586596729/",
        ):
            self.assertEqual(douyin_cdp.aweme_id(blurb), "7709518080586596729")

    def test_unreadable_link_raises(self):
        with patch.object(douyin_cdp, "_effective_url", return_value="https://example.com/nothing"):
            with self.assertRaises(ValueError):
                douyin_cdp.aweme_id("https://example.com/nothing")


class BrowserBinTest(unittest.TestCase):
    def test_browser_bin_env_wins(self):
        chrome = str(Path(tempfile.gettempdir()) / "chrome")
        with patch.dict(os.environ, {"BROWSER_BIN": chrome}, clear=False), patch.object(
            douyin_cdp.os.path, "exists", return_value=True
        ):
            self.assertEqual(douyin_cdp.browser_bin(), chrome)

    def test_edge_bin_still_accepted(self):
        edge = str(Path(tempfile.gettempdir()) / "edge")
        with patch.dict(os.environ, {"EDGE_BIN": edge}, clear=True), patch.object(
            douyin_cdp.os.path, "exists", return_value=True
        ):
            self.assertEqual(douyin_cdp.browser_bin(), edge)

    def test_wrong_env_var_names_the_variable(self):
        chrome = str(Path(tempfile.gettempdir()) / "nope" / "chrome")
        with patch.dict(os.environ, {"BROWSER_BIN": chrome}, clear=False), patch.object(
            douyin_cdp.os.path, "exists", return_value=False
        ):
            with self.assertRaises(RuntimeError) as caught:
                douyin_cdp.browser_bin()
            self.assertIn("BROWSER_BIN", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
