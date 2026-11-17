"""End-to-end local CLI integration on generated media."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from backend.cli import clip_video

@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class CLITests(unittest.TestCase):
    def test_produces_clips_and_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            src = root / "input.mp4"
            generated = subprocess.run([
                "ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                "-i", "testsrc2=size=320x180:rate=10:duration=9",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=9",
                "-shortest", "-c:v", "libx264", "-preset", "ultrafast",
                "-c:a", "aac", str(src)
            ], capture_output=True, text=True, timeout=60)
            self.assertEqual(generated.returncode, 0, generated.stderr)
            clips = clip_video(src, root / "out", preset="dynamic", count=1,
                               window=10, max_minutes=1)
            self.assertEqual(len(clips), 1)
            self.assertTrue((root / "out" / clips[0]["file"]).is_file())
            self.assertTrue((root / "out" / "highlights.json").is_file())

if __name__ == "__main__":
    unittest.main()
