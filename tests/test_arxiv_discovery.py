import unittest
from xml.sax.saxutils import escape

from src.arxiv_discovery import parse_arxiv_response
from src.id_ranges import iter_assigned_ids


MSSV = "24120232"
ARXIV_ID = "2502.08235"
PUBLISHED = "2025-02-12T09:00:00Z"
UPDATED = "2025-03-01T10:00:00Z"


def make_entry(identifier=f"http://arxiv.org/abs/{ARXIV_ID}v1", title="Sample paper",
               authors=("First Author", "Second Author"), published=PUBLISHED,
               updated=UPDATED, extra=""):
    fields = []
    for tag, value in (("id", identifier), ("title", title),
                       ("published", published), ("updated", updated)):
        if value is not None:
            fields.append(f"<{tag}>{escape(value)}</{tag}>")
    for author in authors:
        name = f"<name>{escape(author)}</name>" if author is not None else ""
        fields.append(f"<author>{name}</author>")
    return "<entry>" + "".join(fields) + extra + "</entry>"


def make_feed(entries, total="1", start="0"):
    metadata = ""
    for tag, value in (("totalResults", total), ("startIndex", start)):
        if value is not None:
            metadata += f"<opensearch:{tag}>{escape(value)}</opensearch:{tag}>"
    return (
        '<feed xmlns="http://www.w3.org/2005/Atom" '
        'xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">'
        + metadata + entries + "</feed>"
    )


