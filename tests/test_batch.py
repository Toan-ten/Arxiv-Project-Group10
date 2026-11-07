"""Batch tests use the real Pipeline/Storage and fake HTTP, only in TEMP."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.error import URLError

import test_pipeline as fixtures
from src import batch, storage
from src.id_ranges import load_config
from src.pilots import build_smoke_set


PAIR, OTHER, THIRD = fixtures.PAIR, fixtures.OTHER, fixtures.THIRD
Response, TEX = fixtures.Response, fixtures.TEX
xml_response, http_error = fixtures.xml_response, fixtures.http_error


class BatchTests(unittest.TestCase):
    setUp = fixtures.PipelineTests.setUp
    sleep = fixtures.PipelineTests.sleep
    open_response = fixtures.PipelineTests.open_response
    store = fixtures.PipelineTests.store
    manifest = fixtures.PipelineTests.manifest
    paper_path = fixtures.PipelineTests.paper_path
    snapshot = fixtures.PipelineTests.snapshot

    def run_batch(self, pairs=None, **options):
        settings = dict(
            batch_id="offline-batch", max_ids=3,
            log_root=Path(self.temp.name) / "logs", versions=("v1",),
            timeout=15, max_attempts=1,
        )
        settings.update(options)
        return batch.run_batch(
            [PAIR, OTHER, THIRD] if pairs is None else pairs, self.root, **settings,
        )

    def successes(self, pairs=(PAIR, OTHER, THIRD), versions=1):
        self.outcomes = [
            response for pair in pairs
            for response in (xml_response(pair[1], versions=versions), Response(TEX))
        ]

    def logs(self):
        root = Path(self.temp.name) / "logs" / "offline-batch"
        return {p.relative_to(root).as_posix(): p.read_bytes()
                for p in root.rglob("*.json")}

    def attempts(self):
        directory = Path(self.temp.name) / "logs" / "offline-batch" / "attempts"
        return sorted(
            [json.loads(p.read_text(encoding="utf-8")) for p in directory.glob("*.json")],
            key=lambda value: value["attempt_number"],
        )

    def test_three_papers_success_ownership_checksums_and_precise_stats(self):
        self.successes(versions=3)
        result = self.run_batch()
        self.assertEqual(result["status"], "finished")
        self.assertEqual([r["arxiv_id"] for r in result["papers"]], [p[1] for p in (PAIR, OTHER, THIRD)])
        self.assertEqual(result["stats"]["processed_papers"], 3)
        self.assertEqual(result["stats"]["discovery_actions"], {"queried": 3})
        self.assertEqual(result["stats"]["source_actions"], {"downloaded": 3, "pending": 6})
        self.assertEqual(result["stats"]["http_requests"], 6)
        self.assertEqual(result["stats"]["http_metrics_status"], "collected")
        self.assertEqual(self.opener.open.call_count, 6)
        self.assertEqual(self.starts, [0, 3, 6, 9, 12, 15])
        self.assertGreaterEqual(result["wall_seconds"], 0)
        self.assertIsNotNone(result["ended_at"])
        for pair, row in zip((PAIR, OTHER, THIRD), result["papers"]):
            self.assertEqual([row["mssv"], row["arxiv_id"]], list(pair))
            self.assertEqual(row["state"], "processed")
            self.assertGreaterEqual(row["wall_seconds"], 0)
            manifest = self.manifest(pair)
            self.assertEqual(manifest["contract_version"], 2)
            self.assertEqual(manifest["run_id"], result["run_id"])
            self.assertEqual(manifest["references_status"], "not_attempted")
            self.assertNotEqual(manifest["status"], "complete")
            self.assertEqual(manifest["versions"]["v1"]["status"], "success")
            self.assertIn("main.tex", manifest["versions"]["v1"]["file_checksums"])
            self.assertEqual(manifest["versions"]["v2"]["status"], "pending")
            self.assertFalse((self.paper_path(pair) / "v2").exists())
        self.assertEqual(self.attempts(), [result])

    def test_limit_uses_first_n_and_never_adds_replacements(self):
        self.outcomes = [Response(b'<feed xmlns="http://www.w3.org/2005/Atom" xmlns:o="http://a9.com/-/spec/opensearch/1.1/"><o:totalResults>0</o:totalResults></feed>', "application/atom+xml")]
        result = self.run_batch(max_ids=1)
        self.assertEqual(len(result["papers"]), 1)
        self.assertEqual(result["papers"][0]["result"]["paper_status"], "not_found")
        self.assertEqual(self.opener.open.call_count, 1)
        self.assertFalse(self.paper_path(OTHER).exists())
        definition = json.loads(next((Path(self.temp.name) / "logs").rglob("batch.json")).read_text())["definition"]
        self.assertEqual(definition["selection"], [list(PAIR)])
        self.assertEqual(len(definition["input_pairs"]), 3)

    def test_pilot_adapter_preserves_order_and_delegates_validation(self):
        entries = build_smoke_set(list(load_config().values()))
        pairs = batch.pilot_pairs(entries)
        self.assertEqual(pairs, [(e.mssv, e.arxiv_id) for e in entries])
        self.successes(pairs[:2])
        result = self.run_batch(pairs, max_ids=2)
        self.assertEqual([(r["mssv"], r["arxiv_id"]) for r in result["papers"]], pairs[:2])
        self.assertEqual(self.opener.open.call_count, 4)
        for invalid in ([{}], "not entries"):
            with self.assertRaises(batch.BatchError):
                batch.pilot_pairs(invalid)

    def test_invalid_inputs_including_beyond_limit_fail_before_io(self):
        invalid = [
            [PAIR, PAIR], [PAIR, ("unknown", OTHER[1])],
            [PAIR, (OTHER[0], PAIR[1])], [PAIR, (PAIR[0], "../escape")],
            [PAIR, (PAIR[0], "2502.09992v1")], [("24120232", "2502.00001")],
            [PAIR, "invalid"], iter([PAIR]),
        ]
        for pairs in invalid:
            with self.subTest(pairs=pairs):
                with self.assertRaises(ValueError):
                    self.run_batch(pairs, max_ids=1)
        self.opener.open.assert_not_called()
        self.assertFalse(self.root.exists())
        self.assertFalse((Path(self.temp.name) / "logs").exists())

    def test_invalid_options_and_log_locations_fail_before_io(self):
        variants = [
            dict(max_ids=0), dict(max_ids=True), dict(max_ids=1.5),
            dict(batch_id="../x"), dict(batch_id="CON"), dict(resume="yes"),
            dict(versions=()), dict(max_attempts=0), dict(timeout=float("nan")),
            dict(log_root=self.root / "logs"), dict(log_root=self.root.parent),
        ]
        for options in variants:
            with self.subTest(options=options):
                with self.assertRaises(ValueError):
                    self.run_batch(**options)
        self.opener.open.assert_not_called()
        self.assertFalse(self.root.exists())

    def test_second_not_found_and_http_404_continue_third(self):
        for mode in ("not_found", "404"):
            with self.subTest(mode=mode):
                self.root = Path(self.temp.name) / mode
                self.outcomes = [
                    xml_response(), Response(TEX),
                    Response(b'<feed xmlns="http://www.w3.org/2005/Atom" xmlns:o="http://a9.com/-/spec/opensearch/1.1/"><o:totalResults>0</o:totalResults></feed>', "application/atom+xml")
                    if mode == "not_found" else http_error(404),
                    xml_response(THIRD[1]), Response(TEX),
                ]
                self.opener.open.reset_mock()
                result = self.run_batch(batch_id=mode)
                self.assertEqual(result["status"], "finished")
                self.assertEqual(len(result["papers"]), 3)
                if mode == "not_found":
                    self.assertEqual(result["papers"][1]["result"]["paper_status"], "not_found")
                self.assertEqual(result["papers"][2]["result"]["source"]["v1"]["status"], "success")
                self.assertEqual(self.opener.open.call_count, 5)

    def test_policy_stops_both_stages_after_second_paper_and_keeps_checkpoint(self):
        for stage in ("discovery", "source"):
            for code in (403, 429):
                with self.subTest(stage=stage, code=code):
                    self.root = Path(self.temp.name) / (stage + str(code))
                    self.opener.open.reset_mock()
                    self.outcomes = [xml_response(), Response(TEX)]
                    if stage == "source":
                        self.outcomes.append(xml_response(OTHER[1], versions=2))
                    self.outcomes += [http_error(code), xml_response(THIRD[1]), Response(TEX)]
                    result = self.run_batch(batch_id=stage + str(code), versions=None)
                    self.assertEqual(result["status"], "stopped")
                    self.assertEqual(result["stop_reason"]["http_status"], code)
                    self.assertTrue(result["stop_reason"]["checkpoint_saved"])
                    self.assertEqual(result["papers"][2]["state"], "pending")
                    self.assertEqual(result["papers"][1]["state"], "processed")
                    self.assertEqual(self.opener.open.call_count, 3 if stage == "discovery" else 4)
                    self.assertFalse(self.paper_path(THIRD).exists())
                    self.assertEqual(self.manifest()["versions"]["v1"]["status"], "success")
                    if stage == "source":
                        self.assertEqual(self.manifest(OTHER)["versions"]["v1"]["http_status"], code)
                        self.assertEqual(self.manifest(OTHER)["versions"]["v2"]["status"], "pending")

    def test_log_is_durable_before_next_http_operation(self):
        original = self.open_response
        observed = []
        def transport(request, timeout):
            attempt = self.attempts()[0]
            active = [row for row in attempt["papers"] if row["state"] == "in_progress"]
            self.assertEqual(len(active), 1)
            index = active[0]["index"]
            self.assertTrue(all(r["state"] == "processed" for r in attempt["papers"][:index]))
            observed.append(index)
            return original(request, timeout)
        self.opener.open.side_effect = transport
        self.successes()
        self.run_batch()
        self.assertEqual(observed, [0, 0, 1, 1, 2, 2])

    def test_keyboard_interrupt_preserves_history_and_resume_skips_good_source(self):
        self.outcomes = [xml_response(), Response(TEX), xml_response(OTHER[1]), KeyboardInterrupt()]
        with self.assertRaises(KeyboardInterrupt):
            self.run_batch()
        first = self.attempts()[0]
        self.assertEqual(first["status"], "interrupted")
        self.assertEqual([r["state"] for r in first["papers"]], ["processed", "interrupted", "pending"])
        before_logs = self.logs()
        good_bytes = (self.paper_path() / "v1" / "main.tex").read_bytes()
        checksum = self.manifest()["versions"]["v1"]["file_checksums"].copy()
        self.outcomes = [Response(TEX), xml_response(THIRD[1]), Response(TEX)]
        resumed = self.run_batch(resume=True)
        self.assertEqual(resumed["status"], "finished")
        self.assertNotEqual(resumed["run_id"], first["run_id"])
        self.assertEqual(resumed["attempt_number"], 2)
        self.assertEqual(resumed["papers"][0]["result"]["source"]["v1"]["action"], "skipped")
        self.assertEqual(resumed["papers"][1]["result"]["discovery"]["action"], "cached")
        self.assertEqual(self.opener.open.call_count, 7)
        self.assertEqual((self.paper_path() / "v1" / "main.tex").read_bytes(), good_bytes)
        self.assertEqual(self.manifest()["versions"]["v1"]["file_checksums"], checksum)
        for name, content in before_logs.items():
            self.assertEqual(self.logs()[name], content)

    def test_unfinished_attempt_is_detected_without_rewriting_old_log(self):
        self.successes()
        result = self.run_batch()
        path = next((Path(self.temp.name) / "logs").rglob(result["attempt_id"] + ".json"))
        incomplete = copy.deepcopy(result)
        incomplete.update(status="running", ended_at=None)
        incomplete["papers"][-1].update(state="in_progress", ended_at=None)
        path.write_text(json.dumps(incomplete), encoding="utf-8")
        before = path.read_bytes()
        self.opener.open.reset_mock()
        second = self.run_batch(resume=True)
        self.assertEqual(second["previous_incomplete_attempts"], [result["attempt_id"]])
        self.assertEqual(path.read_bytes(), before)
        self.opener.open.assert_not_called()

    def test_cached_attempts_are_not_reported_as_new_http_requests(self):
        self.successes()
        self.run_batch()
        self.opener.open.reset_mock()
        second = self.run_batch(resume=True)
        self.opener.open.assert_not_called()
        self.assertEqual(second["stats"]["discovery_actions"], {"cached": 3})
        self.assertEqual(second["stats"]["source_actions"], {"skipped": 3})
        self.assertEqual(second["stats"]["http_requests"], 0)
        self.assertEqual(second["stats"]["http_errors"], 0)
        self.assertEqual(self.attempts()[1], second)

    def test_storage_checksum_overrules_previous_success_log(self):
        self.successes()
        self.run_batch()
        source = self.paper_path() / "v1" / "main.tex"
        source.write_bytes(b"corrupt fixture")
        self.opener.open.reset_mock()
        result = self.run_batch(resume=True)
        self.opener.open.assert_not_called()
        self.assertIn("v1", result["papers"][0]["result"]["blocked"])
        self.assertEqual(source.read_bytes(), b"corrupt fixture")
        self.assertEqual(self.manifest()["versions"]["v1"]["status"], "validation_failed")

    def test_changed_selection_or_options_refused_without_writes(self):
        self.successes()
        self.run_batch()
        before = self.logs()
        changed = [
            dict(pairs=[OTHER, PAIR, THIRD]), dict(pairs=[PAIR, OTHER]),
            dict(max_ids=2), dict(versions=("v2",)), dict(timeout=16),
            dict(max_attempts=2), dict(user_agent="changed/1.0"),
        ]
        for settings in changed:
            with self.subTest(settings=settings):
                with self.assertRaisesRegex(batch.BatchError, "definition changed"):
                    self.run_batch(resume=True, **settings)
                self.assertEqual(self.logs(), before)
        self.opener.open.reset_mock()
        with self.assertRaisesRegex(batch.BatchError, "definition changed"):
            batch.run_batch(
                [PAIR, OTHER, THIRD], self.root.parent / "other-corpus",
                batch_id="offline-batch", max_ids=3, log_root=self.root.parent / "logs",
                resume=True, versions=("v1",), timeout=15, max_attempts=1,
            )
        self.opener.open.assert_not_called()

    def test_policy_restart_requires_explicit_resume_and_has_no_outer_retry(self):
        self.outcomes = [xml_response(), Response(TEX), http_error(429)]
        first = self.run_batch()
        self.assertEqual(first["status"], "stopped")
        count = self.opener.open.call_count
        with self.assertRaisesRegex(batch.BatchError, "resume=True"):
            self.run_batch()
        self.assertEqual(self.opener.open.call_count, count)
        self.successes((OTHER, THIRD))
        resumed = self.run_batch(resume=True)
        self.assertEqual(resumed["status"], "finished")
        self.assertEqual(self.opener.open.call_count, count + 4)

    def test_initial_log_failure_prevents_all_requests(self):
        with patch.object(batch, "_atomic_json", side_effect=OSError("offline disk failure")):
            with self.assertRaises(batch.BatchLogError):
                self.run_batch()
        self.opener.open.assert_not_called()
        self.assertFalse(self.root.exists())

    def test_progress_log_failure_stops_before_second_paper_and_preserves_snapshot(self):
        original = batch._atomic_json
        def fail_after_first(path, value):
            if value.get("papers") and value["papers"][0]["state"] == "processed":
                raise OSError("offline disk failure")
            return original(path, value)
        self.successes()
        with patch.object(batch, "_atomic_json", side_effect=fail_after_first):
            with self.assertRaises(batch.BatchLogError):
                self.run_batch()
        self.assertEqual(self.opener.open.call_count, 2)
        before = self.logs()
        first = self.attempts()[0]
        self.assertEqual(first["status"], "running")
        self.assertEqual(first["papers"][0]["state"], "in_progress")
        self.assertEqual(self.manifest()["versions"]["v1"]["status"], "success")
        self.assertFalse(self.paper_path(OTHER).exists())
        self.successes((OTHER, THIRD))
        resumed = self.run_batch(resume=True)
        self.assertEqual(resumed["status"], "finished")
        self.assertEqual(resumed["papers"][0]["result"]["source"]["v1"]["action"], "skipped")
        for name, content in before.items():
            self.assertEqual(self.logs()[name], content)

    def test_atomic_replace_failure_keeps_previous_log_bytes(self):
        path = Path(self.temp.name) / "log.json"
        batch._save(path, {"status": "running"})
        before = path.read_bytes()
        with patch.object(storage.os, "replace", side_effect=OSError("fixture")):
            with self.assertRaises(batch.BatchLogError):
                batch._save(path, {"status": "finished"})
        self.assertEqual(path.read_bytes(), before)

    def test_invalid_run_history_is_preserved_and_blocks_requests(self):
        self.successes()
        first = self.run_batch()
        path = next((Path(self.temp.name) / "logs").rglob(first["attempt_id"] + ".json"))
        for content in ('{broken', '{"status":"running","status":"finished"}'):
            path.write_text(content, encoding="utf-8")
            before = self.logs()
            self.opener.open.reset_mock()
            with self.assertRaises(batch.BatchError):
                self.run_batch(resume=True)
            self.opener.open.assert_not_called()
            self.assertEqual(self.logs(), before)

    def test_missing_or_corrupt_paper_checkpoint_is_blocked_without_overwrite(self):
        store = self.store()
        store.initialize_paper(*OTHER)
        path = self.paper_path(OTHER) / "manifest.json"
        path.write_bytes(b"{broken")
        self.successes((PAIR, THIRD))
        result = self.run_batch()
        self.assertEqual(result["status"], "finished")
        self.assertIn("paper", result["papers"][1]["result"]["blocked"])
        self.assertEqual(path.read_bytes(), b"{broken")
        self.assertEqual(self.opener.open.call_count, 4)

    def test_network_failure_is_logged_and_does_not_drop_third_paper(self):
        self.outcomes = [xml_response(), Response(TEX), URLError(TimeoutError("fixture")),
                         xml_response(THIRD[1]), Response(TEX)]
        result = self.run_batch()
        self.assertEqual(result["status"], "finished")
        self.assertEqual(result["papers"][1]["result"]["discovery"]["status"], "timeout")
        self.assertEqual(result["papers"][2]["state"], "processed")
        self.assertEqual(self.opener.open.call_count, 5)

    def test_fatal_pipeline_exception_is_durable_and_reraised(self):
        self.outcomes = [xml_response(), Response(TEX), RuntimeError("fixture")]
        with self.assertRaises(fixtures.PipelineStopped):
            self.run_batch()
        first = self.attempts()[0]
        self.assertEqual(first["status"], "failed")
        self.assertEqual(first["error"]["type"], "PipelineStopped")
        self.assertEqual([r["state"] for r in first["papers"]], ["processed", "failed", "pending"])
        self.assertFalse(self.paper_path(THIRD).exists())

    def test_no_tex_and_unresolved_results_remain_distinct(self):
        self.outcomes = [xml_response(), Response(b"%PDF-1.7\nfixture"),
                         xml_response(OTHER[1], versions=None),
                         xml_response(THIRD[1]), Response(TEX)]
        result = self.run_batch()
        self.assertEqual(result["status"], "finished")
        self.assertEqual(result["papers"][0]["result"]["source"]["v1"]["status"], "no_tex_source")
        self.assertEqual(result["papers"][1]["result"]["blocked"]["discovery"], "needs_verification")
        self.assertEqual(self.opener.open.call_count, 5)
        self.assertTrue(all(r["result"]["paper_status"] != "complete" for r in result["papers"]))

    def test_unchosen_input_tail_cannot_change_between_attempts(self):
        self.successes((PAIR,))
        self.run_batch(max_ids=1)
        before = self.logs()
        self.opener.open.reset_mock()
        with self.assertRaisesRegex(batch.BatchError, "definition changed"):
            self.run_batch([PAIR, OTHER, ("24120232", "2502.08236")],
                           max_ids=1, resume=True)
        self.opener.open.assert_not_called()
        self.assertEqual(self.logs(), before)


if __name__ == "__main__":
    unittest.main()
