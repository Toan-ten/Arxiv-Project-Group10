# arXiv — Group 10

Đồ án môn Introduction to Data Science, HCMUS — Milestone 1: Data Crawling.
Hiện có module kiểm tra cấu hình và sinh arXiv ID offline. Crawler và phần
tích hợp API arXiv/Semantic Scholar chưa được triển khai.

## Cấu trúc repository

```text
config/group10_id_ranges.json  Khoảng ID theo MSSV
config/pilot_10.json           Bộ ID smoke test
config/pilot_500.json          Bộ ID pilot/benchmark
scripts/make_pilots.py         Sinh lại hai bộ pilot
src/__init__.py
src/id_ranges.py               Đọc cấu hình, kiểm tra và sinh ID
src/pilots.py                  Chọn pilot deterministic từ các range
tests/test_id_ranges.py        Unit tests offline
tests/test_pilot_sets.py       Unit tests cho pilot sets
docs/id_ranges.md              Hướng dẫn module ID
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

## Chạy unit tests

```powershell
python -B -m unittest discover -s tests -v
```

`-B` ngăn tạo cache `.pyc`. Bộ kiểm thử chạy hoàn toàn offline.
