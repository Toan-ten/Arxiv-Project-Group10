# Vận hành M1 trên Colab

Hướng dẫn thao tác chuẩn nằm trong [Hướng dẫn thành viên chạy M1](../M1_COLAB_MEMBER_GUIDE.md). Notebook chạy ba pha arXiv, Semantic Scholar và kiểm toán/đối soát trên phạm vi ID xác định theo MSSV.

## Mã nguồn của phiên vận hành

Notebook vận hành trong repo ghim `M1_RUNTIME_COMMIT` tại:

```text
e29ea3c9a0525371b4ee6fb6b60ba069ac00b647
```

Ô cấu hình kiểm tra SHA có đủ 40 ký tự, fetch từ `origin`, checkout tới commit đó ở trạng thái detached HEAD và xác minh `HEAD` trước khi import mã M1. Nếu kernel đã nạp `src`, ô cấu hình yêu cầu khởi động lại phiên để loại bỏ module đã nạp trong bộ nhớ.

Notebook dùng `reference_strategy="batch_m1"`. Mở [notebook M1 trên Colab](https://colab.research.google.com/github/Toan-ten/Arxiv-Project-Group10/blob/feat%2Fs2-request-optimization/notebooks/m1_pipeline_thu_thap_va_kiem_toan.ipynb), chọn CPU, nhập MSSV và đặt `COHORT_LIMIT = 1` nếu muốn kiểm tra một lô. Sau ô cấu hình, kiểm tra SHA runtime và dòng `Chiến lược S2: batch_m1`. Có thể chạy riêng ô S2 sau cấu hình; hai Secret vẫn bắt buộc theo hợp đồng M1.

Dữ liệu chạy thử nằm trong `M1-pipeline-optimization-pilot/<MSSV>` trên My Drive; phạm vi đầy đủ dùng `M1-pipeline-optimization-full/<MSSV>`. Chạy lại cùng MSSV, phạm vi và thư mục sẽ tái sử dụng checkpoint hợp lệ. Khi công bố phép đo, ghi rõ trạng thái dữ liệu tại thời điểm bắt đầu. Khi sao chép notebook, kiểm tra SHA đã ghim trong ô cấu hình trước khi chạy.

Nhánh chứa notebook và commit cung cấp mã runtime là hai tham chiếu riêng. Mã thực thi do SHA trong notebook quyết định. Khi thay đổi mã xử lý, cần phát hành SHA tương ứng trong notebook; Git không cố định các gói phụ thuộc được khai báo không khóa trong `requirements.txt`.

Notebook nhập API từ `src.m1_workflow`. Trong checkout chứa cấu trúc module hiện hành, các điểm vào được xuất lại qua `src.m1_phases` và chuyển tới package nội bộ `src.m1_phase_modules`. Cấu trúc chi tiết nằm trong [tài liệu kiến trúc](../architecture/repository-structure.md).

## Phát hành runtime

Notebook vận hành phải ghim một SHA đầy đủ đã tồn tại trên remote. Quy trình phát hành gồm:

1. Tạo commit chứa mã runtime đã hoàn tất kiểm thử.
2. Đưa commit runtime lên remote để môi trường Colab có thể fetch.
3. Cập nhật `M1_RUNTIME_COMMIT` và kiểm thử hợp đồng notebook trong commit phát hành tiếp theo.
4. Kiểm tra notebook từ nhánh phát hành bằng một phiên Colab độc lập, xác minh SHA thực tế trước khi chạy các pha.

Chỉ đổi SHA ghim khi mã xử lý đã qua kiểm thử và có trên remote. Liên kết Colab công bố cho thành viên phải trỏ tới notebook trên đúng nhánh phát hành.

## Hợp đồng và benchmark

Quy tắc lưu source, xử lý `.bbl`, provenance và bảng xuất nằm trong [hợp đồng dữ liệu nguồn](../contracts/source_data_contracts.md). Cách ghi và tổng hợp thời gian, RAM, dung lượng cùng phạm vi benchmark nằm trong [hướng dẫn đo hiệu năng](../operations/benchmark_runner.md). Quy trình ba pha và giới hạn API được mô tả trong [quy trình M1](../operations/m1_production.md).
