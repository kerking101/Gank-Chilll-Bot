from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "igns.json"


def _load() -> dict[str, Any]:
    if not DATA_PATH.exists():
        return {}
    with open(DATA_PATH, encoding="utf-8") as f:
        raw = json.load(f)
    # migrate old "userid": "ign" strings
    out: dict[str, Any] = {}
    for uid, value in raw.items():
        if isinstance(value, str):
            out[uid] = {"ign": value, "server": ""}
        else:
            out[uid] = value
    return out


def _save(data: dict[str, Any]) -> None:
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def get_registration(user_id: int) -> dict[str, str] | None:
    entry = _load().get(str(user_id))
    if not entry:
        return None
    return {"ign": entry.get("ign", ""), "server": entry.get("server", "")}


def get_ign(user_id: int) -> str | None:
    reg = get_registration(user_id)
    return reg["ign"] if reg and reg.get("ign") else None


def set_registration(
    user_id: int,
    *,
    ign: str,
    server: str,
    albion_id: str | None = None,
) -> None:
    data = _load()
    entry: dict[str, Any] = {
        "ign": ign.strip(),
        "server": server.strip(),
    }
    if albion_id:
        entry["albion_id"] = albion_id
    data[str(user_id)] = entry
    _save(data)


def has_ign(user_id: int) -> bool:
    reg = get_registration(user_id)
    if not reg:
        return False
    return bool(reg.get("ign") and reg.get("server"))


def clear_registration(user_id: int) -> bool:
    """Remove stored Albion IGN/server. Returns True if an entry existed."""
    data = _load()
    if str(user_id) not in data:
        return False
    del data[str(user_id)]
    _save(data)
    return True


def format_nick(ign: str, server: str = "") -> str:
    return ign.strip()[:32]  # Discord nickname limit — IGN only
