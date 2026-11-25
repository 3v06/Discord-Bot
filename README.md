# Discord Community Bot

A complete, modular Discord moderation and utility bot built on **discord.py
2.7.1**. It keeps the original prefix commands (`-ping`, `-hi`, `-afk`,
`-unafk`) and adds a ticket system, anti-spam, blacklist automod, staff
timeouts, bulk message clearing, a welcome system, automatic keyword replies
and persistent per-server configuration backed by SQLite.

---

## Features

| Module | What it does |
| --- | --- |
| 🎫 Tickets | Private ticket channels, persistent **Open / Close / Delete** buttons, staff access, confirmation messages, optional ticket log channel |
| 🛡️ Anti-spam | Message flood, duplicate messages, excessive mentions and `@everyone` abuse detection; warnings, strikes and automatic timeouts |
| 🚫 Blacklist automod | Deletes messages containing words from `blacklisted_words.txt`, times the author out, logs the hit, supports hot reload |
| 🔨 Moderation | `/timeout`, `/untimeout`, `/clearm`, role-hierarchy checks, permission-error handling, DM notifications |
| 🏓 Ping | `/ping` embed with WebSocket, database, user request and round-trip latency |
| 👋 Welcome | Persistent welcome channel + customisable template, avatar embed, DM fallback, test command |
| 🧹 Clear | Bulk delete up to 200 messages, chunked at Discord's 100-message limit, messages older than 14 days deleted individually, pinned messages preserved |
| 💬 Auto replies | Keyword → response rules from `auto_replies.json`, per-user cooldowns, no bot loops |
| 🗄️ Storage | SQLite (`data/bot.sqlite3`) for settings, AFK, tickets and a moderation audit log |
| 📋 Logging | Embed logs for timeouts, spam, blacklist hits, clears, deleted messages and tickets; rotating file log |

---

## 1. Setup

### Requirements

* Python 3.10+ (tested with **Python 3.14.10**)
* The included virtual environment already has discord.py 2.7.1
* A Discord application / bot token

### Install dependencies

```bash
cd ~/Desktop/Discord-Bot
./venv/bin/pip install -r requirements.txt
```

### Configure `.env`

```bash
cp .env.example .env
```

Edit `.env`:

```env
DISCORD_TOKEN=your-bot-token
GUILD_ID=123456789012345678   # optional, recommended while developing
```

* `GUILD_ID` makes slash commands appear **instantly** in that server.
  Without it Discord may take up to an hour to propagate global commands.
* Optional overrides: `DB_PATH`, `BLACKLIST_PATH`, `AUTO_REPLIES_PATH`,
  `LOG_FILE`, `LOG_LEVEL`.

### Enable the required intents

In the [Discord Developer Portal](https://discord.com/developers/applications)
→ your application → **Bot** → **Privileged Gateway Intents**, enable:

* ✅ **Message Content Intent** — needed for prefix commands, automod and auto replies
* ✅ **Server Members Intent** — needed for welcome messages and member lookups

### Invite the bot

OAuth2 → URL Generator:

* Scopes: `bot`, `applications.commands`
* Permissions:
  `View Channels`, `Send Messages`, `Send Messages in Threads`,
  `Embed Links`, `Attach Files`, `Read Message History`, `Add Reactions`,
  `Manage Messages`, `Manage Channels`, `Moderate Members`

Move the bot's role **above** the roles it must moderate (timeouts fail if the
bot's highest role is not above the target's).

### Verify and run

```bash
./venv/bin/python scripts/selfcheck.py   # offline: imports, commands, DB, filters
./venv/bin/python main-bot.py
```

`selfcheck.py` does not connect to Discord. It verifies all cogs load, all
commands register, the filter files parse, and SQLite works.

---

## 2. First-time server setup

Run these as an administrator inside your server:

```text
/setlogchannel              # moderation log channel (defaults to current channel)
/setwelcomechannel          # welcome channel (defaults to current channel)
/ticket settings category:#tickets staff_role:@Staff log_channel:#ticket-logs
/ticket panel               # posts the persistent "Open a ticket" panel
/blacklist reload           # after editing blacklisted_words.txt
/autoreplies reload         # after editing auto_replies.json
```

Recommended permission checks:

* The bot can **Manage Channels** (ticket creation/deletion).
* The bot can **Manage Messages** (deleting spam and bulk clears).
* The bot can **Moderate Members** (timeouts).
* The bot's role is above regular members.

---

## 3. Commands

### General (slash + original prefix)

| Command | Prefix | Description |
| --- | --- | --- |
| `/ping` | `-ping` | Latency embed |
| `/hi` | `-hi` | Greeting |
| `/afk [duration] [reason]` | `-afk ...` | Persistent AFK status; auto-clears when you speak |
| `/unafk` | `-unafk` | Remove AFK status |
| — | `-help` | discord.py's built-in help for prefix commands |

