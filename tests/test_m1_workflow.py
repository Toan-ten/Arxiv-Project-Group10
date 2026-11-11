"""Tests for compatibility entry points around the separated M1 phases."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from src import m1_phases, m1_workflow
from src.fast_production import member_cohort_range
from src.id_ranges import load_config


class TestMemberScope(unittest.TestCase):
    def test_each_member_keeps_its_assigned_fifty_cohorts(self):
        self.assertEqual(len(load_config()), 4)
        for member in load_config():
            selection = m1_workflow.build_member_selection(member)
            self.assertEqual(selection["cohorts"], member_cohort_range(member))
            self.assertEqual(len(selection["pairs"]), 5000)
            self.assertEqual(selection["scope"], "member_full_assignment")

    def test_sample_is_a_prefix_of_the_same_member_assignment(self):
        full = m1_workflow.build_member_selection("24120260")
        pilot = m1_workflow.build_member_selection("24120260", 1)
        self.assertEqual(pilot["cohorts"], full["cohorts"][:1])
        self.assertEqual(pilot["pairs"], full["pairs"][:100])
        self.assertEqual(pilot["scope"], "member_subset_same_pipeline")

    def test_rejects_invalid_member_scope(self):
        for limit in (0, 51, True, 1.5):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                m1_workflow.build_member_selection("24120260", limit)


class TestSequentialCompatibilityWorkflow(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.arxiv = {
            "stage": "arxiv", "selection_fingerprint": "same-selection",
            "stopped": False, "stop_reason": None,
        }
        self.s2 = {"stage": "s2", "stopped": False, "stop_reason": None}
        self.audit = {"stage": "audit", "is_reconciled": True, "export": {"status": "exported"}}

        def call(name, result):
            def _run(*args, **kwargs):
                self.calls.append((name, args, kwargs))
                return result
            return _run

        self.patchers = [
            patch.object(m1_phases, "run_m1_arxiv_member", side_effect=call("arxiv", self.arxiv)),
            patch.object(m1_phases, "run_m1_s2_member", side_effect=call("s2", self.s2)),
            patch.object(m1_phases, "run_m1_audit_member", side_effect=call("audit", self.audit)),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()

    def run_compat(self):
        return m1_workflow.run_m1_member(
            "24120260", drive_root="/tmp/drive", local_root="/tmp/local",
            kaggle_token="unused", semantic_scholar_api_key="s2-key",
            cohort_limit=1, budget_seconds=90,
        )

    def test_runs_arxiv_then_s2_then_audit_with_shared_scope(self):
        result = self.run_compat()
        self.assertEqual([call[0] for call in self.calls], ["arxiv", "s2", "audit"])
        for _name, args, kwargs in self.calls:
            self.assertEqual(args, ("24120260",))
            self.assertEqual(kwargs["drive_root"], "/tmp/drive")
            self.assertEqual(kwargs["local_root"], "/tmp/local")
            self.assertEqual(kwargs["cohort_limit"], 1)
            self.assertGreater(kwargs["budget_seconds"], 0)
        self.assertEqual(self.calls[1][2]["semantic_scholar_api_key"], "s2-key")
        self.assertEqual(result["selection_fingerprint"], "same-selection")
        self.assertEqual(result["handoff_status"], "verified")
        self.assertEqual(result["export"], {"status": "exported"})

    def test_arxiv_stop_does_not_call_s2_or_audit(self):
        self.arxiv.update(stopped=True, stop_reason={"type": "interrupted"})
        result = self.run_compat()
        self.assertEqual([call[0] for call in self.calls], ["arxiv"])
        self.assertEqual(result["stage"], "arxiv")
        self.assertIsNone(result["s2"])
        self.assertIsNone(result["audit"])

    def test_s2_stop_skips_audit_and_keeps_checkpoint_reason(self):
        self.s2.update(stopped=True, stop_reason={"type": "http_policy", "http_status": 429})
        result = self.run_compat()
        self.assertEqual([call[0] for call in self.calls], ["arxiv", "s2"])
        self.assertEqual(result["stage"], "s2")
        self.assertEqual(result["s2"]["stop_reason"]["http_status"], 429)
        self.assertIsNone(result["audit"])


if __name__ == "__main__":
    unittest.main()
