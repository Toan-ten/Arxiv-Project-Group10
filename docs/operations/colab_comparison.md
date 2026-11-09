# M1: Combined vs Two-phase trên Colab CPU-only

[Notebook riêng](../../notebooks/m1_colab_strategy_comparison.ipynb) dùng helper
`scripts/colab_comparison.py`. Không thay notebook Pilot, Storage contract hay
crawler. Chưa chạy Colab/API thật trong lần chuẩn bị; chưa có kết quả A/B.

## Phạm vi thử nghiệm

- **A / combined:** `run_benchmark(..., s2_config=...)` gọi Pipeline arXiv → S2
  cho từng paper theo thứ tự cohort.
- **B / arxiv, s2:** lần đầu `s2_config=None` cho toàn cohort; chỉ khi source/history
  hợp lệ mới gọi batch `s2` trên **cùng corpus B**, bật cùng cấu hình S2 như A.
  Hai pha dùng batch ID khác nhau, reports/attempt IDs riêng.
- `versions=None`, `revision_history=True`, arXiv timeout 30/attempts 1; S2 mặc định
  timeout 30/attempts 1/min_interval 2/reference_limit 100/max_pages 1000. Pacing
  arXiv ≥3 giây và limiter metadata/references S2 của Pipeline giữ nguyên.
  Interval S2 cần phù hợp quota thực tế; không bảo đảm tránh 429.
- Chọn **50 vị trí ID ứng viên** từ bốn assigned ranges: 13/13/12/12 theo thứ tự
  config, lấy mẫu không hoàn lại bằng seed 10 rồi interleave owners. Không gọi API
  để chọn sample, không lọc theo tồn tại và không dùng Pilot 10/500. Seed/list/
  fingerprint lưu cố định; không coi 50 candidates là 50 paper hợp lệ.
- S2 preflight riêng dùng 4 ID đầu (bốn owner), tối đa **một POST metadata**,
  attempts=1, strict identity. Không gọi references/source, không lưu metadata vào
  A/B nên không làm ấm corpus. Đây là kiểm tra truy cập tại thời điểm đó, không
  bảo đảm workload 50 ID sẽ không bị rate limit.

## Cách dùng bằng nút bấm

1. Người dùng tự review/commit/push các file mới. Mở notebook trên Colab, chọn
   **Runtime → Change runtime type → None**. Điền full `CODE_COMMIT` của bộ code
   đã review, `EXPERIMENT_ID` mới cho trial lạnh. Giữ cùng SEED/S2_CONFIG cho A/B.
2. Chạy setup: mount Drive, clone đúng commit trong `/content`, cài
   `requirements-colab-compare.txt`. Clone có sẵn sai commit/dirty bị từ chối;
   không reset hoặc sửa notebook Pilot. Nếu psutil bị cache phiên bản khác,
   restart session rồi chạy lại setup. Không cài package lên Windows trong tác vụ này.
3. Thêm Colab Secret `SEMANTIC_SCHOLAR_API_KEY`, cấp notebook access. Không dán key
   vào code, text widget, outputs hoặc .env. Mã chỉ nạp key khi bấm nút mạng;
   giữ nguyên key/quota trong trial, không đổi Secret giữa A và B;
   exceptions/UI chỉ in thông báo an toàn, không headers/key/traceback chứa secret.
4. Chạy ô giao diện. Probe filesystem phải PASS trước khi nút được tạo. Xem đúng
   cohort/fingerprint/output roots. Tick CPU-only và Only writer sau khi bảo đảm
   runtime cũ/máy khác không ghi corpus. Đây là xác nhận thủ công liên runtime.
5. Bấm **S2 preflight (4 IDs)**; xem HTTP status/attempts và `ok=true` trước 50 ID.
6. Bấm **Run A** hoặc **Run B**, đợi xong rồi mới bấm phương án còn lại. B tự chuyển
   từ arXiv sang S2 nếu đủ điều kiện; không có chạy song song/Run all/Full Crawl.
7. Bấm **Audit + compare** để xem bảng và lưu JSON/CSV mới. Xem stopped, integrity,
   source/no-TeX/not_found, references complete và publication complete riêng biệt.
