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
from merryn.store import BacklogItem, ContinuityStore, GuildSettings


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
    store.set_second_required(42, True)
    reloaded = ContinuityStore.load(path)
    assert reloaded.settings_for(42) == GuildSettings(
        quorum_enabled=True, quorum_size=8, second_required=True
    )
    assert reloaded.settings_for(999) == GuildSettings()
    print("settings persistence OK")


def test_duration_parse():
    p = minutes.parse_duration
    assert p("10m") == 600
    assert p("1h30m") == 5400
    assert p("2d") == 172800
    assert p("45s") == 45
    assert p("1h30m10s") == 5410
    assert p("  5m  ") == 300
    assert p("") is None
    assert p("garbage") is None
    assert p("5") is None
    print("duration parse OK")


def test_chunk_for_discord():
    c = minutes.chunk_for_discord("a" * 100)
    assert c == ["a" * 100]

    many_lines = "\n".join(f"line {i}" for i in range(500))
    chunks = minutes.chunk_for_discord(many_lines, limit=2000)
    assert all(len(x) <= 2000 for x in chunks)
    assert "\n".join(chunks) == many_lines

    # A single line longer than the limit must be hard-split, not dropped.
    huge = "x" * 5000
    chunks = minutes.chunk_for_discord(huge, limit=2000)
    assert sum(len(x) for x in chunks) == 5000
    assert "".join(chunks) == huge
    print("chunk for discord OK")


def test_reminder_persistence():
    from merryn.store import PendingReminder, PersonalReminder

    path = Path(tempfile.mkdtemp()) / "continuity.json"
    store = ContinuityStore(path)
    store.add_reminder(
        1, PendingReminder(event_id=99, channel_id=2, title="Meeting",
                            start_at="2026-08-01T19:00:00+00:00")
    )
    store.add_personal_reminder(
        PersonalReminder(user_id=9, guild_id=1, channel_id=5, text="stretch",
                          fire_at="2026-07-29T18:00:00+00:00")
    )
    reloaded = ContinuityStore.load(path)
    assert len(list(reloaded.iter_reminders())) == 1
    assert len(reloaded.personal_reminders) == 1

    reloaded.mark_reminder_fired(1, 99)
    reloaded.prune_reminder(1, 99)
    reloaded.pop_personal_reminder(reloaded.personal_reminders[0].id)
    final = ContinuityStore.load(path)
    assert list(final.iter_reminders()) == []
    assert final.personal_reminders == []
    print("reminder persistence OK")


def test_item_numbers_parse():
    p = minutes.parse_item_numbers
    assert p("3") == [3]
    assert p("1, 4, 6") == [1, 4, 6]
    assert p("2-5") == [2, 3, 4, 5]
    assert p("2 - 4, 8") == [2, 3, 4, 8]
    assert p("5 1 1") == [1, 5]
    assert p("") is None
    assert p("0") is None
    assert p("5-2") is None
    assert p("two") is None
    print("item numbers parse OK")


def test_backlog_bulk_drop():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "continuity.json"
        store = ContinuityStore(path)
        for text in "abcde":
            store.add_backlog(1, BacklogItem(text=text, submitted_by="x", submitted_by_id=9))
        removed = store.drop_backlog(1, [0, 2, 4, 99])
        assert [i.text for i in removed] == ["a", "c", "e"]
        assert [i.text for i in ContinuityStore.load(path).backlog_items(1)] == ["b", "d"]
        assert len(store.drop_backlog(1, [0, 1])) == 2
        assert store.backlog_items(1) == []
    print("backlog bulk drop OK")


def test_live_agenda_drop_keeps_reached_items():
    m = Meeting(
        guild_id=1, text_channel_id=1, voice_channel_id=1, mode="advisory",
        started_by_id=1, started_by_name="x",
    )
    for text in "abcde":
        m.add_agenda_item(text)
    m.advance_agenda()  # on "b": a and b are reached
    assert m.first_upcoming_index() == 2
    removed = m.drop_agenda_items([0, 1, 3])
    assert [i.text for i in removed] == ["d"]
    assert [i.text for i in m.agenda] == ["a", "b", "c", "e"]
    assert m.current_agenda_item().text == "b" and len(m.agenda_started) == 2
    assert [i.text for i in m.drop_agenda_items(list(range(4)))] == ["c", "e"]
    print("live agenda drop OK")


def test_update_is_newer():
    from merryn.update import is_newer

    assert is_newer("v1.2.0", "1.1.0")
    assert is_newer("1.10.0", "1.9.0")  # numeric, not lexical
    assert is_newer("v1.2.0-rc1", "1.1.0")  # pre-release suffix ignored
    assert is_newer("v1.2", "1.1.0")  # uneven lengths pad with zeros
    assert not is_newer("1.2.0", "1.2.0")
    assert not is_newer("v1.1.0", "1.2.0")
    assert not is_newer("garbage", "1.1.0")  # unparseable stays quiet
    assert not is_newer("v1.2.0", "also-garbage")
    print("update is_newer OK")


def test_version_single_sourced():
    import merryn

    assert merryn.__version__ and merryn.__version__[0].isdigit()
    print(f"version single-sourced OK ({merryn.__version__})")


if __name__ == "__main__":
    test_hold_music_loops()
    test_motion_outcomes()
    test_minutes_render()
    test_quorum_gating()
    test_schedule_parse()
    test_minutes_quorum_and_procedural()
    test_settings_persistence()
    test_duration_parse()
    test_chunk_for_discord()
    test_reminder_persistence()
    test_item_numbers_parse()
    test_backlog_bulk_drop()
    test_live_agenda_drop_keeps_reached_items()
    test_update_is_newer()
    test_version_single_sourced()
    print("all smoke tests passed")
