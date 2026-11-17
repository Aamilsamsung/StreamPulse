# StreamPulse AI

**Open-source livestream and video highlight clipping — early local prototype.**

StreamPulse is being developed to help creators turn authorized livestreams and recorded videos into short vertical clips. The long-term goal is a public, multi-user service for gaming, podcasts, IRL, and other content, with optional subtitles and publishing to social platforms.

> **Status: local prototype, NOT production-ready.** The current highlight ranking is based primarily on audio volume. It does not reliably understand gameplay, humor, or conversational quality. No social account connection or automatic posting is implemented.

## Current capabilities

- Admin-key-protected local dashboard for initiating processing and reviewing clips.
- Accept local MP4, MOV, MKV, and WebM files up to a configurable size.
- Capture **authorized HTTPS HLS (.m3u8)** livestreams from an explicit host allowlist.
- Analyze ~30-second segments for loudness, with **optional** keyword-based transcript cues.
- Generate up to three vertical MP4 highlight candidates per upload.
- Review, approve and download clips. Approval currently **does not publish**.
- Store job metadata in local SQLite.

### Not implemented yet

- Public user accounts, multi-tenant isolation, quotas and production-safe media processing.
- YouTube/Twitch page URL ingestion or Instagram URL downloading.
- Intelligent vision/gameplay-based highlight detection or precise pre/post moment boundaries.
- Animated/burned-in subtitles, face tracking, smart cropping, branding and finishing effects.
- YouTube/Instagram OAuth, scheduling and automated publishing.
- Verified continuous 24/7 operation or public deployment.

## Architecture

```text
Browser dashboard
       |
       v
FastAPI (single ADMIN_KEY)
       |
       +-- SQLite metadata (local)
       +-- Upload processor: FFprobe -> 30s candidates -> score -> FFmpeg 9:16 export
       +-- HLS processor: FFmpeg segment capture -> score -> FFmpeg 9:16 export
       |
       v
Local clip review / download
```

**Current output uses letterboxing, not smart cropping.** It preserves source content but may leave padding.

## Prerequisites

- Python **3.10 or later**
- `ffmpeg` and `ffprobe` installed and on your PATH
- Sufficient local disk space and CPU/GPU capacity
- For livestream monitoring, a working HTTPS HLS .m3u8 URL you are authorized to record, on a trusted configured host

## Run locally

```bash
git clone https://github.com/Aamilsamsung/StreamPulse.git
cd StreamPulse
python -m venv .venv
```

Activate the environment:

- Windows PowerShell: `.venv\Scripts\Activate.ps1`
- macOS/Linux: `source .venv/bin/activate`

Install dependencies and configure an administrator key:

```bash
pip install -r requirements.txt
```

**macOS/Linux:**

```bash
export ADMIN_KEY='replace-with-a-long-random-secret'
export ALLOWED_STREAM_HOSTS='your-trusted-stream-host.example'
uvicorn backend.app:app --host 127.0.0.1 --port 8000
```

**Windows PowerShell:**

```powershell
$env:ADMIN_KEY="replace-with-a-long-random-secret"
$env:ALLOWED_STREAM_HOSTS="your-trusted-stream-host.example"
uvicorn backend.app:app --host 127.0.0.1 --port 8000
```

Then open **http://127.0.0.1:8000** in your browser and enter the same admin key.

`.env.example` is a reference only: the application **does not automatically load .env files**.

### Optional transcription

```bash
pip install faster-whisper
```

Set `ENABLE_TRANSCRIPTION=1` and optionally `WHISPER_MODEL=tiny`. The current implementation uses CPU inference and may need to download a model. Speech cues only help rank candidates; captions are **not** added to videos.

### Config

| Variable | Meaning | Default |
| --- | --- | --- |
| `ADMIN_KEY` | Required admin secret | unset (API locked) |
| `ALLOWED_STREAM_HOSTS` | Comma-separated trusted HLS hostnames | unset |
| `MAX_STREAMS` | Concurrent HLS streams | 1 |
| `MAX_UPLOAD_MB` | Maximum uploaded file size | 150 |
| `MAX_VIDEO_SECONDS` | Maximum uploaded duration | 1200 |
| `MAX_CLIPS_PER_UPLOAD` | Maximum generated clips per upload | 3 |
| `UPLOAD_HIGHLIGHT_THRESHOLD` | Upload candidate threshold | 20 |
| `HIGHLIGHT_THRESHOLD` | Live segment threshold | 42 |
| `ENABLE_TRANSCRIPTION` | Enable optional Whisper transcript cues | 0 |
| `WHISPER_MODEL` | Faster-Whisper model | tiny |

## Security and privacy

**Do not deploy this backend publicly as-is.** It uses one shared administrator key and does not offer user accounts, per-user job isolation, rate limiting, content abuse protection, robust SSRF protections for media ingestion, hardened sandboxing, storage quotas or secure publishing tokens. Even HLS hostname allowlisting is not a sufficient SSRF boundary in a public service.

