"""A tiny SSH server used only for local end-to-end tests.

It accepts password auth (tester/test) and bridges every shell channel to a
BusyBox ``sh`` process.  BusyBox provides the POSIX surface that yhShell's
agent command runner needs (printf, brace groups, exit status) without needing
WSL or Docker on the test machine.

Usage:  python tests/fake_sshd.py [--port 2222]
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import tempfile
import threading

import paramiko

HERE = os.path.dirname(os.path.abspath(__file__))
HOST_KEY_PATH = os.path.join(HERE, "test_host_key")
BUSYBOX = os.environ.get("YHS_BUSYBOX") or os.path.join(tempfile.gettempdir(), "busybox64.exe")

SANDBOX_FILES = {
    "README.txt": "yhShell 测试服务器\n\n这是一个本地假 SSH 服务，只用于开发测试。\n",
    "deploy.sh": "#!/bin/sh\necho deploying...\necho done\n",
    "logs/app.log": "2026-11-06 01:00:00 INFO started\n2026-11-06 01:02:11 ERROR connection refused\n",
}


def ensure_host_key() -> paramiko.RSAKey:
    if os.path.exists(HOST_KEY_PATH):
        return paramiko.RSAKey(filename=HOST_KEY_PATH)
    key = paramiko.RSAKey.generate(2048)
    key.write_private_key_file(HOST_KEY_PATH)
    return key


def make_sandbox() -> str:
    root = tempfile.mkdtemp(prefix="yhshell-fake-")
    for name, content in SANDBOX_FILES.items():
        path = os.path.join(root, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
    return root


def echo_input(data: bytes) -> bytes:
    """Fake the echo a real PTY performs, so the terminal feels interactive."""
    out = bytearray()
    for byte in data:
        if byte in (10, 13):  # Enter
            out.extend(b"\r\n")
        elif byte == 127:  # Backspace
            out.extend(b"\b \b")
        elif byte == 3:  # Ctrl-C
            out.extend(b"^C\r\n")
        elif 32 <= byte < 127 or byte in (9,):
            out.append(byte)
    return bytes(out)


class Server(paramiko.ServerInterface):
    def __init__(self) -> None:
        self.username = ""

    def check_auth_password(self, username: str, password: str) -> int:
        if username == "tester" and password == "test":
            self.username = username
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_auth_publickey(self, username: str, key) -> int:  # noqa: ANN001
        if username == "tester":
            self.username = username
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def get_allowed_auths(self, username: str) -> str:
        return "password,publickey"

    def check_channel_request(self, kind: str, chanid: int) -> int:
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_pty_request(self, channel, term, width, height, pixelwidth, pixelheight, modes) -> bool:  # noqa: ANN001
        return True

    def check_channel_window_change_request(self, channel, width, height, pixelwidth, pixelheight) -> bool:  # noqa: ANN001
        return True

    def check_channel_shell_request(self, channel) -> bool:  # noqa: ANN001
        return True

    def check_channel_exec_request(self, channel, command) -> bool:  # noqa: ANN001
        return False


def serve_channel(channel: paramiko.Channel, sandbox: str) -> None:
    env = {
        "PATH": os.path.dirname(BUSYBOX) + os.pathsep + os.environ.get("PATH", ""),
        "HOME": sandbox,
        "PS1": "$ ",
        "PS2": "> ",
        "TERM": "xterm-256color",
    }
    process = subprocess.Popen(
        [BUSYBOX, "sh", "-i"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
        cwd=sandbox,
        env=env,
    )
    echo_state = {"enabled": True}

    # BusyBox only prints PS1 after its first line of input, while a real login
    # shell shows a prompt immediately; send one empty line to match that.
    try:
        if process.stdin is not None:
            process.stdin.write(b"\n")
            process.stdin.flush()
    except Exception:  # noqa: BLE001
        pass

    def pump_input() -> None:
        while True:
            try:
                data = channel.recv(4096)
            except Exception:  # noqa: BLE001
                break
            if not data:
                break
            try:
                if echo_state["enabled"]:
                    channel.send(echo_input(data))
                # A real PTY maps CR to NL for the foreground process.
                payload = data.replace(b"\r", b"\n")
                if process.stdin is not None:
                    process.stdin.write(payload)
                    process.stdin.flush()
                if b"stty -echo" in data:
                    echo_state["enabled"] = False
                elif b"stty echo" in data:
                    echo_state["enabled"] = True
            except Exception:  # noqa: BLE001
                break
        try:
            if process.stdin is not None:
                process.stdin.close()
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=pump_input, daemon=True).start()
    assert process.stdout is not None
    fd = process.stdout.fileno()
    try:
        while True:
            try:
                data = os.read(fd, 4096)
            except OSError:
                break
            if not data:
                break
            # BusyBox writes LF only; a real PTY delivers CRLF to the client.
            data = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
            channel.send(data)
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            process.kill()
        except Exception:  # noqa: BLE001
            pass
        try:
            channel.close()
        except Exception:  # noqa: BLE001
            pass


def serve_connection(client: socket.socket, host_key: paramiko.RSAKey, sandbox: str) -> None:
    transport = paramiko.Transport(client)
    transport.add_server_key(host_key)
    server = Server()
    try:
        transport.start_server(server=server)
    except Exception:  # noqa: BLE001
        try:
            transport.close()
        except Exception:  # noqa: BLE001
            pass
        return
    while transport.is_active():
        try:
            channel = transport.accept(1.0)
        except Exception:  # noqa: BLE001
            channel = None
        if channel is None:
            continue
        threading.Thread(target=serve_channel, args=(channel, sandbox), daemon=True).start()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=2222)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    if not os.path.exists(BUSYBOX):
        print(f"busybox not found at {BUSYBOX}; set YHS_BUSYBOX", file=sys.stderr)
        return 2
    host_key = ensure_host_key()
    sandbox = make_sandbox()
    print(f"fake sshd on {args.host}:{args.port} as tester/test (sandbox {sandbox})", flush=True)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((args.host, args.port))
    sock.listen(32)
    while True:
        try:
            client, _addr = sock.accept()
        except KeyboardInterrupt:
            break
        threading.Thread(target=serve_connection, args=(client, host_key, sandbox), daemon=True).start()
    return 0


if __name__ == "__main__":
    sys.exit(main())
