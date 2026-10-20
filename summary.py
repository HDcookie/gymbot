"""Daily server summary: collect the day's messages, summarize them with OpenAI, format a DM."""

import datetime as dt
import json
import logging
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

import discord
from openai import AsyncOpenAI

log = logging.getLogger("biblebot.summary")

MAX_CONTENT_CHARS = 500  # per message, so one wall of text can't eat the whole request
DISCORD_LIMIT = 2000


@dataclass
class Msg:
    id: int
    guild_id: int
    channel_id: int
    channel_name: str
    author: str
    is_owner: bool
    time: dt.datetime
    content: str
    reply_to: int | None
    flags: list[str] = field(default_factory=list)

    @property
    def link(self) -> str:
        return f"https://discord.com/channels/{self.guild_id}/{self.channel_id}/{self.id}"


def channel_link(guild_id: int, channel_id: int) -> str:
    return f"https://discord.com/channels/{guild_id}/{channel_id}"


async def collect(guilds, since: dt.datetime, owner_id: int, ignore: set[int], max_per_channel: int) -> list[Msg]:
    """Read every channel and thread the bot can see, back to `since`."""
    msgs: list[Msg] = []
    for guild in guilds:
        channels = [*guild.text_channels, *guild.threads]
        for channel in channels:
            if channel.id in ignore or (getattr(channel, "parent_id", None) in ignore):
                continue
            perms = channel.permissions_for(guild.me)
            if not (perms.view_channel and perms.read_message_history):
                log.info("Skipping #%s: no View Channel / Read Message History", channel)
                continue
            try:
                # Newest first so the cap keeps the latest messages, then put back in order.
                history = [m async for m in channel.history(after=since, limit=max_per_channel, oldest_first=False)]
            except discord.Forbidden:
                log.warning("Skipping #%s: Discord refused to read history", channel)
                continue
            except discord.HTTPException as e:
                log.warning("Skipping #%s: Discord error %s", channel, e.status)
                continue
            if len(history) == max_per_channel:
                log.info("#%s hit the %d message cap; older messages left out", channel, max_per_channel)

            for m in reversed(history):
                if m.author.bot:
                    continue
                msgs.append(to_msg(m, guild.id, owner_id))
    msgs.sort(key=lambda m: m.time)
    log.info("Collected %d messages from %d server(s) since %s", len(msgs), len(guilds), since)
    return msgs


def to_msg(m: discord.Message, guild_id: int, owner_id: int) -> Msg:
    content = m.content
    if m.attachments:
        content += " " + " ".join(f"[attachment: {a.filename}]" for a in m.attachments)
    if m.stickers:
        content += " [sticker]"
    content = content.strip() or "[no text]"
    if len(content) > MAX_CONTENT_CHARS:
        content = content[:MAX_CONTENT_CHARS] + "…"

    flags = []
    is_owner = m.author.id == owner_id
    if not is_owner:
        if any(u.id == owner_id for u in m.mentions):
            flags.append("MENTIONS YOU")
        ref = m.reference.resolved if m.reference else None
        if isinstance(ref, discord.Message) and ref.author.id == owner_id:
            flags.append("REPLY TO YOU")
        if m.mention_everyone:
            flags.append("@everyone/@here")
        if m.role_mentions:
            flags.append("ROLE PING")

    return Msg(
        id=m.id,
        guild_id=guild_id,
        channel_id=m.channel.id,
        channel_name=getattr(m.channel, "name", str(m.channel.id)),
        author=m.author.display_name,
        is_owner=is_owner,
        time=m.created_at,
        content=content,
        reply_to=m.reference.message_id if m.reference else None,
        flags=flags,
    )


