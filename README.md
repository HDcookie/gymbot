# Bible in a Year Discord Bot

Posts each day's readings from `bible_in_a_year.json` to a channel, and DMs the owner a daily
AI summary of everything that happened in the server.

## Setup
1. Create a bot at https://discord.com/developers/applications → Bot → copy the token.
   Turn on **Message Content Intent** (the summary needs to read messages).
2. Invite it: OAuth2 → URL Generator → scopes `bot` and `applications.commands`,
   permissions `Send Messages`, `View Channels`, `Read Message History`.
3. Enable Developer Mode in Discord, right-click your channel → Copy Channel ID.
4. `cp .env.example .env` and fill it in.
5. `pip install -r requirements.txt && python bot.py`

## Reading plan
Put `bible_in_a_year.json` next to `bot.py`. `START_DATE` in `.env` is the date of Day 1.
The day number is `(today - START_DATE) + 1`. The bot posts nothing before Day 1 or after Day 365.

## Slash commands
- `/today` shows today's readings.
- `/day 42` shows the readings for any day.
- `/test` sends today's readings (or `/test day:42`) to the daily channel right away, so you can
  check the bot can post there. Only server admins and the user in `OWNER_ID` can run it, and the
  reply tells you exactly why it failed if it can't post.

Set `GUILD_ID` in `.env` to your server's ID (right-click the server → Copy Server ID) so the
commands appear right away. Without it they're registered globally, which can take up to an hour.

## Daily summary DM
At `SUMMARY_TIME` (default 20:00 in `TIMEZONE`) the bot reads the last 24 hours of every channel
and thread it can see, sends them to OpenAI, and DMs `OWNER_ID`:
- every conversation that happened, by channel, with channel and message links
- **Waiting on you**: questions to you, mentions, replies to your messages, unanswered questions
- **Could use your take**: discussions already answered where your opinion would still add something

`/summary` (owner only) sends it right away; `/summary hours:48` looks further back.

Settings: `OPENAI_API_KEY` (unset = feature off), `OPENAI_MODEL` (default `gpt-5-mini`),
`SUMMARY_TIME`, `SUMMARY_IGNORE_CHANNELS` (comma-separated channel IDs), `SUMMARY_MAX_PER_CHANNEL`
(default 300, newest kept). Messages from bots are left out. Your DMs must allow messages from
server members.

Note: server members' messages are sent to OpenAI to make the summary.

## Logs
The bot logs to the terminal. At startup it reports which servers it's in, today's day number,
whether it has View Channel and Send Messages in the post channel, and when the next post is.
Every daily post logs success or the exact reason it failed.
