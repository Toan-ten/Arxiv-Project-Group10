# Quy trình vận hành M1

## Điểm vào

Notebook gọi ba API thu thập và kiểm định từ `src.m1_workflow`; chuẩn hóa và xuất bảng có API riêng:

- `run_m1_arxiv_member`: xác minh ID arXiv, xác định các lần nộp và thu thập mã nguồn.
- `run_m1_s2_member`: lấy metadata và references Semantic Scholar cho cùng danh sách ID, độc lập với tiến độ arXiv.
- `run_m1_validation_member`: kiểm tra bằng chứng, ghi báo cáo theo từng bài; chuẩn hóa và xuất bảng là API riêng.

Kiểm định không gọi API và không hợp nhất dữ liệu S2 vào corpus. Pha này đọc điểm khôi phục arXiv/S2, khôi phục archive vào ổ cục bộ để kiểm tra, rồi ghi báo cáo và tiến độ kiểm định lên Drive. Corpus, archive nguồn và điểm khôi phục arXiv/S2 không bị sửa. Báo cáo được cập nhật sau mỗi lô và dùng để tiếp tục khi phiên bị dừng. Notebook chạy S2 sau pha arXiv trên cùng phạm vi ID; việc lấy S2 không phụ thuộc trạng thái hoàn tất của mã nguồn từng bài. Kiểm định cần archive arXiv hợp lệ. Khi phân trang kết thúc nhưng số lượng tài liệu tham khảo lệch, hệ thống ghi cảnh báo chất lượng và không tự tải lại chỉ vì chênh lệch đó. Khi dừng, bộ lấy mẫu chờ hữu hạn và ghi rõ nếu thiếu mẫu cuối.

`run_m1_member()` lần lượt gọi thu thập arXiv, thu thập S2, kiểm định và chuẩn hóa. Notebook gọi riêng từng bước để người vận hành có thể tiếp tục từ điểm khôi phục hoặc báo cáo tiến độ tương ứng; chuẩn hóa tạo lại bảng từ bằng chứng đã lưu.

`src.m1_workflow` xuất các API từ `src.m1_phases`. Module `m1_phases` điều phối các pha; phần triển khai nằm trong `src.m1_phase_modules` theo [kiến trúc M1](../architecture/repository-structure.md). Notebook là điểm vào vận hành cho thành viên. Hàm `run_m1_member()` gọi cùng quy trình theo thứ tự và dùng `batch_m1` cho S2.

Notebook vận hành dùng SHA trong `M1_RUNTIME_COMMIT`. Xem [cấu hình runtime](../guides/colab.md).

## Diễn giải kết quả các pha

| Lời gọi | Kết quả cần kiểm tra | Phạm vi kết luận |
|---|---|---|
| `run_m1_arxiv_member` | `source_complete_papers`, `cohorts`, `stopped`, `stop_reason` | Tiến độ mã nguồn arXiv; `m1_complete` vẫn là `False` |
| `run_m1_s2_member` | `metadata_complete`, `references_complete`, `cohorts`, `stopped`, `stop_reason` | Tiến độ bằng chứng S2; `m1_complete` vẫn là `False` |
| `run_m1_validation_member` | `status`, `ready_for_m2`, `ready_for_m3`, trạng thái theo bài và báo cáo | Tình trạng bằng chứng của phạm vi ID đã chọn |
| `run_m1_member` | Kết quả các bước và `handoff_status` khi chuỗi chạy tới kiểm định | `validated` khi báo cáo kiểm định không có `issues`; nếu có thì là `partial` |

`stopped = False` chỉ cho biết pha không báo dừng, không thay thế việc kiểm tra độ bao phủ và toàn vẹn. Điều kiện hoàn tất dữ liệu và điều kiện đủ bằng chứng benchmark được đánh giá riêng. Một lô thử hoặc một bản chụp đã đối chiếu không chứng minh toàn bộ 5.000 hay 20.000 vị trí ID đã hoàn tất.

## Phạm vi, điểm khôi phục và ghi dữ liệu

`build_member_selection` tạo phạm vi xác định theo MSSV và `config/group10_id_ranges.json`. Mỗi thành viên có tối đa 50 lô; mỗi lô gồm 100 vị trí ID. Dấu vân tay được lưu trong điểm khôi phục để phát hiện phạm vi khác nhau giữa các pha.

