"""The bridge exposed to the frontend through ``window.pywebview.api``.

Every public method returns JSON-serialisable data.  Mutating calls follow the
``{"ok": bool, "error": str}`` convention so the UI can surface failures
without throwing across the bridge.

Implementation note: pywebview reflects over the js_api instance, therefore all
state attributes are underscore-prefixed.  Only methods should stay public.
"""

from __future__ import annotations

import copy
import functools
import os
import platform
import sys
import threading
import time
import webbrowser
from collections import deque
from importlib.metadata import version as package_version
from typing import Any, Callable

import webview

from . import __version__
from .agent import AgentService, LLMError, fetch_models, test_model
from .history import ConversationArchive, record_entries, sanitize_record
from .snippets import merged_groups as snippet_groups
from .ssh import Session
from .store import Store
from .transfer import default_download_dir
from .updater import Updater, validate_release_url


def _guard(method: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(method)
    def wrapper(self: "Api", *args: Any, **kwargs: Any) -> Any:
        try:
            return method(self, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - bridge must always answer
            return {"ok": False, "error": self._explain(exc)}

    return wrapper


class Api:
    def __init__(self, store: Store | None = None, auto_sync: bool = True,
                 auto_update: bool | None = None) -> None:
        self._store = store or Store()
        self._sessions: dict[str, Session] = {}
        self._events: deque[dict[str, Any]] = deque(maxlen=8000)
        self._window: webview.Window | None = None
        self._chrome: Any = None  # yhshell.window.WindowChrome, set by app.build_window
        self._lock = threading.RLock()
        self._sync_stop = threading.Event()
        self._sync_lock = threading.Lock()
        self._sync_attempts: dict[str, float] = {}
        self._updater = Updater(self._store.directory)
        self._update_stop = threading.Event()
        self._update_lock = threading.Lock()
        self._update_running = False
        self._update_notified_version = ""
        self._archive = ConversationArchive(self._store.directory)
        self._agent = AgentService(self._store, self._push_event, self._get_session, self._archive)
        if auto_sync:
            threading.Thread(target=self._sync_loop, name="model-sync", daemon=True).start()
        # Existing test callers use auto_sync=False to disable background networking.
        if (auto_update if auto_update is not None else auto_sync):
            threading.Thread(target=self._update_loop, name="update-scheduler", daemon=True).start()

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _explain(exc: BaseException) -> str:
        message = str(exc).strip()
        if isinstance(exc, (ValueError, LLMError, RuntimeError)):
            return message or exc.__class__.__name__
        if isinstance(exc, OSError):
            return message or "文件系统错误"
        return f"{exc.__class__.__name__}: {message}" if message else exc.__class__.__name__

    def _push_event(self, event: dict[str, Any]) -> None:
        # Coalesce streaming deltas so a slow UI poll cannot overflow the queue.
        if event.get("type") == "delta":
            last = self._events[-1] if self._events else None
            if last and last.get("type") == "delta" and last.get("session") == event.get("session"):
                last["text"] = str(last.get("text", "")) + str(event.get("text", ""))
                return
        self._events.append(event)

    def _get_session(self, session_id: str) -> Session | None:
        with self._lock:
            return self._sessions.get(session_id)

    def _transfer_settings(self) -> dict[str, Any]:
        settings = self._store.settings
        directory = str(settings.get("zmodemDownloadDir") or "").strip() or default_download_dir()
        return {
            "confirm": bool(settings.get("zmodemConfirm", False)),
            "downloadDir": directory,
            "overwrite": bool(settings.get("zmodemOverwrite", False)),
        }

    # ----------------------------------------------------------- bootstrap
    @_guard
    def bootstrap(self) -> dict[str, Any]:
        state = self._store.public_state()
        state.update(
            ok=True,
            platform=platform.system(),
            pythonVersion=sys.version.split()[0],
            webviewVersion=package_version("pywebview"),
            appVersion=__version__,
            updateState=self._update_state(),
            defaultDownloadDir=default_download_dir(),
            snippets=snippet_groups(self._store.snippet_config()),
        )
        return state

    @_guard
    def poll(self) -> dict[str, Any]:
        events: list[dict[str, Any]] = []
        while self._events:
            try:
                events.append(self._events.popleft())
            except IndexError:
                break
        outputs: dict[str, str] = {}
        sessions: list[dict[str, Any]] = []
        transfers: dict[str, dict[str, Any]] = {}
        with self._lock:
            current = list(self._sessions.values())
        for session in current:
            text = session.drain()
            if text:
                outputs[session.id] = text
            sessions.append(session.info())
            snapshot = session.transfer_snapshot()
            if snapshot is not None:
                transfers[session.id] = snapshot
        return {
            "ok": True,
            "events": events,
            "output": outputs,
            "sessions": sessions,
            "transfers": transfers,
            "time": time.time(),
        }

    # --------------------------------------------------------------- hosts
    @_guard
    def save_host(self, payload: dict[str, Any]) -> dict[str, Any]:
        host_id = self._store.upsert_host(payload or {})
        return {"ok": True, "hostId": host_id, "hosts": self._store.public_state()["hosts"]}

    @_guard
    def delete_host(self, host_id: str) -> dict[str, Any]:
        self._store.delete_host(str(host_id))
        return {"ok": True, "hosts": self._store.public_state()["hosts"]}

    # ------------------------------------------------------------- snippets
    def _snippet_state(self) -> dict[str, Any]:
        """The catalogue the frontend should show after a snippet change."""
        return {"ok": True, "snippets": snippet_groups(self._store.snippet_config())}

    @_guard
    def snippet_save_group(self, payload: dict[str, Any]) -> dict[str, Any]:
        group_id = self._store.save_snippet_group(payload or {})
        return {"ok": True, "groupId": group_id, **self._snippet_state()}

    @_guard
    def snippet_delete_group(self, payload: dict[str, Any]) -> dict[str, Any]:
        payload = payload or {}
        group_id = str(payload.get("groupId") or payload.get("id") or "")
        self._store.delete_snippet_group(group_id)
        return self._snippet_state()

    @_guard
    def snippet_save(self, payload: dict[str, Any]) -> dict[str, Any]:
        snippet_id = self._store.save_snippet(payload or {})
        return {"ok": True, "id": snippet_id, **self._snippet_state()}

    @_guard
    def snippet_delete(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._store.delete_snippet(payload or {})
        return self._snippet_state()

    @_guard
    def snippet_reset(self) -> dict[str, Any]:
        """Restore built-in commands while preserving custom groups and commands."""
        self._store.reset_snippets()
        return self._snippet_state()

    @_guard
    def connect(self, payload: dict[str, Any]) -> dict[str, Any]:
        payload = payload or {}
        cols = int(payload.get("cols") or 120)
        rows = int(payload.get("rows") or 32)
        host_id = str(payload.get("hostId") or "")

        if host_id:
            saved = self._store.get_host(host_id)
            if saved is None:
                return {"ok": False, "error": "主机不存在，可能已被删除"}
            password, passphrase = self._store.host_secret(host_id)
            profile = {
                "id": saved["id"],
                "name": saved["name"],
                "host": saved["host"],
                "port": saved["port"],
                "username": saved["username"],
                "auth": saved["auth"],
                "keyPath": saved["keyPath"],
                "password": password,
                "passphrase": passphrase,
            }
            self._store.touch_host(host_id)
        else:
            raw = payload.get("profile") or {}
            host = str(raw.get("host") or "").strip()
            username = str(raw.get("username") or "").strip()
            if not host or not username:
                return {"ok": False, "error": "请填写主机地址与用户名"}
            profile = {
                "id": "",
                "name": str(raw.get("name") or f"{username}@{host}"),
                "host": host,
                "port": int(raw.get("port") or 22),
                "username": username,
                "auth": raw.get("auth") if raw.get("auth") in ("password", "key", "agent") else "password",
                "keyPath": str(raw.get("keyPath") or ""),
                "password": str(raw.get("password") or ""),
                "passphrase": str(raw.get("passphrase") or ""),
            }
            if payload.get("save"):
                profile["id"] = self._store.upsert_host(
                    {
                        "name": profile["name"],
                        "host": profile["host"],
                        "port": profile["port"],
                        "username": profile["username"],
                        "auth": profile["auth"],
                        "password": profile["password"],
                        "keyPath": profile["keyPath"],
                        "passphrase": profile["passphrase"],
                    }
                )

        session = Session(profile, cols=cols, rows=rows, settings_provider=self._transfer_settings)
        with self._lock:
            self._sessions[session.id] = session
        session.start()
        return {"ok": True, "session": session.info(), "hosts": self._store.public_state()["hosts"]}

    @_guard
    def duplicate_session(self, session_id: str, cols: int = 120, rows: int = 32) -> dict[str, Any]:
        source = self._get_session(str(session_id))
        if source is None:
            return {"ok": False, "error": "会话不存在"}
        # Reuse the live credentials, including secrets never saved to the store.
        # Only session.info(), never the private profile, crosses the UI bridge.
        profile = copy.deepcopy(source.profile)
        session = Session(profile, cols=int(cols or 120), rows=int(rows or 32),
                          settings_provider=self._transfer_settings)
        with self._lock:
            self._sessions[session.id] = session
        session.start()
        return {"ok": True, "session": session.info(), "hosts": self._store.public_state()["hosts"]}

    @_guard
    def disconnect(self, session_id: str) -> dict[str, Any]:
        session = self._get_session(str(session_id))
        if session is None:
            return {"ok": False, "error": "会话不存在"}
        session.close()
        return {"ok": True}

    @_guard
    def close_session(self, session_id: str) -> dict[str, Any]:
        session_id = str(session_id)
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is not None:
            session.close()
        self._agent.forget(session_id)
        return {"ok": True}

    @_guard
    def shutdown_sessions(self) -> dict[str, Any]:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.close()
        return {"ok": True}

    @_guard
    def ssh_write(self, session_id: str, data: str) -> dict[str, Any]:
        session = self._get_session(str(session_id))
        if session is None:
            return {"ok": False, "error": "会话不存在"}
        return {"ok": session.write(data or "")}

    @_guard
    def ssh_resize(self, session_id: str, cols: int, rows: int) -> dict[str, Any]:
        session = self._get_session(str(session_id))
        if session is None:
            return {"ok": False, "error": "会话不存在"}
        session.resize(int(cols), int(rows))
        return {"ok": True}

    # ------------------------------------------------------ zmodem (rz/sz)
    @_guard
    def zmodem_accept(self, session_id: str, options: dict[str, Any] | None = None) -> dict[str, Any]:
        session = self._get_session(str(session_id))
        if session is None:
            return {"ok": False, "error": "会话不存在"}
        if not session.transfer_accept(options if isinstance(options, dict) else {}):
            return {"ok": False, "error": "没有正在等待确认的文件传输"}
        return {"ok": True}

    @_guard
    def zmodem_cancel(self, session_id: str) -> dict[str, Any]:
        session = self._get_session(str(session_id))
        if session is None:
            return {"ok": False, "error": "会话不存在"}
        session.transfer_cancel()
        return {"ok": True}

    @_guard
    def zmodem_dismiss(self, session_id: str) -> dict[str, Any]:
        session = self._get_session(str(session_id))
        if session is not None:
            session.transfer_dismiss()
        return {"ok": True}

    @_guard
    def zmodem_pick_files(self) -> dict[str, Any]:
        if self._window is None:
            return {"ok": False, "error": "窗口尚未就绪"}
        result = self._window.create_file_dialog(
            webview.OPEN_DIALOG,
            allow_multiple=True,
            file_types=("所有文件 (*.*)",),
        )
        paths = [str(path) for path in (result or []) if path]
        return {"ok": True, "files": paths}

    @_guard
    def zmodem_pick_directory(self, current: str = "") -> dict[str, Any]:
        if self._window is None:
            return {"ok": False, "error": "窗口尚未就绪"}
        default = str(current or "") or default_download_dir()
        try:
            os.makedirs(default, exist_ok=True)
        except OSError:
            pass
        result = self._window.create_file_dialog(webview.FOLDER_DIALOG, directory=default)
        if not result:
            return {"ok": True, "path": ""}
        path = result[0] if isinstance(result, (list, tuple)) else result
        return {"ok": True, "path": str(path)}

    # --------------------------------------------------------------- agent
    @_guard
    def agent_send(self, session_id: str, text: str) -> dict[str, Any]:
        return self._agent.send(str(session_id), text)

    @_guard
    def agent_cancel(self, session_id: str) -> dict[str, Any]:
        return self._agent.cancel(str(session_id))

    @_guard
    def agent_resolve(self, session_id: str, call_id: str, action: str, command: str = "") -> dict[str, Any]:
        return self._agent.resolve(str(session_id), str(call_id), str(action), str(command or ""))

    @_guard
    def agent_history(self, session_id: str) -> dict[str, Any]:
        return {
            "ok": True,
            "messages": self._agent.history(str(session_id)),
            "busy": self._agent.is_busy(str(session_id)),
        }

    @_guard
    def agent_clear(self, session_id: str) -> dict[str, Any]:
        self._agent.clear(str(session_id))
        return {"ok": True}

    # --------------------------------------------------------- conversation
    @_guard
    def conversation_list(self) -> dict[str, Any]:
        return {"ok": True, "conversations": self._archive.list()}

    @_guard
    def conversation_get(self, conversation_id: str) -> dict[str, Any]:
        record = self._archive.get(str(conversation_id))
        if record is None:
            return {"ok": False, "error": "历史对话不存在或已被删除"}
        cleaned = sanitize_record(record)
        messages = cleaned.get("messages") or []
        meta = {
            "id": cleaned.get("id"),
            "title": cleaned.get("title") or "未命名对话",
            "host": cleaned.get("host", ""),
            "port": cleaned.get("port", 0),
            "username": cleaned.get("username", ""),
            "hostName": cleaned.get("hostName", ""),
            "model": cleaned.get("model", ""),
            "createdAt": cleaned.get("createdAt") or 0,
            "updatedAt": cleaned.get("updatedAt") or 0,
            "messageCount": sum(1 for message in messages if message.get("role") in ("user", "assistant")),
        }
        return {"ok": True, "conversation": meta, "entries": record_entries(cleaned)}

    @_guard
    def conversation_delete(self, conversation_id: str) -> dict[str, Any]:
        conversation_id = str(conversation_id)
        self._agent.detach_record(conversation_id)
        deleted = self._archive.delete(conversation_id)
        return {"ok": True, "deleted": deleted}

    @_guard
    def conversation_export(self, conversation_id: str) -> dict[str, Any]:
        conversation_id = str(conversation_id)
        record = self._archive.get(conversation_id)
        if record is None:
            return {"ok": False, "error": "历史对话不存在或已被删除"}
        path = os.path.join(self._archive.directory, f"{conversation_id}.md")
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(self._archive.to_markdown(record))
        except OSError as exc:
            return {"ok": False, "error": f"导出失败：{exc}"}
        return {"ok": True, "path": path}

    @_guard
    def agent_load(self, session_id: str, conversation_id: str) -> dict[str, Any]:
        return self._agent.load(str(session_id), str(conversation_id))

    @_guard
    def open_history_dir(self) -> dict[str, Any]:
        path = self._archive.directory
        os.makedirs(path, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            import subprocess

            subprocess.Popen(["xdg-open", path])
        return {"ok": True}

    # ----------------------------------------------------------------- ai
    @_guard
    def save_provider(self, payload: dict[str, Any]) -> dict[str, Any]:
        provider_id = self._store.upsert_provider(payload or {})
        provider = self._store.get_provider(provider_id)
        # Saved without a model list (e.g. the user only filled the key): pull the
        # catalogue in the background so the provider becomes usable by itself.
        if provider is not None and not provider["models"] and self._syncable(provider):
            threading.Thread(
                target=self._sync_new_provider,
                args=(provider_id,),
                name=f"model-sync-{provider_id}",
                daemon=True,
            ).start()
        return {"ok": True, "providerId": provider_id, **self._store.public_state()}

    @_guard
    def delete_provider(self, provider_id: str) -> dict[str, Any]:
        self._store.delete_provider(str(provider_id))
        return {"ok": True, **self._store.public_state()}

    @_guard
    def set_provider_models(self, provider_id: str, models: list[str], model_options: list[str] | None = None) -> dict[str, Any]:
        self._store.set_provider_models(str(provider_id), models, model_options)
        return {"ok": True, **self._store.public_state()}

    @_guard
    def fetch_provider_models(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Ask the upstream ``/models`` endpoint which models this provider serves."""
        payload = payload or {}
        provider_id = str(payload.get("providerId") or "")
        provider = self._store.get_provider(provider_id) if provider_id else None
        if provider_id and provider is None:
            return {"ok": False, "message": "供应商不存在"}
        base_url = str(payload.get("baseUrl") or (provider or {}).get("baseUrl") or "").strip()
        api_key = str(payload.get("apiKey") or "").strip()
        if not api_key and provider is not None:
            api_key = self._store.provider_key(provider_id)
        if not base_url.startswith(("http://", "https://")):
            return {"ok": False, "message": "请先填写以 http:// 或 https:// 开头的 Base URL"}
        if not api_key and base_url.lower().startswith("https://"):
            return {"ok": False, "message": "该地址需要 API Key，请先填写"}
        return fetch_models(base_url, api_key)

    @_guard
    def set_active_model(self, model_id: str) -> dict[str, Any]:
        self._store.set_active_model(str(model_id))
        return {"ok": True, **self._store.public_state()}

    @_guard
    def test_model(self, model_id: str) -> dict[str, Any]:
        model = self._store.get_model(str(model_id))
        if model is None:
            return {"ok": False, "error": "模型不存在"}
        result = test_model(model)
        return {"ok": bool(result.get("ok")), "message": result.get("message", ""), "latency": result.get("latency")}

    # ---------------------------------------------------------- model sync
    @staticmethod
    def _syncable(provider: dict[str, Any]) -> bool:
        base_url = str(provider.get("baseUrl") or "")
        if not base_url.startswith(("http://", "https://")):
            return False
        # https endpoints need a key before a sync attempt is worth making.
        return bool(provider.get("apiKey")) or base_url.lower().startswith("http://")

    def _sync_one(self, provider_id: str) -> dict[str, Any]:
        provider = self._store.get_provider(provider_id)
        if provider is None:
            return {"ok": False, "message": "供应商不存在", "changed": False}
        result = fetch_models(provider["baseUrl"], self._store.provider_key(provider_id))
        if not result.get("ok"):
            return {"ok": False, "message": result.get("message", "获取模型失败"), "changed": False}
        models = result.get("models") or []
        changed = self._store.sync_provider_models(provider_id, models)
        return {"ok": True, "changed": changed, "count": len(models)}

    def _sync_due(self, force: bool) -> dict[str, Any]:
        hours = 0.0 if force else float(self._store.settings.get("modelSyncHours", 24) or 0)
        if not force and hours <= 0:
            return {"synced": 0, "changed": 0, "failed": [], "skipped": True}
        cutoff = time.time() - hours * 3600
        synced = 0
        changed = 0
        failed: list[dict[str, str]] = []
        # Snapshot first so the store lock is not held during HTTP requests.
        for provider in self._store.provider_snapshot():
            if not self._syncable(provider):
                continue
            if not force:
                last_attempt = max(
                    float(provider.get("lastSync") or 0),
                    self._sync_attempts.get(provider["id"], 0.0),
                )
                if last_attempt > cutoff:
                    continue
            with self._sync_lock:
                provider = self._store.get_provider(provider["id"]) or provider
                self._sync_attempts[provider["id"]] = time.time()
                result = self._sync_one(provider["id"])
            if result["ok"]:
                synced += 1
                changed += 1 if result["changed"] else 0
            else:
                failed.append({"provider": provider["name"], "message": result["message"]})
        return {"synced": synced, "changed": changed, "failed": failed, "skipped": False}

    def _sync_loop(self) -> None:
        # Give the window a moment to open, then sync whatever is stale.
        if self._sync_stop.wait(5):
            return
        while not self._sync_stop.is_set():
            try:
                result = self._sync_due(force=False)
                if result.get("changed"):
                    self._push_event({"type": "models_changed"})
            except Exception:  # noqa: BLE001 - a background refresh must never crash the app
                pass
            if self._sync_stop.wait(300):
                return

    def _sync_new_provider(self, provider_id: str) -> None:
        provider = self._store.get_provider(provider_id)
        if provider is None:
            return
        with self._sync_lock:
            self._sync_attempts[provider_id] = time.time()
            result = self._sync_one(provider_id)
        if result.get("ok") and result.get("changed"):
            self._push_event({"type": "models_changed"})

    @_guard
    def sync_all_models(self) -> dict[str, Any]:
        """Refresh every configured provider right now and return the new state."""
        result = self._sync_due(force=True)
        return {"ok": True, **result, **self._store.public_state()}

    # ------------------------------------------------------------ updates
    def _update_state(self, result: dict[str, Any] | None = None) -> dict[str, Any]:
        result = dict(result if result is not None else self._updater.state())
        result["ignored"] = bool(result.get("latestVersion") and
                                 result["latestVersion"] == self._store.settings.get("updateIgnoredVersion"))
        if self._update_running and result.get("status") != "error":
            result["status"] = "checking"
        return result

    def _update_loop(self) -> None:
        if self._update_stop.wait(15):
            return
        while not self._update_stop.is_set():
            if self._store.settings.get("updateAutoCheck", True):
                self._start_update_check(manual=False)
            if self._update_stop.wait(300):
                return

    def _start_update_check(self, manual: bool) -> bool:
        with self._update_lock:
            if self._update_running or self._update_stop.is_set():
                return False
            self._update_running = True

        def run() -> None:
            try:
                result = self._updater.check(force=manual)
                # A fresh cached result can still remind a user after restart.
                if not manual and result.get("status") == "skipped" and result.get("reason") == "recent_success":
                    cached = self._updater.state()
                    if cached.get("status") == "available":
                        result = cached
            except Exception:  # a failed optional check must never affect SSH
                result = {"ok": False, "status": "error", "error": "检查更新失败，请稍后重试"}
            finally:
                with self._update_lock:
                    self._update_running = False
            if not self._update_stop.is_set():
                update = self._update_state(result)
                if not manual:
                    if not self._store.settings.get("updateAutoCheck", True):
                        return
                    if update.get("status") == "skipped":
                        return
                    if update.get("status") == "available":
                        version = str(update.get("latestVersion") or "")
                        if version == self._update_notified_version:
                            return
                        self._update_notified_version = version
                self._push_event({"type": "update_check", "manual": manual, "update": update})

        threading.Thread(target=run, name="update-check", daemon=True).start()
        return True

    @_guard
    def update_status(self) -> dict[str, Any]:
        return {"ok": True, "update": self._update_state()}

    @_guard
    def check_updates(self) -> dict[str, Any]:
        return {"ok": True, "started": self._start_update_check(manual=True)}

    @_guard
    def ignore_update(self, version: str) -> dict[str, Any]:
        latest = self._updater.state().get("latestVersion")
        if not latest or version != latest:
            return {"ok": False, "error": "只能忽略当前已检测到的版本，请重新检查更新"}
        settings = self._store.update_settings({"updateIgnoredVersion": latest})
        return {"ok": True, "settings": settings}

    @_guard
    def open_update_release(self) -> dict[str, Any]:
        release = self._updater.state().get("release") or {}
        url = release.get("url", "")
        if not validate_release_url(url):
            return {"ok": False, "error": "没有可用的官方版本页面，请先检查更新"}
        if not webbrowser.open(url, new=2):
            return {"ok": False, "error": "无法打开系统浏览器，请检查默认浏览器设置"}
        return {"ok": True}

    # ------------------------------------------------------------- config
    @_guard
    def save_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        settings = self._store.update_settings(patch or {})
        return {"ok": True, "settings": settings}

    @_guard
    def reveal_secret(self, kind: str, item_id: str) -> dict[str, Any]:
        return {"ok": True, "value": self._store.reveal_secret(str(kind), str(item_id))}

    @_guard
    def pick_key_file(self) -> dict[str, Any]:
        if self._window is None:
            return {"ok": False, "error": "窗口尚未就绪"}
        result = self._window.create_file_dialog(
            webview.OPEN_DIALOG,
            allow_multiple=False,
            file_types=("私钥文件 (*.pem;*.key;id_*;*)", "所有文件 (*.*)"),
        )
        if not result:
            return {"ok": True, "path": ""}
        return {"ok": True, "path": result[0]}

    @_guard
    def open_config_dir(self) -> dict[str, Any]:
        path = self._store.directory
        os.makedirs(path, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            import subprocess

            subprocess.Popen(["xdg-open", path])
        return {"ok": True}

    @_guard
    def open_folder(self, path: str) -> dict[str, Any]:
        target = os.path.abspath(os.path.expanduser(str(path or "")))
        if not target or not os.path.isdir(target):
            return {"ok": False, "error": "目录不存在"}
        if sys.platform == "win32":
            os.startfile(target)  # type: ignore[attr-defined]
        else:
            import subprocess

            subprocess.Popen(["xdg-open", target])
        return {"ok": True, "path": target}

    @_guard
    def set_title(self, title: str) -> dict[str, Any]:
        if self._window is not None:
            try:
                self._window.set_title(str(title))
            except Exception:  # noqa: BLE001
                pass
        return {"ok": True}

    # --------------------------------------------------------------- window
    # The title bar is drawn by the frontend (see web/index.html); these
    # methods provide the native behaviour behind its buttons.
    @_guard
    def window_state(self) -> dict[str, Any]:
        return {"ok": True, "maximized": bool(self._chrome and self._chrome.is_maximized())}

    @_guard
    def window_minimize(self) -> dict[str, Any]:
        if self._window is not None:
            self._window.minimize()
        return {"ok": True}

    @_guard
    def window_toggle_maximize(self) -> dict[str, Any]:
        if self._chrome is None or not getattr(self._chrome, "attached", False):
            return {"ok": False, "error": "窗口尚未就绪", "maximized": False}
        return {"ok": True, "maximized": self._chrome.toggle_maximize()}

    @_guard
    def window_close(self) -> dict[str, Any]:
        if self._window is not None:
            # Answer the bridge call first: destroying the window immediately
            # would make pywebview fail to deliver this method's result.
            timer = threading.Timer(0.12, self._window.destroy)
            timer.daemon = True
            timer.start()
        return {"ok": True}

    @_guard
    def window_drag(self) -> dict[str, Any]:
        if self._chrome is not None:
            self._chrome.start_drag()
        return {"ok": True}

    @_guard
    def window_resize(self, edge: str) -> dict[str, Any]:
        if self._chrome is not None:
            self._chrome.start_resize(str(edge))
        return {"ok": True}

    @_guard
    def window_set_theme(self, theme: str) -> dict[str, Any]:
        if self._chrome is not None:
            self._chrome.set_theme(str(theme))
        return {"ok": True}

    def _stop_background(self) -> None:
        self._sync_stop.set()
        self._update_stop.set()

    @_guard
    def shutdown(self) -> dict[str, Any]:
        self._stop_background()
        if self._window is not None:
            self._window.destroy()
        return {"ok": True}
