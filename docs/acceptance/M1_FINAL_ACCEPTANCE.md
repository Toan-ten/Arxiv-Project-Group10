# BÁO CÁO NGHIỆM THU TỔNG THỂ MILESTONE 1 (M1 FINAL ACCEPTANCE REPORT)
## Dự án: Arxiv-Project-Group10 — Nhóm 10

---

### 1. Bối cảnh & Mục tiêu Nghiệm thu

Báo cáo này tổng hợp kết quả thẩm định, chuẩn hóa và đóng gói hoàn thiện Milestone 1 (M1) cho Đồ án Khoa học Dữ liệu theo đúng Đề bài yêu cầu (`request/crawling.md`, `request/index.md`), Storage Contract v2 (`docs/m1_contract.md`), và Báo cáo Kiểm toán (`docs/reporting_metrics.md`).

- **Repository:** `Toan-ten/Arxiv-Project-Group10`
- **Nhánh thực hiện:** `feat/m1-t02-colab-resume`
- **Baseline bắt đầu:** `b3559ddd92c00b5ff1c7d38650528d4cb3fa1ec0`
- **Mã commit thực thi chính thức (`EXECUTION_COMMIT`):** `69b4b5e758f74d255ff244c56271e294da57b8e4`
- **Mốc lịch sử đối chiếu (`CODE_COMMIT`):** `489a82fd959ccc6cced7c59fc309255e4421bef2`
- **Phạm vi (Scope Boundary):** Tuyệt đối giới hạn trong Milestone 1. Không đưa vào các thành phần phân tích ngữ nghĩa M2/M3, không can thiệp nội dung tệp LaTeX/hình ảnh, không triển khai cơ sở dữ liệu ngoài hay giao diện web không được yêu cầu.

---

### 2. Ma trận Đối chiếu Yêu cầu Kỹ thuật (Traceability Matrix)

| Mã Yêu cầu | Nội dung Yêu cầu theo Đề bài | Trạng thái | Bằng chứng Kiểm chứng Thực tế |
| :---: | :--- | :---: | :--- |
| **REQ-01** | Xác định và phân giải chính xác 20.000 định danh bài báo arXiv được giao cho 4 thành viên. | **PASS** | Cấu hình `config/group10_id_ranges.json` chứa 4 sinh viên, mỗi người 5.000 IDs / 50 lô, tổng cộng 200 lô / 20.000 IDs không trùng lặp, không khoảng trống. Hàm `member_cohort_range` và kiểm thử `tests/test_id_ranges.py` đạt 100%. |
| **REQ-02** | Khám phá (Discovery) đầy đủ các phiên bản (`v1`, `v2`, ...) và lịch sử phiên bản của từng bài báo. | **PASS** | Hỗ trợ truy vấn arXiv Atom API (`arxiv_discovery.py`, `arxiv_history.py`) và nạp trước qua snapshot Kaggle Cornell (`kaggle_discovery.py`). Cờ `revision_history=True` mặc định trong `FastProductionRunner.run_cohort`. |
| **REQ-03** | Tải toàn bộ mã nguồn TeX/LaTeX của mọi phiên bản tìm thấy dưới dạng giải nén nguyên bản. | **PASS** | `arxiv_source.py` tải tệp nén e-print từ arXiv, tự động giải nén và lưu trữ cấu trúc thư mục phẳng/phân cấp nguyên vẹn trong `v1/`, `v2/`. Tuyệt đối không xóa hay sửa đổi các câu lệnh chèn hình ảnh trong LaTeX. |
| **REQ-04** | Lọc an toàn tệp TeX/Bib, loại bỏ binary/unsafe files, ngăn chặn path traversal và symlinks. | **PASS** | `storage_integrity.py` và `storage.py` lọc bỏ tệp thực thi, tệp đặc biệt; kiểm tra ngặt nghèo `_inspect_path` ngăn symlink escaping, symlink loops và directory traversal. |
| **REQ-05** | Tính toán mã băm mật mã SHA-256 cho từng tệp và ghi nhận toàn vẹn trong `manifest.json`. | **PASS** | Hợp đồng lưu trữ Storage Contract v2 (`m1_contract.md`) yêu cầu `file_checksums` SHA-256 cho từng tệp được lưu giữ. Đọc tệp an toàn qua file descriptor (`_read_bytes_safely`) ngăn chặn tấn công TOCTOU. |
| **REQ-06** | Phân loại trạng thái bài báo rõ ràng: Không nhầm lẫn giữa Discovery, Source và M1 Completion. | **PASS** | Tách bạch `discovery_status` (`found`, `not_found`), `source_status` (`success`, `no_tex_source`), và `status` (`complete`, `failed`). Không tính bài partial/failed vào số lượng hoàn thành. |
| **REQ-07** | Khả năng tiếp tục (Resume) an toàn và tính lũy đẳng (Idempotency) sau khi gián đoạn phiên. | **PASS** | Tự động đồng bộ archive `.tar.gz` kèm `.sha256` và `.manifest.json` lên Google Drive. Hàm `restore_cohort` kiểm tra chéo danh mục và mã băm trước khi giải nén phục hồi vào SSD. |
| **REQ-08** | Làm giàu siêu dữ liệu và trích dẫn bài báo qua Semantic Scholar (S2 Enrichment). | **PASS** | Triển khai hàm `enrich_cohort_s2` chuyên biệt, chỉ duyệt qua các bài báo đã hoàn tất mã nguồn, gọi trực tiếp `pipeline._run_s2`, hoàn toàn không gọi arXiv discovery hay downloader. |
| **REQ-09** | Báo cáo kiểm toán độc lập với cơ chế đối chiếu fail-closed nghiêm ngặt. | **PASS** | `scripts/generate_m1_report.py` và `src/reporting.py` thực hiện 2 lượt kiểm toán độc lập (`_perform_independent_reconciliation`). Mọi lỗi truy cập tệp hoặc không khớp chỉ số đều kích hoạt fail-closed (`is_reconciled=False`). |
| **REQ-10** | Sổ tay vận hành Google Colab chuẩn xác, phân quyền và hướng dẫn rõ ràng cho 4 thành viên. | **PASS** | `notebooks/m1_colab_fast_production.ipynb` và tài liệu hướng dẫn `docs/M1_HANDOFF.md` hỗ trợ chọn MSSV và chế độ `PILOT` vs `MEMBER_PRODUCTION`. |
| **REQ-11** | Tiện ích xuất dữ liệu bảng Parquet/CSV tùy chọn, Raw Corpus là chân lý duy nhất. | **PASS** | `src/export.py` xuất bảng tiện ích `papers` và `references` dạng Parquet/CSV, được cô lập an toàn trong khối try/except, không bao giờ làm gián đoạn hay ảnh hưởng tới Raw Corpus. |

