# Quy trình vận hành M1

## Điểm vào

Notebook gọi ba API pha từ `src.m1_workflow`:

- `run_m1_arxiv_member`: xác minh ID arXiv, xác định mọi phiên bản và thu thập mã nguồn.
- `run_m1_s2_member`: lấy metadata và references Semantic Scholar cho cùng danh sách ID, độc lập với tiến độ arXiv.
- `run_m1_audit_member`: kiểm tra bằng chứng, đối soát S2 với corpus arXiv, hợp nhất dữ liệu hợp lệ và xuất bảng.

`run_m1_member` là wrapper tương thích, chạy lần lượt cả ba pha. Notebook gọi riêng từng pha để có thể chạy lại và tiếp tục từng pha độc lập.

## Phạm vi, điểm khôi phục và ghi dữ liệu

`build_member_selection` tạo phạm vi xác định theo MSSV và `config/group10_id_ranges.json`. Mỗi thành viên có tối đa 50 lô; mỗi lô gồm 100 vị trí ID. Dấu vân tay được lưu trong điểm khôi phục để phát hiện phạm vi khác nhau giữa các pha.

Điểm khôi phục arXiv và S2 nằm trong thư mục riêng theo lô. Điểm khôi phục S2 lưu metadata, references thô, trạng thái phân trang, vị trí tiếp tục, lỗi và checksum. Khóa ghi theo thành viên ngăn hai môi trường cùng sửa một phạm vi. Điểm khôi phục không tương thích hoặc sai checksum được giữ để điều tra; chương trình không tự xóa để khởi tạo lại.

Pha arXiv ghi kết quả xác minh và mã nguồn, nhưng không chứng nhận M1 hoàn tất. Pha S2 cũng không thay đổi kết luận về sự tồn tại của ID arXiv. Kiểm toán chỉ hợp nhất S2 khi dấu vân tay, định danh, xuất xứ, checksum và phân trang hợp lệ. Dữ liệu thiếu hoặc xung đột sẽ chặn xuất, không bị thay thế âm thầm.

## Nguồn dữ liệu và giới hạn xử lý

### arXiv và Kaggle

Pha arXiv chuẩn bị snapshot và index Kaggle khi bộ nhớ đệm hợp lệ hoặc Colab Secret `KAGGLE_API_TOKEN` có sẵn. Nếu thiếu token, snapshot hoặc index hợp lệ, pha tiếp tục bằng API arXiv trực tiếp. SHA-256 snapshot, thời điểm tải và thời điểm cập nhật do nhà cung cấp công bố (nếu có) được lưu riêng. Giá trị `snapshot_time="unknown"` không được suy ra từ thời điểm tải. Index Kaggle chỉ cung cấp ứng viên lịch sử; nó không thay thế kết quả Atom hiện hành.

Mỗi lượt arXiv làm mới discovery và gửi tối đa 20 ID cùng thành viên trong `id_list` của Atom API. Parser chỉ nhận entry khớp chính xác ID đã yêu cầu. ID bị thiếu, phản hồi sai hoặc chưa xác định được sẽ được truy vấn riêng. Nhật ký tiến độ ghi trước và sau mỗi request Atom: dải ID, số ID, HTTP status, lượt thử, thời gian và số entry còn cần xác minh. Pha tải mã nguồn báo tiến độ mỗi 10 bài rồi tổng kết theo lô.

Nếu Kaggle khớp danh sách phiên bản và ngày của v1/latest theo `calendar_day_utc`, snapshot có thể cung cấp ngày các lần sửa đổi. Nếu dữ liệu lệch, pipeline ưu tiên arXiv và ghi bằng chứng sai khác bất biến theo ID/phiên, kèm sidecar SHA-256 và điểm khôi phục tham chiếu đến bằng chứng. Bằng chứng được xác minh trước khi tiếp tục. Dữ liệu lịch sử Kaggle thiếu provenance mới không được xem là bằng chứng hợp lệ. Mỗi phiên bản vẫn được tải riêng từ endpoint có hậu tố phiên bản. Lỗi discovery, lịch sử phiên bản và tải mã nguồn được phân loại riêng; các request tuân thủ giới hạn tốc độ, chính sách thử lại và quy tắc dừng 403/429 của client arXiv. Điểm khôi phục cũ vẫn đọc được và có thể tiếp tục tải mã nguồn; lịch sử Kaggle cũ cần được xác minh lại.

