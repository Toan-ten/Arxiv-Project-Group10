# Bàn giao A: vận hành M1 arXiv-first trên Colab CPU-only

Mở `notebooks/m1_colab_arxiv_production.ipynb` bằng Colab. Notebook có đúng
hai ô code: **Setup** và **Run M1**. Không cần widgets hay Semantic Scholar key.
Đây là bước thu thập arXiv; chưa phải hoàn thành M1 vì chưa bổ sung S2/references.

## Lần đầu

1. Người phụ trách review/commit/push bộ bàn giao. Trong Setup, điền **full SHA
   của commit chứa bộ bàn giao** vào `CODE_COMMIT` rồi lưu bản notebook của A.
2. Chọn Runtime → Change runtime type → **CPU / accelerator None**. Dừng mọi
   runtime/crawler khác ghi cùng corpus. Chỉ một writer cho toàn chiến dịch.
3. Bấm Run ô Setup: mount Drive, clone checkout riêng trong `/content`, kiểm tra
   commit sạch, cài `requirements-colab.txt`, in phiên bản thực tế. Setup không crawl.
   Checkout có sẵn khác commit/bị sửa sẽ bị từ chối; không tự reset/pull.
4. Trong **một khối CONFIG ở ô Run M1**, chọn Drive root mới và cohort đầu/cuối.
   Mặc định chỉ **cohort 1**, 100 ID. Kiểm tra đường dẫn trước khi bấm Run.
5. Đọc tóm tắt và audit JSON sau mỗi cohort. Khi kết quả tốt, đổi số cohort và
   bấm Run tiếp; chỉ mở rộng số cohort mỗi lượt sau khi đã kiểm tra runtime/Drive.

Không cần đổi thuật toán hay nhiều cờ True/False. Giữ nguyên root, commit,
timeout và sampling khi Resume: `campaign.json` và Batch bảo vệ cấu hình này.
Không thay campaign.json, batch.json hay manifest bằng tay để vượt kiểm tra.

## Chọn đúng 20.000 vị trí ID

Cohort cố định **100 ID**, đánh số **1–200**, gồm cả hai đầu của các khoảng:

| Cohort | MSSV | Khoảng toàn phần |
| --- | --- | --- |
| 1–50 | 24120010 | 2508.07872–2508.12871 |
| 51–100 | 24120035 | 2507.16659–2507.21658 |
| 101–150 | 24120232 | 2502.08235–2502.13234 |
| 151–200 | 24120260 | 2501.02642–2501.07641 |

Danh sách được cắt từ `iter_assigned_ids()` theo cấu hình đã kiểm tra, chỉ giữ
100 cặp MSSV/ID của cohort hiện tại. Không chọn riêng paper đã biết tồn tại.
20.000 vị trí ứng viên không đồng nghĩa 20.000 publication hợp lệ.
`first_cohort` và `last_cohort` là cửa sổ **bao gồm hai đầu**, không tự mở rộng.
Ví dụ 51–52 chạy tối đa 200 vị trí của **24120035 (Đạt)**. Nếu lỗi/chưa xong
cohort 51 thì cohort 52 chưa được chạy.

## Run, Audit và Resume

- Chưa có batch: tạo `m1-arxiv-NNNN`, gọi `run_benchmark()` → Batch → Pipeline.
- Batch hợp lệ nhưng còn pending/timeout/history thiếu: Resume cùng cấu hình;
  source/checksum tốt và history verified được dùng lại, không tải lại vô điều kiện.
- Đã đủ pha arXiv: **chỉ audit**, không gọi Batch/Benchmark, không tạo attempt
  HTTP mới, không sửa corpus/logs/reports cũ; lưu một audit mới có tên riêng.
- `finished` chỉ có nghĩa đã xử lý danh sách. Pha arXiv chỉ đủ khi mỗi ID là
  not_found đã xác nhận, hoặc found/resolved với mọi version success/no_tex
  đã kiểm chứng và toàn bộ ngày history verified. No-TeX được giữ riêng.
- Metadata/publication có thể vẫn partial, references not_attempted; không
  dùng số references 0 để thay cho chưa thu thập, không tuyên bố M1 complete.
