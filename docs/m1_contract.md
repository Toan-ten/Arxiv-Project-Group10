# Hợp đồng dữ liệu M1 — phiên bản 2

**Rà soát:** 02/11/2026 · **Phạm vi:** crawler M1, không gồm parsing M2 hay matching M3.

Đặc tả này thay thế phiên bản 1 để chuẩn bị Storage + Checkpoint/Resume.
Các schema, trạng thái lưu trữ, CLI và bước export dưới đây **chưa được triển khai**.
ID, discovery, HTTP client, source downloader và benchmark foundation đã có;
việc hoàn tất các module này không đồng nghĩa đã hoàn tất M1.

## 1. Phạm vi và quy ước ID

- Bốn assigned ranges của Group 10 đều thuộc năm 2025. Module assigned ranges
  chỉ hỗ trợ `2015-01..2099-12`, serial `1..99999`, định dạng `YYMM.NNNNN`.
- Mỗi range cùng một tháng, inclusive hai đầu; không overlap, kể cả chung đầu mút.
  Giữ nguyên mapping MSSV và tổng 20.000 **vị trí ID ứng viên**, không phải số paper hợp lệ.
- `arxiv_id_to_folder_key()` chỉ tạo folder key cho assigned base ID:
  `2508.07872` → `2508-07872`. Không dùng formatter/helper này để validate references.
- References và các giao diện crawler có thể nhận ID lịch sử bốn chữ số hoặc
  old-style `archive/YYMMNNN`. Giữ nguyên ID gốc; ID có hậu tố `vN` dùng khi tải source.
- Config vẫn là `config/group10_id_ranges.json`: đúng bốn member, mỗi member có
  `mssv`, `start_month`, `start_id`, `end_month`, `end_id`. Không thêm field vào config.

## 2. Layout và quy tắc source

```text
<out>/<YYMM-NNNNN>/
  metadata.json
  references.json
  manifest.json
  v1/                 # chỉ tồn tại khi có file .tex/.bib được lưu
  v2/
```

Mỗi version được phát hiện đều có record trong manifest, kể cả khi không có thư mục.

- Chỉ giữ các file thường `.tex`/`.bib`, nguyên bytes và relative paths.
  Không decode rồi ghi lại, flatten đường dẫn hoặc sửa/xóa lệnh LaTeX,
  gồm `figure`, `includegraphics`, `input` và `include`.
- Các file ngoài phạm vi, gồm figure binaries, build artifacts, `.sty`, `.cls`
  và `.bbl`, không được đưa vào corpus khi extraction. Không xóa dữ liệu cũ
  để áp dụng quy tắc này. Archive debug nếu có phải ở ngoài corpus.
- Có thể tạo thư mục paper, nhưng **không tạo sẵn `vN/`** trước khi gọi
  `download_source(mssv, versioned_id, destination)`: đích tồn tại sẽ trả
  `output_exists` mà không tải. Thư mục rỗng không chứng minh version đã hoàn tất.
- Archive chỉ có `.bib` vẫn giữ các file đó và trả `no_tex_source`.
  Không có file được giữ thì không cần tạo thư mục version; record vẫn phải được lưu.

Downloader hiện nhận tar, tar.gz, ZIP, TeX đơn và TeX đơn nén gzip; TeX đơn
được đặt tên `main.tex`. PDF/PostScript nhận diện được hoặc archive hợp lệ
không có TeX trả `no_tex_source`; định dạng chưa nhận diện được trả
`unsupported_source`, không được suy thành không có TeX.
Giới hạn kỹ thuật hiện tại: payload 100 MiB, giải nén 200 MiB, 10.000 entries.
Chưa hỗ trợ archive/gzip lồng nhau, ZIP mã hóa, `.Z`, bz2 hoặc xz.
Extraction từ chối traversal, đường dẫn nguy hiểm, links/special files và đường
dẫn trùng/xung đột chữ hoa-thường. Chưa có resume theo byte, checkpoint hoặc
bảo vệ trước process khác thay đổi thư mục đích. Chi tiết: [source downloader](arxiv_source.md).

## 3. `metadata.json`

Ví dụ khởi tạo theo schema, **không phải kết quả crawl**:

