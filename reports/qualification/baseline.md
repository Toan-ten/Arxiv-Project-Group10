# M1 Colab Production Notebook — Qualification Baseline

**Thời điểm thiết lập:** 2026-11-10T00:01:40+07:00  
**Giai đoạn:** Phase 0 — Baseline Provenance & Qualification Identity  
**Người thực hiện:** Independent Qualification Agent (Antigravity)  

---

## 1. Thông Tin Kho Lưu Trữ (Repository Identity)

- **Repository URL:** `https://github.com/Toan-ten/Arxiv-Project-Group10`
- **Nhánh kiểm định:** `feat/m1-t02-colab-resume`
- **Git HEAD SHA (xác thực trực tiếp từ git):** `2d633e2e34cf8b96959d9ab9fd8147f53995d58a`
- **Working Tree Status:** Clean (tất cả các tệp đều được kiểm soát bởi Git)
- **Production Notebook Path:** `notebooks/m1_colab_fast_production.ipynb`
- **Notebook SHA-256:** `57ecd0a5addf3d4beb263065152f96a478c99cb5617cbac25e07e4bbbf6195b1` (sau khi khắc phục F-STATIC-01 và F-STATIC-02; ban đầu: `ab557b31f2bd89c35e0e89a04f9cd9bb05f9206f8040647bef8b6ebb485c12ce`)

---

## 2. Nguồn Gốc Mã Nguồn & Ghim Commit (Commit Provenance)

- **`CODE_COMMIT` (Mốc lịch sử đối chiếu):** `489a82fd959ccc6cced7c59fc309255e4421bef2`  
  - *Ý nghĩa:* Mốc lịch sử chuẩn mực được bảo lưu trong toàn bộ các tài liệu nghiệm thu M1.
- **`EXECUTION_COMMIT` (Mốc thực thi mã nguồn runtime):** `69b4b5e758f74d255ff244c56271e294da57b8e4`  
  - *Ý nghĩa:* Điểm cam kết chứa toàn bộ engine `fast_production.py`, runner tăng tốc SSD/RAM-disk, bảo vệ đối soát độc lập, và cơ chế tagging `archive_state` đã được kiểm thử pass 100%.
- **Cơ chế kiểm soát Fail-closed:**
  - Ô lệnh Bước 1 của sổ tay kiểm tra định dạng hex 40 ký tự cho cả hai mốc commit.
  - Sổ tay thực hiện `git fetch origin` và `git checkout --detach EXECUTION_COMMIT`.
  - Kiểm tra `git rev-parse HEAD == EXECUTION_COMMIT`; nếu không khớp, runtime lập tức ném `RuntimeError` và dừng thực thi.

---

## 3. Bảng Kiểm Kê & Mã Băm Tệp Cấu Hình (Manifest Hashes)

| Tệp tin | Vai trò | SHA-256 |
| :--- | :--- | :--- |
| `notebooks/m1_colab_fast_production.ipynb` | Sổ tay sản xuất chính thức M1 | `57ecd0a5addf3d4beb263065152f96a478c99cb5617cbac25e07e4bbbf6195b1` |
| `config/group10_id_ranges.json` | Cấu hình phân bổ 20.000 IDs / 200 lô cho 4 thành viên | `59a4283d6d6903be3b6d0a60faab86a38d460dd3e60fc16cc0b8f67beb1413bd` |
| `config/pilots/pilot_10.json` | Tập mẫu 10 ID kiểm thử nhanh (smoke) | `7b7a13238f01920c572d9ec119b7cc75038dba7565a7b428ea5ab51dd193cf15` |
| `config/pilots/pilot_500.json` | Tập mẫu 500 ID benchmark tải vừa | `2c7d4d6827686936d7ea98b685e311e7aaeff49f244bc0988e4e73affb94dc56` |
| `requirements.txt` | Phụ thuộc runtime cốt lõi | `bddf6c10efdabc1b9d3a530144fc358da260d8cc0c5690373d3f39ddea74a935` |
| `requirements-dev.txt` | Phụ thuộc phát triển và test runner CI | `9da2aed0cdeff2247ecf3450133df4427643b85f9232705a0f01a271ad44464d` |
| `requirements-colab.txt` | Khóa phiên bản psutil trên Colab | `71fb019d83e1b348b1a6b4923f2f0935dedaf771b3b637e08ccbdf04c09ccfd2` |

---

## 4. Môi Trường Kiểm Định & Kết Quả Baseline

- **Môi trường kiểm định cục bộ:** Linux (Ubuntu 24.04 kernel 6.8), GCC 15.2.0, Python 3.14.4
- **Môi trường kiểm định GitHub Actions CI:** Ubuntu 24.04 LTS runner, Python 3.11 & Python 3.12 (Matrix)
- **Lệnh kiểm thử chuẩn:** `pytest`
- **Kết quả baseline suite:** `1030 passed, 960 subtests passed` (23.31s, 0 failures, 0 errors, 100% offline).
