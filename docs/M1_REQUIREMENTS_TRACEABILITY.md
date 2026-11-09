# Milestone 1 Full Requirements Traceability Baseline

**Document Version:** 1.0.0 (Frozen Baseline)  
**Date:** 2026-11-10  
**Authoritative Source Documents:**
1. `request/crawling.md` — Project Milestone 1 — Data Crawling
2. `request/index.md` — arXiv Project — Problem Statements (Logistics & Regulations)

---

## 1. Traceability Principles

This baseline serves as the **Single Source of Truth** for Milestone 1 compliance. Requirements are classified into:
- **Explicit Assignment Requirements**: Wording drawn directly from `request/crawling.md` or `request/index.md`.
- **Derived Engineering Requirements**: Architecture, fail-closed security, and Colab resilience needed to satisfy the assignment reliably.
- **Ambiguity Status**: `UNAMBIGUOUS`, `AMBIGUOUS`, or `CONFLICTING`.

---

## 2. Master Requirements Matrix

| Requirement ID | Domain | Short Description | Mandatory? | Ambiguity Status | Source Document & Section |
|:---|:---:|:---|:---:|:---:|:---|
| **REQ-ARXIV-01** | arXiv | Crawl all available versions (v1..vN) for every assigned ID | **Yes** | UNAMBIGUOUS | `request/crawling.md` § Requirement |
| **REQ-ARXIV-02** | arXiv | Retrieve source via versioned `/src/{id}v{N}` endpoint from `export.arxiv.org` | **Yes** | UNAMBIGUOUS | `request/crawling.md` § Full-Text Source Download |
| **REQ-ARXIV-03** | arXiv | Point automated downloads strictly at `export.arxiv.org`, never main `arxiv.org` | **Yes** | UNAMBIGUOUS | `request/crawling.md` § Full-Text Source Download |
| **REQ-ARXIV-04** | arXiv | Polite pacing: wait $\ge 3$s between calls, single connection, descriptive User-Agent | **Yes** | UNAMBIGUOUS | `request/crawling.md` § Entry Discovery & Download |
| **REQ-ARXIV-05** | arXiv | Modern `arxiv.py` 4.x compatibility (`client.results(search)` style) | **Yes** | UNAMBIGUOUS | `request/crawling.md` § Entry Discovery & Download |
| **REQ-DATA-01** | Data | Remove figures by extraction only: extract `.tex` and `.bib`, exclude image binaries | **Yes** | UNAMBIGUOUS | `request/crawling.md` § Requirement |
| **REQ-DATA-02** | Data | Preserve LaTeX commands: figure environments and `\includegraphics` strictly untouched | **Yes** | UNAMBIGUOUS | `request/crawling.md` § Requirement |
| **REQ-DATA-03** | Data | Explicit empty record for PDF-only / non-convertible TeX versions (never drop) | **Yes** | UNAMBIGUOUS | `request/crawling.md` § Full-Text Source Download |
| **REQ-DATA-04** | Data | Schema `papers` table: `arxiv_id`, `title`, `authors`, `submission_date`, `revised_dates`, `venue` | **Yes** | UNAMBIGUOUS | `request/crawling.md` § Requirement |
| **REQ-DATA-05** | Data | Schema `references` table: `arxiv_id`, `ref_arxiv_id`, `ref_title`, `ref_authors`, `ref_submission_date`, `ref_s2_id` | **Yes** | UNAMBIGUOUS | `request/crawling.md` § Requirement |
| **REQ-S2-01** | S2 | Crawl reference metadata for every publication via Semantic Scholar API | **Yes** | UNAMBIGUOUS | `request/crawling.md` § Extracting References |
| **REQ-S2-02** | S2 | Request flat fields: `title`, `authors`, `year`, `publicationDate`, `venue`, `externalIds` | **Yes** | UNAMBIGUOUS | `request/crawling.md` § Extracting References |
| **REQ-S2-03** | S2 | Store all references locally; filter to arXiv references for final published table | **Yes** | UNAMBIGUOUS | `request/crawling.md` § Requirement |
| **REQ-S2-04** | S2 | Retain pre-2007 arXiv identifiers (`archive/YYMMNNN` format, e.g. `hep-th/9901001`) | **Yes** | UNAMBIGUOUS | `request/crawling.md` § Extracting arXiv Identifiers |
| **REQ-S2-05** | S2 | Adhere to S2 rate limits (1 req/s, 100 req/5min unauthenticated; API key allowed) | **Yes** | UNAMBIGUOUS | `request/crawling.md` § Rate Limits and Usage |
| **REQ-STORAGE-01** | Storage | No intermediate data submission; raw working corpus stays with group | **Yes** | UNAMBIGUOUS | `request/crawling.md` § Requirement |
| **REQ-STORAGE-02** | Storage | Working folder organization: one folder per publication (`2310-12345`) | Recommended | UNAMBIGUOUS | `request/crawling.md` § Suggested data organization |
| **REQ-REPOSITORY-01**| Repo | Single public GitHub repository with cumulative progress commits | **Yes** | UNAMBIGUOUS | `request/crawling.md` § Submission Format |
| **REQ-REPOSITORY-02**| Repo | Exclude data files, raw binaries, compiled files; source code only (`.py`, `.ipynb`) | **Yes** | UNAMBIGUOUS | `request/crawling.md` § Source Code |
| **REQ-REPOSITORY-03**| Repo | Clean repo structure: `src/`, `requirements.txt`, `README.md` | **Yes** | UNAMBIGUOUS | `request/crawling.md` § Source Code |
| **REQ-REPORT-01** | Report | Jupyter Book maintained on a separate public repository hosted on GitHub Pages | **Yes** | UNAMBIGUOUS | `request/crawling.md` § Report Requirements |
| **REQ-REPORT-02** | Report | Tables and figures generated dynamically during book compilation (no static screenshots) | **Yes** | UNAMBIGUOUS | `request/crawling.md` § Report Requirements |
| **REQ-REPORT-03** | Report | Required statistics: success count, success rate, paper sizes before/after figure removal, ref count, ref success rate | **Yes** | UNAMBIGUOUS | `request/crawling.md` § Report Requirements |
| **REQ-BENCHMARK-01**| Benchmark| Running time: wall time per paper, average time per required paper, discovery time | **Yes** | UNAMBIGUOUS | `request/crawling.md` § Report Requirements |
| **REQ-BENCHMARK-02**| Benchmark| Memory footprint: max RAM, average RAM, max disk required, final storage size | **Yes** | UNAMBIGUOUS | `request/crawling.md` § Report Requirements |
| **REQ-COLAB-01** | Colab | Benchmarking testbed: Google Colab CPU-only mode (single-worker, no GPU) | **Yes** | UNAMBIGUOUS | `request/crawling.md` § Report Requirements |
| **REQ-INTEGRATION-01**| Integration| Corpus must serve as working foundation for Milestone 2 parsing & M3 matching | **Yes** | UNAMBIGUOUS | `request/crawling.md` § Objectives |

