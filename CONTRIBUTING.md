# Hướng Dẫn Đóng Góp (Contributing Guidelines) — Group 10

Tài liệu này quy định các tiêu chuẩn kỹ thuật, quy trình phát triển và yêu cầu kiểm thử đối với tất cả thành viên khi đóng góp mã nguồn hoặc tài liệu vào repository `Arxiv-Project-Group10`.

---

## 1. Nguyên Tắc Cốt Lõi (Core Principles)

1. **Tính đúng đắn là trên hết (Correctness > Convenience):**
   - Không được phép nới lỏng các ràng buộc kiểm toán (audit checks) hoặc bỏ qua lỗi để kiểm thử vượt qua.
   - Luôn tuân thủ nguyên tắc **fail-closed**: khi xảy ra bất kỳ lỗi kiểm toán, I/O hoặc định dạng không xác định, hệ thống phải coi đó là vi phạm tính toàn vẹn (integrity failure) hoặc trạng thái không hoàn tất thay vì âm thầm bỏ qua.

2. **Bảo toàn dữ liệu gốc (Storage Contract Integrity):**
   - Tuyệt đối không thay đổi nội dung tệp mã nguồn TeX (`.tex`) hoặc BibTeX (`.bib`). Không xóa bỏ câu lệnh hình ảnh, macro hoặc định dạng của tác giả.
   - Mọi tệp nhị phân (`.pdf`, `.png`, `.jpg`, `.synctex.gz`, v.v.) phải được loại bỏ khi trích xuất tarball.
   - Mã băm SHA-256 và kích thước byte trong `manifest.json` phải khớp 100% với dữ liệu thực tế trên đĩa.

3. **Kiểm thử ngoại tuyến tuyệt đối (Strict Offline Testing):**
   - Toàn bộ unit tests và integration tests trong thư mục `tests/` phải chạy hoàn toàn ngoại tuyến (offline).
   - Mọi truy vấn HTTP, socket mạng hoặc cuộc gọi bên ngoài đều phải được mock hoặc bọc qua opener giả lập. Việc phát sinh kết nối mạng thật trong quá trình chạy test suite là lỗi nghiêm trọng.

4. **Bảo toàn các mốc ghim lịch sử (Preserve Historical Pins):**
   - Không thay đổi `CODE_COMMIT` lịch sử (`489a82fd959ccc6cced7c59fc309255e4421bef2`) trong các sổ tay hoặc tài liệu nghiệm thu đã được phê duyệt.
   - Không xóa bỏ hoặc sửa đổi các báo cáo kiểm toán lịch sử trong thư mục `reports/remediation/`.

---

## 2. Quy Trình Nhánh & Cam Kết (Branching & Commits)

### 2.1. Quy ước đặt tên nhánh (Branch Naming)

- Tính năng / Module mới: `feat/<tên-tính-năng>` (ví dụ: `feat/m1-fast-production`)
- Sửa lỗi / Vá lỗi kiểm toán: `fix/<tên-lỗi>` (ví dụ: `fix/fail-closed-inspection`)
- Tái cấu trúc / Dọn dẹp: `refactor/<tên-nội-dung>` (ví dụ: `refactor/repo-structure`)
- Tài liệu: `docs/<tên-tài-liệu>` (ví dụ: `docs/handoff-guide`)

### 2.2. Quy ước thông điệp cam kết (Conventional Commits)

Mỗi commit message phải tuân thủ định dạng:
```text
<type>(<scope>): <mô tả ngắn gọn>

[Nội dung chi tiết nếu cần]
```

Các `<type>` được chấp nhận:
- `feat`: Tính năng hoặc module mới.
- `fix`: Sửa lỗi logic, sửa bug kiểm toán hoặc khôi phục tính toàn vẹn.
- `refactor`: Tái cấu trúc mã nguồn hoặc tổ chức thư mục mà không thay đổi hành vi nghiệp vụ.
- `test`: Bổ sung, cập nhật hoặc sửa chữa unit tests / regression tests.
- `docs`: Cập nhật hoặc bổ sung tài liệu kỹ thuật, báo cáo nghiệm thu.
- `ci`: Chỉnh sửa cấu hình GitHub Actions CI, workflow hoặc test runner.
- `chore`: Các tác vụ bảo trì phụ thuộc, cấu hình môi trường.

*Lưu ý:* Tuyệt đối không dùng cờ `--force` (`git push -f`) khi đẩy lên các nhánh cộng tác chung.

---

## 3. Môi Trường Phát Triển & Kiểm Thử

### 3.1. Thiết lập môi trường

```bash
# Khởi tạo môi trường ảo Python 3.11 hoặc 3.12
python3 -m venv venv
source venv/bin/activate  # Trên Linux/macOS
# .\venv\Scripts\Activate.ps1  # Trên Windows PowerShell

# Cài đặt toàn bộ dependencies bao gồm môi trường dev
pip install --upgrade pip
pip install -r requirements-dev.txt
```

### 3.2. Tiêu chuẩn mã nguồn (Code Style)

- Tuân thủ PEP 8 cho định dạng mã Python.
- Bắt buộc khai báo gợi ý kiểu dữ liệu (type hints) cho các hàm công khai (`public API`).
- Viết docstrings rõ ràng, mô tả mục đích, tham số đầu vào, giá trị trả về và các ngoại lệ có thể phát sinh.

### 3.3. Kiểm tra trước khi đẩy mã (Pre-push Checklist)

Trước khi commit và đẩy mã lên kho lưu trữ từ xa, thành viên bắt buộc phải chạy và xác nhận đạt 100% các bước kiểm tra sau:

1. **Kiểm tra biên dịch và cú pháp:**
   ```bash
   python -m compileall src scripts tests
   ```
   *Yêu cầu:* Kết quả trả về 0 lỗi cú pháp.

2. **Chạy toàn bộ test suite với `pytest`:**
   ```bash
   pytest
   ```
   *Yêu cầu:* Toàn bộ 1.030+ tests phải `PASSED`. Không có bài test nào bị `FAILED` hoặc `ERROR`.

3. **Kiểm tra trạng thái git:**
   ```bash
   git status --short
   ```
   *Yêu cầu:* Không để sót tệp tạm, tệp cache (`.pyc`, `__pycache__`), tệp dữ liệu corpus hoặc log ngoài ý muốn.

---

## 4. Quy Trình Kiểm Thử Tự Động (GitHub Actions CI)

Kho lưu trữ tích hợp quy trình CI tự động tại `.github/workflows/tests.yml`. Mỗi khi có commit mới được đẩy lên nhánh (`push`) hoặc có Pull Request (`pull_request`) vào nhánh chính:
- Hệ thống tự động khởi tạo ma trận môi trường kiểm thử trên cả **Python 3.11** và **Python 3.12**.
- Cài đặt tự động `requirements-dev.txt`.
- Kiểm tra toàn diện biên dịch byte-code (`python -m compileall`).
- Thực thi toàn bộ test suite ngoại tuyến với `pytest`.

*Yêu cầu nghiệm thu:* Một nhánh chỉ được xem là đủ điều kiện hợp nhất khi cả hai luồng kiểm thử trên Python 3.11 và Python 3.12 đều hiển thị trạng thái xanh (`success`).
