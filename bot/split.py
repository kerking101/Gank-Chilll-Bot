from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands
from discord.utils import get

from bot.ign import get_ign

SPLITS_PATH = Path(__file__).resolve().parent.parent / "data" / "splits.json"
BALANCES_PATH = Path(__file__).resolve().parent.parent / "data" / "balances.json"
_SPLIT_LOCKS: dict[str, asyncio.Lock] = {}

USER_MENTION_RE = re.compile(r"<@!?(\d+)>")
MESSAGE_LINK_RE = re.compile(
    r"(?:https?://)?(?:(?:ptb|canary)\.)?discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)"
)

LOOTSPLIT_NAMES = {"📋・lootsplit", "lootsplit"}
HISTORY_LIMIT = 25


# ── storage ──────────────────────────────────────────────────────────────────


def _load_splits() -> dict[str, Any]:
    if not SPLITS_PATH.exists():
        return {"splits": {}}
    # utf-8-sig strips a BOM if Windows/PowerShell wrote the file
    with open(SPLITS_PATH, encoding="utf-8-sig") as f:
        data = json.load(f)
    if "splits" not in data:
        data = {"splits": data}
    return data


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
    tmp.replace(path)


def _save_splits(data: dict[str, Any]) -> None:
    _atomic_write_json(SPLITS_PATH, data)


def _load_balances() -> dict[str, Any]:
    if not BALANCES_PATH.exists():
        return {}
    with open(BALANCES_PATH, encoding="utf-8-sig") as f:
        return json.load(f)


def _save_balances(data: dict[str, Any]) -> None:
    _atomic_write_json(BALANCES_PATH, data)


def _lock_for(split_id: str) -> asyncio.Lock:
    lock = _SPLIT_LOCKS.get(split_id)
    if lock is None:
        lock = asyncio.Lock()
        _SPLIT_LOCKS[split_id] = lock
    return lock