---

## 3. Detailed Specification for Each Requirement

### REQ-ARXIV-01 — All Versions Coverage
- **Exact Text:** "For each assigned paper, all available versions must be crawled, including the initial submission and any subsequent revisions... for papers that were submitted once you should suffice it with `v1`."
- **Expected Behavior:** If an assigned paper has 3 revisions (`v1`, `v2`, `v3`), the crawler must download and store source archives for `v1`, `v2`, and `v3`.

### REQ-ARXIV-02 & REQ-ARXIV-03 — Source Endpoints
- **Exact Text:** "Source archives are served by the dedicated mirror export.arxiv.org: the preferred endpoint `https://export.arxiv.org/src/{arxiv_id}` consistently returns a gzipped tar archive, and it also accepts versioned identifiers... Point automated downloads at this mirror and never at the main site arxiv.org."
- **Expected Behavior:** HTTP GET requests target `https://export.arxiv.org/src/{versioned_id}`.

### REQ-ARXIV-04 & REQ-ARXIV-05 — Polite Pacing & arxiv.py 4.x
- **Exact Text:** "wait at least 3 seconds between successive calls and use one connection at a time... always use the modern `client.results(search)` style... arxiv.py 4.x no longer ships a download method, so fetch the archive yourself."
- **Expected Behavior:** Crawler must enforce $\ge 3.0$s interval between successive calls to `export.arxiv.org` and use single-connection concurrency.

