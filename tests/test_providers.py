"""Unit tests for provider storage and upstream ``/models`` discovery.

Run from the repository root:

    .venv\\Scripts\\python.exe -m unittest tests.test_providers -v

They only touch temporary directories and a loopback HTTP server, so no real
provider, API key or network access is involved.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from yhshell.agent import fetch_models, models_endpoint_candidates
from yhshell.store import Store, model_ref, split_model_ref


def _write_config(directory: str, payload: dict) -> None:
    with open(os.path.join(directory, "config.json"), "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)


class StoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.mkdtemp(prefix="yhshell-store-")
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.store = Store(self.directory)

    def state(self) -> dict:
        return self.store.public_state()


class DefaultConfigTests(StoreTestCase):
    def test_defaults_seed_a_provider_without_models(self) -> None:
        state = self.state()
        self.assertEqual(len(state["providers"]), 1)
        self.assertEqual(state["providers"][0]["name"], "DeepSeek")
        self.assertEqual(state["providers"][0]["models"], [])
        self.assertEqual(state["models"], [])
        self.assertEqual(state["activeModelId"], "")

    def test_config_survives_reload(self) -> None:
        self.store.upsert_provider(
            {
                "name": "Ollama",
                "baseUrl": "http://127.0.0.1:11434/v1",
                "models": ["qwen2.5:7b"],
            }
        )
        reloaded = Store(self.directory)
        names = [provider["name"] for provider in reloaded.public_state()["providers"]]
        self.assertIn("Ollama", names)


class SeededPlaceholderTests(unittest.TestCase):
    """Old builds shipped with deepseek-chat preselected; it must not linger."""

    def setUp(self) -> None:
        self.directory = tempfile.mkdtemp(prefix="yhshell-seed-")
        self.addCleanup(shutil.rmtree, self.directory, True)

    def _seeded_config(self, **overrides) -> dict:
        provider = {
            "id": "seed12345678",
            "name": "DeepSeek",
            "baseUrl": "https://api.deepseek.com",
            "apiKey": "",
            "temperature": 0.3,
            "maxTokens": 2048,
            "models": ["deepseek-chat"],
            "modelOptions": ["deepseek-chat"],
            "lastSync": 0,
        }
        provider.update(overrides)
        return {
            "version": 2,
            "settings": {},
            "hosts": [],
            "providers": [provider],
            "activeModel": {"providerId": provider["id"], "model": "deepseek-chat"},
        }

    def test_untouched_placeholder_is_cleared(self) -> None:
        _write_config(self.directory, self._seeded_config())
        store = Store(self.directory)
        state = store.public_state()
        self.assertEqual(state["providers"][0]["models"], [])
        self.assertEqual(state["providers"][0]["modelOptions"], [])
        self.assertEqual(state["models"], [])
        self.assertEqual(state["activeModelId"], "")
        with open(os.path.join(self.directory, "config.json"), "r", encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved["version"], 3)
        self.assertEqual(saved["providers"][0]["models"], [])

    def test_placeholder_with_api_key_is_kept(self) -> None:
        _write_config(self.directory, self._seeded_config(apiKey="dpapi:fake"))
        store = Store(self.directory)
        self.assertEqual(store.public_state()["providers"][0]["models"], ["deepseek-chat"])

    def test_placeholder_with_fetched_options_is_kept(self) -> None:
        options = ["deepseek-chat", "deepseek-v3.2"]
        _write_config(self.directory, self._seeded_config(modelOptions=options))
        store = Store(self.directory)
        self.assertEqual(store.public_state()["providers"][0]["models"], ["deepseek-chat"])

    def test_pristine_v1_default_is_cleared(self) -> None:
        _write_config(
            self.directory,
            {
                "version": 1,
                "settings": {},
                "hosts": [],
                "models": [
                    {
                        "id": "aaa",
                        "name": "DeepSeek",
                        "baseUrl": "https://api.deepseek.com",
                        "model": "deepseek-chat",
                        "apiKey": "",
                        "temperature": 0.3,
                        "maxTokens": 2048,
                    }
                ],
                "activeModelId": "aaa",
            },
        )
        store = Store(self.directory)
        state = store.public_state()
        self.assertEqual(state["providers"][0]["models"], [])
        self.assertEqual(state["activeModelId"], "")


class LegacyMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.mkdtemp(prefix="yhshell-legacy-")
        self.addCleanup(shutil.rmtree, self.directory, True)
        _write_config(
            self.directory,
            {
                "version": 1,
                "settings": {},
                "hosts": [],
                "models": [
                    {
                        "id": "aaa",
                        "name": "DeepSeek",
                        "baseUrl": "https://api.deepseek.com",
                        "model": "deepseek-chat",
                        "apiKey": "",
                        "temperature": 0.2,
                        "maxTokens": 1024,
                    },
                    {
                        "id": "bbb",
                        "name": "DeepSeek",
                        "baseUrl": "https://api.deepseek.com",
                        "model": "deepseek-reasoner",
                        "apiKey": "",
                        "temperature": 0.2,
                        "maxTokens": 1024,
                    },
                    {
                        "id": "ccc",
                        "name": "Ollama",
                        "baseUrl": "http://127.0.0.1:11434/v1",
                        "model": "qwen2.5:7b",
                        "apiKey": "",
                        "temperature": 0.4,
                        "maxTokens": 2048,
                    },
                ],
                "activeModelId": "ccc",
            },
        )
        self.store = Store(self.directory)

    def test_models_grouped_by_endpoint(self) -> None:
        providers = self.store.public_state()["providers"]
        self.assertEqual(len(providers), 2)
        self.assertEqual(providers[0]["models"], ["deepseek-chat", "deepseek-reasoner"])
        self.assertEqual(providers[1]["models"], ["qwen2.5:7b"])

    def test_legacy_active_model_is_preserved(self) -> None:
        state = self.store.public_state()
        ollama = state["providers"][1]
        self.assertEqual(state["activeModelId"], model_ref(ollama["id"], "qwen2.5:7b"))
        self.assertEqual(self.store.get_model()["model"], "qwen2.5:7b")

    def test_all_legacy_models_stay_selectable(self) -> None:
        models = self.store.public_state()["models"]
        self.assertEqual(len(models), 3)
        labels = {model["label"] for model in models}
        self.assertIn("DeepSeek · deepseek-chat", labels)
        self.assertIn("Ollama", labels)

    def test_migration_is_written_back(self) -> None:
        with open(os.path.join(self.directory, "config.json"), "r", encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertIn("providers", saved)
        self.assertNotIn("models", saved)
        self.assertEqual(saved["version"], 3)


class ProviderCrudTests(StoreTestCase):
    def _add(self, **overrides) -> str:
        payload = {
            "name": "本地 Ollama",
            "baseUrl": "http://127.0.0.1:11434/v1/",
            "apiKey": "sk-test-12345678",
            "models": ["qwen2.5:7b", "qwen2.5:7b", "llama3.1"],
            "modelOptions": ["qwen2.5:7b", "llama3.1", "nomic-embed-text"],
            "temperature": 0.5,
            "maxTokens": 4096,
        }
        payload.update(overrides)
        return self.store.upsert_provider(payload)

    def test_upsert_normalises_input(self) -> None:
        provider_id = self._add()
        provider = next(p for p in self.state()["providers"] if p["id"] == provider_id)
        self.assertEqual(provider["baseUrl"], "http://127.0.0.1:11434/v1")
        self.assertEqual(provider["models"], ["qwen2.5:7b", "llama3.1"])
        self.assertEqual(provider["models"][0], "qwen2.5:7b")
        self.assertEqual(provider["temperature"], 0.5)
        self.assertEqual(provider["maxTokens"], 4096)

    def test_api_key_is_encrypted_and_hinted(self) -> None:
        provider_id = self._add()
        provider = next(p for p in self.state()["providers"] if p["id"] == provider_id)
        stored = self.store.get_provider(provider_id)["apiKey"]
        self.assertNotIn("sk-test-12345678", stored)
        self.assertTrue(provider["hasKey"])
        self.assertTrue(provider["keyHint"].endswith("5678"))
        self.assertEqual(self.store.provider_key(provider_id), "sk-test-12345678")

    def test_blank_key_keeps_existing_secret(self) -> None:
        provider_id = self._add()
        self.store.upsert_provider({"id": provider_id, "name": "Ollama", "baseUrl": "http://127.0.0.1:11434/v1"})
        self.assertEqual(self.store.provider_key(provider_id), "sk-test-12345678")

    def test_clear_key_removes_secret(self) -> None:
        provider_id = self._add()
        self.store.upsert_provider(
            {"id": provider_id, "name": "Ollama", "baseUrl": "http://127.0.0.1:11434/v1", "clearKey": True}
        )
        self.assertEqual(self.store.provider_key(provider_id), "")

    def test_base_url_must_be_http(self) -> None:
        with self.assertRaises(ValueError):
            self.store.upsert_provider({"name": "x", "baseUrl": "ftp://example.com"})

    def test_active_model_switch_and_lookup(self) -> None:
        provider_id = self._add()
        ref = model_ref(provider_id, "llama3.1")
        self.store.set_active_model(ref)
        self.assertEqual(self.store.public_state()["activeModelId"], ref)
        model = self.store.get_model()
        self.assertEqual(model["model"], "llama3.1")
        self.assertEqual(model["baseUrl"], "http://127.0.0.1:11434/v1")
        # The chat client decrypts the key itself, so get_model exposes the stored token.
        self.assertNotEqual(model["apiKey"], "sk-test-12345678")

    def test_unknown_model_ref_is_rejected(self) -> None:
        self.assertIsNone(self.store.get_model("nope::ghost"))
        before = self.state()["activeModelId"]
        self.store.set_active_model("nope::ghost")
        self.assertEqual(self.state()["activeModelId"], before)

    def test_removing_active_model_falls_back_within_provider(self) -> None:
        provider_id = self._add()
        self.store.set_active_model(model_ref(provider_id, "llama3.1"))
        self.store.set_provider_models(provider_id, ["qwen2.5:7b"])
        state = self.state()
        self.assertEqual(state["activeModelId"], model_ref(provider_id, "qwen2.5:7b"))
        self.assertEqual(self.store.get_provider(provider_id)["models"], ["qwen2.5:7b"])

    def test_delete_provider_and_reseed(self) -> None:
        provider_id = self._add()
        self.store.delete_provider(provider_id)
        self.assertIsNone(self.store.get_provider(provider_id))
        # Deleting the very last provider restores the starter configuration.
        for provider in list(self.store.data["providers"]):
            self.store.delete_provider(provider["id"])
        self.assertEqual(len(self.state()["providers"]), 1)

    def test_model_ids_may_contain_slashes(self) -> None:
        provider_id = self.store.upsert_provider(
            {"name": "Gateway", "baseUrl": "http://127.0.0.1:4000/v1", "models": ["meta-llama/Llama-3-8B"]}
        )
        ref = model_ref(provider_id, "meta-llama/Llama-3-8B")
        self.assertEqual(split_model_ref(ref), (provider_id, "meta-llama/Llama-3-8B"))
        self.store.set_active_model(ref)
        self.assertEqual(self.store.get_model()["model"], "meta-llama/Llama-3-8B")


class FakeModelsHandler(BaseHTTPRequestHandler):
    responses: dict[str, tuple[int, bytes]] = {}
    last_auth = ""

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        FakeModelsHandler.last_auth = self.headers.get("Authorization") or ""
        status, body = self.responses.get(self.path, (404, b'{"error": "not found"}'))
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # keep test output clean
        pass


class ModelsEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = HTTPServer(("127.0.0.1", 0), FakeModelsHandler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        FakeModelsHandler.responses = {}

    def test_candidates_for_base_urls(self) -> None:
        self.assertEqual(
            models_endpoint_candidates("https://api.deepseek.com"),
            ["https://api.deepseek.com/v1/models", "https://api.deepseek.com/models"],
        )
        self.assertEqual(
            models_endpoint_candidates("https://open.bigmodel.cn/api/paas/v4/"),
            ["https://open.bigmodel.cn/api/paas/v4/models"],
        )
        self.assertEqual(
            models_endpoint_candidates("https://api.openai.com/v1/chat/completions"),
            ["https://api.openai.com/v1/models"],
        )
        self.assertEqual(
            models_endpoint_candidates("https://api.example.com/v1/models"),
            ["https://api.example.com/v1/models"],
        )
        with self.assertRaises(RuntimeError):
            models_endpoint_candidates("")

    def test_parses_openai_list_and_dedupes(self) -> None:
        body = json.dumps({"object": "list", "data": [{"id": "a-1"}, {"id": "b-2"}, {"id": "a-1"}]}).encode()
        FakeModelsHandler.responses = {"/v1/models": (200, body)}
        result = fetch_models(self.base + "/v1", "sk-abc")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["models"], ["a-1", "b-2"])
        self.assertEqual(result["endpoint"], self.base + "/v1/models")

    def test_sends_bearer_token(self) -> None:
        captured = {}

        class CaptureHandler(FakeModelsHandler):
            def do_GET(self) -> None:  # noqa: N802
                captured["auth"] = self.headers.get("Authorization")
                captured["accept"] = self.headers.get("Accept")
                super().do_GET()

        server = HTTPServer(("127.0.0.1", 0), CaptureHandler)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            CaptureHandler.responses = {"/v1/models": (200, b'{"data": [{"id": "m"}]}')}
            result = fetch_models(base + "/v1", "sk-abc")
        finally:
            server.shutdown()
            server.server_close()
        self.assertTrue(result["ok"], result)
        self.assertEqual(captured["auth"], "Bearer sk-abc")
        self.assertEqual(captured["accept"], "application/json")

    def test_falls_back_to_bare_models_path(self) -> None:
        FakeModelsHandler.responses = {"/models": (200, b'{"models": [{"name": "x"}]}')}
        result = fetch_models(self.base, "")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["models"], ["x"])
        self.assertEqual(result["endpoint"], self.base + "/models")

    def test_surfaces_upstream_error(self) -> None:
        FakeModelsHandler.responses = {"/v1/models": (401, b'{"error": {"message": "bad key"}}')}
        result = fetch_models(self.base + "/v1", "sk-wrong")
        self.assertFalse(result["ok"])
        self.assertIn("401", result["message"])
        self.assertIn("bad key", result["message"])

    def test_empty_list_is_reported(self) -> None:
        FakeModelsHandler.responses = {"/v1/models": (200, b'{"data": []}')}
        result = fetch_models(self.base + "/v1", "")
        self.assertFalse(result["ok"])
        self.assertIn("没有模型", result["message"])


class FakeChatHandler(BaseHTTPRequestHandler):
    last_auth = ""

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        FakeChatHandler.last_auth = self.headers.get("Authorization") or ""
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        body = json.dumps({"choices": [{"message": {"role": "assistant", "content": "pong"}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


class ChatClientTests(unittest.TestCase):
    """get_model() -> test_model() must speak the chat protocol with the stored key."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = HTTPServer(("127.0.0.1", 0), FakeChatHandler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        self.directory = tempfile.mkdtemp(prefix="yhshell-chat-")
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.store = Store(self.directory)
        self.provider_id = self.store.upsert_provider(
            {
                "name": "Local",
                "baseUrl": self.base,
                "apiKey": "sk-chat",
                "models": ["m1"],
                "temperature": 0.2,
                "maxTokens": 128,
            }
        )

    def test_test_model_uses_saved_key(self) -> None:
        from yhshell.agent import test_model

        model = self.store.get_model(model_ref(self.provider_id, "m1"))
        self.assertIsNotNone(model)
        result = test_model(model)
        self.assertTrue(result["ok"], result)
        self.assertIn("pong", result["message"])
        self.assertEqual(FakeChatHandler.last_auth, "Bearer sk-chat")


