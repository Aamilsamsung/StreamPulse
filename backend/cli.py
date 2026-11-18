"""Run StreamPulse on a local video without the web dashboard.

Example:
 python -m backend.cli input.mp4 --preset dynamic --clips 3 --subtitles
"""
import argparse
import json
import math
import subprocess
import tempfile
from pathlib import Path

from backend.editor import render, whisper_model
from backend.highlights import rank_candidate, choose_nonoverlapping

def cmd(args, timeout=180):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(result.stderr[-1200:] or "Media command failed")
    return result

def duration(path):
    result = cmd(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                  "-of", "default=noprint_wrappers=1:nokey=1", str(path)], 30)
    value = float(result.stdout.strip())
    if not math.isfinite(value) or value <= 0:
        raise ValueError("Invalid video duration")
    return value

def audio_loudness(path):
    result = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(path),
                             "-af", "volumedetect", "-vn", "-f", "null", "-"],
                            capture_output=True, text=True, timeout=60)
    import re
    match = re.search(r"max_volume:\s*(-?[\d.]+) dB", result.stderr)
    return float(match.group(1)) if match else -90.0

def speech_text(path, model):
    segments, _ = model.transcribe(str(path), beam_size=1, vad_filter=True)
    return " ".join(s.text.strip() for s in segments)[:3000]

def clip_video(source, output, preset="dynamic", count=3, window=30,
               subtitles=False, transcribe=False, model_name="tiny", max_minutes=60):
    source = Path(source).expanduser().resolve()
    output = Path(output).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    if count < 1 or count > 20 or window < 8 or window > 120:
        raise ValueError("Choose 1–20 clips and an 8–120 second window")
    length = duration(source)
    if length > max_minutes * 60:
        raise ValueError(f"Video longer than {max_minutes} minutes; raise --max-minutes explicitly")
    output.mkdir(parents=True, exist_ok=True)
    model = None
    if transcribe:
        try:
            model = whisper_model(model_name)
        except ImportError as exc:
            raise RuntimeError("Install faster-whisper for AI speech scoring") from exc
    candidates = []
    with tempfile.TemporaryDirectory(prefix="streampulse-") as temp:
        tempdir = Path(temp)
        for start in range(0, math.ceil(length), window):
            clip_length = min(window, length - start)
            if start and clip_length < window * 0.5:
                continue
            segment = tempdir / f"candidate_{start:06d}.mp4"
            cmd(["ffmpeg", "-y", "-v", "error", "-ss", str(start), "-i", str(source),
                 "-t", str(clip_length), "-c:v", "libx264", "-preset", "ultrafast",
                 "-crf", "32", "-c:a", "aac", str(segment)], 180)
            loudness = audio_loudness(segment)
            transcript = speech_text(segment, model) if model else ""
            score, reason = rank_candidate(loudness, transcript)
            candidates.append((score, start, segment, reason, transcript))
            print(f"Analyzed {start:>5}s | score {score:>3} | {reason}", flush=True)
        selected = choose_nonoverlapping(candidates, count, min_gap=window)
        results = []
        for index, (score, start, segment, reason, transcript) in enumerate(selected, 1):
            dest = output / f"highlight_{index:02d}_{start:05d}s.mp4"
            render(segment, dest, preset=preset, subtitles=subtitles, model_name=model_name)
            results.append({"file": dest.name, "start_seconds": start,
                            "score": score, "reason": reason, "transcript": transcript})
            print(f"Exported {dest}", flush=True)
    (output / "highlights.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    return results

def main():
    p = argparse.ArgumentParser(description="StreamPulse local video highlight clipper")
    p.add_argument("video", help="Path to a video you own or are authorized to edit")
    p.add_argument("--output", default="output_clips")
    p.add_argument("--clips", type=int, default=3)
    p.add_argument("--window", type=int, default=30)
    p.add_argument("--preset", choices=["clean", "dynamic", "blur"], default="dynamic")
    p.add_argument("--subtitles", action="store_true", help="AI word-level burned subtitles (requires faster-whisper and FFmpeg libass)")
    p.add_argument("--transcribe", action="store_true", help="Use Whisper transcript cues to rank highlights")
    p.add_argument("--model", default="tiny", help="Whisper model size")
    p.add_argument("--max-minutes", type=int, default=60)
    args = p.parse_args()
    clip_video(args.video, args.output, args.preset, args.clips, args.window,
               args.subtitles, args.transcribe, args.model, args.max_minutes)

if __name__ == "__main__":
    main()
