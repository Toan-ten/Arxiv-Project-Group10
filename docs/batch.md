# Batch Runner v1

Module `src.batch` bọc Pipeline hiện có cho một danh sách ID tuần tự.
Import không chạy crawler. Kiểm thử dùng fake HTTP; gọi API dưới đây ngoài tests
sẽ dùng mạng thật và cần một pilot đã được duyệt.

## API và đầu vào

```python
run_batch(
    pairs, corpus_root, *,
    batch_id, max_ids, log_root, resume=False,
    versions=None, timeout=30.0, max_attempts=1,
    user_agent=arxiv_client.USER_AGENT, s2_config=None,
)
```

`pairs` là list/tuple các cặp `(MSSV, base_arxiv_id)`. Toàn bộ danh sách,
kể cả phần vượt giới hạn, được kiểm tra ownership, định dạng và trùng ID trước
HTTP hoặc ghi output. Chỉ N ID đầu được chọn khi `max_ids=N`; không bổ sung ID
thay thế cho not_found. Giới hạn này là số ID, không phải request budget tổng quát.

`pilot_pairs(entries)` chuyển list/tuple `PilotEntry` của Đạt thành các cặp
đầu vào, giữ nguyên thứ tự. Không viết lại thuật toán chọn Pilot 10/500.

`versions`, timeout và attempts được chuyển vào Pipeline. Giữ nguyên limiter
dùng chung, retry hữu hạn, version selection và chính sách 403/429. Mặc định của
Batch Runner là `max_attempts=1`; Pipeline gọi trực tiếp vẫn giữ mặc định cũ.

`s2_config=None` tắt S2; dictionary bật và được lưu trong run definition.
Đổi cấu hình hoặc bật S2 trên batch cũ cần batch_id mới. Logs arXiv-only vẫn đọc
được. Xem [S2 integration](s2_integration.md); giữ hai progress callbacks hiện có.

## Log bền vững

`log_root` và `corpus_root` phải là hai thư mục tách biệt, không lồng nhau.
Batch ID chỉ chứa chữ/số, gạch nối hoặc gạch dưới; không được dùng tên thiết bị
Windows. Ví dụ cấu trúc, không phải kết quả crawl:

```text
logs/batches/<batch_id>/
  batch.json
  attempts/
    <attempt_id>.json
data/<corpus>/
  <paper-folder>/
    metadata.json
    references.json
    manifest.json
    v1/...
```

`batch.json` giữ cố định toàn bộ input, selection theo thứ tự, corpus root đã
chuẩn hóa, max_ids, versions, timeout, attempts và User-Agent.

Mỗi invocation có UUID attempt riêng, số thứ tự và run ID riêng. JSON attempt gồm:

- Batch ID, attempt ID, run ID, thời điểm bắt đầu/kết thúc và wall_seconds.
- status: running, finished, stopped, interrupted hoặc failed.
- Danh sách papers theo đúng selection; mỗi paper có state, thời điểm, wall_seconds
  và result nguyên cấu trúc summary của Pipeline.
- Result giữ rõ queried/cached, downloaded/skipped/failed/blocked, not_found,
  trạng thái source thực tế, pending versions, references và errors.
- stopped, stop_reason và HTTP status khi Pipeline dừng theo chính sách.
- previous_incomplete_attempts: những attempt cũ còn running, có thể đã bị ngắt.
  Không sửa lại lịch sử cũ hoặc gọi chúng là finished.
- Stats đếm số paper đã đi qua Pipeline, thao tác discovery/source và các trạng
  thái quan sát được. Không coi source action downloaded là publication complete;
  trạng thái no_tex_source vẫn được giữ riêng trong result.

Log intent được ghi trước HTTP. Callback `on_progress(event, summary_copy)`
của Pipeline ghi snapshot trước/sau mỗi paper, trước khi paper tiếp theo bắt đầu.
Trong một paper, Storage tiếp tục checkpoint từng version theo contract v2.