---

### 3. Kết quả Xử lý Chi tiết 8 Điểm phát hiện (Findings F-01 .. F-08)

#### F-01 — Phân tách Historical Pin và Runtime Pin trong Notebook
- **Vấn đề:** Notebook trước đây chỉ chứa `CODE_COMMIT = "489a82fd959ccc6cced7c59fc309255e4421bef2"`, dẫn đến việc Colab checkout mã nguồn cũ chưa được khắc phục các lỗi kế toán lưu trữ.
- **Xử lý:**
  - Bảo lưu nguyên vẹn giá trị `CODE_COMMIT = "489a82fd959ccc6cced7c59fc309255e4421bef2"` cho mục đích đối chiếu lịch sử (Historical Pin).
  - Bổ sung `EXECUTION_COMMIT = "69b4b5e758f74d255ff244c56271e294da57b8e4"` là mã commit thực thi chính thức (Runtime Execution Pin).
  - Notebook thực hiện checkout `EXECUTION_COMMIT`, kiểm tra `current_head == EXECUTION_COMMIT` và in provenance đầy đủ của cả hai mốc.
  - Loại bỏ hoàn toàn phụ thuộc vòng (circular dependency) bằng cách commit phần mã nguồn core trước (`69b4b5e`), sau đó ghim SHA đó vào sổ tay và tài liệu bàn giao.

#### F-02 — Phân bổ Toàn bộ 20.000 IDs / 200 Lô cho 4 Thành viên
- **Vấn đề:** Notebook cũ chứa dòng mã cứng `COHORT_LIST = [151]`, chỉ chạy thử 1 lô của 1 thành viên.
- **Xử lý:**
  - Xây dựng hàm `member_cohort_range(mssv: str) -> List[int]` trong `src/fast_production.py`, tự động nạp cấu hình từ `config/group10_id_ranges.json` và phân bổ chính xác 50 lô / thành viên:
    - `24120010`: Lô 1..50 (5.000 IDs)
    - `24120035`: Lô 51..100 (5.000 IDs)
    - `24120232`: Lô 101..150 (5.000 IDs)
    - `24120260`: Lô 151..200 (5.000 IDs)
  - Hỗ trợ 2 chế độ rõ ràng trong notebook: `MODE = "PILOT"` (chạy thử lô đầu tiên) và `MODE = "MEMBER_PRODUCTION"` (chạy toàn bộ 50 lô).
  - Từ chối mọi MSSV hoặc MODE không hợp lệ.

