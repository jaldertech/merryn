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


def _agenda_meeting():
    m = Meeting(
        guild_id=1, text_channel_id=1, voice_channel_id=5, mode="advisory",
        started_by_id=1, started_by_name="x",
    )
    for text in "abcde":
        m.add_agenda_item(text)
    m.advance_agenda()  # on "b": a and b are reached
    return m


def test_agenda_edit_and_move():
    m = _agenda_meeting()
    assert m.edit_agenda_item(1, "z") is None  # reached items keep their wording
    assert m.edit_agenda_item(3, "D!") == "d" and m.agenda[3].text == "D!"
    assert m.move_agenda_item(4, 2).text == "e"
    assert [i.text for i in m.agenda] == ["a", "b", "e", "c", "D!"]
    assert m.move_agenda_item(3, 1) is None  # cannot move into reached items
    assert m.current_agenda_item().text == "b"
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "continuity.json"
        store = ContinuityStore(path)
        for text in "abc":
            store.add_backlog(1, BacklogItem(text=text, submitted_by="x", submitted_by_id=9))
        assert store.edit_backlog(1, 1, "B") == "b"
        assert store.move_backlog(1, 2, 0).text == "c"
        assert store.move_backlog(1, 0, 7) is None
        assert [i.text for i in ContinuityStore.load(path).backlog_items(1)] == ["c", "a", "B"]
    print("agenda edit and move OK")


def test_reminders_for_member():
    from datetime import timedelta, timezone

    from merryn.store import PersonalReminder

    now = datetime.now(timezone.utc)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "continuity.json"
        store = ContinuityStore(path)
        for uid, mins, text in [(7, 30, "late"), (8, 5, "other"), (7, 5, "soon")]:
            store.add_personal_reminder(PersonalReminder(
                user_id=uid, guild_id=1, channel_id=1, text=text,
                fire_at=(now + timedelta(minutes=mins)).isoformat(),
            ))
        assert [r.text for r in store.personal_reminders_for(7)] == ["soon", "late"]
        store.pop_personal_reminder(store.personal_reminders_for(7)[0].id)
        assert [r.text for r in ContinuityStore.load(path).personal_reminders_for(7)] == ["late"]
        assert [r.text for r in store.personal_reminders_for(8)] == ["other"]
    print("reminders for member OK")


def test_withdraw_and_mover():
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch

    from merryn import main as M

    m = _agenda_meeting()
    m.motions.append(MotionRecord(text="Buy a goat", moved_by="Ann", outcome="withdrawn"))
    line = [x for x in minutes.build_minutes(m).splitlines() if "goat" in x][0]
    assert "WITHDRAWN" in line and "✅" not in line

    def member(uid):
        x = MagicMock()
        x.id, x.display_name = uid, f"u{uid}"
        return x

    async def withdraw(user, mod, votes):
        bot = M.client
        rec = MotionRecord(text="t", moved_by="u1", moved_by_id=1)
        view = MagicMock()
        view.record, view.votes, view.guild_id = rec, votes, 1
        view.is_finished.return_value = False
        view.message.edit = AsyncMock()
        inter = MagicMock()
        inter.user = user
        inter.response.send_message = AsyncMock()
        with patch.object(M, "is_moderator", return_value=mod), \
             patch.object(bot, "_stop_ballot_ambience", AsyncMock()), \
             patch.object(bot.registry, "save"), \
             patch.object(bot, "get_guild", return_value=MagicMock()):
            await bot.handle_motion_withdraw(inter, view)
        return rec.outcome

    async def run():
        assert await withdraw(member(1), False, {}) == "withdrawn"  # mover
        assert await withdraw(member(2), False, {}) == "open"  # someone else
        assert await withdraw(member(2), True, {}) == "withdrawn"  # moderator
        assert await withdraw(member(1), True, {3: "aye"}) == "open"  # votes cast

        # A seconded motion records the real mover, and an inquorate
        # override is judged on the mover's rights, not the seconder's.
        bot = M.client
        bot._open_ballots.clear()
        mtg = _agenda_meeting()
        mtg.quorum_enabled, mtg.quorum_size = True, 10
        mover, seconder = member(1), member(2)
        inter = MagicMock()
        inter.user, inter.guild_id = seconder, 1
        inter.response.send_message = AsyncMock()
        inter.original_response = AsyncMock(return_value=MagicMock())
        with patch.object(M, "is_moderator", side_effect=lambda u: u is mover), \
             patch.object(bot, "_eligible_voter_count", return_value=3), \
             patch.object(bot, "_start_ballot_ambience", AsyncMock()), \
             patch.object(bot, "build_ballot_embed", return_value=None), \
             patch.object(bot, "_close_motion_later", AsyncMock()), \
             patch.object(bot.registry, "save"):
            await bot.open_motion(
                inter, mtg, "Forced", 60, override=True, seconded_by="u2", mover=mover
            )
        rec = mtg.motions[-1]
        assert (rec.moved_by, rec.moved_by_id, rec.seconded_by) == ("u1", 1, "u2")
        assert rec.quorum_override

    asyncio.run(run())
    print("withdraw and mover OK")


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
    test_agenda_edit_and_move()
    test_reminders_for_member()
    test_withdraw_and_mover()
    test_update_is_newer()
    test_version_single_sourced()
    print("all smoke tests passed")
