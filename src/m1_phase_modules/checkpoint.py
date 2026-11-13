"""M1 checkpoint I/O, selection, locking and session bookkeeping."""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.fast_production import FastProductionRunner
from src.session_evidence import annotate_session_envelope, start_session_envelope
from src.storage import _atomic_json, arxiv_id_to_folder_key

from .common import (
    PHASE_SCHEMA_VERSION,
    M1PhaseError,
)


def _phase_root(drive_root: Path, selection: dict) -> Path:
    return drive_root / "reports" / "phase_checkpoints" / selection["fingerprint"]


def _ensure_starting_state(runner: FastProductionRunner, phase_root: Path,
                           selection: dict) -> dict:
    """Persist the initial member state once so benchmark resumes stay comparable."""
    path = phase_root / "starting_state.json"
    if path.exists():
        state = _read_json(path)
        return _validate_starting_state(state, selection, path)
    archive_hashes = {}
    for cohort in selection["cohorts"]:
        archive = runner.archives_dir / f"cohort_{cohort:04d}_corpus.tar.gz"
        archive_hashes[str(cohort)] = (
            runner._compute_sha256(archive) if archive.is_file() else "empty"
        )
    local_ids = sorted(
        arxiv_id for member, arxiv_id in selection["pairs"]
        if member == selection["mssv"]
        and ((runner.corpus_dir / arxiv_id_to_folder_key(arxiv_id)).exists()
             or (runner.corpus_dir / arxiv_id_to_folder_key(arxiv_id)).is_symlink())
    )
    fingerprint = hashlib.sha256(json.dumps(
        {"archives": archive_hashes, "local_ids": local_ids},
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    state = {
        "schema_version": PHASE_SCHEMA_VERSION,
        "mssv": selection["mssv"],
        "fingerprint": selection["fingerprint"],
        "starting_state_fingerprint": fingerprint,
        "starting_state_empty": (
            all(value == "empty" for value in archive_hashes.values()) and not local_ids
        ),
        "starting_archive_sha256": archive_hashes,
        "starting_local_ids": local_ids,
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_json(path, state)
    return state


def _validate_starting_state(state: Any, selection: dict, path: Path) -> dict:
    if (not isinstance(state, dict)
            or state.get("schema_version") != PHASE_SCHEMA_VERSION
            or state.get("mssv") != selection["mssv"]
            or state.get("fingerprint") != selection["fingerprint"]
            or not isinstance(state.get("starting_archive_sha256"), dict)
            or not isinstance(state.get("starting_local_ids"), list)):
        raise M1PhaseError(f"Starting-state checkpoint không tương thích: {path}")
    archives = state["starting_archive_sha256"]
    if set(archives) != {str(cohort) for cohort in selection["cohorts"]}:
        raise M1PhaseError(f"Starting-state archive coverage không khớp: {path}")
    if any(value != "empty" and (not isinstance(value, str)
                                  or len(value) != 64
                                  or any(char not in "0123456789abcdef" for char in value))
           for value in archives.values()):
        raise M1PhaseError(f"Starting-state archive checksum sai định dạng: {path}")
    local_ids = state["starting_local_ids"]
    assigned_ids = {arxiv_id for _member, arxiv_id in selection["pairs"]}
    if (any(not isinstance(arxiv_id, str) or arxiv_id not in assigned_ids for arxiv_id in local_ids)
            or local_ids != sorted(set(local_ids))):
        raise M1PhaseError(f"Starting-state ID không khớp phạm vi: {path}")
    expected_fingerprint = hashlib.sha256(json.dumps(
        {"archives": archives, "local_ids": local_ids},
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    expected_empty = all(value == "empty" for value in archives.values()) and not local_ids
    if (state.get("starting_state_fingerprint") != expected_fingerprint
            or state.get("starting_state_empty") is not expected_empty):
        raise M1PhaseError(f"Starting-state fingerprint/checkpoint không khớp: {path}")
    return state


def _ensure_directory(path: Path, root: Path) -> None:
    """Create a path under root and reject symlinked components."""
    root = root.resolve()
    path = Path(path)
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise M1PhaseError("Checkpoint path is outside its selected Drive root.") from exc
    current = root
    if current.is_symlink():
        raise M1PhaseError("Checkpoint root must not be a symlink.")
    for part in relative.parts:
        current = current / part
        if current.exists() and (current.is_symlink() or not current.is_dir()):
            raise M1PhaseError(f"Checkpoint path is not a plain directory: {current}")
        current.mkdir(exist_ok=True)


def _write_json(path: Path, payload: Any) -> str:
    _atomic_json(path, payload)
    return _sha256_file(path)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> Any:
    try:
        if path.is_symlink() or not path.is_file():
            raise M1PhaseError(f"Checkpoint evidence is not a regular file: {path.name}")
        return json.loads(path.read_text(encoding="utf-8"))
    except M1PhaseError:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        raise M1PhaseError(f"Cannot read checkpoint evidence {path.name}; preserve it for inspection.") from exc


def _persist_selection(drive_root: Path, selection: dict) -> Path:
    root = _phase_root(drive_root, selection)
    _ensure_directory(root, drive_root)
    for sibling in root.parent.iterdir():
        if sibling == root or sibling.is_symlink() or not sibling.is_dir():
            continue
        prior_path = sibling / "selection.json"
        if not prior_path.exists():
            continue
        prior = _read_json(prior_path)
        if (isinstance(prior, dict)
                and prior.get("mssv") == selection["mssv"]
                and prior.get("cohorts") == selection["cohorts"]
                and prior.get("scope") == selection["scope"]
                and prior.get("fingerprint") != selection["fingerprint"]):
            raise M1PhaseError(
                f"Có checkpoint cũ cùng phạm vi nhưng fingerprint khác tại {prior_path}; "
                "giữ nguyên để kiểm tra, không tự thay thế."
            )
    path = root / "selection.json"
    if path.exists():
        saved = _read_json(path)
        if saved != selection:
            raise M1PhaseError(
                f"Selection checkpoint mismatch at {path}; no evidence was changed."
            )
    else:
        _write_json(path, selection)
    return root


def _record_session(phase_root: Path, selection: dict, session_id: str, stage: str) -> None:
    path = phase_root / "session_index.json"
    if path.exists():
        saved = _read_json(path)
        if (not isinstance(saved, dict)
                or saved.get("schema_version") != PHASE_SCHEMA_VERSION
                or saved.get("fingerprint") != selection["fingerprint"]
                or not isinstance(saved.get("sessions"), list)):
            raise M1PhaseError(f"Session index không tương thích: {path}")
    else:
        saved = {"schema_version": PHASE_SCHEMA_VERSION,
                 "fingerprint": selection["fingerprint"], "sessions": []}
    if not any(row.get("session_id") == session_id for row in saved["sessions"] if isinstance(row, dict)):
        saved["sessions"].append({"session_id": session_id, "stage": stage})
    _write_json(path, saved)


@contextmanager
def _member_writer_lock(drive_root: Path, selection: dict):
    """Enforce one writer for this member and selected assignment scope."""
    lock_dir = drive_root / "reports" / "phase_locks"
    _ensure_directory(lock_dir, drive_root)
    lock = lock_dir / f"{selection['mssv']}.lock"
    payload = {
        "pid": os.getpid(),
        "mssv": selection["mssv"],
        "fingerprint": selection["fingerprint"],
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise M1PhaseError(
            f"Một tiến trình khác đang ghi phạm vi này hoặc lock cũ cần kiểm tra: {lock}"
        ) from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        yield
    finally:
        lock.unlink(missing_ok=True)


def _session_start(runner: FastProductionRunner, selection: dict, stage: str) -> tuple[str, Path, float]:
    session_id = uuid.uuid4().hex
    started = time.perf_counter()
    envelope = start_session_envelope(runner.reports_dir, session_id)
    annotate_session_envelope(
        envelope,
        mssv=selection["mssv"],
        selection_fingerprint=selection["fingerprint"],
        run_scope=selection["scope"],
        cohorts=selection["cohorts"],
        expected_id_count=selection["expected_count"],
        stage=stage,
    )
    return session_id, envelope, started
