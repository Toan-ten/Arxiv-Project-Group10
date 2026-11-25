"""Read bounded Biber data, never execute the TeX .bbl program.

Uses the M2 balanced reader and existing BibTeX field/entry normalization.
The producer grammar is documented in biblatex.sty (bbl input handlers).
"""
from __future__ import annotations
import hashlib
import re
from .latex import ParseError, command, group, skip_space

_GLOBAL_DATALIST_SCOPE = re.compile(
    r"[A-Za-z][A-Za-z0-9_-]*/[A-Za-z][A-Za-z0-9_-]*//global/global(?:/global)?"
)


def _name_text(value):
    from .bibliography import plain
    for name, replacement in {"bibnamedelima":" ","bibnamedelimb":" ","bibnamedelimi":" ",
                              "bibinitperiod":".","bibinitdelim":" ","bibinithyphendelim":"-"}.items():
        value = re.sub(r"\\" + name + r"\b\s*", lambda _: replacement, value)
    return plain(value)


def _names(raw, count):
    from .bibliography import parse_bibtex
    names, cursor = [], 0
    while (cursor := skip_space(raw, cursor)) < len(raw):
        record, cursor = group(raw, cursor)
        _, start = group(record, 0)  # producer hash/options, not a person's name
        parts, start = group(record, start)
        if record[start:].strip():
            raise ParseError("Unsupported BibLaTeX name record tail")
        fields = parse_bibtex("@misc{name," + parts + "}")[0]["fields"]
        family = _name_text(fields.get("family", ""))
        if not family:
            raise ParseError("BibLaTeX author has no recoverable family/corporate name")
        name = " ".join(v for v in [_name_text(fields.get("given", "")),
                                    _name_text(fields.get("prefix", "")), family] if v)
        suffix = _name_text(fields.get("suffix", ""))
        names.append(name + (", " + suffix if suffix else ""))
    if not count.isdigit() or len(names) != int(count):
        raise ParseError("BibLaTeX name count does not match records")
    return names


def _comments(text):
    from .bibliography import _bbl_comments
    # Biber's line-verbatim is different from LaTeX's delimiter-based verb.
    # Protect it (including URL percent escapes) before the normal comment pass.
    marker = "M2BIBLATEXVERBTOKEN"
    if marker in text:
        raise ParseError("Reserved marker in BibLaTeX data")
    values = []
    def hide(match):
        values.append(match[0])
        return marker + str(len(values)-1) + "END"
    text = re.sub(
        r"\\verb\{[A-Za-z][A-Za-z0-9_-]*\}[\s\S]*?\\endverb\b",
        hide,
        text,
    )
    text = _bbl_comments(text)
    for i, value in enumerate(values):
        text = text.replace(marker + str(i) + "END", value)
    return text


def _prefix(text):
    if not text.strip():
        return
    cursor = 0
    for expected in ("begingroup", "makeatletter", "@ifundefined"):
        name, cursor = command(text, skip_space(text, cursor))
        if name != expected:
            raise ParseError("Unsupported BibLaTeX data header: " + name)
    target, cursor = group(text, cursor)
    failure, cursor = group(text, cursor)
    success, cursor = group(text, cursor)
    expected = r"\@latex@error{Missing 'biblatex' package}{The bibliography requires the 'biblatex' package.}\aftergroup\endinput"
    if target != "ver@biblatex.sty" or success.strip() or re.sub(r"\s+", "", failure) != re.sub(r"\s+", "", expected):
        raise ParseError("Unsupported BibLaTeX package-presence guard")
    name, cursor = command(text, skip_space(text, cursor))
    if name != "endgroup" or text[cursor:].strip():
        raise ParseError("Unsupported BibLaTeX header tail")


