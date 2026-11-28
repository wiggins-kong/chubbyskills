import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from tools import chubby, validate_outputs


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "zhihu-transcribe" / "scripts" / "transcribe.py"
SPEC = importlib.util.spec_from_file_location("zhihu_transcribe_script", SCRIPT)
zhihu = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(zhihu)


class _FakeHTTPResponse:
    def __init__(self, html: str, url: str):
        self._raw = html.encode("utf-8")
        self._url = url

    def read(self):
        return self._raw

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _run_cli(args: list[str]) -> tuple[int, str, str]:
    out = io.StringIO()
    err = io.StringIO()
    code = 0
    with patch.object(sys, "argv", ["transcribe.py", *args]), redirect_stdout(out), redirect_stderr(err):
        try:
            zhihu.main()
        except SystemExit as exc:
            code = int(exc.code or 0)
    return code, out.getvalue(), err.getvalue()


class ZhihuUrlAndContentTypeTest(unittest.TestCase):
    def test_classify_url_shapes(self):
        self.assertEqual(zhihu.classify_url("https://www.zhihu.com/question/12345"), "question_only")
        self.assertEqual(zhihu.classify_url("https://www.zhihu.com/question/12345/?sort=created"), "question_only")
        self.assertEqual(zhihu.classify_url("https://www.zhihu.com/question/12345/answer/67890"), "answer")
        self.assertEqual(zhihu.classify_url("https://zhuanlan.zhihu.com/p/987654"), "article")
        self.assertEqual(zhihu.classify_url("https://www.zhihu.com/zvideo/555666"), "video")
        self.assertEqual(zhihu.classify_url("https://www.zhihu.com/video/111222"), "video")

    def test_infer_content_type_for_zhihu(self):
        self.assertEqual(chubby.infer_content_type("zhihu", "https://zhuanlan.zhihu.com/p/987654"), "article")
        self.assertEqual(chubby.infer_content_type("zhihu", "https://www.zhihu.com/question/1/answer/2"), "note")
        self.assertEqual(chubby.infer_content_type("zhihu", "https://www.zhihu.com/video/123"), "video")
        self.assertEqual(chubby.infer_content_type("zhihu", "https://www.zhihu.com/zvideo/123"), "video")


class ZhihuQuestionOnlyTest(unittest.TestCase):
    def test_question_only_url_exits_nonzero_without_scraping_or_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = os.path.join(tmp, "nested_out")
            with patch.object(zhihu.urllib.request, "urlopen") as mock_http, patch.object(
                zhihu.cdp, "fetch_page_state"
            ) as mock_cdp, patch.object(zhihu.ytdlp, "download_audio") as mock_ydl:
                code, stdout, stderr = _run_cli(
                    ["https://www.zhihu.com/question/12345678?utm_id=1", "-o", out_dir]
                )

            self.assertNotEqual(code, 0)
            self.assertEqual(stdout, "")
            self.assertIn("/answer/<aid>", stderr)
            self.assertIn("zhuanlan.zhihu.com/p/<pid>", stderr)
            mock_http.assert_not_called()
            mock_cdp.assert_not_called()
            mock_ydl.assert_not_called()
            self.assertFalse(os.path.exists(out_dir))


