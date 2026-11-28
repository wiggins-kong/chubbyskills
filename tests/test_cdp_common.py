import http.cookiejar
import io
import json
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

from chubby_common import cdp


class FakeProcess:
    def __init__(self, pid=1001, poll_sequence=None):
        self.pid = pid
        self._poll_sequence = list(poll_sequence or [])
        self.returncode = None
        self.waits = []
        self.terminated = False
        self.killed = False

    def poll(self):
        if self._poll_sequence:
            val = self._poll_sequence.pop(0)
            if val is not None:
                self.returncode = val
        return self.returncode

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def terminate(self):
        self.terminated = True
        if self.returncode is None:
            self.returncode = -15

    def kill(self):
        self.killed = True
        if self.returncode is None:
            self.returncode = -9


class FakeWebSocket:
    def __init__(self, handler, events=None):
        self.handler = handler
        self.events = list(events or [])
        self.sent = []
        self.closed = False
        self.timeout = None
        self._pending = []

    def settimeout(self, timeout):
        self.timeout = timeout

    def send(self, payload):
        req = json.loads(payload)
        self.sent.append(req)
        if self.events:
            self._pending.append(self.events.pop(0))
        response = self.handler(req)
        self._pending.append({"id": req["id"], "result": response})

    def recv(self):
        if self._pending:
            return json.dumps(self._pending.pop(0))
        if self.events:
            return json.dumps(self.events.pop(0))
        raise TimeoutError("no more messages")

    def close(self):
        self.closed = True


class ProfilePathsAndLockTest(unittest.TestCase):
    def test_profile_dir_cross_platform(self):
        with patch.object(cdp.sys, "platform", "win32"), patch.dict(
            os.environ, {"LOCALAPPDATA": r"C:\Users\u\AppData\Local"}, clear=False
        ):
            self.assertEqual(
                cdp.profile_dir("x"),
                os.path.join(r"C:\Users\u\AppData\Local", "chubbyskills", "cdp-profiles", "x"),
            )

        with patch.object(cdp.sys, "platform", "darwin"), patch(
            "chubby_common.cdp.os.path.expanduser", return_value="/Users/u"
        ):
            self.assertEqual(
                cdp.profile_dir("x"),
                os.path.join("/Users/u", "Library", "Application Support", "chubbyskills", "cdp-profiles", "x"),
            )

        with patch.object(cdp.sys, "platform", "linux"), patch.dict(
            os.environ, {"XDG_DATA_HOME": "/xdg/data"}, clear=False
        ):
            self.assertEqual(
                cdp.profile_dir("x"),
                os.path.join("/xdg/data", "chubbyskills", "cdp-profiles", "x"),
            )

        with patch.object(cdp.sys, "platform", "linux"), patch.dict(
            os.environ, {"XDG_DATA_HOME": ""}, clear=False
        ), patch("chubby_common.cdp.os.path.expanduser", return_value="/home/u"):
            self.assertEqual(
                cdp.profile_dir("x"),
                os.path.join("/home/u", ".local", "share", "chubbyskills", "cdp-profiles", "x"),
            )

    def test_invalid_site_name_rejected(self):
        for bad in ("", "..", "../x", "a/b", "a\\b", ".hidden"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                cdp.profile_dir(bad)

    def test_has_profile_session(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            cdp, "profile_dir", return_value=os.path.join(tmp, "site-a")
        ):
            self.assertFalse(cdp.has_profile_session("site-a"))
            os.makedirs(os.path.join(tmp, "site-a", "Default", "Network"), exist_ok=True)
            self.assertFalse(cdp.has_profile_session("site-a"))
            Path(tmp, "site-a", "Default", "Network", "Cookies").write_bytes(b"sqlite")
            self.assertTrue(cdp.has_profile_session("site-a"))

    def test_lock_contention_and_ephemeral_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            cdp, "profile_dir", return_value=os.path.join(tmp, "x")
        ):
            s1 = cdp.CDPSession(site="x", lock_timeout=0.2)
            try:
                with self.assertRaises(RuntimeError) as caught:
                    cdp.CDPSession(site="x", lock_timeout=0.2)
                self.assertIn("lock", str(caught.exception))
            finally:
                s1.close()

            s2 = cdp.CDPSession(site="x", lock_timeout=0.2)
            s2.close()

        eph = cdp.CDPSession(site="douyin", ephemeral=True)
        eph_dir = eph.user_data_dir
        self.assertTrue(os.path.isdir(eph_dir))
        eph.close()
        self.assertFalse(os.path.exists(eph_dir))


