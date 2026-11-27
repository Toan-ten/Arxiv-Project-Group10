"""Offline export orchestration and actual pinned-runner Parquet checks."""
import ast
import hashlib
import io
import json
from pathlib import Path
import tarfile
from unittest.mock import Mock

import pytest

from scripts import m2_colab_export as export
from src.m2 import runner


def fixture_archive(tmp_path, number=101, student="24120232"):
    conf = make_config(tmp_path, student, number, number)
    scope = export.scope_for(conf)
    name = f"cohort_{number:04d}_corpus"
    aid = export.expected_ids(name, scope)[0]
    corpus = tmp_path / "input" / name
    paper = corpus / aid.replace(".", "-")
    paper.mkdir(parents=True)
    versions = {}
    for v in (1, 2):
        root = paper / f"v{v}"
        root.mkdir()
        text = (r"\documentclass{article}\begin{document}\section{Evidence}"
                r"Preserved $x=y$ \cite{k}.\bibliography{refs}\end{document}")
        if v == 2:
            text = text.replace("Preserved", r"\unimplementedCommand{Preserved}")
        (root / "main.tex").write_text(text, encoding="utf8")
        (root / "refs.bib").write_text("@article{k, title={Evidence}, author={Doe, Jane}, year={2025}}", encoding="utf8")
        files = sorted(p.name for p in root.iterdir())
        versions[f"v{v}"] = dict(status="success", files=files, file_checksums={n: export.sha(root / n) for n in files})
    versions.update(v3=dict(status="no_tex_source"), v4=dict(status="timeout"))
    manifest = dict(contract_version=2, arxiv_id=aid, mssv=student, versions=versions)
    for filename in ("manifest.json", "metadata.json"):
        (paper / filename).write_bytes(export.encoded(manifest))
    archives = tmp_path / "archives"
    archives.mkdir(exist_ok=True)
    archive = archives / (name + ".tar.gz")
    with tarfile.open(archive, "w:gz") as t:
        t.add(paper, arcname=paper.name)
    item = dict(archive=archive.name, archive_sha256=export.sha(archive),
                input_fingerprint=runner.source_fingerprint(corpus)[0], papers=1, versions=4,
                zero_version_publications=[])
    ids = export.expected_ids(name, scope)
    am = dict(cohort_number=number, archive_filename=archive.name, archive_sha256=export.sha(archive),
              expected_ids=ids, expected_count=100, included_ids=[aid], included_count=1, omitted_count=99)
    (archives / (name + ".manifest.json")).write_bytes(export.encoded(am))
    (archives / (name + ".tar.gz.sha256")).write_text(export.sha(archive) + "  " + archive.name, encoding="utf8")
    item = export.discover_item(name, scope, archives)
    return corpus, archives, name, item


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import socket
    import urllib.request
    def blocked(*args, **kwargs):
        raise AssertionError("Network is forbidden in export tests")
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(urllib.request, "urlopen", blocked)
    # The fixture uses the unchanged parser source in the development checkout.
    # Model its separate pinned Colab checkout for runner provenance; never edit
    # production reports or relax the production commit guard.
    original = runner.subprocess.check_output
    def pinned_git(args, *a, **kw):
        if args[:2] == ["git", "-c"] and args[-2:] == ["rev-parse", "HEAD"]:
            return export.PARSER_COMMIT
        return original(args, *a, **kw)
    monkeypatch.setattr(runner.subprocess, "check_output", pinned_git)


def make_config(tmp_path, student="24120232", start=101, end=150, size=5):
    return dict(repo=str(Path(__file__).resolve().parents[1]), student_id=student,
                start_cohort=start, end_cohort=end, batch_size=size,
                input_drive_dir=str(tmp_path / "archives"), output_drive_dir=str(tmp_path / "exports"),
                scratch=str(tmp_path / "scratch"), hf_repo_id="member/m2-dataset",
                helper_commit=export.PARSER_COMMIT)


def campaign(config):
    return export.campaign_path(config, export.scope_for(config))


@pytest.fixture
def setup(tmp_path, monkeypatch):
    corpus, archives, name, item = fixture_archive(tmp_path)
    config = make_config(tmp_path)
    monkeypatch.setattr(export, "verify_runtime", lambda _: runner)
    return config, item, corpus


