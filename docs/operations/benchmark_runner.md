# Bộ đo hiệu năng và các chỉ số v2

Module `src.benchmark_runner` nối bộ điều phối lô với `benchmark_pipeline()` của
lớp đo nền. Adapter không đổi engine đo, contract v2, HTTP, cách chọn
pilot, checkpoint/resume hoặc hai callback tiến độ của Pipeline.

## Giao diện gọi

```python
run_benchmark(
    pairs, corpus_root, *, batch_id, max_ids, log_root, report_root,
    experiment_id=None, resume=False, versions=None, timeout=30.0,
    max_attempts=1, user_agent=arxiv_client.USER_AGENT, sample_interval=0.1,
    disk_interval=30.0, disk_roots=None, s2_config=None, revision_history=None,
)
aggregate_batch(corpus_root, *, batch_id, log_root, bbl_artifact_root=None)
```

Import không chạy crawler. `run_benchmark()` gọi Batch thật nên có thể gọi mạng;
chỉ sử dụng khi pilot đã được duyệt. `aggregate_batch()` chỉ đọc log, JSON và
source để kiểm tra checksum, không khôi phục hay sửa checkpoint.

Ví dụ **chỉ đọc kết quả đã có**, không chạy lại pilot:

```python
from src.benchmark_runner import aggregate_batch

audit = aggregate_batch(
    "data/pilot/corpus", batch_id="pilot-3-v1", log_root="logs/batches",
    bbl_artifact_root="data/pilot/reports/derived_bibliography/bbl-exception-v1",
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
| `publication_complete` | Số paper đáp ứng đủ contract v2, gồm metadata, references và mọi lần nộp |

`durable_batch_status` giữ nguyên snapshot log, kể cả `running` khi mất log cuối.
`stop_reason` lưu giai đoạn, mã HTTP, bài và lần nộp khi dừng 403/429.

`resource_metrics.status="success"` của foundation **chỉ nói callback kết thúc
không exception**, không phải corpus success. Batch finished vẫn có thể chứa
timeout, blocked hoặc not_found. Khi có exception, adapter ghi report lỗi rồi
truyền lại exception gốc; không tự retry. Free-form message/traceback của exception
gốc không được chuyển vào report: foundation chỉ nhận tín hiệu lỗi đã lược bỏ
chi tiết. Headers, biến môi trường và remote URLs không được chép vào provenance.

## Tổng hợp các lượt chạy và kiểm chứng

Batch definition xác định cohort và cấu hình cố định. Adapter dùng bộ đọc/validator
log của Batch, kiểm tra thêm cấu trúc kết quả và thời gian. Thiếu log, JSON lỗi,
sequence đứt đoạn hoặc checkpoint không nhất quán được ghi trong `issues`.
Khi log không đủ tin cậy, coverage/history để null thay vì tạo kết quả complete.

Mỗi paper được đọc bằng các API Storage. Adapter đối chiếu metadata, references
và manifest qua `_sync()` trên bản sao trong RAM, không gọi `resume()` hoặc commit.
Lần nộp có trạng thái kết thúc được kiểm bằng `should_skip_version()`, gồm danh sách tệp, SHA-256,
bytes, trạng thái TeX và chỉ mục/bằng chứng `.bbl` ngoài corpus. Truyền
`bbl_artifact_root` trỏ tới `reports/derived_bibliography/bbl-exception-v1` khi
đọc corpus có chính sách `.bbl`. Nếu thiếu root cho checkpoint cần artifact,
paper được ghi `checkpoint_status=unverified`, tăng
`unverified_checkpoints`, và tạo issue `bbl_artifact_root_unconfigured`; nó không
bị tính nhầm là hỏng hoặc được tính là đã xác minh. Khi root được cấu hình nhưng
artifact thiếu/sai checksum, checkpoint vẫn là `invalid`. Pending/error có thư
mục source tồn tại bị báo cần audit.
Paper chưa tới lượt và chưa có thư mục được ghi là not_created, khác checkpoint
bị thiếu sau khi đã có bằng chứng ghi dữ liệu. Checkpoint lỗi được giữ nguyên. Đây là tích hợp với helper nội bộ hiện tại;
khi đổi schema/helper phải chạy lại regression tests.

Đếm độ bao phủ cuối theo (MSSV, ID gốc, lần nộp), không cộng các bản chụp:
attempt cũ timeout vẫn còn trong `error_history`; attempt sau success làm final
lần nộp thành success đúng một lần. `operations` đếm action trong snapshot paper
đã xử lý xong: downloaded, skipped và cached tách riêng. Đây không phải số HTTP
requests và không khôi phục được action chưa ghi log khi process bị kill.

Chỉ cohort trong batch definition được tính. `verified_source_bytes/files`
là tổng source hợp lệ hiện tại của cohort, gồm các lần nộp đã lưu từ trước nếu có,
mỗi lần nộp một lần. `final_archive_payload_bytes` chỉ cộng kích thước payload
cuối của lần nộp terminal đã kiểm; có số lần nộp thiếu phép đo kèm theo.
Không dùng các trường này làm tổng lưu lượng mạng hoặc disk peak.

`selected_sources_terminal` kiểm tra đúng các lần nộp đã chọn; no_tex hợp lệ là
terminal nhưng không tăng `valid_tex_versions`. Not_found/unresolved không phải
source success. `all_version_sources_terminal` kiểm tra mọi lần nộp đã biết.
`publication_complete.all_complete` chỉ true khi mọi paper đủ điều kiện contract
và không có lỗi bằng chứng. References chưa complete có `reference_items=null`;
chỉ tập đã được xác nhận đầy đủ mới có thể là 0.

`run_benchmark()` chuyển cùng `bbl_artifact_dir` mà Batch dùng sang lần
`aggregate_batch()` tự động. Khi gọi aggregation riêng, truyền
`bbl_artifact_root` rõ ràng. `scripts/generate_m1_report.py` nhận cùng đường dẫn
qua `--bbl-artifact-root`; nếu corpus có checkpoint chính sách `.bbl` mà thiếu
root hoặc artifact không hợp lệ, script dừng trước khi xuất báo cáo.

```bash
python3 scripts/generate_m1_report.py \
  --corpus data/corpus \
  --bbl-artifact-root data/reports/derived_bibliography/bbl-exception-v1 \
  --output reports/m1_summary.md
