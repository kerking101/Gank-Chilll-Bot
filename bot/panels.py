from __future__ import annotations

import discord
from discord.utils import get

from bot.albion_api import verify_ign
from bot.config import load_config
from bot.ign import format_nick, get_registration, has_ign, set_registration


def _find_role(guild: discord.Guild, name: str) -> discord.Role | None:
    return get(guild.roles, name=name)


def _find_register_channel(guild: discord.Guild) -> discord.TextChannel | None:
    ch = get(guild.text_channels, name="📝・register")
    if ch:
        return ch
    return discord.utils.find(
        lambda c: isinstance(c, discord.TextChannel) and "register" in c.name,
        guild.text_channels,
    )


def _specialty_role_names(cfg: dict | None = None) -> tuple[set[str], set[str]]:
    cfg = cfg or load_config()
    content = {str(r["name"]) for r in cfg.get("content_roles", [])}
    combat = {str(r["name"]) for r in cfg.get("combat_roles", [])}
    return content, combat


def _has_any_specialty_role(member: discord.Member) -> bool:
    content, combat = _specialty_role_names()
    names = content | combat
    return any(_find_role(member.guild, n) in member.roles for n in names)


def _register_nudge(member: discord.Member) -> str:
    if has_ign(member.id):
        return ""
    reg = _find_register_channel(member.guild)
    if not reg:
        return ""
    return (
        f"\n\n**Next:** go to {reg.mention} and click **Register Albion IGN** "
        f"to get **Member** and unlock the server."
    )


async def ensure_member(member: discord.Member) -> bool:
    """Promote Unregistered → Member once Albion server + IGN are set."""
    if not has_ign(member.id):
        return False
    unregistered = _find_role(member.guild, "Unregistered")
    member_role = _find_role(member.guild, "Member")
    if member_role and member_role not in member.roles:
        await member.add_roles(member_role, reason="Completed registration")
    if unregistered and unregistered in member.roles:
        await member.remove_roles(unregistered, reason="Completed registration")
    return True


class RoleToggleButton(discord.ui.Button):
    """Click to add/remove a role — supports any number of roles at once."""

    def __init__(self, *, role_name: str, emoji: str | None, row: int) -> None:
        super().__init__(
            label=role_name,
            emoji=emoji,
            style=discord.ButtonStyle.secondary,
            custom_id=f"kerp:toggle:{role_name}",
            row=row,
        )
        self.role_name = role_name

    async def callback(self, interaction: discord.Interaction) -> None:
        assert interaction.guild and isinstance(interaction.user, discord.Member)
        member = interaction.user
        role = _find_role(interaction.guild, self.role_name)
        if not role:
            await interaction.response.send_message(
                f"Role **{self.role_name}** is missing — ask a mod to run `/setup`.",
                ephemeral=True,
            )
            return

        try:
            if role in member.roles:
                await member.remove_roles(role, reason="Role toggle panel")
                await interaction.response.send_message(
                    f"Removed **{role.name}**",
                    ephemeral=True,
                )
            else:
                await member.add_roles(role, reason="Role toggle panel")
                fresh = interaction.guild.get_member(member.id) or member
                msg = f"Added **{role.name}** — pick more if you want."
                if _has_any_specialty_role(fresh):
                    msg += _register_nudge(fresh)
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message(
                "I can't manage that role — move my bot role higher in Server Settings → Roles.",
                ephemeral=True,
            )


