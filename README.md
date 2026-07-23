# Merryn

A Discord meeting moderator for voice-channel meetings. Merryn keeps a
raise-hand speaking queue (with optional enforced server-muting so
nobody can talk over the recognised speaker), tracks an agenda, runs
timed anonymous ballots on motions, and publishes deterministic
minutes when the meeting ends.

Built for a LARP court that needed order in council sessions; works
just as well for clubs, societies, co-ops, and anyone else whose
meetings need a firm but fair chair.

## Features

- **Speaking queue** — members press ✋ on the control panel; the
  moderator presses 🔔 *Call next* to give them the floor.
- **Sticky panel** — the control panel reposts itself whenever other
  messages land, so it stays at the bottom of the meeting channel
  instead of scrolling out of view.
- **Two modes, chosen per meeting** — *strict* (everyone except the
  recognised speaker and moderators is server-muted) or *advisory*
  (queue only). Switch mid-meeting with `/meeting mode`.
- **Point of order** — ⚡ jumps the queue and pings the chair.
- **Agenda** — supplied at start (`/meeting start agenda:"a; b; c"`) or
  built up with `/agenda add`; advanced with `/agenda next`. Items can
  have a presenter; advancing to an owned item automatically gives that
  member the floor if they are in the voice channel, and pings them if
  they are not.
- **Agenda backlog (between meetings)** — with no meeting in session,
  *anyone* can `/agenda add` to propose an item for next time. The
  backlog is listed by `/agenda show`, tidied with `/agenda drop`, and
  pulled into the agenda automatically when the next meeting opens.
- **Outstanding actions carry over** — action items recorded with
  `/action` survive the meeting's close. The next meeting opens by
  listing what is still outstanding; `/actions list` shows them at any
  time and `/actions done <n>` ticks one off. Merryn becomes the group's
  memory of what was promised, not just a record of one sitting.
- **Test mode** — `/meeting test` runs a sandbox meeting that behaves
  exactly like a real one but touches none of the cross-meeting memory:
  it never drains the backlog, surfaces or consumes outstanding actions,
  or leaves any behind. Ideal for trying features out.
- **Motions and ballots** — `/motion` opens a timed Aye/Nay ballot for
  members in the voice channel. While the ballot is open only the
  number of votes cast is shown — never who voted or which way, and
  never a running result, so nobody can be swayed by how the vote is
  going. When it closes, the result is announced with the percentage
  in favour and how many of those present abstained by not voting.
  An optional supermajority can be required per motion:
  `/motion text:"Buy the new marquee" pass:75 seconds:120`.
- **Quorum** — `/quorum set 8` fixes how many members must be in the
  chamber before a ballot may be opened; `/quorum enable` and
  `/quorum disable` switch enforcement on and off without forgetting the
  number. An inquorate chamber cannot open a ballot. A moderator may force
  one with `override: True` on `/motion`, and both the override and any
  change to the number are written into the minutes under a **Procedural**
  heading, so a forced vote can never be quietly reinterpreted later. The
  setting is per-server and outlives meetings; `/meeting start quorum:`
  overrides it for a single sitting, and `/quorum show` reports whether
  the chamber is currently quorate.
- **Scheduling** — `/meeting schedule when:"19:30"` creates a native
  Discord scheduled event in the server's calendar, so members can mark
  themselves interested and be reminded. Times are read in the configured
  `MERRYN_TIMEZONE`; accepts `YYYY-MM-DD HH:MM`, `DD/MM/YYYY HH:MM`, or a
  bare `HH:MM` for the next occurrence. **Requires the Manage Events
  permission** (see step 3).
- **Help** — `/help` explains how to use Merryn, privately. Moderators
  additionally see the chairing, quorum, and record-keeping sections.
- **Hold music** — while a ballot is open, everyone in the voice
  channel is muted and Merryn joins to play hold music (a synthesised,
  licence-free muzak loop ships with the bot; supply your own 48 kHz
  16-bit WAV via `HOLD_MUSIC_FILE`). Music stops and mutes are lifted
  the moment the ballot closes, restoring exactly the pre-ballot mute
  state. `/holdmusic` also plays it on demand outside a ballot — Merryn
  joins your voice channel, loops the music muting no one, and leaves
  again when you run the command a second time.
