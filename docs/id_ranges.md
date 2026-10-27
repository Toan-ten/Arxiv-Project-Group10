# Cấu hình khoảng arXiv ID — Group 10


## Chạy trong PowerShell

Dùng Python 3.10 trở lên và thư viện chuẩn; đã kiểm thử trên Python 3.14.1.
Mở terminal tại thư mục gốc repository:

```powershell
python -B -m src.id_ranges
python -B -m unittest discover -s tests -v
```

Lệnh đầu in JSON gồm phân công, dữ liệu gốc và hai mốc đã định dạng. Lệnh sau
chạy kiểm thử; `OK` nghĩa là tất cả kiểm thử đạt. `-B` ngăn tạo file bộ nhớ đệm
`.pyc`.

Để chỉ định file cấu hình:

```powershell
python -B -m src.id_ranges config/group10_id_ranges.json
```

Nếu cấu hình sai, chương trình in lỗi và thoát với mã 2.

## Dùng từ Python

```python
from src.id_ranges import load_config

config = load_config()
toan = config["24120232"]
print(toan.start_arxiv_id)  # 2502.08235
```

Để duyệt ID cùng MSSV phụ trách:

```python
from src.id_ranges import iter_assigned_ids

ids = iter_assigned_ids()
print(next(ids))  # ('24120010', '2508.07872')
print(next(ids))  # ('24120010', '2508.07873')
```

Hàm dùng `yield` để sinh từng cặp `(mssv, arxiv_id)`, không tạo sẵn danh sách.
Có thể truyền cấu hình đã đọc: `iter_assigned_ids(load_config())`.
Các thành viên được duyệt theo thứ tự trong cấu hình, ID tăng dần trong mỗi
khoảng. Cấu hình hiện tại sinh 5.000 ID mỗi MSSV, tổng 20.000 ID không trùng.

## Cấu hình

`config/group10_id_ranges.json` chỉ có danh sách `members`. Mỗi phần tử có
đúng năm trường: `mssv`, `start_month`, `start_id`, `end_month`, `end_id`.
MSSV là chuỗi chữ số, không được trùng. `load_config()` trả về dictionary tra
cứu bằng MSSV, không phụ thuộc thứ tự trong danh sách.

Ví dụ `2025-08` và `7872` được định dạng thành `2508.07872`; số gốc vẫn là
`7872`. Lệnh chạy module in thêm `start_arxiv_id` và `end_arxiv_id` để xem kết
quả, không ghi các trường này vào file cấu hình.

Tháng có dạng `YYYY-MM`, từ `2015-01` đến `2099-12`; ID là số nguyên trong
`1..99999`, không chấp nhận boolean. Mốc đầu không được sau mốc cuối. Cấu hình
phải đủ bốn thành viên, đúng tên trường, không trùng MSSV hoặc khóa JSON.
File dùng UTF-8, có thể có BOM; đường dẫn mặc định dựa vào vị trí module.

Các khoảng được duyệt **bao gồm cả hai đầu mút**; hai mốc bằng nhau sinh đúng
một ID. Hàm chỉ hỗ trợ khoảng cùng tháng. Khoảng khác tháng/năm gây
`ConfigError` khi bắt đầu duyệt generator, trước khi sinh bất kỳ ID nào.
Mã arXiv cũ và hậu tố version không được hỗ trợ. Định dạng hợp lệ không xác
nhận paper tồn tại trên arXiv; 20.000 ID ứng viên không đồng nghĩa 20.000
publication hợp lệ.
