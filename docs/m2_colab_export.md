# M2 Team Colab Dataset Export

Một notebook dùng chung: [m2_team_colab_export.ipynb](../notebooks/m2/m2_team_colab_export.ipynb).
Mở trực tiếp trên Colab:

https://colab.research.google.com/github/Toan-ten/Arxiv-Project-Group10/blob/integration%2Fm2-safe-plus-fixed/notebooks/m2/m2_team_colab_export.ipynb

Đây là **data export**, không phải final performance benchmark. Không crawl, không gọi S2,
không tải BBL artifacts và không upload tự động. Không cần API key hoặc HF token.

## Một ô cấu hình cho bốn thành viên

| STUDENT_ID | START_COHORT–END_COHORT tối đa | Dải arXiv |
|---|---|---|
| `24120010` | 1–50 | 2508.07872–2508.12871 |
| `24120035` | 51–100 | 2507.16659–2507.21658 |
| `24120232` | 101–150 | 2502.08235–2502.13234 |
| `24120260` | 151–200 | 2501.02642–2501.07641 |

Có thể chọn bất kỳ **dải liên tiếp nằm trong phần được giao**; không chọn sang MSSV khác.
Mapping được đọc từ `config/group10_id_ranges.json` của parser checkout đã xác minh,
không hardcode MSSV hoặc 0101–0150 trong helper xử lý.

Ví dụ của Toàn (ô cấu hình duy nhất):

```python
STUDENT_ID = "24120232"
START_COHORT = 101
END_COHORT = 150
BATCH_SIZE = 5
INPUT_DRIVE_DIR = f"/content/drive/MyDrive/M1-pipeline-optimization-full/{STUDENT_ID}/archives"
OUTPUT_DRIVE_DIR = f"/content/drive/MyDrive/ArxivGroup10/{STUDENT_ID}/m2-exports"
HF_REPO_ID = "toanten/arxiv-project-group10"
```

Mỗi người thay MSSV, dải cohort, paths và HF_REPO_ID bằng dataset cá nhân của mình.
Giữ MSSV là string; cohort là số nguyên, `BATCH_SIZE` từ 1 đến 50.
Notebook không tạo hoặc kiểm tra quyền sở hữu HF repo qua mạng; người vận hành tự xác nhận.

## Input M1 cần có trên Drive

Cả bốn thành viên dùng `MyDrive/M1-pipeline-optimization-full/<STUDENT_ID>/archives`.
Chỉ MSSV thay đổi trong đường dẫn input; `OUTPUT_DRIVE_DIR` vẫn riêng theo từng người.
Thư mục input phải tồn tại sau khi mount Drive; Setup dừng và báo đúng path nếu thiếu,
không tự tạo thư mục hoặc dữ liệu M1.

Đặt đủ **ba file cho mỗi cohort** trong INPUT_DRIVE_DIR, không đổi tên hoặc bytes:

```text
cohort_NNNN_corpus.tar.gz
cohort_NNNN_corpus.manifest.json
cohort_NNNN_corpus.tar.gz.sha256
```

Đây là các output M1 đã có, không phải manifest do notebook tự bịa.
Archive chứa trực tiếp thư mục ID: `<ID>/manifest.json`, `<ID>/metadata.json`, `<ID>/vN/...`.
Helper kiểm tra archive hash với sidecar và archive manifest, expected/included IDs,
MSSV và toàn bộ source checksums theo manifest từng version trước khi parse.
Trước Smoke, batch kế tiếp hoặc Audit, notebook liệt kê **tất cả paths thiếu** trong
phạm vi đó và dừng trước khi khởi chạy M2. Không yêu cầu đủ mọi cohort tương lai chỉ để smoke.
Helper tiếp tục kiểm tra độc lập archive/manifest/sidecar và checksum như trước.
Thiếu checksum, metadata không khớp hoặc archive bị đổi thì dừng an toàn.
Có thể chép trước cohort đầu để smoke; sau đó bổ sung từng batch. Input của cohort đã chạy
được khóa trong campaign, nhưng thêm file của cohort chưa chạy không phá Resume.

