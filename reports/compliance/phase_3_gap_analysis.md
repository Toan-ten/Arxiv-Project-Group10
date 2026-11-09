# Milestone 1 Full Compliance Audit — Phase 3: Gap Analysis & Risk Register

**Audit Date:** 2026-11-10  
**Repository:** `Arxiv-Project-Group10`  
**Working Branch:** `feat/m1-t02-colab-resume`  
**Current HEAD SHA:** `09a142909dd215157d306ebe709989d398524977`  
**Audit Standard:** `docs/M1_REQUIREMENTS_TRACEABILITY.md`

---

## 1. Compliance Summary Table

| Category | Count | Percentage |
|:---|:---:|:---:|
| **Total Requirements Evaluated** | **27** | 100.0% |
| **PASS** | **25** | 92.6% |
| **PARTIAL** | **2** | 7.4% |
| **FAIL** | **0** | 0.0% |
| **NOT_VERIFIED** | **0** | 0.0% |
| **NOT_APPLICABLE** | **0** | 0.0% |
| **Blocker Severity Findings** | **1** | (Operational: Unsafe default notebook mode) |
| **High Severity Findings** | **2** | (Operational: E3 Colab OAuth boundary, in-process vs subprocess interruption) |
| **Medium Severity Findings** | **2** | (Operational: Guide synchronization, separate Jupyter Book repo interface) |
| **Low Severity Findings** | **0** | — |

---

## 2. Requirement-by-Requirement Status Register

| Requirement ID | Domain | Compliance Status | Severity | Notes & Evidence |
|:---|:---:|:---:|:---:|:---|
| `REQ-ARXIV-01` | arXiv | **PASS** | — | Fully verified in `tests/test_arxiv_history.py` and `test_colab_release_qualification.py`. |
| `REQ-ARXIV-02` | arXiv | **PASS** | — | Verified in `tests/test_arxiv_source.py` targeting `/src/{id}v{N}` on `export.arxiv.org`. |
| `REQ-ARXIV-03` | arXiv | **PASS** | — | Enforced in `src/arxiv_source.py`; `arxiv.org` host rejected. |
| `REQ-ARXIV-04` | arXiv | **PASS** | — | Enforced by `RateLimiter(MIN_INTERVAL=3.0)`. Verified in `tests/test_arxiv_client.py`. |
| `REQ-ARXIV-05` | arXiv | **PASS** | — | Conforms to modern `arxiv.Client().results(search)` syntax of `arxiv.py 4.0.0`. |
| `REQ-DATA-01` | Data | **PASS** | — | Excludes figures by extraction; only `.tex` and `.bib` saved. |
| `REQ-DATA-02` | Data | **PASS** | — | Binary TeX bytes untouched; `\includegraphics` commands strictly preserved. |
| `REQ-DATA-03` | Data | **PASS** | — | Non-TeX / PDF-only records saved with `status: "no_tex_source"`. |
| `REQ-DATA-04` | Data | **PASS** | — | `export_papers_table` matches exact 6-column schema. |
| `REQ-DATA-05` | Data | **PASS** | — | `export_references_table` matches exact 6-column schema. |
| `REQ-S2-01` | S2 | **PASS** | — | Step 4 (Cell 6) invokes S2 citation enrichment in production notebook. |
| `REQ-S2-02` | S2 | **PASS** | — | Flat query fields enforced in `src/semantic_scholar.py`. |
| `REQ-S2-03` | S2 | **PASS** | — | All references preserved in corpus; arXiv citations filtered for exported table. |
| `REQ-S2-04` | S2 | **PASS** | — | Pre-2007 legacy IDs (`hep-th/9901001`) supported and preserved. |
| `REQ-S2-05` | S2 | **PASS** | — | Monotonic rate limiter enforces 3.0s unauthenticated / 1.0s authenticated pacing. |
| `REQ-STORAGE-01` | Storage | **PASS** | — | Working corpus held locally and backed up to Google Drive. |
| `REQ-STORAGE-02` | Storage | **PASS** | — | Folder layout uses safe keys (e.g. `2508-07872`). |
| `REQ-REPOSITORY-01`| Repo | **PASS** | — | Public GitHub repository `Toan-ten/Arxiv-Project-Group10` on branch `feat/m1-t02-colab-resume`. |
| `REQ-REPOSITORY-02`| Repo | **PASS** | — | Git ignores data, archives, parquet, csv, and credentials. |
| `REQ-REPOSITORY-03`| Repo | **PASS** | — | Standard layout: `src/`, `requirements.txt`, `README.md`, `tests/`, `docs/`, `notebooks/`. |
| `REQ-REPORT-01` | Report | **PARTIAL** | Medium | Separate public Jupyter Book repo is an external submission deliverable. Code repository provides data feeder scripts (`scripts/generate_m1_report.py`). |
| `REQ-REPORT-02` | Report | **PASS** | — | Reports and markdown tables dynamically generated from on-disk JSON data. |
| `REQ-REPORT-03` | Report | **PASS** | — | Statistics calculate success rate, sizes before/after figure removal, and citation metrics. |
| `REQ-BENCHMARK-01`| Benchmark | **PASS** | — | Wall time per paper, average time per paper, and discovery time recorded. |
| `REQ-BENCHMARK-02`| Benchmark | **PASS** | — | Peak RAM, average RAM (via `psutil`), max disk, and final size tracked. |
| `REQ-COLAB-01` | Colab | **PARTIAL** | High | Clean-room simulation passes (E2), but actual live Colab runtime execution (E3) requires interactive Google OAuth login by a human member. |
| `REQ-INTEGRATION-01`| Integration | **PASS** | — | Verified foundation for M2 hierarchical parsing and M3 reference matching. |

