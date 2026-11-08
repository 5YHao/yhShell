"""ZMODEM (``rz`` / ``sz``) sender and receiver for the interactive terminal.

The wire format follows lrzsz (``sz``/``rz``) and the independent JavaScript
implementation zmodem.js, which are the two peers this code is tested against:

* headers may be hex (``**\\x18B``) or binary (``*\\x18A`` / ``*\\x18C``);
* data subpackets end in ZCRCG/ZCRCQ/ZCRCW/ZCRCE and carry a CRC-16 or a
  CRC-32 depending on the width of the header that introduced the frame;
* ZDLE escaping covers ZDLE, XON, XOFF, their high-bit variants and (when the
  peer asked for it) every control character.

The module is transport agnostic: :class:`Link` is fed with raw bytes by the
SSH reader thread and blocks the protocol thread while it waits for more.
"""

from __future__ import annotations

import os
import re
import threading
import time
import zlib
from dataclasses import dataclass
from enum import IntEnum
from typing import Callable, Sequence

# ---------------------------------------------------------------------------
# Protocol constants
# ---------------------------------------------------------------------------

ZPAD = 0x2A
ZDLE = 0x18
ZBIN = 0x41
ZHEX = 0x42
ZBIN32 = 0x43

CAN = 0x18
XON = 0x11
XOFF = 0x13
ZCRCE = 0x68
ZCRCG = 0x69
ZCRCQ = 0x6A
ZCRCW = 0x6B
ZRUB0 = 0x6C
ZRUB1 = 0x6D

# ZRINIT capability flags (byte ZF0)
CANFDX = 0x01
CANOVIO = 0x02
CANBRK = 0x04
CANFC32 = 0x20
ESCCTL = 0x40

# ZFILE conversion option: binary transfer
ZCBIN = 1

MAX_SUBPACKET = 8192
DEFAULT_BUFFER = 8192

ABORT_SEQUENCE = bytes([CAN]) * 8 + b"\x08" * 10
OVER_AND_OUT = b"OO"


class Frame(IntEnum):
    ZRQINIT = 0
    ZRINIT = 1
    ZSINIT = 2
    ZACK = 3
    ZFILE = 4
    ZSKIP = 5
    ZNAK = 6
    ZABORT = 7
    ZFIN = 8
    ZRPOS = 9
    ZDATA = 10
    ZEOF = 11
    ZFERR = 12
    ZCRC = 13
    ZCHALLENGE = 14
    ZCOMPL = 15
    ZCAN = 16
    ZFREECNT = 17
    ZCOMMAND = 18
    ZSTDERR = 19


class ZmodemError(Exception):
    """Base class for protocol failures."""


class ZmodemTimeout(ZmodemError):
    """The peer went quiet for too long."""

    def __init__(self, message: str = "等待远端响应超时") -> None:
        super().__init__(message)


class ZmodemCancelled(Exception):
    """The local side cancelled the transfer."""


class ZmodemAborted(ZmodemError):
    """The peer cancelled the transfer (CAN*5 or ZABORT/ZCAN)."""

    def __init__(self, message: str = "对端取消了传输") -> None:
        super().__init__(message)


class ZmodemProtocolError(ZmodemError):
    """Malformed frame or unexpected header."""


class ZmodemClosed(ZmodemError):
    """The peer closed the session while we still had files to send."""


# ---------------------------------------------------------------------------
# CRC-16 (XMODEM variant used by ZMODEM) and escaping
# ---------------------------------------------------------------------------


def _build_crc16_table() -> tuple[int, ...]:
    table = []
    for index in range(256):
        value = index << 8
        for _ in range(8):
            if value & 0x8000:
                value = ((value << 1) ^ 0x1021) & 0xFFFF
            else:
                value = (value << 1) & 0xFFFF
        table.append(value)
    return tuple(table)


_CRC16_TABLE = _build_crc16_table()


def _crc16_update(crc: int, byte: int) -> int:
    return (_CRC16_TABLE[(crc >> 8) & 0xFF] ^ ((crc & 0xFF) << 8) ^ byte) & 0xFFFF


