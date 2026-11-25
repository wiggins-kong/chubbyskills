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
| 新机器部署 | `douyin-transcribe/scripts/bootstrap.py`：检查 → 补齐 → 用真实链接验真 |

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
