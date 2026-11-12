"""Sequential integration for explicitly selected assigned papers.

No requests or output are produced on import. Calls use the existing HTTP
client; offline callers/tests must provide fake HTTP. Single writer only.
"""
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import math
import json
import os
from pathlib import Path
import re
import time

from src import arxiv_client, arxiv_source, arxiv_history, semantic_scholar, s2_integration
from src.storage import CorpusStorage, StorageError, TERMINAL, RETRYABLE
from src.arxiv_version_page import verify_version_withdrawal


class PipelineStopped(RuntimeError):
    """A fatal error stopped the run; summary contains progress so far."""

    def __init__(self, message, summary):
        super().__init__(message)
        self.summary = summary


class _HttpPolicyStop(Exception):
    """Internal signal to stop the batch for a final 403/429 result."""

    def __init__(self, paper, stage, http_status, version, *, checkpoint_saved=True,
                 attempts=None, retry_wait_budget_seconds=None):
        self.reason = dict(
            type="http_policy", stage=stage, http_status=http_status,
            mssv=paper["mssv"], arxiv_id=paper["arxiv_id"], version=version,
            checkpoint_saved=checkpoint_saved,
        )


        if type(attempts) is int and attempts >= 0:
            self.reason["attempts"] = attempts
        if (isinstance(retry_wait_budget_seconds, (int, float))
                and not isinstance(retry_wait_budget_seconds, bool)
                and math.isfinite(retry_wait_budget_seconds)
                and retry_wait_budget_seconds > 0):
            self.reason["retry_wait_budget_seconds"] = float(retry_wait_budget_seconds)
        super().__init__(f"Batch stopped after HTTP {http_status} at {stage}")


DISCOVERY_MAX_AGE = timedelta(days=30)
_KAGGLE_INDEX_CACHE = {}


def _load_kaggle_index(index_path):
    """Load and checksum a candidate index once, rechecking only file stats per ID."""
    from src.kaggle_candidates import _verified_file_sha256

    path = Path(index_path).expanduser().resolve()
    snapshot_path = os.environ.get("M1_KAGGLE_SNAPSHOT_PATH")
    expected_index_sha = os.environ.get("M1_KAGGLE_INDEX_SHA256")
    expected_snapshot_sha = os.environ.get("M1_KAGGLE_SNAPSHOT_SHA256")
    index_stat = path.stat()
    snapshot_stat = Path(snapshot_path).stat() if snapshot_path else None
    signature = (
        index_stat.st_dev, index_stat.st_ino, index_stat.st_size, index_stat.st_mtime_ns,
        None if snapshot_stat is None else (
            snapshot_stat.st_dev, snapshot_stat.st_ino,
            snapshot_stat.st_size, snapshot_stat.st_mtime_ns,
        ),
    )
    cached = _KAGGLE_INDEX_CACHE.get(str(path))
    if cached and cached["signature"] == signature and cached["expected_index_sha"] == expected_index_sha:
        return cached["index"], cached["sha256"]
    payload = path.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    if expected_index_sha and digest != expected_index_sha:
        raise ValueError("Kaggle candidate index checksum mismatch")
    index = json.loads(payload.decode("utf-8"))
    if snapshot_path:
        _verified_file_sha256(snapshot_path, expected_snapshot_sha)
    _KAGGLE_INDEX_CACHE[str(path)] = {
        "signature": signature, "expected_index_sha": expected_index_sha,
        "sha256": digest, "index": index,
    }
    return index, digest


def cached_discovery_is_fresh(metadata, *, now=None):
    """Return whether cached arXiv discovery evidence is valid for 30 days."""
    provenance = metadata.get("provenance", {}).get("arxiv") if isinstance(metadata, dict) else None
    retrieved_at = provenance.get("retrieved_at") if isinstance(provenance, dict) else None
    if not isinstance(retrieved_at, str):
        return False
    try:
        retrieved = datetime.fromisoformat(retrieved_at.replace("Z", "+00:00"))
        current = now or datetime.now(timezone.utc)
        if retrieved.tzinfo is None or current.tzinfo is None:
            return False
        age = current - retrieved
        return timedelta(0) <= age <= DISCOVERY_MAX_AGE
    except (ValueError, TypeError):
        return False


def _inputs(pairs, store, timeout, max_attempts, user_agent):
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or timeout <= 0):
        raise ValueError("timeout must be a positive finite number")
    if type(max_attempts) is not int or max_attempts < 1:
        raise ValueError("max_attempts must be a positive integer")
    if (not isinstance(user_agent, str) or not user_agent.strip()
            or not user_agent.isascii() or "\r" in user_agent or "\n" in user_agent):
        raise ValueError("Invalid user_agent header")
    if not isinstance(pairs, (list, tuple)):
        raise ValueError("pairs must be an explicit list or tuple")
    selected, seen = [], set()
    for pair in pairs:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise ValueError("Each selection must be (MSSV, base_arxiv_id)")
        mssv, arxiv_id = pair
        # Kiểm tra Storage công khai dựa vào ID/cấu hình và xác minh quyền sở hữu.
        # Hàm trả về đường dẫn mà không tạo thư mục bài báo/phiên bản.
        store.paper_path(mssv, arxiv_id)
        if arxiv_id in seen:
            raise ValueError(f"Duplicate arXiv ID: {arxiv_id}")
        seen.add(arxiv_id)
        selected.append((mssv, arxiv_id))
    return selected


