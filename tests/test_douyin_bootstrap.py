import importlib.util
import io
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "douyin-transcribe" / "scripts" / "bootstrap.py"
SPEC = importlib.util.spec_from_file_location("douyin_bootstrap", SCRIPT)
bootstrap = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bootstrap
SPEC.loader.exec_module(bootstrap)


class BootstrapReportTest(unittest.TestCase):
    def test_missing_websocket_blocks_capture_and_names_the_package(self):
        with patch.object(bootstrap, "_has_module", return_value=False), patch.object(
            bootstrap.douyin_cdp, "browser_bin", return_value="/chrome"
        ), patch.object(bootstrap.shutil, "which", return_value="/ffmpeg"), patch(
            "sys.stdout", new_callable=io.StringIO
        ) as output:
            self.assertEqual(bootstrap.main([]), 1)
        self.assertIn("websocket-client", output.getvalue())
        self.assertIn("抓取未就绪", output.getvalue())

    def test_capture_ready_without_funasr(self):
        with patch.object(
            bootstrap, "_has_module", side_effect=lambda name: name == "websocket"
        ), patch.object(bootstrap.douyin_cdp, "browser_bin", return_value="/chrome"), patch.object(
            bootstrap.shutil, "which", return_value="/ffmpeg"
        ), patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(bootstrap.main([]), 0)
        self.assertIn("抓取已就绪", output.getvalue())
        self.assertIn("还缺 funasr", output.getvalue())


class InstallTest(unittest.TestCase):
    def test_install_uses_the_running_interpreter(self):
        calls = []
        state = {"installed": False}

        def fake_run(command, **kwargs):
            calls.append(command)
            state["installed"] = True
            return subprocess.CompletedProcess(command, 0)

        with patch.object(
            bootstrap, "_has_module", side_effect=lambda name: state["installed"]
        ), patch.object(bootstrap.subprocess, "run", side_effect=fake_run):
            self.assertEqual(bootstrap.install_websocket(), 0)
        self.assertEqual(calls[0][:4], [sys.executable, "-m", "pip", "install"])
        self.assertEqual(calls[0][-1], "websocket-client")


if __name__ == "__main__":
    unittest.main()
