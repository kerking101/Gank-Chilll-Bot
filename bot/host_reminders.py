from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.config import load_config
from bot.host_cooldown import (
    _is_mod,
    has_content_host_role,
    is_host_content_channel,
)
from bot.split import MESSAGE_LINK_RE, parse_message_link

REMINDERS_PATH = Path(__file__).resolve().parent.parent / "data" / "reminders.json"

TIMESTAMP_RE = re.compile(r"<t:(\d+)(?::[^>]+)?>")
ROLE_MENTION_RE = re.compile(r"<@&(\d+)>")


def _reminder_settings() -> dict[str, Any]:
    cfg = load_config().get("host_reminders") or {}
    return {
        "enabled": bool(cfg.get("enabled", True)),
        "minutes_before": int(cfg.get("minutes_before", 15)),
    }


def _load_reminders() -> dict[str, Any]:
    if not REMINDERS_PATH.exists():
        return {"reminders": {}}
    with open(REMINDERS_PATH, encoding="utf-8-sig") as f:
        data = json.load(f)
    if "reminders" not in data:
        data = {"reminders": data}
    return data


def _save_reminders(data: dict[str, Any]) -> None:
    REMINDERS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = REMINDERS_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp.replace(REMINDERS_PATH)


def _parse_event_time(content: str) -> datetime | None:
    """Earliest future Discord timestamp in the message."""
    now = datetime.now(timezone.utc)
    best: datetime | None = None
    for m in TIMESTAMP_RE.finditer(content or ""):
        try:
            dt = datetime.fromtimestamp(int(m.group(1)), tz=timezone.utc)
        except (ValueError, OSError):
            continue
        if dt <= now:
            continue
        if best is None or dt < best:
            best = dt
    return best


def _parse_role_ids(content: str) -> list[int]:
    seen: set[int] = set()
    out: list[int] = []
    for m in ROLE_MENTION_RE.finditer(content or ""):
        rid = int(m.group(1))
        if rid in seen:
            continue
        seen.add(rid)
        out.append(rid)
    return out


def _event_label(content: str) -> str:
    if not content:
        return "Event"
    line = content.splitlines()[0]
    line = ROLE_MENTION_RE.sub("", line)
    line = TIMESTAMP_RE.sub("", line)
    line = re.sub(r"^#+\s*", "", line)
    line = re.sub(r"\s*/\s*$", "", line)
    line = re.sub(r"\s+", " ", line).strip(" -–|/")
    return line[:80] or "Event"


def _can_schedule_host(member: discord.Member) -> bool:
    return _is_mod(member) or has_content_host_role(member)


def cancel_reminder_for_message(message_id: int) -> bool:
    data = _load_reminders()
    key = str(message_id)
    if key not in data["reminders"]:
        return False
    entry = data["reminders"][key]
    if entry.get("status") == "pending":
        entry["status"] = "cancelled"
        data["reminders"][key] = entry
        _save_reminders(data)
        return True
    return False


def schedule_reminder(
    *,
    guild_id: int,
    channel_id: int,
    message_id: int,
    event_at: datetime,
    role_ids: list[int],
    label: str,
    minutes_before: int,
    created_by: int,
) -> tuple[bool, str, dict[str, Any] | None]:
    settings = _reminder_settings()
    if not settings["enabled"]:
        return False, "Host reminders are disabled in config.", None

    if not role_ids:
        return False, "No role ping found — add `<@&roleId>` to your host post.", None

    now = datetime.now(timezone.utc)
    remind_at = event_at - timedelta(minutes=minutes_before)
    if remind_at <= now:
        return False, f"Event is too soon for a **{minutes_before} min** reminder.", None

    data = _load_reminders()
    key = str(message_id)
    entry: dict[str, Any] = {
        "id": key,
        "guild_id": guild_id,
        "channel_id": channel_id,
        "message_id": message_id,
        "event_at": event_at.isoformat(),
        "remind_at": remind_at.isoformat(),
        "minutes_before": minutes_before,
        "role_ids": role_ids,
        "label": label,
        "created_by": created_by,
        "status": "pending",
        "created_at": now.isoformat(),
    }
    data["reminders"][key] = entry
    _save_reminders(data)
    unix = int(event_at.timestamp())
    pings = " ".join(f"<@&{r}>" for r in role_ids)
    return (
        True,
        f"Reminder set — {pings} ping **{minutes_before} min** before "
        f"**{label}** (<t:{unix}:R>).",
        entry,
    )


