# Tích hợp Semantic Scholar trong M1

## Luồng vận hành

Notebook gọi `run_m1_s2_member()` từ `src.m1_workflow`. Module này xuất API của `src.m1_phases`. `src.m1_phase_modules.s2_phase` điều phối pha S2; `src.m1_phase_modules.s2_evidence` quản lý manifest và bằng chứng. `src.s2_integration` xác minh và chuyển đổi dữ liệu S2 theo hợp đồng lưu trữ. Xem [kiến trúc M1](../architecture/repository-structure.md).

Pha S2 nhận danh sách ID xác định theo MSSV, độc lập với tiến độ tải mã nguồn arXiv. Mỗi lô có điểm khôi phục S2 riêng. Kiểm toán chỉ hợp nhất bằng chứng S2 vào corpus sau khi xác minh dấu vân tay phạm vi, định danh và tính toàn vẹn. Runtime notebook được quyết định bởi SHA đã ghim, theo [hướng dẫn Colab](colab.md).

Trạng thái S2 `not_found` chỉ mô tả kết quả tra cứu của Semantic Scholar. Trạng thái này không kết luận ID arXiv không tồn tại và không thay đổi kết quả arXiv.

## Truy vấn và cấu hình M1

S2 dùng Semantic Scholar Graph API: truy vấn batch tại `POST /graph/v1/paper/batch` lấy metadata và references; endpoint `/paper/{paper_id}/references` xử lý những bài cần phân trang. Xem [tài liệu API chính thức](https://api.semanticscholar.org/api-docs) để biết trường dữ liệu và giới hạn dịch vụ.

Notebook và hàm tuần tự `run_m1_member()` dùng `reference_strategy="batch_m1"` để truy vấn metadata cùng references. Notebook dùng SHA trong `M1_RUNTIME_COMMIT`; [hướng dẫn Colab](colab.md) nêu cách kiểm tra mã thực thi.

| Tham số | Giá trị trong pha S2 | Phạm vi |
|---|---:|---|
| `timeout` | 30 giây | Mỗi request |
| `max_attempts` | 8 | Gồm request đầu tiên |
| `min_interval` | 1 giây | Giữa các request trong cùng tiến trình |
| Số ID trong request batch M1 | Tối đa 100 ID | Metadata và references trong cùng phản hồi |
| `reference_limit` | 100 | Mỗi trang references |
| `max_pages` | 1.000 | Mỗi bài báo |
| `max_retry_wait` | 600 giây | Ngân sách chờ thử lại |
| `backoff_base` / `backoff_cap` | 3 / 60 giây | Thời gian chờ thử lại |

Pha M1 xác minh số phần tử, thứ tự và ID arXiv của phản hồi batch trước khi ghi bằng chứng. Các bài không đủ bằng chứng references được phân trang riêng. Điểm khôi phục lưu trạng thái từng ID; cấu hình và số request được ghi trong quan sát phiên.

Notebook yêu cầu Colab Secret `SEMANTIC_SCHOLAR_API_KEY` trước khi bắt đầu M1 và gửi khóa qua header `x-api-key`. Nếu thiếu khóa hoặc không đọc được Secret, pha S2 dừng trước khi tạo checkpoint hay gửi request. M1 không gửi request ẩn danh. Không ghi khóa vào mã nguồn notebook, điểm khôi phục hoặc báo cáo.

Trong mã hiện hành, một limiter được dùng suốt phiên S2, xuyên cohort và các lượt tự tiếp tục. M1 có key nên áp dụng tối thiểu một giây sau phản hồi và giữ cooldown của nhà cung cấp; không áp thêm cửa sổ dành cho request không xác thực. Client cấp thấp vẫn mặc định giữ giới hạn 100 request/300 giây. Limiter nằm trong bộ nhớ tiến trình; các runtime dùng chung key cần được vận hành tuần tự.

## Batch references phục vụ M1

Luồng notebook là batch → kiểm tra → lưu khi hợp lệ và khớp count; riêng bài chưa xác minh được references chuyển sang endpoint phân trang đến hết trang. Không có lượt gọi xác minh thứ ba sau phân trang. Lỗi định danh và HTTP 403/429 vẫn tuân theo chính sách lỗi, không được biến thành kết quả đủ dữ liệu.

Sau phân trang, `complete` mô tả kết thúc thu thập qua API. Bản ghi `quality.json` bên cạnh evidence ghi riêng `collection_complete`, `pagination_complete`, `metadata_reference_count`, `retrieved_count`, `count_match` và cảnh báo. Bản ghi này liên kết SHA-256 của metadata/references trong checkpoint. Batch thành công không có khái niệm trang, nên `pagination_complete` là null. Khớp count không chứng minh đã bao phủ bibliography trong LaTeX.

Batch bị từ chối lưu `batch_fallback` trong envelope metadata: lý do, sự hiện diện của trường references và phần references gốc liên quan. Đánh giá chất lượng dựa trên bằng chứng đã xác minh, không suy diễn lý do chuyển sang phân trang khi điểm khôi phục không ghi nhận lý do đó. Đã hết trang mà count vẫn lệch thì giữ dữ liệu và cảnh báo, không tải lại khi tiếp tục chỉ vì count lệch.

Lựa chọn `batch_m1` gom tối đa 100 ID chưa có metadata hợp lệ vào một request. Các trường references gồm paperId, corpusId, title, authors, year, venue, publicationVenue, publicationDate, externalIds và url. Batch không thu thập contexts, intents, isInfluential. Bằng chứng ghi `retrieval_strategy="paper_batch"`, `fields_profile="m1"` và `relation_fields_collected=false`; không tạo giá trị thay cho các trường chưa thu thập.

Client xác minh ID arXiv của toàn bộ lô trước khi trả kết quả. References chỉ được công nhận hoàn tất khi trường references là danh sách, mọi mục chuẩn hóa được và số mục bằng referenceCount nguyên không âm. Thiếu danh sách hoặc thiếu/sai số lượng sẽ chuyển sang phân trang. Số lượng khớp chỉ xác nhận độ đầy đủ theo dữ liệu S2 trong phản hồi; không chứng minh đã thu thập mọi tài liệu trong LaTeX.

Chỉ lỗi kích thước được nhận diện mới làm lô tách đôi. HTTP 403/429 không làm tách lô hoặc chuyển sang request phân trang; pha ghi bằng chứng và dừng theo chính sách hiện có. Lỗi định danh không được xử lý bằng cách chia lô để bỏ qua kiểm tra. References đang thu thập dở được tiếp tục qua phân trang, bảo toàn raw và kiểm tra phần đầu đã lưu; batch không ghi đè phần đã lưu. Dữ liệu hoàn tất hợp lệ được tái sử dụng.

`metadata_batch_wall_seconds` gồm thời gian nhận metadata và references trong cùng phản hồi batch. `references_wall_seconds` đo đường phân trang. `metadata_fetch_calls` là số lời gọi hàm fetch của pha, khác với số lượt HTTP sau retry hoặc chia lô. Thời gian toàn pha và số lượt HTTP thực tế là các chỉ số vận hành chính.

## Kiểm tra metadata và references

Chỉ gắn metadata với ID yêu cầu khi `externalIds.ArXiv` khớp ID đó. Phần tử `null` trong response hợp lệ được ghi là `not_found`. Timeout, lỗi HTTP, response sai cấu trúc hoặc thiếu bằng chứng định danh vẫn là lỗi hay trạng thái chưa xác định.

Đường phân trang lấy references đến khi máy chủ xác nhận không còn trang; đường batch M1 xác minh danh sách với `referenceCount`. Corpus giữ nguyên thứ tự và dữ liệu `raw` của mọi mục quan sát được, kể cả mục thiếu `citedPaper` hoặc arXiv ID. Bảng xuất chỉ nhận mục đáp ứng lược đồ. Trường title, authors, ngày hoặc S2 paperId còn thiếu được ghi trong `missing_fields`; không tự tạo dữ liệu.

`publicationDate` của S2 là ngày xuất bản theo nguồn đó. Theo Q&A môn học, có thể dùng trường này cho cột `ref_submission_date`. Báo cáo phải nêu xuất xứ của giá trị; ngày này không chứng minh ngày nộp arXiv.

## Tiếp tục và dừng

Điểm khôi phục lưu metadata, bản chụp references, vị trí tiếp tục, lịch sử lỗi và checksum. Nếu references còn một phần, client đọc lại phần đầu đã lưu trước khi tiếp tục để tránh ghép các trang khác nhau. Điểm khôi phục không hợp lệ hoặc sai checksum được giữ nguyên để kiểm tra.

Khi các lượt thử nội bộ đã hết và gặp HTTP 429 ở metadata hoặc references, notebook có thể tự tiếp tục tối đa một lần mỗi lô và hai lần mỗi phiên. Cơ chế chờ ngẫu nhiên 5–10 phút, tôn trọng `Retry-After` nếu lâu hơn, và chỉ chạy khi điểm khôi phục đã được xác minh, giới hạn lượt chưa hết và còn đủ ngân sách. HTTP 403, lỗi điểm khôi phục hoặc hết ngân sách vẫn làm pha dừng. Nếu môi trường bị ngắt trong lúc chờ, người vận hành cần chạy lại ô S2 bằng cùng MSSV, phạm vi, dấu vân tay và thư mục gốc. Không chạy đồng thời hai tiến trình ghi cho cùng thành viên.

Kiểm toán bị chặn nếu bằng chứng phân trang, định danh hoặc xuất xứ chưa đầy đủ. Xem [mô tả client cấp thấp](semantic_scholar.md), [hợp đồng dữ liệu](../contracts/source_data_contracts.md) và [hướng dẫn Colab](../M1_COLAB_MEMBER_GUIDE.md).

## Bổ sung ngày trước khi xuất M1

`run_m1_audit_member()` bổ sung ngày sau khi hai pha đủ điều kiện đối soát, trước khi xuất bảng. Bước này có thể gọi arXiv API; các bước kiểm tra evidence vẫn chạy cục bộ. Chỉ xét reference có arXiv ID và thiếu cả ngày nộp lẫn ngày xuất bản. Chương trình dùng ngày S2 đã có trước; nếu thiếu thì dùng metadata arXiv cục bộ đã kiểm chứng hoặc gọi `query_arxiv_batch` tối đa 20 ID duy nhất/lô qua limiter arXiv hiện hành.

Theo [tài liệu arXiv](https://info.arxiv.org/help/api/user-manual.html), Atom `published` là ngày nộp đầu tiên; `updated` thuộc lần nộp được truy vấn. Chương trình dùng `published`, chuẩn hóa ngày theo UTC và giữ phản hồi gốc để kiểm chứng, không lấy `updated` hoặc đoán ngày từ ID.

Cache nằm ở `reports/phase_checkpoints/<fingerprint>/reference_dates/`: index trỏ tới bản ghi bất biến theo SHA-256; các ID cùng lô dùng chung phản hồi Atom. Giá trị ngày và danh tính được xác minh lại từ bằng chứng trước xuất. Thành công được dùng lại; dữ liệu thiếu có thời gian chờ 24 giờ, lỗi tạm thời hoặc ID chưa được phản hồi có thời gian chờ 5 phút trước lượt chạy sau. Mỗi lần gọi vẫn có retry hữu hạn và dùng backoff arXiv hiện hành; 403/429 dừng lượt bổ sung hiện tại.

Ngày bổ sung chỉ thay đổi phép chiếu `ref_submission_date`, không sửa raw S2, items hay checksum nguồn. Dataset vẫn có đúng sáu cột. `reference_quality.json` trong thư mục xuất ghi cảnh báo count, số dòng còn thiếu ngày và provenance/cache bằng chứng của ngày bổ sung. Nếu nguồn không cung cấp ngày, giữ null và lý do trong cache. Pha S2 riêng không gọi arXiv để bổ sung ngày.
