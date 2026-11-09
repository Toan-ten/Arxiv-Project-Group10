# Milestone 1 Google Colab Operational Runbook for Group 10 Members

**Document Version:** 2.0.0  
**Effective Date:** 2026-11-10  
**Target Repository:** `Toan-ten/Arxiv-Project-Group10`  
**Target Branch:** `feat/m1-t02-colab-resume`  
**Notebook Path:** `notebooks/m1_colab_fast_production.ipynb`  
**Historical CODE_COMMIT Pin:** `489a82fd959ccc6cced7c59fc309255e4421bef2`  
**Execution Runtime Pin:** `69b4b5e758f74d255ff244c56271e294da57b8e4`  

---

## 1. Member Ownership and Cohort Ranges

Each member is allocated exactly 50 cohorts (5,000 assigned arXiv IDs). You are strictly responsible for crawling, storing, and archiving your assigned cohort partition.

| Member Name / Role | MSSV | Assigned Cohorts (1-indexed) | Assigned arXiv ID Range | Total Assigned | Drive Backup Path |
|:---|:---:|:---:|:---:|:---:|:---|
| **Member 1 (Lead)** | `24120010` | `1 .. 50` | `2508.07872` → `2508.12871` | 5,000 | `.../ArxivDataGroup10/24120010/` |
| **Member 2** | `24120035` | `51 .. 100` | `2507.16659` → `2507.21658` | 5,000 | `.../ArxivDataGroup10/24120035/` |
| **Member 3** | `24120232` | `101 .. 150` | `2502.08235` → `2502.13234` | 5,000 | `.../ArxivDataGroup10/24120232/` |
| **Member 4** | `24120260` | `151 .. 200` | `2501.02642` → `2501.07641` | 5,000 | `.../ArxivDataGroup10/24120260/` |

> [!IMPORTANT]
> **Strict Partition Isolation**: Do NOT change your MSSV or run cohorts outside your assigned range. Running another member's cohorts risks corrupting their checkpoints or triggering arXiv rate-limits.

---

## 2. Pre-Flight Preparation & Colab Setup

### 2.1 Google Colab Runtime Selection
1. Open Google Colab: https://colab.research.google.com
2. Go to **Runtime** → **Change runtime type**.
3. Select **CPU** (Standard Python 3 runtime).
4. **DO NOT SELECT GPU OR TPU**. The crawler is I/O-bound (network and tarball extraction). Using GPU wastes compute credits and activates GPU-guard warnings.

### 2.2 Google Drive Space Check
- Ensure your Google Drive has at least **10 GB of free space** for your 50 cohort archives.
- The notebook will automatically create the directory:
  `/content/drive/MyDrive/ArxivDataGroup10/<your_mssv>/archives/`

---

## 3. Cell-by-Cell Execution Instructions

Open `notebooks/m1_colab_fast_production.ipynb` in Colab and execute each cell sequentially.

### Cell 1: Mount Google Drive
Connects your Google Drive storage so checkpoints and archives survive Colab disconnections.
- Click **Run**.
- When prompted, grant Google Colab access to Google Drive.

### Cell 2: Clone Repository & Checkout Branch
Clones the canonical repository and checks out the production branch:
```bash
!git clone https://github.com/Toan-ten/Arxiv-Project-Group10.git /content/Arxiv-Project-Group10
%cd /content/Arxiv-Project-Group10
!git checkout feat/m1-t02-colab-resume
```

### Cell 3: Install Pinned Dependencies
Installs required packages matching the project lockfile:
```bash
!pip install -r requirements.txt
```

### Cell 4: Member Configuration
Customize this cell for **your MSSV** before executing!

Example for Member `24120010`:
```python
MSSV = "24120010"
COHORT_START = 1
COHORT_END = 50
DRIVE_ROOT = f"/content/drive/MyDrive/ArxivDataGroup10/{MSSV}"
```

Example for Member `24120035`:
```python
MSSV = "24120035"
COHORT_START = 51
COHORT_END = 100
DRIVE_ROOT = f"/content/drive/MyDrive/ArxivDataGroup10/{MSSV}"
```

