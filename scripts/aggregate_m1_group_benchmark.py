#!/usr/bin/env python3
"""Aggregate explicit M1 Colab stage evidence for all 20,000 assigned IDs."""
from __future__ import annotations

import argparse
import json
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
    summary["scope"] = "end_to_end_wall_seconds covers each completed session from snapshot preparation through audit, eligible export and size accounting; environment installation and inter-session gaps are excluded; stage intervals separately support diagnostics"
    summary["member_campaign_count"] = len(member_summaries)
    missing_summaries = sorted(set(MEMBERS) - set(member_summaries))
    expected_ids = {member: 5000 for member in MEMBERS}
    signature_fields = ("selection_fingerprint", "data_signature", "environment_fingerprint",
                        "starting_state_fingerprint", "measurement_protocol")
    if not missing_summaries:
        for member, value in member_summaries.items():
            if (len(value.get("cohorts", [])) != 50 or value.get("expected_id_count") != 5000
                    or value.get("data_signature") is None
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
                                               and summary.get("end_to_end_wall_seconds") is not None)
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
