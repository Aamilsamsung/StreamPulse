"""FFmpeg editing primitives for locally generated highlight clips.

All subprocess calls use argument arrays (never a shell). Captions require
optional faster-whisper. No remote publishing is performed.
"""
import json
import math
import re
import shutil
import subprocess
from pathlib import Path

PRESETS = ("clean", "dynamic", "blur")

def _run(args, timeout=180):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)

def probe_size(source):
    result = _run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                   "-show_entries", "stream=width,height", "-of", "json", str(source)], 30)
    if result.returncode:
        raise ValueError("Unable to probe video")
    streams = json.loads(result.stdout).get("streams", [])
    if not streams:
        raise ValueError("No video track found")
    width, height = int(streams[0]["width"]), int(streams[0]["height"])
    if width < 1 or height < 1:
        raise ValueError("Invalid video dimensions")
    return width, height

def filter_for(preset="clean", width=1920, height=1080):
    if preset not in PRESETS:
        raise ValueError("Unknown editing preset")
    # Center crop only when landscape; portrait inputs are fitted without cropping.
    if preset == "dynamic" and width > height:
        return ("scale=1080:1920:force_original_aspect_ratio=increase,"
                "crop=1080:1920,setsar=1")
    if preset == "blur":
        # Background blur with centered full-frame foreground, no face tracking.
        return ("split=2[bg][fg];[bg]scale=1080:1920:force_original_aspect_ratio=increase,"
                "crop=1080:1920,boxblur=24:1[back];[fg]scale=1080:1920:"
                "force_original_aspect_ratio=decrease[front];[back][front]"
                "overlay=(W-w)/2:(H-h)/2,setsar=1")
    return ("scale=1080:1920:force_original_aspect_ratio=decrease,"
            "pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1")

def escape_ass(value):
    """Escape caption text before embedding in ASS subtitle event lines."""
    value = re.sub(r"[\r\n]+", " ", str(value))
    value = value.replace("\\", "").replace("{", "(").replace("}", ")")
    return value.strip()[:180]

def ass_time(seconds):
    seconds = max(0, float(seconds))
    hours = int(seconds // 3600)
    minutes = int(seconds % 3600 // 60)
    sec = int(seconds % 60)
    centiseconds = int(round((seconds - math.floor(seconds)) * 100))
    if centiseconds == 100:
        sec += 1
        centiseconds = 0
    return f"{hours}:{minutes:02d}:{sec:02d}.{centiseconds:02d}"

def write_ass(words, target):
    """Write word-timed, readable bottom-center captions."""
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,64,&H00FFFFFF,&H000000FF,&H00101010,&H80000000,-1,0,0,0,100,100,0,0,1,4,1,2,80,80,250,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [header]
    for word in words:
        text = escape_ass(word.get("word", ""))
        start, end = float(word["start"]), float(word["end"])
        if text and end > start:
            lines.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Default,,0,0,0,,{text}\n")
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("".join(lines), encoding="utf-8")
    return target

def transcribe_words(source, model_name="tiny"):
    """Word timestamps require optional faster-whisper and a local model."""
    from faster_whisper import WhisperModel
    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    segments, _ = model.transcribe(str(source), word_timestamps=True, vad_filter=True)
    words = []
    for segment in segments:
        for word in segment.words or []:
            if word.start is not None and word.end is not None:
                words.append({"start": word.start, "end": word.end, "word": word.word})
    return words

def render(source, destination, preset="clean", subtitles=False, model_name="tiny"):
    """Render 9:16 MP4. Subtitle generation fails explicitly if unavailable."""
    source, destination = Path(source), Path(destination)
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise RuntimeError("ffmpeg and ffprobe are required")
    width, height = probe_size(source)
    vf = filter_for(preset, width, height)
    subtitle_path = None
    if subtitles:
        words = transcribe_words(source, model_name)
        if not words:
            raise ValueError("No speech detected; cannot generate subtitles")
        subtitle_path = destination.with_suffix(".ass")
        write_ass(words, subtitle_path)
        # Use a filename-only reference in the output directory to avoid unsafe
        # FFmpeg filter-path escaping and OS-dependent path separator behavior.
        vf += ",ass=" + subtitle_path.name.replace(":", "\\:")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        args = ["ffmpeg", "-y", "-v", "error", "-i", str(source),
                "-vf", vf, "-c:v", "libx264", "-preset", "veryfast",
                "-crf", "27", "-c:a", "aac", "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
                "-movflags", "+faststart", str(destination)]
        # ASS filename is relative to the render's output directory.
        result = subprocess.run(args, cwd=str(destination.parent), capture_output=True,
                                text=True, timeout=240)
        if result.returncode or not destination.exists():
            raise RuntimeError("FFmpeg render failed: " + result.stderr[-500:])
        return destination
    finally:
        if subtitle_path:
            subtitle_path.unlink(missing_ok=True)
