"""Exercise real media processing through the authenticated upload API."""
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend import app as server
from backend.cli import duration
from backend.editor import probe_size, transcribe_words


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class UploadTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        data = self.folder / "data"
        data.mkdir()
        # Load the application schema (including migrations) into a private database.
        database = data / "app.db"
        with server.db() as connection:
            with server.sqlite3.connect(database) as isolated:
                connection.backup(isolated)
        self.enterContext(patch.multiple(server, DATA=data, DB=database, KEY="integration-key"))
        with server.db() as connection:
            connection.execute("DELETE FROM uploads")
            connection.execute("DELETE FROM clips")
        self.enterContext(patch.dict(os.environ, {"ENABLE_TRANSCRIPTION": "0"}))
        self.key = {"X-Admin-Key": "integration-key"}
        self.client = self.enterContext(TestClient(server.app))

    def ffmpeg(self, *args):
        return subprocess.run(["ffmpeg", "-y", "-v", "error", *args],
                              check=True, capture_output=True, timeout=45).stdout

    def fixture(self, extension, seconds=2, speech=False):
        source = self.folder / ("input." + extension)
        args = ["-f", "lavfi", "-i", f"color=c=0x243345:s=320x180:r=12:d={seconds}"]
        if speech:
            args += ["-f", "lavfi", "-i",
                     "flite=text='Wow that was amazing. Here is why this moment matters. Welcome to the video editing demo.':voice=slt",
                     "-af", "apad", "-c:a", "aac"]
        codec = "libvpx-vp9" if extension == "webm" else "libx264"
        self.ffmpeg(*args, "-t", str(seconds), "-c:v", codec, str(source))
        return source

    def upload(self, source, **settings):
        with source.open("rb") as stream:
            response = self.client.post("/api/uploads", headers=self.key,
                                        files={"file": (source.name, stream)}, data=settings)
        self.assertEqual(response.status_code, 200, response.text)
        uid = response.json()["id"]
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            uploads = self.client.get("/api/uploads", headers=self.key).json()
            job = next(item for item in uploads if item["id"] == uid)
            if job["status"] != "processing":
                self.assertEqual(job["status"], "completed", job)
                clips = self.client.get("/api/clips", headers=self.key).json()
                return job, [clip for clip in clips if clip["stream_id"] == uid]
            time.sleep(0.1)
        self.fail("Upload did not finish")

    def download(self, clip):
        endpoint = f'/api/clips/{clip["id"]}/video'
        self.assertEqual(self.client.get(endpoint).status_code, 401)
        response = self.client.get(endpoint, headers=self.key)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "video/mp4")
        self.assertIn("no-store", response.headers["cache-control"])
        output = self.folder / (clip["id"] + ".mp4")
        output.write_bytes(response.content)
        self.assertEqual(probe_size(output), (1080, 1920))
        self.ffmpeg("-i", str(output), "-f", "null", "-")
        return output

    def test_silent_short_recordings_all_formats(self):
        for extension in ("mp4", "mov", "mkv", "webm"):
            with self.subTest(extension=extension):
                job, clips = self.upload(self.fixture(extension), clip_count=1, captions="true")
                self.assertEqual(len(clips), 1)
                self.assertIn("No speech", job["warning"])
                self.assertAlmostEqual(duration(self.download(clips[0])), 2, delta=0.2)

    def test_configurable_count_duration_and_invalid_media(self):
        rejected = self.client.post("/api/uploads", headers=self.key,
                                    files={"file": ("broken.mp4", b"not a video")})
        self.assertEqual(rejected.status_code, 400)
        self.assertIn("Invalid video", rejected.json()["detail"])
        rejected = self.client.post("/api/video-url", headers=self.key,
                                    json={"url": "https://instagram.com/reel/example"})
        self.assertEqual(rejected.status_code, 400)
        missing = self.client.post("/api/uploads", files={"file": ("input.mp4", b"dummy")})
        self.assertEqual(missing.status_code, 401)
        _, clips = self.upload(self.fixture("mp4", seconds=11), clip_count=2, clip_seconds=5, preset="dynamic")
        self.assertEqual(len(clips), 2)
        for clip in clips:
            self.assertAlmostEqual(duration(self.download(clip)), 5, delta=0.2)

    @unittest.skipUnless(os.getenv("TEST_WHISPER") == "1", "Set TEST_WHISPER=1 for real transcription")
    def test_real_whisper_captions_are_burned_at_the_word_time(self):
        source = self.fixture("mp4", seconds=12, speech=True)
        words = transcribe_words(source, os.getenv("WHISPER_MODEL", "tiny"))
        word = next(word for word in words if word["end"] - word["start"] > 0.4)
        job, clips = self.upload(source, clip_count=1, clip_seconds=12, preset="blur",
                                 captions="true", accent_captions="true")
        self.assertEqual(job["warning"], "")
        self.assertTrue(clips[0]["transcript"])
        output = self.download(clips[0])
        # The background is a flat color. Caption frames contain bright pixels; after
        # the last spoken word, the same region returns to its uncaptioned color.
        def bright_pixels(timestamp):
            pixels = self.ffmpeg("-ss", str(timestamp), "-i", str(output), "-frames:v", "1",
                                 "-vf", "crop=1080:200:0:1520,format=gray", "-f", "rawvideo", "-")
            self.assertEqual(len(pixels), 1080 * 200)
            return sum(pixel >= 170 for pixel in pixels)

        self.assertGreater(bright_pixels((word["start"] + word["end"]) / 2), 25)
        self.assertEqual(bright_pixels(10), 0)
        audio = subprocess.check_output(["ffprobe", "-v", "error", "-select_streams", "a:0",
                                         "-show_entries", "stream=sample_rate", "-of", "csv=p=0", str(output)])
        self.assertEqual(audio.strip(), b"48000")
