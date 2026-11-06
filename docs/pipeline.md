# Pipeline v1 — tích hợp tuần tự

Module `src/pipeline.py` nối ID/config → Discovery → Downloader → Storage.
Chỉ nhận danh sách ID được chỉ định, không tự mở rộng assigned ranges.
V1 được kiểm thử bằng fake HTTP và corpus fixture trong TEMP; chưa phải bằng
chứng chạy crawler trên arXiv thật hoặc hoàn tất M1.

## Kiểm thử offline trên Windows PowerShell

Từ root repository, dùng môi trường Python hiện có:

```powershell
.\venv\Scripts\python.exe -B -m unittest discover -s tests -p "test_pipeline.py" -v
.\venv\Scripts\python.exe -B -m unittest discover -s tests -v
```

Tests thay HTTP opener, clock/sleep và jitter bằng fake/mock. Parser, downloader,
extraction, Storage, JSON và checksum vẫn chạy thật trên fixtures trong TEMP.
Các tests gián đoạn dùng exception giả lập; không chứng minh khả năng chịu mất điện
hoặc chạy trên mọi filesystem. Không cần gọi API hoặc tạo corpus thật để chạy tests.

## Gọi từ Python

`run_selected(pairs, corpus_root, *, run_id=None, timeout=30.0, max_attempts=3,
user_agent=arxiv_client.USER_AGENT, versions=None, on_progress=None, s2_config=None)` trả dictionary summary.

Import module không gửi request hoặc ghi output. **Gọi hàm sẽ dùng HTTP client
thật nếu không có fake HTTP.** Ví dụ dưới đây chỉ dùng sau khi được duyệt pilot,
không phải lệnh kiểm thử offline:

```python
from src.pipeline import run_selected

summary = run_selected(
    [("24120232", "2502.09992")],
    "data/pilot-2502-09992-v1/corpus",
    run_id="pilot-01",
    timeout=15,
    max_attempts=1,
    versions=("v1",),
)
```

Cặp trên thuộc assigned range, chưa khẳng định ID resolve được hay có TeX.
Mỗi ID có thể có nhiều versions: trước pilot phải duyệt số request, dung lượng,
đích lưu và điều kiện dừng. Chọn corpus root mới, không dùng chung dữ liệu cũ;
giữ lại đường dẫn đó khi kiểm tra resume. V1 chưa có ngân sách request tổng quát hoặc CLI.
Không chạy ví dụ để thay thế kiểm thử offline.

`pairs` là list/tuple các cặp `(MSSV, base_arxiv_id)`, không nhận generator.
Toàn bộ input, ownership, ID trùng và HTTP options được kiểm tra trước mọi HTTP
hoặc ghi checkpoint. ID trùng bị từ chối; list rỗng không tạo output.
Storage dùng config mặc định của repository; chưa hỗ trợ config riêng cho pipeline.

## Chọn version và giới hạn pilot

`versions=None` (mặc định) giữ hành vi xử lý mọi version. Có thể truyền list/tuple
không rỗng, gồm các tên duy nhất theo dạng `v1`, `v2`...; không nhận string đơn,
tên có số 0 ở đầu, đường dẫn hoặc tên trùng. Input sai gây `ValueError` trước
HTTP hoặc ghi checkpoint.

Pipeline lọc theo tên version chính xác trước `begin_version()`, không lấy phần
tử đầu của retry. Metadata/manifest vẫn giữ toàn bộ versions discovery xác nhận.
Các version chưa được chọn vẫn pending nếu chưa xử lý; trạng thái có sẵn được
giữ theo cơ chế recovery/validation của Storage. Resume vẫn kiểm tra toàn bộ
checkpoint và source, kể cả version ngoài lựa chọn.

Discovery unresolved không được suy thành v1. Nếu một tên được chọn chưa có trong
danh sách resolved, summary báo `blocked[version]="not_discovered"` và không tải
source nào của paper đó trong lượt này. Không thêm version hoặc trạng thái mới
vào manifest để đáp ứng lựa chọn không được xác nhận.

Với đúng một paper, `versions=("v1",)`, `max_attempts=1`, lần chạy mới có tối đa
**1 discovery + 1 source = 2 HTTP requests**, tính cả attempts/retry. HTTP client
không tự theo redirect và pipeline không có vòng retry ngoài. Đây là giới hạn
của cấu hình cụ thể này; thêm paper/version hoặc tăng attempts sẽ thay đổi giới hạn.
Không cộng attempts của record cached/skipped thành request mới.

