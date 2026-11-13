# Cấu trúc và thành phần của kho mã

Kho mã chứa notebook Colab M1, cấu hình dải ID, mã xử lý, công cụ tổng hợp, kiểm thử ngoại tuyến và tài liệu vận hành. Bảng dưới đây nêu vai trò các thành phần chính; danh sách tệp cụ thể có thể thay đổi theo commit.

```text
Arxiv-Project-Group10/
├── config/
│   ├── group10_id_ranges.json       # Phân công 20.000 vị trí ID
│   └── pilots/                      # Tập ID mẫu
├── docs/
│   ├── architecture/                # Cấu trúc kho mã
│   ├── contracts/                   # Hợp đồng dữ liệu, lưu trữ và thống kê
│   ├── guides/                      # Hướng dẫn các thành phần
│   └── operations/                  # Vận hành và đo lường
├── notebooks/
│   └── m1_pipeline_thu_thap_va_kiem_toan.ipynb
├── scripts/                         # Kiểm toán, tổng hợp và báo cáo
├── src/
│   ├── m1_workflow.py                # Đường import dùng trong notebook
│   ├── m1_phases.py                  # API tương thích và điều phối tuần tự
│   ├── m1_phase_modules/             # Triển khai nội bộ theo trách nhiệm
│   │   ├── common.py
│   │   ├── checkpoint.py
│   │   ├── arxiv_phase.py
│   │   ├── s2_evidence.py
│   │   ├── s2_phase.py
│   │   ├── audit_phase.py
│   │   └── benchmark_summary.py
│   └── ...                          # Client, pipeline, lưu trữ và kiểm toán nền
└── tests/                           # Kiểm thử ngoại tuyến
```

## Quy tắc tổ chức

- `config/group10_id_ranges.json` là nguồn duy nhất cho phân công ID. Notebook không gán mặc định MSSV hoặc lô.
- Notebook gọi riêng các pha M1 qua `src.m1_workflow`; mã xử lý và kiểm tra toàn vẹn nằm trong `src/`; công cụ kiểm toán và tổng hợp nằm trong `scripts/`.
- Chạy mẫu và tiếp tục từ điểm khôi phục dùng cùng luồng M1.
- Kiểm thử không truy cập dịch vụ bên ngoài; các máy chủ loopback phục vụ kiểm thử I/O có kiểm soát. Tên API, schema, trường dữ liệu và mã trạng thái được giữ nguyên.
- Tài liệu trong kho mã dùng tiếng Việt. Nội dung cần khớp hành vi mã hiện tại và không thay đổi hợp đồng dữ liệu chỉ vì biên tập.

## Các lớp API M1

Notebook nhập `build_member_selection`, `run_m1_arxiv_member`, `run_m1_s2_member` và `run_m1_audit_member` từ `src.m1_workflow`. Module này xuất lại các điểm vào của `src.m1_phases`, là lớp API giữ tương thích với các bên gọi hiện có. Hàm `run_m1_member` nằm tại lớp này và gọi tuần tự arXiv → S2 → kiểm toán, dừng chuỗi khi pha trước báo dừng.

Gói `src.m1_phase_modules` chứa triển khai nội bộ. Bên gọi vận hành tiếp tục sử dụng các đường import công khai; chữ ký của hàm nội bộ không phải hợp đồng dành cho notebook.

| Module nội bộ | Trách nhiệm |
|---|---|
| `common.py` | Ngoại lệ M1, phiên bản schema, kiểm tra secret và lựa chọn ID/cohort tất định |
| `checkpoint.py` | Đọc/ghi JSON, SHA-256, selection, trạng thái bắt đầu, khóa ghi và ghi nhận phiên |
| `arxiv_phase.py` | Chuẩn bị và kiểm tra xuất xứ Kaggle, điều phối arXiv, điểm khôi phục mã nguồn và tự tiếp tục sau discovery 429 |
| `s2_evidence.py` | Manifest S2, đọc/ghi bằng chứng, đối chiếu trạng thái, định danh và checksum |
| `s2_phase.py` | Dùng lại cache, gom metadata, lấy references, xử lý ngân sách và tiếp tục S2 |
| `audit_phase.py` | Xác minh mã nguồn, kiểm tra trước hợp nhất, hợp nhất S2, kiểm toán và điều kiện xuất |
| `benchmark_summary.py` | Đọc quan sát các phiên và tổng hợp phép đo theo thành viên |

