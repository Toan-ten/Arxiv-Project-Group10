# Hướng dẫn chạy M1 trên Colab

Notebook ghim mã chạy bằng SHA `M1_RUNTIME_COMMIT`. Hướng dẫn phát hành và SHA đang dùng nằm trong [hướng dẫn Colab](guides/colab.md).

## 1. Cấu hình

1. Mở [notebook M1](../notebooks/m1_pipeline_thu_thap_va_kiem_toan.ipynb) trên Colab và chọn CPU.
2. Nhập MSSV có trong `config/group10_id_ranges.json`.
3. Đặt `COHORT_LIMIT = 1` để thử một lô 100 ID; đặt `None` để chạy đủ 5.000 ID của thành viên.
4. Thêm `KAGGLE_API_TOKEN` và `SEMANTIC_SCHOLAR_API_KEY` vào Colab Secrets, rồi cấp quyền đọc cho notebook.
5. Chạy ô cấu hình. Ghi lại SHA runtime, dấu vân tay phạm vi và đường dẫn Drive được in ra.

## 2. Chạy các bước

1. Chạy ô thu thập arXiv. Ô này xác minh ID, lịch sử phiên bản và mã nguồn; checkpoint được ghi vào Drive.
2. Chạy ô thu thập Semantic Scholar. Ô này lưu metadata và references cùng checkpoint, có thể tiếp tục từ trang đã lưu.
3. Chạy ô kiểm định. Bước này kiểm tra gói nén và checkpoint, in tiến độ theo lô và lưu báo cáo sau mỗi lô hoàn tất. Bước kiểm định không nhập dữ liệu Semantic Scholar vào corpus.
4. Chỉ khi kiểm định kết thúc với `status = "completed"`, notebook mới chạy chuẩn hóa và tạo bảng làm việc. Bảng được lưu dưới `exports/<selection_fingerprint>/working/`.

Nếu bước kiểm định bị dừng bởi ngân sách, notebook lưu các lô đã hoàn tất và không bắt đầu chuẩn hóa. Chạy lại ô 4 với cùng MSSV, phạm vi và Drive root để tiếp tục kiểm tra. Nếu nhấn nút dừng trong lúc khôi phục gói nén, thao tác khôi phục dùng thư mục tạm và tự hoàn nguyên nếu có lỗi; hãy chạy lại ô sau khi Colab đã dừng hoàn toàn.

## 3. Theo dõi và tiếp tục

Nhật ký kiểm định hiển thị lô hiện tại, khôi phục gói nén, số bài đã kiểm tra và trạng thái bàn giao. Báo cáo `m1_validation_<MSSV>_<fingerprint>.json` có danh sách lô đã hoàn tất, lô kế tiếp, trạng thái từng bài và lỗi cần xử lý.

Giữ nguyên MSSV, `COHORT_LIMIT` và Drive root khi tiếp tục. Không xóa checkpoint và không chạy hai runtime ghi cùng phạm vi. Lượt thử 100 ID dùng để kiểm tra vận hành, không đại diện cho toàn bộ phạm vi thành viên hoặc nhóm.
