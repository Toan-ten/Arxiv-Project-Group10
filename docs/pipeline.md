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
user_agent=arxiv_client.USER_AGENT)` trả dictionary summary.

Import module không gửi request hoặc ghi output. **Gọi hàm sẽ dùng HTTP client
thật nếu không có fake HTTP.** Ví dụ dưới đây chỉ dùng sau khi được duyệt pilot,
không phải lệnh kiểm thử offline:

```python
from src.pipeline import run_selected

summary = run_selected(
    [("24120232", "2502.08235")],
    "data/pipeline-pilot-01/corpus",
    run_id="pilot-01",
    timeout=15,
    max_attempts=1,
)
```

Cặp trên thuộc assigned range, chưa khẳng định ID resolve được hay có TeX.
Mỗi ID có thể có nhiều versions: trước pilot phải duyệt số request, dung lượng,
đích lưu và điều kiện dừng. V1 chưa có ngân sách request/version hoặc CLI.
Không chạy ví dụ để thay thế kiểm thử offline.

`pairs` là list/tuple các cặp `(MSSV, base_arxiv_id)`, không nhận generator.
Toàn bộ input, ownership, ID trùng và HTTP options được kiểm tra trước mọi HTTP
hoặc ghi checkpoint. ID trùng bị từ chối; list rỗng không tạo output.
Storage dùng config mặc định của repository; chưa hỗ trợ config riêng cho pipeline.

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

Một version lỗi không ngăn các version/paper khác tiến triển nếu còn an toàn.
Mỗi version được gọi downloader tối đa một lần trong một lần chạy pipeline;
retry bên trong HTTP client vẫn theo `max_attempts`, gồm lần thử đầu.
Dùng chung pacing/lock, backoff và Retry-After của client hiện tại; không reset limiter.
Chỉ chạy một process/writer; chưa có khóa giữa nhiều process.

## Summary và lỗi

Top-level có `run_id`, `papers`, `stopped`. Mỗi paper có:

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

StorageError/IntegrityError được ghi vào summary và chặn paper/version liên quan.
Lỗi I/O khi ghi JSON, downloader trả storage_error hoặc exception bất thường
dừng run bằng `PipelineStopped`; đọc `exception.summary` để xem phần đã xử lý.
KeyboardInterrupt được truyền ra, giữ checkpoint đã commit để lần sau resume.
`stopped=False` chỉ nghĩa không có lỗi nghiêm trọng dừng run, không phải mọi paper thành công.

Paper mới vẫn partial khi chỉ thu xong source: references giữ not_attempted,
items rỗng và coverage chưa complete. Không gọi Semantic Scholar hoặc tạo references.
Metadata/references đã có được giữ nguyên; paper complete từ trước có thể được skip.

V1 chưa có run manifest/log lịch sử HTTP attempts bền vững, request budget,
Semantic Scholar, đầy đủ ngày revision, CLI hoặc benchmark M1.
Summary được trả trong bộ nhớ; tiến độ bền vững nằm trong checkpoint từng paper.
Benchmark foundation không đọc summary để xác định corpus success; không dùng
dummy benchmark hoặc fixtures làm kết quả corpus thật.

Nguồn quy định: [contract v2](m1_contract.md), [Storage](storage.md),
[HTTP client](arxiv_client.md), [Source Downloader](arxiv_source.md).
