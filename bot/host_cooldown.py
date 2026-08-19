from __future__ import annotations



import time



import discord

from discord.utils import get



from bot.config import load_config



COOLDOWN_SECONDS = 120  # 2 minutes



# user_id -> unix timestamp of last allowed host-content post

_last_post: dict[int, float] = {}



HOST_NAMES = {"💥・host-content", "host-content", "host content", "📣・host-content"}





def host_channel_names() -> set[str]:

    names = set(HOST_NAMES)

    for channels in (load_config().get("categories") or {}).values():

        for spec in channels:

            n = spec.get("name") or ""

            if "host-content" in n.lower() or n in HOST_NAMES:

                names.add(n)

                names.update(spec.get("rename_from") or [])

    return names





def is_host_content_channel(channel: discord.abc.Messageable) -> bool:

    """True only for the parent text channel — not threads under it."""

    if isinstance(channel, discord.Thread):

        return False

    if not isinstance(channel, discord.TextChannel):

        return False

    return channel.name in host_channel_names()





def _is_mod(member: discord.Member) -> bool:

    mod = get(member.guild.roles, name="Mod")

    return bool(mod and mod in member.roles) or member.guild_permissions.administrator





def content_role_names() -> set[str]:

    return {e["name"] for e in (load_config().get("content_roles") or []) if e.get("name")}





def has_content_host_role(member: discord.Member) -> bool:

    """True if member has any yaml content role (Ganking, Static, …)."""

    names = content_role_names()

    return any(r.name in names for r in member.roles)





def _remaining(user_id: int) -> float:

    last = _last_post.get(user_id)

    if last is None:

        return 0.0

    return max(0.0, COOLDOWN_SECONDS - (time.time() - last))





async def handle_host_content_message(message: discord.Message) -> bool:

    """

    #host-content parent only:

      - Mods / content-role holders may post (2 min cooldown)

      - Everyone else is deleted (signup belongs in the thread)

    Threads are ignored here.

    """

    if message.author.bot:

        return False

    if not is_host_content_channel(message.channel):

        return False

    if not isinstance(message.author, discord.Member):

        return False



    if _is_mod(message.author):

        _last_post[message.author.id] = time.time()

        return False



    if not has_content_host_role(message.author):

        try:

            await message.delete()

        except discord.HTTPException:

            pass

        try:

            await message.channel.send(

                f"{message.author.mention} only people with a **content role** "

                f"(Ganking, Static, … from `#🔔・pings-roles`) can post hosts here.\n"

                f"To **sign up**, open the host’s **thread** and type there.",

                delete_after=15,

            )

        except discord.HTTPException:

            pass

        return True



    wait = _remaining(message.author.id)

    if wait <= 0:

        _last_post[message.author.id] = time.time()

        return False



    try:

        await message.delete()

    except discord.HTTPException:

        pass



    mins = int(wait) // 60

    secs = int(wait) % 60

    left = f"{mins}m {secs}s" if mins else f"{secs}s"



    try:

        await message.channel.send(

            f"{message.author.mention} slow down — wait **{left}** before posting "

            f"another host in this channel (2 min cooldown). Threads are fine.",

            delete_after=12,

        )

    except discord.HTTPException:

        pass



    return True


