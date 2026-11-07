"""Bounded S2 retry and transport metrics: fake HTTP/clocks, TEMP corpus only."""
import copy
from email.utils import formatdate
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from src import http_metrics as metrics, semantic_scholar as s2, batch, benchmark_runner
import test_semantic_scholar as fx
import test_s2_integration as integration
from test_semantic_scholar_references import page, reference


class RetryMetricsTests(unittest.TestCase):
    setUp = fx.SemanticScholarTests.setUp
    sleep = fx.SemanticScholarTests.sleep
    open_response = fx.SemanticScholarTests.open_response
    fetch = fx.SemanticScholarTests.fetch
    success = fx.SemanticScholarTests.success

    def measured(self, **options):
        collector = metrics.HttpMetrics()
        with metrics.collect(collector):
            result = self.fetch(**options)
        return result, collector.snapshot(complete=True)

    def test_retry_after_seconds_success_counts_actual_attempts(self):
        self.outcomes = [fx.http_error(429, 7), self.success(fx.identifiers(1))]
        result, data = self.measured(max_attempts=3)
        self.assertEqual(result[0]["status"], "found")
        self.assertEqual(self.starts, [0, 7])
        self.assertEqual(data["total"]["attempts"], 2)
        self.assertEqual(data["total"]["retry_attempts"], 1)
        self.assertEqual(data["total"]["status_counts"], {"429": 1, "200": 1})
        self.assertEqual(data["total"]["http_errors"], 1)
        self.assertEqual(data["total"]["wait_seconds"], 7)
        metrics.validate(data)

    def test_retry_after_http_date(self):
        self.outcomes = [fx.http_error(429, formatdate(1700000009, usegmt=True)),
                         self.success(fx.identifiers(1))]
        self.measured(max_attempts=3)
        self.assertEqual(self.starts, [0, 9])

    def test_missing_header_exponential_backoff(self):
        self.outcomes = [fx.http_error(429), fx.http_error(503), self.success(fx.identifiers(1))]
        _, data = self.measured(max_attempts=3, backoff_base=5, backoff_cap=30)
        self.assertEqual(self.starts, [0, 5, 15])
        self.assertEqual(data["total"]["retry_attempts"], 2)
        self.assertEqual(data["total"]["wait_seconds"], 15)
        self.assertEqual(data["total"]["status_counts"], {"429": 1, "503": 1, "200": 1})

    def test_invalid_header_falls_back_and_jitter_is_capped(self):
        self.outcomes = [fx.http_error(429, "bad-header"), fx.http_error(429),
                         self.success(fx.identifiers(1))]
        with patch.object(s2.random, "uniform", return_value=0.75):
            self.measured(max_attempts=3, backoff_base=5, backoff_cap=6)
        self.assertEqual(self.starts, [0, 5.75, 11.75])

    def test_retry_after_exceeding_budget_never_retries_early(self):
        self.outcomes = [fx.http_error(429, 100)]
        result, data = self.measured(max_attempts=3, max_retry_wait=10)
        self.assertEqual((result[0]["status"], result[0]["http_status"], result[0]["attempts"]),
                         ("rate_limited", 429, 1))
        self.assertEqual(result[0]["error"], "retry_wait_budget_exceeded")
        self.assertEqual(self.starts, [0])
        self.assertEqual(data["total"]["wait_seconds"], 0)
        self.assertEqual(self.limiter._next_request_at, 100)

    def test_overflowing_numeric_retry_after_never_falls_back_to_early_retry(self):
        self.outcomes = [fx.http_error(429, "9" * 500)]
        result, data = self.measured(max_attempts=3, max_retry_wait=10)
        self.assertEqual(result[0]["http_status"], 429)
        self.assertEqual(result[0]["attempts"], 1)
        self.assertEqual(self.starts, [0])
        self.assertEqual(data["total"]["retry_attempts"], 0)

    def test_cumulative_wait_budget_stops_after_second_429(self):
        self.outcomes = [fx.http_error(429), fx.http_error(429)]
        result, data = self.measured(max_attempts=3, backoff_base=5, max_retry_wait=10)
        self.assertEqual(result[0]["attempts"], 2)
        self.assertEqual(self.starts, [0, 5])
        self.assertEqual(data["total"]["wait_seconds"], 5)

    def test_pacing_also_counts_toward_wait_budget(self):
        limiter = s2.RateLimiter(min_interval=2)
        self.limiter = limiter
        with patch.object(s2, "DEFAULT_LIMITER", limiter):
            self.outcomes = [fx.http_error(429)]
            result, data = self.measured(max_attempts=3, max_retry_wait=1)
        self.assertEqual(result[0]["attempts"], 1)
        self.assertEqual(self.starts, [0])
        self.assertEqual(data["total"]["retry_attempts"], 0)

    def test_reused_limiter_cooldown_refuses_without_new_http(self):
        self.outcomes = [fx.http_error(429, 100)]
        self.fetch(max_attempts=1)
        result, data = self.measured(max_attempts=3, max_retry_wait=10)
        self.assertEqual(result[0]["attempts"], 0)
        self.assertEqual(result[0]["http_status"], 429)
        self.assertEqual(result[0]["error"], "cooldown_wait_budget_exceeded")
        integration.adapter.metadata_result(result[0], fx.identifiers(1)[0])
        self.assertEqual(self.starts, [0])
        self.assertEqual(data["total"]["attempts"], 0)

    def test_max_attempts_one_and_403_stop_chunks(self):
        self.outcomes = [fx.http_error(403)]
        collector = metrics.HttpMetrics()
        with metrics.collect(collector):
            result = self.fetch(fx.identifiers(2), batch_size=1, max_attempts=3)
        self.assertEqual([item["attempts"] for item in result], [1, 0])
        self.assertEqual([item["status"] for item in result], ["http_error", "http_error"])
        self.assertEqual(self.starts, [0])
        self.assertEqual(collector.snapshot(complete=True)["total"]["status_counts"], {"403": 1})

    def test_references_partial_survives_over_budget_429(self):
        self.outcomes = [page([reference()], next=1), fx.http_error(429, 100)]
        collector = metrics.HttpMetrics()
        with metrics.collect(collector):
            value = s2.fetch_references("2502.09992", timeout=15, limit=1,
                                       max_attempts=3, max_retry_wait=10)
        self.assertEqual(value["status"], "partial")
        self.assertEqual(len(value["items"]), 1)
        self.assertFalse(value["coverage"]["complete"])
        self.assertEqual(value["coverage"]["continuation"], {"offset": 1})
        self.assertEqual(value["error"]["http_status"], 429)
        self.assertEqual(collector.snapshot(complete=True)["total"]["attempts"], 2)

    def test_invalid_policy_rejected_before_http(self):
        for options in ({"max_retry_wait": 0}, {"max_retry_wait": float("inf")},
                        {"max_retry_wait": True}, {"backoff_base": -1},
                        {"backoff_cap": "secret"}, {"backoff_base": 5, "backoff_cap": 2}):
            with self.subTest(options=list(options)), self.assertRaises(ValueError):
                self.fetch(**options)
        self.transport.open.assert_not_called()

    def test_http_200_bad_json_is_not_an_http_error(self):
        self.outcomes = [fx.Response(body=b"{invalid")]
        result, value = self.measured()
        self.assertEqual(result[0]["status"], "invalid_response")
        self.assertEqual(value["total"]["status_counts"], {"200": 1})
        self.assertEqual(value["total"]["http_errors"], 0)

    def test_interrupted_transport_produces_partial_metrics(self):
        self.outcomes = [KeyboardInterrupt()]
        collector = metrics.HttpMetrics()
        with metrics.collect(collector), self.assertRaises(KeyboardInterrupt):
            self.fetch()
        value = collector.snapshot(complete=True)
        self.assertEqual(value["status"], "partial")
        self.assertEqual(value["total"]["attempts"], 1)
        self.assertEqual(value["total"]["completed_attempts"], 0)

    def test_secrets_never_enter_metrics_or_retry_errors(self):
        secret = "offline-metrics-secret-key"
        self.outcomes = [fx.http_error(429, 100, message=secret)]
        with patch.dict(os.environ, {"SEMANTIC_SCHOLAR_API_KEY": secret}):
            result, value = self.measured(max_attempts=3, max_retry_wait=10)
        self.assertNotIn(secret, json.dumps([result, value]))


