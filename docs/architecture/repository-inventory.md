# BẢNG KIỂM KÊ VÀ MA TRẬN TRÁCH NHIỆM TỆP TIN (REPOSITORY INVENTORY & RESPONSIBILITY MATRIX)
## Dự án: Arxiv-Project-Group10 — Nhánh: `feat/m1-t02-colab-resume`
**Mã commit kiểm kê baseline:** `4c7be42076eb4ddef34e319de9ce554b06099d68`

---

### 1. Tổng quan Kiểm kê & Phân loại
Tổng số tệp tin được theo dõi bởi Git (Git-tracked files): **98 tệp**.

| Nhóm danh mục | Số lượng | Mô tả chức năng |
| :--- | :---: | :--- |
| **Gốc & Cấu hình môi trường (Root Config)** | 5 | Cấu hình git, bản quyền, biến môi trường mẫu và README. |
| **Khai báo phụ thuộc (Requirements)** | 4 | Thư viện môi trường runtime, colab và tệp trùng lặp. |
| **Cấu hình dữ liệu (Config)** | 3 | Dải ID được giao cho Nhóm 10 và các tập dữ liệu pilot mẫu. |
| **Tài liệu kỹ thuật (Docs)** | 21 | Hợp đồng lưu trữ, hướng dẫn module, vận hành, kiểm toán và nghiệm thu. |
| **Sổ tay tính toán (Notebooks)** | 4 | Sổ tay Colab sản xuất chính thức, pilot, so sánh chiến lược. |
| **Báo cáo kiểm toán lịch sử (Reports)** | 5 | Báo cáo kiểm toán độc lập các giai đoạn remediation trước. |
| **Kịch bản thực thi (Scripts)** | 6 | Kịch bản tạo báo cáo M1, kiểm thử smoke, chạy batch, tạo pilot. |
| **Mã nguồn ứng dụng (Source Code - `src/`)** | 21 | Gói thư viện lõi thu thập, lưu trữ, kiểm toán toàn vẹn, làm giàu S2. |
| **Kiểm thử tự động (Tests - `tests/`)** | 29 | Toàn bộ 1.030 bài kiểm thử đơn vị, tích hợp và an toàn offline. |

---

### 2. Ma trận Trách nhiệm Chi tiết Từng Tệp Tin (File Responsibility Matrix)

