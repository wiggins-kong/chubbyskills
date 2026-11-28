"""yt-dlp 统一封装：带平台 UA / Referer / 附加参数、依赖体检、重试和友好错误。

各平台脚本不再自己拼 yt-dlp 命令，避免「同一处修复要改 N 份拷贝」。
"""

import os
import re
import subprocess
import sys
import time
import urllib.parse

from chubby_common import cdp, deps

PLATFORM_AUTH = {
    "bilibili": ("bilibili", "https://passport.bilibili.com/login", ("SESSDATA",)),
    "youtube": (
        "youtube",
        "https://accounts.google.com/ServiceLogin?service=youtube&continue=https://www.youtube.com/",
        ("SID", "SAPISID", "__Secure-1PSID"),
    ),
    "weibo": ("weibo", "https://passport.weibo.com/sso/signin", ("SUB",)),
    "tiktok": ("tiktok", "https://www.tiktok.com/login", ("sessionid", "sid_tt")),
    "zhihu": ("zhihu", "https://www.zhihu.com/signin", ("z_c0",)),
}

_DOMAIN_TO_PLATFORM = {
    "bilibili.com": "bilibili",
    "b23.tv": "bilibili",
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "weibo.com": "weibo",
    "weibo.cn": "weibo",
    "tiktok.com": "tiktok",
    "zhihu.com": "zhihu",
}

_AUTH_ERROR_RE = re.compile(
    r"(?:"
    r"HTTP Error 40[13]|HTTP Error 412|\b403\b|\b412\b|\b401\b|"
    r"forbidden|precondition failed|unauthorized|"
    r"sign in|log\s*in|login required|not a bot|confirm your age|age[- ]?(?:limit|restricted|gate)|"
    r"requires authentication|registered users|cookies-from-browser|--cookies|"
    r"captcha|请登录|需要登录|风控|验证码"
    r")",
    re.IGNORECASE,
)


def resolve_platform_auth(platform: str | None = None, url: str | None = None):
    """按平台 ID 或 URL 解析 (site, login_url, auth_cookie_names)。"""
    if platform:
        spec = PLATFORM_AUTH.get(platform.strip().lower())
        if spec is not None:
            return spec
    if url:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        for domain, plat in _DOMAIN_TO_PLATFORM.items():
            if host == domain or host.endswith("." + domain):
                return PLATFORM_AUTH[plat]
    return None


def has_auth_profile(platform: str | None = None, url: str | None = None) -> bool:
    """判断对应平台是否已有落盘的 CDP 登录态 profile。"""
    spec = resolve_platform_auth(platform, url)
    return bool(spec and cdp.has_profile_session(spec[0]))


def has_explicit_cookies(cfg=None, args=()) -> bool:
    """判断用户是否已通过环境变量或参数显式指定了 cookie。"""
    if os.environ.get("YTDLP_COOKIES_FROM_BROWSER", "").strip():
        return True
    extra = getattr(cfg, "extra_ydl_args", ()) if cfg is not None else ()
    for arg in (*extra, *args):
        s = str(arg)
        if s in ("--cookies", "--cookies-from-browser") or s.startswith(
            ("--cookies=", "--cookies-from-browser=")
        ):
            return True
    return False


def ensure_platform_cookies_txt(platform: str | None = None, url: str | None = None) -> str | None:
    """通过持久化 CDP profile 确保平台已登录，并导出临时 Netscape cookies.txt。

    调用方负责在 finally 中删除返回的临时文件；CDP 不可用或登录失败时返回 None。
    """
    spec = resolve_platform_auth(platform, url)
    if spec is None:
        return None
    site, login_url, auth_cookies = spec
    if len(auth_cookies) == 1:
        gate_kwargs = {"auth_cookies": auth_cookies}
    else:
        # 多候选 cookie 名满足任一即视为已登录，避免 cdp.ensure_session(auth_cookies=...) 要求全量存在
        gate_kwargs = {
            "gate": lambda sess: any(
                c.get("name") in auth_cookies and c.get("value") for c in sess.cookies()
            )
        }
    try:
        with cdp.ensure_session(
            url or login_url,
            site=site,
            login_url=login_url,
            **gate_kwargs,
        ) as session:
            return cdp.export_cookies_txt(session)
    except (Exception, SystemExit) as exc:
        print(f"  ⚠️  CDP cookie 获取失败，跳过 cookie 回退：{exc}", file=sys.stderr)
        return None


def _extract_url(args) -> str | None:
    for arg in args:
        s = str(arg)
        if urllib.parse.urlsplit(s).scheme in ("http", "https"):
            return s
    return None


def _is_auth_error(exc: subprocess.CalledProcessError) -> bool:
    text = f"{exc.stderr or ''}\n{exc.stdout or ''}"
    return bool(text.strip() and _AUTH_ERROR_RE.search(text))


def _remove_file(path: str | None) -> None:
    if not path:
        return
    try:
        os.unlink(path)
    except OSError:
        pass