Notebook bật cơ chế tự tiếp tục có giới hạn cho HTTP 429 ở discovery: tối đa một lần mỗi lô và hai lần mỗi phiên, với thời gian chờ ngẫu nhiên 5–10 phút. Cơ chế chỉ tiếp tục khi bản ghi lượt gọi xác nhận đúng ID/pha/trạng thái, điểm khôi phục bài báo hợp lệ, và archive cùng sidecar checksum đã được xác minh. Nếu thao tác làm mới gặp 429 với bài đã được xác nhận trước đó, giữ trạng thái `found` cũ và ghi bằng chứng 429 trong nhật ký lượt gọi. Thời gian chờ tôn trọng `Retry-After` còn hiệu lực nếu dài hơn khoảng ngẫu nhiên; chỉ chờ khi ngân sách còn đủ cho khoảng chờ, chính sách thử lại nội bộ 300 giây và biên an toàn. Thiếu bằng chứng, hết giới hạn hoặc thiếu ngân sách thì dừng với lý do trong điểm khôi phục. Không tự thử lại HTTP 403, lỗi mã nguồn, lỗi Kaggle hoặc lỗi kiểm toán. API `run_m1_arxiv_member` mặc định tắt cơ chế này để giữ hành vi của caller hiện tại; notebook chỉ bật bằng cấu hình tường minh. Mỗi lần chạy mã nguồn có quan sát riêng. Quan sát `arxiv_retry_wait` ghi thời gian chờ thực tế và được cộng vào thời gian chạy, thời gian discovery và thời gian mã nguồn của bài gây ra 429.

### Chuẩn bị Kaggle và tiến độ

Chuẩn bị snapshot và dựng index có quan sát pha riêng. Discovery arXiv dùng lô tối đa 20 ID; cấu hình M1 lấy metadata Semantic Scholar theo lô tối đa 50 ID. Đây là giới hạn cấu hình M1, không thay đổi giới hạn chung của các tiện ích API.

Khi tải, giải nén, tính SHA-256 snapshot hoặc quét JSONL để dựng candidate index, Kaggle ghi heartbeat mỗi 30 giây. Heartbeat nêu bước hiện tại, dung lượng đã xử lý và tốc độ; tổng dung lượng, phần trăm và ETA được nêu khi tính được. Khi quét JSONL, SHA-256 được tính trên cùng luồng byte và phải khớp checksum snapshot đã xác minh trước khi công bố index. Kiểm tra `device/inode/size/mtime` trước và sau quét phát hiện thay đổi tệp trong lúc xử lý.

Trong cùng môi trường chạy, hash snapshot vừa xác minh được dùng lại khi kiểm tra index và provenance nếu `device/inode/size/mtime` vẫn khớp. Môi trường chạy mới xác minh lại bộ nhớ đệm từ đầu. Nhờ đó, lượt dựng index mới tránh đọc SHA lặp lại mà vẫn xác minh nội dung trước khi chấp nhận index. Tệp `kaggle_preparation_<session>.json` và các quan sát pha ghi thời gian tải, giải nén, kiểm tra bản ghi JSONL, checksum, công bố bộ nhớ đệm, xác minh snapshot và quét index. Nếu phép tính SHA được gộp vào lượt quét, `checksum_in_scan` đánh dấu việc đó và `scan_seconds` đã bao gồm thời gian tương ứng. Index chỉ được công nhận sau các bước kiểm tra checksum; ghi tiến độ không thay đổi khả năng tiếp tục.

### Semantic Scholar

Pha S2 in trạng thái ban đầu và số lượng metadata/references đã có ở mỗi lô, tiến độ theo nhóm 10 ID, cùng thời gian, số request và kết quả của từng lô metadata. Notebook bật cơ chế tự tiếp tục có giới hạn sau HTTP 429 ở metadata/references: tối đa một lần mỗi lô và hai lần mỗi phiên. Chỉ tiếp tục sau khi điểm khôi phục được xác minh, thời gian chờ 5–10 phút hoàn tất và ngân sách còn đủ; vẫn tôn trọng `Retry-After`. HTTP 403, điểm khôi phục lỗi, hết ngân sách và lỗi khác vẫn làm pha dừng. Nhật ký không chứa nội dung references hoặc thông tin xác thực. Thời gian references, metadata, ghi điểm khôi phục và chờ được lưu trong số liệu pha.

Semantic Scholar được truy vấn bằng Graph API. Corpus giữ mọi mục references quan sát được, kể cả mục không có arXiv ID; bảng xuất chỉ giữ mục đáp ứng lược đồ. Theo Q&A môn học, `ref_submission_date` có thể lấy từ `publicationDate` của Semantic Scholar. Provenance phải ghi nguồn của ngày này; không được diễn giải nó thành ngày nộp arXiv đã xác minh.

Metadata S2 được gom tối đa 50 ID mỗi request và lưu điểm khôi phục riêng theo ID sau khi xác minh định danh. References được tải đủ trang riêng cho từng bài. Nếu phản hồi lô lỗi, lô được chia nhỏ để cô lập ID; trạng thái chưa hoàn tất không được tính là metadata hợp lệ.

### Chính sách `.bbl`

Corpus giữ các tệp `.tex`, `.bib` và `.bbl` theo hợp đồng dữ liệu. Bộ chuyển đổi thử tạo `.bib` dẫn xuất khi nhận diện được cấu trúc an toàn; artifact dẫn xuất nằm ngoài corpus và không thay thế tệp nguồn.

## Phép đo

Mỗi quan sát gắn với phiên, commit môi trường chạy, lô, dấu vân tay và phạm vi tài nguyên. Thời gian tổng là tổng các khoảng đo tuần tự đã ghi; thời gian thử lại và chờ API nằm trong khoảng đo. Khoảng nghỉ giữa các phiên và thời gian cài môi trường bị loại. Tiếp tục không cộng lại khoảng đo cũ; thời gian thực tế đọc điểm khôi phục hoặc kiểm toán vẫn thuộc phiên được đo.

