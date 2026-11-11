# Hướng dẫn chạy M1 trên Colab

Mọi thành viên dùng [sổ tay M1](../notebooks/m1_pipeline_thu_thap_va_kiem_toan.ipynb) và dải ID trong [`config/group10_id_ranges.json`](../config/group10_id_ranges.json).

## Cách chạy

1. Mở sổ tay trong Colab, chọn CPU và kết nối Google Drive.
2. Ở cell cấu hình, nhập MSSV và giữ `COHORT_LIMIT = None` để chạy đủ 5.000 ID. Dùng `1` cho pilot 100 ID; chế độ này tự dùng root Drive/local riêng với corpus đầy đủ.
3. Chạy cell cấu hình để checkout commit runtime đã ghim, cài thư viện và tạo fingerprint của danh sách.
4. Chạy cell arXiv để xác minh ID, lịch sử phiên bản và source.
5. Trong Colab Secrets, thêm `SEMANTIC_SCHOLAR_API_KEY`, bật Notebook access rồi chạy cell S2. S2 có thể chạy trước arXiv; nó dùng checkpoint độc lập.
6. Sau khi hai pha đủ bằng chứng, chạy cell kiểm toán/đối soát. Bảng chỉ được xuất nếu phạm vi đạt M1.

## Khi phiên dừng

Stop, lỗi mạng, hết ngân sách hoặc 403/429 giữ checkpoint của đúng pha. Chờ dịch vụ ổn định rồi chạy lại cell tương ứng bằng cùng MSSV, phạm vi và Drive. Không xóa checkpoint và không chạy hai tiến trình cùng ghi một MSSV. Nếu S2 chạy trước arXiv, kiểm toán sẽ chờ source; S2 `not_found` không được dùng để kết luận arXiv không tồn tại.

## Đọc kết quả

Cell arXiv chỉ xác nhận trạng thái source. Cell S2 chỉ xác nhận metadata/references S2. Cell kiểm toán ghi fingerprint, lỗi, xung đột, số liệu và trạng thái xuất; khi bị chặn, xem báo cáo `m1_audit_*.json` trong Drive.

Pilot 100 ID không đại diện cho benchmark cuối. Benchmark cần chạy CPU Colab trên đủ 20.000 ID của nhóm, giữ log từng phiên và cộng thời gian retry/delay; khoảng nghỉ giữa phiên được loại khỏi runtime. Xem [quy trình M1](operations/m1_production.md) và [hướng dẫn benchmark](operations/benchmark_runner.md).
