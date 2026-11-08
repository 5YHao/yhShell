"""Unit tests for the built-in command snippet library (指令集)."""

from __future__ import annotations

import json
import unittest

from yhshell.snippets import FAVORITE_IDS, extract_variables, public_groups, render_command, snippet_total


class SnippetLibraryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.groups = public_groups()

    def test_kubectl_commands_are_the_main_focus(self) -> None:
        self.assertTrue(self.groups)
        self.assertTrue(self.groups[0]["id"].startswith("k8s"))
        k8s = [item for group in self.groups if group["id"].startswith("k8s") for item in group["items"]]
        self.assertGreaterEqual(len(k8s), 30)
        self.assertEqual(snippet_total(self.groups), sum(len(group["items"]) for group in self.groups))

    def test_favorites_exist_for_terminal_quick_bar(self) -> None:
        snippets = {item["id"]: item for group in self.groups for item in group["items"]}
        favorites = {snippet_id for snippet_id, item in snippets.items() if item["favorite"]}
        self.assertGreaterEqual(len(favorites), 10)
        self.assertEqual(favorites, set(FAVORITE_IDS))
        self.assertTrue(all(snippet_id in snippets for snippet_id in FAVORITE_IDS))

    def test_groups_and_snippets_have_unique_ids(self) -> None:
        group_ids = [group["id"] for group in self.groups]
        self.assertEqual(len(group_ids), len(set(group_ids)))
        snippet_ids = [item["id"] for group in self.groups for item in group["items"]]
        self.assertEqual(len(snippet_ids), len(set(snippet_ids)))

    def test_every_snippet_is_labeled_and_single_line(self) -> None:
        for group in self.groups:
            self.assertTrue(group["name"].strip())
            for item in group["items"]:
                with self.subTest(snippet=item["id"]):
                    self.assertTrue(item["name"].strip())
                    command = item["command"]
                    self.assertTrue(command.strip())
                    self.assertEqual(command, command.strip())
                    self.assertNotIn("\n", command)
                    self.assertNotIn("\r", command)
                    self.assertLessEqual(len(command), 400)
                    self.assertEqual(item["variables"], extract_variables(command))

    def test_variable_placeholders_are_supported(self) -> None:
        command = "kubectl logs -f <Pod名> -n <命名空间> --tail <行数>"
        self.assertEqual(extract_variables(command), ["Pod名", "命名空间", "行数"])

    def test_extract_variables_deduplicates_and_ignores_bad_placeholders(self) -> None:
        self.assertEqual(extract_variables("<Pod> <Pod> <> <  > <a\nb>"), ["Pod"])
        self.assertEqual(extract_variables(""), [])
        self.assertEqual(extract_variables(None), [])

    def test_render_command_replaces_every_occurrence(self) -> None:
        rendered = render_command(
            "kubectl get pod <Pod名> -n <命名空间> && kubectl describe pod <Pod名> -n <命名空间>",
            {"Pod名": "api-0", "命名空间": "prod"},
        )
        self.assertEqual(
            rendered,
            "kubectl get pod api-0 -n prod && kubectl describe pod api-0 -n prod",
        )

    def test_blank_values_keep_placeholder(self) -> None:
        command = "kubectl logs <Pod名> -n <命名空间>"
        self.assertEqual(render_command(command, {"Pod名": "api-0"}), "kubectl logs api-0 -n <命名空间>")
        self.assertEqual(render_command(command, {"Pod名": "  ", "命名空间": ""}), command)
        self.assertEqual(render_command(command, None), command)

    def test_payload_is_json_serialisable_and_defensive(self) -> None:
        payload = public_groups()
        json.dumps(payload, ensure_ascii=False)
        payload[0]["items"].clear()
        self.assertTrue(public_groups()[0]["items"])

    def test_zmodem_helpers_are_present(self) -> None:
        commands = [item["command"] for group in self.groups for item in group["items"]]
        self.assertIn("sz <文件路径>", commands)
        self.assertIn("rz -y", commands)

    def test_no_empty_notes(self) -> None:
        for group in self.groups:
            self.assertTrue(group["description"].strip())
            for item in group["items"]:
                with self.subTest(snippet=item["id"]):
                    self.assertTrue(item["note"].strip())


if __name__ == "__main__":
    unittest.main()
