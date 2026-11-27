"""Nocite canonicalization: same-version aliases, wildcard, and real reproduction."""
import json
from pathlib import Path
import socket

import pytest

from src.m2.latex import normalize, citation_keys
from src.m2.parser import parse_versions
from src.m2 import runner
from scripts import m2_colab_recovery as recovery


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("No network in nocite recovery tests")
    monkeypatch.setattr(socket.socket, "connect", blocked)


@pytest.mark.parametrize("source,expected", [
    (r"\nocite{k}", r"\nocite{canonical}"),
    (r"\nocite{k, other,k}", r"\nocite{canonical,other_canonical}"),
    (r"\cite{k} \nocite{k}", r"\cite{canonical} \nocite{canonical}"),
    (r"\nocite{*}", r"\nocite{*}"),
    (r"\nocite{*,k}", r"\nocite{*,canonical}"),
    (r"\citep[see][p.~2]{k}", r"\citep[see][p. 2]{canonical}"),
])
def test_normalize_explicit_and_wildcard(source, expected):
    assert normalize(source, {"k": "canonical", "other": "other_canonical", "*": "wrong"}) == expected


def test_unmapped_key_is_not_invented():
    assert normalize(r"\nocite{unknown}", {"k": "canonical"}) == r"\nocite{unknown}"


def test_version_collision_keeps_bibliography_and_evidence(tmp_path):
    roots = []
    for v, title in [(1, "First work"), (2, "Different work")]:
        path = tmp_path / f"v{v}"; path.mkdir()
        (path / "main.tex").write_text(
            r"\documentclass{article}\begin{document}\section{Evidence}"
            r"Scientific $x=y$ \cite{k}.\nocite{k,other}\bibliography{refs}\end{document}", encoding="utf8")
        (path / "refs.bib").write_text(
            f"@article{{k,title={{{title}}},author={{Doe, Jane}},year={{2025}}}}\n"
            "@article{other,title={Unchanged},year={2024}}", encoding="utf8")
        roots.append((v, path))
    result = parse_versions("2502.09725", roots)
    assert [r["status"] for r in result["versions"]] == ["success", "success"]
    nodes = {r["element_id"]: r for r in result["elements"]}
    for record in result["versions"]:
        own = {e["key"] for e in record["bibliography_evidence"]}
        target = record["citation_aliases"]["k"]
        assert (target == "k") == (record["version"] == 1)
        text = " ".join(nodes[e["element_id"]]["content"] or "" for e in result["hierarchy"] if e["version"] == record["version"])
        assert r"\cite{" + target + "}" in text
        assert r"\nocite{" + target + ",other}" in text
        assert "Scientific $x=y$" in text
        assert own == {"k", "other"}
    assert len(result["bibtex"]) == 3
    runner.validate_tables(result)


REAL = Path(r"D:\NM_KHDL\m1_189_extracted\cohort_0115_corpus\2502-09725")
BASELINE = Path(r"D:\NM_KHDL\m2_cohort0115_diagnosis\pinned_real_payload.json")


@pytest.mark.skipif(not REAL.is_dir(), reason="Exact read-only local cohort 0115 source is not installed")
def test_exact_cohort_0115_source_and_payload():
    manifest = json.loads((REAL / "manifest.json").read_text(encoding="utf8"))
    roots = []
    for v, rec in manifest["versions"].items():
        version = int(v[1:])
        assert runner.inspect_source(REAL, version, rec) == "ready"
        roots.append((version, REAL / v))
    result = parse_versions("2502.09725", roots)
    assert [r["status"] for r in result["versions"]] == ["success", "success"]
    key = "bengio2014representationlearningreviewnew"
    canonical = key + "__6bcf59f320"
    assert result["versions"][1]["citation_aliases"][key] == canonical
    nodes = {e["element_id"]: e for e in result["elements"]}
    texts = {v: " ".join(nodes[e["element_id"]]["content"] or "" for e in result["hierarchy"] if e["version"] == v) for v in (1, 2)}
    assert r"\cite{" + canonical + "}" in texts[2]
    assert canonical in next(n for n in texts[2].split("\\nocite{")[1].split("}")[:1])
    assert not any(key in citation_keys(nodes[e["element_id"]]["content"] or "")
                   for e in result["hierarchy"] if e["version"] == 2)
    if BASELINE.is_file():
        old = json.loads(BASELINE.read_text(encoding="utf8"))
        assert result["bibtex"] == old["bibtex"]
        assert result["versions"] == old["versions"]
        old_nodes = {n["element_id"]: n for n in old["elements"]}
        for version in (1, 2):
            old_edges = [e for e in old["hierarchy"] if e["version"] == version]
            new_edges = [e for e in result["hierarchy"] if e["version"] == version]
            assert len(old_edges) == len(new_edges)
            if version == 1:
                assert old_edges == new_edges
                assert [old_nodes[e["element_id"]] for e in old_edges] == [nodes[e["element_id"]] for e in new_edges]
            else:
                # IDs/parent IDs legitimately change with changed normalized content.
                for before, after in zip(old_edges, new_edges):
                    original, updated = old_nodes[before["element_id"]], nodes[after["element_id"]]
                    expected = original["content"]
                    if r"\nocite{" in (expected or ""):
                        expected = expected.replace(key, canonical)
                    assert original["kind"] == updated["kind"]
                    assert updated["content"] == expected
                    assert before["version"] == after["version"] == 2
