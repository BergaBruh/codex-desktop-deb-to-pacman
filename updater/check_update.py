from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import contextlib
from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from .common import (
        CandidateRecord,
        SourceValidator,
        cache_paths,
        compare_versions,
        download_to_cache,
        read_record,
        read_source_validator,
        write_record,
        write_source_validator,
    )
except ImportError:  # Executed directly from the installed updater directory.
    from common import (
        CandidateRecord,
        SourceValidator,
        cache_paths,
        compare_versions,
        download_to_cache,
        read_record,
        read_source_validator,
        write_record,
        write_source_validator,
    )

try:
    from scripts import review_source
except ImportError:  # Package-owned updater snapshot can place review_source.py next to this file.
    import review_source


DEFAULT_SOURCE_URL = "https://persistent.oaistatic.com/codex-app-prod/linux/deb/latest/chatgpt_amd64.deb"
FIREFOX_LINUX_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:142.0) Gecko/20100101 Firefox/142.0"
INSTALLED_METADATA_PATH = Path("/usr/lib/chatgpt/resources/linux-package-metadata.json")
NOTIFY_SEND = Path("/usr/bin/notify-send")
SAME_VERSION_WARNING_FILE = "same-version-candidate-warning.json"


class CandidateReviewFailure(Exception):
    pass


def _safe_line(value: object) -> str:
    return str(value).replace("\r", " ").replace("\n", " ")


def _read_installed_version(metadata_path: Path) -> str:
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"could not read installed ChatGPT metadata: {error}") from error
    version = metadata.get("version") if isinstance(metadata, dict) else None
    if not isinstance(version, str) or not version or "\n" in version or "\r" in version:
        raise ValueError("installed ChatGPT metadata does not contain a valid version")
    return version


def _candidate_from_review(paths, metadata: Mapping[str, object], deb_path: Path) -> CandidateRecord:
    version = metadata.get("debian_version")
    sha256 = metadata.get("sha256")
    source_date_epoch = metadata.get("source_date_epoch")
    allowed_members = metadata.get("allowed_members")
    if not isinstance(version, str) or not isinstance(sha256, str):
        raise ValueError("review metadata does not contain candidate identity")
    if isinstance(source_date_epoch, bool) or not isinstance(source_date_epoch, int):
        raise ValueError("review metadata does not contain a valid source date epoch")
    if not isinstance(allowed_members, list) or any(not isinstance(member, str) for member in allowed_members):
        raise ValueError("review metadata does not contain a valid allowed-member list")
    return CandidateRecord(
        version=version,
        sha256=sha256,
        source_date_epoch=source_date_epoch,
        deb_path=deb_path,
        allowed_members=tuple(allowed_members),
    )


def _copy_regular_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent, delete=False) as temporary_file:
        temporary_path = Path(temporary_file.name)
        try:
            with source.open("rb") as source_file:
                shutil.copyfileobj(source_file, temporary_file)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
            os.replace(temporary_path, destination)
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise


def _materialize_reviewed_candidate(paths, reviewed_path: Path, sha256: str) -> Path:
    destination = paths.download_dir / f"chatgpt_amd64-{sha256}.deb"
    if destination.exists():
        if not destination.is_file() or hashlib_sha256(destination) != sha256:
            raise ValueError("existing reviewed candidate cache entry does not match its digest")
        return destination
    _copy_regular_file(reviewed_path, destination)
    if hashlib_sha256(destination) != sha256:
        destination.unlink(missing_ok=True)
        raise ValueError("reviewed candidate cache entry changed while being stored")
    return destination


def _preserve_previous_record(paths, previous: CandidateRecord | None) -> CandidateRecord | None:
    if previous is None:
        return None
    destination = paths.download_dir / f"chatgpt_amd64-{previous.sha256}.deb"
    if previous.deb_path == destination:
        return previous
    try:
        if not destination.exists():
            _copy_regular_file(previous.deb_path, destination)
        return CandidateRecord(
            version=previous.version,
            sha256=previous.sha256,
            source_date_epoch=previous.source_date_epoch,
            deb_path=destination,
            allowed_members=previous.allowed_members,
        )
    except OSError as error:
        print(f"ChatGPT update warning: could not preserve previous candidate: {_safe_line(error)}")
        return previous


def hashlib_sha256(path: Path) -> str:
    import hashlib

    with path.open("rb") as contents:
        return hashlib.file_digest(contents, "sha256").hexdigest()


def _read_previous_record(paths) -> CandidateRecord | None:
    try:
        return read_record(paths)
    except ValueError as error:
        print(f"ChatGPT update warning: ignoring invalid cached candidate record: {_safe_line(error)}")
        return None


def _write_same_version_warning(paths, previous: CandidateRecord | None, candidate: CandidateRecord) -> None:
    warning_path = paths.state_dir / SAME_VERSION_WARNING_FILE
    payload = {
        "candidate": _record_payload(candidate),
        "previous": _record_payload(previous) if previous is not None else None,
    }
    temporary_path = warning_path.with_name(f".{warning_path.name}.tmp")
    temporary_path.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    os.replace(temporary_path, warning_path)


def _record_payload(record: CandidateRecord) -> dict[str, object]:
    payload = asdict(record)
    payload["deb_path"] = str(record.deb_path)
    payload["allowed_members"] = list(record.allowed_members)
    return payload


def _emit(title: str, body: str, *, runner, notify_send: Path) -> None:
    title = _safe_line(title)
    body = _safe_line(body)
    if notify_send.is_file():
        result = runner([str(notify_send), title, body], check=False, capture_output=True, text=True)
        if result.returncode == 0:
            return
    print(f"{title}: {body}")


