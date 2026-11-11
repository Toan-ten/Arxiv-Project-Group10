# Hợp đồng dữ liệu nguồn và bảng xuất

Tài liệu này định nghĩa ý nghĩa và cấu trúc dữ liệu mà dự án sử dụng từ ba nguồn, cùng hai bảng đầu ra M1. Các nguồn có thể thiếu trường hoặc trả dữ liệu khác nhau theo thời điểm. Giá trị `null` biểu thị không có dữ liệu phù hợp; không tự tạo giá trị để thay thế.

## 1. Định danh

| Tên | Kiểu | Ý nghĩa |
|---|---|---|
| `arxiv_id` | Chuỗi | Định danh bài báo không có hậu tố version, ví dụ `2501.02724` hoặc `hep-th/9901001`. |
| ID phiên bản | Chuỗi | Định danh bài báo kèm hậu tố `vN`, ví dụ `2501.02724v2`. Mỗi phiên bản thuộc một `arxiv_id`. |
| Semantic Scholar paper ID | Chuỗi hoặc `null` | Định danh paper do Semantic Scholar cấp; không đồng nhất với arXiv ID. |

Khi kết nối các nguồn, dùng arXiv ID gốc; trong Semantic Scholar, arXiv ID được biểu diễn qua `externalIds.ArXiv` khi có.

## 2. Kaggle arXiv metadata snapshot

**Đơn vị dữ liệu:** một đối tượng JSON cho mỗi bài báo, trong tệp JSON Lines. Đây là ảnh chụp metadata tại thời điểm tạo snapshot.

| Trường nguồn | Kiểu | Ý nghĩa |
|---|---|---|
| `id` | Chuỗi | arXiv ID gốc của paper. |
| `title` | Chuỗi | Tên paper. |
| `authors` | Chuỗi | Danh sách tác giả dưới dạng văn bản. |
| `authors_parsed` | Danh sách | Tên tác giả dưới dạng các thành phần. |
| `abstract` | Chuỗi | Tóm tắt của paper. |
| `comments` | Chuỗi hoặc `null` | Ghi chú do nguồn cung cấp. |
| `versions` | Danh sách object | Các version được ghi nhận; mỗi object chứa `version` và `created`. |
| `versions[].version` | Chuỗi | Nhãn version, ví dụ `v1`, `v2`. |
| `versions[].created` | Chuỗi ngày giờ | Thời điểm tạo version theo snapshot. |
| `update_date` | Chuỗi ngày | Ngày cập nhật bản ghi snapshot; không mặc định là ngày nộp revision. |
| `categories`, `doi`, `journal-ref`, `license` | Chuỗi hoặc `null` | Phân loại, định danh hoặc thông tin xuất bản và giấy phép khi có. |

Một ID vắng mặt trong snapshot không tự chứng minh paper không tồn tại. Phiên bản hoặc checksum snapshot cần được lưu cùng dữ liệu thu thập để xác định xuất xứ.

## 3. arXiv API

**Đơn vị dữ liệu:** một Atom entry cho paper, nhận qua arXiv API. Một entry có thể thiếu các trường siêu dữ liệu tùy trường hợp.

| Trường Atom | Kiểu | Ý nghĩa |
|---|---|---|
| `id` | URI | Định danh paper được trả về; có thể mang hậu tố version. |
| `title` | Văn bản | Tên paper. |
| `author/name` | Danh sách văn bản | Tên các tác giả. |
| `published` | Chuỗi ngày giờ | Thời điểm nộp ban đầu được nguồn cung cấp. |
| `updated` | Chuỗi ngày giờ | Thời điểm cập nhật của version được trả về. |
| `summary` | Văn bản | Tóm tắt paper. |
| `arxiv:comment` | Văn bản, có thể thiếu | Ghi chú do nguồn cung cấp. |

Hậu tố version mới nhất trong kết quả tra cứu ID gốc xác định dải version cần thu thập. Source của mỗi version được thu thập riêng. Khi source trả HTTP 404, pipeline kiểm tra trang abstract chính thức của đúng version qua cùng bộ giới hạn tốc độ và retry của arXiv. Chỉ thông báo rút bài rõ ràng trên trang đúng định danh version mới đủ bằng chứng cho `withdrawn_version`; HTTP 404 hoặc từ khóa trong comment/abstract chung không đủ. Khi resume gặp checkpoint cũ ghi `withdrawn_version` nhưng thiếu bằng chứng đúng version, pipeline lưu bằng chứng cũ vào `withdrawal_evidence_history`, hạ trạng thái về `http_error_404_unverified` nếu không có thư mục/tệp source, rồi kiểm tra lại trang chính thức. Nếu có dữ liệu source trên đĩa, pipeline chặn để kiểm toán thủ công thay vì tự sửa.

## 4. Semantic Scholar API

### Metadata paper

Batch metadata trả về một JSON array theo thứ tự các ID yêu cầu; phần tử có thể không có dữ liệu. Các trường trong object paper có thể thiếu hoặc `null`.

| Trường nguồn | Kiểu | Ý nghĩa |
|---|---|---|
| `paperId` | Chuỗi hoặc `null` | Semantic Scholar paper ID. |
| `corpusId` | Số nguyên hoặc `null` | Định danh corpus của Semantic Scholar. |
| `externalIds` | Object hoặc `null` | Các định danh ngoài, có thể gồm `ArXiv`, `DOI` và các khóa khác. |
| `title`, `abstract` | Chuỗi hoặc `null` | Tên và tóm tắt paper. |
| `authors` | Danh sách object hoặc `null` | Tác giả; phần tử có thể có `name`, `authorId`. |
| `year` | Số nguyên hoặc `null` | Năm xuất bản được Semantic Scholar ghi nhận. |
| `venue`, `publicationVenue` | Chuỗi/object hoặc `null` | Thông tin nơi xuất bản, khi có. |
| `referenceCount`, `citationCount` | Số nguyên hoặc `null` | Số lượng references và citations theo nguồn. |

