from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

from scripts.review_source import read_deb_control, review_source


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _write_tar(path: Path, members: list[tuple[str, str, bytes | str, int]]) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for name, kind, value, mtime in members:
            member = tarfile.TarInfo(name)
            member.mtime = mtime
            if kind == "file":
                assert isinstance(value, bytes)
                member.mode = 0o755
                member.size = len(value)
                archive.addfile(member, io.BytesIO(value))
            elif kind == "symlink":
                assert isinstance(value, str)
                member.type = tarfile.SYMTYPE
                member.mode = 0o777
                member.linkname = value
                archive.addfile(member)
            else:
                raise ValueError(f"unsupported fixture member kind: {kind}")


def _build_deb(
    directory: Path,
    *,
    package: str = "chatgpt",
    version: str = "26.810.52044",
    architecture: str = "amd64",
    include_control: bool = True,
    duplicate_control: bool = False,
) -> Path:
    debian_binary = directory / "debian-binary"
    data_archive = directory / "data.tar.gz"
    control_archive = directory / "control.tar.gz"
    debian_binary.write_text("2.0\n", encoding="ascii")
    _write_tar(
        data_archive,
        [
            ("usr/lib/chatgpt/ChatGPT", "file", b"desktop", 100),
            ("usr/bin/chatgpt", "symlink", "../lib/chatgpt/codex-launcher", 200),
            ("usr/share/lintian/overrides/chatgpt", "file", b"lint", 500),
        ],
    )
    members = [debian_binary, data_archive]
    if include_control:
        control_text = (
            f"Package: {package}\n"
            f"Version: {version}\n"
            f"Architecture: {architecture}\n"
            "Description: ChatGPT desktop application\n"
            " continuation line\n"
        ).encode("utf-8")
        _write_tar(control_archive, [("control", "file", control_text, 1)])
        members.insert(1, control_archive)
        if duplicate_control:
            duplicate = directory / "control.tar.xz"
            duplicate.write_bytes(control_archive.read_bytes())
            members.insert(2, duplicate)
    deb_path = directory / "chatgpt_amd64.deb"
    subprocess.run(["ar", "rcs", str(deb_path), *map(str, members)], check=True)
    return deb_path


class ReviewSourceTests(unittest.TestCase):
    def test_review_reports_control_hash_sorted_manifest_and_accepted_epoch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            deb_path = _build_deb(Path(temporary_directory))

            result = review_source(deb_path)

            self.assertEqual(result["debian_version"], "26.810.52044")
            self.assertEqual(result["architecture"], "amd64")
            self.assertEqual(result["sha256"], hashlib.sha256(deb_path.read_bytes()).hexdigest())
            self.assertEqual(
                result["allowed_members"],
                ["usr/bin/chatgpt", "usr/lib/chatgpt/ChatGPT"],
            )
            self.assertEqual(result["source_date_epoch"], 200)

    def test_read_deb_control_parses_continuations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            deb_path = _build_deb(Path(temporary_directory))

            control = read_deb_control(deb_path)

            self.assertEqual(control["Package"], "chatgpt")
            self.assertEqual(control["Description"], "ChatGPT desktop application\ncontinuation line")

    def test_review_rejects_wrong_architecture(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            deb_path = _build_deb(Path(temporary_directory), architecture="arm64")

            with self.assertRaisesRegex(ValueError, "Architecture"):
                review_source(deb_path)

    def test_review_rejects_wrong_package_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            deb_path = _build_deb(Path(temporary_directory), package="not-chatgpt")

            with self.assertRaisesRegex(ValueError, "Package"):
                review_source(deb_path)

    def test_control_reader_rejects_missing_control_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            deb_path = _build_deb(Path(temporary_directory), include_control=False)

            with self.assertRaisesRegex(ValueError, "control.tar"):
                read_deb_control(deb_path)

    def test_control_reader_rejects_duplicate_control_archives(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            deb_path = _build_deb(Path(temporary_directory), duplicate_control=True)

            with self.assertRaisesRegex(ValueError, "exactly one control.tar"):
                read_deb_control(deb_path)

    def test_cli_emits_sorted_json_without_writing_next_to_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture_directory = Path(temporary_directory)
            deb_path = _build_deb(fixture_directory)
            before = sorted(path.name for path in fixture_directory.iterdir())

            result = subprocess.run(
                [
                    sys.executable,
                    str(REPOSITORY_ROOT / "scripts" / "review_source.py"),
                    "--deb",
                    str(deb_path),
                    "--format",
                    "json",
                ],
                check=True,
                capture_output=True,
                text=True,
            )

            self.assertEqual(json.loads(result.stdout)["debian_version"], "26.810.52044")
            self.assertEqual(before, sorted(path.name for path in fixture_directory.iterdir()))


if __name__ == "__main__":
    unittest.main()
