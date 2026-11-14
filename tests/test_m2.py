"""M2 regression and CLI integration tests; synthetic fixtures only, offline."""
import hashlib
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from src.m2.bibliography import parse_bibitems, parse_bibtex, unify
from src.m2.latex import Gatherer, ParseError, normalize, sentences, strip_comments
from src.m2.parser import parse_versions
from src.m2.runner import run, validate_tables, TABLE_COLUMNS
from scripts.run_m2 import main


AID = "2508.07872"
OWNER = "24120010"


def tex(body):
    return "\\documentclass{article}\n\\begin{document}\n" + body + "\n\\end{document}"


class M2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.corpus = self.root / "corpus"
        self.corpus.mkdir()
        self.network = patch.object(socket.socket, "connect", side_effect=AssertionError("No network in M2"))
        self.network.start()
        self.addCleanup(self.network.stop)

    def paper(self, sources, statuses=None, aid=AID):
        directory = self.corpus / aid.replace(".", "-")
        directory.mkdir()
        versions = {}
        for version, files in sources.items():
            vdir = directory / f"v{version}"
            vdir.mkdir()
            for name, text in files.items():
                path = vdir / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))
            hashes = {name: hashlib.sha256((vdir / name).read_bytes()).hexdigest() for name in files}
            versions[f"v{version}"] = dict(status="success", files=list(files), file_checksums=hashes)
        for version, status in (statuses or {}).items():
            versions[f"v{version}"] = dict(status=status)
        manifest = dict(contract_version=2, arxiv_id=aid, mssv=OWNER, versions=versions)
        (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (directory / "metadata.json").write_text(json.dumps(dict(contract_version=2, arxiv_id=aid, mssv=OWNER)), encoding="utf-8")
        return directory

    def parse(self, files, aid=AID):
        directory = self.paper({1: files}, aid=aid)
        return parse_versions(aid, [(1, directory / "v1")])

    def test_realistic_two_versions_union_and_shared_sentence(self):
        p = self.paper({
            1: {"main.tex": tex(r"\section{Intro}\input{sec/body}\bibliography{refs}"),
                "sec/body.tex": r"Shared \textbf{sentence}. See \cite{old}.",
                "unused.tex": b"\xff invalid unused auxiliary",
                "refs.bib": "@article{old,title={Exact title},author={Alice and Bob},year={2020}}"},
            2: {"main.tex": tex(r"\section{Changed}\input{sec/body}\bibliography{refs}"),
                "sec/body.tex": r"Shared sentence. See \cite{new}.",
                "refs.bib": "@article{new,title={Exact title},author={Alice and Bob},doi={10.1/test}}"},
        })
        result = parse_versions(AID, [(1, p / "v1"), (2, p / "v2")])
        self.assertEqual([r["status"] for r in result["versions"]], ["success", "success"])
        shared = [e for e in result["elements"] if e["content"] == "Shared sentence."]
        self.assertEqual(len(shared), 1)
        self.assertEqual({e["version"] for e in result["hierarchy"] if e["element_id"] == shared[0]["element_id"]}, {1, 2})
        self.assertEqual(len(result["bibtex"]), 1)
        self.assertEqual(result["bibtex"][0]["year"], 2020)
        self.assertEqual(result["bibtex"][0]["doi"], "10.1/test")
        self.assertTrue(any(r"\cite{old}" in n["content"] for n in result["elements"]))
        self.assertFalse(any(r"\cite{new}" in n["content"] for n in result["elements"]))
        self.assertNotIn("unused.tex", result["versions"][0]["included_files"])
        validate_tables(result)

    def test_comments_escaped_percent_and_include(self):
        result = self.parse({"main.tex": tex("%\\input{missing}\n" + r"Rate is 10\%. \input{part}"), "part.tex": "Included."})
        self.assertEqual(result["versions"][0]["status"], "success")
        self.assertTrue(any("Included." == n["content"] for n in result["elements"]))
        self.assertIn(r"\%", strip_comments(r"A\% B %gone"))


    def test_unique_main_with_explicit_independent_supplement(self):
        supplements = [
            tex(r"\section*{Supplemental Material}Separate supplement."),
            tex(r"\centering\noindent\Large{\textbf{{A study}\\Supplementary Information}}Separate."),
            r"\documentclass[manuscript=suppinfo]{achemso}\begin{document}Separate.\end{document}",
            r"\documentclass{article}\title{A study: Supplementary Information}\begin{document}Separate.\end{document}",
        ]
        for index, supplement in enumerate(supplements):
            with self.subTest(form=index), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                # The supplement sorts first and has the misleading main name.
                (root / "a-main.tex").write_text(supplement, encoding="utf-8")
                (root / "z.tex").write_text(tex(r"\section{Main}Main text.\input{part}\appendix\include{app}"), encoding="utf-8")
                (root / "part.tex").write_text(r"Included \cite{k}.\bibliography{refs}", encoding="utf-8")
                (root / "app.tex").write_text(r"\section{Extra}Appendix text.", encoding="utf-8")
                (root / "refs.bib").write_text("@article{k,title={Source reference},author={Alice}}", encoding="utf-8")
                result = parse_versions(AID, [(1, root)])
                record = result["versions"][0]
                self.assertEqual(record["status"], "success", record)
                self.assertEqual(record["root"], "z.tex")
                self.assertEqual(set(record["included_files"]), {"z.tex", "part.tex", "app.tex"})
                self.assertEqual(record["root_selection"]["excluded_supplements"][0]["file"], "a-main.tex")
                content = " ".join(n["content"] for n in result["elements"])
                self.assertIn("Main text.", content)
                self.assertIn("Appendix text.", content)
                self.assertIn(r"\cite{k}", content)
                self.assertNotIn("Separate", content)
                self.assertEqual([b["citation_key"] for b in result["bibtex"]], ["k"])
                self.assertEqual({e["version"] for e in result["hierarchy"]}, {1})
                validate_tables(result)

    def test_multiple_roots_remain_ambiguous_without_explicit_roles(self):
        undecidable = [
            tex("Another independent article."),
            tex(r"\section{Results}Main content.\section*{Supplemental Material}An appendix inside the main document."),
            r"\documentclass{article}\renewcommand{\title}[1]{}\title{Supplementary Information}\begin{document}Another article.\end{document}",
            tex("We discuss Supplementary Information in ordinary prose."),
            tex("%\\section*{Supplemental Material}\nAnother article."),
            r"\documentclass{article}\newcommand{\unused}{\title{Supplementary Information}}\begin{document}Another article.\end{document}",
            tex(r"\begin{verbatim}\section*{Supplemental Material}\end{verbatim}Another article."),
            r"\documentclass{article}\iffalse\title{Supplementary Information}\fi\begin{document}Another article.\end{document}",
        ]
        for index, second in enumerate(undecidable):
            with self.subTest(form=index), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                (root / "main.tex").write_text(tex("Main."), encoding="utf-8")
                (root / "SI.tex").write_text(second, encoding="utf-8")
                record = parse_versions(AID, [(1, root)])["versions"][0]
                self.assertEqual(record["status"], "parse_error")
                self.assertIn("found 2", record["error"])
                self.assertIsNone(record["root"])
                # A known supplement cannot resolve TWO remaining main candidates.
                (root / "extra.tex").write_text(tex(r"\section*{Supplemental Material}Extra."), encoding="utf-8")
                record = parse_versions(AID, [(1, root)])["versions"][0]
                self.assertIn("found 3", record["error"])

    def test_top10_grid_flush_font_keep_body_math_and_citations(self):
        source = tex(r"\flushbottom\normalfont{Opening \cite{k}.}"
                     r"\section{Results}Before.\onecolumngrid\appendix"
                     r"\section{Appendix}After $x=1$.\[y=2\]"
                     r"\begin{thebibliography}{1}\bibitem{k}Alice.\newblock Title.\newblock 2020.\end{thebibliography}")
        result = self.parse({"main.tex": source.replace("{article}", "{revtex4-2}")})
        self.assertEqual(result["versions"][0]["status"], "success", result["versions"])
        content = " ".join(n["content"] for n in result["elements"])
        for value in [r"Opening \cite{k}.", "Results", "Before.", "Appendix", "After $x=1$.", "y=2"]:
            self.assertIn(value, content)
        self.assertEqual([r["citation_key"] for r in result["bibtex"]], ["k"])
        validate_tables(result)

    def test_top10_font_declaration_does_not_consume_or_space_adjacent_text(self):
        result = self.parse({"main.tex": tex(r"Pre{\normalfont fix}post. Next \normalfont word.")})
        self.assertEqual(result["versions"][0]["status"], "success")
        self.assertEqual([n["content"] for n in result["elements"] if n["kind"] == "sentence"], ["Prefixpost.", "Next word."])

    def test_top10_definecolor_balanced_literal_declarations_preserve_following_text(self):
        for args in ("{c}{rgb}{.1,0.2,1}", "{c}{RGB}{127,0.0,255}", "{c}{gray}{.95}", "[named]{c}{cmyk}{0,1,.3,0}", "{c}{named}{Blue}"):
            with self.subTest(args=args), tempfile.TemporaryDirectory() as td:
                root=Path(td);(root/"main.tex").write_text(tex(r"Before.\definecolor"+args+r"\textcolor{c}{Kept {nested} text.}After."),encoding="utf-8")
                result=parse_versions(AID,[(1,root)]);record=result["versions"][0]
                self.assertEqual(record["status"],"success",record)
                content=" ".join(n["content"] for n in result["elements"])
                self.assertIn("Kept nested text.",content);self.assertIn("After.",content)
                self.assertEqual(record["layout_metadata"][0]["command"],"definecolor")

    def test_top10_layout_declarations_keep_local_definitions_and_reject_unknowns(self):
        for command in ("normalfont","flushbottom","onecolumngrid","definecolor"):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as td:
                root=Path(td);source=r"\documentclass{article}\newcommand{"+chr(92)+command+r"}{Visible.}\begin{document}"+chr(92)+command+r" After.\end{document}"
                (root/"main.tex").write_text(source,encoding="utf-8")
                result=parse_versions(AID,[(1,root)])
                self.assertEqual(result["versions"][0]["status"],"success",result["versions"])
                self.assertIn("Visible.",[n["content"] for n in result["elements"]])
        for body in (r"\onecolumngrid Body.",r"\definecolor{c}{rgb}{\unknown,0,0}Body.",r"\definecolor{c}{rgb}{2,0,0}Body.",r"\definecolor[ps]{c}{rgb}{code}Body.",r"\definecolor{c}{weird}{x}Body.",r"\definecolor{c}{gray}Body."):
            with self.subTest(body=body),tempfile.TemporaryDirectory() as td:
                root=Path(td);(root/"main.tex").write_text(tex(body),encoding="utf-8")
                self.assertEqual(parse_versions(AID,[(1,root)])["versions"][0]["status"],"parse_error")

    def test_top10_altaffiliation_keeps_optional_and_nested_metadata_before_abstract(self):
        source=tex(r"\author{Alice}\altaffiliation[Also at ]{Institute {A}, City.}"
                   r"\begin{abstract}Actual abstract.\end{abstract}\maketitle\section{Intro}Actual body.")
        result=self.parse({"main.tex":source.replace("{article}","{revtex4-2}")})
        record=result["versions"][0];self.assertEqual(record["status"],"success",record)
        content=" ".join(n["content"] for n in result["elements"])
        self.assertIn("Actual abstract.",content);self.assertIn("Actual body.",content)
        self.assertNotIn("Institute",content)
        metadata=next(m for m in record["layout_metadata"] if m["command"]=="altaffiliation")
        self.assertEqual(metadata["prefix"],"Also at ");self.assertEqual(metadata["arguments"],["Institute {A}, City."])

    def test_top10_altaffiliation_rejects_body_usage_and_missing_argument(self):
        for body in (r"\section{Intro}\altaffiliation{Do not hide this.}",r"\altaffiliation[Also at ]\section{Intro}"):
            with self.subTest(body=body),tempfile.TemporaryDirectory() as td:
                root=Path(td);(root/"main.tex").write_text(tex(body).replace("{article}","{revtex4-2}"),encoding="utf-8")
                self.assertEqual(parse_versions(AID,[(1,root)])["versions"][0]["status"],"parse_error")

    def test_top10_ieee_repeated_title_does_not_consume_abstract_or_section(self):
        result=self.parse({"main.tex":tex(r"\IEEEpeerreviewmaketitle\maketitle\IEEEpeerreviewmaketitle\begin{abstract}Summary.\end{abstract}\section{Intro}Real body.").replace("{article}","{IEEEtran}")})
        self.assertEqual(result["versions"][0]["status"],"success",result["versions"])
        content=" ".join(n["content"] for n in result["elements"])
        self.assertIn("Summary.",content);self.assertIn("Intro",content);self.assertIn("Real body.",content)
        self.assertEqual(sum(m["command"]=="IEEEpeerreviewmaketitle" for m in result["versions"][0]["layout_metadata"]),2)

    def test_top10_qed_preserves_marker_math_and_following_content(self):
        result=self.parse({"main.tex":tex(r"\begin{proof}Proof text.\[x=1\]\qed\end{proof}After proof.").replace("{article}","{amsart}")})
        self.assertEqual(result["versions"][0]["status"],"success",result["versions"])
        content=" ".join(n["content"] for n in result["elements"])
        for value in ("Proof text.","x=1",r"\qed{}","After proof."):self.assertIn(value,content)
        validate_tables(result)

    def test_top10_template_commands_keep_local_semantics_and_unknown_class_errors(self):
        for name in ("altaffiliation","IEEEpeerreviewmaketitle","qed"):
            with self.subTest(local=name),tempfile.TemporaryDirectory() as td:
                root=Path(td);src=r"\documentclass{article}\newcommand{"+chr(92)+name+r"}{Local content.}\begin{document}"+chr(92)+name+r" After.\end{document}"
                (root/"main.tex").write_text(src,encoding="utf-8");result=parse_versions(AID,[(1,root)])
                self.assertEqual(result["versions"][0]["status"],"success",result["versions"])
                self.assertIn("Local content.",[n["content"] for n in result["elements"]])
            with self.subTest(undeclared=name),tempfile.TemporaryDirectory() as td:
                root=Path(td);(root/"main.tex").write_text(tex(chr(92)+name+r"{Unknown.} After."),encoding="utf-8")
                self.assertEqual(parse_versions(AID,[(1,root)])["versions"][0]["status"],"parse_error")

    def test_top10_onlinecite_is_collected_aliased_and_sentence_protected(self):
        directory=self.paper({
            1:{"main.tex":tex(r"See \onlinecite[Ch. 2][p. 3]{old}. Next sentence.\bibliography{refs}").replace("{article}","{revtex4-2}"),"refs.bib":"@article{old,title={Same evidence},author={Alice},year={2020}}"},
            2:{"main.tex":tex(r"See \onlinecite[Ch. 2][p. 3]{new}. Next sentence.\bibliography{refs}").replace("{article}","{revtex4-2}"),"refs.bib":"@article{new,title={Same evidence},author={Alice},doi={10.1/example}}"},
        })
        result=parse_versions(AID,[(1,directory/"v1"),(2,directory/"v2")])
        self.assertEqual([r["status"] for r in result["versions"]],["success","success"],result["versions"])
        self.assertEqual(len(result["bibtex"]),1);self.assertEqual(result["bibtex"][0]["citation_key"],"old")
        nodes=[n for n in result["elements"] if n["kind"]=="sentence"]
        self.assertEqual({n["content"] for n in nodes},{r"See \onlinecite[Ch. 2][p. 3]{old}.","Next sentence."})
        self.assertEqual(len(nodes),2)
        validate_tables(result)

    def test_top10_onlinecite_checks_missing_dynamic_keys_and_macro_revealed_citations(self):
        for body in (r"Only \onlinecite{missing}.",r"\onlinecite{\unknown}",r"\onlinecite{}"):
            with self.subTest(body=body),tempfile.TemporaryDirectory() as td:
                root=Path(td);(root/"main.tex").write_text(tex(body).replace("{article}","{revtex4-2}"),encoding="utf-8")
                self.assertEqual(parse_versions(AID,[(1,root)])["versions"][0]["status"],"parse_error")
        source=r"\documentclass{revtex4-2}\newcommand{\linkcite}{\onlinecite{missing}}\begin{document}See \linkcite.\end{document}"
        result=self.parse({"main.tex":source})
        self.assertIn("after expansion",result["versions"][0]["error"])

    def test_top10_hyperref_keeps_displayed_nested_text_math_citation_and_ref_star(self):
        source=(r"\documentclass{article}\usepackage{hyperref}\begin{document}"
                r"See \hyperref[sec:target]{\textbf{visible words} $x=1$ \cite{k} and \ref*{sec:target}}. After."
                r"\section{Target}\label{sec:target}Body."
                r"\begin{thebibliography}{1}\bibitem{k}Alice.\newblock Evidence.\newblock 2020.\end{thebibliography}\end{document}")
        result=self.parse({"main.tex":source});record=result["versions"][0]
        self.assertEqual(record["status"],"success",record)
        content=" ".join(n["content"] for n in result["elements"])
        for value in ("visible words",r"$x=1$",r"\cite{k}",r"\ref*{sec:target}","After.","Target","Body."):self.assertIn(value,content)
        self.assertNotIn(r"\hyperref",content)
        evidence=next(m for m in record["layout_metadata"] if m["command"]=="hyperref")
        self.assertEqual(evidence["targets"],["sec:target"])
        self.assertEqual([b["citation_key"] for b in result["bibtex"]],["k"])
        validate_tables(result)

    def test_top10_hyperref_long_form_retains_visible_text_and_destination_evidence(self):
        source=r"\documentclass{article}\usepackage{hyperref}\begin{document}Before \hyperref{https://example.org}{category}{target}{\emph{visible}} after.\end{document}"
        result=self.parse({"main.tex":source});record=result["versions"][0]
        self.assertEqual(record["status"],"success",record)
        self.assertIn("Before visible after.",[n["content"] for n in result["elements"]])
        self.assertEqual(record["layout_metadata"][0]["targets"],["https://example.org","category","target"])

    def test_top10_hyperref_missing_arguments_or_unknown_visible_content_fail(self):
        for body in (r"\hyperref[label]",r"\hyperref[\dynamic]{Visible}",r"\hyperref[label]{\unsupported{Hidden}}",r"\hyperref{url}{cat}{target}"):
            with self.subTest(body=body),tempfile.TemporaryDirectory() as td:
                root=Path(td);(root/"main.tex").write_text(r"\documentclass{article}\usepackage{hyperref}\begin{document}"+body+r"\end{document}",encoding="utf-8")
                self.assertEqual(parse_versions(AID,[(1,root)])["versions"][0]["status"],"parse_error")

    def test_top10_cvpr_supplementary_title_keeps_division_and_included_appendix(self):
        source=(r"\documentclass{article}\usepackage{cvpr}\title{Paper title}\begin{document}"
                r"\section{Main}Body.\section*{References}Excluded reference prose."
                r"\maketitlesupplementary\input{extra}\end{document}")
        result=self.parse({"main.tex":source,"extra.tex":r"Supplement text.\subsection{Details}Detail \cite{k}.\[z=3\]\begin{thebibliography}{1}\bibitem{k}Alice.\newblock Supplement evidence.\newblock 2020.\end{thebibliography}"})
        record=result["versions"][0];self.assertEqual(record["status"],"success",record)
        content=" ".join(n["content"] for n in result["elements"])
        for value in ("Supplementary Material","Supplement text.","Details",r"\cite{k}","z=3"):self.assertIn(value,content)
        self.assertNotIn("Excluded reference prose.",content)
        marker=next(n for n in result["elements"] if n["content"]=="Supplementary Material")
        root=next(n for n in result["elements"] if n["kind"]=="document")
        self.assertTrue(any(e["element_id"]==marker["element_id"] and e["parent_id"]==root["element_id"] for e in result["hierarchy"]))
        self.assertIn("extra.tex",record["included_files"]);self.assertTrue(record["normalization_notes"])
        validate_tables(result)

    def test_top10_cvpr_supplementary_title_inside_column_heading_keeps_following_content(self):
        result=self.parse({"main.tex":r"\documentclass{article}\usepackage{cvpr}\begin{document}Main.\twocolumn[{\maketitlesupplementary\begin{center}Caption text.\end{center}}]\section{Extra}After.\end{document}"})
        self.assertEqual(result["versions"][0]["status"],"success",result["versions"])
        content=" ".join(n["content"] for n in result["elements"])
        for value in ("Main.","Supplementary Material","Caption text.","Extra","After."):self.assertIn(value,content)

    def test_top10_cvpr_supplementary_title_requires_package_and_keeps_local_definition(self):
        result=self.parse({"main.tex":tex(r"\maketitlesupplementary After.")})
        self.assertEqual(result["versions"][0]["status"],"parse_error")
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/"main.tex").write_text(r"\documentclass{article}\usepackage{cvpr}\newcommand{\maketitlesupplementary}{Local title.}\begin{document}\maketitlesupplementary After.\end{document}",encoding="utf-8")
            result=parse_versions(AID,[(1,root)])
            self.assertEqual(result["versions"][0]["status"],"success",result["versions"])
            content=" ".join(n["content"] for n in result["elements"])
            self.assertIn("Local title.",content);self.assertNotIn("Supplementary Material",content)

    def test_top10_qed_custom_symbol_keeps_source_defined_words_and_math(self):
        source=r"\documentclass{amsart}\renewcommand{\qedsymbol}{End $x=2$.}\begin{document}Proof.\qed After.\end{document}"
        result=self.parse({"main.tex":source});record=result["versions"][0]
        self.assertEqual(record["status"],"success",record)
        content=" ".join(n["content"] for n in result["elements"])
        self.assertIn("End $x=2$.",content);self.assertIn("After.",content)

    def test_batch10_g1_literal_list_setup_keeps_labels_items_math_and_citation(self):
        for prefix in (r"\setlength{\leftskip}{-1.8em}",r"\setlength{\itemsep}{2pt}",r"\itemsep 5pt"):
            with self.subTest(prefix=prefix),tempfile.TemporaryDirectory() as td:
                root=Path(td);(root/"main.tex").write_text(tex(r"\begin{enumerate}"+prefix+r"\item[(i)] First $x=1$ \cite{k}.\item Second.\end{enumerate}After.\begin{thebibliography}{1}\bibitem{k}A.\newblock Evidence.\end{thebibliography}"),encoding="utf-8")
                result=parse_versions(AID,[(1,root)]);self.assertEqual(result["versions"][0]["status"],"success",result["versions"])
                self.assertEqual(len([n for n in result["elements"] if n["kind"]=="item"]),2)
                content=" ".join(n["content"] for n in result["elements"])
                for word in ("(i)","First $x=1$",r"\cite{k}","Second.","After."):self.assertIn(word,content)
                validate_tables(result)

    def test_batch10_g1_does_not_drop_unknown_list_prefix_or_dynamic_length(self):
        for prefix in ("Unexplained text.",r"\unknown{Important}",r"\setlength{\itemsep}{\dynamic}",r"\itemsep \unknown"):
            with self.subTest(prefix=prefix),tempfile.TemporaryDirectory() as td:
                root=Path(td);(root/"main.tex").write_text(tex(r"\begin{itemize}"+prefix+r"\item Text.\end{itemize}"),encoding="utf-8")
                self.assertEqual(parse_versions(AID,[(1,root)])["versions"][0]["status"],"parse_error")

    def test_batch10_g1_nested_list_without_prefix_still_preserves_structure(self):
        result=self.parse({"main.tex":tex(r"\begin{itemize}\item First.\begin{enumerate}\setlength{\itemsep}{1pt}\item Child.\end{enumerate}\item Last.\end{itemize}")})
        self.assertEqual(result["versions"][0]["status"],"success",result["versions"])
        self.assertEqual(len([n for n in result["elements"] if n["kind"]=="item"]),3);validate_tables(result)

    def test_batch10_g2_keywords_templates_keep_math_citations_and_following_section(self):
        for cls,pre in (("interact",""),("article",r"\usepackage{spconf}"),("l4dc2025","")):
            with self.subTest(cls=cls),tempfile.TemporaryDirectory() as td:
                root=Path(td);source=r"\documentclass{"+cls+"}"+pre+r"\begin{document}\begin{abstract}Summary.\end{abstract}\begin{keywords}BaTiO$_3$; Learning \cite{k}.\end{keywords}\section{Intro}Body.\begin{thebibliography}{1}\bibitem{k}A.\newblock Evidence.\end{thebibliography}\end{document}"
                (root/"main.tex").write_text(source,encoding="utf-8");result=parse_versions(AID,[(1,root)])
                self.assertEqual(result["versions"][0]["status"],"success",result["versions"])
                content=" ".join(n["content"] for n in result["elements"])
                for value in ("keywords","BaTiO$_3$",r"\cite{k}","Summary.","Intro","Body."):self.assertIn(value,content)
                validate_tables(result)

    def test_batch10_g2_unknown_keyword_content_does_not_become_success(self):
        result=self.parse({"main.tex":tex(r"\begin{keywords}\unknown{Important}\end{keywords}").replace("{article}","{interact}")})
        self.assertEqual(result["versions"][0]["status"],"parse_error")

    def test_batch10_g2_keyword_options_are_not_discarded(self):
        result=self.parse({"main.tex":tex(r"\begin{keywords}[Important option]Words.\end{keywords}").replace("{article}","{l4dc2025}")})
        self.assertEqual(result["versions"][0]["status"],"parse_error")

    def test_batch10_g3_jhep_bbl_wrapper_preserves_keys_raw_and_citations(self):
        from src.m2.bibliography import parse_bbl
        for prefix,suffix in ((r"\begingroup\raggedright",r"\endgroup"),(r"\providecommand{\href}[2]{#2}\begingroup\raggedright",r"\endgroup"),(r"\begingroup\raggedright",r"\endgroup\endinput")):
            with self.subTest(prefix=prefix,suffix=suffix):
                bbl=prefix+r"\begin{thebibliography}{2}\bibitem{a}Alice.\newblock Real title.\newblock 2021.\bibitem{b}Bob.\newblock Other title.\end{thebibliography}"+suffix
                entries=parse_bbl(bbl);self.assertEqual([e["key"] for e in entries],["a","b"])
                self.assertIn("Real title",entries[0]["raw"])
                with tempfile.TemporaryDirectory() as td:
                    root=Path(td);(root/"main.tex").write_text(tex(r"Text \cite{a,b}.\bibliography{absent}"),encoding="utf-8");(root/"main.bbl").write_text(bbl,encoding="utf-8")
                    result=parse_versions(AID,[(1,root)]);self.assertEqual(result["versions"][0]["status"],"success",result["versions"])
                    self.assertEqual(len(result["bibtex"]),2);validate_tables(result)

    def test_batch10_g3_unbalanced_layout_group_is_an_error(self):
        from src.m2.bibliography import parse_bbl
        for before,after in ((r"\begingroup\raggedright",""),("",r"\endgroup"),(r"\begingroup\raggedright",r"\endgroup Important text.")):
            with self.subTest(before=before,after=after),self.assertRaises(ParseError):
                parse_bbl(before+r"\begin{thebibliography}{1}\bibitem{k}Evidence.\end{thebibliography}"+after)

    def test_batch10_g3_arbitrary_setup_or_structural_group_content_is_not_dropped(self):
        from src.m2.bibliography import parse_bbl
        for setup in (r"\begingroup\unknown",r"\begingroup\raggedright\input{other}",r"\begingroup\raggedright\renewcommand{\bibitem}[1]{}"):
            with self.subTest(setup=setup),self.assertRaises(ParseError):
                parse_bbl(setup+r"\begin{thebibliography}{1}\bibitem{k}Evidence.\end{thebibliography}\endgroup")

    def test_batch10_g4_exact_identity_url_and_href_setup_keep_all_items(self):
        from src.m2.bibliography import parse_bbl
        guards=[r"\expandafter\ifx\csname bibnamefont\endcsname\relax\def\bibnamefont#1{#1}\fi",
                r"\expandafter\ifx\csname url\endcsname\relax\def\url#1{\texttt{#1}}\fi",
                r"\expandafter\ifx\csname href\endcsname\relax\def\href#1#2{#2}\def\path#1{#1}\fi"]
        for guard in guards:
            with self.subTest(guard=guard):
                entries=parse_bbl(r"\begin{thebibliography}{2}"+guard+r"\bibitem{k}Alice.\newblock Evidence.\newblock \url{https://example.org/a%20b}\bibitem{z}Other.\end{thebibliography}")
                self.assertEqual([e["key"] for e in entries],["k","z"])
                self.assertIn("a%20b",entries[0]["raw"])

    def test_batch10_g4_guard_cannot_hide_structural_changes_or_other_definitions(self):
        from src.m2.bibliography import parse_bbl
        for guard in (r"\expandafter\ifx\csname bibnamefont\endcsname\relax\def\bibitem#1{#1}\fi",r"\expandafter\ifx\csname url\endcsname\relax\def\url#1{\input{hidden}}\fi",r"\expandafter\ifx\csname href\endcsname\relax\bibitem{hidden}Hidden.\fi"):
            with self.subTest(guard=guard),self.assertRaises(ParseError):
                parse_bbl(r"\begin{thebibliography}{1}"+guard+r"\bibitem{k}Kept.\end{thebibliography}")

    def test_batch10_g4_inline_identity_guard_keeps_bibliography_out_of_tree(self):
        source=tex(r"Body \cite{k}.\begin{thebibliography}{1}\expandafter\ifx\csname citenamefont\endcsname\relax\def\citenamefont#1{#1}\fi\bibitem{k}Alice.\newblock Separate reference title.\end{thebibliography}")
        result=self.parse({"main.tex":source});self.assertEqual(result["versions"][0]["status"],"success",result["versions"])
        self.assertEqual(len(result["bibtex"]),1)
        self.assertNotIn("Separate reference title"," ".join(n["content"] for n in result["elements"]))

    def test_batch10_g5_literal_listing_protects_percent_tex_roots_inputs_and_citations(self):
        code="\n    x = 10 % 3\n"+r"\input{missing}\cite{not-real}\begin{document}\documentclass{fake}"+"\n"
        result=self.parse({"main.tex":r"\documentclass{article}\usepackage{listings}\lstset{frame=single,basicstyle=\small\ttfamily}\begin{document}Before.\begin{lstlisting}"+code+r"\end{lstlisting}After.\end{document}"})
        self.assertEqual(result["versions"][0]["status"],"success",result["versions"])
        leaf=next(n for n in result["elements"] if n["kind"]=="verbatim")
        self.assertEqual(leaf["content"],r"\begin{lstlisting}"+code+r"\end{lstlisting}")
        self.assertEqual(result["bibtex"],[])
        self.assertIn("After.",[n["content"] for n in result["elements"]]);validate_tables(result)

    def test_batch10_g5_listing_caption_is_content_and_collects_real_citation(self):
        source=r"\documentclass{article}\usepackage{listings}\begin{document}\begin{lstlisting}[caption={Actual \cite{k}},mathescape=false]"+"\n{code}\n"+r"\end{lstlisting}\begin{thebibliography}{1}\bibitem{k}A.\newblock Caption evidence.\end{thebibliography}\end{document}"
        result=self.parse({"main.tex":source});self.assertEqual(result["versions"][0]["status"],"success",result["versions"])
        self.assertEqual(len(result["bibtex"]),1);self.assertTrue(any(r"\cite{k}" in n["content"] for n in result["elements"] if n["kind"]=="sentence"))

    def test_batch10_g5_active_tex_escape_unknown_style_or_missing_end_remain_errors(self):
        for option in ("mathescape",r"escapeinside={(*@}{@*)}","style=unknown",r"literate={a}{b}1"):
            with self.subTest(option=option),tempfile.TemporaryDirectory() as td:
                root=Path(td);(root/"main.tex").write_text(r"\documentclass{article}\usepackage{listings}\lstset{"+option+r"}\begin{document}\begin{lstlisting}"+"\ncode\n"+r"\end{lstlisting}\end{document}",encoding="utf-8")
                record=parse_versions(AID,[(1,root)])["versions"][0];self.assertEqual(record["status"],"parse_error");self.assertIn("lstlisting",record["error"])
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/"main.tex").write_text(r"\documentclass{article}\usepackage{listings}\begin{document}\begin{lstlisting}unfinished\end{document}",encoding="utf-8")
            self.assertEqual(parse_versions(AID,[(1,root)])["versions"][0]["status"],"parse_error")

    def test_batch10_g6_uppercase_expandafter_literal_roman_and_source_macro(self):
        for number,roman in ((2,"II"),(4,"IV"),(9,"IX")):
            with self.subTest(number=number),tempfile.TemporaryDirectory() as td:
                root=Path(td);src=r"\documentclass{article}\newcommand{\RNum}[1]{\uppercase\expandafter{\romannumeral #1\relax}}\begin{document}Section \RNum{"+str(number)+r"} keeps $x=1$ \cite{k}.\begin{thebibliography}{1}\bibitem{k}A.\newblock Evidence.\end{thebibliography}\end{document}"
                (root/"main.tex").write_text(src,encoding="utf-8");result=parse_versions(AID,[(1,root)])
                self.assertEqual(result["versions"][0]["status"],"success",result["versions"])
                content=" ".join(n["content"] for n in result["elements"]);self.assertIn("Section "+roman,content);self.assertIn(r"\cite{k}",content);self.assertIn("$x=1$",content)

    def test_batch10_g6_plain_ascii_uppercase_keeps_following_content(self):
        result=self.parse({"main.tex":tex(r"\uppercase{Plain {nested} text}. After.")})
        self.assertEqual(result["versions"][0]["status"],"success",result["versions"])
        self.assertIn("PLAIN NESTED TEXT.",[n["content"] for n in result["elements"]]);self.assertIn("After.",[n["content"] for n in result["elements"]])

    def test_batch10_g6_uppercase_does_not_guess_dynamic_roman_or_mutate_keys(self):
        for body in (r"\uppercase\expandafter{\romannumeral\year}",r"\uppercase{\cite{KeepCase}}",r"\uppercase{$x$}",r"\uppercase\expandafter{\romannumeral 2+3}"):
            with self.subTest(body=body),tempfile.TemporaryDirectory() as td:
                root=Path(td);(root/"main.tex").write_text(tex(body),encoding="utf-8");record=parse_versions(AID,[(1,root)])["versions"][0]
                self.assertEqual(record["status"],"parse_error")

    def test_batch10_g7_discretionary_hyphen_joins_word_without_losing_real_hyphen(self):
        for source,expected in ((r"an\-ti","anti"),(r"Nonlocal\--CPD","Nonlocal-CPD"),(r"NR\-EFTs","NREFTs")):
            with self.subTest(source=source),tempfile.TemporaryDirectory() as td:
                root=Path(td);(root/"main.tex").write_text(tex(source+" is retained."),encoding="utf-8");result=parse_versions(AID,[(1,root)])
                self.assertEqual(result["versions"][0]["status"],"success",result["versions"])
                self.assertIn(expected+" is retained.",[n["content"] for n in result["elements"]])

    def test_batch10_g7_discretionary_hyphen_keeps_citation_math_and_space(self):
        result=self.parse({"main.tex":tex(r"Non\-local text $x-y$ \cite{k}. End\- word.\begin{thebibliography}{1}\bibitem{k}A.\newblock Evidence.\end{thebibliography}")})
        self.assertEqual(result["versions"][0]["status"],"success",result["versions"])
        content=" ".join(n["content"] for n in result["elements"])
        self.assertIn(r"Nonlocal text $x-y$ \cite{k}.",content);self.assertIn("End word.",content)

    def test_batch10_g7_literal_code_keeps_backslash_hyphen(self):
        result=self.parse({"main.tex":tex(r"\begin{verbatim}an\-ti\end{verbatim}After.")})
        self.assertEqual(result["versions"][0]["status"],"success")
        self.assertIn(r"an\-ti",next(n["content"] for n in result["elements"] if n["kind"]=="verbatim"))

    def test_batch10_g8_exact_acm_bmc_metadata_defaults_do_not_change_item_count(self):
        from src.m2.bibliography import parse_bbl
        for guard in (r"\ifx\showDOI\undefined\def\showDOI#1{#1}\fi",r"\ifx\bvolume\undefined\def\bvolume#1{\textbf{#1}}\fi",r"\ifx\doiurl\undefined\def\doiurl#1{\url{https://doi.org/#1}}\fi"):
            with self.subTest(guard=guard):
                entries=parse_bbl(r"\begin{thebibliography}{2}"+guard+r"\bibitem{k}Alice.\newblock Actual title.\bibitem{z}Bob.\newblock Other title.\end{thebibliography}")
                self.assertEqual([e["key"] for e in entries],["k","z"]);self.assertEqual(entries[0]["title"],"Actual title")

    def test_batch10_g8_ifx_with_items_unknown_branch_or_changed_target_is_rejected(self):
        from src.m2.bibliography import parse_bbl
        for guard in (r"\ifx\showDOI\undefined\def\bibitem#1{#1}\fi",r"\ifx\showDOI\undefined\def\showDOI#1{\input{missing}}\fi",r"\ifx\showDOI\undefined\def\showDOI#1{#1}\else\bibitem{hidden}Hidden.\fi",r"\ifx\unknown\undefined\def\unknown#1{#1}\fi"):
            with self.subTest(guard=guard),self.assertRaises(ParseError):
                parse_bbl(r"\begin{thebibliography}{1}"+guard+r"\bibitem{k}Evidence.\end{thebibliography}")

    def test_batch10_g8_style_macro_content_is_retained_raw_without_invented_fields(self):
        from src.m2.bibliography import parse_bbl
        entries=parse_bbl(r"\begin{thebibliography}{1}\ifx\batitle\undefined\def\batitle#1{#1}\fi\bibitem{k}\batitle{Opaque title} \byear{2020}.\end{thebibliography}")
        self.assertIn(r"\batitle{Opaque title}",entries[0]["raw"]);self.assertIsNone(entries[0]["title"])

    def test_batch10_g9_three_frontmatter_templates_keep_title_abstract_and_body(self):
        for cls,extra in (("autart",r"\author[a]{Alice}\ead{public@example.org},\address[a]{Institute}"),("ecai",r"\paperid{123}\author[A]{\fnms{Alice}\snm{Smith}}\address[A]{Institute}"),("imsart",r"\author{Alice}\runtitle{Short title}")):
            with self.subTest(cls=cls),tempfile.TemporaryDirectory() as td:
                root=Path(td);source=r"\documentclass{"+cls+r"}\begin{document}\begin{frontmatter}\title{Actual {nested} title}"+extra+r"\begin{abstract}Kept abstract $x=1$ \cite{k}.\end{abstract}\end{frontmatter}\section{Intro}Body.\begin{thebibliography}{1}\bibitem{k}A.\newblock Evidence.\end{thebibliography}\end{document}"
                (root/"main.tex").write_text(source,encoding="utf-8");result=parse_versions(AID,[(1,root)]);record=result["versions"][0]
                self.assertEqual(record["status"],"success",record)
                self.assertTrue(any(m["command"]=="title" and m["arguments"]==["Actual {nested} title"] for m in record["layout_metadata"]))
                content=" ".join(n["content"] for n in result["elements"])
                for word in ("abstract",r"$x=1$",r"\cite{k}","Intro","Body."):self.assertIn(word,content)
                validate_tables(result)

    def test_batch10_g9_ims_keywords_and_frontmatter_note_remain_content(self):
        source=r"\documentclass{imsart}\begin{document}\begin{frontmatter}\title{Title}\footnotetext[1]{Funding note.}\begin{abstract}Summary.\end{abstract}\begin{keyword}\kwd{First term}\kwd{Second term}\end{keyword}\end{frontmatter}After.\end{document}"
        result=self.parse({"main.tex":source});self.assertEqual(result["versions"][0]["status"],"success",result["versions"])
        content=" ".join(n["content"] for n in result["elements"])
        for text in ("Funding note.","First term","Second term","After."):self.assertIn(text,content)

    def test_batch10_g9_frontmatter_unknown_body_is_not_silently_metadata(self):
        source=r"\documentclass{ecai}\begin{document}\begin{frontmatter}\title{Title}\begin{abstract}\unsupported{Real content}\end{abstract}\end{frontmatter}\end{document}"
        result=self.parse({"main.tex":source});self.assertEqual(result["versions"][0]["status"],"parse_error");self.assertIn("unsupported",result["versions"][0]["error"])

    def test_batch10_g10_inline_enumeration_keeps_item_order_math_and_citation(self):
        for option in ("","[1)]","[(1)]"):
            with self.subTest(option=option),tempfile.TemporaryDirectory() as td:
                root=Path(td);(root/"main.tex").write_text(r"\documentclass{article}\usepackage{paralist}\begin{document}Before \begin{inparaenum}"+option+r"\item First $x=1$ \cite{k};\item Second.\end{inparaenum} after.\begin{thebibliography}{1}\bibitem{k}A.\newblock Evidence.\end{thebibliography}\end{document}",encoding="utf-8")
                result=parse_versions(AID,[(1,root)]);record=result["versions"][0];self.assertEqual(record["status"],"success",record)
                nodes=result["elements"];items=[n for n in nodes if n["kind"]=="item"];self.assertEqual(len(items),2)
                content=" ".join(n["content"] for n in nodes)
                for value in ("Before",r"First $x=1$ \cite{k}","Second.","after."):self.assertIn(value,content)
                self.assertLess(next(i for i,n in enumerate(nodes) if "First $x" in n["content"]),next(i for i,n in enumerate(nodes) if n["content"]=="Second."));validate_tables(result)

    def test_batch10_g10_explicit_labels_and_nested_inline_list_remain_distinct(self):
        source=r"\documentclass{article}\usepackage{paralist}\begin{document}\begin{inparaenum}\item[(i)] First.\begin{inparaenum}\item Child.\end{inparaenum}\item[(ii)] Last.\end{inparaenum}\end{document}"
        result=self.parse({"main.tex":source});self.assertEqual(result["versions"][0]["status"],"success",result["versions"])
        items=[n["content"] for n in result["elements"] if n["kind"]=="item"];self.assertEqual(items,["(i)","item 1","(ii)"]);validate_tables(result)

    def test_batch10_g10_unknown_package_options_or_body_do_not_get_dropped(self):
        for pre,body in (("",r"\begin{inparaenum}\item A.\end{inparaenum}"),(r"\usepackage{paralist}",r"\begin{inparaenum}[\dynamic]\item A.\end{inparaenum}"),(r"\usepackage{paralist}",r"\begin{inparaenum}\item \unknown{Important}\end{inparaenum}")):
            with self.subTest(body=body),tempfile.TemporaryDirectory() as td:
                root=Path(td);(root/"main.tex").write_text(r"\documentclass{article}"+pre+r"\begin{document}"+body+r"\end{document}",encoding="utf-8")
                self.assertEqual(parse_versions(AID,[(1,root)])["versions"][0]["status"],"parse_error")

    def test_batch10_g5_style_cannot_hide_displayed_words_or_executable_hooks(self):
        for value in (r"\small Hidden words",r"\color{blue}{Hidden text}",r"\input{missing}"):
            with self.subTest(value=value),tempfile.TemporaryDirectory() as td:
                root=Path(td);(root/"main.tex").write_text(r"\documentclass{article}\usepackage{listings}\lstset{basicstyle={"+value+r"}}\begin{document}\begin{lstlisting}"+"\ncode\n"+r"\end{lstlisting}\end{document}",encoding="utf-8")
                record=parse_versions(AID,[(1,root)])["versions"][0];self.assertEqual(record["status"],"parse_error")

    def test_errors3_r1_supporting_information_subtitle_is_source_evidence(self):
        result=self.parse({"a.tex":tex(r"\section{Main}Actual text.\input{part}"),"b.tex":r"\documentclass{scrartcl}\title{Paper}\subtitle{Supporting Information}\begin{document}Independent extra.\end{document}","part.tex":r"\appendix\section{Appendix}Retained."})
        self.assertEqual(result["versions"][0]["status"],"success",result["versions"]);self.assertEqual(result["versions"][0]["root"],"a.tex")
        content=" ".join(n["content"] for n in result["elements"]);self.assertIn("Retained.",content);self.assertNotIn("Independent extra.",content)

    def test_errors3_r1_two_article_variants_remain_ambiguous(self):
        result=self.parse({"main.tex":r"\documentclass{article}\title{Arxiv paper}\begin{document}One.\end{document}","conference/paper.tex":r"\documentclass{article}\title{Conference paper}\begin{document}Two.\end{document}"})
        self.assertIn("found 2",result["versions"][0]["error"])

    def test_errors3_r1_supporting_information_in_prose_is_not_a_role(self):
        result=self.parse({"a.tex":tex("Main."),"SI.tex":tex("The Supporting Information is discussed here.")})
        self.assertIn("found 2",result["versions"][0]["error"])

    def test_errors3_r1_unrelated_let_does_not_hide_literal_subtitle(self):
        for alias in (r"\let\oldAA\AA",r"\let\cite=\supercite"):
            with self.subTest(alias=alias),tempfile.TemporaryDirectory() as td:
                root=Path(td);(root/"a.tex").write_text(tex("Main."),encoding="utf-8");(root/"b.tex").write_text(r"\documentclass{scrartcl}"+alias+r"\title{Paper}\subtitle{Supporting Information}\begin{document}Extra.\end{document}",encoding="utf-8")
                record=parse_versions(AID,[(1,root)])["versions"][0];self.assertEqual(record["status"],"success",record);self.assertEqual(record["root"],"a.tex")
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/"a.tex").write_text(tex("Main."),encoding="utf-8");(root/"b.tex").write_text(r"\documentclass{article}\let\subtitle\unknown\subtitle{Supporting Information}\begin{document}Extra.\end{document}",encoding="utf-8")
            self.assertIn("found 2",parse_versions(AID,[(1,root)])["versions"][0]["error"])

    def test_duplicate_roots_fail_explicitly(self):
        result = self.parse({"a.tex": tex("A."), "b.tex": tex("B.")})
        self.assertIn("found 2", result["versions"][0]["error"])
        self.assertEqual(result["elements"], [])

    def test_include_cycle_and_traversal(self):
        for include in ("main", "../other"):
            with self.subTest(include=include), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                (root / "main.tex").write_text(tex(r"\input{" + include + "}"))
                with self.assertRaises(ParseError):
                    Gatherer(root).gather()

    def test_includeonly_skips_omitted_file(self):
        result = self.parse({"main.tex": r"\documentclass{article}\includeonly{one}\begin{document}\include{one}\include{missing}\end{document}", "one.tex": "One."})
        self.assertEqual(result["versions"][0]["status"], "success")

    def test_missing_include_is_not_success(self):
        result = self.parse({"main.tex": tex(r"\input{missing}")})
        self.assertIn("Missing compilation resource", result["versions"][0]["error"])

    def test_missing_bib_file_is_explicit(self):
        result = self.parse({"main.tex": tex(r"Text.\bibliography{missing}")})
        self.assertEqual(result["versions"][0]["status"], "parse_error")
        self.assertIn("missing.bib", result["versions"][0]["error"])

    def test_missing_bib_without_citations_is_allowed_and_recorded(self):
        result = self.parse({"main.tex": tex("A plain sentence.")})
        self.assertEqual(result["versions"][0]["status"], "success")
        self.assertEqual(result["versions"][0]["bibliography_status"], "missing")
        self.assertEqual(result["bibtex"], [])

    def test_unknown_citation_not_silently_dropped(self):
        result = self.parse({"main.tex": tex(r"See \cite{absent}.")})
        self.assertIn("absent", result["versions"][0]["error"])
        self.assertEqual(result["elements"], [])

    def test_heading_acknowledgements_appendix_and_references(self):
        result = self.parse({"main.tex": tex(r"\section{Intro}Body.\subsection{Child}Child text.\section*{References}Do not include.\subsection{Books}Hidden reference prose.\section*{Acknowledgements}Thanks.\appendix\section{Appendix}Extra.")})
        content = " ".join(r["content"] for r in result["elements"])
        self.assertNotIn("Do not include", content)
        self.assertNotIn("Hidden reference prose", content)
        self.assertIn("Thanks.", content)
        self.assertIn("Extra.", content)
        validate_tables(result)

    def test_math_normalization_figure_and_lists(self):
        result = self.parse({"main.tex": tex(r"Inline \(x.y\) is fine. $$x=1$$ \begin{align}a&=b\\c&=d\end{align}\begin{figure}[htpb]\centering\caption{A figure}\end{figure}\begin{itemize}\item First.\begin{itemize}\item Nested.\end{itemize}\item Second.\end{itemize}")})
        self.assertEqual(result["versions"][0]["status"], "success", result["versions"])
        formulas = [r for r in result["elements"] if r["kind"] == "block_formula"]
        self.assertEqual(len(formulas), 2)
        self.assertTrue(all(r["content"].startswith(r"\begin{equation}") for r in formulas))
        figures = [r["content"] for r in result["elements"] if r["kind"] == "figure"]
        self.assertEqual(len(figures), 1)
        self.assertNotIn("htpb", figures[0])
        self.assertNotIn(r"\centering", figures[0])
        self.assertEqual(sum(n["kind"] == "item" for n in result["elements"]), 3)

    def test_sentence_boundaries_protect_math_commands_urls_and_abbreviations(self):
        text = r"Smith et al. show i.e. examples in Fig. 1 at 3.14 with $x.y$ and \url{https://a.b/c}. Next sentence."
        self.assertEqual(len(list(sentences(text))), 2)

    def test_standalone_teaser_graphic_keeps_caption_without_image_binary(self):
        # Real 2508.07901 v1-v4 use center + includegraphics + captionof,
        # outside a figure float. M1 intentionally has no image binaries.
        graphic = (r"\includegraphics[width=\linewidth]{images_final/Teaser.pdf}"
                   r"\captionof{figure}[Short]{Given a reference image, our method "
                   r"preserves identity. See \cite{x}.}\label{fig:teaser}")
        result = self.parse({"main.tex": tex(r"Before.\begin{center}\centering"
                             r"\captionsetup{type=figure}" + graphic +
                             r"\end{center}After.\bibliography{refs}"),
                             "refs.bib": "@article{x,title={Evidence},author={Alice}}"})
        self.assertEqual(result["versions"][0]["status"], "success", result["versions"])
        figures = [n for n in result["elements"] if n["kind"] == "figure"]
        self.assertEqual(len(figures), 1)
        self.assertIn(r"\includegraphics[width=\linewidth]{images_final/Teaser.pdf}", figures[0]["content"])
        self.assertIn(r"\captionof{figure}[Short]{Given a reference image", figures[0]["content"])
        self.assertIn(r"\cite{x}", figures[0]["content"])
        self.assertNotIn(r"\label", figures[0]["content"])
        self.assertEqual([n["content"] for n in result["elements"] if n["kind"] == "sentence"], ["Before.", "After."])
        self.assertEqual(len(result["bibtex"]), 1)
        validate_tables(result)

    def test_standalone_graphics_are_separate_leaves_and_preserve_star(self):
        result = self.parse({"main.tex": tex(r"Before.\includegraphics*{a.pdf}"
                             r"Between.\includegraphics[width=2cm]{b.pdf}After.")})
        self.assertEqual(result["versions"][0]["status"], "success", result["versions"])
        figures = [n["content"] for n in result["elements"] if n["kind"] == "figure"]
        self.assertEqual(figures, [r"\includegraphics*{a.pdf}", r"\includegraphics[width=2cm]{b.pdf}"])
        self.assertEqual([n["content"] for n in result["elements"] if n["kind"] == "sentence"], ["Before.", "Between.", "After."])
        validate_tables(result)

    def test_standalone_graphic_does_not_hide_malformed_or_unknown_syntax(self):
        for body in (r"\includegraphics{}", r"\includegraphics{a.pdf}\captionof{figure}{Unclosed",
                     r"\includegraphics{a.pdf}\unknownsemanticcommand{Lost}",
                     r"\includegraphics{a.pdf}\captionof{table}{Uncertain type}"):
            with self.subTest(body=body), tempfile.TemporaryDirectory() as td:
                p = Path(td)
                (p / "main.tex").write_text(tex(body))
                result = parse_versions(AID, [(1, p)])
                self.assertEqual(result["versions"][0]["status"], "parse_error")
                self.assertEqual(result["elements"], [])

    def test_formatting_cleanup_before_dedup(self):
        self.assertEqual(normalize(r"An \textbf{exact} sentence."), "An exact sentence.")

    def test_springer_starred_declarations_keep_abstract_and_first_section(self):
        result = self.parse({"main.tex": tex(
            r"\author*[1,2]{\fnm{Alice {A.}} \sur{Example}}"
            r"\author*{\fnm{Bob} \sur{Other}}"
            r"\affil*[1,2]{\orgname{Institute {of Science}}\orgaddress{\city{Town}}}"
            r"\affil*{\orgname{Other Institute}}"
            r"\begin{abstract}Abstract evidence survives.\end{abstract}"
            r"\section{Introduction}First body sentence.")})
        self.assertEqual(result["versions"][0]["status"], "success", result["versions"])
        nodes = {n["element_id"]: n for n in result["elements"]}
        parents = {e["element_id"]: e["parent_id"] for e in result["hierarchy"]}
        abstract = next(n for n in nodes.values() if n["kind"] == "component" and n["content"] == "abstract")
        abstract_text = next(n for n in nodes.values() if n["content"] == "Abstract evidence survives.")
        heading = next(n for n in nodes.values() if n["kind"] == "heading" and n["content"] == "Introduction")
        body = next(n for n in nodes.values() if n["content"] == "First body sentence.")
        self.assertEqual(parents[abstract_text["element_id"]], abstract["element_id"])
        self.assertEqual(parents[body["element_id"]], heading["element_id"])
        self.assertEqual([n["content"] for n in nodes.values() if n["kind"] == "sentence"],
                         ["Abstract evidence survives.", "First body sentence."])
        self.assertTrue(all(e["version"] == 1 for e in result["hierarchy"]))
        validate_tables(result)

    def test_springer_unstarred_declarations_with_or_without_affiliation_indices(self):
        result = self.parse({"main.tex": tex(
            r"\author[1,2]{\fnm{Alice} \sur{Example {Junior}}}"
            r"\author{\fnm{Bob} \sur{Other}}"
            r"\affil[1,2]{\orgname{Institute}\orgaddress{\city{Town {Centre}}}}"
            r"\affil{\orgname{Other Institute}}"
            r"\begin{abstract}Abstract text.\end{abstract}"
            r"\maketitle\section{Introduction}Body follows.")})
        self.assertEqual(result["versions"][0]["status"], "success", result["versions"])
        self.assertEqual([n["content"] for n in result["elements"] if n["kind"] == "sentence"],
                         ["Abstract text.", "Body follows."])
        validate_tables(result)

    def test_springer_malformed_declarations_do_not_consume_following_content(self):
        for declaration in (r"\author*[1,2]", r"\author**{Name}", r"\affil*[1]{Unclosed",
                            r"\affil[1,2{Institute}", r"\affil*", r"\author[1][2]{Name}"):
            with self.subTest(declaration=declaration), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                (root / "main.tex").write_text(tex(declaration +
                    r"\begin{abstract}Must not disappear.\end{abstract}"), encoding="utf-8")
                result = parse_versions(AID, [(1, root)])
                self.assertEqual(result["versions"][0]["status"], "parse_error")
                self.assertEqual(result["elements"], [])
                self.assertEqual(result["hierarchy"], [])

    def test_springer_command_abstract_matches_environment_including_citation(self):
        abstract = r"A \textbf{nested {finding}} with $x+1$ and \cite{evidence}."
        results = []
        for wrapper in (r"\abstract{" + abstract + "}",
                        r"\begin{abstract}" + abstract + r"\end{abstract}"):
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                (root / "main.tex").write_text(tex(wrapper +
                    r"\section{Introduction}The body remains.\bibliography{refs}"), encoding="utf-8")
                (root / "refs.bib").write_text("@article{evidence,title={Evidence},author={Alice},year={2025}}", encoding="utf-8")
                result = parse_versions(AID, [(1, root)])
                self.assertEqual(result["versions"][0]["status"], "success", result["versions"])
                validate_tables(result)
                results.append(result)
        for table in ("elements", "hierarchy", "bibtex"):
            self.assertEqual(results[0][table], results[1][table])
        nodes = {n["element_id"]: n for n in results[0]["elements"]}
        parents = {e["element_id"]: e["parent_id"] for e in results[0]["hierarchy"]}
        sentence = next(n for n in nodes.values() if r"\cite{evidence}" in n["content"])
        self.assertEqual(nodes[parents[sentence["element_id"]]]["content"], "abstract")
        body = next(n for n in nodes.values() if n["content"] == "The body remains.")
        self.assertEqual(nodes[parents[body["element_id"]]]["content"], "Introduction")
        self.assertEqual(results[0]["bibtex"][0]["citation_key"], "evidence")

    def test_springer_abstract_argument_ends_before_following_prose(self):
        result = self.parse({"main.tex": tex(r"\abstract{Summary with {nested} braces.}"
            r"Text immediately afterwards.\section{Main}Main text.")})
        self.assertEqual(result["versions"][0]["status"], "success", result["versions"])
        nodes = {n["element_id"]: n for n in result["elements"]}
        parents = {e["element_id"]: e["parent_id"] for e in result["hierarchy"]}
        after = next(n for n in nodes.values() if n["content"] == "Text immediately afterwards.")
        self.assertEqual(nodes[parents[after["element_id"]]]["kind"], "document")
        validate_tables(result)

    def test_springer_abstract_malformed_or_unknown_content_is_not_success(self):
        for body in (r"\abstract{Unclosed", r"\abstract", r"\abstract*[opt]{Unknown variant}",
                     r"\abstract{\unknownsemantic{Must not disappear}}"):
            with self.subTest(body=body), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                (root / "main.tex").write_text(tex(body), encoding="utf-8")
                result = parse_versions(AID, [(1, root)])
                self.assertEqual(result["versions"][0]["status"], "parse_error")
                self.assertEqual(result["elements"], [])

    def test_springer_backmatter_keeps_acknowledgements_supplement_and_bibtex(self):
        result = self.parse({"main.tex": tex(
            r"\section{Conclusions}Final finding.\backmatter"
            r"\bmhead{Acknowledgements}We thank the team; see \cite{x}."
            r"\bmhead{References}\begin{thebibliography}{9}"
            r"\bibitem{x} Alice.\newblock Reference title.\newblock 2025.\end{thebibliography}"
            r"\section*{Supplementary Information}\subsection{Details}Extra evidence."
            r"\begin{appendices}\section{Derivation}Appendix evidence.\end{appendices}")})
        self.assertEqual(result["versions"][0]["status"], "success", result["versions"])
        nodes = {n["element_id"]: n for n in result["elements"]}
        parents = {e["element_id"]: e["parent_id"] for e in result["hierarchy"]}
        ack = next(n for n in nodes.values() if n["content"] == "Acknowledgements")
        ack_text = next(n for n in nodes.values() if n["content"].startswith("We thank the team"))
        self.assertEqual(nodes[parents[ack["element_id"]]]["kind"], "document")
        self.assertEqual(parents[ack_text["element_id"]], ack["element_id"])
        self.assertIn("Extra evidence.", [n["content"] for n in nodes.values()])
        self.assertIn("Appendix evidence.", [n["content"] for n in nodes.values()])
        appendix = next(n for n in nodes.values() if n["content"] == "appendices")
        derivation = next(n for n in nodes.values() if n["content"] == "Derivation")
        self.assertEqual(nodes[parents[appendix["element_id"]]]["kind"], "document")
        self.assertEqual(parents[derivation["element_id"]], appendix["element_id"])
        self.assertNotIn("References", [n["content"] for n in nodes.values()])
        self.assertFalse(any("Reference title" in n["content"] for n in nodes.values()))
        self.assertEqual(result["bibtex"][0]["citation_key"], "x")
        validate_tables(result)

    def test_springer_backmatter_does_not_join_or_discard_adjacent_prose(self):
        result = self.parse({"main.tex": tex(r"\section{Main}Before.\backmatter After.")})
        self.assertEqual(result["versions"][0]["status"], "success", result["versions"])
        self.assertEqual([n["content"] for n in result["elements"] if n["kind"] == "sentence"], ["Before.", "After."])
        validate_tables(result)

    def test_springer_explicit_local_macros_take_precedence_over_conventions(self):
        source = (r"\documentclass{article}\newcommand{\abstract}[1]{Custom summary #1.}"
                  r"\newcommand{\backmatter}{Custom back matter.}"
                  r"\newcommand{\bmhead}[1]{Custom heading #1.}"
                  r"\begin{document}\abstract{kept} \backmatter \bmhead{kept}\end{document}")
        result = self.parse({"main.tex": source})
        self.assertEqual(result["versions"][0]["status"], "success", result["versions"])
        self.assertEqual([n["content"] for n in result["elements"] if n["kind"] == "sentence"],
                         ["Custom summary kept.", "Custom back matter.", "Custom heading kept."])

    def test_springer_new_declaration_forms_are_not_ignored_inside_body(self):
        for prefix in (r"\section{Introduction}", "Body text.", r"\maketitle",
                       r"\begin{abstract}Abstract.\end{abstract}",
                       r"\footnote{Body note.}", r"\footnote{Body note.}\par",
                       r"\verb|Body text.|", r"\caption{Body caption.}"):
            for declaration in (r"\author*[1]{Name}", r"\affil[1]{Institute}",
                                r"\affil*[1]{Institute}"):
                with self.subTest(prefix=prefix, declaration=declaration), tempfile.TemporaryDirectory() as td:
                    root = Path(td)
                    (root / "main.tex").write_text(tex(prefix + declaration + "After."), encoding="utf-8")
                    result = parse_versions(AID, [(1, root)])
                    self.assertEqual(result["versions"][0]["status"], "parse_error")
                    self.assertEqual(result["elements"], [])

    def test_onecolumn_changes_layout_without_dropping_appendix_text(self):
        result = self.parse({"main.tex": tex(r"Main text.\appendix\onecolumn"
                             r"\section{Supplementary Materials}Supplement text.")})
        self.assertEqual(result["versions"][0]["status"], "success", result["versions"])
        self.assertIn("Supplement text.", [n["content"] for n in result["elements"]])
        validate_tables(result)

    def test_let_footnote_number_suppression_keeps_both_notes(self):
        for assignment in (r"\let\thefootnote\relax", r"\let \thefootnote = \relax"):
            with self.subTest(assignment=assignment), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                (root / "main.tex").write_text(tex(assignment + r"\footnotetext{$^{*}$ Equal Contribution.}"
                    + assignment + r"\footnotetext{$^{\#}$ Corresponding Author.}Body."))
                result = parse_versions(AID, [(1, root)])
                self.assertEqual(result["versions"][0]["status"], "success", result["versions"])
                content = " ".join(n["content"] for n in result["elements"])
                self.assertIn("Equal Contribution.", content)
                self.assertIn("Corresponding Author.", content)
                self.assertNotIn(r"\let", content)

    def test_unknown_let_alias_still_fails_instead_of_dropping_content(self):
        result = self.parse({"main.tex": tex(r"\let\meaning\unknown\meaning{Important text}")})
        self.assertEqual(result["versions"][0]["status"], "parse_error")
        self.assertIn("Unsupported semantic let", result["versions"][0]["error"])

    def test_macro_expanded_empty_footnote_renewal_keeps_text(self):
        # Shared real-source pattern in 2508.07886 and 2508.07984. The
        # renewal appears only after expanding blfootnote, not at gather time.
        source = (r"\documentclass{article}\newcommand\blfootnote[1]{"
                  r"\begingroup\renewcommand\thefootnote{}\footnote{#1}"
                  r"\addtocounter{footnote}{-1}\endgroup}\begin{document}"
                  r"\begin{abstract}Summary.\blfootnote{Mathematics classification: 35B40.}"
                  r"\blfootnote{Keywords: Hamilton-Jacobi equations.}\end{abstract}"
                  r"\section{Introduction}Main text.\end{document}")
        result = self.parse({"main.tex": source})
        self.assertEqual(result["versions"][0]["status"], "success", result["versions"])
        nodes = {n["element_id"]: n for n in result["elements"]}
        parents = {e["element_id"]: e["parent_id"] for e in result["hierarchy"]}
        for text in ("Mathematics classification: 35B40.", "Keywords: Hamilton-Jacobi equations."):
            note = next(n for n in nodes.values() if text in n["content"])
            self.assertIn(r"\footnote{", note["content"])
            self.assertEqual(nodes[parents[note["element_id"]]]["content"], "abstract")
        self.assertFalse(any(r"\renewcommand" in n["content"] or r"\thefootnote" in n["content"] for n in nodes.values()))
        self.assertTrue(any(n["content"] == "Main text." for n in nodes.values()))
        validate_tables(result)

    def test_empty_footnote_renewal_accepts_exact_target_variants(self):
        for renewal in (r"\renewcommand\thefootnote{}", r"\renewcommand{\thefootnote}{}",
                        r"\renewcommand*\thefootnote{}", r"\renewcommand*{\thefootnote}{}"):
            with self.subTest(renewal=renewal), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                (root / "main.tex").write_text(r"\documentclass{article}\newcommand{\note}[1]{"
                    + renewal + r"\footnote{#1}}\begin{document}\note{Actual note.}Body.\end{document}")
                result = parse_versions(AID, [(1, root)])
                self.assertEqual(result["versions"][0]["status"], "success", result["versions"])
                self.assertTrue(any("Actual note." in n["content"] for n in result["elements"]))

    def test_other_local_renewals_stay_blocked(self):
        for renewal in (r"\renewcommand\important{}", r"\renewcommand\thefootnote{Semantic text}",
                        r"\renewcommand\thefootnote[1]{#1}", r"\renewcommand{\thefootnote\other}{}"):
            with self.subTest(renewal=renewal), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                (root / "main.tex").write_text(r"\documentclass{article}\newcommand{\note}[1]{"
                    + renewal + r"\footnote{#1}}\begin{document}\note{Keep note.}\end{document}")
                result = parse_versions(AID, [(1, root)])
                self.assertEqual(result["versions"][0]["status"], "parse_error")
                self.assertEqual(result["elements"], [])
        # A prior marker definition must not leak through after a local change.
        result = self.parse({"main.tex": r"\documentclass{article}\renewcommand\thefootnote{Previous marker}"
            r"\newcommand{\note}[1]{\begingroup\renewcommand\thefootnote{}\footnote{#1}\endgroup}"
            r"\begin{document}\note{Actual note.}\thefootnote\end{document}"})
        self.assertEqual(result["versions"][0]["status"], "parse_error")
        self.assertIn("requires scoped TeX semantics", result["versions"][0]["error"])

    def test_footnote_renewal_preserves_citation_union_and_version_edges(self):
        preamble = (r"\documentclass{article}\newcommand\blfootnote[1]{"
                    r"\begingroup\renewcommand\thefootnote{}\footnote{#1}\endgroup}")
        sources = {}
        for version, key, field in ((1, "old", "year={2020}"), (2, "new", "doi={10.1/test}")):
            sources[version] = {"main.tex": preamble + r"\begin{document}\begin{abstract}"
                r"\blfootnote{Classification notes; see \cite{" + key + r"}.}\end{abstract}"
                r"\section{Main}Body.\bibliography{refs}\end{document}",
                "refs.bib": "@article{"+key+",title={Exact title},author={Alice},"+field+"}"}
        paper = self.paper(sources)
        result = parse_versions(AID, [(1, paper / "v1"), (2, paper / "v2")])
        self.assertEqual([v["status"] for v in result["versions"]], ["success", "success"])
        self.assertEqual(len(result["bibtex"]), 1)
        self.assertEqual(result["bibtex"][0]["year"], 2020)
        self.assertEqual(result["bibtex"][0]["doi"], "10.1/test")
        notes = [n for n in result["elements"] if "Classification notes" in n["content"]]
        self.assertEqual(len(notes), 1)
        self.assertIn(r"\cite{old}", notes[0]["content"])
        self.assertNotIn(r"\cite{new}", notes[0]["content"])
        self.assertEqual({e["version"] for e in result["hierarchy"] if e["element_id"] == notes[0]["element_id"]}, {1, 2})
        validate_tables(result)

    def test_footnote_renewal_does_not_swallow_next_unknown_command(self):
        result = self.parse({"main.tex": r"\documentclass{article}\newcommand{\note}[1]{"
            r"\renewcommand\thefootnote{}\footnote{#1}\unknownsemanticcommand{Not disposable}}"
            r"\begin{document}\note{Actual note.}\end{document}"})
        self.assertEqual(result["versions"][0]["status"], "parse_error")
        self.assertIn("unknownsemanticcommand", result["versions"][0]["error"])
        self.assertEqual(result["elements"], [])

    def test_theorem_and_proof_optional_titles_are_semantic_content(self):
        result = self.parse({"main.tex": r"\documentclass{article}\newtheorem{thm}{Theorem}"
            r"\begin{document}\section{Main}\begin{thm}[\cite{x}, Named theorem]"
            r"Conclusion.\end{thm}\begin{proof}[Proof of Theorem \ref{main}]"
            r"Proof text.\end{proof}\bibliography{refs}\end{document}",
            "refs.bib": "@article{x,title={Actual reference},author={Alice},year={2020}}"})
        self.assertEqual(result["versions"][0]["status"], "success", result["versions"])
        nodes = {n["element_id"]: n for n in result["elements"]}
        parents = {e["element_id"]: e["parent_id"] for e in result["hierarchy"]}
        theorem_title = next(n for n in nodes.values() if "Named theorem" in n["content"])
        proof_title = next(n for n in nodes.values() if "Proof of Theorem" in n["content"])
        self.assertIn(r"\cite{x}", theorem_title["content"])
        self.assertEqual(nodes[parents[theorem_title["element_id"]]]["content"], "thm")
        self.assertEqual(nodes[parents[proof_title["element_id"]]]["content"], "proof")
        self.assertIn(r"\ref{main}", proof_title["content"])
        validate_tables(result)

    def test_float_placement_is_not_a_semantic_environment_title(self):
        result = self.parse({"main.tex": tex(r"\begin{figure}[htbp]\caption{Figure caption}\end{figure}"
            r"\begin{algorithmic}[1]\STATE Actual statement.\end{algorithmic}")})
        self.assertEqual(result["versions"][0]["status"], "success", result["versions"])
        self.assertNotIn("htbp", [n["content"] for n in result["elements"]])
        self.assertNotIn("1", [n["content"] for n in result["elements"]])

    def test_theorem_title_citations_are_renamed_before_shared_element_dedup(self):
        files = {}
        for v, key, field in ((1, "old", "year={2020}"), (2, "new", "doi={10.1/test}")):
            files[v] = {"main.tex": r"\documentclass{article}\newtheorem{thm}{Theorem}"
                r"\begin{document}\begin{thm}[\cite{" + key + r"}, Named theorem]"
                r"Conclusion.\end{thm}\bibliography{refs}\end{document}",
                "refs.bib": "@article{"+key+",title={Exact title},author={Alice},"+field+"}"}
        p = self.paper(files)
        result = parse_versions(AID, [(1, p/"v1"), (2, p/"v2")])
        titles = [n for n in result["elements"] if "Named theorem" in n["content"]]
        self.assertEqual(len(titles), 1)
        self.assertIn(r"\cite{old}", titles[0]["content"])
        self.assertEqual({e["version"] for e in result["hierarchy"] if e["element_id"] == titles[0]["element_id"]}, {1, 2})
        self.assertEqual(result["bibtex"][0]["year"], 2020)
        self.assertEqual(result["bibtex"][0]["doi"], "10.1/test")
        validate_tables(result)

    def test_supplement_fonts_boxes_rules_and_linebreak_spacing_keep_words(self):
        source = (r"\documentclass{article}\begin{document}\onecolumn\begingroup"
                  r"\makeatletter\def\normalsize{\@setfontsize\normalsize{11pt}{15pt}}"
                  r"\makeatother\normalsize\begin{center}\huge Title\\[10pt]"
                  r"\fontsize{13}{22}\selectfont Contents.\end{center}"
                  r"\makebox[1.5em][l]{A}\textbf{More Results}\makebox[3em][r]{10}."
                  r"\makebox[\linewidth][c]{\rule[0.6ex]{3.5cm}{1pt}"
                  r"\textbf{\LARGE Supplemental Material}\rule{3.5cm}{1pt}}"
                  r"un\makebox{known}.\endgroup\end{document}")
        result = self.parse({"main.tex": source})
        self.assertEqual(result["versions"][0]["status"], "success", result["versions"])
        content = " ".join(n["content"] for n in result["elements"])
        self.assertIn("Title Contents.", content)
        self.assertIn("A More Results 10 .", content)
        self.assertIn("Supplemental Material", content)
        self.assertIn("unknown.", content)
        for token in ("10pt", "3.5cm", "0.6ex", "13", "22", "@setfontsize"):
            self.assertNotIn(token, content)
        validate_tables(result)

    def test_cvpr_abbreviations_require_declared_package_and_keep_local_override(self):
        source = (r"\documentclass{article}\usepackage{cvpr}\begin{document}"
                  r"For example, \ie useful tasks, \etc.\end{document}")
        result = self.parse({"main.tex": source})
        self.assertEqual(result["versions"][0]["status"], "success")
        content = " ".join(n["content"] for n in result["elements"])
        self.assertIn("i.e.", content)
        self.assertIn("etc.", content)
        self.assertNotIn("etc..", content)
        self.assertEqual(len(result["versions"][0]["normalization_notes"]), 2)
        for replacement, expected in (("", "parse_error"),
                                      (r"\newcommand{\etc}{Local meaning}", "success")):
            with self.subTest(replacement=replacement), tempfile.TemporaryDirectory() as td:
                p = Path(td)
                (p / "main.tex").write_text(r"\documentclass{article}" + replacement
                    + r"\begin{document}Use \etc.\end{document}")
                parsed = parse_versions(AID, [(1, p)])
                self.assertEqual(parsed["versions"][0]["status"], expected)
                if expected == "success":
                    self.assertIn("Local meaning", " ".join(n["content"] for n in parsed["elements"]))

    def test_cvpr_local_macro_overrides_package_abbreviation(self):
        result = self.parse({"main.tex": r"\documentclass{article}\usepackage{cvpr}"
                             r"\newcommand{\etc}{Custom wording}\begin{document}"
                             r"Read \etc.\end{document}"})
        self.assertEqual(result["versions"][0]["status"], "success")
        self.assertTrue(any("Custom wording" in n["content"] for n in result["elements"]))
        self.assertNotIn("normalization_notes", result["versions"][0])

    def test_algorithmic_caption_states_nested_controls_and_both_branches(self):
        result = self.parse({"main.tex": tex(r"\section{Pseudocode}\begin{algorithm}[h]"
            r"\caption[Short]{Inference Process}\textbf{Input}: Tokens."
            r"\begin{algorithmic}[1]\STATE Initialize cache."
            r"\FOR{$s$ in $S$}\FOR{each block $B$}\IF{$ready$}"
            r"\STATE Compute $Q$.\ELSE\STATE Retrieve $Q$.\ENDIF"
            r"\STATE Update $T$.\ENDFOR\STATE Predict next step.\ENDFOR"
            r"\STATE\textbf{return} $T$.\end{algorithmic}\end{algorithm}")})
        self.assertEqual(result["versions"][0]["status"], "success", result["versions"])
        validate_tables(result)
        nodes = {n["element_id"]: n for n in result["elements"]}
        parents = {e["element_id"]: e["parent_id"] for e in result["hierarchy"]}
        contents = {n["content"]: n for n in nodes.values()}
        self.assertEqual(sum(n["content"] == "for" for n in nodes.values()), 2)
        compute = contents["Compute $Q$."]
        retrieve = contents["Retrieve $Q$."]
        self.assertEqual(nodes[parents[compute["element_id"]]]["content"], "if")
        self.assertEqual(nodes[parents[retrieve["element_id"]]]["content"], "else")
        self.assertEqual(nodes[parents[parents[retrieve["element_id"]]]]["content"], "if")
        self.assertIn("Inference Process", contents)
        self.assertIn("return $T$.", contents)
        self.assertTrue(any(n["kind"] == "component" and n["content"] == "caption" for n in nodes.values()))

    def test_algorithmic_unbalanced_or_unknown_controls_fail_explicitly(self):
        for body in (r"\STATE Text.\ENDIF", r"\FOR{$s$}\STATE Text.",
                     r"\IF{$x$}\STATE Then.\ELSE\STATE Else.\ELSE\ENDIF",
                     r"\FOR{$s$}\STATE Text.\ENDIF",
                     r"\STATE\WHILE{$x$}Unknown loop.\ENDWHILE", r"\STATE"):
            with self.subTest(body=body), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                (root / "main.tex").write_text(tex(r"\begin{algorithmic}"+body+r"\end{algorithmic}"))
                result = parse_versions(AID, [(1, root)])
                self.assertEqual(result["versions"][0]["status"], "parse_error")
                self.assertEqual(result["elements"], [])

    def test_algorithmic_marker_inside_literal_is_not_control_flow(self):
        result = self.parse({"main.tex": tex(r"\begin{algorithmic}\STATE "
            r"Show \verb|\IF{x}| literally.\end{algorithmic}")})
        self.assertEqual(result["versions"][0]["status"], "success", result["versions"])
        self.assertTrue(any(r"\verb|\IF{x}|" in n["content"] for n in result["elements"]))
        self.assertFalse(any(n["content"] == "if" for n in result["elements"]))

    def test_commented_supplement_and_unused_files_do_not_enter_compilation_path(self):
        result = self.parse({"main.tex": tex(r"\section{Main}Visible." + "\n% \\input{supplement}\n" +
                             r"\input{ack}"),
                             "ack.tex": r"\section*{Acknowledgment}Thanks to the team.",
                             "supplement.tex": r"\unknowncommand{Should not be parsed}",
                             "unused.tex": "Unused and redundant."})
        self.assertEqual(result["versions"][0]["status"], "success", result["versions"])
        self.assertEqual(result["versions"][0]["included_files"], ["main.tex", "ack.tex"])
        content = " ".join(n["content"] for n in result["elements"])
        self.assertIn("Thanks to the team.", content)
        self.assertNotIn("Unused", content)

    def test_missing_bbl_and_tikz_remain_missing_resources_not_empty_versions(self):
        for resource in ("main.bbl", "tikzpictures/drawing.tikz"):
            with self.subTest(resource=resource), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                (root / "main.tex").write_text(tex(r"\begin{figure}\input{"+resource+r"}\caption{Known caption}\end{figure}"))
                result = parse_versions(AID, [(1, root)])
                self.assertEqual(result["versions"][0]["status"], "parse_error")
                self.assertIn(resource, result["versions"][0]["error"])

    def test_unknown_syntax_not_reported_success(self):
        for body in (r"\unknownsemanticcommand{lost}", r"\begin{unknown}Lost.\end{unknown}", "$bad"):
            with self.subTest(body=body), tempfile.TemporaryDirectory() as td:
                p = Path(td); (p / "main.tex").write_text(tex(body))
                result = parse_versions(AID, [(1, p)])
                self.assertEqual(result["versions"][0]["status"], "parse_error")
                self.assertFalse(result["elements"])

    def test_parameter_macro_and_cycle_bound(self):
        result = self.parse({"main.tex": r"\documentclass{article}\newcommand{\term}[1]{\textbf{#1}}\begin{document}\term{Hello}.\end{document}"})
        self.assertEqual(result["versions"][0]["status"], "success")
        self.assertIn("Hello.", [n["content"] for n in result["elements"]])

    def test_two_optional_citation_arguments_and_star(self):
        result = self.parse({"main.tex": tex(r"See \citep*[compare][s 13(1), 14]{x}.\bibliography{refs}"),
                             "refs.bib": "@article{x,title={Title},author={Alice}}"})
        self.assertEqual(result["versions"][0]["status"], "success", result["versions"])
        self.assertTrue(any(r"\citep*[compare][s 13(1), 14]{x}" in n["content"] for n in result["elements"]))

    def test_text_accents_and_scopes_spanning_display_math(self):
        result = self.parse({"main.tex": tex(r"{First \'{e}tude. $$a=1$$ Last.}")})
        self.assertEqual(result["versions"][0]["status"], "success")
        self.assertIn("First étude.", [n["content"] for n in result["elements"]])

    def test_macro_cycle_is_not_success(self):
        result = self.parse({"main.tex": r"\documentclass{article}\newcommand{\loop}{\loop}\begin{document}\loop\end{document}"})
        self.assertIn("limit", result["versions"][0]["error"])

    def test_explicit_selection_and_duplicate_rejection(self):
        self.paper({1: {"main.tex": tex("One.")}, 2: {"main.tex": tex("Two.")}})
        selected = [{"arxiv_id": AID, "version": "v2"}]
        result = run(self.corpus, self.root / "out", selected_versions=selected)
        self.assertEqual(result["versions"], 1)
        with self.assertRaises(ParseError):
            run(self.corpus, self.root / "out", selected_versions=selected + selected)

    def test_conditional_compilation_not_silently_followed(self):
        result = self.parse({"main.tex": r"\documentclass{article}\ifdefined\foo\input{wrong}\fi\begin{document}Body.\end{document}"})
        self.assertIn("conditional", result["versions"][0]["error"])

    def test_bibtex_url_percent_is_preserved_regression_from_real_source(self):
        result = self.parse({"main.tex": tex(r"See \cite{x,y}.\bibliography{refs}"),
                             "refs.bib": "@article{x,title={A},author={A},url={https://example.org/#:~:text=In%20the%20first}}\n@article{y,title={B},author={B}}"})
        self.assertEqual(result["versions"][0]["status"], "success", result["versions"])
        self.assertEqual(len(result["bibtex"]), 2)
        self.assertIn("%20", result["bibtex"][0]["url"])

    def test_bibtex_comment_entry_is_ignored_and_string_scope_shared(self):
        result = self.parse({"main.tex": tex(r"See \cite{x}.\bibliography{strings,refs}"),
                             "strings.bib": "% @string{disabled}\n@string{j-name={Real Journal}}",
                             "refs.bib": "@article{x,title={Title},author={Alice},journal=j-name}"})
        self.assertEqual(result["versions"][0]["status"], "success")
        self.assertEqual(result["bibtex"][0]["journal"], "Real Journal")

    def test_bibtex_comment_between_fields_does_not_consume_url_percent(self):
        entries = parse_bibtex("@article{x,title={Title},\n% annotation = {ignored}\nauthor={Alice},url={https://example.org/a%20b}}")
        self.assertEqual(entries[0]["authors"], ["Alice"])
        self.assertEqual(entries[0]["url"], "https://example.org/a%20b")

    def test_inline_verbatim_does_not_follow_include_or_split_period(self):
        result = self.parse({"main.tex": tex(r"Code \verb|\input{missing}. inner| ends. Next.")})
        self.assertEqual(result["versions"][0]["status"], "success")
        self.assertEqual(sum(n["kind"] == "sentence" for n in result["elements"]), 2)

    def test_bad_version_does_not_remove_good_version(self):
        p = self.paper({1: {"main.tex": tex("Good.")}, 2: {"main.tex": tex(r"\input{missing}")}})
        result = parse_versions(AID, [(1, p / "v1"), (2, p / "v2")])
        self.assertEqual([v["status"] for v in result["versions"]], ["success", "parse_error"])
        self.assertEqual({e["version"] for e in result["hierarchy"]}, {1})

    def test_bibtex_balanced_fields_strings_authors_null_and_crossref(self):
        entries = parse_bibtex(r'@string{prefix="Exact"} @article{x,title=prefix # { {Title}},author={Alice and {Research and Development}},year={2021},doi={10.1/x}} @book{parent,journal={Journal}} @inproceedings{child,title={Child},crossref={parent}}')
        self.assertEqual(entries[0]["title"], "Exact Title")
        self.assertEqual(entries[0]["authors"], ["Alice", "Research and Development"])
        self.assertEqual(entries[2]["journal"], "Journal")
        self.assertIsNone(entries[1]["year"])

    def test_bibitem_no_invented_title_authors_and_raw_preserved(self):
        e = parse_bibitems(r"\bibitem{x} Incomplete reference 2020.")[0]
        self.assertIsNone(e["title"])
        self.assertIsNone(e["authors"])
        self.assertEqual(e["year"], 2020)
        self.assertIn("Incomplete reference", e["raw"])

    def test_bibitem_newblock_into_official_rows(self):
        r = self.parse({"main.tex": tex(r"\section{Intro}See \cite{x}.\begin{thebibliography}{9}\bibitem{x}Alice\newblock Explicit title\newblock Journal 2021.\end{thebibliography}")})
        self.assertEqual(r["versions"][0]["status"], "success")
        self.assertEqual(r["bibtex"][0]["title"], "Explicit title")
        self.assertFalse(any("Explicit title" in n["content"] for n in r["elements"]))

    def test_bbl_jobname_fallback_preserves_citation_fields_and_separates_tree(self):
        r = self.parse({"main.tex": tex(r"\section{Intro}See \cite{x}.\bibliography{missing}After."),
                        "main.bbl": r"\begin{thebibliography}{9}\bibitem{x}Alice\newblock Reference title\newblock Journal 2021.\end{thebibliography}"})
        self.assertEqual(r["versions"][0]["status"], "success")
        self.assertEqual(r["bibtex"][0]["citation_key"], "x")
        self.assertEqual(r["bibtex"][0]["title"], "Reference title")
        self.assertEqual(r["bibtex"][0]["authors"], ["Alice"])
        self.assertEqual(r["bibtex"][0]["year"], 2021)
        self.assertTrue(any(r"\cite{x}" in n["content"] for n in r["elements"]))
        self.assertTrue(any(n["content"] == "After." for n in r["elements"]))
        self.assertFalse(any("Reference title" in n["content"] for n in r["elements"]))
        record = r["versions"][0]
        self.assertEqual(record["bibliography_status"], "bbl")
        self.assertEqual(record["bibliography_files"], ["main.bbl"])
        self.assertEqual(record["bbl_sources"][0]["association"], "document_jobname")
        self.assertEqual(record["bibliography_evidence"][0]["source_file"], "main.bbl")
        validate_tables(r)

    def test_bbl_explicit_input_uses_only_referenced_file(self):
        r = self.parse({"main.tex": tex(r"Before.\cite{x}\input{refs/rendered.bbl}After."),
                        "refs/rendered.bbl": r"\begin{thebibliography}{9}\bibitem{x}Raw evidence 2020.\end{thebibliography}",
                        "other.bbl": "Invalid unused resource"})
        self.assertEqual(r["versions"][0]["status"], "success")
        self.assertEqual(r["versions"][0]["bbl_sources"][0]["association"], "explicit_input")
        self.assertEqual(r["bibtex"][0]["citation_key"], "x")
        self.assertIsNone(r["bibtex"][0]["title"])
        self.assertIsNone(r["bibtex"][0]["authors"])
        self.assertFalse(any("Raw evidence" in n["content"] for n in r["elements"]))

    def test_bbl_cannot_be_borrowed_from_another_version_or_paper(self):
        p = self.paper({1: {"main.tex": tex(r"See \cite{x}.\bibliography{refs}"),
                            "main.bbl": r"\begin{thebibliography}{9}\bibitem{x}Evidence 2020.\end{thebibliography}"},
                        2: {"main.tex": tex(r"See \cite{x}.\bibliography{refs}")}})
        r = parse_versions(AID, [(1, p / "v1"), (2, p / "v2")])
        self.assertEqual([v["status"] for v in r["versions"]], ["success", "parse_error"])
        self.assertNotIn("bbl_sources", r["versions"][1])
        other = self.paper({1: {"main.tex": tex(r"See \cite{x}.\bibliography{refs}")}}, aid="2508.07873")
        other_result = parse_versions("2508.07873", [(1, other / "v1")])
        self.assertEqual(other_result["versions"][0]["status"], "parse_error")
        self.assertIn("no .bbl associated", other_result["versions"][0]["error"])
        self.assertEqual(other_result["bibtex"], [])

    def test_bbl_wrong_jobname_is_not_selected_even_if_only_candidate(self):
        r = self.parse({"main.tex": tex(r"See \cite{x}.\bibliography{missing}"),
                        "unrelated.bbl": r"\begin{thebibliography}{9}\bibitem{x}Wrong document.\end{thebibliography}"})
        self.assertEqual(r["versions"][0]["status"], "parse_error")
        self.assertIn("no .bbl associated", r["versions"][0]["error"])
        self.assertEqual(r["bibtex"], [])

    def test_bbl_outside_version_is_not_read_even_when_explicit(self):
        p = self.paper({1: {"main.tex": tex("Good."),
                            "main.bbl": r"\begin{thebibliography}{9}\bibitem{x}Other version.\end{thebibliography}"},
                        2: {"main.tex": tex(r"See \cite{x}.\input{../v1/main.bbl}")}})
        r = parse_versions(AID, [(2, p / "v2")])
        self.assertEqual(r["versions"][0]["status"], "parse_error")
        self.assertIn("escapes version", r["versions"][0]["error"])

    def test_bbl_local_jobname_covers_missing_external_bib_without_reading_it(self):
        r = self.parse({"draft.tex": tex(r"See \cite{x}.\bibliography{../outside}"),
                        "draft.bbl": r"\begin{thebibliography}{9}\bibitem{x}Local evidence 2021.\end{thebibliography}"})
        self.assertEqual(r["versions"][0]["status"], "success")
        self.assertEqual(r["versions"][0]["bibliography_files"], ["draft.bbl"])
        self.assertIn("escapes version", r["versions"][0]["unavailable_bib_resources"][0]["error"])

    def test_bbl_supplements_missing_keys_without_replacing_good_bib_fields(self):
        r = self.parse({"main.tex": tex(r"See \cite{x,y}.\bibliography{refs}"),
                        "refs.bib": "@article{x,title={Verified title},author={Alice},year={2020}}",
                        "main.bbl": r"\begin{thebibliography}{9}\bibitem{x}Changed\newblock Wrong title\newblock 2022.\bibitem{y}Bob\newblock Supplement title\newblock 2021.\end{thebibliography}"})
        self.assertEqual(r["versions"][0]["status"], "success")
        by_key = {x["citation_key"]: x for x in r["bibtex"]}
        self.assertEqual(by_key["x"]["title"], "Verified title")
        self.assertEqual(by_key["x"]["year"], 2020)
        self.assertEqual(by_key["y"]["title"], "Supplement title")
        self.assertEqual(r["versions"][0]["bibliography_status"], "bibtex+bbl")

    def test_bbl_is_not_read_when_bib_or_inline_bibitems_already_suffice(self):
        for mode in ("bib", "inline"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                body = (r"\cite{x}\bibliography{refs}" if mode == "bib" else
                        r"\cite{x}\begin{thebibliography}{9}\bibitem{x}Inline 2020.\end{thebibliography}\bibliography{absent}")
                (root / "main.tex").write_text(tex(body), encoding="utf-8")
                (root / "main.bbl").write_text("Bad unused .bbl", encoding="utf-8")
                if mode == "bib":
                    (root / "refs.bib").write_text("@article{x,title={Original},author={Alice}}", encoding="utf-8")
                r = parse_versions(AID, [(1, root)])
                self.assertEqual(r["versions"][0]["status"], "success")
                self.assertNotIn("bbl_sources", r["versions"][0])

    def test_bbl_unsupported_malformed_or_partial_never_reports_success(self):
        cases = [
            (r"\entry{x}{article}{}\endentry", "BibLaTeX"),
            (r"\begin{thebibliography}{9}\bibitem{x}Unclosed", "Unclosed environment"),
            (r"Prose outside.\begin{thebibliography}{9}\bibitem{x}Evidence.\end{thebibliography}", "outside bibliography"),
            (r"\begin{thebibliography}{9}\bibitem{x}Evidence.\end{thebibliography}Other prose.", "after thebibliography"),
            (r"\begin{thebibliography}{9}\bibitem[y[inner]]{x}Evidence.\end{thebibliography}", "malformed .bbl"),
            (r"\begin{thebibliography}{9}\bibitem{x}Evidence.\iffalse\bibitem{y}Hidden.\fi\end{thebibliography}", "conditional/dynamic"),
            (r"\begin{thebibliography}{9}\bibitem{other}Not the cited key.\end{thebibliography}", "Cited bibliography entries unavailable"),
        ]
        for bbl, error in cases:
            with self.subTest(error=error), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                (root / "main.tex").write_text(tex(r"See \cite{x}.\bibliography{missing}"), encoding="utf-8")
                (root / "main.bbl").write_text(bbl, encoding="utf-8")
                r = parse_versions(AID, [(1, root)])
                self.assertEqual(r["versions"][0]["status"], "parse_error")
                self.assertIn(error, r["versions"][0]["error"])
                self.assertEqual(r["bibtex"], [])

    def test_bbl_root_association_rejects_ambiguity_and_jobname_override(self):
        for override in (False, True):
            with self.subTest(override=override), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                (root / "sub").mkdir()
                prefix = r"\newcommand{\jobname}{custom}" if override else ""
                (root / "sub/main.tex").write_text(prefix + tex(r"See \cite{x}.\bibliography{missing}"), encoding="utf-8")
                bbl = r"\begin{thebibliography}{9}\bibitem{x}Evidence.\end{thebibliography}"
                (root / "sub/main.bbl").write_text(bbl, encoding="utf-8")
                (root / "main.bbl").write_text(bbl, encoding="utf-8")
                r = parse_versions(AID, [(1, root)])
                self.assertEqual(r["versions"][0]["status"], "parse_error")
                self.assertIn("redefined" if override else "Ambiguous .bbl", r["versions"][0]["error"])

    def test_bbl_revtex_labelled_metadata_and_style_setup(self):
        r = self.parse({"main.tex": tex(r"See \cite{x}.\bibliography{missing}"),
                        "main.bbl": r"\begin{thebibliography}{9}\makeatletter\providecommand\bibinfo[2]{#2}\let\auto@bib@innerbib\relax\bibitem[{\citenamefont{Alpha}(2021)}]{x}\href{https://example.invalid/1999}{Evidence}\bibinfo{author}{\bibfnamefont{A.}~\bibnamefont{Alpha}}\bibinfo{title}{Labelled title}\bibinfo{year}{2021}\bibinfo{journal}{Journal}\end{thebibliography}"})
        self.assertEqual(r["versions"][0]["status"], "success")
        entry = r["bibtex"][0]
        self.assertEqual(entry["citation_key"], "x")
        self.assertEqual(entry["authors"], ["A. Alpha"])
        self.assertEqual(entry["title"], "Labelled title")
        self.assertEqual(entry["year"], 2021)
        self.assertEqual(entry["journal"], "Journal")

    def test_bbl_natbib_url_guard_and_ieee_noop_declaration(self):
        for setup in (
            r"\providecommand{\BIBdecl}{\relax}\BIBdecl",
            r"\expandafter\ifx\csname natexlab\endcsname\relax\def\natexlab#1{#1}\fi",
            r"\providecommand{\url}[1]{\texttt{#1}}\expandafter\ifx\csname urlstyle\endcsname\relax\providecommand{\doi}[1]{doi: #1}\else\providecommand{\doi}{doi: \begingroup \urlstyle{rm}\Url}\fi",
        ):
            with self.subTest(setup=setup), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                (root / "main.tex").write_text(tex(r"See \cite{x}.\bibliography{refs}"), encoding="utf-8")
                (root / "main.bbl").write_text(r"\begin{thebibliography}{9}" + setup +
                    r"\bibitem{x}Alice\newblock Title\newblock 2021.\end{thebibliography}", encoding="utf-8")
                result = parse_versions(AID, [(1, root)])
                self.assertEqual(result["versions"][0]["status"], "success")
                self.assertEqual(result["bibtex"][0]["citation_key"], "x")

    def test_bbl_accent_conditional_in_author_is_preserved_without_guessing_name(self):
        r = self.parse({"main.tex": tex(r"See \cite{x}.\bibliography{missing}"),
                        "main.bbl": r"\begin{thebibliography}{9}\bibitem[{Pet\ifmmode x\else y\fi}]{x}\bibinfo{author}{\bibnamefont{Pet\ifmmode x\else y\fi}}\bibinfo{title}{Observed title}\bibinfo{year}{2021}\end{thebibliography}"})
        self.assertEqual(r["versions"][0]["status"], "success")
        self.assertIsNone(r["bibtex"][0]["authors"])
        self.assertEqual(r["bibtex"][0]["title"], "Observed title")
        self.assertIn(r"\ifmmode", r["versions"][0]["bibliography_evidence"][0]["raw"])

    def test_bbl_conditional_title_is_raw_only(self):
        r = self.parse({"main.tex": tex(r"\cite{x}\bibliography{refs}"),
                        "main.bbl": r"\begin{thebibliography}{9}\bibitem{x}\bibinfo{title}{\iftrue First\else Second\fi}\bibinfo{year}{2020}\end{thebibliography}"})
        self.assertEqual(r["versions"][0]["status"], "success")
        self.assertIsNone(r["bibtex"][0]["title"])
        self.assertEqual(r["bibtex"][0]["year"], 2020)
        self.assertIn(r"\iftrue", r["versions"][0]["bibliography_evidence"][0]["raw"])

    def test_bbl_unbalanced_reference_group_fails_explicitly(self):
        r = self.parse({"main.tex": tex(r"\cite{x}\bibliography{refs}"),
                        "main.bbl": r"\begin{thebibliography}{9}\bibitem{x}Broken {group.\end{thebibliography}"})
        self.assertEqual(r["versions"][0]["status"], "parse_error")
        self.assertIn("Unbalanced .bbl", r["versions"][0]["error"])

    def test_bbl_diagnostics_do_not_mask_source_open_failure(self):
        with patch("src.m2.parser.Gatherer", side_effect=OSError("source cannot be opened")):
            r = parse_versions(AID, [(1, self.root)])
        self.assertEqual(r["versions"][0]["status"], "parse_error")
        self.assertEqual(r["versions"][0]["error"], "source cannot be opened")


    def test_bbl_percent_encoded_url_does_not_become_a_tex_comment(self):
        r = self.parse({"main.tex": tex(r"See \cite{x,y}.\bibliography{missing}"),
                        "main.bbl": "\\begin{thebibliography}{9}\n% \\url{unused broken comment\n"
                            "\\bibitem{x}Evidence 2000.\\newblock \\url{https://example.invalid/2000A%26A...363L...1K}% real comment\n"
                            "\\bibitem{y}Second 2021.\\href{https://example.invalid/a%20b}{Link}\n\\end{thebibliography}"})
        self.assertEqual(r["versions"][0]["status"], "success")
        rows = {e["citation_key"]: e for e in r["bibtex"]}
        self.assertEqual(set(rows), {"x", "y"})
        self.assertEqual(rows["x"]["url"], "https://example.invalid/2000A%26A...363L...1K")
        self.assertEqual(rows["y"]["url"], "https://example.invalid/a%20b")
        self.assertNotIn("real comment", r["versions"][0]["bibliography_evidence"][0]["raw"])

    def test_bbl_duplicate_key_conflict_is_not_silently_discarded(self):
        r = self.parse({"main.tex": tex(r"\cite{x}\bibliography{missing}"),
                        "main.bbl": r"\begin{thebibliography}{9}\bibitem{x}Alice\newblock Title one\newblock 2020.\bibitem{x}Bob\newblock Title two\newblock 2021.\end{thebibliography}"})
        self.assertEqual(r["versions"][0]["status"], "parse_error")
        self.assertIn("Ambiguous citation key", r["versions"][0]["error"])


    def test_conflicting_key_isolated_to_one_version(self):
        p = self.paper({1: {"main.tex": tex("Good.")}, 2: {"main.tex": tex(r"\nocite{*}\bibliography{refs}"),
                "refs.bib": "@article{x,title={A},author={A}} @article{x,title={B},author={B}}"}})
        result = parse_versions(AID, [(1, p / "v1"), (2, p / "v2")])
        self.assertEqual([v["status"] for v in result["versions"]], ["success", "parse_error"])

    def test_end_to_end_empty_missing_checksum_and_rerun(self):
        p = self.paper({1: {"main.tex": tex("Actual content.")}, 4: {"main.tex": tex("Missing.")}},
                       statuses={2: "no_tex_source", 3: "connection_error"})
        (p / "v4" / "main.tex").write_text("Changed")
        before = {f: f.read_bytes() for f in self.corpus.rglob("*") if f.is_file()}
        first = run(self.corpus, self.root / "output")
        self.assertEqual(first["statuses"], {"success": 1, "empty_no_tex": 1, "source_unavailable": 1, "input_error": 1})
        second = run(self.corpus, self.root / "output")
        self.assertEqual(second["cached_papers"], 1)
        self.assertEqual(first["tables"], second["tables"])
        self.assertEqual(before, {f: f.read_bytes() for f in self.corpus.rglob("*") if f.is_file()})
        import pyarrow.parquet as pq
        path = Path(first["run_root"])
        for name in TABLE_COLUMNS:
            table = pq.read_table(path / "data" / name / "part-00000.parquet")
            self.assertEqual(table.column_names, list(TABLE_COLUMNS[name]))

    def test_debug_selection_and_cli(self):
        self.paper({1: {"main.tex": tex("One.")}, 2: {"main.tex": tex("Two.")}})
        r = run(self.corpus, self.root / "output", paper=AID, version="v2")
        self.assertEqual(r["versions"], 1)
        self.assertEqual(r["statuses"], {"success": 1})
        self.assertEqual(main(["--corpus", str(self.corpus), "--output", str(self.root / "out2"), "--paper", AID, "--version", "v2"]), 0)

    def test_corrupt_output_checkpoint_is_not_overwritten(self):
        self.paper({1: {"main.tex": tex("One.")}})
        r = run(self.corpus, self.root / "output")
        p = Path(r["run_root"]) / "papers" / AID.replace(".", "-") / "result.json"
        envelope = json.loads(p.read_text()); envelope["payload"]["elements"] = []
        p.write_text(json.dumps(envelope)); before = p.read_bytes()
        with self.assertRaises(ParseError):
            run(self.corpus, self.root / "output")
        self.assertEqual(p.read_bytes(), before)

    def test_reject_input_output_overlap_and_invalid_selection(self):
        self.paper({1: {"main.tex": tex("One.")}})
        for output in (self.corpus / "output", self.root):
            with self.assertRaises(ParseError):
                run(self.corpus, output)
        with self.assertRaises(ParseError):
            run(self.corpus, self.root / "out", version="../v1")

    def test_dangling_hierarchy_is_rejected(self):
        tables = dict(elements=[], hierarchy=[dict(arxiv_id=AID, version=1, element_id="x", parent_id="y")], bibtex=[])
        with self.assertRaises(ParseError):
            validate_tables(tables)

    def test_repeated_identical_sentences_keep_one_parent_per_version(self):
        result = self.parse({"main.tex": tex(r"\section{A}Same.\section{B}Same.")})
        validate_tables(result)
        self.assertEqual(sum(n["content"] == "Same." for n in result["elements"]), 2)


    def test_ams_subject_classification_retains_codes_edition_and_following_content(self):
        for declaration in (r"\subjclass[2020]{Primary 03C66; Secondary {03E35}}",
                            r"\subjclass{03C66}"):
            with self.subTest(declaration=declaration), tempfile.TemporaryDirectory() as td:
                p = Path(td)
                (p / "main.tex").write_text(tex(declaration + r"\begin{abstract}Abstract stays.\end{abstract}\maketitle\section{Intro}Body stays.").replace("{article}", "{amsart}"), encoding="utf-8")
                result = parse_versions(AID, [(1, p)])
                self.assertEqual(result["versions"][0]["status"], "success")
                contents = [n["content"] for n in result["elements"]]
                self.assertTrue(any(n.startswith("subject classification") for n in contents))
                self.assertTrue(any("03C66" in n for n in contents))
                self.assertIn("Abstract stays.", contents)
                self.assertIn("Body stays.", contents)
                self.assertEqual("subject classification (2020)" in contents, "[2020]" in declaration)
                validate_tables(result)

    def test_ams_subjclass_does_not_guess_other_class_or_override_local_macro(self):
        r = self.parse({"main.tex": tex(r"\subjclass[2020]{03C66}Body.")})
        self.assertEqual(r["versions"][0]["status"], "parse_error")
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            (p / "main.tex").write_text(r"\documentclass{amsart}\newcommand{\subjclass}[1]{Local #1.}\begin{document}\subjclass{meaning} After.\end{document}",encoding="utf-8")
            r = parse_versions(AID, [(1, p)])
            self.assertEqual(r["versions"][0]["status"], "success")
            self.assertTrue(any("Local meaning." == n["content"] for n in r["elements"]))

    def test_ams_subjclass_malformed_or_nested_is_not_success(self):
        for body in (r"\subjclass[2020]{03C66", r"\subjclass[2020]", r"\begin{quote}\subjclass{03C66}\end{quote}"):
            with self.subTest(body=body), tempfile.TemporaryDirectory() as td:
                p=Path(td)
                (p/"main.tex").write_text(tex(body).replace("{article}","{amsart}"),encoding="utf-8")
                r=parse_versions(AID, [(1,p)])
                self.assertEqual(r["versions"][0]["status"],"parse_error")


    def test_ams_nobreakdash_keeps_literal_hyphen_and_zero_width_attachment(self):
        r = self.parse({"main.tex": r"\documentclass{article}\usepackage{amsmath}\begin{document}" + r"An $X$\nobreakdash-\hspace{0pt}stable theory. Then $Y$\nobreakdash--bounded." + r"\end{document}"})
        self.assertEqual(r["versions"][0]["status"],"success")
        self.assertIn("An $X$-stable theory.",[n["content"] for n in r["elements"]])
        self.assertTrue(any("$Y$--bounded" in n["content"] for n in r["elements"]))
        for body in (r"\nobreakdash{important}",r"\nobreakdash a"):
            with self.subTest(body=body),tempfile.TemporaryDirectory() as td:
                p=Path(td);(p/"main.tex").write_text(r"\documentclass{article}\usepackage{amsmath}\begin{document}"+body+r"\end{document}",encoding="utf-8")
                self.assertEqual(parse_versions(AID,[(1,p)])["versions"][0]["status"],"parse_error")

    def test_identical_math_mode_macro_branches_preserve_math_and_following_text(self):
        r=self.parse({"main.tex":r"\documentclass{article}\newcommand{\ZFC}{\ifmmode\mathsf{ZFC}\else$\mathsf{ZFC}$\fi}\begin{document}In \ZFC, this holds. After.\end{document}"})
        self.assertEqual(r["versions"][0]["status"],"success")
        self.assertIn(r"In $\mathsf{ZFC}$, this holds.",[n["content"] for n in r["elements"]])
        self.assertIn("After.",[n["content"] for n in r["elements"]])
        self.assertTrue(r["versions"][0]["normalization_notes"])

    def test_nested_or_non_math_name_conditionals_remain_explicit_errors(self):
        for replacement in (r"\ifmmode A\else B\fi",r"\ifmmode\iftrue A\fi\else$\iftrue A\fi$\fi"):
            with self.subTest(replacement=replacement),tempfile.TemporaryDirectory() as td:
                p=Path(td)
                (p/"main.tex").write_text(r"\documentclass{article}\newcommand{\mode}{"+replacement+r"}\begin{document}Text \mode.\end{document}",encoding="utf-8")
                r=parse_versions(AID,[(1,p)])
                self.assertEqual(r["versions"][0]["status"],"parse_error")
                self.assertIn("math-mode conditional",r["versions"][0]["error"])


    def test_sloppypar_preserves_theorem_prose_and_list_item_boundaries(self):
        r=self.parse({"main.tex":tex(r"\section{Main}\begin{sloppypar}All text remains.\end{sloppypar}\begin{enumerate}\item First.\begin{sloppypar}\item Second \cite{k}.\end{sloppypar}\item Third.\end{enumerate}\begin{thebibliography}{1}\bibitem{k}A.\newblock Title.\newblock 2020.\end{thebibliography}")})
        self.assertEqual(r["versions"][0]["status"],"success")
        contents=[n["content"] for n in r["elements"]]
        self.assertIn("All text remains.",contents)
        self.assertEqual(sum(n["kind"]=="item" for n in r["elements"]),3)
        self.assertTrue(any(r"Second \cite{k}."==n for n in contents))
        self.assertFalse(any(n["content"]=="sloppypar" for n in r["elements"]))
        validate_tables(r)

    def test_sloppypar_does_not_hide_unsupported_syntax_or_unclosed_scope(self):
        for body in (r"\begin{sloppypar}\unknown{Text}\end{sloppypar}",r"\begin{sloppypar}Incomplete."):
            with self.subTest(body=body),tempfile.TemporaryDirectory() as td:
                p=Path(td);(p/"main.tex").write_text(tex(body),encoding="utf-8")
                r=parse_versions(AID,[(1,p)])
                self.assertEqual(r["versions"][0]["status"],"parse_error")

    def test_stroked_l_keeps_letter_and_control_word_space_semantics(self):
        r=self.parse({"main.tex":tex(r"\L ojasiewicz and \l{} notation.")})
        self.assertEqual(r["versions"][0]["status"],"success")
        self.assertIn("Łojasiewicz and ł notation.",[n["content"] for n in r["elements"]])


    def test_redefined_sloppypar_is_not_assumed_to_be_only_layout(self):
        for body in (r"\begin{sloppypar}Text.\end{sloppypar}",r"\begin{enumerate}\item First.\begin{sloppypar}\item Second.\end{sloppypar}\end{enumerate}"):
            with self.subTest(body=body),tempfile.TemporaryDirectory() as td:
                p=Path(td)
                (p/"main.tex").write_text(r"\documentclass{article}\renewenvironment{sloppypar}{Important prefix.}{Important suffix.}\begin{document}"+body+r"\end{document}",encoding="utf-8")
                r=parse_versions(AID,[(1,p)])
                self.assertEqual(r["versions"][0]["status"],"parse_error")
                self.assertIn("Locally redefined sloppypar",r["versions"][0]["error"])


    def test_math_name_macro_uses_text_branch_in_prose_without_correcting_math_branch(self):
        r=self.parse({"main.tex":r"\documentclass{article}\newcommand{\AD}{\ifmmode\mathsf{AC}\else$\mathsf{AD}$\fi}\begin{document}In \AD, text branch. Math stays $\AD$.\end{document}"})
        self.assertEqual(r["versions"][0]["status"],"success")
        contents=[n["content"] for n in r["elements"]]
        self.assertIn(r"In $\mathsf{AD}$, text branch.",contents)
        self.assertIn(r"Math stays $\AD$.",contents)
        self.assertFalse(any(r"$\mathsf{AC}$" in n for n in contents))
        self.assertTrue(any("text branch" in n for n in r["versions"][0]["normalization_notes"]))


    def test_citation_optional_note_periods_do_not_split_invocation(self):
        for citation in (r"\cite[Ch. 5]{k}",r"\citep*[e.g.][Sec. 1.2]{k}"):
            with self.subTest(citation=citation),tempfile.TemporaryDirectory() as td:
                p=Path(td)
                (p/"main.tex").write_text(tex("See "+citation+r" for details. After.\begin{thebibliography}{1}\bibitem{k}Author.\newblock Title.\newblock 2020.\end{thebibliography}"),encoding="utf-8")
                r=parse_versions(AID,[(1,p)])
                self.assertEqual(r["versions"][0]["status"],"success")
                self.assertIn("See "+citation+" for details.",[n["content"] for n in r["elements"]])
                self.assertIn("After.",[n["content"] for n in r["elements"]])
                self.assertEqual(r["bibtex"][0]["citation_key"],"k")

    def test_citation_nested_optional_note_and_escaped_periods_keep_key(self):
        citation=r"\cite[see {Ch. 5}][pp. 1--2]{k}"
        self.assertEqual(list(sentences("See "+citation+". Next.")),["See "+citation+".","Next."])


    def test_ieee_keywords_preserve_text_citation_and_following_section(self):
        body=r"\begin{abstract}Abstract.\end{abstract}\begin{IEEEkeywords}Signals, \emph{learning} \cite{k}.\end{IEEEkeywords}\section{Intro}\IEEEPARstart{T}{he} content stays.\begin{thebibliography}{1}\bibitem{k}A.\newblock Title.\newblock 2020.\end{thebibliography}"
        r=self.parse({"main.tex":tex(body).replace("{article}","{IEEEtran}")})
        self.assertEqual(r["versions"][0]["status"],"success")
        contents=[n["content"] for n in r["elements"]]
        self.assertIn("keywords",contents)
        self.assertIn(r"Signals, learning \cite{k}.",contents)
        self.assertIn("The content stays.",contents)
        self.assertEqual(r["bibtex"][0]["citation_key"],"k")
        validate_tables(r)

    def test_ieee_keywords_other_class_or_local_redefinition_is_not_guessed(self):
        for source in (tex(r"\begin{IEEEkeywords}Text.\end{IEEEkeywords}"),r"\documentclass{IEEEtran}\renewenvironment{IEEEkeywords}{Extra text.}{}\begin{document}\begin{IEEEkeywords}Key.\end{IEEEkeywords}\end{document}"):
            with self.subTest(source=source),tempfile.TemporaryDirectory() as td:
                p=Path(td);(p/"main.tex").write_text(source,encoding="utf-8")
                self.assertEqual(parse_versions(AID,[(1,p)])["versions"][0]["status"],"parse_error")

    def test_running_headers_keep_raw_evidence_without_truncating_body(self):
        body=r"\fancyhead[LE,RO]{Nested {header}.}\markboth{Left.}{Right \unknown{style}}\section{Intro}Actual content."
        r=self.parse({"main.tex":tex(body)})
        self.assertEqual(r["versions"][0]["status"],"success")
        contents=[n["content"] for n in r["elements"]]
        self.assertIn("Actual content.",contents)
        self.assertNotIn("Nested header.",contents)
        meta=r["versions"][0]["layout_metadata"]
        self.assertEqual(meta[0]["placement"],"LE,RO")
        self.assertEqual(meta[0]["arguments"],["Nested {header}."])
        self.assertEqual(meta[1]["arguments"],["Left.",r"Right \unknown{style}"])

    def test_header_macro_override_and_malformed_arguments_are_not_dropped(self):
        r=self.parse({"main.tex":r"\documentclass{article}\newcommand{\markboth}[2]{#1 and #2.}\begin{document}\markboth{A}{B}\end{document}"})
        self.assertEqual(r["versions"][0]["status"],"success")
        self.assertIn("A and B.",[n["content"] for n in r["elements"]])
        with tempfile.TemporaryDirectory() as td:
            p=Path(td);(p/"main.tex").write_text(tex(r"\markboth{Only one}"),encoding="utf-8")
            self.assertEqual(parse_versions(AID,[(1,p)])["versions"][0]["status"],"parse_error")

    def test_centerline_retains_prose_math_citation_and_graphic(self):
        r=self.parse({"main.tex":tex(r"\centerline{Before $x$ \cite{k}.}\centerline{\includegraphics{missing.png}}After.\begin{thebibliography}{1}\bibitem{k}A.\newblock Title.\newblock 2020.\end{thebibliography}")})
        self.assertEqual(r["versions"][0]["status"],"success")
        self.assertIn(r"Before $x$ \cite{k}.",[n["content"] for n in r["elements"]])
        self.assertTrue(any(n["kind"]=="figure" and "missing.png" in n["content"] for n in r["elements"]))
        self.assertIn("After.",[n["content"] for n in r["elements"]])

    def test_centerline_unknown_content_stays_an_error(self):
        r=self.parse({"main.tex":tex(r"\centerline{\unknown{Do not drop}}")})
        self.assertEqual(r["versions"][0]["status"],"parse_error")
        self.assertIn("unknown",r["versions"][0]["error"])


    def test_layout_length_is_bounded_and_does_not_remove_following_content(self):
        r=self.parse({"main.tex":tex(r"\setlength{\parskip}{0pt}\setlength{\tabcolsep}{1.75pt}Body stays.")})
        self.assertEqual(r["versions"][0]["status"],"success")
        self.assertIn("Body stays.",[n["content"] for n in r["elements"]])
        self.assertEqual(len(r["versions"][0]["layout_metadata"]),2)
        with tempfile.TemporaryDirectory() as td:
            p=Path(td);(p/"main.tex").write_text(tex(r"\setlength{\unknown}{\dynamic}Text."),encoding="utf-8")
            self.assertEqual(parse_versions(AID,[(1,p)])["versions"][0]["status"],"parse_error")

    def test_subequations_keep_separate_formula_nodes_and_following_prose(self):
        r=self.parse({"main.tex":r"\documentclass{article}\usepackage{amsmath}\begin{document}\begin{subequations}\begin{equation}a=b\end{equation}\begin{align}x&=y\end{align}\end{subequations}After.\end{document}"})
        self.assertEqual(r["versions"][0]["status"],"success")
        self.assertEqual(sum(n["kind"]=="block_formula" for n in r["elements"]),2)
        self.assertIn("After.",[n["content"] for n in r["elements"]])
        validate_tables(r)

    def test_acm_xml_and_human_classification_are_preserved_separately(self):
        body=r"\begin{CCSXML}<ccs2012><concept><concept_desc>Systems</concept_desc></concept></ccs2012>\end{CCSXML}\ccsdesc[500]{Information systems}\section{Intro}Body."
        r=self.parse({"main.tex":tex(body).replace("{article}","{acmart}")})
        self.assertEqual(r["versions"][0]["status"],"success")
        self.assertIn("Information systems",[n["content"] for n in r["elements"]])
        self.assertTrue(any("concept_desc" in m.get("raw","") for m in r["versions"][0]["layout_metadata"]))
        self.assertFalse(any("<ccs2012>" in n["content"] for n in r["elements"]))

    def test_acm_malformed_xml_or_missing_class_is_not_silently_dropped(self):
        for cls,xml in (("acmart","<ccs2012>"),("article","<ccs2012/>"),("acmart",'<!DOCTYPE a [<!ENTITY x "bad">]><a>&x;</a>')):
            with self.subTest(cls=cls,xml=xml),tempfile.TemporaryDirectory() as td:
                p=Path(td);(p/"main.tex").write_text(tex(r"\begin{CCSXML}"+xml+r"\end{CCSXML}").replace("{article}","{"+cls+"}"),encoding="utf-8")
                self.assertEqual(parse_versions(AID,[(1,p)])["versions"][0]["status"],"parse_error")

    def test_centerline_teaser_spacing_keeps_caption_on_the_figure(self):
        r=self.parse({"main.tex":tex(r"\centerline{\includegraphics{missing.png}\vspace{-0.2cm}}\captionof{figure}{Actual \textbf{caption}.}After.")})
        self.assertEqual(r["versions"][0]["status"],"success")
        figures=[n for n in r["elements"] if n["kind"]=="figure"]
        self.assertEqual(len(figures),1)
        self.assertIn("Actual caption.",figures[0]["content"])
        self.assertIn("After.",[n["content"] for n in r["elements"]])

    def test_subfigure_reference_remains_in_prose(self):
        r=self.parse({"main.tex":r"\documentclass{article}\usepackage{subfig}\begin{document}See \ref{fig}\subref{fig:a}. After.\end{document}"})
        self.assertEqual(r["versions"][0]["status"],"success")
        self.assertIn(r"See \ref{fig}\subref{fig:a}.",[n["content"] for n in r["elements"]])


    def test_setspace_and_starred_vertical_spacing_keep_content(self):
        r=self.parse({"main.tex":r"\documentclass{article}\usepackage{setspace}\begin{document}\onehalfspacing\setstretch{1.6}\vspace*{2ex}A \textsl{visible} word.\end{document}"})
        self.assertEqual(r["versions"][0]["status"],"success")
        self.assertIn("A visible word.",[n["content"] for n in r["elements"]])
        self.assertEqual([m["command"] for m in r["versions"][0]["layout_metadata"]],["onehalfspacing","setstretch"])

    def test_unknown_spacing_value_or_missing_package_is_not_guessed(self):
        for pre,body in ((r"\usepackage{setspace}",r"\setstretch{\dynamic}"),("",r"\onehalfspacing")):
            with self.subTest(body=body),tempfile.TemporaryDirectory() as td:
                p=Path(td);(p/"main.tex").write_text(r"\documentclass{article}"+pre+r"\begin{document}"+body+r"Body.\end{document}",encoding="utf-8")
                self.assertEqual(parse_versions(AID,[(1,p)])["versions"][0]["status"],"parse_error")

    def test_acknowledgments_command_preserves_text_and_citation(self):
        r=self.parse({"main.tex":tex(r"\section{End}Body.\acknowledgments{We thank X \cite{k}.}After.\begin{thebibliography}{1}\bibitem{k}A.\newblock Title.\newblock 2020.\end{thebibliography}")})
        self.assertEqual(r["versions"][0]["status"],"success")
        self.assertIn("acknowledgments",[n["content"] for n in r["elements"]])
        self.assertIn(r"We thank X \cite{k}.",[n["content"] for n in r["elements"]])
        self.assertIn("After.",[n["content"] for n in r["elements"]])

    def test_aastex_corresponding_author_keeps_raw_name_and_body(self):
        r=self.parse({"main.tex":tex(r"\correspondingauthor{Jane {Luu}}\begin{abstract}Actual abstract.\end{abstract}").replace("{article}","{aastex631}")})
        self.assertEqual(r["versions"][0]["status"],"success")
        self.assertEqual(r["versions"][0]["layout_metadata"][0]["arguments"],["Jane {Luu}"])
        self.assertIn("Actual abstract.",[n["content"] for n in r["elements"]])

    def test_crefrange_keeps_both_labels_and_nobreak_keeps_text(self):
        r=self.parse({"main.tex":r"\documentclass{article}\usepackage{cleveref}\begin{document}By \crefrange{a}{b}, this holds.\par\nobreak Next.\end{document}"})
        self.assertEqual(r["versions"][0]["status"],"success")
        self.assertIn(r"By \crefrange{a}{b}, this holds.",[n["content"] for n in r["elements"]])
        self.assertIn("Next.",[n["content"] for n in r["elements"]])


    def test_percent_url_is_data_but_label_and_trailing_comments_are_not(self):
        text=r"\href{https://example.test/DANCE%2046}{Visible label} % comment"+"\nAfter."
        cleaned=strip_comments(text)
        self.assertIn("DANCE%2046",cleaned)
        self.assertNotIn("comment",cleaned)
        self.assertIn("After.",cleaned)
        r=self.parse({"main.tex":tex(text)})
        self.assertEqual(r["versions"][0]["status"],"success")
        self.assertTrue(any("DANCE%2046" in n["content"] for n in r["elements"]))

    def test_biblatex_citation_names_collect_normalize_and_preserve_notes(self):
        for name in ("textcite","parencite","autocite"):
            with self.subTest(name=name),tempfile.TemporaryDirectory() as td:
                p=Path(td)
                for v,key in ((1,"old"),(2,"new")):
                    d=p/f"v{v}";d.mkdir()
                    (d/"main.tex").write_text(tex("See \\"+name+r"[Ch. 5]{"+key+r"}. After.\bibliography{refs}"),encoding="utf-8")
                    (d/"refs.bib").write_text("@article{"+key+",title={Same title},author={A},year={2020}}",encoding="utf-8")
                r=parse_versions(AID,[(1,p/"v1"),(2,p/"v2")])
                self.assertEqual([x["status"] for x in r["versions"]],["success","success"])
                self.assertEqual(len(r["bibtex"]),1)
                self.assertTrue(any("See \\"+name+r"[Ch. 5]{old}."==n["content"] for n in r["elements"]))
                self.assertFalse(any("{new}" in n["content"] for n in r["elements"]))

    def test_aastex_angle_symbols_and_revtex_pacs_preserve_meaning(self):
        r=self.parse({"main.tex":tex(r"An angle 11.2\degr, separation 8\arcsec.").replace("{article}","{aastex631}")})
        self.assertEqual(r["versions"][0]["status"],"success")
        self.assertIn("An angle 11.2°, separation 8″.",[n["content"] for n in r["elements"]])
        with tempfile.TemporaryDirectory() as td:
            p=Path(td);(p/"main.tex").write_text(tex(r"\pacs{12.15.Hh, 13.25.Hw}\section{Intro}Body.").replace("{article}","{revtex4-2}"),encoding="utf-8")
            r=parse_versions(AID,[(1,p)])
            self.assertEqual(r["versions"][0]["status"],"success")
            self.assertIn("12.15.Hh, 13.25.Hw",[n["content"] for n in r["elements"]])
            self.assertIn("Body.",[n["content"] for n in r["elements"]])


    def test_aastex_deluxetable_keeps_caption_cells_and_citation(self):
        r=self.parse({"main.tex":tex(r"\section{Results}Before.\begin{deluxetable*}{lr}\tablecaption{Measured values \cite{k}.}\tablehead{Source & Value}\startdata A & 8 \\ \enddata\end{deluxetable*}After.\begin{thebibliography}{1}\bibitem{k}A.\newblock Title.\newblock 2020.\end{thebibliography}").replace("{article}","{aastex631}")})
        self.assertEqual(r["versions"][0]["status"],"success")
        tables=[n for n in r["elements"] if n["kind"]=="figure"]
        self.assertEqual(len(tables),1)
        self.assertIn(r"Measured values \cite{k}.",tables[0]["content"])
        self.assertIn("A & 8",tables[0]["content"])
        self.assertIn("After.",[n["content"] for n in r["elements"]])
        self.assertEqual(r["bibtex"][0]["citation_key"],"k")

    def test_allowbreak_is_zero_width_and_angstrom_character_is_preserved(self):
        r=self.parse({"main.tex":tex(r"A term\allowbreak{}ination at 8 \AA.")})
        self.assertEqual(r["versions"][0]["status"],"success")
        self.assertIn("A termination at 8 Å.",[n["content"] for n in r["elements"]])


    def test_aastex_backmatter_facilities_and_software_preserve_citations(self):
        r=self.parse({"main.tex":tex(r"\section{End}Body.\facilities{VLT(VISIR).}\software{Package \cite{k}.}\begin{thebibliography}{1}\bibitem{k}A.\newblock Title.\newblock 2020.\end{thebibliography}").replace("{article}","{aastex631}")})
        self.assertEqual(r["versions"][0]["status"],"success")
        contents=[n["content"] for n in r["elements"]]
        self.assertIn("facilities",contents)
        self.assertIn("VLT(VISIR).",contents)
        self.assertIn(r"Package \cite{k}.",contents)

    def test_numberwithin_keeps_formula_and_rlap_keeps_visible_argument(self):
        r=self.parse({"main.tex":r"\documentclass{article}\usepackage{amsmath}\begin{document}\numberwithin{equation}{section}A\rlap{.}\begin{equation}x=y\end{equation}After.\end{document}"})
        self.assertEqual(r["versions"][0]["status"],"success")
        self.assertIn("A.",[n["content"] for n in r["elements"]])
        self.assertTrue(any(n["kind"]=="block_formula" and "x=y" in n["content"] for n in r["elements"]))
        self.assertEqual(r["versions"][0]["layout_metadata"][0]["counter"],"equation")


    def test_math_environment_aliases_follow_source_definitions_before_pairing(self):
        r=self.parse({"main.tex":r"\documentclass{article}\newcommand{\openmath}{\begin{equation}}\newcommand{\closemath}{\end{equation}}\begin{document}Before.\openmath x=y\closemath After.\end{document}"})
        self.assertEqual(r["versions"][0]["status"],"success")
        self.assertIn(r"\begin{equation}x=y\end{equation}",[n["content"] for n in r["elements"]])
        self.assertIn("After.",[n["content"] for n in r["elements"]])
        self.assertEqual(len(r["versions"][0]["normalization_notes"]),2)

    def test_math_aliases_do_not_expand_verbatim_or_repair_mismatched_source(self):
        r=self.parse({"main.tex":r"\documentclass{article}\newcommand{\openmath}{\begin{equation}}\newcommand{\closemath}{\end{align}}\begin{document}\openmath x=y\closemath\end{document}"})
        self.assertEqual(r["versions"][0]["status"],"parse_error")
        with tempfile.TemporaryDirectory() as td:
            p=Path(td);(p/"main.tex").write_text(r"\documentclass{article}\newcommand{\openmath}{\begin{equation}}\begin{document}Literal \verb|\openmath| remains.\end{document}",encoding="utf-8")
            r=parse_versions(AID,[(1,p)])
            self.assertEqual(r["versions"][0]["status"],"success")
            self.assertTrue(any(r"\verb|\openmath|" in n["content"] for n in r["elements"]))

    def test_aastex_single_facility_keeps_its_citation(self):
        r=self.parse({"main.tex":tex(r"\facility{VLT \cite{k}.}\begin{thebibliography}{1}\bibitem{k}A.\newblock Title.\newblock 2020.\end{thebibliography}").replace("{article}","{aastex631}")})
        self.assertEqual(r["versions"][0]["status"],"success")
        self.assertIn(r"VLT \cite{k}.",[n["content"] for n in r["elements"]])


    def test_metadata_environment_options_are_not_silently_removed(self):
        for cls,pre,env in (("IEEEtran","","IEEEkeywords"),("article",r"\usepackage{amsmath}","subequations"),("acmart","","CCSXML")):
            with self.subTest(env=env),tempfile.TemporaryDirectory() as td:
                p=Path(td);(p/"main.tex").write_text(r"\documentclass{"+cls+"}"+pre+r"\begin{document}\begin{"+env+r"}[do not lose this]Text.\end{"+env+r"}\end{document}",encoding="utf-8")
                r=parse_versions(AID,[(1,p)])
                self.assertEqual(r["versions"][0]["status"],"parse_error")
                self.assertIn("optional argument",r["versions"][0]["error"])

    def test_centerline_boundaries_do_not_glue_distinct_sentences(self):
        r=self.parse({"main.tex":tex(r"Before.\centerline{Centered.}After.")})
        self.assertEqual(r["versions"][0]["status"],"success")
        contents=[n["content"] for n in r["elements"]]
        for sentence in ("Before.","Centered.","After."):self.assertIn(sentence,contents)


    def test_nested_or_empty_citation_keys_cannot_report_success(self):
        for cite in (r"\cite{{k}}",r"\cite{}",r"\cite{\dynamic}"):
            with self.subTest(cite=cite),tempfile.TemporaryDirectory() as td:
                p=Path(td);(p/"main.tex").write_text(tex(cite+r"\bibliography{refs}"),encoding="utf-8")
                (p/"refs.bib").write_text("@article{k,title={Title},year={2020}}",encoding="utf-8")
                r=parse_versions(AID,[(1,p)])
                self.assertEqual(r["versions"][0]["status"],"parse_error")
                self.assertIn("citation key syntax",r["versions"][0]["error"])

    def test_citation_exposed_by_macro_cannot_lack_bibliography_evidence(self):
        r=self.parse({"main.tex":r"\documentclass{article}\newcommand{\hiddenref}{\cite{absent}}\begin{document}A \hiddenref.\end{document}"})
        self.assertEqual(r["versions"][0]["status"],"parse_error")
        self.assertIn("after expansion",r["versions"][0]["error"])

    def test_literal_citation_inside_verbatim_does_not_request_a_reference(self):
        r=self.parse({"main.tex":tex(r"Literal \verb|\cite{not-a-real-reference}| stays.")})
        self.assertEqual(r["versions"][0]["status"],"success")
        self.assertTrue(any(r"\verb|\cite{not-a-real-reference}|" in n["content"] for n in r["elements"]))


    def test_unused_tex_url_fragment_does_not_break_lexical_root_detection(self):
        r=self.parse({"main.tex":tex("Real body."),"unused.tex":r"An unused fragment \url{unfinished" + chr(10) + chr(92)})
        self.assertEqual(r["versions"][0]["status"],"success")
        self.assertEqual(r["versions"][0]["included_files"],["main.tex"])
        self.assertEqual(strip_comments("\\"),"\\")
        with tempfile.TemporaryDirectory() as td:
            p=Path(td);(p/"main.tex").write_text(tex(r"Active \url{unfinished"),encoding="utf-8")
            self.assertEqual(parse_versions(AID,[(1,p)])["versions"][0]["status"],"parse_error")


    def test_top_level_acknowledgments_are_not_nested_in_last_research_section(self):
        r=self.parse({"main.tex":tex(r"\section{Results}\subsection{Last}Research.\acknowledgments{Thanks.}\appendix\section{Appendix}Extra.")})
        self.assertEqual(r["versions"][0]["status"],"success")
        ack=next(n["element_id"] for n in r["elements"] if n["content"]=="acknowledgments")
        self.assertEqual(next(e["parent_id"] for e in r["hierarchy"] if e["element_id"]==ack),"2508-07872#root1")


    def test_literal_false_branch_excludes_inactive_input_and_citation(self):
        r=self.parse({"main.tex":tex(r"Before.\iffalse\input{absent}\cite{missing}\else Active.\fi After.")})
        self.assertEqual(r["versions"][0]["status"],"success")
        content=" ".join(n["content"] for n in r["elements"])
        self.assertIn("Active.",content);self.assertIn("After.",content);self.assertNotIn("absent",content)

    def test_literal_nested_branches_preserve_only_active_content(self):
        r=self.parse({"main.tex":tex(r"\iftrue Yes.\iffalse No.\else Nested.\fi\else Wrong.\fi End.")})
        self.assertEqual(r["versions"][0]["status"],"success")
        c=" ".join(n["content"] for n in r["elements"])
        for value in ["Yes.","Nested.","End."]:self.assertIn(value,c)
        self.assertNotIn("Wrong.",c);self.assertNotIn("No.",c)

    def test_literal_conditional_rejects_unknown_predicate_and_unclosed(self):
        for body in [r"\iffalse Hidden.",r"\iffalse\ifcustom X.\fi\fi",r"\iftrue\newcommand{\x}{Y}\fi"]:
            with self.subTest(body=body), tempfile.TemporaryDirectory() as td:
                p=Path(td);(p/"main.tex").write_text(tex(body),encoding="utf-8")
                self.assertEqual(parse_versions(AID,[(1,p)])["versions"][0]["status"],"parse_error")

    def test_algpseudocode_functions_require_return_and_branches(self):
        r=self.parse({"main.tex":tex(r"\begin{algorithmic}[1]\Require Input.\Ensure Output.\Function{Solve}{$x$}\ForAll{$i$}\If{$i>0$}\State \Return \Call{Eval}{$x$}\ElsIf{$i=0$}\State Zero.\Else\State Negative.\EndIf\EndFor\EndFunction\end{algorithmic}After.")})
        self.assertEqual(r["versions"][0]["status"],"success")
        c=" ".join(n["content"] for n in r["elements"])
        for value in ["Input.","Output.","Solve","Eval($x$)","Zero.","Negative.","After."]:self.assertIn(value,c)
        kinds={n["content"] for n in r["elements"] if n["kind"]=="component"}
        self.assertTrue({"function","forall","if","elsif","else"}<=kinds)

    def test_algorithmic_uppercase_require_ensure_and_label_after_end(self):
        r=self.parse({"main.tex":tex(r"\begin{algorithmic}\REQUIRE Data.\ENSURE Model.\FOR{$i$}\STATE Train.\ENDFOR\label{loop}\end{algorithmic}")})
        self.assertEqual(r["versions"][0]["status"],"success")

    def test_algpseudocode_unbalanced_function_is_not_success(self):
        r=self.parse({"main.tex":tex(r"\begin{algorithmic}\Function{Solve}{$x$}\State Data.\EndWhile\end{algorithmic}")})
        self.assertEqual(r["versions"][0]["status"],"parse_error")


    def test_source_defined_proof_environment_retains_label_body_and_following_text(self):
        source=r"\documentclass{article}\newenvironment{Proof}{\noindent{\it Proof.}}{\hbox{\rule{6pt}{6pt}}}\begin{document}\section{Main}\begin{Proof}Actual proof.\end{Proof}After.\end{document}"
        r=self.parse({"main.tex":source})
        self.assertEqual(r["versions"][0]["status"],"success")
        c=" ".join(n["content"] for n in r["elements"])
        for v in ["Proof.","Actual proof.","After."]:self.assertIn(v,c)

    def test_source_defined_environment_argument_and_unknown_semantics(self):
        source=r"\documentclass{article}\newenvironment{ProofOf}[1]{Proof of #1.}{End.}\begin{document}\begin{ProofOf}{Lemma}Body.\end{ProofOf}\end{document}"
        r=self.parse({"main.tex":source})
        self.assertEqual(r["versions"][0]["status"],"success")
        self.assertIn("Proof of Lemma.",[n["content"] for n in r["elements"]])
        with tempfile.TemporaryDirectory() as td:
            p=Path(td);(p/"main.tex").write_text(source.replace("End.",r"\unknownEnd"),encoding="utf-8")
            self.assertEqual(parse_versions(AID,[(1,p)])["versions"][0]["status"],"parse_error")

    def test_theorem_factory_and_body_declaration(self):
        source=r"\documentclass{article}\newcommand{\Declare}[2]{\newaliascnt{#1}{base}\newtheorem{#1}[base]{#1}\crefname{#1}{#1}{#2}}\Declare{Claim}{Claims}\begin{document}\newtheorem{remark}{Remark}\begin{Claim}Claim text.\end{Claim}\begin{remark}Remark text.\end{remark}\end{document}"
        r=self.parse({"main.tex":source})
        self.assertEqual(r["versions"][0]["status"],"success")
        self.assertIn("Claim text.",[n["content"] for n in r["elements"]])

    def test_frontmatter_keywords_keeps_abstract_and_body(self):
        source=r"\documentclass{elsarticle}\begin{document}\begin{frontmatter}\title{Title}\author[1]{Name}\cortext[1]{Contact}\begin{abstract}Abstract content.\end{abstract}\begin{keyword}Graph\sep Control\end{keyword}\end{frontmatter}\section{Intro}Body.\end{document}"
        r=self.parse({"main.tex":source})
        self.assertEqual(r["versions"][0]["status"],"success")
        c=" ".join(n["content"] for n in r["elements"])
        for v in ["Abstract content.","Graph; Control","Intro","Body."]:self.assertIn(v,c)

    def test_layout_containers_keep_all_text_and_equations(self):
        r=self.parse({"main.tex":tex(r"\begin{titlepage}Title text.\end{titlepage}\fbox{\begin{minipage}[b]{\textwidth}Box text.\[x=1\]\end{minipage}}After.")})
        self.assertEqual(r["versions"][0]["status"],"success")
        c=" ".join(n["content"] for n in r["elements"])
        for v in ["Title text.","Box text.","x=1","After."]:self.assertIn(v,c)

    def test_literal_diagnostic_registers_and_layout_glue(self):
        r=self.parse({"main.tex":tex(r"\hbadness=10000\hfuzz=200pt\vfuzz=5pt\vskip .5cm Before.\quad After.")})
        self.assertEqual(r["versions"][0]["status"],"success")
        self.assertIn("Before.",[n["content"] for n in r["elements"]])

    def test_dynamic_layout_register_is_not_silently_consumed(self):
        r=self.parse({"main.tex":tex(r"\vskip\unknownlength Important.")})
        self.assertEqual(r["versions"][0]["status"],"parse_error")

    def test_algorithm2e_keeps_inputs_results_nested_blocks(self):
        source=r"\documentclass{article}\usepackage{algorithm2e}\begin{document}\begin{algorithm}\KwIn{Data.}\KwOut{Model.}\For{$i$}{\While{$x$}{Update.\;}}\KwResult{Solution.}\end{algorithm}After.\end{document}"
        r=self.parse({"main.tex":source})
        self.assertEqual(r["versions"][0]["status"],"success")
        c=" ".join(n["content"] for n in r["elements"])
        for v in ["Data.","Model.","Update.","Solution.","After."]:self.assertIn(v,c)

    def test_jabref_underscore_field_preserves_raw_metadata(self):
        entries=parse_bibtex('@article{k,title={Known},__markedentry={[editor:6]},year={2020}}')
        self.assertEqual(entries[0]["title"],"Known")
        self.assertIn("__markedentry",entries[0]["raw"])


    def test_input_delimiter_spaces_resolve_existing_file_without_crossing_versions(self):
        r=self.parse({"main.tex":tex(r"\section{Intro}\input{ section/part }"),"section/part.tex":"Real content."})
        self.assertEqual(r["versions"][0]["status"],"success")
        self.assertIn("Real content.",[n["content"] for n in r["elements"]])

    def test_raisebox_and_framebox_keep_math_and_following_prose(self):
        r=self.parse({"main.tex":tex(r"Before.\raisebox{1.5pt}[10pt][0pt]{\tiny\framebox{$x$}} After.")})
        self.assertEqual(r["versions"][0]["status"],"success")
        self.assertIn("$x$"," ".join(n["content"] for n in r["elements"]))

    def test_baseline_scaling_layout_does_not_consume_title(self):
        r=self.parse({"main.tex":tex(r"{\setlength{\baselineskip}{1.5\baselineskip}{\LARGE Title.}}After.")})
        self.assertEqual(r["versions"][0]["status"],"success")
        c=" ".join(n["content"] for n in r["elements"])
        self.assertIn("Title.",c);self.assertIn("After.",c)

    def test_algorithmic_condition_comment_keeps_visible_annotation(self):
        r=self.parse({"main.tex":tex(r"\begin{algorithmic}\For{$i$}:\hfill\Comment{Loop explanation.}\State Work.\EndFor\end{algorithmic}")})
        self.assertEqual(r["versions"][0]["status"],"success")
        self.assertIn("Loop explanation.",[n["content"] for n in r["elements"]])


    def test_inline_bibliography_uses_bounded_style_guards_not_compilation_predicates(self):
        source=tex(r"See \cite{k}.\begin{thebibliography}{1}\expandafter\ifx\csname urlstyle\endcsname\relax\providecommand{\doi}[1]{doi: #1}\else\providecommand{\doi}{doi: \begingroup \urlstyle{rm}\Url}\fi\bibitem{k}Alice.\newblock Title.\newblock 2020.\end{thebibliography}")
        r=self.parse({"main.tex":source})
        self.assertEqual(r["versions"][0]["status"],"success")
        self.assertEqual(r["bibtex"][0]["citation_key"],"k")
        self.assertFalse(any("Alice" in n["content"] for n in r["elements"]))

    def test_inline_bibliography_does_not_hide_conditional_items_or_inputs(self):
        for raw in [r"\iftrue\bibitem{k}Title.\fi",r"\input{missing}\bibitem{k}Title."]:
            with self.subTest(raw=raw),tempfile.TemporaryDirectory() as td:
                p=Path(td);(p/"main.tex").write_text(tex(r"See \cite{k}.\begin{thebibliography}{1}"+raw+r"\end{thebibliography}"),encoding="utf-8")
                self.assertEqual(parse_versions(AID,[(1,p)])["versions"][0]["status"],"parse_error")

    def test_math_text_argument_nested_dollars_preserves_entire_expression(self):
        expression=r"$w=70.7 \text{fA/$\sqrt{\text{Hz}}$}$"
        r=self.parse({"main.tex":tex("Current "+expression+" remains. After.")})
        self.assertEqual(r["versions"][0]["status"],"success")
        self.assertIn(expression," ".join(n["content"] for n in r["elements"]))

    def test_twocolumn_keeps_optional_title_material(self):
        r=self.parse({"main.tex":tex(r"\twocolumn[\begin{abstract}Opening abstract.\end{abstract}]\section{Intro}Body.\twocolumn End.")})
        self.assertEqual(r["versions"][0]["status"],"success")
        c=" ".join(n["content"] for n in r["elements"])
        for v in ["Opening abstract.","Intro","Body.","End."]:self.assertIn(v,c)

    def test_algorithm2e_source_keyword_declaration_preserves_arguments(self):
        source=tex(r"\begin{algorithm}\SetKwInOut{Input}{Given}\Input{Data.}\BlankLine\For{$i$}{\Indp Work.\Indm}\end{algorithm}").replace(r"\begin{document}",r"\usepackage{algorithm2e}\begin{document}")
        r=self.parse({"main.tex":source})
        self.assertEqual(r["versions"][0]["status"],"success")
        c=" ".join(n["content"] for n in r["elements"])
        for v in ["Given","Data.","Work."]:self.assertIn(v,c)


    def test_aaai_links_preserve_label_url_and_next_section(self):
        source=tex(r"\begin{links}\link{Code}{https://example.org/code}\link{Data}{https://example.org/data}\end{links}\section{Intro}Actual text.").replace(r"\begin{document}",r"\usepackage{aaai25}\begin{document}")
        r=self.parse({"main.tex":source})
        self.assertEqual(r["versions"][0]["status"],"success")
        c=" ".join(n["content"] for n in r["elements"])
        self.assertIn(r"\href{https://example.org/code}{Code}",c);self.assertIn("Actual text.",c)

    def test_font_symbols_remain_explicit_not_dropped_or_guessed(self):
        source=tex(r"Compass \faCompass, \ding{202} and sentence.").replace(r"\begin{document}",r"\usepackage{fontawesome5,pifont}\begin{document}")
        r=self.parse({"main.tex":source})
        self.assertEqual(r["versions"][0]["status"],"success")
        c=" ".join(n["content"] for n in r["elements"])
        self.assertIn(r"\faCompass{}",c);self.assertIn(r"\ding{202}",c)

    def test_icml_author_declarations_retained_separately_from_abstract(self):
        source=tex(r"\twocolumn[\icmltitle{Title}\begin{icmlauthorlist}\icmlauthor{Alice}{one}\end{icmlauthorlist}\icmlaffiliation{one}{Institute}]\begin{abstract}Real abstract.\end{abstract}\section{Intro}Real text.").replace(r"\begin{document}",r"\usepackage{icml2025}\begin{document}")
        r=self.parse({"main.tex":source})
        self.assertEqual(r["versions"][0]["status"],"success")
        c=" ".join(n["content"] for n in r["elements"])
        self.assertIn("Real abstract.",c);self.assertNotIn("Institute",c)
        self.assertTrue(any(m.get("environment")=="icmlauthorlist" for m in r["versions"][0]["layout_metadata"]))

    def test_biography_with_missing_picture_keeps_person_and_text(self):
        source=tex(r"\thebiography\begin{biographywithpic}{Alice}{missing.png}Research biography.\end{biographywithpic}").replace("{article}","{IEEEAerospaceCLS}")
        r=self.parse({"main.tex":source})
        self.assertEqual(r["versions"][0]["status"],"success")
        c=" ".join(n["content"] for n in r["elements"])
        self.assertIn("biography: Alice",c);self.assertIn("Research biography.",c)


    def test_single_token_formatting_argument_keeps_following_text(self):
        r=self.parse({"main.tex":tex(r"Inclination \textit i after.")})
        self.assertEqual(r["versions"][0]["status"],"success")
        self.assertIn("Inclination i after.",[n["content"] for n in r["elements"]])

    def test_sized_delimiters_in_prose_keep_literal_grouping(self):
        r=self.parse({"main.tex":tex(r"Near \big(together with $x$\big), text.")})
        self.assertEqual(r["versions"][0]["status"],"success")
        self.assertIn(r"\big(together with $x$\big)"," ".join(n["content"] for n in r["elements"]))

    def test_iop_indented_item_does_not_invent_compilation_date(self):
        source=tex(r"\begin{indented}\item Submitted: \today\end{indented}After.").replace("{article}","{iopart}")
        r=self.parse({"main.tex":source})
        self.assertEqual(r["versions"][0]["status"],"success")
        c=" ".join(n["content"] for n in r["elements"])
        self.assertIn(r"\today{}",c);self.assertIn("After.",c)
        self.assertTrue(any("unresolved" in note for note in r["versions"][0]["normalization_notes"]))

    def test_algorithm2e_single_line_if_and_return_keep_content(self):
        source=tex(r"\begin{algorithm}\lIf{$x$}{\KwRet{Solution.}}\end{algorithm}").replace(r"\begin{document}",r"\usepackage{algorithm2e}\begin{document}")
        r=self.parse({"main.tex":source})
        self.assertEqual(r["versions"][0]["status"],"success")
        self.assertIn("Solution.",[n["content"] for n in r["elements"]])


    def test_source_box_environment_preserves_title_body_and_citation(self):
        source=r"\documentclass{article}\usepackage{tcolorbox}\newenvironment{example}[1]{\begin{tcolorbox}[colback=gray!5!white,colframe=gray!75!black,title={#1}]}{\end{tcolorbox}}\begin{document}\begin{example}{Box title}\section{Inside}Actual content.\end{example}After.\end{document}"
        r=self.parse({"main.tex":source})
        self.assertEqual(r["versions"][0]["status"],"success")
        c=" ".join(n["content"] for n in r["elements"])
        for v in ["Box title","Inside","Actual content.","After."]:self.assertIn(v,c)

    def test_tcolorbox_executable_option_is_not_discarded(self):
        source=tex(r"\begin{tcolorbox}[before={Important text.}]Body.\end{tcolorbox}").replace(r"\begin{document}",r"\usepackage{tcolorbox}\begin{document}")
        r=self.parse({"main.tex":source})
        self.assertEqual(r["versions"][0]["status"],"parse_error")

    def test_literal_code_preserves_indentation_percent_and_unevaluated_commands(self):
        literal="\nif ready:\n    value = 10 % 3\n    " + r"\input{absent}\cite{missing}" + "\n"
        r=self.parse({"main.tex":tex(r"\section{Code}\begin{verbatim}"+literal+r"\end{verbatim}After.")})
        self.assertEqual(r["versions"][0]["status"],"success")
        code=next(n for n in r["elements"] if n["kind"]=="verbatim")
        self.assertEqual(code["content"],r"\begin{verbatim}"+literal+r"\end{verbatim}")
        self.assertIn("After.",[n["content"] for n in r["elements"]])

    def test_ieee_biography_requires_name_and_keeps_body_without_image(self):
        source=tex(r"\begin{IEEEbiography}[{\includegraphics{absent.png}}]{Alice}Biography text.\end{IEEEbiography}").replace("{article}","{IEEEtran}")
        r=self.parse({"main.tex":source})
        self.assertEqual(r["versions"][0]["status"],"success")
        self.assertIn("Biography text.",[n["content"] for n in r["elements"]])
        with tempfile.TemporaryDirectory() as td:
            p=Path(td);(p/"main.tex").write_text(source.replace("{Alice}",r"\noindent"),encoding="utf-8")
            self.assertEqual(parse_versions(AID,[(1,p)])["versions"][0]["status"],"parse_error")


    def test_literal_code_does_not_become_a_bibliography_or_comment_environment(self):
        literal="\n    [1, 2]\n"+r"\begin{comment}Keep this.\end{comment}\begin{thebibliography}{1}\bibitem{fake}Code.\end{thebibliography}"+"\n"
        r=self.parse({"main.tex":tex(r"\begin{verbatim}"+literal+r"\end{verbatim}Real body.")})
        self.assertEqual(r["versions"][0]["status"],"success")
        self.assertEqual(r["bibtex"],[])
        code=next(n["content"] for n in r["elements"] if n["kind"]=="verbatim")
        self.assertEqual(code,r"\begin{verbatim}"+literal+r"\end{verbatim}")

    def test_unused_literal_fragment_does_not_validate_or_introduce_document_root(self):
        r=self.parse({"main.tex":tex("Actual body."),"unused.tex":r"\begin{verbatim}"+chr(10)+r"\documentclass{article}\end{verbatim}"})
        self.assertEqual(r["versions"][0]["status"],"success")
        with tempfile.TemporaryDirectory() as td:
            p=Path(td);(p/"main.tex").write_text(tex("Actual body."),encoding="utf-8")
            (p/"unused.tex").write_text(r"\begin{verbatim}unfinished",encoding="utf-8")
            self.assertEqual(parse_versions(AID,[(1,p)])["versions"][0]["status"],"success")


    def test_formatting_unwrap_preserves_control_word_token_boundary(self):
        r=self.parse({"main.tex":tex(r"\textbf{Effects\quad}Table remains. {Near\quad}Next stays.")})
        self.assertEqual(r["versions"][0]["status"],"success")
        c=" ".join(n["content"] for n in r["elements"])
        self.assertIn("Effects Table remains.",c);self.assertIn("Near Next stays.",c)
        self.assertNotIn(r"\quadTable",c)

    def test_macro_expansion_preserves_control_word_before_following_letters(self):
        source=r"\documentclass{article}\usepackage{pifont}\newcommand{\mark}[1]{#1\ding{202}\quad}\begin{document}\mark{A}Next.\end{document}"
        r=self.parse({"main.tex":source})
        self.assertEqual(r["versions"][0]["status"],"success")
        self.assertIn("Next."," ".join(n["content"] for n in r["elements"]))

    def test_algorithmic_spacing_between_ends_keeps_nested_content(self):
        source=tex(r"\begin{algorithmic}\FOR{$i$}\FOR{$j$}\STATE Work.\ENDFOR\setstretch{1.4}\ENDFOR\end{algorithmic}").replace(r"\begin{document}",r"\usepackage{setspace}\begin{document}")
        r=self.parse({"main.tex":source})
        self.assertEqual(r["versions"][0]["status"],"success")
        self.assertIn("Work.",[n["content"] for n in r["elements"]])


    def test_literal_branch_keeps_command_token_boundary(self):
        r=self.parse({"main.tex":tex(r"\iftrue Near\quad\fi Next.")})
        self.assertEqual(r["versions"][0]["status"],"success")
        self.assertIn("Near Next.",[n["content"] for n in r["elements"]])


    def test_literal_jot_spacing_retains_following_math(self):
        source=tex(r"\setlength{\jot}{0.25em}\begin{align}x&=1\\y&=2\end{align}After.")
        r=self.parse({"main.tex":source})
        self.assertEqual(r["versions"][0]["status"],"success")
        self.assertIn("x&=1"," ".join(n["content"] for n in r["elements"]))
        self.assertIn("After.",[n["content"] for n in r["elements"]])


    def test_mathtools_text_dependency_keeps_prose_argument(self):
        source=tex(r"Use \text{nd.array} after.").replace(r"\begin{document}",r"\usepackage{mathtools}\begin{document}")
        r=self.parse({"main.tex":source})
        self.assertEqual(r["versions"][0]["status"],"success")
        self.assertIn("Use nd.array after.",[n["content"] for n in r["elements"]])


    def test_algorithmic_control_line_break_retains_following_statement(self):
        r=self.parse({"main.tex":tex(r"\begin{algorithmic}\For{$i$}\\\State Work.\EndFor\end{algorithmic}")})
        self.assertEqual(r["versions"][0]["status"],"success")
        self.assertIn("Work.",[n["content"] for n in r["elements"]])


    def test_nohyper_retains_content_and_following_section(self):
        r=self.parse({"main.tex":tex(r"\begin{NoHyper}Funding text.\end{NoHyper}\section{Appendix}Appendix text.")})
        self.assertEqual(r["versions"][0]["status"],"success")
        c=" ".join(n["content"] for n in r["elements"])
        self.assertIn("Funding text.",c);self.assertIn("Appendix text.",c)


    def test_literal_bibliography_spacing_keeps_following_prose(self):
        r=self.parse({"main.tex":tex(r"\setlength{\bibsep}{0pt}Before. After.")})
        self.assertEqual(r["versions"][0]["status"],"success")
        self.assertIn("After.",[n["content"] for n in r["elements"]])


    def test_empty_change_marker_preserves_following_conclusion(self):
        source=tex(r"\section{Conclusion}\chreplaced{}{}Real conclusion.").replace(r"\begin{document}",r"\usepackage{changes}\begin{document}")
        r=self.parse({"main.tex":source})
        self.assertEqual(r["versions"][0]["status"],"success")
        self.assertIn("Real conclusion.",[n["content"] for n in r["elements"]])
        with tempfile.TemporaryDirectory() as td:
            p=Path(td);(p/"main.tex").write_text(source.replace(r"\chreplaced{}{}",r"\chreplaced{New}{Old}"),encoding="utf-8")
            self.assertEqual(parse_versions(AID,[(1,p)])["versions"][0]["status"],"parse_error")


    def test_errors3_r2_optional_color_model_preserves_body_and_citation(self):
        r=self.parse({"main.tex":tex(r"Before \textcolor[rgb]{.9,.6,.5}{orange \textbf{boxes} \cite{k}} after.\bibliography{r}"),"r.bib":"@article{k,title={Source},author={A},year={2025}}"})
        self.assertEqual(r["versions"][0]["status"],"success")
        c=" ".join(n["content"] for n in r["elements"])
        self.assertIn(r"Before orange boxes \cite{k} after.",c)
        self.assertEqual(len(r["bibtex"]),1)

    def test_errors3_r2_revtex_acknowledgments_declaration_keeps_following_section(self):
        source=tex(r"\section{Results}Result.\acknowledgments Thanks to Ada.\section{Appendix}Appendix body.").replace("{article}","{revtex4-2}")
        r=self.parse({"main.tex":source})
        self.assertEqual(r["versions"][0]["status"],"success")
        nodes={n["element_id"]:n for n in r["elements"]}
        ack=next(n for n in r["elements"] if n["content"]=="acknowledgments")
        thanks=next(n for n in r["elements"] if n["content"]=="Thanks to Ada.")
        self.assertTrue(any(e["parent_id"]==ack["element_id"] and e["element_id"]==thanks["element_id"] for e in r["hierarchy"]))
        self.assertIn("Appendix body.",[n["content"] for n in r["elements"]])

    def test_errors3_r2_braced_acknowledgments_still_supported(self):
        r=self.parse({"main.tex":tex(r"\acks{Thanks to Ada.}\section{Next}Next body.")})
        self.assertEqual(r["versions"][0]["status"],"success")
        self.assertIn("Thanks to Ada.",[n["content"] for n in r["elements"]])

    def test_errors3_r2_svmult_star_title_and_abstract_keep_real_abstract(self):
        source=tex(r"\title*{Nested {Title}}\maketitle\abstract*{}\abstract{Abstract with $x$.}\section{Start}After abstract.").replace("{article}","{svmult}")
        r=self.parse({"main.tex":source})
        self.assertEqual(r["versions"][0]["status"],"success")
        c=" ".join(n["content"] for n in r["elements"])
        self.assertIn("Abstract with $x$.",c);self.assertIn("After abstract.",c)


    def test_errors3_r3_ieee_class_options_select_correct_branch(self):
        for option,expected,absent in [("compsoc","Society.","Ordinary."),("journal","Ordinary.","Society.")]:
            with self.subTest(option=option),tempfile.TemporaryDirectory() as td:
                p=Path(td);p.joinpath("main.tex").write_text(tex(r"\ifCLASSOPTIONcompsoc Society.\else Ordinary.\fi After.").replace("{article}","["+option+"]{IEEEtran}"),encoding="utf-8")
                r=parse_versions(AID,[(1,p)]);self.assertEqual(r["versions"][0]["status"],"success")
                c=" ".join(n["content"] for n in r["elements"]);self.assertIn(expected,c);self.assertNotIn(absent,c);self.assertIn("After.",c)

    def test_errors3_r3_pdf_condition_only_equal_branches(self):
        for body,status in [(r"\ifCLASSINFOpdf\else\fi Body.","success"),(r"\ifCLASSINFOpdf PDF text.\else DVI text.\fi","parse_error")]:
            with self.subTest(body=body),tempfile.TemporaryDirectory() as td:
                p=Path(td);p.joinpath("main.tex").write_text(tex(body).replace("{article}","{IEEEtran}"),encoding="utf-8")
                self.assertEqual(parse_versions(AID,[(1,p)])["versions"][0]["status"],status)

    def test_errors3_r3_changed_or_unknown_class_conditional_is_blocked(self):
        for body in [r"\CLASSOPTIONcompsoctrue\ifCLASSOPTIONcompsoc A.\else B.\fi",r"\ifCLASSOPTIONunknown A.\else B.\fi",r"\newcommand{\ifCLASSOPTIONcompsoc}{X}\ifCLASSOPTIONcompsoc A.\else B.\fi"]:
            with self.subTest(body=body),tempfile.TemporaryDirectory() as td:
                p=Path(td);p.joinpath("main.tex").write_text(tex(body).replace("{article}","{IEEEtran}"),encoding="utf-8")
                self.assertEqual(parse_versions(AID,[(1,p)])["versions"][0]["status"],"parse_error")


    def test_errors3_r4_exact_bibitem_alias_keeps_keys_and_separates_tree(self):
        source=tex(r"Body \cite{key}.\begin{thebibliography}{1}\bib{key}A.\newblock Real Title.\newblock 2025.\end{thebibliography}After.").replace(r"\begin{document}",r"\newcommand{\bib}{\bibitem}\begin{document}")
        r=self.parse({"main.tex":source});self.assertEqual(r["versions"][0]["status"],"success")
        self.assertEqual(r["bibtex"][0]["citation_key"],"key");self.assertEqual(r["bibtex"][0]["title"],"Real Title")
        c=" ".join(n["content"] for n in r["elements"]);self.assertIn(r"Body \cite{key}.",c);self.assertIn("After.",c);self.assertNotIn("Real Title",c)

    def test_errors3_r4_nontrivial_bib_macro_does_not_invent_items(self):
        source=tex(r"Body \cite{key}.\begin{thebibliography}{1}\bib{key}Real title.\end{thebibliography}").replace(r"\begin{document}",r"\newcommand{\bib}{Not a marker}\begin{document}")
        r=self.parse({"main.tex":source});self.assertEqual(r["versions"][0]["status"],"parse_error")
        self.assertIn("Cited bibliography entries unavailable",r["versions"][0]["error"])


    def test_errors3_r8_literal_spaced_bibitem_key_keeps_exact_citation(self):
        key="Forest-like permutations"
        r=self.parse({"main.tex":tex(r"See \cite[Section 3]{"+key+r"}.\begin{thebibliography}{1}\bibitem{"+key+r"}Author.\newblock Source title.\newblock 2025.\end{thebibliography}")})
        self.assertEqual(r["versions"][0]["status"],"success")
        self.assertEqual(r["bibtex"][0]["citation_key"],key)
        self.assertIn(r"\cite[Section 3]{"+key+"}"," ".join(n["content"] for n in r["elements"]))

    def test_errors3_r8_spaced_key_requires_exact_entry_no_guessing(self):
        r=self.parse({"main.tex":tex(r"See \cite{Two words}.\begin{thebibliography}{1}\bibitem{Twowords}Other.\end{thebibliography}")})
        self.assertEqual(r["versions"][0]["status"],"parse_error");self.assertIn("unavailable",r["versions"][0]["error"])

    def test_errors3_r8_empty_components_and_dynamic_or_unstable_keys_stay_errors(self):
        from src.m2.latex import citation_keys
        for keys in [",valid","valid,","valid,,other",r"\dynamic","Two  words","Two\twords"]:
            with self.subTest(keys=keys),self.assertRaises(ParseError):citation_keys(r"\cite{"+keys+"}")


    def test_errors3_r10_list_prefix_label_spacing_keeps_order_math_and_citation(self):
        r=self.parse({"main.tex":tex(r"\begin{enumerate}\label{list}\vspace*{-2mm}\setlength{\parskip}{0pt}\setlength{\parsep}{0pt}\item First $x$.\item Second \cite{k}.\end{enumerate}After.\begin{thebibliography}{1}\bibitem{k}A.\newblock Title.\newblock 2025.\end{thebibliography}")})
        self.assertEqual(r["versions"][0]["status"],"success")
        c=" ".join(n["content"] for n in r["elements"])
        self.assertLess(c.index("First $x$."),c.index(r"Second \cite{k}."));self.assertIn("After.",c)
        self.assertEqual(r["bibtex"][0]["citation_key"],"k")
        self.assertTrue(all(e["version"]==1 for e in r["hierarchy"]))

    def test_errors3_r10_list_prefix_cannot_hide_prose_or_dynamic_spacing(self):
        for prefix in ["Discarded text.",r"\vspace{\unknown}",r"\setlength{\parsep}{discarded}"]:
            with self.subTest(prefix=prefix),tempfile.TemporaryDirectory() as td:
                p=Path(td);p.joinpath("main.tex").write_text(tex(r"\begin{itemize}"+prefix+r"\item Body.\end{itemize}"),encoding="utf-8")
                self.assertEqual(parse_versions(AID,[(1,p)])["versions"][0]["status"],"parse_error")

    def test_errors3_r10_local_layout_macro_is_not_silently_discarded(self):
        source=tex(r"\begin{itemize}\vspace{1mm}\item Body.\end{itemize}").replace(r"\begin{document}",r"\renewcommand{\vspace}[1]{Meaningful text}\begin{document}")
        r=self.parse({"main.tex":source});self.assertEqual(r["versions"][0]["status"],"parse_error")

    def test_errors3_r5_unclosed_source_group_is_not_auto_repaired(self):
        r=self.parse({"main.tex":tex(r"Before. {\color{black} Meaningful text.")})
        self.assertEqual(r["versions"][0]["status"],"parse_error");self.assertIn("Unclosed",r["versions"][0]["error"])

    def test_errors3_r6_missing_tex_annotation_is_not_an_absent_image(self):
        r=self.parse({"main.tex":tex(r"\begin{figure}\input{drawing.pdf_tex}\caption{Diagram}\end{figure}")})
        self.assertEqual(r["versions"][0]["status"],"parse_error");self.assertIn("drawing.pdf_tex",r["versions"][0]["error"])

    def test_errors3_r7_conflicting_duplicate_fields_stay_explicit_errors(self):
        from src.m2.bibliography import parse_bibtex
        for field,one,two in [("title","Real book","(AMS-204)"),("doi","10.1/one","10.2/two"),("pages","1--2","8--9")]:
            with self.subTest(field=field),self.assertRaisesRegex(ParseError,"Conflicting duplicate"):
                parse_bibtex("@article{k,"+field+"={"+one+"},"+field+"={"+two+"}}")


    def test_errors3_r3_class_overrides_are_not_guessed(self):
        for preamble in [r"\let\ifCLASSOPTIONcompsoc\iftrue",r"\newif\ifCLASSOPTIONcompsoc",r"\PassOptionsToClass{compsoc}{IEEEtran}"]:
            with self.subTest(preamble=preamble),tempfile.TemporaryDirectory() as td:
                p=Path(td);p.joinpath("main.tex").write_text(r"\documentclass{IEEEtran}"+preamble+r"\begin{document}\ifCLASSOPTIONcompsoc First.\else Second.\fi\end{document}",encoding="utf-8")
                self.assertEqual(parse_versions(AID,[(1,p)])["versions"][0]["status"],"parse_error")

    def test_errors3_r4_redefined_marker_inside_bibliography_is_blocked(self):
        source=tex(r"Body \cite{k}.\begin{thebibliography}{1}\renewcommand{\bib}[1]{Not an item}\bib{k}Entry.\end{thebibliography}").replace(r"\begin{document}",r"\newcommand{\bib}{\bibitem}\begin{document}")
        r=self.parse({"main.tex":source});self.assertEqual(r["versions"][0]["status"],"parse_error")
        self.assertIn("marker redefinition",r["versions"][0]["error"])


    def test_errors3_r3_nonliteral_or_interacting_class_options_are_blocked(self):
        for opts in [r"\mode","compsoc,transmag","compsoc,comsoc"]:
            with self.subTest(opts=opts),tempfile.TemporaryDirectory() as td:
                p=Path(td);p.joinpath("main.tex").write_text(r"\documentclass["+opts+r"]{IEEEtran}\begin{document}\ifCLASSOPTIONcompsoc First.\else Second.\fi\end{document}",encoding="utf-8")
                r=parse_versions(AID,[(1,p)]);self.assertEqual(r["versions"][0]["status"],"parse_error")


    def test_125_si_keeps_quantity_unit_citation_and_following_text(self):
        source=tex(r"At \SI{50}{Hz} and \SI{10}{\mu eV}, see \cite{k}. After.\begin{thebibliography}{1}\bibitem{k}A.\newblock Title.\newblock 2025.\end{thebibliography}").replace(r"\begin{document}",r"\usepackage{siunitx}\begin{document}")
        r=self.parse({"main.tex":source});self.assertEqual(r["versions"][0]["status"],"success")
        c=" ".join(n["content"] for n in r["elements"]);self.assertIn(r"\SI{50}{Hz}",c);self.assertIn(r"\SI{10}{\mu eV}",c);self.assertIn("After.",c);self.assertEqual(r["bibtex"][0]["citation_key"],"k")

    def test_125_si_options_nested_preunit_preserved(self):
        source=tex(r"Before \SI[mode=text]{2.3}[\text{prefix}]{\micro s} after.").replace(r"\begin{document}",r"\usepackage{siunitx}\begin{document}")
        r=self.parse({"main.tex":source});self.assertEqual(r["versions"][0]["status"],"success");self.assertIn(r"\SI[mode=text]{2.3}[\text{prefix}]{\micro s}"," ".join(n["content"] for n in r["elements"]))

    def test_125_si_unknown_unit_or_dynamic_number_remains_error(self):
        for quantity in [r"\SI{1}{\unknownunit}",r"\SI{\countvalue}{Hz}"]:
            with self.subTest(quantity=quantity),tempfile.TemporaryDirectory() as td:
                p=Path(td);p.joinpath("main.tex").write_text(tex(quantity).replace(r"\begin{document}",r"\usepackage{siunitx}\begin{document}"),encoding="utf-8");self.assertEqual(parse_versions(AID,[(1,p)])["versions"][0]["status"],"parse_error")

    def test_125_si_local_definition_takes_precedence(self):
        source=tex(r"\SI{Meaningful text.}{After.}").replace(r"\begin{document}",r"\usepackage{siunitx}\newcommand{\SI}[2]{#1 #2}\begin{document}")
        r=self.parse({"main.tex":source});self.assertEqual(r["versions"][0]["status"],"success");self.assertIn("Meaningful text."," ".join(n["content"] for n in r["elements"]))


    def test_125_si_empty_unit_keeps_number_and_external_unit_text(self):
        source=tex(r"Rate \SI{20}{} counts per second. Probability \SI{1.3e-3}{}.").replace(r"\begin{document}",r"\usepackage{siunitx}\begin{document}")
        r=self.parse({"main.tex":source});self.assertEqual(r["versions"][0]["status"],"success");c=" ".join(n["content"] for n in r["elements"]);self.assertIn(r"\SI{20}{} counts per second.",c);self.assertIn(r"\SI{1.3e-3}{}",c)


    def test_125_newif_selected_definitions_and_includes_keep_correct_content(self):
        source=r"\documentclass{article}\newif\iflong\longtrue\iflong\newcommand{\name}{Correct}\else\newcommand{\name}{Wrong}\fi\begin{document}\input{body}\end{document}"
        r=self.parse({"main.tex":source,"body.tex":r"\iflong\name branch.\else\input{missing}\fi After."})
        self.assertEqual(r["versions"][0]["status"],"success");c=" ".join(n["content"] for n in r["elements"]);self.assertIn("Correct branch.",c);self.assertNotIn("Wrong",c);self.assertIn("After.",c)

    def test_125_newif_default_false_and_switch(self):
        r=self.parse({"main.tex":r"\documentclass{article}\newif\ifshow\begin{document}\ifshow Hidden.\else Default.\fi\showtrue\ifshow Now visible.\fi\end{document}"})
        self.assertEqual(r["versions"][0]["status"],"success");c=" ".join(n["content"] for n in r["elements"]);self.assertIn("Default.",c);self.assertIn("Now visible.",c);self.assertNotIn("Hidden.",c)

    def test_125_newif_scoped_or_redefined_flag_stays_error(self):
        for body in [r"\newif\ifshow{\showtrue}\ifshow A.\fi",r"{\newif\ifshow}\ifshow A.\fi",r"\newif\ifshow\newcommand{\ifshow}{X}\ifshow A.\fi"]:
            with self.subTest(body=body),tempfile.TemporaryDirectory() as td:
                p=Path(td);p.joinpath("main.tex").write_text(tex(body),encoding="utf-8");self.assertEqual(parse_versions(AID,[(1,p)])["versions"][0]["status"],"parse_error")

    def test_125_literal_if_character_comparison_keeps_selected_prose(self):
        for body,keep,drop in [(r"\if{and hidden.}\fi Kept.","Kept.","hidden"),(r"\if aa True.\else False.\fi After.","True.","False.")]:
            with self.subTest(body=body),tempfile.TemporaryDirectory() as td:
                p=Path(td);p.joinpath("main.tex").write_text(tex(body),encoding="utf-8");r=parse_versions(AID,[(1,p)]);self.assertEqual(r["versions"][0]["status"],"success");c=" ".join(n["content"] for n in r["elements"]);self.assertIn(keep,c);self.assertNotIn(drop,c)

    def test_125_if_with_expandable_operand_or_unknown_engine_stays_blocked(self):
        for body in [r"\if0\begin{quote}Unknown.\end{quote}\fi",r"\ifpdf PDF.\else DVI.\fi",r"\ifPDFTeX\usepackage{inputenc}\fi"]:
            with self.subTest(body=body),tempfile.TemporaryDirectory() as td:
                p=Path(td);p.joinpath("main.tex").write_text(tex(body),encoding="utf-8");self.assertEqual(parse_versions(AID,[(1,p)])["versions"][0]["status"],"parse_error")


    def test_125_biblatex_structured_fields_names_and_verbatim_url(self):
        from src.m2.bibliography import parse_bbl
        data=r"\refsection{0}\datalist[entry]{none}\entry{k}{article}{}{}\name{author}{1}{}{{{hash=x}{family={Doe},given={Jane\bibnamedelima Q.}}}}\field{title}{Exact {Title}}\field{year}{2025}\field{journaltitle}{Journal}"+"\n"+r"\verb{url}"+"\n"+r"\verb https://example.org/a%20b"+"\n"+r"\endverb\endentry\enddatalist\endrefsection\endinput"
        e=parse_bbl(data)[0];self.assertEqual(e["key"],"k");self.assertEqual(e["authors"],["Jane Q. Doe"]);self.assertEqual(e["title"],"Exact Title");self.assertEqual(e["year"],2025);self.assertEqual(e["journal"],"Journal");self.assertEqual(e["url"],"https://example.org/a%20b")

    def test_125_biblatex_normal_path_connects_citation_and_keeps_bibliography_out_of_tree(self):
        source=tex(r"Body \cite{k}.\printbibliography[heading=bibintoc]After.").replace(r"\begin{document}",r"\usepackage{biblatex}\addbibresource{missing.bib}\begin{document}")
        bbl=r"\refsection{0}\datalist[entry]{none}\entry{k}{article}{}{}\field{title}{Source reference title}\field{year}{2025}\endentry\enddatalist\endrefsection\endinput"
        r=self.parse({"main.tex":source,"main.bbl":bbl});self.assertEqual(r["versions"][0]["status"],"success");self.assertEqual(r["bibtex"][0]["citation_key"],"k");self.assertEqual(r["bibtex"][0]["authors"],None)
        c=" ".join(n["content"] for n in r["elements"]);self.assertIn(r"Body \cite{k}.",c);self.assertIn("After.",c);self.assertNotIn("Source reference title",c)

    def test_125_biblatex_unknown_structure_duplicates_and_scopes_stay_blocked(self):
        from src.m2.bibliography import parse_bbl
        template=r"\refsection{0}\datalist[entry]{none}%s\enddatalist\endrefsection\endinput"
        for raw in [r"\entry{k}{article}{}{}\unknown{Value}\endentry",r"\entry{k}{article}{}{}\field{title}{One}\field{title}{Two}\endentry",r"\entry{k}{article}{}{}\field{title}{One}\endentry"*2]:
            with self.subTest(raw=raw),self.assertRaises(ParseError):parse_bbl(template%raw)
        with self.assertRaises(ParseError):parse_bbl((template%r"\entry{k}{article}{}{}\field{title}{One}\endentry").replace("refsection{0}","refsection{1}"))

    def test_125_biblatex_filter_is_not_mistaken_for_heading_only(self):
        source=tex(r"Body \printbibliography[type=article]").replace(r"\begin{document}",r"\usepackage{biblatex}\begin{document}")
        r=self.parse({"main.tex":source});self.assertEqual(r["versions"][0]["status"],"parse_error");self.assertIn("Filtered",r["versions"][0]["error"])


    def test_125_si_standalone_unit_and_following_content(self):
        source=tex(r"Keep $t<200$~\si{ns}, then continue.").replace(r"\begin{document}",r"\usepackage{siunitx}\begin{document}")
        r=self.parse({"main.tex":source});self.assertEqual(r["versions"][0]["status"],"success");self.assertIn(r"$t<200$ \si{ns}, then continue."," ".join(n["content"] for n in r["elements"]))

    def test_125_biblatex_rendering_definition_is_not_a_compilation_conditional(self):
        source=tex(r"Body \cite{k}.\printbibliography").replace(r"\begin{document}",r"\usepackage{biblatex}\addbibresource{missing.bib}\renewbibmacro{in:}{\ifentrytype{article}{}{\printtext{in}}}\begin{document}")
        data=r"\refsection{0}\datalist[entry]{none}\entry{k}{article}{}{}\field{title}{Source title}\endentry\enddatalist\endrefsection"
        r=self.parse({"main.tex":source,"main.bbl":data});self.assertEqual(r["versions"][0]["status"],"success");self.assertEqual(r["bibtex"][0]["title"],"Source title");self.assertTrue(r["versions"][0]["bibliography_rendering_declarations"])


    def test_125_root_explicit_cvpr_response_is_independent_from_main(self):
        extra=tex(r"\title{Response}Response text.").replace(r"\begin{document}",r"\usepackage[rebuttal]{cvpr}\begin{document}")
        r=self.parse({"a.tex":tex(r"Main content.\input{app}"),"b.tex":extra,"app.tex":r"\section{Appendix}Main appendix."})
        self.assertEqual(r["versions"][0]["status"],"success");self.assertEqual(r["versions"][0]["root"],"a.tex");c=" ".join(n["content"] for n in r["elements"]);self.assertIn("Main appendix.",c);self.assertNotIn("Response text.",c)

    def test_125_root_explicit_letter_and_change_note_do_not_replace_article(self):
        letter=tex(r"\makelettertitle Dear editor.").replace("{article}","{moderncv}")
        changes=tex(r"\maketitle Changed experiments.").replace(r"\begin{document}",r"\author{Summary of Changes over Workshop Paper}\begin{document}")
        r=self.parse({"article.tex":tex("Actual article."),"one.tex":letter,"two.tex":changes})
        self.assertEqual(r["versions"][0]["status"],"success");self.assertEqual(r["versions"][0]["root"],"article.tex")

    def test_125_root_auxiliary_words_in_prose_or_macro_body_are_not_evidence(self):
        other=tex(r"\newcommand{\example}{\usepackage[rebuttal]{cvpr}}We discuss a Summary of Changes and cover letter.")
        r=self.parse({"one.tex":tex("One article."),"two.tex":other});self.assertIn("found 2",r["versions"][0]["error"])

    def test_125_biblatex_field_format_is_metadata_not_a_live_condition(self):
        source=tex("Body.").replace(r"\begin{document}",r"\usepackage{biblatex}\DeclareFieldFormat{url}{\ifhyperref{\href{#1}{#1}}{#1}}\begin{document}")
        r=self.parse({"main.tex":source});self.assertEqual(r["versions"][0]["status"],"success");self.assertIn("Body.",[n["content"] for n in r["elements"]])


    def test_125_root_response_metadata_def_does_not_hide_role(self):
        response=tex("Response only.").replace(r"\begin{document}",r"\usepackage[rebuttal]{cvpr}\def\paperID{123}\begin{document}")
        r=self.parse({"main.tex":tex("Main."),"response.tex":response});self.assertEqual(r["versions"][0]["status"],"success");self.assertEqual(r["versions"][0]["root"],"main.tex")


    def test_125_newif_environment_mutation_or_alias_is_blocked(self):
        for body in [r"\newif\ifshow\begin{quote}\showtrue\end{quote}\ifshow Unknown.\fi",r"\newif\ifshow\showtrue\let\ifshow\iffalse\ifshow Wrong.\fi",r"\newif\ifshow\let\showtrue\relax\showtrue\ifshow Wrong.\fi"]:
            with self.subTest(body=body),tempfile.TemporaryDirectory() as td:
                p=Path(td);p.joinpath("main.tex").write_text(tex(body),encoding="utf-8");self.assertEqual(parse_versions(AID,[(1,p)])["versions"][0]["status"],"parse_error")

    def test_125_biblatex_missing_name_count_and_malformed_verb_do_not_pass(self):
        from src.m2.bibliography import parse_bbl
        for body in [r"\name{author}{2}{}{{{hash=x}{family={Doe}}}}",r"\verb{url}\verb broken\endverb"]:
            with self.subTest(body=body),self.assertRaises(ParseError):parse_bbl(r"\refsection{0}\datalist[entry]{none}\entry{k}{article}{}{}"+body+r"\endentry\enddatalist\endrefsection")


    def test_125_iff_math_relation_is_not_a_conditional_predicate(self):
        r=self.parse({"main.tex":tex(r"A $x\iff y$ relation.\iftrue Also $a\iff b$.\fi After.")})
        self.assertEqual(r["versions"][0]["status"],"success");c=" ".join(n["content"] for n in r["elements"]);self.assertIn(r"$x\iff y$",c);self.assertIn(r"$a\iff b$",c);self.assertIn("After.",c)

    def test_125_overridden_condition_primitives_are_not_assumed(self):
        for decl,body in [(r"\newcommand{\newif}[1]{Not primitive}",r"\newif\ifshow"),(r"\newcommand{\if}[2]{Not primitive}",r"\if aa True.\fi"),(r"\newcommand{\ifpdf}{Not engine}",r"\ifpdf\else\fi")]:
            with self.subTest(decl=decl),tempfile.TemporaryDirectory() as td:
                p=Path(td);p.joinpath("main.tex").write_text(tex(decl+body),encoding="utf-8");self.assertEqual(parse_versions(AID,[(1,p)])["versions"][0]["status"],"parse_error")
