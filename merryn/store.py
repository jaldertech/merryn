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


class ContinuityStore:
    """Per-guild open actions and agenda backlog, persisted to disk."""

    def __init__(self, path: Path):
        self.path = path
        self.actions: dict[int, list[OpenAction]] = {}
        self.backlog: dict[int, list[BacklogItem]] = {}

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
        return store
