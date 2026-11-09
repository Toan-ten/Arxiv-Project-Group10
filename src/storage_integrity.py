"""Storage Contract and Data Integrity Audit API for Milestone 1.

Provides unified, robust cross-file consistency verification, source file and checksum
auditing, schema validation, and lifecycle invariant enforcement across corpus papers.
Adheres strictly to the M1 v2 Storage Contract (docs/m1_contract.md and src/storage.py).
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from src.id_ranges import ConfigError, arxiv_id_to_folder_key, load_config
from src.storage import (
    CONTRACT_VERSION,
    DISCOVERY_STATES,
    IntegrityError,
    PAPER_STATES,
    SOURCE_ERRORS,
    STAGES,
    StorageError,
    TERMINAL,
    VERSION_STATES,
    _relative,
    _valid_date,
    _version,
)


@dataclass
class IntegrityViolation:
    """Represents a specific contract violation or integrity error."""
    arxiv_id: str
    category: str  # "missing", "corrupted", "invalid", "incomplete", "unmanaged", "stale"
    severity: str  # "error", "warning"
    file_path: Optional[str] = None
    version: Optional[str] = None
    expected: Any = None
    actual: Any = None
    message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass
class PaperIntegrityResult:
    """Comprehensive integrity audit assessment for a single paper."""
    arxiv_id: str
    mssv: str
    status: str  # "valid", "invalid", "missing", "incomplete", "corrupted"
    is_source_complete: bool
    is_m1_complete: bool
    manifest_status: Optional[str] = None
    metadata_status: Optional[str] = None
    references_status: Optional[str] = None
    discovery_status: Optional[str] = None
    version_status: Optional[str] = None
    versions: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    violations: List[IntegrityViolation] = field(default_factory=list)
    warnings: List[IntegrityViolation] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return len(self.violations) == 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "arxiv_id": self.arxiv_id,
            "mssv": self.mssv,
            "status": self.status,
            "is_valid": self.is_valid,
            "is_source_complete": self.is_source_complete,
            "is_m1_complete": self.is_m1_complete,
            "manifest_status": self.manifest_status,
            "metadata_status": self.metadata_status,
            "references_status": self.references_status,
            "discovery_status": self.discovery_status,
            "version_status": self.version_status,
            "versions": self.versions,
            "violations": [v.to_dict() for v in self.violations],
            "warnings": [w.to_dict() for w in self.warnings],
        }


@dataclass
class CohortIntegrityReport:
    """Aggregated integrity report across an entire cohort or set of papers."""
    cohort_number: Optional[int]
    total_candidates: int
    valid_count: int
    invalid_count: int
    missing_count: int
    incomplete_count: int
    corrupted_count: int
    source_complete_count: int
    m1_complete_count: int
    papers: Dict[str, PaperIntegrityResult] = field(default_factory=dict)
    violations_by_category: Dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cohort_number": self.cohort_number,
            "total_candidates": self.total_candidates,
            "valid_count": self.valid_count,
            "invalid_count": self.invalid_count,
            "missing_count": self.missing_count,
            "incomplete_count": self.incomplete_count,
            "corrupted_count": self.corrupted_count,
            "source_complete_count": self.source_complete_count,
            "m1_complete_count": self.m1_complete_count,
            "violations_by_category": self.violations_by_category,
            "papers": {aid: res.to_dict() for aid, res in self.papers.items()},
        }


SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


def _compute_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def audit_version_integrity(
    paper_dir: Path,
    arxiv_id: str,
    version_name: str,
    version_record: Dict[str, Any],
) -> List[IntegrityViolation]:
    """Verify filesystem source files, checksums, and invariants for a single version."""
    violations: List[IntegrityViolation] = []
    expected_versioned_id = arxiv_id + version_name
    actual_versioned_id = version_record.get("versioned_id")

    if actual_versioned_id != expected_versioned_id:
        violations.append(
            IntegrityViolation(
                arxiv_id=arxiv_id,
                category="invalid",
                severity="error",
                version=version_name,
                expected=expected_versioned_id,
                actual=actual_versioned_id,
                message=f"Version ID mismatch in manifest: expected {expected_versioned_id}, got {actual_versioned_id}",
            )
        )

    st = version_record.get("status")
    if st not in VERSION_STATES:
        violations.append(
            IntegrityViolation(
                arxiv_id=arxiv_id,
                category="invalid",
                severity="error",
                version=version_name,
                expected=list(VERSION_STATES),
                actual=st,
                message=f"Unknown version status '{st}'",
            )
        )
        return violations

    version_dir = paper_dir / version_name

    # Reject version_dir if it is a symlink or resolves outside paper directory (R5)
    if version_dir.is_symlink() or os.path.islink(version_dir):
        violations.append(
            IntegrityViolation(
                arxiv_id=arxiv_id,
                category="corrupted",
                severity="error",
                file_path=str(version_dir),
                version=version_name,
                message=f"Version directory {version_name} is a symbolic link",
            )
        )
        return violations

    try:
        if version_dir.exists() and not version_dir.resolve().is_relative_to(paper_dir.resolve()):
            violations.append(
                IntegrityViolation(
                    arxiv_id=arxiv_id,
                    category="corrupted",
                    severity="error",
                    file_path=str(version_dir),
                    version=version_name,
                    message=f"Version directory {version_name} resolves outside paper directory",
                )
            )
            return violations
    except OSError as exc:
        violations.append(
            IntegrityViolation(
                arxiv_id=arxiv_id,
                category="corrupted",
                severity="error",
                file_path=str(version_dir),
                version=version_name,
                message=f"Filesystem error resolving version directory {version_name}: {exc}",
            )
        )
        return violations

    src_st = version_record.get("source_status")

    if st == "success":
        if src_st != "success":
            violations.append(
                IntegrityViolation(
                    arxiv_id=arxiv_id,
                    category="invalid",
                    severity="error",
                    version=version_name,
                    expected="success",
                    actual=src_st,
                    message="source_status must match status 'success'",
                )
            )

        if not version_dir.exists():
            violations.append(
                IntegrityViolation(
                    arxiv_id=arxiv_id,
                    category="missing",
                    severity="error",
                    file_path=str(version_dir),
                    version=version_name,
                    expected="directory",
                    actual="missing",
                    message=f"Source directory {version_name}/ is missing for status 'success'",
                )
            )
            return violations

        if not version_dir.is_dir():
            violations.append(
                IntegrityViolation(
                    arxiv_id=arxiv_id,
                    category="corrupted",
                    severity="error",
                    file_path=str(version_dir),
                    version=version_name,
                    expected="directory",
                    actual="file",
                    message=f"Source path {version_name} is not a directory",
                )
            )
            return violations

        # Verify files and checksum metadata in manifest (R3)
        raw_files = version_record.get("files")
        if not isinstance(raw_files, list) or not raw_files:
            violations.append(
                IntegrityViolation(
                    arxiv_id=arxiv_id,
                    category="invalid",
                    severity="error",
                    version=version_name,
                    message="Version marked 'success' must have non-empty files list in manifest",
                )
            )
            expected_files = set()
        else:
            expected_files = set(raw_files)

        raw_checksums = version_record.get("file_checksums")
        if not isinstance(raw_checksums, dict):
            violations.append(
                IntegrityViolation(
                    arxiv_id=arxiv_id,
                    category="invalid",
                    severity="error",
                    version=version_name,
                    message="file_checksums must be a dictionary in manifest",
                )
            )
            expected_checksums = {}
        else:
            expected_checksums = raw_checksums

        # Validate checksum format for each entry (R3)
        for fname, sha in expected_checksums.items():
            if not isinstance(sha, str) or not SHA256_HEX_RE.match(sha):
                violations.append(
                    IntegrityViolation(
                        arxiv_id=arxiv_id,
                        category="invalid",
                        severity="error",
                        file_path=str(version_dir / str(fname)),
                        version=version_name,
                        expected="64-character lowercase hex string",
                        actual=sha,
                        message=f"Malformed SHA-256 checksum for {fname}: '{sha}'",
                    )
                )

        # 1:1 match between expected files and checksum keys (R3)
        missing_checksums = expected_files - set(expected_checksums.keys())
        if missing_checksums:
            violations.append(
                IntegrityViolation(
                    arxiv_id=arxiv_id,
                    category="incomplete",
                    severity="error",
                    version=version_name,
                    expected="SHA-256 checksum for every file in files list",
                    actual=sorted(missing_checksums),
                    message=f"Files missing checksum entries in manifest: {sorted(missing_checksums)}",
                )
            )

        unexpected_checksums = set(expected_checksums.keys()) - expected_files
        if unexpected_checksums:
            violations.append(
                IntegrityViolation(
                    arxiv_id=arxiv_id,
                    category="invalid",
                    severity="error",
                    version=version_name,
                    expected="checksums only for files in files list",
                    actual=sorted(unexpected_checksums),
                    message=f"Unexpected checksum entries in manifest not in files list: {sorted(unexpected_checksums)}",
                )
            )

        actual_files = set()
        actual_checksums = {}
        total_bytes = 0
        has_tex = False

        # Non-following directory traversal (R5)
        try:
            for root, dirs, files in os.walk(version_dir, followlinks=False):
                root_path = Path(root)
                if root_path.is_symlink() or os.path.islink(root_path):
                    violations.append(
                        IntegrityViolation(
                            arxiv_id=arxiv_id,
                            category="corrupted",
                            severity="error",
                            file_path=str(root_path),
                            version=version_name,
                            message=f"Directory {root_path} is a symbolic link",
                        )
                    )
                    dirs.clear()
                    continue

                # Prune and report symlinked subdirectories (do not enter)
                symlink_dirs = [d for d in dirs if (root_path / d).is_symlink() or os.path.islink(root_path / d)]
                for d in symlink_dirs:
                    violations.append(
                        IntegrityViolation(
                            arxiv_id=arxiv_id,
                            category="corrupted",
                            severity="error",
                            file_path=str(root_path / d),
                            version=version_name,
                            message=f"Subdirectory {d} is a symbolic link",
                        )
                    )
                    dirs.remove(d)

                for fname in sorted(files):
                    fpath = root_path / fname
                    try:
                        rel_posix = fpath.relative_to(version_dir).as_posix()
                    except ValueError:
                        violations.append(
                            IntegrityViolation(
                                arxiv_id=arxiv_id,
                                category="corrupted",
                                severity="error",
                                file_path=str(fpath),
                                version=version_name,
                                message=f"Path {fpath} outside version dir",
                            )
                        )
                        continue

                    try:
                        lstat_res = os.lstat(fpath)
                    except OSError as exc:
                        violations.append(
                            IntegrityViolation(
                                arxiv_id=arxiv_id,
                                category="corrupted",
                                severity="error",
                                file_path=str(fpath),
                                version=version_name,
                                message=f"Filesystem error inspecting {rel_posix}: {exc}",
                            )
                        )
                        continue

                    # Reject symlink files immediately (do not open or hash) (R5)
                    if stat.S_ISLNK(lstat_res.st_mode) or fpath.is_symlink() or os.path.islink(fpath):
                        violations.append(
                            IntegrityViolation(
                                arxiv_id=arxiv_id,
                                category="corrupted",
                                severity="error",
                                file_path=str(fpath),
                                version=version_name,
                                message=f"Source file {rel_posix} is a symbolic link",
                            )
                        )
                        actual_files.add(rel_posix)
                        continue

                    if not stat.S_ISREG(lstat_res.st_mode):
                        violations.append(
                            IntegrityViolation(
                                arxiv_id=arxiv_id,
                                category="corrupted",
                                severity="error",
                                file_path=str(fpath),
                                version=version_name,
                                message=f"Source file {rel_posix} is not a regular file",
                            )
                        )
                        actual_files.add(rel_posix)
                        continue

                    # Reject multi-link hardlinks (R5)
                    if lstat_res.st_nlink != 1:
                        violations.append(
                            IntegrityViolation(
                                arxiv_id=arxiv_id,
                                category="corrupted",
                                severity="error",
                                file_path=str(fpath),
                                version=version_name,
                                message=f"Source file {rel_posix} has multiple hard links ({lstat_res.st_nlink})",
                            )
                        )

                    # Containment check within version_dir (R5)
                    try:
                        if not fpath.resolve().is_relative_to(version_dir.resolve()):
                            violations.append(
                                IntegrityViolation(
                                    arxiv_id=arxiv_id,
                                    category="corrupted",
                                    severity="error",
                                    file_path=str(fpath),
                                    version=version_name,
                                    message=f"Source file {rel_posix} resolves outside version directory",
                                )
                            )
                            continue
                    except OSError as exc:
                        violations.append(
                            IntegrityViolation(
                                arxiv_id=arxiv_id,
                                category="corrupted",
                                severity="error",
                                file_path=str(fpath),
                                version=version_name,
                                message=f"Filesystem error resolving {rel_posix}: {exc}",
                            )
                        )
                        continue

                    suffix = fpath.suffix.lower()
                    if suffix not in {".tex", ".bib"}:
                        violations.append(
                            IntegrityViolation(
                                arxiv_id=arxiv_id,
                                category="invalid",
                                severity="error",
                                file_path=str(fpath),
                                version=version_name,
                                expected=[".tex", ".bib"],
                                actual=suffix,
                                message=f"Forbidden source file extension '{suffix}' in {rel_posix}",
                            )
                        )

                    if suffix == ".tex":
                        has_tex = True

                    try:
                        sha = _compute_sha256(fpath)
                    except OSError as exc:
                        violations.append(
                            IntegrityViolation(
                                arxiv_id=arxiv_id,
                                category="corrupted",
                                severity="error",
                                file_path=str(fpath),
                                version=version_name,
                                message=f"Filesystem error reading/hashing {rel_posix}: {exc}",
                            )
                        )
                        actual_files.add(rel_posix)
                        continue

                    actual_files.add(rel_posix)
                    actual_checksums[rel_posix] = sha
                    total_bytes += lstat_res.st_size
        except OSError as exc:
            violations.append(
                IntegrityViolation(
                    arxiv_id=arxiv_id,
                    category="corrupted",
                    severity="error",
                    file_path=str(version_dir),
                    version=version_name,
                    message=f"Filesystem error traversing {version_name}/: {exc}",
                )
            )

        if not has_tex:
            violations.append(
                IntegrityViolation(
                    arxiv_id=arxiv_id,
                    category="invalid",
                    severity="error",
                    version=version_name,
                    expected="at least one .tex file",
                    actual="no .tex files found",
                    message="Version marked 'success' must contain at least one .tex file",
                )
            )

        # Check missing files on disk (R3)
        missing_files = expected_files - actual_files
        if missing_files:
            violations.append(
                IntegrityViolation(
                    arxiv_id=arxiv_id,
                    category="missing",
                    severity="error",
                    version=version_name,
                    expected=sorted(missing_files),
                    actual="not on disk",
                    message=f"Expected files missing from {version_name}/: {sorted(missing_files)}",
                )
            )

        # Check unmanaged extra files on disk (R3)
        extra_files = actual_files - expected_files
        if extra_files:
            violations.append(
                IntegrityViolation(
                    arxiv_id=arxiv_id,
                    category="unmanaged",
                    severity="error",
                    version=version_name,
                    expected="files recorded in manifest",
                    actual=sorted(extra_files),
                    message=f"Unmanaged files found in {version_name}/: {sorted(extra_files)}",
                )
            )

        # Check checksums for present expected files (R3)
        for fname in sorted(expected_files & actual_files):
            if fname in expected_checksums and fname in actual_checksums:
                exp_sha = expected_checksums[fname]
                act_sha = actual_checksums[fname]
                if act_sha != exp_sha:
                    violations.append(
                        IntegrityViolation(
                            arxiv_id=arxiv_id,
                            category="corrupted",
                            severity="error",
                            file_path=str(version_dir / fname),
                            version=version_name,
                            expected=exp_sha,
                            actual=act_sha,
                            message=f"SHA-256 checksum mismatch for {version_name}/{fname}",
                        )
                    )

        # Check kept_bytes (R3)
        exp_kept = version_record.get("kept_bytes")
        if exp_kept is not None and exp_kept != total_bytes:
            violations.append(
                IntegrityViolation(
                    arxiv_id=arxiv_id,
                    category="corrupted",
                    severity="error",
                    version=version_name,
                    expected=exp_kept,
                    actual=total_bytes,
                    message=f"Kept bytes mismatch for {version_name}: expected {exp_kept}, got {total_bytes}",
                )
            )

        if not version_record.get("completed_at"):
            violations.append(
                IntegrityViolation(
                    arxiv_id=arxiv_id,
                    category="incomplete",
                    severity="error",
                    version=version_name,
                    expected="ISO-8601 timestamp",
                    actual=None,
                    message=f"Missing completed_at timestamp for terminal version {version_name}",
                )
            )

    elif st == "no_tex_source":
        # Fully validate no_tex_source (R4)
        raw_files = version_record.get("files") or []
        expected_files = set(raw_files)

        if not expected_files:
            # Empty no_tex_source: directory vN/ must not exist on disk
            if version_dir.exists():
                violations.append(
                    IntegrityViolation(
                        arxiv_id=arxiv_id,
                        category="unmanaged",
                        severity="error",
                        file_path=str(version_dir),
                        version=version_name,
                        expected="directory should not exist for empty no_tex_source",
                        actual="directory exists",
                        message=f"Unexpected version directory {version_name}/ for empty no_tex_source",
                    )
                )
            raw_checksums = version_record.get("file_checksums")
            if raw_checksums:
                violations.append(
                    IntegrityViolation(
                        arxiv_id=arxiv_id,
                        category="invalid",
                        severity="error",
                        version=version_name,
                        message="Empty no_tex_source must not have file_checksums",
                    )
                )
            exp_kept = version_record.get("kept_bytes")
            if exp_kept not in (0, None):
                violations.append(
                    IntegrityViolation(
                        arxiv_id=arxiv_id,
                        category="invalid",
                        severity="error",
                        version=version_name,
                        message=f"Empty no_tex_source must have kept_bytes=0, got {exp_kept}",
                    )
                )
        else:
            # Non-empty no_tex_source (bib-only): version directory must exist and be a directory
            if not version_dir.exists():
                violations.append(
                    IntegrityViolation(
                        arxiv_id=arxiv_id,
                        category="missing",
                        severity="error",
                        file_path=str(version_dir),
                        version=version_name,
                        message=f"Source directory {version_name}/ missing for bib-only no_tex_source",
                    )
                )
                return violations

            if not version_dir.is_dir():
                violations.append(
                    IntegrityViolation(
                        arxiv_id=arxiv_id,
                        category="corrupted",
                        severity="error",
                        file_path=str(version_dir),
                        version=version_name,
                        message=f"Source path {version_name} is not a directory",
                    )
                )
                return violations

            # All files in manifest must strictly be .bib files
            for f in expected_files:
                if Path(f).suffix.lower() != ".bib":
                    violations.append(
                        IntegrityViolation(
                            arxiv_id=arxiv_id,
                            category="invalid",
                            severity="error",
                            version=version_name,
                            expected=".bib file",
                            actual=Path(f).suffix.lower(),
                            message=f"no_tex_source cannot contain non-.bib file '{f}' in files list",
                        )
                    )

            raw_checksums = version_record.get("file_checksums")
            if not isinstance(raw_checksums, dict):
                violations.append(
                    IntegrityViolation(
                        arxiv_id=arxiv_id,
                        category="invalid",
                        severity="error",
                        version=version_name,
                        message="file_checksums must be a dictionary in manifest",
                    )
                )
                expected_checksums = {}
            else:
                expected_checksums = raw_checksums

            # Validate checksum format
            for fname, sha in expected_checksums.items():
                if not isinstance(sha, str) or not SHA256_HEX_RE.match(sha):
                    violations.append(
                        IntegrityViolation(
                            arxiv_id=arxiv_id,
                            category="invalid",
                            severity="error",
                            file_path=str(version_dir / str(fname)),
                            version=version_name,
                            expected="64-character lowercase hex string",
                            actual=sha,
                            message=f"Malformed SHA-256 checksum for {fname}: '{sha}'",
                        )
                    )

            missing_checksums = expected_files - set(expected_checksums.keys())
            if missing_checksums:
                violations.append(
                    IntegrityViolation(
                        arxiv_id=arxiv_id,
                        category="incomplete",
                        severity="error",
                        version=version_name,
                        expected="SHA-256 checksum for every .bib file in files list",
                        actual=sorted(missing_checksums),
                        message=f"Files missing checksum entries in manifest: {sorted(missing_checksums)}",
                    )
                )

            unexpected_checksums = set(expected_checksums.keys()) - expected_files
            if unexpected_checksums:
                violations.append(
                    IntegrityViolation(
                        arxiv_id=arxiv_id,
                        category="invalid",
                        severity="error",
                        version=version_name,
                        expected="checksums only for files in files list",
                        actual=sorted(unexpected_checksums),
                        message=f"Unexpected checksum entries in manifest not in files list: {sorted(unexpected_checksums)}",
                    )
                )

            actual_files = set()
            actual_checksums = {}
            total_bytes = 0

            # Non-following traversal of version_dir (R4, R5)
            try:
                for root, dirs, files in os.walk(version_dir, followlinks=False):
                    root_path = Path(root)
                    if root_path.is_symlink() or os.path.islink(root_path):
                        violations.append(
                            IntegrityViolation(
                                arxiv_id=arxiv_id,
                                category="corrupted",
                                severity="error",
                                file_path=str(root_path),
                                version=version_name,
                                message=f"Directory {root_path} is a symbolic link",
                            )
                        )
                        dirs.clear()
                        continue

                    symlink_dirs = [d for d in dirs if (root_path / d).is_symlink() or os.path.islink(root_path / d)]
                    for d in symlink_dirs:
                        violations.append(
                            IntegrityViolation(
                                arxiv_id=arxiv_id,
                                category="corrupted",
                                severity="error",
                                file_path=str(root_path / d),
                                version=version_name,
                                message=f"Subdirectory {d} is a symbolic link",
                            )
                        )
                        dirs.remove(d)

                    for fname in sorted(files):
                        fpath = root_path / fname
                        try:
                            rel_posix = fpath.relative_to(version_dir).as_posix()
                        except ValueError:
                            violations.append(
                                IntegrityViolation(
                                    arxiv_id=arxiv_id,
                                    category="corrupted",
                                    severity="error",
                                    file_path=str(fpath),
                                    version=version_name,
                                    message=f"Path {fpath} outside version dir",
                                )
                            )
                            continue

                        try:
                            lstat_res = os.lstat(fpath)
                        except OSError as exc:
                            violations.append(
                                IntegrityViolation(
                                    arxiv_id=arxiv_id,
                                    category="corrupted",
                                    severity="error",
                                    file_path=str(fpath),
                                    version=version_name,
                                    message=f"Filesystem error inspecting {rel_posix}: {exc}",
                                )
                            )
                            continue

                        if stat.S_ISLNK(lstat_res.st_mode) or fpath.is_symlink() or os.path.islink(fpath):
                            violations.append(
                                IntegrityViolation(
                                    arxiv_id=arxiv_id,
                                    category="corrupted",
                                    severity="error",
                                    file_path=str(fpath),
                                    version=version_name,
                                    message=f"Source file {rel_posix} is a symbolic link",
                                )
                            )
                            actual_files.add(rel_posix)
                            continue

                        if not stat.S_ISREG(lstat_res.st_mode):
                            violations.append(
                                IntegrityViolation(
                                    arxiv_id=arxiv_id,
                                    category="corrupted",
                                    severity="error",
                                    file_path=str(fpath),
                                    version=version_name,
                                    message=f"Source file {rel_posix} is not a regular file",
                                )
                            )
                            actual_files.add(rel_posix)
                            continue

                        if lstat_res.st_nlink != 1:
                            violations.append(
                                IntegrityViolation(
                                    arxiv_id=arxiv_id,
                                    category="corrupted",
                                    severity="error",
                                    file_path=str(fpath),
                                    version=version_name,
                                    message=f"Source file {rel_posix} has multiple hard links ({lstat_res.st_nlink})",
                                )
                            )

                        try:
                            if not fpath.resolve().is_relative_to(version_dir.resolve()):
                                violations.append(
                                    IntegrityViolation(
                                        arxiv_id=arxiv_id,
                                        category="corrupted",
                                        severity="error",
                                        file_path=str(fpath),
                                        version=version_name,
                                        message=f"Source file {rel_posix} resolves outside version directory",
                                    )
                                )
                                continue
                        except OSError as exc:
                            violations.append(
                                IntegrityViolation(
                                    arxiv_id=arxiv_id,
                                    category="corrupted",
                                    severity="error",
                                    file_path=str(fpath),
                                    version=version_name,
                                    message=f"Filesystem error resolving {rel_posix}: {exc}",
                                )
                            )
                            continue

                        suffix = fpath.suffix.lower()
                        if suffix != ".bib":
                            violations.append(
                                IntegrityViolation(
                                    arxiv_id=arxiv_id,
                                    category="invalid",
                                    severity="error",
                                    file_path=str(fpath),
                                    version=version_name,
                                    expected=[".bib"],
                                    actual=suffix,
                                    message=f"no_tex_source cannot contain non-.bib file '{rel_posix}' (found '{suffix}')",
                                )
                            )

                        try:
                            sha = _compute_sha256(fpath)
                        except OSError as exc:
                            violations.append(
                                IntegrityViolation(
                                    arxiv_id=arxiv_id,
                                    category="corrupted",
                                    severity="error",
                                    file_path=str(fpath),
                                    version=version_name,
                                    message=f"Filesystem error reading/hashing {rel_posix}: {exc}",
                                )
                            )
                            actual_files.add(rel_posix)
                            continue

                        actual_files.add(rel_posix)
                        actual_checksums[rel_posix] = sha
                        total_bytes += lstat_res.st_size
            except OSError as exc:
                violations.append(
                    IntegrityViolation(
                        arxiv_id=arxiv_id,
                        category="corrupted",
                        severity="error",
                        file_path=str(version_dir),
                        version=version_name,
                        message=f"Filesystem error traversing {version_name}/: {exc}",
                    )
                )

            missing_files = expected_files - actual_files
            if missing_files:
                violations.append(
                    IntegrityViolation(
                        arxiv_id=arxiv_id,
                        category="missing",
                        severity="error",
                        version=version_name,
                        expected=sorted(missing_files),
                        actual="not on disk",
                        message=f"Expected bib files missing from {version_name}/: {sorted(missing_files)}",
                    )
                )

            extra_files = actual_files - expected_files
            if extra_files:
                violations.append(
                    IntegrityViolation(
                        arxiv_id=arxiv_id,
                        category="unmanaged",
                        severity="error",
                        version=version_name,
                        expected="files recorded in manifest",
                        actual=sorted(extra_files),
                        message=f"Unmanaged files found in {version_name}/: {sorted(extra_files)}",
                    )
                )

            for fname in sorted(expected_files & actual_files):
                if fname in expected_checksums and fname in actual_checksums:
                    exp_sha = expected_checksums[fname]
                    act_sha = actual_checksums[fname]
                    if act_sha != exp_sha:
                        violations.append(
                            IntegrityViolation(
                                arxiv_id=arxiv_id,
                                category="corrupted",
                                severity="error",
                                file_path=str(version_dir / fname),
                                version=version_name,
                                expected=exp_sha,
                                actual=act_sha,
                                message=f"SHA-256 checksum mismatch for {version_name}/{fname}",
                            )
                        )

            exp_kept = version_record.get("kept_bytes")
            if exp_kept is not None and exp_kept != total_bytes:
                violations.append(
                    IntegrityViolation(
                        arxiv_id=arxiv_id,
                        category="corrupted",
                        severity="error",
                        version=version_name,
                        expected=exp_kept,
                        actual=total_bytes,
                        message=f"Kept bytes mismatch for {version_name}: expected {exp_kept}, got {total_bytes}",
                    )
                )

            if not version_record.get("completed_at"):
                violations.append(
                    IntegrityViolation(
                        arxiv_id=arxiv_id,
                        category="incomplete",
                        severity="error",
                        version=version_name,
                        expected="ISO-8601 timestamp",
                        actual=None,
                        message=f"Missing completed_at timestamp for terminal version {version_name}",
                    )
                )

    else:
        # Non-terminal version: version directory should NOT exist on disk
        if version_dir.exists():
            violations.append(
                IntegrityViolation(
                    arxiv_id=arxiv_id,
                    category="corrupted",
                    severity="error",
                    file_path=str(version_dir),
                    version=version_name,
                    expected="no directory for non-terminal version",
                    actual="directory exists",
                    message=f"Uncommitted or orphaned source directory {version_name}/ exists while status is '{st}'",
                )
            )

    return violations


def audit_paper_integrity(
    store: Any,
    mssv: str,
    arxiv_id: str,
) -> PaperIntegrityResult:
    """Perform exhaustive Storage Contract and cross-file integrity verification on a paper."""
    violations: List[IntegrityViolation] = []
    warnings: List[IntegrityViolation] = []

    try:
        folder_key = arxiv_id_to_folder_key(arxiv_id)
    except ConfigError as exc:
        violations.append(
            IntegrityViolation(
                arxiv_id=arxiv_id,
                category="invalid",
                severity="error",
                message=f"Invalid assigned arXiv ID format: {exc}",
            )
        )
        return PaperIntegrityResult(
            arxiv_id=arxiv_id,
            mssv=mssv,
            status="invalid",
            is_source_complete=False,
            is_m1_complete=False,
            violations=violations,
        )

    paper_dir = store.root / folder_key

    # 1. Paper directory presence
    if not paper_dir.exists():
        violations.append(
            IntegrityViolation(
                arxiv_id=arxiv_id,
                category="missing",
                severity="error",
                file_path=str(paper_dir),
                message="Paper directory does not exist in corpus root",
            )
        )
        return PaperIntegrityResult(
            arxiv_id=arxiv_id,
            mssv=mssv,
            status="missing",
            is_source_complete=False,
            is_m1_complete=False,
            violations=violations,
        )

    # 2. Path safety check (symlinks, junctions)
    try:
        store._safe(paper_dir)
    except (StorageError, IntegrityError) as exc:
        violations.append(
            IntegrityViolation(
                arxiv_id=arxiv_id,
                category="invalid",
                severity="error",
                file_path=str(paper_dir),
                message=f"Path safety violation: {exc}",
            )
        )
        return PaperIntegrityResult(
            arxiv_id=arxiv_id,
            mssv=mssv,
            status="invalid",
            is_source_complete=False,
            is_m1_complete=False,
            violations=violations,
        )

    # 3. Read and validate core JSON checkpoints
    manifest: Optional[Dict[str, Any]] = None
    metadata: Optional[Dict[str, Any]] = None
    references: Optional[Dict[str, Any]] = None

    manifest_path = paper_dir / "manifest.json"
    metadata_path = paper_dir / "metadata.json"
    references_path = paper_dir / "references.json"

    # Validate manifest.json
    if not manifest_path.is_file():
        violations.append(
            IntegrityViolation(
                arxiv_id=arxiv_id,
                category="missing",
                severity="error",
                file_path=str(manifest_path),
                message="manifest.json does not exist",
            )
        )
    else:
        try:
            manifest = store.read_manifest(mssv, arxiv_id)
        except IntegrityError as exc:
            violations.append(
                IntegrityViolation(
                    arxiv_id=arxiv_id,
                    category="corrupted",
                    severity="error",
                    file_path=str(manifest_path),
                    message=f"manifest.json corrupted or invalid checkpoint: {exc}",
                )
            )
        except StorageError as exc:
            violations.append(
                IntegrityViolation(
                    arxiv_id=arxiv_id,
                    category="invalid",
                    severity="error",
                    file_path=str(manifest_path),
                    message=f"manifest.json schema violation: {exc}",
                )
            )
        except Exception as exc:
            violations.append(
                IntegrityViolation(
                    arxiv_id=arxiv_id,
                    category="corrupted",
                    severity="error",
                    file_path=str(manifest_path),
                    message=f"manifest.json unparseable JSON: {exc}",
                )
            )

    # Validate metadata.json
    if not metadata_path.is_file():
        violations.append(
            IntegrityViolation(
                arxiv_id=arxiv_id,
                category="missing",
                severity="error",
                file_path=str(metadata_path),
                message="metadata.json does not exist",
            )
        )
    else:
        try:
            metadata = store.read_metadata(mssv, arxiv_id)
        except (IntegrityError, StorageError) as exc:
            violations.append(
                IntegrityViolation(
                    arxiv_id=arxiv_id,
                    category="invalid",
                    severity="error",
                    file_path=str(metadata_path),
                    message=f"metadata.json validation failed: {exc}",
                )
            )
        except Exception as exc:
            violations.append(
                IntegrityViolation(
                    arxiv_id=arxiv_id,
                    category="corrupted",
                    severity="error",
                    file_path=str(metadata_path),
                    message=f"metadata.json unparseable JSON: {exc}",
                )
            )

    # Validate references.json
    if not references_path.is_file():
        violations.append(
            IntegrityViolation(
                arxiv_id=arxiv_id,
                category="missing",
                severity="error",
                file_path=str(references_path),
                message="references.json does not exist",
            )
        )
    else:
        try:
            references = store.read_references(mssv, arxiv_id)
        except (IntegrityError, StorageError) as exc:
            violations.append(
                IntegrityViolation(
                    arxiv_id=arxiv_id,
                    category="invalid",
                    severity="error",
                    file_path=str(references_path),
                    message=f"references.json validation failed: {exc}",
                )
            )
        except Exception as exc:
            violations.append(
                IntegrityViolation(
                    arxiv_id=arxiv_id,
                    category="corrupted",
                    severity="error",
                    file_path=str(references_path),
                    message=f"references.json unparseable JSON: {exc}",
                )
            )

    # If any mandatory JSON failed to load, determine initial failure status
    if manifest is None or metadata is None or references is None:
        has_corrupt = any(v.category == "corrupted" for v in violations)
        has_missing = any(v.category == "missing" for v in violations)
        overall_status = "corrupted" if has_corrupt else ("missing" if has_missing else "invalid")
        return PaperIntegrityResult(
            arxiv_id=arxiv_id,
            mssv=mssv,
            status=overall_status,
            is_source_complete=False,
            is_m1_complete=False,
            violations=violations,
            warnings=warnings,
        )

    # 4. Cross-file consistency checks between manifest, metadata, references
    man_status = manifest.get("status")
    disc_status = manifest.get("discovery_status")
    ver_status = manifest.get("version_status")
    meta_status = metadata.get("status")
    ref_status = references.get("status")

    if manifest.get("metadata_status") != meta_status:
        violations.append(
            IntegrityViolation(
                arxiv_id=arxiv_id,
                category="invalid",
                severity="error",
                expected=meta_status,
                actual=manifest.get("metadata_status"),
                message="manifest.metadata_status disagrees with metadata.status",
            )
        )

    if manifest.get("references_status") != ref_status:
        violations.append(
            IntegrityViolation(
                arxiv_id=arxiv_id,
                category="invalid",
                severity="error",
                expected=ref_status,
                actual=manifest.get("references_status"),
                message="manifest.references_status disagrees with references.status",
            )
        )

    if ver_status != metadata.get("version_status"):
        violations.append(
            IntegrityViolation(
                arxiv_id=arxiv_id,
                category="invalid",
                severity="error",
                expected=metadata.get("version_status"),
                actual=ver_status,
                message="manifest.version_status disagrees with metadata.version_status",
            )
        )

    # Check version coverage synchronization
    manifest_versions = manifest.get("versions") or {}
    metadata_versions = [v.get("version") for v in (metadata.get("versions") or []) if isinstance(v, dict)]

    if set(manifest_versions.keys()) != set(metadata_versions):
        violations.append(
            IntegrityViolation(
                arxiv_id=arxiv_id,
                category="invalid",
                severity="error",
                expected=sorted(metadata_versions),
                actual=sorted(manifest_versions.keys()),
                message="Version keys in manifest do not match version list in metadata",
            )
        )

    # 5. References coverage semantics (distinguish null vs empty vs complete)
    ref_coverage = references.get("coverage") or {}
    cov_complete = ref_coverage.get("complete") is True
    ref_items = references.get("items") or []

    if ref_status == "complete":
        if not cov_complete:
            violations.append(
                IntegrityViolation(
                    arxiv_id=arxiv_id,
                    category="invalid",
                    severity="error",
                    expected=True,
                    actual=ref_coverage.get("complete"),
                    message="references marked 'complete' but coverage.complete is False",
                )
            )
        if ref_coverage.get("continuation") is not None:
            violations.append(
                IntegrityViolation(
                    arxiv_id=arxiv_id,
                    category="invalid",
                    severity="error",
                    expected=None,
                    actual=ref_coverage.get("continuation"),
                    message="references marked 'complete' but continuation token is not None",
                )
            )

    # 6. Audit source files for each version
    all_versions_terminal = bool(manifest_versions)
    all_versions_verified = bool(manifest_versions)

    for v_name, v_rec in manifest_versions.items():
        v_violations = audit_version_integrity(paper_dir, arxiv_id, v_name, v_rec)
        violations.extend(v_violations)
        st = v_rec.get("status")
        if st not in TERMINAL:
            all_versions_terminal = False
            all_versions_verified = False
        elif len(v_violations) > 0:
            all_versions_verified = False

    # 7. Scan paper directory for extraneous / unmanaged files
    allowed_paper_entries = {"manifest.json", "metadata.json", "references.json"}
    for v_name, v_rec in manifest_versions.items():
        if v_rec.get("status") == "success" or (v_rec.get("status") == "no_tex_source" and v_rec.get("files")):
            allowed_paper_entries.add(v_name)

    for child in paper_dir.iterdir():
        if child.is_symlink() or os.path.islink(child):
            violations.append(
                IntegrityViolation(
                    arxiv_id=arxiv_id,
                    category="corrupted",
                    severity="error",
                    file_path=str(child),
                    message=f"Symbolic link found in paper directory: {child.name}",
                )
            )
        elif child.name not in allowed_paper_entries:
            violations.append(
                IntegrityViolation(
                    arxiv_id=arxiv_id,
                    category="unmanaged",
                    severity="error",
                    file_path=str(child),
                    expected="managed files or terminal version folders",
                    actual=child.name,
                    message=f"Unmanaged extraneous file or folder in paper directory: {child.name}",
                )
            )

    # 8. Compute completeness flags
    is_source_complete = (
        disc_status == "found"
        and ver_status == "resolved"
        and all_versions_terminal
        and all_versions_verified
        and len(manifest_versions) > 0
    )

    is_m1_complete = (
        is_source_complete
        and meta_status == "complete"
        and ref_status == "complete"
        and cov_complete
        and man_status in {"complete", "complete_with_no_tex"}
        and len(violations) == 0
    )

    # 9. Determine overall paper integrity classification
    if disc_status == "not_found":
        overall_status = "valid" if len(violations) == 0 else "invalid"
    elif any(v.category == "corrupted" for v in violations):
        overall_status = "corrupted"
    elif any(v.category in {"invalid", "unmanaged"} for v in violations):
        overall_status = "invalid"
    elif not is_source_complete or man_status in {"not_attempted", "in_progress", "partial", "interrupted", "failed"}:
        overall_status = "incomplete"
    else:
        overall_status = "valid"

    return PaperIntegrityResult(
        arxiv_id=arxiv_id,
        mssv=mssv,
        status=overall_status,
        is_source_complete=is_source_complete,
        is_m1_complete=is_m1_complete,
        manifest_status=man_status,
        metadata_status=meta_status,
        references_status=ref_status,
        discovery_status=disc_status,
        version_status=ver_status,
        versions=manifest_versions,
        violations=violations,
        warnings=warnings,
    )


def audit_cohort_integrity(
    store: Any,
    pairs: Iterable[Tuple[str, str]],
    cohort_number: Optional[int] = None,
) -> CohortIntegrityReport:
    """Audit every candidate paper in a cohort or list of assigned pairs."""
    papers_dict: Dict[str, PaperIntegrityResult] = {}
    valid_count = 0
    invalid_count = 0
    missing_count = 0
    incomplete_count = 0
    corrupted_count = 0
    source_complete_count = 0
    m1_complete_count = 0
    cat_counts: Dict[str, int] = {}

    pairs_list = list(pairs)
    total_candidates = len(pairs_list)

    for mssv, arxiv_id in pairs_list:
        res = audit_paper_integrity(store, mssv, arxiv_id)
        papers_dict[arxiv_id] = res

        if res.status == "valid":
            valid_count += 1
        elif res.status == "invalid":
            invalid_count += 1
        elif res.status == "missing":
            missing_count += 1
        elif res.status == "incomplete":
            incomplete_count += 1
        elif res.status == "corrupted":
            corrupted_count += 1

        if res.is_source_complete:
            source_complete_count += 1
        if res.is_m1_complete:
            m1_complete_count += 1

        for v in res.violations:
            cat_counts[v.category] = cat_counts.get(v.category, 0) + 1

    return CohortIntegrityReport(
        cohort_number=cohort_number,
        total_candidates=total_candidates,
        valid_count=valid_count,
        invalid_count=invalid_count,
        missing_count=missing_count,
        incomplete_count=incomplete_count,
        corrupted_count=corrupted_count,
        source_complete_count=source_complete_count,
        m1_complete_count=m1_complete_count,
        papers=papers_dict,
        violations_by_category=cat_counts,
    )


def format_integrity_report_markdown(report: CohortIntegrityReport) -> str:
    """Format cohort integrity audit results into a structured Markdown document."""
    lines = [
        f"### Storage Contract & Data Integrity Audit Report" + (f" — Cohort {report.cohort_number:04d}" if report.cohort_number is not None else ""),
        "",
        "#### 1. Tổng quan kiểm toán tính toàn vẹn (Integrity Overview)",
        "",
        "| Chỉ tiêu kiểm toán | Số lượng | Tỷ lệ (%) / Đánh giá |",
        "|---|---:|---:|",
        f"| Tổng số bài báo ứng viên | {report.total_candidates:,} | 100.0% |",
        f"| Hồ sơ đạt chuẩn hợp lệ (valid) | {report.valid_count:,} | {round(report.valid_count / report.total_candidates * 100.0, 1) if report.total_candidates else 0.0}% |",
        f"| Hồ sơ chưa hoàn tất (incomplete / partial) | {report.incomplete_count:,} | Cần resume |",
        f"| Hồ sơ hỏng (corrupted checksum / files) | {report.corrupted_count:,} | Cần khôi phục |",
        f"| Hồ sơ không hợp lệ (contract / schema invalid) | {report.invalid_count:,} | Lỗi contract |",
        f"| Hồ sơ chưa khởi tạo (missing) | {report.missing_count:,} | Chưa crawl |",
        f"| Bài báo hoàn tất mã nguồn (source_complete) | {report.source_complete_count:,} | Checksum verified |",
        f"| Bài báo hoàn tất toàn diện M1 (m1_complete) | {report.m1_complete_count:,} | Sẵn sàng nghiệm thu |",
        "",
        "#### 2. Phân loại vi phạm theo danh mục (Violations by Category)",
        "",
        "| Danh mục vi phạm | Số lượng vi phạm |",
        "|---|---:|",
    ]
    if report.violations_by_category:
        for cat, cnt in sorted(report.violations_by_category.items()):
            lines.append(f"| `{cat}` | {cnt:,} |")
    else:
        lines.append("| Không phát hiện vi phạm nào | 0 |")

    lines.append("")
    return "\n".join(lines) + "\n"