Quan sát S2 ghi số lô, request metadata, số ID yêu cầu và thời gian metadata, references, ghi điểm khôi phục. Khoảng metadata và ghi điểm khôi phục có thể chồng lấn nên không cộng trực tiếp các trường đó để suy ra thời gian chạy. So sánh trước/sau nâng cấp phải dùng cùng phạm vi, trạng thái bắt đầu và cấu hình. Lô 151 cũ chỉ là mốc vận hành lịch sử.

`paper_timings` tính cho các bài đã được discovery tìm thấy. `assigned_paper_timings` và `average_assigned_paper_seconds` dùng toàn bộ ID được giao, kể cả `not_found`; chỉ được đánh dấu hoàn chỉnh khi mọi ID có khoảng đo mã nguồn và S2. Nhờ đó mẫu số không bị thu hẹp vào các bài tìm thấy. Thời gian chờ arXiv được ghi trong `arxiv_retry_wait`, thời gian chờ S2 trong `s2_retry_wait`; cả hai được cộng vào thời gian bài và thời gian chạy của phiên. Thời gian chờ một lô metadata được chia đều cho các ID bị ảnh hưởng. Chỉ thời gian chờ arXiv được cộng vào cấu phần discovery. Không tính khoảng nghỉ thủ công khi không có ô đang chạy, khoảng giữa các phiên hoặc thời gian cài môi trường; không suy thời gian chờ từ timestamp phiên.

`discovery_timings.components` tách thời gian discovery/lịch sử phiên bản arXiv (gồm thời gian chờ tự tiếp tục), chuẩn bị snapshot/index Kaggle và tổng hai phần. `arxiv_retry_wait_seconds` ghi riêng tổng thời gian chờ arXiv đã quan sát. `assigned_paper_timings.s2_retry_wait_seconds` ghi thời gian chờ S2 đã phân bổ cho ID; quan sát `s2_retry_wait` giữ bằng chứng từng lượt chờ và nguyên nhân. Các trường `wall_seconds` và `known_wall_seconds` cũ vẫn được giữ để đọc điểm khôi phục cũ. Thời gian đầu-cuối được tính riêng từ session envelope và gồm các pha tuần tự thực sự đã chạy, như chuẩn bị snapshot, archive, kiểm toán và xuất dữ liệu.

RAM gồm RSS được lấy mẫu trên cây tiến trình crawler và số RAM `used` toàn hệ thống do psutil báo cáo. Số toàn hệ thống bao gồm các tiến trình nền của Colab; không diễn giải nó là RAM chỉ do crawler dùng. Trung bình được tính theo số mẫu, không theo thời gian; đỉnh là mức cao nhất trong các lần lấy mẫu. Thiếu backend hoặc mẫu thì để `null`/incomplete.

Disk là tổng byte logic của tệp thường trong các thư mục gốc không trùng nhau. Chu kỳ lấy mẫu mặc định 30 giây, nên đỉnh đo được có thể bỏ sót biến động ngắn. Kích thước cuối được ghi riêng cho corpus, archive `.tar.gz`, thư mục xuất dữ liệu chính thức và artifact bibliography dẫn xuất. `final_output_storage_bytes` là tổng archive nguồn đã chọn và thư mục xuất chính thức; corpus làm việc và artifact bibliography được báo riêng để tránh đếm trùng. Thư mục artifact bibliography là thư mục gốc dùng chung nên không cộng số đo đó qua các thành viên; mỗi bản tóm tắt thành viên vẫn ghi số đo và phạm vi. Kích thước mã nguồn trước và sau lọc được so sánh trên cùng tập bài có đủ phép đo. Nếu thiếu phép đo cần thiết, trạng thái benchmark là incomplete.

Kết quả tổng hợp nhóm có các trường `arxiv_discovery_and_version_history_wall_seconds`, `kaggle_snapshot_and_index_preparation_wall_seconds` và `discovery_with_kaggle_preparation_wall_seconds`. Các trường này cần đủ quan sát pha của từng thành viên. Điểm khôi phục cũ vẫn đọc được; cấu phần mới để `null` nếu thiếu bằng chứng.

Phép đo cuối cần nhật ký đủ bốn thành viên và 20.000 vị trí ID. `scripts/aggregate_m1_group_benchmark.py` kiểm tra độ bao phủ, dấu vân tay, phiên và kiểm toán trước khi xác nhận tổng hợp. Công cụ cộng kích thước cuối theo loại đầu ra và chỉ công nhận RAM toàn hệ thống khi có đủ quan sát. Điểm khôi phục benchmark cũ vẫn đọc được, nhưng thiếu RAM toàn hệ thống hoặc thời gian theo toàn bộ ID thì không được tự nâng thành phép đo hoàn chỉnh theo quy trình mới. Lượt thử 100 ID chỉ dùng để đánh giá vận hành, không thay thế phép đo toàn nhóm.
