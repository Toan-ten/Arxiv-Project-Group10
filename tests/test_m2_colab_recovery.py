"""Offline recovery integration, real native checkpoints/Parquet, no Drive access."""
import io
from collections import Counter
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tarfile

import pytest

from scripts import m2_colab_export as export
from scripts import m2_colab_recovery as recovery
from src.m2 import runner

REPO = Path(__file__).resolve().parents[1]
HELPER = "0c0b02aea2b235f95e3984dbb85063aa36f41a59"
BASE = subprocess.check_output(["git", "-C", str(REPO), "rev-parse", "HEAD"], text=True).strip()


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Network forbidden")
    monkeypatch.setattr(socket.socket, "connect", blocked)


@pytest.fixture(scope="session")
def legacy_checkout(tmp_path_factory):
    root = tmp_path_factory.mktemp("exact-pinned-parser")
    archive = subprocess.check_output(["git", "-C", str(REPO), "archive", recovery.OLD_COMMIT, "src", "config/group10_id_ranges.json"])
    with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
        stream.extractall(root, filter="data")
    (root / "scripts").mkdir()
    (root / "scripts/__init__.py").write_bytes(b"")
    (root / "scripts/m2_colab_export.py").write_bytes(subprocess.check_output(
        ["git", "-C", str(REPO), "show", HELPER + ":scripts/m2_colab_export.py"]))
    return root


def config(tmp_path, start=101, end=102, size=5):
    return dict(repo=str(REPO), student_id="24120232", start_cohort=start, end_cohort=end,
        batch_size=size, input_drive_dir=str(tmp_path / "archives"), scratch=str(tmp_path / "scratch"),
        output_drive_dir=str(tmp_path / "old-export"), hf_repo_id="member/m2-data", helper_commit=HELPER,
        recovery_root=str(tmp_path / "new-export"), parser_commit=BASE,
        parser_fingerprint=recovery.fingerprint(REPO), allow_uncommitted=True)


def write_archives(conf, tmp_path, *, broken=102):
    archives = Path(conf["input_drive_dir"]); archives.mkdir()
    scope = export.scope_for(conf)
    for name in scope["cohorts"]:
        n = int(name[7:11]); ids = export.expected_ids(name, scope)
        corpus = tmp_path / "corpus-fixture" / name
        included = ids[:2] if n == broken else ids[:1]
        for i, aid in enumerate(included):
            paper = corpus / aid.replace(".", "-")
            records = {}
            for v in (1, 2):
                path = paper / f"v{v}"; path.mkdir(parents=True)
                title = f"Different work {v}" if n == broken and i == 0 else "Stable work"
                body = (r"\documentclass{article}\begin{document}\section{Evidence}"
                        r"Scientific $x=y$ \cite{k}." + (r"\nocite{k}" if n == broken and i == 0 and v == 2 else "") +
                        r"\bibliography{refs}\end{document}")
                (path / "main.tex").write_text(body, encoding="utf8")
                (path / "refs.bib").write_text(f"@article{{k,title={{{title}}},author={{Doe, Jane}},year={{2025}}}}", encoding="utf8")
                files = ["main.tex", "refs.bib"]
                records[f"v{v}"] = dict(status="success", files=files, file_checksums={f: export.sha(path / f) for f in files})
            metadata = dict(contract_version=2, arxiv_id=aid, mssv=scope["owner"], versions=records)
            for f in ("manifest.json", "metadata.json"):
                (paper / f).write_bytes(export.encoded(metadata))
        archive = archives / (name + ".tar.gz")
        with tarfile.open(archive, "w:gz") as stream:
            for paper in sorted(corpus.iterdir()):
                stream.add(paper, arcname=paper.name)
        manifest = dict(cohort_number=n, archive_filename=archive.name, archive_sha256=export.sha(archive),
            expected_ids=ids, expected_count=100, included_ids=included, included_count=len(included), omitted_count=100-len(included))
        (archives / (name + ".manifest.json")).write_bytes(export.encoded(manifest))
        (archives / (name + ".tar.gz.sha256")).write_text(export.sha(archive) + "  " + archive.name, encoding="utf8")


