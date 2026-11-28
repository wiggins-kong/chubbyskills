**中文** · [English](./README.en.md)

# chubbyskills（fork）

把中文平台内容采集进本地知识库的 Agent Skills。本仓库是
[chubbyguan/chubbyskills](https://github.com/chubbyguan/chubbyskills) 的 fork：
上游的能力、安装说明、平台状态和文档都以原仓库为准，这里**只记录本 fork 改了什么**。

## 与上游的差异

上游是完整的 14 个技能加一个 CLI。本 fork 目前只改抓取层，表里没有的都与上游一致：

| 模块 | 改动 |
|---|---|
| 抖音抓取 | 只走 CDP：独立 profile 的无头 Chromium 抓 `aweme/detail`，不再每次尝试分享页和 yt-dlp |
| 抖音图文 | 支持 `/note/`、`/slides/`：原文 + 图片本地化到 `<标题>.assets/`，不再报「不是视频」 |
| 桌面 CDP 兜底 | `chubby_common/cdp.py` 统一用桌面端 `Edge`/`Chrome` + `websocket-client` 与每站点独立本地 profile；无头被拦时弹有头窗口等待登录/验证（180 秒超时），覆盖知乎图文与各平台 Cookie/DOM 回退 |
| 14 合 1 技能包 | `install_skill.py --bundle` 将 14 个技能合并安装为单一 `chubby` 技能目录（仅 1 个根 `SKILL.md` + `references/*.md` + `tools/chubby_ingest.py`） |
| 新机器部署 | `douyin-transcribe/scripts/bootstrap.py`：检查 → 补齐 → 用真实链接验真 |

## 14 合 1 统一技能包安装

```bash
git clone https://github.com/wiggins-kong/chubbyskills.git
cd chubbyskills
python3 tools/install_skill.py --bundle --dest <你的 agent skills 目录>
# 可选：--bundle-name <自定义名称>；--clean-legacy 移除旧 14 个独立技能目录（自动保留其中的 .venv / venv）
```

安装后生成单个 `chubby/` 目录（只有一个 `SKILL.md` 入口），Agent 通过 `tools/chubby_ingest.py "<source>"` 自动识别平台并采集，需要摘要加工、知识库管理、学习笔记或情报雷达时再按需读取 `references/*.md`。单技能安装模式（`python3 tools/install_skill.py <skill> --dest ...`）保持兼容，并会为所有依赖 `chubby_common` 的技能自动打入 `chubby_common/`。

## 桌面端 Chromium CDP 兜底

`chubby_common/cdp.py` 仅在桌面端运行，使用本机 `Edge` / `Chrome` / `Chromium`（109+）+ `websocket-client`，按站点隔离保存本地 profile（不读取你日常浏览器的 Cookie）。当 HTTP 或 `yt-dlp` 触发登录墙或风控时，会先用无头模式检查本地登录态，未通过则弹出同一 profile 的有头浏览器窗口等待完成登录/验证（默认 180 秒超时）。浏览器不在默认路径时用 `BROWSER_BIN` 指定。

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
