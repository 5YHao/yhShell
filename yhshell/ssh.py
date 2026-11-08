"""SSH transport: one interactive PTY that both the user and the AI agent drive.

There is only one channel.  The agent types its commands into the very shell
the user is looking at, waits for the shell to come back to a prompt, and reads
the result out of the same byte stream.  This is what makes the client usable
behind jump servers (JumpServer / Teleport / any bastion): the privileged shell
only exists *inside* the interactive session, so a second channel would land in
the bastion menu instead of the target host.

Because a plain interactive shell has no protocol, commands are wrapped in
echo-safe markers (``__YHS_<token>_START__`` / ``__YHS_<token>_END__ <rc>``)
that never appear verbatim in the typed line, and those marker lines are
filtered out of the UI stream so the user sees the command and its output only.
"""

from __future__ import annotations

import codecs
import os
import re
import socket
import threading
import time
import uuid
from typing import Any

import paramiko

from . import zmodem
from .transfer import ZmodemTransfer, human_size

STATUS_CONNECTING = "connecting"
STATUS_CONNECTED = "connected"
STATUS_CLOSED = "closed"
STATUS_ERROR = "error"

MAX_OUTPUT_BUFFER = 2 * 1024 * 1024
AGENT_BUFFER_LIMIT = 4 * 1024 * 1024

# Terminal state that matters when the agent wants to type into the shell.
_ALT_SCREEN_RE = re.compile(r"\x1b\[\?(?:1047|1048|1049|47)([hl])")
_MARKER_LINE_RE = re.compile(r"^__YHS_[0-9a-f]{6,}_[A-Z]+__(?:\s+-?\d+)?$")
# A shell prompt ends with one of these.  ">" is deliberately absent: jump
# server menus and tools like mysql/ftp also end with ">" and typing there is
# exactly what we must avoid.
_PROMPT_TAIL_RE = re.compile(r"[#$%❯]\s*$")
_PROMPT_LINE_RE = re.compile(r"^.{0,120}[#$%❯]\s*$")

_ANSI_RE = re.compile(
    r"""
    \x1b\[[0-?]*[ -/]*[@-~]      # CSI
    | \x1b\][^\x07]*(?:\x07|\x1b\\)  # OSC
    | \x1b[@-Z\\-_]              # other escapes
    | \r
    """,
    re.VERBOSE,
)


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def friendly_error(exc: BaseException) -> str:
    if isinstance(exc, paramiko.AuthenticationException):
        return "认证失败：用户名、密码或密钥不正确"
    if isinstance(exc, paramiko.BadHostKeyException):
        return "主机密钥校验失败：服务器密钥已变更"
    if isinstance(exc, paramiko.ssh_exception.NoValidConnectionsError):
        return "无法连接到主机：网络不可达或端口未开放"
    if isinstance(exc, paramiko.SSHException):
        message = str(exc).strip()
        return f"SSH 错误：{message}" if message else "SSH 连接失败"
    if isinstance(exc, socket.timeout):
        return "连接超时：请检查网络或防火墙设置"
    if isinstance(exc, FileNotFoundError):
        return "私钥文件不存在"
    if isinstance(exc, OSError):
        message = str(exc).strip()
        return f"网络错误：{message}" if message else "网络错误"
    message = str(exc).strip()
    return message or exc.__class__.__name__


