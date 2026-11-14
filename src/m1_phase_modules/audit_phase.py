"""M1 reconciliation, verified reference-date enrichment and export gating."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from src.fast_production import (
    FastProductionRunner,
    cohort_pairs,
)
from src.session_evidence import SessionBudget, finish_session_envelope, observe_stage
from src.storage import IntegrityError, StorageError

from .arxiv_phase import (
    _kaggle_source_provenance_issue,
    _source_state_manifest,
)
from .benchmark_summary import (
    _member_benchmark_summary,
)
from .checkpoint import (
    _member_writer_lock,
    _persist_selection,
    _read_json,
    _record_session,
    _session_start,
    _write_json,
)
from .common import (
    PHASE_SCHEMA_VERSION,
    M1PhaseError,
    build_member_selection,
)
from .s2_evidence import (
    _validate_s2_cohort,
)


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


def run_m1_audit_member(
    mssv: str,
    *,
    drive_root: str | Path,
    local_root: str | Path,
    cohort_limit: int | None = None,
    output_dir: str | Path | None = None,
    budget_seconds: float = 10 * 60 * 60,
    runner_factory: Callable[..., FastProductionRunner],
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
    runner = runner_factory(local_root=local, drive_root=drive)
    budget = SessionBudget(budget_seconds)
    session_id, envelope, started = _session_start(runner, selection, "audit")
    phase_root = _persist_selection(drive, selection)

    def audit_and_reconcile() -> dict:
        from src.kaggle_candidates import validate_discrepancy_checkpoints

        source_issues = []
        kaggle_source_issues = []
        source_states = {}
        discrepancy_checkpoint_root = phase_root / "arxiv" / "kaggle_discrepancy_checkpoints"
        discrepancy_evidence_root = phase_root / "arxiv" / "kaggle_discrepancies"
        kaggle_evidence_issues = []
        kaggle_evidence_summary = {"status": "valid", "verified_ids": 0}
        try:
            verified_discrepancies = validate_discrepancy_checkpoints(
                discrepancy_checkpoint_root, discrepancy_evidence_root,
            )
            selected_ids = {arxiv_id for _member, arxiv_id in selection["pairs"]}
            unrelated = [item.get("arxiv_id") for item in verified_discrepancies
                         if item.get("arxiv_id") not in selected_ids]
            if unrelated:
                raise ValueError("discrepancy checkpoint contains IDs outside this selection")
            kaggle_evidence_summary["verified_ids"] = len(verified_discrepancies)
            kaggle_evidence_summary["checkpoints"] = verified_discrepancies
        except (OSError, ValueError, KeyError, TypeError) as exc:
            kaggle_evidence_summary = {"status": "invalid", "error_type": type(exc).__name__}
            kaggle_evidence_issues.append({
                "issue": "kaggle_discrepancy_evidence_invalid",
                "error_type": type(exc).__name__,
            })
        for cohort in selection["cohorts"]:
            state_path = phase_root / "arxiv" / f"cohort_{cohort:04d}.json"
            state = _source_state_manifest(state_path, selection, cohort)
            source_states[str(cohort)] = {
                key: state.get(key) for key in (
                    "status", "source_complete_papers", "expected_count",
                    "archive_sha256", "stop_reason", "updated_at",
                    "kaggle_status", "kaggle_snapshot_sha256",
                    "kaggle_index_sha256", "kaggle_policy_version", "kaggle_session_id",
                )
            }
            kaggle_issue = _kaggle_source_provenance_issue(runner, state, cohort)
            if kaggle_issue is not None:
                kaggle_source_issues.append(kaggle_issue)
            # Tiếp tục và xác minh mọi archive đã có trong phạm vi này.
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
        issues = (
            source_issues + kaggle_source_issues + s2_issues
            + preflight_issues + kaggle_evidence_issues
        )
        merged_ids = []
        if not issues:
            if budget.should_stop():
                merge_conflicts.append({"issue": "audit_session_budget"})
            else:
                try:
                    merged_ids = _merge_s2(runner, selection, s2_evidence)
                    # Làm mới archive bền vững sau khi đối soát S2; giữ lại
                    # archive của pha mã nguồn trước khi công bố bản thay thế.
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
        reference_dates = None
        from src.s2_quality import assess_references, summarize_quality
        reference_quality = summarize_quality({
            aid: assess_references(metadata, references)
            for aid, (metadata, references) in s2_evidence.items()
        })
        if data_complete:
            from src.reference_dates import enrich_reference_dates
            from src import http_metrics
            import time

            date_metrics = http_metrics.HttpMetrics()
            date_started = time.perf_counter()
            with http_metrics.collect(date_metrics):
                reference_dates = enrich_reference_dates(
                    runner.store, [tuple(pair) for pair in selection["pairs"]],
                    phase_root / "reference_dates", should_stop=budget.should_stop,
                )
            reference_dates["wall_seconds"] = time.perf_counter() - date_started
            reference_dates["http_metrics"] = date_metrics.snapshot(complete=True)
            # Full evidence stays in the checksum-verified cache, not every report.
            reference_dates.pop("entries", None)
            if reference_dates["stopped"]:
                data_complete = False
                export_error = "Export deferred: reference-date enrichment stopped; resume with the same selection."
            else:
                destination = Path(output_dir).expanduser().resolve() if output_dir else drive / "exports" / selection["fingerprint"]
                export_result = export_official_datasets(
                    runner.store,
                    [tuple(pair) for pair in selection["pairs"]],
                    destination,
                    require_m1_complete=True,
                    reference_date_cache=phase_root / "reference_dates",
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
            "kaggle_source_issues": kaggle_source_issues,
            "source_checkpoints": source_states,
            "kaggle_evidence": kaggle_evidence_summary,
            "kaggle_evidence_issues": kaggle_evidence_issues,
            "s2_issues": s2_issues,
            "reference_quality": reference_quality,
            "reference_dates": reference_dates,
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
            "stop_reason": ("reference_date_enrichment_stopped" if reference_dates and reference_dates["stopped"]
                            else None if data_complete else "incomplete_or_conflicting_phase_evidence"),
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
