"""Selective M2 integration regression tests; offline synthetic fixtures.

Runner test ported from Tran Van Tan Khoi, PRs #17/#22.
Token regex optimization ported from DatLe, PRs #27/#29.
"""
import hashlib
import json
from pathlib import Path
import re
from unittest.mock import patch

import pytest
from src.m2.latex import command, ParseError
from src.m2.parser import parse_versions
from src.m2.runner import run, output_bytes, TABLE_COLUMNS, peak_rss_kib_linux

def test_runner_attempt_records_benchmark_metrics(tmp_path):
    """The report needs wall time, per-publication time, peak RAM, output size and dedup counts."""
    corpus = tmp_path / "corpus"
    paper = corpus / "2508-00001"
    (paper / "v1").mkdir(parents=True)
    source = r"\documentclass{article}\begin{document}First sentence. Second sentence.\end{document}"
    (paper / "v1" / "main.tex").write_text(source, encoding="utf8")
    identity = {"contract_version": 2, "arxiv_id": "2508.00001", "mssv": "24120010"}
    manifest = {**identity, "versions": {"v1": {
        "status": "success", "files": ["main.tex"],
        "file_checksums": {"main.tex": hashlib.sha256((paper / "v1" / "main.tex").read_bytes()).hexdigest()}}}}
    (paper / "manifest.json").write_text(json.dumps(manifest), encoding="utf8")
    (paper / "metadata.json").write_text(json.dumps(identity), encoding="utf8")

    result = run(corpus, tmp_path / "output")
    attempt = json.loads(next((Path(result["run_root"]) / "attempts").glob("*.json")).read_text(encoding="utf8"))

    assert attempt["papers"] == 1
    assert attempt["wall_seconds"] > 0
    assert attempt["avg_seconds_per_publication"] == pytest.approx(attempt["wall_seconds"])
    assert attempt["peak_rss_kib_linux"] is None or attempt["peak_rss_kib_linux"] > 0
    assert attempt["output_bytes"] > 0
    dedup = attempt["dedup"]
    assert dedup["elements_after_dedup"] == attempt["tables"]["elements"] > 0
    # A single version contributes nothing to merge, so nothing is removed.
    assert dedup["elements_before_dedup"] == dedup["elements_after_dedup"]
    assert dedup["elements_removed"] == 0
    assert attempt["provenance"]["measurement_environment"] in {"local", "colab-cpu", "colab-gpu", "colab-tpu"}
    # attempts/ is bookkeeping: it must not inflate the published output size.
    assert attempt["output_bytes"] < sum(p.stat().st_size for p in Path(result["run_root"]).rglob("*") if p.is_file())

    # The full attempt keeps bibliography evidence and grows to hundreds of megabytes
    # on a corpus-wide run; the compact summary must carry the same counters and the
    # per-version outcome without it, so the report can be built from it.
    run_root = Path(result["run_root"])
    summaries = sorted((run_root / "summaries").glob("*.json"))
    assert len(summaries) == 1
    compact = json.loads(summaries[0].read_text(encoding="utf8"))
    assert compact["statuses"] == attempt["statuses"]
    assert compact["dedup"] == attempt["dedup"]
    assert [r["status"] for r in compact["versions_detail"]] == [r["status"] for r in attempt["versions_detail"]]
    assert all("arxiv_id" in r and "bibliography_evidence" not in r for r in compact["versions_detail"])
    assert summaries[0].stat().st_size < (sorted((run_root / "attempts").glob("*.json"))[-1]).stat().st_size


def _parse_case(tmp_path, body, preamble="", files=None):
    root = tmp_path / "source"
    root.mkdir()
    (root / "main.tex").write_text(r"\documentclass{article}" + preamble + r"\begin{document}" + body + r"\end{document}", encoding="utf8")
    for name, text in (files or {}).items():
        (root / name).write_text(text, encoding="utf8")
    return parse_versions("2508.00001", [(1, root)])


@pytest.mark.parametrize("text", [r"prefix \alpha{x} suffix", "a\\%b\\é", "\\line\nnext", "\\", "plain", r"\@name\1\{x\}"])
def test_command_optimization_matches_old_tokenizer_for_all_offsets(text):
    for pos in range(len(text) + 1):
        match = re.match(r"\\([a-zA-Z@]+|.)", text[pos:], re.S)
        if match:
            assert command(text, pos) == (match[1], pos + len(match[0]))
        else:
            with pytest.raises(ParseError, match="Trailing backslash"):
                command(text, pos)


