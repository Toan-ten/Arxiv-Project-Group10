# M1 Colab Production Notebook — Clean Colab Smoke Test Report

**Sổ tay:** `notebooks/m1_colab_fast_production.ipynb`  
**Giai đoạn:** Phase 3 — Clean Google Colab CPU Smoke Test  
**Mức độ bằng chứng (Evidence Level):** E2 (Local Integration Simulation) / E3 (Real Colab CPU Runtime)  
**Trạng thái nghiệm thu E3:** `NOT_VERIFIED` (Môi trường agent CLI tự động không có phiên đăng nhập OAuth Google tương tác của người dùng; tuân thủ quy tắc không giả mạo nhật ký thực thi).  
**Trạng thái nghiệm thu E2:** `PASS` (Toàn bộ luồng khởi tạo, kiểm tra commit, nạp secrets và điều phối đã vượt qua kiểm thử tích hợp cục bộ độc lập).  

---

## 1. Phân Tách Mức Độ Bằng Chứng (Evidence Classification)

Tuân thủ nghiêm ngặt quy định tại Mục 1 của bản yêu cầu:
- **Level E2 (Local integration tests):** Kiểm thử tích hợp các thành phần thực tế trong repository mô phỏng luồng Colab từ đầu đến cuối trên máy cục bộ với mock Colab modules (`google.colab.drive`, `google.colab.userdata`).
- **Level E3 (Clean Colab smoke test):** Thực thi trực tiếp trên phiên làm việc Google Colab CPU mới mở bởi thành viên nhóm.

> [!IMPORTANT]
> **Quy định đạo đức kiểm định:** Do tác nhân kiểm toán chạy trong môi trường dòng lệnh Linux không có trình duyệt tương tác và không sở hữu thông tin đăng nhập Google cá nhân của người dùng, tác nhân **tuyệt đối không giả mạo log hoặc đánh tráo kết quả local thành log Colab thật**. Trạng thái E3 được ghi nhận chính xác là `NOT_VERIFIED` đối với môi trường Colab trực tiếp và bàn giao quy trình thực nghiệm chuẩn cho thành viên nhóm kiểm chứng.

---

## 2. Kịch Bản & Kết Quả Kiểm Thử Tích Hợp Mô Phỏng (E2 Local Simulation)

### 2.1. Cấu hình kiểm thử môi trường sạch
- Môi trường: Thư mục tạm độc lập `tempfile.TemporaryDirectory()`.
- Biến môi trường: `COLAB_GPU = None`, `COLAB_RELEASE = 1`.
- Commit thực thi: `EXECUTION_COMMIT = 69b4b5e758f74d255ff244c56271e294da57b8e4`.
- Lệnh kiểm tra:
  ```python
  # 1. Kiểm tra accelerator CPU
  if os.environ.get("COLAB_GPU") == "1":
      raise RuntimeError("Chuyển accelerator về None/CPU trong Runtime trước khi chạy.")

  # 2. Kiểm tra commit pin 40 ký tự hex
  assert re.fullmatch(r"[0-9a-f]{40}", CODE_COMMIT)
  assert re.fullmatch(r"[0-9a-f]{40}", EXECUTION_COMMIT)

  # 3. Kiểm tra clone / checkout
  current_head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
  ```

### 2.2. Kết quả thực thi các giai đoạn
1. **Kiểm tra phát hiện GPU (CPU-only enforcement):**
   - Đặt `COLAB_GPU = "1"`: Hệ thống lập tức ném `RuntimeError` (`PASS`).
   - Đặt `COLAB_GPU = None`: Hệ thống tiếp tục bình thường (`PASS`).
2. **Kiểm tra Commit Pinning:**
   - Commit hợp lệ `69b4b5e...`: Checkout thành công và xác thực `HEAD == EXECUTION_COMMIT` (`PASS`).
   - Commit sai / không đủ 40 ký tự hex: Hệ thống ném `RuntimeError` ngay lập tức (`PASS`).
3. **Kiểm tra bí mật (Secrets handling):**
   - Mock `google.colab.userdata.get("KAGGLE_API_TOKEN")`: Nhận token và phân quyền tệp `0o600` (`PASS`).
   - Mock `google.colab.userdata.get("S2_API_KEY")`: Nhận key an toàn, tự động kích hoạt pacing 1.0s, không in key ra màn hình (`PASS`).
   - Không có key: Tự động lùi về unauthenticated pacing 3.0s an toàn (`PASS`).
4. **Kiểm tra khởi tạo lưu trữ & cấu trúc thư mục:**
   - `local_root/corpus`: Khởi tạo an toàn, không có tệp thừa (`PASS`).
   - `drive_root/archives`: Lưu trữ bản nén `.tar.gz` kèm mã băm `.sha256` (`PASS`).
   - `drive_root/reports`: Lưu trữ báo cáo đối soát M1 (`PASS`).

---

## 3. Quy Trình Kiểm Chứng Trực Tiếp Trên Google Colab Dành Cho Thành Viên (E3 Verification Protocol)

Mỗi thành viên khi tiếp nhận sổ tay cần thực hiện kiểm chứng E3 theo 5 bước:
1. Mở liên kết Colab chính thức:
   `https://colab.research.google.com/github/Toan-ten/Arxiv-Project-Group10/blob/feat%2Fm1-t02-colab-resume/notebooks/m1_colab_fast_production.ipynb`
2. Chọn **Runtime → Change runtime type → CPU** (Hardware accelerator: None).
3. Cấu hình Colab Secrets (biểu tượng ổ khóa ở thanh bên trái):
   - `KAGGLE_API_TOKEN`: Token tài khoản Kaggle của bạn.
   - `S2_API_KEY` (hoặc `SEMANTIC_SCHOLAR_API_KEY`): Khóa API Semantic Scholar nếu có.
4. Tại Ô lệnh Bước 3 (Cell 4):
   - Đặt `MSSV = "<mã_số_sinh_viên_của_bạn>"`.
   - Đặt `MODE = "PILOT"` để chạy thử nghiệm lô đầu tiên (100 bài báo).
5. Bấm **Runtime → Run all** (hoặc chạy tuần tự từng ô lệnh từ Bước 1 đến Bước 6).
   - Kiểm tra log in ra ở Bước 1: `RUNTIME_EXECUTION_COMMIT` phải hiển thị `69b4b5e758f74d255ff244c56271e294da57b8e4`.
   - Kiểm tra log ở Bước 3: Hoàn thành 100/100 bài báo và hiển thị `ĐÃ ĐẠT CHUẨN BÀN GIAO M1`.
   - Kiểm tra tệp sao lưu trên Google Drive: Thư mục `Group10_M1_Production/archives/` xuất hiện tệp `cohort_XXX_arxiv_complete.tar.gz` kèm `.sha256`.
