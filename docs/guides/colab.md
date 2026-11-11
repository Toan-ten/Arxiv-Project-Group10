# Vận hành M1 trên Colab

Thực hiện các bước trong [hướng dẫn chạy M1](../M1_COLAB_MEMBER_GUIDE.md). Sổ tay dùng một luồng bắt buộc cho mọi thành viên: chuẩn bị ứng viên Kaggle, xác minh qua arXiv, tải mã nguồn từng phiên bản, thu thập lịch sử phiên bản, lấy siêu dữ liệu và tài liệu tham khảo từ Semantic Scholar, rồi kiểm toán.

## Điều kiện chạy

- Chọn CPU; mỗi corpus chỉ có một tiến trình ghi.
- Lưu `KAGGLE_API_TOKEN` và `SEMANTIC_SCHOLAR_API_KEY` trong Colab Secrets. Không chép khóa vào mã hoặc báo cáo.
- Dùng MSSV trong `config/group10_id_ranges.json`. Mặc định chạy đủ 50 lô của thành viên; `COHORT_LIMIT` chỉ giới hạn số lô đầu trong cùng luồng.
- Kaggle chỉ cung cấp dữ liệu ứng viên. arXiv xác nhận bài báo và phiên bản; khi snapshot không dùng được, quy trình tiếp tục bằng arXiv.

## Dừng và tiếp tục

Ngân sách phiên mặc định là 10 giờ. Thời gian chờ và thử lại API được tính vào thời gian xử lý. Khi chạm ngân sách hoặc gặp lỗi chính sách, quy trình lưu checkpoint và kết thúc lượt chạy. Sau khi dịch vụ ổn định, chạy lại sổ tay với cùng MSSV, phạm vi và Drive. Không gửi yêu cầu liên tục trong lúc API còn giới hạn.

## Dữ liệu đầu ra

Quy ước ánh xạ ngày tham khảo và xử lý tệp `.bbl` được nêu trong [hợp đồng dữ liệu](../contracts/source_data_contracts.md).

Phép đo cuối cần nhật ký các phiên của toàn nhóm và đủ 20.000 vị trí ID. Mẫu kiểm tra không đại diện cho kết quả toàn nhóm. Xem [hướng dẫn tổng hợp benchmark](../operations/benchmark_runner.md).
