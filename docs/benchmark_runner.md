# Benchmark Adapter và Metrics v2

Module `src.benchmark_runner` nối Batch Runner với `benchmark_pipeline()` của
benchmark foundation. Adapter không đổi engine đo, contract v2, HTTP, cách chọn
pilot, checkpoint/resume hoặc hai callback tiến độ của Pipeline.

## API

```python
run_benchmark(
    pairs, corpus_root, *, batch_id, max_ids, log_root, report_root,
    experiment_id=None, resume=False, versions=None, timeout=30.0,
    max_attempts=1, user_agent=arxiv_client.USER_AGENT, sample_interval=0.1,
    disk_interval=30.0, disk_roots=None, s2_config=None, revision_history=None,
)
aggregate_batch(corpus_root, *, batch_id, log_root)
```

Import không chạy crawler. `run_benchmark()` gọi Batch thật nên có thể gọi mạng;
chỉ sử dụng khi pilot đã được duyệt. `aggregate_batch()` chỉ đọc log, JSON và
source để kiểm tra checksum, không khôi phục hay sửa checkpoint.

Ví dụ **chỉ đọc kết quả đã có**, không chạy lại pilot:

```python
from src.benchmark_runner import aggregate_batch

audit = aggregate_batch(
    "data/pilot/corpus", batch_id="pilot-3-v1", log_root="logs/batches",
)
print(audit["status"], audit["issues"], audit["data_coverage"])
```

Để đo một lượt đã được phép, truyền cùng các tham số của `run_batch()` vào
`run_benchmark()`, thêm `report_root="benchmark_results"` và experiment ID mới.
Resume dùng cùng batch ID/cấu hình, `resume=True`, experiment ID khác.
Ba root corpus, logs và reports phải tách biệt, không lồng nhau. Chỉ một writer;
trước khi đọc/tổng hợp phải dừng writer. Không có khóa chống process khác sửa file.

## Bốn ý nghĩa riêng

| Trường | Ý nghĩa |
| --- | --- |
| `execution_status` | `returned` hoặc `raised`: invocation có trả về bình thường không |
| `batch_status` | `finished/stopped/interrupted/failed` từ attempt mới; null nếu chưa có trạng thái cuối đáng tin |
| `data_coverage` | Độ phủ hiện tại của cohort, chỉ tính source terminal sau kiểm tra Storage/checksum |
| `publication_complete` | Số paper đáp ứng đủ contract v2, gồm metadata, references và mọi version |

`durable_batch_status` giữ nguyên snapshot log, kể cả `running` khi mất log cuối.
`stop_reason` lưu stage, HTTP status và paper/version khi dừng 403/429.

`resource_metrics.status="success"` của foundation **chỉ nói callback kết thúc
không exception**, không phải corpus success. Batch finished vẫn có thể chứa
timeout, blocked hoặc not_found. Khi có exception, adapter ghi report lỗi rồi
truyền lại exception gốc; không tự retry. Free-form message/traceback của exception
gốc không được chuyển vào report: foundation chỉ nhận tín hiệu lỗi đã lược bỏ
chi tiết. Headers, biến môi trường và remote URLs không được chép vào provenance.

## Tổng hợp attempts và kiểm chứng

Batch definition xác định cohort và cấu hình cố định. Adapter dùng bộ đọc/validator
log của Batch, kiểm tra thêm cấu trúc kết quả và thời gian. Thiếu log, JSON lỗi,
sequence đứt đoạn hoặc checkpoint không nhất quán được ghi trong `issues`.
Khi log không đủ tin cậy, coverage/history để null thay vì tạo kết quả complete.

