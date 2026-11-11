"""Independent, resumable arXiv and Semantic Scholar phases for M1.

S2 evidence is staged outside the v2 corpus until the offline audit verifies
that it belongs to the exact assigned-ID selection and can be reconciled
without replacing arXiv provenance or source bytes.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time
import uuid
from typing import Any, Callable

from src.fast_production import (
    FastProductionRunner,
    cohort_pairs,
    member_cohort_range,
    verify_cohort_handoff,
)
from src.session_evidence import (
    SessionBudget,
    annotate_session_envelope,
    finish_session_envelope,
    observe_stage,
    start_session_envelope,
)
from src.storage import IntegrityError, StorageError, _atomic_json, arxiv_id_to_folder_key


PHASE_SCHEMA_VERSION = 1
S2_DEFAULTS = {
    "timeout": 30.0,
    "max_attempts": 8,
    "min_interval": 1.0,
    "reference_limit": 100,
    "max_pages": 1000,
    "max_response_bytes": 10_000_000,
    "max_retry_wait": 600.0,
    "backoff_base": 3.0,
    "backoff_cap": 60.0,
}


class M1PhaseError(RuntimeError):
    """A phase checkpoint is absent, incompatible, or fails verification."""


def build_member_selection(mssv: str, cohort_limit: int | None = None) -> dict:
    """Build the deterministic member selection shared by all M1 phases."""
    if not isinstance(mssv, str) or not mssv.strip():
        raise ValueError("MSSV không được để trống.")
    mssv = mssv.strip()
    assigned = member_cohort_range(mssv)
    if cohort_limit is not None and (
        isinstance(cohort_limit, bool)
        or not isinstance(cohort_limit, int)
        or not 1 <= cohort_limit <= len(assigned)
    ):
        raise ValueError(f"COHORT_LIMIT phải là số nguyên từ 1 đến {len(assigned)} hoặc None.")
    selected = assigned if cohort_limit is None else assigned[:cohort_limit]
    pairs = [pair for number in selected for pair in cohort_pairs(number)]
    if len(pairs) != 100 * len(selected) or any(member != mssv for member, _ in pairs):
        raise M1PhaseError("Phạm vi lô không khớp danh sách ID được giao trong cấu hình.")
    payload = json.dumps(pairs, ensure_ascii=False, separators=(",", ":"))
    fingerprint = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    group_pairs = [pair for number in range(1, 201) for pair in cohort_pairs(number)]
    group_payload = json.dumps(group_pairs, ensure_ascii=False, separators=(",", ":"))
    group_fingerprint = hashlib.sha256(group_payload.encode("utf-8")).hexdigest()
    return {
        "schema_version": PHASE_SCHEMA_VERSION,
        "mssv": mssv,
        "cohorts": selected,
        "expected_count": len(pairs),
        "pairs": [[member, arxiv_id] for member, arxiv_id in pairs],
        "fingerprint": fingerprint,
        "group_fingerprint": group_fingerprint,
        "scope": "member_full_assignment" if cohort_limit is None else "member_subset_same_pipeline",
    }


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


@contextmanager
def _api_key_environment(api_key: str | None):
    names = ("SEMANTIC_SCHOLAR_API_KEY", "S2_API_KEY")
    previous = {name: os.environ.get(name) for name in names}
    value = api_key.strip() if isinstance(api_key, str) and api_key.strip() else None
    for name in names:
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    try:
        yield value
    finally:
        for name, old in previous.items():
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old


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


def _source_state_manifest(path: Path, selection: dict, cohort: int) -> dict:
    if not path.exists():
        return {
            "schema_version": PHASE_SCHEMA_VERSION,
            "stage": "arxiv",
            "mssv": selection["mssv"],
            "cohort": cohort,
            "fingerprint": selection["fingerprint"],
            "ids": [aid for member, aid in cohort_pairs(cohort) if member == selection["mssv"]],
            "status": "not_started",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    saved = _read_json(path)
    expected_ids = [aid for member, aid in cohort_pairs(cohort) if member == selection["mssv"]]
    if (
        not isinstance(saved, dict)
        or saved.get("schema_version") != PHASE_SCHEMA_VERSION
        or saved.get("stage") != "arxiv"
        or saved.get("mssv") != selection["mssv"]
        or saved.get("cohort") != cohort
        or saved.get("fingerprint") != selection["fingerprint"]
        or saved.get("ids") != expected_ids
        or saved.get("status") not in {"not_started", "source_partial", "source_complete"}
    ):
        raise M1PhaseError(f"arXiv phase checkpoint không tương thích: {path}")
    count = saved.get("source_complete_papers")
    if count is not None and (type(count) is not int or not 0 <= count <= len(expected_ids)):
        raise M1PhaseError(f"Số lượng source trong checkpoint arXiv sai: {path}")
    digest = saved.get("archive_sha256")
    if digest is not None and (
        not isinstance(digest, str) or len(digest) != 64
        or any(char not in "0123456789abcdef" for char in digest)
    ):
        raise M1PhaseError(f"Checksum archive trong checkpoint arXiv sai: {path}")
    return saved


def _mark_archive_arxiv_only(runner: FastProductionRunner, cohort: int) -> None:
    """Keep source-phase archive metadata from certifying M1 through cached S2."""
    archive = runner.archives_dir / f"cohort_{cohort:04d}_corpus.tar.gz"
    manifest_path = archive.with_name(archive.name.replace(".tar.gz", ".manifest.json"))
    if not archive.is_file() or not manifest_path.is_file():
        raise M1PhaseError(f"Thiếu archive hoặc accounting manifest sau pha arXiv: {archive}")
    manifest = _read_json(manifest_path)
    digest = runner._compute_sha256(archive)
    if manifest.get("archive_sha256") != digest:
        raise M1PhaseError(f"Archive checksum/accounting không khớp: {archive}")
    source_complete = runner.count_source_completed_papers(cohort) == len(cohort_pairs(cohort))
    manifest["archive_state"] = "source_verified" if source_complete else "checkpoint_partial"
    manifest["phase_certification"] = "arxiv_only"
    _write_json(manifest_path, manifest)


def run_m1_arxiv_member(
    mssv: str,
    *,
    drive_root: str | Path,
    local_root: str | Path,
    cohort_limit: int | None = None,
    budget_seconds: float = 10 * 60 * 60,
) -> dict:
    """Verify assigned IDs and archive every arXiv source version, without S2."""
    selection = build_member_selection(mssv, cohort_limit)
    drive = Path(drive_root).expanduser().resolve()
    local = Path(local_root).expanduser().resolve()
    runner = FastProductionRunner(
        local_root=local,
        drive_root=drive,
        retry_429=True,
        retry_wait_budget=300.0,
        timeout=30.0,
    )
    budget = SessionBudget(budget_seconds)
    session_id, envelope, started = _session_start(runner, selection, "arxiv")
    phase_root = _persist_selection(drive, selection)
    cohort_results = {}
    stopped = False
    stop_reason = None
    try:
        with _member_writer_lock(drive, selection):
            _record_session(phase_root, selection, session_id, "arxiv")
            _ensure_starting_state(runner, phase_root, selection)
            for cohort in selection["cohorts"]:
                state_path = phase_root / "arxiv" / f"cohort_{cohort:04d}.json"
                _ensure_directory(state_path.parent, drive)
                state = _source_state_manifest(state_path, selection, cohort)
                if budget.should_stop():
                    stopped = True
                    stop_reason = {"type": "session_budget", "stage": "arxiv_before_cohort"}
                    state.update(status="source_partial", stop_reason=stop_reason)
                    _write_json(state_path, state)
                    break

                def process_cohort(cohort_number=cohort, manifest=state):
                    archive = runner.archives_dir / f"cohort_{cohort_number:04d}_corpus.tar.gz"
                    sidecars = [
                        archive,
                        archive.with_name(archive.name + ".sha256"),
                        archive.with_name(archive.name.replace(".tar.gz", ".manifest.json")),
                    ]
                    if any(path.exists() for path in sidecars):
                        runner.backup_cohort_archive(cohort_number, require_sidecars=True)
                    restored = runner.restore_cohort(cohort_number)
                    result = runner.run_cohort(
                        cohort_number,
                        resume=True,
                        revision_history=True,
                        s2_config=None,
                        max_ids=100,
                        should_stop=budget.should_stop,
                    )
                    _mark_archive_arxiv_only(runner, cohort_number)
                    source_count = runner.count_source_completed_papers(cohort_number)
                    complete = source_count == len(cohort_pairs(cohort_number))
                    archive_sha = None
                    if archive.is_file():
                        archive_sha = runner._compute_sha256(archive)
                    fast = result.get("fast_runner", {})
                    manifest.update(
                        status="source_complete" if complete else "source_partial",
                        restored_papers=restored,
                        source_complete_papers=source_count,
                        expected_count=len(cohort_pairs(cohort_number)),
                        archive_sha256=archive_sha,
                        stop_reason=fast.get("stop_reason"),
                        updated_at=datetime.now(timezone.utc).isoformat(),
                    )
                    _write_json(state_path, manifest)
                    return {
                        "status": manifest["status"],
                        "source_complete_papers": source_count,
                        "expected_count": manifest["expected_count"],
                        "stopped": bool(fast.get("stopped")),
                        "stop_reason": fast.get("stop_reason"),
                        "fast_runner": fast,
                        "timing_metrics": result.get("timing_metrics"),
                    }

                result = observe_stage(
                    runner,
                    "source",
                    cohort,
                    process_cohort,
                    session_id=session_id,
                    configuration={
                        "selection_fingerprint": selection["fingerprint"],
                        "mssv": selection["mssv"],
                        "run_scope": selection["scope"],
                        "resume": True,
                        "revision_history": True,
                        "s2_enabled": False,
                        "session_budget_seconds": budget_seconds,
                    },
                )
                cohort_results[str(cohort)] = result
                if result.get("stopped"):
                    stopped = True
                    stop_reason = result.get("stop_reason")
                    break
        completed = sum(
            value.get("source_complete_papers", 0) for value in cohort_results.values()
        )
        return {
            "stage": "arxiv",
            "mssv": selection["mssv"],
            "selection_fingerprint": selection["fingerprint"],
            "expected_id_count": selection["expected_count"],
            "source_complete_papers": completed,
            "cohorts": cohort_results,
            "stopped": stopped,
            "stop_reason": stop_reason,
            "m1_complete": False,
            "checkpoint_root": str(phase_root / "arxiv"),
            "session_id": session_id,
        }
    finally:
        finish_session_envelope(envelope, started_monotonic=started)


def _cohort_s2_paths(phase_root: Path, cohort: int) -> tuple[Path, Path]:
    cohort_root = phase_root / "s2" / f"cohort_{cohort:04d}"
    return cohort_root, cohort_root / "checkpoint.json"


def _new_s2_manifest(selection: dict, cohort: int) -> dict:
    ids = [aid for member, aid in cohort_pairs(cohort) if member == selection["mssv"]]
    return {
        "schema_version": PHASE_SCHEMA_VERSION,
        "stage": "s2",
        "mssv": selection["mssv"],
        "cohort": cohort,
        "fingerprint": selection["fingerprint"],
        "ids": ids,
        "status": "in_progress",
        "files": {},
        "papers": {
            aid: {
                "metadata_status": "not_attempted",
                "references_status": "not_attempted",
                "attempt_errors": [],
            }
            for aid in ids
        },
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _load_s2_manifest(path: Path, selection: dict, cohort: int) -> dict:
    if not path.exists():
        return _new_s2_manifest(selection, cohort)
    saved = _read_json(path)
    expected = _new_s2_manifest(selection, cohort)
    if (
        not isinstance(saved, dict)
        or saved.get("schema_version") != PHASE_SCHEMA_VERSION
        or saved.get("stage") != "s2"
        or saved.get("mssv") != selection["mssv"]
        or saved.get("cohort") != cohort
        or saved.get("fingerprint") != selection["fingerprint"]
        or saved.get("ids") != expected["ids"]
        or saved.get("status") not in {"in_progress", "partial", "complete"}
        or not isinstance(saved.get("files"), dict)
        or not isinstance(saved.get("papers"), dict)
        or set(saved["papers"]) != set(expected["ids"])
    ):
        raise M1PhaseError(f"S2 checkpoint không tương thích; giữ nguyên để kiểm tra: {path}")
    for relative, digest in saved["files"].items():
        if not isinstance(relative, str) or not isinstance(digest, str):
            raise M1PhaseError(f"S2 checkpoint có mục checksum sai định dạng: {path}")
        evidence = path.parent / "papers" / relative
        try:
            evidence.resolve().relative_to(path.parent.resolve())
        except ValueError as exc:
            raise M1PhaseError("Đường dẫn bằng chứng S2 thoát khỏi checkpoint.") from exc
        if evidence.is_symlink() or not evidence.is_file() or _sha256_file(evidence) != digest:
            raise M1PhaseError(
                f"Checksum S2 không khớp hoặc file bị thiếu: {evidence}; không tự xóa hay sửa."
            )
    return saved


def _evidence_file(cohort_root: Path, arxiv_id: str, kind: str) -> tuple[Path, str]:
    relative = f"{arxiv_id_to_folder_key(arxiv_id)}/{kind}.json"
    return cohort_root / "papers" / relative, relative


def _read_s2_paper(manifest: dict, cohort_root: Path, arxiv_id: str) -> tuple[dict | None, dict | None]:
    from src import s2_integration

    record = manifest["papers"][arxiv_id]
    metadata_result = None
    references_result = None
    if record.get("metadata_status") != "not_attempted":
        path, relative = _evidence_file(cohort_root, arxiv_id, "metadata")
        if relative not in manifest["files"]:
            raise M1PhaseError(f"Thiếu checksum cho metadata S2 {arxiv_id}.")
        payload = _read_json(path)
        if not isinstance(payload, dict) or payload.get("schema_version") != PHASE_SCHEMA_VERSION:
            raise M1PhaseError(f"Metadata S2 sai schema cho {arxiv_id}.")
        metadata_result = s2_integration.metadata_result(payload.get("result"), arxiv_id)
        if record.get("metadata_status") != metadata_result["status"]:
            raise M1PhaseError(f"Trạng thái metadata S2 không khớp evidence cho {arxiv_id}.")
    if record.get("references_status") != "not_attempted":
        path, relative = _evidence_file(cohort_root, arxiv_id, "references")
        if relative not in manifest["files"]:
            raise M1PhaseError(f"Thiếu checksum cho references S2 {arxiv_id}.")
        payload = _read_json(path)
        if not isinstance(payload, dict) or payload.get("schema_version") != PHASE_SCHEMA_VERSION:
            raise M1PhaseError(f"References S2 sai schema cho {arxiv_id}.")
        references_result = payload.get("snapshot")
        s2_integration.references_evidence(references_result, arxiv_id, for_resume=True)
        if record.get("references_status") != references_result.get("status"):
            raise M1PhaseError(f"Trạng thái references S2 không khớp evidence cho {arxiv_id}.")
    return metadata_result, references_result


def _save_s2_metadata(manifest: dict, cohort_root: Path, arxiv_id: str, result: dict) -> None:
    path, relative = _evidence_file(cohort_root, arxiv_id, "metadata")
    _ensure_directory(path.parent, cohort_root)
    previous = None
    if relative in manifest["files"]:
        previous = _read_json(path)
    history = list(previous.get("history", [])) if isinstance(previous, dict) else []
    if isinstance(previous, dict) and isinstance(previous.get("result"), dict):
        old = previous["result"]
        if old.get("status") not in {"found", "not_found"}:
            history.append(old)
    payload = {"schema_version": PHASE_SCHEMA_VERSION, "result": result, "history": history}
    manifest["files"][relative] = _write_json(path, payload)
    manifest["papers"][arxiv_id]["metadata_status"] = result["status"]
    if result.get("error"):
        manifest["papers"][arxiv_id]["attempt_errors"].append({
            "stage": "metadata", "status": result["status"],
            "http_status": result.get("http_status"), "error": result["error"],
            "observed_at": datetime.now(timezone.utc).isoformat(),
        })


def _save_s2_references(manifest: dict, cohort_root: Path, arxiv_id: str, snapshot: dict) -> None:
    path, relative = _evidence_file(cohort_root, arxiv_id, "references")
    _ensure_directory(path.parent, cohort_root)
    previous = None
    if relative in manifest["files"]:
        previous = _read_json(path)
    history = list(previous.get("history", [])) if isinstance(previous, dict) else []
    if isinstance(previous, dict) and isinstance(previous.get("snapshot"), dict):
        old = previous["snapshot"]
        if old.get("status") not in {"complete", "not_found"}:
            history.append(old)
    payload = {"schema_version": PHASE_SCHEMA_VERSION, "snapshot": snapshot, "history": history}
    manifest["files"][relative] = _write_json(path, payload)
    manifest["papers"][arxiv_id]["references_status"] = snapshot["status"]
    if snapshot.get("error"):
        manifest["papers"][arxiv_id]["attempt_errors"].append({
            "stage": "references", "status": snapshot["status"],
            "http_status": (snapshot.get("error") or {}).get("http_status"),
            "error": snapshot["error"],
            "observed_at": datetime.now(timezone.utc).isoformat(),
        })


def _reuse_corpus_s2(runner: FastProductionRunner, mssv: str, arxiv_id: str) -> tuple[dict | None, dict | None]:
    """Return only v2 S2 evidence that passes the same validators as fresh data."""
    from src import s2_integration

    try:
        metadata = runner.store.read_metadata(mssv, arxiv_id)
        references = runner.store.read_references(mssv, arxiv_id)
    except (StorageError, IntegrityError, OSError, ValueError):
        return None, None
    provenance = (metadata.get("provenance") or {}).get("semantic_scholar")
    meta_result = None
    if isinstance(provenance, dict) and isinstance(provenance.get("snapshot"), dict):
        try:
            meta_result = s2_integration.metadata_result(provenance["snapshot"], arxiv_id)
        except StorageError:
            meta_result = None
    refs_result = None
    if references.get("status") in {"complete", "not_found"}:
        try:
            s2_integration.references_evidence(references, arxiv_id)
            refs_result = references
        except StorageError:
            refs_result = None
    return meta_result, refs_result


def _save_cached_evidence(manifest: dict, cohort_root: Path, arxiv_id: str,
                          metadata_result: dict | None, references_result: dict | None) -> None:
    if metadata_result is not None:
        _save_s2_metadata(manifest, cohort_root, arxiv_id, metadata_result)
    if references_result is not None:
        _save_s2_references(manifest, cohort_root, arxiv_id, references_result)
    record = manifest["papers"][arxiv_id]
    record["reused_from_corpus_v2"] = {
        "metadata": metadata_result is not None,
        "references": references_result is not None,
    }


def _process_s2_cohort(
    runner: FastProductionRunner,
    selection: dict,
    cohort: int,
    phase_root: Path,
    s2_key: str | None,
    budget: SessionBudget,
    session_id: str,
) -> dict:
    from src import s2_integration, semantic_scholar

    cohort_root, checkpoint_path = _cohort_s2_paths(phase_root, cohort)
    _ensure_directory(cohort_root, runner.drive_root)
    _ensure_directory(cohort_root / "papers", runner.drive_root)
    manifest = _load_s2_manifest(checkpoint_path, selection, cohort)
    _write_json(checkpoint_path, manifest)
    config = s2_integration.configuration(S2_DEFAULTS)
    limiter = semantic_scholar.RateLimiter(config["min_interval"])
    timings = []
    stopped = False
    stop_reason = None
    attempted = 0

    pairs = [(member, aid) for member, aid in cohort_pairs(cohort) if member == selection["mssv"]]
    with _api_key_environment(s2_key):
        semantic_scholar._key_from_environment()
        for index, (member, arxiv_id) in enumerate(pairs, start=1):
            record = manifest["papers"][arxiv_id]
            try:
                metadata_result, references_result = _read_s2_paper(
                    manifest, cohort_root, arxiv_id
                )
            except M1PhaseError:
                raise
            except (StorageError, ValueError, TypeError) as exc:
                raise M1PhaseError(
                    f"S2 checkpoint không hợp lệ cho {arxiv_id}: {type(exc).__name__}; giữ nguyên file."
                ) from exc

            if metadata_result is None and references_result is None:
                cached_meta, cached_refs = _reuse_corpus_s2(runner, member, arxiv_id)
                if cached_meta is not None or cached_refs is not None:
                    _save_cached_evidence(
                        manifest, cohort_root, arxiv_id, cached_meta, cached_refs
                    )
                    metadata_result, references_result = cached_meta, cached_refs
                    _write_json(checkpoint_path, manifest)

            if (
                metadata_result is not None
                and metadata_result["status"] in {"found", "not_found"}
                and references_result is not None
                and references_result["status"] in {"complete", "not_found"}
            ):
                continue
            if budget.should_stop():
                stopped = True
                stop_reason = {"type": "session_budget", "stage": "s2_before_paper"}
                break

            started = time.perf_counter()
            attempted += 1
            try:
                if metadata_result is None or metadata_result["status"] not in {"found", "not_found"}:
                    response = semantic_scholar.fetch_metadata(
                        [arxiv_id],
                        batch_size=1,
                        limiter=limiter,
                        require_arxiv_identity=True,
                        **{
                            name: config[name]
                            for name in (
                                "timeout", "max_attempts", "max_response_bytes",
                                "max_retry_wait", "backoff_base", "backoff_cap",
                            )
                        },
                    )[0]
                    response = s2_integration.metadata_result(response, arxiv_id)
                    _save_s2_metadata(manifest, cohort_root, arxiv_id, response)
                    metadata_result = response
                    _write_json(checkpoint_path, manifest)
                    if response.get("http_status") in {403, 429}:
                        stopped = True
                        stop_reason = {
                            "type": "http_policy", "stage": "s2_metadata",
                            "http_status": response["http_status"],
                            "checkpoint_saved": True,
                        }
                        break

                if budget.should_stop():
                    stopped = True
                    stop_reason = {"type": "session_budget", "stage": "s2_before_references"}
                    break

                if references_result is None or references_result.get("status") not in {"complete", "not_found"}:
                    previous_snapshot = references_result
                    reference_response = semantic_scholar.fetch_references(
                        arxiv_id,
                        limit=config["reference_limit"],
                        max_pages=config["max_pages"],
                        timeout=config["timeout"],
                        max_attempts=config["max_attempts"],
                        limiter=limiter,
                        max_response_bytes=config["max_response_bytes"],
                        resume_from=previous_snapshot if previous_snapshot else None,
                        max_retry_wait=config["max_retry_wait"],
                        backoff_base=config["backoff_base"],
                        backoff_cap=config["backoff_cap"],
                    )
                    s2_integration.references_evidence(
                        reference_response, arxiv_id, for_resume=True
                    )
                    _save_s2_references(manifest, cohort_root, arxiv_id, reference_response)
                    references_result = reference_response
                    _write_json(checkpoint_path, manifest)
                    http_status = (reference_response.get("error") or {}).get("http_status")
                    if http_status in {403, 429}:
                        stopped = True
                        stop_reason = {
                            "type": "http_policy", "stage": "s2_references",
                            "http_status": http_status,
                            "checkpoint_saved": True,
                        }
                        break
            except KeyboardInterrupt:
                stopped = True
                stop_reason = {"type": "interrupted", "stage": "s2"}
                break
            finally:
                elapsed = max(0.0, time.perf_counter() - started)
                timings.append({"arxiv_id": arxiv_id, "s2_wall_seconds": elapsed})
                _write_json(checkpoint_path, manifest)
            if index % 10 == 0 or index == len(pairs):
                print(f"[M1/S2] Lô {cohort}: đã xét {index}/{len(pairs)} ID; hiện tại {arxiv_id}.")
            if stopped:
                break

    statuses = manifest["papers"].values()
    metadata_complete = sum(item.get("metadata_status") in {"found", "not_found"} for item in statuses)
    references_complete = sum(item.get("references_status") in {"complete", "not_found"} for item in statuses)
    complete = metadata_complete == len(pairs) and references_complete == len(pairs)
    manifest.update(
        status="complete" if complete else "partial",
        updated_at=datetime.now(timezone.utc).isoformat(),
        stopped=stopped,
        stop_reason=stop_reason,
        session_id=session_id,
        metadata_complete=metadata_complete,
        references_complete=references_complete,
    )
    _write_json(checkpoint_path, manifest)
    return {
        "status": manifest["status"],
        "cohort": cohort,
        "attempted_papers": attempted,
        "metadata_complete": metadata_complete,
        "references_complete": references_complete,
        "fully_enriched": sum(
            item.get("metadata_status") == "found"
            and item.get("references_status") == "complete"
            for item in manifest["papers"].values()
        ),
        "stopped": stopped,
        "stop_reason": stop_reason,
        "paper_timings": timings,
        "selection_fingerprint": selection["fingerprint"],
        "checkpoint_path": str(checkpoint_path),
    }


def run_m1_s2_member(
    mssv: str,
    *,
    drive_root: str | Path,
    local_root: str | Path,
    semantic_scholar_api_key: str | None = None,
    cohort_limit: int | None = None,
    budget_seconds: float = 10 * 60 * 60,
) -> dict:
    """Collect S2 metadata and all references independently of arXiv source state."""
    selection = build_member_selection(mssv, cohort_limit)
    drive = Path(drive_root).expanduser().resolve()
    local = Path(local_root).expanduser().resolve()
    runner = FastProductionRunner(local_root=local, drive_root=drive)
    budget = SessionBudget(budget_seconds)
    session_id, envelope, started = _session_start(runner, selection, "s2")
    phase_root = _persist_selection(drive, selection)
    cohort_results = {}
    stopped = False
    stop_reason = None
    try:
        with _member_writer_lock(drive, selection):
            _record_session(phase_root, selection, session_id, "s2")
            _ensure_starting_state(runner, phase_root, selection)
            for cohort in selection["cohorts"]:
                result = observe_stage(
                    runner,
                    "s2",
                    cohort,
                    lambda number=cohort: _process_s2_cohort(
                        runner, selection, number, phase_root,
                        semantic_scholar_api_key, budget, session_id,
                    ),
                    session_id=session_id,
                    configuration={
                        "selection_fingerprint": selection["fingerprint"],
                        "mssv": selection["mssv"],
                        "run_scope": selection["scope"],
                        "authenticated": bool(semantic_scholar_api_key),
                        "s2_min_interval": S2_DEFAULTS["min_interval"],
                        "max_attempts": S2_DEFAULTS["max_attempts"],
                        "max_retry_wait": S2_DEFAULTS["max_retry_wait"],
                        "backoff_base": S2_DEFAULTS["backoff_base"],
                        "backoff_cap": S2_DEFAULTS["backoff_cap"],
                        "session_budget_seconds": budget_seconds,
                    },
                )
                cohort_results[str(cohort)] = result
                if result.get("stopped"):
                    stopped = True
                    stop_reason = result.get("stop_reason")
                    break
        return {
            "stage": "s2",
            "mssv": selection["mssv"],
            "selection_fingerprint": selection["fingerprint"],
            "expected_id_count": selection["expected_count"],
            "metadata_complete": sum(r.get("metadata_complete", 0) for r in cohort_results.values()),
            "references_complete": sum(r.get("references_complete", 0) for r in cohort_results.values()),
            "cohorts": cohort_results,
            "stopped": stopped,
            "stop_reason": stop_reason,
            "m1_complete": False,
            "checkpoint_root": str(phase_root / "s2"),
            "session_id": session_id,
        }
    finally:
        finish_session_envelope(envelope, started_monotonic=started)


def _validate_s2_cohort(phase_root: Path, selection: dict, cohort: int) -> tuple[dict, dict[str, tuple[dict, dict]]]:
    from src import s2_integration

    cohort_root, checkpoint_path = _cohort_s2_paths(phase_root, cohort)
    manifest = _load_s2_manifest(checkpoint_path, selection, cohort)
    if (manifest.get("status") != "complete"
            or manifest.get("metadata_complete") != len(manifest["ids"])
            or manifest.get("references_complete") != len(manifest["ids"])):
        raise M1PhaseError(f"S2 cohort {cohort} chưa có checkpoint hoàn tất.")
    evidence = {}
    for member, arxiv_id in cohort_pairs(cohort):
        if member != selection["mssv"]:
            continue
        metadata_result, references_result = _read_s2_paper(manifest, cohort_root, arxiv_id)
        if metadata_result is None or references_result is None:
            raise M1PhaseError(f"S2 evidence chưa đầy đủ cho {arxiv_id}.")
        s2_integration.metadata_result(metadata_result, arxiv_id)
        if metadata_result["status"] not in {"found", "not_found"}:
            raise M1PhaseError(
                f"S2 metadata chưa đạt trạng thái cuối cho {arxiv_id}: {metadata_result['status']}"
            )
        status = s2_integration.references_evidence(references_result, arxiv_id)
        if status != "cached" and references_result.get("status") != "not_found":
            raise M1PhaseError(f"S2 references chưa hoàn tất cho {arxiv_id}.")
        if references_result.get("status") not in {"complete", "not_found"}:
            raise M1PhaseError(
                f"S2 references chưa đạt trạng thái cuối cho {arxiv_id}: {references_result.get('status')}"
            )
        evidence[arxiv_id] = (metadata_result, references_result)
    expected_files = set()
    for arxiv_id, record in manifest["papers"].items():
        folder = arxiv_id_to_folder_key(arxiv_id)
        if record.get("metadata_status") != "not_attempted":
            expected_files.add(f"{folder}/metadata.json")
        if record.get("references_status") != "not_attempted":
            expected_files.add(f"{folder}/references.json")
    if set(manifest["files"]) != expected_files:
        raise M1PhaseError(f"S2 checkpoint {cohort} có mục checksum thừa hoặc thiếu.")
    return manifest, evidence


def _validate_source_paper(runner: FastProductionRunner, mssv: str, arxiv_id: str) -> tuple[str, dict | None]:
    from src.storage_integrity import audit_paper_integrity

    try:
        result = audit_paper_integrity(runner.store, mssv, arxiv_id)
        if result.has_integrity_failure:
            return "integrity_failure", None
        manifest = runner.store.read_manifest(mssv, arxiv_id)
    except Exception:
        return "missing_or_invalid", None
    if manifest.get("discovery_status") == "not_found" and manifest.get("status") == "not_found":
        if manifest.get("versions") != {} or result.status != "valid":
            return "integrity_failure", manifest
        return "not_found", manifest
    if result.is_source_complete:
        return "found_source_complete", manifest
    return "source_incomplete", manifest


def _same_reference_snapshot(left: dict, right: dict) -> bool:
    return all(left.get(key) == right.get(key) for key in ("status", "coverage", "items"))


def _preflight_merge(runner: FastProductionRunner, selection: dict,
                     evidence_by_id: dict[str, tuple[dict, dict]]) -> list[dict]:
    """Validate source identity and S2 conflicts before any v2 corpus write."""
    from src import s2_integration

    issues = []
    for mssv, arxiv_id in selection["pairs"]:
        source_status, _manifest = _validate_source_paper(runner, mssv, arxiv_id)
        if source_status in {"missing_or_invalid", "source_incomplete", "integrity_failure"}:
            issues.append({"arxiv_id": arxiv_id, "issue": source_status})
            continue
        metadata_result, references_result = evidence_by_id[arxiv_id]
        if source_status == "not_found":
            if metadata_result["status"] == "found" or references_result.get("status") == "complete" and references_result.get("items"):
                issues.append({"arxiv_id": arxiv_id, "issue": "s2_conflicts_with_arxiv_not_found"})
            continue
        try:
            old_metadata = runner.store.read_metadata(mssv, arxiv_id)
            old_references = runner.store.read_references(mssv, arxiv_id)
            s2_integration.metadata_cached(old_metadata)
            s2_integration.references_evidence(old_references, arxiv_id, for_resume=True)
        except (StorageError, IntegrityError, ValueError, KeyError, TypeError):
            issues.append({"arxiv_id": arxiv_id, "issue": "existing_v2_checkpoint_invalid"})
            continue
        old_s2 = (old_metadata.get("provenance") or {}).get("semantic_scholar")
        if isinstance(old_s2, dict):
            old_snapshot = old_s2.get("snapshot")
            if old_snapshot != metadata_result:
                issues.append({"arxiv_id": arxiv_id, "issue": "s2_metadata_conflict"})
        if old_references.get("status") in {"complete", "not_found"}:
            if not _same_reference_snapshot(old_references, references_result):
                issues.append({"arxiv_id": arxiv_id, "issue": "s2_references_conflict"})
        elif old_references.get("items"):
            if references_result.get("items", [])[:len(old_references["items"])] != old_references["items"]:
                issues.append({"arxiv_id": arxiv_id, "issue": "s2_references_prefix_conflict"})
    return issues


def _merge_s2(runner: FastProductionRunner, selection: dict,
              evidence_by_id: dict[str, tuple[dict, dict]]) -> list[str]:
    from src import s2_integration

    merged = []
    for mssv, arxiv_id in selection["pairs"]:
        source_status, _manifest = _validate_source_paper(runner, mssv, arxiv_id)
        if source_status == "not_found":
            continue
        if source_status != "found_source_complete":
            raise M1PhaseError(f"Nguồn arXiv đổi trạng thái trong lúc hợp nhất: {arxiv_id}")
        metadata_result, references_result = evidence_by_id[arxiv_id]
        old_metadata = runner.store.read_metadata(mssv, arxiv_id)
        existing_snapshot = (old_metadata.get("provenance") or {}).get("semantic_scholar")
        if not isinstance(existing_snapshot, dict):
            merged_metadata = s2_integration.metadata_snapshot(old_metadata, metadata_result)
            runner.store.save_metadata(mssv, arxiv_id, merged_metadata)
        old_references = runner.store.read_references(mssv, arxiv_id)
        if old_references.get("status") not in {"complete", "not_found"}:
            merged_references = s2_integration.reference_result(old_references, references_result)
            runner.store.save_references(mssv, arxiv_id, merged_references)
        merged.append(arxiv_id)
    return merged


def _load_phase_observations(runner: FastProductionRunner, phase_root: Path,
                             selection: dict) -> tuple[list[dict], list[dict]]:
    index_path = phase_root / "session_index.json"
    if not index_path.is_file():
        raise M1PhaseError("Thiếu session index; không thể cộng runtime giữa các phiên.")
    index = _read_json(index_path)
    if (not isinstance(index, dict)
            or index.get("fingerprint") != selection["fingerprint"]
            or not isinstance(index.get("sessions"), list)):
        raise M1PhaseError("Session index không khớp fingerprint lựa chọn.")
    session_ids = [
        row.get("session_id") for row in index["sessions"]
        if isinstance(row, dict) and isinstance(row.get("session_id"), str)
    ]
    if len(session_ids) != len(set(session_ids)):
        raise M1PhaseError("Session index chứa ID phiên trùng.")
    observations = []
    envelopes = []
    for session_id in session_ids:
        envelope_path = runner.reports_dir / "session_envelopes" / f"{session_id}.json"
        if not envelope_path.is_file():
            envelopes.append({})
        else:
            envelopes.append(_read_json(envelope_path))
        for path in sorted((runner.reports_dir / "stage_observations").glob(f"{session_id}_*.json")):
            row = _read_json(path)
            if row.get("session_id") != session_id:
                raise M1PhaseError(f"Observation không khớp phiên: {path.name}")
            observations.append(row)
    return observations, envelopes


def _member_benchmark_summary(runner: FastProductionRunner, phase_root: Path,
                              selection: dict, audit_result: dict) -> dict:
    from src.session_evidence import (
        fingerprint_complete_corpus,
        summarize_discovery_timings,
        summarize_paper_timings,
        summarize_workflow,
    )

    observations, envelopes = _load_phase_observations(runner, phase_root, selection)
    starting_state_path = phase_root / "starting_state.json"
    starting_state = (
        _validate_starting_state(_read_json(starting_state_path), selection, starting_state_path)
        if starting_state_path.is_file() else {}
    )
    required = [
        (stage, cohort)
        for cohort in selection["cohorts"]
        for stage in ("source", "s2")
    ]
    required.append(("audit", 0))
    workflow = summarize_workflow(observations, required, session_envelopes=envelopes)
    found_ids = []
    for member, arxiv_id in selection["pairs"]:
        try:
            if runner.store.read_manifest(member, arxiv_id).get("discovery_status") == "found":
                found_ids.append(arxiv_id)
        except Exception:
            continue
    paper_timings = summarize_paper_timings(observations, found_ids)
    discovery_timings = summarize_discovery_timings(
        observations, [aid for _member, aid in selection["pairs"]]
    )
    environment_rows = sorted({
        json.dumps(row.get("environment") or {}, sort_keys=True, separators=(",", ":"))
        for row in observations
    })
    runtime_commits = sorted({
        row.get("commit") for row in observations
        if isinstance(row.get("commit"), str) and row.get("commit")
    })
    environment_fingerprint = hashlib.sha256(
        "\n".join(environment_rows + runtime_commits).encode("utf-8")
    ).hexdigest() if environment_rows else None
    data_signature = (
        fingerprint_complete_corpus(runner.store, selection["pairs"])
        if audit_result.get("is_reconciled") else None
    )
    audit_stats = audit_result.get("statistics") or {}
    logical_count = audit_stats.get("logical_size_measured_papers")
    found_count = audit_stats.get("found_papers")
    logical_before = audit_stats.get("average_logical_before_bytes_per_paper")
    logical_after = audit_stats.get("average_logical_after_bytes_per_paper")
    logical_size_complete = (
        type(logical_count) is int and type(found_count) is int and logical_count == found_count
        and ((found_count == 0 and logical_before is None and logical_after is None)
             or (found_count > 0 and type(logical_before) in (int, float)
                 and type(logical_after) in (int, float)
                 and all(value >= 0 and value < float("inf") for value in (logical_before, logical_after))))
    )
    all_m1 = (
        audit_result.get("is_reconciled") is True
        and workflow.get("status") == "measured_intervals"
        and paper_timings.get("status") == "complete"
        and discovery_timings.get("status") == "complete"
        and data_signature is not None
        and len(runtime_commits) == 1
        and logical_size_complete
    )
    summary = {
        "schema_version": PHASE_SCHEMA_VERSION,
        "mssv": selection["mssv"],
        "run_scope": selection["scope"],
        "cohorts": selection["cohorts"],
        "expected_id_count": selection["expected_count"],
        "selection_fingerprint": selection["group_fingerprint"],
        "member_selection_fingerprint": selection["fingerprint"],
        "campaign_id": selection["fingerprint"],
        "data_signature": data_signature,
        "environment_fingerprint": environment_fingerprint,
        "runtime_commit": runtime_commits[0] if len(runtime_commits) == 1 else None,
        "runtime_commits": runtime_commits,
        "starting_state_fingerprint": starting_state.get("starting_state_fingerprint"),
        "starting_state_empty": starting_state.get("starting_state_empty"),
        "execution_mode": (
            "from_scratch" if starting_state.get("starting_state_empty") is True
            else "resume" if starting_state.get("starting_state_empty") is False
            else "unknown"
        ),
        "discovery_arm": "arxiv_only",
        "session_ids": [row["session_id"] for row in _read_json(phase_root / "session_index.json")["sessions"]],
        "status": workflow.get("status"),
        "known_active_seconds": workflow.get("known_active_seconds"),
        "active_seconds": workflow.get("active_seconds"),
        "end_to_end_wall_seconds": workflow.get("end_to_end_wall_seconds"),
        "end_to_end_complete": bool(all_m1 and selection["expected_count"] == 5000),
        "sampled_peak_ram_bytes": workflow.get("sampled_peak_ram_bytes"),
        "sampled_average_ram_bytes": workflow.get("sampled_average_ram_bytes"),
        "sampled_peak_disk_bytes": workflow.get("sampled_peak_disk_bytes"),
        "paper_timings": paper_timings,
        "discovery_timings": discovery_timings,
        "measurement_scope": (
            "Sequential arXiv source and S2 intervals across recorded sessions; "
            "retry/delay and audit/export session time are included, inter-session gaps "
            "and environment installation are excluded."
        ),
        "resource_measurement_scope": (
            "RAM is sampled process RSS; disk is sampled logical regular-file bytes "
            "under the disjoint roots listed in each stage observation. Sampled peaks "
            "can miss short-lived files or memory spikes."
        ),
        "measurement_protocol": (
            "m1-independent-phases-v1; source/S2 retry and delays included; "
            "sequential session wall time includes audit/export; inter-session gaps excluded"
        ),
        "logical_size_metrics": {
            key: (audit_result.get("statistics") or {}).get(key)
            for key in (
                "logical_size_measured_papers",
                "average_logical_before_bytes_per_paper",
                "average_logical_after_bytes_per_paper",
                "logical_size_scope",
            )
        },
        "logical_size_status": "complete" if logical_size_complete else "incomplete",
        "reference_metadata_success": {
            "complete_records": audit_stats.get("reference_metadata_complete_records"),
            "eligible_records": audit_stats.get("total_complete_reference_records"),
            "success_rate_percent": audit_stats.get("reference_metadata_success_rate"),
            "definition": audit_stats.get("reference_metadata_success_definition"),
        },
        "missing_stages": workflow.get("missing_stages", []),
        "session_gaps_seconds": workflow.get("session_gaps_seconds"),
    }
    return summary


def run_m1_audit_member(
    mssv: str,
    *,
    drive_root: str | Path,
    local_root: str | Path,
    cohort_limit: int | None = None,
    output_dir: str | Path | None = None,
    budget_seconds: float = 10 * 60 * 60,
) -> dict:
    """Validate both phases, safely merge S2, then export only complete M1 data."""
    from src.reporting import compute_m1_statistics
    from src.storage_integrity import audit_cohort_integrity
    from src.fast_production import verify_cohort_handoff
    from src.export import export_official_datasets
    from src.bbl_handling import audit_bbl_sources

    selection = build_member_selection(mssv, cohort_limit)
    drive = Path(drive_root).expanduser().resolve()
    local = Path(local_root).expanduser().resolve()
    runner = FastProductionRunner(local_root=local, drive_root=drive)
    budget = SessionBudget(budget_seconds)
    session_id, envelope, started = _session_start(runner, selection, "audit")
    phase_root = _persist_selection(drive, selection)

    def audit_and_reconcile() -> dict:
        source_issues = []
        source_states = {}
        for cohort in selection["cohorts"]:
            state_path = phase_root / "arxiv" / f"cohort_{cohort:04d}.json"
            state = _source_state_manifest(state_path, selection, cohort)
            source_states[str(cohort)] = {
                key: state.get(key) for key in (
                    "status", "source_complete_papers", "expected_count",
                    "archive_sha256", "stop_reason", "updated_at",
                )
            }
            # Resume and validate any archive already present in this selection.
            runner.restore_cohort(cohort)
            for member, arxiv_id in cohort_pairs(cohort):
                if member != selection["mssv"]:
                    continue
                status, _ = _validate_source_paper(runner, member, arxiv_id)
                if status not in {"found_source_complete", "not_found"}:
                    source_issues.append({"arxiv_id": arxiv_id, "issue": status})

        s2_evidence = {}
        s2_manifests = {}
        s2_issues = []
        for cohort in selection["cohorts"]:
            try:
                manifest, evidence = _validate_s2_cohort(phase_root, selection, cohort)
                s2_manifests[str(cohort)] = manifest
                s2_evidence.update(evidence)
            except M1PhaseError as exc:
                s2_issues.append({"cohort": cohort, "issue": str(exc)})

        preflight_issues = []
        merge_conflicts = []
        if not source_issues and not s2_issues and len(s2_evidence) == selection["expected_count"]:
            preflight_issues = _preflight_merge(runner, selection, s2_evidence)
            merge_conflicts.extend(preflight_issues)
        issues = source_issues + s2_issues + preflight_issues
        merged_ids = []
        if not issues:
            if budget.should_stop():
                merge_conflicts.append({"issue": "audit_session_budget"})
            else:
                try:
                    merged_ids = _merge_s2(runner, selection, s2_evidence)
                    # Refresh durable archives after S2 reconciliation; preserve the
                    # source-stage archive before publishing each replacement.
                    for cohort in selection["cohorts"]:
                        archive = runner.archives_dir / f"cohort_{cohort:04d}_corpus.tar.gz"
                        if archive.exists():
                            runner.backup_cohort_archive(cohort, require_sidecars=True)
                        runner._archive_cohort(cohort, archive)
                except Exception as exc:
                    merge_conflicts.append({
                        "issue": "merge_or_archive_failed",
                        "error_type": type(exc).__name__,
                        "detail": str(exc),
                    })

        bbl_audit = None
        bbl_audit_error = None
        try:
            bbl_audit = audit_bbl_sources(
                runner.store,
                selection["pairs"],
                output_dir=runner.reports_dir / "derived_bibliography" / session_id,
                report_path=runner.reports_dir / f"bbl_handling_{session_id}.json",
                session_id=session_id,
            )
        except Exception as exc:
            bbl_audit_error = {"error": type(exc).__name__, "detail": str(exc)}

        cohort_audits = {}
        all_handoffs = True
        all_cohort_audits = True
        for cohort in selection["cohorts"]:
            pairs = [(member, aid) for member, aid in cohort_pairs(cohort) if member == selection["mssv"]]
            try:
                integrity = audit_cohort_integrity(runner.store, pairs, cohort_number=cohort)
                counts = runner.count_m1_completed_papers(cohort)
                handoff = verify_cohort_handoff(runner, cohort, inspect_members=True)
                handoff_ok = handoff.get("archive_state") in {"m1_handoff_verified", "handoff_verified"}
                integrity_payload = integrity.to_dict()
                integrity_ok = (
                    integrity_payload.get("total_candidates") == len(pairs)
                    and integrity_payload.get("valid_count") == len(pairs)
                    and not any(integrity_payload.get(key, 0) for key in (
                        "invalid_count", "missing_count", "incomplete_count", "corrupted_count"
                    ))
                )
            except Exception as exc:
                integrity = {"is_reconciled": False, "integrity_failed_papers": len(pairs), "error": type(exc).__name__, "detail": str(exc)}
                counts = {"is_cohort_complete": False, "unprocessed": len(pairs), "integrity_failures": len(pairs)}
                handoff = {"archive_state": "invalid", "error": type(exc).__name__, "detail": str(exc)}
                handoff_ok = False
                integrity_payload = integrity
                integrity_ok = False
            integrity_payload["is_reconciled"] = integrity_ok
            cohort_audits[str(cohort)] = {
                "integrity": integrity_payload,
                "m1_counts": counts,
                "handoff": handoff,
                "s2_status": (s2_manifests.get(str(cohort)) or {}).get("status", "missing"),
            }
            all_handoffs = all_handoffs and handoff_ok
            all_cohort_audits = all_cohort_audits and integrity_ok and counts.get("is_cohort_complete") is True

        stats = compute_m1_statistics(
            runner.store,
            selection["pairs"],
            report_scope=selection["scope"],
        )
        data_complete = (
            not issues
            and not merge_conflicts
            and bbl_audit_error is None
            and stats.get("is_reconciled") is True
            and stats.get("integrity_failed_papers", 0) == 0
            and stats.get("unprocessed_papers", selection["expected_count"]) == 0
            and stats.get("m1_complete_papers", 0) + stats.get("confirmed_not_found", 0)
            == selection["expected_count"]
            and all_cohort_audits
            and all_handoffs
        )
        export_result = None
        export_error = None
        if data_complete:
            destination = Path(output_dir).expanduser().resolve() if output_dir else drive / "exports" / selection["fingerprint"]
            export_result = export_official_datasets(
                runner.store,
                selection["pairs"],
                destination,
                require_m1_complete=True,
            )
        else:
            export_error = "Export blocked: both phase checkpoints and full M1 audit are required."
        report = {
            "schema_version": PHASE_SCHEMA_VERSION,
            "mssv": selection["mssv"],
            "selection_fingerprint": selection["fingerprint"],
            "scope": selection["scope"],
            "expected_id_count": selection["expected_count"],
            "source_issues": source_issues,
            "source_checkpoints": source_states,
            "s2_issues": s2_issues,
            "merge_conflicts": merge_conflicts,
            "merged_s2_ids": len(merged_ids),
            "bbl_audit": bbl_audit,
            "bbl_audit_error": bbl_audit_error,
            "cohorts": cohort_audits,
            "statistics": stats,
            "is_reconciled": data_complete,
            "integrity_failed_papers": stats.get("integrity_failed_papers", 0),
            "unprocessed_papers": stats.get("unprocessed_papers"),
            "export_status": "exported" if export_result else "blocked",
            "export": export_result,
            "export_error": export_error,
            "stop_reason": None if data_complete else "incomplete_or_conflicting_phase_evidence",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        report_path = runner.reports_dir / f"m1_audit_{selection['mssv']}_{selection['fingerprint'][:16]}.json"
        _write_json(report_path, report)
        report["report_path"] = str(report_path)
        return report

    try:
        with _member_writer_lock(drive, selection):
            _record_session(phase_root, selection, session_id, "audit")
            result = observe_stage(
                runner,
                "audit",
                0,
                audit_and_reconcile,
                session_id=session_id,
                configuration={
                    "selection_fingerprint": selection["fingerprint"],
                    "mssv": selection["mssv"],
                    "run_scope": selection["scope"],
                    "expected_id_count": selection["expected_count"],
                    "session_budget_seconds": budget_seconds,
                },
            )
            finish_session_envelope(envelope, started_monotonic=started)
            benchmark = _member_benchmark_summary(runner, phase_root, selection, result)
            result["benchmark"] = benchmark
            report_path = Path(result["report_path"])
            saved_report = _read_json(report_path)
            saved_report["benchmark"] = benchmark
            _write_json(report_path, saved_report)
            summary_path = runner.reports_dir / (
                f"benchmark_observed_{selection['mssv']}_{selection['fingerprint'][:16]}.json"
            )
            _write_json(summary_path, benchmark)
            result["benchmark_summary_path"] = str(summary_path)
            return result
    finally:
        envelope_value = _read_json(envelope)
        if envelope_value.get("status") == "running":
            finish_session_envelope(envelope, started_monotonic=started)


def run_m1_member(
    mssv: str,
    *,
    drive_root: str | Path,
    local_root: str | Path,
    kaggle_token: str | None = None,
    semantic_scholar_api_key: str | None = None,
    cohort_limit: int | None = None,
    budget_seconds: float = 10 * 60 * 60,
) -> dict:
    """Backward-compatible sequential source → S2 → audit M1 entry point."""
    started = time.monotonic()

    def remaining_budget() -> float:
        return max(0.001, budget_seconds - (time.monotonic() - started))

    arxiv_result = run_m1_arxiv_member(
        mssv,
        drive_root=drive_root,
        local_root=local_root,
        cohort_limit=cohort_limit,
        budget_seconds=remaining_budget(),
    )
    if arxiv_result["stopped"]:
        return {"stage": "arxiv", "arxiv": arxiv_result, "s2": None, "audit": None}
    s2_result = run_m1_s2_member(
        mssv,
        drive_root=drive_root,
        local_root=local_root,
        semantic_scholar_api_key=semantic_scholar_api_key,
        cohort_limit=cohort_limit,
        budget_seconds=remaining_budget(),
    )
    if s2_result["stopped"]:
        return {"stage": "s2", "arxiv": arxiv_result, "s2": s2_result, "audit": None}
    audit_result = run_m1_audit_member(
        mssv,
        drive_root=drive_root,
        local_root=local_root,
        cohort_limit=cohort_limit,
        budget_seconds=remaining_budget(),
    )
    return {
        "stage": "audit",
        "mssv": mssv,
        "selection_fingerprint": arxiv_result["selection_fingerprint"],
        "arxiv": arxiv_result,
        "s2": s2_result,
        "audit": audit_result,
        "handoff_status": "verified" if audit_result.get("is_reconciled") else "blocked",
        "export": audit_result.get("export"),
    }
