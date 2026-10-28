# arXiv — Group 10

Đồ án môn Introduction to Data Science, HCMUS — Milestone 1: Data Crawling.
Hiện có module sinh arXiv ID, Entry Discovery và tải source theo từng version.
Source downloader được kiểm thử offline, chưa thử tải source thật.
Chưa tích hợp Semantic Scholar hoặc pipeline crawl hàng loạt.

## Cấu trúc repository

```text
config/group10_id_ranges.json  Khoảng ID theo MSSV
src/__init__.py
src/id_ranges.py               Đọc cấu hình, kiểm tra và sinh ID
src/arxiv_discovery.py         Phân tích Atom XML và xác định version
src/arxiv_client.py            HTTP client cho một ID
src/arxiv_source.py            Tải source và trích xuất TeX/BibTeX an toàn
tests/test_id_ranges.py        Unit tests offline
tests/test_arxiv_discovery.py  Unit tests discovery bằng XML giả lập
tests/test_arxiv_client.py     Unit tests HTTP và thời gian giả lập
tests/test_arxiv_source.py     Unit tests downloader và archive giả lập
docs/id_ranges.md             Hướng dẫn module ID
docs/arxiv_discovery.md        Giao diện và trạng thái discovery
docs/arxiv_client.md           HTTP, retry và kế hoạch thử thủ công
docs/arxiv_source.md           Cách dùng và trạng thái source downloader
.gitignore
LICENSE
README.md
```

## Chạy module ID

Dùng Python 3.10 trở lên; đã kiểm thử trên Python 3.14.1. Chỉ cần thư viện
chuẩn Python, không cần cài package. Chạy từ thư mục gốc repository:

```powershell
python -B -m src.id_ranges
```

Lệnh trên hiển thị cấu hình và hai mốc đã định dạng. Để sinh từng ID từ Python:

```python
from src.id_ranges import iter_assigned_ids

ids = iter_assigned_ids()
print(next(ids))  # ('24120010', '2508.07872')
```

Hàm dùng generator, bao gồm cả hai đầu khoảng và chỉ hỗ trợ khoảng cùng tháng.
Cấu hình hiện tại có 5.000 vị trí mỗi thành viên, tổng **20.000 ID ứng viên**;
không đồng nghĩa 20.000 publication hợp lệ. Xem [hướng dẫn chi tiết](docs/id_ranges.md).

## Chạy unit tests

```powershell
python -B -m unittest discover -s tests -v
```

`-B` ngăn tạo cache `.pyc`. Toàn bộ kiểm thử chạy offline; discovery dùng XML giả lập.
Xem [hướng dẫn Entry Discovery](docs/arxiv_discovery.md).
HTTP client và kế hoạch thử API thật được mô tả trong [hướng dẫn HTTP](docs/arxiv_client.md).
Xem [hướng dẫn source downloader](docs/arxiv_source.md) để xử lý archive offline
và gọi tải từng version. API và source dùng chung pacing trong một process.
