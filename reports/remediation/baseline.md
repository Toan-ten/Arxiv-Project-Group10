# Baseline Snapshot Report — Phase 0

**Thời điểm lập:** 2026-11-09T14:46:00+07:00  
**Repository:** `Toan-ten/Arxiv-Project-Group10`  
**Nhánh:** `feat/m1-t02-colab-resume`  
**Commit Baseline Audit:** `8ce364ff804f998c993ba54dfdb8cdf719fa8040`  
**Môi trường:** Linux, Python 3.14.4  

---

## 1. Thống Kê Hiện Trạng Repository Tại Baseline

| Hạng mục | Số lượng | Chi tiết |
|---|---:|---|
| Tổng số tệp Python trong `src/` | 21 | Bao gồm core crawler, storage, fast runner, export, s2 |
| Tổng số tệp kiểm thử trong `tests/` | 27 | Bao gồm unit, integration, fault-injection, live smoke |
| Tổng số test cases thực thi | 911 | Đạt 911/911 (100% pass) sau khi thêm safety tests |
| Tổng số notebook | 4 | `m1_colab_fast_production`, `m1_colab_arxiv_production`, `m1_colab_cpu_pilot`, `m1_colab_strategy_comparison` |
| Sổ tay production chính thức | 1 | `notebooks/m1_colab_fast_production.ipynb` |

---

## 2. Các Vấn Đề Nhận Diện Cần Khắc Phục (Theo Master Remediation Plan)

1. **P0-01:** Ô lệnh cuối cùng (Cell 8) trong `m1_colab_fast_production.ipynb` thực hiện `shutil.rmtree(drive_production)`, có nguy cơ xóa toàn bộ dữ liệu trên Google Drive khi chạy "Run All". *(Đã xử lý xóa bỏ trong Phase 0)*.
2. **P1-01:** Lệch tên biến môi trường API key: Notebook dùng `S2_API_KEY`, nhưng `local_config.py` đọc `SEMANTIC_SCHOLAR_API_KEY`.
3. **P1-02:** S2 metadata batching chưa được kết nối trực tiếp trong pipeline production (hiện pipeline gửi 1 ID/request).
4. **P1-03:** Cổng admission cho Semantic Scholar chưa khóa cứng điều kiện toàn bộ bài báo trong cohort phải đạt terminal source status.
5. **P1-04 — P1-08:** Cơ chế sao lưu cohort cần transactional manifest, khôi phục an toàn không xóa backup cũ khi rollback thất bại, bắt buộc kiểm tra SHA-256.
6. **P1-09 — P1-11:** Báo cáo M1 cần xác minh checksum file nguồn thực tế trên đĩa, cross-check metadata, và nhận `CODE_COMMIT` trực tiếp.
7. **P1-12 — P1-14:** Bảng xuất dữ liệu cần nghiêm ngặt loại trừ references chưa `coverage.complete`, giữ nguyên phân biệt giữa `None` và `[]`, kiểm tra foreign key `references.arxiv_id in papers.arxiv_id`.
8. **P1-15:** Cơ chế ghim `CODE_COMMIT` trong notebook cần fail-closed (dừng ngay nếu SHA không hợp lệ, không fallback ngầm). *(Đã xử lý trong Phase 0)*.
