# -*- coding: utf-8 -*-
"""Rotating application logging for telegram-parser v2.4."""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path


LOG_FILE = "app.log"
LOGGER_NAME = "telegram_parser"


class SecureRotatingFileHandler(RotatingFileHandler):
    """Rotating log handler that creates every POSIX log file as 0600."""

    def _open(self):
        if os.name == "nt":
            return super()._open()

        flags = os.O_WRONLY | os.O_CREAT
        if "a" in self.mode:
            flags |= os.O_APPEND
        else:
            flags |= os.O_TRUNC
        fd = os.open(self.baseFilename, flags, 0o600)
        return os.fdopen(
            fd,
            self.mode,
            encoding=self.encoding,
            errors=self.errors,
        )


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

    handler = SecureRotatingFileHandler(
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


LOGGER = logging.getLogger(LOGGER_NAME)
LOGGER.setLevel(logging.INFO)
LOGGER.propagate = False


def _logger() -> logging.Logger:
    # Keep imports side-effect free: app.log is created on first real log write.
    return setup_logging()


def log_info(msg: str) -> None:
    _logger().info(msg)


def log_ok(msg: str) -> None:
    _logger().info("УСПЕХ | %s", msg)


def log_warn(msg: str) -> None:
    _logger().warning(msg)


def log_pause(msg: str) -> None:
    _logger().info("ПАУЗА | %s", msg)


def log_stop(msg: str) -> None:
    _logger().error(msg)
