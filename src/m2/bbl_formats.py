"""Bounded readers and field extraction for bibliography styles in M2."""
from __future__ import annotations

import re

from .latex import ParseError, command, group, skip_space


def _command_values(text: str, name: str) -> list[str]:
    values = []
    for match in re.finditer(r"\\" + re.escape(name) + r"\b", text):
        position = skip_space(text, match.end())
        if text[position:position + 1] == "{":
            value, _ = group(text, position)
            values.append(value)
    return values


def _comma_parts(text: str) -> list[str]:
    """Split rendered citation commas outside balanced TeX groups."""
    parts, start, depth, cursor = [], 0, 0, 0
    while cursor < len(text):
        if text[cursor] == "\\":
            cursor += 2
            continue
        if text[cursor] == "{":
            depth += 1
        elif text[cursor] == "}":
            depth -= 1
        elif text[cursor] == "," and depth == 0:
            parts.append(text[start:cursor].strip())
            start = cursor + 1
        cursor += 1
    parts.append(text[start:].strip())
    return parts


def _initial_author(value: str) -> bool:
    from .bibliography import plain

    value = plain(value) or ""
    return bool(re.fullmatch(
        r"(?:[A-Z](?:\.-[A-Z])?\.\s*){1,4}(?:[a-z]{2,4}\s+)?[A-Z][\w\\'\"-]+(?:\s+[A-Z][\w\\'\"-]+){0,2}",
        value))


def parse_harvard(text: str) -> list[dict]:
    """Read static harvarditem records without evaluating style macros."""
    from .bibliography import _bbl_comments, _entry, plain

    text = _bbl_comments(text)
    start = re.search(r"\\begin\s*\{thebibliography\}\s*\{[^{}]*\}", text)
    end = re.search(r"\\end\s*\{thebibliography\}", text)
    if not start or not end or text[:start.start()].strip() or text[end.end():].strip():
        raise ParseError("Unsupported Harvard bibliography wrapper")
    body = text[start.end():end.start()]
    markers = []
    for match in re.finditer(r"\\harvarditem\b", body):
        cursor = skip_space(body, match.end())
        if body[cursor:cursor + 1] == "[":
            _, cursor = group(body, cursor, "[", "]")
        author, cursor = group(body, skip_space(body, cursor))
        year, cursor = group(body, skip_space(body, cursor))
        key, cursor = group(body, skip_space(body, cursor))
        if not re.fullmatch(r"[^\s{}\\,]+", key):
            raise ParseError("Unsafe Harvard citation key")
        markers.append((match.start(), cursor, key, author, year))
    if not markers or body[:markers[0][0]].strip():
        raise ParseError("Unsupported Harvard bibliography content")
    entries = []
    for index, (begin, stop, key, author, year) in enumerate(markers):
        raw = body[stop:markers[index + 1][0] if index + 1 < len(markers) else len(body)].strip()
        if not raw:
            raise ParseError("Empty Harvard bibliography entry: " + key)
        author_text = plain(re.sub(r"\\harvardand\b\s*\\?", " and ", author))
        author_parts = [part.strip() for part in re.split(r",\s*|\s+and\s+", author_text or "")
                        if part.strip()]
        title = (re.search(r"``(.*?)''", raw, re.S)
                 or re.search(r"“(.*?)”", raw, re.S))
        fields = {"author": " and ".join(author_parts), "year": year}
        if title:
            fields["title"] = title[1].strip(" .")
        entry = _entry(key, "misc", fields, body[begin:stop] + raw, "harvard_bbl")
        entry["field_methods"] = {"author": "harvarditem", "year": "harvarditem",
                                  "title": "rendered_quote" if title else "missing"}
        entries.append(entry)
    return entries


