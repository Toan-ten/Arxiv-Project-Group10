# Security & Destructive Operations Audit — Phase 0

**Dự án:** Nhập môn Khoa học Dữ liệu — Đồ án Milestone 1  
**Repository:** `Toan-ten/Arxiv-Project-Group10`  
**Nhánh:** `feat/m1-t02-colab-resume`  
**Thời điểm audit:** 2026-11-09T15:15:00+07:00  

---

## 1. Kết Quả Audit Thao Tác Phá Hủy Dữ Liệu (Destructive Operations)

### Phát hiện P0-01 và biện pháp xử lý:
- **Phát hiện:** Ô lệnh cuối cùng (Cell 8) trong [`notebooks/m1_colab_fast_production.ipynb`](file:///home/tuan-anh/Documents/DS_PJ/Arxiv-Project-Group10/notebooks/m1_colab_fast_production.ipynb) chứa lệnh `shutil.rmtree(Path("/content/drive/MyDrive/Group10_M1_Production"))`.
- **Hành động khắc phục:** Đã **xóa bỏ hoàn toàn Cell 8** khỏi notebook. Notebook production hiện chỉ còn đúng 8 cells (1 markdown banner, 1 quy định vận hành, và 6 code cells tương ứng với 6 bước tuần tự).
- **Quy tắc an toàn mới:** Không có bất kỳ dòng code production nào được phép xóa tự động thư mục `DRIVE_ROOT` hoặc `drive_production`. Bất kỳ thao tác dọn dẹp nào chỉ được phép tác động lên vùng tạm thời (`scratch_shm` hoặc `local_workspace`).
- **Cơ chế kiểm thử tự động (Regex + AST):** Bài kiểm thử `test_notebooks_do_not_contain_destructive_drive_cleanup` trong [`tests/test_fast_production.py`](file:///home/tuan-anh/Documents/DS_PJ/Arxiv-Project-Group10/tests/test_fast_production.py) kết hợp hai tầng bảo vệ:
  1. *Regex pattern matching:* Quét toàn diện các lệnh xóa nguy hiểm (`rmtree`, `unlink`, `remove`, `rm -rf`) nhắm vào các đường dẫn Google Drive (`drive`, `MyDrive`, `Group10`, `production`).
  2. *AST inspection & Alias Tracking:* Phân tích cây cú pháp trừu tượng (Abstract Syntax Tree) của toàn bộ code cells. Trình duyệt AST (`DestructiveCallVisitor`) theo dõi cả các tham số chuỗi trực tiếp và bí danh biến cục bộ (`target = Path(...); shutil.rmtree(target)` hoặc `target.unlink()`), phát hiện mọi lời gọi xóa file/thư mục liên quan đến từ khóa `drive`, `MyDrive`, `Group10`, `production`.
  3. *Quy định nghiêm ngặt:* Khẳng định không có bất kỳ lệnh gọi `rmtree` nào trong sổ tay `m1_colab_fast_production.ipynb`.
- **Tuyên bố về giới hạn kỹ thuật (Limitation Notice):** Phân tích tĩnh (Static Analysis bằng Regex và AST) có khả năng phát hiện tin cậy các thao tác xóa trực tiếp và các bí danh biến trong cùng phạm vi hàm/cell. Tuy nhiên, phân tích tĩnh không thể bảo đảm phát hiện tuyệt đối mọi dạng xóa dữ liệu nếu đường dẫn được tính toán động tại thời điểm chạy (dynamic path resolution, `getattr`, `eval`, bí danh liên hàm/module, hoặc qua tiến trình nhị phân bên ngoài). Do đó, hệ thống bắt buộc phải áp dụng kiến trúc phòng thủ theo chiều sâu (defense-in-depth): quy định vận hành đơn tiến trình (single-writer) và cách ly hoàn toàn vùng đệm ghi chép (`/content/fast_crawling_workspace`).

---

## 2. Kết Quả Audit Bí Mật & Thông Tin Xác Thực (Secret Security Audit)

### Phạm vi rà soát đồng bộ:
- Toàn bộ 4 sổ tay Jupyter trong `notebooks/`: quét chi tiết từng source cell, toàn bộ cấu trúc metadata (cấp notebook và cấp cell), cùng toàn bộ kết quả thực thi trong `outputs` (luồng stream, display data, kết quả trả về, traceback lỗi).
- Toàn bộ mã nguồn Python và bài test trong các thư mục `src/`, `scripts/`, `tests/`.
- Lịch sử Git commit bằng lệnh tìm kiếm nhị phân (`git log -S`) đối với các tiền tố xác thực.
- 4 mẫu gán khóa xác thực được chuẩn hóa:
  1. Gán biến trực tiếp: `KEY = "..."`
  2. Gán chỉ mục biến môi trường: `os.environ["KEY"] = "..."`
  3. Gọi thiết lập mặc định: `os.environ.setdefault("KEY", "...")`
  4. Gán từ điển cấu hình: `{"KEY": "..."}`
  5. Tiền tố nhận diện token: `KGAT_` (Kaggle), `s2k-` (Semantic Scholar), `ghp_`, `gho_`, `glpat-` (GitHub/GitLab).

### Kết luận kiểm toán:
1. **Mã nguồn, Bài test và Sổ tay:** Không phát hiện bất kỳ thông tin xác thực chưa được cấp phép nào trong phạm vi quét xác định.
2. **Cơ chế miễn trừ thu hẹp (Narrowly Scoped Allowlist):** Toàn bộ cơ chế miễn trừ dựa trên chuỗi con (substring) đã bị xóa bỏ. Thay vào đó, chỉ những fixture kiểm thử cụ thể được định danh tường minh trong `allowed_test_fixture_assignments` (như `"offline-response-secret-key"` trong `tests/test_s2_integration.py`) mới được phép tồn tại trong `tests/`. Mọi chuỗi khóa có hình thức token thực tế (kể cả có chứa từ ngữ như "test", "mock", hay "sample") đều không được miễn trừ tự động.
3. **Cơ chế nạp bí mật an toàn:**
   - Kaggle Token: Nạp từ Colab Secrets `userdata.get("KAGGLE_API_TOKEN")` hoặc biến môi trường `os.environ.get("KAGGLE_API_TOKEN")`.
   - Semantic Scholar API Key: Nạp từ Colab Secrets qua `local_config.get_semantic_scholar_api_key()`.
4. **Phát hiện cảnh báo:** Đối với các token xác thực (`[REDACTED]` và `[REDACTED]`) từng xuất hiện trong nội dung trao đổi, toàn bộ giá trị đã được coi là bị lộ (compromised). Khuyến nghị người dùng bắt buộc phải thu hồi (revoke/rotate) ngay lập tức trên trang quản lý tài khoản Kaggle và Semantic Scholar. Toàn bộ tài liệu báo cáo và commit tuyệt đối không lưu lại giá trị thực tế của các token này dưới bất kỳ hình thức nào.
5. **Giới hạn kỹ thuật:** Quét tĩnh không phát hiện được các thông tin xác thực được mã hóa hoặc dựng động lúc chạy (như giải mã chuỗi base64 trong runtime). Việc bảo mật phụ thuộc vào quy trình quản trị bí mật thông qua Colab Secrets.

---

## 3. Kết Quả Audit An Toàn Sổ Tay Production (Notebook Safety)

1. **Khóa cứng commit (Fail-closed CODE_COMMIT):**
   - Logic kiểm tra `CODE_COMMIT` được triển khai inline trực tiếp trong Bước 1 của sổ tay production [`notebooks/m1_colab_fast_production.ipynb`](file:///home/tuan-anh/Documents/DS_PJ/Arxiv-Project-Group10/notebooks/m1_colab_fast_production.ipynb) để bảo đảm tính tự chứa (self-contained) và tính tái lập đối với các commit lịch sử. Hàm trợ giúp trùng lặp không có consumer đã được gỡ bỏ khỏi `src/fast_production.py`.
   - Nếu người dùng cung cấp giá trị rỗng, sai kiểu dữ liệu hoặc không đủ 40 ký tự hex, hệ thống bật lỗi `RuntimeError` ngay lập tức (fail-closed), tuyệt đối không gọi bất kỳ lệnh Git nào và không fallback ngầm về HEAD của nhánh.
   - Sau khi checkout `--detach`, hệ thống đối chiếu `git rev-parse HEAD` với `CODE_COMMIT`, nếu không khớp sẽ dừng ngay lập tức.
2. **Kiểm thử thực thi trực tiếp với mô phỏng Git có trạng thái (Stateful Git Execution):**
   - Bài kiểm thử `test_fast_production_notebook_fail_closed_execution` thực thi trực tiếp chính code cell của sổ tay trong môi trường Colab giả lập, sử dụng bộ theo dõi lệnh Git có trạng thái để xác thực chính xác chuỗi thao tác:
     + Kịch bản A: Mã commit sai định dạng / rỗng / None / không phải chuỗi $\rightarrow$ phát sinh `RuntimeError` ngay lập tức; xác nhận 0 lệnh Git nào được gọi.
     + Kịch bản B: Lỗi checkout từ Git $\rightarrow$ lan truyền `subprocess.CalledProcessError`; xác nhận `git fetch` và `git checkout` đã được gọi với `check=True`, và `git rev-parse HEAD` tuyệt đối không được gọi.
     + Kịch bản C: Mã HEAD sau checkout không khớp `CODE_COMMIT` $\rightarrow$ phát sinh `RuntimeError`; xác nhận `git rev-parse HEAD` đã được gọi để đối chiếu.
     + Kịch bản D: Checkout thành công commit hợp lệ $\rightarrow$ hoàn tất chính xác với đúng chuỗi lệnh `git fetch`, `git checkout --detach <CODE_COMMIT>`, và `git rev-parse HEAD`.
3. **Chặn tài nguyên GPU:**
   - Kiểm tra `COLAB_GPU == "1"` và dừng ngay lập tức nếu runtime đang bật GPU, bảo đảm tuân thủ yêu cầu môi trường CPU-only.
