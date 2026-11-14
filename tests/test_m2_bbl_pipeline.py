"""Offline regression tests for the bibliography conversion path."""
from __future__ import annotations

import csv
import io
import json
import tarfile
from pathlib import Path

import pytest

from src.m2.bbl_pipeline import render_bibtex, run_bbl_pipeline
from src.m2.bibliography import parse_bbl, parse_bibtex
from src.m2.latex import ParseError
from src.m2.runner import digest


def _archive(path: Path, members: dict[str, str]) -> None:
    with tarfile.open(path, "w:gz") as stream:
        for name, content in members.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(data)
            stream.addfile(info, io.BytesIO(data))


def test_labelled_acm_and_publisher_fields() -> None:
    acm = (r"\begin{thebibliography}{2}"
           r"\bibitem[A({[n.\,d.]})]{a}"
           r"\bibfield{author}{\bibinfo{person}{Alice A.}, \bibinfo{person}{Bob B.}}"
           r"\bibinfo{year}{2021}\newblock\showarticletitle{Reliable title}."
           r"\bibitem{b}\bauthor{\bsnm{Tobin}, \binits{I.}}"
           r"\batitle{Second title}\bjtitle{Journal}\byear{2020}"
           r"\doiurl{10.1234/example}\end{thebibliography}")
    entries = parse_bbl(acm, enriched=True)
    assert [entry["key"] for entry in entries] == ["a", "b"]
    assert entries[0]["authors"] == ["Alice A.", "Bob B."]
    assert entries[0]["title"] == "Reliable title"
    assert entries[1]["authors"] == ["I. Tobin"]
    assert entries[1]["doi"] == "10.1234/example"
    assert len(parse_bibtex(render_bibtex(entries))) == 2


def test_harvard_and_amsrefs_preserve_keys_and_fields() -> None:
    harvard = (r"\begin{thebibliography}{1}"
               r"\harvarditem[A et al.]{Alice A, Bob B \harvardand\ Carol C}{2020}{key-a}"
               r"Alice A. 2020. ``Explicit title.''"
               r"\end{thebibliography}")
    entry = parse_bbl(harvard, enriched=True)[0]
    assert (entry["key"], entry["title"], entry["year"]) == ("key-a", "Explicit title", 2020)
    assert entry["authors"] == ["Alice A", "Bob B", "Carol C"]
    ams = (r"\begin{bibdiv}\begin{biblist}"
           r"\bib{ref1}{article}{author={Amice, Yvette},author={V{\'e}lu, Jacques},"
           r"title={Evidence title},date={1975},journal={Journal},}"
           r"\end{biblist}\end{bibdiv}")
    entry = parse_bbl(ams, enriched=True)[0]
    assert entry["authors"] == ["Amice, Yvette", "V\\'elu, Jacques"]
    assert entry["year"] == 1975
    assert entry["journal"] == "Journal"


def test_missing_title_is_not_invented_and_apa_title_is_labelled() -> None:
    missing = parse_bbl(r"\begin{thebibliography}{1}\bibitem{k}A. 2020."
                        r"\end{thebibliography}", enriched=True)[0]
    assert missing["title"] is None
    apa = parse_bbl(r"\begin{thebibliography}{1}\bibitem{k}"
                    r"\APACrefYearMonthDay{2015}{}{}\newblock"
                    r"\APACrefatitle{Observed title}{Observed title}"
                    r"\APACjournalVolNumPages{Journal}{2}{3}{4}"
                    r"\end{thebibliography}", enriched=True)[0]
    assert (apa["title"], apa["year"], apa["journal"]) == ("Observed title", 2015, "Journal")
    assert apa["authors"] is None


