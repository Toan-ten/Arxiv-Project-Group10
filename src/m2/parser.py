"""Compilation-path hierarchy parser; never executes TeX or external commands."""
from __future__ import annotations

import hashlib
import re
import unicodedata
import xml.etree.ElementTree as ET
from collections import Counter

from .bibliography import parse_bbl, parse_bibitems, parse_bibtex, unify
from .latex import Gatherer, ParseError, citation_keys, command, environment, group, mask_literals, normalize, options, listing_options, sentences, skip_space, splice_tokens


LEVELS = {"part": 0, "chapter": 1, "section": 2, "subsection": 3,
          "subsubsection": 4, "paragraph": 5, "subparagraph": 6}
MATH_ENVS = {"equation", "equation*", "align", "align*", "alignat", "alignat*",
             "gather", "gather*", "multline", "multline*", "displaymath", "eqnarray", "eqnarray*"}
FIGURE_ENVS = {"figure", "figure*", "table", "table*", "longtable", "tabular", "tabular*", "tikzpicture"}
CONTAINERS = {"abstract", "center", "quote", "quotation", "flushleft", "flushright",
              "theorem", "lemma", "proof", "proposition", "corollary", "definition",
              "remark", "example", "algorithm", "algorithmic", "algorithmicx",
              "acknowledgments", "acknowledgements", "acks", "appendices", "appendix"}
KEEP_COMMANDS = {"url", "href", "path", "ref", "eqref", "autoref", "cref", "Cref",
                 "footnote", "footnotetext", "thanks", "textsuperscript", "textsubscript", "ensuremath"}
FORMATTING = {"textbf", "textit", "emph", "textrm", "textsf", "texttt", "underline", "mbox",
              "textsc", "textnormal", "textup", "textsl", "rlap", "enquote", "textquote", "keywords"}
DROP = {"maketitle", "newpage", "clearpage", "pagebreak", "noindent", "centering", "raggedright",
        "raggedleft", "small", "footnotesize", "normalsize", "large", "Large", "bfseries",
        "itshape", "rm", "em", "appendix", "tableofcontents", "printbibliography",
        "sloppy", "protect", "hfill", "vfill", "medskip", "smallskip", "bigskip", "qedhere",
        "begingroup", "endgroup", "footnotemark", "authornotemark", "xspace", "nocopyright", "and",
        "bf", "it", "tt", "sf", "onecolumn", "makeatletter", "makeatother",
        "huge", "LARGE", "selectfont", "upshape", "tiny", "scriptsize", "sc", "rmfamily"}
DROP_ARGUMENT = {"label", "bibliographystyle", "date", "pagestyle", "thispagestyle", "index",
                 "vspace", "hspace", "addcontentsline", "setcounter", "addtocounter",
                 "title", "author", "affiliation", "institute", "address", "email",
                 "titlerunning", "authorrunning", "authornote", "orcid", "captionsetup", "color", "fontsize"}

SI_UNIT_COMMANDS = {"micro", "mu", "milli", "centi", "nano", "pico", "kilo", "mega", "giga",
                         "metre", "meter", "second", "hertz", "gram", "kilogram", "kelvin", "tesla",
                         "volt", "electronvolt", "joule", "watt", "ampere", "pascal", "percent", "%",
                         "per", "squared", "cubed", "square", "cubic", "degree", "degreeCelsius"}

# Documented by cvpr-org/author-kit, cvpr.sty (onedot definitions). Local
# definitions take precedence. This is not a general package interpreter.
CVPR_ABBREVIATIONS = {"etc": "etc.", "ie": "i.e."}


def bibliography(gatherer, body):
    entries = []
    pattern = re.compile(r"\\begin\s*\{thebibliography\}")
    while (match := pattern.search(mask_literals(body))):
        start = match.end()
        _, start = group(body, start)  # widest label argument
        raw, end = environment(body, start, "thebibliography")
        # Only exact source-defined, zero-argument aliases of \bibitem are
        # structural markers. Do not expand arbitrary bibliography macros or
        # commands occurring inside a field's balanced group.
        aliases = {name for name, definition in gatherer.macros.items()
                   if definition == (0, None, r"\bibitem")}
        if aliases:
            structural = "|".join(re.escape(n) for n in aliases | {"bibitem"})
            if re.search(r"\\(?:def|gdef|edef|xdef|let|newcommand|renewcommand|providecommand|DeclareRobustCommand)\s*\{?\s*\\(?:" + structural + r")(?![A-Za-z@])", raw):
                raise ParseError("Local bibliography marker redefinition requires scoped TeX semantics")
            rewritten, cursor = [], 0
            while cursor < len(raw):
                if raw[cursor] == "{":
                    _, stop = group(raw, cursor)
                elif raw[cursor] == "\\":
                    token, stop = command(raw, cursor)
                    if token in aliases:
                        rewritten.append(r"\bibitem")
                        gatherer.math_aliases_used.add("Bibliography marker alias from source: " + token + " -> bibitem")
                        cursor = stop
                        continue
                else:
                    stop = cursor + 1
                rewritten.append(raw[cursor:stop])
                cursor = stop
            raw = "".join(rewritten)
        if re.search(r"\\(?:if[a-zA-Z]*|expandafter|else|fi)\b", raw):
            entries.extend(parse_bbl(r"\begin{thebibliography}{99}" + raw + r"\end{thebibliography}"))
        else:
            entries.extend(parse_bibitems(raw))
        body = body[:match.start()] + "\n" + body[end:]
    citations = citation_keys(body)
    inline_keys = {e["key"] for e in entries}
    strings = {}
    for path in gatherer.bib_files:
        # BibTeX percentages inside a braced URL/abstract are data, not TeX comments.
        entries.extend(parse_bibtex(gatherer.read(path, comments=False), strings))
    missing = set(citations) - {"*"} - {e["key"] for e in entries}
    needs_bbl = bool(missing or gatherer.missing_bib_resources and (
        not inline_keys or "*" in citations))
    if needs_bbl and not gatherer.bbl_files:
        gatherer.fallback_bbl()
    for path in gatherer.bbl_files:
        try:
            rendered = parse_bbl(gatherer.read(path, comments=False), enriched=True)
        except ParseError as exc:
            raise ParseError(f"{path.relative_to(gatherer.root).as_posix()}: {exc}") from exc
        relative = path.relative_to(gatherer.root).as_posix()
        known = {e["key"] for e in entries}
        for entry in rendered:
            entry["source_file"] = relative
            # Verified .bib/inline fields take precedence; .bbl supplies absent keys.
            if entry["key"] not in known:
                entries.append(entry)
    if needs_bbl and gatherer.missing_bib_resources and not gatherer.bbl_files:
        raise ParseError(gatherer.missing_bib_resources[0]["error"] +
                         "; no .bbl associated with document root")
    # Manually rendered bibitems are included even without a \cite invocation.
    entries = [e for e in entries if e["provider"].startswith("bibitem") or "*" in citations or e["key"] in citations]
    missing = sorted(set(citations) - {"*"} - {e["key"] for e in entries})
    return body, entries, missing


