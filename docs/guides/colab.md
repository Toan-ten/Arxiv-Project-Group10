# Vận hành M1 trên Colab

Hướng dẫn thao tác chuẩn nằm trong [Hướng dẫn thành viên chạy M1](../M1_COLAB_MEMBER_GUIDE.md). Notebook chạy ba pha arXiv, Semantic Scholar và kiểm toán/đối soát trên phạm vi ID xác định theo MSSV.

## Phát hành notebook

Notebook phải ghim một SHA runtime đầy đủ đã có trên remote. Khi cập nhật mã chạy, tạo commit runtime trước, sau đó cập nhật `M1_RUNTIME_COMMIT` và kiểm thử hợp đồng notebook trong commit phát hành tiếp theo. Liên kết Colab phải mở notebook từ đúng nhánh phát hành.

## Hợp đồng và benchmark

Quy tắc lưu source, xử lý `.bbl`, provenance và bảng xuất nằm trong [hợp đồng dữ liệu nguồn](../contracts/source_data_contracts.md). Cách ghi và tổng hợp thời gian, RAM, dung lượng cùng phạm vi benchmark nằm trong [hướng dẫn đo hiệu năng](../operations/benchmark_runner.md). Quy trình ba pha và giới hạn API được mô tả trong [quy trình M1](../operations/m1_production.md).
