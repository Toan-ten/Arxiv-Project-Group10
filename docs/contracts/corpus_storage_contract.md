# Hợp đồng lưu trữ corpus và trạng thái thu thập

Hợp đồng này quy định cấu trúc dữ liệu được lưu cho từng bài báo, các tệp nguồn và trạng thái thu thập. Định nghĩa dữ liệu từ các nhà cung cấp và bảng đầu ra nằm trong [hợp đồng dữ liệu nguồn và bảng xuất](source_data_contracts.md). Hợp đồng lưu trữ sử dụng `contract_version: 2`.

## Định danh và cấu trúc thư mục

Mỗi bản ghi thuộc về một arXiv ID gốc và một thành viên được phân công (`mssv`). ID của phiên bản gồm ID gốc và hậu tố `vN`. ID trong references có thể theo định dạng arXiv cũ và phải được giữ nguyên.

```text
<corpus>/<paper_key>/
  metadata.json
  references.json
  manifest.json
  v1/
  v2/
```

Mỗi phiên bản đã phát hiện phải có bản ghi trạng thái, kể cả khi không có thư mục chứa tệp. Thư mục phiên bản lưu `.tex`, `.bib` gốc và chỉ giữ `.bbl` theo ngoại lệ ở mục Chính sách BBL. Giữ nguyên nội dung byte và đường dẫn tương đối; không sửa lệnh LaTeX hoặc lưu tệp hình ảnh trong corpus.

## `metadata.json`

| Trường | Kiểu | Quy định |
|---|---|---|
| `contract_version` | Số nguyên | Giá trị `2`. |
| `arxiv_id`, `mssv` | Chuỗi | Định danh bài báo gốc và thành viên phụ trách. |
| `status` | Chuỗi | Trạng thái xử lý metadata. |
| `title`, `venue` | Chuỗi hoặc `null` | Tên bài báo và nơi xuất bản khi có. |
| `authors` | Danh sách chuỗi hoặc `null` | Tác giả khi có. |
| `submission_date` | Chuỗi ngày hoặc `null` | Ngày nộp ban đầu có bằng chứng. |
| `revised_dates` | Danh sách ngày hoặc `null` | Ngày revision theo thứ tự; `null` khi chưa biết đủ, `[]` khi xác nhận không có revision. |
| `version_status` | Chuỗi | Trạng thái xác định danh sách phiên bản. |
| `versions` | Danh sách object | Các phiên bản và ngày tương ứng. |
| `missing_fields` | Danh sách chuỗi | Các trường cần xử lý nhưng chưa có dữ liệu. |
| `provenance` | Object | Nguồn, thời điểm và bằng chứng được dùng để xác định metadata. |

Mỗi phần tử `versions` gồm `version`, `versioned_id`, `date` và `date_status`. `date` có thể là `null`; biết ID phiên bản không đồng nghĩa đã xác minh ngày của phiên bản đó. Không dùng ngày cập nhật của phiên bản cuối để điền cho các phiên bản trước.

## `references.json`

Tệp chứa toàn bộ references đã thu thập cho bài báo, kể cả references không có arXiv ID.

| Trường | Kiểu | Quy định |
|---|---|---|
| `contract_version` | Số nguyên | Giá trị `2`. |
| `arxiv_id` | Chuỗi | ID gốc của bài báo trích dẫn. |
| `status` | Chuỗi | Trạng thái thu thập references. |
| `provenance` | Object | Nguồn, định danh truy vấn và thời điểm thu thập. |
| `coverage` | Object | Phạm vi và tiến độ thu thập. |
| `items` | Danh sách object | Các references đã thu thập. |
| `error` | Object hoặc `null` | Thông tin lỗi khi có. |

`coverage` gồm `complete`, `continuation`, `reported_total` và `retrieved_count`. `complete` chỉ là `true` khi đã thu thập đầy đủ các trang; tổng số do nguồn cung cấp có thể là `null`. `items=[]` chỉ xác nhận tập rỗng khi `status` là `complete` và `coverage.complete` là `true`.

Mỗi reference có thể gồm `arxiv_id`, `title`, `authors`, `submission_date`, `publication_date`, `year`, `venue`, `s2_paper_id`, `missing_fields` và `raw`. Các giá trị chưa có được để `null`; `raw` giữ bản ghi nguồn. `s2_paper_id` là `paperId`, không phải `corpusId`.