def _version_selection(versions):
    if versions is None:
        return None
    if not isinstance(versions, (list, tuple)) or not versions:
        raise ValueError("versions must be None or a nonempty list/tuple of vN names")
    if any(not isinstance(name, str) or re.fullmatch(r"v[1-9][0-9]*", name) is None
           for name in versions):
        raise ValueError("Each selected version must be a canonical vN name, such as v1")
    selected = frozenset(versions)
    if len(selected) != len(versions):
        raise ValueError("Duplicate version names are not allowed")
    return selected


def _error(paper, stage, kind, message, version=None):
    paper["errors"].append(dict(
        stage=stage, type=kind, message=str(message), version=version,
    ))


def _stop_on_http(paper, stage, http_status, version=None, *, attempts=None,
                  retry_wait_budget_seconds=None):
    if http_status in (403, 429):
        raise _HttpPolicyStop(
            paper, stage, http_status, version, attempts=attempts,
            retry_wait_budget_seconds=retry_wait_budget_seconds,
        )


def _checkpoint(paper, manifest):
    paper["paper_status"] = manifest["status"]
    paper["metadata_status"] = manifest["metadata_status"]
    paper["references_status"] = manifest["references_status"]
    paper["discovery"].update(
        status=manifest["discovery_status"], version_status=manifest["version_status"],
    )
    for name, record in manifest["versions"].items():
        source = paper["source"].setdefault(name, dict(action="pending"))
        source.update(status=record["status"], http_status=record["http_status"],
                      attempts=record["attempts"], error=record["error"])
    paper["pending"] = [name for name, record in manifest["versions"].items()
                        if record["status"] == "pending"]


def _cached_snapshot(store, paper):
    provenance = store.read_metadata(paper["mssv"], paper["arxiv_id"])["provenance"].get("arxiv")
    if not isinstance(provenance, dict):
        return None
    snapshot = provenance.get("snapshot") if isinstance(provenance, dict) else None
    if not isinstance(snapshot, dict):
        return None
    if snapshot.get("status") != paper["discovery"]["status"]:
        raise StorageError("Cached discovery status conflicts with its arXiv snapshot")
    # Luồng Kaggle cũ ghi bản chụp vào trường xuất xứ arXiv.
    # Cần xem xét bằng chứng này; không được xem nó là kết quả discovery arXiv.
    if "revised_dates" in snapshot and snapshot.get("http_status") != 200:
        return None
    return snapshot


def _verify_withdrawal_404(store, paper, version, options):
    """Resolve a source 404 only from the exact official arXiv version page."""
    started = time.perf_counter()
    evidence = verify_version_withdrawal(
        paper["arxiv_id"], version, timeout=options["timeout"],
        max_attempts=options["max_attempts"], user_agent=options["user_agent"],
    )
    paper["source"].setdefault(version, {})["withdrawal_verification"] = {
        key: evidence.get(key) for key in ("status", "http_status", "attempts", "error", "url")
    }
    paper["source"][version]["withdrawal_verification"]["wall_seconds"] = max(
        0.0, time.perf_counter() - started,
    )
    if evidence.get("http_status") in (403, 429):
        _stop_on_http(paper, "withdrawal_verification", evidence["http_status"], version,
                      attempts=evidence.get("attempts"))
    if evidence.get("status") != "confirmed_withdrawn":
        return None
    metadata = store.read_metadata(paper["mssv"], paper["arxiv_id"])
    snapshot = (metadata.get("provenance", {}).get("arxiv") or {}).get("snapshot") or {}
    evidence["version_ids"] = list(snapshot.get("version_ids") or [])
    manifest = store.confirm_withdrawn_version(
        paper["mssv"], paper["arxiv_id"], version, evidence,
    )
    _checkpoint(paper, manifest)
    paper["source"][version]["withdrawal_verification"]["status"] = "confirmed_withdrawn"
    return manifest


def _record_http_result(paper, stage, result, save, version=None):
    try:
        manifest = save(result)
    except StorageError as exc:
        if result.get("http_status") in (403, 429):
            # Điểm khôi phục bị từ chối cũng không được phép phát sinh request HTTP mới.
            paper["blocked"][version or "paper"] = type(exc).__name__
            _error(paper, "storage", type(exc).__name__, exc, version)
            raise _HttpPolicyStop(
                paper, stage, result["http_status"], version, checkpoint_saved=False,
            ) from exc
        raise
    _checkpoint(paper, manifest)
    return manifest


