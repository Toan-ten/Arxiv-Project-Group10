# M2 Colab recovery — release nocite

Mở notebook riêng: [m2_nocite_recovery.ipynb](../notebooks/m2/m2_nocite_recovery.ipynb).
[Open in Colab](https://colab.research.google.com/github/Toan-ten/Arxiv-Project-Group10/blob/fix%2Fm2-nocite-checkpoint-migration/notebooks/m2/m2_nocite_recovery.ipynb).

Notebook ghim **RELEASE_COMMIT** đầy đủ, cùng SHA cho parser và helper; không lấy
helper/parser từ HEAD động. Commit phát hành notebook có thể mới hơn runtime pin
vì SHA runtime phải tồn tại trước khi notebook ghim nó. Tests kiểm hai implementation
khớp nhau. Notebook M2 team và helper legacy giữ nguyên; không đổi campaign cũ.

## Phạm vi và bảo toàn dữ liệu

Đây là recovery data export, không phải final benchmark. Bản vá canonicalize
explicit `\nocite{key1,key2}` theo cùng aliases với cite; giữ `\nocite{*}`. Audit
bibliography vẫn bắt buộc evidence của đúng version. Schema ba bảng không thay đổi.

Fingerprint parser Git/LF:
`4e88c7fb0d3463c4c6815b5afff3f64066815068dd4c34db1e92528966d60b70`.
Compatibility rule `nocite-alias-v1` chỉ cho phép đúng source delta đã review so
với parser `072ee384efa2e22f743ada7786aa0f67113a1490`. Không tự chấp nhận bản tối
ưu khác hoặc sửa fingerprint cũ để ép khớp.

Campaign mới `recovery-<identity hash>` được tạo riêng. Identity chứa old campaign
và hash identity cũ, runtime SHA/fingerprint, helper hash, MSSV/range/HF repo và
pilot cohort. Chỉ BATCH_SIZE được đổi mà không sinh campaign mới.

| Thao tác | Drive | Parser |
|---|---|---|
| Setup | Mount, đọc identity cũ; clone/config/dependencies ở `/content` | Không |
| `preflight`, `dry-run` | Chỉ đọc; giải nén archives vào scratch local | Không |
| `migrate` | Ghi identity/verified markers vào campaign mới | Không |
| `package` | Sao chép Parquet/provenance vào package mới | Không |
| `pilot` | Ghi checkpoint/Parquet/verified/package mới đúng pilot | Chỉ paper cần parse trong cohort đó |
| `audit`, `audit-pilot` | Chỉ đọc, không ghi lock/artifacts | Không |
| `continue` | Ghi các cohort sau pilot chưa packaged | Tối đa BATCH_SIZE cohorts/lượt |

Nếu lỗi: artifacts mới dở dang được giữ, không ghi completion/READY trước audit.
Không có thao tác xóa/ghi đè campaign cũ, cleanup tự động, tải corpus, API hay HF.
Migration dùng immutable writes; checksum sai làm dừng, không bỏ qua lỗi để PASS.

## Cấu hình notebook

Chỉ chỉnh ô 1, trước Setup:

```python
OLD_CAMPAIGN = "/content/drive/MyDrive/ArxivGroup10/24120232/m2-exports/24120232/0101-0150-072ee384efa2-3ffc7291afbd"
RECOVERY_ROOT = ""  # Tự suy từ owner đã xác minh trong campaign cũ.
PILOT_COHORT = 115
BATCH_SIZE = 5
```

MSSV, start/end cohort, input archives và HF repo đọc từ identity cũ; helper đối
chiếu lại assignment trong repo. Không sửa scope hoặc output của campaign cũ.
Recovery root mặc định `/content/drive/MyDrive/ArxivGroup10/<owner>/m2-recovery`.
Scratch cố định `/content/m2-nocite-recovery-scratch`, config `/content`, không ở
Drive. M2 không cần API keys. Chọn CPU runtime; không dùng GPU hoặc Run all.

Setup clone đủ history để kiểm source Git cũ, checkout SHA runtime trong notebook,
kiểm working tree sạch, cài `pyarrow==26.0.1` (phiên bản kiểm chứng local). Python
3.11–3.14. Sau runtime mới, chạy lại cấu hình/Setup/controller và preflight; giữ
nguyên release, old path, recovery root và pilot. Không tự reset code có thay đổi.

## Bước 1 — Preflight và dry-run chỉ đọc

Chạy hai ô tương ứng, đọc kết quả trước khi xác nhận ghi. Bắt buộc thấy:

- Đúng owner, scope101–150, old campaign và recovery campaign mới.
- 0101–0114 đều `MIGRATABLE`; cohort trước pilot thiếu verified marker thì dừng,
  không tự parse lại những cohort người vận hành tin rằng đã hoàn tất.
- 0101–0105 packaged ở campaign cũ. 0106–0114 được kiểm trạng thái package riêng.
- 0115: số paper có thể seed, paper cần reparse và checkpoint bị loại; paper lỗi
  nocite không được coi là reusable. Không giả định lỗi Colab khớp ca local khi
  chưa đọc evidence thực tế.
- `ready=true`, không writer locks, không checksum/provenance/schema errors.
- 0116–0150 là `PENDING_NOT_INSPECTED`, chưa được chốt source integrity. Mỗi cohort
  sẽ được kiểm archive/source trước lúc chạy, không giả vờ preflight đã đọc tất cả.

Preflight kiểm archive triplet, M1 identity/file hashes, source fingerprint, native
run definition/attempt, paper checkpoint SHA, version coverage, ba Parquet/schema,
root hierarchy và own-version bibliography. Packages cũ kiểm READY/coverage/hash.
Source changed sau approval → receipt không khớp, ghi Drive bị chặn. Token chỉ có
trong RAM/config local, không tự tạo approval trên Drive.

Nếu thiếu file: giữ đủ `cohort_NNNN_corpus.tar.gz`, `.manifest.json`, `.tar.gz.sha256`
và old campaign `campaign.json`, `inputs/`, `verified/`, native definitions,
attempts, paper results, output checksums, Parquet, batch/package artifacts. Không
sửa JSON hay tạo file giả. Absolute paths cũ phải còn truy cập được.

## Bước 2 — Migrate và package 0101–0114

Chạy ô Migrate, nhập xác nhận được yêu cầu. Helper kiểm lại preflight trước khi
ghi. Chỉ tạo campaign/verified markers mới cho cohort đủ bằng chứng và tương thích.

Chạy ô Package legacy tới khi `recovery_packaged` chứa đủ 0101–0114. Đổi BATCH_SIZE
thành50 nếu muốn đóng gói cả14 trong một lượt; không cần Setup lại. Bước này sao
chép **nguyên bytes Parquet** sang campaign mới cùng provenance, không parse lại.

Mỗi publication có `parser_origins`: SHA/fingerprint thật, source checkpoint và
checksum. Kết quả legacy không bị gắn nhãn đã parse bằng parser mới. Native
fingerprint của run mới chỉ định implementation assemble/validate; source origins
riêng mới cho biết paper được parse bởi ai. Giữ campaign cũ để audit lineage.

Packages mới có `coverage.json`/`checksums.json`/`READY.json` và batch marker. Coverage
công bố riêng partial inputs, processing_complete, all_versions_parsed và nguồn
parser của từng publication. Không coi processing_complete là mọi version success.

## Bước 3 — Chạy riêng 0115 rồi audit

Ô Pilot gọi action `pilot`, **không dùng next chung**. BATCH_SIZE=50 vẫn chỉ chọn
0115. Nếu 0101–0114 chưa migrate/package đủ thì helper chặn trước khi parse.

Trong 0115, chỉ seed các paper checkpoint đã qua checksum và own-version audit.
Checkpoint lỗi không được copy vào cache mới. Paper cần sửa được parse với **mọi
versions** để canonicalization giữ đúng quan hệ giữa versions; không lấy bibliography
khác version và không parse v2 riêng. Paper còn cache tốt được dùng lại.

Sau Pilot, chạy Audit pilot. Kiểm:

- `verified/cohort_0115_corpus.json`: `excluded_legacy_checkpoints`, `parser_origins`,
  statuses và native attempt path.
- Native paper `2502-09725/result.json` nếu đúng source đã tái hiện: v1/v2 success,
  nocite v2 dùng canonical key `bengio2014representationlearningreviewnew__6bcf59f320`.
- Package 0115 có full checksum/schema/citation/hierarchy audit và batch marker.
- Version từng success ở paper bị loại do nocite phải vẫn success; nếu không,
  continuation bị chặn để review. Errors của paper khác vẫn được báo trung thực.

## Bước 4 — Tiếp tục 0116–0150

Chạy ô Continue từng lượt. Helper audit pilot lại, yêu cầu pilot verified/packaged,
rồi chỉ chọn cohort sau115 chưa packaged, tối đa BATCH_SIZE. Không tự loop fullrange.
BATCH_SIZE thay đổi sau Setup có hiệu lực ngay, không tạo campaign khác.

Chạy ô Audit để kiểm lại mọi completed recovery. Không tự upload HF; khi upload
thủ công, chọn **một** release cho mỗi cohort, tránh package cũ/mới trùng nhau.

## Gián đoạn và rollback

- Stop bình thường: checkpoint/seed plan đã ghi được giữ. Chạy lại cùng release/
  config; immutable markers và native cache tránh parse lại phần hoàn tất.
- Hard reset có thể để lại `.export-writer.lock` hoặc native `.writer.lock`.
  Preflight báo lock và dừng. Xác minh không còn writer rồi review riêng lock;
  không tự xóa hoặc sửa checkpoint, không chạy thêm runtime trên cùng campaign.
- Rollback không cần phục hồi campaign cũ vì recovery không sửa nó: dừng writer
  mới, giữ partial recovery/evidence để chẩn đoán. Có thể quay về notebook cũ để
  đọc/audit completed; **không tiếp tục 0115 bằng parser cũ còn bug**.
- Nếu release không đạt, không gán marker hoặc đổi fingerprint để Resume. Sau khi
  review bản sửa mới, dùng identity/version mới; không ghi đè recovery đã có.
- Artifact writes/rename/fsync trên Drive FUSE phải được preflight/readback kiểm
  thực tế; tests local không chứng minh toàn bộ hành vi Colab/Drive.

## CLI tương ứng

Dùng config JSON do controller tạo ở `/content`, có release SHA/fingerprint đã ghim:

```bash
python -B -m scripts.m2_colab_recovery --config /content/m2-nocite-recovery-config.json --action preflight
python -B -m scripts.m2_colab_recovery --config /content/m2-nocite-recovery-config.json --action dry-run
```

Các actions ghi yêu cầu `approved_preflight_token` của preflight hiện tại; notebook
chỉ thêm token sau khi người vận hành đọc và xác nhận thao tác. Production config
luôn `allow_uncommitted=false`. Không chạy các actions ghi trong lúc chuẩn bị release.
