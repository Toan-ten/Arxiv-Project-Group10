"""Read BibTeX/bibitem and unify citation keys within one publication only."""
from __future__ import annotations

import hashlib
import re
import unicodedata

from .latex import ParseError, command, environment, group, normalize, skip_space, strip_comments


FIELDS = ("title", "authors", "year", "doi", "journal", "url")


def plain(value):
    return normalize(value).replace("{", "").replace("}", "").strip() or None


def authors(value):
    if not value:
        return None
    parts, start, depth = [], 0, 0
    for m in re.finditer(r"\{|\}|\s+and\s+", value, re.I):
        if m[0] == "{":
            depth += 1
        elif m[0] == "}":
            depth -= 1
        elif depth == 0:
            parts.append(plain(value[start:m.start()]))
            start = m.end()
    parts.append(plain(value[start:]))
    return [p for p in parts if p] or None


def _value(text, pos, strings):
    chunks = []
    while True:
        pos = skip_space(text, pos)
        if pos >= len(text):
            raise ParseError("Missing BibTeX field value")
        if text[pos] == "{":
            chunk, pos = group(text, pos)
        elif text[pos] == '"':
            start, pos, depth = pos + 1, pos + 1, 0
            while pos < len(text):
                if text[pos] == "\\":
                    pos += 2
                    continue
                depth += int(text[pos] == "{") - int(text[pos] == "}")
                if text[pos] == '"' and depth == 0:
                    break
                pos += 1
            if pos >= len(text):
                raise ParseError("Unclosed BibTeX quoted value")
            chunk, pos = text[start:pos], pos + 1
        else:
            m = re.match(r"[^,#\s]+", text[pos:])
            if not m:
                raise ParseError("Invalid BibTeX value")
            chunk, pos = strings.get(m[0].lower(), m[0]), pos + len(m[0])
        chunks.append(chunk)
        pos = skip_space(text, pos)
        if text[pos:pos + 1] != "#":
            return "".join(chunks), pos
        pos += 1


def parse_bibtex(text, strings=None):
    entries, pos = [], 0
    strings = {} if strings is None else strings
    while True:
        m = re.search(r"@([A-Za-z]+)\s*([{(])", text[pos:])
        if not m:
            break
        absolute_start = pos + m.start()
        line_start = text.rfind("\n", 0, absolute_start) + 1
        if "%" in text[line_start:absolute_start]:
            newline = text.find("\n", absolute_start)
            if newline < 0:
                break
            pos = newline + 1
            continue
        typ = m[1].lower()
        opening = pos + m.end() - 1
        body, pos = group(text, opening, m[2], "}" if m[2] == "{" else ")")
        if typ in {"comment", "preamble"}:
            continue
        if typ == "string":
            match = re.match(r"\s*([\w:.-]+)\s*=", body)
            if not match:
                raise ParseError("Invalid BibTeX @string")
            strings[match[1].lower()], _ = _value(body, match.end(), strings)
            continue
        if "," not in body:
            raise ParseError("BibTeX entry has no citation key separator")
        key, body = body.split(",", 1)
        key = key.strip()
        if not key or re.search(r"[\s{}\\]", key):
            raise ParseError("Unsafe/empty BibTeX citation key")
        fields, cursor = {}, 0
        while cursor < len(body):
            cursor = skip_space(body, cursor)
            if body[cursor:cursor + 1] == "%":
                newline = body.find("\n", cursor)
                cursor = len(body) if newline < 0 else newline + 1
                continue
            if body[cursor:cursor + 1] == ",":
                cursor += 1
                continue
            if cursor >= len(body):
                break
            match = re.match(r"([A-Za-z_][\w-]*)\s*=", body[cursor:])
            if not match:
                raise ParseError(f"Malformed BibTeX fields for {key}: {body[cursor:cursor+40]!r}")
            field = match[1].lower()
            value, cursor = _value(body, cursor + match.end(), strings)
            if field in fields and fields[field] != value:
                raise ParseError(f"Conflicting duplicate BibTeX field {key}.{field}")
            fields[field] = value
        entries.append(_entry(key, typ, fields, body, "bibtex"))
    # Resolve inherited metadata without dropping raw crossref evidence.
    by_key = {e["key"]: e for e in entries}
    for e in entries:
        parent = by_key.get(e["fields"].get("crossref"))
        if parent:
            for f, val in parent["fields"].items():
                if f != "crossref":
                    e["fields"].setdefault(f, val)
            e.update(_entry(e["key"], e["entry_type"], e["fields"], e["raw"], "bibtex"))
    return entries


