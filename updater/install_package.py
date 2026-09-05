from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence


PACKAGE_NAME = "chatgpt-bin"
PACKAGE_ARCHITECTURE = "x86_64"
BSDTAR = "/usr/bin/bsdtar"
IMMUTABLE_RECIPE_DIR = Path("/usr/lib/chatgpt-bin/updater/recipe")
_SHA256_PATTERN = set("0123456789abcdef")
_PACKAGE_ARCHIVE_RE = re.compile(
    rf"^{re.escape(PACKAGE_NAME)}-(?P<version>[^/]+)-(?P<pkgrel>[^/]+)-{re.escape(PACKAGE_ARCHITECTURE)}"
    r"\.pkg\.tar(?:\.[A-Za-z0-9]+)?$"
)
_METADATA_PATHS = frozenset({".BUILDINFO", ".MTREE", ".PKGINFO"})
_GENERATED_PACKAGE_PATHS = frozenset(
    {
        "usr/bin/chatgpt-bin-check-update",
        "usr/bin/chatgpt-bin-update",
        "usr/lib/chatgpt-bin/updater/build_update.py",
        "usr/lib/chatgpt-bin/updater/check_update.py",
        "usr/lib/chatgpt-bin/updater/common.py",
        "usr/lib/chatgpt-bin/updater/deb_payload.py",
        "usr/lib/chatgpt-bin/updater/install_package.py",
        "usr/lib/chatgpt-bin/updater/review_source.py",
        "usr/lib/chatgpt-bin/updater/workflow_update.py",
        "usr/lib/chatgpt-bin/updater/recipe/.SRCINFO",
        "usr/lib/chatgpt-bin/updater/recipe/PKGBUILD",
        "usr/lib/chatgpt-bin/updater/recipe/scripts/deb_payload.py",
        "usr/lib/chatgpt-bin/updater/recipe/scripts/review_source.py",
        "usr/lib/chatgpt-bin/updater/recipe/scripts/update_source.py",
        "usr/lib/chatgpt-bin/updater/recipe/updater/build_update.py",
        "usr/lib/chatgpt-bin/updater/recipe/updater/chatgpt-bin-check-update",
        "usr/lib/chatgpt-bin/updater/recipe/updater/chatgpt-bin-install-package",
        "usr/lib/chatgpt-bin/updater/recipe/updater/chatgpt-bin-update",
        "usr/lib/chatgpt-bin/updater/recipe/updater/check_update.py",
        "usr/lib/chatgpt-bin/updater/recipe/updater/common.py",
        "usr/lib/chatgpt-bin/updater/recipe/updater/install_package.py",
        "usr/lib/chatgpt-bin/updater/recipe/updater/workflow_update.py",
        "usr/lib/chatgpt-bin/updater/recipe/updater/polkit/org.chatgpt-bin.install-package.policy",
        "usr/lib/chatgpt-bin/updater/recipe/updater/systemd/chatgpt-bin-update-check.service",
        "usr/lib/chatgpt-bin/updater/recipe/updater/systemd/chatgpt-bin-update-workflow.service",
        "usr/lib/chatgpt-bin/updater/recipe/updater/systemd/chatgpt-bin-update-check.timer",
        "usr/libexec/chatgpt-bin/install-package",
        "usr/lib/systemd/user/chatgpt-bin-update-check.service",
        "usr/lib/systemd/user/chatgpt-bin-update-workflow.service",
        "usr/lib/systemd/user/chatgpt-bin-update-check.timer",
        "usr/share/licenses/chatgpt-bin/copyright",
        "usr/share/polkit-1/actions/org.chatgpt-bin.install-package.policy",
    }
)


class InstallPackageError(Exception):
    pass


@dataclass(frozen=True)
class ExpectedPackage:
    version: str
    sha256: str
    candidate_sha256: str
    candidate_path: Path
    allowed_package_paths: frozenset[str]


def _source_archive_opened(_path: Path) -> None:
    """Instrumentation seam for the post-open same-file race test."""


