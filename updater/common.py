from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, closing
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess


_SHA256_PATTERN = set("0123456789abcdef")
SOURCE_VALIDATORS_FILE = "source-validators.json"


@dataclass(frozen=True)
class CandidateRecord:
    version: str
    sha256: str
    source_date_epoch: int
    deb_path: Path
    allowed_members: tuple[str, ...]


@dataclass(frozen=True)
class SourceValidator:
    url: str
    etag: str | None = None
    last_modified: str | None = None


@dataclass(frozen=True)
class CachePaths:
    cache_home: Path
    state_home: Path
    cache_dir: Path
    state_dir: Path
    download_dir: Path
    recipe_dir: Path
    work_dir: Path
    package_dir: Path
    download_file: Path
    state_file: Path
    validator_file: Path
    lock_file: Path


def _path_components(path: Path) -> tuple[str, ...]:
    if not path.is_absolute() or any(component in {"", ".", ".."} for component in path.parts[1:]):
        raise ValueError("updater directory must be an absolute normal path")
    return path.parts[1:]


def _open_directory_component(parent_descriptor: int, name: str, *, create: bool, private: bool) -> int:
    if name in {"", ".", ".."} or "/" in name or "\x00" in name:
        raise ValueError("updater directory component is invalid")
    try:
        if create:
            try:
                os.mkdir(name, mode=0o700, dir_fd=parent_descriptor)
            except FileExistsError:
                pass
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=parent_descriptor,
        )
    except OSError as error:
        raise ValueError(f"could not safely open updater directory component: {name}") from error
    try:
        directory_stat = os.fstat(descriptor)
        if not stat.S_ISDIR(directory_stat.st_mode):
            raise ValueError(f"updater path component is not a directory: {name}")
        if private:
            if directory_stat.st_uid != os.geteuid():
                raise ValueError(f"updater directory is not privately owned: {name}")
            os.fchmod(descriptor, 0o700)
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _open_xdg_root(path: Path, *, create: bool) -> int:
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for component in _path_components(path):
            child_descriptor = _open_directory_component(descriptor, component, create=create, private=False)
            os.close(descriptor)
            descriptor = child_descriptor
        directory_stat = os.fstat(descriptor)
        if directory_stat.st_uid != os.geteuid():
            raise ValueError(f"XDG root is not owned by the invoking user: {path}")
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _open_updater_directory(base: Path, components: tuple[str, ...], *, create: bool = False) -> int:
    descriptor = _open_xdg_root(base, create=create)
    try:
        for component in components:
            child_descriptor = _open_directory_component(descriptor, component, create=create, private=True)
            os.close(descriptor)
            descriptor = child_descriptor
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def cache_paths(environ: Mapping[str, str]) -> CachePaths:
    """Create and return the updater's private XDG cache and state paths."""

    home_value = environ.get("HOME", "")
    home = Path(home_value) if home_value and Path(home_value).is_absolute() else Path.home()
    if any(component in {"", ".", ".."} for component in home.parts[1:]):
        home = Path.home()
    cache_value = environ.get("XDG_CACHE_HOME", "")
    state_value = environ.get("XDG_STATE_HOME", "")
    cache_home = Path(cache_value) if cache_value and Path(cache_value).is_absolute() else home / ".cache"
    state_home = Path(state_value) if state_value and Path(state_value).is_absolute() else home / ".local" / "state"
    cache_dir = cache_home / "chatgpt-bin"
    state_dir = state_home / "chatgpt-bin"
    download_dir = cache_dir / "downloads"
    recipe_dir = cache_dir / "recipe"
    work_dir = cache_dir / "work"
    package_dir = cache_dir / "packages"
    for components in (
        ("chatgpt-bin",),
        ("chatgpt-bin", "downloads"),
        ("chatgpt-bin", "recipe"),
        ("chatgpt-bin", "work"),
        ("chatgpt-bin", "packages"),
    ):
        cache_descriptor = _open_updater_directory(cache_home, components, create=True)
        os.close(cache_descriptor)
    state_descriptor = _open_updater_directory(state_home, ("chatgpt-bin",), create=True)
    os.close(state_descriptor)
    return CachePaths(
        cache_home=cache_home,
        state_home=state_home,
        cache_dir=cache_dir,
        state_dir=state_dir,
        download_dir=download_dir,
        recipe_dir=recipe_dir,
        work_dir=work_dir,
        package_dir=package_dir,
        download_file=download_dir / "chatgpt_amd64.deb",
        state_file=state_dir / "candidate.json",
        validator_file=state_dir / SOURCE_VALIDATORS_FILE,
        lock_file=state_dir / "updater.lock",
    )


