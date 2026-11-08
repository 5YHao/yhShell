"""A fake OpenAI-compatible streaming endpoint for local end-to-end tests.

The first assistant turn asks to run ``df -h && free -m`` through a tool call.
Once a tool result is present, it streams a short final answer.  This exercises
yhShell's streaming parser, tool-call assembly, approval flow and follow-up
round trip without touching a real model provider.

Usage:  python tests/fake_llm.py [--port 8900]
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


def sse(payload: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


def chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
    return {
        "id": "chatcmpl-yhtest",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": "mock-agent",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


FINAL_ANSWER = """磁盘和内存看起来都在健康范围：

- **磁盘**：根分区使用率约 42%，还有充足空间。
- **内存**：可用内存约 1.2 GB，没有明显压力。

暂时不需要清理。如果使用率继续上涨，可以再检查 `/var/log` 里的大文件。"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: ANN401
        sys.stderr.write("[fake-llm] " + (fmt % args) + "\n")

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            return {}

    def do_POST(self) -> None:  # noqa: N802
        payload = self._read_json()
        messages = payload.get("messages") or []
        stream = bool(payload.get("stream"))
        has_tool_result = any(message.get("role") == "tool" for message in messages)
        if not stream:
            body = json.dumps(
                {
                    "id": "chatcmpl-yhtest",
                    "object": "chat.completion",
                    "model": "mock-agent",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "pong"},
                            "finish_reason": "stop",
                        }
                    ],
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()

        def write(data: bytes) -> None:
            self.wfile.write(data)
            self.wfile.flush()

        try:
            write(sse(chunk({"role": "assistant", "content": ""})))
            time.sleep(0.15)
            if not has_tool_result:
                write(
                    sse(
                        chunk(
                            {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_yhtest_df",
                                        "type": "function",
                                        "function": {
                                            "name": "run_command",
                                            "arguments": json.dumps(
                                                {
                                                    "command": "df -h && free -m",
                                                    "explanation": "查看磁盘使用率与内存占用，判断是否需要清理。",
                                                },
                                                ensure_ascii=False,
                                            ),
                                        },
                                    }
                                ]
                            }
                        )
                    )
                )
                time.sleep(0.1)
                write(sse(chunk({}, finish="tool_calls")))
            else:
                for piece in ["磁盘", "和内存", "看起来", "都在健康范围。"]:
                    write(sse(chunk({"content": piece})))
                    time.sleep(0.08)
                write(sse(chunk({"content": "\n\n"})))
                for piece in ["- 根分区使用率 **42%**\n", "- 可用内存 **1.2 GB**\n"]:
                    write(sse(chunk({"content": piece})))
                    time.sleep(0.08)
                write(sse(chunk({"content": "\n暂时不需要清理。"})))
                write(sse(chunk({}, finish="stop")))
            write(b"data: [DONE]\n\n")
        except (BrokenPipeError, ConnectionResetError):
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8900)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"fake llm on http://127.0.0.1:{args.port}/v1", flush=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
