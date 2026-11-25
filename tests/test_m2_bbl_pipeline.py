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


def _archive(path: Path, members: dict[str, str | bytes]) -> None:
    with tarfile.open(path, "w:gz") as stream:
        for name, content in members.items():
            data = content.encode("utf-8") if isinstance(content, str) else content
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


def test_pdi_style_macros_are_read_without_expanding_tex() -> None:
    source = (r"\csname url@rmstyle\endcsname"
              r"\begin{thebibliography}{1}"
              r"\setlength{\itemsep}{2.1pt}\setlength{\parsep}{2.1pt}"
              r"\setlength{\parskip}{2.1pt}"
              r"\makeatletter\def\@biblabel#1{#1.}\makeatother"
              r"\def\au#1{#1}\def\at#1{#1}\def\jt#1{\textit{#1}}"
              r"\def\yr#1{#1}"
              r"\ifx\xfnm\undefined\def\xfnm[#1]{\unskip,\space#1}\fi"
              r"\bibitem{pdi}{\sc \au{Alice A.} \& \au{Bob B.}}"
              r"\yr{2024}\at{Evidence title}\jt{Evidence journal}."
              r"\end{thebibliography}")
    entry = parse_bbl(source, enriched=True)[0]
    assert entry["authors"] == ["Alice A.", "Bob B."]
    assert entry["title"] == "Evidence title"
    assert entry["year"] == 2024
    assert entry["journal"] == "Evidence journal"
    assert entry["field_methods"]["authors"] == "publisher_macro"


def test_pdi_style_definition_rejects_non_presentation_code() -> None:
    source = (r"\begin{thebibliography}{1}"
              r"\def\au#1{\input{unsafe}}"
              r"\bibitem{pdi}\au{Alice}"
              r"\end{thebibliography}")
    with pytest.raises(ParseError, match="presentation definition"):
        parse_bbl(source, enriched=True)


def test_biblatex_implicit_global_datalist() -> None:
    source = r"""% $ biblatex auxiliary file $
% $ biblatex bbl format version 3.3 $
\begingroup
\makeatletter
\@ifundefined{ver@biblatex.sty}
  {\@latex@error
     {Missing 'biblatex' package}
     {The bibliography requires the 'biblatex' package.}
      \aftergroup\endinput}
  {}
\endgroup
\datalist[entry]{none/global//global/global/global}
\entry{implicit}{article}{}{}
  \name{author}{1}{}{%
    {{hash=JD}{%
       family={Doe},
       familyi={D\bibinitperiod},
       given={Jane},
       giveni={J\bibinitperiod},
    }}%
  }
  \field{title}{Implicit global title}
  \field{journaltitle}{Test Journal}
  \field{year}{2024}
\endentry
\enddatalist
\endinput
"""
    entry = parse_bbl(source, enriched=True)[0]
    assert entry["key"] == "implicit"
    assert entry["title"] == "Implicit global title"
    assert entry["authors"] == ["Jane Doe"]
    assert entry["journal"] == "Test Journal"
    assert entry["year"] == 2024


def test_biblatex_implicit_global_datalist_accepts_biber_32_sort_context() -> None:
    source = (r"\datalist[entry]{nyt/global//global/global}"
              r"\entry{sorted}{article}{}{}"
              r"\field{title}{Sorted global entry}"
              r"\endentry\enddatalist\endinput")
    entry = parse_bbl(source)[0]
    assert entry["title"] == "Sorted global entry"
    unsafe_scope = source.replace("nyt/global//global/global", "nyt/1//global/global")
    with pytest.raises(ParseError, match="datalist scope"):
        parse_bbl(unsafe_scope)


def _duplicate_global_datalists(second_title: str = "Same title") -> str:
    item = "\n" + r"\entry{shared}{article}{}{}\field{title}{Same title}\endentry"
    duplicate = item.replace("Same title", second_title)
    return (r"\refsection{0}\datalist[entry]{apa/apasortcite//global/global/global}" + item
            + r"\enddatalist\datalist[entry]{apa/global//global/global/global}"
            + duplicate + r"\enddatalist\endrefsection\endinput")


