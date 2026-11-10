"""Live smoke script tests: real Batch/Pipeline/Storage, fake HTTP, TEMP only."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import test_pipeline as fixtures
from scripts import live_smoke
from src.batch import run_batch
from src.pilots import PilotEntry

PAIR, OTHER, THIRD = fixtures.PAIR, fixtures.OTHER, fixtures.THIRD
PAIRS = (PAIR, OTHER, THIRD)
Response, TEX = fixtures.Response, fixtures.TEX
xml_response = fixtures.xml_response


def entry(pair):
    return PilotEntry(arxiv_id=pair[1], mssv=pair[0], range_start=pair[1],
                      range_end=pair[1], reason="fixture")


class PilotLoadingTests(unittest.TestCase):
    def write(self, payload):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "pilot.json"
        path.write_text(payload if isinstance(payload, str)
                        else json.dumps(payload), encoding="utf-8")
        return path

    def test_repository_pilot_10_matches_the_generator(self):
        entries = live_smoke.load_pilot()
        self.assertEqual(len(entries), 10)
        self.assertEqual(live_smoke.pilot_problems(entries), [])

    def test_duplicate_id_is_rejected(self):
        item = dict(arxiv_id=PAIR[1], mssv=PAIR[0], range_start=PAIR[1],
                    range_end=PAIR[1], reason="range_start")
        with self.assertRaises(SystemExit):
            live_smoke.load_pilot(self.write([item, dict(item)]))

    def test_unassigned_id_is_rejected(self):
        item = dict(arxiv_id="2601.00001", mssv=PAIR[0], range_start="2601.00001",
                    range_end="2601.00001", reason="range_start")
        with self.assertRaises(SystemExit):
            live_smoke.load_pilot(self.write([item]))

    def test_malformed_file_is_rejected(self):
        with self.assertRaises(SystemExit):
            live_smoke.load_pilot(self.write("{}"))
        with self.assertRaises(SystemExit):
            live_smoke.load_pilot(self.write([{"arxiv_id": PAIR[1]}]))

    def test_pilot_drift_is_reported(self):
        entries = live_smoke.load_pilot()[:9]
        problems = live_smoke.pilot_problems(entries)
        self.assertEqual(len(problems), 1)
        self.assertIn("build_smoke_set", problems[0])


class InterruptTests(unittest.TestCase):
    def test_inactive_by_default(self):
        self.assertFalse(live_smoke.SimulatedInterrupt().active)

    def test_raises_after_n_finished_papers(self):
        interrupt = live_smoke.SimulatedInterrupt(after=2)
        interrupt("paper_started", {"papers": summary_papers(1)})
        interrupt("paper_finished", {"papers": summary_papers(1)})
        self.assertEqual(interrupt.finished, 1)
        with self.assertRaises(KeyboardInterrupt):
            interrupt("paper_finished", {"papers": summary_papers(2)})

    def test_raises_when_the_named_paper_starts(self):
        interrupt = live_smoke.SimulatedInterrupt(at_start=2)
        interrupt("paper_started", {"papers": summary_papers(1)})
        with self.assertRaises(KeyboardInterrupt):
            interrupt("paper_started", {"papers": summary_papers(2)})

    def test_cli_flags_are_mutually_exclusive(self):
        parser = live_smoke.build_parser()
        args = parser.parse_args(["smoke", "--interrupt-after", "5"])
        self.assertEqual(args.interrupt_after, 5)
        with self.assertRaises(SystemExit):
            parser.parse_args(["smoke", "--interrupt-after", "5",
                               "--interrupt-at-start", "3"])


def summary_papers(count):
    return [{"arxiv_id": pair[1]} for pair in PAIRS[:count]]


class TempSweepTests(unittest.TestCase):
    def test_finds_every_temporary_marker(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "2502-08235" / "v1").mkdir(parents=True)
            (root / "2502-08235" / "v1" / "main.tex").write_text("ok", encoding="utf-8")
            (root / "2502-08235" / ".manifest.json.abc.tmp").write_text("x", encoding="utf-8")
            (root / "2502-08235" / "v2.part").write_text("x", encoding="utf-8")
            (root / "notes.writing").write_text("x", encoding="utf-8")
            found = {Path(item["path"]).name for item in live_smoke.sweep_temp(root)}
            self.assertEqual(found, {".manifest.json.abc.tmp", "v2.part", "notes.writing"})

    def test_clean_tree_reports_nothing(self):
        with tempfile.TemporaryDirectory() as name:
            (Path(name) / "2502-08235" / "v1").mkdir(parents=True)
            self.assertEqual(live_smoke.sweep_temp(Path(name)), [])
        self.assertEqual(live_smoke.sweep_temp(Path(tempfile.gettempdir()) / "missing"), [])


class CorpusVerificationTests(unittest.TestCase):
    """Verify runs against a corpus produced by the real Batch/Pipeline."""

    setUp = fixtures.PipelineTests.setUp
    open_response = fixtures.PipelineTests.open_response
    sleep = fixtures.PipelineTests.sleep

    def crawl(self, pairs=PAIRS):
        self.outcomes = [
            response for pair in pairs
            for response in (xml_response(pair[1], versions=1), Response(TEX))
        ]
        return run_batch(
            list(pairs), self.root, batch_id="offline-smoke", max_ids=len(pairs),
            log_root=Path(self.temp.name) / "logs", versions=("v1",),
            timeout=15, max_attempts=1,
        )

    def test_verified_corpus_passes(self):
        self.crawl()
        report = live_smoke.verify_corpus(self.root, [entry(pair) for pair in PAIRS])
        self.assertTrue(report["ok"], report["problems"])
        self.assertEqual(len(report["papers"]), 3)
        for paper in report["papers"]:
            self.assertTrue(paper["present"])
            self.assertEqual(paper["versions"][0]["version"], "v1")
            self.assertEqual(paper["versions"][0]["verified"], 1)
            self.assertEqual(report["temp_files"], [])
        self.assertIn("Result: PASS", live_smoke.render_verify(report))

    def test_overwritten_tex_is_reported(self):
        self.crawl()
        target = self.root / "2502-08235" / "v1" / "main.tex"
        target.write_bytes(TEX + b"% tampered\r\n")
        report = live_smoke.verify_corpus(self.root, [entry(pair) for pair in PAIRS])
        self.assertFalse(report["ok"])
        self.assertTrue(any("checksum mismatch" in item for item in report["problems"]))

    def test_image_in_version_folder_is_reported(self):
        self.crawl()
        (self.root / "2502-08235" / "v1" / "figure.png").write_bytes(b"\x89PNG")
        report = live_smoke.verify_corpus(self.root, [entry(pair) for pair in PAIRS])
        self.assertFalse(report["ok"])
        self.assertTrue(any("not in manifest.files" in item for item in report["problems"]))

    def test_missing_paper_and_temp_file_are_reported(self):
        self.crawl()
        (self.root / "2507-16659" / ".manifest.json.x.tmp").write_text("{}", encoding="utf-8")
        report = live_smoke.verify_corpus(self.root, [entry(pair) for pair in PAIRS] + [
            entry(("24120260", "2501.02642")),
        ])
        self.assertFalse(report["ok"])
        self.assertEqual(len(report["temp_files"]), 1)
        self.assertTrue(any("2501-02642 missing" in item for item in report["problems"]))
        self.assertIn("Result: FAIL", live_smoke.render_verify(report))

    def test_wrong_contract_version_is_reported(self):
        self.crawl()
        path = self.root / "2508-07872" / "metadata.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["contract_version"] = 1
        path.write_text(json.dumps(value), encoding="utf-8")
        report = live_smoke.verify_corpus(self.root, [entry(pair) for pair in PAIRS])
        self.assertTrue(any("contract_version=1" in item for item in report["problems"]))

    def test_empty_corpus_fails_without_raising(self):
        report = live_smoke.verify_corpus(Path(self.temp.name) / "missing",
                                          [entry(pair) for pair in PAIRS])
        self.assertFalse(report["ok"])
        self.assertEqual(len(report["problems"]), 3)


class ResumeProofTests(unittest.TestCase):
    """The card's core claim: interrupt at paper 2, resume, prove no re-crawl.

    These tests drive the real CLI (``live_smoke.main``) so the evidence files,
    the snapshot and the proof report are exercised end to end.
    """

    setUp = fixtures.PipelineTests.setUp
    open_response = fixtures.PipelineTests.open_response
    sleep = fixtures.PipelineTests.sleep

    def setUp(self):
        fixtures.PipelineTests.setUp(self)
        self.logs = Path(self.temp.name) / "logs"
        self.entries = [entry(pair) for pair in PAIRS]
        pilot = Path(self.temp.name) / "pilot_3.json"
        pilot.write_text(
            json.dumps([{"arxiv_id": pair[1], "mssv": pair[0],
                         "range_start": pair[1], "range_end": pair[1],
                         "reason": "fixture"} for pair in PAIRS]),
            encoding="utf-8",
        )
        self.pilot = str(pilot)
        self.batch_id = "offline-resume"
        self.batch_dir = self.logs / self.batch_id

    def cli(self, *argv):
        stream, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(errors):
            code = live_smoke.main(list(argv))
        return code, stream.getvalue() + errors.getvalue()

    def common(self):
        return ["--pilot", self.pilot, "--batch-id", self.batch_id,
                "--corpus-root", str(self.root), "--log-root", str(self.logs),
                "--max-ids", "3", "--versions", "v1", "--timeout", "15"]

    def interrupt_after_two(self):
        self.outcomes = [
            response for pair in PAIRS
            for response in (xml_response(pair[1], versions=1), Response(TEX))
        ]
        code, output = self.cli("smoke", *self.common(), "--interrupt-after", "2")
        self.assertEqual(code, 0)
        self.assertIn("interrupted", output.lower())
        self.assertEqual(self.opener.open.call_count, 4)

    def resume_third(self):
        self.opener.open.reset_mock()
        self.outcomes = [xml_response(THIRD[1], versions=1), Response(TEX)]
        code, output = self.cli("resume", *self.common())
        self.assertEqual(code, 0)
        self.assertIn("Status: finished", output)
        return output

    def proof(self):
        code, output = self.cli(
            "proof", "--pilot", self.pilot, "--batch-id", self.batch_id,
            "--corpus-root", str(self.root), "--log-root", str(self.logs),
            "--md-out", str(Path(self.temp.name) / "proof.md"),
        )
        return code, output

    def test_interrupt_then_resume_crawls_only_the_rest(self):
        self.interrupt_after_two()
        self.resume_third()
        self.assertEqual(self.opener.open.call_count, 2)  # THIRD: discovery + source
        self.assertEqual(len(self.attempts()), 2)

        report = live_smoke.build_proof(self.batch_dir, self.root, self.entries)
        self.assertTrue(report["ok"], report["problems"])
        self.assertEqual(report["interrupted_after"], 2)
        self.assertEqual([item["index"] for item in report["skipped"]], [1, 2])
        self.assertEqual([item["index"] for item in report["resumed"]], [3])
        self.assertTrue(all(item["ok"] for item in report["checks"]))

        code, output = self.proof()
        self.assertEqual(code, 0)
        self.assertIn("Result: PASS", output)
        self.assertIn("Skipped on resume", output)
        self.assertTrue((Path(self.temp.name) / "proof.md").is_file())

    def test_snapshot_proves_files_were_not_rewritten(self):
        self.interrupt_after_two()
        self.resume_third()
        report = live_smoke.build_proof(self.batch_dir, self.root, self.entries)
        checks = {item["check"]: item for item in report["checks"]}
        self.assertIn("kept their data", checks["existing files not rewritten"]["detail"])
        self.assertEqual(report["snapshot"]["rewritten"], [])
        # 2 papers x (metadata, references, manifest, v1/main.tex). Only the two
        # manifests move: resume stamps the new run_id on them. The .tex files
        # and the collected JSON data stay untouched.
        self.assertEqual(report["snapshot"]["unchanged"], 6)
        self.assertEqual(len(report["snapshot"]["refreshed"]), 2)
        self.assertTrue(all(path.endswith("manifest.json")
                            for path in report["snapshot"]["refreshed"]))
        self.assertEqual(report["snapshot"]["files"], 8)

    def test_overwritten_file_breaks_the_snapshot_check(self):
        self.interrupt_after_two()
        # Rewrite a crawled file with the same bytes before resuming: the digest
        # still matches, so only mtime_ns can tell that it was touched.
        target = self.root / "2502-08235" / "v1" / "main.tex"
        target.write_bytes(TEX)
        self.resume_third()
        report = live_smoke.build_proof(self.batch_dir, self.root, self.entries)
        checks = {item["check"]: item for item in report["checks"]}
        self.assertFalse(checks["existing files not rewritten"]["ok"])
        self.assertIn("new mtime", " ".join(report["snapshot"]["rewritten"]))

    def test_lost_version_record_breaks_the_snapshot_check(self):
        self.interrupt_after_two()
        path = self.root / "2502-08235" / "manifest.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["versions"]["v1"]["status"] = "pending"
        path.write_text(json.dumps(value), encoding="utf-8")
        self.resume_third()
        report = live_smoke.build_proof(self.batch_dir, self.root, self.entries)
        checks = {item["check"]: item for item in report["checks"]}
        self.assertFalse(checks["existing files not rewritten"]["ok"])
        self.assertIn("content changed", " ".join(report["snapshot"]["rewritten"]))

    def attempts(self):
        """Attempt logs in execution order (file names are random UUIDs)."""
        values = [json.loads(path.read_text(encoding="utf-8"))
                  for path in (self.batch_dir / "attempts").glob("*.json")]
        return sorted(values, key=lambda value: value["attempt_number"])

    def test_resume_records_the_previous_attempt(self):
        self.interrupt_after_two()
        self.resume_third()
        first, second = self.attempts()
        self.assertEqual(first["status"], "interrupted")
        self.assertEqual(first["attempt_number"], 1)
        self.assertEqual(second["attempt_number"], 2)
        self.assertEqual(first["previous_incomplete_attempts"], [])

    def test_proof_without_interruption_fails_cleanly(self):
        self.outcomes = [
            response for pair in PAIRS
            for response in (xml_response(pair[1], versions=1), Response(TEX))
        ]
        self.cli("smoke", *self.common())
        code, output = self.proof()
        self.assertEqual(code, 1)
        self.assertIn("no interrupted attempt", output)
        self.assertIn("Result: FAIL", output)

    def test_proof_without_a_batch_directory_fails_cleanly(self):
        report = live_smoke.build_proof(self.logs / "missing", self.root, self.entries)
        self.assertFalse(report["ok"])
        self.assertIn("missing batch definition", report["problems"][0])
        self.assertIn("Result: FAIL", live_smoke.render_proof(report))

    def test_resume_without_a_batch_is_rejected(self):
        code, _ = self.cli("resume", *self.common())
        self.assertEqual(code, 2)
        self.opener.open.assert_not_called()


if __name__ == "__main__":
    unittest.main()