Các file input của Toàn đang có tại máy:
`D:\NM_KHDL\m1_189_download\2502.08235–2502.13234\01_raw\arxiv\`.
Đây chỉ là ví dụ chuyển dữ liệu; notebook không tham chiếu ổ D: hoặc catalog local.
Những thành viên khác cần tự có bộ input tương ứng. Thiếu cohort (ví dụ 0090–0100 ở bộ local cũ)
được báo thiếu, không được gọi là đã hoàn tất.

## Chạy và Resume

1. Mở link Colab trên, chọn Runtime → Change runtime type → **CPU**; tạo bản sao trong Drive nếu cần.
2. Chỉ chỉnh ô cấu hình. Chạy **Setup** để mount Drive, clone helper release và parser,
   cài dependency M2. Phiên bản thực tế và campaign path được hiển thị.
3. Chạy **Smoke**: toàn cohort đầu, giữ mọi version của một paper cùng nhau.
   Đọc counts và so sánh một paper checkpoint với source trước khi mở rộng.
4. Chạy **Run batch**: mỗi lần tối đa BATCH_SIZE cohorts, batch cuối có thể ít hơn.
   Cohort đã verified chỉ audit; batch bị ngắt tiếp tục ở lần Run chủ động kế tiếp.
5. Chạy **Audit** trước khi chọn gói upload. Không có lệnh tự chạy cả 200 cohorts.

Runtime mới: chạy lại cấu hình → Setup → Run batch, giữ nguyên cấu hình và Drive paths.
Parser luôn ghim `072ee384efa2e22f743ada7786aa0f67113a1490`.
Helper nằm trong **release commit riêng** được notebook ghim bằng full SHA;
Setup tải thẳng từ GitHub và checkout detached. Không nhúng source helper trong notebook,
không đọc helper từ branch thay đổi hoặc yêu cầu file trên máy Windows.
Hai checkout riêng bảo đảm source parser giữ nguyên khi phát hành helper/notebook mới.

Campaign thực tế dưới:
`OUTPUT_DRIVE_DIR/<STUDENT_ID>/<START-END>-<parser SHA12>-<HF repo hash12>/`.
HF repo khác, MSSV khác hoặc dải khác có namespace riêng. Campaign identity còn khóa
BATCH_SIZE, parser fingerprint, helper SHA/commit, Python minor, Arrow, input path và HF repo.
Không đổi identity để ép dùng checkpoint khác. Notebook cá nhân cũ không tự migration sang campaign mới.

```text
<campaign>/
  campaign.json
  inputs/cohort_NNNN_corpus.json
  results/cohort_NNNN_corpus/run-<fingerprint>/
    definition.json
    papers/<ID>/result.json
    papers/<ID>/partial/<attempt>.json
    attempts/<attempt>.json
    summaries/<attempt>.json
    data/{elements,hierarchy,bibtex}/part-00000.parquet
    output_checksums.json
  verified/cohort_NNNN_corpus.json
  batches/<first-last>.json
  upload/m2-<STUDENT_ID>-<first-last>-<parser SHA12>/
    data/{elements,hierarchy,bibtex}/cohort_NNNN_corpus.parquet
    coverage.json
    checksums.json
    BATCH_CARD.md
    READY.json