AFK status is stored in SQLite. When someone mentions an AFK member, the bot
replies with the reason and how long ago it was set.

### Moderation

| Command | Permission | Description |
| --- | --- | --- |
| `/timeout <member> <duration> <reason>` | Moderate Members | Applies a timeout (max 28 days), checks role hierarchy, DMs the member, logs it |
| `/untimeout <member> [reason]` | Moderate Members | Removes an active timeout |
| `/clearm <amount>` | Manage Messages | Deletes 1–200 recent messages, respects the 14-day bulk limit and pinned messages |
| `/setlogchannel [channel]` | Administrator | Sets the moderation log channel |
| `/logconfig show` | Administrator | Shows logging configuration |
| `/logconfig deletes <true/false>` | Administrator | Enables/disables deleted-message logging |

Durations accept `60s`, `10m`, `2h`, `1d`, `1w`, combinations like `1h30m`,
or a bare number (minutes): `10` = 10 minutes.

### Blacklist automod

| Command | Permission | Description |
| --- | --- | --- |
| `/blacklist reload` | Manage Server | Reload `blacklisted_words.txt` immediately |
| `/blacklist list` | Manage Server | Show loaded entries |
| `/blacklist status` | Manage Server | Show enabled state, entry count, timeout, staff exemption |
| `/blacklist toggle <true/false>` | Manage Server | Enable/disable blacklist automod |
| `/blacklist timeout <seconds>` | Manage Server | Timeout duration (0 = delete only) |

The file is also re-read automatically when its modification time changes, so
editing it takes effect within seconds — no restart needed.

### Anti-spam

| Command | Permission | Description |
| --- | --- | --- |
| `/antispam settings` | Manage Server | Show the current configuration |
| `/antispam set <key> <value>` | Manage Server | Change a numeric setting |
| `/antispam toggle <true/false>` | Manage Server | Enable/disable anti-spam |
| `/antispam ignorestaff <true/false>` | Manage Server | Exempt staff (Manage Messages, Administrator, or ticket staff role) |

Defaults: 8 messages per 10 seconds, a flood limit of 5 messages per 3 seconds,
3 identical messages, 6 mentions; first detection warns, third strike applies a
5-minute timeout. Strikes expire after 10 minutes. `@everyone`/`@here` counts as
a mention flood.

### Tickets

| Command | Permission | Description |
| --- | --- | --- |
| `/ticket panel [channel]` | Manage Server | Posts the persistent ticket panel |
| `/ticket settings [category] [staff_role] [log_channel]` | Manage Server | View/update settings (only supplied values change) |
| `/ticket close` | Manage Messages | Closes the ticket in the current channel |
| `/ticket delete` | Manage Messages | Deletes the ticket channel after confirmation |

Ticket channels are private: `@everyone` is denied, the creator, the configured
staff role and the bot are allowed. The creator or staff can close; staff can
delete. Both actions are logged.

### Welcome

| Command | Permission | Description |
| --- | --- | --- |
| `/setwelcomechannel [channel]` | Administrator | Saves the welcome channel persistently |
| `/setwelcomemessage <template>` | Administrator | Customises the welcome text |
| `/welcomeconfig` | Administrator | Shows the current welcome configuration |
| `/testwelcome` | Administrator | Sends a test welcome embed |

Template placeholders: `{mention}`, `{user}`, `{username}`, `{display_name}`,
`{server}`, `{count}`. If no welcome channel is configured (or it is not
reachable), the bot falls back to DMing the new member, preserving the original
behaviour.

### Auto replies

| Command | Permission | Description |
| --- | --- | --- |
| `/autoreplies reload` | Manage Server | Reload `auto_replies.json` |
| `/autoreplies list` | Manage Server | List configured rules |
| `/autoreplies toggle <true/false>` | Manage Server | Enable/disable automatic replies |
| `/autoreplies cooldown <seconds>` | Manage Server | Per-user cooldown per rule (default 30s) |

---

## 4. Editable files

### `blacklisted_words.txt`

One word or phrase per line. `#` starts a comment line. Blank lines ignored.

```text
badword1
badword2
some bad phrase
```

Matching is case-insensitive and tolerates common bypasses: leetspeak
(`b4dw0rd`), repeated letters (`baaadword`), inserted spaces/punctuation
(`b a d w o r d`), full-width/zero-width characters and common Cyrillic/Greek
lookalike letters. Word boundaries prevent short entries from matching inside
legitimate words. Entries of two characters or fewer are matched literally to
avoid false positives.

On a hit the bot deletes the message (if permitted), applies the configured
timeout, DMs the author when possible and posts a moderation log embed.

### `auto_replies.json`

```json
{
  "replies": [
    {
      "keywords": ["hello", "hi", "hey"],
      "response": "Hello! Welcome to the server! 👋",
      "match": "word"
    },
    {
      "keywords": ["how are you"],
      "response": "I'm just a bot, but I'm running great! 🤖",
      "match": "contains"
    }
  ]
}
```

