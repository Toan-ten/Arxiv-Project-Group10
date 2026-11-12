# Kết nối Semantic Scholar

`src.semantic_scholar` dùng thư viện chuẩn Python để gọi
`POST /graph/v1/paper/batch` và lấy references qua endpoint phân trang.
Import module không gửi request hoặc ghi corpus. Pipeline và storage xử lý kết
quả qua [lớp tích hợp Semantic Scholar](s2_integration.md). Notebook M1 luôn
chạy giai đoạn này. Xem thêm [tài liệu API chính thức](https://api.semanticscholar.org/api-docs).

## API và đầu vào

```python
fetch_metadata(
    arxiv_ids, *, batch_size=100, timeout=30.0, max_attempts=3,
    limiter=None, max_response_bytes=10_000_000,
    require_arxiv_identity=True,
)
```

Đầu vào là list/tuple ID gốc, không có tiền tố `ARXIV:` hoặc hậu tố `vN`.
Client chấp nhận ID new-style và legacy như `hep-th/9901001`, kiểm tra toàn bộ
đầu vào/cấu hình trước request đầu tiên và chuyển mỗi ID thành `ARXIV:{id}`
trong JSON request. ID gốc, thứ tự và phần tử lặp được giữ nguyên; client không
suy ra ownership/MSSV. Caller có thể ghép kết quả theo vị trí với mapping đã có.

Ví dụ sau gọi API thật nếu được chạy. Dùng phạm vi nhỏ và khóa API được cấp cho người vận hành:

```python
from src.semantic_scholar import RateLimiter, fetch_metadata

# Dùng lại cùng bộ giới hạn cho mọi tác vụ S2 trong tiến trình.
s2_limiter = RateLimiter(min_interval=1.0)
results = fetch_metadata(
    ["2502.09992"], batch_size=100, timeout=30.0,
    max_attempts=1, limiter=s2_limiter,
)
```

Không truyền `limiter` thì mọi lời gọi dùng chung `DEFAULT_LIMITER`.
`RateLimiter.request_slot()` cung cấp khóa/cooldown chung cho cả metadata và references. Context giữ khóa đến khi đóng response, rồi chờ ít
nhất `min_interval` trước request kế tiếp. Mặc định 1 giây là lựa chọn cấu hình,
không bảo đảm quota cho mọi API key. Cần điều chỉnh theo quyền truy cập thực tế;
không tạo limiter riêng cho metadata và references. Limiter chỉ chia sẻ trong
một process, không phối hợp nhiều máy/process.

## Trường dữ liệu và kết quả

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

## Chia lô yêu cầu và kiểm tra phản hồi

Mặc định 100 ID/request; `batch_size` nằm trong 1–500. Ví dụ 501 ID mặc định tạo
6 chunk, còn `batch_size=500` tạo 2 chunk. Không tự mở rộng ID/range hoặc loại trùng.

Toàn bộ array phải đúng độ dài trước khi ánh xạ bất kỳ paper nào. Mỗi mục là
null hoặc dict có `paperId` hợp lệ; các metadata fields được kiểm tra kiểu.
Nếu response có `externalIds.ArXiv`, base ID phải trùng ID của vị trí tương ứng.
Sai một mục làm cả chunk `invalid_response`, không gán một phần rồi dịch vị trí.
Mặc định `require_arxiv_identity=True`: một paper thiếu externalIds.ArXiv làm
cả chunk `invalid_response / arxiv_identity_unverified`. `paperId`, title hoặc
URL S2 riêng lẻ không chứng minh quan hệ với arXiv ID đã hỏi. Null vẫn là kết quả
not_found theo vị trí response; không có metadata để gán sang ID khác.

`require_arxiv_identity=False` là lựa chọn rõ ràng để dùng mapping positional
cũ khi có thể chấp nhận giới hạn này. `found` trong chế độ đó **không xác minh**
danh tính nếu external ArXiv ID thiếu; nếu có ID mâu thuẫn thì vẫn từ chối cả
chunk. Ví dụ API chính thức cho kết quả theo vị trí input, nhưng phần tài liệu
đã đọc không nêu một bảo đảm thứ tự đủ rõ để thay cho kiểm tra ID. Không lấy ví
dụ hoặc mock tests làm bằng chứng production luôn giữ thứ tự. Thay đổi mặc định
này cần được người tích hợp lưu ý; hình dạng sáu fields kết quả vẫn giữ nguyên.

Response giới hạn tối đa 10.000.000 bytes; có thể cấu hình thấp hơn. Client kiểm
tra Content-Length và đọc tối đa limit + 1 byte, từ chối JSON sai, keys trùng,
NaN/Infinity, media type/encoding không hỗ trợ hoặc dữ liệu bị cắt ngắn.
HTTP 413 trả `http_error / http_413_request_too_large`; response vượt giới hạn
trả `invalid_response / response_too_large`. HTTP 400 vẫn là `http_error`,
không đoán nguyên nhân từ response body. Không tự chia nhỏ/retry lỗi kích thước;
caller có thể giảm batch trong lượt riêng sau khi xem kết quả. Các chunk độc lập
sau lỗi kích thước/cấu trúc vẫn được xử lý; chỉ 429 hết attempts dừng toàn lời gọi.

## Thử lại, giãn cách yêu cầu và khóa API

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

## Khóa API: biến môi trường hoặc tệp .env tại thư mục gốc

Metadata, References và các entrypoint Pipeline/Batch/Benchmark bật S2 dùng chung
`src.local_config.get_semantic_scholar_api_key()`. Live Smoke dùng cùng bộ đọc
để kiểm tra cấu hình và che secret, kể cả ở dry run. Không đọc key khi import;
Các lời gọi thư viện arXiv-only không cần đọc `.env`; notebook M1 vẫn yêu cầu bước S2.

Bộ đọc dùng thư viện chuẩn, không cần cài package mới. Nó chỉ mở `.env` cạnh
thư mục `src/` của repository đã clone, không tìm theo working directory,
thư mục cha hoặc home. Không nạp các biến khác vào `os.environ`.

Từ root repository, tạo file một lần nếu chưa có:

```powershell
if (-not (Test-Path -LiteralPath .env)) {
    Copy-Item -LiteralPath .env.example -Destination .env
}
notepad .env
```

Điền key vào giá trị đang trống của `SEMANTIC_SCHOLAR_API_KEY=`, lưu file và
đóng trình soạn thảo. Chỉ sửa file local; không dán key vào command, source,
notebook, log hoặc tài liệu. `.env` bị Git ignore; `.env.example` chỉ chứa
placeholder rỗng và được phép commit.

Thứ tự ưu tiên:

1. Biến môi trường đã được đặt có ưu tiên cao hơn `.env`. Biến rỗng hoặc chỉ
   có khoảng trắng giữ hành vi cũ: không gửi key và không fallback sang file.
2. Khi biến môi trường không tồn tại, đọc key từ `.env` tại root.
3. Thiếu file, thiếu biến hoặc giá trị rỗng: client tiếp tục không có key.
   Truy cập không key có thể bị hạn chế; không coi đó là key đã được cấu hình.

Quy tắc trên áp dụng cho client Semantic Scholar dùng độc lập và các phép thử
không thuộc M1. Entry point M1 Colab kiểm tra `SEMANTIC_SCHOLAR_API_KEY` bắt buộc
trước khi tạo checkpoint; M1 không gửi request khi thiếu khóa.

Key sai định dạng bị từ chối bằng lỗi cố định, không in giá trị và không âm
thầm đổi sang key khác. Chỉ gửi header `x-api-key` khi có key hợp lệ về định dạng.
Kiểm tra này không xác thực quyền truy cập API hay phát hiện mọi ký tự gõ sai.

Kiểm tra local, không gọi API và chỉ in trạng thái có/không:

```powershell
.\venv\Scripts\python.exe -B -c "from src.local_config import get_semantic_scholar_api_key; print('S2 key configured:', bool(get_semantic_scholar_api_key()))"
git check-ignore -v .env
```

Nếu Windows đang giữ biến môi trường cũ/sai, xóa riêng biến đó trong phiên
PowerShell hiện tại rồi chạy lại kiểm tra để dùng `.env`:

```powershell
Remove-Item Env:SEMANTIC_SCHOLAR_API_KEY -ErrorAction SilentlyContinue
```

Nếu trước đây đã lưu biến ở cấp User và muốn bỏ nó cho các phiên sau:

```powershell
[Environment]::SetEnvironmentVariable('SEMANTIC_SCHOLAR_API_KEY', $null, 'User')
```

Sau đó mở lại PowerShell/VS Code để tránh kế thừa biến cũ từ process cha.
Biến cấp Machine, nếu có, vẫn có thể được kế thừa; không tự thay đổi cấp đó.
Key sai một ký tự nhưng vẫn đúng định dạng ASCII sẽ không bị phát hiện offline.

Định dạng file hỗ trợ: UTF-8 (có thể có BOM), LF/CRLF, comment `#`, dòng
`KEY=value`, tùy chọn `export`, giá trị một dòng có hoặc không có dấu nháy.
Comment cuối giá trị không nháy phải cách bằng khoảng trắng. Không hỗ trợ
multiline, escape sequences hoặc nội suy biến/lệnh; duplicate key, cú pháp
sai, file quá 64 KiB hoặc symlink bị từ chối bằng thông báo không chứa secret.
Đây là bộ đọc cấu hình S2 nhỏ, không phải toàn bộ cú pháp của python-dotenv.

Trên Colab có thể đặt `os.environ["SEMANTIC_SCHOLAR_API_KEY"]` từ Colab Secrets;
không cần tạo `.env`. Bộ đọc không thay đổi biến môi trường hoặc limiter.
Client không log request headers, key, response body hoặc raw exception text.

## Kiểm thử ngoại tuyến và giới hạn

Từ root repository, dùng Python trong môi trường hiện có:

```powershell
python -B -m unittest discover -s tests -p "test_semantic_scholar.py" -v
python -B -m unittest discover -s tests -p "test_arxiv_client.py" -v
python -B -m unittest discover -s tests -v
```

Tests dùng fake opener/clock và thư mục tạm, không gọi API thật. Chưa kiểm chứng
quota/key hoặc response production bằng pilot thật. Client không lưu cache,
checkpoint hay metadata; không đánh dấu publication complete.
Tích hợp Pipeline/Storage và chính sách dừng toàn Pipeline thuộc bước sau.

## Lấy tài liệu tham khảo và bản chụp v2

`fetch_references()` lấy một snapshot mới trong bộ nhớ theo schema references
của [hợp đồng lưu trữ v2](../contracts/corpus_storage_contract.md#referencesjson).
Không ghi corpus, không cập nhật manifest, không nhận/ghi đè snapshot cũ.

```python
fetch_references(
    arxiv_id, *, fields=None, limit=100, max_pages=1000,
    timeout=30.0, max_attempts=3, limiter=None,
    max_response_bytes=10_000_000, resume_from=None,
)
```

Hàm nhận **một base ID**; legacy ID có dấu slash được giữ nguyên trong snapshot
và percent-encode khi đặt vào URL. Luôn bắt đầu offset 0. `fields=None` lấy:

```text
paperId, corpusId, title, authors, year, venue, publicationVenue,
publicationDate, externalIds, url, contexts, intents, isInfluential
```

Theo [API Graph chính thức](https://api.semanticscholar.org/api-docs), endpoint là
`GET /graph/v1/paper/ARXIV:{id}/references`. Tên fields được gửi **phẳng**, như
`title,authors,contexts`, không thêm `citedPaper.` vào query. Trong response,
metadata nằm ở `data[].citedPaper`; `contexts/intents/isInfluential` thuộc quan hệ.
Có thể truyền tuple/list tên fields hoặc chuỗi phân cách dấu phẩy. Field không
hỗ trợ, lặp hoặc cấu hình sai bị từ chối trước request; không âm thầm bỏ field.

Ví dụ sau gọi mạng nếu được chạy:

```python
from src.semantic_scholar import RateLimiter, fetch_metadata, fetch_references

s2_limiter = RateLimiter(min_interval=1.0)  # Điều chỉnh theo quyền truy cập.
# Dùng cùng s2_limiter cho metadata và references.
snapshot = fetch_references(
    "2502.09992", limiter=s2_limiter, max_attempts=1, max_pages=10,
)
```

Không truyền limiter thì cả hai API cùng dùng `DEFAULT_LIMITER`; không có
limiter riêng cho references. Thử lại, Retry-After, API key, giới hạn phản hồi và
chặn chuyển hướng dùng chung HTTP helper. `max_attempts` áp dụng cho
mỗi trang, gồm lần đầu; tối đa `max_pages * max_attempts` transport attempts.
Không có request phụ để tìm paperId của publication đang truy vấn.

### Phân trang và độ phủ

`limit` mặc định 100, hợp lệ từ 1 đến 1000. Mỗi response phải có đúng offset
đã yêu cầu và array `data` không vượt limit. Chỉ dùng `next` từ response, không
suy trang cuối từ việc trang ngắn hơn limit. `next` phải là integer (không phải
bool), tiến lên, chưa gặp trước đó và bằng offset + số items trang này; gap hoặc
overlap bị coi là pagination không hợp lệ. Giữ nguyên thứ tự và mọi phần tử trùng.

Trang cuối có `next` vắng mặt hoặc null. Chỉ sau kiểm tra terminal response và
total (nếu có), `coverage.complete=true`; không gửi request đoán thêm khi next
đã kết thúc. Trang cuối nằm đúng max_pages vẫn được coi là complete.

Snapshot giữ các trường bắt buộc của contract:

- `contract_version=2`, `arxiv_id`, `status`.
- `provenance`: provider, query_identifier, s2_paper_id, retrieved_at.
  `s2_paper_id=null` vì endpoint không cung cấp ID của publication đang truy vấn;
  không lấy ID từ cited paper. Thời điểm UTC ghi khi request cuối kết thúc, kể cả
  khi lỗi; không phải ngày nộp hoặc ngày xuất bản.
- `coverage`: complete, continuation, reported_total, retrieved_count.
  `reported_total=null` nếu API không cung cấp tổng; không suy từ count một trang.
  Tổng do API trả phải là integer nhất quán giữa các trang và khớp count cuối.
  Nếu count vượt tổng cũ, tổng đó bị đánh dấu không đáng tin (null), giữ items và
  báo lỗi; không điều chỉnh số đếm để làm snapshot có vẻ complete.
- `items`: toàn bộ references đã chuẩn hóa từ các trang hợp lệ.
- `error`: extension chẩn đoán, null khi complete; khi lỗi gồm
  `type, message, http_status, attempts, offset`. Đây là các mã cố định,
  không chứa response body/headers hoặc raw exception. Schema validator hiện có
  cho phép field bổ sung; contract và Storage không bị sửa.

`coverage.complete` chỉ nói đã duyệt hết references **S2 trả về**, không chứng minh
S2 đã nhận diện tất cả references trong source LaTeX.

### Chuẩn hóa và dữ liệu thiếu

Mỗi item có đúng các fields contract: `arxiv_id, title, authors, submission_date,
publication_date, year, venue, s2_paper_id, missing_fields, raw`.

- arXiv ID lấy từ externalIds.ArXiv; giữ nguyên ID hợp lệ, gồm legacy slash và
  hậu tố version nếu nguồn trả về. ID thiếu/không hợp lệ → null, raw vẫn giữ
  nguyên dữ liệu nguồn; không loại item.
- `s2_paper_id` lấy từ citedPaper.paperId; corpusId chỉ nằm trong raw.
- Authors là danh sách tên có bằng chứng. Authors thiếu → null; danh sách rỗng
  được trả nguyên [] và đánh dấu missing. Nếu một số author thiếu tên, giữ tên
  đã biết, đánh dấu authors chưa đầy đủ; tác giả gốc vẫn còn trong raw.
- `publication_date` chỉ lấy từ publicationDate hợp lệ; year không thành ngày.
  `submission_date` luôn null vì endpoint này không cung cấp bằng chứng ngày
  nộp arXiv, kể cả khi raw có một field lạ cùng tên.
- `missing_fields` nêu các normalized fields thiếu hoặc authors chưa đủ.
  citedPaper null/thiếu vẫn có một item với fields chưa biết, không bị loại.
- `raw` là bản sao đầy đủ của bản ghi quan hệ gốc, giữ publicationVenue,
  contexts, intents, isInfluential và metadata chưa ánh xạ. Không dedupe.

Field được chuẩn hóa hoặc dùng để xác minh danh tính sai kiểu bị coi là response
lỗi, không biến thành một reference giả. `corpusId` không được chuẩn hóa; giá trị
nguyên bản của nó nằm trong `raw`, nên kiểu bất thường của trường phụ này không
làm mất cả trang references. Trang sai offset, JSON hoặc cấu trúc item bị từ chối
nguyên trang; mọi trang hợp lệ trước đó vẫn giữ. Nếu dữ liệu items trang hiện tại hợp lệ nhưng
next/total sai, items đó vẫn được giữ và coverage không complete.

### Các trạng thái lỗi

| Trường hợp | status snapshot | Coverage và dữ liệu |
| --- | --- | --- |
| Pagination kết thúc hợp lệ, kể cả rỗng | complete | complete=true, continuation=null |
| HTTP 404 ngay trang đầu | not_found | complete=false; không coi là tập rỗng complete |
| Lỗi khi chưa có item | failed | complete=false; error phân biệt loại lỗi |
| Lỗi sau khi đã có item | partial | Giữ toàn bộ items đã nhận, complete=false |
| Chạm max_pages khi còn next | partial | Giữ continuation hợp lệ; error.type=pagination_limit |

404 ở trang sau không hủy dữ liệu cũ: partial, kèm HTTP 404. Hết attempts còn
429: failed/partial với `error.type=rate_limited`; không request trang tiếp theo.
5xx/mạng/timeout chỉ retry hữu hạn. Timeout có type riêng; HTTP status và mã lỗi
giữ trong error. Lỗi JSON/next/total dùng `invalid_response`.

Nếu trang chưa chấp nhận bị lỗi HTTP/JSON, continuation là `{"offset": N}`
của trang cần thử lại. Nếu next sai kiểu/lặp/không tiến hoặc total mâu thuẫn, continuation=null vì
không có offset tiếp theo đáng tin cậy. Caller không được diễn giải null là complete
khi status/coverage còn incomplete. Có thể dùng `resume_from` theo kiểm tra và
cách đọc lại ở phần dưới; client không nhảy thẳng tới continuation.

Client không sửa snapshot tốt của lời gọi trước. Khi response bất thường chứa
chính API key đang dùng, từ chối trang với `response_contains_secret`, không
redact raw rồi giả vờ trang đã đầy đủ. Numeric overflow/NaN không được đi vào
snapshot; lỗi đóng HTTP error stream được chuyển thành mã cố định. Nếu đã quan
sát HTTP 429 mà đóng stream/context bị lỗi, vẫn giữ `rate_limited / http_429`
và dừng ngay, kể cả khi còn attempts. Cleanup không được che tín hiệu 429 hoặc
cho phép metadata gửi chunk tiếp theo.

Kiểm thử riêng cho schema references:

```powershell
python -B -m unittest discover -s tests -p "test_semantic_scholar_references.py" -v
```

Các tests schema gọi validator references hiện có ở chế độ chỉ đọc, không gọi
Storage writer hoặc tạo corpus. Chưa kiểm chứng endpoint bằng live S2 pilot.

## Tiếp tục bằng cách đọc lại và đối chiếu bản chụp

`fetch_references(..., resume_from=partial_snapshot)` không ghi vào Storage.
Đây là cơ chế phục hồi dữ liệu trong bộ nhớ có kiểm tra lại, **không phải**
tối ưu bỏ qua các request prefix. Nó vẫn đọc mới từ offset 0, dùng cùng limiter,
timeout, attempts, fields, limit và max_pages; không có request verification phụ
ngoài lượt pagination mới. Có thể đổi page limit, nhưng thay đổi fields có thể
làm raw prefix khác và bị từ chối.

Trước HTTP, snapshot phải hợp lệ:

- Contract version 2, cùng arXiv ID và query_identifier, provider semantic_scholar.
- Status partial/failed/interrupted, coverage.complete=false, retrieved_count
  đúng số items, reported_total null hoặc integer không nhỏ hơn số items.
- Continuation có đúng dạng `{"offset": N}`, N là integer không phải bool,
  không âm và bằng số items đã có; không nhận continuation riêng lẻ.
- Provenance có timestamp với timezone. Giao diện hiện chỉ nhận snapshot client
  với parent s2_paper_id=null; không tự tin một parent ID caller gán vào.
- Mỗi item phải khớp hoàn toàn với kết quả normalize raw của chính nó. Không tin
  normalized fields đã sửa tay hoặc snapshot chứa dữ liệu không phải JSON hữu hạn.
- Không có chính API key hiện tại trong snapshot. Lỗi validation dùng thông báo
  cố định, không echo dữ liệu/key. Snapshot không hợp lệ không phát HTTP.

Sau lượt đọc mới:

1. Nếu prefix mới khớp nguyên vẹn mọi item cũ và đã đọc ít nhất tới số items cũ,
   trả **snapshot mới**, không nối thêm prefix cũ. Giữ cả duplicate và đúng thứ tự.
2. Nếu request lỗi trước khi đọc lại đủ prefix, trả bản sao snapshot cũ với status
   partial/failed và lỗi mới; không rút ngắn items để khớp lượt lỗi.
3. Nếu prefix thay đổi, publication 404 trong khi đã có dữ liệu, hoặc kết quả
   complete mới ngắn hơn dữ liệu cũ, giữ items cũ, báo mâu thuẫn và xóa continuation.
   Không tự chọn bản mới để thay thế hoặc tự dedupe.
4. HTTP 429 ở lượt mới luôn còn trong `error.type/http_status`. Nếu đồng thời có
   mâu thuẫn prefix, thêm `error.resume_reason`; không thay 429 bằng lỗi resume.

Khi giữ dữ liệu cũ, provenance cũ được giữ nguyên. Thời điểm lần thử mới nằm ở
`error.observed_at`, tránh gắn references cũ với phản hồi lỗi mới. Lỗi so sánh
snapshot không gắn với một HTTP attempt cụ thể có http_status/attempts/offset
null, không suy số attempts từ số trang.

Ví dụ sử dụng sau gọi API nếu được chạy:

```python
continued = fetch_references(
    "2502.09992", resume_from=partial_snapshot,
    limiter=s2_limiter, max_attempts=1, max_pages=10,
)
```

Giới hạn: API offset/next không cung cấp token snapshot bất biến trong giao diện
đang dùng. Đọc lại phát hiện prefix đổi giữa hai lượt, nhưng không chứng minh
upstream đứng yên giữa mọi request trong cùng lượt. Client không xác thực tính
nguyên gốc của file checkpoint, không khóa writer, không tự skip snapshot complete
và không lưu kết quả. Những việc này cần Storage/Pipeline Integration riêng.

Khi tích hợp, cần kiểm tra checksum/schema/ownership trước gọi client; bật identity
policy phù hợp; dùng một limiter S2; lưu snapshot qua Storage API chỉ khi bảo toàn
prefix và giữ bản tốt; xử lý error.http_status=429 trước khi sang paper khác.
Không dùng batch finished hoặc source success để suy publication complete.
HTTP 413/oversize vẫn được báo hữu hạn và không tự chia chunk hoặc chia để né 429.

Kiểm thử safety bổ sung nằm trong hai file tests S2 hiện có, dùng fake HTTP/clock.
Hai ca metadata cũ về ID thiếu được điều chỉnh theo mặc định strict; mục đích
kiểm tra metadata thiếu và explicit positional mode vẫn có regression riêng.
Client mức thấp không thay thế pilot hoặc benchmark Colab; xem quy trình M1 để biết phạm vi kiểm chứng.

## Thử lại có giới hạn và giãn cách yêu cầu

Batch gọi Pipeline một lần; Pipeline tạo một RateLimiter S2 và truyền cùng
instance qua mọi paper, Metadata và References. arXiv dùng limiter riêng.
Request đầu tiên đi ngay nếu không có cooldown. Limiter chỉ phối hợp các lời
gọi dùng cùng instance trong một process; không phối hợp máy/phiên khác.

Các tham số keyword-only mới của fetch_metadata()/fetch_references():

- max_retry_wait=60.0: tổng thời gian sleep của limiter trong một HTTP operation
  (một chunk metadata hoặc một trang references), gồm pacing và backoff.
- backoff_base=1.0, backoff_cap=60.0: giữ cơ sở mặc định tương thích trước đây.
  Lần retry thứ n chờ tối thiểu min(base * 2**(n-1) + jitter[0,1], cap),
  đồng thời tôn trọng pacing và Retry-After lớn hơn.
- max_attempts tiếp tục gồm lần đầu; max_attempts=1 không tự retry.

Retry-After hỗ trợ số nguyên giây hoặc HTTP-date. Header sai/hết hạn dùng backoff.
Nếu thời gian chờ cần thiết lớn hơn ngân sách còn lại, không sleep đến hết ngân
sách rồi gửi sớm: trả kết quả lỗi cuối cùng với retry_wait_budget_exceeded.
HTTP 429 vẫn là rate_limited/429; 403 không retry và dừng các chunk metadata còn lại.
Kết quả references lỗi/partial vẫn giữ items, continuation và coverage chưa complete.

Limiter đã có cooldown có thể từ chối trước request đầu tiên:
error=cooldown_wait_budget_exceeded, attempts=0. http_status là status đã quan sát
gây cooldown (nếu có), không phải response mới. Metrics không đếm thêm request.
Integration chỉ chấp nhận attempts=0 cho lỗi admission cụ thể này; success vẫn cần
attempt thực và identity hợp lệ.

Ngân sách áp dụng riêng từng operation, không phải deadline toàn Batch hoặc thời
gian HTTP I/O. Max pages và số paper vẫn cần giới hạn. Sleep thực đo bằng monotonic;
OS suspension/scheduling có thể làm elapsed vượt ngân sách, khi đó không gửi retry.

Ví dụ cấu hình kiểm thử:
min_interval=2, max_attempts=3, max_retry_wait=60, backoff_base=5, backoff_cap=30.
Retry-After dài hơn ngân sách sẽ dừng, không bị cap xuống để né giới hạn.

Client mức thấp không tự tiếp tục sau khi policy retry dừng và limiter không lưu
cooldown qua restart. Riêng workflow M1 S2 có thể bật auto-resume giới hạn trong
cùng process sau 429 ở metadata/references: chỉ khi checkpoint hợp lệ, tôn trọng
Retry-After còn hiệu lực, chờ cooldown 5–10 phút và có đủ ngân sách để thử tiếp.
Notebook bật một lần mỗi cohort, tối đa hai lần mỗi phiên; 403, checkpoint lỗi,
hết ngân sách và lỗi khác vẫn dừng. Nếu process bị ngắt trong lúc chờ, lần chạy
mới vẫn cần được khởi động lại thủ công từ cell S2 bằng cùng phạm vi/root.
