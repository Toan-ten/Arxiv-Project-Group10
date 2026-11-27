"""File-level SHA, publication provenance and path guards for external M1 BBL."""
import hashlib
import json
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from src.m2.latex import BblIntegrityError, Gatherer
from src.m2.parser import parse_versions
from src.m2.runner import run, validate_tables


def save_manifest(case):
    (case["paper"] / "manifest.json").write_text(json.dumps(case["manifest"]), encoding="utf8")


def change_source(case, command):
    path = case["source"] / "main.tex"
    path.write_text(r"\documentclass{article}\begin{document}Evidence \cite{k}."
                    + command + r"\end{document}", encoding="utf8")
    record = next(iter(case["manifest"]["versions"].values()))
    record["file_checksums"]["main.tex"] = hashlib.sha256(path.read_bytes()).hexdigest()
    save_manifest(case)


def change_index(case, transform):
    path = case["paper"] / "bbl" / case["policy"]["index_path"]
    index = json.loads(path.read_text())
    transform(index)
    path.write_text(json.dumps(index), encoding="utf8")
    case["policy"]["index_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    save_manifest(case)


def parse(case):
    result = parse_versions(case["aid"], [(1, case["source"])])
    validate_tables(result)
    return result


def test_verified_external_bbl_native_runner_and_resume(m2_external_bbl, tmp_path):
    case = m2_external_bbl()
    first = run(case["paper"].parent, tmp_path / "output")
    assert first["statuses"] == {"success": 1}
    root = Path(first["run_root"])
    rows = pq.read_table(root / "data/bibtex/part-00000.parquet").to_pylist()
    assert [(r["citation_key"], r["title"], r["year"]) for r in rows] == [("k", "Verified evidence", 2020)]
    assert run(case["paper"].parent, tmp_path / "output")["cached_papers"] == 1


def test_corrupt_bbl_cannot_reach_parquet_even_with_archive_hash(m2_external_bbl, tmp_path):
    case = m2_external_bbl()
    artifact = case["artifact"]
    artifact.write_bytes(artifact.read_bytes().replace(b"2020", b"2049"))
    # An archive/download checksum is not the expected SHA of this BBL file.
    case["manifest"]["versions"]["v1"]["archive_sha256"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
    save_manifest(case)
    result = run(case["paper"].parent, tmp_path / "output")
    assert result["statuses"] == {"parse_error": 1}
    assert "bbl_source_evidence_checksum_mismatch" in result["versions_detail"][0]["error"]
    rows = pq.read_table(Path(result["run_root"]) / "data/bibtex/part-00000.parquet").to_pylist()
    assert rows == []


@pytest.mark.parametrize("field,value", [("arxiv_id", "2508.92002"), ("version", "v2"), ("mssv", "24120232")])
def test_wrong_index_identity_cannot_supply_publication(m2_external_bbl, field, value, tmp_path):
    case = m2_external_bbl()
    change_index(case, lambda index: index.update({field: value}))
    result = run(case["paper"].parent, tmp_path / "output")
    assert result["statuses"] == {"parse_error": 1}
    assert "bbl_policy_index_mismatch" in result["versions_detail"][0]["error"]
    assert pq.read_table(Path(result["run_root"]) / "data/bibtex/part-00000.parquet").num_rows == 0


@pytest.mark.parametrize("target", ["../main.bbl", "C:/outside/main.bbl", "sub/../../main.bbl", r"..\main.bbl", r"\dynamic.bbl"])
def test_rejected_explicit_paths_never_use_basename_fallback(m2_external_bbl, target):
    case = m2_external_bbl()
    change_source(case, r"\input{" + target + "}")
    result = parse(case)
    record = result["versions"][0]
    assert record["status"] == "parse_error"
    assert any(s in record["error"] for s in ("Source escapes", "Unsafe source path", "Dynamic include", "Dynamic input filename"))
    assert not result["bibtex"]


def test_missing_subpath_does_not_take_unrelated_basename(m2_external_bbl):
    case = m2_external_bbl()
    change_source(case, r"\input{chapter/main.bbl}")
    result = parse(case)
    assert result["versions"][0]["status"] == "parse_error"
    assert "Missing compilation resource" in result["versions"][0]["error"]
    assert not result["bibtex"]


@pytest.mark.parametrize("name", ["main.bbl", "chapter/refs.bbl"])
def test_exact_verified_input_path_can_be_resolved(m2_external_bbl, name):
    case = m2_external_bbl(names=(name,))
    change_source(case, r"\input{" + name + "}")
    result = parse(case)
    assert result["versions"][0]["status"] == "success", result["versions"]
    assert result["bibtex"][0]["citation_key"] == "k"


def test_version_without_recorded_evidence_is_ignored_not_rejected(m2_external_bbl):
    """No bbl_policy means M1 recorded nothing for this version, not that it is corrupt."""
    case = m2_external_bbl()
    del case["manifest"]["versions"]["v1"]["bbl_policy"]
    save_manifest(case)
    result = parse(case)
    assert result["versions"][0]["status"] == "parse_error"
    assert "BBL" not in result["versions"][0]["error"]
    assert "bbl_policy" not in result["versions"][0]["error"]
    assert not result["bibtex"]


def test_missing_artifact_is_not_empty_verified_bibliography(m2_external_bbl):
    case = m2_external_bbl()
    case["artifact"].unlink()
    result = parse(case)
    assert result["versions"][0]["status"] == "parse_error"
    assert "bbl_source_evidence_checksum_mismatch" in result["versions"][0]["error"]
    assert not result["bibtex"]


def test_index_checksum_must_match(m2_external_bbl):
    case = m2_external_bbl()
    path = case["paper"] / "bbl" / case["policy"]["index_path"]
    path.write_bytes(path.read_bytes() + b" ")
    result = parse(case)
    assert "bbl_policy_index_checksum_mismatch" in result["versions"][0]["error"]
    assert not result["bibtex"]


@pytest.mark.parametrize("field", ["source_evidence_path", "derived_bib_path"])
def test_index_cannot_redirect_to_another_publication_or_version(m2_external_bbl, field):
    case = m2_external_bbl()
    case["policy"]["items"][0][field] = "2508-92002/v2/source_bbl/main.bbl"
    change_index(case, lambda index: index.update(items=case["policy"]["items"]))
    result = parse(case)
    assert result["versions"][0]["status"] == "parse_error"
    assert "not bound to this paper/version" in result["versions"][0]["error"]
    assert not result["bibtex"]


def test_ambiguous_verified_artifacts_remain_rejected(m2_external_bbl):
    case = m2_external_bbl(names=("a/main.bbl", "b/main.bbl"))
    result = parse(case)
    assert result["versions"][0]["status"] == "parse_error"
    assert not result["bibtex"]


def test_hash_rechecked_on_actual_bbl_read(m2_external_bbl):
    case = m2_external_bbl()
    gatherer = Gatherer(case["source"], arxiv_id=case["aid"], version=1)
    paths = gatherer.recorded_bbl()
    paths[0].write_bytes(paths[0].read_bytes().replace(b"2020", b"2049"))
    with pytest.raises(BblIntegrityError, match="changed after verification"):
        gatherer.read(paths[0])


def test_standalone_parser_uses_expected_identity_not_manifest_only(m2_external_bbl):
    case = m2_external_bbl()
    result = parse_versions("2508.99999", [(1, case["source"])])
    assert result["versions"][0]["status"] == "parse_error"
    assert "identity mismatch" in result["versions"][0]["error"]
    assert not result["bibtex"]


def test_unlisted_bbl_cannot_replace_verified_entry(m2_external_bbl):
    case = m2_external_bbl()
    extra = case["artifact"].parent / "unlisted.bbl"
    extra.write_bytes(case["artifact"].read_bytes().replace(b"2020", b"2049"))
    result = parse(case)
    assert result["versions"][0]["status"] == "success"
    assert [row["year"] for row in result["bibtex"]] == [2020]


def test_legacy_identity_cannot_borrow_modern_publication_artifacts(m2_external_bbl):
    # Current M1 artifact schema only has new-style publication folder keys.
    case = m2_external_bbl()
    result = parse_versions("hep-th/9901001", [(1, case["source"])])
    assert result["versions"][0]["status"] == "parse_error"
    assert "identity mismatch" in result["versions"][0]["error"]
    assert not result["bibtex"]


def test_artifact_symlink_is_rejected(m2_external_bbl):
    case = m2_external_bbl()
    artifact = case["artifact"]
    other = artifact.with_name("other.dat")
    other.write_bytes(artifact.read_bytes())
    artifact.unlink()
    try:
        artifact.symlink_to(other)
    except OSError as exc:
        pytest.skip("Symlink creation unavailable: " + str(exc.winerror if hasattr(exc, "winerror") else exc.errno))
    result = parse(case)
    assert result["versions"][0]["status"] == "parse_error"
    assert "Symlink" in result["versions"][0]["error"]
    assert not result["bibtex"]
