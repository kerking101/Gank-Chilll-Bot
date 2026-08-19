from __future__ import annotations

from typing import Any

import discord
from discord.utils import get

from bot.config import PERM_ALIASES, hex_color, load_config
from bot.guides import build_admin_guide_embeds, build_member_guide_embeds
from bot.onboarding import sync_onboarding
from bot.panels import (
    CombatRolesView,
    ContentPingsView,
    RegisterView,
    build_combat_roles_embed,
    build_content_pings_embed,
    build_register_embed,
)


def _permissions_from_list(names: list[str]) -> discord.Permissions:
    kwargs: dict[str, bool] = {}
    for name in names:
        key = PERM_ALIASES.get(name, name)
        if hasattr(discord.Permissions, key):
            kwargs[key] = True
    if not kwargs:
        return discord.Permissions.none()
    return discord.Permissions(**kwargs)


async def ensure_role(
    guild: discord.Guild,
    *,
    name: str,
    color: str,
    hoist: bool = False,
    permissions: list[str] | None = None,
    mentionable: bool = True,
) -> discord.Role | None:
    existing = get(guild.roles, name=name)
    perms = _permissions_from_list(permissions or [])
    colour = discord.Colour(hex_color(color))
    if existing:
        try:
            await existing.edit(
                colour=colour,
                hoist=hoist,
                permissions=perms,
                mentionable=mentionable,
                reason="Sync from current.yaml",
            )
        except discord.Forbidden:
            # Bot role too low / missing perms — keep existing role as-is
            print(f"  warn: cannot edit role {name} (Missing Permissions) — left unchanged")
        return existing
    try:
        return await guild.create_role(
            name=name,
            colour=colour,
            hoist=hoist,
            permissions=perms,
            mentionable=mentionable,
            reason="Sync from current.yaml",
        )
    except discord.Forbidden:
        print(f"  warn: cannot create role {name} (Missing Permissions)")
        return None


# Extra known-dead role names (also any role not in current.yaml is deleted)
OBSOLETE_ROLES = ("ZvZ", "Faction", "Dungeons")


def kept_role_names(cfg: dict[str, Any] | None = None) -> set[str]:
    cfg = cfg or load_config()
    names: set[str] = set()
    for key in ("roles", "content_roles", "combat_roles"):
        for entry in cfg.get(key) or []:
            names.add(entry["name"])
    return names


async def sync_roles(guild: discord.Guild, cfg: dict[str, Any] | None = None) -> list[str]:
    cfg = cfg or load_config()
    created: list[str] = []
    keep = kept_role_names(cfg)

    for entry in cfg.get("roles", []):
        role = await ensure_role(
            guild,
            name=entry["name"],
            color=entry.get("color", "#99aab5"),
            hoist=entry.get("hoist", False),
            permissions=entry.get("permissions"),
            mentionable=False,
        )
        if role:
            created.append(entry["name"])

    for entry in cfg.get("content_roles", []):
        role = await ensure_role(
            guild,
            name=entry["name"],
            color=entry.get("color", "#99aab5"),
            hoist=False,
            permissions=[],
            mentionable=True,
        )
        if role:
            created.append(entry["name"])

    for entry in cfg.get("combat_roles", []):
        role = await ensure_role(
            guild,
            name=entry["name"],
            color=entry.get("color", "#99aab5"),
            hoist=True,
            permissions=[],
            mentionable=True,
        )
        if role:
            created.append(entry["name"])

    # Delete anything not in config (pre-bot / leftover roles)
    for role in list(guild.roles):
        if role.is_default() or role.managed:
            continue
        if role.name in keep:
            continue
        if guild.me and role >= guild.me.top_role:
            print(f"  warn: cannot delete role {role.name} (above bot)")
            continue
        try:
            await role.delete(reason="Not in current.yaml — purged leftover role")
            print(f"  deleted leftover role: {role.name}")
        except discord.Forbidden:
            print(f"  warn: cannot delete leftover role {role.name}")
        except discord.HTTPException as e:
            print(f"  warn: delete role {role.name} failed: {e}")

    for name in OBSOLETE_ROLES:
        role = get(guild.roles, name=name)
        if role:
            try:
                await role.delete(reason="Removed from current.yaml")
            except discord.Forbidden:
                print(f"  warn: cannot delete obsolete role {name}")

    return created


