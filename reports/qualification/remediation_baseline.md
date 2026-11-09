# Milestone 1 Colab Release Qualification — Remediation Baseline

**Remediation Baseline Date:** 2026-11-10  
**Repository:** `Toan-ten/Arxiv-Project-Group10`  
**Working Branch:** `feat/m1-t02-colab-resume`  
**Current Verified HEAD:** `09a1429d2f2d93e5fae16d443217462faab9db5f`  
**Remote origin/feat/m1-t02-colab-resume HEAD:** `09a1429d2f2d93e5fae16d443217462faab9db5f`  
**Production Notebook Path:** `notebooks/m1_colab_fast_production.ipynb`  
**Production Notebook SHA-256:** `57ecd0a5addf3d4beb263065152f96a478c99cb5617cbac25e07e4bbbf6195b1`  
**Runtime Execution Commit (`EXECUTION_COMMIT`):** `69b4b5e758f74d255ff244c56271e294da57b8e4`  
**Historical Code Commit Pin (`CODE_COMMIT`):** `489a82fd959ccc6cced7c59fc309255e4421bef2`  
**Current Test Suite Status:** 1,046 passed, 981 subtests passed, 0 failures, 0 errors.

---

## 1. Context and Remediation Scope

An independent review of the initial qualification report identified several critical areas where safety, documentation, and evidence claims must be strengthened before any member handoff:

1. **R01 — Production Notebook Default-Mode Safety**:
   The notebook currently defaults to `MSSV = "24120260"` and `MODE = "MEMBER_PRODUCTION"`. If a student opens the shared notebook and clicks "Run All", it will immediately start crawling 50 cohorts (5,000 papers). It must be changed to safe `MODE = "PILOT"` by default, and `MEMBER_PRODUCTION` must require explicit confirmation.
2. **R02 — Notebook and Member Guide Synchronization**:
   Cell references and execution variables in `docs/M1_COLAB_MEMBER_GUIDE.md` must accurately mirror the 8 cells of `notebooks/m1_colab_fast_production.ipynb`, providing two distinct workflows: Workflow A (Pilot) and Workflow B (Production).
3. **R03 — Production Archive, Restore, and Handoff Verification**:
   Testing must directly invoke production APIs (`fresh_runner.restore_cohort(cohort_id)` and `verify_cohort_handoff(runner, cohort_id)`), testing partial archives, complete archives, and adversarial corruption cases (path traversal, corrupted checksums, unexpected files) without relying on manual `shutil.unpack_archive` shortcuts.
4. **R04 — Process-Level Interruption and Resume Qualification**:
   Interruption tests must prove recovery after real process termination (subprocess execution killed mid-flight), verifying that in-memory state is cleared, on-disk state is preserved, and resume avoids redundant downloads.
5. **R05 — Evidence-Level Corrections**:
   Explicitly correct overclaimed evidence levels. Disallow labeling local E1/E2 tests as E4/E5, and clearly maintain `NOT_VERIFIED` for clean interactive Colab runs (E3) in headless container environments.
6. **R06 — Release Decision Policy**:
   Remove the policy rule that live Google Drive mounting automatically transitions the release to `APPROVED_FOR_MEMBER_HANDOFF`. Define 12 strict prerequisite gates (G01–G12).
7. **R07 — Git Provenance & CI Verification**:
   Ensure all changes are committed in structured commits and verified against the full test suite.

---

## 2. Existing Baseline Evidence Status

| Evidence Item | Previous Claim | Verified Actual Level | Remediation Action |
|:---|:---:|:---:|:---|
| **E0 Static Inspection** | E0 | E0 (Verified) | Notebook cell structure audited. R01 defaults and R02 documentation to be updated. |
| **E1 Unit Tests** | E1 | E1 (Verified) | 1,046 passing tests verified. New regression tests for R01, R03, R04 to be added. |
| **E2 Local Integration Simulation** | E2 | E2 (Verified) | Clean-room local crawler and storage verified. Real subprocess interruption to be added. |
| **E3 Clean Colab CPU Execution** | E3 / E2 | E2 only (`NOT_VERIFIED` for E3) | Retain `NOT_VERIFIED` due to headless agent boundary; human member verification required. |
| **E4 Interruption / Resume** | E4 / E1 | E1/E2 only | Add real subprocess process-kill test; do not claim E4 Colab execution. |
| **E5 Archive / Restore** | E5 / E1 | E1/E2 only | Add real `restore_cohort()` and corruption tests; do not claim E5 Colab Drive execution. |
| **E6 Member Handoff Usability** | E6 | E0/E1 only | Synchronize operational guide with exact notebook cells. |

---

## 3. Findings Register (Baseline)

- **F-STATIC-01 (Closed)**: `all_pairs` missing in Cell 7 if run standalone $\rightarrow$ Fixed with generator fallback.
- **F-STATIC-02 (Closed)**: `DRIVE_ROOT` reassignment in Cell 7 $\rightarrow$ Fixed with `globals().get("DRIVE_ROOT", ...)`.
- **F-STATIC-03 (Closed)**: README cohort table 0-indexed vs 1-indexed $\rightarrow$ Fixed to 1-indexed.
- **F-REM-01 (Open - R01)**: Unsafe production default `MODE = "MEMBER_PRODUCTION"` in Cell 5.
- **F-REM-02 (Open - R01)**: Lack of affirmative confirmation barrier before 50-cohort run.
- **F-REM-03 (Open - R02)**: Mismatch between member guide cell narrative and actual notebook cells.
- **F-REM-04 (Open - R03)**: Archive restore test bypassed `restore_cohort()` and used `shutil.unpack_archive()`.
- **F-REM-05 (Open - R04)**: Interruption test simulated in-process without process termination.
- **F-REM-06 (Open - R05)**: Overclaimed evidence levels in qualification matrix.
- **F-REM-07 (Open - R06)**: Premature automatic transition to `APPROVED_FOR_MEMBER_HANDOFF` upon Drive mount.

**Acceptance**: Baseline re-established and documented.
