# Thu thập dữ liệu arXiv — Nhóm 10

[![Kiểm thử tự động](https://github.com/Toan-ten/Arxiv-Project-Group10/actions/workflows/tests.yml/badge.svg)](https://github.com/Toan-ten/Arxiv-Project-Group10/actions/workflows/tests.yml)
[![Python 3.11 và 3.12](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue.svg)](https://www.python.org/)
[![Mở sổ tay Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/Toan-ten/Arxiv-Project-Group10/blob/feat%2Fm1-t02-colab-resume/notebooks/m1_pipeline_thu_thap_va_kiem_toan.ipynb)

Kho mã này chứa quy trình thu thập và kiểm toán dữ liệu arXiv của Nhóm 10. Mỗi thành viên xử lý dải ID đã cấu hình; tổng phạm vi gồm 20.000 vị trí. Số vị trí được giao, số bài báo tồn tại và số phiên bản là các đại lượng riêng.

## Bắt đầu

Quy trình Colab nằm trong [sổ tay M1](notebooks/m1_pipeline_thu_thap_va_kiem_toan.ipynb). Mọi thành viên dùng cùng một luồng: lập chỉ mục ứng viên Kaggle, xác minh ID và phiên bản bằng arXiv, tải mã nguồn, lấy siêu dữ liệu và tài liệu tham khảo từ Semantic Scholar, lưu checkpoint rồi kiểm toán.

1. Mở sổ tay bằng Colab và chọn CPU.
2. Chạy ô thiết lập để kết nối Drive, cài thư viện và xác nhận commit được ghim.
3. Nhập MSSV theo [`config/group10_id_ranges.json`](config/group10_id_ranges.json). Để chạy đủ dải, giữ `COHORT_LIMIT = None`.
4. Thêm `KAGGLE_API_TOKEN` và `SEMANTIC_SCHOLAR_API_KEY` vào Colab Secrets, bật Notebook access rồi chạy ô M1.
5. Nếu phiên dừng vì giới hạn dịch vụ, chờ dịch vụ ổn định rồi chạy lại cùng sổ tay, MSSV và Drive để tiếp tục từ điểm khôi phục.

Chỉ trạng thái `handoff_status = verified` mới xác nhận lô đạt điều kiện bàn giao. Một phiên dừng, mã nguồn đã tải xong hoặc báo cáo đã đối chiếu không tự chứng minh M1 hoàn tất.

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
- Kaggle chỉ cung cấp siêu dữ liệu và ngày phiên bản để đối chiếu. Quy trình chỉ dùng ứng viên khớp với kết quả arXiv đã xác minh; nếu không, quy trình lấy dữ liệu từ arXiv.
- Semantic Scholar cung cấp siêu dữ liệu bài báo và tài liệu tham khảo theo trang. Trạng thái từng giai đoạn được lưu riêng; lỗi giới hạn API không được xem là danh sách tài liệu tham khảo rỗng.
- Điểm khôi phục, manifest và checksum giúp tiếp tục sau khi phiên dừng. Lỗi toàn vẹn hoặc dữ liệu M1 còn thiếu sẽ chặn bàn giao.

Kho dữ liệu dùng hợp đồng lưu trữ v2. Cấu trúc, xuất xứ và ý nghĩa các trường được mô tả trong [hợp đồng lưu trữ](docs/contracts/corpus_storage_contract.md) và [hợp đồng dữ liệu nguồn](docs/contracts/source_data_contracts.md).

## Môi trường và kiểm tra

CI chạy trên Python 3.11 và 3.12. Để thiết lập môi trường phát triển:

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

Các kiểm thử thay kết nối mạng bằng dữ liệu giả lập. Không cần khóa API để chạy chúng.

## Cấu trúc kho mã

```text
config/      Dải ID và tập mẫu
docs/        Hướng dẫn, hợp đồng dữ liệu và kiến trúc
notebooks/   Sổ tay Colab M1
scripts/     Công cụ báo cáo và tổng hợp dữ liệu nhóm
src/         Quy trình, mô-đun gọi API, lưu trữ, kiểm toán và đo lường
tests/       Kiểm thử ngoại tuyến
```

Xem [hướng dẫn vận hành Colab](docs/guides/colab.md), [quy trình M1](docs/operations/m1_production.md) và [cấu trúc kho mã](docs/architecture/repository-structure.md).

## Đóng góp và giấy phép

Quy trình phát triển nằm trong [`CONTRIBUTING.md`](CONTRIBUTING.md). Repository được phân phối theo [MIT License](LICENSE).
