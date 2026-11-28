#!/usr/bin/env python3
"""
知乎图文 / 专栏 / 视频一键采集与转录工具

用法：
    python transcribe.py "https://www.zhihu.com/video/xxxxx"
    python transcribe.py "https://www.zhihu.com/zvideo/xxxxx"
    python transcribe.py "https://www.zhihu.com/question/xxx/answer/yyy" -o ./out
    python transcribe.py "https://zhuanlan.zhihu.com/p/xxxxx" -o ./out

特点：
    - 纯问题链接（/question/<qid> 无 /answer/<aid>）直接提示并退出，不抓不写文件
    - 回答/专栏优先轻量 HTTP（已有 zhihu CDP 登录态时直走无头 CDP），遇拦截回退桌面 CDP 登录态
    - 图文与混合视频内容优先保留完整正文与本地图片（.assets/），纯视频与 /zvideo/ 走音频转录
"""

import argparse
from datetime import datetime
import html as html_lib
from html.parser import HTMLParser
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = SKILL_ROOT if os.path.isdir(os.path.join(SKILL_ROOT, "chubby_common")) else os.path.dirname(SKILL_ROOT)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from chubby_common import cdp, funasr, markdown, ytdlp
from chubby_common.config import PlatformConfig

CFG = PlatformConfig(
    id="zhihu",
    name="知乎",
    tag="知乎",
    default_title="知乎视频",
    language="zh",  # 知乎以中文为主
    referer="https://www.zhihu.com/",
)

_BLOCK_MARKERS = (
    "__zse_ck",
    "zse-ck",
    "zse_ck",
    "安全验证",
    "请求存在异常",
    "请您登录后查看更多",
    "登录即可查看",
    "account/unhuman",
)
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"}
_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
_CONTENT_CLASSES = ("RichContent-inner", "Post-RichTextContainer", "Post-RichText", "RichText")


def classify_url(url: str) -> str:
    """区分纯问题、具体回答、专栏文章与视频链接。"""
    parsed = urllib.parse.urlsplit(str(url or "").strip())
    host = (parsed.hostname or "").lower()
    path = parsed.path or ""
    if host == "zhuanlan.zhihu.com" and re.match(r"^/p/[^/]+/?$", path):
        return "article"
    if host == "zhihu.com" or host.endswith(".zhihu.com"):
        if re.match(r"^/question/[^/]+/answer/[^/]+/?$", path) or re.match(r"^/answer/[^/]+/?$", path):
            return "answer"
        if re.match(r"^/question/[^/]+(?:/.*)?$", path):
            return "question_only"
        if re.match(r"^/(?:z?video)/[^/]+/?$", path):
            return "video"
        if re.match(r"^/p/[^/]+/?$", path):
            return "article"
    return "video"


def _attrs_dict(attrs) -> dict[str, str]:
    return {k.lower(): (v or "") for k, v in attrs}


def _classes(attrs: dict[str, str]) -> set[str]:
    return set(attrs.get("class", "").split())


def _clean_inline(text: str) -> str:
    return re.sub(r"[ \t\r\f\v]+", " ", text or "")


def _unwrap_zhihu_link(href: str, base_url: str) -> str:
    resolved = urllib.parse.urljoin(base_url, href)
    parsed = urllib.parse.urlsplit(resolved)
    if parsed.hostname == "link.zhihu.com":
        target = urllib.parse.parse_qs(parsed.query).get("target", [""])[0]
        if target:
            return target
    return resolved


def _normalize_date(raw) -> str | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, (int, float)) or (isinstance(raw, str) and raw.isdigit()):
        ts = int(raw)
        if ts > 1_000_000_000_000:
            ts //= 1000
        if ts > 0:
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    match = re.search(r"(\d{4}-\d{2}-\d{2})", str(raw))
    return match.group(1) if match else None


