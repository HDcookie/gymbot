# Bible in a Year Discord Bot

Posts each day's readings from `bible_in_a_year.json` to a channel.

## Setup
1. Create a bot at https://discord.com/developers/applications → Bot → copy the token.
2. Invite it: OAuth2 → URL Generator → scopes `bot` and `applications.commands`,
   permissions `Send Messages`, `View Channels`.
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

## Logs
The bot logs to the terminal. At startup it reports which servers it's in, today's day number,
whether it has View Channel and Send Messages in the post channel, and when the next post is.
Every daily post logs success or the exact reason it failed.