Uploaded files are processed locally and intended to be deleted after processing; generated clips and SQLite metadata persist in `data/`. Keep that directory private and back it up intentionally. Never commit `data/`, credentials, or video files.

Only capture, transform and republish media that you own or have the necessary permission to use. Respect platform terms and copyright.

## Verification and known limitations

- The initial source was assembled for a local prototype.
- Basic Python compile/API checks were reported in development; **live-stream ingestion and full video processing remain unverified end-to-end**.
- Clip quality, performance and the earlier 1–2 minute turnaround target are **not benchmarks or guarantees**.
- In-memory worker threads are not crash-proof or suitable for a production job queue.

## Public beta roadmap

1. Add automated synthetic-media and real-world integration tests.
2. Add robust highlight ranking using audio, transcripts, scenes and optional game-specific signals.
3. Add burned-in word-level subtitles, smart composition, deduplication and editing presets.
4. Add secure login, per-user ownership, quotas, processing queues and restricted worker egress.
5. Add account linking and publishing through official YouTube/Meta APIs, with a review-first setting.
6. Deploy a separately hosted public frontend and scalable media workers; validate cost and observability.

## Contributing

Issues and pull requests are welcome for bug fixes, testing, documentation and improvements. Please avoid opening PRs that silently add scraping of restricted content or weaken public-upload security.

## Project status

**Prototype / work in progress.** This README deliberately distinguishes implemented functionality from future plans.

## Editing engine (experimental)

The clip exporter now supports `EDIT_PRESET=clean` (letterbox), `EDIT_PRESET=dynamic` (center-crop landscape video), and `EDIT_PRESET=blur` (blurred background). These are **fixed layouts, not AI subject tracking**. Audio loudness normalization is applied on export.

Set `BURN_SUBTITLES=1` to opt into word-timed captions. Install `faster-whisper`, use an FFmpeg build with the `ass` filter/libass, and allow the Whisper model to download. **This feature has not yet been verified with a real video in CI**. The `blur` preset cannot currently be combined with burned subtitles. Subtitle generation fails explicitly when speech or dependencies are unavailable.

`backend/highlights.py` contains an explainable audio/transcript scoring function and candidate overlap reduction. These are heuristics, not a trained AI ranking model.

## Real-video integration test

A separate GitHub Actions workflow (`.github/workflows/video-integration.yml`) now creates a **synthetic test video** with FFmpeg and attempts to render all three editing presets into 1080×1920 MP4 files. This is a real media export test, not just a Python import check. It does **not** test live streaming, Whisper caption rendering, platform publishing, public traffic or gameplay-specific highlight quality.

Run locally with FFmpeg installed:

```bash
python -m unittest discover -s tests -p 'test_video_integration.py' -v
```

The CI workflow must report success before treating the tested preset exports as verified. The synthetic clip contains no third-party copyrighted footage.

## Clip a video today — fastest route (Windows)

The repository includes a **local clipper CLI** and `clip-now.bat`. This path does **not** require starting the FastAPI dashboard, creating an admin key, connecting a social account, or deploying a server.

1. Install [Python 3.10+](https://www.python.org/downloads/) and [FFmpeg](https://ffmpeg.org/download.html). Make sure `ffmpeg` and `ffprobe` work in a new terminal. On Windows, `winget install Gyan.FFmpeg` is one possible installation method.
2. Download/clone this repository. Double-click `clip-now.bat` with a video file dragged onto it. It will set up a local Python environment if needed, generate up to three candidate clips, and open the `output_clips` folder.
3. For manual control, run:

```powershell
python -m pip install -r requirements.txt
python -m backend.cli "C:\\Videos\\my-video.mp4" --clips 3 --preset dynamic --output output_clips
```

Optional AI speech ranking and word-timed captions:

```powershell
python -m pip install faster-whisper
python -m backend.cli "C:\\Videos\\my-video.mp4" --clips 3 --preset clean --transcribe --subtitles
```

The first transcription run may download the Whisper model. Your FFmpeg build must support the `ass` subtitle filter. `--preset clean` is recommended with captions. The `blur` preset and captions cannot be combined yet.

### What this local clipper really does

- Analyzes fixed windows (30 seconds by default) for audio loudness and optionally speech keywords.
- Ranks candidates and avoids picking near-duplicate moments.
- Produces up to three 1080×1920 MP4 exports and a `highlights.json` manifest.
- Offers fixed framing presets and optional word-timed captions.

It is **not yet a semantic AI video editor**: there is no game-event recognition, scene-level context analysis, subject tracking, timeline editor, automatic publishing, or guaranteed viral-score prediction. Loud audio can be mistaken for an important moment. Review clips before sharing.

### CLI options

`--clips 3`, `--window 30`, `--preset clean|dynamic|blur`, `--transcribe`, `--subtitles`, `--model tiny`, `--max-minutes 60`, `--output folder`.

The local CLI is designed for videos you own or are authorized to process. It does not download restricted social-media URLs. The CI integration test creates a synthetic input and verifies that the CLI exports a clip and JSON manifest.