Mỗi paper được đọc bằng các API Storage. Adapter đối chiếu metadata, references
và manifest qua `_sync()` trên bản sao trong RAM, không gọi `resume()` hoặc commit.
Version terminal được kiểm bằng `should_skip_version()`, gồm file list, SHA-256,
bytes và trạng thái TeX. Pending/error có thư mục source tồn tại bị báo cần audit.
Paper chưa tới lượt và chưa có thư mục được ghi là not_created, khác checkpoint
bị thiếu sau khi đã có bằng chứng ghi dữ liệu. Checkpoint lỗi được giữ nguyên. Đây là tích hợp với helper nội bộ hiện tại;
khi đổi schema/helper phải chạy lại regression tests.

Đếm final coverage theo (MSSV, base ID, version), không cộng snapshots:
attempt cũ timeout vẫn còn trong `error_history`; attempt sau success làm final
version thành success đúng một lần. `operations` đếm action trong snapshot paper
đã xử lý xong: downloaded, skipped và cached tách riêng. Đây không phải số HTTP
requests và không khôi phục được action chưa ghi log khi process bị kill.

Chỉ cohort trong batch definition được tính. `verified_source_bytes/files`
là tổng source hợp lệ hiện tại của cohort, gồm các version đã lưu từ trước nếu có,
mỗi version một lần. `final_archive_payload_bytes` chỉ cộng kích thước payload
cuối của version terminal đã kiểm; có số version thiếu phép đo kèm theo.
Không dùng các trường này làm tổng lưu lượng mạng hoặc disk peak.

`selected_sources_terminal` kiểm tra đúng versions đã chọn; no_tex hợp lệ là
terminal nhưng không tăng `valid_tex_versions`. Not_found/unresolved không phải
source success. `all_version_sources_terminal` kiểm tra mọi version đã biết.
`publication_complete.all_complete` chỉ true khi mọi paper đủ điều kiện contract
và không có lỗi bằng chứng. References chưa complete có `reference_items=null`;
chỉ tập đã được xác nhận đầy đủ mới có thể là 0.

## Schema report và provenance

```text
benchmark_results/<experiment_id>/
  intent.json       # cấu hình/provenance trước khi gọi Batch
  resources.json    # output benchmark foundation cho invocation này
  report.json       # report adapter và aggregate tại thời điểm kết thúc
```

Experiment directory được tạo độc quyền; nếu đã tồn tại thì từ chối trước khi
gọi Batch. Không ghi đè experiment cũ. Intent còn lại khi process bị kill là bằng
chứng chưa có report cuối; không coi là phép đo hoàn tất.

Report schema version 1 gồm:
- Experiment ID, batch ID, attempt IDs và run IDs của invocation hiện tại.
- `previous_attempt_ids`, selection, SHA-256 fingerprint, versions, timeout,
  max_attempts, thời điểm bắt đầu/kết thúc, corpus/log/report/resources paths.
- `environment`: Git commit và dirty state nếu đọc được, Python, OS, CPU count.
  Loại môi trường Colab/hardware chưa tự xác minh: `measurement_environment=not_collected`.
- Bốn nhóm trạng thái trên, trạng thái log bền vững, nguyên nhân dừng.
- Resource metrics của invocation, phạm vi/đơn vị, aggregate gồm lịch sử attempts,
  lỗi, operations, thời gian, dữ liệu và issues.
- `unavailable_metrics` chứa null cho các phép đo chưa triển khai.

Git provenance là code **hiện tại**, không gán ngược cho các attempts cũ.
Durable logs cũ chưa có environment/resource measurements. Lượt đo v2 lưu chúng
trong measurements sidecar; aggregate đọc từng sidecar theo attempt ID. Lượt cũ
không có sidecar giữ null/missing, không được điền ngược. Giữ các report experiment
riêng để truy xuất provenance của từng invocation.

## Benchmark Metrics v2: công thức và phạm vi

