import os
import re
import time
import uuid
import sqlite3
import subprocess
import threading
import shutil
import secrets
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError

from fastapi import FastAPI, HTTPException, Header, Depends, UploadFile, File, Form
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from backend.highlights import rank_candidate, choose_nonoverlapping
from backend.editor import render, PRESETS

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
DB = DATA / "app.db"
KEY = os.getenv("ADMIN_KEY", "")
ALLOWED = {x.strip().lower() for x in os.getenv("ALLOWED_STREAM_HOSTS", "").split(",") if x.strip()}
app = FastAPI(title="StreamPulse AI", version="0.2.0")
stop_flags = {}

def db():
    c = sqlite3.connect(DB, timeout=15)
    c.row_factory = sqlite3.Row
    return c

with db() as c:
    c.execute("CREATE TABLE IF NOT EXISTS streams(id TEXT PRIMARY KEY, name TEXT, url TEXT, status TEXT, created REAL, error TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS clips(id TEXT PRIMARY KEY, stream_id TEXT, created REAL, score REAL, reason TEXT, filename TEXT, transcript TEXT, status TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS uploads(id TEXT PRIMARY KEY, name TEXT, status TEXT, created REAL, error TEXT)")
    c.execute("UPDATE streams SET status='stopped' WHERE status='running'")
    c.execute("UPDATE uploads SET status='interrupted' WHERE status='processing'")

def auth(x_admin_key: str | None = Header(default=None)):
    if not KEY or not secrets.compare_digest(x_admin_key or "", KEY):
        raise HTTPException(401, "Set ADMIN_KEY and supply X-Admin-Key")

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
    try:
        import faster_whisper
        return True
    except ImportError:
        return False

@app.get("/api/health")
def health():
    return {"ok": True, "ffmpeg": bool(shutil.which("ffmpeg")), "transcription": whisper_available(), "configured": bool(KEY and ALLOWED)}

@app.get("/api/uploads")
def uploads(_: None = Depends(auth)):
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM uploads ORDER BY created DESC LIMIT 100")]

@app.post("/api/uploads")
async def upload_video(file: UploadFile = File(...), topic: str = Form(default='', max_length=160), _: None = Depends(auth)):
    topic = topic.strip()
    if topic and not (os.getenv('ENABLE_TRANSCRIPTION') == '1' and whisper_available()):
        raise HTTPException(503, 'Topic-based clipping requires ENABLE_TRANSCRIPTION=1 and faster-whisper installed on the server')
    max_bytes = int(os.getenv("MAX_UPLOAD_MB", "500")) * 1024 * 1024
    if not file.filename or Path(file.filename).suffix.lower() not in (".mp4", ".mov", ".mkv", ".webm"):
        raise HTTPException(400, "Supported formats: MP4, MOV, MKV, WebM")
    with db() as c:
        if c.execute("SELECT count(*) FROM uploads WHERE status='processing'").fetchone()[0] >= 1:
            raise HTTPException(409, "Processing capacity reached; try again later")
        uid = uuid.uuid4().hex[:12]
        c.execute("INSERT INTO uploads VALUES(?,?,?,?,?)", (uid, Path(file.filename).name[:120], "processing", time.time(), ""))
    folder = DATA / "uploads"
    folder.mkdir(exist_ok=True)
    path = folder / (uid + Path(file.filename).suffix.lower())
    size = 0
    try:
        with path.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(413, "Video exceeds upload size limit")
                out.write(chunk)
        if size == 0:
            raise HTTPException(400, "Empty video")
        probe = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_type", "-of", "default=noprint_wrappers=1", str(path)], 20)
        if probe.returncode != 0 or "video" not in probe.stdout:
            raise HTTPException(400, "Invalid video file")
    except Exception as exc:
        path.unlink(missing_ok=True)
        with db() as c:
            c.execute("UPDATE uploads SET status='failed',error=? WHERE id=?", (str(exc)[:250], uid))
        raise
    threading.Thread(target=process_upload, args=(uid, path, topic), daemon=True).start()
    return {"id": uid, "status": "processing"}