async def _category_overwrites(
    guild: discord.Guild,
    cat_name: str,
    channel_specs: list[dict[str, Any]],
) -> dict[discord.abc.Snowflake, discord.PermissionOverwrite]:
    """Unregistered only sees Info; Member areas deny Unregistered/@everyone."""
    everyone = guild.default_role
    member = get(guild.roles, name="Member")
    mod = get(guild.roles, name="Mod")
    unregistered = get(guild.roles, name="Unregistered")
    overs: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {}

    is_info = "info" in cat_name.casefold()
    is_staff = "staff" in cat_name.casefold() or (
        bool(channel_specs) and all(s.get("staff_only") for s in channel_specs)
    )

    if is_info:
        overs[everyone] = discord.PermissionOverwrite(view_channel=True)
        if unregistered:
            overs[unregistered] = discord.PermissionOverwrite(view_channel=True)
        if member:
            overs[member] = discord.PermissionOverwrite(view_channel=True)
        if mod:
            overs[mod] = discord.PermissionOverwrite(view_channel=True)
        return overs

    if is_staff:
        overs[everyone] = discord.PermissionOverwrite(view_channel=False)
        if unregistered:
            overs[unregistered] = discord.PermissionOverwrite(view_channel=False)
        if member:
            overs[member] = discord.PermissionOverwrite(view_channel=False)
        if mod:
            overs[mod] = discord.PermissionOverwrite(view_channel=True)
        return overs

    # Community / Voice / Resources / etc. — Members (+ Mods) only
    overs[everyone] = discord.PermissionOverwrite(view_channel=False)
    if unregistered:
        overs[unregistered] = discord.PermissionOverwrite(view_channel=False)
    if member:
        overs[member] = discord.PermissionOverwrite(view_channel=True)
    if mod:
        overs[mod] = discord.PermissionOverwrite(view_channel=True)
    return overs


