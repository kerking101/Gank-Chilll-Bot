from __future__ import annotations

import discord
from discord.enums import OnboardingMode, OnboardingPromptType
from discord.onboarding import OnboardingPrompt, OnboardingPromptOption
from discord.utils import get

from bot.config import load_config


async def ensure_community(guild: discord.Guild) -> str:
    """Community mode is required for the join customization popup."""
    rules = (
        get(guild.text_channels, name="📜・rules")
        or get(guild.text_channels, name="rules")
    )
    updates = (
        get(guild.text_channels, name="📡・discord-updates")
        or get(guild.text_channels, name="📢・announcements")
        or get(guild.text_channels, name="announcements")
    )
    if not rules or not updates:
        return "skipped (need #rules + updates channel)"

    if "COMMUNITY" in guild.features:
        return "already enabled"

    await guild.edit(
        community=True,
        rules_channel=rules,
        public_updates_channel=updates,
        verification_level=discord.VerificationLevel.medium,
        explicit_content_filter=discord.ContentFilter.all_members,
        reason="Enable Community for onboarding",
    )
    return "enabled"


def _default_channels(guild: discord.Guild) -> list[discord.abc.GuildChannel]:
    """Register-first list of onboarding default channels from yaml."""
    cfg = load_config()
    first: list[str] = []
    rest: list[str] = []
    for channels in (cfg.get("categories") or {}).values():
        for spec in channels:
            if not spec.get("onboarding_default"):
                continue
            if spec.get("onboarding_first"):
                first.append(spec["name"])
            else:
                rest.append(spec["name"])

    found: list[discord.abc.GuildChannel] = []
    for name in first + rest:
        ch = get(guild.text_channels, name=name) or get(guild.voice_channels, name=name)
        if ch and ch not in found:
            found.append(ch)
    return found


def _prompt_from_roles(
    *,
    title: str,
    entries: list[dict],
    guild: discord.Guild,
) -> OnboardingPrompt | None:
    options: list[OnboardingPromptOption] = []
    for entry in entries:
        role = get(guild.roles, name=entry["name"])
        if not role:
            continue
        options.append(
            OnboardingPromptOption(
                title=entry["name"],
                emoji=entry.get("emoji"),
                description=f"Get the {entry['name']} role",
                roles=[role],
            )
        )
    if not options:
        return None
    return OnboardingPrompt(
        type=OnboardingPromptType.multiple_choice,
        title=title,
        options=options,
        single_select=False,
        required=True,
        in_onboarding=True,
    )


async def sync_onboarding(guild: discord.Guild) -> str:
    """Re-enable Discord join popup for content + combat role picks."""
    community = await ensure_community(guild)
    cfg = load_config()

    prompts: list[OnboardingPrompt] = []
    content = _prompt_from_roles(
        title="What content are you interested in?",
        entries=cfg.get("content_roles", []),
        guild=guild,
    )
    combat = _prompt_from_roles(
        title="What roles can you play?",
        entries=cfg.get("combat_roles", []),
        guild=guild,
    )
    if content:
        prompts.append(content)
    if combat:
        prompts.append(combat)

    defaults = _default_channels(guild)
    if len(defaults) < 5:
        return (
            f"Community: {community}. Onboarding not enabled — "
            f"need at least 5 default channels (have {len(defaults)})."
        )

    try:
        await guild.edit_onboarding(
            prompts=prompts,
            default_channels=defaults,
            enabled=True,
            mode=OnboardingMode.default,
            reason="Restore join role customization popup",
        )
    except discord.HTTPException as e:
        return f"Community: {community}. Onboarding failed: {e}"

    first = defaults[0].name if defaults else "?"
    return (
        f"Community: {community}. Onboarding ON — "
        f"{len(prompts)} questions, lands near #{first}. "
        f"Pings first, then Albion IGN in #📝・register for Member."
    )
