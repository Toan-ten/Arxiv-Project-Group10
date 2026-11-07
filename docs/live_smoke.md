# Controlled live smoke — một publication

Công cụ `scripts/live_smoke.py` mặc định dry run, không gọi API hoặc ghi output.
Chỉ người dùng chủ động thêm `--live` mới chạy mạng. Chuẩn bị công cụ và các tests
không phải bằng chứng production đã hoạt động; không chạy Pilot 10/500 hoặc full crawl.

## Kế hoạch cố định

- Mặc định MSSV `24120010`, arXiv `2508.07872`. Nếu chỉ định ID khác,
  chỉ nhận một cặp có ownership đúng; không nhận list, range hoặc version suffix.
- Source chỉ `v1`, sau khi Discovery xác nhận. Mọi version khác vẫn pending.
- arXiv và S2 đều `max_attempts=1`, không retry hoặc follow redirect.
- Timeout mặc định 15 giây/request, cho phép 1..60 giây; không phải timeout toàn invocation.
- arXiv dùng pacing hiện có ít nhất 3 giây và một connection; S2 dùng một limiter
  chung cho metadata/references, mặc định interval 2 giây, cấu hình tối thiểu 1 giây.
  Interval này không bảo đảm phù hợp mọi quota/key; chọn chậm hơn khi cần.
- References limit cố định 100; max_pages mặc định 3 và chỉ cho phép 1..3.

Giới hạn **tối đa 6 HTTP attempts/invocation**, kể cả retry:
1 Discovery + 1 Source + 1 metadata + tối đa 3 trang references.
Cache, lỗi và policy stop có thể làm số thực tế thấp hơn. Tool không instrument
tổng HTTP count; report giữ actual_http_requests=null, không suy từ cached/skipped.
Giới hạn được kiểm thử bằng fake transport thực tế. Không tự đổi endpoint/ID.
Downloader giữ cap hiện có: payload 100 MiB, giải nén 200 MiB; S2 cap 10.000.000
bytes/response. Không có deadline tổng hoặc resume theo byte.

## PowerShell: chỉ dry run

Từ repository Code:

```powershell
Set-Location 'D:\NM_KHDL\Arxiv-Project-Group10'
.\venv\Scripts\python.exe -B scripts/live_smoke.py
```

Chọn một output mới trên đĩa local, hoàn toàn ngoài cả hai repository:

```powershell
$smokeOutput = Join-Path $env:USERPROFILE ("arxiv-group10-live-smoke\smoke-" + [guid]::NewGuid().ToString("N"))
.\venv\Scripts\python.exe -B scripts/live_smoke.py --output "$smokeOutput"
```

Dry run chỉ in JSON kế hoạch; không tạo thư mục đó, không tạo lock hoặc corpus.
Mặc định output nằm dưới thư mục home/arxiv-group10-live-smoke với tên UTC + UUID.
Không dùng corpus pilot cũ hoặc dataset chính thức. Từ chối output trong Git repo,
ancestor của hai repo, đường dẫn tương đối/UNC/network drive/device hoặc symlink/junction.
Thư mục đã tồn tại bị từ chối nếu chưa chọn resume; không tự xóa hoặc ghi đè.

## Người dùng tự chạy live sau khi review

