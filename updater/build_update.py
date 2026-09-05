from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from .common import CandidateRecord, CachePaths, cache_lock, cache_paths, read_record
    from .common import _validate_record
except ImportError:  # Executed directly from the installed updater directory.
    from common import CandidateRecord, CachePaths, cache_lock, cache_paths, read_record
    from common import _validate_record

try:
    from scripts import review_source
except ImportError:  # Package-owned updater snapshot can place review_source.py next to this file.
    import review_source


INSTALLED_RECIPE_DIR = Path("/usr/lib/chatgpt-bin/updater/recipe")
PACKAGE_NAME = "chatgpt-bin"
PACKAGE_ARCHITECTURE = "x86_64"
DEBIAN_SOURCE_NAME = "chatgpt_amd64.deb"
REQUIRED_BUILD_TOOLS = ("makepkg", "fakeroot", "python", "bsdtar")
PROHIBITED_MAKEPKG_FLAGS = {"-s", "-i", "--syncdeps", "--install"}
INSTALL_HELPER = Path("/usr/libexec/chatgpt-bin/install-package")


class BuildUpdateError(Exception):
    pass


@dataclass(frozen=True)
class BuiltArtifact:
    archive_path: Path
    sha256: str
    version: str
    manifest_path: Path


def _safe_line(value: object) -> str:
    return str(value).replace("\r", " ").replace("\n", " ")


def _sha256(path: Path) -> str:
    with path.open("rb") as contents:
        return hashlib.file_digest(contents, "sha256").hexdigest()


def _is_lower_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _ensure_cache_layout(paths: CachePaths) -> None:
    expected = {
        "recipe workspace": paths.cache_dir / "recipe",
        "build workspace": paths.cache_dir / "work",
        "package output": paths.cache_dir / "packages",
    }
    actual = {
        "recipe workspace": paths.recipe_dir,
        "build workspace": paths.work_dir,
        "package output": paths.package_dir,
    }
    for label, expected_path in expected.items():
        if actual[label] != expected_path:
            raise ValueError(f"{label} must stay inside the updater cache root")
        expected_path.mkdir(mode=0o700, parents=True, exist_ok=True)
        expected_path.chmod(0o700)


def _require_build_tools() -> None:
    missing = [tool for tool in REQUIRED_BUILD_TOOLS if shutil.which(tool) is None]
    if missing:
        raise BuildUpdateError(f"missing build requirement: {', '.join(missing)}")


def _require_recipe(source: Path) -> None:
    if not source.is_dir():
        raise BuildUpdateError(f"installed updater recipe is missing: {source}")
    for relative in (Path("PKGBUILD"), Path(".SRCINFO"), Path("scripts/update_source.py")):
        candidate = source / relative
        if not candidate.is_file() or candidate.is_symlink():
            raise BuildUpdateError(f"installed updater recipe is incomplete: {relative}")


def _copy_recipe(source: Path, destination: Path) -> None:
    _require_recipe(source)
    for child in sorted(source.rglob("*")):
        relative = child.relative_to(source)
        if child.is_symlink():
            raise BuildUpdateError(f"installed updater recipe contains a symlink: {relative}")
        target = destination / relative
        if child.is_dir():
            target.mkdir(mode=0o700, parents=True, exist_ok=True)
            target.chmod(0o700)
        elif child.is_file():
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copy2(child, target, follow_symlinks=False)
        else:
            raise BuildUpdateError(f"installed updater recipe contains an unsupported file: {relative}")
    destination.chmod(0o700)


