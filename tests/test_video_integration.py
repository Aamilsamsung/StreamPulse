"""FFmpeg integration checks using a generated, copyright-free synthetic video."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from backend.editor import render, probe_size

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

if __name__ == "__main__":
    unittest.main()
