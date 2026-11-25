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