def process_upload(uid, path, topic=''):
    samples = []
    try:
        result = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)], 20)
        duration = float(result.stdout.strip())
        if duration <= 0 or duration > int(os.getenv("MAX_VIDEO_SECONDS", "3600")):
            raise ValueError("Video duration exceeds beta limit")
        folder = DATA / "clips"
        folder.mkdir(exist_ok=True)
        windows = []
        for start in range(0, int(duration), 30):
            if duration - start < 8:
                break
            sample = DATA / "uploads" / (uid + "_" + str(start) + ".mp4")
            r = run(["ffmpeg", "-y", "-v", "error", "-ss", str(start), "-i", str(path), "-t", str(min(30, duration - start)), "-c:v", "libx264", "-preset", "ultrafast", "-crf", "32", "-c:a", "aac", str(sample)], 100)
            if r.returncode == 0 and sample.exists():
                samples.append(sample)
                score, reason, transcript = score_segment(sample)
                if topic:
                    score, reason = topic_rank(score, reason, transcript, topic)
                windows.append((score, start, sample, reason, transcript))
        selected = choose_nonoverlapping(windows, int(os.getenv("MAX_CLIPS_PER_UPLOAD", "3")), 25)
        for score, start, sample, reason, transcript in selected:
            if score < (15 if topic else int(os.getenv("UPLOAD_HIGHLIGHT_THRESHOLD", "20"))):
                continue
            cid = uuid.uuid4().hex[:12]
            dest = folder / (cid + ".mp4")
            if export_clip(sample, dest):
                with db() as c:
                    c.execute("INSERT INTO clips VALUES(?,?,?,?,?,?,?,?)", (cid, uid, time.time(), score, f"At {start}s: {reason}", dest.name, transcript, "review"))
        with db() as c:
            c.execute("UPDATE uploads SET status='completed' WHERE id=?", (uid,))
    except Exception as exc:
        with db() as c:
            c.execute("UPDATE uploads SET status='failed',error=? WHERE id=?", (str(exc)[:250], uid))
    finally:
        for sample in samples:
            sample.unlink(missing_ok=True)
        path.unlink(missing_ok=True)


class VideoURLInput(BaseModel):
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

def download_youtube_and_process(uid, video_id, topic=""):
    folder = DATA / "uploads"
    folder.mkdir(exist_ok=True)
    path = folder / (uid + ".mp4")
    limit = int(os.getenv("MAX_UPLOAD_MB", "500")) * 1024 * 1024
    duration_limit = int(os.getenv("MAX_VIDEO_SECONDS", "3600"))
    try:
        if not shutil.which("yt-dlp"):
            raise RuntimeError("YouTube importer is unavailable on this server")
        # The URL is reconstructed from a validated 11-character video ID.
        cmd = ["yt-dlp", "--no-playlist", "--no-config", "--no-cache-dir",
               "--js-runtimes", "node:/usr/local/bin/node", "--socket-timeout", "25", "--retries", "3", "--fragment-retries", "3",
               "--max-filesize", str(limit), "--match-filter", f"duration <= {duration_limit}",
               "--format", "b[ext=mp4][height<=480]/bv*[height<=480]+ba/b[height<=480]/best",
               "--merge-output-format", "mp4", "--remux-video", "mp4",
               "--output", str(path), "https://www.youtube.com/watch?v=" + video_id]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, timeout=900)
        if result.returncode != 0 or not path.exists():
            detail = (result.stderr or result.stdout or "").strip()
            lines = [line.strip() for line in detail.splitlines() if line.strip()]
            last = next((line for line in reversed(lines) if "ERROR:" in line or "does not pass filter" in line), lines[-1] if lines else "No downloader details")
            if "does not pass filter" in detail or "duration" in last.lower() and "filter" in last.lower():
                raise ValueError(f"Video exceeds the {duration_limit // 60}-minute processing limit")
            if "Sign in to confirm" in detail or "bot" in last.lower() or "Failed to extract any player response" in detail:
                raise ValueError("YouTube extraction blocked or changed; importer could not retrieve this video. Upload an authorized video file instead.")
            if "Requested format is not available" in detail:
                raise ValueError("YouTube did not offer a compatible MP4 stream for this video")
            raise ValueError("YouTube importer: " + last[:180])
        if path.stat().st_size > limit:
            raise ValueError("YouTube video exceeds the server file limit")
        process_upload(uid, path, topic)
    except Exception as exc:
        path.unlink(missing_ok=True)
        with db() as c:
            c.execute("UPDATE uploads SET status='failed',error=? WHERE id=?", (str(exc)[:250], uid))

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
def submit_video_url(item: VideoURLInput, _: None = Depends(auth)):
    video_id = youtube_video_id(item.url)
    suffix = '.mp4' if video_id else allowed_video_url(item.url)
    if item.topic.strip() and not (os.getenv('ENABLE_TRANSCRIPTION') == '1' and whisper_available()):
        raise HTTPException(503, 'Topic-based clipping requires ENABLE_TRANSCRIPTION=1 and faster-whisper installed on the server')
    with db() as c:
        if c.execute("SELECT count(*) FROM uploads WHERE status='processing'").fetchone()[0] >= 1:
            raise HTTPException(409, "Processing capacity reached")
        uid = uuid.uuid4().hex[:12]
        c.execute("INSERT INTO uploads VALUES(?,?,?,?,?)", (uid, urlparse(item.url).hostname[:120], "processing", time.time(), ""))
    if video_id:
        threading.Thread(target=download_youtube_and_process, args=(uid, video_id, item.topic.strip()), daemon=True).start()
    else:
        threading.Thread(target=download_and_process, args=(uid, item.url, suffix, item.topic.strip()), daemon=True).start()
    return {"id": uid, "status": "processing"}

