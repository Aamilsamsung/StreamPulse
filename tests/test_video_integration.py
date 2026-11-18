"""FFmpeg integration checks using a generated, copyright-free synthetic video."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from backend.editor import render, probe_size, write_ass
from backend.cli import clip_video

@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class RealRenderTests(unittest.TestCase):
    def test_all_presets_export_vertical_video(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.mp4"
            generated = subprocess.run(
                ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                 "-i", "testsrc2=size=320x180:rate=12:duration=2",
                 "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                 "-shortest", "-c:v", "libx264", "-preset", "ultrafast",
                 "-c:a", "aac", str(source)],
                capture_output=True, text=True, timeout=60)
            self.assertEqual(generated.returncode, 0, generated.stderr)
            for preset in ("clean", "dynamic", "blur"):
                with self.subTest(preset=preset):
                    dest = root / (preset + ".mp4")
                    render(source, dest, preset=preset)
                    self.assertGreater(dest.stat().st_size, 1000)
                    self.assertEqual(probe_size(dest), (1080, 1920))

    def test_cli_generates_downloadable_clip(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "input.mp4"
            created = subprocess.run([
                "ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                "-i", "testsrc2=size=320x180:rate=12:duration=9",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=9",
                "-shortest", "-c:v", "libx264", "-preset", "ultrafast",
                "-c:a", "aac", str(source)
            ], capture_output=True, text=True, timeout=60)
            self.assertEqual(created.returncode, 0, created.stderr)
            output = root / "clips"
            clips = clip_video(source, output, preset="clean", count=1, window=9)
            self.assertEqual(len(clips), 1)
            self.assertTrue((output / "highlights.json").is_file())
            exported = output / clips[0]["file"]
            self.assertGreater(exported.stat().st_size, 1000)
            self.assertEqual(probe_size(exported), (1080, 1920))

    def test_subtitle_file_generation(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "captions.ass"
            write_ass([{"start": 0.1, "end": 0.6, "word": "Hello"}], target)
            self.assertIn("Hello", target.read_text(encoding="utf-8"))

if __name__ == "__main__":
    unittest.main()
