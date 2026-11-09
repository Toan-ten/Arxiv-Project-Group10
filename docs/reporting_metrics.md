# Đặc Tả Chỉ Số Thống Kê & Báo Cáo Milestone 1 (Canonical Reporting Metric Specification)

**Phiên bản định nghĩa số liệu (Metric Definition Version):** `2.1.0`  
**Tuân thủ:** Hợp đồng lưu trữ Milestone 1 v2 ([`docs/m1_contract.md`](file:///home/tuan-anh/Documents/DS_PJ/Arxiv-Project-Group10/docs/m1_contract.md))  
**Tài liệu tham chiếu:** [`reports/remediation/storage_contract_audit.md`](file:///home/tuan-anh/Documents/DS_PJ/Arxiv-Project-Group10/reports/remediation/storage_contract_audit.md), [`reports/remediation/reporting_audit.md`](file:///home/tuan-anh/Documents/DS_PJ/Arxiv-Project-Group10/reports/remediation/reporting_audit.md)  
**Ngày hiệu lực:** 2026-11-09  

---

## 1. Nguyên Tắc Thiết Kế Cốt Lõi (Core Principles)

1. **Bằng chứng thực tế trên đĩa (Evidence-First):** Không dựa vào các trường trạng thái chuỗi trong `manifest.json` để kết luận tính hoàn tất. Một bài báo chỉ đạt `source_complete`, `metadata_complete`, `references_complete`, hay `m1_complete` khi dữ liệu thực tế trên đĩa vượt qua đầy đủ kiểm toán băm SHA-256 và xác thực schema hợp đồng v2.
2. **Phân hoạch đầy đủ và không chồng lấn (Mutually Exclusive & Exhaustive Partition):** Toàn bộ định danh ứng viên được phân công (`assigned_candidates`) phải được phân bổ chính xác vào các tập trạng thái không giao nhau, bảo đảm phương trình đối chiếu (reconciliation equation) luôn cân bằng.
3. **Mẫu số tường minh và ngữ nghĩa Null (Explicit Denominators & Null Semantics):** Mọi tỷ lệ phần trăm đều có mẫu số xác định rõ ràng. Khi mẫu số bằng 0, giá trị trả về trong JSON là `None` (null) và trong bảng hiển thị là `N/A`, tuyệt đối không hiển thị `0.0%` gây hiểu nhầm.
4. **Không nuốt lỗi (No Silent Failures):** Mọi lỗi định dạng JSON, lỗi I/O hệ thống tệp, hoặc vi phạm tính toàn vẹn đều được ghi nhận vào các chỉ số lỗi tương ứng (`invalid_manifests`, `invalid_references`, `integrity_failed_papers`, `unresolved_papers`).

---

## 2. Đặc Tả Chỉ Số Cấp Bài Báo (Paper-Level Metrics)

### 2.1. Nhóm Kế Toán Phân Bổ & Trạng Thái Phát Hiện (Discovery Accounting)

#### `assigned_candidates`
- **Ý nghĩa:** Tổng số vị trí định danh arXiv ID được phân công cho nhóm theo cấu hình chính thức.
- **Đơn vị:** Định danh (ID count).
- **Nguồn dữ liệu:** `config/group10_id_ranges.json` thông qua `src.id_ranges.iter_assigned_ids()`.
- **Điều kiện:** Mọi cặp `(mssv, arxiv_id)` hợp lệ được sinh ra từ dải phân công.

#### `found_papers`
- **Ý nghĩa:** Số bài báo được xác nhận tồn tại trên arXiv thông qua bước Discovery (`discovery_status == "found"`).
- **Đơn vị:** Bài báo.
- **Điều kiện:** Manifest tồn tại, hợp lệ, có `discovery_status == "found"` và `arxiv_id` khớp danh mục phân công.

#### `confirmed_not_found`
- **Ý nghĩa:** Số định danh được xác nhận âm tính hợp lệ (`discovery_status == "not_found"`).
- **Đơn vị:** Định danh.
- **Điều kiện:** Manifest hợp lệ, `discovery_status == "not_found"`, `status == "not_found"`, `versions == {}` (từ điển rỗng), và không có thư mục `vN/` nào trên đĩa.

#### `unprocessed_papers`
- **Ý nghĩa:** Số bài báo chưa được tiến trình cào xử lý (`discovery_status == "not_attempted"`).
- **Đơn vị:** Bài báo.
- **Điều kiện:** Manifest hợp lệ nhưng ở trạng thái sơ khởi (`not_attempted`).

#### `missing_manifests`
- **Ý nghĩa:** Số bài báo có thư mục bài báo hoặc định danh phân công nhưng hoàn toàn thiếu tệp `manifest.json`.
- **Đơn vị:** Bài báo.
- **Điều kiện:** Tệp `manifest.json` không tồn tại trên hệ thống tệp.

#### `invalid_manifests`
- **Ý nghĩa:** Số bài báo có tệp `manifest.json` nhưng bị hỏng cấu trúc cú pháp JSON, sai phiên bản hợp đồng (`contract_version != 2`), hoặc không vượt qua kiểm thực schema `CorpusStorage._manifest()`.
- **Đơn vị:** Bài báo.
- **Điều kiện:** Phát sinh `IntegrityError`, `StorageError`, `json.JSONDecodeError`, hoặc `OSError` khi đọc manifest.

#### `discovery_failed`
- **Ý nghĩa:** Số bài báo có lượt thử discovery nhưng kết thúc ở trạng thái lỗi (`discovery_status == "failed"` hoặc thuộc tập lỗi discovery).
- **Đơn vị:** Bài báo.

#### `other_unresolved`
- **Ý nghĩa:** Số bài báo ở trạng thái không thể phân loại an toàn vào các nhóm trên (ví dụ: lỗi hệ thống tệp bất thường, định danh sai quy chuẩn).
- **Đơn vị:** Bài báo.

---

### 2.2. Nhóm Chỉ Số Hoàn Tất Giai Đoạn (Stage & Milestone Completion)

#### `source_complete_papers`
- **Ý nghĩa:** Số bài báo có toàn bộ các phiên bản đã được thu thập và xác minh trọn vẹn mã nguồn trên đĩa.
- **Đơn vị:** Bài báo.
- **Điều kiện bắt buộc (đồng thời):**
  1. `discovery_status == "found"`.
  2. `version_status == "resolved"`.
  3. `manifest["versions"]` chứa ít nhất 1 phiên bản.
  4. 100% các phiên bản đạt trạng thái kết thúc hợp lệ (`TERMINAL = {"success", "no_tex_source"}`).
  5. 100% các phiên bản vượt qua kiểm toán `audit_version_integrity()`: tệp nguồn tồn tại trên đĩa, mã băm SHA-256 khớp 100% với `file_checksums`, tổng dung lượng khớp `kept_bytes`, không có symlink/hardlink, và thư mục phiên bản tuân thủ quy chuẩn.

#### `metadata_complete_papers`
- **Ý nghĩa:** Số bài báo có siêu dữ liệu học thuật được thu thập và xác minh hoàn tất.
- **Đơn vị:** Bài báo.
- **Điều kiện bắt buộc (đồng thời):**
  1. Tệp `metadata.json` tồn tại và hợp lệ schema hợp đồng v2.
  2. `metadata["status"] == "complete"`.
  3. Khớp định danh: `metadata["arxiv_id"] == arxiv_id` và `metadata["mssv"] == mssv`.
  4. Nhất quán liên tệp: `manifest["metadata_status"] == metadata["status"]`.
  5. Các trường bắt buộc (`title`, `authors`, `submission_date`) hợp lệ và danh sách phiên bản có ngày tháng được xác minh.

#### `references_complete_papers`
- **Ý nghĩa:** Số bài báo có quan hệ trích dẫn được thu thập và xác nhận hoàn tất đầy đủ độ phủ từ Semantic Scholar.
- **Đơn vị:** Bài báo.
- **Điều kiện bắt buộc (đồng thời):**
  1. Tệp `references.json` tồn tại và hợp lệ schema hợp đồng v2.
  2. `references["status"] == "complete"`.
  3. Độ phủ hoàn tất: `references["coverage"]["complete"] is True` và `references["coverage"]["continuation"] is None`.
  4. Nhất quán liên tệp: `manifest["references_status"] == references["status"]`.
  5. *Lưu ý:* Trường hợp bài báo thực sự không có trích dẫn (`items == []`) nhưng có `coverage.complete is True` được tính là hoàn tất hợp lệ.

#### `m1_complete_papers`
- **Ý nghĩa:** Số bài báo hoàn tất toàn bộ mọi tiêu chí chất lượng của Milestone 1.
- **Đơn vị:** Bài báo.
- **Bất biến M1 (M1 Completion Invariant):**
  $$\text{m1\_complete\_papers} = \text{source\_complete} \land \text{metadata\_complete} \land \text{references\_complete} \land (\text{manifest.status} \in \{\text{"complete"}, \text{"complete\_with\_no\_tex"}\}) \land (\text{violations} == 0)$$
  *Bất đẳng thức kiểm toán bắt buộc:*
  $$\text{m1\_complete\_papers} \le \text{source\_complete\_papers}$$
  $$\text{m1\_complete\_papers} \le \text{metadata\_complete\_papers}$$
  $$\text{m1\_complete\_papers} \le \text{references\_complete\_papers}$$

---

### 2.3. Nhóm Chỉ Số Trạng Thái Vòng Đời Bài Báo (Lifecycle Stage Accounting)

Đối với các bài báo `found` nhưng chưa đạt `m1_complete`:
- **`partial_papers`:** `manifest["status"] == "partial"` (cần tiếp tục chạy resume).
- **`failed_papers`:** `manifest["status"] == "failed"` (gặp lỗi terminal ở một giai đoạn).
- **`interrupted_papers`:** `manifest["status"] == "interrupted"` (bị dừng do timeout hoặc hủy phiên).
- **`integrity_failed_papers`:** Bài báo có ghi nhận `violations` trong kiểm toán tính toàn vẹn (tệp hỏng, sai mã băm, tệp rác unmanaged).

---

## 3. Đặc Tả Chỉ Số Cấp Phiên Bản (Version-Level Metrics)

Áp dụng cho toàn bộ các phiên bản bài báo nằm trong các bài báo `found`:

| Tên chỉ số | Ý nghĩa | Điều kiện |
|---|---|---|
| `total_discovered_versions` | Tổng số phiên bản bài báo quan sát được | Tổng số phần tử trong từ điển `manifest.versions` |
| `successful_tex_versions` | Phiên bản trích xuất TeX thành công | `v.status == "success"` và tệp `.tex` được xác minh trên đĩa |
| `no_tex_versions` | Phiên bản không chứa mã nguồn TeX hợp lệ | `v.status == "no_tex_source"` (đạt chuẩn không có `.tex`) |
| `terminal_versions` | Tổng phiên bản ở trạng thái kết thúc | `successful_tex_versions + no_tex_versions` |
| `incomplete_versions` | Phiên bản đang xử lý dở dang | `v.status in {"pending", "in_progress", "interrupted"}` |
| `failed_versions` | Phiên bản gặp lỗi bóc tách | `v.status in SOURCE_ERRORS` |
| `invalid_versions` | Phiên bản sai schema hoặc định dạng | Phiên bản không đúng schema hoặc tên phiên bản sai chuẩn |
| `source_integrity_failed_versions` | Phiên bản lỗi mã băm hoặc tệp | Phiên bản có vi phạm trong `audit_version_integrity()` |

**Bất biến phiên bản (Version Invariant):**
$$\text{terminal\_versions} = \text{successful\_tex\_versions} + \text{no\_tex\_versions}$$
$$\text{total\_discovered\_versions} = \text{terminal\_versions} + \text{incomplete\_versions} + \text{failed\_versions} + \text{invalid\_versions} + \text{unresolved\_versions}$$

---

## 4. Đặc Tả Chỉ Số Cấp Trích Dẫn (References-Level Metrics)

| Tên chỉ số | Ý nghĩa | Điều kiện |
|---|---|---|
| `complete_reference_papers` | Bài báo có trích dẫn hoàn tất | `references.status == "complete"` và `coverage.complete is True` |
| `partial_reference_papers` | Bài báo có trích dẫn dở dang | `references.status == "partial"` hoặc (`complete` nhưng `coverage.complete == False`) |
| `failed_reference_papers` | Bài báo lỗi thu thập trích dẫn | `references.status == "failed"` |
| `not_attempted_reference_papers` | Bài báo chưa thu thập trích dẫn | `references.status == "not_attempted"` hoặc chưa có tệp |
| `invalid_reference_papers` | Bài báo có tệp trích dẫn sai hỏng | Lỗi cú pháp JSON hoặc vi phạm schema |
| `total_complete_reference_records` | Tổng số liên kết trích dẫn hoàn tất | Tổng số phần tử `items` trong các bài báo đạt `complete_reference_papers` |
| `total_observed_reference_records` | Tổng số bản ghi trích dẫn đọc được | Tổng số phần tử `items` trong tất cả các tệp `references.json` đọc được |

---

## 5. Đặc Tả Tỷ Lệ & Quy Tắc Mẫu Số (Rates & Denominators)

| Tên tỷ lệ | Công thức tính | Mẫu số (Denominator) | Quy tắc khi Mẫu số = 0 |
|---|---|---|---|
| `source_completion_rate` | $\frac{\text{source\_complete\_papers}}{\text{found\_papers}} \times 100\%$ | `found_papers` | Trả về `None` trong JSON; hiển thị `N/A` trong Markdown |
| `metadata_completion_rate` | $\frac{\text{metadata\_complete\_papers}}{\text{found\_papers}} \times 100\%$ | `found_papers` | Trả về `None` trong JSON; hiển thị `N/A` trong Markdown |
| `references_coverage_rate` | $\frac{\text{complete\_reference\_papers}}{\text{found\_papers}} \times 100\%$ | `found_papers` | Trả về `None` trong JSON; hiển thị `N/A` trong Markdown |
| `m1_completion_rate` | $\frac{\text{m1\_complete\_papers}}{\text{found\_papers}} \times 100\%$ | `found_papers` | Trả về `None` trong JSON; hiển thị `N/A` trong Markdown |
| `version_extraction_success_rate` | $\frac{\text{successful\_tex\_versions}}{\text{total\_discovered\_versions}} \times 100\%$ | `total_discovered_versions` | Trả về `None` trong JSON; hiển thị `N/A` trong Markdown |
| `no_tex_rate` | $\frac{\text{no\_tex\_versions}}{\text{total\_discovered\_versions}} \times 100\%$ | `total_discovered_versions` | Trả về `None` trong JSON; hiển thị `N/A` trong Markdown |
| `average_references_per_paper` | $\frac{\text{total\_complete\_reference\_records}}{\text{complete\_reference\_papers}}$ | `complete_reference_papers` | Trả về `None` trong JSON; hiển thị `N/A` trong Markdown |

---

## 6. Các Phương Trình Đối Chiếu Bắt Buộc (Reconciliation Invariants)

Báo cáo phải tự động đối chiếu các phương trình số học sau. Bất kỳ sự mất cân bằng nào đều được phát hiện và cảnh báo:

### 6.1. Đối chiếu tổng thể bài báo (Paper Population Reconciliation)
$$\text{assigned\_candidates} = \text{found\_papers} + \text{confirmed\_not\_found} + \text{unprocessed\_papers} + \text{discovery\_failed} + \text{missing\_manifests} + \text{invalid\_manifests} + \text{other\_unresolved}$$

### 6.2. Đối chiếu bài báo tìm thấy (Found Papers Lifecycle Reconciliation)
Đối với tập `found_papers`:
$$\text{found\_papers} = \text{m1\_complete\_papers} + \text{partial\_papers} + \text{failed\_papers} + \text{interrupted\_papers} + \text{unresolved\_found\_papers}$$
Trong đó `unresolved_found_papers` là các bài báo `found` nhưng không rơi vào 4 nhóm vòng đời chuẩn hoặc có vi phạm tính toàn vẹn khiến trạng thái không thể xác định.

### 6.3. Bất biến thứ tự hoàn tất M1 (M1 Completion Hierarchy)
$$\text{m1\_complete\_papers} \le \min(\text{source\_complete\_papers}, \text{metadata\_complete\_papers}, \text{references\_complete\_papers})$$

---

## 7. Xuất Xứ Báo Cáo (Provenance & Reproducibility Contract)

Báo cáo JSON và Markdown bắt buộc phải chứa khối `provenance` gồm:
- `report_generated_at`: Thời điểm tạo báo cáo theo chuẩn ISO 8601 UTC.
- `repository_commit`: Mã băm Git SHA đầy đủ (40 ký tự) của repository chứa mã nguồn đang chạy. Nếu không xác định được, ghi `UNKNOWN` kèm cảnh báo.
- `metric_definition_version`: `"2.1.0"`.
- `storage_contract_version`: `2`.
- `corpus_root`: Đường dẫn chuẩn hóa đến thư mục gốc corpus.
- `assigned_candidate_count`: Số lượng ứng viên trong cấu hình phân công.
- `report_scope`: Phạm vi kiểm toán (ví dụ: toàn bộ dải phân công, hoặc danh sách cohort cụ thể).
- Tuyệt đối không chứa khóa API, token, hay thông tin nhạy cảm.

---

## 8. Tính Tương Thích Ngược (Backward Compatibility Aliases)

Để bảo đảm các đoạn mã cũ hoặc sổ tay Jupyter Notebook không bị gián đoạn, báo cáo giữ lại các trường alias có tài liệu:
- `completed_papers` $\rightarrow$ gán bằng `source_complete_papers` (kèm tài liệu chỉ rõ đây là source completion, không phải M1 complete).
- `completed_versions` $\rightarrow$ gán bằng `terminal_versions`.
- `success_rate` $\rightarrow$ gán bằng `source_completion_rate`.
- `total_references` $\rightarrow$ gán bằng `total_complete_reference_records`.
- `papers_with_references` $\rightarrow$ gán bằng `complete_reference_papers`.
- Bổ sung trường mới `is_reconciled: bool` thông báo tình trạng hợp chuẩn số học của báo cáo.
