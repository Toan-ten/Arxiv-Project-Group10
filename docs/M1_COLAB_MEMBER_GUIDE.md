# Milestone 1 Google Colab Operational Runbook for Group 10 Members

**Document Version:** 2.1.0 (Remediated)  
**Effective Date:** 2026-11-10  
**Target Repository:** `Toan-ten/Arxiv-Project-Group10`  
**Target Branch:** `feat/m1-t02-colab-resume`  
**Notebook Path:** `notebooks/m1_colab_fast_production.ipynb`  
**Historical CODE_COMMIT Pin:** `489a82fd959ccc6cced7c59fc309255e4421bef2`  
**Execution Runtime Pin:** `69b4b5e758f74d255ff244c56271e294da57b8e4`  

---

## 1. Member Ownership and Cohort Ranges

Each member is allocated exactly 50 cohorts (5,000 assigned arXiv IDs). You are strictly responsible for crawling, storing, and archiving your assigned cohort partition.

| Member Name | MSSV | Assigned Cohorts (1-indexed) | Assigned arXiv ID Range | Total Assigned | Drive Backup Path |
|:---|:---:|:---:|:---:|:---:|:---|
| **Trần Văn Tấn Khôi (Lead)** | `24120010` | `1 .. 50` | `2508.07872` → `2508.12871` | 5,000 | `.../ArxivDataGroup10/24120010/` |
| **Lê Thành Đạt** | `24120035` | `51 .. 100` | `2507.16659` → `2507.21658` | 5,000 | `.../ArxivDataGroup10/24120035/` |
| **Nguyễn Văn Toàn** | `24120232` | `101 .. 150` | `2502.08235` → `2502.13234` | 5,000 | `.../ArxivDataGroup10/24120232/` |
| **Trương Tuấn Anh** | `24120260` | `151 .. 200` | `2501.02642` → `2501.07641` | 5,000 | `.../ArxivDataGroup10/24120260/` |

> [!IMPORTANT]
> **Strict Partition Isolation**: Do NOT change your MSSV or run cohorts outside your assigned range. Running another member's cohorts risks corrupting their checkpoints or triggering arXiv rate-limits.

---

## 2. Notebook Execution Contract (Cell-by-Cell Specification)

