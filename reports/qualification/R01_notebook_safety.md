# Milestone 1 Qualification Remediation — R01: Production Notebook Default-Mode Safety

**Remediation Date:** 2026-11-10  
**Repository:** `Toan-ten/Arxiv-Project-Group10`  
**Branch:** `feat/m1-t02-colab-resume`  
**Target Notebook:** `notebooks/m1_colab_fast_production.ipynb`  
**Severity of Initial Finding:** Critical (Release-Blocking)  
**Status:** **RESOLVED & VERIFIED**

---

## 1. Problem Statement & Risk Analysis

In the previous notebook state, Cell 5 (Step 3) was configured with:
```python
MSSV = "24120260"
MODE = "MEMBER_PRODUCTION"
```
**Risk:** When this notebook was shared with the 4 team members, any member clicking "Run All" or running Cell 5 without scrutinizing the configuration would immediately launch a full 50-cohort (5,000 papers) crawl assigned to member `24120260`. This had severe operational consequences:
1. Accidental exhaustion of arXiv and Semantic Scholar rate limits.
2. Unintended execution of another member's partition.
3. Waste of student Colab compute credits before verifying pilot execution.
4. Risk of partial or corrupted state if interrupted mid-run without monitoring.

---

## 2. Remediated Architecture & Safety Barriers

### 2.1 Safe Default Execution Mode (`MODE = "PILOT"`)
The default execution mode is now hardcoded to `MODE = "PILOT"`.
- By default, executing Cell 5 only targets **1 cohort** (the first assigned cohort, 100 papers).
- It never touches the full 50 cohorts unless explicitly and deliberately reconfigured.

### 2.2 Explicit Affirmative Confirmation Barrier (`CONFIRM_PRODUCTION`)
To transition from `PILOT` to `MEMBER_PRODUCTION`, the student must satisfy an explicit fail-closed confirmation check:
```python
# CẤU HÌNH THÀNH VIÊN VÀ CHẾ ĐỘ CHẠY (SAFE BY DEFAULT):
MSSV = "24120010"  # Chọn mã sinh viên được phân bổ: "24120010", "24120035", "24120232", hoặc "24120260"
MODE = "PILOT"  # Mặc định an toàn: "PILOT" (chạy thử 1 lô). Chỉ đổi sang "MEMBER_PRODUCTION" khi thực sự chạy 50 lô.
CONFIRM_PRODUCTION = False  # BẮT BUỘC: Khi MODE == "MEMBER_PRODUCTION", phải đặt True để xác nhận chạy toàn bộ 50 lô (5.000 IDs).
```
When `MODE == "MEMBER_PRODUCTION"` is set, the notebook executes the following verification block:
```python
elif MODE == "MEMBER_PRODUCTION":
    print("=================================================================")
    print("  CẢNH BÁO: BẠN ĐANG KÍCH HOẠT CHẾ ĐỘ THU THẬP PRODUCTION!")
    print(f"  - Thành viên (MSSV): {MSSV}")
    print(f"  - Dải lô phân bổ: Lô {assigned_cohorts[0]} .. Lô {assigned_cohorts[-1]}")
    print(f"  - Tổng số lô: {len(assigned_cohorts)} lô")
    print(f"  - Tổng số định danh dự kiến: {len(assigned_cohorts) * 100} IDs")
    print("=================================================================")
    if globals().get("CONFIRM_PRODUCTION") is not True:
        raise RuntimeError(
            f"BẢO VỆ CHẶN TỰ ĐỘNG (FAIL-CLOSED): Chế độ 'MEMBER_PRODUCTION' yêu cầu xác nhận rõ ràng trước khi chạy. "
            f"Vui lòng đặt CONFIRM_PRODUCTION = True trong ô cấu hình này để xác nhận bạn muốn thu thập toàn bộ "
            f"{len(assigned_cohorts)} lô ({assigned_cohorts[0]}..{assigned_cohorts[-1]}) cho MSSV {MSSV}."
        )
    COHORT_LIST = assigned_cohorts
```

### 2.3 Fail-Closed Properties
1. **Interactive Colab Run**: If a user switches `MODE = "MEMBER_PRODUCTION"` but forgets to toggle `CONFIRM_PRODUCTION = True`, execution halts immediately with a descriptive `RuntimeError`.
2. **Headless / Automated Execution**: If executed in non-interactive batch runners without human confirmation, the script halts safely before any network requests or disk writes occur.
3. **Invalid Confirmation**: Setting `CONFIRM_PRODUCTION` to empty string `""`, `0`, `None`, or `False` is rejected.

### 2.4 Strict Output Root Isolation
To prevent any accidental data cross-contamination between members:
- `LOCAL_ROOT = f'/content/workspace_{MSSV}'`
- `DRIVE_ROOT = f'/content/drive/MyDrive/ArxivDataGroup10/{MSSV}'`
Each member's archives, logs, reports, and parquet tables are placed in their own isolated directory structure on Google Drive.

---

## 3. Regression Test Coverage

The following automated tests in `tests/test_colab_release_qualification.py` verify the R01 safety contract:
- `test_default_notebook_configuration_selects_pilot`: Confirms `MODE = "PILOT"` and `CONFIRM_PRODUCTION = False` in raw notebook JSON.
- `test_pilot_mode_cannot_process_all_50_cohorts`: Confirms PILOT mode isolates execution to exactly 1 cohort.
- `test_production_mode_requires_explicit_confirmation`: Confirms missing confirmation halts execution with `RuntimeError`.
- `test_invalid_confirmation_stops_execution`: Tests falsy and invalid confirmation values.
- `test_valid_confirmation_permits_intended_execution_path`: Confirms `CONFIRM_PRODUCTION = True` permits all 50 cohorts across all 4 valid MSSVs.
- `test_output_roots_isolate_members`: Confirms unique, non-colliding Drive output roots for all members.

**Conclusion:** R01 remediation is complete, safe by default, and verified.
