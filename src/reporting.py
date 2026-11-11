"""Canonical Reporting and Statistics Module for Milestone 1.

Computes comprehensive crawling, storage, extraction, metadata, reference coverage,
and M1 milestone completeness metrics strictly aligned with the M1 v2 Storage Contract
(docs/contracts/corpus_storage_contract.md, src.storage, src.storage_integrity, and docs/contracts/statistics_contract.md).
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from src.id_ranges import ConfigError, arxiv_id_to_folder_key
from src.storage import (
    CONTRACT_VERSION,
    CorpusStorage,
    IntegrityError,
    SOURCE_ERRORS,
    StorageError,
    TERMINAL,
)
from src.storage_integrity import (
    IntegrityCode,
    IntegrityViolation,
    _inspect_path,
    _read_bytes_safely,
    audit_paper_integrity,
    audit_version_integrity,
)

METRIC_DEFINITION_VERSION = "2.1.0"
REPORT_SCHEMA_VERSION = "2.1.0"


def _get_git_commit(cwd: Optional[Path] = None) -> str:
    """Attempt to get current 40-character Git commit SHA, falling back to UNKNOWN.

    Prioritizes the repository containing the codebase (PROJECT_ROOT, then repository
    root of this script), and checks cwd only as a fallback.
    """
    candidates: List[Path] = []
    try:
        from src.local_config import PROJECT_ROOT
        candidates.append(PROJECT_ROOT)
    except Exception:
        pass

    try:
        candidates.append(Path(__file__).resolve().parents[1])
    except Exception:
        pass

    if cwd is not None:
        candidates.append(Path(cwd))

    for target_dir in candidates:
        try:
            sha = subprocess.check_output(
                ["git", "-C", str(target_dir), "rev-parse", "HEAD"],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
            if re.fullmatch(r"[0-9a-f]{40}", sha):
                return sha
        except Exception:
            continue
    return "UNKNOWN"


def _perform_independent_reconciliation(
    store: CorpusStorage,
    pairs_list: List[Tuple[str, str]],
    stats: Dict[str, Any],
    m1_complete_candidates: Optional[List[Tuple[str, str]]] = None,
) -> Dict[str, Any]:
    """Perform a second-pass independent reconciliation audit across disk and candidate records.

    Verifies the accuracy of first-pass classifications against raw filesystem structures,
    raw manifest checkpoints, version directory consistency, and full 12-point M1 integrity
    re-evaluations (FIND-REP-D).
    """
    total_candidates = len(pairs_list)
    raw_found_count = 0
    raw_confirmed_not_found_count = 0
    raw_unprocessed_count = 0
    raw_discovery_failed_count = 0
    raw_missing_manifest_count = 0
    raw_invalid_manifest_count = 0
    raw_other_unresolved_count = 0

    raw_total_versions = 0
    raw_terminal_versions = 0
    raw_version_dirs_count = 0

    discrepancies: List[str] = []

    try:
        resolved_corpus_root = store.root.resolve()
    except (OSError, RuntimeError) as exc:
        resolved_corpus_root = store.root
        discrepancies.append(f"Corpus root unresolvable during reconciliation: {exc}")

    # Pass 2A: Iterate all pairs to compute raw discovery, manifest, and version structures
    for mssv, arxiv_id in pairs_list:
        try:
            folder_key = arxiv_id_to_folder_key(arxiv_id)
        except Exception as exc:
            raw_other_unresolved_count += 1
            discrepancies.append(f"Invalid folder key for {arxiv_id}: {exc}")
            continue

        paper_dir = store.root / folder_key
        try:
            try:
                p_link, p_is_d, _ = _inspect_path(paper_dir)
            except FileNotFoundError:
                raw_missing_manifest_count += 1
                continue
            except PermissionError as exc:
                raw_other_unresolved_count += 1
                discrepancies.append(
                    f"Permission error inspecting paper directory {folder_key} ({arxiv_id}): {exc}"
                )
                continue
            except (OSError, RuntimeError) as exc:
                raw_other_unresolved_count += 1
                discrepancies.append(
                    f"Filesystem error inspecting paper directory {folder_key} ({arxiv_id}): {exc}"
                )
                continue

            if p_link:
                raw_other_unresolved_count += 1
                discrepancies.append(f"Symlink found for paper directory {folder_key} ({arxiv_id})")
                continue

            if not p_is_d:
                raw_invalid_manifest_count += 1
                discrepancies.append(f"Paper directory is not a directory {folder_key} ({arxiv_id})")
                continue

            if not paper_dir.resolve().is_relative_to(resolved_corpus_root):
                raw_other_unresolved_count += 1
                discrepancies.append(f"Paper directory resolves outside corpus root {folder_key} ({arxiv_id})")
                continue
        except (OSError, RuntimeError) as exc:
            raw_other_unresolved_count += 1
            discrepancies.append(
                f"Filesystem or access error for paper directory {folder_key} ({arxiv_id}): {type(exc).__name__}: {exc}"
            )
            continue

        manifest_path = paper_dir / "manifest.json"
        try:
            try:
                m_link, _, m_file = _inspect_path(manifest_path)
            except FileNotFoundError:
                raw_missing_manifest_count += 1
                continue
            except PermissionError as exc:
                raw_other_unresolved_count += 1
                discrepancies.append(
                    f"Permission error inspecting manifest.json for {arxiv_id}: {exc}"
                )
                continue
            except (OSError, RuntimeError) as exc:
                raw_other_unresolved_count += 1
                discrepancies.append(
                    f"Filesystem error inspecting manifest.json for {arxiv_id}: {exc}"
                )
                continue

            if m_link:
                raw_invalid_manifest_count += 1
                discrepancies.append(f"manifest.json is a symlink for {arxiv_id}")
                continue

            if not m_file:
                raw_invalid_manifest_count += 1
                discrepancies.append(f"manifest.json is not a regular file for {arxiv_id}")
                continue

            try:
                raw_bytes = _read_bytes_safely(manifest_path)
            except (PermissionError, OSError, RuntimeError) as exc:
                raw_other_unresolved_count += 1
                discrepancies.append(
                    f"Filesystem or access error reading manifest.json for {arxiv_id}: {type(exc).__name__}: {exc}"
                )
                continue

            try:
                raw_man = json.loads(raw_bytes.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                raw_invalid_manifest_count += 1
                continue

            if not isinstance(raw_man, dict):
                raw_invalid_manifest_count += 1
                continue
        except (OSError, RuntimeError) as exc:
            raw_other_unresolved_count += 1
            discrepancies.append(
                f"Filesystem error inspecting manifest for {arxiv_id}: {type(exc).__name__}: {exc}"
            )
            continue
        except Exception as exc:
            raw_invalid_manifest_count += 1
            discrepancies.append(
                f"Unexpected error inspecting manifest for {arxiv_id}: {type(exc).__name__}: {exc}"
            )
            continue

        disc = raw_man.get("discovery_status")
        man_st = raw_man.get("status")
        raw_vers = raw_man.get("versions")

        if disc == "not_found":
            has_v_dirs = False
            has_unmanaged = False
            try:
                for child in paper_dir.iterdir():
                    try:
                        c_link, c_is_d, _ = _inspect_path(child)
                    except (OSError, RuntimeError) as exc:
                        has_unmanaged = True
                        discrepancies.append(f"Filesystem error checking child in not_found {folder_key}: {exc}")
                        break
                    if c_link:
                        has_unmanaged = True
                        break
                    if c_is_d and re.fullmatch(r"v[1-9][0-9]*", child.name):
                        has_v_dirs = True
                        break
                    if child.name not in {"manifest.json", "metadata.json", "references.json"}:
                        has_unmanaged = True
                        break
            except (OSError, RuntimeError) as exc:
                has_unmanaged = True
                discrepancies.append(f"Filesystem error checking not_found paper {folder_key}: {exc}")

            if (
                man_st == "not_found"
                and isinstance(raw_vers, dict)
                and not raw_vers
                and not has_v_dirs
                and not has_unmanaged
            ):
                raw_confirmed_not_found_count += 1
            else:
                raw_invalid_manifest_count += 1

        elif disc == "not_attempted":
            raw_unprocessed_count += 1
        elif disc == "failed":
            raw_discovery_failed_count += 1
        elif disc == "found":
            raw_found_count += 1
            paper_declared_v_dirs: set[str] = set()
            if isinstance(raw_vers, dict):
                raw_total_versions += len(raw_vers)
                for v_name, v_info in raw_vers.items():
                    if isinstance(v_info, dict):
                        st = v_info.get("status")
                        if st in TERMINAL:
                            raw_terminal_versions += 1
                        if st == "success" or (st == "no_tex_source" and v_info.get("files")):
                            paper_declared_v_dirs.add(v_name)
            try:
                paper_disk_v_dirs: set[str] = set()
                for child in paper_dir.iterdir():
                    try:
                        c_link, c_is_d, _ = _inspect_path(child)
                    except (OSError, RuntimeError) as exc:
                        discrepancies.append(f"Filesystem error inspecting child in found {folder_key}: {exc}")
                        continue
                    if c_link:
                        discrepancies.append(f"Symlink found in found paper {folder_key}: {child.name}")
                        continue
                    if c_is_d and re.fullmatch(r"v[1-9][0-9]*", child.name):
                        raw_version_dirs_count += 1
                        paper_disk_v_dirs.add(child.name)
                # Check version directory mismatch: on-disk version directories must match declared directories
                if paper_disk_v_dirs != paper_declared_v_dirs:
                    discrepancies.append(
                        f"Version directory on disk mismatch for {arxiv_id}: disk={sorted(paper_disk_v_dirs)} vs declared={sorted(paper_declared_v_dirs)}"
                    )
            except (OSError, RuntimeError) as exc:
                discrepancies.append(f"Filesystem error scanning version dirs for {folder_key}: {exc}")
        else:
            raw_other_unresolved_count += 1

    # Cross-check independent raw counts against classified statistics
    if raw_found_count != stats["found_papers"]:
        discrepancies.append(
            f"Found papers mismatch: classified {stats['found_papers']} vs independent {raw_found_count}"
        )
    if raw_confirmed_not_found_count != stats["confirmed_not_found"]:
        discrepancies.append(
            f"Confirmed not-found mismatch: classified {stats['confirmed_not_found']} vs independent {raw_confirmed_not_found_count}"
        )
    if raw_unprocessed_count != stats["unprocessed_papers"]:
        discrepancies.append(
            f"Unprocessed papers mismatch: classified {stats['unprocessed_papers']} vs independent {raw_unprocessed_count}"
        )
    if raw_discovery_failed_count != stats["discovery_failed"]:
        discrepancies.append(
            f"Discovery failed mismatch: classified {stats['discovery_failed']} vs independent {raw_discovery_failed_count}"
        )
    if raw_missing_manifest_count != stats["missing_manifests"]:
        discrepancies.append(
            f"Missing manifests mismatch: classified {stats['missing_manifests']} vs independent {raw_missing_manifest_count}"
        )
    if raw_invalid_manifest_count != stats["invalid_manifests"]:
        discrepancies.append(
            f"Invalid manifests mismatch: classified {stats['invalid_manifests']} vs independent {raw_invalid_manifest_count}"
        )
    if raw_other_unresolved_count != stats["other_unresolved"]:
        discrepancies.append(
            f"Other unresolved mismatch: classified {stats['other_unresolved']} vs independent {raw_other_unresolved_count}"
        )
    if raw_total_versions != stats["total_discovered_versions"]:
        discrepancies.append(
            f"Total versions mismatch: classified {stats['total_discovered_versions']} vs independent {raw_total_versions}"
        )
    if raw_terminal_versions != stats["terminal_versions"]:
        discrepancies.append(
            f"Terminal versions mismatch: classified {stats['terminal_versions']} vs independent {raw_terminal_versions}"
        )

    # Reconcile on-disk version directories with declared versions (FIND-REP-D, Step 3)
    if raw_version_dirs_count > raw_total_versions:
        discrepancies.append(
            f"On-disk version directories ({raw_version_dirs_count}) exceed total declared versions ({raw_total_versions})"
        )

    # Pass 2B: Independent Revalidation of M1-complete candidates (FIND-REP-D 12 checks)
    if m1_complete_candidates is None:
        # Fall back to finding pairs declaring terminal complete status in manifest
        m1_complete_candidates = []
        for mssv, arxiv_id in pairs_list:
            try:
                folder_key = arxiv_id_to_folder_key(arxiv_id)
                mpath = store.root / folder_key / "manifest.json"
                m_link, _, m_file = _inspect_path(mpath)
                if not m_link and m_file:
                    raw_b = _read_bytes_safely(mpath)
                    mdata = json.loads(raw_b.decode("utf-8"))
                    if isinstance(mdata, dict) and mdata.get("status") in {"complete", "complete_with_no_tex", "complete_with_withdrawn"}:
                        m1_complete_candidates.append((mssv, arxiv_id))
            except FileNotFoundError:
                pass
            except (PermissionError, OSError, RuntimeError) as exc:
                discrepancies.append(
                    f"Filesystem or access error identifying M1 candidate for {arxiv_id}: {exc}"
                )
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
            except Exception as exc:
                discrepancies.append(f"Unexpected error identifying M1 candidate for {arxiv_id}: {exc}")

    verified_m1_complete_count = 0
    for mssv, arxiv_id in m1_complete_candidates:
        candidate_failed = False
        reasons: List[str] = []

        # Check 1: Safe path & containment
        try:
            folder_key = arxiv_id_to_folder_key(arxiv_id)
            p_dir = store.root / folder_key
            try:
                p_link, p_is_d, _ = _inspect_path(p_dir)
            except FileNotFoundError:
                candidate_failed = True
                reasons.append("Check 1 (safe path): paper directory does not exist")
            except PermissionError as exc:
                candidate_failed = True
                reasons.append(f"Check 1 (safe path): permission denied inspecting paper directory: {exc}")
            except (OSError, RuntimeError) as exc:
                candidate_failed = True
                reasons.append(f"Check 1 (safe path): filesystem error inspecting paper directory: {exc}")
            else:
                if p_link:
                    candidate_failed = True
                    reasons.append("Check 1 (safe path): paper directory is a symlink")
                elif not p_is_d:
                    candidate_failed = True
                    reasons.append("Check 1 (safe path): paper directory is not a directory")
                elif not p_dir.resolve().is_relative_to(resolved_corpus_root):
                    candidate_failed = True
                    reasons.append("Check 1 (safe path): paper directory resolves outside corpus root")
        except (OSError, RuntimeError) as exc:
            candidate_failed = True
            reasons.append(f"Check 1 (safe path): filesystem error: {exc}")

        if candidate_failed:
            discrepancies.append(f"Paper {arxiv_id} failed M1 independent revalidation: {'; '.join(reasons)}")
            continue

        # Check 2: Raw manifest
        manifest = None
        man_path = p_dir / "manifest.json"
        try:
            try:
                m_link, _, m_file = _inspect_path(man_path)
            except FileNotFoundError:
                candidate_failed = True
                reasons.append("Check 2 (raw manifest): manifest.json missing")
            except PermissionError as exc:
                candidate_failed = True
                reasons.append(f"Check 2 (raw manifest): permission denied inspecting manifest.json: {exc}")
            except (OSError, RuntimeError) as exc:
                candidate_failed = True
                reasons.append(f"Check 2 (raw manifest): filesystem error inspecting manifest.json: {exc}")
            else:
                if m_link:
                    candidate_failed = True
                    reasons.append("Check 2 (raw manifest): manifest.json is a symlink")
                elif not m_file:
                    candidate_failed = True
                    reasons.append("Check 2 (raw manifest): manifest.json is not a regular file")
                else:
                    try:
                        raw_b = _read_bytes_safely(man_path)
                    except PermissionError as exc:
                        candidate_failed = True
                        reasons.append(f"Check 2 (raw manifest): permission denied reading manifest.json: {exc}")
                    except (OSError, RuntimeError) as exc:
                        candidate_failed = True
                        reasons.append(f"Check 2 (raw manifest): filesystem error reading manifest.json: {exc}")
                    else:
                        manifest = json.loads(raw_b.decode("utf-8"))
                        if not isinstance(manifest, dict):
                            candidate_failed = True
                            reasons.append("Check 2 (raw manifest): manifest is not a JSON object")
        except (OSError, RuntimeError, json.JSONDecodeError) as exc:
            candidate_failed = True
            reasons.append(f"Check 2 (raw manifest): read/parse error: {exc}")

        if candidate_failed or manifest is None:
            discrepancies.append(f"Paper {arxiv_id} failed M1 independent revalidation: {'; '.join(reasons)}")
            continue

        # Check 3: Identity & contract_version=2
        contract_v = manifest.get("contract_version", manifest.get("storage_contract_version"))
        if contract_v != CONTRACT_VERSION or type(contract_v) is not int or isinstance(contract_v, bool):
            candidate_failed = True
            reasons.append(f"Check 3 (identity): invalid contract_version {contract_v}")
        if manifest.get("arxiv_id") != arxiv_id:
            candidate_failed = True
            reasons.append(f"Check 3 (identity): arxiv_id mismatch: {manifest.get('arxiv_id')} != {arxiv_id}")
        if manifest.get("mssv") != mssv:
            candidate_failed = True
            reasons.append(f"Check 3 (identity): mssv mismatch: {manifest.get('mssv')} != {mssv}")

        # Check 4: Discovery=found
        if manifest.get("discovery_status") != "found":
            candidate_failed = True
            reasons.append(f"Check 4 (discovery): discovery_status is {manifest.get('discovery_status')}, expected 'found'")

        # Check 5: Terminal manifest status
        if manifest.get("status") not in {"complete", "complete_with_no_tex", "complete_with_withdrawn"}:
            candidate_failed = True
            reasons.append(f"Check 5 (terminal manifest status): manifest status is {manifest.get('status')}")
        ver_st = manifest.get("version_status", manifest.get("version_resolution_status"))
        if ver_st != "resolved":
            candidate_failed = True
            reasons.append(f"Check 5 (terminal manifest status): version_status is {ver_st}")
        if manifest.get("metadata_status") != "complete":
            candidate_failed = True
            reasons.append(f"Check 5 (terminal manifest status): manifest.metadata_status is {manifest.get('metadata_status')}")
        if manifest.get("references_status") != "complete":
            candidate_failed = True
            reasons.append(f"Check 5 (terminal manifest status): manifest.references_status is {manifest.get('references_status')}")

        # Check 6: Terminal versions
        versions = manifest.get("versions")
        if not isinstance(versions, dict) or len(versions) == 0:
            candidate_failed = True
            reasons.append("Check 6 (terminal versions): manifest versions is empty or not a dict")
        else:
            for v_name, v_info in versions.items():
                if not re.fullmatch(r"v[1-9][0-9]*", v_name):
                    candidate_failed = True
                    reasons.append(f"Check 6 (terminal versions): invalid version name format '{v_name}'")
                if not isinstance(v_info, dict) or v_info.get("status") not in TERMINAL:
                    candidate_failed = True
                    reasons.append(f"Check 6 (terminal versions): version {v_name} status '{v_info.get('status') if isinstance(v_info, dict) else None}' not in TERMINAL")

        # Check 7: Audit source files & SHA-256
        if isinstance(versions, dict):
            for v_name, v_info in versions.items():
                if isinstance(v_info, dict):
                    try:
                        v_violations = audit_version_integrity(p_dir, arxiv_id, v_name, v_info)
                        if v_violations:
                            candidate_failed = True
                            reasons.append(f"Check 7 (source files & SHA-256): {v_name} has violations: {[v.message for v in v_violations]}")
                    except Exception as exc:
                        candidate_failed = True
                        reasons.append(f"Check 7 (source files & SHA-256): {v_name} audit failed with error: {exc}")

        # Check 8: Read & validate metadata
        meta_path = p_dir / "metadata.json"
        meta = None
        try:
            try:
                meta_link, _, meta_file = _inspect_path(meta_path)
            except FileNotFoundError:
                candidate_failed = True
                reasons.append("Check 8 (read/validate metadata): metadata.json missing")
            except PermissionError as exc:
                candidate_failed = True
                reasons.append(f"Check 8 (read/validate metadata): permission denied inspecting metadata.json: {exc}")
            except (OSError, RuntimeError) as exc:
                candidate_failed = True
                reasons.append(f"Check 8 (read/validate metadata): filesystem error inspecting metadata.json: {exc}")
            else:
                if meta_link:
                    candidate_failed = True
                    reasons.append("Check 8 (read/validate metadata): metadata.json is a symlink")
                elif not meta_file:
                    candidate_failed = True
                    reasons.append("Check 8 (read/validate metadata): metadata.json is not regular file")
                else:
                    try:
                        raw_meta_b = _read_bytes_safely(meta_path)
                    except PermissionError as exc:
                        candidate_failed = True
                        reasons.append(f"Check 8 (read/validate metadata): permission denied reading metadata.json: {exc}")
                    except (OSError, RuntimeError) as exc:
                        candidate_failed = True
                        reasons.append(f"Check 8 (read/validate metadata): filesystem error reading metadata.json: {exc}")
                    else:
                        meta = json.loads(raw_meta_b.decode("utf-8"))
                        if not isinstance(meta, dict):
                            candidate_failed = True
                            reasons.append("Check 8 (read/validate metadata): metadata is not a JSON object")
                        elif meta.get("status") != "complete":
                            candidate_failed = True
                            reasons.append(f"Check 8 (read/validate metadata): metadata.status is '{meta.get('status')}'")
                        else:
                            store._metadata(meta, mssv, arxiv_id)
        except (StorageError, IntegrityError, json.JSONDecodeError, OSError, RuntimeError) as exc:
            candidate_failed = True
            reasons.append(f"Check 8 (read/validate metadata): validation error: {exc}")
        except Exception as exc:
            candidate_failed = True
            reasons.append(f"Check 8 (read/validate metadata): unexpected error: {exc}")

        # Check 9: Metadata consistency
        if meta is not None and isinstance(meta, dict) and isinstance(versions, dict):
            if meta.get("arxiv_id") != arxiv_id:
                candidate_failed = True
                reasons.append(f"Check 9 (metadata consistency): arxiv_id mismatch ({meta.get('arxiv_id')} != {arxiv_id})")
            meta_cv = meta.get("contract_version", meta.get("storage_contract_version"))
            if meta_cv != CONTRACT_VERSION:
                candidate_failed = True
                reasons.append(f"Check 9 (metadata consistency): contract_version mismatch ({meta_cv})")
            meta_v_names = [v.get("version") if isinstance(v, dict) else v for v in meta.get("versions", [])]
            if sorted(versions.keys()) != sorted(meta_v_names):
                candidate_failed = True
                reasons.append(f"Check 9 (metadata consistency): versions mismatch ({sorted(versions.keys())} != {sorted(meta_v_names)})")
            if not meta.get("title") or not meta.get("authors"):
                candidate_failed = True
                reasons.append("Check 9 (metadata consistency): missing title or authors")

        # Check 10: Read & validate references
        ref_path = p_dir / "references.json"
        ref = None
        try:
            try:
                ref_link, _, ref_file = _inspect_path(ref_path)
            except FileNotFoundError:
                candidate_failed = True
                reasons.append("Check 10 (read/validate references): references.json missing")
            except PermissionError as exc:
                candidate_failed = True
                reasons.append(f"Check 10 (read/validate references): permission denied inspecting references.json: {exc}")
            except (OSError, RuntimeError) as exc:
                candidate_failed = True
                reasons.append(f"Check 10 (read/validate references): filesystem error inspecting references.json: {exc}")
            else:
                if ref_link:
                    candidate_failed = True
                    reasons.append("Check 10 (read/validate references): references.json is a symlink")
                elif not ref_file:
                    candidate_failed = True
                    reasons.append("Check 10 (read/validate references): references.json is not regular file")
                else:
                    try:
                        raw_ref_b = _read_bytes_safely(ref_path)
                    except PermissionError as exc:
                        candidate_failed = True
                        reasons.append(f"Check 10 (read/validate references): permission denied reading references.json: {exc}")
                    except (OSError, RuntimeError) as exc:
                        candidate_failed = True
                        reasons.append(f"Check 10 (read/validate references): filesystem error reading references.json: {exc}")
                    else:
                        ref = json.loads(raw_ref_b.decode("utf-8"))
                        if not isinstance(ref, dict):
                            candidate_failed = True
                            reasons.append("Check 10 (read/validate references): references is not a JSON object")
                        elif ref.get("status") != "complete":
                            candidate_failed = True
                            reasons.append(f"Check 10 (read/validate references): references.status is '{ref.get('status')}'")
                        else:
                            store._references(ref, mssv, arxiv_id)
        except (StorageError, IntegrityError, json.JSONDecodeError, OSError, RuntimeError) as exc:
            candidate_failed = True
            reasons.append(f"Check 10 (read/validate references): validation error: {exc}")
        except Exception as exc:
            candidate_failed = True
            reasons.append(f"Check 10 (read/validate references): unexpected error: {exc}")

        # Check 11: Reference coverage complete & continuation=None
        if ref is not None and isinstance(ref, dict):
            cov = ref.get("coverage")
            if not isinstance(cov, dict):
                candidate_failed = True
                reasons.append("Check 11 (references coverage): missing coverage dictionary")
            else:
                if cov.get("complete") is not True:
                    candidate_failed = True
                    reasons.append("Check 11 (references coverage): coverage.complete is not True")
                if cov.get("continuation") is not None:
                    candidate_failed = True
                    reasons.append("Check 11 (references coverage): coverage.continuation is not None")
            if not isinstance(ref.get("items"), list):
                candidate_failed = True
                reasons.append("Check 11 (references coverage): items is not a list")

        # Check 12: No unmanaged/symlink violations in paper_dir
        allowed_entries = {"manifest.json", "metadata.json", "references.json"}
        if isinstance(versions, dict):
            for v_name, v_info in versions.items():
                if isinstance(v_info, dict):
                    if v_info.get("status") == "success" or (v_info.get("status") == "no_tex_source" and v_info.get("files")):
                        allowed_entries.add(v_name)
        try:
            for child in p_dir.iterdir():
                try:
                    c_link, c_is_d, _ = _inspect_path(child)
                except (OSError, RuntimeError) as exc:
                    candidate_failed = True
                    reasons.append(f"Check 12 (unmanaged/symlink): filesystem error inspecting child {child.name}: {exc}")
                    continue

                if c_link:
                    candidate_failed = True
                    reasons.append(f"Check 12 (unmanaged/symlink): symlink found: {child.name}")
                elif child.name not in allowed_entries:
                    candidate_failed = True
                    reasons.append(f"Check 12 (unmanaged/symlink): unmanaged file or folder: {child.name}")
                elif c_is_d and not child.resolve().is_relative_to(p_dir.resolve()):
                    candidate_failed = True
                    reasons.append(f"Check 12 (unmanaged/symlink): path traversal detected: {child.name}")
        except (OSError, RuntimeError) as exc:
            candidate_failed = True
            reasons.append(f"Check 12 (unmanaged/symlink): filesystem error: {exc}")
            reasons.append(f"Check 12 (unmanaged/symlink): filesystem error: {exc}")

        if candidate_failed:
            discrepancies.append(f"Paper {arxiv_id} failed M1 independent revalidation: {'; '.join(reasons)}")
        else:
            verified_m1_complete_count += 1

    if verified_m1_complete_count != stats["m1_complete_papers"]:
        discrepancies.append(
            f"M1-complete count mismatch: classified {stats['m1_complete_papers']} vs independently verified {verified_m1_complete_count}"
        )

    # Verification of arithmetic invariants
    paper_population_balance = (
        stats["found_papers"]
        + stats["confirmed_not_found"]
        + stats["unprocessed_papers"]
        + stats["discovery_failed"]
        + stats["missing_manifests"]
        + stats["invalid_manifests"]
        + stats["other_unresolved"]
    )
    if paper_population_balance != total_candidates:
        discrepancies.append(
            f"Paper population balance equation violated: {paper_population_balance} != {total_candidates}"
        )

    found_lifecycle_balance = (
        stats["m1_complete_papers"]
        + stats["partial_papers"]
        + stats["failed_papers"]
        + stats["interrupted_papers"]
        + stats["unresolved_found_papers"]
    )
    if found_lifecycle_balance != stats["found_papers"]:
        discrepancies.append(
            f"Found lifecycle balance equation violated: {found_lifecycle_balance} != {stats['found_papers']}"
        )

    version_balance = (
        stats["terminal_versions"]
        + stats["incomplete_versions"]
        + stats["failed_versions"]
        + stats["invalid_versions"]
    )
    if version_balance != stats["total_discovered_versions"]:
        discrepancies.append(
            f"Version balance equation violated: {version_balance} != {stats['total_discovered_versions']}"
        )

    if not (
        stats["m1_complete_papers"] <= stats["source_complete_papers"]
        and stats["m1_complete_papers"] <= stats["metadata_complete_papers"]
        and stats["m1_complete_papers"] <= stats["references_complete_papers"]
    ):
        discrepancies.append("M1 complete hierarchy violation: m1_complete must be <= stage completions")

    return {
        "is_reconciled": len(discrepancies) == 0,
        "discrepancies": discrepancies,
        "verified_m1_complete_count": verified_m1_complete_count,
        "independent_audit": {
            "raw_found_count": raw_found_count,
            "raw_confirmed_not_found_count": raw_confirmed_not_found_count,
            "raw_unprocessed_count": raw_unprocessed_count,
            "raw_discovery_failed_count": raw_discovery_failed_count,
            "raw_missing_manifest_count": raw_missing_manifest_count,
            "raw_invalid_manifest_count": raw_invalid_manifest_count,
            "raw_other_unresolved_count": raw_other_unresolved_count,
            "raw_total_versions": raw_total_versions,
            "raw_terminal_versions": raw_terminal_versions,
            "raw_version_dirs_count": raw_version_dirs_count,
        },
        "paper_population_balance": paper_population_balance,
        "paper_population_expected": total_candidates,
        "found_lifecycle_balance": found_lifecycle_balance,
        "found_lifecycle_expected": stats["found_papers"],
        "version_balance": version_balance,
        "version_expected": stats["total_discovered_versions"],
        "m1_hierarchy_valid": (
            stats["m1_complete_papers"] <= stats["source_complete_papers"]
            and stats["m1_complete_papers"] <= stats["metadata_complete_papers"]
            and stats["m1_complete_papers"] <= stats["references_complete_papers"]
        ),
    }


def compute_m1_statistics(
    store: CorpusStorage,
    pairs: Iterable[Tuple[str, str]],
    *,
    repository_commit: Optional[str] = None,
    report_scope: Optional[str] = None,
    _between_passes_hook: Optional[Callable[[], None]] = None,
) -> Dict[str, Any]:
    """Compute all canonical Milestone 1 crawling, extraction, metadata, and reference metrics.

    Adheres strictly to the M1 v2 Storage Contract and Canonical Metric Specification:
    - Evidence-first verification: integrates audit_paper_integrity and audit_version_integrity.
    - Mutually exclusive & exhaustive candidate classification.
    - Preserves null semantics (None/N/A for zero denominators, no artificial 0.0%).
    - Enforces M1 completion invariant: source_complete and meta_complete and refs_complete.
    """
    pairs_list = list(pairs)
    total_candidates = len(pairs_list)

    # 1. Primary Paper Classification (Mutually Exclusive Partition of assigned_candidates)
    found_papers = 0
    confirmed_not_found = 0
    unprocessed_papers = 0
    missing_manifests = 0
    invalid_manifests = 0
    discovery_failed = 0
    other_unresolved = 0

    # 2. Stage Completion Counters
    source_complete_papers = 0
    metadata_complete_papers = 0
    references_complete_papers = 0
    m1_complete_papers = 0
    m1_complete_candidates: List[Tuple[str, str]] = []

    # 3. Found Papers Lifecycle Counters (Mutually Exclusive Partition of found_papers)
    partial_papers = 0
    failed_papers = 0
    interrupted_papers = 0
    unresolved_found_papers = 0
    integrity_failed_papers = 0

    # 4. Version Level Counters
    total_discovered_versions = 0
    successful_tex_versions = 0
    no_tex_versions = 0
    withdrawn_versions = 0
    terminal_versions = 0
    incomplete_versions = 0
    failed_versions = 0
    invalid_versions = 0
    source_integrity_failed_versions = 0

    # 5. Reference Counters
    complete_reference_papers = 0
    partial_reference_papers = 0
    failed_reference_papers = 0
    not_attempted_reference_papers = 0
    invalid_reference_papers = 0
    total_complete_reference_records = 0
    total_observed_reference_records = 0

    for mssv, arxiv_id in pairs_list:
        try:
            folder_key = arxiv_id_to_folder_key(arxiv_id)
        except (ConfigError, Exception):
            other_unresolved += 1
            continue

        paper_dir = store.root / folder_key

        # Check paper directory presence and path safety
        try:
            p_link, p_dir_is_d, _ = _inspect_path(paper_dir)
        except FileNotFoundError:
            missing_manifests += 1
            continue
        except PermissionError:
            other_unresolved += 1
            integrity_failed_papers += 1
            continue
        except (OSError, RuntimeError):
            other_unresolved += 1
            integrity_failed_papers += 1
            continue

        if p_link:
            other_unresolved += 1
            integrity_failed_papers += 1
            continue

        if not p_dir_is_d:
            invalid_manifests += 1
            integrity_failed_papers += 1
            continue

        manifest_path = paper_dir / "manifest.json"
        try:
            m_link, _, m_file = _inspect_path(manifest_path)
        except FileNotFoundError:
            missing_manifests += 1
            continue
        except PermissionError:
            other_unresolved += 1
            integrity_failed_papers += 1
            continue
        except (OSError, RuntimeError):
            other_unresolved += 1
            integrity_failed_papers += 1
            continue

        if m_link:
            invalid_manifests += 1
            integrity_failed_papers += 1
            continue

        if not m_file:
            invalid_manifests += 1
            integrity_failed_papers += 1
            continue

        try:
            manifest = store.read_manifest(mssv, arxiv_id)
        except PermissionError:
            other_unresolved += 1
            integrity_failed_papers += 1
            continue
        except (StorageError, IntegrityError, json.JSONDecodeError, OSError, Exception):
            invalid_manifests += 1
            integrity_failed_papers += 1
            continue

        disc = manifest.get("discovery_status")

        if disc == "not_found":
            # Verify negative discovery contract (R14: versions must be exact empty dict)
            versions = manifest.get("versions")
            is_valid_manifest_not_found = (
                manifest.get("status") == "not_found"
                and isinstance(versions, dict)
                and not versions
            )

            # FIND-REP-C: Check filesystem: no version directories and no unmanaged entries
            has_version_dirs = False
            has_unmanaged_entries = False
            try:
                for child in paper_dir.iterdir():
                    try:
                        c_link, c_is_d, _ = _inspect_path(child)
                    except (OSError, RuntimeError):
                        has_unmanaged_entries = True
                        break
                    if c_link:
                        has_unmanaged_entries = True
                        break
                    if c_is_d and re.fullmatch(r"v[1-9][0-9]*", child.name):
                        has_version_dirs = True
                        break
                    if child.name not in {"manifest.json", "metadata.json", "references.json"}:
                        has_unmanaged_entries = True
                        break
            except (OSError, RuntimeError):
                has_unmanaged_entries = True

            if is_valid_manifest_not_found and not has_version_dirs and not has_unmanaged_entries:
                confirmed_not_found += 1
            else:
                invalid_manifests += 1
                integrity_failed_papers += 1
            continue

        elif disc == "not_attempted":
            unprocessed_papers += 1
            continue

        elif disc == "failed":
            discovery_failed += 1
            continue

        elif disc == "found":
            found_papers += 1
            versions = manifest.get("versions")
            if not isinstance(versions, dict):
                versions = {}

            total_discovered_versions += len(versions)

            # Audit paper integrity using canonical Phase 1 API
            audit_res = audit_paper_integrity(store, mssv, arxiv_id)
            if audit_res.has_integrity_failure:
                integrity_failed_papers += 1

            # --- A. Source Completion Check ---
            # Decoupled from metadata and references (FIND-REP-A)
            if audit_res.is_source_complete:
                source_complete_papers += 1

            # --- B. Metadata Completion Check ---
            # Verify metadata file independently
            is_meta_complete = False
            meta_path = paper_dir / "metadata.json"
            try:
                meta_link, _, meta_file = _inspect_path(meta_path)
                has_meta_file = not meta_link and meta_file
            except (OSError, RuntimeError):
                has_meta_file = False

            if has_meta_file:
                try:
                    meta = store.read_metadata(mssv, arxiv_id)
                    if (
                        meta.get("status") == "complete"
                        and manifest.get("metadata_status") == "complete"
                        and not any(
                            v.severity == "error"
                            and ("metadata" in v.message.lower() or (v.file_path and "metadata" in v.file_path))
                            for v in audit_res.violations
                        )
                    ):
                        is_meta_complete = True
                except (StorageError, IntegrityError, json.JSONDecodeError, OSError):
                    is_meta_complete = False
                except Exception:
                    is_meta_complete = False

            if is_meta_complete:
                metadata_complete_papers += 1

            # --- C. References Completion Check ---
            is_ref_complete = False
            ref_path = paper_dir / "references.json"
            try:
                ref_link, _, ref_file = _inspect_path(ref_path)
                has_ref_file = not ref_link and ref_file
            except (OSError, RuntimeError):
                has_ref_file = False

            if has_ref_file:
                try:
                    ref_data = store.read_references(mssv, arxiv_id)
                    ref_st = ref_data.get("status")
                    coverage = ref_data.get("coverage") or {}
                    cov_complete = coverage.get("complete") is True and coverage.get("continuation") is None
                    items = ref_data.get("items") or []
                    item_count = len(items) if isinstance(items, list) else 0
                    total_observed_reference_records += item_count

                    if (
                        ref_st == "complete"
                        and cov_complete
                        and manifest.get("references_status") == "complete"
                        and not any(
                            v.severity == "error"
                            and ("reference" in v.message.lower() or (v.file_path and "references" in v.file_path))
                            for v in audit_res.violations
                        )
                    ):
                        is_ref_complete = True
                        complete_reference_papers += 1
                        total_complete_reference_records += item_count
                    elif ref_st == "partial" or (ref_st == "complete" and not cov_complete):
                        partial_reference_papers += 1
                    elif ref_st == "failed":
                        failed_reference_papers += 1
                    elif ref_st == "not_attempted":
                        not_attempted_reference_papers += 1
                    else:
                        invalid_reference_papers += 1
                except (StorageError, IntegrityError, json.JSONDecodeError, OSError):
                    invalid_reference_papers += 1
                except Exception:
                    invalid_reference_papers += 1
            else:
                not_attempted_reference_papers += 1

            if is_ref_complete:
                references_complete_papers += 1

            # --- D. Milestone 1 Complete Invariant ---
            # Strict: source complete and metadata complete and references complete and no violations
            if audit_res.is_m1_complete and is_meta_complete and is_ref_complete:
                m1_complete_papers += 1
                m1_complete_candidates.append((mssv, arxiv_id))

            # --- E. Found Papers Lifecycle Classification (Mutually Exclusive) ---
            man_status = manifest.get("status")
            if audit_res.is_m1_complete and is_meta_complete and is_ref_complete:
                pass  # Accounted as m1_complete
            elif man_status == "partial":
                partial_papers += 1
            elif man_status == "failed":
                failed_papers += 1
            elif man_status == "interrupted":
                interrupted_papers += 1
            else:
                unresolved_found_papers += 1

            # --- F. Version-Level Accounting ---
            for v_name, v_info in versions.items():
                if not isinstance(v_info, dict):
                    invalid_versions += 1
                    continue
                v_st = v_info.get("status")
                if v_st == "success":
                    successful_tex_versions += 1
                    terminal_versions += 1
                elif v_st == "no_tex_source":
                    no_tex_versions += 1
                    terminal_versions += 1
                elif v_st == "withdrawn_version":
                    withdrawn_versions += 1
                    terminal_versions += 1
                elif v_st in {"pending", "in_progress", "interrupted"}:
                    incomplete_versions += 1
                elif v_st in SOURCE_ERRORS:
                    failed_versions += 1
                else:
                    invalid_versions += 1

                # Check version source integrity violations
                try:
                    v_violations = audit_version_integrity(paper_dir, arxiv_id, v_name, v_info)
                except Exception:
                    v_violations = [
                        IntegrityViolation(
                            arxiv_id=arxiv_id,
                            category="corrupted",
                            severity="error",
                            message="Filesystem error auditing version",
                            code=IntegrityCode.FILESYSTEM_ERROR,
                        )
                    ]
                if v_violations:
                    source_integrity_failed_versions += 1

        else:
            other_unresolved += 1

    # Computed percentages (None when denominator == 0, explicit semantics)
    source_completion_rate: Optional[float] = (
        round(source_complete_papers / found_papers * 100.0, 2) if found_papers > 0 else None
    )
    metadata_completion_rate: Optional[float] = (
        round(metadata_complete_papers / found_papers * 100.0, 2) if found_papers > 0 else None
    )
    references_coverage_rate: Optional[float] = (
        round(complete_reference_papers / found_papers * 100.0, 2) if found_papers > 0 else None
    )
    m1_completion_rate: Optional[float] = (
        round(m1_complete_papers / found_papers * 100.0, 2) if found_papers > 0 else None
    )
    version_extraction_success_rate: Optional[float] = (
        round(successful_tex_versions / total_discovered_versions * 100.0, 2)
        if total_discovered_versions > 0
        else None
    )
    no_tex_rate: Optional[float] = (
        round(no_tex_versions / total_discovered_versions * 100.0, 2)
        if total_discovered_versions > 0
        else None
    )
    withdrawn_rate: Optional[float] = (
        round(withdrawn_versions / total_discovered_versions * 100.0, 2)
        if total_discovered_versions > 0
        else None
    )
    average_references_per_paper: Optional[float] = (
        round(total_complete_reference_records / complete_reference_papers, 2)
        if complete_reference_papers > 0
        else None
    )

    # Reconciliation Checks
    paper_population_balance = (
        found_papers
        + confirmed_not_found
        + unprocessed_papers
        + discovery_failed
        + missing_manifests
        + invalid_manifests
        + other_unresolved
    )
    found_lifecycle_balance = (
        m1_complete_papers
        + partial_papers
        + failed_papers
        + interrupted_papers
        + unresolved_found_papers
    )
    version_balance = (
        terminal_versions
        + incomplete_versions
        + failed_versions
        + invalid_versions
    )

    stats_draft = {
        "found_papers": found_papers,
        "confirmed_not_found": confirmed_not_found,
        "unprocessed_papers": unprocessed_papers,
        "discovery_failed": discovery_failed,
        "missing_manifests": missing_manifests,
        "invalid_manifests": invalid_manifests,
        "other_unresolved": other_unresolved,
        "total_discovered_versions": total_discovered_versions,
        "terminal_versions": terminal_versions,
        "incomplete_versions": incomplete_versions,
        "failed_versions": failed_versions,
        "invalid_versions": invalid_versions,
        "m1_complete_papers": m1_complete_papers,
        "source_complete_papers": source_complete_papers,
        "metadata_complete_papers": metadata_complete_papers,
        "references_complete_papers": references_complete_papers,
        "partial_papers": partial_papers,
        "failed_papers": failed_papers,
        "interrupted_papers": interrupted_papers,
        "unresolved_found_papers": unresolved_found_papers,
    }
    if _between_passes_hook is not None:
        _between_passes_hook()

    reconciliation_result = _perform_independent_reconciliation(
        store,
        pairs_list,
        stats_draft,
        m1_complete_candidates=m1_complete_candidates,
    )
    is_reconciled = reconciliation_result["is_reconciled"]

    commit_sha = repository_commit or _get_git_commit(store.root)

    try:
        corpus_root_str = str(store.root.resolve())
    except (OSError, RuntimeError):
        corpus_root_str = str(store.root)

    return {
        "provenance": {
            "report_generated_at": datetime.now(timezone.utc).isoformat(),
            "repository_commit": commit_sha,
            "metric_definition_version": METRIC_DEFINITION_VERSION,
            "storage_contract_version": CONTRACT_VERSION,
            "report_schema_version": REPORT_SCHEMA_VERSION,
            "corpus_root": corpus_root_str,
            "assigned_candidate_count": total_candidates,
            "report_scope": report_scope or "cohort_assignment",
        },
        # Candidate Accounting
        "assigned_candidates": total_candidates,
        "total_candidates": total_candidates,  # Compatibility alias
        "found_papers": found_papers,
        "confirmed_not_found": confirmed_not_found,
        "not_found_papers": confirmed_not_found,  # Compatibility alias
        "unprocessed_papers": unprocessed_papers,
        "missing_manifests": missing_manifests,
        "invalid_manifests": invalid_manifests,
        "discovery_failed": discovery_failed,
        "other_unresolved": other_unresolved,
        # Stage & M1 Completion
        "source_complete_papers": source_complete_papers,
        "metadata_complete_papers": metadata_complete_papers,
        "references_complete_papers": references_complete_papers,
        "m1_complete_papers": m1_complete_papers,
        # Found Lifecycle Partition
        "partial_papers": partial_papers,
        "failed_papers": failed_papers,
        "interrupted_papers": interrupted_papers,
        "unresolved_found_papers": unresolved_found_papers,
        "integrity_failed_papers": integrity_failed_papers,
        # Version Accounting
        "total_discovered_versions": total_discovered_versions,
        "total_versions": total_discovered_versions,  # Compatibility alias
        "successful_tex_versions": successful_tex_versions,
        "no_tex_versions": no_tex_versions,
        "withdrawn_versions": withdrawn_versions,
        "withdrawn_rate": withdrawn_rate,
        "terminal_versions": terminal_versions,
        "incomplete_versions": incomplete_versions,
        "failed_versions": failed_versions,
        "invalid_versions": invalid_versions,
        "unresolved_versions": invalid_versions,  # Canonical metric spec name
        "source_integrity_failed_versions": source_integrity_failed_versions,
        # References Accounting
        "complete_reference_papers": complete_reference_papers,
        "partial_reference_papers": partial_reference_papers,
        "failed_reference_papers": failed_reference_papers,
        "not_attempted_reference_papers": not_attempted_reference_papers,
        "invalid_reference_papers": invalid_reference_papers,
        "invalid_references": invalid_reference_papers,  # Compatibility alias
        "total_complete_reference_records": total_complete_reference_records,
        "total_observed_reference_records": total_observed_reference_records,
        # Rates and Averages
        "source_completion_rate": source_completion_rate,
        "metadata_completion_rate": metadata_completion_rate,
        "references_coverage_rate": references_coverage_rate,
        "m1_completion_rate": m1_completion_rate,
        "version_extraction_success_rate": version_extraction_success_rate,
        "no_tex_rate": no_tex_rate,
        "average_references_per_paper": average_references_per_paper,
        # Reconciliation Results
        "is_reconciled": is_reconciled,
        "reconciliation": reconciliation_result,
        # Backward compatibility aliases
        "completed_papers": source_complete_papers,
        "completed_versions": terminal_versions,
        "success_rate": source_completion_rate if source_completion_rate is not None else 0.0,
        "total_references": total_complete_reference_records,
        "papers_with_references": complete_reference_papers,
    }


def _fmt_rate(val: Optional[float], suffix: str = "%") -> str:
    """Format nullable percentage/rate for Markdown tables."""
    if val is None:
        return "N/A"
    return f"{val}{suffix}"


def format_markdown_summary(stats: Dict[str, Any]) -> str:
    """Format computed statistics into standard, verified Markdown for technical reports."""
    prov = stats.get("provenance", {})
    reconciled_badge = "ĐẠT (Balanced)" if stats.get("is_reconciled") else "CẦN KIỂM TRA (Unbalanced)"

    lines = [
        "### Báo cáo kiểm toán và thống kê thu thập dữ liệu Milestone 1",
        "",
        "#### 1. Xuất xứ kiểm toán (Audit Provenance)",
        "",
        "| Thuộc tính | Giá trị |",
        "|---|---|",
        f"| Thời điểm tạo báo cáo | `{prov.get('report_generated_at', 'N/A')}` |",
        f"| Mã băm mã nguồn (Commit) | `{prov.get('repository_commit', 'N/A')}` |",
        f"| Thư mục lưu trữ Corpus | `{prov.get('corpus_root', 'N/A')}` |",
        f"| Phiên bản định nghĩa số liệu | `v{prov.get('metric_definition_version', 'N/A')}` |",
        f"| Phiên bản hợp đồng lưu trữ | `v{prov.get('storage_contract_version', 'N/A')}` |",
        f"| Trạng thái đối chiếu số liệu | **{reconciled_badge}** |",
        "",
        "#### 2. Chỉ số tiến độ các giai đoạn & Chất lượng M1 (Canonical Stage Metrics)",
        "",
        "| Chỉ số kiểm toán M1 | Số lượng | Tỷ lệ (%) / Mẫu số | Ghi chú |",
        "|---|---:|---:|---|",
        f"| Tổng định danh phân công (assigned_candidates) | {stats['assigned_candidates']:,} | 100.0% | Dải ID cấu hình |",
        f"| Bài báo tồn tại trên arXiv (found_papers) | {stats['found_papers']:,} | {_fmt_rate(stats['found_papers'] / stats['assigned_candidates'] * 100 if stats['assigned_candidates'] > 0 else None)} | Ứng viên thực tế |",
        f"| Bài báo không tồn tại (confirmed_not_found) | {stats['confirmed_not_found']:,} | {_fmt_rate(stats['confirmed_not_found'] / stats['assigned_candidates'] * 100 if stats['assigned_candidates'] > 0 else None)} | Xác nhận âm tính |",
        f"| Bài báo hoàn tất mã nguồn (source_complete) | {stats['source_complete_papers']:,} | {_fmt_rate(stats['source_completion_rate'])} | Trên bài tìm thấy (found) |",
        f"| Bài báo hoàn tất siêu dữ liệu (metadata_complete) | {stats['metadata_complete_papers']:,} | {_fmt_rate(stats['metadata_completion_rate'])} | Trên bài tìm thấy (found) |",
        f"| Bài báo hoàn tất trích dẫn (references_complete) | {stats['complete_reference_papers']:,} | {_fmt_rate(stats['references_coverage_rate'])} | Trên bài tìm thấy (found) |",
        f"| Bài báo hoàn tất toàn diện M1 (m1_complete) | {stats['m1_complete_papers']:,} | {_fmt_rate(stats['m1_completion_rate'])} | Đạt 100% tiêu chí M1 |",
        f"| Bài báo thu thập dở dang (partial_papers) | {stats['partial_papers']:,} | Cần resume | Giai đoạn chưa hoàn tất |",
        f"| Bài báo thất bại (failed_papers) | {stats['failed_papers']:,} | Cần xử lý lỗi | Gặp lỗi terminal |",
        f"| Bài báo bị gián đoạn (interrupted_papers) | {stats['interrupted_papers']:,} | Cần resume | Do timeout / cancel |",
        f"| Bài báo có vi phạm tính toàn vẹn (integrity_failed) | {stats['integrity_failed_papers']:,} | Cần kiểm toán | Sai SHA/tệp rác |",
        "",
        "#### 3. Thống kê bóc tách phiên bản (Version-Level Statistics)",
        "",
        "| Phân loại phiên bản | Số lượng | Tỷ lệ (%) / Mẫu số | Ghi chú |",
        "|---|---:|---:|---|",
        f"| Tổng phiên bản quan sát được (total_discovered_versions) | {stats['total_discovered_versions']:,} | 100.0% | Từ bài tìm thấy |",
        f"| Phiên bản trích xuất TeX thành công (successful_tex) | {stats['successful_tex_versions']:,} | {_fmt_rate(stats['version_extraction_success_rate'])} | Có tệp .tex xác minh |",
        f"| Phiên bản không chứa TeX hợp lệ (no_tex_source) | {stats['no_tex_versions']:,} | {_fmt_rate(stats['no_tex_rate'])} | Terminal hợp lệ |",
        f"| Phiên bản bị rút / huỷ (withdrawn_version) | {stats.get('withdrawn_versions', 0):,} | {_fmt_rate(stats.get('withdrawn_rate'))} | Terminal hợp lệ |",
        f"| Tổng phiên bản kết thúc (terminal_versions) | {stats['terminal_versions']:,} | {_fmt_rate(stats['terminal_versions'] / stats['total_discovered_versions'] * 100 if stats['total_discovered_versions'] > 0 else None)} | success + no_tex + withdrawn |",
        f"| Phiên bản chưa xong (incomplete_versions) | {stats['incomplete_versions']:,} | {_fmt_rate(stats['incomplete_versions'] / stats['total_discovered_versions'] * 100 if stats['total_discovered_versions'] > 0 else None)} | pending / in_progress |",
        f"| Phiên bản bóc tách lỗi (failed_versions) | {stats['failed_versions']:,} | {_fmt_rate(stats['failed_versions'] / stats['total_discovered_versions'] * 100 if stats['total_discovered_versions'] > 0 else None)} | Lỗi nguồn arXiv |",
        "",
        "#### 4. Thống kê quan hệ trích dẫn (References Statistics)",
        "",
        "| Chỉ số trích dẫn | Giá trị | Ghi chú |",
        "|---|---:|---|",
        f"| Bài báo trích dẫn hoàn tất (complete_refs) | {stats['complete_reference_papers']:,} | Đạt coverage.complete |",
        f"| Bài báo trích dẫn dở dang (partial_refs) | {stats['partial_reference_papers']:,} | Cần bổ sung trích dẫn |",
        f"| Bài báo trích dẫn lỗi (failed_refs) | {stats['failed_reference_papers']:,} | Lỗi API S2 |",
        f"| Bài báo chưa thu thập trích dẫn (not_attempted) | {stats['not_attempted_reference_papers']:,} | Chưa cào |",
        f"| Tổng liên kết trích dẫn hoàn tất | {stats['total_complete_reference_records']:,} | Thu thập từ bài complete |",
        f"| Số liên kết trích dẫn trung bình / bài | {stats['average_references_per_paper'] if stats['average_references_per_paper'] is not None else 'N/A'} | Trên bài complete |",
        "",
        "#### 5. Bảng kiểm tra đối chiếu số liệu (Reconciliation Summary)",
        "",
        "| Hạng mục đối chiếu | Giá trị tính toán | Kỳ vọng | Kết luận |",
        "|---|---:|---:|:---:|",
        f"| Cân bằng tập định danh phân công | {stats['reconciliation']['paper_population_balance']:,} | {stats['assigned_candidates']:,} | {'KHỚP' if stats['reconciliation']['paper_population_balance'] == stats['assigned_candidates'] else 'LỆCH'} |",
        f"| Cân bằng vòng đời bài tìm thấy | {stats['reconciliation']['found_lifecycle_balance']:,} | {stats['found_papers']:,} | {'KHỚP' if stats['reconciliation']['found_lifecycle_balance'] == stats['found_papers'] else 'LỆCH'} |",
        f"| Cân bằng phân loại phiên bản | {stats['reconciliation']['version_balance']:,} | {stats['total_discovered_versions']:,} | {'KHỚP' if stats['reconciliation']['version_balance'] == stats['total_discovered_versions'] else 'LỆCH'} |",
        f"| Thứ bậc M1 complete (M1 <= Source, Meta, Refs) | {stats['m1_complete_papers']:,} <= {min(stats['source_complete_papers'], stats['metadata_complete_papers'], stats['references_complete_papers']):,} | Hợp lệ | {'HỢP LỆ' if stats['reconciliation']['m1_hierarchy_valid'] else 'VI PHẠM'} |",
    ]
    return "\n".join(lines) + "\n"


def export_report_json(stats: Dict[str, Any], out_path: Path) -> None:
    """Save canonical statistics dictionary as structured, indented JSON."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(stats, indent=2, ensure_ascii=False), encoding="utf-8")