async def _channel_overwrites(
    guild: discord.Guild,
    spec: dict[str, Any],
) -> dict[discord.abc.Snowflake, discord.PermissionOverwrite]:
    """
    Access model:
      Unregistered / @everyone → Info channels (onboarding / pre_register)
      Member → normal server access
      Mod → staff channels
    """
    everyone = guild.default_role
    member = get(guild.roles, name="Member")
    mod = get(guild.roles, name="Mod")
    unregistered = get(guild.roles, name="Unregistered")

    overs: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {}

    if spec.get("staff_only"):
        overs[everyone] = discord.PermissionOverwrite(view_channel=False)
        if unregistered:
            overs[unregistered] = discord.PermissionOverwrite(view_channel=False)
        if member:
            overs[member] = discord.PermissionOverwrite(view_channel=False)
        if mod:
            overs[mod] = discord.PermissionOverwrite(view_channel=True)
        return overs

    # Guest-visible (e.g. host-content): Unregistered can read pings, Members post
    if spec.get("guest_view"):
        guest = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=False,
            read_message_history=True,
            add_reactions=False,
            create_public_threads=False,
            create_private_threads=False,
            send_messages_in_threads=False,
        )
        overs[everyone] = guest
        if unregistered:
            overs[unregistered] = guest
        if member:
            overs[member] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                add_reactions=True,
                create_public_threads=False,
                create_private_threads=False,
            )
        if mod:
            overs[mod] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                manage_messages=True,
                manage_threads=True,
                create_public_threads=True,
            )
        return overs

    # Host-content: content roles post hosts; Members reply in threads to sign up
    if spec.get("host_content"):
        cfg = load_config()
        read = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=False,
            read_message_history=True,
            add_reactions=True,
            create_public_threads=False,
            create_private_threads=False,
            send_messages_in_threads=False,
        )
        overs[everyone] = read
        if unregistered:
            overs[unregistered] = read
        # Members: no channel posts — signup happens in threads
        if member:
            overs[member] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=False,
                read_message_history=True,
                add_reactions=True,
                create_public_threads=False,
                create_private_threads=False,
                send_messages_in_threads=True,
            )
        # Anyone with a content ping role can host (post + open threads)
        host_perms = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            add_reactions=True,
            mention_everyone=False,
            create_public_threads=True,
            create_private_threads=False,
            send_messages_in_threads=True,
            embed_links=True,
            attach_files=True,
        )
        for entry in cfg.get("content_roles") or []:
            role = get(guild.roles, name=entry["name"])
            if role:
                overs[role] = host_perms
        if mod:
            overs[mod] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                manage_messages=True,
                manage_threads=True,
                create_public_threads=True,
                send_messages_in_threads=True,
                mention_everyone=True,
            )
        return overs

    # Public help chat in Info — Unregistered + Members can type
    if spec.get("guest_chat"):
        chat = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            add_reactions=True,
            attach_files=True,
            embed_links=True,
            create_public_threads=False,
            create_private_threads=False,
            send_messages_in_threads=False,
        )
        overs[everyone] = chat
        if unregistered:
            overs[unregistered] = chat
        if member:
            overs[member] = chat
        if mod:
            overs[mod] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                manage_messages=True,
                manage_threads=True,
                add_reactions=True,
            )
        return overs

    # Pre-register / join-visible Info channels — no chat for people.
    # Discord onboarding requires ≥1 channel where @everyone can send (register).
    # Member + Unregistered still get send denied so they cannot type.
    if spec.get("pre_register") or spec.get("onboarding_default"):
        everyone_send = bool(spec.get("pre_register"))
        overs[everyone] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=everyone_send,
            read_message_history=True,
            connect=False,
            add_reactions=False,
            create_public_threads=False,
            create_private_threads=False,
            send_messages_in_threads=False,
            use_external_emojis=False,
            use_external_stickers=False,
        )
        deny_chat = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=False,
            read_message_history=True,
            add_reactions=False,
            create_public_threads=False,
            create_private_threads=False,
            send_messages_in_threads=False,
            use_external_emojis=False,
            use_external_stickers=False,
        )
        if unregistered:
            overs[unregistered] = deny_chat
        if member:
            overs[member] = deny_chat
        if mod:
            overs[mod] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_messages=True,
                add_reactions=True,
            )
        return overs

    # Everything else: Members only
    overs[everyone] = discord.PermissionOverwrite(view_channel=False)
    if unregistered:
        overs[unregistered] = discord.PermissionOverwrite(view_channel=False)

    if spec.get("locked"):
        # Read-only: no posts, threads, or reactions from members
        if member:
            overs[member] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=False,
                read_message_history=True,
                add_reactions=False,
                create_public_threads=False,
                create_private_threads=False,
                send_messages_in_threads=False,
                use_external_emojis=False,
                use_external_stickers=False,
            )
        if mod:
            overs[mod] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                create_public_threads=True,
                send_messages_in_threads=True,
                add_reactions=True,
                manage_messages=True,
                manage_threads=True,
            )
    else:
        if member:
            m = discord.PermissionOverwrite(view_channel=True)
            if spec.get("type") == "voice":
                m.connect = True
                m.speak = True
                if spec.get("ptt"):
                    m.use_voice_activation = False
            elif spec.get("type") == "forum":
                # Members can open posts + reply in threads
                m.create_public_threads = True
                m.send_messages_in_threads = True
                m.attach_files = True
                m.embed_links = True
                m.add_reactions = True
            else:
                # Normal text: chat ok, but no random threads on chat channels
                m.send_messages = True
                m.add_reactions = True
                m.create_public_threads = False
                m.create_private_threads = False
            overs[member] = m
        if mod:
            overs[mod] = discord.PermissionOverwrite(
                view_channel=True,
                manage_messages=True,
                manage_threads=True,
                create_public_threads=True,
            )

    if spec.get("ptt") and spec.get("type") == "voice" and member:
        m = overs.get(member, discord.PermissionOverwrite(view_channel=True))
        m.use_voice_activation = False
        m.connect = True
        m.speak = True
        overs[member] = m

    return overs


