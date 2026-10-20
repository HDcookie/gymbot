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

# Secrets come from .env; everything else from config.json.
TOKEN = os.environ["DISCORD_TOKEN"]
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

CONFIG_FILE = Path(os.getenv("CONFIG_FILE", Path(__file__).with_name("config.json")))
with CONFIG_FILE.open(encoding="utf-8") as f:
    CONFIG = json.load(f)
log.info("Loaded config from %s", CONFIG_FILE)

TIMEZONE = ZoneInfo(CONFIG.get("timezone", "UTC"))
# Optional: a server ID makes slash commands show up instantly in that server.
# Without it they sync globally, which can take up to an hour the first time.
GUILD_ID = CONFIG.get("guild_id")
# Can run /test without being an admin.
OWNER_ID = int(CONFIG.get("owner_id", 1521185982475407421))

READINGS = CONFIG["readings"]
CHANNEL_ID = int(READINGS["channel_id"])
START_DATE = dt.date.fromisoformat(READINGS["start_date"])  # the date of Day 1
POST_TIME = READINGS.get("post_time", "06:00")  # 24h HH:MM in TIMEZONE
# Relative paths are relative to bot.py.
PLAN_FILE = Path(__file__).parent / READINGS.get("plan_file", "bible_in_a_year.json")

SUMMARY = CONFIG.get("summary", {})
SUMMARY_ENABLED = SUMMARY.get("enabled", True) and bool(OPENAI_API_KEY)
SUMMARY_TIME = SUMMARY.get("time", "20:00")  # 24h HH:MM in TIMEZONE
# Each gets their own nightly DM, written from their point of view.
SUMMARY_RECIPIENTS = [int(i) for i in SUMMARY.get("recipients", [OWNER_ID])]
OPENAI_MODEL = SUMMARY.get("model", "gpt-5-mini")
SUMMARY_IGNORE_CHANNELS = {int(i) for i in SUMMARY.get("ignore_channels", [])}
SUMMARY_MAX_PER_CHANNEL = int(SUMMARY.get("max_messages_per_channel", 300))


def parse_time(value: str) -> dt.time:
    hour, minute = map(int, value.split(":"))
    return dt.time(hour=hour, minute=minute, tzinfo=TIMEZONE)


SEND_AT = parse_time(POST_TIME)
SUMMARY_AT = parse_time(SUMMARY_TIME)
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY) if SUMMARY_ENABLED else None

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


async def collect_messages(hours: int) -> list[summary.Msg]:
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    guilds = [g for g in bot.guilds if not GUILD_ID or g.id == int(GUILD_ID)]
    return await summary.collect(guilds, since, SUMMARY_IGNORE_CHANNELS, SUMMARY_MAX_PER_CHANNEL)


async def send_summary(user_id: int, msgs: list[summary.Msg]) -> str | None:
    """Summarize msgs for one person and DM it to them. Returns None on success, or why it failed."""
    if openai_client is None:
        return "Summaries are off (no OPENAI_API_KEY, or summary.enabled is false)."
    try:
        user = await bot.fetch_user(user_id)
    except discord.HTTPException as e:
        return f"Couldn't look up user {user_id}: {e.text}"

    if not msgs:
        chunks = [f"## 📋 Daily Summary\n-# {today():%a, %b} {today().day}\n\nQuiet day, no messages."]
    else:
        try:
            result = await summary.summarize(openai_client, OPENAI_MODEL, msgs, TIMEZONE, user_id)
        except Exception as e:  # network, auth, quota, bad model name...
            log.exception("OpenAI request failed")
            return f"OpenAI request failed: {e}"
        chunks = summary.format_digest(result, msgs, today(), user_id)

    try:
        for chunk in chunks:
            await user.send(chunk, suppress_embeds=True)
    except discord.Forbidden:
        return f"Discord won't let the bot DM {user}. They need to allow DMs from server members."
    except discord.HTTPException as e:
        return f"Discord error {e.status} sending the DM to {user}: {e.text}"
    log.info("Sent summary to %s: %d messages summarized, %d chars in %d DM part(s)",
             user, len(msgs), sum(len(c) for c in chunks), len(chunks))
    return None


@tasks.loop(time=SUMMARY_AT)
async def daily_summary():
    msgs = await collect_messages(24)
    for user_id in SUMMARY_RECIPIENTS:
        error = await send_summary(user_id, msgs)
        if error:
            log.error("Daily summary for %s failed: %s", user_id, error)


@daily_summary.error
async def daily_summary_error(error: BaseException):
    log.exception("Daily summary crashed", exc_info=error)


@bot.tree.command(name="summary", description="DM yourself the server summary right now")
@app_commands.describe(hours="How far back to look (default 24)")
async def summary_cmd(interaction: discord.Interaction, hours: Optional[app_commands.Range[int, 1, 168]] = 24):
    if interaction.user.id not in (OWNER_ID, *SUMMARY_RECIPIENTS):
        await interaction.response.send_message("You're not on the summary list.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    log.info("/summary by %s (%s) for the last %d hours", interaction.user, interaction.user.id, hours)
    error = await send_summary(interaction.user.id, await collect_messages(hours))
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
        log.warning("Daily summary is off (no OPENAI_API_KEY, or summary.enabled is false)")
    elif not daily_summary.is_running():
        daily_summary.start()
        log.info("Next summary DM at %s to %d recipient(s) (model %s)",
                 next_run(SUMMARY_AT), len(SUMMARY_RECIPIENTS), OPENAI_MODEL)


bot.run(TOKEN, log_handler=None)