def old_campaign(conf, legacy_checkout, *, broken=102, packaged=1):
    old = dict(conf, repo=str(legacy_checkout), scratch=str(Path(conf["scratch"]).with_name("old-scratch")))
    command = r'''
import json,sys,socket
from pathlib import Path
from scripts import m2_colab_export as e
from src.m2 import runner
cfg=json.loads(sys.argv[1]);broken=int(sys.argv[2]);count=int(sys.argv[3])
socket.socket.connect=lambda *a,**k: (_ for _ in ()).throw(AssertionError('No network'))
e.verify_runtime=lambda _:runner
original=runner.subprocess.check_output
def git(args,*a,**kw):
 if args[:2]==['git','-c'] and args[-2:]==['rev-parse','HEAD']:return e.PARSER_COMMIT
 if args[:2]==['git','-c'] and args[-2:]==['status','--porcelain']:return ''
 return original(args,*a,**kw)
runner.subprocess.check_output=git
c=e.runtime_context(cfg)
with e.writer(c['campaign']):
 e.immutable(c['campaign']/'campaign.json',e.encoded(c['identity']))
 audits=[];names=[]
 for name in c['scope']['cohorts']:
  try:
   result=e.cohort_run(name,c['archives'],c['scratch'],c['campaign'],runner,c['scope'])
  except ValueError as exc:
   if int(name[7:11])!=broken or str(exc)!='Citation lacks bibliography evidence in its own version':raise
   continue
  if len(names)<count:names.append(name);audits.append(result)
 if names:
  package=e.export_batch(names,audits,c['campaign'],c['scope'],dict(effective_batch_size=count,helper_commit=cfg['helper_commit'],helper_sha256=e.sha(e.__file__)))
  label=names[0][7:11]+'-'+names[-1][7:11]
  e.immutable(c['campaign']/'batches'/(label+'.json'),e.encoded(dict(cohorts=names,package=str(package),coverage_sha256=e.sha(package/'coverage.json'))))
'''
    env = os.environ | dict(PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(legacy_checkout), PYTHONUTF8="1")
    process = subprocess.run([sys.executable, "-X", "utf8", "-B", "-c", command, json.dumps(old), str(broken), str(packaged)],
        cwd=legacy_checkout, env=env, text=True, capture_output=True)
    assert process.returncode == 0, process.stdout + process.stderr
    conf["old_campaign"] = str(export.campaign_path(old, export.scope_for(old)))
    return Path(conf["old_campaign"])


def hashes(root):
    return {p.relative_to(root).as_posix(): export.sha(p) for p in root.rglob("*") if p.is_file()}


@pytest.fixture
def campaign(tmp_path, legacy_checkout):
    conf = config(tmp_path)
    write_archives(conf, tmp_path)
    old = old_campaign(conf, legacy_checkout)
    return conf, old


def test_completed_reuse_partial_reparse_resume_and_package(campaign, monkeypatch):
    conf, old = campaign
    before = hashes(old)
    assert not (old / "verified/cohort_0102_corpus.json").exists()
    report = recovery.operate(conf, "migrate")
    root = Path(report["campaign"])
    assert report["cohorts"] == ["cohort_0101_corpus"]
    original_parse = runner.parse_versions
    calls = []
    def counting(aid, *args, **kwargs):
        calls.append(aid)
        return original_parse(aid, *args, **kwargs)
    monkeypatch.setattr(runner, "parse_versions", counting)
    package = Path(recovery.operate(conf, "next"))
    expected = export.expected_ids("cohort_0102_corpus", export.scope_for(conf))[0]
    assert calls == [expected]  # Only the bad publication; both its versions together.
    record = export.read(root / "verified/cohort_0102_corpus.json")
    assert record["audit"]["statuses"] == {"success": 4}
    assert len(record["excluded_legacy_checkpoints"]) == 1
    modes = Counter(o["mode"] for o in record["parser_origins"].values())
    assert modes == {"verified_legacy_reuse": 1, "parsed_by_new_runtime": 1}
    coverage = export.read(package / "coverage.json")
    assert "parser_commit" not in coverage  # No false uniform parser claim.
    assert coverage["partial_coverage"]  # 99/98 absent M1 input IDs, never called full coverage.
    assert hashes(old) == before
    monkeypatch.setattr(runner, "run", lambda *a, **k: pytest.fail("Completed results must not parse"))
    assert recovery.operate(conf | {"batch_size": 50}, "next") == dict(campaign=str(root), cohorts=[], verified=0)
    assert recovery.operate(conf, "audit")["verified"] == 2
    assert hashes(old) == before



