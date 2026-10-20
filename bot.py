"""Discord bot that posts the day's Bible in a Year readings and DMs the owner a daily server summary."""

import datetime as dt
import json
import logging
import os
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv
from openai import AsyncOpenAI

import summary

load_dotenv()

discord.utils.setup_logging(level=logging.INFO)
log = logging.getLogger("biblebot")

TOKEN = os.environ["DISCORD_TOKEN"]
CHANNEL_ID = int(os.environ["CHANNEL_ID"])
START_DATE = dt.date.fromisoformat(os.environ["START_DATE"])  # the date of Day 1
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "UTC"))
POST_TIME = os.getenv("POST_TIME", "06:00")  # 24h HH:MM in TIMEZONE
# Optional: a server ID makes slash commands show up instantly in that server.
# Without it they sync globally, which can take up to an hour the first time.
GUILD_ID = os.getenv("GUILD_ID")
# You: can run /test without admin, and gets the daily summary DM.
OWNER_ID = int(os.getenv("OWNER_ID", "1521185982475407421"))
PLAN_FILE = Path(os.getenv("PLAN_FILE", Path(__file__).with_name("bible_in_a_year.json")))

# Daily summary DM. Leave OPENAI_API_KEY unset to turn it off.
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5-mini")
SUMMARY_TIME = os.getenv("SUMMARY_TIME", "20:00")  # 24h HH:MM in TIMEZONE
SUMMARY_IGNORE_CHANNELS = {int(c) for c in os.getenv("SUMMARY_IGNORE_CHANNELS", "").split(",") if c.strip()}
SUMMARY_MAX_PER_CHANNEL = int(os.getenv("SUMMARY_MAX_PER_CHANNEL", "300"))


def parse_time(value: str) -> dt.time:
    hour, minute = map(int, value.split(":"))
    return dt.time(hour=hour, minute=minute, tzinfo=TIMEZONE)


SEND_AT = parse_time(POST_TIME)
SUMMARY_AT = parse_time(SUMMARY_TIME)
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

with PLAN_FILE.open(encoding="utf-8") as f:
    PLAN = json.load(f)
log.info("Loaded %d days from %s", len(PLAN["days"]), PLAN_FILE)


def today() -> dt.date:
    return dt.datetime.now(TIMEZONE).date()


def next_run(time: dt.time) -> dt.datetime:
    now = dt.datetime.now(TIMEZONE)
    at = now.replace(hour=time.hour, minute=time.minute, second=0, microsecond=0)
    return at if at > now else at + dt.timedelta(days=1)


def day_number(date: dt.date) -> int:
    return (date - START_DATE).days + 1


def reading_for(day: int) -> dict | None:
    if 1 <= day <= len(PLAN["days"]):
        return PLAN["days"][day - 1]
    return None


def format_reading(entry: dict) -> str:
    lines = [
        f"📖 **Day {entry['day']} of {PLAN['total_days']}** · *{entry['period']}*",
        "",
        f"• {entry['first_reading']}",
    ]
    if entry["second_reading"]:
        lines.append(f"• {entry['second_reading']}")
    lines.append(f"• {entry['psalm_proverb']}")
    return "\n".join(lines)


class BibleBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True  # the summary needs to read message text
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Synced %d slash commands to guild %s", len(synced), GUILD_ID)
        else:
            synced = await self.tree.sync()
            log.info("Synced %d slash commands globally (may take up to an hour to appear)", len(synced))


bot = BibleBot()


async def get_channel() -> discord.abc.Messageable:
    return bot.get_channel(CHANNEL_ID) or await bot.fetch_channel(CHANNEL_ID)


def check_channel_permissions(channel) -> list[str]:
    """Return the permissions the bot is missing in the post channel."""
    if not isinstance(channel, discord.abc.GuildChannel):
        return []
    perms = channel.permissions_for(channel.guild.me)
    needed = {"view_channel": perms.view_channel, "send_messages": perms.send_messages}
    return [name for name, ok in needed.items() if not ok]


async def post_reading(entry: dict) -> str | None:
    """Send a reading to the post channel. Returns None on success, or why it failed."""
    try:
        channel = await get_channel()
    except discord.NotFound:
        return f"Channel {CHANNEL_ID} doesn't exist. Check CHANNEL_ID."
    except discord.Forbidden:
        return f"The bot can't see channel {CHANNEL_ID}. Is it in that server, with View Channel?"

    missing = check_channel_permissions(channel)
    if missing:
        return f"Missing permissions in #{channel}: {', '.join(missing)}"

    try:
        await channel.send(format_reading(entry))
    except discord.Forbidden as e:
        return f"Discord refused the message in #{channel}: {e.text}"
    except discord.HTTPException as e:
        return f"Discord error {e.status} sending to #{channel}: {e.text}"
    return None


@tasks.loop(time=SEND_AT)
async def daily_post():
    day = day_number(today())
    entry = reading_for(day)
    if entry is None:
        log.warning("Skipping %s: it's day %d, outside the 1-%d plan (START_DATE=%s)",
                    today(), day, len(PLAN["days"]), START_DATE)
        return
    error = await post_reading(entry)
    if error:
        log.error("Daily post for day %d failed: %s", day, error)
    else:
        log.info("Posted day %d to channel %s", day, CHANNEL_ID)


@daily_post.error
async def daily_post_error(error: BaseException):
    log.exception("Daily post crashed", exc_info=error)