The official notebook [`notebooks/m1_colab_fast_production.ipynb`](file:///home/tuan-anh/Documents/DS_PJ/Arxiv-Project-Group10/notebooks/m1_colab_fast_production.ipynb) consists of 8 cells executed in strict sequence:

| Cell # | Type | Title / Step | Purpose | Required Inputs | Outputs & Side Effects | Mandatory | Safe to Rerun |
|:---:|:---:|:---|:---|:---|:---|:---:|:---:|
| **1** | Markdown | Colab Badge | Open notebook directly in Google Colab | None | None | No | Yes |
| **2** | Markdown | Operating Principles | Rules on CPU-only, single worker, Storage Contract v2 | None | None | No | Yes |
| **3** | Code | **Bước 1: Thiết lập môi trường & kiểm tra mã nguồn** | Connect Google Drive, clone repo, checkout `EXECUTION_COMMIT`, install packages | `CODE_COMMIT`, `EXECUTION_COMMIT` | Mounts `/content/drive`, clones into `/content/Arxiv-Project-Group10`, installs `requirements.txt` | **Yes** | Yes |
| **4** | Code | **Bước 2: Nạp cấu hình Kaggle API (Tùy chọn)** | Accelerate metadata discovery with Kaggle OAI snapshot | `KAGGLE_API_TOKEN` (via Colab Secrets) | `~/.kaggle/access_token`, `arxiv-metadata-oai-snapshot.json` | Optional | Yes |
| **5** | Code | **Bước 3: Thu thập mã nguồn TeX & lịch sử phiên bản** | Core crawler & archive engine; restores Drive backups, crawls assigned cohorts | `MSSV`, `MODE`, `CONFIRM_PRODUCTION` | Local corpus (`workspace_{MSSV}`), Drive archives (`ArxivDataGroup10/{MSSV}/archives/`) | **Yes** | Yes |
| **6** | Code | **Bước 4: Làm giàu trích dẫn Semantic Scholar (S2)** | Enrich citation graph and paper metadata | `SEMANTIC_SCHOLAR_API_KEY` (via Colab Secrets or unauthenticated) | Updated `metadata.json`, saved `references.json` per paper | **Yes** | Yes |
| **7** | Code | **Bước 5: Kiểm toán độc lập & tạo báo cáo M1** | Run 12-point two-pass fail-closed reconciliation | `COHORT_LIST`, `runner.store` | `m1_audit_summary_mssv_{MSSV}_{MODE}.md` on Google Drive | **Yes** | Yes |
| **8** | Code | **Bước 6: Xuất bảng dữ liệu Parquet & CSV** | Export tabular format for Milestone 2/3 downstream | `all_pairs`, `runner.store` | `papers.parquet`, `references.parquet` under `final_output/` | Optional | Yes |

---

## 3. Workflows

### Workflow A — Pilot Execution (Mandatory Pre-Flight)
Every member must run Workflow A first to verify their Google Drive connectivity, secrets, and environment on a single cohort (100 papers) before requesting production approval.

1. Open `notebooks/m1_colab_fast_production.ipynb` in Google Colab.
2. Select **Runtime** → **Change runtime type** → **CPU** (No GPU/TPU).
3. In Colab **Secrets** (key icon in left sidebar):
   - Add `SEMANTIC_SCHOLAR_API_KEY` (if available) and grant notebook access.
   - Add `KAGGLE_API_TOKEN` (optional, for metadata snapshot).
4. Run **Cell 3 (Bước 1)** to mount Google Drive and checkout the verified codebase.
5. Run **Cell 4 (Bước 2)** to prepare snapshot (or skip if not using Kaggle).
6. In **Cell 5 (Bước 3)**, configure:
   ```python
   MSSV = "24120010"  # Thay bằng MSSV của bạn
   MODE = "PILOT"     # MẶC ĐỊNH AN TOÀN
   CONFIRM_PRODUCTION = False
   ```
7. Run **Cell 5 (Bước 3)**: It will process only your 1st cohort (e.g., Cohort 1 for `24120010`, Cohort 51 for `24120035`).
8. Run **Cell 6 (Bước 4)**: Enriches the pilot cohort with Semantic Scholar citations.
9. Run **Cell 7 (Bước 5)**: Audits the pilot cohort and generates the markdown summary report.
10. Inspect the report on Google Drive (`.../reports/m1_audit_summary_mssv_{MSSV}_pilot.md`). Confirm `has_integrity_failure: false`.

---

### Workflow B — Member Production (Authorized Full 50-Cohort Crawl)

> [!CAUTION]
> **PRODUCTION GATING**: Workflow B must NOT be started until Milestone 1 Release Acceptance is officially signed off.

When authorized:
1. Re-open notebook on CPU runtime.
2. Run **Cell 3 (Bước 1)** and **Cell 4 (Bước 2)**.
3. In **Cell 5 (Bước 3)**, set your configuration:
   ```python
   MSSV = "24120010"  # Thay đúng MSSV của bạn
   MODE = "MEMBER_PRODUCTION"
   CONFIRM_PRODUCTION = True  # BẮT BUỘC: Xác nhận chạy toàn bộ 50 lô (5.000 bài)
   ```
4. Run **Cell 5 (Bước 3)**:
   - The cell displays the safety banner showing your MSSV, cohort range (e.g. 1..50), and 5,000 ID count.
   - It iterates through all 50 cohorts with automatic checkpoint restoration (`restore_cohort()`).
   - Every completed cohort is automatically packaged into `cohort_{id:04d}_corpus.tar.gz` and uploaded to Google Drive.
5. Run **Cell 6 (Bước 4)**: Enriches citations across all 50 cohorts.
6. Run **Cell 7 (Bước 5)**: Runs the full 12-point reconciliation across all 5,000 papers and generates the final M1 audit report.
7. Run **Cell 8 (Bước 6)**: Exports `papers.parquet` and `references.parquet` for Milestone 2.

---

## 4. Disconnection & Resume Guide

Colab sessions may timeout (idle disconnect or 12-hour limit). **Your progress is safe.**

To resume:
1. Reconnect to a new Colab CPU session.
2. Run **Cell 3 (Bước 1)** to mount Google Drive.
3. In **Cell 5 (Bước 3)**, ensure your `MSSV` and `CONFIRM_PRODUCTION = True` are set.
4. Run **Cell 5 (Bước 3)**:
   - The runner detects existing `cohort_{id:04d}_corpus.tar.gz` archives in Google Drive.
   - It calls `restore_cohort()` to unpack completed cohorts to local SSD in seconds.
   - It skips already-completed papers with status `complete` and resumes exactly where it stopped.

---

## 5. Member Deliverables & Handoff Checklist

Before reporting your M1 partition complete, verify the following in your Google Drive:

- [ ] **50 Cohort Archives**: Exactly 50 files named `cohort_{id:04d}_corpus.tar.gz` in `/MyDrive/ArxivDataGroup10/<MSSV>/archives/`.
- [ ] **Zero Integrity Failures**: Cell 7 reporting output shows `has_integrity_failure: false` and `discrepancies: []`.
- [ ] **Checksum Sidecars**: Each archive has a corresponding `.sha256` and `.manifest.json` sidecar.
- [ ] **Audit Report**: `m1_audit_summary_mssv_{MSSV}_member_production.md` present in Drive `/reports/`.
- [ ] **Parquet Tables**: `papers.parquet` and `references.parquet` generated in `/final_output/`.