def _safe_line(value: object) -> str:
    return str(value).replace("\r", " ").replace("\n", " ")


def _is_lower_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(character in _SHA256_PATTERN for character in value)


def _require_absolute_normal_path(value: str, label: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or any(component in {"", ".", ".."} for component in path.parts[1:]):
        raise InstallPackageError(f"{label} must be an absolute normal path")
    return path


def _parse_args(argv: Sequence[str]) -> tuple[Path, Path]:
    if len(argv) != 4:
        raise InstallPackageError("expected exactly --report REPORT --archive ARCHIVE")
    parsed: dict[str, str] = {}
    index = 0
    while index < len(argv):
        flag = argv[index]
        if flag not in {"--report", "--archive"}:
            raise InstallPackageError("expected exactly --report REPORT --archive ARCHIVE")
        if flag in parsed:
            raise InstallPackageError(f"duplicate argument: {flag}")
        parsed[flag] = argv[index + 1]
        index += 2
    if set(parsed) != {"--report", "--archive"}:
        raise InstallPackageError("expected exactly --report REPORT --archive ARCHIVE")
    return (
        _require_absolute_normal_path(parsed["--report"], "report"),
        _require_absolute_normal_path(parsed["--archive"], "archive"),
    )


def _pkexec_uid(environ: Mapping[str, str]) -> int:
    value = environ.get("PKEXEC_UID")
    if value is None or not value.isdecimal():
        raise InstallPackageError("PKEXEC_UID is required")
    return int(value)


def _open_regular_owned(path: Path, owner_uid: int, label: str) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise InstallPackageError(f"could not safely open {label}: {path}") from error
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise InstallPackageError(f"{label} must be a regular file")
        if file_stat.st_uid != owner_uid:
            raise InstallPackageError(f"{label} must be owned by PKEXEC_UID")
        return descriptor, file_stat
    except Exception:
        os.close(descriptor)
        raise


def _directory_stat_no_symlink_ancestors(path: Path, label: str) -> os.stat_result:
    if not path.is_absolute() or any(component in {"", ".", ".."} for component in path.parts[1:]):
        raise InstallPackageError(f"{label} must be an absolute normal path")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0))
    try:
        for component in path.parts[1:]:
            try:
                child_descriptor = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=descriptor,
                )
            except OSError as error:
                raise InstallPackageError(f"{label} contains an unsafe or missing directory: {component}") from error
            os.close(descriptor)
            descriptor = child_descriptor
        directory_stat = os.fstat(descriptor)
        if not stat.S_ISDIR(directory_stat.st_mode):
            raise InstallPackageError(f"{label} must be a directory")
        return directory_stat
    finally:
        os.close(descriptor)


def _expected_cache_download_dir(report_path: Path, archive_path: Path, owner_uid: int) -> Path:
    package_dir = report_path.parent
    cache_dir = package_dir.parent
    downloads_dir = cache_dir / "downloads"
    if archive_path.parent != package_dir:
        raise InstallPackageError("report and archive must be in the same updater package cache")
    if package_dir.name != "packages" or cache_dir.name != PACKAGE_NAME:
        raise InstallPackageError("report and archive must be in the updater package cache")
    for label, directory in (
        ("updater cache", cache_dir),
        ("updater package cache", package_dir),
        ("updater download cache", downloads_dir),
    ):
        directory_stat = _directory_stat_no_symlink_ancestors(directory, label)
        if directory_stat.st_uid != owner_uid:
            raise InstallPackageError(f"{label} must be owned by PKEXEC_UID")
    return downloads_dir


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _assert_path_still_identifies_descriptor(path: Path, descriptor_stat: os.stat_result) -> None:
    try:
        current = os.stat(path, follow_symlinks=False)
    except OSError as error:
        raise InstallPackageError("archive path changed after open") from error
    if not stat.S_ISREG(current.st_mode) or not _same_file(current, descriptor_stat):
        raise InstallPackageError("archive path changed after open")


