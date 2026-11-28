import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chubby_common import ytdlp
from chubby_common.config import PlatformConfig


FAIL = "import sys; sys.stderr.write('ERROR: HTTP Error 412: Precondition Failed'); sys.exit(7)"
FORBIDDEN = "import sys; sys.stderr.write('ERROR: unable to download video data: HTTP Error 403: Forbidden'); sys.exit(1)"
BOT_CHECK = "import sys; sys.stderr.write(\"ERROR: [youtube] Sign in to confirm you're not a bot\"); sys.exit(1)"
SUCCEED = "from pathlib import Path; import sys; Path(sys.argv[sys.argv.index('-o') + 1]).write_bytes(b'audio')"


class YtdlpFailureTest(unittest.TestCase):
    def setUp(self):
        self.cfg = PlatformConfig(
            id="bilibili", name="Bilibili", tag="B站", default_title="视频", max_retry=2
        )
        self.run_process = subprocess.run
        ensure = patch.object(ytdlp.deps, "ensure_ytdlp")
        ensure.start()
        self.addCleanup(ensure.stop)
        no_cdp = patch.object(ytdlp, "ensure_platform_cookies_txt", return_value=None)
        no_cdp.start()
        self.addCleanup(no_cdp.stop)

    def child(self, script):
        # Exercise subprocess exit handling without yt-dlp or network access.
        def run(cmd, **kwargs):
            return self.run_process([sys.executable, "-c", script, *cmd], **kwargs)

        return run

    def test_captured_failure_raises_with_exit_code_and_stderr(self):
        with patch.object(ytdlp.subprocess, "run", side_effect=self.child(FAIL)):
            with self.assertRaises(subprocess.CalledProcessError) as ctx:
                ytdlp.run_ydl(self.cfg, ["https://example.invalid/video"], timeout=10)
        self.assertEqual(ctx.exception.returncode, 7)
        self.assertIn("HTTP Error 412", ctx.exception.stderr)

    def test_uncaptured_failure_still_raises(self):
        with patch.object(ytdlp.subprocess, "run", side_effect=self.child("raise SystemExit(7)")):
            with self.assertRaises(subprocess.CalledProcessError) as ctx:
                ytdlp.run_ydl(self.cfg, [], timeout=10, capture=False)
        self.assertEqual(ctx.exception.returncode, 7)

    def test_captured_success_keeps_stdout(self):
        with patch.object(ytdlp.subprocess, "run", side_effect=self.child("print('video title')")):
            result = ytdlp.run_ydl(self.cfg, [], timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "video title")

    def test_download_retries_then_returns_file(self):
        with tempfile.TemporaryDirectory() as folder:
            scripts = iter((FAIL, SUCCEED))

            def run(cmd, **kwargs):
                return self.child(next(scripts))(cmd, **kwargs)

            with patch.object(ytdlp.subprocess, "run", side_effect=run) as process, patch.object(
                ytdlp.time, "sleep"
            ) as sleep:
                result = ytdlp.download_audio(self.cfg, "https://example.invalid/video", folder)
            self.assertEqual(result, os.path.join(folder, "audio.mp3"))
            self.assertEqual(Path(result).read_bytes(), b"audio")
            self.assertEqual(process.call_count, 2)
            sleep.assert_called_once_with(2)

    def test_exhausted_retries_report_real_failure(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(
            ytdlp.subprocess, "run", side_effect=self.child(FAIL)
        ) as process, patch.object(ytdlp.time, "sleep"):
            with self.assertRaises(RuntimeError) as ctx:
                ytdlp.download_audio(self.cfg, "https://example.invalid/video", folder)
        message = str(ctx.exception)
        self.assertEqual(process.call_count, self.cfg.max_retry)
        self.assertIn("退出码 7", message)
        self.assertIn("HTTP Error 412: Precondition Failed", message)
        self.assertNotIn("退出成功", message)

    def test_a_403_names_the_lever_that_actually_fixes_it(self):
        """Measured 2026-10-05: YouTube 403s without the JS challenge component.

        A generic "platform restriction" hint sent the reader looking for
        cookies, which is not the first thing that helps.
        """
        with tempfile.TemporaryDirectory() as folder, patch.object(
            ytdlp.subprocess, "run", side_effect=self.child(FORBIDDEN)
        ), patch.object(ytdlp.time, "sleep"):
            with self.assertRaises(RuntimeError) as ctx:
                ytdlp.download_audio(self.cfg, "https://example.invalid/video", folder)
        message = str(ctx.exception)
        self.assertIn("YTDLP_REMOTE_COMPONENTS=ejs:github", message)
        self.assertIn("YTDLP_COOKIES_FROM_BROWSER", message)

    def test_an_unrelated_failure_does_not_claim_a_403(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(
            ytdlp.subprocess, "run", side_effect=self.child(FAIL)
        ), patch.object(ytdlp.time, "sleep"):
            with self.assertRaises(RuntimeError) as ctx:
                ytdlp.download_audio(self.cfg, "https://example.invalid/video", folder)
        self.assertNotIn("YTDLP_REMOTE_COMPONENTS", str(ctx.exception))

    def test_failure_without_stderr_still_reports_exit_code(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(
            ytdlp.subprocess, "run", side_effect=self.child("raise SystemExit(9)")
        ), patch.object(ytdlp.time, "sleep"):
            with self.assertRaises(RuntimeError) as ctx:
                ytdlp.download_audio(self.cfg, "https://example.invalid/video", folder)
        self.assertIn("退出码 9", str(ctx.exception))
        self.assertNotIn("退出成功", str(ctx.exception))

    def test_zero_exit_without_file_keeps_missing_file_diagnostic(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(
            ytdlp.subprocess, "run", side_effect=self.child("pass")
        ) as process:
            with self.assertRaisesRegex(RuntimeError, "退出成功但未产出音频文件"):
                ytdlp.download_audio(self.cfg, "https://example.invalid/video", folder)
        self.assertEqual(process.call_count, 1)


class _FakeCdpSession(ytdlp.cdp.CDPSession):
    def __init__(self, cookies):
        self._cookies = list(cookies)
        self.closed = False
        self.process = None
        self.ws = None
        self._events = []
        self._lock = None
        self.ephemeral = False
        self.user_data_dir = ""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def cookies(self, urls=None):
        return list(self._cookies)

    def close(self):
        self.closed = True


class YtdlpCdpFallbackTest(unittest.TestCase):
    def setUp(self):
        self.run_process = subprocess.run
        ensure = patch.object(ytdlp.deps, "ensure_ytdlp")
        ensure.start()
        self.addCleanup(ensure.stop)

    def child(self, script):
        def run(cmd, **kwargs):
            return self.run_process([sys.executable, "-c", script, *cmd], **kwargs)

        return run

    def test_platform_and_url_mapping_covers_all_five_platforms(self):
        cases = [
            ("bilibili", "https://www.bilibili.com/video/BV1xx411c7mD", "bilibili", ("SESSDATA",)),
            ("youtube", "https://youtu.be/dQw4w9WgXcQ", "youtube", ("SID", "SAPISID", "__Secure-1PSID")),
            ("weibo", "https://m.weibo.cn/status/123", "weibo", ("SUB",)),
            ("tiktok", "https://vm.tiktok.com/ZM123/", "tiktok", ("sessionid", "sid_tt")),
            ("zhihu", "https://zhuanlan.zhihu.com/p/123", "zhihu", ("z_c0",)),
        ]
        for platform, url, expected_site, expected_cookies in cases:
            with self.subTest(platform=platform):
                by_plat = ytdlp.resolve_platform_auth(platform, None)
                by_url = ytdlp.resolve_platform_auth(None, url)
                self.assertEqual(by_plat, by_url)
                self.assertEqual(by_plat[0], expected_site)
                self.assertTrue(by_plat[1].startswith("https://"))
                self.assertEqual(by_plat[2], expected_cookies)
        self.assertIsNone(ytdlp.resolve_platform_auth("unknown", "https://example.invalid/v"))

    def test_auth_errors_export_temp_netscape_cookies_retry_and_cleanup(self):
        cfg = PlatformConfig(
            id="youtube", name="YouTube", tag="YouTube", default_title="Video", max_retry=1
        )
        cookies = [
            {
                "name": "__Secure-1PSID",
                "value": "yt-secret",
                "domain": ".youtube.com",
                "path": "/",
                "secure": True,
                "expires": 1893456000,
            }
        ]
        seen_cmds = []
        exported_paths = []
        session = _FakeCdpSession(cookies)

        def fake_ensure_session(url, **kwargs):
            # Multi-cookie platform uses gate accepting any listed cookie name.
            self.assertEqual(kwargs["site"], "youtube")
            self.assertTrue(kwargs["gate"](session))
            return session

        real_export = ytdlp.cdp.export_cookies_txt

        def spy_export(src, **kwargs):
            path = real_export(src, **kwargs)
            exported_paths.append(path)
            return path

        require_cookies_script = (
            "import sys; "
            "assert '--cookies' in sys.argv, 'missing --cookies'; "
            "cpath = sys.argv[sys.argv.index('--cookies') + 1]; "
            "text = open(cpath, encoding='utf-8').read(); "
            "assert '# Netscape HTTP Cookie File' in text and 'yt-secret' in text; "
            "print('Recovered Title')"
        )
        scripts = iter((BOT_CHECK, require_cookies_script))

        def run(cmd, **kwargs):
            seen_cmds.append(list(cmd))
            return self.child(next(scripts))(cmd, **kwargs)

        with patch.dict(os.environ, {"YTDLP_COOKIES_FROM_BROWSER": ""}, clear=False), patch.object(
            ytdlp.subprocess, "run", side_effect=run
        ), patch.object(
            ytdlp.cdp, "ensure_session", side_effect=fake_ensure_session
        ), patch.object(
            ytdlp.cdp, "export_cookies_txt", side_effect=spy_export
        ):
            title = ytdlp.get_title(cfg, "https://www.youtube.com/watch?v=abc")

        self.assertEqual(title, "Recovered Title")
        self.assertEqual(len(seen_cmds), 2)
        self.assertNotIn("--cookies", seen_cmds[0])
        self.assertIn("--cookies", seen_cmds[1])
        self.assertEqual(len(exported_paths), 1)
        self.assertFalse(os.path.exists(exported_paths[0]))
        self.assertTrue(session.closed)

    def test_download_audio_403_uses_cdp_cookies_and_cleans_up_even_on_failure(self):
        cfg = PlatformConfig(
            id="bilibili", name="Bilibili", tag="B站", default_title="视频", max_retry=1
        )
        cookies = [{"name": "SESSDATA", "value": "bili-ok", "domain": ".bilibili.com", "path": "/"}]
        exported_paths = []
        real_export = ytdlp.cdp.export_cookies_txt

        def spy_export(src, **kwargs):
            path = real_export(src, **kwargs)
            exported_paths.append(path)
            return path

        with tempfile.TemporaryDirectory() as folder, patch.dict(
            os.environ, {"YTDLP_COOKIES_FROM_BROWSER": ""}, clear=False
        ), patch.object(
            ytdlp.subprocess, "run", side_effect=self.child(FORBIDDEN)
        ), patch.object(
            ytdlp.cdp, "ensure_session", return_value=_FakeCdpSession(cookies)
        ), patch.object(
            ytdlp.cdp, "export_cookies_txt", side_effect=spy_export
        ), patch.object(
            ytdlp.time, "sleep"
        ):
            with self.assertRaises(RuntimeError):
                ytdlp.download_audio(cfg, "https://www.bilibili.com/video/BV1xx411c7mD", folder)

        self.assertEqual(len(exported_paths), 1)
        self.assertFalse(os.path.exists(exported_paths[0]))

    def test_explicit_cookies_skip_cdp_fallback(self):
        cfg = PlatformConfig(
            id="bilibili", name="Bilibili", tag="B站", default_title="视频", max_retry=1
        )
        with patch.dict(
            os.environ, {"YTDLP_COOKIES_FROM_BROWSER": "chrome"}, clear=False
        ), patch.object(
            ytdlp.subprocess, "run", side_effect=self.child(FAIL)
        ), patch.object(
            ytdlp.cdp, "ensure_session"
        ) as ensure_session:
            with self.assertRaises(subprocess.CalledProcessError):
                ytdlp.run_ydl(cfg, ["https://www.bilibili.com/video/BV1xx411c7mD"], timeout=10)
            ensure_session.assert_not_called()

        with patch.dict(
            os.environ, {"YTDLP_COOKIES_FROM_BROWSER": ""}, clear=False
        ), patch.object(
            ytdlp.subprocess, "run", side_effect=self.child(FAIL)
        ), patch.object(
            ytdlp.cdp, "ensure_session"
        ) as ensure_session:
            with self.assertRaises(subprocess.CalledProcessError):
                ytdlp.run_ydl(
                    cfg,
                    ["--cookies", "/tmp/user.txt", "https://www.bilibili.com/video/BV1xx411c7mD"],
                    timeout=10,
                )
            ensure_session.assert_not_called()

    def test_batch_calls_reuse_same_cdp_site_profile(self):
        cfg = PlatformConfig(
            id="weibo", name="微博", tag="微博", default_title="微博视频", max_retry=1
        )
        cookies = [{"name": "SUB", "value": "wb-sub", "domain": ".weibo.com", "path": "/"}]
        sites_requested = []

        def fake_ensure_session(url, **kwargs):
            sites_requested.append((kwargs["site"], kwargs["auth_cookies"]))
            return _FakeCdpSession(cookies)

        scripts = iter((FAIL, "print('First')", FAIL, SUCCEED))

        def run(cmd, **kwargs):
            return self.child(next(scripts))(cmd, **kwargs)

        with tempfile.TemporaryDirectory() as folder, patch.dict(
            os.environ, {"YTDLP_COOKIES_FROM_BROWSER": ""}, clear=False
        ), patch.object(
            ytdlp.subprocess, "run", side_effect=run
        ), patch.object(
            ytdlp.cdp, "ensure_session", side_effect=fake_ensure_session
        ):
            title = ytdlp.get_title(cfg, "https://weibo.com/123")
            audio = ytdlp.download_audio(cfg, "https://weibo.com/123", folder)
            self.assertEqual(title, "First")
            self.assertTrue(os.path.exists(audio))

        self.assertEqual(sites_requested, [("weibo", ("SUB",)), ("weibo", ("SUB",))])

    def test_bilibili_missing_subtitle_triggers_cdp_cookie_retry_before_audio(self):
        spec = importlib.util.spec_from_file_location(
            "bilibili_transcribe_test_ref", ROOT / "bilibili-transcribe" / "scripts" / "transcribe.py"
        )
        bili = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bili)

        cookies = [{"name": "SESSDATA", "value": "bili-sess", "domain": ".bilibili.com", "path": "/"}]
        exported_paths = []
        real_export = ytdlp.cdp.export_cookies_txt

        def spy_export(src, **kwargs):
            path = real_export(src, **kwargs)
            exported_paths.append(path)
            return path

        seen_calls = []
        vtt_content = (
            "WEBVTT\n\n"
            "00:00:00.000 --> 00:00:05.000\n"
            + ("这是一段足够长的B站视频字幕测试内容用来验证不会静默降级到音频转录。" * 2)
            + "\n"
        )

        def fake_run_ydl(cfg, args, timeout, capture=True):
            seen_calls.append(list(args))
            if "--cookies" in args:
                out_tpl = args[args.index("-o") + 1]
                out_path = out_tpl.replace("%(ext)s", "zh-Hans.vtt")
                Path(out_path).write_text(vtt_content, encoding="utf-8")
                cpath = args[args.index("--cookies") + 1]
                self.assertIn("bili-sess", Path(cpath).read_text(encoding="utf-8"))
            return subprocess.CompletedProcess(args, 0, "", "")

        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ, {"YTDLP_COOKIES_FROM_BROWSER": ""}, clear=False
        ), patch.object(
            bili.ytdlp, "run_ydl", side_effect=fake_run_ydl
        ), patch.object(
            ytdlp.cdp, "ensure_session", return_value=_FakeCdpSession(cookies)
        ) as ensure_session, patch.object(
            ytdlp.cdp, "export_cookies_txt", side_effect=spy_export
        ):
            sub = bili.try_subtitles("https://www.bilibili.com/video/BV1xx411c7mD/", tmpdir)

        self.assertIsNotNone(sub)
        self.assertEqual(sub[1], "zh")
        self.assertEqual(len(seen_calls), 2)
        self.assertNotIn("--cookies", seen_calls[0])
        self.assertIn("--cookies", seen_calls[1])
        ensure_session.assert_called_once()
        self.assertEqual(len(exported_paths), 1)
        self.assertFalse(os.path.exists(exported_paths[0]))

    def test_bilibili_skips_cdp_when_browser_cookies_env_is_set(self):
        spec = importlib.util.spec_from_file_location(
            "bilibili_transcribe_test_env_ref", ROOT / "bilibili-transcribe" / "scripts" / "transcribe.py"
        )
        bili = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bili)

        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ, {"YTDLP_COOKIES_FROM_BROWSER": "edge"}, clear=False
        ), patch.object(
            bili.ytdlp, "run_ydl", return_value=subprocess.CompletedProcess([], 0, "", "")
        ) as run_ydl, patch.object(
            ytdlp.cdp, "ensure_session"
        ) as ensure_session:
            sub = bili.try_subtitles("https://www.bilibili.com/video/BV1xx411c7mD/", tmpdir)

        self.assertIsNone(sub)
        run_ydl.assert_called_once()
        ensure_session.assert_not_called()

    def test_python_isolated_import_succeeds(self):
        code = (
            "import sys; "
            "sys.path.insert(0, sys.argv[1]); "
            "import chubby_common.ytdlp as y; "
            "assert 'websocket' not in sys.modules; "
            "assert hasattr(y, 'ensure_platform_cookies_txt'); "
            "assert hasattr(y, 'has_auth_profile')"
        )
        result = subprocess.run(
            [sys.executable, "-I", "-c", code, str(ROOT)],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