JSON dùng writer atomic hiện có của Storage: file tạm cùng thư mục, flush/fsync,
rồi replace. Không ghi logs vào source. Lỗi ghi bắt buộc phát sinh
`BatchLogError` và dừng ngay; không retry crawler hoặc âm thầm bỏ log.
Exception tổng quát chỉ lưu loại lỗi ở cấp attempt, không lưu traceback/credentials.

## Resume và gián đoạn

Chạy lại cùng batch cần `resume=True`, cùng input, thứ tự, giới hạn, cấu hình
và corpus root. Thay đổi các giá trị này bị từ chối trước HTTP và ghi log;
hãy tạo batch ID mới cho một phép chạy khác.

Mỗi attempt vẫn chuyển toàn bộ selection tới Pipeline. Pipeline/Storage kiểm tra
checkpoint và checksum, dùng discovery cache hợp lệ, bỏ qua source tốt, tiếp tục
phần pending và chặn dữ liệu hỏng/orphan. Không dùng log success để bỏ qua kiểm tra.
Lịch sử attempt cũ giữ nguyên bytes khi resume.

KeyboardInterrupt/SystemExit được ghi interrupted rồi truyền lại; lỗi nghiêm trọng
được ghi failed và truyền lại. Nếu process bị kill trước khi ghi trạng thái cuối,
snapshot còn running; lần resume ghi nhận nó trong previous_incomplete_attempts.
Không có kết luận rằng thao tác chưa được checkpoint đã thành công.

403/429 trả về từ Pipeline được ghi stopped, giữ nguyên stop_reason, không chuyển
paper/version kế tiếp và không tự gọi lại. Chỉ invocation mới với `resume=True`
thể hiện quyết định chạy lại của người vận hành. Với max_attempts > 1, 429 trung gian
có thể bị Client retry rồi thành công; Pipeline không quan sát được attempt đó.

**Chỉ một process/writer tại một thời điểm**, kể cả giữa các batch dùng chung
corpus. Trước resume phải bảo đảm process cũ đã dừng. V1 chưa có khóa liên process.

## Số liệu và giới hạn

wall_seconds đo bằng perf_counter thực tế: attempt tính từ khởi tạo attempt đến
trước lần ghi snapshot gần nhất; paper tính giữa hai callback. Chưa bao gồm toàn bộ
setup/ghi snapshot cuối, nên không thay thế benchmark M1 end-to-end.

HTTP requests và HTTP errors có giá trị null với http_metrics_status=not_collected.
Không cộng attempts của cache hoặc source skipped thành requests mới. Chưa thu
RAM/disk peak, số bytes/files hoặc toàn bộ metrics M1 trong module này.
Các lần thử giả lập trong tests không phải benchmark crawler thật.

finished chỉ nghĩa Pipeline đã xử lý hết danh sách, có thể gồm failed/blocked
hoặc not_found. Không đồng nghĩa corpus/publication complete; khi S2 tắt,
references mới vẫn not_attempted. Benchmark foundation được adapter dùng lại; xem
[Benchmark Adapter v1](benchmark_runner.md) để phân biệt execution, batch và độ phủ dữ liệu.

## Kiểm thử offline trên PowerShell

```powershell
.\venv\Scripts\python.exe -B -m unittest discover -s tests -p test_batch.py -v
.\venv\Scripts\python.exe -B -m unittest discover -s tests -p test_pipeline.py -v
.\venv\Scripts\python.exe -B -m unittest discover -s tests -v
```

Tests chạy parser, extraction, Storage và atomic writer thật trên fixtures TEMP,
fake HTTP/clock; mô phỏng exception, interruption và lỗi replace. Trạng thái còn
running sau kill được mô phỏng bằng fixture; chưa kiểm chứng mất điện hoặc mọi
filesystem, không tự chạy integration có mạng.

## Lệnh pilot đề xuất — chỉ chạy sau review và cho phép mạng

