from __future__ import annotations

import asyncio
import json
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from discord.utils import get

from bot.config import load_config

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "temp_voices.json"

# channel_id -> owner_id
_owners: dict[int, int] = {}
_creating: set[int] = set()
_lock = asyncio.Lock()


def load_owners() -> None:
    global _owners
    if not DATA_PATH.exists():
        _owners = {}
        return
    with open(DATA_PATH, encoding="utf-8") as f:
        raw = json.load(f)
    _owners = {int(k): int(v) for k, v in raw.items()}


def _save() -> None:
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump({str(k): v for k, v in _owners.items()}, f, indent=2)


def hub_channel_names() -> set[str]:
    names: set[str] = set()
    for channels in (load_config().get("categories") or {}).values():
        for spec in channels:
            if spec.get("temp_voice_hub"):
                names.add(spec["name"])
                names.update(spec.get("rename_from") or [])
    return names or {"➕・Create VC", "Create VC"}


def is_hub(channel: discord.abc.GuildChannel | None) -> bool:
    return isinstance(channel, discord.VoiceChannel) and channel.name in hub_channel_names()


def is_temp(channel_id: int) -> bool:
    return channel_id in _owners


def _is_owner(member: discord.Member, channel: discord.VoiceChannel) -> bool:
    oid = _owners.get(channel.id)
    if oid and member.id == oid:
        return True
    mod = get(member.guild.roles, name="Mod")
    return bool(mod and mod in member.roles)


async def create_temp_voice(member: discord.Member, hub: discord.VoiceChannel) -> None:
    if member.id in _creating:
        return
    _creating.add(member.id)
    try:
        category = hub.category
        everyone = member.guild.default_role
        member_role = get(member.guild.roles, name="Member")
        unregistered = get(member.guild.roles, name="Unregistered")

        overs: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
            everyone: discord.PermissionOverwrite(view_channel=False),
            member: discord.PermissionOverwrite(
                view_channel=True,
                connect=True,
                speak=True,
                stream=True,
                manage_channels=True,
                move_members=True,
                mute_members=True,
            ),
        }
        if member_role:
            overs[member_role] = discord.PermissionOverwrite(
                view_channel=True,
                connect=True,
                speak=True,
                stream=True,
            )
        if unregistered:
            overs[unregistered] = discord.PermissionOverwrite(view_channel=False)

        name = f"🎮・{member.display_name}"[:100]
        channel = await member.guild.create_voice_channel(
            name,
            category=category,
            overwrites=overs,
            reason=f"Temp VC for {member}",
        )
        _owners[channel.id] = member.id
        _save()

        try:
            await member.move_to(channel, reason="Joined Create VC hub")
        except discord.HTTPException:
            pass

        await post_vc_chat_panel(channel)
    finally:
        _creating.discard(member.id)


async def cleanup_if_empty(guild: discord.Guild, channel_id: int) -> None:
    if channel_id not in _owners:
        return
    ch = guild.get_channel(channel_id)
    if not isinstance(ch, discord.VoiceChannel):
        _owners.pop(channel_id, None)
        _save()
        return
    if len(ch.members) > 0:
        return
    _owners.pop(channel_id, None)
    _save()
    try:
        await ch.delete(reason="Temp VC empty")
    except discord.HTTPException:
        pass


async def handle_voice_state(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
) -> None:
    async with _lock:
        if after.channel and is_hub(after.channel):
            await create_temp_voice(member, after.channel)

        if before.channel and is_temp(before.channel.id):
            await cleanup_if_empty(member.guild, before.channel.id)


class RenameModal(discord.ui.Modal, title="Rename voice channel"):
    new_name = discord.ui.TextInput(
        label="Channel name",
        placeholder="e.g. Ganking duo",
        min_length=1,
        max_length=100,
        required=True,
    )

    def __init__(self, channel: discord.VoiceChannel) -> None:
        super().__init__()
        self.channel = channel
        self.new_name.default = channel.name

    async def on_submit(self, interaction: discord.Interaction) -> None:
        name = str(self.new_name).strip()
        try:
            await self.channel.edit(name=name, reason=f"Renamed by {interaction.user}")
            await interaction.response.send_message(f"Renamed to **{name}**", ephemeral=True)
        except discord.HTTPException as e:
            await interaction.response.send_message(f"Couldn't rename: {e}", ephemeral=True)