def crc16(data: bytes) -> int:
    """ZMODEM's CRC-16: XMODEM polynomial with two trailing zero bytes folded in.

    lrzsz clocks two zero bytes through the register before sending the value,
    so it is not plain CRC-16/XMODEM (known vector "123456789" → 0x31c3 is the
    result of the XMODEM step alone).  Both lrzsz and zmodem.js agree on the
    extra step, and interop depends on reproducing it.
    """
    crc = 0
    for byte in data:
        crc = _crc16_update(crc, byte)
    crc = _crc16_update(crc, 0)
    crc = _crc16_update(crc, 0)
    return crc


def crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def zdle_escape(data: bytes, *, escape_ctrl: bool = True, previous: int = 0) -> bytes:
    """Return ``data`` with ZMODEM's ZDLE escaping applied."""
    out = bytearray()
    last = previous & 0xFF
    for byte in data:
        if byte in (ZDLE, XON, XOFF, XON | 0x80, XOFF | 0x80):
            out.append(ZDLE)
            out.append(byte ^ 0x40)
        elif byte in (0x10, 0x90):
            if escape_ctrl:
                out.append(ZDLE)
                out.append(byte ^ 0x40)
            else:
                out.append(byte)
        elif byte in (0x0D, 0x8D):
            # Telenet-style CR escape: only after '@' unless control escaping.
            if escape_ctrl or (last & 0x7F) == 0x40:
                out.append(ZDLE)
                out.append(byte ^ 0x40)
            else:
                out.append(byte)
        elif escape_ctrl and not (byte & 0x60):
            out.append(ZDLE)
            out.append(byte ^ 0x40)
        else:
            out.append(byte)
        last = byte
    return bytes(out)


# ---------------------------------------------------------------------------
# Headers and file metadata
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Header:
    frame: int
    data: bytes = b"\x00\x00\x00\x00"

    @property
    def offset(self) -> int:
        return int.from_bytes(self.data, "little")

    @property
    def flags(self) -> int:
        return self.data[3]

    @property
    def name(self) -> str:
        try:
            return Frame(self.frame).name
        except ValueError:
            return f"0x{self.frame:02x}"


@dataclass
class FileInfo:
    name: str
    size: int
    mtime: int = 0
    mode: int = 0
    files_remaining: int = 0
    bytes_remaining: int = 0


def parse_zfile(payload: bytes) -> FileInfo:
    """Parse the name/metadata payload carried by a ZFILE frame."""
    head, _, tail = payload.partition(b"\x00")
    name = head.decode("utf-8", "replace")
    fields = tail.decode("ascii", "replace").split(" ") if tail else []

    def number(index: int, base: int = 10) -> int:
        try:
            value = fields[index] if len(fields) > index else ""
            return int(value, base) if value else 0
        except ValueError:
            return 0

    return FileInfo(
        name=name,
        size=number(0),
        mtime=number(1, 8),
        mode=number(2, 8),
        files_remaining=number(4),
        bytes_remaining=number(5),
    )


def build_zfile(info: FileInfo, files_remaining: int, bytes_remaining: int) -> bytes:
    mode = (0x8000 | (info.mode & 0o7777)) if info.mode else 0o100644
    meta = "%d %o %o 0 %d %d" % (
        max(0, info.size),
        max(0, info.mtime),
        mode,
        max(0, files_remaining),
        max(0, bytes_remaining),
    )
    return info.name.encode("utf-8", "replace") + b"\x00" + meta.encode("ascii")


_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def safe_filename(name: str) -> str:
    """Turn a remote name into a safe local basename."""
    name = name.replace("\\", "/").split("/")[-1]
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    if not name:
        return "download"
    if name.split(".")[0].upper() in _WINDOWS_RESERVED:
        name = "_" + name
    return name[:180]


