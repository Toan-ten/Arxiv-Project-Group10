# Biên bản kiểm chứng Resume — M1-T02

**Thẻ:** `[KHOI_ANH - M1-T02]` · **Ngày chạy:** 10/11/2026 · **Môi trường:** Google Colab CPU-only,
corpus trên Google Drive (`/content/drive/MyDrive/arxiv_corpus`).
**Trình bày cho:** Nguyễn Văn Toàn (ký duyệt Full Crawl `[ALL - M1-T04]`).
**Công cụ:** `scripts/live_smoke.py` trên nhánh `feat/m1-smoke-resume` (commit `7412c25`),
notebook `notebooks/colab_m1_runner.ipynb`.

**Lượt chạy lại có đo lường (cùng ngày, `--benchmark`, corpus
`/content/drive/MyDrive/arxiv_corpus_bench`)** xác nhận lại toàn bộ chuỗi
interrupt → resume → verify → proof **kèm metrics**: `PROOF_OK: True`, `VERIFY_OK: True`,
wall time 50,7 s + 47,8 s, đỉnh RAM 30,4 MB (xem mục 4.2 và 6).

## 1. Kết quả tổng hợp

| Hạng mục | Kết quả |
| --- | --- |
| `proof` (báo cáo nghiệm thu resume) | **PASS** — toàn bộ 11 check đều đạt |
| `verify` (toàn vẹn corpus, contract v2) | **PASS** — 0 vi phạm, 0 file tạm |
| Attempt 1 | `interrupted`, 5/10 paper hoàn tất |
| Attempt 2 | `finished`, 10/10 paper |
| Request tiết kiệm khi resume | **12 request** (5 discovery + 7 source) không gửi lại |
| Số đo tài nguyên | wall 50,7 s + 47,8 s; đỉnh RAM 30,4 MB; 23 request cho 10 paper |

Corpus sau hai lượt: 10 paper, tổng **13 version** đã lưu, tất cả ID đều resolve được và đều
có source (không có `not_found`, không có `no_tex_source`).

## 2. Bằng chứng theo từng tiêu chí nghiệm thu của thẻ

| Tiêu chí | Bằng chứng | Kết quả |
| --- | --- | --- |
| Log chứng minh phục hồi, không trùng ID, không ghi đè dữ liệu cũ | Check `no duplicate arXiv ID`; check `existing files not rewritten` so snapshot (SHA-256 + `mtime_ns`) trước và sau resume; check `same selection in both attempts` | PASS |
| `pilot_10` toàn vẹn: đủ `manifest.json`, `references.json`, `v1`/`v2` chỉ `.tex`/`.bib` | `verify` (exit 0): đủ ba JSON, `contract_version=2`, ownership khớp, mỗi `vN/` chỉ có `.tex`/`.bib`, SHA-256 khớp `manifest.file_checksums` | PASS |
| Notebook Colab gắn Drive `/content/drive/MyDrive/arxiv_corpus` | Nhánh `colab_m1_runner.ipynb` bước 2; corpus và log nằm trên Drive, sống sót qua phiên Colab | PASS |
| Chạy `live_smoke` với `pilot_10`, tách `.tex`/`.bib`, loại ảnh | 13/13 version `status=success`; `verify` không thấy file ngoài `.tex`/`.bib` trong `vN/` | PASS |
| Ngắt nhân tạo khi đang chạy dở ở bài 5 | Attempt 1 `status=interrupted`, paper 1-5 `processed`, paper 6-10 `pending` | PASS |
| Resume bỏ qua 5 bài đã crawl, tiếp tục từ bài 6 theo `manifest.json` | Attempt 2: paper 1-5 `discovery=cached` và mọi version `source=skipped`; paper 6-10 `discovery=queried` + `downloaded` | PASS |
| Không có `.tmp` rác hay bản ghi hỏng khi bị ngắt | Check `no leftover temp files` = 0; quét cả corpus và log | PASS |
| Báo cáo cho Toàn ký duyệt | Tệp này | — |

## 3. Diễn biến hai lượt

**Attempt 1 — ngắt sau bài 5**