async def sync_channels(guild: discord.Guild, cfg: dict[str, Any] | None = None) -> list[str]:
    cfg = cfg or load_config()
    synced: list[str] = []

    for cat_index, (cat_name, channels) in enumerate((cfg.get("categories") or {}).items()):
        category = _find_category(guild, cat_name)
        cat_overs = await _category_overwrites(guild, cat_name, channels)
        if not category:
            category = await guild.create_category(
                cat_name,
                position=cat_index,
                overwrites=cat_overs,
                reason="Sync from current.yaml",
            )
        else:
            await category.edit(
                name=cat_name,
                position=cat_index,
                overwrites=cat_overs,
                reason="Sync from current.yaml",
            )

        for position, spec in enumerate(channels):
            name = spec["name"]
            ctype = spec.get("type", "text")
            overs = await _channel_overwrites(guild, spec)
            existing = _find_channel(category, name, ctype, spec.get("rename_from") or [])

            if ctype == "voice":
                ch = await _sync_voice(category, existing, name, overs, position)
            elif ctype == "forum":
                ch = await _sync_forum(category, existing, name, overs, position)
            else:
                ch = await _sync_text(category, existing, name, overs, position)
            synced.append(f"{cat_name}/{ch.name}")

    await _delete_obsolete_channels(guild, cfg)
    await _lock_orphan_channels(guild, cfg)
    return synced


async def force_reregister(guild: discord.Guild) -> str:
    """
    Strip everyone back to Unregistered (keep Mod), clear IGN nicknames,
    delete leftover roles, so members must register again.
    """
    from bot.ign import clear_registration

    print(f"  fetching members for {guild.name}…")
    humans: list[discord.Member] = []
    async for member in guild.fetch_members(limit=None):
        if not member.bot:
            humans.append(member)
    unregistered = get(guild.roles, name="Unregistered")
    mod_role = get(guild.roles, name="Mod")
    if not unregistered:
        return "Missing **Unregistered** role — run `/setup` first."

    print(f"  resetting {len(humans)} members…")
    reset = 0
    cleared_ign = 0
    failed = 0

    for i, member in enumerate(humans, 1):
        keep_roles: list[discord.Role] = [unregistered]
        if mod_role and mod_role in member.roles:
            keep_roles.append(mod_role)

        try:
            # One API call: replace roles + clear nick
            kwargs: dict[str, Any] = {"roles": keep_roles, "reason": "Force re-register"}
            if member.nick:
                kwargs["nick"] = None
            await member.edit(**kwargs)
            if clear_registration(member.id):
                cleared_ign += 1
            reset += 1
        except discord.Forbidden:
            failed += 1
            print(f"  warn: cannot reset {member} (Forbidden)")
        except discord.HTTPException as e:
            failed += 1
            print(f"  warn: reset {member} failed: {e}")
        if i % 25 == 0 or i == len(humans):
            print(f"  … {i}/{len(humans)}")

    print("  syncing roles (purge leftovers)…")
    await sync_roles(guild)
    print("  syncing channels (Info-only perms)…")
    await sync_channels(guild)

    return (
        f"Reset **{reset}** member(s) to Unregistered"
        f" (cleared **{cleared_ign}** IGN record(s)"
        f"{f', **{failed}** failed' if failed else ''}).\n"
        f"Leftover roles purged; Info-only perms reapplied.\n"
        f"Everyone must register again in `#📝・register`."
    )


async def _sync_voice(
    category: discord.CategoryChannel,
    existing: discord.abc.GuildChannel | None,
    name: str,
    overs: dict,
    position: int,
) -> discord.abc.GuildChannel:
    if existing and isinstance(existing, discord.VoiceChannel):
        await existing.edit(
            name=name,
            category=category,
            overwrites=overs,
            position=position,
            reason="Sync from current.yaml",
        )
        return existing
    if existing:
        await existing.delete(reason="Recreate as voice channel")
    return await category.create_voice_channel(
        name,
        overwrites=overs,
        position=position,
        reason="Sync from current.yaml",
    )


async def _sync_text(
    category: discord.CategoryChannel,
    existing: discord.abc.GuildChannel | None,
    name: str,
    overs: dict,
    position: int,
) -> discord.abc.GuildChannel:
    if existing and isinstance(existing, discord.TextChannel) and not isinstance(existing, discord.ForumChannel):
        # NewsChannel subclasses TextChannel — leave as-is if already news
        await existing.edit(
            name=name,
            category=category,
            overwrites=overs,
            position=position,
            reason="Sync from current.yaml",
        )
        return existing
    if existing and isinstance(existing, discord.ForumChannel):
        await existing.delete(reason="Recreate as text channel")
        existing = None
    elif existing and not isinstance(existing, discord.TextChannel):
        await existing.delete(reason="Recreate as text channel")
        existing = None
    if existing:
        return existing
    return await category.create_text_channel(
        name,
        overwrites=overs,
        position=position,
        reason="Sync from current.yaml",
    )