| Đường dẫn hiện tại | Trách nhiệm & Mục đích thực tế | Danh mục | Đường dẫn đề xuất | Hành động | Phụ thuộc / Tham chiếu | Mức độ rủi ro | Biện pháp xác minh |
| :--- | :--- | :--- | :--- | :---: | :--- | :---: | :--- |
| `.env.example` | Mẫu biến môi trường (`KAGGLE_API_TOKEN`, `S2_API_KEY`) | Config | `.env.example` | **KEEP** | `local_config.py`, README | Thấp | Kiểm tra cú pháp |
| `.gitattributes` | Quy định chuẩn hóa ngắt dòng LF cho Git | Config | `.gitattributes` | **KEEP** | Git repository | Thấp | `git status` |
| `.gitignore` | Bỏ qua tệp nhị phân, cache, dữ liệu tạm, corpus | Config | `.gitignore` | **KEEP** | Toàn bộ repository | Thấp | `git status` |
| `LICENSE` | Giấy phép mã nguồn mở (MIT) | Root | `LICENSE` | **KEEP** | GitHub repo | Thấp | Đọc tệp |
| `README.md` | Tài liệu giới thiệu tổng quan và hướng dẫn dự án | Docs | `README.md` | **UPDATE** | Toàn bộ người dùng | Trung bình | Rà soát liên kết, cập nhật hiện trạng |
| `requirements.txt` | Phụ thuộc môi trường runtime chính thức của dự án | Requirements | `requirements.txt` | **KEEP** | Notebooks, Colab, CI | Trung bình | `pip install -r requirements.txt` |
| `src/requirements.txt` | Bản sao chép trùng lặp của `requirements.txt` trong package | Requirements | *(Xóa)* | **DELETE** | Không có mã nguồn nào trỏ vào | Thấp | `git diff`, kiểm thử test suite |
| `requirements-colab.txt` | Khóa phiên bản `psutil` cho môi trường Colab | Requirements | `requirements-colab.txt` | **KEEP** | Notebooks | Thấp | Cài đặt pip |
| `requirements-colab-compare.txt` | Phụ thuộc cho notebook so sánh chiến lược | Requirements | `requirements-colab-compare.txt` | **KEEP** | `m1_colab_strategy_comparison.ipynb` | Thấp | Cài đặt pip |
| `config/group10_id_ranges.json` | Cấu hình gốc phân bổ 20.000 IDs / 200 lô cho 4 thành viên | Config | `config/group10_id_ranges.json` | **KEEP** | `id_ranges.py`, `fast_production.py`, notebooks | Cao | `test_id_ranges.py`, `test_fast_production.py` |
| `config/pilot_10.json` | Tập mẫu 10 định danh arXiv cho kiểm thử nhanh | Config | `config/pilots/pilot_10.json` | **MOVE** | `pilots.py`, `test_pilot_sets.py`, notebooks | Trung bình | `test_pilot_sets.py` |
| `config/pilot_500.json` | Tập mẫu 500 định danh arXiv cho kiểm thử tải vừa | Config | `config/pilots/pilot_500.json` | **MOVE** | `pilots.py`, `test_pilot_sets.py`, notebooks | Trung bình | `test_pilot_sets.py` |
| `docs/m1_contract.md` | Storage Contract v2 quy định cấu trúc và tính toàn vẹn | Contracts | `docs/contracts/m1_contract.md` | **MOVE** | `storage.py`, `storage_integrity.py`, README | Trung bình | Rà soát liên kết |
| `docs/reporting_metrics.md` | Quy chuẩn số liệu báo cáo, invariants và đối chiếu | Contracts | `docs/contracts/reporting_metrics.md` | **MOVE** | `reporting.py`, `generate_m1_report.py` | Trung bình | Rà soát liên kết |
| `docs/arxiv_client.md` | Hướng dẫn kỹ thuật module giao tiếp arXiv API | Guides | `docs/guides/arxiv_client.md` | **MOVE** | README, `arxiv_client.py` | Thấp | Rà soát liên kết |
| `docs/arxiv_discovery.md` | Hướng dẫn kỹ thuật module khám phá bài báo arXiv | Guides | `docs/guides/arxiv_discovery.md` | **MOVE** | README, `arxiv_discovery.py` | Thấp | Rà soát liên kết |
| `docs/arxiv_source.md` | Hướng dẫn kỹ thuật tải và trích xuất TeX source | Guides | `docs/guides/arxiv_source.md` | **MOVE** | README, `arxiv_source.py` | Thấp | Rà soát liên kết |
| `docs/batch.md` | Hướng dẫn xử lý lô và checkpoint | Guides | `docs/guides/batch.md` | **MOVE** | README, `batch.py` | Thấp | Rà soát liên kết |
| `docs/benchmark_runner.md` | Hướng dẫn đo lường tài nguyên và hiệu năng thu thập | Guides | `docs/guides/benchmark_runner.md` | **MOVE** | README, `benchmark_runner.py` | Thấp | Rà soát liên kết |
| `docs/id_ranges.md` | Hướng dẫn xử lý khoảng dải ID và định dạng arXiv ID | Guides | `docs/guides/id_ranges.md` | **MOVE** | README, `id_ranges.py` | Thấp | Rà soát liên kết |
| `docs/pipeline.md` | Hướng dẫn quy trình pipeline thu thập dữ liệu tuần tự | Guides | `docs/guides/pipeline.md` | **MOVE** | README, `pipeline.py` | Thấp | Rà soát liên kết |
| `docs/s2_integration.md` | Hướng dẫn tích hợp Semantic Scholar metadata | Guides | `docs/guides/s2_integration.md` | **MOVE** | README, `s2_integration.py` | Thấp | Rà soát liên kết |
| `docs/semantic_scholar.md` | Hướng dẫn chi tiết Semantic Scholar API & Rate Limiting | Guides | `docs/guides/semantic_scholar.md` | **MOVE** | README, `semantic_scholar.py` | Thấp | Rà soát liên kết |
| `docs/storage.md` | Hướng dẫn lưu trữ tệp, atomic commit và manifest | Guides | `docs/guides/storage.md` | **MOVE** | README, `storage.py` | Thấp | Rà soát liên kết |
| `docs/colab.md` | Hướng dẫn vận hành trên Google Colab cơ bản | Operations | `docs/operations/colab.md` | **MOVE** | README, notebooks | Thấp | Rà soát liên kết |
| `docs/colab_comparison.md` | Hướng dẫn so sánh các chiến lược thu thập trên Colab | Operations | `docs/operations/colab_comparison.md` | **MOVE** | `m1_colab_strategy_comparison.ipynb` | Thấp | Rà soát liên kết |
| `docs/fast_crawling_guide.md` | Hướng dẫn thu thập tốc độ cao qua SSD và RAM-disk | Operations | `docs/operations/fast_crawling_guide.md` | **MOVE** | README, `fast_production.py` | Thấp | Rà soát liên kết |
| `docs/live_smoke.md` | Hướng dẫn kiểm thử smoke có kiểm soát | Operations | `docs/operations/live_smoke.md` | **MOVE** | `scripts/live_smoke.py` | Thấp | Rà soát liên kết |
| `docs/m1_finalization.md` | Tài liệu chốt tiêu chí kỹ thuật hoàn thiện M1 | Operations | `docs/operations/m1_finalization.md` | **MOVE** | README | Thấp | Rà soát liên kết |
| `docs/m1_production.md` | Tài liệu sản xuất dữ liệu quy mô lớn M1 | Operations | `docs/operations/m1_production.md` | **MOVE** | README, `scripts/m1_production.py` | Thấp | Rà soát liên kết |
| `docs/M1_HANDOFF.md` | Sổ tay vận hành chính thức dành cho 4 thành viên | Operations | `docs/operations/m1_handoff.md` | **MOVE/RENAME** | Notebook sản xuất, README | Cao | Rà soát liên kết, test notebook |
| `docs/fast_production_audit.md` | Báo cáo kiểm toán fast production runner | Audits | `docs/audits/fast_production_audit.md` | **MOVE** | README | Thấp | Rà soát liên kết |
| `docs/M1_FINAL_ACCEPTANCE.md` | Báo cáo nghiệm thu toàn diện Milestone 1 | Acceptance | `docs/acceptance/m1_final_acceptance.md` | **MOVE/RENAME** | README, ban kiểm toán | Cao | Rà soát liên kết, sửa link request |
| `notebooks/m1_colab_fast_production.ipynb` | Sổ tay Colab sản xuất chính thức M1 cho 4 thành viên | Notebooks | `notebooks/production/m1_colab_fast_production.ipynb` | **MOVE** | `test_fast_production.py`, thành viên | Rất cao | AST parse, test notebook execution |
| `notebooks/m1_colab_cpu_pilot.ipynb` | Sổ tay Colab chạy pilot xác minh CPU-only | Notebooks | `notebooks/pilots/m1_colab_cpu_pilot.ipynb` | **MOVE** | `test_colab_notebook.py` | Cao | `test_colab_notebook.py` |
| `notebooks/m1_colab_arxiv_production.ipynb` | Sổ tay Colab sản xuất baseline (truy vấn trực tiếp) | Notebooks | `notebooks/pilots/m1_colab_arxiv_production.ipynb` | **MOVE** | Tham chiếu tham khảo | Trung bình | AST parse |
| `notebooks/m1_colab_strategy_comparison.ipynb` | Sổ tay Colab đo lường và so sánh hiệu năng I/O | Notebooks | `notebooks/experiments/m1_colab_strategy_comparison.ipynb` | **MOVE** | `colab_comparison.py` | Trung bình | AST parse |
| `reports/remediation/baseline.md` | Báo cáo kiểm toán baseline ban đầu | Reports | `reports/remediation/baseline.md` | **KEEP** | Lịch sử nghiệm thu | Không | Bảo lưu nguyên vẹn |
| `reports/remediation/reporting_audit.md` | Báo cáo kiểm toán báo cáo Milestone 1 | Reports | `reports/remediation/reporting_audit.md` | **KEEP** | Lịch sử nghiệm thu | Không | Bảo lưu nguyên vẹn |
| `reports/remediation/reporting_correctness_audit.md` | Báo cáo kiểm toán tính đúng đắn của reconciliation | Reports | `reports/remediation/reporting_correctness_audit.md` | **KEEP** | Lịch sử nghiệm thu | Không | Bảo lưu nguyên vẹn |
| `reports/remediation/security_audit.md` | Báo cáo kiểm toán an ninh lưu trữ và path traversal | Reports | `reports/remediation/security_audit.md` | **KEEP** | Lịch sử nghiệm thu | Không | Bảo lưu nguyên vẹn |
| `reports/remediation/storage_contract_audit.md` | Báo cáo kiểm toán hợp đồng lưu trữ v2 | Reports | `reports/remediation/storage_contract_audit.md` | **KEEP** | Lịch sử nghiệm thu | Không | Bảo lưu nguyên vẹn |
| `scripts/__init__.py` | Đánh dấu package cho thư mục scripts | Scripts | `scripts/__init__.py` | **KEEP** | Python import | Thấp | Import check |
| `scripts/colab_comparison.py` | Kịch bản chạy đo lường so sánh chiến lược Colab | Scripts | `scripts/colab_comparison.py` | **KEEP** | `test_colab_comparison.py` | Trung bình | `test_colab_comparison.py` |
| `scripts/generate_m1_report.py` | Kịch bản tạo báo cáo kiểm toán Markdown M1 | Scripts | `scripts/generate_m1_report.py` | **KEEP** | `test_generate_m1_report.py`, notebooks | Cao | `test_generate_m1_report.py` |
| `scripts/live_smoke.py` | Kịch bản smoke test với kiểm soát an toàn | Scripts | `scripts/live_smoke.py` | **KEEP** | `test_live_smoke.py` | Trung bình | `test_live_smoke.py` |
| `scripts/m1_production.py` | Kịch bản chạy sản xuất M1 chế độ CLI | Scripts | `scripts/m1_production.py` | **KEEP** | `test_m1_production.py` | Trung bình | `test_m1_production.py` |
| `scripts/make_pilots.py` | Kịch bản trích xuất tập dữ liệu pilot từ config | Scripts | `scripts/make_pilots.py` | **KEEP** | `test_pilot_sets.py` | Trung bình | `test_pilot_sets.py` |
| `src/*` (20 modules) | Toàn bộ mã nguồn thư viện lõi | Source | `src/*` | **KEEP** | Toàn bộ repository | Rất cao | Không đổi tên module, giữ nguyên public API |
| `tests/*` (29 modules) | Toàn bộ 1.030 bài kiểm thử đơn vị và tích hợp | Tests | `tests/*` | **KEEP** | Pytest runner | Rất cao | Giữ nguyên cấu trúc phát hiện test tự động |

