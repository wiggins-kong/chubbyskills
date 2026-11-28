---
name: zhihu-transcribe
description: >
  知乎内容与视频采集：回答/专栏提取干净 Markdown 与本地图片，
  纯视频与 zvideo 下载音频并用 SenseVoice-Small 转录为中文文字稿。
metadata:
  category: "media"
  triggers: "[\"用户发送知乎链接\", \"把这个知乎回答存成笔记\", \"把这个知乎视频转成文字\", \"帮我采集这个知乎\"]"
  version: "1.1.0"
  tags: "[media, video, article, transcription, zhihu]"
---

# 知乎图文与视频采集 Skill

支持知乎具体回答（`/question/<qid>/answer/<aid>`）、专栏文章（`zhuanlan.zhihu.com/p/<pid>`）以及知乎视频（`/video/<vid>`、`/zvideo/<vid>`）。

## URL 规则

- **具体回答 / 专栏文章**：优先输出完整正文 Markdown，并将 `<figure>` / `<img>` 图片下载到 `<标题>.assets/` 目录、重写为相对路径；若正文混有内嵌视频链接，优先保留完整图文并在 Markdown 中保留视频链接。
- **纯问题链接（`/question/<qid>` 无 `/answer/<aid>`）**：不抓取、不创建任何文件，直接退出并提示用户提供具体的回答或专栏链接。
- **纯视频（无有效正文或 `/video/`、`/zvideo/`）**：走 `yt-dlp` 下载音频 + `SenseVoice-Small` 转录。

## 登录态与 CDP 回退

- 若本机已保存知乎 CDP Profile 登录态（`cdp.has_profile_session('zhihu')`），回答与专栏直接走无头 CDP（验证 `z_c0` cookie 与渲染后的正文 DOM）。
- 否则先尝试轻量 HTTP 抓取；遇到 `403`、`zse-ck`、登录墙或截断时，自动回退桌面 Chromium CDP 登录门禁（先无头，未登录时弹出有头窗口等待 180 秒登录/验证）。

## 环境要求

```bash
# Python 3.9+
python -m venv .venv
source .venv/bin/activate

# CDP 回退（回答/专栏遇到反爬或登录墙时需要）
pip install websocket-client

# 纯视频转录依赖
pip install funasr modelscope torch torchaudio

# 系统依赖（纯视频转录）
# macOS: brew install ffmpeg yt-dlp
# Ubuntu: sudo apt install ffmpeg && pip install yt-dlp
```

## 使用方法

```bash
# 具体回答（图文 / 混排视频）
python scripts/transcribe.py "https://www.zhihu.com/question/12345/answer/67890" -o ./output

# 专栏文章
python scripts/transcribe.py "https://zhuanlan.zhihu.com/p/123456" -o ./output

# 独立视频页（/video 或 /zvideo）
python scripts/transcribe.py "https://www.zhihu.com/video/1234567890" -o ./output
python scripts/transcribe.py "https://www.zhihu.com/zvideo/1234567890" -o ./output
```

## 已知限制

- **纯问题链接**：多回答聚合页不自动猜测代表回答，需提供具体的 `/answer/<aid>` 链接
- **反爬 / 登录墙**：需本机安装 Chrome/Edge/Chromium 109+ 与 `websocket-client` 才能弹出登录态复用窗口
- **盐选 / 付费内容**：需登录具备权限的账号
- **语言**：纯视频转录默认 `language=zh`；英文视频建议改用 youtube-transcribe
- **模型大小**：SenseVoice-Small 首次下载约 893MB

## ⚖️ 合规声明

仅供**个人学习与研究**使用。请遵守目标平台的服务条款（ToS）与 robots 规则，控制请求频率，不要用于批量抓取、商用爬取或侵犯他人权益的场景。下载内容的版权归原作者所有。
