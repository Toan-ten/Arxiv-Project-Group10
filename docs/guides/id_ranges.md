# Phân công dải arXiv ID

`config/group10_id_ranges.json` định nghĩa dải ID của từng thành viên. `load_config()` kiểm tra cấu hình; `iter_assigned_ids()` lần lượt tạo các cặp `(mssv, arxiv_id)` theo thứ tự cấu hình.

## Xem cấu hình

Từ thư mục gốc repository, chạy:

```bash
python -m src.id_ranges
```

Để chạy kiểm thử:

```bash
pytest tests/test_id_ranges.py
```

Có thể chỉ định tệp cấu hình khi chạy module:

```bash
python -m src.id_ranges config/group10_id_ranges.json
```

## Duyệt dải bằng Python

```python
from src.id_ranges import iter_assigned_ids, load_config

config = load_config()
for mssv, arxiv_id in iter_assigned_ids(config):
    print(mssv, arxiv_id)
```

Hàm sinh từng cặp, không tạo toàn bộ danh sách trước. Các khoảng ID bao gồm cả hai đầu mút; cấu hình hiện tại có bốn thành viên, mỗi người 5.000 vị trí, tổng cộng 20.000 vị trí không trùng.

## Quy tắc định dạng

Một mục cấu hình gồm `mssv`, `start_month`, `start_id`, `end_month` và `end_id`. Tháng dùng dạng `YYYY-MM`; hai đầu dải phải thuộc cùng tháng. ID được định dạng thành `YYMM.NNNNN`, còn khóa thư mục có dạng `YYMM-NNNNN`.

`arxiv_id_to_folder_key()` nhận ID arXiv năm chữ số từ tháng 01/2015 đến tháng 12/2099, kể cả ID không nằm trong bốn dải phân công. Hàm không nhận ID có dấu gạch chéo hoặc hậu tố `vN`. Hàm chỉ chuyển định dạng khóa thư mục; quyền sở hữu ID phải được kiểm tra riêng theo cấu hình. ID đúng định dạng cũng không chứng minh bài báo tồn tại trên arXiv.

| MSSV | Bắt đầu | Kết thúc | Số vị trí |
|---|---|---|---:|
| `24120010` | `2508.07872` | `2508.12871` | 5.000 |
| `24120035` | `2507.16659` | `2507.21658` | 5.000 |
| `24120232` | `2502.08235` | `2502.13234` | 5.000 |
| `24120260` | `2501.02642` | `2501.07641` | 5.000 |
