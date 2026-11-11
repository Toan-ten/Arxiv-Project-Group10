# Vận hành M1 trên Colab

Thực hiện các bước trong [hướng dẫn chạy M1](../M1_COLAB_MEMBER_GUIDE.md). Notebook dùng danh sách ID tất định theo MSSV, chia 50 cohort × 100 ID, với ba bước vận hành: arXiv, Semantic Scholar, kiểm toán/đối soát.

## Điều kiện chạy

Notebook ghim một commit runtime cụ thể. Commit runtime phải có trên remote mà notebook clone trước khi chạy Colab. Khi cập nhật runtime, commit mã trước, đổi pin notebook sang SHA đó trong commit notebook kế tiếp, rồi chạy lại cell cấu hình trên runtime Colab mới.

- Chọn CPU; mỗi corpus chỉ có một tiến trình ghi.
- Dùng MSSV trong `config/group10_id_ranges.json`. Mặc định xử lý đủ 50 lô; `COHORT_LIMIT=1` dành cho pilot 100 ID và tự dùng root Drive/local riêng.
- Cell arXiv xác minh ID, lịch sử phiên bản và tải source. Cell S2 truy vấn cùng danh sách ID độc lập, kể cả khi arXiv chưa chạy. Cell kiểm toán yêu cầu fingerprint khớp và xác minh checkpoint trước khi nhập S2 vào corpus.
- Lưu `SEMANTIC_SCHOLAR_API_KEY` trong Colab Secrets. Không chép khóa vào mã hoặc báo cáo. arXiv không cần khóa Kaggle để thực hiện discovery trực tiếp theo ID.
- Các pha ghi checkpoint riêng, còn khóa writer theo MSSV ngăn nhiều runtime cùng ghi vào một phạm vi. Chạy arXiv → S2 → kiểm toán theo thứ tự mặc định; có thể chạy S2 trước nếu cần.

## Dừng và tiếp tục

Ngân sách mỗi cell mặc định là 10 giờ. Thời gian chờ và thử lại API được tính vào runtime. Stop, hết ngân sách, lỗi mạng hoặc 403/429 giữ checkpoint của đúng pha; sau khi dịch vụ ổn định, chạy lại cell đó với cùng MSSV, phạm vi và Drive. S2 `not_found` chỉ là kết quả S2, không xác nhận arXiv không tồn tại. Nếu một pha chưa có đủ bằng chứng hoặc checkpoint không tương thích, kiểm toán chặn xuất bảng.

## Dữ liệu đầu ra

Quy ước ánh xạ ngày tham khảo và xử lý tệp `.bbl` được nêu trong [hợp đồng dữ liệu](../contracts/source_data_contracts.md).

Các phép đo kích thước nguồn so sánh tổng byte file thường sau giải nén trước lọc với byte file nguồn được giữ; bước lọc cũng loại các file không phải hình. Kích thước archive nén được ghi riêng. Các phiên bản cũ thiếu số đo trước lọc phải giữ trạng thái chưa đo.

Thời gian source/S2 được ghi riêng từng cohort và từng phiên; retry/delay nằm trong thời gian pha. Tổng runtime cộng các phiên chạy tuần tự, loại khoảng nghỉ giữa phiên và không đếm lại paper chỉ được đọc từ checkpoint. RAM và disk có phạm vi ghi trong từng stage observation. Nếu không có số đo đáng tin cậy, báo thiếu thay vì ước tính.

Phép đo cuối cần nhật ký các phiên của toàn nhóm và đủ 20.000 vị trí ID. Mẫu kiểm tra không đại diện cho kết quả toàn nhóm. Xem [hướng dẫn tổng hợp benchmark](../operations/benchmark_runner.md).