class _ContainerExtractor(HTMLParser):
    """提取首个匹配类名容器的内部 HTML，并收集基础 meta/标题/作者信息。"""

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.title_candidates: list[tuple[int, str]] = []
        self.author_candidates: list[str] = []
        self.date_candidates: list[str] = []
        self.zop_data: dict | None = None
        self.initial_data: dict | None = None
        self.containers: dict[str, str] = {}

        self._in_title = False
        self._title_buf: list[str] = []
        self._h1_kind: str | None = None
        self._h1_buf: list[str] = []
        self._author_depth = 0
        self._author_buf: list[str] = []
        self._time_depth = 0
        self._time_buf: list[str] = []
        self._in_initial_script = False
        self._script_buf: list[str] = []

        self._cap_cls: str | None = None
        self._cap_depth = 0
        self._cap_buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        ad = _attrs_dict(attrs)
        cls = _classes(ad)

        if self._cap_cls is not None:
            self._cap_buf.append(self.get_starttag_text() or f"<{tag}>")
            if tag not in _VOID_TAGS:
                self._cap_depth += 1
            return

        for target_cls in _CONTENT_CLASSES:
            if target_cls in cls and target_cls not in self.containers:
                self._cap_cls = target_cls
                self._cap_depth = 1
                self._cap_buf = []
                break

        if "data-zop" in ad and self.zop_data is None:
            try:
                parsed = json.loads(html_lib.unescape(ad["data-zop"]))
                if isinstance(parsed, dict):
                    self.zop_data = parsed
            except ValueError:
                pass

        if tag == "meta":
            prop = ad.get("property", "") or ad.get("itemprop", "")
            content = html_lib.unescape(ad.get("content", "")).strip()
            if prop == "og:title" and content:
                self.title_candidates.append((2, content))
            elif prop == "datePublished" and content:
                self.date_candidates.append(content)
        elif tag == "title":
            self._in_title = True
            self._title_buf = []
        elif tag == "h1":
            if "QuestionHeader-title" in cls or "Post-Title" in cls:
                self._h1_kind = "primary"
                self._h1_buf = []
            elif not self._h1_kind:
                self._h1_kind = "secondary"
                self._h1_buf = []
        elif tag == "script" and ad.get("id") == "js-initialData":
            self._in_initial_script = True
            self._script_buf = []

        if self._author_depth > 0:
            self._author_depth += 1
        elif "AuthorInfo-name" in cls or "Post-Author" in cls:
            self._author_depth = 1
            self._author_buf = []

        if self._time_depth > 0:
            self._time_depth += 1
        elif "ContentItem-time" in cls:
            self._time_depth = 1
            self._time_buf = []
            if ad.get("data-tooltip"):
                self.date_candidates.append(html_lib.unescape(ad["data-tooltip"]))

    def handle_startendtag(self, tag, attrs):
        if self._cap_cls is not None:
            self._cap_buf.append(self.get_starttag_text() or f"<{tag}/>")
            return
        ad = _attrs_dict(attrs)
        if tag == "meta":
            prop = ad.get("property", "") or ad.get("itemprop", "")
            content = html_lib.unescape(ad.get("content", "")).strip()
            if prop == "og:title" and content:
                self.title_candidates.append((2, content))
            elif prop == "datePublished" and content:
                self.date_candidates.append(content)

    def handle_endtag(self, tag):
        if self._cap_cls is not None:
            if tag not in _VOID_TAGS:
                self._cap_depth -= 1
            if self._cap_depth <= 0:
                self.containers[self._cap_cls] = "".join(self._cap_buf)
                self._cap_cls = None
                self._cap_buf = []
                return
            self._cap_buf.append(f"</{tag}>")
            return

        if tag == "title" and self._in_title:
            self._in_title = False
            raw = re.sub(r"\s*[-–—]\s*知乎\s*$", "", "".join(self._title_buf).strip())
            if raw:
                self.title_candidates.append((3, html_lib.unescape(raw)))
        elif tag == "h1" and self._h1_kind:
            text = html_lib.unescape("".join(self._h1_buf)).strip()
            if text:
                priority = 1 if self._h1_kind == "primary" else 4
                self.title_candidates.append((priority, text))
            self._h1_kind = None
        elif tag == "script" and self._in_initial_script:
            self._in_initial_script = False
            raw_json = "".join(self._script_buf).strip()
            if raw_json:
                try:
                    self.initial_data = json.loads(raw_json)
                except ValueError:
                    pass

        if self._author_depth > 0:
            self._author_depth -= 1
            if self._author_depth == 0:
                author = re.sub(r"\s+", " ", html_lib.unescape("".join(self._author_buf))).strip()
                if author:
                    self.author_candidates.append(author)

        if self._time_depth > 0:
            self._time_depth -= 1
            if self._time_depth == 0:
                self.date_candidates.append(html_lib.unescape("".join(self._time_buf)))

    def handle_data(self, data):
        if self._cap_cls is not None:
            self._cap_buf.append(data)
            return
        if self._in_title:
            self._title_buf.append(data)
        if self._h1_kind:
            self._h1_buf.append(data)
        if self._author_depth > 0:
            self._author_buf.append(data)
        if self._time_depth > 0:
            self._time_buf.append(data)
        if self._in_initial_script:
            self._script_buf.append(data)

    def handle_entityref(self, name):
        text = f"&{name};"
        if self._cap_cls is not None:
            self._cap_buf.append(text)
        else:
            self.handle_data(html_lib.unescape(text))

    def handle_charref(self, name):
        text = f"&#{name};"
        if self._cap_cls is not None:
            self._cap_buf.append(text)
        else:
            self.handle_data(html_lib.unescape(text))