def unique_path(directory: str, name: str) -> str:
    """Pick a non-existing path: ``file.txt`` → ``file (1).txt``."""
    path = os.path.join(directory, name)
    if not os.path.exists(path):
        return path
    stem, suffix = os.path.splitext(name)
    for index in range(1, 1000):
        candidate = os.path.join(directory, f"{stem} ({index}){suffix}")
        if not os.path.exists(candidate):
            return candidate
    return os.path.join(directory, f"{stem}-{int(time.time())}{suffix}")


# ---------------------------------------------------------------------------
# Transport link
# ---------------------------------------------------------------------------


class Link:
    """Byte pipe between the SSH reader thread and the protocol thread."""

    def __init__(self, write: Callable[[bytes], None]) -> None:
        self._write = write
        self._cond = threading.Condition()
        self._buffer = bytearray()
        self._eof = False
        self._aborted = False
        self.bytes_sent = 0
        self.bytes_received = 0
        self._last_receive = time.monotonic()

    # -- called from the reader thread ------------------------------------
    def feed(self, data: bytes) -> None:
        if not data:
            return
        with self._cond:
            self._buffer += data
            self.bytes_received += len(data)
            self._last_receive = time.monotonic()
            self._cond.notify_all()

    def set_eof(self) -> None:
        with self._cond:
            self._eof = True
            self._cond.notify_all()

    # -- called from the protocol thread ----------------------------------
    def write(self, data: bytes) -> None:
        if not data:
            return
        if self._aborted:
            raise ZmodemCancelled()
        self._write(data)
        with self._cond:
            self.bytes_sent += len(data)

    def read(self, size: int, timeout: float) -> bytes:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._cond:
            while len(self._buffer) < size:
                if self._aborted:
                    raise ZmodemCancelled()
                if self._eof:
                    raise ZmodemAborted("SSH 连接已关闭")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ZmodemTimeout()
                self._cond.wait(min(remaining, 0.25))
            data = bytes(self._buffer[:size])
            del self._buffer[:size]
            return data

    def read_byte(self, timeout: float) -> int:
        return self.read(1, timeout)[0]

    def unread(self, data: bytes) -> None:
        if not data:
            return
        with self._cond:
            self._buffer[:0] = data

    def peek(self, timeout: float) -> bytes:
        """Return (without consuming) whatever is buffered after a short wait."""
        with self._cond:
            if not self._buffer and not self._eof and not self._aborted:
                self._cond.wait(max(0.0, timeout))
            return bytes(self._buffer)

    def leftover(self) -> bytes:
        with self._cond:
            data = bytes(self._buffer)
            self._buffer.clear()
            return data

    def cancel(self) -> None:
        with self._cond:
            self._aborted = True
            self._cond.notify_all()

    def quiet_for(self) -> float:
        with self._cond:
            return time.monotonic() - self._last_receive


# ---------------------------------------------------------------------------
# Codec
# ---------------------------------------------------------------------------


Progress = Callable[[int, int], None]