class EnsureSessionAndLifecycleTest(unittest.TestCase):
    def _patch_runtime(self, launches, ws_factory, proc_factory):
        fake_ws_mod = type(
            "FakeWebsocketMod",
            (),
            {"create_connection": staticmethod(lambda url, timeout=20: ws_factory(launches[-1]))},
        )

        def fake_popen(cmd, **kwargs):
            proc = proc_factory(cmd, kwargs)
            launches.append({"cmd": cmd, "kwargs": kwargs, "proc": proc})
            return proc

        return (
            patch.object(cdp.deps, "check_python_module"),
            patch.object(cdp, "browser_bin", return_value="/fake/chrome"),
            patch.object(cdp, "_free_port", side_effect=[9100 + i for i in range(10)]),
            patch.object(cdp, "_wait_target", return_value={"webSocketDebuggerUrl": "ws://127.0.0.1/page"}),
            patch.object(cdp.subprocess, "Popen", side_effect=fake_popen),
            patch.dict(sys.modules, {"websocket": fake_ws_mod}),
        )

    def test_headless_pass_keeps_headless_and_inherits_proxy(self):
        launches = []
        ws = FakeWebSocket(
            lambda req: (
                {"cookies": [{"name": "auth_token", "value": "tok"}]}
                if req["method"] == "Network.getAllCookies"
                else {"result": {"value": False}}
                if req["method"] == "Runtime.evaluate"
                else {}
            )
        )
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            cdp, "profile_dir", return_value=os.path.join(tmp, "x")
        ):
            patches = self._patch_runtime(launches, lambda _: ws, lambda *_: FakeProcess())
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
                sess = cdp.ensure_session(
                    "https://x.com/home",
                    site="x",
                    auth_cookies=("auth_token",),
                    blocked_selectors=("#captcha",),
                    settle_seconds=0,
                )
                try:
                    self.assertEqual(len(launches), 1)
                    cmd = launches[0]["cmd"]
                    self.assertIn("--headless=new", cmd)
                    self.assertNotIn("--noproxy", cmd)
                    self.assertNotIn("--no-proxy-server", cmd)
                    self.assertEqual(launches[0]["kwargs"].get("stdin"), subprocess.DEVNULL)
                finally:
                    sess.close()
        self.assertTrue(any(m["method"] == "Browser.close" for m in ws.sent))
        self.assertTrue(launches[0]["proc"].waits)

    def test_headless_to_headed_transition_closes_browser_and_reuses_profile(self):
        launches = []
        timeline = []
        state = {"headed": False, "polls": 0}
        sockets = []

        def make_ws(launch):
            is_headed = "--headless=new" not in launch["cmd"]
            state["headed"] = is_headed

            def handler(req):
                method = req["method"]
                if method == "Browser.close":
                    timeline.append(("Browser.close", is_headed))
                    return {}
                if method == "Network.getAllCookies":
                    if is_headed:
                        state["polls"] += 1
                        if state["polls"] >= 2:
                            return {"cookies": [{"name": "auth_token", "value": "ok"}]}
                    return {"cookies": []}
                if method == "Runtime.evaluate":
                    return {"result": {"value": False}}
                return {}

            ws = FakeWebSocket(handler)
            sockets.append(ws)
            return ws

        def make_proc(cmd, _kwargs):
            is_headed = "--headless=new" not in cmd
            timeline.append(("Popen", is_headed, [a for a in cmd if a.startswith("--user-data-dir=")][0]))
            p = FakeProcess(pid=2000 + len(launches))
            orig_wait = p.wait

            def tracked_wait(timeout=None):
                timeline.append(("proc.wait", is_headed))
                return orig_wait(timeout)

            p.wait = tracked_wait
            return p

        with tempfile.TemporaryDirectory() as tmp, patch.object(
            cdp, "profile_dir", return_value=os.path.join(tmp, "x")
        ):
            patches = self._patch_runtime(launches, make_ws, make_proc)
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
                sess = cdp.ensure_session(
                    "https://x.com/home",
                    site="x",
                    login_url="https://x.com/i/flow/login",
                    auth_cookies=("auth_token",),
                    require_login=True,
                    poll_interval=0.01,
                    settle_seconds=0,
                )
                try:
                    self.assertEqual(len(launches), 2)
                    self.assertIn("--headless=new", launches[0]["cmd"])
                    self.assertNotIn("--headless=new", launches[1]["cmd"])
                    udd_0 = [a for a in launches[0]["cmd"] if a.startswith("--user-data-dir=")][0]
                    udd_1 = [a for a in launches[1]["cmd"] if a.startswith("--user-data-dir=")][0]
                    self.assertEqual(udd_0, udd_1)

                    # Verify Browser.close + proc.wait() happened BEFORE headed Popen
                    idx_close = timeline.index(("Browser.close", False))
                    idx_wait = timeline.index(("proc.wait", False))
                    idx_headed = next(i for i, e in enumerate(timeline) if e[0] == "Popen" and e[1] is True)
                    self.assertLess(idx_close, idx_wait)
                    self.assertLess(idx_wait, idx_headed)

                    navs = [m["params"]["url"] for m in sockets[1].sent if m["method"] == "Page.navigate"]
                    self.assertEqual(navs[0], "https://x.com/i/flow/login")
                finally:
                    sess.close()

    def test_immediate_abort_when_user_closes_headed_window(self):
        launches = []

        def make_ws(_launch):
            return FakeWebSocket(lambda req: {"cookies": []} if req["method"] == "Network.getAllCookies" else {})

        def make_proc(cmd, _kwargs):
            if "--headless=new" not in cmd:
                return FakeProcess(poll_sequence=[None, 0])  # alive on start, then closed by user
            return FakeProcess()

        with tempfile.TemporaryDirectory() as tmp, patch.object(
            cdp, "profile_dir", return_value=os.path.join(tmp, "x")
        ):
            patches = self._patch_runtime(launches, make_ws, make_proc)
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
                start = cdp.time.monotonic()
                with self.assertRaises(RuntimeError) as caught:
                    cdp.ensure_session(
                        "https://x.com/home",
                        site="x",
                        require_login=True,
                        timeout=180,
                        poll_interval=0.05,
                        settle_seconds=0,
                    )
                elapsed = cdp.time.monotonic() - start
                self.assertIn("窗口已关闭", str(caught.exception))
                self.assertLess(elapsed, 2.0)

    def test_timeout_aborts_and_releases_lock(self):
        launches = []
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            cdp, "profile_dir", return_value=os.path.join(tmp, "x")
        ):
            patches = self._patch_runtime(
                launches,
                lambda _: FakeWebSocket(lambda req: {"cookies": []} if req["method"] == "Network.getAllCookies" else {}),
                lambda *_: FakeProcess(),
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
                with self.assertRaises(RuntimeError) as caught:
                    cdp.ensure_session(
                        "https://x.com/home",
                        site="x",
                        require_login=True,
                        timeout=0.05,
                        poll_interval=0.01,
                        settle_seconds=0,
                    )
                self.assertIn("超时", str(caught.exception))

            # Lock must be released after failure
            reacquired = cdp.CDPSession(site="x", lock_timeout=0.1)
            reacquired.close()

    def test_batch_reuse_and_fetch_page_state_html_js_xhr(self):
        launches = []
        xhr_event = {
            "method": "Network.responseReceived",
            "params": {
                "requestId": "req-1",
                "response": {"url": "https://api.example/graphql/TweetDetail", "status": 200},
            },
        }

        def make_ws(_launch):
            ws_ref = {}

            def handler(req):
                m = req["method"]
                if m == "Page.navigate" and req["params"]["url"].endswith("/1"):
                    ws_ref["ws"].events.append(xhr_event)
                if m == "Network.getAllCookies":
                    return {"cookies": [{"name": "auth_token", "value": "tok", "domain": ".example.com"}]}
                if m == "Network.getResponseBody":
                    return {"body": '{"ok": true}', "base64Encoded": False}
                if m == "Runtime.evaluate":
                    expr = req["params"]["expression"]
                    if "outerHTML" in expr:
                        return {"result": {"value": "<html><body>hi</body></html>"}}
                    return {"result": {"value": {"count": 42}}}
                return {}

            ws = FakeWebSocket(handler)
            ws_ref["ws"] = ws
            return ws

        with tempfile.TemporaryDirectory() as tmp, patch.object(
            cdp, "profile_dir", return_value=os.path.join(tmp, "x")
        ):
            patches = self._patch_runtime(launches, make_ws, lambda *_: FakeProcess())
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
                with cdp.ensure_session(
                    "https://example.com",
                    site="x",
                    auth_cookies=("auth_token",),
                    settle_seconds=0,
                ) as sess:
                    state1 = cdp.fetch_page_state(
                        "https://example.com/1",
                        session=sess,
                        js="window.__DATA__",
                        xhr_match="TweetDetail",
                        wait_seconds=1.0,
                        settle_seconds=0,
                    )
                    state2 = cdp.fetch_page_state(
                        "https://example.com/2",
                        session=sess,
                        js="1 + 1",
                        settle_seconds=0,
                    )
                    self.assertEqual(len(launches), 1)
                    self.assertEqual(state1["html"], "<html><body>hi</body></html>")
                    self.assertEqual(state1["js"], {"count": 42})
                    self.assertEqual(len(state1["xhr"]), 1)
                    self.assertEqual(state1["xhr"][0]["body"], '{"ok": true}')
                    self.assertEqual(state2["cookies"][0]["name"], "auth_token")
                    self.assertTrue(sess.is_alive())
                self.assertFalse(sess.is_alive())


class CookiesAndDownloadTest(unittest.TestCase):
    def test_export_cookies_txt_netscape_and_mozilla_cookiejar(self):
        cookies = [
            {
                "name": "auth_token",
                "value": "secret123",
                "domain": ".x.com",
                "path": "/",
                "secure": True,
                "expires": 1893456000.5,
            },
            {
                "name": "session",
                "value": "abc",
                "domain": "api.x.com",
                "path": "/v2",
                "secure": False,
                "expires": -1,
            },
        ]
        path = cdp.export_cookies_txt(cookies)
        try:
            text = Path(path).read_text(encoding="utf-8")
            self.assertIn("# Netscape HTTP Cookie File", text)
            self.assertIn(".x.com\tTRUE\t/\tTRUE\t1893456000\tauth_token\tsecret123", text)
            self.assertIn("api.x.com\tFALSE\t/v2\tFALSE\t0\tsession\tabc", text)

            jar = http.cookiejar.MozillaCookieJar(path)
            jar.load(ignore_discard=True, ignore_expires=True)
            loaded = {c.name: c.value for c in jar}
            self.assertEqual(loaded["auth_token"], "secret123")
            self.assertEqual(loaded["session"], "abc")
        finally:
            os.unlink(path)

    def test_download_with_cookies_matches_domain_and_sends_referer(self):
        captured = {}

        class FakeResponse(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.close()

        def fake_urlopen(request, timeout=60):
            captured["headers"] = {k.lower(): v for k, v in request.header_items()}
            return FakeResponse(b"asset-bytes")

        cookies = [
            {"name": "sid", "value": "111", "domain": ".example.com", "path": "/", "secure": True},
            {"name": "other", "value": "222", "domain": ".evil.com", "path": "/", "secure": True},
        ]
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            cdp.urllib.request, "urlopen", side_effect=fake_urlopen
        ):
            out = os.path.join(tmp, "sub", "pic.jpg")
            cdp.download_with_cookies(
                "https://cdn.example.com/media/pic.jpg",
                out,
                cookies=cookies,
                referer="https://example.com/",
            )
            self.assertEqual(Path(out).read_bytes(), b"asset-bytes")
            self.assertEqual(captured["headers"]["referer"], "https://example.com/")
            self.assertEqual(captured["headers"]["cookie"], "sid=111")


class IsolationImportTest(unittest.TestCase):
    def test_python_isolated_import_succeeds_without_websocket(self):
        code = (
            "import sys; "
            "sys.path.insert(0, sys.argv[1]); "
            "import chubby_common.cdp as cdp; "
            "assert 'websocket' not in sys.modules; "
            "assert hasattr(cdp, 'ensure_session')"
        )
        result = subprocess.run(
            [sys.executable, "-I", "-c", code, str(ROOT)],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_douyin_cdp_fetch_detail_uses_ephemeral_session(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "douyin_cdp_test_ref", ROOT / "douyin-transcribe" / "scripts" / "douyin_cdp.py"
        )
        douyin_cdp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(douyin_cdp)

        launches = []
        detail_payload = {"aweme_detail": {"aweme_id": "123456789012345", "desc": "demo"}}
        xhr_event = {
            "method": "Network.responseReceived",
            "params": {
                "requestId": "dy-1",
                "response": {"url": "https://www.iesdouyin.com/web/api/v2/aweme/detail/?id=1", "status": 200},
            },
        }

        def make_ws():
            ws_ref = {}

            def handler(req):
                if req["method"] == "Page.navigate":
                    ws_ref["ws"].events.append(xhr_event)
                if req["method"] == "Network.getResponseBody":
                    return {"body": json.dumps(detail_payload), "base64Encoded": False}
                return {}

            ws = FakeWebSocket(handler)
            ws_ref["ws"] = ws
            return ws

        ws = make_ws()
        fake_ws_mod = type(
            "FakeWebsocketMod",
            (),
            {"create_connection": staticmethod(lambda url, timeout=20: ws)},
        )

        def fake_popen(cmd, **kwargs):
            proc = FakeProcess()
            launches.append({"cmd": cmd, "proc": proc})
            return proc

        with patch.object(cdp.deps, "check_python_module"), patch.object(
            cdp, "browser_bin", return_value="/fake/chrome"
        ), patch.object(cdp, "_free_port", return_value=9222), patch.object(
            cdp, "_wait_target", return_value={"webSocketDebuggerUrl": "ws://127.0.0.1/page"}
        ), patch.object(cdp.subprocess, "Popen", side_effect=fake_popen), patch.dict(
            sys.modules, {"websocket": fake_ws_mod}
        ):
            data = douyin_cdp.fetch_detail("123456789012345", attempts=(1,))

        self.assertEqual(data, detail_payload)
        udd = [a.split("=", 1)[1] for a in launches[0]["cmd"] if a.startswith("--user-data-dir=")][0]
        self.assertFalse(os.path.exists(udd))


if __name__ == "__main__":
    unittest.main()