```

Mỗi paper được xử lý nguyên nhóm versions; không chia publication giữa hai batch.
Input chỉ đọc, giải nén vào scratch `/content/m2-input/<STUDENT_ID>/<START-END>/`.
Checkpoint, reports và Parquet bền vững trên Drive. Cần đủ dung lượng cho source,
checkpoint, Parquet và gói upload (có bản sao Parquet); scratch không tự xóa file cũ.

**Chỉ một writer cho một campaign.** Wrapper và native runner có writer locks.
Stop gửi SIGINT tới cả process group, rồi kill hữu hạn nếu không kết thúc.
Hard runtime termination có thể để lại lock: không tự phá lock. Chỉ lưu trữ/di chuyển lock
sau khi chắc chắn process/runtime cũ đã chết và đã giữ bằng chứng để review.
Drive FUSE không đảm bảo toàn bộ tính chất atomic của filesystem local; phải smoke/Resume thật trên Drive.

## Audit, trạng thái và partial release

- Giữ schema Parquet nguyên bản: `elements`, `hierarchy`, `bibtex`.
- Kiểm tra đầy đủ columns/types/nullability, checkpoint checksum, exact row equality,
  version coverage/counters, hierarchy không cycle/dangling/cross-paper và đúng version root.
- Citation phải trỏ tới entry có evidence của chính version đó, không chỉ bibliography union của paper.
- `parse_error`, `source_unavailable`, `empty_no_tex` được tách riêng. Không bịa records cho version lỗi.
- Chỉ trường hợp `.TEX` uppercase bị parser Linux từ chối sau checksum hợp lệ mới có thể giữ
  `input_error` terminal trong coverage; lỗi input khác chặn batch.
- `omitted_ids` từ archive manifest luôn được ghi rõ. Không tự gán version cho ID không có record.
- Publication có manifest nhưng không có version records là blocker cần review, không âm thầm bỏ qua.
- Timeout còn retryable không có completion marker mới; Resume chủ động, không retry vô hạn.
- `processing_complete` chỉ xác nhận xử lý kết thúc và audit hợp lệ, khác `all_versions_parsed`.
  Coverage công bố số cohorts/bài/versions thực tế, omitted IDs và từng status.
- `READY.json` cho phép upload gói đã kiểm tra với coverage minh bạch; không chứng nhận mọi
  scientific content đã được parser hiểu hoặc cả dải đã hoàn tất.

Chỉ số wall/output native dùng vận hành; không cộng các lần cached Resume thành thời gian
parse mới, không cộng RAM peaks/disk snapshots, không dùng lượt này làm final benchmark.
BBL artifacts độc lập không tự tải. Thiếu file/key vẫn báo lỗi. Parser baseline còn giới hạn
TeX semantics; hai ca uppercase đã biết là 2502.10009v1 và 2502.12888v6.

## Upload thủ công lên Hugging Face

Có thể công bố batch verified trước khi đủ dải (ví dụ 5/50), nhưng ghi partial coverage.
Sau Audit, tải gói có READY từ Drive; mở `https://huggingface.co/datasets/<HF_REPO_ID>` →
Files and versions → Add file → Upload files.
Giữ prefix riêng `m2/<STUDENT_ID>/072ee384efa2/<batch>/`, không upload trùng batch hay overwrite của người khác.
Không upload raw M1, checkpoints hoặc secrets. Không thay README dataset chung bằng BATCH_CARD.

Ba bảng khác schema cần ba dataset configs. Ví dụ cho **chỉ batch 0101–0105 đã verified**:

```yaml
configs:
  - config_name: m2_elements
    data_files:
      - split: train
        path: m2/24120232/072ee384efa2/0101-0105/data/elements/*.parquet
  - config_name: m2_hierarchy
    data_files:
      - split: train
        path: m2/24120232/072ee384efa2/0101-0105/data/hierarchy/*.parquet
  - config_name: m2_bibtex
    data_files:
      - split: train
        path: m2/24120232/072ee384efa2/0101-0105/data/bibtex/*.parquet
```

Thay path theo MSSV và batch thực tế. Thêm shards verified, không liệt kê batch chưa upload.
`train` chỉ là tên split cho Hub, không phải chia tập train/test khoa học.
Dataset card phải ghi parser/helper commits, số cohorts thực tế, version statuses và các thiếu sót từ coverage.json.

Nguồn vận hành: [Colab FAQ](https://research.google.com/colaboratory/faq.html) và
[Hugging Face Uploading datasets](https://huggingface.co/docs/hub/datasets-adding).
Chưa xác minh chạy trên Colab/Drive thật trong lượt refactor; không upload dữ liệu.
