"""One ZMODEM transfer job: prompt the user, run the protocol, report progress.

The SSH session detects a ZMODEM handshake in the raw byte stream and hands the
bytes over to a :class:`ZmodemTransfer`.  Everything after that happens on a
worker thread; the UI polls :meth:`ZmodemTransfer.snapshot` for the panel.
"""

from __future__ import annotations

import itertools
import os
import threading
import time
from typing import Any, Callable

from . import zmodem


def default_download_dir() -> str:
    """``%USERPROFILE%\\Downloads\\yhShell`` (or the home directory)."""
    home = os.path.expanduser("~")
    downloads = os.path.join(home, "Downloads")
    base = downloads if os.path.isdir(downloads) else home
    return os.path.join(base, "yhShell")


def human_size(size: int | float) -> str:
    value = float(max(0, size))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


class ZmodemTransfer:
    """Drives a single ``sz`` (download) or ``rz`` (upload) exchange."""

    _counter = itertools.count(1)

    def __init__(
        self,
        link: zmodem.Link,
        *,
        settings: dict[str, Any],
        on_finish: Callable[["ZmodemTransfer"], None] | None = None,
    ) -> None:
        self.id = next(self._counter)
        self.link = link
        self.settings = dict(settings or {})
        self.cancelled = threading.Event()
        self.decided = threading.Event()
        self.options: dict[str, Any] | None = None

        self.phase = "starting"
        self.direction = ""
        self.files: list[str] = []
        self.file = ""
        self.file_size = 0
        self.file_done = 0
        self.file_base = 0
        self.done_bytes = 0
        self.total_bytes = 0
        self.directory = ""
        self.saved_to = ""
        self.message = ""
        self.error = ""
        self.started_at = time.time()
        self.transfer_started_at = 0.0
        self.finished_at = 0.0

        self._lock = threading.Lock()
        self._protocol: zmodem.Receiver | zmodem.Sender | None = None
        self._on_finish = on_finish
        self._thread = threading.Thread(target=self._run, name=f"zmodem-{self.id}", daemon=True)

    # ------------------------------------------------------------- control
    def start(self) -> None:
        self._thread.start()

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)

    def accept(self, options: dict[str, Any] | None = None) -> None:
        if self.phase != "prompt":
            return
        self.options = dict(options or {})
        self.decided.set()

    def cancel(self) -> None:
        self.cancelled.set()
        self.decided.set()
        protocol = self._protocol
        if protocol is not None:
            try:
                protocol.cancel()
            except Exception:  # noqa: BLE001 - cancellation is best effort
                pass
        else:
            try:
                zmodem.Codec(self.link).send_abort()
            except Exception:  # noqa: BLE001
                pass

    def dismiss(self) -> None:
        """Mark the finished transfer as acknowledged (the UI is done with it)."""
        with self._lock:
            self.message = ""
            self.finished_at = self.finished_at or time.time()

    # ------------------------------------------------------------- snapshot
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            elapsed = 0.0
            if self.transfer_started_at:
                end = self.finished_at or time.time()
                elapsed = max(0.001, end - self.transfer_started_at)
            speed = int(self.done_bytes / elapsed) if elapsed else 0
            percent = 0.0
            if self.file_size > 0:
                percent = min(100.0, 100.0 * self.file_done / self.file_size)
            elif self.phase == "done":
                percent = 100.0
            return {
                "id": self.id,
                "direction": self.direction,
                "phase": self.phase,
                "file": self.file,
                "files": list(self.files),
                "fileSize": self.file_size,
                "fileDone": self.file_done,
                "doneBytes": self.done_bytes,
                "totalBytes": self.total_bytes,
                "percent": round(percent, 1),
                "speed": speed,
                "directory": self.directory,
                "savedTo": self.saved_to,
                "message": self.message,
                "error": self.error,
                "startedAt": self.started_at,
                "finishedAt": self.finished_at,
                "confirm": self.phase == "prompt",
                "filesDone": len([name for name in self.files if name != self.file]),
            }

    # ----------------------------------------------------------------- run
    def _run(self) -> None:
        try:
            header = zmodem.read_initial_header(self.link, 15.0)
        except zmodem.ZmodemError as exc:
            self._finish("error", error=f"没有收到 ZMODEM 握手：{exc}")
            return
        if header.frame == zmodem.Frame.ZRQINIT:
            direction = "download"
        elif header.frame == zmodem.Frame.ZRINIT:
            direction = "upload"
        else:
            self._finish("error", error=f"意外的 ZMODEM 帧：{header.name}")
            return

        needs_prompt = direction == "upload" or bool(self.settings.get("confirm", False))
        self._set(
            direction=direction,
            directory=self._download_dir(),
            phase="prompt" if needs_prompt else "starting",
        )
        if not needs_prompt:
            self.options = {"directory": self._download_dir()}
            self.decided.set()
        elif not self.decided.wait(120.0):
            self._abort()
            self._finish("cancelled", message="等待确认超时，已取消")
            return
        if self.cancelled.is_set():
            self._abort()
            self._finish("cancelled", message="已取消传输")
            return
        try:
            if direction == "download":
                self._receive(header)
            else:
                self._send(header)
        except zmodem.ZmodemCancelled:
            self._finish("cancelled", message="已取消传输")
        except zmodem.ZmodemAborted as exc:
            self._finish("error", error=str(exc))
        except (zmodem.ZmodemTimeout, zmodem.ZmodemProtocolError) as exc:
            self._finish("error", error=str(exc))
        except OSError as exc:
            self._finish("error", error=f"文件读写失败：{exc}")
        except Exception as exc:  # noqa: BLE001 - never kill the worker silently
            self._finish("error", error=f"{exc.__class__.__name__}: {exc}")

    # ------------------------------------------------------------ download
    def _receive(self, header: zmodem.Header) -> None:
        directory = str((self.options or {}).get("directory") or self._download_dir())
        os.makedirs(directory, exist_ok=True)
        self._set(directory=directory, phase="transferring", transfer_started_at=time.time())
        receiver = zmodem.Receiver(
            self.link,
            directory=directory,
            cancelled=self.cancelled,
            on_file=self._on_file,
            on_progress=self._on_progress,
            on_file_done=self._on_file_done,
            overwrite=bool(self.settings.get("overwrite", False)),
        )
        self._protocol = receiver
        saved = receiver.run(header)
        if not saved:
            self._finish("done", message="没有文件需要接收")
            return
        names = [str(item.get("name", "")) for item in saved]
        label = names[0] if len(names) == 1 else f"{len(names)} 个文件"
        self._finish("done", message=f"已接收 {label}", saved_to=str(saved[-1].get("path", "")))

    # -------------------------------------------------------------- upload
    def _send(self, header: zmodem.Header) -> None:
        files = [str(path) for path in (self.options or {}).get("files") or []]
        valid = [path for path in files if os.path.isfile(path)]
        if not valid:
            self._abort()
            self._finish("cancelled", message="没有选择要发送的文件")
            return
        total = 0
        for path in valid:
            try:
                total += os.path.getsize(path)
            except OSError:
                pass
        self._set(phase="transferring", transfer_started_at=time.time(), total_bytes=total)
        sender = zmodem.Sender(
            self.link,
            files=valid,
            cancelled=self.cancelled,
            on_file=self._on_file,
            on_progress=self._on_progress,
            on_file_done=self._on_file_done,
        )
        self._protocol = sender
        sender.run(header)
        label = valid[0] if len(valid) == 1 else f"{len(valid)} 个文件"
        self._finish("done", message=f"已发送 {label}", saved_to=os.path.dirname(valid[0]))

    # ------------------------------------------------------------ callbacks
    def _on_file(self, name: str, size: int) -> None:
        with self._lock:
            self.file = name
            self.file_size = max(0, int(size))
            self.file_done = 0
            self.file_base = self.done_bytes
            self.files.append(name)

    def _on_progress(self, done: int, total: int) -> None:
        with self._lock:
            self.file_done = max(0, int(done))
            if total:
                self.file_size = int(total)
            self.done_bytes = self.file_base + self.file_done

    def _on_file_done(self, name: str, size: int, path: str) -> None:
        with self._lock:
            self.saved_to = path or self.saved_to
            self.file_done = max(0, int(size))
            self.file_size = self.file_done or self.file_size
            self.done_bytes = self.file_base + self.file_done

    # -------------------------------------------------------------- helpers
    def _download_dir(self) -> str:
        configured = str(self.settings.get("downloadDir") or "").strip()
        return configured or default_download_dir()

    def _set(self, **fields: Any) -> None:
        with self._lock:
            for key, value in fields.items():
                setattr(self, key, value)

    def _abort(self) -> None:
        try:
            zmodem.Codec(self.link).send_abort()
        except Exception:  # noqa: BLE001
            pass

    def _finish(self, phase: str, *, message: str = "", error: str = "", saved_to: str = "") -> None:
        with self._lock:
            self.phase = phase
            self.message = message
            self.error = error
            if saved_to:
                self.saved_to = saved_to
            if self.file_size and phase == "done":
                self.file_done = self.file_size
            self.finished_at = time.time()
        callback = self._on_finish
        if callback is not None:
            try:
                callback(self)
            except Exception:  # noqa: BLE001
                pass
