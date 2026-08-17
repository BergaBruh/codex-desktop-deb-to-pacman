from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest import mock

from scripts.review_source import review_source
from tests.test_review_source import _build_deb
from updater.common import (
    CandidateRecord,
    SourceValidator,
    cache_lock,
    cache_paths,
    compare_versions,
    download_to_cache,
    read_record,
    read_source_validator,
    write_record,
    write_source_validator,
)


class _Response:
    def __init__(self, chunks: list[bytes], error: Exception | None = None) -> None:
        self._chunks = iter(chunks)
        self._error = error

    def read(self, _size: int) -> bytes:
        if self._error is not None:
            error = self._error
            self._error = None
            raise error
        return next(self._chunks, b"")

    def close(self) -> None:
        return None


class UpdateCommonTests(unittest.TestCase):
    def _paths(self, directory: Path):
        return cache_paths(
            {
                "XDG_CACHE_HOME": str(directory / "cache"),
                "XDG_STATE_HOME": str(directory / "state"),
            }
        )

    def _record(self, paths, *, sha256: str | None = None, deb_path: Path | None = None) -> CandidateRecord:
        candidate = deb_path or paths.download_file
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_bytes(b"reviewed Debian candidate")
        return CandidateRecord(
            version="26.810.52044",
            sha256=sha256 or hashlib.sha256(candidate.read_bytes()).hexdigest(),
            source_date_epoch=1786770000,
            deb_path=candidate,
            allowed_members=("usr/bin/chatgpt", "usr/lib/chatgpt/codex-launcher"),
        )

    @staticmethod
    def _matching_reviewer(candidate: Path) -> dict[str, str]:
        return {"sha256": hashlib.sha256(candidate.read_bytes()).hexdigest()}

    def test_cache_paths_create_private_xdg_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            cache = temporary_path / "cache"
            state = temporary_path / "state"

            paths = cache_paths({"XDG_CACHE_HOME": str(cache), "XDG_STATE_HOME": str(state)})

            self.assertEqual(paths.cache_dir, cache / "chatgpt-bin")
            self.assertEqual(paths.state_file, state / "chatgpt-bin" / "candidate.json")
            for directory in (
                paths.cache_dir,
                paths.download_dir,
                paths.recipe_dir,
                paths.work_dir,
                paths.package_dir,
                paths.state_dir,
            ):
                self.assertTrue(directory.is_dir())
                self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)

    def test_cache_paths_fall_back_to_home_xdg_locations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory) / "home"
            home.mkdir()

            paths = cache_paths({"HOME": str(home)})

            self.assertEqual(paths.cache_dir, home / ".cache" / "chatgpt-bin")
            self.assertEqual(paths.state_dir, home / ".local" / "state" / "chatgpt-bin")

    def test_cache_paths_ignore_empty_and_relative_xdg_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory) / "home"
            home.mkdir()

            paths = cache_paths(
                {
                    "HOME": str(home),
                    "XDG_CACHE_HOME": "relative-cache",
                    "XDG_STATE_HOME": "",
                }
            )

            self.assertEqual(paths.cache_dir, home / ".cache" / "chatgpt-bin")
            self.assertEqual(paths.state_dir, home / ".local" / "state" / "chatgpt-bin")

    def test_cache_paths_rejects_a_symlinked_cache_root_without_chmodding_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            cache_home = temporary_path / "cache"
            state_home = temporary_path / "state"
            cache_home.mkdir()
            state_home.mkdir()
            target = temporary_path / "cache-target"
            target.mkdir(mode=0o755)
            (cache_home / "chatgpt-bin").symlink_to(target, target_is_directory=True)

            with self.assertRaises(ValueError):
                cache_paths({"XDG_CACHE_HOME": str(cache_home), "XDG_STATE_HOME": str(state_home)})

            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)

    def test_cache_paths_rejects_a_symlinked_state_root_without_chmodding_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            cache_home = temporary_path / "cache"
            state_home = temporary_path / "state"
            cache_home.mkdir()
            state_home.mkdir()
            target = temporary_path / "state-target"
            target.mkdir(mode=0o755)
            (state_home / "chatgpt-bin").symlink_to(target, target_is_directory=True)

            with self.assertRaises(ValueError):
                cache_paths({"XDG_CACHE_HOME": str(cache_home), "XDG_STATE_HOME": str(state_home)})

            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)

    def test_missing_record_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            self.assertIsNone(read_record(self._paths(Path(temporary_directory))))

    def test_write_then_read_record_preserves_reviewed_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            paths = self._paths(Path(temporary_directory))
            record = self._record(paths)

            write_record(paths, record)

            self.assertEqual(read_record(paths), record)
            stored = json.loads(paths.state_file.read_text(encoding="utf-8"))
            self.assertEqual(stored["deb_path"], str(paths.download_file.resolve()))
            self.assertEqual(stored["sha256"], record.sha256)
            self.assertEqual(stored["allowed_members"], list(record.allowed_members))
            self.assertFalse(list(paths.state_dir.glob("candidate.json.*.tmp")))

    def test_source_validator_state_is_keyed_by_url_and_separate_from_candidate_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            paths = self._paths(Path(temporary_directory))
            record = self._record(paths)
            write_record(paths, record)

            first = SourceValidator(
                url="https://example.invalid/chatgpt_amd64.deb",
                etag='"first"',
                last_modified="Mon, 17 Aug 2026 00:00:00 GMT",
            )
            second = SourceValidator(
                url="https://mirror.example.invalid/chatgpt_amd64.deb",
                last_modified="Tue, 18 Aug 2026 00:00:00 GMT",
            )

            write_source_validator(paths, first)
            write_source_validator(paths, second)

            self.assertEqual(read_source_validator(paths, first.url), first)
            self.assertEqual(read_source_validator(paths, second.url), second)
            self.assertIsNone(read_source_validator(paths, "https://missing.example.invalid/chatgpt_amd64.deb"))
            self.assertEqual(read_record(paths), record)

    def test_malformed_source_validator_state_is_ignored_for_safe_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            paths = self._paths(Path(temporary_directory))
            paths.validator_file.write_text("{not json", encoding="utf-8")

            self.assertIsNone(read_source_validator(paths, "https://example.invalid/chatgpt_amd64.deb"))

    def test_record_rejects_unsafe_deb_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            paths = self._paths(Path(temporary_directory))
            outside = Path(temporary_directory) / "outside.deb"
            outside.write_bytes(b"outside")
            paths.download_file.write_bytes(b"candidate")
            symlink = paths.download_dir / "linked.deb"
            symlink.symlink_to(outside)

            for unsafe_path in (Path("downloads/chatgpt.deb"), outside, paths.download_dir, symlink):
                with self.subTest(path=unsafe_path):
                    state = {
                        "version": "26.810.52044",
                        "sha256": "a" * 64,
                        "source_date_epoch": 1786770000,
                        "deb_path": str(unsafe_path),
                        "allowed_members": ["usr/bin/chatgpt"],
                    }
                    paths.state_file.write_text(json.dumps(state), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        read_record(paths)

    def test_write_record_rejects_an_invalid_sha256(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            paths = self._paths(Path(temporary_directory))

            with self.assertRaises(ValueError):
                write_record(paths, self._record(paths, sha256="UPPERCASE"))

            self.assertFalse(paths.state_file.exists())

    def test_write_record_rejects_hostile_allowed_member_components(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            paths = self._paths(Path(temporary_directory))
            record = self._record(paths)

            for member in (
                "../etc/shadow",
                "usr/../etc/shadow",
                "usr/./bin/chatgpt",
                "/usr/bin/chatgpt",
                "usr//bin/chatgpt",
                "usr/bin/chatgpt/",
                "usr/\x00chatgpt",
                "usr/\x1fchatgpt",
            ):
                with self.subTest(member=repr(member)):
                    with self.assertRaises(ValueError):
                        write_record(paths, replace(record, allowed_members=(member,)))

            self.assertFalse(paths.state_file.exists())

    def test_record_rejects_a_non_list_allowed_member_value(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            paths = self._paths(Path(temporary_directory))
            record = self._record(paths)
            paths.state_file.write_text(
                json.dumps(
                    {
                        "version": record.version,
                        "sha256": record.sha256,
                        "source_date_epoch": record.source_date_epoch,
                        "deb_path": str(record.deb_path.resolve()),
                        "allowed_members": "member",
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(ValueError):
                read_record(paths)

    def test_download_rejects_a_missing_reviewer_before_reading_network(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            paths = self._paths(Path(temporary_directory))
            requested_urls: list[str] = []

            def opener(url: str) -> _Response:
                requested_urls.append(url)
                return _Response([b"raw network bytes"])

            with self.assertRaises(ValueError):
                download_to_cache(
                    "https://example.invalid/chatgpt.deb",
                    paths.download_file,
                    opener,
                    reviewer=None,
                )

            self.assertFalse(paths.download_file.exists())
            self.assertEqual(requested_urls, [])
            self.assertFalse(list(paths.download_dir.glob("*.tmp")))

    def test_failed_download_keeps_the_previous_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            paths = self._paths(Path(temporary_directory))
            paths.download_file.write_bytes(b"previous reviewed candidate")

            with self.assertRaises(OSError):
                download_to_cache(
                    "https://example.invalid/chatgpt.deb",
                    paths.download_file,
                    lambda _url: _Response([b"partial"], OSError("network interrupted")),
                    reviewer=self._matching_reviewer,
                )

            self.assertEqual(paths.download_file.read_bytes(), b"previous reviewed candidate")
            self.assertFalse(list(paths.download_dir.glob("*.tmp")))

    def test_rejected_download_review_keeps_the_previous_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            paths = self._paths(Path(temporary_directory))
            paths.download_file.write_bytes(b"previous reviewed candidate")

            def reject_review(candidate: Path) -> None:
                self.assertTrue(candidate.is_file())
                self.assertEqual(candidate.read_bytes(), b"unreviewed candidate")
                raise ValueError("candidate Debian payload is not allowlisted")

            with self.assertRaisesRegex(ValueError, "not allowlisted"):
                download_to_cache(
                    "https://example.invalid/chatgpt.deb",
                    paths.download_file,
                    lambda _url: _Response([b"unreviewed candidate"]),
                    reviewer=reject_review,
                )

            self.assertEqual(paths.download_file.read_bytes(), b"previous reviewed candidate")
            self.assertFalse(list(paths.download_dir.glob("*.tmp")))

    def test_download_rejects_a_download_directory_swapped_to_a_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            paths = self._paths(temporary_path)
            target = temporary_path / "download-target"
            target.mkdir(mode=0o755)
            paths.download_dir.rmdir()
            paths.download_dir.symlink_to(target, target_is_directory=True)

            with self.assertRaises(ValueError):
                download_to_cache(
                    "https://example.invalid/chatgpt.deb",
                    paths.download_file,
                    lambda _url: _Response([b"candidate"]),
                    reviewer=self._matching_reviewer,
                )

            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)
            self.assertFalse(any(target.iterdir()))

    def test_write_rejects_a_state_directory_swapped_to_a_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            paths = self._paths(temporary_path)
            record = self._record(paths)
            target = temporary_path / "state-target"
            target.mkdir(mode=0o755)
            paths.state_dir.rmdir()
            paths.state_dir.symlink_to(target, target_is_directory=True)

            with self.assertRaises(ValueError):
                write_record(paths, record)

            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)
            self.assertFalse(any(target.iterdir()))

    def test_download_rejects_a_swapped_cache_root_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            paths = self._paths(temporary_path)
            target = temporary_path / "cache-target"
            target.mkdir(mode=0o755)
            redirected_downloads = target / "downloads"
            redirected_downloads.mkdir(mode=0o755)
            for directory in (paths.download_dir, paths.recipe_dir, paths.work_dir, paths.package_dir):
                directory.rmdir()
            paths.cache_dir.rmdir()
            paths.cache_dir.symlink_to(target, target_is_directory=True)

            with self.assertRaises(ValueError):
                download_to_cache(
                    "https://example.invalid/chatgpt.deb",
                    paths.download_file,
                    lambda _url: _Response([b"candidate"]),
                    reviewer=self._matching_reviewer,
                )

            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(redirected_downloads.stat().st_mode), 0o755)
            self.assertEqual(list(target.iterdir()), [redirected_downloads])

    def test_write_rejects_a_swapped_state_home_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            paths = self._paths(temporary_path)
            record = self._record(paths)
            target = temporary_path / "state-target"
            target.mkdir(mode=0o755)
            redirected_state = target / "chatgpt-bin"
            redirected_state.mkdir(mode=0o755)
            paths.state_dir.rmdir()
            paths.state_dir.parent.rmdir()
            paths.state_dir.parent.symlink_to(target, target_is_directory=True)

            with self.assertRaises(ValueError):
                write_record(paths, record)

            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(redirected_state.stat().st_mode), 0o755)
            self.assertEqual(list(target.iterdir()), [redirected_state])

    def test_record_write_rejects_a_download_directory_swapped_to_a_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            paths = self._paths(temporary_path)
            record = self._record(paths)
            target = temporary_path / "download-target"
            target.mkdir(mode=0o755)
            paths.download_file.unlink()
            paths.download_dir.rmdir()
            paths.download_dir.symlink_to(target, target_is_directory=True)
            (target / paths.download_file.name).write_bytes(b"reviewed Debian candidate")

            with self.assertRaises(ValueError):
                write_record(paths, record)

            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)
            self.assertEqual((target / paths.download_file.name).read_bytes(), b"reviewed Debian candidate")

    def test_reviewed_download_rejects_a_mismatched_report_sha256(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            paths = self._paths(Path(temporary_directory))
            paths.download_file.write_bytes(b"previous reviewed candidate")

            with self.assertRaises(ValueError):
                download_to_cache(
                    "https://example.invalid/chatgpt.deb",
                    paths.download_file,
                    lambda _url: _Response([b"reviewed candidate"]),
                    reviewer=lambda _candidate: {"sha256": "0" * 64},
                )

            self.assertEqual(paths.download_file.read_bytes(), b"previous reviewed candidate")
            self.assertFalse(list(paths.download_dir.glob("*.tmp")))

    def test_reviewed_download_rejects_a_temp_name_swap_during_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            paths = self._paths(Path(temporary_directory))
            original = b"reviewed candidate"
            paths.download_file.write_bytes(b"previous reviewed candidate")
            swapped_name: Path | None = None

            def reviewer(candidate: Path) -> dict[str, str]:
                nonlocal swapped_name
                self.assertEqual(candidate.read_bytes(), original)
                temporary_files = list(paths.download_dir.glob("*.tmp"))
                self.assertEqual(len(temporary_files), 1)
                replacement = paths.download_dir / "replacement.deb"
                replacement.write_bytes(b"unreviewed replacement")
                os.replace(replacement, temporary_files[0])
                swapped_name = temporary_files[0]
                return {"sha256": hashlib.sha256(original).hexdigest()}

            with self.assertRaises(ValueError):
                download_to_cache(
                    "https://example.invalid/chatgpt.deb",
                    paths.download_file,
                    lambda _url: _Response([original]),
                    reviewer=reviewer,
                )

            self.assertEqual(paths.download_file.read_bytes(), b"previous reviewed candidate")
            self.assertIsNotNone(swapped_name)
            assert swapped_name is not None
            self.assertEqual(swapped_name.read_bytes(), b"unreviewed replacement")

    def test_reviewed_download_rejects_a_destination_swap_after_replace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            paths = self._paths(Path(temporary_directory))
            paths.download_file.write_bytes(b"previous reviewed candidate")
            real_replace = os.replace

            def replace_then_swap(source: str, destination: str, **kwargs: object) -> None:
                real_replace(source, destination, **kwargs)
                if destination == paths.download_file.name:
                    replacement = paths.download_dir / "post-rename-replacement.deb"
                    replacement.write_bytes(b"unreviewed post-rename replacement")
                    real_replace(
                        replacement.name,
                        destination,
                        src_dir_fd=kwargs["dst_dir_fd"],
                        dst_dir_fd=kwargs["dst_dir_fd"],
                    )

            with mock.patch("updater.common.os.replace", side_effect=replace_then_swap):
                with self.assertRaises(ValueError):
                    download_to_cache(
                        "https://example.invalid/chatgpt.deb",
                        paths.download_file,
                        lambda _url: _Response([b"reviewed candidate"]),
                        reviewer=self._matching_reviewer,
                    )

            self.assertEqual(paths.download_file.read_bytes(), b"unreviewed post-rename replacement")
            self.assertIsNone(read_record(paths))

    def test_review_source_can_inspect_the_held_download_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            paths = self._paths(temporary_path)
            source_directory = temporary_path / "source"
            source_directory.mkdir()
            source = _build_deb(source_directory)
            source_bytes = source.read_bytes()

            result = download_to_cache(
                "https://example.invalid/chatgpt.deb",
                paths.download_file,
                lambda _url: _Response([source_bytes]),
                reviewer=review_source,
            )

            self.assertEqual(result.read_bytes(), source_bytes)

    def test_cache_lock_reports_existing_flock_without_waiting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            paths = self._paths(Path(temporary_directory))
            descriptor = os.open(paths.lock_file, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaises(BlockingIOError):
                    with cache_lock(paths):
                        self.fail("a held updater lock must not be acquired")
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

    def test_compare_versions_uses_vercmp_result(self) -> None:
        observed_argv: list[str] = []

        def runner(argv: list[str], **_kwargs: object) -> SimpleNamespace:
            observed_argv.extend(argv)
            return SimpleNamespace(returncode=0, stdout="-1\n", stderr="")

        self.assertEqual(compare_versions("26.810.52044", "26.900.1", runner), -1)
        self.assertEqual(observed_argv, ["/usr/bin/vercmp", "26.810.52044", "26.900.1"])

    def test_compare_versions_rejects_unusable_vercmp_output(self) -> None:
        def runner(_argv: list[str], **_kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(returncode=0, stdout="not-a-comparison\n", stderr="")

        with self.assertRaises(ValueError):
            compare_versions("26.810.52044", "26.900.1", runner)


if __name__ == "__main__":
    unittest.main()
