# -*- coding: utf-8 -*-
"""Canonical application configuration for telegram-parser v2.4."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import List, Optional


CONFIG_PATH = "config.json"
LEGACY_OPTIONS_PATH = "options.txt"


@dataclass
class AppConfig:
    api_id: Optional[int] = None
    api_hash: str = ""
    parse_user_id: bool = True
    parse_username: bool = True


def _secure_file(path: str) -> None:
    if os.name != "nt":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def _parse_bool(value: str, default: bool = True) -> bool:
    value = str(value).strip().lower()
    if value in ("true", "1", "yes", "y", "да", "д"):
        return True
    if value in ("false", "0", "no", "n", "нет", "н"):
        return False
    return default


def _json_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return _parse_bool(value, default)
    return default


def _backup_broken_config(path: str) -> Optional[str]:
    if not os.path.exists(path):
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = f"{path}.broken-{stamp}"
    suffix = 1
    while os.path.exists(backup):
        backup = f"{path}.broken-{stamp}-{suffix}"
        suffix += 1
    try:
        os.replace(path, backup)
        _secure_file(backup)
        return backup
    except OSError:
        return None


def _from_legacy_lines(lines: List[str]) -> AppConfig:
    values = [str(line).strip() for line in lines]
    while len(values) < 4:
        values.append("")
    raw_id = values[0]
    api_id: Optional[int] = None
    if raw_id not in ("", "NONEID"):
        try:
            api_id = int(raw_id)
        except ValueError:
            api_id = None
    api_hash = "" if values[1] in ("", "NONEHASH") else values[1]
    return AppConfig(
        api_id=api_id,
        api_hash=api_hash,
        parse_user_id=_parse_bool(values[2], True),
        parse_username=_parse_bool(values[3], True),
    )


def save_config(config: AppConfig, path: str = CONFIG_PATH) -> None:
    payload = asdict(config)
    tmp_path = path + ".tmp"

    if os.name != "nt":
        fd = os.open(
            tmp_path,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    else:
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")

    os.replace(tmp_path, path)
    _secure_file(path)


def migrate_legacy_options(
    config_path: str = CONFIG_PATH,
    legacy_path: str = LEGACY_OPTIONS_PATH,
) -> Optional[AppConfig]:
    if os.path.exists(config_path) or not os.path.exists(legacy_path):
        return None
    with open(legacy_path, "r", encoding="utf-8") as handle:
        config = _from_legacy_lines(handle.readlines())
    save_config(config, config_path)
    migrated_path = legacy_path + ".migrated"
    try:
        os.replace(legacy_path, migrated_path)
        _secure_file(migrated_path)
    except OSError:
        pass
    return config


def load_config(path: str = CONFIG_PATH) -> AppConfig:
    if path == CONFIG_PATH:
        migrate_legacy_options()

    if not os.path.exists(path):
        config = AppConfig()
        save_config(config, path)
        return config

    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
        if not isinstance(raw, dict):
            raise ValueError("config root must be an object")
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        # Never silently destroy the only copy of API credentials.
        backup = _backup_broken_config(path)
        if backup is None and os.path.exists(path):
            # If backup failed, leave the broken config untouched.
            return AppConfig()
        config = AppConfig()
        save_config(config, path)
        return config

    raw_api_id = raw.get("api_id")
    api_id: Optional[int] = None
    if raw_api_id not in (None, "", "NONEID"):
        try:
            api_id = int(raw_api_id)
        except (TypeError, ValueError):
            api_id = None

    config = AppConfig(
        api_id=api_id,
        api_hash=str(raw.get("api_hash") or ""),
        parse_user_id=_json_bool(raw.get("parse_user_id", True), True),
        parse_username=_json_bool(raw.get("parse_username", True), True),
    )
    _secure_file(path)
    return config


def legacy_options(config: Optional[AppConfig] = None) -> List[str]:
    """Compatibility view for the old index-based code."""
    config = config or load_config()
    return [
        (str(config.api_id) if config.api_id is not None else "NONEID") + "\n",
        (config.api_hash if config.api_hash else "NONEHASH") + "\n",
        ("True" if config.parse_user_id else "False") + "\n",
        ("True" if config.parse_username else "False") + "\n",
    ]


def save_legacy_options(lines: List[str]) -> AppConfig:
    config = _from_legacy_lines(lines)
    save_config(config)
    return config
