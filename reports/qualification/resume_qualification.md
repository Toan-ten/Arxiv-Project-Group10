# Milestone 1 Colab Release Qualification — Interruption & Resume Qualification

**Qualification Run Date:** 2026-11-10  
**Repository:** `Toan-ten/Arxiv-Project-Group10`  
**Branch:** `feat/m1-t02-colab-resume`  
**Execution Runtime Commit:** `69b4b5e758f74d255ff244c56271e294da57b8e4`  
**Historical CODE_COMMIT:** `489a82fd959ccc6cced7c59fc309255e4421bef2`  
**Evidence Level:** E1 (Unit Test Suite `tests/test_colab_release_qualification.py`) & E2 (Simulation)

---

## 1. Objective and Architecture

Colab runtimes are ephemeral and subject to disconnection (browser close, network drops, 12-hour timeout, or idle disconnects). The Milestone 1 architecture must guarantee:
1. **Zero Data Loss on Interruption**: Completed papers are synced or packed into cohort archives stored in Google Drive (`DRIVE_ROOT/archives/cohort_{number:04d}_corpus.tar.gz`).
2. **Deterministic Resumption**: When a new runtime starts with only Google Drive mounted, `restore_cohort(n)` unpacks prior work, checks manifests, and skips already-completed papers without re-downloading or making duplicate API calls.
3. **Bit-Identical Idempotency**: Running a cohort multiple times yields identical file hashes, consistent manifests, and unchanged checksums.

---

## 2. 6-Stage Interruption & Resume Verification Matrix

| Stage | Interruption / Action Point | Verification Protocol | Observed Behavior | Status |
|:---:|:---|:---|:---|:---:|
| **Stage 1** | Interruption after 1 paper | Force terminate process mid-cohort after 1 paper complete. Inspect state. | Paper folder `2508-07872` contains `manifest.json` with status `complete`. Partial files in other folders cleaned up. | **PASS** |
| **Stage 2** | Interruption after cohort archive | Archive generated (`cohort_0001_corpus.tar.gz`) on Google Drive, then kill runtime. | Drive archive exists, contains valid gzip tarball with member/cohort manifest and paper directory. | **PASS** |
| **Stage 3** | Complete runtime kill & clean restart | Simulate fresh Colab session by discarding local workspace entirely (`rm -rf /content/workspace`), leaving only Drive. | Local workspace starts empty: `fresh_runner.corpus_dir` has 0 papers before restore. | **PASS** |
| **Stage 4** | Run resume in fresh runtime | Call `fresh_runner.restore_cohort(1)`. | Runner detects `cohort_0001_corpus.tar.gz` on Drive, unpacks all completed papers into local workspace, and returns count of restored papers. | **PASS** |
| **Stage 5** | Verify no re-downloading | Execute cohort crawl on resumed workspace. | Manifest status `complete` causes crawler to skip paper immediately; 0 network requests dispatched for completed items. | **PASS** |
| **Stage 6** | Compare SHA-256 with uninterrupted run | Compute SHA-256 of all extracted `.tex` and `.json` files before interruption and after resume. | Checksums match byte-for-byte (`orig_hash == resumed_hash`). Tarball deterministic re-archive verified. | **PASS** |

---

## 3. Implementation Invariants Verified

### 3.1 Idempotent Cohort Re-execution
Verified in `test_idempotent_repeated_execution`:
- Re-running an archive operation on the same directory produces deterministic results.
- Source `.tex` files retain their original SHA-256 digest (`hashlib.sha256(tex_path.read_bytes()).hexdigest()`).
- File timestamps inside tar archives do not cause schema corruption or manifest invalidation.

### 3.2 Recovery Protocol for Colab Members
When a Colab runtime terminates unexpectedly:
1. Member reconnects to a new Colab runtime (Standard CPU).
2. Runs Cell 1 (Mount Google Drive).
3. Runs Cell 2 (Clone repo and checkout `feat/m1-t02-colab-resume`).
4. Runs Cell 3 (Install pinned dependencies).
5. Runs Cell 4 (Configure member MSSV and cohort index).
6. Runs Cell 5 (Verify environment & directories).
7. Runs Cell 6 (Execute crawl — runner automatically calls `restore_cohort()` for each cohort before processing).
8. Only remaining incomplete papers in the cohort will be processed.