def parse_amsrefs(text: str) -> list[dict]:
    """Read amsrefs bib records and preserve repeated author fields."""
    from .bibliography import _entry, _value, plain

    if not re.search(r"\\begin\s*\{bibdiv\}", text) or not re.search(r"\\begin\s*\{biblist\}", text):
        raise ParseError("Unsupported AMSRefs wrapper")
    prefix = text.split(r"\begin{bibdiv}", 1)[0]
    if re.sub(r"(?m)^%[^\n]*(?:\n|$)|\s+", "", prefix):
        raise ParseError("Unsupported AMSRefs setup")
    body = text.split(r"\begin{biblist}", 1)[1].split(r"\end{biblist}", 1)[0]
    cursor, entries = 0, []
    while (cursor := skip_space(body, cursor)) < len(body):
        if body[cursor:cursor + 1] == "%":
            cursor = body.find("\n", cursor)
            if cursor < 0:
                break
            continue
        name, cursor = command(body, cursor)
        if name != "bib":
            raise ParseError("Unsupported AMSRefs item command: " + name)
        start = cursor - len(r"\bib")
        key, cursor = group(body, cursor)
        typ, cursor = group(body, cursor)
        raw_fields, cursor = group(body, cursor)
        if not re.fullmatch(r"[^\s{}\\,]+", key) or not re.fullmatch(r"[A-Za-z]+", typ):
            raise ParseError("Unsafe AMSRefs entry identity")
        fields, authors, offset = {}, [], 0
        while (offset := skip_space(raw_fields, offset)) < len(raw_fields):
            if raw_fields[offset] == ",":
                offset += 1
                continue
            match = re.match(r"([A-Za-z][\w-]*)\s*=", raw_fields[offset:])
            if not match:
                raise ParseError("Malformed AMSRefs field: " + key)
            field = match[1].lower()
            value, offset = _value(raw_fields, offset + match.end(), {})
            if field == "author":
                authors.append(value)
            elif field in fields and fields[field] != value:
                raise ParseError("Conflicting AMSRefs field: " + key + "." + field)
            else:
                fields[field] = value
        if authors:
            fields["author"] = " and ".join(authors)
        date = fields.get("date", "")
        if re.fullmatch(r"\d{4}", date):
            fields["year"] = date
        elif re.fullmatch(r"\d{4}/\d{2}", date):
            fields["year"] = date[:4]
        entry = _entry(key, typ.lower(), fields, body[start:cursor], "amsrefs_bbl")
        if authors:
            entry["authors"] = [plain(author) for author in authors]
        entry["field_methods"] = {field: "amsrefs_label" for field in fields}
        entries.append(entry)
    if not entries or not re.search(r"\\end\s*\{biblist\}\s*\\end\s*\{bibdiv\}", text):
        raise ParseError("Incomplete AMSRefs bibliography")
    return entries