def _unique_players(players: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """One row per user (last share wins) so nobody is paid twice on the sheet."""
    by_id: dict[int, dict[str, Any]] = {}
    order: list[int] = []
    for p in players or []:
        try:
            uid = int(p["user_id"])
        except (KeyError, TypeError, ValueError):
            continue
        if uid not in by_id:
            order.append(uid)
        by_id[uid] = {"user_id": uid, "share": float(p.get("share", 100))}
    return [by_id[uid] for uid in order]


def get_split(split_id: str) -> dict[str, Any] | None:
    return _load_splits()["splits"].get(split_id)


def save_split(split: dict[str, Any]) -> None:
    data = _load_splits()
    split["players"] = _unique_players(list(split.get("players") or []))
    split["rev"] = int(split.get("rev") or 0) + 1
    data["splits"][split["id"]] = split
    _save_splits(data)


def find_open_split_by_thread(thread_id: int) -> dict[str, Any] | None:
    for s in _load_splits()["splits"].values():
        if s.get("forum_thread_id") == thread_id and s.get("status") == "OPEN":
            return s
    return None


def find_split_by_thread(thread_id: int) -> dict[str, Any] | None:
    for s in _load_splits()["splits"].values():
        if s.get("forum_thread_id") == thread_id:
            return s
    return None


# ── money math ───────────────────────────────────────────────────────────────


def parse_silver(raw: str | float | int | None) -> int | None:
    """Parse silver amounts.

    Accepts: 1250000, 1.25m, 1,25m (EU decimal), 1,250,000, 1.250.000, 8100k, 1.5b
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return int(raw)
    text = str(raw).strip().lower().replace(" ", "").replace("_", "")
    if not text:
        return None

    mult = 1
    if text.endswith("b"):
        mult = 1_000_000_000
        text = text[:-1]
    elif text.endswith("m"):
        mult = 1_000_000
        text = text[:-1]
    elif text.endswith("k"):
        mult = 1_000
        text = text[:-1]

    if not text:
        return None

    if mult > 1:
        # Short form like 1,25m / 1.25m — normalize decimal separator
        if "," in text and "." in text:
            # 1.250,5m (EU) or 1,250.5m (US): last separator is decimal
            if text.rfind(",") > text.rfind("."):
                text = text.replace(".", "").replace(",", ".")
            else:
                text = text.replace(",", "")
        elif "," in text:
            left, _, right = text.partition(",")
            # 1,25 → decimal; 1,250 with 3 digits could be thousands OR 1.250 decimal
            # Prefer EU decimal when using m/k/b (how people type quickly)
            if len(right) <= 3 and left.isdigit() and right.isdigit():
                text = f"{left}.{right}"
            else:
                text = text.replace(",", "")
        # else: plain 1.25 or 1250 — fine for float()
    else:
        # Full silver number: strip thousand separators
        if "," in text and "." in text:
            if text.rfind(",") > text.rfind("."):
                # 1.250.000,5 → unlikely for int silver; treat EU
                text = text.replace(".", "").replace(",", ".")
            else:
                # 1,250,000.5
                text = text.replace(",", "")
        elif "," in text:
            parts = text.split(",")
            if len(parts) > 1 and all(len(p) == 3 for p in parts[1:]) and parts[0].isdigit():
                text = "".join(parts)  # 1,250,000
            elif len(parts) == 2 and len(parts[1]) <= 2 and parts[0].isdigit() and parts[1].isdigit():
                text = f"{parts[0]}.{parts[1]}"  # 1,25 silver (rare)
            else:
                text = text.replace(",", "")
        elif "." in text:
            parts = text.split(".")
            if len(parts) > 1 and all(len(p) == 3 for p in parts[1:]) and parts[0].isdigit():
                text = "".join(parts)  # 1.250.000 EU thousands
            # else leave as decimal float (e.g. 1.5) → int truncates

    try:
        return int(float(text) * mult)
    except ValueError:
        return None


def format_silver(amount: int) -> str:
    """Trade-friendly full silver: 1,250,000 (commas, no decimal dots)."""
    n = int(amount)
    sign = "-" if n < 0 else ""
    return f"{sign}{abs(n):,}"


def format_silver_short(amount: int) -> str:
    """Compact label for big tab fields (1.25m). Not for copy-paste into trade."""
    n = int(amount)
    sign = "-" if n < 0 else ""
    n = abs(n)
    if n >= 1_000_000_000:
        v = n / 1_000_000_000
        s = f"{v:.2f}".rstrip("0").rstrip(".")
        return f"{sign}{s}b"
    if n >= 1_000_000:
        v = n / 1_000_000
        s = f"{v:.2f}".rstrip("0").rstrip(".")
        return f"{sign}{s}m"
    if n >= 1_000:
        v = n / 1_000
        s = f"{v:.2f}".rstrip("0").rstrip(".")
        return f"{sign}{s}k"
    return f"{sign}{n}"


def amount_for_input(amount: int) -> str:
    """Prefill modal fields — prefer 1,25m style."""
    short = format_silver_short(amount)
    if short[-1:] in "kmb" and "." in short:
        return short.replace(".", ",")
    return short


def compute_breakdown(
    market: int,
    repairs: int,
    discount: int,
    silver_bags: int = 0,
) -> dict[str, int]:
    """TabBuyer math: discount tab first, then repairs, then add untaxed silver bags."""
    market = max(0, int(market))
    repairs = max(0, int(repairs))
    discount = max(0, min(100, int(discount)))
    silver_bags = max(0, int(silver_bags))
    discounted_tab = int(market * (100 - discount) / 100)
    after_repair = discounted_tab - repairs
    pool = after_repair + silver_bags
    return {
        "discounted_tab": discounted_tab,
        "after_repair": after_repair,
        "silver_bags": silver_bags,
        "pool": pool,
    }


def compute_pool(
    market: int,
    repairs: int,
    discount: int,
    silver_bags: int = 0,
) -> int:
    """Total silver to split across weighted shares."""
    return compute_breakdown(market, repairs, discount, silver_bags)["pool"]


def split_money_fields(split: dict[str, Any]) -> tuple[int, int, int, int]:
    return (
        int(split.get("market") or 0),
        int(split.get("repairs") or 0),
        int(split.get("discount") or 10),
        int(split.get("silver_bags") or 0),
    )


def compute_cuts(players: list[dict[str, Any]], pool: int) -> dict[int, int]:
    """Weighted split by share %; leftover silver goes to highest-share players."""
    players = _unique_players(players)
    if not players or pool <= 0:
        return {int(p["user_id"]): 0 for p in players}

    weights = [
        (int(p["user_id"]), max(0.0, float(p.get("share", 100)) / 100.0)) for p in players
    ]
    total_w = sum(w for _, w in weights)
    if total_w <= 0:
        return {uid: 0 for uid, _ in weights}

    raw = {uid: pool * w / total_w for uid, w in weights}
    cuts = {uid: int(v) for uid, v in raw.items()}
    leftover = pool - sum(cuts.values())
    # distribute leftover 1 silver at a time by largest fractional remainder
    order = sorted(
        weights,
        key=lambda item: (raw[item[0]] - cuts[item[0]], item[1]),
        reverse=True,
    )
    i = 0
    while leftover > 0 and order:
        cuts[order[i % len(order)][0]] += 1
        leftover -= 1
        i += 1
    return cuts


# ── mentions / display ───────────────────────────────────────────────────────


def parse_user_mentions(content: str) -> list[int]:
    seen: set[int] = set()
    out: list[int] = []
    for m in USER_MENTION_RE.finditer(content or ""):
        uid = int(m.group(1))
        if uid in seen:
            continue
        seen.add(uid)
        out.append(uid)
    return out


def collect_mentions_from_message(message: discord.Message) -> list[int]:
    chunks = [message.content or ""]
    for emb in message.embeds:
        if emb.title:
            chunks.append(emb.title)
        if emb.description:
            chunks.append(emb.description)
        if emb.author and emb.author.name:
            chunks.append(emb.author.name)
        for field in emb.fields:
            chunks.append(field.name or "")
            chunks.append(field.value or "")
        if emb.footer and emb.footer.text:
            chunks.append(emb.footer.text)
    # Also use Discord's parsed mentions (covers some edge cases)
    ordered = parse_user_mentions("\n".join(chunks))
    for u in message.mentions:
        if u.id not in ordered:
            ordered.append(u.id)
    return ordered


def display_name_for(guild: discord.Guild | None, user_id: int) -> str:
    ign = get_ign(user_id)
    if ign:
        return ign
    if guild:
        member = guild.get_member(user_id)
        if member:
            return member.display_name
    return f"User {user_id}"


def _is_mod(member: discord.Member) -> bool:
    mod = get(member.guild.roles, name="Mod")
    return bool(mod and mod in member.roles) or member.guild_permissions.administrator


def _find_lootsplit_forum(guild: discord.Guild) -> discord.ForumChannel | None:
    for ch in guild.channels:
        if isinstance(ch, discord.ForumChannel) and ch.name in LOOTSPLIT_NAMES:
            return ch
    return discord.utils.find(
        lambda c: isinstance(c, discord.ForumChannel) and "lootsplit" in c.name.lower(),
        guild.channels,
    )


def parse_message_link(text: str) -> tuple[int, int, int] | None:
    m = MESSAGE_LINK_RE.search(text.strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


ROLE_MENTION_RE = re.compile(r"<@&\d+>")
CHANNEL_MENTION_RE = re.compile(r"<#\d+>")


def suggest_label_from_message(message: discord.Message) -> str:
    """Guess a short content label from the host post."""
    if message.embeds:
        emb = message.embeds[0]
        for candidate in (emb.title, emb.author.name if emb.author else None):
            if candidate and candidate.strip():
                cleaned = _clean_label_text(candidate)
                if cleaned:
                    return cleaned[:40]
    text = message.content or ""
    for raw_line in text.splitlines():
        cleaned = _clean_label_text(raw_line)
        if cleaned:
            return cleaned[:40]
    return "Loot"


def _clean_label_text(text: str) -> str:
    text = USER_MENTION_RE.sub("", text or "")
    text = ROLE_MENTION_RE.sub("", text)
    text = CHANNEL_MENTION_RE.sub("", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\s+", " ", text).strip(" -\u2014|:•*")
    return text


def format_created_stamp(iso: str | None = None) -> str:
    """e.g. 25 Jul · 14:32 — from event post time (or now)."""
    if iso:
        try:
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except ValueError:
            dt = datetime.now().astimezone()
    else:
        dt = datetime.now().astimezone()
    local = dt.astimezone()
    return local.strftime("%d %b · %H:%M")


def build_split_title(label: str, *, posted_at: str | None = None) -> str:
    """Human title: 'Ganking · 25 Jul · 14:32' using host-post time."""
    label = (label or "").strip() or "Loot"
    # If they pasted a full title already containing a time, don't double-stamp
    if "·" in label and re.search(r"\d{1,2}:\d{2}", label):
        return label[:100]
    stamp = format_created_stamp(posted_at)
    return f"{label} · {stamp}"[:100]


def event_time_iso(split: dict[str, Any]) -> str | None:
    """Prefer host-post time; fall back to when the sheet was opened."""
    return split.get("source_posted_at") or split.get("created_at")


def split_label(split: dict[str, Any]) -> str:
    if split.get("label"):
        return str(split["label"])
    title = str(split.get("title") or "Loot")
    # strip trailing " · 25 Jul · 14:32" if present
    parts = title.split(" · ")
    if len(parts) >= 3 and re.match(r"\d{1,2}:\d{2}$", parts[-1].strip()):
        return " · ".join(parts[:-2]).strip() or title
    if len(parts) >= 2 and re.match(r"\d{1,2} \w+", parts[-1].strip()):
        return " · ".join(parts[:-1]).strip() or title
    return title


# ── sheet embed ──────────────────────────────────────────────────────────────


def _money_receipt(
    market: int,
    repairs: int,
    discount: int,
    bags: int,
    br: dict[str, int],
) -> str:
    """Compact spreadsheet-style block for Discord (monospace)."""
    rows = [
        ("Tab value", format_silver_short(market)),
        (f"Discount (−{discount}%)", format_silver_short(br["discounted_tab"])),
        ("Repairs", f"−{format_silver_short(repairs)}"),
        ("After repair", format_silver_short(br["after_repair"])),
        ("Silver bags", format_silver_short(bags)),
    ]
    width = max(len(label) for label, _ in rows)
    lines = [f"{label:<{width}}  {value:>10}" for label, value in rows]
    pool = br["pool"]
    lines.append("─" * (width + 12))
    lines.append(f"{'TOTAL POOL':<{width}}  {format_silver(pool):>10}")
    # Also show short form under the full number when useful
    short = format_silver_short(pool)
    if short != format_silver(pool) and pool != 0:
        lines.append(f"{'':<{width}}  {short:>10}")
    return "```\n" + "\n".join(lines) + "\n```"


def build_sheet_embed(split: dict[str, Any], guild: discord.Guild | None) -> discord.Embed:
    market, repairs, discount, bags = split_money_fields(split)
    br = compute_breakdown(market, repairs, discount, bags)
    pool = br["pool"]
    players = _unique_players(list(split.get("players") or []))
    cuts = compute_cuts(players, pool)
    status = split.get("status", "OPEN")

    color = {
        "OPEN": discord.Color.gold(),
        "FINALIZED": discord.Color.green(),
        "CANCELLED": discord.Color.dark_grey(),
        "UNDONE": discord.Color.orange(),
    }.get(status, discord.Color.gold())
    status_icon = {"OPEN": "🟡", "FINALIZED": "✅", "CANCELLED": "⬛", "UNDONE": "↩"}.get(status, "•")

    player_lines: list[str] = []
    for i, p in enumerate(players, start=1):
        uid = int(p["user_id"])
        share = float(p.get("share", 100))
        name = display_name_for(guild, uid)
        cut = cuts.get(uid, 0)
        # Share pill + trade-friendly cut
        player_lines.append(
            f"`{i:02}` **{name}** · `{share:g}%` → `{format_silver(cut)}`"
        )

    players_body = "\n".join(player_lines) if player_lines else "_No players yet — add some below._"
    if len(players_body) > 1800:
        players_body = players_body[:1800] + "\n…"

    money_filled = market > 0 or repairs > 0 or bags > 0 or pool > 0
    if money_filled:
        money_section = _money_receipt(market, repairs, discount, bags, br)
    else:
        money_section = "_Not filled yet — click **Fill / edit money**._"

    description = (
        f"{status_icon} **{status}** · **{len(players)}** player"
        f"{'' if len(players) == 1 else 's'}\n\n"
        f"### Players\n{players_body}\n\n"
        f"### Money\n{money_section}"
    )
    if len(description) > 4090:
        description = description[:4090] + "…"

    embed = discord.Embed(
        title=split.get("title") or "Loot Split",
        description=description,
        color=color,
        timestamp=datetime.now(timezone.utc),
    )

    meta: list[str] = []
    src = split.get("source_jump_url")
    if src:
        meta.append(f"[Host post]({src})")
    created_by = split.get("created_by")
    if created_by:
        meta.append(f"by **{display_name_for(guild, int(created_by))}**")
    stamp = format_created_stamp(event_time_iso(split))
    meta.append(stamp)

    if meta:
        embed.add_field(name="Info", value=" · ".join(meta), inline=False)

    embed.set_footer(text=f"{split_label(split)} · Mod tools below")
    return embed


async def refresh_sheet(bot: commands.Bot, split: dict[str, Any]) -> None:
    split_id = split["id"]
    async with _lock_for(split_id):
        while True:
            current = get_split(split_id) or split
            guild = bot.get_guild(int(current["guild_id"]))
            channel_id = current.get("forum_thread_id")
            message_id = current.get("forum_message_id")
            if not channel_id or not message_id:
                return
            channel = bot.get_channel(int(channel_id))
            if channel is None:
                try:
                    channel = await bot.fetch_channel(int(channel_id))
                except discord.HTTPException:
                    return
            if not isinstance(channel, (discord.Thread, discord.TextChannel)):
                return
            try:
                msg = await channel.fetch_message(int(message_id))
            except discord.HTTPException:
                return

            # Reload after awaits so we never paint a stale roster (removed ppl / old %)
            current = get_split(split_id) or current
            rev = int(current.get("rev") or 0)
            embed = build_sheet_embed(current, guild)
            view: discord.ui.View | None = None
            if current.get("status") == "OPEN":
                view = build_sheet_view(current, guild)
            try:
                await msg.edit(embed=embed, view=view)
            except discord.HTTPException:
                return
            latest = get_split(split_id)
            if not latest or int(latest.get("rev") or 0) == rev:
                return
            split = latest


# ── balances ─────────────────────────────────────────────────────────────────


def get_balance_entry(user_id: int) -> dict[str, Any]:
    data = _load_balances()
    key = str(user_id)
    entry = data.get(key)
    if not entry:
        return {"ign": get_ign(user_id) or "", "balance": 0, "history": []}
    return entry


def list_nonzero_balances() -> list[tuple[int, dict[str, Any]]]:
    """All ledger rows with balance != 0, highest first."""
    out: list[tuple[int, dict[str, Any]]] = []
    for uid_str, entry in _load_balances().items():
        try:
            uid = int(uid_str)
        except ValueError:
            continue
        bal = int(entry.get("balance") or 0)
        if bal != 0:
            out.append((uid, entry))
    out.sort(key=lambda item: int(item[1].get("balance") or 0), reverse=True)
    return out


def _already_credited_split(entry: dict[str, Any], split_id: str) -> bool:
    for h in entry.get("history") or []:
        if str(h.get("split_id") or "") == str(split_id) and h.get("type") == "credit":
            return True
    return False


def _already_reversed_split(entry: dict[str, Any], split_id: str) -> bool:
    for h in entry.get("history") or []:
        if (
            str(h.get("split_id") or "") == str(split_id)
            and h.get("reversal")
            and int(h.get("amount") or 0) < 0
        ):
            return True
    return False


def credit_balance(
    user_id: int,
    amount: int,
    *,
    reason: str,
    split_id: str | None,
    by: int,
) -> int:
    data = _load_balances()
    key = str(user_id)
    entry = data.get(key) or {"ign": "", "balance": 0, "history": []}
    if split_id and amount > 0 and _already_credited_split(entry, split_id):
        return int(entry.get("balance") or 0)
    entry["ign"] = get_ign(user_id) or entry.get("ign") or ""
    entry["balance"] = int(entry.get("balance") or 0) + amount
    hist = list(entry.get("history") or [])
    hist.insert(
        0,
        {
            "type": "credit" if amount >= 0 else "debit",
            "amount": amount,
            "reason": reason,
            "split_id": split_id,
            "by": by,
            "at": datetime.now(timezone.utc).isoformat(),
        },
    )
    entry["history"] = hist[:HISTORY_LIMIT]
    data[key] = entry
    _save_balances(data)
    return int(entry["balance"])


def credit_split_cuts(
    cuts: dict[int, int],
    *,
    reason: str,
    split_id: str,
    by: int,
) -> None:
    """Credit every cut in one balances write; skip anyone already paid for this split."""
    data = _load_balances()
    now = datetime.now(timezone.utc).isoformat()
    changed = False
    for uid, amount in cuts.items():
        if amount <= 0:
            continue
        key = str(uid)
        entry = data.get(key) or {"ign": "", "balance": 0, "history": []}
        if _already_credited_split(entry, split_id):
            continue
        entry["ign"] = get_ign(uid) or entry.get("ign") or ""
        entry["balance"] = int(entry.get("balance") or 0) + amount
        hist = list(entry.get("history") or [])
        hist.insert(
            0,
            {
                "type": "credit",
                "amount": amount,
                "reason": reason,
                "split_id": split_id,
                "by": by,
                "at": now,
            },
        )
        entry["history"] = hist[:HISTORY_LIMIT]
        data[key] = entry
        changed = True
    if changed:
        _save_balances(data)


def pay_balance(user_id: int, amount: int, *, by: int, note: str = "") -> tuple[bool, str, int]:
    """Subtract payout from ledger. Returns (ok, message, new_balance)."""
    data = _load_balances()
    key = str(user_id)
    entry = data.get(key) or {"ign": "", "balance": 0, "history": []}
    bal = int(entry.get("balance") or 0)
    if amount <= 0:
        return False, "Amount must be positive.", bal
    if amount > bal:
        return False, f"Balance is only `{format_silver(bal)}`.", bal
    entry["ign"] = get_ign(user_id) or entry.get("ign") or ""
    entry["balance"] = bal - amount
    hist = list(entry.get("history") or [])
    hist.insert(
        0,
        {
            "type": "debit",
            "amount": -amount,
            "reason": note or "Paid out in-game",
            "split_id": None,
            "by": by,
            "at": datetime.now(timezone.utc).isoformat(),
        },
    )
    entry["history"] = hist[:HISTORY_LIMIT]
    data[key] = entry
    _save_balances(data)
    return True, f"Paid `{format_silver(amount)}`. New balance: `{format_silver(entry['balance'])}`.", int(entry["balance"])


def remove_balance(
    user_id: int,
    amount: int,
    *,
    by: int,
    note: str = "",
    split_id: str | None = None,
) -> tuple[bool, str, int]:
    """Subtract silver from ledger. Balance may go negative."""
    if amount <= 0:
        entry = get_balance_entry(user_id)
        return False, "Amount must be positive.", int(entry.get("balance") or 0)
    data = _load_balances()
    key = str(user_id)
    entry = data.get(key) or {"ign": "", "balance": 0, "history": []}
    bal = int(entry.get("balance") or 0)
    entry["ign"] = get_ign(user_id) or entry.get("ign") or ""
    entry["balance"] = bal - amount
    hist = list(entry.get("history") or [])
    hist.insert(
        0,
        {
            "type": "debit",
            "amount": -amount,
            "reason": note or "Manual removal",
            "split_id": split_id,
            "by": by,
            "at": datetime.now(timezone.utc).isoformat(),
        },
    )
    entry["history"] = hist[:HISTORY_LIMIT]
    data[key] = entry
    _save_balances(data)
    new_bal = int(entry["balance"])
    neg = " (negative — they were already paid out)" if new_bal < 0 else ""
    return True, f"Removed `{format_silver(amount)}`. New balance: `{format_silver(new_bal)}`{neg}.", new_bal


def undo_last_credit(user_id: int, *, by: int) -> tuple[bool, str, int]:
    """Reverse the most recent credit on this ledger."""
    data = _load_balances()
    key = str(user_id)
    entry = data.get(key)
    if not entry:
        return False, "No balance history.", 0
    hist = list(entry.get("history") or [])
    for h in hist:
        amt = int(h.get("amount") or 0)
        if h.get("type") == "credit" and amt > 0:
            reason = str(h.get("reason") or "credit")
            entry["ign"] = get_ign(user_id) or entry.get("ign") or ""
            entry["balance"] = int(entry.get("balance") or 0) - amt
            hist.insert(
                0,
                {
                    "type": "debit",
                    "amount": -amt,
                    "reason": f"Undo: {reason}",
                    "split_id": h.get("split_id"),
                    "by": by,
                    "at": datetime.now(timezone.utc).isoformat(),
                    "reversal": True,
                },
            )
            entry["history"] = hist[:HISTORY_LIMIT]
            data[key] = entry
            _save_balances(data)
            new_bal = int(entry["balance"])
            neg = " (negative — they were already paid out)" if new_bal < 0 else ""
            return (
                True,
                f"Undid last credit `{format_silver(amt)}` ({reason}). "
                f"New balance: `{format_silver(new_bal)}`{neg}.",
                new_bal,
            )
    return False, "No credit entry to undo.", int(entry.get("balance") or 0)


def reverse_split_credits(
    cuts: dict[int, int],
    *,
    split_id: str,
    by: int,
) -> dict[int, int]:
    """Subtract finalized split payouts. Balance may go negative."""
    data = _load_balances()
    now = datetime.now(timezone.utc).isoformat()
    reason = f"Reversed loot split {split_id}"
    new_balances: dict[int, int] = {}
    for uid, amount in cuts.items():
        if amount <= 0:
            continue
        key = str(uid)
        entry = data.get(key) or {"ign": "", "balance": 0, "history": []}
        if _already_reversed_split(entry, split_id):
            new_balances[uid] = int(entry.get("balance") or 0)
            continue
        entry["ign"] = get_ign(uid) or entry.get("ign") or ""
        entry["balance"] = int(entry.get("balance") or 0) - amount
        hist = list(entry.get("history") or [])
        hist.insert(
            0,
            {
                "type": "debit",
                "amount": -amount,
                "reason": reason,
                "split_id": split_id,
                "by": by,
                "at": now,
                "reversal": True,
            },
        )
        entry["history"] = hist[:HISTORY_LIMIT]
        data[key] = entry
        new_balances[uid] = int(entry["balance"])
    _save_balances(data)
    return new_balances


# ── split actions (shared by slash + sheet buttons) ───────────────────────────


def _sync_split(dst: dict[str, Any], src: dict[str, Any]) -> None:
    if dst is src:
        return
    dst.clear()
    dst.update(src)


def add_player_to_split(split: dict[str, Any], user_id: int, share: float = 100) -> tuple[bool, str]:
    fresh = get_split(split["id"]) or split
    players = _unique_players(list(fresh.get("players") or []))
    if any(int(p["user_id"]) == user_id for p in players):
        _sync_split(split, fresh)
        return False, "Already on the sheet."
    players.append({"user_id": user_id, "share": float(share)})
    fresh["players"] = players
    fresh.pop("_selected_user", None)
    save_split(fresh)
    _sync_split(split, get_split(fresh["id"]) or fresh)
    return True, "added"


def remove_player_from_split(split: dict[str, Any], user_id: int) -> tuple[bool, str]:
    fresh = get_split(split["id"]) or split
    players = _unique_players(list(fresh.get("players") or []))
    kept = [p for p in players if int(p["user_id"]) != user_id]
    if len(kept) == len(players):
        _sync_split(split, fresh)
        return False, "They weren't on the sheet."
    fresh["players"] = kept
    if int(fresh.get("_selected_user") or 0) == user_id:
        fresh.pop("_selected_user", None)
    save_split(fresh)
    _sync_split(split, get_split(fresh["id"]) or fresh)
    return True, "removed"


def set_player_share(split: dict[str, Any], user_id: int, share: float) -> tuple[bool, str]:
    fresh = get_split(split["id"]) or split
    players = _unique_players(list(fresh.get("players") or []))
    found = False
    for p in players:
        if int(p["user_id"]) == user_id:
            p["share"] = float(share)
            found = True
            break
    if not found:
        _sync_split(split, fresh)
        return False, "That player is not on this split."
    fresh["players"] = players
    fresh.pop("_selected_user", None)
    save_split(fresh)
    _sync_split(split, get_split(fresh["id"]) or fresh)
    return True, "ok"


async def finalize_split(
    bot: commands.Bot,
    split: dict[str, Any],
    *,
    by: int,
    guild: discord.Guild | None,
) -> tuple[bool, str]:
    async with _lock_for(split["id"]):
        fresh = get_split(split["id"])
        if not fresh:
            return False, "Split not found."
        if fresh.get("status") == "FINALIZED":
            return False, "Already finalized — balances were not credited again."
        if fresh.get("status") != "OPEN":
            return False, f"Cannot finalize a **{fresh.get('status')}** split."

        fresh["players"] = _unique_players(list(fresh.get("players") or []))
        market, repairs, discount, bags = split_money_fields(fresh)
        pool = compute_pool(market, repairs, discount, bags)
        if pool <= 0:
            return False, "Pool is 0 — fill money first (tab / repairs / discount / bags)."

        cuts = compute_cuts(list(fresh.get("players") or []), pool)

        # Lock the sheet BEFORE crediting so a second Finalize can't pay again
        fresh["status"] = "FINALIZED"
        fresh["finalized_at"] = datetime.now(timezone.utc).isoformat()
        fresh["finalized_by"] = by
        fresh["final_pool"] = pool
        fresh["final_cuts"] = {str(k): v for k, v in cuts.items()}
        fresh.pop("_selected_user", None)
        save_split(fresh)

        credit_split_cuts(
            cuts,
            reason=f"Loot split {fresh['id']}",
            split_id=fresh["id"],
            by=by,
        )

    await refresh_sheet(bot, fresh)

    thread_id = fresh.get("forum_thread_id")
    if thread_id:
        ch = bot.get_channel(int(thread_id))
        if isinstance(ch, discord.Thread):
            try:
                new_name = (fresh.get("title") or "Split")[:90]
                if not new_name.lower().startswith("finalized"):
                    await ch.edit(name=f"✔ {new_name}"[:100], archived=False)
            except discord.HTTPException:
                pass

    lines = [f"Finalized — pool **{format_silver(pool)}** credited:"]
    for uid, amount in cuts.items():
        lines.append(f"• {display_name_for(guild, uid)}: `{format_silver(amount)}`")
    return True, "\n".join(lines)[:2000]


async def undo_split(
    bot: commands.Bot,
    split: dict[str, Any],
    *,
    by: int,
    guild: discord.Guild | None,
) -> tuple[bool, str]:
    async with _lock_for(split["id"]):
        fresh = get_split(split["id"])
        if not fresh:
            return False, "Split not found."
        if fresh.get("status") == "UNDONE":
            return False, "This split was already undone."
        if fresh.get("status") != "FINALIZED":
            return False, (
                f"Only **FINALIZED** splits can be undone (this is **{fresh.get('status')}**)."
            )

        cuts_raw = fresh.get("final_cuts") or {}
        cuts = {int(k): int(v) for k, v in cuts_raw.items() if int(v) > 0}
        if not cuts:
            return False, "No payout recorded on this split — nothing to reverse."

        fresh["status"] = "UNDONE"
        fresh["undone_at"] = datetime.now(timezone.utc).isoformat()
        fresh["undone_by"] = by
        fresh.pop("_selected_user", None)
        save_split(fresh)

        new_bals = reverse_split_credits(cuts, split_id=fresh["id"], by=by)

    await refresh_sheet(bot, fresh)

    thread_id = fresh.get("forum_thread_id")
    if thread_id:
        ch = bot.get_channel(int(thread_id))
        if isinstance(ch, discord.Thread):
            try:
                title = (fresh.get("title") or "Split")[:90]
                if title.startswith("✔ "):
                    title = title[2:]
                await ch.edit(name=f"↩ {title}"[:100], archived=False)
            except discord.HTTPException:
                pass

    lines = [
        f"Undone **{fresh.get('title') or fresh['id']}** — credits reversed "
        f"(balance can go **negative** if already paid out):"
    ]
    for uid, amount in cuts.items():
        bal = new_bals.get(uid, 0)
        lines.append(
            f"• {display_name_for(guild, uid)}: −`{format_silver(amount)}` → `{format_silver(bal)}`"
        )
    return True, "\n".join(lines)[:2000]


async def undo_all_splits(
    bot: commands.Bot,
    guild_id: int,
    *,
    by: int,
    guild: discord.Guild | None,
) -> tuple[bool, str]:
    finalized = [
        s
        for s in _load_splits()["splits"].values()
        if s.get("guild_id") == guild_id and s.get("status") == "FINALIZED"
    ]
    if not finalized:
        return False, "No **FINALIZED** splits to undo."

    finalized.sort(key=lambda s: s.get("finalized_at") or "", reverse=True)
    ok_count = 0
    summary: list[str] = []
    for s in finalized:
        ok, msg = await undo_split(bot, s, by=by, guild=guild)
        title = s.get("title") or s["id"]
        if ok:
            ok_count += 1
            summary.append(f"✓ {title}")
        else:
            summary.append(f"✗ {title}: {msg.split(chr(10))[0][:80]}")

    return True, (
        f"Undid **{ok_count}/{len(finalized)}** finalized split(s). "
        f"Balances may be **negative** if people were already paid.\n"
        + "\n".join(summary)
    )[:2000]


async def cancel_split(bot: commands.Bot, split: dict[str, Any]) -> tuple[bool, str]:
    if split.get("status") != "OPEN":
        return False, f"Split is already **{split.get('status')}**."
    split["status"] = "CANCELLED"
    split.pop("_selected_user", None)
    save_split(split)
    await refresh_sheet(bot, split)
    return True, "Split cancelled — no balances changed."


# ── sheet buttons ────────────────────────────────────────────────────────────


class CreateLabelModal(discord.ui.Modal, title="Name this loot split"):
    """Ask for a short content label so same-day runs stay distinguishable."""

    def __init__(self, source: discord.Message) -> None:
        super().__init__()
        self.channel_id = source.channel.id
        self.message_id = source.id
        suggested = suggest_label_from_message(source)
        self.label_input = discord.ui.TextInput(
            label="Content name",
            placeholder="e.g. Ganking, Static #2, Lym T8",
            default=suggested[:60],
            required=True,
            max_length=60,
        )
        self.add_item(self.label_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if not guild:
            await interaction.followup.send("Guild only.", ephemeral=True)
            return
        ch = guild.get_channel(self.channel_id) or interaction.client.get_channel(self.channel_id)
        if ch is None:
            try:
                ch = await interaction.client.fetch_channel(self.channel_id)
            except discord.HTTPException:
                await interaction.followup.send("Couldn't find the host message channel.", ephemeral=True)
                return
        if not isinstance(ch, (discord.TextChannel, discord.Thread)):
            await interaction.followup.send("Invalid host channel.", ephemeral=True)
            return
        try:
            source = await ch.fetch_message(self.message_id)
        except discord.HTTPException:
            await interaction.followup.send("Couldn't find the host message.", ephemeral=True)
            return
        await create_split_from_message(interaction, source, title=str(self.label_input.value))


class RenameModal(discord.ui.Modal, title="Rename loot split"):
    def __init__(self, split: dict[str, Any]) -> None:
        super().__init__()
        self.split_id = split["id"]
        self.label_input = discord.ui.TextInput(
            label="Content name",
            placeholder="e.g. Ganking #2, Static evening",
            default=split_label(split)[:60],
            required=True,
            max_length=60,
        )
        self.add_item(self.label_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        split = get_split(self.split_id)
        if not split or split.get("status") != "OPEN":
            await interaction.response.send_message("Split is not open.", ephemeral=True)
            return
        if not isinstance(interaction.user, discord.Member) or not _is_mod(interaction.user):
            await interaction.response.send_message("Only **Mod** can rename.", ephemeral=True)
            return
        label = str(self.label_input.value).strip() or "Loot"
        split["label"] = label
        split["title"] = build_split_title(label, posted_at=event_time_iso(split))
        save_split(split)
        await interaction.response.defer(ephemeral=True)
        await refresh_sheet(interaction.client, split)  # type: ignore[arg-type]
        # rename forum thread
        thread_id = split.get("forum_thread_id")
        if thread_id:
            ch = interaction.client.get_channel(int(thread_id))
            if isinstance(ch, discord.Thread):
                try:
                    await ch.edit(name=split["title"][:100])
                except discord.HTTPException:
                    pass
        await interaction.followup.send(f"Renamed to **{split['title']}**.", ephemeral=True)


class MoneyModal(discord.ui.Modal, title="Fill / edit money"):
    """Spreadsheet-style inputs: tab → discount → repairs → bags."""

    def __init__(self, split: dict[str, Any]) -> None:
        super().__init__()
        self.split_id = split["id"]
        market, repairs, discount, bags = split_money_fields(split)

        self.tab_input = discord.ui.TextInput(
            label="Tab value",
            placeholder="e.g. 100m or 1,25m",
            default=amount_for_input(market),
            required=True,
            max_length=32,
        )
        self.repair_input = discord.ui.TextInput(
            label="Repair cost",
            placeholder="e.g. 15m",
            default=amount_for_input(repairs),
            required=True,
            max_length=32,
        )
        self.discount_input = discord.ui.TextInput(
            label="Discount % (TabBuyer)",
            placeholder="10 or 15",
            default=str(discount),
            required=True,
            max_length=3,
        )
        self.bags_input = discord.ui.TextInput(
            label="Silver bags (untaxed)",
            placeholder="e.g. 0 or 2,5m",
            default=amount_for_input(bags),
            required=True,
            max_length=32,
        )
        self.add_item(self.tab_input)
        self.add_item(self.repair_input)
        self.add_item(self.discount_input)
        self.add_item(self.bags_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        split = get_split(self.split_id)
        if not split or split.get("status") != "OPEN":
            await interaction.response.send_message("Split is not open.", ephemeral=True)
            return
        if not isinstance(interaction.user, discord.Member) or not _is_mod(interaction.user):
            await interaction.response.send_message("Only **Mod** can edit money.", ephemeral=True)
            return

        tab = parse_silver(str(self.tab_input.value))
        rep = parse_silver(str(self.repair_input.value))
        bags = parse_silver(str(self.bags_input.value))
        try:
            disc = int(str(self.discount_input.value).strip().replace("%", ""))
        except ValueError:
            disc = -1

        if tab is None or rep is None or bags is None:
            await interaction.response.send_message(
                "Invalid silver amount — use e.g. `100m`, `1,25m`, or `1250000`.",
                ephemeral=True,
            )
            return
        if disc < 0 or disc > 100:
            await interaction.response.send_message("Discount must be 0–100 (usually 10 or 15).", ephemeral=True)
            return

        split["market"] = tab
        split["repairs"] = rep
        split["discount"] = disc
        split["silver_bags"] = bags
        save_split(split)

        br = compute_breakdown(tab, rep, disc, bags)
        pool = br["pool"]
        cuts = compute_cuts(list(split.get("players") or []), pool)
        n = len(split.get("players") or [])
        equalish = format_silver(pool // n) if n else "0"

        await interaction.response.defer(ephemeral=True)
        await refresh_sheet(interaction.client, split)  # type: ignore[arg-type]

        cut_preview = ", ".join(
            f"{display_name_for(interaction.guild, uid)} `{format_silver(cut)}`"
            for uid, cut in list(cuts.items())[:8]
        )
        await interaction.followup.send(
            f"**Filled in**\n"
            f"Tab `{format_silver_short(tab)}` → discounted `{format_silver_short(br['discounted_tab'])}` (−{disc}%)\n"
            f"After repair `{format_silver_short(br['after_repair'])}` + bags `{format_silver_short(bags)}`\n"
            f"Total pool **`{format_silver(pool)}`** · {n} players"
            f"{f' · equal share ~`{equalish}`' if n else ''}\n"
            f"{cut_preview}",
            ephemeral=True,
        )


class OpenMoneyButton(discord.ui.View):
    """Ephemeral one-click opener for the money modal (from /split money or refresh)."""

    def __init__(self, split_id: str) -> None:
        super().__init__(timeout=180)
        self.split_id = split_id

    @discord.ui.button(label="Open money form", style=discord.ButtonStyle.success)
    async def open_form(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not isinstance(interaction.user, discord.Member) or not _is_mod(interaction.user):
            await interaction.response.send_message("Only **Mod** can edit money.", ephemeral=True)
            return
        split = get_split(self.split_id)
        if not split or split.get("status") != "OPEN":
            await interaction.response.send_message("Split is not open.", ephemeral=True)
            return
        await interaction.response.send_modal(MoneyModal(split))


class ConfirmActionView(discord.ui.View):
    """Ephemeral Yes/No for finalize or cancel."""

    def __init__(self, split_id: str, action: str) -> None:
        super().__init__(timeout=60)
        self.split_id = split_id
        self.action = action  # "finalize" | "cancel" | "undo" | "undo-all"

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.danger)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not isinstance(interaction.user, discord.Member) or not _is_mod(interaction.user):
            await interaction.response.send_message("Only **Mod** can do that.", ephemeral=True)
            return
        await interaction.response.edit_message(content="Working…", view=None)
        bot = interaction.client  # type: ignore[assignment]
        if self.action == "undo-all":
            if not interaction.guild:
                await interaction.edit_original_response(content="Guild only.", view=None)
                return
            _ok, msg = await undo_all_splits(
                bot,
                interaction.guild.id,
                by=interaction.user.id,
                guild=interaction.guild,
            )
        else:
            split = get_split(self.split_id)
            if not split:
                await interaction.edit_original_response(content="Split not found.", view=None)
                return
            if self.action == "finalize":
                _ok, msg = await finalize_split(
                    bot, split, by=interaction.user.id, guild=interaction.guild
                )
            elif self.action == "undo":
                _ok, msg = await undo_split(
                    bot, split, by=interaction.user.id, guild=interaction.guild
                )
            else:
                _ok, msg = await cancel_split(bot, split)
        try:
            await interaction.edit_original_response(content=msg[:2000])
        except discord.HTTPException:
            await interaction.followup.send(msg[:2000], ephemeral=True)

    @discord.ui.button(label="No", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Okay, nothing changed.", view=None)


class SplitSheetView(discord.ui.View):
    """Persistent controls on the forum starter message."""

    def __init__(self, split_id: str | None = None) -> None:
        super().__init__(timeout=None)
        self.split_id = split_id

    async def _resolve_split(self, interaction: discord.Interaction) -> dict[str, Any] | None:
        if self.split_id:
            s = get_split(self.split_id)
            if s:
                return s
        channel = interaction.channel
        if isinstance(channel, discord.Thread):
            return find_split_by_thread(channel.id)
        return None

    async def _ensure_mod_open(self, interaction: discord.Interaction) -> dict[str, Any] | None:
        if not isinstance(interaction.user, discord.Member) or not _is_mod(interaction.user):
            await interaction.response.send_message("Only **Mod** can edit this split.", ephemeral=True)
            return None
        split = await self._resolve_split(interaction)
        if not split:
            await interaction.response.send_message("Split not found.", ephemeral=True)
            return None
        if split.get("status") != "OPEN":
            await interaction.response.send_message(
                f"Split is **{split.get('status')}** — locked.",
                ephemeral=True,
            )
            return None
        return split

    def _selected_uid(self, split: dict[str, Any]) -> int | None:
        uid = split.get("_selected_user")
        if uid:
            return int(uid)
        for child in self.children:
            if isinstance(child, discord.ui.Select) and getattr(child, "custom_id", None) == "kerp:split:pick":
                if child.values:
                    try:
                        return int(child.values[0])
                    except ValueError:
                        return None
        return None

    @discord.ui.button(
        label="Fill / edit money",
        style=discord.ButtonStyle.success,
        custom_id="kerp:split:money",
        row=0,
    )
    async def edit_money(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        split = await self._ensure_mod_open(interaction)
        if not split:
            return
        await interaction.response.send_modal(MoneyModal(split))

    @discord.ui.button(label="Rename", style=discord.ButtonStyle.secondary, custom_id="kerp:split:rename", row=0)
    async def rename_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        split = await self._ensure_mod_open(interaction)
        if not split:
            return
        await interaction.response.send_modal(RenameModal(split))

    @discord.ui.button(label="Finalize", style=discord.ButtonStyle.primary, custom_id="kerp:split:finalize", row=0)
    async def finalize_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        split = await self._ensure_mod_open(interaction)
        if not split:
            return
        market, repairs, discount, bags = split_money_fields(split)
        pool = compute_pool(market, repairs, discount, bags)
        if pool <= 0:
            await interaction.response.send_message("Pool is 0 — fill money first.", ephemeral=True)
            return
        n = len(split.get("players") or [])
        await interaction.response.send_message(
            f"Finalize this split?\n"
            f"Pool **`{format_silver(pool)}`** → credit **{n}** player balance(s).\n"
            f"This cannot be undone.",
            view=ConfirmActionView(split["id"], "finalize"),
            ephemeral=True,
        )

    @discord.ui.button(label="Cancel split", style=discord.ButtonStyle.danger, custom_id="kerp:split:cancel", row=0)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        split = await self._ensure_mod_open(interaction)
        if not split:
            return
        await interaction.response.send_message(
            "Cancel this open split? No balances will change.",
            view=ConfirmActionView(split["id"], "cancel"),
            ephemeral=True,
        )

    @discord.ui.select(
        placeholder="Pick player (share / remove)…",
        custom_id="kerp:split:pick",
        min_values=1,
        max_values=1,
        row=1,
        options=[discord.SelectOption(label="Refresh sheet first", value="0")],
    )
    async def pick_player(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        split = await self._ensure_mod_open(interaction)
        if not split:
            return
        uid = select.values[0]
        if uid == "0":
            await interaction.response.send_message("No players on this sheet.", ephemeral=True)
            return
        split["_selected_user"] = int(uid)
        save_split(split)
        name = display_name_for(interaction.guild, int(uid))
        await interaction.response.send_message(
            f"Selected **{name}** — set their share or remove them here:",
            view=PlayerEditView(split["id"], int(uid)),
            ephemeral=True,
        )

    async def _apply_share(self, interaction: discord.Interaction, share: float) -> None:
        split = await self._ensure_mod_open(interaction)
        if not split:
            return
        uid = self._selected_uid(split)
        if not uid:
            await interaction.response.send_message(
                "Pick a player in the dropdown first — action buttons will pop up.",
                ephemeral=True,
            )
            return
        ok, err = set_player_share(split, uid, share)
        if not ok:
            await interaction.response.send_message(err, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        split = get_split(split["id"]) or split
        await refresh_sheet(interaction.client, split)  # type: ignore[arg-type]
        name = display_name_for(interaction.guild, uid)
        await interaction.followup.send(f"Set **{name}** to **{share:g}%**.", ephemeral=True)

    @discord.ui.button(label="25%", style=discord.ButtonStyle.secondary, custom_id="kerp:split:25", row=2)
    async def share_25(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._apply_share(interaction, 25)

    @discord.ui.button(label="50%", style=discord.ButtonStyle.secondary, custom_id="kerp:split:50", row=2)
    async def share_50(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._apply_share(interaction, 50)

    @discord.ui.button(label="75%", style=discord.ButtonStyle.secondary, custom_id="kerp:split:75", row=2)
    async def share_75(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._apply_share(interaction, 75)

    @discord.ui.button(label="100%", style=discord.ButtonStyle.primary, custom_id="kerp:split:100", row=2)
    async def share_100(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._apply_share(interaction, 100)

    @discord.ui.button(label="Remove", style=discord.ButtonStyle.danger, custom_id="kerp:split:remove", row=2)
    async def remove_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        split = await self._ensure_mod_open(interaction)
        if not split:
            return
        uid = self._selected_uid(split)
        if not uid:
            await interaction.response.send_message(
                "Pick a player in the dropdown first, then **Remove**.",
                ephemeral=True,
            )
            return
        name = display_name_for(interaction.guild, uid)
        ok, err = remove_player_from_split(split, uid)
        if not ok:
            await interaction.response.send_message(err, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        # reload after save
        split = get_split(split["id"]) or split
        await refresh_sheet(interaction.client, split)  # type: ignore[arg-type]
        await interaction.followup.send(f"Removed **{name}**.", ephemeral=True)

    @discord.ui.select(
        cls=discord.ui.UserSelect,
        placeholder="Add a player…",
        custom_id="kerp:split:adduser",
        min_values=1,
        max_values=1,
        row=3,
    )
    async def add_user(self, interaction: discord.Interaction, select: discord.ui.UserSelect) -> None:
        split = await self._ensure_mod_open(interaction)
        if not split:
            return
        user = select.values[0]
        if getattr(user, "bot", False):
            await interaction.response.send_message("Can't add bots.", ephemeral=True)
            return
        ok, err = add_player_to_split(split, user.id, 100)
        if not ok:
            await interaction.response.send_message(err, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        split = get_split(split["id"]) or split
        await refresh_sheet(interaction.client, split)  # type: ignore[arg-type]
        name = display_name_for(interaction.guild, user.id)
        await interaction.followup.send(f"Added **{name}** at **100%**.", ephemeral=True)


class PlayerEditView(discord.ui.View):
    """Ephemeral actions bound to one player — avoids applying % / remove to the wrong person."""

    def __init__(self, split_id: str, user_id: int) -> None:
        super().__init__(timeout=180)
        self.split_id = split_id
        self.user_id = user_id

    async def _open_split(self, interaction: discord.Interaction) -> dict[str, Any] | None:
        if not isinstance(interaction.user, discord.Member) or not _is_mod(interaction.user):
            await interaction.response.send_message("Only **Mod** can edit this split.", ephemeral=True)
            return None
        split = get_split(self.split_id)
        if not split or split.get("status") != "OPEN":
            await interaction.response.send_message("Split is not open.", ephemeral=True)
            return None
        return split

    async def _apply_share(self, interaction: discord.Interaction, share: float) -> None:
        split = await self._open_split(interaction)
        if not split:
            return
        ok, err = set_player_share(split, self.user_id, share)
        if not ok:
            await interaction.response.send_message(err, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        split = get_split(self.split_id) or split
        await refresh_sheet(interaction.client, split)  # type: ignore[arg-type]
        name = display_name_for(interaction.guild, self.user_id)
        await interaction.followup.send(f"Set **{name}** to **{share:g}%**.", ephemeral=True)

    @discord.ui.button(label="25%", style=discord.ButtonStyle.secondary)
    async def share_25(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._apply_share(interaction, 25)

    @discord.ui.button(label="50%", style=discord.ButtonStyle.secondary)
    async def share_50(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._apply_share(interaction, 50)

    @discord.ui.button(label="75%", style=discord.ButtonStyle.secondary)
    async def share_75(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._apply_share(interaction, 75)

    @discord.ui.button(label="100%", style=discord.ButtonStyle.primary)
    async def share_100(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._apply_share(interaction, 100)

    @discord.ui.button(label="Remove", style=discord.ButtonStyle.danger)
    async def remove_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        split = await self._open_split(interaction)
        if not split:
            return
        name = display_name_for(interaction.guild, self.user_id)
        ok, err = remove_player_from_split(split, self.user_id)
        if not ok:
            await interaction.response.send_message(err, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        split = get_split(self.split_id) or split
        await refresh_sheet(interaction.client, split)  # type: ignore[arg-type]
        await interaction.followup.send(f"Removed **{name}**.", ephemeral=True)


def build_sheet_view(split: dict[str, Any], guild: discord.Guild | None) -> SplitSheetView:
    view = SplitSheetView(split["id"])
    # Rebuild select options from current players
    options: list[discord.SelectOption] = []
    for p in _unique_players(list(split.get("players") or [])):
        uid = int(p["user_id"])
        name = display_name_for(guild, uid)[:100]
        options.append(
            discord.SelectOption(
                label=name,
                value=str(uid),
                description=f"{float(p.get('share', 100)):g}% share",
            )
        )
    if not options:
        options = [discord.SelectOption(label="No players", value="0")]
    # Discord max 25 options
    options = options[:25]
    for child in view.children:
        if isinstance(child, discord.ui.Select) and child.custom_id == "kerp:split:pick":
            child.options = options
            break
    return view


# ── create helpers ───────────────────────────────────────────────────────────


async def resolve_source_message(
    interaction: discord.Interaction,
    message: str | None,
) -> discord.Message | None:
    """Resolve host roster message from link, or from a replied-to message if available."""
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
        # bare snowflake in current channel
        if message.isdigit():
            try:
                return await interaction.channel.fetch_message(int(message))  # type: ignore[union-attr]
            except (discord.HTTPException, AttributeError):
                return None

    # If the user recently replied to a message in this channel, use that target
    # (Discord slash commands do not pass reply context; this approximates the flow.)
    if isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
        async for msg in interaction.channel.history(limit=20):
            if msg.author.id != interaction.user.id:
                continue
            if msg.reference and msg.reference.message_id:
                try:
                    return await interaction.channel.fetch_message(msg.reference.message_id)
                except discord.HTTPException:
                    return None
            break
    return None


async def create_split_from_message(
    interaction: discord.Interaction,
    source: discord.Message,
    *,
    title: str | None = None,
) -> dict[str, Any] | None:
    assert interaction.guild and isinstance(interaction.user, discord.Member)
    guild = interaction.guild
    forum = _find_lootsplit_forum(guild)
    if not forum:
        await interaction.followup.send(
            "Forum **📋・lootsplit** not found — run `/setup` first.",
            ephemeral=True,
        )
        return None

    user_ids = collect_mentions_from_message(source)
    players: list[dict[str, Any]] = []
    for uid in user_ids:
        member = guild.get_member(uid)
        if member and member.bot:
            continue
        # skip if we can confirm it's the bot
        if interaction.client.user and uid == interaction.client.user.id:
            continue
        players.append({"user_id": uid, "share": 100})
    players = _unique_players(players)

    if not players:
        await interaction.followup.send(
            "No user mentions found on that message (role pings are ignored).",
            ephemeral=True,
        )
        return None

    for existing in _load_splits()["splits"].values():
        if (
            existing.get("source_message_id") == source.id
            and existing.get("status") == "OPEN"
            and existing.get("guild_id") == guild.id
        ):
            thread_id = existing.get("forum_thread_id")
            mention = f"<#{thread_id}>" if thread_id else existing.get("title") or existing.get("id")
            await interaction.followup.send(
                f"There's already an **open** split from that host post: {mention}\n"
                f"Edit that sheet (add/remove / %) instead of creating another — "
                f"a second sheet would pay people twice.",
                ephemeral=True,
            )
            return None

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    created_at = datetime.now(timezone.utc).isoformat()
    # Stamp from when the host posted the event, not when the sheet was opened
    posted_at = source.created_at
    if posted_at.tzinfo is None:
        posted_at = posted_at.replace(tzinfo=timezone.utc)
    source_posted_at = posted_at.isoformat()
    label = (title or "Loot").strip() or "Loot"
    # strip accidental full stamped titles down to label
    if "·" in label and re.search(r"\d{1,2}:\d{2}", label):
        label = split_label({"title": label, "label": None})
    split_title = build_split_title(label, posted_at=source_posted_at)
    split_id = uuid.uuid4().hex[:10]
    split: dict[str, Any] = {
        "id": split_id,
        "guild_id": guild.id,
        "status": "OPEN",
        "label": label[:60],
        "title": split_title,
        "created_by": interaction.user.id,
        "created_at": created_at,
        "source_posted_at": source_posted_at,
        "source_channel_id": source.channel.id,
        "source_message_id": source.id,
        "source_jump_url": source.jump_url,
        "forum_channel_id": forum.id,
        "forum_thread_id": None,
        "forum_message_id": None,
        "market": 0,
        "repairs": 0,
        "discount": 10,
        "silver_bags": 0,
        "players": players,
    }

    embed = build_sheet_embed(split, guild)
    view = build_sheet_view(split, guild)
    try:
        thread_with_msg = await forum.create_thread(
            name=split_title,
            embed=embed,
            view=view,
            reason=f"Loot split by {interaction.user}",
        )
    except discord.HTTPException as e:
        await interaction.followup.send(f"Couldn't create lootsplit thread: {e}", ephemeral=True)
        return None

    thread = thread_with_msg.thread
    starter = thread_with_msg.message
    split["forum_thread_id"] = thread.id
    split["forum_message_id"] = starter.id
    save_split(split)

    await interaction.followup.send(
        f"Open split created: {thread.mention}\n"
        f"**{split_title}** — {len(players)} players.\n"
        f"Use **Rename** on the sheet if you want a clearer name.",
        ephemeral=True,
    )
    return split


# ── resolve active split for edit commands ───────────────────────────────────


def resolve_split_for_channel(
    interaction: discord.Interaction,
    split_id: str | None,
) -> dict[str, Any] | None:
    if split_id:
        return get_split(split_id)
    ch = interaction.channel
    if isinstance(ch, discord.Thread):
        return find_split_by_thread(ch.id)
    # latest open split created by this user in guild
    opens = [
        s
        for s in _load_splits()["splits"].values()
        if s.get("guild_id") == (interaction.guild.id if interaction.guild else None)
        and s.get("status") == "OPEN"
        and s.get("created_by") == interaction.user.id
    ]
    if len(opens) == 1:
        return opens[0]
    if opens:
        opens.sort(key=lambda s: s.get("created_at") or "", reverse=True)
        return opens[0]
    return None


# ── register ─────────────────────────────────────────────────────────────────


def register_split(bot: commands.Bot) -> None:
    split = app_commands.Group(name="split", description="Loot split ledger")

    async def _require_mod(interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member) or not _is_mod(interaction.user):
            await interaction.response.send_message("Only **Mod** can do that.", ephemeral=True)
            return False
        return True

    async def _send_balance(
        interaction: discord.Interaction,
        target: discord.abc.User,
    ) -> None:
        entry = get_balance_entry(target.id)
        name = display_name_for(interaction.guild, target.id)
        hist = entry.get("history") or []
        hist_lines = []
        for h in hist[:5]:
            amt = int(h.get("amount") or 0)
            hist_lines.append(
                f"`{format_silver(amt)}` — {h.get('reason') or h.get('type')} "
                f"({(h.get('at') or '')[:10]})"
            )
        body = f"**{name}** balance: `{format_silver(int(entry.get('balance') or 0))}`"
        if hist_lines:
            body += "\n\nRecent:\n" + "\n".join(hist_lines)
        await interaction.response.send_message(body, ephemeral=True)

    @split.command(name="create", description="Create a loot split from a host roster message")
    @app_commands.describe(
        message="Message link (Copy Message Link). Optional if you just replied to the host post.",
        title="Short name e.g. Ganking or Static #2 — time is added automatically",
    )
    async def split_create(
        interaction: discord.Interaction,
        message: str | None = None,
        title: str | None = None,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        if not await _require_mod(interaction):
            return
        source = await resolve_source_message(interaction, message)
        if not source:
            await interaction.response.send_message(
                "Couldn't find the roster message.\n"
                "• Paste a **message link**, or\n"
                "• Reply to the host post first, then run `/split create`, or\n"
                "• Right‑click the host post → **Apps → Create Loot Split**.",
                ephemeral=True,
            )
            return
        if title and title.strip():
            await interaction.response.defer(ephemeral=True)
            await create_split_from_message(interaction, source, title=title.strip())
            return
        # Ask for a readable name (Ganking / Static #2) instead of a cryptic id
        await interaction.response.send_modal(CreateLabelModal(source))

    @split.command(name="list", description="List open loot splits (by name + time)")
    async def split_list(interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        opens = [
            s
            for s in _load_splits()["splits"].values()
            if s.get("guild_id") == interaction.guild.id and s.get("status") == "OPEN"
        ]
        opens.sort(key=lambda s: s.get("created_at") or "", reverse=True)
        if not opens:
            await interaction.response.send_message("No **OPEN** splits right now.", ephemeral=True)
            return
        lines = ["**Open loot splits:**"]
        for s in opens[:15]:
            title = s.get("title") or split_label(s)
            thread_id = s.get("forum_thread_id")
            link = f"https://discord.com/channels/{interaction.guild.id}/{thread_id}/{s.get('forum_message_id') or thread_id}" if thread_id else ""
            who = display_name_for(interaction.guild, int(s["created_by"])) if s.get("created_by") else "?"
            if link:
                lines.append(f"• [{title}]({link}) — by {who}")
            else:
                lines.append(f"• **{title}** — by {who}")
        await interaction.response.send_message("\n".join(lines)[:2000], ephemeral=True)

    @split.command(name="share", description="Set a player's share % (100 / 75 / 50 / 25 / custom)")
    @app_commands.describe(
        player="Player to update",
        percent="Share percent (e.g. 100, 75, 50, or custom)",
        split_id="Split ID (optional inside the split thread)",
    )
    async def split_share(
        interaction: discord.Interaction,
        player: discord.Member,
        percent: app_commands.Range[float, 0, 500],
        split_id: str | None = None,
    ) -> None:
        if not await _require_mod(interaction):
            return
        s = resolve_split_for_channel(interaction, split_id)
        if not s:
            await interaction.response.send_message(
                "No open split found. Use this in the lootsplit thread or pass `split_id`.",
                ephemeral=True,
            )
            return
        if s.get("status") != "OPEN":
            await interaction.response.send_message("That split is locked.", ephemeral=True)
            return
        found = False
        for p in s.get("players") or []:
            if int(p["user_id"]) == player.id:
                found = True
                break
        if not found:
            await interaction.response.send_message(
                f"{player.mention} is not on this split — use `/split add`.",
                ephemeral=True,
            )
            return
        ok, err = set_player_share(s, player.id, float(percent))
        if not ok:
            await interaction.response.send_message(err, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        s = get_split(s["id"]) or s
        await refresh_sheet(bot, s)
        await interaction.followup.send(
            f"Set **{display_name_for(interaction.guild, player.id)}** to **{float(percent):g}%**.",
            ephemeral=True,
        )

    @split.command(name="add", description="Add a player to the open split")
    @app_commands.describe(
        player="Player to add",
        percent="Starting share % (default 100)",
        split_id="Split ID (optional inside the split thread)",
    )
    async def split_add(
        interaction: discord.Interaction,
        player: discord.Member,
        percent: app_commands.Range[float, 0, 500] = 100,
        split_id: str | None = None,
    ) -> None:
        if not await _require_mod(interaction):
            return
        s = resolve_split_for_channel(interaction, split_id)
        if not s or s.get("status") != "OPEN":
            await interaction.response.send_message("No open split found.", ephemeral=True)
            return
        ok, err = add_player_to_split(s, player.id, float(percent))
        if not ok:
            await interaction.response.send_message(err, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        s = get_split(s["id"]) or s
        await refresh_sheet(bot, s)
        await interaction.followup.send(
            f"Added **{display_name_for(interaction.guild, player.id)}** at **{float(percent):g}%**.",
            ephemeral=True,
        )

    @split.command(name="remove", description="Remove a player from the open split")
    @app_commands.describe(player="Player to remove", split_id="Split ID (optional)")
    async def split_remove(
        interaction: discord.Interaction,
        player: discord.Member,
        split_id: str | None = None,
    ) -> None:
        if not await _require_mod(interaction):
            return
        s = resolve_split_for_channel(interaction, split_id)
        if not s or s.get("status") != "OPEN":
            await interaction.response.send_message("No open split found.", ephemeral=True)
            return
        ok, err = remove_player_from_split(s, player.id)
        if not ok:
            await interaction.response.send_message(err, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        s = get_split(s["id"]) or s
        await refresh_sheet(bot, s)
        await interaction.followup.send(
            f"Removed **{display_name_for(interaction.guild, player.id)}**.",
            ephemeral=True,
        )

    @split.command(name="money", description="Fill or edit tab / repairs / discount / bags (opens a form)")
    @app_commands.describe(
        market="Optional: set tab value directly (e.g. 100m)",
        repairs="Optional: set repair cost",
        discount="Optional: TabBuyer discount % (10 or 15)",
        bags="Optional: silver bags (untaxed)",
        split_id="Split ID (optional inside the split thread)",
    )
    async def split_money(
        interaction: discord.Interaction,
        market: str | None = None,
        repairs: str | None = None,
        discount: app_commands.Range[int, 0, 100] | None = None,
        bags: str | None = None,
        split_id: str | None = None,
    ) -> None:
        if not await _require_mod(interaction):
            return
        s = resolve_split_for_channel(interaction, split_id)
        if not s or s.get("status") != "OPEN":
            await interaction.response.send_message("No open split found.", ephemeral=True)
            return

        # No fields given → refresh sheet buttons + offer the form
        if market is None and repairs is None and discount is None and bags is None:
            await interaction.response.defer(ephemeral=True)
            await refresh_sheet(bot, s)
            await interaction.followup.send(
                "Sheet updated. Click **Open money form** below "
                "(or **Fill / edit money** on the sheet) — you can change values anytime while it's OPEN.",
                view=OpenMoneyButton(s["id"]),
                ephemeral=True,
            )
            return

        if market is not None:
            v = parse_silver(market)
            if v is None:
                await interaction.response.send_message("Invalid tab value.", ephemeral=True)
                return
            s["market"] = v
        if repairs is not None:
            v = parse_silver(repairs)
            if v is None:
                await interaction.response.send_message("Invalid repairs amount.", ephemeral=True)
                return
            s["repairs"] = v
        if discount is not None:
            s["discount"] = int(discount)
        if bags is not None:
            v = parse_silver(bags)
            if v is None:
                await interaction.response.send_message("Invalid silver bags amount.", ephemeral=True)
                return
            s["silver_bags"] = v

        save_split(s)
        mkt, rep, disc, bag = split_money_fields(s)
        br = compute_breakdown(mkt, rep, disc, bag)
        pool = br["pool"]
        cuts = compute_cuts(list(s.get("players") or []), pool)
        preview = ", ".join(
            f"{display_name_for(interaction.guild, uid)} `{format_silver(cut)}`"
            for uid, cut in list(cuts.items())[:12]
        )
        await interaction.response.defer(ephemeral=True)
        await refresh_sheet(bot, s)
        await interaction.followup.send(
            f"Tab `{format_silver_short(mkt)}` → discounted `{format_silver_short(br['discounted_tab'])}` "
            f"(−{disc}%) − repairs `{format_silver_short(rep)}` + bags `{format_silver_short(bag)}` "
            f"= pool **{format_silver(pool)}**\n"
            f"Cuts: {preview or '—'}\n"
            f"Edit again anytime with **Fill / edit money** on the sheet.",
            ephemeral=True,
        )

    @split.command(name="refresh", description="Refresh the split sheet + buttons (Fill / edit money)")
    @app_commands.describe(split_id="Split ID (optional inside the split thread)")
    async def split_refresh(
        interaction: discord.Interaction,
        split_id: str | None = None,
    ) -> None:
        if not await _require_mod(interaction):
            return
        s = resolve_split_for_channel(interaction, split_id)
        if not s:
            await interaction.response.send_message("Split not found.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await refresh_sheet(bot, s)
        if s.get("status") == "OPEN":
            await interaction.followup.send(
                "Sheet refreshed. Use **Fill / edit money** on the post, or open the form here:",
                view=OpenMoneyButton(s["id"]),
                ephemeral=True,
            )
        else:
            await interaction.followup.send("Sheet refreshed.", ephemeral=True)

    @split.command(name="finalize", description="Lock split and credit player balances")
    @app_commands.describe(split_id="Split ID (optional inside the split thread)")
    async def split_finalize(
        interaction: discord.Interaction,
        split_id: str | None = None,
    ) -> None:
        if not await _require_mod(interaction):
            return
        s = resolve_split_for_channel(interaction, split_id)
        if not s:
            await interaction.response.send_message("Split not found.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        _ok, msg = await finalize_split(bot, s, by=interaction.user.id, guild=interaction.guild)
        await interaction.followup.send(msg, ephemeral=True)

    @split.command(name="cancel", description="Void an open split (no balance changes)")
    @app_commands.describe(split_id="Split ID (optional inside the split thread)")
    async def split_cancel(
        interaction: discord.Interaction,
        split_id: str | None = None,
    ) -> None:
        if not await _require_mod(interaction):
            return
        s = resolve_split_for_channel(interaction, split_id)
        if not s:
            await interaction.response.send_message("Split not found.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        _ok, msg = await cancel_split(bot, s)
        await interaction.followup.send(msg, ephemeral=True)

    @split.command(
        name="undo",
        description="Reverse a finalized split (balances may go negative if already paid out)",
    )
    @app_commands.describe(split_id="Split ID (optional inside the split thread)")
    async def split_undo(
        interaction: discord.Interaction,
        split_id: str | None = None,
    ) -> None:
        if not await _require_mod(interaction):
            return
        s = resolve_split_for_channel(interaction, split_id)
        if not s:
            await interaction.response.send_message("Split not found.", ephemeral=True)
            return
        if s.get("status") != "FINALIZED":
            await interaction.response.send_message(
                f"Split is **{s.get('status')}** — only **FINALIZED** splits can be undone.",
                ephemeral=True,
            )
            return
        cuts = {int(k): int(v) for k, v in (s.get("final_cuts") or {}).items() if int(v) > 0}
        n = len(cuts)
        pool = int(s.get("final_pool") or 0)
        await interaction.response.send_message(
            f"Undo this split?\n"
            f"**{s.get('title') or s['id']}** — reverse **{n}** payout(s), pool **`{format_silver(pool)}`**.\n"
            f"Anyone already paid out will go **negative**.",
            view=ConfirmActionView(s["id"], "undo"),
            ephemeral=True,
        )

    @split.command(
        name="undo-all",
        description="Reverse ALL finalized splits in this server (dangerous)",
    )
    async def split_undo_all(interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        if not await _require_mod(interaction):
            return
        n = sum(
            1
            for s in _load_splits()["splits"].values()
            if s.get("guild_id") == interaction.guild.id and s.get("status") == "FINALIZED"
        )
        if n == 0:
            await interaction.response.send_message("No **FINALIZED** splits to undo.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"Undo **all {n} finalized split(s)** in this server?\n"
            f"This reverses every payout — balances can go **negative** if people were already paid.\n"
            f"This cannot be undone automatically.",
            view=ConfirmActionView(str(interaction.guild.id), "undo-all"),
            ephemeral=True,
        )

    bot.tree.add_command(split)

    @bot.tree.command(name="balance", description="Show your silver balance (Mods can look up others)")
    @app_commands.describe(player="Mod only: look up another player's balance")
    async def balance_cmd(
        interaction: discord.Interaction,
        player: discord.Member | None = None,
    ) -> None:
        # No player → always your own balance
        if player is None or player.id == interaction.user.id:
            await _send_balance(interaction, interaction.user)
            return
        # Looking up someone else → Mod only
        if not isinstance(interaction.user, discord.Member) or not _is_mod(interaction.user):
            await interaction.response.send_message(
                "You can only view **your own** balance. Mods can look up others.",
                ephemeral=True,
            )
            return
        await _send_balance(interaction, player)

    @bot.tree.command(name="balance-pay", description="Mod: mark silver paid out (partial or full balance)")
    @app_commands.describe(
        player="Player paid in-game",
        payout="Pay everything, or a custom amount",
        amount="Only for Custom — e.g. 1,25m or 1,250,000",
        note="Optional note",
    )
    @app_commands.choices(
        payout=[
            app_commands.Choice(name="Pay entire balance", value="all"),
            app_commands.Choice(name="Custom amount", value="custom"),
        ]
    )
    async def balance_pay(
        interaction: discord.Interaction,
        player: discord.Member,
        payout: app_commands.Choice[str],
        amount: str | None = None,
        note: str | None = None,
    ) -> None:
        if not await _require_mod(interaction):
            return

        entry = get_balance_entry(player.id)
        bal = int(entry.get("balance") or 0)
        name = display_name_for(interaction.guild, player.id)

        if payout.value == "all":
            if bal <= 0:
                await interaction.response.send_message(
                    f"**{name}** already has balance `0`.",
                    ephemeral=True,
                )
                return
            silver = bal
            pay_note = note or "Paid out in-game (full balance)"
        else:
            if not amount:
                await interaction.response.send_message(
                    "Custom payout needs an **amount** (e.g. `1,25m`).",
                    ephemeral=True,
                )
                return
            silver = parse_silver(amount)
            if silver is None or silver <= 0:
                await interaction.response.send_message(
                    "Invalid amount — use e.g. `1,25m` or `1,250,000`.",
                    ephemeral=True,
                )
                return
            pay_note = note or "Paid out in-game"

        _ok, msg, _bal = pay_balance(player.id, silver, by=interaction.user.id, note=pay_note)
        await interaction.response.send_message(msg, ephemeral=True)

    @bot.tree.command(name="balance-add", description="Mod: manually add silver to a balance")
    @app_commands.describe(
        amount="Amount to add — e.g. 1,25m or 1,250,000",
        player="Who gets the silver (defaults to you)",
        note="Optional note",
    )
    async def balance_add(
        interaction: discord.Interaction,
        amount: str,
        player: discord.Member | None = None,
        note: str | None = None,
    ) -> None:
        if not await _require_mod(interaction):
            return

        silver = parse_silver(amount)
        if silver is None or silver <= 0:
            await interaction.response.send_message(
                "Invalid amount — use e.g. `1,25m` or `1,250,000`.",
                ephemeral=True,
            )
            return

        target = player or interaction.user
        name = display_name_for(interaction.guild, target.id)
        reason = note or "Manual credit"
        new_bal = credit_balance(
            target.id,
            silver,
            reason=reason,
            split_id=None,
            by=interaction.user.id,
        )
        await interaction.response.send_message(
            f"Added `{format_silver(silver)}` to **{name}**.\n"
            f"New balance: `{format_silver(new_bal)}`.",
            ephemeral=True,
        )

    @bot.tree.command(name="balance-remove", description="Mod: remove silver from a balance")
    @app_commands.describe(
        mode="Custom amount or undo their last credit",
        amount="Only for Custom — e.g. 1,25m",
        player="Who to remove from (defaults to you)",
        note="Optional note (Custom only)",
    )
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="Custom amount", value="custom"),
            app_commands.Choice(name="Undo last credit", value="undo_last"),
        ]
    )
    async def balance_remove(
        interaction: discord.Interaction,
        mode: app_commands.Choice[str],
        amount: str | None = None,
        player: discord.Member | None = None,
        note: str | None = None,
    ) -> None:
        if not await _require_mod(interaction):
            return

        target = player or interaction.user
        name = display_name_for(interaction.guild, target.id)

        if mode.value == "undo_last":
            _ok, msg, _bal = undo_last_credit(target.id, by=interaction.user.id)
            if not _ok:
                await interaction.response.send_message(
                    f"**{name}**: {msg}",
                    ephemeral=True,
                )
                return
            await interaction.response.send_message(f"**{name}**: {msg}", ephemeral=True)
            return

        if not amount:
            await interaction.response.send_message(
                "Custom removal needs an **amount** (e.g. `1,25m`).",
                ephemeral=True,
            )
            return
        silver = parse_silver(amount)
        if silver is None or silver <= 0:
            await interaction.response.send_message(
                "Invalid amount — use e.g. `1,25m` or `1,250,000`.",
                ephemeral=True,
            )
            return

        _ok, msg, _bal = remove_balance(
            target.id,
            silver,
            by=interaction.user.id,
            note=note or "Manual removal",
        )
        await interaction.response.send_message(f"**{name}**: {msg}", ephemeral=True)

    @bot.tree.command(name="who-owes", description="Mod: list all non-zero balances (payout checklist)")
    async def who_owes(interaction: discord.Interaction) -> None:
        if not await _require_mod(interaction):
            return

        rows = list_nonzero_balances()
        if not rows:
            await interaction.response.send_message(
                "All clear — every balance is **0**.",
                ephemeral=True,
            )
            return

        owed = [(uid, e) for uid, e in rows if int(e.get("balance") or 0) > 0]
        debt = [(uid, e) for uid, e in rows if int(e.get("balance") or 0) < 0]
        total_owed = sum(int(e.get("balance") or 0) for _, e in owed)
        total_debt = sum(int(e.get("balance") or 0) for _, e in debt)

        lines: list[str] = []
        copy_lines: list[str] = []

        if owed:
            lines.append(f"**Pay out** — `{format_silver(total_owed)}` total ({len(owed)} player(s)):")
            for uid, entry in owed:
                bal = int(entry.get("balance") or 0)
                ign = (get_ign(uid) or entry.get("ign") or "?").strip()
                disc = display_name_for(interaction.guild, uid)
                lines.append(f"• `{ign}` — `{format_silver(bal)}` · {disc}")
                copy_lines.append(f"{ign} {format_silver(bal)}")

        if debt:
            if lines:
                lines.append("")
            lines.append(
                f"**In debt** — `{format_silver(total_debt)}` ({len(debt)} player(s), already over-paid):"
            )
            for uid, entry in debt:
                bal = int(entry.get("balance") or 0)
                ign = (get_ign(uid) or entry.get("ign") or "?").strip()
                disc = display_name_for(interaction.guild, uid)
                lines.append(f"• `{ign}` — `{format_silver(bal)}` · {disc}")

        body = "\n".join(lines)
        if len(body) > 3600:
            body = body[:3600] + "\n…"

        if copy_lines and len(copy_lines) <= 20:
            body += "\n\n**Copy for in-game pay:**\n```\n" + "\n".join(copy_lines) + "\n```"

        await interaction.response.send_message(body[:4000], ephemeral=True)

    @bot.tree.command(name="balance-history", description="Show ledger history (own, or Mod lookup)")
    @app_commands.describe(player="Mod only: look up another player")
    async def balance_history(
        interaction: discord.Interaction,
        player: discord.Member | None = None,
    ) -> None:
        if player is None or player.id == interaction.user.id:
            target = interaction.user
        else:
            if not isinstance(interaction.user, discord.Member) or not _is_mod(interaction.user):
                await interaction.response.send_message(
                    "You can only view **your own** history. Mods can look up others.",
                    ephemeral=True,
                )
                return
            target = player
        entry = get_balance_entry(target.id)
        hist = entry.get("history") or []
        if not hist:
            await interaction.response.send_message("No history yet.", ephemeral=True)
            return
        lines = [f"**{display_name_for(interaction.guild, target.id)}** history:"]
        for h in hist[:15]:
            amt = int(h.get("amount") or 0)
            lines.append(
                f"• `{format_silver(amt)}` — {h.get('reason') or h.get('type')} "
                f"— {(h.get('at') or '')[:19]}"
            )
        await interaction.response.send_message("\n".join(lines)[:2000], ephemeral=True)

    @bot.tree.context_menu(name="Create Loot Split")
    async def ctx_create_split(interaction: discord.Interaction, message: discord.Message) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        if not isinstance(interaction.user, discord.Member) or not _is_mod(interaction.user):
            await interaction.response.send_message("Only **Mod** can create splits.", ephemeral=True)
            return
        # Modal must be the first response — asks for Ganking / Static #2 etc.
        await interaction.response.send_modal(CreateLabelModal(message))

    bot.add_view(SplitSheetView())
