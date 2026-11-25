---
name: douyin-transcribe
description: >
  抖音视频 + 图文笔记 → 下载 → 转录/存图 → 存为 Markdown 的完整工作流。
  抓取走本地无头 Chromium 的 CDP：不读 cookie、不用关掉正在用的浏览器，短链和整段分享文案都认。
  新机器先跑 scripts/bootstrap.py 自检与补齐环境。
metadata:
  category: "media"
  triggers: "[\"用户发送抖音分享链接或口头提到要转录抖音视频\", \"把这个抖音视频转成文字\", \"帮我存这个抖音图文\"]"
  version: "1.1.0"
  tags: "[\"media\", \"audio\", \"video\", \"note\", \"transcription\", \"douyin\", \"cdp\"]"
---

# 抖音采集 Skill

视频转文字稿；图文笔记存原文 + 本地图片。抓取统一走 CDP（本机无头 Chromium 打开分享页，
监听前端自己发的 `aweme/detail` 响应），不依赖登录态，也不会碰你正在用的浏览器。

## 新机器部署

抓取只需要三样：Python 3.9+、`websocket-client`、Edge/Chrome/Chromium 109+。
视频转文字另需 `ffmpeg` 与 `funasr`（体积较大，只存图文不需要）。

```bash
python scripts/bootstrap.py                    # 逐项检查，缺什么打印安装命令
python scripts/bootstrap.py --install          # 装 websocket-client（不在 PATH 时自动退到 uv）
python scripts/bootstrap.py --probe "<真实链接>"   # 真抓一次，确认跑通
```

退出码 0 表示抓取就绪。浏览器不在默认路径时用 `BROWSER_BIN` 指定可执行文件（旧名 `EDGE_BIN` 仍可用）。
没有浏览器就先装一个：Windows `winget install --id Microsoft.Edge`；macOS `brew install --cask google-chrome`；
Linux `sudo apt install chromium`。`funasr` 用 `pip install funasr modelscope torch torchaudio`，仓库内也可 `bash setup.sh video`。

## 使用方法

```bash
python scripts/transcribe.py "https://v.douyin.com/xxxxx"          # 视频 → 文字稿
python scripts/transcribe.py "https://www.douyin.com/note/123456" ./out
python scripts/transcribe.py "4.61 复制打开抖音 https://v.douyin.com/xxxxx/ 看看"   # 整段分享文案
```

任取其一：短链、`/video/`、`/note/`、`/slides/`、裸 `aweme_id`、整段分享文案。
只想拿原始 JSON 时用 `python scripts/douyin_cdp.py "<链接>" -o ./out`，输出 `douyin_<id>.json`。

## 流程（3 步）

### Step 1: 抓 aweme detail

`scripts/douyin_cdp.py` 用随机端口 + 临时 profile 启动一个无头浏览器，打开
`https://www.iesdouyin.com/share/video/<id>/`，从 `Network.responseReceived` 里截获
`aweme/detail` 响应。抖音分享页本身只给反爬空壳，所以旧的两条路径已删除，不再每次重试：

- ❌ 解析分享页 `window._ROUTER_DATA`（现在没有 `videoInfoRes`）
- ❌ 无 cookie 的 yt-dlp（返回 403；Windows 新版 Chromium 的 App-Bound 加密也让读 cookie 失效）
- ✅ CDP：浏览器自己发请求，cookie/风控上下文都在它自己手里

`images` 非空 → 图文，取 `images[].url_list[0]`；否则视频，取 `video.play_addr.url_list[0]`，
为空时退到 `video.bit_rate[].play_addr`，`playwm` → `play` 去水印。

### Step 2: SenseVoice-Small 转录（仅视频）

```python
from funasr import AutoModel
from funasr.utils.postprocess_utils import rich_transcription_postprocess

model = AutoModel(
    model="iic/SenseVoiceSmall",
    trust_remote_code=True,
    vad_model="fsmn-vad",
    vad_kwargs={"max_single_segment_time": 30000},
    device="cpu",
)

result = model.generate(input=audio_path, language="zh", use_itn=True, batch_size_s=60)
```

### Step 3: 生成 Markdown

- 视频：`<标题>.md`，带转录引擎与耗时。
- 图文：`<标题>.md` + `<标题>.assets/img_N.webp`，正文是 `desc`，图片按顺序嵌入。
  `chubby ingest` 会把 `.assets/` 一起搬进知识库，引用路径自动改写。

## 失败时

- 报缺 `websocket-client`：`python scripts/bootstrap.py --install`。
- 报找不到浏览器：装一个 109+ 的 Chromium 系浏览器，或 `BROWSER_BIN=/path/to/chrome`。
- 45 秒超时：先确认链接没过期、网络能访问抖音；抖音会限流，隔几分钟再试。
- 完全装不了浏览器（如受限服务器）：只能手工处理——用有浏览器的机器抓，或把正文粘贴成 Markdown 导入。

## 性能数据

| 视频时长 | 模型加载 | 转录耗时 | 总耗时 |
|------|------|------|------|
| 10 min | ~30s | ~25s | ~1 min |
| 30 min | ~30s | ~75s | ~2 min |
| 1h 22min | ~40s | ~180s | ~4 min |

抓取本身通常 10–20 秒（含浏览器启动），与视频长度无关。

## 已知限制

- 必须有本地 Chromium 109+；纯服务器环境要先装 `chromium` 才能 headless 跑。
- 整条链依赖抖音前端继续请求 `aweme/detail`，接口变化时需要更新 `douyin_cdp.py`。
- 图文只存图片与文案，不下载背景音乐；图片是平台压缩后的 webp/jpg，不是原片。
- 评论、作者主页、私密内容需要真实浏览器登录态，不在本 skill 范围。
- SenseVoice-Small 首次下载约 893MB（ModelScope），CPU 推理质量略低于 GPU，不支持说话人分离。
- yt-dlp 不再作为自动回退；真要用只能手工执行，且需要浏览器 cookie。

## 参考项目

- [FunAudioLLM/SenseVoice](https://github.com/FunAudioLLM/SenseVoice) - 语音识别模型
- [FunASR](https://github.com/modelscope/FunASR) - 语音识别框架
- [vangie/douyin-transcriber](https://github.com/vangie/douyin-transcriber) - 原始灵感（Go CLI）

## ⚖️ 合规声明

仅供**个人学习与研究**使用。请遵守目标平台的服务条款（ToS）与 robots 规则，控制请求频率，
不要用于批量抓取、商用爬取或侵犯他人权益的场景。下载内容的版权归原作者所有。
