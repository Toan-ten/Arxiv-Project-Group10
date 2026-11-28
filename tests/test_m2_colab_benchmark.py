"""Real offline archive -> native parser -> Parquet -> strict audit -> reports."""
import csv
import hashlib
import json
from pathlib import Path
import socket
from unittest.mock import Mock

import pytest

from scripts import m2_colab_benchmark as bench
from scripts import m2_colab_export as export
from scripts import m2_colab_recovery as recovery
from src.m2 import runner
from tests.test_m2_colab_export import fixture_archive

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def blocked(*a, **kw):
        raise AssertionError("No network allowed")
    monkeypatch.setattr(socket.socket, "connect", blocked)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    corpus, archives, name, item = fixture_archive(tmp_path)
    config = dict(repo=str(REPO), release_commit=bench.subprocess.check_output(
        ["git", "-C", str(REPO), "rev-parse", "HEAD"], text=True).strip(),
        start_cohort=101, end_cohort=101, input_drive_dir=str(archives),
        output_drive_dir=str(tmp_path / "benchmark"), scratch=str(tmp_path / "scratch"))
    monkeypatch.setattr(bench, "verify_runtime", lambda c: dict(parser_sha=c["release_commit"],
        parser_source_commit=bench.PARSER_SOURCE_COMMIT, parser_fingerprint=bench.PARSER_FINGERPRINT))
    # Real parser/runner/audit, direct executor for inexpensive offline fixtures.
    monkeypatch.setattr(runner, "VersionExecutor", lambda seconds: lambda version, func, args: func(*args))
    return config, archives, name


def read_csv(path):
    return list(csv.DictReader(Path(path).open(encoding="utf-8-sig", newline="")))


def files_hashes(path):
    return {str(p.relative_to(path)): export.sha(p) for p in path.rglob("*") if p.is_file()}


def test_end_to_end_statuses_denominators_schema_and_resume(setup, monkeypatch):
    config, archives, name = setup
    before = files_hashes(archives)
    result = bench.operate(config, "smoke")
    s = result["summary"]
    assert s["statuses"] == dict(success=1, parse_error=1, empty_no_tex=1, source_unavailable=1)
    assert s["version_success_rate_percent"] == 25
    assert s["eligible_success_rate_percent"] == 50
    assert s["failure_rate_percent"] == 50
    assert s["missing_latex_rate_percent"] == 25
    assert s["paper_success_rate_percent"] == 0
    assert s["benchmark_complete"] and s["tables"]["bibtex"] == 1
    report = Path(result["report_directory"])
    versions = read_csv(report / "paper_results.csv")
    assert tuple(versions[0]) == bench.PAPER_COLUMNS
    assert all(float(v["runtime_seconds"]) > 0 for v in versions[:2])
    assert all(v["runtime_seconds"] == "" for v in versions[2:])
    assert all(v["runtime_scope"] == "prepare_render_worker_wall_including_spawn_this_attempt" for v in versions[:2])
    assert versions[0]["bibtex_count"] == "1"
    errors = read_csv(report / "m2_errors.csv")
    assert tuple(errors[0]) == bench.ERROR_COLUMNS
    assert {e["error_category"] for e in errors} == {"latex_parse_error", "no_tex_source", "input_missing"}
    assert all(e["exception_class"] == "" and e["traceback_path"] == "" for e in errors)
    assert len(list(report.glob("*.png"))) == 4
    assert all(p.stat().st_size > 1000 for p in report.glob("*.png"))
    assert files_hashes(archives) == before
    monkeypatch.setattr(runner, "run", Mock(side_effect=AssertionError("Resume must not parse")))
    resumed = bench.operate(json.loads(json.dumps(config)), "run")
    assert resumed["summary"]["statuses"] == s["statuses"]
    assert resumed["summary"]["measured_active_seconds"] == s["measured_active_seconds"]
    assert len(bench.load_events(Path(result["report_directory"]).parent.parent)) == 1
    assert files_hashes(archives) == before


@pytest.mark.parametrize("mutation,category", [("missing", "input_missing"), ("checksum", "checksum_mismatch"),
                                               ("corrupt", "archive_corrupt"), ("identity", "version_mismatch")])
