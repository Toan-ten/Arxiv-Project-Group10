# Milestone 1 Full Compliance Audit — Phase 4: Remediation Plan

**Plan Date:** 2026-11-10  
**Repository:** `Arxiv-Project-Group10`  
**Working Branch:** `feat/m1-t02-colab-resume`  
**Current HEAD SHA:** `09a142909dd215157d306ebe709989d398524977`  
**Audit Reference:** `reports/compliance/phase_3_gap_analysis.md`

---

## 1. Remediation Strategy & Prioritization

Remediation tasks are prioritized according to the strict hierarchy:
1. Operational Safety & Default-Mode Guardrails (`GAP-REM-01`, `GAP-REM-06`).
2. Production API Verification & Corruption Hardening (`GAP-REM-03`).
3. Subprocess Process-Termination & Resume Verification (`GAP-REM-04`).
4. Documentation-to-Code Traceability (`GAP-REM-02`).
5. Truthful Evidence Matrix & 12 Strict Release Gates (`GAP-REM-05`).
6. Downstream Integration & Jupyter Book Feeder Verification (`REQ-REPORT-01`).

---

## 2. Detailed Task Breakdown

### Task T01: Production Notebook Default-Mode Safety (GAP-REM-01, REQ-COLAB-01)
- **Target File:** `notebooks/m1_colab_fast_production.ipynb`
- **Specification:**
  - Hardcode safe default: `MODE = "PILOT"`, `CONFIRM_PRODUCTION = False`.
  - Guard `MEMBER_PRODUCTION`: require `CONFIRM_PRODUCTION = True` or halt execution fail-closed with `RuntimeError`.
  - Output isolation: `DRIVE_ROOT = f'/content/drive/MyDrive/ArxivDataGroup10/{MSSV}'`.
- **Tests Required:** `TestNotebookSafetyAndConfirmation` in `tests/test_colab_release_qualification.py`.
- **Acceptance Criteria:** Notebook runs in PILOT by default (1 cohort only); production crawl cannot be started accidentally without explicit affirmative confirmation.

### Task T02: Member Guide & Notebook Synchronization (GAP-REM-02, REQ-REPOSITORY-03)
- **Target File:** `docs/M1_COLAB_MEMBER_GUIDE.md`
- **Specification:**
  - 1:1 mapping of all 8 notebook cells with titles, inputs, outputs, side effects, and re-run safety.
  - Distinct Workflows: Workflow A (Pilot) and Workflow B (Production).
  - Explicit warning: Workflow B cannot be started prior to official release sign-off.
- **Acceptance Criteria:** Every referenced variable and cell matches notebook JSON.

### Task T03: Strengthen Production Archive/Restore & Corruption Rejection (GAP-REM-03, REQ-STORAGE-01)
- **Target File:** `tests/test_colab_release_qualification.py`
- **Specification:**
  - Test `fresh_runner.restore_cohort(cohort_id)` directly on fresh destination.
  - Add corruption tests: checksum tampering, path traversal (`../evil.txt`), absolute paths (`/etc/passwd`), symlinks, and foreign cohort papers.
  - Test `verify_cohort_handoff()` API for compliant vs incomplete cohorts.
- **Acceptance Criteria:** All adversarial archive tests raise `ValueError` without corrupting local files.

### Task T04: Real Subprocess Process-Termination & Resume (GAP-REM-04, REQ-STORAGE-01)
- **Target File:** `tests/test_colab_release_qualification.py`
- **Specification:**
  - Launch crawler in an independent Python subprocess (`subprocess.Popen`).
  - Terminate mid-cohort using SIGTERM (`proc.terminate()`), verifying in-memory state is wiped.
  - Start a clean fresh Python process pointing only to Google Drive backups.
  - Resume cohort crawl; verify restored files match uninterrupted run.
- **Acceptance Criteria:** Process killed with returncode `-15`; fresh resumed process completes cohort and produces matching SHA-256 digests.

### Task T05: Truthful Evidence Matrix & 12 Strict Release Gates (GAP-REM-05, GAP-REM-06, REQ-COLAB-01)
- **Target File:** `docs/M1_COLAB_RELEASE_ACCEPTANCE.md`
- **Specification:**
  - Clearly classify evidence levels: E0 (Static), E1 (Unit tests), E2 (Simulation), E3 (Colab CPU), E4 (Interruption resume), E5 (Archive restore), E6 (Member handoff).
  - Establish 12 prerequisite release gates (G01–G12).
  - Remove rule that Drive mount automatically transitions release to approved.
- **Acceptance Criteria:** All matrix entries accurately reflect actual execution evidence.

### Task T06: Jupyter Book Feeder & Downstream Parquet Verification (REQ-REPORT-01, REQ-DATA-04, REQ-DATA-05)
- **Target Files:** `src/export.py`, `scripts/generate_m1_report.py`
- **Specification:**
  - Verify `papers.parquet` and `references.parquet` schemas match `request/crawling.md`.
  - Verify `generate_m1_report.py` outputs Markdown and JSON summaries for Jupyter Book compilation.
- **Acceptance Criteria:** Tables validate under `validate_exported_tables()`; report compiles cleanly.

**Gate G03 Passed:** Remediation plan established and prioritized.