async def _sync_forum(
    category: discord.CategoryChannel,
    existing: discord.abc.GuildChannel | None,
    name: str,
    overs: dict,
    position: int,
) -> discord.abc.GuildChannel:
    if existing and isinstance(existing, discord.ForumChannel):
        await existing.edit(
            name=name,
            category=category,
            overwrites=overs,
            position=position,
            reason="Sync from current.yaml",
        )
        return existing

    # Convert text → forum when possible; otherwise recreate
    if existing and isinstance(existing, discord.TextChannel):
        try:
            converted = await existing.edit(
                name=name,
                category=category,
                overwrites=overs,
                position=position,
                type=discord.ChannelType.forum,
                reason="Convert to forum",
            )
            if isinstance(converted, discord.ForumChannel):
                return converted
        except discord.HTTPException:
            await existing.delete(reason="Recreate as forum channel")
            existing = None
    elif existing:
        await existing.delete(reason="Recreate as forum channel")
        existing = None

    return await category.create_forum(
        name,
        overwrites=overs,
        position=position,
        reason="Sync from current.yaml",
    )


async def _delete_obsolete_channels(guild: discord.Guild, cfg: dict[str, Any]) -> None:
    names = set(cfg.get("obsolete_channels") or [])
    if not names:
        return
    for ch in list(guild.channels):
        if isinstance(ch, discord.CategoryChannel):
            continue
        if ch.name in names:
            try:
                await ch.delete(reason="Removed from current.yaml")
            except discord.HTTPException:
                pass


async def _lock_orphan_channels(guild: discord.Guild, cfg: dict[str, Any]) -> None:
    """Hide any channel not managed by current.yaml from non-members."""
    managed_cats: set[str] = set()
    for cat_name in (cfg.get("categories") or {}):
        managed_cats.add(cat_name)
        managed_cats.add(cat_name.split()[-1])  # "🔊 Voice" → "Voice"

    managed_names: set[str] = set()
    for channels in (cfg.get("categories") or {}).values():
        for spec in channels:
            managed_names.add(spec["name"])
            managed_names.update(spec.get("rename_from") or [])
            # bare name after emoji separator
            if "・" in spec["name"]:
                managed_names.add(spec["name"].split("・", 1)[-1])

    everyone = guild.default_role
    member = get(guild.roles, name="Member")
    unregistered = get(guild.roles, name="Unregistered")

    for ch in list(guild.channels):
        if isinstance(ch, discord.CategoryChannel):
            continue
        cat = ch.category
        in_managed = cat is not None and cat.name in managed_cats and ch.name in managed_names
        if in_managed:
            continue
        # Orphan (old Discord defaults, etc.) — hide or delete empty leftovers
        try:
            if ch.name.lower() in {"general", "logs"} and (cat is None or cat.name not in managed_cats):
                await ch.delete(reason="Orphan channel outside bot layout")
                continue
        except discord.HTTPException:
            pass

        overs = {
            everyone: discord.PermissionOverwrite(view_channel=False),
        }
        if unregistered:
            overs[unregistered] = discord.PermissionOverwrite(view_channel=False)
        if member:
            overs[member] = discord.PermissionOverwrite(view_channel=True)
        try:
            await ch.edit(overwrites=overs, reason="Lock orphan channel")
        except discord.HTTPException:
            pass


def _find_category(guild: discord.Guild, name: str) -> discord.CategoryChannel | None:
    found = get(guild.categories, name=name)
    if found:
        return found
    # "🔊 Voice" → also match old "Voice"
    bare = name.split()[-1] if " " in name else name
    found = get(guild.categories, name=bare)
    if found:
        return found
    # legacy renames
    aliases = {
        "Resources": ["Content", "⚔️ Content", "Builds", "🛠️ Builds"],
        "Builds": ["Content", "⚔️ Content"],
    }
    for old in aliases.get(bare, []):
        found = get(guild.categories, name=old)
        if found:
            return found
        found = discord.utils.find(lambda c: old in c.name or c.name.endswith(old.split()[-1]), guild.categories)
        if found:
            return found
    for cat in guild.categories:
        if cat.name.endswith(bare) or bare in cat.name:
            return cat
    return None