| # | arXiv ID | discovery | source |
| --- | --- | --- | --- |
| 1 | 2508.07872 | queried | v1 downloaded, v2 downloaded |
| 2 | 2508.12871 | queried | v1 downloaded |
| 3 | 2507.16659 | queried | v1 downloaded, v2 downloaded |
| 4 | 2507.21658 | queried | v1 downloaded |
| 5 | 2502.08235 | queried | v1 downloaded |
| 6-10 | 2502.13234, 2501.02642, 2501.07641, 2508.10371, 2507.19158 | — | pending |

12 request (5 discovery + 7 source). Ngắt xảy ra **sau khi snapshot của bài 5 đã bền**, đúng
đường phục hồi của Ctrl+C.

**Attempt 2 — resume**

| # | arXiv ID | discovery | source |
| --- | --- | --- | --- |
| 1-5 | như trên | **cached** | **toàn bộ skipped** |
| 6 | 2502.13234 | queried | v1 downloaded |
| 7 | 2501.02642 | queried | v1 downloaded |
| 8 | 2501.07641 | queried | v1 downloaded, v2 downloaded |
| 9 | 2508.10371 | queried | v1 downloaded |
| 10 | 2507.19158 | queried | v1 downloaded |

11 request. Năm bài đầu không tốn request nào; dữ liệu của chúng được đối chiếu lại bằng
SHA-256 và giữ nguyên byte.

## 4. Điểm cần Toàn quyết định trước khi duyệt Full Crawl

### 4.1. Mọi paper đang `partial`, không phải `complete`

Cả 10 paper đều `paper=partial`. Nguyên nhân: lượt chạy này chạy **arXiv-only**; stage
Semantic Scholar để `not_attempted`, nên `references.json` mới là khung rỗng và paper không
thể `complete` theo contract v2.

Điều này **không** ảnh hưởng kết luận về resume (checkpoint theo version nằm ở nguồn arXiv),
nhưng có nghĩa là **điều kiện tiên quyết "khóa Semantic Scholar hợp lệ để tránh 429" trên thẻ
chưa được kiểm chứng**. Đề nghị chọn một trong hai:

1. Chấp nhận S2 là hạng mục riêng, kiểm chứng ở `[TOAN - M1-T05]` trước khi Full Crawl; hoặc
2. Chạy thêm một lượt `--s2` với batch id mới (`m1-smoke-pilot-10-s2`) để xác nhận cả metadata
   và references trên cùng 10 bài.

### 4.2. Full Crawl 20.000 ID cần ~55 giờ, không thể xong trong một phiên Colab

Số đo thật (10/11/2026, `--benchmark`, Colab CPU-only):

| Lượt | Request | Paper | Wall time | Đỉnh RAM |
| --- | --- | --- | --- | --- |
| 1 (interrupted) | 12 | 5 | 50,7 s | 30,4 MB |
| 2 (resume) | 11 | 5 | 47,8 s | 30,2 MB |
| **Tổng** | **23** | **10** | **98,5 s** | **30,4 MB** |

Chia ra: **4,28 s/request**, **9,85 s/paper**, 2,3 request/paper. Pacing 3 s/request của client
chiếm 69 s trên 98,5 s; phần còn lại ~15 s là overhead khởi động mỗi invocation.
Ngoại suy tuyến tính cho 20.000 ID:

| | Giá trị |
| --- | --- |
| Request ước tính | ~46.000 |
| Sàn pacing (3 s/request) | 38,3 giờ |
| **Theo tốc độ đo được (4,28 s/request)** | **~54,7 giờ** |
| Số phiên Colab 12 giờ | **~4,6 → cần 5-6 phiên** |

Hai kết luận dùng được ngay:

- **Chi phí của một lần ngắt chỉ là overhead khởi động một invocation (~15 s)**, không phải
  crawl lại 5 bài (12 request). Resume gần như miễn phí về thời gian.
- **Full Crawl bắt buộc chia phiên và dựa vào checkpoint/resume** — đúng cơ chế đã kiểm chứng
  ở đây. Một phiên Colab không đủ cho 55 giờ.

Cần chốt trước khi chạy:

- chiến lược chia phiên (mỗi phiên một `batch_id` theo dải ID, hay một batch duy nhất
  `resume=True` nhiều lần). **Lưu ý:** mỗi lần resume duyệt lại toàn bộ selection để kiểm tra
  checkpoint/checksum, nên batch càng lớn thì overhead kiểm tra đầu phiên càng tăng; chia theo
  dải ID giữ chi phí này nhỏ và cố định;
- có giảm pacing hay không (giảm xuống 1 giây sẽ rủi ro 429/403 và vi phạm hướng dẫn của arXiv);
- `--versions` cho Full Crawl: lấy tất cả version (như pilot, trung bình 1,3 version/paper) hay
  chỉ `v1` để giảm khoảng 30% request → ~38 giờ.

RAM đỉnh 30 MB cho thấy bộ nhớ không phải ràng buộc ở quy mô này; ràng buộc thật là thời gian
và (có thể) dung lượng Drive.

### 4.3. Có số liệu benchmark, nhưng chưa đối chiếu được với tên tệp mà Toàn/Anh chờ

Lượt chạy này **đã** ghi metrics: mỗi invocation tạo một experiment riêng với
`report.json` + `resources.json` (schema `metrics_v2`, RAM lấy mẫu bằng `psutil`, xem
mục 6).

Tuy vậy thẻ `[ANH - M1-T03]` và `[ANH - M1-T06]` nhắc tới `benchmark_summary.json` và
`http_metrics.json` — hiện **chưa có** tệp với đúng hai tên đó. Cần Anh chốt: dùng
`report.json`/`resources.json` của `benchmark_runner` hay cần thêm bước export sang hai tên kia.
Nếu cần, nên làm ở M1-T03 chứ không nhân bản logic đo.

## 5. Phạm vi bằng chứng

Bằng chứng này là **lượt chạy thật** trên arXiv (không phải fake HTTP) cho 10 paper của
`pilot_10`, trên Colab CPU-only. Nó **không** chứng minh:

- khả năng chịu lỗi ở quy mô 20.000 ID (chưa chạy);
- xử lý 429/403 thực tế của S2 (S2 chưa bật);
- tính đúng đắn của metadata/references ở mức publication `complete`;
- độ bền khi Colab kill process **giữa** một paper (lượt này ngắt ở ranh giới paper; trường hợp
  này có `--interrupt-at-start N` và đã có unit test offline, chưa chạy thật);
- dung lượng corpus ở quy mô 20.000 ID (mới có ngoại suy từ pilot).

Đề nghị Toàn xác nhận: (a) chấp nhận S2 tách riêng hay yêu cầu chạy thêm lượt `--s2`;
(b) kế hoạch chia phiên cho Full Crawl theo mục 4.2.

## 6. Bàn giao cho M1-T03 / M1-T06

Mỗi invocation tạo một experiment riêng (không ghi đè):

```text
<report-root>/<experiment-id>/
  intent.json      định nghĩa lượt đo, môi trường, cấu hình sampling
  report.json      execution_status, batch_status, data_coverage, benchmark_metrics
  resources.json   wall time, RAM đầu/cuối/đỉnh/trung bình, dung lượng corpus
```

Hai experiment của lượt chạy này: `66b1d616…` (interrupted, 50,7 s) và `90ae2cf6…`
(finished, 47,8 s), nằm trong `logs/m1-smoke-pilot-10/evidence/` và
`reports/<experiment-id>/` trên Drive.

Lưu ý khi tổng hợp:

- **Không cộng wall time hai experiment thành một con số “thời gian crawl”** — chúng là hai
  invocation độc lập, mỗi lượt có overhead khởi động riêng.
- `batch_status` phản ánh trạng thái Batch; `execution_status="raised"` ở lượt bị ngắt là
  bình thường, kèm `resource_metrics.status="failed"` cho callback bị ngắt.
- `http_requests`/`http_errors` hiện là `null` (`http_metrics_status="not_collected"`): số
  request trong biên bản này được **đếm từ attempt log**, không phải counter của client.
  Muốn có HTTP metrics thật cần bổ sung ở tầng client, không phải suy từ log.
