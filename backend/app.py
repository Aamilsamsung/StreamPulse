from importlib.util import find_spec
import math
import os
import re
import time
from tempfile import TemporaryDirectory
import uuid
import sqlite3
import subprocess
import threading
import shutil
import secrets
import logging
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, build_opener, HTTPRedirectHandler

from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Request as APIRequest
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from backend.highlights import rank_candidate, choose_nonoverlapping
from backend.editor import EVEN_DIMENSIONS, PRESETS, render, transcribe_words, whisper_model

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.getenv("STREAMPULSE_DATA_DIR", str(ROOT / "data"))).resolve()
DATA.mkdir(parents=True, exist_ok=True)
DB = DATA / "app.db"
KEY = os.getenv("ADMIN_KEY", "")
ALLOWED = {x.strip().lower() for x in os.getenv("ALLOWED_STREAM_HOSTS", "").split(",") if x.strip()}
app = FastAPI(title="StreamPulse AI", version="0.2.0")
stop_flags = {}
logger = logging.getLogger("streampulse")
LOCAL_INPUT = ["-protocol_whitelist", "file", "-format_whitelist", "mov,matroska,webm"]

def db():
    c = sqlite3.connect(DB, timeout=15)
    c.row_factory = sqlite3.Row
    return c

with db() as c:
    c.execute("CREATE TABLE IF NOT EXISTS streams(id TEXT PRIMARY KEY, name TEXT, url TEXT, status TEXT, created REAL, error TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS clips(id TEXT PRIMARY KEY, stream_id TEXT, created REAL, score REAL, reason TEXT, filename TEXT, transcript TEXT, status TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS uploads(id TEXT PRIMARY KEY, name TEXT, status TEXT, created REAL, error TEXT)")
    c.execute("UPDATE streams SET status='stopped' WHERE status='running'")
    columns = {row["name"] for row in c.execute("PRAGMA table_info(uploads)")}
    for name in ("stage", "warning"):
        if name not in columns:
            c.execute(f"ALTER TABLE uploads ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
    c.execute("UPDATE uploads SET status='interrupted',error='Server restarted; submit the video again' WHERE status='processing'")

def run(args, timeout=80):
    return subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)

class StreamInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    url: str = Field(min_length=10, max_length=2000)

def validate_url(url):
    p = urlparse(url)
    if p.scheme != "https" or not p.hostname or p.username or p.password:
        raise HTTPException(400, "HTTPS stream URL required")
    if not ALLOWED or p.hostname.lower() not in ALLOWED:
        raise HTTPException(400, "Host not allowed. Configure ALLOWED_STREAM_HOSTS for trusted HLS providers")
    if not p.path.lower().endswith(".m3u8"):
        raise HTTPException(400, "Provide an authorized HLS .m3u8 URL")

def whisper_available():
    return find_spec("faster_whisper") is not None


@app.middleware("http")
async def protect_api(request: APIRequest, call_next):
    path = request.url.path
    if not path.startswith("/api/") or path == "/api/health":
        return await call_next(request)
    # Reject unauthenticated requests before FastAPI reads multipart bodies.
    if not KEY or not secrets.compare_digest(request.headers.get("X-Admin-Key", "").encode(), KEY.encode()):
        response = JSONResponse({"detail": "Enter the configured StreamPulse admin key"}, status_code=401)
    else:
        response = await call_next(request)
    response.headers["Cache-Control"] = "private, no-store"
    return response


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "ffmpeg": bool(shutil.which("ffmpeg") and shutil.which("ffprobe")),
        "transcription": whisper_available(),
        "configured": bool(KEY and ALLOWED),
        "workspace_ready": bool(KEY),
    }


@app.get("/api/uploads")
def uploads():
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM uploads ORDER BY created DESC LIMIT 100")]

class ClipOptions(BaseModel):
    clip_count: int = Field(default_factory=lambda: int(os.getenv("MAX_CLIPS_PER_UPLOAD", "3")), ge=1, le=10)
    clip_seconds: int = Field(default=30, ge=5, le=120)
    preset: str = Field(default_factory=lambda: os.getenv("EDIT_PRESET", "clean").lower())
    captions: bool = Field(default_factory=lambda: os.getenv("BURN_SUBTITLES", "0") == "1")
    accent_captions: bool = False


