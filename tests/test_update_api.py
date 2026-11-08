"""Update bridge/settings tests: no external network or real browser."""
from __future__ import annotations

import tempfile
import threading
import unittest
from unittest.mock import patch

from yhshell.api import Api
from yhshell.store import Store


class UpdateApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.api = Api(self.store, auto_sync=False, auto_update=False)
        self.addCleanup(self.api._stop_background)

    def available(self, version="0.2.0"):
        return {"ok": True, "status": "available", "currentVersion": "0.1.0",
                "latestVersion": version, "lastSuccessAt": 100,
                "release": {"version": version, "url": "https://github.com/5YHao/yhShell/releases/tag/v" + version,
                            "notes": "<script>not html</script>", "publishedAt": "2026-11-06T00:00:00Z"}}

    def test_settings_defaults_roundtrip_and_invalid_boolean(self):
        self.assertTrue(self.store.settings["updateAutoCheck"])
        self.api.save_settings({"updateAutoCheck": False, "updateIgnoredVersion": "0.2.0"})
        reloaded = Store(self.temp.name)
        self.assertFalse(reloaded.settings["updateAutoCheck"])
        self.assertEqual(reloaded.settings["updateIgnoredVersion"], "0.2.0")
        self.api.save_settings({"updateAutoCheck": "false"})
        self.assertTrue(self.store.settings["updateAutoCheck"])

    def test_manual_check_async_single_flight_and_event(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def check(force=False):
            self.assertTrue(force)
            entered.set()
            release.wait(3)
            return self.available()
        original_push = self.api._push_event
        def push(event):
            original_push(event)
            finished.set()
        with patch.object(self.api._updater, "check", side_effect=check), patch.object(self.api, "_push_event", side_effect=push):
            try:
                self.assertTrue(self.api.check_updates()["started"])
                self.assertTrue(entered.wait(2))
                self.assertFalse(self.api.check_updates()["started"])
                self.assertEqual(self.api.update_status()["update"]["status"], "checking")
            finally:
                release.set()
                self.assertTrue(finished.wait(3))
        event = self.api.poll()["events"][0]
        self.assertEqual(event["type"], "update_check")
        self.assertTrue(event["manual"])
        self.assertEqual(event["update"]["status"], "available")

    def test_ignored_version_does_not_ignore_higher_release(self):
        with patch.object(self.api._updater, "state", return_value=self.available()):
            self.assertFalse(self.api.ignore_update("9.0.0")["ok"])
            self.assertTrue(self.api.ignore_update("0.2.0")["ok"])
            self.assertTrue(self.api.update_status()["update"]["ignored"])
        with patch.object(self.api._updater, "state", return_value=self.available("0.3.0")):
            self.assertFalse(self.api.update_status()["update"]["ignored"])

    def test_open_uses_only_validated_cached_official_url(self):
        with patch.object(self.api._updater, "state", return_value=self.available()), patch("yhshell.api.webbrowser.open", return_value=True) as browser:
            self.assertTrue(self.api.open_update_release()["ok"])
            browser.assert_called_once_with(self.available()["release"]["url"], new=2)
        invalid = self.available()
        invalid["release"]["url"] = "https://evil.example/releases/tag/v0.2.0"
        with patch.object(self.api._updater, "state", return_value=invalid), patch("yhshell.api.webbrowser.open") as browser:
            self.assertFalse(self.api.open_update_release()["ok"])
            browser.assert_not_called()

    def test_disabled_scheduler_never_checks(self):
        self.store.update_settings({"updateAutoCheck": False})
        # Complete two scheduler waits without sleeping; end before third wait.
        with patch.object(self.api._update_stop, "wait", side_effect=[False, True]), patch.object(self.api, "_start_update_check") as start:
            self.api._update_loop()
            start.assert_not_called()

    def test_manual_cooldown_is_not_replaced_by_cached_available(self):
        finished = threading.Event()
        result = {**self.available(), "status": "skipped", "reason": "server_cooldown"}
        def push(event):
            self.assertTrue(event["manual"])
            self.assertEqual(event["update"]["reason"], "server_cooldown")
            self.assertEqual(event["update"]["status"], "skipped")
            finished.set()
        with patch.object(self.api._updater, "check", return_value=result), patch.object(self.api._updater, "state", return_value=self.available()), patch.object(self.api, "_push_event", side_effect=push):
            self.api.check_updates()
            self.assertTrue(finished.wait(3))

    def test_cached_auto_reminder_is_emitted_only_once_per_version(self):
        result = {**self.available(), "status": "skipped", "reason": "recent_success"}
        def inline_thread(*, target, **kwargs):
            class Inline:
                def start(self):
                    target()
            return Inline()
        with patch.object(self.api._updater, "check", return_value=result), patch.object(self.api._updater, "state", return_value=self.available()), patch("yhshell.api.threading.Thread", side_effect=inline_thread):
            self.api._start_update_check(manual=False)
            self.api._start_update_check(manual=False)
        events = self.api.poll()["events"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["update"]["status"], "available")

    def test_worker_exception_returns_error_and_releases_single_flight(self):
        finished = threading.Event()
        original_push = self.api._push_event
        def push(event):
            original_push(event)
            finished.set()
        with patch.object(self.api._updater, "check", side_effect=RuntimeError("unexpected")), patch.object(self.api, "_push_event", side_effect=push):
            self.api.check_updates()
            self.assertTrue(finished.wait(3))
        self.assertFalse(self.api._update_running)
        self.assertEqual(self.api.poll()["events"][0]["update"]["status"], "error")


if __name__ == "__main__":
    unittest.main()