class SyncModelsTests(unittest.TestCase):
    """The automatic refresh replaces the whole list with the upstream one."""

    def setUp(self) -> None:
        self.directory = tempfile.mkdtemp(prefix="yhshell-sync-")
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.store = Store(self.directory)
        self.provider_id = self.store.upsert_provider(
            {"name": "Local", "baseUrl": "http://127.0.0.1:1/v1", "models": ["old-model"]}
        )

    def test_sync_replaces_every_model(self) -> None:
        changed = self.store.sync_provider_models(self.provider_id, ["a", "b"])
        provider = self.store.get_provider(self.provider_id)
        self.assertTrue(changed)
        self.assertEqual(provider["models"], ["a", "b"])
        self.assertEqual(provider["modelOptions"], ["a", "b"])
        self.assertGreater(provider["lastSync"], 0)
        self.assertEqual(self.store.public_state()["activeModelId"], model_ref(self.provider_id, "a"))

    def test_sync_is_idempotent(self) -> None:
        self.store.sync_provider_models(self.provider_id, ["a", "b"])
        self.assertFalse(self.store.sync_provider_models(self.provider_id, ["a", "b"]))

    def test_empty_catalogue_does_not_wipe_models(self) -> None:
        self.assertFalse(self.store.sync_provider_models(self.provider_id, []))
        self.assertEqual(self.store.get_provider(self.provider_id)["models"], ["old-model"])

    def test_active_model_survives_when_still_advertised(self) -> None:
        self.store.sync_provider_models(self.provider_id, ["a", "b"])
        self.store.set_active_model(model_ref(self.provider_id, "b"))
        self.store.sync_provider_models(self.provider_id, ["c", "b"])
        self.assertEqual(self.store.public_state()["activeModelId"], model_ref(self.provider_id, "b"))

    def test_sync_hours_setting_defaults_and_clamps(self) -> None:
        self.assertEqual(self.store.settings["modelSyncHours"], 24)
        self.assertEqual(self.store.update_settings({"modelSyncHours": 0})["modelSyncHours"], 0)
        self.assertEqual(self.store.update_settings({"modelSyncHours": 999})["modelSyncHours"], 720)
        self.assertEqual(self.store.update_settings({"modelSyncHours": "nope"})["modelSyncHours"], 24)