class _ZhihuBodyParser(HTMLParser):
    """把知乎正文 HTML 转为整洁 Markdown，同时提取图片与内嵌视频链接。"""

    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.blocks: list[str] = []
        self.prose_parts: list[str] = []
        self.images: list[str] = []
        self._seen_images: set[str] = set()
        self.videos: list[str] = []
        self._seen_videos: set[str] = set()

        self._skip_depth = 0
        self._cur_parts: list[str] = []
        self._block_stack: list[str] = []
        self._list_stack: list[tuple[str, int]] = []
        self._in_pre = False
        self._pre_lang = ""
        self._pre_buf: list[str] = []
        self._in_code = False
        self._link: dict | None = None
        self._in_figcaption = False
        self._fig_buf: list[str] = []
        self._heading_level = 0
        self._blockquote_depth = 0
        self._in_figure = False
        self._in_noscript = False
        self._figure_img_emitted = False
        self._figure_noscript_imgs: list[tuple[str, str]] = []

    def _add_image(self, src: str, alt: str):
        if not src or src.startswith("data:image/svg+xml"):
            return
        resolved = urllib.parse.urljoin(self.base_url, src)
        if resolved.startswith("data:"):
            return
        if resolved in self._seen_images:
            return
        self._seen_images.add(resolved)
        self.images.append(resolved)
        self._flush_block()
        self.blocks.append(f"![{alt}]({resolved})")

    def _add_video(self, video_url: str, label: str = "视频"):
        if not video_url:
            return
        resolved = urllib.parse.urljoin(self.base_url, video_url)
        if resolved not in self._seen_videos:
            self._seen_videos.add(resolved)
            self.videos.append(resolved)
        self._flush_block()
        self.blocks.append(f"[{label}]({resolved})")

    def _flush_block(self):
        text = "".join(self._cur_parts).strip()
        self._cur_parts = []
        if not text:
            return
        text = re.sub(r"\n{3,}", "\n\n", text)
        if self._heading_level:
            level = min(6, max(2, self._heading_level))
            self.blocks.append(f"{'#' * level} {text}")
            return
        if self._blockquote_depth > 0:
            lines = [f"> {line}" if line.strip() else ">" for line in text.splitlines()]
            self.blocks.append("\n".join(lines))
            return
        self.blocks.append(text)

    def _append_text(self, text: str, count_prose: bool = True):
        if self._in_pre:
            self._pre_buf.append(text)
            if count_prose:
                self.prose_parts.append(text)
            return
        if self._in_figcaption:
            self._fig_buf.append(text)
            if count_prose:
                self.prose_parts.append(text)
            return
        if self._link is not None:
            self._link["text"].append(text)
            if count_prose and not self._link["is_video"]:
                self.prose_parts.append(text)
            return
        self._cur_parts.append(text)
        if count_prose:
            self.prose_parts.append(text)

    def handle_starttag(self, tag, attrs):
        ad = _attrs_dict(attrs)
        cls = _classes(ad)
        if self._skip_depth > 0:
            if tag not in _VOID_TAGS:
                self._skip_depth += 1
            return
        if tag in {"script", "style", "svg"}:
            self._skip_depth = 1
            return

        if tag == "noscript":
            self._in_noscript = True
            return

        if tag == "img":
            raw_src = ad.get("data-original") or ad.get("data-actualsrc") or ad.get("data-src") or ""
            if not raw_src or raw_src.startswith("data:image/svg+xml"):
                cand = ad.get("src", "")
                if not cand.startswith("data:image/svg+xml"):
                    raw_src = cand
            alt = _clean_inline(ad.get("alt", "")).strip()
            if self._in_figure and self._in_noscript:
                if raw_src:
                    self._figure_noscript_imgs.append((raw_src, alt))
                return
            if self._in_figure:
                self._figure_img_emitted = True
            self._add_image(raw_src, alt)
            return

        if tag in {"video", "source", "iframe"}:
            vsrc = ad.get("src") or ad.get("data-src") or ""
            if vsrc and ("video" in vsrc or "zhihu" in vsrc or tag in {"video", "source"}):
                self._add_video(vsrc)
            return

        if tag == "br":
            self._append_text("\n", count_prose=False)
            return
        if tag == "hr":
            self._flush_block()
            self.blocks.append("---")
            return

        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._flush_block()
            self._heading_level = int(tag[1])
            self._block_stack.append(tag)
            return

        if tag == "pre":
            self._flush_block()
            self._in_pre = True
            self._pre_lang = ""
            self._pre_buf = []
            return

        if tag == "code":
            if self._in_pre:
                for name in cls:
                    if name.startswith("language-"):
                        self._pre_lang = name[len("language-"):]
                        break
            else:
                self._in_code = True
                self._append_text("`", count_prose=False)
            return

        if tag == "blockquote":
            self._flush_block()
            self._blockquote_depth += 1
            return

        if tag in {"ul", "ol"}:
            self._flush_block()
            self._list_stack.append((tag, 0))
            return

        if tag == "li":
            self._flush_block()
            if self._list_stack:
                ltype, idx = self._list_stack[-1]
                idx += 1
                self._list_stack[-1] = (ltype, idx)
                prefix = f"{idx}. " if ltype == "ol" else "- "
            else:
                prefix = "- "
            self._cur_parts.append(prefix)
            return

        if tag in {"strong", "b"} and not self._in_pre:
            self._append_text("**", count_prose=False)
            return
        if tag in {"em", "i"} and not self._in_pre:
            self._append_text("*", count_prose=False)
            return

        if tag == "figcaption":
            self._in_figcaption = True
            self._fig_buf = []
            return

        if tag == "a":
            href = ad.get("href", "")
            lens_id = ad.get("data-lens-id", "")
            is_video = (
                "video-box" in cls
                or bool(lens_id)
                or bool(re.search(r"/(?:z?video)/", href))
            )
            if is_video and not href and lens_id:
                href = f"https://www.zhihu.com/video/{lens_id}"
            self._link = {"href": href, "is_video": is_video, "text": []}
            return

        if tag in {"p", "figure"}:
            self._flush_block()
            self._block_stack.append(tag)
            if tag == "figure":
                self._in_figure = True
                self._figure_img_emitted = False
                self._figure_noscript_imgs = []
            return

        if "video-box" in cls or "ZVideoItem" in cls or "RichText-video" in cls:
            lens_id = ad.get("data-lens-id", "")
            if lens_id:
                self._add_video(f"https://www.zhihu.com/video/{lens_id}")

    def handle_endtag(self, tag):
        if self._skip_depth > 0:
            if tag not in _VOID_TAGS:
                self._skip_depth -= 1
            return

        if tag == "pre" and self._in_pre:
            code = "".join(self._pre_buf).strip("\n")
            self._in_pre = False
            self._pre_buf = []
            if code:
                self.blocks.append(f"```{self._pre_lang}\n{code}\n```")
            return

        if tag == "code" and not self._in_pre and self._in_code:
            self._in_code = False
            self._append_text("`", count_prose=False)
            return

        if tag in {"strong", "b"} and not self._in_pre:
            self._append_text("**", count_prose=False)
            return
        if tag in {"em", "i"} and not self._in_pre:
            self._append_text("*", count_prose=False)
            return

        if tag == "figcaption" and self._in_figcaption:
            self._in_figcaption = False
            caption = _clean_inline("".join(self._fig_buf)).strip()
            self._fig_buf = []
            if caption:
                self._flush_block()
                self.blocks.append(f"*{caption}*")
            return

        if tag == "a" and self._link is not None:
            link = self._link
            self._link = None
            text = _clean_inline("".join(link["text"])).strip()
            href = link["href"]
            if link["is_video"] and href:
                self._add_video(href, text or "视频")
            elif href:
                resolved = _unwrap_zhihu_link(href, self.base_url)
                label = text or resolved
                self._append_text(f"[{label}]({resolved})", count_prose=False)
            elif text:
                self._append_text(text, count_prose=False)
            return

        if tag == "blockquote" and self._blockquote_depth > 0:
            self._flush_block()
            self._blockquote_depth -= 1
            return

        if tag in {"ul", "ol"} and self._list_stack:
            self._flush_block()
            self._list_stack.pop()
            return

        if tag == "li":
            self._flush_block()
            return

        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._flush_block()
            self._heading_level = 0
            if self._block_stack and self._block_stack[-1] == tag:
                self._block_stack.pop()
            return

        if tag == "noscript":
            self._in_noscript = False
            return

        if tag in {"p", "figure"}:
            if tag == "figure":
                if not self._figure_img_emitted:
                    for raw_src, alt in self._figure_noscript_imgs:
                        self._add_image(raw_src, alt)
                self._in_figure = False
                self._figure_img_emitted = False
                self._figure_noscript_imgs = []
            self._flush_block()
            if self._block_stack and self._block_stack[-1] == tag:
                self._block_stack.pop()

    def handle_data(self, data):
        if self._skip_depth > 0:
            return
        if self._in_pre:
            self._append_text(data)
            return
        cleaned = _clean_inline(data)
        if not cleaned.strip() and not self._cur_parts and self._link is None:
            return
        self._append_text(cleaned)

    def render(self) -> tuple[str, str, list[str], list[str]]:
        self._flush_block()
        body = "\n\n".join(b for b in self.blocks if b.strip())
        prose = re.sub(r"\s+", "", "".join(self.prose_parts))
        return body, prose, self.images, self.videos


