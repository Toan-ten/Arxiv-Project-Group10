"""Offline BBL conversion with immutable source evidence and restartable output."""
from __future__ import annotations

import csv
import json
import re
import sys
import tarfile
import time
import uuid
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath

from .bibliography import parse_bbl, parse_bibtex, unify
from .latex import ParseError
from .runner import digest, encoded, write_new


ARTIFACT = re.compile(r"^(\d{4})-(\d{5})/v([1-9]\d*)/source_bbl/(.+\.bbl)$")
MAX_BBL_BYTES = 64 * 1024 * 1024


def _decode_bbl(data: bytes) -> tuple[str, dict | None]:
    """Decode UTF-8 while recording isolated malformed auxiliary bytes."""
    try:
        return data.decode("utf-8-sig"), None
    except UnicodeDecodeError as exc:
        text = data.decode("utf-8-sig", errors="replace")
        bom_bytes = 3 if data.startswith(b"\xef\xbb\xbf") else 0
        return text, {
            "encoding": "utf-8-with-replacement",
            "replacement_characters": text.count("\ufffd"),
            "first_invalid_byte": exc.start + bom_bytes,
            "decoder_reason": exc.reason,
        }


def _replacement_in(value) -> bool:
    if isinstance(value, str):
        return "\ufffd" in value
    if isinstance(value, (list, tuple)):
        return any(_replacement_in(item) for item in value)
    return False


def _source_files(inputs: list[Path]) -> list[tuple[str, str, bytes]]:
    """Read BBL members from archives or extracted directories without links."""
    found: list[tuple[str, str, bytes]] = []
    identities = set()
    for source in inputs:
        source = source.resolve()
        if source.is_dir():
            files = sorted(source.rglob("*.bbl"))
            records = []
            for path in files:
                if path.is_symlink() or path.stat().st_size > MAX_BBL_BYTES:
                    raise ParseError("Unsafe or oversized BBL input: " + str(path))
                records.append((path.relative_to(source).as_posix(), path.read_bytes()))
        elif tarfile.is_tarfile(source):
            records = []
            with tarfile.open(source, "r:*") as archive:
                for member in archive:
                    if not member.name.endswith(".bbl"):
                        continue
                    if (not member.isfile() or member.size > MAX_BBL_BYTES
                            or PurePosixPath(member.name).is_absolute()
                            or ".." in PurePosixPath(member.name).parts):
                        raise ParseError("Unsafe or oversized BBL archive member: " + member.name)
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise ParseError("Unreadable BBL archive member: " + member.name)
                    data = stream.read(MAX_BBL_BYTES + 1)
                    if len(data) != member.size:
                        raise ParseError("BBL archive member size mismatch: " + member.name)
                    records.append((member.name, data))
        else:
            raise ParseError("BBL input must be an archive or directory: " + str(source))
        for name, data in records:
            if not ARTIFACT.fullmatch(name):
                raise ParseError("Unrecognized BBL artifact path: " + name)
            identity = (source.name, name)
            if identity in identities:
                raise ParseError("Duplicate BBL input: " + name)
            identities.add(identity)
            found.append((source.name, name, data))
    if not found:
        raise ParseError("No BBL artifacts found")
    return sorted(found, key=lambda row: row[:2])


def _verify_inventory(records: list[tuple[str, str, bytes]], inventory: Path) -> None:
    with inventory.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    expected = {(row["archive"], row["path"]): row["sha256"] for row in rows}
    if (len(expected) != len(rows) or any(not re.fullmatch(r"[0-9a-f]{64}", value)
                                         for value in expected.values())):
        raise ParseError("Invalid or duplicated BBL inventory rows")
    actual = {(archive, path): digest(data) for archive, path, data in records}
    if actual != expected:
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        changed = sorted(key for key in set(actual) & set(expected)
                         if actual[key] != expected[key])
        raise ParseError(
            f"BBL inventory mismatch: missing={missing[:3]}, "
            f"extra={extra[:3]}, changed={changed[:3]}")