def test_biblatex_repeated_global_datalist_entries_are_deduplicated_with_evidence() -> None:
    entries = parse_bbl(_duplicate_global_datalists())
    assert len(entries) == 1
    metadata = entries[0]["biblatex_metadata"]
    assert metadata["datalist_names"] == [
        "apa/apasortcite//global/global/global",
        "apa/global//global/global/global",
    ]
    assert metadata["source_occurrences"] == 2
    assert len(metadata["datalist_evidence"]) == 2
    assert all(evidence["sha256"] and evidence["raw"].startswith(r"\entry{shared}")
               for evidence in metadata["datalist_evidence"])


def test_biblatex_conflicting_global_datalist_duplicates_are_rejected() -> None:
    with pytest.raises(ParseError, match="Conflicting duplicate BibLaTeX public entry"):
        parse_bbl(_duplicate_global_datalists("Different title"))


def test_biblatex_duplicate_keys_in_scoped_datalists_are_rejected() -> None:
    source = _duplicate_global_datalists().replace(
        "apa/apasortcite//global/global/global", "apa/local//local/local/local")
    with pytest.raises(ParseError, match="scoped datalists"):
        parse_bbl(source)


def test_pipeline_counts_physical_datalist_occurrences_after_safe_dedup(tmp_path) -> None:
    archive = tmp_path / "cohort_0001_bbl_artifacts.tar.gz"
    _archive(archive, {"2508-00001/v1/source_bbl/main.bbl": _duplicate_global_datalists()})
    result = run_bbl_pipeline([archive], tmp_path / "output")
    record = result["files"][0]
    assert record["status"] == "success"
    assert record["candidate_markers"] == 2
    assert record["entry_count"] == 1
    assert record["source_entry_count"] == 2
    assert record["deduplicated_entry_occurrences"] == 1


def test_static_mcite_bibliography_alias_is_normalized() -> None:
    source = r"""\providecommand*\mcitethebibliography{\thebibliography}
\csname @ifundefined\endcsname{endmcitethebibliography}
  {\let\endmcitethebibliography\endthebibliography}{}
\begin{mcitethebibliography}{1}
\providecommand*\natexlab[1]{#1}
\providecommand*\mciteSetBstSublistMode[1]{}
\providecommand*\mciteSetBstMaxWidthForm[2]{}
\providecommand*\mciteSetBstSublistLabelBeginEnd[3]{}
\providecommand*\mciteBstWouldAddEndPuncttrue{\def\EndOfBibitem{\unskip.}}
\providecommand*\mciteBstWouldAddEndPunctfalse{\let\EndOfBibitem\relax}
\providecommand*\EndOfBibitem{}
\mciteSetBstSublistMode{f}
\mciteSetBstMaxWidthForm{subitem}{(\alph{mcitesubitemcount})}
\mciteSetBstSublistLabelBeginEnd
  {\mcitemaxwidthsubitemform\space}{\relax}{\relax}
\bibitem{k}A. Author. Evidence title. 2025.
\end{mcitethebibliography}"""
    assert [entry["key"] for entry in parse_bbl(source)] == ["k"]
    unsafe = source.replace(r"{\thebibliography}", r"{\input{unsafe}}")
    with pytest.raises(ParseError, match="mcite bibliography wrapper"):
        parse_bbl(unsafe)
    unsafe_setup = source.replace(
        r"\providecommand*\mciteSetBstSublistMode[1]{}",
        r"\providecommand*\mciteSetBstSublistMode[1]{\input{unsafe}}")
    with pytest.raises(ParseError, match="mcite setup definition"):
        parse_bbl(unsafe_setup)


def test_biblatex_multiline_verbatim_field_fragments_are_concatenated() -> None:
    source = (r"\refsection{0}\datalist[entry]{none}"
              r"\entry{verb}{misc}{}{}\field{title}{Verbatim field}"
              r"\verb{url}" + "\n" +
              r"\verb https://example.org/a%20" + "\n" +
              r"\verb b?x=1" + "\n" +
              r"\endverb\endentry\enddatalist\endrefsection\endinput")
    entry = parse_bbl(source)[0]
    assert entry["url"] == "https://example.org/a%20b?x=1"


def test_bbl_setlength_rejects_dynamic_or_unrelated_values() -> None:
    source = (r"\begin{thebibliography}{1}\setlength{\itemsep}{\dynamic}"
              r"\bibitem{k}Evidence.\end{thebibliography}")
    with pytest.raises(ParseError, match="setlength"):
        parse_bbl(source)