- **Speaking timer** — `/timer 120` warns the chair when a speaker
  exceeds the limit (nobody is cut off automatically).
- **Attendance** — who was present at the start, who joined late, who
  left early.
- **Minutes** — on `/meeting end`, a Markdown file is posted to the
  channel and archived under the data directory. Assembled purely from
  observed events: attendance, speakers and their durations, agenda
  progress, ballot results, and whatever was recorded with `/note`,
  `/decision`, and `/action`. Note that Merryn does **not** record or
  transcribe what is said — Discord's end-to-end encryption of voice
  makes bot audio capture impossible — so the minutes only contain
  what participants log manually with those commands during the
  meeting. Get into the habit of `/note`-ing as you go.
- **Motivation** — `/motivation` works in any channel, any time; Merryn
  replies with a random word of encouragement.

## Quick start

### Download a binary (no Python needed)

Grab the latest build for your platform from
[Releases](https://github.com/jaldertech/merryn/releases):

| Platform | File |
|---|---|
| Linux (x86_64) | `merryn-linux-x86_64` |
| Windows (x86_64) | `merryn-windows-x86_64.exe` |
| macOS (Apple Silicon) | `merryn-macos-arm64` |

Run it from a terminal (or double-click on Windows). On first run it
asks for your bot token and offers to remember it. On Linux/macOS you
may need `chmod +x` first; on macOS, unsigned downloads need
right-click → Open the first time (or
`xattr -d com.apple.quarantine merryn-macos-arm64`). Intel Macs and
ARM Linux: use pip below.

### pip

Python 3.10+ and libopus (`apt install libopus0` / `brew install opus`;
Windows needs nothing extra):

```
pip install git+https://github.com/jaldertech/merryn
merryn
```

### Docker

```
git clone https://github.com/jaldertech/merryn && cd merryn
cp .env.example .env   # fill in DISCORD_TOKEN (and ideally GUILD_ID)
docker compose up -d --build
```

## Plugging it into Discord

1. Create an application at
   <https://discord.com/developers/applications>, add a **Bot**, and
   copy its token.
2. Under **Bot → Privileged Gateway Intents**, enable **Server Members
   Intent**. (Message Content is *not* needed.)
3. Invite it: **OAuth2 → URL Generator** → scopes `bot` +
   `applications.commands`; bot permissions **View Channels, Send
   Messages, Embed Links, Attach Files, Mute Members, Connect, Speak,
   Manage Events** (Connect/Speak are for ballot hold music; Manage
   Events lets `/meeting schedule` add events to the calendar). Or use
   this template with your client ID:

   ```
   https://discord.com/oauth2/authorize?client_id=YOUR_CLIENT_ID&scope=bot+applications.commands&permissions=8597326848
   ```

   > **Upgrading an existing install?** Discord does not grant new
   > permissions retroactively. If you invited Merryn before scheduling
   > existed, `/meeting schedule` will report a missing permission until
   > you either re-invite with the link above or grant Merryn's role
   > **Manage Events** in Server Settings → Roles. Nothing else needs it.

4. In the server, drag Merryn's role **above** the members it should
   be able to mute.
5. Start Merryn with the token (prompted on first run, or via `.env`
   or the environment).

## Configuration

All settings come from environment variables, or a `.env` file in the
directory Merryn is started from (see `.env.example`):

| Variable | Required | Purpose |
|---|---|---|
| `DISCORD_TOKEN` | yes | Bot token |
| `GUILD_ID` | recommended | Your server ID; makes slash commands appear instantly instead of within the hour |
| `MOD_ROLE_ID` | no | Role whose holders count as moderators (Manage Server always qualifies) |
| `DATA_DIR` | no | State and minutes location (default `./merryn-data`) |
| `HOLD_MUSIC_FILE` | no | Your own hold music: 48 kHz 16-bit WAV, mono or stereo |
| `MERRYN_TIMEZONE` | no | Timezone for minutes, e.g. `Europe/London` (default: system local time) |
| `OPUS_LIBRARY` | no | Explicit path to libopus if it is somewhere unusual |

## Commands

| Command | Who | Purpose |
|---|---|---|
| `/help` | anyone | How to use Merryn; moderators see the chairing sections too |
| `/meeting start mode:<strict\|advisory> [agenda] [voice_channel] [quorum]` | moderator | Open a meeting |
| `/meeting schedule when:<time> [length] [title] [voice_channel] [description]` | moderator | Add a meeting to the server's event calendar (needs Manage Events) |
| `/meeting test [mode] [agenda] [voice_channel]` | moderator | Sandbox meeting; nothing is carried forward |
| `/meeting end` | moderator | Adjourn and publish minutes |
| `/meeting mode` | moderator | Switch strict/advisory mid-meeting |
| `/quorum set <n>` · `/quorum enable` · `/quorum disable` | moderator | Members required in the chamber for a ballot; toggle enforcement |
| `/quorum show` | anyone | The standing setting, and whether the chamber is quorate now |
| `/agenda add [owner]` | anyone between meetings, moderator during one | Add an item to the live agenda, or to the next meeting's backlog if none is in session |
| `/agenda assign` · `/agenda next` | moderator | Assign a presenter; advance the agenda |
| `/agenda show` · `/agenda drop <n>` | anyone (drop: proposer or moderator) | Show the agenda/backlog; remove a backlog item |
| `/actions list` · `/actions done <n>` | anyone (done: moderator) | Outstanding actions carried between meetings |
| `/floor give <member>` | moderator | Give the floor directly, bypassing the queue |
| `/note <text>` | anyone | Record a note in the minutes |
| `/decision <text>` · `/action <text> [assignee]` | moderator | Record decisions/actions |
| `/timer <seconds>` | moderator | Per-speaker limit (0 = off) |
| `/motion <text> [seconds] [pass] [override]` | anyone in the VC | Open a timed ballot; `pass:75` requires 75% in favour; `override:` (moderators) forces an inquorate ballot |
| `/holdmusic` | anyone | Merryn joins your voice channel and loops hold music; run again to stop |
| `/motivation` | anyone | A random word of encouragement |

"Moderator" = anyone with **Manage Server**, or the role named in
`MOD_ROLE_ID`.

## Operational notes

- State (including who the bot has muted) is persisted to
  `state.json` in the data directory, so a restart mid-meeting resumes
  the session and never strands anyone server-muted. Members who
  disconnect while muted are unmuted the moment they next join any
  voice channel.
- Cross-meeting memory — the agenda backlog and outstanding actions —
  lives in `continuity.json` in the same data directory, separate from
  live meeting state so it outlives any single sitting. Test meetings
  never write to it.
- A ballot open across a restart is voided rather than resumed —
  in-flight votes are held in memory only, by design: they are
  anonymous and are discarded once the ballot closes. Ballot mutes
  *are* persisted, and are lifted on resume.
- There is no voice recording or transcription, and there cannot be:
  Discord's DAVE end-to-end voice encryption (enforced since March
  2026) prevents bots from decrypting received audio. Merryn only ever
  joins voice self-deafened. Minutes are therefore only as rich as
  what participants log with `/note`, `/decision`, and `/action`.
- Strict mode requires the **Mute Members** permission and role
  hierarchy (step 4 above); failures are reported to the moderator
  rather than silently ignored.
- One meeting per server at a time; one ballot per meeting at a time.

## Building from source

```
git clone https://github.com/jaldertech/merryn && cd merryn
pip install .
python tests/test_smoke.py
merryn
```

The hold music is synthesised from scratch by
`tools/make_hold_music.py` (no samples, nothing to licence) — the
committed WAV is its exact output, regenerable with
`python tools/make_hold_music.py merryn/assets/hold_music.wav`.

## Licence

[MIT](LICENSE).