class IgnModal(discord.ui.Modal, title="Albion Registration"):
    server = discord.ui.TextInput(
        label="Server",
        placeholder="EU, Americas, or Asia",
        min_length=2,
        max_length=16,
        required=True,
    )
    ign = discord.ui.TextInput(
        label="In-game name (IGN)",
        placeholder="e.g. kerpower",
        min_length=1,
        max_length=32,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        assert interaction.guild and isinstance(interaction.user, discord.Member)
        member = interaction.user
        server = str(self.server).strip()
        name = str(self.ign).strip()

        servers = load_config().get("albion_servers", ["EU", "Americas", "Asia"])
        matched = next((s for s in servers if s.lower() == server.lower()), None)
        if not matched:
            await interaction.response.send_message(
                f"Unknown server **{server}**. Use one of: {', '.join(servers)}",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        check = await verify_ign(name, matched)
        if not check.ok:
            await interaction.followup.send(check.message, ephemeral=True)
            return

        name = check.canonical_name or name
        set_registration(
            member.id,
            ign=name,
            server=matched,
            albion_id=check.player_id,
        )
        nick = format_nick(name, matched)

        nick_note = ""
        try:
            await member.edit(nick=nick, reason="Albion IGN registration")
            nick_note = f" Nickname set to **{nick}**."
        except discord.Forbidden:
            nick_note = " (Couldn't change nickname — move the bot role higher.)"

        await ensure_member(member)
        await interaction.followup.send(
            f"Registered **{name}** on **{matched}** (verified in-game).{nick_note}\n"
            f"You now have **Member** — the rest of the server is unlocked.",
            ephemeral=True,
        )


class IgnButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            label="Register Albion IGN",
            style=discord.ButtonStyle.success,
            custom_id="kerp:set_ign",
            emoji="📝",
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        modal = IgnModal()
        current = get_registration(interaction.user.id)
        if current:
            if current.get("server"):
                modal.server.default = current["server"]
            if current.get("ign"):
                modal.ign.default = current["ign"]
        await interaction.response.send_modal(modal)


class RegisterView(discord.ui.View):
    """IGN registration only — posted in #📝・register."""

    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(IgnButton())


class ContentPingsView(discord.ui.View):
    """Content ping toggles — own message in #🔔・pings-roles."""

    def __init__(self) -> None:
        super().__init__(timeout=None)
        for i, entry in enumerate(load_config().get("content_roles", [])):
            self.add_item(
                RoleToggleButton(
                    role_name=entry["name"],
                    emoji=entry.get("emoji"),
                    row=i // 5,
                )
            )


class CombatRolesView(discord.ui.View):
    """Combat role toggles — separate message in the same channel."""

    def __init__(self) -> None:
        super().__init__(timeout=None)
        for i, entry in enumerate(load_config().get("combat_roles", [])):
            self.add_item(
                RoleToggleButton(
                    role_name=entry["name"],
                    emoji=entry.get("emoji"),
                    row=i // 5,
                )
            )


# Back-compat alias
class RolePanelView(ContentPingsView):
    pass


def build_register_embed() -> discord.Embed:
    servers = load_config().get("albion_servers", ["EU", "Americas", "Asia"])
    return discord.Embed(
        title="📝 Register your Albion character",
        description=(
            "**Step 2** — after you picked pings in **#🔔・pings-roles** "
            "(or the join popup).\n\n"
            "Click the green **Register Albion IGN** button on **this message** below.\n\n"
            "Enter:\n"
            f"• **Server** — {', '.join(servers)}\n"
            "• **IGN** — your exact in-game name (checked against Albion)\n\n"
            "You get **Member** and unlock the rest of the server.\n"
            "Nickname example: `kerpower`"
        ),
        color=0x2ECC71,
    )


def build_content_pings_embed() -> discord.Embed:
    lines = [
        f"{r.get('emoji', '•')} **{r['name']}**"
        for r in load_config().get("content_roles", [])
    ]
    return discord.Embed(
        title="🔔 Content pings",
        description=(
            "**Step 1** — pick what you want pinged for.\n"
            "Click to **get pinged** for that content.\n"
            "Take as many as you want — click again to remove.\n\n"
            "Then go to **#📝・register** for your Albion IGN (Member unlock)."
        ),
        color=0x3498DB,
    ).add_field(name="Options", value="\n".join(lines) or "—", inline=False)


def build_combat_roles_embed() -> discord.Embed:
    lines = [
        f"{r.get('emoji', '•')} **{r['name']}**"
        for r in load_config().get("combat_roles", [])
    ]
    return discord.Embed(
        title="⚔️ Combat roles",
        description=(
            "Click what you play.\n"
            "Take as many as you want — click again to remove."
        ),
        color=0xE67E22,
    ).add_field(name="Options", value="\n".join(lines) or "—", inline=False)
