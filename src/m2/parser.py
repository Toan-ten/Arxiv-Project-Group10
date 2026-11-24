"""Compilation-path hierarchy parser; never executes TeX or external commands."""
from __future__ import annotations

import hashlib
import re

_CITATION_RE = re.compile(r"(?:[cC]ite[a-zA-Z]*|[tT]extcite|[pP]arencite|[aA]utocite|onlinecite)")
import unicodedata
import xml.etree.ElementTree as ET
from collections import Counter

from .bibliography import parse_bbl, parse_bibitems, parse_bibtex, unify, _top_level_bibitems, _bbl_comments
from .latex import Gatherer, ParseError, grouped_natbib_keys, literal_length_target, algorithm_declaration, citation_keys, command, environment, group, mask_literals, mask_root_inline_verbatim, normalize, options, listing_options, resolve_listing_options, box_options, sentences, skip_space, splice_tokens


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

SI_PREFIXES = {"y", "z", "a", "f", "p", "n", "u", "m", "c", "d", "da", "h", "k",
               "M", "G", "T", "P", "E", "Z", "Y"}
SI_BASE_UNITS = {"m", "g", "s", "A", "K", "mol", "cd", "Hz", "N", "Pa", "J", "W",
                 "C", "V", "F", "S", "Wb", "T", "H", "lm", "lx", "Bq", "Gy", "Sv",
                 "kat", "L", "eV"}
SI_UNIT_COMMANDS = ({
    # Spelled-out names and siunitx prefixes/qualifiers.
    "micro", "mu", "milli", "centi", "deci", "nano", "pico", "kilo", "mega",
    "giga", "tera", "per", "squared", "cubed", "square", "cubic",
    "metre", "meter", "second", "hertz", "gram", "kilogram", "kelvin", "tesla",
    "volt", "electronvolt", "joule", "watt", "ampere", "pascal", "percent", "%",
    "degree", "degreeCelsius", "celsius", "litre", "liter", "minute", "hour",
    "day", "tonne", "hectare", "bar", "bel", "decibel", "neper", "permille",
    "astronomicalunit", "atomicmassunit", "dalton",
} | SI_BASE_UNITS
  | {prefix + unit for prefix in SI_PREFIXES for unit in SI_BASE_UNITS})

# Documented by cvpr-org/author-kit, cvpr.sty (onedot definitions). Local
# definitions take precedence. This is not a general package interpreter.
CVPR_ABBREVIATIONS = {"etc": "etc.", "ie": "i.e."}
VISION_EXAMPLES = {"eg": "e.g.", "ie": "i.e.", "etal": "et al."}
VISION_PACKAGES = {"cvpr", "iccv", "wacv", "eccvabbrv"}




def literal_layout_glue(value):
    """A finite dimension plus optional finite stretch/shrink; no TeX execution."""
    dim = r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)\s*(?:pt|em|ex|mm|cm|in|bp|pc|sp)"
    return bool(re.fullmatch(dim + r"(?:\s+plus\s+" + dim + r")?(?:\s+minus\s+" + dim + r")?", value.strip()))


def roman_numeral(number):
    """Render the bounded literal Roman form used by the uppercase idiom."""
    number, rendered = max(0, number), ""
    for unit, letters in ((1000,"M"),(900,"CM"),(500,"D"),(400,"CD"),(100,"C"),(90,"XC"),(50,"L"),(40,"XL"),(10,"X"),(9,"IX"),(5,"V"),(4,"IV"),(1,"I")):
        count, number = divmod(number, unit)
        rendered += letters * count
    return rendered


def layout_column_spec(spec, argc, gatherer):
    """Validate a purely visual subset of array columns; never execute it."""
    pos, columns = 0, 0
    number = r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)"
    while (pos := skip_space(spec, pos)) < len(spec):
        char = spec[pos]
        if char in "lcr":
            columns += 1
            pos += 1
        elif char == "X" and "tabularx" in gatherer.packages:
            columns += 1
            pos += 1
        elif char in "pmb":
            width, pos = group(spec, pos + 1)
            valid = (re.fullmatch(number + r"\s*(?:pt|em|ex|mm|cm|in|bp|pc|sp)", width.strip())
                     or re.fullmatch(number + r"\s*\\(?:textwidth|linewidth|columnwidth)", width.strip()))
            references = re.findall(r"\\([A-Za-z]+)", width)
            if any(name in gatherer.macros for name in references):
                raise ParseError("Locally redefined custom column width register")
            parameter = re.fullmatch(r"#([1-9])", width.strip())
            if not valid and not (parameter and int(parameter[1]) <= argc):
                raise ParseError("Unsupported custom column width: " + width)
            columns += 1
        elif char in "><":
            decoration, pos = group(spec, pos + 1)
            cursor = 0
            while (cursor := skip_space(decoration, cursor)) < len(decoration):
                token, after = command(decoration, cursor)
                if token in gatherer.macros:
                    raise ParseError("Locally redefined custom column decoration: " + token)
                if token in {"centering", "raggedleft", "raggedright", "arraybackslash"}:
                    cursor = after
                elif token in {"RaggedLeft", "RaggedRight", "Centering"} and "ragged2e" in gatherer.packages:
                    cursor = after
                elif token == "columncolor" and "colortbl" in gatherer.packages:
                    color, cursor = group(decoration, after)
                    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9!._-]*", color.strip()):
                        raise ParseError("Unsupported custom column color")
                elif token == "hsize":
                    match = re.match(r"\s*=\s*" + number + r"\s*\\hsize(?![A-Za-z@])", decoration[after:])
                    if not match:
                        raise ParseError("Unsupported custom column hsize expression")
                    cursor = after + match.end()
                else:
                    raise ParseError("Custom column decoration can insert content or needs TeX semantics: " + token)
        elif char == "|":
            pos += 1
        else:
            raise ParseError("Unsupported custom column type syntax near " + spec[pos:pos+60])
    if not columns:
        raise ParseError("Custom column type has no alignment column")



def bibliography_groups(raw, gatherer):
    """Expose executed bibitems in literal groups and proved font/color wrappers.

    Other command arguments remain opaque, especially unused macro bodies.
    This projects structure only; source files are never modified.
    """
    # Existing BBL style guards own conditional setup and definitions. Do not
    # change that path or project groups across an unproved conditional.
    # Leave the established reader entirely unchanged when every literal
    # marker is already top-level. In particular, harmless BBL declarations
    # must not become new failures merely because this projection exists.
    try:
        markers = _top_level_bibitems(raw)
    except ParseError:
        markers = None  # Keep the explicit diagnostic from the projection.
    if markers is not None and len(markers) == len(re.findall(r"\\bibitem\b", raw)):
        return raw
    prefix = ""
    if re.search(r"\\(?:if[a-zA-Z]*|expandafter|else|fi)\b", raw):
        markers = _top_level_bibitems(raw)
        if not markers:
            return raw
        prefix, tail = raw[:markers[0][0]], raw[markers[0][0]:]
        if re.search(r"\\(?:if[a-zA-Z]*|expandafter|else|fi)\b", tail):
            return raw
        # Keep the complete setup verbatim for parse_bbl's existing guard.
        # Only unconditional item text following it is projected.
        raw = tail
    fonts = {"small", "footnotesize", "scriptsize", "normalsize", "tiny", "bf", "it", "em", "rm", "sc", "sf", "tt"}
    wrappers = {"textbf", "textit", "emph", "textrm", "textnormal"}
    def walk(text, depth=0):
        if depth > 60:
            raise ParseError("Bibliography grouping depth exceeds 60")
        out, cursor = [], 0
        while cursor < len(text):
            if text[cursor] == "{":
                value, end = group(text, cursor)
                if r"\bibitem" in value:
                    out.append(walk(value, depth+1))
                    gatherer.math_aliases_used.add("Bibliography: literal formatting group projected")
                else:
                    out.append(text[cursor:end])
                cursor = end
                continue
            if text[cursor] != "\\":
                out.append(text[cursor]); cursor += 1; continue
            name, end = command(text, cursor)
            if name in {"def", "gdef", "edef", "xdef", "let", "newcommand", "renewcommand", "providecommand", "DeclareRobustCommand"}:
                # Definitions are not reference invocations; do not interpret
                # their parameter tokens or expose replacement-body bibitems.
                raise ParseError("Local bibliography declaration requires scoped TeX semantics: " + name)
            if name == "bibitem":
                stop = skip_space(text, end)
                if text[stop:stop+1] == "[":
                    _, stop = group(text, stop, "[", "]")
                _, stop = group(text, stop)
                out.append(text[cursor:stop]); cursor = stop; continue
            definition = gatherer.macros.get(name)
            if name in fonts and definition is None:
                out.append(text[cursor:end]); cursor = end; continue
            transparent = name in wrappers and definition is None
            color = name == "textcolor" and definition is None and bool(gatherer.packages & {"color", "xcolor"})
            if definition and definition[:2] == (1, None):
                replacement = definition[2].strip()
                transparent = bool(re.fullmatch(r"\{?\s*\\color\{[A-Za-z]+\}\s*\{?#1\}?\s*\}?", replacement)) and "color" not in gatherer.macros and bool(gatherer.packages & {"color", "xcolor"})
                color = bool(re.fullmatch(r"\\textcolor\{[A-Za-z]+\}\{#1\}", replacement)) and "textcolor" not in gatherer.macros and bool(gatherer.packages & {"color", "xcolor"})
                # A common review wrapper appends literal horizontal spacing.
                # Prove the exact zero-argument alias and literal dimension;
                # unknown trailing macros or content are never discarded.
                spacing = re.fullmatch(r"\\textcolor\{[A-Za-z]+\}\{#1\}\s*\\([A-Za-z]+)\{([+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:pt|cm|mm|em|ex|in|bp|pc|sp))\}\s*", replacement)
                if spacing and gatherer.macros.get(spacing[1]) in {
                        (0, None, r"\hspace"), (0, None, r"\hspace*")}:
                    color = ("textcolor" not in gatherer.macros and "hspace" not in gatherer.macros
                             and bool(gatherer.packages & {"color", "xcolor"}))
            start = skip_space(text, end)
            if color and definition is None:
                color_name, start = group(text, start)
                if not re.fullmatch(r"[A-Za-z]+", color_name):
                    raise ParseError("Dynamic bibliography color wrapper")
            start = skip_space(text, start)
            if (transparent or color) and text[start:start+1] == "{":
                value, stop = group(text, start)
                if r"\bibitem" in value:
                    out.append(walk(value, depth+1))
                    gatherer.math_aliases_used.add("Bibliography: proved content-preserving wrapper " + name)
                    cursor = stop; continue
            if name == "color" and definition is None and gatherer.packages & {"color", "xcolor"}:
                _, stop = group(text, end)
                out.append(text[cursor:stop]); cursor = stop; continue
            if definition == (0, None, r"\color{black}") and "color" not in gatherer.macros and gatherer.packages & {"color", "xcolor"}:
                out.append(text[cursor:end]); cursor = end; continue
            # Unknown commands own their immediate balanced arguments. Never
            # expose a bibitem hidden in a macro definition or an unknown call.
            stop = end
            arity = {"href": 2, "url": 1, "path": 1, "doi": 1,
                     "textbf": 1, "textit": 1, "emph": 1, "textrm": 1, "textnormal": 1,
                     "textcolor": 2, "bibinfo": 2, "bibfield": 2}.get(name)
            consumed = 0
            while arity is None or consumed < arity:
                probe = skip_space(text, stop)
                if text[probe:probe+1] not in {"{", "["}:
                    break
                opening = text[probe]
                _, stop = group(text,probe,opening,"}" if opening=="{" else "]")
                consumed += 1
            out.append(text[cursor:stop]); cursor = stop
        return "".join(out)
    return prefix + walk(raw)


def bbl_item_group_projection(text, gatherer):
    """Reuse inline bibitem projection after an unchanged BBL setup prefix.

    Other BBL formats and setup still belong to their existing readers. A
    nested item group is not a missing key merely because its marker has
    nonzero brace depth. Unknown declarations/conditionals stay guarded.
    """
    cleaned = _bbl_comments(text)
    starts = list(re.finditer(r"\\begin\s*\{thebibliography\}", cleaned))
    if len(starts) != 1:
        return text
    _, start = group(cleaned, starts[0].end())
    raw, _ = environment(cleaned, start, "thebibliography")
    markers = _top_level_bibitems(raw)
    if not markers:
        return text  # No proven boundary between setup and item declarations.
    boundary = markers[0][0]
    tail = bibliography_groups(raw[boundary:], gatherer)
    if tail == raw[boundary:]:
        return text
    return cleaned[:start + boundary] + tail + cleaned[start + len(raw):]


