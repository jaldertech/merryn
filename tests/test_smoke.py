"""Dependency-light smoke test: python tests/test_smoke.py"""
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from merryn import audio, meeting, minutes, views  # noqa: F401
from merryn.audio import FRAME_BYTES, LoopingWAVAudio, resolve_hold_music
from merryn.meeting import Meeting, MotionRecord
from merryn.store import ContinuityStore, GuildSettings


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


def test_quorum_gating():
    m = Meeting(
        guild_id=1, text_channel_id=2, voice_channel_id=3,
        mode=meeting.MODE_ADVISORY, started_by_id=4, started_by_name="Chair",
    )
    # Off by default.
    assert not m.quorum_active() and m.is_quorate(0)
    # Enabled and set: gates on the head-count.
    m.quorum_enabled, m.quorum_size = True, 5
    assert m.quorum_active()
    assert m.is_quorate(5) and m.is_quorate(6) and not m.is_quorate(4)
    # Enabled-but-unset must gate nothing.
    m.quorum_size = 0
    assert not m.quorum_active() and m.is_quorate(0)
    print("quorum gating OK")


def test_schedule_parse():
    tz = ZoneInfo("Europe/London")
    now = datetime(2026, 7, 23, 20, 0, tzinfo=tz)
    p = minutes.parse_local_datetime
    assert p("2026-08-01 19:30", now=now, tz=tz).hour == 19
    assert p("01/08/2026 19:30", now=now, tz=tz).day == 1
    # A bare time later today stays today; one already passed rolls to tomorrow.
    assert p("21:15", now=now, tz=tz).day == 23
    assert p("09:00", now=now, tz=tz).day == 24
    assert p("not a time", now=now, tz=tz) is None
    print("schedule parse OK")


def test_minutes_quorum_and_procedural():
    m = Meeting(
        guild_id=1, text_channel_id=2, voice_channel_id=3,
        mode=meeting.MODE_ADVISORY, started_by_id=4, started_by_name="Chair",
    )
    m.quorum_enabled, m.quorum_size = True, 5
    m.motions.append(
        MotionRecord(text="Forced motion", moved_by="Chair", yes=2, no=0,
                     outcome="carried", eligible=3, quorum_size=5,
                     quorum_override=True)
    )
    m.add_log("procedural", "Quorum enforcement switched on by Chair.", "Chair")
    text = minutes.build_minutes(m, meeting.now_iso())
    assert "**Quorum:** 5 members" in text
    assert "Taken under chair override" in text
    assert "## Procedural" in text
    print("minutes quorum + procedural OK")


def test_settings_persistence():
    path = Path(tempfile.mkdtemp()) / "continuity.json"
    store = ContinuityStore(path)
    # Default settings are never written.
    assert store.settings_for(42) == GuildSettings()
    store.set_quorum_size(42, 8)
    store.set_quorum_enabled(42, True)
    reloaded = ContinuityStore.load(path)
    assert reloaded.settings_for(42) == GuildSettings(quorum_enabled=True, quorum_size=8)
    assert reloaded.settings_for(999) == GuildSettings()
    print("settings persistence OK")


if __name__ == "__main__":
    test_hold_music_loops()
    test_motion_outcomes()
    test_minutes_render()
    test_quorum_gating()
    test_schedule_parse()
    test_minutes_quorum_and_procedural()
    test_settings_persistence()
    print("all smoke tests passed")
