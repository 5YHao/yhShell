"""Terminal highlight settings defaults, persistence, and strict validation."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from yhshell.store import CONFIG_FILENAME, DEFAULT_SETTINGS, Store, default_config


DEFAULT_RULES = {"error": True, "warning": True, "success": True, "address": True}


class HighlightSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="yhshell-highlight-")
        self.addCleanup(directory.cleanup)
        self.directory = directory.name

    def write_config(self, config: dict) -> None:
        with open(os.path.join(self.directory, CONFIG_FILENAME), "w", encoding="utf-8") as handle:
            json.dump(config, handle)

    def assert_defaults(self, settings: dict) -> None:
        self.assertIs(settings["terminalHighlight"], True)
        self.assertEqual(settings["terminalHighlightRules"], DEFAULT_RULES)
        for value in settings["terminalHighlightRules"].values():
            self.assertIs(value, True)

    def test_old_config_gets_highlight_defaults(self) -> None:
        for config in ({"version": 1}, {"version": 3, "settings": {"fontSize": 17}}):
            with self.subTest(config=config):
                self.write_config(config)
                store = Store(self.directory)
                self.assert_defaults(store.settings)
                if "settings" in config:
                    self.assertEqual(store.settings["fontSize"], 17)

    def test_default_rules_are_independent(self) -> None:
        first = default_config()
        first["settings"]["terminalHighlightRules"]["error"] = False
        self.assert_defaults(default_config()["settings"])
        self.assertEqual(DEFAULT_SETTINGS["terminalHighlightRules"], DEFAULT_RULES)

    def test_save_and_reload_preserves_boolean_choices(self) -> None:
        store = Store(self.directory)
        self.assert_defaults(store.settings)
        for enabled in (False, True):
            rules = {"error": False, "warning": True, "success": False, "address": True}
            with self.subTest(enabled=enabled):
                saved = store.update_settings({"terminalHighlight": enabled, "terminalHighlightRules": rules})
                reloaded = Store(self.directory)
                self.assertIs(reloaded.settings["terminalHighlight"], enabled)
                self.assertEqual(reloaded.settings["terminalHighlightRules"], rules)
                self.assertEqual(reloaded.settings, saved)

    def test_invalid_main_switch_falls_back_to_default(self) -> None:
        store = Store(self.directory)
        for value in (None, 0, 1, 0.0, 1.0, "", "false", "true", [], {}, [False]):
            with self.subTest(value=value):
                normalized = store._normalize({"settings": {"terminalHighlight": value}})
                self.assertIs(normalized["settings"]["terminalHighlight"], True)

    def test_invalid_rule_container_falls_back_to_defaults(self) -> None:
        store = Store(self.directory)
        for value in (None, False, True, 0, 1, "false", [], ["error"]):
            with self.subTest(value=value):
                normalized = store._normalize({"settings": {"terminalHighlightRules": value}})
                self.assertEqual(normalized["settings"]["terminalHighlightRules"], DEFAULT_RULES)

    def test_invalid_rule_values_fall_back_to_defaults(self) -> None:
        store = Store(self.directory)
        for category in DEFAULT_RULES:
            for value in (None, 0, 1, 0.0, 1.0, "", "false", "true", [], {}, [False]):
                with self.subTest(category=category, value=value):
                    normalized = store._normalize({"settings": {"terminalHighlightRules": {category: value}}})
                    self.assert_defaults(normalized["settings"])

    def test_missing_categories_default_and_unknown_categories_are_ignored(self) -> None:
        store = Store(self.directory)
        saved = store.update_settings({"terminalHighlightRules": {"error": False, "unknown": False}})
        expected = {**DEFAULT_RULES, "error": False}
        self.assertEqual(saved["terminalHighlightRules"], expected)
        self.assertEqual(Store(self.directory).settings["terminalHighlightRules"], expected)

    def test_invalid_saved_types_are_normalized_on_load(self) -> None:
        self.write_config({
            "version": 3,
            "settings": {
                "terminalHighlight": "false",
                "terminalHighlightRules": {"error": 0, "warning": False, "success": None, "extra": True},
            },
        })
        store = Store(self.directory)
        self.assertIs(store.settings["terminalHighlight"], True)
        self.assertEqual(store.settings["terminalHighlightRules"], {**DEFAULT_RULES, "warning": False})
        store.save()
        self.assertEqual(Store(self.directory).settings, store.settings)


if __name__ == "__main__":
    unittest.main()