def test_bad_inputs_keep_partial_reports_without_running_parser(setup, monkeypatch, mutation, category):
    config, archives, name = setup
    if mutation == "missing":
        (archives / (name + ".manifest.json")).unlink()
    elif mutation == "checksum":
        (archives / (name + ".tar.gz")).write_bytes(b"wrong checksum")
    elif mutation == "identity":
        p = archives / (name + ".manifest.json")
        d = export.read(p); d["cohort_number"] = 102; p.write_bytes(export.encoded(d))
    else:
        archive = archives / (name + ".tar.gz"); archive.write_bytes(b"not tar")
        p = archives / (name + ".manifest.json")
        d = export.read(p); d["archive_sha256"] = export.sha(archive); p.write_bytes(export.encoded(d))
        (archives / (name + ".tar.gz.sha256")).write_text(export.sha(archive) + "  " + archive.name)
    monkeypatch.setattr(runner, "run", Mock(side_effect=AssertionError("Bad input must not parse")))
    with pytest.raises(bench.BenchmarkFailure) as error:
        bench.operate(config, "run")
    assert error.value.category == category
    _, root, _ = bench.paths(config)
    report = Path(export.read(root / "LATEST.json")["report_directory"])
    s = export.read(report / "benchmark_summary.json")
    assert not s["benchmark_complete"] and s["versions"] == 0
    assert s["version_success_rate_percent"] is None
    errors = read_csv(report / "m2_errors.csv")
    assert errors[-1]["error_category"] == category
    assert Path(errors[-1]["traceback_path"]).is_file()
    assert not (root / "completed").exists()


def test_audit_violation_stops_and_preserves_native_outputs(setup, monkeypatch):
    config, _, name = setup
    config["end_cohort"] = 102
    original = recovery.audit_attempt
    def fail(attempt, rows, r, **kw):
        original(attempt, rows, r, **kw)
        raise ValueError("fixture invariant violation")
    monkeypatch.setattr(recovery, "audit_attempt", fail)
    with pytest.raises(bench.BenchmarkFailure, match="Dừng an toàn"):
        bench.operate(config, "run")
    _, root, _ = bench.paths(config)
    s = export.read(Path(export.read(root / "LATEST.json")["report_directory"]) / "benchmark_summary.json")
    assert s["audit_errors"] == 1 and s["unaudited_parse_outcomes"]
    assert s["tables"] == dict(elements=0, hierarchy=0, bibtex=0)
    assert len(list((root / "native" / name).rglob("result.json"))) == 1
    assert not (root / "native/cohort_0102_corpus").exists()
    assert not (root / "completed").exists()


def test_completed_checkpoint_tampering_never_skipped(setup, monkeypatch):
    config, _, name = setup
    bench.operate(config, "run")
    _, root, _ = bench.paths(config)
    marker = export.read(root / "completed" / (name + ".json"))["payload"]
    path = next(root / k for k in marker["evidence"] if k.endswith(".parquet"))
    path.write_bytes(b"damaged")
    monkeypatch.setattr(runner, "run", Mock(side_effect=AssertionError("Do not overwrite damaged evidence")))
    with pytest.raises(bench.BenchmarkFailure, match="Dừng an toàn"):
        bench.operate(config, "run")
    assert path.read_bytes() == b"damaged"


def test_restart_after_completion_before_timing_commit(setup):
    config, _, name = setup
    bench.operate(config, "run")
    _, root, _ = bench.paths(config)
    finished = next((root / "events").glob("*/finished.json"))
    finished.unlink()  # Simulate hard kill after immutable completion publication.
    result = bench.operate(config, "report")
    assert result["summary"]["benchmark_complete"]
    assert result["summary"]["interrupted_intervals_unmeasured"] == 1
    assert result["summary"]["throughput_versions_per_second"] is None
    assert result["summary"]["measured_active_seconds"] == 0


def test_partial_smoke_is_not_entire_range(setup):
    config, _, _ = setup
    config["end_cohort"] = 150
    result = bench.operate(config, "smoke")
    assert not result["summary"]["benchmark_complete"]
    assert result["summary"]["cohorts_observed"] == 1
    assert len(result["summary"]["corpus_range"]) == 50


@pytest.mark.parametrize("number,owner,aid", [(1,"24120010","2508.07872"), (51,"24120035","2507.16659"),
    (101,"24120232","2502.08235"), (200,"24120260","2501.07542")])
