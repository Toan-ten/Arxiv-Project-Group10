"""Shared content-integrity qualification, retained from the branch comparison."""
import json
import hashlib
from pathlib import Path
import time
from unittest.mock import patch

import pytest

from src.m2.parser import parse_versions
from src.m2.runner import validate_tables
from src.m2 import runner
from src.m2.execution import call_with_timeout


CASES = json.loads((Path(__file__).parent / "fixtures/m2_reconcile_safe.json").read_text(encoding="utf-8"))
# A clear rejection is safer than pretending to interpret dynamic TeX or local
# environment definitions which the existing gatherer does not expand.
SAFE_REJECTIONS = {
    "futurelet_execution", "primitive_eqref_override", "local_renew_SCfigure",
    "local_renew_tikzcd", "font_local_override",
}


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
def test_common_content_integrity(tmp_path, case):
    source = case.get("source") or (
        r"\documentclass{" + case.get("documentclass", "article") + "}"
        + case.get("preamble", "") + r"\begin{document}" + case["body"] + r"\end{document}"
    )
    (tmp_path / "main.tex").write_text(source, encoding="utf-8")
    (tmp_path / "r.bib").write_text("@article{k,title={Evidence},author={Ada Lovelace},year={2025}}", encoding="utf-8")
    result = parse_versions("2508.00001", [(1, tmp_path)])
    validate_tables(result)
    record = result["versions"][0]
    if case.get("expect_reject") or case["name"] in SAFE_REJECTIONS:
        assert record["status"] == "parse_error", record
        assert record["error"]
        assert not result["elements"] and not result["hierarchy"]
        return
    assert record["status"] == "success", record
    text = " ".join(n["content"] for n in result["elements"])
    for value in case.get("contains", []):
        assert value in text
    for value in case.get("not_contains", []):
        assert value not in text
    if case.get("kind"):
        assert any(n["kind"] == case["kind"] for n in result["elements"])
    if case.get("relation"):
        child, parent = case["relation"]
        nodes = {n["content"]: n for n in result["elements"]}
        parents = {e["element_id"]: e["parent_id"] for e in result["hierarchy"]}
        assert parents[nodes[child]["element_id"]] == nodes[parent]["element_id"]
    if case["name"] == "tikzstyle_configuration":
        assert "=[draw]" not in text


def test_captionof_star_retains_structural_caption_and_citation(tmp_path):
    (tmp_path / "main.tex").write_text(
        r"\documentclass{article}\usepackage{caption}\begin{document}"
        r"\captionof*{figure}{Scientific $x=y$ \cite{k}.}After.\bibliography{r}\end{document}", encoding="utf-8")
    (tmp_path / "r.bib").write_text("@article{k,title={Evidence}}", encoding="utf-8")
    result = parse_versions("2508.00001", [(1, tmp_path)])
    assert result["versions"][0]["status"] == "success", result["versions"]
    nodes = {n["element_id"]: n for n in result["elements"]}
    captions = {key for key, n in nodes.items() if n["content"] == "figure caption"}
    assert captions
    assert any(e["parent_id"] in captions and r"\cite{k}" in nodes[e["element_id"]]["content"] for e in result["hierarchy"])
    assert result["bibtex"][0]["citation_key"] == "k"


def test_portable_worker_returns_and_terminates_a_timed_out_child():
    assert call_with_timeout(sum, ([2, 3],), 10) == 5
    with pytest.raises(TimeoutError):
        call_with_timeout(time.sleep, (30,), 0.1)
    # No global alarm was changed, and a later independent worker still works.
    assert call_with_timeout(sum, ([4, 5],), 10) == 9


def _corpus_with_two_versions(tmp_path):
    corpus = tmp_path / "corpus"
    paper = corpus / "2508-00001"
    identity = dict(contract_version=2, arxiv_id="2508.00001", mssv="24120010")
    versions = {}
    for version in (1, 2):
        root = paper / f"v{version}"
        root.mkdir(parents=True)
        files = {"main.tex": (r"\documentclass{article}\begin{document}Shared scientific content $x=y$ "
                              + rf"\cite{{key{version}}}.\bibliography{{refs}}\end{{document}}"),
                 "refs.bib": f"@article{{key{version},title={{Same evidence}},author={{Ada Lovelace}},year={{2025}}}}"}
        checksums = {}
        for name, text in files.items():
            data = text.encode("utf-8")
            (root / name).write_bytes(data)
            checksums[name] = hashlib.sha256(data).hexdigest()
        versions[f"v{version}"] = dict(status="success", files=list(files), file_checksums=checksums)
    (paper / "metadata.json").write_text(json.dumps(identity), encoding="utf-8")
    (paper / "manifest.json").write_text(json.dumps(dict(identity, versions=versions)), encoding="utf-8")
    return corpus