class Session:
    """One interactive SSH shell."""

    def __init__(self, profile: dict[str, Any], cols: int = 120, rows: int = 32,
                 settings_provider: Any = None) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.profile = profile
        self.cols = max(20, int(cols or 120))
        self.rows = max(5, int(rows or 32))
        self.status = STATUS_CONNECTING
        self.error = ""
        self.fingerprint = ""
        self.created_at = time.time()
        self.connected_at = 0.0

        self._client: paramiko.SSHClient | None = None
        self._transport: paramiko.Transport | None = None
        self._channel: paramiko.Channel | None = None
        self._lock = threading.RLock()
        self._out = ""
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._closed = threading.Event()
        self._thread: threading.Thread | None = None

        # The agent shares this PTY: _agent_buf is a copy of the incoming text
        # that TerminalShell waits on, while _out is what xterm.js renders.
        self._agent = TerminalShell(self)
        self._agent_buf = ""
        self._agent_cond = threading.Condition(self._lock)
        self._last_output_at = 0.0
        self._alt_screen = False
        self._user_typing = False
        self._ui_filter = False
        self._ui_pending = ""
        self._noise_lines: tuple[str, ...] = ()

        # ZMODEM (rz/sz) support: raw bytes are routed to a transfer link as
        # soon as a handshake header shows up in the stream.
        self._settings_provider = settings_provider or (lambda: {})
        self._link: zmodem.Link | None = None
        self._scan = b""
        self._transfer: ZmodemTransfer | None = None

    # ----------------------------------------------------------- lifecycle
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name=f"ssh-{self.id}", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            self._connect()
        except BaseException as exc:  # noqa: BLE001 - surfaced to the UI
            if not self._closed.is_set():
                self._set_error(friendly_error(exc))
                self._append(f"\r\n\x1b[31m[连接失败] {friendly_error(exc)}\x1b[0m\r\n")
            self._teardown()
            return

        if self._closed.is_set():
            self._teardown()
            return

        try:
            self._read_loop()
        except BaseException as exc:  # noqa: BLE001
            if not self._closed.is_set():
                self._set_error(friendly_error(exc))
        finally:
            with self._lock:
                was_error = self.status == STATUS_ERROR
                if not was_error:
                    self.status = STATUS_CLOSED
            if not was_error:
                self._append("\r\n\x1b[90m[连接已关闭]\x1b[0m\r\n")
            self._teardown()

    def _connect(self) -> None:
        profile = self.profile
        client = paramiko.SSHClient()
        try:
            client.load_system_host_keys()
        except Exception:  # noqa: BLE001 - known_hosts is best effort
            pass
        known_hosts = os.path.join(os.path.expanduser("~"), ".ssh", "known_hosts")
        if os.path.exists(known_hosts):
            try:
                client.load_host_keys(known_hosts)
            except Exception:  # noqa: BLE001
                pass
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        password = str(profile.get("password") or "")
        passphrase = str(profile.get("passphrase") or "")
        auth = profile.get("auth") or "password"
        key_path = str(profile.get("keyPath") or "")

        pkey = None
        if auth == "key":
            if not key_path:
                raise ValueError("未配置私钥文件路径")
            pkey = paramiko.PKey.from_path(key_path, passphrase or None)

        client.connect(
            hostname=str(profile.get("host") or ""),
            port=int(profile.get("port") or 22),
            username=str(profile.get("username") or ""),
            password=password if auth == "password" and password else None,
            pkey=pkey,
            allow_agent=auth == "agent",
            look_for_keys=auth == "agent",
            timeout=15,
            banner_timeout=20,
            auth_timeout=25,
            channel_timeout=25,
        )
        self._client = client
        transport = client.get_transport()
        if transport is None:
            raise paramiko.SSHException("SSH transport 不可用")
        transport.set_keepalive(30)
        self._transport = transport
        try:
            key = transport.get_remote_server_key()
            digest = key.get_fingerprint()
            self.fingerprint = f"{key.get_name()} {digest[:16]}…"
        except Exception:  # noqa: BLE001 - fingerprint is cosmetic
            self.fingerprint = ""

        channel = client.invoke_shell(term="xterm-256color", width=self.cols, height=self.rows)
        channel.settimeout(None)
        self._channel = channel
        with self._lock:
            self.status = STATUS_CONNECTED
            self.connected_at = time.time()

    def _read_loop(self) -> None:
        assert self._channel is not None
        channel = self._channel
        while not self._closed.is_set():
            try:
                data = channel.recv(65536)
            except socket.timeout:
                continue
            except Exception as exc:  # noqa: BLE001
                if self._closed.is_set():
                    return
                raise exc
            if not data:
                return
            self._handle_bytes(data)

    # ------------------------------------------------------- zmodem (rz/sz)
    def _handle_bytes(self, data: bytes) -> None:
        """Forward terminal text, or hand raw bytes to an active transfer."""
        with self._lock:
            link = self._link
            if link is not None:
                link.feed(data)
                return
        buffer = self._scan + data
        self._scan = b""
        start = 0
        while True:
            index = buffer.find(b"\x2a\x18", start)  # ZPAD ZDLE starts every header
            if index < 0:
                # Only a trailing ZPAD can begin a header in the next packet.
                # Keeping every final byte delays interactive echo by one key.
                keep = 1 if buffer.endswith(b"*") else 0
                end = len(buffer) - keep
                if end:
                    self._append(self._decoder.decode(buffer[:end]))
                self._scan = buffer[end:]
                return
            if len(buffer) < index + 3:
                if index:
                    self._append(self._decoder.decode(buffer[:index]))
                self._scan = buffer[index:]
                return
            if buffer[index + 2] not in (zmodem.ZBIN, zmodem.ZHEX, zmodem.ZBIN32):
                # Not a header: continue within this packet instead of waiting
                # for another packet to release the remaining terminal text.
                start = index + 2
                continue
            if index:
                self._append(self._decoder.decode(buffer[:index]))
            self._begin_transfer(buffer[index:])
            return

    def _begin_transfer(self, payload: bytes) -> None:
        try:
            self._decoder.reset()
        except Exception:  # noqa: BLE001 - reset is cosmetic
            pass
        link = zmodem.Link(self._send_bytes)
        link.feed(payload)
        try:
            settings = dict(self._settings_provider() or {})
        except Exception:  # noqa: BLE001 - settings are best effort
            settings = {}
        transfer = ZmodemTransfer(link, settings=settings, on_finish=self._transfer_finished)
        with self._lock:
            self._link = link
            self._transfer = transfer
        transfer.start()

    def _send_bytes(self, data: bytes) -> None:
        channel = self._channel
        if channel is None or self.status != STATUS_CONNECTED:
            raise OSError("SSH 连接已断开")
        channel.send(data)

    def _transfer_finished(self, transfer: ZmodemTransfer) -> None:
        with self._lock:
            if self._link is None:
                return
            leftover = self._link.leftover()
            self._link = None
            self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        summary = self._transfer_summary(transfer)
        if summary:
            self._append(summary)
        if leftover:
            self._append(self._decoder.decode(leftover))

    @staticmethod
    def _transfer_summary(transfer: ZmodemTransfer) -> str:
        snapshot = transfer.snapshot()
        phase = snapshot.get("phase")
        text = snapshot.get("message") or snapshot.get("error") or ""
        if not text:
            return ""
        color = "32" if phase == "done" else ("33" if phase == "cancelled" else "31")
        detail = ""
        if phase == "done":
            size = int(snapshot.get("doneBytes") or snapshot.get("fileSize") or 0)
            detail = f"（{human_size(size)}"
            saved = str(snapshot.get("savedTo") or "")
            if saved and snapshot.get("direction") == "download":
                detail += f" → {saved}"
            detail += "）"
        return f"\r\n\x1b[{color}m[ZMODEM] {text}{detail}\x1b[0m\r\n"

    def transfer_snapshot(self) -> dict[str, Any] | None:
        transfer = self._transfer
        return transfer.snapshot() if transfer is not None else None

    def transfer_accept(self, options: dict[str, Any] | None = None) -> bool:
        transfer = self._transfer
        if transfer is None:
            return False
        transfer.accept(options or {})
        return True

    def transfer_cancel(self) -> bool:
        transfer = self._transfer
        if transfer is None:
            return False
        transfer.cancel()
        return True

    def transfer_dismiss(self) -> None:
        with self._lock:
            self._transfer = None

    def _append(self, text: str) -> None:
        if not text:
            return
        with self._lock:
            self._agent_buf += text
            if len(self._agent_buf) > AGENT_BUFFER_LIMIT:
                self._agent_buf = self._agent_buf[-AGENT_BUFFER_LIMIT:]
            for match in _ALT_SCREEN_RE.finditer(text):
                self._alt_screen = match.group(1) == "h"
            self._last_output_at = time.time()
            visible = self._filter_ui(text)
            if visible:
                self._out += visible
                if len(self._out) > MAX_OUTPUT_BUFFER:
                    self._out = self._out[-MAX_OUTPUT_BUFFER:]
            self._agent_cond.notify_all()

    # ------------------------------------------------ agent stream helpers
    def _is_noise(self, line: str) -> bool:
        """True for the marker plumbing that the user should not see."""
        body = line.rstrip("\r")
        if _MARKER_LINE_RE.match(body):
            return True
        return any(body.endswith(script) for script in self._noise_lines)

    def _filter_ui(self, text: str) -> str:
        """Drop injected marker lines from what xterm.js renders.

        Only active while the agent owns the terminal, so ordinary typing is
        never delayed by line buffering.
        """
        if not self._ui_filter:
            return text
        pending = self._ui_pending + text
        lines = pending.split("\n")
        complete = lines[:-1]
        self._ui_pending = lines[-1]
        if len(self._ui_pending) > 8192:
            complete.append(self._ui_pending)
            self._ui_pending = ""
        keep = [line for line in complete if not self._is_noise(line)]
        if not keep:
            return ""
        return "\n".join(keep) + "\n"

    def flush_ui(self) -> None:
        with self._lock:
            pending, self._ui_pending = self._ui_pending, ""
            if pending and not self._is_noise(pending):
                self._out += pending

    def agent_wait_for(self, pattern: "re.Pattern[str]", deadline: float) -> tuple[Any, str]:
        """Wait for a marker in the shared stream; returns (match, buffer)."""
        with self._agent_cond:
            while True:
                text = self._agent_buf
                match = pattern.search(text)
                if match is not None:
                    # Keep whatever follows the marker (usually the new prompt).
                    self._agent_buf = text[match.end():]
                    return match, text
                remaining = deadline - time.time()
                if remaining <= 0 or self._closed.is_set():
                    self._agent_buf = ""
                    return None, text
                self._agent_cond.wait(min(0.2, remaining))

    def _set_error(self, message: str) -> None:
        with self._lock:
            self.status = STATUS_ERROR
            self.error = message

    def _teardown(self) -> None:
        try:
            self._agent.close()
        except Exception:  # noqa: BLE001
            pass
        if self._channel is not None:
            try:
                self._channel.close()
            except Exception:  # noqa: BLE001
                pass
            self._channel = None
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001
                pass
            self._client = None
        self._transport = None

    def close(self) -> None:
        self._closed.set()
        with self._lock:
            if self.status not in (STATUS_ERROR, STATUS_CLOSED):
                self.status = STATUS_CLOSED
        transfer = self._transfer
        if transfer is not None:
            try:
                transfer.cancel()
            except Exception:  # noqa: BLE001
                pass
        if self._agent is not None:
            try:
                self._agent.close()
            except Exception:  # noqa: BLE001
                pass
        if self._channel is not None:
            try:
                self._channel.close()
            except Exception:  # noqa: BLE001
                pass
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------- io
    def drain(self) -> str:
        with self._lock:
            text, self._out = self._out, ""
            return text

    def write(self, data: str) -> bool:
        transfer = self._transfer
        if transfer is not None and transfer.phase in ("starting", "prompt", "transferring"):
            # Keystrokes would corrupt the binary stream; Ctrl-C means cancel.
            if "\x03" in data:
                transfer.cancel()
            return True
        self._track_typing(data)
        channel = self._channel
        if channel is None or self.status != STATUS_CONNECTED:
            return False
        try:
            channel.send(data.encode("utf-8", "replace"))
            return True
        except Exception as exc:  # noqa: BLE001
            if not self._closed.is_set():
                self._set_error(friendly_error(exc))
            return False

    def _track_typing(self, data: str) -> None:
        """Remember whether the user is in the middle of a command line."""
        if not data:
            return
        with self._lock:
            if "\r" in data or "\n" in data or "\x03" in data:
                self._user_typing = False
            else:
                self._user_typing = True

    def send_raw(self, data: str) -> None:
        """Write to the PTY without counting it as user typing (agent path)."""
        channel = self._channel
        if channel is None or self.status != STATUS_CONNECTED:
            raise RuntimeError("SSH 连接已断开")
        channel.send(data.encode("utf-8", "replace"))

    def resize(self, cols: int, rows: int) -> None:
        self.cols = max(20, int(cols or 120))
        self.rows = max(5, int(rows or 32))
        channel = self._channel
        if channel is None or self.status != STATUS_CONNECTED:
            return
        try:
            channel.resize_pty(width=self.cols, height=self.rows)
        except Exception:  # noqa: BLE001 - resize races with disconnect
            pass

    def info(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "error": self.error,
            "fingerprint": self.fingerprint,
            "host": self.profile.get("host"),
            "port": self.profile.get("port"),
            "username": self.profile.get("username"),
            "name": self.profile.get("name") or self.profile.get("host"),
        }

    # ------------------------------------------------------------- agent
    def agent_shell(self) -> "TerminalShell":
        """The runner that types agent commands into this very terminal."""
        with self._lock:
            if self._transport is None or self._channel is None:
                raise RuntimeError("SSH 连接已断开")
            return self._agent


