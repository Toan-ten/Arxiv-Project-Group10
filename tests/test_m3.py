"""Offline tests for M3 reference matching (synthetic only)."""
import unittest
from src.m3.matching import (
    normalize_title,
    jaccard_authors,
    score_candidate,
    build_candidates,
    match_topk,
    mrr_at_k,
)


class TestM3Matching(unittest.TestCase):
    def test_normalize_title(self):
        self.assertEqual(normalize_title("A Title: with Punct!"), "a title with punct")
        self.assertEqual(normalize_title(None), "")

    def test_jaccard_authors(self):
        self.assertAlmostEqual(jaccard_authors(["Smith", "Jones"], ["Smith"]), 0.5)
        self.assertEqual(jaccard_authors(None, ["Smith"]), 0.0)

    def test_score_exact_doi(self):
        be = {"title": "T", "doi": "10.1/x", "authors": ["A"], "year": 2020}
        ref = {"title": "Other", "doi": "10.1/x", "authors": ["B"], "year": 2019}
        self.assertEqual(score_candidate(be, ref), 1.0)

    def test_score_title_match(self):
        be = {"title": "Graph Neural Networks", "authors": ["Smith"], "year": 2021}
        ref = {"title": "Graph Neural Networks", "authors": ["Smith"], "year": 2021}
        self.assertGreater(score_candidate(be, ref), 0.9)

    def test_match_topk(self):
        be = {"citation_key": "k", "title": "Hello World", "authors": ["A"], "year": 2020}
        refs = [
            {"title": "Hello World", "authors": ["A"], "year": 2020},
            {"title": "Unrelated", "authors": ["B"], "year": 2019},
        ]
        res = match_topk([be], refs, k=2)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0][1][0]["title"], "Hello World")

    def test_mrr_at_k(self):
        preds = [[2, 3, 1], [5]]
        gt = [1, 5]
        # first query: 1 at rank3 -> 1/3; second: 5 at rank1 -> 1
        self.assertAlmostEqual(mrr_at_k(preds, gt, k=5), (1/3 + 1) / 2)

    def test_build_candidates_empty(self):
        self.assertEqual(build_candidates([], [{"title": "x"}]), [])
        self.assertEqual(build_candidates([{"title": "x"}], []), [({"title": "x"}, [])])


if __name__ == "__main__":
    unittest.main()


class TestM3Corpus(unittest.TestCase):
    def test_directory_load_reads_only_the_named_table(self):
        """A run writes data/bibtex, data/elements and data/hierarchy side by side."""
        import pyarrow as pa
        import pyarrow.parquet as pq
        import tempfile
        from pathlib import Path
        from src.m3.corpus import load_bibtex, load_references

        with tempfile.TemporaryDirectory() as scratch:
            data = Path(scratch) / "data"
            for table, rows in (
                ("bibtex", [{"arxiv_id": "2508.00001", "key": "a2020", "title": "Bib"} ]),
                ("elements", [{"arxiv_id": "2508.00001", "kind": "sentence", "content": "Prose"} ]),
                ("hierarchy", [{"arxiv_id": "2508.00001", "child": "x", "parent": "y"} ]),
                ("references", [{"arxiv_id": "2508.00001", "title": "Ref", "doi": "10.1/x"} ]),
            ):
                folder = data / table
                folder.mkdir(parents=True)
                pq.write_table(pa.Table.from_pylist(rows), folder / "part-0.parquet")

            bibtex = load_bibtex(str(data))
            self.assertEqual([row["key"] for row in bibtex], ["a2020"], bibtex)
            references = load_references(str(data))
            self.assertEqual([row["doi"] for row in references], ["10.1/x"], references)

    def test_missing_table_fails_instead_of_loading_everything(self):
        import tempfile
        from pathlib import Path
        from src.m3.corpus import load_bibtex

        with tempfile.TemporaryDirectory() as scratch:
            (Path(scratch) / "elements").mkdir(parents=True)
            with self.assertRaises(FileNotFoundError):
                load_bibtex(scratch)


