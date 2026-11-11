# Cấu trúc kho mã

Repository chứa một quy trình Colab M1, cấu hình dải ID, mã xử lý, kiểm thử ngoại tuyến và tài liệu vận hành.

```text
Arxiv-Project-Group10/
├── config/
│   ├── group10_id_ranges.json       # Phân công 20.000 vị trí ID
│   └── pilots/                      # Tập ID mẫu
├── docs/
│   ├── architecture/                # Cấu trúc repository
│   ├── contracts/                   # Hợp đồng dữ liệu, lưu trữ và thống kê
│   ├── guides/                      # Hướng dẫn các thành phần
│   └── operations/                  # Vận hành và đo lường
├── notebooks/
│   └── m1_pipeline_thu_thap_va_kiem_toan.ipynb
├── scripts/                         # Kiểm toán, tổng hợp và báo cáo
├── src/                             # API, pipeline, lưu trữ và kiểm toán
└── tests/                           # Kiểm thử ngoại tuyến
```

## Quy tắc tổ chức

- `config/group10_id_ranges.json` là nguồn duy nhất cho phân công ID. Notebook không gán mặc định MSSV hoặc lô.
- Notebook gọi một bộ điều phối M1; mã xử lý và kiểm tra toàn vẹn nằm trong `src/`; công cụ kiểm toán và tổng hợp nằm trong `scripts/`.
- Chạy mẫu và tiếp tục từ checkpoint dùng cùng luồng M1.
- Kiểm thử không gọi mạng thật. Tên API, schema, trường dữ liệu và mã trạng thái được giữ nguyên.
- Tài liệu trong kho mã dùng tiếng Việt. Nội dung cần khớp hành vi mã hiện tại và không thay đổi hợp đồng dữ liệu chỉ vì biên tập.
