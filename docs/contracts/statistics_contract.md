# Hợp đồng chỉ số thống kê

Phiên bản định nghĩa: `2.1.0`. Phạm vi dữ liệu và quy tắc lưu trữ được quy định trong [hợp đồng lưu trữ corpus](corpus_storage_contract.md).

## Nguyên tắc

- Mỗi chỉ số phải xác định rõ tập dữ liệu, điều kiện tính và đơn vị. Các nhóm dùng trong cùng một phương trình đối chiếu không được chồng lấn.
- Trạng thái hoàn tất phải phù hợp với dữ liệu lưu trữ và kết quả kiểm tra tính toàn vẹn. Lỗi đọc, dữ liệu sai định dạng và sai lệch checksum phải được ghi nhận.
- Tỷ lệ có mẫu số bằng 0 nhận giá trị `null` trong JSON và hiển thị `N/A`; không biểu diễn là 0%.
- Trạng thái nguồn, metadata và references được tính riêng. Hoàn tất một giai đoạn không đồng nghĩa hoàn tất toàn bộ bài báo.

## Chỉ số bài báo

### Phân loại định danh

Đơn vị là định danh được phân công, trừ khi có ghi chú khác.

| Chỉ số | Định nghĩa |
|---|---|
| `assigned_candidates` | Tổng số cặp `(mssv, arxiv_id)` trong phạm vi phân công. |
| `found_papers` | Số bài báo có manifest hợp lệ, đúng định danh và `discovery_status == "found"`. |
| `confirmed_not_found` | Số ID được xác nhận `not_found`, có manifest hợp lệ, không có phiên bản hoặc tệp nguồn mâu thuẫn. |
| `unprocessed_papers` | Số ID có manifest hợp lệ và discovery ở trạng thái `not_attempted`. |
| `missing_manifests` | Số ID thiếu `manifest.json`. |
| `invalid_manifests` | Số ID có manifest không hợp lệ, hỏng hoặc không đọc được. |
| `discovery_failed` | Số ID có lần discovery kết thúc bằng lỗi. |
| `other_unresolved` | Số ID chưa thể phân loại an toàn vào các nhóm trên. |

### Hoàn tất từng giai đoạn

| Chỉ số | Điều kiện |
|---|---|
| `source_complete_papers` | Paper đã được tìm thấy, danh sách version đã xác định và không rỗng; mọi version có trạng thái kết thúc hợp lệ (`success`, `no_tex_source` hoặc `withdrawn_version`) và vượt qua kiểm tra toàn vẹn áp dụng cho trạng thái đó. |
| `metadata_complete_papers` | `metadata.json` hợp lệ, đúng định danh, có trạng thái `complete` khớp manifest; title, authors, ngày nộp và lịch sử ngày version đáp ứng hợp đồng. |
| `references_complete_papers` | `references.json` hợp lệ, trạng thái `complete` khớp manifest; `coverage.complete == true` và `continuation == null`. Một tập references rỗng vẫn hợp lệ nếu thỏa điều kiện này. |
| `m1_complete_papers` | Paper đồng thời hoàn tất source, metadata và references, có trạng thái hoàn tất hợp lệ và không có vi phạm toàn vẹn. |

Kiểm tra source áp dụng cho tệp đã khai báo và bằng chứng tương ứng. Phiên bản `no_tex_source` hoặc `withdrawn_version` hợp lệ không nhất thiết có tệp TeX hay thư mục phiên bản.

### Trạng thái xử lý và toàn vẹn

Đối với các paper đã tìm thấy nhưng chưa hoàn tất M1:

| Chỉ số | Định nghĩa |
|---|---|
| `partial_papers` | Paper có `manifest.status == "partial"`. |
| `failed_papers` | Paper có `manifest.status == "failed"`. |
| `interrupted_papers` | Paper có `manifest.status == "interrupted"`. |
| `unresolved_found_papers` | Paper chưa thuộc nhóm hoàn tất hoặc các trạng thái trên. |
| `integrity_failed_papers` | Paper có vi phạm toàn vẹn dữ liệu hoặc tệp. Chỉ số này có thể giao với các nhóm trạng thái và không được cộng như một nhóm phân loại độc lập. |

## Chỉ số phiên bản