Example for Member `24120232`:
```python
MSSV = "24120232"
COHORT_START = 101
COHORT_END = 150
DRIVE_ROOT = f"/content/drive/MyDrive/ArxivDataGroup10/{MSSV}"
```

Example for Member `24120260`:
```python
MSSV = "24120260"
COHORT_START = 151
COHORT_END = 200
DRIVE_ROOT = f"/content/drive/MyDrive/ArxivDataGroup10/{MSSV}"
```

### Cell 5: Sanity Checks & Directory Creation
Verifies that:
- You are running on CPU (not GPU).
- Your MSSV and cohort ranges match `id_ranges.py`.
- Drive paths and local scratch directories exist.

### Cell 6: Run Fast Production Crawler
Starts the crawling process:
- Automatically checks Google Drive for existing cohort archives (`restore_cohort()`).
- Crawls assigned papers with polite arXiv delays (3.0s between requests).
- Extracts LaTeX source files and bibtex bibliographies (excluding heavy images).
- Enriches metadata and citations with Semantic Scholar.
- Packs completed cohorts into `cohort_{id:04d}_corpus.tar.gz` directly on Google Drive.

> [!TIP]
> **Batching Recommendation**: You do not have to crawl all 50 cohorts in one go. You can set `COHORT_START = 1` and `COHORT_END = 10` for your first session, then `11 .. 25`, etc. Checkpoints are automatically preserved.

### Cell 7: Full Integrity Audit & Reconciliation Report
Runs the two-pass fail-closed reporting verification across your entire corpus.
- Generates `m1_report_{MSSV}.md` and `m1_report_{MSSV}.json`.
- Confirms zero integrity failures and calculates your M1 completion metrics.

### Cell 8: Download Deliverables / Verification Summary
Displays archive counts, total file sizes on Google Drive, and exports summary metrics.

---

## 4. Disconnection & Resume Guide

Colab sessions may timeout or disconnect due to inactivity or network interruptions. **Your data is safe.**

To resume:
1. Reopen Google Colab and select CPU runtime.
2. Run **Cell 1** (Mount Drive).
3. Run **Cell 2** (Clone repo).
4. Run **Cell 3** (Install dependencies).
5. Run **Cell 4** with your MSSV.
6. Run **Cell 6** (Run Crawler).
7. The runner detects your existing archives in Google Drive, restores your progress, and continues crawling only the remaining incomplete papers.

---

## 5. Member Deliverables & Handoff Checklist

Before reporting your M1 task complete, ensure the following are verified in Google Drive:

- [ ] **50 Cohort Archives**: Exactly 50 files named `cohort_{id:04d}_corpus.tar.gz` in your Drive directory (`/MyDrive/ArxivDataGroup10/<MSSV>/archives/`).
- [ ] **Zero Integrity Failures**: Cell 7 reporting output shows `has_integrity_failure: false` and `discrepancies: []`.
- [ ] **Checksum File**: File `checksums.sha256` generated and present alongside archives.
- [ ] **Audit Report**: `m1_report_{MSSV}.md` saved in Google Drive and committed to your member handoff branch.

---

## 6. Escalation & Troubleshooting Protocol

| Issue / Error Symptom | Cause | Remedy |
|:---|:---|:---|
| `HTTP 429 Too Many Requests` | arXiv or Semantic Scholar rate-limiting | The runner automatically applies exponential backoff. Do not stop the cell; let it wait. |
| `Colab Session Disconnected` | 12-hour timeout or idle browser tab | Re-mount Drive and run Cells 1–6. Checkpoint resume will pick up seamlessly. |
| `Drive Quota Exceeded` | Insufficient free space in Google Drive | Empty your Google Drive trash or free up at least 10 GB. |
| `IntegrityError on Resume` | Partial paper file corrupted during abrupt disconnect | Delete the single corrupt paper directory in local workspace; re-run Cell 6 to re-download that paper cleanly. |
| `GPU Guard Warning` | Accidental selection of GPU runtime | Change runtime type to CPU in Colab menu and restart runtime. |
