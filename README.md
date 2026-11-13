# Thu thập dữ liệu arXiv — Nhóm 10

[![Kiểm thử tự động](https://github.com/Toan-ten/Arxiv-Project-Group10/actions/workflows/tests.yml/badge.svg)](https://github.com/Toan-ten/Arxiv-Project-Group10/actions/workflows/tests.yml)
[![Python 3.11 và 3.12](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue.svg)](https://www.python.org/)
[![Mở sổ tay Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/Toan-ten/Arxiv-Project-Group10/blob/feat%2Fm1-pipeline-optimization/notebooks/m1_pipeline_thu_thap_va_kiem_toan.ipynb)

Kho mã này chứa quy trình thu thập và kiểm toán dữ liệu arXiv của Nhóm 10. Mỗi thành viên xử lý dải ID đã cấu hình; tổng phạm vi gồm 20.000 vị trí. Số vị trí được giao, số bài báo tồn tại và số phiên bản là các đại lượng riêng.

## Bắt đầu

Quy trình Colab nằm trong [sổ tay M1](notebooks/m1_pipeline_thu_thap_va_kiem_toan.ipynb). Mọi thành viên dùng cùng danh sách ID tất định theo MSSV, rồi chạy các ô arXiv, Semantic Scholar và kiểm toán/đối soát. Mỗi pha có điểm khôi phục riêng; có thể chạy S2 trước arXiv hoặc chạy lại riêng một pha.

Notebook vận hành tải mã theo SHA trong `M1_RUNTIME_COMMIT`. Mã trên nhánh đang đọc có thể khác mã tại SHA đã ghim; mở notebook từ một nhánh khác không tự chuyển phiên Colab sang mã của nhánh đó. Xem [quy tắc ghim và phát hành runtime](docs/guides/colab.md).

1. Mở sổ tay bằng Colab và chọn CPU.
2. Thêm `KAGGLE_API_TOKEN` và `SEMANTIC_SCHOLAR_API_KEY` vào Colab Secrets, rồi cấp quyền truy cập cho notebook.
3. Nhập MSSV theo [`config/group10_id_ranges.json`](config/group10_id_ranges.json), giữ `COHORT_LIMIT = None` để xử lý đủ 5.000 ID, rồi chạy ô cấu hình.
4. Chạy ô arXiv để xác minh ID, phiên bản và tải mã nguồn; sau đó chạy ô S2.
5. Sau khi hai pha đủ bằng chứng, chạy ô kiểm toán/đối soát. Nếu một pha dừng, chạy lại riêng ô đó với cùng MSSV và Drive.

Khi gọi riêng pha kiểm toán như trong notebook, kiểm tra `is_reconciled = True` và `export_status = "exported"` cho phạm vi ID đã chọn. Hàm tương thích `run_m1_member()` trả thêm `handoff_status = "verified"` khi kết quả kiểm toán đạt điều kiện đối soát. Việc tải xong mã nguồn hoặc kết thúc một phiên không tự chứng minh M1 hoàn tất; phạm vi và kết quả kiểm toán phải được xác minh cùng nhau.

## Phạm vi ID

| MSSV | Thành viên | Dải ID | Số vị trí | Lô |
|---|---|---|---:|---:|
| `24120010` | Trần Văn Tấn Khôi | `2508.07872`–`2508.12871` | 5.000 | 1–50 |
| `24120035` | Lê Thành Đạt | `2507.16659`–`2507.21658` | 5.000 | 51–100 |
| `24120232` | Nguyễn Văn Toàn | `2502.08235`–`2502.13234` | 5.000 | 101–150 |
| `24120260` | Trương Tuấn Anh | `2501.02642`–`2501.07641` | 5.000 | 151–200 |

Cấu hình JSON là nguồn duy nhất để tạo các cặp thành viên–ID. `COHORT_LIMIT` chỉ thu hẹp số lô trong một lượt; mọi giai đoạn M1 vẫn chạy.

## Dữ liệu và trạng thái

- arXiv xác nhận bài báo và danh sách phiên bản. Quy trình tải mã nguồn riêng cho từng phiên bản, giữ các tệp `.tex`, `.bib`, `.bbl` và loại tệp nhị phân khỏi kho dữ liệu.
- Pha arXiv yêu cầu `KAGGLE_API_TOKEN` và snapshot/index Kaggle hợp lệ trước khi discovery bắt đầu. Atom xác nhận ID và phiên bản theo lô tối đa 20 ID; mỗi phiên bản vẫn được tải riêng từ endpoint arXiv có mã phiên bản. Snapshot hoặc index không sẵn sàng sẽ dừng pha, không chuyển sang đường arXiv-only.
- Semantic Scholar cung cấp siêu dữ liệu và tài liệu tham khảo theo trang trong pha riêng. Trạng thái S2 `not_found` không kết luận arXiv không tồn tại; lỗi API không được xem là danh sách tài liệu tham khảo rỗng.
- Pha S2 yêu cầu `SEMANTIC_SCHOLAR_API_KEY`; M1 không gửi request S2 ẩn danh.
- Điểm khôi phục, manifest và mã kiểm tra giúp tiếp tục sau khi phiên dừng. Lỗi toàn vẹn hoặc dữ liệu M1 còn thiếu sẽ chặn bàn giao.

Kho dữ liệu dùng hợp đồng lưu trữ v2. Cấu trúc, xuất xứ và ý nghĩa các trường được mô tả trong [hợp đồng lưu trữ](docs/contracts/corpus_storage_contract.md) và [hợp đồng dữ liệu nguồn](docs/contracts/source_data_contracts.md).

## Môi trường và kiểm tra

CI chạy trên Python 3.11 và 3.12. Các lệnh dưới đây giả định `python3` trỏ tới một trong hai phiên bản này:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
```

Chạy kiểm thử và kiểm tra cú pháp:

```bash
pytest
python -m compileall src scripts tests
```

Các kiểm thử không gọi dịch vụ bên ngoài và không cần khóa API. Phản hồi HTTP được giả lập; một số kiểm thử gián đoạn dùng máy chủ loopback do chính kiểm thử khởi tạo. Có thể dùng cấu hình chặn kết nối ngoài và cô lập thư mục tạm trong [hướng dẫn đóng góp](CONTRIBUTING.md).

## Cấu trúc kho mã

```text
config/      Dải ID và tập mẫu
docs/        Hướng dẫn, hợp đồng dữ liệu và kiến trúc
notebooks/   Sổ tay Colab M1
scripts/     Công cụ báo cáo và tổng hợp dữ liệu nhóm
src/         API công khai, các pha M1 và mô-đun xử lý nền
tests/       Kiểm thử ngoại tuyến
```

Notebook nhập các API từ `src.m1_workflow`. Module này xuất lại các điểm vào của `src.m1_phases`; phần triển khai hiện hành được tổ chức theo trách nhiệm trong `src/m1_phase_modules/`. Các đường import công khai và chữ ký hàm được giữ tương thích.

Xem [hướng dẫn vận hành Colab](docs/guides/colab.md), [quy trình M1](docs/operations/m1_production.md) và [kiến trúc các pha M1](docs/architecture/repository-structure.md).

## Đóng góp và giấy phép

Quy trình phát triển nằm trong [`CONTRIBUTING.md`](CONTRIBUTING.md). Kho mã được phân phối theo [Giấy phép MIT](LICENSE).