def _record_kaggle_comparison(paper, discovery):
    """Persist a per-ID mismatch before discovery can fail checkpoint reconciliation."""
    policy_version = os.environ.get("M1_KAGGLE_POLICY_VERSION")
    index_path = os.environ.get("M1_KAGGLE_CANDIDATE_INDEX")
    if not policy_version or not index_path or discovery.get("http_status") != 200:
        return
    evidence_required = False
    try:
        from src.kaggle_candidates import (
            compare_candidate_to_discovery,
            persist_discrepancy_evidence,
        )
        snapshot_path = os.environ.get("M1_KAGGLE_SNAPSHOT_PATH")
        snapshot_sha256 = os.environ.get("M1_KAGGLE_SNAPSHOT_SHA256")
        index, index_sha256 = _load_kaggle_index(index_path)
        if (index.get("status") != "complete" or index.get("schema_version") != 2
                or index.get("snapshot_sha256") != snapshot_sha256
                or index.get("comparison_precision") != "calendar_day_utc"
                or not snapshot_path):
            raise ValueError("Kaggle snapshot/index provenance failed verification")
        candidate = index.get("candidates", {}).get(paper["arxiv_id"])
        comparison = compare_candidate_to_discovery(candidate, discovery)
        paper["discovery"]["kaggle_comparison"] = comparison["status"]
        if comparison["status"] == "match":
            return
        evidence_required = True
        evidence_root = os.environ.get("M1_KAGGLE_EVIDENCE_ROOT")
        checkpoint_root = os.environ.get("M1_KAGGLE_CHECKPOINT_ROOT")
        session_id = os.environ.get("M1_KAGGLE_EVIDENCE_SESSION")
        if not all((evidence_root, checkpoint_root, session_id)):
            raise ValueError("Kaggle discrepancy evidence destination is not configured")
        diagnostic = None
        diagnostics = index.get("diagnostics", {})
        for name, field in (("duplicate", "duplicate_ids"),
                            ("invalid", "invalid_ids"), ("missing", "missing_ids")):
            if paper["arxiv_id"] in diagnostics.get(field, []):
                diagnostic = name
                break
        evidence = {
            "schema_version": 1,
            "session_id": session_id,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "arxiv_id": paper["arxiv_id"],
            "mssv": paper["mssv"],
            "policy_version": policy_version,
            "comparison_precision": "calendar_day_utc",
            "kaggle": {
                "dataset": index.get("dataset"),
                "snapshot_path": str(Path(os.environ["M1_KAGGLE_SNAPSHOT_PATH"]).resolve()),
                "index_path": str(Path(index_path).resolve()),
                "snapshot_sha256": index.get("snapshot_sha256"),
                "index_sha256": index_sha256,
                "downloaded_at": index.get("downloaded_at"),
                "provider_updated_at": index.get("provider_updated_at"),
                "version_ids": candidate.get("version_ids") if isinstance(candidate, dict) else None,
                "dates": candidate.get("dates") if isinstance(candidate, dict) else None,
                "diagnostic": diagnostic,
            },
            "arxiv_discovery": {
                key: discovery.get(key) for key in (
                    "status", "http_status", "attempts", "version_status", "version_ids",
                    "latest_version_id", "published", "updated", "error",
                )
            },
            "queried_at": paper["discovery"].get("query_started_at"),
            "response_observed_at": paper["discovery"].get("queried_at"),
            "comparison": comparison,
            "decision": (
                "use_arxiv_latest" if discovery.get("status") == "found"
                and discovery.get("version_status") == "resolved"
                else "use_arxiv_not_found" if discovery.get("status") == "not_found"
                else "arxiv_discovery_unresolved"
            ),
        }
        paper["discovery"]["comparison_evidence"] = persist_discrepancy_evidence(
            evidence, evidence_root=evidence_root,
            checkpoint_root=checkpoint_root, session_id=session_id,
        )
    except (OSError, ValueError, KeyError, TypeError):
        if evidence_required:
            # Bắt buộc có artifact ghi nhận sai khác; nếu tạo thất bại thì phải dừng lượt chạy.
            raise
        paper["discovery"]["kaggle_error"] = "snapshot_or_index_validation_failed"