Có thể lưu key một lần trong `.env` tại root theo
[hướng dẫn cấu hình S2](semantic_scholar.md#api-key-môi-trường-hoặc-env-tại-root).
Các lần chạy sau tự đọc file; biến môi trường đã đặt luôn được ưu tiên.
Nếu biến Windows cũ đang sai, xem hướng dẫn gỡ biến ở liên kết trên.

Kiểm tra đã nạp key mà không hiển thị secret hoặc gọi API:

```powershell
.\venv\Scripts\python.exe -B -c "from src.local_config import get_semantic_scholar_api_key; print('S2 key configured:', bool(get_semantic_scholar_api_key()))"
```

Không bắt buộc có key để dùng client, nhưng truy cập không key có thể bị hạn chế.
Không gửi key qua argument, không chép key vào tài liệu/log. Dry run vẫn không
gọi mạng hoặc ghi corpus; cấu hình key sai định dạng sẽ bị từ chối an toàn.

**Lệnh bên dưới dùng mạng thật; tác vụ chuẩn bị chưa thực hiện lệnh này:**

```powershell
.\venv\Scripts\python.exe -B scripts/live_smoke.py --live --output "$smokeOutput" --mssv 24120010 --arxiv-id 2508.07872 --timeout 15 --s2-interval 2 --max-pages 3
```

Script chạy Batch/Pipeline hiện có đúng một lần, tuần tự; không tự lặp lại.
Một lock độc quyền trong TEMP ngăn hai invocation của tool chạy đồng thời kể cả
khác output. Lock còn sau hard kill cần kiểm tra process và xử lý thủ công;
tool không tự xóa lock cũ. Không đồng thời chạy crawler khác/runtime khác.

403/429 cuối cùng được ghi nhận rồi dừng; lỗi lưu checkpoint không cho phép request
tiếp theo. Công cụ chặn live resume nếu lịch sử có policy stop 403/429: kiểm tra
quota/cooldown và review phương án tiếp theo, không đổi key/endpoint/ID để né.

## Output và kiểm chứng

```text
<smokeOutput>/
  smoke.json                 marker identity/config của tool
  corpus/2508-07872/          Storage contract v2, source chỉ v1 nếu có TeX
  logs/live-smoke/            Batch definition + durable attempts
  reports/<uuid>.json         report riêng, không ghi đè report cũ
```

Report đọc lại logs và Storage/checksum qua Benchmark Adapter, không chỉ tin
summary Pipeline. Kiểm tra MSSV/ID, schema, provenance/query S2, source/checksum,
manifest đồng bộ, coverage/pagination và publication complete. snapshot_http_status
thuộc snapshot đang lưu; không có nghĩa đã gửi request mới trong invocation resume.

- execution_status/batch_status: hàm và batch đã kết thúc thế nào.
- smoke_status: verified/partial/stopped/failed cho mục tiêu smoke giới hạn.
- verification.integrity_status: tính hợp lệ của checkpoint/source.
- verification.reference_coverage_verified: pagination complete, không có mâu thuẫn
  với referenceCount trong metadata S2 nếu count đó được cung cấp.
- verification.publication_complete_verified: complete đã kiểm chứng theo phạm vi này.

Nếu metadata count mâu thuẫn với snapshot references complete, tool báo partial và
chặn live resume để audit; không tự sửa snapshot/contract. aggregation bên trong
giữ kết quả kiểm tra Storage gốc; không dùng riêng claim complete của nó để bỏ qua
coverage_issue của tool. S2 có thể thay đổi count theo thời gian, cần xem xét thực tế.
Không gán metadata referenceCount vào coverage.reported_total của endpoint references.

300 items mà còn next là partial, giữ continuation. Không biến timeout/404 thành
zero references; không nới strict identity khi externalIds.ArXiv thiếu.
Source xong, API HTTP 200 hoặc batch finished không đồng nghĩa publication complete.
Nhiều versions có thể còn pending và ngày revision chưa biết dù smoke_status=verified.
No-TeX được báo riêng, không được gọi là tải TeX thành công.

Exit code: 0 dry run hoặc đạt mục tiêu smoke; 2 partial; 3 policy stop; 1 lỗi/invalid;
130 bị ngắt. Report không phải benchmark M1 thực nghiệm trên Colab.

## Kiểm tra cache/resume

Giữ nguyên $smokeOutput và cấu hình lần trước. Kiểm tra đọc trước:

```powershell
.\venv\Scripts\python.exe -B scripts/live_smoke.py --resume --output "$smokeOutput"
```

Sau review, người dùng có thể tự chạy:

```powershell
.\venv\Scripts\python.exe -B scripts/live_smoke.py --live --resume --output "$smokeOutput" --timeout 15 --s2-interval 2 --max-pages 3
```

Source hợp lệ và S2 metadata tốt được skip dù metadata M1 partial. References complete
được skip; partial đọc lại từ offset 0 và đối chiếu prefix, không tiết kiệm request.
Cap 3 trang cố định có thể không tiến thêm khi vượt 300 references: không chạy lặp
để cố hoàn tất; cần kế hoạch khác được review. Không checkpoint từng trang trong
lời gọi đang chạy; hard kill giữ snapshot đã lưu nhưng có thể mất các trang mới.
Metadata/manifest lệch do interrupted atomic commit có thể được Storage phục hồi;
source hỏng/orphan hoặc checkpoint không hợp lệ bị chặn, giữ nguyên để audit.

## Tests offline

```powershell
.\venv\Scripts\python.exe -B -W always::ResourceWarning -m unittest discover -s tests -p test_live_smoke.py -v
.\venv\Scripts\python.exe -B -W always::ResourceWarning -m unittest discover -s tests -v
```

Tests chỉ parse cờ live và gọi hàm thực thi nội bộ với HTTP giả; không chạy CLI
--live hoặc production API. Mọi dữ liệu là fixtures trong TEMP.