Điểm khôi phục arXiv và S2 nằm trong thư mục riêng theo lô. Điểm khôi phục S2 lưu metadata, references thô, trạng thái phân trang, vị trí tiếp tục, lỗi và checksum. Khóa ghi theo thành viên ngăn hai môi trường cùng sửa một phạm vi. Điểm khôi phục không tương thích hoặc sai checksum được giữ để điều tra; chương trình không tự xóa để khởi tạo lại.

Các đường dẫn chính được tính từ `drive_root` của thành viên:

```text
<drive_root>/
├── reports/
│   ├── phase_checkpoints/<fingerprint>/
│   │   ├── selection.json
│   │   ├── starting_state.json
│   │   ├── session_index.json
│   │   ├── arxiv/cohort_NNNN.json
│   │   └── s2/cohort_NNNN/
│   │       ├── checkpoint.json
│   │       └── papers/<folder_key>/
│   │           ├── metadata.json
│   │           ├── references.json
│   │           └── quality.json
│   ├── phase_locks/<mssv>.lock
│   └── m1_validation_<mssv>_<fingerprint-prefix>.json
└── exports/<fingerprint>/working/
    ├── papers.parquet
    ├── references.parquet
    └── normalization_report.json
```

`NNNN` là số lô có bốn chữ số; `folder_key` do hàm lưu trữ xác định. `selection.json` giữ phạm vi ID; `starting_state.json` ghi nhận trạng thái dữ liệu lúc bắt đầu; `session_index.json` liệt kê các phiên của cùng phạm vi. Trong điểm khôi phục S2, `metadata.json` và `references.json` xuất hiện khi đã có kết quả tương ứng; `quality.json` liên kết checksum của hai tệp và có thể chưa tồn tại trước khi đánh giá chất lượng. Báo cáo kiểm định và báo cáo chuẩn hóa là đầu ra riêng trên Drive. Luồng M1 hiện tại không tra cứu arXiv để bổ sung ngày references trong kiểm định hoặc chuẩn hóa. Trạng thái `not_attempted` không được suy thành dữ liệu rỗng đã hoàn tất. Khóa ghi theo MSSV bao phủ cả phạm vi đầy đủ lẫn phạm vi con của cùng thành viên.

Pha arXiv ghi kết quả xác minh và mã nguồn, nhưng không chứng nhận M1 hoàn tất. Pha S2 cũng không thay đổi kết luận về sự tồn tại của ID arXiv. Kiểm định xác minh riêng điểm khôi phục arXiv, archive nguồn và bằng chứng S2; pha này không thay đổi các dữ liệu đó. Khi kiểm định hoàn tất, chuẩn hóa tạo bảng làm việc từ bài có mã nguồn hợp lệ và references S2 hoàn tất. `ref_submission_date` lấy từ `submission_date` nếu có, nếu không thì lấy `publication_date` trong bằng chứng S2; nếu cả hai đều thiếu, bảng để giá trị `null`. Xuất bảng chính thức là API riêng.

## Nguồn dữ liệu và giới hạn xử lý

### arXiv và Kaggle

Pha arXiv yêu cầu Colab Secret `KAGGLE_API_TOKEN` ngay cả khi snapshot hợp lệ đã có trong bộ nhớ đệm. Pha xác minh snapshot và index Kaggle trước khi bắt đầu discovery; nếu snapshot/index không sẵn sàng hoặc không ghi được bằng chứng chuẩn bị, pha dừng và không chuyển sang đường arXiv-only. SHA-256 snapshot, thời điểm tải và thời điểm cập nhật do nhà cung cấp công bố (nếu có) được lưu riêng. Giá trị `snapshot_time="unknown"` không được suy ra từ thời điểm tải. Index Kaggle chỉ cung cấp ứng viên lịch sử; Atom vẫn xác nhận inventory hiện hành và mã nguồn từng lần nộp vẫn được tải riêng.