def _extract_from_initial_data(initial_data: dict | None, url: str, kind: str) -> dict:
    if not isinstance(initial_data, dict):
        return {}
    entities = (initial_data.get("initialState") or {}).get("entities") or {}
    if kind == "answer":
        answers = entities.get("answers") or {}
        m = re.search(r"/answer/([^/?#]+)", url)
        ans = answers.get(m.group(1)) if m else None
        if not isinstance(ans, dict) and answers:
            ans = next((v for v in answers.values() if isinstance(v, dict)), None)
        if isinstance(ans, dict):
            q = ans.get("question") or {}
            q_id = str(q.get("id") or "")
            questions = entities.get("questions") or {}
            title = q.get("title") or (questions.get(q_id) or {}).get("title") or ""
            return {
                "title": title,
                "author": (ans.get("author") or {}).get("name") or "",
                "content": ans.get("content") or "",
                "created": _normalize_date(ans.get("createdTime") or ans.get("updatedTime")),
            }
    if kind == "article":
        articles = entities.get("articles") or {}
        m = re.search(r"/p/([^/?#]+)", url)
        art = articles.get(m.group(1)) if m else None
        if not isinstance(art, dict) and articles:
            art = next((v for v in articles.values() if isinstance(v, dict)), None)
        if isinstance(art, dict):
            return {
                "title": art.get("title") or "",
                "author": (art.get("author") or {}).get("name") or "",
                "content": art.get("content") or "",
                "created": _normalize_date(art.get("created") or art.get("updated")),
            }
    return {}


