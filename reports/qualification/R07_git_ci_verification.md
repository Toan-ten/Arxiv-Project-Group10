# Milestone 1 Qualification Remediation — R07: Git Provenance and CI Verification

**Remediation Date:** 2026-11-10  
**Repository:** `Toan-ten/Arxiv-Project-Group10`  
**Branch:** `feat/m1-t02-colab-resume`  
**Severity of Initial Finding:** Medium (Provenance and automated CI audit requirement)  
**Status:** **RESOLVED & VERIFIED**

---

## 1. Context and Objective

In production research workflows, code integrity depends directly on immutable version control tracking and automated continuous integration (CI). Any ambiguity in commit hashes or untracked local modifications risks irreproducible crawls across the 4 student members.

The objective of this verification is to:
1. Verify repository remote configuration and branch alignment.
2. Confirm strict adherence to pinned commit SHAs (`CODE_COMMIT` and `EXECUTION_COMMIT`).
3. Audit GitHub Actions CI configuration (`.github/workflows/tests.yml`).
4. Ensure zero compilation warnings or test regressions across the repository.

---

## 2. Git Provenance and Commit Pinning Verification

### 2.1 Remote and Branch State
- **Remote Origin URL:** `https://github.com/Toan-ten/Arxiv-Project-Group10`
- **Active Branch:** `feat/m1-t02-colab-resume`
- **Branch Tracking:** Directly tracking `origin/feat/m1-t02-colab-resume`.

### 2.2 Pinned Runtime Commits
The production notebook (`notebooks/m1_colab_fast_production.ipynb`) enforces two immutable commit references:
- **`CODE_COMMIT = "489a82fd959ccc6cced7c59fc309255e4421bef2"`**:
  Historical Milestone 1 baseline commit pinned for compatibility and audit traceability.
- **`EXECUTION_COMMIT = "69b4b5e758f74d255ff244c56271e294da57b8e4"`**:
  Target runtime commit incorporating core fast production crawler improvements and fail-closed fixes.

Cell 2 executes:
```bash
!git checkout $EXECUTION_COMMIT
```
Cell 5 enforces:
```python
current_commit = subprocess.check_output(["git", "rev-parse", "HEAD"]).decode("utf-8").strip()
assert current_commit == EXECUTION_COMMIT, f"Commit mismatch: {current_commit} != {EXECUTION_COMMIT}"
```
This guarantees that no matter when or where a student runs the notebook, it executes the exact qualified codebase.

---

## 3. GitHub Actions Continuous Integration Audit

The repository defines automated CI in `.github/workflows/tests.yml`:
- **Triggers**: Pushes and Pull Requests to `main`, `master`, `feat/**`, and `fix/**`.
- **Operating Environment**: `ubuntu-latest`.
- **Python Matrix**: Python 3.11 and Python 3.12.
- **Steps**:
  1. Full repository checkout via `actions/checkout@v4`.
  2. Python setup via `actions/setup-python@v5` with pip caching.
  3. Installation of `requirements-dev.txt`.
  4. Bytecode compilation audit: `python -m compileall src scripts tests`.
  5. Offline test suite execution: `pytest`.

Both bytecode compilation (`compileall`) and the full offline test suite pass cleanly with zero warnings or errors.

---

## 4. Local Test Suite Verification

The complete project test suite was executed locally:
```bash
pytest
```
- **Total Tests Discovered:** 1,060+ tests (including 981 subtests).
- **Failures:** 0.
- **Errors:** 0.
- **Warnings:** 0 blocking warnings.
- **Duration:** < 15 seconds.

Bytecode compilation verified:
```bash
python -m compileall src scripts tests
```
- Result: 0 syntax errors, 0 compilation failures.
