from __future__ import annotations

from dataclasses import dataclass
import io
from pathlib import Path
import posixpath
import re
import shutil
import subprocess
import tarfile
import tempfile


_ARCHIVE_MEMBER_PATTERN = re.compile(r"^(?:control|data)\.tar\.[A-Za-z0-9]+$")
_DISCARDED_MEMBER = "usr/share/lintian/overrides/chatgpt"
_DISCARDED_PARENT_DIRECTORIES = {
    "usr/share/lintian",
    "usr/share/lintian/overrides",
}
_LAUNCHER_PATH = "usr/bin/chatgpt"
_EXACT_REGULAR_FILES = {
    "usr/share/applications/chatgpt.desktop",
    "usr/share/doc/chatgpt/copyright",
    "usr/share/pixmaps/chatgpt.png",
    "etc/apparmor.d/chatgpt",
}
_DIRECTORY_ANCESTORS = {
    "usr",
    "usr/bin",
    "usr/lib",
    "usr/lib/chatgpt",
    "usr/share",
    "usr/share/applications",
    "usr/share/doc",
    "usr/share/doc/chatgpt",
    "usr/share/pixmaps",
    "etc",
    "etc/apparmor.d",
}


@dataclass(frozen=True)
class PayloadMember:
    path: str
    kind: str
    mode: int
    link_target: str | None
    mtime: int
    discarded: bool


