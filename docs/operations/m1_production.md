# Bàn giao A: vận hành M1 arXiv-first trên Colab CPU-only

Mở `notebooks/m1_colab_arxiv_production.ipynb` bằng Colab. Notebook có đúng
hai ô code: **Setup** và **Run M1**. Không cần widgets hay Semantic Scholar key.
Đây là bước thu thập arXiv; chưa phải hoàn thành M1 vì chưa bổ sung S2/references.

## Lần đầu

1. Người phụ trách review/commit/push bộ bàn giao. Trong Setup, điền **full SHA
   của commit chứa bộ bàn giao** vào `CODE_COMMIT` rồi lưu bản notebook của A.
2. Chọn Runtime → Change runtime type → **CPU / accelerator None**. Dừng mọi
   runtime/crawler khác ghi cùng corpus. Chỉ một writer cho toàn chiến dịch.
3. Bấm Run ô Setup: mount Drive, clone checkout riêng trong `/content`, kiểm tra
   commit sạch, cài `requirements-colab.txt`, in phiên bản thực tế. Setup không crawl.
   Checkout có sẵn khác commit/bị sửa sẽ bị từ chối; không tự reset/pull.
4. Trong **một khối CONFIG ở ô Run M1**, chọn Drive root mới và cohort đầu/cuối.
   Mặc định chỉ **cohort 1**, 100 ID. Kiểm tra đường dẫn trước khi bấm Run.
5. Đọc tóm tắt và audit JSON sau mỗi cohort. Khi kết quả tốt, đổi số cohort và
   bấm Run tiếp; chỉ mở rộng số cohort mỗi lượt sau khi đã kiểm tra runtime/Drive.

Không cần đổi thuật toán hay nhiều cờ True/False. Giữ nguyên root, timeout và
sampling khi Resume; thông thường giữ cùng commit. Ngoại lệ nâng cấp bản vá
HTTP từ baseline `1fe51d7` được mô tả bên dưới. `campaign.json` và Batch bảo vệ cấu hình.
Không thay campaign.json, batch.json hay manifest bằng tay để vượt kiểm tra.

## Chọn đúng 20.000 vị trí ID

Cohort cố định **100 ID**, đánh số **1–200**, gồm cả hai đầu của các khoảng:

| Cohort | MSSV | Khoảng toàn phần |
| --- | --- | --- |
| 1–50 | 24120010 | 2508.07872–2508.12871 |
| 51–100 | 24120035 | 2507.16659–2507.21658 |
| 101–150 | 24120232 | 2502.08235–2502.13234 |
| 151–200 | 24120260 | 2501.02642–2501.07641 |

Danh sách được cắt từ `iter_assigned_ids()` theo cấu hình đã kiểm tra, chỉ giữ
100 cặp MSSV/ID của cohort hiện tại. Không chọn riêng paper đã biết tồn tại.
20.000 vị trí ứng viên không đồng nghĩa 20.000 publication hợp lệ.
`first_cohort` và `last_cohort` là cửa sổ **bao gồm hai đầu**, không tự mở rộng.
Ví dụ 51–52 chạy tối đa 200 vị trí của **24120035 (Đạt)**. Nếu lỗi/chưa xong
cohort 51 thì cohort 52 chưa được chạy.

## Run, Audit và Resume

- Chưa có batch: tạo `m1-arxiv-NNNN`, gọi `run_benchmark()` → Batch → Pipeline.
- Batch hợp lệ nhưng còn pending/timeout/history thiếu: Resume cùng cấu hình;
  source/checksum tốt và history verified được dùng lại, không tải lại vô điều kiện.
- Đã đủ pha arXiv: **chỉ audit**, không gọi Batch/Benchmark, không tạo attempt
  HTTP mới, không sửa corpus/logs/reports cũ; lưu một audit mới có tên riêng.
- `finished` chỉ có nghĩa đã xử lý danh sách. Pha arXiv chỉ đủ khi mỗi ID là
  not_found đã xác nhận, hoặc found/resolved với mọi version success/no_tex
  đã kiểm chứng và toàn bộ ngày history verified. No-TeX được giữ riêng.