@pytest.mark.parametrize("student,start,end,first_id", [
    ("24120010", 1, 50, "2508.07872"), ("24120035", 51, 100, "2507.16659"),
    ("24120232", 101, 150, "2502.08235"), ("24120260", 151, 200, "2501.02642"),
])
def test_all_four_assignments(tmp_path, student, start, end, first_id):
    scope = export.scope_for(make_config(tmp_path, student, start, end))
    assert len(scope["cohorts"]) == 50
    assert export.expected_ids(scope["cohorts"][0], scope)[0] == first_id


@pytest.mark.parametrize("changes", [dict(student_id="00000000"), dict(start_cohort=100),
    dict(end_cohort=151), dict(start_cohort=102,end_cohort=101), dict(batch_size=0),
    dict(batch_size=True), dict(hf_repo_id="../bad/path")])
def test_invalid_scope_fails_safely(tmp_path, changes):
    with pytest.raises(ValueError):
        export.scope_for(make_config(tmp_path) | changes)


def test_native_export_partial_coverage_and_resume_without_parser(setup, monkeypatch):
    config, catalog, _ = setup
    result = export.operate(config, "smoke")
    assert result["statuses"] == dict(success=1, parse_error=1, empty_no_tex=1, source_unavailable=1)
    assert result["verified"] and result["partial_coverage"]
    assert result["tables"]["bibtex"] == 1
    monkeypatch.setattr(runner, "run", Mock(side_effect=AssertionError("must audit only")))
    assert export.operate(config, "smoke") == result


def test_catalog_mismatch_never_reuses_campaign(setup):
    config, _, _ = setup
    export.operate(config, "smoke")
    path = Path(config["input_drive_dir"]) / "cohort_0101_corpus.manifest.json"
    manifest = export.read(path)
    manifest["phase_certification"] = "changed"
    path.write_bytes(export.encoded(manifest))
    with pytest.raises(ValueError, match="Input catalog changed"):
        export.operate(config, "smoke")


def test_restart_with_changed_parser_manifest_is_rejected(setup):
    config, catalog, _ = setup
    export.operate(config, "smoke")
    path = campaign(config) / "campaign.json"
    identity = export.read(path)
    identity["parser_commit"] = "different-commit"
    path.write_bytes(export.encoded(identity))
    with pytest.raises(ValueError, match="Existing evidence differs"):
        export.operate(config, "smoke")


def test_corrupt_completed_parquet_not_reused(setup):
    config, catalog, _ = setup
    result = export.operate(config, "smoke")
    path = Path(result["publication_root"]) / "data/bibtex/part-00000.parquet"
    path.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="checksum"):
        export.operate(config, "smoke")


def test_own_version_citation_evidence_required(setup):
    config, catalog, _ = setup
    result = export.operate(config, "smoke")
    root = Path(result["publication_root"])
    path = next(root.glob("papers/*/result.json"))
    envelope = export.read(path)
    envelope["payload"]["versions"][0]["bibliography_evidence"] = []
    envelope["sha256"] = runner.digest(runner.encoded(envelope["payload"]))
    path.write_bytes(export.encoded(envelope))
    with pytest.raises(ValueError, match="own version"):
        export.operate(config, "smoke")


def test_retryable_attempt_cannot_be_certified(setup):
    config, catalog, corpus = setup
    rows = runner.selection(corpus)
    with pytest.raises(ValueError, match="partial"):
        export.audit(dict(run_root="x", publication_root="x", retryable=True), rows, runner)


@pytest.mark.parametrize("kind", ["traversal", "symlink", "absolute", "windows"])
def test_unsafe_archive_rejected(tmp_path, kind):
    corpus, archives, name, item = fixture_archive(tmp_path)
    archive = archives / item["archive"]
    with tarfile.open(archive, "w:gz") as t:
        member = tarfile.TarInfo({"traversal": "../outside", "absolute": "/outside",
                                  "windows": "C:\\outside", "symlink": "linked"}[kind])
        if kind == "symlink":
            member.type, member.linkname = tarfile.SYMTYPE, "../outside"
            t.addfile(member)
        else:
            member.size = 1
            t.addfile(member, io.BytesIO(b"x"))
    item["archive_sha256"] = export.sha(archive)
    with pytest.raises(ValueError, match="Unsafe|Link"):
        export.materialize(name, item, archives, tmp_path / "scratch", runner, export.scope_for(make_config(tmp_path)))
    assert not (tmp_path / "outside").exists()