def _ar_members(deb_path: Path) -> list[str]:
    result = subprocess.run(
        ["ar", "t", str(deb_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise ValueError(f"could not list Debian archive: {result.stderr.strip()}")
    return [member for member in result.stdout.splitlines() if member]


def _read_ar_member(deb_path: Path, member: str) -> bytes:
    result = subprocess.run(
        ["ar", "p", str(deb_path), member],
        check=False,
        capture_output=True,
    )
    if result.returncode:
        raise ValueError(f"could not read {member} from Debian archive: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout


def validate_debian_binary(deb_path: Path) -> None:
    members = _ar_members(deb_path)
    if members.count("debian-binary") != 1:
        raise ValueError("expected exactly one debian-binary archive member")
    if _read_ar_member(deb_path, "debian-binary") != b"2.0\n":
        raise ValueError("debian-binary must contain exactly 2.0\\n")


def _find_archive_member(deb_path: Path, prefix: str) -> str:
    candidates = [
        member
        for member in _ar_members(deb_path)
        if _ARCHIVE_MEMBER_PATTERN.fullmatch(member) and member.startswith(f"{prefix}.tar.")
    ]
    if len(candidates) != 1:
        raise ValueError(f"expected exactly one {prefix}.tar.* archive, found {len(candidates)}")
    return candidates[0]


def _normalise_path(name: str) -> str | None:
    if name.startswith("/"):
        raise ValueError(f"absolute archive path is not allowed: {name}")
    while name.startswith("./"):
        name = name[2:]
    if not name or name == ".":
        return None
    pieces = name.split("/")
    if any(piece in {"", ".", ".."} for piece in pieces):
        raise ValueError(f"unsafe archive path is not allowed: {name}")
    return "/".join(pieces)


def _kind(member: tarfile.TarInfo) -> str:
    if member.isreg():
        return "file"
    if member.isdir():
        return "directory"
    if member.issym():
        return "symlink"
    raise ValueError(f"unsupported archive member type for {member.name}")


def _validate_location(path: str, kind: str) -> bool | None:
    if path in _DISCARDED_PARENT_DIRECTORIES:
        if kind != "directory":
            raise ValueError(f"discarded Debian metadata parent is not a directory: {path}")
        return None
    if path == _DISCARDED_MEMBER:
        if kind != "file":
            raise ValueError(f"discarded Debian metadata is not a regular file: {path}")
        return True
    if path.startswith("usr/lib/chatgpt/"):
        return False
    if path == _LAUNCHER_PATH:
        if kind != "symlink":
            raise ValueError(f"launcher is not a symbolic link: {path}")
        return False
    if path in _EXACT_REGULAR_FILES:
        if kind != "file":
            raise ValueError(f"exact payload file is not regular: {path}")
        return False
    if path in _DIRECTORY_ANCESTORS and kind == "directory":
        return False
    raise ValueError(f"unallowlisted payload member: {path}")


def _validate_link(path: str, target: str) -> None:
    if target.startswith("/"):
        raise ValueError(f"absolute symbolic link target is not allowed: {path}")
    if path == "usr/bin/chatgpt":
        if target != "../lib/chatgpt/codex-launcher":
            raise ValueError("usr/bin/chatgpt must point to ../lib/chatgpt/codex-launcher")
        return
    if not path.startswith("usr/lib/chatgpt/"):
        raise ValueError(f"symbolic link is not permitted at {path}")
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
    if resolved == "usr/lib/chatgpt" or not resolved.startswith("usr/lib/chatgpt/"):
        raise ValueError(f"symbolic link escapes the ChatGPT payload: {path}")


def _inspect_payload_data(deb_path: Path) -> tuple[list[PayloadMember], bytes]:
    """Return fully validated metadata and the corresponding immutable data bytes."""

    validate_debian_binary(deb_path)
    data_member = _find_archive_member(deb_path, "data")
    payload = _read_ar_member(deb_path, data_member)
    try:
        archive = tarfile.open(fileobj=io.BytesIO(payload), mode="r:*")
    except (tarfile.ReadError, EOFError) as error:
        raise ValueError(f"could not read {data_member}") from error

    with archive:
        inspected: list[PayloadMember] = []
        seen_paths: set[str] = set()
        for member in archive.getmembers():
            if member.mode & 0o6000:
                raise ValueError(f"setuid or setgid payload member is not allowed: {member.name}")
            if any(key.endswith("security.capability") for key in member.pax_headers):
                raise ValueError(f"file capability payload member is not allowed: {member.name}")
            path = _normalise_path(member.name)
            if path is None:
                if not member.isdir():
                    raise ValueError("archive root entry must be a directory")
                if "." in seen_paths:
                    raise ValueError("duplicate payload member: .")
                seen_paths.add(".")
                continue
            if path in seen_paths:
                raise ValueError(f"duplicate payload member: {path}")
            seen_paths.add(path)
            kind = _kind(member)
            discarded = _validate_location(path, kind)
            if discarded is None:
                continue
            target = member.linkname if kind == "symlink" else None
            if target is not None:
                _validate_link(path, target)
            inspected.append(
                PayloadMember(
                    path=path,
                    kind=kind,
                    mode=member.mode,
                    link_target=target,
                    mtime=member.mtime,
                    discarded=discarded,
                )
            )
    return inspected, payload


def inspect_payload(deb_path: Path) -> list[PayloadMember]:
    """Return validated `data.tar.*` metadata without extracting the payload."""

    inspected, _payload = _inspect_payload_data(deb_path)
    return inspected


def stage_payload(deb_path: Path, destination: Path) -> list[PayloadMember]:
    """Safely stage the accepted data payload without running Debian package hooks."""

    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise ValueError(f"destination must be absent or an empty directory: {destination}")

    members, payload = _inspect_payload_data(deb_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging_directory = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent))
    try:
        result = subprocess.run(
            [
                "bsdtar",
                "--exclude",
                "./usr/share/lintian",
                "--exclude",
                "usr/share/lintian",
                "-xpf",
                "-",
                "-C",
                str(staging_directory),
                "--no-same-owner",
            ],
            input=payload,
            check=False,
            capture_output=True,
        )
        if result.returncode:
            raise ValueError(f"could not extract validated data payload: {result.stderr.decode(errors='replace').strip()}")
        if destination.exists():
            destination.rmdir()
        staging_directory.replace(destination)
    except Exception:
        shutil.rmtree(staging_directory, ignore_errors=True)
        raise
    return [member for member in members if not member.discarded]


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Safely stage the allowlisted ChatGPT Debian payload")
    parser.add_argument("--deb", type=Path, required=True, help="path to the checked Debian artifact")
    parser.add_argument("--destination", type=Path, required=True, help="empty or absent package destination")
    arguments = parser.parse_args()
    stage_payload(arguments.deb, arguments.destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