def _single_line(value: object) -> str | None:
    if not isinstance(value, str) or not value or "\r" in value or "\n" in value:
        return None
    return value


def _header_value(headers: object, name: str) -> str | None:
    getter = getattr(headers, "get", None)
    if callable(getter):
        value = getter(name)
        if value is not None:
            return _single_line(value)
    if isinstance(headers, Mapping):
        for header_name, value in headers.items():
            if isinstance(header_name, str) and header_name.lower() == name.lower():
                return _single_line(value)
    return None


def _source_validator_from_headers(url: str, headers: object) -> SourceValidator | None:
    etag = _header_value(headers, "ETag")
    last_modified = _header_value(headers, "Last-Modified")
    if etag is None and last_modified is None:
        return None
    return SourceValidator(url=url, etag=etag, last_modified=last_modified)


def _not_modified_by_preflight(url: str, validator: SourceValidator) -> bool:
    headers = {"User-Agent": FIREFOX_LINUX_USER_AGENT}
    if validator.etag is not None:
        headers["If-None-Match"] = validator.etag
    elif validator.last_modified is not None:
        headers["If-Modified-Since"] = validator.last_modified
    else:
        return False
    request = urllib.request.Request(url, headers=headers, method="HEAD")
    try:
        with contextlib.closing(urllib.request.urlopen(request)):
            return False
    except urllib.error.HTTPError as error:
        try:
            return error.code == 304
        finally:
            error.close()
    except (OSError, urllib.error.URLError, TimeoutError):
        return False


def _write_source_validator_if_present(paths, validator: SourceValidator | None) -> None:
    if validator is None:
        return
    try:
        write_source_validator(paths, validator)
    except ValueError as error:
        print(f"ChatGPT update warning: could not store source validator: {_safe_line(error)}")


def _download_and_review(url: str, paths) -> tuple[Path, Mapping[str, object], SourceValidator | None]:
    review_report: Mapping[str, object] | None = None
    source_validator: SourceValidator | None = None

    def reviewer(candidate: Path) -> Mapping[str, object]:
        nonlocal review_report
        try:
            review_report = review_source.review_source(candidate)
        except Exception as error:
            raise CandidateReviewFailure(str(error)) from error
        return review_report

    def opener(download_url: str) -> object:
        nonlocal source_validator
        request = urllib.request.Request(download_url, headers={"User-Agent": FIREFOX_LINUX_USER_AGENT})
        response = urllib.request.urlopen(request)
        source_validator = _source_validator_from_headers(url, getattr(response, "headers", None))
        return response

    downloaded_path = download_to_cache(url, paths.download_file, opener, reviewer=reviewer)
    if review_report is None:
        raise CandidateReviewFailure("review did not return candidate metadata")
    return downloaded_path, review_report, source_validator


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check for a reviewed ChatGPT Debian candidate")
    parser.add_argument("--url", default=DEFAULT_SOURCE_URL, help="candidate Debian URL")
    parser.add_argument("--metadata", type=Path, default=INSTALLED_METADATA_PATH, help=argparse.SUPPRESS)
    parser.add_argument(
        "--defer-new-candidate-notification",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parse_args(list(argv) if argv is not None else [])
    paths = cache_paths(os.environ)
    try:
        installed_version = _read_installed_version(arguments.metadata)
    except ValueError as error:
        print(f"ChatGPT update check failed: {_safe_line(error)}")
        return 4

    previous_record = _preserve_previous_record(paths, _read_previous_record(paths))
    if previous_record is not None:
        source_validator = read_source_validator(paths, arguments.url)
        if source_validator is not None and _not_modified_by_preflight(arguments.url, source_validator):
            return 0

    try:
        downloaded_path, review_report, source_validator = _download_and_review(arguments.url, paths)
    except CandidateReviewFailure as error:
        print(f"ChatGPT candidate review failed: {_safe_line(error)}")
        return 3
    except (OSError, urllib.error.URLError, TimeoutError) as error:
        print(f"ChatGPT candidate download failed: {_safe_line(error)}")
        return 2

    try:
        digest = review_report.get("sha256")
        if not isinstance(digest, str):
            raise ValueError("review metadata does not contain a candidate SHA-256")
        stored_path = _materialize_reviewed_candidate(paths, downloaded_path, digest)
        candidate = _candidate_from_review(paths, review_report, stored_path)
        comparison = compare_versions(candidate.version, installed_version, subprocess.run)
    except ValueError as error:
        print(f"ChatGPT update check failed: {_safe_line(error)}")
        return 4

    if comparison <= 0:
        if comparison == 0 and previous_record is not None and previous_record.sha256 != candidate.sha256:
            try:
                write_record(paths, previous_record)
                _write_same_version_warning(paths, previous_record, candidate)
            except ValueError as error:
                print(f"ChatGPT update check failed: {_safe_line(error)}")
                return 4
            _emit(
                "ChatGPT candidate checksum changed",
                f"{candidate.version} matches the installed version but has a different SHA-256",
                runner=subprocess.run,
                notify_send=NOTIFY_SEND,
            )
        return 0

    if previous_record is not None and previous_record.sha256 == candidate.sha256:
        _write_source_validator_if_present(paths, source_validator)
        return 0

    try:
        write_record(paths, candidate)
    except ValueError as error:
        print(f"ChatGPT update check failed: {_safe_line(error)}")
        return 4
    _write_source_validator_if_present(paths, source_validator)
    if not arguments.defer_new_candidate_notification:
        _emit("ChatGPT update available", candidate.version, runner=subprocess.run, notify_send=NOTIFY_SEND)
    return 10


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
