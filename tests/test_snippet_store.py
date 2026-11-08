"""Snippet CRUD persistence, catalogue merging, and Python bridge regressions."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest

from yhshell.api import Api
from yhshell.snippets import merged_groups, public_groups, snippet_total
from yhshell.store import Store


class SnippetStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="yhshell-snippets-")
        self.addCleanup(directory.cleanup)
        self.directory = directory.name
        self.store = Store(self.directory)

    def reload(self) -> dict:
        expected = self.store.snippet_config()
        self.store = Store(self.directory)
        self.assertEqual(self.store.snippet_config(), expected)
        return expected

    def test_custom_group_and_snippet_crud_persist(self) -> None:
        group_id = self.store.save_snippet_group({"name": " 我的分组 ", "description": "说明"})
        snippet_id = self.store.save_snippet(
            {"groupId": group_id, "name": "查看", "command": " echo <内容> ", "note": "备注"}
        )
        group = self.reload()["groups"][0]
        self.assertEqual(group["id"], group_id)
        self.assertEqual(group["name"], "我的分组")
        self.assertEqual(group["description"], "说明")
        self.assertEqual(group["items"], [
            {"id": snippet_id, "name": "查看", "command": "echo <内容>", "note": "备注"}
        ])
        self.assertEqual(self.store.save_snippet_group({"id": group_id, "name": "重命名"}), group_id)
        self.assertEqual(self.store.save_snippet(
            {"id": snippet_id, "groupId": group_id, "name": "更新", "command": "pwd", "note": ""}
        ), snippet_id)
        group = self.reload()["groups"][0]
        self.assertEqual(group["name"], "重命名")
        self.assertEqual(group["items"], [
            {"id": snippet_id, "name": "更新", "command": "pwd", "note": ""}
        ])
        self.store.delete_snippet({"id": snippet_id})
        self.assertEqual(self.reload()["groups"][0]["items"], [])
        self.store.save_snippet({"groupId": group_id, "command": "ls"})
        self.store.delete_snippet_group(group_id)
        self.assertEqual(self.reload()["groups"], [])

    def test_snippet_can_create_its_group_and_config_is_defensive(self) -> None:
        snippet_id = self.store.save_snippet({"groupName": "临时", "command": "echo <值>"})
        config = self.reload()
        self.assertEqual(config["groups"][0]["name"], "临时")
        self.assertEqual(config["groups"][0]["items"][0]["id"], snippet_id)
        config["groups"][0]["items"].clear()
        self.assertEqual(len(self.store.snippet_config()["groups"][0]["items"]), 1)

    def test_move_existing_snippet_preserves_id_and_removes_source(self) -> None:
        source = self.store.save_snippet_group({"name": "Source"})
        target = self.store.save_snippet_group({"name": "Target"})
        snippet_id = self.store.save_snippet({"groupId": source, "name": "Original", "command": "pwd"})
        other_id = self.store.save_snippet({"groupId": target, "command": "ls"})
        for destination in (target, source, target):
            with self.subTest(destination=destination):
                result = self.store.save_snippet(
                    {"id": snippet_id, "groupId": destination, "command": "echo moved"}
                )
                self.assertEqual(result, snippet_id)
                config = self.reload()
                matches = [(g["id"], i) for g in config["groups"] for i in g["items"] if i["id"] == snippet_id]
                self.assertEqual(len(matches), 1)
                self.assertEqual(matches[0][0], destination)
                self.assertEqual(matches[0][1]["name"], "Original")
                self.assertEqual(matches[0][1]["command"], "echo moved")
                self.assertEqual(sum(len(g["items"]) for g in config["groups"]), 2)
                self.assertTrue(any(i["id"] == other_id for g in config["groups"] for i in g["items"]))

    def test_move_existing_snippet_into_new_group(self) -> None:
        snippet_id = self.store.save_snippet({"groupName": "Source", "command": "pwd"})
        self.assertEqual(self.store.save_snippet(
            {"id": snippet_id, "groupName": "New destination", "command": "ls"}
        ), snippet_id)
        groups = self.reload()["groups"]
        self.assertEqual(groups[0]["items"], [])
        self.assertEqual(groups[1]["name"], "New destination")
        self.assertEqual(groups[1]["items"][0]["id"], snippet_id)

    def test_builtin_override_hidden_restore_and_reset_preserve_custom_groups(self) -> None:
        self.store.save_snippet({"groupName": "Custom", "command": "echo <值>"})
        custom_groups = self.store.snippet_config()["groups"]
        payload = {"id": "k8s-pods-all", "builtin": True, "name": "Changed", "command": "kubectl get <资源>", "note": ""}
        self.assertEqual(self.store.save_snippet(payload), payload["id"])
        config = self.reload()
        item = next(i for g in merged_groups(config) for i in g["items"] if i["id"] == payload["id"])
        self.assertEqual(item["name"], "Changed")
        self.assertEqual(item["command"], payload["command"])
        self.assertEqual(item["note"], "")
        self.assertEqual(item["variables"], ["资源"])
        self.assertTrue(item["builtin"])
        self.assertTrue(item["edited"])
        self.assertTrue(item["favorite"])
        for _ in range(2):
            self.store.delete_snippet({"id": payload["id"], "builtin": True})
        config = self.reload()
        self.assertEqual(config["hidden"], [payload["id"]])
        self.assertNotIn(payload["id"], config["overrides"])
        self.assertFalse(any(i["id"] == payload["id"] for g in merged_groups(config) for i in g["items"]))
        self.store.save_snippet(payload)
        self.assertEqual(self.reload()["hidden"], [])
        self.store.delete_snippet({"id": "docker-ps", "builtin": True})
        self.store.reset_snippets()
        config = self.reload()
        self.assertEqual(config, {"groups": custom_groups, "overrides": {}, "hidden": []})
        catalogue = merged_groups(config)
        builtin_items = [i for g in catalogue if g["builtin"] for i in g["items"]]
        self.assertEqual(len(builtin_items), snippet_total())
        self.assertTrue(all(not i["edited"] for i in builtin_items))
        self.assertEqual(next(i for i in builtin_items if i["id"] == payload["id"])["command"], "kubectl get pods -A -o wide")

    def test_invalid_crud_payloads_do_not_change_persisted_config(self) -> None:
        original = self.store.snippet_config()
        operations = [
            lambda: self.store.save_snippet_group(None),
            lambda: self.store.save_snippet_group({"name": " "}),
            lambda: self.store.save_snippet(None),
            lambda: self.store.save_snippet({"command": " "}),
            lambda: self.store.save_snippet({"builtin": True, "command": "ls"}),
            lambda: self.store.delete_snippet(None),
            lambda: self.store.delete_snippet({}),
        ]
        for operation in operations:
            with self.subTest(operation=operation):
                with self.assertRaises(ValueError):
                    operation()
                self.assertEqual(self.reload(), original)

    def test_api_crud_returns_updated_catalogue_and_persists(self) -> None:
        api = Api(store=self.store, auto_sync=False)
        self.addCleanup(api.shutdown)
        response = api.snippet_save_group({"name": "API group"})
        self.assertTrue(response["ok"])
        group_id = response["groupId"]
        response = api.snippet_save({"groupId": group_id, "command": "echo <内容>"})
        self.assertTrue(response["ok"])
        snippet_id = response["id"]
        group = next(g for g in response["snippets"] if g["id"] == group_id)
        self.assertFalse(group["builtin"])
        self.assertEqual(group["items"][0]["variables"], ["内容"])
        self.assertFalse(group["items"][0]["builtin"])
        self.assertFalse(group["items"][0]["favorite"])
        json.dumps(response, ensure_ascii=False)
        self.reload()
        response = api.snippet_delete({"id": "docker-ps", "builtin": True})
        self.assertTrue(response["ok"])
        self.assertFalse(any(i["id"] == "docker-ps" for g in response["snippets"] for i in g["items"]))
        response = api.snippet_reset()
        self.assertTrue(response["ok"])
        self.assertTrue(any(g["id"] == group_id for g in response["snippets"]))
        self.assertTrue(api.snippet_delete({"id": snippet_id})["ok"])
        self.assertTrue(api.snippet_delete_group({"groupId": group_id})["ok"])
        self.assertEqual(Store(self.directory).snippet_config()["groups"], [])

    def test_api_guards_invalid_snippet_payloads(self) -> None:
        api = Api(store=self.store, auto_sync=False)
        self.addCleanup(api.shutdown)
        for method in (api.snippet_save_group, api.snippet_save, api.snippet_delete):
            with self.subTest(method=method.__name__):
                response = method({})
                self.assertFalse(response["ok"])
                self.assertTrue(response["error"])
        self.assertEqual(self.reload(), {"groups": [], "overrides": {}, "hidden": []})


class SnippetMergeTests(unittest.TestCase):
    def test_merge_does_not_mutate_inputs_or_builtin_definitions(self) -> None:
        builtin = public_groups()
        config = {"groups": [{"id": "mine", "name": "Mine", "items": [
            {"id": "custom", "command": "echo <值>", "note": "note"}
        ]}], "overrides": {"docker-ps": {"command": "docker ps --filter name=<名称>"}}, "hidden": ["git-status"]}
        original = copy.deepcopy(config)
        result = merged_groups(config)
        self.assertEqual(config, original)
        self.assertFalse(any(i["id"] == "git-status" for g in result for i in g["items"]))
        result[-1]["items"][0]["variables"].clear()
        result[0]["items"].clear()
        self.assertEqual(config, original)
        self.assertEqual(public_groups(), builtin)
        self.assertEqual(merged_groups(config)[-1]["items"][0]["variables"], ["值"])


if __name__ == "__main__":
    unittest.main()
