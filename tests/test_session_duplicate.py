"""Session duplication keeps live credentials private and creates a fresh shell."""

from __future__ import annotations

import json
import tempfile
import unittest
from unittest.mock import Mock, patch

from yhshell.api import Api
from yhshell.ssh import Session
from yhshell.store import Store


class SessionDuplicateTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="yhshell-duplicate-")
        self.addCleanup(directory.cleanup)
        self.store = Store(directory.name)
        self.api = Api(self.store, auto_sync=False)
        self.addCleanup(self.api.shutdown_sessions)
        # Keep real profile ownership, public info and buffers, but never open SSH.
        self.session_class = self.enterContext(
            patch("yhshell.api.Session", side_effect=self._new_session)
        )

    @staticmethod
    def _new_session(*args, **kwargs) -> Session:
        session = Session(*args, **kwargs)
        session.start = Mock()
        return session

    def _connect(self, auth: str = "password") -> Session:
        result = self.api.connect({"profile": {
            "name": "Unsaved shell",
            "host": "example.test",
            "port": 2222,
            "username": "tester",
            "auth": auth,
            "keyPath": "/private/test-key",
            "password": "unsaved-password-secret",
            "passphrase": "unsaved-passphrase-secret",
        }})
        self.assertTrue(result["ok"], result)
        return self.api._sessions[result["session"]["id"]]

    def test_unsaved_credentials_retained_without_host_store_lookup(self) -> None:
        for auth in ("password", "key", "agent"):
            with self.subTest(auth=auth):
                source = self._connect(auth)
                expected_hosts = self.store.public_state()["hosts"]
                self.assertEqual(expected_hosts, [])
                # Duplication must not rebuild credentials from any saved host.
                with (
                    patch.object(self.store, "get_host", side_effect=AssertionError("host lookup")) as lookup,
                    patch.object(self.store, "host_secret", side_effect=AssertionError("secret lookup")) as secret,
                    patch.object(self.store, "touch_host", side_effect=AssertionError("host touch")) as touch,
                    patch.object(self.store, "upsert_host", side_effect=AssertionError("host save")) as save,
                ):
                    result = self.api.duplicate_session(source.id)
                self.assertTrue(result["ok"], result)
                duplicate = self.api._sessions[result["session"]["id"]]
                self.assertEqual(duplicate.profile, source.profile)
                self.assertIsNot(duplicate.profile, source.profile)
                self.assertEqual((duplicate.cols, duplicate.rows), (120, 32))
                self.assertEqual(result, {
                    "ok": True, "session": duplicate.info(), "hosts": expected_hosts,
                })
                for method in (lookup, secret, touch, save):
                    method.assert_not_called()
                serialized = json.dumps(result)
                for private in ("password", "passphrase", "keyPath",
                                source.profile["password"], source.profile["passphrase"]):
                    self.assertNotIn(private, serialized)
                duplicate.start.assert_called_once_with()
                self.assertEqual(duplicate._settings_provider, self.api._transfer_settings)

    def test_deleted_saved_host_does_not_prevent_duplication(self) -> None:
        source = self._connect()
        host_id = self.store.upsert_host({
            "name": "Unsaved shell", "host": "example.test", "port": 2222,
            "username": "tester", "auth": "password",
        })
        source.profile["id"] = host_id
        self.store.delete_host(host_id)
        self.assertIsNone(self.store.get_host(host_id))

        result = self.api.duplicate_session(source.id)

        self.assertTrue(result["ok"], result)
        duplicate = self.api._sessions[result["session"]["id"]]
        self.assertEqual(duplicate.profile, source.profile)
        self.assertEqual(result["hosts"], [])

    def test_duplicate_is_independent_and_accepts_terminal_dimensions(self) -> None:
        source = self._connect()
        source.profile["extra"] = {"options": ["original"]}
        source._out = "source output"
        source._agent_buf = "source agent history"

        result = self.api.duplicate_session(source.id, cols=160, rows=48)

        self.assertTrue(result["ok"], result)
        duplicate = self.api._sessions[result["session"]["id"]]
        self.assertIsNot(duplicate, source)
        self.assertNotEqual(duplicate.id, source.id)
        self.assertEqual((duplicate.cols, duplicate.rows), (160, 48))
        self.assertEqual((source.cols, source.rows), (120, 32))
        self.assertEqual(duplicate.drain(), "")
        self.assertEqual(duplicate._agent_buf, "")
        self.assertIsNot(duplicate._agent, source._agent)
        duplicate.profile["password"] = "changed"
        duplicate.profile["extra"]["options"].append("duplicate")
        self.assertEqual(source.profile["password"], "unsaved-password-secret")
        self.assertEqual(source.profile["extra"]["options"], ["original"])
        self.assertEqual(self.api.close_session(duplicate.id), {"ok": True})
        self.assertFalse(source._closed.is_set())
        self.assertIs(self.api._sessions[source.id], source)
        self.assertEqual(source.drain(), "source output")
        source.start.assert_called_once_with()
        duplicate.start.assert_called_once_with()

    def test_missing_source_returns_clean_error_without_creating_session(self) -> None:
        result = self.api.duplicate_session("missing-session")

        self.assertEqual(result, {"ok": False, "error": "会话不存在"})
        self.assertEqual(self.api._sessions, {})
        self.session_class.assert_not_called()


if __name__ == "__main__":
    unittest.main()