def _sha256_from_fd(descriptor: int) -> str:
    os.lseek(descriptor, 0, os.SEEK_SET)
    with os.fdopen(os.dup(descriptor), "rb") as contents:
        digest = hashlib.file_digest(contents, "sha256").hexdigest()
    os.lseek(descriptor, 0, os.SEEK_SET)
    return digest


def _sha256(path: Path) -> str:
    with path.open("rb") as contents:
        return hashlib.file_digest(contents, "sha256").hexdigest()


def _load_report(descriptor: int) -> Mapping[str, object]:
    os.lseek(descriptor, 0, os.SEEK_SET)
    with os.fdopen(os.dup(descriptor), "r", encoding="utf-8") as contents:
        report = json.load(contents)
    if not isinstance(report, Mapping):
        raise InstallPackageError("build report must be a JSON object")
    return report


def _safe_package_path(path: object) -> str:
    if not isinstance(path, str) or not path or path.startswith("/"):
        raise InstallPackageError("package path must be relative")
    if path[0].isspace() or path[-1].isspace():
        raise InstallPackageError("package path must not contain leading or trailing whitespace")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in path):
        raise InstallPackageError("package path contains control characters")
    while path.startswith("./"):
        path = path[2:]
    if not path or path == ".":
        return "."
    components = path.rstrip("/").split("/")
    if any(component in {"", ".", ".."} for component in components):
        raise InstallPackageError(f"unsafe package path: {path}")
    return "/".join(components)


def _validated_path_set(paths: object) -> frozenset[str]:
    if not isinstance(paths, list) or not paths:
        raise InstallPackageError("allowed package paths must be a non-empty list")
    return frozenset(_safe_package_path(path) for path in paths)


