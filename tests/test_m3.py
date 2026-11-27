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

    def test_latex_markup_does_not_hide_a_perfect_match(self):
        from src.m3.matching import normalize_title
        from src.m3.matrix import pair_features
        from src.m3.review import heuristic_score

        # The corpus writes the same title with and without markup.
        self.assertEqual(normalize_title(r"$\mathrm{Sr_2RuO_4}$"), normalize_title("Sr2RuO4"))
        self.assertEqual(normalize_title(r"Higher-T$_c$ Phase"), normalize_title("Higher-Tc Phase"))
        self.assertEqual(normalize_title(r'Bl\"ugel'), normalize_title("Blügel"))

        entry = dict(arxiv_id="2508.08026", citation_key="Mackenzie2017", entry_type="article",
                     title=r"the superconducting order parameter puzzle of $\mathrm{Sr_2RuO_4}$",
                     authors=["Mackenzie, A. P.,"], year=2017, doi=None, journal=None, url=None)
        ref = dict(arxiv_id="2508.08026", ref_arxiv_id="1706.01942",
                   ref_title="the superconducting order parameter puzzle of Sr2RuO4",
                   ref_authors=["Andrew P. Mackenzie"], ref_submission_date="2017-06-06")
        features = pair_features(entry, ref)
        self.assertEqual(features["title_sequence_ratio"], 1.0)
        self.assertEqual(features["title_token_jaccard"], 1.0)
        self.assertGreaterEqual(heuristic_score(features), 0.97)


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

    def test_bulk_classification_needs_three_agreements_to_auto_accept(self):
        from src.m3.review import bulk_classification

        # near-verbatim title, shared surname, no contradictory number
        self.assertEqual(bulk_classification("2507.1", 0.98, 0.99, 2), "accept")
        self.assertEqual(bulk_classification("2507.1", 0.98, 0.99, 2, number_conflict=True), "read")
        self.assertEqual(bulk_classification("2507.1", 0.98, 0.96, 2), "read")   # title short
        self.assertEqual(bulk_classification("2507.1", 0.98, 0.99, 0), "read")   # no author
        self.assertEqual(bulk_classification(None, 0.20, 0.25, 0), "none")
        self.assertEqual(bulk_classification(None, 0.50, 0.60, 0), "read")       # middle ground

    def test_payload_reports_how_much_needs_reading(self):
        from src.m3.review import build_payload

        bib, refs = self._publication(entries=3)
        payload = build_payload(bib, refs, [dict(arxiv_id="2508.00001")], members=1, names=["ana"])
        bulk = payload["summary"]["bulk"]
        self.assertEqual(sum(bulk.values()), payload["summary"]["entries"])
        self.assertEqual(bulk.get("accept"), 1)      # only key0 carries both title and surname
        self.assertGreaterEqual(bulk.get("read", 0), 2)

    def test_validate_labels_counts_bulk_against_read(self):
        from src.m3.review import validate_labels

        rows = [dict(arxiv_id="2508.00001", citation_key=f"k{i}", groundtruth_arxiv_id=None,
                     reviewer="ana", via="bulk" if i < 15 else "read",
                     reviewed_at="2026-01-01T00:00:00Z") for i in range(20)]
        report = validate_labels(rows, expected_publications=1, minimum_entries=20)
        self.assertTrue(report["ok"], report["problems"])
        self.assertEqual(report["via"], {"bulk": 15, "read": 5})