- Metadata/publication có thể vẫn partial, references not_attempted; không
  dùng số references 0 để thay cho chưa thu thập, không tuyên bố M1 complete.
- `max_attempts`: Có thể tùy biến trong cấu hình (mặc định 1 trong ProductionRun, có thể tăng lên 3 khi môi trường mạng có độ trễ cao để retry cho timeout/5xx).
- 403/429 dừng ngay lượt Run, không sang version/paper/cohort tiếp theo;
  pacing arXiv dùng chung từ client hiện có. Không có outer retry.
  **Không bấm Run liên tục sau 403/429**: xem lỗi, chờ và quyết định trước.
  Bấm lại ô Run là yêu cầu thử mới của người vận hành, không tự lên lịch.
- Ngoài quy trình chuẩn hóa trong `m1_colab_arxiv_production.ipynb`, nhóm có thể sử dụng `m1_colab_fast_production.ipynb` để thu thập dữ liệu nhanh trên SSD kèm nạp trước siêu dữ liệu Kaggle offline.
- Checksum sai, orphan, thiếu log hoặc checkpoint không nhất quán: dừng để
  review, giữ nguyên bằng chứng, không tự sửa/xóa/ghi đè. Lỗi/exception nghiêm
  trọng cũng dừng; xem logs/reports trước lần tiếp theo.

Sau khi runtime mất kết nối: chắc chắn runtime cũ đã ngừng, mở lại notebook,
chạy Setup đúng commit, chọn lại **cùng cohort và Drive root**, rồi Run M1.
Storage giữ checkpoint trên Drive, không phụ thuộc `/content` tạm. Hard kill có
thể để lại orphan hoặc thiếu số đo; phải audit, không cam kết tự phục hồi mọi lỗi
filesystem. Atomic replace trên Drive không phải giao dịch nhiều JSON hay bảo
đảm chống mất dữ liệu khi Drive bị lỗi/quota. Có lỗi integrity thì dừng và bàn giao
log cho Toàn, không đổi root để che lỗi.

## HTTP audit và cooldown sau khi Colab restart

Mỗi attempt mới có thêm `arxiv_http_requests` trong
`logs/m1-arxiv-NNNN/attempts/<attempt-id>.json`. Mỗi phần tử ghi:

- `request_started_at_utc`: thời điểm ngay trước transport attempt; không phải
  bằng chứng server đã nhận request.
- `response_received_at_utc`, `http_status`: khi nhận headers; null nếu chưa
  nhận được HTTP response. Timeout khi đọc body có thể vẫn mang HTTP 200.
- `endpoint`, `query_identifier`, `attempt`: endpoint/ID arXiv được cho phép và
  số attempt; không lưu query tùy ý, API key, header khác hay response body.
- `retry_after`: nguyên header Retry-After nếu có.
- `cooldown_deadline_utc`: deadline từ số giây tính tại lúc nhận headers hoặc
  HTTP-date. Thiếu/sai header hay ngày không biểu diễn được thì null; không tự
  tạo thời gian chờ mới.

Event được ghi atomic vào attempt log trước khi client trả về Pipeline hoặc gửi
request tiếp theo. Lỗi ghi log dừng xử lý. Metrics HTTP cũ và Storage contract v2
không đổi; pacing vẫn 3 giây và Production vẫn `max_attempts=1`.

Trước một lượt crawl/Resume, Production và Batch đọc deadline trong các attempt
của **mọi batch dùng chung log root**. Nếu còn cooldown, dừng ngay với
`ArxivCooldownError` và in thời điểm UTC có thể tiếp tục; không tự sleep/retry,
không tạo attempt Benchmark mới ở Production. Có thể chạy lại ô Run sau deadline,
giữ cùng root/cohort/cấu hình. Không đổi batch ID để né cooldown. Cohort đã đủ dữ
liệu vẫn được audit không gọi API. Bằng chứng mới bị hỏng thì dừng để review.

