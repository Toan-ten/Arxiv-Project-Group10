# Thu thập dữ liệu arXiv — Nhóm 10

[![Kiểm thử tự động](https://github.com/Toan-ten/Arxiv-Project-Group10/actions/workflows/tests.yml/badge.svg)](https://github.com/Toan-ten/Arxiv-Project-Group10/actions/workflows/tests.yml)
[![Python 3.11 và 3.12](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue.svg)](https://www.python.org/)
[![Mở sổ tay Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/Toan-ten/Arxiv-Project-Group10/blob/refactor%2Fm1-audit-normalization/notebooks/m1_pipeline_thu_thap_va_kiem_toan.ipynb)

Kho mã này chứa một quy trình thu thập và kiểm toán dữ liệu arXiv của Nhóm 10. Mỗi thành viên xử lý dải ID đã cấu hình; tổng phạm vi gồm 20.000 vị trí. Số vị trí được giao, số bài báo tồn tại và số lần nộp là các đại lượng riêng.

## Bắt đầu

Quy trình Colab nằm trong [sổ tay M1](notebooks/m1_pipeline_thu_thap_va_kiem_toan.ipynb). Mỗi thành viên dùng danh sách ID xác định theo MSSV và thực hiện theo thứ tự: thu thập arXiv, thu thập Semantic Scholar, kiểm định bằng chứng rồi tạo bảng làm việc. Hai pha thu thập lưu điểm khôi phục riêng; kiểm định lưu báo cáo tiến độ; chuẩn hóa tạo lại bảng từ bằng chứng đã lưu.

Notebook vận hành tải mã theo SHA trong `M1_RUNTIME_COMMIT`. Ô cấu hình xác minh commit thực thi trước khi nhập mã M1. Xem [quy tắc ghim và phát hành runtime](docs/guides/colab.md).

1. Mở sổ tay bằng Colab và chọn CPU.
2. Thêm `KAGGLE_API_TOKEN` và `SEMANTIC_SCHOLAR_API_KEY` vào Colab Secrets, rồi cấp quyền truy cập cho notebook.
3. Nhập MSSV theo [`config/group10_id_ranges.json`](config/group10_id_ranges.json), giữ `COHORT_LIMIT = None` để xử lý đủ 5.000 ID, rồi chạy ô cấu hình.
4. Chạy ô arXiv để xác minh ID, các lần nộp và tải mã nguồn; sau đó chạy ô S2.
5. Sau hai bước thu thập, chạy ô kiểm định. Notebook chỉ chuẩn hóa khi kiểm định hoàn tất; nếu bị dừng, chạy lại ô đó với cùng MSSV và Drive root.

Notebook gọi riêng `run_m1_validation_member()` và `run_m1_normalization_member()` sau hai pha thu thập. Kiểm định đọc điểm khôi phục arXiv/S2, khôi phục archive vào ổ cục bộ và ghi báo cáo cùng tiến độ lên Drive; không sửa corpus, archive nguồn hay điểm khôi phục arXiv/S2. Khi kiểm định hoàn tất, chuẩn hóa tạo bảng làm việc. `run_m1_official_export_member()` chỉ tạo bảng chính thức khi bằng chứng đáp ứng điều kiện trên toàn bộ phạm vi được giao.

Khi tạo bảng làm việc thành công, ô 4 đọc `papers.parquet` qua `WORKING_RESULT["output_dir"]`, in kích thước, tên cột và số giá trị thiếu, rồi hiển thị 20 dòng đầu. Ô này chỉ đọc bảng làm việc.

## Phạm vi ID

| MSSV | Thành viên | Dải ID | Số vị trí | Lô |
|---|---|---|---:|---:|
| `24120010` | Trần Văn Tấn Khôi | `2508.07872`–`2508.12871` | 5.000 | 1–50 |
| `24120035` | Lê Thành Đạt | `2507.16659`–`2507.21658` | 5.000 | 51–100 |
| `24120232` | Nguyễn Văn Toàn | `2502.08235`–`2502.13234` | 5.000 | 101–150 |
| `24120260` | Trương Tuấn Anh | `2501.02642`–`2501.07641` | 5.000 | 151–200 |

Cấu hình JSON là nguồn duy nhất để tạo các cặp thành viên–ID. `COHORT_LIMIT` chỉ thu hẹp số lô trong một lượt; mọi giai đoạn M1 vẫn chạy.

## Dữ liệu và trạng thái

- arXiv xác nhận bài báo và các lần nộp. Quy trình tải mã nguồn riêng cho từng lần nộp, giữ các tệp `.tex`, `.bib`, `.bbl` và loại tệp nhị phân khỏi kho dữ liệu.
- Pha arXiv yêu cầu `KAGGLE_API_TOKEN` và snapshot/index Kaggle hợp lệ trước khi discovery bắt đầu. Atom xác nhận ID và các lần nộp theo lô tối đa 20 ID; mã nguồn của từng lần nộp vẫn được tải riêng theo hậu tố `vN`. Snapshot hoặc index không sẵn sàng sẽ dừng pha.
- Semantic Scholar cung cấp siêu dữ liệu và tài liệu tham khảo trong pha riêng. Notebook dùng `batch_m1`, gom tối đa 100 ID và phân trang khi references chưa được xác minh. Trạng thái S2 `not_found` không kết luận arXiv không tồn tại; lỗi API không được xem là danh sách tài liệu tham khảo rỗng.
- Pha S2 yêu cầu `SEMANTIC_SCHOLAR_API_KEY`; M1 không gửi request S2 ẩn danh.
- Điểm khôi phục, manifest và mã kiểm tra giúp tiếp tục sau khi phiên dừng. Lỗi toàn vẹn hoặc dữ liệu M1 còn thiếu sẽ chặn bàn giao.

Kết thúc thu thập S2 và khớp `referenceCount` là hai kết quả riêng. Nếu đã hết trang nhưng số mục vẫn lệch, chương trình giữ dữ liệu thực nhận, ghi cảnh báo chất lượng và không tự gọi lại chỉ vì chênh lệch đó. Bảng làm việc lấy ngày của tài liệu tham khảo từ bằng chứng S2; nếu nguồn không cung cấp ngày thì để trống. Chi tiết nằm trong [hướng dẫn tích hợp S2](docs/guides/s2_integration.md).

Kho dữ liệu dùng lược đồ lưu trữ 2. Cấu trúc, xuất xứ và ý nghĩa các trường được mô tả trong [hợp đồng lưu trữ](docs/contracts/corpus_storage_contract.md) và [hợp đồng dữ liệu nguồn](docs/contracts/source_data_contracts.md).

## Môi trường và kiểm tra

CI chạy trên Python 3.11 và 3.12. Các lệnh dưới đây giả định `python3` trỏ tới Python 3.11 hoặc 3.12:

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

Notebook nhập các API từ `src.m1_workflow`. Module này xuất lại các điểm vào của `src.m1_phases`; phần xử lý nằm trong `src/m1_phase_modules/`. Hàm tuần tự `run_m1_member()` dùng cùng chiến lược S2 và chính sách tiếp tục 429 như notebook.

Xem [hướng dẫn vận hành Colab](docs/guides/colab.md), [quy trình M1](docs/operations/m1_production.md) và [kiến trúc các pha M1](docs/architecture/repository-structure.md).

## Xử lý M2

M2 đọc mã nguồn từ kho M1, xác định đường biên dịch, dựng cấu trúc nội dung và hợp nhất tài liệu tham khảo theo bài. Cài `requirements-m2.txt` rồi chạy `python -m scripts.run_m2 --corpus <thu-muc-M1> --output <thu-muc-ket-qua>`; xem [hướng dẫn M2](docs/m2.md) để biết điều kiện đầu vào và trạng thái xử lý.

Để kiểm tra và chuyển các archive `.bbl` riêng, chạy `python -m scripts.run_m2_bbl` với `--input` và `--output`. Công cụ này dùng cùng bộ đọc tài liệu tham khảo với M2, ghi BibTeX theo tệp và chỉ hợp nhất khi nguồn của từng phiên bản xác định. Cú pháp đầy đủ và cách đọc bằng chứng nằm trong [hướng dẫn chuyển BBL](docs/m2_bbl_pipeline.md).

## Đóng góp và giấy phép

Quy trình phát triển nằm trong [`CONTRIBUTING.md`](CONTRIBUTING.md). Kho mã được phân phối theo [Giấy phép MIT](LICENSE).
