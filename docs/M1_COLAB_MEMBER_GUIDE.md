# Hướng dẫn chạy M1 trên Colab

Mọi thành viên dùng [sổ tay M1](../notebooks/m1_pipeline_thu_thap_va_kiem_toan.ipynb) và dải ID trong [`config/group10_id_ranges.json`](../config/group10_id_ranges.json).

## Cách chạy

1. Mở sổ tay trong Colab, chọn CPU và kết nối Google Drive.
2. Chạy ô thiết lập. Sổ tay checkout commit đã ghim, kiểm tra mã nguồn rồi cài thư viện.
3. Nhập MSSV. Giữ `COHORT_LIMIT = None` để chạy đủ 5.000 ID; đặt giới hạn nhỏ hơn để chạy mẫu trên cùng quy trình.
4. Trong Colab Secrets, thêm `KAGGLE_API_TOKEN` và `SEMANTIC_SCHOLAR_API_KEY`, rồi bật Notebook access.
5. Chạy ô M1. Sổ tay tự thực hiện source, lịch sử phiên bản, S2, lưu checkpoint và kiểm toán.

## Khi phiên dừng

Lỗi 403/429, hết ngân sách phiên hoặc lỗi toàn vẹn sẽ dừng lượt chạy và lưu trạng thái có thể tiếp tục. Với 429, chờ dịch vụ ổn định rồi chạy lại sổ tay bằng cùng MSSV, phạm vi và thư mục Drive. Không xóa checkpoint và không chạy hai tiến trình cùng ghi một corpus.

## Đọc kết quả

`handoff_status = verified` xác nhận lô đã qua kiểm tra bàn giao. Nếu trạng thái là `blocked`, xem `audit`, `stop_reason` và báo cáo trong Drive. `source_complete` chỉ nói về mã nguồn; M1 còn cần metadata và references đầy đủ. Xem [quy trình M1](operations/m1_production.md) để hiểu các trạng thái.
