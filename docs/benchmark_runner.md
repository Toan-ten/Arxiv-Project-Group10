# Benchmark Adapter v1

Module `src.benchmark_runner` nối Batch Runner với `benchmark_pipeline()` của
benchmark foundation. Adapter không đổi engine đo, contract v2, HTTP, cách chọn
pilot, checkpoint/resume hoặc hai callback tiến độ của Pipeline.

## API

```python
run_benchmark(
    pairs, corpus_root, *, batch_id, max_ids, log_root, report_root,
    experiment_id=None, resume=False, versions=None, timeout=30.0,
    max_attempts=1, user_agent=arxiv_client.USER_AGENT, sample_interval=0.1,
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
Trong aggregate, environment/resource measurements của từng attempt lịch sử là
null vì durable logs cũ chưa thu thông tin đó. Giữ các report experiment riêng
để truy xuất phép đo/provenance của từng invocation mới.

## Phạm vi đo và giới hạn

- Foundation đo wall time của invocation Batch, có pacing/retry; không gồm kiểm
  checksum/tổng hợp sau đo của adapter. RAM peak là RSS lấy mẫu theo backend
  foundation; MB trong tên trường thực chất là MiB. Backend unavailable được
  trình bày null trong report adapter; resources.json vẫn giữ output foundation.
- `logged_attempt_wall_seconds` cộng các khoảng perf_counter đã ghi trong logs,
  không lấy end-start lịch để cộng khoảng nghỉ giữa sessions. Snapshot running
  chỉ cung cấp lower bound. Đây không phải wall time end-to-end đã đo ngược cho
  các attempts cũ và không được cộng thêm resource elapsed vào cùng tổng.
- Thời gian mỗi paper là tổng các interval đã có; `missing_intervals` chỉ rõ
  khoảng chưa có phép đo. Chưa có thời gian discovery riêng.
- Resource corpus size của foundation là snapshot toàn root, có thể chứa paper
  ngoài cohort và là phép đọc best-effort. Số source bytes được kiểm chứng của
  cohort nằm riêng trong coverage. Không cộng corpus size giữa attempts.
- HTTP requests/errors, tổng download traffic, RAM average, disk peak và môi
  trường Colab CPU-only chưa được instrument/xác minh. Chưa có Semantic Scholar.
- Logs không phải nhật ký chống sửa/xóa: phát hiện được sequence thiếu, file hỏng
  và run ID checkpoint thuộc batch nhưng mất log; không chứng minh được lịch sử
  chưa từng bị xóa ở cuối nếu không còn dấu vết. Không coi thống kê logs là
  chứng nhận đầy đủ của mọi HTTP attempt.

## Kiểm thử offline

```powershell
.\venv\Scripts\python.exe -B -m unittest discover -s tests -p "test_benchmark*.py" -v
.\venv\Scripts\python.exe -B -m unittest discover -s tests -v
```

Fixtures dùng corpus/logs TEMP, fake HTTP/Pipeline và mock engine đo; có ca gọi
foundation thật trên fixture nhỏ để kiểm tra semantics và lược bỏ exception.
Đây là kiểm thử phần mềm, không phải số liệu benchmark crawler thực tế.
