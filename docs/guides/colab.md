# Vận hành M1 trên Colab

Hướng dẫn thao tác chuẩn nằm trong [Hướng dẫn thành viên chạy M1](../M1_COLAB_MEMBER_GUIDE.md). Notebook chạy ba pha arXiv, Semantic Scholar và kiểm toán/đối soát trên phạm vi ID xác định theo MSSV.

## Mã nguồn của phiên vận hành

Notebook vận hành trong repo ghim `M1_RUNTIME_COMMIT` tại:

```text
f905c40a1ccdeca16d3243a3e43f1ee5345b723e
```

Ô cấu hình kiểm tra SHA có đủ 40 ký tự, fetch từ `origin`, checkout tới commit đó ở trạng thái detached HEAD và xác minh `HEAD` trước khi import mã M1. Nếu kernel đã nạp `src`, ô cấu hình yêu cầu khởi động lại phiên để tránh dùng module cũ còn trong bộ nhớ.

Nhánh chứa notebook và commit cung cấp mã runtime là hai tham chiếu riêng. Mở notebook từ một nhánh mới không tự chuyển runtime sang mã của nhánh đó. Thay đổi cách tổ chức mã nguồn không tự cập nhật pin; giá trị trong notebook là cấu hình quyết định phiên vận hành. Việc ghim mã Git cũng không cố định toàn bộ phiên bản dependency trong `requirements.txt`.

Notebook nhập API từ `src.m1_workflow`. Trong checkout chứa cấu trúc module hiện hành, các điểm vào được xuất lại qua `src.m1_phases` và chuyển tới package nội bộ `src.m1_phase_modules`. Cấu trúc chi tiết nằm trong [tài liệu kiến trúc](../architecture/repository-structure.md).

## Phát hành runtime mới

Notebook vận hành phải ghim một SHA đầy đủ đã tồn tại trên remote. Quy trình phát hành gồm:

1. Tạo commit chứa mã runtime đã hoàn tất kiểm thử.
2. Đưa commit runtime lên remote để môi trường Colab có thể fetch.
3. Cập nhật `M1_RUNTIME_COMMIT` và kiểm thử hợp đồng notebook trong commit phát hành tiếp theo.
4. Kiểm tra notebook từ nhánh phát hành bằng một phiên Colab mới, xác minh SHA thực tế trước khi chạy các pha.

Chỉ đổi pin khi thực hiện một đợt phát hành runtime riêng. Việc cập nhật tài liệu hoặc tổ chức lại module không phải thao tác chuyển production sang mã mới. Liên kết Colab công bố cho thành viên phải trỏ tới notebook trên đúng nhánh phát hành.

## Notebook đối chiếu mẫu

Notebook đối chiếu dùng để kiểm tra một giả thuyết hoặc phạm vi dữ liệu cụ thể. Notebook này cần ghi rõ phiên bản mã, phương thức nạp mã, nguồn mẫu, điều kiện kiểm tra và giới hạn kết luận; không thay thế sổ tay vận hành M1.

Nếu notebook giải nén mã nguồn từ một gói ZIP rồi thêm đường dẫn tạm vào `sys.path`, phiên chạy đang dùng bản chụp mã (snapshot) đã đóng gói. SHA ghi kèm nhận diện phiên bản dùng để tạo gói; SHA đó không chứng minh Colab đã fetch hoặc checkout commit tương ứng từ remote. Cần xác minh gói được tạo từ checkout sạch của commit đã ghi, hoặc ghi riêng các thay đổi chưa commit; checksum của gói chỉ xác minh tính toàn vẹn của gói. Khi kiểm tra quy trình phát hành qua Git, phải thực hiện fetch và checkout SHA từ remote trong một phiên mới.

Để đối chiếu dữ liệu đã lưu:

1. Tạo vùng thử riêng cho dữ liệu đầu vào và mọi đầu ra có thể ghi.
2. Sao chép các mẫu cùng manifest, checkpoint và checksum liên quan vào vùng thử.
3. Ghi nhận hai phiên bản mã, phương thức nạp và checksum đầu vào trước khi chạy.
4. Chạy phép kiểm tra trên các bản sao; chặn truy cập API nếu mục tiêu chỉ là xác minh dữ liệu đã lưu.
5. So sánh kết quả, trạng thái, checksum và byte đầu ra xác định; kiểm tra byte đầu vào vẫn giữ nguyên.

Giữ nguyên MSSV, danh sách ID và fingerprint trong bằng chứng đã sao chép. Không rút gọn manifest, sửa checksum hoặc đổi fingerprint để làm cho checkpoint được chấp nhận. Nếu chỉ có một số bài, phép kiểm tra chỉ chứng minh tính tương thích của các mẫu đó; xác minh resume toàn lô cần đủ checkpoint, evidence và archive liên quan.

Đối chiếu mẫu không chứng minh crawler đã tải dữ liệu mới, hoàn tất toàn bộ M1 hoặc đạt một mức hiệu năng trên Colab. Các kết luận về thời gian, RAM và dung lượng phải dựa trên phép đo đúng phạm vi và trạng thái bắt đầu.

## Hợp đồng và benchmark

Quy tắc lưu source, xử lý `.bbl`, provenance và bảng xuất nằm trong [hợp đồng dữ liệu nguồn](../contracts/source_data_contracts.md). Cách ghi và tổng hợp thời gian, RAM, dung lượng cùng phạm vi benchmark nằm trong [hướng dẫn đo hiệu năng](../operations/benchmark_runner.md). Quy trình ba pha và giới hạn API được mô tả trong [quy trình M1](../operations/m1_production.md).