`submission_date` và `publication_date` là hai giá trị riêng. Quy ước sử dụng `publication_date` cho cột `ref_submission_date` khi xuất bảng được mô tả trong hợp đồng dữ liệu nguồn và bảng xuất.

## `manifest.json`

Manifest lưu trạng thái tổng thể và trạng thái theo phiên bản. Các trường chính gồm `contract_version`, `arxiv_id`, `mssv`, `run_id`, `status`, `discovery_status`, `version_status`, `metadata_status`, `references_status`, `versions` và `updated_at`.

`versions` là object có khóa `v1`, `v2`, … Mỗi bản ghi phiên bản lưu `versioned_id`, `status`, `source_status`, thông tin HTTP và nguồn, `files`, `file_checksums`, số liệu kích thước khi có, số lần thử, thời điểm hoàn tất và `error` khi xảy ra lỗi. Đường dẫn trong `files` là đường dẫn tương đối; `file_checksums` ánh xạ từng đường dẫn tới SHA-256 của nội dung đã lưu. Số liệu chưa biết phải là `null`, không thay bằng `0`.

Trạng thái từng giai đoạn phải phân biệt ít nhất: chưa chạy, đang chạy, hoàn tất, hoàn tất một phần, gián đoạn và lỗi. Các trạng thái kết thúc hợp lệ của phiên bản gồm `success`, `no_tex_source` và `withdrawn_version` có bằng chứng. Một phiên bản không có TeX hợp lệ được ghi `no_tex_source`; lỗi mạng, lỗi định dạng hoặc HTTP 404 không tự trở thành `no_tex_source` hay withdrawn. Trường hợp withdrawn cần bằng chứng gắn với đúng phiên bản.

Bài báo chỉ được coi là hoàn tất M1 khi discovery, danh sách phiên bản, source của từng phiên bản, metadata và references đã được xử lý theo yêu cầu; dữ liệu lưu trữ phải hợp lệ. Source hoàn tất khi references còn thiếu là trạng thái chưa hoàn tất M1.

## Ghi nhận lỗi và khôi phục

- Lưu lỗi cùng giai đoạn, định danh liên quan và trạng thái để có thể tiếp tục xử lý; không lưu thông tin xác thực trong dữ liệu hoặc log.
- Không thay dữ liệu hợp lệ bằng phản hồi lỗi. Tệp tạm, thư mục rỗng hoặc đích đã tồn tại không chứng minh một phiên bản đã hoàn tất.
- Khi khôi phục, kiểm tra định danh, trạng thái, tệp và checksum trước khi bỏ qua phần đã xử lý. Giữ dữ liệu không khớp để kiểm tra, không tự xóa hoặc ghi đè.

## Chính sách BBL

Chính sách bbl-exception-v1 được xét riêng cho từng version:

- Nếu cùng version có `.bib`, hoặc `.tex` có lệnh `\bibitem` đang hoạt động ngoài comment TeX, loại `.bbl` khỏi corpus. Giữ bytes `.bbl` cùng checksum trong artifact bên ngoài corpus và ghi lý do.
- Nếu thiếu cả `.bib` lẫn lệnh `\bibitem`, thử bộ chuyển đổi cục bộ. Chuyển đổi thành công thì loại `.bbl` khỏi corpus, giữ `.bbl` gốc và `.bib` dẫn xuất bên ngoài corpus. Nếu thất bại, giữ nguyên `.bbl` trong corpus kèm mã lý do.
- Nếu TeX không đọc được để phân loại, không sửa/xóa `.bbl` và không đánh dấu version tuân thủ chính sách.

Trường `versions.<version>.bbl_policy` lưu phiên bản chính sách, quyết định cho từng file, đường dẫn tương đối đến index bên ngoài corpus và SHA-256 của index. Index lưu đường dẫn tương đối, checksum `.bbl` gốc, checksum `.bib` dẫn xuất và số entry. Artifact được ghi bền vững, không thay file đã tồn tại có nội dung khác; archive cohort có gói artifact và checksum sidecar riêng.

Checkpoint cũ không có trường `bbl_policy` vẫn đọc được. Nếu checkpoint đó còn `.bbl`, audit báo `legacy_unverified`; chỉ lượt đã xử lý hoặc đối chiếu mới được đánh dấu tuân thủ. Resume và restore xác minh cả checksum corpus lẫn index/artifact ngoài corpus.