def test_bad_archive_hash_rejected(tmp_path):
    _, archives, name, item = fixture_archive(tmp_path)
    item["archive_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="corrupt"):
        export.materialize(name, item, archives, tmp_path / "scratch", runner, export.scope_for(make_config(tmp_path)))


def test_interrupted_five_cohort_batch_resumes_and_packages_partial_coverage(setup, tmp_path, monkeypatch):
    config, catalog, _ = setup
    for n in range(102, 106):
        _, _, name, item = fixture_archive(tmp_path, n)
    original = runner.run
    calls = []

    def stop_second(corpus, output):
        calls.append(Path(corpus).name)
        if len(calls) == 2:
            raise KeyboardInterrupt()
        return original(corpus, output)

    monkeypatch.setattr(runner, "run", stop_second)
    with pytest.raises(KeyboardInterrupt):
        export.operate(config, "next")
    assert not list((campaign(config) / "batches").glob("*.json"))
    assert len(list((campaign(config) / "verified").glob("*.json"))) == 1
    monkeypatch.setattr(runner, "run", original)
    package = Path(export.operate(config, "next"))
    coverage = export.read(package / "coverage.json")
    assert coverage["cohort_count"] == 5 and coverage["processing_complete"]
    assert not coverage["all_versions_parsed"] and not coverage["assigned_range_complete"]
    assert len(coverage["versions"]) == 20
    assert len(list(package.glob("data/*/*.parquet"))) == 15
    assert export.read(package / "READY.json")["verified"]
    monkeypatch.setattr(runner, "run", Mock(side_effect=AssertionError("audit only")))
    assert len(export.operate(config, "audit")) == 5
    next(package.glob("data/elements/*.parquet")).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="corrupt"):
        export.operate(config, "next")


def test_writer_lock_is_never_broken_automatically(tmp_path):
    with export.writer(tmp_path):
        with pytest.raises(FileExistsError):
            with export.writer(tmp_path):
                pass
    assert not (tmp_path / ".export-writer.lock").exists()


def test_output_refuses_overwrite(tmp_path):
    path = tmp_path / "existing"
    export.immutable(path, b"old")
    with pytest.raises(ValueError, match="overwrite|differs"):
        export.immutable(path, b"new")
    assert path.read_bytes() == b"old"


def test_notebook_has_one_config_and_no_embedded_personal_catalog():
    repo = Path(__file__).resolve().parents[1]
    nb = export.read(repo / "notebooks/m2/m2_team_colab_export.ipynb")
    assert nb["nbformat"] == 4
    sources = ["".join(c["source"]) for c in nb["cells"] if c["cell_type"] == "code"]
    for source in sources:
        ast.parse(source)
    config = sources[0]
    for key in ("STUDENT_ID", "START_COHORT", "END_COHORT", "BATCH_SIZE", "INPUT_DRIVE_DIR", "OUTPUT_DRIVE_DIR", "HF_REPO_ID"):
        assert key + " =" in config
    assert "INPUT_CATALOG_JSON" not in "\n".join(sources)
    assert "EXPORT_TOOL_SOURCE" not in "\n".join(sources)
    assert export.PARSER_COMMIT in sources[1]
    assert all(not c.get("outputs") for c in nb["cells"])


def test_uppercase_tex_is_explicit_failed_coverage_not_fabricated_success(setup, monkeypatch):
    config, catalog, corpus = setup
    paper = next(corpus.iterdir())
    root = paper / "v1"
    (root / "main.tex").rename(root / "main.TEX")
    manifest = export.read(paper / "manifest.json")
    files = sorted(p.name for p in root.iterdir())
    manifest["versions"]["v1"].update(files=files, file_checksums={n: export.sha(root/n) for n in files})
    (paper / "manifest.json").write_bytes(export.encoded(manifest))
    name = "cohort_0101_corpus"
    archive = Path(config["input_drive_dir"]) / (name + ".tar.gz")
    with tarfile.open(archive, "w:gz") as t:
        t.add(paper, arcname=paper.name)
    manifest_path = archive.with_name(name + ".manifest.json")
    am = export.read(manifest_path)
    am["archive_sha256"] = export.sha(archive)
    manifest_path.write_bytes(export.encoded(am))
    archive.with_name(name + ".tar.gz.sha256").write_text(export.sha(archive) + "  " + archive.name, encoding="utf8")
    original = runner.inspect_source

    def linux_inspection(directory, version, record):
        result = original(directory, version, record)
        if version == 1:
            raise runner.ParseError("M1 success has no TeX")
        return result

    monkeypatch.setattr(runner, "inspect_source", linux_inspection)
    result = export.operate(config, "smoke")
    assert result["statuses"]["input_error"] == 1
    assert result["statuses"].get("success", 0) == 0
    assert result["partial_coverage"] and result["tables"]["elements"] == 0