def _open_download_directory(paths: CachePaths) -> int:
    expected = paths.cache_home / "chatgpt-bin" / "downloads"
    if paths.download_dir != expected:
        raise ValueError("updater download directory does not match its cache root")
    return _open_updater_directory(paths.cache_home, ("chatgpt-bin", "downloads"))


def _open_state_directory(paths: CachePaths) -> int:
    expected = paths.state_home / "chatgpt-bin"
    if paths.state_dir != expected:
        raise ValueError("updater state directory does not match its state root")
    return _open_updater_directory(paths.state_home, ("chatgpt-bin",))


def _open_download_destination_directory(destination: Path) -> int:
    directory = destination.parent
    if directory.name != "downloads" or directory.parent.name != "chatgpt-bin":
        raise ValueError("download destination is not in the updater download directory")
    return _open_updater_directory(directory.parent.parent, ("chatgpt-bin", "downloads"))


def _leaf_name(path: Path) -> str:
    if path.name in {"", ".", ".."}:
        raise ValueError("updater file name must be a single path component")
    return path.name


def _open_regular_file_at(directory_descriptor: int, name: str) -> int:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=directory_descriptor,
        )
    except FileNotFoundError:
        raise
    except OSError as error:
        raise ValueError(f"could not safely open updater file: {name}") from error
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_uid != os.geteuid():
            raise ValueError(f"updater file is not privately owned and regular: {name}")
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _create_temporary_file_at(directory_descriptor: int, destination_name: str) -> tuple[str, int]:
    for _attempt in range(128):
        name = f".{destination_name}.{secrets.token_hex(16)}.tmp"
        try:
            descriptor = os.open(
                name,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory_descriptor,
            )
        except FileExistsError:
            continue
        os.fchmod(descriptor, 0o600)
        return name, descriptor
    raise RuntimeError("could not create a unique updater temporary file")


def _sha256_from_descriptor(descriptor: int) -> str:
    os.lseek(descriptor, 0, os.SEEK_SET)
    with os.fdopen(os.dup(descriptor), "rb") as candidate_file:
        digest = hashlib.file_digest(candidate_file, "sha256").hexdigest()
    os.lseek(descriptor, 0, os.SEEK_SET)
    return digest


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _validate_record(paths: CachePaths, record: CandidateRecord) -> CandidateRecord:
    if not isinstance(record.version, str) or not record.version or "\n" in record.version:
        raise ValueError("candidate version must be a non-empty single-line string")
    if (
        not isinstance(record.sha256, str)
        or len(record.sha256) != 64
        or any(character not in _SHA256_PATTERN for character in record.sha256)
    ):
        raise ValueError("candidate SHA-256 must be 64 lowercase hexadecimal characters")
    if isinstance(record.source_date_epoch, bool) or not isinstance(record.source_date_epoch, int) or record.source_date_epoch < 0:
        raise ValueError("candidate source date epoch must be a non-negative integer")
    if not isinstance(record.allowed_members, tuple) or not record.allowed_members:
        raise ValueError("candidate allowed members must be a non-empty tuple")
    if any(not _is_safe_allowed_member(member) for member in record.allowed_members):
        raise ValueError("candidate allowed members must be relative non-empty paths")
    if not record.deb_path.is_absolute():
        raise ValueError("candidate Debian path must be absolute")

    if record.deb_path.parent != paths.download_dir:
        raise ValueError("candidate Debian path must be a regular file in the updater download directory")
    name = _leaf_name(record.deb_path)
    download_descriptor = _open_download_directory(paths)
    try:
        try:
            candidate_descriptor = _open_regular_file_at(download_descriptor, name)
        except FileNotFoundError as error:
            raise ValueError("candidate Debian path does not exist") from error
        with os.fdopen(candidate_descriptor, "rb") as candidate_file:
            digest = hashlib.file_digest(candidate_file, "sha256").hexdigest()
    finally:
        os.close(download_descriptor)
    if digest != record.sha256:
        raise ValueError("candidate Debian SHA-256 does not match the reviewed record")
    return CandidateRecord(
        version=record.version,
        sha256=record.sha256,
        source_date_epoch=record.source_date_epoch,
        deb_path=paths.download_dir / name,
        allowed_members=record.allowed_members,
    )


def _is_safe_allowed_member(member: object) -> bool:
    if not isinstance(member, str) or not member or member.startswith("/"):
        return False
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in member):
        return False
    return all(component not in {"", ".", ".."} for component in member.split("/"))


def _is_safe_single_line(value: object) -> bool:
    return isinstance(value, str) and bool(value) and not any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)


