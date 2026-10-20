"""Discord bot that posts the day's Bible in a Year readings."""

import datetime as dt
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.environ["DISCORD_TOKEN"]
CHANNEL_ID = int(os.environ["CHANNEL_ID"])
START_DATE = dt.date.fromisoformat(os.environ["START_DATE"])  # the date of Day 1
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "UTC"))
POST_TIME = os.getenv("POST_TIME", "06:00")  # 24h HH:MM in TIMEZONE
# Optional: a server ID makes slash commands show up instantly in that server.
# Without it they sync globally, which can take up to an hour the first time.
GUILD_ID = os.getenv("GUILD_ID")
PLAN_FILE = Path(os.getenv("PLAN_FILE", Path(__file__).with_name("bible_in_a_year.json")))

hour, minute = map(int, POST_TIME.split(":"))
SEND_AT = dt.time(hour=hour, minute=minute, tzinfo=TIMEZONE)

with PLAN_FILE.open(encoding="utf-8") as f:
    PLAN = json.load(f)


def today() -> dt.date:
    return dt.datetime.now(TIMEZONE).date()


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
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()


bot = BibleBot()


@tasks.loop(time=SEND_AT)
async def daily_post():
    entry = reading_for(day_number(today()))
    if entry is None:
        print(f"{today()} is outside the plan (day {day_number(today())}), skipping.")
        return
    channel = bot.get_channel(CHANNEL_ID) or await bot.fetch_channel(CHANNEL_ID)
    await channel.send(format_reading(entry))
    print(f"Posted day {entry['day']}.")


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


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}. Day 1 = {START_DATE}, posting at {POST_TIME} {TIMEZONE}.")
    if not daily_post.is_running():
        daily_post.start()


bot.run(TOKEN)