Các pha dùng `common` và `checkpoint`; S2 và kiểm toán dùng chung `s2_evidence`. Pha kiểm toán còn dùng bộ kiểm tra xuất xứ arXiv và bộ tổng hợp benchmark. Các module nội bộ không import ngược về `m1_phases` hoặc `m1_workflow`, nhằm tránh vòng phụ thuộc.

`FastProductionRunner`, lớp lưu trữ, các mô-đun giao tiếp với nhà cung cấp và bộ ghi nhận phép đo tiếp tục nằm trong các module nền. Lớp `m1_phases` truyền phụ thuộc cần thiết tại thời điểm gọi để giữ cơ chế thay thế phụ thuộc trong kiểm thử (monkeypatch); ví dụ, thay `FastProductionRunner` hoặc `_mark_archive_arxiv_only` trong kiểm thử vẫn tác động tới lời gọi qua lớp API. Phần triển khai gộp lịch sử trong `m1_workflow.py` được giữ ở phạm vi riêng; notebook hiện hành dùng các pha độc lập.

Cấu trúc này mô tả mã trong checkout đang đọc. Runtime Colab do SHA đã ghim trong notebook quyết định; xem [quy tắc ghim runtime](../guides/colab.md).

## Thành phần và kiểm thử liên quan

| Thành phần | Vai trò | Kiểm thử liên quan |
|---|---|---|
| `config/group10_id_ranges.json` | Phân công 20.000 vị trí ID cho bốn thành viên | `tests/test_id_ranges.py` |
| `config/pilots/` | Tập con ID dùng để kiểm tra quy trình | `tests/test_pilot_sets.py` |
| `notebooks/m1_pipeline_thu_thap_va_kiem_toan.ipynb` | Điểm vào Colab duy nhất cho M1 | `tests/test_colab_release_qualification.py` |
| `src/` | Xác minh arXiv, tải source, lấy dữ liệu S2, lưu trữ, kiểm toán và đo lường | Kiểm thử ngoại tuyến theo mô-đun |
| `src/m1_phases.py` | API tương thích và điều phối các pha | `tests/test_m1_workflow.py`, `tests/test_m1_refactor_equivalence.py` |
| `src/m1_phase_modules/` | Các trách nhiệm nội bộ của M1 | `tests/test_m1_phases.py`, `tests/test_m1_arxiv_phase_policy.py`, `tests/test_m1_phase_cleanup.py` |
| `scripts/aggregate_m1_group.py` | Kiểm tra độ bao phủ ID và tính toàn vẹn archive | `tests/test_group_aggregator.py` |
| `scripts/aggregate_m1_group_benchmark.py` | Tổng hợp phép đo theo thành viên và phiên | `tests/test_group_benchmark_aggregator.py` |
| `scripts/generate_m1_report.py` | Tạo báo cáo thống kê và kiểm toán M1 | `tests/test_generate_m1_report.py` |
| `docs/contracts/` | Định nghĩa dữ liệu, lưu trữ và thống kê | Mã lưu trữ và kiểm thử hợp đồng |
| `docs/guides/`, `docs/operations/` | Hướng dẫn thành phần và vận hành | README và kiểm thử tương ứng |
| `tests/` | Kiểm thử ngoại tuyến, không gọi dịch vụ thật | `pytest` |

## Bảo trì

- Chỉ gỡ thành phần sau khi kiểm tra tham chiếu trong mã, notebook, tài liệu và kiểm thử.
- Không xóa dữ liệu, archive, log hoặc bằng chứng kiểm toán để dọn mã.
- Giữ nguyên schema và cách tính chỉ số khi chỉ biên tập tài liệu. Nếu định nghĩa chỉ số thay đổi, cập nhật hợp đồng và kiểm thử tương ứng.
