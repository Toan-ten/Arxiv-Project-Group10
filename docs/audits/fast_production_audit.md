# Báo cáo Audit Khoảng Trống Triển Khai (Implementation Gap Report)
## Fast Production — ArXiv Project Group 10

**Thời điểm kiểm tra ban đầu:** 09/11/2026  
**Thời điểm hoàn tất khắc phục:** 09/11/2026  
**Nhánh:** `feat/m1-t02-colab-resume`  
**Commit cơ sở:** `2a44fd4e50c0c3be6cd7efc3e4eee66308c9ee54`  
**Hợp đồng đối chiếu:** `docs/m1_contract.md`, `src/storage.py`, yêu cầu cào dữ liệu Milestone 1 HCMUS.

---

## 1. Tổng quan hiện trạng

Sau khi phân tích và triển khai theo Kế hoạch Thực thi Toàn diện (Master Implementation Plan), toàn bộ 19 phát hiện từ P0 đến P2 đều đã được khắc phục hoàn toàn, có kiểm thử đơn vị, kiểm thử chịu lỗi và kiểm tra hợp chuẩn schema bảo vệ.

---

## 2. Bảng phát hiện và kết quả nghiệm thu chi tiết

| ID | Phát hiện (Finding) | Mức độ | Tệp bị ảnh hưởng | Giải pháp đã triển khai | Trạng thái nghiệm thu |
|---|---|---|---|---|---|
| **REP-01** | Kiểm tra sai trạng thái version khi thống kê | **P1** | `scripts/generate_m1_report.py` | Sử dụng `TERMINAL = {"success", "no_tex_source"}`, tách rõ `successful_tex_versions` và `no_tex_versions`. | **RESOLVED** (Pass test) |
| **REP-02** | `completed_papers` không tuân thủ Storage contract | **P1** | `scripts/generate_m1_report.py` | Tách riêng `source_complete_papers` và `m1_complete_papers` (yêu cầu `manifest["status"]` thuộc `{"complete", "complete_with_no_tex"}`). | **RESOLVED** (Pass test) |
| **REP-03** | Thống kê references chưa kiểm tra `coverage.complete` | **P1** | `scripts/generate_m1_report.py` | Yêu cầu `status == "complete"` và `coverage["complete"] is True`. Phân biệt rõ `complete_reference_papers` và `partial_reference_papers`. | **RESOLVED** (Pass test) |
| **REP-04** | Bỏ qua API bảo vệ của `CorpusStorage` | **P1** | `scripts/generate_m1_report.py` | Chuyển sang gọi `store.read_manifest()`, `store.read_metadata()`, `store.read_references()` để tận dụng path safety và ownership check. | **RESOLVED** (Pass test) |
| **REP-05** | Nuốt lỗi âm thầm (`silent failure`) | **P1** | `scripts/generate_m1_report.py` | Báo cáo chi tiết `missing_manifests`, `invalid_manifests`, `invalid_references`, `unprocessed_papers` thay vì dùng `except Exception: continue`. | **RESOLVED** (Pass test) |
| **REP-06** | Thiếu thông tin xuất xứ báo cáo (Provenance) | **P2** | `scripts/generate_m1_report.py` | Bổ sung phần provenance metadata vào đầu bảng xuất Markdown (`report_generated_at`, `repository_commit`, `corpus_root`, `metric_definition_version`). | **RESOLVED** (Pass test) |
| **REP-07** | Thiếu kiểm thử đơn vị cho báo cáo thống kê | **P1** | `tests/test_generate_m1_report.py` | Bổ sung bộ kiểm thử 8 trường hợp bao phủ đầy đủ terminal success, no_tex, multi-version, references rỗng/partial, schema hỏng. | **RESOLVED** (Pass 8/8 tests) |
| **REC-01** | Xóa thư mục đích trước khi xác minh backup | **P0** | `src/fast_production.py` | Thực hiện nghiêm ngặt nguyên tắc **Validate → Stage → Verify → Atomic Swap** (sao chép ra thư mục tạm trước khi thay thế, có rollback nếu lỗi). | **RESOLVED** (Pass test) |
| **REC-02** | Khôi phục chỉ dựa vào `updated_at` | **P1** | `src/fast_production.py` | Xác minh manifest và mã băm SHA-256 của các tệp nguồn trong backup trước khi cho phép thay thế. | **RESOLVED** (Pass test) |
| **REC-03** | Đóng gói archive không kiểm tra checksum | **P1** | `src/fast_production.py` | Tự động sinh tệp `.sha256` đi kèm tệp nén `.tar.gz`, kiểm tra tính toàn vẹn của archive trước khi commit lên Drive. | **RESOLVED** (Pass test) |
| **REC-04** | Khôi phục không giới hạn phạm vi lô | **P1** | `src/fast_production.py` | Giới hạn chỉ trích xuất các thư mục bài báo thuộc danh sách `cohort_pairs(number)`, bỏ qua hoàn toàn các bài báo ngoại lai. | **RESOLVED** (Pass test) |
| **RUN-01** | `revision_history` mặc định bằng `False` trong runner | **P1** | `src/fast_production.py` | Đổi giá trị mặc định của `run_cohort` thành `revision_history: bool = True`. | **RESOLVED** (Pass test) |
| **RUN-02** | `count_completed_papers()` đếm sai trạng thái | **P1** | `src/fast_production.py` | Chỉ đếm là hoàn tất khi các version đạt `TERMINAL` (`success` hoặc `no_tex_source`) và `version_status == "resolved"`. | **RESOLVED** (Pass test) |
| **RUN-03** | Tính thời gian trung bình sai khi không có bài mới | **P2** | `src/fast_production.py` | Đặt `avg_seconds_per_paper = None` nếu không có bài báo mới được xử lý trong phiên chạy (`newly_done == 0`). | **RESOLVED** (Pass test) |
| **RUN-04** | Nhận diện batch ID thiếu các trường cấu hình cốt lõi | **P2** | `src/fast_production.py` | Đối chiếu dấu vân tay toàn diện gồm `max_attempts`, `revision_history`, `timeout` và cờ cấu hình `s2`. | **RESOLVED** (Pass test) |
| **S2-01** | Cố định `s2_config=None` trong Fast runner | **P1** | `src/fast_production.py` | Bổ sung phương thức `enrich_cohort_s2()` và tham số `s2_config` vào `run_cohort()` để thực hiện làm giàu dữ liệu S2 an toàn. | **RESOLVED** (Pass test) |
| **EXP-01** | Tự ý điền `ref_submission_date` không có nguồn | **P1** | `src/export.py` | Giữ nguyên giá trị `None` nếu không có `submission_date` thực tế trong trích dẫn Semantic Scholar; không tự bịa ngày nộp. | **RESOLVED** (Pass test) |
| **KAG-01** | Thiếu lưu vết xuất xứ (Provenance) Kaggle snapshot | **P2** | `src/kaggle_discovery.py` | Ghi nhận kích thước tệp, thời điểm nạp và dataset identifier vào kết quả nạp trước siêu dữ liệu. | **RESOLVED** (Pass test) |
| **NOTE-01** | Sổ tay Colab chưa ghim chặt commit SHA | **P2** | `notebooks/m1_colab_fast_production.ipynb` | Thêm ô kiểm tra `CODE_COMMIT` dạng 40 ký tự bất biến, chặn runtime bật GPU và phân chia rõ 6 bước thực thi. | **RESOLVED** (Pass test) |

