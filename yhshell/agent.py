"""OpenAI-compatible chat client and the agent loop that drives SSH commands.

The agent can propose shell commands through a ``run_command`` tool call.  The
UI decides whether a command may run (all, safe read-only, or ask every time);
approved commands are typed into the user's interactive terminal and the output
is fed back to the model until it produces a final answer.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from typing import Any, Callable

import requests

from . import security
from .history import ConversationArchive, sanitize_record
from .store import Store

SYSTEM_PROMPT = """你是 yhShell（一款桌面 SSH 客户端）内置的 AI 运维助手，正坐在一名工程师的终端旁边。

当前会话：
- 目标主机：{target}
- 远端环境：{env}
- 当前工作目录：{cwd}

工作规则：
1. 使用简体中文回答（用户使用其他语言时跟随用户），结论优先，简洁专业。
2. 需要读取或修改远端服务器时，必须调用 `run_command` 工具；工具一次执行一条命令（可用 && 串联），不要臆造命令输出。
3. 命令会先展示给用户确认再执行；执行时命令会直接输入到用户正在看的那个终端里（用户能看到），所以不要发送全屏交互程序（vim、less、top）或需要人工输入密码的命令。
4. 执行结果返回后，再根据结果继续推理，直到能给出完整结论。
5. 请主动使用只读命令（ls、cat、ps、df、systemctl status、journalctl 等）收集信息；修改类操作先说明目的与风险。
6. 严禁执行以下操作，除非用户明确要求且已确认风险：rm -rf 关键路径、mkfs、dd 写磁盘、修改防火墙/SSH 配置、重启或关机、清空数据。
7. 返回的命令输出可能被截断；如需更多内容，缩小范围重试。
8. 如果命令失败，先分析错误原因，不要盲目重试相同命令。"""

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "在用户当前的 SSH 终端里执行一条 shell 命令（命令会显示在终端中），并把 stdout/stderr 与退出码返回给模型。命令会先由用户确认（除非用户开启了自动执行）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "要执行的完整 shell 命令，单行，可用 && 串联多条命令。",
                    },
                    "explanation": {
                        "type": "string",
                        "description": "用一句简短中文说明这条命令的目的，展示给用户确认。",
                    },
                },
                "required": ["command", "explanation"],
            },
        },
    }
]

SAFE_PREFIXES = (
    "ls", "ll", "la", "cat", "head", "tail", "grep", "egrep", "fgrep", "rg",
    "find", "df", "du", "free", "ps", "uptime", "whoami", "id", "hostname",
    "uname", "pwd", "date", "stat", "wc", "file", "lsblk", "mount", "echo",
    "which", "type", "env", "printenv", "lscpu", "nproc", "vmstat", "iostat",
    "systemctl status", "systemctl is-active", "systemctl is-enabled",
    "journalctl", "docker ps", "docker images", "docker logs", "docker inspect",
    "git status", "git log", "git diff", "git show", "git branch", "git remote",
    "netstat", "ss", "ip addr", "ip route", "ip link", "ping", "curl -I",
    "curl -s", "dig", "nslookup", "getent", "top -b", "sensors", "lsblk -f",
    "openssl x509", "md5sum", "sha256sum", "awk", "sed -n", "sort", "uniq",
    "cut", "tr", "jq", "yq", "tree", "locate", "updatedb -n",
)

UNSAFE_TOKENS = (";", "&&", "||", "|", ">", "<", "`", "$(", "&", "\n")

# Short reasons used in the "why not automatic" hint shown on the tool card.
UNSAFE_TOKEN_REASONS = {
    ";": "含分号 ;",
    "&&": "含 &&",
    "||": "含 ||",
    "|": "含管道 |",
    ">": "含重定向 >",
    "<": "含重定向 <",
    "`": "含反引号 `",
    "$(": "含 $( )",
    "&": "含后台符号 &",
    "\n": "含换行",
}

# A whitelisted prefix is not enough for commands that can start another
# program (`env bash`, `find -exec ...`, `awk 'BEGIN{system(...)}'`).
LAUNCHER_GUARDS = (
    ("find", r"\s-(exec|execdir|ok|okdir)\b", "`find -exec` 可启动其他程序"),
    ("awk", r"\bsystem\s*\(", "`awk system()` 可启动其他程序"),
    ("sort", r"--compress-program", "`sort --compress-program` 可启动其他程序"),
)

DANGEROUS_PATTERNS = [
    r"rm\s+(-[a-zA-Z]*[rf][a-zA-Z]*\s+)*/(\s|$|\*)",
    r"rm\s+-rf\s+/(etc|usr|var|boot|home|root|bin|sbin|lib|opt|srv|sys|proc|dev)\b(?:/|\s|$)",
    r"\bmkfs(\.\w+)?\b",
    r"\bdd\b[^\n]*\bof=/dev/",
    r"\b(shutdown|reboot|poweroff|halt)\b",
    r"\binit\s+[06]\b",
    r":\(\)\s*\{.*\};\s*:",
    r"\bchmod\s+-R\s+777\s+/\b",
    r">\s*/dev/(sd|nvme|hd)",
    r"\b(curl|wget)\b[^\n]*\|\s*(ba)?sh",
    r"\buserdel\b|\bgroupdel\b",
    r"\b(iptables|nft)\b[^\n]*(-F|flush)",
]


class LLMError(RuntimeError):
    pass


def chat_endpoint(base_url: str) -> str:
    url = (base_url or "").strip().rstrip("/")
    if not url:
        raise LLMError("未配置模型 Base URL")
    if url.endswith("/chat/completions"):
        return url
    # Providers like Zhipu expose /v4/chat/completions, DeepSeek/OpenAI /v1/...
    if re.search(r"/v\d+$", url):
        return url + "/chat/completions"
    return url + "/v1/chat/completions"


def _headers(api_key: str) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "User-Agent": "yhShell/0.1",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _error_text(response: requests.Response) -> str:
    try:
        payload = response.json()
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict) and error.get("message"):
                return str(error["message"])
            if isinstance(error, str):
                return error
            if payload.get("message"):
                return str(payload["message"])
    except ValueError:
        pass
    text = (response.text or "").strip()
    return text[:600] or f"HTTP {response.status_code}"


def models_endpoint_candidates(base_url: str) -> list[str]:
    """Return the ``/models`` URLs worth trying for an OpenAI-compatible base URL."""
    url = (base_url or "").strip().rstrip("/")
    if not url:
        raise LLMError("未配置模型 Base URL")
    if url.endswith("/chat/completions"):
        url = url[: -len("/chat/completions")]
    if url.endswith("/models"):
        return [url]
    # Providers such as Zhipu expose /v4/models while DeepSeek/OpenAI use /v1.
    if re.search(r"/v\d+$", url):
        return [url + "/models"]
    return [url + "/v1/models", url + "/models"]


def _parse_models(payload: Any) -> list[str]:
    """Accept the common shapes: ``{"data": [...]}``, ``{"models": [...]}`` or a bare list."""
    items: Any = payload
    if isinstance(payload, dict):
        for key in ("data", "models", "result", "items"):
            if isinstance(payload.get(key), list):
                items = payload[key]
                break
        else:
            items = []
    if not isinstance(items, list):
        return []
    models: list[str] = []
    for item in items[:1000]:
        if isinstance(item, str):
            name = item.strip()
        elif isinstance(item, dict):
            name = str(item.get("id") or item.get("name") or item.get("model") or "").strip()
        else:
            continue
        name = name[:160]
        if name and name not in models:
            models.append(name)
    return models


def fetch_models(base_url: str, api_key: str, timeout: float = 20.0) -> dict[str, Any]:
    """Query an OpenAI-compatible ``/models`` endpoint and return the upstream model ids."""
    headers = {"Accept": "application/json", "User-Agent": "yhShell/0.1"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    endpoints = models_endpoint_candidates(base_url)
    last_error = ""
    for index, endpoint in enumerate(endpoints):
        try:
            response = requests.get(endpoint, headers=headers, timeout=(8, timeout))
        except requests.RequestException as exc:
            return {"ok": False, "message": f"请求失败：{exc}"}
        with response:
            if response.status_code == 404 and index < len(endpoints) - 1:
                last_error = f"HTTP 404：{endpoint}"
                continue
            if response.status_code >= 400:
                return {"ok": False, "message": f"HTTP {response.status_code}：{_error_text(response)}"}
            try:
                payload = response.json()
            except ValueError:
                return {"ok": False, "message": "上游返回的不是 JSON，可能不是 OpenAI 兼容接口"}

        models = _parse_models(payload)
        if not models:
            return {"ok": False, "message": "接口请求成功，但返回中没有模型列表"}
        return {"ok": True, "models": models, "endpoint": endpoint, "message": f"获取到 {len(models)} 个模型"}
    return {"ok": False, "message": last_error or "无法获取模型列表"}



def stream_chat(
    model: dict[str, Any],
    messages: list[dict[str, Any]],
    on_delta: Callable[[str], None],
    tools: list[dict[str, Any]] | None = None,
    timeout: float = 180.0,
    cancel: threading.Event | None = None,
) -> dict[str, Any]:
    api_key = security.unprotect(model.get("apiKey", ""))
    if not api_key and model["baseUrl"].lower().startswith("https://"):
        raise LLMError("当前模型没有配置 API Key，请到「设置 → AI 模型」填写")

    payload: dict[str, Any] = {
        "model": model["model"],
        "messages": messages,
        "temperature": float(model.get("temperature", 0.3)),
        "max_tokens": int(model.get("maxTokens", 2048)),
        "stream": True,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    content = ""
    calls: dict[int, dict[str, Any]] = {}

    try:
        response = requests.post(
            chat_endpoint(model["baseUrl"]),
            headers=_headers(api_key),
            json=payload,
            stream=True,
            timeout=(15, timeout),
        )
    except requests.RequestException as exc:
        raise LLMError(f"请求模型失败：{exc}") from exc

    with response:
        if response.status_code >= 400:
            raise LLMError(f"模型返回错误 {response.status_code}：{_error_text(response)}")
        for raw_line in response.iter_lines(decode_unicode=False):
            if cancel is not None and cancel.is_set():
                break
            if not raw_line:
                continue
            # Decode UTF-8 ourselves: requests' decode_unicode uses latin-1 when the
            # provider omits a charset, and str.splitlines() then breaks on bytes
            # such as 0x85 that appear inside multi-byte characters.
            line = raw_line.decode("utf-8", "replace").strip()
            if line.startswith(":") or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except ValueError:
                continue
            choices = chunk.get("choices") or []
            if not choices:
                continue
            choice = choices[0] or {}
            delta = choice.get("delta") or {}
            piece = delta.get("content")
            if piece:
                content += piece
                if cancel is None or not cancel.is_set():
                    on_delta(piece)
            for tool_delta in delta.get("tool_calls") or []:
                index = int(tool_delta.get("index", 0))
                slot = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                if tool_delta.get("id"):
                    slot["id"] = tool_delta["id"]
                function = tool_delta.get("function") or {}
                if function.get("name"):
                    slot["name"] = function["name"]
                if function.get("arguments"):
                    slot["arguments"] += function["arguments"]

    tool_calls = []
    for index in sorted(calls):
        slot = calls[index]
        if not slot["name"]:
            continue
        tool_calls.append(
            {
                "id": slot["id"] or f"call_{uuid.uuid4().hex[:12]}",
                "type": "function",
                "function": {"name": slot["name"], "arguments": slot["arguments"] or "{}"},
            }
        )
    return {"content": content, "tool_calls": tool_calls}


def test_model(model: dict[str, Any]) -> dict[str, Any]:
    api_key = security.unprotect(model.get("apiKey", ""))
    payload = {
        "model": model["model"],
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 8,
        "stream": False,
    }
    started = time.time()
    try:
        response = requests.post(
            chat_endpoint(model["baseUrl"]),
            headers={k: v for k, v in _headers(api_key).items() if k != "Accept"},
            json=payload,
            timeout=(10, 40),
        )
    except requests.RequestException as exc:
        return {"ok": False, "message": f"请求失败：{exc}"}
    latency = int((time.time() - started) * 1000)
    if response.status_code >= 400:
        return {"ok": False, "message": f"HTTP {response.status_code}：{_error_text(response)}"}
    try:
        data = response.json()
        reply = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    except ValueError:
        reply = ""
    return {"ok": True, "message": f"连接成功（{latency} ms）" + (f"：{reply[:60]}" if reply else ""), "latency": latency}


def split_stages(command: str) -> list[tuple[str, str]]:
    """Split ``command`` into ``(operator, stage)`` pairs.

    Unquoted ``|``, ``&&`` and ``||`` are the separators; the first pair has an
    empty operator.  Quotes and backslashes are respected, so ``grep -E "a|b"``
    stays one stage, and ``;`` is deliberately *not* a separator: a semicolon
    chain still asks for confirmation.
    """
    stages: list[tuple[str, str]] = []
    current: list[str] = []
    operator = ""
    quote = ""
    escaped = False
    index = 0
    length = len(command)
    while index < length:
        char = command[index]
        if escaped:
            current.append(char)
            escaped = False
            index += 1
            continue
        if char == "\\" and quote != "'":
            current.append(char)
            escaped = True
            index += 1
            continue
        if quote:
            current.append(char)
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in ("'", '"'):
            quote = char
            current.append(char)
            index += 1
            continue
        pair = command[index : index + 2]
        if pair in ("&&", "||"):
            stages.append((operator, "".join(current)))
            operator = pair
            current = []
            index += 2
            continue
        if char == "|":
            stages.append((operator, "".join(current)))
            operator = "|"
            current = []
            index += 1
            continue
        current.append(char)
        index += 1
    stages.append((operator, "".join(current)))
    return stages


def _launcher_reason(head: str, text: str) -> str:
    if head == "env" and len(text.split()) > 1:
        return "`env` 可启动其他程序"
    for command, pattern, reason in LAUNCHER_GUARDS:
        if head != command:
            continue
        if re.search(pattern, text, re.IGNORECASE):
            return reason
    return ""


def _stage_safety(stage: str) -> tuple[bool, str]:
    """Check one pipeline stage: no metacharacters, and a read-only prefix."""
    text = stage.strip()
    if not text:
        return False, "命令为空"
    for token in UNSAFE_TOKENS:
        if token in text:
            return False, UNSAFE_TOKEN_REASONS.get(token, f"含 {token}")
    lowered = text.lower()
    if not any(lowered == prefix or lowered.startswith(prefix + " ") for prefix in SAFE_PREFIXES):
        return False, f"`{text.split()[0]}` 不在只读白名单"
    reason = _launcher_reason(lowered.split()[0], text)
    if reason:
        return False, reason
    return True, ""


def command_safety(command: str) -> tuple[bool, str]:
    """Return ``(safe, reason)`` for ``command``; ``reason`` is '' when safe.

    Read-only stages may be chained: ``ps -eo ... | head`` and
    ``df -h && free -m`` are automatic, while ``ps | sh``, ``df -h && rm -rf /``
    or ``ls || xargs rm`` still need a click, because every stage is checked
    against the read-only whitelist and for the remaining metacharacters.
    """
    stripped = (command or "").strip()
    if not stripped:
        return False, "命令为空"
    stages = split_stages(stripped)
    if len(stages) == 1:
        return _stage_safety(stages[0][1].strip())
    for index, (operator, raw_stage) in enumerate(stages, start=1):
        if index == 1:
            label = ""
        elif operator == "|":
            label = f"管道第 {index} 段"
        else:
            label = f"`{operator}` 第 {index} 段"
        stage = raw_stage.strip()
        if not stage:
            return False, f"{label}为空" if label else "命令为空"
        safe, reason = _stage_safety(stage)
        if not safe:
            return False, f"{label}：{reason}" if label else reason
    return True, ""


def is_safe_command(command: str) -> bool:
    return command_safety(command)[0]


def is_dangerous_command(command: str) -> bool:
    for pattern in DANGEROUS_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return True
    return False


def parse_tool_arguments(raw: str) -> tuple[str, str]:
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        return raw.strip(), ""
    if not isinstance(data, dict):
        return str(data).strip(), ""
    command = str(data.get("command") or "").strip()
    explanation = str(data.get("explanation") or "").strip()
    return command, explanation


class Conversation:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.messages: list[dict[str, Any]] = []
        self.tool_meta: dict[str, dict[str, Any]] = {}
        self.pending: list[str] = []
        self.pending_calls: dict[str, dict[str, Any]] = {}
        self.decisions: dict[str, dict[str, str]] = {}
        self.decision_lock = threading.RLock()
        self.executing = False
        self.loop_lock = threading.Lock()
        self.busy = False
        self.cancel = threading.Event()
        self.env = ""
        self.steps = 0
        # Local archive identity (persisted in the conversations directory).
        self.record_id = ""
        self.created_at = 0.0
        self.updated_at = 0.0
        self.host = ""
        self.port = 0
        self.username = ""
        self.host_name = ""
        self.persist_warned = False

    def tool_message(self, call_id: str, text: str) -> dict[str, Any]:
        return {"role": "tool", "tool_call_id": call_id, "content": text}


class AgentService:
    def __init__(
        self,
        store: Store,
        emit: Callable[[dict[str, Any]], None],
        get_session: Callable[[str], Any],
        archive: ConversationArchive | None = None,
    ) -> None:
        self.store = store
        self.emit = emit
        self.get_session = get_session
        self.archive = archive
        self._conversations: dict[str, Conversation] = {}
        self._lock = threading.RLock()

    # -------------------------------------------------------------- state
    def conversation(self, session_id: str) -> Conversation:
        with self._lock:
            conv = self._conversations.get(session_id)
            if conv is None:
                conv = Conversation(session_id)
                self._conversations[session_id] = conv
            return conv

    def forget(self, session_id: str) -> None:
        with self._lock:
            conv = self._conversations.pop(session_id, None)
        if conv is not None:
            conv.cancel.set()

    def detach_record(self, conversation_id: str) -> None:
        """Forget the archive id of any live conversation that points at it."""
        with self._lock:
            conversations = list(self._conversations.values())
        for conv in conversations:
            if conv.record_id == conversation_id:
                conv.record_id = ""
                conv.created_at = 0.0
                conv.updated_at = 0.0

    def history(self, session_id: str) -> list[dict[str, Any]]:
        conv = self.conversation(session_id)
        entries: list[dict[str, Any]] = []
        for message in conv.messages:
            role = message.get("role")
            if role == "user":
                entries.append({"role": "user", "text": message.get("content", "")})
            elif role == "assistant":
                text = message.get("content") or ""
                if text.strip():
                    entries.append({"role": "assistant", "text": text})
            elif role == "tool":
                call_id = message.get("tool_call_id", "")
                meta = conv.tool_meta.get(call_id, {})
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
                    }
                )
        for call_id in list(conv.pending):
            meta = conv.tool_meta.get(call_id, {})
            entries.append(
                {
                    "role": "tool",
                    "callId": call_id,
                    "command": meta.get("command", ""),
                    "explanation": meta.get("explanation", ""),
                    "status": "pending",
                    "output": meta.get("output", ""),
                    "exitCode": None,
                    "dangerous": meta.get("dangerous", False),
                }
            )
        return entries

    def is_busy(self, session_id: str) -> bool:
        return self.conversation(session_id).busy

    # ------------------------------------------------------------- storage
    def _title(self, conv: Conversation) -> str:
        for message in conv.messages:
            if message.get("role") == "user":
                text = str(message.get("content") or "").strip()
                if text:
                    return text.splitlines()[0][:60]
        return "未命名对话"

    def _apply_session_identity(self, conv: Conversation, session: Any) -> None:
        profile = getattr(session, "profile", {}) or {}
        conv.host = str(profile.get("host") or "")
        conv.port = int(profile.get("port") or 0)
        conv.username = str(profile.get("username") or "")
        conv.host_name = str(profile.get("name") or "")

    def _persist(self, conv: Conversation) -> None:
        if self.archive is None or not conv.record_id:
            return
        conv.updated_at = time.time()
        model = self.store.get_model()
        record = {
            "version": 1,
            "id": conv.record_id,
            "title": self._title(conv),
            "host": conv.host,
            "port": conv.port,
            "username": conv.username,
            "hostName": conv.host_name,
            "model": (model or {}).get("model", ""),
            "createdAt": conv.created_at,
            "updatedAt": conv.updated_at,
            "messages": conv.messages,
            "toolMeta": conv.tool_meta,
        }
        try:
            self.archive.save(record)
        except Exception as exc:  # noqa: BLE001 - archive failure must not break chat
            if not conv.persist_warned:
                conv.persist_warned = True
                self.emit(
                    {
                        "type": "notice",
                        "session": conv.session_id,
                        "message": f"本地对话保存失败：{exc}",
                    }
                )

    def load(self, session_id: str, conversation_id: str) -> dict[str, Any]:
        if self.archive is None:
            return {"ok": False, "error": "本地对话存储不可用"}
        session = self.get_session(session_id)
        if session is None:
            return {"ok": False, "error": "请先连接服务器再继续历史对话"}
        conv = self.conversation(session_id)
        if conv.busy:
            return {"ok": False, "error": "AI 正在回复中，请稍候"}
        if conv.pending:
            return {"ok": False, "error": "还有待确认的命令，请先处理"}
        record = self.archive.get(conversation_id)
        if record is None:
            return {"ok": False, "error": "历史对话不存在或已被删除"}

        profile = getattr(session, "profile", {}) or {}
        same_host = (
            str(record.get("host") or "") == str(profile.get("host") or "")
            and int(record.get("port") or 0) == int(profile.get("port") or 0)
            and str(record.get("username") or "") == str(profile.get("username") or "")
        )
        if not same_host:
            return {"ok": False, "error": "该对话属于另一台主机，请先连接对应服务器"}

        cleaned = sanitize_record(record)
        conv.messages = list(cleaned.get("messages") or [])
        conv.tool_meta = dict(cleaned.get("toolMeta") or {})
        conv.pending.clear()
        conv.pending_calls.clear()
        with conv.decision_lock:
            conv.decisions.clear()
        conv.record_id = str(cleaned.get("id") or conversation_id)
        if conv.record_id != conversation_id:
            # The filename is authoritative; never trust an id from file content.
            conv.record_id = conversation_id
        conv.created_at = float(cleaned.get("createdAt") or time.time())
        conv.updated_at = float(cleaned.get("updatedAt") or conv.created_at)
        conv.host = str(cleaned.get("host") or profile.get("host") or "")
        conv.port = int(cleaned.get("port") or profile.get("port") or 0)
        conv.username = str(cleaned.get("username") or profile.get("username") or "")
        conv.host_name = str(cleaned.get("hostName") or profile.get("name") or "")
        conv.env = ""
        conv.steps = 0
        conv.persist_warned = False
        return {"ok": True, "entries": self.history(session_id)}

    # --------------------------------------------------------------- chat
    def send(self, session_id: str, text: str) -> dict[str, Any]:
        text = (text or "").strip()
        if not text:
            return {"ok": False, "error": "消息为空"}
        session = self.get_session(session_id)
        if session is None:
            return {"ok": False, "error": "会话不存在，请先连接服务器"}
        conv = self.conversation(session_id)
        if conv.busy:
            return {"ok": False, "error": "AI 正在回复中，请稍候"}
        if conv.pending:
            return {"ok": False, "error": "还有待确认的命令，请先处理"}

        conv.cancel.clear()
        conv.steps = 0
        if not conv.record_id:
            conv.record_id = uuid.uuid4().hex[:16]
            conv.created_at = time.time()
            conv.persist_warned = False
        if not conv.host:
            self._apply_session_identity(conv, session)
        conv.messages.append({"role": "user", "content": text})
        self._persist(conv)
        self._emit_state(conv, busy=True)
        threading.Thread(target=self._run_loop, args=(conv,), name=f"agent-{session_id}", daemon=True).start()
        return {"ok": True}

    def cancel(self, session_id: str) -> dict[str, Any]:
        conv = self.conversation(session_id)
        conv.cancel.set()
        with conv.decision_lock:
            conv.decisions.clear()
            pending = list(conv.pending)
        for call_id in pending:
            self._resolve_pending(conv, call_id, "skip", "")
        self._emit_state(conv, busy=False)
        self.emit({"type": "cancelled", "session": session_id})
        return {"ok": True}

    def resolve(self, session_id: str, call_id: str, action: str, command: str = "") -> dict[str, Any]:
        conv = self.conversation(session_id)
        if action not in ("run", "skip"):
            return {"ok": False, "error": "无效操作"}
        with conv.decision_lock:
            if call_id not in conv.pending:
                return {"ok": False, "error": "该命令已处理或不存在"}
            if action == "run" and command.strip():
                call = conv.pending_calls.get(call_id)
                meta = conv.tool_meta.get(call_id)
                if call is not None:
                    call["command"] = command.strip()
                if meta is not None:
                    meta["command"] = command.strip()
            conv.pending.remove(call_id)
            conv.decisions[call_id] = {"action": action, "command": command}
            start = not conv.executing
            if start:
                conv.executing = True
        if start:
            threading.Thread(target=self._drain_decisions, args=(conv,), name=f"agent-exec-{session_id}", daemon=True).start()
        return {"ok": True}

    def _drain_decisions(self, conv: Conversation) -> None:
        """Execute resolved commands one by one, then resume the model loop."""
        try:
            while True:
                with conv.decision_lock:
                    if conv.cancel.is_set():
                        conv.decisions.clear()
                    if not conv.decisions:
                        conv.executing = False
                        pending_left = bool(conv.pending)
                        break
                    call_id, decision = next(iter(conv.decisions.items()))
                    del conv.decisions[call_id]
                self._resolve_pending(conv, call_id, decision["action"], decision["command"])
            if not pending_left and not conv.cancel.is_set():
                self._run_loop(conv)
        finally:
            with conv.decision_lock:
                conv.executing = False

    def clear(self, session_id: str) -> None:
        with self._lock:
            conv = self._conversations.get(session_id)
        if conv is None:
            return
        conv.cancel.set()
        with conv.decision_lock:
            conv.decisions.clear()
            conv.pending.clear()
        conv.messages.clear()
        conv.tool_meta.clear()
        conv.pending_calls.clear()
        conv.steps = 0
        conv.busy = False
        conv.record_id = ""
        conv.created_at = 0.0
        conv.updated_at = 0.0
        conv.host = ""
        conv.port = 0
        conv.username = ""
        conv.host_name = ""
        conv.persist_warned = False

    # ------------------------------------------------------------- agent
    def _emit_state(self, conv: Conversation, busy: bool) -> None:
        conv.busy = busy
        self.emit({"type": "busy", "session": conv.session_id, "busy": busy})

    def _run_loop(self, conv: Conversation) -> None:
        if not conv.loop_lock.acquire(blocking=False):
            return
        try:
            self._run_loop_inner(conv)
        finally:
            conv.loop_lock.release()

    def _run_loop_inner(self, conv: Conversation) -> None:
        session_id = conv.session_id
        session = self.get_session(session_id)
        if session is None:
            self.emit({"type": "error", "session": session_id, "message": "会话已关闭"})
            self._emit_state(conv, busy=False)
            return

        self._emit_state(conv, busy=True)
        max_steps = int(self.store.settings.get("maxAgentSteps", 8))
        try:
            if not conv.env:
                conv.env = self._probe_env(session)
            while conv.steps < max_steps:
                if conv.cancel.is_set():
                    self.emit({"type": "cancelled", "session": session_id})
                    return
                conv.steps += 1
                model = self.store.get_model()
                if model is None:
                    raise LLMError("还没有可用的模型：请到「设置 → AI 模型」为供应商勾选模型")
                messages = self._api_messages(conv, session)
                result = stream_chat(
                    model,
                    messages,
                    on_delta=lambda text: self.emit({"type": "delta", "session": session_id, "text": text}),
                    tools=TOOLS,
                    timeout=float(self.store.settings.get("commandTimeout", 90)) + 90,
                    cancel=conv.cancel,
                )
                if conv.cancel.is_set():
                    self.emit({"type": "cancelled", "session": session_id})
                    return
                content = result["content"]
                tool_calls = result["tool_calls"]
                assistant: dict[str, Any] = {"role": "assistant", "content": content}
                if tool_calls:
                    assistant["tool_calls"] = [
                        {"id": call["id"], "type": "function", "function": call["function"]} for call in tool_calls
                    ]
                conv.messages.append(assistant)
                self._persist(conv)

                if not tool_calls:
                    if not content.strip():
                        self.emit(
                            {
                                "type": "notice",
                                "session": session_id,
                                "message": "模型返回了空回复。可以换个说法，或检查当前模型的接口配置。",
                            }
                        )
                    return

                auto, pending = self._plan_calls(conv, tool_calls)
                self._persist(conv)
                for call in auto:
                    self._execute_call(conv, call)
                if pending:
                    self._emit_state(conv, busy=False)
                    return
            else:
                self.emit(
                    {
                        "type": "notice",
                        "session": session_id,
                        "message": f"已达到单轮 {max_steps} 步自动执行上限，先停在这里。可以继续回复「继续」让我接着做。",
                    }
                )
        except LLMError as exc:
            self.emit({"type": "error", "session": session_id, "message": str(exc)})
        except Exception as exc:  # noqa: BLE001 - never lose the UI state
            self.emit({"type": "error", "session": session_id, "message": f"AI 处理失败：{exc}"})
        finally:
            self._emit_state(conv, busy=False)

    def _probe_env(self, session: Any) -> str:
        try:
            shell = session.agent_shell()
            result = shell.run("uname -srm 2>/dev/null; whoami; pwd", timeout=10)
            text = (result.get("output") or "").strip().replace("\n", " / ")
            return text or "未知"
        except Exception as exc:  # noqa: BLE001
            self.emit(
                {
                    "type": "error",
                    "session": session.id,
                    "message": f"无法在终端里执行命令：{exc}",
                }
            )
            return "未知"

    def _api_messages(self, conv: Conversation, session: Any) -> list[dict[str, Any]]:
        profile = getattr(session, "profile", {}) or {}
        target = f"{profile.get('username')}@{profile.get('host')}:{profile.get('port')}"
        system = SYSTEM_PROMPT.format(target=target, env=conv.env or "未知", cwd="远端 shell 当前目录")
        messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        history = conv.messages
        if len(history) > 40:
            cut = len(history) - 40
            while cut < len(history) and history[cut].get("role") == "tool":
                cut += 1
            history = history[cut:]
        for message in history:
            if message.get("role") == "tool":
                content = str(message.get("content") or "")
                if len(content) > 24000:
                    content = content[:24000] + "\n…（输出已截断）"
                messages.append({**message, "content": content})
            else:
                messages.append(message)
        return messages

    def _plan_calls(self, conv: Conversation, tool_calls: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        approval = self.store.settings.get("agentApproval", "confirm")
        auto: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []
        for call in tool_calls:
            function = call.get("function") or {}
            tool_name = str(function.get("name") or "")
            if tool_name != "run_command":
                message = f"错误：不支持的工 {tool_name or '(unknown)'}，只能调用 run_command。"
                conv.messages.append(conv.tool_message(call["id"], message))
                self.emit(
                    {
                        "type": "tool_update",
                        "session": conv.session_id,
                        "callId": call["id"],
                        "status": "error",
                        "output": message,
                        "exitCode": None,
                    }
                )
                continue
            command, explanation = parse_tool_arguments(function.get("arguments", "{}"))
            command = command.strip()
            if command.startswith("```"):
                command = command.strip("`").strip()
                if command.split("\n", 1)[0].lower() in ("bash", "shell", "sh"):
                    command = command.split("\n", 1)[1] if "\n" in command else ""
            command = command.strip()
            safe, safe_reason = command_safety(command)
            meta = {
                "command": command,
                "explanation": explanation,
                "dangerous": is_dangerous_command(command),
                "safe": safe,
                "safeReason": safe_reason,
                "approval": approval,
                "status": "pending",
                "output": "",
                "exitCode": None,
            }
            conv.tool_meta[call["id"]] = meta
            can_auto = (
                approval == "auto" or (approval == "safe" and safe)
            ) and not meta["dangerous"]
            if can_auto:
                auto.append(call)
            else:
                pending.append(call)
                conv.pending.append(call["id"])
                conv.pending_calls[call["id"]] = {"id": call["id"], "command": command}
                self.emit(
                    {
                        "type": "tool",
                        "session": conv.session_id,
                        "call": {
                            "callId": call["id"],
                            "command": command,
                            "explanation": explanation,
                            "dangerous": meta["dangerous"],
                            "safe": safe,
                            "safeReason": safe_reason,
                            "approval": approval,
                        },
                    }
                )
        return auto, pending

    def _resolve_pending(self, conv: Conversation, call_id: str, action: str, command: str) -> None:
        if call_id in conv.pending:
            conv.pending.remove(call_id)
        call = conv.pending_calls.pop(call_id, None)
        meta = conv.tool_meta.get(call_id)
        if meta is None or call is None:
            return
        if action == "skip":
            meta["status"] = "skipped"
            meta["output"] = "用户跳过了这条命令。"
            self.emit(
                {
                    "type": "tool_update",
                    "session": conv.session_id,
                    "callId": call_id,
                    "status": "skipped",
                    "output": "",
                    "exitCode": None,
                }
            )
            conv.messages.append(conv.tool_message(call_id, "用户跳过了这条命令，未执行。"))
            self._persist(conv)
        else:
            self._execute_call(conv, call)

    def _execute_call(self, conv: Conversation, call: dict[str, Any]) -> None:
        call_id = call["id"]
        meta = conv.tool_meta.get(call_id, {})
        command = (call.get("command") or meta.get("command") or "").strip()
        if not command:
            meta["status"] = "error"
            meta["output"] = "命令为空"
            conv.messages.append(conv.tool_message(call_id, "错误：命令为空，未执行。"))
            self._persist(conv)
            self.emit(
                {
                    "type": "tool_update",
                    "session": conv.session_id,
                    "callId": call_id,
                    "status": "error",
                    "output": "命令为空",
                    "exitCode": None,
                }
            )
            return

        session = self.get_session(conv.session_id)
        if session is None:
            meta["status"] = "error"
            meta["output"] = "会话已关闭"
            conv.messages.append(conv.tool_message(call_id, "错误：SSH 会话已关闭，命令未执行。"))
            self._persist(conv)
            self.emit(
                {
                    "type": "tool_update",
                    "session": conv.session_id,
                    "callId": call_id,
                    "status": "error",
                    "output": "会话已关闭",
                    "exitCode": None,
                }
            )
            return

        meta["status"] = "running"
        # Recompute: the user may have edited the command before approving it.
        safe, safe_reason = command_safety(command)
        meta["safe"] = safe
        meta["safeReason"] = safe_reason
        self.emit(
            {
                "type": "tool",
                "session": conv.session_id,
                "call": {
                    "callId": call_id,
                    "command": command,
                    "explanation": meta.get("explanation", ""),
                    "dangerous": meta.get("dangerous", False),
                    "safe": safe,
                    "safeReason": safe_reason,
                    "approval": meta.get("approval", ""),
                    "auto": True,
                },
            }
        )
        self.emit(
            {
                "type": "tool_update",
                "session": conv.session_id,
                "callId": call_id,
                "status": "running",
                "output": "",
                "exitCode": None,
            }
        )
        timeout = float(self.store.settings.get("commandTimeout", 90))
        try:
            shell = session.agent_shell()
            result = shell.run(command, timeout=timeout)
            output = result.get("output") or ""
            exit_code = result.get("exitCode")
            status = "timeout" if result.get("timedOut") else ("done" if exit_code == 0 else "error")
            meta.update(status=status, output=output, exitCode=exit_code)
            self.emit(
                {
                    "type": "tool_update",
                    "session": conv.session_id,
                    "callId": call_id,
                    "status": status,
                    "output": output,
                    "exitCode": exit_code,
                }
            )
            summary = f"$ {command}\n退出码：{exit_code}\n输出：\n{output or '（无输出）'}"
            if len(summary) > 24000:
                summary = summary[:24000] + "\n…（输出已截断）"
            conv.messages.append(conv.tool_message(call_id, summary))
            self._persist(conv)
        except Exception as exc:  # noqa: BLE001
            message = f"执行失败：{exc}"
            meta.update(status="error", output=message, exitCode=None)
            self.emit(
                {
                    "type": "tool_update",
                    "session": conv.session_id,
                    "callId": call_id,
                    "status": "error",
                    "output": message,
                    "exitCode": None,
                }
            )
            conv.messages.append(conv.tool_message(call_id, f"错误：{message}"))
            self._persist(conv)