def start_upload(name, topic, settings):
    if settings.preset not in PRESETS:
        raise HTTPException(400, "Choose Fit, Center crop or Blur background")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise HTTPException(503, "Install ffmpeg and ffprobe on the server")
    if topic and not whisper_available():
        raise HTTPException(503, "Install faster-whisper to search for spoken keywords")
    uid = uuid.uuid4().hex[:12]
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        if c.execute("SELECT 1 FROM uploads WHERE status='processing' LIMIT 1").fetchone():
            raise HTTPException(409, "One video is already processing; try again when it finishes")
        c.execute(
            "INSERT INTO uploads(id,name,status,created,error,stage) VALUES(?,?,?,?,?,?)",
            (uid, name[:120], "processing", time.time(), "", "Receiving video"),
        )
    return uid


def update_upload(uid, stage="", status="processing", error="", warning=""):
    with db() as c:
        c.execute("UPDATE uploads SET stage=?,status=?,error=?,warning=? WHERE id=?",
                  (stage, status, error[:300], warning, uid))


def probe_upload(path):
    result = run(["ffprobe", "-v", "error", *LOCAL_INPUT, "-select_streams", "v:0",
                  "-show_entries", "stream=codec_type:format=duration",
                  "-of", "default=noprint_wrappers=1", str(path)], 20)
    if result.returncode or "codec_type=video" not in result.stdout:
        raise ValueError("Invalid video file. Choose a playable MP4, MOV, MKV or WebM")
    match = re.search(r"^duration=([0-9.]+)$", result.stdout, re.MULTILINE)
    duration = float(match.group(1)) if match else 0
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Video has no readable duration")
    limit = int(os.getenv("MAX_VIDEO_SECONDS", "3600"))
    if duration > limit:
        raise ValueError(f"Video exceeds the {limit}-second processing limit")
    return duration


@app.post("/api/uploads")
async def upload_video(
    file: UploadFile = File(...), topic: str = Form(default="", max_length=160),
    clip_count: int = Form(default_factory=lambda: int(os.getenv("MAX_CLIPS_PER_UPLOAD", "3")), ge=1, le=10),
    clip_seconds: int = Form(default=30, ge=5, le=120),
    preset: str = Form(default_factory=lambda: os.getenv("EDIT_PRESET", "clean").lower()),
    captions: bool = Form(default_factory=lambda: os.getenv("BURN_SUBTITLES", "0") == "1"),
    accent_captions: bool = Form(default=False),
):
    settings = ClipOptions(clip_count=clip_count, clip_seconds=clip_seconds, preset=preset,
                           captions=captions, accent_captions=accent_captions)
    max_bytes = int(os.getenv("MAX_UPLOAD_MB", "500")) * 1024 * 1024
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in (".mp4", ".mov", ".mkv", ".webm"):
        raise HTTPException(400, "Supported formats: MP4, MOV, MKV, WebM")
    if file.size is not None and file.size > max_bytes:
        raise HTTPException(413, "Video exceeds upload size limit")
    uid = start_upload(Path(file.filename).name, topic.strip(), settings)
    path = DATA / "uploads" / (uid + suffix)
    size = 0
    try:
        path.parent.mkdir(exist_ok=True)
        with path.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(413, "Video exceeds upload size limit")
                out.write(chunk)
        if not size:
            raise HTTPException(400, "Empty video")
        try:
            duration = await run_in_threadpool(probe_upload, path)
        except (ValueError, subprocess.TimeoutExpired) as exc:
            raise HTTPException(400, str(exc) if isinstance(exc, ValueError) else "Video took too long to inspect") from exc
        threading.Thread(target=process_upload, args=(uid, path, topic.strip(), settings, duration), daemon=True).start()
    except HTTPException as exc:
        path.unlink(missing_ok=True)
        update_upload(uid, status="failed", error=exc.detail)
        raise
    except Exception:
        path.unlink(missing_ok=True)
        update_upload(uid, status="failed", error="Could not save or queue this video")
        logger.exception("Upload setup failed")
        raise HTTPException(500, "Could not save or queue this video")
    return {"id": uid, "status": "processing"}


