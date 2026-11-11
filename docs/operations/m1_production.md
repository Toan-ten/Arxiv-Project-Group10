# Quy trình thu thập M1

## Luồng xử lý

Mọi thành viên chạy [sổ tay M1 trên Colab](../../notebooks/m1_pipeline_thu_thap_va_kiem_toan.ipynb). Phân công lấy từ `config/group10_id_ranges.json`; mỗi thành viên xử lý 50 lô, tương ứng 5.000 vị trí ID. Toàn nhóm có 20.000 vị trí được giao; đây không phải số bài báo chắc chắn tồn tại.

Notebook chia vận hành thành ba cell trên cùng lựa chọn ID và fingerprint: arXiv, Semantic Scholar (S2), rồi kiểm toán/đối soát. Mỗi pha dùng checkpoint riêng theo cohort. S2 không phụ thuộc source đã tải và có thể chạy trước arXiv; S2 `not_found` chỉ là trạng thái của S2. `run_m1_member()` giữ tương thích với lệnh cũ bằng cách gọi lần lượt cả ba pha.

Đặt `COHORT_LIMIT=1` cho pilot 100 ID; notebook tự chuyển pilot sang root Drive/local riêng. `COHORT_LIMIT=None` chạy đủ 5.000 ID và giữ root corpus đầy đủ để có thể resume dữ liệu v2 hợp lệ.

Pha arXiv xác minh ID, xác định mọi phiên bản và tải source. Pha S2 lưu metadata, toàn bộ references thô, phân trang, continuation, lỗi và checksum trong staging riêng. Kiểm toán kiểm tra fingerprint, danh tính và tính toàn vẹn trước khi hợp nhất; một khóa writer theo MSSV ngăn các runtime ghi đồng thời. Hai bảng `papers` và `references` chỉ được xuất khi source và S2 đều có bằng chứng cuối, xung đột đã được xử lý và kiểm toán M1 đạt.

## Dữ liệu nguồn

- arXiv xác minh bài báo và phiên bản. Mỗi phiên bản được tải riêng; corpus chỉ giữ `.tex`, `.bib` và `.bbl`, không giữ tệp nhị phân.
- Discovery theo ID dùng arXiv trực tiếp. Snapshot Kaggle không phải đầu vào bắt buộc của luồng vận hành.
- Semantic Scholar cung cấp siêu dữ liệu và tài liệu tham khảo theo thời điểm truy vấn. `ref_submission_date` có thể lấy từ `publicationDate`; báo cáo phải giữ xuất xứ và không diễn giải giá trị này thành ngày nộp arXiv.
- Quy tắc xử lý `.bbl` và ánh xạ ngày tham khảo được định nghĩa trong [hợp đồng dữ liệu nguồn](../contracts/source_data_contracts.md).

## Dừng và tiếp tục

Mỗi cell có ngân sách mềm mặc định 10 giờ. Giãn cách, retry và delay được tính vào runtime. Khi Stop, hết ngân sách, lỗi mạng hoặc gặp 403/429, checkpoint của pha đang chạy được giữ lại để resume. Chạy lại đúng cell với cùng MSSV, fingerprint và Drive. Không chạy nhiều tiến trình ghi trên cùng MSSV.

Lỗi toàn vẹn, dữ liệu chưa xác minh, mã nguồn thiếu, siêu dữ liệu thiếu hoặc tài liệu tham khảo chưa lấy đủ đều chặn bàn giao. `source_complete` chỉ mô tả giai đoạn mã nguồn; nó không thay thế trạng thái M1.

## Kiểm toán và tổng hợp

Báo cáo gắn với phiên, commit, cohort, fingerprint và checksum. Mỗi thành viên giữ nhật ký Colab; [`aggregate_m1_group.py`](../../scripts/aggregate_m1_group.py) kiểm tra độ bao phủ và archive, còn [`aggregate_m1_group_benchmark.py`](../../scripts/aggregate_m1_group_benchmark.py) tổng hợp source, S2, thời gian discovery và tài nguyên. Tổng runtime cộng các phiên tuần tự, loại khoảng nghỉ và tránh cộng lại paper chỉ đọc từ checkpoint. Báo thiếu phép đo thay vì suy rộng. Chỉ ghi benchmark cuối sau khi chạy đủ 20.000 vị trí ID trên CPU Colab; pilot 100 ID chỉ đánh giá quy trình.
