# Tích hợp Semantic Scholar

Tài liệu này mô tả lớp thư viện nối Semantic Scholar với hợp đồng lưu trữ v2. Notebook M1 luôn chạy giai đoạn này; thiếu siêu dữ liệu hoặc tài liệu tham khảo thì bài báo chưa đạt M1. Các tham số của API mức thấp chỉ phục vụ kiểm thử và phát triển, không tạo quy trình vận hành khác. Xem [hợp đồng dữ liệu](../contracts/source_data_contracts.md).

## Vai trò của S2 trong M1

Các API mức thấp `run_selected()`, `run_batch()` và `run_benchmark()` nhận
`s2_config=None` để hỗ trợ kiểm thử không gọi S2. Notebook M1 luôn truyền cấu
hình S2. Ví dụ sau minh họa cấu hình thư viện, không phải điểm vào vận hành:

```python
s2_config = {
    "timeout": 15,
    "max_attempts": 1,
    "min_interval": 2.0,  # ví dụ cấu hình; không bảo đảm phù hợp mọi quota
    "reference_limit": 100,
    "max_pages": 10,
    "max_response_bytes": 10_000_000,
}
# summary = run_selected(pairs, corpus_root, versions=("v1",), s2_config=s2_config)
```

Timeout/attempts S2 độc lập với arXiv. Mặc định S2 là timeout 30 giây, attempts 1,
interval 1 giây, limit 100, max_pages 1000, response cap 10.000.000 bytes.
Cấu hình chỉ nhận các khóa trên; API key đọc từ SEMANTIC_SCHOLAR_API_KEY,
không được đưa vào config/log/report. Gọi Pipeline ngoài fake HTTP sẽ dùng mạng thật.

Một limiter S2 được tạo ngoài vòng lặp papers và dùng chung cho metadata/references.
arXiv giữ limiter riêng hiện có. Chỉ một process/writer; limiter và cooldown không
bền vững qua restart và không phối hợp các máy/runtime độc lập.

## Luồng xử lý và metadata

Sau phần arXiv của mỗi publication, Pipeline gọi metadata rồi references trước
paper_finished. Mỗi publication dùng một metadata batch một ID; không gọi theo
version. Không chạy S2 sau discovery not_found, policy stop, checkpoint sai hoặc
source bị blocked cần audit. Discovery unresolved không bị suy thành v1.

Integration kiểm tra ownership cùng cả hai cache trước khi Storage.resume có thể
ghi phục hồi. Identity metadata luôn dùng policy strict externalIds.ArXiv.
Cache hỏng bị blocked, không gọi API để thay thế checkpoint lỗi.

Metadata giữ MSSV, ID, versions, ngày và provenance arXiv. Title/authors tốt của
arXiv được giữ khi S2 thiếu hoặc mâu thuẫn; chỉ điền fields còn thiếu. Authors
chỉ được điền khi có đủ tên; venue dùng giá trị S2 thực có. Full envelope lưu trong
provenance.semantic_scholar cùng provider, query_identifier và retrieved_at.
Không dùng year/publicationDate làm ngày nộp/revision.

S2 found được cache riêng qua provenance, kể cả metadata M1 còn partial do thiếu
ngày revision. Stage complete chỉ khi đủ điều kiện Storage v2; source và S2 xong
không bảo đảm publication complete. S2 not_found không thay discovery found.

## Tài liệu tham khảo và checkpoint

Snapshot đi qua Storage.save_references; giữ items, raw, thứ tự và duplicates,
kể cả citedPaper null hoặc không có arXiv ID. Query phải khớp publication.
Parent s2_paper_id vẫn null để tương thích resume hiện tại.

Complete hợp lệ được skip, không cập nhật lại timestamp. Partial/failed/interrupted
có continuation hợp lệ dùng resume_from: client đọc lại từ offset 0 và đối chiếu
prefix, không nối hai lượt hoặc tiết kiệm request bằng offset. Prefix khác, raw
không khớp normalization hoặc continuation hỏng bị blocked. Không reset checkpoint.

Metadata và references mới có intent in_progress qua API Storage; resume phục hồi
thành interrupted. Khi references đã có snapshot partial hoặc not_found, giữ nguyên snapshot đó
trong lúc request chạy; chỉ lưu kết quả tương thích sau khi client trả về.
Không đổi bản partial tốt thành intent và không ghi đè bằng tập items ngắn hơn.
Mỗi JSON atomic riêng; Storage đồng bộ lại manifest sau một commit JSON bị ngắt.

Chưa có checkpoint từng trang. Hard kill giữa pagination giữ bản đã lưu, nhưng
không giữ các trang mới của lời gọi chưa trả về. max_pages là số trang của lượt
đọc lại từ đầu; cap thấp cố định có thể làm resume không tiến thêm. Snapshot không
có continuation an toàn cần audit. coverage.complete chỉ xác nhận pagination S2,
không xác nhận mọi reference trong LaTeX đã được S2 nhận diện.

## Dừng, logs và metrics

HTTP 403/429 cuối cùng ở metadata hoặc error.http_status của references làm
stopped=true, stop_reason ghi stage và HTTP status. Pipeline thử ghi kết quả trước
khi dừng; nếu validation/save thất bại vẫn dừng, checkpoint_saved=false, không gửi
request tiếp theo. Lỗi I/O khác vẫn dùng PipelineStopped hiện có.

429 trung gian được retry theo cấu hình của caller. Sổ tay Colab M1 đặt
max_attempts=8, max_retry_wait=600 giây cho mỗi request, backoff_base=3 giây,
backoff_cap=60 giây; Retry-After dài hơn ngân sách không bị cắt ngắn để gửi sớm.
Nếu hết ngân sách mà vẫn nhận 429, pipeline ghi attempts/ngân sách chờ vào
stop_reason, lưu checkpoint và dừng lô. Không khởi chạy lại request vô hạn.
Smoke cần attempts=1 ở cả arXiv và S2.

Summary thêm s2_metadata/s2_references với action, status, http_status, attempts,
error. Action queried/resumed không có nghĩa dữ liệu complete. Metadata attempts
là attempts của request batch; references attempts khi lỗi chỉ thuộc trang lỗi,
null khi thành công, không phải tổng HTTP counts.

Giữ đúng hai callback paper_started/paper_finished. Batch lưu summary S2 vào attempt;
run definition lưu config S2 không có secrets. Batch cũ không có S2 vẫn đọc được;
đổi enabled/config cần batch_id mới, không tự nâng một batch arXiv-only khi resume.

Benchmark truyền cấu hình, phân loại stage/error S2 và kiểm tra evidence S2 cho
batch bật S2. Wall time, paper time và resource sampling bao gồm S2 trong invocation;
Discovery timing vẫn chỉ đo arXiv. Chưa thêm timing riêng cho S2 hoặc HTTP counts.
Aggregation giữ lịch sử lỗi, dùng Storage/checksum để tính coverage cuối một lần;
references chưa complete vẫn là unknown, không phải zero.

## Kiểm thử ngoại tuyến

```powershell
.\venv\Scripts\python.exe -B -W always::ResourceWarning -m unittest discover -s tests -p test_s2_integration.py -v
.\venv\Scripts\python.exe -B -W always::ResourceWarning -m unittest discover -s tests -v
```

Kiểm tra trực tiếp cần được giới hạn rõ về ID, phiên bản, số trang, số lần thử,
giãn cách yêu cầu và thư mục đầu ra. Kiểm thử ngoại tuyến không chạy bộ mẫu hoặc
toàn bộ 20.000 ID.
