"""Discord UI components for Merryn.

PanelView is a *persistent* view: one instance is registered at startup
with static custom_ids, so its buttons keep working across bot restarts.
Every callback resolves the active meeting from the interaction's guild
rather than holding per-meeting state.

MotionView is deliberately non-persistent: a vote open across a bot
restart is voided rather than resumed, which is the honest outcome when
in-flight ballots have been lost.
"""
from __future__ import annotations

import discord


class PanelView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    # --- Row 0: everyone -------------------------------------------------

    @discord.ui.button(
        label="Raise hand",
        emoji="✋",
        style=discord.ButtonStyle.primary,
        custom_id="merryn:hand",
        row=0,
    )
    async def hand(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.bot.handle_hand(interaction)

    @discord.ui.button(
        label="Point of order",
        emoji="⚡",
        style=discord.ButtonStyle.danger,
        custom_id="merryn:point_of_order",
        row=0,
    )
    async def point_of_order(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await self.bot.handle_point_of_order(interaction)

    # --- Row 1: moderators only (enforced in the handlers) ----------------

    @discord.ui.button(
        label="Call next",
        emoji="🔔",
        style=discord.ButtonStyle.success,
        custom_id="merryn:next",
        row=1,
    )
    async def call_next(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await self.bot.handle_next(interaction)

    @discord.ui.button(
        label="Next agenda item",
        emoji="📜",
        style=discord.ButtonStyle.secondary,
        custom_id="merryn:agenda_next",
        row=1,
    )
    async def agenda_next(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await self.bot.handle_agenda_next(interaction)

    @discord.ui.button(
        label="Adjourn",
        emoji="🌙",
        style=discord.ButtonStyle.secondary,
        custom_id="merryn:adjourn",
        row=1,
    )
    async def adjourn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await self.bot.handle_adjourn_request(interaction)


class ConfirmAdjournView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=30)
        self.bot = bot

    @discord.ui.button(label="Confirm — adjourn the court", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await self.bot.end_meeting(interaction)


class MotionView(discord.ui.View):
    """Ballot buttons for one motion. Votes may be changed until close.

    Only the *number* of votes cast is ever displayed; who voted which
    way stays in memory and is discarded when the ballot closes.
    """

    def __init__(self, bot, voice_channel_id: int):
        super().__init__(timeout=None)  # closed explicitly by the bot's timer task
        self.bot = bot
        self.voice_channel_id = voice_channel_id
        self.votes: dict[int, str] = {}
        # Wired up by open_motion once the ballot message exists.
        self.record = None
        self.message: discord.Message | None = None
        self.close_task = None
        self.closes_at_unix: int = 0
        self.guild_id: int = 0

    async def _cast(self, interaction: discord.Interaction, choice: str) -> None:
        member = interaction.user
        voice = getattr(member, "voice", None)
        if not voice or not voice.channel or voice.channel.id != self.voice_channel_id:
            await interaction.response.send_message(
                "Only members present in the voice chamber may vote.", ephemeral=True
            )
            return
        self.votes[member.id] = choice
        await interaction.response.send_message(
            f"Your vote has been recorded: **{choice}**. "
            "You may change it until the ballot closes.",
            ephemeral=True,
        )
        await self.bot.update_ballot_tally(interaction.guild, self)

    @discord.ui.button(label="Aye", emoji="✅", style=discord.ButtonStyle.success)
    async def aye(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._cast(interaction, "aye")

    @discord.ui.button(label="Nay", emoji="❌", style=discord.ButtonStyle.danger)
    async def nay(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._cast(interaction, "nay")

    @discord.ui.button(
        label="Close ballot", emoji="🔨", style=discord.ButtonStyle.secondary, row=1
    )
    async def close_early(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await self.bot.handle_ballot_close(interaction, self)