---

## 3. Operational Risk Register & Remediations

| Finding ID | Severity | Root Cause | Impact | Required Remediation | Verification Gate |
|:---:|:---:|:---|:---|:---|:---:|
| **GAP-REM-01** | **BLOCKER** | Cell 5 previously defaulted to `MODE = "MEMBER_PRODUCTION"` and `MSSV = "24120260"` without confirmation. | Accidental 5,000-ID execution if a member clicks "Run All". | Default to `MODE = "PILOT"` and require explicit `CONFIRM_PRODUCTION = True` for production. | `TestNotebookSafetyAndConfirmation` |
| **GAP-REM-02** | **MEDIUM** | Member guide cell numbers and configuration paths did not match the 8-cell layout. | Member confusion or improper configuration. | Update `docs/M1_COLAB_MEMBER_GUIDE.md` to map 1:1 with all 8 cells and define Workflows A & B. | Documentation audit |
| **GAP-REM-03** | **HIGH** | Archive restore test used `shutil.unpack_archive` instead of testing production API `restore_cohort()`. | Untested production restore API; missing adversarial tests (path traversal, symlinks). | Add unit tests directly exercising `restore_cohort()` and corruption rejection. | `TestArchiveRestoreAndHandoffIntegrity` |
| **GAP-REM-04** | **HIGH** | Interruption test only constructed a second Python object rather than terminating a real OS process. | Does not prove memory loss and clean recovery across process boundaries. | Add subprocess termination test simulating process SIGTERM and fresh-process resume. | `TestProcessInterruptionAndResume` |
| **GAP-REM-05** | **MEDIUM** | Qualification matrix previously labeled local simulation as E4/E5. | Overclaimed evidence levels. | Downgrade local tests to E1/E2; reserve E3/E4/E5 for actual Colab runs. | Matrix audit |
| **GAP-REM-06** | **HIGH** | Previous acceptance rule automatically promoted release to approved upon Drive mount. | Premature release authorization. | Establish 12 strict prerequisite gates (G01–G12) requiring full evidence before handoff. | Policy audit |

**Gate G02 Passed:** Gap analysis and risk register established.
