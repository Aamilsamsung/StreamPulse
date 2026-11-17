import unittest
from backend.highlights import rank_candidate, transcript_score, choose_nonoverlapping

class HighlightTests(unittest.TestCase):
    def test_score_bounds(self):
        self.assertEqual(rank_candidate(-100)[0], 0)
        self.assertEqual(rank_candidate(30, "oh my god no way clutch", 100)[0], 100)
    def test_phrase_detection(self):
        score, cues = transcript_score("NO WAY, that was a clutch!")
        self.assertIn("no way", cues)
        self.assertIn("clutch", cues)
        self.assertGreater(score, 0)
        self.assertNotIn("ace", transcript_score("spacecraft")[1])
    def test_nonoverlapping(self):
        candidates = [(90, 0, "a"), (85, 10, "b"), (80, 40, "c"), (70, 80, "d")]
        chosen = choose_nonoverlapping(candidates, 3, 25)
        self.assertEqual([x[2] for x in chosen], ["a", "c", "d"])
    def test_empty(self):
        self.assertEqual(choose_nonoverlapping([], 3), [])

if __name__ == "__main__":
    unittest.main()
