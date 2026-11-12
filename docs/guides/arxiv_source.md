# Tải và trích xuất mã nguồn arXiv

`download_source(mssv, versioned_id, destination)` tải đúng một version từ
`https://export.arxiv.org/src/{versioned_id}`. ID bắt buộc có hậu tố, ví dụ
`2502.09992v1`; legacy ID như `hep-th/9901001v2` được giữ nguyên.

Kết quả giữ `mssv`, `arxiv_id` gốc, `versioned_id`, `status`, `http_status`,
`attempts`, `content_type`, `downloaded_bytes`, `files` và `error`.
`files` là danh sách đường dẫn tương đối; `downloaded_bytes` là kích thước
payload tải hoàn tất, không cộng dữ liệu của những lần tải dở.

## HTTP

Mặc định `timeout=30`, `max_attempts=3`, dùng User-Agent của Group 10.
`max_attempts` tính cả lần đầu; timeout áp dụng cho thao tác socket.
Không tự theo redirect hoặc đổi endpoint.

API và source dùng chung khóa, đồng hồ pacing và retry trong `arxiv_client.py`:
một kết nối, chờ ít nhất 3 giây sau mỗi request, kể cả retry; backoff tăng dần
có jitter và tôn trọng `Retry-After`. Chỉ chạy một process, không chạy song song
notebook/script hoặc nhiều máy để tăng tốc độ truy cập.

Payload được ghi theo từng khối vào file tạm, có giới hạn kích thước và kiểm tra
`Content-Length` khi header có mặt. Khi phản hồi bị thiếu dữ liệu và máy chủ hỗ trợ byte range, chương trình có thể yêu cầu phần còn thiếu trong cùng lượt tải, với kiểm tra offset, kích thước và ETag khi có. Dữ liệu tạm không phải checkpoint để tiếp tục giữa các runtime.

## Xử lý archive offline

`extract_source(source_path, destination)` chỉ xử lý file có sẵn, không gọi mạng:

```python
from src.arxiv_source import extract_source

result = extract_source("sources/sample.tar.gz", "corpus/2502.09992/v1")
print(result)
```

- Nhận diện bằng nội dung: tar, tar.gz, ZIP, TeX đơn hoặc TeX đơn nén gzip.
- Giải nén an toàn file thường `.tex`/`.bib`/`.bbl` vào thư mục tạm; trước khi
  publish version, áp dụng Chính sách BBL bên dưới. File `.tex`/`.bib` gốc
  giữ nguyên bytes và đường dẫn tương đối; không sửa lệnh LaTeX hoặc lấy ảnh/PDF
  vào corpus.
- TeX đơn được lưu thành `main.tex`. Nhận diện các lệnh TeX phổ biến trong
  64 KiB đầu; văn bản không nhận diện được trả `unsupported_source`.
- Từ chối `..`, đường dẫn tuyệt đối/Windows nguy hiểm, symlink, hardlink,
  device, đường dẫn trùng hoặc xung đột chữ hoa/thường. Không dùng `extractall()`.
- Xử lý trong thư mục tạm rồi chuyển các file đã kiểm tra sang đích.
  Thư mục đích phải chưa tồn tại; không ghi đè dữ liệu cũ.
- Giới hạn kỹ thuật: payload 100 MiB, dữ liệu giải nén 200 MiB, 10.000 entries.
  Đây là giới hạn bảo vệ do chương trình đặt ra.

## Trạng thái

| `status` | Ý nghĩa |
| --- | --- |
| `success` | Tải/xử lý hoàn tất, có ít nhất một file TeX |
| `no_tex_source` | Archive hợp lệ không có `.tex`, hoặc nhận diện PDF/PostScript |
| `archive_error` | Archive hỏng, bị cắt hoặc sai checksum/kích thước |
| `unsafe_archive` | Có đường dẫn, link hoặc loại file nguy hiểm |
| `unsupported_source` | Chưa nhận diện/hỗ trợ định dạng; chưa kết luận không có TeX |
| `invalid_response` | Payload rỗng, HTML/XML hoặc header/kích thước phản hồi bất thường |
| `size_limit` | Vượt giới hạn kích thước hoặc số entries |
| `output_exists` | Đích đã tồn tại; không ghi đè, không gửi request tải |
| `storage_error` | Lỗi đọc/ghi file local |

## Chính sách BBL trong quy trình tải mã nguồn

`download_source()` nhận `bbl_artifact_dir`; pipeline M1 cấu hình
`reports/derived_bibliography/bbl-exception-v1`, nằm ngoài corpus. Khi archive
không chứa `.bbl`, không cần thư mục artifact. Khi có `.bbl`, thiếu cấu hình hoặc
không ghi được artifact sẽ chặn việc publish version và trả `storage_error`.

Mỗi `.bbl` được xét theo đúng version:

- Nếu version đã có `.bib`, hoặc file `.tex` có lệnh bibitem ngoài comment TeX,
  loại `.bbl` khỏi corpus và lưu bytes nguồn cùng checksum ngoài corpus.
- Nếu thiếu cả `.bib` lẫn lệnh bibitem, thử chuyển đổi cục bộ. Chuyển đổi thành
  công thì loại `.bbl` khỏi corpus và lưu `.bib` dẫn xuất cùng `.bbl` gốc ngoài
  corpus. Chuyển đổi thất bại thì giữ nguyên `.bbl` trong corpus, ghi mã lý do
  và không thay đổi bytes.
- Lỗi đọc `.tex` làm quyết định ở trạng thái chưa xác định; version không được
  publish cho đến khi xử lý lại được.

Manifest của version có `bbl_policy` và đường dẫn/checksum tới index ngoài corpus.
Index theo paper/version ghi từng đường dẫn BBL, checksum nguồn, trạng thái,
đường dẫn `.bib` dẫn xuất, checksum dẫn xuất và số entry. Artifact archive cohort
được tạo riêng, có checksum sidecar và được kiểm tra trong handoff/restore.

Checkpoint cũ không có `bbl_policy` vẫn đọc và resume được. Nếu còn `.bbl`, audit
ghi `legacy_unverified`; không đánh dấu hoàn tất chính sách mới trước khi xử lý
hoặc đối chiếu.
| `http_error`, `rate_limited`, `server_error` | HTTP lỗi; không kết luận không có TeX |
| `timeout`, `dns_error`, `connection_error`, `tls_error` | Lỗi mạng/TLS |

`extract_source()` trả `status`, `files`, `error`; các trường HTTP và định danh
được `download_source()` bổ sung. Archive không có `.tex` vẫn trả `no_tex_source`.
Bất kỳ `.bib`/`.bbl` còn lại sau khi áp dụng chính sách vẫn được lưu; nếu toàn bộ
BBL được loại sau khi chuyển đổi thành công và không còn file nào thì không tạo
thư mục đích.
Kết quả vẫn giữ record version qua MSSV/ID; bước checkpoint sau này cần lưu
record này, kể cả khi không có TeX.

Chưa hỗ trợ gzip lồng nhau, archive lồng nhau, ZIP mã hóa hoặc các định dạng nén
khác như `.Z`, bz2, xz. Tệp tải tạm không được dùng làm checkpoint giữa các phiên. Không bảo vệ trước process khác cố tình thay đổi
thư mục đích trong lúc chương trình chạy.

Nguồn: [arXiv — Unpacking Retrieved Papers](https://info.arxiv.org/help/unpack.html),
[arXiv Bulk Access](https://info.arxiv.org/help/bulk_data.html),
[arXiv Terms of Use](https://info.arxiv.org/help/api/tou.html),
[Python tarfile](https://docs.python.org/3.14/library/tarfile.html).