def parse_biblatex(text):
    from .bibliography import _entry
    text = _comments(text)
    has_refsection = bool(re.search(r"\\refsection\b", text))
    first = re.search(r"\\refsection\b" if has_refsection else r"\\datalist\b", text)
    if not first:
        raise ParseError("Unsupported BibLaTeX data: missing refsection or datalist")
    _prefix(text[:first.start()])
    cursor = first.start()
    entries, section_entries, sections = [], {}, set()
    section = listing = finished = False
    implicit_global_section = not has_refsection
    current_section_id = None
    datalist_keys = set()
    while (cursor := skip_space(text, cursor)) < len(text):
        name, cursor = command(text, cursor)
        if name == "refsection" and not section and not finished:
            if implicit_global_section:
                raise ParseError("Unexpected BibLaTeX refsection in implicit global data")
            number, cursor = group(text, cursor)
            if not number.isdigit() or number in sections:
                raise ParseError("Invalid/duplicate BibLaTeX refsection identifier")
            if not sections and number != "0":
                raise ParseError("BibLaTeX data lacks the global refsection")
            current_section_id = number
            sections.add(number)
            section_entries = {}
            section = True
        elif (name == "datalist" and not listing
              and (section or implicit_global_section and not finished)):
            opts, cursor = group(text, skip_space(text, cursor), "[", "]")
            datalist_name, cursor = group(text, cursor)
            if opts != "entry":
                raise ParseError("Unsupported BibLaTeX datalist type: " + opts)
            datalist_keys = set()
            if implicit_global_section:
                if not _GLOBAL_DATALIST_SCOPE.fullmatch(datalist_name):
                    raise ParseError("Unsupported implicit BibLaTeX datalist scope")
                section = True
            listing = True
        elif name == "entry" and section and listing:
            start = cursor - len(r"\entry")
            key, cursor = group(text, cursor)
            typ, cursor = group(text, cursor)
            opts, cursor = group(text, cursor)
            if text[skip_space(text, cursor):skip_space(text, cursor)+1] == "{":
                extra, cursor = group(text, cursor)
                if extra.strip():
                    raise ParseError("Unsupported BibLaTeX fourth entry argument")
            if not re.fullmatch(r"[^\s{}\\,*]+", key) or key in datalist_keys or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", typ):
                raise ParseError("Unsafe/duplicate BibLaTeX entry identity: " + key)
            fields, names, flags, auxiliary = {}, {}, {}, []
            def put(k, value):
                if k in fields and fields[k] != value:
                    raise ParseError("Conflicting BibLaTeX field " + key + "." + k)
                fields[k] = value
            while True:
                cursor = skip_space(text, cursor)
                token, cursor = command(text, cursor)
                if token == "endentry":
                    break
                if token in {"field", "strng", "range"}:
                    field, cursor = group(text, cursor)
                    value, cursor = group(text, cursor)
                    if token == "field":
                        put(field, value)
                    else:
                        auxiliary.append(dict(kind=token, field=field, value=value))
                elif token in {"name", "list"}:
                    field, cursor = group(text, cursor)
                    count, cursor = group(text, cursor)
                    if token == "name":
                        options, cursor = group(text, cursor)
                    else:
                        options = None
                    raw, cursor = group(text, cursor)
                    auxiliary.append(dict(kind=token,field=field,count=count,options=options,raw=raw))
                    if token == "name" and field == "author":
                        parsed = _names(raw, count)
                        if field in names and names[field] != parsed:
                            raise ParseError("Conflicting BibLaTeX author data: " + key)
                        names[field] = parsed
                elif token == "verb":
                    field, cursor = group(text, cursor)
                    ending = re.search(r"(?m)^[ \t]*\\endverb\b", text[cursor:])
                    if not ending:
                        raise ParseError("Malformed BibLaTeX verb field: " + field)
                    lines = text[cursor:cursor + ending.start()].splitlines()
                    chunks = []
                    for line in lines:
                        content = line.lstrip(" \t")
                        if not content:
                            continue
                        match = re.fullmatch(r"\\verb[ \t](.*)", content)
                        if not match:
                            raise ParseError("Malformed BibLaTeX verb field: " + field)
                        chunks.append(match[1])
                    if not chunks:
                        raise ParseError("Empty BibLaTeX verb field: " + field)
                    put(field, "".join(chunks))
                    cursor += ending.end()
                elif token in {"true", "false"}:
                    flag, cursor = group(text, cursor)
                    flags[flag] = token == "true"
                elif token == "keyw":
                    value, cursor = group(text, cursor)
                    auxiliary.append(dict(kind=token,value=value))
                elif token == "warn":
                    value, cursor = group(text, cursor)
                    auxiliary.append(dict(kind="warning", value=value))
                else:
                    raise ParseError("Unsupported BibLaTeX entry macro: " + token)
            mapped = dict(fields)
            if "journaltitle" in fields:
                mapped["journal"] = fields["journaltitle"]
            if "url" not in mapped and "urlraw" in fields:
                mapped["url"] = fields["urlraw"]
            entry = _entry(key, typ, mapped, text[start:cursor], "biblatex_bbl")
            entry["authors"] = names.get("author")
            public_signature = tuple(entry.get(field) for field in
                                     ("entry_type", "title", "authors", "year", "doi", "journal", "url"))
            entry["biblatex_metadata"] = dict(options=opts, flags=flags,
                                               auxiliary=auxiliary,
                                               refsection_id=current_section_id,
                                               datalist_names=[datalist_name],
                                               source_occurrences=1,
                                               datalist_evidence=[{
                                                   "name": datalist_name,
                                                   "sha256": hashlib.sha256(
                                                       entry["raw"].encode("utf-8")).hexdigest(),
                                                   "raw": entry["raw"],
                                               }])
            datalist_keys.add(key)
            previous = section_entries.get(key)
            if previous is None:
                section_entries[key] = entry
                entries.append(entry)
            else:
                previous_metadata = previous["biblatex_metadata"]
                if datalist_name in previous_metadata["datalist_names"]:
                    raise ParseError("Duplicate BibLaTeX key within datalist: " + key)
                if (not _GLOBAL_DATALIST_SCOPE.fullmatch(datalist_name)
                        or not all(_GLOBAL_DATALIST_SCOPE.fullmatch(name)
                                   for name in previous_metadata["datalist_names"])):
                    raise ParseError("Ambiguous duplicate BibLaTeX key across scoped datalists: " + key)
                previous_signature = tuple(previous.get(field) for field in
                                           ("entry_type", "title", "authors", "year",
                                            "doi", "journal", "url"))
                if public_signature != previous_signature:
                    raise ParseError("Conflicting duplicate BibLaTeX public entry: " + key)
                previous_metadata["datalist_names"].append(datalist_name)
                previous_metadata["source_occurrences"] += 1
                previous_metadata["datalist_evidence"].extend(
                    entry["biblatex_metadata"]["datalist_evidence"])
        elif name == "enddatalist" and listing:
            listing = False
        elif name == "endrefsection" and section and not listing and not implicit_global_section:
            section = False
            current_section_id = None
            section_entries = {}
        elif (name == "endinput" and not text[cursor:].strip()
              and not listing
              and (implicit_global_section and section or not implicit_global_section and not section)):
            finished = True
            break
        else:
            raise ParseError("Unsupported BibLaTeX outer data command/state: " + name)
    if (not implicit_global_section and not section and not listing and sections
            and not text[cursor:].strip()):
        finished = True
    if (listing or not finished
            or implicit_global_section and not section
            or not implicit_global_section and section):
        raise ParseError("Unclosed BibLaTeX data section")
    return entries
