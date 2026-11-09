# M1 Colab CPU-only — pilot và bàn giao vận hành

Notebook: [m1_colab_cpu_pilot.ipynb](../../notebooks/m1_colab_cpu_pilot.ipynb).
API baseline đã đọc: commit `63f80da`; notebook/hướng dẫn này chưa được chạy trên
Colab. Smoke Windows một paper không chứng minh Drive hoặc Colab đã nghiệm thu.

## Bắt đầu

1. Người dùng review rồi tự commit/push bộ bàn giao. A mở notebook từ GitHub hoặc
   upload file vào Colab. Chọn **Runtime → Change runtime type → None**.
2. Điền **full hash của commit đã review chứa bộ bàn giao** vào `CODE_COMMIT`.
   Code được clone vào `/content`, checkout đúng commit chỉ trong clone mới.
   Nếu clone cũ sai commit/dirty thì notebook dừng, không reset/ghi đè. Bản baseline
   `63f80da` chưa có notebook/requirements mới, không dùng nó làm commit bàn giao.
3. Mount Drive, cho phép truy cập. Cài `requirements-colab.txt` trong runtime Colab;
   không cài gì lên Python Windows. Crawler/config dùng stdlib, chỉ `psutil==7.2.2`
   cần thêm để đo RSS process tree. Python tối thiểu theo code là 3.10; Python
   3.14.1/psutil 7.2.2 đã chạy smoke Windows, Python/psutil thực tế Colab phải ghi
   nhận và kiểm chứng. Không mặc định phiên bản Colab giống máy local.
4. Giữ 3 ID mặc định đã có Discovery resolved trong checkpoint pilot local:
   `24120010/2508.07872`, `24120010/2508.12871`, `24120035/2507.16659`.
   Đây không phải cam kết server luôn có cùng số versions. ID mới vẫn qua
   validation MSSV/ownership/trùng trước request; notebook chặn quá 5 ID.
5. Chạy preflight. Kiểm tra đúng Drive/cohort mới, dung lượng/quota và quyền ghi.
   Chỉ xác nhận `CPU_ONLY_CONFIRMED=True`, `SINGLE_WRITER_CONFIRMED=True` sau khi
   biết runtime CPU-only và không có writer khác dùng corpus.
6. Đổi `RUN_PILOT=True` ngay trong ô Pilot, chạy riêng ô đó. Tham số thực tế:
   `versions=None, revision_history=True, s2_config=None, timeout=30,
   max_attempts=1`; gọi `run_benchmark()` để đo chính `run_batch()` đang dùng.
7. Chạy ô audit. Xem cả report, dates/provenance, checksum, pending/errors và
   trạng thái phép đo; `batch_status=finished`/`aggregation=verified` chưa phải
   publication complete. Không suy số references là 0 khi S2 chưa chạy.
8. Chỉ sau review, bật riêng `RUN_RESUME=True` trong ô Resume. Không bật hai ô
   cùng lúc hoặc tạo vòng lặp tự chạy lại. Chạy lại ô audit để lưu report mới.

Không nạp Secrets/.env trong phase arXiv-only. Notebook không có key thật hoặc
outputs đã thực thi. Không dùng “Run all” để bắt đầu một crawl chưa review.

## Drive, filesystem và một writer

```text
MyDrive/Group10-M1/<cohort_id>/
  corpus/<paper-folder>/{metadata,references,manifest}.json
  corpus/<paper-folder>/vN/...tex/bib
  logs/<batch_id>/batch.json
  logs/<batch_id>/attempts/<attempt_id>.json
  logs/<batch_id>/measurements/<attempt_id>.json
  reports/<experiment_id>/{intent,resources,report}.json
  sessions/<experiment_id>.json
  handoff/audit-<uuid>.json
  preflight/fixture-<uuid>/...       # fixture riêng, không tính corpus/benchmark
/content/group10-scratch/<cohort_id>/  # archive tạm, mất khi runtime bị xóa
```

Storage ghi file tạm cùng thư mục, flush/fsync rồi `os.replace`; JSON atomic
**từng file**, không phải giao dịch của cả bộ metadata/references/manifest.
Preflight kiểm tra writer/reader thật, replace, link count và SHA-256 trên mount
hiện tại. Không bảo đảm remote sync hoặc crash consistency của Drive. Corpus thật
vẫn phải qua Storage/manifest/checksum và thử Resume sau pilot. Nếu probe lỗi,
dừng trước request; không đổi các validator hoặc xóa checkpoint để vượt kiểm tra.

