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
    argv = ["makepkg"]
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


def _find_built_archive(pkgdest: Path, version: str, before: set[Path]) -> Path:
    candidates = [
        path
        for path in pkgdest.glob(f"{PACKAGE_NAME}-{version}-*-{PACKAGE_ARCHITECTURE}.pkg.tar*")
        if path.is_file() and path not in before and not path.name.endswith(".sig")
    ]
    if len(candidates) != 1:
        raise BuildUpdateError(f"expected exactly one built {PACKAGE_NAME} package archive, found {len(candidates)}")
    return candidates[0]


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
        before_packages = {path for path in paths.package_dir.glob("*.pkg.tar*") if path.is_file()}
        result = _run_makepkg(recipe_workspace, srcdest, paths.package_dir, makepkg_work, runner)
        if result.returncode:
            raise BuildUpdateError(f"makepkg failed: {_safe_line(result.stderr.strip() or result.stdout.strip())}")
        archive_path = _find_built_archive(paths.package_dir, reviewed.version, before_packages)
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


def _load_latest_build_report(paths: CachePaths) -> tuple[Path, Path]:
    reports = sorted(
        (path for path in paths.package_dir.glob(f"{PACKAGE_NAME}-*.build.json") if path.is_file() and not path.is_symlink()),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
        reverse=True,
    )
    if not reports:
        raise FileNotFoundError("no built ChatGPT package report is cached")
    report_path = reports[0]
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        package = report["package"]
        archive_path = Path(package["archive_path"])
        package_sha256 = package["sha256"]
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("cached build report is malformed") from error
    if package.get("name") != PACKAGE_NAME or package.get("architecture") != PACKAGE_ARCHITECTURE:
        raise ValueError("cached build report does not describe chatgpt-bin x86_64")
    if not isinstance(package_sha256, str) or package_sha256 not in report_path.name:
        raise ValueError("cached build report is not bound to the package digest")
    if not archive_path.is_absolute() or archive_path.parent != paths.package_dir:
        raise ValueError("cached build report archive path is outside the updater package cache")
    if not archive_path.is_file() or archive_path.is_symlink():
        raise FileNotFoundError("cached build report package archive is missing")
    return report_path, archive_path


def _install(
    paths: CachePaths,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> int:
    try:
        report_path, archive_path = _load_latest_build_report(paths)
    except FileNotFoundError as error:
        print(f"ChatGPT update install failed: {_safe_line(error)}")
        return 1
    except (OSError, ValueError) as error:
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
