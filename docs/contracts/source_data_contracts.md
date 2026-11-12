# Hợp đồng dữ liệu nguồn và bảng xuất

Tài liệu này quy định định danh, xuất xứ và ý nghĩa các trường trong corpus M1. Phản hồi nguồn có thể thiếu trường hoặc thay đổi theo thời điểm truy vấn. Giá trị `null` biểu thị dữ liệu chưa có bằng chứng; không tự điền từ nguồn khác nếu chưa có quy tắc đối chiếu.

## 1. Định danh

| Trường | Kiểu | Ý nghĩa |
|---|---|---|
| `arxiv_id` | Chuỗi | ID arXiv gốc, không có hậu tố phiên bản. |
| ID phiên bản | Chuỗi | ID gốc kèm hậu tố `vN`. |
| `paperId` của S2 | Chuỗi hoặc `null` | Định danh Semantic Scholar; khác với `arxiv_id` và `corpusId`. |

Khi ghép dữ liệu từ nhiều nguồn, dùng ID arXiv gốc. Chỉ gán metadata S2 cho một ID khi có bằng chứng định danh phù hợp.

## 2. arXiv

Pha arXiv của notebook M1 làm mới danh sách ID bằng Atom API theo lô tối đa 20 ID. Parser chỉ nhận entry khớp chính xác ID được yêu cầu. ID bị thiếu hoặc phản hồi chưa xác định được sẽ được truy vấn riêng để cô lập lỗi.

Snapshot Kaggle là nguồn tùy chọn để lập chỉ mục ứng viên và hỗ trợ lịch sử phiên bản. Nó không thay thế kết quả discovery arXiv hiện hành. Nếu bản ghi Kaggle thiếu hoặc lệch danh sách phiên bản/ngày so với arXiv, pipeline dùng kết quả arXiv và lưu bằng chứng sai khác. Khi không có token hoặc snapshot/index hợp lệ, pipeline tiếp tục bằng đường arXiv trực tiếp. Mỗi phiên bản vẫn được tải riêng từ endpoint có hậu tố phiên bản.

Feed rỗng hợp lệ tạo trạng thái `not_found`. Lỗi HTTP, mạng, XML hoặc phản hồi không khớp được ghi thành trạng thái lỗi riêng. Metadata arXiv lưu tiêu đề, tác giả, ngày nộp ban đầu, ngày cập nhật và danh sách phiên bản khi nguồn cung cấp; không suy ra ngày hoặc phiên bản còn thiếu từ nguồn khác.

## 3. Semantic Scholar

Metadata bài báo và tài liệu tham khảo được truy vấn trong pha S2 độc lập với arXiv. Lỗi S2 hoặc trạng thái S2 `not_found` không làm thay đổi kết luận arXiv. Tài liệu tham khảo được phân trang đến khi có bằng chứng trang cuối; trạng thái một phần hoặc lỗi không được xem là danh sách đầy đủ.

Corpus giữ mọi mục tài liệu tham khảo quan sát được, kể cả mục thiếu arXiv ID hoặc thông tin bài báo. Bảng xuất `references` chỉ gồm mục có arXiv ID theo lược đồ. `paperId` lấy từ Semantic Scholar; không dùng `corpusId` thay thế.

`publicationDate` là ngày xuất bản theo Semantic Scholar. Theo Q&A môn học, có thể dùng trường này cho `ref_submission_date` để thống nhất tên cột với bảng `papers`. Khi dùng, báo cáo phải ghi rõ xuất xứ; không gọi đây là ngày nộp arXiv đã xác minh. Nếu không có `publicationDate`, để giá trị `null`, trừ khi có phép đối chiếu arXiv được ghi trong provenance.

## 4. Bảng xuất

### `papers`

| Cột | Kiểu | Ý nghĩa |
|---|---|---|
| `arxiv_id` | Chuỗi, không null | ID arXiv gốc. |
| `title` | Chuỗi hoặc null | Tiêu đề theo metadata đã đối soát. |
| `authors` | Danh sách chuỗi hoặc null | Tác giả. |
| `submission_date` | Ngày hoặc null | Ngày nộp ban đầu theo arXiv. |
| `revised_dates` | Danh sách ngày hoặc null | Ngày của các phiên bản tiếp theo, theo thứ tự phiên bản. |
| `venue` | Chuỗi hoặc null | Nơi xuất bản nếu có bằng chứng. |

### `references`

| Cột | Kiểu | Ý nghĩa |
|---|---|---|
| `arxiv_id` | Chuỗi, không null | ID bài báo chứa mục trích dẫn. |
| `ref_arxiv_id` | Chuỗi, không null | ID arXiv của mục được trích dẫn. |
| `ref_title` | Chuỗi hoặc null | Tiêu đề mục được trích dẫn. |
| `ref_authors` | Danh sách chuỗi hoặc null | Tác giả mục được trích dẫn. |
| `ref_submission_date` | Ngày hoặc null | Ngày theo quy ước ở trên, kèm xuất xứ. |
| `ref_s2_id` | Chuỗi hoặc null | `paperId` S2 của mục được trích dẫn. |

## 5. Mã nguồn và tệp `.bbl`

Corpus giữ các tệp `.tex` và `.bib` gốc. Chỉ giữ `.bbl` trong corpus nếu cùng phiên bản không có `.bib`, không có lệnh `bibitem` đang hoạt động trong `.tex` và bộ chuyển đổi cục bộ không khôi phục được BibTeX an toàn.

Nếu cùng phiên bản có `.bib` hoặc lệnh `bibitem`, loại `.bbl` khỏi corpus và giữ bằng chứng bên ngoài corpus. Nếu thiếu cả `.bib` lẫn `bibitem`, thử chuyển đổi cục bộ. Khi chuyển đổi thành công, lưu `.bib` dẫn xuất và `.bbl` gốc ngoài corpus; khi thất bại, giữ nguyên `.bbl` trong corpus và ghi lý do.

Thao tác chuyển đổi không sửa manifest hay checksum nguồn. Pipeline ghi riêng quyết định chính sách vào manifest; checksum các tệp nguồn gốc được giữ nguyên. Chỉ mục ngoài corpus lưu đường dẫn tương đối và checksum để M2 có thể truy xuất artifact.

## 6. Bản chụp Kaggle tùy chọn và dữ liệu lịch sử

Luồng M1 ba pha có thể tải hoặc tái sử dụng bản chụp Kaggle, xác minh SHA-256 và dựng chỉ mục theo danh sách ID được chọn. Bản chụp và chỉ mục có provenance riêng; chúng hỗ trợ đối chiếu danh sách ID và lịch sử phiên bản, không thay thế xác nhận arXiv hoặc tải mã nguồn riêng cho từng phiên bản.

Khi Kaggle không sẵn có, pipeline dùng đường arXiv trực tiếp. Các trường Kaggle cũ trong điểm khôi phục chỉ được công nhận nếu đáp ứng chính sách provenance hiện hành. Dữ liệu lịch sử thiếu provenance vẫn được giữ để đối chiếu nhưng không tự được nâng thành bằng chứng mới.