def _hash_recipe(recipe_root: Path) -> str:
    if not recipe_root.is_dir() or recipe_root.is_symlink():
        raise BuildUpdateError("installed updater recipe is missing")
    digest = hashlib.sha256()
    for path in sorted(recipe_root.rglob("*")):
        relative = path.relative_to(recipe_root).as_posix()
        path_stat = path.lstat()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(stat.S_IMODE(path_stat.st_mode)).encode("ascii"))
        digest.update(b"\0")
        if path.is_dir() and not path.is_symlink():
            digest.update(b"dir\0")
            continue
        if path.is_file() and not path.is_symlink():
            digest.update(b"file\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
            continue
        raise BuildUpdateError(f"recipe hash rejected unsupported file: {relative}")
    return digest.hexdigest()


def _copy_candidate_to_srcdest(record: CandidateRecord, srcdest: Path) -> Path:
    srcdest.mkdir(mode=0o700, parents=True, exist_ok=True)
    srcdest.chmod(0o700)
    destination = srcdest / DEBIAN_SOURCE_NAME
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        shutil.copy2(record.deb_path, temporary, follow_symlinks=False)
        if _sha256(temporary) != record.sha256:
            raise ValueError("candidate Debian SHA-256 changed while copying into the build workspace")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _review_again(candidate: Path, record: CandidateRecord) -> Mapping[str, object]:
    metadata = review_source.review_source(candidate)
    if metadata.get("sha256") != record.sha256:
        raise ValueError("candidate Debian SHA-256 does not match the reviewed record")
    if metadata.get("debian_version") != record.version:
        raise ValueError("candidate Debian version does not match the reviewed record")
    if metadata.get("source_date_epoch") != record.source_date_epoch:
        raise ValueError("candidate Debian source date epoch does not match the reviewed record")
    allowed_members = metadata.get("allowed_members")
    if not isinstance(allowed_members, list) or tuple(allowed_members) != record.allowed_members:
        raise ValueError("candidate Debian payload manifest does not match the reviewed record")
    return metadata


def _run_update_source(recipe_root: Path, candidate: Path, record: CandidateRecord) -> None:
    update_script = recipe_root / "scripts" / "update_source.py"
    result = subprocess.run(
        [
            "python",
            str(update_script),
            "--deb",
            str(candidate),
            "--repo-root",
            str(recipe_root),
            "--apply",
            "--expected-version",
            record.version,
            "--expected-sha256",
            record.sha256,
        ],
        cwd=recipe_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise BuildUpdateError(f"update_source.py failed: {_safe_line(result.stderr.strip() or result.stdout.strip())}")


def _run_makepkg(
    recipe_root: Path,
    srcdest: Path,
    pkgdest: Path,
    work_dir: Path,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> subprocess.CompletedProcess[str]:
    argv = ["makepkg", "-f"]
    if set(argv) & PROHIBITED_MAKEPKG_FLAGS:
        raise BuildUpdateError("internal makepkg invocation requested a prohibited install or sync flag")
    environment = os.environ.copy()
    environment.update(
        {
            "SRCDEST": str(srcdest),
            "PKGDEST": str(pkgdest),
            "BUILDDIR": str(work_dir),
        }
    )
    return runner(
        argv,
        cwd=recipe_root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


def _pkgbuild_assignment(recipe_root: Path, name: str) -> str:
    prefix = f"{name}="
    values: list[str] = []
    for line in (recipe_root / "PKGBUILD").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith(prefix):
            value = stripped.removeprefix(prefix).strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            values.append(value)
    if len(values) != 1:
        raise BuildUpdateError(f"expected exactly one {name} assignment in PKGBUILD")
    value = values[0]
    if not value or "/" in value or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise BuildUpdateError(f"PKGBUILD {name} assignment is invalid")
    return value


def _expected_archive_stem(recipe_root: Path, version: str) -> str:
    pkgrel = _pkgbuild_assignment(recipe_root, "pkgrel")
    return f"{PACKAGE_NAME}-{version}-{pkgrel}-{PACKAGE_ARCHITECTURE}.pkg.tar"


def _matching_expected_archives(pkgdest: Path, archive_stem: str) -> list[Path]:
    candidates: list[Path] = []
    for path in pkgdest.glob(f"{archive_stem}*"):
        if path.name.endswith(".sig"):
            continue
        if path.name == archive_stem or path.name.startswith(f"{archive_stem}."):
            candidates.append(path)
    return candidates


def _quarantine_preexisting_archive(pkgdest: Path, archive_stem: str, quarantine_dir: Path) -> None:
    matches = _matching_expected_archives(pkgdest, archive_stem)
    if len(matches) > 1:
        raise BuildUpdateError(f"expected at most one preexisting {PACKAGE_NAME} package archive, found {len(matches)}")
    if not matches:
        return
    archive_path = matches[0]
    try:
        archive_stat = archive_path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(archive_stat.st_mode) or archive_stat.st_uid != os.geteuid():
        raise BuildUpdateError("preexisting package archive is not a regular tool-owned file")
    quarantine_dir.mkdir(mode=0o700)
    quarantine_dir.chmod(0o700)
    quarantine_path = quarantine_dir / archive_path.name
    os.replace(archive_path, quarantine_path)
    try:
        archive_path.lstat()
    except FileNotFoundError:
        return
    raise BuildUpdateError("preexisting package archive could not be quarantined")


def _find_built_archive(pkgdest: Path, archive_stem: str) -> Path:
    matches = _matching_expected_archives(pkgdest, archive_stem)
    if len(matches) != 1:
        raise BuildUpdateError(f"expected exactly one freshly built {PACKAGE_NAME} package archive, found {len(matches)}")
    expected_archive = matches[0]
    archive_stat = expected_archive.lstat()
    if not stat.S_ISREG(archive_stat.st_mode) or archive_stat.st_uid != os.geteuid():
        raise BuildUpdateError("freshly built package archive is not a regular tool-owned file")
    return expected_archive


def _record_payload(record: CandidateRecord) -> dict[str, object]:
    payload = asdict(record)
    payload["deb_path"] = str(record.deb_path)
    payload["allowed_members"] = list(record.allowed_members)
    return payload


def _write_report(paths: CachePaths, report: Mapping[str, object], package_sha256: str, version: str) -> Path:
    paths.package_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    report_path = paths.package_dir / f"{PACKAGE_NAME}-{version}-{package_sha256}.build.json"
    contents = json.dumps(report, sort_keys=True, indent=2).encode("utf-8")
    with tempfile.NamedTemporaryFile(prefix=f".{report_path.name}.", suffix=".tmp", dir=paths.package_dir, delete=False) as temporary_file:
        temporary_path = Path(temporary_file.name)
        try:
            os.fchmod(temporary_file.fileno(), 0o600)
            temporary_file.write(contents)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
            os.replace(temporary_path, report_path)
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise
    return report_path


def build_candidate(
    record: CandidateRecord,
    paths: CachePaths,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> BuiltArtifact:
    """Build a reviewed candidate as the invoking user in a private cache workspace."""

    _ensure_cache_layout(paths)
    _require_build_tools()
    with cache_lock(paths):
        reviewed = _validate_record(paths, record)
        workspace_root = Path(tempfile.mkdtemp(prefix="build-", dir=paths.recipe_dir))
        workspace_root.chmod(0o700)
        recipe_workspace = workspace_root / "recipe"
        srcdest = workspace_root / "srcdest"
        makepkg_work = workspace_root / "makepkg-work"
        makepkg_work.mkdir(mode=0o700)
        paths.package_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        paths.package_dir.chmod(0o700)
        recipe_base_sha256 = _hash_recipe(INSTALLED_RECIPE_DIR)
        _copy_recipe(INSTALLED_RECIPE_DIR, recipe_workspace)
        candidate = _copy_candidate_to_srcdest(reviewed, srcdest)
        review_metadata = _review_again(candidate, reviewed)
        _run_update_source(recipe_workspace, candidate, reviewed)
        recipe_sha256 = _hash_recipe(recipe_workspace)
        expected_archive_stem = _expected_archive_stem(recipe_workspace, reviewed.version)
        _quarantine_preexisting_archive(paths.package_dir, expected_archive_stem, workspace_root / "quarantine")
        result = _run_makepkg(recipe_workspace, srcdest, paths.package_dir, makepkg_work, runner)
        if result.returncode:
            raise BuildUpdateError(f"makepkg failed: {_safe_line(result.stderr.strip() or result.stdout.strip())}")
        archive_path = _find_built_archive(paths.package_dir, expected_archive_stem)
        archive_sha256 = _sha256(archive_path)
        report = {
            "schema_version": 1,
            "workspace": str(recipe_workspace),
            "candidate": _record_payload(reviewed),
            "candidate_review": {
                "architecture": review_metadata.get("architecture"),
                "source_date_epoch": review_metadata.get("source_date_epoch"),
            },
            "recipe": {
                "source_path": str(INSTALLED_RECIPE_DIR),
                "workspace_path": str(recipe_workspace),
                "base_sha256": recipe_base_sha256,
                "sha256": recipe_sha256,
            },
            "package": {
                "name": PACKAGE_NAME,
                "version": reviewed.version,
                "architecture": PACKAGE_ARCHITECTURE,
                "archive_path": str(archive_path),
                "sha256": archive_sha256,
            },
            "allowed_package_paths": list(reviewed.allowed_members),
        }
        manifest_path = _write_report(paths, report, archive_sha256, reviewed.version)
        return BuiltArtifact(
            archive_path=archive_path,
            sha256=archive_sha256,
            version=reviewed.version,
            manifest_path=manifest_path,
        )


def _print_json(payload: Mapping[str, object]) -> None:
    print(json.dumps(payload, sort_keys=True, indent=2))


def _status(paths: CachePaths) -> int:
    record = read_record(paths)
    if record is None:
        _print_json({"candidate": None})
        return 1
    payload = _record_payload(record)
    _print_json({"candidate": payload})
    return 0


def _build(paths: CachePaths, runner: Callable[..., subprocess.CompletedProcess[str]]) -> int:
    record = read_record(paths)
    if record is None:
        print("ChatGPT update build failed: no reviewed candidate is cached")
        return 1
    try:
        artifact = build_candidate(record, paths, runner)
    except (BlockingIOError, BuildUpdateError, OSError, ValueError) as error:
        print(f"ChatGPT update build failed: {_safe_line(error)}")
        return 2
    _print_json(
        {
            "archive_path": str(artifact.archive_path),
            "manifest_path": str(artifact.manifest_path),
            "sha256": artifact.sha256,
            "version": artifact.version,
        }
    )
    return 0


def _read_build_report(report_path: Path) -> Mapping[str, object]:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if not isinstance(report, Mapping):
        raise ValueError("cached build report is malformed")
    return report


def _compatible_build_report(paths: CachePaths, report_path: Path, current_recipe_sha256: str) -> tuple[Path, Path] | None:
    try:
        report = _read_build_report(report_path)
        package = report.get("package")
        recipe = report.get("recipe")
        if not isinstance(package, Mapping) or not isinstance(recipe, Mapping):
            return None
        archive_path = Path(str(package.get("archive_path")))
        package_sha256 = package.get("sha256")
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if package.get("name") != PACKAGE_NAME or package.get("architecture") != PACKAGE_ARCHITECTURE:
        return None
    if not _is_lower_sha256(package_sha256) or package_sha256 not in report_path.name:
        return None
    if recipe.get("source_path") != str(INSTALLED_RECIPE_DIR):
        return None
    if recipe.get("base_sha256") != current_recipe_sha256:
        return None
    if not archive_path.is_absolute() or archive_path.parent != paths.package_dir:
        return None
    if not archive_path.is_file() or archive_path.is_symlink():
        return None
    try:
        if _sha256(archive_path) != package_sha256:
            return None
    except OSError:
        return None
    return report_path, archive_path


def _load_latest_build_report(paths: CachePaths) -> tuple[Path, Path]:
    reports = sorted(
        (path for path in paths.package_dir.glob(f"{PACKAGE_NAME}-*.build.json") if path.is_file() and not path.is_symlink()),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
        reverse=True,
    )
    if not reports:
        raise FileNotFoundError("no built ChatGPT package report is cached")
    current_recipe_sha256 = _hash_recipe(INSTALLED_RECIPE_DIR)
    for report_path in reports:
        compatible = _compatible_build_report(paths, report_path, current_recipe_sha256)
        if compatible is not None:
            return compatible
    raise FileNotFoundError("no-compatible-current-build: no cached build report matches the current installed recipe and package archive")


def _install(
    paths: CachePaths,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> int:
    try:
        report_path, archive_path = _load_latest_build_report(paths)
    except FileNotFoundError as error:
        print(f"ChatGPT update install failed: {_safe_line(error)}")
        return 1
    except (BuildUpdateError, OSError, ValueError) as error:
        print(f"ChatGPT update install failed: {_safe_line(error)}")
        return 2
    result = runner(
        [
            "pkexec",
            str(INSTALL_HELPER),
            "--report",
            str(report_path),
            "--archive",
            str(archive_path),
        ],
        check=False,
    )
    return int(result.returncode)


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a reviewed ChatGPT candidate from the user cache")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status", help="show the currently reviewed cached candidate")
    subparsers.add_parser("build", help="build the reviewed cached candidate as the invoking user")
    subparsers.add_parser("install", help="install a previously built artifact through the package helper")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parse_args(list(argv) if argv is not None else sys.argv[1:])
    paths = cache_paths(os.environ)
    if arguments.command == "status":
        return _status(paths)
    if arguments.command == "build":
        return _build(paths, subprocess.run)
    if arguments.command == "install":
        return _install(paths, subprocess.run)
    raise AssertionError(f"unhandled command: {arguments.command}")


if __name__ == "__main__":
    raise SystemExit(main())