def test_bbl_urlstyle_accepts_known_presentation_values() -> None:
    source = (r"\begin{thebibliography}{1}\urlstyle{rm}"
              r"\expandafter\ifx\csname url\endcsname\relax"
              r"\def\url#1{\burl{#1}}\fi"
              r"\expandafter\ifx\csname doiprefix\endcsname\relax"
              r"\def\doiprefix{DOI: }\fi"
              r"\bibitem{k}Evidence.\end{thebibliography}")
    assert parse_bbl(source)[0]["key"] == "k"
    unsafe = source.replace("{rm}", "{unknown}")
    with pytest.raises(ParseError, match="URL style"):
        parse_bbl(unsafe)


def test_bbl_urlprefix_accepts_known_presentation_labels() -> None:
    for label in ("URL: ", "URL ", "Available at "):
        source = (r"\begin{thebibliography}{1}"
                  r"\expandafter\ifx\csname urlprefix\endcsname\relax"
                  r"\def\urlprefix{" + label + r"}\fi"
                  r"\bibitem{k}Evidence.\end{thebibliography}")
        assert parse_bbl(source)[0]["key"] == "k"


def test_bbl_doi_url_command_setup_is_bounded_presentation_metadata() -> None:
    source = r"""\begin{thebibliography}{1}
\providecommand{\url}[1]{#1}
\expandafter\ifx\csname urlstyle\endcsname\relax\else
  \urlstyle{same}\fi
\expandafter\ifx\csname href\endcsname\relax
  \DeclareUrlCommand\doi{\urlstyle{rm}}
  \def\eprint#1#2{#2}
\else
  \def\doi#1{\href{https://doi.org/#1}{\nolinkurl{#1}}}
  \let\eprint\href
\fi
\bibitem{k}Evidence title.
\end{thebibliography}"""
    assert parse_bbl(source)[0]["key"] == "k"


def test_biblatex_warning_is_preserved_as_auxiliary_metadata() -> None:
    source = (r"\datalist[entry]{nyt/global//global/global}"
              r"\entry{k}{article}{}{}\field{title}{A title}"
              r"\warn{\item Invalid format of field 'month'}"
              r"\endentry\enddatalist\endinput")
    entry = parse_bbl(source)[0]
    assert entry["biblatex_metadata"]["auxiliary"] == [
        {"kind": "warning", "value": r"\item Invalid format of field 'month'"}
    ]


def _two_refsection_bbl() -> str:
    return r"""\refsection{0}
\datalist[entry]{none/global//global/global/global}
\entry{shared}{article}{}{}\field{title}{Section zero title}\endentry
\enddatalist\endrefsection
\refsection{1}
\datalist[entry]{none/global//global/global/global}
\entry{shared}{article}{}{}\field{title}{Section one title}\endentry
\enddatalist\endrefsection\endinput"""


def test_biblatex_duplicate_keys_remain_scoped_to_refsections() -> None:
    entries = parse_bbl(_two_refsection_bbl())
    assert [entry["biblatex_metadata"]["refsection_id"] for entry in entries] == ["0", "1"]
    assert [entry["title"] for entry in entries] == ["Section zero title", "Section one title"]
    with pytest.raises(ParseError, match="separate BibTeX outputs"):
        from src.m2.bbl_pipeline import render_bibtex
        render_bibtex(entries)


def test_pipeline_writes_each_refsection_and_blocks_cross_section_merge(tmp_path) -> None:
    archive = tmp_path / "cohort_0001_bbl_artifacts.tar.gz"
    _archive(archive, {"2508-00001/v1/source_bbl/references.bbl": _two_refsection_bbl()})
    output = tmp_path / "output"
    result = run_bbl_pipeline([archive], output)
    record = result["files"][0]
    assert record["status"] == "success"
    assert record["biblatex_refsections"] == ["0", "1"]
    assert len(record["bibtex_paths"]) == 2
    for path in record["bibtex_paths"]:
        assert len(parse_bibtex((Path(result["run_root"]) / path).read_text(encoding="utf-8"))) == 1
    publication = result["publications"][0]
    assert publication["status"] == "requires_review"
    assert "multiple BibLaTeX refsections" in publication["problems"][0]
    assert not (Path(result["run_root"]) / "papers/2508.00001/bibliography.bib").exists()

    resumed = run_bbl_pipeline([archive], output)
    assert resumed["cached_files"] == 1