#### F-03 — Kế toán Lưu trữ và Trạng thái Archive Phân biệt Rõ ràng
- **Vấn đề:** `_archive_cohort` có thể tạo archive khi chưa đủ bài, và `restore_cohort` không kiểm tra đối chiếu danh mục bài thực tế trong archive với manifest.
- **Xử lý:**
  - Bổ sung trường `archive_state` trong tệp kế toán `.manifest.json`:
    - `"checkpoint_partial"`: Khi số bài thu thập được nhỏ hơn 100 (`included_count < 100`).
    - `"checkpoint_complete"`: Khi đã gom đủ 100 bài nhưng chưa được kiểm toán toàn vẹn.
    - `"handoff_verified"`: Khi toàn bộ 100 bài đạt trạng thái hoàn thành cuối cùng (`count_completed_papers == 100`).
  - Trong `restore_cohort()`: Tự động đối chiếu danh mục thư mục trong tệp tar với `m_payload["included_ids"]`. Nếu phát hiện tệp tar bị sửa đổi hoặc danh mục không khớp, lập tức ném lỗi `ValueError` (fail-closed).
  - Bổ sung hàm thẩm định nghiệm thu `verify_cohort_handoff(runner, cohort_num)` kiểm tra đồng bộ cả archive, `.sha256` và `archive_state == "handoff_verified"`.

#### F-04 — Triệt tiêu Toàn bộ Filesystem Predicate Bỏ sót Lỗi
- **Vấn đề:** Các hàm `Path.exists()`, `Path.is_dir()`, `Path.is_file()` trong Python (đặc biệt trên Python 3.14) có thể nuốt lỗi `OSError` (ví dụ `PermissionError`, symlink loop) và trả về `False`, khiến tệp bị coi là "không tồn tại".
- **Xử lý:**
  - Thay thế toàn bộ predicate đường dẫn trong `FastProductionRunner` (`count_completed_papers`, `_archive_cohort`, `restore_cohort`, `_safe_replace_paper`, `_reset_recoverable_archive_conflicts`, `_resolve_batch_id`) bằng `_inspect_path(path)` và `_read_bytes_safely(path)`.
  - Phân biệt rõ `FileNotFoundError` (tệp thực sự vắng mặt) với `PermissionError` và các `OSError` khác (lỗi truy cập hệ thống tệp).

#### F-05 — Đọc Tệp An toàn qua Descriptor Tránh TOCTOU
- **Vấn đề:** `_read_bytes_safely()` có nhánh fallback mở lại tệp theo đường dẫn khi `Path.read_bytes` bị monkeypatch, gây nguy cơ TOCTOU (Time-of-Check to Time-of-Use).
- **Xử lý:**
  - Loại bỏ hoàn toàn fallback mở lại đường dẫn. Mọi luồng đọc tệp đều được thực thi trực tiếp trên file descriptor `fd` đã được mở với cờ `O_RDONLY | O_NOFOLLOW | O_CLOEXEC` và đã qua kiểm tra `os.fstat(fd)`.

#### F-06 — Tách biệt Hoàn toàn Làm giàu Semantic Scholar khỏi arXiv Pipeline
- **Vấn đề:** `enrich_cohort_s2()` trước đây gọi `run_cohort()`, gây dư thừa chi phí benchmark và có nguy cơ kích hoạt tải nguồn arXiv nếu có bài báo chưa hoàn tất.
- **Xử lý:**
  - Tái cấu trúc hàm `enrich_cohort_s2()` trong `src/fast_production.py`:
    - Chỉ duyệt qua các bài báo trong lô đã đạt trạng thái source-complete (`version_status == "resolved"` và toàn bộ phiên bản thuộc tập `TERMINAL`).
    - Bỏ qua các bài chưa hoàn tất source mà không gọi bất kỳ tác vụ tải nào.
    - Gọi trực tiếp `pipeline._run_s2(self.store, paper, cfg, limiter)`.
    - Kiểm thử tự động `test_enrich_cohort_s2_isolated_from_arxiv` khẳng định `arxiv_client.query_arxiv` và `arxiv_source.download_source` không bao giờ được gọi.