8. Khi cần, bấm **Resume A/B**. Giữ đúng experiment/cohort/config/commit/paths.
   Runtime mới: mount/setup lại, chọn cùng experiment; không bấm Run mới. B Resume
   bỏ invocation arXiv đã hoàn tất và chỉ tiếp tục pha S2 nếu pha đó còn cần xử lý.

S2 requests không được gửi tự động khi import/tạo UI. Nút bị vô hiệu khi callback
đang chạy; lock chung trong kernel chặn re-entry, kể cả hai đối tượng UI. Không có
distributed lock giữa các máy. Một writer là yêu cầu bắt buộc của người vận hành.

## Dừng và Resume

403/429 ở arXiv, history, metadata hoặc references được Pipeline ghi checkpoint/
run log rồi stopped. Wrapper không gọi pha tiếp theo hoặc tự chạy phương án kia.
Client dùng attempts=1 nên không retry sau 429. Preflight 403/429 cũng chặn workload.
Trước một hành động mạng tiếp theo phải review cooldown/quota/key/quyền truy cập,
rồi tick **Policy stop reviewed**. Tick được tiêu thụ cho một thao tác, không tạo
retry loop. Lịch sử stop được giữ, không sửa thành success để xếp hạng tốc độ.

Lỗi mạng/metadata partial không được biến thành references rỗng complete. B không
chuyển sang S2 nếu arXiv source/history còn chưa hợp lệ; A vẫn theo chính sách
paper-level hiện tại của Pipeline. Hỏng log/checkpoint phải audit trước request.
Không xóa orphan, sửa manifest hay bỏ checksum để tiếp tục.

Source đã lưu được hash trước/sau Resume. Khi mọi source/history/S2 đều cacheable,
wrapper yêu cầu attempt Resume có metrics collected và **0 HTTP**, đồng thời giữ
nguyên metadata/references/source; manifest run_id/updated_at có thể đổi. S2
not_found không phải cache hoàn chỉnh hiện tại nên Resume có thể truy vấn lại;
partial references đọc lại prefix để đối chiếu theo client hiện có.

S2 phase B là enrichment trên corpus hiện có, không phải strict S2-only mode:
Pipeline vẫn kiểm tra arXiv/cache. Gate arXiv của wrapper giúp tránh chạy enrichment
khi còn pending. Không thêm S2 metadata batching; cả A và B vẫn một metadata
publication/request theo Pipeline hiện tại. Vì vậy so sánh này đánh giá cách sắp
xếp công việc, không đo hiệu quả batching 100 ID/request.

## Nơi lưu và an toàn Drive

```text
MyDrive/Group10-M1-Comparison/<experiment>/
  experiment.json                 # commit, config, 50 IDs, fingerprint, paths
  preflights/<uuid>.json           # status/counters, không raw paper/key
  filesystem-preflight/fixture-*/  # fixture riêng, ngoài metrics/corpus thật
  A/{corpus,logs,reports}/         # batch combined
  B/{corpus,logs,reports}/         # batches arxiv + s2; dùng chung B/corpus
  sessions/<experiment_id>.json    # CPU confirmation/runtime/profile/order
  operations/<uuid>.json          # whole button interval; running nếu hard kill
  comparisons/<uuid>/{comparison.json,comparison.csv}
/content/group10-comparison-scratch/<experiment>/{A,B}/
```

