"""Read bounded Biber data, never execute the TeX .bbl program.

Uses the M2 balanced reader and existing BibTeX field/entry normalization.
The producer grammar is documented in biblatex.sty (bbl input handlers).
"""
from __future__ import annotations
import re
from .latex import ParseError, command, group, skip_space


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
    text = re.sub(r"\\verb\{[A-Za-z][A-Za-z0-9_-]*\}\s*\\verb[ \t][^\r\n]*\r?\n\s*\\endverb\b", hide, text)
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
    first = re.search(r"\\refsection\b", text)
    if not first:
        raise ParseError("Unsupported BibLaTeX data: missing refsection")
    _prefix(text[:first.start()])
    cursor = first.start()
    entries, keys = [], set()
    section = listing = finished = False
    while (cursor := skip_space(text, cursor)) < len(text):
        name, cursor = command(text, cursor)
        if name == "refsection" and not section and not finished:
            number, cursor = group(text, cursor)
            if number != "0":
                raise ParseError("Multiple/non-global BibLaTeX refsections require scoped bibliography semantics")
            section = True
        elif name == "datalist" and section and not listing:
            opts, cursor = group(text, skip_space(text, cursor), "[", "]")
            _, cursor = group(text, cursor)
            if opts != "entry":
                raise ParseError("Unsupported BibLaTeX datalist type: " + opts)
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
            if not re.fullmatch(r"[^\s{}\\,*]+", key) or key in keys or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", typ):
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
                    match = re.match(r"\s*\\verb[ \t]([^\r\n]*)\r?\n\s*\\endverb\b", text[cursor:])
                    if not match:
                        raise ParseError("Malformed BibLaTeX verb field: " + field)
                    put(field, match[1]);cursor += match.end()
                elif token in {"true", "false"}:
                    flag, cursor = group(text, cursor)
                    flags[flag] = token == "true"
                elif token == "keyw":
                    value, cursor = group(text, cursor)
                    auxiliary.append(dict(kind=token,value=value))
                else:
                    raise ParseError("Unsupported BibLaTeX entry macro: " + token)
            mapped = dict(fields)
            if "journaltitle" in fields:
                mapped["journal"] = fields["journaltitle"]
            if "url" not in mapped and "urlraw" in fields:
                mapped["url"] = fields["urlraw"]
            entry = _entry(key, typ, mapped, text[start:cursor], "biblatex_bbl")
            entry["authors"] = names.get("author")
            entry["biblatex_metadata"] = dict(options=opts,flags=flags,auxiliary=auxiliary)
            entries.append(entry);keys.add(key)
        elif name == "enddatalist" and listing:
            listing = False
        elif name == "endrefsection" and section and not listing:
            section, finished = False, True
        elif name == "endinput" and finished and not text[cursor:].strip():
            break
        else:
            raise ParseError("Unsupported BibLaTeX outer data command/state: " + name)
    if section or listing or not finished:
        raise ParseError("Unclosed BibLaTeX data section")
    return entries