def render_bibtex(entries: list[dict]) -> str:
    """Render supported fields as parseable BibTeX without inventing metadata."""
    keys = [entry["key"] for entry in entries]
    if len(keys) != len(set(keys)):
        raise ParseError("Duplicate citation keys require separate BibTeX outputs")
    output = []
    for entry in entries:
        key = entry["key"]
        typ = entry["entry_type"]
        if not re.fullmatch(r"[^\s{}\\,]+", key) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", typ):
            raise ParseError("Unsafe BibTeX entry identity: " + key)
        fields = {}
        for field in ("title", "year", "doi", "journal", "url"):
            value = entry.get(field)
            if value is not None:
                fields[field] = str(value)
        if entry.get("authors"):
            fields["author"] = " and ".join(
                "{" + name + "}" if re.search(r"\band\b", name, re.I) else name
                for name in entry["authors"])
        lines = [f"@{typ}{{{key},"]
        for field, value in fields.items():
            if value.count("{") != value.count("}") or "\x00" in value:
                raise ParseError("Unbalanced/unsafe BibTeX field: " + key + "." + field)
            lines.append(f"  {field} = {{{value}}},")
        output.append("\n".join(lines + ["}"]))
    result = "\n\n".join(output) + ("\n" if output else "")
    parsed = parse_bibtex(result)
    if [entry["key"] for entry in parsed] != [entry["key"] for entry in entries]:
        raise ParseError("BibTeX round-trip lost an entry")
    if any(len(parsed_entry["authors"] or []) != len(original["authors"] or [])
           for parsed_entry, original in zip(parsed, entries)):
        raise ParseError("BibTeX round-trip changed author count")
    return result


def _bibtex_output_groups(file_path: Path, entries: list[dict]) -> list[tuple[Path, list[dict]]]:
    if not entries:
        return [(file_path.with_suffix(".bib"), [])]
    groups: dict[str | None, list[dict]] = defaultdict(list)
    for entry in entries:
        metadata = entry.get("biblatex_metadata", {})
        groups[metadata.get("refsection_id")].append(entry)
    if len(groups) == 1:
        return [(file_path.with_suffix(".bib"), entries)]
    if None in groups or any(scope is None or not str(scope).isdigit() for scope in groups):
        raise ParseError("Cannot separate BibLaTeX entries with unscoped citation keys")
    return [
        (file_path.with_name(f"{file_path.stem}.refsection-{scope}.bib"), groups[scope])
        for scope in sorted(groups, key=lambda value: int(value))
    ]


def _cached_file(path: Path, expected: dict, root: Path) -> tuple[dict, list[dict]] | None:
    if not path.exists():
        return None
    envelope = json.loads(path.read_text(encoding="utf-8"))
    payload = envelope["payload"]
    if digest(encoded(payload)) != envelope["sha256"]:
        raise ParseError("Corrupt BBL checkpoint: " + str(path))
    record = payload["record"]
    if any(record.get(key) != expected[key] for key in ("archive", "path", "arxiv_id", "version", "sha256")):
        raise ParseError("BBL checkpoint identity mismatch: " + str(path))
    entries = payload.get("entries", [])
    if record["status"] == "success":
        outputs = payload.get("bib_outputs")
        if outputs is None:
            outputs = [{"path": path.with_suffix(".bib").relative_to(root).as_posix(),
                        "sha256": payload.get("bib_sha256")}]
        total_entries = 0
        for output in outputs:
            relative = PurePosixPath(output["path"])
            if relative.is_absolute() or ".." in relative.parts:
                raise ParseError("Unsafe BBL BibTeX checkpoint path")
            bib_path = root.joinpath(*relative.parts)
            if (not bib_path.is_file()
                    or digest(bib_path.read_bytes()) != output["sha256"]):
                raise ParseError("Corrupt BBL BibTeX output: " + str(bib_path))
            total_entries += len(parse_bibtex(bib_path.read_text(encoding="utf-8")))
        if total_entries != len(entries):
            raise ParseError("BBL BibTeX output entry count changed: " + str(path))
    return record, entries


