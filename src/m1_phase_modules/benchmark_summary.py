"""M1 member benchmark aggregation from recorded phase observations."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from src.fast_production import FastProductionRunner

from .checkpoint import (
    _read_json,
    _validate_starting_state,
)
from .common import (
    PHASE_SCHEMA_VERSION,
    M1PhaseError,
)


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
    from src.benchmark_metrics import directory_bytes
    from src.session_evidence import (
        fingerprint_complete_corpus,
        summarize_discovery_timings,
        summarize_paper_timings,
        summarize_workflow,
    )

    observations, envelopes = _load_phase_observations(runner, phase_root, selection)
    assigned_ids = [arxiv_id for _member, arxiv_id in selection["pairs"]]
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
    assigned_paper_timings = summarize_paper_timings(observations, assigned_ids)
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

    def measured_tree_size(path: Path | None) -> int | None:
        if path is None or not Path(path).is_dir():
            return None
        try:
            return directory_bytes(Path(path))
        except OSError:
            return None

    corpus_size_bytes = measured_tree_size(runner.corpus_dir)
    archive_sizes = []
    for cohort in selection["cohorts"]:
        archive_path = runner.archives_dir / f"cohort_{cohort:04d}_corpus.tar.gz"
        if not archive_path.is_file():
            archive_sizes = []
            break
        try:
            archive_sizes.append(directory_bytes(archive_path))
        except OSError:
            archive_sizes = []
            break
    archive_size_bytes = sum(archive_sizes) if len(archive_sizes) == len(selection["cohorts"]) else None
    export_info = audit_result.get("export") or {}
    export_dir = export_info.get("output_dir") if audit_result.get("export_status") == "exported" else None
    export_size_bytes = measured_tree_size(Path(export_dir)) if isinstance(export_dir, str) else None
    bbl_artifact_root = getattr(runner, "bbl_artifact_dir", None)
    try:
        bbl_artifact_size_bytes = directory_bytes(Path(bbl_artifact_root)) if bbl_artifact_root else None
    except OSError:
        bbl_artifact_size_bytes = None
    storage_size_complete = all(
        type(value) is int and value >= 0
        for value in (corpus_size_bytes, archive_size_bytes, export_size_bytes)
    )
    storage_size_metrics = {
        "status": "complete" if storage_size_complete else "incomplete",
        "corpus_bytes": corpus_size_bytes,
        "source_archives_bytes": archive_size_bytes,
        "official_export_bytes": export_size_bytes,
        "derived_bibliography_artifacts_bytes": bbl_artifact_size_bytes,
        "derived_bibliography_status": (
            "collected" if type(bbl_artifact_size_bytes) is int else "not_collected"
        ),
        "derived_bibliography_scope": "configured shared artifact root; may include other selections and is not summed across members",
        "final_output_storage_bytes": (
            archive_size_bytes + export_size_bytes
            if archive_size_bytes is not None and export_size_bytes is not None else None
        ),
        "final_output_scope": "selected source archives plus official export directory; local corpus and derived bibliography artifacts are reported separately",
        "size_scope": "logical regular-file bytes; archive size includes selected cohort .tar.gz files, not their sidecars",
    }
    all_m1 = (
        audit_result.get("is_reconciled") is True
        and workflow.get("status") == "measured_intervals"
        and workflow.get("system_ram_status") == "collected"
        and workflow.get("resource_scope_status") == "collected"
        and assigned_paper_timings.get("status") == "complete"
        and discovery_timings.get("status") == "complete"
        and data_signature is not None
        and len(runtime_commits) == 1
        and logical_size_complete
        and storage_size_complete
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
        "process_ram_source": workflow.get("process_ram_source"),
        "process_ram_scope": workflow.get("process_ram_scope"),
        "system_ram_status": workflow.get("system_ram_status"),
        "sampled_peak_system_ram_bytes": workflow.get("sampled_peak_system_ram_bytes"),
        "sampled_average_system_ram_bytes": workflow.get("sampled_average_system_ram_bytes"),
        "system_ram_scope": workflow.get("system_ram_scope"),
        "system_ram_source": workflow.get("system_ram_source"),
        "sampled_peak_disk_bytes": workflow.get("sampled_peak_disk_bytes"),
        "disk_roots": workflow.get("disk_roots"),
        "paper_timings": paper_timings,
        "assigned_paper_timings": assigned_paper_timings,
        "assigned_paper_count": len(assigned_ids),
        "average_assigned_paper_seconds": assigned_paper_timings.get("average_seconds"),
        "discovery_timings": discovery_timings,
        "measurement_scope": (
            "Sequential arXiv source and S2 intervals across recorded sessions; "
            "retry/delay and audit/export session time are included, inter-session gaps "
            "and environment installation are excluded."
        ),
        "resource_measurement_scope": (
            "Crawler RAM is sampled process-tree RSS; system RAM is sampled host-wide "
            "used RAM and includes unrelated Colab processes. Disk is sampled logical "
            "regular-file bytes under each stage observation's disjoint roots. Sampled "
            "peaks can miss short-lived files or memory spikes."
        ),
        "storage_size_metrics": storage_size_metrics,
        "corpus_size_bytes": corpus_size_bytes,
        "archive_size_bytes": archive_size_bytes,
        "export_size_bytes": export_size_bytes,
        "final_output_storage_bytes": storage_size_metrics["final_output_storage_bytes"],
        "final_output_storage_scope": storage_size_metrics["final_output_scope"],
        "measurement_protocol": (
            "m1-independent-phases-v2; source/S2 retry and automatic 429 cooldown included; "
            "all-assigned-ID average; process RSS and host-wide RAM sampled; inter-session gaps excluded"
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
        "reference_arxiv_id_coverage": {
            "identified_records": audit_stats.get("reference_arxiv_id_records"),
            "eligible_records": audit_stats.get("total_complete_reference_records"),
            "coverage_rate_percent": audit_stats.get("reference_arxiv_id_coverage_rate"),
            "definition": audit_stats.get("reference_arxiv_id_coverage_definition"),
        },
        "average_references_per_paper": {
            "value": audit_stats.get("average_references_per_paper"),
            "eligible_papers": audit_stats.get("complete_reference_papers"),
            "raw_reference_records": audit_stats.get("total_complete_reference_records"),
            "definition": audit_stats.get("average_references_per_paper_definition"),
        },
        "missing_stages": workflow.get("missing_stages", []),
        "session_gaps_seconds": workflow.get("session_gaps_seconds"),
    }
    return summary
