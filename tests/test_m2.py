"""M2 regression and CLI integration tests; synthetic fixtures only, offline."""
import hashlib
import json
from pathlib import Path
import socket
import tempfile
import unittest

import pytest
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

    def test_to01_included_fragment_is_not_an_independent_document(self):
        for index, include in enumerate(("input", "include")):
            with self.subTest(command=include):
                result = self.parse({
                    "z-article.tex": tex(r"\section{Main}Main text." + "\\"
                                        + include + r"{extras/part}"),
                    "extras/part.tex": (r"\iffalse\documentclass{article}\fi"
                                        r"\section{Appendix}Included evidence \cite{k}."
                                        r"\begin{thebibliography}{1}\bibitem{k}Source reference."
                                        r"\end{thebibliography}"),
                }, aid=f"2508.{7872 + index:05d}")
                record = result["versions"][0]
                self.assertEqual(record["status"], "success", record)
                self.assertEqual(record["root"], "z-article.tex")
                self.assertEqual(set(record["included_files"]), {"z-article.tex", "extras/part.tex"})
                self.assertEqual(record["root_selection"]["policy"],
                                 "unique_main_with_explicit_included_fragments")
                content = " ".join(row["content"] for row in result["elements"])
                self.assertIn("Main text.", content)
                self.assertIn(r"Included evidence \cite{k}.", content)
                self.assertNotIn("Source reference.", content)
                self.assertEqual(result["bibtex"][0]["citation_key"], "k")
                self.assertTrue(all(edge["version"] == 1 for edge in result["hierarchy"]))
                validate_tables(result)

    def test_to01_unlinked_or_uncertain_fragment_keeps_root_ambiguity(self):
        fragment = r"\documentclass{article} Fragment content."
        bodies = [
            "Main text.",
            "%\\input{extra}\nMain text.",
            r"\newcommand{\unused}{\input{extra}}Main text.",
            r"\iffalse\input{extra}\fi Main text.",
        ]
        for index, body in enumerate(bodies):
            with self.subTest(body=body):
                result = self.parse({"article.tex": tex(body), "extra.tex": fragment},
                                    aid=f"2508.{7872 + index:05d}")
                self.assertIn("found 2", result["versions"][0]["error"])

    def test_to01_supplemental_information_with_literal_metadata_definition(self):
        supplement = (r"\def\CTeXPreproc{Created by ctex v0.2.12}"
                      r"\documentclass{article}\begin{document}"
                      r"\title{Supplemental Information: A study}Separate results."
                      r"\end{document}")
        result = self.parse({"a.tex": supplement, "z.tex": tex(r"\section{Main}Actual study.")})
        record = result["versions"][0]
        self.assertEqual(record["status"], "success", record)
        self.assertEqual(record["root"], "z.tex")
        self.assertEqual(record["included_files"], ["z.tex"])
        content = " ".join(row["content"] for row in result["elements"])
        self.assertIn("Actual study.", content)
        self.assertNotIn("Separate results.", content)

    def test_to01_two_independent_documents_or_redefined_role_remain_ambiguous(self):
        for index, second in enumerate((
            tex(r"\title{An equally valid article}Other study."),
            r"\def\title{Supplemental Information}\documentclass{article}"
            r"\begin{document}\title{Other study}Other study.\end{document}",
            r"\def\metadata{\title{Supplemental Information}}\documentclass{article}"
            r"\begin{document}\metadata Other study.\end{document}",
        )):
            with self.subTest(second=second):
                record = self.parse({"one.tex": tex("Main study."), "two.tex": second},
                                    aid=f"2508.{7872 + index:05d}")["versions"][0]
                self.assertEqual(record["status"], "parse_error")
                self.assertIn("found 2", record["error"])

    def test_to_batch_three_roots_follow_verified_subfile_parent_and_keep_content(self):
        files = {
            "combined.tex": (r"\documentclass{article}\usepackage{subfiles}"
                             r"\newcommand{\shared}{Inherited marker.}\begin{document}"
                             r"\subfile{article}\appendix\subfile{supp}\bibliography{refs}\end{document}"),
            "article.tex": (r"\documentclass[combined.tex]{subfiles}"
                            r"\renewcommand{\shared}{WRONG child preamble.}\begin{document}"
                            r"\section{Main}Body \shared\cite{k}.\end{document}\textit{}"),
            "supp.tex": (r"\documentclass[combined.tex]{subfiles}\begin{document}"
                         r"\section{Supplement}Supplement evidence.\end{document}"),
            "refs.bib": "@article{k,title={Exact source},author={Alice},year={2025}}",
        }
        result = self.parse(files)
        record = result["versions"][0]
        self.assertEqual(record["status"], "success", record)
        self.assertEqual(record["root"], "combined.tex")
        self.assertEqual(set(record["included_files"]), {"combined.tex", "article.tex", "supp.tex"})
        content = " ".join(row["content"] for row in result["elements"])
        self.assertIn("Inherited marker.", content)
        self.assertNotIn("WRONG child preamble.", content)
        self.assertIn("Supplement evidence.", content)
        self.assertIn(r"\cite{k}", content)
        self.assertEqual(result["bibtex"][0]["citation_key"], "k")
        self.assertTrue(all(edge["version"] == 1 for edge in result["hierarchy"]))
        self.assertTrue(any("Post-document source excluded" in note for note in record["normalization_notes"]))
        validate_tables(result)

    def test_to_batch_standalone_inputs_keep_body_and_do_not_end_parent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "main.tex").write_text(
                r"\documentclass{article}\usepackage{standalone}\begin{document}"
                r"Before.\input{drawing}After.\end{document}", encoding="utf-8")
            (root / "drawing.tex").write_text(
                r"\documentclass{standalone}\begin{document}Drawing body.\end{document}", encoding="utf-8")
            gatherer = Gatherer(root)
            selected, body = gatherer.gather()
            self.assertEqual(selected, "main.tex")
            self.assertIn("Before.", body)
            self.assertIn("Drawing body.", body)
            self.assertIn("After.", body)
            self.assertNotIn(r"\documentclass", body)
            self.assertNotIn(r"\end{document}", body)
            result = parse_versions(AID, [(1, root)])
            self.assertEqual(result["versions"][0]["status"], "success", result["versions"])

    def test_to_batch_unlinked_standalone_and_independent_articles_stay_ambiguous(self):
        record = self.parse({
            "one.tex": tex("One real article."),
            "two.tex": tex("Another real article."),
            "drawing.tex": r"\documentclass{standalone}\begin{document}Figure.\end{document}",
        })["versions"][0]
        self.assertIn("found 3", record["error"])

    def test_to_batch_four_mirror_roots_require_same_bytes_and_bibliography(self):
        main = tex(r"\section{Main}Shared study \cite{k}.\bibliography{references}")
        supplement = (r"\documentclass{osa-supplemental-document}\begin{document}"
                      r"\title{Supplemental Document}Separate supplement.\end{document}")
        bib = "@article{k,title={Source},author={Alice}}"
        files = {"main.tex": main, "Supplement.tex": supplement, "references.bib": bib,
                 "mirror/main.tex": main, "mirror/Supplement.tex": supplement,
                 "mirror/references.bib": bib}
        result = self.parse(files)
        record = result["versions"][0]
        self.assertEqual(record["status"], "success", record)
        self.assertEqual(record["root"], "main.tex")
        self.assertEqual(len(record["root_selection"]["equivalent_root_copies"]), 2)
        self.assertEqual(record["included_files"], ["main.tex"])
        content = " ".join(row["content"] for row in result["elements"])
        self.assertIn("Shared study", content)
        self.assertNotIn("Separate supplement.", content)
        self.assertEqual(result["bibtex"][0]["title"], "Source")

    def test_to_batch_mirror_title_or_text_without_resource_equivalence_is_ambiguous(self):
        main = tex(r"Same study \cite{k}.\bibliography{refs}")
        result = self.parse({
            "main.tex": main, "refs.bib": "@article{k,title={One source}}",
            "mirror/main.tex": main, "mirror/refs.bib": "@article{k,title={Different source}}",
        })
        self.assertIn("found 2", result["versions"][0]["error"])

    def test_to_batch_mirror_with_relative_input_graph_is_not_assumed_equivalent(self):
        result = self.parse({
            "main.tex": tex(r"\input{part}"), "part.tex": "One.",
            "mirror/main.tex": tex(r"\input{part}"), "mirror/part.tex": "Different.",
        })
        self.assertIn("found 2", result["versions"][0]["error"])

    def test_to_batch_dotted_stems_resolve_without_replacing_the_stem(self):
        result = self.parse({
            "main.tex": tex(r"\section{Main}\input{parts/3.1_intro}\include{parts/3.2_more}"),
            "parts/3.1_intro.tex": "First content.",
            "parts/3.2_more.tex": "Second content.",
        })
        record = result["versions"][0]
        self.assertEqual(record["status"], "success", record)
        self.assertEqual(record["included_files"],
                         ["main.tex", "parts/3.1_intro.tex", "parts/3.2_more.tex"])
        content = " ".join(row["content"] for row in result["elements"])
        self.assertLess(content.index("First content."), content.index("Second content."))

    def test_to_batch_exact_existing_resource_wins_over_suffix_fallback(self):
        result = self.parse({
            "main.tex": tex(r"\input{part.v1}"),
            "part.v1": "Exact resource.",
            "part.v1.tex": "Wrong fallback.",
        })
        record = result["versions"][0]
        self.assertEqual(record["status"], "success", record)
        self.assertIn("part.v1", record["included_files"])
        self.assertNotIn("part.v1.tex", record["included_files"])
        self.assertIn("Exact resource.", " ".join(row["content"] for row in result["elements"]))

    def test_to_batch_dotted_missing_or_escaping_resource_still_fails(self):
        for index, target in enumerate(("3.1_absent", "../../outside.1")):
            record = self.parse({"main.tex": tex(r"\input{" + target + "}")},
                                aid=f"2508.{7900 + index:05d}")["versions"][0]
            self.assertEqual(record["status"], "parse_error")
            self.assertTrue("Missing compilation resource" in record["error"] or
                            "Source escapes version directory" in record["error"])

    def test_to_batch_nonutf8_comments_do_not_require_guessing_prose_encoding(self):
        raw = (b"\\documentclass{article}\n% Legacy comment \x93\x94\xd7\xd4\n"
               b"\\begin{document}\\section{Main}Caf\xc3\xa9 evidence.\\end{document}")
        result = self.parse({"main.tex": raw})
        record = result["versions"][0]
        self.assertEqual(record["status"], "success", record)
        self.assertIn("Caf\u00e9 evidence.", " ".join(row["content"] for row in result["elements"]))
        self.assertIn("comments", record["encodings"]["main.tex"])
        self.assertTrue(any("Non-UTF8 bytes confined" in note for note in record["normalization_notes"]))

    def test_to_batch_unknown_encoding_in_prose_or_literal_is_not_discarded(self):
        bodies = [b"Invalid active \x93text.",
                  b"Escaped percent \\% invalid \x93.",
                  b"\\begin{verbatim}% literal \x93\\end{verbatim}"]
        for index, body in enumerate(bodies):
            result = self.parse({"main.tex": b"\\documentclass{article}\\begin{document}" +
                                body + b"\\end{document}"}, aid=f"2508.{7910 + index:05d}")
            self.assertIn("Unknown source encoding", result["versions"][0]["error"])

    def test_to_batch_legacy_declared_encoding_is_still_honored(self):
        raw = (b"\\documentclass{article}\\usepackage[latin1]{inputenc}"
               b"\\begin{document}Caf\xe9 text.\\end{document}")
        result = self.parse({"main.tex": raw})
        self.assertEqual(result["versions"][0]["status"], "success", result["versions"])
        self.assertIn("Caf\u00e9 text.", " ".join(row["content"] for row in result["elements"]))

    def test_to_batch_size_limit_is_enforced_by_a_bounded_read(self):
        from unittest.mock import mock_open
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "anthology.bib"
            source.write_bytes(b"unchanged")
            mocked = mock_open(read_data=b"x" * (8 * 1024 * 1024 + 2))
            with patch.object(Path, "open", mocked):
                with self.assertRaisesRegex(ParseError, "exceeds 8 MiB: anthology.bib"):
                    Gatherer(root).read(source, comments=False)
                mocked().read.assert_called_once_with(8 * 1024 * 1024 + 1)
            self.assertEqual(source.read_bytes(), b"unchanged")

    def test_to_batch_missing_tex_libraries_are_not_silently_ignored(self):
        for index, lib in enumerate(("epsf", "xy")):
            result = self.parse({"main.tex": tex(r"\input " + lib + "\nVisible content.")},
                                aid=f"2508.{7920 + index:05d}")
            self.assertEqual(result["versions"][0]["status"], "parse_error")
            self.assertIn(f"Missing compilation resource: {lib}.tex", result["versions"][0]["error"])

    def test_to_batch_existing_library_named_source_is_read_not_discarded(self):
        result = self.parse({"main.tex": tex(r"\input{epsf}"),
                             "epsf.tex": "Actual supplied content."})
        self.assertEqual(result["versions"][0]["status"], "success", result["versions"])
        self.assertIn("Actual supplied content.", " ".join(row["content"] for row in result["elements"]))

    def test_to_batch_literal_enddocument_terminates_without_inventing_missing_boundary(self):
        result = self.parse({"main.tex": r"\documentclass{article}\begin{document}"
                             r"\section{Main}Preserved.\enddocument Ignored after termination."})
        self.assertEqual(result["versions"][0]["status"], "success", result["versions"])
        content = " ".join(row["content"] for row in result["elements"])
        self.assertIn("Preserved.", content)
        self.assertNotIn("Ignored after termination.", content)

    def test_to_batch_included_document_terminator_stops_the_compilation_path(self):
        result = self.parse({
            "main.tex": tex(r"\section{Main}Before.\input{appendix}Unreachable.\input{missing}"),
            "appendix.tex": r"\section{Appendix}Retained appendix.\end{document}Unreachable appendix tail.",
        })
        record = result["versions"][0]
        self.assertEqual(record["status"], "success", record)
        self.assertEqual(record["included_files"], ["main.tex", "appendix.tex"])
        content = " ".join(row["content"] for row in result["elements"])
        self.assertIn("Before.", content)
        self.assertIn("Retained appendix.", content)
        self.assertNotIn("Unreachable", content)
        self.assertTrue(any("termination in included source" in note for note in record["normalization_notes"]))

    def test_to_batch_missing_or_multiple_document_starts_remain_errors(self):
        for index, source in enumerate((
                r"\documentclass{article}\begin{document}No end.",
                r"\documentclass{article}\begin{document}\begin{document}Two starts.\end{document}",
                r"\documentclass{article}\def\enddocument{Custom text.}\begin{document}Body.\enddocument")):
            record = self.parse({"main.tex": source}, aid=f"2508.{7930 + index:05d}")["versions"][0]
            self.assertEqual(record["status"], "parse_error")

    def test_to_batch_subfile_fragments_with_executed_package_preserve_order(self):
        result = self.parse({
            "main.tex": (r"\documentclass{article}\usepackage{amsmath,subfiles}"
                         r"\newif\ifshow\showfalse\begin{document}"
                         r"\section{Main}Before.\subfile{sections/part}After.\end{document}"),
            "sections/part.tex": r"\section{Extra}Subfile fragment.",
        })
        record = result["versions"][0]
        self.assertEqual(record["status"], "success", record)
        content = " ".join(row["content"] for row in result["elements"])
        self.assertLess(content.index("Before."), content.index("Subfile fragment."))
        self.assertLess(content.index("Subfile fragment."), content.index("After."))

    def test_to_batch_subfile_without_package_or_with_wrong_parent_is_rejected(self):
        record = self.parse({"main.tex": tex(r"\subfile{part}"),
                             "part.tex": "Not implicitly supported."})["versions"][0]
        self.assertIn("subfiles package", record["error"])
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "other.tex").write_text(tex("Other."), encoding="utf-8")
            g = Gatherer(root)
            with self.assertRaisesRegex(ParseError, "outside the active compilation path"):
                g.included_document_body(root / "part.tex",
                    r"\documentclass[other.tex]{subfiles}\begin{document}Body.\end{document}",
                    (root / "main.tex",), "subfile")
            with self.assertRaisesRegex(ParseError, "included document terminator"):
                g.included_document_body(root / "part.tex", r"Body.\enddocument Hidden tail.",
                                         (root / "main.tex",), "subfile")

    def test_to_batch_aliased_document_terminator_remains_unverified(self):
        record = self.parse({"main.tex": r"\documentclass{article}\let\enddocument\relax"
                             r"\begin{document}Body.\enddocument"})["versions"][0]
        self.assertEqual(record["status"], "parse_error")
        self.assertIn("redefined document terminator", record["error"])

    def test_to_batch_commented_encoding_declaration_does_not_validate_active_bytes(self):
        raw = (b"\\documentclass{article}\n%\\usepackage[latin1]{inputenc}\n"
               b"\\begin{document}Unknown \xe9 byte.\\end{document}")
        record = self.parse({"main.tex": raw})["versions"][0]
        self.assertIn("Unknown source encoding", record["error"])

    def test_to_batch_redefined_subfile_and_custom_package_modes_stay_blocked(self):
        sources = [
            r"\documentclass{article}\usepackage{subfiles}\newcommand{\subfile}[1]{Custom body.}"
            r"\begin{document}\subfile{part}\end{document}",
            r"\documentclass{article}\usepackage[subpreambles=true]{standalone}"
            r"\begin{document}Body.\end{document}",
        ]
        for index, source in enumerate(sources):
            record = self.parse({"main.tex": source, "part.tex": "Original content."},
                                aid=f"2508.{7940 + index:05d}")["versions"][0]
            self.assertEqual(record["status"], "parse_error")
            self.assertIn("redefined subfile" if index == 0 else "package options", record["error"])

    def test_root_fix01_braced_and_unbraced_input_preserve_all_document_bodies(self):
        results = []
        for syntax in (r"\input{fig}", "\\input fig\n"):
            with self.subTest(syntax=syntax), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                (root / "main.tex").write_text(
                    r"\documentclass{article}\usepackage{standalone}\begin{document}"
                    + "Main content.\n" + syntax + "\nMore content." + r"\end{document}",
                    encoding="utf-8")
                (root / "fig.tex").write_text(
                    r"\documentclass{standalone}\begin{document}Figure content.\end{document}",
                    encoding="utf-8")
                result = parse_versions(AID, [(1, root)])
                record = result["versions"][0]
                self.assertEqual(record["status"], "success", record)
                self.assertEqual(record["root"], "main.tex")
                self.assertEqual(record["included_files"], ["main.tex", "fig.tex"])
                evidence = record["root_selection"]["excluded_documents"][0]
                self.assertEqual(evidence["file"], "fig.tex")
                self.assertEqual(evidence["evidence"][0]["kind"], "included_document_body")
                content = " ".join(row["content"] for row in result["elements"])
                self.assertLess(content.index("Main content."), content.index("Figure content."))
                self.assertLess(content.index("Figure content."), content.index("More content."))
                validate_tables(result)
                results.append(result)
        self.assertEqual(results[0], results[1])

    def test_root_fix01_dynamic_or_escaping_filename_cannot_select_a_root(self):
        forms = [r"\input \filename", r"\input fig\suffix", r"\input{fig\suffix}",
                 r"\input fig#1", r"\input $filename", r"\input ../../fig"]
        for index, invocation in enumerate(forms):
            with self.subTest(invocation=invocation):
                record = self.parse({
                    "main.tex": r"\documentclass{article}\usepackage{standalone}\begin{document}"
                                + invocation + "\nMore content." + r"\end{document}",
                    "fig.tex": r"\documentclass{standalone}\begin{document}Figure content.\end{document}",
                }, aid=f"2508.{7950 + index:05d}")["versions"][0]
                self.assertEqual(record["status"], "parse_error")
                self.assertIn("found 2", record["error"])

    def test_root_fix01_unbraced_include_and_subfile_do_not_gain_root_evidence(self):
        from src.m2.latex import root_document_inputs
        for name in ("include", "subfile"):
            with self.subTest(command=name):
                probe = (r"\documentclass{article}\begin{document}"
                         + "\\" + name + " fig\n" + r"\end{document}")
                self.assertEqual(root_document_inputs(probe), [])

    def test_root_fix01_expand_also_rejects_a_dynamic_filename_suffix(self):
        record = self.parse({"main.tex": tex("\\input fig\\suffix\n"),
                             "fig.tex": "Wrong prefix file."})["versions"][0]
        self.assertEqual(record["status"], "parse_error")
        self.assertIn("Dynamic unbraced input filename", record["error"])

    def test_root_fix02_literal_false_document_is_not_an_independent_root(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            main = tex("Main content.")
            notes = ("\\iffalse\n" + tex("Inactive document.") + "\n\\fi\n")
            (root / "main.tex").write_text(main, encoding="utf-8")
            (root / "notes.tex").write_text(notes, encoding="utf-8")
            raw = (root / "notes.tex").read_bytes()
            result = parse_versions(AID, [(1, root)])
            record = result["versions"][0]
            self.assertEqual(record["status"], "success", record)
            self.assertEqual(record["root"], "main.tex")
            self.assertEqual(record["included_files"], ["main.tex"])
            content = " ".join(row["content"] for row in result["elements"])
            self.assertIn("Main content.", content)
            self.assertNotIn("Inactive document.", content)
            self.assertEqual((root / "notes.tex").read_bytes(), raw)
            validate_tables(result)

    def test_root_fix02_nested_else_unknown_or_unbalanced_blocks_remain_ambiguous(self):
        inactive = tex("Uncertain document.")
        notes = [
            r"\iffalse\iftrue" + inactive + r"\fi\fi",
            r"\iffalse" + inactive + r"\else Active branch.\fi",
            r"\ifpdf" + inactive + r"\fi",
            r"\ifcustom" + inactive + r"\fi",
            r"\iffalse" + inactive,
            r"\iffalse" + inactive + r"\fi\fi",
            r"\def\iffalse{\iftrue}\iffalse" + inactive + r"\fi",
            r"\let\iffalse\iftrue\iffalse" + inactive + r"\fi",
            "{" + r"\iffalse" + inactive + r"\fi}",
        ]
        for index, source in enumerate(notes):
            with self.subTest(source=source):
                record = self.parse({"main.tex": tex("Main."), "notes.tex": source},
                                    aid=f"2508.{7960 + index:05d}")["versions"][0]
                self.assertEqual(record["status"], "parse_error")
                self.assertIn("found 2", record["error"])

    def test_root_fix02_a_truly_independent_document_is_still_a_root(self):
        source = r"\iffalse Ignored text.\fi" + tex("Another actual document.")
        record = self.parse({"main.tex": tex("Main."), "notes.tex": source})["versions"][0]
        self.assertEqual(record["status"], "parse_error")
        self.assertIn("found 2", record["error"])

    def test_root_fix02_comments_and_separate_simple_blocks_are_safe(self):
        notes = ("%\\else \\ifpdf ignored comment\n" + r"\iffalse" +
                 tex("First inactive.") + "\\fi\n" +
                 r"\iffalse" + tex("Second inactive.") + r"\fi")
        result = self.parse({"main.tex": tex("Actual main."), "notes.tex": notes})
        self.assertEqual(result["versions"][0]["status"], "success", result["versions"])
        self.assertEqual(result["versions"][0]["included_files"], ["main.tex"])

    def test_root_fix02_literal_scanner_ambiguity_is_not_erased(self):
        sources = [
            r"\iffalse\documentclass{article}\begin{document}"
            r"\begin{verbatim}\fi\end{verbatim}Uncertain.\end{document}\fi",
            r"\iffalse\begin{comment}\else\end{comment}"
            + tex("Potentially active document.") + r"\fi",
        ]
        for index, notes in enumerate(sources):
            with self.subTest(notes=notes):
                record = self.parse({"main.tex": tex("Main."), "notes.tex": notes},
                                    aid=f"2508.{7980 + index:05d}")["versions"][0]
                self.assertIn("found 2", record["error"])

    def test_root_fix03_inline_verb_and_starred_delimiters_are_not_roots(self):
        forms = [r"\verb|\documentclass{article}|",
                 r"\verb*|\documentclass{article}|",
                 r"\verb+\documentclass{article}+",
                 r"\verb*!\documentclass{article}!",
                 r"\verb@\documentclass{article}@",
                 r"\verb#\documentclass{article}#"]
        for index, literal in enumerate(forms):
            with self.subTest(literal=literal):
                result = self.parse({"main.tex": tex("Main content."), "notes.tex": literal},
                                    aid=f"2508.{8000 + index:05d}")
                record = result["versions"][0]
                self.assertEqual(record["status"], "success", record)
                self.assertEqual(record["root"], "main.tex")
                self.assertEqual(record["included_files"], ["main.tex"])
                content = " ".join(row["content"] for row in result["elements"])
                self.assertIn("Main content.", content)
                self.assertNotIn("documentclass", content)
                validate_tables(result)

    def test_root_fix03_active_independent_document_is_not_hidden_by_inline_literal(self):
        for index, source in enumerate((
                tex("A real second document."),
                r"\verb|\documentclass{fake}|" + tex("A real second document."))):
            with self.subTest(source=source):
                record = self.parse({"main.tex": tex("Main."), "notes.tex": source},
                                    aid=f"2508.{8010 + index:05d}")["versions"][0]
                self.assertEqual(record["status"], "parse_error")
                self.assertIn("found 2", record["error"])

    def test_root_fix03_malformed_inline_literals_cannot_silently_remove_candidate(self):
        sources = [
            r"\verb|\documentclass{article}",
            "\\verb|unfinished\n" + r"\documentclass{article}|",
            "\\verb*+unfinished\r\n" + r"\documentclass{article}+",
            r"\verb |\documentclass{article}|",
            r"\verb* |\documentclass{article}*",
        ]
        for index, source in enumerate(sources):
            with self.subTest(source=source):
                record = self.parse({"main.tex": tex("Main."), "notes.tex": source},
                                    aid=f"2508.{8020 + index:05d}")["versions"][0]
                self.assertEqual(record["status"], "parse_error")
                self.assertTrue("found 2" in record["error"] or
                                "Unclosed inline verbatim" in record["error"], record)

    def test_root_fix03_mask_is_local_and_does_not_change_expanded_source(self):
        from src.m2.latex import mask_root_inline_verbatim
        probe = r"Before \verb|\documentclass{fake}| after \documentclass{article}"
        masked = mask_root_inline_verbatim(probe)
        self.assertEqual(len(masked), len(probe))
        self.assertTrue(masked.startswith("Before "))
        self.assertTrue(masked.endswith(r" after \documentclass{article}"))
        self.assertNotIn(r"\documentclass{fake}", masked)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            body = r"Before \verb|\documentclass{fake}| and \verb*+\documentclass{fake}+ after."
            source = tex(body)
            (root / "main.tex").write_text(source, encoding="utf-8")
            raw = (root / "main.tex").read_bytes()
            selected, gathered = Gatherer(root).gather()
            self.assertEqual(selected, "main.tex")
            expected = raw.decode("utf-8").split(r"\begin{document}", 1)[1].rsplit(r"\end{document}", 1)[0]
            self.assertEqual(gathered, expected)
            self.assertEqual((root / "main.tex").read_bytes(), raw)

    def test_root_fix03_control_word_prefixes_and_escaped_slashes_are_not_verb(self):
        for index, source in enumerate((
                r"\verbatim|\documentclass{article}|",
                r"\\verb|\documentclass{article}|")):
            with self.subTest(source=source):
                record = self.parse({"main.tex": tex("Main."), "notes.tex": source},
                                    aid=f"2508.{8030 + index:05d}")["versions"][0]
                self.assertIn("found 2", record["error"])

    def test_root_fix04_transitive_fragment_preserves_order_and_citation(self):
        result = self.parse({
            "main.tex": (r"\documentclass{article}\usepackage{standalone}\begin{document}"
                         r"\section{Main}Main before.\input{section}Main after."
                         r"\bibliography{refs}\end{document}"),
            "section.tex": r"Section before \cite{k}.\input figure" + "\nSection after.",
            "figure.tex": r"\documentclass{standalone}\begin{document}Figure content.\end{document}",
            "refs.bib": "@article{k,title={Source evidence},author={Alice},year={2025}}",
        })
        record = result["versions"][0]
        self.assertEqual(record["status"], "success", record)
        self.assertEqual(record["root"], "main.tex")
        self.assertEqual(record["included_files"], ["main.tex", "section.tex", "figure.tex"])
        evidence = record["root_selection"]["excluded_documents"][0]["evidence"][0]
        self.assertEqual(evidence["parent"], "section.tex")
        self.assertEqual(evidence["via"], ["main.tex", "section.tex"])
        content = " ".join(row["content"] for row in result["elements"])
        previous = -1
        for phrase in ["Main before.", "Section before", "Figure content.", "Section after.", "Main after."]:
            current = content.index(phrase)
            self.assertGreater(current, previous)
            previous = current
        self.assertIn(r"\cite{k}", content)
        self.assertEqual(result["bibtex"][0]["citation_key"], "k")
        self.assertTrue(all(edge["version"] == 1 for edge in result["hierarchy"]))
        validate_tables(result)

    def test_root_fix04_an_independent_document_remains_ambiguous(self):
        record = self.parse({
            "main.tex": r"\documentclass{article}\usepackage{standalone}\begin{document}\input{section}\end{document}",
            "section.tex": r"Section.\input{figure}",
            "figure.tex": r"\documentclass{standalone}\begin{document}Drawing.\end{document}",
            "independent.tex": tex("A different independent paper."),
        })["versions"][0]
        self.assertEqual(record["status"], "parse_error")
        self.assertIn("Expected exactly one documentclass root", record["error"])
        self.assertIsNone(record["root"])

    def test_root_fix04_explicit_article_input_is_not_a_standalone_document(self):
        record = self.parse({
            "main.tex": r"\documentclass{article}\usepackage{standalone}\begin{document}\input{section}\end{document}",
            "section.tex": r"\input{independent}",
            "independent.tex": tex("An article, not a standalone figure."),
        })["versions"][0]
        self.assertEqual(record["status"], "parse_error")
        self.assertIn("found 2", record["error"])

    def test_root_fix04_cycles_discard_partial_graph_evidence(self):
        for index, section in enumerate((
                r"\input{figure}\input{loop}", r"\input{loop}\input{figure}")):
            with self.subTest(section=section):
                record = self.parse({
                    "main.tex": r"\documentclass{article}\usepackage{standalone}\begin{document}\input{section}\end{document}",
                    "section.tex": section,
                    "loop.tex": r"\input{section}",
                    "figure.tex": r"\documentclass{standalone}\begin{document}Drawing.\end{document}",
                }, aid=f"2508.{8050 + index:05d}")["versions"][0]
                self.assertEqual(record["status"], "parse_error")
                self.assertIn("found 2", record["error"])
                self.assertIsNone(record["root_selection"] if "root_selection" in record else None)

    def test_root_fix04_deep_fragment_graph_is_bounded(self):
        files = {
            "main.tex": r"\documentclass{article}\usepackage{standalone}\begin{document}\input{part0}\end{document}",
            "figure.tex": r"\documentclass{standalone}\begin{document}Drawing.\end{document}",
        }
        for index in range(41):
            files[f"part{index}.tex"] = (r"\input{" + (f"part{index+1}" if index < 40 else "figure") + "}")
        record = self.parse(files)["versions"][0]
        self.assertEqual(record["status"], "parse_error")
        self.assertIn("found 2", record["error"])

    def test_root_fix04_unsafe_or_conditional_fragments_do_not_prove_inclusion(self):
        fragments = [
            r"\input{../../figure}",
            r"\input{\dynamic}",
            r"\ifcustom\input{figure}\fi",
            r"\iftrue\input{figure}\else Other.\fi",
            r"\verb|\input{figure}|",
            r"\newcommand{\load}{\input{figure}}\load",
            r"\end{document}\input{figure}",
        ]
        for index, fragment in enumerate(fragments):
            with self.subTest(fragment=fragment):
                record = self.parse({
                    "main.tex": r"\documentclass{article}\usepackage{standalone}\begin{document}\input{section}\end{document}",
                    "section.tex": fragment,
                    "figure.tex": r"\documentclass{standalone}\begin{document}Drawing.\end{document}",
                }, aid=f"2508.{8060 + index:05d}")["versions"][0]
                self.assertEqual(record["status"], "parse_error")
                self.assertIn("found 2", record["error"])

    def test_root_fix04_transitive_subfile_uses_a_verified_ancestor(self):
        result = self.parse({
            "main.tex": r"\documentclass{article}\usepackage{subfiles}\begin{document}Main.\input{section}After.\end{document}",
            "section.tex": r"Section.\subfile{chapter}",
            "chapter.tex": r"\documentclass[main.tex]{subfiles}\begin{document}\section{Extra}Child content.\end{document}",
        })
        record = result["versions"][0]
        self.assertEqual(record["status"], "success", record)
        self.assertEqual(record["root"], "main.tex")
        self.assertEqual(record["included_files"], ["main.tex", "section.tex", "chapter.tex"])
        self.assertIn("Child content.", " ".join(row["content"] for row in result["elements"]))

    def test_root_fix05_uncalled_balanced_common_declarations_are_not_roots(self):
        forms = [
            r"\newcommand{\mytemplate}{\documentclass{article}}",
            r"\newcommand*{\mytemplate}[1]{\documentclass{article}\title{{#1}}}",
            r"\newcommand\mytemplate[2][{Nested default}]{\documentclass{article}#1#2}",
            r"\renewcommand{\mytemplate}{\documentclass{article}}",
            r"\providecommand{\mytemplate}{\documentclass{article}}",
            r"\DeclareRobustCommand{\mytemplate}{\documentclass{article}}",
            r"\def\mytemplate{\documentclass{article}}",
            r"\gdef\mytemplate#1#2{\documentclass{article}\title{{#1}#2}}",
        ]
        for index, declaration in enumerate(forms):
            with self.subTest(declaration=declaration):
                result = self.parse({"main.tex": tex("Main content."), "other.tex": declaration},
                                    aid=f"2508.{8070 + index:05d}")
                record = result["versions"][0]
                self.assertEqual(record["status"], "success", record)
                self.assertEqual(record["root"], "main.tex")
                self.assertEqual(record["included_files"], ["main.tex"])
                self.assertNotIn("documentclass", " ".join(row["content"] for row in result["elements"]))

    def test_root_fix05_an_ordinary_group_cannot_hide_an_active_root(self):
        record = self.parse({
            "main.tex": tex("Main."),
            "other.tex": "{" + tex("Independent grouped document.") + "}",
        })["versions"][0]
        self.assertEqual(record["status"], "parse_error")
        self.assertIn("found 2", record["error"])

    def test_root_fix05_invoked_dynamic_or_malformed_declarations_stay_ambiguous(self):
        forms = [
            r"\newcommand{\mytemplate}{\documentclass{article}}\mytemplate",
            r"\newcommand{\mytemplate}{\documentclass{article}}\csname mytemplate\endcsname",
            r"\newcommand{\mytemplate}{\documentclass{article}",
            r"\newcommand{\mytemplate}[bad]{\documentclass{article}}",
            r"\expandafter\def\csname mytemplate\endcsname{\documentclass{article}}",
            r"\edef\mytemplate{\documentclass{article}}",
            r"\def\mytemplate#2{\documentclass{article}}",
            r"\newcommand{\documentclass}{\documentclass{article}}",
        ]
        for index, declaration in enumerate(forms):
            with self.subTest(declaration=declaration):
                record = self.parse({"main.tex": tex("Main."), "other.tex": declaration},
                                    aid=f"2508.{8080 + index:05d}")["versions"][0]
                self.assertEqual(record["status"], "parse_error")
                self.assertIn("found 2", record["error"])

    def test_root_fix05_lexical_mask_does_not_modify_source_or_expansion(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = (r"\documentclass{article}\newcommand{\mytemplate}{\documentclass{unused}}"
                      r"\begin{document}Body retained.\end{document}")
            (root / "main.tex").write_text(source, encoding="utf-8")
            (root / "other.tex").write_text(
                r"\newcommand{\mytemplate}{\documentclass{unused}}", encoding="utf-8")
            before = {p.name: p.read_bytes() for p in root.iterdir()}
            selected, body = Gatherer(root).gather()
            self.assertEqual(selected, "main.tex")
            self.assertEqual(body, "Body retained.")
            self.assertEqual(before, {p.name: p.read_bytes() for p in root.iterdir()})

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

    def test_wraptable_keeps_table_caption_and_citation(self):
        body = (r"Before.\begin{wraptable}[10]{r}{0.5\linewidth}\centering"
                r"\begin{tabular}{lc}Method & Score\\Ours & 1.0\\\end{tabular}"
                r"\caption{Exact caption with \cite{k}.}\label{tab:x}"
                r"\end{wraptable}After.\bibliography{refs}")
        source = tex(body).replace(r"\begin{document}", r"\usepackage{wrapfig}\begin{document}")
        result = self.parse({"main.tex": source,
                             "refs.bib": "@article{k,title={Evidence},author={Alice}}"})
        self.assertEqual(result["versions"][0]["status"], "success", result["versions"])
        figures = [n for n in result["elements"] if n["kind"] == "figure"]
        self.assertEqual(len(figures), 1)
        self.assertIn(r"\begin{tabular}{lc}", figures[0]["content"])
        self.assertIn(r"Ours & 1.0", figures[0]["content"])
        self.assertIn(r"\caption{Exact caption with \cite{k}.}", figures[0]["content"])
        self.assertNotIn(r"\label", figures[0]["content"])
        self.assertEqual([b["citation_key"] for b in result["bibtex"]], ["k"])
        self.assertEqual([n["content"] for n in result["elements"] if n["kind"] == "sentence"],
                         ["Before.", "After."])
        validate_tables(result)

    def test_wraptable_is_rejected_without_wrapfig(self):
        result = self.parse({"main.tex": tex(
            r"\begin{wraptable}{r}{0.5\linewidth}Visible text.\end{wraptable}")})
        record = result["versions"][0]
        self.assertEqual(record["status"], "parse_error")
        self.assertIn("Unsupported environment: wraptable", record["error"] or "")
        self.assertEqual(result["elements"], [])

    def test_layout_declarations_baselineskip_and_pagenumbering(self):
        source = tex(r"\pagenumbering{roman}\baselineskip=12pt"
                    r"\setlength{\baselineskip}{14pt}Visible text.")
        result = self.parse({"main.tex": source})
        record = result["versions"][0]
        self.assertEqual(record["status"], "success", record)
        self.assertIn("Visible text.", " ".join(n["content"] for n in result["elements"]))
        commands = [m["command"] for m in record["layout_metadata"]]
        self.assertIn("pagenumbering", commands)
        self.assertIn("baselineskip", commands)
        self.assertIn("setlength", commands)

    def test_at_spacing_has_no_visible_content(self):
        result = self.parse({"main.tex": tex(r"Value 5\@. Next sentence.")})
        self.assertEqual(result["versions"][0]["status"], "success", result["versions"])
        content = " ".join(n["content"] for n in result["elements"])
        self.assertIn("Value 5.", content)
        self.assertNotIn(r"\@", content)

    def test_textbackslash_is_retained_as_source_token(self):
        result = self.parse({"main.tex": tex(
            r"Path C:\textbackslash{}temp stays literal.")})
        self.assertEqual(result["versions"][0]["status"], "success", result["versions"])
        content = " ".join(n["content"] for n in result["elements"])
        self.assertIn(r"\textbackslash", content)
        self.assertIn("Path", content)

    def test_boldmath_declaration_keeps_math_content(self):
        result = self.parse({"main.tex": tex(
            r"Before \boldmath$x+y$\unboldmath after.")})
        self.assertEqual(result["versions"][0]["status"], "success", result["versions"])
        content = " ".join(n["content"] for n in result["elements"])
        self.assertIn("$x+y$", content)
        self.assertNotIn(r"\boldmath", content)
        self.assertNotIn(r"\unboldmath", content)

    def test_column_grid_switches_are_layout_metadata(self):
        body = r"Before.\twocolumngrid Between.\onecolumngrid After."
        source = tex(body).replace(r"\begin{document}",
                                   r"\usepackage{ltxgrid}\begin{document}")
        result = self.parse({"main.tex": source})
        record = result["versions"][0]
        self.assertEqual(record["status"], "success", record)
        content = " ".join(n["content"] for n in result["elements"])
        for value in ("Before.", "Between.", "After."):
            self.assertIn(value, content)
        commands = [m["command"] for m in record["layout_metadata"]]
        self.assertIn("twocolumngrid", commands)
        self.assertIn("onecolumngrid", commands)
        rejected = self.parse({"main.tex": tex(r"Before.\twocolumngrid After.")},
                              aid="2508.07874")
        self.assertEqual(rejected["versions"][0]["status"], "parse_error")
        self.assertIn("Undeclared grid command: twocolumngrid",
                      rejected["versions"][0]["error"] or "")

    def test_common_text_abbreviations_expand_but_local_macros_win(self):
        # Preserve package-aware interpretation from Batch 02; this is not a
        # fallback meaning for arbitrary undefined control sequences.
        source = tex(r"Use \eg, this and \ie that.").replace(
            r"\begin{document}", r"\usepackage{iccv}\begin{document}")
        result = self.parse({"main.tex": source})
        self.assertEqual(result["versions"][0]["status"], "success", result["versions"])
        content = " ".join(n["content"] for n in result["elements"])
        self.assertIn("e.g.", content)
        self.assertIn("i.e.", content)
        local = (r"\documentclass{article}\newcommand{\eg}{custom}"
                 r"\begin{document}Use \eg here.\end{document}")
        overridden = self.parse({"main.tex": local}, aid="2508.07873")
        self.assertEqual(overridden["versions"][0]["status"], "success",
                         overridden["versions"])
        local_content = " ".join(n["content"] for n in overridden["elements"])
        self.assertIn("custom", local_content)
        self.assertNotIn("e.g.", local_content)
        for command_name in ("eg", "ie"):
            with self.subTest(command=command_name):
                rejected = self._batch01_parse("\\" + command_name + " text.")
                self.assertEqual(rejected["versions"][0]["status"], "parse_error")
                self.assertEqual(rejected["versions"][0]["error"],
                                 "Unsupported prose command: \\" + command_name)

    def test_aastex_received_and_date_declarations_are_metadata(self):
        source = (r"\documentclass{aastex631}\begin{document}"
                  r"\received{1 January 2026}\revised{2 February 2026}"
                  r"\accepted{3 March 2026}\published{4 April 2026}"
                  r"Body text.\end{document}")
        result = self.parse({"main.tex": source})
        record = result["versions"][0]
        self.assertEqual(record["status"], "success", record)
        commands = [m["command"] for m in record["layout_metadata"]]
        for name in ("received", "revised", "accepted", "published"):
            self.assertIn(name, commands)
        self.assertIn("Body text.",
                      " ".join(n["content"] for n in result["elements"]))
        rejected = self.parse({"main.tex": tex(r"\received{1 January 2026}Body.")},
                              aid="2508.07875")
        self.assertEqual(rejected["versions"][0]["status"], "parse_error")

    def test_ieee_title_abstract_index_text_parses_inner_content(self):
        source = (r"\documentclass{IEEEtran}\begin{document}"
                  r"\IEEEtitleabstractindextext{"
                  r"\begin{abstract}Summary text.\end{abstract}"
                  r"\begin{IEEEkeywords}Signal, \emph{learning}.\end{IEEEkeywords}}"
                  r"\section{Intro}Body.\end{document}")
        result = self.parse({"main.tex": source})
        self.assertEqual(result["versions"][0]["status"], "success", result["versions"])
        contents = [n["content"] for n in result["elements"]]
        self.assertIn("Summary text.", contents)
        self.assertIn("keywords", contents)
        self.assertIn("Body.", contents)

    def test_mhchem_ce_is_retained_as_source_token(self):
        source = tex(r"Water \ce{H2O} and \ce{CO2} stay.").replace(
            r"\begin{document}", r"\usepackage{mhchem}\begin{document}")
        result = self.parse({"main.tex": source})
        self.assertEqual(result["versions"][0]["status"], "success", result["versions"])
        content = " ".join(n["content"] for n in result["elements"])
        self.assertIn(r"\ce{H2O}", content)
        self.assertIn(r"\ce{CO2}", content)
        self.assertIn("Water", content)
        self.assertIn("stay.", content)
        rejected = self.parse({"main.tex": tex(r"Water \ce{H2O} stays.")},
                              aid="2508.07876")
        self.assertEqual(rejected["versions"][0]["status"], "parse_error")

    def test_acmart_teaserfigure_is_a_figure_leaf(self):
        body = (r"Before.\begin{teaserfigure}"
                r"\includegraphics[width=\textwidth]{teaser.pdf}"
                r"\caption{Teaser caption.}\Description{Teaser description.}"
                r"\end{teaserfigure}After.")
        source = tex(body).replace("{article}", "{acmart}")
        result = self.parse({"main.tex": source})
        self.assertEqual(result["versions"][0]["status"], "success", result["versions"])
        figures = [n["content"] for n in result["elements"] if n["kind"] == "figure"]
        self.assertEqual(len(figures), 1)
        self.assertIn(r"\includegraphics[width=\textwidth]{teaser.pdf}", figures[0])
        self.assertIn(r"\caption{Teaser caption.}", figures[0])
        self.assertIn(r"\Description{Teaser description.}", figures[0])
        self.assertIn("Before.", " ".join(n["content"] for n in result["elements"]))
        self.assertIn("After.", " ".join(n["content"] for n in result["elements"]))
        rejected = self.parse({"main.tex": tex(
            r"\begin{teaserfigure}Text.\end{teaserfigure}")}, aid="2508.07877")
        self.assertEqual(rejected["versions"][0]["status"], "parse_error")
        self.assertIn("Unsupported environment: teaserfigure",
                      rejected["versions"][0]["error"] or "")

    def test_llncs_credits_environment_keeps_acknowledgments_and_disclosure(self):
        source = (r"\documentclass{llncs}\begin{document}"
                  r"\section{Conclusion}Body."
                  r"\begin{credits}"
                  r"\subsubsection{\ackname}Funded by a grant. "
                  r"\subsubsection{\discintname}No competing interests."
                  r"\end{credits}\end{document}")
        result = self.parse({"main.tex": source})
        self.assertEqual(result["versions"][0]["status"], "success", result["versions"])
        contents = [n["content"] for n in result["elements"]]
        self.assertIn("Acknowledgments", contents)
        self.assertIn("Disclosure of Interests", contents)
        content = " ".join(contents)
        self.assertIn("Funded by a grant.", content)
        self.assertIn("No competing interests.", content)
        self.assertTrue(any(m.get("environment") == "credits"
                            for m in result["versions"][0]["layout_metadata"]))
        rejected = self.parse({"main.tex": tex(r"\begin{credits}Text.\end{credits}")},
                              aid="2508.07878")
        self.assertEqual(rejected["versions"][0]["status"], "parse_error")
        self.assertIn("Unsupported environment: credits",
                      rejected["versions"][0]["error"] or "")

    @pytest.mark.xfail(strict=True, raises=AssertionError,
        reason="Source acronym forms are not integrated: active-definition validation and first-use/reset/scope semantics are unimplemented.")
    def test_acro_declarations_expand_forms(self):
        source = (r"\documentclass{article}\usepackage{acro}"
                  r"\DeclareAcronym{ny}{short=NY, long=New York, short-plural=s, long-plural=s}"
                  r"\begin{document}"
                  r"\ac{ny} and \acs{ny} and \acl{ny}. "
                  r"\acp{ny}.\end{document}")
        result = self.parse({"main.tex": source})
        self.assertEqual(result["versions"][0]["status"], "success", result["versions"])
        content = " ".join(n["content"] for n in result["elements"])
        self.assertIn("New York (NY)", content)
        self.assertIn("New Yorks (NYs)", content)
        self.assertIn("NY", content)
        self.assertIn("New York", content)


    @pytest.mark.xfail(strict=True, raises=AssertionError,
        reason="Glossary expansion is not integrated: first-use/reset/scope semantics and active declaration validation are unimplemented.")
    def test_glossaries_acronyms_expand_forms(self):
        source = (r"\documentclass{article}\usepackage{glossaries}"
                  r"\newacronym[longplural={machine learning models}, shortplural={MLs}]"
                  r"{mlm}{ML}{machine learning model}"
                  r"\begin{document}\gls{mlm} and \acrshort{mlm} and \acrlong{mlm}. "
                  r"\glspl{mlm}.\end{document}")
        result = self.parse({"main.tex": source})
        self.assertEqual(result["versions"][0]["status"], "success", result["versions"])
        content = " ".join(n["content"] for n in result["elements"])
        self.assertIn("machine learning model (ML)", content)
        self.assertIn("machine learning models (MLs)", content)
        self.assertIn("ML", content)
        self.assertIn("machine learning model", content)


    @pytest.mark.xfail(strict=True, raises=AssertionError,
        reason="Unresolved acronym token retention as success is not accepted by M2 integration; unresolved semantic content remains unsupported.")
    def test_undefined_acronym_is_retained_as_source_token(self):
        source = tex(r"Use \ac{missing}.").replace(
            r"\begin{document}", r"\usepackage{acro}\begin{document}")
        result = self.parse({"main.tex": source})
        self.assertEqual(result["versions"][0]["status"], "success", result["versions"])
        content = " ".join(n["content"] for n in result["elements"])
        self.assertIn(r"\ac{missing}", content)
        self.assertTrue(any("Unresolved acronym" in warning
                            for warning in result["versions"][0]["normalization_notes"]))


    def test_acronym_without_package_or_definition_is_rejected(self):
        result = self.parse({"main.tex": tex(r"Use \ac{missing}.")})
        record = result["versions"][0]
        self.assertEqual(record["status"], "parse_error")
        self.assertIn("Unsupported prose command: \\ac", record["error"] or "")

    def test_local_acronym_command_definition_wins(self):
        source = (r"\documentclass{article}\usepackage{acro}"
                  r"\DeclareAcronym{ny}{short=NY, long=New York}"
                  r"\newcommand{\ac}[1]{custom}"
                  r"\begin{document}\ac{ny}\end{document}")
        result = self.parse({"main.tex": source})
        self.assertEqual(result["versions"][0]["status"], "success", result["versions"])
        content = " ".join(n["content"] for n in result["elements"])
        self.assertIn("custom", content)
        self.assertNotIn("New York", content)



    def test_merge_inactive_or_uncalled_acronym_definitions_are_not_expansion_evidence(self):
        for package, declaration, invocation in (
            ("acro", r"\DeclareAcronym{ny}{short=NY,long=New York}", r"\ac{ny}"),
            ("glossaries", r"\newacronym{ny}{NY}{New York}", r"\gls{ny}"),
        ):
            for declaration_context in (
                r"\iffalse" + declaration + r"\fi",
                r"\newcommand{\unused}{" + declaration + "}",
            ):
                with self.subTest(package=package, declaration_context=declaration_context):
                    result = self._batch01_parse(
                        "Before. " + invocation + " After.",
                        preamble=r"\usepackage{" + package + "}" + declaration_context)
                    record = result["versions"][0]
                    self.assertEqual(record["status"], "parse_error", record)
                    self.assertEqual(record["error"],
                                     "Unsupported prose command: " + invocation.split("{", 1)[0])
                    self.assertEqual(result["elements"], [])
                    self.assertEqual(result["hierarchy"], [])

    def test_merge_unresolved_acronym_version_does_not_corrupt_valid_sibling_version(self):
        bad = (r"\documentclass{article}\usepackage{acro}"
               r"\DeclareAcronym{ny}{short=NY,long=New York}"
               r"\begin{document}Before \ac{ny}. Then \ac{ny}."
               r"\acreset{ny}\ac{ny}.\end{document}")
        good = tex(r"\section{Intro}Verified sibling \cite{k}.\bibliography{refs}")
        p = self.paper({1: {"main.tex": bad},
                        2: {"main.tex": good, "refs.bib": "@article{k,title={Evidence}}"}})
        result = parse_versions(AID, [(1, p / "v1"), (2, p / "v2")])
        self.assertEqual([v["status"] for v in result["versions"]], ["parse_error", "success"])
        self.assertEqual(result["versions"][0]["error"], r"Unsupported prose command: \ac")
        self.assertEqual({e["version"] for e in result["hierarchy"]}, {2})
        content = " ".join(n["content"] for n in result["elements"])
        self.assertIn(r"Verified sibling \cite{k}.", content)
        self.assertNotIn("New York", content)
        self.assertEqual([b["citation_key"] for b in result["bibtex"]], ["k"])
        validate_tables(result)

    def test_merge_credits_preserves_literals_local_macros_and_versioned_edges(self):
        source = (r"\documentclass{llncs}\begin{document}\section{Conclusion}Before."
                  r"\begin{credits}\subsubsection{\ackname}Supported by \cite{k}. "
                  r"\verb|\ackname|"
                  r"\begin{verbatim}\discintname\end{verbatim}"
                  r"\subsubsection{\discintname}No conflict.\end{credits}After."
                  r"\bibliography{r}\end{document}")
        p = self.paper({1: {"main.tex": source, "r.bib": "@article{k,title={Evidence}}"},
                        2: {"main.tex": source, "r.bib": "@article{k,title={Evidence}}"}})
        result = parse_versions(AID, [(1, p / "v1"), (2, p / "v2")])
        self.assertEqual([v["status"] for v in result["versions"]], ["success", "success"], result["versions"])
        contents = [n["content"] for n in result["elements"]]
        self.assertIn("Acknowledgments", contents)
        self.assertIn("Disclosure of Interests", contents)
        self.assertTrue(any(r"\verb|\ackname|" in c for c in contents))
        self.assertTrue(any(r"\discintname" in c for c in contents))
        self.assertTrue(any(r"Supported by \cite{k}." == c for c in contents))
        self.assertIn("After.", contents)
        self.assertEqual([b["citation_key"] for b in result["bibtex"]], ["k"])
        shared = next(n["element_id"] for n in result["elements"] if n["content"] == "Acknowledgments")
        self.assertEqual({e["version"] for e in result["hierarchy"] if e["element_id"] == shared}, {1, 2})
        validate_tables(result)

    def test_merge_credits_local_labels_and_environment_are_not_replaced(self):
        result = self._batch01_parse(
            r"\begin{credits}\ackname Text.\discintname End.\end{credits}",
            document_class="llncs",
            preamble=r"\newcommand{\ackname}{Local thanks}\newcommand{\discintname}{Local disclosure}")
        self.assertEqual(result["versions"][0]["status"], "success", result["versions"])
        content = " ".join(n["content"] for n in result["elements"])
        self.assertIn("Local thanks", content)
        self.assertIn("Local disclosure", content)
        self.assertNotIn("Acknowledgments", content)
        for preamble, body in (
            ("", r"\begin{credits}[unknown]Text.\end{credits}"),
            ("", r"\ackname Outside."),
            (r"\renewenvironment{credits}{\unknown}{Done.}", r"\begin{credits}Text.\end{credits}"),
        ):
            with self.subTest(preamble=preamble, body=body):
                result = self._batch01_parse(body, document_class="llncs", preamble=preamble)
                self.assertEqual(result["versions"][0]["status"], "parse_error")

    def test_merge_grid_package_extension_keeps_local_override_and_math(self):
        for cls, preamble in (
            ("aastex631", ""),
            ("revtex4-2", ""),
            ("article", r"\usepackage{ltxgrid}"),
        ):
            with self.subTest(cls=cls):
                result = self._batch01_parse(
                    r"\onecolumngrid Before $x$.\twocolumngrid After.",
                    document_class=cls, preamble=preamble)
                self.assertEqual(result["versions"][0]["status"], "success", result["versions"])
                self.assertIn("Before $x$.", [n["content"] for n in result["elements"]])
                self.assertIn("After.", [n["content"] for n in result["elements"]])
        result = self._batch01_parse(
            r"\onecolumngrid After.",
            preamble=r"\usepackage{ltxgrid}\newcommand{\onecolumngrid}{Local grid text.}")
        self.assertEqual(result["versions"][0]["status"], "success", result["versions"])
        self.assertIn("Local grid text.", [n["content"] for n in result["elements"]])

    def test_merge_declared_length_and_baseline_handlers_coexist(self):
        result = self._batch01_parse(
            r"\newlength{\mywidth}\setlength{\mywidth}{0.5\linewidth}"
            r"\baselineskip=12pt plus 1pt\setlength{\baselineskip}{14pt}"
            r"\pagenumbering{gobble}Before $x$.\textbackslash{}cite\{literal\}.After.")
        self.assertEqual(result["versions"][0]["status"], "success", result["versions"])
        self.assertIn(r"\textbackslash{}cite\{literal\}", " ".join(n["content"] for n in result["elements"]))
        self.assertEqual(result["bibtex"], [])
        commands = [m["command"] for m in result["versions"][0]["layout_metadata"]]
        for name in ("newlength", "setlength", "baselineskip", "pagenumbering"):
            self.assertIn(name, commands)
        validate_tables(result)

    def test_merge_new_symbol_chemical_and_ieee_handlers_keep_local_macros(self):
        for name in ("textdegree", "ce", "boldmath", "unboldmath", "IEEEtitleabstractindextext"):
            with self.subTest(name=name):
                result = self._batch01_parse(
                    "\\" + name + " After.",
                    document_class="IEEEtran",
                    preamble=r"\usepackage{mhchem}\newcommand{" + "\\" + name + r"}{Local content.}")
                self.assertEqual(result["versions"][0]["status"], "success", result["versions"])
                self.assertIn("Local content.", [n["content"] for n in result["elements"]])

    def test_merge_ieee_wrapper_keeps_citations_equations_and_following_section(self):
        result = self._batch01_parse(
            r"\IEEEtitleabstractindextext{\begin{abstract}Summary \cite{k}.\end{abstract}"
            r"\begin{IEEEkeywords}Signals.\end{IEEEkeywords}}"
            r"\section{Intro}Body.\[x=1\]\bibliography{r}",
            document_class="IEEEtran",
            files={"r.bib": "@article{k,title={Evidence}}"})
        self.assertEqual(result["versions"][0]["status"], "success", result["versions"])
        content = [n["content"] for n in result["elements"]]
        self.assertIn(r"Summary \cite{k}.", content)
        self.assertIn("Intro", content)
        self.assertIn("Body.", content)
        self.assertTrue(any(n["kind"] == "block_formula" and "x=1" in n["content"] for n in result["elements"]))
        self.assertEqual([b["citation_key"] for b in result["bibtex"]], ["k"])
        validate_tables(result)

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

    def test_125_si_short_unit_alias_keeps_source_quantity(self):
        source=tex(r"Sampled at \SI{500}{\Hz}, \SI{1.0}{\ms}, \SI{2}{\kHz} and \SI{3}{\uF}.").replace(r"\begin{document}",r"\usepackage{siunitx}\begin{document}")
        r=self.parse({"main.tex":source});self.assertEqual(r["versions"][0]["status"],"success");c=" ".join(n["content"] for n in r["elements"])
        for quantity in (r"\SI{500}{\Hz}",r"\SI{1.0}{\ms}",r"\SI{2}{\kHz}",r"\SI{3}{\uF}"):self.assertIn(quantity,c)

    def test_125_textcomp_degree_symbol_is_normalized(self):
        r=self.parse({"main.tex":tex(r"Angles from -70\textdegree{} to +70\textdegree.")})
        self.assertEqual(r["versions"][0]["status"],"success")
        c=" ".join(n["content"] for n in r["elements"])
        self.assertIn("-70\u00b0",c);self.assertIn("+70\u00b0",c)

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


    def _batch01_parse(self, body, *, document_class="article", preamble="", files=None):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sources = {"main.tex": r"\documentclass{" + document_class + "}" + preamble
                       + r"\begin{document}" + body + r"\end{document}", **(files or {})}
            for name, content in sources.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            result = parse_versions(AID, [(1, root)])
            validate_tables(result)
            return result

    def test_batch01_baselineskip_literal_glue_preserves_following_content(self):
        for assignment in ("18pt", "=15.5pt", " = 12pt plus 2pt minus 1pt"):
            with self.subTest(assignment=assignment):
                result = self._batch01_parse(r"\baselineskip" + assignment
                    + r" \section{Intro}Words \cite{k}.\[x=y\]After.",
                    files={"r.bib": "@article{k,title={Evidence},year={2020}}"},
                    preamble=r"\bibliography{r}")
                self.assertEqual(result["versions"][0]["status"], "success", result["versions"])
                content = " ".join(x["content"] for x in result["elements"])
                self.assertIn(r"Words \cite{k}.", content)
                self.assertIn(r"x=y", content)
                self.assertIn("After.", content)
                self.assertNotIn("18pt", content)
                self.assertEqual(result["bibtex"][0]["citation_key"], "k")

    def test_batch01_baselineskip_dynamic_or_incomplete_glue_is_not_dropped(self):
        for value in (r"=\unknown Text.", r"=2\unknown Text.", "=12pt plus Bad Text.",
                      "=12pt minus Bad Text.", "=12pt plus 2pt plus 3pt Text.", "=12bogus Text.",
                      r"\section{Lost}"):
            with self.subTest(value=value):
                r = self._batch01_parse(r"\baselineskip" + value)
                self.assertEqual(r["versions"][0]["status"], "parse_error")
                self.assertEqual(r["elements"], [])

    def test_batch01_baselineskip_local_macro_takes_precedence(self):
        r = self._batch01_parse(r"\baselineskip Words.", preamble=r"\newcommand{\baselineskip}{Local}")
        self.assertEqual(r["versions"][0]["status"], "success")
        self.assertIn("Local Words.", " ".join(x["content"] for x in r["elements"]))

    def test_batch01_pagenumbering_supported_styles_do_not_consume_body(self):
        for style in ("arabic", "roman", "Roman", "alph", "Alph", "gobble"):
            with self.subTest(style=style):
                r = self._batch01_parse(r"Before.\pagenumbering{" + style + r"}\section{Next}After.")
                self.assertEqual(r["versions"][0]["status"], "success")
                self.assertEqual([x["content"] for x in r["elements"]][1:], ["Before.", "Next", "After."])
                self.assertEqual(r["versions"][0]["layout_metadata"][0]["style"], style)

    def test_batch01_pagenumbering_invalid_or_missing_argument_is_not_swallowed(self):
        for body in (r"\pagenumbering{\style}After.", r"\pagenumbering{unknown}After.",
                     r"\pagenumbering After.", r"\pagenumbering{arabic"):
            with self.subTest(body=body):
                self.assertEqual(self._batch01_parse(body)["versions"][0]["status"], "parse_error")

    def test_batch01_pagenumbering_local_macro_is_not_replaced(self):
        r = self._batch01_parse(r"\pagenumbering{Actual} After.",
                               preamble=r"\newcommand{\pagenumbering}[1]{#1 wording.}")
        self.assertEqual(r["versions"][0]["status"], "success")
        self.assertIn("Actual wording.", " ".join(x["content"] for x in r["elements"]))

    def test_batch01_sentence_spacing_at_sign_keeps_text_math_and_citation(self):
        r = self._batch01_parse(r"See M.Sc.\@ thesis \cite{k}. Next $a=b$.",
            preamble=r"\bibliography{r}", files={"r.bib": "@article{k,title={Thesis}}"})
        self.assertEqual(r["versions"][0]["status"], "success")
        content = " ".join(x["content"] for x in r["elements"])
        self.assertIn(r"M.Sc. thesis \cite{k}.", content)
        self.assertIn("$a=b$", content)

    def test_batch01_at_sign_does_not_hide_internal_commands_or_local_definitions(self):
        bad = self._batch01_parse(r"\@unknown{Do not lose this.}")
        self.assertEqual(bad["versions"][0]["status"], "parse_error")
        r = self._batch01_parse(r"\@ Tail.", preamble=r"\newcommand{\@}{Visible}")
        self.assertEqual(r["versions"][0]["status"], "success")
        self.assertIn("Visible Tail.", " ".join(x["content"] for x in r["elements"]))

    def test_batch01_textbackslash_is_literal_not_an_invented_citation(self):
        r = self._batch01_parse(r"Grant IES\textbackslash R3\textbackslash243264. "
                               r"Literal \textbackslash{}cite\{missing\}. After.")
        self.assertEqual(r["versions"][0]["status"], "success", r["versions"])
        content = " ".join(x["content"] for x in r["elements"])
        self.assertIn(r"IES\textbackslash{}R3\textbackslash{}243264", content)
        self.assertIn(r"\textbackslash{}cite\{missing\}", content)
        self.assertEqual(r["bibtex"], [])

    def test_batch01_textbackslash_does_not_hide_real_unknown_command_or_missing_citation(self):
        for body in (r"\textbackslash{}\unknown{Content}", r"\textbackslash{} \cite{missing}"):
            with self.subTest(body=body):
                self.assertEqual(self._batch01_parse(body)["versions"][0]["status"], "parse_error")
        r = self._batch01_parse(r"\textbackslash After.",
                               preamble=r"\newcommand{\textbackslash}{Slash word}")
        self.assertEqual(r["versions"][0]["status"], "success")
        self.assertIn("Slash word After.", " ".join(x["content"] for x in r["elements"]))

    def test_batch01_twocolumngrid_preserves_prose_formula_and_order(self):
        r = self._batch01_parse(r"Before.\onecolumngrid\[a=b\]\twocolumngrid After.",
                               document_class="revtex4-2")
        self.assertEqual(r["versions"][0]["status"], "success")
        self.assertEqual([x["kind"] for x in r["elements"]], ["document", "sentence", "block_formula", "sentence"])
        self.assertEqual([x["content"] for x in r["elements"] if x["kind"]=="sentence"], ["Before.", "After."])

    def test_batch01_twocolumngrid_requires_class_or_explicit_local_definition(self):
        self.assertEqual(self._batch01_parse(r"\twocolumngrid After.")["versions"][0]["status"], "parse_error")
        r = self._batch01_parse(r"\twocolumngrid After.",
                               preamble=r"\newcommand{\twocolumngrid}{Local grid text.}")
        self.assertEqual(r["versions"][0]["status"], "success")
        self.assertIn("Local grid text.", " ".join(x["content"] for x in r["elements"]))

    def test_batch01_eg_cvpr_iccv_onedot_keeps_citations_and_spacing(self):
        for package in ("cvpr", "iccv"):
            with self.subTest(package=package):
                r = self._batch01_parse(r"Examples, \eg. cases \cite{k}, \eg, other cases.",
                    preamble=r"\usepackage{" + package + r"}\bibliography{r}",
                    files={"r.bib": "@article{k,title={Evidence}}"})
                self.assertEqual(r["versions"][0]["status"], "success", r["versions"])
                content = " ".join(x["content"] for x in r["elements"])
                self.assertIn(r"e.g. cases \cite{k}", content)
                self.assertIn("e.g., other cases.", content)
                self.assertNotIn("e.g..", content)
                self.assertIn("original style file not interpreted",
                              " ".join(r["versions"][0]["normalization_notes"]))

    def test_batch01_eg_without_package_is_not_guessed_and_local_definition_wins(self):
        self.assertEqual(self._batch01_parse(r"\eg cases.")["versions"][0]["status"], "parse_error")
        r = self._batch01_parse(r"\eg cases.",
            preamble=r"\usepackage{iccv}\newcommand{\eg}{Custom phrase}")
        self.assertEqual(r["versions"][0]["status"], "success")
        content = " ".join(x["content"] for x in r["elements"])
        self.assertIn("Custom phrase cases.", content)
        self.assertNotIn("e.g.", content)

    def test_batch01_received_retains_exact_dates_and_optional_stage(self):
        r = self._batch01_parse(r"\received{5 August {2025}}\received[revised]{9 August 2025}"
                               r"\received[accepted]{12 August 2025}\section{Intro}After.",
                               document_class="acmart")
        self.assertEqual(r["versions"][0]["status"], "success", r["versions"])
        content = " ".join(x["content"] for x in r["elements"])
        self.assertIn("5 August 2025", content)
        self.assertIn("9 August 2025", content)
        self.assertIn("12 August 2025", content)
        self.assertIn("After.", content)
        self.assertEqual([x["stage"] for x in r["versions"][0]["layout_metadata"]], [None,"revised","accepted"])

    def test_batch01_received_aastex_keeps_text_after_metadata(self):
        r = self._batch01_parse(r"\received{Unspecified date}\begin{abstract}Abstract text.\end{abstract}After.",
                               document_class="aastex631")
        self.assertEqual(r["versions"][0]["status"], "success")
        content = " ".join(x["content"] for x in r["elements"])
        self.assertIn("Unspecified date", content)
        self.assertIn("Abstract text.", content)
        self.assertIn("After.", content)

    def test_batch01_received_unknown_stage_class_and_missing_or_dynamic_content_fail(self):
        for cls, body in (("acmart",r"\received[unknown]{Date}"),
                          ("article",r"\received{Date}"),
                          ("acmart",r"\received Date."),
                          ("acmart",r"\received{\unknown{Date}}"),
                          ("aastex631",r"\received[accepted]{Date}")):
            with self.subTest(cls=cls,body=body):
                self.assertEqual(self._batch01_parse(body,document_class=cls)["versions"][0]["status"], "parse_error")

    def test_batch01_received_local_definition_takes_precedence(self):
        r = self._batch01_parse(r"\received{Actual wording} After.", document_class="acmart",
                               preamble=r"\newcommand{\received}[1]{#1.}")
        self.assertEqual(r["versions"][0]["status"], "success")
        self.assertIn("Actual wording.", " ".join(x["content"] for x in r["elements"]))

    def test_batch01_subtitle_nested_text_math_and_citations_are_preserved(self):
        r = self._batch01_parse(r"\subtitle{The {quiet} Sun \& $Ca$ \cite{k}}"
                               r"\begin{abstract}Abstract follows.\end{abstract}\section{Intro}Body.",
            document_class="aa", preamble=r"\bibliography{r}",
            files={"r.bib":"@article{k,title={Sun study}}"})
        self.assertEqual(r["versions"][0]["status"], "success", r["versions"])
        content = " ".join(x["content"] for x in r["elements"])
        self.assertIn(r"The quiet Sun \& $Ca$ \cite{k}", content)
        self.assertIn("Abstract follows.", content)
        self.assertIn("Body.", content)
        self.assertEqual(r["bibtex"][0]["citation_key"],"k")

    def test_batch01_empty_subtitle_leaves_following_abstract_and_section(self):
        r = self._batch01_parse(r"\subtitle{}\begin{abstract}Abstract.\end{abstract}\section{Intro}Body.",
                               document_class="aa")
        self.assertEqual(r["versions"][0]["status"], "success")
        self.assertNotIn("subtitle", [x["content"] for x in r["elements"]])
        self.assertIn("Body.", [x["content"] for x in r["elements"]])

    def test_batch01_subtitle_unknown_class_malformed_and_unknown_nested_text_fail(self):
        for cls, body in (("article",r"\subtitle{Text}"),
                          ("aa",r"\subtitle Missing."),
                          ("aa",r"\subtitle{\unknown{Text}}")):
            with self.subTest(cls=cls,body=body):
                self.assertEqual(self._batch01_parse(body,document_class=cls)["versions"][0]["status"], "parse_error")

    def test_batch01_subtitle_local_definition_and_existing_roman_logic(self):
        r = self._batch01_parse(r"\subtitle{\uppercase\expandafter{\romannumeral2}: Inner halo}After.",
                               document_class="aa")
        self.assertEqual(r["versions"][0]["status"], "success", r["versions"])
        self.assertIn("II: Inner halo", " ".join(x["content"] for x in r["elements"]))
        r = self._batch01_parse(r"\subtitle{Custom} After.", document_class="aa",
                               preamble=r"\newcommand{\subtitle}[1]{#1 text.}")
        self.assertEqual(r["versions"][0]["status"],"success")
        self.assertIn("Custom text."," ".join(x["content"] for x in r["elements"]))

    def test_batch01_icml_notice_keeps_marker_and_following_abstract(self):
        r = self._batch01_parse(r"\printAffiliationsAndNotice{\icmlEqualContribution}"
                               r"\begin{abstract}Actual abstract.\end{abstract}\section{Intro}Body.",
                               preamble=r"\usepackage{icml2025}")
        self.assertEqual(r["versions"][0]["status"], "success", r["versions"])
        self.assertIn(r"\icmlEqualContribution{}",[x["content"] for x in r["elements"]])
        self.assertIn("Actual abstract.",[x["content"] for x in r["elements"]])
        self.assertIn("Body.",[x["content"] for x in r["elements"]])
        self.assertIn("original style wording not interpreted"," ".join(r["versions"][0]["normalization_notes"]))

    def test_batch01_icml_marker_outside_notice_undeclared_and_unknown_notice_fail(self):
        for preamble,body in (("",r"\icmlEqualContribution Body."),
                              (r"\usepackage{icml2025}",r"\icmlEqualContribution Body."),
                              (r"\usepackage{icml2025}",r"\printAffiliationsAndNotice{\icmlEqualContribution\unknown{Body}}")):
            with self.subTest(preamble=preamble,body=body):
                self.assertEqual(self._batch01_parse(body,preamble=preamble)["versions"][0]["status"],"parse_error")

    def test_batch01_icml_marker_explicit_local_wording_is_not_overridden(self):
        r = self._batch01_parse(r"\printAffiliationsAndNotice{\icmlEqualContribution}After.",
            preamble=r"\usepackage{icml2025}\newcommand{\icmlEqualContribution}{Alice and Bob contributed equally.}")
        self.assertEqual(r["versions"][0]["status"], "success", r["versions"])
        content=" ".join(x["content"] for x in r["elements"])
        self.assertIn("Alice and Bob contributed equally.",content)
        self.assertNotIn(r"\icmlEqualContribution",content)

    def test_batch01_ac_without_first_use_semantics_remains_explicit_error(self):
        r = self._batch01_parse(r"\ac{TM} After.",
            preamble=r"\usepackage{acronym}\begin{acronym}\acro{TM}{Tsetlin Machine}\end{acronym}")
        self.assertEqual(r["versions"][0]["status"],"parse_error")
        self.assertEqual(r["versions"][0]["error"],r"Unsupported prose command: \ac")

    def test_batch01_new_commands_keep_versioned_hierarchy_and_shared_content(self):
        source = r"\documentclass{revtex4-2}\begin{document}\baselineskip=12pt\pagenumbering{arabic}\section{Intro}Shared.\twocolumngrid\[a=b\]After.\end{document}"
        p=self.paper({1:{"main.tex":source},2:{"main.tex":source}})
        r=parse_versions(AID,[(1,p/"v1"),(2,p/"v2")])
        validate_tables(r)
        self.assertEqual([v["status"] for v in r["versions"]],["success","success"])
        node=next(x for x in r["elements"] if x["content"]=="Shared.")
        self.assertEqual({e["version"] for e in r["hierarchy"] if e["element_id"]==node["element_id"]},{1,2})
        nodes={x["element_id"]:x for x in r["elements"]}
        for version in (1,2):
            edges=[e for e in r["hierarchy"] if e["version"]==version]
            parent={e["element_id"]:e["parent_id"] for e in edges}
            for e in edges:
                cursor=e["element_id"];seen=set()
                while cursor in parent:
                    self.assertNotIn(cursor,seen);seen.add(cursor);cursor=parent[cursor]
                self.assertEqual(nodes[cursor]["kind"],"document")
                self.assertEqual(nodes[cursor]["content"],f"Version {version} root")


    def test_batch02_a_vision_abbreviations_supported_packages_and_punctuation(self):
        for package in ("iccv","cvpr","eccvabbrv","wacv"):
            with self.subTest(package=package):
                r=self._batch01_parse(r"\eg. cases, \ie useful work by Doe \etal~\cite{k}.",
                    preamble=r"\usepackage{"+package+r"}\bibliography{r}",
                    files={"r.bib":"@article{k,title={Evidence}}"})
                self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
                content=" ".join(n["content"] for n in r["elements"])
                self.assertIn("e.g. cases",content);self.assertIn("i.e. useful",content)
                self.assertIn(r"et al. \cite{k}",content);self.assertNotIn("e.g..",content)

    def test_batch02_a_vision_abbreviations_unknown_package_and_override(self):
        for name in ("eg","ie","etal"):
            with self.subTest(name=name):
                self.assertEqual(self._batch01_parse("\\"+name+" words.")["versions"][0]["status"],"parse_error")
                # A longer, unknown control word is not the abbreviation.
                r=self._batch01_parse("\\"+name+"word words.",preamble=r"\usepackage{iccv}")
                self.assertEqual(r["versions"][0]["status"],"parse_error")
                r=self._batch01_parse("\\"+name+" words.",
                    preamble=r"\usepackage{iccv}\newcommand{"+"\\"+name+r"}{Local}")
                self.assertEqual(r["versions"][0]["status"],"success")
                self.assertIn("Local words."," ".join(n["content"] for n in r["elements"]))

    def test_batch02_a_restatable_keeps_heading_body_citation_and_replay(self):
        r=self._batch01_parse(r"\section{Main}\begin{restatable}[Named \cite{k}]{theorem}{ResultA}"
            r"Statement $x=1$.\end{restatable}\section{Proofs}\ResultA*After.",
            preamble=r"\usepackage{thm-restate}\newtheorem{theorem}{Theorem}\bibliography{r}",
            files={"r.bib":"@article{k,title={Theorem source}}"})
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        content=[n["content"] for n in r["elements"]]
        self.assertEqual(content.count("Statement $x=1$."),2)
        self.assertEqual(sum(r"Named \cite{k}"==c for c in content),2)
        self.assertIn("After.",content)
        self.assertEqual(r["bibtex"][0]["citation_key"],"k")
        self.assertEqual(sum(e["version"]==1 for e in r["hierarchy"]),len(r["elements"])-1)

    def test_batch02_a_restatable_unverified_unknown_type_or_collision_fails(self):
        for pre,body in [
            (r"\newtheorem{theorem}{Theorem}",r"\begin{restatable}{theorem}{R}Words.\end{restatable}"),
            (r"\usepackage{thm-restate}",r"\begin{restatable}{unknown}{R}Words.\end{restatable}"),
            (r"\usepackage{thm-restate}\newtheorem{theorem}{Theorem}",r"\begin{restatable}{theorem}{section}Lost.\end{restatable}"),
            (r"\usepackage{thm-restate}\newtheorem{theorem}{Theorem}",r"\begin{restatable}{theorem}{R}A.\end{restatable}\begin{restatable}{theorem}{R}B.\end{restatable}"),
            (r"\usepackage{thm-restate}\newtheorem{theorem}{Theorem}",r"\R*\begin{restatable}{theorem}{R}Words.\end{restatable}")]:
            with self.subTest(pre=pre,body=body):
                self.assertEqual(self._batch01_parse(body,preamble=pre)["versions"][0]["status"],"parse_error")

    def test_batch02_a_restatable_unknown_body_and_missing_heading_reference_not_dropped(self):
        for statement in (r"\unknown{Lost}",r"\cite{missing}"):
            with self.subTest(statement=statement):
                r=self._batch01_parse(r"\begin{restatable}{theorem}{R}"+statement+r"\end{restatable}",
                    preamble=r"\usepackage{thm-restate}\newtheorem{theorem}{Theorem}")
                self.assertEqual(r["versions"][0]["status"],"parse_error")

    def test_batch02_a_restatable_source_defined_environment_takes_precedence(self):
        r=self._batch01_parse(r"\begin{restatable}Body.\end{restatable}",
            preamble=r"\usepackage{thm-restate}\newenvironment{restatable}{Begin.}{End.}")
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        content=" ".join(n["content"] for n in r["elements"])
        for word in ("Begin.","Body.","End."):self.assertIn(word,content)

    def test_batch02_a_linebreak_valid_strength_preserves_text_and_math(self):
        for option in ("","[0]","[4]"):
            with self.subTest(option=option):
                r=self._batch01_parse(r"First\linebreak"+option+r" second $x$.")
                self.assertEqual(r["versions"][0]["status"],"success")
                self.assertIn("First second $x$."," ".join(n["content"] for n in r["elements"]))

    def test_batch02_a_linebreak_bad_strength_and_local_override(self):
        for option in ("[9]",r"[\value]","["):
            with self.subTest(option=option):
                self.assertEqual(self._batch01_parse(r"First\linebreak"+option+" second.")["versions"][0]["status"],"parse_error")
        r=self._batch01_parse(r"\linebreak After.",preamble=r"\newcommand{\linebreak}{Local wording.}")
        self.assertEqual(r["versions"][0]["status"],"success")
        self.assertIn("Local wording."," ".join(n["content"] for n in r["elements"]))

    def test_batch02_a_iccv_supplement_title_keeps_appendix_and_citations(self):
        r=self._batch01_parse(r"Main.\maketitlesupplementary\section{Proofs}Appendix \cite{k}.",
            preamble=r"\usepackage{iccv}\bibliography{r}",files={"r.bib":"@article{k,title={Proof}}"})
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        content=[n["content"] for n in r["elements"]]
        self.assertLess(content.index("Main."),content.index("Supplementary Material"))
        self.assertLess(content.index("Supplementary Material"),content.index("Proofs"))
        self.assertIn(r"Appendix \cite{k}.",content)

    def test_batch02_a_supplement_unknown_aaai_and_local_override(self):
        self.assertEqual(self._batch01_parse(r"\maketitlesupplementary Lost.",
            preamble=r"\usepackage{aaai2026}")["versions"][0]["status"],"parse_error")
        r=self._batch01_parse(r"\maketitlesupplementary After.",
            preamble=r"\usepackage{iccv}\newcommand{\maketitlesupplementary}{Own title.}")
        self.assertEqual(r["versions"][0]["status"],"success")
        self.assertIn("Own title."," ".join(n["content"] for n in r["elements"]))

    def test_batch02_a_acm_teaserfigure_keeps_caption_citation_and_order_without_image(self):
        r=self._batch01_parse(r"\begin{teaserfigure}\includegraphics{missing.png}"
            r"\caption{Teaser $x$ \cite{k}.}\end{teaserfigure}\begin{abstract}Abstract.\end{abstract}",
            document_class="acmart",preamble=r"\bibliography{r}",
            files={"r.bib":"@article{k,title={Teaser reference}}"})
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        self.assertEqual(r["elements"][1]["kind"],"figure")
        self.assertIn(r"\caption{Teaser $x$ \cite{k}.}",r["elements"][1]["content"])
        self.assertIn("Abstract.",[n["content"] for n in r["elements"]])

    def test_batch02_a_teaserfigure_unknown_class_bad_option_and_missing_cite_fail(self):
        for cls,body in [
            ("article",r"\begin{teaserfigure}Caption.\end{teaserfigure}"),
            ("acmart",r"\begin{teaserfigure}[odd]Caption.\end{teaserfigure}"),
            ("acmart",r"\begin{teaserfigure}\caption{See \cite{missing}.}\end{teaserfigure}")]:
            with self.subTest(cls=cls,body=body):
                self.assertEqual(self._batch01_parse(body,document_class=cls)["versions"][0]["status"],"parse_error")

    def test_batch02_a_newlength_literal_and_scaled_width_keep_table_and_words(self):
        for declaration in (r"\newlength{\colwidth}",r"\newlength\colwidth"):
            with self.subTest(declaration=declaration):
                r=self._batch01_parse(declaration+r"\setlength{\colwidth}{0.19\textwidth}"
                    r"\begin{tabular}{p{\colwidth}}Cell \cite{k}.\end{tabular}After.",
                    preamble=r"\bibliography{r}",files={"r.bib":"@article{k,title={Cell ref}}"})
                self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
                self.assertIn(r"Cell \cite{k}"," ".join(n["content"] for n in r["elements"]))
                self.assertIn("After.",[n["content"] for n in r["elements"]])

    def test_batch02_a_newlength_malformed_duplicate_dynamic_and_readback_fail(self):
        for body in (r"\newlength{\section}",r"\newlength{\length\other}",r"\newlength{\x}\newlength{\x}",
                     r"\newlength{\x}\setlength{\x}{\unknown}",
                     r"\newlength{\x}\setlength{\x}{12pt}\baselineskip=\x",
                     r"\setlength{\undeclared}{12pt}",r"\newlength\@bad"):
            with self.subTest(body=body):
                self.assertEqual(self._batch01_parse(body)["versions"][0]["status"],"parse_error")

    def test_batch02_a_newlength_local_command_definition_takes_precedence(self):
        r=self._batch01_parse(r"\newlength{Actual} After.",
            preamble=r"\newcommand{\newlength}[1]{#1 wording.}")
        self.assertEqual(r["versions"][0]["status"],"success")
        self.assertIn("Actual wording."," ".join(n["content"] for n in r["elements"]))

    def test_batch02_a_textbullet_keeps_visible_glyph_and_following_citation(self):
        r=self._batch01_parse(r"\textbf{\textbullet}\ Contribution \cite{k}.",
            preamble=r"\bibliography{r}",files={"r.bib":"@article{k,title={Reference}}"})
        self.assertEqual(r["versions"][0]["status"],"success")
        self.assertIn("\u2022 Contribution "+r"\cite{k}."," ".join(n["content"] for n in r["elements"]))

    def test_batch02_a_textbullet_local_override_and_unknown_group_not_hidden(self):
        r=self._batch01_parse(r"\textbullet After.",preamble=r"\newcommand{\textbullet}{Marker}")
        self.assertEqual(r["versions"][0]["status"],"success")
        self.assertIn("Marker After."," ".join(n["content"] for n in r["elements"]))
        self.assertEqual(self._batch01_parse(r"\textbullet{\unknown{Lost}}")["versions"][0]["status"],"parse_error")

    def test_batch02_a_addtolength_braced_and_unbraced_layout_only(self):
        for assignment in (r"\addtolength{\tabcolsep}{2pt}",r"\addtolength\linewidth{-2em}"):
            with self.subTest(assignment=assignment):
                r=self._batch01_parse(assignment+r"\section{Next}Words $x$ stay.")
                self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
                self.assertIn("Words $x$ stay.",[n["content"] for n in r["elements"]])

    def test_batch02_a_addtolength_dynamic_target_or_value_fails(self):
        for body in (r"\addtolength{\section}{2pt}",r"\addtolength{\tabcolsep}{\unknown}",
                     r"\addtolength{\tabcolsep}{2pt+1pt}",r"\addtolength{\tabcolsep}"):
            with self.subTest(body=body):
                self.assertEqual(self._batch01_parse(body)["versions"][0]["status"],"parse_error")

    def test_batch02_a_addtolength_local_override_retains_content(self):
        r=self._batch01_parse(r"\addtolength{One}{Two}After.",
            preamble=r"\newcommand{\addtolength}[2]{#1 and #2.}")
        self.assertEqual(r["versions"][0]["status"],"success")
        self.assertIn("One and Two."," ".join(n["content"] for n in r["elements"]))


    def test_batch02_b_restatable_literal_declaretheorem_and_replay(self):
        r=self._batch01_parse(r"\begin{restatable}[Named]{thm}{R}Statement $x$.\end{restatable}\R*After.",
            preamble=r"\usepackage{thmtools,thm-restate}\declaretheorem[name=Theorem]{thm}")
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        self.assertEqual([n["content"] for n in r["elements"]].count("Statement $x$."),2)

    def test_batch02_b_declaretheorem_dynamic_hook_or_unknown_environment_stays_error(self):
        for declaration in (r"\declaretheorem[name=\unknown]{thm}",r"\declaretheorem[preheadhook={Lost.}]{thm}",
                            r"\declaretheorem[name=Theorem]{\dynamic}"):
            with self.subTest(declaration=declaration):
                r=self._batch01_parse(r"\begin{restatable}{thm}{R}Text.\end{restatable}",
                    preamble=r"\usepackage{thmtools,thm-restate}"+declaration)
                self.assertEqual(r["versions"][0]["status"],"parse_error")

    def test_batch02_b_declaretheorem_in_inactive_branch_is_not_evidence(self):
        r=self._batch01_parse(r"\begin{restatable}{thm}{R}Text.\end{restatable}",
            preamble=r"\usepackage{thmtools,thm-restate}\iffalse\declaretheorem[name=Theorem]{thm}\fi")
        self.assertEqual(r["versions"][0]["status"],"parse_error")
        self.assertIn("Unregistered restatable theorem",r["versions"][0]["error"])

    def test_batch02_b_ac_and_gls_are_not_replaced_by_keys(self):
        for command,preamble in [
            ("ac",r"\usepackage{acronym}\begin{acronym}\acro{TM}{Tsetlin Machine}\end{acronym}"),
            ("gls",r"\usepackage{glossaries}\newacronym{TM}{TM}{Tsetlin Machine}")]:
            with self.subTest(command=command):
                r=self._batch01_parse("\\"+command+"{TM} After.",preamble=preamble)
                self.assertEqual(r["versions"][0]["status"],"parse_error")
                self.assertIn("Unsupported prose command",r["versions"][0]["error"])
                self.assertEqual(r["elements"],[])

    def test_batch02_b_roman_literal_int_lowercase_and_existing_uppercase(self):
        for number,expected in [(1,"i"),(4,"iv"),(9,"ix"),(49,"xlix"),(3999,"mmmcmxcix"),(0,""),(-1,"")]:
            with self.subTest(number=number):
                r=self._batch01_parse(r"("+r"\romannumeral"+str(number)+r") Words $x$.")
                self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
                self.assertIn("("+expected+") Words $x$."," ".join(n["content"] for n in r["elements"]))
        r=self._batch01_parse(r"\uppercase\expandafter{\romannumeral4} Body.")
        self.assertEqual(r["versions"][0]["status"],"success")
        self.assertIn("IV Body."," ".join(n["content"] for n in r["elements"]))

    def test_batch02_b_roman_does_not_guess_macro_integer_or_expandable_terminator(self):
        for value in (r"\year",r"2\unknown","4000","9"*5000):
            with self.subTest(value=value[:20]):
                self.assertEqual(self._batch01_parse(r"\romannumeral"+value+") After.")["versions"][0]["status"],"parse_error")
        r=self._batch01_parse(r"\romannumeral3\relax) Body.")
        self.assertEqual(r["versions"][0]["status"],"success")
        self.assertIn("iii) Body."," ".join(n["content"] for n in r["elements"]))

    def test_batch02_b_roman_local_definition_wins(self):
        r=self._batch01_parse(r"\romannumeral{Local} After.",
            preamble=r"\newcommand{\romannumeral}[1]{#1 text.}")
        self.assertEqual(r["versions"][0]["status"],"success")
        self.assertIn("Local text."," ".join(n["content"] for n in r["elements"]))

    def test_batch02_b_cleardoublepage_preserves_text_and_override(self):
        for pre,expected in [("","Before. After."),(r"\newcommand{\cleardoublepage}{Local text.}","Local text.")]:
            with self.subTest(pre=pre):
                r=self._batch01_parse(r"Before.\cleardoublepage After.",preamble=pre)
                self.assertEqual(r["versions"][0]["status"],"success")
                self.assertIn(expected," ".join(n["content"] for n in r["elements"]))
        self.assertEqual(self._batch01_parse(r"\cleardoublepage\unknown{Lost}")["versions"][0]["status"],"parse_error")

    def test_batch02_b_parskip_literal_glue_preserves_following_sections(self):
        for value in ("4pt","=3pt"," = 3pt plus 1pt minus .5pt"):
            with self.subTest(value=value):
                r=self._batch01_parse(r"\parskip"+value+r"\section{Next}Words.")
                self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
                self.assertIn("Next",[n["content"] for n in r["elements"]])
                self.assertIn("Words.",[n["content"] for n in r["elements"]])

    def test_batch02_b_parskip_dynamic_and_local_definition(self):
        for value in (r"=\unknown","=3pt plus Bad",r"\section{Lost}"):
            with self.subTest(value=value):
                self.assertEqual(self._batch01_parse(r"\parskip"+value)["versions"][0]["status"],"parse_error")
        r=self._batch01_parse(r"\parskip After.",preamble=r"\newcommand{\parskip}{Local text.}")
        self.assertEqual(r["versions"][0]["status"],"success")
        self.assertIn("Local text."," ".join(n["content"] for n in r["elements"]))

    def test_batch02_b_aa_acknowledgement_keeps_text_citation_and_root_parent(self):
        r=self._batch01_parse(r"\section{Conclusions}Done.\begin{acknowledgement}Thanks \cite{k}."
            r"\end{acknowledgement}\appendix\section{Extra}Appendix.",
            document_class="aa",preamble=r"\bibliography{r}",files={"r.bib":"@article{k,title={Thanks}}"})
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        node=next(n for n in r["elements"] if n["content"]=="acknowledgement")
        edge=next(e for e in r["hierarchy"] if e["element_id"]==node["element_id"])
        root=next(n for n in r["elements"] if n["kind"]=="document")
        self.assertEqual(edge["parent_id"],root["element_id"])
        self.assertIn(r"Thanks \cite{k}.",[n["content"] for n in r["elements"]])

    def test_batch02_b_acknowledgement_unknown_class_and_unknown_content_fail(self):
        for cls,body in [("article",r"\begin{acknowledgement}Words.\end{acknowledgement}"),
                          ("aa",r"\begin{acknowledgement}\unknown{Words}\end{acknowledgement}")]:
            with self.subTest(cls=cls):
                self.assertEqual(self._batch01_parse(body,document_class=cls)["versions"][0]["status"],"parse_error")

    def test_batch02_b_arcsec_aa_keeps_units_math_and_following_content(self):
        r=self._batch01_parse(r"Beam 1\arcsec\ pixel. Math $2\arcsec$ stays.",document_class="aa")
        self.assertEqual(r["versions"][0]["status"],"success")
        content=" ".join(n["content"] for n in r["elements"])
        self.assertIn("1\u2033 pixel.",content);self.assertIn(r"$2\arcsec$",content)

    def test_batch02_b_arcsec_undeclared_class_or_local_override(self):
        self.assertEqual(self._batch01_parse(r"1\arcsec angle.")["versions"][0]["status"],"parse_error")
        r=self._batch01_parse(r"\arcsec After.",document_class="aa",
            preamble=r"\newcommand{\arcsec}{Source unit}")
        self.assertEqual(r["versions"][0]["status"],"success")
        self.assertIn("Source unit After."," ".join(n["content"] for n in r["elements"]))

    def test_batch02_b_leftline_reparses_heading_math_and_citations(self):
        r=self._batch01_parse(r"\leftline{Line $x$ \cite{k}.}\section{Intro}After.",
            preamble=r"\bibliography{r}",files={"r.bib":"@article{k,title={Ref}}"})
        self.assertEqual(r["versions"][0]["status"],"success")
        self.assertIn(r"Line $x$ \cite{k}.",[n["content"] for n in r["elements"]])
        self.assertIn("After.",[n["content"] for n in r["elements"]])

    def test_batch02_b_leftline_unknown_content_and_local_override(self):
        self.assertEqual(self._batch01_parse(r"\leftline{\unknown{Lost}}")["versions"][0]["status"],"parse_error")
        r=self._batch01_parse(r"\leftline{Own}After.",preamble=r"\newcommand{\leftline}[1]{#1 source.}")
        self.assertEqual(r["versions"][0]["status"],"success")
        self.assertIn("Own source."," ".join(n["content"] for n in r["elements"]))

    def test_batch02_b_newcolumntype_pure_layout_keeps_table_citation(self):
        for pre,declaration in [
            (r"\usepackage{array}",r"\newcolumntype{P}[1]{>{\centering\arraybackslash}p{#1}}"),
            (r"\usepackage{array,colortbl}",r"\newcolumntype{R}{>{\columncolor{gray!40}}r}"),
            (r"\usepackage{array,tabularx}",r"\newcolumntype{s}{>{\hsize=.2\hsize}X}"),
            (r"\usepackage{array,ragged2e}",r"\newcolumntype{R}[1]{>{\RaggedLeft\arraybackslash}p{#1}}")]:
            with self.subTest(declaration=declaration):
                r=self._batch01_parse(declaration+r"\begin{tabular}{P{2cm}}Cell \cite{k}.\end{tabular}After.",
                    preamble=pre+r"\bibliography{r}",files={"r.bib":"@article{k,title={Cell evidence}}"})
                self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
                self.assertIn(r"Cell \cite{k}"," ".join(n["content"] for n in r["elements"]))
                self.assertIn("After.",[n["content"] for n in r["elements"]])

    def test_batch02_b_newcolumntype_content_injection_cycle_or_bad_parameter_fails(self):
        for spec,option in [(r">{Visible words}l",""),(r">{\cite{k}}l",""),(r">{\unknown}l",""),
                            ("P",""),("p{#2}","[1]"),("p{#0}","[1]"),("X",""),
                            (r">{\hsize=\unknown}l","")]:
            with self.subTest(spec=spec):
                r=self._batch01_parse(r"\newcolumntype{P}"+option+"{"+spec+"}After.",preamble=r"\usepackage{array}")
                self.assertEqual(r["versions"][0]["status"],"parse_error")
        self.assertEqual(self._batch01_parse(r"\newcolumntype{P}{l}After.")["versions"][0]["status"],"parse_error")

    def test_batch02_b_newcolumntype_conflict_and_local_override(self):
        r=self._batch01_parse(r"\newcolumntype{P}{l}\newcolumntype{P}{r}",
            preamble=r"\usepackage{array}")
        self.assertEqual(r["versions"][0]["status"],"parse_error")
        r=self._batch01_parse(r"\newcolumntype{Words}{Here}",
            preamble=r"\newcommand{\newcolumntype}[2]{#1 #2.}")
        self.assertEqual(r["versions"][0]["status"],"success")
        self.assertIn("Words Here."," ".join(n["content"] for n in r["elements"]))

    def test_batch02_b_scalebox_reparses_math_citation_and_following_body(self):
        r=self._batch01_parse(r"\scalebox{.9}[1.2]{Before \cite{k}.\[x=y\]After.} Tail.",
            preamble=r"\usepackage{graphicx}\bibliography{r}",files={"r.bib":"@article{k,title={Reference}}"})
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        content=[n["content"] for n in r["elements"]]
        for value in (r"Before \cite{k}.","After.","Tail."):self.assertIn(value,content)
        self.assertTrue(any(n["kind"]=="block_formula" and "x=y" in n["content"] for n in r["elements"]))
        adjacent=self._batch01_parse(r"\scalebox{1}{After.}Tail.",preamble=r"\usepackage{graphicx}")
        self.assertEqual(adjacent["versions"][0]["status"],"success")
        self.assertIn("After.Tail.",[n["content"] for n in adjacent["elements"]])

    def test_batch02_b_scalebox_invalid_factor_undeclared_or_unknown_body_fails(self):
        for pre,body in [(r"\usepackage{graphicx}",r"\scalebox{0}{Lost}"),
                         (r"\usepackage{graphicx}",r"\scalebox{\dynamic}{Lost}"),
                         (r"\usepackage{graphicx}",r"\scalebox{.9}[\dynamic]{Lost}"),
                         (r"\usepackage{graphicx}",r"\scalebox{.9}{\unknown{Lost}}"),
                         ("",r"\scalebox{1}{Words}")]:
            with self.subTest(pre=pre,body=body):
                self.assertEqual(self._batch01_parse(body,preamble=pre)["versions"][0]["status"],"parse_error")

    def test_batch02_b_scalebox_local_definition_takes_precedence(self):
        r=self._batch01_parse(r"\scalebox{Own}{Words}After.",
            preamble=r"\usepackage{graphicx}\newcommand{\scalebox}[2]{#1 #2.}")
        self.assertEqual(r["versions"][0]["status"],"success")
        self.assertIn("Own Words."," ".join(n["content"] for n in r["elements"]))


    def test_batch02_b_declaretheorem_inside_unused_argument_or_scope_is_not_evidence(self):
        for hidden in (r"\title{\declaretheorem[name=Theorem]{thm}}",
                       r"{\declaretheorem[name=Theorem]{thm}}"):
            with self.subTest(hidden=hidden):
                r=self._batch01_parse(r"\begin{restatable}{thm}{R}Words.\end{restatable}",
                    preamble=r"\usepackage{thmtools,thm-restate}"+hidden)
                self.assertEqual(r["versions"][0]["status"],"parse_error")
                self.assertIn("Unregistered restatable theorem",r["versions"][0]["error"])


    def test_batch02_b_custom_column_local_format_commands_are_not_assumed_invisible(self):
        for declaration,spec in [
            (r"\newcommand{\centering}{VISIBLE}",r">{\centering}l"),
            (r"\newcommand{\arraybackslash}{VISIBLE}",r">{\arraybackslash}l"),
            (r"\newcommand{\columncolor}[1]{VISIBLE}",r">{\columncolor{gray}}l"),
            (r"\newcommand{\hsize}{VISIBLE}",r">{\hsize=.2\hsize}l"),
            (r"\newcommand{\linewidth}{VISIBLE}",r"p{.2\linewidth}")]:
            with self.subTest(declaration=declaration):
                r=self._batch01_parse(r"\newcolumntype{P}{"+spec+r"}\begin{tabular}{P}Cell.\end{tabular}",
                    preamble=r"\usepackage{array,colortbl}"+declaration)
                self.assertEqual(r["versions"][0]["status"],"parse_error")
                self.assertIn("Locally redefined custom column",r["versions"][0]["error"])

    def test_batch02_b_redefined_roman_terminator_and_length_register_are_blocked(self):
        for pre,body in [
            (r"\newcommand{\relax}{VISIBLE}",r"\romannumeral2\relax) After."),
            (r"\newcommand{\linewidth}{VISIBLE}",r"\addtolength\linewidth{2pt}After.")]:
            with self.subTest(pre=pre):
                r=self._batch01_parse(body,preamble=pre)
                self.assertEqual(r["versions"][0]["status"],"parse_error")

    def test_batch03_keyword_template_variants_keep_visible_content(self):
        for cls,env in [('siamltex','keywords'),('siamart1116','keywords'),('JFM-FLM_Au','keywords'),
                        ('ieeetran','IEEEkeywords'),('IEEEtai','IEEEkeywords'),('ieeecolor','IEEEkeywords')]:
            with self.subTest(cls=cls),tempfile.TemporaryDirectory() as td:
                root=Path(td);source=tex(r'\begin{'+env+r'}Signal $x$.\end{'+env+r'}\section{Body}After.').replace('{article}','{'+cls+'}')
                (root/'main.tex').write_text(source,encoding='utf8');r=parse_versions(AID,[(1,root)]);validate_tables(r)
                self.assertEqual(r['versions'][0]['status'],'success');content=' '.join(n['content'] for n in r['elements'])
                for value in ('keywords','Signal $x$.','Body','After.'):self.assertIn(value,content)

    def test_batch03_subequations_implicit_class_and_mathtools_keep_math(self):
        for cls,pre in [('acmart',''),('revtex4-2',''),('article',r'\usepackage{mathtools}')]:
            with self.subTest(cls=cls),tempfile.TemporaryDirectory() as td:
                root=Path(td);(root/'main.tex').write_text(r'\documentclass{'+cls+'}'+pre+r'\begin{document}\begin{subequations}\begin{equation}x=y\end{equation}\begin{align}a&=b\end{align}\end{subequations}After.\end{document}')
                r=parse_versions(AID,[(1,root)]);self.assertEqual(r['versions'][0]['status'],'success')
                maths=[n for n in r['elements'] if n['kind']=='block_formula'];self.assertEqual(len(maths),2)
                self.assertTrue(all(e['version']==1 for e in r['hierarchy']))

    def test_batch03_algorithm_star_keeps_caption_nested_control_and_citation(self):
        r=self._batch01_parse(r'\begin{algorithm*}[t]\caption{Wide algorithm}\begin{algorithmic}\FOR{$i$}\STATE Step \cite{k}.\ENDFOR\RETURN Result.\end{algorithmic}\end{algorithm*}After.\begin{thebibliography}{1}\bibitem{k}A.\newblock Evidence.\end{thebibliography}',preamble=r'\usepackage{algorithm,algorithmic}')
        self.assertEqual(r['versions'][0]['status'],'success');content=' '.join(n['content'] for n in r['elements'])
        for value in ('Wide algorithm',r'\cite{k}','return Result.','After.'):self.assertIn(value,content)
        self.assertEqual(r['bibtex'][0]['citation_key'],'k')

    def test_batch03_algorithm_star_requires_known_package(self):
        r=self.parse({'main.tex':tex(r'\begin{algorithm*}Text.\end{algorithm*}')})
        self.assertEqual(r['versions'][0]['status'],'parse_error')

    def test_batch03_accents_on_dotless_letters_keep_unicode_and_following_text(self):
        r=self._batch01_parse(r"Cient\'{\i}fico. Kre\v{\i}n. \^{\j} Next.")
        self.assertEqual(r['versions'][0]['status'],'success');text=' '.join(n['content'] for n in r['elements'])
        for value in ('Cient\u00edfico.','Kre\u01d0n.','\u0135','Next.'):self.assertIn(value.encode().decode('unicode_escape') if '\\u' in value else value,text)

    def test_batch03_unknown_compound_accent_is_not_guessed(self):
        r=self._batch01_parse(r"\'{\unknown}Text.")
        self.assertEqual(r['versions'][0]['status'],'parse_error');self.assertIn('compound text accent',r['versions'][0]['error'])

    def test_batch03_listing_multiple_braced_value_is_kept_and_not_executed(self):
        from src.m2.latex import listing_options
        config=listing_options(r'literate={x}{{\textbf{y}}}1,caption={A,B},mathescape=false')
        self.assertEqual(config['literate'],r'{x}{{\textbf{y}}}1');self.assertEqual(config['caption'],'A,B')
        r=self._batch01_parse(r'\begin{lstlisting}x\end{lstlisting}',preamble=r'\usepackage{listings}\lstset{literate={x}{{y}}1}')
        self.assertEqual(r['versions'][0]['status'],'parse_error');self.assertIn('interpretation option: literate',r['versions'][0]['error'])

    def test_batch03_box_named_style_keeps_title_body_and_following_section(self):
        r=self._batch01_parse(r'\begin{tcolorbox}[prompt_box,title={Evidence \cite{k}}]Visible $x$.\end{tcolorbox}\section{After}End.\begin{thebibliography}{1}\bibitem{k}A.\newblock Title.\end{thebibliography}',preamble=r'\usepackage{tcolorbox}\tcbset{prompt_box/.style={enhanced,breakable,colback=gray!10,fontupper=\ttfamily\small,width=1.\columnwidth}}')
        self.assertEqual(r['versions'][0]['status'],'success');text=' '.join(n['content'] for n in r['elements'])
        for value in ('Evidence',r'\cite{k}','Visible $x$.','After','End.'):self.assertIn(value,text)

    def test_batch03_box_hooks_unknown_style_and_cycles_are_blocked(self):
        for declaration,option in [(r'bad/.style={before={LOST}}','bad'),('a/.style={b},b/.style={a}','a'),('','unresolved')]:
            with self.subTest(option=option),tempfile.TemporaryDirectory() as td:
                root=Path(td);(root/'main.tex').write_text(r'\documentclass{article}\usepackage{tcolorbox}\tcbset{'+declaration+r'}\begin{document}\begin{tcolorbox}['+option+r']Body.\end{tcolorbox}\end{document}')
                r=parse_versions(AID,[(1,root)]);self.assertEqual(r['versions'][0]['status'],'parse_error');self.assertIn('tcolorbox',r['versions'][0]['error'])

    def test_batch03_algorithmic_prefix_return_and_linebreak_after_end(self):
        r=self._batch01_parse(r'\begin{algorithmic}\label{a}\small\FOR{$i$}\STATE Work.\ENDFOR\\\RETURN Result.\end{algorithmic}After.')
        self.assertEqual(r['versions'][0]['status'],'success');text=' '.join(n['content'] for n in r['elements'])
        for value in ('Work.','return Result.','After.'):self.assertIn(value,text)

    def test_batch03_algorithmic_visible_prefix_and_bad_nesting_still_fail(self):
        for body in (r'Unmarked text.\STATE Work.',r'\FOR{$i$}\STATE Work.\ENDIF\\\RETURN Result.'):
            with self.subTest(body=body),tempfile.TemporaryDirectory() as td:
                root=Path(td);(root/'main.tex').write_text(tex(r'\begin{algorithmic}'+body+r'\end{algorithmic}'))
                self.assertEqual(parse_versions(AID,[(1,root)])['versions'][0]['status'],'parse_error')

    def test_batch03_algorithmic_displayed_condition_body_keeps_equations_and_prose(self):
        r=self._batch01_parse(r'\begin{algorithmic}\For{$k$}\begin{equation}x=y\end{equation}where $x$ is retained.\EndFor\end{algorithmic}')
        self.assertEqual(r['versions'][0]['status'],'success');text=' '.join(n['content'] for n in r['elements'])
        self.assertIn('x=y',text);self.assertIn('where $x$ is retained.',text)

    def test_batch03_root_supplementary_material_title_is_explicit_not_filename(self):
        r=self.parse({'article.tex':tex(r'\section{Main}Scientific body.'),
                      'unrelated-name.tex':r'\documentclass{article}\title{\textbf{Supplementary Material for Main}}\begin{document}Supplement.\end{document}'})
        self.assertEqual(r['versions'][0]['status'],'success');self.assertEqual(r['versions'][0]['root'],'article.tex')
        self.assertNotIn('Supplement.',' '.join(n['content'] for n in r['elements']))

    def test_batch03_root_explicit_acl_template_titles_are_auxiliary(self):
        r=self.parse({'paper.tex':tex(r'\section{Introduction}Actual science.'),
                      'a.tex':r'\documentclass{article}\title{Instructions for *ACL Proceedings}\begin{document}Template.\end{document}',
                      'b.tex':r'\documentclass{article}\title{LuaLaTeX and XeLaTeX Template for *ACL Style Files}\begin{document}Example.\end{document}'})
        self.assertEqual(r['versions'][0]['status'],'success');self.assertEqual(r['versions'][0]['root'],'paper.tex')
        self.assertNotIn('Template.',' '.join(n['content'] for n in r['elements']))

    def test_batch03_root_incidental_template_words_or_two_real_papers_stay_ambiguous(self):
        for title in ('Analysis of Templates','Actual second paper'):
            with self.subTest(title=title),tempfile.TemporaryDirectory() as td:
                root=Path(td);(root/'first.tex').write_text(tex('Main.'))
                (root/'second.tex').write_text(r'\documentclass{article}\title{'+title+r'}\begin{document}Independent.\end{document}')
                r=parse_versions(AID,[(1,root)]);self.assertIn('found 2',r['versions'][0]['error'])

    def test_batch03_preamble_engine_equivalent_extensions_keep_content_and_citations(self):
        pre=r'\usepackage{graphicx,ifpdf}\ifpdf\DeclareGraphicsExtensions{.pdf,.png}\else\DeclareGraphicsExtensions{.eps}\fi'
        r=self._batch01_parse(r'\section{Body}Actual $x$ \cite{k}.\begin{thebibliography}{1}\bibitem{k}A.\newblock Title.\end{thebibliography}',preamble=pre)
        self.assertEqual(r['versions'][0]['status'],'success');self.assertEqual(r['bibtex'][0]['citation_key'],'k')
        self.assertTrue(any('projected preamble' in note for note in r['versions'][0]['normalization_notes']))
        self.assertIn('Actual $x$', ' '.join(n['content'] for n in r['elements']))

    def test_batch03_preamble_driver_or_link_color_equivalence_is_not_engine_truth(self):
        for conditional in (r'\ifx\pdfoutput\undefined\usepackage[dvipdfmx]{graphicx,hyperref}\else\usepackage{graphicx,hyperref}\fi',
                            r'\ifpdf\usepackage[linkcolor=blue,colorlinks=true]{hyperref}\else\usepackage[linkcolor=red,colorlinks=true]{hyperref}\fi'):
            with self.subTest(conditional=conditional),tempfile.TemporaryDirectory() as td:
                root=Path(td);(root/'main.tex').write_text(r'\documentclass{article}'+conditional+r'\begin{document}Actual.\end{document}')
                r=parse_versions(AID,[(1,root)]);self.assertEqual(r['versions'][0]['status'],'success')
                self.assertIn('Actual.',' '.join(n['content'] for n in r['elements']))

    def test_batch03_engine_content_branches_hooks_and_local_overrides_stay_blocked(self):
        cases=[(r'\ifpdf PDF content.\else DVI content.\fi',''),
               (r'\ifpdf\usepackage[draft]{graphicx}\else\usepackage{graphicx}\fi',''),
               (r'\ifpdf\DeclareGraphicsExtensions{.pdf}\else\DeclareGraphicsExtensions{.eps}\fi',r'\newcommand{\DeclareGraphicsExtensions}[1]{VISIBLE}')]
        for conditional,pre in cases:
            with self.subTest(conditional=conditional),tempfile.TemporaryDirectory() as td:
                root=Path(td);(root/'main.tex').write_text(r'\documentclass{article}'+pre+conditional+r'\begin{document}Body.\end{document}')
                self.assertEqual(parse_versions(AID,[(1,root)])['versions'][0]['status'],'parse_error')

    def test_batch03_ifnum_literal_source_value_keeps_only_verified_branch(self):
        r=self._batch01_parse(r'\ifnum 1=\showvalue Selected $x$.\else Wrong.\fi After.',preamble=r'\def\showvalue{1}')
        self.assertEqual(r['versions'][0]['status'],'success');text=' '.join(n['content'] for n in r['elements'])
        self.assertIn('Selected $x$.',text);self.assertIn('After.',text);self.assertNotIn('Wrong.',text)

    def test_batch03_ifnum_unknown_counter_scope_alias_and_overflow_are_blocked(self):
        cases=[('',r'\ifnum\value{page}=1 Unknown.\fi'),
               (r'\def\a{1}{\def\a{0}}',r'\ifnum\a=1 Uncertain.\fi'),
               (r'\def\a{1}\let\a\unknown',r'\ifnum\a=1 Uncertain.\fi'),
               ('',r'\ifnum2147483648=1 Overflow.\fi')]
        for pre,body in cases:
            with self.subTest(body=body),tempfile.TemporaryDirectory() as td:
                root=Path(td);(root/'main.tex').write_text(r'\documentclass{article}'+pre+r'\begin{document}'+body+r'\end{document}')
                self.assertEqual(parse_versions(AID,[(1,root)])['versions'][0]['status'],'parse_error')

    def test_batch03_ifdefined_needs_prior_source_proof(self):
        r=self._batch01_parse(r'\ifdefined\proven Yes.\else No.\fi',preamble=r'\def\proven{}')
        self.assertEqual(r['versions'][0]['status'],'success');text=' '.join(n['content'] for n in r['elements'])
        self.assertIn('Yes.',text);self.assertNotIn('No.',text)

    def test_batch03_ifdefined_unknown_or_scoped_definition_stays_blocked(self):
        for pre in ('',r'{\def\proven{}}'):
            with self.subTest(pre=pre),tempfile.TemporaryDirectory() as td:
                root=Path(td);(root/'main.tex').write_text(r'\documentclass{article}'+pre+r'\begin{document}\ifdefined\proven Yes.\else No.\fi\end{document}')
                self.assertEqual(parse_versions(AID,[(1,root)])['versions'][0]['status'],'parse_error')

    def test_batch03_inactive_definitions_do_not_run_or_change_source_metadata(self):
        r=self._batch01_parse(r'\iffalse\renewcommand{\arraystretch}{1.2}\newcommand{\hidden}{LOST}\fi Visible.\begin{equation}x=y\end{equation}')
        self.assertEqual(r['versions'][0]['status'],'success');text=' '.join(n['content'] for n in r['elements'])
        self.assertIn('Visible.',text);self.assertNotIn('LOST',text);self.assertNotIn('hidden',text)

    def test_batch03_inactive_definition_conditional_tokens_are_balanced_not_executed(self):
        r=self._batch01_parse(r'\iffalse\def\hidden{\iftrue X.\fi}\else Visible.\fi After.')
        self.assertEqual(r['versions'][0]['status'],'success');text=' '.join(n['content'] for n in r['elements'])
        self.assertIn('Visible.',text);self.assertNotIn('X.',text)

    def test_batch03_empty_citations_and_invalid_bibtex_keys_are_not_repaired(self):
        from src.m2.latex import citation_keys
        for citation in (r'\cite{}',r'\cite{a,}',r'\cite{,a}'):
            with self.subTest(citation=citation),self.assertRaises(ParseError):citation_keys(citation)
        for bib in ('@article{,title={Missing identity}}','@article{key\n title={No separator},year={2020}}'):
            with self.subTest(bib=bib),self.assertRaises(ParseError):parse_bibtex(bib)

    def test_batch03_box_scoped_styles_and_redefined_font_are_not_discarded(self):
        for pre in (r'\begingroup\tcbset{p/.style={colback=red}}\endgroup',
                    r'\newcommand{\small}{VISIBLE}\tcbset{p/.style={fontupper=\small}}',
                    r'\newcommand{\tcbset}[1]{VISIBLE}\tcbset{p/.style={colback=red}}'):
            with self.subTest(pre=pre),tempfile.TemporaryDirectory() as td:
                root=Path(td);(root/'main.tex').write_text(r'\documentclass{article}\usepackage{tcolorbox}'+pre+r'\begin{document}\begin{tcolorbox}[p]Body.\end{tcolorbox}\end{document}')
                r=parse_versions(AID,[(1,root)]);self.assertEqual(r['versions'][0]['status'],'parse_error')

    def test_batch03_selected_literal_does_not_inherit_inactive_newif_definitions(self):
        r=self._batch01_parse(r'\iffalse\newcommand{\showvalue}{1}\fi\ifdefined\showvalue Wrong.\fi')
        self.assertEqual(r['versions'][0]['status'],'parse_error');self.assertIn('ifdefined',r['versions'][0]['error'])

    def test_batch03_box_global_layout_defaults_do_not_block_valid_content(self):
        r=self._batch01_parse(r'\begin{tcolorbox}[title={Actual title}]Visible $x$ \cite{k}.\end{tcolorbox}After.\begin{thebibliography}{1}\bibitem{k}A.\newblock Evidence.\end{thebibliography}',preamble=r'\usepackage{tcolorbox}\tcbset{colback=white,colframe=black,boxrule=0.5pt}')
        self.assertEqual(r['versions'][0]['status'],'success');text=' '.join(n['content'] for n in r['elements'])
        for value in ('Actual title','Visible $x$',r'\cite{k}','After.'):self.assertIn(value,text)
        self.assertEqual(r['bibtex'][0]['citation_key'],'k')

    def test_batch03_box_global_executable_hook_is_not_silently_ignored(self):
        r=self._batch01_parse(r'\begin{tcolorbox}Body.\end{tcolorbox}',preamble=r'\usepackage{tcolorbox}\tcbset{before={Important text.}}')
        self.assertEqual(r['versions'][0]['status'],'parse_error');self.assertIn('before',r['versions'][0]['error'])

    def test_batch03_root_research_about_supplementary_material_is_not_a_role(self):
        r=self.parse({'first.tex':tex('Main.'),'second.tex':r'\documentclass{article}\title{A Model for Supplementary Material Retrieval}\begin{document}Actual second paper.\end{document}'})
        self.assertEqual(r['versions'][0]['status'],'parse_error');self.assertIn('found 2',r['versions'][0]['error'])


    def test_batch04_glyph_commands_preserve_symbols_and_delimiters(self):
        for body, expected in [(r"88\textmu m.", "88\u00b5m."),
                               (r"\lq{}Quoted\rq{} text.", "\u2018Quoted\u2019 text."),
                               (r"Path \textasciitilde/user.", r"\textasciitilde{}/user.")]:
            with self.subTest(body=body):
                r=self._batch01_parse(body)
                self.assertEqual(r['versions'][0]['status'],'success')
                self.assertIn(expected,' '.join(n['content'] for n in r['elements']))

    def test_batch04_glyph_local_macro_takes_precedence(self):
        for token in ('textmu','lq','rq','textasciitilde'):
            with self.subTest(token=token):
                r=self._batch01_parse('\\'+token+'{}.',preamble='\\newcommand{\\'+token+'}{Actual}')
                self.assertEqual(r['versions'][0]['status'],'success')
                self.assertIn('Actual',' '.join(n['content'] for n in r['elements']))

    def test_batch04_layout_declarations_keep_following_content_math_and_citation(self):
        for token in ('strut','raggedbottom','leavevmode','sl'):
            with self.subTest(token=token):
                r=self._batch01_parse('Before. \\'+token+r' {After $x$ \cite{k}.}\begin{thebibliography}{1}\bibitem{k}Evidence.\end{thebibliography}')
                self.assertEqual(r['versions'][0]['status'],'success')
                text=' '.join(n['content'] for n in r['elements'])
                for value in ('Before.','After $x$',r'\cite{k}'):self.assertIn(value,text)
                self.assertEqual(r['bibtex'][0]['citation_key'],'k')

    def test_batch04_layout_local_macro_is_not_discarded(self):
        for token in ('strut','raggedbottom','leavevmode','sl','nolinenumbers'):
            with self.subTest(token=token):
                r=self._batch01_parse('\\'+token+'{}.',preamble='\\newcommand{\\'+token+'}{Actual}')
                self.assertEqual(r['versions'][0]['status'],'success')
                self.assertIn('Actual',' '.join(n['content'] for n in r['elements']))

    def test_batch04_nolinenumbers_requires_package_or_class(self):
        for cls, preamble in [('article',r'\usepackage{lineno}'),('aastex631','')]:
            with self.subTest(cls=cls):
                r=self._batch01_parse(r'\nolinenumbers After.',document_class=cls,preamble=preamble)
                self.assertEqual(r['versions'][0]['status'],'success')
                self.assertIn('After.',' '.join(n['content'] for n in r['elements']))
        self.assertEqual(self._batch01_parse(r'\nolinenumbers After.')['versions'][0]['status'],'parse_error')

    def test_batch04_siam_AMS_preserves_codes_and_following_section(self):
        for cls in ('siamltex','siamart1116'):
            with self.subTest(cls=cls):
                r=self._batch01_parse(r'\begin{AMS}65M60, 74F10.\end{AMS}\section{Intro}After.',document_class=cls)
                self.assertEqual(r['versions'][0]['status'],'success')
                text=' '.join(n['content'] for n in r['elements'])
                for value in ('65M60','74F10','Intro','After.'):self.assertIn(value,text)
        self.assertEqual(self._batch01_parse(r'\begin{AMS}65M60\end{AMS}')['versions'][0]['status'],'parse_error')

    def test_batch04_ieee_drop_cap_context_and_content(self):
        for cls in ('IEEEtran','ieeetran','IEEEtai','ieeecolor'):
            with self.subTest(cls=cls):
                r=self._batch01_parse(r'\IEEEPARstart{C}{omputer} vision.',document_class=cls)
                self.assertEqual(r['versions'][0]['status'],'success')
                self.assertIn('Computer vision.',' '.join(n['content'] for n in r['elements']))
        self.assertEqual(self._batch01_parse(r'\IEEEPARstart{C}{omputer}')['versions'][0]['status'],'parse_error')


    def test_batch04_parbox_retains_nested_content_in_order(self):
        r=self._batch01_parse(r'Before. \parbox[t][2cm][c]{0.95\textwidth}{Box $x$. \section{Inner}See \cite{k}.}After.\begin{thebibliography}{1}\bibitem{k}Evidence.\end{thebibliography}')
        self.assertEqual(r['versions'][0]['status'],'success')
        text=' '.join(n['content'] for n in r['elements'])
        for value in ('Before.','Box $x$.','Inner',r'\cite{k}','After.'):self.assertIn(value,text)
        self.assertLess(text.index('Before.'),text.index('Box $x$.'));self.assertLess(text.index('Box $x$.'),text.index('After.'))
        validate_tables(r)

    def test_batch04_parbox_dynamic_dimensions_and_unknown_content_stay_errors(self):
        for body in (r'\parbox{\unknown}{Important.}',r'\parbox{2cm}{\unknown{Important.}}',r'\parbox[x]{2cm}{Important.}',r'\parbox{2cm}{Unclosed'):
            with self.subTest(body=body):self.assertEqual(self._batch01_parse(body)['versions'][0]['status'],'parse_error')

    def test_batch04_textcircled_retains_circle_and_raisebox_source(self):
        for value in ('1',r'\raisebox{-0.9pt}{2}'):
            with self.subTest(value=value):
                r=self._batch01_parse(r'Step \textcircled{'+value+'} continues.')
                self.assertEqual(r['versions'][0]['status'],'success')
                self.assertIn(r'\textcircled{'+value+'}',' '.join(n['content'] for n in r['elements']))
        self.assertEqual(self._batch01_parse(r'\textcircled{\unknown}End.')['versions'][0]['status'],'parse_error')

    def test_batch04_amstext_in_prose_preserves_nested_text_and_citation(self):
        r=self._batch01_parse(r'Before \text{visible \textbf{words} \cite{k}} after.\begin{thebibliography}{1}\bibitem{k}Evidence.\end{thebibliography}',preamble=r'\usepackage{amsmath}')
        self.assertEqual(r['versions'][0]['status'],'success')
        self.assertIn(r'Before visible words \cite{k} after.',' '.join(n['content'] for n in r['elements']))
        self.assertEqual(self._batch01_parse(r'\text{Unknown.}')['versions'][0]['status'],'parse_error')

    def test_batch04_layout_environments_preserve_body_and_bibliography(self):
        for env,arg,pkg in [('spacing','{1.17}','setspace'),('multicols','{2}','multicol'),('multicols*','{2}[Column heading.]','multicol'),('strip','','cuted')]:
            with self.subTest(env=env):
                r=self._batch01_parse(r'Before.\begin{'+env+'}'+arg+r'\section{Inside}Text $x$ \cite{k}.\end{'+env+r'}After.\begin{thebibliography}{1}\bibitem{k}Evidence.\end{thebibliography}',preamble=r'\usepackage{'+pkg+'}')
                self.assertEqual(r['versions'][0]['status'],'success');text=' '.join(n['content'] for n in r['elements'])
                for value in ('Before.','Inside','Text $x$',r'\cite{k}','After.'):self.assertIn(value,text)
                validate_tables(r)
                self.assertEqual(r['bibtex'][0]['citation_key'],'k')

    def test_batch04_layout_environments_fail_safely(self):
        for body,pkg in [(r'\begin{spacing}{\unknown}Text.\end{spacing}','setspace'),(r'\begin{multicols}{\unknown}Text.\end{multicols}','multicol'),(r'\begin{strip}Text.\end{strip}',''),(r'\begin{spacing}{0}Text.\end{spacing}','setspace')]:
            with self.subTest(body=body):self.assertEqual(self._batch01_parse(body,preamble=r'\usepackage{'+pkg+'}' if pkg else '')['versions'][0]['status'],'parse_error')

    def test_batch04_strip_keeps_graphic_and_separated_caption(self):
        r=self._batch01_parse(r'\begin{strip}\includegraphics{absent.pdf}\centering\vspace{-2mm}\captionof{figure}{Visible $x$ \cite{k}.}\end{strip}\begin{thebibliography}{1}\bibitem{k}Evidence.\end{thebibliography}',preamble=r'\usepackage{cuted,capt-of,graphicx}')
        self.assertEqual(r['versions'][0]['status'],'success');text=' '.join(n['content'] for n in r['elements'])
        for value in ('absent.pdf','Visible $x$',r'\cite{k}'):self.assertIn(value,text)
        self.assertTrue(any(n['kind']=='figure' for n in r['elements']))

    def test_batch04_flalign_preserves_formula_and_version_edges(self):
        r=self._batch01_parse(r'\section{Math}\begin{flalign*}a&=b+c&&\label{eq:x}\end{flalign*}After.',preamble=r'\usepackage{amsmath}')
        self.assertEqual(r['versions'][0]['status'],'success')
        formulas=[n for n in r['elements'] if n['kind']=='block_formula'];self.assertEqual(len(formulas),1)
        self.assertIn('a&=b+c&&',formulas[0]['content']);self.assertEqual({e['version'] for e in r['hierarchy']},{1});validate_tables(r)
        self.assertEqual(self._batch01_parse(r'\begin{flalign}a=b\end{flalign}')['versions'][0]['status'],'parse_error')

    def test_batch04_ieee_biography_without_photo_keeps_name_and_text(self):
        r=self._batch01_parse(r'\begin{IEEEbiographynophoto}{Alice}Research $x$.\end{IEEEbiographynophoto}After.',document_class='IEEEtran')
        self.assertEqual(r['versions'][0]['status'],'success');text=' '.join(n['content'] for n in r['elements'])
        for value in ('Alice','Research $x$.','After.'):self.assertIn(value,text)
        self.assertEqual(self._batch01_parse(r'\begin{IEEEbiographynophoto}{Alice}Text.\end{IEEEbiographynophoto}')['versions'][0]['status'],'parse_error')

    def test_batch04_uppercase_algorithmic_comments_are_content(self):
        r=self._batch01_parse(r'\begin{algorithmic}\STATE Work.\COMMENT{Why $x$ \cite{k}.}\FOR{each item}\COMMENT{Loop reason.}\STATE More.\ENDFOR\end{algorithmic}\begin{thebibliography}{1}\bibitem{k}Evidence.\end{thebibliography}',preamble=r'\usepackage{algorithmic}')
        self.assertEqual(r['versions'][0]['status'],'success');text=' '.join(n['content'] for n in r['elements'])
        for value in ('Work.','Why $x$',r'\cite{k}','Loop reason.','More.'):self.assertIn(value,text)
        self.assertEqual(self._batch01_parse(r'\COMMENT{Outside.}')['versions'][0]['status'],'parse_error')

    def test_batch04_repeat_until_preserves_body_then_condition(self):
        for upper in (False,True):
            repeat,until,state=('REPEAT','UNTIL','STATE') if upper else ('Repeat','Until','State')
            with self.subTest(upper=upper):
                r=self._batch01_parse('\\begin{algorithmic}\\'+repeat+'\\'+state+' Work $x$.\\'+until+r'{Ready $y$}\end{algorithmic}After.',preamble=r'\usepackage{algorithmic}' if upper else r'\usepackage{algpseudocode}')
                self.assertEqual(r['versions'][0]['status'],'success');text=' '.join(n['content'] for n in r['elements'])
                self.assertLess(text.index('Work $x$'),text.index('Ready $y$'));self.assertIn('After.',text)
                validate_tables(r)

    def test_batch04_repeat_unbalanced_or_unknown_content_is_not_ignored(self):
        for body in (r'\Until{Ready}',r'\Repeat\State Work.',r'\Repeat\State\unknown{Text}\Until{Ready}'):
            with self.subTest(body=body):self.assertEqual(self._batch01_parse(r'\begin{algorithmic}'+body+r'\end{algorithmic}',preamble=r'\usepackage{algpseudocode}')['versions'][0]['status'],'parse_error')

    def test_batch04_algorithm2e_repeat_uses_its_distinct_braced_syntax(self):
        r=self._batch01_parse(r'\begin{algorithm}\Repeat{Ready $y$}{Work $x$.}\end{algorithm}',preamble=r'\usepackage{algorithm2e}')
        self.assertEqual(r['versions'][0]['status'],'success');text=' '.join(n['content'] for n in r['elements'])
        self.assertLess(text.index('Work $x$'),text.index('Ready $y$'))


    def test_batch04_phantom_literal_is_hidden_but_following_text_is_not(self):
        for name in ('phantom','hphantom','vphantom'):
            with self.subTest(name=name):
                r=self._batch01_parse('Before \\'+name+'{Hidden sizing words} after.')
                self.assertEqual(r['versions'][0]['status'],'success');text=' '.join(n['content'] for n in r['elements'])
                self.assertIn('Before after.',text);self.assertNotIn('Hidden sizing words',text)
                self.assertTrue(any(m.get('hidden_body')=='Hidden sizing words' for m in r['versions'][0].get('layout_metadata',[])))

    def test_batch04_phantom_commands_are_not_executed_or_silently_lost(self):
        for body in (r'\phantom{\cite{k}}',r'\phantom{\unknown}',r'\phantom{\label{x}}',r'\phantom{Unclosed'):
            with self.subTest(body=body):self.assertEqual(self._batch01_parse(body)['versions'][0]['status'],'parse_error')

    def test_batch04_hypersetup_accepts_only_rendering_values(self):
        r=self._batch01_parse(r'\hypersetup{colorlinks=true,linkcolor=black,urlcolor=blue!50,}Visible \cite{k}.\begin{thebibliography}{1}\bibitem{k}Evidence.\end{thebibliography}',preamble=r'\usepackage{hyperref}')
        self.assertEqual(r['versions'][0]['status'],'success');self.assertIn(r'Visible \cite{k}.',' '.join(n['content'] for n in r['elements']))
        for options in (r'linkcolor=\unknown',r'unknown={Important}',r'pdfstartpage=\unknown'):
            with self.subTest(options=options):self.assertEqual(self._batch01_parse(r'\hypersetup{'+options+'}After.',preamble=r'\usepackage{hyperref}')['versions'][0]['status'],'parse_error')

    def test_batch04_ion_preserves_exact_source_without_guessing_stage(self):
        for cls,notation in [('aa',r'\ion{Fe}{ii}'),('aastex631',r'\ion{H}{1}')]:
            with self.subTest(cls=cls):
                r=self._batch01_parse(notation+' line.',document_class=cls)
                self.assertEqual(r['versions'][0]['status'],'success');self.assertIn(notation,' '.join(n['content'] for n in r['elements']))
        self.assertEqual(self._batch01_parse(r'\ion{H}{\stage}',document_class='aa')['versions'][0]['status'],'parse_error')

    def test_batch04_listing_named_style_keeps_exact_literal_and_title(self):
        pre=r'\usepackage{listings,xcolor}\lstdefinestyle{base}{basicstyle=\ttfamily\small,breaklines=true}\lstdefinestyle{prompt}{style=base,backgroundcolor=\color{gray!10},frame=single}\lstset{style=base}'
        r=self._batch01_parse('Before.\n'+r'\begin{lstlisting}[style=prompt,title={Actual title}]'+'\nx = {"a": 1}  # literal\n'+r'\end{lstlisting}After.',preamble=pre)
        self.assertEqual(r['versions'][0]['status'],'success')
        self.assertTrue(any('x = {"a": 1}  # literal' in n['content'] for n in r['elements']))
        self.assertIn('After.',' '.join(n['content'] for n in r['elements']))
        self.assertIn('Actual title',' '.join(n['content'] for n in r['elements']))

    def test_batch04_listing_style_unsafe_inheritance_and_cycles_fail(self):
        for declarations in (r'\lstdefinestyle{bad}{literate={x}{{y}}1}',r'\lstdefinestyle{bad}{escapeinside={(*}{*)}}',r'\lstdefinestyle{bad}{style=bad}',r'\lstdefinestyle{bad}{style=missing}'):
            with self.subTest(declarations=declarations):
                r=self._batch01_parse('\\begin{lstlisting}[style=bad]\nx\n\\end{lstlisting}',preamble=r'\usepackage{listings}'+declarations)
                self.assertEqual(r['versions'][0]['status'],'parse_error')

    def test_batch04_listing_source_language_is_not_assumed_literal(self):
        r=self._batch01_parse('\\begin{lstlisting}[language=custom]\nx\n\\end{lstlisting}',preamble=r'\usepackage{listings}\lstdefinelanguage{custom}{literate={x}{{y}}1}')
        self.assertEqual(r['versions'][0]['status'],'parse_error');self.assertIn('Source-defined listing language',r['versions'][0]['error'])

    def test_batch04_root_package_in_preamble_fragment_proves_subfile_relation(self):
        r=self.parse({'main.tex':r'\documentclass{article}\input{setup}\begin{document}Main.\subfile{part}After.\end{document}',
                      'setup.tex':r'\usepackage{subfiles}',
                      'part.tex':r'\documentclass[main]{subfiles}\begin{document}Included.\end{document}'})
        self.assertEqual(r['versions'][0]['status'],'success');self.assertEqual(r['versions'][0]['root'],'main.tex')
        text=' '.join(n['content'] for n in r['elements']);self.assertLess(text.index('Main.'),text.index('Included.'));self.assertLess(text.index('Included.'),text.index('After.'))

    def test_batch04_root_uncertain_preamble_does_not_prove_package(self):
        for setup in (r'\ifdefined\unknown\usepackage{subfiles}\fi',r'\input{setup}',r'\input{../outside}',r'\newcommand{\usepackage}[1]{}\usepackage{subfiles}'):
            with self.subTest(setup=setup),tempfile.TemporaryDirectory() as td:
                d=Path(td)
                for name,body in {'main.tex':r'\documentclass{article}\input{setup}\begin{document}\subfile{part}\end{document}','setup.tex':setup,'part.tex':r'\documentclass[main]{subfiles}\begin{document}Part.\end{document}'}.items():(d/name).write_text(body)
                r=parse_versions(AID,[(1,d)]);self.assertEqual(r['versions'][0]['status'],'parse_error');self.assertIn('found 2',r['versions'][0]['error'])

    def test_batch04_IEEE_pdf_rendering_equivalence_does_not_guess_engine(self):
        r=self._batch01_parse(r'\section{Intro}Visible $x$.',document_class='IEEEtran',preamble=r'\ifCLASSINFOpdf\usepackage[pdftex]{graphicx}\else\usepackage[dvips]{graphicx}\fi')
        self.assertEqual(r['versions'][0]['status'],'success');self.assertIn('Visible $x$.',' '.join(n['content'] for n in r['elements']))
        r=self._batch01_parse(r'\ifCLASSINFOpdf First.\else Second.\fi',document_class='IEEEtran')
        self.assertEqual(r['versions'][0]['status'],'parse_error')


    def test_batch04_repeat_local_redefinition_is_not_a_builtin_loop(self):
        r=self._batch01_parse(r'\begin{algorithmic}\Repeat\State Work.\Until{Done}\end{algorithmic}',preamble=r'\usepackage{algpseudocode}\newcommand{\Repeat}{Different}')
        self.assertEqual(r['versions'][0]['status'],'parse_error');self.assertIn('Locally redefined',r['versions'][0]['error'])

    def test_batch04_new_handlers_respect_local_macros(self):
        for name in ('parbox','textcircled','text','phantom','hypersetup','ion','IEEEPARstart'):
            with self.subTest(name=name):
                r=self._batch01_parse('\\'+name+'{Important}.',preamble='\\newcommand{\\'+name+'}[1]{Actual #1}')
                self.assertEqual(r['versions'][0]['status'],'success');self.assertIn('Actual Important.',' '.join(n['content'] for n in r['elements']))


    def test_batch04_listing_repeated_style_keys_keep_unsafe_prior_effects(self):
        pre=r'\usepackage{listings}\lstdefinestyle{unsafe}{escapeinside={(*}{*)}}\lstdefinestyle{visual}{frame=single}'
        r=self._batch01_parse('\\begin{lstlisting}[style=unsafe,style=visual]\nx\n\\end{lstlisting}',preamble=pre)
        self.assertEqual(r['versions'][0]['status'],'parse_error');self.assertIn('escapeinside',r['versions'][0]['error'])

    def test_batch04_IEEE_projection_redefined_package_command_stays_error(self):
        r=self._batch01_parse('After.',document_class='IEEEtran',preamble=r'\newcommand{\usepackage}[2][]{Important.}\ifCLASSINFOpdf\usepackage[pdftex]{graphicx}\else\usepackage[dvips]{graphicx}\fi')
        self.assertEqual(r['versions'][0]['status'],'parse_error');self.assertIn('Locally redefined',r['versions'][0]['error'])


    def test_final_review_ifnum_unterminated_numeric_expansion_is_not_a_branch(self):
        cases=[(r'\def\two{2}',r'\ifnum1=1\two TrueBranch.\else FalseBranch.\fi'),
               (r'\def\one{1}',r'\ifnum1=\one 2 TrueBranch.\else FalseBranch.\fi'),
               (r'\def\one{1}\def\two{2}',r'\ifnum1=\one \two TrueBranch.\else FalseBranch.\fi')]
        for pre,body in cases:
            with self.subTest(body=body):
                r=self._batch01_parse(body,preamble=pre)
                self.assertEqual(r['versions'][0]['status'],'parse_error')
                self.assertIn('Unterminated expandable ifnum operand',r['versions'][0]['error'])
                self.assertEqual(r['elements'],[])

    def test_final_review_ifnum_explicit_and_actual_space_terminators(self):
        cases=[(r'\def\one{1}\def\two{2}',r'\ifnum1=\one\relax \two Correct.\else Wrong.\fi'),
               (r'\def\two{2}',r'\ifnum1=1 \two Correct.\else Wrong.\fi'),
               (r'\def\one{1 }\def\two{2}',r'\ifnum1=\one\two Correct.\else Wrong.\fi')]
        for pre,body in cases:
            with self.subTest(body=body):
                r=self._batch01_parse(body,preamble=pre);self.assertEqual(r['versions'][0]['status'],'success')
                content=' '.join(n['content'] for n in r['elements']);self.assertIn('2 Correct.',content);self.assertNotIn('Wrong.',content)
        r=self._batch01_parse(r'\ifnum1=1\relax Correct.\else Wrong.\fi')
        self.assertEqual(r['versions'][0]['status'],'success');self.assertIn('Correct.',' '.join(n['content'] for n in r['elements']))

    def test_final_review_listing_custom_language_dialect_and_case_remain_unsupported(self):
        for language in ('custom','CUSTOM','[dialect]Custom','[DIALECT]CUSTOM'):
            with self.subTest(language=language):
                r=self._batch01_parse('\\begin{lstlisting}[language={'+language+'}]\nx\n\\end{lstlisting}',preamble=r'\usepackage{listings}\lstdefinelanguage{Custom}{literate={x}{{REPLACED}}1}')
                self.assertEqual(r['versions'][0]['status'],'parse_error');self.assertIn('Source-defined listing language',r['versions'][0]['error'])

    def test_final_review_listing_style_total_work_is_bounded(self):
        from src.m2.latex import resolve_listing_options
        styles={'level0':r'basicstyle=\small'}
        for level in range(1,6):styles['level'+str(level)]=','.join(['style=level'+str(level-1)]*5)
        with self.assertRaisesRegex(ParseError,'style expansion limit'):resolve_listing_options('style=level5',styles)
        self.assertEqual(resolve_listing_options('style=level1',styles),{'basicstyle':r'\small'})

    def test_final_review_root_role_words_in_research_title_are_not_supplements(self):
        for title in ('Supplementary Material Retrieval with Neural Models','Supplementary Document Classification for Scientific Search','A Survey of Supplemental Material Retrieval','Supporting Information Extraction in Scientific Articles'):
            for display in (False,True):
                with self.subTest(title=title,display=display),tempfile.TemporaryDirectory() as td:
                    d=Path(td);(d/'main.tex').write_text(tex(r'\section{Introduction}Main independent content.'))
                    header=(r'\begin{document}\Large{'+title+'}') if display else (r'\title{'+title+r'}\begin{document}')
                    (d/'other.tex').write_text(r'\documentclass{article}'+header+r'\section{Introduction}Second independent article.\end{document}')
                    r=parse_versions(AID,[(1,d)]);self.assertEqual(r['versions'][0]['status'],'parse_error');self.assertIn('found 2',r['versions'][0]['error'])

    def test_final_review_explicit_supplement_labels_still_select_main(self):
        for title in ('Supplementary Material','Supplementary Material for A Main Study','A Main Study: Supplementary Information','Supplementary Document for A Main Study'):
            with self.subTest(title=title),tempfile.TemporaryDirectory() as td:
                d=Path(td);(d/'main.tex').write_text(tex('Main independent content.'))
                (d/'other.tex').write_text(r'\documentclass{article}\title{'+title+r'}\begin{document}Separate supplement.\end{document}')
                r=parse_versions(AID,[(1,d)]);self.assertEqual(r['versions'][0]['status'],'success');self.assertEqual(r['versions'][0]['root'],'main.tex')


    def test_final_review_role_phrase_inside_unknown_title_macro_is_not_evidence(self):
        r=self.parse({'a.tex':tex('First independent article.'),'b.tex':r'\documentclass{article}\newcommand{\mask}[1]{Independent research title}\title{\mask{X: Supplementary Information}}\begin{document}Second independent article.\end{document}'})
        self.assertEqual(r['versions'][0]['status'],'parse_error');self.assertIn('found 2',r['versions'][0]['error'])

    def test_final_review_two_returns_keep_statement_order_values_and_citations(self):
        body=r'\begin{algorithmic}\STATE Compute $x$.\RETURN $y$ \cite{k}.\end{algorithmic}\section{Second}\begin{algorithmic}\STATE Compute $z$.\RETURN $w$ \cite{k}.\end{algorithmic}After.\begin{thebibliography}{1}\bibitem{k}Evidence.\end{thebibliography}'
        r=self._batch01_parse(body,preamble=r'\usepackage{algorithmic}')
        self.assertEqual(r['versions'][0]['status'],'success');validate_tables(r)
        text=' '.join(n['content'] for n in r['elements'])
        self.assertLess(text.index('Compute $x$'),text.index('return $y$'));self.assertLess(text.index('return $y$'),text.index('Second'));self.assertLess(text.index('Compute $z$'),text.index('return $w$'))
        returns=[n for n in r['elements'] if n['kind']=='sentence' and n['content'].startswith('return ')]
        self.assertEqual(len(returns),2);self.assertTrue(all(r'\cite{k}' in n['content'] for n in returns));self.assertEqual(r['bibtex'][0]['citation_key'],'k')
        nodes={n['element_id']:n for n in r['elements']};parents={e['element_id']:e['parent_id'] for e in r['hierarchy']}
        for node in returns:self.assertEqual(nodes[parents[node['element_id']]]['content'],'algorithmic')


    def test_post_batch04_bibitems_in_literal_groups_keep_keys_and_order(self):
        body = (r"Before \cite{a,b,c}. \begin{thebibliography}{3}"
                r"{\small\bibitem{a}Alice.\newblock First.\newblock 2020.}"
                r"\bibitem{b}{Bob.\newblock Second.\newblock 2021."
                r"\bibitem{c}Carol.\newblock Third.\newblock 2022.}"
                r"\end{thebibliography}After.")
        result = self.parse({"main.tex": tex(body)})
        self.assertEqual(result["versions"][0]["status"], "success", result["versions"])
        self.assertEqual([r["citation_key"] for r in result["bibtex"]], ["a", "b", "c"])
        text = " ".join(n["content"] for n in result["elements"])
        self.assertIn(r"\cite{a,b,c}", text)
        self.assertIn("After.", text)
        self.assertNotIn("bibitem", text)
        self.assertNotIn("First.", text)
        validate_tables(result)
        self.assertTrue(all(e["version"] == 1 for e in result["hierarchy"]))

    def test_post_batch04_bibitem_group_after_href_is_not_an_extra_href_argument(self):
        body = (r"See \cite{b}.\begin{thebibliography}{2}"
                r"\bibitem{a}Alice. \href{https://example.test}{A.}"
                r"{\bibitem{b}Bob. 2021.}\end{thebibliography}")
        result = self.parse({"main.tex": tex(body)})
        self.assertEqual(result["versions"][0]["status"], "success", result["versions"])
        self.assertEqual([r["citation_key"] for r in result["bibtex"]], ["a", "b"])
        self.assertNotIn("Bob.", result["versions"][0]["bibliography_evidence"][0]["raw"])

    def test_post_batch04_proved_color_bibliography_wrapper_keeps_citation(self):
        for definition, invocation in (
            (r"\newcommand{\markref}[1]{\textcolor{black}{#1}}", r"\markref"),
            (r"\newcommand{\markref}[1]{{\color{black}{#1}}}", r"\markref"),
            ("", r"\textbf"),
        ):
            with self.subTest(definition=definition), tempfile.TemporaryDirectory() as td:
                d = Path(td)
                (d/"main.tex").write_text(r"\documentclass{article}\usepackage{xcolor}"+definition+
                    r"\begin{document}See \cite{k}.\begin{thebibliography}{1}"+
                    invocation+r"{\bibitem{k}Author.\newblock Title.\newblock 2020.}"
                    r"\end{thebibliography}After.\end{document}", encoding="utf8")
                result = parse_versions(AID, [(1,d)])
                self.assertEqual(result["versions"][0]["status"], "success", result["versions"])
                self.assertEqual(result["bibtex"][0]["citation_key"], "k")
                self.assertEqual(result["bibtex"][0]["title"], "Title")

    def test_post_batch04_unknown_bibliography_macro_cannot_reveal_a_hidden_reference(self):
        for declaration in (r"\newcommand{\discard}[1]{}", r"\newcommand{\discard}[1]{Not #1 evidence.}"):
            with self.subTest(declaration=declaration), tempfile.TemporaryDirectory() as td:
                d=Path(td)
                (d/"main.tex").write_text(r"\documentclass{article}"+declaration+
                    r"\begin{document}See \cite{hidden}.\begin{thebibliography}{1}"
                    r"\discard{\bibitem{hidden}Not executed.}\end{thebibliography}\end{document}",encoding="utf8")
                result=parse_versions(AID,[(1,d)])
                self.assertEqual(result["versions"][0]["status"],"parse_error")
                self.assertIn("unavailable",result["versions"][0]["error"])

    def test_post_batch04_bibliography_marker_redefinition_remains_blocked(self):
        result=self.parse({"main.tex":tex(r"See \cite{k}.\begin{thebibliography}{1}"
            r"\def\bibitem#1{Suppressed.}\bibitem{k}Not evidence.\end{thebibliography}")})
        self.assertEqual(result["versions"][0]["status"],"parse_error")
        self.assertIn("scoped TeX semantics",result["versions"][0]["error"])
        valid=self._batch01_parse(r"See \cite{k}.\begin{thebibliography}{1}"
            r"\providecommand{\noop}[1]{#1}\bibitem{k}Source.\end{thebibliography}")
        self.assertEqual(valid["versions"][0]["status"],"success",valid["versions"])

    def test_post_batch04_return_without_value_preserves_control_statement(self):
        result=self._batch01_parse(r"\begin{algorithmic}\State Work."
            r"\State \Return\State Continue.\Return $x$\end{algorithmic}After.",
            preamble=r"\usepackage{algpseudocode}")
        self.assertEqual(result["versions"][0]["status"],"success",result["versions"])
        contents=[e["content"] for e in result["elements"] if e["kind"]=="sentence"]
        self.assertIn("return",contents)
        self.assertLess(contents.index("return"),contents.index("Continue."))
        self.assertTrue(any("return $x$" in text for text in contents))
        validate_tables(result)

    def test_post_batch04_boxsep_keeps_box_title_body_and_citation(self):
        result=self._batch01_parse(r"\begin{tcolorbox}[title=Evidence]Preserved \cite{k}."
            r"\end{tcolorbox}After.\begin{thebibliography}{1}\bibitem{k}Source.\end{thebibliography}",
            preamble=r"\usepackage{tcolorbox}\tcbset{boxsep=4pt}")
        self.assertEqual(result["versions"][0]["status"],"success",result["versions"])
        text=" ".join(e["content"] for e in result["elements"])
        self.assertIn("Evidence",text);self.assertIn(r"\cite{k}",text);self.assertIn("After.",text)
        bad=self._batch01_parse(r"\begin{tcolorbox}Body.\end{tcolorbox}",
            preamble=r"\usepackage{tcolorbox}\tcbset{boxsep=\unknown}")
        self.assertEqual(bad["versions"][0]["status"],"parse_error")

    def test_post_batch04_theorem_numberlike_and_balanced_reference_names(self):
        result=self._batch01_parse(r"\begin{theorem}First.\end{theorem}"
            r"\begin{corollary}Second \cite{k}.\end{corollary}"
            r"\begin{thebibliography}{1}\bibitem{k}Source.\end{thebibliography}",
            preamble=r"\usepackage{thmtools}\declaretheorem[refname={theorem,theorems},"
            r"Refname={Theorem,Theorems}]{theorem}\declaretheorem[numberlike=theorem]{corollary}")
        self.assertEqual(result["versions"][0]["status"],"success",result["versions"])
        text=" ".join(e["content"] for e in result["elements"])
        self.assertIn("First.",text);self.assertIn("Second",text);self.assertIn(r"\cite{k}",text)
        bad=self._batch01_parse(r"\begin{thm}Body.\end{thm}",preamble=
            r"\usepackage{thmtools}\declaretheorem[refname={\unknown,theorems}]{thm}")
        self.assertEqual(bad["versions"][0]["status"],"parse_error")

    def test_post_batch04_explicit_supplement_typography_is_not_root_ambiguity(self):
        titles=(r"A Main Study: (Supplementary Information)",
                r"A Main Study\\ - Supplemental Material -",
                r"{{\Large{Supplementary Information:}}} \\ Main Study",
                r"Supplemental Material to \\ Main $x_\text{2}$",
                r"Supplemental Material to \texorpdfstring{\\}{} Main Study",
                r"Supplementary Information \ \\ \ \\ Main Study",
                "Supplementary information supporting the article Main Study")
        for title in titles:
            with self.subTest(title=title),tempfile.TemporaryDirectory() as td:
                d=Path(td);(d/"main.tex").write_text(tex("Main content."))
                (d/"supp.tex").write_text(r"\documentclass{article}\title{"+title+
                    r"}\begin{document}Separate supplement.\end{document}")
                result=parse_versions(AID,[(1,d)])
                self.assertEqual(result["versions"][0]["status"],"success",result["versions"])
                self.assertEqual(result["versions"][0]["root"],"main.tex")
                self.assertNotIn("Separate supplement."," ".join(n["content"] for n in result["elements"]))


    def test_post_batch04_inline_bbl_color_spacing_wrapper_does_not_merge_titles(self):
        source = (r"\documentclass{article}\usepackage{xcolor}"
            r"\newcommand{\padref}{\hspace*}"
            r"\newcommand{\reviewref}[1]{\textcolor{black}{#1}\padref{-1mm}}"
            r"\begin{document}See \cite{a,b}.\begin{thebibliography}{2}"
            r"\expandafter\ifx\csname natexlab\endcsname\relax\def\natexlab#1{#1}\fi"
            r"\bibitem{a}\bibinfo{title}{First title}\bibinfo{year}{2020}."
            r"\reviewref{\bibitem{b}\bibinfo{title}{Second title}\bibinfo{year}{2021}.}"
            r"\end{thebibliography}After.\end{document}")
        result=self.parse({"main.tex":source})
        self.assertEqual(result["versions"][0]["status"],"success",result["versions"])
        self.assertEqual([(b["citation_key"],b["title"]) for b in result["bibtex"]],
                         [("a","First title"),("b","Second title")])
        self.assertIn("After."," ".join(n["content"] for n in result["elements"]))
        validate_tables(result)

    def test_post_batch04_unknown_spacing_tail_is_not_a_transparent_bbl_wrapper(self):
        result=self.parse({"main.tex":r"\documentclass{article}\usepackage{xcolor}"
            r"\newcommand{\reviewref}[1]{\textcolor{black}{#1}\unknown{-1mm}}"
            r"\begin{document}See \cite{k}.\begin{thebibliography}{1}"
            r"\reviewref{\bibitem{k}Not proved.}\end{thebibliography}\end{document}"})
        self.assertEqual(result["versions"][0]["status"],"parse_error")


    def test_batch05_acknowledgements_revtex_declaration_retains_following_text(self):
        r=self._batch01_parse(r"\section{Results}Science.\acknowledgements Thanks \cite{k}."
            r"\section{Appendix}More.\begin{thebibliography}{1}\bibitem{k}Source.\end{thebibliography}",
            document_class="revtex4-2")
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        text=" ".join(n["content"] for n in r["elements"])
        for word in ("Science.","Thanks","Appendix","More.",r"\cite{k}"):self.assertIn(word,text)
        nodes={n["element_id"]:n for n in r["elements"]};ack=next(n for n in r["elements"] if n["content"]=="acknowledgments")
        self.assertTrue(any(e["parent_id"]==ack["element_id"] and "Thanks" in nodes[e["element_id"]]["content"] for e in r["hierarchy"]))
        self.assertEqual(self._batch01_parse(r"\acknowledgements Text.")["versions"][0]["status"],"parse_error")

    def test_batch05_theoremstyle_literal_changes_no_theorem_body(self):
        r=self._batch01_parse(r"\theoremstyle{remark}\begin{thm}Important $x=y$.\end{thm}After.",
            preamble=r"\usepackage{amsthm}\newtheorem{thm}{Theorem}")
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        self.assertIn("Important $x=y$"," ".join(n["content"] for n in r["elements"]))
        for body in (r"\theoremstyle{\unknown}After.",r"\theoremstyle{custom}After."):
            self.assertEqual(self._batch01_parse(body,preamble=r"\usepackage{amsthm}")["versions"][0]["status"],"parse_error")

    def test_batch05_acmart_topmatter_validated_and_content_preserved(self):
        r=self._batch01_parse(r"\settopmatter{authorsperrow=4,printacmref=false}"
            r"\begin{abstract}Abstract retained.\end{abstract}\section{Intro}Body.",document_class="acmart")
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        self.assertIn("Body."," ".join(n["content"] for n in r["elements"]))
        for setting in ("hook={Lost.}","authorsperrow=\\unknown","printacmref=maybe"):
            bad=self._batch01_parse(r"\settopmatter{"+setting+"}Body.",document_class="acmart")
            self.assertEqual(bad["versions"][0]["status"],"parse_error")

    def test_batch05_aastex_altaffiliation_retains_attribution_and_abstract(self):
        r=self._batch01_parse(r"\author{Alice}\altaffiliation{Fellow}"
            r"\begin{abstract}Abstract retained.\end{abstract}Body.",document_class="aastex631")
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        self.assertTrue(any(x.get("command")=="altaffiliation" and x.get("arguments")==["Fellow"]
            for x in r["versions"][0]["layout_metadata"]))
        self.assertIn("Abstract retained."," ".join(n["content"] for n in r["elements"]))

    def test_batch05_ieee_pubid_retains_notice_and_does_not_duplicate_abstract(self):
        r=self._batch01_parse(r"\IEEEpubid{2025 \copyright IEEE}"
            r"\IEEEtitleabstractindextext{\begin{abstract}Unique abstract.\end{abstract}}"
            r"\IEEEdisplaynontitleabstractindextext\IEEEpubidadjcol\section{Intro}Body.",document_class="IEEEtran")
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        text=" ".join(n["content"] for n in r["elements"])
        self.assertIn("2025",text);self.assertIn("\u00a9",text);self.assertEqual(text.count("Unique abstract."),1)
        bad=self._batch01_parse(r"\IEEEdisplaynontitleabstractindextext\IEEEdisplaynontitleabstractindextext",document_class="IEEEtran")
        self.assertEqual(bad["versions"][0]["status"],"parse_error")

    def test_batch05_algorithm2e_semicolon_controls_keep_statements(self):
        r=self._batch01_parse(r"\begin{algorithm}\DontPrintSemicolon\SetNoFillComment"
            r"\KwIn{Input.}Compute $x$.\;\KwOut{Output.}\end{algorithm}After.",
            preamble=r"\usepackage{algorithm2e}")
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        text=" ".join(n["content"] for n in r["elements"])
        for word in ("Input.","Compute $x$","Output.","After."):self.assertIn(word,text)
        self.assertEqual(self._batch01_parse(r"\DontPrintSemicolon Body.")["versions"][0]["status"],"parse_error")

    def test_batch05_dirtytalk_quote_preserves_content_math_and_citation(self):
        r=self._batch01_parse(r"A \say{quote $x=y$ \cite{k}} after."
            r"\begin{thebibliography}{1}\bibitem{k}Source.\end{thebibliography}",preamble=r"\usepackage{dirtytalk}")
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        text=" ".join(n["content"] for n in r["elements"])
        self.assertIn("\u201cquote",text);self.assertIn(r"$x=y$ \cite{k}"+'\u201d',text)
        self.assertEqual(self._batch01_parse(r"\say{Words.}")["versions"][0]["status"],"parse_error")

    def test_batch05_literal_layout_glue_and_padding_do_not_consume_text(self):
        r=self._batch01_parse(r"\setlength{\intextsep}{-1pt}"
            r"\setlength{\belowcaptionskip}{-3ex plus .5ex minus .5ex}After $x=y$.")
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        self.assertIn("After $x=y$."," ".join(n["content"] for n in r["elements"]))
        for value in (r"\unknown","1pt plus \\unknown","1pt ignored science"):
            self.assertEqual(self._batch01_parse(r"\setlength{\intextsep}{"+value+"}After.")["versions"][0]["status"],"parse_error")

    def test_batch05_listing_pure_padding_keeps_literal_code_and_caption(self):
        body=r"\begin{lstlisting}[lineskip=-1pt,xrightmargin=0pt,upquote=true,caption={Code}]"+ "\nx = 'a';\n"+r"\end{lstlisting}After."
        r=self._batch01_parse(body,preamble=r"\usepackage{listings}")
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        self.assertTrue(any(n["kind"]=="verbatim" and "x = 'a';" in n["content"] for n in r["elements"]))
        self.assertEqual(self._batch01_parse(body.replace("lineskip=-1pt",r"lineskip=\unknown"),preamble=r"\usepackage{listings}")["versions"][0]["status"],"parse_error")

    def test_batch05_new_builtins_do_not_override_local_content_macros(self):
        for name,pkg,cls in (("say","dirtytalk","article"),("DontPrintSemicolon","algorithm2e","article"),
                             ("theoremstyle","amsthm","article"),("settopmatter","","acmart")):
            body="\\"+name+r"{Visible argument.}After."
            preamble=(r"\usepackage{"+pkg+"}" if pkg else "")+r"\newcommand{\ "+name+r"}[1]{Local #1}"
            preamble=preamble.replace(r"\ ","\\")
            r=self._batch01_parse(body,preamble=preamble,document_class=cls)
            self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
            self.assertIn("Local Visible argument."," ".join(n["content"] for n in r["elements"]))


    def test_batch05_natbib_redundant_key_groups_preserve_targets_and_notes(self):
        r=self._batch01_parse(r"\citep[see][p. 2]{{a},b} \cite{{a}}."
            r"\begin{thebibliography}{2}\bibitem{a}First.\bibitem{b}Second.\end{thebibliography}",
            preamble=r"\usepackage{natbib}")
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        self.assertEqual({b["citation_key"] for b in r["bibtex"]},{"a","b"})
        text=" ".join(n["content"] for n in r["elements"])
        self.assertIn(r"\citep[see][p. 2]{a,b}",text);self.assertNotIn("{{a}}",text)
        for bad in (r"\cite{{a,b}}",r"\cite{{\macro}}",r"\cite{}"):
            x=self._batch01_parse(bad,preamble=r"\usepackage{natbib}")
            self.assertEqual(x["versions"][0]["status"],"parse_error")
        x=self._batch01_parse(r"\cite{{a}}\begin{thebibliography}{1}\bibitem{a}Source.\end{thebibliography}")
        self.assertEqual(x["versions"][0]["status"],"parse_error")

    def test_batch05_declared_latin1_with_whitespace_decodes_without_guessing(self):
        source=(r"\documentclass{article}\usepackage [ latin1 ] { inputenc }"
            r"\begin{document}"+ "Caf\u00e9."+r"\end{document}").encode("latin1")
        r=self.parse({"main.tex":source})
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        self.assertIn("Caf\u00e9."," ".join(n["content"] for n in r["elements"]))
        self.assertEqual(r["versions"][0]["encodings"]["main.tex"],"latin-1")

    def test_batch05_control_space_at_end_of_figure_is_not_a_dangling_slash(self):
        source=tex(r"\begin{figure}\caption{Keep caption \cite{k}.}\label{f}"+"\\\n"+
            r"\end{figure}After.\begin{thebibliography}{1}\bibitem{k}Source.\end{thebibliography}")
        r=self.parse({"main.tex":source})
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        figure=next(n for n in r["elements"] if n["kind"]=="figure")
        self.assertIn(r"\cite{k}",figure["content"]);self.assertFalse(figure["content"].endswith("\\"))
        self.assertIn(r"\\",normalize(r"First\\ Second"))
        self.assertEqual(normalize("First\\\nSecond"),r"First\ Second")
        self.assertEqual(normalize(r"$a\ b$"),r"$a\ b$")

    def test_batch05_unbraced_setlength_register_is_shared_with_list_prefix(self):
        r=self._batch01_parse(r"\setlength\intextsep{-1pt}\begin{enumerate}"
            r"\setlength\itemsep{1pt}\setlength{\partopsep}{0pt}"
            r"\item First \cite{k}.\item Second.\end{enumerate}After."
            r"\begin{thebibliography}{1}\bibitem{k}Source.\end{thebibliography}")
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        text=" ".join(n["content"] for n in r["elements"])
        self.assertIn("First",text);self.assertIn("Second.",text);self.assertIn("After.",text)
        self.assertEqual(self._batch01_parse(r"\setlength\unknown{1pt}After.")["versions"][0]["status"],"parse_error")

    def test_batch05_algorithmic_spacing_prefix_preserves_first_statement(self):
        r=self._batch01_parse(r"\begin{algorithmic}\medskip\STATE First $x=y$."
            r"\medskip\STATE Second.\end{algorithmic}After.",preamble=r"\usepackage{algorithmic}")
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        text=" ".join(n["content"] for n in r["elements"])
        self.assertIn("First $x=y$.",text);self.assertIn("Second.",text);self.assertIn("After.",text)

    def test_batch05_ieee_bst_control_is_proved_by_entry_type_and_not_exported(self):
        body=r"\bstctlcite{config}\section{Intro}See \cite{paper}.\bibliography{refs}"
        data="@IEEEtranBSTCTL{config,CTLmax_names_forced_etal={6}}\n@article{paper,title={Real paper},year={2025}}"
        r=self._batch01_parse(body,document_class="IEEEtran",files={"refs.bib":data})
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        self.assertEqual([b["citation_key"] for b in r["bibtex"]],["paper"])
        self.assertTrue(any(n["content"]=="Intro" for n in r["elements"]))
        for bad in ("@article{config,title={Not a control record}}","@IEEEtranBSTCTL{other,CTLuse_forced_etal={yes}}"):
            x=self._batch01_parse(body,document_class="IEEEtran",files={"refs.bib":bad})
            self.assertEqual(x["versions"][0]["status"],"parse_error")


    def test_batch05_natbib_projection_does_not_parse_bbl_citenamefont_as_a_citation(self):
        r=self._batch01_parse(r"\cite{k}.\begin{thebibliography}{1}"
            r"\expandafter\ifx\csname citenamefont\endcsname\relax\def\citenamefont#1{#1}\fi"
            r"\bibitem[{\citenamefont{Alice}(2020)}]{k}Alice. 2020."
            r"\end{thebibliography}After.",preamble=r"\usepackage{natbib}")
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        self.assertEqual(r["bibtex"][0]["citation_key"],"k")
        self.assertIn("After."," ".join(n["content"] for n in r["elements"]))

    def test_batch05_grouped_natbib_manual_key_retains_literal_space(self):
        r=self._batch01_parse(r"See \cite{{Di Ventra}}.\begin{thebibliography}{1}"
            r"\bibitem{Di Ventra}Source.\end{thebibliography}",document_class="revtex4-1")
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        self.assertEqual(r["bibtex"][0]["citation_key"],"Di Ventra")
        self.assertIn(r"\cite{Di Ventra}"," ".join(n["content"] for n in r["elements"]))


    def test_batch05_ieee_notice_replaces_only_exact_nonliteral_copyright_token(self):
        literal=self._batch01_parse(r"\IEEEpubid{\verb|\copyright|}After.",document_class="IEEEtran")
        self.assertEqual(literal["versions"][0]["status"],"success",literal["versions"])
        self.assertIn(r"\verb|\copyright|"," ".join(n["content"] for n in literal["elements"]))
        unknown=self._batch01_parse(r"\IEEEpubid{\copyrightextra}After.",document_class="IEEEtran")
        self.assertEqual(unknown["versions"][0]["status"],"parse_error")
        self.assertIn("copyrightextra",unknown["versions"][0]["error"])


    def test_batch05_natbib_projection_does_not_rewrite_inline_literal_citations(self):
        r=self._batch01_parse(r"\verb|\cite{{not_a_reference}}| After.",preamble=r"\usepackage{natbib}")
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        self.assertIn(r"\verb|\cite{{not_a_reference}}|"," ".join(n["content"] for n in r["elements"]))
        self.assertEqual(r["bibtex"],[])


    def test_batch06_ieee_initial_accepts_literal_digit_and_one_token_continuation(self):
        for source,word in ((r"\IEEEPARstart{V}ision continues.","Vision"),(r"\IEEEPARstart{3}{D} science.","3D")):
            r=self._batch01_parse(source,document_class="IEEEtran")
            self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
            self.assertIn(word," ".join(n["content"] for n in r["elements"]))
        self.assertEqual(self._batch01_parse(r"\IEEEPARstart{\unknown}{Rest}",document_class="IEEEtran")["versions"][0]["status"],"parse_error")

    def test_batch06_ieee_raised_section_keeps_following_content_under_heading(self):
        r=self._batch01_parse(r"\IEEEraisesectionheading{\section{Intro}\label{s}}First $x=y$."
            r"\section{Next}After.",document_class="IEEEtran")
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        nodes={n["element_id"]:n for n in r["elements"]};heading=next(n for n in r["elements"] if n["content"]=="Intro")
        self.assertTrue(any(e["parent_id"]==heading["element_id"] and "First" in nodes[e["element_id"]]["content"] for e in r["hierarchy"]))
        self.assertEqual(self._batch01_parse(r"\IEEEraisesectionheading{No section.}",document_class="IEEEtran")["versions"][0]["status"],"parse_error")

    def test_batch06_nolinenumbers_environment_retains_abstract_and_citation(self):
        body=r"\begin{abstract}\begin{nolinenumbers}Keep $x$ \cite{k}.\end{nolinenumbers}\end{abstract}After." \
             r"\begin{thebibliography}{1}\bibitem{k}Source.\end{thebibliography}"
        r=self._batch01_parse(body,document_class="aastex631")
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        self.assertIn(r"Keep $x$ \cite{k}."," ".join(n["content"] for n in r["elements"]))
        self.assertEqual(self._batch01_parse(body)["versions"][0]["status"],"parse_error")

    def test_batch06_aastex_dataset_accepts_url_and_doi_keeps_label(self):
        for target in ("https://doi.org/10.17909/example","10.17909/example"):
            r=self._batch01_parse(r"Data \dataset["+target+r"]{Source label}.After.",document_class="aastex631")
            self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
            self.assertIn(r"\href{https://doi.org/10.17909/example}{Source label}"," ".join(n["content"] for n in r["elements"]))
        self.assertEqual(self._batch01_parse(r"\dataset[\unknown]{Visible}",document_class="aastex631")["versions"][0]["status"],"parse_error")

    def test_batch06_aastex_fractional_angle_notation_is_preserved(self):
        r=self._batch01_parse(r"Between 2\farcs2 and 3\farcm5, at 4\fdg1. After.",document_class="aastex631")
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        text=" ".join(n["content"] for n in r["elements"])
        self.assertIn(r"2\farcs{}2",text);self.assertIn(r"3\farcm{}5",text)
        self.assertEqual(self._batch01_parse(r"2\farcs2")["versions"][0]["status"],"parse_error")

    def test_batch06_cleveref_names_keep_heading_and_formula(self):
        r=self._batch01_parse(r"\Crefname{criterion}{Criterion}{Criteria}\crefname{criterion}{Crit.}{Crit.}"
            r"\section{Intro}Keep $x=y$ and \cref{s}.",preamble=r"\usepackage{cleveref}")
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        self.assertIn(r"Keep $x=y$"," ".join(n["content"] for n in r["elements"]))
        self.assertEqual(self._batch01_parse(r"\Crefname{x}{\unknown}{Plural}",preamble=r"\usepackage{cleveref}")["versions"][0]["status"],"parse_error")

    def test_batch06_control_space_crlf_preserves_following_word(self):
        r=self._batch01_parse("First\\\r\nsecond.")
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        self.assertIn("First second."," ".join(n["content"] for n in r["elements"]))

    def test_batch06_text_tilde_and_empty_circumflex_keep_literal_symbols(self):
        r=self._batch01_parse(r"Path a\texttildelow b and SET557\^{}1.p. After.")
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        text=" ".join(n["content"] for n in r["elements"])
        self.assertIn(r"\textasciitilde{}",text);self.assertIn(r"\textasciicircum{}1.p",text)

    def test_batch06_algorithm_comments_ranges_and_returns_keep_scientific_text(self):
        body=r"\begin{algorithm}\DontPrintSemicolon\tcp*[h]{Outer \cite{k}.}"
        body+=r"\For{$i=1$ \KwTo $n$}{Work $x=y$.\;}"
        body+=r"\While(\tcp*[f]{Test ) literal.}){$x>0$}{Reduce.\;}"
        body+=r"\Return{} First output.\;\Return{Second output.}\end{algorithm}After."
        body+=r"\begin{thebibliography}{1}\bibitem{k}Source.\end{thebibliography}"
        r=self._batch01_parse(body,preamble=r"\usepackage{algorithm2e}")
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        text=" ".join(n["content"] for n in r["elements"])
        for value in (r"Outer \cite{k}","$i=1$ to $n$","Work $x=y$","Reduce.","First output.","Second output.","After."):self.assertIn(value,text)
        self.assertEqual(text.count("return"),2)
        for bad in (r"\tcp*[z]{Text}",r"\tcp*[f]{Text",r"\While(\unknown{Lost}){x}{Work}"):
            self.assertEqual(self._batch01_parse(r"\begin{algorithm}"+bad+r"\end{algorithm}",preamble=r"\usepackage{algorithm2e}")["versions"][0]["status"],"parse_error")


    def test_batch06_literal_text_glyphs_spaces_and_local_redefinition(self):
        r=self._batch01_parse(r"St\o rmer, gro\ss{} and \i, \j. Before\space after. 20\degree{}C.",
            preamble=r"\usepackage{gensymb}")
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        text=" ".join(n["content"] for n in r["elements"])
        for x in ("St\u00f8rmer","gro\u00df","\u0131","\u0237","Before after","20\u00b0C"):self.assertIn(x,text)
        local=self._batch01_parse(r"\o{}After.",preamble=r"\newcommand{\o}{Custom.}")
        self.assertEqual(local["versions"][0]["status"],"success",local["versions"])
        self.assertIn("Custom."," ".join(n["content"] for n in local["elements"]))
        self.assertEqual(self._batch01_parse(r"20\degree")["versions"][0]["status"],"parse_error")

    def test_batch06_page_reference_is_retained_without_inventing_page_numbers(self):
        r=self._batch01_parse(r"Page \pageref{sec:a}\dotfill $x$.",preamble=r"\usepackage{hyperref}")
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        text=" ".join(n["content"] for n in r["elements"])
        self.assertIn(r"\pageref{sec:a}",text);self.assertIn(r"\dotfill{}",text)
        self.assertEqual(self._batch01_parse(r"\pageref{\dynamic}")["versions"][0]["status"],"parse_error")

    def test_batch06_hyperlink_preserves_display_and_scientific_citation(self):
        r=self._batch01_parse(r"\hypertarget{a}{Start.} \hyperlink{a}{Keep $x$ \cite{k}.}After."
            r"\begin{thebibliography}{1}\bibitem{k}Source.\end{thebibliography}",preamble=r"\usepackage{hyperref}")
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        self.assertIn(r"Keep $x$ \cite{k}."," ".join(n["content"] for n in r["elements"]))
        self.assertEqual(self._batch01_parse(r"\hyperlink{\dynamic}{Text}",preamble=r"\usepackage{hyperref}")["versions"][0]["status"],"parse_error")

    def test_batch06_landscape_keeps_table_and_following_text(self):
        body=r"\begin{landscape}\begin{table}\caption{Measured $x$.}\begin{tabular}{cc}A&B\\1&2\end{tabular}\end{table}\end{landscape}After."
        r=self._batch01_parse(body,preamble=r"\usepackage{pdflscape}")
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        text=" ".join(n["content"] for n in r["elements"]);self.assertIn("Measured $x$",text);self.assertIn("After.",text)
        self.assertEqual(self._batch01_parse(body)["versions"][0]["status"],"parse_error")

    def test_batch06_ieee_proof_preserves_optional_title_equation_and_citation(self):
        body=r"\begin{IEEEproof}[Evidence]Proof \cite{k}.\[x=y\]\end{IEEEproof}After." \
             r"\begin{thebibliography}{1}\bibitem{k}Source.\end{thebibliography}"
        r=self._batch01_parse(body,document_class="IEEEtran")
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        text=" ".join(n["content"] for n in r["elements"]);self.assertIn("Evidence",text);self.assertIn(r"\cite{k}",text)
        self.assertTrue(any(n["kind"]=="block_formula" and "x=y" in n["content"] for n in r["elements"]))
        self.assertEqual(self._batch01_parse(body)["versions"][0]["status"],"parse_error")

    def test_batch06_layout_increments_and_page_enlargement_are_bounded(self):
        r=self._batch01_parse(r"\addtolength{\textheight}{-12cm}\addtolength{\topmargin}{.03in}"
            r"\addtolength{\baselineskip}{.5mm}\setlength{\spaceskip}{.3em}\enlargethispage*{-4\baselineskip}Body.")
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        self.assertIn("Body."," ".join(n["content"] for n in r["elements"]))
        for bad in (r"\enlargethispage{\unknown}",r"\enlargethispage{1pt plus 2pt}",r"\addtolength{\counter}{1pt}"):
            self.assertEqual(self._batch01_parse(bad)["versions"][0]["status"],"parse_error")

    def test_batch06_rotatebox_and_relative_font_reparse_all_content(self):
        r=self._batch01_parse(r"\rotatebox[origin=c]{15}{\textsmaller[2]{Rotated $x$ \cite{k}.}}After."
            r"\begin{thebibliography}{1}\bibitem{k}Source.\end{thebibliography}",
            preamble=r"\usepackage{graphicx,relsize}")
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        self.assertIn(r"Rotated $x$ \cite{k}."," ".join(n["content"] for n in r["elements"]))
        for bad in (r"\rotatebox{\unknown}{Text}",r"\rotatebox[origin=\unknown]{15}{Text}",r"\rotatebox{15}{\unknown}"):
            self.assertEqual(self._batch01_parse(bad,preamble=r"\usepackage{graphicx}")["versions"][0]["status"],"parse_error")

    def test_batch06_bbl_exact_cprime_definition_keeps_keys_and_raw(self):
        body=r"See \cite{k}.\bibliography{refs}"
        bbl=r"\def\cprime{$'$}\begin{thebibliography}{1}\bibitem{k}Author.\newblock Title\cprime.\newblock 2020.\end{thebibliography}"
        r=self._batch01_parse(body,files={"main.bbl":bbl})
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        self.assertEqual(r["bibtex"][0]["citation_key"],"k")
        self.assertIn(r"\cprime",r["versions"][0]["bibliography_evidence"][0]["raw"])
        bad=self._batch01_parse(body,files={"main.bbl":bbl.replace("$'$",r"\bibitem{fake}Lost.")})
        self.assertEqual(bad["versions"][0]["status"],"parse_error")

    def test_batch06_unscoped_literal_environment_aliases_keep_tree(self):
        preamble=r"\usepackage{amsmath}\newcommand{\be}{\begin{enumerate}}\newcommand{\ee}{\end{enumerate}}" \
                 r"\newcommand{\bs}{\begin{subequations}}\newcommand{\es}{\end{subequations}}" \
                 r"\newcommand{\ep}{\end{proof}\medskip}"
        body=r"\be\item First.\item Second.\ee\bs\begin{equation}x=y\end{equation}\es" \
             r"\begin{proof}Proof.\ep After."
        r=self._batch01_parse(body,preamble=preamble)
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        text=" ".join(n["content"] for n in r["elements"])
        for word in ("First.","Second.","x=y","Proof.","After."):self.assertIn(word,text)
        scoped=self._batch01_parse(r"\begin{proof}Proof.\ep",preamble=r"{\newcommand{\ep}{\end{proof}}}")
        self.assertEqual(scoped["versions"][0]["status"],"parse_error")
        dangerous=self._batch01_parse(r"\begin{proof}Proof.\ep",preamble=r"\newcommand{\ep}{\end{proof}Lost.}")
        self.assertEqual(dangerous["versions"][0]["status"],"parse_error")

    def test_batch06_balanced_group_ignores_braces_inside_valid_inline_verb(self):
        r=self._batch01_parse(r"Before {\tt \verb|{|}{\em content}{\tt \verb|}|} after."
            r"\begin{itemize}\item Keep {\tt \verb*+}+} content.\end{itemize}")
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        text=" ".join(n["content"] for n in r["elements"])
        self.assertIn(r"\verb|{|",text);self.assertIn("content",text);self.assertIn("after.",text)
        self.assertEqual(self._batch01_parse(r"Before {\verb|not closed}After.")["versions"][0]["status"],"parse_error")


    def test_batch06_unbraced_dotless_accent_consumes_exact_control_word(self):
        r=self._batch01_parse(r"Voskresenski\u\i\ " + "name. " + r"Also \'{\i} and \'\j.")
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        text=" ".join(n["content"] for n in r["elements"])
        self.assertIn("Voskresenski\u012d name.",text);self.assertIn("\u00ed",text)
        self.assertNotIn("\\\u0306",text)
        for source,preamble in ((r"\u\unknown",""),(r"\u\i",r"\newcommand{\i}{Changed}")):
            self.assertEqual(self._batch01_parse(source,preamble=preamble)["versions"][0]["status"],"parse_error")


    def test_batch06_boundary_alias_scope_matches_literal_begin_and_end(self):
        from src.m2.latex import Gatherer
        for body in (r"\bp Proof.\end{proof}",r"\begin{proof}Proof.\ep",r"\bp Proof.\ep"):
            with self.subTest(body=body),tempfile.TemporaryDirectory() as td:
                d=Path(td)
                (d/"main.tex").write_text(r"\documentclass{article}"
                    r"\newcommand{\bp}{\begin{proof}}\newcommand{\ep}{\end{proof}}"
                    r"\begin{document}"+body+r"\newcommand{\outside}{After.}\outside\end{document}",encoding="utf8")
                g=Gatherer(d);g.gather()
                self.assertEqual(g.environment_depth,0)
                self.assertIs(g.condition_symbols["outside"],True)
                r=parse_versions(AID,[(1,d)])
                self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        with tempfile.TemporaryDirectory() as td:
            d=Path(td);(d/"main.tex").write_text(r"\documentclass{article}"
                r"\newcommand{\bp}{\begin{proof}}\newcommand{\ep}{\end{proof}}"
                r"\begin{document}\bp\newcommand{\inside}{Scoped.}Text.\ep\end{document}",encoding="utf8")
            g=Gatherer(d);g.gather()
            self.assertIs(g.condition_symbols["inside"],False)


    def _batch07_content(self, result):
        self.assertEqual(result["versions"][0]["status"], "success", result["versions"])
        return " ".join(n["content"] for n in result["elements"])

    def test_batch07_quoting_keeps_nested_content_and_citation(self):
        r = self._batch01_parse(r"Before.\begin{quoting}{}\textbf{Keep} $x$ \cite{k}.\end{quoting}After.\bibliography{r}",
            preamble=r"\usepackage{quoting}", files={"r.bib": "@article{k,title={Evidence},year={2020}}"})
        text = self._batch07_content(r)
        for value in ("Before.", "Keep", "$x$", r"\cite{k}", "After."):
            self.assertIn(value, text)
        self.assertTrue(any(n["content"] == "quoting" for n in r["elements"]))

    def test_batch07_quoting_requires_provider_and_rejects_uninterpreted_options(self):
        for preamble, body in [("", r"\begin{quoting}Keep.\end{quoting}"),
                              (r"\usepackage{quoting}", r"\begin{quoting}[unknown]Keep.\end{quoting}")]:
            self.assertEqual(self._batch01_parse(body, preamble=preamble)["versions"][0]["status"], "parse_error")

    def test_batch07_appendixpage_keeps_appendix_heading_and_formula(self):
        r = self._batch01_parse(r"\section{Main}Body.\appendix\appendixpage\section{Details}Kept.\[x=y\]After.",
            preamble=r"\usepackage{appendix}")
        text = self._batch07_content(r)
        for value in ("Main", "Details", "Kept.", "x=y", "After."):
            self.assertIn(value, text)
        self.assertTrue(any(m.get("command") == "appendixpage" for m in r["versions"][0]["layout_metadata"]))

    def test_batch07_tcolorbox_font_color_preserves_title_body_and_citation(self):
        r = self._batch01_parse(r"\begin{tcolorbox}[title={Evidence},fonttitle=\bfseries\color{white},colbacktitle=gray!75!black]"
            r"Keep $x$ \cite{k}.\end{tcolorbox}After.\bibliography{r}",
            preamble=r"\usepackage{tcolorbox,xcolor}", files={"r.bib": "@article{k,title={Source}}"})
        text = self._batch07_content(r)
        for value in ("Evidence", "Keep", "$x$", r"\cite{k}", "After."):
            self.assertIn(value, text)

    def test_batch07_tcolorbox_font_rejects_hooks_dynamic_color_and_local_override(self):
        for font, preamble in [(r"\bfseries\unknown{Lost}", ""), (r"\color{\custom}", ""),
                               (r"\color{white}", r"\newcommand{\color}[1]{Lost}")]:
            r = self._batch01_parse(r"\begin{tcolorbox}[fonttitle={" + font + r"}]Keep.\end{tcolorbox}",
                preamble=r"\usepackage{tcolorbox,xcolor}" + preamble)
            self.assertEqual(r["versions"][0]["status"], "parse_error", r["versions"])

    def test_batch07_empty_algorithmic_state_retains_following_statements(self):
        r = self._batch01_parse(r"\begin{algorithmic}\STATE First $x$.\STATE"
            r"\STATE Second \cite{k}.\STATE\RETURN $x$\end{algorithmic}After.\bibliography{r}",
            preamble=r"\usepackage{algorithmic}", files={"r.bib": "@article{k,title={Evidence}}"})
        text = self._batch07_content(r)
        for value in ("First", "Second", r"\cite{k}", "return", "After."):
            self.assertIn(value, text)
        self.assertTrue(any(m.get("blank_line") for m in r["versions"][0]["layout_metadata"]))

    def test_batch07_empty_algorithmic_require_still_rejected(self):
        r = self._batch01_parse(r"\begin{algorithmic}\REQUIRE\STATE Work.\end{algorithmic}",
            preamble=r"\usepackage{algorithmic}")
        self.assertEqual(r["versions"][0]["status"], "parse_error")
        self.assertIn("Empty algorithmic REQUIRE", r["versions"][0]["error"])

    def test_batch07_mnras_typesetting_marker_is_not_scientific_text(self):
        r = self._batch01_parse(r"\section{Main}Science.\bsp\label{lastpage}", document_class="mnras")
        self.assertIn("Science.", self._batch07_content(r))
        self.assertTrue(any(m.get("command") == "bsp" for m in r["versions"][0]["layout_metadata"]))
        self.assertEqual(self._batch01_parse(r"\bsp")["versions"][0]["status"], "parse_error")

    def test_batch07_vgtc_firstsection_has_correct_following_children(self):
        r = self._batch01_parse(r"\firstsection{Introduction}\maketitle Main.\subsection{Details}More.",
            document_class="vgtc")
        self._batch07_content(r)
        intro = next(n["element_id"] for n in r["elements"] if n["content"] == "Introduction")
        main = next(n["element_id"] for n in r["elements"] if n["content"] == "Main.")
        details = next(n["element_id"] for n in r["elements"] if n["content"] == "Details")
        for eid in (main, details):
            self.assertTrue(any(e["element_id"] == eid and e["parent_id"] == intro and e["version"] == 1 for e in r["hierarchy"]))

    def test_batch07_hrule_preserves_neighbors_and_literal_dimensions(self):
        r = self._batch01_parse(r"Before.\hrule height 0.4pt width 12cm depth 0pt\section{After}Keep.")
        text = self._batch07_content(r)
        for value in ("Before.", "After", "Keep."):
            self.assertIn(value, text)
        self.assertTrue(any(m.get("dimensions") == [("height", "0.4pt"), ("width", "12cm"), ("depth", "0pt")]
            for m in r["versions"][0]["layout_metadata"]))
        self.assertEqual(self._batch01_parse(r"\hrule width \dynamic Keep.")["versions"][0]["status"], "parse_error")

    def test_batch07_elsevier_graphical_abstract_and_highlights_keep_all_items(self):
        for cls in ("elsarticle", "cas-sc", "cas-dc"):
            graphics = r"\begin{graphicalabstract}Figure text.\includegraphics{absent.png}\end{graphicalabstract}" if cls == "elsarticle" else ""
            r = self._batch01_parse(graphics + r"\begin{highlights}\item First $x$.\item Second \cite{k}."
                r"\end{highlights}\section{Main}After.\bibliography{r}", document_class=cls,
                files={"r.bib": "@article{k,title={Evidence}}"})
            text = self._batch07_content(r)
            for value in ("First", "Second", "$x$", r"\cite{k}", "After."):
                self.assertIn(value, text)
            if graphics:
                self.assertIn("Figure text.", text)
                self.assertTrue(any(n["kind"] == "figure" and "absent.png" in n["content"] for n in r["elements"]))

    def test_batch07_sideways_table_keeps_caption_cells_and_citation(self):
        for cls, preamble in [("article", r"\usepackage{rotating}"), ("sn-jnl", "")]:
            r = self._batch01_parse(r"\begin{sidewaystable}[t]\caption{Kept caption}"
                r"\begin{tabular}{l}Cell \cite{k}\\\end{tabular}\end{sidewaystable}After.\bibliography{r}",
                document_class=cls, preamble=preamble, files={"r.bib": "@article{k,title={Evidence}}"})
            text = self._batch07_content(r)
            for value in ("Kept caption", "Cell", r"\cite{k}", "After."):
                self.assertIn(value, text)
            self.assertTrue(any(n["kind"] == "figure" for n in r["elements"]))
        self.assertEqual(self._batch01_parse(r"\begin{sidewaystable}Keep.\end{sidewaystable}")["versions"][0]["status"], "parse_error")

    def test_batch07_nameref_retains_token_without_inventing_title(self):
        r = self._batch01_parse(r"See \nameref{sec:later} and \nameref*{sec:later}. After.",
            preamble=r"\usepackage{hyperref}")
        text = self._batch07_content(r)
        self.assertIn(r"\nameref{sec:later}", text)
        self.assertIn(r"\nameref*{sec:later}", text)
        self.assertEqual(self._batch01_parse(r"\nameref{\dynamic}", preamble=r"\usepackage{hyperref}")["versions"][0]["status"], "parse_error")

    def test_batch07_uline_retains_visible_content_math_and_citation(self):
        r = self._batch01_parse(r"Before \uline{nested \textbf{content} $x$ \cite{k}} after.\bibliography{r}",
            preamble=r"\usepackage{ulem}", files={"r.bib": "@article{k,title={Evidence}}"})
        text = self._batch07_content(r)
        for value in ("Before", "nested", "content", "$x$", r"\cite{k}", "after."):
            self.assertIn(value, text)
        self.assertEqual(self._batch01_parse(r"\uline{Keep}")["versions"][0]["status"], "parse_error")

    def test_batch07_line_numbering_keeps_algorithm_body(self):
        r = self._batch01_parse(r"\LinesNumbered\begin{algorithm}\KwIn{Input $x$}Work.\Return $x$\end{algorithm}After.",
            preamble=r"\usepackage{algorithm2e}")
        text = self._batch07_content(r)
        for value in ("Input", "Work.", "return", "After."):
            self.assertIn(value, text)

    def test_batch07_cas_credit_keeps_original_value_in_working_metadata(self):
        r = self._batch01_parse(r"\author{A}\credit{Data collection, Writing}\begin{abstract}Science.\end{abstract}After.",
            document_class="cas-sc")
        self.assertIn("Science.", self._batch07_content(r))
        self.assertTrue(any(m.get("command") == "credit" and m.get("arguments") == ["Data collection, Writing"]
            for m in r["versions"][0]["layout_metadata"]))

    def test_batch07_qty_retains_source_units_and_rejects_unknown_semantics(self):
        r = self._batch01_parse(r"Length \qty{200}{\mm}. Time \qty[mode=text]{2}{\second}. After.",
            preamble=r"\usepackage{siunitx}")
        text = self._batch07_content(r)
        self.assertIn(r"\qty{200}{\mm}", text)
        self.assertIn(r"\qty[mode=text]{2}{\second}", text)
        for body, preamble in [(r"\qty{word}{\mm}", r"\usepackage{siunitx}"),
                               (r"\qty[unknown=\hook]{2}{\mm}", r"\usepackage{siunitx}"),
                               (r"\qty{2}{\unknown}", r"\usepackage{siunitx}"),
                               (r"\qty{2}{\mm}", r"\usepackage{siunitx,physics}"),
                               (r"\qty{2}{\mm}", r"\usepackage{siunitx}\newcommand{\mm}{Changed}")]:
            self.assertEqual(self._batch01_parse(body, preamble=preamble)["versions"][0]["status"], "parse_error")

    def test_batch07_local_command_definitions_keep_priority(self):
        for cls, preamble, body in [
            ("vgtc", r"\newcommand{\firstsection}[1]{Custom #1.}", r"\firstsection{Title}"),
            ("mnras", r"\newcommand{\bsp}{Custom.}", r"\bsp"),
            ("article", r"\usepackage{ulem}\newcommand{\uline}[1]{Custom #1.}", r"\uline{Text}"),
            ("article", r"\usepackage{algorithm2e}\newcommand{\LinesNumbered}{Custom.}", r"\LinesNumbered"),
            ("cas-sc", r"\newcommand{\credit}[1]{Custom #1.}", r"\credit{Role}")]:
            with self.subTest(body=body):
                self.assertIn("Custom", self._batch07_content(self._batch01_parse(body, document_class=cls, preamble=preamble)))


    def test_batch08_unused_macro_ending_in_verb_is_tokenized_not_executed(self):
        preamble = (r"\newcommand\fverb{\setbox\mybox=\hbox\bgroup\verb}"
                    r"\newcommand\fverbdo{\egroup\medskip\noindent\fbox{\unhbox\mybox}\ }")
        r = self._batch01_parse(r"\section{Intro}Keep $x$ \cite{k}.\bibliography{refs}",
            preamble=preamble, files={"refs.bib":"@article{k,title={Evidence},year={2020}}"})
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        text = " ".join(n["content"] for n in r["elements"])
        self.assertIn("Keep $x$",text);self.assertIn(r"\cite{k}",text)
        self.assertNotIn("setbox",text)
        called = self._batch01_parse(r"\fverb|Keep|",preamble=preamble)
        self.assertEqual(called["versions"][0]["status"],"parse_error")
        self.assertIn("setbox",called["versions"][0]["error"])

    def test_batch08_executed_verbatim_and_malformed_literals_keep_strict_reader(self):
        for definition in (r"\newcommand{\later}{\verb}",r"\def\later{\verb}"):
            r = self._batch01_parse(r"{\verb|{|} After.",preamble=definition)
            self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
            self.assertIn(r"\verb|{|"," ".join(n["content"] for n in r["elements"]))
        from src.m2.latex import group, ParseError
        for literal in ("{"+r"\verb|unclosed}", "{"+r"\verb|line"+"\n"+r"next|}"):
            with self.assertRaises(ParseError): group(literal,0)
        with self.assertRaises(ParseError):
            group(r"{\verb|{|}",0,inline_literals=False)


    def test_batch08_cjk_utf8_wrapper_preserves_inline_text_and_top_level_abstract(self):
        r=self._batch01_parse(r"Before \begin{CJK*}{UTF8}{gbsn}Chinese \textbf{content} $x$\end{CJK*} after.",
            preamble=r"\usepackage{CJKutf8}")
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        self.assertIn("Before Chinese content $x$ after."," ".join(n["content"] for n in r["elements"]))
        r=self._batch01_parse(r"\begin{CJK}{UTF8}{mj}\begin{abstract}Keep abstract.\end{abstract}"
            r"\section{Intro}Keep body.\end{CJK}",preamble=r"\usepackage{CJK}")
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        for token in ("Keep abstract.","Intro","Keep body."):self.assertIn(token," ".join(n["content"] for n in r["elements"]))
        for body,pre in [(r"\begin{CJK}{GBK}{gbsn}Keep.\end{CJK}",r"\usepackage{CJK}"),
                         (r"\begin{CJK}{UTF8}{\dynamic}Keep.\end{CJK}",r"\usepackage{CJK}"),
                         (r"\begin{CJK}{UTF8}{mj}\unknown{Keep}\end{CJK}",r"\usepackage{CJK}")]:
            self.assertEqual(self._batch01_parse(body,preamble=pre)["versions"][0]["status"],"parse_error")

    def test_batch08_mdframed_defaults_keep_list_equation_and_citation(self):
        r=self._batch01_parse(r"\begin{mdframed}Keep.\begin{itemize}\item Evidence \cite{k}.\end{itemize}\[x=y\]\end{mdframed}After.\bibliography{r}",
            preamble=r"\usepackage{mdframed}",files={"r.bib":"@article{k,title={Evidence}}"})
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        for token in ("Keep.","After.",r"\cite{k}","x=y"):self.assertIn(token," ".join(n["content"] for n in r["elements"]))
        for pre in (r"\usepackage{mdframed}\mdfsetup{before={Lost.}}",r"\usepackage[unknown]{mdframed}"):
            self.assertEqual(self._batch01_parse(r"\begin{mdframed}Keep.\end{mdframed}",preamble=pre)["versions"][0]["status"],"parse_error")

    def test_batch08_inline_enumitem_keeps_all_items_and_rejects_setup_hooks(self):
        body=r"\begin{enumerate*}[label=(\roman*)]\item First $x$.\item Second.\end{enumerate*}After."
        r=self._batch01_parse(body,preamble=r"\usepackage[inline]{enumitem}")
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        self.assertEqual(len([n for n in r["elements"] if n["kind"]=="item"]),2)
        for pre in (r"\usepackage{enumitem}",r"\usepackage[inline]{enumitem}\setlist{after={Lost.}}"):
            self.assertEqual(self._batch01_parse(body,preamble=pre)["versions"][0]["status"],"parse_error")

    def test_batch08_twocolumn_title_wrapper_keeps_abstract_and_requires_context(self):
        body=r"\twocolumn[\begin{@twocolumnfalse}\section*{Title}\begin{abstract}Keep abstract.\end{abstract}\end{@twocolumnfalse}]After."
        r=self._batch01_parse(body)
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        for text in ("Title","Keep abstract.","After."):self.assertIn(text," ".join(n["content"] for n in r["elements"]))
        self.assertEqual(self._batch01_parse(r"\begin{@twocolumnfalse}Keep.\end{@twocolumnfalse}")["versions"][0]["status"],"parse_error")

    def test_batch08_resizebox_keeps_math_prose_citation_and_local_override(self):
        r=self._batch01_parse(r"Before \resizebox{.98\linewidth}{!}{$x=y$ \cite{k}} after.\bibliography{r}",
            preamble=r"\usepackage{graphicx}",files={"r.bib":"@article{k,title={Evidence}}"})
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        self.assertIn(r"Before $x=y$ \cite{k} after."," ".join(n["content"] for n in r["elements"]))
        for dims in (r"{\dynamic}{!}",r"{!}{!}",r"{2cm}{\dynamic}"):
            self.assertEqual(self._batch01_parse(r"\resizebox"+dims+r"{Keep.}",preamble=r"\usepackage{graphicx}")["versions"][0]["status"],"parse_error")
        r=self._batch01_parse(r"\resizebox{a}{b}{Science.}",preamble=r"\usepackage{graphicx}\newcommand{\resizebox}[3]{Local #3}")
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        self.assertIn("Local Science."," ".join(n["content"] for n in r["elements"]))

    def test_batch08_font_and_line_spread_keep_words_and_reject_dynamic_values(self):
        r=self._batch01_parse(r"{\fontfamily{cmss}\selectfont Switcher}--{\fontfamily{qcr}\selectfont Adversary}.\linespread{1.2}After.")
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        for text in ("Switcher","Adversary","After."):self.assertIn(text," ".join(n["content"] for n in r["elements"]))
        for body in (r"\fontfamily{\dynamic}Keep.",r"\linespread{\dynamic}Keep.",r"\linespread{0}Keep."):
            self.assertEqual(self._batch01_parse(body)["versions"][0]["status"],"parse_error")

    def test_batch08_page_break_and_figure_name_are_retained_safely(self):
        r=self._batch01_parse(r"See \figurename~\ref{fig:x}.\nopagebreak[4]After.")
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        text=" ".join(n["content"] for n in r["elements"])
        self.assertIn(r"\figurename{}",text);self.assertIn("After.",text)
        for option in ("5",r"\dynamic"):
            self.assertEqual(self._batch01_parse(r"\nopagebreak["+option+r"]Keep.")["versions"][0]["status"],"parse_error")

    def test_batch08_ams_url_and_agu_authors_preserve_raw_metadata(self):
        for cls,body,command in [
            ("amsart",r"\urladdr{\url{https://example.org/person}}\begin{abstract}Science.\end{abstract}After.","urladdr"),
            ("agujournal2019",r"\authors{Alice\affil{1}, Bob}\begin{abstract}Science.\end{abstract}After.","authors")]:
            r=self._batch01_parse(body,document_class=cls)
            self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
            self.assertTrue(any(m.get("command")==command and m.get("arguments") for m in r["versions"][0]["layout_metadata"]))
            self.assertIn("Science."," ".join(n["content"] for n in r["elements"]))
        self.assertEqual(self._batch01_parse(r"\authors{Unknown.}")["versions"][0]["status"],"parse_error")

    def test_batch08_title_note_keeps_funding_text_citation_and_body(self):
        r=self._batch01_parse(r"\tnotetext[funding]{Funding \textbf{support} \cite{k}.}\begin{abstract}Science.\end{abstract}After.\bibliography{r}",
            document_class="elsarticle",files={"r.bib":"@article{k,title={Evidence}}"})
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        for text in ("Funding support",r"\cite{k}","Science.","After."):self.assertIn(text," ".join(n["content"] for n in r["elements"]))
        bad=self._batch01_parse(r"\tnotetext[x]{\unknown{Lost.}}",document_class="cas-sc")
        self.assertEqual(bad["versions"][0]["status"],"parse_error")

    def test_batch08_classification_fields_keep_codes_and_edition_without_guessing(self):
        r=self._batch01_parse(r"\pacs[MSC Classification]{28A75, 49Q15}\begin{abstract}Keep.\end{abstract}",document_class="sn-jnl")
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        self.assertTrue(any(m.get("arguments")==["28A75, 49Q15"] for m in r["versions"][0]["layout_metadata"]))
        self.assertIn("28A75, 49Q15", " ".join(n["content"] for n in r["elements"]))
        r=self._batch01_parse(r"\begin{keyword}Geometry \MSC[2008] 57K30 \sep 57K16\end{keyword}After.",document_class="elsarticle")
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        for text in ("57K30","57K16","After."):self.assertIn(text," ".join(n["content"] for n in r["elements"]))
        self.assertEqual(self._batch01_parse(r"\MSC 57K30",document_class="elsarticle")["versions"][0]["status"],"parse_error")

    def test_batch08_html_color_is_bounded_xcolor_only(self):
        r=self._batch01_parse(r"\definecolor{shade}{HTML}{00aAFF}Keep.",preamble=r"\usepackage{xcolor}")
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        for spec,pre in [("GG0000",r"\usepackage{xcolor}"),("000",r"\usepackage{xcolor}"),("000000",r"\usepackage{color}")]:
            self.assertEqual(self._batch01_parse(r"\definecolor{x}{HTML}{"+spec+r"}Keep.",preamble=pre)["versions"][0]["status"],"parse_error")

    def test_batch08_listing_numbersep_preserves_literal_code_and_rejects_dynamic(self):
        r=self._batch01_parse("\\begin{lstlisting}[numbersep=4pt]\n  x = 1 % literal\n\\end{lstlisting}After.",preamble=r"\usepackage{listings}")
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        self.assertTrue(any("  x = 1 % literal" in n["content"] for n in r["elements"] if n["kind"]=="verbatim"))
        self.assertEqual(self._batch01_parse(r"\begin{lstlisting}[numbersep=\dynamic]Keep.\end{lstlisting}",preamble=r"\usepackage{listings}")["versions"][0]["status"],"parse_error")

    def test_batch08_eif_keeps_both_branches_and_return_boundaries(self):
        r=self._batch01_parse(r"\begin{algorithm}\eIf{$x>0$}{Positive \cite{k}.\Return $x$}{Negative.\Return $y$}\end{algorithm}After.\bibliography{r}",
            preamble=r"\usepackage{algorithm2e}",files={"r.bib":"@article{k,title={Evidence}}"})
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        nodes={n["element_id"]:n for n in r["elements"]}
        branches=[n for n in r["elements"] if n["kind"]=="component" and n["content"] in {"then","else"}]
        self.assertEqual(len(branches),2)
        for branch in branches:
            text=" ".join(nodes[e["element_id"]]["content"] for e in r["hierarchy"] if e["parent_id"]==branch["element_id"])
            self.assertIn("return",text)
        self.assertEqual(self._batch01_parse(r"\eIf{$x$}{A}{B}",preamble=r"\usepackage{algorithm2e}")["versions"][0]["status"],"parse_error")

    def test_batch08_listing_display_options_preserve_all_code_lines(self):
        body = "\\begin{lstlisting}[numbersep=5pt,showspaces=false,showtabs=true,stepnumber=2]\nfirst()\nsecond()\nthird()\n\\end{lstlisting}After."
        r = self._batch01_parse(body, preamble=r"\usepackage{listings}")
        self.assertEqual(r["versions"][0]["status"], "success", r["versions"])
        text = " ".join(n["content"] for n in r["elements"])
        self.assertIn("first()\nsecond()\nthird()", text.replace("\r\n", "\n"))
        self.assertIn("After.", text)
        for option in (r"showtabs=\hook", "showspaces=maybe", "stepnumber=0", r"stepnumber=\hook", "firstline=2"):
            r = self._batch01_parse("\\begin{lstlisting}["+option+"]\nKeep\n\\end{lstlisting}", preamble=r"\usepackage{listings}")
            self.assertEqual(r["versions"][0]["status"], "parse_error")

    def test_batch08_table_name_preserves_token_and_local_definition(self):
        r = self._batch01_parse(r"See \tablename~\ref{tab:x}. After.")
        self.assertEqual(r["versions"][0]["status"], "success", r["versions"])
        self.assertIn(r"\tablename{}", " ".join(n["content"] for n in r["elements"]))
        r = self._batch01_parse(r"See \tablename.", preamble=r"\renewcommand{\tablename}{Tableau}")
        self.assertIn("Tableau", " ".join(n["content"] for n in r["elements"]))


    def test_exhaustive_requirepackage_is_real_provider_not_commented_evidence(self):
        r=self._batch01_parse(r"\text{Kept} \resizebox{2cm}{!}{$x$}.",
            preamble=r"\RequirePackage{amsmath,graphicx}")
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        self.assertIn("Kept $x$.",self._batch07_content(r))
        r=self._batch01_parse(r"\text{Keep}",preamble="%"+r"\RequirePackage{amsmath}"+"\n")
        self.assertEqual(r["versions"][0]["status"],"parse_error")
        r=self._batch01_parse(r"\text{Keep}",preamble=r"\iffalse\RequirePackage{amsmath}\fi")
        self.assertEqual(r["versions"][0]["status"],"parse_error")

    def test_exhaustive_acmart_text_wrapper_keeps_nested_content(self):
        r=self._batch01_parse(r"A \text{\textbf{mention} $x$} after.",document_class="acmart")
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        self.assertIn("A mention $x$ after.",self._batch07_content(r))
        self.assertEqual(self._batch01_parse(r"\text{\unknown{Lost}}",document_class="acmart")["versions"][0]["status"],"parse_error")

    def test_exhaustive_kernel_font_spacing_declarations_do_not_consume_words(self):
        body=r"\ttfamily Alpha \sffamily Beta \scshape Gamma \Huge Delta \frenchspacing Epsilon \null Zeta \thinspace Eta \medbreak Theta \smallbreak Iota \bigbreak Kappa \goodbreak Lambda."
        r=self._batch01_parse(body)
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        text=self._batch07_content(r)
        words=["Alpha","Beta","Gamma","Delta","Epsilon","Zeta","Eta","Theta","Iota","Kappa","Lambda"]
        self.assertEqual(sorted(words,key=text.index),words)
        r=self._batch01_parse(r"\scshape Next.",preamble=r"\renewcommand{\scshape}{Local.}")
        self.assertIn("Local.",self._batch07_content(r))

    def test_exhaustive_text_glyphs_keep_visible_values(self):
        r=self._batch01_parse(r"\ae{} \AE{} \textquotesingle{} \texttrademark{} \textsection{} \textsterling{} \textperiodcentered{} \textemdash{} \textendash{}.")
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        for glyph in ("æ","Æ","'","™","§","£","·","—","–"):
            self.assertIn(glyph,self._batch07_content(r))

    def test_exhaustive_linebreak_strength_is_bounded(self):
        r=self._batch01_parse(r"Before\nolinebreak[4] after.")
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        self.assertIn("Before after.",self._batch07_content(r))
        for strength in ("5",r"\dynamic"):
            self.assertEqual(self._batch01_parse(r"\nolinebreak["+strength+r"]Keep.")["versions"][0]["status"],"parse_error")

    def test_exhaustive_ulem_normalem_needs_provider(self):
        r=self._batch01_parse(r"\normalem Before \emph{science}.",preamble=r"\usepackage{ulem}")
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        self.assertIn("Before science.",self._batch07_content(r))
        self.assertEqual(self._batch01_parse(r"\normalem Keep.")["versions"][0]["status"],"parse_error")

    def test_exhaustive_lineno_commands_and_display_breaks_validate_arguments(self):
        for pre,body in [(r"\usepackage{lineno}",r"\nolinenumbers Keep."),
                         (r"\usepackage{amsmath}",r"\allowdisplaybreaks[2]Keep.")]:
            r=self._batch01_parse(body,preamble=pre)
            self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
            self.assertIn("Keep.",self._batch07_content(r))
        for pre,body in [("",r"\nolinenumbers Keep."),("",r"\allowdisplaybreaks Keep."),
                         (r"\usepackage{amsmath}",r"\allowdisplaybreaks[7]Keep.")]:
            self.assertEqual(self._batch01_parse(body,preamble=pre)["versions"][0]["status"],"parse_error")

    def test_exhaustive_footref_keeps_label_without_guessing_number(self):
        r=self._batch01_parse(r"Reuse\footref{fn:a}. After.",preamble=r"\usepackage{footmisc}")
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        self.assertIn(r"\footref{fn:a}",self._batch07_content(r))
        for pre,body in [("",r"\footref{fn:a}"),(r"\usepackage{footmisc}",r"\footref{\dynamic}")]:
            self.assertEqual(self._batch01_parse(body,preamble=pre)["versions"][0]["status"],"parse_error")

    def test_exhaustive_class_metadata_preserves_raw_values(self):
        for cls,pre,body,cmd in [
            ("acmart","",r"\copyrightyear{2025}\begin{abstract}Keep.\end{abstract}After.","copyrightyear"),
            ("article",r"\usepackage{jheppub}",r"\emailAdd{a@example.org}\begin{abstract}Keep.\end{abstract}After.","emailAdd"),
            ("amsart","",r"\curraddr[Author]{Institute}\begin{abstract}Keep.\end{abstract}After.","curraddr"),
            ("IEEEtran","",r"\IEEEoverridecommandlockouts\begin{abstract}Keep.\end{abstract}After.","IEEEoverridecommandlockouts")]:
            r=self._batch01_parse(body,document_class=cls,preamble=pre)
            self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
            self.assertIn("Keep.",self._batch07_content(r));self.assertIn("After.",self._batch07_content(r))
            self.assertTrue(any(m.get("command")==cmd for m in r["versions"][0]["layout_metadata"]))

    def test_exhaustive_framed_keeps_science_and_rejects_custom_hooks(self):
        body=r"\begin{framed}Before.\begin{itemize}\item Keep $x$ \cite{k}.\end{itemize}\[x=y\]\end{framed}After.\bibliography{r}"
        r=self._batch01_parse(body,preamble=r"\usepackage{framed}",files={"r.bib":"@article{k,title={Evidence}}"})
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        for x in ("Before.","After.",r"\cite{k}","x=y"):self.assertIn(x,self._batch07_content(r))
        for pre in ("",r"\usepackage{framed}\renewcommand{\FrameCommand}[1]{Lost #1}"):
            self.assertEqual(self._batch01_parse(body,preamble=pre,files={"r.bib":"@article{k,title={Evidence}}"})["versions"][0]["status"],"parse_error")

    def test_exhaustive_compact_lists_keep_all_items_and_reject_options(self):
        for env in ("compactitem","compactenum"):
            body=r"\begin{"+env+r"}\item First $x$.\item Second.\end{"+env+r"}After."
            r=self._batch01_parse(body,preamble=r"\usepackage{paralist}")
            self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
            self.assertEqual(len([n for n in r["elements"] if n["kind"]=="item"]),2)
            self.assertIn("After.",self._batch07_content(r))
            self.assertEqual(self._batch01_parse(body)["versions"][0]["status"],"parse_error")

    def test_exhaustive_tabularx_preserves_width_columns_cells_and_citation(self):
        body=r"\begin{tabularx}{.9\textwidth}{lX}A & $x$ \cite{k}\\B & C\end{tabularx}After.\bibliography{r}"
        r=self._batch01_parse(body,preamble=r"\usepackage{tabularx}",files={"r.bib":"@article{k,title={Evidence}}"})
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        text=self._batch07_content(r)
        for token in (r"{.9\textwidth}{lX}",r"$x$ \cite{k}",r"B & C","After."):self.assertIn(token,text)
        self.assertEqual(self._batch01_parse(body,files={"r.bib":"@article{k,title={Evidence}}"})["versions"][0]["status"],"parse_error")

    def test_exhaustive_ieeeeqnarray_keeps_formula_and_provider_guard(self):
        body=r"\begin{IEEEeqnarray}{rCl}a&=&b+c\\d&=&e\label{eq:a}\end{IEEEeqnarray}After."
        r=self._batch01_parse(body,document_class="IEEEtran")
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        formulas=[n["content"] for n in r["elements"] if n["kind"]=="block_formula"]
        self.assertEqual(len(formulas),1);self.assertIn(r"a&=&b+c\\d&=&e",formulas[0])
        self.assertIn("After.",self._batch07_content(r))
        self.assertEqual(self._batch01_parse(body)["versions"][0]["status"],"parse_error")
        self.assertEqual(self._batch01_parse(body.replace("{rCl}",r"{\dynamic}"),document_class="IEEEtran")["versions"][0]["status"],"parse_error")

    def test_exhaustive_algorithm_input_declarations_and_data_keep_labels(self):
        body=r"\begin{algorithm}\SetKwInput{KwInput}{Evidence}\KwInput{Sample \cite{k}}\KwData{Data $x$}\Return{$x$}\end{algorithm}\bibliography{r}"
        r=self._batch01_parse(body,preamble=r"\usepackage{algorithm2e}",files={"r.bib":"@article{k,title={Evidence}}"})
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        for x in ("Evidence","Sample",r"\cite{k}","data","Data $x$","return"):self.assertIn(x,self._batch07_content(r))


    def test_exhaustive_unbraced_latex_replacement_is_exactly_one_token(self):
        from src.m2.latex import latex_replacement
        self.assertEqual(latex_replacement(r"\rightarrowtail Tail",0),(r"\rightarrowtail",15))
        for definition in (r"\newcommand\cofib\rightarrowtail",r"\newcommand{\cofib}\rightarrowtail",
                           r"\DeclareRobustCommand{\cofib}\rightarrowtail"):
            r=self._batch01_parse(r"\section{Intro}Keep $\cofib$ after.",preamble=definition)
            self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
            self.assertIn(r"$\cofib$",self._batch07_content(r))
            self.assertIn("after.",self._batch07_content(r))
        r=self._batch01_parse(r"\single Next.",preamble=r"\newcommand\single X")
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        self.assertIn("XNext.",self._batch07_content(r).replace(" ",""))
        r=self._batch01_parse(r"\newcommand\single XRemain. \single")
        self.assertIn("Remain.",self._batch07_content(r))
        for definition in (r"\newcommand\x",r"\newcommand\x#1",r"\def\x\rightarrowtail"):
            self.assertEqual(self._batch01_parse("Keep.",preamble=definition)["versions"][0]["status"],"parse_error")

    def test_exhaustive_unbraced_replacement_does_not_invent_root(self):
        r=self._batch01_parse(r"\section{Intro}Keep.",files={"notes.tex":r"\newcommand\template\documentclass"})
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        self.assertEqual(r["versions"][0]["root"],"main.tex")


    def test_exhaustive_shortcite_uses_existing_validation_and_preserves_key(self):
        body=r"Evidence \shortcite[Ch. 2]{k}. \bibliography{r}"
        r=self._batch01_parse(body,preamble=r"\usepackage{natbib}",files={"r.bib":"@article{k,title={Evidence},year={2020}}"})
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        self.assertIn(r"\citeyear[Ch. 2]{k}",self._batch07_content(r))
        self.assertTrue(any("shortcite" in n for n in r["versions"][0]["normalization_notes"]))
        missing=self._batch01_parse(r"Evidence \shortcite{absent}.",preamble=r"\usepackage{natbib}")
        self.assertEqual(missing["versions"][0]["status"],"parse_error")
        self.assertIn("absent",missing["versions"][0]["error"])

    def test_exhaustive_shortcite_local_macro_and_verbatim_are_not_projected(self):
        r=self._batch01_parse(r"\shortcite{plain} \verb|\shortcite{not_a_cite}|",
            preamble=r"\usepackage{natbib}\newcommand{\shortcite}[1]{Word #1}")
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        self.assertIn("Word plain",self._batch07_content(r))
        self.assertIn(r"\verb|\shortcite{not_a_cite}|",self._batch07_content(r))
        r=self._batch01_parse(r"\shortcite{k}",files={"r.bib":"@article{k,title={Evidence}}"})
        self.assertEqual(r["versions"][0]["status"],"parse_error")

    def test_exhaustive_math_environment_stays_inline_and_keeps_surrounding_words(self):
        r=self._batch01_parse(r"A \begin{math}x+y\end{math} after.")
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        self.assertIn("A $x+y$ after.",self._batch07_content(r))
        self.assertFalse(any(n["kind"]=="block_formula" for n in r["elements"]))
        self.assertEqual(self._batch01_parse(r"\begin{math}x $y$\end{math}")["versions"][0]["status"],"parse_error")

    def test_exhaustive_adjustwidth_preserves_all_body_and_rejects_dynamic_margins(self):
        body=r"\begin{adjustwidth}{2cm}{-1em}\section{Inside}Keep $x$ \cite{k}.\end{adjustwidth}After.\bibliography{r}"
        r=self._batch01_parse(body,preamble=r"\usepackage{changepage}",files={"r.bib":"@article{k,title={Evidence}}"})
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        for x in ("Inside","Keep $x$",r"\cite{k}","After."):self.assertIn(x,self._batch07_content(r))
        heading=next(n["element_id"] for n in r["elements"] if n["kind"]=="heading" and n["content"]=="Inside")
        after=next(n["element_id"] for n in r["elements"] if n["content"]=="After.")
        self.assertTrue(any(e["element_id"]==after and e["parent_id"]==heading for e in r["hierarchy"]))
        self.assertEqual(self._batch01_parse(body.replace("{2cm}",r"{\dynamic}"),preamble=r"\usepackage{changepage}",files={"r.bib":"@article{k,title={Evidence}}"})["versions"][0]["status"],"parse_error")

    def test_exhaustive_displayquote_preserves_quote_and_citation(self):
        body=r"\begin{displayquote}Scientific quotation \cite{k}.\end{displayquote}After.\bibliography{r}"
        r=self._batch01_parse(body,preamble=r"\usepackage{csquotes}",files={"r.bib":"@article{k,title={Evidence}}"})
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        self.assertIn("Scientific quotation",self._batch07_content(r));self.assertIn(r"\cite{k}",self._batch07_content(r))
        self.assertEqual(self._batch01_parse(body.replace("{displayquote}","{displayquote}[citation]",1),preamble=r"\usepackage{csquotes}",files={"r.bib":"@article{k,title={Evidence}}"})["versions"][0]["status"],"parse_error")


    def test_exhaustive_algorithm_preamble_bindings_keep_input_and_program_bodies(self):
        pre=r"\usepackage{algorithm2e}\SetKwInput{KwInput}{Evidence}\SetKwFunction{FMain}{Solve}\SetKwProg{Fn}{Function}{:}{end}"
        body=r"\begin{algorithm}\KwInput{Data \cite{k}}\Fn{\FMain{$x$}}{Keep $y$.\Return{$y$}}\end{algorithm}After.\bibliography{r}"
        r=self._batch01_parse(body,preamble=pre,files={"r.bib":"@article{k,title={Evidence}}"})
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        for token in ("Evidence",r"\cite{k}","Function","Solve($x$)","Keep $y$","return","end","After."):
            self.assertIn(token,self._batch07_content(r))

    def test_exhaustive_algorithm_function_declaration_validates_and_is_scoped(self):
        body=r"\begin{algorithm}\SetKwFunction{FMain}{Solve}\FMain{$x$}\end{algorithm}\begin{algorithm}\FMain{$y$}\end{algorithm}"
        r=self._batch01_parse(body,preamble=r"\usepackage{algorithm2e}")
        self.assertEqual(r["versions"][0]["status"],"parse_error")
        self.assertIn("FMain",r["versions"][0]["error"])
        for definition in (r"\SetKwFunction{section}{Lost}",r"\SetKwFunction{F}{\dynamic}",r"\SetKwProg{Fn}{Function}{\dynamic}{}"):
            r=self._batch01_parse(r"\begin{algorithm}"+definition+r"Keep.\end{algorithm}",preamble=r"\usepackage{algorithm2e}")
            self.assertEqual(r["versions"][0]["status"],"parse_error")

    def test_exhaustive_algorithm_function_local_override_keeps_priority(self):
        pre=r"\usepackage{algorithm2e}\newcommand{\FMain}[1]{Local #1}"
        r=self._batch01_parse(r"\begin{algorithm}\FMain{$x$}\end{algorithm}",preamble=pre)
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        self.assertIn("Local $x$",self._batch07_content(r))
        r=self._batch01_parse(r"\begin{algorithm}\FMain{$x$}\end{algorithm}",
            preamble=pre+r"\SetKwFunction{FMain}{Other}")
        self.assertEqual(r["versions"][0]["status"],"parse_error")
        self.assertIn("order semantics",r["versions"][0]["error"])

    def test_exhaustive_algorithm_declarations_cannot_shadow_builtin_commands(self):
        for definition in (r"\SetKwFunction{Huge}{Lost}",r"\SetKwProg{subsubsection}{Lost}{:}{}",r"\SetKwInput{title}{Lost}"):
            r=self._batch01_parse(r"\begin{algorithm}"+definition+r"Keep.\end{algorithm}",preamble=r"\usepackage{algorithm2e}")
            self.assertEqual(r["versions"][0]["status"],"parse_error")

    def test_exhaustive_algorithm_alias_requires_algo2e_option(self):
        body=r"\begin{algorithm2e}\KwData{Data $x$}\Return{$x$}\end{algorithm2e}After."
        r=self._batch01_parse(body,preamble=r"\usepackage[algo2e]{algorithm2e}")
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        self.assertIn("Data $x$",self._batch07_content(r));self.assertIn("After.",self._batch07_content(r))
        self.assertEqual(self._batch01_parse(body,preamble=r"\usepackage{algorithm2e}")["versions"][0]["status"],"parse_error")


    def test_exhaustive_algorithm_declaration_does_not_leak_from_if_argument(self):
        pre=r"\usepackage{algorithm2e}"
        body=r"\begin{algorithm}\eIf{$x$}{\SetKwFunction{FMain}{Solve}\FMain{$x$}}{\FMain{$y$}}\end{algorithm}"
        r=self._batch01_parse(body,preamble=pre)
        self.assertEqual(r["versions"][0]["status"],"parse_error")
        self.assertIn(r"\FMain",r["versions"][0]["error"])


    def test_exhaustive_existing_displaybreak_metadata_remains_compatible(self):
        r=self._batch01_parse(r"\allowdisplaybreaks[2]Keep.",preamble=r"\usepackage{amsmath}")
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        m=next(m for m in r["versions"][0]["layout_metadata"] if m.get("command")=="allowdisplaybreaks")
        self.assertEqual(m,{"command":"allowdisplaybreaks","level":"2"})

    def test_exhaustive_existing_dash_normalization_is_not_shadowed(self):
        r=self._batch01_parse(r"Before\textemdash after\textendash next.")
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        self.assertIn("Before\u2014 after\u2013 next.",self._batch07_content(r))


    def test_exhaustive_relative_font_and_justification_keep_following_content(self):
        r=self._batch01_parse(r"\smaller[1]Small $x$.\larger More.\justifying After.",
            preamble=r"\usepackage{relsize,ragged2e}")
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        for phrase in ("Small $x$","More.","After."):self.assertIn(phrase,self._batch07_content(r))
        for pre,body in [("",r"\justifying Keep."),("",r"\smaller Keep."),
                         (r"\usepackage{relsize}",r"\smaller[\dynamic]Keep.")]:
            self.assertEqual(self._batch01_parse(body,preamble=pre)["versions"][0]["status"],"parse_error")

    def test_exhaustive_dot_below_accent_braced_and_single_token(self):
        r=self._batch01_parse(r"Ng\d{o}c and Ng\d oc.")
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        self.assertIn("Ng\u1ecdc and Ng\u1ecdc.",self._batch07_content(r))
        self.assertEqual(self._batch01_parse(r"\d{word}")["versions"][0]["status"],"parse_error")

    def test_exhaustive_safe_layout_registers_and_provider_checks(self):
        r=self._batch01_parse(r"\setlength{\arraycolsep}{3pt}\setlength{\fboxsep}{1pt}\setlength{\algomargin}{2em}\parindent=0pt Keep $x$.",
            preamble=r"\usepackage{algorithm2e}")
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        self.assertIn("Keep $x$.",self._batch07_content(r))
        for body,pre in [(r"\setlength{\algomargin}{2em}Keep.",""),
                         (r"\setlength{\arraycolsep}{\dynamic}Keep.",""),
                         (r"\parindent=\dynamic Keep.","")]:
            self.assertEqual(self._batch01_parse(body,preamble=pre)["versions"][0]["status"],"parse_error")

    def test_exhaustive_textbar_and_arxiv_metadata_are_preserved(self):
        r=self._batch01_parse(r"A\textbar{}B.\arxivnumber{2501.01234}\begin{abstract}Keep.\end{abstract}After.",
            preamble=r"\usepackage{jheppub}")
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        self.assertIn("A|B.",self._batch07_content(r))
        self.assertTrue(any(m.get("command")=="arxivnumber" and m.get("arguments")==["2501.01234"] for m in r["versions"][0]["layout_metadata"]))


    def test_exhaustive_acknowledgments_declaration_keeps_backmatter_and_appendix(self):
        for cls,pre in [("spie",""),("ametsocV61",""),("article",r"\usepackage{jheppub}"),
                        ("article",r"\usepackage{jcappub}")]:
            body=r"\section{Results}Science.\acknowledgments Funding \cite{k}.\section*{Appendix}Extra.\bibliography{r}"
            r=self._batch01_parse(body,document_class=cls,preamble=pre,files={"r.bib":"@article{k,title={Evidence}}"})
            self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
            for token in ("Science.","Funding",r"\cite{k}","Appendix","Extra."):self.assertIn(token,self._batch07_content(r))
            ack=next(n["element_id"] for n in r["elements"] if n["content"]=="acknowledgments")
            appendix=next(n["element_id"] for n in r["elements"] if n["kind"]=="heading" and n["content"]=="Appendix")
            root=next(n["element_id"] for n in r["elements"] if n["kind"]=="document")
            for node in (ack,appendix):self.assertTrue(any(e["element_id"]==node and e["parent_id"]==root for e in r["hierarchy"]))
        self.assertEqual(self._batch01_parse(r"\acknowledgments Unknown.")["versions"][0]["status"],"parse_error")
        r=self._batch01_parse(r"\acknowledgments{Evidence}.",preamble=r"\usepackage{jheppub}\newcommand{\acknowledgments}[1]{Local #1}")
        self.assertIn("Local Evidence",self._batch07_content(r))


    def test_exhaustive_rare_glyphs_preserve_names_quotes_and_visible_spaces(self):
        r=self._batch01_parse(r"M{\aa}nsson, M\oe{}glin \textquotedbl quoted\textquotedbl. A\textdagger{}B. the\textvisiblespace\textvisiblespace dog.")
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        for value in ("M\u00e5nsson", "M\u0153glin", '"quoted"', "A\u2020B", "the\u2423\u2423dog"):
            self.assertIn(value,self._batch07_content(r))

    def test_exhaustive_kernel_spaces_and_fussy_do_not_add_scientific_tokens(self):
        r=self._batch01_parse(r"A\enspace B\enskip C\negthinspace D.\fussy After.")
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        self.assertIn("A B CD.",self._batch07_content(r))
        self.assertIn("After.",self._batch07_content(r))

    def test_exhaustive_wiley_metadata_and_optional_abstract_preserve_science(self):
        body=r"\authormark{A \textsc{et al.}}\titlemark{Short}\corres{A \email{a@example.org}}\presentaddress{Institute}\abstract[Abstract]{Scientific $x$ \cite{k}.}\section{Intro}After.\bibliography{r}"
        r=self._batch01_parse(body,document_class="WileyNJDv5",files={"r.bib":"@article{k,title={Evidence}}"})
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        for value in ("Scientific $x$",r"\cite{k}","Intro","After."):self.assertIn(value,self._batch07_content(r))
        self.assertTrue(any(m.get("command")=="authormark" and m.get("arguments")==[r"A \textsc{et al.}"] for m in r["versions"][0]["layout_metadata"]))
        self.assertEqual(self._batch01_parse(r"\authormark{Unknown}")["versions"][0]["status"],"parse_error")
        self.assertEqual(self._batch01_parse(r"\abstract[\dynamic]{Keep}",document_class="WileyNJDv5")["versions"][0]["status"],"parse_error")


    def test_exhaustive_ametsoc_data_statement_and_appendix_are_peer_divisions(self):
        body=r"\section{Results}Science.\acknowledgments Funding.\datastatement Data \cite{k}.\appendix[A]\appendixtitle{Methodology}\subsection{Model}Extra $x$.\bibliography{r}"
        r=self._batch01_parse(body,document_class="ametsocV61",files={"r.bib":"@article{k,title={Evidence}}"})
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        for value in ("Science.","Funding.","Data",r"\cite{k}","Methodology","Model","Extra $x$"):
            self.assertIn(value,self._batch07_content(r))
        nodes={n["content"]:n["element_id"] for n in r["elements"]}
        root=next(n["element_id"] for n in r["elements"] if n["kind"]=="document")
        for content in ("acknowledgments","data availability statement","appendix A"):
            self.assertTrue(any(e["element_id"]==nodes[content] and e["parent_id"]==root for e in r["hierarchy"]))
        self.assertTrue(any(e["element_id"]==nodes["Methodology"] and e["parent_id"]==nodes["appendix A"] for e in r["hierarchy"]))
        for body in (r"\appendixtitle{Orphan}",r"\appendix[\dynamic]\appendixtitle{Bad}"):
            self.assertEqual(self._batch01_parse(body,document_class="ametsocV61")["versions"][0]["status"],"parse_error")


    def test_exhaustive_ametsoc_appendix_after_references_retains_body(self):
        r=self._batch01_parse(r"\section*{References}\appendix[A]\appendixtitle{Method}Keep $x$.",
            document_class="ametsocV61")
        self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
        self.assertIn("Keep $x$.",self._batch07_content(r))


    def test_exhaustive_algorithm_literal_group_declarations_fail_safely(self):
        body=r"\begin{algorithm}\SetKwFunction{FMain}{Outer}{\SetKwFunction{FMain}{Inner}\FMain{$x$}}\FMain{$y$}\end{algorithm}"
        r=self._batch01_parse(body,preamble=r"\usepackage{algorithm2e}")
        self.assertEqual(r["versions"][0]["status"],"parse_error")
        self.assertIn("scoped TeX semantics",r["versions"][0]["error"])


    def test_exhaustive_default_algorithm_labels_keep_existing_canonical_form(self):
        for label,expected in (("Input","input"),("Evidence","Evidence")):
            pre=r"\usepackage{algorithm2e}\SetKwInput{KwIn}{"+label+"}"
            r=self._batch01_parse(r"\begin{algorithm}\KwIn{Science $x$}\end{algorithm}",preamble=pre)
            self.assertEqual(r["versions"][0]["status"],"success",r["versions"])
            self.assertTrue(any(n["kind"]=="component" and n["content"]==expected for n in r["elements"]))
            self.assertIn("Science $x$",self._batch07_content(r))

    def test_cont03_macro_citations_recover_only_exact_invoked_bib_entries(self):
        result = self.parse({"main.tex": r"\documentclass{article}\newcommand{\late}[1]{\cite{#1}}\newcommand{\unused}{\cite{Absent}}\begin{document}Before. \late{ExactKey} After.\bibliography{refs}\end{document}",
            "refs.bib": "@article{ExactKey,title={Verified source title},author={Alice},year={2025}}\n@article{Unused,title={Unused title}}"})
        self.assertEqual(result['versions'][0]['status'], 'success')
        self.assertEqual([b['citation_key'] for b in result['bibtex']], ['ExactKey'])
        self.assertTrue(any(r'\cite{ExactKey}' in n['content'] for n in result['elements']))
        self.assertTrue(any('After.' in n['content'] for n in result['elements']))
        validate_tables(result)

    def test_cont03_late_citation_missing_case_and_version_are_not_invented(self):
        for index, key in enumerate(['Missing', 'exactkey']):
            d=self.paper({1:{'main.tex':r'\documentclass{article}\newcommand{\late}[1]{\cite{#1}}\begin{document}\late{'+key+r'}\bibliography{refs}\end{document}',
                              'refs.bib':'@article{ExactKey,title={Evidence}}'},
                          2:{'main.tex':tex(r'Valid \cite{'+key+r'}.\bibliography{refs}'),
                              'refs.bib':'@article{'+key+',title={Other version evidence}}'}},aid=f'2508.0{index+1:04d}')
            result=parse_versions(f'2508.0{index+1:04d}',[(1,d/'v1'),(2,d/'v2')])
            self.assertEqual([v['status'] for v in result['versions']], ['parse_error','success'])
            self.assertIn('after expansion', result['versions'][0]['error'])

    def test_cont03_late_citation_conflicting_duplicate_is_still_rejected(self):
        result=self.parse({'main.tex':r'\documentclass{article}\newcommand{\late}{\cite{key}}\begin{document}\late\bibliography{refs}\end{document}',
                           'refs.bib':'@article{key,title={One}}\n@article{key,title={Different}}'})
        self.assertEqual(result['versions'][0]['status'],'parse_error')
        self.assertIn('Ambiguous',result['versions'][0]['error'])

    def test_cont03_template_ack_environments_keep_body_and_following_section(self):
        for index, (cls, package, env) in enumerate([('article',r'\usepackage[preprint]{neurips_2025}','ack'),('ecai','','ack'),('achemso','','acknowledgement'),('cup-journal','','acknowledgement')]):
            result=self.parse({'main.tex':r'\documentclass{'+cls+'}'+package+r'\begin{document}\section{Main}First.\begin{'+env+r'}Funding retained. See \cite{ref}. $E=mc^2$.\end{'+env+r'}\section{After}After retained.\bibliography{refs}\end{document}',
                               'refs.bib':'@article{ref,title={Acknowledged source}}'},aid=f'2508.1{index:04d}')
            self.assertEqual(result['versions'][0]['status'],'success',result['versions'])
            self.assertTrue(any(n['content']=='acknowledgments' for n in result['elements']))
            self.assertTrue(any('Funding retained.' in n['content'] for n in result['elements']))
            self.assertTrue(any(n['content']=='After' for n in result['elements']))
            self.assertEqual(result['bibtex'][0]['citation_key'],'ref')
            validate_tables(result)

    def test_cont03_ack_custom_definition_options_unknown_provider_stay_blocked(self):
        for i,(decl,start) in enumerate([('',r'\begin{ack}'),(r'\usepackage{neurips_2025}\renewenvironment{ack}{Injected}{End}',r'\begin{ack}'),(r'\usepackage{neurips_2025}',r'\begin{ack}[Unknown]')]):
            result=self.parse({'main.tex':r'\documentclass{article}'+decl+r'\begin{document}'+start+r'Body.\end{ack}\end{document}'},aid=f'2508.2{i:04d}')
            self.assertEqual(result['versions'][0]['status'],'parse_error')

    def test_cont03_ifac_frontmatter_preserves_metadata_abstract_and_body(self):
        result=self.parse({'main.tex':r'\documentclass{ifacconf}\begin{document}\begin{frontmatter}\title{A title\thanksref{fund}}\thanks[fund]{Grant text}\author[one]{A. Author}\address[one]{An institute}\begin{abstract}Abstract retained.\end{abstract}\end{frontmatter}\section{Intro}Scientific content.\end{document}'})
        self.assertEqual(result['versions'][0]['status'],'success',result['versions'])
        content=' '.join(n['content'] for n in result['elements'])
        self.assertIn('Abstract retained.',content);self.assertIn('Scientific content.',content)
        self.assertNotIn('Grant text',content)
        self.assertTrue(any(x.get('command')=='thanks' and x.get('optional')=='fund' for x in result['versions'][0]['layout_metadata']))

    def test_cont03_ams_class_subequations_keeps_equations_and_version(self):
        result=self.parse({'main.tex':r'\documentclass{amsart}\begin{document}Before.\begin{subequations}\label{group}\begin{align}a&=b\\c&=d\end{align}\end{subequations}After.\end{document}'})
        self.assertEqual(result['versions'][0]['status'],'success',result['versions'])
        formula=next(n for n in result['elements'] if n['kind']=='block_formula')
        self.assertIn(r'a&=b\\c&=d',formula['content'])
        self.assertTrue(all(e['version']==1 for e in result['hierarchy']))

    def test_cont03_empheq_keeps_math_decorations_and_citations(self):
        for i,(option,inner) in enumerate([('','equation'),(r'[box=\fbox]','equation'),(r'[left=\empheqlbrace]','align')]):
            result=self.parse({'main.tex':r'\documentclass{article}\usepackage{empheq}\begin{document}Before.\begin{empheq}'+option+'{'+inner+r'}E=mc^2\label{mass}\text{See }\cite{ref}.\end{empheq}After.\bibliography{refs}\end{document}',
                               'refs.bib':'@article{ref,title={Equation source}}'},aid=f'2508.3{i:04d}')
            self.assertEqual(result['versions'][0]['status'],'success',result['versions'])
            formula=next(n for n in result['elements'] if n['kind']=='block_formula')
            self.assertIn('E=mc^2',formula['content']);self.assertIn(r'\cite{ref}',formula['content'])
            if 'left=' in option:self.assertIn(r'\left\{',formula['content'])
            self.assertTrue(any('After.' in n['content'] for n in result['elements']))
            validate_tables(result)

    def test_cont03_empheq_unknown_options_provider_or_local_definition_stay_blocked(self):
        for i, (package, option) in enumerate([('', ''),(r'\usepackage{empheq}', '[left=Dynamic]'),(r'\usepackage{empheq}\renewenvironment{empheq}{Changed}{End}','')]):
            result=self.parse({'main.tex':r'\documentclass{article}'+package+r'\begin{document}\begin{empheq}'+option+r'{equation}x=y\end{empheq}\end{document}'},aid=f'2508.4{i:04d}')
            self.assertEqual(result['versions'][0]['status'],'parse_error')

    def test_cont03_acmart_captionof_keeps_caption_and_body(self):
        result=self.parse({'main.tex':r'\documentclass{acmart}\begin{document}\captionof{table}{Visible $x$ and \cite{ref}}After.\bibliography{refs}\end{document}','refs.bib':'@article{ref,title={Caption source}}'})
        self.assertEqual(result['versions'][0]['status'],'success',result['versions'])
        self.assertTrue(any('Visible $x$' in n['content'] for n in result['elements']))
        self.assertTrue(any('After.' in n['content'] for n in result['elements']))

    def test_cont03_literal_newenvironment_overrides_are_expanded_not_discarded(self):
        for i,env in enumerate(['ack','empheq']):
            result=self.parse({'main.tex':r'\documentclass{article}\newenvironment{'+env+r'}{Injected prefix.}{Injected suffix.}\begin{document}\begin{'+env+r'}Scientific body.\end{'+env+r'}After.\end{document}'},aid=f'2508.5{i:04d}')
            self.assertEqual(result['versions'][0]['status'],'success',result['versions'])
            content=' '.join(n['content'] for n in result['elements'])
            for phrase in ['Injected prefix.','Injected suffix.','Scientific body.','After.']:self.assertIn(phrase,content)

    def test_cont03_body_lstset_uses_validation_and_keeps_all_code(self):
        result=self.parse({'main.tex':r'\documentclass{article}\usepackage{listings}\begin{document}\lstset{basicstyle=\small\ttfamily,columns=flexible,breaklines=true}\begin{lstlisting}[language=Python]'+'\nprint("first")\n'+r'\end{lstlisting}\lstset{numbers=left}\begin{lstlisting}'+'\nprint("second")\n'+r'\end{lstlisting}After.\end{document}'})
        self.assertEqual(result['versions'][0]['status'],'success',result['versions'])
        code=[n['content'] for n in result['elements'] if n['kind']=='verbatim']
        self.assertIn('print("first")',code[0]);self.assertIn('print("second")',code[1])
        configs=[x['options'] for x in result['versions'][0]['layout_metadata'] if x.get('environment')=='lstlisting']
        self.assertNotIn('numbers',configs[0]);self.assertEqual(configs[1]['numbers'],'left')

    def test_cont03_body_lstset_rejects_escapes_scope_and_does_not_leak_across_trees(self):
        for i,body in enumerate([r'\lstset{escapeinside={(*}{*)}}',r'{\lstset{numbers=left}}',r'\begingroup\lstset{numbers=left}\endgroup',r'\lstset{firstline=2}',r'\begin{quote}\lstset{numbers=left}\end{quote}']):
            result=self.parse({'main.tex':r'\documentclass{article}\usepackage{listings}\begin{document}'+body+r'\begin{lstlisting}'+'\nKEEP ALL\n'+r'\end{lstlisting}\end{document}'},aid=f'2508.6{i:04d}')
            self.assertEqual(result['versions'][0]['status'],'parse_error',body)

    def test_cont03_babel_english_records_activation_preserves_content_and_rejects_other_languages(self):
        result=self.parse({'main.tex':r'\documentclass{article}\usepackage[english]{babel}\begin{document}\selectlanguage{english}Text retained. $x=y$.\end{document}'})
        self.assertEqual(result['versions'][0]['status'],'success',result['versions'])
        self.assertTrue(any('Text retained.' in n['content'] for n in result['elements']))
        for i,decl in enumerate(['',r'\usepackage[english,french]{babel}']):
            language='english' if not decl else 'french'
            r=self.parse({'main.tex':r'\documentclass{article}'+decl+r'\begin{document}\selectlanguage{'+language+r'}Body.\end{document}'},aid=f'2508.7{i:04d}')
            self.assertEqual(r['versions'][0]['status'],'parse_error')

    def test_cont03_template_followup_containers_and_ifac_keywords_keep_all_text(self):
        for i,(cls,env,body) in enumerate([('achemso','suppinfo','Supporting data retained.'),('wiley-article','Practitionernotes',r'Known facts.\begin{itemize}\item First fact.\item Second fact.\end{itemize}'),('ifacconf','keyword',r'control\sep learning')]):
            result=self.parse({'main.tex':r'\documentclass{'+cls+r'}\begin{document}\begin{'+env+'}'+body+r'\end{'+env+r'}\section{After}Science retained.\end{document}'},aid=f'2508.8{i:04d}')
            self.assertEqual(result['versions'][0]['status'],'success',result['versions'])
            self.assertTrue(any('Science retained.' in n['content'] for n in result['elements']))
            if env=='Practitionernotes':self.assertEqual(sum(n['kind']=='item' for n in result['elements']),2)

    def test_cont03_proof_command_preserves_heading_math_citation_and_following_text(self):
        result=self.parse({'main.tex':r'\documentclass{article}\usepackage{amsthm,amsmath}\begin{document}\section{Claim}\proof[Proof of claim]We prove \cite{ref}.\begin{equation}x=y\end{equation}\endproof\section{After}Following text.\bibliography{refs}\end{document}','refs.bib':'@article{ref,title={Proof source}}'})
        self.assertEqual(result['versions'][0]['status'],'success',result['versions'])
        self.assertTrue(any(n['content']=='proof' for n in result['elements']))
        self.assertTrue(any('Proof of claim' in n['content'] for n in result['elements']))
        self.assertTrue(any(n['kind']=='block_formula' and 'x=y' in n['content'] for n in result['elements']))
        self.assertTrue(any('Following text.' in n['content'] for n in result['elements']))
        validate_tables(result)

    def test_cont03_proof_malformed_local_or_nested_remains_blocked(self):
        for i,(decl,body) in enumerate([(r'\usepackage{amsthm}',r'\proof Unclosed.'),(r'\usepackage{amsthm}',r'\proof One.\proof Two.\endproof\endproof'),(r'\usepackage{amsthm}\renewenvironment{proof}{Changed}{End}',r'\proof Body.\endproof'),('',r'\proof No provider.\endproof')]):
            result=self.parse({'main.tex':r'\documentclass{article}'+decl+r'\begin{document}'+body+r'\end{document}'},aid=f'2508.9{i:04d}')
            self.assertEqual(result['versions'][0]['status'],'parse_error')

    def test_cont03_list_color_groups_and_literal_color_macro_keep_all_items(self):
        for i,part in enumerate([r'{\color{blue}\item Second \cite{ref}.\item Third.}',r'\mycolor{\item Second \cite{ref}.\item Third.}']):
            result=self.parse({'main.tex':r'\documentclass{article}\usepackage{xcolor}\newcommand{\mycolor}[1]{{\color{black}#1}}\begin{document}\section{List}\begin{itemize}\item First.'+part+r'\item Fourth.\end{itemize}After.\bibliography{refs}\end{document}','refs.bib':'@article{ref,title={List source}}'},aid=f'2508.01{i:03d}')
            self.assertEqual(result['versions'][0]['status'],'success',result['versions'])
            self.assertEqual(sum(n['kind']=='item' for n in result['elements']),4)
            text=' '.join(n['content'] for n in result['elements'])
            for term in ['First.','Second','Third.','Fourth.','After.']:self.assertIn(term,text)
            self.assertEqual(result['bibtex'][0]['citation_key'],'ref')
            validate_tables(result)

    def test_cont03_list_projection_does_not_expose_opaque_arguments_nested_lists_or_verbatim(self):
        result=self.parse({'main.tex':tex(r'\begin{itemize}\item First \footnote{Example \verb|\item|}.\begin{itemize}\item Nested.\end{itemize}\item Second.\end{itemize}')})
        self.assertEqual(result['versions'][0]['status'],'success',result['versions'])
        self.assertEqual(sum(n['kind']=='item' for n in result['elements']),3)

    def test_cont03_list_item_group_with_state_changes_remains_blocked(self):
        result=self.parse({'main.tex':tex(r'\begin{enumerate}\item First.{\setcounter{enumi}{9}\item Tenth.}\end{enumerate}')})
        self.assertEqual(result['versions'][0]['status'],'parse_error')
        self.assertIn('scoped TeX semantics',result['versions'][0]['error'])

    def test_cont03_ifac_definition_keeps_optional_title_equation_and_following_text(self):
        result=self.parse({'main.tex':r'\documentclass{ifacconf}\usepackage{amsmath}\begin{document}\begin{defn}[Distance]A metric.\begin{equation}d(x,y)=0\end{equation}\end{defn}After.\end{document}'})
        self.assertEqual(result['versions'][0]['status'],'success',result['versions'])
        self.assertTrue(any(n['content']=='definition' for n in result['elements']))
        self.assertTrue(any('Distance' in n['content'] for n in result['elements']))
        self.assertTrue(any(n['kind']=='block_formula' and 'd(x,y)=0' in n['content'] for n in result['elements']))

    def test_cont03_ifac_theorem_lemma_proof_aliases_keep_nested_science(self):
        result=self.parse({'main.tex':r'\documentclass{ifacconf}\usepackage{amsmath}\begin{document}\begin{thm}[Claim]Theorem text.\end{thm}\begin{lem}Lemma text.\end{lem}\begin{pf}Proof text.\begin{equation}a=b\end{equation}\end{pf}Following.\end{document}'})
        self.assertEqual(result['versions'][0]['status'],'success',result['versions'])
        labels={n['content'] for n in result['elements'] if n['kind']=='component'}
        self.assertTrue({'theorem','lemma','proof'} <= labels)
        self.assertTrue(any('Following.' in n['content'] for n in result['elements']))
        self.assertTrue(any(n['kind']=='block_formula' and 'a=b' in n['content'] for n in result['elements']))
        validate_tables(result)

    def test_cont03_ifac_remark_and_ack_complete_template_chain(self):
        result=self.parse({'main.tex':r'\documentclass{ifacconf}\begin{document}\begin{rem}[Observation]Remark science. $x=y$.\end{rem}\begin{ack}Funding retained.\end{ack}After.\end{document}'})
        self.assertEqual(result['versions'][0]['status'],'success',result['versions'])
        self.assertTrue(any(n['content']=='remark' for n in result['elements']))
        content=' '.join(n['content'] for n in result['elements'])
        for term in ['Observation','Remark science.','Funding retained.','After.']:self.assertIn(term,content)

    def test_cont04_cite_prefixed_local_macro_locator_is_not_a_bib_key(self):
        result = self.parse({"main.tex": r"\documentclass{article}\newcommand{\citeSP}[1]{\cite[Locator #1]{stacks}}\begin{document}Before. \citeSP{0AU3} After.\bibliography{refs}\end{document}",
                             "refs.bib": "@article{stacks,title={Verified reference}}"})
        self.assertEqual(result['versions'][0]['status'], 'success', result['versions'])
        self.assertEqual([b['citation_key'] for b in result['bibtex']], ['stacks'])
        text = ' '.join(n['content'] for n in result['elements'])
        self.assertIn(r'\cite[Locator 0AU3]{stacks}', text)
        self.assertIn('Before.', text); self.assertIn('After.', text)
        validate_tables(result)

    def test_cont04_cite_prefixed_local_crossref_is_not_a_bibliographic_reference(self):
        result = self.parse({"main.tex": r"\documentclass{article}\newcommand{\citeSI}[1]{(\ref{#1})}\begin{document}\section{Evidence}\label{sec:evidence}Read \citeSI{sec:evidence}.\end{document}"})
        self.assertEqual(result['versions'][0]['status'], 'success', result['versions'])
        self.assertEqual(result['bibtex'], [])
        self.assertIn(r'\ref{sec:evidence}', ' '.join(n['content'] for n in result['elements']))

    def test_cont04_deferred_citation_optional_argument_and_missing_entry_stay_checked(self):
        for i, invocation in enumerate([r'\citeLocator{7}', r'\citeLocator[Other]{8}']):
            result = self.parse({'main.tex': r'\documentclass{article}\newcommand{\citeLocator}[2][Default]{\cite[#1 #2]{Missing}}\begin{document}'+invocation+r'\bibliography{refs}\end{document}',
                                 'refs.bib': '@article{Other,title={Unrelated}}'}, aid=f'2508.2{i:04d}')
            self.assertEqual(result['versions'][0]['status'], 'parse_error')
            self.assertIn('after expansion: Missing', result['versions'][0]['error'])

    def test_cont04_local_citation_missing_argument_dynamic_or_cycle_remains_error(self):
        cases = [(r'\newcommand{\citeWrap}[1]{\cite{#1}}',r'\citeWrap'),
                 (r'\newcommand{\citeWrap}[1]{\citeWrap{#1}}',r'\citeWrap{k}'),
                 (r'\def\citeWrap#1;{\cite{#1}}',r'\citeWrap k;')]
        for i,(definition,body) in enumerate(cases):
            result=self.parse({'main.tex':r'\documentclass{article}'+definition+r'\begin{document}'+body+r'\bibliography{refs}\end{document}','refs.bib':'@article{k,title={Evidence}}'},aid=f'2508.3{i:04d}')
            self.assertEqual(result['versions'][0]['status'],'parse_error')

    def test_cont04_local_cite_builtin_redefinition_and_nocite_wildcard(self):
        for i, (decl, body, expected) in enumerate([
                (r'\renewcommand{\cite}[1]{\ref{#1}}',r'See \cite{sec:x}.',set()),
                (r'\newcommand{\citeAll}{\nocite{*}}',r'\citeAll',{'a','b'})]):
            result=self.parse({'main.tex':r'\documentclass{article}'+decl+r'\begin{document}\section{Main}\label{sec:x}'+body+r'\bibliography{refs}\end{document}','refs.bib':'@article{a,title={One}}\n@article{b,title={Two}}'},aid=f'2508.4{i:04d}')
            self.assertEqual(result['versions'][0]['status'],'success',result['versions'])
            self.assertEqual({b['citation_key'] for b in result['bibtex']},expected)

    def test_cont04_acmart_citestyle_records_style_without_inventing_reference(self):
        for i,style in enumerate(['acmnumeric','acmauthoryear']):
            result=self.parse({'main.tex':r'\documentclass{acmart}\begin{document}Before \cite{k}.\citestyle{'+style+r'}After.\bibliography{refs}\end{document}','refs.bib':'@article{k,title={Evidence}}'},aid=f'2508.5{i:04d}')
            self.assertEqual(result['versions'][0]['status'],'success',result['versions'])
            self.assertEqual([b['citation_key'] for b in result['bibtex']],['k'])
            self.assertIn({'command':'citestyle','arguments':[style]},result['versions'][0]['layout_metadata'])
            self.assertIn('After.', ' '.join(n['content'] for n in result['elements']))

    def test_cont04_citestyle_unknown_provider_style_and_local_override_are_not_ignored(self):
        for i,(cls,pre,body) in enumerate([('article','',r'\citestyle{acmnumeric}'),('acmart','',r'\citestyle{unknown}'),('acmart',r'\newcommand{\citestyle}[1]{Local #1}',r'\citestyle{Evidence}')]):
            result=self.parse({'main.tex':r'\documentclass{'+cls+'}'+pre+r'\begin{document}'+body+r'\end{document}'},aid=f'2508.6{i:04d}')
            if pre:
                self.assertEqual(result['versions'][0]['status'],'success')
                self.assertIn('Local Evidence',' '.join(n['content'] for n in result['elements']))
            else:self.assertEqual(result['versions'][0]['status'],'parse_error')

    def test_cont04_associated_bbl_grouped_bibitem_preserves_key_fields_and_citation(self):
        bbl = r"\begin{thebibliography}{9}\providecommand{\newblock}{\relax}\bibitem{first}Alice.\newblock First reference.\newblock 2020.{\bibitem{grouped}Bob.\newblock Grouped evidence.\newblock 2025.}\end{thebibliography}"
        result=self.parse({'main.tex':tex(r'See \cite{grouped}. After.\bibliography{missing}'),'main.bbl':bbl})
        self.assertEqual(result['versions'][0]['status'],'success',result['versions'])
        row=next(b for b in result['bibtex'] if b['citation_key']=='grouped')
        self.assertEqual(row['title'],'Grouped evidence');self.assertEqual(row['year'],2025)
        self.assertTrue(any(r'\cite{grouped}' in n['content'] for n in result['elements']))
        self.assertFalse(any('Grouped evidence' in n['content'] for n in result['elements']))
        validate_tables(result)

    def test_cont04_bbl_group_cannot_cross_version_or_unrelated_jobname(self):
        bbl=r'\begin{thebibliography}{9}\bibitem{first}First.{\bibitem{k}Evidence.}\end{thebibliography}'
        d=self.paper({1:{'main.tex':tex(r'See \cite{k}.\bibliography{missing}'),'unrelated.bbl':bbl},
                      2:{'main.tex':tex(r'See \cite{k}.\bibliography{missing}'),'main.bbl':bbl}})
        result=parse_versions(AID,[(1,d/'v1'),(2,d/'v2')])
        self.assertEqual([v['status'] for v in result['versions']],['parse_error','success'])
        self.assertTrue(all(e['version']==2 for e in result['hierarchy']))

    def test_cont04_bbl_group_declarations_conditionals_and_unclosed_stay_rejected(self):
        tails=[r'{\newcommand{\hidden}{\bibitem{k}Fake.}\hidden}',
               r'\iffalse{\bibitem{k}Inactive.}\fi',r'{\bibitem{k}Unclosed.']
        for i,tail in enumerate(tails):
            result=self.parse({'main.tex':tex(r'\cite{k}\bibliography{missing}'),
                               'main.bbl':r'\begin{thebibliography}{9}\bibitem{first}Real.'+tail+r'\end{thebibliography}'},aid=f'2508.7{i:04d}')
            self.assertEqual(result['versions'][0]['status'],'parse_error')

    def test_cont04_bbl_group_conflicting_key_is_not_silently_merged(self):
        result=self.parse({'main.tex':tex(r'\cite{k}\bibliography{missing}'),
                           'main.bbl':r'\begin{thebibliography}{9}\bibitem{k}Alice.\newblock One. {\bibitem{k}Bob.\newblock Different.}\end{thebibliography}'})
        self.assertEqual(result['versions'][0]['status'],'parse_error')
        self.assertIn('Ambiguous',result['versions'][0]['error'])

    def test_cont04_nocite_wildcard_revealed_by_non_cite_macro_keeps_all_entries(self):
        result=self.parse({'main.tex':r'\documentclass{article}\newcommand{\allReferences}{\nocite{*}}\begin{document}Before.\allReferences After.\bibliography{refs}\end{document}',
                           'refs.bib':'@article{a,title={One}}\n@article{b,title={Two}}'})
        self.assertEqual(result['versions'][0]['status'],'success',result['versions'])
        self.assertEqual({b['citation_key'] for b in result['bibtex']},{'a','b'})
        self.assertIn('After.',' '.join(n['content'] for n in result['elements']))


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