def _entry(key, typ, fields, raw, provider):
    year = plain(fields.get("year", ""))
    return dict(key=key, entry_type=typ, title=plain(fields.get("title", "")),
                authors=authors(fields.get("author")), year=int(year) if year and re.fullmatch(r"\d{4}", year) else None,
                doi=plain(fields.get("doi", "")), journal=plain(fields.get("journal", "")),
                url=plain(fields.get("url", "")), fields=dict(fields), raw=raw, provider=provider)


def _top_level_bibitems(body):
    matches, cursor, depth = [], 0, 0
    while cursor < len(body):
        if body[cursor] == "\\":
            start = cursor
            name, cursor = command(body, cursor)
            if name == "bibitem" and depth == 0:
                cursor = skip_space(body, cursor)
                if body[cursor:cursor + 1] == "[":
                    label, cursor = group(body, cursor, "[", "]")
                    # An unbraced nested square label is not portable.
                    if re.search(r"(?<!\{)\[[^{}]*", label):
                        raise ParseError("malformed .bbl bibitem optional label")
                key, cursor = group(body, skip_space(body, cursor))
                matches.append((start, cursor, key.strip()))
        else:
            depth += int(body[cursor] == "{") - int(body[cursor] == "}")
            if depth < 0:
                raise ParseError("Unbalanced .bbl item braces")
            cursor += 1
    if depth:
        raise ParseError("Unbalanced .bbl item braces")
    return matches


def parse_bibitems(body: str) -> list[dict]:
    """Read top-level bibitems from an already selected bibliography body."""
    matches = _top_level_bibitems(body)
    entries = []
    for idx, (_, end, key) in enumerate(matches):
        raw = body[end:matches[idx + 1][0] if idx + 1 < len(matches) else len(body)].strip()
        # Only explicit newblock boundaries justify author/title assignment.
        blocks = re.split(r"\\newblock\b", raw)
        fields = {}
        if len(blocks) >= 2:
            first, second = blocks[0].strip(), blocks[1].strip()
            year_in_first = re.search(r"(?<!\d)(?:18|19|20)\d{2}(?!\d)", first)
            if first and (not year_in_first or re.search(r"(?:18|19|20)\d{2}\.?\s*$", first)):
                fields["author"] = first.strip(" .\n")
            if second and not re.match(r"\\(?:url|href|path|doi)\b|In\s+\\?emph\b", second):
                fields["title"] = re.sub(
                    r"[,\s]+(?:(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+)?"
                    r"(?:18|19|20)\d{2}\.?\s*$", "", second).strip(" .\n")
        doi = re.search(r"(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)", raw)
        url = re.search(r"\\(?:url|href)\{([^}]+)\}", raw)
        year = re.search(r"(?<!\d)((?:18|19|20)\d{2})(?!\d)", raw)
        if doi:
            fields["doi"] = doi[1].rstrip(".,")
        if url:
            fields["url"] = url[1]
        if year:
            fields["year"] = year[1]
        entries.append(_entry(key, "misc", fields, raw, "bibitem_conservative"))
    return entries


