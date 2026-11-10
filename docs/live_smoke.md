# Live smoke, ngắt phiên và Resume — M1-T02

Bộ công cụ cho thẻ M1-T02: chạy `pilot_10` trên Colab CPU-only, lưu corpus bền vững
trên Google Drive, ngắt tiến trình có chủ đích rồi kiểm chứng khả năng phục hồi.

Ba thành phần:

| Tệp | Vai trò |
| --- | --- |
| `scripts/live_smoke.py` | CLI `smoke` / `resume` / `verify` / `proof` |
| `notebooks/colab_m1_runner.ipynb` | Notebook Colab gắn Drive và chạy bốn bước trên |
| `tests/test_live_smoke.py` | 35 unit tests offline, fake HTTP, corpus trong TEMP |

Module này **không** thay đổi contract v2, không gọi mạng khi import và không tự chạy
pilot. Nó chỉ orchestration quanh `src.batch`, `src.pipeline` và `src.storage`.

## Gốc thư mục

Thứ tự ưu tiên khi không truyền `--corpus-root` / `--log-root`:

1. Nếu `/content/drive/MyDrive` tồn tại (Colab đã mount Drive):
   corpus = `/content/drive/MyDrive/arxiv_corpus/corpus`,
   log = `/content/drive/MyDrive/arxiv_corpus/logs`.
2. Nếu không: `data/pilot-10-smoke/corpus` và `data/pilot-10-smoke/logs` trong repository.

Hai thư mục phải tách biệt và không lồng nhau, đúng ràng buộc của Batch Runner.

## Lệnh

```powershell
# 0. Kiểm tra bộ pilot và corpus hiện có (không gọi mạng)
python -B -m scripts.live_smoke verify

# 1. Lượt mới, ngắt sau khi xong bài thứ 5
python -B -m scripts.live_smoke smoke --interrupt-after 5

# 2. Chạy lại chính batch đó với resume=True
python -B -m scripts.live_smoke resume

# 3. Kiểm tra corpus và xuất báo cáo nghiệm thu
python -B -m scripts.live_smoke verify
python -B -m scripts.live_smoke proof
```

Không truyền `--interrupt-after` thì `smoke` chạy hết 10 bài.
`--interrupt-at-start N` ngắt **khi bắt đầu** bài N, dùng để kiểm chứng trường hợp
gián đoạn giữa một paper thay vì ở ranh giới paper.

### Cờ của `smoke` / `resume`

| Cờ | Mặc định | Ý nghĩa |
| --- | --- | --- |
| `--pilot` | `config/pilot_10.json` | Bộ ID; mọi entry phải thuộc assigned range |
| `--batch-id` | `m1-smoke-pilot-10` | Batch ID; đổi batch thì phải chạy `smoke` mới |
| `--corpus-root`, `--log-root` | Drive hoặc `data/` | Phải tách biệt, không lồng nhau |
| `--max-ids` | `10` | Số ID đầu tiên được chọn |
| `--versions` | tất cả | Ví dụ `--versions v1` giảm số request |
| `--timeout` | `30` | Timeout arXiv |
| `--max-attempts` | `1` | Smoke phải giữ `1` |
| `--s2` | tắt | Bật Semantic Scholar; cần `SEMANTIC_SCHOLAR_API_KEY` |
| `--benchmark` | tắt | Đi qua `run_benchmark` để ghi wall time/RAM/disk; đường ngắt và resume giữ nguyên |
| `--report-root` | cùng cấp corpus | Thư mục report benchmark, phải tách khỏi corpus và log |
| `--experiment-id` | tự sinh mỗi lượt | Report không bao giờ ghi đè; resume mở experiment mới |
| `--sample-interval` | `0.1` | Chu kỳ lấy mẫu RAM (giây), chỉ khi `--benchmark` |
| `--disk-interval` | `30` | Chu kỳ lấy mẫu đĩa (giây), chỉ khi `--benchmark` |
| `--s2-min-interval` | `2.0` | Pacing S2 (giây) |
| `--interrupt-after N` | — | Ngắt sau khi N paper hoàn tất |
| `--interrupt-at-start N` | — | Ngắt khi paper N bắt đầu |

Hai cờ ngắt loại trừ nhau. `--interrupt-after` và `--interrupt-at-start` không có tác
dụng ngoài script này: chúng tiêm `KeyboardInterrupt` qua progress callback của Batch.

## Vì sao giả lập ngắt là hợp lệ

`run_batch()` bắt `KeyboardInterrupt` giống hệt Ctrl+C: attempt được ghi
`status="interrupted"`, phần đã checkpoint giữ nguyên, rồi exception được truyền ra.
`SimulatedInterrupt` chỉ gọi `raise KeyboardInterrupt(...)` từ callback
`on_progress` **sau khi** snapshot của paper đã bền (Batch gọi observer sau `persist()`).

Vì vậy đường đi mã nguồn của lần ngắt giả lập trùng với lần ngắt thật:

- attempt log còn `status="running"` nếu process bị kill trước khi ghi trạng thái cuối
  (trường hợp mất runtime Colab), và lần resume sau ghi nhận nó trong
  `previous_incomplete_attempts`;
- attempt log đổi thành `interrupted` nếu việc ghi trạng thái cuối kịp (Ctrl+C và
  `--interrupt-after`).

`proof` chấp nhận cả hai dạng, nên bằng chứng đúng cho cả ngắt thật lẫn ngắt giả lập.

## Cờ `on_progress` của Batch

`run_batch(..., on_progress=None)` là tham số tùy chọn mới: observer được gọi sau mỗi
snapshot đã ghi atomic, nhận `(event, summary_copy)` giống Pipeline. Không truyền thì
hành vi cũ giữ nguyên; `docs/batch.md` vẫn đúng cho mọi mô tả khác.

## `--benchmark`: dữ liệu và metrics trong cùng một lượt

Mặc định `smoke`/`resume` gọi `src.batch.run_batch` (chỉ dữ liệu). Với
`--benchmark`, chúng gọi `src.benchmark_runner.run_benchmark`: cùng selection,
cùng checkpoint, cùng đường ngắt/resume, nhưng có thêm wall time, RAM đỉnh và
dung lượng đĩa cho M1-T03/M1-T06.

Mỗi invocation tạo một experiment riêng dưới `reports/<experiment-id>/`
(`intent.json`, `report.json`, `resources.json`), và resume **không** ghi đè
report cũ. Evidence JSON ghi lại đường dẫn report và vài số đo đầu mục.

Hệ quả cần lưu ý: `resume` phải giữ nguyên `--benchmark` nếu muốn số đo cho lượt
sau; hai lượt là hai experiment độc lập, không cộng gộp wall time thành một con số.

## Dung lượng: đo trước khi chạy 20.000 ID

`verify` cộng dung lượng thật của từng paper và in một dòng ngoại suy:

```text
Storage: Measured on 10 paper(s): 1.23 MiB, of which 0.94 MiB is .tex/.bib.
Projected for 20,000 papers: 2.46 GiB
```

Đây là **ngoại suy tuyến tính từ pilot**, dùng để đối chiếu hạn mức Google Drive
trước khi đầu tư hàng chục giờ crawl; không phải cam kết về corpus cuối.

## Resume bỏ qua gì

Lần `resume` chuyển toàn bộ selection vào Pipeline, không tự bỏ qua ở tầng CLI:

- Discovery đã `found`/`not_found` dùng lại từ checkpoint → `action="cached"`, không request.
- Version `success`/`no_tex_source` còn khớp SHA-256 → `action="skipped"`, không tải lại.
- Version `pending`/`interrupted`/lỗi retryable được thử lại.
- Checkpoint hỏng, checksum lệch hoặc source orphan bị `blocked` để audit; không xoá,
  không ghi đè, không tự nhận là thành công.

Do đó yêu cầu nghiệm thu "bỏ qua 5 bài đã crawl, tiếp tục từ bài 6" được kiểm chứng
bằng cặp `discovery=cached` + `source=skipped` trong attempt log của lần resume.

## `verify` kiểm tra gì

Độc lập với log: đọc lại corpus và tự băm lại từng file.

1. Đủ `metadata.json`, `references.json`, `manifest.json`; JSON hợp lệ.
2. `contract_version == 2` và `arxiv_id` khớp ID đã chọn.
3. `manifest.mssv` khớp MSSV được giao.
4. Mỗi thư mục `vN/` chỉ chứa file thường `.tex`/`.bib`; không ảnh, `.sty`, `.bbl`,
   build artifact.
5. Mọi file trong `vN/` phải có trong `manifest.files`; không file lạ.
6. SHA-256 của từng file khớp `manifest.file_checksums` (phát hiện ghi đè/hỏng).
7. Không có `.tmp`, `.part`, `.writing` còn sót trong corpus.

`--json-out` ghi báo cáo máy đọc được; lệnh trả exit code 1 khi có vi phạm.

## `proof` kiểm tra gì

So sánh hai attempt log với nhau và với snapshot corpus ghi ngay sau lần ngắt:

| Check | Bằng chứng |
| --- | --- |
| `attempts >= 2` | Có lần ngắt và lần resume |
| `interruption recorded` | Attempt đầu `interrupted` (hoặc đang `running` nếu bị kill) |
| `resume attempt ran to completion` | Attempt sau `finished`/`stopped` |
| `interruption visible to the resume` | Trạng thái ngắt hoặc `previous_incomplete_attempts` |
| `same selection`, `no duplicate arXiv ID` | Không trùng lặp, không đổi thứ tự |
| `papers 1..N skipped without re-crawl` | `discovery=cached`, không version hoàn tất nào bị tải lại |
| `papers N+1.. processed by the resume` | Phần còn lại thực sự được xử lý |
| `existing files not rewritten` | So snapshot: file `.tex`/`.bib` phải **nguyên byte và nguyên mtime** |
| `corpus integrity` | Chạy lại `verify_corpus()` |
| `no leftover temp files` | Quét corpus và log |