def _run_paper(store, paper, options, versions, bbl_artifact_dir=None, query_discovery=None,
               force_discovery_refresh=False):
    mssv, arxiv_id = paper["mssv"], paper["arxiv_id"]
    _checkpoint(paper, store.initialize_paper(mssv, arxiv_id))
    if paper["discovery"]["status"] in {"found", "not_found"}:
        paper["discovery"]["action"] = "cached"
        # Từ chối bằng chứng bộ nhớ đệm không hợp lệ trước khi thao tác tiếp tục ghi trạng thái phục hồi.
        _cached_snapshot(store, paper)
    if "s2_metadata" in paper:
        s2_integration.validate_checkpoint(store, mssv, arxiv_id)
    plan = store.resume(mssv, arxiv_id)
    _checkpoint(paper, plan["manifest"])

    if paper["discovery"]["status"] in {"found", "not_found"}:
        paper["discovery"]["action"] = "cached"
        snapshot = _cached_snapshot(store, paper)
        paper["discovery"].update(wall_seconds=0.0, timing_status="cached")
        cached_metadata = store.read_metadata(mssv, arxiv_id)
        if force_discovery_refresh or not cached_discovery_is_fresh(cached_metadata):
            started = time.perf_counter()
            paper["discovery"]["query_started_at"] = datetime.now(timezone.utc).isoformat()
            try:
                if query_discovery is None:
                    fresh, allocated = arxiv_client.query_arxiv(mssv, arxiv_id, **options), None
                else:
                    fresh, allocated = query_discovery(mssv, arxiv_id)
            finally:
                paper["discovery"].update(wall_seconds=max(0.0, time.perf_counter() - started),
                                          timing_status="measured", action="queried")
            if allocated is not None:
                paper["discovery"]["wall_seconds"] = allocated
            paper["discovery"]["queried_at"] = datetime.now(timezone.utc).isoformat()
            paper["discovery"].update(http_status=fresh.get("http_status"),
                                      attempts=fresh.get("attempts", 0), error=fresh.get("error"))
            _record_kaggle_comparison(paper, fresh)
            if fresh.get("http_status") in (403, 429):
                paper["blocked"]["discovery"] = "refresh_stopped"
                _error(paper, "discovery", "refresh_stopped", "Fresh discovery blocked by HTTP policy")
                _stop_on_http(paper, "discovery", fresh.get("http_status"))
            if (paper["discovery"]["status"] == "not_found" and fresh.get("status") == "not_found"
                    and fresh.get("http_status") == 200):
                manifest = store.record_discovery(fresh)
                _checkpoint(paper, manifest)
                return True
            if (fresh.get("status") != "found" or fresh.get("http_status") != 200
                    or fresh.get("version_status") != "resolved"
                    or fresh.get("mssv") != mssv or fresh.get("arxiv_id") != arxiv_id):
                paper["blocked"]["discovery"] = "refresh_unverified"
                _error(paper, "discovery", "refresh_unverified", "Fresh discovery could not confirm cached paper")
                return False
            paper["discovery"]["reverified_at"] = datetime.now(timezone.utc).isoformat()
            paper["discovery"]["reverified_version_ids"] = list(fresh["version_ids"])
            if paper["discovery"]["status"] == "not_found":
                manifest = store.record_discovery(fresh)
            else:
                manifest = store.refresh_discovery(fresh)
            _checkpoint(paper, manifest)
            snapshot = fresh
    else:
        paper["discovery"]["action"] = "queried"
        started = time.perf_counter()
        paper["discovery"]["query_started_at"] = datetime.now(timezone.utc).isoformat()
        try:
            if query_discovery is None:
                snapshot, allocated = arxiv_client.query_arxiv(mssv, arxiv_id, **options), None
            else:
                snapshot, allocated = query_discovery(mssv, arxiv_id)
        finally:
            paper["discovery"].update(
                wall_seconds=max(0.0, time.perf_counter() - started), timing_status="measured",
            )
        if allocated is not None:
            paper["discovery"]["wall_seconds"] = allocated
        paper["discovery"]["queried_at"] = datetime.now(timezone.utc).isoformat()
        _record_kaggle_comparison(paper, snapshot)
        _record_http_result(paper, "discovery", snapshot, store.record_discovery)
    paper["discovery"].update(
        http_status=snapshot.get("http_status"), attempts=snapshot.get("attempts", 0),
        error=snapshot.get("error"),
    )

    status = paper["discovery"]["status"]
    if status == "not_found":
        return True
    if status != "found":
        _error(paper, "discovery", status, snapshot.get("error") or status)
        _stop_on_http(paper, "discovery", snapshot.get("http_status"))
        return True
    if paper["discovery"]["version_status"] != "resolved":
        paper["blocked"]["discovery"] = "needs_verification"
        _error(paper, "discovery", "needs_verification", "Version list is not resolved")
        return True

    if versions is not None:
        missing = versions.difference(paper["source"])
        if missing:
            for name in sorted(missing):
                paper["blocked"][name] = "not_discovered"
                _error(paper, "discovery", "not_discovered",
                       f"Selected version {name} is not confirmed by discovery", name)
            return True

    # Đồng thời kiểm tra đích mồ côi của các phiên bản vừa phát hiện.
    plan = store.resume(mssv, arxiv_id)
    _checkpoint(paper, plan["manifest"])
    resolved = False
    for name in plan.get("verify_withdrawal", []):
        if _verify_withdrawal_404(store, paper, name, options) is not None:
            resolved = True
        else:
            plan["blocked"][name] = plan["manifest"]["versions"][name]["status"]
    if resolved:
        plan = store.resume(mssv, arxiv_id)
        _checkpoint(paper, plan["manifest"])
    paper["skipped"] = list(plan["skip"])
    for name in plan["skip"]:
        paper["source"][name]["action"] = "skipped"
    for name, reason in plan["blocked"].items():
        paper["source"][name]["action"] = "blocked"
        paper["blocked"][name] = reason
        record = plan["manifest"]["versions"][name]
        message = record["error"]["message"] if record["error"] else reason
        _error(paper, "source", reason, message, name)

    for name in sorted(plan["retry"], key=lambda value: int(value[1:])):
        if versions is not None and name not in versions:
            continue
        try:
            destination = store.begin_version(mssv, arxiv_id, name)
            if destination is None:
                paper["source"][name]["action"] = "skipped"
                paper["skipped"].append(name)
                continue
            _checkpoint(paper, store.read_manifest(mssv, arxiv_id))
            paper['source'][name]['action'] = 'in_progress'
            source_options = dict(options)
            if bbl_artifact_dir is not None:
                source_options["bbl_artifact_dir"] = bbl_artifact_dir
            result = arxiv_source.download_source(
                mssv, arxiv_id + name, destination, **source_options,
            )
            manifest = _record_http_result(
                paper, "source", result, store.record_source_result, name,
            )
            if result.get("http_status") == 404:
                _verify_withdrawal_404(store, paper, name, options)
                manifest = store.read_manifest(mssv, arxiv_id)
                _checkpoint(paper, manifest)
        except StorageError as exc:
            # Storage có thể đã ghi trạng thái validation_failed.
            manifest = store.read_manifest(mssv, arxiv_id)
            _checkpoint(paper, manifest)
            state = manifest["versions"][name]["status"]
            reason = state if state == "validation_failed" else type(exc).__name__
            paper["source"][name]["action"] = "blocked"
            paper["blocked"][name] = reason
            _error(paper, "storage", reason, exc, name)
            continue

        state = manifest["versions"][name]["status"]
        paper["source"][name]["action"] = "downloaded" if state in TERMINAL else "failed"
        if state not in TERMINAL:
            _error(paper, "source", state, result["error"] or state, name)
            if state not in RETRYABLE:
                paper["blocked"][name] = state
            if state == "storage_error":
                return False  # Lỗi ổ đĩa hoặc quyền truy cập có thể ảnh hưởng mọi bài báo.
            _stop_on_http(paper, "source", result.get("http_status"), name)
    return True



