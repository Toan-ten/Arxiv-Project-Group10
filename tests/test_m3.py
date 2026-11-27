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