@bot.tree.command(name="today", description="Show today's readings")
async def today_cmd(interaction: discord.Interaction):
    entry = reading_for(day_number(today()))
    await interaction.response.send_message(
        format_reading(entry) if entry else "Today isn't part of the reading plan."
    )


@bot.tree.command(name="day", description="Show the readings for a specific day")
@app_commands.describe(day="Day number in the plan")
async def day_cmd(interaction: discord.Interaction, day: app_commands.Range[int, 1, 365]):
    entry = reading_for(day)
    await interaction.response.send_message(
        format_reading(entry) if entry else f"Pick a day from 1 to {PLAN['total_days']}."
    )


@bot.tree.command(name="test", description="Send a reading to the daily channel right now")
@app_commands.describe(day="Day to send (defaults to today, or Day 1 if today is outside the plan)")
async def test_cmd(interaction: discord.Interaction, day: Optional[app_commands.Range[int, 1, 365]] = None):
    is_admin = interaction.guild is not None and interaction.permissions.administrator
    if interaction.user.id != OWNER_ID and not is_admin:
        log.info("Denied /test for %s (%s)", interaction.user, interaction.user.id)
        await interaction.response.send_message("Only admins can use /test.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    if day is None:
        day = day_number(today())
        if reading_for(day) is None:
            day = 1
    log.info("/test by %s (%s): sending day %d", interaction.user, interaction.user.id, day)

    error = await post_reading(reading_for(day))
    if error:
        log.error("/test failed: %s", error)
        await interaction.followup.send(f"❌ {error}", ephemeral=True)
    else:
        await interaction.followup.send(f"✅ Sent day {day} to <#{CHANNEL_ID}>.", ephemeral=True)


async def send_summary(hours: int = 24) -> str | None:
    """Build the summary and DM it to the owner. Returns None on success, or why it failed."""
    if openai_client is None:
        return "OPENAI_API_KEY isn't set, so summaries are off."
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    guilds = [g for g in bot.guilds if not GUILD_ID or g.id == int(GUILD_ID)]
    msgs = await summary.collect(guilds, since, OWNER_ID, SUMMARY_IGNORE_CHANNELS, SUMMARY_MAX_PER_CHANNEL)

    try:
        owner = await bot.fetch_user(OWNER_ID)
    except discord.HTTPException as e:
        return f"Couldn't look up owner {OWNER_ID}: {e.text}"

    if not msgs:
        chunks = [f"## 📋 Daily Summary\n-# {today():%a, %b} {today().day}\n\nQuiet day, no messages."]
    else:
        try:
            result = await summary.summarize(openai_client, OPENAI_MODEL, msgs, TIMEZONE)
        except Exception as e:  # network, auth, quota, bad model name...
            log.exception("OpenAI request failed")
            return f"OpenAI request failed: {e}"
        chunks = summary.format_digest(result, msgs, today())

    try:
        for chunk in chunks:
            await owner.send(chunk, suppress_embeds=True)
    except discord.Forbidden:
        return "Discord won't let the bot DM you. Allow DMs from server members (Privacy Settings)."
    except discord.HTTPException as e:
        return f"Discord error {e.status} sending the DM: {e.text}"
    log.info("Sent summary to %s: %d messages summarized, %d chars in %d DM part(s)",
             owner, len(msgs), sum(len(c) for c in chunks), len(chunks))
    return None


@tasks.loop(time=SUMMARY_AT)
async def daily_summary():
    error = await send_summary()
    if error:
        log.error("Daily summary failed: %s", error)


@daily_summary.error
async def daily_summary_error(error: BaseException):
    log.exception("Daily summary crashed", exc_info=error)


@bot.tree.command(name="summary", description="DM yourself the server summary right now")
@app_commands.describe(hours="How far back to look (default 24)")
async def summary_cmd(interaction: discord.Interaction, hours: Optional[app_commands.Range[int, 1, 168]] = 24):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("Only the bot owner can use /summary.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    log.info("/summary requested for the last %d hours", hours)
    error = await send_summary(hours)
    if error:
        log.error("/summary failed: %s", error)
        await interaction.followup.send(f"❌ {error}", ephemeral=True)
    else:
        await interaction.followup.send("✅ Sent to your DMs.", ephemeral=True)


@bot.event
async def on_ready():
    log.info("Logged in as %s (%s), in %d server(s): %s", bot.user, bot.user.id,
             len(bot.guilds), ", ".join(f"{g.name} ({g.id})" for g in bot.guilds))

    day = day_number(today())
    if reading_for(day):
        log.info("Today (%s) is day %d", today(), day)
    else:
        log.warning("Today (%s) is day %d, outside the 1-%d plan, so nothing will post. "
                    "Check START_DATE=%s", today(), day, len(PLAN["days"]), START_DATE)

    try:
        channel = await get_channel()
        missing = check_channel_permissions(channel)
        if missing:
            log.error("Post channel #%s: missing permissions %s", channel, ", ".join(missing))
        else:
            log.info("Post channel #%s (%s): permissions OK", channel, CHANNEL_ID)
    except (discord.NotFound, discord.Forbidden) as e:
        log.error("Can't access post channel %s: %s", CHANNEL_ID, e)

    if not daily_post.is_running():
        daily_post.start()
    log.info("Next daily post at %s", next_run(SEND_AT))

    if openai_client is None:
        log.warning("OPENAI_API_KEY not set: daily summary is off")
    elif not daily_summary.is_running():
        daily_summary.start()
        log.info("Next summary DM at %s (model %s)", next_run(SUMMARY_AT), OPENAI_MODEL)


bot.run(TOKEN, log_handler=None)
