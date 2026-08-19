from __future__ import annotations

import os

import discord
import yaml
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from bot.config import load_config
from bot.host_cooldown import handle_host_content_message
from bot.host_reminders import maybe_schedule_host_reminder, register_host_reminders
from bot.panels import CombatRolesView, ContentPingsView, RegisterView
from bot.split import register_split
from bot.sync import force_reregister, full_setup
from bot.temp_voice import VCChatPanelView, handle_voice_state, register_temp_voice

load_dotenv()

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)
register_temp_voice(bot)
register_split(bot)
register_host_reminders(bot)


@bot.event
async def on_ready() -> None:
    # Persistent panels keep working after restart
    bot.add_view(RegisterView())
    bot.add_view(ContentPingsView())
    bot.add_view(CombatRolesView())
    bot.add_view(VCChatPanelView())

    synced = await bot.tree.sync()
    print(f"Logged in as {bot.user} ({bot.user.id})")
    print(f"Synced {len(synced)} slash command(s)")
    for g in bot.guilds:
        print(f"  guild: {g.name} — {g.id}")


@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
) -> None:
    await handle_voice_state(member, before, after)


@bot.event
async def on_message(message: discord.Message) -> None:
    blocked = await handle_host_content_message(message)
    if not blocked:
        await maybe_schedule_host_reminder(message)
    await bot.process_commands(message)


@bot.event
async def on_member_join(member: discord.Member) -> None:
    """New joins start as Unregistered until they register Albion IGN."""
    role = discord.utils.get(member.guild.roles, name="Unregistered")
    if role:
        await member.add_roles(role, reason="New member — awaiting Albion registration")

    from bot.sync import ensure_register_panel

    await ensure_register_panel(member.guild)

    pings = discord.utils.get(member.guild.text_channels, name="🔔・pings-roles")
    if not pings:
        pings = discord.utils.find(
            lambda c: isinstance(c, discord.TextChannel) and "pings-roles" in c.name,
            member.guild.text_channels,
        )
    register = discord.utils.get(member.guild.text_channels, name="📝・register")
    if not register:
        register = discord.utils.find(
            lambda c: isinstance(c, discord.TextChannel) and "register" in c.name,
            member.guild.text_channels,
        )
    if pings or register:
        steps = []
        if pings:
            steps.append(f"1. Pick pings in {pings.mention} (or use the join popup)")
        if register:
            steps.append(
                f"{'2' if pings else '1'}. Register in {register.mention} — "
                f"click **Register Albion IGN** on the pinned bot message"
            )
        try:
            await member.send(
                f"Welcome to **{member.guild.name}**!\n"
                + "\n".join(steps)
                + "\n\nYou get **Member** after IGN register — that unlocks the server."
            )
        except discord.Forbidden:
            pass


@bot.tree.command(name="ping", description="Check if the bot is alive")
@app_commands.default_permissions(administrator=True)
async def ping(interaction: discord.Interaction) -> None:
    await interaction.response.send_message("pong", ephemeral=True)


@bot.tree.command(
    name="setup",
    description="Sync roles/channels from current.yaml and post the role panel",
)
@app_commands.default_permissions(administrator=True)
async def setup(interaction: discord.Interaction) -> None:
    if not interaction.guild:
        await interaction.response.send_message("Guild only.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    summary = await full_setup(interaction.guild)
    await interaction.followup.send(summary, ephemeral=True)


@bot.tree.command(
    name="force-reregister",
    description="Strip old roles, clear IGNs, put everyone on Unregistered (keep Mod)",
)
@app_commands.default_permissions(administrator=True)
async def force_reregister_cmd(interaction: discord.Interaction) -> None:
    if not interaction.guild:
        await interaction.response.send_message("Guild only.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    summary = await force_reregister(interaction.guild)
    await interaction.followup.send(summary, ephemeral=True)


@bot.tree.command(
    name="postroles",
    description="Refresh register, pings-roles, and bot guide panels",
)
@app_commands.default_permissions(administrator=True)
async def postroles(interaction: discord.Interaction) -> None:
    if not interaction.guild:
        await interaction.response.send_message("Guild only.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    from bot.sync import post_panels

    panels = await post_panels(interaction.guild)
    if panels:
        await interaction.followup.send(
            "Panels ready: " + ", ".join(f"#{p}" for p in panels),
            ephemeral=True,
        )
    else:
        await interaction.followup.send("No panel channels found — run `/setup` first.", ephemeral=True)


@bot.tree.command(name="reloadconfig", description="Reload current.yaml from disk")
@app_commands.default_permissions(administrator=True)
async def reloadconfig(interaction: discord.Interaction) -> None:
    cfg = load_config()
    n_content = len(cfg.get("content_roles", []))
    n_combat = len(cfg.get("combat_roles", []))
    await interaction.response.send_message(
        f"Config loaded — {n_content} content roles, {n_combat} combat roles.\n"
        f"Run `/setup` to apply role/channel changes to the server.",
        ephemeral=True,
    )


@bot.tree.command(name="export", description="Dump current server layout to current.yaml")
@app_commands.default_permissions(administrator=True)
async def export(interaction: discord.Interaction) -> None:
    g = interaction.guild
    if not g:
        await interaction.response.send_message("Guild only.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    existing = load_config()
    data = {
        "roles": [],
        "content_roles": existing.get("content_roles", []),
        "combat_roles": existing.get("combat_roles", []),
        "categories": {},
    }

    for r in sorted(g.roles, key=lambda r: r.position, reverse=True):
        if r.is_default() or r.managed:
            continue
        # Keep specialty role lists separate — skip dumping them into base roles
        specialty = {x["name"] for x in data["content_roles"]} | {
            x["name"] for x in data["combat_roles"]
        }
        if r.name in specialty:
            continue
        data["roles"].append(
            {
                "name": r.name,
                "color": str(r.color),
                "hoist": r.hoist,
                "permissions": [p for p, v in r.permissions if v],
            }
        )

    for cat, channels in g.by_category():
        entries = []
        for c in channels:
            entry: dict = {"name": c.name, "type": "voice" if isinstance(c, discord.VoiceChannel) else "text"}
            entries.append(entry)
        if cat:
            data["categories"][cat.name] = entries

    with open("current.yaml", "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)

    await interaction.followup.send("Dumped layout to `current.yaml` (kept content/combat role lists).", ephemeral=True)


bot.run(os.getenv("TOKEN"))
