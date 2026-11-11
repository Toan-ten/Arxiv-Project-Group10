# Vận hành M1 trên Colab

Thực hiện các bước trong [hướng dẫn chạy M1](../M1_COLAB_MEMBER_GUIDE.md). Sổ tay dùng một luồng bắt buộc cho mọi thành viên: chuẩn bị ứng viên Kaggle, xác minh qua arXiv, tải mã nguồn từng phiên bản, thu thập lịch sử phiên bản, lấy siêu dữ liệu và tài liệu tham khảo từ Semantic Scholar, rồi kiểm toán.

## Điều kiện chạy

- Chọn CPU; mỗi corpus chỉ có một tiến trình ghi.
- Lưu `KAGGLE_API_TOKEN` và `SEMANTIC_SCHOLAR_API_KEY` trong Colab Secrets. Không chép khóa vào mã hoặc báo cáo.
- Dùng MSSV trong `config/group10_id_ranges.json`. Mặc định chạy đủ 50 lô của thành viên; `COHORT_LIMIT` chỉ giới hạn số lô đầu trong cùng luồng.
- Kaggle chỉ cung cấp dữ liệu ứng viên. arXiv xác nhận bài báo và phiên bản; khi snapshot không dùng được, quy trình tiếp tục bằng arXiv.
- Khi tải snapshot, ô Colab báo lúc bắt đầu và nhắc trạng thái mỗi 30 giây; thời hạn tải là 60 phút. Nếu dừng thủ công, tiến trình tải bị dừng và tệp tạm được dọn. Lần chạy sau có thể phải tải lại nếu snapshot chưa hoàn tất.
- Ô 3 in thời điểm bắt đầu và kết thúc từng giai đoạn. Khi tải mã nguồn hoặc lấy dữ liệu S2, ô hiển thị tiến độ theo lô, cập nhật ID đang xét và nhắc trạng thái sau mỗi 30 giây.

## Dừng và tiếp tục

Ngân sách phiên mặc định là 10 giờ. Thời gian chờ và thử lại API được tính vào thời gian xử lý. Khi chạm ngân sách hoặc gặp lỗi chính sách, quy trình lưu checkpoint và kết thúc lượt chạy. Sau khi dịch vụ ổn định, chạy lại sổ tay với cùng MSSV, phạm vi và Drive. Không gửi yêu cầu liên tục trong lúc API còn giới hạn.

## Dữ liệu đầu ra

Quy ước ánh xạ ngày tham khảo và xử lý tệp `.bbl` được nêu trong [hợp đồng dữ liệu](../contracts/source_data_contracts.md).

Các phép đo kích thước nguồn so sánh tổng byte file thường sau giải nén trước lọc với byte file nguồn được giữ; bước lọc cũng loại các file không phải hình. Kích thước archive nén được ghi riêng. Các phiên bản cũ thiếu số đo trước lọc phải giữ trạng thái chưa đo.

Thời gian theo bài gồm khoảng đo source và S2 ghi trong các quan sát; phần chuẩn bị snapshot, sao lưu archive, kiểm toán và xuất bảng là chi phí dùng chung và được báo riêng. Tỷ lệ metadata reference tính trên reference của các danh sách đã hoàn tất, với điều kiện có tiêu đề, tác giả, ngày nộp hoặc công bố và ID Semantic Scholar; arXiv ID không bắt buộc.

Phép đo cuối cần nhật ký các phiên của toàn nhóm và đủ 20.000 vị trí ID. Mẫu kiểm tra không đại diện cho kết quả toàn nhóm. Xem [hướng dẫn tổng hợp benchmark](../operations/benchmark_runner.md).
