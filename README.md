# arXiv — Group 10

Đồ án môn Introduction to Data Science, HCMUS — Milestone 1: Data Crawling.
Hiện có module quản lý ID, Entry Discovery, HTTP Client, Source Downloader
và bộ đo benchmark nền. Các module crawler được kiểm thử offline, chưa thử
tải source thật. Storage + Checkpoint/Resume giai đoạn 1 đã có và được kiểm thử offline.
Chưa tích hợp Semantic Scholar hoặc pipeline crawl hàng loạt;
chưa có benchmark M1 thực nghiệm.

## Cấu trúc repository

```text
config/group10_id_ranges.json  Khoảng ID theo MSSV
src/id_ranges.py              Đọc cấu hình, kiểm tra và sinh ID
src/arxiv_discovery.py        Phân tích Atom XML và xác định version
src/arxiv_client.py           HTTP client cho một ID
src/arxiv_source.py           Tải source và trích xuất TeX/BibTeX an toàn
src/storage.py                Lưu JSON, kiểm tra source và checkpoint/resume offline
src/benchmark.py              Bộ đo thời gian, RAM và dung lượng corpus
docs/                         Hướng dẫn module và contract M1
tests/                        Unit tests offline cho các module
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

## Contract và benchmark foundation

[Contract M1](docs/m1_contract.md) mô tả layout corpus, schema
`metadata.json`, `references.json`, `manifest.json`, trạng thái và log.
Contract phiên bản 2 đã được đối chiếu với giao diện crawler hiện tại.
Storage + Checkpoint/Resume giai đoạn 1 đã được triển khai theo contract;
CLI, Semantic Scholar và bước export vẫn chưa có.

[Hướng dẫn Storage](docs/storage.md) mô tả giao diện, atomic JSON, checksum
và kế hoạch resume. Storage không gọi mạng và không tạo sẵn thư mục version;
source có sẵn nhưng thiếu checkpoint hợp lệ được giữ nguyên để audit.

`benchmark_pipeline(crawler_main_func, ...)` trong `src/benchmark.py` bọc một
hàm crawler để đo wall time, RAM đầu/cuối/đỉnh và kích thước corpus cuối,
rồi ghi báo cáo cùng môi trường và commit. Khi có `psutil`, bộ đo lấy RAM
của process và các process con; fallback phụ thuộc hệ điều hành.

Bộ đo nền chưa có RAM trung bình, disk peak hoặc thời gian từng paper,
trung bình mỗi paper và discovery.
Entry point `python -m src.benchmark` hiện chạy dummy crawler, không phải
benchmark M1 thực nghiệm trên Colab CPU-only.

## Chạy toàn bộ unit tests

Từ thư mục gốc repository:

```powershell
python -B -m unittest discover -s tests -v
```

`-B` ngăn tạo cache `.pyc`. Tests chạy offline, dùng XML/archive giả lập,
mock HTTP/clock; các file tạm của tests nằm ngoài repository.
