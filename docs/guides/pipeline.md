# Xử lý ID trong thư viện

Tài liệu này mô tả `src.pipeline`, API thư viện dùng để xử lý các cặp `(mssv, arxiv_id)`. Quy trình M1 của thành viên có một điểm vào vận hành là notebook. Notebook gọi ba pha qua `src.m1_workflow`; phần triển khai nằm trong `src.m1_phase_modules`. Mỗi pha dùng cùng phạm vi ID và có checkpoint riêng. `run_m1_member()` gọi tuần tự arXiv → S2 `batch_m1` → kiểm toán trên cùng phạm vi. Xem [kiến trúc và trách nhiệm các module](../architecture/repository-structure.md).

## Kiểm thử ngoại tuyến trên Windows PowerShell

Từ thư mục gốc repo, sau khi tạo môi trường `.venv` theo [hướng dẫn đóng góp](../../CONTRIBUTING.md), chạy:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_pipeline.py
.\.venv\Scripts\python.exe -m pytest
```

Kiểm thử pipeline thay HTTP opener, đồng hồ, hàm chờ và độ lệch ngẫu nhiên bằng dữ liệu giả lập. Parser, bộ tải/giải nén, storage, JSON và checksum vẫn chạy trên dữ liệu mẫu trong thư mục tạm. Toàn bộ suite còn có kiểm thử ngắt tiến trình và máy chủ loopback do kiểm thử tự khởi tạo; các tình huống đó không chứng minh khả năng chịu mất điện hoặc tính đúng trên mọi hệ thống tệp. Không cần khóa API, truy cập dịch vụ bên ngoài hoặc dùng corpus production để chạy kiểm thử. Dùng `pytest` để thu thập cả kiểm thử `unittest` lẫn các trường hợp tham số hóa.

## Điểm vào vận hành

Notebook M1 là điểm vào duy nhất để thu thập dữ liệu. `run_selected()` là API
mức thấp phục vụ kiểm thử và phát triển mô-đun; lời gọi này có thể gửi yêu cầu
HTTP thật. Không dùng các tham số lựa chọn phiên bản, tắt lịch sử phiên bản hoặc
tắt S2 để thay đổi quy trình M1. Để chạy một tập mẫu, giới hạn `COHORT_LIMIT`
trong notebook; các giai đoạn và kiểm tra vẫn giữ nguyên.

`pairs` là list/tuple các cặp `(MSSV, base_arxiv_id)`, không nhận generator.
Toàn bộ input, ownership, ID trùng và HTTP options được kiểm tra trước mọi HTTP
hoặc ghi checkpoint. ID trùng bị từ chối; list rỗng không tạo output.
Storage dùng config mặc định của repository; chưa hỗ trợ config riêng cho pipeline.

## Chọn lần nộp trong lời gọi thư viện

`versions=None` (mặc định) giữ hành vi xử lý mọi lần nộp. Có thể truyền list/tuple
không rỗng, gồm các tên duy nhất theo dạng `v1`, `v2`...; không nhận string đơn,
tên có số 0 ở đầu, đường dẫn hoặc tên trùng. Input sai gây `ValueError` trước
HTTP hoặc ghi checkpoint.

Pipeline lọc theo tên lần nộp chính xác trước `begin_version()`, không lấy phần
tử đầu của retry. Metadata và manifest vẫn giữ toàn bộ các lần nộp đã được xác nhận.
Các lần nộp chưa được chọn vẫn pending nếu chưa xử lý; trạng thái có sẵn được
giữ theo cơ chế recovery/validation của Storage. Resume vẫn kiểm tra toàn bộ
checkpoint và source, kể cả lần nộp ngoài lựa chọn.

Discovery unresolved không được suy thành v1. Nếu một tên được chọn chưa có trong
danh sách resolved, summary báo `blocked[version]="not_discovered"` và không tải
source nào của paper đó trong lượt này. Không thêm lần nộp hoặc trạng thái mới
vào manifest để đáp ứng lựa chọn không được xác nhận.

Với đúng một paper, `versions=("v1",)`, `max_attempts=1`, lần chạy mới có tối đa
**1 discovery + 1 source = 2 HTTP requests**, tính cả attempts/retry. HTTP client
không tự theo redirect và pipeline không có vòng retry ngoài. Đây là giới hạn
của cấu hình cụ thể này; thêm bài, lần nộp hoặc tăng số lượt thử sẽ thay đổi giới hạn.
Không cộng attempts của record cached/skipped thành request mới.

Chạy lại cùng corpus root, run ID và lựa chọn v1 hợp lệ dùng discovery cached,
skip v1 và không tải v2: **0 requests mới**. Không tự chạy lại sau lỗi để vượt
ngân sách pilot; cần duyệt riêng lần thử mạng tiếp theo. Các tests xác minh số
lần gọi fake HTTP opener, không gọi API thật. Ví dụ trên chưa phải bằng chứng
ID tồn tại, có TeX hoặc pilot đã thành công.

## Luồng xử lý và tiếp tục

1. Khởi tạo/đọc đủ ba JSON và gọi `resume()` để phục hồi checkpoint.
2. Dùng lại discovery `found`/`not_found`; không query lại chỉ vì paper còn partial.
   Discovery lỗi có thể được query lại một lần gọi hàm ở phiên sau.
3. `found` nhưng chưa xác định được các lần nộp thì báo `blocked/needs_verification`.
   V1 chưa có bước xác minh bổ sung hoặc refresh tự động; không suy thành v1.
4. Với các lần nộp đã xác nhận, dùng kế hoạch bỏ qua, thử lại hoặc chặn của Storage.
   Gọi `begin_version()` trước download, dùng đúng destination trả về;
   không tự tạo thư mục đích. `None` nghĩa lần nộp đã hoàn tất hợp lệ.
5. Chuyển nguyên result downloader cho `record_source_result()`.
   Storage kiểm tra file list, bytes và SHA-256 trước khi commit success.
   no-TeX được giữ riêng, gồm trường hợp chỉ có BibTeX hoặc không có thư mục.
6. Source hợp lệ được skip. File thiếu/checksum sai, orphan hoặc checkpoint hỏng
   được giữ để audit. Không xóa, tải đè hoặc tự công nhận `output_exists`.

Một lần nộp lỗi thông thường không ngăn các lần nộp hoặc bài khác tiến triển nếu còn
an toàn. Kết quả HTTP 403/429 do client trả về sẽ dừng toàn bộ batch sau khi thử
lưu kết quả qua Storage; không tải lần nộp hoặc paper tiếp theo.
Mỗi lần nộp được gọi downloader tối đa một lần trong một lần chạy pipeline;
retry bên trong HTTP client vẫn theo `max_attempts`, gồm lần thử đầu.
Dùng chung pacing/lock, backoff và Retry-After của client hiện tại; không reset limiter.
API cấp thấp này không cung cấp khóa ghi liên tiến trình; bên gọi phải bảo đảm chỉ có một tiến trình ghi. Khóa theo MSSV thuộc lớp pha M1, không phải bảo đảm chung của mọi lời gọi `run_selected()`.

## Tóm tắt và lỗi

Top-level có `run_id`, `papers`, `stopped`. Khi dừng do HTTP 403/429, summary
còn có `stop_reason` với `type="http_policy"`, `stage`, `http_status`,
`mssv`, `arxiv_id`, `version` (null ở discovery), `checkpoint_saved`.
Các paper đã xử lý và paper
gây dừng vẫn có trong `papers`; paper chưa chạy không được khởi tạo thêm.
Mỗi paper có:

- `mssv`, `arxiv_id`, `paper_status`, `metadata_status`, `references_status`;
- `discovery`: action queried/cached/not_attempted, status, version_status,
  HTTP status, attempts và error;
- `source`: map vN → action, status, HTTP status, attempts và error;
- `skipped`, `pending`, `blocked`, `errors`.

`action` mô tả thao tác trong lần chạy: downloaded/skipped/failed/blocked/pending/in_progress.
`status` giữ trạng thái hợp đồng của discovery và lần nộp. Không ghi skipped hoặc
blocked thành trạng thái mới trong manifest. `pending` chỉ liệt kê lần nộp có
checkpoint pending; timeout không bị đổi thành chưa chạy.
HTTP fields/attempts của record cached/skipped thuộc lần thu thập đã lưu,
không phải request mới trong phiên hiện tại.

Dừng theo chính sách HTTP trả summary bình thường với `stopped=True`, không ném
`PipelineStopped`. Bình thường `checkpoint_saved=True`: kết quả và lỗi
discovery/source được lưu trước khi dừng:
403 giữ `http_error`, 429 giữ `rate_limited`; các lần nộp chưa tải vẫn pending.
`not_found` hoặc HTTP 404 không tự dừng toàn batch, vẫn theo hành vi lỗi paper
hiện tại. Với source 404, pipeline truy vấn trang abstract chính thức của đúng
lần nộp bằng bộ giới hạn tốc độ và retry dùng chung của arXiv. Chỉ banner rút
bài rõ ràng trên trang đúng ID mới chuyển trạng thái sang `withdrawn_version`;
comment chung, 404 ở trang hoặc trang không khớp lần nộp vẫn giữ trạng thái cần
xử lý. Source 404 không được đổi thành `not_found` hoặc `no_tex_source`.

StorageError/IntegrityError được ghi vào báo cáo và chặn bài hoặc lần nộp liên quan.
Lỗi I/O khi ghi JSON, downloader trả storage_error hoặc exception bất thường
dừng run bằng `PipelineStopped`; đọc `exception.summary` để xem phần đã xử lý.
KeyboardInterrupt được truyền ra, giữ checkpoint đã commit để lần sau resume.
`stopped=False` chỉ nghĩa không có chính sách/lỗi nghiêm trọng dừng run, không
phải mọi paper thành công. Nếu Storage từ chối checkpoint 403/429 bằng
StorageError, batch vẫn dừng và báo `checkpoint_saved=False` cùng lỗi storage;
không tự sửa dữ liệu hoặc gửi request tiếp theo. Lỗi I/O khi ghi vẫn ném
`PipelineStopped`; không báo một lần dừng HTTP đã lưu thành công.

Chính sách xét HTTP status cuối cùng mà client trả về. Với `max_attempts=1`,
không có request tiếp theo sau khi gặp 403/429. Với attempts lớn hơn 1, client
vẫn có thể retry 429 trước khi trả kết quả; nếu retry thành công hoặc kết thúc
bằng status khác, pipeline không biết 429 trung gian và có thể tiếp tục.
Muốn dừng ở từng attempt cần thay đổi giao diện client ở bước sau. Pilot cần
giữ `max_attempts=1`; không tự gọi lại pipeline sau khi bị dừng. Pacing và
Retry-After trong cùng process được giữ nguyên, chưa có cooldown bền vững qua
restart. Summary policy cũng chưa được ghi thành run log.

Khi S2 mặc định tắt, paper mới vẫn partial nếu chỉ thu xong source:
references giữ not_attempted, items rỗng và coverage chưa complete.
Metadata/references đã có được giữ nguyên; paper complete từ trước có thể được skip.
Truyền `s2_config={...}` để bật metadata và references theo publication;
xem [S2 integration](s2_integration.md) về cache, checkpoint và dừng 429.

Pipeline chưa có log từng HTTP attempt, request budget,
đầy đủ ngày revision, CLI hoặc benchmark M1. Summary được trả trong bộ nhớ;
checkpoint từng paper do Storage quản lý. [Batch Runner](batch.md) bổ sung
run logs và lịch sử attempt qua progress callback.
Benchmark foundation không đọc summary để xác định corpus success; không dùng
dummy benchmark hoặc fixtures làm kết quả corpus thật.

Nguồn quy định: [hợp đồng lưu trữ](../contracts/corpus_storage_contract.md),
[HTTP client](arxiv_client.md), [Source Downloader](arxiv_source.md).

## Hàm gọi lại báo tiến độ

`on_progress(event, summary_copy)` nhận `paper_started` trước xử lý và
`paper_finished` sau mỗi paper, kể cả khi dừng 403/429. Snapshot là bản sao độc lập.
Lỗi callback truyền ra ngay, không chuyển sang HTTP tiếp theo. Lỗi nghiêm trọng
hoặc KeyboardInterrupt có thể không có callback kết thúc; bên gọi phải ghi trạng thái
cuối từ exception. [Batch Runner](batch.md) dùng callback để lưu tiến độ atomic.


## Tự động bổ sung lịch sử phiên bản

`revision_history=True` là mặc định khi gọi Pipeline trực tiếp. Sau khi xử lý
source, Pipeline gọi `enrich_version_history()` một lần cho paper đã resolve,
trước Semantic Scholar. Chọn `versions=("v1",)` chỉ giới hạn source; history
vẫn xác minh ngày của toàn bộ các lần nộp được Discovery phát hiện. Để giữ ngân
sách pilot v1 cũ, truyền `revision_history=False`; Live Smoke cố định đã tắt
bước này, không thêm CLI flag hoặc thay đổi marker của smoke cũ.

V1 có ngày submission và bằng chứng Discovery hợp lệ không cần request thêm.
History đầy đủ được kiểm chứng lại từ cache; không gọi lại API. Paper nhiều
các lần nộp chưa có lịch sử cần tối đa N request nếu `max_attempts=1`,
ngoài Discovery và source. Mỗi response phải khớp đúng ID lần nộp và ngày có
múi giờ. Không suy ngày các revision từ `updated` của latest. HTTP client,
lock, pacing >= 3 giây, retry hữu hạn và HTTP metrics hiện có được dùng chung.

Summary có `revision_history`: action/status, provider/query ID, retrieved_at,
observed_at, received_versions, HTTP status/error và wall_seconds. XML đầy đủ
chỉ nằm trong metadata provenance khi history đã xác minh, không nhân bản vào
run logs. Các callback vẫn chỉ là `paper_started` và `paper_finished`.

Khi history timeout/response thiếu ngày, metadata chưa đủ ngày vẫn partial;
ngày đã biết và source/S2/references cũ được giữ. Kết quả lỗi và provenance truy
vấn nằm trong summary/run log hiện có. Prefix history nhận được trước lỗi chưa
được checkpoint riêng: resume skip source tốt rồi thử lại history từ đầu.
Cache hỏng/mâu thuẫn bị blocked để audit, không tự refetch đè. Lỗi ghi I/O
nghiêm trọng dừng Pipeline; 403/429 history dừng toàn batch trước S2/ID kế tiếp.
History failure không được ghi đè metadata tốt: stop_reason.checkpoint_saved
là false cho lỗi history; diagnostic vẫn được callback ghi vào attempt log.

History thành công không tự hoàn thành publication. Metadata còn partial nếu
thiếu title/authors/ngày hoặc chưa có S2 verified; publication còn partial nếu
thiếu mã nguồn của một lần nộp hoặc references theo hợp đồng.
