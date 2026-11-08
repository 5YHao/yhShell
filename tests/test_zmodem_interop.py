"""Interop tests: :mod:`yhshell.zmodem` against zmodem.js (Node.js).

The peer script (``tests/zmodem_peer.mjs``) speaks the protocol over its
stdin/stdout, so the Python side is exercised through the same byte-level
interface it uses on an SSH channel.  Skipped when Node.js or the npm
dependency is unavailable.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from yhshell import zmodem  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PEER = os.path.join(HERE, "zmodem_peer.mjs")
NODE = shutil.which("node")
HAS_PEER = bool(NODE) and os.path.exists(PEER) and os.path.isdir(os.path.join(HERE, "node_modules", "zmodem.js"))


class Peer:
    """A zmodem.js peer process plus a Link wired to its pipes."""

    def __init__(self, mode: str, target: str) -> None:
        assert NODE is not None
        self.proc = subprocess.Popen(
            [NODE, PEER, mode, target],
            cwd=HERE,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.link = zmodem.Link(self._write)
        self.errors: list[BaseException] = []
        self._pump = threading.Thread(target=self._read, daemon=True)
        self._pump.start()

    def _write(self, data: bytes) -> None:
        try:
            assert self.proc.stdin is not None
            self.proc.stdin.write(data)
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise zmodem.ZmodemAborted(f"对端进程已退出：{exc}") from exc

    def _read(self) -> None:
        assert self.proc.stdout is not None
        reader = getattr(self.proc.stdout, "read1", self.proc.stdout.read)
        while True:
            chunk = reader(65536)
            if not chunk:
                break
            self.link.feed(chunk)
        self.link.set_eof()

    def close(self, timeout: float = 20.0) -> tuple[int, str]:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except OSError:
            pass
        try:
            code = self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            code = -9
        stderr = b""
        if self.proc.stderr:
            stderr = self.proc.stderr.read() or b""
        return code, stderr.decode("utf-8", "replace")


@unittest.skipUnless(HAS_PEER, "需要 node 与 tests/node_modules/zmodem.js")
class ZmodemJsInterop(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _make_file(self, name: str, data: bytes) -> str:
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def test_python_sends_to_zmodem_js(self) -> None:
        payload = bytes(range(256)) * 40 + b"tail\r\n\x11\x13\x18"
        source = self._make_file("from-python.bin", payload)
        dest = os.path.join(self.tmp, "incoming")
        os.makedirs(dest, exist_ok=True)
        peer = Peer("recv", dest)
        errors: list[BaseException] = []
        try:
            sender = zmodem.Sender(peer.link, files=[source])
            thread = threading.Thread(target=lambda: self._run(sender, errors))
            thread.start()
            thread.join(45)
            self.assertFalse(thread.is_alive(), "sender did not finish")
        finally:
            code, log = peer.close()
        self.assertEqual(errors, [])
        self.assertEqual(code, 0, log)
        saved = os.path.join(dest, "from-python.bin")
        self.assertTrue(os.path.exists(saved), log)
        with open(saved, "rb") as handle:
            self.assertEqual(handle.read(), payload)

    def test_zmodem_js_sends_to_python(self) -> None:
        payload = os.urandom(120_000) + b"\x00\x18\x11\x13\x2a"
        source = self._make_file("from-node.bin", payload)
        dest = os.path.join(self.tmp, "incoming")
        os.makedirs(dest, exist_ok=True)
        peer = Peer("send", source)
        errors: list[BaseException] = []
        try:
            first = zmodem.read_initial_header(peer.link, 20.0)
            self.assertEqual(first.frame, zmodem.Frame.ZRQINIT)
            receiver = zmodem.Receiver(peer.link, directory=dest)
            thread = threading.Thread(target=lambda: self._run(receiver, errors, first))
            thread.start()
            thread.join(45)
            self.assertFalse(thread.is_alive(), "receiver did not finish")
        finally:
            code, log = peer.close()
        self.assertEqual(errors, [])
        self.assertEqual(code, 0, log)
        saved = os.path.join(dest, "from-node.bin")
        self.assertTrue(os.path.exists(saved), log)
        with open(saved, "rb") as handle:
            self.assertEqual(handle.read(), payload)

    @staticmethod
    def _run(worker, errors: list[BaseException], *args) -> None:
        try:
            worker.run(*args)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)


if __name__ == "__main__":
    unittest.main(verbosity=2)