Source/checkpoints/logs trên Drive; scratch có thể mất khi runtime bị thu hồi.
Probe chỉ kiểm roundtrip fsync/replace/regular-file/checksum ở mount hiện tại,
không chứng minh remote sync hoặc crash durability. Nhiều JSON vẫn được ghi
atomic riêng, không giao dịch cả corpus. Drive có quota/latency/lỗi I/O; dừng writer
trước sao lưu/đối soát. Không sửa corpus Pilot cũ. [Colab FAQ](https://research.google.com/colaboratory/faq.html).

## Đọc bảng và quyết định

| Cột/nhóm | Ý nghĩa |
| --- | --- |
| First-pass end-to-end | Interval thực đo từ sau admission/intent đến kết thúc hành động nút, gồm credentials, boundary waits, crawler, gates/audits/reporting. B đo hai pha liên tiếp trong cùng một hành động. Không gồm clone/pip/mount/preflight hay ghi operation log. |
| First-pass crawler callbacks | Tổng callback wall của **attempt đầu mỗi pha**: A một, B hai; gồm pacing/retry/checkpoint nhưng không reporting/audit ngoài callback. Giữ riêng để đối soát end-to-end. |
| All attempts wall | Tổng callback intervals của các attempts duy nhất, gồm Resume. Không cộng các report aggregate lũy kế. Thiếu sidecar thì unknown và có known subtotal. |
| RAM peak/mean | Peak lấy max; average = tổng sum_sample_bytes / tổng sample_count, không average các averages. Bảng chính hiển thị first-pass; JSON/CSV giữ thêm tổng mọi attempts. RSS bao gồm kernel notebook và descendants quan sát được, không phải toàn RAM VM. |
| Disk peak/final | Peak lấy max mẫu corpus+scratch **cùng phương án**; B không cộng peaks hai pha. Final size dùng snapshot cuối, không cộng snapshot mỗi phase/Resume. Không gồm corpus phương án kia, logs/reports/Git; là logical bytes, không allocated/quota. |
| HTTP | Đếm transport attempts từ durable metrics, không tính cached/skipped thành request mới; 403/429 và attempts bị ngắt không được che mất. |
| Coverage | Chỉ final checksum-verified snapshot của cohort; không cộng coverage arXiv và S2. Not_found/no-TeX/unknown được phân biệt. Publication complete không suy từ batch finished. |
| Cold eligible | Gate bảo thủ: đủ pha, lượt đầu finished/không lỗi, source/history/S2 hợp lệ, đầy đủ resource/HTTP metrics và không có stop. Hai arms phải cùng outcome fingerprint và runtime profile. |

Resource sums dùng observations `(batch_id, attempt_id)` riêng, không cộng RAM
peaks/disk snapshots. Discovery time cộng interval queried, cached không tăng API
time. Average paper time = tổng paper intervals các attempts/phases / **50 ID duy
nhất**, không chia 100 vì B có hai phase. Không có mẫu thì null, không giả số 0.
Khoảng chờ ≥3s trước invocation và audit/reporting nằm trong end-to-end, nhưng ngoài
callback wall. RAM/disk chỉ lấy mẫu trong callbacks, chưa đo liên tục toàn wrapper
đặc biệt khi audit/reporting; không gọi các peaks đó là cực đại toàn nút bấm. Operation
log còn running sau hard kill giữ wall unknown, không điền 0 hoặc tự tái tạo interval.

Nếu có stop/rate limit, thiếu measurement, coverage khác nhau, source/refs chưa đủ
hoặc runtime thay đổi, bảng ghi **không đủ bằng chứng**, không tuyên bố A/B nhanh
hơn từ thời gian một run bị dừng sớm. Publication vẫn có thể partial vì fields
thiếu; số complete và chất lượng dữ liệu luôn được công bố riêng.

Một trial 50 ID chỉ là thăm dò. Quota/server latency thay đổi theo thời gian; kernel
có thể giữ RAM sau A. Giữ thứ tự chạy trong session notes, kiểm tra baseline RSS
trong resources.json, rồi lặp trial mới cùng seed thứ tự B→A khi được duyệt, chỉ
một runtime truy cập tại một thời điểm. Không ngoại suy trực tiếp ra 20.000 ID.
Chọn chiến lược dựa trên độ phủ, tỷ lệ stop/lỗi, khả năng Resume, chi phí recovery
và tài nguyên cùng với thời gian, không chỉ lấy cột wall nhỏ hơn.

## Offline validation

Tests dùng fake HTTP, fake benchmark và fake widgets; không mount Drive, clone,
cài dependencies hay gọi API. Giao diện Colab/ipywidgets thực vẫn cần kiểm chứng.

```powershell
.\venv\Scripts\python.exe -B -W always::ResourceWarning -m unittest discover -s tests -p test_colab_comparison.py -v
.\venv\Scripts\python.exe -B -W always::ResourceWarning -m unittest discover -s tests -v
git diff --check
```

Nguồn API giao diện: [ipywidgets Button/Output events](https://github.com/jupyter-widgets/ipywidgets/blob/main/docs/source/examples/Widget%20Events.ipynb),
[Colab Secrets](https://github.com/googlecolab/colabtools/blob/main/google/colab/userdata.py).
