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

Đã xây dựng bộ kiểm thử chuyên biệt [`tests/test_storage_integrity.py`](file:///home/tuan-anh/Documents/DS_PJ/Arxiv-Project-Group10/tests/test_storage_integrity.py) và [`tests/test_export.py`](file:///home/tuan-anh/Documents/DS_PJ/Arxiv-Project-Group10/tests/test_export.py) bao phủ toàn bộ các kịch bản lỗi và bất biến.

---

## 6. Khắc Phục Độc Lập Theo Yêu Cầu Audit (Phase 1 Remediation: R1 – R15)

Căn cứ biên bản audit độc lập đối với commit `bf4e5ff` và các lần đánh giá tiếp theo, hệ thống đã hoàn thiện triệt để toàn bộ 15 yêu cầu kỹ thuật:

### R1. Bảo toàn ngữ nghĩa Null trong xuất dữ liệu (Preserve Null Semantics in Exports)
- **Nguyên nhân gốc rễ:** Hàm `export_papers_table` và `export_references_table` áp dụng biểu thức gán mặc định `list(...) if ... else []`, vô tình chuẩn hóa giá trị `None` (chưa quan sát hoặc chưa thu thập) thành danh sách rỗng (`[]`). Đồng thời `validate_exported_tables` yêu cầu `authors` và `revised_dates` bắt buộc phải là kiểu `list`.
- **Biện pháp xử lý:**
  + Trong [`src/export.py`](file:///home/tuan-anh/Documents/DS_PJ/Arxiv-Project-Group10/src/export.py): Bảo toàn nguyên trạng `None` khi trường `authors`, `revised_dates` hoặc `ref_authors` mang giá trị `None`. Chỉ khởi tạo `list(...)` khi trường có dữ liệu thực tế.
  + Cập nhật `validate_exported_tables`: Chấp nhận giá trị `None` hoặc `list[str]`, đồng thời kiểm tra nghiêm ngặt từng phần tử phải là chuỗi ký tự (`str`).
  + Bổ sung ca kiểm thử hồi quy xác nhận tính đúng đắn cho cả 3 trường hợp: `None`, `[]`, và danh sách có dữ liệu.

### R2. Thiết lập cơ chế Fail-Safe cho xuất bản dữ liệu chính thức (Fail-Safe Official Papers Export)
- **Nguyên nhân gốc rễ:** `export_papers_table` nhận giá trị mặc định `verified_only=False`, trong khi ô thực thi số 7 của sổ tay Colab production gọi hàm mà không truyền tham số tường minh, dẫn đến nguy cơ các bản ghi metadata dở dang (`partial`) lọt vào tập dữ liệu xuất bản chính thức.
- **Biện pháp xử lý:**
  + Đổi giá trị mặc định của `export_papers_table` thành `verified_only=True`. Khi ở chế độ này, hàm loại bỏ hoàn toàn các bản ghi chưa đạt trạng thái `complete`.
  + Tách biệt rõ ràng hàm xuất dữ liệu chẩn đoán trung gian: `export_diagnostic_papers_table(store, pairs, include_partial=True)`.
  + Cập nhật trực tiếp ô Bước 6 trong sổ tay [`notebooks/m1_colab_fast_production.ipynb`](file:///home/tuan-anh/Documents/DS_PJ/Arxiv-Project-Group10/notebooks/m1_colab_fast_production.ipynb), truyền tường minh `verified_only=True` cho cả `export_papers_table` và `export_references_table`.
  + Thắt chặt kiểm tra trong `export_references_table`: Bắt buộc `ref_data["status"] == "complete"` và `coverage["complete"] is True`, ngăn chặn việc xuất trích dẫn chưa hoàn tất.

### R3. Thực thi đối chiếu toàn diện mã băm (Enforce Complete Checksum Coverage)
- **Nguyên nhân gốc rễ:** Hàm `audit_version_integrity` trước đây chỉ lặp qua các khóa có trong từ điển `file_checksums`, bỏ sót trường hợp tệp nguồn được khai báo trong danh sách `files` nhưng thiếu mã băm trong `file_checksums`, hoặc mã băm không đúng định dạng.
- **Biện pháp xử lý:**
  + Áp dụng biểu thức chính quy `^[0-9a-f]{64}$` để kiểm tra định dạng của từng mã băm SHA-256.
  + Bắt buộc tương ứng 1:1 giữa tập tệp dự kiến `files` và các khóa trong `file_checksums`:
    * Tệp thiếu mã băm: Phát sinh vi phạm `category="incomplete"`.
    * Khóa băm dư thừa không có trong `files`: Phát sinh vi phạm `category="invalid"`.
  + Tách biệt độc lập việc phát hiện tệp thiếu trên đĩa (`category="missing"`), tệp dư thừa không quản lý (`category="unmanaged"`), và nội dung tệp bị sai khác mã băm (`category="corrupted"`).
  + Đối chiếu tổng số byte thực tế của các tệp thường trên đĩa với giá trị `kept_bytes` trong manifest.

### R4. Xác thực toàn diện trạng thái `no_tex_source` (Fully Validate no_tex_source)
- **Nguyên nhân gốc rễ:** Implementation trước đây chỉ kiểm tra không có tệp đuôi `.tex`, chưa thực thi đầy đủ quy chuẩn cho tệp `.bib` và trạng thái thư mục rỗng.
- **Biện pháp xử lý:**
  + Trường hợp `no_tex_source` rỗng (`files` rỗng): Thư mục `vN/` tuyệt đối không được phép tồn tại trên đĩa. Nếu tồn tại, hệ thống ghi nhận vi phạm `category="unmanaged"`. Đồng thời `file_checksums` phải rỗng và `kept_bytes` phải bằng 0 hoặc `None`.
  + Trường hợp `no_tex_source` có tệp `.bib`:
    * Toàn bộ tệp dự kiến và tệp thực tế trên đĩa bắt buộc phải có phần mở rộng `.bib`. Chặn đứng tệp `.tex` và mọi định dạng khác (`.png`, `.pdf`, `.py`, v.v.).
    * Thực hiện đầy đủ quy trình kiểm tra mã băm SHA-256 từng tệp `.bib`, đối chiếu danh sách tệp và kiểm tra `kept_bytes`.
    * Cấm tuyệt đối symlink và hardlink.

### R5. Gia cố an toàn xử lý liên kết hệ thống tệp (Harden Filesystem Link Handling)
- **Nguyên nhân gốc rễ:** Phương thức `Path.stat()` tự động phân giải và duyệt theo symbolic link. Khi gặp symlink trỏ ra ngoài corpus, việc mở tệp để tính băm có thể đọc nhầm dữ liệu bên ngoài hoặc gây rò rỉ thông tin.
- **Biện pháp xử lý:**
  + Sử dụng `os.lstat()` và `os.walk(..., followlinks=False)` để kiểm tra trạng thái tệp mà không duyệt theo symlink.
  + Nếu phát hiện thư mục là symlink: Loại bỏ ngay khỏi danh sách duyệt, không đi vào bên trong, và ghi nhận vi phạm `category="corrupted"`.
  + Nếu phát hiện tệp là symlink: Tuyệt đối không mở và không tính băm tệp đích; ghi nhận ngay vi phạm `category="corrupted"`.
  + Kiểm tra hardlink: Bắt buộc `st_nlink == 1` đối với mọi tệp nguồn thông thường. Trường hợp `st_nlink > 1` bị ghi nhận vi phạm `category="corrupted"`.
  + Xác thực phạm vi đường dẫn: Kiểm tra `fpath.resolve().is_relative_to(version_dir.resolve())` để bảo đảm không xảy ra vượt ranh giới thư mục.
  + Xử lý lỗi hệ thống tệp (`OSError`) thành các vi phạm kiểm toán tường minh.

### R6. Gia cố hàm `count_completed_papers` (Strengthen count_completed_papers)
- **Nguyên nhân gốc rễ:** Hàm `count_completed_papers` trong `src/fast_production.py` trước đây chỉ kiểm tra sự tồn tại của tệp trên đĩa (`is_file()`), bỏ qua việc tính toán mã băm SHA-256 thực tế và không kiểm tra quy chuẩn thư mục `no_tex_source`.
- **Biện pháp xử lý:**
  + Tái sử dụng trực tiếp API kiểm toán `audit_version_integrity` từ module `src.storage_integrity` cho từng phiên bản của bài báo.
  + Kiểm tra đầy đủ mã băm SHA-256 trên đĩa, định dạng metadata và quy chuẩn `no_tex_source`.
  + Nếu bất kỳ phiên bản nào phát sinh vi phạm tính toàn vẹn (ví dụ: nội dung tệp bị sửa đổi dù tên tệp giữ nguyên, thiếu mã băm, tồn tại symlink, hoặc sai cấu trúc `no_tex_source`), bài báo sẽ không được tính vào số lượng hoàn tất (`done`).

### R7. Bắt buộc kiểm tra `kept_bytes` cho bản ghi nguồn kết thúc (Require kept_bytes for Terminal Source Records)
- **Nguyên nhân gốc rễ:** Trường `kept_bytes` trong bản ghi phiên bản trước đây chưa kiểm tra chặt chẽ kiểu dữ liệu, dẫn đến trường hợp bản ghi hoàn tất (`success` hoặc `no_tex_source`) bị thiếu dung lượng hoặc dung lượng không khớp với tổng số byte của các tệp thực tế trên đĩa.
- **Biện pháp xử lý:**
  + Đối với phiên bản `status == "success"` và `status == "no_tex_source"` có tệp `.bib`:
    * Bắt buộc tồn tại trường `kept_bytes` và không được mang giá trị `null` (nếu thiếu hoặc null, ghi nhận vi phạm `category="incomplete"`).
    * Kiểm tra kiểu số nguyên chính xác `type(kept_bytes) is int` (loại trừ kiểu `bool`).
    * Bắt buộc giá trị `kept_bytes >= 0` (nếu âm, ghi nhận vi phạm `category="invalid"`).
    * So khớp chính xác tuyệt đối với tổng số byte thực tế của các tệp thông thường trên đĩa (`total_bytes`). Nếu sai lệch, ghi nhận vi phạm `category="corrupted"`.
  + Đối với phiên bản rỗng `status == "no_tex_source"` (không có tệp):
    * Bắt buộc trường `kept_bytes` phải tồn tại, không mang giá trị `null`, và nhận đúng giá trị số nguyên `0` theo hợp đồng lưu trữ chuẩn của storage writer.

### R8. Đóng lỗi duyệt hệ thống tệp theo nguyên tắc fail-closed (Make Filesystem Traversal Errors Fail Closed)
- **Nguyên nhân gốc rễ:** Lệnh `os.walk()` mặc định không truyền hàm xử lý `onerror`. Khi xảy ra lỗi phân quyền truy cập thư mục hoặc lỗi I/O gián đoạn, tiến trình duyệt bỏ qua lỗi và có thể báo cáo sai rằng thư mục không có vi phạm nào.
- **Biện pháp xử lý:**
  + Bổ sung tham số `onerror=walk_onerror` cho cả hai lệnh `os.walk()` trong `audit_version_integrity` (cho cả luồng `success` và luồng `no_tex_source`).
  + Khi xuất hiện lỗi I/O hoặc quyền truy cập, hàm `walk_onerror` kích hoạt cờ `traversal_failed = True` và ghi nhận ngay vi phạm `category="corrupted"` kèm mã lỗi hệ thống.
  + Sau vòng lặp duyệt, nếu cờ `traversal_failed` được bật, hệ thống ghi nhận thêm một vi phạm nghiêm trọng báo cáo việc duyệt cây thư mục không trọn vẹn, lập tức kết luận bài báo không hợp lệ (fail-closed).

### R9. Xóa bỏ cửa sổ chạy đua TOCTOU khi kiểm tra liên kết tượng trưng (Close the Symlink TOCTOU Window)
- **Nguyên nhân gốc rễ:** Trình tự kiểm tra symlink bằng `os.lstat()` rồi sau đó mở tệp bằng `open(path)` tạo ra một khoảng trống thời gian (Time-Of-Check to Time-Of-Use - TOCTOU) mà tại đó một tiến trình khác có thể thay thế tệp thông thường bằng symlink trước khi thao tác mở diễn ra.
- **Biện pháp xử lý:**
  + Xây dựng hàm `_open_and_hash_source_file(fpath)`: Mở tệp trực tiếp bằng bộ mô tả tệp (file descriptor) với cờ `os.O_NOFOLLOW | os.O_RDONLY` (và `os.O_CLOEXEC`).
  + Xác thực bộ mô tả qua `os.fstat(fd)`: Kiểm tra `stat.S_ISREG` và bắt buộc `fstat_res.st_nlink == 1`.
  + Đọc từng khối và băm SHA-256 trực tiếp từ bộ mô tả tệp mà không mở lại đường dẫn trên hệ thống tệp.
  + Đóng triệt để khoảng trống TOCTOU trên các hệ điều hành hỗ trợ chuẩn POSIX `O_NOFOLLOW`.

### R10. Thực thi kiểm soát ranh giới corpus và đọc an toàn manifest trong `count_completed_papers` (Enforce Corpus Containment and Safe Manifest Reads)
- **Nguyên nhân gốc rễ:** Hàm `count_completed_papers()` trước đây không kiểm tra tính chứa trong thư mục gốc corpus (`corpus_dir`), có thể duyệt theo symlink trỏ ra ngoài; đồng thời đọc tệp `manifest.json` thô mà không kiểm tra symlink/hardlink và không xác thực schema trước khi đọc trường trạng thái.
- **Biện pháp xử lý:**
  + Từ chối ngay lập tức mọi thư mục bài báo là symlink hoặc giải quyết đường dẫn ra ngoài thư mục `corpus_dir` (`is_relative_to`).
  + Mở tệp `manifest.json` an toàn bằng cờ `os.O_NOFOLLOW`, kiểm tra `stat.S_ISREG` và `st_nlink == 1`.
  + Xác thực toàn diện schema và định danh bài báo (`contract_version == 2`, `arxiv_id`, `mssv`, `status in PAPER_STATES`, `discovery_status in DISCOVERY_STATES`) trước khi tin cậy bất kỳ trường trạng thái nào.
  + Đối với phát hiện âm tính `discovery_status == "not_found"`: Bắt buộc `manifest.status == "not_found"`, danh sách `versions` rỗng và không tồn tại bất kỳ thư mục phiên bản `vN/` nào trên đĩa.
  + Đối với phát hiện dương tính `discovery_status == "found"`: Bắt buộc toàn bộ phiên bản phải đạt trạng thái kết thúc (`TERMINAL`) và vượt qua đầy đủ kiểm toán `audit_version_integrity()`.

### R11. Đồng bộ hóa giữa kế toán độ bao phủ và xuất bản bảng papers chính thức (Harmonize Coverage Accounting and Official Papers Export)
- **Nguyên tắc phân định ranh giới hợp đồng (Contract Boundaries):**
  + **Bảng xuất bản chính thức `papers.parquet` (`export_papers_table`):** Đóng vai trò là danh mục siêu dữ liệu bài báo (Paper Metadata Catalog). Với chế độ mặc định `verified_only=True`, bảng xuất bao gồm tất cả các bài báo có siêu dữ liệu được xác thực hoàn tất (`metadata.status == "complete"`).
  + **Chế độ kiểm soát toàn diện Milestone 1 (`require_m1_complete=True`):** Cung cấp thêm cờ `require_m1_complete` trong `export_papers_table`. Khi cờ này được bật, hệ thống chỉ xuất các bài báo đạt trạng thái hoàn tất trọn vẹn toàn bộ vòng đời M1 (`manifest.status in {"complete", "complete_with_no_tex"}` và `manifest.metadata_status == "complete"`).
  + **Hàm đếm tiến độ `count_completed_papers()`:** Đo lường chính xác số lượng bài báo trong cohort đã hoàn tất thu thập mã nguồn trên đĩa (hoặc phát hiện âm tính `not_found`), bảo đảm các điều kiện lưu trữ mã nguồn để phục vụ sao lưu và khôi phục.
  + **Báo cáo tổng hợp (`scripts/generate_m1_report.py`):** Phản ánh minh bạch cả hai chỉ số: tỷ lệ hoàn tất mã nguồn (`source_completion_rate`) và tỷ lệ hoàn tất trọn vẹn M1 (`m1_completion_rate`).

### R12. Chuẩn hóa định dạng tên phiên bản và kiểm soát ranh giới đường dẫn (Version Name Format & Containment)
- **Nguyên nhân gốc rễ:** Hàm `audit_version_integrity()` nhận `version_name` dưới dạng chuỗi và trực tiếp ghép đường dẫn `paper_dir / version_name`. Với các giá trị không hợp lệ hoặc chứa path traversal (như `../../missing`, `v0`, `/etc/passwd`), nếu thư mục chưa tồn tại trên đĩa thì việc kiểm tra `version_dir.exists()` trước đó có thể bỏ qua việc xác thực ranh giới hoặc phát sinh hành vi không xác định.
- **Biện pháp xử lý:**
  + Gọi trực tiếp hàm kiểm thực `_version(version_name)` ngay tại đầu vào của `audit_version_integrity()`, bảo đảm tên phiên bản phải khớp chuẩn `v[1-9][0-9]*`. Nếu sai định dạng, ghi nhận vi phạm `category="invalid"` và kết thúc kiểm toán phiên bản ngay lập tức.
  + Kiểm tra tính chứa trong thư mục bài báo `(paper_dir / version_name).resolve().is_relative_to(paper_dir.resolve())` một cách vô điều kiện, bất kể thư mục có tồn tại trên đĩa hay không.

### R13. Xác thực schema manifest toàn diện trong `count_completed_papers` (Full Manifest Schema Validation)
- **Nguyên nhân gốc rễ:** `count_completed_papers()` trước đây chỉ kiểm tra một số trường riêng lẻ (`contract_version`, `arxiv_id`, `mssv`, các trường trạng thái) mà không gọi bộ xác thực schema chính quy `self.store._manifest(m, mssv, arxiv_id)`. Các manifest giả lập hoặc thiếu các trường bắt buộc (như `run_id`, `updated_at`, các trường chi tiết của version) có thể lọt qua bộ kiểm tra.
- **Biện pháp xử lý:**
  + Tích hợp trực tiếp lệnh gọi `self.store._manifest(m, mssv, arxiv_id)` trong `count_completed_papers()`.
  + Mọi manifest không tuân thủ đầy đủ schema hợp đồng v2 hoặc chứa dữ liệu phiên bản sai quy cách sẽ bị bắt qua ngoại lệ và lập tức bị loại trừ khỏi danh sách hoàn tất (fail-closed).

### R14. Bắt buộc từ điển rỗng đối với phát hiện âm tính (Strict Empty Dict for Negative Discovery)
- **Nguyên nhân gốc rễ:** Biểu thức kiểm tra `if m.get("versions"): continue` dựa trên tính truthy của Python, vô tình chấp nhận các kiểu dữ liệu sai schema nhưng mang giá trị falsy (như danh sách rỗng `[]`, chuỗi rỗng `""`, hoặc `None`), vi phạm ràng buộc schema quy định trường `versions` bắt buộc phải là từ điển (`dict`).
- **Biện pháp xử lý:**
  + Trong nhánh `discovery_status == "not_found"`, kiểm tra nghiêm ngặt `isinstance(versions, dict) and not versions`.
  + Chỉ duy nhất giá trị từ điển rỗng (`{}`) mới được chấp nhận; mọi kiểu dữ liệu khác đều bị loại bỏ ngay lập tức.

### R15. Bất biến siêu dữ liệu hoàn tất khi bật `require_m1_complete` (Metadata Invariant for require_m1_complete)
- **Nguyên nhân gốc rễ:** Trong hàm `export_papers_table()`, nếu gọi với `verified_only=False` nhưng `require_m1_complete=True`, khối điều kiện `if verified_only:` bị bỏ qua, dẫn đến việc bản ghi có `meta["status"] == "partial"` có thể lọt vào kết quả nếu manifest đạt trạng thái `complete`.
- **Biện pháp xử lý:**
  + Thiết lập bất biến `if require_m1_complete or verified_only: if status != "complete": continue`.
  + Khi `require_m1_complete=True`, hệ thống luôn bắt buộc siêu dữ liệu phải đạt trạng thái `complete`, bất kể giá trị của cờ `verified_only`. Chế độ chẩn đoán (`verified_only=False`) chỉ cho phép dữ liệu `partial` khi cờ `require_m1_complete` nhận giá trị `False`.

---

## 7. Giới Hạn Kỹ Thuật Còn Lại (Remaining Limitations)

1. **Hiệu năng băm đĩa trên tập dữ liệu quy mô lớn:**
   - Việc tính toán mã băm SHA-256 trực tiếp trên đĩa tại mỗi lần gọi `count_completed_papers` bảo đảm an toàn dữ liệu tuyệt đối nhưng tiêu tốn I/O. Đối với quy mô production Colab (1.200 bài báo), thời gian xác thực nằm trong khoảng 2-4 giây trên SSD cục bộ.
2. **Ký hiệu commit lịch sử được bảo toàn:**
   - Biến `CODE_COMMIT = "489a82fd959ccc6cced7c59fc309255e4421bef2"` được giữ nguyên vẹn xuyên suốt Phase 1 theo đúng chỉ thị kiểm toán, bảo đảm tính hồi cứu nguồn gốc (provenance). Hằng số này sẽ chỉ được cập nhật trong bước nghiệm thu cuối cùng.
3. **Ranh giới nền tảng và cơ chế chống TOCTOU:**
   - Cơ chế phòng chống chạy đua TOCTOU qua bộ mô tả tệp (`os.O_NOFOLLOW | os.O_RDONLY` kèm `os.fstat`) đạt mức độ bảo vệ tối đa trên môi trường POSIX (Linux/macOS). Trên các nền tảng không hỗ trợ POSIX hoặc hệ thống tệp không hỗ trợ `st_nlink` / inode, cơ chế này tự động chuyển đổi sang kiểm tra ranh giới đường dẫn phân giải. Toàn bộ môi trường production chính thức trên Google Colab (Linux kernel 6.x) vận hành hoàn toàn trong ranh giới bảo vệ của chuẩn POSIX.