### Snapshot và ngoại lệ có kiểm soát

Mỗi lần `smoke`/`resume` ghi `evidence/<timestamp>-<mode>.json`, gồm snapshot corpus
(SHA-256 + `mtime_ns` của từng file). `proof` so snapshot của lần ngắt với trạng thái
hiện tại:

- **unchanged** — trùng hash và trùng `mtime_ns`: file không bị chạm.
- **checkpoint-only refresh** — chỉ `manifest.json` đổi, và chỉ ở hai khóa
  `run_id`/`updated_at`; version record, checksum và status giữ nguyên. Đây là bookkeeping
  hợp lệ của lần resume, không phải ghi đè dữ liệu.
- **rewritten** — mọi khác biệt còn lại, kể cả ghi lại đúng byte cũ nhưng `mtime` mới.
  Check thất bại.

Ngoại lệ trên được giới hạn đúng hai khóa và chỉ áp cho `.json`; nội dung đã thu
(metadata, references, `.tex`, `.bib`) vẫn phải trùng tuyệt đối.

## Ngân sách request

Với `--max-attempts 1`, một paper mới tốn tối đa 1 discovery + N source request, N là số
version arXiv xác nhận; `--versions v1` đặt N = 1. Lượt resume chỉ tiêu request cho phần
chưa xong. Không có retry vòng ngoài, không tự chạy lại sau 403/429.

Pilot 10 với tất cả version có thể vượt 20 request (10 discovery + source của từng
version). Muốn giảm, dùng `--versions v1` cho lượt đầu; nhưng khi đó corpus chỉ có `v1/`
và bước kiểm tra `v2/` không áp dụng.

## Bằng chứng nộp thẻ

| Bằng chứng | Đường dẫn |
| --- | --- |
| Resume proof PASS/FAIL | `logs/<batch-id>/evidence/resume-proof.md` |
| Kiểm tra corpus | `logs/<batch-id>/evidence/corpus-verification.json` |
| Attempt bị ngắt | `logs/<batch-id>/attempts/<attempt-id>.json` |
| Attempt resume | `logs/<batch-id>/attempts/<attempt-id>.json` |
| Góc nhìn người vận hành | `logs/<batch-id>/evidence/*-smoke.json`, `*-resume.json` |
| Corpus | `corpus/<folder-key>/{metadata,references,manifest}.json`, `v1/`, `v2/` |

## Trạng thái thực thi

Các kiểm thử trong `tests/test_live_smoke.py` chạy offline bằng fake HTTP, corpus trong
TEMP. Bản thân chúng không phải bằng chứng crawl thật.

Lượt chạy thật đã thực hiện **10/11/2026** trên Colab CPU-only, corpus trên Google Drive,
hánh `feat/m1-smoke-resume`: `proof` PASS, `verify` PASS, attempt 1 `interrupted` sau 5/10
paper, attempt 2 `finished` 10/10, tiết kiệm 12 request. Chi tiết và các điểm cần Toàn
quyết định trước Full Crawl: [biên bản M1-T02](m1_t02_resume_report.md).

Chưa kiểm chứng ở quy mô 20.000 ID, chưa bật Semantic Scholar, chưa có số liệu
benchmark; xem mục “Phạm vi bằng chứng” trong biên bản.

## Kiểm thử offline

```powershell
python -B -m unittest discover -s tests -p "test_live_smoke.py" -v
python -B -m unittest discover -s tests -v
```

Tests phủ: đọc/kiểm tra pilot (trùng ID, ID ngoài range, file hỏng, lệch generator),
`SimulatedInterrupt` ở cả hai chế độ, quét file tạm, `verify_corpus` (thiếu paper, ghi đè
`.tex`, ảnh trong `vN/`, `contract_version` sai, file tạm, corpus rỗng, ngoại suy dung lượng)
và end-to-end `smoke → resume → proof` qua CLI thật với corpus nhiều version, cả đường
`run_batch` lẫn đường `--benchmark` (report không ghi đè, attempt `interrupted` vẫn có
metrics, resume mở experiment mới).

## Giới hạn

- Chỉ một process/writer, kể cả giữa hai batch dùng chung corpus; chưa có lock liên process.
- Pacing/cooldown không bền vững qua restart.
- `proof` cần snapshot do chính script này ghi; lượt `smoke` chạy bằng lệnh Python thủ công
  sẽ không có snapshot và check tương ứng báo thiếu bằng chứng.
- Chưa có CLI production cho full crawl; `smoke` chỉ chạy danh sách ID được chỉ định.
- `--s2` chưa cover hết trạng thái references phân trang; xem [s2 integration](s2_integration.md).
