# Hướng dẫn đóng góp

Tài liệu này quy định cách sửa mã nguồn, tài liệu và kiểm thử trong kho mã Nhóm 10.

## Nguyên tắc

- Không nới lỏng kiểm tra toàn vẹn để làm cho kiểm thử đạt. Khi trạng thái hoặc dữ liệu không xác định, giữ nguyên bằng chứng và báo lỗi.
- Không sửa nội dung `.tex`, `.bib` hoặc `.bbl` đã tải. Tệp nhị phân không được đưa vào corpus; checksum trong manifest phải khớp dữ liệu trên đĩa.
- Bộ kiểm thử phải chạy ngoại tuyến. Mọi HTTP, socket và truy cập dịch vụ bên ngoài cần được giả lập.
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

CI hỗ trợ Python 3.11 và 3.12. Tạo môi trường phát triển và cài thư viện:

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

## Quy ước mã nguồn và tài liệu

- Dùng PEP 8, bốn khoảng trắng, type hints cho API công khai và docstring nêu rõ hành vi.
- Tên module và kiểm thử dùng `snake_case`; tên tệp kiểm thử bắt đầu bằng `test_`.
- Viết tài liệu bằng tiếng Việt tự nhiên, câu ngắn và rõ chủ thể. Giữ nguyên tên API, biến, trường dữ liệu, giao thức và thông báo chính thức khi dịch làm sai nghĩa.
- Tiêu đề, nội dung giải thích, ghi chú trong `src/` và `scripts/`, cùng văn bản notebook dùng tiếng Việt. Chỉ giữ tiếng Anh cho tên kỹ thuật cần chính xác như tên API, biến, schema, trạng thái máy đọc được và giao thức. Ghi chú chỉ được giữ khi giải thích quyết định hoặc ràng buộc không hiển nhiên; xóa ghi chú chỉ nhắc lại lệnh kế tiếp.
- Tài liệu hướng dẫn phải khớp mã hiện hành. Nêu rõ phạm vi, điều kiện chạy, kết quả và cách tiếp tục sau khi dừng.

## Tích hợp liên tục

GitHub Actions chạy kiểm thử ngoại tuyến trên Python 3.11 và 3.12 khi có thay đổi trên nhánh hoặc pull request. Bản bàn giao cần có cả hai lượt kiểm thử đạt.
