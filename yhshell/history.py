"""Local archive for AI conversations.

Every conversation is stored as a single JSON document under
``%APPDATA%\\yhShell\\conversations``.  Files are written atomically and are
named by the conversation id (hex), so nothing user supplied ever becomes a
path.  The archive deliberately keeps the OpenAI-style message list plus the
tool metadata; this is what allows a conversation to be viewed or resumed
after an application restart.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from typing import Any

ID_PATTERN = set("0123456789abcdef")
MAX_LIST = 300


def _valid_id(value: str) -> bool:
    value = str(value or "")
    return 8 <= len(value) <= 40 and all(char in ID_PATTERN for char in value)


def sanitize_record(record: dict[str, Any]) -> dict[str, Any]:
    """Make a stored record safe to resume.

    A crash can leave an assistant ``tool_calls`` message without the matching
    tool result.  OpenAI-compatible endpoints reject such histories, so missing
    results are materialised as skipped tool messages.
    """
    messages = list(record.get("messages") or [])
    tool_meta = {str(key): dict(value) for key, value in (record.get("toolMeta") or {}).items() if isinstance(value, dict)}
    answered = {
        message.get("tool_call_id")
        for message in messages
        if isinstance(message, dict) and message.get("role") == "tool"
    }
    cleaned: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        cleaned.append(message)
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            call_id = str(call.get("id") or "")
            if not call_id or call_id in answered:
                continue
            cleaned.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": "该命令未执行：对话已中断。",
                }
            )
            meta = tool_meta.get(call_id, {})
            meta.update(status="skipped", output="对话已中断，该命令未执行", exitCode=None)
            tool_meta[call_id] = meta
            answered.add(call_id)
    result = dict(record)
    result["messages"] = cleaned
    result["toolMeta"] = tool_meta
    return result


def record_entries(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert a stored record into the render-ready entries the chat UI uses."""
    tool_meta = record.get("toolMeta") or {}
    entries: list[dict[str, Any]] = []
    for message in record.get("messages") or []:
        role = message.get("role")
        if role == "user":
            entries.append({"role": "user", "text": message.get("content", "")})
        elif role == "assistant":
            text = message.get("content") or ""
            if text.strip():
                entries.append({"role": "assistant", "text": text})
        elif role == "tool":
            call_id = str(message.get("tool_call_id") or "")
            meta = tool_meta.get(call_id, {})
            entries.append(
                {
                    "role": "tool",
                    "callId": call_id,
                    "command": meta.get("command", ""),
                    "explanation": meta.get("explanation", ""),
                    "status": meta.get("status", "done"),
                    "output": meta.get("output", ""),
                    "exitCode": meta.get("exitCode"),
                    "dangerous": meta.get("dangerous", False),
                    "safe": meta.get("safe"),
                    "safeReason": meta.get("safeReason", ""),
                    "approval": meta.get("approval", ""),
                }
            )
    return entries


class ConversationArchive:
    def __init__(self, base_dir: str) -> None:
        self.directory = os.path.join(base_dir, "conversations")
        os.makedirs(self.directory, exist_ok=True)
        self._lock = threading.RLock()

    def _path(self, conversation_id: str) -> str:
        if not _valid_id(conversation_id):
            raise ValueError("无效的对话 ID")
        return os.path.join(self.directory, f"{conversation_id}.json")

    def save(self, record: dict[str, Any]) -> None:
        conversation_id = str(record.get("id") or "")
        path = self._path(conversation_id)
        payload = json.dumps(record, ensure_ascii=False, indent=1)
        with self._lock:
            fd, tmp_path = tempfile.mkstemp(prefix=".conversation-", suffix=".json", dir=self.directory)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                os.replace(tmp_path, path)
            finally:
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass

    def get(self, conversation_id: str) -> dict[str, Any] | None:
        try:
            path = self._path(conversation_id)
        except ValueError:
            return None
        with self._lock:
            try:
                with open(path, encoding="utf-8") as handle:
                    record = json.load(handle)
            except (OSError, ValueError):
                return None
        if not isinstance(record, dict):
            return None
        record.setdefault("id", conversation_id)
        return record

    def delete(self, conversation_id: str) -> bool:
        try:
            path = self._path(conversation_id)
        except ValueError:
            return False
        with self._lock:
            try:
                os.remove(path)
                return True
            except OSError:
                return False

    def list(self, limit: int = MAX_LIST) -> list[dict[str, Any]]:
        entries: list[tuple[float, str]] = []
        with self._lock:
            try:
                names = os.listdir(self.directory)
            except OSError:
                return []
            for name in names:
                if not name.endswith(".json") or name.startswith("."):
                    continue
                path = os.path.join(self.directory, name)
                try:
                    entries.append((os.path.getmtime(path), name))
                except OSError:
                    continue
        entries.sort(reverse=True)
        result: list[dict[str, Any]] = []
        for _mtime, name in entries[:limit]:
            record = self.get(name[:-5])
            if not record:
                continue
            messages = record.get("messages") or []
            preview = ""
            for message in messages:
                if message.get("role") == "user" and (message.get("content") or "").strip():
                    preview = str(message.get("content")).strip().replace("\n", " ")
                    break
            result.append(
                {
                    "id": record.get("id"),
                    "title": record.get("title") or preview[:60] or "未命名对话",
                    "host": record.get("host", ""),
                    "port": record.get("port", 0),
                    "username": record.get("username", ""),
                    "hostName": record.get("hostName", ""),
                    "model": record.get("model", ""),
                    "createdAt": record.get("createdAt") or 0,
                    "updatedAt": record.get("updatedAt") or 0,
                    "messageCount": sum(1 for message in messages if message.get("role") in ("user", "assistant")),
                    "preview": preview[:120],
                }
            )
        return result

    def to_markdown(self, record: dict[str, Any]) -> str:
        lines = [f"# {record.get('title') or 'yhShell 对话'}", ""]
        host = record.get("hostName") or record.get("host") or ""
        lines.append(f"- 主机：{host} ({record.get('username')}@{record.get('host')}:{record.get('port')})")
        lines.append(f"- 模型：{record.get('model') or '—'}")
        created = record.get("createdAt") or 0
        if created:
            lines.append(f"- 时间：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(created))}")
        lines.append("")
        tool_meta = record.get("toolMeta") or {}
        for message in record.get("messages") or []:
            role = message.get("role")
            if role == "user":
                lines.append("## 我")
                lines.append("")
                lines.append(str(message.get("content") or ""))
                lines.append("")
            elif role == "assistant":
                content = str(message.get("content") or "").strip()
                if content:
                    lines.append("## AI")
                    lines.append("")
                    lines.append(content)
                    lines.append("")
            elif role == "tool":
                meta = tool_meta.get(str(message.get("tool_call_id") or ""), {})
                lines.append(f"### 命令：{meta.get('command') or '(未知)'}")
                lines.append("")
                lines.append("```")
                lines.append(str(meta.get("output") or "").rstrip())
                lines.append("```")
                lines.append("")
        return "\n".join(lines)
