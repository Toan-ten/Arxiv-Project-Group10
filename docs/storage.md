# Storage + Checkpoint/Resume — giai đoạn 1

Module `src/storage.py` dùng thư viện chuẩn và contract v2.
Chỉ lưu dữ liệu/result được caller cung cấp; không gọi API, không tải source,
không tích hợp Semantic Scholar và không tự chạy toàn bộ assigned ranges.

## Cấu trúc và định danh

```text
<corpus_root>/2502-08235/
  metadata.json
  references.json
  manifest.json
  v1/main.tex
  v1/bib/refs.bib
  v2/...
```

Caller chọn corpus root, ví dụ `CorpusStorage("data/corpus", run_id="pilot-01")`.
Module đọc config assigned ranges hiện tại để kiểm tra cặp MSSV/arXiv ID.
Helper folder key chỉ áp dụng cho paper được giao; reference ID được giữ nguyên,
gồm old-style có slash hoặc giá trị null.

`initialize_paper()` tạo ba JSON theo schema v2, với metadata/reference chưa lấy
ở trạng thái `not_attempted`, không tạo reference giả và không tạo trước `vN/`.
Ngày/history chưa biết vẫn null. Discovery chỉ điền dữ liệu đã có trong result;
ngày v2..vN không được suy từ updated của latest entry.

## Giao diện

| Hàm | Vai trò |
| --- | --- |
| `initialize_paper(mssv, arxiv_id)` | Khởi tạo hoặc đọc lại paper; giữ nguyên checkpoint có sẵn |
| `record_discovery(result)` | Nhận result của parser/client, lưu provenance và danh sách version resolved |
| `read_metadata/read_references/read_manifest(mssv, arxiv_id)` | Đọc và kiểm tra schema, định danh, contract |
| `save_metadata/save_references(mssv, arxiv_id, value)` | Nhận snapshot đầy đủ theo contract; ghi JSON rồi cập nhật manifest |
| `version_destination(mssv, arxiv_id, version)` | Trả đường dẫn an toàn, không tạo đích |
| `begin_version(mssv, arxiv_id, version)` | Ghi intent in_progress; trả đích chưa tồn tại, hoặc None nếu đã hoàn tất hợp lệ |
| `record_source_result(result)` | Nhận result downloader/extractor, kiểm tra file và commit trạng thái |
| `should_skip_version(mssv, arxiv_id, version)` | True chỉ khi record hoàn tất và source còn khớp checksum |
| `resume(mssv, arxiv_id)` | Phục hồi checkpoint và trả kế hoạch skip/retry/blocked; không tải dữ liệu |

Version argument có dạng `"v1"`; source result giữ `versioned_id="2502.08235v1"`.
`record_source_result()` nhận trực tiếp dictionary của downloader.
Với `extract_source()` offline, caller bổ sung MSSV/arXiv ID/versioned ID
vào result rồi truyền cho Storage; HTTP fields chưa có vẫn null.
Xem ví dụ hoạt động trong `tests/test_storage.py`, dùng archive và HTTP giả lập.

Không đổi danh sách version qua save_metadata; dùng record_discovery để giữ
các version đã biết. Metadata complete cần đủ field, nguồn S2 và ngày version
đã xác minh. Giai đoạn 1 chưa cung cấp các dữ liệu đó từ mạng.
Snapshot lỗi cũng phải giữ các field/ngày đã biết, không ghi đè chúng thành null.

`provenance.arxiv.snapshot` giữ nguyên phản hồi discovery mới nhất. Map tùy chọn
`field_sources` lưu nguồn riêng cho `title`, `authors`, `published`: mỗi nguồn gồm
query identifier, thời điểm và snapshot đã cung cấp giá trị. Khi phản hồi mới
thiếu trường hoặc published không hợp lệ, giá trị và nguồn cũ được giữ; dữ liệu
mới được chấp nhận sẽ cập nhật cả giá trị lẫn nguồn. Map có tối đa ba mục, không
lồng lại toàn bộ lịch sử. Checkpoint v2 cũ chưa có map vẫn đọc được; chỉ dùng
snapshot cũ làm bằng chứng khi giá trị khớp, không tự dựng nguồn đã bị mất.

References dùng list, không lọc/dedupe theo arXiv ID. Caller lưu snapshot tích lũy:
snapshot sau phải giữ nguyên prefix items đã lưu, chỉ nối thêm; không rút ngắn,
đổi thứ tự hoặc âm thầm sửa item. Refresh/enrichment cần thiết kế riêng ở bước sau.
Complete đòi hỏi count/pagination nhất quán cùng provenance thu thập; placeholder
items=[] không được tự chuyển thành kết quả rỗng complete.

## Ghi atomic và kiểm tra source

