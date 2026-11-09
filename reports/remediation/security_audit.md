# Security & Destructive Operations Audit — Phase 0

**Dự án:** Nhập môn Khoa học Dữ liệu — Đồ án Milestone 1  
**Repository:** `Toan-ten/Arxiv-Project-Group10`  
**Nhánh:** `feat/m1-t02-colab-resume`  
**Thời điểm audit:** 2026-11-09T15:30:00+07:00  

---

## 1. Kết Quả Audit Thao Tác Phá Hủy Dữ Liệu (Destructive Operations)

### Phát hiện P0-01 và biện pháp xử lý:
- **Phát hiện:** Ô lệnh cuối cùng (Cell 8) trong [`notebooks/m1_colab_fast_production.ipynb`](file:///home/tuan-anh/Documents/DS_PJ/Arxiv-Project-Group10/notebooks/m1_colab_fast_production.ipynb) chứa lệnh `shutil.rmtree(Path("/content/drive/MyDrive/Group10_M1_Production"))`.
- **Hành động khắc phục:** Đã **xóa bỏ hoàn toàn Cell 8** khỏi notebook. Notebook production hiện chỉ còn đúng 8 cells (1 markdown banner, 1 quy định vận hành, và 6 code cells tương ứng với 6 bước tuần tự).
- **Quy tắc an toàn mới:** Không có bất kỳ dòng code production nào được phép xóa tự động thư mục `DRIVE_ROOT` hoặc `drive_production`. Bất kỳ thao tác dọn dẹp nào chỉ được phép tác động lên vùng tạm thời (`scratch_shm` hoặc `local_workspace`).
- **Trình duyệt AST dùng chung (Shared `DestructiveCallVisitor`):**
  1. *Hợp nhất triển khai và phạm vi theo dõi (Scope Tracking):* Đã loại bỏ hoàn toàn các bản sao `DestructiveCallVisitor` cục bộ. Lớp `DestructiveCallVisitor` hiện được định nghĩa duy nhất tại module level của [`tests/test_fast_production.py`](file:///home/tuan-anh/Documents/DS_PJ/Arxiv-Project-Group10/tests/test_fast_production.py) và được dùng chung cho cả việc quét sổ tay lẫn kiểm thử hồi quy. Visitor triển khai ngăn xếp phạm vi từ vựng (lexical scope stack) cho `FunctionDef`, `AsyncFunctionDef`, và `ClassDef`. Các biến được định nghĩa trong hàm này không bị rò rỉ (leak) sang hàm khác, giải quyết triệt để nguy cơ dương tính giả khi hai hàm sử dụng trùng tên biến cục bộ.
  2. *Cập nhật trạng thái gán lại biến (Variable Reassignment):* Khi một biến được gán lại trong cùng phạm vi, visitor cập nhật trạng thái ô nhiễm (taint) một cách tuần tự:
     - Nếu gán lại thành một đường dẫn an toàn (ví dụ: `target = Path("/tmp/safe")`), trạng thái ô nhiễm được gỡ bỏ ngay lập tức, không gây cảnh báo sai nếu sau đó gọi `shutil.rmtree(target)`.
     - Nếu biến bị gọi xóa trước khi gán lại an toàn, lời gọi trước vẫn bị bắt chính xác theo đúng thứ tự thực thi.
  3. *Khả năng phát hiện đã được kiểm chứng bằng regression tests:*
     - Lời gọi phá hủy trực tiếp (`rmtree`, `remove`, `unlink`, `rmdir`) chứa bất kỳ từ khóa bảo vệ nào: `drive`, `MyDrive`, `Group10`, `production`.
     - Bí danh đơn giản (Simple alias) ở module level hoặc trong hàm: `target = Path(drive_path); shutil.rmtree(target)`.
     - Lời gọi phương thức phá hủy trên biến bí danh: `target = Path(...); target.unlink()`.
     - Cách ly phạm vi (Scope isolation): Hai hàm có cùng tên biến nhưng một hàm trỏ đường dẫn Drive còn một hàm trỏ thư mục tạm thời chỉ kích hoạt đúng 1 vi phạm tại hàm Drive.
     - Đường dẫn sạch (`/tmp/local_scratch_space`, `/tmp/local_temp_file.txt`) trả về 0 vi phạm (không có false positive).
  4. *Giới hạn kỹ thuật đã kiểm chứng (Documented Limitations):*
     - Bí danh bắc cầu (Chained alias): Trường hợp `target = Path(...); target2 = target; shutil.rmtree(target2)` không được phát hiện do visitor không triển khai công cụ phân tích luồng dữ liệu tượng trưng (symbolic data-flow engine). Giới hạn này đã được chứng minh và kiểm thử tường minh trong `test_destructive_call_visitor_cases`.
     - Phân tích liên hàm (Inter-procedural analysis): Không theo dõi các biến được truyền qua tham số hàm hoặc trả về từ hàm khác.
     - Phân tích tĩnh (Static Analysis bằng Regex và AST) không thể bảo đảm phát hiện tuyệt đối mọi dạng xóa dữ liệu nếu đường dẫn được tính toán động tại thời điểm chạy (dynamic path resolution, `os.path.join` với các biến độc lập, `getattr`, `eval`, hoặc qua tiến trình nhị phân bên ngoài). Do đó, hệ thống bắt buộc phải áp dụng kiến trúc phòng thủ theo chiều sâu (defense-in-depth): quy định vận hành đơn tiến trình (single-writer) và cách ly hoàn toàn vùng đệm ghi chép (`/content/fast_crawling_workspace`).

---

## 2. Kết Quả Audit Bí Mật & Thông Tin Xác Thực (Secret Security Audit)

### Phạm vi rà soát đồng bộ:
- Toàn bộ 4 sổ tay Jupyter trong `notebooks/`: quét chi tiết từng source cell, toàn bộ cấu trúc metadata (cấp notebook và cấp cell), cùng toàn bộ kết quả thực thi trong `outputs` (luồng stream, display data, kết quả trả về, traceback lỗi).
- Toàn bộ mã nguồn Python và bài test trong các thư mục `src/`, `scripts/`, `tests/` (bao gồm chính file [`tests/test_fast_production.py`](file:///home/tuan-anh/Documents/DS_PJ/Arxiv-Project-Group10/tests/test_fast_production.py)).
- Hàm quét dùng chung `scan_credential_text()` chuẩn hóa 4 mẫu gán khóa xác thực và danh sách tiền tố cấm:
  1. Gán biến trực tiếp: `KEY = "..."`
  2. Gán chỉ mục biến môi trường: `os.environ["KEY"] = "..."`
  3. Gọi thiết lập mặc định: `os.environ.setdefault("KEY", "...")`
  4. Gán từ điển cấu hình: `{"KEY": "..."}`
  5. Tiền tố nhận diện token: `KGAT_` (Kaggle), `s2k-` (Semantic Scholar), `ghp_`, `gho_`, `glpat-` (GitHub/GitLab).

### Tự bao phủ kiểm thử (Self-Coverage) và Miễn trừ thu hẹp:
1. **Không miễn trừ file test:** File `tests/test_fast_production.py` được đưa vào phạm vi quét tự động của `test_notebooks_and_source_do_not_contain_hardcoded_credentials`. Các chuỗi khóa phục vụ kiểm thử hồi quy được dựng động qua phép nối chuỗi (`"KAGGLE" + "_API_TOKEN"`, `"s" + "2k-..."`), bảo đảm file kiểm thử không có kết quả dương tính giả tĩnh.
2. **Cơ chế miễn trừ thu hẹp (Narrowly Scoped Allowlist):** Toàn bộ cơ chế miễn trừ dựa trên chuỗi con (substring) đã bị loại bỏ. Chỉ duy nhất fixture kiểm thử cụ thể được định danh tường minh trong `ALLOWED_TEST_FIXTURE_ASSIGNMENTS`: `("tests/test_s2_integration.py", "SEMANTIC_SCHOLAR_API_KEY", "offline-response-secret-key")`. Mọi chuỗi khóa có hình thức token thực tế (kể cả có chứa từ ngữ như "test", "mock", hay "sample") đều không được miễn trừ tự động.
3. **Cơ chế nạp bí mật an toàn:**
   - Kaggle Token: Nạp từ Colab Secrets `userdata.get("KAGGLE_API_TOKEN")` hoặc biến môi trường `os.environ.get("KAGGLE_API_TOKEN")`.
   - Semantic Scholar API Key: Nạp từ Colab Secrets qua `local_config.get_semantic_scholar_api_key()`.
4. **Cảnh báo thu hồi token:** Đối với các token xác thực (`[REDACTED]` và `[REDACTED]`) từng xuất hiện trong nội dung trao đổi, toàn bộ giá trị đã được coi là bị lộ (compromised). Khuyến nghị người dùng bắt buộc phải thu hồi (revoke/rotate) ngay lập tức trên trang quản lý tài khoản Kaggle và Semantic Scholar. Toàn bộ tài liệu báo cáo và commit tuyệt đối không lưu lại giá trị thực tế của các token này dưới bất kỳ hình thức nào.
5. **Giới hạn kỹ thuật:** Quét tĩnh không phát hiện được các thông tin xác thực được mã hóa hoặc dựng động lúc chạy (như giải mã chuỗi base64 trong runtime). Việc bảo mật phụ thuộc vào quy trình quản trị bí mật thông qua Colab Secrets.

---

## 3. Kết Quả Audit An Toàn Sổ Tay Production (Notebook Safety)

1. **Khóa cứng commit (Fail-closed CODE_COMMIT):**
   - Logic kiểm tra `CODE_COMMIT` được triển khai inline trực tiếp trong Bước 1 của sổ tay production [`notebooks/m1_colab_fast_production.ipynb`](file:///home/tuan-anh/Documents/DS_PJ/Arxiv-Project-Group10/notebooks/m1_colab_fast_production.ipynb) để bảo đảm tính tự chứa (self-contained) và tính tái lập đối với các commit lịch sử.
   - Sổ tay production hiện đang ghim `CODE_COMMIT = "489a82fd959ccc6cced7c59fc309255e4421bef2"` nhằm bảo toàn nguồn gốc (provenance) của phiên chạy Colab trước đó; SHA này sẽ chỉ được cập nhật khi hoàn thành toàn bộ kế hoạch và bước vào nghiệm thu cuối cùng.
   - Nếu người dùng cung cấp giá trị rỗng, sai kiểu dữ liệu hoặc không đủ 40 ký tự hex, hệ thống bật lỗi `RuntimeError` ngay lập tức (fail-closed), tuyệt đối không gọi bất kỳ lệnh Git nào và không fallback ngầm về HEAD của nhánh.
   - Sau khi checkout `--detach`, hệ thống đối chiếu `git rev-parse HEAD` với `CODE_COMMIT`, nếu không khớp sẽ dừng ngay lập tức.
2. **Kiểm thử thực thi trực tiếp với mô phỏng Git siết chặt (Tightened Stateful Git Execution):**
   - Bài kiểm thử `test_fast_production_notebook_fail_closed_execution` thực thi trực tiếp chính code cell của sổ tay trong môi trường Colab giả lập, sử dụng `TightenedSimulatedGitRunner` để xác thực nghiêm ngặt chuỗi thao tác:
     + Kịch bản A: Mã commit sai định dạng / rỗng / None / không phải chuỗi $\rightarrow$ phát sinh `RuntimeError` ngay lập tức; xác nhận 0 lệnh Git nào được gọi.
     + Kịch bản B: Lỗi checkout từ Git $\rightarrow$ lan truyền `subprocess.CalledProcessError`; xác nhận `git fetch origin` và `git checkout --detach <CODE_COMMIT>` đã được gọi với `check=True`, và `git rev-parse HEAD` tuyệt đối không được gọi.
     + Kịch bản C: Mã HEAD sau checkout không khớp `CODE_COMMIT` $\rightarrow$ phát sinh `RuntimeError`; xác nhận `git rev-parse HEAD` đã được gọi với `text=True` để đối chiếu.
     + Kịch bản D: Checkout thành công commit hợp lệ $\rightarrow$ hoàn tất chính xác với đúng chuỗi lệnh `git fetch origin`, `git checkout --detach <CODE_COMMIT>`, và `git rev-parse HEAD`.
     + Kịch bản E: Bất kỳ lệnh Git không mong muốn nào (ví dụ `git pull`) hoặc thiếu cờ an toàn (`check=False`, `text=False`) đều bị chặn và kích hoạt `AssertionError` ngay lập tức.
3. **Chặn tài nguyên GPU:**
   - Kiểm tra `COLAB_GPU == "1"` và dừng ngay lập tức nếu runtime đang bật GPU, bảo đảm tuân thủ yêu cầu môi trường CPU-only.
