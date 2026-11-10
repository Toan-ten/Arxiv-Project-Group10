# Biên bản kiểm chứng Resume — M1-T02

**Thẻ:** `[KHOI_ANH - M1-T02]` · **Ngày chạy:** 10/11/2026 · **Môi trường:** Google Colab CPU-only,
corpus trên Google Drive (`/content/drive/MyDrive/arxiv_corpus`).
**Trình bày cho:** Nguyễn Văn Toàn (ký duyệt Full Crawl `[ALL - M1-T04]`).
**Công cụ:** `scripts/live_smoke.py` trên nhánh `feat/m1-smoke-resume` (commit `7b24479`),
notebook `notebooks/colab_m1_runner.ipynb`.

## 1. Kết quả tổng hợp

| Hạng mục | Kết quả |
| --- | --- |
| `proof` (báo cáo nghiệm thu resume) | **PASS** — toàn bộ 11 check đều đạt |
| `verify` (toàn vẹn corpus, contract v2) | **PASS** — 0 vi phạm, 0 file tạm |
| Attempt 1 | `interrupted`, 5/10 paper hoàn tất |
| Attempt 2 | `finished`, 10/10 paper |
| Request tiết kiệm khi resume | **12 request** (5 discovery + 7 source) không gửi lại |

Corpus sau hai lượt: 10 paper, tổng **13 version** đã lưu, tất cả ID đều resolve được và đều
có source (không có `not_found`, không có `no_tex_source`).

## 2. Bằng chứng theo từng tiêu chí nghiệm thu của thẻ

| Tiêu chí | Bằng chứng | Kết quả |
| --- | --- | --- |
| Log chứng minh phục hồi, không trùng ID, không ghi đè dữ liệu cũ | Check `no duplicate arXiv ID`; check `existing files not rewritten` so snapshot (SHA-256 + `mtime_ns`) trước và sau resume; check `same selection in both attempts` | PASS |
| `pilot_10` toàn vẹn: đủ `manifest.json`, `references.json`, `v1`/`v2` chỉ `.tex`/`.bib` | `verify` (exit 0): đủ ba JSON, `contract_version=2`, ownership khớp, mỗi `vN/` chỉ có `.tex`/`.bib`, SHA-256 khớp `manifest.file_checksums` | PASS |
| Notebook Colab gắn Drive `/content/drive/MyDrive/arxiv_corpus` | Nhánh `colab_m1_runner.ipynb` bước 2; corpus và log nằm trên Drive, sống sót qua phiên Colab | PASS |
| Chạy `live_smoke` với `pilot_10`, tách `.tex`/`.bib`, loại ảnh | 13/13 version `status=success`; `verify` không thấy file ngoài `.tex`/`.bib` trong `vN/` | PASS |
| Ngắt nhân tạo khi đang chạy dở ở bài 5 | Attempt 1 `status=interrupted`, paper 1-5 `processed`, paper 6-10 `pending` | PASS |
| Resume bỏ qua 5 bài đã crawl, tiếp tục từ bài 6 theo `manifest.json` | Attempt 2: paper 1-5 `discovery=cached` và mọi version `source=skipped`; paper 6-10 `discovery=queried` + `downloaded` | PASS |
| Không có `.tmp` rác hay bản ghi hỏng khi bị ngắt | Check `no leftover temp files` = 0; quét cả corpus và log | PASS |
| Báo cáo cho Toàn ký duyệt | Tệp này | — |

## 3. Diễn biến hai lượt

**Attempt 1 — ngắt sau bài 5**

| # | arXiv ID | discovery | source |
| --- | --- | --- | --- |
| 1 | 2508.07872 | queried | v1 downloaded, v2 downloaded |
| 2 | 2508.12871 | queried | v1 downloaded |
| 3 | 2507.16659 | queried | v1 downloaded, v2 downloaded |
| 4 | 2507.21658 | queried | v1 downloaded |
| 5 | 2502.08235 | queried | v1 downloaded |
| 6-10 | 2502.13234, 2501.02642, 2501.07641, 2508.10371, 2507.19158 | — | pending |

12 request (5 discovery + 7 source). Ngắt xảy ra **sau khi snapshot của bài 5 đã bền**, đúng
đường phục hồi của Ctrl+C.