class Codec:
    """Header/subpacket encoding and decoding over a :class:`Link`."""

    def __init__(self, link: Link, *, escape_ctrl: bool = True) -> None:
        self.link = link
        self.escape_ctrl = escape_ctrl
        self.tx_crc32 = False
        self.rx_crc32 = False
        self._last_byte = 0

    # -- sending ----------------------------------------------------------
    def _escape(self, data: bytes) -> bytes:
        encoded = zdle_escape(data, escape_ctrl=self.escape_ctrl, previous=self._last_byte)
        if data:
            self._last_byte = data[-1]
        return encoded

    def send_hex_header(self, frame: int, data: bytes = b"\x00\x00\x00\x00") -> None:
        body = bytes([int(frame) & 0x7F]) + data
        checksum = crc16(body)
        text = (body + checksum.to_bytes(2, "big")).hex().encode("ascii")
        out = bytearray(b"**")
        out.append(ZDLE)
        out.append(ZHEX)
        out += text
        out += b"\r\n"
        if int(frame) not in (Frame.ZACK, Frame.ZFIN):
            out.append(XON)
        self.link.write(bytes(out))
        self._last_byte = 0x0A

    def send_bin_header(self, frame: int, data: bytes = b"\x00\x00\x00\x00") -> None:
        body = bytes([int(frame) & 0x7F]) + data
        if self.tx_crc32:
            checksum = crc32(body)
            prefix = bytes([ZPAD, ZDLE, ZBIN32])
            raw = body + checksum.to_bytes(4, "little")
        else:
            checksum = crc16(body)
            prefix = bytes([ZPAD, ZDLE, ZBIN])
            raw = body + checksum.to_bytes(2, "big")
        self.link.write(prefix + self._escape(raw))

    def send_subpacket(self, payload: bytes, frame_end: int) -> None:
        body = payload + bytes([frame_end])
        if self.tx_crc32:
            crc_bytes = crc32(body).to_bytes(4, "little")
        else:
            crc_bytes = crc16(body).to_bytes(2, "big")
        self.link.write(self._escape(payload) + bytes([ZDLE, frame_end]) + self._escape(crc_bytes))

    def send_abort(self) -> None:
        try:
            self.link.write(ABORT_SEQUENCE)
        except Exception:  # noqa: BLE001 - abort is best effort
            pass

    # -- receiving --------------------------------------------------------
    def _read_plain_byte(self, deadline: float) -> int:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ZmodemTimeout()
            byte = self.link.read_byte(remaining)
            # XON/XOFF are flow control; every sender escapes them in payloads.
            if byte in (XON, XOFF, XON | 0x80, XOFF | 0x80):
                continue
            return byte

    def _read_encoded_byte(self, deadline: float) -> int:
        byte = self._read_plain_byte(deadline)
        if byte != ZDLE:
            return byte
        nxt = self._read_plain_byte(deadline)
        if nxt == ZRUB0:
            return 0x7F
        if nxt == ZRUB1:
            return 0xFF
        if nxt & 0x60 == 0x40:
            return nxt ^ 0x40
        raise ZmodemProtocolError(f"非法的 ZDLE 转义字节 0x{nxt:02x}")

    def _read_hex_digit(self, deadline: float) -> int:
        char = self._read_plain_byte(deadline) & 0x7F
        if 0x30 <= char <= 0x39:
            return char - 0x30
        if 0x61 <= char <= 0x66:
            return char - 0x61 + 10
        if 0x41 <= char <= 0x46:
            return char - 0x41 + 10
        raise ZmodemProtocolError(f"非法的十六进制字符 0x{char:02x}")

    def _read_hex_byte(self, deadline: float) -> int:
        return (self._read_hex_digit(deadline) << 4) | self._read_hex_digit(deadline)

    def read_header(self, timeout: float) -> Header:
        """Read the next header, skipping anything that is not one."""
        deadline = time.monotonic() + timeout
        garbage = 0
        failures = 0
        while True:
            if time.monotonic() >= deadline:
                raise ZmodemTimeout()
            byte = self._read_plain_byte(deadline)
            if byte != ZPAD:
                garbage += 1
                if garbage > 65536:
                    raise ZmodemProtocolError("没有找到 ZMODEM 帧头")
                continue
            while True:
                nxt = self._read_plain_byte(deadline)
                if nxt == ZPAD:
                    continue
                break
            if nxt != ZDLE:
                garbage += 1
                if garbage > 65536:
                    raise ZmodemProtocolError("没有找到 ZMODEM 帧头")
                continue
            kind = self._read_plain_byte(deadline)
            if kind not in (ZHEX, ZBIN, ZBIN32):
                continue
            try:
                if kind == ZHEX:
                    return self._read_hex_header(deadline)
                return self._read_bin_header(deadline, 4 if kind == ZBIN32 else 2)
            except ZmodemProtocolError:
                failures += 1
                if failures > 8:
                    raise
                # A corrupt frame: keep scanning, the peer will retransmit.

    def _read_hex_header(self, deadline: float) -> Header:
        body = bytes(self._read_hex_byte(deadline) for _ in range(5))
        checksum = int.from_bytes(bytes(self._read_hex_byte(deadline) for _ in range(2)), "big")
        if checksum != crc16(body):
            raise ZmodemProtocolError("帧头 CRC 校验失败")
        # Headers are followed by CR LF (sometimes only LF / high-bit variants).
        tail = self.link.peek(0.15)
        if tail:
            first = tail[0]
            if first in (0x0D, 0x8D):
                self.link.read_byte(0.5)
                more = self.link.peek(0.15)
                if more and more[0] in (0x0A, 0x8A):
                    self.link.read_byte(0.5)
            elif first in (0x0A, 0x8A):
                self.link.read_byte(0.5)
        self.rx_crc32 = False
        return Header(body[0], body[1:5])

    def _read_bin_header(self, deadline: float, crc_len: int) -> Header:
        body = bytes(self._read_encoded_byte(deadline) for _ in range(5))
        checksum = int.from_bytes(
            bytes(self._read_encoded_byte(deadline) for _ in range(crc_len)),
            "little" if crc_len == 4 else "big",
        )
        if checksum != (crc32(body) if crc_len == 4 else crc16(body)):
            raise ZmodemProtocolError("帧头 CRC 校验失败")
        self.rx_crc32 = crc_len == 4
        return Header(body[0], body[1:5])

    def read_subpacket(self, timeout: float, max_len: int = 1 << 20) -> tuple[bytes, int]:
        """Read one data subpacket; returns (payload, frame-end byte)."""
        deadline = time.monotonic() + timeout
        payload = bytearray()
        while True:
            byte = self._read_plain_byte(deadline)
            if byte != ZDLE:
                payload.append(byte)
            else:
                nxt = self._read_plain_byte(deadline)
                if nxt in (ZCRCE, ZCRCG, ZCRCQ, ZCRCW):
                    frame_end = nxt
                    break
                if nxt == CAN:
                    for _ in range(4):
                        if self._read_plain_byte(deadline) != CAN:
                            break
                    raise ZmodemAborted()
                if nxt == ZRUB0:
                    payload.append(0x7F)
                elif nxt == ZRUB1:
                    payload.append(0xFF)
                elif nxt & 0x60 == 0x40:
                    payload.append(nxt ^ 0x40)
                else:
                    raise ZmodemProtocolError(f"非法的数据转义 0x{nxt:02x}")
            if len(payload) > max_len:
                raise ZmodemProtocolError("数据子包过大")
        crc_len = 4 if self.rx_crc32 else 2
        checksum = int.from_bytes(
            bytes(self._read_encoded_byte(deadline) for _ in range(crc_len)),
            "little" if crc_len == 4 else "big",
        )
        body = bytes(payload) + bytes([frame_end])
        if checksum != (crc32(body) if crc_len == 4 else crc16(body)):
            raise ZmodemProtocolError("数据子包 CRC 校验失败")
        return bytes(payload), frame_end


