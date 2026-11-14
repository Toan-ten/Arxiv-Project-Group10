# Hợp đồng dữ liệu nguồn và bảng xuất

Tài liệu này quy định định danh, xuất xứ và ý nghĩa các trường trong corpus M1. Phản hồi nguồn có thể thiếu trường hoặc thay đổi theo thời điểm truy vấn. Giá trị `null` biểu thị dữ liệu chưa có bằng chứng; không tự điền từ nguồn khác nếu chưa có quy tắc đối chiếu.

## 1. Định danh

| Trường | Kiểu | Ý nghĩa |
|---|---|---|
| `arxiv_id` | Chuỗi | ID arXiv gốc, không có hậu tố lần nộp. |
| ID lần nộp | Chuỗi | ID gốc kèm hậu tố `vN`. |
| `paperId` của S2 | Chuỗi hoặc `null` | Định danh Semantic Scholar; khác với `arxiv_id` và `corpusId`. |

Khi ghép dữ liệu từ nhiều nguồn, dùng ID arXiv gốc. Chỉ gán metadata S2 cho một ID khi có bằng chứng định danh phù hợp.

## 2. arXiv

Pha arXiv của notebook M1 làm mới danh sách ID bằng Atom API theo lô tối đa 20 ID. Parser chỉ nhận entry khớp chính xác ID được yêu cầu. ID bị thiếu hoặc phản hồi chưa xác định được sẽ được truy vấn riêng để cô lập lỗi.

Trong luồng M1, `KAGGLE_API_TOKEN` là thông tin xác thực bắt buộc. Snapshot Kaggle hợp lệ và index tương ứng phải sẵn sàng trước khi discovery bắt đầu; nếu không, pha dừng. Snapshot lập chỉ mục ứng viên và hỗ trợ lịch sử lần nộp; Atom xác nhận kết quả hiện hành. Nếu bản ghi Kaggle thiếu hoặc lệch danh sách lần nộp/ngày so với arXiv, pipeline dùng kết quả arXiv và lưu bằng chứng sai khác. Mã nguồn của mỗi lần nộp được tải riêng từ endpoint có hậu tố tương ứng.

Feed rỗng hợp lệ tạo trạng thái `not_found`. Lỗi HTTP, mạng, XML hoặc phản hồi không khớp được ghi thành trạng thái lỗi riêng. Metadata arXiv lưu tiêu đề, tác giả, ngày nộp ban đầu, ngày cập nhật và danh sách lần nộp khi nguồn cung cấp; không suy ra ngày hoặc lần nộp còn thiếu từ nguồn khác.

## 3. Semantic Scholar

Metadata bài báo và tài liệu tham khảo được truy vấn trong pha S2 độc lập với arXiv. M1 yêu cầu `SEMANTIC_SCHOLAR_API_KEY` và không gửi request ẩn danh; client Semantic Scholar dùng riêng ngoài M1 vẫn hỗ trợ chế độ không khóa. Lỗi S2 hoặc trạng thái S2 `not_found` không làm thay đổi kết luận arXiv. Quy trình M1 lấy references bằng batch; bài chưa xác minh được chuyển sang phân trang đến khi có bằng chứng trang cuối. Trạng thái một phần hoặc lỗi không được xem là danh sách đầy đủ.

Corpus giữ mọi mục tài liệu tham khảo quan sát được, kể cả mục thiếu arXiv ID hoặc thông tin bài báo. Bảng xuất `references` chỉ gồm mục có arXiv ID theo lược đồ. `paperId` lấy từ Semantic Scholar; không dùng `corpusId` thay thế.

Notebook dùng batch làm đường chính và chỉ phân trang bài có references chưa xác minh được. Đối với batch, cần danh sách hợp lệ và khớp count. Đối với tuyến phân trang, hoàn tất thu thập cần kết thúc chuỗi trang hợp lệ; việc khớp count metadata được đánh giá riêng. Bất nhất sau khi hết trang được giữ thành cảnh báo, không tạo mục giả, không bỏ mục thật và không biến snapshot partial thành complete.