def test_all_200_cohorts_keep_owner_mapping(number, owner, aid):
    scope = bench.scope_for(dict(repo=str(REPO)), number)
    assert scope["owner"] == owner
    assert export.expected_ids(f"cohort_{number:04d}_corpus", scope)[0] == aid


@pytest.mark.parametrize("changes", [dict(start_cohort=0), dict(end_cohort=201), dict(start_cohort=True),
                                       dict(start_cohort=150,end_cohort=101)])
def test_invalid_range(setup, changes):
    config, _, _ = setup
    with pytest.raises(ValueError):
        bench.paths(config | changes)


def test_no_input_output_overlap(setup):
    config, archives, _ = setup
    with pytest.raises(ValueError, match="disjoint"):
        bench.paths(config | dict(output_drive_dir=str(archives / "outputs")))


def test_empty_metrics_and_informational_statuses():
    s = bench.summary([], [], ["cohort_0001_corpus"], Path("benchmark-empty"), {})
    assert not s["benchmark_complete"] and s["paper_success_rate_percent"] is None
    assert s["version_success_rate_percent"] is None and s["throughput_versions_per_second"] is None
    assert bench.error_category(dict(status="parse_error", missing_citation_keys=["k"])) == "citation_mapping_error"
    assert bench.error_category(dict(status="input_error")) == "unknown"


def test_no_changes_to_verified_parser():
    assert recovery.fingerprint(REPO) == bench.PARSER_FINGERPRINT
    assert bench.PARSER_SOURCE_COMMIT != export.PARSER_COMMIT


def test_measurement_preserves_executor_results_budget_and_exceptions(monkeypatch):
    calls = []
    class Original:
        def __init__(self, seconds):
            calls.append(seconds)
        def __call__(self, version, function, args):
            calls.append((version, function, args))
            return function(*args)
    monkeypatch.setattr(runner, "VersionExecutor", Original)
    def value(aid, data):
        return data
    def fail(aid):
        raise TimeoutError("preserved")
    with bench.measure_versions() as durations:
        measured = runner.VersionExecutor(300)
        token = object()
        assert measured(1, value, ("2502.08235", token)) is token
        with pytest.raises(TimeoutError, match="preserved"):
            measured(2, fail, ("2502.08235",))
    assert runner.VersionExecutor is Original
    assert calls[0] == 300
    assert set(durations) == {("2502.08235", 1), ("2502.08235", 2)}


def test_duplicate_input_triplets_are_not_arbitrarily_selected(setup):
    import shutil
    config, archives, _ = setup
    duplicate = archives / "duplicate"
    duplicate.mkdir()
    for p in list(archives.iterdir()):
        if p.is_file():
            shutil.copyfile(p, duplicate / p.name)
    with pytest.raises(bench.BenchmarkFailure, match="Dừng an toàn"):
        bench.operate(config, "smoke")
    _, root, _ = bench.paths(config)
    errors = read_csv(Path(export.read(root / "LATEST.json")["report_directory"]) / "m2_errors.csv")
    assert "Ambiguous archive triplets" in errors[-1]["error_message"]


def test_retryable_native_timeout_not_certified(setup, monkeypatch):
    config, _, _ = setup
    class TimeoutExecutor:
        def __init__(self, seconds):
            pass
        def __call__(self, *args):
            raise TimeoutError("fixture timeout")
    monkeypatch.setattr(runner, "VersionExecutor", TimeoutExecutor)
    with pytest.raises(bench.BenchmarkFailure, match="Dừng an toàn"):
        bench.operate(config, "smoke")
    _, root, _ = bench.paths(config)
    summary = export.read(Path(export.read(root / "LATEST.json")["report_directory"]) / "benchmark_summary.json")
    assert not summary["benchmark_complete"]
    assert summary["statuses"]["parse_error"] == 2
    assert not (root / "completed").exists()
    assert list((root / "native").rglob("partial/*.json"))


def test_crash_after_started_then_restart_does_not_invent_time(setup):
    config, _, name = setup
    _, root, key = bench.paths(config)
    root.mkdir(parents=True)
    export.immutable(root / "events/old/started.json", export.encoded(dict(cohort=name,
        event_id="old", started_at="2026-01-01T00:00:00+00:00", elapsed_seconds=None)))
    result = bench.operate(config, "run")
    assert result["summary"]["benchmark_complete"]
    assert result["summary"]["interrupted_intervals_unmeasured"] == 1
    assert result["summary"]["throughput_versions_per_second"] is None


