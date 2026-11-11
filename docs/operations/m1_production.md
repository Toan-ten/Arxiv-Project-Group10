# Quy trình thu thập M1

## Luồng xử lý

Mọi thành viên chạy [sổ tay M1 trên Colab](../../notebooks/m1_pipeline_thu_thap_va_kiem_toan.ipynb). Phân công lấy từ `config/group10_id_ranges.json`; mỗi thành viên xử lý 50 lô, tương ứng 5.000 vị trí ID. Toàn nhóm có 20.000 vị trí được giao; đây không phải số bài báo chắc chắn tồn tại.

Một lượt chạy chuẩn bị snapshot và chỉ mục Kaggle, xác minh ID và danh sách phiên bản bằng arXiv, tải mã nguồn, thu thập lịch sử phiên bản, lấy siêu dữ liệu và tài liệu tham khảo qua Semantic Scholar, lưu checkpoint rồi kiểm toán. S2 là giai đoạn bắt buộc để đạt M1. Hai bảng `papers` và `references` chỉ được xuất sau khi kiểm toán bàn giao đạt.

## Dữ liệu nguồn

- arXiv xác minh bài báo và phiên bản. Mỗi phiên bản được tải riêng; corpus chỉ giữ `.tex`, `.bib` và `.bbl`, không giữ tệp nhị phân.
- Kaggle cung cấp snapshot ứng viên. Quy trình chỉ dùng ngày phiên bản khi ID, thứ tự phiên bản, ngày và xuất xứ khớp với kết quả arXiv đã xác minh. Nếu không, quy trình lấy lịch sử từ arXiv.
- Semantic Scholar cung cấp siêu dữ liệu và tài liệu tham khảo theo thời điểm truy vấn. `ref_submission_date` có thể lấy từ `publicationDate`; báo cáo phải giữ xuất xứ và không diễn giải giá trị này thành ngày nộp arXiv.
- Quy tắc xử lý `.bbl` và ánh xạ ngày tham khảo được định nghĩa trong [hợp đồng dữ liệu nguồn](../contracts/source_data_contracts.md).

## Dừng và tiếp tục

Mỗi phiên có ngân sách mềm mặc định 10 giờ. Giãn cách yêu cầu, thời gian chờ và thử lại trong các giai đoạn được tính vào thời gian phiên. Khi chạm ngân sách hoặc gặp lỗi chính sách, quy trình lưu checkpoint rồi kết thúc lượt chạy. Với HTTP 429, chờ dịch vụ ổn định rồi chạy lại cùng sổ tay, MSSV, phạm vi và Drive. Không chạy nhiều tiến trình ghi trên cùng corpus.

Lỗi toàn vẹn, dữ liệu chưa xác minh, mã nguồn thiếu, siêu dữ liệu thiếu hoặc tài liệu tham khảo chưa lấy đủ đều chặn bàn giao. `source_complete` chỉ mô tả giai đoạn mã nguồn; nó không thay thế trạng thái M1.

## Kiểm toán và tổng hợp

Báo cáo gắn với phiên, commit, lô, checksum snapshot và checksum archive. Mỗi thành viên giữ nhật ký Colab; [`aggregate_m1_group.py`](../../scripts/aggregate_m1_group.py) kiểm tra độ bao phủ và archive, còn [`aggregate_m1_group_benchmark.py`](../../scripts/aggregate_m1_group_benchmark.py) tổng hợp thời gian và tài nguyên. Chỉ công bố kết quả cuối khi có dữ liệu và nhật ký cho đủ 20.000 vị trí ID.
