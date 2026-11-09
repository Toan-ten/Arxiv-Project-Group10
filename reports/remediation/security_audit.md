# Security & Destructive Operations Audit — Phase 0

**Dự án:** Nhập môn Khoa học Dữ liệu — Đồ án Milestone 1  
**Repository:** `Toan-ten/Arxiv-Project-Group10`  
**Nhánh:** `feat/m1-t02-colab-resume`  
**Thời điểm audit:** 2026-11-09T14:46:00+07:00  

---

## 1. Kết Quả Audit Thao Tác Phá Hủy Dữ Liệu (Destructive Operations)

### Phát hiện P0-01 và biện pháp xử lý:
- **Phát hiện:** Ô lệnh cuối cùng (Cell 8) trong [`notebooks/m1_colab_fast_production.ipynb`](file:///home/tuan-anh/Documents/DS_PJ/Arxiv-Project-Group10/notebooks/m1_colab_fast_production.ipynb) chứa lệnh `shutil.rmtree(Path("/content/drive/MyDrive/Group10_M1_Production"))`.
- **Hành động khắc phục:** Đã **xóa bỏ hoàn toàn Cell 8** khỏi notebook. Notebook production hiện chỉ còn đúng 8 cells (1 markdown banner, 1 quy định vận hành, và 6 code cells tương ứng với 6 bước tuần tự).
- **Quy tắc an toàn mới:** Không có bất kỳ dòng code production nào được phép xóa tự động thư mục `DRIVE_ROOT` hoặc `drive_production`. Bất kỳ thao tác dọn dẹp nào chỉ được phép tác động lên vùng tạm thời (`scratch_shm` hoặc `local_workspace`).
- **Bài kiểm thử tự động:** Đã bổ sung `test_notebooks_do_not_contain_destructive_drive_cleanup` vào [`tests/test_fast_production.py`](file:///home/tuan-anh/Documents/DS_PJ/Arxiv-Project-Group10/tests/test_fast_production.py), bảo đảm chặn mọi commit vô tình đưa lệnh xóa `shutil.rmtree` đối với `drive_production` hoặc `DRIVE_ROOT`.

---

## 2. Kết Quả Audit Bí Mật & Thông Tin Xác Thực (Secret Security Audit)

### Phạm vi rà soát:
- Toàn bộ mã nguồn Python trong `src/`, `scripts/`, `tests/`.
- Toàn bộ 4 sổ tay Jupyter trong `notebooks/` (bao gồm source cells, metadata và cell outputs).
- Lịch sử Git commit bằng lệnh tìm kiếm nhị phân (`git log -S`) đối với các tiền tố `KGAT_`, `s2k-`, `ghp_`.

### Kết luận kiểm toán:
1. **Mã nguồn và Notebook:** 100% sạch, không chứa bất kỳ khóa Kaggle token (`KGAT_...`), Semantic Scholar key (`s2k-...`) hay GitHub token nào.
2. **Cơ chế nạp bí mật an toàn:**
   - Kaggle Token: Nạp từ Colab Secrets `userdata.get("KAGGLE_API_TOKEN")` hoặc biến môi trường `os.environ.get("KAGGLE_API_TOKEN")`.
   - Semantic Scholar API Key: Nạp từ Colab Secrets qua `local_config.get_semantic_scholar_api_key()`.
3. **Phát hiện cảnh báo:** Đối với các token xác thực (`[REDACTED]` và `[REDACTED]`) từng xuất hiện trong nội dung trao đổi, toàn bộ giá trị đã được coi là bị lộ (compromised). Khuyến nghị người dùng bắt buộc phải thu hồi (revoke/rotate) ngay lập tức trên trang quản lý tài khoản Kaggle và Semantic Scholar. Toàn bộ tài liệu báo cáo và commit tuyệt đối không lưu lại giá trị thực tế của các token này dưới bất kỳ hình thức nào.

---

## 3. Kết Quả Audit An Toàn Sổ Tay Production (Notebook Safety)

1. **Khóa cứng commit (Fail-closed CODE_COMMIT):**
   - Đã thay đổi logic kiểm tra `CODE_COMMIT` trong Bước 1: Nếu người dùng không cung cấp chuỗi SHA 40 ký tự hợp lệ, hệ thống sẽ **bật lỗi `RuntimeError` ngay lập tức và dừng thực thi** (fail-closed), không tự động fallback sang `HEAD` của nhánh để bảo đảm tính tái lập kết quả.
   - Kiểm tra commit sau khi checkout: Nếu mã băm HEAD không khớp `CODE_COMMIT`, hệ thống báo lỗi dừng lại.
2. **Chặn tài nguyên GPU:**
   - Hệ thống giữ nguyên cơ chế kiểm tra biến môi trường `COLAB_GPU == "1"` và dừng ngay lập tức nếu phát hiện accelerator là GPU, tuân thủ nghiêm ngặt yêu cầu phần cứng CPU-only của đề bài M1.
3. **Bài kiểm thử tự động:** Đã bổ sung `test_fast_production_notebook_safety_guards` vào [`tests/test_fast_production.py`](file:///home/tuan-anh/Documents/DS_PJ/Arxiv-Project-Group10/tests/test_fast_production.py) để tự động bảo vệ hai tính năng trên.
