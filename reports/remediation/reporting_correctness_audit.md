# Báo Cáo Nghiệm Thu Hệ Thống Báo Cáo M1 (Reporting Correctness Audit) — Phase 2

**Dự án:** Nhập môn Khoa học Dữ liệu — Đồ án Milestone 1  
**Repository:** `Toan-ten/Arxiv-Project-Group10`  
**Nhánh:** `feat/m1-t02-colab-resume`  
**Baseline commit:** `d9a52d4dc439e63cfc065096f0737f1cd2398dde`  
**Thời điểm hoàn tất audit:** 2026-11-09T20:10:00+07:00  
**Giai đoạn:** Phase 2 — Reporting Correctness (Hoàn tất)  

---

## 1. Tóm Tắt Thực Thi (Execution Summary)

Hệ thống tính toán chỉ số và sinh báo cáo kiểm toán Milestone 1 đã được tái cấu trúc toàn diện theo phương châm **Correctness-First**:
- Thay thế hoàn toàn cơ chế đọc chuỗi trạng thái thô trong manifest bằng **bằng chứng kiểm toán thực tế trên đĩa** thông qua API `src.storage_integrity` (Phase 1).
- Xây dựng mô hình phân loại đa tầng bảo đảm các **bất biến đối chiếu số học (Reconciliation Invariants)**: tập định danh phân công, vòng đời bài báo tìm thấy và phân loại phiên bản đều cân bằng 100%.
- Bổ sung xác thực độc lập cho siêu dữ liệu học thuật (`metadata.json`) và tài liệu tham khảo (`references.json`), bảo đảm thứ bậc hoàn tất M1: bài báo chỉ đạt `m1_complete` khi đồng thời đạt `source_complete`, `metadata_complete`, `references_complete` và không có vi phạm tính toàn vẹn nào.
- Chuẩn hóa xuất xứ báo cáo (Provenance): tự động phát hiện mã băm Git commit 40 ký tự từ thư mục gốc dự án, bảo toàn ngữ nghĩa Null (`None`/`N/A`) cho các trường hợp mẫu số bằng 0.

---

## 2. Các Tệp Được Bổ Sung & Thay Đổi (Files Changed)

