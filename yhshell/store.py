"""Persistent configuration for yhShell.

Everything lives in a single JSON document under ``%APPDATA%\\yhShell`` (or
``$YHSELL_HOME``).  Secrets are encrypted with :mod:`yhshell.security` before
they touch the disk and are never sent to the frontend in clear text; the UI
only learns whether a secret exists and, for providers, a short hint.

Models are organised by *provider*: one endpoint + API key, plus the list of
upstream model identifiers the user picked after querying ``/models``.  A
selectable model is addressed by ``"<providerId>::<model>"``.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import time
import uuid
from typing import Any
from urllib.parse import urlparse

from . import security

APP_NAME = "yhShell"
CONFIG_FILENAME = "config.json"
CONFIG_VERSION = 3
MODEL_REF_SEP = "::"
# The starter provider used to ship with this model id; configs saved by older
# builds get the placeholder cleared because the upstream no longer serves it.
SEEDED_BASE_URL = "https://api.deepseek.com"
SEEDED_MODEL = "deepseek-chat"

DEFAULT_SETTINGS: dict[str, Any] = {
    "fontSize": 13,
    "fontFamily": "Cascadia Mono, JetBrains Mono, Consolas, monospace",
    "theme": "dark",
    "agentApproval": "confirm",  # confirm | safe | auto
    "commandTimeout": 90,
    "maxAgentSteps": 8,
    "copyOnSelect": False,
    "terminalHighlight": True,
    "terminalHighlightRules": {"error": True, "warning": True, "success": True, "address": True},
    # Hours between automatic provider /models refreshes; 0 disables it.
    "modelSyncHours": 24,
    "updateAutoCheck": True,
    "updateIgnoredVersion": "",
    # ZMODEM (rz/sz) file transfer
    "zmodemConfirm": False,
    "zmodemDownloadDir": "",
    "zmodemOverwrite": False,
}


def app_dir() -> str:
    override = os.environ.get("YHSELL_HOME")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    base = os.environ.get("APPDATA") or os.path.expanduser("~/.config")
    return os.path.join(base, APP_NAME)


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _clean(value: Any, limit: int = 400) -> str:
    return str(value or "").strip()[:limit]


def model_ref(provider_id: str, model: str) -> str:
    """Public identifier for one selected model of a provider."""
    return f"{provider_id}{MODEL_REF_SEP}{model}" if provider_id and model else ""


def split_model_ref(ref: str) -> tuple[str, str]:
    provider_id, sep, model = str(ref or "").partition(MODEL_REF_SEP)
    return (provider_id, model) if sep else ("", "")


def _clean_models(value: Any, limit: int = 400) -> list[str]:
    """Normalise a list of model identifiers, preserving order and uniqueness."""
    result: list[str] = []
    if not isinstance(value, list):
        return result
    for item in value[:limit]:
        name = _clean(item, 160)
        if name and name not in result:
            result.append(name)
    return result


def _as_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def _as_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def default_config() -> dict[str, Any]:
    provider_id = _new_id()
    # No models are preselected: upstream catalogues change, and shipping a
    # hard-coded model id would leave users with a dead entry in the list.
    return {
        "version": CONFIG_VERSION,
        "settings": copy.deepcopy(DEFAULT_SETTINGS),
        "hosts": [],
        "providers": [
            {
                "id": provider_id,
                "name": "DeepSeek",
                "baseUrl": SEEDED_BASE_URL,
                "apiKey": "",
                "temperature": 0.3,
                "maxTokens": 2048,
                "models": [],
                "modelOptions": [],
                "lastSync": 0,
            }
        ],
        "activeModel": {"providerId": "", "model": ""},
        # User edits on top of the built-in snippet library: custom groups with
        # their own commands, patches for built-in commands and the list of
        # built-in commands the user removed.
        "snippets": {"groups": [], "overrides": {}, "hidden": []},
    }


class Store:
    def __init__(self, directory: str | None = None) -> None:
        self.directory = directory or app_dir()
        self.path = os.path.join(self.directory, CONFIG_FILENAME)
        self._lock = threading.RLock()
        self.data: dict[str, Any] = default_config()
        self.load()

    # ------------------------------------------------------------------ io
    def load(self) -> None:
        with self._lock:
            if not os.path.exists(self.path):
                self.data = default_config()
                self.save()
                return
            try:
                with open(self.path, "r", encoding="utf-8") as handle:
                    loaded = json.load(handle)
            except (OSError, ValueError):
                backup = self.path + f".broken-{int(time.time())}"
                try:
                    os.replace(self.path, backup)
                except OSError:
                    pass
                self.data = default_config()
                self.save()
                return
            self.data = self._normalize(loaded)
            # Persist the upgraded shape once so a v1 config is migrated on disk.
            if loaded.get("version") != CONFIG_VERSION or not isinstance(loaded.get("providers"), list):
                self.save()

    def save(self) -> None:
        with self._lock:
            os.makedirs(self.directory, exist_ok=True)
            payload = json.dumps(self.data, ensure_ascii=False, indent=2)
            fd, tmp_path = tempfile.mkstemp(prefix=".config-", suffix=".json", dir=self.directory)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                os.replace(tmp_path, self.path)
            finally:
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass

    def _normalize(self, raw: Any) -> dict[str, Any]:
        base = default_config()
        if not isinstance(raw, dict):
            return base

        settings = copy.deepcopy(DEFAULT_SETTINGS)
        incoming_settings = raw.get("settings") if isinstance(raw.get("settings"), dict) else {}
        settings["fontSize"] = _as_int(incoming_settings.get("fontSize"), 13, 9, 28)
        font = _clean(incoming_settings.get("fontFamily"), 200)
        if font:
            settings["fontFamily"] = font
        theme = incoming_settings.get("theme")
        settings["theme"] = theme if theme in ("dark", "light") else "dark"
        approval = incoming_settings.get("agentApproval")
        settings["agentApproval"] = approval if approval in ("confirm", "safe", "auto") else "confirm"
        settings["commandTimeout"] = _as_int(incoming_settings.get("commandTimeout"), 90, 10, 900)
        settings["maxAgentSteps"] = _as_int(incoming_settings.get("maxAgentSteps"), 8, 1, 30)
        settings["copyOnSelect"] = bool(incoming_settings.get("copyOnSelect", False))
        highlight = incoming_settings.get("terminalHighlight")
        if isinstance(highlight, bool):
            settings["terminalHighlight"] = highlight
        highlight_rules = incoming_settings.get("terminalHighlightRules")
        if isinstance(highlight_rules, dict):
            for category in settings["terminalHighlightRules"]:
                enabled = highlight_rules.get(category)
                if isinstance(enabled, bool):
                    settings["terminalHighlightRules"][category] = enabled
        settings["modelSyncHours"] = _as_int(incoming_settings.get("modelSyncHours"), 24, 0, 720)
        auto_check = incoming_settings.get("updateAutoCheck", True)
        settings["updateAutoCheck"] = auto_check if isinstance(auto_check, bool) else True
        settings["updateIgnoredVersion"] = _clean(incoming_settings.get("updateIgnoredVersion"), 64)
        settings["zmodemConfirm"] = bool(incoming_settings.get("zmodemConfirm", False))
        settings["zmodemOverwrite"] = bool(incoming_settings.get("zmodemOverwrite", False))
        download_dir = _clean(incoming_settings.get("zmodemDownloadDir"), 500)
        if download_dir:
            settings["zmodemDownloadDir"] = download_dir

        hosts: list[dict[str, Any]] = []
        for item in raw.get("hosts") or []:
            if not isinstance(item, dict):
                continue
            hostname = _clean(item.get("host"), 255)
            if not hostname:
                continue
            auth = item.get("auth")
            hosts.append(
                {
                    "id": _clean(item.get("id"), 40) or _new_id(),
                    "name": _clean(item.get("name"), 80) or hostname,
                    "host": hostname,
                    "port": _as_int(item.get("port"), 22, 1, 65535),
                    "username": _clean(item.get("username"), 80),
                    "auth": auth if auth in ("password", "key", "agent") else "password",
                    "password": str(item.get("password") or ""),
                    "keyPath": _clean(item.get("keyPath"), 500),
                    "passphrase": str(item.get("passphrase") or ""),
                    "lastUsed": int(item.get("lastUsed") or 0),
                }
            )

        providers, legacy_refs = self._normalize_providers(raw)
        if not providers:
            providers = base["providers"]
        providers = self._drop_seeded_placeholder(providers)

        active = raw.get("activeModel") if isinstance(raw.get("activeModel"), dict) else {}
        active_provider = _clean(active.get("providerId"), 40)
        active_model = _clean(active.get("model"), 160)
        if not (active_provider and active_model):
            legacy_id = _clean(raw.get("activeModelId"), 200)
            if legacy_id in legacy_refs:
                active_provider, active_model = legacy_refs[legacy_id]
            else:
                active_provider, active_model = split_model_ref(legacy_id)

        base.update(settings=settings, hosts=hosts, providers=providers)
        base["snippets"] = self._normalize_snippets(raw.get("snippets"))
        base["activeModel"] = self._pick_active(providers, active_provider, active_model)
        return base

    @staticmethod
    def _normalize_snippets(raw: Any) -> dict[str, Any]:
        """Normalise the user's snippet edits into a predictable shape.

        ``groups`` holds the user's own groups and commands, ``overrides`` patches
        individual built-in commands and ``hidden`` lists the built-ins the user
        removed.  Anything malformed is dropped instead of failing the load.
        """
        config: dict[str, Any] = {"groups": [], "overrides": {}, "hidden": []}
        if not isinstance(raw, dict):
            return config

        for group in raw.get("groups") or []:
            if not isinstance(group, dict):
                continue
            name = _clean(group.get("name"), 80)
            if not name:
                continue
            items: list[dict[str, Any]] = []
            for item in group.get("items") or []:
                if not isinstance(item, dict):
                    continue
                command = _clean(item.get("command"), 500)
                if not command:
                    continue
                items.append(
                    {
                        "id": _clean(item.get("id"), 40) or _new_id(),
                        "name": _clean(item.get("name"), 80) or command[:40],
                        "command": command,
                        "note": _clean(item.get("note"), 300),
                    }
                )
            config["groups"].append(
                {
                    "id": _clean(group.get("id"), 40) or _new_id(),
                    "name": name,
                    "description": _clean(group.get("description"), 200),
                    "items": items,
                }
            )

        overrides = raw.get("overrides")
        if isinstance(overrides, dict):
            for key, patch in overrides.items():
                snippet_id = _clean(key, 60)
                if not snippet_id or not isinstance(patch, dict):
                    continue
                clean_patch = {
                    field: _clean(patch.get(field), 500)
                    for field in ("name", "command", "note")
                    if field in patch
                }
                if clean_patch:
                    config["overrides"][snippet_id] = clean_patch

        hidden = raw.get("hidden")
        if isinstance(hidden, list):
            seen: set[str] = set()
            for item in hidden:
                snippet_id = _clean(item, 60)
                if snippet_id and snippet_id not in seen:
                    seen.add(snippet_id)
                    config["hidden"].append(snippet_id)
        return config

    @staticmethod
    def _drop_seeded_placeholder(providers: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Clear the model id older builds shipped with the starter provider.

        Only an untouched placeholder is removed: no key, never synced and no
        other model option. Anything the user actually chose or fetched stays.
        """
        for provider in providers:
            if (
                provider["baseUrl"] == SEEDED_BASE_URL
                and not provider["apiKey"]
                and not provider["lastSync"]
                and provider["models"] == [SEEDED_MODEL]
                and provider["modelOptions"] == [SEEDED_MODEL]
            ):
                provider["models"] = []
                provider["modelOptions"] = []
        return providers

    @staticmethod
    def _pick_active(providers: list[dict[str, Any]], provider_id: str, model_name: str) -> dict[str, str]:
        """Return a usable active selection, falling back to the first model available."""
        for provider in providers:
            if provider["id"] == provider_id and model_name in provider["models"]:
                return {"providerId": provider_id, "model": model_name}
        for provider in providers:
            if provider["models"]:
                return {"providerId": provider["id"], "model": provider["models"][0]}
        return {"providerId": "", "model": ""}

    @staticmethod
    def _normalize_providers(raw: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, tuple[str, str]]]:
        providers: list[dict[str, Any]] = []
        legacy_refs: dict[str, tuple[str, str]] = {}
        incoming = raw.get("providers")
        if isinstance(incoming, list) and incoming:
            seen: set[str] = set()
            for item in incoming:
                if not isinstance(item, dict):
                    continue
                base_url = _clean(item.get("baseUrl"), 500).rstrip("/")
                if not base_url.startswith(("http://", "https://")):
                    continue
                provider_id = _clean(item.get("id"), 40)
                if not provider_id or provider_id in seen:
                    provider_id = _new_id()
                seen.add(provider_id)
                selected = _clean_models(item.get("models"))
                options = _clean_models(item.get("modelOptions"))
                for model_name in selected:
                    if model_name not in options:
                        options.append(model_name)
                providers.append(
                    {
                        "id": provider_id,
                        "name": _clean(item.get("name"), 80) or urlparse(base_url).hostname or base_url,
                        "baseUrl": base_url,
                        "apiKey": str(item.get("apiKey") or ""),
                        "temperature": _as_float(item.get("temperature"), 0.3, 0.0, 2.0),
                        "maxTokens": _as_int(item.get("maxTokens"), 2048, 64, 32768),
                        "models": selected,
                        "modelOptions": options,
                        "lastSync": int(item.get("lastSync") or 0),
                    }
                )
        if not providers:
            providers, legacy_refs = Store._migrate_models(raw.get("models"))
        return providers, legacy_refs

    @staticmethod
    def _migrate_models(items: Any) -> tuple[list[dict[str, Any]], dict[str, tuple[str, str]]]:
        """Turn the pre-0.2 flat model list into providers grouped by endpoint + key."""
        providers: list[dict[str, Any]] = []
        grouped: dict[tuple[str, str], dict[str, Any]] = {}
        refs: dict[str, tuple[str, str]] = {}
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            base_url = _clean(item.get("baseUrl"), 500).rstrip("/")
            model_name = _clean(item.get("model"), 160)
            if not base_url.startswith(("http://", "https://")) or not model_name:
                continue
            key = (base_url, str(item.get("apiKey") or ""))
            provider = grouped.get(key)
            if provider is None:
                provider = {
                    "id": _clean(item.get("id"), 40) or _new_id(),
                    "name": _clean(item.get("name"), 80) or urlparse(base_url).hostname or base_url,
                    "baseUrl": base_url,
                    "apiKey": key[1],
                    "temperature": _as_float(item.get("temperature"), 0.3, 0.0, 2.0),
                    "maxTokens": _as_int(item.get("maxTokens"), 2048, 64, 32768),
                    "models": [],
                    "modelOptions": [],
                    "lastSync": 0,
                }
                grouped[key] = provider
                providers.append(provider)
            if model_name not in provider["models"]:
                provider["models"].append(model_name)
                provider["modelOptions"].append(model_name)
            legacy_id = _clean(item.get("id"), 40)
            if legacy_id:
                refs[legacy_id] = (provider["id"], model_name)
        return providers, refs

    # -------------------------------------------------------------- lookup
    @property
    def settings(self) -> dict[str, Any]:
        return self.data["settings"]

    def get_host(self, host_id: str) -> dict[str, Any] | None:
        for host in self.data["hosts"]:
            if host["id"] == host_id:
                return host
        return None

    def get_provider(self, provider_id: str) -> dict[str, Any] | None:
        for provider in self.data["providers"]:
            if provider["id"] == provider_id:
                return provider
        return None

    def _resolve_model(self, model_id: str | None = None) -> tuple[dict[str, Any] | None, str]:
        """Map ``"<providerId>::<model>"`` (or the active selection) to its provider."""
        if model_id:
            provider_id, model_name = split_model_ref(model_id)
            provider = self.get_provider(provider_id) if provider_id else None
            if provider is None or model_name not in provider["models"]:
                return None, ""
            return provider, model_name
        active = self.data.get("activeModel") or {}
        provider = self.get_provider(str(active.get("providerId") or ""))
        model_name = str(active.get("model") or "")
        if provider is None or model_name not in provider["models"]:
            return None, ""
        return provider, model_name

    def get_model(self, model_id: str | None = None) -> dict[str, Any] | None:
        """Return a chat-ready view of one model (``apiKey`` still encrypted)."""
        provider, model_name = self._resolve_model(model_id)
        if provider is None:
            return None
        return {
            "id": model_ref(provider["id"], model_name),
            "providerId": provider["id"],
            "providerName": provider["name"],
            "name": model_name,
            "baseUrl": provider["baseUrl"],
            "model": model_name,
            "apiKey": provider.get("apiKey", ""),
            "temperature": provider["temperature"],
            "maxTokens": provider["maxTokens"],
        }

    def provider_key(self, provider_id: str) -> str:
        provider = self.get_provider(str(provider_id))
        return security.unprotect(provider["apiKey"]) if provider else ""

    def touch_host(self, host_id: str) -> None:
        host = self.get_host(host_id)
        if host:
            host["lastUsed"] = int(time.time())
            self.save()

    # -------------------------------------------------------------- public
    def public_state(self) -> dict[str, Any]:
        with self._lock:
            active = self.data.get("activeModel") or {}
            active_provider = str(active.get("providerId") or "")
            active_model = str(active.get("model") or "")
            models: list[dict[str, Any]] = []
            providers: list[dict[str, Any]] = []
            for provider in self.data["providers"]:
                key = security.unprotect(provider.get("apiKey", ""))
                hint = ("••••" + key[-4:]) if len(key) >= 8 else ("••••" if key else "")
                selected = list(provider.get("models") or [])
                options = list(provider.get("modelOptions") or [])
                for model_name in selected:
                    models.append(
                        {
                            "id": model_ref(provider["id"], model_name),
                            "providerId": provider["id"],
                            "providerName": provider["name"],
                            "name": model_name,
                            "label": (
                                f"{provider['name']} · {model_name}" if len(selected) > 1 else provider["name"]
                            ),
                            "model": model_name,
                            "baseUrl": provider["baseUrl"],
                            "hasKey": bool(key),
                            "keyHint": hint,
                            "isActive": provider["id"] == active_provider and model_name == active_model,
                        }
                    )
                providers.append(
                    {
                        "id": provider["id"],
                        "name": provider["name"],
                        "baseUrl": provider["baseUrl"],
                        "temperature": provider["temperature"],
                        "maxTokens": provider["maxTokens"],
                        "models": selected,
                        "modelOptions": options,
                        "lastSync": int(provider.get("lastSync") or 0),
                        "hasKey": bool(key),
                        "keyHint": hint,
                        "isActive": provider["id"] == active_provider and active_model in selected,
                    }
                )
            hosts = []
            for host in self.data["hosts"]:
                hosts.append(
                    {
                        "id": host["id"],
                        "name": host["name"],
                        "host": host["host"],
                        "port": host["port"],
                        "username": host["username"],
                        "auth": host["auth"],
                        "hasPassword": bool(security.unprotect(host.get("password", ""))),
                        "keyPath": host["keyPath"],
                        "hasPassphrase": bool(security.unprotect(host.get("passphrase", ""))),
                        "lastUsed": host["lastUsed"],
                    }
                )
            hosts.sort(key=lambda item: item["lastUsed"], reverse=True)
            return {
                "settings": copy.deepcopy(self.data["settings"]),
                "models": models,
                "providers": providers,
                "hosts": hosts,
                "activeModelId": model_ref(active_provider, active_model),
                "appName": APP_NAME,
                "configDir": self.directory,
            }

    def reveal_secret(self, kind: str, item_id: str) -> str:
        if kind in ("model", "provider"):
            provider_id = split_model_ref(item_id)[0] or str(item_id)
            provider = self.get_provider(provider_id)
            return security.unprotect(provider["apiKey"]) if provider else ""
        if kind == "host-password":
            host = self.get_host(item_id)
            return security.unprotect(host["password"]) if host else ""
        if kind == "host-passphrase":
            host = self.get_host(item_id)
            return security.unprotect(host["passphrase"]) if host else ""
        return ""

    # ------------------------------------------------------------- mutate
    def update_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            incoming = dict(self.data["settings"])
            incoming.update(patch if isinstance(patch, dict) else {})
            normalized = self._normalize({**self.data, "settings": incoming})
            self.data["settings"] = normalized["settings"]
            self.save()
            return copy.deepcopy(self.data["settings"])

    def _fix_active(self) -> None:
        """Keep the active selection pointing at a model that still exists."""
        active = self.data.get("activeModel") or {}
        provider_id = str(active.get("providerId") or "")
        model_name = str(active.get("model") or "")
        for provider in self.data["providers"]:
            if provider["id"] == provider_id:
                if model_name in provider["models"]:
                    return
                # Prefer another model of the same provider before switching provider.
                if provider["models"]:
                    self.data["activeModel"] = {"providerId": provider_id, "model": provider["models"][0]}
                    return
                break
        self.data["activeModel"] = self._pick_active(self.data["providers"], "", "")

    def upsert_provider(self, payload: dict[str, Any]) -> str:
        if not isinstance(payload, dict):
            raise ValueError("参数无效")
        base_url = _clean(payload.get("baseUrl"), 500).rstrip("/")
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("Base URL 必须以 http:// 或 https:// 开头")

        with self._lock:
            provider_id = _clean(payload.get("id"), 40)
            existing = self.get_provider(provider_id) if provider_id else None
            name = _clean(payload.get("name"), 80) or (existing or {}).get("name") or ""
            if not name:
                name = urlparse(base_url).hostname or base_url
            temperature = _as_float(
                payload.get("temperature"),
                float((existing or {}).get("temperature", 0.3)),
                0.0,
                2.0,
            )
            max_tokens = _as_int(
                payload.get("maxTokens"),
                int((existing or {}).get("maxTokens", 2048)),
                64,
                32768,
            )
            new_key = payload.get("apiKey")
            if existing is None:
                provider_id = _new_id()
                self.data["providers"].append(
                    {
                        "id": provider_id,
                        "name": name,
                        "baseUrl": base_url,
                        "apiKey": security.protect(str(new_key or "").strip()),
                        "temperature": temperature,
                        "maxTokens": max_tokens,
                        "models": _clean_models(payload.get("models")),
                        "modelOptions": _clean_models(payload.get("modelOptions")),
                        "lastSync": int(payload.get("lastSync") or 0),
                    }
                )
            else:
                existing.update(name=name, baseUrl=base_url, temperature=temperature, maxTokens=max_tokens)
                if payload.get("clearKey"):
                    existing["apiKey"] = ""
                elif isinstance(new_key, str) and new_key.strip():
                    existing["apiKey"] = security.protect(new_key.strip())
                if isinstance(payload.get("models"), list):
                    existing["models"] = _clean_models(payload.get("models"))
                if isinstance(payload.get("modelOptions"), list):
                    existing["modelOptions"] = _clean_models(payload.get("modelOptions"))
                if payload.get("lastSync"):
                    existing["lastSync"] = int(payload.get("lastSync") or 0)

            provider = self.get_provider(provider_id)
            if provider is not None:
                for model_name in provider["models"]:
                    if model_name not in provider["modelOptions"]:
                        provider["modelOptions"].append(model_name)
            self._fix_active()
            self.save()
            return provider_id

    def delete_provider(self, provider_id: str) -> None:
        with self._lock:
            self.data["providers"] = [p for p in self.data["providers"] if p["id"] != provider_id]
            if not self.data["providers"]:
                self.data["providers"] = default_config()["providers"]
            self._fix_active()
            self.save()

    def set_provider_models(self, provider_id: str, models: Any, model_options: Any = None) -> None:
        with self._lock:
            provider = self.get_provider(str(provider_id))
            if provider is None:
                raise ValueError("供应商不存在")
            provider["models"] = _clean_models(models)
            if isinstance(model_options, list):
                options = _clean_models(model_options)
            else:
                options = list(provider.get("modelOptions") or [])
            for model_name in provider["models"]:
                if model_name not in options:
                    options.append(model_name)
            provider["modelOptions"] = options
            self._fix_active()
            self.save()

    def sync_provider_models(self, provider_id: str, models: Any, last_sync: float | None = None) -> bool:
        """Replace a provider's whole model list with the upstream catalogue.

        This is what the automatic refresh uses: every model the endpoint
        advertises becomes selectable, no per-model opt-in.  Returns whether the
        list actually changed, so the UI only refreshes when needed.
        """
        with self._lock:
            provider = self.get_provider(str(provider_id))
            if provider is None:
                raise ValueError("供应商不存在")
            refreshed = _clean_models(models)
            if not refreshed:
                # An empty catalogue must never wipe a usable configuration.
                return False
            changed = refreshed != provider["models"]
            provider["models"] = refreshed
            provider["modelOptions"] = list(refreshed)
            provider["lastSync"] = int(last_sync if last_sync is not None else time.time())
            self._fix_active()
            self.save()
            return changed

    def provider_snapshot(self) -> list[dict[str, Any]]:
        """Shallow copies of the providers, safe to iterate outside the lock."""
        with self._lock:
            return [dict(provider) for provider in self.data["providers"]]

    def set_active_model(self, model_id: str) -> None:
        with self._lock:
            provider, model_name = self._resolve_model(str(model_id))
            if provider is not None:
                self.data["activeModel"] = {"providerId": provider["id"], "model": model_name}
                self.save()

    def upsert_host(self, payload: dict[str, Any]) -> str:
        if not isinstance(payload, dict):
            raise ValueError("参数无效")
        hostname = _clean(payload.get("host"), 255)
        if not hostname:
            raise ValueError("请填写主机地址")
        username = _clean(payload.get("username"), 80)
        if not username:
            raise ValueError("请填写用户名")
        auth = payload.get("auth")
        if auth not in ("password", "key", "agent"):
            auth = "password"

        with self._lock:
            host_id = _clean(payload.get("id"), 40)
            existing = self.get_host(host_id) if host_id else None
            if existing is None:
                host_id = _new_id()
                self.data["hosts"].append(
                    {
                        "id": host_id,
                        "name": _clean(payload.get("name"), 80) or f"{username}@{hostname}",
                        "host": hostname,
                        "port": _as_int(payload.get("port"), 22, 1, 65535),
                        "username": username,
                        "auth": auth,
                        "password": security.protect(str(payload.get("password") or "").strip()),
                        "keyPath": _clean(payload.get("keyPath"), 500),
                        "passphrase": security.protect(str(payload.get("passphrase") or "").strip()),
                        "lastUsed": int(payload.get("lastUsed") or 0),
                    }
                )
            else:
                existing.update(
                    name=_clean(payload.get("name"), 80) or f"{username}@{hostname}",
                    host=hostname,
                    port=_as_int(payload.get("port"), 22, 1, 65535),
                    username=username,
                    auth=auth,
                    keyPath=_clean(payload.get("keyPath"), 500),
                )
                if isinstance(payload.get("password"), str) and payload["password"]:
                    existing["password"] = security.protect(payload["password"].strip())
                if isinstance(payload.get("passphrase"), str) and payload["passphrase"]:
                    existing["passphrase"] = security.protect(payload["passphrase"].strip())
            self.save()
            return host_id

    def delete_host(self, host_id: str) -> None:
        with self._lock:
            self.data["hosts"] = [h for h in self.data["hosts"] if h["id"] != host_id]
            self.save()

    def host_secret(self, host_id: str) -> tuple[str, str]:
        """Return (password, passphrase) in clear text for connecting."""
        host = self.get_host(host_id)
        if not host:
            return "", ""
        return security.unprotect(host.get("password", "")), security.unprotect(host.get("passphrase", ""))

    # ------------------------------------------------------------- snippets
    def snippet_config(self) -> dict[str, Any]:
        """Return a copy of the user's snippet edits."""
        with self._lock:
            return copy.deepcopy(self.data.get("snippets") or {"groups": [], "overrides": {}, "hidden": []})

    def save_snippet_group(self, payload: dict[str, Any]) -> str:
        """Create or rename one of the user's own snippet groups."""
        if not isinstance(payload, dict):
            raise ValueError("参数无效")
        name = _clean(payload.get("name"), 80)
        if not name:
            raise ValueError("请填写分组名称")
        with self._lock:
            snippets = self._snippet_state()
            group_id = _clean(payload.get("id"), 40)
            group = next((g for g in snippets["groups"] if g["id"] == group_id), None) if group_id else None
            if group is None:
                group = {"id": group_id or _new_id(), "name": name, "description": "", "items": []}
                snippets["groups"].append(group)
            group["name"] = name
            description = _clean(payload.get("description"), 200)
            if description:
                group["description"] = description
            self.save()
            return group["id"]

    def delete_snippet_group(self, group_id: str) -> None:
        """Remove a custom group together with its commands."""
        with self._lock:
            snippets = self._snippet_state()
            snippets["groups"] = [g for g in snippets["groups"] if g["id"] != _clean(group_id, 40)]
            self.save()

    def save_snippet(self, payload: dict[str, Any]) -> str:
        """Create or update one command.

        Built-in commands are patched through ``overrides``; everything else
        becomes a command inside one of the user's own groups.
        """
        if not isinstance(payload, dict):
            raise ValueError("参数无效")
        command = _clean(payload.get("command"), 500)
        if not command:
            raise ValueError("请填写命令")
        name = _clean(payload.get("name"), 80)
        note = _clean(payload.get("note"), 300)
        snippet_id = _clean(payload.get("id"), 60)
        group_id = _clean(payload.get("groupId"), 40)

        with self._lock:
            snippets = self._snippet_state()
            if payload.get("builtin"):
                if not snippet_id:
                    raise ValueError("内置指令缺少 ID")
                snippets["overrides"][snippet_id] = {
                    "name": name or command[:40],
                    "command": command,
                    "note": note,
                }
                if snippet_id in snippets["hidden"]:
                    snippets["hidden"].remove(snippet_id)
                self.save()
                return snippet_id

            group = next((g for g in snippets["groups"] if g["id"] == group_id), None)
            if group is None:
                group_name = _clean(payload.get("groupName"), 80) or "我的指令"
                group = {"id": _new_id(), "name": group_name, "description": "", "items": []}
                snippets["groups"].append(group)
            # IDs identify custom commands across the whole catalogue, not only
            # inside the destination group. Editing a command's group moves it
            # without changing its ID or leaving a second copy in the source.
            existing = next(
                (item for source in snippets["groups"] for item in source["items"] if item["id"] == snippet_id),
                None,
            ) if snippet_id else None
            if existing is None:
                existing = {"id": _new_id(), "name": "", "command": "", "note": ""}
                group["items"].append(existing)
            else:
                for source in snippets["groups"]:
                    if source is not group:
                        source["items"] = [item for item in source["items"] if item["id"] != snippet_id]
                if not any(item is existing for item in group["items"]):
                    group["items"].append(existing)
            existing["name"] = name or existing.get("name") or command[:40]
            existing["command"] = command
            existing["note"] = note
            self.save()
            return existing["id"]

    def delete_snippet(self, payload: dict[str, Any]) -> None:
        """Remove one command — hidden when it is a built-in."""
        if not isinstance(payload, dict):
            raise ValueError("参数无效")
        snippet_id = _clean(payload.get("id"), 60)
        if not snippet_id:
            raise ValueError("缺少指令 ID")
        with self._lock:
            snippets = self._snippet_state()
            if payload.get("builtin"):
                snippets["overrides"].pop(snippet_id, None)
                if snippet_id not in snippets["hidden"]:
                    snippets["hidden"].append(snippet_id)
            else:
                for group in snippets["groups"]:
                    group["items"] = [i for i in group["items"] if i["id"] != snippet_id]
            self.save()

    def reset_snippets(self) -> None:
        """Restore built-in commands while preserving custom groups and commands."""
        with self._lock:
            snippets = self._snippet_state()
            snippets["overrides"] = {}
            snippets["hidden"] = []
            self.save()

    def _snippet_state(self) -> dict[str, Any]:
        """Return the mutable snippet section, creating it when missing."""
        snippets = self.data.get("snippets")
        if not isinstance(snippets, dict):
            snippets = {"groups": [], "overrides": {}, "hidden": []}
            self.data["snippets"] = snippets
        snippets.setdefault("groups", [])
        snippets.setdefault("overrides", {})
        snippets.setdefault("hidden", [])
        return snippets