class LimitModal(discord.ui.Modal, title="Set user limit"):
    limit = discord.ui.TextInput(
        label="User limit (0 = unlimited)",
        placeholder="0–99",
        min_length=1,
        max_length=2,
        required=True,
    )

    def __init__(self, channel: discord.VoiceChannel) -> None:
        super().__init__()
        self.channel = channel
        self.limit.default = str(channel.user_limit)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = str(self.limit).strip()
        if not raw.isdigit():
            await interaction.response.send_message("Enter a number 0–99.", ephemeral=True)
            return
        value = min(99, int(raw))
        try:
            await self.channel.edit(user_limit=value, reason=f"Limit set by {interaction.user}")
            label = "unlimited" if value == 0 else str(value)
            await interaction.response.send_message(f"User limit set to **{label}**", ephemeral=True)
        except discord.HTTPException as e:
            await interaction.response.send_message(f"Couldn't set limit: {e}", ephemeral=True)


async def _set_locked(channel: discord.VoiceChannel, *, locked: bool) -> None:
    member_role = get(channel.guild.roles, name="Member")
    everyone = channel.guild.default_role
    if locked:
        await channel.set_permissions(everyone, connect=False, view_channel=False)
        if member_role:
            await channel.set_permissions(member_role, connect=False, view_channel=True)
        for m in channel.members:
            await channel.set_permissions(m, connect=True, view_channel=True, speak=True)
    else:
        await channel.set_permissions(everyone, connect=False, view_channel=False)
        if member_role:
            await channel.set_permissions(
                member_role, connect=True, view_channel=True, speak=True, stream=True
            )


async def _set_ptt(channel: discord.VoiceChannel, *, enabled: bool) -> None:
    """enabled=True → force push-to-talk; False → allow voice activation (open mic)."""
    member_role = get(channel.guild.roles, name="Member")
    everyone = channel.guild.default_role
    vad_allowed = not enabled  # PTT on => VAD denied

    ow_everyone = channel.overwrites_for(everyone)
    ow_everyone.use_voice_activation = vad_allowed
    await channel.set_permissions(everyone, overwrite=ow_everyone)

    if member_role:
        ow = channel.overwrites_for(member_role)
        ow.use_voice_activation = vad_allowed
        if ow.view_channel is not False:
            if ow.connect is not False:
                ow.connect = True
            if ow.speak is not False:
                ow.speak = True
        await channel.set_permissions(member_role, overwrite=ow)

    for m in channel.members:
        ow = channel.overwrites_for(m)
        ow.use_voice_activation = vad_allowed
        ow.connect = True
        ow.speak = True
        await channel.set_permissions(m, overwrite=ow)


def _can_control_vc(member: discord.Member, channel: discord.VoiceChannel) -> bool:
    mod = get(member.guild.roles, name="Mod")
    if mod and mod in member.roles:
        return True
    return is_temp(channel.id) and _owners.get(channel.id) == member.id


def _current_voice(interaction: discord.Interaction) -> discord.VoiceChannel | None:
    if not isinstance(interaction.user, discord.Member):
        return None
    voice = interaction.user.voice
    if not voice or not isinstance(voice.channel, discord.VoiceChannel):
        return None
    return voice.channel


def _resolve_vc(interaction: discord.Interaction) -> discord.VoiceChannel | None:
    ch = interaction.channel
    if isinstance(ch, discord.VoiceChannel):
        return ch
    # fallback: whatever VC the clicker is in
    return _current_voice(interaction)