```

## Lược đồ báo cáo và xuất xứ dữ liệu

```text
benchmark_results/<experiment_id>/
  intent.json       # cấu hình/provenance trước khi gọi Batch
  resources.json    # output benchmark foundation cho invocation này
  report.json       # report adapter và aggregate tại thời điểm kết thúc
```

Experiment directory được tạo độc quyền; nếu đã tồn tại thì từ chối trước khi
gọi Batch. Không ghi đè experiment cũ. Intent còn lại khi process bị kill là bằng
chứng chưa có report cuối; không coi là phép đo hoàn tất.

Báo cáo dùng lược đồ 1, gồm:
- Experiment ID, batch ID, attempt IDs và run IDs của invocation hiện tại.
- `previous_attempt_ids`, selection, SHA-256 fingerprint, các lần nộp, timeout,
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

## Chỉ số v2: công thức và phạm vi đo

Các chỉ số được định nghĩa theo phép đo mà mã hiện hành ghi nhận. Môi trường
benchmark của quy trình M1 là Colab CPU-only.

| Chỉ số | Định nghĩa trong code |
| --- | --- |
| Wall time end-to-end | `metrics_v2.wall_seconds`: perf_counter trước/sau toàn callback Batch; gồm setup Batch, checkpoint, pacing, retry và log cuối của Batch |
| Trung bình mỗi paper | `timing_metrics.average_paper_seconds`: tổng interval paper đã ghi qua các attempts / số ID duy nhất trong cohort |
| M1 trung bình mỗi ID được giao | `assigned_paper_timings.average_seconds`: source và S2 cho toàn bộ ID được giao, gồm ID xác nhận `not_found`; để null nếu thiếu interval ở bất kỳ ID nào |
| Entry Discovery | `timing_metrics.discovery_wall_seconds`: tổng thời gian quanh `query_arxiv()`, gồm chờ limiter, retry và parsing; không gồm cache hoặc ghi Storage |
| RAM crawler | `ram.sampled_peak_bytes` và `ram.average_bytes`: RSS lấy mẫu của tiến trình crawler cùng descendant psutil quan sát được |
| RAM toàn hệ thống | `system_ram.sampled_peak_bytes` và `system_ram.average_bytes`: RAM `used` toàn host theo psutil, gồm cả tiến trình không thuộc crawler |
| Disk peak | `disk.sampled_peak_bytes`: tổng logical bytes các thư mục đo tại mỗi lần quét, lấy giá trị lớn nhất |
| Final corpus size | `disk.final_corpus_bytes`: dung lượng file của riêng corpus tại lần quét cuối |

Wall time không gồm khởi động Python, setup/final probes của sampler, serialize
benchmark report và audit sau crawl. RAM endpoint probes nằm sát hai đầu callback;
probe RAM cuối không chờ disk scan dài. Cả trung bình RSS crawler và RAM toàn host
là trung bình số học theo mẫu hợp lệ, không time-weighted. Thời gian paper nằm giữa
hai callback hiện có, không gồm toàn bộ overhead cấp Batch. Với M1 ba pha, summary
phân biệt average trên paper tìm thấy (`paper_timings`) và average trên toàn bộ ID
được giao (`assigned_paper_timings`); benchmark cuối dùng mẫu số toàn bộ ID được giao.
Nếu còn ID chưa xử lý hoặc mất interval do gián đoạn, average để null; subtotal
đã biết và số interval thiếu vẫn được ghi.

Discovery timing được thêm trực tiếp vào summary của Pipeline:
`discovery.wall_seconds` và `timing_status=measured|cached|not_attempted`.
Cached có 0 giây gọi API và không tăng số queried. Không thêm observer, không thêm
sự kiện mới vào `on_progress`; Batch tiếp tục nhận đúng paper_started/paper_finished.
Log cũ thiếu timing không được suy ra thời gian Discovery từ thời gian paper.

### Lấy mẫu tài nguyên

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
Backend, số phát hành và phạm vi đo được ghi rõ. Khi thiếu psutil trên Linux, phương án dự phòng
`/proc/self/statm` chỉ đo process hiện tại; không được gọi đó là RAM toàn hệ thống
nếu có subprocess. Windows thiếu psutil ghi backend_missing/not_collected.
RAM toàn host chỉ có khi psutil đọc được `virtual_memory().used`; nếu backend này
thiếu, `system_ram` là `not_collected` và benchmark đầy đủ bị đánh incomplete.
Không dùng ru_maxrss lịch sử để tính average RSS.

Peak RAM/disk là **sampled peak**, có thể bỏ sót spike hoặc file tạm sống ngắn.
RAM toàn host chịu ảnh hưởng bởi tiến trình nền của Colab; RSS crawler dùng để
phân biệt footprint của chương trình. Disk đo logical bytes của regular files,
không phải allocated blocks hay toàn bộ dung lượng filesystem. Scan thiếu/lỗi
không được thay bằng 0.

### Kho dữ liệu, vùng tạm và Colab

`disk_roots=None` chỉ đo corpus. Có thể truyền list/tuple gồm corpus và các thư mục
scratch riêng, không trùng/lồng nhau. Final corpus size vẫn tách khỏi tổng scratch.

Downloader hiện dùng TEMP của process để chứa archive; thư mục extraction nằm
dưới corpus. Vì vậy, **mặc định chưa bao gồm archive ngoài corpus**. Muốn đo phạm vi
đó trong benchmark thật, chuẩn bị scratch riêng và cấu hình TEMP/TMP trên Windows
hoặc TMPDIR trên Linux **trước khi khởi động Python**, rồi thêm scratch vào
disk_roots. Không thay TEMP toàn máy, không trỏ vào TEMP dùng chung, không quét
toàn bộ /tmp. Sampler không tự tạo/xóa dữ liệu hoặc đổi môi trường.

Code dùng Python/threading/pathlib và psutil tùy chọn, chạy được trên Windows;
Linux có fallback nêu trên. Phép đo M1 chính thức cần chạy trên CPU Colab,
chọn phạm vi disk đầy đủ và ghi lại môi trường thực tế. Nếu một thư mục không
đọc được hoặc phép quét chưa kết thúc, báo cáo giữ giá trị dung lượng là `null`
và nêu trạng thái của từng thư mục trong `disk.root_samples`; không dùng mẫu gần
nhất làm kích thước cuối. Khi tổng hợp nhiều lượt, công cụ chỉ chuyển tiếp các
trường chẩn đoán đã kiểm tra và giữ chúng theo từng lượt đo.

### Lưu và tổng hợp nhiều lượt chạy

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
- Paper/lần nộp coverage vẫn do Storage/checksum xác nhận; không lấy số attempts
  làm số paper hoặc cached/skipped làm download mới.

Legacy logs/reports vẫn đọc được. Attempt cũ không có measurement giữ missing,
full aggregate để null, có known subtotal riêng. Không gán commit hay môi trường
mới cho lượt cũ. Sidecar hỏng hoặc sai identity/fingerprint được liệt kê trong
invalid_attempt_ids, giữ nguyên để audit. Mất report do hard kill không được suy
ra là zero resource usage.

HTTP requests/errors và tổng download traffic vẫn not_collected; chưa thay HTTP
client để instrument từng attempt. `publication_complete` tiếp tục tuân thủ
contract v2, không đổi vì benchmark hoặc batch kết thúc.

## Kiểm thử ngoại tuyến

```powershell
.\venv\Scripts\python.exe -B -m unittest discover -s tests -p "test_benchmark*.py" -v
.\venv\Scripts\python.exe -B -m unittest discover -s tests -v
```

Fixtures dùng corpus/logs TEMP, fake HTTP/Pipeline và mock engine đo; có ca gọi
foundation thật trên fixture nhỏ để kiểm tra semantics và lược bỏ exception.
Đây là kiểm thử phần mềm, không phải số liệu benchmark crawler thực tế.

## Đo giai đoạn S2

`run_benchmark(..., s2_config={...})` truyền cấu hình qua Batch/Pipeline và lưu cấu hình không có khóa trong báo cáo/định nghĩa lượt chạy. Lớp đo mức thấp hỗ trợ lượt arXiv-only để thử nghiệm; quy trình M1 Colab luôn bật S2. Xem [hướng dẫn tích hợp S2](../guides/s2_integration.md).
Wall time/resource sampling bao gồm S2; Discovery timing vẫn chỉ thuộc arXiv.
History nhận diện stage s2_metadata/s2_references, nhưng không suy HTTP counts
từ attempts hoặc số items. References chưa complete vẫn có count unknown.

## Số liệu HTTP bổ sung, tương thích báo cáo cũ

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


## Phạm vi đo lịch sử phiên bản

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
