"""Unit tests for HTTP-group desktop CDP fallbacks (WeChat, Xiaohongshu, X, Podcast)."""
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chubby_common import cdp


def _load(name: str, rel_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


wechat = _load("test_wechat_fetch", "wechat-article-ingest/scripts/fetch_article.py")
xhs = _load("test_xhs_fetch", "xiaohongshu-ingest/scripts/fetch_note.py")
x_fetch = _load("test_x_fetch", "x-ingest/scripts/fetch_tweet.py")
podcast = _load("test_podcast_transcribe", "podcast-transcribe/scripts/transcribe.py")


class _FakeSession:
    def __init__(self, eval_result):
        self.eval_result = eval_result
        self.expressions = []

    def eval_js(self, expression, await_promise=True):
        self.expressions.append((expression, await_promise))
        if callable(self.eval_result):
            return self.eval_result(expression)
        return self.eval_result


def _run_main(module, argv):
    out = io.StringIO()
    err = io.StringIO()
    code = 0
    with patch.object(sys, "argv", argv), redirect_stdout(out), redirect_stderr(err):
        try:
            res = module.main()
            if isinstance(res, int):
                code = res
        except SystemExit as exc:
            code = int(exc.code or 0)
    return code, out.getvalue(), err.getvalue()


class IsolatedImportTest(unittest.TestCase):
    def test_scripts_import_in_python_isolated_mode(self):
        scripts = (
            "wechat-article-ingest/scripts/fetch_article.py",
            "xiaohongshu-ingest/scripts/fetch_note.py",
            "x-ingest/scripts/fetch_tweet.py",
            "podcast-transcribe/scripts/safe_download.py",
            "podcast-transcribe/scripts/transcribe.py",
        )
        for rel in scripts:
            with self.subTest(script=rel):
                res = subprocess.run(
                    [
                        sys.executable,
                        "-I",
                        "-c",
                        "import runpy, sys; runpy.run_path(sys.argv[1], run_name='portable_probe')",
                        str(ROOT / rel),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                self.assertEqual(res.returncode, 0, res.stderr)


def _bs4_module():
    try:
        import bs4
        return bs4
    except ImportError:
        pass

    class NavigableString(str):
        pass

    class Tag:
        def __init__(self, name, attrs=None):
            self.name = name
            self.attrs = dict(attrs or {})
            self.children = []

        def get(self, key, default=None):
            return self.attrs.get(key, default)

        def get_text(self, sep="", strip=False):
            parts = []
            for child in self.children:
                parts.append(child.get_text(sep, strip=strip) if isinstance(child, Tag) else str(child))
            text = sep.join(parts)
            return text.strip() if strip else text

        def find(self, name=None, class_=None):
            classes = (self.attrs.get("class") or "").split()
            if (name is None or self.name == name) and (class_ is None or class_ in classes):
                return self
            for child in self.children:
                if isinstance(child, Tag):
                    hit = child.find(name, class_=class_)
                    if hit is not None:
                        return hit
            return None

    class _Builder(HTMLParser):
        def __init__(self):
            super().__init__()
            self.root = Tag("[document]")
            self.stack = [self.root]

        def handle_starttag(self, tag, attrs):
            node = Tag(tag, dict(attrs))
            self.stack[-1].children.append(node)
            if tag not in {"br", "img", "meta", "link", "input"}:
                self.stack.append(node)

        def handle_endtag(self, tag):
            for idx in range(len(self.stack) - 1, 0, -1):
                if self.stack[idx].name == tag:
                    del self.stack[idx:]
                    break

        def handle_data(self, data):
            if data:
                self.stack[-1].children.append(NavigableString(data))

    class BeautifulSoup(Tag):
        def __init__(self, markup, parser="html.parser"):
            builder = _Builder()
            builder.feed(markup)
            super().__init__("[document]")
            self.children = builder.root.children

    mod = types.ModuleType("bs4")
    mod.BeautifulSoup = BeautifulSoup
    mod.Tag = Tag
    mod.NavigableString = NavigableString
    return mod


class WeChatCDPFallbackTest(unittest.TestCase):
    VALID_HTML = (
        '<html><body>'
        '<h1 class="rich_media_title">WeChat Title</h1>'
        '<span class="rich_media_meta_nickname">Tester</span>'
        '<div class="rich_media_content"><p>' + ("这是一段足够长的微信公众号测试正文内容。" * 10) + "</p></div>"
        "</body></html>"
    )

    def test_fast_path_skips_cdp_when_curl_returns_article(self):
        proc = subprocess.CompletedProcess(["curl"], 0, self.VALID_HTML, "")
        with patch.dict(sys.modules, {"bs4": _bs4_module()}), patch.object(
            wechat.subprocess, "run", return_value=proc
        ), patch.object(cdp, "fetch_page_state") as mock_cdp:
            title, author, content = wechat.fetch_from_url("https://mp.weixin.qq.com/s/ok")
        self.assertEqual(title, "WeChat Title")
        self.assertEqual(author, "Tester")
        self.assertIn("微信公众号测试正文", content)
        mock_cdp.assert_not_called()

    def test_blocked_or_missing_content_falls_back_to_dom_gate_cdp(self):
        for body in (
            "<html><body>环境异常，请完成验证</body></html>",
            "<html><body>请在微信客户端打开链接</body></html>",
            "<html><body><div>empty</div></body></html>",
        ):
            with self.subTest(body=body[:20]):
                proc = subprocess.CompletedProcess(["curl"], 0, body, "")
                with patch.dict(sys.modules, {"bs4": _bs4_module()}), patch.object(
                    wechat.subprocess, "run", return_value=proc
                ), patch.object(
                    cdp,
                    "fetch_page_state",
                    return_value={"html": self.VALID_HTML, "cookies": []},
                ) as mock_cdp:
                    title, author, content = wechat.fetch_from_url("https://mp.weixin.qq.com/s/blocked")
                self.assertEqual(title, "WeChat Title")
                self.assertEqual(author, "Tester")
                self.assertIn("微信公众号测试正文", content)
                mock_cdp.assert_called_once()
                kwargs = mock_cdp.call_args.kwargs
                self.assertEqual(kwargs["site"], "wechat")
                self.assertIs(kwargs["require_login"], False)
                self.assertNotIn("auth_cookies", kwargs)
                self.assertEqual(kwargs["timeout"], 180)
                self.assertTrue(callable(kwargs["gate"]))

    def test_wechat_gate_checks_rich_media_content_and_block_text(self):
        self.assertFalse(wechat._wechat_gate(_FakeSession(False)))
        s_true = _FakeSession(True)
        self.assertTrue(wechat._wechat_gate(s_true))
        self.assertIn("div.rich_media_content", s_true.expressions[0][0])
        self.assertIn("环境异常", s_true.expressions[0][0])


class XiaohongshuCDPFallbackTest(unittest.TestCase):
    NOTE_STATE = {
        "note": {
            "noteDetailMap": {
                "n1": {
                    "note": {
                        "title": "XHS Note Title",
                        "desc": "Note body #ai",
                        "type": "normal",
                        "user": {"nickName": "Alice"},
                        "tagList": [{"name": "ai"}],
                        "interactInfo": {"likedCount": "12", "collectedCount": "3", "commentCount": "1"},
                        "imageList": [{"urlDefault": "https://sns-img.xhscdn.com/a.jpg"}],
                    }
                }
            }
        }
    }

    def test_fast_path_uses_http_and_skips_cdp(self):
        html = f"<script>window.__INITIAL_STATE__={json.dumps(self.NOTE_STATE)}</script>"
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"XHS_COOKIE": "web_session=env"}, clear=False), patch.object(
                xhs, "http_get", return_value=(html, "https://www.xiaohongshu.com/explore/1")
            ), patch.object(
                xhs, "download_images", return_value=[("url", "https://sns-img.xhscdn.com/a.jpg")]
            ) as mock_dl, patch.object(cdp, "fetch_page_state") as mock_cdp:
                code, out, _ = _run_main(
                    xhs, ["fetch_note.py", "https://www.xiaohongshu.com/explore/1", "-o", tmp]
                )
            self.assertEqual(code, 0)
            self.assertTrue(Path(out.strip()).exists())
            mock_cdp.assert_not_called()
            self.assertEqual(mock_dl.call_args.args[3], "web_session=env")

    def test_fallback_text_skips_cdp_even_when_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            fb = Path(tmp) / "note.txt"
            fb.write_text("Manual copied note body", encoding="utf-8")
            with patch.object(
                xhs, "http_get", return_value=("请通过小红书安全验证", "https://www.xiaohongshu.com/verify")
            ), patch.object(cdp, "fetch_page_state") as mock_cdp:
                code, out, _ = _run_main(
                    xhs,
                    [
                        "fetch_note.py",
                        "https://www.xiaohongshu.com/explore/1",
                        "--fallback-text",
                        str(fb),
                        "-o",
                        tmp,
                    ],
                )
            self.assertEqual(code, 0)
            self.assertIn("Manual copied note body", Path(out.strip()).read_text(encoding="utf-8"))
            mock_cdp.assert_not_called()

    def test_cdp_fallback_on_block_missing_state_and_403_406_and_reuses_cookie(self):
        triggers = [
            ("blocked_text", ("请通过小红书验证", "https://www.xiaohongshu.com/explore/1")),
            ("verify_url", ("<html>ok</html>", "https://www.xiaohongshu.com/website-login/verify")),
            ("missing_state", ("<html><body>no initial state</body></html>", "https://www.xiaohongshu.com/explore/1")),
            ("http_403", urllib.error.HTTPError("https://www.xiaohongshu.com/explore/1", 403, "Forbidden", {}, None)),
            ("http_406", urllib.error.HTTPError("https://www.xiaohongshu.com/explore/1", 406, "Not Acceptable", {}, None)),
        ]
        for name, effect in triggers:
            with self.subTest(trigger=name), tempfile.TemporaryDirectory() as tmp:
                cdp_resp = {
                    "url": "https://www.xiaohongshu.com/explore/1",
                    "html": "",
                    "js": json.dumps(self.NOTE_STATE),
                    "cookies": [
                        {"name": "web_session", "value": "fresh_sess"},
                        {"name": "a1", "value": "abc"},
                    ],
                }
                with patch.dict(os.environ, {}, clear=False), patch.object(
                    xhs, "http_get", side_effect=effect if isinstance(effect, Exception) else [effect]
                ), patch.object(cdp, "fetch_page_state", return_value=cdp_resp) as mock_cdp, patch.object(
                    xhs, "download_images", return_value=[("local", "img_1.jpg")]
                ) as mock_dl:
                    os.environ.pop("XHS_COOKIE", None)
                    code, out, _ = _run_main(
                        xhs, ["fetch_note.py", "https://www.xiaohongshu.com/explore/1", "-o", tmp]
                    )
                self.assertEqual(code, 0)
                md = Path(out.strip()).read_text(encoding="utf-8")
                self.assertIn("XHS Note Title", md)
                self.assertIn("Note body #ai", md)
                mock_cdp.assert_called_once()
                kwargs = mock_cdp.call_args.kwargs
                self.assertEqual(kwargs["site"], "xiaohongshu")
                self.assertEqual(kwargs["auth_cookies"], ("web_session",))
                self.assertIs(kwargs["gate"], xhs._xhs_gate)
                self.assertEqual(mock_dl.call_args.args[3], "web_session=fresh_sess; a1=abc")


class XCDPFallbackTest(unittest.TestCase):
    def test_fast_path_skips_cdp(self):
        tw = {
            "id_str": "123",
            "text": "Hello from syndication",
            "created_at": "2026-10-05",
            "user": {"name": "Ada", "screen_name": "ada"},
            "favorite_count": 5,
            "conversation_count": 1,
        }
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(x_fetch, "fetch_tweet", return_value=tw), patch.object(
                cdp, "fetch_page_state"
            ) as mock_cdp:
                code, out, _ = _run_main(
                    x_fetch, ["fetch_tweet.py", "https://x.com/ada/status/123", "-o", tmp]
                )
            self.assertEqual(code, 0)
            self.assertIn("Hello from syndication", Path(out.strip()).read_text(encoding="utf-8"))
            mock_cdp.assert_not_called()

    def test_cookies_arg_and_fallback_args_skip_cdp(self):
        preview_tw = {
            "id_str": "123",
            "text": "https://t.co/x",
            "user": {"name": "Ada", "screen_name": "ada"},
            "article": {"title": "Art", "preview_text": "Short preview", "rest_id": "1"},
        }
        gql_full = {
            "article": {"article_results": {"result": {"title": "Art", "plain_text": "Full via cookie"}}}
        }
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(x_fetch, "fetch_tweet", return_value=preview_tw), patch.object(
                x_fetch, "fetch_article_graphql", return_value=gql_full
            ), patch.object(cdp, "fetch_page_state") as mock_cdp:
                code, out, _ = _run_main(
                    x_fetch,
                    ["fetch_tweet.py", "https://x.com/ada/status/123", "--cookies", "auth_token=a; ct0=b", "-o", tmp],
                )
            self.assertEqual(code, 0)
            self.assertIn("Full via cookie", Path(out.strip()).read_text(encoding="utf-8"))
            mock_cdp.assert_not_called()

            fb = Path(tmp) / "fb.txt"
            fb.write_text("Fallback tweet text", encoding="utf-8")
            with patch.object(x_fetch, "fetch_tweet", side_effect=RuntimeError("down")), patch.object(
                cdp, "fetch_page_state"
            ) as mock_cdp:
                code, out, _ = _run_main(
                    x_fetch,
                    ["fetch_tweet.py", "https://x.com/ada/status/123", "--fallback-text", str(fb), "-o", tmp],
                )
            self.assertEqual(code, 0)
            self.assertIn("Fallback tweet text", Path(out.strip()).read_text(encoding="utf-8"))
            mock_cdp.assert_not_called()

    def test_syndication_unavailable_and_protected_post_use_cdp_xhr(self):
        xhr_payload = {
            "data": {
                "tweetResult": {
                    "result": {
                        "rest_id": "123",
                        "core": {
                            "user_results": {
                                "result": {
                                    "rest_id": "99",
                                    "legacy": {"name": "Ada", "screen_name": "ada"},
                                }
                            }
                        },
                        "legacy": {
                            "full_text": "Recovered from CDP XHR",
                            "created_at": "Mon Oct 05 12:00:00 +0000 2026",
                            "favorite_count": 42,
                            "reply_count": 7,
                        },
                    }
                }
            }
        }
        cdp_resp = {
            "url": "https://x.com/ada/status/123",
            "html": "",
            "js": None,
            "xhr": [{"url": "https://x.com/i/api/graphql/abc/TweetResultByRestId", "status": 200, "body": json.dumps(xhr_payload)}],
            "cookies": [{"name": "auth_token", "value": "tok"}, {"name": "ct0", "value": "csrf"}],
        }
        for failure in (RuntimeError("syndication_unavailable"), {}):
            with self.subTest(failure=repr(failure)), tempfile.TemporaryDirectory() as tmp:
                with patch.dict(os.environ, {}, clear=False), patch.object(
                    x_fetch, "fetch_tweet", side_effect=failure if isinstance(failure, Exception) else [failure]
                ), patch.object(cdp, "fetch_page_state", return_value=cdp_resp) as mock_cdp:
                    os.environ.pop("X_COOKIES", None)
                    code, out, _ = _run_main(
                        x_fetch, ["fetch_tweet.py", "https://x.com/ada/status/123", "-o", tmp]
                    )
                self.assertEqual(code, 0)
                md = Path(out.strip()).read_text(encoding="utf-8")
                self.assertIn("Recovered from CDP XHR", md)
                self.assertIn("likes: 42", md)
                mock_cdp.assert_called_once()
                kwargs = mock_cdp.call_args.kwargs
                self.assertEqual(kwargs["site"], "x")
                self.assertEqual(kwargs["auth_cookies"], ("auth_token", "ct0"))
                self.assertEqual(kwargs["xhr_match"], "TweetResultByRestId")

    def test_article_preview_uses_cdp_cookies_with_fetch_article_graphql(self):
        preview_tw = {
            "id_str": "123",
            "text": "https://t.co/x",
            "user": {"name": "Ada", "screen_name": "ada"},
            "article": {"title": "Long Art", "preview_text": "Short preview", "rest_id": "1"},
        }
        cdp_resp = {
            "url": "https://x.com/ada/status/123",
            "html": "",
            "js": None,
            "xhr": [],
            "cookies": [{"name": "auth_token", "value": "tok_cdp"}, {"name": "ct0", "value": "ct0_cdp"}],
        }
        gql_full = {
            "article": {"article_results": {"result": {"title": "Long Art", "plain_text": "Full CDP article body"}}}
        }
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {}, clear=False), patch.object(
                x_fetch, "fetch_tweet", return_value=preview_tw
            ), patch.object(cdp, "fetch_page_state", return_value=cdp_resp) as mock_cdp, patch.object(
                x_fetch, "fetch_article_graphql", return_value=gql_full
            ) as mock_gql:
                os.environ.pop("X_COOKIES", None)
                code, out, _ = _run_main(
                    x_fetch, ["fetch_tweet.py", "https://x.com/ada/status/123", "-o", tmp]
                )
            self.assertEqual(code, 0)
            md = Path(out.strip()).read_text(encoding="utf-8")
            self.assertIn("Full CDP article body", md)
            self.assertNotIn("以下为预览", md)
            mock_cdp.assert_called_once()
            mock_gql.assert_called_once_with("123", "auth_token=tok_cdp; ct0=ct0_cdp")


