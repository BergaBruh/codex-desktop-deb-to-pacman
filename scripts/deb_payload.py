from __future__ import annotations

from dataclasses import dataclass
import io
from pathlib import Path
import posixpath
import re
import subprocess
import tarfile


_ARCHIVE_MEMBER_PATTERN = re.compile(r"^(?:control|data)\.tar\.[A-Za-z0-9]+$")
_DISCARDED_MEMBER = "usr/share/lintian/overrides/chatgpt"
_EXACT_FILES = {
    "usr/bin/chatgpt",
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


def _find_archive_member(deb_path: Path, prefix: str) -> str:
    candidates = [
        member
        for member in _ar_members(deb_path)
        if _ARCHIVE_MEMBER_PATTERN.fullmatch(member) and member.startswith(f"{prefix}.tar.")
    ]
    if len(candidates) != 1:
        raise ValueError(f"expected exactly one {prefix}.tar.* archive, found {len(candidates)}")
    return candidates[0]


def _normalise_path(name: str) -> str:
    if name.startswith("/"):
        raise ValueError(f"absolute archive path is not allowed: {name}")
    while name.startswith("./"):
        name = name[2:]
    if not name or name == ".":
        raise ValueError("empty archive path is not allowed")
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


def _validate_location(path: str, kind: str) -> bool:
    if path == _DISCARDED_MEMBER:
        if kind != "file":
            raise ValueError(f"discarded Debian metadata is not a regular file: {path}")
        return True
    if path.startswith("usr/lib/chatgpt/"):
        return False
    if path in _EXACT_FILES:
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


def inspect_payload(deb_path: Path) -> list[PayloadMember]:
    """Return validated `data.tar.*` metadata without extracting the payload."""

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
            path = _normalise_path(member.name)
            if path in seen_paths:
                raise ValueError(f"duplicate payload member: {path}")
            seen_paths.add(path)
            if member.mode & 0o6000:
                raise ValueError(f"setuid or setgid payload member is not allowed: {path}")
            if any(key.endswith("security.capability") for key in member.pax_headers):
                raise ValueError(f"file capability payload member is not allowed: {path}")
            kind = _kind(member)
            discarded = _validate_location(path, kind)
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
    return inspected
