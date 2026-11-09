# Milestone 1 Full Compliance Audit — Phase 2: Implementation Traceability Audit

**Audit Date:** 2026-11-10  
**Repository:** `Arxiv-Project-Group10`  
**Working Branch:** `feat/m1-t02-colab-resume`  
**Current HEAD SHA:** `09a142909dd215157d306ebe709989d398524977`  
**Audit Standard:** `docs/M1_REQUIREMENTS_TRACEABILITY.md` (Frozen Baseline)

---

## 1. Traceability Mapping Matrix

| Req ID | Required Capability | Implementation Module & Symbol | Notebook Cell | Existing Tests & Verification | Audit Finding / Implementation Notes |
|:---|:---|:---|:---:|:---|:---|
| **REQ-ARXIV-01** | All version discovery (v1..vN) | `src.arxiv_history.resolve_paper_history`<br>`src.fast_production.FastProductionRunner.run_cohort(..., revision_history=True)` | Cell 5 | `tests/test_arxiv_history.py`<br>`tests/test_colab_release_qualification.py::test_multi_version_paper_storage` | **Implemented**: Parses Atom XML `<feed>` or Kaggle snapshot version lists, crawls all versions `v1` to `vN`. |
| **REQ-ARXIV-02** | Versioned `/src/{id}v{N}` endpoint from `export.arxiv.org` | `src.arxiv_source.download_source`<br>`url = f"https://export.arxiv.org/src/{versioned_id}"` | Cell 5 | `tests/test_arxiv_source.py`<br>`tests/test_arxiv_client.py` | **Implemented**: Targets `https://export.arxiv.org/src/{versioned_id}` directly using urllib/requests. |
| **REQ-ARXIV-03** | Automated downloads strictly at `export.arxiv.org` | `src.arxiv_source.ARXIV_SOURCE_HOST = "export.arxiv.org"` | Cell 5 | `tests/test_arxiv_source.py::test_disallowed_host_raises` | **Implemented**: Host header and URL enforce `export.arxiv.org`. Rejects requests to `arxiv.org`. |
| **REQ-ARXIV-04** | Polite pacing ($\ge 3.0$s interval, single worker, User-Agent) | `src.arxiv_client.RateLimiter`<br>`src.arxiv_client.MIN_INTERVAL = 3.0`<br>`src.arxiv_client.USER_AGENT` | Cell 5 | `tests/test_arxiv_client.py::test_shared_pacing_between_separate_calls`<br>`scripts/live_smoke.py` | **Implemented**: Rate limiter enforces monotonic clock $\ge 3.0$s delay between calls. Custom User-Agent identifies group and MSSV. |
| **REQ-ARXIV-05** | Modern `arxiv.py` 4.x compatibility | `src.arxiv_client.query_arxiv_official`<br>`arxiv.Client(delay_seconds=3.0).results(...)` | Cell 5 | `tests/test_arxiv_client.py::test_query_arxiv_with_official_arxiv_client` | **Implemented**: Uses `arxiv>=4.0.0` modern generator `client.results(search)`. Compatible with both official library and fallback Atom parser. |
| **REQ-DATA-01** | Figure binary removal by extraction (`.tex` & `.bib` kept) | `src.arxiv_source.extract_source_files`<br>`ALLOWED_EXTENSIONS = {".tex", ".bib"}` | Cell 5 | `tests/test_arxiv_source.py::test_extract_excludes_figures`<br>`tests/test_colab_release_qualification.py::test_figure_exclusion_and_tex_command_preservation` | **Implemented**: Extracts only `.tex` and `.bib` files. `.png`, `.jpg`, `.pdf`, `.eps` binary files are omitted during extraction. |
| **REQ-DATA-02** | Preserve LaTeX commands (`\includegraphics` untouched) | `src.arxiv_source.extract_source_files` | Cell 5 | `tests/test_colab_release_qualification.py::test_figure_exclusion_and_tex_command_preservation` | **Implemented**: Writes raw bytes without AST regex transformations or macro stripping. |
| **REQ-DATA-03** | Explicit empty record for PDF-only / non-TeX | `src.storage.record_source_result(status="no_tex_source")` | Cell 5 | `tests/test_storage_integrity.py::test_audit_no_tex_source_semantics`<br>`tests/test_colab_release_qualification.py::test_no_tex_source_explicit_record` | **Implemented**: PDF-only papers recorded with `status: "no_tex_source"`; no fake `.tex` files created; counts toward complete M1 corpus. |
| **REQ-DATA-04** | Parquet / CSV schema for `papers` table | `src.export.export_papers_table`<br>`src.export.export_to_parquet` | Cell 8 | `tests/test_export.py::test_export_papers_table_schema` | **Implemented**: Columns match `arxiv_id`, `title`, `authors`, `submission_date`, `revised_dates`, `venue`. |
| **REQ-DATA-05** | Parquet / CSV schema for `references` table | `src.export.export_references_table`<br>`src.export.export_to_parquet` | Cell 8 | `tests/test_export.py::test_export_references_table_schema` | **Implemented**: Columns match `arxiv_id`, `ref_arxiv_id`, `ref_title`, `ref_authors`, `ref_submission_date`, `ref_s2_id`. |
| **REQ-S2-01** | Crawl reference metadata via Semantic Scholar API | `src.semantic_scholar.fetch_paper_references`<br>`src.pipeline._run_s2` | Cell 6 | `tests/test_semantic_scholar.py`<br>`tests/test_s2_integration.py` | **Implemented**: Queries `https://api.semanticscholar.org/graph/v1/paper/arXiv:{arxiv_id}`. |
| **REQ-S2-02** | S2 query flat fields | `src.semantic_scholar.S2_FIELDS = "title,authors,year,publicationDate,venue,externalIds"` | Cell 6 | `tests/test_semantic_scholar.py` | **Implemented**: Uses comma-separated flat fields; rejects nested paths. |
| **REQ-S2-03** | Store all references locally; filter to arXiv references for export | `src.storage.save_references`<br>`src.export.export_references_table` | Cell 6, Cell 8 | `tests/test_colab_release_qualification.py::test_s2_references_without_arxiv_ids_preserved` | **Implemented**: Local `references.json` retains all items; `export_references_table` filters to `ref_arxiv_id is not None`. |
| **REQ-S2-04** | Retain pre-2007 arXiv identifiers (`archive/YYMMNNN`) | `src.id_ranges.arxiv_id_to_folder_key`<br>`src.storage.CorpusStorage` | Cell 5, Cell 6 | `tests/test_id_ranges.py::test_legacy_arxiv_id_conversion` | **Implemented**: Legacy IDs like `hep-th/9901001` converted to filesystem safe keys `hep-th-9901001` and preserved. |
| **REQ-S2-05** | Respect S2 rate limits (1 req/s, 100 req/5min) | `src.semantic_scholar.RateLimiter`<br>`src.local_config` | Cell 6 | `tests/test_semantic_scholar.py::test_rate_limiter` | **Implemented**: Monotonic rate limiter with 3.0s pacing unauthenticated (1.0s authenticated via API key). |
| **REQ-STORAGE-01**| No intermediate data submission | Project operational architecture | Project-wide | `reports/qualification/baseline.md` | **Implemented**: Raw corpus stays on local SSD / Google Drive. |
| **REQ-STORAGE-02**| Working folder organization (`2310-12345`) | `src.storage.CorpusStorage`<br>`src.id_ranges.arxiv_id_to_folder_key` | Cell 5 | `tests/test_storage.py` | **Implemented**: Publication folder keys replace dots and slashes with hyphens (e.g., `2508-07872`). |
| **REQ-REPOSITORY-01**| Single public GitHub repository with progress commits | Git configuration & branch history | Repository | `git log` | **Implemented**: Repository `Toan-ten/Arxiv-Project-Group10` on branch `feat/m1-t02-colab-resume`. |
| **REQ-REPOSITORY-02**| Exclude data files and raw binaries from repository | `.gitignore`<br>`tests/test_fast_production.py::test_credential_scanner_regression` | Root | `tests/test_fast_production.py` | **Implemented**: `.gitignore` excludes `corpus/`, `*.tar.gz`, `*.parquet`, `*.csv`. |
| **REQ-REPOSITORY-03**| Clean structure (`src/`, `requirements.txt`, `README.md`) | Repository file tree | Root | `docs/architecture/repository-structure.md` | **Implemented**: Fully modular structure with `src/`, `requirements.txt`, `README.md`, `tests/`, `docs/`, `notebooks/`. |
| **REQ-REPORT-01** | Jupyter Book separate public repository | Lab book specification & repository scope | External / Documentation | `docs/M1_COLAB_RELEASE_ACCEPTANCE.md` | **Partial/External**: The report repository is a separate repo hosted on GitHub Pages per assignment logistics. Data & report generation scripts (`scripts/generate_m1_report.py`) live here. |
| **REQ-REPORT-02** | Figures/tables generated dynamically during compilation | `scripts/generate_m1_report.py`<br>`src.reporting.format_markdown_summary` | Cell 7 | `tests/test_generate_m1_report.py` | **Implemented**: Markdown summary and metrics dynamically computed from on-disk JSON without static screenshots. |
| **REQ-REPORT-03** | Required statistics (success count, rate, sizes before/after, refs) | `src.reporting.compute_m1_statistics`<br>`docs/contracts/reporting_metrics.md` | Cell 7 | `tests/test_reporting_correctness.py` | **Implemented**: Two-pass fail-closed calculation of all assigned, discovered, source-complete, and reference metrics. |
| **REQ-BENCHMARK-01**| Wall time per paper, average time per paper, discovery time | `src.benchmark_metrics.MetricCollector`<br>`src.benchmark_runner.run_benchmark` | Cell 5 | `tests/test_benchmark_metrics.py`<br>`tests/test_benchmark_runner.py` | **Implemented**: Wall-clock sampling records discovery time, download time, and total time per paper. |
| **REQ-BENCHMARK-02**| Memory footprint (max RAM, average RAM, max disk, final size) | `src.benchmark_metrics.ResourceSampler`<br>`psutil.Process().memory_info().rss` | Cell 5 | `tests/test_benchmark_metrics.py` | **Implemented**: Periodic RSS sampling of process tree via `psutil` and `shutil.disk_usage`. |
| **REQ-COLAB-01** | Benchmarking testbed on Google Colab CPU-only | Notebook environment guards (`COLAB_GPU` checks) | Cell 3 | `tests/test_fast_production.py::test_fast_production_notebook_safety_guards` | **Implemented**: Explicit guard halts execution if GPU is active (`accelerator must be None/CPU`). |
| **REQ-INTEGRATION-01**| Working foundation for M2 parsing and M3 reference matching | `src.export`<br>`docs/contracts/m1_contract.md` | Cell 8 | `tests/test_export.py` | **Implemented**: Outputs raw LaTeX and standardized Parquet tables (`papers`, `references`) feeding directly into M2/M3. |