def _bbl_setup(text):
    """Accept bounded style setup without executing it or dropping prose."""
    pos = 0
    noops = set()
    while (pos := skip_space(text, pos)) < len(text):
        if text[pos] != "\\":
            raise ParseError("Unsupported .bbl text outside bibliography items")
        name, pos = command(text, pos)
        if name in {"makeatletter", "makeatother", "relax", "bibcommenthead"}:
            continue
        if name in noops:
            cursor = skip_space(text, pos)
            if text[cursor:cursor + 2] == "{}":
                pos = cursor + 2
            continue
        if name == "itemsep":
            match = re.match(r"\s*=\s*-?\d+(?:\.\d+)?(?:pt|em|ex|mm|cm)\b", text[pos:])
            if not match:
                raise ParseError("Unsupported .bbl itemsep value")
            pos += match.end()
            continue
        if name == "urlstyle":
            style, pos = group(text, skip_space(text, pos))
            if style not in {"rm", "sf", "tt", "same"}:
                raise ParseError("Unsupported .bbl URL style")
            continue
        if name in {"mciteSetBstSublistMode", "mciteSetBstMaxWidthForm",
                    "mciteSetBstSublistLabelBeginEnd", "mciteSetBstMidEndSepPunct"}:
            arity = {"mciteSetBstSublistMode": 1,
                     "mciteSetBstMaxWidthForm": 2,
                     "mciteSetBstSublistLabelBeginEnd": 3,
                     "mciteSetBstMidEndSepPunct": 3}[name]
            arguments = []
            for _ in range(arity):
                argument, pos = group(text, skip_space(text, pos))
                arguments.append(argument)
            allowed = {
                "mciteSetBstSublistMode": {("f",)},
                "mciteSetBstMaxWidthForm": {("subitem", r"(\alph{mcitesubitemcount})")},
                "mciteSetBstSublistLabelBeginEnd": {
                    (r"\mcitemaxwidthsubitemform\space", r"\relax", r"\relax")
                },
                "mciteSetBstMidEndSepPunct": {
                    (r"\mcitedefaultmidpunct", r"\mcitedefaultendpunct",
                     r"\mcitedefaultseppunct")
                },
            }
            if tuple(arguments) not in allowed[name]:
                raise ParseError("Unsupported .bbl citation-list presentation setup: " + name)
            continue
        if name == "setlength":
            length, pos = group(text, skip_space(text, pos))
            value, pos = group(text, skip_space(text, pos))
            if (length not in {r"\itemsep", r"\parsep", r"\parskip"}
                    or not re.fullmatch(r"\s*-?\d+(?:\.\d+)?(?:pt|em|ex|mm|cm)\s*", value)):
                raise ParseError("Unsupported .bbl setlength value")
            continue
        if name == "csname":
            hook = re.match(r"\s*PreBibitemsHook\s*\\endcsname\b", text[pos:])
            if hook:
                pos += hook.end()
                continue
            match = re.match(
                r"\s*(?:url@samestyle|url@rmstyle)\s*\\endcsname\b", text[pos:])
            if not match:
                raise ParseError("Unsupported .bbl setup csname")
            pos += match.end()
            continue
        if name == "def":
            target, pos = command(text, skip_space(text, pos))
            if target.startswith("mn@"):
                brace = text.find("{", pos)
                if brace < 0 or len(text[pos:brace]) > 120:
                    raise ParseError("Malformed .bbl presentation definition")
                replacement, pos = group(text, brace)
                if re.search(r"\\(?:bibitem|harvarditem|entry|input|include)\b|\\(?:begin|end)\s*\{thebibliography\}", replacement):
                    raise ParseError("Structural command in .bbl presentation definition")
                continue
            if target == "gobbledotblock":
                match = re.match(r"\s*,\s*\{\s*\}", text[pos:])
                if not match:
                    raise ParseError("Unsupported .bbl punctuation helper definition")
                pos += match.end()
                continue
            parameters = re.match(r"\s*#1\s*", text[pos:])
            if target not in {"au", "ed", "yr", "at", "jt", "bt", "bvol",
                              "vol", "pg", "publ", "arxiv", "org", "st",
                              "@biblabel"} or not parameters:
                raise ParseError("Unsupported .bbl setup definition: " + target)
            brace = pos + parameters.end()
            replacement, pos = group(text, brace)
            normalized = re.sub(r"\s+", "", replacement)
            if normalized not in {
                    "#1", r"\textit{#1}", r"\textbf{#1}",
                    r"\textsc{#1}", r"\emph{#1}", "#1."}:
                raise ParseError("Unsupported .bbl presentation definition: " + target)
            continue
        if name in {"newcommand", "renewcommand", "providecommand", "DeclareRobustCommand"}:
            pos += int(text[pos:pos + 1] == "*")
            pos = skip_space(text, pos)
            if text[pos:pos + 1] == "{":
                target, pos = group(text, pos)
            else:
                target, pos = command(text, pos)
            if target.lstrip("\\") in {"bibitem", "begin", "end"}:
                raise ParseError("Unsupported .bbl structural command redefinition")
            arguments = []
            for _ in range(2):
                pos = skip_space(text, pos)
                if text[pos:pos + 1] == "[":
                    value, pos = group(text, pos, "[", "]")
                    arguments.append(value)
            replacement, pos = group(text, pos)
            mcite_arities = {
                "mciteSetBstSublistMode": "1",
                "mciteSetBstMaxWidthForm": "2",
                "mciteSetBstSublistLabelBeginEnd": "3",
                "mciteSetBstMidEndSepPunct": "3",
            }
            target_name = target.lstrip("\\")
            if (not arguments or arguments == ["0"]) and replacement.strip() in ("", r"\relax"):
                noops.add(target_name)
            elif target_name in mcite_arities:
                if arguments != [mcite_arities[target_name]] or replacement.strip():
                    raise ParseError("Unsupported .bbl mcite setup definition: " + target_name)
            elif target_name in {"mciteBstWouldAddEndPuncttrue",
                                 "mciteBstWouldAddEndPunctfalse"}:
                expected = {
                    "mciteBstWouldAddEndPuncttrue": r"\def\EndOfBibitem{\unskip.}",
                    "mciteBstWouldAddEndPunctfalse": r"\let\EndOfBibitem\relax",
                }
                if arguments or re.sub(r"\s+", "", replacement) != expected[target_name]:
                    raise ParseError("Unsupported .bbl mcite punctuation definition")
                noops.add(target_name)
            elif target_name == "natexlab":
                if arguments != ["1"] or replacement.strip() != "#1":
                    raise ParseError("Unsupported .bbl natbib label definition")
                noops.add("natexlab")
        elif name == "let":
            target, pos = command(text, skip_space(text, pos))
            if target in {"bibitem", "begin", "end"}:
                raise ParseError("Unsupported .bbl structural command alias")
            pos = skip_space(text, pos)
            if text[pos:pos + 1] == "=":
                pos = skip_space(text, pos + 1)
            _, pos = command(text, pos)
        elif name == "ifx":
            # ACM/BMC style defaults: only a known presentation helper may be
            # defined if undefined. Neither branch adds/removes bibliography
            # items. We keep item metadata raw instead of executing the helper
            # or assuming whether an external style already defined it.
            target, cursor = command(text, skip_space(text, pos))
            comparator, cursor = command(text, skip_space(text, cursor))
            definition, cursor = command(text, skip_space(text, cursor))
            defined, cursor = command(text, skip_space(text, cursor))
            if (target == "xfnm" and comparator in {"relax", "undefined"}
                    and definition == "def" and defined == "xfnm"):
                match = re.match(r"\s*\[#1\]\s*\{\\unskip,\\space#1\}\s*\\fi\b", text[cursor:])
                if not match:
                    raise ParseError("Unsupported .bbl xfnm definition")
                pos = cursor + match.end()
                continue
            allowed = {"showCODEN", "showDOI", "showISBNx", "showISBNxiii", "showISSN", "showLCCN", "shownote", "showarticletitle", "showURL", "bisbn", "binits", "bauthor", "batitle", "bjtitle", "bvolume", "byear", "bissue", "bfpage", "blpage", "burl", "doiurl", "betal", "binstitute", "binstitutionaled", "bctitle", "beditor", "bpublisher", "bbtitle", "bedition", "bseriesno", "blocation", "bsertitle", "bsnm", "bsuffix", "bparticle", "barticle", "bconfdate", "botherref", "url", "bchapter", "bbook", "bcomment", "oauthor", "citeauthoryear", "endbibitem", "bconflocation", "arxivurl"}
            if comparator != "undefined" or definition != "def" or target != defined or target not in allowed:
                raise ParseError("Unsupported .bbl setup ifx target/definition: " + target)
            brace = text.find("{", cursor)
            if brace < 0 or text[cursor:brace].strip() not in {"", "#1"}:
                raise ParseError("Unsupported .bbl setup ifx parameters: " + target)
            replacement, cursor = group(text, brace)
            commands = set(re.findall(r"\\([A-Za-z@]+)", replacement))
            if not commands <= {"relax", "unskip", "textbf", "textit", "textsf", "url", "burl"} or re.search(r"#(?!1)|[{}]", re.sub(r"\{[^{}]*\}", "", replacement)):
                raise ParseError("Unsupported .bbl setup ifx replacement: " + target)
            terminator, cursor = command(text, skip_space(text, cursor))
            if terminator != "fi":
                raise ParseError("Unsupported .bbl setup ifx branch: " + target)
            pos = cursor
        elif name == "expandafter":
            # Standard natbib URL-style guard. Both branches only define DOI
            # presentation; neither changes which references/items exist.
            identity = re.match(r"\s*\\ifx\s*\\csname\s+(?P<identity>natexlab|bibnamefont|bibfnamefont|citenamefont)\\endcsname\s*\\relax\s*\\def\s*\\(?P=identity)\s*#1\s*\{#1\}\s*\\fi\b", text[pos:])
            if identity:
                pos += identity.end()
                continue
            style_guard = re.match(
                r"\s*\\ifx\s*\\csname\s+urlstyle\\endcsname\s*\\relax"
                r"(.*?)\\else(.*?)\\fi\b", text[pos:], re.S)
            if style_guard and all(re.fullmatch(
                    r"\s*(?:\\urlstyle\{(?:rm|sf|tt|same)\}\s*)?",
                    branch) for branch in style_guard.groups()):
                pos += style_guard.end()
                continue
            href_guard = re.match(
                r"\s*\\ifx\s*\\csname\s+href\\endcsname\s*\\relax"
                r"(.*?)\\else(.*?)\\fi\b", text[pos:], re.S)
            if href_guard:
                branches = tuple(re.sub(r"\s+", "", branch)
                                 for branch in href_guard.groups())
                expected = (
                    r"\DeclareUrlCommand\doi{\urlstyle{rm}}\def\eprint#1#2{#2}",
                    r"\def\doi#1{\href{https://doi.org/#1}{\nolinkurl{#1}}}\let\eprint\href",
                )
                if branches == expected:
                    pos += href_guard.end()
                    continue
            guard = re.match(r"\s*\\ifx\s*\\csname\s+urlstyle\\endcsname\s*\\relax(.*?)\\else(.*?)\\fi\b",
                             text[pos:], re.S)
            if not guard:
                # IOP-generated URL fallbacks only affect presentation. Exact
                # patterns exclude structural changes or conditional items.
                literal = re.match(
                    r"\s*\\ifx\s*\\csname\s+url\\endcsname\s*\\relax\s*"
                        r"\\def\s*\\url\s*#1\s*\{(?:\{\\tt\s+#1\}|\\texttt\s*\{#1\}|\\burl\s*\{#1\})\}\s*\\fi\b", text[pos:])
                prefix = re.match(
                    r"\s*\\ifx\s*\\csname\s+urlprefix\\endcsname\s*\\relax\s*"
                        r"\\def\s*\\urlprefix\s*\{(?:URL: |URL |Available at )\}\s*\\fi\b", text[pos:])
                doi_prefix = re.match(
                    r"\s*\\ifx\s*\\csname\s+doiprefix\\endcsname\s*\\relax\s*"
                    r"\\def\s*\\doiprefix\s*\{(?:DOI: | )\}\s*\\fi\b", text[pos:])
                href = re.match(
                    r"\s*\\ifx\s*\\csname\s+href\\endcsname\s*\\relax\s*"
                    r"\\def\s*\\href\s*#1\s*#2\s*\{#2\}\s*"
                    r"\\def\s*\\path\s*#1\s*\{#1\}\s*\\fi\b", text[pos:])
                if literal or prefix or doi_prefix or href:
                    pos += (literal or prefix or doi_prefix or href).end()
                    continue
                raise ParseError("Unsupported .bbl setup conditional")
            for branch in guard.group(1, 2):
                if not re.match(r"\s*\\providecommand\s*\{\\doi\}", branch):
                    raise ParseError("Unsupported .bbl URL setup branch")
                _bbl_setup(branch)
            pos += guard.end()
        else:
            raise ParseError(f"Unsupported .bbl setup command: \\{name}")