def download_and_process(uid, url, suffix, topic=''):
    folder = DATA / "uploads"
    folder.mkdir(exist_ok=True)
    path = folder / (uid + suffix)
    limit = int(os.getenv("MAX_UPLOAD_MB", "100")) * 1024 * 1024
    try:
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
        probe = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_type", "-of", "default=noprint_wrappers=1", str(path)], 20)
        if probe.returncode != 0 or "video" not in probe.stdout:
            raise ValueError("URL did not return a playable video")
        process_upload(uid, path, topic)
    except Exception as exc:
        path.unlink(missing_ok=True)
        with db() as c:
            c.execute("UPDATE uploads SET status='failed',error=? WHERE id=?", (str(exc)[:250], uid))

@app.get("/api/streams")
def streams(_: None = Depends(auth)):
    with db() as c:
        return [dict(r) for r in c.execute("SELECT id,name,status,created,error FROM streams ORDER BY created DESC")]

@app.get("/api/clips")
def clips(_: None = Depends(auth)):
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM clips ORDER BY created DESC LIMIT 100")]

@app.post("/api/streams")
def create_stream(item: StreamInput, _: None = Depends(auth)):
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
def stop(sid: str, _: None = Depends(auth)):
    if sid in stop_flags:
        stop_flags[sid].set()
    with db() as c:
        c.execute("UPDATE streams SET status='stopped' WHERE id=?", (sid,))
    return {"status": "stopping"}

@app.post("/api/clips/{cid}/approve")
def approve(cid: str, _: None = Depends(auth)):
    with db() as c:
        row = c.execute("SELECT id FROM clips WHERE id=?", (cid,)).fetchone()
        if not row:
            raise HTTPException(404, "Clip not found")
        c.execute("UPDATE clips SET status='approved' WHERE id=?", (cid,))
    return {"status": "approved", "note": "Publishing integration not yet enabled"}

@app.get("/api/clips/{cid}/video")
def video(cid: str, _: None = Depends(auth)):
    with db() as c:
        r = c.execute("SELECT filename FROM clips WHERE id=?", (cid,)).fetchone()
    if not r:
        raise HTTPException(404, "Clip not found")
    p = DATA / "clips" / Path(r["filename"]).name
    if not p.is_file():
        raise HTTPException(404, "Video unavailable")
    return FileResponse(p, media_type="video/mp4", filename=p.name)

MODEL = None

def transcribe(path):
    global MODEL
    if not whisper_available() or os.getenv("ENABLE_TRANSCRIPTION", "0") != "1":
        return ""
    try:
        from faster_whisper import WhisperModel
        if MODEL is None:
            MODEL = WhisperModel(os.getenv("WHISPER_MODEL", "tiny"), device="cpu", compute_type="int8")
        segments, _ = MODEL.transcribe(str(path), beam_size=1, vad_filter=True)
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