1. [`src/reporting.py`](file:///home/tuan-anh/Documents/DS_PJ/Arxiv-Project-Group10/src/reporting.py): Module tính toán số liệu chuẩn hóa trung tâm (Canonical Reporting Computation Layer), định nghĩa `compute_m1_statistics`, `format_markdown_summary`, `export_report_json`.
2. [`scripts/generate_m1_report.py`](file:///home/tuan-anh/Documents/DS_PJ/Arxiv-Project-Group10/scripts/generate_m1_report.py): Điểm nhập CLI chính thức, tái xuất bản API và hỗ trợ xuất cả Markdown và JSON có cấu trúc.
3. [`docs/reporting_metrics.md`](file:///home/tuan-anh/Documents/DS_PJ/Arxiv-Project-Group10/docs/reporting_metrics.md): Đặc tả chuẩn hóa toàn bộ chỉ số, công thức, mẫu số và bất biến đối chiếu v2.1.0.
4. [`reports/remediation/reporting_audit.md`](file:///home/tuan-anh/Documents/DS_PJ/Arxiv-Project-Group10/reports/remediation/reporting_audit.md): Báo cáo rà soát hiện trạng và phân tích khoảng trống kiểm thử (Step 2.0).
5. [`tests/test_reporting_correctness.py`](file:///home/tuan-anh/Documents/DS_PJ/Arxiv-Project-Group10/tests/test_reporting_correctness.py): Bộ kiểm thử đơn vị, kiểm thử tích hợp và kiểm thử lỗi (16 test cases chuyên biệt cho Phase 2).
6. [`reports/remediation/reporting_correctness_audit.md`](file:///home/tuan-anh/Documents/DS_PJ/Arxiv-Project-Group10/reports/remediation/reporting_correctness_audit.md): Báo cáo nghiệm thu chính thức Phase 2.

---

## 3. Khắc Phục Chi Tiết Các Phát Hiện Kiểm Toán

### 3.1. Các phát hiện vòng rà soát ban đầu (Initial Audit Findings FIND-REP-01 đến FIND-REP-10)

| ID | Vấn đề đã xác nhận | Biện pháp xử lý kỹ thuật | Ca kiểm thử bảo vệ |
|---|---|---|---|
| **FIND-REP-01** | `m1_complete_papers` đếm theo `manifest.status` mà không kiểm tra thực tế | Bắt buộc thỏa mãn đồng thời: `is_source_complete AND is_metadata_complete AND is_references_complete AND manifest.status in {"complete", "complete_with_no_tex"} AND len(violations) == 0`. | `test_manifest_complete_with_partial_metadata_rejected_from_m1_complete`, `test_manifest_complete_with_incomplete_references_rejected_from_m1_complete` |
| **FIND-REP-02** | `source_complete_papers` không kiểm tra tệp và mã băm thực tế trên đĩa | Tích hợp trực tiếp `audit_version_integrity()` kiểm tra 100% tệp TeX/Bib, mã băm SHA-256, `kept_bytes` và liên kết an toàn. | `test_manifest_complete_with_tampered_source_checksum_rejected_from_m1_complete`, `test_version_state_partition_and_reconciliation` |
| **FIND-REP-03** | Bỏ qua việc đọc và xác thực `metadata.json` | Đọc độc lập `metadata.json` qua `store.read_metadata()`, kiểm tra `metadata.status == "complete"`, nhất quán với manifest và không có vi phạm. Bổ sung metric `metadata_complete_papers`. | `test_manifest_complete_with_partial_metadata_rejected_from_m1_complete`, `test_full_m1_complete_paper_passes_all_invariants` |
| **FIND-REP-04** | Trạng thái trích dẫn không đối chiếu nhất quán liên tệp | Bắt buộc đối chiếu `manifest.references_status == "complete"` VÀ `references.status == "complete"` VÀ `coverage.complete is True` VÀ `coverage.continuation is None`. | `test_manifest_complete_with_incomplete_references_rejected_from_m1_complete`, `test_references_partial_and_failed_states` |
| **FIND-REP-05** | Phân loại bài báo không tạo thành phân hoạch đầy đủ | Thiết lập phân hoạch cấp định danh: $\text{assigned} = \text{found} + \text{not\_found} + \text{unprocessed} + \text{discovery\_failed} + \text{missing} + \text{invalid} + \text{unresolved}$. Phân hoạch bài báo tìm thấy: $\text{found} = \text{m1\_complete} + \text{partial} + \text{failed} + \text{interrupted} + \text{unresolved\_found}$. | `test_reconciliation_equation_holds_across_mixed_cohort`, `test_missing_and_malformed_manifests` |
| **FIND-REP-06** | `_get_git_commit` gọi git trên thư mục corpus, luôn trả về `UNKNOWN` | Tìm kiếm Git root bắt đầu từ `PROJECT_ROOT` (`src.local_config`) và vị trí tệp mã nguồn. Xác thực chuỗi trả về khớp chuẩn regex SHA-256 (`[0-9a-f]{40}`). | `test_get_git_commit_returns_40_hex_sha_from_project_root` |
| **FIND-REP-07** | Tỷ lệ phần trăm âm thầm trả về `0.0%` khi mẫu số bằng 0 | Khi mẫu số bằng 0, hàm trả về `None` trong JSON và hiển thị `N/A` trong bảng Markdown. | `test_zero_candidates_and_zero_found_returns_none_rates` |
| **FIND-REP-08** | Thao tác `store.root.resolve()` thiếu bắt ngoại lệ | Bao bọc trong khối `try ... except (OSError, RuntimeError)` để xử lý an toàn khi đường dẫn không thể phân giải. | `test_corpus_root_unresolvable_handled_gracefully` |
| **FIND-REP-09** | Bảng Markdown thiếu thông số kiểm toán | Thiết kế lại Markdown với 5 mục rõ ràng: Provenance, Stage Metrics, Version Statistics, References Statistics, và Reconciliation Summary. | `test_format_markdown_summary_provenance`, `test_zero_candidates_and_zero_found_returns_none_rates` |
| **FIND-REP-10** | Thiếu tùy chọn xuất báo cáo JSON máy đọc được | Bổ sung hàm `export_report_json(stats, out_path)` và tham số `--json` trong CLI `generate_m1_report.py`. | `test_export_report_json_creates_valid_file` |

### 3.2. Khắc phục các phát hiện thẩm định độc lập (Phase 2 Remediation FIND-REP-A đến FIND-REP-D)

| ID | Mức độ | Khiếm khuyết thẩm định | Giải pháp kỹ thuật đã triển khai | Ca kiểm thử bảo vệ |
|---|---|---|---|---|
| **FIND-REP-A** | Critical | `source_complete` phụ thuộc vào `metadata` và `references` | Tách biệt hoàn toàn việc thẩm định source: `audit_paper_integrity()` và `compute_m1_statistics()` không thoát sớm hay đánh rớt `is_source_complete` khi metadata hoặc references bị hỏng/thiếu. | `test_find_rep_a_source_complete_independent_of_broken_metadata_and_references` |
| **FIND-REP-B** | Critical | `integrity_failed_papers` đếm nhầm trạng thái incomplete thông thường | Định nghĩa lại `has_integrity_failure` với ngữ nghĩa tường minh: chỉ đếm các vi phạm toàn vẹn thực tế (tệp hỏng `corrupted`, tệp ngoại lai `unmanaged`, sai SHA-256, sai `kept_bytes`, vi phạm symlink, lỗi schema trên tệp tồn tại, hoặc thư mục `vN/` tồn tại khi manifest ghi `not_found`). Các trạng thái dữ liệu chưa thu thập thông thường (`not_attempted`, `in_progress`) và các tệp metadata/references chưa sinh không bị tính là lỗi toàn vẹn. | `test_find_rep_b_integrity_failed_papers_distinguishes_incomplete_from_corruption`, `test_has_integrity_failure_semantic_boundaries` |
| **FIND-REP-C** | High | `confirmed_not_found` chưa kiểm tra hệ thống tệp | Bổ sung kiểm tra thực tế: bài báo `not_found` bắt buộc không có bất kỳ thư mục `vN/` hay tệp unmanaged nào trên đĩa. Nếu phát hiện thư mục version, bài báo bị chuyển sang `invalid_manifests` và tăng `integrity_failed_papers`. | `test_find_rep_c_not_found_with_filesystem_version_dir_rejected_from_confirmed_not_found` |
| **FIND-REP-D** | High | Reconciliation chưa được kiểm chứng độc lập bài báo M1-complete | Triển khai `_perform_independent_reconciliation` gồm 2 pha kiểm toán độc lập hoàn chỉnh: (1) Quét đĩa thô và đối chiếu phân hoạch ứng viên kèm đối chiếu thư mục phiên bản fail-closed (`raw_version_dirs_count <= raw_total_versions`); (2) Tái thẩm định độc lập toàn diện 12 tiêu chí chất lượng cho 100% bài báo `m1_complete` từ dữ liệu đĩa thô mà không tái sử dụng biến boolean của lượt 1. Bất kỳ sai lệch hoặc lỗi I/O nào đều kích hoạt fail-closed (`is_reconciled = False`). Hỗ trợ hook `_between_passes_hook` để kiểm thử đột biến có kiểm soát. | `test_find_rep_d_independent_reconciliation_detects_spoofed_or_discrepant_counts`, `test_find_rep_d_m1_complete_metadata_corrupted_between_passes_fails_reconciliation`, `test_find_rep_d_m1_complete_references_incomplete_between_passes_fails_reconciliation`, `test_find_rep_d_m1_complete_source_checksum_tampered_between_passes_fails_reconciliation`, `test_find_rep_d_m1_complete_manifest_status_changed_between_passes_fails_reconciliation`, `test_find_rep_d_m1_complete_version_directory_or_source_file_removed_between_passes_fails_reconciliation`, `test_find_rep_d_m1_complete_io_error_during_reconciliation_fails_reconciliation`, `test_find_rep_d_valid_m1_complete_paper_passes_both_passes`, `test_find_rep_d_version_directory_on_disk_mismatch_fails_reconciliation` |
| **SEC3-01** | Medium | `except Exception: pass` khi đọc metadata | Bắt ngoại lệ cụ thể (`StorageError`, `IntegrityError`, `JSONDecodeError`, `OSError`) và ghi nhận trạng thái lỗi rõ ràng. | Toàn bộ suite |
| **SEC3-02** | Medium | Bổ sung và đồng bộ metrics phiên bản | Xuất `source_integrity_failed_versions` và đồng bộ `unresolved_versions` trong kết quả và đặc tả. | `test_source_integrity_failed_versions_counter` |
| **SEC3-03** | Medium | Ưu tiên repository chứa source code trong `_get_git_commit` | `_get_git_commit` tìm kiếm `PROJECT_ROOT` trước, sau đó mới dùng `cwd` làm fallback. | `test_git_commit_prioritizes_project_root_over_cwd` |

---

## 4. Các Phương Trình Đối Chiếu Đã Được Xác Minh (Reconciliation Proof)

Toàn bộ các phép tính thống kê đều được kiểm chứng độc lập và bảo đảm các bất đẳng thức số học:

1. **Cân bằng tập ứng viên phân bổ (Paper Population Balance):**
   $$\text{assigned\_candidates} = \text{found} + \text{confirmed\_not\_found} + \text{unprocessed} + \text{discovery\_failed} + \text{missing\_manifests} + \text{invalid\_manifests} + \text{other\_unresolved}$$
2. **Cân bằng vòng đời bài báo tìm thấy (Found Lifecycle Balance):**
   $$\text{found\_papers} = \text{m1\_complete\_papers} + \text{partial\_papers} + \text{failed\_papers} + \text{interrupted\_papers} + \text{unresolved\_found\_papers}$$
3. **Cân bằng phân loại phiên bản (Version State Balance):**
   $$\text{total\_discovered\_versions} = \text{terminal\_versions} + \text{incomplete\_versions} + \text{failed\_versions} + \text{invalid\_versions}$$
   $$\text{terminal\_versions} = \text{successful\_tex\_versions} + \text{no\_tex\_versions}$$
4. **Bất biến thứ bậc hoàn tất M1 (M1 Hierarchy Invariant):**
   $$\text{m1\_complete\_papers} \le \min(\text{source\_complete\_papers}, \text{metadata\_complete\_papers}, \text{references\_complete\_papers})$$

---

## 5. Bằng Chứng Kiểm Thử Đầy Đủ (Verification Evidence)

### 5.1. Kiểm thử chuyên biệt Phase 2
- `tests/test_reporting_correctness.py`: **31/31 tests passed** (Bao phủ paper-level, version-level, metadata, references, reconciliation, zero-denominator, git commit, JSON export, fault injection, và 15 tests remediation FIND-REP-A đến FIND-REP-D, bao gồm 8 test hook đột biến kiểm toán độc lập hai lượt).
- `tests/test_generate_m1_report.py`: **8/8 tests passed** (Bảo đảm tính tương thích ngược hoàn hảo).

### 5.2. Kiểm thử hồi quy hệ thống
- `tests/test_storage_integrity.py`: **35/35 tests passed**.
- `tests/test_fast_production.py`: **31/31 tests passed**.
- `tests/test_export.py`: **8/8 tests passed**.
- **Toàn bộ Test Suite:** **995 passed** trong 22.21 giây.
- **AST compile check:** 57 tệp Python hợp lệ cú pháp (100%).
- **Jupyter Notebook validation:** 4 sổ tay hợp lệ cấu trúc JSON (100%).
- **Định dạng code:** `git diff --check` sạch, không có lỗi khoảng trắng thừa.
- **Bảo toàn hằng số lịch sử:** `CODE_COMMIT = "489a82fd959ccc6cced7c59fc309255e4421bef2"` được giữ nguyên vẹn.

---

## 6. Giới Hạn Còn Lại & Khuyến Nghị (Limitations & Recommendation)

### 6.1. Giới hạn kỹ thuật còn lại
1. **Phụ thuộc vào Git CLI để lấy commit:** Khi thực thi trong môi trường tách rời (detached container) không có thư mục `.git`, provenance sẽ ghi nhận `UNKNOWN` kèm cảnh báo theo đúng thiết kế fail-safe.
2. **Authoritative arXiv Discovery Reconciliation:** Phase 2 chỉ thống kê và kiểm toán những gì đã được ghi nhận trong corpus hiện tại; việc đối chiếu toàn bộ dải ID phân bổ bằng truy vấn trực tiếp đến arXiv API thuộc về một phase nghiệp vụ riêng biệt.

### 6.2. Khuyến nghị nghiệm thu (Acceptance Recommendation)
Căn cứ kết quả kiểm toán, đối chiếu bất biến và bằng chứng kiểm thử 995/995 tests xanh tuyệt đối:

**Khuyến nghị: PASS** (Sẵn sàng nghiệm thu và đóng chính thức Phase 2 — Reporting Correctness).
Tuyệt đối không tự ý chuyển sang Phase 3 (Final Dataset Export) trước khi có chỉ thị nghiệm thu độc lập.