def _build_ydl_cmd(cfg, args, cookies_file: str | None = None) -> list[str]:
    cmd = ["yt-dlp", "--no-check-certificates", "--user-agent", cfg.ua]
    browser = os.environ.get("YTDLP_COOKIES_FROM_BROWSER", "").strip()
    if browser:
        cmd += ["--cookies-from-browser", browser]
    if cookies_file:
        cmd += ["--cookies", cookies_file]
    components = os.environ.get("YTDLP_REMOTE_COMPONENTS", "").strip()
    if components:
        cmd += ["--remote-components", components]
    if cfg.referer:
        cmd += ["--referer", cfg.referer]
    cmd += list(cfg.extra_ydl_args) + list(args)
    return cmd


def run_ydl(cfg, args, timeout, capture=True):
    """统一的 yt-dlp 调用，自动带上平台的 UA / Referer / 额外参数。

    遇到登录/403/412/年龄或机器人拦截且未显式指定 cookie 时，自动通过
    chubby_common.cdp 导出临时 cookies.txt 并重试一次。

    环境变量：
    - YTDLP_COOKIES_FROM_BROWSER：如 chrome / safari，传递给 --cookies-from-browser，
      用于平台把出口 IP 标记为机器人时的登录态证明。
    - YTDLP_REMOTE_COMPONENTS：如 ejs:github，YouTube JS challenge 求解组件。
    """
    deps.ensure_ytdlp()
    cmd = _build_ydl_cmd(cfg, args)
    try:
        return subprocess.run(
            cmd, capture_output=capture, text=True, timeout=timeout, check=True
        )
    except subprocess.CalledProcessError as exc:
        if has_explicit_cookies(cfg, args) or not _is_auth_error(exc):
            raise
        cookies_file = ensure_platform_cookies_txt(
            getattr(cfg, "id", None), _extract_url(args)
        )
        if not cookies_file:
            raise
        try:
            retry_cmd = _build_ydl_cmd(cfg, args, cookies_file=cookies_file)
            return subprocess.run(
                retry_cmd, capture_output=capture, text=True, timeout=timeout, check=True
            )
        finally:
            _remove_file(cookies_file)


def get_title(cfg, url: str) -> str:
    """获取视频标题，失败回退到平台默认标题。"""
    try:
        result = run_ydl(cfg, ["--get-title", url], timeout=cfg.info_timeout, capture=True)
        return result.stdout.strip() or cfg.default_title
    except Exception:
        return cfg.default_title


def download_audio(cfg, url: str, output_dir: str, filename: str = "audio.mp3") -> str:
    """下载音频为 mp3，失败自动重试，全部失败时抛可读 RuntimeError。"""
    audio_path = os.path.join(output_dir, filename)
    last_err = None
    for attempt in range(1, cfg.max_retry + 1):
        try:
            print(
                f"  ⬇️  Downloading (attempt {attempt}/{cfg.max_retry})...",
                file=sys.stderr,
            )
            run_ydl(
                cfg,
                [
                    "--extract-audio",
                    "--audio-format",
                    "mp3",
                    "--audio-quality",
                    "128K",
                    "-o",
                    audio_path,
                    url,
                ],
                timeout=cfg.download_timeout,
            )
            if not os.path.exists(audio_path):
                raise RuntimeError(
                    f"yt-dlp 退出成功但未产出音频文件：{audio_path}，链接可能已失效"
                )
            size_mb = os.path.getsize(audio_path) / (1024 * 1024)
            print(f"  ✅ Audio: {size_mb:.1f} MB", file=sys.stderr)
            return audio_path
        except subprocess.CalledProcessError as exc:
            last_err = f"yt-dlp 退出码 {exc.returncode}"
            if exc.stderr and exc.stderr.strip():
                last_err += f"：{exc.stderr.strip()}"
            print(f"  ⚠️  下载失败，{2 * attempt}s 后重试...", file=sys.stderr)
            time.sleep(2 * attempt)
        except subprocess.TimeoutExpired as exc:
            last_err = exc
            print("  ⚠️  下载超时，重试...", file=sys.stderr)
    hint = "可能原因：链接失效、需要登录 cookie、或平台限制。"
    if "403" in str(last_err) or "Forbidden" in str(last_err):
        # Measured 2026-10-05: a YouTube subtitle/download attempt 403s without
        # this, and succeeds with it. Naming the actual lever beats a generic
        # "platform restriction".
        hint += (
            "\n   403 多见于 YouTube 反爬：先设 YTDLP_REMOTE_COMPONENTS=ejs:github"
            "（JS 挑战求解组件），仍失败再加 YTDLP_COOKIES_FROM_BROWSER=<浏览器名>。"
        )
    raise RuntimeError(
        f"{cfg.name} 下载失败（已重试 {cfg.max_retry} 次）。{hint}原始错误：{last_err}"
    )