class AutoSyncTests(unittest.TestCase):
    """Api-level background refresh: due checks, force sync, failure handling."""

    def setUp(self) -> None:
        from yhshell.api import Api

        self.directory = tempfile.mkdtemp(prefix="yhshell-autosync-")
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.store = Store(self.directory)
        self.api = Api(store=self.store, auto_sync=False)
        self.server = HTTPServer(("127.0.0.1", 0), FakeModelsHandler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        FakeModelsHandler.responses = {"/v1/models": (200, b'{"data": [{"id": "a"}, {"id": "b"}]}')}
        self.provider_id = self.store.upsert_provider(
            {"name": "Local", "baseUrl": self.base + "/v1", "apiKey": "sk-sync", "models": ["old-model"]}
        )

    def test_force_sync_updates_all_models(self) -> None:
        result = self.api.sync_all_models()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["synced"], 1)
        self.assertEqual(self.store.get_provider(self.provider_id)["models"], ["a", "b"])
        self.assertTrue(any(model["model"] == "a" for model in result["models"]))

    def test_due_sync_skips_fresh_providers(self) -> None:
        self.store.sync_provider_models(self.provider_id, ["old-model"])
        # lastSync is now, and the default interval is 24h.
        result = self.api._sync_due(force=False)
        self.assertEqual(result["synced"], 0)

    def test_due_sync_skips_when_disabled(self) -> None:
        self.store.update_settings({"modelSyncHours": 0})
        result = self.api._sync_due(force=False)
        self.assertTrue(result["skipped"])
        self.assertEqual(result["synced"], 0)

    def test_failed_sync_keeps_existing_models(self) -> None:
        FakeModelsHandler.responses = {"/v1/models": (500, b'{"error": "boom"}')}
        result = self.api.sync_all_models()
        self.assertEqual(result["synced"], 0)
        self.assertEqual(len(result["failed"]), 1)
        self.assertEqual(self.store.get_provider(self.provider_id)["models"], ["old-model"])

    def test_unconfigured_https_providers_are_not_synced(self) -> None:
        # The starter DeepSeek provider has no key, so it must not be probed.
        deepseek = self.store.get_provider(self.store.data["providers"][0]["id"])
        self.assertEqual(deepseek["baseUrl"], "https://api.deepseek.com")
        self.assertNotIn("deepseek-chat", [model for model in deepseek["models"]])
        self.assertFalse(self.api._syncable(deepseek))

    def test_saving_provider_without_models_fetches_in_background(self) -> None:
        provider_id = self.store.upsert_provider(
            {"name": "Saved empty", "baseUrl": self.base + "/v1", "apiKey": "sk-bg"}
        )
        self.assertEqual(self.store.get_provider(provider_id)["models"], [])
        self.api.save_provider(
            {"id": provider_id, "name": "Saved empty", "baseUrl": self.base + "/v1", "apiKey": "sk-bg"}
        )
        deadline = time.time() + 5
        while time.time() < deadline and not self.store.get_provider(provider_id)["models"]:
            time.sleep(0.05)
        self.assertEqual(self.store.get_provider(provider_id)["models"], ["a", "b"])


