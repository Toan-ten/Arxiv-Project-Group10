# Hướng dẫn thành viên chạy M1 trên Colab

## 1. Cấu hình phạm vi

1. Mở [sổ tay M1](../notebooks/m1_pipeline_thu_thap_va_kiem_toan.ipynb) bằng Google Colab và chọn môi trường chỉ dùng CPU.
2. Kết nối Google Drive khi ô cấu hình yêu cầu.
3. Nhập MSSV có trong `config/group10_id_ranges.json`.
4. Để xử lý đủ 5.000 vị trí ID, đặt `COHORT_LIMIT = None`. Đặt `COHORT_LIMIT = 1` để chạy thử một lô 100 ID. Lượt chạy thử dùng thư mục lưu trữ riêng; không dùng chung thư mục đó với lượt đầy đủ.
5. Chạy ô cấu hình. Ô này kiểm tra SHA môi trường chạy, cài gói phụ thuộc, tạo danh sách ID và in dấu vân tay của phạm vi. Ghi lại SHA, dấu vân tay và đường dẫn Drive trong nhật ký phiên.

Có thể thêm `KAGGLE_API_TOKEN` vào Colab Secrets và bật quyền truy cập notebook để tải bản chụp Kaggle. Không dán khóa vào notebook, Drive hoặc nhật ký. Bản chụp và chỉ mục hợp lệ trong bộ nhớ đệm vẫn có thể được dùng khi không có khóa; nếu không có dữ liệu Kaggle hợp lệ, pha arXiv tiếp tục bằng API trực tiếp.

## 2. Chạy ba pha

1. Chạy ô arXiv. Pha này xác nhận danh sách ID bằng Atom theo lô tối đa 20 ID, đối chiếu bản chụp Kaggle nếu có, rồi tải mã nguồn riêng cho từng phiên bản. Khi dữ liệu Kaggle lệch, kết quả arXiv được ưu tiên và bằng chứng sai khác được lưu theo ID.
2. Thêm `SEMANTIC_SCHOLAR_API_KEY` vào Colab Secrets, bật quyền truy cập notebook rồi chạy ô S2. Siêu dữ liệu được lấy theo lô tối đa 50 ID; tài liệu tham khảo được phân trang riêng cho từng bài. Nếu không có khóa, API có thể áp dụng hạn mức khác.
3. Chạy ô kiểm toán/đối soát sau khi arXiv và S2 đã ghi đủ bằng chứng. Kiểm toán xác minh phạm vi, định danh, xuất xứ, mã kiểm tra và trạng thái phân trang trước khi hợp nhất dữ liệu và xuất bảng.

Mặc định, sổ tay chạy arXiv, S2 rồi kiểm toán. Có thể chạy S2 trước arXiv hoặc chạy lại riêng một pha. S2 `not_found` chỉ mô tả kết quả tìm kiếm ở Semantic Scholar; trạng thái này không xác nhận ID arXiv không tồn tại.

## 3. Dừng và tiếp tục

Mỗi pha có ngân sách phiên mặc định 10 giờ và điểm khôi phục riêng theo lô. Nếu một pha dừng, chạy lại đúng ô với cùng MSSV, phạm vi, SHA môi trường chạy và thư mục gốc Drive. Không xóa điểm khôi phục và không chạy đồng thời nhiều môi trường ghi cùng phạm vi.

Khi các lượt thử nội bộ đã hết và pha gặp HTTP 429 ở discovery arXiv hoặc metadata/references S2, sổ tay có thể tự tiếp tục trong phiên hiện tại với giới hạn một lần mỗi lô và hai lần mỗi phiên. Cơ chế chờ ngẫu nhiên 5–10 phút, tôn trọng `Retry-After` nếu lâu hơn, chỉ tiếp tục khi điểm khôi phục được xác minh và còn đủ ngân sách. HTTP 403, lỗi xác minh, hết ngân sách hoặc lỗi ở pha khác vẫn dừng. Nếu môi trường Colab bị ngắt trong lúc chờ, cần chạy lại ô thủ công bằng cùng phạm vi và thư mục gốc.

Ô arXiv hoặc S2 chỉ xác nhận pha tương ứng đã chạy xong. Chỉ kết quả kiểm toán đã đối soát mới xác nhận dữ liệu đủ điều kiện xuất. Nếu kiểm toán bị chặn, đọc đường dẫn báo cáo và tóm tắt phép đo mà ô kiểm toán in ra; không diễn giải số liệu thiếu thành 0.

## 4. Phép đo và phạm vi kết luận

Lô thử 100 ID kiểm tra quy trình dừng, tiếp tục và kiểm toán; kết quả đó không đại diện cho 5.000 hay 20.000 vị trí ID. Phép đo cuối cần bao phủ đủ 20.000 vị trí của nhóm trên CPU Colab. Giữ nhật ký của từng phiên, cộng thời gian thử lại và chờ API, đồng thời loại khoảng nghỉ giữa các phiên. Không ngoại suy thời gian, RAM hoặc dung lượng từ lô thử.

Công cụ tổng hợp theo thành viên là `scripts/aggregate_m1_group_benchmark.py`. Chỉ kết luận từ các phép đo có đủ phạm vi, phiên, kiểm toán và dấu vân tay tương ứng. Xem thêm [quy trình M1](operations/m1_production.md) và [hướng dẫn đo hiệu năng](operations/benchmark_runner.md).
