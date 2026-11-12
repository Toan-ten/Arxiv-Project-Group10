"""Engine di thu thập M1, checkpoint và đồng bộ archive theo từng lô."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import shutil
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import re
import tarfile

from src import arxiv_client, batch, benchmark_runner, id_ranges
from src.storage import CorpusStorage, _atomic_json, arxiv_id_to_folder_key
from src.storage_integrity import _inspect_path, _read_bytes_safely


COHORT_SIZE = 100
_CACHED_ASSIGNED_PAIRS: Optional[List[Tuple[str, str]]] = None


class RestoreRollbackError(RuntimeError):
    """A failed restore left the original paper in the named backup directory."""


def cohort_pairs(number: int) -> List[Tuple[str, str]]:
    """Return 100 candidate (mssv, arxiv_id) pairs for cohort 1..200."""
    if not isinstance(number, int) or not 1 <= number <= 200:
        raise ValueError("Cohort number must be between 1 and 200")
    global _CACHED_ASSIGNED_PAIRS
    if _CACHED_ASSIGNED_PAIRS is None:
        config = id_ranges.load_config()
        _CACHED_ASSIGNED_PAIRS = list(id_ranges.iter_assigned_ids(config))
    start = (number - 1) * COHORT_SIZE
    return _CACHED_ASSIGNED_PAIRS[start:start + COHORT_SIZE]


def member_cohort_range(mssv: str) -> List[int]:
    """Return the list of 50 cohort numbers assigned to the member identified by MSSV."""
    config = id_ranges.load_config()
    if mssv not in config:
        raise ValueError(f"Unknown MSSV {mssv}. Must be one of: {list(config.keys())}")
    member_keys = list(config.keys())
    idx = member_keys.index(mssv)
    start_cohort = idx * 50 + 1
    end_cohort = start_cohort + 50
    return list(range(start_cohort, end_cohort))


def _is_pid_alive(pid: int) -> bool:
    """Return True if a process with given PID is active or cannot be confirmed dead.

    Fail-safe policy:
    - PID <= 0 is invalid and cannot be confirmed dead (returns True to protect files).
    - Only confirmed ESRCH (No such process) returns False (confirmed dead).
    - EPERM, EACCES, or any other OS/probing error returns True (assumed alive / preserved).
    """
    import errno
    if not isinstance(pid, int) or pid <= 0:
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError as err:
        if err.errno == errno.ESRCH:
            return False
        # EPERM, EACCES hoặc lỗi hệ điều hành khác: an toàn khi giả định tiến trình còn sống
        return True
    except Exception:
        # Lỗi ngoài dự kiến: an toàn khi giả định tiến trình còn sống
        return True


def cleanup_orphaned_archive_tmp(
    directory: Path,
    archive_name: str,
) -> List[Path]:
    """Safely clean up orphaned temporary files for archive_name in directory.

    Guarantees:
    - Files with active PIDs are preserved.
    - Files with unparseable/unknown PID patterns are preserved (fail-safe).
    - Files with PID <= 0 are preserved (fail-safe).
    - Probing with permission denied (EPERM, EACCES) or unknown errors preserves files (fail-safe).
    - Only files belonging to confirmed dead PIDs (ESRCH) are removed.
    """
    removed: List[Path] = []
    try:
        for orphan in directory.glob(f".{archive_name}.*.tmp"):
            if not orphan.is_file():
                continue
            m = re.search(r"\.pid_(\d+)_", orphan.name)
            if not m:
                # PID không rõ hoặc không thể xác định: giữ lại để an toàn
                continue
            try:
                orphan_pid = int(m.group(1))
            except (ValueError, TypeError):
                continue
            if orphan_pid <= 0 or _is_pid_alive(orphan_pid):
                # PID không hợp lệ, tiến trình đang chạy hoặc chưa xác nhận đã dừng: không xóa
                continue
            try:
                orphan.unlink()
                removed.append(orphan)
            except OSError:
                pass
    except Exception:
        pass
    return removed


def audit_cohort_archive_payload(
    archive_path: Path,
    number: int,
    *,
    require_full: bool = True,
) -> Dict[str, Any]:
    """Audit the internal members and files of a cohort archive.

    Verifies:
    1. Safe members: no symlinks, hardlinks, device nodes, fifos, absolute paths, traversal, unsafe characters.
    2. Paper top folders belong strictly to expected cohort members.
    3. Every paper folder has valid manifest.json with matching MSSV and arxiv_id.
    4. Complete papers have metadata.json and references.json as valid JSON files in tar.
    5. Version files match file_checksums with SHA-256 computation on extracted bytes.
    6. no_tex_source contract is enforced (no .tex files, only .bib/.bbl, 0 kept bytes if empty).
    7. success versions contain at least one .tex file.
    8. Zero unmanaged files exist in any paper directory.
    9. not_found papers have valid empty version contract and only the standard
       manifest/metadata/references checkpoint files.
    """
    pairs = cohort_pairs(number)
    expected_folders = {arxiv_id_to_folder_key(aid): (mssv, aid) for mssv, aid in pairs}

    try:
        is_l, _, is_f = _inspect_path(archive_path)
        if is_l or not is_f:
            raise ValueError(f"Cohort archive not found or invalid: {archive_path}")
    except (FileNotFoundError, OSError) as e:
        raise ValueError(f"Cohort archive not found or invalid: {e}") from e

    tar_folders: Set[str] = set()
    manifests_in_tar: Dict[str, dict] = {}
    tar_members_map: Dict[str, tarfile.TarInfo] = {}
    files_by_paper: Dict[str, Set[PurePosixPath]] = {}

    try:
        with tarfile.open(archive_path, "r:gz") as tar:
            for m in tar.getmembers():
                if m.issym() or m.islnk() or m.isdev() or m.isfifo():
                    raise ValueError(f"Special files not allowed in backup: {m.name}")
                if not (m.isfile() or m.isdir()):
                    raise ValueError(f"Unrecognized member type in backup: {m.name}")
                p = PurePosixPath(m.name)
                if p.is_absolute() or m.name.startswith("/") or m.name.startswith("\\"):
                    raise ValueError(f"Unsafe archive member: {m.name}")
                if ".." in p.parts or "." in p.parts:
                    raise ValueError(f"Unsafe archive member: {m.name}")
                if len(p.parts) == 0:
                    continue
                if ":" in p.parts[0] or "\\" in m.name:
                    raise ValueError(f"Unsafe characters in archive member path: {m.name}")
                top = p.parts[0]
                if top not in expected_folders:
                    raise ValueError(f"Foreign folder in cohort archive: {top}")
                tar_folders.add(top)
                tar_members_map[m.name] = m
                if m.isfile():
                    files_by_paper.setdefault(top, set()).add(p)
                if len(p.parts) == 2 and p.parts[1] == "manifest.json" and m.isfile():
                    f = tar.extractfile(m)
                    if f is None:
                        raise ValueError(f"Cannot read manifest.json inside archive for {top}")
                    try:
                        manifests_in_tar[top] = json.loads(f.read().decode("utf-8"))
                    except Exception as e:
                        raise ValueError(f"Corrupted manifest.json in archive for {top}: {e}")

            if require_full and tar_folders != set(expected_folders.keys()):
                missing = set(expected_folders.keys()) - tar_folders
                extra = tar_folders - set(expected_folders.keys())
                raise ValueError(
                    f"Tar members mismatch expected cohort folders: missing={sorted(missing)}, extra={sorted(extra)}"
                )

            found_m1_complete = 0
            confirmed_not_found = 0
            legacy_bbl_found = False
            included_ids: List[str] = []
            omitted: List[Dict[str, str]] = []

            for folder_name, (mssv, aid) in expected_folders.items():
                if folder_name not in tar_folders:
                    if require_full:
                        raise ValueError(f"Missing expected paper {aid} ({folder_name})")
                    omitted.append({"arxiv_id": aid, "reason": "missing_from_archive"})
                    continue

                p_man = manifests_in_tar.get(folder_name)
                if p_man is None:
                    raise ValueError(f"Missing manifest.json inside archive for paper {aid} ({folder_name})")
                if p_man.get("arxiv_id") != aid or p_man.get("mssv") != mssv:
                    raise ValueError(f"Identity mismatch in manifest for {aid}")

                managed_files_for_paper = {"manifest.json"}
                disc_st = p_man.get("discovery_status")
                if disc_st == "found":
                    if p_man.get("version_status") != "resolved":
                        raise ValueError(f"Paper {aid} version_status is '{p_man.get('version_status')}', not resolved")
                    if p_man.get("metadata_status") != "complete":
                        raise ValueError(f"Paper {aid} metadata_status is '{p_man.get('metadata_status')}', not complete")

                    meta_rel = f"{folder_name}/metadata.json"
                    if meta_rel not in tar_members_map or not tar_members_map[meta_rel].isfile():
                        raise ValueError(f"Paper {aid} missing metadata.json inside archive")
                    f_meta = tar.extractfile(tar_members_map[meta_rel])
                    if f_meta is None:
                        raise ValueError(f"Cannot read metadata.json for paper {aid}")
                    try:
                        meta_payload = json.loads(f_meta.read().decode("utf-8"))
                        if not isinstance(meta_payload, dict):
                            raise ValueError("metadata.json must be a JSON object")
                    except Exception as exc:
                        raise ValueError(f"Corrupted metadata.json for paper {aid}: {exc}")
                    managed_files_for_paper.add("metadata.json")

                    if p_man.get("references_status") != "complete":
                        raise ValueError(f"Paper {aid} references_status is '{p_man.get('references_status')}', not complete")

                    ref_rel = f"{folder_name}/references.json"
                    if ref_rel not in tar_members_map or not tar_members_map[ref_rel].isfile():
                        raise ValueError(f"Paper {aid} missing references.json inside archive")
                    f_ref = tar.extractfile(tar_members_map[ref_rel])
                    if f_ref is None:
                        raise ValueError(f"Cannot read references.json for paper {aid}")
                    try:
                        ref_payload = json.loads(f_ref.read().decode("utf-8"))
                        if not isinstance(ref_payload, dict):
                            raise ValueError("references.json must be a JSON object")
                    except Exception as exc:
                        raise ValueError(f"Corrupted references.json for paper {aid}: {exc}")
                    managed_files_for_paper.add("references.json")

                    st = p_man.get("status")
                    if st not in {"complete", "complete_with_no_tex", "complete_with_withdrawn"}:
                        raise ValueError(f"Paper {aid} status is '{st}', not in complete states")

                    versions = p_man.get("versions", {})
                    if not isinstance(versions, dict) or not versions:
                        raise ValueError(f"Paper {aid} has empty or non-dict versions")

                    has_any_tex_file = False
                    has_any_no_tex_version = False
                    has_any_withdrawn_version = False

                    for v_name, v_data in versions.items():
                        if not isinstance(v_data, dict):
                            raise ValueError(f"Paper {aid} version {v_name} is not a dictionary")
                        v_st = v_data.get("status")
                        if v_st not in {"success", "no_tex_source", "withdrawn_version"}:
                            raise ValueError(f"Paper {aid} version {v_name} status '{v_st}' is not terminal")

                        checksums = v_data.get("file_checksums") or {}
                        v_files = v_data.get("files") or []
                        all_v_files = set(checksums.keys()) | set(v_files)
                        bbl_policy = v_data.get("bbl_policy")
                        if (bbl_policy is None
                                and any(PurePosixPath(name).suffix.lower() == ".bbl"
                                        for name in v_files)):
                            legacy_bbl_found = True
                        if bbl_policy is not None:
                            if (not isinstance(bbl_policy, dict)
                                    or bbl_policy.get("policy_version") != "bbl-exception-v1"
                                    or bbl_policy.get("status") != "complete"
                                    or not isinstance(bbl_policy.get("items"), list)):
                                raise ValueError(f"Paper {aid} has an invalid BBL policy in {v_name}")
                            decisions = bbl_policy["items"]
                            decision_names = [
                                item.get("bbl_path") for item in decisions if isinstance(item, dict)
                            ]
                            if (len(decision_names) != len(decisions)
                                    or any(not isinstance(name, str) for name in decision_names)
                                    or len(decision_names) != len(set(decision_names))):
                                raise ValueError(f"Paper {aid} has invalid BBL decisions in {v_name}")
                            retained_bbls = set()
                            for item in decisions:
                                name = item["bbl_path"]
                                digest = item.get("bbl_sha256")
                                if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                                    raise ValueError(f"Paper {aid} has an invalid BBL checksum in {v_name}")
                                decision = item.get("status")
                                if decision == "retained_conversion_failed":
                                    retained_bbls.add(name)
                                    if checksums.get(name) != digest or name not in v_files:
                                        raise ValueError(f"Retained BBL checksum mismatch for {aid}/{v_name}/{name}")
                                elif decision in {
                                    "removed_bib_present", "removed_bibitem_in_tex", "converted_and_removed",
                                }:
                                    if name in all_v_files:
                                        raise ValueError(f"Removed BBL remains in corpus: {aid}/{v_name}/{name}")
                                    if not isinstance(item.get("source_evidence_path"), str):
                                        raise ValueError(f"Missing BBL source evidence path for {aid}/{v_name}/{name}")
                                    if decision == "converted_and_removed":
                                        if (not isinstance(item.get("derived_bib_path"), str)
                                                or not isinstance(item.get("derived_bib_sha256"), str)
                                                or type(item.get("entry_count")) is not int
                                                or item["entry_count"] < 1):
                                            raise ValueError(f"Missing derived BibTeX evidence for {aid}/{v_name}/{name}")
                                else:
                                    raise ValueError(f"Unknown BBL decision for {aid}/{v_name}/{name}")
                            actual_bbls = {
                                name for name in v_files
                                if PurePosixPath(name).suffix.lower() == ".bbl"
                            }
                            if retained_bbls != actual_bbls:
                                raise ValueError(f"BBL policy does not account for corpus files in {aid}/{v_name}")

                        if v_st == "withdrawn_version":
                            has_any_withdrawn_version = True
                            if all_v_files:
                                raise ValueError(f"Paper {aid} withdrawn_version in {v_name} must have empty files")
                            if checksums:
                                raise ValueError(f"Paper {aid} withdrawn_version in {v_name} has unexpected file_checksums")
                            if v_data.get("kept_bytes") != 0:
                                raise ValueError(f"Paper {aid} withdrawn_version in {v_name} kept_bytes must be 0")
                        elif v_st == "no_tex_source":
                            has_any_no_tex_version = True
                            if not all_v_files:
                                if checksums:
                                    raise ValueError(f"Paper {aid} empty no_tex_source in {v_name} has unexpected file_checksums")
                                if v_data.get("kept_bytes") != 0:
                                    raise ValueError(f"Paper {aid} empty no_tex_source in {v_name} kept_bytes must be 0")
                            else:
                                for fname in all_v_files:
                                    if fname.lower().endswith(".tex"):
                                        raise ValueError(f"Paper {aid} no_tex_source in {v_name} contains .tex file: {fname}")
                                    if Path(fname).suffix.lower() not in {".bib", ".bbl"}:
                                        raise ValueError(f"Paper {aid} no_tex_source in {v_name} contains non-bib file: {fname}")
                        elif v_st == "success":
                            v_has_tex = any(fname.lower().endswith(".tex") for fname in all_v_files)
                            if not v_has_tex:
                                raise ValueError(f"Paper {aid} version {v_name} marked success but has no .tex files")
                            has_any_tex_file = True

                        for fname in all_v_files:
                            rel_file = f"{v_name}/{fname}"
                            managed_files_for_paper.add(rel_file)
                            rel_tar_path = f"{folder_name}/{rel_file}"
                            m_file = tar_members_map.get(rel_tar_path)
                            if not m_file or not m_file.isfile():
                                raise ValueError(f"Missing file {rel_tar_path} for paper {aid} inside archive")
                            if fname in checksums:
                                exp_hash = checksums[fname]
                                f_handle = tar.extractfile(m_file)
                                if f_handle is None:
                                    raise ValueError(f"Cannot read file {rel_tar_path} for paper {aid}")
                                file_hash = hashlib.sha256(f_handle.read()).hexdigest()
                                if file_hash != exp_hash:
                                    raise ValueError(f"Checksum mismatch for {rel_tar_path}: expected {exp_hash}, got {file_hash}")

                    if st == "complete":
                        if not has_any_tex_file:
                            raise ValueError(f"Paper {aid} status is 'complete' but no .tex files found in any version")
                        if has_any_no_tex_version:
                            raise ValueError(f"Paper {aid} status is 'complete' but has no_tex_source versions")
                        if has_any_withdrawn_version:
                            raise ValueError(f"Paper {aid} status is 'complete' but has withdrawn_version versions")
                    elif st == "complete_with_no_tex":
                        if not has_any_no_tex_version:
                            raise ValueError(f"Paper {aid} status is 'complete_with_no_tex' but no no_tex_source version found")
                    elif st == "complete_with_withdrawn":
                        if not has_any_withdrawn_version:
                            raise ValueError(f"Paper {aid} status is 'complete_with_withdrawn' but no withdrawn_version found")

                    # Xác minh thư mục bài báo không có tệp ngoài manifest.
                    for p_member in files_by_paper.get(folder_name, set()):
                        rel_to_paper = "/".join(p_member.parts[1:])
                        if rel_to_paper not in managed_files_for_paper:
                            raise ValueError(f"Paper {aid} contains unmanaged file outside manifest: {p_member.as_posix()}")

                    found_m1_complete += 1
                    included_ids.append(aid)

                elif disc_st == "not_found":
                    if p_man.get("status") != "not_found" or p_man.get("versions") != {}:
                        raise ValueError(f"Paper {aid} has invalid not_found contract")
                    for p_member in files_by_paper.get(folder_name, set()):
                        rel_to_paper = "/".join(p_member.parts[1:])
                        if rel_to_paper not in {"manifest.json", "metadata.json", "references.json"}:
                            raise ValueError(f"not_found Paper {aid} contains unmanaged file: {p_member.as_posix()}")
                    confirmed_not_found += 1
                    included_ids.append(aid)
                else:
                    if require_full:
                        raise ValueError(f"Paper {aid} discovery_status is incomplete: '{disc_st}'")
                    omitted.append({"arxiv_id": aid, "reason": f"incomplete_status_{disc_st}"})

            is_cohort_complete = (
                len(included_ids) == len(pairs)
                and (found_m1_complete + confirmed_not_found) == len(pairs)
            )
            if require_full and not is_cohort_complete:
                raise ValueError(
                    f"Cohort {number} is incomplete: {len(included_ids)}/{len(pairs)} included, "
                    f"m1_complete={found_m1_complete}, not_found={confirmed_not_found}"
                )

            return {
                "cohort_number": number,
                "archive_path": str(archive_path),
                "inspected_papers": len(tar_folders),
                "found_m1_complete": found_m1_complete,
                "confirmed_not_found": confirmed_not_found,
                "included_ids": sorted(included_ids),
                "included_count": len(included_ids),
                "expected_count": len(pairs),
                "omitted": omitted,
                "omitted_count": len(omitted),
                "is_cohort_complete": is_cohort_complete,
                "bbl_policy_state": (
                    "legacy_unverified" if legacy_bbl_found else "complete"
                ),
            }

    except (tarfile.TarError, OSError, EOFError) as e:
        raise ValueError(f"Corrupted or unreadable archive: {e}") from e


class FastProductionRunner:
    """High-throughput cohort runner with SSD acceleration and automated Drive backup."""

    def __init__(
        self,
        local_root: str | Path,
        drive_root: str | Path,
        *,
        scratch_root: Optional[str | Path] = None,
        retry_429: bool = True,
        retry_wait_budget: float = 300.0,
        timeout: float = 30.0,
    ):
        self.local_root = Path(local_root).resolve()
        self.drive_root = Path(drive_root).resolve()
        self.corpus_dir = self.local_root / "corpus"
        self.logs_dir = self.drive_root / "logs"
        self.reports_dir = self.drive_root / "reports"
        self.archives_dir = self.drive_root / "archives"
        self.bbl_artifact_dir = (
            self.reports_dir / "derived_bibliography" / "bbl-exception-v1"
        )

        if scratch_root is not None:
            self.scratch_dir = Path(scratch_root).resolve()
        elif Path("/dev/shm").exists() and os.access("/dev/shm", os.W_OK):
            self.scratch_dir = Path("/dev/shm/arxiv_scratch")
        else:
            self.scratch_dir = self.local_root / "scratch"

        self.corpus_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self.archives_dir.mkdir(parents=True, exist_ok=True)
        self.scratch_dir.mkdir(parents=True, exist_ok=True)

        self.retry_429 = retry_429
        self.retry_wait_budget = float(retry_wait_budget)
        self.timeout = float(timeout)
        self.store = CorpusStorage(
            self.corpus_dir, bbl_artifact_root=self.bbl_artifact_dir,
        )

    def _reset_recoverable_archive_conflicts(self, number: int) -> int:
        """Reset versions previously marked unsafe_archive due to duplicate non-TeX/non-Bib files."""
        from src.storage import arxiv_id_to_folder_key
        reset_count = 0
        for mssv, arxiv_id in cohort_pairs(number):
            folder_key = arxiv_id_to_folder_key(arxiv_id)
            manifest_path = self.corpus_dir / folder_key / "manifest.json"
            try:
                is_link, _, is_f = _inspect_path(manifest_path)
                if is_link or not is_f:
                    continue
                manifest = self.store.read_manifest(mssv, arxiv_id)
                changed = False
                for ver_name, rec in list(manifest.get("versions", {}).items()):
                    err_msg = (rec.get("error") or {}).get("message") or ""
                    if rec.get("status") == "unsafe_archive" and err_msg.startswith("Conflicting path:"):
                        ver_dir = self.corpus_dir / folder_key / ver_name
                        try:
                            is_l_v, is_d_v, _ = _inspect_path(ver_dir)
                            if not is_l_v and is_d_v:
                                continue
                        except FileNotFoundError:
                            pass
                        manifest["versions"][ver_name] = self.store._pending(arxiv_id, ver_name)
                        changed = True
                if changed:
                    self.store._commit(manifest)
                    reset_count += 1
            except Exception:
                pass
        if reset_count > 0:
            print(f"Lô {number}: Đã phục hồi {reset_count} phiên bản bị xung đột tệp không-TeX trước đó.")
        return reset_count

    def count_source_completed_papers(self, number: int) -> int:
        """Count how many papers in the cohort have verified source terminal completion.

        Enforces the Storage Contract by verifying (R10):
        1. Reject any paper directory that is a symlink or resolves outside the corpus root.
        2. Verify manifest.json is a regular file with st_nlink == 1 (safely opened with O_NOFOLLOW).
        3. Validate manifest schema and arXiv identity before trusting any status field.
        4. Negative terminal discovery (not_found): verifies manifest.status is terminal ('not_found'),
           no versions recorded in manifest, and no version directories exist on disk.
        5. Resolved terminal versions (found): all versions are terminal (TERMINAL = {'success', 'no_tex_source'})
           and pass full cryptographic checksum and source audit via audit_version_integrity.
        """
        import os
        import re
        import stat
        from src.storage import (
            CONTRACT_VERSION,
            DISCOVERY_STATES,
            PAPER_STATES,
            TERMINAL,
            arxiv_id_to_folder_key,
        )
        from src.storage_integrity import audit_version_integrity

        done = 0
        try:
            is_link, is_dir, _ = _inspect_path(self.corpus_dir)
            if is_link or not is_dir:
                return 0
            corpus_resolved = self.corpus_dir.resolve()
            if not corpus_resolved.is_dir():
                return 0
        except (OSError, RuntimeError):
            return 0

        for mssv, arxiv_id in cohort_pairs(number):
            try:
                folder_key = arxiv_id_to_folder_key(arxiv_id)
            except Exception:
                continue

            paper_dir = self.corpus_dir / folder_key
            try:
                is_link, is_dir, _ = _inspect_path(paper_dir)
                if is_link or not is_dir:
                    continue
                if not paper_dir.resolve().is_relative_to(corpus_resolved):
                    continue
            except (OSError, RuntimeError):
                continue

            manifest_path = paper_dir / "manifest.json"
            try:
                is_link, _, is_file = _inspect_path(manifest_path)
                if is_link or not is_file:
                    continue
                m_bytes = _read_bytes_safely(manifest_path)
                m = json.loads(m_bytes.decode("utf-8"))
            except Exception:
                continue

            # 3. Kiểm tra schema manifest và định danh arXiv trước khi tin cậy bất kỳ trường trạng thái nào (R10, R13).
            if not isinstance(m, dict):
                continue
            try:
                self.store._manifest(m, mssv, arxiv_id)
            except Exception:
                continue

            status = m.get("status")
            disc = m.get("discovery_status")

            # 4. Kết quả discovery âm đã kết thúc: not_found (R10, R14).
            if disc == "not_found":
                if status != "not_found":
                    continue
                versions = m.get("versions")
                if not isinstance(versions, dict) or versions:
                    continue
                # Xác minh trên đĩa không có thư mục phiên bản.
                try:
                    has_version_dir = False
                    for p in paper_dir.iterdir():
                        if re.match(r"^v\d+$", p.name):
                            try:
                                is_l_p, is_d_p, _ = _inspect_path(p)
                                if not is_l_p and is_d_p:
                                    has_version_dir = True
                                    break
                            except Exception:
                                has_version_dir = True
                                break
                    if has_version_dir:
                        continue
                except OSError:
                    continue
                done += 1
                continue

            # 5. Các phiên bản đã xác định và kết thúc: found (R10).
            versions = m.get("versions")
            if disc == "found" and isinstance(versions, dict) and versions and m.get("version_status") == "resolved":
                if not all(isinstance(v, dict) and v.get("status") in TERMINAL for v in versions.values()):
                    continue
                paper_ok = True
                for v_name, v_info in versions.items():
                    if not isinstance(v_info, dict):
                        paper_ok = False
                        break
                    v_violations = audit_version_integrity(paper_dir, arxiv_id, v_name, v_info)
                    if v_violations:
                        paper_ok = False
                        break
                if paper_ok:
                    done += 1

        return done

    def count_completed_papers(self, number: int) -> int:
        """Alias to count_source_completed_papers for backward compatibility."""
        return self.count_source_completed_papers(number)

    def count_m1_completed_papers(self, number: int) -> Dict[str, Any]:
        """Count papers in cohort that satisfy full M1 completion, confirmed not-found, etc.

        Reuses audit_paper_integrity from storage_integrity.py.
        Returns:
            {
                "found_m1_complete": int,
                "confirmed_not_found": int,
                "discovery_failed": int,
                "unprocessed": int,
                "integrity_failures": int,
                "is_cohort_complete": bool,
            }
        """
        from src.storage_integrity import audit_paper_integrity
        from src.storage import arxiv_id_to_folder_key

        pairs = cohort_pairs(number)
        found_m1_complete = 0
        confirmed_not_found = 0
        discovery_failed = 0
        unprocessed = 0
        integrity_failures = 0

        for mssv, arxiv_id in pairs:
            try:
                res = audit_paper_integrity(self.store, mssv, arxiv_id)
                if res.has_integrity_failure:
                    integrity_failures += 1
                elif res.is_m1_complete:
                    found_m1_complete += 1
                elif res.status == "valid":
                    folder_key = arxiv_id_to_folder_key(arxiv_id)
                    manifest_path = self.corpus_dir / folder_key / "manifest.json"
                    if manifest_path.is_file():
                        try:
                            m = json.loads(_read_bytes_safely(manifest_path).decode("utf-8"))
                            if m.get("discovery_status") == "not_found" and m.get("status") == "not_found":
                                confirmed_not_found += 1
                            else:
                                unprocessed += 1
                        except Exception:
                            integrity_failures += 1
                    else:
                        unprocessed += 1
                elif res.status in {"unprocessed", "incomplete", "missing"}:
                    unprocessed += 1
                elif res.status == "failed":
                    discovery_failed += 1
                else:
                    integrity_failures += 1
            except Exception:
                integrity_failures += 1

        is_cohort_complete = (
            discovery_failed == 0
            and unprocessed == 0
            and integrity_failures == 0
            and (found_m1_complete + confirmed_not_found == len(pairs))
        )

        return {
            "found_m1_complete": found_m1_complete,
            "confirmed_not_found": confirmed_not_found,
            "discovery_failed": discovery_failed,
            "unprocessed": unprocessed,
            "integrity_failures": integrity_failures,
            "is_cohort_complete": is_cohort_complete,
        }

    def _resolve_batch_id(
        self,
        number: int,
        max_attempts: int,
        revision_history: bool,
        timeout: Optional[float] = None,
        s2_config: Optional[dict] = None,
        discovery_batch_size: int = 1,
        force_discovery_refresh: bool = False,
    ) -> Tuple[str, bool]:
        """Resolve a non-destructive batch_id and determine if it can safely resume."""
        from src import s2_integration

        effective_timeout = timeout if timeout is not None else self.timeout

        def _is_compatible_definition(saved_def: dict) -> bool:
            if not isinstance(saved_def, dict):
                return False
            if saved_def.get("max_attempts") != max_attempts:
                return False
            if saved_def.get("revision_history", False) != bool(revision_history):
                return False
            if saved_def.get("timeout") != effective_timeout:
                return False
            if saved_def.get("discovery_batch_size", 1) != discovery_batch_size:
                return False
            if saved_def.get("force_discovery_refresh", False) != force_discovery_refresh:
                return False

            try:
                norm_target_s2 = s2_integration.configuration(s2_config)
            except Exception:
                norm_target_s2 = None

            raw_saved_s2 = saved_def.get("s2") if "s2" in saved_def else saved_def.get("s2_config")
            try:
                norm_saved_s2 = s2_integration.configuration(raw_saved_s2)
            except Exception:
                norm_saved_s2 = None

            return norm_saved_s2 == norm_target_s2

        base_id = f"fast-arxiv-{number:04d}" if s2_config is None else f"fast-arxiv-{number:04d}-s2"
        base_dir = self.logs_dir / base_id
        batch_json_path = base_dir / "batch.json"

        def _can_resume(batch_id, directory):
            """Resume only when the complete benchmark history passes validation."""
            try:
                saved_def, history = benchmark_runner._load(
                    directory, batch_id, self.corpus_dir,
                )
                return bool(history) and _is_compatible_definition(saved_def)
            except (benchmark_runner.BenchmarkError, OSError, ValueError, KeyError,
                    TypeError, AttributeError):
                return False

        try:
            is_l, _, is_f = _inspect_path(batch_json_path)
            if not is_l and is_f:
                saved_def = json.loads(_read_bytes_safely(batch_json_path).decode("utf-8")).get("definition", {})
                if _is_compatible_definition(saved_def) and _can_resume(base_id, base_dir):
                    return base_id, True
        except Exception:
            pass

        s2_flag = 1 if s2_config is not None else 0
        refresh_flag = "-d1" if force_discovery_refresh else ""
        variant_id = f"fast-arxiv-{number:04d}-a{max_attempts}-h{int(bool(revision_history))}-s{s2_flag}{refresh_flag}"
        variant_dir = self.logs_dir / variant_id
        variant_json = variant_dir / "batch.json"
        try:
            is_l_v, _, is_f_v = _inspect_path(variant_json)
            if not is_l_v and is_f_v:
                saved_def_v = json.loads(_read_bytes_safely(variant_json).decode("utf-8")).get("definition", {})
                if _is_compatible_definition(saved_def_v) and _can_resume(variant_id, variant_dir):
                    return variant_id, True
        except Exception:
            pass

        # Nếu variant_dir đã tồn tại nhưng không tương thích, phải trả về batch_id mới không trùng.
        if variant_dir.exists():
            counter = 1
            while (self.logs_dir / f"{variant_id}-{counter}").exists():
                counter += 1
            return f"{variant_id}-{counter}", False

        return variant_id, False

    def run_cohort(
        self,
        number: int,
        *,
        resume: bool = True,
        revision_history: bool = True,
        s2_config: Optional[dict] = None,
        max_ids: Optional[int] = None,
        should_stop=None,
        on_progress: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        on_discovery_progress: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        discovery_batch_size: int = 1,
        force_discovery_refresh: bool = False,
    ) -> dict:
        """Execute one cohort (100 papers) with SSD acceleration, backoff retries, Drive sync, and revision history."""
        if should_stop is not None and not callable(should_stop):
            raise ValueError("should_stop must be callable or None")
        if on_progress is not None and not callable(on_progress):
            raise ValueError("on_progress must be callable or None")
        if on_discovery_progress is not None and not callable(on_discovery_progress):
            raise ValueError("on_discovery_progress must be callable or None")
        self._reset_recoverable_archive_conflicts(number)
        pairs = cohort_pairs(number)
        target_limit = min(max_ids, len(pairs)) if max_ids is not None else len(pairs)
        max_attempts = 6 if (self.retry_429 and self.retry_wait_budget > 0) else 1
        batch_id, can_resume = self._resolve_batch_id(
            number, max_attempts, revision_history, timeout=self.timeout, s2_config=s2_config,
            discovery_batch_size=discovery_batch_size,
            force_discovery_refresh=force_discovery_refresh,
        )
        effective_resume = can_resume if resume else False
        experiment_id = f"exp-fast-{number:04d}-{int(time.time())}"

        already_done = self.count_completed_papers(number)
        if already_done > 0:
            print(f"Lô {number}: Đã có {already_done}/{len(pairs)} bài báo hoàn thành từ phiên trước, tiếp tục thu thập các bài còn lại.")

        started = time.time()
        archive_name = f"cohort_{number:04d}_corpus.tar.gz"
        archive_dest = self.archives_dir / archive_name
        report = {}
        interrupted = False
        old_temp, old_tmpdir = tempfile.tempdir, os.environ.get("TMPDIR")
        try:
            tempfile.tempdir = os.environ["TMPDIR"] = str(self.scratch_dir)
            with arxiv_client.golden_ratio_backoff_policy(
                base_seconds=8.0,
                cap_seconds=60.0,
                wait_budget_seconds=self.retry_wait_budget if self.retry_429 else 0.0,
            ):
                report = benchmark_runner.run_benchmark(
                    pairs=pairs,
                    corpus_root=self.corpus_dir,
                    batch_id=batch_id,
                    max_ids=target_limit,
                    log_root=self.logs_dir,
                    report_root=self.reports_dir,
                    experiment_id=experiment_id,
                    resume=effective_resume,
                    max_attempts=max_attempts,
                    timeout=self.timeout,
                    disk_roots=[self.corpus_dir, self.scratch_dir],
                    s2_config=s2_config,
                    revision_history=revision_history,
                    bbl_artifact_dir=self.bbl_artifact_dir,
                    discovery_batch_size=discovery_batch_size,
                    force_discovery_refresh=force_discovery_refresh,
                    should_stop=should_stop,
                    **({"on_progress": on_progress} if on_progress is not None else {}),
                    **({"on_discovery_progress": on_discovery_progress}
                       if on_discovery_progress is not None else {}),
                )
        except KeyboardInterrupt:
            interrupted = True
            done_now = self.count_completed_papers(number)
            print(f"\nĐã tạm dừng lô {number} tại bài báo {done_now}/{len(pairs)}. Chạy lại ô lệnh để tiếp tục từ bài {done_now + 1}.")
        finally:
            tempfile.tempdir = old_temp
            if old_tmpdir is None:
                os.environ.pop("TMPDIR", None)
            else:
                os.environ["TMPDIR"] = old_tmpdir
            self._archive_cohort(number, archive_dest)

        elapsed = time.time() - started
        completed_final = self.count_completed_papers(number)
        newly_done = max(0, completed_final - already_done)
        avg_seconds = (elapsed / newly_done) if newly_done > 0 else None
        batch_stopped = bool(
            interrupted
            or report.get("stopped")
            or report.get("batch_status") in {"stopped", "interrupted", "failed"}
            or report.get("execution_status") == "raised"
        )
        was_interrupted = interrupted or report.get("batch_status") == "interrupted"
        stop_reason = report.get("stop_reason")
        if was_interrupted and not stop_reason:
            stop_reason = {"type": "interrupted", "stage": "source"}
        report["fast_runner"] = {
            "cohort": number,
            "batch_id": batch_id,
            "effective_resume": effective_resume,
            "can_resume": can_resume,
            "s2_enabled": bool(s2_config is not None),
            "interrupted": was_interrupted,
            "stopped": batch_stopped,
            "stop_reason": stop_reason,
            "completed_papers": completed_final,
            "target_papers": target_limit,
            "newly_completed_papers": newly_done,
            "elapsed_seconds": elapsed,
            "avg_seconds_per_paper": avg_seconds,
            "archive_path": str(archive_dest),
        }
        return report

    def enrich_cohort_s2(
        self,
        number: int,
        *,
        s2_config: Optional[dict] = None,
        max_ids: Optional[int] = None,
        should_stop=None,
        on_progress: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    ) -> dict:
        """Enrich a completed cohort with Semantic Scholar metadata and references.

        Runs S2 enrichment directly on source-complete papers without re-downloading arXiv TeX sources
        or invoking arXiv discovery.
        Synchronizes the enriched cohort back to Google Drive upon completion.
        """
        if should_stop is not None and not callable(should_stop):
            raise ValueError("should_stop must be callable or None")
        if on_progress is not None and not callable(on_progress):
            raise ValueError("on_progress must be callable or None")
        already_done = self.count_completed_papers(number)
        if already_done == 0:
            self.restore_cohort(number)
            already_done = self.count_completed_papers(number)

        effective_s2 = s2_config if s2_config is not None else {
            "timeout": 30.0,
            "max_attempts": 3,
            "min_interval": 3.0,
            "reference_limit": 100,
        }

        from src import pipeline, s2_integration, semantic_scholar
        from src.storage import TERMINAL, arxiv_id_to_folder_key

        cfg = s2_integration.configuration(effective_s2)
        if cfg is not None:
            semantic_scholar._key_from_environment()
        limiter = None if cfg is None else semantic_scholar.RateLimiter(cfg["min_interval"])

        pairs = cohort_pairs(number)
        if max_ids is not None:
            pairs = pairs[:max_ids]

        attempted_papers = 0
        metadata_complete = 0
        references_complete = 0
        fully_enriched = 0
        failed_or_partial = 0
        skipped_incomplete = 0
        skipped_stale_discovery = 0
        stale_discovery_ids = []
        stopped = False
        stop_reason = None
        paper_results = []

        for index, (mssv, arxiv_id) in enumerate(pairs, start=1):
            if should_stop is not None and should_stop():
                stopped = True
                stop_reason = {"type": "session_budget", "stage": "s2_before_paper",
                               "message": "Soft session budget reached; S2 checkpoint is resumable."}
                self._archive_cohort(number, self.archives_dir / f"cohort_{number:04d}_corpus.tar.gz")
                break
            if on_progress is not None:
                on_progress("paper_started", {
                    "cohort": number,
                    "index": index,
                    "total": len(pairs),
                    "mssv": mssv,
                    "arxiv_id": arxiv_id,
                    "attempted_papers": attempted_papers,
                })
            folder_key = arxiv_id_to_folder_key(arxiv_id)
            paper_dir = self.corpus_dir / folder_key
            try:
                is_link, is_d, _ = _inspect_path(paper_dir)
                if is_link or not is_d:
                    skipped_incomplete += 1
                    continue
                mf = paper_dir / "manifest.json"
                is_link_mf, _, is_f_mf = _inspect_path(mf)
                if is_link_mf or not is_f_mf:
                    skipped_incomplete += 1
                    continue
                m = json.loads(_read_bytes_safely(mf).decode("utf-8"))
            except Exception:
                skipped_incomplete += 1
                continue

            disc = m.get("discovery_status")
            if disc != "found":
                skipped_incomplete += 1
                continue

            versions = m.get("versions")
            if m.get("version_status") != "resolved" or not isinstance(versions, dict) or not versions:
                skipped_incomplete += 1
                continue

            if not all(isinstance(v, dict) and v.get("status") in TERMINAL for v in versions.values()):
                skipped_incomplete += 1
                continue

            metadata = self.store.read_metadata(mssv, arxiv_id)
            if not pipeline.cached_discovery_is_fresh(metadata):
                skipped_stale_discovery += 1
                stale_discovery_ids.append(arxiv_id)
                paper_results.append({
                    "mssv": mssv, "arxiv_id": arxiv_id,
                    "status": "skipped_stale_discovery",
                })
                continue

            paper = dict(
                mssv=mssv,
                arxiv_id=arxiv_id,
                paper_status=m.get("status"),
                metadata_status=m.get("metadata_status"),
                references_status=m.get("references_status"),
                discovery=dict(
                    action="cached",
                    status="found",
                    version_status=m.get("version_status"),
                    http_status=200,
                    attempts=1,
                    error=None,
                    wall_seconds=0.0,
                    timing_status="cached",
                ),
                source={},
                skipped=[],
                pending=[],
                blocked={},
                errors=[],
                s2_metadata=dict(action="not_attempted", status=None, http_status=None, attempts=None, error=None),
                s2_references=dict(action="not_attempted", status=None, http_status=None, attempts=None, error=None),
            )

            attempted_papers += 1
            try:
                s2_started = time.perf_counter()
                try:
                    pipeline._run_s2(self.store, paper, cfg, limiter)
                finally:
                    paper["s2_wall_seconds"] = max(0.0, time.perf_counter() - s2_started)
            except KeyboardInterrupt:
                stopped = True
                stop_reason = {"type": "interrupted", "message": "S2 paused by user; cohort checkpoint saved."}
                paper_results.append(paper)
                self._archive_cohort(number, self.archives_dir / f"cohort_{number:04d}_corpus.tar.gz")
                break
            except pipeline._HttpPolicyStop as exc:
                stopped = True
                stop_reason = exc.reason
                paper_results.append(paper)
                failed_or_partial += 1
                self._archive_cohort(number, self.archives_dir / f"cohort_{number:04d}_corpus.tar.gz")
                break
            except Exception as exc:
                stopped = True
                key = semantic_scholar._key_from_environment()
                raw_msg = str(exc)
                if key and key in raw_msg:
                    raw_msg = raw_msg.replace(key, "[REDACTED]")
                stop_reason = {
                    "type": "unexpected_exception",
                    "exception_type": type(exc).__name__,
                    "message": raw_msg,
                }
                paper_results.append(paper)
                failed_or_partial += 1
                self._archive_cohort(number, self.archives_dir / f"cohort_{number:04d}_corpus.tar.gz")
                break
            paper_results.append(paper)

            # Kiểm tra trạng thái kết thúc thực tế.
            m_ok = False
            try:
                m_after = self.store.read_metadata(mssv, arxiv_id)
                m_ok = m_after.get("status") in {"complete", "not_found"}
            except Exception:
                m_ok = (m.get("metadata_status") in {"complete", "not_found"})

            r_ok = False
            try:
                r_after = self.store.read_references(mssv, arxiv_id)
                cov = r_after.get("coverage") or {}
                r_ok = (
                    r_after.get("status") in {"complete", "not_found"}
                    and (r_after.get("status") == "not_found" or (cov.get("complete") is True and cov.get("continuation") is None))
                )
            except Exception:
                r_ok = (m.get("references_status") in {"complete", "not_found"})

            if m_ok:
                metadata_complete += 1
            if r_ok:
                references_complete += 1
            if m_ok and r_ok:
                fully_enriched += 1
            else:
                failed_or_partial += 1

            # Ghi sau mỗi bài để mất môi trường chạy không làm mất tiến độ S2 kéo dài.
            self._archive_cohort(number, self.archives_dir / f"cohort_{number:04d}_corpus.tar.gz")

        archive_dest = self.archives_dir / f"cohort_{number:04d}_corpus.tar.gz"
        self._archive_cohort(number, archive_dest)

        return {
            "cohort": number,
            "total_target": len(pairs),
            "attempted_papers": attempted_papers,
            "metadata_complete": metadata_complete,
            "references_complete": references_complete,
            "fully_enriched": fully_enriched,
            "failed_or_partial": failed_or_partial,
            "enriched_papers": fully_enriched,
            "skipped_incomplete": skipped_incomplete,
            "skipped_stale_discovery": skipped_stale_discovery,
            "stale_discovery_ids": stale_discovery_ids,
            "stopped": stopped,
            "stop_reason": stop_reason,
            "papers": paper_results,
            "paper_timings": [
                {"arxiv_id": paper["arxiv_id"], "s2_wall_seconds": paper["s2_wall_seconds"]}
                for paper in paper_results if "s2_wall_seconds" in paper
            ],
            "archive_path": str(archive_dest),
        }

    @staticmethod
    def _compute_sha256(file_path: Path) -> str:
        h = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    def backup_cohort_archive(self, number: int, *, require_sidecars: bool = False) -> Optional[Dict[str, Any]]:
        """Preserve existing cohort archive, sha256 sidecar, and manifest before updating a snapshot.

        Creates an immutable, collision-resistant, non-overwriting copy in self.archives_dir:
          cohort_{number:04d}_corpus.tar.gz -> cohort_{number:04d}_corpus.bak_<timestamp>_<sha256[:12]>[_<counter>].tar.gz
        along with matching verified .sha256 and .manifest.json sidecars.
        Returns a dictionary with backup paths and archive SHA-256, or None if no prior archive exists.
        Fails closed on any corruption, mismatch, or failure, cleaning up all partial backup files.
        """
        import shutil
        import tarfile
        import re

        archive_name = f"cohort_{number:04d}_corpus.tar.gz"
        archive_path = self.archives_dir / archive_name
        if not archive_path.exists():
            return None

        # Trước tiên xác minh có thể đọc archive mã nguồn.
        try:
            with tarfile.open(archive_path, "r:gz") as test_tar:
                _ = test_tar.getmembers()
        except Exception as exc:
            raise ValueError(f"Cannot backup unreadable or corrupt archive {archive_path}: {exc}") from exc

        sha256_cur = self._compute_sha256(archive_path)
        from src.archive_validation import validate_archive_sidecars
        # Phải chạy TRƯỚC nhánh dùng lại bản sao lưu. Archive cũ có thể
        # thiếu sidecar chỉ khi bên gọi cho phép tường minh.
        validate_archive_sidecars(archive_path, number,
                                  require_complete=require_sidecars,
                                  expected_digest=sha256_cur)
        timestamp = int(archive_path.stat().st_mtime)

        # Dùng lại bản sao lưu đã xác minh nếu nó chứa đúng các byte này.
        # Việc này ngăn các lần tiếp tục Colab tạo thêm archive trùng lặp.
        existing_prefix = f"cohort_{number:04d}_corpus.bak_"
        for existing in sorted(self.archives_dir.glob(f"{existing_prefix}*.tar.gz")):
            try:
                if self._compute_sha256(existing) != sha256_cur:
                    continue
                with tarfile.open(existing, "r:gz") as test_tar:
                    _ = test_tar.getmembers()
                existing_sha = existing.with_name(existing.name + ".sha256")
                sha_text = existing_sha.read_text(encoding="utf-8") if existing_sha.exists() else ""
                sha_match = re.search(r"[0-9a-f]{64}", sha_text)
                if not sha_match or sha_match.group(0) != sha256_cur:
                    continue
                validate_archive_sidecars(existing, number,
                                          require_complete=True,
                                          expected_digest=sha256_cur,
                                          require_backup=True)
                return {
                    "cohort": number,
                    "original_archive": str(archive_path),
                    "backup_archive": str(existing),
                    "backup_sha256_file": str(existing_sha),
                    "backup_manifest_file": str(existing.with_name(existing.name.replace(".tar.gz", ".manifest.json"))),
                    "archive_path": str(existing),
                    "sha256_path": str(existing_sha),
                    "manifest_path": str(existing.with_name(existing.name.replace(".tar.gz", ".manifest.json"))),
                    "archive_sha256": sha256_cur,
                    "reused": True,
                }
            except (OSError, ValueError, tarfile.TarError):
                # Không dùng lại bản sao lưu cũ nếu không đọc hoặc xác minh được.
                continue

        # Nếu có sidecar .sha256 gốc thì xác minh nó.
        sha_cur_file = archive_path.with_name(archive_name + ".sha256")
        if sha_cur_file.exists():
            sha_text = sha_cur_file.read_text(encoding="utf-8").strip()
            match = re.search(r"[0-9a-f]{64}", sha_text)
            if not match or match.group(0).lower() != sha256_cur.lower():
                raise ValueError(
                    f"Original sha256 sidecar is corrupt or mismatched ({match.group(0) if match else 'None'} != {sha256_cur})"
                )

        # Nếu có sidecar .manifest.json gốc thì xác minh nó.
        manifest_cur_file = archive_path.with_name(archive_name.replace(".tar.gz", ".manifest.json"))
        manifest_data = None
        if manifest_cur_file.exists():
            try:
                manifest_data = json.loads(manifest_cur_file.read_text(encoding="utf-8"))
            except Exception as exc:
                raise ValueError(f"Original manifest sidecar is corrupt JSON: {exc}") from exc
            if not isinstance(manifest_data, dict):
                raise ValueError("Original manifest sidecar is not a JSON object")
            if manifest_data.get("archive_sha256") and manifest_data["archive_sha256"] != sha256_cur:
                raise ValueError(
                    f"Original manifest sidecar archive_sha256 mismatch ({manifest_data.get('archive_sha256')} != {sha256_cur})"
                )
            if manifest_data.get("cohort_number") and manifest_data["cohort_number"] != number:
                raise ValueError(
                    f"Original manifest sidecar cohort mismatch ({manifest_data.get('cohort_number')} != {number})"
                )

        # Tạo tên gốc duy nhất, tránh va chạm.
        short_sha = sha256_cur[:12]
        base_prefix = f"cohort_{number:04d}_corpus.bak_{timestamp}_{short_sha}"
        counter = 0
        fd = -1
        candidate_tar = None
        candidate_sha = None
        candidate_manifest = None

        while True:
            suffix = f"_{counter}" if counter > 0 else ""
            candidate_base = f"{base_prefix}{suffix}"
            candidate_tar = self.archives_dir / f"{candidate_base}.tar.gz"
            candidate_sha = self.archives_dir / f"{candidate_base}.tar.gz.sha256"
            candidate_manifest = self.archives_dir / f"{candidate_base}.manifest.json"

            # Kiểm tra trước rằng tệp chưa tồn tại.
            if candidate_tar.exists() or candidate_sha.exists() or candidate_manifest.exists():
                counter += 1
                continue

            try:
                fd = os.open(str(candidate_tar), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                break
            except FileExistsError:
                counter += 1
                continue

        created_files: List[Path] = [candidate_tar]
        try:
            # 1. Ghi tuần tự nội dung archive vào tệp được tạo độc quyền.
            with open(archive_path, "rb") as f_in, os.fdopen(fd, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out, length=65536)
                f_out.flush()
                os.fsync(f_out.fileno())

            # 2. Xác minh SHA-256 của archive sao lưu bằng thuật toán băm.
            backup_sha256 = self._compute_sha256(candidate_tar)
            if backup_sha256 != sha256_cur:
                raise RuntimeError(
                    f"Backup archive verification failed: SHA-256 mismatch ({backup_sha256} != {sha256_cur})"
                )

            # 3. Xác minh archive sao lưu hợp lệ và đọc được.
            with tarfile.open(candidate_tar, "r:gz") as test_tar:
                _ = test_tar.getmembers()

            # 4. Tạo nguyên tử sidecar .sha256 đã xác minh.
            sha_fd = os.open(str(candidate_sha), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            with os.fdopen(sha_fd, "w", encoding="utf-8") as f_sha:
                f_sha.write(f"{sha256_cur}  {candidate_tar.name}\n")
                f_sha.flush()
                os.fsync(f_sha.fileno())
            created_files.append(candidate_sha)

            # Kiểm tra lại nội dung sidecar.
            sha_content = candidate_sha.read_text(encoding="utf-8")
            if not (sha256_cur in sha_content and candidate_tar.name in sha_content):
                raise RuntimeError("Backup sha256 sidecar verification failed")

            # 5. Nếu bản gốc có sidecar .manifest.json thì tạo bản đã xác minh theo cách nguyên tử.
            if manifest_data is not None:
                backup_manifest_data = dict(manifest_data)
                backup_manifest_data["archive_filename"] = candidate_tar.name
                backup_manifest_data["is_backup"] = True
                backup_manifest_data["original_archive"] = archive_path.name
                mf_fd = os.open(str(candidate_manifest), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                with os.fdopen(mf_fd, "w", encoding="utf-8") as f_mf:
                    json.dump(backup_manifest_data, f_mf, indent=2)
                    f_mf.write("\n")
                    f_mf.flush()
                    os.fsync(f_mf.fileno())
                created_files.append(candidate_manifest)

            return {
                "cohort": number,
                "original_archive": str(archive_path),
                "backup_archive": str(candidate_tar),
                "backup_sha256_file": str(candidate_sha),
                "backup_manifest_file": str(candidate_manifest) if manifest_data is not None else None,
                "archive_sha256": sha256_cur,
                "timestamp": timestamp,
            }
        except Exception:
            for f in created_files:
                try:
                    f.unlink(missing_ok=True)
                except Exception:
                    pass
            raise

    def _archive_cohort(self, number: int, destination: Path) -> None:
        """Create a compressed archive for the cohort and store in Google Drive atomically."""
        pairs = cohort_pairs(number)
        import tarfile
        import fcntl
        import uuid
        import re
        from datetime import datetime, timezone
        from src.storage import arxiv_id_to_folder_key

        destination.parent.mkdir(parents=True, exist_ok=True)
        lock_path = destination.parent / f".{destination.name}.lock"
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                os.ftruncate(lock_fd, 0)
                os.lseek(lock_fd, 0, os.SEEK_SET)
                os.write(lock_fd, f"{os.getpid()}\n".encode("utf-8"))
            except OSError:
                pass

            # Chỉ dọn an toàn tệp tạm mồ côi do tiến trình đã dừng để lại.
            cleanup_orphaned_archive_tmp(destination.parent, destination.name)

            pid = os.getpid()
            tmp_filename = f".{destination.name}.pid_{pid}_{uuid.uuid4().hex[:8]}.tmp"
            tmp_path = destination.parent / tmp_filename
            fd = os.open(str(tmp_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            included_ids: List[str] = []
            omitted: List[Dict[str, str]] = []
            try:
                with os.fdopen(fd, "wb") as f_out:
                    with tarfile.open(fileobj=f_out, mode="w:gz") as tar:
                        for _, arxiv_id in pairs:
                            safe_folder = arxiv_id_to_folder_key(arxiv_id)
                            paper_path = self.corpus_dir / safe_folder
                            try:
                                is_link, is_d, _ = _inspect_path(paper_path)
                                if is_link or not is_d:
                                    omitted.append({"arxiv_id": arxiv_id, "reason": "missing_paper_dir"})
                                    continue
                            except Exception as exc:
                                omitted.append({"arxiv_id": arxiv_id, "reason": f"uninspectable_paper_dir: {exc}"})
                                continue

                            mf = paper_path / "manifest.json"
                            try:
                                is_link_mf, _, is_f_mf = _inspect_path(mf)
                                if is_link_mf or not is_f_mf:
                                    omitted.append({"arxiv_id": arxiv_id, "reason": "missing_manifest"})
                                    continue
                                m_data = json.loads(_read_bytes_safely(mf).decode("utf-8"))
                                if not isinstance(m_data, dict):
                                    omitted.append({"arxiv_id": arxiv_id, "reason": "invalid_manifest_type"})
                                    continue
                            except Exception as exc:
                                omitted.append({"arxiv_id": arxiv_id, "reason": f"corrupt_manifest: {exc}"})
                                continue

                            has_unsafe = False
                            for root, dirs, files in os.walk(paper_path):
                                for name in dirs + files:
                                    full_p = Path(root) / name
                                    try:
                                        is_l_sub, is_d_sub, is_f_sub = _inspect_path(full_p)
                                        if is_l_sub or not (is_d_sub or is_f_sub):
                                            has_unsafe = True
                                            break
                                    except Exception:
                                        has_unsafe = True
                                        break
                                if has_unsafe:
                                    break
                            if has_unsafe:
                                omitted.append({"arxiv_id": arxiv_id, "reason": "unsafe_filesystem_entry"})
                                continue

                            tar.add(paper_path, arcname=safe_folder, recursive=True)
                            included_ids.append(arxiv_id)
                    f_out.flush()
                    os.fsync(f_out.fileno())

                # Xác minh archive mới đọc được trước khi thay thế nguyên tử.
                with tarfile.open(tmp_path, "r:gz") as test_tar:
                    _ = test_tar.getmembers()

                archive_sha256 = self._compute_sha256(tmp_path)
                os.replace(tmp_path, destination)

                # Công bố nguyên tử tệp mã kiểm tra SHA-256.
                sha_file = destination.with_name(destination.name + ".sha256")
                sha_tmp = destination.with_name(f".{destination.name}.sha256.tmp")
                sha_tmp.write_text(f"{archive_sha256}  {destination.name}\n", encoding="utf-8")
                os.replace(sha_tmp, sha_file)

                # Xác định archive_state và verified_completed_count.
                m1_stats = self.count_m1_completed_papers(number)
                source_count = self.count_source_completed_papers(number)
                if len(included_ids) < len(pairs):
                    archive_state = "checkpoint_partial"
                elif m1_stats["is_cohort_complete"]:
                    archive_state = "m1_handoff_verified"
                elif source_count == len(pairs):
                    archive_state = "source_verified"
                else:
                    archive_state = "checkpoint_partial"

                # Công bố nguyên tử manifest kiểm kê archive của lô.
                manifest_name = destination.name.replace(".tar.gz", ".manifest.json")
                manifest_file = destination.with_name(manifest_name)
                manifest_tmp = destination.with_name(f".{manifest_name}.tmp")
                bbl_artifacts = self._archive_bbl_artifacts(number)
                bbl_policy_state = self._cohort_bbl_policy_state(number)
                accounting_payload = {
                    "cohort_number": number,
                    "archive_filename": destination.name,
                    "archive_sha256": archive_sha256,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "expected_count": len(pairs),
                    "expected_ids": [aid for _, aid in pairs],
                    "included_count": len(included_ids),
                    "included_ids": sorted(included_ids),
                    "archive_state": archive_state,
                    "verified_completed_count": m1_stats["found_m1_complete"],
                    "source_completed_count": source_count,
                    "m1_stats": m1_stats,
                    "bbl_policy_state": bbl_policy_state,
                    "bbl_artifact_archive": bbl_artifacts,
                    "omitted_count": len(omitted),
                    "omitted": omitted,
                }
                manifest_tmp.write_text(json.dumps(accounting_payload, indent=2), encoding="utf-8")
                os.replace(manifest_tmp, manifest_file)
            finally:
                if tmp_path.exists():
                    try:
                        tmp_path.unlink()
                    except OSError:
                        pass
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(lock_fd)
            except OSError:
                pass

    def _cohort_bbl_policy_state(self, number: int) -> str:
        """Report whether all retained BBL files have a verified policy record."""
        from src.storage import arxiv_id_to_folder_key

        for mssv, arxiv_id in cohort_pairs(number):
            manifest_path = self.corpus_dir / arxiv_id_to_folder_key(arxiv_id) / "manifest.json"
            if not manifest_path.is_file() or manifest_path.is_symlink():
                continue
            try:
                manifest = self.store.read_manifest(mssv, arxiv_id)
            except (OSError, ValueError):
                return "unverified"
            for version, record in manifest.get("versions", {}).items():
                if (any(PurePosixPath(name).suffix.lower() == ".bbl"
                        for name in record.get("files", []))
                        and record.get("bbl_policy") is None):
                    return "legacy_unverified"
                if record.get("bbl_policy") is not None:
                    try:
                        is_valid = self.store.should_skip_version(mssv, arxiv_id, version)
                    except (OSError, ValueError):
                        return "unverified"
                    if not is_valid:
                        return "unverified"
        return "complete"

    def _archive_bbl_artifacts(self, number: int) -> dict | None:
        """Package external BBL indexes and evidence with a verified checksum map."""
        import fcntl
        import uuid
        from src.bbl_handling import validate_bbl_source_policy
        from src.storage import arxiv_id_to_folder_key

        files: dict[str, str] = {}
        for mssv, arxiv_id in cohort_pairs(number):
            paper_key = arxiv_id_to_folder_key(arxiv_id)
            try:
                manifest = self.store.read_manifest(mssv, arxiv_id)
            except (OSError, ValueError):
                continue
            for version, record in manifest.get("versions", {}).items():
                policy = record.get("bbl_policy")
                if policy is None:
                    continue
                version_dir = self.corpus_dir / paper_key / version
                validate_bbl_source_policy(
                    version_dir,
                    record.get("files", []),
                    policy,
                    artifact_root=self.bbl_artifact_dir,
                    mssv=mssv,
                    arxiv_id=arxiv_id,
                    version=version,
                )
                relatives = [policy.get("index_path")]
                for item in policy.get("items", []):
                    relatives.extend((item.get("source_evidence_path"),
                                      item.get("derived_bib_path")))
                for relative in relatives:
                    if relative is None:
                        continue
                    path = self.bbl_artifact_dir / relative
                    is_link, _, is_file = _inspect_path(path)
                    if is_link or not is_file:
                        raise ValueError(f"BBL artifact is missing or unsafe: {relative}")
                    files[relative] = self._compute_sha256(path)
        if not files:
            return None

        self.archives_dir.mkdir(parents=True, exist_ok=True)
        name = f"cohort_{number:04d}_bbl_artifacts.tar.gz"
        destination = self.archives_dir / name
        lock_path = self.archives_dir / f".{name}.lock"
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            temporary = self.archives_dir / f".{name}.pid_{os.getpid()}_{uuid.uuid4().hex[:8]}.tmp"
            try:
                with tarfile.open(temporary, "w:gz") as tar:
                    for relative in sorted(files):
                        tar.add(self.bbl_artifact_dir / relative, arcname=relative, recursive=False)
                with tarfile.open(temporary, "r:gz") as check:
                    members = check.getmembers()
                    if any(not member.isfile() for member in members):
                        raise ValueError("BBL artifact archive contains a non-file member")
                    if {member.name for member in members} != set(files):
                        raise ValueError("BBL artifact archive contents do not match its index")
                archive_sha = self._compute_sha256(temporary)
                os.replace(temporary, destination)
                sha_path = destination.with_name(destination.name + ".sha256")
                sha_tmp = sha_path.with_name(f".{sha_path.name}.tmp")
                sha_tmp.write_text(f"{archive_sha}  {destination.name}\n", encoding="utf-8")
                os.replace(sha_tmp, sha_path)
                manifest_path = destination.with_name(
                    destination.name.replace(".tar.gz", ".manifest.json")
                )
                _atomic_json(manifest_path, {
                    "cohort_number": number,
                    "archive_filename": destination.name,
                    "archive_sha256": archive_sha,
                    "files": dict(sorted(files.items())),
                    "file_count": len(files),
                })
                return {
                    "archive_filename": destination.name,
                    "archive_sha256": archive_sha,
                    "files": dict(sorted(files.items())),
                    "file_count": len(files),
                }
            finally:
                temporary.unlink(missing_ok=True)
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)

    def _safe_replace_paper(
        self,
        src_folder: Path,
        target_folder: Path,
        *,
        expected_arxiv_id: Optional[str] = None,
    ) -> bool:
        """Validate source folder manifest and checksums before staging and replacing target folder."""
        src_mf = src_folder / "manifest.json"
        try:
            is_link, _, is_f = _inspect_path(src_mf)
            if is_link or not is_f:
                return False
            m_src = json.loads(_read_bytes_safely(src_mf).decode("utf-8"))
            if not isinstance(m_src, dict) or ("discovery_status" not in m_src and "status" not in m_src):
                return False
            if expected_arxiv_id is not None and "arxiv_id" in m_src:
                if m_src.get("arxiv_id") != expected_arxiv_id:
                    return False
        except Exception:
            return False

        # Nếu phiên bản có file_checksums, xác minh checksum tệp trong src_folder.
        versions = m_src.get("versions") or {}
        for ver_name, ver_info in versions.items():
            if isinstance(ver_info, dict) and ver_info.get("status") == "success" and ver_info.get("file_checksums"):
                ver_dir = src_folder / ver_name
                try:
                    is_l_v, is_d_v, _ = _inspect_path(ver_dir)
                    if is_l_v or not is_d_v:
                        return False
                except Exception:
                    return False
                for fname, expected_hash in ver_info["file_checksums"].items():
                    fpath = ver_dir / fname
                    try:
                        is_l_f, _, is_f_f = _inspect_path(fpath)
                        if is_l_f or not is_f_f:
                            return False
                        if self._compute_sha256(fpath) != expected_hash:
                            return False
                    except Exception:
                        return False
            if isinstance(ver_info, dict) and ver_info.get("bbl_policy") is not None:
                from src.bbl_handling import validate_bbl_source_policy

                try:
                    validate_bbl_source_policy(
                        src_folder / ver_name,
                        ver_info.get("files", []),
                        ver_info["bbl_policy"],
                        artifact_root=self.bbl_artifact_dir,
                        mssv=m_src.get("mssv"),
                        arxiv_id=m_src.get("arxiv_id"),
                        version=ver_name,
                    )
                except (OSError, ValueError, TypeError):
                    return False

        try:
            is_l_tgt, is_d_tgt, _ = _inspect_path(target_folder)
            if not is_l_tgt and is_d_tgt:
                target_mf = target_folder / "manifest.json"
                try:
                    is_l_tmf, _, is_f_tmf = _inspect_path(target_mf)
                    if not is_l_tmf and is_f_tmf:
                        m_target = json.loads(_read_bytes_safely(target_mf).decode("utf-8"))
                        src_up = str(m_src.get("updated_at", ""))
                        tgt_up = str(m_target.get("updated_at", ""))
                        if tgt_up and src_up and tgt_up >= src_up:
                            return False
                except Exception:
                    pass
        except Exception:
            pass

        # Thay thế nguyên tử: chuẩn bị dữ liệu trong thư mục tạm trên cùng hệ thống tệp.
        target_parent = target_folder.parent
        target_parent.mkdir(parents=True, exist_ok=True)
        stage_dir = Path(tempfile.mkdtemp(prefix=".stage-", dir=str(target_parent)))
        backup_dir = None
        preserve_backup = False
        try:
            stage_paper = stage_dir / target_folder.name
            shutil.copytree(src_folder, stage_paper)

            try:
                is_l_tgt, is_d_tgt, _ = _inspect_path(target_folder)
            except FileNotFoundError:
                is_l_tgt, is_d_tgt = False, False
            if not is_l_tgt and is_d_tgt:
                backup_dir = Path(tempfile.mkdtemp(prefix=".backup-", dir=str(target_parent)))
                backup_paper = backup_dir / target_folder.name
                os.replace(target_folder, backup_paper)

            os.replace(stage_paper, target_folder)

            if backup_dir and backup_dir.exists():
                shutil.rmtree(backup_dir, ignore_errors=True)
            return True
        except Exception:
            if backup_dir and (backup_dir / target_folder.name).exists() and target_folder.exists():
                preserve_backup = True
                raise RestoreRollbackError(
                    f"Restore failed; original paper preserved at {backup_dir / target_folder.name}"
                )
            if backup_dir and (backup_dir / target_folder.name).exists() and not target_folder.exists():
                try:
                    os.replace(backup_dir / target_folder.name, target_folder)
                except Exception as exc:
                    preserve_backup = True
                    raise RestoreRollbackError(
                        f"Restore rollback failed; original paper preserved at {backup_dir / target_folder.name}"
                    ) from exc
            return False
        finally:
            if stage_dir.exists():
                shutil.rmtree(stage_dir, ignore_errors=True)
            if backup_dir and backup_dir.exists() and not preserve_backup:
                shutil.rmtree(backup_dir, ignore_errors=True)

    def _validate_bbl_artifact_archive(
        self,
        number: int,
        artifact_info: dict | None,
        *,
        restore: bool = False,
    ) -> dict:
        """Verify the external BBL bundle and optionally restore missing files."""
        if artifact_info is None:
            return {"status": "not_present", "file_count": 0}
        if not isinstance(artifact_info, dict):
            raise ValueError("Invalid BBL artifact archive record")
        expected_name = f"cohort_{number:04d}_bbl_artifacts.tar.gz"
        if artifact_info.get("archive_filename") != expected_name:
            raise ValueError("Unexpected BBL artifact archive filename")
        files = artifact_info.get("files")
        if (not isinstance(files, dict)
                or type(artifact_info.get("file_count")) is not int
                or artifact_info["file_count"] != len(files)):
            raise ValueError("Invalid BBL artifact checksum map")
        for relative, digest in files.items():
            if not isinstance(relative, str):
                raise ValueError("Unsafe BBL artifact checksum entry")
            path = PurePosixPath(relative)
            if (path.is_absolute() or "\\" in relative
                    or any(part in {"", ".", ".."} for part in path.parts)
                    or not isinstance(digest, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", digest)):
                raise ValueError("Unsafe BBL artifact checksum entry")

        archive_path = self.archives_dir / expected_name
        is_link, _, is_file = _inspect_path(archive_path)
        if is_link or not is_file:
            raise ValueError(f"Missing BBL artifact archive {expected_name}")
        actual_sha = self._compute_sha256(archive_path)
        if actual_sha != artifact_info.get("archive_sha256"):
            raise ValueError("BBL artifact archive checksum mismatch")
        sha_path = archive_path.with_name(archive_path.name + ".sha256")
        is_link, _, is_file = _inspect_path(sha_path)
        if is_link or not is_file:
            raise ValueError("Missing BBL artifact checksum sidecar")
        sidecar = _read_bytes_safely(sha_path).decode("utf-8").strip().split()
        if not sidecar or sidecar[0] != actual_sha:
            raise ValueError("BBL artifact checksum sidecar mismatch")

        import tarfile

        payloads = {}
        with tarfile.open(archive_path, "r:gz") as archive:
            members = archive.getmembers()
            if any(not member.isfile() for member in members):
                raise ValueError("BBL artifact archive contains unsafe member types")
            if {member.name for member in members} != set(files):
                raise ValueError("BBL artifact archive member list mismatch")
            for member in members:
                stream = archive.extractfile(member)
                if stream is None:
                    raise ValueError("Cannot read BBL artifact archive member")
                data = stream.read()
                if hashlib.sha256(data).hexdigest() != files[member.name]:
                    raise ValueError(f"BBL artifact member checksum mismatch: {member.name}")
                payloads[member.name] = data

        for relative, data in payloads.items():
            relative_path = PurePosixPath(relative)
            target = self.bbl_artifact_dir.joinpath(*relative_path.parts)
            current = self.bbl_artifact_dir
            for part in relative_path.parts:
                current = current / part
                if current.is_symlink():
                    raise ValueError(f"BBL artifact path contains a symlink: {relative}")
            if target.exists():
                if not target.is_file() or self._compute_sha256(target) != files[relative]:
                    raise ValueError(f"Existing BBL artifact conflicts with archive: {relative}")
                continue
            if not restore:
                raise ValueError(f"BBL artifact is missing from the index: {relative}")
            target.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".restore.tmp", dir=target.parent,
            )
            temporary_path = Path(temporary)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.link(temporary_path, target)
                except FileExistsError:
                    if not target.is_file() or self._compute_sha256(target) != files[relative]:
                        raise ValueError(f"Concurrent BBL artifact conflict: {relative}")
            finally:
                temporary_path.unlink(missing_ok=True)
        return {"status": "verified", "file_count": len(files)}

    def restore_cohort(
        self,
        number: int,
        *,
        require_manifest: bool = False,
        require_handoff: bool = False,
    ) -> int:
        """Unpack an existing cohort archive or legacy Drive corpus back to local SSD safely.

        Enables seamless resumption across Colab runtime reconnects in seconds.
        Migrates verified papers from legacy drive_root / "corpus" and extracts
        tar archive members without overwriting newer local files.
        Restricts restoration strictly to the cohort's assigned papers.
        Returns the count of restored/migrated paper folders.
        """
        restored = 0
        legacy_corpus = self.drive_root / "corpus"
        from src.storage import arxiv_id_to_folder_key
        if require_manifest or require_handoff:
            from src.archive_validation import validate_archive_sidecars
            validate_archive_sidecars(
                self.archives_dir / f"cohort_{number:04d}_corpus.tar.gz",
                number, require_complete=True,
            )

        valid_pairs = cohort_pairs(number)
        allowed_folders = {arxiv_id_to_folder_key(aid): (mssv, aid) for mssv, aid in valid_pairs}

        # 1. Nếu có, chuyển đổi bố cục corpus Drive cũ.
        try:
            is_l, is_d, _ = _inspect_path(legacy_corpus)
            if not is_l and is_d:
                for folder_name, (mssv, arxiv_id) in allowed_folders.items():
                    legacy_folder = legacy_corpus / folder_name
                    local_folder = self.corpus_dir / folder_name
                    try:
                        is_l_f, is_d_f, _ = _inspect_path(legacy_folder)
                        if not is_l_f and is_d_f:
                            if self._safe_replace_paper(legacy_folder, local_folder, expected_arxiv_id=arxiv_id):
                                restored += 1
                    except RestoreRollbackError:
                        raise
                    except Exception:
                        pass
        except RestoreRollbackError:
            raise
        except Exception:
            pass

        # 2. Giải nén archive của lô từ Drive mà không ghi đè bài báo cục bộ mới hơn.
        archive_path = self.archives_dir / f"cohort_{number:04d}_corpus.tar.gz"
        try:
            is_link, _, is_f = _inspect_path(archive_path)
            if is_link or not is_f:
                if require_manifest or require_handoff:
                    raise ValueError(f"Archive not found: {archive_path.name}")
                return restored
        except FileNotFoundError:
            if require_manifest or require_handoff:
                raise ValueError(f"Archive not found: {archive_path.name}")
            return restored

        actual_sha = self._compute_sha256(archive_path)
        sha_file = archive_path.with_name(archive_path.name + ".sha256")
        try:
            is_link_sha, _, is_f_sha = _inspect_path(sha_file)
            if is_link_sha or not is_f_sha:
                if require_manifest or require_handoff:
                    raise ValueError(f"Missing checksum sidecar file for {archive_path.name}")
            else:
                expected_sha = _read_bytes_safely(sha_file).decode("utf-8").strip().split()[0]
                if expected_sha != actual_sha:
                    raise ValueError(f"Archive checksum mismatch for {archive_path.name}: expected {expected_sha}, got {actual_sha}")
        except FileNotFoundError:
            if require_manifest or require_handoff:
                raise ValueError(f"Missing checksum sidecar file for {archive_path.name}")

        manifest_name = archive_path.name.replace(".tar.gz", ".manifest.json")
        manifest_file = archive_path.with_name(manifest_name)
        m_payload = None
        try:
            is_link_mf, _, is_f_mf = _inspect_path(manifest_file)
            if is_link_mf or not is_f_mf:
                if require_manifest or require_handoff:
                    raise ValueError(f"Missing accounting manifest for {archive_path.name}")
            else:
                m_payload = json.loads(_read_bytes_safely(manifest_file).decode("utf-8"))
                if not isinstance(m_payload, dict):
                    raise ValueError(f"Invalid accounting manifest format for {archive_path.name}")
                if m_payload.get("archive_sha256") != actual_sha:
                    raise ValueError(
                        f"Archive SHA-256 mismatch with accounting manifest: expected {m_payload.get('archive_sha256')}, got {actual_sha}"
                    )
                if m_payload.get("cohort_number") != number:
                    raise ValueError(
                        f"Cohort number mismatch: manifest has {m_payload.get('cohort_number')}, expected {number}"
                    )
                if require_handoff and m_payload.get("archive_state") not in {"m1_handoff_verified", "handoff_verified"}:
                    if self.reconcile_cohort_manifest(number):
                        try:
                            m_payload = json.loads(_read_bytes_safely(manifest_file).decode("utf-8"))
                        except Exception:
                            pass
                    if m_payload.get("archive_state") not in {"m1_handoff_verified", "handoff_verified"}:
                        raise ValueError(
                            f"Archive state is '{m_payload.get('archive_state')}', required 'm1_handoff_verified'"
                        )
        except FileNotFoundError:
            if require_handoff:
                if not self.reconcile_cohort_manifest(number):
                    raise ValueError(f"Missing accounting manifest for {archive_path.name}")
                try:
                    m_payload = json.loads(_read_bytes_safely(manifest_file).decode("utf-8"))
                except Exception:
                    pass
            elif require_manifest:
                raise ValueError(f"Missing accounting manifest for {archive_path.name}")

        if isinstance(m_payload, dict) and "bbl_artifact_archive" in m_payload:
            self._validate_bbl_artifact_archive(
                number, m_payload.get("bbl_artifact_archive"), restore=True,
            )

        import tarfile
        with tempfile.TemporaryDirectory(prefix=".restore-", dir=str(self.local_root)) as temp_extract:
            temp_dir = Path(temp_extract)
            tar_top_folders = set()
            try:
                with tarfile.open(archive_path, "r:gz") as tar:
                    for m in tar.getmembers():
                        p = PurePosixPath(m.name)
                        if p.is_absolute() or ".." in p.parts:
                            raise ValueError(f"Unsafe archive member: {m.name}")
                        if m.issym() or m.islnk() or m.isdev() or m.isfifo():
                            raise ValueError(f"Special files not allowed in backup: {m.name}")
                        if not (m.isfile() or m.isdir()):
                            raise ValueError(f"Unrecognized member type in backup: {m.name}")
                        if len(p.parts) > 0:
                            tar_top_folders.add(p.parts[0])
                    tar.extractall(path=temp_dir)
            except (tarfile.TarError, OSError, EOFError) as e:
                if require_manifest or require_handoff:
                    raise ValueError(f"Corrupted or unreadable archive {archive_path.name}: {e}") from e
                return restored

            # Nếu có manifest kiểm kê, đối chiếu nghiêm ngặt nội dung archive với manifest đó.
            if m_payload is not None and "included_ids" in m_payload:
                expected_folders = {arxiv_id_to_folder_key(aid) for aid in m_payload["included_ids"]}
                if tar_top_folders != expected_folders:
                    raise ValueError(
                        f"Archive contents mismatch accounting manifest included_ids for {archive_path.name}: "
                        f"tar has {sorted(tar_top_folders)}, manifest specifies {sorted(expected_folders)}"
                    )

            for item in temp_dir.iterdir():
                if item.is_dir() and item.name in allowed_folders:
                    _, expected_aid = allowed_folders[item.name]
                    target_folder = self.corpus_dir / item.name
                    if self._safe_replace_paper(item, target_folder, expected_arxiv_id=expected_aid):
                        restored += 1
        return restored

    def reconcile_cohort_manifest(self, number: int) -> bool:
        """Reconstruct or reconcile the cohort accounting manifest from an existing valid archive.

        Handles crash scenarios where .tar.gz and .sha256 were published but the process was
        interrupted before the accounting manifest was committed or updated.
        """
        archive_name = f"cohort_{number:04d}_corpus.tar.gz"
        archive_dest = self.archives_dir / archive_name
        try:
            is_l, _, is_f = _inspect_path(archive_dest)
            if is_l or not is_f:
                return False
        except Exception:
            return False

        actual_sha = self._compute_sha256(archive_dest)
        sha_file = archive_dest.with_name(archive_dest.name + ".sha256")
        try:
            is_l_s, _, is_f_s = _inspect_path(sha_file)
            if is_l_s or not is_f_s:
                sha_tmp = archive_dest.with_name(f".{archive_dest.name}.sha256.tmp")
                sha_tmp.write_text(f"{actual_sha}  {archive_dest.name}\n", encoding="utf-8")
                os.replace(sha_tmp, sha_file)
            else:
                expected_sha = _read_bytes_safely(sha_file).decode("utf-8").strip().split()[0]
                if expected_sha != actual_sha:
                    return False
        except Exception:
            return False

        # Dùng chung bộ kiểm tra toàn diện; từ chối khi phát hiện bất kỳ vi phạm toàn vẹn nào.
        try:
            audit_res = audit_cohort_archive_payload(archive_dest, number, require_full=False)
        except Exception:
            return False

        pairs = cohort_pairs(number)
        if audit_res["is_cohort_complete"]:
            archive_state = "m1_handoff_verified"
        else:
            archive_state = "checkpoint_partial"

        from datetime import datetime, timezone
        manifest_name = archive_dest.name.replace(".tar.gz", ".manifest.json")
        manifest_file = archive_dest.with_name(manifest_name)
        manifest_tmp = archive_dest.with_name(f".{manifest_name}.tmp")
        artifact_info = None
        artifact_manifest_path = self.archives_dir / (
            f"cohort_{number:04d}_bbl_artifacts.manifest.json"
        )
        if artifact_manifest_path.is_file() and not artifact_manifest_path.is_symlink():
            try:
                artifact_payload = json.loads(
                    _read_bytes_safely(artifact_manifest_path).decode("utf-8")
                )
                artifact_info = {
                    "archive_filename": artifact_payload["archive_filename"],
                    "archive_sha256": artifact_payload["archive_sha256"],
                    "files": artifact_payload["files"],
                    "file_count": artifact_payload["file_count"],
                }
                self._validate_bbl_artifact_archive(number, artifact_info, restore=False)
            except (OSError, ValueError, KeyError, TypeError):
                return False
        accounting_payload = {
            "cohort_number": number,
            "archive_filename": archive_dest.name,
            "archive_sha256": actual_sha,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "expected_count": len(pairs),
            "expected_ids": [aid for _, aid in pairs],
            "included_count": audit_res["included_count"],
            "included_ids": audit_res["included_ids"],
            "archive_state": archive_state,
            "verified_completed_count": audit_res["found_m1_complete"],
            "source_completed_count": audit_res["found_m1_complete"],
            "m1_stats": {
                "found_m1_complete": audit_res["found_m1_complete"],
                "confirmed_not_found": audit_res["confirmed_not_found"],
                "discovery_failed": 0,
                "unprocessed": len(pairs) - audit_res["included_count"],
                "integrity_failures": 0,
                "is_cohort_complete": audit_res["is_cohort_complete"],
            },
            "bbl_policy_state": audit_res.get("bbl_policy_state", "unverified"),
            "bbl_artifact_archive": artifact_info,
            "omitted_count": audit_res["omitted_count"],
            "omitted": audit_res["omitted"],
        }
        try:
            manifest_tmp.write_text(json.dumps(accounting_payload, indent=2), encoding="utf-8")
            os.replace(manifest_tmp, manifest_file)
            return True
        except Exception:
            return False


def verify_cohort_handoff(
    runner: FastProductionRunner,
    number: int,
    *,
    inspect_members: bool = True,
) -> dict:
    """Verify that a cohort archive is fully compliant for M1 final handoff.

    Requires:
    1. Archive .tar.gz exists and is a regular file.
    2. Checksum sidecar .sha256 exists and matches archive sha256.
    3. Accounting manifest .manifest.json exists and sha256 matches.
    4. archive_state is 'm1_handoff_verified' or 'handoff_verified'.
    5. included_count equals the full cohort count (100 papers).
    6. When inspect_members=True:
       - All tar members are safe (no absolute paths, no ../ traversal, no symlinks, no devices).
       - All top-level folders match expected cohort paper folders with zero foreign IDs.
       - Every paper's manifest.json inside the archive is verified:
         - found papers must have status complete/complete_with_no_tex/complete_with_withdrawn, version_status resolved,
           metadata_status complete, and references_status complete.
         - not_found papers must have status not_found.
    """
    pairs = cohort_pairs(number)
    archive_path = runner.archives_dir / f"cohort_{number:04d}_corpus.tar.gz"
    sha_file = archive_path.with_name(archive_path.name + ".sha256")
    manifest_file = archive_path.with_name(f"cohort_{number:04d}_corpus.manifest.json")

    try:
        is_l, _, is_f = _inspect_path(archive_path)
        if is_l or not is_f:
            raise ValueError(f"Handoff archive not found or invalid: {archive_path}")
    except (FileNotFoundError, OSError) as e:
        raise ValueError(f"Handoff archive not found or invalid: {e}") from e

    actual_sha = runner._compute_sha256(archive_path)

    try:
        is_l_sha, _, is_f_sha = _inspect_path(sha_file)
        if is_l_sha or not is_f_sha:
            raise ValueError(f"Missing checksum sidecar file: {sha_file}")
    except (FileNotFoundError, OSError) as e:
        raise ValueError(f"Missing checksum sidecar file: {e}") from e
    expected_sha = _read_bytes_safely(sha_file).decode("utf-8").strip().split()[0]
    if expected_sha != actual_sha:
        raise ValueError(f"Checksum mismatch: expected {expected_sha}, got {actual_sha}")

    try:
        is_l_mf, _, is_f_mf = _inspect_path(manifest_file)
        if is_l_mf or not is_f_mf:
            raise ValueError(f"Missing accounting manifest: {manifest_file}")
    except (FileNotFoundError, OSError) as e:
        raise ValueError(f"Missing accounting manifest: {e}") from e
    m_payload = json.loads(_read_bytes_safely(manifest_file).decode("utf-8"))

    manifest_cohort = m_payload.get("cohort_number") if "cohort_number" in m_payload else m_payload.get("cohort_id")
    if manifest_cohort is not None and manifest_cohort != number:
        raise ValueError(f"Cohort number mismatch in manifest: expected {number}, got {manifest_cohort}")

    if m_payload.get("archive_sha256") != actual_sha:
        raise ValueError(f"Accounting manifest sha256 mismatch: {m_payload.get('archive_sha256')} vs {actual_sha}")
    if m_payload.get("archive_state") not in {"m1_handoff_verified", "handoff_verified"}:
        raise ValueError(
            f"Cohort {number} archive_state is '{m_payload.get('archive_state')}', expected 'm1_handoff_verified'"
        )
    if m_payload.get("included_count") != len(pairs):
        raise ValueError(f"Cohort {number} included_count is {m_payload.get('included_count')}, expected {len(pairs)}")

    inspected_count = 0
    if inspect_members:
        audit_res = audit_cohort_archive_payload(archive_path, number, require_full=True)
        inspected_count = audit_res["inspected_papers"]
    bbl_artifact_check = runner._validate_bbl_artifact_archive(
        number, m_payload.get("bbl_artifact_archive"), restore=False,
    )
    bbl_policy_state = runner._cohort_bbl_policy_state(number)
    recorded_bbl_state = m_payload.get("bbl_policy_state")
    if recorded_bbl_state == "complete" and bbl_policy_state != "complete":
        raise ValueError(
            f"BBL policy was recorded complete but currently audits as {bbl_policy_state}"
        )

    return {
        "cohort_number": number,
        "archive_path": str(archive_path),
        "archive_sha256": actual_sha,
        "archive_state": m_payload["archive_state"],
        "included_count": m_payload["included_count"],
        "expected_count": len(pairs),
        "verified": True,
        "inspected_papers": inspected_count,
        "bbl_policy_state": bbl_policy_state,
        "bbl_artifact_archive": bbl_artifact_check,
    }
