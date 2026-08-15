from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import tarfile

try:
    from .deb_payload import _find_archive_member, _read_ar_member, inspect_payload, validate_debian_binary
except ImportError:  # Executed directly as `python scripts/review_source.py`.
    from deb_payload import _find_archive_member, _read_ar_member, inspect_payload, validate_debian_binary


_REQUIRED_CONTROL_FIELDS = {"Package", "Version", "Architecture"}


def _parse_control(contents: bytes) -> dict[str, str]:
    try:
        lines = contents.decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise ValueError("Debian control file is not UTF-8") from error

    fields: dict[str, str] = {}
    current_field: str | None = None
    for line in lines:
        if not line:
            continue
        if line.startswith((" ", "\t")):
            if current_field is None:
                raise ValueError("control continuation has no field")
            fields[current_field] = f"{fields[current_field]}\n{line.lstrip()}"
            continue
        if ":" not in line:
            raise ValueError(f"malformed Debian control line: {line}")
        field, value = line.split(":", 1)
        if not field or field in fields:
            raise ValueError(f"duplicate or empty Debian control field: {field}")
        fields[field] = value.lstrip()
        current_field = field
    missing = sorted(field for field in _REQUIRED_CONTROL_FIELDS if not fields.get(field))
    if missing:
        raise ValueError(f"missing required Debian control fields: {', '.join(missing)}")
    return fields


def read_deb_control(deb_path: Path) -> dict[str, str]:
    """Read the control file from a `.deb` without unpacking it to disk."""

    validate_debian_binary(deb_path)
    control_member = _find_archive_member(deb_path, "control")
    payload = _read_ar_member(deb_path, control_member)
    try:
        archive = tarfile.open(fileobj=io.BytesIO(payload), mode="r:*")
    except (tarfile.ReadError, EOFError) as error:
        raise ValueError(f"could not read {control_member}") from error
    with archive:
        control_files = []
        for member in archive.getmembers():
            name_parts = member.name.split("/")
            if member.name.startswith("/") or ".." in name_parts:
                raise ValueError(f"unsafe control archive member: {member.name}")
            if member.name in {"control", "./control"} and member.isreg():
                control_files.append(member)
        if len(control_files) != 1:
            raise ValueError(f"expected exactly one regular control file, found {len(control_files)}")
        extracted = archive.extractfile(control_files[0])
        if extracted is None:
            raise ValueError("could not read Debian control file")
        return _parse_control(extracted.read())


def review_source(deb_path: Path) -> dict[str, object]:
    """Return deterministic local review metadata for a validated ChatGPT `.deb`."""

    control = read_deb_control(deb_path)
    if control["Package"] != "chatgpt":
        raise ValueError(f"unexpected Debian Package: {control['Package']}")
    if control["Architecture"] != "amd64":
        raise ValueError(f"unexpected Debian Architecture: {control['Architecture']}")
    members = inspect_payload(deb_path)
    accepted = sorted(member.path for member in members if not member.discarded)
    source_date_epoch = max(member.mtime for member in members if not member.discarded)
    return {
        "sha256": hashlib.sha256(deb_path.read_bytes()).hexdigest(),
        "debian_version": control["Version"],
        "architecture": control["Architecture"],
        "allowed_members": accepted,
        "source_date_epoch": source_date_epoch,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Review a local ChatGPT Debian package without extracting it")
    parser.add_argument("--deb", type=Path, required=True, help="path to a local .deb artifact")
    parser.add_argument("--format", choices=("json",), default="json", help="output format")
    arguments = parser.parse_args()
    print(json.dumps(review_source(arguments.deb), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
