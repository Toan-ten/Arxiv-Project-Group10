# Milestone 1 Qualification Remediation — R03: Production Archive, Restore, and Handoff Integrity

**Remediation Date:** 2026-11-10  
**Repository:** `Toan-ten/Arxiv-Project-Group10`  
**Branch:** `feat/m1-t02-colab-resume`  
**Target Modules:** `src/fast_production.py` (`restore_cohort`, `verify_cohort_handoff`, `_archive_cohort`), `tests/test_colab_release_qualification.py`  
**Severity of Initial Finding:** High (Deficiency in qualification rigor; reliance on generic unzip shortcuts)  
**Status:** **RESOLVED & VERIFIED**

---

## 1. Context and Problem Statement

In earlier qualification runs, archive restoration was primarily verified by calling generic `shutil.unpack_archive()` or `tar -xzf` into a temporary folder. While this proved basic tarball decompression, it bypassed the project's actual production ingestion pipeline:
1. It did not test the production `FastProductionRunner.restore_cohort(cohort_id)` API.
2. It did not exercise sidecar checksum validation (`.sha256`).
3. It did not validate the accounting manifest (`.manifest.json`).
4. It did not test adversarial defense against corrupted archives, path traversal (`../`), absolute paths (`/`), symlinks, or foreign paper injection.
5. It did not verify `verify_cohort_handoff(runner, cohort_id)` compliance for final student delivery.

To ensure production safety and bulletproof data portability across Google Colab runtimes, the archive/restore qualification suite was expanded to directly test all production APIs against both nominal and adversarial inputs.

---

## 2. Production API Architecture

### 2.1 Direct Restoration Pipeline (`restore_cohort`)
`FastProductionRunner.restore_cohort(number, require_manifest=False, require_handoff=False)`:
- Locates `drive_root / "archives" / f"cohort_{number:04d}_corpus.tar.gz"`.
- Computes actual SHA-256 digest of the archive.
- Reads `.sha256` sidecar and verifies exact cryptographic match.
- Reads `.manifest.json` sidecar, verifies `archive_sha256`, `cohort_number`, and `archive_state`.
- Inspects tarball members inside a secure temporary directory:
  - Rejects any member with absolute paths (`p.is_absolute()`).
  - Rejects any member containing parent directory traversal (`".." in p.parts`).
  - Rejects special file types (symlinks `m.issym()`, hardlinks `m.islnk()`, character/block devices `m.isdev()`, FIFOs `m.isfifo()`).
- Cross-validates unpacked directory keys against the cohort's assigned paper IDs (`cohort_pairs(number)`).
- Safely migrates verified papers into `local_root / "corpus"` using atomic operations without overwriting newer valid local papers.

### 2.2 Formal Handoff Verification (`verify_cohort_handoff`)
`verify_cohort_handoff(runner, number)`:
- Confirms archive `.tar.gz`, sidecar `.sha256`, and accounting `.manifest.json` exist.
- Validates that archive checksum matches sidecar and accounting manifest.
- Requires `archive_state == "handoff_verified"`.
- Enforces `included_count == 100` (full cohort completeness).
- Verifies that all 100 IDs match `cohort_pairs(number)`.
- Validates terminal completion status for all included papers.

---

## 3. Adversarial and Corruption Qualification Tests

Six comprehensive test cases were implemented in `TestArchiveRestoreAndHandoffIntegrity` (`tests/test_colab_release_qualification.py`):

| Test Case | Attack / Scenario Simulated | Expected Defense | Verified Result |
|:---|:---|:---|:---:|
| `test_independent_destination_restore_via_production_api` | Nominal export, archive, fresh runner restore, and handoff verification | `restore_cohort` unpacks all papers; checksums match; `verify_cohort_handoff` passes | **PASS** |
| `test_restore_rejects_checksum_mismatch` | Tampered archive bytes or corrupted `.sha256` sidecar | Raises `ValueError("Archive checksum mismatch...")` | **PASS** |
| `test_restore_rejects_path_traversal_archive` | Malicious tarball with `../evil.txt` member escaping target directory | Raises `ValueError("Unsafe archive member: ../evil.txt")` | **PASS** |
| `test_restore_rejects_absolute_path_archive` | Malicious tarball with `/tmp/evil.txt` member targeting filesystem root | Raises `ValueError("Unsafe archive member: /tmp/evil.txt")` | **PASS** |
| `test_restore_rejects_symlink_in_archive` | Malicious tarball containing symlink pointing to sensitive system file | Raises `ValueError("Special files not allowed in backup: link_to_passwd")` | **PASS** |
| `test_restore_ignores_foreign_papers_not_in_cohort` | Archive containing valid paper from Cohort 2 injected into Cohort 1 | Foreign paper excluded; only assigned Cohort 1 papers restored | **PASS** |

---

## 4. Execution Evidence

The full qualification test suite was executed under pytest:

```bash
pytest -v tests/test_colab_release_qualification.py::TestArchiveRestoreAndHandoffIntegrity
```

Output:
```text
tests/test_colab_release_qualification.py::TestArchiveRestoreAndHandoffIntegrity::test_independent_destination_restore_via_production_api PASSED [ 16%]
tests/test_colab_release_qualification.py::TestArchiveRestoreAndHandoffIntegrity::test_restore_ignores_foreign_papers_not_in_cohort PASSED [ 33%]
tests/test_colab_release_qualification.py::TestArchiveRestoreAndHandoffIntegrity::test_restore_rejects_absolute_path_archive PASSED [ 50%]
tests/test_colab_release_qualification.py::TestArchiveRestoreAndHandoffIntegrity::test_restore_rejects_checksum_mismatch PASSED [ 66%]
tests/test_colab_release_qualification.py::TestArchiveRestoreAndHandoffIntegrity::test_restore_rejects_path_traversal_archive PASSED [ 83%]
tests/test_colab_release_qualification.py::TestArchiveRestoreAndHandoffIntegrity::test_restore_rejects_symlink_in_archive PASSED [100%]
============================== 6 passed in 1.42s ===============================
```

## 5. Conclusion & Safety Evaluation

The production archive and restore engine satisfies all criteria for Milestone 1 handoff:
1. Zero custom code is required to extract archives on standard systems (`tar -xzf` works universally).
2. The production `restore_cohort` API completely rejects malicious, tampered, or mismatched archives fail-closed.
3. Handoff verification (`verify_cohort_handoff`) provides mathematical proof of cohort completeness and integrity prior to member delivery.