class VCChatPanelView(discord.ui.View):
    """Persistent buttons posted in the VC text chat — click to control that channel."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="PTT On",
        style=discord.ButtonStyle.danger,
        emoji="🔇",
        custom_id="kerp:vcchat:ptt_on",
        row=0,
    )
    async def ptt_on(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._ptt(interaction, enabled=True)

    @discord.ui.button(
        label="PTT Off",
        style=discord.ButtonStyle.success,
        emoji="🎤",
        custom_id="kerp:vcchat:ptt_off",
        row=0,
    )
    async def ptt_off(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._ptt(interaction, enabled=False)

    @discord.ui.button(
        label="Lock",
        style=discord.ButtonStyle.secondary,
        emoji="🔒",
        custom_id="kerp:vcchat:lock",
        row=0,
    )
    async def lock(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        channel = await self._gated(interaction)
        if not channel:
            return
        await _set_locked(channel, locked=True)
        await interaction.response.send_message("**Locked** — no new joins.", ephemeral=True)

    @discord.ui.button(
        label="Unlock",
        style=discord.ButtonStyle.secondary,
        emoji="🔓",
        custom_id="kerp:vcchat:unlock",
        row=0,
    )
    async def unlock(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        channel = await self._gated(interaction)
        if not channel:
            return
        await _set_locked(channel, locked=False)
        await interaction.response.send_message("**Unlocked.**", ephemeral=True)

    @discord.ui.button(
        label="Rename",
        style=discord.ButtonStyle.primary,
        emoji="✏️",
        custom_id="kerp:vcchat:rename",
        row=1,
    )
    async def rename(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        channel = await self._gated(interaction)
        if not channel:
            return
        await interaction.response.send_modal(RenameModal(channel))

    @discord.ui.button(
        label="Limit",
        style=discord.ButtonStyle.primary,
        emoji="👥",
        custom_id="kerp:vcchat:limit",
        row=1,
    )
    async def limit(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        channel = await self._gated(interaction)
        if not channel:
            return
        await interaction.response.send_modal(LimitModal(channel))

    async def _gated(self, interaction: discord.Interaction) -> discord.VoiceChannel | None:
        assert isinstance(interaction.user, discord.Member)
        channel = _resolve_vc(interaction)
        if not channel:
            await interaction.response.send_message("Couldn't find this voice channel.", ephemeral=True)
            return None
        if not _can_control_vc(interaction.user, channel):
            await interaction.response.send_message(
                "Only the **VC owner** or a **Mod** can use these.",
                ephemeral=True,
            )
            return None
        return channel

    async def _ptt(self, interaction: discord.Interaction, *, enabled: bool) -> None:
        channel = await self._gated(interaction)
        if not channel:
            return
        await _set_ptt(channel, enabled=enabled)
        if enabled:
            await interaction.response.send_message(
                "**Push-to-talk ON** — open mic off. Rejoin VC if it doesn't apply yet.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "**Push-to-talk OFF** — open mic on. Rejoin VC if it doesn't apply yet.",
                ephemeral=True,
            )


def build_vc_chat_embed(channel: discord.VoiceChannel) -> discord.Embed:
    return discord.Embed(
        title="🎚️ Voice controls",
        description=(
            f"Controls for **{channel.name}**\n\n"
            "🔇 **PTT On** — force push-to-talk\n"
            "🎤 **PTT Off** — allow open mic\n"
            "🔒 / 🔓 — lock or unlock joins\n"
            "✏️ / 👥 — rename or set user limit"
        ),
        color=0x5865F2,
    )


async def post_vc_chat_panel(channel: discord.VoiceChannel) -> discord.Message | None:
    """Post (or refresh) the button panel in this VC's text chat."""
    try:
        async for msg in channel.history(limit=20):
            if msg.author == channel.guild.me and msg.embeds:
                if msg.embeds[0].title == "🎚️ Voice controls":
                    await msg.delete()
    except (discord.Forbidden, discord.HTTPException):
        pass

    try:
        return await channel.send(embed=build_vc_chat_embed(channel), view=VCChatPanelView())
    except discord.Forbidden:
        return None
    except discord.HTTPException:
        return None


async def post_static_vc_panels(guild: discord.Guild) -> list[str]:
    """Put control panels in permanent VCs (not the Create hub)."""
    posted: list[str] = []
    hubs = hub_channel_names()
    for ch in guild.voice_channels:
        if ch.name in hubs:
            continue
        if is_temp(ch.id):
            continue
        msg = await post_vc_chat_panel(ch)
        if msg:
            posted.append(ch.name)
    return posted


