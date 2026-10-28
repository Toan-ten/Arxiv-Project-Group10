# Hợp đồng dữ liệu M1 — phiên bản 1

**Ngày chốt:** 28/10/2026 · **Phạm vi:** crawler M1, không gồm parsing M2 hay matching M3.

Tài liệu này là nguồn thống nhất cho crawler, test, auditor và Jupyter Book. Mọi thay đổi phải được cả nhóm đồng ý và tăng `contract_version`.

## 1. Phạm vi và quy ước ID

- Chỉ dùng arXiv ID **new-style** `YYMM.NNNNN` (từ 2007-04). ID cũ `archive/YYMMNNN` chỉ xuất hiện trong references, không dùng làm ID bài được giao.
- Mỗi range là **một tháng**, inclusive hai đầu: `end_id - start_id + 1`. Range cross-month là lỗi config ở M1, không tự suy diễn.
- Sequence luôn pad 5 chữ số: `7872` → `2508.07872`; `2642` → `2501.02642`.
- Folder key: thay dấu chấm bằng gạch ngang: `2508.07872` → `2508-07872`.

Config tối thiểu:

```yaml
ranges:
  - student_id: "24120035"   # optional, dùng để đối chiếu phân công
    start_month: "2025-07"
    start_id: 16659
    end_month: "2025-07"
    end_id: 21658
```

## 2. CLI tối thiểu

```bash
python -m src.crawl --config config/id_ranges.yaml --out data/corpus --limit 10 --resume
```

| Flag | Ý nghĩa |
|---|---|
| `--config` | File range (bắt buộc) |
| `--out` | Thư mục corpus (bắt buộc) |
| `--limit N` | Chỉ xử lý N paper đầu, dùng cho smoke test/pilot |
| `--ids ID...` | Chỉ chạy các ID cụ thể, dùng debug |
| `--resume` | Bỏ qua phần đã hoàn tất; không ghi đè bản tốt |
| `--dry-run` | Chỉ in số range/ID dự kiến, không ghi output |
| `--no-network` | Chạy bằng fixture, dùng cho test/CI |

Exit code: `0` khi mọi paper được xử lý và có trạng thái cuối; `1` khi lỗi config/hệ thống không thể tiếp tục. Lỗi từng paper không làm hỏng cả run.

## 3. Layout output

```text
<out>/<YYMM-NNNNN>/
  metadata.json
  references.json
  references_all.json     # optional: cả reference không có arXiv ID
  manifest.json
  v1/
  v2/
```

- Luôn tạo `vN/` cho mọi version, kể cả version không có TeX (để trống).
- Chỉ giữ file text của source: `.tex`, `.bib`, `.bbl`, `.sty`, `.cls`, `.bst`, `.clo`, `.def`, `.cfg`, `.ldf`, `.fd`, `.dtx`, `.ins` và file text khác không phải figure/build artifact.
- Xóa figure/media (`.png`, `.jpg`, `.jpeg`, `.pdf`, `.eps`, `.ps`, `.svg`, `.gif`, `.tif`, `.tiff`, `.bmp`, video/audio) và build artifact (`.aux`, `.log`, `.out`, `.toc`, `.synctex.gz`, `.fls`, `.fdb_latexmk`).
- Không sửa nội dung `.tex`; giữ nguyên `figure`/`includegraphics`. Không giữ archive trong corpus (nếu cần debug, để ngoài `<out>`).
- Không giữ đường dẫn tuyệt đối hoặc `..`; giữ relative path bên trong archive.

## 4. `metadata.json`

```json
{
  "arxiv_id": "2507.16659",
  "title": "string",
  "authors": ["string"],
  "submission_date": "2025-07-21T12:34:56Z",
  "revised_dates": ["2025-07-25T08:00:00Z"],
  "venue": null,
  "versions": [
    {"version": "v1", "date": "2025-07-21T12:34:56Z", "source_type": "D"}
  ]
}
```

