# Source downloader

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
`Content-Length` khi header có mặt. Retry bắt đầu lại từ đầu, không nối dữ liệu dở.

## Xử lý archive offline

`extract_source(source_path, destination)` chỉ xử lý file có sẵn, không gọi mạng:

```python
from src.arxiv_source import extract_source

result = extract_source("sources/sample.tar.gz", "corpus/2502.09992/v1")
print(result)
```

- Nhận diện bằng nội dung: tar, tar.gz, ZIP, TeX đơn hoặc TeX đơn nén gzip.
- Chỉ giữ file thường `.tex`/`.bib`, giữ bytes và đường dẫn tương đối;
  không giải mã, sửa lệnh LaTeX, flatten đường dẫn hay lấy ảnh/PDF vào corpus.
- TeX đơn được lưu thành `main.tex`. Nhận diện các lệnh TeX phổ biến trong
  64 KiB đầu; văn bản không nhận diện được trả `unsupported_source`.
- Từ chối `..`, đường dẫn tuyệt đối/Windows nguy hiểm, symlink, hardlink,
  device, đường dẫn trùng hoặc xung đột chữ hoa/thường. Không dùng `extractall()`.
- Xử lý trong thư mục tạm rồi chuyển các file đã kiểm tra sang đích.
  Thư mục đích phải chưa tồn tại; không ghi đè dữ liệu cũ.
- Giới hạn kỹ thuật: payload 100 MiB, dữ liệu giải nén 200 MiB, 10.000 entries.
  Đây là giới hạn bảo vệ của chương trình, không phải giới hạn của đề bài.

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
| `http_error`, `rate_limited`, `server_error` | HTTP lỗi; không kết luận không có TeX |
| `timeout`, `dns_error`, `connection_error`, `tls_error` | Lỗi mạng/TLS |

`extract_source()` trả `status`, `files`, `error`; các trường HTTP và định danh
được `download_source()` bổ sung. Archive chỉ có `.bib` vẫn giữ file đó nhưng
trả `no_tex_source`. Không tạo thư mục đích nếu không có file được giữ.
Kết quả vẫn giữ record version qua MSSV/ID; bước checkpoint sau này cần lưu
record này, kể cả khi không có TeX.

Chưa hỗ trợ gzip lồng nhau, archive lồng nhau, ZIP mã hóa hoặc các định dạng nén
khác như `.Z`, bz2, xz. Chưa có checkpoint, resume theo byte, benchmark hoặc
kiểm chứng tải source thật. Không bảo vệ trước process khác cố tình thay đổi
thư mục đích trong lúc chương trình chạy.

## Kiểm thử

```powershell
python -B -m unittest discover -s tests -v
```

Tests dùng archive giả lập trong thư mục tạm, mock HTTP/clock/sleep; không gọi
mạng, tải source thật hoặc đợi pacing thật.

Nguồn: [arXiv — Unpacking Retrieved Papers](https://info.arxiv.org/help/unpack.html),
[arXiv Bulk Access](https://info.arxiv.org/help/bulk_data.html),
[arXiv Terms of Use](https://info.arxiv.org/help/api/tou.html),
[M1](https://hcmus-cs-ta.github.io/lab-book/intro-ds/statement/crawling/),
[Python tarfile](https://docs.python.org/3.14/library/tarfile.html).