def bibliography_citation_probe(body, gatherer):
    """Defer source-defined cite-prefixed invocations to the existing Tree.

    Their arguments may be section labels or locator text, not cite keys.
    Mask only a balanced invocation with the exact stored arity/default;
    Tree still expands and validates its actual content before success.
    """
    probe = mask_root_inline_verbatim(mask_literals(body))
    edits, cursor = [], 0
    while cursor < len(probe):
        if probe[cursor] != "\\":
            cursor += 1
            continue
        start = cursor
        name, cursor = command(probe, cursor)
        if not re.fullmatch(r"(?:[cC]ite[a-zA-Z]*|[tT]extcite|[pP]arencite|[aA]utocite|onlinecite|nocite)", name):
            continue
        definition = gatherer.macros.get(name)
        if definition is not None:
            argc, default, _ = definition
            if argc is None:
                raise ParseError("Parameterized citation macro requires TeX semantics: " + name)
            consumed = 0
            if default is not None:
                cursor = skip_space(probe, cursor)
                if probe[cursor:cursor + 1] == "[":
                    _, cursor = group(probe, cursor, "[", "]")
                consumed = 1
            for _ in range(argc - consumed):
                _, cursor = group(probe, cursor)
            edits.append((start, cursor))
        elif name == "citestyle" and gatherer.document_class == "acmart":
            _, cursor = group(probe, cursor)
            edits.append((start, cursor))
    for start, end in reversed(edits):
        probe = probe[:start] + " " * (end - start) + probe[end:]
    return probe


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
        raw = bibliography_groups(raw, gatherer)
        if re.search(r"\\(?:if[a-zA-Z]*|expandafter|else|fi)\b", raw):
            entries.extend(parse_bbl(r"\begin{thebibliography}{99}" + raw + r"\end{thebibliography}"))
        else:
            entries.extend(parse_bibitems(raw))
        body = body[:match.start()] + "\n" + body[end:]
    if ("natbib" in gatherer.packages or (gatherer.document_class or "").startswith("revtex")
            or gatherer.document_class == "aa"):
        normalized = grouped_natbib_keys(body)
        if normalized != body:
            gatherer.math_aliases_used.add("Redundant groups around literal natbib keys removed")
        body = normalized
    if "shortcite" not in gatherer.macros and (
            "natbib" in gatherer.packages
            or any(p.startswith(("acl", "emnlp", "aaai", "ijcai", "naacl", "eacl")) for p in gatherer.packages)):
        # Chuẩn hóa kiểu trích năm; giữ key và kiểm tra bibliography như citeyear.
        probe = mask_root_inline_verbatim(mask_literals(body))
        edits, cursor = [], 0
        while cursor < len(probe):
            if probe[cursor] != "\\":
                cursor += 1
                continue
            token, end = command(probe, cursor)
            if token == "shortcite":
                edits.append((cursor, end))
            cursor = end
        for start, end in reversed(edits):
            body = body[:start] + r"\citeyear" + body[end:]
        if edits:
            gatherer.math_aliases_used.add("Source shortcite normalized to citeyear; citation keys unchanged")
    citations = citation_keys(bibliography_citation_probe(body, gatherer))
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
            rendered = parse_bbl(bbl_item_group_projection(
                gatherer.read(path, comments=False), gatherer), enriched=True)
            scopes = {entry.get("biblatex_metadata", {}).get("refsection_id")
                      for entry in rendered if entry["provider"] == "biblatex_bbl"}
            if any(scope not in {None, "0"} for scope in scopes):
                raise ParseError("BibLaTeX refsections require source-scoped citation mapping; "
                                 "cannot merge into the M2 publication bibliography")
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
    controls = {entry["key"] for entry in entries if entry["entry_type"].lower()=="ieeetranbstctl"}
    gatherer.bst_control_keys = controls
    # Giữ ứng viên đã đọc từ đúng version đến khi biết citation thực sự được
    # gọi sau macro expansion; không xuất các entry chỉ có trong macro chưa dùng.
    gatherer.bibliography_candidates = [e for e in entries if e["key"] not in controls]
    # Manually rendered bibitems are included even without a \cite invocation.
    entries = [e for e in entries if e["entry_type"].lower()!="ieeetranbstctl" and (e["provider"].startswith("bibitem") or "*" in citations or e["key"] in citations)]
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
        self.twocolumn_depth = 0
        self.keyword_depth = 0
        self.listing_options = dict(getattr(gatherer, "listing_options", {}))
        self.algorithm_keywords = {}
        self.algorithm_functions = {}
        self.algorithm_programs = {}
        for declaration in getattr(gatherer, "algorithm_initial", []):
            command_name = declaration["command"]
            target = declaration["target"]
            if target in gatherer.macros:
                raise ParseError("Conflicting local macro/algorithm declaration needs order semantics")
            if command_name in {"SetKwInput", "SetKwInOut"}:
                self.algorithm_keywords[target] = declaration["label"]
            elif command_name == "SetKwFunction":
                self.algorithm_functions[target] = declaration["label"]
            else:
                self.algorithm_programs[target] = declaration
        self.frontmatter_depth = 0
        self.credits_depth = 0
        self.icml_notice_depth = 0
        self.restatements = {}
        self.layout_lengths = set()
        self.column_types = {}

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

    def listing_config(self, raw_options):
        """Dùng chung validation listings; không thực thi escape hoặc style động."""
        config = dict(self.listing_options)
        styles = config.pop("@styles", {})
        languages = config.pop("@languages", set())
        config.update(resolve_listing_options(raw_options or "", styles))
        language = config.get("language", "")
        # listings names are case insensitive; [dialect] is not part
        # of the base language identity. Neither form bypasses the
        # refusal to interpret a source-defined language.
        base_language = re.sub(r"^\[[^\]\\{}]*\]\s*", "", language).strip().casefold()
        if base_language in {value.strip().casefold() for value in languages}:
            raise ParseError("Source-defined listing language requires interpretation: " + language)
        # Formatting is preserved as evidence. TeX escape modes,
        # substitutions and selective line ranges require separate
        # semantics and cannot be passed off as literal code.
        unsafe = {"mathescape", "texcl", "escapechar", "escapeinside", "literate", "firstline", "lastline", "linerange", "firstnumber", "deletekeywords", "alsoletter", "alsodigit", "morecomment", "morestring", "moredelim"}
        for key in unsafe & config.keys():
            if key in {"mathescape", "texcl"} and config[key].strip().lower() == "false":
                continue
            raise ParseError("Unsupported lstlisting interpretation option: " + key)
        allowed = {"language", "frame", "aboveskip", "belowskip", "showstringspaces", "columns", "basicstyle", "numbers", "numberstyle", "keywordstyle", "commentstyle", "stringstyle", "breaklines", "breakatwhitespace", "tabsize", "xleftmargin", "breakindent", "caption", "title", "label", "mathescape", "texcl", "backgroundcolor", "rulecolor", "framerule", "captionpos", "keepspaces", "upquote", "lineskip", "xrightmargin", "numbersep", "showspaces", "showtabs", "stepnumber"}
        unknown = config.keys() - allowed
        if unknown:
            raise ParseError("Unsupported lstlisting option: " + sorted(unknown)[0])
        for key in {"basicstyle", "numberstyle", "keywordstyle", "commentstyle", "stringstyle"} & config.keys():
            style, cursor = config[key], 0
            while (cursor := skip_space(style, cursor)) < len(style):
                if style[cursor] != "\\":
                    raise ParseError("Unsupported visible text in lstlisting style: " + key)
                token, cursor = command(style, cursor)
                if token in self.gatherer.macros:
                    raise ParseError("Locally redefined lstlisting style command: " + token)
                if token == "color":
                    color, cursor = group(style, cursor)
                    if not re.fullmatch(r"[A-Za-z0-9!._:-]+", color):
                        raise ParseError("Unsupported dynamic lstlisting style color: " + key)
                elif token not in {"small", "footnotesize", "tiny", "scriptsize", "ttfamily", "bfseries", "itshape", "normalfont", "normalsize"}:
                    raise ParseError("Unsupported executable lstlisting style: " + key)
        for flag in {"showspaces", "showtabs"} & config.keys():
            if config[flag] not in {"true", "false"}:
                raise ParseError("Unsupported listing display flag: " + flag)
        if "stepnumber" in config and not re.fullmatch(r"[1-9][0-9]*", config["stepnumber"]):
            raise ParseError("Unsupported listing number step")
        if "upquote" in config and config["upquote"] not in {"true","false"}:
            raise ParseError("Unsupported lstlisting upquote flag")
        for padding in {"lineskip","xrightmargin","numbersep"} & config.keys():
            if not literal_layout_glue(config[padding]):
                raise ParseError("Unsupported dynamic listing padding: " + padding)
        if "keepspaces" in config and config["keepspaces"] not in {"true", "false"}:
            raise ParseError("Unsupported lstlisting keepspaces flag")
        if "captionpos" in config and config["captionpos"] not in {"t", "b"}:
            raise ParseError("Unsupported lstlisting caption position")
        if "framerule" in config and not re.fullmatch(r"[0-9.]+(?:pt|em|ex|mm|cm)", config["framerule"]):
            raise ParseError("Unsupported lstlisting frame dimension")
        for key in {"backgroundcolor", "rulecolor"} & config.keys():
            if not re.fullmatch(r"\\color\s*\{[A-Za-z0-9!._:-]+\}", config[key]) or "color" in self.gatherer.macros:
                raise ParseError("Unsupported executable listing color: " + key)
        return config

    def parse_algorithm_argument(self, text, parent, depth):
        """Gi? binding algorithm2e c?c b? trong t?ng argument/block."""
        saved = (self.algorithm_keywords.copy(), self.algorithm_functions.copy(), self.algorithm_programs.copy())
        try:
            self.parse(text, parent, depth)
        finally:
            self.algorithm_keywords, self.algorithm_functions, self.algorithm_programs = saved

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
                if re.search(r"\\lstset(?![A-Za-z@])", mask_root_inline_verbatim(mask_literals(value))):
                    raise ParseError("Scoped lstset requires explicit TeX group semantics")
                if self.algorithm2e_depth and re.search(
                        r"\\(?:SetKwInput|SetKwInOut|SetKwFunction|SetKwProg)\b",
                        mask_root_inline_verbatim(mask_literals(value))):
                    raise ParseError("Algorithm declarations in literal groups require scoped TeX semantics")
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
                if self.credits_depth:
                    labels = {"ackname": "Acknowledgments", "discintname": "Disclosure of Interests"}
                    for label, value in labels.items():
                        if title.strip() == "\\" + label and label not in self.gatherer.macros:
                            title = value
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
            elif (self.credits_depth and name in {"ackname", "discintname"}
                  and name not in self.gatherer.macros):
                buffer.append({"ackname": "Acknowledgments", "discintname": "Disclosure of Interests"}[name])
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
                if self.gatherer.document_class == "WileyNJDv5" and text[start:start+1] == "[":
                    label, start = group(text, start, "[", "]")
                    if label.strip().casefold() != "abstract":
                        raise ParseError("Unsupported Wiley abstract label")
                    self.layout_metadata.append(dict(command=name, label=label))
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
            elif name in {"ttfamily", "sffamily", "scshape", "Huge", "frenchspacing",
                          "medbreak", "smallbreak", "bigbreak", "goodbreak", "null", "thinspace"} and name not in self.gatherer.macros:
                # Declaration/spacing của kernel không lấy group theo sau làm argument.
                self.layout_metadata.append(dict(command=name))
                if name in {"medbreak", "smallbreak", "bigbreak", "goodbreak", "thinspace"}:
                    buffer.append(" ")
                end = skip_space(text, end)
            elif name in {"ae", "AE", "textquotesingle", "texttrademark", "textsection",
                          "textsterling", "textperiodcentered"} and name not in self.gatherer.macros:
                buffer.append({"ae":"æ", "AE":"Æ", "textquotesingle":"'", "texttrademark":"™",
                    "textsection":"§", "textsterling":"£", "textperiodcentered":"·"}[name])
                end = skip_space(text, end)
            elif name in {"aa", "oe", "textquotedbl", "textdagger", "textvisiblespace"} and name not in self.gatherer.macros:
                buffer.append({"aa":"\u00e5", "oe":"\u0153", "textquotedbl":'"',
                               "textdagger":"\u2020", "textvisiblespace":"\u2423"}[name])
                end = skip_space(text, end)
            elif name in {"enspace", "enskip", "negthinspace", "fussy"} and name not in self.gatherer.macros:
                if name in {"enspace", "enskip"}:
                    buffer.append(" ")
                self.layout_metadata.append(dict(command=name))
                end = skip_space(text, end)
            elif name in {"authormark", "titlemark", "corres", "presentaddress"} and self.gatherer.document_class == "WileyNJDv5" and name not in self.gatherer.macros:
                value, end = group(text, end)
                self.layout_metadata.append(dict(command=name, arguments=[value]))
            elif name == "datastatement" and (self.gatherer.document_class or "").startswith("ametsocV6") and name not in self.gatherer.macros:
                if depth != 0:
                    raise ParseError("Nested data-availability declaration")
                flush()
                headings.clear()
                reference_section, reference_rank = False, None
                division = self.add("component", "data availability statement", current())
                headings.append((LEVELS["section"], division))
            elif name == "appendix" and (self.gatherer.document_class or "").startswith("ametsocV6") and name not in self.gatherer.macros:
                if depth != 0:
                    raise ParseError("Nested AMETSOC appendix declaration")
                start = skip_space(text, end); marker = None
                if text[start:start+1] == "[":
                    marker, end = group(text, start, "[", "]")
                    if not re.fullmatch(r"[A-Z]", marker):
                        raise ParseError("Unsupported AMETSOC appendix marker")
                flush()
                headings.clear()
                reference_section, reference_rank = False, None
                division = self.add("component", "appendix" + (" " + marker if marker else ""), current())
                self.ametsoc_appendix = division
                headings.append((LEVELS["part"], division))
                self.layout_metadata.append(dict(command=name, marker=marker))
            elif name == "appendixtitle" and (self.gatherer.document_class or "").startswith("ametsocV6") and name not in self.gatherer.macros:
                if not getattr(self, "ametsoc_appendix", None) or not any(eid==self.ametsoc_appendix for _,eid in headings):
                    raise ParseError("Appendix title without active AMETSOC appendix")
                title, end = group(text, end)
                flush()
                while headings and headings[-1][0] >= LEVELS["section"]:
                    headings.pop()
                heading = self.add("heading", title, current())
                headings.append((LEVELS["section"], heading))
            elif name == "hrulefill" and name not in self.gatherer.macros:
                buffer.append(r"\hrulefill{}")
                end = skip_space(text, end)
            elif name == "nolinebreak" and name not in self.gatherer.macros:
                start = skip_space(text, end); strength = None
                if text[start:start+1] == "[":
                    strength, end = group(text, start, "[", "]")
                    if not re.fullmatch(r"[0-4]", strength):
                        raise ParseError("Unsupported line-break strength")
                self.layout_metadata.append(dict(command=name, strength=strength))
            elif name == "normalem" and "ulem" in self.gatherer.packages and name not in self.gatherer.macros:
                self.layout_metadata.append(dict(command=name))
            elif name in {"smaller", "larger"} and "relsize" in self.gatherer.packages and name not in self.gatherer.macros:
                start = skip_space(text, end); steps = None
                if text[start:start+1] == "[":
                    steps, end = group(text, start, "[", "]")
                    if not re.fullmatch(r"[1-9]", steps):
                        raise ParseError("Unsupported relative font declaration")
                self.layout_metadata.append(dict(command=name, steps=steps))
            elif name == "justifying" and "ragged2e" in self.gatherer.packages and name not in self.gatherer.macros:
                self.layout_metadata.append(dict(command=name))
            elif name == "textbar" and name not in self.gatherer.macros:
                buffer.append("|")
                end = skip_space(text, end)
            elif name == "arxivnumber" and self.gatherer.packages & {"jheppub", "jcappub"} and name not in self.gatherer.macros:
                value, end = group(text, end)
                self.layout_metadata.append(dict(command=name, arguments=[value]))
            elif name == "footref" and self.gatherer.packages & {"footmisc", "footref"} and name not in self.gatherer.macros:
                label, end = group(text, end)
                if not label or re.search(r"[\\{}\s]", label):
                    raise ParseError("Unsupported dynamic footnote reference")
                buffer.append(text[pos:end])  # Giữ label, không tự điền số footnote.
            elif name == "IEEEoverridecommandlockouts" and self.gatherer.document_class in {"IEEEtran", "IEEEtai", "ieeecolor"} and name not in self.gatherer.macros:
                self.layout_metadata.append(dict(command=name))
            elif (name == "copyrightyear" and self.gatherer.document_class == "acmart"
                  or name == "emailAdd" and self.gatherer.packages & {"jheppub", "jcappub"}
                  or name == "curraddr" and self.gatherer.document_class in {"amsart", "amsproc", "amsbook"}) and name not in self.gatherer.macros:
                start = skip_space(text, end); marker = None
                if name == "curraddr" and text[start:start+1] == "[":
                    marker, start = group(text, start, "[", "]")
                value, end = group(text, start)
                self.layout_metadata.append(dict(command=name, marker=marker, arguments=[value]))
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
            elif (name in {"flushbottom", "normalfont", "onecolumngrid", "twocolumngrid"}
                  and name not in self.gatherer.macros):
                if (name in {"onecolumngrid", "twocolumngrid"}
                        and not ("ltxgrid" in self.gatherer.packages
                                 or (self.gatherer.document_class or "").startswith(("revtex", "aastex")))):
                    raise ParseError("Undeclared grid command: " + name)
                # Font selection is a declaration, not a one-argument command.
                # Following groups, math and citations remain in the parse stream.
                if name == "normalfont":
                    end = skip_space(text, end)
                else:
                    buffer.append(" ")
                self.layout_metadata.append(dict(command=name))
            elif name in {"baselineskip", "parskip", "parindent"} and name not in self.gatherer.macros:
                # A direct literal glue-register assignment, not a dimension
                # expression or a command to drop along with following prose.
                number = r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)"
                dimension = number + r"\s*(?:pt|em|ex|mm|cm|in|bp|pc|sp)(?![A-Za-z@])"
                match = re.match(r"\s*=?\s*" + dimension
                                 + r"(?:\s+plus\s+" + dimension + r")?"
                                 + r"(?:\s+minus\s+" + dimension + r")?", text[end:])
                if not match:
                    raise ParseError("Unsupported nonliteral " + ("baseline" if name == "baselineskip" else "paragraph") + " glue assignment")
                end += match.end()
                tail = text[end:]
                if re.match(r"\s*(?:plus|minus)(?![A-Za-z@])", tail):
                    raise ParseError("Unsupported nonliteral " + ("baseline" if name == "baselineskip" else "paragraph") + " glue stretch/shrink")
                self.layout_metadata.append(dict(command=name, value=match[0].strip()))
            elif name == "pagenumbering" and name not in self.gatherer.macros:
                style, end = group(text, end)
                if style.strip() not in {"arabic", "roman", "Roman", "alph", "Alph", "gobble"}:
                    raise ParseError("Unsupported page numbering style: " + style)
                self.layout_metadata.append(dict(command=name, style=style.strip()))
            elif (name == "@" and name not in self.gatherer.macros):
                # LaTeX's sentence-space factor: no printable character and
                # no argument. Keep whitespace and the next token intact.
                self.layout_metadata.append(dict(command=name))
            elif name in {"textmu", "lq", "rq", "textasciitilde", "texttildelow"} and name not in self.gatherer.macros:
                # Kernel/textcomp glyphs. Keep tilde as a command because the
                # normalizer treats a literal ~ as a nonbreaking space.
                buffer.append({"textmu": "\u00b5", "lq": "\u2018", "rq": "\u2019",
                               "textasciitilde": r"\textasciitilde{}", "texttildelow": r"\textasciitilde{}"}[name])
                end = skip_space(text, end)
            elif name in {"o", "O", "ss", "i", "j"} and name not in self.gatherer.macros:
                buffer.append({"o":"\u00f8", "O":"\u00d8", "ss":"\u00df", "i":"\u0131", "j":"\u0237"}[name])
                end = skip_space(text, end)
            elif name == "textregistered" and name not in self.gatherer.macros:
                buffer.append("\u00ae")
                end = skip_space(text, end)
            elif name == "dotfill" and name not in self.gatherer.macros:
                # Keep the leader token, not an invented number of dots/pages.
                buffer.append(r"\dotfill{}")
                end = skip_space(text, end)
            elif name in {"textsmaller", "textlarger"} and "relsize" in self.gatherer.packages and name not in self.gatherer.macros:
                start = skip_space(text, end)
                steps = None
                if text[start:start + 1] == "[":
                    steps, start = group(text, start, "[", "]")
                    if not re.fullmatch(r"[1-9]", steps):
                        raise ParseError("Unsupported dynamic relative font size")
                raw, end = group(text, start)
                self.layout_metadata.append(dict(command=name, steps=steps))
                text = splice_tokens(text, pos, raw, end)
                continue
            elif name == "space" and name not in self.gatherer.macros:
                buffer.append(" ")
                end = skip_space(text, end)
            elif name == "degree" and "gensymb" in self.gatherer.packages and name not in self.gatherer.macros:
                buffer.append("\u00b0")
                end = skip_space(text, end)
            elif name == "pageref" and name not in self.gatherer.macros:
                start = end + int(text[end:end + 1] == "*" and "hyperref" in self.gatherer.packages)
                label, end = group(text, start)
                if not re.fullmatch(r"[^\\{}\s]+", label):
                    raise ParseError("Unsupported dynamic page reference")
                # Page numbers require TeX compilation. Preserve the source
                # reference exactly, as for ref, rather than inventing a page.
                buffer.append(text[pos:end])
            elif name in {"hyperlink", "hypertarget"} and "hyperref" in self.gatherer.packages and name not in self.gatherer.macros:
                target, start = group(text, end)
                visible, end = group(text, start)
                if not target or re.search(r"[\\{}\s]", target):
                    raise ParseError("Unsupported dynamic hyperlink anchor")
                self.layout_metadata.append(dict(command=name, anchor=target))
                text = splice_tokens(text, pos, visible, end)
                continue
            elif name == "rotatebox" and self.gatherer.packages & {"graphicx", "graphics"} and name not in self.gatherer.macros:
                start = skip_space(text, end)
                settings = None
                if text[start:start + 1] == "[":
                    settings, start = group(text, start, "[", "]")
                    if not re.fullmatch(r"\s*origin\s*=\s*[clrtbB]{1,2}\s*", settings):
                        raise ParseError("Unsupported rotatebox origin/options")
                angle, start = group(text, start)
                if not re.fullmatch(r"[+-]?(?:[0-9]{1,4}(?:\.[0-9]+)?|\.[0-9]+)",angle.strip()) or abs(float(angle)) > 3600:
                    raise ParseError("Unsupported nonliteral rotatebox angle")
                raw, end = group(text, start)
                self.layout_metadata.append(dict(command=name, angle=angle, options=settings))
                text = splice_tokens(text, pos, raw, end)
                continue
            elif name == "enlargethispage" and name not in self.gatherer.macros:
                start = end + int(text[end:end + 1] == "*")
                length, end = group(text, start)
                scaled = re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)\\baselineskip",length.strip())
                literal = re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)\s*(?:pt|em|ex|mm|cm|in|bp|pc|sp)",length.strip())
                if not literal and not (scaled and "baselineskip" not in self.gatherer.macros):
                    raise ParseError("Unsupported nonliteral page enlargement")
                self.layout_metadata.append(dict(command=name, length=length))
            elif name in {"strut", "raggedbottom", "leavevmode", "sl"} and name not in self.gatherer.macros:
                # Strut/mode/font/page-height declarations have no visible
                # argument. Do not consume the following group or prose.
                self.layout_metadata.append(dict(command=name))
                end = skip_space(text, end)
            elif (name == "nolinenumbers" and name not in self.gatherer.macros
                  and (self.gatherer.packages & {"lineno", "linenoaa"}
                       or (self.gatherer.document_class or "").startswith("aastex"))):
                self.layout_metadata.append(dict(command=name))
            elif (name == "dataset" and name not in self.gatherer.macros
                  and (self.gatherer.document_class or "").startswith("aastex")):
                start = skip_space(text, end)
                target = None
                if text[start:start + 1] == "[":
                    target, start = group(text, start, "[", "]")
                label, end = group(text, start)
                original_target = target
                if target is not None:
                    if re.fullmatch(r"10\.[0-9]{4,9}/[^\s{}\\]+", target):
                        target = "https://doi.org/" + target
                    elif not re.fullmatch(r"https?://[^\s{}\\]+", target):
                        raise ParseError("Unsupported dynamic AASTeX dataset target")
                self.layout_metadata.append(dict(command=name, identifier=original_target, target=target, label=label))
                if target is None:
                    text = splice_tokens(text, pos, label, end)
                else:
                    text = splice_tokens(text, pos, r"\href{" + target + "}{" + label + "}", end)
                continue
            elif (name in {"crefname", "Crefname"} and "cleveref" in self.gatherer.packages
                  and name not in self.gatherer.macros):
                target, start = group(text, end)
                singular, start = group(text, start)
                plural, end = group(text, start)
                if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", target):
                    raise ParseError("Unsupported dynamic cleveref name target")
                if not all(re.fullmatch(r"[A-Za-z .-]+", v) for v in (singular, plural)):
                    raise ParseError("Unsupported executable cleveref name")
                self.layout_metadata.append(dict(command=name, target=target, singular=singular, plural=plural))
            elif (name in {"farcs", "farcm", "fdg"} and name not in self.gatherer.macros
                  and (self.gatherer.document_class or "").startswith("aastex")):
                # Fractional angular-unit glyphs carry scientific notation.
                # Retain the explicit token rather than inventing a number or
                # replacing its decimal-position convention by an ordinary dot.
                buffer.append("\\" + name + "{}")
                end = skip_space(text, end)
            elif name == "textbackslash" and name not in self.gatherer.macros:
                # Keep a literal glyph as canonical LaTeX, NOT as an active
                # backslash that could turn the following word into a command.
                buffer.append(r"\textbackslash{}")
                end = skip_space(text, end)  # control-word delimiter whitespace
            elif (name == "subtitle" and name not in self.gatherer.macros
                  and self.gatherer.document_class in {"aa", "acmart", "llncs"}):
                if depth != 0:
                    raise ParseError("Nested title subtitle requires template semantics")
                raw, end = group(text, end)
                self.layout_metadata.append(dict(command=name, arguments=[raw]))
                if raw.strip():
                    flush()
                    container = self.add("component", "subtitle", current())
                    self.parse(raw, container, depth + 1)
            elif (name == "received" and name not in self.gatherer.macros
                  and (self.gatherer.document_class == "acmart"
                       or (self.gatherer.document_class or "").startswith("aastex"))):
                if depth != 0:
                    raise ParseError("Nested received metadata requires template semantics")
                start, stage = skip_space(text, end), None
                if text[start:start + 1] == "[":
                    stage, start = group(text, start, "[", "]")
                    if self.gatherer.document_class != "acmart" or stage not in {"revised", "accepted"}:
                        raise ParseError("Unsupported received metadata stage: " + stage)
                raw, end = group(text, start)
                self.layout_metadata.append(dict(command=name, stage=stage, arguments=[raw]))
                if raw.strip():
                    flush()
                    container = self.add("component", "received" + (" (" + stage + ")" if stage else ""), current())
                    self.parse(raw, container, depth + 1)
            elif (name == "icmlEqualContribution" and name not in self.gatherer.macros
                  and any(re.fullmatch(r"icml20[0-9]{2}(?:_arxiv)?", p) for p in self.gatherer.packages)):
                if not self.icml_notice_depth:
                    raise ParseError("ICML equal-contribution marker outside author notice")
                # Preserve the semantic marker verbatim. The unavailable style
                # may choose its wording/footnote marker; do not invent either.
                buffer.append(r"\icmlEqualContribution{}")
                end = skip_space(text, end)
                self.warnings.append("ICML author notice retains equal-contribution marker; original style wording not interpreted")
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
                elif model == "HTML" and "xcolor" in self.gatherer.packages:
                    if not re.fullmatch(r"[0-9A-Fa-f]{6}", spec.strip()):
                        raise ParseError("Invalid literal HTML color")
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
                  and ((self.gatherer.document_class or "").startswith("revtex") or (self.gatherer.document_class or "").startswith("aastex"))):
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
                  and self.gatherer.packages & {"cvpr", "iccv"}):
                # CVPR/ICCV render a repeated paper title plus this fixed division
                # label. The existing title-metadata policy omits the repeat,
                # but keep the supplement boundary and ALL following content.
                flush()
                front_matter = reference_section = False
                reference_rank = None
                headings.clear()
                marker = self.add("heading", "Supplementary Material", self.root)
                headings.append((LEVELS["section"], marker))
                convention = "cvpr" if "cvpr" in self.gatherer.packages else "iccv"
                self.layout_metadata.append(dict(command=name, convention=convention, repeated_title="document title metadata"))
                self.warnings.append(convention.upper() + " supplementary title convention; original style file not interpreted")
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
                    buffer.append(roman_numeral(int(literal[1])))
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
                    self.twocolumn_depth += 1
                    try:
                        self.parse(raw, current(), depth + 1)
                    finally:
                        self.twocolumn_depth -= 1
                self.layout_metadata.append(dict(command=name))
            elif name == "resizebox" and self.gatherer.packages & {"graphicx", "graphics"} and name not in self.gatherer.macros:
                start = skip_space(text, end)
                star = text[start:start + 1] == "*"
                start += int(star)
                width, start = group(text, start)
                height, start = group(text, start)
                for dimension in (width, height):
                    if dimension.strip() == "!":
                        continue
                    refs = re.findall(r"\\([A-Za-z@]+)", dimension)
                    valid = re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)\s*(?:pt|em|ex|mm|cm|in|bp|pc|sp)", dimension.strip())
                    valid = valid or re.fullmatch(r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)?\\(?:textwidth|linewidth|columnwidth|textheight)",dimension.strip())
                    if not valid or self.gatherer.macros.keys() & set(refs):
                        raise ParseError("Unsupported dynamic resizebox dimension")
                if width.strip() == height.strip() == "!":
                    raise ParseError("Resizebox needs a literal dimension")
                raw, end = group(text, start)
                self.layout_metadata.append(dict(command=name, starred=star, width=width, height=height))
                text = splice_tokens(text, pos, raw, end)
                continue
            elif name == "fontfamily" and name not in self.gatherer.macros:
                family, end = group(text, end)
                if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}",family):
                    raise ParseError("Unsupported dynamic font family")
                self.layout_metadata.append(dict(command=name, family=family))
            elif name == "linespread" and name not in self.gatherer.macros:
                factor, end = group(text, end)
                if not re.fullmatch(r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)",factor.strip()) or not 0 < float(factor) <= 10:
                    raise ParseError("Unsupported line spread factor")
                self.layout_metadata.append(dict(command=name, factor=factor))
            elif name == "nopagebreak" and name not in self.gatherer.macros:
                start = skip_space(text,end)
                strength = None
                if text[start:start+1] == "[":
                    strength,end = group(text,start,"[","]")
                    if not re.fullmatch(r"[0-4]",strength):
                        raise ParseError("Unsupported page-break strength")
                self.layout_metadata.append(dict(command=name,strength=strength))
            elif name in {"figurename", "tablename"} and name not in self.gatherer.macros:
                # Language/style determines its visible name; preserve the token.
                buffer.append("\\" + name + "{}")
                end = skip_space(text,end)
            elif (name == "urladdr" and self.gatherer.document_class in {"amsart","amsproc","amsbook"}
                  or name == "authors" and self.gatherer.document_class == "agujournal2019") and name not in self.gatherer.macros:
                value,end = group(text,end)
                self.layout_metadata.append(dict(command=name,arguments=[value]))
            elif name == "tnotetext" and self.gatherer.document_class in {"elsarticle","cas-sc","cas-dc"} and name not in self.gatherer.macros:
                start=skip_space(text,end);marker=None
                if text[start:start+1]=="[":
                    marker,start=group(text,start,"[","]")
                    if not re.fullmatch(r"[A-Za-z0-9:,_ -]+",marker):
                        raise ParseError("Unsupported title-note marker")
                raw,end=group(text,start);flush()
                note=self.add("component","title note",current())
                self.parse(raw,note,depth+1)
                self.layout_metadata.append(dict(command=name,marker=marker,raw=raw))
            elif name == "pacs" and self.gatherer.document_class == "sn-jnl" and name not in self.gatherer.macros:
                start=skip_space(text,end);label=None
                if text[start:start+1]=="[":
                    label,start=group(text,start,"[","]")
                value,end=group(text,start)
                if label is not None and not re.fullmatch(r"[A-Za-z -]+",label):
                    raise ParseError("Unsupported classification label")
                self.layout_metadata.append(dict(command=name,label=label,arguments=[value]))
                if value.strip():
                    flush()
                    classification = self.add("component", label or "PACS classification", current())
                    self.parse(value, classification, depth + 1)
            elif name == "MSC" and self.keyword_depth and self.gatherer.document_class=="elsarticle" and name not in self.gatherer.macros:
                start=skip_space(text,end);edition=None
                if text[start:start+1]=="[":
                    edition,start=group(text,start,"[","]")
                    if not re.fullmatch(r"\d{4}",edition):
                        raise ParseError("Unsupported MSC edition")
                value=text[start:]
                probe=re.sub(r"\\sep\b",";",value)
                if not re.fullmatch(r"[0-9A-Z;,\s-]+",probe):
                    raise ParseError("Unsupported MSC classification tokens")
                flush()
                classification=self.add("component","MSC classification",current())
                self.parse(probe,classification,depth+1)
                self.layout_metadata.append(dict(command=name,edition=edition,arguments=[value]))
                end=len(text)
            elif name == "firstsection" and self.gatherer.document_class == "vgtc" and name not in self.gatherer.macros:
                # VGTC's first section is a section before maketitle, not an
                # independent recursive parse (which would lose later children).
                text = splice_tokens(text, pos, r"\section", end)
                continue
            elif name == "bsp" and self.gatherer.document_class == "mnras" and name not in self.gatherer.macros:
                self.layout_metadata.append(dict(command=name, meaning="MNRAS typesetting marker"))
            elif name == "appendixpage" and "appendix" in self.gatherer.packages and name not in self.gatherer.macros:
                # A separate title page, not the end of the compilation path.
                # Retain the marker; subsequent real appendix headings are parsed.
                flush()
                self.layout_metadata.append(dict(command=name, meaning="appendix title page"))
            elif name == "nameref" and self.gatherer.packages & {"nameref", "hyperref"} and name not in self.gatherer.macros:
                start = skip_space(text, end)
                if text[start:start + 1] == "*":
                    if "hyperref" not in self.gatherer.packages:
                        raise ParseError("Unverified starred nameref provider")
                    start += 1
                label, end = group(text, start)
                if not re.fullmatch(r"[A-Za-z0-9:_.+/-]+", label):
                    raise ParseError("Unsupported dynamic name reference")
                buffer.append(text[pos:end])  # Never invent a referenced title.
            elif name == "uline" and "ulem" in self.gatherer.packages and name not in self.gatherer.macros:
                raw, end = group(text, end)
                text = splice_tokens(text, pos, raw, end)
                continue
            elif name == "LinesNumbered" and "algorithm2e" in self.gatherer.packages and name not in self.gatherer.macros:
                self.layout_metadata.append(dict(command=name, meaning="algorithm line numbering"))
            elif name == "credit" and self.gatherer.document_class in {"cas-dc", "cas-sc"} and name not in self.gatherer.macros:
                raw, end = group(text, end)
                # Like the adjacent CAS author declarations, keep the complete
                # contribution value and its order in working metadata.
                self.layout_metadata.append(dict(command=name, arguments=[raw]))
            elif name == "hrule" and name not in self.gatherer.macros:
                flush()
                cursor, dimensions = end, []
                while True:
                    start = skip_space(text, cursor)
                    keyword = re.match(r"(?:width|height|depth)\b", text[start:])
                    if not keyword:
                        break
                    value_start = skip_space(text, start + len(keyword[0]))
                    value = re.match(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)\s*(?:pt|em|ex|mm|cm|in|bp|pc|sp)(?![A-Za-z])", text[value_start:])
                    if not value:
                        raise ParseError("Unsupported dynamic hrule dimension")
                    cursor = value_start + len(value[0])
                    dimensions.append((keyword[0], value[0]))
                end = cursor
                self.layout_metadata.append(dict(command=name, dimensions=dimensions))
            elif name == "proof" and name not in self.gatherer.macros and "endproof" not in self.gatherer.macros and (
                    "amsthm" in self.gatherer.packages or self.gatherer.document_class in {"amsart", "amsproc", "amsbook"}):
                if "proof" in self.gatherer.local_environments:
                    raise ParseError("Local proof declaration requires command semantics")
                start = skip_space(text, end)
                title = None
                if text[start:start+1] == "[":
                    title, start = group(text, start, "[", "]")
                probe = mask_root_inline_verbatim(mask_literals(text))
                cursor, nesting, stop = start, 0, None
                while cursor < len(probe):
                    if probe[cursor] == "\\":
                        token_start = cursor
                        token, cursor = command(probe, cursor)
                        if token == "proof" and not nesting:
                            raise ParseError("Nested proof command requires scope semantics")
                        if token == "endproof" and not nesting:
                            stop, end = token_start, cursor
                            break
                    else:
                        nesting += int(probe[cursor] == "{") - int(probe[cursor] == "}")
                        if nesting < 0:
                            raise ParseError("Unbalanced proof command body")
                        cursor += 1
                if stop is None:
                    raise ParseError("Unclosed proof command body")
                flush()
                container = self.add("component", "proof", current())
                if title is not None:
                    self.parse(title, container, depth + 1)
                self.parse(text[start:stop], container, depth + 1)
            elif name == "backmatter" and name not in self.gatherer.macros:
                # This phase marker has no text argument and does not end the
                # document. Keep subsequent headings, notes and appendices.
                front_matter = False
                flush()
            elif name == "lstset" and "listings" in self.gatherer.packages and name not in self.gatherer.macros:
                if depth or re.search(r"\\(?:begingroup|endgroup|bgroup|egroup)\b", mask_root_inline_verbatim(mask_literals(text))):
                    raise ParseError("Scoped lstset requires explicit TeX group semantics")
                raw_options, end = group(text, end)
                self.listing_config(raw_options)  # Validate before changing this tree's state.
                updates = resolve_listing_options(raw_options, self.listing_options.get("@styles", {}))
                self.listing_options.update(updates)
                self.layout_metadata.append(dict(command=name, options=updates))
            elif name == "selectlanguage" and "babel" in self.gatherer.packages and name not in self.gatherer.macros:
                language, end = group(text, end)
                if language != "english":
                    raise ParseError("Unsupported Babel language activation: " + language)
                self.layout_metadata.append(dict(command=name, language=language))
            elif name == "begin":
                env, start = group(text, end)
                if env not in {"CJK","CJK*","math"}:
                    front_matter = False
                    flush()
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
                    config = self.listing_config(optional_title)
                    self.layout_metadata.append(dict(environment=env, options=config, raw_options=optional_title))
                    container = self.add("component", "code listing", current())
                    for key in ("caption", "title"):
                        if config.get(key):
                            caption = self.add("component", key, container)
                            self.parse(config[key], caption, depth + 1)
                    self.add("verbatim", r"\begin{lstlisting}" + raw + r"\end{lstlisting}", container, literal=True)
                elif env in {"CJK","CJK*"} and self.gatherer.packages & {"CJK","CJKutf8"}:
                    if env in self.gatherer.local_environments or optional_title is not None:
                        raise ParseError("Unsupported/local CJK environment")
                    encoding,cursor=group(raw,0);family,cursor=group(raw,cursor)
                    if encoding!="UTF8" or not re.fullmatch(r"[A-Za-z0-9_-]+",family):
                        raise ParseError("Unsupported CJK encoding/font")
                    if any(not enc.startswith("utf-8") for enc in self.gatherer.encoding.values()):
                        raise ParseError("CJK UTF8 requires verified UTF-8 source")
                    self.layout_metadata.append(dict(environment=env,encoding=encoding,family=family))
                    text=splice_tokens(text,pos,raw[cursor:],end)
                    continue
                elif env == "math":
                    if env in self.gatherer.local_environments or optional_title is not None or "$" in raw or re.search(r"\\(?:begin|end)\b", raw):
                        raise ParseError("Unsupported/local inline math environment")
                    buffer.append("$" + raw.strip() + "$")
                    front_matter = False
                elif env == "adjustwidth" and "changepage" in self.gatherer.packages:
                    if env in self.gatherer.local_environments or optional_title is not None:
                        raise ParseError("Unsupported adjustwidth options/local definition")
                    left, cursor = group(raw, 0)
                    right, cursor = group(raw, cursor)
                    if not all(literal_layout_glue(x) for x in (left, right)):
                        raise ParseError("Unsupported dynamic adjustwidth margins")
                    self.layout_metadata.append(dict(environment=env, left=left, right=right))
                    text = splice_tokens(text, pos, raw[cursor:] + "\n\n", end)
                    continue
                elif env == "displayquote" and "csquotes" in self.gatherer.packages:
                    if env in self.gatherer.local_environments or optional_title is not None:
                        raise ParseError("Unsupported displayquote attribution/options")
                    quote = self.add("component", "quotation", current())
                    self.parse(raw, quote, depth + 1)
                elif env == "framed" and "framed" in self.gatherer.packages:
                    if env in self.gatherer.local_environments or optional_title is not None or self.gatherer.macros.keys() & {"FrameCommand", "MakeFramed", "FrameRestore"}:
                        raise ParseError("Unsupported local framed setup")
                    container = self.add("component", "framed", current())
                    self.parse(raw, container, depth + 1)
                elif env in {"compactitem", "compactenum"} and "paralist" in self.gatherer.packages:
                    if env in self.gatherer.local_environments or optional_title is not None:
                        raise ParseError("Unsupported compact list options/local definition")
                    container = self.add("itemize" if env == "compactitem" else "enumerate", env, current())
                    self.items(raw, container, depth + 1)
                elif env == "tabularx" and self.gatherer.packages & {"tabularx", "ltablex", "xltabular"}:
                    if env in self.gatherer.local_environments:
                        raise ParseError("Locally redefined tabularx")
                    # Giữ kích thước, column spec và mọi cell như đường tabular hiện có.
                    self.add("figure", raw, current())
                elif env in {"IEEEeqnarray", "IEEEeqnarray*"} and (
                        self.gatherer.document_class in {"IEEEtran", "IEEEtai", "ieeecolor"}
                        or "IEEEtrantools" in self.gatherer.packages):
                    if env in self.gatherer.local_environments or optional_title is not None:
                        raise ParseError("Unsupported IEEEeqnarray options/local definition")
                    columns, cursor = group(raw, 0)
                    if columns not in {"rCl", "rcl"}:
                        raise ParseError("Unsupported IEEEeqnarray column specification")
                    self.layout_metadata.append(dict(environment=env, columns=columns))
                    self.add("block_formula", r"\begin{equation}\begin{aligned}" + raw[cursor:].strip() + r"\end{aligned}\end{equation}", current())
                elif env == "mdframed" and "mdframed" in self.gatherer.packages:
                    if env in self.gatherer.local_environments or optional_title is not None or not self.gatherer.mdframed_default_setup:
                        raise ParseError("Unsupported mdframed setup/options/local definition")
                    container=self.add("component",env,current())
                    self.parse(raw,container,depth+1)
                elif env == "enumerate*" and "enumitem" in self.gatherer.packages and self.gatherer.enumitem_inline:
                    if env in self.gatherer.local_environments:
                        raise ParseError("Locally redefined inline enumerate")
                    if optional_title is not None and not re.fullmatch(r"\s*label\s*=\s*\(\\(?:roman|alph|arabic)\*\)\s*",optional_title):
                        raise ParseError("Unsupported inline enumeration options")
                    container=self.add("enumerate","enumerate*",current())
                    self.layout_metadata.append(dict(environment=env,options=optional_title))
                    self.items(raw,container,depth+1)
                elif env == "@twocolumnfalse" and self.twocolumn_depth:
                    if env in self.gatherer.local_environments or optional_title is not None:
                        raise ParseError("Unsupported local column title wrapper")
                    self.parse(raw,current(),depth+1)
                elif env == "quoting" and "quoting" in self.gatherer.packages:
                    if env in self.gatherer.local_environments or optional_title is not None:
                        raise ParseError("Unsupported/local quoting environment semantics")
                    container = self.add("component", env, current())
                    self.parse(raw, container, depth + 1)
                elif env == "highlights" and self.gatherer.document_class in {"elsarticle", "cas-sc", "cas-dc"}:
                    if env in self.gatherer.local_environments or optional_title is not None:
                        raise ParseError("Unsupported/local highlights semantics")
                    container = self.add("component", env, current())
                    self.items(raw, container, depth + 1)
                elif env == "graphicalabstract" and self.gatherer.document_class == "elsarticle":
                    if env in self.gatherer.local_environments or optional_title is not None:
                        raise ParseError("Unsupported/local graphical abstract semantics")
                    container = self.add("component", env, current())
                    self.parse(raw, container, depth + 1)  # Keep text and figure nodes.
                elif env in {"sidewaystable", "sidewaystable*"} and ("rotating" in self.gatherer.packages or self.gatherer.document_class == "sn-jnl"):
                    if env in self.gatherer.local_environments:
                        raise ParseError("Locally redefined sideways table")
                    if optional_title is not None and not re.fullmatch(r"[!htbpH ]*", optional_title):
                        raise ParseError("Unsupported sideways table placement")
                    self.layout_metadata.append(dict(environment=env, placement=optional_title))
                    self.add("figure", raw, current())  # Same raw table semantics as table.
                elif env == "tcolorbox" and "tcolorbox" in self.gatherer.packages:
                    # Only text title and explicit presentation keys. Hooks,
                    # overlays and arbitrary key execution remain unsupported.
                    if env in self.gatherer.local_environments:
                        raise ParseError("Locally redefined tcolorbox requires environment semantics")
                    title = None
                    style_options_seen = 0
                    def resolve_box(raw_options, chain=()):
                        nonlocal style_options_seen
                        if len(chain) > 12:
                            raise ParseError("tcolorbox style recursion limit")
                        for key,value in box_options(raw_options):
                            style_options_seen += 1
                            if style_options_seen > 1024:
                                raise ParseError("tcolorbox style expansion limit")
                            if value is None and key in self.gatherer.box_styles:
                                if key in chain:
                                    raise ParseError("tcolorbox style cycle: " + key)
                                yield from resolve_box(self.gatherer.box_styles[key], (*chain,key))
                            else:
                                yield key,value
                    global_options = self.gatherer.box_styles.get("@global", [])
                    for key,value in [*global_options, *resolve_box(optional_title or "")]:
                        if key == "title" and value is not None:
                            if title is not None:
                                raise ParseError("Duplicate tcolorbox title")
                            title = value
                        elif key in {"enhanced", "breakable", "center", "sharp corners"} and value is None:
                            pass
                        elif key in {"fonttitle", "fontupper", "fontlower"} and value is not None:
                            if self.gatherer.macros.keys() & set(re.findall(r"\\([A-Za-z]+)",value)):
                                raise ParseError("Locally redefined tcolorbox font: " + key)
                            cursor = 0
                            while (cursor := skip_space(value, cursor)) < len(value):
                                if value[cursor] != "\\":
                                    raise ParseError("Unsupported tcolorbox font: " + key)
                                token, cursor = command(value, cursor)
                                if token == "color" and self.gatherer.packages & {"color", "xcolor"}:
                                    color, cursor = group(value, cursor)
                                    if not re.fullmatch(r"[A-Za-z0-9!._:-]+", color):
                                        raise ParseError("Unsupported dynamic tcolorbox font color")
                                elif token not in {"bfseries", "ttfamily", "itshape", "small", "footnotesize", "scriptsize", "normalfont"}:
                                    raise ParseError("Unsupported tcolorbox font: " + key)
                        elif key == "width" and value is not None:
                            if self.gatherer.macros.keys() & set(re.findall(r"\\([A-Za-z]+)",value)):
                                raise ParseError("Locally redefined tcolorbox width")
                            if not re.fullmatch(r"[0-9.]*\\(?:textwidth|linewidth|columnwidth)|[0-9.]+(?:pt|cm|mm|em|ex)",value):
                                raise ParseError("Unsupported tcolorbox width")
                        elif key == "boxsep" and value is not None and re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:pt|cm|mm|em|ex|in|bp|pc|sp)", value):
                            pass  # Literal interior padding, with no content hook.
                        elif key in {"colback", "colframe", "coltitle", "colbacktitle", "boxrule", "arc", "left", "right", "top", "bottom", "rounded corners"} and value is not None and re.fullmatch(r"[A-Za-z0-9!._ -]+",value):
                            pass
                        else:
                            raise ParseError("Unsupported tcolorbox key: " + key)
                    self.layout_metadata.append(dict(environment=env, options=optional_title))
                    container = self.add("component", env, current())
                    if title is not None:
                        heading = self.add("component", "box title", container)
                        self.parse(title, heading, depth + 1)
                    self.parse(raw, container, depth + 1)
                elif env == "AMS" and self.gatherer.document_class in {"siamltex", "siamart1116"}:
                    if optional_title is not None or env in self.gatherer.local_environments:
                        raise ParseError("Unsupported/local AMS classification environment")
                    container = self.add("component", "AMS subject classification", current())
                    self.parse(raw, container, depth + 1)
                elif env in {"IEEEbiography", "IEEEbiographynophoto"} and self.gatherer.document_class == "IEEEtran":
                    if env in self.gatherer.local_environments or env == "IEEEbiographynophoto" and optional_title is not None:
                        raise ParseError("Unsupported/local IEEE biography semantics")
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
                elif env == "restatable":
                    if env in self.gatherer.local_environments or "thm-restate" not in self.gatherer.packages:
                        raise ParseError("Unverified restatable package/environment declaration")
                    theorem, cursor = group(raw, 0)
                    replay, cursor = group(raw, cursor)
                    if theorem not in self.gatherer.theorems:
                        raise ParseError("Unregistered restatable theorem: " + theorem)
                    reserved = set(LEVELS) | DROP | DROP_ARGUMENT | KEEP_COMMANDS | FORMATTING | {
                        "begin", "end", "documentclass", "input", "include", "cite", "newcommand", "bibliography"}
                    if (not re.fullmatch(r"[A-Za-z]{1,64}", replay) or replay in reserved
                            or replay in self.gatherer.macros or replay in self.restatements):
                        raise ParseError("Unsafe/duplicate restatable replay name: " + replay)
                    statement = raw[cursor:]
                    self.restatements[replay] = (theorem, optional_title, statement)
                    container = self.add("component", theorem, current())
                    self.layout_metadata.append(dict(environment=env, theorem=theorem, replay=replay,
                                                     heading=optional_title, body=statement))
                    if optional_title:
                        self.parse(optional_title, container, depth + 1)
                    self.parse(statement, container, depth + 1)
                elif env == "teaserfigure" and self.gatherer.document_class == "acmart":
                    if env in self.gatherer.local_environments or optional_title is not None:
                        raise ParseError("Unsupported/local ACM teaserfigure semantics")
                    self.add("figure", raw, current())
                elif env == "acknowledgement" and self.gatherer.document_class == "aa":
                    if env in self.gatherer.local_environments or optional_title is not None:
                        raise ParseError("Unsupported/local A&A acknowledgement semantics")
                    if depth == 0:
                        headings.clear()
                    container = self.add("component", env, current())
                    self.parse(raw, container, depth + 1)
                elif env in {"small", "footnotesize", "scriptsize", "normalsize", "titlepage", "NoHyper"}:
                    if optional_title is not None:
                        raise ParseError("Unexpected option on layout environment: " + env)
                    self.parse(raw, current(), depth + 1)
                elif env in {"spacing", "multicols", "multicols*", "strip"}:
                    required = "setspace" if env == "spacing" else "cuted" if env == "strip" else "multicol"
                    if required not in self.gatherer.packages or env in self.gatherer.local_environments:
                        raise ParseError("Unverified/local layout environment: " + env)
                    cursor, values = 0, []
                    if env == "spacing":
                        if optional_title is not None:
                            raise ParseError("Unsupported spacing optional argument")
                        factor, cursor = group(raw, cursor)
                        if not re.fullmatch(r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)", factor.strip()) or not 0 < float(factor) <= 10:
                            raise ParseError("Unsupported nonliteral spacing factor")
                        values.append(factor)
                    elif env.startswith("multicols"):
                        if optional_title is not None:
                            raise ParseError("Column count must precede multicols optional heading")
                        count, cursor = group(raw, cursor)
                        if not count.strip().isdigit() or not 2 <= int(count) <= 20:
                            raise ParseError("Unsupported nonliteral multicols count")
                        values.append(count)
                        cursor = skip_space(raw, cursor)
                        if raw[cursor:cursor + 1] == "[":
                            heading, cursor = group(raw, cursor, "[", "]")
                            self.parse(heading, current(), depth + 1)
                            cursor = skip_space(raw, cursor)
                            if raw[cursor:cursor + 1] == "[":
                                prespace, cursor = group(raw, cursor, "[", "]")
                                if not re.fullmatch(r"[+-]?[0-9.]+(?:pt|em|ex|mm|cm|in)", prespace.strip()):
                                    raise ParseError("Unsupported multicols prespace")
                                values.append(prespace)
                    elif optional_title is not None:
                        raise ParseError("Unsupported strip optional argument")
                    self.layout_metadata.append(dict(environment=env, arguments=values))
                    self.parse(raw[cursor:], current(), depth + 1)
                elif env in {"flalign", "flalign*"}:
                    if (not self.gatherer.packages & {"amsmath", "mathtools"}
                            or env in self.gatherer.local_environments or optional_title is not None):
                        raise ParseError("Unverified/local flalign environment")
                    self.add("block_formula", r"\begin{equation}\begin{aligned}" + raw.strip()
                             + r"\end{aligned}\end{equation}", current())
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
                elif env in {"wrapfigure", "wraptable"} and "wrapfig" in self.gatherer.packages:
                    # Both wrapfig floats share placement, optional overhang and
                    # width arguments; retain the full float source as one leaf.
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
                elif env == "frontmatter" and self.gatherer.document_class in {"autart", "ecai", "imsart", "ifacconf", "wiley-article"}:
                    if optional_title is not None or env in self.gatherer.local_environments:
                        raise ParseError("Unsupported/local frontmatter environment semantics")
                    container = self.add("component", "frontmatter", current())
                    self.frontmatter(raw, container, depth + 1)
                elif (env == "ack" and (self.gatherer.document_class in {"ecai", "ifacconf"}
                          or any(re.fullmatch(r"(?:neurips|nips)_\d{4}", p) for p in self.gatherer.packages))
                      or env == "acknowledgement" and self.gatherer.document_class in {"achemso", "cup-journal"}):
                    if optional_title is not None or env in self.gatherer.local_environments:
                        raise ParseError("Unsupported/local acknowledgment environment")
                    container = self.add("component", "acknowledgments", current())
                    self.parse(raw, container, depth + 1)
                elif (env == "suppinfo" and self.gatherer.document_class == "achemso"
                      or env == "Practitionernotes" and self.gatherer.document_class == "wiley-article"):
                    if optional_title is not None or env in self.gatherer.local_environments:
                        raise ParseError("Unsupported/local template content environment: " + env)
                    container = self.add("component", env, current())
                    self.parse(raw, container, depth + 1)
                elif env == "frontmatter" and self.gatherer.document_class == "elsarticle":
                    self.parse(raw, current(), depth + 1)
                elif (env in {"keyword", "keywords"} and (
                      self.gatherer.document_class in {"elsarticle", "IEEEtran", "ceurart", "mnras", "cas-dc", "cas-sc", "interact", "l4dc2025", "autart", "imsart", "siamltex", "siamart1116", "JFM-FLM_Au", "ifacconf"}
                      or env == "keywords" and "spconf" in self.gatherer.packages)):
                    if optional_title is not None or env in self.gatherer.local_environments:
                        raise ParseError("Unsupported/local keyword environment semantics: " + env)
                    container = self.add("component", "keywords", current())
                    self.keyword_depth += 1
                    try:
                        self.parse(raw, container, depth + 1)
                    finally:
                        self.keyword_depth -= 1
                elif (env in {"algorithm", "algorithm*"} or env == "algorithm2e" and self.gatherer.algorithm2e_alias) and "algorithm2e" in self.gatherer.packages and not re.search(r"\\begin\s*\{algorithmic\}", raw):
                    container = self.add("component", env, current())
                    saved = (self.algorithm_keywords.copy(), self.algorithm_functions.copy(), self.algorithm_programs.copy())
                    self.algorithm2e_depth += 1
                    try:
                        self.parse(raw, container, depth + 1)
                    finally:
                        self.algorithm2e_depth -= 1
                        self.algorithm_keywords, self.algorithm_functions, self.algorithm_programs = saved
                elif env == "sloppypar":
                    if env in self.gatherer.local_environments:
                        raise ParseError("Locally redefined sloppypar requires environment semantics")
                    # Standard paragraph line-breaking tolerance, not a
                    # semantic component. Keep all text under its real parent.
                    self.parse(raw, current(), depth + 1)
                elif env == "IEEEkeywords" and self.gatherer.document_class in {"IEEEtran", "ieeetran", "IEEEtai", "ieeecolor"}:
                    if optional_title is not None:
                        raise ParseError("Unsupported optional argument on " + env)
                    if env in self.gatherer.local_environments:
                        raise ParseError("Locally redefined IEEEkeywords requires environment semantics")
                    container = self.add("component", "keywords", current())
                    self.parse(raw, container, depth + 1)
                elif env == "subequations" and (self.gatherer.packages & {"amsmath", "mathtools"}
                        or self.gatherer.document_class in {"acmart", "amsart", "amsproc", "amsbook"}
                        or (self.gatherer.document_class or "").startswith("revtex")):
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
                elif env == "empheq" and "empheq" in self.gatherer.packages:
                    if env in self.gatherer.local_environments:
                        raise ParseError("Locally redefined empheq")
                    equation_env, cursor = group(raw, 0)
                    if equation_env not in {"equation", "equation*", "align", "align*", "gather", "gather*"}:
                        raise ParseError("Unsupported empheq equation environment")
                    decoration = (optional_title or "").strip()
                    if decoration not in {"", r"box=\fbox", r"left=\empheqlbrace"}:
                        raise ParseError("Unsupported empheq decoration")
                    if (decoration == r"box=\fbox" and "fbox" in self.gatherer.macros
                            or decoration == r"left=\empheqlbrace" and "empheqlbrace" in self.gatherer.macros):
                        raise ParseError("Locally redefined empheq decoration")
                    formula = raw[cursor:].strip()
                    aligned = equation_env.rstrip("*") in {"align", "gather"}
                    if aligned:
                        wrapper = "aligned" if equation_env.startswith("align") else "gathered"
                        formula = r"\begin{" + wrapper + "}" + formula + r"\end{" + wrapper + "}"
                    if decoration == r"left=\empheqlbrace":
                        formula = r"\left\{" + formula + r"\right."
                    self.layout_metadata.append(dict(environment=env, equation_environment=equation_env, decoration=decoration))
                    self.add("block_formula", r"\begin{equation}" + formula + r"\end{equation}", current())
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
                elif env == "credits" and self.gatherer.document_class == "llncs":
                    if env in self.gatherer.local_environments:
                        raise ParseError("Locally redefined LLNCS credits requires environment semantics")
                    if optional_title is not None:
                        raise ParseError("Unsupported LLNCS credits option")
                    # Labels are interpreted at their command/heading position,
                    # never by substituting over literal code or the whole body.
                    self.layout_metadata.append(dict(environment=env))
                    self.credits_depth += 1
                    try:
                        self.parse(raw, current(), depth + 1)
                    finally:
                        self.credits_depth -= 1
                elif env == "algorithm*" and self.gatherer.packages & {"algorithm", "algorithm2e"}:
                    if env in self.gatherer.local_environments:
                        raise ParseError("Locally redefined algorithm* requires environment semantics")
                    container = self.add("component", env, current())
                    self.parse(raw, container, depth + 1)
                elif (env == "nolinenumbers" and env not in self.gatherer.local_environments
                      and (self.gatherer.packages & {"lineno", "linenoaa"}
                           or (self.gatherer.document_class or "").startswith("aastex"))):
                    # The scoped environment changes line numbering only.
                    # Reparse all content, including abstract/math/citations.
                    self.layout_metadata.append(dict(environment=env))
                    self.parse(raw, current(), depth + 1)
                elif (env == "landscape" and self.gatherer.packages & {"lscape", "pdflscape"}
                      and env not in self.gatherer.local_environments):
                    self.layout_metadata.append(dict(environment=env))
                    self.parse(raw, current(), depth + 1)
                elif (env == "IEEEproof" and self.gatherer.document_class in {"IEEEtran", "IEEEtai", "ieeecolor"}
                      and env not in self.gatherer.local_environments):
                    container = self.add("component", "proof", current())
                    if optional_title is not None:
                        self.parse(optional_title, container, depth + 1)
                    self.parse(raw, container, depth + 1)
                elif (env == "subequations" and self.gatherer.packages & {"amsmath", "mathtools"}
                      and env not in self.gatherer.local_environments):
                    if optional_title is not None:
                        raise ParseError("Unsupported subequations options")
                    container = self.add("component", env, current())
                    self.parse(raw, container, depth + 1)
                elif env in {"defn", "thm", "lem", "pf", "rem"} and self.gatherer.document_class == "ifacconf":
                    if env in self.gatherer.local_environments:
                        raise ParseError("Locally redefined IFAC theorem/proof environment")
                    container = self.add("component", {"defn":"definition", "thm":"theorem", "lem":"lemma", "pf":"proof", "rem":"remark"}[env], current())
                    if optional_title is not None:
                        self.parse(optional_title, container, depth + 1)
                    self.parse(raw, container, depth + 1)
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
            elif name == "textdegree" and name not in self.gatherer.macros:
                # Kernel textcomp symbol: emit the actual degree sign.
                buffer.append("\u00b0")
            elif name == "ce" and name not in self.gatherer.macros and "mhchem" in self.gatherer.packages:
                # mhchem notation is retained verbatim; the offline parser does
                # not interpret chemical formulas.
                start = skip_space(text, end)
                value, end = group(text, start)
                buffer.append(r"\ce{" + value + "}")
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
            elif name == "sep" and (self.gatherer.document_class in {"elsarticle", "ceurart", "cas-dc", "cas-sc"} or self.gatherer.document_class == "ifacconf" and self.keyword_depth) and name not in self.gatherer.macros:
                buffer.append("; ")
            elif name == "@" and self.gatherer.document_class == "ceurart" and name not in self.gatherer.macros:
                pass  # TeX sentence-spacing declaration, not an at-sign.
            elif name == "num" and "siunitx" in self.gatherer.packages and name not in self.gatherer.macros:
                start = options(text, end)
                value, end = group(text, start)
                if not re.fullmatch(r"[+\-0-9.eE]+", value.strip()):
                    raise ParseError("Unsupported nonliteral siunitx number")
                buffer.append(text[pos:end])  # Preserve formatting rather than guess a locale.
            elif name in {"SI", "qty"} and "siunitx" in self.gatherer.packages and name not in self.gatherer.macros and (name != "qty" or "physics" not in self.gatherer.packages):
                # As with num, retain the source quantity rather than guess
                # locale, unit spelling or package rendering options. Consume
                # its balanced syntax as one token so unit macros are not
                # mistaken for independent prose commands.
                start = options(text, end)
                if name == "qty" and text[skip_space(text, end):start].strip() not in {"", "[mode=text]", "[mode=math]"}:
                    raise ParseError("Unsupported siunitx qty options")
                value, start = group(text, start)
                if name == "SI":
                    start = options(text, start)  # optional pre-unit (siunitx v2)
                unit, end = group(text, start)
                if not re.fullmatch(r"[+\-0-9.eE()]+", value.strip()) or not re.search(r"[0-9]", value):
                    raise ParseError("Unsupported nonliteral siunitx quantity")

                if any(token not in SI_UNIT_COMMANDS for token in re.findall(r"\\([A-Za-z@]+|.)", unit)):
                    raise ParseError("Unsupported siunitx unit; preserve source for interpretation: " + unit)
                if name == "qty" and self.gatherer.macros.keys() & set(re.findall(r"\\([A-Za-z@]+)", unit)):
                    raise ParseError("Locally redefined siunitx quantity unit")
                buffer.append(text[pos:end])
                self.layout_metadata.append(dict(command=name, number=value, unit=unit, normalization="source_quantity_retained"))
            elif name == "si" and "siunitx" in self.gatherer.packages and name not in self.gatherer.macros:
                start = options(text, end)
                unit, end = group(text, start)
                if any(token not in SI_UNIT_COMMANDS for token in re.findall(r"\\([A-Za-z@]+|.)", unit)):
                    raise ParseError("Unsupported siunitx unit; preserve source for interpretation: " + unit)
                buffer.append(text[pos:end])
                self.layout_metadata.append(dict(command=name, unit=unit, normalization="source_unit_retained"))
            elif self.algorithm2e_depth and name in {"SetKwFunction", "SetKwProg"} and name not in self.gatherer.macros:
                declaration, end = algorithm_declaration(text, end, name)
                target = declaration["target"]
                if target in self.gatherer.macros:
                    raise ParseError("Local macro conflicts with algorithm declaration")
                if name == "SetKwFunction":
                    self.algorithm_functions[target] = declaration["label"]
                else:
                    self.algorithm_programs[target] = declaration
                self.layout_metadata.append(declaration)
            elif self.algorithm2e_depth and name in self.algorithm_functions and name not in self.gatherer.macros:
                raw, end = group(text, end)
                text = splice_tokens(text, pos, self.algorithm_functions[name] + "(" + raw + ")", end)
                continue
            elif self.algorithm2e_depth and name in self.algorithm_programs and name not in self.gatherer.macros:
                declaration = self.algorithm_programs[name]
                header, start = group(text, end)
                body, end = group(text, start)
                flush()
                program = self.add("component", declaration["label"], current())
                self.parse_algorithm_argument(header + declaration["separator"], program, depth + 1)
                self.parse_algorithm_argument(body, program, depth + 1)
                if declaration["ending"]:
                    self.add("sentence", declaration["ending"], program)
            elif self.algorithm2e_depth and name in {"KwIn", "KwOut", "KwResult", "KwRet", "KwData"} and name not in self.gatherer.macros and name not in self.algorithm_keywords:
                raw, end = group(text, end)
                flush()
                container = self.add("component", {"KwIn":"input", "KwOut":"output", "KwResult":"result", "KwRet":"return", "KwData":"data"}[name], current())
                self.parse_algorithm_argument(raw, container, depth + 1)
            elif self.algorithm2e_depth and name in {"KwTo", "Return"} and name not in self.gatherer.macros:
                buffer.append("to " if name == "KwTo" else "return ")
                end = skip_space(text, end)
            elif self.algorithm2e_depth and name in {"tcp", "tcc"} and name not in self.gatherer.macros:
                start = skip_space(text, end)
                star = text[start:start + 1] == "*"
                start += int(star)
                start = skip_space(text, start)
                alignment = None
                if text[start:start + 1] == "[":
                    if not star:
                        raise ParseError("Algorithm comment alignment requires starred form")
                    alignment, start = group(text, start, "[", "]")
                    if alignment not in {"h", "f", "r", "l"}:
                        raise ParseError("Unsupported algorithm comment alignment")
                raw, end = group(text, start)
                flush()
                container = self.add("component", "comment", current())
                self.parse_algorithm_argument(raw, container, depth + 1)
                self.layout_metadata.append(dict(command=name, starred=star, alignment=alignment))
            elif self.algorithm2e_depth and name == "eIf" and name not in self.gatherer.macros:
                condition,start=group(text,end)
                yes,start=group(text,start)
                no,end=group(text,start)
                flush()
                block=self.add("component","if",current())
                self.parse_algorithm_argument(condition,block,depth+1)
                then=self.add("component","then",block)
                self.parse_algorithm_argument(yes,then,depth+1)
                otherwise=self.add("component","else",block)
                self.parse_algorithm_argument(no,otherwise,depth+1)
            elif self.algorithm2e_depth and name == "Repeat" and name not in self.gatherer.macros:
                condition, start = group(text, end)
                body, end = group(text, start)
                flush()
                container = self.add("component", "repeat", current())
                self.parse_algorithm_argument(body, container, depth + 1)
                until = self.add("component", "until", container)
                self.parse_algorithm_argument(condition, until, depth + 1)
            elif self.algorithm2e_depth and name in {"For", "ForEach", "While", "If", "lIf", "ElseIf", "Else"} and name not in self.gatherer.macros:
                condition = None
                start = skip_space(text, end)
                attached = None
                if text[start:start + 1] == "(":
                    opening = start
                    cursor = skip_space(text, start + 1)
                    comment, cursor = command(text, cursor)
                    if comment not in {"tcp", "tcc"} or comment in self.gatherer.macros:
                        raise ParseError("Unsupported algorithm side comment")
                    cursor += int(text[cursor:cursor + 1] == "*")
                    cursor = options(text, cursor)
                    _, cursor = group(text, cursor)
                    closing = skip_space(text, cursor)
                    if text[closing:closing + 1] != ")":
                        raise ParseError("Unsupported algorithm side comment boundary")
                    attached, start = text[opening + 1:closing], closing + 1
                if name != "Else":
                    condition, start = group(text, start)
                raw, end = group(text, start)
                flush()
                container = self.add("component", name.lower(), current())
                if attached is not None:
                    self.parse_algorithm_argument(attached, container, depth + 1)
                if condition is not None:
                    self.parse_algorithm_argument(condition, container, depth + 1)
                self.parse_algorithm_argument(raw, container, depth + 1)
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
            elif name == "allowdisplaybreaks" and name not in self.gatherer.macros and (
                    self.gatherer.packages & {"amsmath", "mathtools"}
                    or self.gatherer.document_class in {"amsart", "amsproc", "amsbook"}):
                start = skip_space(text, end)
                level = None
                if text[start:start + 1] == "[":
                    level, end = group(text, start, "[", "]")
                    if level not in {"0", "1", "2", "3", "4"}:
                        raise ParseError("Invalid allowdisplaybreaks level")
                self.layout_metadata.append(dict(command=name, level=level))
            elif name in {"phantom", "hphantom", "vphantom"} and name not in self.gatherer.macros:
                raw, end = group(text, end)
                # Literal characters size an invisible box. Unknown commands
                # could have side effects (including citations), so refuse them.
                if re.search(r"[\\#${}]", raw):
                    raise ParseError("Unsupported nonliteral phantom body")
                self.layout_metadata.append(dict(command=name, hidden_body=raw))
            elif name == "hypersetup" and name not in self.gatherer.macros and "hyperref" in self.gatherer.packages:
                raw, end = group(text, end)
                for key, value in box_options(raw):
                    if key in {"colorlinks", "hidelinks", "bookmarks", "bookmarksopen", "bookmarksnumbered", "breaklinks"}:
                        if value is not None and value not in {"true", "false"}:
                            raise ParseError("Unsupported hyperref flag: " + key)
                    elif key in {"linkcolor", "citecolor", "urlcolor", "filecolor", "anchorcolor", "allcolors"}:
                        if value is None or not re.fullmatch(r"[A-Za-z0-9!._-]+", value):
                            raise ParseError("Unsupported hyperref color: " + key)
                    else:
                        raise ParseError("Unsupported hyperref setup key: " + key)
                self.layout_metadata.append(dict(command=name, options=raw))
            elif (name == "ion" and name not in self.gatherer.macros
                  and (self.gatherer.document_class == "aa" or (self.gatherer.document_class or "").startswith("aastex"))):
                atom, start = group(text, end)
                stage, end = group(text, start)
                if (not re.fullmatch(r"[A-Z][a-z]?", atom.strip())
                        or not re.fullmatch(r"[1-9][0-9]?|[ivxIVX]+", stage.strip())):
                    raise ParseError("Unsupported nonliteral ion notation")
                buffer.append(text[pos:end])  # Preserve stage, never infer an ionization value.
            elif name == "parbox" and name not in self.gatherer.macros:
                start, placement = skip_space(text, end), []
                for _ in range(3):
                    if text[start:start + 1] != "[":
                        break
                    option, start = group(text, start, "[", "]")
                    placement.append(option)
                    start = skip_space(text, start)
                if (placement and placement[0].strip() not in {"t", "c", "b"}
                        or len(placement) == 3 and placement[2].strip() not in {"t", "c", "b", "s"}):
                    raise ParseError("Unsupported parbox alignment")
                width, start = group(text, start)
                for value in [width, *placement[1:2]]:
                    if (not re.fullmatch(r"[+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)\s*(?:pt|em|ex|mm|cm|in|bp|pc|sp|\\(?:textwidth|linewidth|columnwidth))", value.strip())
                            and not re.fullmatch(r"\\(?:textwidth|linewidth|columnwidth)", value.strip())):
                        raise ParseError("Unsupported nonliteral parbox dimension: " + value)
                    if self.gatherer.macros.keys() & set(re.findall(r"\\([A-Za-z]+)", value)):
                        raise ParseError("Locally redefined parbox dimension")
                raw, end = group(text, start)
                self.layout_metadata.append(dict(command=name, width=width, placement=placement))
                flush()
                self.parse(raw, current(), depth + 1)
            elif name == "textcircled" and name not in self.gatherer.macros:
                value, end = group(text, end)
                # Preserve the circle, not just the letter/digit it encloses.
                # Only literal glyphs or their literal raisebox form are proven.
                if not (re.fullmatch(r"[A-Za-z0-9]", value.strip()) or re.fullmatch(
                        r"\\raisebox\s*\{[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:pt|em|ex)\}\s*\{[A-Za-z0-9]\}", value.strip())
                        and "raisebox" not in self.gatherer.macros):
                    raise ParseError("Unsupported nonliteral circled text")
                buffer.append(text[pos:end])
            elif (name == "text" and name not in self.gatherer.macros
                  and (self.gatherer.packages & {"amsmath", "amstext", "mathtools"}
                       or self.gatherer.document_class in {"amsart", "amsproc", "amsbook", "acmart"})):
                raw, end = group(text, end)
                text = splice_tokens(text, pos, raw, end)
                continue
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
            elif self.pseudocode_depth and name in {"Comment", "COMMENT"} and name not in self.gatherer.macros:
                raw, end = group(text, end)
                flush()
                container = self.add("component", "comment", current())
                self.parse(raw, container, depth + 1)
            elif name == "par":
                flush()
            elif (name == "captionof" and name not in self.gatherer.macros
                  and (self.gatherer.packages & {"caption", "capt-of"} or self.gatherer.document_class == "acmart")):
                kind, start = group(text, end)
                if kind not in {"figure", "table"}:
                    raise ParseError("Unsupported captionof type: " + kind)
                start = options(text, start)
                value, end = group(text, start)
                flush()
                if not reference_section:
                    container = self.add("component", kind + " caption", current())
                    self.parse(value, container, depth + 1)
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
                  and self.gatherer.document_class in {"IEEEtran", "ieeetran", "IEEEtai", "ieeecolor"}):
                first, start = group(text, end)
                start = skip_space(text, start)
                if text[start:start + 1] == "{":
                    rest, end = group(text, start)
                elif re.fullmatch(r"[A-Za-z0-9]", text[start:start + 1]):
                    # A TeX undelimited argument can be one literal token.
                    # Consume exactly that token, never the rest of the word.
                    rest, end = text[start], start + 1
                else:
                    raise ParseError("Unsupported nonliteral IEEEPARstart continuation")
                if not re.fullmatch(r"[A-Za-z0-9]", first):
                    raise ParseError("Unsupported nonliteral IEEEPARstart initial")
                text = text[:pos] + first + rest + text[end:]
                continue
            elif (name == "IEEEraisesectionheading" and name not in self.gatherer.macros
                  and self.gatherer.document_class == "IEEEtran"):
                raw, end = group(text, end)
                if not re.match(r"\s*\\(?:section|subsection|subsubsection)(?![A-Za-z])", raw):
                    raise ParseError("IEEE raised heading requires an explicit section")
                # Splice into the same parse context so following content uses
                # that section as its parent, not a fresh recursive context.
                text = splice_tokens(text, pos, raw, end)
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
            elif (name == "theoremstyle" and name not in self.gatherer.macros
                  and ("amsthm" in self.gatherer.packages or self.gatherer.document_class in {"amsart","amsproc","amsbook"})):
                style, end = group(text, end)
                if style.strip() not in {"plain","definition","remark"}:
                    raise ParseError("Unknown/dynamic theoremstyle: " + style)
                self.layout_metadata.append(dict(command=name, style=style))
            elif (name == "settopmatter" and self.gatherer.document_class == "acmart"
                  and name not in self.gatherer.macros):
                if depth != 0:
                    raise ParseError("Nested ACM settopmatter needs scope semantics")
                settings, end = group(text, end)
                for key, value in listing_options(settings, ordered=True):
                    valid = (key in {"printacmref","printccs","printfolios"} and value in {"true","false"}
                             or key == "authorsperrow" and value is not None and re.fullmatch(r"[1-9][0-9]?",value))
                    if not valid:
                        raise ParseError("Unsupported ACM topmatter setting: " + key)
                self.layout_metadata.append(dict(command=name, options=settings))
            elif (name in {"IEEEpubid","IEEEpubidadjcol","IEEEdisplaynontitleabstractindextext"}
                  and self.gatherer.document_class == "IEEEtran" and name not in self.gatherer.macros):
                if name == "IEEEpubid":
                    raw, end = group(text, end)
                    if "copyright" in self.gatherer.macros:
                        raise ParseError("Local IEEE publication notice glyph")
                    flush()
                    notice = self.add("component","publication notice",current())
                    # Replace the exact kernel glyph token only, preserving
                    # longer commands, escaped backslashes and literal text.
                    probe = mask_root_inline_verbatim(mask_literals(raw))
                    edits, cursor = [], 0
                    while cursor < len(probe):
                        if probe[cursor] != "\\":
                            cursor += 1
                            continue
                        token, stop = command(probe, cursor)
                        if token == "copyright":
                            edits.append((cursor, stop))
                        cursor = stop
                    display = raw
                    for start, stop in reversed(edits):
                        display = display[:start] + "\u00a9" + display[stop:]
                    self.parse(display,notice,depth+1)
                    self.layout_metadata.append(dict(command=name, arguments=[raw]))
                elif name == "IEEEdisplaynontitleabstractindextext":
                    # The existing IEEEtitleabstractindextext handler emits the
                    # stored abstract once. Moving its display is layout only.
                    if getattr(self, "_ieee_display_seen", False):
                        raise ParseError("Repeated IEEE abstract display needs placement semantics")
                    self._ieee_display_seen = True
                    self.layout_metadata.append(dict(command=name, stored_abstract_emitted_once=True))
                else:
                    self.layout_metadata.append(dict(command=name))
            elif (name == "say" and "dirtytalk" in self.gatherer.packages
                  and name not in self.gatherer.macros):
                raw, end = group(text, end)
                if re.search(r"\\say\b",raw):
                    raise ParseError("Nested dirtytalk quotes need quote-depth semantics")
                text = splice_tokens(text,pos,"\u201c"+raw+"\u201d",end)
                continue
            elif (name in {"DontPrintSemicolon","PrintSemicolon","SetNoFillComment","SetAlgoNoLine"}
                  and self.algorithm2e_depth and "algorithm2e" in self.gatherer.packages
                  and name not in self.gatherer.macros):
                self.layout_metadata.append(dict(command=name))
            elif (name == "SetAlgoLined" and name not in self.gatherer.macros
                  and "algorithm2e" in self.gatherer.packages):
                self.layout_metadata.append(dict(command=name))
            elif name in {"nobreak", "allowbreak", "boldmath", "unboldmath"} and name not in self.gatherer.macros:
                # Break/spacing penalties and math font declarations have no
                # visible characters; surrounding content is retained.
                pass
            elif (name in {"degr", "arcdeg", "arcmin", "arcsec"}
                  and name not in self.gatherer.macros
                  and ((self.gatherer.document_class or "").startswith("aastex")
                       or name == "arcsec" and self.gatherer.document_class == "aa")):
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
            elif (name == "IEEEtitleabstractindextext" and name not in self.gatherer.macros
                  and self.gatherer.document_class == "IEEEtran"):
                # IEEEtran wraps abstract and index terms in one balanced group;
                # parse the inner content instead of discarding the wrapper.
                raw, end = group(text, end)
                self.layout_metadata.append(dict(command=name, arguments=[raw]))
                flush()
                self.parse(raw, current(), depth + 1)
            elif (name in {"correspondingauthor", "revised", "accepted", "published"}
                  and name not in self.gatherer.macros
                  and (self.gatherer.document_class or "").startswith("aastex")):
                value, end = group(text, end)
                self.layout_metadata.append(dict(command=name, arguments=[value]))
            elif (name in {"acknowledgments", "acks"} or name == "acknowledgements" and (self.gatherer.document_class or "").startswith("revtex")) and name not in self.gatherer.macros:
                flush()
                start = skip_space(text, end)
                declaration = (name in {"acknowledgments", "acknowledgements"} and text[start:start + 1] != "{"
                               and ((self.gatherer.document_class or "").startswith("revtex") or ((self.gatherer.document_class or "").startswith(("aastex", "ametsocV6")) or self.gatherer.document_class == "spie" or self.gatherer.packages & {"jheppub", "jcappub"})))
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
            elif (name == "bstctlcite" and self.gatherer.document_class in {"IEEEtran","IEEEtai","ieeecolor"}
                  and name not in self.gatherer.macros):
                keys,end = group(text,end)
                requested = citation_keys(r"\cite{"+keys+"}")
                if set(requested) - getattr(self.gatherer,"bst_control_keys",set()):
                    raise ParseError("IEEE bibliography control entries unavailable: " + keys)
                self.layout_metadata.append(dict(command=name, control_keys=requested))
            elif name == "setlength" and name not in self.gatherer.macros:
                target, start = literal_length_target(text, end)
                value, end = group(text, start)
                known = {r"\parskip", r"\parindent", r"\tabcolsep", r"\columnsep",
                         r"\abovedisplayskip", r"\belowdisplayskip", r"\abovedisplayshortskip", r"\belowdisplayshortskip", r"\textfloatsep", r"\floatsep", r"\dbltextfloatsep",
                         r"\spaceskip", r"\intextsep", r"\topsep", r"\partopsep", r"\abovecaptionskip", r"\belowcaptionskip", r"\headheight", r"\footskip", r"\jot", r"\bibsep", r"\itemsep", r"\leftskip", r"\parsep", r"\baselineskip"}
                known |= {r"\arraycolsep", r"\fboxsep", r"\fboxrule"}
                if "algorithm2e" in self.gatherer.packages:
                    known.add(r"\algomargin")
                if target.strip().removeprefix("\\") in self.layout_lengths:
                    literal = re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)\s*(?:pt|em|ex|mm|cm|in|bp|pc|sp)", value.strip())
                    scaled = re.fullmatch(r"([+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+))\s*\\(textwidth|linewidth|columnwidth)", value.strip())
                    if not literal and not (scaled and scaled[2] not in self.gatherer.macros):
                        raise ParseError("Unsupported declared layout length value: " + value)
                    self.layout_metadata.append(dict(command=name, target=target, value=value))
                    pos = end
                    continue
                if target.strip().lstrip("\\") in self.gatherer.macros:
                    raise ParseError("Locally redefined layout register: " + target)
                scaled_baseline = (target.strip() == r"\baselineskip" and re.fullmatch(
                    r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)\s*\\baselineskip", value.strip()))
                if not scaled_baseline and (target.strip() not in known or not literal_layout_glue(value)):
                    raise ParseError("Unsupported nonliteral layout length assignment: " + target + "=" + value)
                self.layout_metadata.append(dict(command=name, target=target, value=value))
            elif name in {"newline", "indent"} and name not in self.gatherer.macros:
                buffer.append(" ")
            elif (name == "subref" and name not in self.gatherer.macros
                  and self.gatherer.packages & {"subcaption", "subfig", "subfigure"}):
                start = end + int(text[end:end + 1] == "*")
                _, end = group(text, start)
                buffer.append(text[pos:end])
            elif name == "cleardoublepage" and name not in self.gatherer.macros:
                buffer.append(" ")
                self.layout_metadata.append(dict(command=name))
            elif name == "romannumeral" and name not in self.gatherer.macros:
                integer = re.match(r"\s*([+-]?[0-9]+)", text[end:])
                if (not integer or len(integer[1].lstrip("+-")) > 5
                        or not -3999 <= int(integer[1]) <= 3999):
                    raise ParseError("Unsupported Roman expression: bounded literal integer required")
                stop = end + integer.end()
                if text[stop:stop + 1] == "\\":
                    terminator, after = command(text, stop)
                    if terminator != "relax" or terminator in self.gatherer.macros:
                        raise ParseError("Unsupported expandable Roman-number terminator")
                    stop = after
                elif text[stop:stop + 1].isspace():
                    stop += 1  # one integer-terminating space is consumed by TeX
                buffer.append(roman_numeral(int(integer[1])).lower())
                end = stop
            elif name == "scalebox" and name not in self.gatherer.macros and self.gatherer.packages & {"graphicx", "graphics"}:
                horizontal, start = group(text, end)
                vertical = None
                start = skip_space(text, start)
                if text[start:start + 1] == "[":
                    vertical, start = group(text, start, "[", "]")
                for scale in (horizontal, vertical):
                    if scale is not None and (not re.fullmatch(r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)", scale.strip())
                                              or float(scale) <= 0):
                        raise ParseError("Unsupported nonpositive/nonliteral scalebox factor: " + scale)
                raw, end = group(text, start)
                self.layout_metadata.append(dict(command=name, horizontal=horizontal, vertical=vertical))
                text = splice_tokens(text, pos, raw, end)
                continue
            elif name == "newcolumntype" and name not in self.gatherer.macros:
                if not self.gatherer.packages & {"array", "tabularx", "tabulary", "colortbl"}:
                    raise ParseError("Unverified array column-type package")
                target, start = group(text, end)
                if not re.fullmatch(r"[A-Za-z]", target) or target in "lcrpmbX":
                    raise ParseError("Unsupported custom column name: " + target)
                start, argc = skip_space(text, start), 0
                if text[start:start + 1] == "[":
                    count, start = group(text, start, "[", "]")
                    if not re.fullmatch(r"[0-9]", count):
                        raise ParseError("Unsupported custom column argument count")
                    argc = int(count)
                spec, end = group(text, start)
                layout_column_spec(spec, argc, self.gatherer)
                if target in self.column_types and self.column_types[target] != (argc, spec):
                    raise ParseError("Conflicting custom column declaration: " + target)
                self.column_types[target] = (argc, spec)
                self.layout_metadata.append(dict(command=name, target=target, argc=argc, specification=spec,
                                                 interpretation="pure layout; table body retained"))
            elif name == "linebreak" and name not in self.gatherer.macros:
                start = skip_space(text, end)
                strength = None
                if text[start:start + 1] == "[":
                    strength, end = group(text, start, "[", "]")
                    if strength.strip() not in {"0", "1", "2", "3", "4"}:
                        raise ParseError("Unsupported linebreak strength: " + strength)
                buffer.append(" ")
                self.layout_metadata.append(dict(command=name, strength=strength))
            elif name == "textbullet" and name not in self.gatherer.macros:
                buffer.append("\u2022")
                end = skip_space(text, end)
            elif name == "newlength" and name not in self.gatherer.macros:
                start = skip_space(text, end)
                if text[start:start + 1] == "{":
                    target, end = group(text, start)
                    match = re.fullmatch(r"\s*\\([A-Za-z]+)\s*", target)
                    if not match:
                        raise ParseError("Unsupported nonliteral length declaration")
                    target = match[1]
                else:
                    target, end = command(text, start)
                reserved = set(LEVELS) | DROP | DROP_ARGUMENT | KEEP_COMMANDS | FORMATTING | {
                    "begin", "end", "documentclass", "input", "include", "cite", "newlength",
                    "textwidth", "linewidth", "columnwidth", "baselineskip", "parskip", "tabcolsep"}
                if (not re.fullmatch(r"[A-Za-z]+", target) or target in reserved
                        or target in self.gatherer.macros or target in self.layout_lengths):
                    raise ParseError("Unsafe/duplicate length declaration: " + target)
                self.layout_lengths.add(target)
                self.layout_metadata.append(dict(command=name, target=target))
            elif name == "addtolength" and name not in self.gatherer.macros:
                start = skip_space(text, end)
                if text[start:start + 1] == "{":
                    target, start = group(text, start)
                else:
                    target_name, start = command(text, start)
                    target = "\\" + target_name
                value, end = group(text, start)
                known = {r"\linewidth", r"\textwidth", r"\columnwidth", r"\tabcolsep",
                         r"\parskip", r"\parindent", r"\itemsep", r"\parsep", r"\columnsep",
                         r"\textheight", r"\topmargin", r"\baselineskip", r"\textfloatsep", r"\floatsep", r"\dbltextfloatsep", r"\bibsep"}
                if (target.strip().removeprefix("\\") in self.gatherer.macros
                        or target.strip() not in known or not re.fullmatch(
                        r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)\s*(?:pt|em|ex|mm|cm|in|bp|pc|sp)", value.strip())):
                    raise ParseError("Unsupported nonliteral layout length increment: " + target + "=" + value)
                self.layout_metadata.append(dict(command=name, target=target, value=value))
            elif name in {"centerline", "leftline"} and name not in self.gatherer.macros:
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
            elif self.algorithm2e_depth and name in {"SetKwInOut", "SetKwInput"} and name not in self.gatherer.macros:
                target, start = group(text, end)
                label, end = group(text, start)
                if not re.fullmatch(r"[A-Za-z]+", target) or target in self.gatherer.macros or target in LEVELS:
                    raise ParseError("Unsafe algorithm keyword declaration: " + target)
                if name == "SetKwInput" and not (
                        re.fullmatch(r"Kw[A-Z][A-Za-z]*", target) or target in {"Input", "Output", "Data", "Require", "Ensure"}):
                    raise ParseError("Unsupported/colliding algorithm input target")
                self.algorithm_keywords[target] = label
            elif self.algorithm2e_depth and name in self.algorithm_keywords:
                raw, end = group(text, end)
                flush()
                label = self.algorithm_keywords[name]
                canonical = {"KwIn":"input", "KwOut":"output", "KwData":"data", "KwRet":"return", "KwResult":"result"}.get(name)
                if canonical is not None and label.casefold() == canonical:
                    label = canonical
                container = self.add("component", label, current())
                self.parse_algorithm_argument(raw, container, depth + 1)
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
                        self.icml_notice_depth += 1
                        try:
                            self.parse(notice, container, depth + 1)
                        finally:
                            self.icml_notice_depth -= 1
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
            elif (name in VISION_EXAMPLES and self.gatherer.packages & VISION_PACKAGES
                  and not (name == "ie" and "cvpr" in self.gatherer.packages)
                  and name not in self.gatherer.macros):
                value = VISION_EXAMPLES[name]
                if text[end:end + 1] == ".":
                    end += 1  # the package's onedot avoids a duplicated period
                buffer.append(value)
                family = "CVPR/ICCV" if self.gatherer.packages & {"cvpr", "iccv"} else "Vision template"
                self.warnings.append(family + " convention: \\" + name + " -> " + value
                                     + "; original style file not interpreted")
            elif (name in CVPR_ABBREVIATIONS and "cvpr" in self.gatherer.packages
                  and name not in self.gatherer.macros):
                value = CVPR_ABBREVIATIONS[name]
                if text[end:end + 1] == ".":
                    end += 1  # cvpr's onedot avoids a duplicated period.
                buffer.append(value)
                self.warnings.append("CVPR convention: \\" + name + " -> " + value + "; original style file not interpreted")
            elif name in self.restatements and name not in self.gatherer.macros:
                theorem, heading, statement = self.restatements[name]
                end += int(text[end:end + 1] == "*")
                self.expansions += 1
                if self.expansions > 10000:
                    raise ParseError("Restatable replay expansion limit/cycle")
                flush()
                container = self.add("component", theorem, current())
                self.layout_metadata.append(dict(command=name, restatable_replay=True))
                if heading:
                    self.parse(heading, container, depth + 1)
                self.parse(statement, container, depth + 1)
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
            elif name in {"'", '"', chr(96), "^", "~", "=", ".", "c", "v", "u", "H", "r", "d"}:
                start = skip_space(text, end)
                if text[start:start + 1] == "{":
                    value, end = group(text, start)
                elif text[start:start + 1] == "\\":
                    base, end = command(text, start)
                    if base not in {"i", "j"} or base in self.gatherer.macros:
                        raise ParseError("Unsupported control-word text accent")
                    value = "\\" + base
                else:
                    value, end = text[start:start + 1], start + 1
                if value.strip() in {r"\i", r"\j"}:
                    if value.strip()[1:] in self.gatherer.macros:
                        raise ParseError("Locally redefined dotless accent base")
                    value = {r"\i": "i", r"\j": "j"}[value.strip()]
                if value == "" and name == "^":
                    buffer.append(r"\textasciicircum{}")
                    pos = end
                    continue
                if len(value) != 1:
                    raise ParseError("Unsupported compound text accent")
                mark = {"'": "\u0301", '"': "\u0308", chr(96): "\u0300", "^": "\u0302",
                        "~": "\u0303", "=": "\u0304", ".": "\u0307", "c": "\u0327",
                        "v": "\u030c", "u": "\u0306", "H": "\u030b", "r": "\u030a", "d": "\u0323"}[name]
                buffer.append(unicodedata.normalize("NFC", value + mark))
            elif name == "ensuremath":
                value, end = group(text, end)
                buffer.append("$" + value + "$")
            elif name == "citestyle" and self.gatherer.document_class == "acmart":
                # ACM selects citation presentation, not a bibliography entry.
                # Local redefinitions have already passed through the macro arm.
                style, end = group(text, end)
                if style.strip() not in {"acmnumeric", "acmauthoryear"}:
                    raise ParseError("Unsupported ACM citation style: " + style)
                self.layout_metadata.append(dict(command=name, arguments=[style]))
            elif name in KEEP_COMMANDS or _CITATION_RE.fullmatch(name) or name == "nocite":
                is_citation = bool(_CITATION_RE.fullmatch(name))
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
            elif name in {" ", "\t", "\r", ",", ";", ":", "!", "\\", "\n", "/"}:
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
                     "Procedure": "PROCEDURE", "EndProcedure": "ENDPROCEDURE",
                     "RETURN": "RETURN", "Return": "RETURN",
                     "Repeat": "REPEAT", "REPEAT": "REPEAT", "Until": "UNTIL", "UNTIL": "UNTIL"}
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
                elif name in {"Repeat", "REPEAT", "Until", "UNTIL"} and braces == 0 and not math and name in self.gatherer.macros:
                    raise ParseError("Locally redefined algorithmic control: " + name)
                elif name in canonical and braces == 0 and not math and not (name in {"Return", "RETURN"} and name in self.gatherer.macros):
                    positions.append((pos, end, canonical[name]))
                pos = end
            else:
                if text[pos] == "$":
                    math = None if math == "$" else "$"
                if not math:
                    braces += int(text[pos] == "{") - int(text[pos] == "}")
                pos += 1
        prefix = text[:positions[0][0]] if positions else text
        harmless = r"\s*(?:(?:\\label\s*\{[^{}]*\}|\\(?:small|footnotesize|scriptsize|normalsize|medskip|smallskip|bigskip)\b)\s*)*"
        if not positions or not re.fullmatch(harmless,prefix):
            raise ParseError("Unsupported text before first algorithmic statement")
        if prefix.strip():
            self.parse(prefix,parent,depth+1)
        stack, current = [], parent
        self.pseudocode_depth += 1
        try:
            for index, (_, start, name) in enumerate(positions):
                stop = positions[index + 1][0] if index + 1 < len(positions) else len(text)
                fragment = text[start:stop]
                if name == "RETURN":
                    if not fragment.strip():
                        self.add("sentence", "return", current)
                    else:
                        self.parse(r"\Return " + fragment,current,depth+1)
                elif name == "REPEAT":
                    if fragment.strip():
                        raise ParseError("Unsupported text after algorithmic Repeat")
                    block = self.add("component", "repeat", current)
                    stack.append(dict(kind="REPEAT", parent=current, block=block, has_else=False))
                    if len(stack) > 60:
                        raise ParseError("Algorithmic nesting exceeds 60")
                    current = block
                elif name == "UNTIL":
                    if not stack or stack[-1]["kind"] != "REPEAT":
                        raise ParseError("Unbalanced algorithmic UNTIL")
                    condition, after = group(fragment, 0)
                    if fragment[after:].strip():
                        raise ParseError("Unsupported text after algorithmic Until")
                    until = self.add("component", "until", current)
                    self.parse(condition, until, depth + 1)
                    current = stack.pop()["parent"]
                elif name in {"STATE", "STATEX", "REQUIRE", "ENSURE"}:
                    if not fragment.strip() and name != "STATEX":
                        if name == "STATE" and index + 1 < len(positions) and positions[index + 1][2] == "RETURN":
                            continue  # State Return is one statement, not an empty State.
                        if name == "STATE" and any(
                                other in {"STATE", "RETURN"} and text[after:(positions[j + 1][0] if j + 1 < len(positions) else len(text))].strip()
                                for j, (_, after, other) in enumerate(positions)):
                            # A blank item within a substantive algorithm is a
                            # numbered spacer. An entirely empty algorithm remains
                            # invalid, preserving the earlier rejection policy.
                            self.layout_metadata.append(dict(environment="algorithmic", command=name, blank_line=True))
                            continue
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
                    comment = re.match(r"\s*\\(?:Comment|COMMENT)\b", allowed_tail)
                    displayed = re.match(r"\s*\\begin\s*\{(?:equation\*?|align\*?|gather\*?)\}",allowed_tail)
                    if allowed_tail.strip() and not comment and not displayed:
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
                    linebreak_only = bool(re.fullmatch(r"\s*(?:\\\\(?:\s*\[[^\]]*\])?\s*)*",tail))
                    if (tail.strip() and not spacing_only and not linebreak_only) or not stack or stack[-1]["kind"] != name[3:]:
                        raise ParseError("Unbalanced algorithmic " + name)
                    current = stack.pop()["parent"]
                    if spacing_only or linebreak_only and tail.strip():
                        self.parse(tail, current, depth + 1)
            if stack:
                raise ParseError("Unclosed algorithmic control block")
        finally:
            self.pseudocode_depth -= 1

    def frontmatter(self, text, parent, depth):
        """Keep template title/author fields as evidence and parse real content."""
        out, pos = [], 0
        metadata = {"title", "runtitle", "author", "address", "ead", "paperid"}
        if self.gatherer.document_class == "ifacconf":
            metadata.add("thanks")
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


    def list_item_projection(self, text, depth=0):
        """Lộ item trong group/font wrapper literal; không thực thi macro tùy ý."""
        if depth > 60:
            raise ParseError("List formatting projection exceeds 60")
        out, cursor = [], 0
        def has_item(value):
            return bool(re.search(r"\\item(?![A-Za-z@])", mask_root_inline_verbatim(mask_literals(value))))
        def safe_group(value):
            probe = mask_root_inline_verbatim(mask_literals(value))
            if re.search(r"\\(?:if[A-Za-z]*|else|fi|def|gdef|edef|xdef|let|newcommand|renewcommand|providecommand|newenvironment|renewenvironment|setcounter|addtocounter|lstset|SetKw[A-Za-z]*|begingroup|endgroup)(?![A-Za-z@])", probe):
                raise ParseError("List item group requires scoped TeX semantics")
            return self.list_item_projection(value, depth + 1)
        while cursor < len(text):
            if text[cursor] == "{":
                value, end = group(text, cursor)
                if has_item(value):
                    # Chỉ group literal ở vị trí được thực thi, không argument
                    # của command chưa hiểu. Giữ mọi nội dung trong group.
                    out.append(safe_group(value))
                    self.gatherer.math_aliases_used.add("Literal list item formatting group exposed")
                else:
                    out.append(text[cursor:end])
                cursor = end
                continue
            if text[cursor] != "\\":
                out.append(text[cursor]);cursor += 1;continue
            name, end = command(text, cursor)
            if name == "begin":
                env, start = group(text, end)
                _, end = environment(text, start, env)
            elif name == "verb":
                start = end + int(text[end:end+1] == "*")
                delimiter = text[start:start+1]
                stop = text.find(delimiter, start+1) if delimiter else -1
                if stop < 0:raise ParseError("Unclosed list verbatim")
                end = stop + 1
            elif name in self.gatherer.macros:
                argc, default, replacement = self.gatherer.macros[name]
                normalized = replacement.strip()
                if normalized.startswith("{"):
                    body, stop = group(normalized, 0)
                    if stop == len(normalized):normalized = body.strip()
                formatting = re.fullmatch(r"\\color\s*\{([A-Za-z][A-Za-z0-9!._:-]*)\}\s*#1", normalized)
                if argc == 1 and default is None and formatting and "color" not in self.gatherer.macros:
                    argument, stop = group(text, end)
                    if has_item(argument):
                        out.append(safe_group(argument));cursor = stop
                        self.layout_metadata.append(dict(command="list_item_color_wrapper", macro=name, color=formatting[1]))
                        continue
            # Các argument của command vẫn opaque. Riêng color/font là declaration
            # không lấy body; scanner sẽ gặp item theo sau nó như bình thường.
            stop = end
            while (start := skip_space(text, stop)) < len(text) and text[start] == "{":
                _, stop = group(text, start)
            out.append(text[cursor:stop]);cursor = stop
        return "".join(out)

    def items(self, text, parent, depth):
        text = self.list_item_projection(text)
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
                    target, cursor = literal_length_target(prefix, cursor)
                    if target.strip() not in {r"\itemsep", r"\leftskip", r"\parskip", r"\parsep", r"\partopsep", r"\topsep"}:
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
            expanded_citations = {key for node in tree.nodes for key in citation_keys(node["content"])}
            used = expanded_citations - {"*"}
            retained_keys = {entry["key"] for entry in entries}
            entries.extend(entry for entry in gatherer.bibliography_candidates
                           if (entry["key"] in used or "*" in expanded_citations)
                           and entry["key"] not in retained_keys)
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
