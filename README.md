# Bible in a Year Discord Bot

Posts each day's readings from `bible_in_a_year.json` to a channel, and DMs a daily AI summary of
everything that happened in the server to a list of people.

## Setup
1. Create a bot at https://discord.com/developers/applications → Bot → copy the token.
   Turn on **Message Content Intent** (the summary needs to read messages).
2. Invite it: OAuth2 → URL Generator → scopes `bot` and `applications.commands`,
   permissions `Send Messages`, `View Channels`, `Read Message History`.
3. `cp .env.example .env` and add your Discord token and OpenAI key.
4. `cp config.example.json config.json` and fill it in (see below).
5. Put `bible_in_a_year.json` next to `bot.py`.
6. `pip install -r requirements.txt && python bot.py`

To get IDs, turn on Developer Mode in Discord, then right-click a server, channel or user → Copy ID.

## config.json
| Setting | What it does |
|---|---|
| `guild_id` | Your server. Slash commands show up instantly there (otherwise up to an hour). |
| `owner_id` | You. Can run `/test` without being an admin. |
| `timezone` | Used for all times, e.g. `America/New_York`. |
| `readings.channel_id` | Where the daily readings are posted. |
| `readings.start_date` | The date of Day 1 (`YYYY-MM-DD`). |
| `readings.post_time` | When to post, 24h `HH:MM`. |
| `readings.plan_file` | The reading plan JSON. |
| `summary.enabled` | Turn the summary DM on or off. |
| `summary.time` | When to send the summary, 24h `HH:MM`. |
| `summary.recipients` | User IDs that get the summary DM every day. |
| `summary.model` | OpenAI model. |
| `summary.ignore_channels` | Channel IDs to leave out of the summary. |
| `summary.max_messages_per_channel` | Cap per channel per summary; the newest are kept. |

`config.json` isn't committed, so `git pull` won't overwrite your settings.

## Reading plan
The day number is `(today - start_date) + 1`. The bot posts nothing before Day 1 or after Day 365.

## Slash commands
- `/today` shows today's readings.
- `/day 42` shows the readings for any day.
- `/test` sends today's readings (or `/test day:42`) to the readings channel right away, so you can
  check the bot can post there. Only server admins and `owner_id` can run it, and the reply tells
  you exactly why it failed if it can't post.
- `/summary` DMs the summary to whoever ran it, right away (`/summary hours:48` looks further back).
  Only people in `summary.recipients` (and `owner_id`) can run it.

## Daily summary DM
At `summary.time` the bot reads the last 24 hours of every channel and thread it can see, and DMs
each person in `summary.recipients` their own summary:
- **Needs your reply**: questions to them, mentions, replies to their messages, unanswered questions
- **Your take would help**: discussions already answered where their opinion would still add something
- **What happened**: the main conversations by channel, with channel and message links

Each recipient's summary is written from their point of view, so it's one OpenAI request per person.
Summaries focus on what happened rather than listing everyone who took part, and small chats are
usually left out. Messages from bots are left out. Recipients' DMs must allow messages from server
members.

Note: server members' messages are sent to OpenAI to make the summary.

## Logs
The bot logs to the terminal. At startup it reports which servers it's in, today's day number,
whether it has View Channel and Send Messages in the readings channel, and when the next post and
summary are. Every post and summary logs success or the exact reason it failed.
