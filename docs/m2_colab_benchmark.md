# Benchmark M2: batch tự động, CSV/JSON tăng dần

Mở [notebook benchmark](../notebooks/m2/m2_colab_benchmark.ipynb) trên branch `feat/m2-colab-benchmark`.
Đây là benchmark M2 độc lập, không sửa export/recovery campaign và không tự tải dữ liệu.
Parser không đổi: source `e6869ff26e36b7122128b7ff165a2bd3da9003b4`, fingerprint
`4e88c7fb0d3463c4c6815b5afff3f64066815068dd4c34db1e92528966d60b70`.
Notebook ghim release helper cụ thể; fingerprint thực tế được kiểm tra khi Setup/Run.

## Cấu hình và chạy

```python
STUDENT_ID = "24120232"  # chỉ đặt output của người vận hành, không đổi owner của paper
START_COHORT = 1
END_COHORT = 200
COHORTS_PER_BATCH = 50
AUTO_CONTINUE = True
INPUT_DRIVE_DIR = "/content/drive/MyDrive/M1-pipeline-optimization-full"
OUTPUT_DRIVE_DIR = f"/content/drive/MyDrive/ArxivGroup10/{STUDENT_ID}/m2-benchmark"
BENCHMARK_CAMPAIGN = ""
```

Chọn CPU-only. Chạy Configuration → Setup (hai ô) → Plan → Smoke → Run batches.
Smoke chỉ xử lý cohort đầu tiên, giữ nguyên range để cùng campaign. Xem audit/Parquet trước Run.
1–200 tự chạy 0001–0050, 0051–0100, 0101–0150, 0151–0200 theo thứ tự.
Chỉ 50 cohort: đặt 101–150, batch size 50. Phạm vi không tròn 50 vẫn chia đúng từ START_COHORT.
AUTO_CONTINUE=False xử lý nhóm chưa hoàn tất kế tiếp rồi dừng; không yêu cầu đổi biến khi để True.
Configuration được đọc lại mỗi lần Run. Đổi batch size/auto không đổi campaign identity.
Không thay đổi parser trong lúc campaign đang chạy.

Input parent phải nhìn thấy archives của tất cả thành viên trong scope, qua MyDrive shortcuts hoặc Shared Drive
được tài khoản mount. Có thể chọn `/content/drive/Shareddrives/<drive>/<folder>`.
Mỗi cohort có đúng một triplet cùng thư mục:

- `cohort_XXXX_corpus.tar.gz`
- `cohort_XXXX_corpus.manifest.json`
- `cohort_XXXX_corpus.tar.gz.sha256`

Plan liệt kê đường dẫn và tất cả cohort thiếu/mơ hồ; kiểm tra sự hiện diện này chưa chứng minh checksum đúng.
Run kiểm tra đủ triplet của toàn phạm vi trước parser; khi xử lý từng cohort mới xác minh archive hash,
manifest, assignment, inventory và source checksum. Không tạo dữ liệu đầu vào, không tải cohort thiếu.
Owner được giữ từ `config/group10_id_ranges.json`, mọi phiên bản một publication xử lý cùng native runner.

## Report bất biến, đọc song song

```text
benchmark-<identity>/
  benchmark.json
  native/<cohort>/run-*/data/{elements,hierarchy,bibtex}/part-00000.parquet
  completed/<cohort>.json
  events/<event>/{started,finished}.json
  native_evidence/<event>.json
  reports/
    batch_0001-0050/
      LATEST.json
      snapshot-<signature>-<id>/
        benchmark_summary.json
        benchmark_summary.csv
        paper_results.csv
        m2_errors.csv
        error_statistics.csv
        report_checksums.json
        READY.json
    batch_0051-0100/...
    aggregate_0001-0200/...
  LATEST.json
```

Sau mỗi batch, helper ghi snapshot, đọc lại checksum, publish READY, cập nhật LATEST và in đường dẫn,
rồi mới chạy batch kế tiếp. Snapshot hoàn tất không sửa lại; partial và complete có snapshot khác nhau.
Folder `.building-*` hoặc snapshot không READY là ghi dở, không dùng làm report hoàn chỉnh.
Nếu nội dung snapshot đã publish bị sửa, validation dừng thay vì ghi đè để che lỗi.
Chỉ LATEST là con trỏ được thay thế. Report có dữ liệu không đổi được tái sử dụng sau kiểm checksum.
Không có PNG, Matplotlib hoặc Markdown report. Thông báo lỗi LaTeX giữ nguyên dạng text trong CSV/JSON.

Để Codex phân tích khi Colab vẫn chạy: sao chép/download **toàn bộ snapshot** của batch trước,
gồm năm file dữ liệu, report_checksums và READY. Gửi đường dẫn bản local cho Codex.
Không chỉnh report trong Drive; không đổi helper/parser của runtime đang chạy theo kết quả phân tích.
Bản sửa parser chỉ áp dụng vào campaign/release đã xác minh sau này.

`benchmark_summary.csv` có dòng từng cohort và dòng ALL_REQUESTED: counts, rates, audit, phạm vi và parser SHAs.
JSON bổ sung input included/omitted IDs, environment, mẫu số và scope đo.
`paper_results.csv` một dòng/ID/version; `m2_errors.csv` giữ diagnostic version hoặc cohort.
`error_statistics.csv` có category/type/count/percentage/denominator; percentage là tỷ lệ trên diagnostic records
trong scope, gồm no_tex_source mang tính thông tin. Cột version_rate_percent có mẫu số version riêng.
Đây là nhóm thông báo quan sát, không tự khẳng định nguyên nhân source hay parser.