---

## 3. Tổng kết nghiệm thu các Acceptance Gates

| Cổng kiểm soát (Gate) | Điều kiện nghiệm thu | Bằng chứng kiểm chứng | Kết quả |
|---|---|---|---|
| **G0** | Repository Snapshot xác định rõ | Branch `feat/m1-t02-colab-resume`, lịch sử commit rõ ràng | **PASS** |
| **G1** | Reporting đúng Storage Contract | 8/8 unit tests `test_generate_m1_report.py` đạt | **PASS** |
| **G2** | Backup/Restore an toàn | 16/16 unit tests `test_fast_production.py` đạt | **PASS** |
| **G3** | Fast Runner Resume đúng chuẩn | Phân định batch fingerprint, không xóa log cũ | **PASS** |
| **G4** | Kaggle Discovery có xuất xứ | 9/9 unit tests `test_kaggle_discovery.py` đạt | **PASS** |
| **G5** | Revision History đầy đủ | Bật mặc định `revision_history=True` trong runner và notebook | **PASS** |
| **G6** | Tích hợp Semantic Scholar an toàn | `enrich_cohort_s2()`, pacing 3.0s, chặn khi chưa xong arXiv | **PASS** |
| **G7** | Dataset Export hợp chuẩn M1 | Hàm `validate_exported_tables()`, 3/3 tests đạt | **PASS** |
| **G8** | Notebook Production chính thức duy nhất | `notebooks/m1_colab_fast_production.ipynb` chuẩn 6 bước | **PASS** |
| **G9** | Dữ liệu đối chứng Baseline/Fast tương đương | Mới kiểm thử logic offline, chưa chạy thực nghiệm đối chứng live | **NOT_TESTED** |
| **G10** | Hiệu năng cải thiện có đo lường thực tế | Chưa có bảng số liệu benchmark thời gian/RAM trên Colab CPU-only thực tế | **NOT_TESTED** |
| **G11** | Nghiệm thu trên Google Colab CPU-only thực tế | Cần người vận hành thực hiện trực tiếp trên Google Colab theo runbook | **BLOCKED** |
| **G12** | Tài liệu hóa và bảo mật hoàn tất | `README.md`, `fast_crawling_guide.md`, `audit.md` | **PASS** |