```json
{
  "contract_version": 2,
  "arxiv_id": "2507.16659",
  "mssv": "24120035",
  "status": "not_attempted",
  "title": null,
  "authors": null,
  "submission_date": null,
  "revised_dates": null,
  "venue": null,
  "version_status": "needs_verification",
  "versions": [],
  "missing_fields": [],
  "provenance": {
    "arxiv": null,
    "semantic_scholar": null,
    "version_history": null
  }
}
```

- `arxiv_id`/MSSV là định danh đã biết; các field chưa xác minh cho phép `null`.
  Khi có dữ liệu, authors là list tên string, ngày là chuỗi ISO 8601 có độ chính
  xác đúng nguồn. Không suy ngày đầy đủ từ năm, không bịa tác giả hoặc venue.
- `submission_date` là ngày v1. `revised_dates=null` khi chưa biết đủ lịch sử,
  chỉ dùng `[]` khi xác nhận không có revision. Không nhân bản `updated`
  của latest entry thành ngày của mọi version.
- Discovery hiện trả `published`, `updated`, `latest_version_id`, `version_ids`,
  `version_status` và `missing_fields`. Chỉ nhận danh sách version khi
  `version_status="resolved"`; chưa có hậu tố rõ ràng phải tiếp tục xác minh.
- Mỗi phần tử `versions` có `version`, `versioned_id`, `date` nullable và
  `date_status` (`verified|unknown`). Biết ID version không đồng nghĩa biết ngày.
- `provenance` ghi nguồn, định danh truy vấn, thời điểm thu thập và snapshot
  phản hồi/field đã dùng. Giữ `published`/`updated` gốc trong provenance.
  Metadata entry arXiv không được gọi là metadata riêng từng revision.
- Stage `complete` đòi hỏi metadata M1 đã được thu thập/kiểm tra, gồm nguồn
  Semantic Scholar theo yêu cầu M1. Dữ liệu bắt buộc còn thiếu dùng `partial`
  và `missing_fields`; venue xác minh không có có thể là `null`.
  Stage chưa gọi không được đánh dấu complete bằng cách ghi toàn `null`.

## 4. `references.json`: toàn bộ references trung gian

Phiên bản 2 dùng **một file `references.json` làm bản đầy đủ**.
Không lọc mất reference thiếu arXiv ID, metadata hoặc S2 ID; không cần một
`references_all.json` tùy chọn để cứu phần dữ liệu bị loại.
Semantic Scholar và bước thu references chưa được triển khai trong lượt này.

```json
{
  "contract_version": 2,
  "arxiv_id": "2507.16659",
  "status": "not_attempted",
  "provenance": {
    "provider": "semantic_scholar",
    "query_identifier": null,
    "s2_paper_id": null,
    "retrieved_at": null
  },
  "coverage": {
    "complete": false,
    "continuation": null,
    "reported_total": null,
    "retrieved_count": 0
  },
  "items": []
}
```

Mỗi item có các field sau; đây chỉ là ví dụ hình dạng record:

```json
{
  "arxiv_id": null,
  "title": null,
  "authors": null,
  "submission_date": null,
  "publication_date": null,
  "year": null,
  "venue": null,
  "s2_paper_id": null,
  "missing_fields": [],
  "raw": {}
}
```

- Dùng list thay vì dictionary theo arXiv ID để giữ được mọi item đã lấy,
  kể cả khi không có định danh. Giữ record nguồn trong `raw`; không tự gộp
  những item chưa chứng minh là cùng một reference.
- `s2_paper_id` là Semantic Scholar `paperId`, không phải `CorpusId`.
  Giữ arXiv ID gốc, gồm dấu slash của old-style; không ép qua folder helper.
- `submission_date` chỉ điền khi biết ngày nộp arXiv. Nếu nguồn chỉ có
  `publicationDate`, lưu ở `publication_date` cùng provenance; chỉ có năm
  thì giữ `year` và để ngày `null`.
- `coverage.complete` chỉ true sau khi kiểm tra mọi trang/phạm vi response.
  `continuation` lưu offset/token theo endpoint thực tế khi triển khai.
  `reported_total=null` nếu nguồn không cung cấp, không suy tổng từ một trang.
