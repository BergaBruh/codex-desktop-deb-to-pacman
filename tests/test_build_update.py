from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import stat
import subprocess
import tempfile
import textwrap
import unittest
from unittest import mock

from tests.test_review_source import _build_deb
from updater.common import CandidateRecord, cache_paths
import updater.build_update as build_update


_FORBIDDEN_BUILD_FLAGS = {"-s", "-i", "--syncdeps", "--install"}
_FORBIDDEN_PRIVILEGED_COMMANDS = {"pkexec", "sudo", "pacman", "systemctl"}


class _FakeMakepkg:
    def __init__(self, *, returncode: int = 0, create_archive: bool = True, create_archive_on_failure: bool = False) -> None:
        self.returncode = returncode
        self.create_archive = create_archive
        self.create_archive_on_failure = create_archive_on_failure
        self.argv: list[str] = []
        self.kwargs: dict[str, object] = {}
        self.commands: list[list[str]] = []
        self.archive_existed_during_makepkg: bool | None = None

    def __call__(self, argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.argv = [str(argument) for argument in argv]
        self.kwargs = kwargs
        self.commands.append(self.argv)
        if self.argv[0] in _FORBIDDEN_PRIVILEGED_COMMANDS:
            raise AssertionError(f"privileged command was requested: {self.argv!r}")
        if self.create_archive and (self.returncode == 0 or self.create_archive_on_failure):
            environment = kwargs.get("env")
            if not isinstance(environment, dict) or "PKGDEST" not in environment:
                raise AssertionError("makepkg runner did not receive PKGDEST")
            archive = Path(str(environment["PKGDEST"])) / "chatgpt-bin-26.810.52044-1-x86_64.pkg.tar.zst"
            archive.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.archive_existed_during_makepkg = archive.exists()
            archive.write_bytes(b"fake arch package archive")
        return subprocess.CompletedProcess(self.argv, self.returncode, stdout="", stderr="makepkg failed")


class _UtimeOnlyMakepkg:
    def __init__(self, archive: Path) -> None:
        self.archive = archive
        self.argv: list[str] = []
        self.commands: list[list[str]] = []
        self.archive_existed_during_makepkg: bool | None = None

    def __call__(self, argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        self.argv = [str(argument) for argument in argv]
        self.commands.append(self.argv)
        self.archive_existed_during_makepkg = self.archive.exists()
        if self.archive_existed_during_makepkg:
            self.archive.touch()
        return subprocess.CompletedProcess(self.argv, 0, stdout="", stderr="")


class BuildUpdateTests(unittest.TestCase):
    def _paths(self, directory: Path):
        return cache_paths(
            {
                "XDG_CACHE_HOME": str(directory / "cache"),
                "XDG_STATE_HOME": str(directory / "state"),
            }
        )

    def _record(self, paths) -> CandidateRecord:
        deb_path = _build_deb(paths.download_dir)
        return CandidateRecord(
            version="26.810.52044",
            sha256=hashlib.sha256(deb_path.read_bytes()).hexdigest(),
            source_date_epoch=200,
            deb_path=deb_path,
            allowed_members=("usr/bin/chatgpt", "usr/lib/chatgpt/ChatGPT"),
        )

    def _recipe_fixture(self, directory: Path) -> Path:
        recipe = directory / "installed-recipe"
        scripts = recipe / "scripts"
        scripts.mkdir(parents=True)
        (recipe / "PKGBUILD").write_text(
            textwrap.dedent(
                """\
                pkgname=chatgpt-bin
                pkgver=0.0.1
                pkgrel=7
                arch=('x86_64')
                source=('chatgpt_amd64.deb::https://example.invalid/chatgpt_amd64.deb')
                sha256sums=('old-checksum')
                SOURCE_DATE_EPOCH=1
                package() {
                  :
                }
                """
            ),
            encoding="utf-8",
        )
        (recipe / ".SRCINFO").write_text("pkgbase = chatgpt-bin\n\tpkgver = 0.0.1\n", encoding="utf-8")
        (scripts / "update_source.py").write_text(
            textwrap.dedent(
                """\
                from __future__ import annotations

                import argparse
                import hashlib
                import json
                from pathlib import Path

                parser = argparse.ArgumentParser()
                parser.add_argument("--deb", type=Path, required=True)
                parser.add_argument("--repo-root", type=Path, required=True)
                parser.add_argument("--apply", action="store_true")
                parser.add_argument("--expected-version", required=True)
                parser.add_argument("--expected-sha256", required=True)
                args = parser.parse_args()
                if not args.apply:
                    raise SystemExit("missing --apply")
                actual = hashlib.sha256(args.deb.read_bytes()).hexdigest()
                if actual != args.expected_sha256:
                    raise SystemExit("unexpected sha256")
                pkgbuild = args.repo_root / "PKGBUILD"
                contents = pkgbuild.read_text(encoding="utf-8")
                contents = contents.replace("pkgver=0.0.1", f"pkgver={args.expected_version}")
                contents = contents.replace("pkgrel=7", "pkgrel=1")
                contents = contents.replace("sha256sums=('old-checksum')", f"sha256sums=('{args.expected_sha256}')")
                contents = contents.replace("SOURCE_DATE_EPOCH=1", "SOURCE_DATE_EPOCH=200")
                pkgbuild.write_text(contents, encoding="utf-8")
                (args.repo_root / ".SRCINFO").write_text(
                    f"pkgbase = chatgpt-bin\\n\\tpkgver = {args.expected_version}\\n",
                    encoding="utf-8",
                )
                print(json.dumps({"debian_version": args.expected_version, "sha256": actual, "architecture": "amd64"}))
                """
            ),
            encoding="utf-8",
        )
        (scripts / "review_source.py").write_text("# copied recipe fixture\n", encoding="utf-8")
        (scripts / "deb_payload.py").write_text("# copied recipe fixture\n", encoding="utf-8")
        return recipe

    def _tool_patch(self, missing: str | None = None):
        def which(name: str) -> str | None:
            if name == missing:
                return None
            return f"/usr/bin/{name}"

        return mock.patch.object(build_update.shutil, "which", which)

    def test_build_copies_recipe_updates_only_cached_workspace_and_runs_makepkg_without_install_flags(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            paths = self._paths(root)
            record = self._record(paths)
            recipe = self._recipe_fixture(root)
            original_recipe = (recipe / "PKGBUILD").read_text(encoding="utf-8")
            fake_runner = _FakeMakepkg()

            with self._tool_patch(), mock.patch.object(build_update, "INSTALLED_RECIPE_DIR", recipe):
                artifact = build_update.build_candidate(record, paths, fake_runner)

            manifest = json.loads(artifact.manifest_path.read_text(encoding="utf-8"))
            workspace = Path(manifest["workspace"])
            srcdest = Path(str(fake_runner.kwargs["env"]["SRCDEST"]))

            self.assertTrue(artifact.archive_path.is_file())
            self.assertEqual(artifact.sha256, hashlib.sha256(artifact.archive_path.read_bytes()).hexdigest())
            self.assertEqual(artifact.version, record.version)
            self.assertEqual((recipe / "PKGBUILD").read_text(encoding="utf-8"), original_recipe)
            self.assertEqual((workspace / "PKGBUILD").read_text(encoding="utf-8").count(record.sha256), 1)
            self.assertEqual(stat.S_IMODE(workspace.stat().st_mode), 0o700)
            self.assertEqual((srcdest / "chatgpt_amd64.deb").read_bytes(), record.deb_path.read_bytes())
            self.assertEqual(fake_runner.argv[:1], ["makepkg"])
            self.assertFalse(set(fake_runner.argv) & _FORBIDDEN_BUILD_FLAGS)
            self.assertFalse({command[0] for command in fake_runner.commands} & _FORBIDDEN_PRIVILEGED_COMMANDS)
            self.assertEqual(manifest["candidate"]["version"], record.version)
            self.assertEqual(manifest["candidate"]["sha256"], record.sha256)
            self.assertEqual(manifest["package"]["name"], "chatgpt-bin")
            self.assertEqual(manifest["package"]["architecture"], "x86_64")
            self.assertEqual(manifest["package"]["sha256"], artifact.sha256)
            self.assertEqual(manifest["allowed_package_paths"], list(record.allowed_members))

    def test_build_force_rebuilds_preexisting_archive_and_writes_report_for_refreshed_package(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            paths = self._paths(root)
            record = self._record(paths)
            recipe = self._recipe_fixture(root)
            archive = paths.package_dir / "chatgpt-bin-26.810.52044-1-x86_64.pkg.tar.zst"
            other_archive = paths.package_dir / "chatgpt-bin-26.810.52044-2-x86_64.pkg.tar.zst"
            archive.write_bytes(b"stale package from an earlier build")
            other_archive.write_bytes(b"unrelated cached package")
            fake_runner = _FakeMakepkg()

            with self._tool_patch(), mock.patch.object(build_update, "INSTALLED_RECIPE_DIR", recipe):
                artifact = build_update.build_candidate(record, paths, fake_runner)

            manifest = json.loads(artifact.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(fake_runner.argv, ["makepkg", "-f"])
            self.assertIs(fake_runner.archive_existed_during_makepkg, False)
            self.assertEqual(artifact.archive_path, archive)
            self.assertEqual(archive.read_bytes(), b"fake arch package archive")
            self.assertEqual(other_archive.read_bytes(), b"unrelated cached package")
            self.assertEqual(artifact.sha256, hashlib.sha256(archive.read_bytes()).hexdigest())
            self.assertEqual(manifest["package"]["archive_path"], str(archive))
            self.assertEqual(manifest["package"]["sha256"], artifact.sha256)
            self.assertIn(artifact.sha256, artifact.manifest_path.name)

    def test_build_preflight_requires_makepkg_fakeroot_python_and_bsdtar_before_running_makepkg(self) -> None:
        for missing in ("makepkg", "fakeroot", "python", "bsdtar"):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as temporary_directory:
                root = Path(temporary_directory)
                paths = self._paths(root)
                record = self._record(paths)
                recipe = self._recipe_fixture(root)
                fake_runner = _FakeMakepkg()

                with self._tool_patch(missing), mock.patch.object(build_update, "INSTALLED_RECIPE_DIR", recipe):
                    with self.assertRaisesRegex(build_update.BuildUpdateError, missing):
                        build_update.build_candidate(record, paths, fake_runner)

                self.assertEqual(fake_runner.commands, [])

    def test_build_rejects_missing_or_hash_mismatched_debian_candidate_before_makepkg(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            paths = self._paths(root)
            record = self._record(paths)
            recipe = self._recipe_fixture(root)

            for candidate_record in (record, replace(record, sha256="a" * 64)):
                with self.subTest(sha256=candidate_record.sha256):
                    if candidate_record is record:
                        record.deb_path.unlink()
                    else:
                        record.deb_path.write_bytes(b"not the reviewed candidate")
                    fake_runner = _FakeMakepkg()
                    with self._tool_patch(), mock.patch.object(build_update, "INSTALLED_RECIPE_DIR", recipe):
                        with self.assertRaisesRegex(ValueError, "candidate Debian"):
                            build_update.build_candidate(candidate_record, paths, fake_runner)
                    self.assertEqual(fake_runner.commands, [])

    def test_build_rejects_recipe_workspace_outside_cache_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            paths = self._paths(root)
            record = self._record(paths)
            recipe = self._recipe_fixture(root)
            outside_paths = replace(paths, recipe_dir=root / "outside-recipe-cache")
            fake_runner = _FakeMakepkg()

            with self._tool_patch(), mock.patch.object(build_update, "INSTALLED_RECIPE_DIR", recipe):
                with self.assertRaisesRegex(ValueError, "recipe workspace"):
                    build_update.build_candidate(record, outside_paths, fake_runner)

            self.assertEqual(fake_runner.commands, [])

    def test_makepkg_failure_writes_no_build_report_and_never_requests_install(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            paths = self._paths(root)
            record = self._record(paths)
            recipe = self._recipe_fixture(root)
            fake_runner = _FakeMakepkg(returncode=7, create_archive=False)

            with self._tool_patch(), mock.patch.object(build_update, "INSTALLED_RECIPE_DIR", recipe):
                with self.assertRaisesRegex(build_update.BuildUpdateError, "makepkg failed"):
                    build_update.build_candidate(record, paths, fake_runner)

            self.assertEqual(fake_runner.argv[:1], ["makepkg"])
            self.assertFalse(set(fake_runner.argv) & _FORBIDDEN_BUILD_FLAGS)
            self.assertFalse({command[0] for command in fake_runner.commands} & _FORBIDDEN_PRIVILEGED_COMMANDS)
            self.assertFalse(list(paths.package_dir.glob("*.json")))

    def test_makepkg_failure_writes_no_report_even_if_makepkg_left_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            paths = self._paths(root)
            record = self._record(paths)
            recipe = self._recipe_fixture(root)
            fake_runner = _FakeMakepkg(returncode=7, create_archive=True, create_archive_on_failure=True)

            with self._tool_patch(), mock.patch.object(build_update, "INSTALLED_RECIPE_DIR", recipe):
                with self.assertRaisesRegex(build_update.BuildUpdateError, "makepkg failed"):
                    build_update.build_candidate(record, paths, fake_runner)

            self.assertEqual(fake_runner.argv, ["makepkg", "-f"])
            self.assertTrue(list(paths.package_dir.glob("*.pkg.tar*")))
            self.assertFalse(list(paths.package_dir.glob("*.build.json")))

    def test_makepkg_success_that_only_utimes_preexisting_archive_writes_no_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            paths = self._paths(root)
            record = self._record(paths)
            recipe = self._recipe_fixture(root)
            archive = paths.package_dir / "chatgpt-bin-26.810.52044-1-x86_64.pkg.tar.zst"
            archive.write_bytes(b"stale package from an earlier build")
            fake_runner = _UtimeOnlyMakepkg(archive)

            with self._tool_patch(), mock.patch.object(build_update, "INSTALLED_RECIPE_DIR", recipe):
                with self.assertRaisesRegex(build_update.BuildUpdateError, "freshly built"):
                    build_update.build_candidate(record, paths, fake_runner)

            self.assertEqual(fake_runner.argv, ["makepkg", "-f"])
            self.assertIs(fake_runner.archive_existed_during_makepkg, False)
            self.assertFalse(archive.exists())
            self.assertFalse(list(paths.package_dir.glob("*.build.json")))

    def test_public_launcher_supports_check_status_build_and_install_commands(self) -> None:
        launcher = Path(__file__).resolve().parents[1] / "updater" / "chatgpt-bin-update"

        for command in ("check", "status", "build", "install", "workflow"):
            with self.subTest(command=command):
                result = subprocess.run(
                    [str(launcher), command, "--help"],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