def test_input_checksum_failure_blocks_materialization(tmp_path):
    corpus, archives, name, item = fixture_archive(tmp_path)
    extracted, _ = export.materialize(name, item, archives, tmp_path / "scratch", runner, export.scope_for(make_config(tmp_path)))
    next(extracted.glob("*/v1/main.tex")).write_text("tampered", encoding="utf8")
    with pytest.raises(ValueError, match="differs"):
        export.materialize(name, item, archives, tmp_path / "scratch", runner, export.scope_for(make_config(tmp_path)))


def test_runtime_rejects_a_different_commit(monkeypatch, tmp_path):
    monkeypatch.setattr(export.subprocess, "check_output", lambda *a, **k: "different-commit")
    with pytest.raises(ValueError, match="commit"):
        export.verify_runtime(tmp_path)

def test_notebook_setup_and_launcher_commands_without_network(tmp_path, monkeypatch, capsys):
    import subprocess
    import sys
    import types
    repo = Path(__file__).resolve().parents[1]
    nb = export.read(repo / "notebooks/m2/m2_team_colab_export.ipynb")
    source = next("".join(c["source"]) for c in nb["cells"] if c["cell_type"] == "code"
                  and "def checkout_release" in "".join(c["source"]))
    # Translate only ephemeral /content paths for the Windows fixture.
    source = source.replace('Path("/content/m2-parser-" + PARSER_COMMIT[:12])', 'TEST_REPO')
    source = source.replace('Path("/content/m2-export-helper-" + HELPER_COMMIT[:12])', 'TEST_REPO')
    source = source.replace('Path("/content")', 'TEST_TEMP')
    namespace = dict(Path=Path, TEST_REPO=repo, TEST_TEMP=tmp_path, STUDENT_ID="24120232",
                     START_COHORT=101, END_COHORT=101, BATCH_SIZE=1,
                     INPUT_DRIVE_DIR=str(tmp_path/"archives"), OUTPUT_DRIVE_DIR=str(tmp_path/"outputs"),
                     HF_REPO_ID="member/dataset")
    Path(namespace["INPUT_DRIVE_DIR"]).mkdir()
    for suffix in (".tar.gz", ".manifest.json", ".tar.gz.sha256"):
        (Path(namespace["INPUT_DRIVE_DIR"]) / ("cohort_0101_corpus" + suffix)).write_bytes(b"mock launcher input")
    colab = types.ModuleType("google.colab")
    colab.drive = types.SimpleNamespace(mount=Mock())
    monkeypatch.setitem(sys.modules, "google.colab", colab)
    monkeypatch.delenv("COLAB_TPU_ADDR", raising=False)
    monkeypatch.delenv("COLAB_GPU", raising=False)
    monkeypatch.setattr(export.shutil, "which", lambda _: None)
    disk = export.shutil.disk_usage(tmp_path)
    monkeypatch.setattr(export.shutil, "disk_usage", lambda _: disk)
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: calls.append(args))
    # Read the pin assignments from this notebook before mocking checkout identity.
    pins = {}
    tree = ast.parse(source)
    for n in tree.body:
        if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name) and n.targets[0].id in {"PARSER_COMMIT", "HELPER_COMMIT"}:
            pins[n.targets[0].id] = ast.literal_eval(n.value)
    identities = iter([pins["PARSER_COMMIT"], pins["HELPER_COMMIT"]])
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **kw: next(identities))
    exec(source, namespace)
    assert any("pip" in c and c[-1] == str(repo / "requirements-m2.txt") for c in calls)
    popen_calls = []
    process = types.SimpleNamespace(stdout=io.StringIO("fake cohort verified\n"), wait=Mock(return_value=0))
    monkeypatch.setattr(subprocess, "Popen", lambda args, **kw: (popen_calls.append((args, kw)) or process))
    namespace["run_export"]("smoke")
    assert popen_calls[0][0][-2:] == ["--action", "smoke"]
    assert "--catalog" not in popen_calls[0][0]
    assert popen_calls[0][1]["start_new_session"]
    assert "fake cohort verified" in capsys.readouterr().out
    assert process.stdout.closed


