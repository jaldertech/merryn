"""Dependency-light smoke test: python tests/test_smoke.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from merryn import audio, meeting, minutes, views  # noqa: F401
from merryn.audio import FRAME_BYTES, LoopingWAVAudio, resolve_hold_music
from merryn.meeting import Meeting, MotionRecord


def test_hold_music_loops():
    source = LoopingWAVAudio(resolve_hold_music())
    total = len(source._pcm)
    # Read enough frames to wrap the loop at least once.
    for _ in range(total // FRAME_BYTES + 2):
        frame = source.read()
        assert len(frame) == FRAME_BYTES, len(frame)
    print(f"hold music OK ({total} PCM bytes, seamless wrap)")


def test_motion_outcomes():
    cases = [
        (2, 1, None, 0, "carried"),
        (1, 1, None, 0, "tied"),
        (3, 1, 75, 0, "carried"),
        (2, 1, 75, 0, "failed"),
        (0, 0, 75, 0, "failed"),
    ]
    for yes, no, threshold, eligible, want in cases:
        record = MotionRecord(
            text="t", moved_by="m", yes=yes, no=no,
            pass_threshold=threshold, eligible=eligible,
        )
        pct = record.percent_in_favour()
        if threshold is not None:
            got = "carried" if pct is not None and pct >= threshold else "failed"
        else:
            got = "carried" if yes > no else "failed" if no > yes else "tied"
        assert got == want, (yes, no, threshold, got, want)
    record = MotionRecord(text="t", moved_by="m", yes=4, no=1, eligible=6)
    assert record.abstained() == 1 and record.percent_abstained() == 17
    print("motion outcomes OK")


def test_minutes_render():
    m = Meeting(
        guild_id=1, text_channel_id=2, voice_channel_id=3,
        mode=meeting.MODE_ADVISORY, started_by_id=4, started_by_name="Chair",
    )
    m.motions.append(
        MotionRecord(text="Test motion", moved_by="Chair", yes=3, no=1,
                     outcome="carried", eligible=5)
    )
    text = minutes.build_minutes(m, meeting.now_iso())
    assert "Test motion" in text and "75% in favour" in text
    print("minutes render OK")


if __name__ == "__main__":
    test_hold_music_loops()
    test_motion_outcomes()
    test_minutes_render()
    print("all smoke tests passed")