class TestM3AutoMatch(unittest.TestCase):
    """The classic matcher that fills the automatic pool."""

    def _corpus(self, arxiv_id="2508.20001"):
        entries = [
            dict(arxiv_id=arxiv_id, citation_key="good", entry_type="article",
                 title="Stability and bifurcation in a discrete plankton model",
                 authors=["Shoyimardonov, S.,"], year=2025, doi=None, journal=None, url=None),
            dict(arxiv_id=arxiv_id, citation_key="unrelated", entry_type="article",
                 title="A survey of graph neural networks for molecules",
                 authors=["Other, B.,"], year=2019, doi=None, journal=None, url=None),
        ]
        refs = [
            dict(arxiv_id=arxiv_id, ref_arxiv_id="2507.30001",
                 ref_title="Stability and bifurcation in a discrete plankton model",
                 ref_authors=["S. Shoyimardonov"], ref_submission_date="2025-07-24"),
            dict(arxiv_id=arxiv_id, ref_arxiv_id="2507.30003",
                 ref_title="Molecular property prediction with message passing networks",
                 ref_authors=["M. Zaripov"], ref_submission_date="2024-02-02"),
        ]
        return entries, refs

    def _sibling_corpus(self, arxiv_id="2508.20002"):
        """Two papers of one series: same authors, the title differs in the part number."""
        entries = [dict(arxiv_id=arxiv_id, citation_key="part2", entry_type="article",
                        title="Stability and bifurcation in a discrete plankton model part 2",
                        authors=["Shoyimardonov, S.,"], year=2025, doi=None, journal=None,
                        url=None)]
        refs = [
            dict(arxiv_id=arxiv_id, ref_arxiv_id="2507.30001",
                 ref_title="Stability and bifurcation in a discrete plankton model",
                 ref_authors=["S. Shoyimardonov"], ref_submission_date="2025-07-24"),
            dict(arxiv_id=arxiv_id, ref_arxiv_id="2507.30002",
                 ref_title="Stability and bifurcation in a discrete plankton model part 1",
                 ref_authors=["S. Shoyimardonov"], ref_submission_date="2025-07-01"),
        ]
        return entries, refs

    def test_verbatim_title_with_shared_surname_is_matched(self):
        from src.m3.auto_match import MATCHED, label_publication

        entries, refs = self._corpus()
        rows = {row["citation_key"]: row for row in label_publication(entries, refs)}
        self.assertEqual(rows["good"]["decision"], MATCHED)
        self.assertEqual(rows["good"]["groundtruth_arxiv_id"], "2507.30001")
        self.assertEqual(rows["good"]["method"], "title_and_author")

    def test_series_paper_is_not_matched_to_its_sibling(self):
        from src.m3.auto_match import label_publication

        entries, refs = self._sibling_corpus()
        row = label_publication(entries, refs)[0]
        # "part 2" against the title of "part 1": the numbers decide, so no label is invented.
        self.assertEqual(row["decision"], "auto_none")
        self.assertIn("number", row["reason"])

    def test_a_near_tie_is_left_unlabelled_rather_than_guessed(self):
        from src.m3.auto_match import label_publication

        entries, refs = self._sibling_corpus()
        exact = dict(refs[0], ref_title=entries[0]["title"])
        row = label_publication(entries, [exact, refs[1]])[0]
        # Both references carry the same title up to the part number, so the winner has no
        # margin over the runner-up; labelling either would be a coin flip.
        self.assertEqual(row["decision"], "auto_none")
        self.assertIn("margin", row["reason"])

    def test_unrelated_entry_carries_a_reason_not_a_label(self):
        from src.m3.auto_match import UNMATCHED, label_publication

        entries, refs = self._corpus()
        rows = {row["citation_key"]: row for row in label_publication(entries, refs)}
        self.assertEqual(rows["unrelated"]["decision"], UNMATCHED)
        self.assertIsNone(rows["unrelated"]["groundtruth_arxiv_id"])
        self.assertTrue(rows["unrelated"]["reason"])

    def test_a_reference_is_claimed_at_most_once(self):
        from src.m3.auto_match import label_publication

        entries, refs = self._corpus()
        twin = dict(entries[0], citation_key="duplicate")
        rows = label_publication([entries[0], twin], refs)
        claimed = [row["groundtruth_arxiv_id"] for row in rows if row["groundtruth_arxiv_id"]]
        self.assertEqual(len(claimed), len(set(claimed)))
        self.assertEqual(sum(1 for row in rows if row["decision"] == "auto_match"), 1)
        self.assertEqual(sum(1 for row in rows if row["decision"] == "auto_none"), 1)
        # Every "no" explains itself; a silent one would be impossible to audit.
        for row in rows:
            self.assertTrue(row["reason"], row)

    def test_pool_selection_samples_representative_publications(self):
        from src.m3.auto_match import select_auto_publications

        summary = {"a": dict(entries=30, matched=0, references=10),      # no matches
                   "b": dict(entries=30, matched=25, references=12),
                   "c": dict(entries=20, matched=20, references=20),
                   "d": dict(entries=400, matched=200, references=350),  # a survey
                   "e": dict(entries=40, matched=2, references=30)}      # below the floor
        selected, eligible = select_auto_publications(summary, limit=5, minimum_matches=10)
        ids = [row["arxiv_id"] for row in selected]
        self.assertEqual(ids, ["b", "c"])          # the survey and the two weak ones are out
        self.assertEqual(len(eligible), 2)
        selected, _ = select_auto_publications(summary, limit=5, minimum_matches=10, exclude={"c"})
        self.assertNotIn("c", [row["arxiv_id"] for row in selected])

    def test_pool_sampling_is_seeded_and_reproducible(self):
        from src.m3.auto_match import select_auto_publications

        summary = {f"2508.{index:05d}": dict(entries=50, matched=30, references=40)
                   for index in range(40)}
        first, _ = select_auto_publications(summary, limit=10, minimum_matches=10)
        second, _ = select_auto_publications(summary, limit=10, minimum_matches=10)
        self.assertEqual([row["arxiv_id"] for row in first], [row["arxiv_id"] for row in second])
        self.assertEqual(len(first), 10)
        other, _ = select_auto_publications(summary, limit=10, minimum_matches=10, seed=7)
        self.assertNotEqual([row["arxiv_id"] for row in first],
                            [row["arxiv_id"] for row in other])

    def test_pool_statistics_report_the_decision_split(self):
        from src.m3.auto_match import MATCHED, UNMATCHED, pool_statistics

        rows = [dict(decision=MATCHED, method="title_and_author"),
                dict(decision=UNMATCHED, method="none")]
        stats = pool_statistics(rows, {"p": dict(entries=2, matched=1, references=3)})
        self.assertEqual((stats["matched"], stats["unmatched"]), (1, 1))
        self.assertEqual(stats["match_rate"], 0.5)
        self.assertEqual(stats["methods"], {"title_and_author": 1})
        self.assertEqual(stats["publications_with_matches"], 1)

    def test_scores_do_not_saturate_so_evidence_still_orders_candidates(self):
        from src.m3.matrix import pair_features
        from src.m3.review import heuristic_score

        entries, refs = self._sibling_corpus()
        exact = dict(refs[0], ref_title=entries[0]["title"])
        identical = heuristic_score(pair_features(entries[0], exact))
        near_copy = heuristic_score(pair_features(entries[0], refs[1]))
        self.assertGreater(identical, near_copy)
        self.assertLessEqual(identical, 1.0)

    def test_calibration_sweeps_thresholds_against_human_labels(self):
        from src.m3.auto_match import MATCHED, calibrate, label_publication

        entries, refs = self._corpus()
        predicted = {row["citation_key"]: row for row in label_publication(entries, refs)}
        truth = [dict(arxiv_id="2508.20001", citation_key=key,
                      groundtruth_arxiv_id=row["groundtruth_arxiv_id"] if row["decision"] == MATCHED else None)
                 for key, row in predicted.items()]
        curve = calibrate(truth, entries, refs, thresholds=[0.5, 0.9], margins=[0.0, 0.2])
        self.assertEqual(len(curve), 4)
        for row in curve:
            self.assertTrue(0.0 <= row["precision"] <= 1.0)
            self.assertTrue(0.0 <= row["recall"] <= 1.0)
            self.assertEqual(row["actual_matches"], 1)
        lenient = [row for row in curve if row["threshold"] == 0.5][0]
        self.assertGreaterEqual(lenient["accepted"], 1)
        self.assertEqual(lenient["precision"], 1.0)


