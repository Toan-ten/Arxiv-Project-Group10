# Benchmark M2 trên Colab CPU-only

Notebook: [`m2_colab_benchmark.ipynb`](../notebooks/m2/m2_colab_benchmark.ipynb).
Đây là phép đo M2 riêng; không phải export campaign đang chạy và không tự chạy benchmark cuối cùng.
Parser giữ nguyên bản sửa `nocite` đã phát hành ở `e6869ff26e36b7122128b7ff165a2bd3da9003b4`.
Notebook ghim SHA release chứa helper mới và đúng fingerprint của parser này; không dùng parser lỗi `072ee384`.

1. Mở notebook trên branch `feat/m2-colab-benchmark`, chọn **Runtime → Change runtime type → CPU**.
2. Sửa một ô Configuration: `STUDENT_ID` dùng đặt output riêng; `START_COHORT`, `END_COHORT`, `INPUT_DRIVE_DIR`, `OUTPUT_DRIVE_DIR`.
3. Chạy Setup: mount Drive, clone vào `/content`, checkout detached đúng release, cài dependencies, kiểm tra môi trường.
4. Chạy Plan (chỉ đọc) rồi Smoke. Smoke chỉ chạy cohort đầu tiên trong phạm vi đã cấu hình. Giữ nguyên phạm vi để Resume cùng campaign.
5. Kiểm tra CSV, audit, Parquet và input manifest trước khi chạy ô Run range. Mỗi cohort xong được checkpoint và xuất báo cáo riêng.
6. Khi runtime mới: chạy lại Configuration, Setup, Plan rồi Run range với cùng input/output/range và release. Cohort đã hoàn tất chỉ được skip sau khi kiểm tra input, checksum, schema và own-version citation evidence.

## Input và Drive dùng chung

Mỗi cohort cần đúng ba file nằm cạnh nhau:

- `cohort_0101_corpus.tar.gz`
- `cohort_0101_corpus.manifest.json`
- `cohort_0101_corpus.tar.gz.sha256`

Manifest phải có assignment, included/omitted IDs và checksum do M1 đã tạo. Không tự tạo manifest để vượt kiểm tra.
Mặc định input: `/content/drive/MyDrive/M1-pipeline-optimization-full/{STUDENT_ID}/archives`.
Output: `/content/drive/MyDrive/ArxivGroup10/{STUDENT_ID}/m2-benchmark`.
Không chọn output là campaign export/recovery cũ. Scratch phải là `/content/...`, tách khỏi input/output/repository.

Với folder được chia sẻ: thêm shortcut vào MyDrive, mount Drive và xác nhận Colab nhìn thấy đúng triplet.
Có thể dùng `/content/drive/Shareddrives/<drive>/<folder>` nếu tài khoản mount được Shared Drive đó.
Helper chỉ đọc input, không sao chép/đổi tên/xóa file trên input Drive; tìm triplet đệ quy dưới INPUT_DRIVE_DIR.
Nếu có hai triplet cùng cohort thì dừng vì mơ hồ, không chọn tùy tiện.

Một cohort: đặt start=end. 50 cohort: ví dụ 101–150. Toàn 200: đặt 1–200 và chọn input parent chứa archive của cả bốn thành viên.
Helper tự xác minh owner theo `config/group10_id_ranges.json`; STUDENT_ID vận hành không gán lại owner của dữ liệu.
Thiếu cohort nào sẽ dừng ở cohort đó và xuất báo cáo partial. Không tải dữ liệu còn thiếu.

## Checkpoint và restart

Campaign `benchmark-<identity hash>` chứa `benchmark.json`, `native/`, `completed/`, `events/`, `native_evidence/`, `reports/` và `LATEST.json`.
Identity gồm range, absolute input path, release SHA, parser fingerprint, helper hash và timeout. Đổi các giá trị này tạo campaign khác, không tái sử dụng cache khác parser.
Mọi version của một bài được native runner xử lý chung để giữ dedup/citation mapping giữa các version. Không cắt publication theo version để tăng pass.
Lỗi parse cô lập được ghi và tiếp tục; input lỗi, invariant/audit lỗi hoặc native timeout retryable thì dừng, giữ diagnostic/checkpoint và không chứng nhận cohort.
Sau sửa nguyên nhân input ở bên ngoài, chỉ Resume với nguồn đã xác minh; không thay input đã tạo completed marker. Nếu thay dữ liệu cần output/campaign mới, không sửa marker.