Mỗi lượt arXiv cập nhật discovery và gửi tối đa 20 ID cùng thành viên trong `id_list` của Atom API. Parser chỉ nhận entry khớp chính xác ID đã yêu cầu. ID bị thiếu, phản hồi sai hoặc chưa xác định được sẽ được truy vấn riêng. Nhật ký tiến độ ghi trước và sau mỗi request Atom: dải ID, số ID, HTTP status, lượt thử, thời gian và số entry còn cần xác minh. Pha tải mã nguồn báo tiến độ mỗi 10 bài rồi tổng kết theo lô.

Nếu Kaggle khớp danh sách lần nộp và ngày của v1/latest theo `calendar_day_utc`, snapshot có thể cung cấp ngày các lần sửa đổi. Nếu dữ liệu lệch, pipeline ưu tiên arXiv và ghi bằng chứng sai khác bất biến theo ID/lần nộp, kèm sidecar SHA-256 và điểm khôi phục tham chiếu đến bằng chứng. Bằng chứng được xác minh trước khi tiếp tục. Lịch sử Kaggle chỉ được chấp nhận khi có đủ xuất xứ. Mã nguồn của mỗi lần nộp được tải riêng từ endpoint có hậu tố tương ứng. Lỗi discovery, lịch sử và tải mã nguồn được phân loại riêng; các request tuân thủ giới hạn tốc độ, chính sách thử lại và quy tắc dừng 403/429 của client arXiv. Điểm khôi phục có thể được tiếp tục tải mã nguồn sau khi xác minh; lịch sử Kaggle thiếu xuất xứ cần được xác minh lại.

Notebook bật cơ chế tự tiếp tục có giới hạn cho HTTP 429 ở discovery: tối đa một lần mỗi lô và hai lần mỗi phiên, với thời gian chờ ngẫu nhiên 5–10 phút. Cơ chế chỉ tiếp tục khi bản ghi lượt gọi xác nhận đúng ID/pha/trạng thái, điểm khôi phục bài báo hợp lệ, và archive cùng sidecar checksum đã được xác minh. Nếu thao tác làm mới gặp 429 với bài đã được xác nhận trước đó, giữ trạng thái `found` đã xác minh và ghi bằng chứng 429 trong nhật ký lượt gọi. Thời gian chờ tôn trọng `Retry-After` còn hiệu lực nếu dài hơn khoảng ngẫu nhiên; chỉ chờ khi ngân sách còn đủ cho khoảng chờ, chính sách thử lại nội bộ 300 giây và biên an toàn. Thiếu bằng chứng, hết giới hạn hoặc thiếu ngân sách thì dừng với lý do trong điểm khôi phục. Không tự thử lại HTTP 403, lỗi mã nguồn, lỗi Kaggle hoặc lỗi kiểm toán. Notebook và `run_m1_member()` truyền `auto_resume_429=True`; API pha cho phép bên gọi cấu hình tham số này. Mỗi lần chạy mã nguồn có quan sát riêng. Quan sát `arxiv_retry_wait` ghi thời gian chờ thực tế và được cộng vào thời gian chạy, thời gian discovery và thời gian mã nguồn của bài gây ra 429.

### Chuẩn bị Kaggle và tiến độ

Chuẩn bị snapshot và dựng index có quan sát pha riêng. Discovery arXiv dùng lô tối đa 20 ID. Quy trình M1 dùng `batch_m1` để lấy metadata Semantic Scholar cùng references theo lô tối đa 100 ID. Đây là giới hạn cấu hình M1 của notebook và `run_m1_member()`.

Khi tải, giải nén, tính SHA-256 snapshot hoặc quét JSONL để dựng candidate index, Kaggle ghi heartbeat mỗi 30 giây. Heartbeat nêu bước hiện tại, dung lượng đã xử lý và tốc độ; tổng dung lượng, phần trăm và ETA được nêu khi tính được. Khi quét JSONL, SHA-256 được tính trên cùng luồng byte và phải khớp checksum snapshot đã xác minh trước khi công bố index. Kiểm tra `device/inode/size/mtime` trước và sau quét phát hiện thay đổi tệp trong lúc xử lý.