Các yêu cầu về thời gian, RAM và disk được đối chiếu với
[đề M1 chính thức](https://hcmus-cs-ta.github.io/lab-book/intro-ds/statement/crawling/).
Tool đo là lựa chọn kỹ thuật; môi trường benchmark cuối theo đề là Colab CPU-only.

| Chỉ số | Định nghĩa trong code |
| --- | --- |
| Wall time end-to-end | `metrics_v2.wall_seconds`: perf_counter trước/sau toàn callback Batch; gồm setup Batch, checkpoint, pacing, retry và log cuối của Batch |
| Trung bình mỗi paper | `timing_metrics.average_paper_seconds`: tổng interval paper đã ghi qua các attempts / số ID duy nhất trong cohort |
| Entry Discovery | `timing_metrics.discovery_wall_seconds`: tổng thời gian quanh `query_arxiv()`, gồm chờ limiter, retry và parsing; không gồm cache hoặc ghi Storage |
| RAM peak | `ram.sampled_peak_bytes`: RSS lớn nhất trong các mẫu hợp lệ |
| RAM average | `ram.average_bytes = sum_sample_bytes / sample_count`; trung bình số học các quan sát, không phải trung bình theo thời gian |
| Disk peak | `disk.sampled_peak_bytes`: tổng logical bytes các thư mục đo tại mỗi lần quét, lấy giá trị lớn nhất |
| Final corpus size | `disk.final_corpus_bytes`: dung lượng file của riêng corpus tại lần quét cuối |

Wall time không gồm khởi động Python, setup/final probes của sampler, serialize
benchmark report và audit sau crawl. RAM endpoint probes nằm sát hai đầu callback;
probe RAM cuối không chờ disk scan dài. Thời gian paper nằm giữa hai callback
hiện có, không gồm toàn bộ overhead cấp Batch. Mẫu số là **ID ứng viên được chọn**,
gồm cả not_found/failed và công việc lặp do resume, không phải số paper thành công.
Nếu còn ID chưa xử lý hoặc mất interval do gián đoạn, average để null; subtotal
đã biết và số interval thiếu vẫn được ghi.

Discovery timing được thêm trực tiếp vào summary của Pipeline:
`discovery.wall_seconds` và `timing_status=measured|cached|not_attempted`.
Cached có 0 giây gọi API và không tăng số queried. Không thêm observer, không thêm
sự kiện mới vào `on_progress`; Batch tiếp tục nhận đúng paper_started/paper_finished.
Log cũ thiếu timing không được suy ra thời gian Discovery từ thời gian paper.

### Lấy mẫu resource

`sample_interval` mặc định 0.1 giây cho RAM, tối thiểu hiệu dụng 0.01 giây.
`disk_interval` mặc định 30 giây, tối thiểu hiệu dụng 1 giây; disk dùng thread
riêng để không quét corpus ở mỗi RAM tick. Có mẫu ở endpoints. Chu kỳ thực tế có
thể dài hơn do chi phí đọc. Sampler lưu thống kê tổng hợp, không giữ chuỗi mẫu
tăng vô hạn trong RAM.

Mỗi chuỗi có status, reason/failure_reasons, sample_count, failed_sample_count,
đơn vị bytes và thời điểm mẫu đầu/cuối theo giây tương đối. Không có mẫu hợp lệ:
`not_collected`, các giá trị đo là null. Một số probe lỗi: `partial`; trung bình
nếu có chỉ mô tả những mẫu hợp lệ còn lại. Không dùng 0 thay cho lỗi phép đo.

Khi có psutil, RAM gồm process Python hiện tại và các descendant quan sát được,
cộng RSS; có thể tính trùng shared pages giữa process. Child đã thoát trước khi
đọc không còn được cộng; access denied làm probe lỗi, không âm thầm bỏ process.
Backend/version/scope được ghi rõ. Khi thiếu psutil trên Linux, fallback
`/proc/self/statm` chỉ đo process hiện tại; không được gọi đó là RAM toàn hệ thống
nếu có subprocess. Windows thiếu psutil ghi backend_missing/not_collected.
Không dùng ru_maxrss lịch sử để tính average RSS.

Peak RAM/disk là **sampled peak**, có thể bỏ sót spike hoặc file tạm sống ngắn.
Disk đo logical bytes của regular files, không phải allocated blocks hay toàn
bộ dung lượng filesystem. Scan thiếu/lỗi không được thay bằng 0.

### Corpus, scratch và Colab

`disk_roots=None` chỉ đo corpus. Có thể truyền list/tuple gồm corpus và các thư mục
scratch riêng, không trùng/lồng nhau. Final corpus size vẫn tách khỏi tổng scratch.

Downloader hiện dùng TEMP của process để chứa archive; thư mục extraction nằm
dưới corpus. Vì vậy, **mặc định chưa bao gồm archive ngoài corpus**. Muốn đo phạm vi
đó trong benchmark thật, chuẩn bị scratch riêng và cấu hình TEMP/TMP trên Windows
hoặc TMPDIR trên Linux **trước khi khởi động Python**, rồi thêm scratch vào
disk_roots. Không thay TEMP toàn máy, không trỏ vào TEMP dùng chung, không quét
toàn bộ /tmp. Sampler không tự tạo/xóa dữ liệu hoặc đổi môi trường.

Code dùng Python/threading/pathlib và psutil tùy chọn, chạy được trên Windows;
Linux có fallback nêu trên. Chưa chạy Colab và chưa xác minh CPU-only runtime.
Benchmark cuối vẫn cần chạy trên Colab, chọn phạm vi disk đầy đủ và ghi môi trường
thật. Không có notebook Colab hoặc crawler thật được chạy trong lượt triển khai.

### Lưu và tổng hợp nhiều attempts

Ngoài các file experiment, lượt đo mới ghi:
`<log_root>/<batch_id>/measurements/<attempt_id>.json`.
File này chứa schema_version=2, batch/run/attempt IDs, selection fingerprint,
resources, environment và report_path. Ghi atomic, không ghi đè file đã tồn tại;
không sửa attempt log cũ hoặc Storage JSON. Lỗi lưu measurement được ghi riêng
trong report, không biến số liệu thiếu thành collected.

Report adapter giữ schema v1 với các trường bổ sung metrics_version=2:
- `resource_metrics.metrics_v2`: phép đo invocation hiện tại.
- `benchmark_metrics`: tổng hợp resources đã lưu của tất cả attempts thuộc batch.
- `timing_metrics`: thời gian paper/Discovery tổng hợp từ durable logs.
- `history.attempts[].timing_metrics`: thời gian từng attempt.
- `measurement_configuration`: interval và roots thực tế.
- `measurement_issue` và `unavailable_metrics`: thông tin chưa có hoặc không lưu được.

Công thức tổng hợp:
- Wall time: cộng từng interval callback đã đo đúng một lần; không cộng lịch
  end-start, khoảng nghỉ giữa phiên hoặc cộng lại logged Batch time.
- RAM average: tổng sum_sample_bytes / tổng sample_count; không lấy trung bình
  các average của attempts có số mẫu khác nhau. Chỉ hợp nhất khi backend/scope
  tương thích và samples đầy đủ; nếu không, aggregate để null và giữ từng attempt.
- Peak RAM/disk: max của các sampled peaks cùng phạm vi, không cộng peaks.
- Final size: chỉ snapshot của attempt cuối đã đo, không cộng snapshots và không
  thế snapshot cũ nếu attempt cuối thiếu. Đây là size tại thời điểm đo, không phải
  một phép đo mới tại thời điểm aggregate.
- Paper/version coverage vẫn do Storage/checksum xác nhận; không lấy số attempts
  làm số paper hoặc cached/skipped làm download mới.

Legacy logs/reports vẫn đọc được. Attempt cũ không có measurement giữ missing,
full aggregate để null, có known subtotal riêng. Không gán commit hay môi trường
mới cho lượt cũ. Sidecar hỏng hoặc sai identity/fingerprint được liệt kê trong
invalid_attempt_ids, giữ nguyên để audit. Mất report do hard kill không được suy
ra là zero resource usage.

HTTP requests/errors và tổng download traffic vẫn not_collected; chưa thay HTTP
client để instrument từng attempt. `publication_complete` tiếp tục tuân thủ
contract v2, không đổi vì benchmark hoặc batch kết thúc.

## Kiểm thử offline

```powershell
.\venv\Scripts\python.exe -B -m unittest discover -s tests -p "test_benchmark*.py" -v
.\venv\Scripts\python.exe -B -m unittest discover -s tests -v
```

Fixtures dùng corpus/logs TEMP, fake HTTP/Pipeline và mock engine đo; có ca gọi
foundation thật trên fixture nhỏ để kiểm tra semantics và lược bỏ exception.
Đây là kiểm thử phần mềm, không phải số liệu benchmark crawler thực tế.

## S2 opt-in

`run_benchmark(..., s2_config={...})` truyền cùng cấu hình qua Batch/Pipeline và
lưu cấu hình không có key trong report/run definition. Mặc định None giữ logs
arXiv-only; đổi cấu hình cần batch_id mới. Xem [S2 integration](s2_integration.md).
Wall time/resource sampling bao gồm S2; Discovery timing vẫn chỉ thuộc arXiv.
History nhận diện stage s2_metadata/s2_references, nhưng không suy HTTP counts
từ attempts hoặc số items. References chưa complete vẫn có count unknown.

## HTTP metrics bổ sung, tương thích báo cáo cũ

Aggregation đọc http_metrics của từng attempt đã được kiểm tra ID/sequence.
history.http_requests/http_errors là tổng các attempt khác nhau, chỉ có giá trị
khi toàn bộ lịch sử có metrics đầy đủ/hợp lệ. Cached/skipped không tạo số request.

Nếu lịch sử có attempt cũ chưa instrument: tổng vẫn null, http_metrics_status
là partial; http_metrics.known/providers chỉ mô tả phần đã đo. missing_attempt_ids,
invalid_attempt_ids và partial_attempt_ids nêu phần thiếu. Toàn bộ legacy logs
giữ not_collected. Metrics hỏng được đánh dấu invalid, không in payload lỗi.

status_counts giữ cả 429/403/5xx trung gian dù retry cuối thành công.
retry_attempts chỉ tăng khi retry thực sự gọi transport, không tăng khi chỉ định
retry nhưng hết ngân sách. wait_seconds không cộng khoảng nghỉ giữa các sessions.
Hard kill có thể mất counters chưa flush; không gọi lower bound là tổng chính xác.

Các trường RAM/disk/timing của Metrics v2 và điều kiện publication_complete không
thay đổi. Audit Storage/checksum vẫn độc lập với HTTP metrics. Đổi cấu hình để dùng
batch ID mới với cùng corpus sẽ tái sử dụng dữ liệu, nhưng metrics/thời gian của
batch mới chỉ đo phần chạy mới; không tự gộp các cấu hình khác nhau thành một
benchmark hoàn chỉnh. Không cộng lại dung lượng snapshot corpus qua nhiều attempts.


## Phạm vi đo revision history

`revision_history` được chuyển vào Batch, bật mặc định với batch mới và kế thừa
cấu hình khi resume; batch legacy thiếu field vẫn tắt. Không nâng cấp log cũ.
Config true được giữ trong intent/report. Muốn đổi chế độ cần batch ID mới.

Callback benchmark bao trọn bước history trong Pipeline: wall time tổng và từng
paper, RAM sampling, disk sampling và HTTP counts hiện có đều bao gồm nó.
Không đổi công thức RAM/disk hoặc foundation. `discovery_wall_seconds` vẫn đo
riêng `query_arxiv()` cho Entry Discovery; không gộp history thành số discovery
API calls. Thời gian history có trong result.revision_history.wall_seconds của
mỗi paper/attempt, không có phép đo hồi tố cho logs cũ. Chỉ gọi run_benchmark()
mới có resource sampling; run_batch() đơn thuần không tạo số đo RAM/disk.
