# Chuyển tài liệu tham khảo BBL sang BibTeX

Pipeline này đọc tệp `.bbl` trong các archive `source_bbl` của M1, ghi BibTeX theo từng tệp và hợp nhất theo bài báo khi nguồn không mơ hồ. Bộ đọc không thực thi TeX và không tra cứu siêu dữ liệu bên ngoài.

## Chạy pipeline

Tạo môi trường Python 3.10 trở lên và cài `requirements-m2.txt`. Từ thư mục gốc repo mã nguồn, chạy:

```bash
python -m scripts.run_m2_bbl \
  --input /duong/dan/cohort_0017_bbl_artifacts.tar.gz \
  --input /duong/dan/cohort_0031_bbl_artifacts.tar.gz \
  --inventory /duong/dan/inventory.csv \
  --output /duong/dan/ket-qua-m2-bbl
```

Có thể thay archive bằng thư mục đã giải nén. `--inventory` là tệp CSV gồm `archive,path,sha256`; khi có tham số này, pipeline đối chiếu danh sách và SHA-256 của toàn bộ tệp `.bbl` trước khi xử lý. Thư mục đầu ra phải nằm ngoài thư mục đầu vào.

Pipeline nhận các cấu trúc `bibitem`, trường có nhãn `bibinfo`, macro trường của nhà xuất bản, BibLaTeX, Harvard và AMSRefs. Mỗi trường trích xuất có phương pháp và giá trị nguồn trong `field_evidence`. Trường không có bằng chứng được để trống. BibTeX được đọc lại sau khi ghi để phát hiện lỗi cú pháp hoặc mất khóa trích dẫn.

Pipeline giải mã BBL bằng UTF-8. Nếu dữ liệu có byte UTF-8 không hợp lệ, pipeline ghi cảnh báo giải mã và chỉ tiếp tục khi ký tự thay thế không xuất hiện trong tiêu đề, tác giả, năm, DOI, tạp chí hoặc URL đã trích xuất. Byte gốc tiếp tục được xác định bằng SHA-256; lỗi giải mã chạm trường xuất ra khiến tệp mang trạng thái `parse_error`.

## Kết quả

Mỗi lượt xử lý có thư mục `run-<mã kiểm tra>` xác định từ danh sách tệp đầu vào và mã parser:

```text
run-<mã kiểm tra>/
  definition.json
  files/<bài>/v<n>/source_bbl/<tên>.bib
  files/<bài>/v<n>/source_bbl/<tên>.refsection-<n>.bib
  files/<bài>/v<n>/source_bbl/<tên>.json
  papers/<arxiv_id>/bibliography.bib
  papers/<arxiv_id>/evidence.json
  report.json
  attempts/<mã lượt>.json
```

Tệp JSON theo nguồn giữ trạng thái, SHA-256, khóa, trường đã chuẩn hóa, văn bản gốc, phương pháp trích xuất và dấu hiệu danh sách tác giả bị rút gọn. Với BibLaTeX, bằng chứng còn ghi phạm vi `refsection`, tên datalist, số lần xuất hiện và khối nguồn của từng lần xuất hiện. `evidence.json` theo bài chứa các hàng đúng schema bảng `bibtex` của M2, ánh xạ khóa trích dẫn giữa các phiên bản và xung đột trường. `report.json` tổng hợp số tệp, entry và bài cần rà soát. Đầu ra theo bài chỉ được ghi khi tất cả tệp của bài đã đọc được và mỗi phiên bản chỉ có một nội dung BBL xác định.

Nếu một BBL BibLaTeX có nhiều `refsection`, pipeline ghi một tệp BibTeX cho mỗi phạm vi và giữ nguyên citation key. Pipeline không hợp nhất các phạm vi này vào một bibliography chung vì cùng khóa có thể trỏ tới các tài liệu khác nhau. Bài tương ứng mang trạng thái `requires_review`; các BibTeX theo phạm vi vẫn được giữ để đối chiếu.

