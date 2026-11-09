# Milestone 1 Full Compliance Audit — Phase 0: Repository Baseline

**Audit Date:** 2026-11-10  
**Repository Name:** `Arxiv-Project-Group10`  
**Remote URL:** `https://github.com/Toan-ten/Arxiv-Project-Group10`  
**Current Working Branch:** `feat/m1-t02-colab-resume`  
**Exact Git HEAD SHA:** `09a142909dd215157d306ebe709989d398524977`  
**Working Tree Status:** Clean working tree relative to committed code; uncommitted qualification remediation files (`docs/M1_COLAB_MEMBER_GUIDE.md`, `notebooks/m1_colab_fast_production.ipynb`, `reports/qualification/R01_notebook_safety.md`, `reports/qualification/R02_documentation_consistency.md`, `reports/qualification/remediation_baseline.md`).  
**Automated CI Status:** GitHub Actions workflow `.github/workflows/tests.yml` configured for Ubuntu Python 3.11 & 3.12, running `compileall` and full `pytest` offline test suite.

---

## 1. Commit Provenance and History

The latest 5 commits on `feat/m1-t02-colab-resume`:
1. `09a142909dd215157d306ebe709989d398524977` — `docs(m1): add qualification reports and member operational runbook`
2. `c435cfcc6fbe8593ba4f479ea298ebfa217db550` — `fix(m1): resolve notebook static audit defects and docs consistency`
3. `4d77508ca58f553a6771d538e1a17c7689bb556c` — `test(m1): add notebook release qualification test suite`
4. `2d633e2e34cf8b96959d9ab9fd8147f53995d58a` — `docs(repo): add repository structure, contributing guidelines, acceptance report, and update README`
5. `a88e42095f9c15feae7a6fc7f8eec4a29a3a71b1` — `refactor(repo): organize docs into subdirectories, move pilots to config/pilots, and remove duplicate requirements`

---

## 2. Complete Repository Inventory

The repository is structured into distinct functional tiers:

### 2.1 Authoritative Assignment Requests (`request/`)
- `request/index.md`: Overarching project logistics, milestones, Jupyter Book requirements, Hugging Face Parquet dataset hosting, portrait A2 poster session.
- `request/crawling.md`: **Authoritative Milestone 1 Data Crawling specification** (objectives, entry discovery, full-text source download, reference extraction, schema requirements, submission format, grading criteria).
- `request/parsing.md`: Milestone 2 specification (hierarchical tree parsing, standardization, bibtex).
- `request/matching.md`: Milestone 3 specification (reference matching ML pipeline, MRR@5).
- `request/link.md` & `request/paper.md`: Reference URLs to course materials.

### 2.2 Production Entry Points vs Experimental / Legacy (`notebooks/` & `scripts/`)
- **Official Production Entry Point:** `notebooks/m1_colab_fast_production.ipynb` (Colab CPU notebook orchestrating single-worker 50-cohort crawls with Drive backup and two-pass verification).
- **Secondary / Legacy Production Notebook:** `notebooks/m1_colab_arxiv_production.ipynb` (pure arXiv API crawler without Kaggle snapshot acceleration).
- **Benchmarking & Experimental Notebooks:**
  - `notebooks/m1_colab_cpu_pilot.ipynb`: Small pilot testbed.
  - `notebooks/m1_colab_strategy_comparison.ipynb`: Compares live API vs Kaggle-accelerated caching strategies.
- **Production Scripts (`scripts/`):**
  - `scripts/generate_m1_report.py`: CLI driver for independent metrics calculation and markdown report generation.
  - `scripts/m1_production.py`: Orchestrates batch crawls across cohorts.
  - `scripts/live_smoke.py`: Live network smoke test verifying polite arXiv 3.0s pacing.

### 2.3 Core Application Packages (`src/`)
- `src/fast_production.py`: High-throughput production engine with SSD staging, automated Google Drive tarball archiving, `restore_cohort()`, and `verify_cohort_handoff()`.
- `src/storage.py` & `src/storage_integrity.py`: Storage Contract v2 implementation and fail-closed integrity verification.
- `src/reporting.py`: Canonical statistics, metrics calculation, and two-pass independent disk reconciliation.
- `src/arxiv_client.py`, `src/arxiv_discovery.py`, `src/arxiv_source.py`, `src/arxiv_history.py`: arXiv API Atom feed parser, versioned `/src/` tarball downloader, and version history provenance.
- `src/semantic_scholar.py` & `src/s2_integration.py`: Semantic Scholar API client, citation graph extractor, and local cache validation.
- `src/export.py`: Projection from internal corpus structure into official `papers` and `references` Parquet and CSV tables.
- `src/benchmark.py`, `src/benchmark_metrics.py`, `src/benchmark_runner.py`: Auditable wall-time, process-tree RSS RAM, and disk footprint sampling.
- `src/id_ranges.py` & `config/group10_id_ranges.json`: Single source of truth for the 20,000 assigned arXiv IDs across 4 members.

### 2.4 Automated Test Suite (`tests/`)
- 30 test modules containing 1,046 passing tests.
- 100% offline, deterministic mocks, fail-closed security, storage schema verification, and pilot validation.

---

## 3. Existing Evidence Baseline

- Historical `CODE_COMMIT`: `489a82fd959ccc6cced7c59fc309255e4421bef2`.
- Historical `EXECUTION_COMMIT`: `69b4b5e758f74d255ff244c56271e294da57b8e4`.
- Core test results: 1,046 passed offline in 28.47s.
- Prior qualification reports preserved under `reports/qualification/` and `reports/remediation/`.

**Baseline Established.** Ready for Phase 1 requirements extraction.