def test_timeout_snapshot_is_retryable_preserves_good_version_and_does_not_freeze_parquet(tmp_path):
    corpus = _corpus_with_two_versions(tmp_path)
    output = tmp_path / "output"

    def first_executor(version, function, arguments):
        if version == 2:
            raise TimeoutError("transient fixture timeout")
        return function(*arguments)

    with patch.object(runner, "VersionExecutor", return_value=first_executor):
        first = runner.run(corpus, output)
    root = Path(first["run_root"])
    assert first["retryable"] and not first["all_versions_processed"]
    assert first["statuses"] == {"success": 1, "parse_error": 1}
    assert not (root / "papers/2508-00001/result.json").exists()
    snapshot = next((root / "papers/2508-00001/partial").glob("*.json"))
    snapshot_bytes = snapshot.read_bytes()
    partial = json.loads(snapshot_bytes)["payload"]
    assert {edge["version"] for edge in partial["hierarchy"]} == {1}
    assert any("Shared scientific content" in node["content"] for node in partial["elements"])
    assert partial["versions"][1]["retryable"] is True
    assert Path(first["publication_root"]).joinpath("output_checksums.json").is_file()
    assert not root.joinpath("output_checksums.json").exists()

    # Retry with the same input, output and code. Both versions now succeed.
    with patch.object(runner, "VersionExecutor", return_value=lambda version, f, args: f(*args)):
        second = runner.run(corpus, output)
    assert second["run_root"] == first["run_root"]
    assert second["statuses"] == {"success": 2}
    assert second["all_versions_processed"] and not second["retryable"]
    assert snapshot.read_bytes() == snapshot_bytes
    result = json.loads((root / "papers/2508-00001/result.json").read_text())["payload"]
    assert {edge["version"] for edge in result["hierarchy"]} == {1, 2}
    assert len(result["bibtex"]) == 1
    aliases = [v["citation_aliases"] for v in result["versions"]]
    assert aliases[0]["key1"] == aliases[1]["key2"]
    parquet_before = {str(p): p.read_bytes() for p in (root / "data").rglob("*.parquet")}
    third = runner.run(corpus, output)
    assert third["cached_papers"] == 1
    assert {str(p): p.read_bytes() for p in (root / "data").rglob("*.parquet")} == parquet_before


@pytest.mark.parametrize("seconds", [0, -1, True, float("inf"), float("nan")])
def test_invalid_deadline_is_rejected_before_writing(tmp_path, seconds):
    with pytest.raises(runner.ParseError, match="finite and positive"):
        runner.run(tmp_path / "absent", tmp_path / "output", parse_timeout_seconds=seconds)
    assert not (tmp_path / "output").exists()


def test_output_size_excludes_nested_retry_history(tmp_path):
    files = {"data/table": 4, "papers/id/result.json": 5,
             "papers/id/partial/old.json": 200, "partial_attempts/old/data/table": 100}
    for name, size in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
    assert runner.output_bytes(tmp_path) == 9


@pytest.mark.parametrize("provider", ["cvpr", "iccv"])
def test_vision_caption_provider_keeps_structure_and_following_content(tmp_path, provider):
    (tmp_path / "main.tex").write_text(
        r"\documentclass{article}\usepackage{" + provider + r"}\begin{document}"
        r"\captionof{figure}{Scientific caption.}After.\end{document}", encoding="utf-8")
    result = parse_versions("2508.00001", [(1, tmp_path)])
    assert result["versions"][0]["status"] == "success"
    assert any(node["content"] == "figure caption" for node in result["elements"])
    assert any(node["content"] == "Scientific caption." for node in result["elements"])
    assert any(node["content"] == "After." for node in result["elements"])
    assert any("caption convention" in note for note in result["versions"][0]["normalization_notes"])


def test_jmlr_keyword_provider_keeps_keywords_and_next_section(tmp_path):
    (tmp_path / "main.tex").write_text(
        r"\documentclass{article}\usepackage{jmlr2e}\begin{document}"
        r"\begin{keywords}Scientific terms.\end{keywords}\section{Results}After.\end{document}", encoding="utf-8")
    result = parse_versions("2508.00001", [(1, tmp_path)])
    assert result["versions"][0]["status"] == "success"
    assert any(node["content"] == "Scientific terms." for node in result["elements"])
    assert any(node["content"] == "Results" for node in result["elements"])


@pytest.mark.parametrize("preamble,body,marker", [
    (r"\renewenvironment{abstract}{\small\noindent\textbf{Abstract.}\normalsize\ignorespaces}"
     r"{\par\noindent\ignorespacesafterend}", r"\begin{abstract}Scientific result.\end{abstract}After.", "Scientific result."),
    (r"\def\proof{{\it Proof.}\ }\def\endproof{\hfill $\Box$\par\vskip3mm}",
     r"\begin{proof}Scientific result.\end{proof}After.", r"$\Box$"),
])
def test_literal_abstract_and_proof_layout_overrides_preserve_content(tmp_path, preamble, body, marker):
    (tmp_path / "main.tex").write_text(r"\documentclass{article}" + preamble + r"\begin{document}" + body + r"\end{document}", encoding="utf-8")
    result = parse_versions("2508.00001", [(1, tmp_path)])
    assert result["versions"][0]["status"] == "success", result["versions"]
    content = " ".join(n["content"] for n in result["elements"])
    assert "Scientific result." in content and "After." in content and marker in content
    assert any(m.get("literal_layout_override") for m in result["versions"][0]["layout_metadata"])


def test_proof_replacement_with_scientific_payload_is_not_treated_as_layout(tmp_path):
    (tmp_path / "main.tex").write_text(
        r"\documentclass{article}\def\proof{SCIENTIFIC}\def\endproof{RESULT}"
        r"\begin{document}\begin{proof}BODY\end{proof}\end{document}", encoding="utf-8")
    result = parse_versions("2508.00001", [(1, tmp_path)])
    assert result["versions"][0]["status"] == "parse_error"
