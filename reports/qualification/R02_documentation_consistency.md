# Milestone 1 Qualification Remediation — R02: Documentation Consistency

**Remediation Date:** 2026-11-10  
**Repository:** `Toan-ten/Arxiv-Project-Group10`  
**Branch:** `feat/m1-t02-colab-resume`  
**Target Documents:** `docs/M1_COLAB_MEMBER_GUIDE.md` & `notebooks/m1_colab_fast_production.ipynb`  
**Status:** **RESOLVED & VERIFIED**

---

## 1. Audit Findings & Discrepancies Resolved

The independent review identified several critical documentation inconsistencies in the previous member guide:
1. **Cell Numbering Inaccuracy**: The previous guide referenced cell numbers that did not match the actual 8-cell layout of `notebooks/m1_colab_fast_production.ipynb`.
2. **Missing Configuration Variables**: The guide did not document the `MODE` or `CONFIRM_PRODUCTION` safety flags introduced in R01.
3. **Discrepancy in Drive Output Paths**: The previous guide suggested paths like `/content/drive/MyDrive/ArxivDataGroup10/{MSSV}` while the notebook used a hardcoded shared root `/content/drive/MyDrive/Group10_M1_Production`.
4. **Lack of Dual-Workflow Separation**: The guide blurred the boundary between pilot testing and production crawling, risking premature production runs.

---

## 2. Remediated Execution Contract & Alignment

### 2.1 1:1 Cell Mapping Matrix
Every cell in `notebooks/m1_colab_fast_production.ipynb` is now explicitly mapped in `docs/M1_COLAB_MEMBER_GUIDE.md`:
- **Cell 1**: Markdown — Colab badge.
- **Cell 2**: Markdown — Operating principles (CPU-only, single-worker, storage contract v2).
- **Cell 3 (Bước 1)**: Code — Drive mount, repository checkout at `EXECUTION_COMMIT`, dependency installation.
- **Cell 4 (Bước 2)**: Code — Optional Kaggle snapshot preloading via Colab Secrets.
- **Cell 5 (Bước 3)**: Code — Core crawling engine (`MSSV`, `MODE = "PILOT"`, `CONFIRM_PRODUCTION`).
- **Cell 6 (Bước 4)**: Code — Semantic Scholar citation enrichment (`SEMANTIC_SCHOLAR_API_KEY`).
- **Cell 7 (Bước 5)**: Code — Two-pass fail-closed reporting & integrity audit (`m1_audit_summary_mssv_{MSSV}_{MODE}.md`).
- **Cell 8 (Bước 6)**: Code — Optional Parquet and CSV tabular export.

### 2.2 Dual Workflows Clearly Defined
- **Workflow A (Pilot Pre-Flight)**: Mandatory 1-cohort test with `MODE = "PILOT"`. Every member must run this first.
- **Workflow B (Member Production)**: Authorized 50-cohort run with `MODE = "MEMBER_PRODUCTION"` and `CONFIRM_PRODUCTION = True`. Strictly marked with caution: cannot be executed before full release approval.

### 2.3 Variable Reconciliation
All variables documented in the guide now match the notebook exactly:
- `MSSV`: Member ID ("24120010", "24120035", "24120232", "24120260").
- `MODE`: "PILOT" (default) or "MEMBER_PRODUCTION".
- `CONFIRM_PRODUCTION`: Boolean flag (default `False`).
- `LOCAL_ROOT`: `f'/content/workspace_{MSSV}'`.
- `DRIVE_ROOT`: `f'/content/drive/MyDrive/ArxivDataGroup10/{MSSV}'`.
- `EXECUTION_COMMIT`: `69b4b5e758f74d255ff244c56271e294da57b8e4`.
- `CODE_COMMIT`: `489a82fd959ccc6cced7c59fc309255e4421bef2`.

**Conclusion:** Documentation and notebook are 100% synchronized and verified.