Lệnh sau chọn 3 ID đầu của Pilot 10, v1 đã xác minh, tối đa 6 HTTP requests với
max_attempts=1: mỗi paper tối đa 1 discovery và 1 source. Có thể ít hơn nếu cache,
not_found, unresolved hoặc dừng 403/429. Chưa thực thi lệnh này trong lần triển khai.

```powershell
@'
from src.batch import pilot_pairs, run_batch
from src.id_ranges import load_config
from src.pilots import build_smoke_set

result = run_batch(
    pilot_pairs(build_smoke_set(list(load_config().values()))),
    "data/pilot-batch-3-v1/corpus",
    batch_id="pilot-batch-3-v1",
    max_ids=3,
    log_root="logs/batches",
    versions=("v1",),
    timeout=15,
    max_attempts=1,
)
print(result["status"], result["stats"], result["stop_reason"])
'@ | .\venv\Scripts\python.exe -B -
```

Dùng đích mới cho pilot. Chỉ thêm `resume=True` ở một lần chạy lại được chủ động
cho phép sau khi kiểm tra log và bảo đảm process cũ dừng. Không tự chạy Pilot 10/500.

## HTTP metrics và cấu hình S2 retry

Mỗi run_batch() tạo một HttpMetrics collector riêng cho attempt mới. Client ghi
số lần thực sự gọi HTTP opener, không suy từ source_downloaded/cached/skipped.
Các request arXiv và S2 được ghi riêng, không đổi pacing/retry arXiv.

Attempt JSON thêm trường tùy chọn http_metrics (schema_version=1):

- providers.arxiv và providers.semantic_scholar: attempts, completed_attempts,
  retry_attempts, http_errors, without_http_response, status_counts, wait_seconds.
- total: tổng hai providers.
- status: collected khi attempt đã kết thúc và mọi transport call đã đóng;
  partial khi running hoặc bị ngắt trong transport.

http_errors đếm response >=400; timeout/network không có HTTP status được tách
vào without_http_response. HTTP 200 với JSON lỗi không phải HTTP error.
Đếm transport attempts không chứng minh server đã nhận request.
wait_seconds là sleep thực đo bằng monotonic; phần pacing/backoff chồng nhau chỉ
đếm một lần. Không gồm thời gian I/O, lock contention, khoảng nghỉ giữa sessions.
Không ghi URL/body/headers/key hoặc exception text.

Metrics được flush cùng các log progress/final hiện có; không thêm stage event
vào on_progress. Hard kill có thể làm mất phần chưa flush, vì vậy log running
không được coi là metrics đầy đủ. Không khôi phục số request từ checkpoint.

Legacy definitions giữ nguyên: ba policy keys max_retry_wait, backoff_base,
backoff_cap chỉ được thêm vào fingerprint nếu caller chỉ định. Batch cũ vẫn có
thể Resume với đúng cấu hình max_attempts=1; attempt mới có metrics, log cũ
không bị sửa hoặc điền ngược số liệu.

Để đổi S2 max_attempts từ 1 sang 3, không sửa batch.json và không bỏ fingerprint:
dùng batch_id mới, cùng corpus_root, cùng danh sách input và log_root. Đặt
resume=False cho lần đầu của batch mới, sau đó resume=True với đúng cấu hình mới.
Các source/metadata/references hợp lệ được đọc lại và skip qua Storage; không copy,
xóa hoặc tải đè source. Manifest run_id có thể cập nhật theo lần chạy mới.

Cấu hình S2 mới đề xuất:
dict(min_interval=2, max_attempts=3, max_retry_wait=60, backoff_base=5,
     backoff_cap=30, timeout=30, reference_limit=100, max_pages=3).
Giữ arXiv max_attempts=1 để chính sách dừng tại 403/429 không đổi.

Chỉ một process/writer được dùng corpus. Kiểm tra process cũ đã dừng và review
cooldown trước khi tiếp tục sau 403/429. Batch API không tự lặp lại attempt.
Không chạy lệnh live trong lượt triển khai/kiểm thử offline này.
