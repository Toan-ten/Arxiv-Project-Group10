# Milestone 1 Colab Release Qualification — Reporting Metrics & Reconciliation Audit

**Qualification Run Date:** 2026-11-10  
**Repository:** `Toan-ten/Arxiv-Project-Group10`  
**Branch:** `feat/m1-t02-colab-resume`  
**Execution Runtime Commit:** `69b4b5e758f74d255ff244c56271e294da57b8e4`  
**Historical CODE_COMMIT:** `489a82fd959ccc6cced7c59fc309255e4421bef2`  
**Metric Specification:** `docs/contracts/reporting_metrics.md` (v2.1.0)  
**Evidence Level:** E1 (Test Suite `tests/test_reporting_correctness.py` & `tests/test_colab_release_qualification.py`)

---

## 1. Metric Mathematical Definitions and Invariants

Every metric in the final report must be derived directly from persisted filesystem structures and verified by independent reconciliation.

### 1.1 Core Counting Invariants
1. **Total Assigned Papers**:
   $$\text{total\_assigned} = 5,000 \text{ per student} \quad (\sum = 20,000)$$
2. **Discovery Partition Invariant**:
   $$\text{total\_assigned} = \text{total\_discovered} + \text{unprocessed} + \text{confirmed\_not\_found} + \text{discovery\_failed}$$
3. **Completeness Partition Invariant**:
   $$\text{total\_discovered} = \text{m1\_complete\_papers} + \text{incomplete\_papers}$$
   Where:
   $$\text{m1\_complete\_papers} = \text{m1\_complete\_with\_tex} + \text{m1\_complete\_no\_tex}$$
4. **Source Classification**:
   - `total_tex_sources`: count of papers where at least one version has verified `.tex` source files.
   - `total_no_tex_source`: count of papers where arXiv explicitly provides no TeX source (`status: "no_tex_source"`).
5. **Reference Preservation**:
   - `total_references`: exact sum of all valid items in `references.json` across all papers. Non-arXiv references are preserved and counted.

---

## 2. Independent Fail-Closed Reconciliation (Two-Pass Audit)

The reporting module `src/reporting.py` does not rely on transient memory counters or accumulator variables. It executes a rigorous two-pass verification:

### Pass 1: Corpus Walk & Aggregation
- Iterates over all assigned `(mssv, arxiv_id)` pairs.
- Inspects storage directories, parses manifests, metadata, and references.
- Runs `audit_paper_integrity(store, mssv, arxiv_id)` to evaluate fail-closed integrity.

### Pass 2: Independent Disk Reconciliation (`_perform_independent_reconciliation`)
- Evaluates raw disk structures independently of Pass 1 in-memory objects.
- Performs 12 strict checks for every paper claiming M1 completeness:
  1. Real non-symlink paper directory within canonical corpus root.
  2. Uncorrupted, non-symlink `manifest.json`.
  3. Valid identity: `contract_version == 2`, `arxiv_id`, `mssv`.
  4. Discovery status: `discovery_status == "found"`.
  5. Terminal manifest status: `status in {"complete", "complete_with_no_tex"}`.
  6. Terminal versions: All versions match regex `v[1-9][0-9]*` with terminal status.
  7. SHA-256 byte audit: Every source file verified against on-disk content.
  8. Uncorrupted `metadata.json`: Authors, title, submission date, version history provenance.
  9. Valid `references.json`: Coverage flags, exact counts, item schema.
  10. Directory cleanliness: No untracked files, temp files, or leftover lock files.
  11. Integrity violation check: `audit_paper_integrity` must yield zero integrity failures.
  12. Two-pass consistency: Pass 1 classification must match Pass 2 classification.

---

## 3. Distinction: Reconciliation Consistency vs Corpus Integrity

A critical remediation from Phase 1 audit:
- **Corpus Integrity**: Indicates whether all papers meet the storage schema contract, have complete files, valid JSON, and uncorrupted SHA-256 digests. If any manifest has invalid JSON, `corpus_integrity` is **FAIL**.
- **Reconciliation Consistency**: Indicates whether Pass 1 and Pass 2 independently arrived at the identical classification.
- **Fail-Closed Rule**: A paper with invalid JSON or missing files is classified as incomplete/failed in both passes (reconciliation consistent), but corpus integrity is strictly marked `FAIL` with explicit violation records. No defect is silently ignored.

---

## 4. Verification Evidence & Test Results

The metric definitions, arithmetic invariants, and two-pass reconciliation logic are verified under automated unit and integration tests:
- `tests/test_reporting_correctness.py`: 100% pass (verifies fail-closed filesystem errors, missing manifests, symlink rejection, corrupted JSON, and arithmetic invariants).
- `tests/test_colab_release_qualification.py`: 100% pass (verifies end-to-end report generation on pilot cohorts).

**Result:** Compliant and Qualified for Production.
