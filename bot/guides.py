from __future__ import annotations

import discord


def build_member_guide_embeds() -> list[discord.Embed]:
    """Simple guide for Members (and anyone who can see #bot-guide)."""
    e1 = discord.Embed(
        title="Bot guide — for members",
        description=(
            "Short guide to what **Gank&Chill** does for you.\n"
            "You don’t need slash commands for most of this."
        ),
        color=discord.Color.gold(),
    )
    e1.add_field(
        name="1. Ping & combat roles",
        value=(
            "Use the **join popup** or go to **#🔔・pings-roles** → click the buttons "
            "for content you want (Ganking, Static, …) and your combat role "
            "(Healer, Dps, …).\n"
            "Click again to remove. You can have several at once."
        ),
        inline=False,
    )
    e1.add_field(
        name="2. Register",
        value=(
            "Go to **#📝・register** → click **Register Albion IGN** on the "
            "**pinned bot message**.\n"
            "Enter your Albion **server** + **IGN**.\n"
            "Your nickname becomes your IGN.\n"
            "IGN is **checked against Albion** — fake names won't register.\n"
            "Stuck? Ask in **#💬・help** (works even before you register)."
        ),
        inline=False,
    )
    e1.add_field(
        name="3. Voice channels",
        value=(
            "Join **➕・Create VC** → the bot makes a personal VC for you.\n"
            "Use the buttons in the **VC chat** to rename, lock, set limit, or toggle PTT."
        ),
        inline=False,
    )
    e1.add_field(
        name="4. Hosting content",
        value=(
            "Need a **content role** from `#🔔・pings-roles` (Ganking, Static, …) "
            "to **post a host** in `#💥・host-content`.\n"
            "Post however you like — role ping + `<t:unix:R>` time + comp/roster.\n"
            "The bot adds a ⏰ and pings your role **15 min before** start.\n"
            "Open a **thread** on your post — people sign up by typing there.\n"
            "**2 min** cooldown between host posts in the channel (threads unlimited)."
        ),
        inline=False,
    )

    e2 = discord.Embed(
        title="Loot silver — your balance",
        color=discord.Color.green(),
    )
    e2.add_field(
        name="What it is",
        value=(
            "After content, mods run a **loot split**. Your cut is saved as a "
            "**ledger balance** (bookkeeping — you still get paid in-game)."
        ),
        inline=False,
    )
    e2.add_field(
        name="Commands you use",
        value=(
            "• `/balance` — show **your** silver balance (copy-paste friendly number)\n"
            "• `/balance-history` — recent credits / payouts\n\n"
            "You **cannot** see other people’s balances."
        ),
        inline=False,
    )
    e2.add_field(
        name="Getting paid",
        value=(
            "A mod pays you in-game, then marks it paid on the bot so your balance goes down.\n"
            "If something looks wrong, ping a **Mod**."
        ),
        inline=False,
    )
    e2.set_footer(text="Questions? Ask in chat or ping Mod.")
    return [e1, e2]


def build_admin_guide_embeds() -> list[discord.Embed]:
    """Mod/admin-only guide — keep practical and short."""
    e1 = discord.Embed(
        title="Staff bot guide — Mods",
        description=(
            "Only **Mod** can see this channel.\n"
            "After code changes: restart the bot, then `/setup` or `/postroles` if panels need a refresh."
        ),
        color=discord.Color.dark_magenta(),
    )
    e1.add_field(
        name="Server setup",
        value=(
            "• `/setup` — sync roles/channels from `current.yaml`, post panels, onboarding\n"
            "• `/postroles` — refresh register + pings-roles panels\n"
            "• `/reloadconfig` — reload yaml without full sync\n"
            "• `/export` — dump current layout to yaml"
        ),
        inline=False,
    )
    e1.add_field(
        name="Voice",
        value=(
            "Members join **➕・Create VC** for a temp VC.\n"
            "Owner/Mod can use VC chat buttons or `/vc panel`."
        ),
        inline=False,
    )
    e1.add_field(
        name="Host reminders",
        value=(
            "Hosts keep posting normally (role ping + `<t:unix:R>` + comp text).\n"
            "Bot auto-schedules a **15 min** ping on `<@&role>` — reacts ⏰ on the post.\n"
            "• `/remind set` — manual (message link or reply to post)\n"
            "• `/remind cancel` · `/remind list`\n"
            "Config: `host_reminders.minutes_before` in `current.yaml`."
        ),
        inline=False,
    )

    e2 = discord.Embed(
        title="Loot split — step by step",
        color=discord.Color.gold(),
    )
    e2.add_field(
        name="1. Create",
        value=(
            "Host posts the roster in **#💥・host-content** (mention the players).\n"
            "Channel has a **2 min** cooldown between posts (threads are exempt). **Mods** bypass it.\n"
            "Then either:\n"
            "• Right‑click the post → **Apps → Create Loot Split**, or\n"
            "• `/split create` with the message link\n\n"
            "You’ll name it (e.g. `Ganking` / `Static #2`). "
            "Time on the title = **when the host post was posted**.\n"
            "Sheet appears in **#📋・lootsplit**."
        ),
        inline=False,
    )
    e2.add_field(
        name="2. Edit on the sheet",
        value=(
            "• **Fill / edit money** — tab, repairs, discount (10/15), silver bags (untaxed)\n"
            "• **Add a player…** / pick + **Remove**\n"
            "• Shares: **25 / 50 / 75 / 100%**\n"
            "• **Rename** — change the label anytime while OPEN\n"
            "• **Finalize** — credits balances (asks Yes first)\n"
            "• **Cancel split** — void, no balance changes\n\n"
            "If buttons look old: `/split refresh` in that thread.\n"
            "`/split list` — open splits by name."
        ),
        inline=False,
    )
    e2.add_field(
        name="Money math",
        value=(
            "1. Tab value × (1 − discount%) → discounted tab (TabBuyer)\n"
            "2. − repairs\n"
            "3. + silver bags (not discounted)\n"
            "4. Split by share weights → credit each player\n\n"
            "Type amounts like `100m` or `1,25m`."
        ),
        inline=False,
    )

    e3 = discord.Embed(
        title="Balances & paying out",
        color=discord.Color.green(),
    )
    e3.add_field(
        name="Lookup & pay",
        value=(
            "• `/balance` — your balance\n"
            "• `/balance player:@name` — **Mod** lookup\n"
            "• `/balance-pay` — mark paid in-game:\n"
            "  – payout: **Pay entire balance**, or\n"
            "  – **Custom amount** + amount (`1,25m`)\n"
            "• `/balance-add` — **Mod**: add silver manually (`1,25m`; player optional)\n"
            "• `/balance-remove` — **Mod**: remove silver or **undo last credit**\n"
            "  – can go **negative** if they were already paid out\n"
            "• `/who-owes` — **Mod**: payout checklist (IGN + balance, copy block)\n"
            "• `/split undo` — reverse one finalized split (same negative rules)\n"
            "• `/split undo-all` — reverse **all** finalized splits (asks Yes first)\n"
            "• `/balance-history` — ledger history (Mod can look up others)"
        ),
        inline=False,
    )
    e3.add_field(
        name="Slash leftovers (optional)",
        value=(
            "`/split share` `/split add` `/split remove` `/split money` "
            "`/split finalize` `/split cancel` — same as the sheet buttons."
        ),
        inline=False,
    )
    e3.set_footer(text="Members only see #📖・bot-guide — not this channel.")
    return [e1, e2, e3]
