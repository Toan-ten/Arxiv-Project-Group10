# HTTP client cho Entry Discovery

`query_arxiv(mssv, arxiv_id)` truy vấn một ID gốc bằng `id_list`, sau đó chuyển
Atom XML cho `parse_arxiv_response()`. MSSV và ID được giữ trong mọi kết quả.
Hàm nhận trực tiếp cặp `(mssv, arxiv_id)` từ `iter_assigned_ids()`.

Mặc định: `timeout=30.0`, `max_attempts=3`, User-Agent nhận diện Group 10 kèm
URL repository. `max_attempts` tính cả lần đầu; timeout áp dụng cho thao tác
socket, không phải tổng thời gian gồm chờ và retry. Có thể truyền User-Agent
ASCII riêng, chẳng hạn để thêm thông tin liên hệ.

## Giãn cách và retry

- Đóng response trước lần tiếp theo; chờ ít nhất 3 giây sau mỗi lần hoàn tất,
  kể cả khi lỗi. Dùng đồng hồ monotonic và kiểm tra lại sau khi chờ.
- Bộ hẹn giờ và khóa dùng chung cho các lần gọi trong cùng process. Khóa chỉ
  tuần tự hóa lời gọi, không tạo thread hoặc worker.
- Retry HTTP 429, 5xx, timeout, DNS và lỗi kết nối. Backoff bắt đầu từ 3 giây,
  tăng gấp đôi, giới hạn phần backoff ở 60 giây, cộng jitter tối đa 1 giây.
- `Retry-After` hỗ trợ số giây và HTTP-date. Chờ theo giá trị lớn nhất giữa
  giãn cách, backoff và chỉ dẫn server; không cắt chỉ dẫn server xuống 60 giây.
  Chỉ dẫn vẫn áp dụng cho lần gọi sau nếu đã hết số lần thử hiện tại.
- Không tự theo redirect. HTTP 3xx/4xx khác 429, lỗi TLS, MIME sai, XML lỗi
  hoặc API error entry được trả về, không tự retry.

Chỉ chạy một process truy vấn; không chạy đồng thời notebook/script hoặc máy
khác để nhân tốc độ. Giới hạn arXiv áp dụng chung cho các máy do nhóm kiểm soát.

## Kết quả

Khi nhận HTTP 200 với Content-Type Atom/XML, hàm giữ kết quả của parser và thêm
`http_status`, `attempts`. `found`, `not_found`, `api_error`, `invalid_xml`,
`invalid_response` có ý nghĩa như [tài liệu parser](arxiv_discovery.md).

Lỗi transport trả dictionary gồm `mssv`, `arxiv_id`, `status`, `http_status`,
`attempts`, `error`; không có metadata publication:

| `status` | Ý nghĩa |
| --- | --- |
| `rate_limited` | HTTP 429 sau khi hết lượt thử |
| `server_error` | HTTP 5xx sau khi hết lượt thử |
| `http_error` | HTTP khác 200, không thuộc hai trường hợp trên |
| `timeout` | Timeout kết nối hoặc đọc dữ liệu |
| `dns_error` | Lỗi phân giải tên miền |
| `connection_error` | Lỗi kết nối, ngắt kết nối hoặc đọc thiếu dữ liệu |
| `tls_error` | Lỗi TLS/chứng chỉ, không tự retry |

HTTP 200 với MIME thiếu/sai trả `invalid_response` trước khi gọi parser.
HTTP 404 và lỗi mạng không trở thành `not_found`. Chỉ parser xác nhận feed
hợp lệ không có paper mới trả `not_found`. Đối số sai gây `ValueError` trước
khi gửi request.

## Kiểm thử offline

```powershell
python -B -m unittest discover -s tests -v
```

Tests mock HTTP, monotonic time, thời gian thực, sleep và jitter; không gọi mạng
hoặc chờ thật. Module ID và parser có bộ kiểm thử riêng được giữ nguyên.

## Đề xuất thử thủ công ở bước sau

Chỉ chạy khi được cho phép gọi API thật. Chọn tối đa ba cặp trong cấu hình:
`("24120232", "2502.08235")`, `("24120010", "2508.07872")`,
`("24120035", "2507.16659")`. Chúng vẫn là ID ứng viên, chưa xác minh tồn tại.

Gọi tuần tự `query_arxiv(mssv, arxiv_id, timeout=15, max_attempts=1)` cho từng
cặp, không duyệt toàn bộ generator. Ghi nhận `status`, `http_status`, `attempts`,
ID, version và thời gian; tối đa ba request, dừng để xem xét khi gặp 429, 403,
redirect hoặc lỗi khác. Nếu server gửi `Retry-After`, giữ nguyên thời gian chờ.

Nguồn: [arXiv API Manual](https://info.arxiv.org/help/api/user-manual.html),
[Terms of Use](https://info.arxiv.org/help/api/tou.html),
[Retry-After — RFC 9110](https://www.rfc-editor.org/rfc/rfc9110.html#section-10.2.3).