class ArxivDiscoveryTests(unittest.TestCase):
    def parse(self, xml):
        result = parse_arxiv_response(xml, MSSV, ARXIV_ID)
        self.assertEqual(result["mssv"], MSSV)
        self.assertEqual(result["arxiv_id"], ARXIV_ID)
        return result

    def test_one_version_includes_v1(self):
        result = self.parse(make_feed(make_entry()))
        self.assertEqual(result["status"], "found")
        self.assertEqual(result["version_status"], "resolved")
        self.assertEqual(result["latest_version_id"], f"{ARXIV_ID}v1")
        self.assertEqual(result["version_ids"], [f"{ARXIV_ID}v1"])

    def test_three_versions_include_initial_submission(self):
        links = (f'<link rel="alternate" href="http://arxiv.org/abs/{ARXIV_ID}v3"/>'
                 f'<link rel="related" href="http://arxiv.org/pdf/{ARXIV_ID}v3"/>')
        xml = make_feed(make_entry(identifier=f"https://arxiv.org/abs/{ARXIV_ID}v3",
                                   extra=links))
        result = self.parse(xml)
        self.assertEqual(result["status"], "found")
        self.assertEqual(result["latest_version_id"], f"{ARXIV_ID}v3")
        self.assertEqual(result["version_ids"],
                         [f"{ARXIV_ID}v1", f"{ARXIV_ID}v2", f"{ARXIV_ID}v3"])

    def test_metadata_and_whitespace(self):
        xml = make_feed(make_entry(title="  Example\n paper & results ",
                                  authors=("  Nguyễn Văn Toàn ", "First\n Author")))
        result = self.parse(xml)
        self.assertEqual(result["title"], "Example paper & results")
        self.assertEqual(result["authors"], ["Nguyễn Văn Toàn", "First Author"])
        self.assertEqual(result["published"], PUBLISHED)
        self.assertEqual(result["updated"], UPDATED)
        self.assertEqual(result["missing_fields"], [])
        self.assertIsNone(result["error"])

    def test_utf8_bytes(self):
        xml = make_feed(make_entry(title="Nghiên cứu dữ liệu"))
        result = self.parse(xml.encode("utf-8"))
        self.assertEqual(result["title"], "Nghiên cứu dữ liệu")

    def test_prefixed_atom_namespace(self):
        xml = f'''<a:feed xmlns:a="http://www.w3.org/2005/Atom">
            <a:entry><a:id>http://arxiv.org/abs/{ARXIV_ID}v1</a:id>
            <a:title>Prefixed title</a:title></a:entry></a:feed>'''
        result = self.parse(xml)
        self.assertEqual(result["status"], "found")
        self.assertEqual(result["title"], "Prefixed title")

    def test_empty_feed_explicitly_reports_zero_results(self):
        result = self.parse(make_feed("", total="0"))
        self.assertEqual(result["status"], "not_found")
        self.assertEqual(result["version_ids"], [])
        self.assertEqual(result["version_status"], "not_applicable")
        self.assertIsNone(result["error"])

    def test_empty_feed_without_evidence_is_not_not_found(self):
        for total in (None, "1"):
            with self.subTest(total=total):
                result = self.parse(make_feed("", total=total))
                self.assertEqual(result["status"], "invalid_response")
                self.assertIn("totalResults=0", result["error"])

    def test_api_error_entry_preserves_message(self):
        identifier = "http://arxiv.org/api/errors#incorrect_id_format"
        xml = make_feed(make_entry(identifier=identifier, title="Error",
                                   extra="<summary>incorrect id format</summary>"))
        result = self.parse(xml)
        self.assertEqual(result["status"], "api_error")
        self.assertIn("incorrect id format", result["error"])
        self.assertIn(identifier, result["error"])
        self.assertEqual(result["version_ids"], [])

    def test_api_error_without_summary(self):
        xml = make_feed(make_entry(identifier="https://arxiv.org/api/errors#bad_query"))
        result = self.parse(xml)
        self.assertEqual(result["status"], "api_error")
        self.assertIn("arXiv API error entry", result["error"])

    def test_paper_titled_error_is_not_an_api_error(self):
        result = self.parse(make_feed(make_entry(title="Error")))
        self.assertEqual(result["status"], "found")

    def test_malformed_xml(self):
        for xml in ("", "<feed>", b"\xff", "<html>unclosed"):
            with self.subTest(xml=xml):
                result = self.parse(xml)
                self.assertEqual(result["status"], "invalid_xml")
                self.assertTrue(result["error"])

    def test_wrong_root_or_namespace(self):
        for xml in ("<html/>", "<feed/>", '<feed xmlns="urn:wrong"/>',
                    '<entry xmlns="http://www.w3.org/2005/Atom"/>'):
            with self.subTest(xml=xml):
                result = self.parse(xml)
                self.assertEqual(result["status"], "invalid_response")

    def test_wrong_namespace_or_nested_entry_is_not_absence(self):
        for entry in ('<entry xmlns="urn:wrong"/>',
                      "<wrapper>" + make_entry() + "</wrapper>"):
            with self.subTest(entry=entry):
                result = self.parse(make_feed(entry, total="0"))
                self.assertEqual(result["status"], "invalid_response")

    def test_response_must_match_requested_id(self):
        xml = make_feed(make_entry(identifier="http://arxiv.org/abs/2502.13234v2"))
        result = self.parse(xml)
        self.assertEqual(result["status"], "invalid_response")
        self.assertIn("received 2502.13234", result["error"])
        self.assertEqual(result["version_ids"], [])

    def test_matching_feed_self_link(self):
        link = (f'<link rel="self" href="https://arxiv.org/api/query?'
                f'search_query=&amp;id_list={ARXIV_ID}"/>')
        result = self.parse(make_feed(make_entry() + link))
        self.assertEqual(result["status"], "found")

    def test_self_link_for_other_or_versioned_lookup_is_rejected(self):
        for lookup in ("2502.13234", f"{ARXIV_ID}v1", f"{ARXIV_ID},2502.13234", ""):
            with self.subTest(lookup=lookup):
                link = f'<link rel="self" href="https://arxiv.org/api/query?id_list={lookup}"/>'
                result = self.parse(make_feed(make_entry() + link))
                self.assertEqual(result["status"], "invalid_response")
                self.assertIn("self link", result["error"])

    def test_missing_or_invalid_entry_id(self):
        for identifier in (None, "", "2502.08235v1", "https://example.org/abs/2502.08235v1",
                           "http://arxiv.org/abs/2502.08235v0",
                           "http://arxiv.org/abs/2502.08235v01"):
            with self.subTest(identifier=identifier):
                result = self.parse(make_feed(make_entry(identifier=identifier)))
                self.assertEqual(result["status"], "invalid_response")

    def test_multiple_entries_are_rejected(self):
        result = self.parse(make_feed(make_entry() + make_entry(), total="2"))
        self.assertEqual(result["status"], "invalid_response")
        self.assertIn("at most one entry", result["error"])

    def test_unknown_version_is_not_inferred_from_dates(self):
        for updated in (PUBLISHED, UPDATED):
            with self.subTest(updated=updated):
                xml = make_feed(make_entry(identifier=f"http://arxiv.org/abs/{ARXIV_ID}",
                                           updated=updated))
                result = self.parse(xml)
                self.assertEqual(result["status"], "found")
                self.assertEqual(result["version_status"], "needs_verification")
                self.assertIsNone(result["latest_version_id"])
                self.assertEqual(result["version_ids"], [])

    def test_explicit_version_from_alternate_link(self):
        link = f'<link rel="alternate" href="http://arxiv.org/abs/{ARXIV_ID}v3"/>'
        xml = make_feed(make_entry(identifier=f"http://arxiv.org/abs/{ARXIV_ID}", extra=link))
        result = self.parse(xml)
        self.assertEqual(result["latest_version_id"], f"{ARXIV_ID}v3")
        self.assertEqual(len(result["version_ids"]), 3)

    def test_conflicting_version_or_alternate_id_is_rejected(self):
        for linked_id in (f"{ARXIV_ID}v2", "2502.13234v1"):
            with self.subTest(linked_id=linked_id):
                link = f'<link rel="alternate" href="http://arxiv.org/abs/{linked_id}"/>'
                result = self.parse(make_feed(make_entry(extra=link)))
                self.assertEqual(result["status"], "invalid_response")

    def test_missing_metadata_keeps_available_values(self):
        xml = make_feed(make_entry(title=None, authors=("Known Author", None),
                                   published=None))
        result = self.parse(xml)
        self.assertEqual(result["status"], "found")
        self.assertIsNone(result["title"])
        self.assertEqual(result["authors"], ["Known Author"])
        self.assertIsNone(result["published"])
        self.assertEqual(result["updated"], UPDATED)
        self.assertEqual(set(result["missing_fields"]),
                         {"title", "published", "authors[1].name"})
        self.assertEqual(result["version_ids"], [f"{ARXIV_ID}v1"])

    def test_all_basic_metadata_missing(self):
        xml = make_feed(make_entry(title=" ", authors=(), published=None, updated=None))
        result = self.parse(xml)
        self.assertEqual(result["status"], "found")
        self.assertEqual(result["authors"], [])
        self.assertEqual(set(result["missing_fields"]),
                         {"title", "authors", "published", "updated"})

    def test_duplicate_scalar_fields_are_rejected(self):
        for extra in ("<title>Other title</title>", f"<id>http://arxiv.org/abs/{ARXIV_ID}</id>"):
            with self.subTest(extra=extra):
                result = self.parse(make_feed(make_entry(extra=extra)))
                self.assertEqual(result["status"], "invalid_response")
                self.assertIn("Repeated field", result["error"])

    def test_inconsistent_or_invalid_opensearch_metadata(self):
        for total, start in (("0", "0"), ("2", "0"), ("-1", "0"),
                             ("bad", "0"), ("", "0"), ("1", "1"), ("1", "bad")):
            with self.subTest(total=total, start=start):
                result = self.parse(make_feed(make_entry(), total=total, start=start))
                self.assertEqual(result["status"], "invalid_response")

    def test_legacy_and_four_digit_ids_are_preserved(self):
        for base_id in ("hep-th/9901001", "0704.0001"):
            with self.subTest(base_id=base_id):
                xml = make_feed(make_entry(identifier=f"http://arxiv.org/abs/{base_id}v2"))
                result = parse_arxiv_response(xml, MSSV, base_id)
                self.assertEqual(result["status"], "found")
                self.assertEqual(result["arxiv_id"], base_id)
                self.assertEqual(result["version_ids"], [f"{base_id}v1", f"{base_id}v2"])

    def test_integration_preserves_mssv_from_assigned_ids(self):
        mssv, arxiv_id = next(iter_assigned_ids())
        xml = make_feed(make_entry(identifier=f"http://arxiv.org/abs/{arxiv_id}v1"))
        result = parse_arxiv_response(xml, mssv, arxiv_id)
        self.assertEqual(result["mssv"], mssv)
        self.assertEqual(result["arxiv_id"], arxiv_id)
        self.assertEqual(result["status"], "found")

    def test_invalid_arguments_are_caller_errors(self):
        for mssv, arxiv_id in ((24120232, ARXIV_ID), ("", ARXIV_ID),
                                (MSSV, ARXIV_ID + "v1"), (MSSV, "wrong")):
            with self.subTest(mssv=mssv, arxiv_id=arxiv_id), self.assertRaises(ValueError):
                parse_arxiv_response(make_feed(make_entry()), mssv, arxiv_id)
        with self.assertRaises(TypeError):
            parse_arxiv_response(None, MSSV, ARXIV_ID)


if __name__ == "__main__":
    unittest.main()