class MetricsSummaryTests(unittest.TestCase):
    def attempt(self, identity, *, status="finished"):
        collector = metrics.HttpMetrics()
        with metrics.collect(collector):
            with metrics.request("semantic_scholar", 1) as observe:
                observe(200)
        return dict(attempt_id=identity, status=status, http_metrics=collector.snapshot(complete=True))

    def test_two_distinct_attempts_summed_once(self):
        result = metrics.summarize([self.attempt("one"), self.attempt("two")])
        self.assertEqual(result["http_requests"], 2)
        self.assertEqual(result["http_errors"], 0)

    def test_duplicate_attempt_id_is_not_counted_twice(self):
        result = metrics.summarize([self.attempt("same"), self.attempt("same")])
        self.assertEqual(result["http_metrics"]["known"]["attempts"], 1)
        self.assertEqual(result["http_metrics_status"], "invalid")
        self.assertIsNone(result["http_requests"])

    def test_legacy_mixed_history_is_partial_not_reconstructed(self):
        result = metrics.summarize([dict(attempt_id="old", status="finished"), self.attempt("new")])
        self.assertIsNone(result["http_requests"])
        self.assertEqual(result["http_metrics_status"], "partial")
        self.assertEqual(result["http_metrics"]["known"]["attempts"], 1)
        self.assertEqual(result["http_metrics"]["missing_attempt_ids"], ["old"])

    def test_all_legacy_history_remains_not_collected(self):
        result = metrics.summarize([dict(attempt_id="old", status="finished")])
        self.assertEqual(result["http_metrics_status"], "not_collected")
        self.assertIsNone(result["http_errors"])

    def test_corrupt_metrics_are_reported_without_echoing_payload(self):
        value = self.attempt("bad")
        value["http_metrics"]["providers"] = {"secret-value": "never-print"}
        result = metrics.summarize([value])
        self.assertEqual(result["http_metrics_status"], "invalid")
        self.assertIsNone(result["http_requests"])
        self.assertNotIn("secret-value", json.dumps(result))

    def test_running_or_unflushed_attempt_is_not_complete(self):
        result = metrics.summarize([self.attempt("running", status="running")])
        self.assertEqual(result["http_metrics_status"], "partial")
        self.assertIsNone(result["http_requests"])

    def test_invalid_counts_and_waits_rejected(self):
        for mutate in (lambda v: v["total"].update(attempts=100),
                       lambda v: v["total"].update(wait_seconds=float("nan")),
                       lambda v: v["providers"]["arxiv"].update(attempts=True),
                       lambda v: v["total"]["status_counts"].update({"429": "secret"})):
            value = self.attempt("x")["http_metrics"]
            mutate(value)
            with self.assertRaises(ValueError):
                metrics.validate(value)


