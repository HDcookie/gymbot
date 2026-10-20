"""Daily server summary: collect the day's messages, summarize them with OpenAI, format a DM."""

import datetime as dt
import json
import logging
from dataclasses import dataclass, field

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
You write a daily digest of a Discord server for its owner. Messages from the owner are marked (YOU).

Write about topics, never about people:
- No names, usernames, nicknames or times anywhere in your output. Describe what was said, not who
  said it: "Asked whether the plan restarts in January", "Meetup moved to 10 AM Saturday".
- Plain, short, factual. No filler like "The conversation was about" or "Members discussed".

conversations: the discussions worth knowing about.
- Leave out small talk, greetings, jokes, one-off reactions, and anything that's only a few
  messages, unless the owner needs to see it.
- One entry per topic. For each: channel_id; title (2-6 words); summary (one sentence, two at most,
  covering what was discussed and any outcome or decision); start_message_id and end_message_id
  (the first and last message of that discussion).

respond: messages the owner should look at.
- priority "reply": anything waiting on the owner: questions or requests to them, mentions, replies to
  their messages, and questions to the group nobody answered.
- priority "opinion": questions or discussions that were already answered or settled, where the
  owner's view would still add something.
- For each: message_id; title (2-6 words); reason (one short sentence saying what's needed, no names).
- Skip messages written by the owner and anything the owner already replied to.

Only use message ids and channel ids that appear in the input."""

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
                "required": ["channel_id", "title", "summary", "start_message_id", "end_message_id"],
                "properties": {
                    "channel_id": {"type": "string"},
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "start_message_id": {"type": "string"},
                    "end_message_id": {"type": "string"},
                },
            },
        },
        "respond": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["message_id", "priority", "title", "reason"],
                "properties": {
                    "message_id": {"type": "string"},
                    "priority": {"type": "string", "enum": ["reply", "opinion"]},
                    "title": {"type": "string"},
                    "reason": {"type": "string"},
                },
            },
        },
    },
}


def build_transcript(msgs: list[Msg]) -> str:
    lines, current = [], None
    for m in msgs:
        if m.channel_id != current:
            current = m.channel_id
            lines.append(f"\n## #{m.channel_name} (channel_id {m.channel_id})")
        who = f"{m.author} (YOU)" if m.is_owner else m.author
        reply = f" (reply to {m.reply_to})" if m.reply_to else ""
        flags = f" [{', '.join(m.flags)}]" if m.flags else ""
        lines.append(f"[{m.id}] {who}{reply}{flags}: {m.content}")
    return "\n".join(lines).strip()


async def summarize(client: AsyncOpenAI, model: str, msgs: list[Msg]) -> dict:
    transcript = build_transcript(msgs)
    log.info("Sending %d messages (%d chars) to %s", len(msgs), len(transcript), model)
    resp = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": transcript},
        ],
        response_format={"type": "json_schema", "json_schema": {"name": "digest", "strict": True, "schema": SCHEMA}},
    )
    if resp.usage:
        log.info("OpenAI usage: %d in, %d out tokens", resp.usage.prompt_tokens, resp.usage.completion_tokens)
    return json.loads(resp.choices[0].message.content)


def conversation_messages(c: dict, msgs: list[Msg], by_id: dict[str, Msg]) -> list[Msg]:
    """The messages in a conversation's channel between its first and last message."""
    start, end = by_id.get(c["start_message_id"]), by_id.get(c["end_message_id"])
    if start is None:
        return []
    end = end or start
    return [m for m in msgs if m.channel_id == start.channel_id and start.time <= m.time <= end.time]


def format_digest(result: dict, msgs: list[Msg], date: dt.date, min_messages: int) -> list[str]:
    """Turn the model's JSON into DM text, building every link from real ids."""
    by_id = {str(m.id): m for m in msgs}
    channels = {str(m.channel_id): m for m in msgs}  # any message, for guild id + channel name

    respond = []
    for r in result["respond"]:
        m = by_id.get(r["message_id"])
        if m is None:
            log.warning("Dropping respond item with unknown message id %s", r["message_id"])
        elif not m.is_owner:
            respond.append((r, m))
    respond_ids = {m.id for _, m in respond}

    # Keep a conversation if it's long enough, or if something in it needs the owner.
    convos_by_channel: dict[str, list[dict]] = {}
    skipped = 0
    for c in result["conversations"]:
        if c["channel_id"] not in channels:
            log.warning("Dropping conversation with unknown channel id %s", c["channel_id"])
            continue
        in_convo = conversation_messages(c, msgs, by_id)
        if len(in_convo) < min_messages and not respond_ids & {m.id for m in in_convo}:
            skipped += 1
            continue
        convos_by_channel.setdefault(c["channel_id"], []).append(c)

    replies = [(r, m) for r, m in respond if r["priority"] == "reply"]
    opinions = [(r, m) for r, m in respond if r["priority"] == "opinion"]
    kept = sum(len(c) for c in convos_by_channel.values())

    stats = [f"{date:%a, %b} {date.day}"]
    stats.append(f"{len(replies)} need{'s' if len(replies) == 1 else ''} you" if replies else "nothing needs you")
    stats.append(f"{kept} conversation{'' if kept == 1 else 's'}")
    lines = ["## 📋 Daily Summary", f"-# {' · '.join(stats)}"]

    for heading, items in (("🔴 Needs your reply", replies), ("💬 Your take would help", opinions)):
        if not items:
            continue
        lines += ["", f"### {heading}"]
        for r, m in items:
            lines.append(f"- **{r['title']}**: {r['reason']} · #{m.channel_name} · [Jump]({m.link})")
    if not replies and not opinions:
        lines += ["", "### ✅ Nothing needs you today"]

    lines += ["", "### 📝 What happened"]
    if not convos_by_channel:
        lines.append("Nothing notable, just small talk.")
    for channel_id, convos in convos_by_channel.items():
        ch = channels[channel_id]
        lines += ["", f"**#{ch.channel_name}** · [Open]({channel_link(ch.guild_id, ch.channel_id)})"]
        for c in convos:
            start = by_id.get(c["start_message_id"])
            jump = f" [Jump]({start.link})" if start else ""
            lines.append(f"- **{c['title']}**: {c['summary']}{jump}")

    footer = f"{len(msgs)} messages read"
    if skipped:
        footer += f" · {skipped} short conversation{'' if skipped == 1 else 's'} left out"
    lines += ["", f"-# {footer}"]
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