- `items=[]` với stage chưa chạy/lỗi không có nghĩa paper không có references.
  Chỉ `status="complete"` và `coverage.complete=true` mới xác nhận tập đã thu đủ;
  tập rỗng hợp lệ khi có bằng chứng phản hồi đầy đủ.
- Bảng `references` xuất ở milestone sau là dữ liệu khác: chỉ các item có
  arXiv ID hợp lệ, ánh xạ sang `arxiv_id` (paper đang trích dẫn),
  `ref_arxiv_id`, `ref_title`, `ref_authors`, `ref_submission_date`, `ref_s2_id`.
  Lọc/dedupe khi export phải có quy tắc và thống kê; không ghi đè bản trung gian.

## 5. `manifest.json` và trạng thái

```json
{
  "contract_version": 2,
  "arxiv_id": "2507.16659",
  "mssv": "24120035",
  "run_id": null,
  "status": "not_attempted",
  "discovery_status": "not_attempted",
  "version_status": "needs_verification",
  "metadata_status": "not_attempted",
  "references_status": "not_attempted",
  "versions": {},
  "updated_at": null
}
```

Sau discovery đã xác minh, `versions` có key `v1` đến `vN`.
Mỗi record version có hình dạng sau trước khi bắt đầu xử lý:

```json
{
  "versioned_id": "2507.16659v1",
  "status": "pending",
  "source_status": null,
  "http_status": null,
  "content_type": null,
  "source_url": "https://export.arxiv.org/src/2507.16659v1",
  "archive_bytes": null,
  "archive_sha256": null,
  "files": [],
  "file_checksums": {},
  "kept_bytes": null,
  "extracted_bytes": null,
  "removed_files": null,
  "attempts": 0,
  "run_id": null,
  "completed_at": null,
  "error": null
}
```

`source_status` giữ nguyên kết quả downloader. `files` chứa relative paths,
`file_checksums` ánh xạ path → SHA-256 của bytes đã lưu.
`archive_bytes` lấy từ `downloaded_bytes` khi payload đã tải đầy đủ.
Downloader chưa trả archive checksum, kept/extracted bytes hoặc removed count;
Storage chỉ điền giá trị đã đo/xác minh, nếu chưa có thì `null`, không thay bằng 0.
`error` khi có lỗi lưu stage, loại lỗi và message; không chứa token.

| Cấp | Trạng thái và ý nghĩa |
| --- | --- |
| Paper | `not_attempted`, `in_progress`, `partial`, `failed`, `interrupted`, `not_found`, `complete`, `complete_with_no_tex` |
| Version | `pending`, `in_progress`, `success`, `no_tex_source`, `interrupted`, hoặc trạng thái lỗi bên dưới |
| Metadata/references | `not_attempted`, `in_progress`, `partial`, `complete`, `not_found`, `failed`, `interrupted` |

Giữ các loại lỗi downloader: `timeout`, `dns_error`, `connection_error`,
`tls_error`, `http_error`, `rate_limited`, `server_error`,
`archive_error`, `unsafe_archive`, `unsupported_source`, `invalid_response`,
`size_limit`, `storage_error`, `output_exists`; thêm `validation_failed`
cho kiểm tra lưu trữ. Lỗi mạng, giải nén và định dạng chưa hỗ trợ không phải no-TeX.
`no_tex_source` chỉ là kết quả âm hợp lệ từ downloader, không phải lỗi mạng.
HTTP 404 không tự thành paper `not_found`; trạng thái đó phải do discovery xác nhận.
Metadata/references `not_found` cũng không được đổi thành tập rỗng complete.

Paper chỉ `complete` khi discovery found, danh sách version resolved, mọi version
đã lưu/xác minh thành `success` hoặc `no_tex_source`, metadata và references đều
complete và các JSON hợp lệ. Có ít nhất một version `no_tex_source` thì dùng
`complete_with_no_tex`. Source xong nhưng metadata/references chưa đủ là `partial`.
`failed` dùng khi chưa có phần bắt buộc nào hoàn tất và run không thể tiếp tục;
giữ lỗi cụ thể và phần đã lưu. Run dừng giữa chừng là `interrupted`.
`run_id` liên kết lần cập nhật với run manifest; lịch sử attempts nằm trong log.

## 6. Trình tự lưu và resume dự kiến

