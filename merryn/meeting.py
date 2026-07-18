"""Meeting state for Merryn.

All timestamps are stored as UTC ISO-8601 strings. Conversion to the
display timezone (MERRYN_TIMEZONE, defaulting to the system's local
time) happens only at the presentation layer (minutes.py and the live
panel embed).

State is persisted to DATA_DIR/state.json after every mutation so a
container restart mid-meeting can recover the session — most
importantly the set of members the bot has server-muted, which must
never be orphaned.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

UTC = timezone.utc

MODE_STRICT = "strict"
MODE_ADVISORY = "advisory"


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def iso_to_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


@dataclass
class QueueEntry:
    user_id: int
    display_name: str
    point_of_order: bool = False
    raised_at: str = field(default_factory=now_iso)


@dataclass
class SpeakerTurn:
    user_id: int
    display_name: str
    point_of_order: bool = False
    started_at: str = field(default_factory=now_iso)
    ended_at: str | None = None

    def seconds(self) -> float:
        end = iso_to_dt(self.ended_at) if self.ended_at else datetime.now(UTC)
        return max(0.0, (end - iso_to_dt(self.started_at)).total_seconds())


@dataclass
class AgendaItem:
    text: str
    owner_id: int | None = None  # member due to present this item
    owner_name: str | None = None


@dataclass
class LogEntry:
    kind: str  # "note" | "decision" | "action"
    text: str
    author: str
    agenda_index: int | None = None
    at: str = field(default_factory=now_iso)


@dataclass
class MotionRecord:
    text: str
    moved_by: str
    yes: int = 0
    no: int = 0
    # Abstain was removed from the ballot; the field remains so persisted
    # records that carry an "abstain" key still load via MotionRecord(**m).
    abstain: int = 0
    outcome: str = "open"  # "carried" | "failed" | "tied" | "open" | "void"
    # Percentage of votes cast that must be ayes for the motion to carry.
    # None means the original rule: simple majority (yes > no), tie possible.
    pass_threshold: int | None = None
    # Members present in the chamber when the ballot closed; 0 = not
    # recorded (legacy records). Only ever a head-count, never identities.
    eligible: int = 0
    at: str = field(default_factory=now_iso)

    def percent_in_favour(self) -> int | None:
        """Ayes as a whole percentage of votes cast, or None if nobody voted."""
        total = self.yes + self.no
        if not total:
            return None
        return round(self.yes / total * 100)

    def abstained(self) -> int:
        """Members present at close who cast no vote either way."""
        return max(self.eligible - self.yes - self.no, 0)

    def percent_abstained(self) -> int | None:
        """Non-voters as a percentage of members present, or None if unrecorded."""
        if not self.eligible:
            return None
        return round(self.abstained() / self.eligible * 100)


@dataclass
class AttendanceEvent:
    user_id: int
    display_name: str
    event: str  # "present" | "join" | "leave"
    at: str = field(default_factory=now_iso)


@dataclass
class Meeting:
    guild_id: int
    text_channel_id: int
    voice_channel_id: int
    mode: str
    started_by_id: int
    started_by_name: str
    started_at: str = field(default_factory=now_iso)
    panel_message_id: int | None = None
    # A test meeting behaves identically live but never touches the
    # cross-meeting continuity store: it does not drain the agenda
    # backlog, surface outstanding actions, or persist its own actions.
    persistent: bool = True

    agenda: list[AgendaItem] = field(default_factory=list)
    agenda_index: int | None = None
    agenda_started: list[str] = field(default_factory=list)  # start time per item reached

    queue: list[QueueEntry] = field(default_factory=list)
    current: SpeakerTurn | None = None
    turns: list[SpeakerTurn] = field(default_factory=list)

    logs: list[LogEntry] = field(default_factory=list)
    motions: list[MotionRecord] = field(default_factory=list)
    attendance: list[AttendanceEvent] = field(default_factory=list)

    timer_seconds: int = 0  # 0 = no per-speaker limit
    timer_alerted: bool = False

    muted_user_ids: list[int] = field(default_factory=list)
    # Members muted for the duration of an open ballot (they were not
    # already muted when it opened); unmuted when the ballot closes.
    ballot_muted_user_ids: list[int] = field(default_factory=list)

    # --- queue mechanics -------------------------------------------------

    def sorted_queue(self) -> list[QueueEntry]:
        """Points of order first, FIFO within each class."""
        return sorted(self.queue, key=lambda e: (not e.point_of_order, e.raised_at))

    def find_queued(self, user_id: int) -> QueueEntry | None:
        for entry in self.queue:
            if entry.user_id == user_id:
                return entry
        return None

    def toggle_hand(self, user_id: int, display_name: str) -> bool:
        """Returns True if the hand is now raised, False if lowered."""
        existing = self.find_queued(user_id)
        if existing and not existing.point_of_order:
            self.queue.remove(existing)
            return False
        if existing:
            # Already queued as a point of order; leave it alone.
            return True
        self.queue.append(QueueEntry(user_id=user_id, display_name=display_name))
        return True

    def raise_point_of_order(self, user_id: int, display_name: str) -> bool:
        """Returns True if newly raised/upgraded, False if already pending."""
        existing = self.find_queued(user_id)
        if existing and existing.point_of_order:
            return False
        if existing:
            existing.point_of_order = True
            existing.raised_at = now_iso()
            return True
        self.queue.append(
            QueueEntry(user_id=user_id, display_name=display_name, point_of_order=True)
        )
        return True

    def next_speaker(self) -> tuple[SpeakerTurn | None, SpeakerTurn | None]:
        """Ends the current turn and promotes the head of the queue.

        Returns (previous_turn, new_turn); either may be None.
        """
        previous = self.end_current_turn()
        ordered = self.sorted_queue()
        if not ordered:
            return previous, None
        entry = ordered[0]
        self.queue.remove(entry)
        self.current = SpeakerTurn(
            user_id=entry.user_id,
            display_name=entry.display_name,
            point_of_order=entry.point_of_order,
        )
        self.timer_alerted = False
        return previous, self.current

    def give_floor(
        self, user_id: int, display_name: str
    ) -> tuple[SpeakerTurn | None, SpeakerTurn]:
        """Ends the current turn and recognises the named member directly,
        bypassing queue order. If they were queued, the entry is consumed.

        Returns (previous_turn, new_turn).
        """
        previous = self.end_current_turn()
        entry = self.find_queued(user_id)
        if entry is not None:
            self.queue.remove(entry)
        self.current = SpeakerTurn(user_id=user_id, display_name=display_name)
        self.timer_alerted = False
        return previous, self.current

    def end_current_turn(self) -> SpeakerTurn | None:
        if self.current is None:
            return None
        turn = self.current
        turn.ended_at = now_iso()
        self.turns.append(turn)
        self.current = None
        return turn

    # --- agenda -----------------------------------------------------------

    def current_agenda_item(self) -> AgendaItem | None:
        if self.agenda_index is None or self.agenda_index >= len(self.agenda):
            return None
        return self.agenda[self.agenda_index]

    def advance_agenda(self) -> AgendaItem | None:
        """Moves to the next item; returns it, or None when exhausted."""
        if not self.agenda:
            return None
        if self.agenda_index is None:
            self.agenda_index = 0
        else:
            self.agenda_index += 1
        if self.agenda_index >= len(self.agenda):
            self.agenda_index = len(self.agenda)  # past the end = complete
            return None
        self.agenda_started.append(now_iso())
        return self.agenda[self.agenda_index]

    def add_agenda_item(
        self,
        item: str,
        owner_id: int | None = None,
        owner_name: str | None = None,
    ) -> None:
        self.agenda.append(
            AgendaItem(text=item, owner_id=owner_id, owner_name=owner_name)
        )
        if self.agenda_index is None:
            self.agenda_index = 0
            self.agenda_started.append(now_iso())

    def assign_owner(
        self, index: int, owner_id: int, owner_name: str
    ) -> AgendaItem | None:
        """Sets the presenter of the item at index; None if out of range."""
        if index < 0 or index >= len(self.agenda):
            return None
        item = self.agenda[index]
        item.owner_id = owner_id
        item.owner_name = owner_name
        return item

    # --- logging ----------------------------------------------------------

    def add_log(self, kind: str, text: str, author: str) -> LogEntry:
        entry = LogEntry(
            kind=kind,
            text=text,
            author=author,
            agenda_index=self.agenda_index
            if self.agenda_index is not None and self.agenda_index < len(self.agenda)
            else None,
        )
        self.logs.append(entry)
        return entry

    def record_attendance(self, user_id: int, display_name: str, event: str) -> None:
        self.attendance.append(
            AttendanceEvent(user_id=user_id, display_name=display_name, event=event)
        )

    # --- serialisation ------------------------------------------------------

    def to_dict(self) -> dict:
        data = asdict(self)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Meeting":
        meeting = cls(
            guild_id=data["guild_id"],
            text_channel_id=data["text_channel_id"],
            voice_channel_id=data["voice_channel_id"],
            mode=data["mode"],
            started_by_id=data["started_by_id"],
            started_by_name=data["started_by_name"],
            started_at=data["started_at"],
            panel_message_id=data.get("panel_message_id"),
            # Legacy states (written before test meetings existed) are real.
            persistent=data.get("persistent", True),
            # Entries persisted before agenda owners existed are bare strings.
            agenda=[
                AgendaItem(text=e) if isinstance(e, str) else AgendaItem(**e)
                for e in data.get("agenda", [])
            ],
            agenda_index=data.get("agenda_index"),
            agenda_started=list(data.get("agenda_started", [])),
            timer_seconds=data.get("timer_seconds", 0),
            timer_alerted=data.get("timer_alerted", False),
            muted_user_ids=list(data.get("muted_user_ids", [])),
            ballot_muted_user_ids=list(data.get("ballot_muted_user_ids", [])),
        )
        meeting.queue = [QueueEntry(**e) for e in data.get("queue", [])]
        current = data.get("current")
        meeting.current = SpeakerTurn(**current) if current else None
        meeting.turns = [SpeakerTurn(**t) for t in data.get("turns", [])]
        meeting.logs = [LogEntry(**e) for e in data.get("logs", [])]
        meeting.motions = [MotionRecord(**m) for m in data.get("motions", [])]
        meeting.attendance = [AttendanceEvent(**a) for a in data.get("attendance", [])]
        return meeting


class Registry:
    """All live meetings plus the residual-mute ledger, persisted to disk.

    residual_mutes holds members the bot muted but could not unmute
    because they had disconnected from voice (Discord rejects mute edits
    for offline members). They are unmuted the moment they next join any
    voice channel.
    """

    def __init__(self, path: Path):
        self.path = path
        self.meetings: dict[int, Meeting] = {}
        self.residual_mutes: dict[int, set[int]] = {}

    def get(self, guild_id: int) -> Meeting | None:
        return self.meetings.get(guild_id)

    def save(self) -> None:
        payload = {
            "meetings": {str(gid): m.to_dict() for gid, m in self.meetings.items()},
            "residual_mutes": {
                str(gid): sorted(ids) for gid, ids in self.residual_mutes.items() if ids
            },
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    @classmethod
    def load(cls, path: Path) -> "Registry":
        registry = cls(path)
        if not path.exists():
            return registry
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return registry
        for gid, data in payload.get("meetings", {}).items():
            try:
                registry.meetings[int(gid)] = Meeting.from_dict(data)
            except (KeyError, TypeError):
                continue
        for gid, ids in payload.get("residual_mutes", {}).items():
            registry.residual_mutes[int(gid)] = set(ids)
        return registry