def process_upload(uid, path, topic="", settings=None, duration=None):
    settings = settings or ClipOptions()
    samples = []
    outputs = []
    warnings = set()
    try:
        duration = duration if duration is not None else probe_upload(path)
        folder = DATA / "clips"
        folder.mkdir(exist_ok=True)
        window = settings.clip_seconds
        starts = list(range(0, math.ceil(duration), window))
        if len(starts) > 1 and duration - starts[-1] < window * 0.5:
            starts.pop()
        windows = []
        use_speech = bool(topic) or os.getenv("ENABLE_TRANSCRIPTION") == "1"
        for index, start in enumerate(starts, 1):
            update_upload(uid, f"Analyzing segment {index} of {len(starts)}")
            length = min(window, duration - start)
            words = []
            # Decode only audio while ranking, then extract video for selected clips.
            audio = path.parent / f"{uid}_{start}.wav"
            if use_speech:
                samples.append(audio)
                result = run(["ffmpeg", "-y", "-v", "error", "-nostdin", "-ss", str(start),
                              *LOCAL_INPUT, "-i", str(path), "-t", str(length), "-map", "0:a:0",
                              "-ac", "1", "-ar", "16000", str(audio)], 60)
                if result.returncode == 0:
                    try:
                        words = transcribe_words(audio, os.getenv("WHISPER_MODEL", "tiny"))
                    except Exception:
                        use_speech = False
                        warnings.add("Speech analysis unavailable. Highlight selection continued using audio levels.")
                        logger.exception("Speech analysis failed")
            transcript = " ".join(word["word"].strip() for word in words)[:3000]
            loudness = segment_loudness(path, start, length)
            score, reason = rank_candidate(loudness, transcript)
            if topic and transcript:
                topic_score, topic_reason = topic_rank(score, reason, transcript, topic)
                if topic_score:
                    score, reason = 100 + topic_score, topic_reason
            windows.append((score, start, length, reason, transcript, words))
        selected = choose_nonoverlapping(windows, settings.clip_count, window)
        if topic and not any(score > 100 for score, *_ in selected):
            warnings.add("No spoken keyword match found. Exported the best available segments.")
        if len(selected) < settings.clip_count:
            warnings.add(f"This recording has room for {len(selected)} clips at the selected duration.")
        rows = []
        captions_enabled = settings.captions
        for index, (score, start, length, reason, transcript, words) in enumerate(selected, 1):
            update_upload(uid, f"Preparing clip {index} of {len(selected)}")
            sample = path.parent / f"{uid}_{start}.mp4"
            samples.append(sample)
            result = run(["ffmpeg", "-y", "-v", "error", "-nostdin", "-threads", "2",
                          "-ss", str(start), *LOCAL_INPUT, "-i", str(path), "-t", str(length),
                          "-map", "0:v:0", "-map", "0:a:0?", "-vf", EVEN_DIMENSIONS,
                          "-c:v", "libx264", "-threads", "2",
                          "-pix_fmt", "yuv420p", "-preset", "ultrafast", "-crf", "21",
                          "-c:a", "aac", str(sample)], 150)
            if result.returncode:
                raise RuntimeError(f"Could not extract clip {index}: {result.stderr[-500:]}")
            cid = uuid.uuid4().hex[:12]
            dest = folder / (cid + ".mp4")
            outputs.append(dest)
            if captions_enabled and not words:
                update_upload(uid, f"Transcribing clip {index} of {len(selected)}")
                try:
                    if has_audio(sample):
                        words = transcribe_words(sample, os.getenv("WHISPER_MODEL", "tiny"))
                except Exception:
                    captions_enabled = False
                    warnings.add("Transcription unavailable. Remaining clips were exported without captions.")
                    logger.exception("Clip transcription failed")
            if captions_enabled and not words:
                warnings.add("No speech detected in one or more clips. Those clips were exported without captions.")
            if words and not transcript:
                transcript = " ".join(word["word"].strip() for word in words)[:3000]
            update_upload(uid, f"Rendering clip {index} of {len(selected)}")
            burn = settings.captions and bool(words)
            try:
                render(sample, dest, preset=settings.preset, subtitles=burn, words=words,
                       accent=settings.accent_captions)
            except (RuntimeError, ValueError):
                if not burn:
                    raise
                logger.exception("Caption rendering failed")
                warnings.add("Caption rendering failed for one or more clips. Uncaptioned MP4s were exported.")
                render(sample, dest, preset=settings.preset)
            rows.append((cid, uid, time.time(), score - 100 if score > 100 else score,
                         f"At {start}s: {reason}", dest.name, transcript, "review"))
        with db() as c:
            c.executemany("INSERT INTO clips VALUES(?,?,?,?,?,?,?,?)", rows)
            c.execute("UPDATE uploads SET status='completed',stage=?,warning=? WHERE id=?",
                      (f"{len(rows)} {'clip' if len(rows) == 1 else 'clips'} ready", " ".join(sorted(warnings)), uid))
    except Exception:
        logger.exception("Processing failed for upload %s", uid)
        for output in outputs:
            output.unlink(missing_ok=True)
        update_upload(uid, status="failed", error="Could not render clips. Retry with a shorter video or the Fit layout.")
    finally:
        for sample in samples:
            sample.unlink(missing_ok=True)
        path.unlink(missing_ok=True)


