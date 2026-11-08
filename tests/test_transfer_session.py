"""Integration test: ZMODEM detection and prompting inside :class:`Session`.

No SSH connection is involved: a fake channel and a local ZMODEM peer stand in
for the remote shell, which exercises the byte-stream detection, the prompt /
accept handshake, progress reporting and the return to terminal mode.
"""

from __future__ import annotations

import os
import queue
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from yhshell import ssh, zmodem  # noqa: E402


class FakeChannel:
    def __init__(self) -> None:
        self.inbox: "queue.Queue[bytes]" = queue.Queue()
        self.peer_link: zmodem.Link | None = None
        self.closed = False

    def send(self, data: bytes) -> int:
        if self.peer_link is not None:
            self.peer_link.feed(bytes(data))
        return len(data)

    def recv(self, count: int) -> bytes:
        while True:
            try:
                chunk = self.inbox.get(timeout=0.05)
            except queue.Empty:
                if self.closed:
                    return b""
                continue
            if not chunk or self.closed:
                return b""
            return chunk

    def feed(self, data: bytes) -> None:
        self.inbox.put(bytes(data))

    def close(self) -> None:
        self.closed = True

    def settimeout(self, timeout) -> None:  # noqa: ANN001 - paramiko API shape
        return None


def wait_for(predicate, timeout: float = 20.0, interval: float = 0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


class TransferSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _session(self, settings: dict) -> ssh.Session:
        session = ssh.Session({"host": "example.test", "username": "tester"}, settings_provider=lambda: settings)
        channel = FakeChannel()
        session._channel = channel  # type: ignore[assignment]
        session.status = ssh.STATUS_CONNECTED
        self.threads: list[threading.Thread] = []
        reader = threading.Thread(target=session._read_loop, daemon=True)
        reader.start()
        self.threads.append(reader)
        return session

    def _close(self, session: ssh.Session) -> None:
        session.close()
        for thread in self.threads:
            thread.join(2)

    def test_download_with_prompt(self) -> None:
        payload = os.urandom(50_000) + b"\x18\x11\x13\x2a\x00"
        source = os.path.join(self.tmp, "remote.bin")
        with open(source, "wb") as handle:
            handle.write(payload)
        destination = os.path.join(self.tmp, "downloads")

        session = self._session({"confirm": True, "downloadDir": destination, "overwrite": False})
        channel = session._channel
        peer_link = zmodem.Link(channel.feed)  # type: ignore[union-attr]
        channel.peer_link = peer_link  # type: ignore[attr-defined]

        sender = zmodem.Sender(peer_link, files=[source])
        errors: list[BaseException] = []

        def run_peer() -> None:
            try:
                sender.run(None)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        peer_thread = threading.Thread(target=run_peer, daemon=True)
        peer_thread.start()

        prompt = wait_for(lambda: (session.transfer_snapshot() or {}).get("phase") == "prompt")
        snapshot = session.transfer_snapshot() or {}
        self.assertTrue(prompt, f"no prompt: {snapshot}")
        self.assertEqual(snapshot["direction"], "download")
        self.assertTrue(snapshot["confirm"])
        self.assertEqual(snapshot["directory"], destination)

        # Typing must not reach the binary stream while the session is active.
        self.assertTrue(session.write("x"))

        self.assertTrue(session.transfer_accept({"directory": destination}))
        done = wait_for(lambda: (session.transfer_snapshot() or {}).get("phase") in ("done", "error"), 30)
        self.assertTrue(done, session.transfer_snapshot())
        snapshot = session.transfer_snapshot() or {}
        self.assertEqual(snapshot["phase"], "done", snapshot)
        peer_thread.join(10)
        self.assertEqual(errors, [])

        saved = os.path.join(destination, "remote.bin")
        self.assertTrue(os.path.exists(saved))
        with open(saved, "rb") as handle:
            self.assertEqual(handle.read(), payload)

        # The terminal resumes: bytes after the handshake go back to the screen.
        channel.feed(b"\r\nuser@example:~$ ")
        collected: list[str] = []

        def collect():
            chunk = session.drain()
            if chunk:
                collected.append(chunk)
            joined = "".join(collected)
            return joined if "user@example" in joined else None

        text = wait_for(collect, 5)
        self.assertTrue(text, "terminal output did not resume")
        self.assertIn("[ZMODEM]", text)
        self._close(session)

    def test_download_without_prompt(self) -> None:
        payload = b"auto accepted\r\n" + os.urandom(20_000)
        source = os.path.join(self.tmp, "auto.bin")
        with open(source, "wb") as handle:
            handle.write(payload)
        destination = os.path.join(self.tmp, "auto-downloads")

        session = self._session({"confirm": False, "downloadDir": destination})
        channel = session._channel
        peer_link = zmodem.Link(channel.feed)  # type: ignore[union-attr]
        channel.peer_link = peer_link  # type: ignore[attr-defined]
        sender = zmodem.Sender(peer_link, files=[source])
        errors: list[BaseException] = []

        def run_peer() -> None:
            try:
                sender.run(None)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        peer_thread = threading.Thread(target=run_peer, daemon=True)
        peer_thread.start()
        done = wait_for(lambda: (session.transfer_snapshot() or {}).get("phase") in ("done", "error"), 30)
        self.assertTrue(done, session.transfer_snapshot())
        snapshot = session.transfer_snapshot() or {}
        self.assertEqual(snapshot["phase"], "done", snapshot)
        peer_thread.join(10)
        self.assertEqual(errors, [])
        saved = os.path.join(destination, "auto.bin")
        self.assertTrue(os.path.exists(saved))
        with open(saved, "rb") as handle:
            self.assertEqual(handle.read(), payload)
        self._close(session)

    def test_upload_with_prompt(self) -> None:
        payload = b"upload from the desktop\r\n" + bytes(range(0, 64))
        source = os.path.join(self.tmp, "local.txt")
        with open(source, "wb") as handle:
            handle.write(payload)
        remote_dir = os.path.join(self.tmp, "remote")
        os.makedirs(remote_dir, exist_ok=True)

        session = self._session({"confirm": True, "downloadDir": self.tmp, "overwrite": False})
        channel = session._channel
        peer_link = zmodem.Link(channel.feed)  # type: ignore[union-attr]
        channel.peer_link = peer_link  # type: ignore[attr-defined]

        receiver = zmodem.Receiver(peer_link, directory=remote_dir)
        errors: list[BaseException] = []

        def run_peer() -> None:
            try:
                receiver.run(None)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        peer_thread = threading.Thread(target=run_peer, daemon=True)
        peer_thread.start()

        prompt = wait_for(lambda: (session.transfer_snapshot() or {}).get("phase") == "prompt")
        snapshot = session.transfer_snapshot() or {}
        self.assertTrue(prompt, snapshot)
        self.assertEqual(snapshot["direction"], "upload")

        self.assertTrue(session.transfer_accept({"files": [source]}))
        done = wait_for(lambda: (session.transfer_snapshot() or {}).get("phase") in ("done", "error"), 30)
        self.assertTrue(done, session.transfer_snapshot())
        snapshot = session.transfer_snapshot() or {}
        self.assertEqual(snapshot["phase"], "done", snapshot)
        peer_thread.join(10)
        self.assertEqual(errors, [])

        saved = os.path.join(remote_dir, "local.txt")
        self.assertTrue(os.path.exists(saved))
        with open(saved, "rb") as handle:
            self.assertEqual(handle.read(), payload)
        self._close(session)

    def test_plain_output_is_not_swallowed(self) -> None:
        session = self._session({"confirm": True, "downloadDir": self.tmp})
        channel = session._channel
        channel.feed(b"hello * world\r\n$ ls\r\n")
        text = wait_for(lambda: "hello * world" in session.drain() and "hello * world" or None, 5)
        self.assertTrue(text)
        self.assertIsNone(session.transfer_snapshot())
        self._close(session)


if __name__ == "__main__":
    unittest.main(verbosity=2)