def _hash_recipe(recipe_root: Path) -> str:
    if not recipe_root.is_dir() or recipe_root.is_symlink():
        raise InstallPackageError("immutable updater recipe is missing")
    digest = hashlib.sha256()
    for path in sorted(recipe_root.rglob("*")):
        relative = path.relative_to(recipe_root).as_posix()
        path_stat = path.lstat()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(stat.S_IMODE(path_stat.st_mode)).encode("ascii"))
        digest.update(b"\0")
        if stat.S_ISDIR(path_stat.st_mode) and not path.is_symlink():
            digest.update(b"dir\0")
            continue
        if stat.S_ISREG(path_stat.st_mode) and not path.is_symlink():
            digest.update(b"file\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
            continue
        raise InstallPackageError(f"immutable updater recipe contains an unsupported file: {relative}")
    return digest.hexdigest()


def _validate_recipe_binding(report: Mapping[str, object]) -> None:
    recipe = report.get("recipe")
    if not isinstance(recipe, Mapping):
        raise InstallPackageError("build report is missing recipe metadata")
    if recipe.get("source_path") != str(IMMUTABLE_RECIPE_DIR):
        raise InstallPackageError("build report recipe source path does not match the installed recipe")
    base_sha256 = recipe.get("base_sha256")
    if not _is_lower_sha256(base_sha256):
        raise InstallPackageError("build report recipe base SHA-256 is invalid")
    if recipe.get("sha256") is not None and not _is_lower_sha256(recipe.get("sha256")):
        raise InstallPackageError("build report recipe SHA-256 is invalid")
    if _hash_recipe(IMMUTABLE_RECIPE_DIR) != base_sha256:
        raise InstallPackageError("build report recipe base SHA-256 does not match the installed recipe")


def _validate_report(
    report: Mapping[str, object],
    report_path: Path,
    archive_path: Path,
    expected_download_dir: Path,
) -> ExpectedPackage:
    if report.get("schema_version") != 1:
        raise InstallPackageError("unsupported build report schema")
    package = report.get("package")
    candidate = report.get("candidate")
    if not isinstance(package, Mapping) or not isinstance(candidate, Mapping):
        raise InstallPackageError("build report is missing package or candidate metadata")

    version = package.get("version")
    package_sha256 = package.get("sha256")
    if package.get("name") != PACKAGE_NAME:
        raise InstallPackageError("build report package name does not match")
    if package.get("architecture") != PACKAGE_ARCHITECTURE:
        raise InstallPackageError("build report package architecture does not match")
    if not isinstance(version, str) or not version or "/" in version or "\n" in version:
        raise InstallPackageError("build report package version is invalid")
    if not _is_lower_sha256(package_sha256):
        raise InstallPackageError("build report package SHA-256 is invalid")
    if package.get("archive_path") != str(archive_path):
        raise InstallPackageError("build report archive path does not match")
    if report_path.name and package_sha256 not in report_path.name:
        raise InstallPackageError("build report file name is not bound to the package digest")

    if candidate.get("version") != version:
        raise InstallPackageError("build report candidate version does not match package version")
    candidate_sha256 = candidate.get("sha256")
    if not _is_lower_sha256(candidate_sha256):
        raise InstallPackageError("build report candidate SHA-256 is invalid")
    expected_candidate_path = expected_download_dir / f"chatgpt_amd64-{candidate_sha256}.deb"
    if candidate.get("deb_path") != str(expected_candidate_path):
        raise InstallPackageError("build report candidate path does not match the updater cache")
    allowed_package_paths = _validated_path_set(report.get("allowed_package_paths"))
    candidate_allowed = _validated_path_set(candidate.get("allowed_members"))
    if candidate_allowed != allowed_package_paths:
        raise InstallPackageError("build report candidate manifest does not match package paths")
    _validate_recipe_binding(report)
    return ExpectedPackage(
        version=version,
        sha256=package_sha256,
        candidate_sha256=candidate_sha256,
        candidate_path=expected_candidate_path,
        allowed_package_paths=allowed_package_paths,
    )


def _validate_archive_name(path: Path, version: str) -> None:
    match = _PACKAGE_ARCHIVE_RE.fullmatch(path.name)
    if match is None or match.group("version") != version:
        raise InstallPackageError("archive file name does not match the reviewed package")


def _copy_open_archive_to_staging(source_descriptor: int, staging_dir: Path, archive_name: str) -> Path:
    destination = staging_dir / archive_name
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    output_descriptor = os.open(destination, flags, 0o600)
    try:
        os.fchmod(output_descriptor, 0o600)
        os.lseek(source_descriptor, 0, os.SEEK_SET)
        while chunk := os.read(source_descriptor, 1024 * 1024):
            offset = 0
            while offset < len(chunk):
                offset += os.write(output_descriptor, chunk[offset:])
        os.fsync(output_descriptor)
    finally:
        os.close(output_descriptor)
    os.lseek(source_descriptor, 0, os.SEEK_SET)
    return destination


def _run_bsdtar(argv: list[str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(argv, check=False, capture_output=True, text=True)
    if result.returncode:
        raise InstallPackageError(f"bsdtar failed: {_safe_line(result.stderr.strip() or result.stdout.strip())}")
    return result


def _normalised_archive_paths(archive_path: Path) -> list[str]:
    result = _run_bsdtar([BSDTAR, "-tf", str(archive_path)])
    return [_safe_package_path(line) for line in result.stdout.splitlines() if line]


def _reject_unsupported_member_types(archive_path: Path) -> None:
    result = _run_bsdtar([BSDTAR, "-tvf", str(archive_path)])
    for line in result.stdout.splitlines():
        if not line:
            continue
        kind = line[0]
        if kind not in {"-", "d", "l"}:
            raise InstallPackageError("package archive contains an unsupported member type")


def _read_pkginfo(archive_path: Path) -> dict[str, str]:
    result = _run_bsdtar([BSDTAR, "-xOf", str(archive_path), ".PKGINFO"])
    fields: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if " = " not in line:
            continue
        key, value = line.split(" = ", 1)
        fields[key] = value
    return fields


def _ancestor_paths(path: str) -> set[str]:
    ancestors: set[str] = set()
    pieces = path.split("/")
    for index in range(1, len(pieces)):
        ancestors.add("/".join(pieces[:index]))
    return ancestors


def _allowed_archive_paths(expected: ExpectedPackage) -> set[str]:
    allowed = set(expected.allowed_package_paths) | set(_GENERATED_PACKAGE_PATHS) | set(_METADATA_PATHS)
    for path in tuple(allowed):
        allowed.update(_ancestor_paths(path))
    return allowed


def _validate_staged_archive(staged_path: Path, expected: ExpectedPackage) -> None:
    pkginfo = _read_pkginfo(staged_path)
    if pkginfo.get("pkgname") != PACKAGE_NAME:
        raise InstallPackageError("staged package name does not match")
    if pkginfo.get("arch") != PACKAGE_ARCHITECTURE:
        raise InstallPackageError("staged package architecture does not match")
    pkgver = pkginfo.get("pkgver", "")
    if pkgver.rsplit("-", 1)[0] != expected.version:
        raise InstallPackageError("staged package version does not match")

    _reject_unsupported_member_types(staged_path)
    allowed = _allowed_archive_paths(expected)
    for package_path in _normalised_archive_paths(staged_path):
        if package_path in {".", ""}:
            continue
        if package_path == ".INSTALL" or package_path.startswith("usr/share/libalpm/hooks/") or package_path.startswith(
            "etc/pacman.d/hooks/"
        ):
            raise InstallPackageError("package archive contains an install hook")
        if package_path not in allowed:
            raise InstallPackageError(f"package archive contains an unallowlisted path: {package_path}")


def _install(
    report_path: Path,
    archive_path: Path,
    pacman_runner: Callable[..., subprocess.CompletedProcess[str]],
) -> int:
    if os.geteuid() != 0:
        raise InstallPackageError("install helper must run as root")
    pkexec_uid = _pkexec_uid(os.environ)
    expected_download_dir = _expected_cache_download_dir(report_path, archive_path, pkexec_uid)
    report_descriptor, _report_stat = _open_regular_owned(report_path, pkexec_uid, "report")
    try:
        archive_descriptor, archive_stat = _open_regular_owned(archive_path, pkexec_uid, "archive")
        try:
            _source_archive_opened(archive_path)
            report = _load_report(report_descriptor)
            expected = _validate_report(report, report_path, archive_path, expected_download_dir)
            candidate_descriptor, _candidate_stat = _open_regular_owned(expected.candidate_path, pkexec_uid, "candidate")
            try:
                _validate_archive_name(archive_path, expected.version)
                if _sha256_from_fd(candidate_descriptor) != expected.candidate_sha256:
                    raise InstallPackageError("candidate SHA-256 does not match build report")
                if _sha256_from_fd(archive_descriptor) != expected.sha256:
                    raise InstallPackageError("archive SHA-256 does not match build report")
                _assert_path_still_identifies_descriptor(archive_path, archive_stat)

                staging_dir = Path(tempfile.mkdtemp(prefix="chatgpt-bin-install-"))
                try:
                    staging_dir.chmod(0o700)
                    staged_path = _copy_open_archive_to_staging(archive_descriptor, staging_dir, archive_path.name)
                    if _sha256(staged_path) != expected.sha256:
                        raise InstallPackageError("staged archive SHA-256 does not match build report")
                    _validate_staged_archive(staged_path, expected)
                    result = pacman_runner(["pacman", "-U", "--", str(staged_path)], check=False)
                    return int(result.returncode)
                finally:
                    shutil.rmtree(staging_dir, ignore_errors=True)
            finally:
                os.close(candidate_descriptor)
        finally:
            os.close(archive_descriptor)
    finally:
        os.close(report_descriptor)


def main(
    argv: Sequence[str] | None = None,
    *,
    pacman_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> int:
    try:
        report_path, archive_path = _parse_args(list(argv) if argv is not None else sys.argv[1:])
        return _install(report_path, archive_path, pacman_runner)
    except (InstallPackageError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"ChatGPT package install rejected: {_safe_line(error)}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
