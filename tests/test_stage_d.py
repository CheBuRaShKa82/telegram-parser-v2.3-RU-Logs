import json
import logging
import os
import tempfile
import unittest
from logging.handlers import RotatingFileHandler

from config_store import (
    AppConfig,
    load_config,
    migrate_legacy_options,
    save_config,
)
from logging_setup import setup_logging


class ConfigMigrationTests(unittest.TestCase):
    def test_legacy_options_migrate_to_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            legacy = os.path.join(tmp, "options.txt")
            config_path = os.path.join(tmp, "config.json")
            with open(legacy, "w", encoding="utf-8") as handle:
                handle.write("12345\nsecret_hash\nTrue\nFalse\n")

            migrated = migrate_legacy_options(
                config_path=config_path,
                legacy_path=legacy,
            )
            self.assertIsNotNone(migrated)
            self.assertEqual(migrated.api_id, 12345)
            self.assertEqual(migrated.api_hash, "secret_hash")
            self.assertTrue(migrated.parse_user_id)
            self.assertFalse(migrated.parse_username)
            self.assertTrue(os.path.exists(config_path))
            self.assertTrue(os.path.exists(legacy + ".migrated"))
            self.assertFalse(os.path.exists(legacy))

            loaded = load_config(config_path)
            self.assertEqual(loaded, migrated)

    def test_config_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            expected = AppConfig(
                api_id=99,
                api_hash="abc",
                parse_user_id=False,
                parse_username=True,
            )
            save_config(expected, path)
            self.assertEqual(load_config(path), expected)
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            self.assertEqual(payload["api_id"], 99)


class LoggingTests(unittest.TestCase):
    def test_rotating_handler_is_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "app.log")
            logger = setup_logging(path, max_bytes=1024, backup_count=2)
            matching = [
                handler
                for handler in logger.handlers
                if isinstance(handler, RotatingFileHandler)
                and os.path.abspath(handler.baseFilename) == os.path.abspath(path)
            ]
            self.assertEqual(len(matching), 1)
            self.assertEqual(matching[0].maxBytes, 1024)
            self.assertEqual(matching[0].backupCount, 2)

            logger.info("stage-d-test")
            for handler in matching:
                handler.flush()
                handler.close()
                logger.removeHandler(handler)
            self.assertTrue(os.path.exists(path))


if __name__ == "__main__":
    unittest.main()
