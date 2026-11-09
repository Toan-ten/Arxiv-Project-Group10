# Báo Cáo Kiểm Toán Hệ Thống Báo Cáo M1 (Reporting Audit & Gap Analysis) — Step 2.0

**Dự án:** Nhập môn Khoa học Dữ liệu — Đồ án Milestone 1  
**Repository:** `Toan-ten/Arxiv-Project-Group10`  
**Nhánh:** `feat/m1-t02-colab-resume`  
**Baseline commit:** `d9a52d4dc439e63cfc065096f0737f1cd2398dde`  
**Thời điểm thực hiện kiểm toán:** 2026-11-09T18:58:00+07:00  
**Giai đoạn:** Phase 2 — Reporting Correctness (Step 2.0)  

---

## 1. Mục Tiêu & Phạm Vi Kiểm Toán

Kiểm toán toàn diện mã nguồn tính toán chỉ số thống kê và sinh báo cáo Milestone 1:
1. Đối chiếu hiện trạng triển khai với Hợp đồng lưu trữ v2 ([`docs/m1_contract.md`](file:///home/tuan-anh/Documents/DS_PJ/Arxiv-Project-Group10/docs/m1_contract.md), [`src/storage.py`](file:///home/tuan-anh/Documents/DS_PJ/Arxiv-Project-Group10/src/storage.py)) và API kiểm toán tính toàn vẹn Phase 1 ([`src/storage_integrity.py`](file:///home/tuan-anh/Documents/DS_PJ/Arxiv-Project-Group10/src/storage_integrity.py)).
2. Rà soát toàn bộ các điểm tính toán chỉ số: `compute_m1_statistics()`, `format_markdown_summary()`, CLI `main()`, và các điểm tiêu thụ số liệu trong sổ tay Colab (`notebooks/m1_colab_fast_production.ipynb`).
3. Xác định các sai lệch (findings), khoảng trống kiểm thử (coverage gaps), và rủi ro tính toàn vẹn số liệu trước khi xây dựng đặc tả chuẩn hóa (Step 2.1).

---

## 2. Các Tệp Được Kiểm Tra (Files Inspected)

- [`scripts/generate_m1_report.py`](file:///home/tuan-anh/Documents/DS_PJ/Arxiv-Project-Group10/scripts/generate_m1_report.py): Điểm tính toán số liệu và định dạng báo cáo Markdown chính.
- [`src/storage.py`](file:///home/tuan-anh/Documents/DS_PJ/Arxiv-Project-Group10/src/storage.py): Định nghĩa trạng thái và hợp đồng lưu trữ v2.
- [`src/storage_integrity.py`](file:///home/tuan-anh/Documents/DS_PJ/Arxiv-Project-Group10/src/storage_integrity.py): API kiểm toán tính toàn vẹn tệp nguồn, checksum và tính hoàn tất M1.
- [`src/fast_production.py`](file:///home/tuan-anh/Documents/DS_PJ/Arxiv-Project-Group10/src/fast_production.py): Runner production, hàm `count_completed_papers()` và `cohort_pairs()`.
- [`src/id_ranges.py`](file:///home/tuan-anh/Documents/DS_PJ/Arxiv-Project-Group10/src/id_ranges.py): Nguồn danh sách định danh phân công (`iter_assigned_ids`).
- [`src/benchmark_runner.py`](file:///home/tuan-anh/Documents/DS_PJ/Arxiv-Project-Group10/src/benchmark_runner.py): Đo lường hiệu năng và đối chiếu trạng thái bài báo.
- [`tests/test_generate_m1_report.py`](file:///home/tuan-anh/Documents/DS_PJ/Arxiv-Project-Group10/tests/test_generate_m1_report.py): Bộ kiểm thử đơn vị hiện có của báo cáo.
- [`notebooks/m1_colab_fast_production.ipynb`](file:///home/tuan-anh/Documents/DS_PJ/Arxiv-Project-Group10/notebooks/m1_colab_fast_production.ipynb): Ô thực thi Bước 5 tạo báo cáo kiểm toán trên Drive.

---

## 3. Danh Mục Chỉ Số Hiện Tại (Existing Metrics Inventory)

| Tên chỉ số trong code | Ý nghĩa theo định nghĩa cũ | Cách tính hiện tại | Đánh giá sơ bộ |
|---|---|---|---|
| `total_candidates` | Tổng số ứng viên kiểm tra | `len(pairs_list)` | Phụ thuộc vào danh sách đầu vào `pairs` |
| `found_papers` | Bài báo tìm thấy trên arXiv | `discovery_status == "found"` | Chưa đối chiếu schema đầy đủ |
| `not_found_papers` | Bài báo không tồn tại | `discovery_status == "not_found"` | Chưa đối chiếu điều kiện versions rỗng |
| `unprocessed_papers` | Bài báo chưa xử lý | `discovery_status == "not_attempted"` | Chưa đối chiếu trạng thái manifest |
| `missing_manifests` | Hồ sơ thiếu manifest | `not manifest_file.exists()` | Đúng |
| `invalid_manifests` | Hồ sơ manifest hỏng | Exception khi gọi `store.read_manifest` | Đúng |
| `source_complete_papers` | Bài báo hoàn tất mã nguồn | `all(v["status"] in TERMINAL)` trong manifest | **SAI:** Không kiểm tra tệp thực tế trên đĩa |
| `m1_complete_papers` | Bài báo hoàn tất toàn bộ M1 | `manifest["status"] in {"complete", "complete_with_no_tex"}` | **SAI:** Chỉ dựa vào trường chuỗi của manifest |
| `partial_papers` | Bài báo dở dang | `manifest["status"] == "partial"` | Đúng nhưng chưa thành partition đầy đủ |
| `failed_papers` | Bài báo thất bại | `manifest["status"] == "failed"` hoặc discovery lạ | Có nguy cơ đếm trùng |
| `interrupted_papers` | Bài báo bị gián đoạn | `manifest["status"] == "interrupted"` | Đúng |
| `total_versions` | Tổng số phiên bản bài báo | Tổng `len(versions)` của các bài `found` | Phụ thuộc `manifest.versions` |
| `successful_tex_versions` | Phiên bản TeX thành công | `v["status"] == "success"` | Chỉ đếm theo manifest |
| `no_tex_versions` | Phiên bản không TeX | `v["status"] == "no_tex_source"` | Chỉ đếm theo manifest |
| `terminal_versions` | Phiên bản kết thúc | `successful_tex_versions + no_tex_versions` | Đúng công thức |
| `incomplete_versions` | Phiên bản chưa xong | `status in {"pending", "in_progress", "interrupted"}` | Đúng |
| `failed_versions` | Phiên bản lỗi | `status in SOURCE_ERRORS` | Đúng |
| `complete_reference_papers` | Bài báo trích dẫn hoàn tất | `ref_st == "complete" and coverage.complete is True` | Đúng tiêu chí coverage |
| `partial_reference_papers` | Bài báo trích dẫn dở dang | `ref_st == "partial"` hoặc coverage incomplete | Đúng |
| `failed_reference_papers` | Bài báo trích dẫn lỗi | `ref_st == "failed"` | Đúng |
| `not_attempted_reference_papers`| Trích dẫn chưa thử | `ref_st == "not_attempted"` hoặc thiếu file | Đúng |
| `invalid_references` | Hồ sơ trích dẫn hỏng | Exception khi đọc `references.json` | Đếm số lỗi đọc |
| `total_complete_reference_records`| Tổng số liên kết trích dẫn hoàn tất | Tổng `len(items)` của các bài complete | Đúng |
| `total_observed_reference_records`| Tổng số bản ghi trích dẫn thấy | Tổng `len(items)` thấy được | Đúng |
| `source_completion_rate` | Tỷ lệ hoàn tất nguồn | `source_complete_papers / found_papers * 100` | Mặc định 0.0 khi mẫu số bằng 0 |
| `m1_completion_rate` | Tỷ lệ hoàn tất M1 | `m1_complete_papers / found_papers * 100` | Mặc định 0.0 khi mẫu số bằng 0 |
| `version_extraction_success_rate`| Tỷ lệ bóc tách TeX | `successful_tex_versions / total_versions * 100` | Mặc định 0.0 khi mẫu số bằng 0 |
| `no_tex_rate` | Tỷ lệ no_tex | `no_tex_versions / total_versions * 100` | Mặc định 0.0 khi mẫu số bằng 0 |
| `references_coverage_rate` | Độ phủ trích dẫn | `complete_reference_papers / found_papers * 100` | Mặc định 0.0 khi mẫu số bằng 0 |
| `average_references_per_paper` | Trích dẫn trung bình/bài | `total_complete_refs / complete_ref_papers` | Mặc định 0.0 khi mẫu số bằng 0 |

---

## 4. Bảng Tổng Hợp Phát Hiện Kiểm Toán (Confirmed Findings)

| ID | Tiêu đề phát hiện | Mức độ | Vị trí mã nguồn | Kịch bản lỗi thực tế | Đề xuất khắc phục (Phase 2) |
|---|---|:---:|---|---|---|
| **FIND-REP-01** | `m1_complete_papers` chỉ dựa vào `manifest.status`, bỏ qua dữ liệu thực tế | **Critical** | `scripts/generate_m1_report.py:137-140` | Manifest ghi `status="complete"`, nhưng `metadata.json` bị hỏng/dở dang (`partial`), hoặc `references.json` bị thiếu/`coverage.complete=False`, hoặc tệp TeX trên đĩa bị xóa. Báo cáo vẫn cộng 1 vào `m1_complete_papers`. | Bắt buộc đối chiếu đồng thời: `is_source_complete AND is_metadata_complete AND is_references_complete AND manifest.status in {"complete", "complete_with_no_tex"} AND len(violations) == 0`. Tích hợp trực tiếp `audit_paper_integrity`. |
| **FIND-REP-02** | `source_complete_papers` không kiểm toán tệp và mã băm thực tế trên đĩa | **Critical** | `scripts/generate_m1_report.py:115-121` | Tệp `main.tex` bị sửa nội dung làm sai SHA-256, bị thay bằng symlink/hardlink, hoặc dung lượng không khớp `kept_bytes`. Do chỉ đọc manifest, báo cáo vẫn tính bài báo hoàn tất mã nguồn. | Tích hợp API `audit_version_integrity` / `audit_paper_integrity` để kiểm tra toàn bộ tệp, checksum và `kept_bytes` thực tế trên đĩa. |
| **FIND-REP-03** | `metadata.json` hoàn toàn không được đọc và xác thực trong báo cáo | **Major** | `scripts/generate_m1_report.py:88-174` | `compute_m1_statistics` chỉ đọc `manifest.json` và `references.json`, hoàn toàn bỏ qua `metadata.json`. Thiếu chỉ số `metadata_complete_papers`, không kiểm tra cross-check trạng thái `version_status` hay tính đầy đủ của trường học thuật. | Bổ sung khối đọc và xác thực `metadata.json` qua `store.read_metadata()`. Bổ sung metric `metadata_complete_papers`, `metadata_partial_papers`, `metadata_failed_papers`. |
| **FIND-REP-04** | Trạng thái trích dẫn không được đối chiếu nhất quán liên tệp với manifest | **Major** | `scripts/generate_m1_report.py:148-171` | `ref_data["status"]` được đọc nhưng không đối chiếu với `manifest["references_status"]`. Nếu manifest ghi `complete` nhưng file references ghi `partial` (hoặc ngược lại), sự bất nhất không bị phát hiện và vẫn tính số liệu dở dang. | Thực hiện cross-file consistency check giữa manifest và references. Ghi nhận vi phạm tính toàn vẹn nếu hai bên mâu thuẫn. |
| **FIND-REP-05** | Phân loại bài báo không tạo thành phân hoạch đầy đủ (Exhaustive & Mutually Exclusive Partition) | **Major** | `scripts/generate_m1_report.py:59-70, 88-174` | Các counter cấp bài báo bị đếm chồng lấn hoặc bỏ sót: `m1_complete_papers`, `partial_papers`, `failed_papers`, `interrupted_papers` vừa là trạng thái stage vừa được tính song song với `found_papers`. Tổng các trạng thái không thể đối chiếu (reconcile) bằng tổng `total_candidates`. | Thiết kế mô hình phân loại đa tầng tường minh: (1) Discovery state partition, (2) Lifecycle stage partition, (3) Milestone 1 completion invariant. Bảo đảm reconciliation: `assigned_candidates = found + confirmed_not_found + unprocessed + missing_manifest + invalid_manifest + discovery_failed + other_unresolved`. |
| **FIND-REP-06** | `_get_git_commit(store.root)` gọi git trên thư mục corpus, luôn trả về `UNKNOWN` | **Major** | `scripts/generate_m1_report.py:29-38, 197` | `store.root` là thư mục corpus (ví dụ trên Drive hoặc thư mục dữ liệu cục bộ không có `.git`). Lệnh `git -C <store.root> rev-parse HEAD` gặp lỗi `fatal: not a git repository` và luôn fallback về `"UNKNOWN"`, làm mất khả năng truy vết nguồn gốc báo cáo. | Tìm git root bắt đầu từ vị trí script (`Path(__file__).resolve().parents[1]`) hoặc sử dụng `PROJECT_ROOT` từ `src.local_config`. Bổ sung cảnh báo rõ ràng nếu chạy ngoài git repository thay vì âm thầm trả về `UNKNOWN`. |
| **FIND-REP-07** | Tỷ lệ phần trăm âm thầm trả về `0.0%` khi mẫu số bằng 0 | **Moderate** | `scripts/generate_m1_report.py:176-195, 267-275` | Khi `found_papers == 0` (ví dụ chạy trên tập dữ liệu trống hoặc cohort chưa crawl), các tỷ lệ `source_completion_rate`, `m1_completion_rate`, `references_coverage_rate` đều trả về `0.0%`. Điều này gây ngộ nhận rằng đã crawl và đạt kết quả 0% thay vì trạng thái không xác định (undefined / N/A). | Chuẩn hóa quy tắc mẫu số: nếu denominator == 0, trả về `None` trong JSON cấu trúc và hiển thị `N/A` trong bảng Markdown. |
| **FIND-REP-08** | Thao tác `store.root.resolve()` thiếu bắt ngoại lệ phòng vệ hệ thống tệp | **Moderate** | `scripts/generate_m1_report.py:203` | Gọi trực tiếp `store.root.resolve()` trong khối tạo từ điển provenance. Nếu corpus root là một symlink loop hoặc xảy ra lỗi I/O, báo cáo bị dừng đột ngột bằng `RuntimeError` / `OSError`. | Bao bọc `store.root.resolve()` trong khối `try ... except (OSError, RuntimeError)` để xử lý an toàn. |
| **FIND-REP-09** | Bảng Markdown báo cáo thiếu các thông số kiểm toán quan trọng | **Moderate** | `scripts/generate_m1_report.py:260-276` | Bảng Markdown hiện tại không có dòng về `metadata_complete_papers`, không hiển thị thông tin đối chiếu reconciliation, và gộp chung `missing_manifests + invalid_manifests` thành một dòng duy nhất. | Tái cấu trúc bảng Markdown: hiển thị đầy đủ 4 giai đoạn hoàn tất (Discovery, Source, Metadata, References), bảng phân loại toàn diện (Reconciliation Matrix), và chi tiết vi phạm tính toàn vẹn (Integrity Summary). |
| **FIND-REP-10** | Thiếu tùy chọn xuất báo cáo máy đọc được dạng JSON chuẩn hóa | **Minor** | `scripts/generate_m1_report.py:280-295` | CLI `main()` chỉ hỗ trợ `--output` xuất Markdown, không xuất JSON có cấu trúc để các công cụ CI/CD hoặc pipeline tự động kiểm tra định lượng. | Bổ sung cờ `--json` trong CLI và hàm lưu JSON báo cáo chuẩn hóa. |

---

## 5. Khoảng Trống Kiểm Thử (Test Coverage Gaps)

Kiểm tra `tests/test_generate_m1_report.py` cho thấy các kịch bản quan trọng sau chưa được kiểm thử:

1. **Kiểm thử tệp TeX bị lỗi mã băm trên đĩa:** Chưa có test mô phỏng trường hợp manifest ghi version `success` nhưng tệp TeX bị sai SHA-256 hoặc bị xóa, xác nhận `source_complete_papers` và `m1_complete_papers` phải bằng 0.
2. **Kiểm thử bất biến M1 Complete:** Chưa có test mô phỏng manifest ghi `status="complete"` nhưng `metadata.status="partial"` hoặc `references.coverage.complete=False`, xác nhận `m1_complete_papers` phải loại trừ bài báo này.
3. **Kiểm thử phân hoạch đầy đủ (Reconciliation Invariant):** Chưa có test xác thực tính nhất quán số học giữa tổng các tập hợp con và `total_candidates`.
4. **Kiểm thử mẫu số bằng 0 (Zero Denominator):** Chưa có test kiểm tra hành vi khi `total_candidates == 0` hoặc `found_papers == 0`, bảo đảm không báo `0.0%` giả tạo.
5. **Kiểm thử truy xuất Git commit provenance:** Chưa có test kiểm tra hàm lấy commit SHA từ git root thật.
6. **Kiểm thử Fault Injection:** Chưa có test mô phỏng lỗi I/O hoặc JSONDecodeError đối với các tệp metadata/references.

---

## 6. Các Nội Dung Không Thuộc Phạm Vi Phase 2

Để bảo đảm tính tập trung và an toàn kiến trúc, các nội dung sau **tuyệt đối không thực hiện** trong Phase 2:
1. Không thay đổi hoặc thiết kế lại định dạng xuất Parquet/CSV của Phase 3 (`src/export.py`).
2. Không thay đổi kiến trúc crawling hoặc sửa đổi pipeline Fast Production (`src/fast_production.py`).
3. Không thực hiện cào dữ liệu thật từ arXiv hoặc Semantic Scholar API.
4. Không thay đổi các hằng số hợp đồng lưu trữ v2 trong `src/storage.py`.
5. Không thay đổi biến lịch sử `CODE_COMMIT = "489a82fd959ccc6cced7c59fc309255e4421bef2"`.

---

## 7. Kết Luận & Kế Hoạch Bước Tiếp Theo

Audit Step 2.0 đã chỉ rõ nguyên nhân cốt lõi khiến các chỉ số báo cáo hiện tại chưa hoàn toàn phản ánh trung thực thực tế lưu trữ: **báo cáo đang tin tưởng các trường trạng thái chuỗi trong manifest thay vì sử dụng bằng chứng kiểm toán thực tế trên đĩa từ `src/storage_integrity.py`**.

**Kế hoạch tiếp theo (Step 2.1):**
Soạn thảo tài liệu đặc tả số liệu chính thức `docs/reporting_metrics.md`, định nghĩa rõ từng metric, công thức, mẫu số, điều kiện bao gồm/loại trừ, và các bất biến reconciliation trước khi bắt đầu viết code thực thi ở Step 2.2.