def has_audio(path):
    result = run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                  "stream=codec_type", "-of", "csv=p=0", str(path)], 15)
    return result.returncode == 0 and result.stdout.strip() == "audio"


def segment_loudness(path, start, length):
    try:
        result = run(["ffmpeg", "-hide_banner", "-nostdin", "-ss", str(start), "-t", str(length),
                      *LOCAL_INPUT, "-i", str(path), "-map", "0:a:0",
                      "-af", f"atrim=duration={length},volumedetect", "-vn", "-f", "null", "-"], 60)
        match = re.search(r"max_volume:\s*(-?[\d.]+) dB", result.stderr)
        return float(match.group(1)) if match else -90
    except subprocess.TimeoutExpired:
        return -90


class VideoURLInput(ClipOptions):
    url: str = Field(min_length=12, max_length=2000)
    topic: str = Field(default='', max_length=160)

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Redirects are disabled for remote video URLs")

def youtube_video_id(url):
    p = urlparse(url)
    if p.scheme != "https" or p.username or p.password or p.port not in (None, 443):
        return None
    host = (p.hostname or "").lower()
    if host in ("youtu.be", "www.youtu.be"):
        candidate = p.path.strip("/")
    elif host in ("youtube.com", "www.youtube.com", "m.youtube.com") and p.path == "/watch":
        candidate = parse_qs(p.query).get("v", [""])[0]
    elif host in ("youtube.com", "www.youtube.com", "m.youtube.com") and p.path.startswith(("/shorts/", "/live/")):
        candidate = p.path.split("/")[2]
    else:
        return None
    return candidate if re.fullmatch(r"[A-Za-z0-9_-]{11}", candidate) else None

def download_youtube_and_process(uid, video_id, topic="", settings=None):
    folder = DATA / "uploads"
    folder.mkdir(exist_ok=True)
    limit = int(os.getenv("MAX_UPLOAD_MB", "500")) * 1024 * 1024
    duration_limit = int(os.getenv("MAX_VIDEO_SECONDS", "3600"))
    try:
        update_upload(uid, "Importing YouTube video")
        with TemporaryDirectory(dir=folder, prefix=uid + "_") as temporary:
            path = Path(temporary) / "input.mp4"
            # The URL is reconstructed from a validated video ID.
            command = [
                "yt-dlp", "--no-playlist", "--no-config", "--no-cache-dir",
                "--js-runtimes", "node", "--socket-timeout", "12",
                "--retries", "1", "--extractor-retries", "1", "--fragment-retries", "1",
                "--max-filesize", str(limit), "--match-filter", f"!is_live & duration <= {duration_limit}",
                "--format", "b[ext=mp4][height<=480]/bv*[height<=480]+ba/b[height<=480]/best",
                "--merge-output-format", "mp4", "--remux-video", "mp4",
                "--output", str(path), "https://www.youtube.com/watch?v=" + video_id,
            ]
            result = run(command, timeout=180)
            if result.returncode or not path.is_file() or not path.stat().st_size:
                detail = result.stderr + "\n" + result.stdout
                logger.warning("YouTube import failed: %s", detail[-1200:])
                if "does not pass filter" in detail:
                    raise ValueError(f"Choose a finished recording no longer than {duration_limit} seconds, or upload a file")
                if any(term in detail.lower() for term in ("sign in", "private video", "not a bot", "members-only")):
                    raise ValueError("YouTube restricted this import. Upload a copy you are authorized to edit")
                if "timed out" in detail.lower():
                    raise TimeoutError("YouTube did not respond in time. Upload an authorized copy or try again")
                raise ValueError("YouTube did not provide a compatible video. Upload an authorized copy")
            if path.stat().st_size > limit:
                raise ValueError("YouTube video exceeds the server file limit")
            process_upload(uid, path, topic, settings, probe_upload(path))
    except subprocess.TimeoutExpired:
        update_upload(uid, status="failed", error="YouTube import timed out. Upload an authorized copy or try again")
    except Exception as exc:
        update_upload(uid, status="failed", error=str(exc))



