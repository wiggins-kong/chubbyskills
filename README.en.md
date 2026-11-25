**English** · [中文](./README.md)

# chubbyskills (fork)

Agent Skills that capture Chinese-platform content into a local knowledge base. This repository is a fork of
[chubbyguan/chubbyskills](https://github.com/chubbyguan/chubbyskills): upstream's capabilities, installation,
platform status and documentation are the source of truth. This README **only records what the fork changes**.

## Differences from upstream

Upstream ships 14 skills plus a CLI. This fork only changes the capture layer; anything not listed here is
identical to upstream:

| Module | Change |
|---|---|
| Douyin capture | CDP only: a headless Chromium with its own profile reads `aweme/detail`, so the share page and yt-dlp are no longer retried on every run |
| Douyin image posts | `/note/` and `/slides/` links work: text plus images localized into `<title>.assets/`, instead of failing as "not a video" |
| New machine setup | `douyin-transcribe/scripts/bootstrap.py`: check, install, verify with a real link |

## Douyin: start on a new machine

```bash
git clone https://github.com/wiggins-kong/chubbyskills.git
cd chubbyskills
python3 tools/install_skill.py douyin-transcribe --dest <your agent skills directory>
```

From the installed skill directory, let the script tell you what is missing:

```bash
python3 scripts/bootstrap.py                  # per-item check, prints install commands
python3 scripts/bootstrap.py --install        # installs websocket-client only
python3 scripts/bootstrap.py --probe "<a real Douyin link>"   # one live fetch
```

Capture needs Python 3.9+, `websocket-client` and Edge/Chrome/Chromium 109+. Video transcription additionally
needs `ffmpeg` and `funasr`; image posts do not. Then:

```bash
python3 scripts/transcribe.py "<Douyin link or whole share blurb>" ./out
```

Set `BROWSER_BIN` when the browser is not in a default location. Details, failure handling and output format:
[douyin-transcribe/SKILL.md](./douyin-transcribe/SKILL.md).

## Upstream documentation

General capabilities live in the original repository:

- Upstream README: [中文](https://github.com/chubbyguan/chubbyskills/blob/main/README.md) · [English](https://github.com/chubbyguan/chubbyskills/blob/main/README.en.md)
- [Installation](https://github.com/chubbyguan/chubbyskills/blob/main/docs/installation.md) · [Platform status](https://github.com/chubbyguan/chubbyskills/blob/main/docs/platform-status.md) · [Subscriptions](https://github.com/chubbyguan/chubbyskills/blob/main/docs/subscriptions.md) · [MCP](https://github.com/chubbyguan/chubbyskills/blob/main/docs/mcp-workflow.md)
- [Releases](https://github.com/chubbyguan/chubbyskills/releases) · [Changelog](https://github.com/chubbyguan/chubbyskills/blob/main/CHANGELOG.md)

The local `docs/` and `CHANGELOG.md` follow upstream; only the parts named in the table above differ.

## Related projects

- [chubby-recruitment-ops](https://github.com/chubbyguan/chubby-recruitment-ops): a recruiting operations plugin for Codex and Claude Code, by the same author.

## Usage limits

Capture skills are for personal study and research. Follow the source platform's terms, `robots.txt` and applicable
law; only save content you have the right to access. Do not use them for bulk scraping, commercial crawling,
redistribution or anything that infringes on others' rights. Original content remains the property of its authors.

Code is released under the [MIT License](./LICENSE), as is. The code license grants no rights to third-party content.

## About Chubby (upstream author)

The design, skills and documentation come from Chubby:
[X / Twitter](https://x.com/Chubbyguan) · [Jike](https://web.okjike.com/u/a876838d-d9a8-494b-9494-bb3410b77dd5) · [Xiaohongshu](https://www.xiaohongshu.com/user/profile/57c061626a6a696f5a70f9a8) · WeChat official account **关关不过**

This fork is maintained by [@wiggins-kong](https://github.com/wiggins-kong) and only covers the changes listed above.
