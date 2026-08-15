from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile

try:
    from .review_source import review_source
except ImportError:  # Executed directly as `python scripts/update_source.py`.
    from review_source import review_source


_ASSIGNMENT_PATTERNS = {
    "pkgver": re.compile(r"^pkgver=.*$", re.MULTILINE),
    "pkgrel": re.compile(r"^pkgrel=.*$", re.MULTILINE),
    "sha256sums": re.compile(r"^sha256sums=.*$", re.MULTILINE),
    "SOURCE_DATE_EPOCH": re.compile(r"^SOURCE_DATE_EPOCH=.*$", re.MULTILINE),
}


def _replace_assignments(contents: str, metadata: dict[str, object]) -> str:
    replacements = {
        "pkgver": f"pkgver={metadata['debian_version']}",
        "pkgrel": "pkgrel=1",
        "sha256sums": f"sha256sums=('{metadata['sha256']}')",
        "SOURCE_DATE_EPOCH": f"SOURCE_DATE_EPOCH={metadata['source_date_epoch']}",
    }
    updated = contents
    for name, pattern in _ASSIGNMENT_PATTERNS.items():
        if len(pattern.findall(updated)) != 1:
            raise ValueError(f"expected exactly one editable {name} assignment in PKGBUILD")
        updated = pattern.sub(replacements[name], updated, count=1)
    return updated


def _atomic_write(path: Path, contents: bytes) -> None:
    target_stat = path.stat()
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary_file:
        temporary_stat = os.fstat(temporary_file.fileno())
        os.fchmod(temporary_file.fileno(), stat.S_IMODE(target_stat.st_mode))
        if (temporary_stat.st_uid, temporary_stat.st_gid) != (target_stat.st_uid, target_stat.st_gid):
            os.fchown(temporary_file.fileno(), target_stat.st_uid, target_stat.st_gid)
        temporary_file.write(contents)
        temporary_file.flush()
        os.fsync(temporary_file.fileno())
        temporary_path = Path(temporary_file.name)
    os.replace(temporary_path, path)


def update_source(
    deb_path: Path,
    repo_root: Path,
    *,
    apply: bool = False,
    expected_version: str | None = None,
    expected_sha256: str | None = None,
) -> dict[str, object]:
    """Review a local artifact and optionally apply its explicitly approved metadata."""

    metadata = review_source(deb_path)
    if not apply:
        return metadata
    if not expected_version or not expected_sha256:
        raise ValueError("expected version and expected SHA-256 are required with --apply")
    if expected_version != metadata["debian_version"]:
        raise ValueError("expected version does not match the reviewed artifact")
    if expected_sha256 != metadata["sha256"]:
        raise ValueError("expected SHA-256 does not match the reviewed artifact")

    pkgbuild_path = repo_root / "PKGBUILD"
    srcinfo_path = repo_root / ".SRCINFO"
    try:
        original_pkgbuild = pkgbuild_path.read_text(encoding="utf-8")
        srcinfo_path.read_bytes()
    except FileNotFoundError as error:
        raise ValueError(f"required repository file is missing: {error.filename}") from error
    updated_pkgbuild = _replace_assignments(original_pkgbuild, metadata)

    _atomic_write(pkgbuild_path, updated_pkgbuild.encode("utf-8"))
    try:
        result = subprocess.run(
            ["makepkg", "--printsrcinfo"],
            cwd=repo_root,
            check=False,
            capture_output=True,
        )
        if result.returncode:
            raise ValueError(f"makepkg --printsrcinfo failed: {result.stderr.decode(errors='replace').strip()}")
        _atomic_write(srcinfo_path, result.stdout)
    except Exception:
        _atomic_write(pkgbuild_path, original_pkgbuild.encode("utf-8"))
        raise
    return metadata


def main() -> int:
    parser = argparse.ArgumentParser(description="Review or guardedly update local ChatGPT package metadata")
    parser.add_argument("--deb", type=Path, required=True, help="path to a local reviewed .deb artifact")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd(), help="repository containing PKGBUILD")
    parser.add_argument("--apply", action="store_true", help="rewrite reviewed metadata after exact-value checks")
    parser.add_argument("--expected-version", help="required Debian Version when --apply is used")
    parser.add_argument("--expected-sha256", help="required lowercase SHA-256 when --apply is used")
    arguments = parser.parse_args()
    metadata = update_source(
        arguments.deb,
        arguments.repo_root,
        apply=arguments.apply,
        expected_version=arguments.expected_version,
        expected_sha256=arguments.expected_sha256,
    )
    print(json.dumps(metadata, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