class TestM3Matrix(unittest.TestCase):
    """The candidate matrix is the structure M3 labelling and modelling both hang on."""

    def _rows(self):
        entries = [
            dict(arxiv_id="2508.00001", citation_key="smith2020", title="Deep Learning for Graphs",
                 authors="['Smith, John']", year="2020", doi="10.1/x"),
            dict(arxiv_id="2508.00001", citation_key="bare1999", title=None, authors=None, year="1999", doi=None),
        ]
        refs = [
            dict(arxiv_id="2508.00001", ref_arxiv_id="2001.00001", ref_title="Deep learning for graphs",
                 ref_authors="['John Smith']", ref_submission_date="2020-05-01"),
            dict(arxiv_id="2508.00001", ref_arxiv_id="1999.00002", ref_title="Something unrelated",
                 ref_authors="['Ada Lovelace']", ref_submission_date="2019-01-01"),
        ]
        return entries, refs

    def test_cells_are_entries_times_references(self):
        from src.m3.matrix import candidate_cells, FEATURE_COLUMNS

        entries, refs = self._rows()
        cells = candidate_cells(entries, refs)
        self.assertEqual(len(cells), len(entries) * len(refs))
        for column in FEATURE_COLUMNS:
            self.assertIn(column, cells[0])

    def test_matching_pair_scores_above_unrelated_pair(self):
        from src.m3.matrix import pair_features

        entries, refs = self._rows()
        hit = pair_features(entries[0], refs[0])
        miss = pair_features(entries[0], refs[1])
        self.assertGreater(hit["title_sequence_ratio"], miss["title_sequence_ratio"])
        self.assertGreater(hit["author_jaccard"], miss["author_jaccard"])
        self.assertEqual(hit["doi_exact"], 0.0)  # the reference carries no DOI
        self.assertEqual(hit["first_author_match"], 1.0)

    def test_entry_without_title_is_flagged_not_silently_scored(self):
        from src.m3.matrix import pair_features

        entries, refs = self._rows()
        bare = pair_features(entries[1], refs[0])
        self.assertEqual(bare["entry_has_title"], 0.0)
        self.assertEqual(bare["entry_has_authors"], 0.0)

    def test_shortlist_requires_labelable_entries_and_orders_by_cost(self):
        from src.m3.matrix import select_labeling_publications

        entries, refs = self._rows()
        entries = [dict(e, arxiv_id="2508.00001") for e in entries * 11]   # 22 rows, 11 labelable
        shortlist, profiles = select_labeling_publications(entries, refs, minimum_entries=20)
        self.assertEqual(profiles[0]["labelable_entries"], 11)
        self.assertEqual(shortlist, [])
        shortlist, _ = select_labeling_publications(entries, refs, minimum_entries=10)
        self.assertEqual([p["arxiv_id"] for p in shortlist], ["2508.00001"])