def test_pipeline_writes_empty_bibtex_for_valid_empty_bibliography(tmp_path) -> None:
    archive = tmp_path / "cohort_0001_bbl_artifacts.tar.gz"
    _archive(archive, {"2508-00001/v1/source_bbl/supplement.bbl":
                      r"\begin{thebibliography}{0}\end{thebibliography}"})
    result = run_bbl_pipeline([archive], tmp_path / "output")
    record = result["files"][0]
    assert record["status"] == "success"
    assert record["entry_count"] == 0
    assert (Path(result["run_root"]) / record["bibtex_paths"][0]).read_text() == ""
    assert result["publications"][0]["status"] == "success"
    assert result["publications"][0]["unified_entry_count"] == 0


def test_malformed_utf8_in_auxiliary_name_hash_is_reported_and_fields_survive(tmp_path) -> None:
    archive = tmp_path / "cohort_0001_bbl_artifacts.tar.gz"
    source = (b"\\refsection{0}\\datalist[entry]{none}"
              b"\\entry{k}{article}{}{}"
              b"\\name{author}{1}{}{"
              b"{{hash=J\xc3D}{family={Doe},given={Jane}}}"
              b"}\\field{title}{Recoverable title}"
              b"\\endentry\\enddatalist\\endrefsection\\endinput")
    _archive(archive, {"2508-00001/v1/source_bbl/main.bbl": source})
    result = run_bbl_pipeline([archive], tmp_path / "output")
    record = result["files"][0]
    assert record["status"] == "success"
    assert record["decode_warning"]["encoding"] == "utf-8-with-replacement"
    assert record["decode_warning"]["replacement_characters"] == 1
    assert result["counts"]["publications_success"] == 1


def test_malformed_utf8_in_public_bibliography_field_is_not_accepted(tmp_path) -> None:
    archive = tmp_path / "cohort_0001_bbl_artifacts.tar.gz"
    source = (b"\\refsection{0}\\datalist[entry]{none}"
              b"\\entry{k}{article}{}{}\\field{title}{Title \xc3 broken}"
              b"\\endentry\\enddatalist\\endrefsection\\endinput")
    _archive(archive, {"2508-00001/v1/source_bbl/main.bbl": source})
    result = run_bbl_pipeline([archive], tmp_path / "output")
    record = result["files"][0]
    assert record["status"] == "parse_error"
    assert record["error"] == "Invalid UTF-8 intersects an extracted bibliography field"


def test_nested_bibitem_text_does_not_create_a_reference() -> None:
    source = (r"\begin{thebibliography}{1}\bibitem{visible}"
              r"Quoted source {\bibitem{hidden}} remains text."
              r"\end{thebibliography}")
    assert [entry["key"] for entry in parse_bbl(source)] == ["visible"]


# Integration regressions: the conversion helper and full M2 parser must fail
# closed at the same bibliography scope/validation boundaries.
def test_failed_bibtex_render_is_not_a_success_or_empty_publication(tmp_path):
    archive = tmp_path / "duplicate.tar.gz"
    source = (r"\begin{thebibliography}{2}\bibitem{k}Alice.\newblock One."
              r"\bibitem{k}Bob.\newblock Different.\end{thebibliography}")
    _archive(archive, {"2508-00001/v1/source_bbl/main.bbl": source})
    result = run_bbl_pipeline([archive], tmp_path / "output")
    record = result["files"][0]
    assert record["status"] == "parse_error"
    assert record["entry_count"] == 0
    assert "Duplicate citation keys" in record["error"]
    assert result["publications"][0]["status"] == "requires_review"
    assert not (Path(result["run_root"]) / "papers/2508.00001/bibliography.bib").exists()
    assert result["counts"].get("success", 0) == 0
    resumed = run_bbl_pipeline([archive], tmp_path / "output")
    assert resumed["cached_files"] == 1
    assert resumed["files"][0]["status"] == "parse_error"
    assert resumed["publications"][0]["status"] == "requires_review"