def test_14_completed_preserve_bytes_and_packaging_status(tmp_path, legacy_checkout, monkeypatch):
    conf = config(tmp_path, 101, 114, 50)
    write_archives(conf, tmp_path, broken=-1)
    old = old_campaign(conf, legacy_checkout, broken=-1, packaged=5)
    before = hashes(old)
    assert len(list((old / "verified").glob("*.json"))) == 14
    assert len(export.completed_batches(old, export.scope_for(conf))) == 1
    monkeypatch.setattr(runner, "run", lambda *a, **k: pytest.fail("No completed cohort may be reparsed"))
    report = recovery.operate(conf, "migrate")
    assert report["verified"] == 14
    package = Path(recovery.operate(conf, "package"))
    coverage = export.read(package / "coverage.json")
    assert len(coverage["cohorts"]) == 14
    assert all(r["mode"] == "verified_legacy_reuse" for r in coverage["per_cohort_provenance"].values())
    assert hashes(old) == before
    assert recovery.operate(conf, "audit")["verified"] == 14


@pytest.mark.parametrize("damage", ["parquet", "paper", "package", "catalog"])
def test_failed_migration_never_changes_old_artifacts(campaign, damage):
    conf, old = campaign
    if damage == "parquet":
        path = next((old / "results/cohort_0101_corpus").glob("run-*/data/bibtex/*.parquet"))
    elif damage == "paper":
        path = next((old / "results/cohort_0101_corpus").glob("run-*/papers/*/result.json"))
    elif damage == "package":
        path = next((old / "upload").glob("*/data/bibtex/*.parquet"))
    else:
        path = old / "inputs/cohort_0101_corpus.json"
    path.write_bytes(b"corrupted fixture")
    before = hashes(old)
    with pytest.raises((ValueError, KeyError)):
        recovery.operate(conf, "migrate")
    assert hashes(old) == before
    assert not list(Path(conf["recovery_root"]).glob("*/verified/*.json"))


def test_interrupted_recovery_keeps_seed_plan_and_resumes(campaign, monkeypatch):
    conf, old = campaign
    before = hashes(old)
    recovery.operate(conf, "migrate")
    actual = runner.run
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt("fixture Stop")
    monkeypatch.setattr(runner, "run", interrupted)
    with pytest.raises(KeyboardInterrupt):
        recovery.operate(conf, "next")
    root = next(Path(conf["recovery_root"]).glob("recovery-*"))
    assert list(root.glob("results/cohort_0102_corpus/run-*/recovery-seeds.json"))
    assert not (root / "verified/cohort_0102_corpus.json").exists()
    monkeypatch.setattr(runner, "run", actual)
    assert Path(recovery.operate(conf, "next")).is_dir()
    assert recovery.operate(conf, "audit")["verified"] == 2
    assert hashes(old) == before


def test_imported_evidence_mutation_is_rejected(campaign):
    conf, old = campaign
    report = recovery.operate(conf, "migrate")
    path = next((old / "results/cohort_0101_corpus").glob("run-*/papers/*/result.json"))
    path.write_bytes(b"bad")
    with pytest.raises(ValueError, match="evidence changed|origin differs"):
        recovery.operate(conf, "audit")


def test_wrong_version_bibliography_evidence_rejected(campaign):
    conf, old = campaign
    path = next((old / "results/cohort_0101_corpus").glob("run-*/papers/*/result.json"))
    payload = export.read(path)["payload"]
    payload["versions"][0]["bibliography_evidence"] = []
    corpus = next((Path(conf["scratch"]).with_name("old-scratch") / "24120232" / "0101-0102" / "cohort_0101_corpus").glob("*"))
    rows = runner.selection(corpus.parent)
    with pytest.raises(ValueError, match="own version"):
        recovery.payload_audit(payload, rows)


def test_runtime_rejects_silent_fingerprint_change_and_dirty_release(monkeypatch):
    original = recovery.subprocess.check_output
    def dirty(args, *a, **kw):
        if args[-2:] == ["status", "--porcelain"]:
            return " M src/m2/latex.py"
        return original(args, *a, **kw)
    monkeypatch.setattr(recovery.subprocess, "check_output", dirty)
    with pytest.raises(ValueError, match="fingerprint mismatch"):
        recovery.runtime(REPO, BASE, "0" * 64, allow_uncommitted=True)
    with pytest.raises(ValueError, match="approved parser release"):
        recovery.runtime(REPO, BASE, recovery.fingerprint(REPO))


def test_recovery_audit_preserves_every_legacy_guard():
    import ast
    import inspect
    original = ast.parse(inspect.getsource(export.audit)).body[0]
    candidate = ast.parse(inspect.getsource(recovery.audit_attempt)).body[0]
    candidate.name = original.name
    candidate.args = original.args
    class Pin(ast.NodeTransformer):
        def visit_Name(self, node):
            if node.id == "expected_parser_commit":
                return ast.copy_location(ast.Name(id="PARSER_COMMIT", ctx=node.ctx), node)
            return node
    candidate = Pin().visit(candidate)
    assert ast.dump(original, include_attributes=False) == ast.dump(candidate, include_attributes=False)


