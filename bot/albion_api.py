from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import aiohttp

from bot.config import load_config

SERVER_BASES = {
    "EU": "https://gameinfo-ams.albiononline.com/api/gameinfo/",
    "Americas": "https://gameinfo.albiononline.com/api/gameinfo/",
    "Asia": "https://gameinfo-sgp.albiononline.com/api/gameinfo/",
}

USER_AGENT = "GankChillDiscordBot/1.0 (IGN registration verify)"


@dataclass
class VerifyResult:
    ok: bool
    message: str
    player_id: str | None = None
    canonical_name: str | None = None


def _verification_settings() -> dict[str, Any]:
    cfg = load_config().get("ign_verification") or {}
    return {
        "enabled": bool(cfg.get("enabled", True)),
        "timeout_seconds": float(cfg.get("timeout_seconds", 12)),
    }


async def verify_ign(ign: str, server: str) -> VerifyResult:
    """Check that IGN exists on the chosen Albion server (gameinfo search API)."""
    settings = _verification_settings()
    name = (ign or "").strip()
    if not name:
        return VerifyResult(False, "IGN can't be empty.")

    if not settings["enabled"]:
        return VerifyResult(True, "Verification skipped.", canonical_name=name)

    base = SERVER_BASES.get(server)
    if not base:
        return VerifyResult(False, f"Unknown Albion server **{server}**.")

    url = f"{base}search?q={quote(name)}"
    timeout = aiohttp.ClientTimeout(total=settings["timeout_seconds"])
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 404:
                    return VerifyResult(
                        False,
                        f"**{name}** was not found on **{server}**.\n"
                        "Check spelling and that you picked the right server (EU / Americas / Asia).",
                    )
                if resp.status != 200:
                    return VerifyResult(
                        False,
                        f"Albion lookup failed (HTTP {resp.status}). Try again in a minute.",
                    )
                data = await resp.json()
    except (aiohttp.ClientError, TimeoutError, asyncio.TimeoutError):
        return VerifyResult(
            False,
            "Couldn't reach the Albion API — try again in a minute.",
        )

    target = name.casefold()
    for player in data.get("players") or []:
        found = (player.get("Name") or "").strip()
        if found.casefold() == target:
            return VerifyResult(
                True,
                "OK",
                player_id=str(player.get("Id") or "") or None,
                canonical_name=found,
            )

    return VerifyResult(
        False,
        f"**{name}** was not found on **{server}**.\n"
        "Use your **exact** in-game name — random names won't work.",
    )
