"""Merryn — Discord meeting moderator.

Speaking queue with optional server-mute enforcement, agenda tracking,
motions with timed ballots, per-speaker timers, attendance, and
deterministic end-of-meeting minutes.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import sys
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import tasks

from .audio import LoopingWAVAudio, ensure_opus, resolve_hold_music
from .meeting import (
    MODE_ADVISORY,
    MODE_STRICT,
    Meeting,
    MotionRecord,
    Registry,
    SpeakerTurn,
    now_iso,
)
from .minutes import build_minutes, filename_for, fmt_duration
from .views import ConfirmAdjournView, MotionView, PanelView

log = logging.getLogger("merryn")


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines, # comments. Values already
    present in the environment always win."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


_load_dotenv(Path(".env"))

DATA_DIR = Path(os.environ.get("DATA_DIR", "merryn-data"))
TOKEN = os.environ.get("DISCORD_TOKEN", "")
GUILD_ID = int(os.environ["GUILD_ID"]) if os.environ.get("GUILD_ID") else None
MOD_ROLE_ID = int(os.environ["MOD_ROLE_ID"]) if os.environ.get("MOD_ROLE_ID") else None

QUEUE_DISPLAY_CAP = 15
PANEL_TITLE = "Meeting in session"
# Coalesces bursts of messages into one delete-and-repost of the panel.
PANEL_BUMP_DELAY_SECONDS = 2.0

MOTIVATION_QUOTES = [
    "You've got it, buddy!",
    "You can do anything!",
    "Believe in yourself. I do.",
    "Today is your day. Seize it!",
    "You are stronger than you know.",
    "Every great deed starts with showing up — and you showed up!",
    "Keep going. You're closer than you think.",
    "The council believes in you. Officially. It's minuted.",
    "Motion to declare you brilliant: carried unanimously.",
    "Point of order: you're doing great.",
    "Onwards! Glory awaits!",
    "You're doing better than you think you are.",
    "Small steps still move the agenda forward.",
    "Chin up! The agenda of life has many items left.",
    "You have the floor. Make it count!",
    "Someone has to be magnificent today. Might as well be you.",
    "Your effort has been recorded in the minutes of history.",
    "I've seen many speakers. You're one of the good ones.",
    "Rise and conquer, friend!",
    "No storage concerns can dim your shine.",
    "Fortune favours the bold. Be bold!",
    "You are, frankly, tremendous.",
    "Take a deep breath. You've handled worse.",
    "Adjourn your doubts. The motion of confidence is carried.",
]


def is_moderator(member: discord.Member) -> bool:
    if member.guild_permissions.manage_guild:
        return True
    if MOD_ROLE_ID is not None:
        return any(role.id == MOD_ROLE_ID for role in member.roles)
    return False


def in_meeting_voice(member: discord.Member, meeting: Meeting) -> bool:
    voice = member.voice
    return bool(voice and voice.channel and voice.channel.id == meeting.voice_channel_id)


class Merryn(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True  # needed to see who is in the voice channel
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.registry: Registry = Registry(DATA_DIR / "state.json")
        self._motion_tasks: dict[int, set[asyncio.Task]] = {}
        self._panel_bumps: dict[int, asyncio.Task] = {}
        self._open_ballots: dict[int, MotionView] = {}

    # --- lifecycle ---------------------------------------------------------

    async def setup_hook(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        (DATA_DIR / "minutes").mkdir(exist_ok=True)
        self.registry = Registry.load(DATA_DIR / "state.json")
        self.add_view(PanelView(self))

        self.tree.add_command(meeting_group)
        self.tree.add_command(agenda_group)
        self.tree.add_command(floor_group)
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

        self.panel_tick.start()

    async def on_ready(self) -> None:
        log.info("Logged in as %s (id %s)", self.user, self.user.id)
        for meeting in list(self.registry.meetings.values()):
            log.info(
                "Resumed active meeting in guild %s (started %s)",
                meeting.guild_id,
                meeting.started_at,
            )
            guild = self.get_guild(meeting.guild_id)
            if guild is not None and meeting.ballot_muted_user_ids:
                # A ballot was open at shutdown; it is voided rather than
                # resumed, so its mutes must not outlive it.
                await self._ballot_unmute(meeting, guild)
            await self.refresh_panel(meeting)

    # --- panel -------------------------------------------------------------

    def build_panel_embed(self, meeting: Meeting) -> discord.Embed:
        strict = meeting.mode == MODE_STRICT
        embed = discord.Embed(
            title=PANEL_TITLE,
            colour=discord.Colour.dark_red(),
            description=(
                f"Presiding: **{meeting.started_by_name}** · "
                f"Mode: **{'strict (mute enforced)' if strict else 'advisory'}**"
            ),
        )

        item = meeting.current_agenda_item()
        if meeting.agenda:
            shown = []
            for i, entry in enumerate(meeting.agenda):
                label = f"{i + 1}. {entry.text}"
                if entry.owner_name:
                    label += f" · {entry.owner_name}"
                if meeting.agenda_index is not None and i < meeting.agenda_index:
                    shown.append(f"~~{label}~~")
                elif i == meeting.agenda_index:
                    shown.append(f"**▶ {label}**")
                else:
                    shown.append(label)
            if item is None and meeting.agenda_index is not None:
                shown.append("_Agenda complete._")
            embed.add_field(name="Agenda", value="\n".join(shown)[:1024], inline=False)

        if meeting.current:
            elapsed = fmt_duration(meeting.current.seconds())
            limit = ""
            if meeting.timer_seconds:
                over = meeting.current.seconds() > meeting.timer_seconds
                limit = f" / {fmt_duration(meeting.timer_seconds)}" + (" ⏰" if over else "")
            flag = " ⚡" if meeting.current.point_of_order else ""
            value = f"**{meeting.current.display_name}**{flag} — {elapsed}{limit}"
        else:
            value = "_The floor is open._"
        embed.add_field(name="Now speaking", value=value, inline=False)

        ordered = meeting.sorted_queue()
        if ordered:
            rows = [
                f"{i + 1}. {'⚡ ' if e.point_of_order else ''}{e.display_name}"
                for i, e in enumerate(ordered[:QUEUE_DISPLAY_CAP])
            ]
            if len(ordered) > QUEUE_DISPLAY_CAP:
                rows.append(f"… and {len(ordered) - QUEUE_DISPLAY_CAP} more")
            embed.add_field(name="Queue", value="\n".join(rows)[:1024], inline=False)
        else:
            embed.add_field(name="Queue", value="_Empty._", inline=False)

        embed.set_footer(text="✋ raise your hand to speak · ⚡ point of order jumps the queue")
        return embed

    async def refresh_panel(self, meeting: Meeting) -> None:
        guild = self.get_guild(meeting.guild_id)
        if guild is None:
            return
        channel = guild.get_channel(meeting.text_channel_id)
        if channel is None or meeting.panel_message_id is None:
            return
        embed = self.build_panel_embed(meeting)
        try:
            await channel.get_partial_message(meeting.panel_message_id).edit(embed=embed)
        except discord.NotFound:
            # Panel was deleted; repost it so the buttons stay available.
            message = await channel.send(embed=embed, view=PanelView(self))
            meeting.panel_message_id = message.id
            self.registry.save()
        except discord.HTTPException as exc:
            log.warning("Panel refresh failed: %s", exc)

    @tasks.loop(seconds=10)
    async def panel_tick(self) -> None:
        for meeting in list(self.registry.meetings.values()):
            if meeting.current is None:
                continue
            await self.check_speaker_timer(meeting)
            await self.refresh_panel(meeting)

    @panel_tick.before_loop
    async def before_panel_tick(self) -> None:
        await self.wait_until_ready()

    # --- sticky panel --------------------------------------------------------
    # Discord cannot pin a message to the bottom of a channel, so the panel
    # is deleted and reposted whenever other messages land, keeping it the
    # newest message in the meeting channel.

    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None:
            return
        meeting = self.registry.get(message.guild.id)
        if meeting is None or message.channel.id != meeting.text_channel_id:
            return
        if message.author.id == self.user.id:
            if message.id == meeting.panel_message_id:
                return
            # A panel we have just reposted, before its id was recorded.
            if any(e.title == PANEL_TITLE for e in message.embeds):
                return
        self._schedule_panel_bump(meeting)

    def _schedule_panel_bump(self, meeting: Meeting) -> None:
        existing = self._panel_bumps.get(meeting.guild_id)
        if existing and not existing.done():
            return  # a repost is already pending; it will land after this message
        self._panel_bumps[meeting.guild_id] = asyncio.create_task(
            self._bump_panel(meeting)
        )

    async def _bump_panel(self, meeting: Meeting) -> None:
        await asyncio.sleep(PANEL_BUMP_DELAY_SECONDS)
        if self.registry.get(meeting.guild_id) is not meeting:
            return  # meeting ended while the bump was pending
        guild = self.get_guild(meeting.guild_id)
        channel = guild.get_channel(meeting.text_channel_id) if guild else None
        if channel is None:
            return
        old_id = meeting.panel_message_id
        try:
            message = await channel.send(
                embed=self.build_panel_embed(meeting), view=PanelView(self)
            )
        except discord.HTTPException as exc:
            log.warning("Panel bump failed: %s", exc)
            return
        meeting.panel_message_id = message.id
        self.registry.save()
        if old_id:
            try:
                await channel.get_partial_message(old_id).delete()
            except discord.HTTPException:
                pass

    async def check_speaker_timer(self, meeting: Meeting) -> None:
        if (
            not meeting.timer_seconds
            or meeting.current is None
            or meeting.timer_alerted
            or meeting.current.seconds() <= meeting.timer_seconds
        ):
            return
        meeting.timer_alerted = True
        self.registry.save()
        guild = self.get_guild(meeting.guild_id)
        channel = guild.get_channel(meeting.text_channel_id) if guild else None
        if channel:
            await channel.send(
                f"⏰ **{meeting.current.display_name}** has reached the "
                f"{fmt_duration(meeting.timer_seconds)} speaking limit. "
                f"<@{meeting.started_by_id}>"
            )

    # --- muting ------------------------------------------------------------

    async def try_set_mute(self, member: discord.Member, mute: bool) -> bool:
        try:
            await member.edit(mute=mute, reason="Merryn meeting moderation")
            return True
        except discord.HTTPException as exc:
            log.warning("Could not set mute=%s on %s: %s", mute, member, exc)
            return False

    async def enforce_strict(self, meeting: Meeting, guild: discord.Guild) -> int:
        """Mutes everyone in the meeting VC except moderators and the
        current speaker. Returns the number of members it could not mute."""
        channel = guild.get_channel(meeting.voice_channel_id)
        if channel is None:
            return 0
        failures = 0
        for member in channel.members:
            if member.bot or is_moderator(member):
                continue
            if meeting.current and meeting.current.user_id == member.id:
                continue
            if member.voice and member.voice.mute:
                continue
            if await self.try_set_mute(member, True):
                if member.id not in meeting.muted_user_ids:
                    meeting.muted_user_ids.append(member.id)
            else:
                failures += 1
        self.registry.save()
        return failures

    async def lift_all_mutes(self, meeting: Meeting, guild: discord.Guild) -> None:
        """Unmutes everyone we muted. Members no longer in voice cannot be
        edited, so they go on the residual ledger and are unmuted on their
        next voice join."""
        residual = self.registry.residual_mutes.setdefault(meeting.guild_id, set())
        for user_id in list(meeting.muted_user_ids):
            member = guild.get_member(user_id)
            if member and member.voice and member.voice.channel:
                if await self.try_set_mute(member, False):
                    meeting.muted_user_ids.remove(user_id)
                    continue
            meeting.muted_user_ids.remove(user_id)
            residual.add(user_id)
        self.registry.save()

    # --- voice-state tracking ------------------------------------------------

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if member.bot:
            return
        guild_id = member.guild.id
        meeting = self.registry.get(guild_id)
        residual = self.registry.residual_mutes.get(guild_id)

        # Clear residual mutes from past sessions as soon as we can.
        if residual and member.id in residual and after.channel is not None:
            joining_active_vc = bool(
                meeting and after.channel.id == meeting.voice_channel_id
            )
            if not joining_active_vc and await self.try_set_mute(member, False):
                residual.discard(member.id)
                self.registry.save()

        if meeting is None:
            return

        vc_id = meeting.voice_channel_id
        was_in = bool(before.channel and before.channel.id == vc_id)
        now_in = bool(after.channel and after.channel.id == vc_id)

        if now_in and not was_in:
            meeting.record_attendance(member.id, member.display_name, "join")
            if residual:
                residual.discard(member.id)
            if (
                meeting.mode == MODE_STRICT
                and not is_moderator(member)
                and not (meeting.current and meeting.current.user_id == member.id)
            ):
                if await self.try_set_mute(member, True):
                    if member.id not in meeting.muted_user_ids:
                        meeting.muted_user_ids.append(member.id)
            open_ballot = self._open_ballots.get(guild_id)
            if (
                open_ballot is not None
                and not open_ballot.is_finished()
                and member.id not in meeting.muted_user_ids
            ):
                # The chamber is voting: joiners are muted like everyone else.
                if await self.try_set_mute(member, True):
                    if member.id not in meeting.ballot_muted_user_ids:
                        meeting.ballot_muted_user_ids.append(member.id)
            self.registry.save()
            await self.refresh_panel(meeting)

        elif was_in and not now_in:
            meeting.record_attendance(member.id, member.display_name, "leave")
            entry = meeting.find_queued(member.id)
            if entry:
                meeting.queue.remove(entry)
            if meeting.current and meeting.current.user_id == member.id:
                meeting.end_current_turn()
            we_muted = member.id in meeting.muted_user_ids or (
                member.id in meeting.ballot_muted_user_ids
            )
            if member.id in meeting.muted_user_ids:
                meeting.muted_user_ids.remove(member.id)
            if member.id in meeting.ballot_muted_user_ids:
                meeting.ballot_muted_user_ids.remove(member.id)
            if we_muted:
                if after.channel is not None:
                    # Moved to another channel: unmute there and then.
                    if not await self.try_set_mute(member, False):
                        self.registry.residual_mutes.setdefault(guild_id, set()).add(
                            member.id
                        )
                else:
                    # Disconnected: cannot edit mute until they return.
                    self.registry.residual_mutes.setdefault(guild_id, set()).add(
                        member.id
                    )
            self.registry.save()
            await self.refresh_panel(meeting)

    # --- shared handlers (buttons and commands both land here) ---------------

    async def _require_meeting(
        self, interaction: discord.Interaction
    ) -> Meeting | None:
        meeting = self.registry.get(interaction.guild_id)
        if meeting is None:
            await interaction.response.send_message(
                "No meeting is in session.", ephemeral=True
            )
        return meeting

    async def _require_moderator(self, interaction: discord.Interaction) -> bool:
        if is_moderator(interaction.user):
            return True
        await interaction.response.send_message(
            "Moderators only.", ephemeral=True
        )
        return False

    async def handle_hand(self, interaction: discord.Interaction) -> None:
        meeting = await self._require_meeting(interaction)
        if meeting is None:
            return
        member = interaction.user
        if not in_meeting_voice(member, meeting):
            await interaction.response.send_message(
                "Join the meeting voice channel first.", ephemeral=True
            )
            return
        if meeting.current and meeting.current.user_id == member.id:
            await interaction.response.send_message(
                "You already have the floor.", ephemeral=True
            )
            return
        raised = meeting.toggle_hand(member.id, member.display_name)
        self.registry.save()
        if raised:
            position = [e.user_id for e in meeting.sorted_queue()].index(member.id) + 1
            text = f"Hand raised — you are #{position} in the queue."
        else:
            text = "Hand lowered."
        await interaction.response.send_message(text, ephemeral=True)
        await self.refresh_panel(meeting)

    async def handle_point_of_order(self, interaction: discord.Interaction) -> None:
        meeting = await self._require_meeting(interaction)
        if meeting is None:
            return
        member = interaction.user
        if not in_meeting_voice(member, meeting):
            await interaction.response.send_message(
                "Join the meeting voice channel first.", ephemeral=True
            )
            return
        if not meeting.raise_point_of_order(member.id, member.display_name):
            await interaction.response.send_message(
                "Your point of order is already pending.", ephemeral=True
            )
            return
        self.registry.save()
        await interaction.response.send_message(
            f"⚡ **{member.display_name}** raises a point of order. "
            f"<@{meeting.started_by_id}>"
        )
        await self.refresh_panel(meeting)

    async def strict_mute_swap(
        self,
        meeting: Meeting,
        guild: discord.Guild,
        previous: SpeakerTurn | None,
        current: SpeakerTurn | None,
    ) -> None:
        """Applies the strict-mode mute swap for a change of speaker."""
        if meeting.mode != MODE_STRICT:
            return
        if previous is not None:
            prev_member = guild.get_member(previous.user_id)
            if (
                prev_member
                and in_meeting_voice(prev_member, meeting)
                and not is_moderator(prev_member)
            ):
                if await self.try_set_mute(prev_member, True):
                    if prev_member.id not in meeting.muted_user_ids:
                        meeting.muted_user_ids.append(prev_member.id)
        if current is not None:
            new_member = guild.get_member(current.user_id)
            if new_member and in_meeting_voice(new_member, meeting):
                if await self.try_set_mute(new_member, False):
                    if new_member.id in meeting.muted_user_ids:
                        meeting.muted_user_ids.remove(new_member.id)
        self.registry.save()

    async def handle_next(self, interaction: discord.Interaction) -> None:
        meeting = await self._require_meeting(interaction)
        if meeting is None or not await self._require_moderator(interaction):
            return
        previous, current = meeting.next_speaker()
        self.registry.save()
        await self.strict_mute_swap(meeting, interaction.guild, previous, current)

        if current is None:
            await interaction.response.send_message(
                "The queue is empty — the floor is open."
            )
        else:
            flag = " (point of order)" if current.point_of_order else ""
            await interaction.response.send_message(
                f"🔔 **{current.display_name}** has the floor.{flag}"
            )
        await self.refresh_panel(meeting)

    async def handle_floor_give(
        self, interaction: discord.Interaction, member: discord.Member
    ) -> None:
        meeting = await self._require_meeting(interaction)
        if meeting is None or not await self._require_moderator(interaction):
            return
        if member.bot:
            await interaction.response.send_message(
                "Bots do not take the floor.", ephemeral=True
            )
            return
        if not in_meeting_voice(member, meeting):
            await interaction.response.send_message(
                f"{member.display_name} is not in the meeting voice channel.",
                ephemeral=True,
            )
            return
        if meeting.current and meeting.current.user_id == member.id:
            await interaction.response.send_message(
                f"{member.display_name} already has the floor.", ephemeral=True
            )
            return
        previous, current = meeting.give_floor(member.id, member.display_name)
        self.registry.save()
        await self.strict_mute_swap(meeting, interaction.guild, previous, current)
        await interaction.response.send_message(
            f"🔔 **{current.display_name}** has the floor (given by the chair)."
        )
        await self.refresh_panel(meeting)

    async def handle_agenda_next(self, interaction: discord.Interaction) -> None:
        meeting = await self._require_meeting(interaction)
        if meeting is None or not await self._require_moderator(interaction):
            return
        item = meeting.advance_agenda()
        self.registry.save()
        if item is None:
            if not meeting.agenda:
                await interaction.response.send_message(
                    "No agenda has been set. Use /agenda add.", ephemeral=True
                )
            else:
                await interaction.response.send_message("📜 Agenda complete.")
            await self.refresh_panel(meeting)
            return

        lines = [f"📜 Item {meeting.agenda_index + 1}: **{item.text}**"]
        if item.owner_id is not None:
            owner = interaction.guild.get_member(item.owner_id)
            if owner is None or not in_meeting_voice(owner, meeting):
                lines.append(
                    f"⚠️ Presenter **{item.owner_name}** is not in the meeting "
                    "voice channel — the floor is unchanged."
                )
            elif meeting.current and meeting.current.user_id == owner.id:
                lines.append(f"🔔 **{owner.display_name}** already has the floor.")
            else:
                previous, current = meeting.give_floor(
                    owner.id, owner.display_name
                )
                self.registry.save()
                await self.strict_mute_swap(
                    meeting, interaction.guild, previous, current
                )
                lines.append(f"🔔 **{current.display_name}** has the floor.")
        await interaction.response.send_message("\n".join(lines))
        await self.refresh_panel(meeting)

    async def handle_adjourn_request(self, interaction: discord.Interaction) -> None:
        meeting = await self._require_meeting(interaction)
        if meeting is None or not await self._require_moderator(interaction):
            return
        await interaction.response.send_message(
            "End the meeting and publish the minutes?",
            view=ConfirmAdjournView(self),
            ephemeral=True,
        )

    async def end_meeting(self, interaction: discord.Interaction) -> None:
        meeting = self.registry.meetings.pop(interaction.guild_id, None)
        if meeting is None:
            await interaction.response.send_message(
                "No meeting is in session.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)

        bump = self._panel_bumps.pop(interaction.guild_id, None)
        if bump is not None:
            bump.cancel()
        for task in self._motion_tasks.pop(interaction.guild_id, set()):
            task.cancel()
        ballot = self._open_ballots.pop(interaction.guild_id, None)
        if ballot is not None:
            ballot.stop()
        for motion in meeting.motions:
            if motion.outcome == "open":
                motion.outcome = "void"

        meeting.end_current_turn()
        ended_at = now_iso()
        await self._stop_ballot_ambience(meeting, interaction.guild)
        await self.lift_all_mutes(meeting, interaction.guild)
        self.registry.save()

        text = build_minutes(meeting, ended_at)
        path = DATA_DIR / "minutes" / filename_for(meeting)
        path.write_text(text, encoding="utf-8")

        channel = interaction.guild.get_channel(meeting.text_channel_id)
        target = channel or interaction.channel
        await target.send(
            "Meeting adjourned. Minutes attached.",
            file=discord.File(path, filename=path.name),
        )
        await interaction.followup.send("Done.", ephemeral=True)

        # Retire the panel so its buttons stop inviting clicks.
        if meeting.panel_message_id and channel:
            try:
                await channel.get_partial_message(meeting.panel_message_id).edit(
                    content="_This meeting has ended._", embed=None, view=None
                )
            except discord.HTTPException:
                pass

    # --- ballot ambience ----------------------------------------------------
    # While a ballot is open the chamber is muted and Merryn plays hold
    # music in the voice channel. Ballot mutes are tracked separately from
    # strict-mode mutes (ballot_muted_user_ids only ever holds members who
    # were NOT already muted when it opened) so closing the ballot restores
    # exactly the pre-ballot state in either mode.

    async def _ballot_mute_all(self, meeting: Meeting, guild: discord.Guild) -> None:
        channel = guild.get_channel(meeting.voice_channel_id)
        if channel is None:
            return
        for member in channel.members:
            if member.bot:
                continue
            if member.voice and member.voice.mute:
                continue
            if await self.try_set_mute(member, True):
                if member.id not in meeting.ballot_muted_user_ids:
                    meeting.ballot_muted_user_ids.append(member.id)
        self.registry.save()

    async def _ballot_unmute(self, meeting: Meeting, guild: discord.Guild) -> None:
        if not meeting.ballot_muted_user_ids:
            return
        residual = self.registry.residual_mutes.setdefault(meeting.guild_id, set())
        for user_id in list(meeting.ballot_muted_user_ids):
            member = guild.get_member(user_id)
            if member and member.voice and member.voice.channel:
                if await self.try_set_mute(member, False):
                    meeting.ballot_muted_user_ids.remove(user_id)
                    continue
            meeting.ballot_muted_user_ids.remove(user_id)
            residual.add(user_id)
        self.registry.save()

    async def _start_ballot_ambience(
        self, meeting: Meeting, guild: discord.Guild
    ) -> None:
        """Mutes the chamber and starts the hold music. Playback is
        best-effort: a ballot must never fail because voice did."""
        await self._ballot_mute_all(meeting, guild)
        channel = guild.get_channel(meeting.voice_channel_id)
        if channel is None:
            return
        path = resolve_hold_music()
        if not path.exists():
            log.warning("Hold music file missing: %s", path)
            return
        if not ensure_opus():
            log.warning("libopus not found — hold music disabled")
            return
        try:
            source = LoopingWAVAudio(path)
            vc = guild.voice_client
            if vc is None:
                vc = await channel.connect(self_deaf=True)
            elif vc.channel != channel:
                await vc.move_to(channel)
            if vc.is_playing():
                vc.stop()
            vc.play(source)
        except Exception as exc:
            log.warning("Hold music unavailable: %s", exc)

    async def _stop_ballot_ambience(
        self, meeting: Meeting | None, guild: discord.Guild
    ) -> None:
        vc = guild.voice_client
        if vc is not None:
            try:
                await vc.disconnect(force=True)
            except Exception as exc:
                log.warning("Voice disconnect failed: %s", exc)
        if meeting is not None:
            await self._ballot_unmute(meeting, guild)

    # --- motions ----------------------------------------------------------

    def _eligible_voter_count(
        self, guild: discord.Guild, voice_channel_id: int
    ) -> int:
        channel = guild.get_channel(voice_channel_id)
        if channel is None:
            return 0
        return sum(1 for member in channel.members if not member.bot)

    def build_ballot_embed(
        self, guild: discord.Guild, record: MotionRecord, view: MotionView
    ) -> discord.Embed:
        embed = discord.Embed(
            title="Motion",
            description=record.text,
            colour=discord.Colour.dark_gold(),
        )
        cast = len(view.votes)
        eligible = self._eligible_voter_count(guild, view.voice_channel_id)
        requirement = (
            f"**{record.pass_threshold}%** in favour"
            if record.pass_threshold is not None
            else "a simple majority"
        )
        lines = [
            f"Moved by **{record.moved_by}** · closes <t:{view.closes_at_unix}:R>",
            f"Carries on {requirement}",
            f"Votes cast: **{cast} / {eligible}** (anonymous)",
        ]
        if eligible and cast >= eligible:
            lines.append(
                "🔨 Everyone present has voted — the chair may close the ballot early."
            )
        embed.add_field(name="Ballot", value="\n".join(lines))
        return embed

    async def open_motion(
        self,
        interaction: discord.Interaction,
        meeting: Meeting,
        text: str,
        seconds: int,
        pass_threshold: int | None = None,
    ) -> None:
        existing = self._open_ballots.get(interaction.guild_id)
        if existing is not None and not existing.is_finished():
            await interaction.response.send_message(
                "A ballot is already open — one motion at a time.", ephemeral=True
            )
            return

        record = MotionRecord(
            text=text,
            moved_by=interaction.user.display_name,
            pass_threshold=pass_threshold,
        )
        meeting.motions.append(record)
        self.registry.save()

        view = MotionView(self, meeting.voice_channel_id)
        view.record = record
        view.guild_id = interaction.guild_id
        view.closes_at_unix = int(discord.utils.utcnow().timestamp()) + seconds
        await interaction.response.send_message(
            embed=self.build_ballot_embed(interaction.guild, record, view), view=view
        )
        view.message = await interaction.original_response()
        self._open_ballots[interaction.guild_id] = view

        task = asyncio.create_task(self._close_motion_later(view, seconds))
        view.close_task = task
        self._motion_tasks.setdefault(interaction.guild_id, set()).add(task)
        task.add_done_callback(
            lambda t: self._motion_tasks.get(interaction.guild_id, set()).discard(t)
        )

        await self._start_ballot_ambience(meeting, interaction.guild)

    async def update_ballot_tally(
        self, guild: discord.Guild, view: MotionView
    ) -> None:
        if view.is_finished() or view.message is None:
            return
        try:
            await view.message.edit(
                embed=self.build_ballot_embed(guild, view.record, view)
            )
        except discord.HTTPException as exc:
            log.warning("Ballot tally update failed: %s", exc)

    async def handle_ballot_close(
        self, interaction: discord.Interaction, view: MotionView
    ) -> None:
        if not await self._require_moderator(interaction):
            return
        if view.is_finished() or view.record.outcome != "open":
            await interaction.response.send_message(
                "The ballot has already closed.", ephemeral=True
            )
            return
        if view.close_task is not None:
            view.close_task.cancel()
        await interaction.response.send_message(
            "🔨 The chair has closed the ballot early."
        )
        await self._finalise_motion(view, closed_early=True)

    async def _close_motion_later(self, view: MotionView, seconds: int) -> None:
        await asyncio.sleep(seconds)
        await self._finalise_motion(view)

    async def _finalise_motion(
        self, view: MotionView, closed_early: bool = False
    ) -> None:
        # The outcome check stops a late button click from overwriting a
        # motion already voided by /meeting end.
        if view.is_finished() or view.record.outcome != "open":
            return
        view.stop()
        record = view.record
        record.yes = sum(1 for v in view.votes.values() if v == "aye")
        record.no = sum(1 for v in view.votes.values() if v == "nay")
        guild = self.get_guild(view.guild_id)
        if guild is not None:
            # Snapshot the head-count at close so abstentions survive into
            # the minutes. A voter who left after voting still counts.
            record.eligible = max(
                self._eligible_voter_count(guild, view.voice_channel_id),
                record.yes + record.no,
            )
        if record.pass_threshold is not None:
            # Compared against the same rounded figure that is displayed,
            # so the announced percentage and the outcome can never disagree.
            pct = record.percent_in_favour()
            record.outcome = (
                "carried"
                if pct is not None and pct >= record.pass_threshold
                else "failed"
            )
        elif record.yes > record.no:
            record.outcome = "carried"
        elif record.no > record.yes:
            record.outcome = "failed"
        else:
            record.outcome = "tied"
        self.registry.save()

        if guild is not None:
            await self._stop_ballot_ambience(self.registry.get(view.guild_id), guild)

        embed = discord.Embed(
            title="Motion — ballot closed",
            description=record.text,
            colour=discord.Colour.dark_gold(),
        )
        note = " (closed early)" if closed_early else ""
        tally = f"✅ {record.yes} · ❌ {record.no}"
        pct = record.percent_in_favour()
        requirement = (
            f"{record.pass_threshold}% required"
            if record.pass_threshold is not None
            else "simple majority required"
        )
        if pct is not None:
            tally += f" — **{pct}%** in favour ({requirement})"
        abst_pct = record.percent_abstained()
        if abst_pct is not None:
            tally += (
                f"\n⚪ {record.abstained()} of {record.eligible} present "
                f"did not vote (**{abst_pct}%** abstained)"
            )
        embed.add_field(
            name=f"Result: {record.outcome.upper()}{note}",
            value=tally,
        )
        try:
            await view.message.edit(embed=embed, view=None)
        except discord.HTTPException as exc:
            log.warning("Could not close motion message: %s", exc)


client = Merryn()


# --- slash commands ---------------------------------------------------------

meeting_group = app_commands.Group(
    name="meeting", description="Start, control, and end meetings", guild_only=True
)
agenda_group = app_commands.Group(
    name="agenda", description="Manage the agenda", guild_only=True
)
floor_group = app_commands.Group(
    name="floor", description="Control who has the floor", guild_only=True
)


@floor_group.command(
    name="give", description="Give the floor directly to a member, bypassing the queue"
)
@app_commands.describe(member="The member to recognise")
async def floor_give(
    interaction: discord.Interaction, member: discord.Member
) -> None:
    bot: Merryn = interaction.client
    await bot.handle_floor_give(interaction, member)


@meeting_group.command(name="start", description="Open a meeting in your voice channel")
@app_commands.describe(
    mode="Strict mutes everyone except the recognised speaker; advisory only tracks the queue",
    agenda="Agenda items separated by semicolons, e.g. 'Treasury; New members; Feast planning'",
    voice_channel="Meeting voice channel (defaults to the one you are in)",
)
@app_commands.choices(
    mode=[
        app_commands.Choice(name="Strict — enforce muting", value=MODE_STRICT),
        app_commands.Choice(name="Advisory — queue only", value=MODE_ADVISORY),
    ]
)
async def meeting_start(
    interaction: discord.Interaction,
    mode: app_commands.Choice[str],
    agenda: str | None = None,
    voice_channel: discord.VoiceChannel | None = None,
) -> None:
    bot: Merryn = interaction.client
    if not await bot._require_moderator(interaction):
        return
    if bot.registry.get(interaction.guild_id) is not None:
        await interaction.response.send_message(
            "A meeting is already in session. End it first.", ephemeral=True
        )
        return
    if voice_channel is None:
        voice = interaction.user.voice
        if not voice or not voice.channel:
            await interaction.response.send_message(
                "Join a voice channel or name one explicitly.", ephemeral=True
            )
            return
        voice_channel = voice.channel

    meeting = Meeting(
        guild_id=interaction.guild_id,
        text_channel_id=interaction.channel_id,
        voice_channel_id=voice_channel.id,
        mode=mode.value,
        started_by_id=interaction.user.id,
        started_by_name=interaction.user.display_name,
    )
    if agenda:
        items = [part.strip() for part in agenda.split(";") if part.strip()]
        for item in items:
            meeting.add_agenda_item(item)
    for member in voice_channel.members:
        if not member.bot:
            meeting.record_attendance(member.id, member.display_name, "present")

    bot.registry.meetings[interaction.guild_id] = meeting
    bot.registry.save()

    failures = 0
    if meeting.mode == MODE_STRICT:
        failures = await bot.enforce_strict(meeting, interaction.guild)

    await interaction.response.send_message(
        f"Meeting opened in **{voice_channel.name}** ({mode.name.split(' — ')[0].lower()} mode)."
    )
    panel = await interaction.channel.send(
        embed=bot.build_panel_embed(meeting), view=PanelView(bot)
    )
    meeting.panel_message_id = panel.id
    bot.registry.save()

    if failures:
        await interaction.followup.send(
            f"⚠️ Could not mute {failures} member(s) — check that Merryn has the "
            "**Mute Members** permission and a role above theirs.",
            ephemeral=True,
        )


@meeting_group.command(name="end", description="End the meeting and publish the minutes")
async def meeting_end(interaction: discord.Interaction) -> None:
    bot: Merryn = interaction.client
    if bot.registry.get(interaction.guild_id) is None:
        await interaction.response.send_message(
            "No meeting is in session.", ephemeral=True
        )
        return
    if not await bot._require_moderator(interaction):
        return
    await bot.end_meeting(interaction)


@meeting_group.command(
    name="mode", description="Switch between strict and advisory mid-meeting"
)
@app_commands.choices(
    mode=[
        app_commands.Choice(name="Strict — enforce muting", value=MODE_STRICT),
        app_commands.Choice(name="Advisory — queue only", value=MODE_ADVISORY),
    ]
)
async def meeting_mode(
    interaction: discord.Interaction, mode: app_commands.Choice[str]
) -> None:
    bot: Merryn = interaction.client
    meeting = await bot._require_meeting(interaction)
    if meeting is None or not await bot._require_moderator(interaction):
        return
    if meeting.mode == mode.value:
        await interaction.response.send_message(
            f"Already in {mode.value} mode.", ephemeral=True
        )
        return
    meeting.mode = mode.value
    bot.registry.save()
    if mode.value == MODE_ADVISORY:
        await bot.lift_all_mutes(meeting, interaction.guild)
        await interaction.response.send_message("Switched to advisory mode — mutes lifted.")
    else:
        failures = await bot.enforce_strict(meeting, interaction.guild)
        note = f" (⚠️ {failures} could not be muted)" if failures else ""
        await interaction.response.send_message(f"Switched to strict mode.{note}")
    await bot.refresh_panel(meeting)


@agenda_group.command(name="add", description="Append an item to the agenda")
@app_commands.describe(
    item="The agenda item",
    owner="Member presenting this item — given the floor automatically on /agenda next",
)
async def agenda_add(
    interaction: discord.Interaction,
    item: str,
    owner: discord.Member | None = None,
) -> None:
    bot: Merryn = interaction.client
    meeting = await bot._require_meeting(interaction)
    if meeting is None or not await bot._require_moderator(interaction):
        return
    if owner is not None and owner.bot:
        await interaction.response.send_message(
            "Bots do not present agenda items.", ephemeral=True
        )
        return
    meeting.add_agenda_item(
        item.strip(),
        owner_id=owner.id if owner else None,
        owner_name=owner.display_name if owner else None,
    )
    bot.registry.save()
    suffix = f" — presented by **{owner.display_name}**" if owner else ""
    await interaction.response.send_message(f"📜 Added: **{item.strip()}**{suffix}")
    await bot.refresh_panel(meeting)


@agenda_group.command(
    name="assign", description="Assign a presenter to an existing agenda item"
)
@app_commands.describe(
    number="Agenda item number as shown on the panel",
    owner="Member presenting this item — given the floor automatically on /agenda next",
)
async def agenda_assign(
    interaction: discord.Interaction,
    number: app_commands.Range[int, 1, 99],
    owner: discord.Member,
) -> None:
    bot: Merryn = interaction.client
    meeting = await bot._require_meeting(interaction)
    if meeting is None or not await bot._require_moderator(interaction):
        return
    if owner.bot:
        await interaction.response.send_message(
            "Bots do not present agenda items.", ephemeral=True
        )
        return
    item = meeting.assign_owner(number - 1, owner.id, owner.display_name)
    if item is None:
        await interaction.response.send_message(
            f"There is no agenda item {number}.", ephemeral=True
        )
        return
    bot.registry.save()
    await interaction.response.send_message(
        f"📜 Item {number}: **{item.text}** — presented by **{owner.display_name}**"
    )
    await bot.refresh_panel(meeting)


@agenda_group.command(name="next", description="Move to the next agenda item")
async def agenda_next(interaction: discord.Interaction) -> None:
    bot: Merryn = interaction.client
    await bot.handle_agenda_next(interaction)


@agenda_group.command(name="show", description="Show the agenda")
async def agenda_show(interaction: discord.Interaction) -> None:
    bot: Merryn = interaction.client
    meeting = await bot._require_meeting(interaction)
    if meeting is None:
        return
    if not meeting.agenda:
        await interaction.response.send_message("No agenda has been set.", ephemeral=True)
        return
    lines = []
    for i, item in enumerate(meeting.agenda):
        marker = "▶" if i == meeting.agenda_index else " "
        owner = f" · {item.owner_name}" if item.owner_name else ""
        lines.append(f"{marker} {i + 1}. {item.text}{owner}")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@app_commands.command(name="note", description="Record a note in the minutes")
@app_commands.guild_only()
@app_commands.describe(text="What to record")
async def note_command(interaction: discord.Interaction, text: str) -> None:
    bot: Merryn = interaction.client
    meeting = await bot._require_meeting(interaction)
    if meeting is None:
        return
    meeting.add_log("note", text, interaction.user.display_name)
    bot.registry.save()
    await interaction.response.send_message(f"📝 Noted: {text}")


@app_commands.command(name="decision", description="Record a decision in the minutes")
@app_commands.guild_only()
@app_commands.describe(text="The decision as it should appear in the minutes")
async def decision_command(interaction: discord.Interaction, text: str) -> None:
    bot: Merryn = interaction.client
    meeting = await bot._require_meeting(interaction)
    if meeting is None or not await bot._require_moderator(interaction):
        return
    meeting.add_log("decision", text, interaction.user.display_name)
    bot.registry.save()
    await interaction.response.send_message(f"✅ Decision recorded: **{text}**")


@app_commands.command(name="action", description="Record an action item in the minutes")
@app_commands.guild_only()
@app_commands.describe(text="The action", assignee="Who it is assigned to")
async def action_command(
    interaction: discord.Interaction, text: str, assignee: discord.Member | None = None
) -> None:
    bot: Merryn = interaction.client
    meeting = await bot._require_meeting(interaction)
    if meeting is None or not await bot._require_moderator(interaction):
        return
    if assignee is not None:
        text = f"{text} (assigned to {assignee.display_name})"
    meeting.add_log("action", text, interaction.user.display_name)
    bot.registry.save()
    await interaction.response.send_message(f"📌 Action recorded: **{text}**")


@app_commands.command(
    name="timer", description="Set a per-speaker time limit (0 to disable)"
)
@app_commands.guild_only()
@app_commands.describe(seconds="Limit in seconds; 0 disables it")
async def timer_command(
    interaction: discord.Interaction, seconds: app_commands.Range[int, 0, 3600]
) -> None:
    bot: Merryn = interaction.client
    meeting = await bot._require_meeting(interaction)
    if meeting is None or not await bot._require_moderator(interaction):
        return
    meeting.timer_seconds = seconds
    meeting.timer_alerted = False
    bot.registry.save()
    if seconds:
        await interaction.response.send_message(
            f"⏳ Speaking limit set to {fmt_duration(seconds)}."
        )
    else:
        await interaction.response.send_message("⏳ Speaking limit disabled.")
    await bot.refresh_panel(meeting)


@app_commands.command(
    name="motivation", description="A word of encouragement from Merryn"
)
@app_commands.guild_only()
async def motivation_command(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(random.choice(MOTIVATION_QUOTES))


@app_commands.command(name="motion", description="Open a timed ballot on a motion")
@app_commands.guild_only()
@app_commands.rename(pass_percent="pass")
@app_commands.describe(
    text="The motion being put to the meeting",
    seconds="How long the ballot stays open (default 60)",
    pass_percent="Percentage of votes cast needed to carry, e.g. 75 (default: simple majority)",
)
async def motion_command(
    interaction: discord.Interaction,
    text: str,
    seconds: app_commands.Range[int, 15, 600] = 60,
    pass_percent: app_commands.Range[int, 1, 100] | None = None,
) -> None:
    bot: Merryn = interaction.client
    meeting = await bot._require_meeting(interaction)
    if meeting is None:
        return
    if not in_meeting_voice(interaction.user, meeting):
        await interaction.response.send_message(
            "Join the meeting voice channel first.", ephemeral=True
        )
        return
    await bot.open_motion(interaction, meeting, text, seconds, pass_percent)


for command in (
    note_command,
    decision_command,
    action_command,
    timer_command,
    motion_command,
    motivation_command,
):
    client.tree.add_command(command)


def _pause_if_frozen() -> None:
    """Keeps the console window open for double-click users on Windows."""
    if getattr(sys, "frozen", False) and sys.stdin is not None and sys.stdin.isatty():
        try:
            input("Press Enter to close...")
        except EOFError:
            pass


def _prompt_for_token() -> str:
    print("No Discord bot token found (DISCORD_TOKEN in the environment or a .env file).")
    print("Create a bot at https://discord.com/developers/applications and paste its token.")
    try:
        token = input("Token: ").strip()
    except EOFError:
        return ""
    if token:
        try:
            answer = input("Save it to .env in this directory for next time? [y/N]: ")
        except EOFError:
            answer = ""
        if answer.strip().lower().startswith("y"):
            with open(".env", "a", encoding="utf-8") as env_file:
                env_file.write(f"DISCORD_TOKEN={token}\n")
            try:
                os.chmod(".env", 0o600)
            except OSError:
                pass
            print("Saved. Keep .env private — the token controls your bot.")
    return token


def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    token = TOKEN
    if not token and sys.stdin is not None and sys.stdin.isatty():
        token = _prompt_for_token()
    if not token:
        print("DISCORD_TOKEN is not set. See the README for setup.")
        _pause_if_frozen()
        raise SystemExit(1)
    try:
        client.run(token, log_handler=None)
    except discord.LoginFailure:
        print("Discord rejected the token. Check it and try again.")
        _pause_if_frozen()
        raise SystemExit(1)


if __name__ == "__main__":
    run()