Drive có latency, quota và lỗi I/O với nhiều file; VM Colab có thể bị thu hồi.
Không đặt 20.000 folders ngay root Drive. Dùng cohort nhỏ có corpus riêng và đường
dẫn ổn định. Không hứa cấp tài nguyên/runtime cố định. [Colab FAQ](https://research.google.com/colaboratory/faq.html).

Notebook dùng xác nhận thủ công một writer, **không có distributed lock**. Corpus
không được ghi bởi notebook, runtime, máy khác hoặc batch S2 đồng thời. Sau ngắt,
xác minh runtime cũ đã dừng; đừng chỉ dựa vào trạng thái tab trình duyệt. Khi sao
lưu, dừng writer, copy và kiểm chứng bản sao; giữ nguyên bản gốc, không move khi
đang ghi. Scratch có thể mất; dữ liệu thiếu/orphan phải audit, không xóa/ghi đè.

## Resume và cohort mới

- Resume: mount lại cùng Drive, cùng code và **nguyên selection/thứ tự, cohort ID,
  batch ID, corpus/log paths, versions, history, timeout, attempts, User-Agent**.
  Chạy lại setup/preflight; bỏ ô Pilot, chỉ dùng `resume=True`. Mỗi invocation có
  attempt/run ID mới; notebook sinh experiment ID mới, không overwrite report cũ.
- Không sửa `batch.json` để ép fingerprint. Đổi đường dẫn (ví dụ Windows → Colab),
  selection hoặc cấu hình cần batch ID mới; giữ log/definition cũ làm bằng chứng.
- Source/history hợp lệ được skip/cache. Pending/lỗi retryable được thử tiếp;
  checkpoint hỏng, checksum sai và orphan bị chặn. Partial history chưa checkpoint
  từng version nên có thể đọc lại history, không tải lại source tốt.
- 403/429 dừng batch ngay khi client trả kết quả, với attempts=1 không retry.
  Không còn request sang version/paper sau. Review nguyên nhân/cooldown/quyền
  truy cập rồi mới quyết định invocation khác; limiter không chia sẻ qua máy.
- Ô Pilot đầu từ chối corpus/logs/reports đã có dữ liệu trước khi gọi benchmark;
  không dùng batch ID mới để âm thầm tái sử dụng corpus cũ.
- Cohort MỚI: đổi `COHORT_ID`, chọn cặp `(MSSV, base ID)` thuộc Group 10 và batch
  ID mô tả phase; `resume=False`. Giữ max 5 ID cho pilot này. Không lấy toàn bộ
  iterator 20.000 ID vào notebook. Khi vận hành lớn cần duyệt cohort/cap riêng,
  không chỉ bỏ guard 5 ID. `max_ids` giới hạn paper, không phải request budget.
- Trước chuyển cohort: kiểm tra tất cả ID có record hoặc lý do chưa xử lý,
  source/version terminal/failed/no-TeX được phân biệt, ngày verified, checkpoint
  không invalid và references đúng trạng thái. ArXiv phase xong không phải M1 xong.

## S2 enrichment sau arXiv-first

Làm trong invocation được duyệt riêng, **cùng corpus/cohort**, batch ID mới,
`resume=False`; không bật S2 bằng cách đổi definition của batch arXiv cũ.
Giữ nguyên selection và versions/history. Ví dụ dưới chỉ là hướng dẫn, notebook
pilot không tự chạy nó:

```python
import os
from google.colab import userdata
os.environ["SEMANTIC_SCHOLAR_API_KEY"] = userdata.get("SEMANTIC_SCHOLAR_API_KEY")
# Không print key, environment hay headers; không ghi .env lên Drive/Git.
s2_config = dict(timeout=30, max_attempts=1, min_interval=2,
                 reference_limit=100, max_pages=1000)
# min_interval phải theo quota thực tế của key, không phải bảo đảm tránh 429.
s2_report = benchmark_runner.run_benchmark(
    PAIRS, CORPUS, batch_id="s2-enrichment-01", max_ids=len(PAIRS),
    log_root=LOGS, report_root=REPORTS, experiment_id="s2-" + uuid4().hex,
    resume=False, versions=None, revision_history=True, s2_config=s2_config,
    timeout=30, max_attempts=1, sample_interval=0.1, disk_interval=30,
    disk_roots=[CORPUS, SCRATCH],
)
```

Đây **không phải strict S2-only mode**: arXiv pending/retryable vẫn có thể được
thử lại. Audit arXiv hoàn tất trước nếu muốn tránh request arXiv mới. Metadata/
references S2 tốt được cache; partial references giữ prefix và được client đọc
lại/đối chiếu từ đầu, không mặc định nhảy ngay tới offset. Chưa có S2 batching
nhiều publications trong Pipeline. Metadata M1 có thể còn partial vì fields thiếu;
references complete không khẳng định S2 nhận diện mọi reference trong LaTeX.
Xem [S2 integration](s2_integration.md) và [Colab Secrets implementation](https://github.com/googlecolab/colabtools/blob/main/google/colab/userdata.py).

## Bàn giao số liệu cho C: tránh đếm trùng

Giữ reports/intent/resources, logs/batch/attempts/measurements, session notes,
audit JSON, commit và snapshot corpus đã chốt. C đọc đầu vào đã lưu; build Jupyter
Book không gọi crawler. `sessions` bổ sung xác nhận Colab CPU-only: adapter hiện
vẫn ghi `measurement_environment=not_collected`, không sửa report để giả đã đo.
Đầu ra audit JSON của notebook không phải exporter papers/references/Parquet.

| Chỉ số | Cách dùng có thể kiểm toán |
| --- | --- |
| Wall time | Trong một batch dùng `benchmark_metrics.end_to_end_wall_seconds`: cộng callback intervals của attempts duy nhất; không cộng lại các report aggregate lũy kế hoặc khoảng nghỉ giữa sessions. Gồm pacing/retry, không gồm clone/pip, mount, probes cuối/report serialization/audit. |
| Discovery | Cộng interval queried thực đo một lần; cached = 0 API time. Không suy từ tổng paper time. |
| Paper average | Tổng paper intervals của mọi attempts/phases / số ID ứng viên **duy nhất** trong cohort; gồm lượt Resume. Không lấy trung bình các batch averages. Thiếu interval/unprocessed thì ghi unknown/lower bound, không báo đầy đủ. |
| Peak RAM | Max sampled RSS process-tree peaks khi scope/backend phù hợp; không cộng peaks, không gọi sampled peak là cực đại tuyệt đối. |
| Average RAM | `sum(sum_sample_bytes)/sum(sample_count)` khi scope/backend phù hợp. Đây là trung bình quan sát, **không time-weighted**; giữ intervals/sample counts. Không trộn backend hoặc tự bỏ samples lỗi. |
| Peak disk | Max logical-byte samples cùng phạm vi corpus + scratch của notebook; loại logs/reports/repo. Cohort roots khác nhau là scope khác: không lấy max rồi gọi peak toàn bộ corpus đang tích lũy. |
| Final size | Snapshot cuối sau S2, không cộng snapshot mỗi attempt/phase. Với các corpus cohort tách biệt chỉ cộng snapshot cuối của từng cohort tại mốc chốt, không cộng size ở các thời điểm lặp. |
| HTTP/coverage | HTTP đếm transport attempts thực; cached/skipped không tăng requests. Coverage cuối kiểm checksum theo `(MSSV, ID, version)` duy nhất, giữ lịch sử timeout/429. References chưa complete là unknown. |

`aggregate_batch()` hiện **chỉ tổng hợp một batch ID**. Nhiều arXiv/S2 phase hoặc
cohort phải giữ danh mục batch/attempt/experiment IDs và dùng từng interval đúng
một lần; không có cross-batch aggregator tự động trong nhiệm vụ này. Các report
arXiv cũ là snapshot lịch sử; không diễn giải coverage cũ như snapshot sau S2.
Nếu hard kill làm mất sidecar/resource interval, report phải liệt kê thiếu và
known subtotal; không khôi phục số đo bằng 0 hoặc thời gian lịch. Callback intervals
nhiều process chạy song song không được cộng thành system wall time.

Benchmark M1 cuối phải gồm **arXiv + S2** trên Colab CPU-only theo
[đề M1](https://hcmus-cs-ta.github.io/lab-book/intro-ds/statement/crawling/).
Pilot arXiv-only này chưa đáp ứng benchmark cuối. Nếu cần peak disk của toàn bộ
corpus/không gian làm việc xuyên các cohort, phải chốt scope và phép đo chung trước
lượt benchmark cuối; không suy ngược peak đó từ các snapshot riêng. Khi hard kill,
chưa thể tuyên bố phép đo end-to-end đầy đủ chỉ vì Resume sau đó thành công.

## Kiểm thử bộ bàn giao

Notebook lưu outputs rỗng. Offline tests kiểm cú pháp/guard/call wiring, filesystem
probe trên TEMP, audit và Resume với fake benchmark/HTTP bị chặn; không mount
Drive, clone, pip install hoặc gọi API. Từ root repository:

```powershell
.\venv\Scripts\python.exe -B -W always::ResourceWarning -m unittest discover -s tests -p test_colab_notebook.py -v
.\venv\Scripts\python.exe -B -W always::ResourceWarning -m unittest discover -s tests -v
git diff --check
```

Nghiệm thu Colab vẫn cần: cài dependency thật trong runtime CPU-only, probe Drive
PASS, pilot 3 ID được duyệt, source/history/HTTP/resource metrics hợp lệ và Resume
0 HTTP cho dữ liệu đã verified. Chưa chạy các bước live đó trong lần chuẩn bị này.