def test_regular_m2_does_not_merge_unmapped_biblatex_refsections(tmp_path):
    from src.m2.parser import parse_versions
    source = (r"\refsection{0}\datalist[entry]{none}"
              r"\entry{a}{article}{}\field{title}{Global title}\endentry"
              r"\enddatalist\endrefsection"
              r"\refsection{1}\datalist[entry]{none}"
              r"\entry{b}{article}{}\field{title}{Other scope}\endentry"
              r"\enddatalist\endrefsection\endinput")
    (tmp_path / "main.tex").write_text(
        r"\documentclass{article}\usepackage{biblatex}\addbibresource{missing.bib}"
        r"\begin{document}See \cite{a,b}.\printbibliography\end{document}", encoding="utf8")
    (tmp_path / "main.bbl").write_text(source, encoding="utf8")
    result = parse_versions("2508.00001", [(1, tmp_path)])
    assert result["versions"][0]["status"] == "parse_error"
    assert "refsection" in result["versions"][0]["error"]
    assert result["bibtex"] == []
    assert result["elements"] == []


def test_harvard_conditional_cannot_create_an_inactive_reference():
    source = (r"\begin{thebibliography}{2}\harvarditem{Alice}{2020}{a}Visible."
              r"\iffalse\harvarditem{Bob}{2021}{b}Hidden.\fi\end{thebibliography}")
    with pytest.raises(ParseError, match="conditional|dynamic"):
        parse_bbl(source, enriched=True)


def test_nested_harvard_marker_is_opaque_reference_text():
    source = (r"\begin{thebibliography}{1}\harvarditem{Alice}{2020}{a}"
              r"Quoted {\harvarditem{Bob}{2021}{hidden}} remains text."
              r"\end{thebibliography}")
    assert [e["key"] for e in parse_bbl(source)] == ["a"]


@pytest.mark.parametrize("tail", [r"\input{more}", r"Unexpected text", r"\begin{biblist}\end{biblist}"])
def test_amsrefs_rejects_unverified_tail(tail):
    source = (r"\begin{bibdiv}\begin{biblist}\bib{k}{article}{title={Title},}"
              r"\end{biblist}\end{bibdiv}" + tail)
    with pytest.raises(ParseError, match="AMSRefs"):
        parse_bbl(source)


def test_amsrefs_balanced_spaced_wrapper_is_supported():
    source = (r"\begin {bibdiv}\begin {biblist}\bib{k}{article}{title={Title},}"
              r"\end {biblist}\end {bibdiv}\endinput")
    entry = parse_bbl(source, enriched=True)[0]
    assert entry["key"] == "k"
    assert entry["title"] == "Title"


@pytest.mark.parametrize("name", [
    r"2508-00001/v1/source_bbl/..\..\..\outside.bbl",
    "2508-00001/v1/source_bbl/C:outside.bbl",
])
def test_artifact_rejects_windows_unsafe_path_before_output(tmp_path, name):
    archive = tmp_path / "unsafe.tar.gz"
    _archive(archive, {name: r"\begin{thebibliography}{0}\end{thebibliography}"})
    output = tmp_path / "output"
    with pytest.raises(ParseError, match="Unsafe|Unrecognized"):
        run_bbl_pipeline([archive], output)
    assert not output.exists()


def test_colliding_artifact_paths_from_two_inputs_fail_before_output(tmp_path):
    content = r"\begin{thebibliography}{1}\bibitem{k}Evidence.\end{thebibliography}"
    first, second = tmp_path / "one.tar.gz", tmp_path / "two.tar.gz"
    name = "2508-00001/v1/source_bbl/main.bbl"
    _archive(first, {name: content})
    _archive(second, {name: content})
    output = tmp_path / "output"
    with pytest.raises(ParseError, match="Duplicate.*path|collid"):
        run_bbl_pipeline([first, second], output)
    assert not output.exists()


def test_missing_authors_are_not_reported_as_complete_evidence():
    source = (r"\refsection{0}\datalist[entry]{none}"
              r"\entry{k}{article}{}\field{title}{Known title}\endentry"
              r"\enddatalist\endrefsection\endinput")
    entry = parse_bbl(source, enriched=True)[0]
    assert entry["authors"] is None
    assert entry["field_evidence"]["authors"]["complete"] is False