def _explicit_bibinfo(entry):
    """Only labelled REVTeX fields; unknown name macros remain raw, not guessed."""
    values = {}
    for match in re.finditer(r"\\bibinfo\b", entry["raw"]):
        field, pos = group(entry["raw"], match.end())
        value, _ = group(entry["raw"], pos)
        values.setdefault(field.strip(), []).append(value)
    for field in ("title", "journal", "year"):
        if field in values:
            distinct = list(dict.fromkeys(plain(v) for v in values[field]))
            if len(distinct) != 1:
                raise ParseError(f"Conflicting labelled .bbl field: {entry['key']}.{field}")
            value = distinct[0]
            if value and re.search(r"\\(?:if[a-zA-Z]*|else|fi)\b", value):
                value = None  # Keep unresolved conditional metadata only in raw.
            entry[field] = (int(value) if value and re.fullmatch(r"\d{4}", value) else None) if field == "year" else value
            entry["fields"][field] = values[field][0]
    if "author" in values:
        names = []
        for value in values["author"]:
            value = re.sub(r"\\(?:bibfnamefont|bibnamefont|citenamefont)\b", "", value)
            value = re.sub(r"\\\s", " ", value)
            names.append(plain(value) if "\\" not in value else None)
        entry["authors"] = names if names and all(names) else None
        entry["fields"]["author"] = " and ".join(values["author"])


