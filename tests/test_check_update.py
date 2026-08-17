from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from updater.check_update import main
import updater.check_update as check_update
from updater.common import CandidateRecord, SourceValidator, cache_paths, read_record, read_source_validator, write_record, write_source_validator


TEST_URL = "https://example.invalid/chatgpt_amd64.deb"
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class _Response:
    def __init__(
        self,
        chunks: list[bytes],
        error: Exception | None = None,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._chunks = iter(chunks)
        self._error = error
        self._status = status
        self.headers = headers or {}

    def read(self, _size: int) -> bytes:
        if self._error is not None:
            error = self._error
            self._error = None
            raise error
        return next(self._chunks, b"")

    def getcode(self) -> int:
        return self._status

    def close(self) -> None:
        return None


class _FakeNotify:
    def __init__(self, *, available: bool = True) -> None:
        self.path = Path(tempfile.mkdtemp()) / "notify-send"
        self.calls: list[tuple[str, str]] = []
        if available:
            self.path.write_text("#!/bin/sh\n", encoding="utf-8")
            self.path.chmod(0o755)


class _FakeRunner:
    def __init__(self, notify: _FakeNotify, vercmp_result: int = 1, notify_returncode: int = 0) -> None:
        self.notify = notify
        self.vercmp_result = vercmp_result
        self.notify_returncode = notify_returncode
        self.commands: list[list[str]] = []

    def __call__(self, argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        self.commands.append([str(argument) for argument in argv])
        if argv[0] == "/usr/bin/vercmp":
            return subprocess.CompletedProcess(argv, 0, stdout=f"{self.vercmp_result}\n", stderr="")
        if argv[0] == str(self.notify.path):
            self.notify.calls.append((str(argv[1]), str(argv[2])))
            return subprocess.CompletedProcess(argv, self.notify_returncode, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {argv!r}")


class CheckUpdateTests(unittest.TestCase):
    def _metadata(self, directory: Path, version: str = "26.810.52044") -> Path:
        path = directory / "linux-package-metadata.json"
        path.write_text(json.dumps({"version": version}), encoding="utf-8")
        return path

    def _reviewer(self, version: str = "26.900.1"):
        def review(candidate: Path) -> dict[str, object]:
            return {
                "sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
                "debian_version": version,
                "architecture": "amd64",
                "source_date_epoch": 1786770000,
                "allowed_members": ["usr/bin/chatgpt", "usr/lib/chatgpt/ChatGPT"],
            }

        return review

    def _record(self, paths, contents: bytes = b"previous candidate") -> CandidateRecord:
        candidate = paths.download_dir / "previous.deb"
        candidate.write_bytes(contents)
        return CandidateRecord(
            version="26.850.1",
            sha256=hashlib.sha256(contents).hexdigest(),
            source_date_epoch=1780000000,
            deb_path=candidate,
            allowed_members=("usr/bin/chatgpt",),
        )

    def _canonical_record(self, paths, contents: bytes = b"previous candidate") -> CandidateRecord:
        sha256 = hashlib.sha256(contents).hexdigest()
        candidate = paths.download_dir / f"chatgpt_amd64-{sha256}.deb"
        candidate.write_bytes(contents)
        return CandidateRecord(
            version="26.850.1",
            sha256=sha256,
            source_date_epoch=1780000000,
            deb_path=candidate,
            allowed_members=("usr/bin/chatgpt",),
        )

    def test_not_modified_preflight_returns_current_without_downloading_or_notifying_and_preserves_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            paths = cache_paths({"XDG_CACHE_HOME": str(root / "cache"), "XDG_STATE_HOME": str(root / "state")})
            previous = self._canonical_record(paths)
            write_record(paths, previous)
            write_source_validator(paths, SourceValidator(url=TEST_URL, etag='"cached-etag"'))
            notify = _FakeNotify()
            runner = _FakeRunner(notify)
            requests: list[check_update.urllib.request.Request] = []

            def urlopen(request: object) -> _Response:
                self.assertIsInstance(request, check_update.urllib.request.Request)
                assert isinstance(request, check_update.urllib.request.Request)
                requests.append(request)
                if request.get_method() == "HEAD":
                    raise check_update.urllib.error.HTTPError(TEST_URL, 304, "Not Modified", {}, None)
                self.fail("304 preflight must not continue to a GET download")

            with (
                mock.patch.dict(
                    check_update.os.environ,
                    {"XDG_CACHE_HOME": str(root / "cache"), "XDG_STATE_HOME": str(root / "state")},
                    clear=False,
                ),
                mock.patch.object(check_update, "INSTALLED_METADATA_PATH", self._metadata(root)),
                mock.patch.object(check_update, "NOTIFY_SEND", notify.path),
                mock.patch.object(check_update.subprocess, "run", runner),
                mock.patch.object(check_update.urllib.request, "urlopen", urlopen),
                mock.patch.object(check_update.review_source, "review_source") as reviewer,
            ):
                result = main(["--url", TEST_URL])

            self.assertEqual(result, 0)
            self.assertEqual(read_record(paths), previous)
            self.assertFalse(reviewer.called)
            self.assertEqual(notify.calls, [])
            self.assertEqual(runner.commands, [])
            self.assertEqual([request.get_method() for request in requests], ["HEAD"])
            self.assertEqual(requests[0].get_header("User-agent"), check_update.FIREFOX_LINUX_USER_AGENT)
            self.assertEqual(requests[0].get_header("If-none-match"), '"cached-etag"')

    def test_modified_preflight_uses_unconditional_get_and_persists_get_validator_after_record_write(self) -> None:
        candidate_bytes = b"new candidate with response validators"
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            paths = cache_paths({"XDG_CACHE_HOME": str(root / "cache"), "XDG_STATE_HOME": str(root / "state")})
            previous = self._canonical_record(paths)
            write_record(paths, previous)
            write_source_validator(paths, SourceValidator(url=TEST_URL, etag='"old-etag"'))
            notify = _FakeNotify()
            runner = _FakeRunner(notify, vercmp_result=1)
            requests: list[check_update.urllib.request.Request] = []

            def urlopen(request: object) -> _Response:
                self.assertIsInstance(request, check_update.urllib.request.Request)
                assert isinstance(request, check_update.urllib.request.Request)
                requests.append(request)
                if request.get_method() == "HEAD":
                    return _Response([], headers={"ETag": '"head-etag"'})
                return _Response(
                    [candidate_bytes],
                    headers={
                        "ETag": '"get-etag"',
                        "Last-Modified": "Mon, 17 Aug 2026 12:00:00 GMT",
                    },
                )

            with (
                mock.patch.dict(
                    check_update.os.environ,
                    {"XDG_CACHE_HOME": str(root / "cache"), "XDG_STATE_HOME": str(root / "state")},
                    clear=False,
                ),
                mock.patch.object(check_update, "INSTALLED_METADATA_PATH", self._metadata(root)),
                mock.patch.object(check_update, "NOTIFY_SEND", notify.path),
                mock.patch.object(check_update.subprocess, "run", runner),
                mock.patch.object(check_update.urllib.request, "urlopen", urlopen),
                mock.patch.object(check_update.review_source, "review_source", self._reviewer("26.900.1")),
            ):
                result = main(["--url", TEST_URL])

            self.assertEqual(result, 10)
            self.assertEqual(read_record(paths).version, "26.900.1")
            self.assertEqual(
                read_source_validator(paths, TEST_URL),
                SourceValidator(
                    url=TEST_URL,
                    etag='"get-etag"',
                    last_modified="Mon, 17 Aug 2026 12:00:00 GMT",
                ),
            )
            self.assertEqual([request.get_method() for request in requests], ["HEAD", "GET"])
            self.assertEqual(requests[0].get_header("If-none-match"), '"old-etag"')
            self.assertIsNone(requests[1].get_header("If-none-match"))
            self.assertIsNone(requests[1].get_header("If-modified-since"))
            self.assertEqual(notify.calls, [("ChatGPT update available", "26.900.1")])

    def test_missing_or_malformed_validator_state_falls_back_to_unconditional_get(self) -> None:
        for state in ("missing", "malformed"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as temporary_directory:
                root = Path(temporary_directory)
                paths = cache_paths({"XDG_CACHE_HOME": str(root / "cache"), "XDG_STATE_HOME": str(root / "state")})
                previous = self._canonical_record(paths)
                write_record(paths, previous)
                if state == "malformed":
                    paths.validator_file.write_text("{not json", encoding="utf-8")
                notify = _FakeNotify()
                runner = _FakeRunner(notify, vercmp_result=1)
                requests: list[check_update.urllib.request.Request] = []

                def urlopen(request: object) -> _Response:
                    self.assertIsInstance(request, check_update.urllib.request.Request)
                    assert isinstance(request, check_update.urllib.request.Request)
                    requests.append(request)
                    return _Response([b"candidate"], headers={"ETag": f'"{state}-etag"'})

                with (
                    mock.patch.dict(
                        check_update.os.environ,
                        {"XDG_CACHE_HOME": str(root / "cache"), "XDG_STATE_HOME": str(root / "state")},
                        clear=False,
                    ),
                    mock.patch.object(check_update, "INSTALLED_METADATA_PATH", self._metadata(root)),
                    mock.patch.object(check_update, "NOTIFY_SEND", notify.path),
                    mock.patch.object(check_update.subprocess, "run", runner),
                    mock.patch.object(check_update.urllib.request, "urlopen", urlopen),
                    mock.patch.object(check_update.review_source, "review_source", self._reviewer("26.900.1")),
                ):
                    result = main(["--url", TEST_URL])

                self.assertEqual(result, 10)
                self.assertEqual([request.get_method() for request in requests], ["GET"])
                self.assertIsNone(requests[0].get_header("If-none-match"))
                self.assertIsNone(requests[0].get_header("If-modified-since"))
                self.assertEqual(read_source_validator(paths, TEST_URL).etag, f'"{state}-etag"')

    def test_unsupported_head_preflight_falls_back_to_get_without_marking_current(self) -> None:
        for status in (405, 501):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temporary_directory:
                root = Path(temporary_directory)
                paths = cache_paths({"XDG_CACHE_HOME": str(root / "cache"), "XDG_STATE_HOME": str(root / "state")})
                previous = self._canonical_record(paths)
                write_record(paths, previous)
                write_source_validator(paths, SourceValidator(url=TEST_URL, last_modified="Mon, 17 Aug 2026 00:00:00 GMT"))
                notify = _FakeNotify()
                runner = _FakeRunner(notify, vercmp_result=1)
                requests: list[check_update.urllib.request.Request] = []

                def urlopen(request: object) -> _Response:
                    self.assertIsInstance(request, check_update.urllib.request.Request)
                    assert isinstance(request, check_update.urllib.request.Request)
                    requests.append(request)
                    if request.get_method() == "HEAD":
                        raise check_update.urllib.error.HTTPError(TEST_URL, status, "Unsupported", {}, None)
                    return _Response([b"candidate"], headers={"ETag": f'"get-after-{status}"'})

                with (
                    mock.patch.dict(
                        check_update.os.environ,
                        {"XDG_CACHE_HOME": str(root / "cache"), "XDG_STATE_HOME": str(root / "state")},
                        clear=False,
                    ),
                    mock.patch.object(check_update, "INSTALLED_METADATA_PATH", self._metadata(root)),
                    mock.patch.object(check_update, "NOTIFY_SEND", notify.path),
                    mock.patch.object(check_update.subprocess, "run", runner),
                    mock.patch.object(check_update.urllib.request, "urlopen", urlopen),
                    mock.patch.object(check_update.review_source, "review_source", self._reviewer("26.900.1")),
                ):
                    result = main(["--url", TEST_URL])

                self.assertEqual(result, 10)
                self.assertEqual([request.get_method() for request in requests], ["HEAD", "GET"])
                self.assertEqual(requests[0].get_header("If-modified-since"), "Mon, 17 Aug 2026 00:00:00 GMT")
                self.assertEqual(read_source_validator(paths, TEST_URL).etag, f'"get-after-{status}"')

    def test_preflight_transport_error_falls_back_to_existing_get_failure_path_and_preserves_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            paths = cache_paths({"XDG_CACHE_HOME": str(root / "cache"), "XDG_STATE_HOME": str(root / "state")})
            previous = self._canonical_record(paths)
            write_record(paths, previous)
            validator = SourceValidator(url=TEST_URL, etag='"cached-etag"')
            write_source_validator(paths, validator)
            notify = _FakeNotify()
            runner = _FakeRunner(notify, vercmp_result=1)
            requests: list[check_update.urllib.request.Request] = []

            def urlopen(request: object) -> _Response:
                self.assertIsInstance(request, check_update.urllib.request.Request)
                assert isinstance(request, check_update.urllib.request.Request)
                requests.append(request)
                if request.get_method() == "HEAD":
                    raise TimeoutError("preflight timed out")
                return _Response([b"partial"], OSError("network interrupted"), headers={"ETag": '"new-etag"'})

            with (
                mock.patch.dict(
                    check_update.os.environ,
                    {"XDG_CACHE_HOME": str(root / "cache"), "XDG_STATE_HOME": str(root / "state")},
                    clear=False,
                ),
                mock.patch.object(check_update, "INSTALLED_METADATA_PATH", self._metadata(root)),
                mock.patch.object(check_update, "NOTIFY_SEND", notify.path),
                mock.patch.object(check_update.subprocess, "run", runner),
                mock.patch.object(check_update.urllib.request, "urlopen", urlopen),
                mock.patch.object(check_update.review_source, "review_source", self._reviewer("26.900.1")),
            ):
                result = main(["--url", TEST_URL])

            self.assertEqual(result, 2)
            self.assertEqual(read_record(paths), previous)
            self.assertEqual(read_source_validator(paths, TEST_URL), validator)
            self.assertEqual([request.get_method() for request in requests], ["HEAD", "GET"])
            self.assertEqual(notify.calls, [])

    def test_newer_reviewed_candidate_is_recorded_and_notified_without_privileged_commands(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            notify = _FakeNotify()
            runner = _FakeRunner(notify, vercmp_result=1)

            with (
                mock.patch.dict(
                    check_update.os.environ,
                    {"XDG_CACHE_HOME": str(root / "cache"), "XDG_STATE_HOME": str(root / "state")},
                    clear=False,
                ),
                mock.patch.object(check_update, "INSTALLED_METADATA_PATH", self._metadata(root)),
                mock.patch.object(check_update, "NOTIFY_SEND", notify.path),
                mock.patch.object(check_update.subprocess, "run", runner),
                mock.patch.object(check_update.urllib.request, "urlopen", lambda _url: _Response([b"candidate"])),
                mock.patch.object(check_update.review_source, "review_source", self._reviewer("26.900.1")),
            ):
                result = main(["--url", TEST_URL])

            paths = cache_paths({"XDG_CACHE_HOME": str(root / "cache"), "XDG_STATE_HOME": str(root / "state")})
            self.assertEqual(result, 10)
            self.assertEqual(read_record(paths).version, "26.900.1")
            self.assertEqual(notify.calls, [("ChatGPT update available", "26.900.1")])
            captured_commands = {command[0] for command in runner.commands}
            captured_command_strings = {" ".join(command) for command in runner.commands}
            self.assertNotIn("makepkg", captured_commands)
            self.assertNotIn("pkexec", captured_commands)
            self.assertNotIn("pacman -U", captured_command_strings)
            for forbidden in ("pacman", "sudo", "systemctl enable", "update_source"):
                self.assertFalse(any(forbidden in command for command in captured_command_strings))

    def test_candidate_download_uses_firefox_linux_user_agent_request(self) -> None:
        captured_requests: list[object] = []

        def urlopen(request: object) -> _Response:
            captured_requests.append(request)
            return _Response([b"candidate"])

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            notify = _FakeNotify()
            runner = _FakeRunner(notify, vercmp_result=1)

            with (
                mock.patch.dict(
                    check_update.os.environ,
                    {"XDG_CACHE_HOME": str(root / "cache"), "XDG_STATE_HOME": str(root / "state")},
                    clear=False,
                ),
                mock.patch.object(check_update, "INSTALLED_METADATA_PATH", self._metadata(root)),
                mock.patch.object(check_update, "NOTIFY_SEND", notify.path),
                mock.patch.object(check_update.subprocess, "run", runner),
                mock.patch.object(check_update.urllib.request, "urlopen", urlopen),
                mock.patch.object(check_update.review_source, "review_source", self._reviewer("26.900.1")),
            ):
                result = main(["--url", TEST_URL])

        self.assertEqual(result, 10)
        self.assertEqual(len(captured_requests), 1)
        request = captured_requests[0]
        self.assertIsInstance(request, check_update.urllib.request.Request)
        self.assertEqual(request.full_url, TEST_URL)
        self.assertEqual(
            request.get_header("User-agent"),
            "Mozilla/5.0 (X11; Linux x86_64; rv:142.0) Gecko/20100101 Firefox/142.0",
        )

    def test_equal_or_older_candidate_returns_current_without_record_or_notification(self) -> None:
        for comparison in (0, -1):
            with self.subTest(comparison=comparison), tempfile.TemporaryDirectory() as temporary_directory:
                root = Path(temporary_directory)
                notify = _FakeNotify()
                runner = _FakeRunner(notify, vercmp_result=comparison)

                with (
                    mock.patch.dict(
                        check_update.os.environ,
                        {"XDG_CACHE_HOME": str(root / "cache"), "XDG_STATE_HOME": str(root / "state")},
                        clear=False,
                    ),
                    mock.patch.object(check_update, "INSTALLED_METADATA_PATH", self._metadata(root)),
                    mock.patch.object(check_update, "NOTIFY_SEND", notify.path),
                    mock.patch.object(check_update.subprocess, "run", runner),
                    mock.patch.object(check_update.urllib.request, "urlopen", lambda _url: _Response([b"candidate"])),
                    mock.patch.object(check_update.review_source, "review_source", self._reviewer("26.810.52044")),
                ):
                    result = main(["--url", TEST_URL])

                paths = cache_paths({"XDG_CACHE_HOME": str(root / "cache"), "XDG_STATE_HOME": str(root / "state")})
                self.assertEqual(result, 0)
                self.assertIsNone(read_record(paths))
                self.assertEqual(notify.calls, [])

    def test_invalid_debian_review_returns_review_failure_without_recording_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            notify = _FakeNotify()
            runner = _FakeRunner(notify)

            def reject(_candidate: Path) -> dict[str, object]:
                raise ValueError("invalid Debian payload")

            with (
                mock.patch.dict(
                    check_update.os.environ,
                    {"XDG_CACHE_HOME": str(root / "cache"), "XDG_STATE_HOME": str(root / "state")},
                    clear=False,
                ),
                mock.patch.object(check_update, "INSTALLED_METADATA_PATH", self._metadata(root)),
                mock.patch.object(check_update, "NOTIFY_SEND", notify.path),
                mock.patch.object(check_update.subprocess, "run", runner),
                mock.patch.object(check_update.urllib.request, "urlopen", lambda _url: _Response([b"not a deb"])),
                mock.patch.object(check_update.review_source, "review_source", reject),
            ):
                result = main(["--url", TEST_URL])

            paths = cache_paths({"XDG_CACHE_HOME": str(root / "cache"), "XDG_STATE_HOME": str(root / "state")})
            self.assertEqual(result, 3)
            self.assertIsNone(read_record(paths))
            self.assertEqual(notify.calls, [])

    def test_failed_download_preserves_previous_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            paths = cache_paths({"XDG_CACHE_HOME": str(root / "cache"), "XDG_STATE_HOME": str(root / "state")})
            previous = self._record(paths)
            write_record(paths, previous)
            notify = _FakeNotify()
            runner = _FakeRunner(notify)

            with (
                mock.patch.dict(
                    check_update.os.environ,
                    {"XDG_CACHE_HOME": str(root / "cache"), "XDG_STATE_HOME": str(root / "state")},
                    clear=False,
                ),
                mock.patch.object(check_update, "INSTALLED_METADATA_PATH", self._metadata(root)),
                mock.patch.object(check_update, "NOTIFY_SEND", notify.path),
                mock.patch.object(check_update.subprocess, "run", runner),
                mock.patch.object(
                    check_update.urllib.request,
                    "urlopen",
                    lambda _url: _Response([b"partial"], OSError("network interrupted")),
                ),
                mock.patch.object(check_update.review_source, "review_source", self._reviewer("26.900.1")),
            ):
                result = main(["--url", TEST_URL])

            self.assertEqual(result, 2)
            self.assertEqual(read_record(paths), previous)
            self.assertEqual(notify.calls, [])

    def test_missing_notification_binary_falls_back_to_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            notify = _FakeNotify(available=False)
            runner = _FakeRunner(notify, vercmp_result=1)

            with (
                mock.patch.dict(
                    check_update.os.environ,
                    {"XDG_CACHE_HOME": str(root / "cache"), "XDG_STATE_HOME": str(root / "state")},
                    clear=False,
                ),
                mock.patch.object(check_update, "INSTALLED_METADATA_PATH", self._metadata(root)),
                mock.patch.object(check_update, "NOTIFY_SEND", notify.path),
                mock.patch.object(check_update.subprocess, "run", runner),
                mock.patch.object(check_update.urllib.request, "urlopen", lambda _url: _Response([b"candidate"])),
                mock.patch.object(check_update.review_source, "review_source", self._reviewer("26.900.1")),
                mock.patch("builtins.print") as printed,
            ):
                result = main(["--url", TEST_URL])

            self.assertEqual(result, 10)
            self.assertEqual(notify.calls, [])
            printed.assert_any_call("ChatGPT update available: 26.900.1")

    def test_failed_notification_binary_falls_back_to_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            notify = _FakeNotify(available=True)
            runner = _FakeRunner(notify, vercmp_result=1, notify_returncode=1)

            with (
                mock.patch.dict(
                    check_update.os.environ,
                    {"XDG_CACHE_HOME": str(root / "cache"), "XDG_STATE_HOME": str(root / "state")},
                    clear=False,
                ),
                mock.patch.object(check_update, "INSTALLED_METADATA_PATH", self._metadata(root)),
                mock.patch.object(check_update, "NOTIFY_SEND", notify.path),
                mock.patch.object(check_update.subprocess, "run", runner),
                mock.patch.object(check_update.urllib.request, "urlopen", lambda _url: _Response([b"candidate"])),
                mock.patch.object(check_update.review_source, "review_source", self._reviewer("26.900.1")),
                mock.patch("builtins.print") as printed,
            ):
                result = main(["--url", TEST_URL])

            self.assertEqual(result, 10)
            self.assertEqual(notify.calls, [("ChatGPT update available", "26.900.1")])
            printed.assert_any_call("ChatGPT update available: 26.900.1")

    def test_same_version_different_sha_preserves_old_candidate_and_records_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            paths = cache_paths({"XDG_CACHE_HOME": str(root / "cache"), "XDG_STATE_HOME": str(root / "state")})
            old_bytes = b"old reviewed current-version candidate"
            new_bytes = b"new reviewed current-version candidate"
            old_record = CandidateRecord(
                version="26.810.52044",
                sha256=hashlib.sha256(old_bytes).hexdigest(),
                source_date_epoch=1780000000,
                deb_path=paths.download_file,
                allowed_members=("usr/bin/chatgpt",),
            )
            paths.download_file.write_bytes(old_bytes)
            write_record(paths, old_record)
            notify = _FakeNotify()
            runner = _FakeRunner(notify, vercmp_result=0)

            with (
                mock.patch.dict(
                    check_update.os.environ,
                    {"XDG_CACHE_HOME": str(root / "cache"), "XDG_STATE_HOME": str(root / "state")},
                    clear=False,
                ),
                mock.patch.object(check_update, "INSTALLED_METADATA_PATH", self._metadata(root, "26.810.52044")),
                mock.patch.object(check_update, "NOTIFY_SEND", notify.path),
                mock.patch.object(check_update.subprocess, "run", runner),
                mock.patch.object(check_update.urllib.request, "urlopen", lambda _url: _Response([new_bytes])),
                mock.patch.object(check_update.review_source, "review_source", self._reviewer("26.810.52044")),
            ):
                result = main(["--url", TEST_URL])

            preserved = read_record(paths)
            warning = json.loads((paths.state_dir / check_update.SAME_VERSION_WARNING_FILE).read_text(encoding="utf-8"))
            self.assertEqual(result, 0)
            self.assertEqual(preserved.sha256, old_record.sha256)
            self.assertEqual(preserved.deb_path.read_bytes(), old_bytes)
            self.assertEqual(warning["previous"]["sha256"], old_record.sha256)
            self.assertEqual(warning["candidate"]["sha256"], hashlib.sha256(new_bytes).hexdigest())
            self.assertEqual(notify.calls, [("ChatGPT candidate checksum changed", "26.810.52044 matches the installed version but has a different SHA-256")])

    def test_same_version_unrecorded_candidate_validator_is_not_persisted_for_later_304_skip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            paths = cache_paths({"XDG_CACHE_HOME": str(root / "cache"), "XDG_STATE_HOME": str(root / "state")})
            old_bytes = b"old reviewed current-version candidate"
            new_bytes = b"new reviewed current-version candidate"
            old_record = CandidateRecord(
                version="26.810.52044",
                sha256=hashlib.sha256(old_bytes).hexdigest(),
                source_date_epoch=1780000000,
                deb_path=paths.download_file,
                allowed_members=("usr/bin/chatgpt",),
            )
            paths.download_file.write_bytes(old_bytes)
            write_record(paths, old_record)
            notify = _FakeNotify()
            runner = _FakeRunner(notify, vercmp_result=0)
            requests: list[check_update.urllib.request.Request] = []

            def urlopen(request: object) -> _Response:
                self.assertIsInstance(request, check_update.urllib.request.Request)
                assert isinstance(request, check_update.urllib.request.Request)
                requests.append(request)
                if request.get_method() == "HEAD":
                    raise check_update.urllib.error.HTTPError(TEST_URL, 304, "Not Modified", {}, None)
                return _Response([new_bytes], headers={"ETag": '"unrecorded-same-version-etag"'})

            with (
                mock.patch.dict(
                    check_update.os.environ,
                    {"XDG_CACHE_HOME": str(root / "cache"), "XDG_STATE_HOME": str(root / "state")},
                    clear=False,
                ),
                mock.patch.object(check_update, "INSTALLED_METADATA_PATH", self._metadata(root, "26.810.52044")),
                mock.patch.object(check_update, "NOTIFY_SEND", notify.path),
                mock.patch.object(check_update.subprocess, "run", runner),
                mock.patch.object(check_update.urllib.request, "urlopen", urlopen),
                mock.patch.object(check_update.review_source, "review_source", self._reviewer("26.810.52044")),
            ):
                first_result = main(["--url", TEST_URL])
                second_result = main(["--url", TEST_URL])

            preserved = read_record(paths)
            self.assertEqual(first_result, 0)
            self.assertEqual(second_result, 0)
            self.assertEqual(preserved.sha256, old_record.sha256)
            self.assertIsNone(read_source_validator(paths, TEST_URL))
            self.assertEqual([request.get_method() for request in requests], ["GET", "GET"])
            self.assertEqual(
                notify.calls,
                [
                    ("ChatGPT candidate checksum changed", "26.810.52044 matches the installed version but has a different SHA-256"),
                    ("ChatGPT candidate checksum changed", "26.810.52044 matches the installed version but has a different SHA-256"),
                ],
            )

    def test_unchanged_candidate_hash_does_not_repeat_desktop_notification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            paths = cache_paths({"XDG_CACHE_HOME": str(root / "cache"), "XDG_STATE_HOME": str(root / "state")})
            candidate_bytes = b"same reviewed candidate"
            previous = self._record(paths, candidate_bytes)
            previous = CandidateRecord(
                version="26.900.1",
                sha256=previous.sha256,
                source_date_epoch=previous.source_date_epoch,
                deb_path=previous.deb_path,
                allowed_members=previous.allowed_members,
            )
            write_record(paths, previous)
            notify = _FakeNotify()
            runner = _FakeRunner(notify, vercmp_result=1)

            with (
                mock.patch.dict(
                    check_update.os.environ,
                    {"XDG_CACHE_HOME": str(root / "cache"), "XDG_STATE_HOME": str(root / "state")},
                    clear=False,
                ),
                mock.patch.object(check_update, "INSTALLED_METADATA_PATH", self._metadata(root)),
                mock.patch.object(check_update, "NOTIFY_SEND", notify.path),
                mock.patch.object(check_update.subprocess, "run", runner),
                mock.patch.object(check_update.urllib.request, "urlopen", lambda _url: _Response([candidate_bytes])),
                mock.patch.object(check_update.review_source, "review_source", self._reviewer("26.900.1")),
            ):
                result = main(["--url", TEST_URL])

            self.assertEqual(result, 0)
            self.assertEqual(read_record(paths), previous)
            self.assertEqual(notify.calls, [])

    def test_missing_installed_metadata_is_an_installed_version_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            notify = _FakeNotify()
            runner = _FakeRunner(notify)
            with (
                mock.patch.dict(
                    check_update.os.environ,
                    {"XDG_CACHE_HOME": str(root / "cache"), "XDG_STATE_HOME": str(root / "state")},
                    clear=False,
                ),
                mock.patch.object(check_update, "INSTALLED_METADATA_PATH", root / "missing.json"),
                mock.patch.object(check_update, "NOTIFY_SEND", notify.path),
                mock.patch.object(check_update.subprocess, "run", runner),
                mock.patch.object(check_update.urllib.request, "urlopen") as urlopen,
            ):
                result = main(["--url", TEST_URL])

            self.assertEqual(result, 4)
            self.assertFalse(urlopen.called)
            self.assertEqual(runner.commands, [])

    def test_launcher_forwards_cli_options_to_checker_help(self) -> None:
        result = subprocess.run(
            [str(REPOSITORY_ROOT / "updater" / "chatgpt-bin-check-update"), "--help"],
            check=False,
            capture_output=True,
            text=True,
            cwd=REPOSITORY_ROOT,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--url", result.stdout)
        self.assertIn("candidate Debian URL", result.stdout)

    def test_checker_sources_do_not_contain_build_install_or_timer_commands(self) -> None:
        for relative_path in ("updater/check_update.py", "updater/chatgpt-bin-check-update"):
            with self.subTest(path=relative_path):
                source = (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")
                for forbidden in ("pacman", "sudo", "makepkg", "pkexec", "update_source", "systemctl enable"):
                    self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