def allowed_video_url(url):
    p = urlparse(url)
    hosts = {x.strip().lower() for x in os.getenv("ALLOWED_VIDEO_HOSTS", "").split(",") if x.strip()}
    if p.scheme != "https" or not p.hostname or p.username or p.password or p.port not in (None, 443):
        raise HTTPException(400, "Direct HTTPS video URL required")
    if p.hostname.lower() not in hosts:
        raise HTTPException(400, "Video host not allowlisted. Set ALLOWED_VIDEO_HOSTS in Render")
    if not p.path.lower().endswith((".mp4", ".mov", ".mkv", ".webm")):
        raise HTTPException(400, "Use a direct video file URL ending in MP4, MOV, MKV or WebM; page links are not supported yet")
    return Path(p.path).suffix.lower()

@app.post("/api/video-url")
def submit_video_url(item: VideoURLInput):
    try:
        video_id = youtube_video_id(item.url)
        suffix = ".mp4" if video_id else allowed_video_url(item.url)
    except ValueError as exc:
        raise HTTPException(400, "Enter a valid HTTPS video URL") from exc
    if video_id and not shutil.which("yt-dlp"):
        raise HTTPException(503, "Install yt-dlp to import YouTube URLs, or upload your video file")
    uid = start_upload(urlparse(item.url).hostname, item.topic.strip(), item)
    if video_id:
        target, args = download_youtube_and_process, (uid, video_id, item.topic.strip(), item)
    else:
        target, args = download_and_process, (uid, item.url, suffix, item.topic.strip(), item)
    threading.Thread(target=target, args=args, daemon=True).start()
    return {"id": uid, "status": "processing"}

def download_and_process(uid, url, suffix, topic='', settings=None):
    folder = DATA / "uploads"
    folder.mkdir(exist_ok=True)
    path = folder / (uid + suffix)
    limit = int(os.getenv("MAX_UPLOAD_MB", "100")) * 1024 * 1024
    try:
        update_upload(uid, "Downloading video")
        opener = build_opener(NoRedirect)
        request = Request(url, headers={"User-Agent": "StreamPulse/0.2"})
        with opener.open(request, timeout=25) as response:
            if response.status != 200:
                raise ValueError("Video server returned an unsuccessful status")
            if int(response.headers.get("Content-Length", "0")) > limit:
                raise ValueError("Video exceeds configured size limit")
            length = 0
            with path.open("wb") as output:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    length += len(chunk)
                    if length > limit:
                        raise ValueError("Video exceeds configured size limit")
                    output.write(chunk)
        if length == 0:
            raise ValueError("Remote video was empty")
        process_upload(uid, path, topic, settings, probe_upload(path))
    except Exception as exc:
        path.unlink(missing_ok=True)
        with db() as c:
            c.execute("UPDATE uploads SET status='failed',error=? WHERE id=?", (str(exc)[:250], uid))

@app.get("/api/streams")
def streams():
    with db() as c:
        return [dict(r) for r in c.execute("SELECT id,name,status,created,error FROM streams ORDER BY created DESC")]

@app.get("/api/clips")
def clips():
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM clips ORDER BY created DESC LIMIT 100")]

@app.post("/api/streams")
def create_stream(item: StreamInput):
    validate_url(item.url)
    with db() as c:
        active = c.execute("SELECT count(*) FROM streams WHERE status='running'").fetchone()[0]
        if active >= int(os.getenv("MAX_STREAMS", "1")):
            raise HTTPException(409, "Stream capacity reached")
        sid = uuid.uuid4().hex[:12]
        c.execute("INSERT INTO streams VALUES(?,?,?,?,?,?)", (sid, item.name, item.url, "running", time.time(), ""))
    event = threading.Event()
    stop_flags[sid] = event
    threading.Thread(target=worker, args=(sid, item.url, event), daemon=True).start()
    return {"id": sid, "status": "running"}

@app.post("/api/streams/{sid}/stop")
def stop(sid: str):
    if sid in stop_flags:
        stop_flags[sid].set()
    with db() as c:
        c.execute("UPDATE streams SET status='stopped' WHERE id=?", (sid,))
    return {"status": "stopping"}

@app.post("/api/clips/{cid}/approve")
def approve(cid: str):
    with db() as c:
        row = c.execute("SELECT id FROM clips WHERE id=?", (cid,)).fetchone()
        if not row:
            raise HTTPException(404, "Clip not found")
        c.execute("UPDATE clips SET status='approved' WHERE id=?", (cid,))
    return {"status": "approved", "note": "Publishing integration not yet enabled"}

