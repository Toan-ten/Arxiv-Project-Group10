"""Read-only M1 validation and explicit working/official table generation."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Callable

from src.fast_production import FastProductionRunner, cohort_pairs, verify_cohort_handoff
from src.session_evidence import SessionBudget, finish_session_envelope, observe_stage
from src.storage import _atomic_json
from .arxiv_phase import _kaggle_source_provenance_issue, _source_state_manifest
from .benchmark_summary import _member_benchmark_summary
from .checkpoint import (_member_writer_lock, _persist_selection, _read_json,
                         _record_session, _session_start, _write_json)
from .common import PHASE_SCHEMA_VERSION, M1PhaseError, build_member_selection
from .s2_evidence import (_cohort_s2_paths, _load_s2_manifest, _read_s2_paper,
                          _validate_s2_cohort)


def _validate_source_paper(runner: FastProductionRunner, mssv: str, arxiv_id: str):
    from src.storage_integrity import audit_paper_integrity
    try:
        integrity = audit_paper_integrity(runner.store, mssv, arxiv_id)
        manifest = runner.store.read_manifest(mssv, arxiv_id)
    except Exception:
        return "missing_or_invalid", None
    if integrity.has_integrity_failure:
        return "integrity_failure", manifest
    if manifest.get("discovery_status") == "not_found" and manifest.get("status") == "not_found":
        return ("not_found", manifest) if manifest.get("versions") == {} else ("integrity_failure", manifest)
    return ("found_source_complete", manifest) if integrity.is_source_complete else ("source_incomplete", manifest)


def run_m1_validation_member(mssv: str, *, drive_root: str | Path,
                             local_root: str | Path, cohort_limit: int | None = None,
                             budget_seconds: float = 10 * 60 * 60,
                             runner_factory: Callable[..., FastProductionRunner] = FastProductionRunner) -> dict:
    """Inspect arXiv and S2 checkpoints and write a report without merging or exporting."""
    from src.storage_integrity import audit_cohort_integrity
    selection = build_member_selection(mssv, cohort_limit)
    drive, local = Path(drive_root).expanduser().resolve(), Path(local_root).expanduser().resolve()
    runner = runner_factory(local_root=local, drive_root=drive)
    budget = SessionBudget(budget_seconds)
    session_id, envelope, started = _session_start(runner, selection, "validation")
    phase_root = _persist_selection(drive, selection)
    progress_path = runner.reports_dir / f"m1_validation_{mssv}_{selection['fingerprint'][:16]}.json"
    saved_progress = {}
    try:
        candidate = _read_json(progress_path)
        if (candidate.get("selection_fingerprint") == selection["fingerprint"]
                and candidate.get("status") in {"running", "interrupted", "budget_exhausted"}):
            saved_progress = candidate
    except (OSError, ValueError, TypeError):
        saved_progress = {}
    if not saved_progress:
        _write_json(progress_path, {
            "schema_version": PHASE_SCHEMA_VERSION, "stage": "validation", "status": "running",
            "mssv": mssv, "selection_fingerprint": selection["fingerprint"],
            "expected_id_count": selection["expected_count"], "papers": {}, "cohorts": {},
            "issues": [], "completed_cohorts": [], "next_cohort_index": 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })

    def validate():
        papers = dict(saved_progress.get("papers") or {})
        s2_by_id, cohorts = {}, dict(saved_progress.get("cohorts") or {})
        issues = list(saved_progress.get("issues") or [])
        completed_cohorts = list(saved_progress.get("completed_cohorts") or [])
        run_status = "completed"
        total_cohorts = len(selection["cohorts"])
        report_path = progress_path
        print(f"[M1/kiểm định] Bắt đầu: {selection['expected_count']} ID, {total_cohorts} lô; tiếp tục từ {len(completed_cohorts)} lô đã kiểm tra.")
        for cohort_index, cohort in enumerate(selection["cohorts"], start=1):
            previous_cohort = cohorts.get(str(cohort))
            previous_handoff = (previous_cohort or {}).get("handoff") or {}
            previous_sha = previous_handoff.get("archive_sha256")
            if cohort in completed_cohorts and previous_sha:
                try:
                    print(f"[M1/kiểm định] Lô {cohort_index}/{total_cohorts}: xác minh archive đã lưu để tiếp tục.")
                    current_handoff = verify_cohort_handoff(runner, cohort, inspect_members=False)
                except KeyboardInterrupt:
                    run_status = "interrupted"
                    issues.append({"cohort": cohort, "issue": "validation_interrupted_verifying_saved_handoff"})
                    break
                except Exception:
                    current_handoff = None
                if current_handoff and current_handoff.get("archive_sha256") == previous_sha:
                    print(f"[M1/kiểm định] Lô {cohort_index}/{total_cohorts}: archive không đổi; dùng kết quả đã lưu.")
                    try:
                        s2_manifest, evidence = _validate_s2_cohort(phase_root, selection, cohort)
                        s2_by_id.update(evidence)
                        previous_cohort["s2_status"] = s2_manifest.get("status")
                        previous_cohort["handoff"] = current_handoff
                    except Exception as exc:
                        issues.append({"cohort": cohort, "issue": "s2_checkpoint_invalid", "detail": str(exc)})
                        previous_cohort["s2_status"] = "invalid_or_incomplete"
                    cohorts[str(cohort)] = previous_cohort
                    _write_json(progress_path, {
                        "schema_version": PHASE_SCHEMA_VERSION, "stage": "validation", "status": "running",
                        "mssv": mssv, "selection_fingerprint": selection["fingerprint"],
                        "expected_id_count": selection["expected_count"], "papers": papers,
                        "cohorts": cohorts, "issues": issues, "completed_cohorts": completed_cohorts,
                        "next_cohort_index": cohort_index + 1,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    })
                    continue
            if budget.should_stop():
                run_status = "budget_exhausted"
                issues.append({"issue": "validation_budget_exhausted", "next_cohort": cohort})
                print(f"[M1/kiểm định] Hết ngân sách trước lô {cohort_index}/{total_cohorts}; đã lưu tiến độ.")
                break
            print(f"[M1/kiểm định] Lô {cohort_index}/{total_cohorts} (số {cohort}): kiểm tra nguồn.")
            state = _source_state_manifest(phase_root / "arxiv" / f"cohort_{cohort:04d}.json", selection, cohort)
            kaggle_issue = _kaggle_source_provenance_issue(runner, state, cohort)
            if kaggle_issue:
                issues.append(kaggle_issue)
            try:
                print(f"[M1/kiểm định] Lô {cohort}: khôi phục an toàn và kiểm tra archive.")
                restored = runner.restore_cohort(cohort, verify_bbl_artifacts=False)
                print(f"[M1/kiểm định] Lô {cohort}: khôi phục xong ({restored} thư mục bài).")
            except KeyboardInterrupt:
                run_status = "interrupted"
                issues.append({"cohort": cohort, "issue": "validation_interrupted_during_restore"})
                print(f"[M1/kiểm định] Đã nhận yêu cầu dừng ở lô {cohort}; thao tác khôi phục có rollback an toàn.")
                break
            try:
                s2_manifest, evidence = _validate_s2_cohort(phase_root, selection, cohort)
                s2_by_id.update(evidence)
                s2_status = s2_manifest.get("status")
            except Exception as exc:
                s2_status = "invalid_or_incomplete"
                issues.append({"cohort": cohort, "issue": "s2_checkpoint_invalid", "detail": str(exc)})
            cohort_papers = [(member, aid) for member, aid in cohort_pairs(cohort) if member == mssv]
            print(f"[M1/kiểm định] Lô {cohort}: kiểm tra {len(cohort_papers)} bài và checkpoint S2.")
            for paper_index, (member, aid) in enumerate(cohort_papers, start=1):
                if paper_index == 1 or paper_index % 10 == 0 or paper_index == len(cohort_papers):
                    print(f"[M1/kiểm định] Lô {cohort}: bài {paper_index}/{len(cohort_papers)}.")
                source_status, manifest = _validate_source_paper(runner, member, aid)
                meta_status = "missing"
                try:
                    meta = runner.store.read_metadata(member, aid)
                    meta_status = meta.get("status", "invalid")
                except Exception:
                    meta = None
                s2 = s2_by_id.get(aid)
                papers[aid] = {
                    "source_status": source_status,
                    "metadata_status": meta_status,
                    "s2_metadata_status": s2[0].get("status") if s2 else "missing",
                    "references_status": s2[1].get("status") if s2 else "missing",
                    "version_count": len((manifest or {}).get("versions") or {}),
                }
            integrity = audit_cohort_integrity(runner.store, cohort_papers, cohort_number=cohort)
            print(f"[M1/kiểm định] Lô {cohort}: xác minh handoff và tính toàn vẹn archive.")
            handoff = verify_cohort_handoff(runner, cohort, inspect_members=True)
            cohorts[str(cohort)] = {"integrity": integrity.to_dict(), "handoff": handoff, "s2_status": s2_status}
            completed_cohorts.append(cohort)
            progress = {"schema_version": PHASE_SCHEMA_VERSION, "stage": "validation",
                        "status": "running", "mssv": mssv,
                        "selection_fingerprint": selection["fingerprint"],
                        "expected_id_count": selection["expected_count"], "papers": papers,
                        "cohorts": cohorts, "issues": issues,
                        "completed_cohorts": completed_cohorts,
                        "next_cohort_index": cohort_index + 1,
                        "updated_at": datetime.now(timezone.utc).isoformat()}
            _write_json(report_path, progress)
            print(f"[M1/kiểm định] Hoàn tất lô {cohort_index}/{total_cohorts}; đã lưu tiến độ.")
        ready_m2 = [aid for aid, row in papers.items() if row["source_status"] == "found_source_complete"]
        ready_m3 = [aid for aid, row in papers.items() if row["source_status"] == "found_source_complete"
                    and row["metadata_status"] == "complete"
                    and row["references_status"] in {"complete", "not_found"}]
        report = {"schema_version": PHASE_SCHEMA_VERSION, "stage": "validation", "status": run_status, "mssv": mssv,
                  "selection_fingerprint": selection["fingerprint"], "scope": selection["scope"],
                  "expected_id_count": selection["expected_count"], "papers": papers,
                  "ready_for_m2": ready_m2, "ready_for_m3": ready_m3,
                  "cohorts": cohorts, "issues": issues, "completed_cohorts": completed_cohorts,
                  "updated_at": datetime.now(timezone.utc).isoformat()}
        _write_json(report_path, report)
        report["report_path"] = str(report_path)
        print(f"[M1/kiểm định] Kết thúc: {run_status}; đã xử lý {len(completed_cohorts)}/{total_cohorts} lô.")
        return report

    try:
        with _member_writer_lock(drive, selection):
            _record_session(phase_root, selection, session_id, "validation")
            try:
                result = observe_stage(runner, "validation", 0, validate, session_id=session_id,
                                       configuration={"selection_fingerprint": selection["fingerprint"], "mssv": mssv})
            except KeyboardInterrupt:
                result = _read_json(progress_path)
                result["status"] = "interrupted"
                result.setdefault("issues", []).append({"issue": "validation_interrupted_by_user"})
                result["updated_at"] = datetime.now(timezone.utc).isoformat()
                _write_json(progress_path, result)
                print("[M1/kiểm định] Đã lưu tiến độ. Chạy lại ô kiểm định với cùng phạm vi để tiếp tục.")
            finish_session_envelope(envelope, started_monotonic=started)
            result["benchmark"] = _member_benchmark_summary(runner, phase_root, selection, result)
            return result
    finally:
        if _read_json(envelope).get("status") == "running":
            finish_session_envelope(envelope, started_monotonic=started)


def _load_evidence(phase_root: Path, selection: dict) -> dict:
    evidence = {}
    for cohort in selection["cohorts"]:
        root, checkpoint = _cohort_s2_paths(phase_root, cohort)
        try:
            manifest = _load_s2_manifest(checkpoint, selection, cohort)
        except M1PhaseError:
            continue
        for aid in manifest["ids"]:
            try:
                metadata, references = _read_s2_paper(manifest, root, aid)
            except Exception:
                continue
            if metadata is not None and references is not None:
                evidence[aid] = (metadata, references)
    return evidence


def run_m1_normalization_member(mssv: str, *, drive_root: str | Path,
                                local_root: str | Path, output_dir: str | Path | None = None,
                                cohort_limit: int | None = None) -> dict:
    """Create explicitly labeled working M1 tables from separate arXiv and S2 evidence."""
    from src.export import export_papers_table, export_to_parquet
    selection = build_member_selection(mssv, cohort_limit)
    drive, local = Path(drive_root).expanduser().resolve(), Path(local_root).expanduser().resolve()
    runner = FastProductionRunner(local_root=local, drive_root=drive)
    phase_root = _persist_selection(drive, selection)
    from src.bbl_handling import audit_bbl_sources
    print(f"[M1/chuẩn hóa] Bắt đầu: {selection['expected_count']} ID.")
    print("[M1/chuẩn hóa] Kiểm tra chính sách BBL và tạo artifact thư mục dẫn xuất.")
    bbl_report = audit_bbl_sources(
        runner.store,
        selection["pairs"],
        output_dir=runner.reports_dir / "derived_bibliography" / selection["fingerprint"],
        report_path=runner.reports_dir / f"bbl_handling_{selection['fingerprint'][:16]}.json",
        session_id=selection["fingerprint"][:16],
    )
    evidence = _load_evidence(phase_root, selection)
    print(f"[M1/chuẩn hóa] Đã đọc bằng chứng S2 cho {len(evidence)} ID.")
    valid_pairs = []
    for index, pair in enumerate(selection["pairs"], start=1):
        if index == 1 or index % 100 == 0 or index == selection["expected_count"]:
            print(f"[M1/chuẩn hóa] Kiểm tra nguồn {index}/{selection['expected_count']}.")
        state, _ = _validate_source_paper(runner, *pair)
        if state == "found_source_complete":
            valid_pairs.append(pair)
    papers = export_papers_table(runner.store, valid_pairs, verified_only=False, require_m1_complete=False)
    refs_by_edge = {}
    for member, aid in valid_pairs:
        entry = evidence.get(aid)
        if entry is None or entry[1].get("status") != "complete":
            continue
        for item in entry[1].get("items", []):
            ref_id = item.get("arxiv_id")
            if not ref_id:
                continue
            row = {"arxiv_id": aid, "ref_arxiv_id": ref_id, "ref_title": item.get("title"),
                         "ref_authors": item.get("authors"),
                         "ref_submission_date": item.get("submission_date") or item.get("publication_date"),
                         "ref_s2_id": item.get("s2_paper_id")}
            key = (aid, ref_id)
            if key not in refs_by_edge:
                refs_by_edge[key] = row
            else:
                for field in ("ref_title", "ref_authors", "ref_submission_date", "ref_s2_id"):
                    if refs_by_edge[key][field] in (None, "", []) and row[field] not in (None, "", []):
                        refs_by_edge[key][field] = row[field]
    refs = list(refs_by_edge.values())
    out = Path(output_dir).resolve() if output_dir else drive / "exports" / selection["fingerprint"] / "working"
    out.mkdir(parents=True, exist_ok=True)
    export_to_parquet(papers, out / "papers.parquet")
    export_to_parquet(refs, out / "references.parquet")
    report = {"stage": "normalization", "is_working_data": True, "selection_fingerprint": selection["fingerprint"],
              "papers_count": len(papers), "references_count": len(refs), "output_dir": str(out),
              "excluded_ids": sorted(set(a for _, a in selection["pairs"]) - {r["arxiv_id"] for r in papers}),
              "bbl_audit": bbl_report}
    _atomic_json(out / "normalization_report.json", report)
    print(f"[M1/chuẩn hóa] Hoàn tất: {len(papers)} bài, {len(refs)} tài liệu tham khảo; lưu tại {out}.")
    return report


def run_m1_official_export_member(mssv: str, *, drive_root: str | Path,
                                  local_root: str | Path, output_dir: str | Path | None = None,
                                  cohort_limit: int | None = None) -> dict:
    """Publish official M1 tables only when every assigned paper passes validation."""
    from src.export import validate_exported_tables
    selection = build_member_selection(mssv, cohort_limit)
    validation = run_m1_validation_member(mssv, drive_root=drive_root, local_root=local_root, cohort_limit=cohort_limit)
    ready = set(validation["ready_for_m3"])
    not_found = {aid for aid, row in validation["papers"].items() if row["source_status"] == "not_found"}
    if len(ready | not_found) != selection["expected_count"]:
        raise RuntimeError("Official M1 export blocked: assigned source or S2 evidence is incomplete.")
    normalized = run_m1_normalization_member(mssv, drive_root=drive_root, local_root=local_root,
                                             output_dir=output_dir, cohort_limit=cohort_limit)
    out = Path(normalized["output_dir"])
    import pyarrow.parquet as pq
    paper_rows, ref_rows = pq.read_table(out / "papers.parquet").to_pylist(), pq.read_table(out / "references.parquet").to_pylist()
    check = validate_exported_tables(paper_rows, ref_rows)
    if not check["is_valid"]:
        raise RuntimeError(f"Official M1 export failed schema validation: {check['issues'][:5]}")
    official = out.parent / "official"
    official.mkdir(parents=True, exist_ok=True)
    import shutil
    shutil.copy2(out / "papers.parquet", official / "papers.parquet")
    shutil.copy2(out / "references.parquet", official / "references.parquet")
    return {"status": "exported", "output_dir": str(official), "validation": check}
