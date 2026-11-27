"""Keep producer-specific Biber data out of the rendered-TeX lexer."""
from unittest.mock import patch

import pytest

from src.m2.bibliography import _bbl_format, parse_bbl
from src.m2.latex import Gatherer, ParseError
from src.m2.parser import bbl_item_group_projection, parse_versions
from src.m2.runner import validate_tables


def biber_data():
    return (r"\refsection{0}\datalist[entry]{none}\entry{k}{article}{}"
            r"\name{author}{1}{}{{{hash=x}{family={Doe},given={Jane}}}}"
            r"\field{title}{Recorded evidence}\field{year}{2025}"
            r"\field{journaltitle}{Recorded journal}" + "\n"
            r"\verb{doi}" + "\n" + r"\verb 10.1234/recorded" + "\n"
            r"\endverb" + "\n" + r"\verb{url}" + "\n"
            r"\verb https://example.org/a%20b" + "\n"
            r"\endverb\endentry\enddatalist\endrefsection\endinput")


def parse_document(tmp_path, bbl):
    (tmp_path / "main.tex").write_text(
        r"\documentclass{article}\usepackage{biblatex}\addbibresource{missing.bib}"
        r"\begin{document}\section{Evidence}Before $x=y$ \cite{k}."
        r"\printbibliography After.\end{document}", encoding="utf8")
    (tmp_path / "main.bbl").write_text(bbl, encoding="utf8")
    result = parse_versions("2508.00001", [(1, tmp_path)])
    validate_tables(result)
    return result


def test_biber_doi_blocks_reach_dedicated_reader_without_rendered_lexing(tmp_path):
    raw = biber_data()
    with patch("src.m2.parser._bbl_comments", side_effect=AssertionError("wrong lexer")):
        assert bbl_item_group_projection(raw, Gatherer(tmp_path)) == raw
    result = parse_document(tmp_path, raw)
    record = result["versions"][0]
    assert record["status"] == "success", record
    assert result["bibtex"] == [{
        "arxiv_id": "2508.00001", "citation_key": "k", "entry_type": "article",
        "title": "Recorded evidence", "authors": ["Jane Doe"], "year": 2025,
        "doi": "10.1234/recorded", "journal": "Recorded journal",
        "url": "https://example.org/a%20b",
    }]
    assert record["citation_aliases"] == {"k": "k"}
    text = " ".join(n["content"] or "" for n in result["elements"])
    assert r"Before $x=y$ \cite{k}." in text and "After." in text
    assert "Recorded evidence" not in text
    nodes = {n["element_id"]: n for n in result["elements"]}
    section = next(n for n in nodes.values() if n["content"] == "Evidence")
    before = next(n for n in nodes.values() if r"\cite{k}" in (n["content"] or ""))
    assert any(e["element_id"] == before["element_id"] and
               e["parent_id"] == section["element_id"] and e["version"] == 1
               for e in result["hierarchy"])


def test_biber_dispatch_does_not_use_rendered_marker_inside_field(tmp_path):
    raw = biber_data().replace(r"\field{title}{Recorded evidence}",
                              r"\field{title}{Literal \begin{thebibliography} data}")
    assert _bbl_format(raw) == "biblatex"
    with patch("src.m2.parser._bbl_comments", side_effect=AssertionError("wrong lexer")):
        assert bbl_item_group_projection(raw, Gatherer(tmp_path)) == raw


@pytest.mark.parametrize("tail", [r"\entry{bad}{article}{}\unknown{data}\endentry",
                                  r"\entry{bad}{article}{}\field{title}{Unclosed"])
def test_malformed_biber_data_is_not_accepted_after_dispatch(tmp_path, tail):
    raw = r"\refsection{0}\datalist[entry]{none}" + tail + r"\enddatalist\endrefsection"
    with pytest.raises(ParseError):
        parse_bbl(raw)
    result = parse_document(tmp_path, raw)
    assert result["versions"][0]["status"] == "parse_error"
    assert result["bibtex"] == [] and result["elements"] == []


