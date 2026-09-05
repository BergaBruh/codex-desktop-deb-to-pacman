from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock

import updater.build_update as build_update
import updater.install_package as install_package


PACKAGE_NAME = "chatgpt-bin"
PACKAGE_VERSION = "26.810.52044"
PACKAGE_ARCH = "x86_64"


class _PacmanRunner:
    def __init__(self, *, returncode: int = 0) -> None:
        self.returncode = returncode
        self.calls: list[list[str]] = []
        self.staging_modes: list[int] = []

    def __call__(self, argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append([str(argument) for argument in argv])
        if len(argv) == 4 and argv[:3] == ["pacman", "-U", "--"]:
            self.staging_modes.append(stat.S_IMODE(Path(argv[3]).parent.stat().st_mode))
        return subprocess.CompletedProcess(argv, self.returncode, stdout="", stderr="")


def _add_file(archive: tarfile.TarFile, name: str, contents: bytes = b"payload", mode: int = 0o644) -> None:
    member = tarfile.TarInfo(name)
    member.mode = mode
    member.mtime = 200
    member.size = len(contents)
    archive.addfile(member, io.BytesIO(contents))


def _add_symlink(archive: tarfile.TarFile, name: str, target: str) -> None:
    member = tarfile.TarInfo(name)
    member.mode = 0o777
    member.mtime = 200
    member.type = tarfile.SYMTYPE
    member.linkname = target
    archive.addfile(member)


def _add_device(archive: tarfile.TarFile, name: str) -> None:
    member = tarfile.TarInfo(name)
    member.mode = 0o644
    member.mtime = 200
    member.type = tarfile.CHRTYPE
    member.devmajor = 1
    member.devminor = 3
    archive.addfile(member)


def _write_package(
    path: Path,
    *,
    extra: str | None = None,
    install_hook: bool = False,
    device: bool = False,
) -> None:
    with tarfile.open(path, "w") as archive:
        _add_file(
            archive,
            ".PKGINFO",
            (
                f"pkgname = {PACKAGE_NAME}\n"
                f"pkgver = {PACKAGE_VERSION}-1\n"
                f"arch = {PACKAGE_ARCH}\n"
            ).encode("utf-8"),
        )
        _add_file(archive, ".BUILDINFO", b"buildenv = !distcc\n")
        _add_file(archive, "usr/lib/chatgpt/ChatGPT", mode=0o755)
        _add_symlink(archive, "usr/bin/chatgpt", "../lib/chatgpt/codex-launcher")
        _add_file(archive, "usr/share/applications/chatgpt.desktop")
        _add_file(archive, "usr/share/pixmaps/chatgpt.png")
        _add_file(archive, "usr/share/doc/chatgpt/copyright")
        _add_file(archive, "etc/apparmor.d/chatgpt")
        _add_file(archive, "usr/share/licenses/chatgpt-bin/copyright")
        _add_file(archive, "usr/bin/chatgpt-bin-update", mode=0o755)
        _add_file(archive, "usr/bin/chatgpt-bin-check-update", mode=0o755)
        _add_file(archive, "usr/lib/chatgpt-bin/updater/workflow_update.py")
        _add_file(archive, "usr/lib/chatgpt-bin/updater/recipe/updater/workflow_update.py")
        _add_file(archive, "usr/lib/chatgpt-bin/updater/recipe/updater/systemd/chatgpt-bin-update-workflow.service")
        _add_file(archive, "usr/lib/systemd/user/chatgpt-bin-update-workflow.service")
        _add_file(archive, "usr/libexec/chatgpt-bin/install-package", mode=0o755)
        _add_file(archive, "usr/share/polkit-1/actions/org.chatgpt-bin.install-package.policy")
        if install_hook:
            _add_file(archive, ".INSTALL", b"post_install() { :; }\n")
        if extra is not None:
            _add_file(archive, extra)
        if device:
            _add_device(archive, "usr/lib/chatgpt/device")


def _write_recipe(path: Path) -> Path:
    (path / "scripts").mkdir(parents=True)
    (path / "updater").mkdir()
    (path / "updater" / "polkit").mkdir()
    (path / "updater" / "systemd").mkdir()
    for relative in (
        "PKGBUILD",
        ".SRCINFO",
        "scripts/deb_payload.py",
        "scripts/review_source.py",
        "scripts/update_source.py",
        "updater/build_update.py",
        "updater/chatgpt-bin-check-update",
        "updater/chatgpt-bin-install-package",
        "updater/chatgpt-bin-update",
        "updater/check_update.py",
        "updater/common.py",
        "updater/install_package.py",
        "updater/workflow_update.py",
        "updater/polkit/org.chatgpt-bin.install-package.policy",
        "updater/systemd/chatgpt-bin-update-check.service",
        "updater/systemd/chatgpt-bin-update-workflow.service",
        "updater/systemd/chatgpt-bin-update-check.timer",
    ):
        target = path / relative
        target.write_text(f"# fixture {relative}\n", encoding="utf-8")
    return path


def _report_path(package_dir: Path, archive: Path) -> Path:
    archive_sha256 = hashlib.sha256(archive.read_bytes()).hexdigest()
    return package_dir / f"{PACKAGE_NAME}-{PACKAGE_VERSION}-{archive_sha256}.build.json"


def _allowed_package_paths() -> list[str]:
    return [
        "usr/bin/chatgpt",
        "usr/lib/chatgpt/ChatGPT",
        "usr/share/applications/chatgpt.desktop",
        "usr/share/pixmaps/chatgpt.png",
        "usr/share/doc/chatgpt/copyright",
        "etc/apparmor.d/chatgpt",
    ]


def _write_report(
    report: Path,
    archive: Path,
    candidate: Path,
    recipe: Path,
    *,
    sha256: str | None = None,
    name: str = PACKAGE_NAME,
) -> None:
    archive_sha256 = sha256 or hashlib.sha256(archive.read_bytes()).hexdigest()
    candidate_sha256 = hashlib.sha256(candidate.read_bytes()).hexdigest()
    allowed_package_paths = _allowed_package_paths()
    recipe_sha256 = build_update._hash_recipe(recipe)
    report.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "candidate": {
                    "version": PACKAGE_VERSION,
                    "sha256": candidate_sha256,
                    "source_date_epoch": 200,
                    "deb_path": str(candidate),
                    "allowed_members": allowed_package_paths,
                },
                "package": {
                    "name": name,
                    "version": PACKAGE_VERSION,
                    "architecture": PACKAGE_ARCH,
                    "archive_path": str(archive),
                    "sha256": archive_sha256,
                },
                "recipe": {
                    "source_path": str(recipe),
                    "base_sha256": recipe_sha256,
                    "sha256": recipe_sha256,
                },
                "allowed_package_paths": allowed_package_paths,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


class InstallPackageHelperTests(unittest.TestCase):
    def _fixture(self, root: Path):
        paths = build_update.cache_paths(
            {
                "XDG_CACHE_HOME": str(root / "cache"),
                "XDG_STATE_HOME": str(root / "state"),
            }
        )
        recipe = _write_recipe(root / "immutable-recipe")
        candidate_bytes = b"reviewed candidate deb"
        candidate_sha256 = hashlib.sha256(candidate_bytes).hexdigest()
        candidate = paths.download_dir / f"chatgpt_amd64-{candidate_sha256}.deb"
        candidate.write_bytes(candidate_bytes)
        archive = paths.package_dir / f"{PACKAGE_NAME}-{PACKAGE_VERSION}-1-{PACKAGE_ARCH}.pkg.tar"
        _write_package(archive)
        report = _report_path(paths.package_dir, archive)
        _write_report(report, archive, candidate, recipe)
        return paths, recipe, candidate, archive, report

    def _run_helper(
        self,
        argv: list[str],
        recipe: Path,
        runner: _PacmanRunner | None = None,
    ) -> tuple[int, _PacmanRunner]:
        pacman_runner = runner or _PacmanRunner()
        with mock.patch.object(install_package.os, "geteuid", return_value=0), mock.patch.dict(
            install_package.os.environ,
            {"PKEXEC_UID": str(os.getuid())},
            clear=False,
        ), mock.patch.object(install_package, "IMMUTABLE_RECIPE_DIR", recipe, create=True):
            return install_package.main(argv, pacman_runner=pacman_runner), pacman_runner

    def test_success_installs_only_the_private_staged_archive_from_contract_named_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            _paths, recipe, _candidate, archive, report = self._fixture(Path(temporary_directory))

            result, pacman = self._run_helper(["--archive", str(archive), "--report", str(report)], recipe)

            self.assertEqual(result, 0)
            self.assertEqual(len(pacman.calls), 1)
            self.assertEqual(pacman.calls[0][:3], ["pacman", "-U", "--"])
            staged_path = Path(pacman.calls[0][3])
            self.assertNotEqual(staged_path, archive)
            self.assertEqual(staged_path.name, archive.name)
            self.assertEqual(pacman.staging_modes, [0o700])
            self.assertFalse(staged_path.exists())

    def test_rejects_candidate_filename_not_bound_to_candidate_digest_before_pacman(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            paths, recipe, candidate, archive, report = self._fixture(Path(temporary_directory))
            wrong_candidate = paths.download_dir / "chatgpt_amd64.deb"
            candidate.replace(wrong_candidate)
            _write_report(report, archive, wrong_candidate, recipe)

            result, pacman = self._run_helper(["--archive", str(archive), "--report", str(report)], recipe)

            self.assertEqual(result, 2)
            self.assertEqual(pacman.calls, [])

    def test_rejects_caller_owned_report_and_archive_outside_the_updater_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            recipe = _write_recipe(root / "immutable-recipe")
            candidate = root / "chatgpt_amd64.deb"
            candidate.write_bytes(b"reviewed candidate deb")
            archive = root / f"{PACKAGE_NAME}-{PACKAGE_VERSION}-1-{PACKAGE_ARCH}.pkg.tar"
            _write_package(archive)
            report = _report_path(root, archive)
            _write_report(report, archive, candidate, recipe)

            result, pacman = self._run_helper(["--archive", str(archive), "--report", str(report)], recipe)

            self.assertEqual(result, 2)
            self.assertEqual(pacman.calls, [])

    def test_rejects_symlink_archive_before_pacman(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            paths, recipe, candidate, target, _report = self._fixture(Path(temporary_directory))
            symlink = paths.package_dir / f"{PACKAGE_NAME}-{PACKAGE_VERSION}-2-{PACKAGE_ARCH}.pkg.tar"
            symlink.symlink_to(target)
            report = _report_path(paths.package_dir, target)
            _write_report(report, symlink, candidate, recipe, sha256=hashlib.sha256(target.read_bytes()).hexdigest())

            result, pacman = self._run_helper(["--archive", str(symlink), "--report", str(report)], recipe)

            self.assertEqual(result, 2)
            self.assertEqual(pacman.calls, [])

    def test_rejects_wrong_archive_name_before_pacman(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            paths, recipe, candidate, _archive, _report = self._fixture(Path(temporary_directory))
            archive = paths.package_dir / f"not-chatgpt-{PACKAGE_VERSION}-1-{PACKAGE_ARCH}.pkg.tar"
            _write_package(archive)
            report = _report_path(paths.package_dir, archive)
            _write_report(report, archive, candidate, recipe)

            result, pacman = self._run_helper(["--archive", str(archive), "--report", str(report)], recipe)

            self.assertEqual(result, 2)
            self.assertEqual(pacman.calls, [])

    def test_rejects_install_hook_before_pacman(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            paths, recipe, candidate, _archive, _report = self._fixture(Path(temporary_directory))
            archive = paths.package_dir / f"{PACKAGE_NAME}-{PACKAGE_VERSION}-1-{PACKAGE_ARCH}.pkg.tar"
            _write_package(archive, install_hook=True)
            report = _report_path(paths.package_dir, archive)
            _write_report(report, archive, candidate, recipe)

            result, pacman = self._run_helper(["--archive", str(archive), "--report", str(report)], recipe)

            self.assertEqual(result, 2)
            self.assertEqual(pacman.calls, [])

    def test_rejects_extra_payload_path_before_pacman(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            paths, recipe, candidate, _archive, _report = self._fixture(Path(temporary_directory))
            archive = paths.package_dir / f"{PACKAGE_NAME}-{PACKAGE_VERSION}-1-{PACKAGE_ARCH}.pkg.tar"
            _write_package(archive, extra="usr/share/unsupported")
            report = _report_path(paths.package_dir, archive)
            _write_report(report, archive, candidate, recipe)

            result, pacman = self._run_helper(["--archive", str(archive), "--report", str(report)], recipe)

            self.assertEqual(result, 2)
            self.assertEqual(pacman.calls, [])

    def test_rejects_whitespace_prefixed_or_suffixed_archive_member_before_pacman(self) -> None:
        for extra in (" usr/bin/chatgpt", "usr/lib/chatgpt/ChatGPT "):
            with self.subTest(extra=repr(extra)), tempfile.TemporaryDirectory() as temporary_directory:
                paths, recipe, candidate, _archive, _report = self._fixture(Path(temporary_directory))
                archive = paths.package_dir / f"{PACKAGE_NAME}-{PACKAGE_VERSION}-1-{PACKAGE_ARCH}.pkg.tar"
                _write_package(archive, extra=extra)
                report = _report_path(paths.package_dir, archive)
                _write_report(report, archive, candidate, recipe)

                result, pacman = self._run_helper(["--archive", str(archive), "--report", str(report)], recipe)

                self.assertEqual(result, 2)
                self.assertEqual(pacman.calls, [])

    def test_rejects_device_entry_before_pacman(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            paths, recipe, candidate, _archive, _report = self._fixture(Path(temporary_directory))
            archive = paths.package_dir / f"{PACKAGE_NAME}-{PACKAGE_VERSION}-1-{PACKAGE_ARCH}.pkg.tar"
            _write_package(archive, device=True)
            report = _report_path(paths.package_dir, archive)
            _write_report(report, archive, candidate, recipe)

            result, pacman = self._run_helper(["--archive", str(archive), "--report", str(report)], recipe)

            self.assertEqual(result, 2)
            self.assertEqual(pacman.calls, [])

    def test_rejects_archive_swapped_after_open_before_pacman(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            paths, recipe, _candidate, archive, report = self._fixture(Path(temporary_directory))
            replacement = paths.package_dir / "replacement.pkg.tar"
            _write_package(replacement, extra="usr/share/unsupported")

            original_hook = install_package._source_archive_opened

            def swap_after_open(_path: Path) -> None:
                os.replace(replacement, archive)

            with mock.patch.object(install_package, "_source_archive_opened", side_effect=swap_after_open):
                result, pacman = self._run_helper(["--archive", str(archive), "--report", str(report)], recipe)

            self.assertIs(original_hook, install_package._source_archive_opened)
            self.assertEqual(result, 2)
            self.assertEqual(pacman.calls, [])

    def test_rejects_non_root_exact_args_and_wrong_pkexec_uid_before_pacman(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            _paths, recipe, _candidate, archive, report = self._fixture(Path(temporary_directory))

            with mock.patch.object(install_package.os, "geteuid", return_value=1):
                self.assertEqual(
                    install_package.main(["--archive", str(archive), "--report", str(report)], pacman_runner=_PacmanRunner()),
                    2,
                )

            result, pacman = self._run_helper(["--archive", str(archive), "--report", str(report), "--extra", "value"], recipe)
            self.assertEqual(result, 2)
            self.assertEqual(pacman.calls, [])

            wrong_owner = _PacmanRunner()
            with mock.patch.object(install_package.os, "geteuid", return_value=0), mock.patch.dict(
                install_package.os.environ,
                {"PKEXEC_UID": str(os.getuid() + 1)},
                clear=False,
            ), mock.patch.object(install_package, "IMMUTABLE_RECIPE_DIR", recipe, create=True):
                self.assertEqual(
                    install_package.main(["--archive", str(archive), "--report", str(report)], pacman_runner=wrong_owner),
                    2,
                )
            self.assertEqual(wrong_owner.calls, [])

    def test_rejects_candidate_or_recipe_binding_mismatch_before_pacman(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            _paths, recipe, candidate, archive, report = self._fixture(Path(temporary_directory))
            report_data = json.loads(report.read_text(encoding="utf-8"))
            report_data["candidate"]["sha256"] = "b" * 64
            report.write_text(json.dumps(report_data), encoding="utf-8")

            result, pacman = self._run_helper(["--archive", str(archive), "--report", str(report)], recipe)

            self.assertEqual(result, 2)
            self.assertEqual(pacman.calls, [])

            _write_report(report, archive, candidate, recipe)
            (recipe / "PKGBUILD").write_text("# changed recipe\n", encoding="utf-8")

            result, pacman = self._run_helper(["--archive", str(archive), "--report", str(report)], recipe)

            self.assertEqual(result, 2)
            self.assertEqual(pacman.calls, [])


class PublicInstallCommandTests(unittest.TestCase):
    def test_public_install_invokes_fixed_pkexec_helper_for_latest_build_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            paths = build_update.cache_paths(
                {
                    "XDG_CACHE_HOME": str(root / "cache"),
                    "XDG_STATE_HOME": str(root / "state"),
                }
            )
            recipe = _write_recipe(root / "immutable-recipe")
            candidate_bytes = b"reviewed candidate deb"
            candidate_sha256 = hashlib.sha256(candidate_bytes).hexdigest()
            candidate = paths.download_dir / f"chatgpt_amd64-{candidate_sha256}.deb"
            candidate.write_bytes(candidate_bytes)
            archive = paths.package_dir / f"{PACKAGE_NAME}-{PACKAGE_VERSION}-1-{PACKAGE_ARCH}.pkg.tar"
            _write_package(archive)
            report = _report_path(paths.package_dir, archive)
            _write_report(report, archive, candidate, recipe)
            calls: list[list[str]] = []

            def runner(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                calls.append([str(argument) for argument in argv])
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

            with mock.patch.object(build_update, "INSTALLED_RECIPE_DIR", recipe):
                self.assertEqual(build_update._install(paths, runner=runner), 0)

            self.assertEqual(
                calls,
                [
                    [
                        "pkexec",
                        "/usr/libexec/chatgpt-bin/install-package",
                        "--report",
                        str(report),
                        "--archive",
                        str(archive),
                    ]
                ],
            )

    def test_public_install_skips_newer_stale_recipe_report_for_current_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            paths = build_update.cache_paths(
                {
                    "XDG_CACHE_HOME": str(root / "cache"),
                    "XDG_STATE_HOME": str(root / "state"),
                }
            )
            current_recipe = _write_recipe(root / "current-recipe")
            stale_recipe = _write_recipe(root / "stale-recipe")
            (stale_recipe / "PKGBUILD").write_text("# stale immutable recipe\n", encoding="utf-8")
            candidate_bytes = b"reviewed candidate deb"
            candidate_sha256 = hashlib.sha256(candidate_bytes).hexdigest()
            candidate = paths.download_dir / f"chatgpt_amd64-{candidate_sha256}.deb"
            candidate.write_bytes(candidate_bytes)
            current_archive = paths.package_dir / f"{PACKAGE_NAME}-{PACKAGE_VERSION}-1-{PACKAGE_ARCH}.pkg.tar"
            stale_archive = paths.package_dir / f"{PACKAGE_NAME}-{PACKAGE_VERSION}-2-{PACKAGE_ARCH}.pkg.tar"
            _write_package(current_archive)
            _write_package(stale_archive, extra="usr/share/stale-recipe-only")
            current_report = _report_path(paths.package_dir, current_archive)
            stale_report = _report_path(paths.package_dir, stale_archive)
            _write_report(current_report, current_archive, candidate, current_recipe)
            _write_report(stale_report, stale_archive, candidate, stale_recipe)
            os.utime(current_report, ns=(1_000_000_000, 1_000_000_000))
            os.utime(stale_report, ns=(2_000_000_000, 2_000_000_000))
            calls: list[list[str]] = []

            def runner(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                calls.append([str(argument) for argument in argv])
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

            with mock.patch.object(build_update, "INSTALLED_RECIPE_DIR", current_recipe):
                self.assertEqual(build_update._install(paths, runner=runner), 0)

            self.assertEqual(
                calls,
                [
                    [
                        "pkexec",
                        "/usr/libexec/chatgpt-bin/install-package",
                        "--report",
                        str(current_report),
                        "--archive",
                        str(current_archive),
                    ]
                ],
            )

    def test_public_install_reports_no_compatible_current_build_without_pkexec(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            paths = build_update.cache_paths(
                {
                    "XDG_CACHE_HOME": str(root / "cache"),
                    "XDG_STATE_HOME": str(root / "state"),
                }
            )
            current_recipe = _write_recipe(root / "current-recipe")
            stale_recipe = _write_recipe(root / "stale-recipe")
            (stale_recipe / "PKGBUILD").write_text("# stale immutable recipe\n", encoding="utf-8")
            candidate_bytes = b"reviewed candidate deb"
            candidate_sha256 = hashlib.sha256(candidate_bytes).hexdigest()
            candidate = paths.download_dir / f"chatgpt_amd64-{candidate_sha256}.deb"
            candidate.write_bytes(candidate_bytes)
            archive = paths.package_dir / f"{PACKAGE_NAME}-{PACKAGE_VERSION}-1-{PACKAGE_ARCH}.pkg.tar"
            _write_package(archive)
            report = _report_path(paths.package_dir, archive)
            _write_report(report, archive, candidate, stale_recipe)
            calls: list[list[str]] = []

            def runner(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                calls.append([str(argument) for argument in argv])
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

            with mock.patch.object(build_update, "INSTALLED_RECIPE_DIR", current_recipe), mock.patch(
                "sys.stdout",
                new_callable=io.StringIO,
            ) as output:
                self.assertEqual(build_update._install(paths, runner=runner), 1)

            self.assertEqual(calls, [])
            self.assertIn("no-compatible-current-build", output.getvalue())

    def test_public_install_rejects_report_whose_archive_digest_changed_without_pkexec(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            paths = build_update.cache_paths(
                {
                    "XDG_CACHE_HOME": str(root / "cache"),
                    "XDG_STATE_HOME": str(root / "state"),
                }
            )
            recipe = _write_recipe(root / "immutable-recipe")
            candidate_bytes = b"reviewed candidate deb"
            candidate_sha256 = hashlib.sha256(candidate_bytes).hexdigest()
            candidate = paths.download_dir / f"chatgpt_amd64-{candidate_sha256}.deb"
            candidate.write_bytes(candidate_bytes)
            archive = paths.package_dir / f"{PACKAGE_NAME}-{PACKAGE_VERSION}-1-{PACKAGE_ARCH}.pkg.tar"
            _write_package(archive)
            report = _report_path(paths.package_dir, archive)
            _write_report(report, archive, candidate, recipe)
            archive.write_bytes(b"archive contents changed after report was written")
            calls: list[list[str]] = []

            def runner(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                calls.append([str(argument) for argument in argv])
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

            with mock.patch.object(build_update, "INSTALLED_RECIPE_DIR", recipe), mock.patch(
                "sys.stdout",
                new_callable=io.StringIO,
            ) as output:
                self.assertEqual(build_update._install(paths, runner=runner), 1)

            self.assertEqual(calls, [])
            self.assertIn("no-compatible-current-build", output.getvalue())

    def test_public_install_rejects_missing_build_report_without_pkexec(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            paths = build_update.cache_paths(
                {
                    "XDG_CACHE_HOME": str(root / "cache"),
                    "XDG_STATE_HOME": str(root / "state"),
                }
            )
            calls: list[list[str]] = []

            def runner(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                calls.append([str(argument) for argument in argv])
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

            self.assertEqual(build_update._install(paths, runner=runner), 1)
            self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
