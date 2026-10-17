"""Tests for MySync JSON configuration."""

from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import tempfile
import unittest
from uuid import UUID

from mysync.storage.config import AppConfig, ConfigError, load_config, new_device_id, save_config


class ConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "config.json"
        self.config = AppConfig(
            root_path="/tmp/Рабочие файлы",
            device_id=new_device_id(),
            device_name="Домашний компьютер",
            tcp_port=32001,
            udp_port=32002,
        )

    def test_round_trip_unicode_defaults_and_atomic_replacement(self):
        self.path.write_text("old", encoding="utf-8")
        save_config(self.path, self.config)
        self.assertEqual(load_config(self.path), self.config)
        self.assertEqual(self.config.backup_retention_days, 7)
        self.assertTrue(self.config.auto_sync)
        self.assertIn("Домашний компьютер", self.path.read_text(encoding="utf-8"))
        self.assertEqual(list(self.root.glob(".config.json.*.tmp")), [])

    def test_model_is_immutable_and_device_id_is_uuid(self):
        UUID(self.config.device_id)
        with self.assertRaises(FrozenInstanceError):
            self.config.device_name = "changed"

    def test_new_device_ids_are_unique(self):
        self.assertNotEqual(new_device_id(), new_device_id())

    def test_invalid_model_values(self):
        base = dict(
            root_path="/data", device_id=new_device_id(), device_name="PC",
            tcp_port=1, udp_port=65535,
        )
        cases = (
            {"root_path": ""}, {"device_name": "   "}, {"device_id": "bad"},
            {"tcp_port": 0}, {"tcp_port": 65536}, {"tcp_port": True},
            {"udp_port": "100"}, {"backup_retention_days": 0},
            {"backup_retention_days": True}, {"auto_sync": 1},
        )
        for update in cases:
            with self.subTest(update=update), self.assertRaises(ConfigError):
                AppConfig(**(base | update))

    def test_bad_json_shape_version_and_fields(self):
        valid = {"schema_version": 1, **self.config.__dict__}
        variants = (
            "{bad", json.dumps([]),
            json.dumps(valid | {"schema_version": 2}),
            json.dumps({key: value for key, value in valid.items() if key != "udp_port"}),
            json.dumps(valid | {"extra": 1}),
        )
        for index, content in enumerate(variants):
            with self.subTest(index=index):
                self.path.write_text(content, encoding="utf-8")
                with self.assertRaises(ConfigError):
                    load_config(self.path)

    def test_missing_file_and_parent(self):
        with self.assertRaises(FileNotFoundError):
            load_config(self.path)
        with self.assertRaises(FileNotFoundError):
            save_config(self.root / "missing" / "config.json", self.config)


if __name__ == "__main__":
    unittest.main()