def read_initial_header(link: Link, timeout: float = 8.0) -> Header:
    return Codec(link).read_header(timeout)


# ---------------------------------------------------------------------------
# Receiver (remote ``sz`` → local files)
# ---------------------------------------------------------------------------


class Receiver(Codec):
    """Receive one or more files offered by a remote ``sz``."""

    def __init__(
        self,
        link: Link,
        *,
        directory: str,
        cancelled: threading.Event | None = None,
        on_file: Callable[[str, int], None] | None = None,
        on_progress: Progress | None = None,
        on_file_done: Callable[[str, int, str], None] | None = None,
        buffer_size: int = 0,
        overwrite: bool = False,
    ) -> None:
        super().__init__(link, escape_ctrl=True)
        self.directory = directory
        self.cancelled = cancelled or threading.Event()
        self.on_file = on_file or (lambda name, size: None)
        self.on_progress = on_progress or (lambda done, total: None)
        self.on_file_done = on_file_done or (lambda name, size, path: None)
        self.buffer_size = buffer_size
        self.overwrite = overwrite
        self.saved: list[dict[str, object]] = []
        self._current: dict[str, object] | None = None
        self._handle = None

    # -- public -----------------------------------------------------------
    def run(self, first: Header | None = None) -> list[dict[str, object]]:
        if first is not None and first.frame == Frame.ZRINIT:
            raise ZmodemProtocolError("对端也是接收方，无法接收文件")
        try:
            # Announce readiness once; a retransmitted ZRQINIT needs no answer
            # because SSH does not lose the ZRINIT we just sent, and replying
            # again would make sz think we skipped to the next file.
            self.send_zrinit()
            first_wait = True
            while True:
                self._check_cancel()
                header = self.read_header(20.0 if first_wait else 60.0)
                first_wait = False
                frame = header.frame
                if frame == Frame.ZRQINIT:
                    continue
                elif frame == Frame.ZRINIT:
                    continue
                elif frame == Frame.ZSINIT:
                    self._acknowledge_sinit()
                elif frame == Frame.ZFILE:
                    self._open_file()
                elif frame == Frame.ZDATA:
                    self._receive_data(header)
                elif frame == Frame.ZEOF:
                    self._close_file()
                    self.send_zrinit()
                elif frame == Frame.ZFIN:
                    self.send_hex_header(Frame.ZFIN)
                    self.link.write(OVER_AND_OUT)
                    return self.saved
                elif frame == Frame.ZCAN:
                    raise ZmodemAborted()
                elif frame in (Frame.ZABORT, Frame.ZFERR):
                    raise ZmodemAborted("远端中止了传输")
                elif frame in (Frame.ZNAK, Frame.ZSKIP, Frame.ZCOMPL):
                    continue
                else:
                    raise ZmodemProtocolError(f"接收时收到意外的帧 {header.name}")
        finally:
            if self._handle is not None:
                try:
                    self._handle.close()  # type: ignore[union-attr]
                except Exception:  # noqa: BLE001
                    pass
                self._handle = None

    def cancel(self) -> None:
        self.cancelled.set()
        self.send_abort()
        self.link.cancel()

    # -- steps ------------------------------------------------------------
    def send_zrinit(self) -> None:
        data = bytes([
            self.buffer_size & 0xFF,
            (self.buffer_size >> 8) & 0xFF,
            0,
            CANFDX | CANOVIO | CANFC32 | ESCCTL,
        ])
        self.send_hex_header(Frame.ZRINIT, data)

    def _acknowledge_sinit(self) -> None:
        # ZSINIT carries the sender's attention string; acknowledging is enough.
        try:
            self.read_subpacket(10.0)
        except ZmodemError:
            pass
        self.send_hex_header(Frame.ZACK)

    def _open_file(self) -> None:
        payload, _ = self.read_subpacket(15.0)
        info = parse_zfile(payload)
        os.makedirs(self.directory, exist_ok=True)
        name = safe_filename(info.name)
        path = os.path.join(self.directory, name) if self.overwrite else unique_path(self.directory, name)
        handle = open(path, "wb")
        self._handle = handle
        self._current = {
            "name": os.path.basename(path),
            "size": max(0, info.size),
            "path": path,
            "received": 0,
        }
        self.saved.append(self._current)
        self.on_file(str(self._current["name"]), int(self._current["size"]))
        self.send_hex_header(Frame.ZRPOS, (0).to_bytes(4, "little"))

    def _receive_data(self, header: Header) -> None:
        if self._handle is None:
            raise ZmodemProtocolError("未收到文件名就开始传输数据")
        current = self._current or {}
        offset = int(current.get("received", 0))
        if header.offset != offset:
            # Ask the sender to resume from where we actually are.
            self.send_hex_header(Frame.ZRPOS, offset.to_bytes(4, "little"))
            return
        while True:
            self._check_cancel()
            payload, frame_end = self.read_subpacket(60.0)
            if payload:
                self._handle.write(payload)  # type: ignore[union-attr]
                offset += len(payload)
                current["received"] = offset
                self.on_progress(offset, int(current.get("size", 0)))
            if frame_end == ZCRCG:
                continue
            if frame_end == ZCRCQ:
                self.send_hex_header(Frame.ZACK, offset.to_bytes(4, "little"))
                continue
            if frame_end == ZCRCW:
                self.send_hex_header(Frame.ZACK, offset.to_bytes(4, "little"))
                return
            if frame_end == ZCRCE:
                return

    def _close_file(self) -> None:
        if self._handle is None:
            return
        current = self._current or {}
        try:
            self._handle.close()  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass
        self._handle = None
        self._current = None
        received = int(current.get("received", 0))
        self.on_file_done(str(current.get("name", "")), received, str(current.get("path", "")))

    def _check_cancel(self) -> None:
        if self.cancelled.is_set():
            raise ZmodemCancelled()