Các nguồn có thể ghi tên tác giả, tên paper hoặc ngày khác nhau; không giả định các chuỗi phải giống hệt nhau.

### References

Endpoint references trả JSON object có `offset`, danh sách `data` và có thể có `next`. Mỗi phần tử `data` có thể chứa `citedPaper` và metadata của quan hệ trích dẫn. `next` là offset trang tiếp theo khi còn trang.

| Trường trong `citedPaper` | Kiểu | Ý nghĩa |
|---|---|---|
| `paperId` | Chuỗi hoặc `null` | Semantic Scholar ID của paper được trích dẫn. |
| `externalIds.ArXiv` | Chuỗi hoặc thiếu | arXiv ID của paper được trích dẫn, nếu nguồn có. |
| `title` | Chuỗi hoặc `null` | Tên paper được trích dẫn. |
| `authors` | Danh sách object hoặc `null` | Tác giả của paper được trích dẫn. |
| `publicationDate` | Chuỗi ngày hoặc `null` | Ngày xuất bản theo Semantic Scholar, dạng `YYYY-MM-DD` khi có. |
| `year`, `venue` | Số nguyên/chuỗi hoặc `null` | Năm và nơi xuất bản khi có. |

Một danh sách rỗng chỉ biểu thị không có phần tử ở trang đã nhận. Tập references của một paper chỉ được xem là thu thập đầy đủ khi đã nhận toàn bộ các trang đến trang cuối. HTTP 429, lỗi mạng hoặc trang thiếu không có nghĩa là paper có 0 references.

## 5. Hợp đồng đầu ra của dự án

### Bảng `papers`

| Cột | Kiểu | Ý nghĩa |
|---|---|---|
| `arxiv_id` | Chuỗi, không null | arXiv ID gốc của paper. |
| `title` | Chuỗi hoặc `null` | Tên paper. |
| `authors` | Danh sách chuỗi hoặc `null` | Tên tác giả. |
| `submission_date` | Chuỗi ngày hoặc `null` | Ngày nộp ban đầu. |
| `revised_dates` | Danh sách chuỗi ngày hoặc `null` | Ngày các revision theo thứ tự version; `[]` khi xác nhận chỉ có một version. |
| `venue` | Chuỗi hoặc `null` | Nơi xuất bản khi có. |

### Bảng `references`

| Cột | Kiểu | Ý nghĩa |
|---|---|---|
| `arxiv_id` | Chuỗi, không null | arXiv ID của paper trích dẫn. |
| `ref_arxiv_id` | Chuỗi, không null | arXiv ID của paper được trích dẫn. |
| `ref_title` | Chuỗi hoặc `null` | Tên paper được trích dẫn. |
| `ref_authors` | Danh sách chuỗi hoặc `null` | Tên tác giả của paper được trích dẫn. |
| `ref_submission_date` | Chuỗi ngày hoặc `null` | Ngày của paper được trích dẫn, theo quy ước dưới đây. |
| `ref_s2_id` | Chuỗi hoặc `null` | Semantic Scholar ID của paper được trích dẫn. |

Corpus lưu tất cả references thu được, kể cả references không có arXiv ID. Bảng `references` xuất cuối chỉ gồm references có arXiv ID. `ref_submission_date` có thể dùng `publicationDate` của Semantic Scholar hoặc ngày tra theo arXiv ID; nếu dùng ngày publication thay thế, cần ghi rõ xuất xứ và không khẳng định đó luôn là ngày nộp arXiv.

## 6. Quy tắc về trạng thái dữ liệu

- Kết quả source, metadata và references phải có trạng thái riêng; một giai đoạn hoàn tất không chứng minh toàn bộ M1 hoàn tất.
- Một version không có TeX phù hợp vẫn phải có bản ghi thể hiện kết quả. Với `.bbl` không có `.bib` tương ứng và không có `\bibitem` trong `.tex`, pipeline thử chuyển đổi cục bộ khi cấu trúc trường cho phép. Nếu không khôi phục được dữ liệu BibTeX một cách đáng tin cậy, báo cáo ghi lý do và giữ nguyên `.bbl`. Tệp `.bib` dẫn xuất, nếu có, được lưu ngoài corpus; không sửa manifest hay checksum nguồn.
- Dữ liệu thiếu, lỗi thu thập và phần chưa hoàn tất phải được giữ để đối chiếu và xử lý tiếp. Không gộp chúng vào trường hợp thành công với dữ liệu rỗng.

### Tích hợp ứng viên từ Kaggle

Bản ghi Kaggle gắn checksum chỉ là ứng viên chưa xác minh; đây không phải bằng chứng khám phá từ arXiv và không được ghi vào `provenance.arxiv`. Phản hồi arXiv mới phải xác nhận đủ danh sách phiên bản; danh sách ID trong ứng viên phải khớp chính xác, ngày phải hợp lệ và không mâu thuẫn với ngày nộp/phiên bản mới nhất đã xác minh. Khi đạt điều kiện, có thể dùng các ngày ứng viên cho lịch sử revision, kèm `provider=kaggle_snapshot`, bộ dữ liệu, SHA-256 snapshot, thời điểm snapshot và bản ghi ứng viên trong `provenance.version_history`. Nếu điều kiện không đạt, pipeline dùng Atom theo từng version; lỗi hoặc bằng chứng mâu thuẫn giữ lịch sử ở trạng thái chưa xác minh. Vẫn đọc được các bản ghi v2 hiện có. Quy định này không xác nhận ngược tính mới của dữ liệu khám phá đã lưu trước đó.