- Bắt buộc theo đề: `title`, `authors`, `submission_date`, `revised_dates`, `venue` (null nếu không có).
- `submission_date` là ngày v1; `revised_dates` là v2..vN theo thứ tự, rỗng nếu chỉ có v1.
- Date dùng ISO 8601 UTC. `authors` là list tên string, không phải object.
- `versions` là field bổ sung để audit; không thay thế hai field bắt buộc ở trên.

## 5. `references.json`

```json
{
  "2507-12345": {
    "title": "string",
    "authors": ["string"],
    "submission_date": "2025-07-01T00:00:00Z",
    "s2_paper_id": "string",
    "arxiv_id": "2507.12345",
    "year": 2025,
    "venue": null
  }
}
```

- Key chỉ là arXiv ID đã normalize: new-style → `YYMM-NNNNN`; old-style → `archive-YYMMNNN`.
- Chỉ reference **có** arXiv ID nằm trong file này. Reference không có arXiv ID để ở `references_all.json`; không trộn vào file bắt buộc.
- Dedupe theo key. Không bịa field thiếu; ghi lý do vào manifest/log.
- `s2_paper_id` là `paperId` của Semantic Scholar, không phải DOI hay arXiv ID.
- `submission_date` ưu tiên ngày submission arXiv; nếu nguồn chỉ có `publicationDate`, ghi rõ trong methods/limitations.

## 6. `manifest.json` và trạng thái

```json
{
  "arxiv_id": "2507.16659",
  "status": "complete",
  "versions": {
    "v1": {
      "status": "ok",
      "http_status": 200,
      "content_type": "application/gzip",
      "source_url": "https://export.arxiv.org/src/2507.16659v1",
      "archive_sha256": "hex",
      "archive_bytes": 123456,
      "extracted_bytes": 234567,
      "kept_bytes": 123456,
      "kept_files": 12,
      "removed_files": 5,
      "attempts": 1,
      "error": null
    }
  },
  "references_status": "ok",
  "updated_at": "2025-10-28T10:00:00Z"
}
```

| Cấp | Giá trị |
|---|---|
| Paper | `complete`, `complete_with_no_tex`, `partial`, `failed`, `not_found`, `not_attempted` |
| Version | `ok`, `no_tex`, `download_failed`, `extract_failed`, `validation_failed`, `skipped` |
| References | `ok`, `not_found`, `failed`, `skipped` |

- `not_found` là ID không tồn tại; `no_tex` là có source nhưng không có TeX (PDF/docx/…). Cả hai **không** phải `failed`.
- Chỉ ghi `ok` sau khi extract + validate thành công. Không coi file `.part`/archive dở là thành công.
- Ghi atomic: tải vào temp, extract vào temp, validate, rồi rename; không ghi đè version đã tốt.

## 7. Log, run manifest và stats

- Log JSONL: `logs/run-<run_id>.jsonl`, mỗi dòng một attempt với `ts`, `run_id`, `paper_id`, `version`, `stage` (`discovery|metadata|source|references|audit`), `status`, `duration_ms`, `http_status`, `attempt`, `bytes`, `error_type`, `message`.
- Không log API key/token. Không log toàn bộ response lớn.
- `run_manifest.json`: `run_id`, `started_at`, `ended_at`, `git_commit`, `config_hash`, environment (Python/Colab/CPU/RAM/disk), số paper/version, lỗi theo loại, `active_seconds`, `idle_seconds`, `session_ids`.
- Stats cho Book: `papers.csv`, `versions.csv`, `references.csv`, `benchmark.json`, `summary.json`. Mỗi thống kê phải ghi rõ mẫu số (candidate/valid/version/reference).

## 8. Quy ước chung

- JSON UTF-8, `ensure_ascii=False`, key ổn định; không commit corpus, archive, log hay secret vào Git.
- Code/comment/docstring tiếng Anh cơ bản; tài liệu nội bộ tiếng Việt. Book nộp bài theo ngôn ngữ chính thức của chương trình.
- `contract_version = 1`. Thay đổi schema/status/layout phải báo cả nhóm và cập nhật test/auditor/report cùng lúc.
- Việc chưa chốt (không làm thay đổi contract): nguồn metadata (Kaggle/OAI/API), S2 key, chạy full trên Colab hay local.
