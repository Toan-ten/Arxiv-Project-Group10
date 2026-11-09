# SỔ TAY VẬN HÀNH THU THẬP DỮ LIỆU MILESTONE 1 (M1 HANDOFF RUNBOOK)
## Nhóm 10 — Đồ án Khoa học Dữ liệu

---

### 1. Thông tin chung & Cam kết kiến trúc

- **Repository:** `Toan-ten/Arxiv-Project-Group10`
- **Nhánh làm việc (Branch):** `feat/m1-t02-colab-resume`
- **Mốc lịch sử đối chiếu (`CODE_COMMIT`):** `489a82fd959ccc6cced7c59fc309255e4421bef2` (Historical Baseline Pin — mốc nghiệm thu ban đầu, bất biến).
- **Mã commit thực thi chính thức (`EXECUTION_COMMIT`):** `69b4b5e758f74d255ff244c56271e294da57b8e4` (Runtime Execution Pin — mã nguồn đã xử lý triệt để toàn bộ các lỗi lưu trữ, kế toán archive và tách biệt làm giàu S2).
- **Sổ tay vận hành chính thức:** `notebooks/m1_colab_fast_production.ipynb`
- **Nguyên tắc cốt lõi:**
  1. **Raw Corpus là Chân lý (Source of Truth):** Theo Storage Contract v2, dữ liệu thô gồm tệp TeX/Bib đã giải nén và `manifest.json` trên ổ đĩa là sản phẩm bàn giao chính thức của Milestone 1. Các bảng xuất Parquet/CSV chỉ mang tính chất tiện ích hỗ trợ.
  2. **Đơn tiến trình (Single-Writer):** Mỗi sinh viên chỉ chạy 1 phiên Google Colab duy nhất cho vùng dữ liệu của mình. Tuyệt đối không mở đồng thời nhiều tab Colab cùng ghi vào một thư mục Google Drive.
  3. **Fail-Closed & Idempotent:** Hệ thống tự động kiểm tra mã băm SHA-256 và nhật ký kế toán của từng bài báo và từng lô. Nếu gặp sự cố mạng hoặc gián đoạn phiên, quá trình chạy lại sẽ tự động tiếp tục từ bài còn dở dang mà không làm hỏng dữ liệu đã có.

---

### 2. Phân công 4 thành viên (20.000 IDs / 200 Lô)

Toàn bộ 20.000 định danh bài báo arXiv được phân chia đều thành 200 lô (mỗi lô 100 bài). Mỗi thành viên phụ trách đúng 50 lô (5.000 định danh) theo cấu hình chuẩn `config/group10_id_ranges.json`:

| STT | Họ và Tên | MSSV | Khoảng Lô (Cohorts) | Số lượng IDs | Khoảng định danh arXiv (Từ .. Đến) |
| :---: | :--- | :---: | :---: | :---: | :---: |
| 1 | **Trần Văn Tấn Khôi** | `24120010` | Lô 1 .. 50 | 5.000 | `2508.07872` .. `2508.12871` |
| 2 | **Lê Thành Đạt** | `24120035` | Lô 51 .. 100 | 5.000 | `2507.16659` .. `2507.21658` |
| 3 | **Nguyễn Văn Toàn** | `24120232` | Lô 101 .. 150 | 5.000 | `2502.08235` .. `2502.13234` |
| 4 | **Trương Tuấn Anh** | `24120260` | Lô 151 .. 200 | 5.000 | `2501.02642` .. `2501.07641` |

---

### 3. Chuẩn bị môi trường Google Colab

1. **Cấu hình phần cứng:**
   - Vào menu: **Runtime** → **Change runtime type**.
   - Chọn **Hardware accelerator: CPU** (None).
   - *Lưu ý:* Tuyệt đối không chọn T4/A100 GPU vì chi phí tiêu tốn compute units vô ích (thu thập dữ liệu bị nghẽn bởi I/O mạng, không phải năng lực tính toán ma trận).

2. **Dung lượng Google Drive:**
   - Mỗi thành viên cần tối thiểu **10 GB** dung lượng trống trên Google Drive cá nhân.
   - Dữ liệu sao lưu sẽ tự động lưu trữ tại: `/content/drive/MyDrive/Group10_M1_Production`.

3. **Cấu hình Colab Secrets (Biểu tượng ổ khóa ở thanh công cụ bên trái):**
   - `KAGGLE_API_TOKEN`: *(Tùy chọn)* Khóa truy cập Kaggle để tự động tải tệp siêu dữ liệu snapshot `arxiv-metadata-oai-snapshot.json`.
   - `S2_API_KEY`: *(Khuyến nghị)* Khóa API Semantic Scholar để tăng tốc độ làm giàu dữ liệu trích dẫn (giảm thời gian chờ từ 3.0s xuống 1.0s mỗi yêu cầu).
   - Nhớ bật công tắc **Notebook access** cho các secret trên.

---

### 4. Quy trình thực thi từng bước (Step-by-Step)

Mở sổ tay `notebooks/m1_colab_fast_production.ipynb` trên Colab và thực hiện lần lượt các ô lệnh:

#### Bước 1: Kết nối Drive & Ghim mã nguồn thực thi
- Chạy **Ô lệnh 1 (Bước 1)**.
- Cho phép Colab kết nối với Google Drive.
- Hệ thống sẽ tự động fetch và checkout đúng commit `69b4b5e758f74d255ff244c56271e294da57b8e4` (`EXECUTION_COMMIT`).
- Nếu commit sau checkout không khớp, ô lệnh sẽ dừng ngay lập tức (fail-closed) để bảo đảm tính tái lập.

#### Bước 2: Nạp siêu dữ liệu Kaggle Snapshot
- Chạy **Ô lệnh 2 (Bước 2)**.
- Nếu có cấu hình `KAGGLE_API_TOKEN`, hệ thống sẽ tự động tải snapshot Cornell arXiv. Nếu không, hệ thống sử dụng truy vấn arXiv API trực tuyến an toàn.

#### Bước 3: Phân bổ và Thực thi thu thập TeX Source
- Chạy **Ô lệnh 3 (Bước 3)**.
- Trong ô lệnh này, bạn cấu hình 2 thông số:
  ```python
  MSSV = "24120260"          # Điền đúng MSSV của bạn (24120010, 24120035, 24120232, hoặc 24120260)
  MODE = "MEMBER_PRODUCTION" # "PILOT" (thử nghiệm 1 lô) hoặc "MEMBER_PRODUCTION" (toàn bộ 50 lô của bạn)
  ```
- **Khuyến nghị:** Trong lần đầu tiên chạy máy mới, hãy chọn `MODE = "PILOT"` để chạy thử nghiệm lô đầu tiên (100 IDs), kiểm tra xem Google Drive đã đồng bộ tệp nén `.tar.gz` và `.sha256` thành công hay chưa. Sau đó, chuyển sang `MODE = "MEMBER_PRODUCTION"` để tiến hành thu thập toàn bộ 50 lô.
- Hệ thống sẽ tự động gọi hàm `verify_cohort_handoff(runner, cohort_num)` sau mỗi lô hoàn thành 100 bài báo.

#### Bước 4: Làm giàu Semantic Scholar (S2 Enrichment)
- Chạy **Ô lệnh 4 (Bước 4)**.
- Quá trình này hoàn toàn tách biệt khỏi arXiv: chỉ duyệt qua các bài báo đã hoàn tất mã nguồn, nạp siêu dữ liệu bài viết và tối đa 100 trích dẫn từ Semantic Scholar API.
- Tự động đồng bộ bản sao lưu đã làm giàu lên Google Drive.

#### Bước 5: Kiểm toán độc lập và Xuất báo cáo M1
- Chạy **Ô lệnh 5 (Bước 5)**.
- Tạo báo cáo Markdown chi tiết tại `/content/drive/MyDrive/Group10_M1_Production/reports/m1_audit_summary_mssv_<MSSV>_<mode>.md`.
- Báo cáo phản ánh chính xác số bài thành công, số bài không tìm thấy, số phiên bản, và đối chiếu toàn vẹn (reconciliation).

#### Bước 6: (Tùy chọn) Xuất bảng Parquet & CSV
- Chạy **Ô lệnh 6 (Bước 6)**.
- Xuất các bảng `papers.parquet`, `references.parquet`, `papers.csv`, `references.csv` vào thư mục `final_output` trên Drive.
- *Lưu ý:* Bước này là tùy chọn hỗ trợ cho Milestone 2 & 3, không làm ảnh hưởng đến Raw Corpus.

---

### 5. Khôi phục sau sự cố & Tiếp tục chạy (Resume & Crash Recovery)

Nếu phiên Colab bị ngắt quãng (do mất mạng, hết giờ, hoặc người dùng chủ động tắt máy):

1. Mở lại Google Colab và mở lại sổ tay `m1_colab_fast_production.ipynb`.
2. Chạy lại **Ô lệnh 1** để kết nối Google Drive và nạp lại mã nguồn.
3. Chạy lại **Ô lệnh 3**.
4. Khi chạy lại, `runner.restore_cohort(cohort_num)` sẽ tự động:
   - Kiểm tra tệp sao lưu `cohort_XXXX_corpus.tar.gz` trên Drive.
   - Xác minh mã băm SHA-256 từ tệp `.sha256` đi kèm.
   - Xác minh danh mục bài báo trong tệp kế toán `.manifest.json`.
   - Giải nén an toàn sang ổ đĩa SSD cục bộ (`/content/fast_crawling_workspace`).
5. Pipeline sẽ tự động bỏ qua các bài đã hoàn thành và tiếp tục thu thập các bài còn thiếu. Tuyệt đối không tải lại các bài đã đạt chuẩn.

---

### 6. Tiêu chí Nghiệm thu Bàn giao (Handoff Acceptance Criteria)

Một lô (cohort) được công nhận đạt chuẩn bàn giao Milestone 1 khi và chỉ khi:
1. Đầy đủ 100 bài báo trong lô có trạng thái cuối cùng hợp lệ (`complete` hoặc `not_found`).
2. Có đầy đủ 3 tệp đồng bộ trên Google Drive tại thư mục `archives/`:
   - `cohort_XXXX_corpus.tar.gz`
   - `cohort_XXXX_corpus.tar.gz.sha256`
   - `cohort_XXXX_corpus.manifest.json`
3. Trạng thái lưu trữ trong tệp kế toán ghi nhận: `"archive_state": "handoff_verified"`.
4. Lệnh kiểm tra `verify_cohort_handoff(runner, cohort_num)` trả về kết quả `verified: True`.