1. Ghi intent/stage `in_progress` vào manifest bằng JSON tạm cùng filesystem
   rồi replace atomic. Lưu định danh và danh sách version trước khi xử lý source.
2. Gọi downloader với đích version **chưa tồn tại**. Downloader tự tải/extract
   trong thư mục tạm rồi chuyển file đã kiểm tra sang đích.
3. Đối chiếu ID/version, relative paths, loại file, số file, bytes và checksum.
   Chỉ sau khi xác nhận file đã lưu thành công mới ghi version `success`.
   `no_tex_source` cũng phải có record được lưu; nếu có `.bib` thì xác minh các
   file đó, nếu không có file thì chấp nhận không có thư mục version.
4. Ghi metadata/references bằng file tạm rồi replace sau validation; cập nhật
   manifest sau dữ liệu tương ứng. Không ghi đè dữ liệu tốt bằng phản hồi lỗi.
   Chỉ tính trạng thái paper complete sau khi mọi stage bắt buộc đã hoàn tất.
5. Khi resume, kiểm tra contract/config/ownership và output trước khi skip.
   Chỉ skip stage complete/version thành công đã có record và kiểm tra hợp lệ;
   `in_progress` còn sót sau gián đoạn được coi là interrupted và cần xử lý lại.
6. Đích tồn tại mà thiếu manifest hợp lệ, checksum sai hoặc chỉ có file dở:
   giữ nguyên dữ liệu và báo conflict/validation error để audit, không tự xóa,
   ghi đè hoặc coi `output_exists` là thành công. Không dùng `.part` làm kết quả.
   Stage metadata/references còn thiếu/lỗi vẫn phải được tiếp tục dù source đã xong.

Storage chưa có transaction/recovery code. Các tình huống cần test khi triển khai:
crash trước/sau rename, crash trước manifest commit, version chỉ có BibTeX,
no-TeX không có thư mục, checksum sai và chạy lại không tải lại version tốt.

## 7. CLI, log và đầu vào report dự kiến

CLI `python -m src.crawl` chưa tồn tại. Giao diện dự kiến giữ `--config`,
`--out`, `--limit`, `--ids`, `--resume`, `--dry-run`, `--no-network`.
`--dry-run` không ghi output; `--no-network` dùng fixture; thiết kế exit code
phải phân biệt cấu hình/hệ thống lỗi và run chỉ hoàn tất một phần.

- Log JSONL theo attempt: timestamp, run ID, MSSV, arXiv ID/version, stage,
  status, duration, HTTP status, attempt, bytes và error; không log secrets.
- Run manifest: `contract_version`, run/session IDs, commit, config hash,
  thời điểm, phạm vi paper/version, môi trường, pacing, retry và cache/resume.
- Snapshot cho Book: papers/versions/references metrics, benchmark và summary;
  ghi units, mẫu số, provenance và phạm vi run. Book chỉ đọc snapshot, không gọi API.
- JSON UTF-8, `ensure_ascii=False`; không commit corpus, archive, log hay secrets.

## 8. Benchmark foundation: hiện có và còn thiếu

Giữ nguyên `src/benchmark.py`: hiện có wall time của callable, RAM đầu/cuối/đỉnh,
kích thước corpus cuối, chênh dung lượng filesystem trước/sau, environment và commit.
Các field `*_mb` hiện chia cho 1024², tức MiB. `disk_used_diff_mb` không phải disk peak.

Chưa có RAM trung bình, disk peak, thời gian từng paper/trung bình/discovery
hoặc provenance đầy đủ để nghiệm thu một lượt crawl. `psutil` đo process tree;
fallback Linux chỉ process hiện tại, nền tảng không đo được có thể báo 0.
`status="success"` chỉ nghĩa callable không raise; lỗi dạng dictionary của crawler
phải được pipeline tổng hợp riêng, không biến thành paper crawl thành công.
Entry point hiện chạy dummy crawler; output đó chỉ dùng kiểm thử.

Benchmark M1 cuối phải chạy trên **Colab CPU-only** theo yêu cầu M1,
ghi rõ cold/resumed run, pacing và phạm vi IDs/versions. Lượt kiểm thử offline
không phải benchmark corpus thật. Không mở rộng benchmark trong thay đổi này.
