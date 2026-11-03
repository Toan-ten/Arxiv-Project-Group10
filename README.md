# arXiv — Group 10

Đồ án môn Introduction to Data Science, HCMUS — Milestone 1: Data Crawling.
Hiện có module kiểm tra cấu hình và sinh arXiv ID offline. Crawler và phần
tích hợp API arXiv/Semantic Scholar chưa được triển khai.

## Cấu trúc repository

```text
config/group10_id_ranges.json  Khoảng ID theo MSSV
src/__init__.py
src/id_ranges.py               Đọc cấu hình, kiểm tra và sinh ID
src/benchmark.py               Đo thời gian, RAM và dung lượng corpus
tests/test_id_ranges.py        Unit tests offline cho module ID
tests/test_benchmark.py        Unit tests offline cho benchmark
docs/id_ranges.md              Hướng dẫn module ID
docs/m1_contract.md            Hợp đồng output của crawler M1
.gitignore
LICENSE
README.md
```

## Chạy module ID

Dùng Python 3.10 trở lên; đã kiểm thử trên Python 3.14. Chỉ cần thư viện
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
không đồng nghĩa 20.000 publication hợp lệ. Dùng
`arxiv_id_to_folder_key("2508.07872")` để lấy `2508-07872`. Xem
[hướng dẫn chi tiết](docs/id_ranges.md) và
[hợp đồng output M1](docs/m1_contract.md).

## Chạy unit tests

```powershell
python -B -m unittest discover -s tests -v
```

`-B` ngăn tạo cache `.pyc`. Bộ kiểm thử gồm 37 tests, chạy hoàn toàn offline.
