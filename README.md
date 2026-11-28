**中文** · [English](./README.en.md)

# chubbyskills（fork）

把中文平台内容采集进本地知识库的 Agent Skills。本仓库是
[chubbyguan/chubbyskills](https://github.com/chubbyguan/chubbyskills) 的 fork：
上游的能力、安装说明、平台状态和文档都以原仓库为准，这里**只记录本 fork 改了什么**。

## 与上游的差异

上游是完整的 14 个技能加一个 CLI。本 fork 目前只改抓取层，表里没有的都与上游一致：

| 模块 | 改动 |
|---|---|
| 抖音抓取 | 只走 CDP：匿名临时 profile 的无头 Chromium 抓 `aweme/detail`，不再每次尝试分享页和 yt-dlp |
| 抖音图文 | 支持 `/note/`、`/slides/`：原文 + 图片本地化到 `<标题>.assets/`，不再报「不是视频」 |
| 知乎采集 | 回答（`/answer/`）与专栏输出完整图文 Markdown 并本地化图片到 `<标题>.assets/`；图文混排视频时保留完整正文与视频链接；纯问题链接（`/question/<qid>` 无 `/answer/`）直接拒抓并提示；纯视频与 `/video/`、`/zvideo/` 走 `yt-dlp` + `SenseVoice-Small` |
| 桌面 CDP 兜底 | `chubby_common/cdp.py` 优先轻量快路径，被拦时回退本机 `Edge`/`Chrome` + `websocket-client` 与每站点独立本地 profile（无头校验失败弹 180 秒有头窗口），覆盖 B 站防字幕退化、公众号/播客免登录 DOM 门禁、知乎与 `yt-dlp`/X 回退 |
| 14 合 1 技能包 | `install_skill.py --bundle` 将 14 个技能合并安装为单一 `chubby` 技能目录（仅 1 个根 `SKILL.md` + `tools/chubby_ingest.py` + 4 份 `references/*.md` 工作流指南） |
| 新机器部署 | `douyin-transcribe/scripts/bootstrap.py`：检查 → 补齐 → 用真实链接验真 |

## 14 合 1 统一技能包安装

```bash
git clone https://github.com/wiggins-kong/chubbyskills.git
cd chubbyskills
python3 tools/install_skill.py --bundle --clean-legacy --dest <你的 agent skills 目录>
# 首次全新安装可省略 --clean-legacy；--clean-legacy 清理旧独立技能目录时会自动保留其中的 .venv / venv
# 可选：--bundle-name <自定义名称>
```

安装后生成单个 `chubby/` 目录（仅保留 1 个根 `SKILL.md` 入口），`tools/chubby_ingest.py "<source>"` 会结合 `platforms/*.yaml` 自动识别链接、BV 号或本地文件并路由到对应采集脚本（或 `tools/import_document.py`），传 `--enrich` 可顺带生成摘要与标签。4 个非采集类工作流保留在 `references/`（`content-enrich.md`、`knowledge-base-management.md`、`learning-notes-automation.md`、`industry-intelligence-radar.md`），由 Agent 按需读取。单技能安装模式（`python3 tools/install_skill.py --list` / `<skill> --dest ...` / `--all --dest ...`）同样保留，并会自动为所有依赖 `chubby_common` 的技能打包 `chubby_common/`。

## 桌面端 Chromium CDP 兜底

`chubby_common/cdp.py` 仅在桌面端运行，调用本机 `Edge` / `Chrome` / `Chromium`（109+）+ `websocket-client`，按站点隔离保存本地 profile（不读取你日常浏览器的 Cookie）。整体遵循**轻量快路径优先 → 本地按站点 profile 无头校验 → 必要时 180 秒有头弹窗登录/验证**；浏览器不在默认路径时用 `BROWSER_BIN` 指定：

- **公众号 / 播客（免账号登录 DOM 门禁）**：轻量 HTTP 被验证/风控拦截或需 JS 渲染时回退 CDP（`require_login=False`），只等待正文 `div.rich_media_content` 或音频元数据渲染完成，无需登录账号。
- **抖音（匿名临时 profile）**：直接使用 `ephemeral=True` 匿名临时 profile 无头抓取 `aweme/detail`，不保存 Cookie，也不弹登录窗。
- **B 站防字幕退化**：未登录 `yt-dlp` 抓字幕常静默退出 0 却无 `.vtt`；在降级到音频下载 + SenseVoice 转录前，会先用 CDP 导出 Cookie 重试一次字幕抓取。
- **知乎 / X / `yt-dlp` 平台**：知乎在已有本地 `zhihu` profile 时直连无头 CDP，否则 HTTP 遇 `403` / `zse-ck` / 登录墙再回退；`yt-dlp`（B 站、YouTube、微博、TikTok）与 X 在遇到登录或 403/风控时按站点导出 Cookie 或抓 GraphQL XHR。

## 抖音：新机器起步

```bash
git clone https://github.com/wiggins-kong/chubbyskills.git
cd chubbyskills
python3 tools/install_skill.py douyin-transcribe --dest <你的 agent skills 目录>
```

在装好的技能目录里，先让脚本告诉你还缺什么：

```bash
python3 scripts/bootstrap.py                  # 逐项检查，缺什么打印安装命令
python3 scripts/bootstrap.py --install        # 只装 websocket-client
python3 scripts/bootstrap.py --probe "<一条真实的抖音链接>"   # 抓一次，确认跑通
```

抓取需要 Python 3.9+、`websocket-client` 和 Edge/Chrome/Chromium 109+；视频转文字另需
`ffmpeg` 与 `funasr`，只存图文不需要它们。之后：

```bash
python3 scripts/transcribe.py "<抖音链接或整段分享文案>" ./out
```

浏览器不在默认路径时用 `BROWSER_BIN` 指定。细节、失败处理和输出格式见
[douyin-transcribe/SKILL.md](./douyin-transcribe/SKILL.md)。

## 上游文档

通用能力都以原仓库为准：

- 上游 README：[中文](https://github.com/chubbyguan/chubbyskills/blob/main/README.md) · [English](https://github.com/chubbyguan/chubbyskills/blob/main/README.en.md)
- [安装指南](https://github.com/chubbyguan/chubbyskills/blob/main/docs/installation.md) · [平台状态](https://github.com/chubbyguan/chubbyskills/blob/main/docs/platform-status.md) · [订阅与调度](https://github.com/chubbyguan/chubbyskills/blob/main/docs/subscriptions.md) · [MCP 配置](https://github.com/chubbyguan/chubbyskills/blob/main/docs/mcp-workflow.md)
- [Releases](https://github.com/chubbyguan/chubbyskills/releases) · [更新日志](https://github.com/chubbyguan/chubbyskills/blob/main/CHANGELOG.md)

本仓库的 `docs/` 与 `CHANGELOG.md` 沿用上游，只有上面差异表涉及的部分不同。

## 相关项目

- [chubby-recruitment-ops](https://github.com/chubbyguan/chubby-recruitment-ops)：Codex 与 Claude Code 的招聘运营插件，上游作者的另一个项目。

## 使用范围与许可

采集类技能用于个人学习与研究。请遵守来源平台条款、`robots.txt` 和相关法律，使用自己有权访问和保存的内容；
不要用于批量抓取、商用爬取、二次分发或侵犯他人权益的场景。原内容版权归原作者。

代码采用 [MIT License](./LICENSE)，按现状提供；代码许可不授予第三方内容的使用权。

## 关于 Chubby（上游作者）

项目的设计、技能主体和文档都来自 Chubby：
[X / Twitter](https://x.com/Chubbyguan) · [即刻](https://web.okjike.com/u/a876838d-d9a8-494b-9494-bb3410b77dd5) · [小红书](https://www.xiaohongshu.com/user/profile/57c061626a6a696f5a70f9a8) · 微信公众号 **关关不过**

本 fork 由 [@wiggins-kong](https://github.com/wiggins-kong) 维护，只负责上面差异表里的改动。