def test_export_campaign_untouched_and_no_foreign_checkpoints_reused(setup, monkeypatch, tmp_path):
    config, _, _ = setup
    old = tmp_path / "old-export"
    old.mkdir()
    (old / "0115-bad.json").write_bytes(b"known invalid old cache")
    (old / "0101-complete.json").write_bytes(b"valid old export evidence")
    before = files_hashes(old)
    result = bench.operate(config, "smoke")
    assert result["summary"]["benchmark_complete"]
    assert files_hashes(old) == before
    assert not str(old) in str(result)


def test_report_does_not_certify_changed_parquet(setup):
    config, _, name = setup
    bench.operate(config, "smoke")
    _, root, _ = bench.paths(config)
    marker = export.read(root / "completed" / (name + ".json"))["payload"]
    path = next(root / k for k in marker["evidence"] if k.endswith(".parquet"))
    path.write_bytes(b"changed after audit")
    result = bench.operate(config, "report")
    assert result["summary"]["audit_errors"] == 1
    assert not result["summary"]["benchmark_complete"]
    assert result["summary"]["tables"] == dict(elements=0, hierarchy=0, bibtex=0)


def test_restart_environment_changes_are_recorded_without_new_campaign(setup, monkeypatch):
    config, _, _ = setup
    first = bench.operate(config, "smoke")
    original = bench.verify_runtime
    monkeypatch.setattr(bench, "verify_runtime", lambda c: dict(original(c), platform="new Colab VM"))
    monkeypatch.setattr(runner, "run", Mock(side_effect=AssertionError("valid cohort must remain cached")))
    second = bench.operate(config, "run")
    assert first["summary"]["run_id"] == second["summary"]["run_id"]
    assert second["summary"]["provenance"]["platform"] == "new Colab VM"


def test_export_campaign_cannot_be_benchmark_output(setup, tmp_path):
    config, _, _ = setup
    old = tmp_path / "old-campaign"
    old.mkdir(); (old / "campaign.json").write_bytes(b"do not touch")
    with pytest.raises(ValueError, match="export/recovery"):
        bench.paths(config | dict(output_drive_dir=str(old / "benchmark")))
    assert (old / "campaign.json").read_bytes() == b"do not touch"


def test_csv_preserves_commas_newlines_and_non_ascii():
    value = dict(cohort="cohort_0101_corpus", arxiv_id="2502.08235", version=1,
        error_category="unknown", error_type="fixture", error_message="comma, and\nnewline",
        source_file="main.tex", exception_class=None, traceback_path=None,
        status="parse_error", parser_sha="a" * 40)
    import io
    rows = list(csv.DictReader(io.StringIO(bench.csv_bytes([value], bench.ERROR_COLUMNS).decode("utf-8-sig"))))
    assert rows[0]["error_message"] == value["error_message"]


def test_diagnostic_types_are_observed_message_families_not_inferred_causes():
    record = dict(arxiv_id="2502.08235", version=1, status="parse_error", error="Unsupported prose command: mystery", error_origin="unknown")
    attempt = dict(versions_detail=[record], provenance=dict(git_commit="a" * 40))
    versions, errors = bench.version_rows("cohort_0101_corpus", attempt, "verified")
    assert errors[0]["error_type"] == "Unsupported prose command"
    assert errors[0]["error_category"] == "latex_parse_error"
    assert errors[0]["exception_class"] is None


def test_reporting_avoids_quadratic_native_reads_within_one_writer(setup, monkeypatch):
    config, _, name = setup
    bench.operate(config, "smoke")
    _, root, _ = bench.paths(config)
    native_reads = []
    original = export.sha
    def observed(path):
        if "/native/" in Path(path).as_posix():
            native_reads.append(str(path))
        return original(path)
    monkeypatch.setattr(export, "sha", observed)
    identity = bench.verify_runtime(config)
    verified = {}
    bench.write_reports(root, identity, [name], verified)
    count = len(native_reads)
    assert count > 0
    bench.write_reports(root, identity, [name], verified)
    assert len(native_reads) == count
    # Restart/report invocation must not trust the previous process's cache.
    bench.write_reports(root, identity, [name])
    assert len(native_reads) == count * 2
