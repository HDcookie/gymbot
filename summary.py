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
    author_id: int
    time: dt.datetime
    content: str
    reply_to: int | None
    reply_to_author_id: int | None = None
    mention_ids: set[int] = field(default_factory=set)
    pings_everyone: bool = False
    pings_role: bool = False

    def flags_for(self, reader_id: int) -> list[str]:
        """Why this message might matter to a particular reader."""
        if self.author_id == reader_id:
            return []
        flags = []
        if reader_id in self.mention_ids:
            flags.append("MENTIONS YOU")
        if self.reply_to_author_id == reader_id:
            flags.append("REPLY TO YOU")
        if self.pings_everyone:
            flags.append("@everyone/@here")
        if self.pings_role:
            flags.append("ROLE PING")
        return flags

    @property
    def link(self) -> str:
        return f"https://discord.com/channels/{self.guild_id}/{self.channel_id}/{self.id}"


def channel_link(guild_id: int, channel_id: int) -> str:
    return f"https://discord.com/channels/{guild_id}/{channel_id}"


async def collect(guilds, since: dt.datetime, ignore: set[int], max_per_channel: int) -> list[Msg]:
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
                msgs.append(to_msg(m, guild.id))
    msgs.sort(key=lambda m: m.time)
    log.info("Collected %d messages from %d server(s) since %s", len(msgs), len(guilds), since)
    return msgs


def to_msg(m: discord.Message, guild_id: int) -> Msg:
    content = m.content
    if m.attachments:
        content += " " + " ".join(f"[attachment: {a.filename}]" for a in m.attachments)
    if m.stickers:
        content += " [sticker]"
    content = content.strip() or "[no text]"
    if len(content) > MAX_CONTENT_CHARS:
        content = content[:MAX_CONTENT_CHARS] + "…"

    ref = m.reference.resolved if m.reference else None
    return Msg(
        id=m.id,
        guild_id=guild_id,
        channel_id=m.channel.id,
        channel_name=getattr(m.channel, "name", str(m.channel.id)),
        author=m.author.display_name,
        author_id=m.author.id,
        time=m.created_at,
        content=content,
        reply_to=m.reference.message_id if m.reference else None,
        reply_to_author_id=ref.author.id if isinstance(ref, discord.Message) else None,
        mention_ids={u.id for u in m.mentions},
        pings_everyone=m.mention_everyone,
        pings_role=bool(m.role_mentions),
    )


SYSTEM_PROMPT = """\
You write a daily digest of a Discord server for one reader. Messages written by the reader are marked
(YOU), and messages that mention or reply to them are flagged.

Write about what happened, not a roll call of who was there:
- Use a name when one person is the point: they asked something, shared news, made a decision, or
  are waiting on the reader. In a conversation with several people, don't list who took part; just
  say what was discussed and decided.
- Mention a time only when it matters (a deadline, an event time, something late at night).
- Plain, short, factual. No filler like "The conversation was about" or "Members discussed".

conversations: the discussions worth knowing about.
- Small chats usually aren't worth including: small talk, greetings, jokes, one-off reactions, quick
  exchanges of a few messages. Use judgment; a short exchange that matters (news, a decision,
  something the reader would want to know) still belongs.
- One entry per topic. For each: channel_id; title (2-6 words); summary (one sentence, two at most,
  covering what was discussed and any outcome or decision); start_message_id (its first message).

respond: messages the reader should look at.
- priority "reply": anything waiting on the reader: questions or requests to them, mentions, replies to
  their messages, and questions to the group nobody answered.
- priority "opinion": questions or discussions that were already answered or settled, where the
  reader's view would still add something.
- For each: message_id; title (2-6 words); reason (one short sentence saying what they asked or need;
  the author's name is shown next to it, so don't start with it).
- Skip messages written by the reader and anything the reader already replied to.

Length: aim for the finished DM to be 2000 characters or less. Each item you return also gets about
100 characters of links and formatting added, so keep your text tight. On a busy day, prefer fewer,
more important items and shorter summaries. Going over is OK when there's a lot that matters, but
it isn't ideal.

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


def build_transcript(msgs: list[Msg], tz: ZoneInfo, reader_id: int) -> str:
    lines, current = [], None
    for m in msgs:
        if m.channel_id != current:
            current = m.channel_id
            lines.append(f"\n## #{m.channel_name} (channel_id {m.channel_id})")
        who = f"{m.author} (YOU)" if m.author_id == reader_id else m.author
        reply = f" (reply to {m.reply_to})" if m.reply_to else ""
        flags = m.flags_for(reader_id)
        flags = f" [{', '.join(flags)}]" if flags else ""
        lines.append(f"[{m.id}] {m.time.astimezone(tz):%a %H:%M} {who}{reply}{flags}: {m.content}")
    return "\n".join(lines).strip()


async def summarize(client: AsyncOpenAI, model: str, msgs: list[Msg], tz: ZoneInfo, reader_id: int) -> dict:
    transcript = build_transcript(msgs, tz, reader_id)
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


def format_digest(result: dict, msgs: list[Msg], date: dt.date, reader_id: int) -> list[str]:
    """Turn the model's JSON into DM text, building every link from real ids."""
    by_id = {str(m.id): m for m in msgs}
    channels = {str(m.channel_id): m for m in msgs}  # any message, for guild id + channel name

    respond = []
    for r in result["respond"]:
        m = by_id.get(r["message_id"])
        if m is None:
            log.warning("Dropping respond item with unknown message id %s", r["message_id"])
        elif m.author_id != reader_id:
            respond.append((r, m))

    convos_by_channel: dict[str, list[dict]] = {}
    for c in result["conversations"]:
        if c["channel_id"] not in channels:
            log.warning("Dropping conversation with unknown channel id %s", c["channel_id"])
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
            lines.append(f"- **{r['title']}** ({m.author}, #{m.channel_name}): {r['reason']} [Jump]({m.link})")
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

    lines += ["", f"-# {len(msgs)} messages read · small chats left out"]
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
