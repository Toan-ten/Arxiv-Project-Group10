# Storage Contract & Data Integrity Audit — Phase 1

**Dự án:** Nhập môn Khoa học Dữ liệu — Đồ án Milestone 1  
**Repository:** `Toan-ten/Arxiv-Project-Group10`  
**Nhánh:** `feat/m1-t02-colab-resume`  
**Baseline commit:** `12ac338e3fe16aa505910f307a52b7ffb4cd6ee6`  
**Thời điểm hoàn tất audit:** 2026-11-09T16:00:00+07:00  

---

## 1. Kiến Trúc Lưu Trữ & Vị Trí Dữ Liệu Bền Vững (Storage Architecture)

Hệ thống lưu trữ của Milestone 1 tuân thủ nghiêm ngặt Hợp đồng lưu trữ v2 ([`docs/m1_contract.md`](file:///home/tuan-anh/Documents/DS_PJ/Arxiv-Project-Group10/docs/m1_contract.md) và [`src/storage.py`](file:///home/tuan-anh/Documents/DS_PJ/Arxiv-Project-Group10/src/storage.py)). Toàn bộ dữ liệu được quản lý theo mô hình phân cấp, bảo đảm nguyên tử hóa khi ghi (atomic replace) và cách ly vùng ghi:

### Các vị trí dữ liệu bền vững:
1. **Kho lưu trữ cục bộ (Local Corpus Root — SSD):** `<corpus_root>/<folder_key>/` (trong đó `<folder_key>` có định dạng `YYMM-NNNNN` chuẩn hóa từ base arXiv ID).
   - `manifest.json`: Điểm xác thực chân lý duy nhất (Single Source of Truth) điều phối toàn bộ pipeline, theo dõi trạng thái discovery, version, metadata, references và checksum của từng file mã nguồn.
   - `metadata.json`: Dữ liệu học thuật của bài báo (tiêu đề, tác giả, ngày nộp, ngày sửa đổi, venue, provenance discovery và version history).
   - `references.json`: Bản ghi trung gian đầy đủ của toàn bộ tài liệu tham khảo thu thập từ Semantic Scholar Graph API.
   - `v1/`, `v2/`, ...: Thư mục chứa mã nguồn trích xuất được xác thực (`.tex` và `.bib` nguyên bản từng byte).
2. **Kho lưu trữ sao lưu Drive (Drive Archives & Backups):**
   - `<drive_root>/archives/cohort_{number:04d}_corpus.tar.gz`: Tệp nén lưu trữ toàn bộ corpus của lô 100 bài.
   - `<drive_root>/archives/cohort_{number:04d}_corpus.tar.gz.sha256`: Mã băm SHA-256 xác thực tính toàn vẹn của tệp nén.
   - `<drive_root>/corpus/<folder_key>/`: Layout corpus cũ trên Drive (được tự động di trú an toàn).
3. **Nhật ký thực thi & Báo cáo hiệu năng (Execution Logs & Reports):**
   - `<drive_root>/logs/<batch_id>/batch.json`: Trạng thái và định nghĩa batch crawl.
   - `<drive_root>/logs/<batch_id>/attempts/*.jsonl`: Chi tiết từng lượt thử và giao vận HTTP.
   - `<drive_root>/reports/<experiment_id>/`: Dữ liệu đo đạc tài nguyên RAM/Disk và thời gian thực thi.
4. **Vùng đệm tạm thời (Scratch Workspace):**
   - `/dev/shm/arxiv_scratch` hoặc `<local_root>/scratch/`: Vùng đệm RAM disk / SSD tạm để giải nén và lọc ảnh trước khi chuyển nguyên tử vào corpus.

---

## 2. Các Bất Biến & Quy Tắc Xác Thực (Storage Contract Invariants)

Hệ thống thiết lập 8 nhóm bất biến bắt buộc:

1. **Bất biến đường dẫn và thư mục (Directory Safety):**
   - Tên thư mục bài báo bắt buộc phải là `arxiv_id_to_folder_key(arxiv_id)`.
   - Cấm tuyệt đối symlink, hardlink, junction hoặc path traversal (`..`) bên trong corpus.
   - Không chứa bất kỳ tệp lạ nào ngoài các tệp được quản lý: `manifest.json`, `metadata.json`, `references.json`, và các thư mục `vN/` hợp lệ.
2. **Bất biến định danh và quyền sở hữu (Ownership & Contract Version):**
   - Mọi tệp JSON bắt buộc có `contract_version == 2`.
   - `arxiv_id` và `mssv` phải khớp chính xác với cấu hình phân bổ `config/group10_id_ranges.json`.
3. **Bất biến đồng nhất liên tệp (Cross-File Consistency):**
   - `manifest["metadata_status"] == metadata["status"]`.
   - `manifest["references_status"] == references["status"]`.
   - `manifest["version_status"] == metadata["version_status"]`.
   - Danh sách phiên bản trong `manifest["versions"].keys()` phải trùng khớp 1:1 với `metadata["versions"]`.
4. **Bất biến mã nguồn và kiểm tra băm (Source Integrity & Checksums):**
   - Đối với phiên bản `status == "success"`: thư mục `vN/` bắt buộc phải tồn tại, chỉ chứa tệp thường `.tex` và `.bib`, phải có ít nhất một tệp `.tex`, và mã băm SHA-256 thực tế trên đĩa phải khớp 100% với `manifest["versions"][vN]["file_checksums"]`. Tổng dung lượng thực tế phải khớp `kept_bytes`.
   - Đối với phiên bản `status == "no_tex_source"`: nếu không có tệp nào thì cấm tạo thư mục `vN/`; nếu có tệp `.bib` thì thư mục chỉ chứa `.bib` và không có bất kỳ tệp `.tex` nào.
   - Đối với phiên bản chưa hoàn tất (`pending`, `in_progress`, `interrupted`, lỗi): cấm tồn tại thư mục mã nguồn chưa commit trên đĩa.
5. **Bất biến phân biệt trạng thái dữ liệu (Null vs Empty vs Missing):**
   - `None != []`: Không được đồng nhất giá trị chưa biết (`None`) với tập rỗng (`[]`).
   - Tác giả chưa rõ (`authors = None`) khác hoàn toàn bài báo không có tác giả (`authors = []`).
   - Lịch sử sửa đổi chưa rõ (`revised_dates = None`) khác hoàn toàn bài báo xác nhận không có sửa đổi (`revised_dates = []`).
6. **Bất biến tài liệu tham khảo (References Completeness):**
   - `references["status"] == "complete"` chỉ hợp lệ khi và chỉ khi `coverage["complete"] is True`, `continuation is None`, có `retrieved_at` hợp lệ và `query_identifier` xác định.
   - Trường hợp bài báo thực sự không có trích dẫn (`items = []`) kèm `coverage.complete = True` là một kết quả hoàn tất hợp lệ. Ngược lại, `items = []` kèm `coverage.complete = False` là kết quả dở dang (`incomplete`), không được coi là complete.
7. **Bất biến hoàn tất Milestone 1 (M1 Completeness):**
   - Một bài báo chỉ đạt `m1_complete` khi đồng thời: mã nguồn hoàn tất (`is_source_complete`), metadata hoàn tất (`metadata.status == "complete"` với ngày tháng đã xác minh), trích dẫn hoàn tất (`references.status == "complete"` và `coverage.complete == True`), và `manifest.status` thuộc `{"complete", "complete_with_no_tex"}`.
8. **Bất biến Resume và Idempotency:**
   - Quá trình chạy lại (resume) tuyệt đối không tải lại các phiên bản đã hoàn tất (`TERMINAL`) còn nguyên vẹn mã băm trên đĩa.
   - Bất kỳ gián đoạn nào (`in_progress`) được thu hồi an toàn về `interrupted` và không bao giờ đánh dấu nhầm dữ liệu dở dang thành hoàn tất.

---

## 3. Bảng Phát Hiện Chi Tiết (Confirmed Findings & Gaps)

| ID | Tiêu đề phát hiện | Mức độ | Tệp bị ảnh hưởng | Kịch bản lỗi thực tế | Biện pháp xử lý & Kiểm thử |
|---|---|:---:|---|---|---|
| **STO-01** | Thiếu API kiểm toán tính toàn vẹn thống nhất | **P1 (Major)** | `src/storage.py`, `src/storage_integrity.py` | Báo cáo, runner và exporter tự triển khai các logic kiểm tra rời rạc, dẫn đến sai lệch trong việc định nghĩa bài báo hoàn tất. | Xây dựng module [`src/storage_integrity.py`](file:///home/tuan-anh/Documents/DS_PJ/Arxiv-Project-Group10/src/storage_integrity.py) cung cấp `audit_paper_integrity`, `audit_version_integrity`, `audit_cohort_integrity`. Tích hợp trực tiếp vào `CorpusStorage`. |
| **STO-02** | Không phát hiện tệp rác / tệp không quản lý trong thư mục bài báo | **P1 (Major)** | `src/storage.py`, storage contract | Các tệp tạm (`.manifest.json.tmp`), tệp rác hoặc thư mục phiên bản không đăng ký (`v99/`) tồn tại trong corpus nhưng không bị phát hiện hay cảnh báo. | Bổ sung cơ chế quét danh mục trong `audit_paper_integrity`, gán nhãn vi phạm `category="unmanaged"` nếu phát hiện tệp ngoài quy định hợp đồng. |
| **STO-03** | `count_completed_papers` chỉ kiểm tra JSON mà không xác minh mã nguồn trên đĩa | **P1 (Major)** | `src/fast_production.py:168` | Nếu tệp `main.tex` trên đĩa bị xóa hoặc hỏng, hàm đếm vẫn báo 100/100 hoàn tất dựa trên manifest thô, khiến runner bỏ qua việc khôi phục từ Drive. | Cập nhật `count_completed_papers` kiểm tra sự tồn tại của thư mục phiên bản và tệp mã nguồn tương ứng trên đĩa đối với mọi bài báo `status="success"`. |
| **STO-04** | Làm méo mó ngữ nghĩa dữ liệu khi biến `None` thành `[]` trong bảng papers | **P1 (Major)** | `src/export.py:30-32` | `meta.get("authors") or []` biến trường `authors` chưa thu thập (`None`) thành danh sách rỗng (`[]`), làm mất phân biệt giữa thiếu dữ liệu và không có tác giả. | Chuẩn hóa `export_papers_table` bảo toàn giá trị `None` khi dữ liệu gốc là `None`, bổ sung cờ `verified_only=True` để chỉ xuất bài báo đạt chuẩn. |
| **STO-05** | Xuất trích dẫn dở dang / chưa hoàn tất vào bảng references | **P0 (Critical)** | `src/export.py:58-77` | `export_references_table` duyệt qua mọi `items` mà không kiểm tra `ref_data["status"] == "complete"` hay `coverage.complete is True`, khiến dữ liệu partial bị lọt vào bảng xuất bản. | Bổ sung kiểm tra nghiêm ngặt `verified_only=True`: chỉ xuất bản ghi trích dẫn khi bài báo đạt `status == "complete"` VÀ `coverage.complete is True`. |
| **STO-06** | Mơ hồ giữa tập trích dẫn rỗng hợp lệ và dữ liệu trích dẫn dở dang | **P2 (Moderate)** | `src/export.py`, `scripts/generate_m1_report.py` | Khi API trả về 0 trích dẫn hợp lệ (`items = []`, `coverage.complete = True`), một số bộ lọc nhầm lẫn với bài báo chưa được cào trích dẫn. | Định nghĩa bất biến tường minh trong Storage Contract: `items = []` kèm `coverage.complete = True` là kết quả hoàn tất hợp lệ. |

---

## 4. Giao Diện API Kiểm Toán Tính Toàn Vẹn Mới (Integrity Audit API)

Module [`src/storage_integrity.py`](file:///home/tuan-anh/Documents/DS_PJ/Arxiv-Project-Group10/src/storage_integrity.py) cung cấp ba phương thức chính:

1. **`audit_version_integrity(paper_dir, arxiv_id, version_name, record)`:**
   - Xác minh thư mục `vN/`.
   - Xác minh 100% tệp trên đĩa là `.tex` hoặc `.bib`.
   - Xác minh mã băm SHA-256 từng tệp so với `file_checksums`.
   - Xác minh tổng dung lượng so với `kept_bytes`.
   - Kiểm tra bất biến cấm thư mục đối với `no_tex_source` không có tệp.
2. **`audit_paper_integrity(store, mssv, arxiv_id)`:**
   - Trả về đối tượng `PaperIntegrityResult` chứa:
     + `status`: Phân loại chính xác thành `valid`, `invalid`, `missing`, `incomplete`, `corrupted`.
     + `is_source_complete`: Cờ xác nhận hoàn tất mã nguồn có kiểm tra SHA-256.
     + `is_m1_complete`: Cờ xác nhận hoàn tất toàn diện mọi tiêu chí M1.
     + `violations`: Danh sách chi tiết các vi phạm (kèm `category`, `expected`, `actual`, `message`).
3. **`audit_cohort_integrity(store, pairs, cohort_number=None)`:**
   - Duyệt qua toàn bộ danh sách phân công, tổng hợp báo cáo `CohortIntegrityReport`.
   - Cung cấp hàm định dạng báo cáo Markdown: `format_integrity_report_markdown(report)`.

---

## 5. Kết Quả Kiểm Thử (Verification Evidence)

Đã xây dựng bộ kiểm thử chuyên biệt [`tests/test_storage_integrity.py`](file:///home/tuan-anh/Documents/DS_PJ/Arxiv-Project-Group10/tests/test_storage_integrity.py) với 13 ca kiểm thử bao phủ toàn bộ các kịch bản lỗi và bất biến:

```text
$ python3 -m pytest tests/test_storage_integrity.py -v
============================= test session starts ==============================
tests/test_storage_integrity.py::TestStorageIntegrity::test_audit_cohort_integrity_and_report_generation PASSED [  7%]
tests/test_storage_integrity.py::TestStorageIntegrity::test_audit_corrupted_json PASSED [ 15%]
tests/test_storage_integrity.py::TestStorageIntegrity::test_audit_cross_file_status_mismatch PASSED [ 23%]
tests/test_storage_integrity.py::TestStorageIntegrity::test_audit_forbidden_extensions_in_source PASSED [ 30%]
tests/test_storage_integrity.py::TestStorageIntegrity::test_audit_missing_mandatory_jsons PASSED [ 38%]
tests/test_storage_integrity.py::TestStorageIntegrity::test_audit_missing_paper_directory PASSED [ 46%]
tests/test_storage_integrity.py::TestStorageIntegrity::test_audit_no_tex_source_semantics PASSED [ 53%]
tests/test_storage_integrity.py::TestStorageIntegrity::test_audit_references_coverage_semantics PASSED [ 61%]
tests/test_storage_integrity.py::TestStorageIntegrity::test_audit_source_checksum_verification PASSED [ 69%]
tests/test_storage_integrity.py::TestStorageIntegrity::test_audit_unmanaged_extraneous_files_in_paper_dir PASSED [ 76%]
tests/test_storage_integrity.py::TestStorageIntegrity::test_audit_unmanaged_extraneous_files_in_version_dir PASSED [ 84%]
tests/test_storage_integrity.py::TestStorageIntegrity::test_count_completed_papers_verifies_disk_source PASSED [ 92%]
tests/test_storage_integrity.py::TestStorageIntegrity::test_export_verified_only_enforcement PASSED [100%]
============================== 13 passed in 0.28s ==============================
```

Toàn bộ các bài kiểm thử liên quan (`test_storage.py`, `test_export.py`, `test_fast_production.py`) đều vượt qua 100% không phát sinh hồi quy.