def _bbl_comments(text):
    """URL readers treat percent escapes as data; ordinary TeX comments do not."""
    if "\x00" in text:
        raise ParseError("NUL in .bbl source")
    protected, out, pos = [], [], 0
    while pos < len(text):
        if text[pos] == "%":
            end = text.find("\n", pos)
            end = len(text) if end < 0 else end + 1
            out.append(text[pos:end])
            pos = end
        elif text[pos] == "\\":
            name, end = command(text, pos)
            start = skip_space(text, end)
            if name in {"url", "href", "path", "doi"} and text[start:start + 1] == "{":
                _, end = group(text, start)
                out.append(f"\x00{len(protected)}\x00")
                protected.append(text[pos:end])
            else:
                out.append(text[pos:end])
            pos = end
        else:
            out.append(text[pos])
            pos += 1
    result = strip_comments("".join(out))
    for number, original in enumerate(protected):
        result = result.replace(f"\x00{number}\x00", original)
    return result


def _normalize_mcite_wrapper(text: str) -> str:
    """Normalize the exact static mcite alias used for thebibliography."""
    text = _bbl_comments(text)
    start_alias = re.compile(
        r"\\providecommand\s*\*?\s*\\mcitethebibliography\s*\{\s*\\thebibliography\s*\}"
    )
    end_alias = re.compile(
        r"\\csname\s*@ifundefined\s*\\endcsname\s*"
        r"\{\s*endmcitethebibliography\s*\}\s*"
        r"\{\s*\\let\s*\\endmcitethebibliography\s*\\endthebibliography\s*\}\s*\{\s*\}"
    )
    starts, ends = list(start_alias.finditer(text)), list(end_alias.finditer(text))
    begin_envs = list(re.finditer(r"\\begin\s*\{mcitethebibliography\}", text))
    end_envs = list(re.finditer(r"\\end\s*\{mcitethebibliography\}", text))
    if not (len(starts) == len(ends) == len(begin_envs) == len(end_envs) == 1):
        raise ParseError("Unsupported .bbl mcite bibliography wrapper")
    if not (starts[0].start() < ends[0].start() < begin_envs[0].start() < end_envs[0].start()):
        raise ParseError("Unsupported .bbl mcite bibliography wrapper order")
    text = start_alias.sub("", text, count=1)
    text = end_alias.sub("", text, count=1)
    text = re.sub(r"\\begin\s*\{mcitethebibliography\}", r"\\begin{thebibliography}", text, count=1)
    text = re.sub(r"\\end\s*\{mcitethebibliography\}", r"\\end{thebibliography}", text, count=1)
    return text