---

### 3. Sơ đồ Cây Phụ thuộc & Điểm vào (Dependency & Entry-point Map)

```text
[Google Colab User]
    │
    ▼
notebooks/production/m1_colab_fast_production.ipynb
    │
    ├──> checks out EXECUTION_COMMIT (pinned SHA)
    ├──> imports:
    │      src.fast_production (FastProductionRunner, member_cohort_range, verify_cohort_handoff)
    │      scripts.generate_m1_report (compute_m1_statistics, format_markdown_summary)
    │      src.export (export_papers_table, export_references_table, export_to_parquet)
    │
    ├──> reads:
    │      config/group10_id_ranges.json (via src.id_ranges)
    │      arxiv-metadata-oai-snapshot.json (via src.kaggle_discovery)
    │
    └──> synchronizes to Drive:
           archives/cohort_XXXX_corpus.tar.gz (.sha256, .manifest.json)
           reports/m1_audit_summary_mssv_XXXX_production.md
           final_output/papers.parquet, references.parquet
```

- **Mã nguồn ứng dụng (`src/`):** Hoạt động như một thư viện nội bộ thống nhất được import bởi `scripts/`, `notebooks/`, và `tests/`. Để tránh làm hỏng các đường dẫn import trong các commit đã ghim (`EXECUTION_COMMIT`), `src/` giữ nguyên toàn bộ tên module.
- **Tập tin cấu hình (`config/`):** `group10_id_ranges.json` là nguồn chân lý duy nhất cho phân bổ ID của nhóm. Các tập pilot mẫu (`pilot_10.json`, `pilot_500.json`) được gom vào thư mục con `config/pilots/` và cập nhật đường dẫn tương ứng trong `src/pilots.py`, `scripts/make_pilots.py`, và `tests/test_pilot_sets.py`.
- **Tài liệu (`docs/`):** Được phân loại rõ ràng thành 6 thư mục chức năng: `architecture/`, `contracts/`, `guides/`, `operations/`, `audits/`, và `acceptance/`.
