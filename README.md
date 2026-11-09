# arXiv Data Crawler & Production Corpus Pipeline — Group 10

[![Automated Unit & Integration Tests](https://github.com/Toan-ten/Arxiv-Project-Group10/actions/workflows/tests.yml/badge.svg)](https://github.com/Toan-ten/Arxiv-Project-Group10/actions/workflows/tests.yml)
[![Python 3.11 | 3.12](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue.svg)](https://www.python.org/)
[![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/Toan-ten/Arxiv-Project-Group10/blob/feat/m1-t02-colab-resume/notebooks/m1_colab_fast_production.ipynb)

Đồ án môn **Introduction to Data Science**, Trường Đại học Khoa học Tự nhiên, ĐHQG-HCM (HCMUS) — **Milestone 1: Data Crawling**.

Repository này chứa toàn bộ mã nguồn, cấu hình phân bổ định danh, tài liệu đặc tả, kiểm thử tự động CI và sổ tay vận hành sản xuất Google Colab CPU-only để thu thập dữ liệu thô từ arXiv và Semantic Scholar cho 20.000 bài báo khoa học.

---

## 1. Cấu trúc tổng thể Repository

```text
Arxiv-Project-Group10/
├── .github/
│   └── workflows/
│       └── tests.yml                 # GitHub Actions CI chạy unit test tự động (Python 3.11 & 3.12)
├── config/
│   ├── group10_id_ranges.json        # Nguồn chân lý phân bổ 20.000 IDs / 200 lô cho 4 thành viên
│   └── pilots/                       # Các tập định danh mẫu phục vụ kiểm thử và benchmark
│       ├── pilot_10.json             # Bộ 10 ID mẫu (smoke test)
│       └── pilot_500.json            # Bộ 500 ID mẫu (benchmark tải vừa)
├── docs/                             # Tài liệu kỹ thuật dự án được phân cấp chuyên trách
│   ├── acceptance/                   # Báo cáo nghiệm thu và biên bản bàn giao thành viên
│   │   ├── M1_FINAL_ACCEPTANCE.md
│   │   └── M1_HANDOFF.md
│   ├── architecture/                 # Báo cáo kiểm kê và sơ đồ kiến trúc kho lưu trữ
│   │   ├── repository-inventory.md
│   │   └── repository-structure.md
│   ├── audits/                       # Báo cáo kiểm toán bảo mật và tối ưu hóa runtime
│   │   ├── fast_production_audit.md
│   │   └── m1_finalization.md
│   ├── contracts/                    # Quy chuẩn lưu trữ, schema JSON và chỉ số báo cáo
│   │   ├── m1_contract.md
│   │   ├── reporting_metrics.md
│   │   └── storage.md
│   ├── guides/                       # Hướng dẫn chi tiết từng module kỹ thuật
│   │   ├── arxiv_client.md
│   │   ├── arxiv_discovery.md
│   │   ├── arxiv_source.md
│   │   ├── batch.md
│   │   ├── colab.md
│   │   ├── fast_crawling_guide.md
│   │   ├── id_ranges.md
│   │   ├── pipeline.md
│   │   ├── s2_integration.md
│   │   └── semantic_scholar.md
│   └── operations/                   # Hướng dẫn vận hành benchmark và thu thập dữ liệu
│       ├── benchmark_runner.md
│       ├── colab_comparison.md
│       ├── live_smoke.md
│       └── m1_production.md
├── notebooks/                        # Sổ tay tương tác chạy trên Google Colab CPU-only
│   ├── m1_colab_fast_production.ipynb     # [CHÍNH THỨC] Runner sản xuất tốc độ cao (RAM-disk & Kaggle)
│   ├── m1_colab_arxiv_production.ipynb    # [THAM CHIẾU] Baseline runner tuần tự truyền thống
│   ├── m1_colab_cpu_pilot.ipynb           # [PILOT] Kiểm chứng I/O Google Drive CPU-only
│   └── m1_colab_strategy_comparison.ipynb # [THỰC NGHIỆM] So sánh đối chứng A/B giữa các chiến lược
├── reports/
│   └── remediation/                  # Báo cáo kiểm toán độc lập bảo lưu nguyên vẹn
├── request/                          # Đề bài và quy định gốc của môn học
│   ├── crawling.md                   # Đặc tả yêu cầu Milestone 1 (Crawling)
│   ├── index.md                      # Tổng quan đồ án thực hành
│   ├── link.md                       # Liên kết phân bổ đề tài
│   ├── matching.md                   # Đặc tả Milestone 3
│   ├── paper.md                      # Đề bài gốc
│   └── parsing.md                    # Đặc tả Milestone 2
├── scripts/                          # Kịch bản dòng lệnh hỗ trợ vận hành và kiểm tra
│   ├── colab_comparison.py
│   ├── generate_m1_report.py
│   ├── live_smoke.py
│   ├── m1_production.py
│   └── make_pilots.py
├── src/                              # Mã nguồn lõi của package crawler
│   ├── arxiv_client.py               # HTTP client giao tiếp arXiv Atom API kèm pacing & backoff
│   ├── arxiv_discovery.py            # Phân tích cú pháp Atom XML, xác định phiên bản bài báo
│   ├── arxiv_source.py               # Tải source tarball, lọc và trích xuất tệp .tex và .bib
│   ├── batch.py                      # Điều phối batch, attempt logs và checkpoint
│   ├── benchmark.py                  # Đo lường tài nguyên CPU, RAM đỉnh và kích thước dữ liệu
│   ├── benchmark_runner.py           # Adapter benchmark tích hợp pipeline và validation
│   ├── export.py                     # Xuất dữ liệu Parquet và CSV chuẩn hóa từ corpus
│   ├── fast_production.py            # Engine thu thập tốc độ cao tối ưu hóa cho Colab
│   ├── id_ranges.py                  # Nạp cấu hình MSSV, sinh định danh arXiv và xác thực dải
│   ├── kaggle_discovery.py           # Nạp trước metadata ngoại tuyến từ Kaggle dataset
│   ├── local_config.py               # Đọc cấu hình biến môi trường an toàn từ .env
│   ├── pilots.py                     # Sinh tập mẫu pilot deterministic phục vụ thử nghiệm
│   ├── pipeline.py                   # Quy trình thu thập tuần tự end-to-end
│   ├── reporting.py                  # Đối soát độc lập, tính toán chỉ số và lập báo cáo M1
│   ├── s2_integration.py             # Tích hợp metadata và trích dẫn từ Semantic Scholar
│   ├── semantic_scholar.py           # Client giao tiếp Semantic Scholar Graph API
│   ├── storage.py                    # Lưu trữ nguyên tử (atomic write), manifest và checksum
│   └── storage_integrity.py          # Kiểm toán toàn vẹn dữ liệu fail-closed
├── tests/                            # Bộ kiểm thử 1.030 unit tests chạy hoàn toàn offline
├── pytest.ini                        # Cấu hình kiểm thử pytest
├── requirements.txt                  # Phụ thuộc môi trường runtime chính thức
├── requirements-dev.txt              # Phụ thuộc môi trường phát triển và kiểm thử CI
└── requirements-colab.txt            # Khóa phiên bản psutil trên Colab
```

---

## 2. Yêu cầu môi trường & Cài đặt

Dự án hỗ trợ chính thức **Python 3.11** và **Python 3.12** (được kiểm thử liên tục qua GitHub Actions CI). Các phiên bản Python từ 3.10 trở lên (kể cả 3.14) đều tương thích và vượt qua 100% bộ kiểm thử offline.

### Cài đặt môi trường phát triển cục bộ

```bash
# Khởi tạo môi trường ảo
python3 -m venv venv
source venv/bin/activate  # Trên Linux/macOS
# hoặc: .\venv\Scripts\Activate.ps1 trên Windows PowerShell

# Cài đặt toàn bộ dependencies bao gồm test runner
pip install --upgrade pip
pip install -r requirements-dev.txt
```

---

## 3. Kiểm thử tự động (Automated Unit Testing)

Hệ thống sở hữu bộ kiểm thử tự động gồm **1.030 unit tests** bao phủ toàn diện mọi tình huống biên, kiểm soát lỗi fail-closed, chống rò rỉ dữ liệu và ngăn chặn gọi mạng trái phép. Mọi bài kiểm thử đều chạy hoàn toàn ngoại tuyến (offline) bằng mock openers và sockets, hoàn thành trong khoảng **20 - 25 giây**.

### Chạy kiểm thử bằng `pytest` (Khuyến nghị)

```bash
# Chạy toàn bộ test suite
pytest

# Chạy với báo cáo ngắn gọn
pytest -q

# Chạy một tệp kiểm thử cụ thể
pytest tests/test_fast_production.py
```

### Chạy kiểm thử bằng `unittest` chuẩn

```bash
python -B -m unittest discover -s tests -v
```

### Kiểm tra cú pháp và biên dịch byte-code

```bash
python -m compileall src scripts tests
```

---

## 4. Quản lý dải định danh & Pilot Sets

Nhóm 10 được phân bổ 4 dải định danh arXiv tương ứng với 4 MSSV của các thành viên, tổng cộng **20.000 IDs** được chia thành **200 lô (cohort)**, mỗi lô 100 bài báo:

| MSSV | Họ và tên | Tháng arXiv | Dải định danh | Số lượng bài | Số lô (cohort) |
| :--- | :--- | :---: | :---: | :---: | :---: |
| `24120010` | Thành viên 1 | `2508` | `2508.07872` – `2508.12871` | 5.000 | Cohort 0 – 49 |
| `24120035` | Thành viên 2 | `2507` | `2507.16659` – `2507.21658` | 5.000 | Cohort 50 – 99 |
| `24120054` | Thành viên 3 | `2507` | `2507.21659` – `2507.26658` | 5.000 | Cohort 100 – 149 |
| `24120070` | Thành viên 4 | `2508` | `2508.02872` – `2508.07871` | 5.000 | Cohort 150 – 199 |

Nguồn chân lý duy nhất được khai báo tại [`config/group10_id_ranges.json`](config/group10_id_ranges.json).

### Kiểm tra dải định danh từ Python

```python
from src.id_ranges import load_config, iter_assigned_ids

# Tải cấu hình
config = load_config()
print("Các thành viên:", list(config.keys()))

# Duyệt từng cặp (mssv, arxiv_id)
for mssv, arxiv_id in iter_assigned_ids():
    print(f"MSSV: {mssv} -> arXiv ID: {arxiv_id}")
    break
```

### Tái tạo các tập mẫu Pilot

Hai tập dữ liệu mẫu được trích xuất tất định (deterministic) từ cấu hình phục vụ kiểm thử nhanh và benchmark tải:
- [`config/pilots/pilot_10.json`](config/pilots/pilot_10.json): 10 ID mẫu bao gồm các điểm biên và trung vị.
- [`config/pilots/pilot_500.json`](config/pilots/pilot_500.json): 500 ID mẫu phân bổ đồng đều trên cả 4 dải.

Để tạo lại hai tập tin này:
```bash
python scripts/make_pilots.py
```

Chi tiết xem tại [`docs/guides/id_ranges.md`](docs/guides/id_ranges.md).

---

## 5. Quy trình vận hành trên Google Colab CPU-only

Toàn bộ quy trình thu thập dữ liệu được thiết kế vận hành trên **Google Colab CPU-only** (Accelerator: None), bảo đảm tính tương thích với hạ tầng miễn phí, không yêu cầu GPU và tuân thủ nghiêm ngặt các giới hạn tài nguyên của Google Drive FUSE.

### 5.1. Sổ tay sản xuất chính thức: `m1_colab_fast_production.ipynb`

- **Đường dẫn:** [`notebooks/m1_colab_fast_production.ipynb`](notebooks/m1_colab_fast_production.ipynb)
- **Mục đích:** Quy trình sản xuất chính thức duy nhất của nhóm để thu thập 20.000 bài báo.
- **Kiến trúc tối ưu hóa hiệu năng:**
  1. **Tách biệt I/O:** Sử dụng NVMe SSD cục bộ (`/content/fast_crawling_workspace`) làm không gian làm việc chính; giải nén và lọc TeX/Bib trên bộ nhớ chia sẻ RAM-disk (`/dev/shm`), giảm thiểu tới 95% độ trễ thao tác tệp so với ghi trực tiếp lên Drive.
  2. **Tăng tốc siêu dữ liệu với Kaggle:** Nạp trước tệp siêu dữ liệu `arxiv-metadata-oai-snapshot.json` từ Kaggle dataset, bỏ qua hoàn toàn bước gọi HTTP Atom API Discovery cho các bài báo đã có trong snapshot.
  3. **Lấy đầy đủ lịch sử phiên bản:** Tự động phát hiện và trích xuất ngày nộp của mọi phiên bản có sẵn (`revision_history=True`).
  4. **Tải mã nguồn TeX/BibTeX nguyên tử:** Tải tarball từ arXiv, chỉ lưu trữ các tệp `.tex` và `.bib`, loại bỏ tệp nhị phân và ảnh, ghi nhận mã băm SHA-256 vào `manifest.json`.
  5. **Làm giàu trích dẫn Semantic Scholar:** Tích hợp tùy chọn Semantic Scholar Graph API với cơ chế giới hạn tốc độ nghiêm ngặt, tự động dừng an toàn khi gặp HTTP 429.
  6. **Đóng gói lô bền vững:** Sau mỗi lô 100 bài báo (cohort), hệ thống đóng gói nén `cohort_XXX.tar.gz` kèm mã băm `.sha256` đồng bộ lên Google Drive. Nếu runtime bị ngắt kết nối, tiến trình có thể tiếp tục ngay tại lô tiếp theo mà không làm mất dữ liệu.
  7. **Đối soát & Báo cáo M1:** Tự động chạy thuật toán đối soát 2-lượt độc lập, tính toán đầy đủ các chỉ số theo hợp đồng Milestone 1 và xuất dữ liệu phân tích dạng Parquet (`papers.parquet`, `references.parquet`).

Hướng dẫn từng bước dành cho thành viên nhóm: xem [`docs/acceptance/M1_HANDOFF.md`](docs/acceptance/M1_HANDOFF.md) và [`docs/guides/fast_crawling_guide.md`](docs/guides/fast_crawling_guide.md).

### 5.2. Các sổ tay tham chiếu và thực nghiệm khác

- **[`notebooks/m1_colab_arxiv_production.ipynb`](notebooks/m1_colab_arxiv_production.ipynb):** Baseline runner tuần tự ghi trực tiếp lên Drive, dùng để đối chiếu hiệu năng. Chi tiết tại [`docs/operations/m1_production.md`](docs/operations/m1_production.md).
- **[`notebooks/m1_colab_cpu_pilot.ipynb`](notebooks/m1_colab_cpu_pilot.ipynb):** Sổ tay kiểm chứng I/O trên Google Drive CPU-only. Chi tiết tại [`docs/guides/colab.md`](docs/guides/colab.md).
- **[`notebooks/m1_colab_strategy_comparison.ipynb`](notebooks/m1_colab_strategy_comparison.ipynb):** Sổ tay so sánh thực nghiệm A/B giữa chiến lược thuần arXiv và chiến lược kết hợp Semantic Scholar. Chi tiết tại [`docs/operations/colab_comparison.md`](docs/operations/colab_comparison.md).

---

## 6. Hợp đồng lưu trữ & Tiêu chuẩn dữ liệu (Storage Contract v2)

Mọi bài báo hoàn tất đều được lưu trữ theo cấu trúc thư mục phân cấp chuẩn hóa tại `papers/<folder_key>/` với `<folder_key>` dạng `YYMM-NNNNN`:

```text
papers/2508-07872/
├── metadata.json          # Siêu dữ liệu bài báo (tiêu đề, tác giả, ngày tạo, phiên bản)
├── manifest.json          # Danh mục kiểm toán, mã băm SHA-256 và dung lượng các tệp
├── references.json        # Danh sách tài liệu tham khảo trung gian từ Semantic Scholar
└── src/                   # Thư mục chứa mã nguồn TeX/BibTeX theo từng phiên bản
    ├── v1/
    │   ├── main.tex
    │   └── references.bib
    └── v2/
        └── paper.tex
```

Mỗi phiên bản mã nguồn phải tuân thủ nghiêm ngặt các quy tắc:
- Chỉ chứa các tệp văn bản mã nguồn có đuôi `.tex` và `.bib`.
- Loại bỏ tuyệt đối các tệp nhị phân (`.pdf`, `.png`, `.jpg`, `.synctex.gz`, v.v.).
- Mã băm SHA-256 của từng tệp trên đĩa phải khớp chính xác với `manifest.json`.
- Nếu bài báo không có mã nguồn TeX (chỉ có PDF từ tác giả), hệ thống ghi nhận trạng thái `NO_TEX_SOURCE` hợp lệ mà không tạo tệp mã nguồn giả mạo.

Xem đặc tả chi tiết tại [`docs/contracts/m1_contract.md`](docs/contracts/m1_contract.md) và [`docs/contracts/reporting_metrics.md`](docs/contracts/reporting_metrics.md).

---

## 7. Tài liệu kỹ thuật chi tiết

| Nhóm tài liệu | Đường dẫn | Nội dung chính |
| :--- | :--- | :--- |
| **Quy chuẩn & Hợp đồng** | [`docs/contracts/m1_contract.md`](docs/contracts/m1_contract.md) | Đặc tả cấu trúc thư mục, schema JSON và trạng thái bài báo |
| | [`docs/contracts/reporting_metrics.md`](docs/contracts/reporting_metrics.md) | Quy chuẩn tính toán chỉ số thống kê, invariants và đối chiếu dữ liệu |
| | [`docs/contracts/storage.md`](docs/contracts/storage.md) | Cơ chế ghi đĩa nguyên tử, checksum và checkpoint |
| **Hướng dẫn Module** | [`docs/guides/arxiv_client.md`](docs/guides/arxiv_client.md) | Giao tiếp arXiv API, xử lý HTTP 429/503 và golden-ratio backoff |
| | [`docs/guides/arxiv_discovery.md`](docs/guides/arxiv_discovery.md) | Phân tích cú pháp Atom XML, phát hiện đa phiên bản |
| | [`docs/guides/arxiv_source.md`](docs/guides/arxiv_source.md) | Tải và giải nén source tarball an toàn, lọc TeX/Bib |
| | [`docs/guides/batch.md`](docs/guides/batch.md) | Điều phối lô, nhật ký retry và checkpoint bền vững |
| | [`docs/guides/pipeline.md`](docs/guides/pipeline.md) | Quy trình thu thập dữ liệu tuần tự tích hợp |
| | [`docs/guides/semantic_scholar.md`](docs/guides/semantic_scholar.md) | Giao tiếp Semantic Scholar API, cơ chế phân trang và rate limiter |
| | [`docs/guides/s2_integration.md`](docs/guides/s2_integration.md) | Cấu hình bật/tắt Semantic Scholar và chuẩn hóa dữ liệu tham khảo |
| | [`docs/guides/fast_crawling_guide.md`](docs/guides/fast_crawling_guide.md) | Hướng dẫn vận hành hệ thống thu thập tốc độ cao trên SSD |
| **Nghiệm thu & Bàn giao** | [`docs/acceptance/M1_HANDOFF.md`](docs/acceptance/M1_HANDOFF.md) | Sổ tay hướng dẫn chi tiết từng bước cho 4 thành viên vận hành Colab |
| | [`docs/acceptance/M1_FINAL_ACCEPTANCE.md`](docs/acceptance/M1_FINAL_ACCEPTANCE.md) | Báo cáo kiểm toán và nghiệm thu toàn diện Milestone 1 |
| **Kiến trúc & Kiểm kê** | [`docs/architecture/repository-inventory.md`](docs/architecture/repository-inventory.md) | Báo cáo kiểm kê 98 tệp và ma trận trách nhiệm toàn repository |
| | [`docs/architecture/repository-structure.md`](docs/architecture/repository-structure.md) | Sơ đồ và quy tắc tổ chức thư mục của dự án |
| **Đề bài môn học** | [`request/crawling.md`](request/crawling.md) | Đề bài và quy định chính thức cho Milestone 1 |
| | [`request/index.md`](request/index.md) | Tổng quan cấu trúc và yêu cầu đồ án môn học |

---

## 8. Đóng góp & Tiêu chuẩn mã nguồn

- Tuân thủ quy chuẩn mã nguồn Python PEP 8 và gợi ý kiểu dữ liệu (type annotations).
- Mọi commit mới phải vượt qua 100% bộ kiểm thử tự động trên GitHub Actions CI.
- Xem chi tiết quy định đóng góp tại [`CONTRIBUTING.md`](CONTRIBUTING.md).

---

## 9. Giấy phép (License)

Dự án được phân phối dưới giấy phép [MIT License](LICENSE).