Trong cùng môi trường chạy, hash snapshot vừa xác minh được dùng lại khi kiểm tra index và provenance nếu `device/inode/size/mtime` vẫn khớp. Khi môi trường chạy thay đổi, chương trình xác minh lại bộ nhớ đệm. Trong cùng môi trường, việc dựng index tránh đọc SHA lặp lại mà vẫn xác minh nội dung trước khi chấp nhận index. Tệp `kaggle_preparation_<session>.json` và các quan sát pha ghi thời gian tải, giải nén, kiểm tra bản ghi JSONL, checksum, công bố bộ nhớ đệm, xác minh snapshot và quét index. Nếu phép tính SHA được gộp vào lượt quét, `checksum_in_scan` đánh dấu việc đó và `scan_seconds` đã bao gồm thời gian tương ứng. Index chỉ được công nhận sau các bước kiểm tra checksum; ghi tiến độ không thay đổi khả năng tiếp tục.

### Semantic Scholar

Pha S2 yêu cầu Colab Secret `SEMANTIC_SCHOLAR_API_KEY` và không gửi request ẩn danh. Pha in trạng thái ban đầu và số lượng metadata/references đã có ở mỗi lô, tiến độ theo nhóm 10 ID, cùng thời gian, số request và kết quả của từng lô metadata. Notebook bật cơ chế tự tiếp tục có giới hạn sau HTTP 429 ở metadata/references: tối đa một lần mỗi lô và hai lần mỗi phiên. Chỉ tiếp tục sau khi điểm khôi phục được xác minh, thời gian chờ 5–10 phút hoàn tất và ngân sách còn đủ; vẫn tôn trọng `Retry-After`. HTTP 403, điểm khôi phục lỗi, hết ngân sách và lỗi khác vẫn làm pha dừng. Nhật ký không chứa nội dung references hoặc thông tin xác thực. Thời gian references, metadata, ghi điểm khôi phục và chờ được lưu trong số liệu pha.

Semantic Scholar được truy vấn bằng Graph API. Điểm khôi phục S2 giữ mọi mục references quan sát được, kể cả mục không có arXiv ID; bảng làm việc chỉ giữ mục đáp ứng lược đồ. Khi chuẩn hóa, `ref_submission_date` lấy từ `submission_date` nếu có, nếu không thì lấy `publication_date` trong bằng chứng S2. Giá trị này không chứng minh ngày nộp arXiv; có thể truy nguyên về điểm khôi phục S2.

Notebook và `run_m1_member()` truyền `reference_strategy="batch_m1"`, gom metadata cùng references tối đa 100 ID mỗi request. Client xác minh định danh và chỉ công nhận references hoàn tất khi danh sách khớp `referenceCount`; trường hợp chưa đủ bằng chứng chuyển sang phân trang. Lỗi kích thước và lỗi định danh trong batch được chia nhỏ để cô lập ID; một ID còn lỗi sau truy vấn riêng vẫn được giữ là chưa hợp lệ. HTTP 403/429 dừng theo chính sách hiện hành. References đang thu thập dở tiếp tục qua phân trang để bảo toàn dữ liệu đã lưu. Phạm vi trường dữ liệu và xuất xứ được nêu trong [hướng dẫn S2](../guides/s2_integration.md).

### Chính sách `.bbl`

Corpus giữ các tệp `.tex`, `.bib` và `.bbl` theo hợp đồng dữ liệu. Bộ chuyển đổi thử tạo `.bib` dẫn xuất khi nhận diện được cấu trúc an toàn; artifact dẫn xuất nằm ngoài corpus và không thay thế tệp nguồn.

## Phép đo

Mỗi quan sát gắn với phiên, commit môi trường chạy, lô, dấu vân tay và phạm vi tài nguyên. Thời gian tổng là tổng các khoảng đo tuần tự đã ghi; thời gian thử lại và chờ API nằm trong khoảng đo. Khoảng nghỉ giữa các phiên và thời gian cài môi trường bị loại. Tiếp tục không cộng lại khoảng đo đã ghi; thời gian thực tế đọc điểm khôi phục hoặc kiểm toán vẫn thuộc phiên được đo.

Quan sát S2 ghi số lô, request metadata, số ID yêu cầu và thời gian metadata, references, ghi điểm khôi phục. Khoảng metadata và ghi điểm khôi phục có thể chồng lấn nên không cộng trực tiếp các trường đó để suy ra thời gian chạy. Phép đo hiệu năng phải ghi rõ phạm vi ID, trạng thái dữ liệu khi bắt đầu và cấu hình thực thi.