---

## 2. In-Depth Technical Verification Notes

### 2.1 arXiv Client & arxiv.py 4.x Modern Style (REQ-ARXIV-05)
- **Finding:** The repository provides two entry points for discovery:
  1. `src.arxiv_client.query_arxiv_official` directly executes `arxiv.Client(delay_seconds=3.0).results(arxiv.Search(id_list=[arxiv_id]))` conforming to `arxiv.py 4.0.0` modern generator syntax.
  2. `src.arxiv_client.query_arxiv` provides a lightweight standard-library Atom HTTP fallback with identical 3.0s polite pacing, exponential backoff, and XML parsing.
  3. `src.kaggle_discovery.ingest_kaggle_snapshot` enables pre-caching metadata directly from the Cornell Kaggle snapshot as explicitly permitted by `request/crawling.md` § Kaggle Dataset for Pre-Aggregated Metadata.
- **Verification:** Verified in `tests/test_arxiv_client.py` and `tests/test_kaggle_discovery.py`.

### 2.2 Figure Removal by Extraction & TeX Command Preservation (REQ-DATA-01, REQ-DATA-02)
- **Finding:** In `src.arxiv_source.extract_source_files`, extraction inspects each tar member's extension: only extensions matching `{".tex", ".bib"}` are extracted to local disk.
- **Verification:** The raw contents of `main.tex` containing `\begin{figure}` and `\includegraphics{...}` are never rewritten or sanitized. Image binaries (`.png`, `.jpg`, `.pdf`, `.eps`) are never saved into the corpus. Verified in `tests/test_arxiv_source.py` and `tests/test_colab_release_qualification.py`.

### 2.3 Semantic Scholar Production Path Activation (REQ-S2-01)
- **Finding:** In `notebooks/m1_colab_fast_production.ipynb`, Step 4 (Cell 6) explicitly invokes `runner.enrich_cohort_s2(cohort_num, s2_config=s2_cfg)` for each cohort in `COHORT_LIST`.
- **Verification:** Step 4 runs after source extraction (Cell 5) and before reporting (Cell 7), ensuring that S2 metadata and citations are populated and saved into `metadata.json` and `references.json` prior to the final audit. Verified in `tests/test_fast_production.py::test_enrich_cohort_s2_isolated_from_arxiv`.

### 2.4 Tabular Schema Compliance (REQ-DATA-04, REQ-DATA-05)
- **Finding:** `src.export.export_papers_table` and `src.export.export_references_table` export exact columns matching the schema table in `request/crawling.md` lines 237–238.
- **Verification:** Schema validation is enforced in `tests/test_export.py` and `validate_exported_tables()`.

**Traceability Audit Complete.** Ready for Phase 3 Gap Analysis.