async def maybe_schedule_host_reminder(message: discord.Message) -> None:
    """Auto-schedule from a normal host post — no template changes needed."""
    if message.author.bot or not message.guild:
        return
    if not is_host_content_channel(message.channel):
        return
    if not isinstance(message.author, discord.Member):
        return
    if not _can_schedule_host(message.author):
        return

    settings = _reminder_settings()
    if not settings["enabled"]:
        return

    event_at = _parse_event_time(message.content or "")
    if not event_at:
        return

    role_ids = _parse_role_ids(message.content or "")
    if not role_ids:
        return

    ok, _msg, _entry = schedule_reminder(
        guild_id=message.guild.id,
        channel_id=message.channel.id,
        message_id=message.id,
        event_at=event_at,
        role_ids=role_ids,
        label=_event_label(message.content or ""),
        minutes_before=settings["minutes_before"],
        created_by=message.author.id,
    )
    if ok:
        try:
            await message.add_reaction("⏰")
        except discord.HTTPException:
            pass


async def _fire_reminder(bot: commands.Bot, entry: dict[str, Any]) -> None:
    channel = bot.get_channel(int(entry["channel_id"]))
    if channel is None:
        try:
            channel = await bot.fetch_channel(int(entry["channel_id"]))
        except discord.HTTPException:
            channel = None
    if not isinstance(channel, discord.TextChannel):
        entry["status"] = "failed"
        return

    try:
        host_msg = await channel.fetch_message(int(entry["message_id"]))
    except discord.HTTPException:
        entry["status"] = "cancelled"
        return

    event_at = datetime.fromisoformat(entry["event_at"])
    unix = int(event_at.timestamp())
    pings = " ".join(f"<@&{r}>" for r in entry.get("role_ids") or [])
    label = entry.get("label") or "Event"
    mins = int(entry.get("minutes_before") or 15)

    text = (
        f"{pings} **Reminder** — **{label}** starts <t:{unix}:R> (<t:{unix}:t>)\n"
        f"{mins} min heads-up · [host post]({host_msg.jump_url})"
    )
    try:
        await channel.send(text, allowed_mentions=discord.AllowedMentions(roles=True))
        entry["status"] = "sent"
        entry["sent_at"] = datetime.now(timezone.utc).isoformat()
    except discord.HTTPException:
        entry["status"] = "failed"


async def _resolve_host_message(
    interaction: discord.Interaction,
    message: str | None,
) -> discord.Message | None:
    assert interaction.guild and interaction.channel

    if message:
        link = parse_message_link(message)
        if link:
            guild_id, channel_id, message_id = link
            if guild_id != interaction.guild.id:
                return None
            ch = interaction.guild.get_channel(channel_id) or interaction.client.get_channel(channel_id)
            if ch is None:
                try:
                    ch = await interaction.client.fetch_channel(channel_id)
                except discord.HTTPException:
                    return None
            if not isinstance(ch, (discord.TextChannel, discord.Thread)):
                return None
            try:
                return await ch.fetch_message(message_id)
            except discord.HTTPException:
                return None
        if message.isdigit() and isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
            try:
                return await interaction.channel.fetch_message(int(message))
            except discord.HTTPException:
                return None

    if isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
        async for msg in interaction.channel.history(limit=25):
            if msg.author.id != interaction.user.id:
                continue
            if msg.reference and msg.reference.message_id:
                try:
                    return await interaction.channel.fetch_message(msg.reference.message_id)
                except discord.HTTPException:
                    return None
            break
    return None


