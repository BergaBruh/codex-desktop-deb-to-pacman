from __future__ import annotations

import io
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

from scripts.deb_payload import stage_payload
from tests.test_review_source import _write_tar


def _write_data_tar(path: Path, members: list[dict[str, object]]) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for specification in members:
            member = tarfile.TarInfo(str(specification["name"]))
            member.mode = int(specification.get("mode", 0o755))
            member.mtime = int(specification.get("mtime", 100))
            member.pax_headers = dict(specification.get("pax_headers", {}))
            kind = specification["kind"]
            if kind == "file":
                contents = specification.get("contents", b"payload")
                assert isinstance(contents, bytes)
                member.size = len(contents)
                archive.addfile(member, io.BytesIO(contents))
            elif kind == "directory":
                member.type = tarfile.DIRTYPE
                archive.addfile(member)
            elif kind == "symlink":
                member.type = tarfile.SYMTYPE
                member.linkname = str(specification["target"])
                archive.addfile(member)
            elif kind == "hardlink":
                member.type = tarfile.LNKTYPE
                member.linkname = str(specification["target"])
                archive.addfile(member)
            elif kind == "fifo":
                member.type = tarfile.FIFOTYPE
                archive.addfile(member)
            elif kind == "character-device":
                member.type = tarfile.CHRTYPE
                member.devmajor = 1
                member.devminor = 3
                archive.addfile(member)
            else:
                raise ValueError(f"unsupported fixture kind: {kind}")


def _build_deb(directory: Path, data_members: list[dict[str, object]]) -> Path:
    debian_binary = directory / "debian-binary"
    control_archive = directory / "control.tar.gz"
    data_archive = directory / "data.tar.gz"
    debian_binary.write_text("2.0\n", encoding="ascii")
    _write_tar(
        control_archive,
        [
            (
                "control",
                "file",
                b"Package: chatgpt\nVersion: 26.810.52044\nArchitecture: amd64\n",
                1,
            ),
            ("control-only", "file", b"must not be staged", 1),
        ],
    )
    _write_data_tar(data_archive, data_members)
    deb_path = directory / "chatgpt_amd64.deb"
    subprocess.run(
        ["ar", "rcs", str(deb_path), str(debian_binary), str(control_archive), str(data_archive)],
        check=True,
    )
    return deb_path


def _valid_members() -> list[dict[str, object]]:
    return [
        {"name": "./", "kind": "directory", "mode": 0o755, "mtime": 9},
        {"name": "usr", "kind": "directory", "mode": 0o755, "mtime": 10},
        {"name": "usr/bin", "kind": "directory", "mode": 0o755, "mtime": 11},
        {"name": "usr/lib", "kind": "directory", "mode": 0o755, "mtime": 12},
        {"name": "usr/lib/chatgpt", "kind": "directory", "mode": 0o750, "mtime": 13},
        {"name": "usr/share", "kind": "directory", "mode": 0o755, "mtime": 14},
        {"name": "usr/share/lintian", "kind": "directory", "mode": 0o755, "mtime": 15},
        {"name": "usr/share/lintian/overrides", "kind": "directory", "mode": 0o755, "mtime": 16},
        {
            "name": "usr/lib/chatgpt/ChatGPT",
            "kind": "file",
            "contents": b"desktop-binary",
            "mode": 0o751,
            "mtime": 200,
        },
        {
            "name": "usr/bin/chatgpt",
            "kind": "symlink",
            "target": "../lib/chatgpt/codex-launcher",
            "mode": 0o777,
            "mtime": 201,
        },
        {
            "name": "usr/share/lintian/overrides/chatgpt",
            "kind": "file",
            "contents": b"discarded Debian lint metadata",
            "mode": 0o644,
            "mtime": 202,
        },
    ]


class DebPayloadStagingTests(unittest.TestCase):
    def _assert_rejected_without_destination(self, members: list[dict[str, object]]) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            deb_path = _build_deb(directory, members)
            destination = directory / "pkgdir"

            with self.assertRaises(ValueError):
                stage_payload(deb_path, destination)

            self.assertFalse(destination.exists())

    def test_stages_only_valid_data_members_with_metadata_and_launcher_link(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            deb_path = _build_deb(directory, _valid_members())
            destination = directory / "pkgdir"

            members = stage_payload(deb_path, destination)

            binary = destination / "usr/lib/chatgpt/ChatGPT"
            self.assertEqual(binary.read_bytes(), b"desktop-binary")
            self.assertEqual(binary.stat().st_mode & 0o777, 0o751)
            self.assertEqual(int(binary.stat().st_mtime), 200)
            self.assertEqual((destination / "usr/lib/chatgpt").stat().st_mode & 0o777, 0o750)
            self.assertEqual(os.readlink(destination / "usr/bin/chatgpt"), "../lib/chatgpt/codex-launcher")
            self.assertFalse((destination / "usr/share/lintian/overrides/chatgpt").exists())
            self.assertFalse((destination / "usr/share/lintian").exists())
            self.assertFalse((destination / "control-only").exists())
            self.assertNotIn("usr/share/lintian/overrides/chatgpt", [member.path for member in members])

    def test_rejects_absolute_member_without_destination(self) -> None:
        self._assert_rejected_without_destination([{"name": "/usr/lib/chatgpt/evil", "kind": "file"}])

    def test_rejects_traversal_member_without_destination(self) -> None:
        self._assert_rejected_without_destination([{"name": "../usr/lib/chatgpt/evil", "kind": "file"}])

    def test_rejects_unallowlisted_member_without_destination(self) -> None:
        self._assert_rejected_without_destination([{"name": "usr/share/unsupported", "kind": "file"}])

    def test_rejects_duplicate_member_without_destination(self) -> None:
        self._assert_rejected_without_destination(
            [
                {"name": "usr/lib/chatgpt/ChatGPT", "kind": "file"},
                {"name": "usr/lib/chatgpt/ChatGPT", "kind": "file"},
            ]
        )

    def test_rejects_setuid_member_without_destination(self) -> None:
        self._assert_rejected_without_destination(
            [{"name": "usr/lib/chatgpt/ChatGPT", "kind": "file", "mode": 0o4755}]
        )

    def test_rejects_setgid_member_without_destination(self) -> None:
        self._assert_rejected_without_destination(
            [{"name": "usr/lib/chatgpt/ChatGPT", "kind": "file", "mode": 0o2755}]
        )

    def test_rejects_file_capability_without_destination(self) -> None:
        self._assert_rejected_without_destination(
            [
                {
                    "name": "usr/lib/chatgpt/ChatGPT",
                    "kind": "file",
                    "pax_headers": {"SCHILY.xattr.security.capability": "cap_net_raw=ep"},
                }
            ]
        )

    def test_rejects_fifo_without_destination(self) -> None:
        self._assert_rejected_without_destination([{"name": "usr/lib/chatgpt/fifo", "kind": "fifo"}])

    def test_rejects_device_without_destination(self) -> None:
        self._assert_rejected_without_destination(
            [{"name": "usr/lib/chatgpt/device", "kind": "character-device"}]
        )

    def test_rejects_hardlink_without_destination(self) -> None:
        self._assert_rejected_without_destination(
            [{"name": "usr/lib/chatgpt/hardlink", "kind": "hardlink", "target": "ChatGPT"}]
        )

    def test_rejects_escaping_symlink_without_destination(self) -> None:
        self._assert_rejected_without_destination(
            [{"name": "usr/lib/chatgpt/escape", "kind": "symlink", "target": "../../../etc/passwd"}]
        )


if __name__ == "__main__":
    unittest.main()
