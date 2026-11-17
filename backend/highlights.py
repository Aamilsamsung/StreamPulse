"""Deterministic highlight candidate selection.

Combines audio intensity, transcript cues and shot activity. This is a heuristic,
not a trained virality or gameplay understanding model.
"""
import re

HYPE = {
    "no way": 18, "oh my god": 18, "insane": 12, "unbelievable": 14,
    "clutch": 16, "ace": 14, "wow": 8, "amazing": 8, "crazy": 8,
    "let's go": 15, "what happened": 10, "important": 5,
    "here is why": 8, "the point is": 7,
}
def transcript_score(transcript: str) -> tuple[int, list[str]]:
    text = (transcript or "").lower()
    matched = [word for word in HYPE if re.search(r"(?<!\w)" + re.escape(word) + r"(?!\w)", text)]
    return min(40, sum(HYPE[word] for word in matched)), matched

def rank_candidate(max_volume_db: float, transcript: str = "", scene_changes: int = 0):
    """Return bounded score and interpretable reasons, never AI-invented findings."""
    audio = max(0, min(55, round((max_volume_db + 45) * 1.35)))
    speech, hits = transcript_score(transcript)
    scene = min(15, max(0, int(scene_changes)) * 3)
    score = min(100, audio + speech + scene)
    reasons = [f"Audio intensity ({audio}/55)"]
    if hits:
        reasons.append("Transcript cues: " + ", ".join(hits))
    if scene:
        reasons.append(f"Visual cuts ({scene}/15)")
    return score, "; ".join(reasons)

def choose_nonoverlapping(candidates, count=3, min_gap=25):
    """Candidates: iterable of (score, start_seconds, ...). Stable, diverse picks."""
    ordered = sorted(candidates, key=lambda item: (-item[0], item[1]))
    result = []
    for item in ordered:
        if all(abs(item[1] - chosen[1]) >= min_gap for chosen in result):
            result.append(item)
            if len(result) >= count:
                break
    return result
