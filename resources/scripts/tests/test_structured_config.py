import json
import os
import tempfile
import tomllib
import unittest
from pathlib import Path

from envy.structured_config import contains, update_file


class StructuredConfigTests(unittest.TestCase):
    def test_contains_ignores_object_key_order_but_preserves_list_order(self):
        self.assertTrue(contains({"a": {"x": 1}}, {"a": {"x": 1, "y": 2}}))
        self.assertTrue(contains({"items": [{"x": 1, "y": 2}]}, {"items": [{"y": 2, "x": 1}]}))
        self.assertFalse(contains({"items": [1, 2]}, {"items": [2, 1]}))
        self.assertFalse(contains({"items": [1]}, {"items": [1, 2]}))

    def test_json_merge_preserves_runtime_fields_and_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "settings.json"
            target.write_text(json.dumps({"runtime": True, "nested": {"keep": 1, "set": "old"}}))
            target.chmod(0o640)
            self.assertTrue(update_file(target, {"nested": {"set": "new"}}))
            self.assertEqual(json.loads(target.read_text()), {"runtime": True, "nested": {"keep": 1, "set": "new"}})
            self.assertEqual(target.stat().st_mode & 0o777, 0o640)

    def test_toml_merge_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "config.toml"
            target.write_text('[runtime]\nkeep = true\n\n[model]\nname = "old"\n')
            update_file(target, {"model": {"name": "new"}, "tools": ["a", "b"]}, format="toml")
            self.assertEqual(
                tomllib.loads(target.read_text()),
                {"runtime": {"keep": True}, "model": {"name": "new"}, "tools": ["a", "b"]},
            )

    def test_parse_failure_keeps_original(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "settings.json"
            target.write_text("{broken")
            with self.assertRaises(ValueError):
                update_file(target, {"new": True})
            self.assertEqual(target.read_text(), "{broken")

    def test_store_link_is_replaced_with_writable_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.json"
            source.write_text('{"existing": 1}')
            target = root / "settings.json"
            target.symlink_to(source)
            update_file(target, {"declared": 2})
            self.assertFalse(target.is_symlink())
            self.assertEqual(json.loads(target.read_text()), {"existing": 1, "declared": 2})
            self.assertTrue(os.access(target, os.W_OK))


if __name__ == "__main__":
    unittest.main()