class PodcastCDPFallbackTest(unittest.TestCase):
    def _fake_downloader(self, *, fetch_text_impl, public_url_impl=None, download_impl=None):
        class FakeDownloader:
            @staticmethod
            def public_url(url):
                if public_url_impl:
                    return public_url_impl(url)
                return (url, "example.com", 443, "93.184.216.34")

            @staticmethod
            def fetch_text(url):
                return fetch_text_impl(url)

            @staticmethod
            def download(url, path):
                if download_impl:
                    return download_impl(url, path)
                Path(path).write_bytes(b"0" * 20480)

        return FakeDownloader

    def test_fast_path_skips_cdp_and_downloads_via_safe_download(self):
        dl_calls = []
        html = '<html><head><title>Ep 1 - Show</title><meta property="og:audio" content="https://cdn.example.com/ep1.mp3"></head></html>'
        fake = self._fake_downloader(
            fetch_text_impl=lambda url: html,
            download_impl=lambda url, path: (dl_calls.append(url), Path(path).write_bytes(b"0" * 20480)),
        )
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(podcast, "_sibling", return_value=fake), patch.object(
                cdp, "fetch_page_state"
            ) as mock_cdp:
                audio_path, title = podcast.download_audio("https://podcast.example.com/ep/1", tmp)
        self.assertEqual(title, "Ep 1")
        self.assertTrue(audio_path.endswith(".mp3"))
        self.assertEqual(dl_calls, ["https://cdn.example.com/ep1.mp3"])
        mock_cdp.assert_not_called()

    def test_ssrf_value_error_never_falls_back_to_cdp(self):
        fake = self._fake_downloader(
            fetch_text_impl=lambda url: (_ for _ in ()).throw(ValueError("private address blocked")),
        )
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(podcast, "_sibling", return_value=fake), patch.object(
                cdp, "fetch_page_state"
            ) as mock_cdp:
                with self.assertRaises(ValueError):
                    podcast.download_audio("http://127.0.0.1/secret", tmp)
        mock_cdp.assert_not_called()

    def test_spa_and_antibot_fall_back_to_cdp_then_safe_download(self):
        rendered_html = (
            '<html><head><title>SPA Episode - Pod</title></head>'
            '<body><audio src="https://cdn.example.com/spa.m4a"></audio></body></html>'
        )
        for fetch_impl in (
            lambda url: "<html><body><div id='app'>Loading...</div></body></html>",
            lambda url: (_ for _ in ()).throw(RuntimeError("HTTP 403")),
        ):
            dl_calls = []
            fake = self._fake_downloader(
                fetch_text_impl=fetch_impl,
                download_impl=lambda url, path: (dl_calls.append(url), Path(path).write_bytes(b"0" * 20480)),
            )
            with self.subTest(), tempfile.TemporaryDirectory() as tmp:
                with patch.object(podcast, "_sibling", return_value=fake), patch.object(
                    cdp,
                    "fetch_page_state",
                    return_value={"html": rendered_html, "js": {"title": "SPA Episode", "audio_url": ""}, "cookies": []},
                ) as mock_cdp:
                    audio_path, title = podcast.download_audio("https://podcast.example.com/ep/spa", tmp)
                self.assertEqual(title, "SPA Episode")
                self.assertTrue(audio_path.endswith(".m4a"))
                self.assertEqual(dl_calls, ["https://cdn.example.com/spa.m4a"])
                mock_cdp.assert_called_once()
                kwargs = mock_cdp.call_args.kwargs
                self.assertEqual(kwargs["site"], "podcast")
                self.assertIs(kwargs["require_login"], False)
                self.assertNotIn("auth_cookies", kwargs)
                self.assertIs(kwargs["gate"], podcast._podcast_gate)

    def test_podcast_gate_checks_audio_dom_without_login(self):
        self.assertFalse(podcast._podcast_gate(_FakeSession(False)))
        s_true = _FakeSession(True)
        self.assertTrue(podcast._podcast_gate(s_true))
        self.assertIn("og:audio", s_true.expressions[0][0])


if __name__ == "__main__":
    unittest.main()