def parse_zhihu_page(html_text: str, url: str, kind: str) -> dict:
    """解析知乎回答/专栏 HTML，返回标题、作者、日期、Markdown 正文、图片与视频列表。"""
    ext = _ContainerExtractor()
    ext.feed(html_text)
    ext.close()

    init = _extract_from_initial_data(ext.initial_data, url, kind)
    zop = ext.zop_data or {}

    title = ""
    if ext.title_candidates:
        ext.title_candidates.sort(key=lambda pair: pair[0])
        title = ext.title_candidates[0][1]
    title = title or init.get("title") or zop.get("title") or ("知乎专栏文章" if kind == "article" else "知乎回答")

    author = (
        (ext.author_candidates[0] if ext.author_candidates else "")
        or init.get("author")
        or zop.get("authorName")
        or ""
    )

    created = None
    for cand in ext.date_candidates:
        created = _normalize_date(cand)
        if created:
            break
    if not created:
        created = init.get("created")

    body_html = ""
    for cls_name in _CONTENT_CLASSES:
        if cls_name in ext.containers and ext.containers[cls_name].strip():
            body_html = ext.containers[cls_name]
            break
    if not body_html:
        body_html = init.get("content") or ""

    parser = _ZhihuBodyParser(url)
    parser.feed(body_html)
    parser.close()
    body_md, prose, images, videos = parser.render()

    return {
        "title": title.strip(),
        "author": author.strip(),
        "created": created,
        "body_html": body_html,
        "body_md": body_md,
        "prose": prose,
        "images": images,
        "videos": videos,
    }


