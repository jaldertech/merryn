"""Cross-meeting continuity for Merryn: open actions and the agenda backlog.

Unlike Registry (live meeting state in state.json), this store OUTLIVES
individual meetings. It is Merryn's institutional memory:

  * Open action items recorded during a meeting survive its close and are
    surfaced at the start of the next meeting until a moderator marks them
    done.
  * Agenda items submitted between meetings by any member queue in a
    backlog and pre-populate the next meeting's agenda.

Persisted to DATA_DIR/continuity.json with the same atomic
write-then-rename Registry uses. All timestamps are UTC ISO-8601 strings;
conversion to the display timezone happens only at the presentation layer.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

UTC = timezone.utc


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class OpenAction:
    """An action item carried out of a meeting, awaiting completion."""

    text: str
    recorded_by: str
    meeting_date: str  # ISO timestamp of the meeting it originated in
    at: str = field(default_factory=now_iso)


@dataclass
class BacklogItem:
    """An agenda item proposed between meetings by any member."""

    text: str
    submitted_by: str
    submitted_by_id: int
    owner_id: int | None = None  # member due to present it, if named
    owner_name: str | None = None
    at: str = field(default_factory=now_iso)


@dataclass
class PendingReminder:
    """A pre-meeting ping still owed for a /meeting schedule event.

    Keyed by the Discord scheduled event's id, which is unique per guild
    and lets a restart tell a reminder already fired from one still due
    without needing its own separate id scheme.
    """

    event_id: int
    channel_id: int
    title: str
    start_at: str  # UTC ISO-8601, matching the rest of the codebase
    fired: bool = False


@dataclass
class PersonalReminder:
    """A member's own /remind — independent of any guild business.

    Delivered by DM; falls back to pinging the member in the channel it
    was set from if their DMs are closed. Not guild-keyed like the rest
    of this store, since delivery is to a user, not a chamber.
    """

    user_id: int
    guild_id: int
    channel_id: int
    text: str
    fire_at: str  # UTC ISO-8601
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    at: str = field(default_factory=now_iso)


@dataclass
class GuildSettings:
    """Standing configuration for a guild, independent of any meeting.

    Quorum lives here rather than on Meeting so that a server sets it once
    and every subsequent meeting inherits it; each meeting snapshots the
    values at open so a later change never rewrites past ballots.
    """

    quorum_enabled: bool = False
    quorum_size: int = 0
    # Whether a motion needs a second before it reaches a ballot. Off by
    # default — an opt-in stricter procedure, not a retroactive change to
    # how existing servers already run. /second enable|disable|show.
    second_required: bool = False


class ContinuityStore:
    """Per-guild open actions, agenda backlog and standing settings."""

    def __init__(self, path: Path):
        self.path = path
        self.actions: dict[int, list[OpenAction]] = {}
        self.backlog: dict[int, list[BacklogItem]] = {}
        self.settings: dict[int, GuildSettings] = {}
        # Pre-meeting pings still owed, per guild. Pruned once the meeting's
        # start time has passed, fired or not, so a cancelled or forgotten
        # event does not linger forever.
        self.reminders: dict[int, list[PendingReminder]] = {}
        # Members' own /remind reminders, flat rather than guild-keyed —
        # delivery is to a user via DM, independent of chamber business.
        self.personal_reminders: list[PersonalReminder] = []

    # --- actions ---------------------------------------------------------

    def open_actions(self, guild_id: int) -> list[OpenAction]:
        return self.actions.get(guild_id, [])

    def add_actions(self, guild_id: int, items: list[OpenAction]) -> None:
        if not items:
            return
        self.actions.setdefault(guild_id, []).extend(items)
        self.save()

    def complete_action(self, guild_id: int, index: int) -> OpenAction | None:
        """Removes the action at a 0-based index; returns it, or None if the
        index is out of range."""
        items = self.actions.get(guild_id, [])
        if index < 0 or index >= len(items):
            return None
        removed = items.pop(index)
        if not items:
            self.actions.pop(guild_id, None)
        self.save()
        return removed

    # --- agenda backlog --------------------------------------------------

    def backlog_items(self, guild_id: int) -> list[BacklogItem]:
        return self.backlog.get(guild_id, [])

    def add_backlog(self, guild_id: int, item: BacklogItem) -> int:
        """Appends an item; returns the new backlog length."""
        items = self.backlog.setdefault(guild_id, [])
        items.append(item)
        self.save()
        return len(items)

    def drop_backlog(self, guild_id: int, index: int) -> BacklogItem | None:
        """Removes the backlog item at a 0-based index; returns it, or None
        if the index is out of range."""
        items = self.backlog.get(guild_id, [])
        if index < 0 or index >= len(items):
            return None
        removed = items.pop(index)
        if not items:
            self.backlog.pop(guild_id, None)
        self.save()
        return removed

    def take_backlog(self, guild_id: int) -> list[BacklogItem]:
        """Removes and returns the whole backlog for a guild — called when a
        meeting opens and the backlog is brought forward into its agenda."""
        items = self.backlog.pop(guild_id, [])
        if items:
            self.save()
        return items

    # --- standing settings -----------------------------------------------

    def settings_for(self, guild_id: int) -> GuildSettings:
        """The guild's standing settings, defaulted but not yet persisted."""
        return self.settings.get(guild_id) or GuildSettings()

    def set_quorum_size(self, guild_id: int, size: int) -> GuildSettings:
        settings = self.settings.setdefault(guild_id, GuildSettings())
        settings.quorum_size = max(0, size)
        self.save()
        return settings

    def set_quorum_enabled(self, guild_id: int, enabled: bool) -> GuildSettings:
        settings = self.settings.setdefault(guild_id, GuildSettings())
        settings.quorum_enabled = enabled
        self.save()
        return settings

    def set_second_required(self, guild_id: int, enabled: bool) -> GuildSettings:
        settings = self.settings.setdefault(guild_id, GuildSettings())
        settings.second_required = enabled
        self.save()
        return settings

    # --- meeting reminders -------------------------------------------------

    def add_reminder(self, guild_id: int, reminder: PendingReminder) -> None:
        self.reminders.setdefault(guild_id, []).append(reminder)
        self.save()

    def mark_reminder_fired(self, guild_id: int, event_id: int) -> None:
        for r in self.reminders.get(guild_id, []):
            if r.event_id == event_id:
                r.fired = True
        self.save()

    def prune_reminder(self, guild_id: int, event_id: int) -> None:
        items = [
            r for r in self.reminders.get(guild_id, []) if r.event_id != event_id
        ]
        if items:
            self.reminders[guild_id] = items
        else:
            self.reminders.pop(guild_id, None)
        self.save()

    def iter_reminders(self):
        """Yields (guild_id, PendingReminder) for every reminder still
        tracked, across all guilds — the reminder tick has nothing else to
        key on ahead of time."""
        for gid, items in self.reminders.items():
            for r in items:
                yield gid, r

    # --- personal reminders ------------------------------------------------

    def add_personal_reminder(self, reminder: PersonalReminder) -> None:
        self.personal_reminders.append(reminder)
        self.save()

    def pop_personal_reminder(self, reminder_id: str) -> None:
        before = len(self.personal_reminders)
        self.personal_reminders = [
            r for r in self.personal_reminders if r.id != reminder_id
        ]
        if len(self.personal_reminders) != before:
            self.save()

    # --- persistence -----------------------------------------------------

    def save(self) -> None:
        payload = {
            "actions": {
                str(gid): [asdict(a) for a in items]
                for gid, items in self.actions.items()
                if items
            },
            "backlog": {
                str(gid): [asdict(b) for b in items]
                for gid, items in self.backlog.items()
                if items
            },
            "settings": {
                str(gid): asdict(s)
                for gid, s in self.settings.items()
                if s != GuildSettings()
            },
            "reminders": {
                str(gid): [asdict(r) for r in items]
                for gid, items in self.reminders.items()
                if items
            },
            "personal_reminders": [asdict(r) for r in self.personal_reminders],
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    @classmethod
    def load(cls, path: Path) -> "ContinuityStore":
        store = cls(path)
        if not path.exists():
            return store
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return store
        for gid, items in payload.get("actions", {}).items():
            try:
                store.actions[int(gid)] = [OpenAction(**a) for a in items]
            except (KeyError, TypeError):
                continue
        for gid, items in payload.get("backlog", {}).items():
            try:
                store.backlog[int(gid)] = [BacklogItem(**b) for b in items]
            except (KeyError, TypeError):
                continue
        for gid, values in payload.get("settings", {}).items():
            try:
                store.settings[int(gid)] = GuildSettings(**values)
            except (KeyError, TypeError):
                continue
        for gid, items in payload.get("reminders", {}).items():
            try:
                store.reminders[int(gid)] = [PendingReminder(**r) for r in items]
            except (KeyError, TypeError):
                continue
        for item in payload.get("personal_reminders", []):
            try:
                store.personal_reminders.append(PersonalReminder(**item))
            except (KeyError, TypeError):
                continue
        return store