* `match`: `word` (whole-word, default), `contains` (substring) or `exact`.
* Rules are evaluated top to bottom; only the **first** matching rule replies.
* Only one reply is sent per message.
* Bots (including the bot itself) are ignored, so reply loops are impossible.
* Duplicate whitespace and casing are normalised before matching.
* The file is re-read automatically when it changes; `/autoreplies reload`
  forces it. Invalid JSON leaves the previous valid rules active and reports the
  error in `/autoreplies list`.

---

## 5. Configuration storage

Everything persistent lives in SQLite (`data/bot.sqlite3`, configurable with
`DB_PATH`):

| Table | Contents |
| --- | --- |
| `guild_settings` | Key/value settings per server (welcome channel, logs, tickets, anti-spam, blacklist, auto-reply cooldowns) |
| `afk` | AFK reason + timestamp per member |
| `tickets` | Ticket number, channel, creator, subject, status, timestamps |
| `moderation_log` | Audit trail with action, user, moderator, reason, duration, timestamp |

Settings are cached in memory for 30 seconds and invalidated immediately when
changed through a command. Defaults are defined in `bot/database.py`, so a new
server needs no setup to work — only the optional channel/role configuration.

The Discord token is **never** stored in the database or source code; it is read
from `.env`, which is git-ignored.

---

## 6. Logging

* File log: `discord.log` (rotating, 5 MB × 3) — configurable with `LOG_FILE`.
* Moderation embeds: the channel set with `/setlogchannel` (ticket events use
  the ticket log channel if configured, otherwise the moderation log channel).
* Logged actions: manual timeouts, timeout removal, automatic spam timeouts,
  spam detections, blacklist hits, bulk clears, deleted messages, ticket
  creation/closure/deletion.
* Every action is also written to the `moderation_log` SQLite table, so history
  is not lost if a log channel is removed.

If no log channel is configured, events are still written to the database and
the rotating file log.

---

## 7. Project structure

```text
Discord-Bot/
├── main-bot.py              # entry point
├── bot/
│   ├── config.py            # .env/config loading
│   ├── database.py          # SQLite storage + default settings
│   ├── client.py            # Bot subclass, settings cache, error handlers
│   ├── filters.py           # blacklist + auto-reply file loaders
│   ├── logging_setup.py     # file logging + moderation embeds
│   └── utils.py             # durations, hierarchy checks, text helpers
├── cogs/
│   ├── general.py           # /ping, /hi, AFK
│   ├── moderation.py        # timeouts, clear, blacklist automod, delete logs
│   ├── antispam.py          # flood/duplicate/mention detection
│   ├── tickets.py           # ticket panel, channels, close/delete
│   ├── welcome.py           # welcome messages
│   └── autoreplies.py       # keyword replies
├── blacklisted_words.txt
├── auto_replies.json
├── scripts/selfcheck.py     # offline verification
├── requirements.txt
└── .env.example
```

Adding a feature: create a new file in `cogs/`, subclass `commands.Cog`, expose
`async def setup(bot)`, and add the module path to `EXTENSIONS` in
`bot/client.py`.

---

## 8. Troubleshooting

**`PrivilegedIntentsRequired`** — enable Message Content and Server Members in
the Developer Portal (see Setup).

**Slash commands missing** — set `GUILD_ID` in `.env` and restart; make sure the
bot was invited with the `applications.commands` scope. Global sync can take up
to an hour.

**Timeouts fail** — check the bot has `Moderate Members` and its highest role is
above the target's. The bot also refuses to time out administrators (unless you
are the owner), the server owner, bots and itself.

**Ticket creation fails** — the bot needs `Manage Channels`; a category can hold
at most 50 channels.

**No moderation logs appear** — run `/setlogchannel` and confirm the bot can
send embeds there. Without it, logs still go to `discord.log` and SQLite.

**Blacklist not matching** — run `/blacklist reload` and check `/blacklist
status`. Add one word/phrase per line; short words (≤2 chars) match literally.

**Auto replies not matching** — run `/autoreplies reload`; invalid JSON is
reported by `/autoreplies list`. Use `match: "contains"` for phrases that appear
inside longer sentences.

**Deleted-message logging is noisy** — disable it with
`/logconfig deletes enabled:false`.

---

## 9. Notes

* Prefix commands are intentionally preserved alongside slash commands.
* Anti-spam and blacklist actions ignore bots, so the bot can never punish
  itself or another bot.
* A message handled by the blacklist is skipped by anti-spam and auto replies,
  even though Discord dispatches listeners concurrently.
* Maximum Discord timeout is 28 days; `/timeout` rejects longer values.
* Bulk deletion uses Discord's 100-message endpoint in chunks; messages older
  than 14 days are deleted individually, and pinned messages are preserved.
* Slash commands are synced automatically on startup (guild-scoped when
  `GUILD_ID` is set, otherwise globally).