**Attempt 2 — resume**

| # | arXiv ID | discovery | source |
| --- | --- | --- | --- |
| 1-5 | như trên | **cached** | **toàn bộ skipped** |
| 6 | 2502.13234 | queried | v1 downloaded |
| 7 | 2501.02642 | queried | v1 downloaded |
| 8 | 2501.07641 | queried | v1 downloaded, v2 downloaded |
| 9 | 2508.10371 | queried | v1 downloaded |
| 10 | 2507.19158 | queried | v1 downloaded |

11 request. Năm bài đầu không tốn request nào; dữ liệu của chúng được đối chiếu lại bằng
SHA-256 và giữ nguyên byte.

## 4. Điểm cần Toàn quyết định trước khi duyệt Full Crawl

### 4.1. Mọi paper đang `partial`, không phải `complete`

Cả 10 paper đều `paper=partial`. Nguyên nhân: lượt chạy này chạy **arXiv-only**; stage
Semantic Scholar để `not_attempted`, nên `references.json` mới là khung rỗng và paper không
thể `complete` theo contract v2.

Điều này **không** ảnh hưởng kết luận về resume (checkpoint theo version nằm ở nguồn arXiv),
nhưng có nghĩa là **điều kiện tiên quyết "khóa Semantic Scholar hợp lệ để tránh 429" trên thẻ
chưa được kiểm chứng**. Đề nghị chọn một trong hai:

1. Chấp nhận S2 là hạng mục riêng, kiểm chứng ở `[TOAN - M1-T05]` trước khi Full Crawl; hoặc
2. Chạy thêm một lượt `--s2` với batch id mới (`m1-smoke-pilot-10-s2`) để xác nhận cả metadata
   và references trên cùng 10 bài.

### 4.2. Full Crawl 20.000 ID không thể xong trong một phiên Colab

Đo trên pilot: 23 request cho 10 paper (2,3 request/paper), client đặt pacing **3 giây/request**
(`src/arxiv_client.py`). Ngoại suy cho 20.000 ID:

| | Giá trị |
| --- | --- |
| Request ước tính | ~46.000 |
| Thời gian chỉ riêng pacing | **~38 giờ** |
| Số phiên Colab (giới hạn 12 giờ/phiên) | **~3,2 phiên** |

Hệ quả: Full Crawl bắt buộc phải **chia nhiều phiên và dựa vào checkpoint/resume** — đúng cơ
chế đã kiểm chứng ở đây. Nhưng cần chốt trước:

- chiến lược chia phiên (ví dụ mỗi phiên một `batch_id` theo dải ID, hoặc một batch duy nhất
  `resume=True` nhiều lần);
- có giảm pacing hay không (giảm xuống 1 giây sẽ rủi ro 429/403 và vi phạm hướng dẫn của arXiv);
- `--versions` cho Full Crawl: lấy tất cả version (như pilot, trung bình 1,3 version/paper) hay
  chỉ `v1` để giảm ~30% request.

### 4.3. Chưa có số liệu benchmark

Wall time, RAM đỉnh và dung lượng đĩa chưa được thu trong lượt này: phần đó thuộc
`[ANH - M1-T03]` và cần được nối vào cùng invocation Colab trước khi `[ANH - M1-T06]` tổng hợp
chương M1. Pilot hiện chưa cung cấp `benchmark_summary.json` / `http_metrics.json`.

## 5. Phạm vi bằng chứng

Bằng chứng này là **lượt chạy thật** trên arXiv (không phải fake HTTP) cho 10 paper của
`pilot_10`, trên Colab CPU-only. Nó **không** chứng minh:

- khả năng chịu lỗi ở quy mô 20.000 ID (chưa chạy);
- xử lý 429/403 thực tế của S2 (S2 chưa bật);
- tính đúng đắn của metadata/references ở mức publication `complete`;
- độ bền khi Colab kill process **giữa** một paper (lượt này ngắt ở ranh giới paper; trường hợp
  này có `--interrupt-at-start N` và đã có unit test offline, chưa chạy thật).

Đề nghị Toàn xác nhận: (a) chấp nhận S2 tách riêng hay yêu cầu chạy thêm lượt `--s2`;
(b) kế hoạch chia phiên cho Full Crawl theo mục 4.2.
