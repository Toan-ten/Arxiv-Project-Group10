# Hướng dẫn vận hành thu thập dữ liệu trên Google Colab (Giai đoạn 1)

Tài liệu này mô tả quy trình thu thập mã nguồn LaTeX trên Google Colab (chế độ CPU) đối với tập 20.000 định danh arXiv của Nhóm 10, xây dựng trên nhánh tích hợp `e45b588`.

## 1. Kiến trúc lưu trữ và xử lý

1. **Lưu trữ làm việc trên ổ đĩa cục bộ (`/content/fast_crawling_workspace`)**: Quá trình ghi tệp `.tex`, `.bib`, `manifest.json` và `metadata.json` được thực hiện trên ổ đĩa cục bộ của máy ảo Colab nhằm tránh độ trễ ghi tệp nhỏ trên Google Drive.
2. **Giải nén tạm trên bộ nhớ RAM (`/dev/shm/arxiv_scratch`)**: Các gói nén `.tar.gz` tải về từ `export.arxiv.org` được giải nén và lọc bỏ tệp hình ảnh trực tiếp trong bộ nhớ RAM.
3. **Nạp trước siêu dữ liệu từ bộ dữ liệu Kaggle (`src/kaggle_discovery.py`)**: Đọc tệp `arxiv-metadata-oai-snapshot.json` theo luồng để trích xuất danh sách phiên bản (`v1`, `v2`, ...), ngày nộp ban đầu (`submission_date`), danh sách ngày chỉnh sửa (`revised_dates`) và nơi công bố (`publication_venue`).
4. **Xử lý giới hạn truy cập HTTP 429**: Sử dụng cơ chế thử lại lũy thừa có nhiễu ngẫu nhiên (Exponential Backoff with Jitter: `3,5s -> 6,5s -> 12,5s -> 24,5s -> 48,5s`, tối đa 5 lần thử trên mỗi yêu cầu) độc lập cho từng bài báo, không giới hạn tổng thời gian chờ của lô.
5. **Sao lưu và khôi phục theo từng lô**: Sau mỗi lô (100 định danh) hoặc khi ngắt phiên chạy, dữ liệu được đóng gói thành tệp `cohort_XXXX_corpus.tar.gz` trong thư mục `archives/` trên Google Drive và có thể khôi phục về ổ đĩa cục bộ qua phương thức `restore_cohort()`.

---

## 2. Danh sách tệp mã nguồn

- `src/kaggle_discovery.py`: Đọc tệp `arxiv-metadata-oai-snapshot.json` và khởi tạo bản ghi siêu dữ liệu trong `CorpusStorage`.
- `src/fast_production.py`: Cung cấp lớp `FastProductionRunner` quản lý việc nạp siêu dữ liệu, thực thi `benchmark_runner.run_benchmark`, xử lý tín hiệu ngắt phiên (`KeyboardInterrupt`) và đồng bộ tệp `.tar.gz` với Google Drive.
- `notebooks/m1_colab_arxiv_production.ipynb`: Sổ tay thực thi từng bước trên Google Colab.
- `tests/test_kaggle_discovery.py` và `tests/test_fast_production.py`: Các bộ kiểm thử đơn vị.

---

## 3. Các bước thực hiện trên Google Colab

1. Mở tệp `notebooks/m1_colab_arxiv_production.ipynb` trên Google Colab và chọn cấu hình xử lý bằng CPU.
2. Chạy **Bước 1** để kết nối Google Drive tại `/content/drive` và tải nhánh `feat/m1-t02-colab-resume`.
3. Chạy **Bước 2** để tải và giải nén tệp `arxiv-metadata-oai-snapshot.json` từ Kaggle vào ổ đĩa cục bộ của máy ảo Colab.
4. Tại **Bước 3**, cập nhật biến `COHORT_LIST` theo khoảng lô dữ liệu được phân công (từ `1` đến `200`) và thực thi ô lệnh. Khi cần tạm dừng, nhấn nút dừng trên Colab; hệ thống sẽ lưu trạng thái hiện tại vào Google Drive và tự động chạy tiếp từ bài báo chưa hoàn thành ở lần thực thi kế tiếp.
