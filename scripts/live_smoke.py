#!/usr/bin/env python3
"""Controlled one-publication smoke. Default is read-only, offline dry run."""
from __future__ import annotations

import argparse
import copy
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sys
import tempfile
from uuid import uuid4

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import arxiv_client, batch, benchmark_runner, semantic_scholar, s2_integration
from src.storage import CorpusStorage, TERMINAL, _atomic_json, _relative

CODE_ROOT = Path(__file__).resolve().parents[1]
REPORT_ROOT = CODE_ROOT.with_name("Arxiv-Project-Group10-Report")
DEFAULT_PAIR = ("24120010", "2508.07872")
BATCH_ID = "live-smoke"
LOCK_PATH = Path(tempfile.gettempdir()) / "group10-live-smoke.lock"


class SmokeError(ValueError):
    """Fixed, nonsecret diagnostic for a rejected smoke plan."""


class _Once(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        if getattr(namespace, self.dest) is not None:
            raise argparse.ArgumentError(self, "Only one value is allowed")
        setattr(namespace, self.dest, values)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse can echo arbitrary input, including accidentally pasted keys.
        self.exit(2, "Invalid arguments. Use --help; put API keys in the environment.\n")


def parse_args(argv=None):
    parser = _Parser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Explicitly allow real API requests")
    parser.add_argument("--resume", action="store_true", help="Reuse only this tool's verified output")
    for name in ("mssv", "arxiv-id", "output"):
        parser.add_argument("--" + name, action=_Once)
    parser.add_argument("--timeout", type=float, default=15.0, help="Per request, 1..60 seconds")
    parser.add_argument("--s2-interval", type=float, default=2.0, help="S2 pacing, at least 1 second")
    parser.add_argument("--max-pages", type=int, default=3, help="References page cap, 1..3")
    return parser.parse_args(argv)


def _redact(value):
    key = semantic_scholar._key_from_environment()

    def redact(item):
        if isinstance(item, dict):
            return {k: redact(v) for k, v in item.items()}
        if isinstance(item, list):
            return [redact(v) for v in item]
        return item.replace(key, "[REDACTED]") if key and isinstance(item, str) else item

    return redact(value)


def _local_drive(path):
    if os.name != "nt":
        return True
    import ctypes
    drive_type = ctypes.windll.kernel32.GetDriveTypeW
    drive_type.argtypes = [ctypes.c_wchar_p]
    return drive_type(path.anchor) in (2, 3, 6)  # removable, fixed, RAM disk


def _safe_output(value):
    if str(value).startswith(("\\\\", "//")):
        raise SmokeError("Network/UNC output paths are forbidden")
    path = Path(value).expanduser()
    if not path.is_absolute() or ".." in path.parts:
        raise SmokeError("Output must be an absolute path without parent traversal")
    if not _local_drive(path):
        raise SmokeError("Output requires a local drive; mapped network drives are forbidden")
    _relative("/".join(path.parts[1:]))  # Reject device names and unsafe Windows components.
    path = batch._safe(path)
    if path == Path(path.anchor) or any(
            root == path or root.is_relative_to(path) or path.is_relative_to(root)
            for root in (CODE_ROOT.resolve(), REPORT_ROOT.resolve())):
        raise SmokeError("Output must be outside both repositories and their ancestor directories")
    if any((parent / ".git").exists() for parent in (path, *path.parents)):
        raise SmokeError("Output inside a Git repository is forbidden")
    key = semantic_scholar._key_from_environment()
    if key and key in str(path):
        raise SmokeError("Output path contains a secret")
    return path


def _marker(plan):
    return dict(schema_version=1, kind="group10-controlled-live-smoke",
                output=plan["output"], pair=plan["pair"], versions=["v1"],
                timeout=plan["timeout"], max_attempts=1, s2_config=plan["s2_config"])


def _resume_preflight(plan):
    root = Path(plan["output"])
    if batch._read(root / "smoke.json") != _marker(plan):
        raise SmokeError("Output marker, identity or configuration does not match")
    if {p.name for p in root.iterdir()} - {"smoke.json", "corpus", "logs", "reports"}:
        raise SmokeError("Unexpected files in smoke output; audit before reuse")
    pair = tuple(plan["pair"])
    store = CorpusStorage(root / "corpus")
    for name in ("corpus", "logs", "reports"):
        batch._safe(root / name)
    if {p.name for p in store.root.iterdir()} != {store.paper_path(*pair).name}:
        raise SmokeError("Corpus is not exactly the selected publication")
    if {p.name for p in (root / "logs").iterdir()} != {BATCH_ID}:
        raise SmokeError("Output contains unrelated batch logs")
    definition, history = benchmark_runner._load(root / "logs" / BATCH_ID, BATCH_ID, store.root)
    expected = benchmark_runner._definition(
        [pair], store.root, 1, ("v1",), plan["timeout"], 1, arxiv_client.USER_AGENT, plan["s2_config"])
    if definition != expected:
        raise SmokeError("Batch configuration does not match")
    s2_integration.validate_checkpoint(store, *pair)
    metadata, refs = store.read_metadata(*pair), store.read_references(*pair)
    if _count_conflict(metadata, refs):
        raise SmokeError("Reference count conflicts with metadata; review before reuse")
    manifest = store.read_manifest(*pair)
    store._sync(copy.deepcopy(manifest), store.read_metadata(*pair), store.read_references(*pair))
    for version, record in manifest["versions"].items():
        if version != "v1" and record["status"] != "pending":
            raise SmokeError("A nonselected source version has been processed")
        if record["status"] in TERMINAL:
            if not store.should_skip_version(*pair, version):
                raise SmokeError("Source checksum is not valid")
        elif store.version_destination(*pair, version).exists():
            raise SmokeError("Unfinished or orphan source requires audit")
    # A manual restart after a policy stop needs a separate quota/cooldown review.
    return any((attempt.get("stop_reason") or {}).get("http_status") in (403, 429)
               for attempt in history)


def make_plan(options):
    pair = (options.mssv if options.mssv is not None else DEFAULT_PAIR[0],
            options.arxiv_id if options.arxiv_id is not None else DEFAULT_PAIR[1])
    if not (math.isfinite(options.timeout) and 1 <= options.timeout <= 60
            and math.isfinite(options.s2_interval) and options.s2_interval >= 1
            and 1 <= options.max_pages <= 3):
        raise SmokeError("Invalid timeout, S2 interval or references page cap")
    if options.resume and options.output is None:
        raise SmokeError("Resume requires an explicit output path")
    default = Path.home() / "arxiv-group10-live-smoke" / (
        datetime.now(timezone.utc).strftime("smoke-%Y%m%dT%H%M%SZ-") + uuid4().hex[:12])
    root = _safe_output(options.output if options.output is not None else default)
    pipeline_store = CorpusStorage(root / "corpus")
    from src import pipeline
    pipeline._inputs([pair], pipeline_store, options.timeout, 1, arxiv_client.USER_AGENT)
    config = s2_integration.configuration(dict(
        timeout=options.timeout, max_attempts=1, min_interval=options.s2_interval,
        reference_limit=100, max_pages=options.max_pages))
    plan = dict(mode="live" if options.live else "dry_run", resume=options.resume,
                pair=list(pair), versions=["v1"], timeout=options.timeout, max_attempts=1,
                s2_config=config, output=str(root), corpus_root=str(root / "corpus"),
                log_root=str(root / "logs"), request_bound=3 + options.max_pages,
                actual_http_requests=None, http_metrics_status="not_collected",
                arxiv_pacing_seconds=arxiv_client.MIN_INTERVAL,
                global_lock_present=LOCK_PATH.exists(), resume_blocked_by_http_policy=False)
    if options.resume:
        if not root.is_dir():
            raise SmokeError("Resume output does not exist")
        plan["resume_blocked_by_http_policy"] = _resume_preflight(plan)
    elif root.exists():
        raise SmokeError("Output already exists; select a new directory or explicit resume")
    return plan


def _count_conflict(metadata, refs):
    provider = metadata["provenance"]["semantic_scholar"] or {}
    hint = ((provider.get("snapshot") or {}).get("paper") or {}).get("referenceCount")
    return refs["status"] == "complete" and type(hint) is int and hint != len(refs["items"])


def verify_output(plan):
    """Read existing logs + Storage/checksums without repairing any checkpoint."""
    root, pair = Path(plan["output"]), tuple(plan["pair"])
    audit = benchmark_runner.aggregate_batch(root / "corpus", batch_id=BATCH_ID, log_root=root / "logs")
    result = dict(integrity_status="verified" if audit["status"] == "verified" else "invalid",
                  aggregation=audit, metadata_status=None, s2_metadata_status=None,
                  s2_identity_verified=False, references=None, source_v1=None,
                  discovery=None, s2_metadata_snapshot_http_status=None,
                  reference_coverage_verified=False, publication_complete_verified=False,
                  metadata_reference_count_hint=None, coverage_issue=None)
    try:
        store = CorpusStorage(root / "corpus")
        manifest, metadata, refs = (store.read_manifest(*pair), store.read_metadata(*pair),
                                    store.read_references(*pair))
        s2_integration.validate_checkpoint(store, *pair, for_resume=False)
        result["metadata_status"] = metadata["status"]
        arxiv = (metadata["provenance"]["arxiv"] or {}).get("snapshot") or {}
        result["discovery"] = dict(status=manifest["discovery_status"],
                                   snapshot_http_status=arxiv.get("http_status"))
        provenance = metadata["provenance"]["semantic_scholar"]
        result["s2_metadata_status"] = None if provenance is None else provenance["snapshot"]["status"]
        result["s2_metadata_snapshot_http_status"] = (
            None if provenance is None else provenance["snapshot"]["http_status"])
        result["s2_identity_verified"] = s2_integration.metadata_cached(metadata)
        result["metadata_reference_count_hint"] = (
            ((provenance or {}).get("snapshot") or {}).get("paper") or {}).get("referenceCount")
        conflict = _count_conflict(metadata, refs)
        result["coverage_issue"] = "metadata_reference_count_disagrees" if conflict else None
        result["reference_coverage_verified"] = refs["status"] == "complete" and not conflict
        result["publication_complete_verified"] = bool(
            result["reference_coverage_verified"] and audit.get("publication_complete")
            and audit["publication_complete"]["all_complete"] and audit["status"] == "verified")
        result["references"] = dict(
            status=refs["status"], coverage=refs["coverage"],
            error_type=(refs.get("error") or {}).get("type"),
            http_status=(refs.get("error") or {}).get("http_status"),
            missing_arxiv_items=sum(r["arxiv_id"] is None for r in refs["items"]))
        record = manifest["versions"].get("v1")
        if record:
            verified = record["status"] in TERMINAL and store.should_skip_version(*pair, "v1")
            result["source_v1"] = dict(status=record["status"], verified=verified,
                                       files=len(record["files"]), kept_bytes=record["kept_bytes"],
                                       snapshot_http_status=record["http_status"])
    except (OSError, ValueError, KeyError, TypeError):
        result["integrity_status"] = "invalid"
    return result


@contextmanager
def _single_writer():
    # Global to this tool/user TEMP, including invocations with different outputs.
    lock = batch._safe(LOCK_PATH)
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise SmokeError("Smoke lock exists; inspect the previous process before retrying") from None
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(dict(pid=os.getpid(), started_at=datetime.now(timezone.utc).isoformat()), stream)
        yield
    finally:
        try:
            lock.unlink()  # Only this invocation's own lock; never a stale lock.
        except OSError:
            # Preserve an already recorded HTTP stop/result rather than masking it.
            print("Smoke lock cleanup failed; inspect it before retrying", file=sys.stderr)


def _execute_live(plan):
    """Private execution boundary. Tests replace HTTP; CLI calls only after --live."""
    if plan["mode"] != "live":
        raise SmokeError("Explicit live authorization is required")
    root = _safe_output(plan["output"])
    semantic_scholar._key_from_environment()  # Validate without logging the value.
    if arxiv_client.MIN_INTERVAL < 3:
        raise SmokeError("arXiv pacing must be at least three seconds")
    with _single_writer():
        # Recheck ownership/path/checkpoint immediately before any writes or HTTP.
        if plan["resume"]:
            if _resume_preflight(plan):
                raise SmokeError("Previous 403/429 requires review; live resume is blocked")
        else:
            root.mkdir(parents=True, exist_ok=False)
            _atomic_json(root / "smoke.json", _marker(plan))
        reports = batch._safe(root / "reports")
        reports.mkdir(exist_ok=True)
        destination = reports / (uuid4().hex + ".json")
        if destination.exists():
            raise SmokeError("Report path already exists")
        execution_status, error_type, outcome = "returned", None, None
        try:
            outcome = batch.run_batch(
                [tuple(plan["pair"])], root / "corpus", batch_id=BATCH_ID, max_ids=1,
                log_root=root / "logs", resume=plan["resume"], versions=("v1",),
                timeout=plan["timeout"], max_attempts=1, s2_config=plan["s2_config"],
                revision_history=False)
        except (KeyboardInterrupt, SystemExit):
            execution_status, error_type = "interrupted", "interrupted"
        except Exception as exc:
            execution_status, error_type = "raised", type(exc).__name__
        verification = verify_output(plan)
        source = verification["source_v1"] or {}
        refs = verification["references"] or {}
        if execution_status != "returned" or verification["integrity_status"] != "verified":
            status = "failed"
        elif outcome["stopped"]:
            status = "stopped"
        elif (source.get("verified") and source.get("status") == "success"
              and verification["s2_identity_verified"] and verification["reference_coverage_verified"]):
            status = "verified"
        else:
            status = "partial"
        report = _redact(dict(
            schema_version=1, plan=plan, execution_status=execution_status, execution_error=error_type,
            batch_status=None if outcome is None else outcome["status"], smoke_status=status,
            stop_reason=None if outcome is None else outcome["stop_reason"],
            environment=benchmark_runner._environment(), verification=verification,
            report_path=str(destination)))
        _atomic_json(destination, report)
        code = 130 if execution_status == "interrupted" else {
            "verified": 0, "partial": 2, "stopped": 3, "failed": 1}[status]
        return report, code


def main(argv=None):
    try:
        options = parse_args(argv)
        plan = make_plan(options)
        if not options.live:
            payload = dict(plan=plan, message="DRY RUN: no network and no output writes")
            if options.resume:
                payload["verification"] = verify_output(plan)
            print(json.dumps(_redact(payload), ensure_ascii=False, indent=2))
            return 0
        report, code = _execute_live(plan)
        print(json.dumps(_redact(report), ensure_ascii=False, indent=2))
        return code
    except SmokeError as exc:
        print("Smoke refused: " + str(exc), file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        # Never print arbitrary exception text, response bodies or credentials.
        print("Smoke refused: " + type(exc).__name__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

