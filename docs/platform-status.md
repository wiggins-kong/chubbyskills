# Platform Status

Generated at: `2026-11-25T23:56:29+08:00`

This page is generated from `platforms/*.yaml` and `templates/sites/*.yaml`.
Default checks validate repository structure and template coverage. Run with `--local` to include local dependency readiness.
The status labels below are declared capabilities, not recent live success measurements.
For dated real-source evidence see [live verification](live-verification.md). Untested platforms remain unverified.

Local dependency check: `off`

| platform | skill | status | content | failure modes | fallback | template | notes |
|---|---|---|---|---|---|---|---|
| Bilibili | bilibili-transcribe | stable | video, subtitle | missing_yt_dlp, no_subtitle, ffmpeg_or_funasr_missing, region_or_login_limit | subtitle-first then audio transcription | yes | Subtitle-first path keeps the default install light. |
| Douyin | douyin-transcribe | heavy deps | video, note | expired_short_link, anti_bot, browser_unavailable, cdp_timeout, rate_limited, ffmpeg_or_funasr_missing, download_blocked | audio transcription | yes | Capture is CDP-only: a headless Chromium in its own profile opens the iesdouyin share page and reads the aweme/detail response the page itself requests, with no cookies and without touching the user's browser. The share page now returns only an anti-bot shell and cookie-less yt-dlp gets 403, so both old paths were removed. Image posts (non-empty images) save images + desc instead of transcribing. Run douyin-transcribe/scripts/bootstrap.py when websocket-client or a browser is missing. |
| Podcast | podcast-transcribe | heavy deps | audio, rss, local_file | rss_unreachable, audio_download_failed, ffmpeg_missing, funasr_missing, long_audio_timeout | local audio file | yes | Long-form audio is slow but stable when local dependencies are ready. Optional synchronous cloud providers: dashscope (qwen3-asr-flash, 10MB/5min) and groq (whisper-large-v3-turbo, 25MB free tier). |
| TikTok | tiktok-transcribe | beta | video | region_limit, anti_bot, missing_yt_dlp, ffmpeg_or_funasr_missing, download_blocked | audio transcription after yt-dlp download | yes | Region limits and anti-bot behavior may affect downloads. |
| WeChat Official Account | wechat-article-ingest | beta | article, pdf | wechat_client_only, html_structure_changed, beautifulsoup_missing, pdf_parser_missing, content_too_short | pdf or saved HTML | yes | HTML structure changes frequently; PDF fallback keeps useful coverage. |
| Weibo | weibo-transcribe | beta | video | mobile_link_required, anti_bot, missing_yt_dlp, ffmpeg_or_funasr_missing, download_blocked | audio transcription after yt-dlp download | yes | Mobile links are usually more reliable than desktop links. |
| X Twitter | x-ingest | manual fallback | text, image, video | syndication_unavailable, protected_or_deleted_post, media_download_failed, ffmpeg_or_funasr_missing, article_preview_only | manual text via --fallback-text or structured tweet data via --fallback-json | yes | Embed endpoint works for many public posts; X Articles return preview only — use --cookies (logged-in GraphQL) or --fallback-text for full text; text and structured JSON fallbacks protect capture flow. |
| Xiaohongshu | xiaohongshu-ingest | manual fallback | text, image, video | missing_cookie, anti_bot, html_structure_changed, image_download_failed, ffmpeg_or_funasr_missing | manual text via --fallback-text | yes | Cookie improves success rate; fallback text keeps the knowledge pipeline moving. |
| YouTube | youtube-transcribe | stable | video, subtitle | missing_yt_dlp, no_caption, age_or_region_limit, ffmpeg_or_funasr_missing | subtitle-first then audio transcription | yes | Captions provide the fastest path; audio transcription remains available. |
| Zhihu | zhihu-transcribe | beta | video | embedded_video_unavailable, anti_bot, missing_yt_dlp, ffmpeg_or_funasr_missing, download_blocked | audio transcription after yt-dlp download | yes | Supports standalone video links and embedded answer/article videos. |

## Status Meaning

- `stable`: maintained text/subtitle-first path; this label alone does not prove a recent successful capture.
- `beta`: experimental support; platform markup or anti-bot behavior may change.
- `heavy deps`: requires local audio/video transcription dependencies.
- `manual fallback`: supports manual text fallback when live fetch fails.
- `blocked` / `degraded`: local dependency check found missing required or optional dependencies.
- `broken`: repository definition, script, or template validation failed.

## Commands

```bash
python3 tools/platform_health.py --check
python3 tools/platform_health.py --local --check
python3 tools/platform_health.py --json
```