def _find_channel(
    category: discord.CategoryChannel,
    name: str,
    ctype: str,
    rename_from: list[str],
) -> discord.abc.GuildChannel | None:
    guild = category.guild
    names = [name, *rename_from]

    if ctype == "voice":
        pools = [category.voice_channels, guild.voice_channels]
    elif ctype == "forum":
        forums = list(guild.forums)
        pools = [
            forums,
            list(category.text_channels),
            list(guild.text_channels),
        ]
    else:
        pools = [category.text_channels, guild.text_channels]

    for pool in pools:
        for n in names:
            found = get(pool, name=n)
            if found:
                return found
    return None


def _channel_by_panel(guild: discord.Guild, panel: str) -> discord.TextChannel | None:
    cfg = load_config()
    for channels in (cfg.get("categories") or {}).values():
        for spec in channels:
            if spec.get("panel") != panel:
                continue
            names = [spec["name"], *(spec.get("rename_from") or [])]
            for n in names:
                ch = get(guild.text_channels, name=n)
                if ch:
                    return ch
    return None


async def _clear_bot_embeds(channel: discord.TextChannel) -> None:
    async for msg in channel.history(limit=40):
        if msg.author == channel.guild.me and msg.embeds:
            try:
                await msg.delete()
            except discord.HTTPException:
                pass


async def _pin_panel(message: discord.Message) -> None:
    try:
        await message.pin()
    except discord.HTTPException:
        pass


async def _panel_exists(channel: discord.TextChannel) -> bool:
    async for msg in channel.history(limit=15):
        if msg.author == channel.guild.me and msg.embeds and msg.components:
            return True
    return False


async def ensure_register_panel(guild: discord.Guild) -> bool:
    """Re-post register panel if the button message is missing."""
    register = _channel_by_panel(guild, "register")
    if not register:
        return False
    if await _panel_exists(register):
        return False
    msg = await register.send(embed=build_register_embed(), view=RegisterView())
    await _pin_panel(msg)
    return True


async def post_panels(guild: discord.Guild) -> list[str]:
    posted: list[str] = []

    roles_ch = _channel_by_panel(guild, "roles")
    if roles_ch:
        await _clear_bot_embeds(roles_ch)
        msg1 = await roles_ch.send(embed=build_content_pings_embed(), view=ContentPingsView())
        msg2 = await roles_ch.send(embed=build_combat_roles_embed(), view=CombatRolesView())
        await _pin_panel(msg1)
        await _pin_panel(msg2)
        posted.append(roles_ch.name)

    register = _channel_by_panel(guild, "register")
    if register:
        await _clear_bot_embeds(register)
        msg = await register.send(embed=build_register_embed(), view=RegisterView())
        await _pin_panel(msg)
        posted.append(register.name)
        # Don't use register as system channel — join messages bury the Register button
        try:
            await guild.edit(system_channel=None, reason="Keep register panel visible (no join spam)")
        except discord.HTTPException:
            pass

    member_guide = _channel_by_panel(guild, "guide_member")
    if member_guide:
        await _clear_bot_embeds(member_guide)
        await member_guide.send(embeds=build_member_guide_embeds())
        posted.append(member_guide.name)
    else:
        posted.append("(missing 📖・bot-guide — run /setup)")

    admin_guide = _channel_by_panel(guild, "guide_admin")
    if admin_guide:
        await _clear_bot_embeds(admin_guide)
        await admin_guide.send(embeds=build_admin_guide_embeds())
        posted.append(admin_guide.name)
    else:
        posted.append("(missing 📖・staff-guide — run /setup)")

    return posted


async def full_setup(guild: discord.Guild) -> str:
    roles = await sync_roles(guild)
    channels = await sync_channels(guild)
    # Onboarding after channels exist (needs default channels + @everyone access)
    onboard = await sync_onboarding(guild)
    panels = await post_panels(guild)
    from bot.temp_voice import post_static_vc_panels

    vc_panels = await post_static_vc_panels(guild)
    panel_txt = ", ".join(f"#{p}" for p in panels) if panels else "none"
    vc_txt = ", ".join(vc_panels) if vc_panels else "none"
    return (
        f"Synced **{len(roles)}** roles, **{len(channels)}** channels.\n"
        f"Panels -> {panel_txt}\n"
        f"VC chat controls -> {vc_txt}\n"
        f"{onboard}"
    )