def enrich_entry(entry: dict) -> dict:
    """Extract explicitly labelled fields while retaining original raw evidence."""
    from .bibliography import plain

    raw = entry["raw"]
    methods = dict(entry.get("field_methods", {}))
    source_values = {}
    if entry["provider"] == "bibitem_conservative":
        defaults = {"title": "rendered_newblock", "authors": "rendered_newblock",
                    "year": "rendered_year_regex", "doi": "doi_regex", "url": "url_macro"}
    elif entry["provider"] == "biblatex_bbl":
        defaults = {field: "biblatex_label" for field in
                    ("title", "authors", "year", "doi", "journal", "url")}
    else:
        defaults = {}
    for field, method in defaults.items():
        if entry.get(field) is not None:
            methods.setdefault(field, method)
    for field in ("title", "year", "journal", "doi", "url"):
        if entry.get(field) is not None and re.search(r"\\bibinfo\s*\{" + field + r"\}", raw):
            methods[field] = "labelled_macro"
    if entry.get("authors") is not None and re.search(r"\\bibinfo\s*\{author\}", raw):
        methods["authors"] = "labelled_macro"

    if r"\APAC" in raw:
        # APA-generated BBLs place display commands between newblocks. Those
        # blocks are not author/title fields in the ordinary BibTeX style.
        entry["title"] = None
        entry["authors"] = None
        titles = _command_values(raw, "APACrefatitle")
        if titles:
            entry["title"] = plain(titles[0])
            methods["title"] = "labelled_macro"
            source_values["title"] = titles[0]
        years = _command_values(raw, "APACrefYearMonthDay")
        if years and re.fullmatch(r"\d{4}", years[0].strip()):
            entry["year"] = int(years[0].strip())
            methods["year"] = "labelled_macro"
            source_values["year"] = years[0]
        journals = _command_values(raw, "APACjournalVolNumPages")
        if journals:
            entry["journal"] = plain(journals[0])
            methods["journal"] = "labelled_macro"
            source_values["journal"] = journals[0]

    def assign(field: str, value: str | None, method: str) -> None:
        if not value:
            return
        original = value
        value = plain(value)
        if field == "year":
            value = int(value) if value and re.fullmatch(r"\d{4}", value) else None
        if value is None:
            return
        if entry.get(field) is None or method in {"labelled_macro", "publisher_macro"}:
            entry[field] = value
            methods[field] = method
            source_values[field] = original

    title = _command_values(raw, "showarticletitle") or _command_values(raw, "batitle")
    if title:
        assign("title", title[0], "publisher_macro" if r"\batitle" in raw else "labelled_macro")
    for source, target in (("bjtitle", "journal"), ("byear", "year"),
                           ("doiurl", "doi"), ("burl", "url")):
        values = _command_values(raw, source)
        if values:
            assign(target, values[0], "publisher_macro")
    people = _command_values(raw, "bauthor")
    if people:
        names = []
        for person in people:
            surname = _command_values(person, "bsnm")
            initials = _command_values(person, "binits")
            if not surname:
                break
            names.append(" ".join(filter(None, [plain(initials[0]) if initials else None,
                                                 plain(surname[0])])))
        if len(names) == len(people):
            entry["authors"] = names
            methods["authors"] = "publisher_macro"
            source_values["authors"] = people
    # bibinfo has two arguments; collect only the explicitly tagged persons.
    if r"\bibinfo{person}" in raw:
        names = []
        for match in re.finditer(r"\\bibinfo\s*\{person\}", raw):
            name, _ = group(raw, skip_space(raw, match.end()))
            names.append(plain(name))
        if names and all(names):
            entry["authors"] = names
            methods["authors"] = "labelled_macro"
            source_values["authors"] = names
    if entry["provider"] == "bibitem_conservative" and methods.get("authors") in {None, "rendered_newblock"}:
        block = re.split(r"\\newblock\b", raw, maxsplit=1)[0]
        truncated_list = bool(re.search(r",\s*et~?al\.?\s*$", block, re.I))
        if (";" in block or ", and " in block or truncated_list) and not re.search(r"\\[A-Za-z]+", block):
            block = re.sub(r"\s+(?:18|19|20)\d{2}\.?\s*$", "", block).strip()
            if ";" in block:
                parts = re.split(r"\s*;\s*|\s+and\s+", block)
            else:
                parts = [part.rstrip(".") for part in re.split(r",\s*|\s+and\s+", block)]
            if truncated_list:
                parts = parts[:-1]
            names = [plain(re.sub(r"^and\s+", "", part.strip()))
                     for part in parts]
            if len(names) >= 2 and all(names):
                entry["authors"] = names
                methods["authors"] = "rendered_truncated_list" if truncated_list else "rendered_list"
                source_values["authors"] = block
        if entry.get("authors") is None:
            dated = re.fullmatch(r"(.+?)\s*\(((?:18|19|20)\d{2})\)\.?,?\s*", plain(block) or "")
            if dated and re.fullmatch(r"[A-Z][\w'-]+,\s*(?:[A-Z]\.\s*){1,3}", dated[1]):
                entry["authors"] = [dated[1].strip()]
                methods["authors"] = "rendered_author_year"
                source_values["authors"] = block
    if (entry["provider"] == "bibitem_conservative"
            and entry.get("authors") is None and entry.get("title") is None):
        first = re.split(r"\\newblock\b", raw, maxsplit=1)[0]
        parts = _comma_parts(first)
        author_parts = []
        for part in parts:
            if not _initial_author(part):
                break
            author_parts.append(part)
        if 1 < len(author_parts) < len(parts) - 1:
            title_raw = ", ".join(parts[len(author_parts):-1])
            venue_raw = parts[-1]
            venue = re.match(r"(.+?)\s+\d+(?:\s*\([^)]*\))?.*?(?:18|19|20)\d{2}",
                             plain(venue_raw) or "")
            if (title_raw and venue and not re.match(r"(?i)(?:and\b|et\s*al\b)", plain(title_raw) or "")
                    and not re.search(r"\\|``|''|[“”]", title_raw + venue_raw)):
                entry["authors"] = [plain(part) for part in author_parts]
                entry["title"] = plain(title_raw.rstrip(" ."))
                journal = plain(venue[1])
                if journal and not re.match(r"(?i)^in(?:\s|:|$)", journal):
                    entry["journal"] = journal
                    methods["journal"] = "rendered_comma_pattern"
                    source_values["journal"] = venue_raw
                for field in ("authors", "title"):
                    methods[field] = "rendered_comma_pattern"
                source_values.update(authors=author_parts, title=title_raw)
    entry["field_methods"] = methods
    entry["authors_truncated"] = entry["provider"] == "bibitem_conservative" and bool(re.search(
        r"\\(?:emph|textit)\s*\{et\s*~?\s*al\.?\}|\bet\s*~?\s*al\.?,?",
        raw[:raw.find(r"\newblock") if r"\newblock" in raw else len(raw)], re.I))
    entry["field_evidence"] = {
        field: {"value": entry.get(field), "method": methods.get(field, "missing"),
                "source": source_values.get(field, entry["fields"].get(
                    "author" if field == "authors" else
                    "journaltitle" if field == "journal" and entry["provider"] == "biblatex_bbl" else field)),
                "complete": (not entry["authors_truncated"] if field == "authors"
                             else entry.get(field) is not None)}
        for field in ("title", "authors", "year", "doi", "journal", "url")
    }
    return entry
