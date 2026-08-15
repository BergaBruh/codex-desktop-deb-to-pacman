from __future__ import annotations

import hashlib
import os
from pathlib import Path
import socket
import tempfile
import textwrap
import unittest
from contextlib import contextmanager
from unittest import mock
import urllib.request
import http.client

from scripts.update_source import update_source
from tests.test_review_source import _build_deb


_SRCINFO = "pkgbase = chatgpt-bin\n\tpkgver = 26.810.52044\n"


class UpdateSourceTests(unittest.TestCase):
    def _repository_fixture(self, directory: Path) -> tuple[Path, Path, Path, Path]:
        repository = directory / "repository"
        fake_bin = directory / "bin"
        repository.mkdir()
        fake_bin.mkdir()
        pkgbuild = repository / "PKGBUILD"
        srcinfo = repository / ".SRCINFO"
        calls = directory / "makepkg-calls.txt"
        pkgbuild.write_text(
            textwrap.dedent(
                """\
                pkgname=chatgpt-bin
                pkgver=0.0.1
                pkgrel=7
                source=('https://example.invalid/chatgpt_amd64.deb')
                sha256sums=('old-checksum')
                SOURCE_DATE_EPOCH=1
                """
            ),
            encoding="utf-8",
        )
        srcinfo.write_text("stale srcinfo\n", encoding="utf-8")
        fake_makepkg = fake_bin / "makepkg"
        fake_makepkg.write_text(
            textwrap.dedent(
                f"""\
                #!/usr/bin/env python3
                import os
                import sys
                if sys.argv[1:] != ['--printsrcinfo']:
                    raise SystemExit(9)
                with open(os.environ['MAKEPKG_CALLS'], 'a', encoding='utf-8') as calls:
                    calls.write(' '.join(sys.argv[1:]) + '\\n')
                print({ _SRCINFO!r }, end='')
                """
            ),
            encoding="utf-8",
        )
        fake_makepkg.chmod(0o755)
        return repository, pkgbuild, srcinfo, calls

    @contextmanager
    def _network_forbidden(self):
        forbidden = AssertionError("network is forbidden")
        urlopen = mock.Mock(side_effect=forbidden)
        create_connection = mock.Mock(side_effect=forbidden)
        connect = mock.Mock(side_effect=forbidden)
        with (
            mock.patch.object(urllib.request, "urlopen", urlopen),
            mock.patch.object(socket, "create_connection", create_connection),
            mock.patch.object(http.client.HTTPConnection, "connect", connect),
        ):
            yield
        self.assertFalse(urlopen.called)
        self.assertFalse(create_connection.called)
        self.assertFalse(connect.called)

    def test_read_only_review_does_not_change_files_or_run_makepkg(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            deb_path = _build_deb(directory)
            repository, pkgbuild, srcinfo, calls = self._repository_fixture(directory)
            before_pkgbuild = pkgbuild.read_bytes()
            before_srcinfo = srcinfo.read_bytes()
            with self._network_forbidden():
                result = update_source(deb_path, repository)

            self.assertEqual(result["debian_version"], "26.810.52044")
            self.assertEqual(pkgbuild.read_bytes(), before_pkgbuild)
            self.assertEqual(srcinfo.read_bytes(), before_srcinfo)
            self.assertFalse(calls.exists())

    def test_apply_requires_both_expected_values_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            deb_path = _build_deb(directory)
            repository, pkgbuild, srcinfo, _calls = self._repository_fixture(directory)
            before_pkgbuild = pkgbuild.read_bytes()
            before_srcinfo = srcinfo.read_bytes()

            with self._network_forbidden():
                for version, checksum in ((None, None), ("26.810.52044", None), (None, "a" * 64)):
                    with self.assertRaisesRegex(ValueError, "expected"):
                        update_source(
                            deb_path,
                            repository,
                            apply=True,
                            expected_version=version,
                            expected_sha256=checksum,
                        )
                    self.assertEqual(pkgbuild.read_bytes(), before_pkgbuild)
                    self.assertEqual(srcinfo.read_bytes(), before_srcinfo)

    def test_apply_rejects_mismatched_expected_values_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            deb_path = _build_deb(directory)
            repository, pkgbuild, srcinfo, _calls = self._repository_fixture(directory)
            before_pkgbuild = pkgbuild.read_bytes()
            before_srcinfo = srcinfo.read_bytes()

            with self._network_forbidden():
                with self.assertRaisesRegex(ValueError, "version"):
                    update_source(
                        deb_path,
                        repository,
                        apply=True,
                        expected_version="99.0.0",
                        expected_sha256=hashlib.sha256(deb_path.read_bytes()).hexdigest(),
                    )
                with self.assertRaisesRegex(ValueError, "SHA-256"):
                    update_source(
                        deb_path,
                        repository,
                        apply=True,
                        expected_version="26.810.52044",
                        expected_sha256="f" * 64,
                    )
            self.assertEqual(pkgbuild.read_bytes(), before_pkgbuild)
            self.assertEqual(srcinfo.read_bytes(), before_srcinfo)

    def test_guarded_apply_updates_only_reviewed_metadata_and_srcinfo(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            deb_path = _build_deb(directory)
            repository, pkgbuild, srcinfo, calls = self._repository_fixture(directory)
            checksum = hashlib.sha256(deb_path.read_bytes()).hexdigest()
            expected_source_line = "source=('https://example.invalid/chatgpt_amd64.deb')"
            previous = pkgbuild.read_text(encoding="utf-8")
            environment = {
                "PATH": f"{directory / 'bin'}:{os.environ['PATH']}",
                "MAKEPKG_CALLS": str(calls),
            }

            with self._network_forbidden(), mock.patch.dict(os.environ, environment, clear=False):
                result = update_source(
                    deb_path,
                    repository,
                    apply=True,
                    expected_version="26.810.52044",
                    expected_sha256=checksum,
                )

            updated = pkgbuild.read_text(encoding="utf-8")
            self.assertIn("pkgver=26.810.52044", updated)
            self.assertIn("pkgrel=1", updated)
            self.assertIn(f"sha256sums=('{checksum}')", updated)
            self.assertIn("SOURCE_DATE_EPOCH=200", updated)
            self.assertIn(expected_source_line, updated)
            self.assertEqual(srcinfo.read_text(encoding="utf-8"), _SRCINFO)
            self.assertEqual(calls.read_text(encoding="utf-8"), "--printsrcinfo\n")
            self.assertEqual(result["source_date_epoch"], 200)
            self.assertNotEqual(updated, previous)

    def test_makepkg_failure_restores_original_metadata_and_srcinfo(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            deb_path = _build_deb(directory)
            repository, pkgbuild, srcinfo, _calls = self._repository_fixture(directory)
            fake_makepkg = directory / "bin" / "makepkg"
            fake_makepkg.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            fake_makepkg.chmod(0o755)
            before_pkgbuild = pkgbuild.read_bytes()
            before_srcinfo = srcinfo.read_bytes()
            environment = {"PATH": f"{directory / 'bin'}:{os.environ['PATH']}"}

            with self._network_forbidden(), mock.patch.dict(os.environ, environment, clear=False):
                with self.assertRaisesRegex(ValueError, "makepkg"):
                    update_source(
                        deb_path,
                        repository,
                        apply=True,
                        expected_version="26.810.52044",
                        expected_sha256=hashlib.sha256(deb_path.read_bytes()).hexdigest(),
                    )

            self.assertEqual(pkgbuild.read_bytes(), before_pkgbuild)
            self.assertEqual(srcinfo.read_bytes(), before_srcinfo)


if __name__ == "__main__":
    unittest.main()
