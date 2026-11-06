# Semantic Scholar metadata client (Issue #5)

`src.semantic_scholar` dùng thư viện chuẩn Python để gọi
`POST /graph/v1/paper/batch`. Module chỉ trả metadata; chưa gọi references,
chưa nối Pipeline/Storage và không tự gọi mạng khi import. API được đối chiếu với
[Issue #5](https://github.com/Toan-ten/Arxiv-Project-Group10/issues/5) và
[tài liệu Semantic Scholar](https://api.semanticscholar.org/api-docs).

## API và đầu vào

```python
fetch_metadata(
    arxiv_ids, *, batch_size=100, timeout=30.0, max_attempts=3,
    limiter=None, max_response_bytes=10_000_000,
)
```

Đầu vào là list/tuple ID gốc, không có tiền tố `ARXIV:` hoặc hậu tố `vN`.
Client chấp nhận ID new-style và legacy như `hep-th/9901001`, kiểm tra toàn bộ
đầu vào/cấu hình trước request đầu tiên và chuyển mỗi ID thành `ARXIV:{id}`
trong JSON request. ID gốc, thứ tự và phần tử lặp được giữ nguyên; client không
suy ra ownership/MSSV. Caller có thể ghép kết quả theo vị trí với mapping đã có.

Ví dụ sau **sẽ gọi API thật nếu chạy**, chưa được thực hiện trong lượt triển khai
offline này. Chỉ chạy sau khi duyệt quyền truy cập và phạm vi pilot:

```python
from src.semantic_scholar import RateLimiter, fetch_metadata

# Dùng lại cùng instance cho mọi tác vụ S2 trong process.
s2_limiter = RateLimiter(min_interval=1.0)
results = fetch_metadata(
    ["2502.09992"], batch_size=100, timeout=30.0,
    max_attempts=1, limiter=s2_limiter,
)
```

Không truyền `limiter` thì mọi lời gọi dùng chung `DEFAULT_LIMITER`.
`RateLimiter.request_slot()` cung cấp khóa/cooldown chung để client References
sau này có thể tái sử dụng. Context giữ khóa đến khi đóng response, rồi chờ ít
nhất `min_interval` trước request kế tiếp. Mặc định 1 giây là lựa chọn cấu hình,
không bảo đảm quota cho mọi API key. Cần điều chỉnh theo quyền truy cập thực tế;
không tạo limiter riêng cho metadata và references. Limiter chỉ chia sẻ trong
một process, không phối hợp nhiều máy/process.

## Fields và kết quả

`fields` được gửi qua query parameter duy nhất, gồm:

```text
paperId, corpusId, externalIds, title, abstract, authors, year, venue,
publicationVenue, publicationTypes, journal, citationCount,
influentialCitationCount, referenceCount, fieldsOfStudy, s2FieldsOfStudy,
openAccessPdf, url
```

Không nhầm `paperId` với `corpusId`. Giá trị thiếu/null được giữ như response,
không tự bổ sung tác giả, ngày hay metadata khác; không biến `year` thành ngày.

Mỗi vị trí đầu vào nhận đúng một dict có sáu trường:

| Trường | Ý nghĩa |
| --- | --- |
| `arxiv_id` | ID gốc của vị trí đầu vào |
| `status` | `found / not_found / rate_limited / http_error / timeout / invalid_response` |
| `http_status` | HTTP status nhận được, hoặc null khi chưa nhận/không gửi |
| `attempts` | Số lần thử request của chunk chứa ID; không phải tổng request toàn lời gọi |
| `paper` | Dict metadata nguyên giá trị đã kiểm tra, hoặc null |
| `error` | Mã chẩn đoán cố định, hoặc null; không chứa response body/key |

Chỉ phần tử JSON `null` trong response HTTP 200 hợp lệ mới là `not_found`.
HTTP 404 là `http_error`. Timeout, response hỏng và rate limiting không được
đổi thành `not_found`.

## Chunking và kiểm tra response

Mặc định 100 ID/request; `batch_size` nằm trong 1–500. Ví dụ 501 ID mặc định tạo
6 chunk, còn `batch_size=500` tạo 2 chunk. Không tự mở rộng ID/range hoặc loại trùng.

Toàn bộ array phải đúng độ dài trước khi ánh xạ bất kỳ paper nào. Mỗi mục là
null hoặc dict có `paperId` hợp lệ; các metadata fields được kiểm tra kiểu.
Nếu response có `externalIds.ArXiv`, base ID phải trùng ID của vị trí tương ứng.
Sai một mục làm cả chunk `invalid_response`, không gán một phần rồi dịch vị trí.
Khi không có external ArXiv ID, client dựa trên quy ước thứ tự của batch API;
không thể tự chứng minh danh tính từ title/abstract và không đoán để reorder.

Response giới hạn tối đa 10.000.000 bytes; có thể cấu hình thấp hơn. Client kiểm
tra Content-Length và đọc tối đa limit + 1 byte, từ chối JSON sai, keys trùng,
NaN/Infinity, media type/encoding không hỗ trợ hoặc dữ liệu bị cắt ngắn.
HTTP 413 trả `http_error / http_413_request_too_large`; response vượt giới hạn
trả `invalid_response / response_too_large`. HTTP 400 vẫn là `http_error`,
không đoán nguyên nhân từ response body. Không tự chia nhỏ/retry lỗi kích thước;
caller có thể giảm batch trong lượt riêng sau khi xem kết quả. Các chunk độc lập
sau lỗi kích thước/cấu trúc vẫn được xử lý; chỉ 429 hết attempts dừng toàn lời gọi.

## Retry, pacing và API key

`max_attempts` tính cả request đầu tiên. HTTP 429, 5xx và lỗi mạng/timeout có
retry hữu hạn; lỗi TLS không retry. Các 4xx khác và redirect không retry.
Redirect bị chặn để không gửi key sang host khác hoặc phát request ngoài pacing.

Trước attempt kế tiếp, cooldown là giá trị lớn hơn giữa:
`min(2**(attempt-1), 60) + uniform(0, 1)` giây và Retry-After hợp lệ.
Retry-After hỗ trợ delay-seconds hoặc HTTP-date; giá trị không hợp lệ bị bỏ qua.
Cùng limiter tiếp tục áp dụng khoảng chờ sau khi đóng response.
Retry-After được giữ trong cooldown kể cả khi đã hết attempts.

Nếu 429 còn tồn tại sau attempts cuối:

- Chunk vừa gửi có `rate_limited`, `http_status=429`, số attempts thực tế.
- Không gửi chunk tiếp theo trong lời gọi.
- Mỗi ID còn lại vẫn có kết quả: `status=rate_limited`, `http_status=null`,
  `attempts=0`, `paper=null`, `error=not_attempted_after_rate_limit`.
  Đây là ID chưa gửi do dừng theo chính sách, không phải một HTTP 429 đã quan sát.
- Kết quả các chunk trước được giữ nguyên.

Key chỉ đọc từ biến môi trường `SEMANTIC_SCHOLAR_API_KEY` khi gọi hàm với đầu
vào không rỗng. Chỉ thêm `x-api-key` khi có key; chuỗi rỗng không tạo header.
Key có ký tự không hợp lệ bị từ chối bằng lỗi cố định, không in giá trị.
Client không log request headers, key, response body hoặc raw exception text.
Không đặt key vào file nguồn, README, notebook hay command có thể lưu lịch sử.

## Kiểm thử offline và giới hạn

Từ root repository, dùng Python trong môi trường hiện có:

```powershell
python -B -m unittest discover -s tests -p "test_semantic_scholar.py" -v
python -B -m unittest discover -s tests -p "test_arxiv_client.py" -v
python -B -m unittest discover -s tests -v
```

Tests dùng fake opener/clock và thư mục tạm, không gọi API thật. Chưa kiểm chứng
quota/key hoặc response production bằng pilot thật. Client không lưu cache,
checkpoint hay metadata; không đánh dấu publication complete. References,
tích hợp Pipeline/Storage và chính sách dừng toàn Pipeline thuộc bước sau.
