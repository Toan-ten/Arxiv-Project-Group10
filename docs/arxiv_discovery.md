# Entry Discovery: phân tích Atom XML

`parse_arxiv_response(xml, mssv, arxiv_id)` nhận chuỗi hoặc bytes XML từ một
truy vấn **một ID gốc**, không có hậu tố version. Hàm chỉ phân tích dữ liệu,
không thực hiện HTTP. MSSV và ID yêu cầu luôn được giữ trong kết quả.

```python
from src.arxiv_discovery import parse_arxiv_response

xml = '''<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2502.08235v3</id>
    <title>Sample paper</title>
    <author><name>Example Author</name></author>
    <published>2025-02-12T09:00:00Z</published>
    <updated>2025-03-01T10:00:00Z</updated>
  </entry>
</feed>'''
result = parse_arxiv_response(xml, "24120232", "2502.08235")
print(result["version_ids"])
# ['2502.08235v1', '2502.08235v2', '2502.08235v3']
```

XML và metadata trong ví dụ là dữ liệu giả.
Khi ghép pipeline, truyền nguyên cặp `(mssv, arxiv_id)` từ `iter_assigned_ids()`.

| `status` | Ý nghĩa |
| --- | --- |
| `found` | Entry khớp ID yêu cầu; metadata có thể chưa đầy đủ |
| `not_found` | Feed không có entry, `totalResults=0`, không nằm ở trang sau |
| `api_error` | Entry lỗi của arXiv; xem `error` |
| `invalid_xml` | Không đọc được XML |
| `invalid_response` | Sai namespace/cấu trúc, sai ID, nhiều entry hoặc dữ liệu mâu thuẫn |

Trường thiếu được liệt kê trong `missing_fields`; giá trị thiếu là `None`
hoặc danh sách tác giả rỗng. Tác giả có tên vẫn được giữ khi tác giả khác thiếu
tên. Title/tên tác giả được gọn khoảng trắng; ngày giữ nguyên chuỗi nhận được,
không tự bổ sung hoặc suy ra ngày từ năm.

Với phản hồi truy vấn ID gốc, hậu tố `vN` trong entry ID hoặc liên kết
`alternate` xác định `latest_version_id` và danh sách `v1` đến `vN`.
Nếu không có hậu tố rõ ràng, `version_status="needs_verification"`,
`latest_version_id=None`, `version_ids=[]`. Không suy version từ ngày tháng.
Các hậu tố mâu thuẫn được báo `invalid_response`.

Metadata thuộc entry trả về, chưa phải metadata riêng của từng version.
Danh sách version chưa xác nhận source có thể tải được. Parser không xử lý
batch/pagination; feed rỗng thiếu `totalResults=0` được báo bất thường.
Nếu feed có self link chứa `id_list`, giá trị phải khớp đúng ID gốc yêu cầu.
Đối số MSSV/ID sai gây `ValueError`; XML không phải chuỗi/bytes gây `TypeError`.

Chạy toàn bộ kiểm thử offline:

```powershell
python -B -m unittest discover -s tests -v
```

Nguồn: [arXiv API User's Manual](https://info.arxiv.org/help/api/user-manual.html),
[yêu cầu M1](https://hcmus-cs-ta.github.io/lab-book/intro-ds/statement/crawling/).
HTTP client xem [arxiv_client.md](arxiv_client.md), tuân thủ
[Terms of Use](https://info.arxiv.org/help/api/tou.html):
tối đa một request mỗi 3 giây, một kết nối tại một thời điểm.