Log cũ thiếu trường này vẫn đọc được, nhưng **không khôi phục được Retry-After
của lần 429 trước bản vá**. Người vận hành phải xem xét lần dừng cũ trước khi Run;
deadline hết hạn cũng không bảo đảm server sẽ chấp nhận request tiếp theo.

Sau khi người phụ trách review/commit/push bản vá, A dùng runtime Colab mới,
điền full SHA commit bản vá vào Setup, giữ nguyên CONFIG và Drive root. Bản vá
chỉ cho phép campaign cũ từ đúng commit
`1fe51d7c02d3baa520936c93c6e2a266541d53c3`, với mọi cấu hình khác giống hệt.
Không sửa campaign.json hoặc checkpoint để cập nhật SHA. Campaign giữ commit
ban đầu; audit mới ghi `campaign_code_commit` và `executing_code_commit`,
Benchmark ghi commit thực tế đang chạy. Commit cũ khác hoặc cấu hình đổi sẽ bị từ chối.

## Dữ liệu và số đo để bàn giao C

```text
<Drive root>/
  campaign.json                 Cấu hình bất biến, code commit và assignments
  corpus/                       JSON checkpoint + source mọi version (.tex/.bib)
  logs/m1-arxiv-NNNN/            batch.json, attempts/, measurements/
  reports/<experiment-id>/      intent.json, resources.json, report.json
  audits/cohort-NNNN-<uuid>.json Audit từng lượt, coverage/history/metrics
  scratch/                      Archive tạm do downloader quản lý
```

Corpus dùng chung giữa cohort; scope disk là **toàn corpus + scratch** trên Drive,
không gồm code/logs/reports. Sampling disk mặc định 120 giây để hạn chế quét toàn
corpus lớn; sampled peak có thể bỏ lỡ đỉnh giữa hai mẫu. Không có quota tổng corpus
tự động: A theo dõi dung lượng Drive và dừng giữa cohort khi gần giới hạn.

Benchmark tái sử dụng Adapter/Metrics v2: wall time callback gồm pacing/retry và
checkpoint, per-paper/discovery queried, HTTP transport attempts, sampled RAM
process-tree peak/mean và disk peak/final. Clone, pip, mount, audit/report ngoài
callback không nằm trong crawl wall time. RAM bao gồm kernel notebook và process
con; trung bình là **trung bình RSS các mẫu**, không phải time-weighted. Nếu thiếu
số đo/hard kill, giữ unknown/not_collected và known subtotal.

Trong một batch dùng aggregate của attempt IDs duy nhất, không cộng các report
lũy kế. Giữa cohort: cộng interval thời gian độc lập một lần, lấy max peak RAM/disk
khi cùng scope/backend, tính RAM mean bằng tổng sample bytes / tổng sample count;
không cộng peaks. Final corpus size là snapshot toàn corpus **cuối cùng**, không
cộng các snapshots ở từng cohort. Các cohort Resume muộn vẫn dùng scope corpus
chung. Chưa có công cụ tự tổng hợp toàn chiến dịch; giao C danh mục audits,
logs và reports gốc để đối soát, không tự coi bảng cuối mỗi cohort là tổng nhóm.
S2 phải được đo ở pha riêng sau này để benchmark M1 cuối bao gồm cả hai pha.

## Kiểm thử và giới hạn bàn giao

`tests/test_m1_production.py` dùng HTTP giả và corpus TEMP, kiểm tra lựa chọn đủ
20.000 vị trí, new/resume/audit, dừng chính sách, provenance/checksum và bảo vệ
dữ liệu. Không gọi API thật, mount Drive, cài package hoặc chạy 20.000 ID trong tests.
Notebook mới cần A kiểm tra một cohort trên Colab trước khi tăng cửa sổ chạy.
Không có distributed writer lock giữa nhiều VM; chỉ một runtime được phép ghi.

## Thử nghiệm retry HTTP 429 một lần (mặc định tắt)

Chỉ bật sau review, trong CONFIG duy nhất của ô Run M1:

```python
"retry_429": True,
"retry_wait_budget": 300.0,
```

