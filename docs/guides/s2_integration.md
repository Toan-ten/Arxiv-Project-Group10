# Tích hợp Semantic Scholar trong M1

## Luồng vận hành

Notebook gọi `run_m1_s2_member()` từ `src.m1_workflow`; phần triển khai nằm trong `src.m1_phases`. Pha S2 nhận danh sách ID xác định theo MSSV, độc lập với tiến độ tải mã nguồn arXiv. Mỗi lô có điểm khôi phục S2 riêng. Kiểm toán chỉ hợp nhất bằng chứng S2 vào corpus sau khi xác minh dấu vân tay phạm vi, định danh và tính toàn vẹn.

Trạng thái S2 `not_found` chỉ mô tả kết quả tra cứu của Semantic Scholar. Trạng thái này không kết luận ID arXiv không tồn tại và không thay đổi kết quả arXiv.

## Truy vấn và cấu hình M1

S2 dùng Semantic Scholar Graph API: metadata qua `POST /graph/v1/paper/batch` và references qua endpoint phân trang `/paper/{paper_id}/references`. Xem [tài liệu API chính thức](https://api.semanticscholar.org/api-docs) để biết trường dữ liệu và giới hạn dịch vụ.

| Tham số | Giá trị mặc định trong M1 | Phạm vi |
|---|---:|---|
| `timeout` | 30 giây | Mỗi request |
| `max_attempts` | 8 | Gồm request đầu tiên |
| `min_interval` | 1 giây | Giữa các request trong cùng tiến trình |
| `metadata_batch_size` | 50 ID | Mỗi request metadata; references vẫn truy vấn riêng từng bài |
| `reference_limit` | 100 | Mỗi trang references |
| `max_pages` | 1.000 | Mỗi bài báo |
| `max_retry_wait` | 600 giây | Ngân sách chờ thử lại |
| `backoff_base` / `backoff_cap` | 3 / 60 giây | Thời gian chờ thử lại |

Pha M1 gom tối đa 50 ID chưa có metadata hợp lệ vào một request. Trước khi ghi bằng chứng, response phải có đúng số phần tử, đúng thứ tự và ID arXiv khớp từng vị trí. Sau đó điểm khôi phục được lưu riêng theo ID. Nếu response của cả lô không hợp lệ hoặc quá lớn, pha chia nhỏ lô để cô lập lỗi; sai số phần tử hoặc sai thứ tự sẽ chặn việc ghi bằng chứng. ID lỗi vẫn chưa hoàn tất và được thử lại khi tiếp tục. References được phân trang riêng theo từng bài. Cấu hình thực tế và số request metadata được ghi trong quan sát phiên.

Notebook yêu cầu Colab Secret `SEMANTIC_SCHOLAR_API_KEY` trước khi bắt đầu M1 và gửi khóa qua header `x-api-key`. Nếu thiếu khóa hoặc không đọc được Secret, pha S2 dừng trước khi tạo checkpoint hay gửi request. M1 không gửi request ẩn danh. Không ghi khóa vào mã nguồn notebook, điểm khôi phục hoặc báo cáo.

## Kiểm tra metadata và references

Chỉ gắn metadata với ID yêu cầu khi `externalIds.ArXiv` khớp ID đó. Phần tử `null` trong response hợp lệ được ghi là `not_found`. Timeout, lỗi HTTP, response sai cấu trúc hoặc thiếu bằng chứng định danh vẫn là lỗi hay trạng thái chưa xác định.

References được lấy đến khi máy chủ xác nhận không còn trang. Corpus giữ nguyên thứ tự và dữ liệu `raw` của mọi mục quan sát được, kể cả mục thiếu `citedPaper` hoặc arXiv ID. Bảng xuất chỉ nhận mục đáp ứng lược đồ. Trường title, authors, ngày hoặc S2 paperId còn thiếu được ghi trong `missing_fields`; không tự tạo dữ liệu.

`publicationDate` của S2 là ngày xuất bản theo nguồn đó. Theo Q&A môn học, có thể dùng trường này cho cột `ref_submission_date`. Báo cáo phải nêu xuất xứ của giá trị; ngày này không chứng minh ngày nộp arXiv.

## Tiếp tục và dừng

Điểm khôi phục lưu metadata, bản chụp references, vị trí tiếp tục, lịch sử lỗi và checksum. Nếu references còn một phần, client đọc lại phần đầu đã lưu trước khi tiếp tục để tránh ghép các trang khác nhau. Điểm khôi phục không hợp lệ hoặc sai checksum được giữ nguyên để kiểm tra.

Khi các lượt thử nội bộ đã hết và gặp HTTP 429 ở metadata hoặc references, notebook có thể tự tiếp tục tối đa một lần mỗi lô và hai lần mỗi phiên. Cơ chế chờ ngẫu nhiên 5–10 phút, tôn trọng `Retry-After` nếu lâu hơn, và chỉ chạy khi điểm khôi phục đã được xác minh, giới hạn lượt chưa hết và còn đủ ngân sách. HTTP 403, lỗi điểm khôi phục hoặc hết ngân sách vẫn làm pha dừng. Nếu môi trường bị ngắt trong lúc chờ, người vận hành cần chạy lại ô S2 bằng cùng MSSV, phạm vi, dấu vân tay và thư mục gốc. Không chạy đồng thời hai tiến trình ghi cho cùng thành viên.

Kiểm toán bị chặn nếu bằng chứng phân trang, định danh hoặc xuất xứ chưa đầy đủ. Xem [mô tả client cấp thấp](semantic_scholar.md), [hợp đồng dữ liệu](../contracts/source_data_contracts.md) và [hướng dẫn Colab](../M1_COLAB_MEMBER_GUIDE.md).
