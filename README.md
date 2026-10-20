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

Set `GUILD_ID` in `.env` to your server's ID (right-click the server → Copy Server ID) so the
commands appear right away. Without it they're registered globally, which can take up to an hour.