def register_host_reminders(bot: commands.Bot) -> None:
    remind = app_commands.Group(name="remind", description="Host reminder pings (keeps your post format)")

    @remind.command(
        name="set",
        description="Schedule a reminder ping on a host post (uses role + timestamp from the message)",
    )
    @app_commands.describe(
        minutes_before="Minutes before start to ping (default from config, usually 15)",
        message="Message link — optional if you replied to the host post",
    )
    async def remind_set(
        interaction: discord.Interaction,
        minutes_before: app_commands.Range[int, 1, 180] | None = None,
        message: str | None = None,
    ) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        if not _can_schedule_host(interaction.user):
            await interaction.response.send_message(
                "Only **Mod** or **content role** hosts can set reminders.",
                ephemeral=True,
            )
            return

        host = await _resolve_host_message(interaction, message)
        if not host:
            await interaction.response.send_message(
                "Couldn't find the host message — paste a **message link** or reply to the post first.",
                ephemeral=True,
            )
            return
        if isinstance(host.channel, discord.Thread):
            ch = host.channel.parent
        else:
            ch = host.channel
        if not ch or not is_host_content_channel(ch):
            await interaction.response.send_message(
                "That message isn't in **#host-content**.",
                ephemeral=True,
            )
            return

        event_at = _parse_event_time(host.content or "")
        if not event_at:
            await interaction.response.send_message(
                "No future Discord timestamp found — use `<t:unix:R>` in your host post.",
                ephemeral=True,
            )
            return

        role_ids = _parse_role_ids(host.content or "")
        mins = minutes_before if minutes_before is not None else _reminder_settings()["minutes_before"]
        ok, msg, _entry = schedule_reminder(
            guild_id=interaction.guild.id,
            channel_id=ch.id,
            message_id=host.id,
            event_at=event_at,
            role_ids=role_ids,
            label=_event_label(host.content or ""),
            minutes_before=mins,
            created_by=interaction.user.id,
        )
        await interaction.response.send_message(msg, ephemeral=True)
        if ok:
            try:
                await host.add_reaction("⏰")
            except discord.HTTPException:
                pass

    @remind.command(name="cancel", description="Cancel the reminder on a host post")
    @app_commands.describe(message="Message link — optional if you replied to the host post")
    async def remind_cancel(
        interaction: discord.Interaction,
        message: str | None = None,
    ) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        if not _can_schedule_host(interaction.user):
            await interaction.response.send_message("Only **Mod** or content hosts.", ephemeral=True)
            return

        host = await _resolve_host_message(interaction, message)
        if not host:
            await interaction.response.send_message("Couldn't find that host message.", ephemeral=True)
            return
        if cancel_reminder_for_message(host.id):
            await interaction.response.send_message("Reminder cancelled.", ephemeral=True)
        else:
            await interaction.response.send_message("No pending reminder on that post.", ephemeral=True)

    @remind.command(name="list", description="Upcoming host reminder pings")
    async def remind_list(interaction: discord.Interaction) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        if not _can_schedule_host(interaction.user):
            await interaction.response.send_message("Only **Mod** or content hosts.", ephemeral=True)
            return

        now = datetime.now(timezone.utc)
        pending = [
            e
            for e in _load_reminders()["reminders"].values()
            if e.get("guild_id") == interaction.guild.id and e.get("status") == "pending"
        ]
        pending.sort(key=lambda e: e.get("remind_at") or "")
        if not pending:
            await interaction.response.send_message("No upcoming reminders.", ephemeral=True)
            return

        lines = ["**Upcoming host reminders:**"]
        for e in pending[:15]:
            label = e.get("label") or "Event"
            remind_at = e.get("remind_at") or ""
            try:
                dt = datetime.fromisoformat(remind_at)
                when = f"<t:{int(dt.timestamp())}:R>"
            except ValueError:
                when = remind_at[:16]
            lines.append(f"• **{label}** — ping {when} · `{e.get('id')}`")
        await interaction.response.send_message("\n".join(lines)[:2000], ephemeral=True)

    bot.tree.add_command(remind)

    @tasks.loop(seconds=30)
    async def reminder_tick() -> None:
        data = _load_reminders()
        now = datetime.now(timezone.utc)
        changed = False
        for key, entry in list(data["reminders"].items()):
            if entry.get("status") != "pending":
                continue
            try:
                remind_at = datetime.fromisoformat(entry["remind_at"])
            except ValueError:
                entry["status"] = "failed"
                data["reminders"][key] = entry
                changed = True
                continue
            if remind_at > now:
                continue
            await _fire_reminder(bot, entry)
            data["reminders"][key] = entry
            changed = True
        if changed:
            _save_reminders(data)

    @bot.listen("ready")
    async def _start_reminders() -> None:
        if not reminder_tick.is_running():
            reminder_tick.start()

    @bot.listen("message_delete")
    async def _on_host_delete(message: discord.Message) -> None:
        cancel_reminder_for_message(message.id)
