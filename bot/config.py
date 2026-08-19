from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

CONFIG_PATH = Path(__file__).resolve().parent.parent / "current.yaml"

# Discord permission flag names used in YAML → Permissions kwargs
PERM_ALIASES = {
    "read_messages": "read_messages",
    "view_channel": "view_channel",
    "send_messages": "send_messages",
    "read_message_history": "read_message_history",
    "add_reactions": "add_reactions",
    "embed_links": "embed_links",
    "attach_files": "attach_files",
    "external_emojis": "external_emojis",
    "external_stickers": "external_stickers",
    "connect": "connect",
    "speak": "speak",
    "stream": "stream",
    "use_voice_activation": "use_voice_activation",
    "use_application_commands": "use_application_commands",
    "create_public_threads": "create_public_threads",
    "send_messages_in_threads": "send_messages_in_threads",
    "send_polls": "send_polls",
    "administrator": "administrator",
}


def load_config(path: Path | None = None) -> dict[str, Any]:
    with open(path or CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def hex_color(value: str) -> int:
    return int(value.lstrip("#"), 16)


def content_role_names(cfg: dict[str, Any]) -> list[str]:
    return [r["name"] for r in cfg.get("content_roles", [])]


def combat_role_names(cfg: dict[str, Any]) -> list[str]:
    return [r["name"] for r in cfg.get("combat_roles", [])]