Hard kill có thể để lại `.export-writer.lock` hoặc `.writer.lock` trong **campaign benchmark mới**. Không tự xóa lock.
Sau khi xác nhận tất cả runtime/process cũ đã dừng, dùng ô Inspect locks (chỉ đọc), rồi ô Archive stale locks với xác nhận thủ công đúng run ID.
Ô đó chỉ rename lock thành file evidence `.reviewed-<uuid>` trong campaign benchmark; không chạm export/recovery/input.
Nếu còn process cũ hoặc chưa rõ, dừng để review. Đồng bộ Drive không phải distributed lock; chỉ một writer cho một campaign.
Nếu runtime chết giữa lúc đo, thời gian thiếu không thể tái tạo. Báo cáo ghi unmeasured intervals và throughput để null; không tính thời gian nghỉ thành runtime.

## Outputs và cách đọc số liệu

Mỗi snapshot trong `reports/<timestamp>-<id>/` có:

- `benchmark_summary.json`, `benchmark_summary.csv` theo cohort.
- `paper_results.csv`: một dòng/ID/version, status, counts, thời gian, audit outcome.
- `m2_errors.csv`, `error_statistics.csv`: diagnostic theo version hoặc cohort; cohort lỗi không có version denominator bị bịa.
- `benchmark_report.md`: nhận xét tiếng Việt, mẫu số, giới hạn và ví dụ lỗi thật.
- `parse_status_distribution.png`, `error_type_distribution.png`, `success_rate_by_cohort.png`, `runtime_by_cohort.png`.
- `report_checksums.json`. `LATEST.json` trỏ snapshot xuất hoàn chỉnh gần nhất. Không chỉnh tay CSV/PNG.

Native Parquet nằm ở `native/<cohort>/run-*/data/{elements,hierarchy,bibtex}/part-00000.parquet`.
Giữ schema gốc; bibtex union theo bài không có cột version. Audit dùng bibliography evidence của từng version, không mượn entry của version khác.
Tổng bảng chỉ cộng unique rows của cohort đã audit. Counts gắn version không cộng làm union totals.

Success version = success / mọi version quan sát; eligible success = success / (success + parse_error).
Failure = (parse_error + input_error + source_unavailable) / version quan sát.
No-TeX = empty_no_tex / version quan sát, là kết quả hợp lệ chứ không phải parser bug.
Paper success chỉ khi mọi version đều success; `papers_all_versions_handled` cho phép empty_no_tex.
Audit outcome, input availability và benchmark completion được báo riêng. Partial không đại diện 200 cohort.

Runtime campaign cộng các khoảng active đã ghi, gồm verify/extract, đọc, parse, ghi và audit; không gồm Setup, nghỉ giữa phiên, vẽ báo cáo hoặc re-audit checkpoint đã xong.
Per-version runtime chỉ là prepare/render worker (có spawn), không phải end-to-end; không gồm cross-version merge, source validation và ghi Parquet. Cached/no-worker để null; xem `runtime_scope`.
Native parser không giữ traceback/exception class cho lỗi đã bắt; helper để null thay vì tự dựng. Lỗi input/audit ngoài parser có traceback thật.
`latex_parse_error` chỉ nói outcome, không kết luận source sai. Missing keys có evidence được phân `citation_mapping_error`; chưa xác định nguyên nhân giữ unknown.
Không tính RAM lifetime của process cha thành RAM toàn pipeline. Notebook này chưa đo RAM/disk mới; không dùng nó thay cho benchmark tài nguyên cuối cùng.

## Kiểm tra local (fixture, không Drive)

```powershell
python -X utf8 -B -m pytest tests/test_m2_colab_benchmark.py tests/test_m2_benchmark_notebook.py -q
```

Chưa có kết quả benchmark Colab thật từ bước chuẩn bị này. Dependencies riêng: `requirements-m2-benchmark.txt`.
