# arXiv — Group 10

Đồ án môn Introduction to Data Science, HCMUS — Milestone 1: Data Crawling.
Hiện có module quản lý ID, Entry Discovery, HTTP Client, Source Downloader
và bộ đo benchmark nền. Crawler được kiểm thử offline và đã có live smoke local
ở phạm vi nhỏ; chưa có benchmark M1 đầy đủ trên Colab CPU-only. Storage + Checkpoint/Resume giai đoạn 1 đã có và được kiểm thử offline.
Pipeline v1 đã nối các module cho danh sách ID chỉ định và được kiểm thử offline.
Semantic Scholar đã nối Pipeline theo chế độ opt-in, mặc định tắt;
chưa có benchmark M1 thực nghiệm.

## Cấu trúc repository

```text
config/group10_id_ranges.json  Khoảng ID theo MSSV
config/pilot_10.json          Bộ ID smoke test
config/pilot_500.json         Bộ ID pilot/benchmark
scripts/make_pilots.py        Sinh lại hai bộ pilot
src/id_ranges.py              Đọc cấu hình, kiểm tra và sinh ID
src/pilots.py                 Chọn pilot deterministic từ các range
src/arxiv_discovery.py        Phân tích Atom XML và xác định version
src/arxiv_client.py           HTTP client cho một ID
src/arxiv_source.py           Tải source và trích xuất TeX/BibTeX an toàn
src/semantic_scholar.py       HTTP client metadata/references S2
src/s2_integration.py         Identity, cache và chuyển dữ liệu S2 sang contract v2
src/storage.py                Lưu JSON, kiểm tra source và checkpoint/resume offline
src/pipeline.py               Điều phối tuần tự các ID chỉ định và resume
src/benchmark.py              Bộ đo thời gian, RAM và dung lượng corpus
docs/                         Hướng dẫn module và contract M1
tests/                        Unit tests offline cho các module
tests/test_pilot_sets.py      Unit tests cho pilot sets
```

## Chạy module ID

Dùng Python 3.10 trở lên; môi trường đã kiểm thử là Python 3.14.1.
Module ID và các module crawler dùng thư viện chuẩn Python.
Chạy từ thư mục gốc repository:

```powershell
python -B -m src.id_ranges
```

Lệnh trên hiển thị cấu hình và hai mốc đã định dạng. Để dùng từ Python:

```python
from src.id_ranges import arxiv_id_to_folder_key, iter_assigned_ids

ids = iter_assigned_ids()
print(next(ids))  # ('24120010', '2508.07872')
print(arxiv_id_to_folder_key("2508.07872"))  # 2508-07872
```

Iterator sinh từng cặp `(mssv, arxiv_id)`, bao gồm hai đầu khoảng, hỗ trợ
assigned IDs năm chữ số từ `2015-01` đến `2099-12`, chỉ nhận khoảng cùng
tháng và từ chối overlap. Cấu hình hiện tại có 5.000 vị trí mỗi
thành viên, tổng **20.000 ID ứng viên**, không đồng nghĩa 20.000 publication
hợp lệ. Folder key dùng cho lưu trữ; ID gốc vẫn được giữ khi gọi API.
Xem [hướng dẫn module ID](docs/id_ranges.md).

## Pilot sets

Hai bộ ID dưới đây được chọn deterministic từ cấu hình gốc, dùng cho smoke
test và benchmark; chúng **không thay thế** 20.000 ID ứng viên của full run.

- `config/pilot_10.json`: 2 boundary ID mỗi range, cộng 2 midpoint.
- `config/pilot_500.json`: 125 ID mỗi range, trải đều và gồm hai boundary.

Sinh lại và kiểm tra:

```powershell
python -B scripts/make_pilots.py
python -B -m unittest discover -s tests -p "test_pilot_sets.py" -v
```

Các danh sách chỉ chọn ID ứng viên, không xác nhận publication tồn tại và
không tự chạy crawler. Kích thước 10/500 không phải ngân sách HTTP requests.

## Entry Discovery, HTTP Client và Source Downloader

- [Entry Discovery](docs/arxiv_discovery.md): `parse_arxiv_response(xml, mssv, arxiv_id)`
  phân tích Atom XML offline, trả metadata và thông tin version.
- [HTTP Client](docs/arxiv_client.md): `query_arxiv(mssv, arxiv_id)` truy vấn một
  ID gốc, có timeout, pacing và retry.