class ZhihuAnswerAndArticleTest(unittest.TestCase):
    def test_answer_http_text_and_images_saved_to_assets(self):
        html = """
        <html>
          <head><title>如何评价本地优先架构 - 知乎</title></head>
          <body>
            <h1 class="QuestionHeader-title">如何评价本地优先架构</h1>
            <div class="AuthorInfo-name"><a class="UserLink-link">林深</a></div>
            <div class="ContentItem-time">发布于 2026-05-01 10:20</div>
            <div class="RichContent-inner">
              <p>这是一篇详细解释本地优先架构的长文回答，包含架构图与外部参考。</p>
              <figure>
                <noscript><img src="https://pic1.zhimg.com/v2-demo.jpg" alt="架构图"/></noscript>
                <img class="lazy" src="data:image/svg+xml;utf8,<svg></svg>"
                     data-actualsrc="https://pic1.zhimg.com/v2-demo.jpg"
                     data-original="https://pic1.zhimg.com/v2-demo_r.png" alt="架构图"/>
                <figcaption>架构总览</figcaption>
              </figure>
              <p>参考 <a href="https://link.zhihu.com/?target=https%3A%2F%2Fexample.com%2Fspec">设计文档</a>。</p>
            </div>
          </body>
        </html>
        """
        url = "https://www.zhihu.com/question/100/answer/200"
        downloads = []

        def fake_download(img_url, dest, *, cookies=None, referer="", **kwargs):
            downloads.append((img_url, dest, cookies, referer))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            Path(dest).write_bytes(b"PNGDATA")
            return dest

        with tempfile.TemporaryDirectory() as tmp, patch.object(
            zhihu.cdp, "has_profile_session", return_value=False
        ), patch.object(
            zhihu.urllib.request, "urlopen", return_value=_FakeHTTPResponse(html, url)
        ), patch.object(
            zhihu.cdp, "fetch_page_state"
        ) as mock_cdp, patch.object(
            zhihu.cdp, "download_with_cookies", side_effect=fake_download
        ):
            code, stdout, stderr = _run_cli([url, "-o", tmp])

            self.assertEqual(code, 0, stderr)
            mock_cdp.assert_not_called()
            md_path = stdout.strip()
            self.assertTrue(os.path.isfile(md_path))
            content = Path(md_path).read_text(encoding="utf-8")

            self.assertEqual(len(downloads), 1)
            self.assertEqual(downloads[0][0], "https://pic1.zhimg.com/v2-demo_r.png")
            self.assertEqual(downloads[0][3], url)

            stem = Path(md_path).stem
            expected_rel = f"{stem}.assets/img_1.png"
            self.assertTrue((Path(tmp) / f"{stem}.assets" / "img_1.png").is_file())
            self.assertIn(f"![架构图]({expected_rel})", content)
            self.assertIn("*架构总览*", content)
            self.assertIn("[设计文档](https://example.com/spec)", content)
            self.assertIn('created: "2026-05-01"', content)
            self.assertIn('author: "林深"', content)

            problems = validate_outputs.validate_file(md_path)
            self.assertEqual(problems, [])

    def test_blocked_http_falls_back_to_cdp_login_gate_for_zhuanlan(self):
        url = "https://zhuanlan.zhihu.com/p/778899"
        cdp_html = """
        <html>
          <body>
            <h1 class="Post-Title">专栏深度复盘</h1>
            <div class="Post-Author"><span class="AuthorInfo-name">周舟</span></div>
            <div class="Post-RichTextContainer">
              <p>登录后才能看到的完整专栏正文内容，这里有超过二十个字的详细说明。</p>
              <figure><img src="https://pic2.zhimg.com/col1.webp" alt="复盘图"/></figure>
            </div>
          </body>
        </html>
        """
        cookies = [{"name": "z_c0", "value": "secret-token", "domain": ".zhihu.com"}]
        downloads = []

        def fake_download(img_url, dest, *, cookies=None, referer="", **kwargs):
            downloads.append((img_url, cookies, referer))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            Path(dest).write_bytes(b"WEBP")
            return dest

        http_403 = urllib.error.HTTPError(url, 403, "Forbidden", {}, io.BytesIO(b""))
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            zhihu.cdp, "has_profile_session", return_value=False
        ), patch.object(
            zhihu.urllib.request, "urlopen", side_effect=http_403
        ), patch.object(
            zhihu.cdp,
            "fetch_page_state",
            return_value={"url": url, "html": cdp_html, "cookies": cookies, "js": None, "xhr": []},
        ) as mock_cdp, patch.object(
            zhihu.cdp, "download_with_cookies", side_effect=fake_download
        ):
            code, stdout, stderr = _run_cli([url, "-o", tmp])

            self.assertEqual(code, 0, stderr)
            mock_cdp.assert_called_once()
            _, kwargs = mock_cdp.call_args
            self.assertEqual(kwargs["site"], "zhihu")
            self.assertEqual(kwargs["auth_cookies"], ("z_c0",))
            self.assertEqual(kwargs["timeout"], 180)
            self.assertEqual(downloads[0][1], cookies)

            content = Path(stdout.strip()).read_text(encoding="utf-8")
            self.assertIn('note_type: "article"', content)
            self.assertIn("专栏深度复盘", content)
            self.assertIn(".assets/img_1.webp", content)

    def test_zse_ck_shell_triggers_cdp_fallback(self):
        url = "https://www.zhihu.com/question/10/answer/20"
        blocked_html = "<html><body><script>window.__zse_ck='1'</script>安全验证</body></html>"
        real_html = (
            "<h1 class='QuestionHeader-title'>反爬恢复回答</h1>"
            "<div class='RichContent-inner'><p>通过 CDP 正常拿到完整回答正文内容，无需手动复制。</p></div>"
        )
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            zhihu.cdp, "has_profile_session", return_value=False
        ), patch.object(
            zhihu.urllib.request, "urlopen", return_value=_FakeHTTPResponse(blocked_html, url)
        ), patch.object(
            zhihu.cdp,
            "fetch_page_state",
            return_value={"url": url, "html": real_html, "cookies": [{"name": "z_c0", "value": "1"}], "js": None, "xhr": []},
        ) as mock_cdp:
            code, stdout, stderr = _run_cli([url, "-o", tmp])
            self.assertEqual(code, 0, stderr)
            mock_cdp.assert_called_once()
            self.assertIn("通过 CDP 正常拿到完整回答正文内容", Path(stdout.strip()).read_text(encoding="utf-8"))

    def test_existing_profile_session_goes_straight_to_cdp(self):
        url = "https://www.zhihu.com/question/11/answer/22"
        cdp_html = (
            "<h1 class='QuestionHeader-title'>直连 CDP 回答</h1>"
            "<div class='RichContent-inner'><p>已有本地 profile 会话，直接通过无头 CDP 抓取正文。</p></div>"
        )
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            zhihu.cdp, "has_profile_session", return_value=True
        ), patch.object(
            zhihu.urllib.request, "urlopen"
        ) as mock_http, patch.object(
            zhihu.cdp,
            "fetch_page_state",
            return_value={"url": url, "html": cdp_html, "cookies": [{"name": "z_c0", "value": "ok"}], "js": None, "xhr": []},
        ) as mock_cdp:
            code, stdout, stderr = _run_cli([url, "-o", tmp])

            self.assertEqual(code, 0, stderr)
            mock_http.assert_not_called()
            mock_cdp.assert_called_once()
            self.assertIn("直连 CDP 回答", Path(stdout.strip()).read_text(encoding="utf-8"))


