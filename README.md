# StreamPulse AI

StreamPulse takes an authorized recording and exports vertical MP4 highlights. It uses audio peaks and, when enabled, spoken cues to rank segments. Set the clip count, length, framing, and caption style in the browser. Preview clips and download them from the same workspace.

## Start on Windows

Install Python 3.10+ and put FFmpeg and FFprobe on your PATH. In PowerShell, open the repository folder and run:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-ai.txt
$env:ADMIN_KEY = Read-Host "Choose a private StreamPulse workspace key"
.\.venv\Scripts\python.exe -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
```

Open [127.0.0.1:8000](http://127.0.0.1:8000), choose **Connect** and enter that same key. Upload an MP4, MOV, MKV or WebM file. Choose **White, word by word** or **Yellow pop animation**, then select **Generate highlights**. When your clips are ready, select **Preview** or **Download MP4**.

The first captioned clip needs internet access to download the Faster-Whisper tiny model. FFmpeg must include the `ass` filter. For a job with no speech or an unavailable caption model, the dashboard explains the fallback and still exports clips. Uploaded originals are removed after processing. Downloaded clips stay on your computer, while server-side copies and job metadata are kept under `data/` by default.

### macOS and Linux

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-ai.txt
read -rsp "Choose a private StreamPulse workspace key: " ADMIN_KEY
export ADMIN_KEY
.venv/bin/python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
```

### Windows without the website

Drag a recording onto `clip-with-subtitles.bat` for captioned clips, or onto `clip-now.bat` for clips without captions. Both write MP4s into `output_clips`. For manual control, run:

```powershell
.\.venv\Scripts\python.exe -m backend.cli "C:\Videos\my-video.mp4" --clips 3 --window 20 --preset blur --transcribe --subtitles
```

## Sources and controls

- **Upload** handles recordings and authorized copies of YouTube or Instagram videos. Select up to 10 clips and 5–120 seconds per clip. Short videos can yield fewer or shorter clips.
- **Video URL** accepts direct HTTPS links to media on hosts in `ALLOWED_VIDEO_HOSTS`. YouTube watch and Shorts links use optional `yt-dlp[default]` and Node.js. Availability depends on the source and host, so upload an authorized copy if YouTube blocks extraction. Instagram page links require an authorized platform integration.
- **Livestream** retains experimental recording from explicitly allowed HTTPS HLS sources in `ALLOWED_STREAM_HOSTS`. Live monitoring uses its existing environment-driven settings.
- **Framing** offers full-frame fit, landscape center crop, and a blurred background. Each exports 1080×1920 H.264 MP4 with normalized audio when the source contains audio.
- **Highlight selection** uses audio peaks by default. `ENABLE_TRANSCRIPTION=1` also looks for speech cues during ranking. Entering optional spoken keywords enables transcript analysis for that job. The highest-ranking non-overlapping segments are exported even if the audio is quiet or the transcript is unavailable.

The app runs one uploaded-video job at a time. `MAX_UPLOAD_MB` and `MAX_VIDEO_SECONDS` default to 500 MB and 3600 seconds for local uploads respectively. Use `WHISPER_MODEL` to choose the speech model. `STREAMPULSE_DATA_DIR` overrides the storage path. The `.env.example` file lists settings and is a reference for configuring the process environment.

## Tests

With FFmpeg and FFprobe on PATH:

```bash
python -m pip install -r requirements-dev.txt -r requirements-ai.txt
TEST_WHISPER=1 python -m unittest discover -s tests -v
```

The speech test needs FFmpeg's `flite` and `ass` filters and access to the tiny model. It uses generated speech, transcribes it, downloads the processed MP4 and checks that caption pixels appear during a recognized word and disappear in a later silent frame. The other integration tests exercise four real upload formats, multiple clips, invalid-file handling, authentication, MP4 decoding and the framing presets.

## Hosted use and remaining work

The Docker image includes FFmpeg, Faster-Whisper, and the optional YouTube importer. Configure the existing private `ADMIN_KEY` and run one Uvicorn worker. Use a private workspace and trust the people you give its key to. Render free instances can sleep and discard local files on restart. Download clips before leaving. Durable queues, per-user isolation and persistent object storage are needed for a public multi-user launch. See [SECURITY.md](SECURITY.md).

YouTube and Instagram publishing still need official OAuth apps, platform permissions, account linking, and a verified upload. Instagram page import, automatic subject tracking, and visual or game-event recognition remain open. Fixed framing and MP4 downloads are available without linking any social account.
