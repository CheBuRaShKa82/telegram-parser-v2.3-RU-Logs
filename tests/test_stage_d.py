import json
import logging
import os
import tempfile
import glob
import stat
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

    def test_broken_config_is_backed_up_before_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write('{"api_id": 123, "api_hash": "secret"')

            loaded = load_config(path)
            self.assertEqual(loaded, AppConfig())
            backups = glob.glob(path + ".broken-*")
            self.assertEqual(len(backups), 1)
            with open(backups[0], "r", encoding="utf-8") as handle:
                self.assertIn('"api_hash": "secret"', handle.read())

    def test_non_object_json_is_backed_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(["not", "an", "object"], handle)

            self.assertEqual(load_config(path), AppConfig())
            self.assertEqual(len(glob.glob(path + ".broken-*")), 1)

    def test_string_false_is_parsed_as_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "api_id": 1,
                        "api_hash": "hash",
                        "parse_user_id": "false",
                        "parse_username": "TRUE",
                    },
                    handle,
                )

            loaded = load_config(path)
            self.assertFalse(loaded.parse_user_id)
            self.assertTrue(loaded.parse_username)


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
            if os.name != "nt":
                self.assertEqual(
                    stat.S_IMODE(os.stat(path).st_mode),
                    0o600,
                )


if __name__ == "__main__":
    unittest.main()