class TestM3Review(unittest.TestCase):
    """The labelling page: drafts, assignment, export validation."""

    def _publication(self, arxiv_id="2508.00001", entries=3):
        bib = []
        for index in range(entries):
            bib.append(dict(
                arxiv_id=arxiv_id, citation_key=f"key{index}", entry_type="article",
                title=f"Phytoplankton blooms and toxin release part {index}",
                authors=[f"Surname{index}, A., Coauthor{index}, B.,"], year=2019,
                doi=None, journal=None, url=None))
        refs = [
            dict(arxiv_id=arxiv_id, ref_arxiv_id="2507.00001",
                 ref_title="Phytoplankton blooms and toxin release part 0",
                 ref_authors=["A. Surname0"], ref_submission_date="2025-07-24"),
            dict(arxiv_id=arxiv_id, ref_arxiv_id="2507.00002",
                 ref_title="Unrelated work on graph neural networks",
                 ref_authors=["B. Other"], ref_submission_date="2024-01-01"),
        ]
        return bib, refs

    def test_published_quota_matches_the_statement(self):
        from src.m3.review import quota

        four = quota(4)
        self.assertEqual((four["publications"], four["entries"]), (20, 400))
        self.assertEqual(four["entries_per_publication"], 20)
        self.assertEqual(four["auto_publications_floor"], 8)
        self.assertEqual((four["test_manual"], four["valid_manual"]), (4, 4))

    def test_payload_assigns_an_equal_share_and_skips_unlabelable_entries(self):
        from src.m3.review import build_payload

        bib, refs = self._publication(entries=3)
        bib.append(dict(bib[0], citation_key="bare", title=None, authors=None))
        shortlist = [dict(arxiv_id="2508.00001")]
        payload = build_payload(bib, refs, shortlist, members=2, names=["ana", "bo"])
        card = payload["publications"][0]
        self.assertEqual(len(card["entries"]), 3)          # the bare entry stays out
        self.assertNotIn("bare", [entry["key"] for entry in card["entries"]])
        self.assertEqual(payload["summary"]["entries"], 3)

    def test_clear_match_is_suggested_and_unrelated_entries_get_no_pick(self):
        from src.m3.review import draft_suggestions

        bib, refs = self._publication(entries=3)
        drafts = {draft["key"]: draft for draft in draft_suggestions(bib, refs)}
        self.assertEqual(drafts["key0"]["pick"], "2507.00001")
        self.assertEqual(drafts["key0"]["tier"], "high")
        self.assertGreater(drafts["key0"]["margin"], 0.1)
        self.assertIn("title", drafts["key0"]["why"])
        self.assertIsNone(drafts["key1"]["pick"])           # nothing convincing to offer
        self.assertEqual(drafts["key1"]["tier"], "low")

    def test_candidate_scores_are_ordered_for_the_page(self):
        from src.m3.review import draft_suggestions

        bib, refs = self._publication(entries=1)
        draft = draft_suggestions(bib, refs)[0]
        scores = [candidate["score"] for candidate in draft["candidates"]]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertEqual(draft["candidates"][0]["id"], "2507.00001")

    def test_page_render_cannot_be_closed_by_a_title(self):
        from src.m3.review import build_payload, render_page

        bib, refs = self._publication(entries=1)
        bib[0]["title"] = "Injection </script><script>alert(1)</script> attempt"
        payload = build_payload(bib, refs, [dict(arxiv_id="2508.00001")], members=1, names=["ana"])
        from pathlib import Path
        import src.m3 as m3

        page = render_page(payload, Path(m3.__file__).parent / "labelling_page.html")
        self.assertEqual(page.count("</script>"), 2)        # payload + app, nothing injected
        self.assertIn("<\\/script>", page)
        self.assertNotIn("__PAYLOAD__", page)

    def test_export_without_a_reviewer_is_not_a_label_set(self):
        from src.m3.review import validate_labels

        rows = [dict(arxiv_id="2508.00001", citation_key=f"k{i}", groundtruth_arxiv_id=None,
                     reviewer="", reviewed_at="2026-01-01T00:00:00Z") for i in range(20)]
        report = validate_labels(rows, expected_publications=1, minimum_entries=20)
        self.assertFalse(report["ok"])
        self.assertTrue(any("no reviewer" in problem for problem in report["problems"]))
        for row in rows:
            row["reviewer"] = "ana"
        report = validate_labels(rows, expected_publications=1, minimum_entries=20)
        self.assertTrue(report["ok"], report["problems"])
        self.assertEqual((report["labels"], report["none_rows"]), (20, 20))

    def test_serialised_author_lists_yield_every_surname(self):
        from src.m3.matrix import element_surnames, surnames

        # M2 packs a whole author list into one element, M1 uses one element per author.
        self.assertEqual(element_surnames("Chattopadhayay, J., Sarkar, R. R., Mandal, S.,"),
                         ["chattopadhayay", "sarkar", "mandal"])
        self.assertEqual(element_surnames("Abdar, Moloud"), ["abdar"])
        self.assertEqual(element_surnames("Evdoxia Taka"), ["taka"])
        self.assertEqual(surnames(["Smith, John, Doe, Jane,"]), {"smith", "doe"})