`paper_timings` tính cho các bài đã được discovery tìm thấy. `assigned_paper_timings` và `average_assigned_paper_seconds` dùng toàn bộ ID được giao, kể cả `not_found`; chỉ được đánh dấu hoàn chỉnh khi mọi ID có khoảng đo mã nguồn và S2. Nhờ đó mẫu số không bị thu hẹp vào các bài tìm thấy. Thời gian chờ arXiv được ghi trong `arxiv_retry_wait`, thời gian chờ S2 trong `s2_retry_wait`; cả hai được cộng vào thời gian bài và thời gian chạy của phiên. Thời gian chờ một lô metadata được chia đều cho các ID bị ảnh hưởng. Chỉ thời gian chờ arXiv được cộng vào cấu phần discovery. Không tính khoảng nghỉ thủ công khi không có ô đang chạy, khoảng giữa các phiên hoặc thời gian cài môi trường; không suy thời gian chờ từ timestamp phiên.

`discovery_timings.components` tách thời gian discovery và lịch sử lần nộp arXiv (gồm thời gian chờ tự tiếp tục), chuẩn bị snapshot/index Kaggle và tổng hai phần. `arxiv_retry_wait_seconds` ghi riêng tổng thời gian chờ arXiv đã quan sát. `assigned_paper_timings.s2_retry_wait_seconds` ghi thời gian chờ S2 đã phân bổ cho ID; quan sát `s2_retry_wait` giữ bằng chứng từng lượt chờ và nguyên nhân. `wall_seconds` và `known_wall_seconds` hỗ trợ đọc điểm khôi phục đã lưu. Thời gian đầu-cuối được tính riêng từ session envelope và gồm các pha tuần tự thực sự đã chạy, như chuẩn bị snapshot, archive, kiểm toán và xuất dữ liệu.

RAM gồm RSS được lấy mẫu trên cây tiến trình crawler và số RAM `used` toàn hệ thống do psutil báo cáo. Số toàn hệ thống bao gồm các tiến trình nền của Colab; không diễn giải nó là RAM chỉ do crawler dùng. Trung bình được tính theo số mẫu, không theo thời gian; đỉnh là mức cao nhất trong các lần lấy mẫu. Thiếu backend hoặc mẫu thì để `null`/incomplete.

Disk là tổng byte logic của tệp thường trong các thư mục gốc không trùng nhau. Chu kỳ lấy mẫu mặc định 30 giây, nên đỉnh đo được có thể bỏ sót biến động ngắn. Kích thước cuối được ghi riêng cho corpus, archive `.tar.gz`, thư mục xuất dữ liệu chính thức và artifact bibliography dẫn xuất. `final_output_storage_bytes` là tổng archive nguồn đã chọn và thư mục xuất chính thức; corpus làm việc và artifact bibliography được báo riêng để tránh đếm trùng. Thư mục artifact bibliography là thư mục gốc dùng chung nên không cộng số đo đó qua các thành viên; mỗi bản tóm tắt thành viên vẫn ghi số đo và phạm vi. Kích thước mã nguồn trước và sau lọc được so sánh trên cùng tập bài có đủ phép đo. Nếu thiếu phép đo cần thiết, trạng thái benchmark là incomplete.

Kết quả tổng hợp nhóm có các trường `arxiv_discovery_and_version_history_wall_seconds`, `kaggle_snapshot_and_index_preparation_wall_seconds` và `discovery_with_kaggle_preparation_wall_seconds`. Các trường này cần đủ quan sát pha của từng thành viên. Khi thiếu bằng chứng ở điểm khôi phục, cấu phần tương ứng là `null`.

Phép đo cuối cần nhật ký đủ bốn thành viên và 20.000 vị trí ID. `scripts/aggregate_m1_group_benchmark.py` kiểm tra độ bao phủ, dấu vân tay, phiên và kiểm toán trước khi xác nhận tổng hợp. Công cụ cộng kích thước cuối theo loại đầu ra và chỉ công nhận RAM toàn hệ thống khi có đủ quan sát. Nếu thiếu RAM toàn hệ thống hoặc thời gian theo toàn bộ ID, phép đo chưa hoàn chỉnh. Lượt thử 100 ID chỉ dùng để đánh giá vận hành, không thay thế phép đo toàn nhóm.
