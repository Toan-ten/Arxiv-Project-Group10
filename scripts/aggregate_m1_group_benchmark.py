#!/usr/bin/env python3
"""Aggregate explicit M1 Colab stage evidence for all 20,000 assigned IDs."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.fast_production import member_cohort_range
from src.session_evidence import aggregate_group_observations, compare_workflows
from src.storage import _atomic_json

MEMBERS = ["24120010", "24120035", "24120232", "24120260"]


def load_observations(base: Path, session_ids: set[str]) -> tuple[list[dict], list[str]]:
    rows, errors = [], []
    roots = [base / "shared" / "reports" / "stage_observations"]
    roots.extend(base / member / "reports" / "stage_observations" for member in MEMBERS)
    seen = set()
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*.json")):
            if path.name.split("_", 1)[0] not in session_ids:
                continue
            try:
                row = json.loads(path.read_text(encoding="utf-8"))
                identity = row.get("observation_id")
                if not isinstance(identity, str) or identity in seen:
                    raise ValueError("missing_or_duplicate_observation_id")
                seen.add(identity)
                rows.append(row)
            except (OSError, ValueError, TypeError) as exc:
                errors.append(f"invalid_observation:{path.name}:{type(exc).__name__}")
    return rows, errors


def load_member_summaries(base: Path) -> tuple[dict[str, dict], list[str]]:
    summaries, errors = {}, []
    for member in MEMBERS:
        candidates = []
        for path in sorted((base / member / "reports").glob("benchmark_observed_*.json")):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if (value.get("mssv") == member
                        and value.get("run_scope") == "member_full_assignment"):
                    candidates.append((path.stat().st_mtime_ns, value))
            except (OSError, ValueError, TypeError):
                errors.append(f"invalid_member_benchmark_summary:{member}:{path.name}")
        if candidates:
            summaries[member] = max(candidates, key=lambda item: item[0])[1]
    return summaries, errors


def load_session_envelopes(base: Path, session_ids: set[str]) -> tuple[list[dict], list[str]]:
    rows, errors = [], []
    roots = [base / "shared" / "reports" / "session_envelopes"]
    roots.extend(base / member / "reports" / "session_envelopes" for member in MEMBERS)
    for session_id in sorted(session_ids):
        matches = [root / f"{session_id}.json" for root in roots
                   if (root / f"{session_id}.json").is_file()]
        if len(matches) != 1:
            rows.append({})
            reason = "missing" if not matches else "duplicate"
            errors.append(f"{reason}_session_envelope:{session_id}")
            continue
        try:
            rows.append(json.loads(matches[0].read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            rows.append({})
            errors.append(f"invalid_session_envelope:{session_id}")
    return rows, errors


def aggregate(base: Path) -> dict:
    member_summaries, errors = load_member_summaries(base)
    session_ids = {sid for value in member_summaries.values() for sid in value.get("session_ids", [])}
    rows, observation_errors = load_observations(base, session_ids)
    envelopes, envelope_errors = load_session_envelopes(base, session_ids)
    errors.extend(observation_errors)
    errors.extend(envelope_errors)
    summary = aggregate_group_observations(
        rows, members=MEMBERS,
        cohorts_by_member={member: member_cohort_range(member) for member in MEMBERS},
        session_envelopes=envelopes,
    )
    # The group audit is executed once by each member; require all four records.
    audited_members = {((row.get("configuration") or {}).get("mssv")) for row in rows
                       if row.get("stage") == "audit" and row.get("cohort") == 0
                       and row.get("execution_status") == "returned"
                       and (row.get("result") or {}).get("is_reconciled") is True
                       and (row.get("result") or {}).get("integrity_failed_papers", 0) == 0}
    missing_audits = sorted(set(MEMBERS) - audited_members)
    if missing_audits:
        summary["missing_stages"].extend([["audit", 0, member] for member in missing_audits])
        summary["status"] = "incomplete"
        summary["active_seconds"] = None
        summary["end_to_end_complete"] = False
    failed_handoffs = [row for row in rows if row.get("stage") == "handoff_audit"
                       and row.get("execution_status") == "returned"
                       and (row.get("result") or {}).get("archive_state") not in
                       {"m1_handoff_verified", "handoff_verified"}]
    if failed_handoffs:
        summary["failed_handoff_count"] = len(failed_handoffs)
        summary["status"] = "incomplete"
        summary["active_seconds"] = None
        summary["end_to_end_complete"] = False
    if errors:
        summary["status"] = "incomplete"
        summary["active_seconds"] = None
        summary["end_to_end_complete"] = False
    summary["observation_errors"] = errors
    summary["observed_interval_count"] = len(rows)
    summary["scope"] = ("end_to_end_wall_seconds sums each completed sequential phase session, including audit/export; "
                         "environment installation and inter-session gaps are excluded. RAM is sampled process RSS; "
                         "disk is sampled logical regular-file bytes under the disjoint roots recorded per stage; "
                         "sampled peaks may miss short-lived spikes. Stage intervals separately support diagnostics.")
    summary["member_campaign_count"] = len(member_summaries)
    missing_summaries = sorted(set(MEMBERS) - set(member_summaries))
    paper_rows = []
    discovery_seconds = []
    measurement_missing = []
    for member in MEMBERS:
        value = member_summaries.get(member, {})
        paper_metrics = value.get("paper_timings") or {}
        discovery = value.get("discovery_timings") or {}
        rows_for_member = paper_metrics.get("papers")
        seconds = discovery.get("wall_seconds")
        if (paper_metrics.get("status") != "complete" or not isinstance(rows_for_member, list)
                or not rows_for_member or discovery.get("status") != "complete"
                or type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0):
            measurement_missing.append(member)
            continue
        paper_rows.extend(rows_for_member)
        discovery_seconds.append(seconds)
    ids = [row.get("arxiv_id") for row in paper_rows if isinstance(row, dict)]
    durations = [row.get("total_seconds") for row in paper_rows if isinstance(row, dict)]
    measurements_valid = (not measurement_missing and len(ids) == len(paper_rows) and len(set(ids)) == len(ids)
                          and all(isinstance(aid, str) and aid for aid in ids)
                          and all(type(v) in (int, float) and math.isfinite(v) and v >= 0 for v in durations))
    summary["paper_timing_count"] = len(paper_rows)
    summary["average_paper_seconds"] = sum(durations) / len(durations) if measurements_valid else None
    summary["discovery_wall_seconds"] = sum(discovery_seconds) if measurements_valid else None
    summary["paper_and_discovery_measurement_status"] = "complete" if measurements_valid else "incomplete"
    size_rows = [member_summaries.get(member, {}).get("logical_size_metrics") for member in MEMBERS]
    size_valid = all(
        isinstance(row, dict)
        and type(row.get("logical_size_measured_papers")) is int
        and row["logical_size_measured_papers"] >= 0
        and isinstance(row.get("logical_size_scope"), str)
        for row in size_rows
    )
    if size_valid and len({row["logical_size_scope"] for row in size_rows}) != 1:
        size_valid = False
    size_count = sum(row["logical_size_measured_papers"] for row in size_rows) if size_valid else 0
    before_values = [row.get("average_logical_before_bytes_per_paper") for row in size_rows] if size_valid else []
    after_values = [row.get("average_logical_after_bytes_per_paper") for row in size_rows] if size_valid else []
    if size_valid:
        size_valid = all(
            (count == 0 and before is None and after is None)
            or (count > 0 and type(before) in (int, float) and math.isfinite(before) and before >= 0
                and type(after) in (int, float) and math.isfinite(after) and after >= 0)
            for count, before, after in zip(
                (row["logical_size_measured_papers"] for row in size_rows), before_values, after_values
            )
        )
    if size_valid:
        summary["logical_size_metrics"] = {
            "status": "complete" if size_count else "not_applicable_no_found_source",
            "logical_size_measured_papers": size_count,
            "average_logical_before_bytes_per_paper": (
                sum(row["logical_size_measured_papers"] * row["average_logical_before_bytes_per_paper"]
                    for row in size_rows) / size_count if size_count else None
            ),
            "average_logical_after_bytes_per_paper": (
                sum(row["logical_size_measured_papers"] * row["average_logical_after_bytes_per_paper"]
                    for row in size_rows) / size_count if size_count else None
            ),
            "logical_size_scope": size_rows[0]["logical_size_scope"],
        }
    else:
        summary["logical_size_metrics"] = {"status": "incomplete"}
        summary["status"] = "incomplete"
        summary["active_seconds"] = None
        summary["end_to_end_complete"] = False
    reference_rows = [member_summaries.get(member, {}).get("reference_metadata_success") for member in MEMBERS]
    reference_valid = all(
        isinstance(row, dict)
        and type(row.get("complete_records")) is int
        and type(row.get("eligible_records")) is int
        and 0 <= row["complete_records"] <= row["eligible_records"]
        and isinstance(row.get("definition"), str)
        for row in reference_rows
    )
    if reference_valid and len({row["definition"] for row in reference_rows}) == 1:
        reference_successes = sum(row["complete_records"] for row in reference_rows)
        reference_eligible = sum(row["eligible_records"] for row in reference_rows)
        summary["reference_metadata_success"] = {
            "status": "complete" if reference_eligible else "not_applicable_no_complete_references",
            "complete_records": reference_successes,
            "eligible_records": reference_eligible,
            "success_rate_percent": (
                100.0 * reference_successes / reference_eligible if reference_eligible else None
            ),
            "definition": reference_rows[0]["definition"],
        }
    else:
        summary["reference_metadata_success"] = {"status": "incomplete"}
        summary["status"] = "incomplete"
        summary["active_seconds"] = None
        summary["end_to_end_complete"] = False
    expected_ids = {member: 5000 for member in MEMBERS}
    signature_fields = ("selection_fingerprint", "data_signature", "environment_fingerprint",
                        "starting_state_fingerprint", "measurement_protocol")
    if not missing_summaries:
        for member, value in member_summaries.items():
            if (len(value.get("cohorts", [])) != 50 or value.get("expected_id_count") != 5000
                    or value.get("data_signature") is None
                    or not value.get("starting_state_fingerprint")
                    or not value.get("measurement_protocol")
                    or not value.get("runtime_commit")
                    or value.get("end_to_end_complete") is not True
                    or not value.get("campaign_id")
                    or value.get("discovery_arm") in {None, "unknown", "mixed_or_unknown"}):
                missing_summaries.append(member)
        summary["member_campaign_ids"] = {m: v.get("campaign_id") for m, v in member_summaries.items()}
        summary["member_expected_ids"] = expected_ids
        for field in signature_fields:
            values = [member_summaries[m].get(field) for m in MEMBERS]
            if any(not value for value in values):
                summary[field] = None
            elif field == "selection_fingerprint":
                # Each member notebook records the full group selection hash.
                summary[field] = values[0] if len(set(values)) == 1 else None
            else:
                summary[field] = __import__("hashlib").sha256(
                    json.dumps(dict(zip(MEMBERS, values)), sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
        arms_by_member = {member: member_summaries[member].get("discovery_arm") for member in MEMBERS}
        summary["member_discovery_arms"] = arms_by_member
        runtime_commits = {member_summaries[member].get("runtime_commit") for member in MEMBERS}
        summary["runtime_commits"] = sorted(commit for commit in runtime_commits if commit)
        if len(runtime_commits) != 1 or None in runtime_commits:
            summary["status"] = "incomplete"
            summary["active_seconds"] = None
            summary["end_to_end_complete"] = False
        arms = {arm for arm in arms_by_member.values() if arm is not None}
        summary["discovery_arm"] = next(iter(arms)) if len(arms) == 1 else "mixed_fallback"
    if missing_summaries or errors:
        summary["missing_member_summaries"] = sorted(set(missing_summaries))
        summary["status"] = "incomplete"
        summary["active_seconds"] = None
        summary["end_to_end_complete"] = False
    elif summary.get("status") == "measured_intervals":
        summary["end_to_end_complete"] = bool(summary.get("expected_id_count") == 20000
                                               and summary.get("data_signature")
                                               and summary.get("end_to_end_wall_seconds") is not None
                                               and measurements_valid)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-dir", type=Path, required=True,
                        help="Drive root containing shared/ and each member folder")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--compare", nargs=2, type=Path, metavar=("KAGGLE_JSON", "ARXIV_ONLY_JSON"),
                        help="Compare two previously aggregated summaries; requires identical recorded scope fingerprints")
    args = parser.parse_args()
    if args.compare:
        left = json.loads(args.compare[0].read_text(encoding="utf-8"))
        right = json.loads(args.compare[1].read_text(encoding="utf-8"))
        result = compare_workflows(left, right)
        payload = {"comparison": result, "kaggle": left, "arxiv_only": right}
    else:
        payload = aggregate(args.base_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    _atomic_json(args.output, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("end_to_end_complete", payload.get("comparison", {}).get("eligible", False)) else 2


if __name__ == "__main__":
    raise SystemExit(main())