`publicationDate` là ngày xuất bản theo Semantic Scholar. Theo Q&A môn học, có thể dùng trường này cho `ref_submission_date` để thống nhất tên cột với bảng `papers`. Khi dùng, báo cáo phải ghi rõ xuất xứ; không gọi đây là ngày nộp arXiv đã xác minh. Nếu không có `publicationDate`, để giá trị `null`, trừ khi có phép đối chiếu arXiv được ghi trong provenance.

Pha kiểm toán thực hiện phép đối chiếu đó trước xuất: chỉ bổ sung các ngày còn thiếu qua metadata arXiv đã xác minh hoặc Atom `published`, lưu bằng chứng và checksum ngoài corpus. Cache và giá trị ngày được kiểm tra trước khi chiếu ra bảng. Raw S2 và mọi trường nguồn không đổi; không lấy Atom `updated` làm ngày nộp đầu tiên. Không tìm thấy ngày thì giữ null và ghi lý do. Việc bổ sung không mở rộng phạm vi tải TeX sang các bài tham chiếu ngoài phân công.

## 4. Bảng xuất

### `papers`

| Cột | Kiểu | Ý nghĩa |
|---|---|---|
| `arxiv_id` | Chuỗi, không null | ID arXiv gốc. |
| `title` | Chuỗi hoặc null | Tiêu đề theo metadata đã đối soát. |
| `authors` | Danh sách chuỗi hoặc null | Tác giả. |
| `submission_date` | Ngày hoặc null | Ngày nộp ban đầu theo arXiv. |
| `revised_dates` | Danh sách ngày hoặc null | Ngày của các lần nộp tiếp theo, theo thứ tự nộp. |
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

Corpus giữ các tệp `.tex` và `.bib` gốc. Chỉ giữ `.bbl` trong corpus nếu cùng lần nộp không có `.bib`, không có lệnh `bibitem` đang hoạt động trong `.tex` và bộ chuyển đổi cục bộ không khôi phục được BibTeX an toàn.

Nếu cùng lần nộp có `.bib` hoặc lệnh `bibitem`, loại `.bbl` khỏi corpus và giữ bằng chứng bên ngoài corpus. Nếu thiếu cả `.bib` lẫn `bibitem`, thử chuyển đổi cục bộ. Khi chuyển đổi thành công, lưu `.bib` dẫn xuất và `.bbl` gốc ngoài corpus; khi thất bại, giữ nguyên `.bbl` trong corpus và ghi lý do.

Thao tác chuyển đổi không sửa manifest hay checksum nguồn. Pipeline ghi riêng quyết định chính sách vào manifest; checksum các tệp nguồn gốc được giữ nguyên. Chỉ mục ngoài corpus lưu đường dẫn tương đối và checksum để M2 có thể truy xuất artifact.

## 6. Bản chụp Kaggle bắt buộc trong M1 và dữ liệu lịch sử

Luồng M1 ba pha phải có Colab Secret `KAGGLE_API_TOKEN`. Pipeline tải hoặc tái sử dụng bản chụp Kaggle, xác minh SHA-256 và dựng chỉ mục theo danh sách ID được chọn. Bản chụp và chỉ mục có xuất xứ riêng; chúng hỗ trợ đối chiếu danh sách ID và lịch sử lần nộp. Atom xác nhận ID và mã nguồn của mỗi lần nộp được tải riêng. Việc tái sử dụng cache không miễn yêu cầu cấu hình khóa.

Khi Kaggle không sẵn có hoặc index không hợp lệ, pha M1 dừng trước khi gọi Atom discovery và giữ nguyên checkpoint đã có. Kiểm toán chỉ chấp nhận source checkpoint có trạng thái Kaggle `ready`, SHA snapshot/index hợp lệ và bằng chứng index khớp checksum. Nếu điểm khôi phục thiếu bằng chứng xuất xứ Kaggle, cần chạy lại pha arXiv để ghi đủ bằng chứng. Dữ liệu đã lưu được giữ để kiểm tra.