def _run_history(store, paper, options):
    info = paper["revision_history"]
    if (paper["discovery"]["status"] != "found"
            or paper["discovery"]["version_status"] != "resolved"):
        return
    if paper["blocked"]:
        info.update(action="blocked", status="blocked")
        return
    mssv, identifier = paper["mssv"], paper["arxiv_id"]
    started = time.perf_counter()
    info.update(action="in_progress", status="in_progress",
                observed_at=datetime.now(timezone.utc).isoformat())
    try:
        # Ứng viên không thay thế discovery; yêu cầu truy vấn arXiv mới thành công
        # trong lượt gọi này, sau đó chỉ chấp nhận lịch sử khớp.
        index_path = os.environ.get("M1_KAGGLE_CANDIDATE_INDEX")
        policy_version = os.environ.get("M1_KAGGLE_POLICY_VERSION")
        if index_path and paper["discovery"].get("action") == "queried" and paper["discovery"].get("http_status") == 200:
            try:
                index, index_sha256 = _load_kaggle_index(index_path)
                candidate = index.get("candidates", {}).get(identifier)
                valid_index = (
                    index.get("status") == "complete"
                    and re.fullmatch(r"[0-9a-f]{64}", index.get("snapshot_sha256", ""))
                )
                if policy_version:
                    from src.kaggle_candidates import apply_verified_history
                    snapshot_sha256 = os.environ.get("M1_KAGGLE_SNAPSHOT_SHA256")
                    if (not valid_index or index.get("schema_version") != 2
                            or index.get("snapshot_sha256") != snapshot_sha256
                            or index.get("comparison_precision") != "calendar_day_utc"):
                        raise ValueError("Kaggle snapshot/index provenance failed verification")
                    if paper["discovery"].get("kaggle_comparison") == "match":
                        apply_verified_history(store, mssv, identifier, candidate, {
                            **index,
                            "index_sha256": index_sha256,
                            "downloaded_at": index.get("downloaded_at"),
                            "provider_updated_at": index.get("provider_updated_at"),
                            "provenance_policy": policy_version,
                        })
                    else:
                        evidence_ref = paper["discovery"].get("comparison_evidence")
                        if evidence_ref:
                            paper["revision_history"]["comparison_evidence"] = evidence_ref
                elif valid_index and candidate:
                    from src.kaggle_candidates import apply_verified_history
                    apply_verified_history(store, mssv, identifier, candidate, index)
            except (OSError, ValueError, KeyError, TypeError):
                # Bản chụp hoặc chỉ mục không hợp lệ không bao giờ thay thế lịch sử arXiv.
                if policy_version:
                    paper["revision_history"]["kaggle_error"] = "snapshot_or_index_validation_failed"
        result = arxiv_history.enrich_version_history(store, mssv, identifier, **options)
    except StorageError as exc:
        info.update(action="blocked", status="blocked", error=dict(type=type(exc).__name__))
        paper["blocked"]["revision_history"] = type(exc).__name__
        _error(paper, "revision_history", type(exc).__name__, exc)
        raise
    except BaseException as exc:
        info.update(action="failed", status="interrupted" if isinstance(
            exc, (KeyboardInterrupt, SystemExit)) else "failed", error=dict(type=type(exc).__name__))
        raise
    finally:
        info["wall_seconds"] = max(0.0, time.perf_counter() - started)
    info.update(action=result["action"], status=result["status"],
                http_status=result.get("http_status"), error=result["error"])
    # Giữ XML thô dung lượng lớn trong xuất xứ metadata, không lặp lại ở mọi bản chụp nhật ký.
    info["received_versions"] = [r["versioned_id"] for r in result["records"]]
    metadata = store.read_metadata(mssv, identifier)
    provenance = metadata["provenance"]["version_history"]
    info["retrieved_at"] = provenance.get("retrieved_at") if provenance else None
    if provenance and provenance.get("provider") == "kaggle_snapshot":
        info["provider"] = "kaggle_snapshot"
    if result["action"] == "cached" and provenance is None:
        discovery = metadata["provenance"]["arxiv"]
        source = discovery.get("field_sources", {}).get("published", discovery)
        info.update(provider="arxiv_discovery", retrieved_at=source["retrieved_at"])
    _checkpoint(paper, store.read_manifest(mssv, identifier))
    if result["error"]:
        error = result["error"]
        version = error["versioned_id"][len(identifier):]
        _error(paper, "revision_history", error["type"], error["message"], version)
        # Lỗi lấy lịch sử vẫn giữ nguyên JSON điểm khôi phục hợp lệ. Callback bài báo hiện tại
        # ghi chẩn đoán và lý do dừng trước khi chuyển sang bài tiếp theo.
        if result["stopped"]:
            raise _HttpPolicyStop(paper, "revision_history", result["http_status"],
                                  version, checkpoint_saved=False)


