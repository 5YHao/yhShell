"""Integration test: the agent drives the user's own interactive PTY.

The fake SSH server runs a BusyBox ``sh`` behind a PTY-like channel, so these
tests exercise the real path used against a jump server: the command is typed
into the one terminal that exists, its echo and output share that stream, and
the marker plumbing must stay invisible to the user.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from yhshell.ssh import STATUS_CONNECTED, Session  # noqa: E402

BUSYBOX = os.environ.get("YHS_BUSYBOX") or os.path.join(tempfile.gettempdir(), "busybox64.exe")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for(predicate, timeout: float = 20.0, interval: float = 0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


@unittest.skipUnless(os.path.exists(BUSYBOX), "BusyBox not available; set YHS_BUSYBOX")
class TerminalShellTest(unittest.TestCase):
    port = 0
    proc: subprocess.Popen | None = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.port = free_port()
        cls.proc = subprocess.Popen(
            [sys.executable, os.path.join(HERE, "fake_sshd.py"), "--port", str(cls.port)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        assert cls.proc.stdout is not None
        line = cls.proc.stdout.readline()
        if "fake sshd on" not in line:
            raise RuntimeError(f"fake sshd did not start: {line!r}")

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.proc is not None:
            cls.proc.kill()
            cls.proc.wait(timeout=5)

    def setUp(self) -> None:
        self.session = Session(
            {
                "host": "127.0.0.1",
                "port": self.port,
                "username": "tester",
                "password": "test",
                "auth": "password",
            }
        )
        self.session.start()
        self.assertTrue(
            wait_for(lambda: self.session.status == STATUS_CONNECTED, 15),
            f"session did not connect: {self.session.status} {self.session.error}",
        )
        self.shell = self.session.agent_shell()

    def tearDown(self) -> None:
        self.session.close()

    def test_output_and_exit_code(self) -> None:
        result = self.shell.run("echo hello-yh", timeout=15)
        self.assertEqual(result["exitCode"], 0)
        self.assertFalse(result["timedOut"])
        self.assertEqual(result["output"], "hello-yh")

    def test_exit_code_is_reported(self) -> None:
        result = self.shell.run("sh -c 'exit 7'", timeout=15)
        self.assertEqual(result["exitCode"], 7)

    def test_working_directory_survives(self) -> None:
        self.shell.run("cd /", timeout=15)
        result = self.shell.run("pwd", timeout=15)
        self.assertTrue(result["output"].endswith("/"), result["output"])

    def test_marker_plumbing_never_reaches_the_user(self) -> None:
        self.shell.run("echo visible-line", timeout=15)
        ui = ""
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            ui += self.session.drain()
            if "visible-line" in ui:
                break
            time.sleep(0.05)
        self.assertIn("visible-line", ui)
        self.assertIn("echo visible-line", ui, "the user must see the command being typed")
        self.assertNotIn("__YHS", ui)
        self.assertNotIn("%s%s", ui)

    def test_timeout_is_reported(self) -> None:
        # The fake server cannot deliver SIGINT (Ctrl-C is a byte on a pipe,
        # not a signal), so only the timeout reporting is checked here.
        result = self.shell.run("sleep 30", timeout=5)
        self.assertTrue(result["timedOut"])
        self.assertEqual(result["exitCode"], -1)
        self.assertIn("超时", result["output"])

    def test_refuses_when_terminal_is_in_a_full_screen_program(self) -> None:
        self.session._append("\x1b[?1049h")
        try:
            with self.assertRaises(RuntimeError):
                self.shell.run("echo nope", timeout=5)
        finally:
            self.session._append("\x1b[?1049l")


if __name__ == "__main__":
    unittest.main(verbosity=2)