class TestM3Split(unittest.TestCase):
    """Train/validation/test partition at the statement's per-member quantities."""

    def _ids(self, count, prefix="2508."):
        return [f"{prefix}{index:05d}" for index in range(count)]

    def test_split_matches_the_per_member_quota(self):
        from src.m3.split import assign_partitions

        split = assign_partitions(self._ids(20), self._ids(245, "2509."), members=4)
        self.assertEqual(len(split["test"]["manual"]), 4)
        self.assertEqual(len(split["test"]["auto"]), 4)
        self.assertEqual(len(split["valid"]["manual"]), 4)
        self.assertEqual(len(split["valid"]["auto"]), 4)
        self.assertEqual(len(split["train"]["manual"]), 12)
        self.assertEqual(len(split["train"]["auto"]), 237)

    def test_no_publication_appears_twice(self):
        from src.m3.split import assign_partitions, partition_map

        split = assign_partitions(self._ids(20), self._ids(245, "2509."), members=4)
        mapping = partition_map(split)
        self.assertEqual(len(mapping), 20 + 245)

    def test_split_is_reproducible_and_seed_dependent(self):
        from src.m3.split import assign_partitions

        manual, auto = self._ids(20), self._ids(245, "2509.")
        first = assign_partitions(manual, auto, members=4, seed=2026)
        again = assign_partitions(manual, auto, members=4, seed=2026)
        other = assign_partitions(manual, auto, members=4, seed=7)
        self.assertEqual(first["test"], again["test"])
        self.assertNotEqual(first["test"], other["test"])

    def test_a_pool_too_small_for_the_quota_is_refused(self):
        from src.m3.split import assign_partitions

        with self.assertRaises(ValueError):
            assign_partitions(self._ids(5), self._ids(245, "2509."), members=4)
        with self.assertRaises(ValueError):
            assign_partitions(self._ids(20), self._ids(3, "2509."), members=4)

    def test_validation_accepts_a_correct_split_and_flags_an_overlap(self):
        from src.m3.split import assign_partitions, validate_split

        split = assign_partitions(self._ids(20), self._ids(245, "2509."), members=4)
        report = validate_split(split, members=4)
        self.assertTrue(report["ok"], report["problems"])

        split["train"]["manual"].append(split["test"]["manual"][0])
        report = validate_split(split, members=4)
        self.assertFalse(report["ok"])
        self.assertTrue(any("more than one partition" in problem for problem in report["problems"]))

    def test_validation_checks_label_counts_of_validation_and_test(self):
        from src.m3.split import assign_partitions, validate_split

        split = assign_partitions(self._ids(20), self._ids(245, "2509."), members=4)
        labels = [dict(arxiv_id=arxiv_id, citation_key=f"k{index}",
                       groundtruth_arxiv_id=None if index % 2 else "2507.00001")
                  for arxiv_id in split["train"]["manual"] + split["valid"]["manual"]
                  for index in range(20)]
        report = validate_split(split, members=4, labels=labels)
        self.assertFalse(report["ok"])
        things = " ".join(report["problems"])
        self.assertIn("no labels at all", things)          # the test publications are unlabelled
        self.assertEqual(report["labels"]["per_partition"]["test"], 0)

        labels += [dict(arxiv_id=arxiv_id, citation_key=f"k{index}", groundtruth_arxiv_id=None)
                   for arxiv_id in split["test"]["manual"] for index in range(20)]
        report = validate_split(split, members=4, labels=labels)
        self.assertTrue(report["ok"], report["problems"])
        self.assertEqual(report["labels"]["per_partition"]["valid"], 80)

    def test_annotate_adds_the_partition_and_reports_strays(self):
        from src.m3.split import annotate, assign_partitions

        split = assign_partitions(self._ids(20), self._ids(245, "2509."), members=4)
        rows = [dict(arxiv_id=split["test"]["manual"][0], citation_key="a"),
                dict(arxiv_id="9999.99999", citation_key="b")]
        annotated, missing = annotate(rows, split)
        self.assertEqual(annotated[0]["partition"], "test")
        self.assertEqual(missing, ["9999.99999"])