@app.get("/api/clips/{cid}/video")
def video(cid: str):
    with db() as c:
        r = c.execute("SELECT filename FROM clips WHERE id=?", (cid,)).fetchone()
    if not r:
        raise HTTPException(404, "Clip not found")
    p = DATA / "clips" / Path(r["filename"]).name
    if not p.is_file():
        raise HTTPException(404, "Video unavailable")
    return FileResponse(p, media_type="video/mp4", filename=p.name)

def transcribe(path):
    if not whisper_available() or os.getenv("ENABLE_TRANSCRIPTION", "0") != "1":
        return ""
    try:
        segments, _ = whisper_model(os.getenv("WHISPER_MODEL", "tiny")).transcribe(
            str(path), beam_size=1, vad_filter=True
        )
        return " ".join(s.text.strip() for s in segments)[:3000]
    except Exception:
        return ""

def topic_rank(base_score, reason, transcript, topic):
    """Transparent lexical relevance ranking, not semantic understanding."""
    terms = set(re.findall(r"[a-z0-9]{3,}", topic.lower()))
    words = set(re.findall(r"[a-z0-9]{3,}", (transcript or "").lower()))
    if not terms:
        return 0, "Topic has no searchable keywords"
    overlap = terms & words
    relevance = round(100 * len(overlap) / len(terms))
    score = min(100, round(relevance * .85 + base_score * .15)) if overlap else 0
    return score, "Topic match: " + (", ".join(sorted(overlap)) if overlap else "none") + "; " + reason

def score_segment(path):
    try:
        r = run(["ffmpeg", "-hide_banner", "-i", str(path), "-af", "volumedetect", "-vn", "-f", "null", "-"], 35)
        match = re.search(r"max_volume:\s*(-?[\d.]+) dB", r.stderr)
        loud = float(match.group(1)) if match else -90
    except Exception:
        loud = -90
    transcript = transcribe(path)
    score, reason = rank_candidate(loud, transcript)
    return score, reason, transcript

def export_clip(src, dest):
    preset = os.getenv("EDIT_PRESET", "clean").lower()
    if preset not in PRESETS:
        raise ValueError("EDIT_PRESET must be clean, dynamic or blur")
    # Explicit opt-in: captions need faster-whisper and an FFmpeg build with libass.
    captions = os.getenv("BURN_SUBTITLES", "0") == "1"
    render(src, dest, preset=preset, subtitles=captions,
           model_name=os.getenv("WHISPER_MODEL", "tiny"))
    return True

def worker(sid, url, event):
    folder = DATA / "segments" / sid
    folder.mkdir(parents=True, exist_ok=True)
    clipsdir = DATA / "clips"
    clipsdir.mkdir(exist_ok=True)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", url, "-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "32", "-c:a", "aac", "-f", "segment", "-segment_time", "30", "-reset_timestamps", "1", str(folder / "seg_%05d.mp4")]
    proc = None
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        seen = set()
        while not event.wait(5):
            files = sorted(folder.glob("seg_*.mp4"))
            for f in files[:-1]:
                if f.name in seen:
                    continue
                seen.add(f.name)
                score, reason, transcript = score_segment(f)
                if score >= int(os.getenv("HIGHLIGHT_THRESHOLD", "42")):
                    cid = uuid.uuid4().hex[:12]
                    dest = clipsdir / (cid + ".mp4")
                    if export_clip(f, dest):
                        with db() as c:
                            c.execute("INSERT INTO clips VALUES(?,?,?,?,?,?,?,?)", (cid, sid, time.time(), score, reason, dest.name, transcript, "review"))
            for f in files[:-6]:
                try:
                    f.unlink()
                except OSError:
                    pass
            if proc.poll() is not None:
                raise RuntimeError("FFmpeg stopped: verify stream URL and availability")
    except Exception as exc:
        with db() as c:
            c.execute("UPDATE streams SET error=? WHERE id=?", (str(exc)[:250], sid))
    finally:
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        with db() as c:
            status = c.execute("SELECT status FROM streams WHERE id=?", (sid,)).fetchone()
            if status and status["status"] == "running":
                c.execute("UPDATE streams SET status='stopped',error='Stream ended or failed' WHERE id=?", (sid,))
        stop_flags.pop(sid, None)

app.mount("/", StaticFiles(directory=str(ROOT / "frontend"), html=True), name="frontend")