- 403/429 dừng ngay lượt Run, không sang version/paper/cohort tiếp theo;
  `max_attempts=1`, pacing arXiv dùng chung từ client hiện có. Không có outer retry.
  **Không bấm Run liên tục sau 403/429**: xem lỗi, chờ và quyết định trước.
  Bấm lại ô Run là yêu cầu thử mới của người vận hành, không tự lên lịch.
- Checksum sai, orphan, thiếu log hoặc checkpoint không nhất quán: dừng để
  review, giữ nguyên bằng chứng, không tự sửa/xóa/ghi đè. Lỗi/exception nghiêm
  trọng cũng dừng; xem logs/reports trước lần tiếp theo.

Sau khi runtime mất kết nối: chắc chắn runtime cũ đã ngừng, mở lại notebook,
chạy Setup đúng commit, chọn lại **cùng cohort và Drive root**, rồi Run M1.
Storage giữ checkpoint trên Drive, không phụ thuộc `/content` tạm. Hard kill có
thể để lại orphan hoặc thiếu số đo; phải audit, không cam kết tự phục hồi mọi lỗi
filesystem. Atomic replace trên Drive không phải giao dịch nhiều JSON hay bảo
đảm chống mất dữ liệu khi Drive bị lỗi/quota. Có lỗi integrity thì dừng và bàn giao
log cho Toàn, không đổi root để che lỗi.

## Dữ liệu và số đo để bàn giao C

```text
<Drive root>/
  campaign.json                 Cấu hình bất biến, code commit và assignments
  corpus/                       JSON checkpoint + source mọi version (.tex/.bib)
  logs/m1-arxiv-NNNN/            batch.json, attempts/, measurements/
  reports/<experiment-id>/      intent.json, resources.json, report.json
  audits/cohort-NNNN-<uuid>.json Audit từng lượt, coverage/history/metrics
  scratch/                      Archive tạm do downloader quản lý
```

Corpus dùng chung giữa cohort; scope disk là **toàn corpus + scratch** trên Drive,
không gồm code/logs/reports. Sampling disk mặc định 120 giây để hạn chế quét toàn
corpus lớn; sampled peak có thể bỏ lỡ đỉnh giữa hai mẫu. Không có quota tổng corpus
tự động: A theo dõi dung lượng Drive và dừng giữa cohort khi gần giới hạn.

Benchmark tái sử dụng Adapter/Metrics v2: wall time callback gồm pacing/retry và
checkpoint, per-paper/discovery queried, HTTP transport attempts, sampled RAM
process-tree peak/mean và disk peak/final. Clone, pip, mount, audit/report ngoài
callback không nằm trong crawl wall time. RAM bao gồm kernel notebook và process
con; trung bình là **trung bình RSS các mẫu**, không phải time-weighted. Nếu thiếu
số đo/hard kill, giữ unknown/not_collected và known subtotal.

Trong một batch dùng aggregate của attempt IDs duy nhất, không cộng các report
lũy kế. Giữa cohort: cộng interval thời gian độc lập một lần, lấy max peak RAM/disk
khi cùng scope/backend, tính RAM mean bằng tổng sample bytes / tổng sample count;
không cộng peaks. Final corpus size là snapshot toàn corpus **cuối cùng**, không
cộng các snapshots ở từng cohort. Các cohort Resume muộn vẫn dùng scope corpus
chung. Chưa có công cụ tự tổng hợp toàn chiến dịch; giao C danh mục audits,
logs và reports gốc để đối soát, không tự coi bảng cuối mỗi cohort là tổng nhóm.
S2 phải được đo ở pha riêng sau này để benchmark M1 cuối bao gồm cả hai pha.

## Kiểm thử và giới hạn bàn giao

`tests/test_m1_production.py` dùng HTTP giả và corpus TEMP, kiểm tra lựa chọn đủ
20.000 vị trí, new/resume/audit, dừng chính sách, provenance/checksum và bảo vệ
dữ liệu. Không gọi API thật, mount Drive, cài package hoặc chạy 20.000 ID trong tests.
Notebook mới cần A kiểm tra một cohort trên Colab trước khi tăng cửa sổ chạy.
Không có distributed writer lock giữa nhiều VM; chỉ một runtime được phép ghi.
