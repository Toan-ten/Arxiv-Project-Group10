"""Unit and regression tests for evidence-based version outcome classification.

Tests Phase 4 requirements:
- Distinguish withdrawn, no-TeX, 404-unverified, version-not-found, 429, and 5xx.
- Regression test for 2501.02724v2 using official arXiv evidence fixture.
- Prove that 404 alone never produces a false withdrawal classification.
"""

from __future__ import annotations

import json
from pathlib import Path
import unittest

from src.withdrawal_classifier import (
    classify_version_outcome,
    is_retryable_source_status,
    is_terminal_source_status,
)


class TestWithdrawalClassifier(unittest.TestCase):
    def setUp(self):
        self.fixtures_dir = Path(__file__).resolve().parent / "fixtures"
        self.fixture_2501_02724 = self.fixtures_dir / "arxiv_2501_02724_evidence.json"

    def test_classify_200_with_tex_returns_success(self):
        """HTTP 200 with TeX files returns terminal success."""
        res = classify_version_outcome(
            "2501.02642",
            "v1",
            http_status=200,
            content_type="application/x-eprint-tar",
            has_tex_files=True,
            file_list=["main.tex", "refs.bib"],
        )
        self.assertEqual(res["classification"], "success")
        self.assertTrue(res["is_terminal"])
        self.assertTrue(is_terminal_source_status("success"))
        self.assertFalse(is_retryable_source_status("success"))

    def test_classify_200_pdf_only_returns_no_tex_source_never_withdrawn(self):
        """Rule 1: HTTP 200 with application/pdf and no TeX archive is no_tex_source, NOT withdrawn."""
        res = classify_version_outcome(
            "2501.02642",
            "v1",
            http_status=200,
            content_type="application/pdf",
            has_tex_files=False,
            file_list=[],
        )
        self.assertEqual(res["classification"], "no_tex_source")
        self.assertTrue(res["is_terminal"])
        self.assertNotEqual(res["classification"], "withdrawn_version")

    def test_classify_429_returns_rate_limited_non_terminal(self):
        """Rule 3: HTTP 429 is rate_limited, non-terminal, and retryable."""
        res = classify_version_outcome(
            "2501.02642",
            "v1",
            http_status=429,
            error_message="HTTP 429 Too Many Requests",
        )
        self.assertEqual(res["classification"], "rate_limited")
        self.assertFalse(res["is_terminal"])
        self.assertTrue(is_retryable_source_status("rate_limited"))
        self.assertNotEqual(res["classification"], "no_tex_source")
        self.assertNotEqual(res["classification"], "withdrawn_version")

    def test_classify_5xx_and_timeout_returns_transient_error(self):
        """Rule 4: HTTP 503 or socket timeout is transient_error, non-terminal, and retryable."""
        res_503 = classify_version_outcome("2501.02642", "v1", http_status=503)
        self.assertEqual(res_503["classification"], "transient_error")
        self.assertFalse(res_503["is_terminal"])
        self.assertTrue(is_retryable_source_status("transient_error"))

        res_timeout = classify_version_outcome(
            "2501.02642", "v1", error_message="Connection timed out during read"
        )
        self.assertEqual(res_timeout["classification"], "transient_error")
        self.assertFalse(res_timeout["is_terminal"])

    def test_classify_404_alone_is_insufficient_for_withdrawal(self):
        """Rule 2: A 404 alone without metadata verification must remain http_error_404_unverified."""
        res = classify_version_outcome(
            "2501.02642",
            "v1",
            http_status=404,
            discovery_metadata=None,  # No metadata available
        )
        self.assertEqual(res["classification"], "http_error_404_unverified")
        self.assertFalse(res["is_terminal"], "Unverified 404 must not pass as terminal completion")
        self.assertEqual(res["evidence_source"], "http_response")

    def test_classify_404_unverified_when_metadata_has_no_withdrawal_evidence(self):
        """Rule 2: 404 with active paper metadata that does NOT mention withdrawal remains unverified."""
        meta = {
            "title": "A perfectly active paper",
            "comments": "12 pages, 4 figures",
            "version_ids": ["2501.02642v1"],
            "latest_version_id": "2501.02642v1",
        }
        res = classify_version_outcome(
            "2501.02642",
            "v1",
            http_status=404,
            discovery_metadata=meta,
        )
        self.assertEqual(res["classification"], "http_error_404_unverified")
        self.assertFalse(res["is_terminal"])

    def test_classify_404_version_not_found_when_outside_metadata_inventory(self):
        """Rule 2: 404 when metadata confirms the paper only has v1 (querying v2) is version_not_found."""
        meta = {
            "title": "Single revision paper",
            "version_ids": ["2501.02642v1"],
            "latest_version_id": "2501.02642v1",
        }
        res = classify_version_outcome(
            "2501.02642",
            "v2",
            http_status=404,
            discovery_metadata=meta,
        )
        self.assertEqual(res["classification"], "version_not_found")
        self.assertTrue(res["is_terminal"])
        self.assertIn("does not exist", res["reason"])

    def test_regression_2501_02724v2_official_withdrawal_fixture(self):
        """Phase 4 Regression: 2501.02724v2 confirmed withdrawn via official arXiv evidence fixture."""
        self.assertTrue(self.fixture_2501_02724.exists(), "Official evidence fixture must exist")
        with open(self.fixture_2501_02724, "r", encoding="utf-8") as f:
            evidence_data = json.load(f)

        self.assertEqual(evidence_data["arxiv_id"], "2501.02724")
        self.assertEqual(evidence_data["versioned_id"], "2501.02724v2")
        self.assertTrue(evidence_data["is_withdrawn"])

        res = classify_version_outcome(
            evidence_data["arxiv_id"],
            "v2",
            http_status=evidence_data["source_http_status"],  # 404
            discovery_metadata=evidence_data,
        )
        self.assertEqual(res["classification"], "withdrawn_version")
        self.assertTrue(res["is_terminal"])
        self.assertEqual(res["evidence_source"], "arxiv_metadata")
        self.assertEqual(res["original_http_status"], 404)
        self.assertIn("insufficient", res["reason"].lower())

    def test_withdrawn_version_does_not_invalidate_earlier_versions(self):
        """Rule 5: Withdrawn v2 does not invalidate v1."""
        v1_res = classify_version_outcome(
            "2501.02724",
            "v1",
            http_status=200,
            has_tex_files=True,
            file_list=["main.tex"],
        )
        self.assertEqual(v1_res["classification"], "success")
        self.assertTrue(v1_res["is_terminal"])


if __name__ == "__main__":
    unittest.main()