def _is_blocked(html_text: str, final_url: str, parsed: dict) -> bool:
    final_lower = (final_url or "").lower()
    if "/signin" in final_lower or "/account/unhuman" in final_lower:
        return True
    has_content = bool(parsed["body_md"].strip() or parsed["images"] or parsed["videos"])
    if not has_content:
        return True
    if any(marker in html_text for marker in _BLOCK_MARKERS) and len(parsed["prose"]) < 20 and not parsed["images"]:
        return True
    return False


def _http_fetch(url: str) -> tuple[str, str]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": cdp.UA,
            "Referer": CFG.referer,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read().decode("utf-8", "replace")
        return raw, resp.geturl()


def _zhihu_dom_ready(session) -> bool:
    return bool(
        session.eval_js(
            "(() => {"
            "  const href = location.href || '';"
            "  if (href.includes('/signin') || href.includes('/account/unhuman')) return false;"
            "  const text = document.body ? document.body.innerText : '';"
            "  if (text.includes('安全验证') && !document.querySelector('.RichContent-inner, .Post-RichText, .Post-RichTextContainer')) return false;"
            "  const el = document.querySelector('.RichContent-inner, .Post-RichTextContainer, .Post-RichText');"
            "  if (!el) return false;"
            "  return (el.innerText || '').trim().length > 0 || el.querySelector('img, video, .video-box, iframe') !== null;"
            "})()",
            await_promise=False,
        )
    )


def _cdp_fetch(url: str) -> tuple[str, list[dict], str]:
    state = cdp.fetch_page_state(
        url,
        site="zhihu",
        auth_cookies=("z_c0",),
        gate=_zhihu_dom_ready,
        timeout=180,
        settle_seconds=1.5,
    )
    return state.get("html") or "", list(state.get("cookies") or []), state.get("url") or url


def _fetch_answer_or_article(url: str, kind: str) -> tuple[dict, list[dict]]:
    if cdp.has_profile_session("zhihu"):
        print("  🧭 检测到已保存的知乎登录态，直连无头 CDP...", file=sys.stderr)
        html_text, cookies, final_url = _cdp_fetch(url)
        return parse_zhihu_page(html_text, final_url or url, kind), cookies

    try:
        html_text, final_url = _http_fetch(url)
        parsed = parse_zhihu_page(html_text, final_url or url, kind)
        if not _is_blocked(html_text, final_url, parsed):
            return parsed, []
        print("  ⚠️  轻量抓取触发拦截或内容受限，切换桌面 CDP 登录态...", file=sys.stderr)
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
        print(f"  ⚠️  轻量抓取失败（{exc}），切换桌面 CDP 登录态...", file=sys.stderr)

    html_text, cookies, final_url = _cdp_fetch(url)
    return parse_zhihu_page(html_text, final_url or url, kind), cookies


def _image_filename(img_url: str, index: int) -> str:
    ext = os.path.splitext(urllib.parse.urlsplit(img_url).path)[1].lower()
    if ext not in _IMAGE_EXTS:
        ext = ".jpg"
    return f"img_{index}{ext}"