def _source_validator_payload(validator: SourceValidator) -> dict[str, str]:
    if not _is_safe_single_line(validator.url):
        raise ValueError("source validator URL must be a non-empty single-line string")
    payload: dict[str, str] = {}
    if validator.etag is not None:
        if not _is_safe_single_line(validator.etag):
            raise ValueError("source validator ETag must be a non-empty single-line string")
        payload["etag"] = validator.etag
    if validator.last_modified is not None:
        if not _is_safe_single_line(validator.last_modified):
            raise ValueError("source validator Last-Modified must be a non-empty single-line string")
        payload["last_modified"] = validator.last_modified
    if not payload:
        raise ValueError("source validator must contain an ETag or Last-Modified value")
    return payload


def _source_validator_from_payload(url: str, payload: object) -> SourceValidator | None:
    if not _is_safe_single_line(url) or not isinstance(payload, dict):
        return None
    etag = payload.get("etag")
    last_modified = payload.get("last_modified")
    if etag is not None and not _is_safe_single_line(etag):
        return None
    if last_modified is not None and not _is_safe_single_line(last_modified):
        return None
    if etag is None and last_modified is None:
        return None
    return SourceValidator(url=url, etag=etag, last_modified=last_modified)


@contextmanager
def cache_lock(paths: CachePaths) -> Iterator[int]:
    """Acquire the updater state lock or immediately raise BlockingIOError."""

    state_descriptor = _open_state_directory(paths)
    try:
        descriptor = os.open(
            "updater.lock",
            os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=state_descriptor,
        )
        try:
            lock_stat = os.fstat(descriptor)
            if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_uid != os.geteuid():
                raise ValueError("updater lock is not privately owned and regular")
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield state_descriptor
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
    finally:
        os.close(state_descriptor)


def read_record(paths: CachePaths) -> CandidateRecord | None:
    """Return the current reviewed candidate after validating its on-disk state."""

    state_descriptor = _open_state_directory(paths)
    try:
        try:
            descriptor = _open_regular_file_at(state_descriptor, "candidate.json")
        except FileNotFoundError:
            return None
        with os.fdopen(descriptor, "r", encoding="utf-8") as state_file:
            contents = state_file.read()
    finally:
        os.close(state_descriptor)
    try:
        state = json.loads(contents)
        if not isinstance(state, dict) or not isinstance(state["allowed_members"], list):
            raise TypeError("candidate record fields have invalid types")
        record = CandidateRecord(
            version=state["version"],
            sha256=state["sha256"],
            source_date_epoch=state["source_date_epoch"],
            deb_path=Path(state["deb_path"]),
            allowed_members=tuple(state["allowed_members"]),
        )
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("candidate record is malformed") from error
    return _validate_record(paths, record)


