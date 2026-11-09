# Hướng dẫn vận hành quy trình Fast Production trên Google Colab CPU-only

Tài liệu này hướng dẫn chi tiết quy trình vận hành quy trình thu thập dữ liệu chính thức của Nhóm 10 cho tập 20.000 định danh arXiv (Milestone 1, môn Nhập môn Khoa học Dữ liệu, HCMUS).

---

## 1. Kiến trúc hệ thống Fast Production

1. **Lưu trữ cục bộ trên NVMe SSD (`/content/fast_crawling_workspace`):** Toàn bộ quá trình cào, kiểm tra checksum, ghi nhận trạng thái được thực hiện trực tiếp trên ổ đĩa cục bộ của máy ảo nhằm loại bỏ độ trễ ghi hàng trăm nghìn tệp nhỏ của Google Drive FUSE.
2. **Vùng giải nén RAM Disk (`/dev/shm/arxiv_scratch`):** Các gói mã nguồn `.tar.gz` được giải nén và lọc tệp `.tex`, `.bib` trực tiếp trên bộ nhớ RAM, không sinh I/O dư thừa xuống ổ đĩa.
3. **Nạp trước siêu dữ liệu từ Kaggle Snapshot (`arxiv-metadata-oai-snapshot.json`):** Trích xuất danh sách phiên bản và ngày nộp của từng phiên bản từ tệp snapshot cục bộ, tiết kiệm hơn 20.000 yêu cầu mạng discovery lên arXiv API.
4. **Bật đầy đủ lịch sử phiên bản (`revision_history=True`):** Mọi phiên bản đều được xác minh ngày nộp chuẩn ISO-8601 theo đúng hợp đồng lưu trữ `docs/m1_contract.md`.
5. **Cơ chế sao lưu và khôi phục an toàn (Durable Backup & Recovery):**
   - Sau mỗi lô (100 định danh) hoặc khi dừng phiên chạy, runner tự động đóng gói `cohort_XXXX_corpus.tar.gz` kèm mã băm `cohort_XXXX_corpus.tar.gz.sha256` lên Google Drive tại `Group10_M1_Production/archives/`.
   - Cơ chế `restore_cohort()` cho phép khôi phục trạng thái sang phiên làm việc Colab mới chỉ trong vài giây. Quá trình khôi phục tuân thủ nghiêm ngặt nguyên tắc **Validate → Stage → Verify → Atomic Swap**, không ghi đè dữ liệu cục bộ mới hơn và từ chối các tệp không thuộc lô.
6. **Làm giàu dữ liệu Semantic Scholar (`enrich_cohort_s2`):** Thu thập siêu dữ liệu bài báo và cây trích dẫn phân trang, tuân thủ chặt chẽ giới hạn tốc độ API (100 requests / 5 phút).
7. **Xuất bản dữ liệu chuẩn hóa:** Xuất tập dữ liệu chính thức ra hai định dạng Parquet (`papers.parquet`, `references.parquet`) và CSV kèm kiểm tra hợp chuẩn schema tự động.

---

## 2. Chuẩn bị môi trường trên Google Colab

1. **Cấu hình phần cứng:**
   - Mở sổ tay [`notebooks/m1_colab_fast_production.ipynb`](../../notebooks/m1_colab_fast_production.ipynb).
   - Chọn **Runtime** → **Change runtime type** → **CPU** (Accelerator: None).
   - *Lưu ý:* Hệ thống sẽ tự động phát hiện và chặn thực thi nếu runtime đang bật GPU.
2. **Cấu hình biến bảo mật (Colab Secrets):**
   - Bấm vào biểu tượng chìa khóa (**Secrets**) tại thanh công cụ bên trái giao diện Colab.
   - Thêm secret `KAGGLE_API_TOKEN`: chuỗi API token lấy từ tài khoản Kaggle của bạn.
   - (Tùy chọn) Thêm secret `S2_API_KEY`: khóa API Semantic Scholar nếu có.
   - Bật quyền truy cập sổ tay (**Notebook access**) cho các secret này.

---

## 3. Quy trình thực thi 6 bước trong sổ tay

### Bước 1: Khởi tạo mã nguồn và môi trường
- Điền mã commit SHA 40 ký tự đã được kiểm toán vào biến `CODE_COMMIT`.
- Chạy ô lệnh 1 để kết nối Google Drive, tải mã nguồn tại commit đã ghim và cài đặt các thư viện phụ thuộc từ `requirements.txt`.

### Bước 2: Chuẩn bị tệp siêu dữ liệu Kaggle Snapshot
- Chạy ô lệnh 2. Sổ tay tự động đọc `KAGGLE_API_TOKEN` từ Colab Secrets và tải tệp `arxiv-metadata-oai-snapshot.json` nếu máy ảo chưa có sẵn.
- Nếu tệp đã có sẵn trên Google Drive, người vận hành có thể sao chép trực tiếp vào `/content/Arxiv-Project-Group10` để tiết kiệm thời gian tải.

### Bước 3: Thu thập mã nguồn TeX và lịch sử phiên bản
- Cập nhật biến `COHORT_LIST` theo phân công thành viên:
  - Lô 1..50: MSSV 24120010 (Trần Văn Tấn Khôi)
  - Lô 51..100: MSSV 24120035 (Lê Thành Đạt)
  - Lô 101..150: MSSV 24120232 (Nguyễn Văn Toàn)
  - Lô 151..200: MSSV 24120260 (Trương Tuấn Anh)
- Chạy ô lệnh 3: Runner tự động kiểm tra và khôi phục tiến trình từ Drive, nạp trước siêu dữ liệu từ Kaggle snapshot, sau đó thực thi cào từng bài báo và lưu trữ gói nén lên Google Drive.

### Bước 4: Làm giàu dữ liệu Semantic Scholar (S2 Enrichment)
- Chạy ô lệnh 4: Gọi phương thức `runner.enrich_cohort_s2()` để thu thập metadata và trích dẫn cho các bài báo đã hoàn tất cào arXiv.
- Nếu không có `S2_API_KEY`, hệ thống tự động đặt pacing giãn cách tối thiểu 3,0 giây/yêu cầu để không vi phạm quota công khai (100 req / 5 phút).

### Bước 5: Kiểm toán chất lượng dữ liệu Milestone 1
- Chạy ô lệnh 5: Gọi công cụ `scripts.generate_m1_report.compute_m1_statistics` để tính toán đầy đủ các chỉ số hoàn thành, tỷ lệ mã nguồn TeX, độ phủ trích dẫn và xuất ra tệp Markdown báo cáo kiểm toán trên Drive.

### Bước 6: Xuất tập dữ liệu chính thức
- Chạy ô lệnh 6: Xuất toàn bộ bảng dữ liệu `papers` và `references` ra thư mục `Group10_M1_Production/final_output/` dưới dạng Parquet và CSV.
- Hàm `validate_exported_tables` tự động kiểm tra tính duy nhất của mã bài báo, tính hợp lệ của khóa ngoại và cấu trúc trường trước khi ghi đĩa.
