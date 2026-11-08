"""Unit tests for :mod:`yhshell.zmodem` (run with ``python -m unittest``).

The encoding fixtures in this file were produced by zmodem.js 0.1.10, an
independent implementation, so they pin the wire format to something other
than this module's own idea of it.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from yhshell import zmodem  # noqa: E402


def connect() -> tuple[zmodem.Link, zmodem.Link]:
    """Two links wired back to back, like a socket pair."""
    left_holder: dict[str, zmodem.Link] = {}
    right_holder: dict[str, zmodem.Link] = {}

    def to_right(data: bytes) -> None:
        right_holder["link"].feed(bytes(data))

    def to_left(data: bytes) -> None:
        left_holder["link"].feed(bytes(data))

    left = zmodem.Link(to_right)
    right = zmodem.Link(to_left)
    left_holder["link"] = left
    right_holder["link"] = right
    return left, right


class Collector:
    def __init__(self) -> None:
        self.data = bytearray()

    def __call__(self, data: bytes) -> None:
        self.data += data


class CodecFixtures(unittest.TestCase):
    def test_crc16_matches_xmodem(self) -> None:
        self.assertEqual(zmodem.crc16(b"123456789"), 0x31C3)
        self.assertEqual(zmodem.crc16(b""), 0x0000)

    def test_crc32_matches_zlib(self) -> None:
        self.assertEqual(zmodem.crc32(b"123456789"), 0xCBF43926)

    def test_hex_header_fixture(self) -> None:
        out = Collector()
        codec = zmodem.Codec(zmodem.Link(out))
        codec.send_hex_header(
            zmodem.Frame.ZRINIT,
            bytes([0x00, 0x20, 0x00, zmodem.CANFDX | zmodem.CANOVIO | zmodem.CANFC32 | zmodem.ESCCTL]),
        )
        self.assertEqual(
            bytes(out.data).hex(),
            "2a2a184230313030323030303633373035320d0a11",
        )

    def test_binary_header_fixtures(self) -> None:
        out = Collector()
        codec = zmodem.Codec(zmodem.Link(out))
        codec.send_bin_header(zmodem.Frame.ZFILE, b"\x00\x00\x00\x00")
        self.assertEqual(bytes(out.data).hex(), "2a18411844184018401840184018c91846")

        out.data.clear()
        codec.send_bin_header(zmodem.Frame.ZRPOS, (0).to_bytes(4, "little"))
        self.assertEqual(bytes(out.data).hex(), "2a184118491840184018401840a87c")

    def test_subpacket_fixtures(self) -> None:
        out = Collector()
        codec = zmodem.Codec(zmodem.Link(out))
        codec.send_subpacket(b"hello", zmodem.ZCRCW)
        self.assertEqual(bytes(out.data).hex(), "68656c6c6f186b56e2")

        out.data.clear()
        codec.tx_crc32 = True
        codec.send_subpacket(bytes([0, 1, 2, 3, 0x41, 0x7F, 0x80, 0xFF]), zmodem.ZCRCE)
        self.assertEqual(bytes(out.data).hex(), "1840184118421843417f18c0ff1868185cb9185f5c")

    def test_escape_roundtrip_all_bytes(self) -> None:
        payload = bytes(range(256))
        for escape_ctrl in (True, False):
            encoded = zmodem.zdle_escape(payload, escape_ctrl=escape_ctrl)
            self.assertNotIn(b"\x18\x68", encoded) if escape_ctrl else None
            out = Collector()
            codec = zmodem.Codec(zmodem.Link(out), escape_ctrl=escape_ctrl)
            codec.send_subpacket(payload, zmodem.ZCRCE)
            left, right = connect()
            reader = zmodem.Codec(right)
            right.feed(bytes(out.data))
            decoded, frame_end = reader.read_subpacket(5.0)
            self.assertEqual(decoded, payload)
            self.assertEqual(frame_end, zmodem.ZCRCE)

    def test_header_roundtrip_hex_and_binary(self) -> None:
        for crc32 in (False, True):
            out = Collector()
            writer = zmodem.Codec(zmodem.Link(out))
            writer.tx_crc32 = crc32
            data = b"\x78\x56\x34\x12"
            writer.send_bin_header(zmodem.Frame.ZRPOS, data)
            right = zmodem.Link(lambda _data: None)
            right.feed(bytes(out.data))
            header = zmodem.Codec(right).read_header(3.0)
            self.assertEqual(header.frame, zmodem.Frame.ZRPOS)
            self.assertEqual(header.offset, 0x12345678)

    def test_read_header_skips_garbage(self) -> None:
        out = Collector()
        writer = zmodem.Codec(zmodem.Link(out))
        writer.send_hex_header(zmodem.Frame.ZFIN)
        right = zmodem.Link(lambda _data: None)
        right.feed(b"noise\r\n" + bytes(out.data))
        header = zmodem.Codec(right).read_header(3.0)
        self.assertEqual(header.frame, zmodem.Frame.ZFIN)


class TransferTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _make_files(self) -> list[str]:
        paths = []
        blob = bytes(range(256)) * 3
        for name, data in (("a.bin", blob), ("b.txt", b"hello world\n"), ("empty.dat", b"")):
            path = os.path.join(self.tmp, name)
            with open(path, "wb") as handle:
                handle.write(data)
            paths.append(path)
        return paths

    def test_sender_to_receiver(self) -> None:
        files = self._make_files()
        dest = os.path.join(self.tmp, "recv")
        os.makedirs(dest, exist_ok=True)
        left, right = connect()
        receiver = zmodem.Receiver(right, directory=dest)
        sender = zmodem.Sender(left, files=files)
        errors: list[BaseException] = []

        def run_receiver() -> None:
            try:
                receiver.run(None)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        def run_sender() -> None:
            try:
                sender.run(None)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        thread = threading.Thread(target=run_receiver, daemon=True)
        thread.start()
        sender_thread = threading.Thread(target=run_sender, daemon=True)
        sender_thread.start()
        thread.join(60)
        sender_thread.join(60)
        self.assertEqual(errors, [])
        self.assertFalse(thread.is_alive(), "receiver did not finish")

        for path in files:
            name = os.path.basename(path)
            saved = os.path.join(dest, name)
            self.assertTrue(os.path.exists(saved), name)
            with open(path, "rb") as original, open(saved, "rb") as received:
                self.assertEqual(original.read(), received.read(), name)
        self.assertEqual(len(sender.sent), 3)
        self.assertEqual(len(receiver.saved), 3)

    def test_progress_and_callbacks(self) -> None:
        path = os.path.join(self.tmp, "big.bin")
        with open(path, "wb") as handle:
            handle.write(os.urandom(300_000))
        dest = os.path.join(self.tmp, "dest")
        left, right = connect()
        seen: list[tuple[str, int, int]] = []
        receiver = zmodem.Receiver(
            right,
            directory=dest,
            on_file=lambda name, size: seen.append(("file", size, 0)),
            on_progress=lambda done, total: seen.append(("progress", done, total)),
        )
        sender = zmodem.Sender(left, files=[path])
        threads = [
            threading.Thread(target=receiver.run, args=(None,), daemon=True),
            threading.Thread(target=sender.run, args=(None,), daemon=True),
        ]
        threads[0].start()
        threads[1].start()
        for thread in threads:
            thread.join(60)
            self.assertFalse(thread.is_alive())
        self.assertTrue(any(kind == "progress" and done > 0 for kind, done, _ in seen))
        self.assertTrue(os.path.exists(os.path.join(dest, "big.bin")))

    def test_receiver_names_collisions(self) -> None:
        dest = os.path.join(self.tmp, "recv")
        os.makedirs(dest, exist_ok=True)
        with open(os.path.join(dest, "a.bin"), "wb") as handle:
            handle.write(b"existing")
        path = os.path.join(self.tmp, "a.bin")
        with open(path, "wb") as handle:
            handle.write(b"new")
        left, right = connect()
        receiver = zmodem.Receiver(right, directory=dest)
        sender = zmodem.Sender(left, files=[path])
        receiver_thread = threading.Thread(target=receiver.run, args=(None,), daemon=True)
        receiver_thread.start()
        sender_thread = threading.Thread(target=sender.run, args=(None,), daemon=True)
        sender_thread.start()
        receiver_thread.join(30)
        sender_thread.join(30)
        self.assertTrue(os.path.exists(os.path.join(dest, "a (1).bin")))
        with open(os.path.join(dest, "a.bin"), "rb") as handle:
            self.assertEqual(handle.read(), b"existing")

    def test_cancel_receiver(self) -> None:
        left, right = connect()
        dest = os.path.join(self.tmp, "recv")
        receiver = zmodem.Receiver(right, directory=dest)
        errors: list[BaseException] = []

        def run_receiver() -> None:
            try:
                receiver.run(None)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        thread = threading.Thread(target=run_receiver, daemon=True)
        thread.start()
        time.sleep(0.1)
        receiver.cancel()
        thread.join(10)
        self.assertTrue(errors)
        self.assertIsInstance(errors[0], zmodem.ZmodemCancelled)

    def test_sender_cancel_while_waiting(self) -> None:
        left, right = connect()
        sender = zmodem.Sender(left, files=[self._make_files()[0]])
        errors: list[BaseException] = []

        def run_sender() -> None:
            try:
                sender.run(None)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        thread = threading.Thread(target=run_sender, daemon=True)
        thread.start()
        time.sleep(0.1)
        sender.cancel()
        thread.join(10)
        self.assertTrue(errors)
        self.assertIsInstance(errors[0], zmodem.ZmodemCancelled)


if __name__ == "__main__":
    unittest.main(verbosity=2)