Chạy lại cùng corpus root, run ID và lựa chọn v1 hợp lệ dùng discovery cached,
skip v1 và không tải v2: **0 requests mới**. Không tự chạy lại sau lỗi để vượt
ngân sách pilot; cần duyệt riêng lần thử mạng tiếp theo. Các tests xác minh số
lần gọi fake HTTP opener, không gọi API thật. Ví dụ trên chưa phải bằng chứng
ID tồn tại, có TeX hoặc pilot đã thành công.

## Luồng xử lý và resume

1. Khởi tạo/đọc đủ ba JSON và gọi `resume()` để phục hồi checkpoint.
2. Dùng lại discovery `found`/`not_found`; không query lại chỉ vì paper còn partial.
   Discovery lỗi có thể được query lại một lần gọi hàm ở phiên sau.
3. `found` nhưng version chưa resolved được báo blocked/needs_verification.
   V1 chưa có bước xác minh bổ sung hoặc refresh tự động; không suy thành v1.
4. Với versions resolved, dùng kế hoạch skip/retry/blocked của Storage.
   Gọi `begin_version()` trước download, dùng đúng destination trả về;
   không mkdir version. `None` nghĩa version đã hoàn tất hợp lệ.
5. Chuyển nguyên result downloader cho `record_source_result()`.
   Storage kiểm tra file list, bytes và SHA-256 trước khi commit success.
   no-TeX được giữ riêng, gồm trường hợp chỉ có BibTeX hoặc không có thư mục.
6. Source hợp lệ được skip. File thiếu/checksum sai, orphan hoặc checkpoint hỏng
   được giữ để audit. Không xóa, tải đè hoặc tự công nhận `output_exists`.

Một version lỗi thông thường không ngăn các version/paper khác tiến triển nếu còn
an toàn. Kết quả HTTP 403/429 do client trả về sẽ dừng toàn bộ batch sau khi thử
lưu kết quả qua Storage; không tải version hoặc paper tiếp theo.
Mỗi version được gọi downloader tối đa một lần trong một lần chạy pipeline;
retry bên trong HTTP client vẫn theo `max_attempts`, gồm lần thử đầu.
Dùng chung pacing/lock, backoff và Retry-After của client hiện tại; không reset limiter.
Chỉ chạy một process/writer; chưa có khóa giữa nhiều process.

## Summary và lỗi

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
`status` giữ trạng thái contract của discovery/version. Không ghi skipped hoặc
blocked thành trạng thái mới trong manifest. `pending` chỉ liệt kê version có
checkpoint pending; timeout không bị đổi thành chưa chạy.
HTTP fields/attempts của record cached/skipped thuộc lần thu thập đã lưu,
không phải request mới trong phiên hiện tại.

Dừng theo chính sách HTTP trả summary bình thường với `stopped=True`, không ném
`PipelineStopped`. Bình thường `checkpoint_saved=True`: kết quả và lỗi
discovery/source được lưu trước khi dừng:
403 giữ `http_error`, 429 giữ `rate_limited`; các version chưa tải vẫn pending.
`not_found` hoặc HTTP 404 không tự dừng toàn batch, vẫn theo hành vi lỗi paper
hiện tại. Source 404 không được đổi thành `not_found` hoặc `no_tex_source`.

StorageError/IntegrityError được ghi vào summary và chặn paper/version liên quan.
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

Nguồn quy định: [contract v2](m1_contract.md), [Storage](storage.md),
[HTTP client](arxiv_client.md), [Source Downloader](arxiv_source.md).

## Progress callback

`on_progress(event, summary_copy)` nhận `paper_started` trước xử lý và
`paper_finished` sau mỗi paper, kể cả khi dừng 403/429. Snapshot là bản sao độc lập.
Lỗi callback truyền ra ngay, không chuyển sang HTTP tiếp theo. Lỗi nghiêm trọng
hoặc KeyboardInterrupt có thể không có callback kết thúc; bên gọi phải ghi trạng thái
cuối từ exception. [Batch Runner](batch.md) dùng callback để lưu tiến độ atomic.