def write_record(paths: CachePaths, record: CandidateRecord) -> None:
    """Atomically replace the reviewed candidate record while holding the cache lock."""

    reviewed = _validate_record(paths, record)
    contents = json.dumps(
        {
            "version": reviewed.version,
            "sha256": reviewed.sha256,
            "source_date_epoch": reviewed.source_date_epoch,
            "deb_path": str(reviewed.deb_path),
            "allowed_members": list(reviewed.allowed_members),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    with cache_lock(paths) as state_descriptor:
        temporary_name, descriptor = _create_temporary_file_at(state_descriptor, "candidate.json")
        try:
            with os.fdopen(descriptor, "wb") as temporary_file:
                temporary_file.write(contents)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(
                temporary_name,
                "candidate.json",
                src_dir_fd=state_descriptor,
                dst_dir_fd=state_descriptor,
            )
            os.fsync(state_descriptor)
        finally:
            try:
                os.unlink(temporary_name, dir_fd=state_descriptor)
            except FileNotFoundError:
                pass


def _read_source_validator_state(state_descriptor: int) -> dict[str, object] | None:
    try:
        descriptor = _open_regular_file_at(state_descriptor, SOURCE_VALIDATORS_FILE)
    except FileNotFoundError:
        return {}
    except ValueError:
        return None
    with os.fdopen(descriptor, "r", encoding="utf-8") as state_file:
        contents = state_file.read()
    try:
        state = json.loads(contents)
    except json.JSONDecodeError:
        return None
    if not isinstance(state, dict):
        return None
    return state


def read_source_validator(paths: CachePaths, url: str) -> SourceValidator | None:
    """Return a stored URL validator, or ``None`` when validator state is unusable."""

    state_descriptor = _open_state_directory(paths)
    try:
        state = _read_source_validator_state(state_descriptor)
    finally:
        os.close(state_descriptor)
    if state is None:
        return None
    return _source_validator_from_payload(url, state.get(url))


def write_source_validator(paths: CachePaths, validator: SourceValidator) -> None:
    """Atomically store the URL validator independently from the candidate record."""

    payload = _source_validator_payload(validator)
    with cache_lock(paths) as state_descriptor:
        state = _read_source_validator_state(state_descriptor)
        if state is None:
            state = {}
        state[validator.url] = payload
        contents = json.dumps(state, sort_keys=True, separators=(",", ":")).encode("utf-8")
        temporary_name, descriptor = _create_temporary_file_at(state_descriptor, SOURCE_VALIDATORS_FILE)
        try:
            with os.fdopen(descriptor, "wb") as temporary_file:
                temporary_file.write(contents)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(
                temporary_name,
                SOURCE_VALIDATORS_FILE,
                src_dir_fd=state_descriptor,
                dst_dir_fd=state_descriptor,
            )
            os.fsync(state_descriptor)
        finally:
            try:
                os.unlink(temporary_name, dir_fd=state_descriptor)
            except FileNotFoundError:
                pass


def download_to_cache(
    url: str,
    destination: Path,
    opener: Callable[[str], object],
    *,
    reviewer: Callable[[Path], Mapping[str, object]] | None = None,
) -> Path:
    """Atomically promote a download only when its reviewed descriptor is unchanged."""

    if reviewer is None:
        raise ValueError("a candidate reviewer is required before downloading network bytes")
    destination_name = _leaf_name(destination)
    directory_descriptor = _open_download_destination_directory(destination)
    try:
        try:
            destination_stat = os.stat(destination_name, dir_fd=directory_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            destination_stat = None
        if destination_stat is not None and not stat.S_ISREG(destination_stat.st_mode):
            raise ValueError("download destination must be a regular file")
        temporary_name, descriptor = _create_temporary_file_at(directory_descriptor, destination_name)
        held_stat = os.fstat(descriptor)
        promoted = False
        try:
            with os.fdopen(os.dup(descriptor), "wb") as temporary_file:
                with closing(opener(url)) as response:
                    while chunk := response.read(1024 * 1024):
                        temporary_file.write(chunk)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            reviewed_sha256 = _sha256_from_descriptor(descriptor)
            review_path = Path(f"/proc/{os.getpid()}/fd/{descriptor}")
            try:
                review_stat = review_path.stat()
            except OSError as error:
                raise ValueError("Linux procfs FD review path is unavailable; refusing candidate promotion") from error
            if not stat.S_ISREG(review_stat.st_mode) or not _same_file(review_stat, os.fstat(descriptor)):
                raise ValueError("Linux procfs FD review path does not identify the downloaded candidate")
            report = reviewer(review_path)
            if not isinstance(report, Mapping) or report.get("sha256") != reviewed_sha256:
                raise ValueError("review report SHA-256 does not match the downloaded candidate")
            if _sha256_from_descriptor(descriptor) != reviewed_sha256:
                raise ValueError("candidate bytes changed while under review")
            named_stat = os.stat(temporary_name, dir_fd=directory_descriptor, follow_symlinks=False)
            if not stat.S_ISREG(named_stat.st_mode) or not _same_file(named_stat, held_stat):
                raise ValueError("candidate temporary name no longer identifies the reviewed file")
            os.replace(
                temporary_name,
                destination_name,
                src_dir_fd=directory_descriptor,
                dst_dir_fd=directory_descriptor,
            )
            canonical_descriptor = _open_regular_file_at(directory_descriptor, destination_name)
            try:
                canonical_stat = os.fstat(canonical_descriptor)
                canonical_sha256 = _sha256_from_descriptor(canonical_descriptor)
            finally:
                os.close(canonical_descriptor)
            if (
                not _same_file(canonical_stat, held_stat)
                or canonical_sha256 != reviewed_sha256
                or _sha256_from_descriptor(descriptor) != reviewed_sha256
            ):
                raise ValueError("canonical candidate no longer identifies the reviewed bytes after promotion")
            os.fsync(directory_descriptor)
            promoted = True
        finally:
            if not promoted:
                try:
                    named_stat = os.stat(temporary_name, dir_fd=directory_descriptor, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    if stat.S_ISREG(named_stat.st_mode) and _same_file(named_stat, held_stat):
                        os.unlink(temporary_name, dir_fd=directory_descriptor)
            os.close(descriptor)
    finally:
        os.close(directory_descriptor)
    return destination


def compare_versions(left: str, right: str, runner: Callable[..., subprocess.CompletedProcess[str]]) -> int:
    """Compare two Arch package versions with the system ``vercmp`` command."""

    result = runner(
        ["/usr/bin/vercmp", left, right],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ValueError(f"vercmp failed: {result.stderr.strip()}")
    output = result.stdout.strip()
    if output not in {"-1", "0", "1"}:
        raise ValueError(f"unexpected vercmp output: {output!r}")
    return int(output)