Tỷ lệ version = success / version thuộc cohort **audit-verified**; parse_error và empty_no_tex cùng mẫu số này.
Eligible success = success / (success + parse_error). Paper-all-success yêu cầu mọi version success;
paper-any-success chỉ cần một version. JSON nêu cả hai. No-TeX hợp lệ không phải parser bug.
Outcome chưa audit vẫn giữ trong raw counts/CSV nhưng không chứng nhận rates hay đóng góp Parquet totals.
Aggregate tính lại từ cohort/version duy nhất, không cộng phần trăm; native union rows không cộng từ version counts.
Không lấy cohort ngoài batch để tính thời gian, errors hay mẫu số của batch đó.
Complete nghĩa cohort đã audit đầy đủ, không có nghĩa mọi version parse success; partial không đại diện toàn scope.

## Resume và campaign cũ

Runtime mới: Configuration → Setup → Plan → Run. Cohort verified được kiểm input/evidence/schema/citation own-version
trước skip, không parse lại. Report-only không gọi parser, không export lại Parquet, không đọc archive.
Input/audit/invariant lỗi hoặc native timeout retryable: dừng toàn invocation, xuất partial từ evidence hiện có;
không tự chạy batch tiếp. Lỗi parse từng version là kết quả thật, không dựng output cho lỗi.
Interrupted interval chưa ghi finish không suy ra được thời gian; throughput để null khi có khoảng chưa đo.

Campaign cũ `benchmark-e7d87a5409189af18313` có thể dùng lại **nếu** kiểm tra tương thích PASS:

1. Giữ START=101, END=150, INPUT_DRIVE_DIR và OUTPUT_DRIVE_DIR chính xác như lần cũ.
   Input cũ thường là `/content/drive/MyDrive/M1-pipeline-optimization-full/24120232/archives`;
   không đổi sang parent toàn nhóm chỉ vì notebook mới mặc định khác.
2. Đặt BENCHMARK_CAMPAIGN đầy đủ đường dẫn cũ, ví dụ
   `/content/drive/MyDrive/ArxivGroup10/24120232/m2-benchmark/benchmark-e7d87a5409189af18313`.
3. Setup → Plan → Report (khắc phục report cũ lỗi mathtext) → Run.
4. Helper xác minh identity hash, scope/input/timeout, helper blob tại release cũ,
   provenance và ancestry cùng parser fingerprint. Native checkpoint còn phải qua full audit/checksum.
5. `benchmark.json`, completion marker, native Parquet/checkpoint cũ giữ nguyên bytes. SHA tạo output cũ vẫn giữ;
   report ghi riêng reporter release và danh sách parser_shas. Các cohort mới ghi SHA runtime mới,
   nhưng cùng parser implementation fingerprint đã chứng minh. Không gọi đây là cold run một phiên duy nhất.

Không có quyền truy cập campaign Drive trong release này nên chưa thể khẳng định checkpoint thật tương thích.
Bất kỳ kiểm tra nào FAIL: dừng, giữ dữ liệu, lấy benchmark.json/diagnostic để review; không sửa marker cho khớp.
Nếu muốn mở scope cũ 101–150 thành 1–200, phải campaign mới; helper từ chối đổi identity âm thầm.
Campaign cũ vẫn giữ nguyên, không tự import dữ liệu giữa hai scope/campaign.

Chỉ một writer mỗi campaign. Hard kill có thể để lại lock. Xác minh process/runtime cũ đã dừng rồi dùng
inspect/archive lock với xác nhận run ID; không tự xóa lock hoặc checkpoint. Drive không cung cấp distributed lock
mạnh cho nhiều runtime; READY + checksum là kiểm tra publication, không bảo đảm mọi semantics POSIX của Drive.

## Hiệu năng và phạm vi đo

Parser unchanged. Loại bỏ plot/Markdown và chỉ tạo report cuối batch, không sau từng cohort;
không parse lại để xuất CSV. Extraction dùng local `/content` scratch. Native outputs/checkpoints vẫn ghi bền trên Drive:
chưa chuyển chúng sang local rồi copy vì native evidence có absolute paths, cần thiết kế riêng để giữ provenance.
Vẫn có chi phí hash archive, đọc evidence và re-audit; không bỏ checksum để nhanh hơn.
Runtime ghi active verify/extract/read/parse/write/audit, loại Setup/report/re-audit cohort cached/session gaps.
Per-version runtime chỉ prepare/render worker gồm spawn, không phải end-to-end. Benchmark tài nguyên RAM/disk
cuối cùng vẫn cần công cụ đo phù hợp; notebook này không tự bổ sung RAM toàn process tree hay disk peak.
Chưa có phép đo tốc độ Drive/Colab thật cho release này; không suy ra parser nhanh hơn từ giảm thời gian report.

## Kiểm tra offline

```powershell
python -X utf8 -B -m pytest tests/test_m2_colab_benchmark.py tests/test_m2_benchmark_batches.py tests/test_m2_benchmark_notebook.py -q
```

Chỉ fixture local, không truy cập Drive/API và không chạy benchmark corpus thật trong lúc phát hành.