def test_rendered_newblock_does_not_turn_url_or_venue_into_title() -> None:
    source = (r"\begin{thebibliography}{2}"
              r"\bibitem{web}Midjourney.\newblock\url{https://example.org}, 2022."
              r"\bibitem{article}R. Kawashima, A. Author, Article title, Journal 284 (2015) 59--69."
              r"\newblock\href{https://doi.org/10.1234/x}{doi:10.1234/x}."
              r"\end{thebibliography}")
    web, article = parse_bbl(source, enriched=True)
    assert web["title"] is None
    assert web["url"] == "https://example.org"
    assert article["title"] == "Article title"
    assert article["authors"] == ["R. Kawashima", "A. Author"]


def test_rendered_year_is_removed_from_title_and_comma_authors_split() -> None:
    source = (r"\begin{thebibliography}{1}\bibitem{rnd}"
              r"Yuri Burda, Harrison Edwards, Amos Storkey, and Oleg Klimov."
              r"\newblock Exploration by random network distillation, 2018."
              r"\end{thebibliography}")
    entry = parse_bbl(source, enriched=True)[0]
    assert entry["title"] == "Exploration by random network distillation"
    assert entry["year"] == 2018
    assert entry["authors"] == ["Yuri Burda", "Harrison Edwards", "Amos Storkey", "Oleg Klimov"]


def test_initial_authors_and_venue_in_one_rendered_block() -> None:
    source = (r"\begin{thebibliography}{1}\bibitem{linegar2023large}"
              r"M.~Linegar, R.~Kocielnik, R.~M. Alvarez, "
              r"Large language models and political science, "
              r"Frontiers in Political Science 5 (2023) 1257092."
              r"\end{thebibliography}")
    entry = parse_bbl(source, enriched=True)[0]
    assert entry["authors"] == ["M. Linegar", "R. Kocielnik", "R. M. Alvarez"]
    assert entry["title"] == "Large language models and political science"
    assert entry["journal"] == "Frontiers in Political Science"
    assert entry["field_methods"]["title"] == "rendered_comma_pattern"


def test_rendered_comma_pattern_does_not_label_et_al_as_title_or_in_as_journal() -> None:
    source = (r"\begin{thebibliography}{2}"
              r"\bibitem{short}K. Gao, Q. Pei, et~al., Real title, Nature 3 (2023) 10."
              r"\bibitem{proceedings}K. Gao, Q. Pei, FABind+: Molecular docking, "
              r"in: Proceedings of ML 2 (2025) 3."
              r"\end{thebibliography}")
    short, proceedings = parse_bbl(source, enriched=True)
    assert short["title"] is None
    assert proceedings["title"] == "FABind+: Molecular docking"
    assert proceedings["journal"] is None


def test_rendered_title_can_contain_comma_before_journal() -> None:
    source = (r"\begin{thebibliography}{1}\bibitem{paper}"
              r"A. Author, B. Author, A web server for binding site prediction, "
              r"analysis and druggability assessment, Bioinformatics 28 (2012) 2074--2076."
              r"\end{thebibliography}")
    entry = parse_bbl(source, enriched=True)[0]
    assert entry["title"] == "A web server for binding site prediction, analysis and druggability assessment"
    assert entry["journal"] == "Bioinformatics"


def test_rendered_truncated_authors_and_month_are_explicit() -> None:
    source = (r"\begin{thebibliography}{1}\bibitem{report}"
              r"Marah Abdin, Jyoti Aneja, Hany Awadalla, et~al."
              r"\newblock Phi-3 technical report, May 2024."
              r"\end{thebibliography}")
    entry = parse_bbl(source, enriched=True)[0]
    assert entry["authors"] == ["Marah Abdin", "Jyoti Aneja", "Hany Awadalla"]
    assert entry["authors_truncated"] is True
    assert entry["field_evidence"]["authors"]["complete"] is False
    assert entry["title"] == "Phi-3 technical report"


def test_rendered_single_author_with_parenthetical_year() -> None:
    source = (r"\begin{thebibliography}{1}\bibitem{alzer2001mean}"
              r"Alzer, H. (2001).\newblock Mean-value inequalities."
              r"\end{thebibliography}")
    entry = parse_bbl(source, enriched=True)[0]
    assert entry["authors"] == ["Alzer, H."]
    assert entry["year"] == 2001


