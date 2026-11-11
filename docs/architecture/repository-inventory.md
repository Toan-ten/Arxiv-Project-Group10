# Thành phần của quy trình M1

Bảng dưới đây nêu vai trò của các thư mục và điểm vào chính. Danh sách tệp có thể thay đổi theo commit.

| Thành phần | Vai trò | Kiểm thử liên quan |
|---|---|---|
| `config/group10_id_ranges.json` | Phân công 20.000 vị trí ID cho bốn thành viên | `tests/test_id_ranges.py` |
| `config/pilots/` | Tập con ID dùng để kiểm tra quy trình | `tests/test_pilot_sets.py` |
| `notebooks/m1_pipeline_thu_thap_va_kiem_toan.ipynb` | Điểm vào Colab duy nhất cho M1 | `tests/test_colab_release_qualification.py` |
| `src/` | Xác minh arXiv, tải source, lấy dữ liệu S2, lưu trữ, kiểm toán và đo lường | Các kiểm thử ngoại tuyến theo mô-đun |
| `scripts/aggregate_m1_group.py` | Kiểm tra độ bao phủ ID và tính toàn vẹn archive | `tests/test_group_aggregator.py` |
| `scripts/aggregate_m1_group_benchmark.py` | Tổng hợp phép đo theo thành viên và phiên | `tests/test_group_benchmark_aggregator.py` |
| `scripts/generate_m1_report.py` | Tạo báo cáo thống kê và kiểm toán M1 | `tests/test_generate_m1_report.py` |
| `docs/contracts/` | Định nghĩa dữ liệu, lưu trữ và thống kê | Mã lưu trữ và kiểm thử hợp đồng |
| `docs/guides/`, `docs/operations/` | Hướng dẫn thành phần và vận hành | README và kiểm thử tương ứng |
| `tests/` | Kiểm thử ngoại tuyến, không gọi dịch vụ thật | `pytest` |

## Quy tắc bảo trì

- Giữ một notebook vận hành. Mẫu và resume dùng cùng quy trình.
- Chỉ gỡ thành phần sau khi tìm tham chiếu trong mã, tài liệu và kiểm thử. Không xóa dữ liệu, archive, log hoặc bằng chứng kiểm toán để dọn mã.
- Giữ nguyên schema và cách tính chỉ số khi chỉ biên tập tài liệu. Nếu định nghĩa chỉ số đổi, cập nhật hợp đồng và kiểm thử.