@pytest.mark.parametrize("assignment", ["=1.4pt", "{ -2.5em }", " = {.5pt}"])
def test_literal_arraycolsep_keeps_following_math_and_citation(tmp_path, assignment):
    result = _parse_case(tmp_path, r"Before.\arraycolsep" + assignment + r" After $x=y$ \cite{k}.\bibliography{r}", files={"r.bib":"@article{k,title={Real evidence}}"})
    assert result['versions'][0]['status'] == 'success', result['versions']
    content = ' '.join(n['content'] for n in result['elements'])
    assert 'Before.' in content and 'After $x=y$' in content and r'\cite{k}' in content
    assert [r['citation_key'] for r in result['bibtex']] == ['k']
    assert all(e['version'] == 1 for e in result['hierarchy'])


@pytest.mark.parametrize("assignment", ["{SCIENTIFIC RESULT}", r"{\dynamic}", "=1.4ptX"])
def test_nonliteral_arraycolsep_stays_error(tmp_path, assignment):
    result = _parse_case(tmp_path, r"\arraycolsep" + assignment + "After.")
    assert result['versions'][0]['status'] == 'parse_error'
    assert 'Unsupported arraycolsep value' in result['versions'][0]['error']


def test_local_arraycolsep_override_preserves_scientific_text(tmp_path):
    result = _parse_case(tmp_path, r"\arraycolsep{RESULT}", preamble=r"\newcommand{\arraycolsep}[1]{SCIENTIFIC #1}")
    assert result['versions'][0]['status'] == 'success'
    assert 'SCIENTIFIC RESULT' in ' '.join(n['content'] for n in result['elements'])


@pytest.mark.parametrize("env", ["large", "small", "footnotesize", "Huge"])
def test_font_environment_keeps_order_equation_and_reference(tmp_path, env):
    result = _parse_case(tmp_path, r"Before.\begin{" + env + r"}Science $x=y$ \cite{k}.\end{" + env + r"}After.\bibliography{r}", files={"r.bib":"@article{k,title={Reference}}"})
    assert result['versions'][0]['status'] == 'success', result['versions']
    content = ' '.join(n['content'] for n in result['elements'])
    assert content.index('Before.') < content.index('Science $x=y$') < content.index('After.')
    assert r'\cite{k}' in content and len(result['bibtex']) == 1
    assert not any(n['content'] == env for n in result['elements'])


def test_local_font_environment_is_not_silently_treated_as_kernel_layout(tmp_path):
    result = _parse_case(tmp_path, r"\begin{large}BODY\end{large}", preamble=r"\renewenvironment{large}{LOCAL }{ END}")
    # Local environment expansion is unsupported by the baseline: keep the
    # rejection instead of treating its replacement body as layout-only.
    assert result['versions'][0]['status'] == 'parse_error'
    assert result['versions'][0]['error'] == 'Unsupported environment: large'


def test_textunderscore_glyph_and_local_override(tmp_path):
    result = _parse_case(tmp_path, r"\textunderscore", preamble=r"\newcommand{\textunderscore}{CUSTOM}")
    assert result['versions'][0]['status'] == 'success'
    assert 'CUSTOM' in ' '.join(n['content'] for n in result['elements'])


def test_textunderscore_kernel_glyph_keeps_following_content(tmp_path):
    result = _parse_case(tmp_path, r"part\textunderscore{}name. Following text.")
    assert result['versions'][0]['status'] == 'success'
    assert 'part_name.' in ' '.join(n['content'] for n in result['elements'])
    assert 'Following text.' in ' '.join(n['content'] for n in result['elements'])


def test_futurelet_remains_rejected_instead_of_deleting_tokens(tmp_path):
    result = _parse_case(tmp_path, r"Before.\futurelet\look\dispatch X After.", preamble=r"\newcommand{\dispatch}{SCIENTIFIC RESULT}")
    assert result['versions'][0]['status'] == 'parse_error'
    assert 'futurelet' in result['versions'][0]['error']


def test_conference_local_definition_is_not_dropped(tmp_path):
    result = _parse_case(tmp_path, r"\conference{RESULT}", preamble=r"\newcommand{\conference}[1]{SCIENTIFIC #1}")
    assert result['versions'][0]['status'] == 'success'
    assert 'SCIENTIFIC RESULT' in ' '.join(n['content'] for n in result['elements'])


def test_unknown_keyword_provider_stays_blocked(tmp_path):
    result = _parse_case(tmp_path, r"\begin{keywords}Science.\end{keywords}")
    assert result['versions'][0]['status'] == 'parse_error'