def test_notebook_stop_interrupts_the_whole_export_process_group(monkeypatch):
    import os
    import signal
    import subprocess
    import sys
    from types import SimpleNamespace
    repo = Path(__file__).resolve().parents[1]
    nb = export.read(repo / "notebooks/m2/m2_team_colab_export.ipynb")
    source = next("".join(c["source"]) for c in nb["cells"] if c["cell_type"] == "code"
                  and "def run_export(action):" in "".join(c["source"]))
    function = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == "run_export")
    namespace = dict(os=os, sys=sys, subprocess=subprocess, HELPER_ROOT=Path("tools"), CONFIG_PATH=Path("config.json"),
                     require_input_files=Mock(), active_input_cohorts=Mock(return_value=[]))
    exec(compile(ast.Module(body=[function], type_ignores=[]), "<notebook>", "exec"), namespace)
    stream = Mock()
    stream.__iter__ = Mock(side_effect=KeyboardInterrupt)
    process = SimpleNamespace(pid=123456, stdout=stream, wait=Mock(return_value=0))
    monkeypatch.setattr(subprocess, "Popen", Mock(return_value=process))
    kill = Mock()
    monkeypatch.setattr(os, "killpg", kill, raising=False)
    with pytest.raises(KeyboardInterrupt):
        namespace["run_export"]("next")
    kill.assert_called_once_with(123456, signal.SIGINT)
    process.wait.assert_called_once_with(timeout=30)
    stream.close.assert_called_once()


@pytest.mark.parametrize("student,start,end", [("24120010", 2, 3), ("24120035", 99, 100), ("24120260", 199, 200)])
def test_native_runner_for_other_members(tmp_path, monkeypatch, student, start, end):
    fixture_archive(tmp_path, start, student)
    fixture_archive(tmp_path, end, student)
    config = make_config(tmp_path, student, start, end, 2)
    monkeypatch.setattr(export, "verify_runtime", lambda _: runner)
    result = export.operate(config, "smoke")
    assert result["versions"] == 4
    package = Path(export.operate(config, "next"))
    coverage = export.read(package / "coverage.json")
    assert coverage["owner"] == student and coverage["hf_repo_id"] == config["hf_repo_id"]
    assert len(coverage["versions"]) == 8
    assert coverage["partial_coverage"] and len(coverage["omitted_ids"]) == 198
    assert student in str(package)
    assert export.operate(config, "audit") == [f"cohort_{n:04d}_corpus" for n in (start,end)]


def test_hf_repo_and_student_campaign_isolation(tmp_path):
    a = make_config(tmp_path)
    assert campaign(a) != campaign(a | dict(hf_repo_id="another/dataset"))
    b = make_config(tmp_path, "24120260", 151, 200)
    assert campaign(a) != campaign(b)


def test_missing_sidecar_does_not_start_parser(setup, monkeypatch):
    config, _, _ = setup
    next(Path(config["input_drive_dir"]).glob("*.sha256")).unlink()
    run = Mock(side_effect=AssertionError("no parser"))
    monkeypatch.setattr(runner, "run", run)
    with pytest.raises(ValueError, match="Missing"):
        export.operate(config,"smoke")
    run.assert_not_called()


def test_wrong_member_or_included_inventory_is_rejected(setup):
    config, _, _ = setup
    path = Path(config["input_drive_dir"]) / "cohort_0101_corpus.manifest.json"
    manifest = export.read(path)
    manifest["included_ids"] = ["2508.07872"]
    path.write_bytes(export.encoded(manifest))
    with pytest.raises(ValueError, match="identity/coverage"):
        export.operate(config, "smoke")


def test_non_multiple_batch_size_finishes_short_last_batch(setup, tmp_path):
    config, _, _ = setup
    config.update(end_cohort=103,batch_size=2)
    fixture_archive(tmp_path,102)
    fixture_archive(tmp_path,103)
    first = export.read(Path(export.operate(config,"next"))/"coverage.json")
    last = export.read(Path(export.operate(config,"next"))/"coverage.json")
    assert first["cohort_count"]==2 and last["cohort_count"]==1
    assert len(export.operate(config,"audit"))==3