- [Source Downloader](docs/arxiv_source.md): `download_source(mssv, versioned_id, destination)`
  tải từng version có hậu tố `vN`; `extract_source()` xử lý archive có sẵn
  offline, giữ bytes và đường dẫn tương đối của các file `.tex`/`.bib`.

API và source dùng chung pacing trong một process. Các tài liệu trên mô tả
giao diện, trạng thái lỗi, kiểm thử và phạm vi thử mạng ở bước sau.

## Semantic Scholar metadata và references

Client `src.semantic_scholar.fetch_metadata()` đã có để truy vấn metadata theo
batch ID gốc, mặc định 100 và tối đa 500 ID/request. Có limiter S2 dùng chung,
timeout, retry hữu hạn và dừng lời gọi khi hết attempts mà vẫn bị HTTP 429.
`fetch_references()` lấy references phân trang và chuẩn hóa snapshot contract v2
trong bộ nhớ, giữ cả references thiếu arXiv ID. Client không ghi corpus;
Pipeline có thể lưu qua Storage khi bật `s2_config`. Tích hợp mới được kiểm thử offline.
Xem [API, cấu hình key/pacing và các trạng thái kết quả](docs/semantic_scholar.md).

Có thể lưu key S2 một lần trong `.env` tại root; dùng `.env.example` làm mẫu
và làm theo [hướng dẫn cấu hình local](docs/semantic_scholar.md#api-key-môi-trường-hoặc-env-tại-root).
Biến môi trường được ưu tiên; không commit `.env`. Bộ đọc dùng thư viện chuẩn,
không cần cài thêm package. [Live Smoke](docs/live_smoke.md) mặc định chỉ dry run.
Xem [S2 integration: bật/tắt, cache, resume và batch stop](docs/s2_integration.md).

Xem [ngày revision, S2 batching và arXiv-first](docs/m1_finalization.md).
Pipeline và batch mới tự bổ sung ngày từng version; history đã xác minh được cache.
Live Smoke có ngân sách cố định giữ chế độ history tắt.

## Pipeline v1

`run_selected(pairs, corpus_root, ...)` trong `src.pipeline` nhận danh sách
`(MSSV, base_arxiv_id)`, kiểm tra ownership rồi nối discovery, download và Storage.
Khi chạy lại, pipeline dùng checkpoint và bỏ qua source còn hợp lệ; không ghi đè
source hỏng/orphan. Khi S2 tắt, references mới vẫn not_attempted. Khi bật,
publication chỉ complete nếu đủ mọi stage và version theo contract v2.

`versions=None` xử lý mọi version đã xác minh; `versions=("v1",)` chỉ chọn v1
nếu Discovery xác nhận phiên bản đó. HTTP 403/429 trả về sẽ dừng batch; cấu hình
`max_attempts=1` không retry thêm sau lỗi.

Kiểm thử riêng bằng fake HTTP, không gọi mạng thật:

```powershell
python -B -m unittest discover -s tests -p "test_pipeline.py" -v
```

Xem [cách gọi Python, summary và giới hạn resume](docs/pipeline.md).
Hàm sử dụng HTTP thật nếu được gọi trực tiếp ngoài fake HTTP; chỉ thực hiện
sau khi duyệt pilot có phạm vi cụ thể. V1 chưa có CLI hoặc request budget.

## Contract và benchmark foundation

[Contract M1](docs/m1_contract.md) mô tả layout corpus, schema
`metadata.json`, `references.json`, `manifest.json`, trạng thái và log.
Contract phiên bản 2 đã được đối chiếu với giao diện crawler hiện tại.
Storage + Checkpoint/Resume giai đoạn 1 đã được triển khai theo contract;
S2 integration dùng API Storage hiện có; CLI và bước export vẫn chưa có.

[Hướng dẫn Storage](docs/storage.md) mô tả giao diện, atomic JSON, checksum
và kế hoạch resume. Storage không gọi mạng và không tạo sẵn thư mục version;
source có sẵn nhưng thiếu checkpoint hợp lệ được giữ nguyên để audit.

`benchmark_pipeline(crawler_main_func, ...)` trong `src/benchmark.py` bọc một
hàm crawler để đo wall time, RAM đầu/cuối/đỉnh và kích thước corpus cuối,
rồi ghi báo cáo cùng môi trường và commit. Khi có `psutil`, bộ đo lấy RAM
của process và các process con; fallback phụ thuộc hệ điều hành.

Metrics v2 bổ sung RAM trung bình/peak và disk peak qua lấy mẫu; adapter tổng hợp
thời gian paper và Discovery từ durable logs. Phạm vi disk mặc định là corpus,
chưa bao gồm archive nằm trong TEMP ngoài corpus.
Entry point `python -m src.benchmark` hiện chạy dummy crawler, không phải
benchmark M1 thực nghiệm trên Colab CPU-only.

## Benchmark Adapter và Metrics v2

`src.benchmark_runner.run_benchmark()` đo một invocation Batch bằng foundation
hiện có rồi kiểm chứng corpus theo Storage/checksum. `aggregate_batch()` chỉ đọc
logs và dữ liệu để tổng hợp nhiều attempts, không đếm trùng paper/version.
Trạng thái thực thi, batch finished/stopped, độ phủ source và publication complete
được trình bày riêng; references chưa thu thập vẫn là dữ liệu chưa biết.
Xem [API, schema report, phạm vi đo và giới hạn](docs/benchmark_runner.md).

## Chạy toàn bộ unit tests

Từ thư mục gốc repository:

```powershell
python -B -m unittest discover -s tests -v
```

`-B` ngăn tạo cache `.pyc`. Tests chạy offline, dùng XML/archive giả lập,
mock HTTP/clock; các file tạm của tests nằm ngoài repository.


## Quy trình vận hành và Sổ tay Google Colab CPU-only

Dự án tổ chức các sổ tay trên Google Colab CPU-only theo cấu trúc phân định rõ ràng giữa quy trình sản xuất chính thức và các quy trình thực nghiệm:

### 1. Quy trình sản xuất chính thức (Official Production Entry Point)
- **Sổ tay:** [`notebooks/m1_colab_fast_production.ipynb`](notebooks/m1_colab_fast_production.ipynb)
- **Vai trò:** Quy trình thu thập dữ liệu chính thức duy nhất của Nhóm 10 cho toàn bộ 200 lô (20.000 định danh arXiv).
- **Đặc tính kỹ thuật:**
  - Thực thi hoàn toàn trên Google Colab **CPU-only** (Accelerator: None).
  - Ghi dữ liệu trên NVMe SSD cục bộ (`/content/fast_crawling_workspace`), giải nén và lọc TeX/Bib trên RAM disk (`/dev/shm`).
  - Nạp trước siêu dữ liệu từ Kaggle snapshot (`arxiv-metadata-oai-snapshot.json`) giúp giảm hàng chục nghìn request Discovery.
  - Bật mặc định `revision_history=True` trích xuất đầy đủ ngày nộp của mọi phiên bản.
  - Tích hợp giai đoạn làm giàu dữ liệu Semantic Scholar (`enrich_cohort_s2`) tuân thủ nghiêm ngặt giới hạn tốc độ.
  - Đóng gói sao lưu `.tar.gz` kèm mã băm `.sha256` lên Google Drive sau mỗi lô 100 bài, hỗ trợ khôi phục tiến trình an toàn khi ngắt kết nối.
  - Xuất bảng báo cáo kiểm toán M1 và sinh tập dữ liệu đầu ra chuẩn hóa dạng Parquet (`papers.parquet`, `references.parquet`) và CSV.
- **Tài liệu hướng dẫn:** Xem [Hướng dẫn vận hành Fast Production](docs/fast_crawling_guide.md).

### 2. Quy trình đối chứng chuẩn hóa (Baseline Benchmark Reference)
- **Sổ tay:** [`notebooks/m1_colab_arxiv_production.ipynb`](notebooks/m1_colab_arxiv_production.ipynb)
- **Vai trò:** Quy trình tham chiếu chuẩn mực ban đầu (arXiv-first, ghi trực tiếp lên Drive FUSE), được duy trì làm mốc so sánh hiệu năng (baseline) cho báo cáo kỹ thuật.
- **Tài liệu hướng dẫn:** Xem [Tài liệu vận hành Baseline Production](docs/m1_production.md).

### 3. Sổ tay thực nghiệm và kiểm chứng (Validation & Experiments)
- **`notebooks/m1_colab_cpu_pilot.ipynb`:** Kiểm chứng filesystem Drive, đo đạc độ trễ I/O và kiểm tra tính năng checkpoint/resume độc lập.
- **`notebooks/m1_colab_strategy_comparison.ipynb`:** Thực nghiệm so sánh có đối chứng A/B giữa chiến lược thuần arXiv và chiến lược kết hợp Semantic Scholar.
