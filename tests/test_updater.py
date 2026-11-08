"""Deterministic updater tests: no requests ever reach a real network."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from email.utils import formatdate
from unittest.mock import Mock, patch

import requests

from yhshell import __version__
from yhshell.updater import Updater, validate_release_url


class UpdaterTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="yhshell-update-")
        self.addCleanup(directory.cleanup)
        self.directory = directory.name
        self.clock = patch("yhshell.updater.time.time", return_value=1800000000.0).start()
        self.addCleanup(patch.stopall)
        self.session_type = patch("yhshell.updater.requests.Session").start()
        self.session = self.session_type.return_value.__enter__.return_value
        self.updater = Updater(self.directory, "1.2.3")

    def response(self, status: int = 200, version: str = "v1.3.0", headers: dict | None = None, **changes: object) -> Mock:
        response = Mock(status_code=status)
        response.headers = requests.structures.CaseInsensitiveDict(headers or {})
        payload = {"tag_name": version, "html_url": f"https://github.com/5YHao/yhShell/releases/tag/{version}",
                   "body": "Public release notes", "published_at": "2026-01-01T00:00:00Z",
                   "draft": False, "prerelease": False, **changes}
        response.json.return_value = payload
        self.session.get.return_value = response
        return response

    def write_cache(self, value: object) -> None:
        with open(self.updater.path, "w", encoding="utf-8") as handle:
            json.dump(value, handle)

    def test_construction_state_and_default_version_do_not_use_network(self) -> None:
        updater = Updater(self.directory)
        self.assertEqual(updater.state()["currentVersion"], __version__)
        self.assertEqual(updater.state()["status"], "skipped")
        self.assertIsNone(updater.state()["lastSuccessAt"])
        self.session_type.assert_not_called()
        self.assertFalse(os.path.exists(updater.path))

    def test_success_transport_contract_and_defensive_snapshot(self) -> None:
        self.response(headers={"ETag": '"release-1"'})
        result = self.updater.check()
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["latestVersion"], "1.3.0")
        self.assertEqual(result["lastSuccessAt"], self.clock.return_value)
        self.assertFalse(self.session.trust_env)
        args, kwargs = self.session.get.call_args
        self.assertEqual(args, ("https://api.github.com/repos/5YHao/yhShell/releases/latest",))
        self.assertEqual(kwargs["timeout"], (3, 8))
        self.assertFalse(kwargs["allow_redirects"])
        self.assertNotIn("Authorization", kwargs["headers"])
        self.assertEqual(kwargs["headers"]["User-Agent"], "yhShell/1.2.3")
        result["release"]["notes"] = "mutated"
        self.assertEqual(self.updater.state()["release"]["notes"], "Public release notes")
        json.dumps(self.updater.state(), allow_nan=False)
        with open(self.updater.path, encoding="utf-8") as handle:
            cache = json.load(handle)
        self.assertEqual(cache["etag"], '"release-1"')
        self.assertNotIn("currentVersion", cache)
        self.assertEqual(os.listdir(self.directory), ["update-cache.json"])

    def test_cache_etag_304_and_success_throttle(self) -> None:
        self.response(headers={"ETag": 'W/"release"'})
        self.updater.check()
        self.assertEqual(self.updater.check()["reason"], "recent_success")
        self.assertEqual(self.updater.state()["status"], "available")
        self.assertEqual(self.session.get.call_count, 1)
        self.clock.return_value += 86400
        self.updater = Updater(self.directory, "1.3.0")
        self.assertEqual(self.updater.state()["status"], "latest")
        self.response(304)
        result = self.updater.check()
        self.assertEqual(result["status"], "latest")
        self.assertEqual(self.session.get.call_args.kwargs["headers"]["If-None-Match"], 'W/"release"')
        self.assertEqual(result["lastSuccessAt"], self.clock.return_value)

    def test_force_bypasses_success_and_failure_intervals(self) -> None:
        self.response()
        self.updater.check()
        self.session.get.side_effect = requests.Timeout("private proxy token should not leak")
        result = self.updater.check(force=True)
        self.assertEqual(result["status"], "error")
        self.assertNotIn("private", json.dumps(result))
        self.assertEqual(result["error"], "无法连接 GitHub，请检查网络后重试")
        self.assertEqual(self.updater.check()["reason"], "failure_backoff")
        self.session.get.side_effect = None
        self.response()
        self.assertTrue(self.updater.check(force=True)["ok"])
        self.assertEqual(self.session.get.call_count, 3)

    def test_failure_backoff_expires(self) -> None:
        self.session.get.side_effect = requests.ConnectionError("secret")
        self.updater.check()
        self.clock.return_value += 3599
        self.assertEqual(self.updater.check()["status"], "skipped")
        self.clock.return_value += 1
        self.updater.check()
        self.assertEqual(self.session.get.call_count, 2)

    def test_rate_limit_retry_after_survives_restart_and_force(self) -> None:
        self.response(429, headers={"Retry-After": "7200", "X-RateLimit-Reset": str(self.clock.return_value + 3600)})
        self.assertEqual(self.updater.check()["status"], "error")
        self.updater = Updater(self.directory, "1.2.3")
        self.assertEqual(self.updater.check(force=True)["reason"], "server_cooldown")
        self.assertEqual(self.session.get.call_count, 1)
        self.clock.return_value += 7200
        self.response()
        self.assertEqual(self.updater.check(force=True)["status"], "available")

    def test_rate_limit_http_date_reset_and_missing_headers(self) -> None:
        for headers, wait in (({"Retry-After": formatdate(self.clock.return_value + 600, usegmt=True)}, 600),
                              ({"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(self.clock.return_value + 800)}, 800),
                              ({}, 3600)):
            with self.subTest(headers=headers):
                with tempfile.TemporaryDirectory() as directory:
                    updater = Updater(directory)
                    self.response(403, headers=headers)
                    self.assertFalse(updater.check()["ok"])
                    self.assertEqual(updater.check(force=True)["reason"], "server_cooldown")
                    self.clock.return_value += wait
                    self.response()
                    self.assertTrue(updater.check(force=True)["ok"])

    def test_expired_server_cooldown_does_not_disable_new_fallback(self) -> None:
        self.response(429, headers={"Retry-After": "60"})
        self.updater.check()
        self.clock.return_value += 60
        self.response(429)
        self.assertEqual(self.updater.check(force=True)["status"], "error")
        self.assertEqual(self.updater.check(force=True)["reason"], "server_cooldown")
        self.assertEqual(self.session.get.call_count, 2)

    def test_cache_sanitization_removes_extra_fields_and_invalid_etag(self) -> None:
        self.response()
        self.updater.check()
        with open(self.updater.path, encoding="utf-8") as handle:
            cache = json.load(handle)
        cache.update(etag="bad\r\nInjected: token", token="must not persist")
        cache["release"]["privateField"] = "must not persist"
        self.write_cache(cache)
        updater = Updater(self.directory, "1.2.3")
        self.assertNotIn("privateField", updater.state()["release"])
        self.response()
        updater.check(force=True)
        self.assertNotIn("If-None-Match", self.session.get.call_args.kwargs["headers"])
        with open(updater.path, encoding="utf-8") as handle:
            sanitized = handle.read()
        self.assertNotIn("must not persist", sanitized)
        self.assertNotIn("Injected", sanitized)

    def test_failure_backoff_survives_restart(self) -> None:
        self.session.get.side_effect = requests.Timeout()
        self.updater.check()
        updater = Updater(self.directory, "1.2.3")
        self.assertEqual(updater.check()["reason"], "failure_backoff")
        self.assertEqual(self.session.get.call_count, 1)

    def test_exhausted_limit_on_success_also_blocks_manual(self) -> None:
        self.response(headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(self.clock.return_value + 600)})
        self.assertTrue(self.updater.check()["ok"])
        self.assertEqual(self.updater.check(force=True)["reason"], "server_cooldown")

    def test_no_release_404_and_304_without_cache(self) -> None:
        self.response(304)
        self.assertFalse(self.updater.check()["ok"])
        self.response(404)
        result = self.updater.check(force=True)
        self.assertEqual(result["status"], "no_release")
        self.assertIsNone(result["release"])
        self.assertIsNone(result["latestVersion"])
        self.assertEqual(Updater(self.directory).state()["status"], "no_release")

    def test_versions_are_numeric_stable_and_v_optional(self) -> None:
        for version, expected in (("1.2.3", "latest"), ("v1.2.2", "ahead"), ("1.10.0", "available"),
                                  ("v2.0.0", "available")):
            with self.subTest(version=version):
                self.response(version=version)
                self.assertEqual(self.updater.check(force=True)["status"], expected)
        for version in ("1.2", "1.2.3.4", "1.2.3-rc1", "1.2.3+build", "01.2.3", "V1.2.3", " 1.2.3", "١.2.3", None):
            with self.subTest(version=version):
                self.response(version=version)
                self.assertEqual(self.updater.check(force=True)["status"], "error")
                with self.assertRaises(ValueError):
                    Updater(self.directory, version)

    def test_invalid_releases_redirects_and_json_do_not_replace_good_cache(self) -> None:
        self.response()
        self.updater.check()
        for changes in ({"draft": True}, {"prerelease": True}, {"body": []},
                        {"html_url": "https://evil.test/"}, {"published_at": {}}, {"tag_name": "nightly"}):
            with self.subTest(changes=changes):
                self.response(**changes)
                self.assertFalse(self.updater.check(force=True)["ok"])
                self.assertEqual(self.updater.state()["release"]["version"], "1.3.0")
        response = self.response()
        response.json.side_effect = ValueError("private payload")
        self.assertFalse(self.updater.check(force=True)["ok"])
        self.response(302, headers={"Location": "https://evil.test/"})
        self.assertFalse(self.updater.check(force=True)["ok"])

    def test_corrupt_and_foreign_caches_are_tolerated_and_sanitized(self) -> None:
        for value in ([], None, {"repo": "other/repo"},
                      {"repo": "5YHao/yhShell", "release": {"version": "1.2.3", "url": "https://evil.test/"},
                       "etag": "bad\r\nheader", "lastSuccessAt": float("inf"), "lastFailureAt": True,
                       "retryAfterUntil": 10**99}):
            self.write_cache(value)
            updater = Updater(self.directory)
            self.assertIsNone(updater.state()["release"])
            self.assertIsNone(updater.state()["lastSuccessAt"])
            self.response()
            self.assertTrue(updater.check()["ok"])
            self.assertNotIn("If-None-Match", self.session.get.call_args.kwargs["headers"])
        with open(self.updater.path, "w", encoding="utf-8") as handle:
            handle.write("{broken JSON")
        self.assertEqual(Updater(self.directory).state()["status"], "skipped")

    def test_atomic_cache_failure_is_best_effort(self) -> None:
        self.response()
        with patch("yhshell.updater.os.replace", side_effect=OSError("read-only")):
            self.assertEqual(self.updater.check()["status"], "available")
        self.assertEqual(os.listdir(self.directory), [])

    def test_concurrent_check_is_nonblocking_and_has_no_second_request(self) -> None:
        entered, finish = threading.Event(), threading.Event()
        response = self.response()

        def get(*args: object, **kwargs: object) -> Mock:
            entered.set()
            if not finish.wait(5):
                raise AssertionError("test check did not finish")
            return response

        self.session.get.side_effect = get
        results = []
        worker = threading.Thread(target=lambda: results.append(self.updater.check()))
        worker.start()
        try:
            self.assertTrue(entered.wait(5))
            self.assertEqual(self.updater.check(force=True)["status"], "busy")
            self.assertEqual(self.session.get.call_count, 1)
        finally:
            finish.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results[0]["status"], "available")


class ReleaseUrlTests(unittest.TestCase):
    def test_only_exact_official_repository_https_tag_urls(self) -> None:
        self.assertTrue(validate_release_url("https://github.com/5YHao/yhShell/releases/tag/v1.2.3"))
        self.assertTrue(validate_release_url("https://github.com/owner/repo/releases/tag/1.0.0", "owner/repo"))
        for url in (None, "", "http://github.com/5YHao/yhShell/releases/tag/v1.0.0",
                    "https://github.com:443/5YHao/yhShell/releases/tag/v1.0.0",
                    "https://user@github.com/5YHao/yhShell/releases/tag/v1.0.0",
                    "https://github.com.evil.test/5YHao/yhShell/releases/tag/v1.0.0",
                    "https://github.com/other/yhShell/releases/tag/v1.0.0",
                    "https://github.com/5YHao/yhShell/releases/tag/",
                    "https://github.com/5YHao/yhShell/releases/latest",
                    "https://github.com/5YHao/yhShell/releases/tag/../evil",
                    "https://github.com/5YHao/yhShell/releases/tag/%2e%2e/evil",
                    "https://github.com/5YHao/yhShell/releases/tag/v1?token=x",
                    "https://github.com/5YHao/yhShell/releases/tag/v1#fragment",
                    "https://github.com/5YHao/yhShell/releases/tag/%0aevil",
                    "https://github.com/5YHao/yhShell/releases/tag/a\\b"):
            with self.subTest(url=url):
                self.assertFalse(validate_release_url(url))

    def test_repository_cannot_change_api_host_or_path(self) -> None:
        for repo in ("evil.test/x/y", "../evil", "owner/..", "owner/repo?x", "https://evil.test"):
            with self.assertRaises(ValueError):
                Updater("unused", repo=repo)


if __name__ == "__main__":
    unittest.main()
