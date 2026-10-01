# -*- coding: utf-8 -*-
"""Telegram session file management."""

from __future__ import annotations

import os
from pathlib import Path
from typing import List

from logging_setup import log_warn


SESSIONS_DIR = "sessoins"


def ensure_sessions_dir() -> str:
    path = Path(SESSIONS_DIR)
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass

    try:
        for legacy in Path(".").glob("*.session"):
            if not legacy.is_file():
                continue
            destination = path / legacy.name
            if destination.exists():
                continue
            legacy.rename(destination)

        if os.name != "nt":
            for session_path in path.glob("*.session"):
                try:
                    os.chmod(session_path, 0o600)
                except OSError:
                    pass
    except OSError as exc:
        log_warn(f"Не удалось мигрировать session-файлы: {type(exc).__name__}")

    return SESSIONS_DIR


def session_name_from_file(session_file: str) -> str:
    ensure_sessions_dir()
    base = os.path.basename(session_file)
    name = base[:-8] if base.endswith(".session") else base
    return os.path.join(SESSIONS_DIR, name)


def list_session_files() -> List[str]:
    ensure_sessions_dir()
    try:
        return sorted(
            path.name
            for path in Path(SESSIONS_DIR).glob("*.session")
            if path.is_file()
        )
    except OSError as exc:
        log_warn(f"Не удалось прочитать каталог сессий: {type(exc).__name__}")
        return []


def secure_session_file(session_path: str) -> None:
    if os.name == "nt":
        return
    path = session_path
    if not path.endswith(".session"):
        path += ".session"
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