class Tree:
    def __init__(self, aid, version, gatherer, aliases):
        self.aid, self.version, self.gatherer, self.aliases = aid, version, gatherer, aliases
        self.nodes, self.edges, self.warnings = [], [], []
        self.occurrences = Counter()
        self.root = self.add("document", f"Version {version} root", None, root=True)
        self.expansions = 0
        self.local_footnote_renewal = False
        self.layout_metadata = []
        self.pseudocode_depth = 0
        self.algorithm2e_depth = 0
        self.algorithm_keywords = {}
        self.frontmatter_depth = 0

    def add(self, kind, content, parent, root=False, literal=False):
        content = content if literal else normalize(content, self.aliases)
        if not content and not root:
            return parent
        signature = kind + "\0" + content
        self.occurrences[signature] += 1
        suffix = (f"root{self.version}" if root else
                  hashlib.sha256(signature.encode()).hexdigest()[:24] + f"-{self.occurrences[signature]}")
        eid = self.aid.replace(".", "-").replace("/", "-") + "#" + suffix
        self.nodes.append(dict(element_id=eid, arxiv_id=self.aid, kind=kind, content=content))
        if parent is not None:
            self.edges.append(dict(arxiv_id=self.aid, version=self.version, element_id=eid, parent_id=parent))
        return eid

    def parse(self, text, parent=None, depth=0):
        if depth > 60:
            raise ParseError("Hierarchy recursion exceeds 60")
        parent = parent or self.root
        headings, buffer, pos, reference_section, reference_rank = [], [], 0, False, None
        # New declaration syntax is only interpreted before top-level content.
        # In particular, do not hide an affiliation-like command in body prose.
        front_matter = depth == 0 and parent == self.root

        def current():
            return headings[-1][1] if headings else parent

        def flush():
            nonlocal front_matter
            if buffer and not reference_section:
                cleaned = normalize("".join(buffer), self.aliases)
                if cleaned:
                    front_matter = False
                for sentence in sentences(cleaned):
                    self.add("sentence", sentence, current())
            buffer.clear()

        while pos < len(text):
            if text[pos] == "{":
                # A TeX scope may span display math/environments. Remove its grouping
                # without treating it as a prose brace that must close before a leaf.
                value, end = group(text, pos)
                text = splice_tokens(text, pos, value, end)
                continue
            if text[pos] == "}":
                raise ParseError("Unmatched TeX scope")
            if text.startswith("$$", pos) or text.startswith(r"\[", pos):
                front_matter = False
                flush()
                token = "$$" if text.startswith("$$", pos) else r"\["
                closing = "$$" if token == "$$" else r"\]"
                end = text.find(closing, pos + len(token))
                if end < 0:
                    raise ParseError("Unclosed display math")
                if not reference_section:
                    self.add("block_formula", r"\begin{equation}" + text[pos + len(token):end].strip() + r"\end{equation}", current())
                pos = end + len(closing)
                continue
            if text[pos] == "$" or text.startswith(r"\(", pos):
                front_matter = False
                opening = "$" if text[pos] == "$" else r"\("
                closing = "$" if opening == "$" else r"\)"
                end, braces = pos + len(opening), 0
                while end < len(text):
                    if not braces and text.startswith(closing, end):
                        break
                    if text[end] == "\\":
                        _, end = command(text, end)
                        continue
                    braces += int(text[end] == "{") - int(text[end] == "}")
                    if braces < 0:
                        raise ParseError("Unbalanced inline math braces")
                    end += 1
                else:
                    raise ParseError("Unclosed inline math")
                buffer.append("$" + text[pos + len(opening):end].strip() + "$")
                pos = end + len(closing)
                continue
            if text[pos] != "\\":
                if not text[pos].isspace():
                    front_matter = False
                buffer.append(text[pos])
                pos += 1
                continue
            name, end = command(text, pos)
            if name in LEVELS or (name == "bmhead" and name not in self.gatherer.macros):
                front_matter = False
                flush()
                if text[end:end + 1] == "*":
                    end += 1
                end = options(text, end)
                title, end = group(text, end)
                # Springer back-matter headings are unnumbered peers of
                # sections, not prose to discard or children of Conclusions.
                rank = LEVELS.get(name, LEVELS["section"])
                while headings and headings[-1][0] >= rank:
                    headings.pop()
                nested_reference = reference_section and reference_rank is not None and rank > reference_rank
                is_reference_heading = bool(re.fullmatch(r"(?:references|bibliography|references and notes)", normalize(title), re.I))
                reference_section = nested_reference or is_reference_heading
                if is_reference_heading:
                    reference_rank = rank
                elif not reference_section:
                    reference_rank = None
                if not reference_section:
                    eid = self.add("heading", title, current())
                    headings.append((rank, eid))
            elif (name == "subjclass" and name not in self.gatherer.macros
                  and self.gatherer.document_class in {"amsart", "amsproc", "amsbook", "tsp-short"}):
                # AMS title metadata, not a prose macro to discard. Preserve
                # both the optional classification edition and its actual codes.
                # Local definitions still take precedence over this convention.
                if depth != 0:
                    raise ParseError("Nested AMS subject classification is unsupported")
                flush()
                start = skip_space(text, end)
                edition = None
                if text[start:start + 1] == "[":
                    edition, start = group(text, start, "[", "]")
                raw, end = group(text, start)
                label = "subject classification" + (" (" + edition + ")" if edition is not None else "")
                container = self.add("component", label, current())
                self.parse(raw, container, depth + 1)
            elif name == "abstract" and name not in self.gatherer.macros:
                front_matter = False
                flush()
                start = skip_space(text, end)
                if self.gatherer.document_class == "svmult" and text[start:start + 1] == "*":
                    start += 1  # Springer unheaded abstract; retain its full body.
                raw, end = group(text, start)
                if not reference_section:
                    container = self.add("component", "abstract", current())
                    self.parse(raw, container, depth + 1)
            elif name in {"icmltitle", "icmlaffiliation", "icmlcorrespondingauthor", "icmlsetsymbol", "icmlkeywords"} and any(p.startswith("icml") for p in self.gatherer.packages) and name not in self.gatherer.macros:
                args, start = [], end
                for _ in range(2 if name in {"icmlaffiliation", "icmlcorrespondingauthor", "icmlsetsymbol"} else 1):
                    value, start = group(text, start)
                    args.append(value)
                end = start
                self.layout_metadata.append(dict(command=name, arguments=args))
            elif name == "link" and any(p.startswith("aaai") for p in self.gatherer.packages) and name not in self.gatherer.macros:
                label, start = group(text, end)
                target, end = group(text, start)
                buffer.append(r"\href{" + target + "}{" + label + "}")
            elif name == "thebiography" and self.gatherer.document_class == "IEEEAerospaceCLS" and name not in self.gatherer.macros:
                flush()
                headings.clear()
                self.layout_metadata.append(dict(command=name))
            elif name == "ding" and "pifont" in self.gatherer.packages and name not in self.gatherer.macros:
                value, end = group(text, end)
                if not value.strip().isdigit() or not 0 <= int(value) <= 255:
                    raise ParseError("Unsupported nonliteral pifont symbol")
                buffer.append(text[pos:end])
            elif re.fullmatch(r"fa[A-Z][A-Za-z]+", name) and self.gatherer.packages & {"fontawesome", "fontawesome5"} and name not in self.gatherer.macros:
                # Retain the exact icon command, never replace it by an invented
                # Unicode mapping or erase an icon that conveys information.
                buffer.append("\\" + name + "{}")
                end = skip_space(text, end)
            elif name == "printcredits" and self.gatherer.document_class in {"cas-dc", "cas-sc"} and name not in self.gatherer.macros:
                self.layout_metadata.append(dict(command=name))
            elif (name in {"flushbottom", "normalfont", "onecolumngrid"}
                  and name not in self.gatherer.macros):
                if name == "onecolumngrid" and not (self.gatherer.document_class or "").startswith("revtex"):
                    raise ParseError("Undeclared REVTeX grid command: " + name)
                # Font selection is a declaration, not a one-argument command.
                # Following groups, math and citations remain in the parse stream.
                if name == "normalfont":
                    end = skip_space(text, end)
                else:
                    buffer.append(" ")
                self.layout_metadata.append(dict(command=name))
            elif name == "definecolor" and name not in self.gatherer.macros:
                start = skip_space(text, end)
                kind = None
                if text[start:start + 1] == "[":
                    kind, start = group(text, start, "[", "]")
                    if kind.strip() != "named":
                        raise ParseError("Unsupported definecolor type: " + kind)
                color, start = group(text, start)
                model, start = group(text, start)
                spec, end = group(text, start)
                if not re.fullmatch(r"[A-Za-z][A-Za-z0-9:_-]*", color.strip()):
                    raise ParseError("Unsupported nonliteral color name: " + color)
                model = model.strip()
                if model == "named":
                    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9:_-]*", spec.strip()):
                        raise ParseError("Unsupported nonliteral named color: " + spec)
                else:
                    models = {"rgb": (3, 1), "RGB": (3, 255), "gray": (1, 1), "cmyk": (4, 1)}
                    if model not in models:
                        raise ParseError("Unsupported definecolor model: " + model)
                    count, maximum = models[model]
                    values = [v.strip() for v in spec.split(",")]
                    if (len(values) != count or any(
                        not re.fullmatch(r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)", v)
                        or not 0 <= float(v) <= maximum for v in values)):
                        raise ParseError("Unsupported nonliteral/out-of-range color specification: " + spec)
                self.layout_metadata.append(dict(command=name, type=kind, color=color, model=model, specification=spec))
            elif (name == "altaffiliation" and name not in self.gatherer.macros
                  and (self.gatherer.document_class or "").startswith("revtex")):
                if not front_matter or depth != 0 or "".join(buffer).strip():
                    raise ParseError("REVTeX altaffiliation outside front matter is unsupported")
                start = skip_space(text, end)
                prefix = None
                if text[start:start + 1] == "[":
                    prefix, start = group(text, start, "[", "]")
                value, end = group(text, start)
                # Like author/affiliation, retain this as front-matter evidence,
                # not scientific prose; do not consume the following abstract.
                self.layout_metadata.append(dict(command=name, prefix=prefix, arguments=[value]))
            elif (name == "IEEEpeerreviewmaketitle" and name not in self.gatherer.macros
                  and self.gatherer.document_class == "IEEEtran"):
                # IEEEtran's repeated title/page-break command has no argument.
                front_matter = False
                self.layout_metadata.append(dict(command=name))
                buffer.append(" ")
            elif (name == "qed" and name not in self.gatherer.macros
                  and ("amsthm" in self.gatherer.packages or self.gatherer.document_class in {"amsart", "amsproc", "amsbook"})):
                # Keep the explicit ending marker, not an invented glyph. A
                # source-defined qedsymbol may change its visual representation.
                self.layout_metadata.append(dict(command=name))
                if "qedsymbol" in self.gatherer.macros:
                    # A custom ending may contain actual words or mathematics.
                    # Route it through ordinary macro/content validation.
                    text = splice_tokens(text, pos, r"\qedsymbol{}", end)
                    continue
                buffer.append(r"\qed{}")
                end = skip_space(text, end)
            elif name == "hyperref" and name not in self.gatherer.macros and "hyperref" in self.gatherer.packages:
                start = skip_space(text, end)
                if text[start:start + 1] == "[":
                    target, start = group(text, start, "[", "]")
                    if not target.strip() or re.search(r"[{}\\]", target):
                        raise ParseError("Unsupported nonliteral hyperref label: " + target)
                    targets = [target]
                    form = "label"
                else:
                    targets = []
                    for _ in range(3):
                        value, start = group(text, start)
                        targets.append(value)
                    form = "url_category_name"
                visible, end = group(text, start)
                self.layout_metadata.append(dict(command=name, form=form, targets=targets, visible=visible))
                # Reparse the complete displayed text, including nested math,
                # local macros and citations. The destination is not prose.
                text = splice_tokens(text, pos, visible, end)
                continue
            elif (name == "onlinecite" and name not in self.gatherer.macros
                  and not (self.gatherer.document_class or "").startswith("revtex")):
                raise ParseError("Undeclared REVTeX citation command: onlinecite")
            elif (name == "maketitlesupplementary" and name not in self.gatherer.macros
                  and "cvpr" in self.gatherer.packages):
                # CVPR renders a repeated paper title plus this fixed division
                # label. The existing title-metadata policy omits the repeat,
                # but keep the supplement boundary and ALL following content.
                flush()
                front_matter = reference_section = False
                reference_rank = None
                headings.clear()
                marker = self.add("heading", "Supplementary Material", self.root)
                headings.append((LEVELS["section"], marker))
                self.layout_metadata.append(dict(command=name, convention="cvpr", repeated_title="document title metadata"))
                self.warnings.append("CVPR supplementary title convention; original style file not interpreted")
            elif name == "-" and name not in self.gatherer.macros:
                # Discretionary word-break opportunity: no visible character
                # in normalized continuous text. Do not add a space or consume
                # any following actual hyphen/whitespace.
                pass
            elif name == "uppercase" and name not in self.gatherer.macros:
                start = skip_space(text, end)
                expanded = False
                if text[start:start + 1] == "\\":
                    token, after = command(text, start)
                    if token != "expandafter":
                        raise ParseError("Unsupported uppercase token expansion: " + token)
                    start, expanded = after, True
                value, end = group(text, start)
                if expanded:
                    literal = re.fullmatch(r"\s*\\romannumeral\s*([+-]?[0-9]+)\s*(?:\\relax\s*)?", value)
                    if not literal or not -3999 <= int(literal[1]) <= 3999:
                        raise ParseError("Unsupported uppercase Roman expression: literal integer required")
                    number, rendered = max(0, int(literal[1])), ""
                    for unit, letters in ((1000,"M"),(900,"CM"),(500,"D"),(400,"CD"),(100,"C"),(90,"XC"),(50,"L"),(40,"XL"),(10,"X"),(9,"IX"),(5,"V"),(4,"IV"),(1,"I")):
                        count, number = divmod(number, unit)
                        rendered += letters * count
                    buffer.append(rendered)
                elif value.isascii() and not re.search(r"[\\$]", value):
                    text = splice_tokens(text, pos, value.upper(), end)
                    continue
                else:
                    # Case-changing arbitrary TeX tokens can alter citation
                    # keys, labels or mathematics. Require separate semantics.
                    raise ParseError("Unsupported uppercase content: commands/math or non-ASCII tokens")
            elif (name == "kwd" and self.frontmatter_depth and self.gatherer.document_class == "imsart"
                  and name not in self.gatherer.macros):
                value, end = group(text, end)
                text = splice_tokens(text, pos, value + "; ", end)
                continue
            elif name == "twocolumn" and name not in self.gatherer.macros:
                flush()
                start = skip_space(text, end)
                if text[start:start + 1] == "[":
                    raw, end = group(text, start, "[", "]")
                    self.parse(raw, current(), depth + 1)
                self.layout_metadata.append(dict(command=name))
            elif name == "backmatter" and name not in self.gatherer.macros:
                # This phase marker has no text argument and does not end the
                # document. Keep subsequent headings, notes and appendices.
                front_matter = False
                flush()
            elif name == "begin":
                front_matter = False
                flush()
                env, start = group(text, end)
                if env not in {"verbatim", "verbatim*", "lstlisting"}:
                    start = skip_space(text, start)
                optional_title = None
                if env not in {"verbatim", "verbatim*"} and text[start:start + 1] == "[":
                    optional_title, start = group(text, start, "[", "]")
                if env in {"verbatim", "verbatim*", "lstlisting"}:
                    closing = r"\end{" + env + "}"
                    stop = text.find(closing, start)
                    if stop < 0:
                        raise ParseError("Unclosed literal environment: " + env)
                    raw, end = text[start:stop], stop + len(closing)
                else:
                    raw, end = environment(text, start, env)
                if reference_section:
                    pass
                elif env in {"verbatim", "verbatim*"}:
                    if optional_title is not None:
                        raise ParseError("Unsupported literal environment option")
                    self.add("verbatim", r"\begin{" + env + "}" + raw + r"\end{" + env + "}", current(), literal=True)
                elif env == "lstlisting" and "listings" in self.gatherer.packages:
                    if env in self.gatherer.local_environments:
                        raise ParseError("Locally redefined lstlisting requires environment semantics")
                    config = dict(self.gatherer.listing_options)
                    config.update(listing_options(optional_title or ""))
                    # Formatting is preserved as evidence. TeX escape modes,
                    # substitutions and selective line ranges require separate
                    # semantics and cannot be passed off as literal code.
                    unsafe = {"mathescape", "texcl", "escapechar", "escapeinside", "literate", "style", "firstline", "lastline", "linerange", "firstnumber", "deletekeywords", "alsoletter", "alsodigit", "morecomment", "morestring", "moredelim"}
                    for key in unsafe & config.keys():
                        if key in {"mathescape", "texcl"} and config[key].strip().lower() == "false":
                            continue
                        raise ParseError("Unsupported lstlisting interpretation option: " + key)
                    allowed = {"language", "frame", "aboveskip", "belowskip", "showstringspaces", "columns", "basicstyle", "numbers", "numberstyle", "keywordstyle", "commentstyle", "stringstyle", "breaklines", "breakatwhitespace", "tabsize", "xleftmargin", "breakindent", "caption", "label", "mathescape", "texcl"}
                    unknown = config.keys() - allowed
                    if unknown:
                        raise ParseError("Unsupported lstlisting option: " + sorted(unknown)[0])
                    for key in {"basicstyle", "numberstyle", "keywordstyle", "commentstyle", "stringstyle"} & config.keys():
                        style, cursor = config[key], 0
                        while (cursor := skip_space(style, cursor)) < len(style):
                            if style[cursor] != "\\":
                                raise ParseError("Unsupported visible text in lstlisting style: " + key)
                            token, cursor = command(style, cursor)
                            if token == "color":
                                color, cursor = group(style, cursor)
                                if not re.fullmatch(r"[A-Za-z0-9!._:-]+", color):
                                    raise ParseError("Unsupported dynamic lstlisting style color: " + key)
                            elif token not in {"small", "footnotesize", "tiny", "scriptsize", "ttfamily", "bfseries", "itshape", "normalfont", "normalsize"}:
                                raise ParseError("Unsupported executable lstlisting style: " + key)
                    self.layout_metadata.append(dict(environment=env, options=config, raw_options=optional_title))
                    container = self.add("component", "code listing", current())
                    if config.get("caption"):
                        caption = self.add("component", "caption", container)
                        self.parse(config["caption"], caption, depth + 1)
                    self.add("verbatim", r"\begin{lstlisting}" + raw + r"\end{lstlisting}", container, literal=True)
                elif env == "tcolorbox" and "tcolorbox" in self.gatherer.packages:
                    # Only text title and explicit presentation keys. Hooks,
                    # overlays and arbitrary key execution remain unsupported.
                    opts, cursor = optional_title or "", 0
                    title = None
                    while (cursor := skip_space(opts, cursor)) < len(opts):
                        match = re.match(r"([A-Za-z]+)\s*=", opts[cursor:])
                        if not match:
                            raise ParseError("Unsupported tcolorbox option syntax")
                        key = match[1]
                        cursor = skip_space(opts, cursor + match.end())
                        if opts[cursor:cursor + 1] == "{":
                            value, cursor = group(opts, cursor)
                        else:
                            stop = opts.find(",", cursor)
                            stop = len(opts) if stop < 0 else stop
                            value, cursor = opts[cursor:stop], stop
                        if key == "title":
                            if title is not None:
                                raise ParseError("Duplicate tcolorbox title")
                            title = value
                        elif key not in {"colback", "colframe", "coltitle", "boxrule", "arc", "left", "right", "top", "bottom"} or re.search(r"[\\{}]", value):
                            raise ParseError("Unsupported tcolorbox key: " + key)
                        cursor = skip_space(opts, cursor)
                        if cursor < len(opts):
                            if opts[cursor] != ",":
                                raise ParseError("Malformed tcolorbox key separator")
                            cursor += 1
                    self.layout_metadata.append(dict(environment=env, options=optional_title))
                    container = self.add("component", env, current())
                    if title is not None:
                        heading = self.add("component", "box title", container)
                        self.parse(title, heading, depth + 1)
                    self.parse(raw, container, depth + 1)
                elif env == "IEEEbiography" and self.gatherer.document_class == "IEEEtran":
                    person, cursor = group(raw, 0)  # Never guess a missing mandatory name.
                    container = self.add("component", "biography: " + person, current())
                    self.layout_metadata.append(dict(environment=env, person=person, picture=optional_title))
                    self.parse(raw[cursor:], container, depth + 1)
                elif env in self.gatherer.environment_definitions:
                    argc, default, before, after = self.gatherer.environment_definitions[env]
                    values = []
                    if default is not None:
                        values.append(optional_title if optional_title is not None else default)
                    elif optional_title is not None:
                        raise ParseError("Unexpected option on source-defined environment: " + env)
                    cursor = 0
                    for _ in range(argc - len(values)):
                        value, cursor = group(raw, cursor)
                        values.append(value)
                    for i, value in reversed(list(enumerate(values, 1))):
                        before = before.replace("#" + str(i), value)
                        after = after.replace("#" + str(i), value)
                    container = self.add("component", env, current())
                    self.layout_metadata.append(dict(environment=env, source_begin=before, source_end=after))
                    self.parse(before + "\n" + raw[cursor:] + "\n" + after, container, depth + 1)
                elif env in {"small", "footnotesize", "scriptsize", "normalsize", "titlepage", "NoHyper"}:
                    if optional_title is not None:
                        raise ParseError("Unexpected option on layout environment: " + env)
                    self.parse(raw, current(), depth + 1)
                elif env == "minipage":
                    # Vertical alignment, optional height/inner alignment and
                    # width affect layout; every character of its body survives.
                    cursor = options(raw, 0)
                    cursor = options(raw, cursor)
                    width, cursor = group(raw, cursor)
                    self.layout_metadata.append(dict(environment=env, width=width, alignment=optional_title))
                    self.parse(raw[cursor:], current(), depth + 1)
                elif env == "widetext" and (self.gatherer.document_class or "").startswith("revtex"):
                    self.parse(raw, current(), depth + 1)
                elif env == "wrapfigure" and "wrapfig" in self.gatherer.packages:
                    _, cursor = group(raw, 0)  # placement
                    cursor = options(raw, cursor)  # overhang
                    _, cursor = group(raw, cursor)  # width
                    self.add("figure", text[pos:end], current())
                elif env == "links" and any(p.startswith("aaai") for p in self.gatherer.packages):
                    if optional_title is not None:
                        raise ParseError("Unsupported links option")
                    container = self.add("component", "links", current())
                    self.parse(raw, container, depth + 1)
                elif env == "icmlauthorlist" and any(p.startswith("icml") for p in self.gatherer.packages):
                    if optional_title is not None:
                        raise ParseError("Unsupported ICML author list option")
                    cursor, authors = 0, []
                    while (cursor := skip_space(raw, cursor)) < len(raw):
                        token, cursor = command(raw, cursor)
                        if token != "icmlauthor":
                            raise ParseError("Unsupported ICML author-list command: " + token)
                        author, cursor = group(raw, cursor)
                        indices, cursor = group(raw, cursor)
                        authors.append(dict(author=author, indices=indices))
                    self.layout_metadata.append(dict(environment=env, authors=authors))
                elif env == "indented" and self.gatherer.document_class == "iopart":
                    container = self.add("component", env, current())
                    self.items(raw, container, depth + 1)
                elif env == "biographywithpic" and self.gatherer.document_class == "IEEEAerospaceCLS":
                    person, cursor = group(raw, 0)
                    picture, cursor = group(raw, cursor)
                    container = self.add("component", "biography: " + person, current())
                    self.layout_metadata.append(dict(environment=env, person=person, picture=picture))
                    self.parse(raw[cursor:], container, depth + 1)
                elif env == "frontmatter" and self.gatherer.document_class in {"autart", "ecai", "imsart"}:
                    if optional_title is not None or env in self.gatherer.local_environments:
                        raise ParseError("Unsupported/local frontmatter environment semantics")
                    container = self.add("component", "frontmatter", current())
                    self.frontmatter(raw, container, depth + 1)
                elif env == "frontmatter" and self.gatherer.document_class == "elsarticle":
                    self.parse(raw, current(), depth + 1)
                elif (env in {"keyword", "keywords"} and (
                      self.gatherer.document_class in {"elsarticle", "IEEEtran", "ceurart", "mnras", "cas-dc", "cas-sc", "interact", "l4dc2025", "autart", "imsart"}
                      or env == "keywords" and "spconf" in self.gatherer.packages)):
                    if optional_title is not None or env in self.gatherer.local_environments:
                        raise ParseError("Unsupported/local keyword environment semantics: " + env)
                    container = self.add("component", "keywords", current())
                    self.parse(raw, container, depth + 1)
                elif env == "algorithm" and "algorithm2e" in self.gatherer.packages and not re.search(r"\\begin\s*\{algorithmic\}", raw):
                    container = self.add("component", env, current())
                    self.algorithm2e_depth += 1
                    try:
                        self.parse(raw, container, depth + 1)
                    finally:
                        self.algorithm2e_depth -= 1
                elif env == "sloppypar":
                    if env in self.gatherer.local_environments:
                        raise ParseError("Locally redefined sloppypar requires environment semantics")
                    # Standard paragraph line-breaking tolerance, not a
                    # semantic component. Keep all text under its real parent.
                    self.parse(raw, current(), depth + 1)
                elif env == "IEEEkeywords" and self.gatherer.document_class == "IEEEtran":
                    if optional_title is not None:
                        raise ParseError("Unsupported optional argument on " + env)
                    if env in self.gatherer.local_environments:
                        raise ParseError("Locally redefined IEEEkeywords requires environment semantics")
                    container = self.add("component", "keywords", current())
                    self.parse(raw, container, depth + 1)
                elif env == "subequations" and "amsmath" in self.gatherer.packages:
                    if optional_title is not None:
                        raise ParseError("Unsupported optional argument on " + env)
                    if env in self.gatherer.local_environments:
                        raise ParseError("Locally redefined subequations requires environment semantics")
                    container = self.add("component", env, current())
                    self.parse(raw, container, depth + 1)
                elif env == "CCSXML" and self.gatherer.document_class == "acmart":
                    if optional_title is not None:
                        raise ParseError("Unsupported optional argument on " + env)
                    if env in self.gatherer.local_environments:
                        raise ParseError("Locally redefined CCSXML requires environment semantics")
                    if re.search(r"<!DOCTYPE|<!ENTITY", raw, re.I):
                        raise ParseError("Unsupported CCSXML entity declaration")
                    try:
                        ET.fromstring(raw)
                    except ET.ParseError as exc:
                        raise ParseError("Malformed ACM classification XML: " + str(exc)) from exc
                    self.layout_metadata.append(dict(command=env, raw=raw))
                elif env in MATH_ENVS:
                    if env.rstrip("*") in {"align", "alignat", "eqnarray"}:
                        raw = r"\begin{aligned}" + raw + r"\end{aligned}"
                    self.add("block_formula", r"\begin{equation}" + raw.strip() + r"\end{equation}", current())
                elif (env in {"deluxetable", "deluxetable*"}
                      and (self.gatherer.document_class or "").startswith("aastex")):
                    if env in self.gatherer.local_environments:
                        raise ParseError("Locally redefined AASTeX table requires environment semantics")
                    # Like tabular/longtable, one table leaf retains its full
                    # alignment, caption, cells, notes and citation commands.
                    self.add("figure", raw, current())
                elif env in FIGURE_ENVS:
                    self.add("figure", raw, current())
                elif env == "inparaenum" and "paralist" in self.gatherer.packages:
                    if env in self.gatherer.local_environments:
                        raise ParseError("Locally redefined inparaenum requires environment semantics")
                    if optional_title is not None and re.search(r"[\\{}$]", optional_title):
                        raise ParseError("Unsupported dynamic inparaenum label format")
                    list_id = self.add("enumerate", "inparaenum", current())
                    self.layout_metadata.append(dict(environment=env, inline=True, label_format=optional_title))
                    self.items(raw, list_id, depth + 1)
                elif env in {"itemize", "enumerate", "description"}:
                    list_id = self.add(env, env, current())
                    self.items(raw, list_id, depth + 1)
                elif env == "algorithmic":
                    container = self.add("component", env, current())
                    self.algorithmic(raw, container, depth + 1)
                elif env in CONTAINERS or env in self.gatherer.theorems:
                    if env in {"appendices", "appendix"} and depth == 0:
                        # An explicit appendix division is not a child of the
                        # last main-text section (e.g. Springer Declarations).
                        headings.clear()
                    container = self.add("component", env, current())
                    # Optional theorem/proof headings contain visible prose and
                    # citations; unlike float placement, they are not layout.
                    if optional_title is not None and (env == "proof" or env in self.gatherer.theorems):
                        self.parse(optional_title, container, depth + 1)
                    self.parse(raw, container, depth + 1)
                else:
                    raise ParseError(f"Unsupported environment: {env}")
            elif name == "end":
                raise ParseError("Unexpected environment end")
            elif name == "newtheorem":
                # A declaration in document body is not printed prose. The
                # gatherer already registers the exact environment identifier.
                start = end + int(text[end:end + 1] == "*")
                env, start = group(text, start)
                start = options(text, start)
                label, start = group(text, start)
                end = options(text, start)
                if env not in self.gatherer.theorems:
                    raise ParseError("Unregistered theorem declaration: " + env)
                self.layout_metadata.append(dict(command=name, environment=env, label=label))
            elif name == "@namedef" and name not in self.gatherer.macros:
                target, start = group(text, end)
                value, end = group(text, start)
                if not re.fullmatch(r"subjclassname@[0-9]{4}", target):
                    raise ParseError("Unsupported named definition: " + target)
                self.layout_metadata.append(dict(command=name, target=target, value=value))
            elif name == "renewcommand":
                # blfootnote wrappers can introduce this declaration only after
                # macro expansion. Emptying the marker does not empty the note.
                start = skip_space(text, end)
                start += int(text[start:start + 1] == "*")
                start = skip_space(text, start)
                if text[start:start + 1] == "{":
                    target, start = group(text, start)
                    target = target.strip()
                else:
                    target, start = command(text, start)
                    target = "\\" + target
                algorithm_labels = {r"\algorithmicrequire": "Input:", r"\algorithmicensure": "Output:",
                                    r"\algorithmicif": "if", r"\algorithmicthen": "then", r"\algorithmicelse": "else",
                                    r"\algorithmicfor": "for", r"\algorithmicdo": "do", r"\algorithmicend": "end"}
                if target in algorithm_labels and "algpseudocode" in self.gatherer.packages:
                    replacement, end = group(text, start)
                    if replacement != r"\textbf{" + algorithm_labels[target] + "}":
                        raise ParseError("Unsupported algorithm keyword redefinition: " + target)
                    self.layout_metadata.append(dict(command=name, target=target, replacement=replacement))
                    pos = end
                    continue
                if target != r"\thefootnote":
                    raise ParseError("Unsupported local renewcommand target: " + target)
                replacement, end = group(text, start)
                if replacement != "":
                    raise ParseError("Unsupported nonempty local footnote marker")
                self.local_footnote_renewal = True
                buffer.append(" ")
            elif name == "let":
                # This exact assignment suppresses footnote numbering, not its
                # text. Do not generalize it to arbitrary semantic aliases.
                target, start = command(text, skip_space(text, end))
                start = skip_space(text, start)
                if text[start:start + 1] == "=":
                    start = skip_space(text, start + 1)
                replacement, end = command(text, start)
                if (target, replacement) == ("WriteBookmarks", "relax") and self.gatherer.document_class in {"cas-dc", "cas-sc"}:
                    self.layout_metadata.append(dict(command=name, target=target, replacement=replacement))
                elif (target, replacement) != ("thefootnote", "relax"):
                    raise ParseError("Unsupported semantic let assignment: " + target + "=" + replacement)
                buffer.append(" ")
            elif name == "includegraphics":
                front_matter = False
                # A teaser can live in center without a figure float. Preserve
                # its source and adjacent caption as one leaf; never open binaries.
                flush()
                start = end + int(text[end:end + 1] == "*")
                start = options(text, start)
                target, end = group(text, start)
                if not target.strip():
                    raise ParseError("Empty includegraphics target")
                caption_start = skip_space(text, end)
                # Teaser layouts may insert vertical spacing between a graphic
                # and its caption. Look through this explicit layout command
                # only; never skip unknown text or commands to find a caption.
                while text.startswith(r"\vspace", caption_start):
                    space_name, after = command(text, caption_start)
                    if space_name != "vspace":
                        break
                    after += int(text[after:after + 1] == "*")
                    _, after = group(text, after)
                    caption_start = skip_space(text, after)
                if text.startswith(r"\captionof", caption_start):
                    caption_name, after = command(text, caption_start)
                    if caption_name == "captionof":
                        kind, after = group(text, after)
                        if kind != "figure":
                            raise ParseError("Unsupported standalone graphic caption type: " + kind)
                        after = options(text, after)
                        _, end = group(text, after)
                if not reference_section:
                    self.add("figure", text[pos:end], current())
            elif (name in {"hbadness", "vbadness", "hfuzz", "vfuzz", "vskip", "hskip", "itemsep", "leftskip"}
                  and name not in self.gatherer.macros):
                # Literal TeX diagnostic thresholds / layout glue only. Dynamic
                # registers and expressions are not evaluated.
                number = r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)"
                dimension = number + r"\s*(?:pt|em|ex|mm|cm|in|bp|pc|sp)"
                value_pattern = r"[0-9]+" if name.endswith("badness") else dimension
                match = re.match(r"\s*=?\s*(" + value_pattern + r")(?![A-Za-z0-9.])", text[end:])
                if not match:
                    raise ParseError("Unsupported nonliteral layout register: " + name)
                end += len(match[0])
                self.layout_metadata.append(dict(command=name, value=match[1]))
            elif name in {"break", "quad", "qquad"} and name not in self.gatherer.macros:
                buffer.append(" ")
            elif name == "qedsymbol" and name not in self.gatherer.macros and "amsthm" in self.gatherer.packages:
                buffer.append(r"\qedsymbol")
            elif name == "textasciicircum" and name not in self.gatherer.macros:
                buffer.append("^")
                end = skip_space(text, end)
            elif (name in {"equalcont", "cormark", "fnmark", "cortext", "fntext", "ead", "dedicatory", "pagerange", "preprint", "runningtitle", "runningauthor", "shorttitle", "shortauthors", "crefalias", "articletype"}
                  and name not in self.gatherer.macros):
                if name == "dedicatory":
                    raw, end = group(text, end)
                    flush()
                    if raw.strip():
                        container = self.add("component", "dedication", current())
                        self.parse(raw, container, depth + 1)
                else:
                    start = skip_space(text, end)
                    option = None
                    if text[start:start + 1] == "[":
                        option, start = group(text, start, "[", "]")
                    args = []
                    if name not in {"cormark", "fnmark"}:
                        for _ in range(2 if name == "crefalias" else 1):
                            value, start = group(text, start); args.append(value)
                    elif option is None:
                        raise ParseError("Missing author marker index: " + name)
                    end = start
                    self.layout_metadata.append(dict(command=name, option=option, arguments=args))
            elif name == "appendices" and self.gatherer.document_class in {"IEEEtran", "IEEEAerospaceCLS"} and name not in self.gatherer.macros:
                flush()
                headings.clear()
                self.layout_metadata.append(dict(command=name))
            elif name == "sep" and self.gatherer.document_class in {"elsarticle", "ceurart", "cas-dc", "cas-sc"} and name not in self.gatherer.macros:
                buffer.append("; ")
            elif name == "@" and self.gatherer.document_class == "ceurart" and name not in self.gatherer.macros:
                pass  # TeX sentence-spacing declaration, not an at-sign.
            elif name == "num" and "siunitx" in self.gatherer.packages and name not in self.gatherer.macros:
                start = options(text, end)
                value, end = group(text, start)
                if not re.fullmatch(r"[+\-0-9.eE]+", value.strip()):
                    raise ParseError("Unsupported nonliteral siunitx number")
                buffer.append(text[pos:end])  # Preserve formatting rather than guess a locale.
            elif name == "SI" and "siunitx" in self.gatherer.packages and name not in self.gatherer.macros:
                # As with num, retain the source quantity rather than guess
                # locale, unit spelling or package rendering options. Consume
                # its balanced syntax as one token so unit macros are not
                # mistaken for independent prose commands.
                start = options(text, end)
                value, start = group(text, start)
                start = options(text, start)  # optional pre-unit (siunitx v2)
                unit, end = group(text, start)
                if not re.fullmatch(r"[+\-0-9.eE()]+", value.strip()) or not re.search(r"[0-9]", value):
                    raise ParseError("Unsupported nonliteral siunitx quantity")

                if any(token not in SI_UNIT_COMMANDS for token in re.findall(r"\\([A-Za-z@]+|.)", unit)):
                    raise ParseError("Unsupported siunitx unit; preserve source for interpretation: " + unit)
                buffer.append(text[pos:end])
                self.layout_metadata.append(dict(command="SI", number=value, unit=unit, normalization="source_quantity_retained"))
            elif name == "si" and "siunitx" in self.gatherer.packages and name not in self.gatherer.macros:
                start = options(text, end)
                unit, end = group(text, start)
                if any(token not in SI_UNIT_COMMANDS for token in re.findall(r"\\([A-Za-z@]+|.)", unit)):
                    raise ParseError("Unsupported siunitx unit; preserve source for interpretation: " + unit)
                buffer.append(text[pos:end])
                self.layout_metadata.append(dict(command=name, unit=unit, normalization="source_unit_retained"))
            elif self.algorithm2e_depth and name in {"KwIn", "KwOut", "KwResult", "KwRet"} and name not in self.gatherer.macros:
                raw, end = group(text, end)
                flush()
                container = self.add("component", {"KwIn":"input", "KwOut":"output", "KwResult":"result", "KwRet":"return"}[name], current())
                self.parse(raw, container, depth + 1)
            elif self.algorithm2e_depth and name in {"For", "ForEach", "While", "If", "lIf", "ElseIf", "Else"} and name not in self.gatherer.macros:
                condition = None
                start = end
                if name != "Else":
                    condition, start = group(text, start)
                raw, end = group(text, start)
                flush()
                container = self.add("component", name.lower(), current())
                if condition is not None:
                    self.parse(condition, container, depth + 1)
                self.parse(raw, container, depth + 1)
            elif name in DROP:
                if name == "maketitle":
                    front_matter = False
                if name == "printbibliography" and text[skip_space(text, end):].startswith("["):
                    raw, end = group(text, skip_space(text, end), "[", "]")
                    if not re.fullmatch(r"\s*heading\s*=\s*(?:bibintoc|bibliography|subbibintoc|subbibliography|none)\s*", raw):
                        raise ParseError("Filtered/options-based biblatex rendering is unsupported")
                    self.layout_metadata.append(dict(command=name, options=raw))
                if name in {"footnotemark", "authornotemark"}:
                    end = options(text, end)
                buffer.append(" ")
            elif (name == "nobreakdash" and name not in self.gatherer.macros
                  and "amsmath" in self.gatherer.packages):
                # amsmath only suppresses line breaks around the following
                # dash(es). Keep those characters, including word attachment.
                dash = re.match(r"-{1,3}(?!-)", text[end:])
                if not dash:
                    raise ParseError("Unsupported nobreakdash without a literal dash")
                buffer.append(dash[0])
                end += len(dash[0])
                zero_space = re.match(r"\\hspace\s*\{\s*0pt\s*\}", text[end:])
                if zero_space:
                    end += len(zero_space[0])
            elif name in {"balance", "FloatBarrier"} and name not in self.gatherer.macros:
                required = "balance" if name == "balance" else "placeins"
                if required not in self.gatherer.packages:
                    raise ParseError("Undeclared layout package for " + name)
                self.layout_metadata.append(dict(command=name))
            elif name == "allowdisplaybreaks" and name not in self.gatherer.macros and "amsmath" in self.gatherer.packages:
                start = skip_space(text, end)
                level = None
                if text[start:start + 1] == "[":
                    level, end = group(text, start, "[", "]")
                    if level not in {"0", "1", "2", "3", "4"}:
                        raise ParseError("Invalid allowdisplaybreaks level")
                self.layout_metadata.append(dict(command=name, level=level))
            elif name == "raisebox" and name not in self.gatherer.macros:
                lift, start = group(text, end)
                start = options(text, start)
                start = options(text, start)
                raw, end = group(text, start)
                self.layout_metadata.append(dict(command=name, lift=lift))
                text = splice_tokens(text, pos, raw, end)
                continue
            elif name == "framebox" and name not in self.gatherer.macros:
                start = options(text, end)
                start = options(text, start)
                raw, end = group(text, start)
                text = splice_tokens(text, pos, raw, end)
                continue
            elif name in {"textquoteleft", "textquoteright", "textquotedblleft", "textquotedblright"} and name not in self.gatherer.macros:
                buffer.append({"textquoteleft":"‘", "textquoteright":"’", "textquotedblleft":"“", "textquotedblright":"”"}[name])
                end = skip_space(text, end)
            elif self.pseudocode_depth and name == "Comment" and name not in self.gatherer.macros:
                raw, end = group(text, end)
                flush()
                container = self.add("component", "comment", current())
                self.parse(raw, container, depth + 1)
            elif name == "par":
                flush()
            elif name == "caption":
                front_matter = False
                flush()
                start = options(text, end)
                value, end = group(text, start)
                if not reference_section:
                    caption = self.add("component", "caption", current())
                    self.parse(value, caption, depth + 1)
            elif name == "verb":
                start = end + int(text[end:end + 1] == "*")
                delimiter = text[start:start + 1]
                stop = text.find(delimiter, start + 1) if delimiter else -1
                if stop < 0:
                    raise ParseError("Unclosed inline verbatim")
                end = stop + 1
                buffer.append(text[pos:end])
            elif name in {"L", "l", "AA"} and name not in self.gatherer.macros:
                buffer.append({"L": "Ł", "l": "ł", "AA": "Å"}[name])
                # TeX ignores spaces delimiting a control word: \L ojasiewicz.
                end = skip_space(text, end)
            elif name in {"S", "P", "LaTeX", "TeX", "ldots", "dots", "textendash", "textemdash", "textpercent"}:
                buffer.append({"S": "§", "P": "¶", "LaTeX": "LaTeX", "TeX": "TeX",
                               "ldots": "…", "dots": "…", "textendash": "–",
                               "textemdash": "—", "textpercent": "%"}[name])
            elif (front_matter and name in {"author", "affil"}
                  and not "".join(buffer).strip()):
                # Springer declarations: optional star, optional affiliation
                # indices, then one balanced argument (including nested names
                # and addresses). Like existing author handling, these are
                # publication metadata, not hierarchy prose. Never expand or
                # skip beyond the closing brace into the abstract/body.
                start = skip_space(text, end)
                start += int(text[start:start + 1] == "*")
                start = options(text, start)
                value, end = group(text, start)
                if name == "author" and self.gatherer.document_class in {"ceurart", "cas-dc", "cas-sc"}:
                    tail = skip_space(text, end)
                    if text[tail:tail + 1] == "[":
                        author_options, end = group(text, tail, "[", "]")
                        self.layout_metadata.append(dict(command=name, author=value, options=author_options))
                buffer.append(" ")
            elif name in DROP_ARGUMENT:
                if name in {"hspace", "vspace"} or (name == "title" and self.gatherer.document_class == "svmult"):
                    end = skip_space(text, end)
                    end += int(text[end:end + 1] == "*")
                end = options(text, end)
                for _ in range({"addcontentsline": 3, "setcounter": 2, "addtocounter": 2, "fontsize": 2}.get(name, 1)):
                    _, end = group(text, end)
                buffer.append(" ")
            elif (name in {"fancyhead", "fancyfoot", "fancyhf", "markboth", "markright"}
                  and name not in self.gatherer.macros):
                # Running headers/footers are layout metadata. Keep their raw
                # arguments for audit; do not mix repeated page furniture into
                # document prose, or execute the commands inside those values.
                start = skip_space(text, end)
                placement = None
                if name.startswith("fancy") and text[start:start + 1] == "[":
                    placement, start = group(text, start, "[", "]")
                args = []
                for _ in range(2 if name == "markboth" else 1):
                    value, start = group(text, start)
                    args.append(value)
                end = start
                self.layout_metadata.append(dict(command=name, placement=placement, arguments=args))
            elif (name == "IEEEPARstart" and name not in self.gatherer.macros
                  and self.gatherer.document_class == "IEEEtran"):
                first, start = group(text, end)
                rest, end = group(text, start)
                if len(first) != 1 or not first.isalpha():
                    raise ParseError("Unsupported nonliteral IEEEPARstart initial")
                text = text[:pos] + first + rest + text[end:]
                continue
            elif (name == "ccsdesc" and name not in self.gatherer.macros
                  and self.gatherer.document_class == "acmart"):
                start = skip_space(text, end)
                weight = None
                if text[start:start + 1] == "[":
                    weight, start = group(text, start, "[", "]")
                raw, end = group(text, start)
                container = self.add("component", "classification", current())
                self.parse(raw, container, depth + 1)
                self.layout_metadata.append(dict(command=name, weight=weight, raw=raw))
            elif (name in {"setstretch", "onehalfspacing", "singlespacing", "doublespacing"}
                  and name not in self.gatherer.macros and "setspace" in self.gatherer.packages):
                value = None
                if name == "setstretch":
                    value, end = group(text, end)
                    if not re.fullmatch(r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)", value.strip()) or float(value) <= 0:
                        raise ParseError("Unsupported nonliteral line spacing: " + value)
                self.layout_metadata.append(dict(command=name, value=value))
            elif (name == "SetAlgoLined" and name not in self.gatherer.macros
                  and "algorithm2e" in self.gatherer.packages):
                self.layout_metadata.append(dict(command=name))
            elif name in {"nobreak", "allowbreak"} and name not in self.gatherer.macros:
                # A break penalty has no argument and no visible characters.
                pass
            elif (name in {"degr", "arcdeg", "arcmin", "arcsec"}
                  and name not in self.gatherer.macros
                  and (self.gatherer.document_class or "").startswith("aastex")):
                buffer.append({"degr": "°", "arcdeg": "°", "arcmin": "′", "arcsec": "″"}[name])
                end = skip_space(text, end)
            elif (name == "pacs" and name not in self.gatherer.macros
                  and (self.gatherer.document_class or "").startswith("revtex")):
                raw, end = group(text, end)
                self.layout_metadata.append(dict(command=name, arguments=[raw]))
                if raw.strip():
                    flush()
                    container = self.add("component", "PACS classification", current())
                    self.parse(raw, container, depth + 1)
            elif name == "correspondingauthor" and name not in self.gatherer.macros and (self.gatherer.document_class or "").startswith("aastex"):
                value, end = group(text, end)
                self.layout_metadata.append(dict(command=name, arguments=[value]))
            elif name in {"acknowledgments", "acks"} and name not in self.gatherer.macros:
                flush()
                start = skip_space(text, end)
                declaration = (name == "acknowledgments" and text[start:start + 1] != "{"
                               and (self.gatherer.document_class or "").startswith("revtex"))
                if not declaration:
                    raw, end = group(text, end)
                if depth == 0:
                    # Backmatter is a peer division of the research sections.
                    headings.clear()
                reference_section, reference_rank = False, None
                container = self.add("component", "acknowledgments", current())
                if declaration:
                    # REVTeX's declaration starts a division; it does not take
                    # the next token as an argument or consume later sections.
                    headings.append((LEVELS["section"], container))
                else:
                    self.parse(raw, container, depth + 1)
            elif (name in {"crefrange", "Crefrange"} and name not in self.gatherer.macros
                  and "cleveref" in self.gatherer.packages):
                _, start = group(text, end)
                _, end = group(text, start)
                buffer.append(text[pos:end])
            elif (name in {"facility", "facilities", "software"} and name not in self.gatherer.macros
                  and (self.gatherer.document_class or "").startswith("aastex")):
                flush()
                raw, end = group(text, end)
                container = self.add("component", name, current())
                self.parse(raw, container, depth + 1)
            elif (name == "numberwithin" and name not in self.gatherer.macros
                  and "amsmath" in self.gatherer.packages):
                counter, start = group(text, end)
                within, end = group(text, start)
                if counter not in {"equation", "figure", "table"} or within not in LEVELS:
                    raise ParseError("Unsupported counter dependency: " + counter + "/" + within)
                self.layout_metadata.append(dict(command=name, counter=counter, within=within))
            elif name == "setlength" and name not in self.gatherer.macros:
                target, start = group(text, end)
                value, end = group(text, start)
                known = {r"\parskip", r"\parindent", r"\tabcolsep", r"\columnsep",
                         r"\textfloatsep", r"\floatsep", r"\dbltextfloatsep",
                         r"\abovecaptionskip", r"\belowcaptionskip", r"\headheight", r"\footskip", r"\jot", r"\bibsep", r"\itemsep", r"\leftskip", r"\parsep"}
                scaled_baseline = (target.strip() == r"\baselineskip" and re.fullmatch(
                    r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)\s*\\baselineskip", value.strip()))
                if not scaled_baseline and (target.strip() not in known or not re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)\s*(?:pt|em|ex|mm|cm|in|bp|pc|sp)", value.strip())):
                    raise ParseError("Unsupported nonliteral layout length assignment: " + target + "=" + value)
                self.layout_metadata.append(dict(command=name, target=target, value=value))
            elif name in {"newline", "indent"} and name not in self.gatherer.macros:
                buffer.append(" ")
            elif (name == "subref" and name not in self.gatherer.macros
                  and self.gatherer.packages & {"subcaption", "subfig", "subfigure"}):
                start = end + int(text[end:end + 1] == "*")
                _, end = group(text, start)
                buffer.append(text[pos:end])
            elif name == "centerline" and name not in self.gatherer.macros:
                value, end = group(text, end)
                # Alignment changes only placement. Reparse every character of
                # its argument, including graphics and unknown nested commands.
                text = text[:pos] + "\n" + value + "\n" + text[end:]
                continue
            elif name == "makebox":
                sized = text[skip_space(text, end):skip_space(text, end) + 1] == "["
                start = options(text, end)
                start = options(text, start)
                value, end = group(text, start)
                if sized:
                    value = " " + value + " "
                text = splice_tokens(text, pos, value, end)
                continue
            elif name == "rule":
                start = options(text, end)
                _, start = group(text, start)
                _, end = group(text, start)
                buffer.append(" ")
            elif self.pseudocode_depth and name in {"Return", "RETURN"} and name not in self.gatherer.macros:
                buffer.append("return ")
            elif self.pseudocode_depth and name == "Call" and name not in self.gatherer.macros:
                target, start = group(text, end)
                arguments, end = group(text, start)
                text = text[:pos] + target + "(" + arguments + ")" + text[end:]
                continue
            elif name == "chreplaced" and "changes" in self.gatherer.packages and name not in self.gatherer.macros:
                start = options(text, end)
                new, start = group(text, start)
                old, end = group(text, start)
                if new or old:
                    raise ParseError("Nonempty changes markup needs an explicit draft/final interpretation")
                self.layout_metadata.append(dict(command=name, new=new, old=old))
            elif name == "texorpdfstring" and "hyperref" in self.gatherer.packages and name not in self.gatherer.macros:
                visible, start = group(text, end)
                bookmark, end = group(text, start)
                self.layout_metadata.append(dict(command=name, bookmark=bookmark))
                text = splice_tokens(text, pos, visible, end)
                continue
            elif name == "phantomsection" and "hyperref" in self.gatherer.packages and name not in self.gatherer.macros:
                self.layout_metadata.append(dict(command=name))
            elif name == "text" and self.gatherer.packages & {"amsmath", "amstext", "mathtools"} and name not in self.gatherer.macros:
                raw, end = group(text, end)
                text = splice_tokens(text, pos, raw, end)
                continue
            elif self.algorithm2e_depth and name == "SetKwInOut" and name not in self.gatherer.macros:
                target, start = group(text, end)
                label, end = group(text, start)
                if not re.fullmatch(r"[A-Za-z]+", target) or target in self.gatherer.macros or target in LEVELS:
                    raise ParseError("Unsafe algorithm keyword declaration: " + target)
                self.algorithm_keywords[target] = label
            elif self.algorithm2e_depth and name in self.algorithm_keywords:
                raw, end = group(text, end)
                flush()
                container = self.add("component", self.algorithm_keywords[name], current())
                self.parse(raw, container, depth + 1)
            elif self.algorithm2e_depth and name in {"BlankLine", "Indp", "Indm"} and name not in self.gatherer.macros:
                self.layout_metadata.append(dict(command=name))
                buffer.append(" ")
            elif name in {"doparttoc", "faketableofcontents", "parttoc"} and "minitoc" in self.gatherer.packages and name not in self.gatherer.macros:
                self.layout_metadata.append(dict(command=name))
            elif name in {"looseness"} and name not in self.gatherer.macros:
                number = re.match(r"\s*=?\s*([+-]?[0-9]+)(?![A-Za-z0-9.])", text[end:])
                if not number:
                    raise ParseError("Unsupported nonliteral paragraph looseness")
                end += len(number[0])
                self.layout_metadata.append(dict(command=name, value=number[1]))
            elif name in {"printAffiliationsAndNotice", "printAffiliationsAndNoticefootnote"} and any(p.startswith("icml") for p in self.gatherer.packages) and name not in self.gatherer.macros:
                start = skip_space(text, end)
                notice = None
                if text[start:start + 1] == "{":
                    notice, end = group(text, start)
                    if notice.strip():
                        flush()
                        container = self.add("component", "author notice", current())
                        self.parse(notice, container, depth + 1)
                self.layout_metadata.append(dict(command=name, notice=notice))
            elif name == "addtocontents" and name not in self.gatherer.macros:
                target, start = group(text, end)
                content, end = group(text, start)
                if target != "toc":
                    raise ParseError("Unsupported auxiliary contents target: " + target)
                self.layout_metadata.append(dict(command=name, target=target, content=content))
            elif name == "today" and name not in self.gatherer.macros:
                # The source does not establish its compilation date. Preserve
                # the unresolved token instead of inventing today's timestamp.
                buffer.append(r"\today{}")
                self.warnings.append("Source compilation date remains unresolved: \\today")
            elif name in {"big", "Big", "bigg", "Bigg", "bigl", "bigr", "Bigl", "Bigr", "biggl", "biggr", "Biggl", "Biggr"} and name not in self.gatherer.macros:
                start = skip_space(text, end)
                if text[start:start + 1] in {"(", ")", "[", "]", "|", "."}:
                    end = start + 1
                elif text[start:start + 1] == "\\":
                    delimiter, end = command(text, start)
                    if delimiter not in {"{", "}", "langle", "rangle", "lvert", "rvert", "lVert", "rVert", "vert", "Vert"}:
                        raise ParseError("Unsupported sized delimiter: " + delimiter)
                else:
                    raise ParseError("Missing literal sized delimiter")
                buffer.append(text[pos:end])
            elif name in {"textless", "textgreater"} and name not in self.gatherer.macros:
                buffer.append("<" if name == "textless" else ">")
                end = skip_space(text, end)
            elif name in FORMATTING or name in {"fbox", "hbox"}:
                start = skip_space(text, end)
                if text[start:start + 1] != "{" and name in FORMATTING and text[start:start + 1].isalnum():
                    value, end = text[start], start + 1
                else:
                    value, end = group(text, end)
                # Re-parse formatting content so nested commands still receive validation.
                text = splice_tokens(text, pos, value, end)
                continue
            elif name in {"textcolor", "colorbox"} and name not in self.gatherer.macros:
                start = skip_space(text, end)
                model = None
                if text[start:start + 1] == "[":
                    model, start = group(text, start, "[", "]")
                color, start = group(text, start)
                value, end = group(text, start)
                self.layout_metadata.append(dict(command=name, color_model=model, color=color))
                text = splice_tokens(text, pos, value, end)
                continue
            elif name == "thefootnote" and self.local_footnote_renewal:
                # Scoping/counter evaluation is not implemented; never expand
                # a stale preamble definition after a local marker change.
                raise ParseError("Explicit footnote marker after local renewal requires scoped TeX semantics")
            elif (name in CVPR_ABBREVIATIONS and "cvpr" in self.gatherer.packages
                  and name not in self.gatherer.macros):
                value = CVPR_ABBREVIATIONS[name]
                if text[end:end + 1] == ".":
                    end += 1  # cvpr's onedot avoids a duplicated period.
                buffer.append(value)
                self.warnings.append("CVPR convention: \\" + name + " -> " + value + "; original style file not interpreted")
            elif name in self.gatherer.macros:
                argc, default, replacement = self.gatherer.macros[name]
                # This command path is prose: inline/display math is consumed
                # as a whole before macro expansion. For the whole, nonnested
                # ifmmode-name idiom, TeX therefore selects the text branch.
                # Preserve even differing branches exactly as defined; do not
                # infer that one branch was an author's typo.
                mode = re.fullmatch(r"\\ifmmode\s*(.*?)\\else\s*\$(.*?)\$\s*\\fi\s*",
                                    replacement, re.S)
                if (mode and not re.search(r"\\(?:if[a-zA-Z]*|else|fi)\b|\$", mode[1] + mode[2])):
                    replacement = r"\ensuremath{" + mode[2].strip() + "}"
                    self.warnings.append("Math-mode name macro uses its source-defined text branch in prose: " + name)
                if re.search(r"\\ifmmode\b", replacement):
                    raise ParseError("Unsupported math-mode conditional in macro \\" + name +
                                     ": only a whole nonnested math-name form is supported in prose")
                if argc is None:
                    raise ParseError(f"Parameterized TeX primitive macro unsupported: {name}")
                start = end
                values = []
                if default is not None:
                    start = skip_space(text, start)
                    if text[start:start + 1] == "[":
                        val, start = group(text, start, "[", "]")
                    else:
                        val = default
                    values.append(val)
                for _ in range(argc - len(values)):
                    val, start = group(text, start)
                    values.append(val)
                for idx, val in reversed(list(enumerate(values, 1))):
                    replacement = replacement.replace("#" + str(idx), val)
                self.expansions += 1
                if self.expansions > 10000 or len(text) + len(replacement) > 16 * 1024 * 1024:
                    raise ParseError("Macro expansion limit/cycle")
                text = splice_tokens(text, pos, replacement, start)
                continue
            elif name in {"'", '"', chr(96), "^", "~", "=", ".", "c", "v", "u", "H", "r"}:
                start = skip_space(text, end)
                if text[start:start + 1] == "{":
                    value, end = group(text, start)
                else:
                    value, end = text[start:start + 1], start + 1
                if len(value) != 1:
                    raise ParseError("Unsupported compound text accent")
                mark = {"'": "\u0301", '"': "\u0308", chr(96): "\u0300", "^": "\u0302",
                        "~": "\u0303", "=": "\u0304", ".": "\u0307", "c": "\u0327",
                        "v": "\u030c", "u": "\u0306", "H": "\u030b", "r": "\u030a"}[name]
                buffer.append(unicodedata.normalize("NFC", value + mark))
            elif name == "ensuremath":
                value, end = group(text, end)
                buffer.append("$" + value + "$")
            elif name in KEEP_COMMANDS or re.fullmatch(r"(?:[cC]ite[a-zA-Z]*|[tT]extcite|[pP]arencite|[aA]utocite|onlinecite)", name) or name == "nocite":
                is_citation = bool(re.fullmatch(r"(?:[cC]ite[a-zA-Z]*|[tT]extcite|[pP]arencite|[aA]utocite|onlinecite)", name))
                if (is_citation or name == "ref" and "hyperref" in self.gatherer.packages) and text[end:end + 1] == "*":
                    end += 1
                start = options(text, end)
                if is_citation:
                    start = options(text, start)
                count = 2 if name == "href" else 1
                for _ in range(count):
                    _, start = group(text, start)
                buffer.append(text[pos:start])
                end = start
            elif name in {"%", "&", "_", "#", "$", "{", "}"}:
                buffer.append("\\" + name)
            elif name in {" ", ",", ";", ":", "!", "\\", "\n", "/"}:
                if name == "\\":
                    end += int(text[end:end + 1] == "*")
                    end = options(text, end)
                buffer.append(" ")
            else:
                raise ParseError(f"Unsupported prose command: \\{name}")
            pos = end
        flush()

    def algorithmic(self, text, parent, depth):
        """Read algorithmic/algpseudocode statements without executing branches."""
        canonical = {"STATE": "STATE", "State": "STATE", "Statex": "STATEX",
                     "REQUIRE": "REQUIRE", "Require": "REQUIRE",
                     "ENSURE": "ENSURE", "Ensure": "ENSURE",
                     "FOR": "FOR", "For": "FOR", "ForAll": "FORALL", "FORALL": "FORALL",
                     "WHILE": "WHILE", "While": "WHILE", "IF": "IF", "If": "IF",
                     "ELSE": "ELSE", "Else": "ELSE", "ELSIF": "ELSIF", "ElsIf": "ELSIF",
                     "ENDFOR": "ENDFOR", "EndFor": "ENDFOR", "ENDIF": "ENDIF", "EndIf": "ENDIF",
                     "ENDWHILE": "ENDWHILE", "EndWhile": "ENDWHILE",
                     "Function": "FUNCTION", "EndFunction": "ENDFUNCTION",
                     "Procedure": "PROCEDURE", "EndProcedure": "ENDPROCEDURE"}
        positions, pos, braces, math = [], 0, 0, None
        while pos < len(text):
            if text[pos] == "\\":
                name, end = command(text, pos)
                if name == "verb":
                    start = end + int(text[end:end + 1] == "*")
                    delimiter = text[start:start + 1]
                    stop = text.find(delimiter, start + 1) if delimiter else -1
                    if stop < 0:
                        raise ParseError("Unclosed algorithmic verbatim")
                    end = stop + 1
                elif name == "(" and not math:
                    math = ")"
                elif name == math:
                    math = None
                elif name in canonical and braces == 0 and not math:
                    positions.append((pos, end, canonical[name]))
                pos = end
            else:
                if text[pos] == "$":
                    math = None if math == "$" else "$"
                if not math:
                    braces += int(text[pos] == "{") - int(text[pos] == "}")
                pos += 1
        if not positions or text[:positions[0][0]].strip():
            raise ParseError("Unsupported text before first algorithmic statement")
        stack, current = [], parent
        self.pseudocode_depth += 1
        try:
            for index, (_, start, name) in enumerate(positions):
                stop = positions[index + 1][0] if index + 1 < len(positions) else len(text)
                fragment = text[start:stop]
                if name in {"STATE", "STATEX", "REQUIRE", "ENSURE"}:
                    if not fragment.strip() and name != "STATEX":
                        raise ParseError("Empty algorithmic " + name)
                    target = self.add("component", name.lower(), current) if name in {"REQUIRE", "ENSURE"} else current
                    self.parse(fragment, target, depth + 1)
                elif name in {"FOR", "FORALL", "IF", "WHILE", "FUNCTION", "PROCEDURE", "ELSIF"}:
                    condition, end = group(fragment, 0)
                    signature = None
                    if name in {"FUNCTION", "PROCEDURE"}:
                        signature, end = group(fragment, end)
                    tail = re.sub(r"\\label\s*\{[^{}]*\}", "", fragment[end:])
                    allowed_tail = re.sub(r"\\hfill\b|:\s*", "", tail)
                    allowed_tail = re.sub(r"\\\\(?:\s*\[[^\]]*\])?", "", allowed_tail)
                    comment = re.match(r"\s*\\Comment\b", allowed_tail)
                    if allowed_tail.strip() and not comment:
                        raise ParseError("Unsupported text after algorithmic condition")
                    if name == "ELSIF":
                        if not stack or stack[-1]["kind"] != "IF" or stack[-1]["has_else"]:
                            raise ParseError("Unbalanced algorithmic ELSIF")
                        current = self.add("component", "elsif", stack[-1]["block"])
                    else:
                        block = self.add("component", name.lower(), current)
                        stack.append(dict(kind="FOR" if name == "FORALL" else name, parent=current,
                                          block=block, has_else=False))
                        if len(stack) > 60:
                            raise ParseError("Algorithmic nesting exceeds 60")
                        current = block
                    self.parse(condition, current, depth + 1)
                    if signature is not None:
                        self.parse(signature, current, depth + 1)
                    if tail.strip():
                        self.parse(tail, current, depth + 1)
                elif name == "ELSE":
                    if fragment.strip() or not stack or stack[-1]["kind"] != "IF" or stack[-1]["has_else"]:
                        raise ParseError("Unbalanced algorithmic ELSE")
                    stack[-1]["has_else"] = True
                    current = self.add("component", "else", stack[-1]["block"])
                else:
                    tail = re.sub(r"\\label\s*\{[^{}]*\}", "", fragment)
                    spacing_only = bool(re.fullmatch(r"\s*(?:\\setstretch\s*\{(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)\}\s*)+", tail))
                    if (tail.strip() and not spacing_only) or not stack or stack[-1]["kind"] != name[3:]:
                        raise ParseError("Unbalanced algorithmic " + name)
                    current = stack.pop()["parent"]
                    if spacing_only:
                        self.parse(tail, current, depth + 1)
            if stack:
                raise ParseError("Unclosed algorithmic control block")
        finally:
            self.pseudocode_depth -= 1

    def frontmatter(self, text, parent, depth):
        """Keep template title/author fields as evidence and parse real content."""
        out, pos = [], 0
        metadata = {"title", "runtitle", "author", "address", "ead", "paperid"}
        while pos < len(text):
            if text[pos] != "\\":
                out.append(text[pos])
                pos += 1
                continue
            name, end = command(text, pos)
            if name == "begin":
                env, start = group(text, end)
                # Do not interpret title-like words inside abstracts or keywords
                # as author metadata; normal recursive parsing validates them.
                raw, after = environment(text, start, env)
                out.append(text[pos:after])
                pos = after
                continue
            if name in metadata and name not in self.gatherer.macros:
                start = skip_space(text, end)
                optional = None
                if text[start:start + 1] == "[":
                    optional, start = group(text, start, "[", "]")
                value, end = group(text, start)
                self.layout_metadata.append(dict(command=name, frontmatter_class=self.gatherer.document_class, optional=optional, arguments=[value]))
                # Author/address lists sometimes separate declarations with a
                # literal comma. It belongs to that metadata, not an empty leaf.
                stop = skip_space(text, end)
                if text[stop:stop + 1] == ",":
                    self.layout_metadata[-1]["trailing_separator"] = ","
                    end = stop + 1
                out.append(" ")
            else:
                out.append(text[pos:end])
            pos = end
        self.frontmatter_depth += 1
        try:
            self.parse("".join(out), parent, depth)
        finally:
            self.frontmatter_depth -= 1

    def items(self, text, parent, depth):
        # A paragraph layout scope can wrap an actual list item. Expose it
        # before splitting items; otherwise that item would be silently merged
        # into its predecessor or rejected as prose. Keep balanced contents.
        cursor = 0
        while cursor < len(text):
            if text[cursor] != "\\":
                cursor += 1
                continue
            name, end = command(text, cursor)
            if name == "verb":
                start = end + int(text[end:end + 1] == "*")
                delimiter = text[start:start + 1]
                stop = text.find(delimiter, start + 1) if delimiter else -1
                if stop < 0:
                    raise ParseError("Unclosed list verbatim")
                cursor = stop + 1
                continue
            if name == "begin":
                env, start = group(text, end)
                if env == "sloppypar":
                    if env in self.gatherer.local_environments:
                        raise ParseError("Locally redefined sloppypar requires environment semantics")
                    raw, end = environment(text, start, env)
                    text = text[:cursor] + raw + text[end:]
                    continue
            cursor = end
        # Find top-level items only, ignoring commands nested in environments/braces.
        positions, pos, nesting, braces = [], 0, 0, 0
        while pos < len(text):
            if text[pos] == "\\":
                name, end = command(text, pos)
                if name in {"begin", "end"}:
                    _, end = group(text, end)
                    nesting += 1 if name == "begin" else -1
                elif name == "item" and nesting == 0 and braces == 0:
                    positions.append((pos, end))
                pos = end
            else:
                braces += int(text[pos] == "{") - int(text[pos] == "}")
                pos += 1
        if not positions:
            raise ParseError("List without explicit items")
        prefix = text[:positions[0][0]]
        if prefix.strip():
            # These literal list layout assignments precede the first item in
            # real sources. Validate their boundaries, then reuse normal layout
            # parsing; arbitrary text/macros are not silently discarded.
            cursor = 0
            while (cursor := skip_space(prefix, cursor)) < len(prefix):
                if prefix[cursor] != "\\":
                    raise ParseError("Unsupported text before first list item")
                name, cursor = command(prefix, cursor)
                if name in self.gatherer.macros:
                    raise ParseError("Locally redefined list prefix command: " + name)
                if name == "label":
                    label, cursor = group(prefix, cursor)
                    self.layout_metadata.append(dict(command="list_label", label=label, element_id=parent))
                elif name == "vspace":
                    cursor = skip_space(prefix, cursor)
                    cursor += int(prefix[cursor:cursor + 1] == "*")
                    value, cursor = group(prefix, cursor)
                    if not re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)\s*(?:pt|em|ex|mm|cm|in|bp|pc|sp)", value.strip()):
                        raise ParseError("Unsupported text before first list item: nonliteral vertical spacing")
                elif name == "setlength":
                    target, cursor = group(prefix, cursor)
                    if target.strip() not in {r"\itemsep", r"\leftskip", r"\parskip", r"\parsep"}:
                        raise ParseError("Unsupported text before first list item: layout target " + target)
                    _, cursor = group(prefix, cursor)
                elif name in {"itemsep", "leftskip"}:
                    match = re.match(r"\s*=?\s*[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)\s*(?:pt|em|ex|mm|cm|in|bp|pc|sp)(?![A-Za-z0-9.])", prefix[cursor:])
                    if not match:
                        raise ParseError("Unsupported text before first list item: nonliteral layout")
                    cursor += match.end()
                else:
                    raise ParseError("Unsupported text before first list item: \\" + name)
            self.parse(prefix, parent, depth)
        for idx, (_, start) in enumerate(positions):
            stop = positions[idx + 1][0] if idx + 1 < len(positions) else len(text)
            start = skip_space(text, start)
            label = f"item {idx + 1}"
            if text[start:start + 1] == "[":
                label, start = group(text, start, "[", "]")
            item = self.add("item", label, parent)
            self.parse(text[start:stop], item, depth)


