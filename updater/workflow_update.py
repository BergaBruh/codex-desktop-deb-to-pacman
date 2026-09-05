from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from pathlib import Path
import subprocess
import sys


CHECKER_COMMAND = "/usr/bin/chatgpt-bin-check-update"
UPDATER_COMMAND = "/usr/bin/chatgpt-bin-update"
NOTIFY_SEND = Path("/usr/bin/notify-send")
INSTALL_ACTION = "install"
FALLBACK_MESSAGE = "ChatGPT update built; run `chatgpt-bin-update install` to install it."


def _fallback() -> None:
    print(FALLBACK_MESSAGE)


def _selected_action(stdout: str) -> str | None:
    lines = stdout.splitlines()
    if len(lines) != 1:
        return None
    action = lines[0]
    if not action or "\r" in action or "\n" in action:
        return None
    return action


def _notify_install_action(
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]],
    notify_send: Path,
) -> str | None:
    if not notify_send.is_file():
        _fallback()
        return None
    result = runner(
        [
            str(notify_send),
            f"--action={INSTALL_ACTION}=Install",
            "ChatGPT update built",
            "Click Install to authenticate and install it.",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        _fallback()
        return None
    action = _selected_action(result.stdout)
    if action is None:
        _fallback()
        return None
    return action


def run_workflow(
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    notify_send: Path = NOTIFY_SEND,
) -> int:
    check_result = runner(
        [CHECKER_COMMAND, "--defer-new-candidate-notification"],
        check=False,
        capture_output=False,
        text=True,
    )
    if check_result.returncode == 0:
        return 0
    if check_result.returncode != 10:
        return int(check_result.returncode)

    build_result = runner([UPDATER_COMMAND, "build"], check=False, capture_output=False, text=True)
    if build_result.returncode != 0:
        return int(build_result.returncode)

    action = _notify_install_action(runner=runner, notify_send=notify_send)
    if action is None:
        return 0
    if action != INSTALL_ACTION:
        _fallback()
        return 0

    install_result = runner([UPDATER_COMMAND, "install"], check=False)
    return int(install_result.returncode)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check, build, and prompt for a reviewed ChatGPT package update")
    parser.parse_args(list(argv) if argv is not None else sys.argv[1:])
    return run_workflow()


if __name__ == "__main__":
    raise SystemExit(main())