def test_restart_after_native_write_never_reparses_valid_payload(campaign, monkeypatch):
    conf, old = campaign
    before = hashes(old)
    recovery.operate(conf, "migrate")
    original = recovery.audit_native
    def stop_after_write(path, rows, commit, *args):
        if commit != recovery.OLD_COMMIT:
            raise KeyboardInterrupt("Stop after native write")
        return original(path, rows, commit, *args)
    monkeypatch.setattr(recovery, "audit_native", stop_after_write)
    with pytest.raises(KeyboardInterrupt):
        recovery.operate(conf, "next")
    monkeypatch.setattr(recovery, "audit_native", original)
    monkeypatch.setattr(runner, "parse_versions", lambda *a, **k: pytest.fail("Native immutable results should resume"))
    package = Path(recovery.operate(conf, "next"))
    assert (package / "READY.json").is_file()
    assert hashes(old) == before


def test_changed_scope_or_overlapping_recovery_refused(campaign):
    conf, old = campaign
    with pytest.raises(ValueError, match="identity/provenance differs"):
        recovery.operate(conf | {"end_cohort": 103}, "migrate")
    with pytest.raises(ValueError, match="must be separate"):
        recovery.operate(conf | {"recovery_root": str(old / "unsafe")}, "migrate")


def test_active_old_writer_refused(campaign):
    conf, old = campaign
    lock = old / ".export-writer.lock"
    lock.write_text("fixture active writer", encoding="utf8")
    before = hashes(old)
    with pytest.raises(ValueError, match="writer lock"):
        recovery.operate(conf, "migrate")
    assert hashes(old) == before


def test_resume_rejects_unapproved_origin_in_marker(campaign):
    conf, old = campaign
    report = recovery.operate(conf, "migrate")
    marker = Path(report["campaign"]) / "verified/cohort_0101_corpus.json"
    record = export.read(marker)
    next(iter(record["parser_origins"].values()))["parser_fingerprint"] = "0" * 64
    marker.write_bytes(export.encoded(record))
    with pytest.raises(ValueError, match="Unapproved publication parser origin"):
        recovery.operate(conf, "audit")


def test_preflight_and_dry_run_do_not_write_campaigns_or_parse(campaign, monkeypatch):
    conf, old = campaign
    conf = conf | {"pilot_cohort": 102}
    before = hashes(old)
    monkeypatch.setattr(runner, "run", lambda *a, **k: pytest.fail("Dry-run cannot parse"))
    first = recovery.operate(conf, "preflight")
    assert first["ready"] and first["can_migrate"] == ["cohort_0101_corpus"]
    pilot = next(r for r in first["cohorts"] if r["cohort"] == "cohort_0102_corpus")
    assert pilot["status"] == "PILOT_REPARSE_REQUIRED"
    assert pilot["reusable_papers"] == 1 and len(pilot["rejected_checkpoints"]) == 1
    assert not Path(conf["recovery_root"]).exists()
    second = recovery.operate(conf, "dry-run")
    assert first == second
    assert hashes(old) == before


def test_pilot_is_exactly_one_cohort_then_gates_continuation(campaign, monkeypatch):
    conf, old = campaign
    conf = conf | {"pilot_cohort": 102, "batch_size": 50}
    before = hashes(old)
    with pytest.raises(ValueError, match="earlier completed"):
        recovery.operate(conf, "pilot")
    recovery.operate(conf, "migrate")
    recovery.operate(conf, "package")
    with pytest.raises(ValueError, match="Pilot must be verified"):
        recovery.operate(conf, "continue")
    actual = runner.run
    calls = []
    def tracked(corpus, *a, **k):
        calls.append(Path(corpus).name)
        return actual(corpus, *a, **k)
    monkeypatch.setattr(runner, "run", tracked)
    recovery.operate(conf, "pilot")
    assert calls == ["cohort_0102_corpus"]
    root = next(Path(conf["recovery_root"]).glob("recovery-*"))
    before_new = hashes(root)
    assert recovery.operate(conf, "audit-pilot")["cohorts"] == ["cohort_0102_corpus"]
    assert hashes(root) == before_new  # Audit does not even write a lock.
    assert recovery.operate(conf, "continue")["cohorts"] == []
    assert hashes(old) == before