class BatchRetryMetricsTests(unittest.TestCase):
    setUp = integration.S2IntegrationTests.setUp
    sleep = integration.S2IntegrationTests.sleep
    open_response = integration.S2IntegrationTests.open_response
    s2_open = integration.S2IntegrationTests.s2_open
    close_remaining = integration.S2IntegrationTests.close_remaining
    store = integration.S2IntegrationTests.store
    paper_path = integration.S2IntegrationTests.paper_path
    snapshot = integration.S2IntegrationTests.snapshot
    arxiv_success = integration.S2IntegrationTests.arxiv_success
    s2_success = integration.S2IntegrationTests.s2_success
    config = integration.S2IntegrationTests.config
    settings = integration.S2IntegrationTests.settings
    run_batch = integration.S2IntegrationTests.run_batch
    aggregate = integration.S2IntegrationTests.aggregate

    def retry_config(self, **changes):
        result = self.config()
        result.update(max_retry_wait=60, backoff_base=5, backoff_cap=30)
        result.update(changes)
        return result

    def test_all_papers_metadata_and_references_share_limiter(self):
        pairs = [integration.PAIR, integration.OTHER]
        self.arxiv_success(pairs)
        self.s2_success(pairs)
        result = self.run_batch(pairs)
        self.assertEqual(len(self.limiters), 1)
        self.assertTrue(all(b - a >= 2 for a, b in zip(self.s2_starts, self.s2_starts[1:])))
        self.assertEqual(result["http_metrics"]["providers"]["arxiv"]["attempts"], len(self.requests))
        self.assertEqual(result["http_metrics"]["providers"]["semantic_scholar"]["attempts"], len(self.s2_requests))
        self.assertEqual(result["stats"]["http_requests"], 8)

    def test_429_then_success_counts_retry_and_wait_inside_batch(self):
        self.arxiv_success()
        self.s2_outcomes = [fx.http_error(429), fx.Response([fx.paper(integration.PAIR[1])]), page([])]
        result = self.run_batch(s2_config=self.retry_config(max_attempts=3))
        self.assertFalse(result["stopped"])
        measured = result["http_metrics"]["providers"]["semantic_scholar"]
        self.assertEqual((measured["attempts"], measured["retry_attempts"], measured["http_errors"]), (3, 1, 1))
        self.assertEqual(measured["status_counts"], {"429": 1, "200": 2})
        self.assertEqual(measured["wait_seconds"], 7)  # Backoff 5, then normal pacing 2.
        self.assertEqual(self.aggregate()["history"]["http_requests"], 5)

    def test_exhausted_429_checkpoints_and_does_not_start_next_paper(self):
        self.arxiv_success()
        self.s2_outcomes = [fx.http_error(429) for _ in range(3)]
        result = self.run_batch([integration.PAIR, integration.OTHER],
                                s2_config=self.retry_config(max_attempts=3))
        self.assertTrue(result["stopped"])
        self.assertEqual(result["stop_reason"]["http_status"], 429)
        self.assertTrue(result["stop_reason"]["checkpoint_saved"])
        self.assertEqual(len(self.s2_requests), 3)
        self.assertEqual(result["papers"][1]["state"], "pending")
        self.assertEqual(self.store().read_references(*integration.PAIR)["status"], "not_attempted")
        self.assertTrue(self.store().should_skip_version(*integration.PAIR, "v1"))

    def test_403_never_retries_and_stops_batch(self):
        self.arxiv_success()
        self.s2_outcomes = [fx.http_error(403)]
        result = self.run_batch([integration.PAIR, integration.OTHER],
                                s2_config=self.retry_config(max_attempts=3))
        self.assertTrue(result["stopped"])
        self.assertEqual(len(self.s2_requests), 1)
        self.assertEqual(result["stats"]["http_errors"], 1)

    def test_partial_references_preserved_after_repeated_429(self):
        self.arxiv_success()
        self.s2_outcomes = [fx.Response([fx.paper(integration.PAIR[1])]), page([reference()], next=1),
                            *[fx.http_error(429) for _ in range(3)]]
        result = self.run_batch(s2_config=self.retry_config(max_attempts=3, reference_limit=1))
        self.assertTrue(result["stopped"])
        refs = self.store().read_references(*integration.PAIR)
        self.assertEqual(refs["status"], "partial")
        self.assertEqual(len(refs["items"]), 1)
        self.assertFalse(refs["coverage"]["complete"])

    def test_resume_counts_zero_new_http_and_unique_source(self):
        self.arxiv_success()
        self.s2_success()
        first = self.run_batch()
        before = self.snapshot()
        second = self.run_batch(resume=True)
        self.assertEqual(second["stats"]["http_requests"], 0)
        self.assertEqual(second["stats"]["http_errors"], 0)
        history = self.aggregate()["history"]
        self.assertEqual(history["http_requests"], first["stats"]["http_requests"])
        self.assertEqual(history["http_metrics_status"], "collected")
        for name in ("metadata.json", "references.json", "v1/main.tex"):
            self.assertEqual(self.snapshot()[name], before[name])

    def test_new_batch_policy_reuses_good_corpus_without_altering_old_logs(self):
        self.arxiv_success()
        self.s2_success()
        self.run_batch()
        before = self.snapshot()
        old_logs = {p:p.read_bytes() for p in (Path(self.temp.name)/"logs"/"s2-offline").rglob("*.json")}
        with self.assertRaises(batch.BatchError):
            self.run_batch(resume=True, s2_config=self.retry_config(max_attempts=3))
        result = self.run_batch(batch_id="new-policy", s2_config=self.retry_config(max_attempts=3))
        self.assertEqual(result["stats"]["http_requests"], 0)
        self.assertEqual({p:p.read_bytes() for p in old_logs}, old_logs)
        for name in ("metadata.json", "references.json", "v1/main.tex"):
            self.assertEqual(self.snapshot()[name], before[name])
        report = benchmark_runner.aggregate_batch(self.root, batch_id="new-policy",
                                                  log_root=Path(self.temp.name)/"logs")
        self.assertEqual(report["issues"], [])

    def test_legacy_attempt_stays_unknown_after_new_cached_resume(self):
        self.arxiv_success()
        self.s2_success()
        first = self.run_batch()
        path = Path(self.temp.name)/"logs"/"s2-offline"/"attempts"/(first["attempt_id"]+".json")
        value = json.loads(path.read_text(encoding="utf-8"))
        del value["http_metrics"]
        path.write_text(json.dumps(value), encoding="utf-8")
        self.run_batch(resume=True)
        result = self.aggregate()["history"]
        self.assertIsNone(result["http_requests"])
        self.assertEqual(result["http_metrics_status"], "partial")
        self.assertEqual(result["http_metrics"]["known"]["attempts"], 0)
        self.assertEqual(result["http_metrics"]["missing_attempt_ids"], [first["attempt_id"]])


if __name__ == "__main__":
    unittest.main()
