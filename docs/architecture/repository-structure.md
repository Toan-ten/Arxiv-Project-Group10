# Cấu trúc và thành phần của kho mã

Kho mã chứa notebook Colab M1, cấu hình dải ID, mã xử lý, công cụ tổng hợp, kiểm thử ngoại tuyến và tài liệu vận hành. Bảng dưới đây nêu vai trò các thành phần chính; danh sách tệp cụ thể có thể thay đổi theo commit.

```text
Arxiv-Project-Group10/
├── config/
│   ├── group10_id_ranges.json       # Phân công 20.000 vị trí ID
│   └── pilots/                      # Tập ID mẫu
├── docs/
│   ├── architecture/                # Cấu trúc kho mã
│   ├── contracts/                   # Hợp đồng dữ liệu, lưu trữ và thống kê
│   ├── guides/                      # Hướng dẫn các thành phần
│   └── operations/                  # Vận hành và đo lường
├── notebooks/
│   └── m1_pipeline_thu_thap_va_kiem_toan.ipynb
├── scripts/                         # Kiểm toán, tổng hợp và báo cáo
├── src/                             # API, quy trình, lưu trữ và kiểm toán
└── tests/                           # Kiểm thử ngoại tuyến
```

## Quy tắc tổ chức

- `config/group10_id_ranges.json` là nguồn duy nhất cho phân công ID. Notebook không gán mặc định MSSV hoặc lô.
- Notebook gọi một bộ điều phối M1; mã xử lý và kiểm tra toàn vẹn nằm trong `src/`; công cụ kiểm toán và tổng hợp nằm trong `scripts/`.
- Chạy mẫu và tiếp tục từ điểm khôi phục dùng cùng luồng M1.
- Kiểm thử không gọi mạng thật. Tên API, schema, trường dữ liệu và mã trạng thái được giữ nguyên.
- Tài liệu trong kho mã dùng tiếng Việt. Nội dung cần khớp hành vi mã hiện tại và không thay đổi hợp đồng dữ liệu chỉ vì biên tập.

## Thành phần và kiểm thử liên quan

| Thành phần | Vai trò | Kiểm thử liên quan |
|---|---|---|
| `config/group10_id_ranges.json` | Phân công 20.000 vị trí ID cho bốn thành viên | `tests/test_id_ranges.py` |
| `config/pilots/` | Tập con ID dùng để kiểm tra quy trình | `tests/test_pilot_sets.py` |
| `notebooks/m1_pipeline_thu_thap_va_kiem_toan.ipynb` | Điểm vào Colab duy nhất cho M1 | `tests/test_colab_release_qualification.py` |
| `src/` | Xác minh arXiv, tải source, lấy dữ liệu S2, lưu trữ, kiểm toán và đo lường | Kiểm thử ngoại tuyến theo mô-đun |
| `scripts/aggregate_m1_group.py` | Kiểm tra độ bao phủ ID và tính toàn vẹn archive | `tests/test_group_aggregator.py` |
| `scripts/aggregate_m1_group_benchmark.py` | Tổng hợp phép đo theo thành viên và phiên | `tests/test_group_benchmark_aggregator.py` |
| `scripts/generate_m1_report.py` | Tạo báo cáo thống kê và kiểm toán M1 | `tests/test_generate_m1_report.py` |
| `docs/contracts/` | Định nghĩa dữ liệu, lưu trữ và thống kê | Mã lưu trữ và kiểm thử hợp đồng |
| `docs/guides/`, `docs/operations/` | Hướng dẫn thành phần và vận hành | README và kiểm thử tương ứng |
| `tests/` | Kiểm thử ngoại tuyến, không gọi dịch vụ thật | `pytest` |

## Bảo trì

- Chỉ gỡ thành phần sau khi kiểm tra tham chiếu trong mã, notebook, tài liệu và kiểm thử.
- Không xóa dữ liệu, archive, log hoặc bằng chứng kiểm toán để dọn mã.
- Giữ nguyên schema và cách tính chỉ số khi chỉ biên tập tài liệu. Nếu định nghĩa chỉ số thay đổi, cập nhật hợp đồng và kiểm thử tương ứng.