def test_pipeline_verifies_input_and_resumes_without_duplicate_output(tmp_path: Path) -> None:
    archive = tmp_path / "cohort_0017_bbl_artifacts.tar.gz"
    name = "2508-09473/v1/source_bbl/main.bbl"
    content = (r"\begin{thebibliography}{1}\bibitem{x}Alice."
               r"\newblock Evidence title.\newblock 2020.\end{thebibliography}")
    _archive(archive, {name: content})
    inventory = tmp_path / "inventory.csv"
    with inventory.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(("archive", "path", "sha256"))
        writer.writerow((archive.name, name, digest(content.encode())))
    output = tmp_path / "output"
    first = run_bbl_pipeline([archive], output, inventory=inventory)
    second = run_bbl_pipeline([archive], output, inventory=inventory)
    assert first["run_root"] == second["run_root"]
    assert second["cached_files"] == 1
    assert second["counts"] == {"entries": 1, "success": 1, "publications_success": 1,
                                "unified_entries": 1, "citation_mappings": 1,
                                "field_conflicts": 0}
    bib = Path(first["run_root"]) / "papers" / "2508.09473" / "bibliography.bib"
    assert [entry["key"] for entry in parse_bibtex(bib.read_text())] == ["x"]
    evidence = json.loads((bib.parent / "evidence.json").read_text())
    assert evidence["bibtex_table"][0]["title"] == "Evidence title"
    file_checkpoint = Path(first["run_root"]) / "files" / name.replace(".bbl", ".json")
    original_checkpoint = file_checkpoint.read_text(encoding="utf-8")
    file_checkpoint.write_text(
        original_checkpoint.replace("Evidence title", "Altered title", 1), encoding="utf-8")
    with pytest.raises(ParseError, match="Corrupt BBL checkpoint"):
        run_bbl_pipeline([archive], output, inventory=inventory)
    file_checkpoint.write_text(original_checkpoint, encoding="utf-8")
    with inventory.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(("archive", "path", "sha256"))
        writer.writerow((archive.name, name, "0" * 64))
    with pytest.raises(ParseError, match="inventory mismatch"):
        run_bbl_pipeline([archive], output, inventory=inventory)
    with inventory.open("a", encoding="utf-8") as stream:
        stream.write(f"{archive.name},{name},{'0' * 64}\n")
    with pytest.raises(ParseError, match="duplicated BBL inventory"):
        run_bbl_pipeline([archive], output, inventory=inventory)


def test_distinct_bbl_files_require_compilation_path(tmp_path: Path) -> None:
    archive = tmp_path / "cohort_0017_bbl_artifacts.tar.gz"
    _archive(archive, {
        "2508-09473/v1/source_bbl/main.bbl": r"\begin{thebibliography}{1}\bibitem{a}A.\end{thebibliography}",
        "2508-09473/v1/source_bbl/supplement.bbl": (
            r"\begin{thebibliography}{1}\bibitem{b}B.\end{thebibliography}"),
    })
    report = run_bbl_pipeline([archive], tmp_path / "output")
    assert report["counts"]["success"] == 2
    assert report["publications"][0]["status"] == "requires_review"
    assert not (Path(report["run_root"]) / "papers" / "2508.09473" / "bibliography.bib").exists()


def test_structural_definition_is_rejected() -> None:
    source = (r"\begin{thebibliography}{1}"
              r"\def\mn@alias{\bibitem{hidden}}"
              r"\bibitem{visible}Evidence.\end{thebibliography}")
    with pytest.raises(ParseError):
        parse_bbl(source)


def test_nested_bibitem_text_does_not_create_a_reference() -> None:
    source = (r"\begin{thebibliography}{1}\bibitem{visible}"
              r"Quoted source {\bibitem{hidden}} remains text."
              r"\end{thebibliography}")
    assert [entry["key"] for entry in parse_bbl(source)] == ["visible"]
