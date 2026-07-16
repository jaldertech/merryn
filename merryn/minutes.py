"""Renders end-of-meeting minutes as deterministic Markdown.

Minutes are assembled purely from events the bot observed during the
meeting, so the same meeting always produces the same minutes.
"""
from __future__ import annotations

import os
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .meeting import Meeting, iso_to_dt, now_iso


def _display_tz() -> ZoneInfo | None:
    """Timezone minutes are rendered in; None means the system's local time."""
    name = os.environ.get("MERRYN_TIMEZONE")
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            pass  # unknown name: fall back to system local time
    return None


DISPLAY_TZ = _display_tz()


def local(ts: str) -> str:
    return iso_to_dt(ts).astimezone(DISPLAY_TZ).strftime("%H:%M")


def local_date(ts: str) -> str:
    return iso_to_dt(ts).astimezone(DISPLAY_TZ).strftime("%A %d %B %Y")


def fmt_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds >= 3600:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    if seconds >= 60:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds}s"


def build_minutes(meeting: Meeting, ended_at: str | None = None) -> str:
    ended_at = ended_at or now_iso()
    duration = (iso_to_dt(ended_at) - iso_to_dt(meeting.started_at)).total_seconds()

    lines: list[str] = []
    lines.append(f"# Court Minutes — {local_date(meeting.started_at)}")
    lines.append("")
    lines.append(f"- **Convened:** {local(meeting.started_at)}")
    lines.append(f"- **Adjourned:** {local(ended_at)}")
    lines.append(f"- **Duration:** {fmt_duration(duration)}")
    lines.append(f"- **Presiding:** {meeting.started_by_name}")
    lines.append(f"- **Mode:** {meeting.mode}")
    lines.append("")

    # --- Attendance ---
    present = [a for a in meeting.attendance if a.event == "present"]
    joins = [a for a in meeting.attendance if a.event == "join"]
    leaves = [a for a in meeting.attendance if a.event == "leave"]
    lines.append("## Attendance")
    if present:
        names = ", ".join(sorted({a.display_name for a in present}))
        lines.append(f"- **Present at start:** {names}")
    else:
        lines.append("- **Present at start:** none recorded")
    for a in joins:
        lines.append(f"- Joined {local(a.at)}: {a.display_name}")
    for a in leaves:
        lines.append(f"- Left {local(a.at)}: {a.display_name}")
    lines.append("")

    # --- Agenda ---
    if meeting.agenda:
        lines.append("## Agenda")
        reached = (
            meeting.agenda_index if meeting.agenda_index is not None else -1
        )
        for i, item in enumerate(meeting.agenda):
            if i < reached:
                status = "✅"
            elif i == reached:
                status = "✅" if reached >= len(meeting.agenda) else "▶️ (in progress at close)"
            else:
                status = "⏳ not reached"
            started = (
                f" — opened {local(meeting.agenda_started[i])}"
                if i < len(meeting.agenda_started)
                else ""
            )
            owner = f" (presented by {item.owner_name})" if item.owner_name else ""
            lines.append(f"{i + 1}. {item.text}{owner} — {status}{started}")
        lines.append("")

    # --- Speakers ---
    lines.append("## Speakers")
    totals: dict[str, float] = defaultdict(float)
    counts: dict[str, int] = defaultdict(int)
    for turn in meeting.turns:
        totals[turn.display_name] += turn.seconds()
        counts[turn.display_name] += 1
    if totals:
        for name, secs in sorted(totals.items(), key=lambda kv: -kv[1]):
            turns = counts[name]
            lines.append(
                f"- **{name}** — {counts[name]} turn{'s' if turns != 1 else ''}, "
                f"{fmt_duration(secs)} total"
            )
        lines.append("")
        lines.append("### Speaking order")
        for turn in meeting.turns:
            marker = " ⚡ (point of order)" if turn.point_of_order else ""
            lines.append(
                f"- {local(turn.started_at)} — {turn.display_name} "
                f"({fmt_duration(turn.seconds())}){marker}"
            )
    else:
        lines.append("- No speakers were called via the queue.")
    lines.append("")

    # --- Motions ---
    if meeting.motions:
        lines.append("## Motions")
        for m in meeting.motions:
            pct = m.percent_in_favour()
            req = (
                f" of {m.pass_threshold}% required"
                if m.pass_threshold is not None
                else ""
            )
            pct_note = f", {pct}% in favour{req}" if pct is not None else ""
            abst_pct = m.percent_abstained()
            abst_note = (
                f", {abst_pct}% abstained" if abst_pct is not None else ""
            )
            lines.append(
                f"- {local(m.at)} — “{m.text}” (moved by {m.moved_by}) — "
                f"**{m.outcome.upper()}** (✅ {m.yes} / ❌ {m.no}{pct_note}{abst_note})"
            )
        lines.append("")

    # --- Decisions / actions / notes ---
    for kind, heading in (("decision", "Decisions"), ("action", "Actions"), ("note", "Notes")):
        entries = [e for e in meeting.logs if e.kind == kind]
        if not entries:
            continue
        lines.append(f"## {heading}")
        for e in entries:
            context = ""
            if e.agenda_index is not None and e.agenda_index < len(meeting.agenda):
                context = f" _(re: {meeting.agenda[e.agenda_index].text})_"
            lines.append(f"- {local(e.at)} — {e.text} — _{e.author}_{context}")
        lines.append("")

    lines.append("---")
    lines.append("_Recorded by Merryn._")
    return "\n".join(lines)


def filename_for(meeting: Meeting) -> str:
    started = iso_to_dt(meeting.started_at).astimezone(DISPLAY_TZ)
    return f"minutes_{started.strftime('%Y%m%d_%H%M')}.md"
