"""S2 checkpoint manifests and identity-checked evidence persistence."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.fast_production import cohort_pairs
from src.storage import arxiv_id_to_folder_key

from .checkpoint import (
    _ensure_directory,
    _read_json,
    _sha256_file,
    _write_json,
)
from .common import (
    PHASE_SCHEMA_VERSION,
    M1PhaseError,
)


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