class BridgeTests(unittest.TestCase):
    """The bridge endpoint must reuse a saved key when the form leaves it blank."""

    def setUp(self) -> None:
        from yhshell.api import Api  # imported lazily: pulls in pywebview

        self.directory = tempfile.mkdtemp(prefix="yhshell-bridge-")
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.store = Store(self.directory)
        self.api = Api(store=self.store, auto_sync=False)
        self.server = HTTPServer(("127.0.0.1", 0), FakeModelsHandler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        FakeModelsHandler.responses = {"/v1/models": (200, b'{"data": [{"id": "a"}, {"id": "b"}]}')}

    def test_fetch_requires_base_url(self) -> None:
        result = self.api.fetch_provider_models({"baseUrl": ""})
        self.assertFalse(result["ok"])

    def test_fetch_uses_saved_key_and_url(self) -> None:
        provider_id = self.store.upsert_provider(
            {"name": "Local", "baseUrl": self.base + "/v1", "apiKey": "sk-local", "models": ["a"]}
        )
        result = self.api.fetch_provider_models({"providerId": provider_id})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["models"], ["a", "b"])
        self.assertEqual(FakeModelsHandler.last_auth, "Bearer sk-local")

    def test_fetch_without_saved_key_is_rejected_for_https(self) -> None:
        provider = self.store.get_provider(self.store.data["providers"][0]["id"])
        provider["apiKey"] = "dpapi:not-a-real-token"  # unreadable => empty key
        result = self.api.fetch_provider_models(
            {"providerId": provider["id"], "baseUrl": "https://api.deepseek.com"}
        )
        self.assertFalse(result["ok"])
        self.assertIn("API Key", result["message"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