def test_rendered_bbl_projection_still_preserves_grouped_entries(tmp_path):
    raw = (r"\begin{thebibliography}{9}\bibitem{first}Alice.\newblock First."
           r"{\bibitem{k}Bob.\newblock Recorded evidence.\newblock 2025.}"
           r"\end{thebibliography}")
    result = parse_document(tmp_path, raw)
    assert result["versions"][0]["status"] == "success", result["versions"]
    assert next(b for b in result["bibtex"] if b["citation_key"] == "k")["title"] == "Recorded evidence"
    assert not any("Recorded evidence" in (n["content"] or "") for n in result["elements"])


@pytest.mark.parametrize("raw", [
    r"\begin{thebibliography}{1}\bibitem{k}Evidence \verb|unfinished\end{thebibliography}",
    r"\begin{thebibliography}{1}\bibitem{k}Evidence.",
    r"\begin{thebibliography}{1}\iffalse\bibitem{k}Hidden.\fi\end{thebibliography}",
])
def test_malformed_or_conditional_rendered_bbl_remains_rejected(tmp_path, raw):
    result = parse_document(tmp_path, raw)
    assert result["versions"][0]["status"] == "parse_error"
    assert result["bibtex"] == [] and result["elements"] == []


@pytest.mark.parametrize("raw, expected", [
    (r"\begin{bibdiv}\begin{biblist}\bib{k}{article}{title={Evidence},}\end{biblist}\end{bibdiv}", "amsrefs"),
    (r"\begin{thebibliography}{1}\harvarditem{Alice}{2020}{k}Evidence.\end{thebibliography}", "harvard"),
    (r"\begin{mcitethebibliography}{1}\bibitem{k}Evidence.\end{mcitethebibliography}", "mcite"),
    (r"\begin{thebibliography}{1}\bibitem{k}Evidence.\end{thebibliography}", "thebibliography"),
    (r"unknown format", None),
])
def test_shared_dispatch_preserves_reader_precedence(tmp_path, raw, expected):
    assert _bbl_format(raw) == expected
    if expected != "thebibliography":
        with patch("src.m2.parser._bbl_comments", side_effect=AssertionError("wrong lexer")):
            assert bbl_item_group_projection(raw, Gatherer(tmp_path)) == raw

def test_biber_dispatch_keeps_later_prose_error_and_exports_no_false_success(tmp_path):
    (tmp_path / "main.tex").write_text(
        r"\documentclass{article}\usepackage{biblatex}\addbibresource{missing.bib}"
        r"\begin{document}Before \cite{k}."
        r"\begin{enumerate}\addtocounter{enumi}{1}\item Evidence.\end{enumerate}"
        r"\printbibliography\end{document}", encoding="utf8")
    (tmp_path / "main.bbl").write_text(biber_data(), encoding="utf8")
    result = parse_versions("2508.00001", [(1, tmp_path)])
    validate_tables(result)
    record = result["versions"][0]
    assert record["status"] == "parse_error"
    assert "Unsupported text before first list item" in record["error"]
    assert r"\addtocounter" in record["error"]
    assert [e["key"] for e in record["bibliography_evidence"]] == ["k"]
    assert all(result[table] == [] for table in ["elements", "hierarchy", "bibtex"])


@pytest.mark.parametrize("comment", [
    r"% producer example: \refsection{0}\entry{example}",
    r"% producer example: \harvarditem{Alice}{2020}{example}",
    r"% producer example: \begin{bibdiv}\begin{biblist}",
    r"% producer example: \begin{mcitethebibliography}{1}",
    r"\providecommand{\newblock}{\relax}% example: \datalist[entry]{none}\entry{example}",
])
def test_commented_producer_markers_do_not_override_rendered_bbl(tmp_path, comment):
    raw = (comment + "\n" + r"\begin{thebibliography}{9}"
           r"\bibitem{first}Alice.\newblock First."
           r"{\bibitem{k}Bob.\newblock Recorded evidence.\newblock 2025.}"
           r"\end{thebibliography}")
    assert _bbl_format(raw) == "thebibliography"
    result = parse_document(tmp_path, raw)
    assert result["versions"][0]["status"] == "success", result["versions"]
    assert [e["citation_key"] for e in result["bibtex"]] == ["first", "k"]
    entry = next(e for e in result["bibtex"] if e["citation_key"] == "k")
    assert entry["title"] == "Recorded evidence" and entry["year"] == 2025
    assert result["versions"][0]["citation_aliases"] == {"first": "first", "k": "k"}
    assert all("Recorded evidence" not in (e["content"] or "") for e in result["elements"])