def repack(corpus, archive):
    paper = next(corpus.iterdir())
    with tarfile.open(archive, "w:gz") as stream:
        stream.add(paper, arcname=paper.name)
    name = archive.name.removesuffix(".tar.gz")
    path = archive.with_name(name + ".manifest.json")
    manifest = export.read(path)
    manifest["archive_sha256"] = export.sha(archive)
    path.write_bytes(export.encoded(manifest))
    archive.with_name(name + ".tar.gz.sha256").write_text(export.sha(archive) + "  " + archive.name, encoding="utf8")


def test_fresh_archive_hash_does_not_replace_source_checksum_validation(setup, monkeypatch):
    config, _, corpus = setup
    next(corpus.glob("*/v1/main.tex")).write_text("corrupted source", encoding="utf8")
    repack(corpus, Path(config["input_drive_dir"]) / "cohort_0101_corpus.tar.gz")
    parse = Mock(side_effect=AssertionError("must not parse bad source"))
    monkeypatch.setattr(runner,"run",parse)
    with pytest.raises(ValueError, match="checksum"):
        export.operate(config,"smoke")
    parse.assert_not_called()
    assert not list((campaign(config) / "verified").glob("*.json"))


def test_wrong_paper_owner_is_rejected_even_with_valid_archive_hash(setup):
    config, _, corpus = setup
    for p in next(corpus.iterdir()).glob("*.json"):
        data=export.read(p)
        data["mssv"]="24120010"
        p.write_bytes(export.encoded(data))
    repack(corpus, Path(config["input_drive_dir"]) / "cohort_0101_corpus.tar.gz")
    with pytest.raises(ValueError, match="MSSV"):
        export.operate(config,"smoke")


def test_new_input_cohort_can_be_added_after_restart_without_changing_campaign(setup, tmp_path):
    config, _, _=setup
    config.update(end_cohort=102,batch_size=1)
    export.operate(config,"next")
    identity=(campaign(config)/"campaign.json").read_bytes()
    with pytest.raises(ValueError,match="Missing"):
        export.operate(config,"next")
    fixture_archive(tmp_path,102)
    export.operate(config,"next")
    assert (campaign(config)/"campaign.json").read_bytes()==identity
    assert len(export.operate(config,"audit"))==2

def test_notebook_pins_a_committed_helper_with_identical_source():
    import subprocess
    repo=Path(__file__).resolve().parents[1]
    nb=export.read(repo/"notebooks/m2/m2_team_colab_export.ipynb")
    source=next("".join(c["source"])for c in nb["cells"]if c["cell_type"]=="code" and "HELPER_COMMIT =" in "".join(c["source"]))
    tree=ast.parse(source)
    pins={n.targets[0].id:ast.literal_eval(n.value)for n in tree.body
          if isinstance(n,ast.Assign) and isinstance(n.targets[0],ast.Name)
          and n.targets[0].id in {"HELPER_COMMIT","PARSER_COMMIT"}}
    assert pins["HELPER_COMMIT"]!=pins["PARSER_COMMIT"]
    blob=subprocess.check_output(["git","-C",str(repo),"show",pins["HELPER_COMMIT"]+":scripts/m2_colab_export.py"])
    assert blob==(repo/"scripts/m2_colab_export.py").read_bytes()
    subprocess.run(["git","-C",str(repo),"diff","--exit-code",pins["PARSER_COMMIT"],"HEAD","--","src/m2"],check=True)


# Preflight-only fixtures: no call to runner.run, parsing, Drive or data APIs.
def notebook_preflight(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    nb = export.read(repo / "notebooks/m2/m2_team_colab_export.ipynb")
    source = next("".join(c["source"]) for c in nb["cells"] if c["cell_type"] == "code"
                  and "def require_input_files" in "".join(c["source"]))
    functions = [n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef)
                 and n.name in {"require_input_files", "active_input_cohorts", "run_export"}]
    import os, sys, subprocess
    ns = dict(Path=Path, INPUT_DRIVE_DIR=tmp_path / "archives", CAMPAIGN=tmp_path / "campaign",
              BATCH_SIZE=2, scope=dict(cohorts=[f"cohort_{n:04d}_corpus" for n in range(101,104)]),
              os=os, sys=sys, subprocess=subprocess)
    exec(compile(ast.Module(body=functions,type_ignores=[]),"<notebook-preflight>","exec"),ns)
    return ns