#### F-07 — Tối ưu hóa I/O RAM-Disk và Kaggle Snapshot
- **Vấn đề:** I/O trên Google Drive FUSE có độ trễ cao khi giải nén hàng nghìn tệp nhỏ.
- **Xử lý:**
  - Định tuyến thư mục giải nén tạm sang RAM-disk `/dev/shm` (hoặc ổ cứng SSD NVMe cục bộ `/content/fast_crawling_workspace`).
  - Nạp trước siêu dữ liệu từ snapshot Cornell arXiv qua `preload_kaggle_discovery()`, giúp giảm tải hàng nghìn truy vấn discovery HTTP lên máy chủ arXiv.

#### F-08 — Cô lập Tiện ích Xuất Parquet/CSV
- **Vấn đề:** Xuất Parquet/CSV có thể thất bại do kiểu dữ liệu và làm sụp đổ tiến trình bàn giao M1.
- **Xử lý:**
  - Cô lập hoàn toàn bước xuất bảng trong Ô 6 của notebook và ghi chú rõ: Raw Corpus là nguồn chân lý chính thức; xuất bảng chỉ là tiện ích hỗ trợ, bất kỳ lỗi nào ở bước này cũng không ảnh hưởng đến tính toàn vẹn của M1.

---

### 4. Bằng chứng Thực thi Kiểm thử Tự động (Full Test Suite Evidence)

Toàn bộ test suite của dự án đã được chạy kiểm chứng trên môi trường Python 3.14.4:

```text
============================= test session starts ==============================
platform linux -- Python 3.14.4, pytest-9.0.2, pluggy-1.6.0
rootdir: /home/tuan-anh/Documents/DS_PJ/Arxiv-Project-Group10
plugins: typeguard-4.4.4
collected 1030 items

tests/test_arxiv_client.py .................................             [  3%]
tests/test_arxiv_discovery.py ............................               [  5%]
tests/test_arxiv_history.py ............................                 [  8%]
tests/test_arxiv_http_log.py ........................................... [ 12%]
tests/test_arxiv_source.py ............................................. [ 18%]
tests/test_batch.py .......................                              [ 21%]
tests/test_benchmark.py ....                                             [ 22%]
tests/test_benchmark_metrics.py ................................         [ 25%]
tests/test_benchmark_runner.py ........................................  [ 29%]
tests/test_colab_comparison.py ...................................       [ 32%]
tests/test_colab_notebook.py ....................                        [ 34%]
tests/test_export.py ........                                            [ 35%]
tests/test_fast_production.py .....................................      [ 38%]
tests/test_generate_m1_report.py ........                                [ 39%]
tests/test_http_metrics.py ..............................                [ 42%]
tests/test_id_ranges.py ....................................             [ 45%]
tests/test_kaggle_discovery.py .........                                 [ 46%]
tests/test_live_smoke.py ................................                [ 49%]
tests/test_local_config.py ..................................            [ 53%]
tests/test_m1_production.py ...............................              [ 56%]
tests/test_pilot_sets.py ...................                             [ 58%]
tests/test_pipeline.py ................................................. [ 62%]
tests/test_pipeline_history.py ..........................                [ 66%]
tests/test_reporting_correctness.py .................................... [ 70%]
tests/test_s2_integration.py ........................................... [ 75%]
tests/test_semantic_scholar.py ......................................... [ 79%]
tests/test_semantic_scholar_references.py .............................. [ 84%]
tests/test_storage.py .................................................. [ 93%]
tests/test_storage_integrity.py ........................................ [ 98%]
............                                                             [100%]

============================ 1030 passed in 22.69s =============================
```

- **Kết quả:** **1.030 / 1.030 tests PASS (100%)**, không có bất kỳ test nào fail hoặc skip do lỗi logic.
- Không có rò rỉ secret hoặc API credentials trong toàn bộ mã nguồn và notebook (`test_notebooks_and_source_do_not_contain_hardcoded_credentials` PASS).

---

### 5. Kết luận Nghiệm thu

Milestone 1 đã hoàn thiện trọn vẹn mọi yêu cầu kỹ thuật:
1. Đã sẵn sàng cho 4 thành viên nhóm thực hiện thu thập theo phân công trên Google Colab.
2. Dữ liệu bàn giao bảo đảm tuyệt đối tính toàn vẹn, bảo toàn mã nguồn TeX và khả năng truy vết nguồn gốc.
3. Cơ sở hạ tầng mã nguồn và quy trình lưu trữ đã sẵn sàng làm nền tảng vững chắc cho việc bóc tách và phân tích dữ liệu ở Milestone 2 & 3.