def _save_s2_result(store, paper, stage, result, build, save, *, references=False,
                    retry_wait_budget_seconds=None):
    """Preserve the HTTP stop signal even when validation/persistence fails."""
    http = s2_integration.http_status(result, references=references)
    try:
        manifest = save(build())
    except Exception as exc:
        if http in (403, 429):
            paper["blocked"][stage] = "checkpoint_write_failed"
            _error(paper, "storage", type(exc).__name__, "S2 checkpoint was not committed")
            raise _HttpPolicyStop(
                paper, stage, http, None, checkpoint_saved=False,
            ) from None
        raise
    _checkpoint(paper, manifest)
    error = result.get("error") or {}
    attempts = error.get("attempts") if references else result.get("attempts")
    _stop_on_http(
        paper, stage, http, attempts=attempts,
        retry_wait_budget_seconds=retry_wait_budget_seconds,
    )


def _run_s2(store, paper, config, limiter):
    mssv, identifier = paper["mssv"], paper["arxiv_id"]
    if paper["discovery"]["status"] != "found":
        return
    # Mã nguồn hỏng hoặc mồ côi là điều kiện cần kiểm toán, không phải lý do gửi thêm request HTTP.
    if any(row["action"] == "blocked" for row in paper["source"].values()):
        for stage in ("s2_metadata", "s2_references"):
            paper[stage]["action"] = "blocked"
            paper["blocked"][stage] = "source_requires_audit"
        return
    metadata = store.read_metadata(mssv, identifier)
    if s2_integration.metadata_cached(metadata):
        paper["s2_metadata"].update(action="cached", status="found")
    else:
        intent = copy.deepcopy(metadata)
        intent["status"] = "in_progress"
        _checkpoint(paper, store.save_metadata(mssv, identifier, intent))
        info = paper["s2_metadata"]
        info.update(action="queried", status="in_progress")
        options = {k: config[k] for k in ("timeout", "max_attempts", "max_response_bytes")}
        options.update({k: config[k] for k in ("max_retry_wait", "backoff_base", "backoff_cap") if k in config})
        result = semantic_scholar.fetch_metadata(
            [identifier], limiter=limiter, require_arxiv_identity=True, **options,
        )[0]
        info.update(status=result["status"], http_status=result["http_status"],
                    attempts=result["attempts"], error=result["error"])
        if result["status"] not in {"found", "not_found"}:
            _error(paper, "s2_metadata", result["status"], result["error"])
        _save_s2_result(
            store, paper, "s2_metadata", result,
            lambda: s2_integration.metadata_snapshot(metadata, result),
            lambda value: store.save_metadata(mssv, identifier, value),
            retry_wait_budget_seconds=config.get("max_retry_wait"),
        )
        if result["status"] == "invalid_response":
            paper["blocked"]["s2_metadata"] = "invalid_response"
            return

    previous = store.read_references(mssv, identifier)
    action = s2_integration.references_evidence(previous, identifier, for_resume=True)
    info = paper["s2_references"]
    if action == "cached":
        info.update(action="cached", status="complete")
        return
    if action == "fresh" and previous["status"] != "not_found":
        intent = copy.deepcopy(previous)
        intent["status"] = "in_progress"
        _checkpoint(paper, store.save_references(mssv, identifier, intent))
    info.update(action=action if action == "resumed" else "queried", status="in_progress")
    options = {k: config[k] for k in ("timeout", "max_attempts", "max_response_bytes", "max_pages")}
    options.update({k: config[k] for k in ("max_retry_wait", "backoff_base", "backoff_cap") if k in config})
    result = semantic_scholar.fetch_references(
        identifier, limit=config["reference_limit"], limiter=limiter,
        resume_from=previous if action == "resumed" else None, **options,
    )
    error = result.get("error")
    info.update(status=result["status"], http_status=s2_integration.http_status(result, references=True),
                attempts=(error or {}).get("attempts"), error=error)
    if error:
        _error(paper, "s2_references", error["type"], error["message"])
    try:
        _save_s2_result(
            store, paper, "s2_references", result,
            lambda: s2_integration.reference_result(previous, result),
            lambda value: store.save_references(mssv, identifier, value), references=True,
            retry_wait_budget_seconds=config.get("max_retry_wait"),
        )
    except StorageError:
        paper["blocked"]["s2_references"] = "invalid_reference_result"
        _error(paper, "s2_references", "StorageError", "Reference result requires audit")
        # Khi có xung đột, các bản chụp một phần hiện có được giữ nguyên từng byte.
        # Không hoàn tác kết quả mà Storage có thể đã lưu.
        raise


