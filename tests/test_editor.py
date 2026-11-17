import tempfile
import unittest
from pathlib import Path
from backend.editor import filter_for, escape_ass, ass_time, write_ass

class EditorTests(unittest.TestCase):
    def test_presets(self):
        self.assertIn("pad=", filter_for("clean"))
        self.assertIn("crop=", filter_for("dynamic", 1920, 1080))
        self.assertNotIn("crop=", filter_for("dynamic", 1080, 1920))
        self.assertIn("boxblur=", filter_for("blur"))
        with self.assertRaises(ValueError):
            filter_for("invalid")
    def test_caption_escaping(self):
        self.assertEqual(escape_ass("hi{there}"), "hi(there)")
        self.assertEqual(ass_time(1.25), "0:00:01.25")
    def test_ass_file(self):
        with tempfile.TemporaryDirectory() as temp:
            path = write_ass([{"start": 0.1, "end": 0.6, "word": "hello"}], Path(temp)/"test.ass")
            data = path.read_text()
            self.assertIn("Dialogue:", data)
            self.assertIn("hello", data)

if __name__ == "__main__":
    unittest.main()