def test_regular_runner_keeps_bib_precedence_and_version_specific_bbl_keys(tmp_path):
    from src.m2.runner import run, validate_tables
    corpus = tmp_path / "corpus"
    paper = corpus / "2508-00001"
    versions = {}
    sources = {
        1: {
            "main.tex": (r"\documentclass{article}\begin{document}First \cite{k,extra}."
                         r"\bibliography{refs}\end{document}"),
            "refs.bib": "@article{k,title={Verified Bib title},author={Alice},doi={10.1000/v1}}",
            "main.bbl": (r"\begin{thebibliography}{2}\bibitem{k}Wrong.\newblock Rendered conflict."
                         r"\bibitem{extra}\bibinfo{author}{Carol}\bibinfo{title}{BBL fallback}"
                         r"\end{thebibliography}"),
        },
        2: {
            "main.tex": (r"\documentclass{article}\begin{document}Second \cite{k}."
                         r"\bibliography{refs}\end{document}"),
            "main.bbl": (r"\begin{thebibliography}{1}\bibitem{k}\bibinfo{author}{Bob}"
                         r"\bibinfo{title}{Version two title}\doi{10.1000/v2}"
                         r"\end{thebibliography}"),
        },
    }
    for v, files in sources.items():
        directory = paper / f"v{v}"
        directory.mkdir(parents=True)
        for name, source in files.items():
            (directory / name).write_text(source, encoding="utf8")
        versions[f"v{v}"] = {
            "status": "success", "files": list(files),
            "file_checksums": {name: digest((directory / name).read_bytes()) for name in files},
        }
    identity = {"contract_version": 2, "arxiv_id": "2508.00001", "mssv": "24120010"}
    (paper / "manifest.json").write_text(json.dumps({**identity, "versions": versions}))
    (paper / "metadata.json").write_text(json.dumps(identity))
    original = {str(p): p.read_bytes() for p in corpus.rglob("*") if p.is_file()}
    result = run(corpus, tmp_path / "output")
    assert [v["status"] for v in result["versions_detail"]] == ["success", "success"]
    root = Path(result["run_root"])
    payload = json.loads((root / "papers/2508-00001/result.json").read_text())["payload"]
    validate_tables(payload)
    rows = {r["citation_key"]: r for r in payload["bibtex"]}
    assert rows["k"]["title"] == "Verified Bib title"
    assert rows["k"]["authors"] == ["Alice"]
    assert rows["k"]["doi"] == "10.1000/v1"
    assert rows["extra"]["title"] == "BBL fallback"
    v2_key = payload["versions"][1]["citation_aliases"]["k"]
    assert v2_key != "k"
    assert rows[v2_key]["title"] == "Version two title"
    assert rows[v2_key]["authors"] == ["Bob"]
    assert rows[v2_key]["doi"] == "10.1000/v2"
    for version, key in ((1, "k"), (2, v2_key)):
        ids = {e["element_id"] for e in payload["hierarchy"] if e["version"] == version}
        content = " ".join(n["content"] for n in payload["elements"] if n["element_id"] in ids)
        assert "\\cite{" + key in content
    assert not any("Rendered conflict" in str(r) for r in rows.values())
    again = run(corpus, tmp_path / "output")
    assert again["cached_papers"] == 1
    assert {str(p): p.read_bytes() for p in corpus.rglob("*") if p.is_file()} == original


def test_regular_m2_cannot_borrow_bbl_from_a_different_version(tmp_path):
    from src.m2.parser import parse_versions
    v1, v2 = tmp_path / "v1", tmp_path / "v2"
    v1.mkdir()
    v2.mkdir()
    source = (r"\documentclass{article}\begin{document}Visible \cite{k}."
              r"\bibliography{missing}\end{document}")
    for d in (v1, v2):
        (d / "main.tex").write_text(source, encoding="utf8")
    (v2 / "main.bbl").write_text(
        r"\begin{thebibliography}{1}\bibitem{k}Alice.\newblock Evidence."
        r"\end{thebibliography}", encoding="utf8")
    result = parse_versions("2508.00001", [(1, v1), (2, v2)])
    assert [v["status"] for v in result["versions"]] == ["parse_error", "success"]
    assert {e["version"] for e in result["hierarchy"]} == {2}
    assert result["bibtex"][0]["citation_key"] == "k"