def run_selected(
    pairs, corpus_root, *, run_id=None, timeout=30.0, max_attempts=3,
    user_agent=arxiv_client.USER_AGENT, versions=None, on_progress=None,
    on_discovery_progress=None, s2_config=None,
    revision_history=True, should_stop=None, bbl_artifact_dir=None,
    discovery_batch_size=1, force_discovery_refresh=False,
):
    """Process a caller-supplied list of (MSSV, base ID), in order.

    All selections/options are checked before any HTTP or checkpoint writes.
    Duplicate IDs are rejected. There is no range expansion, outer retry loop,
    or CLI. S2 is opt-in via s2_config (None disables it). A resolved checkpoint is reused as-is.
    versions=None processes all versions; a nonempty list/tuple of distinct vN
    names limits source requests while retaining the full discovery/checkpoint.
    An unconfirmed selected name blocks source requests for that paper.
    revision_history=True enriches confirmed version dates after source and
    before S2, including when versions limits only the source selection. False
    preserves the bounded legacy profile. History shares arXiv pacing/retries
    and HTTP metrics; the history step itself emits no progress events.

    The returned summary separates operations from contract states: skipped,
    blocked and failed actions never replace a stored version's actual status.
    A final HTTP 403/429 result stops the batch after attempting persistence.
    The summary has stopped=True and stop_reason; intermediate retries are client-owned.
    StorageError blocks one paper/version; fatal I/O or unexpected exceptions
    raise PipelineStopped with a partial summary. KeyboardInterrupt propagates.
    on_progress(event, summary_copy) runs before/after each paper.
    on_discovery_progress(event, detail) reports actual Atom request boundaries.
    Callback exceptions propagate immediately and prevent the next HTTP operation.
    should_stop is checked between papers. A true result returns a resumable
    session_budget stop without starting another paper or issuing another request.
    """
    if type(revision_history) is not bool:
        raise ValueError("revision_history must be bool")
    if on_progress is not None and not callable(on_progress):
        raise ValueError("on_progress must be callable or None")
    if on_discovery_progress is not None and not callable(on_discovery_progress):
        raise ValueError("on_discovery_progress must be callable or None")
    if should_stop is not None and not callable(should_stop):
        raise ValueError("should_stop must be callable or None")
    if type(discovery_batch_size) is not int or not 1 <= discovery_batch_size <= 20:
        raise ValueError("discovery_batch_size must be an integer from 1 to 20")
    if type(force_discovery_refresh) is not bool:
        raise ValueError("force_discovery_refresh must be bool")
    versions = _version_selection(versions)
    store = CorpusStorage(
        corpus_root, run_id=run_id, bbl_artifact_root=bbl_artifact_dir,
    )
    selected = _inputs(pairs, store, timeout, max_attempts, user_agent)
    if bbl_artifact_dir is not None:
        artifact_root = Path(bbl_artifact_dir).expanduser().resolve()
        if artifact_root == store.root or artifact_root.is_relative_to(store.root) \
                or store.root.is_relative_to(artifact_root):
            raise ValueError("bbl_artifact_dir must be outside the source corpus")
    s2_config = s2_integration.configuration(s2_config)
    if s2_config is not None:
        semantic_scholar._key_from_environment()  # Kiểm tra header trước mọi thao tác vào/ra.
    limiter = None if s2_config is None else semantic_scholar.RateLimiter(s2_config["min_interval"])
    options = dict(timeout=timeout, max_attempts=max_attempts, user_agent=user_agent)
    prefetched: dict[tuple[str, str], dict] = {}
    shared_seconds: dict[tuple[str, str], float] = {}
    positions = {pair: index for index, pair in enumerate(selected)}

    def notify_discovery(event: str, detail: dict) -> None:
        if on_discovery_progress is not None:
            on_discovery_progress(event, copy.deepcopy(detail))

    def query_discovery(member: str, identifier: str) -> tuple[dict, float]:
        key = (member, identifier)
        if key in prefetched:
            return prefetched.pop(key), shared_seconds.pop(key)
        had_shared = key in shared_seconds
        allocated = shared_seconds.pop(key, 0.0)
        if not had_shared:
            candidates = []
            for candidate in selected[positions[key]:positions[key] + discovery_batch_size]:
                if candidate[0] != member:
                    break
                if candidate in prefetched or candidate in shared_seconds:
                    continue
                if candidate != key:
                    path = store._path(*candidate, "manifest.json")
                    if path.exists():
                        manifest = store.read_manifest(*candidate)
                        if manifest["discovery_status"] in {"found", "not_found"}:
                            metadata = store.read_metadata(*candidate)
                            if not force_discovery_refresh and cached_discovery_is_fresh(metadata):
                                continue
                candidates.append(candidate[1])
            if len(candidates) > 1:
                request_detail = {
                    "mssv": member, "first_arxiv_id": candidates[0],
                    "last_arxiv_id": candidates[-1], "requested_count": len(candidates),
                    "request_type": "batch",
                }
                notify_discovery("request_started", request_detail)
                started_batch = time.perf_counter()
                batch_result = arxiv_client.query_arxiv_batch(member, candidates, **options)
                batch_elapsed = max(0.0, time.perf_counter() - started_batch)
                share = batch_elapsed / len(candidates)
                for candidate_id in candidates:
                    shared_seconds[(member, candidate_id)] = share
                allocated = shared_seconds.pop(key)
                returned_ids = set(batch_result.get("results", {}))
                notify_discovery("request_finished", {
                    **request_detail,
                    "http_status": batch_result.get("http_status"),
                    "status": batch_result.get("status"),
                    "attempts": batch_result.get("attempts"),
                    "returned_count": len(returned_ids),
                    "unresolved_count": len(set(candidates) - returned_ids),
                    "wall_seconds": batch_elapsed,
                })
                if batch_result.get("http_status") in {403, 429}:
                    return {
                        "mssv": member, "arxiv_id": identifier,
                        "status": batch_result["status"],
                        "http_status": batch_result["http_status"],
                        "attempts": batch_result.get("attempts", 0),
                        "error": batch_result.get("error"),
                    }, allocated
                prefetched.update({
                    (member, candidate_id): value
                    for candidate_id, value in batch_result.get("results", {}).items()
                    if candidate_id != identifier
                })
                if identifier in batch_result.get("results", {}):
                    return batch_result["results"][identifier], allocated
            elif len(candidates) == 1 and candidates[0] != identifier:
                raise ValueError("Current discovery ID was omitted from its batch")
        single_detail = {
            "mssv": member, "first_arxiv_id": identifier,
            "last_arxiv_id": identifier, "requested_count": 1,
            "request_type": "single_fallback" if discovery_batch_size > 1 else "single",
        }
        notify_discovery("request_started", single_detail)
        started_single = time.perf_counter()
        result = arxiv_client.query_arxiv(member, identifier, **options)
        single_elapsed = max(0.0, time.perf_counter() - started_single)
        notify_discovery("request_finished", {
            **single_detail, "http_status": result.get("http_status"),
            "status": result.get("status"), "attempts": result.get("attempts"),
            "returned_count": int(result.get("status") in {"found", "not_found"}),
            "unresolved_count": int(result.get("status") not in {"found", "not_found"}),
            "wall_seconds": single_elapsed,
        })
        return result, allocated + single_elapsed
    summary = dict(run_id=run_id, papers=[], stopped=False)
    for mssv, arxiv_id in selected:
        if should_stop is not None and should_stop():
            summary.update(stopped=True, stop_reason={"type": "session_budget",
                "stage": "before_paper", "message": "Soft session budget reached; checkpoint is resumable."})
            break
        paper = dict(
            mssv=mssv, arxiv_id=arxiv_id, paper_status=None,
            metadata_status=None, references_status=None,
            discovery=dict(action="not_attempted", status=None, version_status=None,
                           http_status=None, attempts=0, error=None,
                           wall_seconds=None, timing_status="not_attempted"),
            source={}, skipped=[], pending=[], blocked={}, errors=[],
        )
        if revision_history:
            paper["revision_history"] = dict(
                action="not_attempted", status=None, http_status=None, error=None,
                provider="arxiv_atom_versioned", query_identifier=arxiv_id,
                retrieved_at=None, received_versions=[], wall_seconds=None,
            )
        if s2_config is not None:
            for stage in ("s2_metadata", "s2_references"):
                paper[stage] = dict(action="not_attempted", status=None, http_status=None,
                                    attempts=None, error=None)
        summary["papers"].append(paper)
        if on_progress is not None:
            on_progress("paper_started", copy.deepcopy(summary))
        try:
            if not _run_paper(
                store, paper, options, versions, bbl_artifact_dir=bbl_artifact_dir,
                query_discovery=query_discovery if discovery_batch_size > 1 else None,
                force_discovery_refresh=force_discovery_refresh,
            ):
                summary["stopped"] = True
                raise PipelineStopped("Downloader reported storage_error; audit before continuing", summary)
            if revision_history:
                _run_history(store, paper, options)
            if s2_config is not None:
                _run_s2(store, paper, s2_config, limiter)
        except _HttpPolicyStop as exc:
            summary.update(stopped=True, stop_reason=exc.reason)
        except StorageError as exc:
            paper["blocked"]["paper"] = type(exc).__name__
            _error(paper, "checkpoint", type(exc).__name__, exc)
        except PipelineStopped:
            raise
        except Exception as exc:
            summary["stopped"] = True
            stage = "storage" if isinstance(exc, OSError) else "pipeline"
            _error(paper, stage, type(exc).__name__, exc)
            raise PipelineStopped(str(exc), summary) from exc
        if on_progress is not None:
            on_progress("paper_finished", copy.deepcopy(summary))
        if summary["stopped"]:
            break
    return summary