def test_runner_resume_summary_does_not_inflate_output_size_and_preserves_tables(tmp_path):
    corpus = tmp_path / 'corpus'
    paper = corpus / '2508-00001'
    versions = {}
    for version in [1, 2]:
        root = paper / f'v{version}'; root.mkdir(parents=True)
        source = r'\documentclass{article}\begin{document}Shared scientific sentence.\end{document}'
        (root / 'main.tex').write_text(source, encoding='utf8')
        versions[f'v{version}'] = dict(status='success',files=['main.tex'],file_checksums={'main.tex':hashlib.sha256((root/'main.tex').read_bytes()).hexdigest()})
    identity = dict(contract_version=2,arxiv_id='2508.00001',mssv='24120010')
    (paper/'manifest.json').write_text(json.dumps(dict(identity,versions=versions)),encoding='utf8')
    (paper/'metadata.json').write_text(json.dumps(identity),encoding='utf8')
    with patch('src.m2.runner.time.perf_counter', side_effect=[10.0,25.0]):
        first = run(corpus,tmp_path/'out')
    assert first['wall_seconds'] == first['avg_seconds_per_publication'] == 15.0
    assert first['papers'] == 1 and first['versions'] == 2
    assert first['dedup']['elements_before_dedup'] > first['dedup']['elements_after_dedup']
    root = Path(first['run_root'])
    before = {str(p.relative_to(root)):p.read_bytes() for p in (root/'data').rglob('*.parquet')}
    second = run(corpus,tmp_path/'out')
    assert second['cached_papers'] == 1
    assert second['output_bytes'] == first['output_bytes']
    assert {str(p.relative_to(root)):p.read_bytes() for p in (root/'data').rglob('*.parquet')} == before
    assert {p.stem for p in (root/'summaries').glob('*.json')} == {p.stem for p in (root/'attempts').glob('*.json')}
    assert second['peak_rss_scope'] == 'current_process_lifetime_excludes_children'


def test_disk_size_ignores_both_attempts_and_summaries(tmp_path):
    for name, size in [('data/table', 5), ('definition.json', 3), ('attempts/a.json',100), ('summaries/a.json',70)]:
        p=tmp_path/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'x'*size)
    assert output_bytes(tmp_path) == 8


def test_linux_rss_has_explicit_process_lifetime_scope():
    import types
    import sys
    fake=types.SimpleNamespace(RUSAGE_SELF=0,getrusage=lambda _:types.SimpleNamespace(ru_maxrss=12345))
    with patch('src.m2.runner.sys.platform','linux'),patch.dict(sys.modules,{'resource':fake}):
        assert peak_rss_kib_linux() == 12345
    with patch('src.m2.runner.sys.platform','win32'):
        assert peak_rss_kib_linux() is None


@pytest.mark.parametrize("name", ["large", "endlarge"])
def test_font_environment_command_override_stays_rejected(tmp_path, name):
    result = _parse_case(tmp_path, r"\begin{large}BODY\end{large}",
                         preamble=r"\newcommand{" + chr(92) + name + "}{SCIENTIFIC}")
    assert result['versions'][0]['status'] == 'parse_error'
    assert result['versions'][0]['error'] == 'Unsupported environment: large'


def test_font_environment_keeps_heading_state_for_following_text(tmp_path):
    result = _parse_case(tmp_path, r"\section{Outer}\begin{large}\section{Inner}Inside.\end{large} After.")
    assert result['versions'][0]['status'] == 'success'
    nodes = {n['content']:n for n in result['elements']}
    parents = {e['element_id']:e['parent_id'] for e in result['hierarchy']}
    assert parents[nodes['After.']['element_id']] == nodes['Inner']['element_id']
    assert parents[nodes['Inside.']['element_id']] == nodes['Inner']['element_id']
    assert parents[nodes['Inner']['element_id']] != nodes['Outer']['element_id']


def test_font_environment_keeps_literal_brackets_and_sentence_boundary(tmp_path):
    result = _parse_case(tmp_path, r"Before \begin{large}[scientific result]\end{large} after.")
    assert result['versions'][0]['status'] == 'success'
    assert [n['content'] for n in result['elements'] if n['kind']=='sentence'] == ['Before [scientific result] after.']


def test_empty_corpus_is_rejected_before_output_or_average(tmp_path):
    corpus = tmp_path / "empty-corpus"
    corpus.mkdir()
    output = tmp_path / "output"
    with pytest.raises(ParseError, match="No matching versions"):
        run(corpus, output)
    assert not output.exists()


def test_manifest_without_versions_is_not_a_completed_empty_run(tmp_path):
    corpus = tmp_path / "corpus"
    paper = corpus / "2508-00001"
    paper.mkdir(parents=True)
    identity = dict(contract_version=2, arxiv_id="2508.00001", mssv="24120010")
    (paper / "metadata.json").write_text(json.dumps(identity), encoding="utf8")
    (paper / "manifest.json").write_text(json.dumps(dict(identity, versions={})), encoding="utf8")
    output = tmp_path / "output"
    with pytest.raises(ParseError, match="No matching versions"):
        run(corpus, output)
    assert not output.exists()