Áp dụng cho các phiên bản thuộc paper đã tìm thấy. Đơn vị là phiên bản.

| Chỉ số | Định nghĩa |
|---|---|
| `total_discovered_versions` | Tổng số phiên bản trong các manifest thuộc phạm vi thống kê. |
| `successful_tex_versions` | Số phiên bản có trạng thái `success`. |
| `no_tex_versions` | Số phiên bản có trạng thái `no_tex_source`. |
| `withdrawn_versions` | Số phiên bản có trạng thái `withdrawn_version`. |
| `terminal_versions` | Tổng số phiên bản `success`, `no_tex_source` và `withdrawn_version`. |
| `incomplete_versions` | Số phiên bản `pending`, `in_progress` hoặc `interrupted`. |
| `failed_versions` | Số phiên bản có trạng thái lỗi nguồn. |
| `invalid_versions` | Số bản ghi phiên bản không hợp lệ hoặc không phân loại được. |
| `source_integrity_failed_versions` | Số phiên bản có vi phạm toàn vẹn tệp hoặc dữ liệu. Chỉ số này có thể giao với các nhóm trạng thái. |

`unresolved_versions` là tên tương thích của `invalid_versions`; không cộng hai giá trị này với nhau.

## Chỉ số references

| Chỉ số | Định nghĩa |
|---|---|
| `complete_reference_papers` | Số paper có references hoàn tất và `coverage.complete == true`. |
| `partial_reference_papers` | Số paper có references ở trạng thái chưa hoàn tất. |
| `failed_reference_papers` | Số paper có trạng thái references `failed`. |
| `not_attempted_reference_papers` | Số paper chưa thu thập references hoặc chưa có tệp references. |
| `invalid_reference_papers` | Số paper có tệp references hỏng hoặc sai schema. |
| `total_complete_reference_records` | Tổng số items của các paper có references hoàn tất. |
| `total_observed_reference_records` | Tổng số items đọc được từ các tệp references. |

## Tỷ lệ

Tỷ lệ phần trăm bằng tử số chia mẫu số rồi nhân 100. Trung bình references không nhân 100.

| Chỉ số | Tử số | Mẫu số |
|---|---|---|
| `source_completion_rate` | `source_complete_papers` | `found_papers` |
| `metadata_completion_rate` | `metadata_complete_papers` | `found_papers` |
| `references_coverage_rate` | `complete_reference_papers` | `found_papers` |
| `m1_completion_rate` | `m1_complete_papers` | `found_papers` |
| `version_extraction_success_rate` | `successful_tex_versions` | `total_discovered_versions` |
| `no_tex_rate` | `no_tex_versions` | `total_discovered_versions` |
| `average_references_per_paper` | `total_complete_reference_records` | `complete_reference_papers` |

## Quy tắc đối chiếu

```text
assigned_candidates = found_papers + confirmed_not_found + unprocessed_papers
                    + discovery_failed + missing_manifests + invalid_manifests
                    + other_unresolved

found_papers = m1_complete_papers + partial_papers + failed_papers
             + interrupted_papers + unresolved_found_papers

terminal_versions = successful_tex_versions + no_tex_versions + withdrawn_versions

total_discovered_versions = terminal_versions + incomplete_versions
                          + failed_versions + invalid_versions
```

`m1_complete_papers` không được lớn hơn bất kỳ chỉ số `source_complete_papers`, `metadata_complete_papers` và `references_complete_papers`. Mọi chênh lệch phải được ghi nhận; số liệu mất cân bằng không được đánh dấu đã đối chiếu thành công.

## Xuất xứ số liệu

Khối `provenance` ghi `report_generated_at`, `repository_commit`, `metric_definition_version`, `storage_contract_version`, `corpus_root`, `assigned_candidate_count` và `report_scope`. Nếu không xác định được commit, ghi `UNKNOWN` và cảnh báo. Không ghi khóa API hoặc token.

## Tên trường tương thích

| Tên tương thích | Chỉ số tương ứng |
|---|---|
| `completed_papers` | `source_complete_papers` |
| `completed_versions` | `terminal_versions` |
| `success_rate` | `source_completion_rate` |
| `total_references` | `total_complete_reference_records` |
| `papers_with_references` | `complete_reference_papers` |

`is_reconciled` cho biết kết quả đối chiếu số liệu.