def parse_versions(aid, sources):
    """One paper: gather per version, union bibliography, then build shared elements."""
    prepared, records = {}, []
    for version, path in sources:
        record = dict(version=version, status="parse_error", root=None, included_files=[],
                      bibliography_status=None, error=None)
        records.append(record)
        gatherer = None
        try:
            gatherer = Gatherer(path)
            root, body = gatherer.gather()
            record.update(root=root, included_files=gatherer.visited)
            body, entries, missing = bibliography(gatherer, body)
            record.update(root=root, included_files=gatherer.visited,
                          encodings=gatherer.encoding, bibliography_files=[p.relative_to(gatherer.root).as_posix() for p in (*gatherer.bib_files, *gatherer.bbl_files)],
                          bibliography_status=("bibtex+bbl" if gatherer.bib_files and gatherer.bbl_files else
                                               "bbl" if gatherer.bbl_files else
                                               "bibtex" if gatherer.bib_files else "bibitem" if entries else "missing"),
                          missing_citation_keys=missing, bibliography_evidence=entries)
            if gatherer.biblatex_rendering:
                record["bibliography_rendering_declarations"] = gatherer.biblatex_rendering
            if missing:
                raise ParseError("Cited bibliography entries unavailable: " + ", ".join(missing[:12]))
            prepared[version] = (gatherer, body, entries, record)
        except (ParseError, OSError, UnicodeError) as exc:
            record["error"] = str(exc)
        finally:
            if gatherer is not None and gatherer.root_selection:
                record["root_selection"] = gatherer.root_selection
            if gatherer is not None and gatherer.bbl_evidence:
                record["bbl_sources"] = gatherer.bbl_evidence
            if gatherer is not None and gatherer.missing_bib_resources:
                record["unavailable_bib_resources"] = gatherer.missing_bib_resources
    # Validate trees once before bibliography union: bad versions cannot change good keys.
    good = {}
    for version, (gatherer, body, entries, record) in prepared.items():
        try:
            tree = Tree(aid, version, gatherer, {})
            tree.parse(body)
            # Macro expansion can reveal citations absent from the literal body.
            # Never certify a tree whose cited keys lack bibliography evidence.
            used = {key for node in tree.nodes for key in citation_keys(node["content"])} - {"*"}
            unavailable = sorted(used - {entry["key"] for entry in entries})
            if unavailable:
                raise ParseError("Cited bibliography entries unavailable after expansion: " + ", ".join(unavailable[:12]))
            unify(aid, {version: entries})  # Reject an ambiguous key in this version only.
            good[version] = entries
        except ParseError as exc:
            record["error"] = str(exc)
    rows, aliases, conflicts = unify(aid, good)
    elements, hierarchy = {}, []
    for version, (gatherer, body, entries, record) in prepared.items():
        if version not in good:
            continue
        tree = Tree(aid, version, gatherer, aliases[version])
        tree.parse(body)
        for node in tree.nodes:
            previous = elements.setdefault(node["element_id"], node)
            if previous != node:
                raise ParseError("Element ID collision")
        hierarchy.extend(tree.edges)
        record.update(status="success", error=None, element_count=len(tree.nodes),
                      edge_count=len(tree.edges), bibliography_count=len(entries),
                      citation_aliases=aliases[version])
        notes = set(tree.warnings) | gatherer.math_aliases_used
        if notes:
            record["normalization_notes"] = sorted(notes)
        if tree.layout_metadata:
            record["layout_metadata"] = tree.layout_metadata
    return dict(elements=list(elements.values()), hierarchy=hierarchy, bibtex=rows,
                versions=records, bibliography_conflicts=conflicts)