def test_input_preflight_missing_directory_reports_path_without_creating_it(tmp_path):
    ns=notebook_preflight(tmp_path)
    with pytest.raises(FileNotFoundError) as exc:
        ns["require_input_files"](["cohort_0101_corpus"])
    assert str(ns["INPUT_DRIVE_DIR"]) in str(exc.value)
    assert not ns["INPUT_DRIVE_DIR"].exists()
    for suffix in (".tar.gz", ".manifest.json", ".tar.gz.sha256"):
        assert str(ns["INPUT_DRIVE_DIR"] / ("cohort_0101_corpus" + suffix)) in str(exc.value)


@pytest.mark.parametrize("missing",[".tar.gz",".manifest.json",".tar.gz.sha256"])
def test_input_preflight_names_the_exact_missing_file(tmp_path,missing):
    ns=notebook_preflight(tmp_path);root=ns["INPUT_DRIVE_DIR"];root.mkdir()
    for suffix in (".tar.gz",".manifest.json",".tar.gz.sha256"):
        if suffix!=missing:(root/("cohort_0101_corpus"+suffix)).write_bytes(b"preflight-only fixture")
    with pytest.raises(FileNotFoundError) as exc:
        ns["require_input_files"](["cohort_0101_corpus"])
    assert str(root/("cohort_0101_corpus"+missing)) in str(exc.value)
    assert len(list(root.iterdir()))==2


def test_input_preflight_lists_all_six_missing_paths(tmp_path):
    ns=notebook_preflight(tmp_path);root=ns["INPUT_DRIVE_DIR"];root.mkdir()
    names=["cohort_0101_corpus","cohort_0102_corpus"]
    with pytest.raises(FileNotFoundError) as exc:ns["require_input_files"](names)
    for name in names:
        for suffix in (".tar.gz",".manifest.json",".tar.gz.sha256"):
            assert str(root/(name+suffix)) in str(exc.value)
    assert not list(root.iterdir())


def test_input_preflight_checks_only_the_active_batch_and_completed_audit(tmp_path):
    ns=notebook_preflight(tmp_path)
    assert ns["active_input_cohorts"]("smoke")==["cohort_0101_corpus"]
    assert ns["active_input_cohorts"]("next")==["cohort_0101_corpus","cohort_0102_corpus"]
    marker=ns["CAMPAIGN"] / "batches/0101-0102.json";marker.parent.mkdir(parents=True);marker.write_text("{}")
    assert ns["active_input_cohorts"]("next")==["cohort_0103_corpus"]
    assert ns["active_input_cohorts"]("audit")==["cohort_0101_corpus","cohort_0102_corpus"]


def test_input_preflight_blocks_launch_when_a_file_is_missing(tmp_path,monkeypatch):
    ns=notebook_preflight(tmp_path);ns["INPUT_DRIVE_DIR"].mkdir()
    launch=Mock(side_effect=AssertionError("No M2 invocation allowed"))
    monkeypatch.setattr(ns["subprocess"],"Popen",launch)
    with pytest.raises(FileNotFoundError):ns["run_export"]("next")
    launch.assert_not_called()


@pytest.mark.parametrize("student",["24120010","24120035","24120232","24120260"])
def test_input_preflight_default_path_is_shared_and_output_stays_isolated(student):
    repo=Path(__file__).resolve().parents[1]
    nb=export.read(repo/"notebooks/m2/m2_team_colab_export.ipynb")
    config=next("".join(c["source"]) for c in nb["cells"] if c["cell_type"]=="code")
    tree=ast.parse(config)
    for node in tree.body:
        if isinstance(node,ast.Assign) and isinstance(node.targets[0],ast.Name) and node.targets[0].id=="STUDENT_ID":
            node.value=ast.Constant(student)
    ast.fix_missing_locations(tree)
    ns={};exec(compile(tree,"<notebook-config>","exec"),ns)
    assert ns["INPUT_DRIVE_DIR"]==f"/content/drive/MyDrive/M1-pipeline-optimization-full/{student}/archives"
    assert ns["OUTPUT_DRIVE_DIR"]==f"/content/drive/MyDrive/ArxivGroup10/{student}/m2-exports"