### REQ-DATA-01 & REQ-DATA-02 — Figure Binary Removal by Extraction & Macro Preservation
- **Exact Text:** "extract only the `.tex` and `.bib` files from each source archive so that figure binaries and other non-source files never enter the corpus, and keep the LaTeX commands that define figures, such as `figure` environments and `\includegraphics` lines, untouched inside the TeX files."
- **Expected Behavior:** Figures (`.png`, `.jpg`, `.pdf`, `.eps`) are excluded during extraction. Raw `.tex` content bytes are never edited, sanitized, or rewritten.

### REQ-DATA-03 — PDF-Only & Non-TeX Versions
- **Exact Text:** "Other forms such as PDF files or `.docx` Word documents may not be convertible into TeX files, and for such versions you should keep an empty record in the corpus rather than dropping the publication."
- **Expected Behavior:** If a version is PDF-only, create an explicit record with `status: "no_tex_source"` without synthetic `.tex` files.

### REQ-DATA-04 & REQ-DATA-05 — Tabular Parquet Schema
- **Papers Table Columns:** `arxiv_id` (string), `title` (string), `authors` (list of strings), `submission_date` (ISO string), `revised_dates` (list of strings), `venue` (string, nullable).
- **References Table Columns:** `arxiv_id` (string), `ref_arxiv_id` (string), `ref_title` (string), `ref_authors` (list of strings), `ref_submission_date` (string), `ref_s2_id` (string).

### REQ-S2-01 to REQ-S2-05 — Semantic Scholar Citation Graph
- **Exact Text:** "The reference metadata must additionally be crawled for each publication via the Semantic Scholar API... specify fields such as `references.title, references.authors, references.year, references.publicationDate, references.venue, references.externalIds`... Keep *all* crawled references locally."
- **Expected Behavior:** Every paper has an accompanying `references.json` containing all references returned by Semantic Scholar. References lacking an arXiv ID are retained locally in the corpus.

### REQ-REPORT-01 to REQ-REPORT-03 — Jupyter Book Requirements
- **Exact Text:** "The milestone report is a Jupyter Book maintained in a public repository of its own, separate from the shared code repository, and hosted on that repository's GitHub Pages... figures and tables must be generated during book compilation, not pasted as static screenshots."
- **Expected Behavior:** A dedicated repository exists or is built containing the Jupyter Book with Python code cells generating figures/tables during compilation.

### REQ-BENCHMARK-01 & REQ-BENCHMARK-02 — Benchmark Auditing
- **Exact Text:** "Running time counts the total time to process one paper, the average time to process each of the required papers, and the total time for entry discovery, while memory footprint counts the maximum RAM used during the scraping, the average RAM consumption, the maximum disk storage required, and the final output's storage size. Measure both points in an auditable way."
- **Expected Behavior:** Benchmarking module collects process-tree RSS memory via `psutil`, calculates wall-clock timings, and computes pre- and post-extraction disk footprints.

---

## 4. Ambiguity Resolution & Edge Cases

| Ambiguity / Question | Analysis | Resolved Project Policy |
|:---|:---|:---|
| **Use of `arxiv.py` vs custom HTTP client** | Assignment states: "The library `arxiv.py` allows flexible interaction with this API... always use the modern `client.results(search)` style... arxiv.py 4.x no longer ships a download method, so fetch the archive yourself". | `src/arxiv_client.py` and `src/arxiv_discovery.py` support official `arxiv.Client().results(...)` while maintaining polite 3.0s pacing and exponential backoff on HTTP 429. |
| **Kaggle snapshot vs live arXiv API** | Assignment § Entry Discovery presents Kaggle snapshot as an entry discovery method ("pre-collected metadata for well over two million papers. It can be used directly without any API calls"). | Pre-caching discovery from Kaggle snapshot is explicitly valid under assignment guidelines; source download must still target live `export.arxiv.org/src/{id}v{N}`. |
| **Jupyter Book location** | Assignment states: "maintained in a public repository of its own, separate from the shared code repository". | This repository contains the crawling code, storage, export scripts, and report-generation logic that supplies data for the Jupyter Book. |

**Gate G01 Passed:** Authoritative requirements frozen.