Trong cùng một `refsection`, một mục BibLaTeX có thể xuất hiện trong nhiều datalist phục vụ các ngữ cảnh sắp xếp. Pipeline chỉ gộp các lần xuất hiện khi khóa, loại mục và các trường công khai (tiêu đề, tác giả, năm, DOI, tạp chí và URL) trùng khớp hoàn toàn. Bằng chứng giữ nguyên từng khối nguồn và SHA-256; bộ đếm đối chiếu số marker trong BBL với tổng số lần xuất hiện nguồn, không chỉ với số mục sau khi khử trùng lặp. Mục cùng khóa nhưng khác trường công khai, hoặc lặp trong cùng datalist, bị từ chối.

Một số kiểu BBL dùng bí danh tĩnh `mcitethebibliography` cho môi trường `thebibliography`. Pipeline chỉ chuẩn hóa khi khai báo bắt đầu/kết thúc khớp mẫu đã xác định và giữ nguyên cấu trúc một danh sách. Các cấu hình TeX điều kiện hoặc định nghĩa ngoài tập hỗ trợ vẫn bị từ chối; parser không thực thi TeX.

Tệp BBL hợp lệ không có entry tạo một tệp BibTeX rỗng và có `entry_count` bằng 0. Trường hợp này không bị ghi nhận là lỗi phân tích.

Chạy lại cùng đầu vào và cùng mã parser dùng lại checkpoint theo tệp sau khi kiểm tra SHA-256 và đọc lại BibTeX. Pipeline không thêm dòng trùng. Đầu vào hoặc mã parser thay đổi sẽ tạo thư mục kết quả khác.

Mỗi lượt chạy ghi thời gian toàn trình, số tệp dùng lại, dung lượng đầu ra và mức RAM đỉnh của tiến trình trên Linux trong `attempts/`. Lượt dùng lại checkpoint được đo riêng với lượt phân tích đầu vào. Khi chạy trên Colab CPU, giữ tệp này cùng cấu hình máy để báo cáo phép đo.

Mã thoát `0` nghĩa là mọi tệp và bài đã xử lý, `1` nghĩa là kết quả theo tệp có thể đã ghi nhưng còn bài cần rà soát, `2` nghĩa là lỗi đầu vào hoặc đầu ra cấp lượt chạy. Mã `0` không thay thế đánh giá độ chính xác từng trường.

## Trường hợp cần rà soát

Nếu một bài có nhiều `.bbl` khác nội dung trong cùng phiên bản, archive BBL đơn lẻ không chứng minh tệp nào thuộc đường biên dịch chính. Pipeline giữ kết quả theo từng tệp và chưa tạo BibTeX hợp nhất cho bài đó. Hãy đối chiếu `.tex`, `.bib`, manifest và đường `\input`/`\include` bằng runner M2 trước khi xác nhận bibliography của bài.

Nếu một tệp có nhiều `refsection`, đối chiếu từng phạm vi với cấu trúc trích dẫn của tài liệu trước khi tạo bibliography hợp nhất. Không gộp các entry chỉ vì citation key giống nhau.

Một mục thiếu tiêu đề hoặc tác giả trong nguồn vẫn được xuất với trường tương ứng để trống. Trường lấy từ khối văn bản hiển thị được ghi phương pháp `rendered_newblock`, `rendered_list`, `rendered_comma_pattern` hoặc `rendered_year_regex`; trường đọc theo nhãn có phương pháp riêng. `authors_truncated` đánh dấu danh sách tác giả có dấu hiệu `et al.` trong nguồn. Mục cần kiểm tra thủ công được xác định bằng nguồn gốc và phương pháp này, không điền nội dung theo phỏng đoán.

Kiểm thử ngoại tuyến:

```bash
pytest -q tests/test_m2.py tests/test_m2_bbl_pipeline.py
python -m compileall src/m2 scripts/run_m2_bbl.py tests/test_m2_bbl_pipeline.py
```