class TerminalShell:
    """Runs agent commands by typing them into the interactive PTY.

    ``run`` waits until the shell looks idle at a prompt, types the command
    wrapped in markers, and waits for the closing marker, which carries the
    exit status.  Because this is the same channel the user types into, the
    user sees every command and every byte of output; only the marker plumbing
    is filtered out of the rendered stream.
    """

    IDLE_SECONDS = 0.4
    PROMPT_WAIT = 12.0

    def __init__(self, session: "Session") -> None:
        self._session = session
        self._run_lock = threading.Lock()
        self._prompt_hint = ""

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _unfinished_syntax(command: str) -> str:
        """Why typing this line would leave the shell waiting for more input."""
        state = ""
        index = 0
        length = len(command)
        while index < length:
            char = command[index]
            if state == "":
                if char == "\\":
                    index += 2
                    continue
                if char == "'":
                    state = "'"
                elif char == '"':
                    state = '"'
            elif state == "'":
                if char == "'":
                    state = ""
            else:
                if char == "\\":
                    index += 2
                    continue
                if char == '"':
                    state = ""
            index += 1
        if state:
            return f"命令里的 {state} 引号没有闭合，直接输入会让终端停在续行提示符；请修正后再执行"
        tail = command.rstrip()
        for token in ("&&", "||", "|", "("):
            if tail.endswith(token):
                return f"命令以 {token} 结尾，会让终端停在续行提示符；请补全命令"
        return ""

    def _blocked_reason(self) -> str:
        """'' when the terminal looks ready for input, else why it does not."""
        session = self._session
        with session._agent_cond:
            if session.status != STATUS_CONNECTED:
                return "SSH 连接已断开"
            transfer = session._transfer
            if transfer is not None and transfer.phase in ("starting", "prompt", "transferring"):
                return "正在传输文件（ZMODEM），先等传输结束"
            if session._user_typing:
                return "你正在终端里输入，请先按 Enter 或 Ctrl-C 再让 AI 执行"
            if session._alt_screen:
                return "终端里开着全屏程序（vim / top / less 等），先退出再执行"
            if session._agent_buf and time.time() - session._last_output_at < self.IDLE_SECONDS:
                return "终端还在输出"
            tail = strip_ansi(session._agent_buf).replace("\r", "")
            lines = [line for line in tail.split("\n") if line.strip()]
            if not lines:
                return "还没有收到 shell 提示符"
            last = lines[-1].rstrip()
            if self._prompt_hint and last.endswith(self._prompt_hint):
                return ""
            if _PROMPT_TAIL_RE.search(last):
                return ""
            return f"终端停在非提示符状态（最后一行：{last[-60:]}）"

    def _wait_ready(self, timeout: float) -> None:
        deadline = time.time() + max(3.0, timeout)
        reason = "终端还没有准备好"
        while True:
            reason = self._blocked_reason()
            if not reason:
                return
            if time.time() >= deadline:
                raise RuntimeError(reason)
            time.sleep(0.12)

    def _learn_prompt(self, tail: str) -> None:
        text = strip_ansi(tail).replace("\r", "")
        for line in text.split("\n"):
            if line.strip():
                self._prompt_hint = line.rstrip()
                return

    def _prompt_at(self) -> str:
        """The prompt line sitting at the bottom of the terminal right now."""
        text = strip_ansi(self._session._agent_buf).replace("\r", "")
        for line in reversed(text.split("\n")):
            if line.strip():
                return line.rstrip()
        return ""

    @staticmethod
    def _clean_output(
        raw: str, start: str, command: str, noise: tuple[str, ...] = (), prompt: str = ""
    ) -> str:
        text = strip_ansi(raw)
        if start in text:
            text = text.split(start, 1)[1]
        lines = text.replace("\r", "").split("\n")
        while lines and not lines[0].strip():
            lines.pop(0)
        wanted = [line.strip() for line in command.split("\n") if line.strip()]
        # The shell echoes the command we typed; drop that echo.
        if lines and wanted and lines[0].rstrip().endswith(wanted[0]):
            lines.pop(0)
            for want in wanted[1:]:
                while lines and not lines[0].strip():
                    lines.pop(0)
                if lines and lines[0].strip() == want:
                    lines.pop(0)
        # Depending on how the PTY echoes the chunk, the injected script lines
        # can land after the start marker too; they are ours, not output.
        kept = [
            line
            for line in lines
            if not _MARKER_LINE_RE.match(line.rstrip())
            and not any(line.rstrip().endswith(script) for script in noise)
        ]
        while kept and not kept[-1].strip():
            kept.pop()
        # A prompt is printed before every line the shell reads, so the prompt
        # that was on screen when we started can wrap the output, or even show
        # up between output lines when the PTY echoes in pieces.
        if prompt:
            needle = prompt + " "
            cleaned: list[str] = []
            for line in kept:
                while line.startswith(needle):
                    line = line[len(needle):]
                if line.rstrip() == prompt:
                    continue
                cleaned.append(line)
            kept = cleaned
        # A trailing bare prompt (printed before the end marker) is not output.
        while kept and _PROMPT_LINE_RE.match(kept[-1].rstrip()):
            kept.pop()
        while kept and not kept[-1].strip():
            kept.pop()
        while kept and not kept[0].strip():
            kept.pop(0)
        return "\n".join(kept)

    def _interrupt(self) -> None:
        session = self._session
        try:
            session.send_raw("\x03")
        except Exception:  # noqa: BLE001
            return
        time.sleep(0.3)
        try:
            session.send_raw("\x03")
        except Exception:  # noqa: BLE001
            pass

    # ---------------------------------------------------------------- run
    def run(self, command: str, timeout: float = 90.0) -> dict[str, Any]:
        command = (command or "").strip("\n")
        if not command.strip():
            raise ValueError("命令为空")
        command = command.rstrip(" \t\r")
        if command.endswith("\\"):
            raise ValueError("命令以反斜杠结尾（续行），请改成单行命令")
        unfinished = self._unfinished_syntax(command)
        if unfinished:
            raise ValueError(unfinished)
        timeout = max(5.0, float(timeout))
        session = self._session

        with self._run_lock:
            self._wait_ready(self.PROMPT_WAIT)

            token = uuid.uuid4().hex[:8]
            start = f"__YHS_{token}_START__"
            end = f"__YHS_{token}_END__"
            # The typed line must not contain the marker as a contiguous
            # string, otherwise its own echo would look like the marker.
            start_line = "printf '\\n%s%s\\n' __YH S_" + token + "_START__"
            end_line = "printf '\\n%s%s %d\\n' __YH S_" + token + '_END__ "$?"'

            noise = (start_line, end_line)
            with session._agent_cond:
                prompt = self._prompt_at()
                session._agent_buf = ""
                session._noise_lines = noise
                session._ui_filter = True
                session._ui_pending = ""

            try:
                session.send_raw(start_line + "\n" + command + "\n" + end_line + "\n")
                pattern = re.compile(re.escape(end) + r"\s+(-?\d+)")
                match, text = session.agent_wait_for(pattern, time.time() + timeout)
            finally:
                with session._agent_cond:
                    session._ui_filter = False
                    session._noise_lines = ()
                session.flush_ui()

            if match is None:
                if session.status != STATUS_CONNECTED or session._closed.is_set():
                    raise RuntimeError("SSH 连接已断开")
                partial = self._clean_output(text, start, command, noise, prompt)
                self._interrupt()
                if partial:
                    partial += "\n"
                return {
                    "output": partial + "[命令超时，已在终端发送 Ctrl-C]",
                    "exitCode": -1,
                    "timedOut": True,
                }

            output = self._clean_output(text[: match.start()], start, command, noise, prompt)
            self._learn_prompt(text[match.end():])
            return {
                "output": output,
                "exitCode": int(match.group(1)),
                "timedOut": False,
            }

    def close(self) -> None:
        with self._session._agent_cond:
            self._session._agent_cond.notify_all()
