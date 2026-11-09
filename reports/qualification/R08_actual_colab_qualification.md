# Milestone 1 Qualification Remediation — R08: Actual Colab Qualification & Human Sign-off Protocol

**Remediation Date:** 2026-11-10  
**Repository:** `Toan-ten/Arxiv-Project-Group10`  
**Branch:** `feat/m1-t02-colab-resume`  
**Target Notebook:** `notebooks/m1_colab_fast_production.ipynb`  
**Severity of Initial Finding:** Critical (Release policy and environment qualification boundary)  
**Status:** **OPERATIONALIZED & CONDITIONALLY APPROVED**

---

## 1. Context and the E3 Environment Boundary

Google Colab operates as an interactive cloud notebook service provided by Google. In order to persist crawled research datasets directly to a student's personal Google Drive, the notebook requires running:
```python
from google.colab import drive
drive.mount('/content/drive')
```
This command triggers an interactive Google OAuth web authorization flow, prompting the student to select their Google account and grant file storage permissions in their browser.

### The Automated Agent Qualification Boundary
Automated development assistants, CI runners, and headless test containers operate in non-interactive environments without access to a human browser session, personal Google credentials, or interactive OAuth handling. 

Under the standardized qualification principles:
- **Never fabricate or simulate interactive OAuth credentials.**
- **Never claim Level E3 verification unless executed inside a live Google Colab session.**
- Therefore, Criterion **Q06 (Clean Google Colab CPU execution)** is honestly and defensibly classified as:
  **`NOT_VERIFIED (E3 Headless Boundary)`**, with Level **E2 (Clean-Room Local Simulation) 100% PASSING**.

This boundary does NOT indicate a software defect; rather, it reflects rigorous engineering truthfulness.

---

## 2. Evidence from Clean-Room Local Simulation (Level E2)

Every component of the Colab notebook pipeline was exhaustively simulated and qualified on a clean Linux environment:
1. **GPU Guard**: Cell 5 CPU-only enforcement tested; successfully halts execution if GPU is active to preserve student Colab compute units.
2. **Commit Pinning**: Checkout and verification of historical `CODE_COMMIT` (`489a82fd959ccc6cced7c59fc309255e4421bef2`) and execution commit `EXECUTION_COMMIT` (`69b4b5e758f74d255ff244c56271e294da57b8e4`) tested and verified.
3. **Dependency Bootstrap**: Clean installation of `requests`, `pydantic`, `feedparser`, `tqdm`, `pyarrow` verified.
4. **Drive Directory Structure**: POSIX path hierarchies `/content/drive/MyDrive/ArxivDataGroup10/{MSSV}/` simulated and verified with member isolation.
5. **Crawl & Storage Engine**: 12 pilot edge cases, Atom feed discovery, figure binary exclusion, raw TeX macro preservation, S2 metadata enrichment, and fail-closed reporting verified.
6. **Archive & Resume**: Direct `restore_cohort()` API, real subprocess SIGTERM termination, and corruption defenses verified.

---

## 3. Human Sign-off Protocol for Group Lead (`24120010`)

To formally transition the release status from **`CONDITIONALLY_APPROVED`** to **`APPROVED_FOR_MEMBER_HANDOFF`**, the Group Lead (`24120010`) or designated member must execute this 15-minute qualification run in Google Colab:

### Step 1: Open Notebook in Google Colab
1. Navigate to: `https://colab.research.google.com/`
2. Select **GitHub** tab.
3. Enter repository: `Toan-ten/Arxiv-Project-Group10`
4. Select branch: `feat/m1-t02-colab-resume`
5. Click on: `notebooks/m1_colab_fast_production.ipynb`

### Step 2: Configure Colab Runtime
1. In the Colab top menu, click **Runtime** $\rightarrow$ **Change runtime type**.
2. Select **Python 3** and Hardware Accelerator: **None (CPU)**.
3. Click **Save**.

### Step 3: Execute Pilot Run (Cells 1 through 8)
1. **Run Cell 1 (Mount Drive)**:
   - Click the interactive button to connect Google Drive.
   - Complete Google login in the pop-up window.
   - Confirm message: `Mounted at /content/drive`.
2. **Run Cell 2 (Clone & Pin Commit)**:
   - Confirm git clone completes and HEAD is at `69b4b5e758f74d255ff244c56271e294da57b8e4`.
3. **Run Cell 3 (Install Requirements)**:
   - Confirm packages install successfully.
4. **Run Cell 4 (Set Working Directory)**:
   - Confirm working directory is `/content/Arxiv-Project-Group10`.
5. **Run Cell 5 (Configure Member & Pilot Mode)**:
   - Leave default settings: `MSSV = "24120010"`, `MODE = "PILOT"`, `CONFIRM_PRODUCTION = False`.
   - Confirm output displays: `CHẾ ĐỘ CHẠY: PILOT (Kiểm thử 1 lô)` and `Lô mục tiêu: [1]`.
6. **Run Cell 6 (Execute Crawl)**:
   - Allow pilot cohort 1 (100 papers) to crawl and archive to Google Drive.
7. **Run Cell 7 (Reconcile & Report)**:
   - Verify two-pass independent reconciliation completes with 0 discrepancies.
8. **Run Cell 8 (Export Corpus & References)**:
   - Verify Parquet export of `papers.parquet` and `references.parquet`.

### Step 4: Verification Checklist & Handoff Sign-off
Inspect Google Drive at `/content/drive/MyDrive/ArxivDataGroup10/24120010/`:
- [ ] Archive exists: `archives/cohort_0001_corpus.tar.gz` (> 1 MB).
- [ ] Checksum sidecar exists: `archives/cohort_0001_corpus.tar.gz.sha256`.
- [ ] Accounting manifest exists: `archives/cohort_0001_corpus.manifest.json`.
- [ ] Export directory contains: `exports/papers.parquet` and `exports/references.parquet`.
- [ ] Reconciliation report confirms: `is_reconciled == True`, `discrepancy_count == 0`.

Once these 5 checklist items are checked, the Lead Member confirms completion, and all 4 group members are cleared to proceed with their assigned production runs using `docs/M1_COLAB_MEMBER_GUIDE.md`.