def test_inline_author_parenthesized_year_does_not_break_duplicate_key_matching(tmp_path):
    from src.m2.parser import parse_versions
    source = (r"\documentclass{article}\begin{document}Evidence \cite{same}."
              r"\begin{thebibliography}{2}\bibitem{same}"
              r"Dou, B., Parrella, M. L., and Yao, Q. (2016)."
              r"\newblock {Exact research title.}\newblock Journal 1."
              r"\end{thebibliography}Appendix."
              r"\begin{thebibliography}{2}\bibitem{same}"
              r"Dou, B., Parrella, M. L., and Yao, Q. (2016)."
              r"\newblock Exact research title.\newblock Journal 1."
              r"\end{thebibliography}\end{document}")
    (tmp_path / "main.tex").write_text(source, encoding="utf8")
    result = parse_versions("2508.00001", [(1, tmp_path)])
    assert result["versions"][0]["status"] == "success"
    assert len(result["bibtex"]) == 1
    entry = result["bibtex"][0]
    assert entry["citation_key"] == "same"
    assert entry["authors"] == ["Dou, B., Parrella, M. L.,", "Yao, Q. (2016)"]
    assert entry["year"] == 2016
    assert any(r"\cite{same}" in n["content"] for n in result["elements"])


def test_inline_labelled_bbl_records_keep_real_person_names(tmp_path):
    from src.m2.parser import parse_versions
    (tmp_path / "main.tex").write_text(
        r"\documentclass{article}\begin{document}Cited \cite{k}."
        r"\begin{thebibliography}{1}\bibitem{k}"
        r"\bibfield{author}{\bibinfo{person}{Alice A.}, \bibinfo{person}{Bob B.}}"
        r"\bibinfo{year}{2024}\newblock\showarticletitle{Labelled title}."
        r"\end{thebibliography}\end{document}", encoding="utf8")
    result = parse_versions("2508.00001", [(1, tmp_path)])
    assert result["versions"][0]["status"] == "success"
    assert result["bibtex"][0]["authors"] == ["Alice A.", "Bob B."]
    assert result["bibtex"][0]["title"] == "Labelled title"
    assert result["bibtex"][0]["year"] == 2024


def test_linked_title_retains_display_text_instead_of_becoming_missing():
    entry = parse_bbl(
        r"\begin{thebibliography}{1}\bibitem{k}Alice."
        r"\newblock\href{https://doi.org/10.1000/test}{Linked research title}."
        r"\newblock Journal, 2020.\end{thebibliography}", enriched=True)[0]
    assert entry["title"] == "Linked research title"
    assert entry["url"] == "https://doi.org/10.1000/test"


def test_natbib_year_suffix_does_not_erase_author_block():
    from src.m2.bibliography import parse_bibitems
    entry = parse_bibitems(
        r"\bibitem{k}Alice, Bob, and Carol. 2024\natexlab{a}."
        r"\newblock Research title.")[0]
    assert entry["authors"] is not None
    assert "Alice" in " ".join(entry["authors"])
    assert entry["title"] == "Research title"


@pytest.mark.parametrize("suffix", [r"{\natexlab{a}}", r"\natexlab{a}", r"\natexlab{}"])
def test_natbib_year_suffix_grouping_keeps_named_authors(suffix):
    source = (r"\begin{thebibliography}{1}\bibitem{k}"
              "Alice, Bob, and Carol. 2024" + suffix +
              r".\newblock Title.\end{thebibliography}")
    entry = parse_bbl(source, enriched=True)[0]
    assert entry["authors"] == ["Alice", "Bob", "Carol"]
    assert entry["title"] == "Title"
    assert entry["year"] == 2024


def test_empty_person_label_keeps_other_names_and_incomplete_evidence():
    entry = parse_bbl(
        r"\begin{thebibliography}{1}\bibitem{k}"
        r"\bibfield{author}{\bibinfo {person}{Alice}, \bibinfo{person}{}, "
        r"\bibinfo{person}{Bob}}\bibinfo{title}{Evidence}"
        r"\end{thebibliography}", enriched=True)[0]
    assert entry["authors"] == ["Alice", "Bob"]
    assert entry["empty_author_labels"] == 1
    assert entry["field_evidence"]["authors"]["complete"] is False
    assert r"\bibinfo{person}{}" in entry["raw"]