JSON tạm được tạo cạnh JSON đích, ghi UTF-8, flush/fsync rồi os.replace.
Nếu lỗi/ngắt trước replace, JSON cũ còn nguyên. File tạm do lượt gọi hiện tại
tạo được dọn trong finally; file tạm còn sót do process chết đột ngột được giữ
và không được đọc như checkpoint chính. Mỗi JSON atomic riêng, không phải
transaction đồng thời cho cả ba file.

Storage không ghi source. Caller dùng parser/downloader/extractor sẵn có.
Trước success/no_tex_source, Storage đối chiếu toàn bộ danh sách file,
suffix .tex/.bib, file thường, bytes và SHA-256. Không chỉnh bytes hoặc relative
paths. Archive checksum/metrics chưa được cung cấp vẫn null.

No-TeX chỉ được chấp nhận từ result có status=no_tex_source, không từ lỗi mạng.
Nếu archive chỉ có .bib, vẫn kiểm tra và giữ file đó. Nếu không có file, không
cần thư mục version; record no_tex_source là bằng chứng âm đã được caller cung cấp.
Storage kiểm tra tính nhất quán của result và file, không tự xác minh lại payload
đã bị downloader dọn hoặc chứng minh lại nội dung của phản hồi mạng.

Các ID/version/path không hợp lệ, traversal, Windows drive/reserved names,
symlink/junction, hardlink và file ngoài .tex/.bib bị từ chối. Source có sẵn
không bị xóa hoặc ghi đè.

## Resume và giới hạn phục hồi

- skip: success/no_tex_source còn hợp lệ, checksum và file list khớp.
- retry: pending, interrupted hoặc lỗi timeout/DNS/connection/429/5xx,
  chỉ khi đích chưa tồn tại. Không có retry loop hoặc request trong Storage.
- blocked: checksum/file thiếu/hỏng, source có sẵn chưa có commit hợp lệ,
  lỗi archive/định dạng/quyền lưu trữ hoặc HTTP/TLS cần kiểm tra.

Resume chuyển in_progress còn sót thành interrupted; nếu chưa có output thì có
thể begin lại. Nếu source đã rename nhưng manifest chưa commit, giữ source,
báo output_exists để audit. Không tự nhận source đó là hoàn tất và không tải đè.
Đích hỏng/mất vẫn báo validation_failed, không làm mới checksum để hợp thức hóa.

Resume đọc lại metadata/references để sửa manifest bị cũ khi crash giữa hai
lần ghi JSON, và tái dựng pending versions từ discovery snapshot đã lưu.
Nếu một JSON bắt buộc bị mất/hỏng, khởi tạo dang dở hoặc sai ownership/contract,
module dừng bằng StorageError/IntegrityError; không tự tái tạo dữ liệu rỗng.
Người dùng phải kiểm tra/khôi phục bản JSON đó trước khi tiếp tục.
Các field trạng thái phải là chuỗi hợp lệ; sai kiểu/giá trị trả StorageError,
không chuyển thành pending. Riêng source_status vẫn cho phép null khi chưa có
kết quả downloader. Resume kiểm tra cả ba JSON trước khi ghi trạng thái gián đoạn;
checkpoint sai trạng thái được giữ nguyên, không tự sửa hoặc ghi đè.

Chỉ dùng một writer. Chưa có khóa giữa process, bảo vệ trước thay đổi filesystem
đồng thời, transaction journal đầy đủ, run manifest/log attempt toàn pipeline,
refresh paper đã complete hoặc tự sửa dữ liệu orphan. Lỗi OSError/KeyboardInterrupt
khi ghi được truyền cho caller; chỉ tiếp tục sau khi kiểm tra checkpoint.

## Ví dụ trạng thái giả lập

Trích rút gọn (manifest thật còn định danh, timestamps, checksums và metrics):

```json
{
  "contract_version": 2,
  "arxiv_id": "2502.08235",
  "mssv": "24120232",
  "status": "partial",
  "metadata_status": "partial",
  "references_status": "not_attempted",
  "versions": {
    "v1": {"status": "success", "source_status": "success"},
    "v2": {"status": "server_error", "source_status": "server_error", "http_status": 503},
    "v3": {"status": "pending", "source_status": null}
  }
}
```

Đây là minh họa từ fixture offline, không phải kết quả crawl Group 10.
Paper vẫn partial vì metadata/references chưa hoàn tất, dù v1 có source hợp lệ.

## Kiểm thử

Từ root repository, dùng Python trong môi trường đã có:

```powershell
python -B -m unittest discover -s tests -p "test_storage.py" -v
python -B -m unittest discover -s tests -v
```

Tests dùng TEMP, archive và HTTP mock. Không gọi mạng, cài package hoặc sinh
corpus thật trong repository. Trước pipeline hoàn chỉnh còn cần CLI/orchestration,
metadata/references S2, lịch revision có provenance, run logging và benchmark đủ M1.