def test_production_writes_require_fresh_preflight_receipt(campaign, monkeypatch):
    conf, old = campaign
    conf = conf | {"pilot_cohort": 102, "allow_uncommitted": False}
    actual = recovery.runtime
    def clean_release(repo, commit, fingerprint, **kwargs):
        result = actual(repo, commit, fingerprint, allow_uncommitted=True)
        return result | {"git_dirty": False, "development_only": False}
    monkeypatch.setattr(recovery, "runtime", clean_release)
    before = hashes(old)
    proof = recovery.operate(conf, "preflight")
    with pytest.raises(ValueError, match="approve its current token"):
        recovery.operate(conf, "migrate")
    assert not Path(conf["recovery_root"]).exists()
    recovery.operate(conf | {"approved_preflight_token": proof["preflight_token"]}, "migrate")
    path = next((old / "results/cohort_0101_corpus").glob("run-*/papers/*/result.json"))
    path.write_bytes(b"fixture changed after approval")
    changed = hashes(old)
    with pytest.raises(ValueError, match="approve its current token"):
        recovery.operate(conf | {"approved_preflight_token": proof["preflight_token"]}, "package")
    assert hashes(old) == changed
    assert before != changed


def test_hard_interruption_lock_is_reported_without_cleanup(campaign):
    conf, old = campaign
    conf = conf | {"pilot_cohort": 102}
    report = recovery.operate(conf, "migrate")
    root = Path(report["campaign"])
    (root / ".export-writer.lock").write_text("hard-stop fixture", encoding="utf8")
    before, before_old = hashes(root), hashes(old)
    proof = recovery.operate(conf, "preflight")
    assert not proof["ready"] and len(proof["writer_locks"]) == 1
    assert hashes(root) == before and hashes(old) == before_old


def test_missing_old_completed_marker_is_not_silently_reparsed(campaign, monkeypatch):
    conf, old = campaign
    # Model a broader original assignment without fabricating missing completion.
    conf = conf | {"pilot_cohort": 102}
    marker = old / "verified/cohort_0101_corpus.json"
    # The test fixture removes its package first; production code never removes it.
    monkeypatch.setattr(export, "completed_batches", lambda *args: [])
    marker.rename(marker.with_suffix(".fixture-missing"))
    before = hashes(old)
    proof = recovery.operate(conf, "preflight")
    assert not proof["ready"]
    assert "Required completed marker is missing" in proof["cohorts"][0]["error"]
    assert hashes(old) == before


def test_interrupted_marker_publish_preserves_old_and_resumes(campaign, monkeypatch):
    conf, old = campaign
    before = hashes(old)
    actual = export.os.replace
    def failed_publish(source, target):
        if Path(target).parent.name == "verified":
            raise OSError("fixture rename failed before completion marker")
        return actual(source, target)
    monkeypatch.setattr(export.os, "replace", failed_publish)
    with pytest.raises(OSError, match="rename failed"):
        recovery.operate(conf, "migrate")
    root = next(Path(conf["recovery_root"]).glob("recovery-*"))
    assert not list((root / "verified").glob("*.json"))
    assert list((root / "verified").glob("*.tmp"))  # Preserve interrupted-write evidence.
    assert hashes(old) == before
    monkeypatch.setattr(export.os, "replace", actual)
    assert recovery.operate(conf, "migrate")["verified"] == 1
    assert hashes(old) == before


def test_native_attempt_cannot_redirect_to_another_root(campaign, tmp_path):
    conf, old = campaign
    marker = export.read(old / "verified/cohort_0101_corpus.json")
    attempt = export.read(marker["attempt_path"])
    fake = tmp_path / "other" / "attempts" / "redirect.json"
    fake.parent.mkdir(parents=True)
    fake.write_bytes(export.encoded(attempt))
    with pytest.raises(ValueError, match="redirects to another run root"):
        recovery.audit_native(fake, [], recovery.OLD_COMMIT,
                              attempt["provenance"]["parser_fingerprint"], {})


def test_legacy_native_lock_also_blocks_read_only_preflight(campaign):
    conf, old = campaign
    conf = conf | {"pilot_cohort": 102}
    native = next((old / "results/cohort_0102_corpus").glob("run-*"))
    (native / ".writer.lock").write_text("fixture interrupted native writer", encoding="utf8")
    before = hashes(old)
    proof = recovery.operate(conf, "preflight")
    assert not proof["ready"]
    assert "Legacy native writer lock exists" in proof["cohorts"][1]["error"]
    assert hashes(old) == before
