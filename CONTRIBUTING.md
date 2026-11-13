# Hướng dẫn đóng góp

Tài liệu này quy định cách sửa mã nguồn, tài liệu và kiểm thử trong kho mã Nhóm 10.

## Nguyên tắc

- Không nới lỏng kiểm tra toàn vẹn để làm cho kiểm thử đạt. Khi trạng thái hoặc dữ liệu không xác định, giữ nguyên bằng chứng và báo lỗi.
- Không sửa nội dung `.tex`, `.bib` hoặc `.bbl` đã tải. Tệp nhị phân không được đưa vào corpus; checksum trong manifest phải khớp dữ liệu trên đĩa.
- Bộ kiểm thử phải chạy ngoại tuyến. Mọi truy cập dịch vụ bên ngoài cần được giả lập; máy chủ loopback do kiểm thử khởi tạo chỉ phục vụ các tình huống I/O và gián đoạn có kiểm soát.
- Không ghi credential vào mã nguồn, log, báo cáo hoặc notebook. Dùng biến môi trường hay kho bí mật của môi trường chạy.
- Không sửa archive, checkpoint hay báo cáo kiểm toán cũ để che lỗi. Thay đổi hợp đồng phải có kiểm thử tương ứng.

## Nhánh và commit

Đặt tên nhánh theo loại công việc, chẳng hạn `feat/...`, `fix/...`, `refactor/...` hoặc `docs/...`.

Thông điệp commit dùng dạng:

```text
<type>(<scope>): <mô tả ngắn>
```

Các loại thường dùng: `feat`, `fix`, `refactor`, `test`, `docs`, `ci` và `chore`. Viết phần mô tả ngắn bằng tiếng Việt khi phù hợp.

Không viết lại lịch sử nhánh đã chia sẻ. Trường hợp bàn giao cần gộp lịch sử phải được thống nhất trước và dùng `--force-with-lease` để tránh ghi đè thay đổi mới trên remote.

## Môi trường và kiểm tra

CI hỗ trợ Python 3.11 và 3.12. Chọn một trong hai phiên bản làm trình thông dịch `python3`, rồi tạo môi trường phát triển và cài thư viện:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
```

Trước khi bàn giao, chạy:

```bash
pytest
python -m compileall src scripts tests
git status --short
```

Không thêm số kiểm thử cố định vào tài liệu; số lượng thay đổi theo bộ kiểm thử. Cần giải quyết mọi lỗi trước khi đẩy mã.

Trên Linux hoặc môi trường CI tương ứng, có thể chặn DNS và kết nối ra ngoài trong cả tiến trình chính lẫn tiến trình Python con bằng lệnh sau:

```bash
PYTHONPATH="$PWD/tests/offline_runtime:$PWD" python -m pytest
```

Mã khởi tạo kiểm thử trong `tests/offline_runtime/sitecustomize.py` vẫn cho phép kết nối loopback và đặt thư mục scratch mặc định dưới `local_root` của từng kiểm thử. Cơ chế này chỉ áp dụng khi thư mục chứa mã khởi tạo nằm trên `PYTHONPATH`; không thêm đường dẫn đó vào môi trường vận hành. Các kiểm thử đối chiếu M1 đọc mã của phiên bản đối chiếu từ lịch sử Git, vì vậy cần giữ đủ lịch sử của nhánh; workflow CI dùng `fetch-depth: 0`.

## Quy ước mã nguồn và tài liệu

- Dùng PEP 8, bốn khoảng trắng, type hints cho API công khai và docstring nêu rõ hành vi.
- Tên module và kiểm thử dùng `snake_case`; tên tệp kiểm thử bắt đầu bằng `test_`.
- Viết tài liệu bằng tiếng Việt tự nhiên, câu ngắn và rõ chủ thể. Giữ nguyên tên API, biến, trường dữ liệu, giao thức và thông báo chính thức khi dịch làm sai nghĩa.
- Tiêu đề, nội dung giải thích, ghi chú trong `src/` và `scripts/`, cùng văn bản notebook dùng tiếng Việt. Chỉ giữ tiếng Anh cho tên kỹ thuật cần chính xác như tên API, biến, schema, trạng thái máy đọc được và giao thức. Ghi chú chỉ được giữ khi giải thích quyết định hoặc ràng buộc không hiển nhiên; xóa ghi chú chỉ nhắc lại lệnh kế tiếp.
- Tài liệu hướng dẫn phải khớp mã hiện hành. Nêu rõ phạm vi, điều kiện chạy, kết quả và cách tiếp tục sau khi dừng.

Khi sửa các pha M1, giữ các điểm vào ở `src.m1_workflow` và `src.m1_phases`. Gói `src.m1_phase_modules` chứa triển khai nội bộ; việc tách hoặc di chuyển hàm phải giữ chữ ký công khai, loại ngoại lệ, thứ tự ghi dữ liệu, schema và các cơ chế thay thế phụ thuộc đã có kiểm thử. Xem [kiến trúc M1](docs/architecture/repository-structure.md) để xác định module chịu trách nhiệm.

Tài liệu chính thức mô tả cấu trúc, hợp đồng và quy trình có thể tái hiện. Báo cáo rà soát từng đợt, nhật ký thử nghiệm, bản sao dữ liệu và notebook đối chiếu có đầu ra cần được lưu riêng ngoài tập commit phát hành. Khi bàn giao thay đổi cấu trúc, cập nhật sơ đồ kiến trúc, mô tả API và hướng dẫn vận hành liên quan; không thay đổi định nghĩa dữ liệu hoặc chỉ số nếu hành vi chương trình giữ nguyên.

## Tích hợp liên tục

GitHub Actions chạy kiểm thử ngoại tuyến trên Python 3.11 và 3.12. Workflow hiện kích hoạt khi push vào `main`, `master`, `feat/**`, `fix/**`; khi mở pull request tới các nhánh đích thuộc cùng tập này; hoặc khi chạy thủ công bằng `workflow_dispatch`. Push lên `refactor/**` hoặc `docs/**` không tự kích hoạt workflow. Bản bàn giao cần xác minh kết quả của cả hai phiên bản Python; kết quả chạy local không thay thế trạng thái CI trên remote.
