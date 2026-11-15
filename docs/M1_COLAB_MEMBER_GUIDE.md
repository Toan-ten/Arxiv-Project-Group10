# Hướng dẫn thành viên chạy M1 trên Colab

Hướng dẫn này áp dụng cho notebook vận hành trong repo. Notebook dùng mã tại SHA `M1_RUNTIME_COMMIT`. Xem [cấu hình và quy trình phát hành runtime](guides/colab.md) để xác định mã thực tế của phiên chạy.

Khi tiếp tục một phiên, giữ nguyên MSSV, phạm vi ID và Drive root; không xóa dữ liệu đã lưu. `reference_quality` báo số bài lệch count và số reference thiếu ngày, độc lập với số bài đã thu thập. Bước kiểm toán sau khi đủ hai pha sẽ bổ sung ngày còn thiếu từ arXiv bằng cache có checksum, rồi ghi `reference_quality.json` cạnh hai bảng xuất. Bấm dừng có thể để thống kê thiếu mẫu cuối; kiểm tra checkpoint và trạng thái observation trước khi tiếp tục.

## 1. Cấu hình phạm vi

1. Mở [sổ tay M1](../notebooks/m1_pipeline_thu_thap_va_kiem_toan.ipynb) bằng Google Colab và chọn môi trường chỉ dùng CPU.
2. Nhập MSSV có trong `config/group10_id_ranges.json`.
3. Chọn `COHORT_LIMIT = None` để xử lý đủ 5.000 vị trí ID, hoặc `COHORT_LIMIT = 1` để chạy thử một lô 100 ID. Lượt chạy thử dùng thư mục lưu trữ riêng; không dùng chung thư mục đó với lượt đầy đủ.
4. Chạy ô cấu hình. Ô này kiểm tra hai secret, yêu cầu kết nối Drive, checkout SHA đã ghim, xác minh HEAD, cài dependency và tạo danh sách ID. Nếu kernel đã nạp `src`, khởi động lại phiên trước khi chạy ô này.
5. Ghi lại SHA thực tế, dấu vân tay phạm vi và đường dẫn Drive mà ô cấu hình in ra.

Trước khi chạy, thêm cả `KAGGLE_API_TOKEN` và `SEMANTIC_SCHOLAR_API_KEY` vào Colab Secrets, rồi bật quyền truy cập notebook. Notebook kiểm tra hai khóa trước khi gắn Drive hoặc gọi API. Không dán khóa vào notebook, Drive hay nhật ký. Khóa Kaggle vẫn bắt buộc khi snapshot đã có trong bộ nhớ đệm.

## 2. Chạy ba pha

1. Chạy ô arXiv. Pha này yêu cầu snapshot và index Kaggle hợp lệ, xác nhận danh sách ID bằng Atom theo lô tối đa 20 ID, đối chiếu lịch sử rồi tải mã nguồn riêng cho từng lần nộp. Khi snapshot hoặc index không sẵn sàng, pha dừng trước discovery; pipeline không chuyển sang đường arXiv-only.
2. Chạy ô S2. Notebook dùng `batch_m1` để gom metadata cùng references theo lô tối đa 100 ID, kiểm tra định danh và số lượng trước khi công nhận đầy đủ. Bài chưa đủ bằng chứng chuyển sang phân trang. M1 dừng nếu thiếu khóa S2 và không gửi request ẩn danh. Xem [phạm vi trường dữ liệu và cơ chế phân trang](guides/s2_integration.md).
3. Chạy ô kiểm toán/đối soát sau khi arXiv và S2 đã ghi đủ bằng chứng. Kiểm toán xác minh phạm vi, định danh, xuất xứ, mã kiểm tra và trạng thái phân trang trước khi hợp nhất dữ liệu và xuất bảng.

Mặc định, sổ tay chạy arXiv, S2 rồi kiểm toán. Có thể chạy S2 trước arXiv hoặc chạy lại riêng một pha. S2 `not_found` chỉ mô tả kết quả tìm kiếm ở Semantic Scholar; trạng thái này không xác nhận ID arXiv không tồn tại.

## 3. Dừng và tiếp tục

Mỗi pha có ngân sách phiên mặc định 10 giờ và điểm khôi phục riêng theo lô. Nếu một pha dừng, chạy lại đúng ô với cùng MSSV, phạm vi, SHA môi trường chạy và thư mục gốc Drive. Không xóa điểm khôi phục và không chạy đồng thời nhiều môi trường ghi cùng phạm vi.

Khi các lượt thử nội bộ đã hết và pha gặp HTTP 429 ở discovery arXiv hoặc metadata/references S2, sổ tay có thể tự tiếp tục trong phiên hiện tại với giới hạn một lần mỗi lô và hai lần mỗi phiên. Cơ chế chờ ngẫu nhiên 5–10 phút, tôn trọng `Retry-After` nếu lâu hơn, chỉ tiếp tục khi điểm khôi phục được xác minh và còn đủ ngân sách. HTTP 403, lỗi xác minh, hết ngân sách hoặc lỗi ở pha khác vẫn dừng. Nếu môi trường Colab bị ngắt trong lúc chờ, cần chạy lại ô thủ công bằng cùng phạm vi và thư mục gốc.

Ô arXiv hoặc S2 chỉ xác nhận pha tương ứng đã chạy xong. Chỉ kết quả kiểm toán đã đối soát mới xác nhận dữ liệu đủ điều kiện xuất. Nếu kiểm toán bị chặn, đọc đường dẫn báo cáo và tóm tắt phép đo mà ô kiểm toán in ra; không diễn giải số liệu thiếu thành 0.

Trong kết quả pha kiểm toán, `is_reconciled = True` xác nhận phạm vi đã chọn đạt điều kiện đối soát; `export_status = "exported"` xác nhận bước xuất trả về kết quả. `handoff_status` thuộc giá trị trả về của hàm tương thích `run_m1_member()`, không phải trường mà notebook gọi ba pha riêng cần tìm. Kết quả của một phạm vi thử không được diễn giải thành hoàn tất phạm vi toàn thành viên hoặc toàn nhóm.

## 4. Phép đo và phạm vi kết luận

Lô thử 100 ID kiểm tra quy trình dừng, tiếp tục và kiểm toán; kết quả đó không đại diện cho 5.000 hay 20.000 vị trí ID. Phép đo cuối cần bao phủ đủ 20.000 vị trí của nhóm trên CPU Colab. Giữ nhật ký của từng phiên, cộng thời gian thử lại và chờ API, đồng thời loại khoảng nghỉ giữa các phiên. Không ngoại suy thời gian, RAM hoặc dung lượng từ lô thử.

Công cụ tổng hợp theo thành viên là `scripts/aggregate_m1_group_benchmark.py`. Chỉ kết luận từ các phép đo có đủ phạm vi, phiên, kiểm toán và dấu vân tay tương ứng. Xem thêm [quy trình M1](operations/m1_production.md) và [hướng dẫn đo hiệu năng](operations/benchmark_runner.md).