Giữ nguyên Drive root, cohort, timeout và sampling khi Resume. Đây là chính sách
của **mỗi lần bấm Run**, ghi riêng trong attempt/audit; không đổi campaign.json,
batch definition hay Storage contract. Campaign baseline `1fe51d7` vẫn được
kiểm tra tương thích như bản vá cooldown trước. Không sửa SHA trong campaign bằng tay.

Khi bật, transport arXiv đang có được phép thử lại **đúng Request đó tối đa một
lần sau 429**. Không chạy lại cả paper/Batch, không bật retry cho timeout/5xx,
không thay pacing 3 giây và không gọi S2. Discovery, source và revision history
dùng chung chính sách/limiter. Nếu 429 tiếp diễn hoặc gặp 403 thì Pipeline ghi
kết quả lỗi cuối và dừng, không sang version/paper/cohort sau.

Trước sleep, HTTP event và kế hoạch chờ phải được ghi atomic vào attempt log:

- Retry-After hợp lệ được ưu tiên. Header số giây tính từ lúc nhận headers;
  HTTP-date là mốc server UTC. Pacing tối thiểu 3 giây vẫn áp dụng, kể cả header 0.
- Header thiếu/sai: backoff nội bộ **60 giây** từ lúc nhận headers.
  `cooldown_deadline_utc` vẫn **null**; không giả đó là deadline do server cấp.
- Các trường riêng: `retry_wait_source` (`retry_after|internal_backoff`),
  `retry_wait_deadline_utc`, `retry_wait_seconds` và `retry_429_decision`
  (`retry_once|retry_exhausted|budget_exceeded`).
- Ghi log thất bại thì dừng trước sleep/request tiếp. Lỗi evidence thì giữ nguyên,
  dừng để review; không tự sửa checkpoint hoặc log.

Ngân sách mặc định **300 giây chờ do retry 429**, dùng chung mọi requests/cohorts
trong một Run M1; không reset theo paper. Đây không phải giới hạn wall time toàn
crawl và không tính pacing thông thường giữa requests độc lập. Chờ retry đi qua
limiter cũ, không có sleep thứ hai. Nếu lần chờ kế tiếp không vừa ngân sách thì
không sleep/không gửi retry, giữ HTTP429 và deadline để Resume an toàn. Nếu sleep
thực tế bị quá ngân sách (ví dụ runtime bị treo), không gửi retry sau khi tỉnh lại.
Attempt/audit có `arxiv_retry_429` ghi ngân sách, thời gian đã chờ và trạng thái.

Benchmark hiện có bao trọn thời gian sleep này trong callback đúng một lần;
không cộng `waited_seconds` thêm vào wall time hoặc cộng report lũy kế vào nhau.
HTTP metrics vẫn đếm từng transport attempt; cache/skip không thành request mới.

**Restart:** không tự khởi động lại Run đã ngắt. Trước deadline được lưu (server
hoặc backoff nội bộ), runtime mới vẫn bị chặn trước Benchmark/HTTP, kể cả đang bật
retry. Hết deadline, A chủ động bấm Run cùng cohort/root; Storage Resume dữ liệu
chưa xong và skip source/history tốt. Đó là Run mới với ngân sách mới, không phải
vòng retry tự động vô hạn. Log cũ thiếu trường mới vẫn đọc được. Bản vá không
khôi phục được header đã mất của các lần 429 cũ và không bảo đảm hết deadline
thì server sẽ chấp nhận request.


Guard xác minh fallback theo `arxiv_retry_429.fallback_seconds` đã lưu trong
**từng attempt**, không dùng mặc định hiện tại để tính lại log lịch sử. Vì vậy
cùng log root đọc được cả attempt cũ 120 giây và attempt mới 60 giây, giữ nguyên
deadline/log/checkpoint đã có. Retry-After server hợp lệ vẫn được ưu tiên, kể cả
khi khác fallback của attempt. Log không có các trường retry thử nghiệm vẫn đọc
được như trước; log có backoff nội bộ nhưng thiếu/sai cấu hình fallback hoặc
không khớp deadline bị từ chối để review, không tự sửa hoặc đoán thời gian chờ.