def parse_bbl(text: str, *, enriched: bool = False) -> list[dict]:
    """Read one static thebibliography list; never execute .bbl control code."""
    from .bbl_formats import enrich_entry, parse_amsrefs, parse_harvard

    if re.search(r"\\begin\s*\{bibdiv\}", text):
        entries = parse_amsrefs(text)
        return [enrich_entry(entry) for entry in entries] if enriched else entries
    if re.search(r"\\harvarditem\b", text):
        entries = parse_harvard(text)
        return [enrich_entry(entry) for entry in entries] if enriched else entries
    if "mcitethebibliography" in text or "endmcitethebibliography" in text:
        text = _normalize_mcite_wrapper(text)
    if (re.search(r"\\(?:refsection|datalist)\b", text)
            and re.search(r"\\entry\b", text)):
        from .biblatex import parse_biblatex
        entries = parse_biblatex(text)
        return [enrich_entry(entry) for entry in entries] if enriched else entries
    if not re.search(r"\\begin\s*\{thebibliography\}", text):
        raise ParseError("Unsupported .bbl format: expected one thebibliography; BibLaTeX entry format is not supported")
    text = _bbl_comments(text)
    starts = list(re.finditer(r"\\begin\s*\{thebibliography\}", text))
    if len(starts) != 1:
        raise ParseError("Unsupported .bbl format: expected one thebibliography; BibLaTeX entry format is not supported")
    match = starts[0]
    _, pos = group(text, match.end())
    raw, end = environment(text, pos, "thebibliography")
    prefix, suffix = text[:match.start()], text[end:]
    # JHEP's static wrapper scopes raggedright around the whole bibliography.
    # Match BOTH boundaries and this exact layout payload; do not erase an
    # arbitrary group or allow setup definitions to leak outside their scope.
    wrapper = re.search(r"\\begingroup\s*\\raggedright\s*$", prefix)
    if wrapper:
        if not re.fullmatch(r"\s*\\endgroup\s*(?:\\endinput\s*)?", suffix):
            raise ParseError("Unbalanced/unsupported .bbl bibliography layout group")
        prefix, suffix = prefix[:wrapper.start()], ""
    _bbl_setup(prefix)
    if suffix.strip() not in ("", r"\endinput"):
        raise ParseError("Unsupported .bbl content after thebibliography")
    markers = _top_level_bibitems(raw)
    _bbl_setup(raw[:markers[0][0]] if markers else raw)
    entries = parse_bibitems(raw)
    if len(markers) != len(entries):
        raise ParseError("Unsupported or malformed .bbl bibitem label/key")
    if markers:
        # A conditional inside an author/accent argument remains opaque metadata.
        # A top-level conditional could hide/add whole items and is rejected.
        depth, cursor = 0, markers[0][0]
        forbidden = {"if", "ifx", "ifnum", "ifdefined", "iffalse", "iftrue", "ifmmode",
                     "ifdim", "ifcat", "ifodd", "ifcase", "ifhmode", "ifvmode", "ifcsname",
                     "else", "fi", "input", "include", "def", "gdef", "newcommand", "renewcommand"}
        while cursor < len(raw):
            if raw[cursor] == "\\":
                name, cursor = command(raw, cursor)
                if name == "bibitem" and depth == 0:
                    cursor = skip_space(raw, cursor)
                    if raw[cursor:cursor + 1] == "[":
                        _, cursor = group(raw, cursor, "[", "]")
                    _, cursor = group(raw, cursor)
                elif name in forbidden and depth == 0:
                    raise ParseError("Unsupported conditional/dynamic .bbl bibliography items")
            else:
                depth += int(raw[cursor] == "{") - int(raw[cursor] == "}")
                if depth < 0:
                    raise ParseError("Unbalanced .bbl item braces")
                cursor += 1
        if depth:
            raise ParseError("Unbalanced .bbl item braces")
    for entry in entries:
        if not entry["key"] or re.search(r"[\s{}\\]", entry["key"]):
            raise ParseError("Unsafe/empty .bbl citation key")
        if r"\bibinfo" in entry["raw"]:
            _explicit_bibinfo(entry)
    pdi_macros = bool(re.search(r"\\def\s*\\au\s*#1", text)
                      and re.search(r"\\def\s*\\at\s*#1", text))
    return [enrich_entry(entry, pdi_macros=pdi_macros) for entry in entries] if enriched else entries