SYSTEM_PROMPT = """\
You write a daily digest of a Discord server for its owner, {owner}. Messages from {owner} are marked (YOU).

Return JSON with:
- conversations: EVERY conversation that happened, grouped by topic. Never skip one because it seems
  minor or doesn't need {owner}. For each: the channel_id, a short title, a 1-2 sentence summary of
  what was said (who, what was decided or asked), and start_message_id (the first message of it).
- respond: messages {owner} should look at. Use priority "reply" for anything waiting on them:
  questions or requests to {owner}, mentions, replies to their messages, unanswered questions to the
  group. Use priority "opinion" for questions or discussions that were already answered or settled
  but where {owner}'s view as the owner would still add something. Give a one-line reason for each.
  Don't include messages {owner} already replied to, or messages written by {owner}.

Only use message ids and channel ids that appear in the input. Be concise and plain."""

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["conversations", "respond"],
    "properties": {
        "conversations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["channel_id", "title", "summary", "start_message_id"],
                "properties": {
                    "channel_id": {"type": "string"},
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "start_message_id": {"type": "string"},
                },
            },
        },
        "respond": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["message_id", "priority", "reason"],
                "properties": {
                    "message_id": {"type": "string"},
                    "priority": {"type": "string", "enum": ["reply", "opinion"]},
                    "reason": {"type": "string"},
                },
            },
        },
    },
}


def build_transcript(msgs: list[Msg], tz: ZoneInfo) -> str:
    lines, current = [], None
    for m in msgs:
        if m.channel_id != current:
            current = m.channel_id
            lines.append(f"\n## #{m.channel_name} (channel_id {m.channel_id})")
        who = f"{m.author} (YOU)" if m.is_owner else m.author
        reply = f" (reply to {m.reply_to})" if m.reply_to else ""
        flags = f" [{', '.join(m.flags)}]" if m.flags else ""
        lines.append(f"[{m.id}] {m.time.astimezone(tz):%H:%M} {who}{reply}{flags}: {m.content}")
    return "\n".join(lines).strip()


async def summarize(client: AsyncOpenAI, model: str, msgs: list[Msg], owner: str, tz: ZoneInfo) -> dict:
    transcript = build_transcript(msgs, tz)
    log.info("Sending %d messages (%d chars) to %s", len(msgs), len(transcript), model)
    resp = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT.format(owner=owner)},
            {"role": "user", "content": transcript},
        ],
        response_format={"type": "json_schema", "json_schema": {"name": "digest", "strict": True, "schema": SCHEMA}},
    )
    if resp.usage:
        log.info("OpenAI usage: %d in, %d out tokens", resp.usage.prompt_tokens, resp.usage.completion_tokens)
    return json.loads(resp.choices[0].message.content)


def format_digest(result: dict, msgs: list[Msg], date: dt.date) -> list[str]:
    """Turn the model's JSON into DM text, building every link from real ids."""
    by_id = {str(m.id): m for m in msgs}
    channels = {str(m.channel_id): m for m in msgs}  # any message, for guild id + channel name

    lines = [f"📋 **Server summary for {date:%a %b} {date.day}**"]

    convos_by_channel: dict[str, list[dict]] = {}
    for c in result["conversations"]:
        if c["channel_id"] not in channels:
            log.warning("Dropping conversation with unknown channel id %s", c["channel_id"])
            continue
        convos_by_channel.setdefault(c["channel_id"], []).append(c)

    for channel_id, convos in convos_by_channel.items():
        ch = channels[channel_id]
        lines.append(f"\n**#{ch.channel_name}** · [open channel]({channel_link(ch.guild_id, ch.channel_id)})")
        for c in convos:
            start = by_id.get(c["start_message_id"])
            jump = f" [Jump]({start.link})" if start else ""
            lines.append(f"• **{c['title']}**: {c['summary']}{jump}")

    for priority, heading in (("reply", "🔴 **Waiting on you**"), ("opinion", "💬 **Could use your take**")):
        items = [r for r in result["respond"] if r["priority"] == priority and r["message_id"] in by_id]
        if not items:
            continue
        lines.append(f"\n{heading}")
        for r in items:
            m = by_id[r["message_id"]]
            lines.append(f"• **{m.author}** in #{m.channel_name}: {r['reason']} [Jump]({m.link})")

    dropped = [r["message_id"] for r in result["respond"] if r["message_id"] not in by_id]
    if dropped:
        log.warning("Dropped %d respond items with unknown message ids: %s", len(dropped), dropped)

    return split_message("\n".join(lines))


def split_message(text: str, limit: int = DISCORD_LIMIT) -> list[str]:
    chunks, current = [], ""
    for line in text.split("\n"):
        while len(line) > limit:  # a single huge line: hard-cut it
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks
