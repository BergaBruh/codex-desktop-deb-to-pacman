from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import tempfile
import textwrap
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PKGBUILD_PATH = REPOSITORY_ROOT / "PKGBUILD"
SRCINFO_PATH = REPOSITORY_ROOT / ".SRCINFO"
WRAPPER_PATH = REPOSITORY_ROOT / "scripts" / "install-and-enable-update-checker.sh"
SERVICE_PATH = REPOSITORY_ROOT / "updater" / "systemd" / "chatgpt-bin-update-check.service"
WORKFLOW_SERVICE_PATH = REPOSITORY_ROOT / "updater" / "systemd" / "chatgpt-bin-update-workflow.service"
TIMER_PATH = REPOSITORY_ROOT / "updater" / "systemd" / "chatgpt-bin-update-check.timer"

UPDATER_INSTALL_ROOT = "/usr/lib/chatgpt-bin/updater"
RECIPE_INSTALL_ROOT = f"{UPDATER_INSTALL_ROOT}/recipe"

EXPECTED_RUNTIME_DESTINATIONS = {
    f"{UPDATER_INSTALL_ROOT}/build_update.py",
    f"{UPDATER_INSTALL_ROOT}/check_update.py",
    f"{UPDATER_INSTALL_ROOT}/common.py",
    f"{UPDATER_INSTALL_ROOT}/deb_payload.py",
    f"{UPDATER_INSTALL_ROOT}/install_package.py",
    f"{UPDATER_INSTALL_ROOT}/review_source.py",
    f"{UPDATER_INSTALL_ROOT}/workflow_update.py",
}
EXPECTED_RECIPE_REQUIRED_SOURCES = {
    ".SRCINFO",
    "PKGBUILD",
    "scripts/deb_payload.py",
    "scripts/review_source.py",
    "scripts/update_source.py",
}
EXPECTED_PUBLIC_COMMANDS = {
    "/usr/bin/chatgpt-bin-check-update",
    "/usr/bin/chatgpt-bin-update",
}
EXPECTED_USER_UNITS = {
    "/usr/lib/systemd/user/chatgpt-bin-update-check.service",
    "/usr/lib/systemd/user/chatgpt-bin-update-workflow.service",
    "/usr/lib/systemd/user/chatgpt-bin-update-check.timer",
}
EXPECTED_PRIVILEGED_INSTALL_FILES = {
    "/usr/libexec/chatgpt-bin/install-package",
    "/usr/share/polkit-1/actions/org.chatgpt-bin.install-package.policy",
}


def _package_body(contents: str) -> str:
    return contents.split("package() {", 1)[1]


def _quoted_install_destinations(contents: str) -> set[str]:
    return {
        destination
        for destination in re.findall(r'"\$pkgdir([^"]+)"', contents)
        if destination.startswith(
            (
                "/usr/lib/chatgpt-bin/updater",
                "/usr/bin/",
                "/usr/lib/systemd/user/",
                "/usr/libexec/chatgpt-bin/",
                "/usr/share/polkit-1/actions/",
            )
        )
    }


def _startdir_references(contents: str) -> set[str]:
    return set(re.findall(r'"\$startdir/([^"]+)"', _package_body(contents)))


def _recipe_install_map(contents: str) -> dict[str, tuple[str, str]]:
    install_map: dict[str, tuple[str, str]] = {}
    pattern = re.compile(
        r'install -Dm(?P<mode>\d+) '
        r'"\$startdir/(?P<source>[^"]+)" '
        r'"\$pkgdir/usr/lib/chatgpt-bin/updater/recipe/(?P<destination>[^"]+)"'
    )
    for match in pattern.finditer(_package_body(contents)):
        source = match.group("source")
        if source in install_map:
            raise AssertionError(f"duplicate recipe source install: {source}")
        install_map[source] = (match.group("destination"), match.group("mode"))
    return install_map


def _expected_recipe_mode(source: str) -> str:
    if source in {"updater/chatgpt-bin-check-update", "updater/chatgpt-bin-install-package", "updater/chatgpt-bin-update"}:
        return "755"
    return "644"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class AutoupdatePackageContractTests(unittest.TestCase):
    def test_pkgbuild_installs_updater_snapshot_launchers_and_user_units(self) -> None:
        contents = _read(PKGBUILD_PATH)
        destinations = _quoted_install_destinations(contents)

        for source in sorted((REPOSITORY_ROOT / "updater").glob("*.py")):
            expected = f"{UPDATER_INSTALL_ROOT}/{source.name}"
            self.assertIn(expected, destinations)

        recipe_install_map = _recipe_install_map(contents)
        self.assertEqual(
            {destination for destination in destinations if destination.startswith(f"{RECIPE_INSTALL_ROOT}/")},
            {f"{RECIPE_INSTALL_ROOT}/{destination}" for destination, _mode in recipe_install_map.values()},
        )
        self.assertTrue(EXPECTED_RUNTIME_DESTINATIONS <= destinations)
        self.assertTrue(EXPECTED_PUBLIC_COMMANDS <= destinations)
        self.assertTrue(EXPECTED_USER_UNITS <= destinations)
        self.assertTrue(EXPECTED_PRIVILEGED_INSTALL_FILES <= destinations)
        self.assertNotIn(".INSTALL", contents)
        self.assertNotIn("install=", contents)
        self.assertNotIn("systemctl --user enable", contents)
        self.assertIn('python "$startdir/scripts/deb_payload.py"', contents)

        installed_files = {
            destination
            for destination in destinations
            if not destination.endswith("/")
        }
        self.assertFalse({destination for destination in installed_files if Path(destination).name == ".INSTALL"})

    def test_recipe_snapshot_contains_every_pkgbuild_startdir_source_at_same_relative_path(self) -> None:
        contents = _read(PKGBUILD_PATH)
        recipe_install_map = _recipe_install_map(contents)
        expected_sources = _startdir_references(contents) | EXPECTED_RECIPE_REQUIRED_SOURCES

        self.assertEqual(set(recipe_install_map), expected_sources)
        for source, (destination, mode) in recipe_install_map.items():
            with self.subTest(source=source):
                self.assertTrue((REPOSITORY_ROOT / source).is_file())
                self.assertEqual(destination, source)
                self.assertEqual(mode, _expected_recipe_mode(source))

    def test_runtime_dependencies_are_minimal_and_notifications_are_optional(self) -> None:
        pkgbuild = _read(PKGBUILD_PATH)
        srcinfo = _read(SRCINFO_PATH)

        self.assertRegex(pkgbuild, r"(?ms)^depends=\(.*?^  'libarchive'$.*?^\)")
        self.assertRegex(pkgbuild, r"(?ms)^depends=\(.*?^  'polkit'$.*?^\)")
        self.assertRegex(pkgbuild, r"(?ms)^depends=\(.*?^  'python'$.*?^\)")
        self.assertNotRegex(pkgbuild, r"(?ms)^depends=\(.*?^  'libnotify'$.*?^\)")
        self.assertIn("libnotify:", pkgbuild)
        self.assertRegex(srcinfo, r"(?m)^\tdepends = libarchive$")
        self.assertRegex(srcinfo, r"(?m)^\tdepends = polkit$")
        self.assertRegex(srcinfo, r"(?m)^\tdepends = python$")
        self.assertNotRegex(srcinfo, r"(?m)^\tdepends = libnotify$")
        self.assertRegex(srcinfo, r"(?m)^\toptdepends = libnotify:")

    def test_user_units_keep_manual_check_detection_only_and_timer_targets_workflow(self) -> None:
        service = _read(SERVICE_PATH)
        workflow_service = _read(WORKFLOW_SERVICE_PATH)
        timer = _read(TIMER_PATH)

        self.assertIn("[Service]", service)
        self.assertIn("Type=oneshot", service)
        self.assertIn("ExecStart=/usr/bin/chatgpt-bin-check-update", service)
        self.assertIn("SuccessExitStatus=10", service)
        self.assertNotIn("[Install]", service)
        for forbidden in ("makepkg", "pkexec", "sudo", "pacman", "systemctl", "chatgpt-bin-update build"):
            self.assertNotIn(forbidden, service)

        self.assertIn("[Service]", workflow_service)
        self.assertIn("Type=oneshot", workflow_service)
        self.assertIn("ExecStart=/usr/bin/chatgpt-bin-update workflow", workflow_service)
        self.assertNotIn("[Install]", workflow_service)
        for forbidden in ("pkexec", "sudo", "pacman", "systemctl"):
            self.assertNotIn(forbidden, workflow_service)

        self.assertIn("[Timer]", timer)
        self.assertIn("OnCalendar=daily", timer)
        self.assertIn("RandomizedDelaySec=1h", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("Unit=chatgpt-bin-update-workflow.service", timer)
        self.assertIn("[Install]", timer)
        self.assertIn("WantedBy=timers.target", timer)

    def test_persistent_is_only_in_timer_file(self) -> None:
        non_timer_paths = [
            PKGBUILD_PATH,
            SRCINFO_PATH,
            WRAPPER_PATH,
            SERVICE_PATH,
            WORKFLOW_SERVICE_PATH,
            REPOSITORY_ROOT / "updater" / "chatgpt-bin-check-update",
            REPOSITORY_ROOT / "updater" / "chatgpt-bin-update",
            *(REPOSITORY_ROOT / "updater").glob("*.py"),
        ]

        for path in non_timer_paths:
            with self.subTest(path=path.relative_to(REPOSITORY_ROOT)):
                self.assertNotIn("Persistent=true", _read(path))

    def test_source_wrapper_enables_timer_only_after_successful_makepkg_install(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            call_log = root / "calls.log"
            self._write_fake_command(
                fake_bin / "makepkg",
                f"""\
                #!/bin/sh
                printf 'makepkg cwd=%s args=%s\\n' "$PWD" "$*" >> {str(call_log)!r}
                exit "${{MAKEPKG_STATUS:-0}}"
                """,
            )
            self._write_fake_command(
                fake_bin / "systemctl",
                f"""\
                #!/bin/sh
                printf 'systemctl cwd=%s args=%s\\n' "$PWD" "$*" >> {str(call_log)!r}
                exit 0
                """,
            )
            environment = os.environ.copy()
            environment["PATH"] = f"{fake_bin}:{environment['PATH']}"

            success = subprocess.run(
                [str(WRAPPER_PATH)],
                cwd=root,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(success.returncode, 0, success.stderr)
            self.assertEqual(
                call_log.read_text(encoding="utf-8").splitlines(),
                [
                    f"makepkg cwd={REPOSITORY_ROOT} args=-si",
                    f"systemctl cwd={REPOSITORY_ROOT} args=--user enable --now chatgpt-bin-update-check.timer",
                ],
            )

            call_log.unlink()
            failure_environment = environment | {"MAKEPKG_STATUS": "37"}
            failure = subprocess.run(
                [str(WRAPPER_PATH)],
                cwd=root,
                env=failure_environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(failure.returncode, 37)
            self.assertEqual(call_log.read_text(encoding="utf-8").splitlines(), [f"makepkg cwd={REPOSITORY_ROOT} args=-si"])

    def _write_fake_command(self, path: Path, contents: str) -> None:
        path.write_text(textwrap.dedent(contents), encoding="utf-8")
        path.chmod(0o755)


if __name__ == "__main__":
    unittest.main()