def _norm(value):
    return re.sub(r"\W+", "", unicodedata.normalize("NFKC", value or "").casefold())


def _same(left, right):
    ld, rd = _norm(left["doi"]), _norm(right["doi"])
    if ld and rd:
        return ld == rd
    if left["title"] and right["title"] and left["authors"] and right["authors"]:
        return (_norm(left["title"]) == _norm(right["title"])
                and _norm(" ".join(left["authors"])) == _norm(" ".join(right["authors"])))
    return left["provider"] == right["provider"] and normalize(left["raw"]) == normalize(right["raw"])


def unify(arxiv_id, per_version):
    """Stable first key, field union, explicit conflicts and per-version aliases."""
    canonical, aliases, conflicts = [], {}, []
    for version, entries in per_version.items():
        amap = aliases[version] = {}
        for e in entries:
            found = next((c for c in canonical if _same(c, e)), None)
            if found is None:
                found = dict(e)
                used = {c["key"] for c in canonical}
                if found["key"] in used:
                    found["key"] += "__" + hashlib.sha256(e["raw"].encode()).hexdigest()[:10]
                canonical.append(found)
            else:
                for field in FIELDS:
                    if found[field] is None:
                        found[field] = e[field]
                    elif e[field] is not None and found[field] != e[field]:
                        conflicts.append(dict(citation_key=found["key"], field=field,
                                              kept=found[field], observed=e[field], version=version))
            if e["key"] in amap and amap[e["key"]] != found["key"]:
                raise ParseError(f"Ambiguous citation key within {version}: {e['key']}")
            amap[e["key"]] = found["key"]
    rows = [dict(arxiv_id=arxiv_id, citation_key=e["key"], entry_type=e["entry_type"],
                 **{f: e[f] for f in FIELDS}) for e in canonical]
    return rows, aliases, conflicts