def _download_images(image_urls: list[str], output_dir: str, asset_dir_name: str, cookies: list[dict], referer: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    asset_dir = os.path.join(output_dir, asset_dir_name)
    for idx, img_url in enumerate(image_urls, 1):
        name = _image_filename(img_url, idx)
        dest = os.path.join(asset_dir, name)
        try:
            cdp.download_with_cookies(img_url, dest, cookies=cookies, referer=referer)
            mapping[img_url] = f"{asset_dir_name}/{name}"
        except Exception as exc:
            print(f"  ⚠️  图片 {idx} 下载失败，保留原链接：{exc}", file=sys.stderr)
    return mapping


def _is_pure_video_post(parsed: dict) -> bool:
    if not parsed["videos"]:
        return False
    return len(parsed["prose"]) < 20 and not parsed["images"]


def _transcribe_video(
    video_url: str,
    output_dir: str,
    *,
    source_url: str,
    title_hint: str = "",
    author: str = "",
    created: str | None = None,
) -> str:
    print("=" * 50, file=sys.stderr)
    print(f"Step 1: Getting {CFG.name} video info...", file=sys.stderr)
    print("=" * 50, file=sys.stderr)
    title = title_hint or ytdlp.get_title(CFG, video_url)
    print(f"  📺 Title: {title}", file=sys.stderr)

    tmpdir = tempfile.mkdtemp(prefix=f"{CFG.tmp_prefix}-")
    try:
        print("\n" + "=" * 50, file=sys.stderr)
        print("Step 2: Downloading audio...", file=sys.stderr)
        print("=" * 50, file=sys.stderr)
        audio_path = ytdlp.download_audio(CFG, video_url, tmpdir)

        print("\n" + "=" * 50, file=sys.stderr)
        print("Step 3: Transcribing...", file=sys.stderr)
        print("=" * 50, file=sys.stderr)
        text, _ = funasr.transcribe(audio_path, language=CFG.language)

        body = markdown.note_markdown(
            title,
            text,
            {
                "type": "note",
                "platform": CFG.id,
                "tags": [CFG.tag],
                "source": source_url,
                "author": author,
                "transcriber": "SenseVoice-Small",
            },
            created=created,
        )
        safe_title = markdown.sanitize_filename(title, CFG.default_title)
        output_path = os.path.join(output_dir, f"{safe_title}.md")
        os.makedirs(output_dir, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(body)
        return output_path
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _save_text_note(parsed: dict, source_url: str, kind: str, output_dir: str, cookies: list[dict]) -> str:
    title = parsed["title"]
    fallback = "知乎专栏文章" if kind == "article" else "知乎回答"
    safe_title = markdown.sanitize_filename(title, fallback)
    asset_dir_name = f"{safe_title}.assets"

    url_map = _download_images(parsed["images"], output_dir, asset_dir_name, cookies, source_url)
    body_md = parsed["body_md"]
    for remote_url, local_rel in url_map.items():
        body_md = body_md.replace(f"]({remote_url})", f"]({local_rel})")

    if parsed["author"]:
        body_md = f"> 作者：{parsed['author']}\n\n{body_md}".strip()

    tags = [CFG.tag, "专栏"] if kind == "article" else [CFG.tag, "回答"]
    if url_map:
        tags.append("图文")

    doc = markdown.note_markdown(
        title,
        body_md,
        {
            "type": "note",
            "platform": CFG.id,
            "note_type": "article" if kind == "article" else "answer",
            "tags": tags,
            "source": source_url,
            "author": parsed["author"],
            "language": CFG.language,
        },
        created=parsed.get("created"),
    )
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, f"{safe_title}.md")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(doc)
    return output_path


def main():
    parser = argparse.ArgumentParser(description=f"{CFG.name} 回答/专栏/视频采集与转录")
    parser.add_argument("url", help=f"{CFG.name} 链接")
    parser.add_argument("output", nargs="?", default=".", help="输出目录（位置参数，兼容旧用法）")
    parser.add_argument("--output", "-o", dest="output_opt", help="输出目录")
    args = parser.parse_args()
    output_dir = args.output_opt or args.output

    kind = classify_url(args.url)
    if kind == "question_only":
        print(
            "❌ 检测到纯问题链接（缺少具体回答 ID），未抓取也未生成任何文件。\n"
            "   请提供具体回答链接（https://www.zhihu.com/question/<qid>/answer/<aid>）"
            "或专栏文章链接（https://zhuanlan.zhihu.com/p/<pid>）。",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        if kind in {"answer", "article"}:
            parsed, cookies = _fetch_answer_or_article(args.url, kind)
            if _is_pure_video_post(parsed):
                video_target = parsed["videos"][0]
                output_path = _transcribe_video(
                    video_target,
                    output_dir,
                    source_url=args.url,
                    title_hint=parsed["title"],
                    author=parsed["author"],
                    created=parsed.get("created"),
                )
            else:
                if not parsed["body_md"].strip() and not parsed["images"]:
                    raise RuntimeError("未能从页面提取正文内容，请检查链接或登录状态后重试")
                output_path = _save_text_note(parsed, args.url, kind, output_dir, cookies)
        else:
            output_path = _transcribe_video(args.url, output_dir, source_url=args.url)

        print(f"\n✅ Saved: {output_path}", file=sys.stderr)
        print(output_path)
    except RuntimeError as e:
        print(f"\n❌ {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