# ---------------------------------------------------------------------------
# Sender (local files → remote ``rz``)
# ---------------------------------------------------------------------------


class Sender(Codec):
    """Send local files to a remote ``rz``."""

    def __init__(
        self,
        link: Link,
        *,
        files: Sequence[str],
        cancelled: threading.Event | None = None,
        on_file: Callable[[str, int], None] | None = None,
        on_progress: Progress | None = None,
        on_file_done: Callable[[str, int, str], None] | None = None,
    ) -> None:
        super().__init__(link, escape_ctrl=True)
        self.files = list(files)
        self.cancelled = cancelled or threading.Event()
        self.on_file = on_file or (lambda name, size: None)
        self.on_progress = on_progress or (lambda done, total: None)
        self.on_file_done = on_file_done or (lambda name, size, path: None)
        self.buffer_size = DEFAULT_BUFFER
        self.rx_flags = 0
        self.sent: list[dict[str, object]] = []

    # -- public -----------------------------------------------------------
    def run(self, first: Header | None = None) -> list[dict[str, object]]:
        zrinit = first if first is not None and first.frame == Frame.ZRINIT else None
        if zrinit is None:
            zrinit = self._await_zrinit()
        self._consume_zrinit(zrinit)
        remaining_bytes = 0
        for path in self.files:
            try:
                remaining_bytes += os.path.getsize(path)
            except OSError:
                pass
        total_files = len(self.files)
        for index, path in enumerate(self.files):
            self._check_cancel()
            try:
                size = os.path.getsize(path)
            except OSError as exc:
                raise ZmodemError(f"无法读取 {path}：{exc}") from exc
            self._send_file(path, size, total_files - index, remaining_bytes)
            remaining_bytes = max(0, remaining_bytes - size)
        self._close_session()
        return self.sent

    def cancel(self) -> None:
        self.cancelled.set()
        self.send_abort()
        self.link.cancel()

    # -- handshake --------------------------------------------------------
    def _await_zrinit(self) -> Header:
        self.send_hex_header(Frame.ZRQINIT)
        deadline = time.monotonic() + 45.0
        while time.monotonic() < deadline:
            self._check_cancel()
            try:
                header = self.read_header(12.0)
            except ZmodemTimeout:
                self.send_hex_header(Frame.ZRQINIT)
                continue
            if header.frame == Frame.ZRINIT:
                return header
            if header.frame in (Frame.ZCAN, Frame.ZABORT):
                raise ZmodemAborted()
        raise ZmodemTimeout("等待远端 rz 响应超时，请确认远端已在接收")

    def _consume_zrinit(self, header: Header) -> None:
        self.rx_flags = header.flags
        self.tx_crc32 = bool(self.rx_flags & CANFC32)
        size = int.from_bytes(header.data[0:2], "little")
        if size:
            self.buffer_size = max(1024, min(DEFAULT_BUFFER, size))

    # -- sending ----------------------------------------------------------
    def _send_file(self, path: str, size: int, files_remaining: int, bytes_remaining: int) -> None:
        name = os.path.basename(path)
        info = FileInfo(name=name, size=size, mtime=int(os.path.getmtime(path)), mode=0o100644)
        self.on_file(name, size)
        payload = build_zfile(info, files_remaining, bytes_remaining)
        for attempt in range(4):
            self._check_cancel()
            self.send_bin_header(Frame.ZFILE, bytes([0, 0, 0, ZCBIN]))
            self.send_subpacket(payload, ZCRCW)
            header = self._wait_header({Frame.ZRPOS, Frame.ZSKIP, Frame.ZNAK, Frame.ZCAN, Frame.ZABORT, Frame.ZFIN}, 30.0)
            if header.frame == Frame.ZNAK:
                continue
            if header.frame == Frame.ZRPOS:
                break
            if header.frame == Frame.ZSKIP:
                raise ZmodemAborted("远端跳过了该文件")
            if header.frame == Frame.ZFIN:
                self._reply_zfin()
                raise ZmodemClosed("远端提前结束了传输")
            raise ZmodemAborted()
        else:
            raise ZmodemProtocolError("远端一直拒绝文件头")

        offset = max(0, header.offset)
        self._stream(path, offset, size)
        self._finish_file(path, size, name)

    def _stream(self, path: str, offset: int, size: int) -> int:
        """Send data from ``offset`` as one unacknowledged frame.

        lrzsz would insert ZCRCW checkpoints, but zmodem.js does not answer
        them, so the whole file goes out as ZCRCG subpackets terminated by
        ZCRCE.  The SSH channel provides the flow control; a checksum failure
        is recovered from via the ZRPOS retry in :meth:`_finish_file`.
        """
        chunk_size = max(1024, min(self.buffer_size, MAX_SUBPACKET))
        sent = offset
        with open(path, "rb") as handle:
            handle.seek(offset)
            self.send_bin_header(Frame.ZDATA, sent.to_bytes(4, "little"))
            last_report = 0.0
            while sent < size:
                self._check_cancel()
                block = handle.read(min(chunk_size, size - sent))
                if not block:
                    break
                at_end = sent + len(block) >= size
                self.send_subpacket(block, ZCRCE if at_end else ZCRCG)
                sent += len(block)
                now = time.monotonic()
                if now - last_report > 0.12:
                    last_report = now
                    self.on_progress(sent, size)
            if sent == offset:
                # Empty file (or resume at EOF): close the frame explicitly.
                self.send_subpacket(b"", ZCRCE)
            self.on_progress(sent, size)
            return sent

    def _finish_file(self, path: str, size: int, name: str) -> None:
        for _ in range(6):
            self._check_cancel()
            self.send_bin_header(Frame.ZEOF, size.to_bytes(4, "little"))
            header = self._wait_header({Frame.ZRINIT, Frame.ZRPOS, Frame.ZFIN, Frame.ZSKIP, Frame.ZCAN, Frame.ZABORT}, 25.0)
            if header.frame == Frame.ZRINIT:
                self.on_progress(size, size)
                self.on_file_done(name, size, path)
                self.sent.append({"name": name, "size": size, "path": path})
                return
            if header.frame == Frame.ZRPOS:
                self._stream(path, max(0, header.offset), size)
                continue
            if header.frame == Frame.ZFIN:
                self._reply_zfin()
                raise ZmodemClosed("远端提前结束了传输")
            if header.frame == Frame.ZSKIP:
                raise ZmodemAborted("远端跳过了该文件")
            raise ZmodemAborted()
        raise ZmodemProtocolError("远端没有确认文件结束")

    def _close_session(self) -> None:
        self.send_hex_header(Frame.ZFIN)
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            self._check_cancel()
            try:
                header = self.read_header(8.0)
            except ZmodemTimeout:
                break
            if header.frame == Frame.ZFIN:
                self.link.write(OVER_AND_OUT)
                return
            if header.frame in (Frame.ZCAN, Frame.ZABORT):
                return
        self.link.write(OVER_AND_OUT)

    def _reply_zfin(self) -> None:
        self.send_hex_header(Frame.ZFIN)
        self.link.write(OVER_AND_OUT)

    def _wait_header(self, wanted: set[int], timeout: float) -> Header:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._check_cancel()
            header = self.read_header(max(1.0, deadline - time.monotonic()))
            if header.frame in wanted:
                return header
            if header.frame in (Frame.ZCAN, Frame.ZABORT):
                raise ZmodemAborted()
        raise ZmodemTimeout()

    def _check_cancel(self) -> None:
        if self.cancelled.is_set():
            raise ZmodemCancelled()