class ZhihuMixedAndVideoTest(unittest.TestCase):
    def test_mixed_text_images_and_video_prefers_text_and_keeps_video_link(self):
        url = "https://www.zhihu.com/question/33/answer/44"
        html = """
        <h1 class="QuestionHeader-title">图文混排带视频测试</h1>
        <div class="RichContent-inner">
          <p>这是正文第一段，详细介绍演示步骤和核心结论，字数足够构成完整图文回答。</p>
          <figure><img src="https://pic3.zhimg.com/step1.jpg" alt="步骤一"/></figure>
          <a class="video-box" href="https://www.zhihu.com/video/888999" data-lens-id="888999">观看演示视频</a>
        </div>
        """

        def fake_download(img_url, dest, **kwargs):
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            Path(dest).write_bytes(b"JPG")
            return dest

        with tempfile.TemporaryDirectory() as tmp, patch.object(
            zhihu.cdp, "has_profile_session", return_value=False
        ), patch.object(
            zhihu.urllib.request, "urlopen", return_value=_FakeHTTPResponse(html, url)
        ), patch.object(
            zhihu.cdp, "download_with_cookies", side_effect=fake_download
        ), patch.object(
            zhihu.ytdlp, "download_audio"
        ) as mock_ydl, patch.object(
            zhihu.funasr, "transcribe"
        ) as mock_asr:
            code, stdout, stderr = _run_cli([url, "-o", tmp])

            self.assertEqual(code, 0, stderr)
            mock_ydl.assert_not_called()
            mock_asr.assert_not_called()
            content = Path(stdout.strip()).read_text(encoding="utf-8")
            self.assertIn("这是正文第一段", content)
            self.assertIn(".assets/img_1.jpg", content)
            self.assertIn("https://www.zhihu.com/video/888999", content)

    def test_pure_video_answer_goes_to_ytdlp_and_funasr(self):
        url = "https://www.zhihu.com/question/55/answer/66"
        html = """
        <h1 class="QuestionHeader-title">纯视频回答标题</h1>
        <div class="AuthorInfo-name">视频作者</div>
        <div class="RichContent-inner">
          <a class="video-box" href="https://www.zhihu.com/video/999000" data-lens-id="999000">观看视频</a>
        </div>
        """
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            zhihu.cdp, "has_profile_session", return_value=False
        ), patch.object(
            zhihu.urllib.request, "urlopen", return_value=_FakeHTTPResponse(html, url)
        ), patch.object(
            zhihu.ytdlp, "download_audio", return_value="/tmp/audio.mp3"
        ) as mock_ydl, patch.object(
            zhihu.funasr, "transcribe", return_value=("这是视频语音转录结果。", 2.0)
        ) as mock_asr:
            code, stdout, stderr = _run_cli([url, "-o", tmp])

            self.assertEqual(code, 0, stderr)
            mock_ydl.assert_called_once()
            self.assertEqual(mock_ydl.call_args.args[1], "https://www.zhihu.com/video/999000")
            mock_asr.assert_called_once_with("/tmp/audio.mp3", language="zh")
            content = Path(stdout.strip()).read_text(encoding="utf-8")
            self.assertIn("这是视频语音转录结果。", content)
            self.assertIn(f'source: "{url}"', content)
            self.assertIn('transcriber: "SenseVoice-Small"', content)

    def test_zvideo_url_goes_directly_to_ytdlp_and_funasr(self):
        url = "https://www.zhihu.com/zvideo/123456789"
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            zhihu.ytdlp, "get_title", return_value="ZVideo 示例"
        ) as mock_title, patch.object(
            zhihu.ytdlp, "download_audio", return_value="/tmp/zv.mp3"
        ) as mock_ydl, patch.object(
            zhihu.funasr, "transcribe", return_value=("ZVideo 转录文本", 1.5)
        ) as mock_asr:
            code, stdout, stderr = _run_cli([url, "-o", tmp])

            self.assertEqual(code, 0, stderr)
            mock_title.assert_called_once()
            mock_ydl.assert_called_once()
            mock_asr.assert_called_once()
            content = Path(stdout.strip()).read_text(encoding="utf-8")
            self.assertIn("ZVideo 转录文本", content)


class ZhihuIsolatedImportTest(unittest.TestCase):
    def test_python_isolated_import_succeeds(self):
        code = (
            "import importlib.util, sys; "
            "spec = importlib.util.spec_from_file_location('zhihu_t', sys.argv[1]); "
            "mod = importlib.util.module_from_spec(spec); "
            "spec.loader.exec_module(mod); "
            "assert 'websocket' not in sys.modules; "
            "assert 'funasr' not in sys.modules; "
            "assert mod.classify_url('https://www.zhihu.com/question/1') == 'question_only'"
        )
        result = subprocess.run(
            [sys.executable, "-I", "-c", code, str(SCRIPT)],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