class VoiceControlView(discord.ui.View):
    """Ephemeral fallback panel (e.g. from /vc panel if chat post fails)."""

    def __init__(self, channel: discord.VoiceChannel) -> None:
        super().__init__(timeout=180)
        self.channel = channel

    @discord.ui.button(label="PTT On", style=discord.ButtonStyle.danger, emoji="🔇", row=0)
    async def ptt_on(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        assert isinstance(interaction.user, discord.Member)
        if not _can_control_vc(interaction.user, self.channel):
            await interaction.response.send_message("You can't control this VC.", ephemeral=True)
            return
        await _set_ptt(self.channel, enabled=True)
        await interaction.response.send_message("**Push-to-talk ON**", ephemeral=True)

    @discord.ui.button(label="PTT Off", style=discord.ButtonStyle.success, emoji="🎤", row=0)
    async def ptt_off(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        assert isinstance(interaction.user, discord.Member)
        if not _can_control_vc(interaction.user, self.channel):
            await interaction.response.send_message("You can't control this VC.", ephemeral=True)
            return
        await _set_ptt(self.channel, enabled=False)
        await interaction.response.send_message("**Push-to-talk OFF**", ephemeral=True)


def _require_temp_vc(interaction: discord.Interaction) -> discord.VoiceChannel | None:
    channel = _current_voice(interaction)
    if not channel or not is_temp(channel.id):
        return None
    return channel


def register_temp_voice(bot: commands.Bot) -> None:
    load_owners()

    vc = app_commands.Group(name="vc", description="Voice channel controls")

    @vc.command(name="panel", description="Post voice control buttons in this VC's chat")
    async def vc_panel(interaction: discord.Interaction) -> None:
        channel = _current_voice(interaction)
        if not channel:
            await interaction.response.send_message("Join a voice channel first.", ephemeral=True)
            return
        assert isinstance(interaction.user, discord.Member)
        if not _can_control_vc(interaction.user, channel):
            await interaction.response.send_message(
                "Only the **VC owner** or a **Mod** can post controls.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        msg = await post_vc_chat_panel(channel)
        if msg:
            await interaction.followup.send("Control panel posted in the **VC chat**.", ephemeral=True)
        else:
            await interaction.followup.send(
                "Couldn't post in VC chat (missing permission). Use the buttons below instead.",
                view=VoiceControlView(channel),
                ephemeral=True,
            )

    @vc.command(name="name", description="Rename your temp voice channel")
    @app_commands.describe(name="New channel name")
    async def vc_name(interaction: discord.Interaction, name: str) -> None:
        channel = _require_temp_vc(interaction)
        if not channel or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("You're not in your temp VC.", ephemeral=True)
            return
        if not _can_control_vc(interaction.user, channel):
            await interaction.response.send_message("Only the VC owner can rename it.", ephemeral=True)
            return
        name = name.strip()[:100]
        await channel.edit(name=name, reason=f"Renamed by {interaction.user}")
        await interaction.response.send_message(f"Renamed to **{name}**", ephemeral=True)

    @vc.command(name="limit", description="Set a user limit on your temp voice channel")
    @app_commands.describe(limit="0 = unlimited, max 99")
    async def vc_limit(interaction: discord.Interaction, limit: app_commands.Range[int, 0, 99]) -> None:
        channel = _require_temp_vc(interaction)
        if not channel or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("You're not in your temp VC.", ephemeral=True)
            return
        if not _can_control_vc(interaction.user, channel):
            await interaction.response.send_message("Only the VC owner can set the limit.", ephemeral=True)
            return
        await channel.edit(user_limit=limit, reason=f"Limit set by {interaction.user}")
        label = "unlimited" if limit == 0 else str(limit)
        await interaction.response.send_message(f"User limit set to **{label}**", ephemeral=True)

    @vc.command(name="lock", description="Lock your temp voice (no new joins)")
    async def vc_lock(interaction: discord.Interaction) -> None:
        channel = _require_temp_vc(interaction)
        if not channel or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("You're not in your temp VC.", ephemeral=True)
            return
        if not _can_control_vc(interaction.user, channel):
            await interaction.response.send_message("Only the VC owner can lock it.", ephemeral=True)
            return
        await _set_locked(channel, locked=True)
        await interaction.response.send_message("Channel **locked**.", ephemeral=True)

    @vc.command(name="unlock", description="Unlock your temp voice")
    async def vc_unlock(interaction: discord.Interaction) -> None:
        channel = _require_temp_vc(interaction)
        if not channel or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("You're not in your temp VC.", ephemeral=True)
            return
        if not _can_control_vc(interaction.user, channel):
            await interaction.response.send_message("Only the VC owner can unlock it.", ephemeral=True)
            return
        await _set_locked(channel, locked=False)
        await interaction.response.send_message("Channel **unlocked**.", ephemeral=True)

    @vc.command(name="ptt", description="Turn push-to-talk on or off for the VC you're in")
    @app_commands.describe(mode="on = force PTT, off = allow open mic")
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="on", value="on"),
            app_commands.Choice(name="off", value="off"),
        ]
    )
    async def vc_ptt(interaction: discord.Interaction, mode: app_commands.Choice[str]) -> None:
        channel = _current_voice(interaction)
        if not channel:
            await interaction.response.send_message("Join a voice channel first.", ephemeral=True)
            return
        assert isinstance(interaction.user, discord.Member)
        if not _can_control_vc(interaction.user, channel):
            await interaction.response.send_message(
                "Only the **temp VC owner** or a **Mod** can change PTT.",
                ephemeral=True,
            )
            return
        enabled = mode.value == "on"
        await _set_ptt(channel, enabled=enabled)
        if enabled:
            await interaction.response.send_message(
                f"**Push-to-talk ON** in {channel.mention}",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                f"**Push-to-talk OFF** (open mic) in {channel.mention}",
                ephemeral=True,
            )

    bot.tree.add_command(vc)