def run_bbl_pipeline(inputs: list[Path], output: Path, *, inventory: Path | None = None) -> dict:
    """Convert all BBL artifacts, preserving per-file failures and paper evidence."""
    started = time.perf_counter()
    inputs = [Path(path).resolve() for path in inputs]
    output = Path(output).resolve()
    code_root = Path(__file__).resolve().parents[2]
    if output.is_relative_to(code_root):
        raise ParseError("Output must be outside the source repository")
    if any(output == source or output.is_relative_to(source) for source in inputs):
        raise ParseError("Output must be outside BBL input")
    records = _source_files(inputs)
    if inventory is not None:
        _verify_inventory(records, Path(inventory))
    code_hash = digest(b"".join(path.read_bytes() for path in sorted(Path(__file__).parent.glob("*.py"))))
    definition = {"inputs": [(archive, name, digest(data)) for archive, name, data in records],
                  "parser_sha256": code_hash, "format": "m2-bbl-conversion-1"}
    root = output / ("run-" + digest(encoded(definition))[:16])
    root.mkdir(parents=True, exist_ok=True)
    write_new(root / "definition.json", encoded(definition))
    files = []
    papers: dict[str, dict[int, list[tuple[dict, list[dict]]]]] = defaultdict(lambda: defaultdict(list))
    counts = Counter()
    cached_files = 0
    for archive, name, data in records:
        match = ARTIFACT.fullmatch(name)
        assert match is not None
        aid = match[1] + "." + match[2]
        version = int(match[3])
        file_path = root / "files" / name
        record = {"archive": archive, "path": name, "arxiv_id": aid, "version": version,
                  "sha256": digest(data), "status": "parse_error", "entry_count": 0,
                  "candidate_markers": len(re.findall(
                      rb"(?m)^[ \t]*\\(?:bibitem|harvarditem|bib|entry)\b", data))}
        cached = _cached_file(file_path.with_suffix(".json"), record, root)
        if cached is not None:
            record, entries = cached
            cached_files += 1
            if record["status"] == "success":
                papers[aid][version].append((record, entries))
                counts["entries"] += len(entries)
            counts[record["status"]] += 1
            files.append(record)
            continue
        try:
            source, decode_warning = _decode_bbl(data)
            if decode_warning:
                record["decode_warning"] = decode_warning
            entries = parse_bbl(source, enriched=True)
            if decode_warning and any(
                    _replacement_in(entry.get(field))
                    for entry in entries
                    for field in ("title", "authors", "year", "doi", "journal", "url")):
                raise ParseError("Invalid UTF-8 intersects an extracted bibliography field")
            if not entries and record["candidate_markers"]:
                raise ParseError("No bibliography entries")
            source_entry_count = sum(
                entry.get("biblatex_metadata", {}).get("source_occurrences", 1)
                for entry in entries)
            if record["candidate_markers"] and source_entry_count != record["candidate_markers"]:
                raise ParseError("BBL entry count differs from source markers")
            output_groups = _bibtex_output_groups(file_path, entries)
            record.update(status="success", entry_count=len(entries),
                          source_entry_count=source_entry_count,
                          deduplicated_entry_occurrences=source_entry_count - len(entries),
                          biblatex_refsections=sorted({
                              entry.get("biblatex_metadata", {}).get("refsection_id")
                              for entry in entries
                              if entry.get("biblatex_metadata", {}).get("refsection_id") is not None
                          }, key=int),
                          provider_counts=dict(Counter(entry["provider"] for entry in entries)),
                          missing_fields={field: sum(entry.get(field) is None for entry in entries)
                                          for field in ("title", "authors", "year", "doi", "journal", "url")})
            bib_outputs = []
            for bib_path, scoped_entries in output_groups:
                bib_bytes = render_bibtex(scoped_entries).encode("utf-8")
                write_new(bib_path, bib_bytes)
                bib_outputs.append({"path": bib_path.relative_to(root).as_posix(),
                                    "sha256": digest(bib_bytes),
                                    "entry_count": len(scoped_entries)})
            record["bibtex_paths"] = [output["path"] for output in bib_outputs]
            payload = {"record": record, "entries": entries, "bib_outputs": bib_outputs}
            if len(bib_outputs) == 1:
                payload["bib_sha256"] = bib_outputs[0]["sha256"]
            envelope = {"payload": payload, "sha256": digest(encoded(payload))}
            write_new(file_path.with_suffix(".json"), encoded(envelope))
            papers[aid][version].append((record, entries))
            counts["entries"] += len(entries)
        except (ParseError, UnicodeError) as exc:
            record["error"] = str(exc)
            payload = {"record": record}
            envelope = {"payload": payload, "sha256": digest(encoded(payload))}
            write_new(file_path.with_suffix(".json"), encoded(envelope))
        counts[record["status"]] += 1
        files.append(record)
    publication_results = []
    for aid in sorted({file["arxiv_id"] for file in files}):
        versions = papers.get(aid, {})
        selected = {}
        problems = []
        for version, sources in sorted(versions.items()):
            distinct = {source[0]["sha256"] for source in sources}
            if len(distinct) != 1:
                problems.append(f"v{version}: multiple distinct BBL files; compilation path not verified")
                continue
            refsection_ids = {
                section
                for record, _ in sources
                for section in record.get("biblatex_refsections", [])
            }
            if len(refsection_ids) > 1:
                problems.append(
                    f"v{version}: multiple BibLaTeX refsections; per-section BibTeX is retained "
                    "without cross-section merging")
                continue
            if refsection_ids and refsection_ids != {"0"}:
                problems.append(
                    f"v{version}: non-global BibLaTeX refsection; BibTeX is retained "
                    "without publication-level merging")
                continue
            selected[version] = sources[0][1]
        if any(file["status"] != "success" for file in files if file["arxiv_id"] == aid):
            problems.append("One or more BBL files failed parsing")
        publication = {"arxiv_id": aid, "status": "requires_review" if problems else "success",
                       "problems": problems, "versions": sorted(versions), "unified_entry_count": None}
        if not problems:
            try:
                rows, aliases, conflicts = unify(aid, selected)
                by_key = {entry["key"]: entry for entries in selected.values() for entry in entries}
                unified = []
                for row in rows:
                    entry = dict(by_key.get(row["citation_key"], {}))
                    entry.update(key=row["citation_key"], entry_type=row["entry_type"])
                    entry.update({field: row[field] for field in
                                  ("title", "authors", "year", "doi", "journal", "url")})
                    unified.append(entry)
                write_new(root / "papers" / aid / "bibliography.bib", render_bibtex(unified).encode("utf-8"))
                write_new(root / "papers" / aid / "evidence.json",
                          encoded({"bibtex_table": rows, "citation_aliases": aliases,
                                   "conflicts": conflicts,
                                   "source_files": [source[0] for values in versions.values()
                                                    for source in values]}))
                publication["unified_entry_count"] = len(rows)
                publication["conflict_count"] = len(conflicts)
                counts["unified_entries"] += len(rows)
                counts["citation_mappings"] += sum(len(mapping) for mapping in aliases.values())
                counts["field_conflicts"] += len(conflicts)
            except ParseError as exc:
                publication.update(status="requires_review", problems=[str(exc)])
        publication_results.append(publication)
        counts["publications_" + publication["status"]] += 1
    report = {"run_root": str(root), "files": files, "publications": publication_results,
              "counts": dict(counts), "wall_seconds": time.perf_counter() - started,
              "input_files": len(records), "parser_sha256": code_hash,
              "cached_files": cached_files}
    write_new(root / "report.json", encoded({k: v for k, v in report.items()
                                             if k not in {"wall_seconds", "cached_files"}}))
    peak_rss_kib = None
    if sys.platform == "linux":
        import resource
        peak_rss_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    report["peak_rss_kib_linux"] = peak_rss_kib
    report["output_bytes"] = sum(path.stat().st_size for path in root.rglob("*")
                                 if path.is_file() and "attempts" not in path.relative_to(root).parts)
    write_new(root / "attempts" / (uuid.uuid4().hex + ".json"), encoded({
        "wall_seconds": report["wall_seconds"], "cached_files": cached_files,
        "peak_rss_kib_linux": peak_rss_kib, "output_bytes": report["output_bytes"],
        "input_files": len(records), "parser_sha256": code_hash,
    }))
    return report
