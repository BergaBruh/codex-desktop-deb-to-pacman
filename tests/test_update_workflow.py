from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import updater.workflow_update as workflow_update


class _Runner:
    def __init__(
        self,
        *,
        check_returncode: int = 0,
        build_returncode: int = 0,
        notify_returncode: int = 0,
        notify_stdout: str = "",
        install_returncode: int = 0,
    ) -> None:
        self.check_returncode = check_returncode
        self.build_returncode = build_returncode
        self.notify_returncode = notify_returncode
        self.notify_stdout = notify_stdout
        self.install_returncode = install_returncode
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        command = [str(argument) for argument in argv]
        self.calls.append(command)
        if command == ["/usr/bin/chatgpt-bin-check-update", "--defer-new-candidate-notification"]:
            return subprocess.CompletedProcess(command, self.check_returncode, stdout="", stderr="")
        if command == ["/usr/bin/chatgpt-bin-update", "build"]:
            return subprocess.CompletedProcess(command, self.build_returncode, stdout="", stderr="")
        if command == ["/usr/bin/chatgpt-bin-update", "install"]:
            return subprocess.CompletedProcess(command, self.install_returncode, stdout="", stderr="")
        if len(command) >= 2 and command[1] == "--action=install=Install":
            return subprocess.CompletedProcess(command, self.notify_returncode, stdout=self.notify_stdout, stderr="")
        raise AssertionError(f"unexpected command: {command!r}")


class WorkflowUpdateTests(unittest.TestCase):
    def test_no_update_returns_success_without_build_or_notification(self) -> None:
        runner = _Runner(check_returncode=0)

        result = workflow_update.run_workflow(runner=runner)

        self.assertEqual(result, 0)
        self.assertEqual(runner.calls, [["/usr/bin/chatgpt-bin-check-update", "--defer-new-candidate-notification"]])

    def test_checker_error_is_returned_without_build_or_install(self) -> None:
        runner = _Runner(check_returncode=3)

        result = workflow_update.run_workflow(runner=runner)

        self.assertEqual(result, 3)
        self.assertEqual(runner.calls, [["/usr/bin/chatgpt-bin-check-update", "--defer-new-candidate-notification"]])

    def test_update_candidate_builds_and_fallback_does_not_install_when_notify_send_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            missing_notify = Path(temporary_directory) / "missing-notify-send"
            runner = _Runner(check_returncode=10)

            with mock.patch("builtins.print") as printed:
                result = workflow_update.run_workflow(runner=runner, notify_send=missing_notify)

        self.assertEqual(result, 0)
        self.assertEqual(
            runner.calls,
            [
                ["/usr/bin/chatgpt-bin-check-update", "--defer-new-candidate-notification"],
                ["/usr/bin/chatgpt-bin-update", "build"],
            ],
        )
        printed.assert_any_call("ChatGPT update built; run `chatgpt-bin-update install` to install it.")

    def test_build_error_is_returned_without_notification_or_install(self) -> None:
        runner = _Runner(check_returncode=10, build_returncode=2)

        result = workflow_update.run_workflow(runner=runner)

        self.assertEqual(result, 2)
        self.assertEqual(
            runner.calls,
            [
                ["/usr/bin/chatgpt-bin-check-update", "--defer-new-candidate-notification"],
                ["/usr/bin/chatgpt-bin-update", "build"],
            ],
        )

    def test_only_install_action_invokes_public_install_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            notify = Path(temporary_directory) / "notify-send"
            notify.write_text("#!/bin/sh\n", encoding="utf-8")
            notify.chmod(0o755)
            runner = _Runner(check_returncode=10, notify_stdout="install\n", install_returncode=0)

            result = workflow_update.run_workflow(runner=runner, notify_send=notify)

        self.assertEqual(result, 0)
        self.assertEqual(
            runner.calls,
            [
                ["/usr/bin/chatgpt-bin-check-update", "--defer-new-candidate-notification"],
                ["/usr/bin/chatgpt-bin-update", "build"],
                [str(notify), "--action=install=Install", "ChatGPT update built", "Click Install to authenticate and install it."],
                ["/usr/bin/chatgpt-bin-update", "install"],
            ],
        )

    def test_non_install_action_or_no_action_never_invokes_install(self) -> None:
        for action in ("", "default\n", "install\nextra\n"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as temporary_directory:
                notify = Path(temporary_directory) / "notify-send"
                notify.write_text("#!/bin/sh\n", encoding="utf-8")
                notify.chmod(0o755)
                runner = _Runner(check_returncode=10, notify_stdout=action)

                with mock.patch("builtins.print") as printed:
                    result = workflow_update.run_workflow(runner=runner, notify_send=notify)

                self.assertEqual(result, 0)
                self.assertNotIn(["/usr/bin/chatgpt-bin-update", "install"], runner.calls)
                printed.assert_any_call("ChatGPT update built; run `chatgpt-bin-update install` to install it.")

    def test_notify_send_failure_falls_back_without_installing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            notify = Path(temporary_directory) / "notify-send"
            notify.write_text("#!/bin/sh\n", encoding="utf-8")
            notify.chmod(0o755)
            runner = _Runner(check_returncode=10, notify_returncode=1)

            with mock.patch("builtins.print") as printed:
                result = workflow_update.run_workflow(runner=runner, notify_send=notify)

        self.assertEqual(result, 0)
        self.assertNotIn(["/usr/bin/chatgpt-bin-update", "install"], runner.calls)
        printed.assert_any_call("ChatGPT update built; run `chatgpt-bin-update install` to install it.")


if __name__ == "__main__":
    unittest.main()
