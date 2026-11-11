"""Validate immutable cohort archive sidecars before backup reuse or restore.

This module never mutates files, and can be exercised without Google Drive.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

_HASH_RE = re.compile(r"\A([0-9a-fA-F]{64})\s+(.+)\Z")


def validate_archive_sidecars(
    archive: Path, cohort: int, *, require_complete: bool = True,
    expected_digest: str | None = None, require_backup: bool = False,
) -> dict:
    archive = Path(archive)
    if archive.is_symlink() or not archive.is_file():
        raise ValueError(f"Missing/unsafe archive: {archive}")
    h = hashlib.sha256()
    with archive.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    actual = h.hexdigest()
    if expected_digest is not None and expected_digest.lower() != actual:
        raise ValueError(f"Archive changed during validation: {archive.name}")

    sha_path = archive.with_name(archive.name + ".sha256")
    manifest_path = archive.with_name(archive.name.replace(".tar.gz", ".manifest.json"))

    if sha_path.exists() or sha_path.is_symlink():
        if sha_path.is_symlink() or not sha_path.is_file():
            raise ValueError("Unsafe archive checksum sidecar")
        match = _HASH_RE.fullmatch(sha_path.read_text(encoding="utf-8").strip())
        if match is None or match.group(1).lower() != actual or match.group(2).strip() != archive.name:
            raise ValueError(f"Original sha256 sidecar is corrupt or mismatched: Checksum mismatch for {archive.name}: Archive checksum sidecar mismatch: {archive.name}")

    if require_complete and (not sha_path.is_file() or not manifest_path.is_file()):
        raise ValueError(f"Missing required sidecars for {archive.name}")

    manifest = None
    if manifest_path.exists() or manifest_path.is_symlink():
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise ValueError("Unsafe archive accounting manifest")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Original manifest sidecar is corrupt JSON: Invalid accounting manifest: {archive.name}") from exc
        if not isinstance(manifest, dict):
            raise ValueError(f"Original manifest sidecar is not a JSON object: {archive.name}")
        if manifest.get("cohort_number") != cohort:
            raise ValueError(f"Original manifest sidecar cohort mismatch: Wrong cohort accounting manifest: {archive.name}")
        if manifest.get("archive_sha256") != actual:
            raise ValueError(f"Original manifest sidecar archive_sha256 mismatch: Accounting archive_sha256 mismatch: {archive.name}")
        if manifest.get("archive_filename") not in (None, archive.name):
            raise ValueError(f"Accounting archive_filename mismatch: {archive.name}")
        if require_backup and manifest.get("is_backup") is not True:
            raise ValueError(f"Backup manifest lacks is_backup: {archive.name}")
    return {"sha256": actual, "sha_path": sha_path, "manifest_path": manifest_path, "manifest": manifest}
