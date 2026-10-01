# -*- coding: utf-8 -*-
"""Rotating application logging for telegram-parser v2.4."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


LOG_FILE = "app.log"
LOGGER_NAME = "telegram_parser"


def setup_logging(
    log_file: str = LOG_FILE,
    max_bytes: int = 2 * 1024 * 1024,
    backup_count: int = 5,
) -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    resolved = str(Path(log_file).resolve())
    for handler in logger.handlers:
        if isinstance(handler, RotatingFileHandler):
            try:
                if str(Path(handler.baseFilename).resolve()) == resolved:
                    return logger
            except Exception:
                continue

    handler = RotatingFileHandler(
        log_file,
        maxBytes=max(1024, int(max_bytes)),
        backupCount=max(1, int(backup_count)),
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    logger.addHandler(handler)
    return logger


LOGGER = setup_logging()


def log_info(msg: str) -> None:
    LOGGER.info(msg)


def log_ok(msg: str) -> None:
    LOGGER.info("УСПЕХ | %s", msg)


def log_warn(msg: str) -> None:
    LOGGER.warning(msg)


def log_pause(msg: str) -> None:
    LOGGER.info("ПАУЗА | %s", msg)


def log_stop(msg: str) -> None:
    LOGGER.error(msg)
