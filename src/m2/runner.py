"""Offline M1-corpus -> M2 tables, with isolated immutable run directories."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import time
import uuid
from collections import Counter
from datetime import datetime, timezone

from .latex import ParseError, safe_path
from .parser import parse_versions


TABLE_COLUMNS = {
    "elements": ("element_id", "arxiv_id", "kind", "content"),
    "hierarchy": ("arxiv_id", "version", "element_id", "parent_id"),
    "bibtex": ("arxiv_id", "citation_key", "entry_type", "title", "authors", "year", "doi", "journal", "url"),
}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False).encode("utf-8")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def write_new(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != data:
            raise ParseError(f"Refusing to overwrite existing output: {path}")
        return
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def source_fingerprint(root):
    hashes = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ParseError(f"Symlink in input corpus: {path}")
        if path.is_file():
            hashes[path.relative_to(root).as_posix()] = digest(path.read_bytes())
    return digest(encoded(hashes)), len(hashes)


def selection(corpus, paper=None, version=None, limit_versions=None, selected_versions=None):
    selected = []
    for directory in sorted(corpus.iterdir()):
        if not directory.is_dir():
            continue
        manifest_path = directory / "manifest.json"
        if not manifest_path.is_file():
            raise ParseError(f"Missing M1 manifest: {directory.name}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
        aid, owner = manifest["arxiv_id"], manifest["mssv"]
        if (metadata.get("arxiv_id") != aid or metadata.get("mssv") != owner
                or manifest.get("contract_version") != 2 or metadata.get("contract_version") != 2):
            raise ParseError(f"Input metadata/manifest identity mismatch: {directory.name}")
        if paper is not None and aid != paper:
            continue
        versions = manifest.get("versions")
        if not isinstance(versions, dict):
            raise ParseError(f"Invalid M1 versions: {aid}")
        for name, record in sorted(versions.items(), key=lambda x: int(x[0][1:]) if re.fullmatch(r"v[1-9]\d*", x[0]) else -1):
            if not re.fullmatch(r"v[1-9]\d*", name) or not isinstance(record, dict):
                raise ParseError(f"Invalid M1 version record: {aid}")
            if version is not None and name != version:
                continue
            selected.append((aid, owner, int(name[1:]), directory, record))
    if selected_versions is not None:
        if paper is not None or version is not None or limit_versions is not None:
            raise ParseError("--selection cannot be combined with other selection flags")
        if not isinstance(selected_versions, list) or not selected_versions:
            raise ParseError("Selection must be a nonempty list")
        requested = []
        for item in selected_versions:
            if not isinstance(item, dict) or set(item) != {"arxiv_id", "version"}:
                raise ParseError("Each selection item needs arxiv_id and version")
            aid, ver = item["arxiv_id"], item["version"]
            if not isinstance(aid, str) or not isinstance(ver, str) or not re.fullmatch(r"v[1-9]\d*", ver):
                raise ParseError("Invalid selection ID/version")
            requested.append((aid, int(ver[1:])))
        if len(set(requested)) != len(requested):
            raise ParseError("Duplicate requested ID/version")
        available = {(a, ver): row for row in selected for a, _, ver, _, _ in [row]}
        if any(key not in available for key in requested):
            raise ParseError("Requested ID/version is absent from M1 manifest")
        selected = [available[key] for key in requested]
    if limit_versions is not None:
        selected = selected[:limit_versions]
    identities = [(a, v) for a, _, v, _, _ in selected]
    if len(identities) != len(set(identities)):
        raise ParseError("Duplicate arXiv ID/version in corpus selection")
    if not selected:
        raise ParseError("No matching versions; check --paper / --version")
    return selected


def inspect_source(directory, version, record):
    root = directory / f"v{version}"
    status = record.get("status")
    if status == "no_tex_source":
        if root.exists() and list(root.rglob("*.tex")):
            raise ParseError("M1 no_tex_source conflicts with TeX on disk")
        return "empty_no_tex"
    if status != "success":
        return "source_unavailable"
    if not root.is_dir():
        raise ParseError("M1 success has no version directory")
    checksums, files = record.get("file_checksums"), record.get("files")
    if not isinstance(checksums, dict) or not checksums or not isinstance(files, list) or set(checksums) != set(files):
        raise ParseError("Missing/inconsistent M1 source checksum evidence")
    actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
    if actual != set(files):
        raise ParseError("M1 files differ from manifest")
    for name in files:
        path = safe_path(root, name)
        if not path.is_file() or digest(path.read_bytes()) != checksums[name]:
            raise ParseError(f"M1 source checksum mismatch: {name}")
    if not list(root.rglob("*.tex")):
        raise ParseError("M1 success has no TeX")
    return "ready"


def validate_tables(tables):
    for name, columns in TABLE_COLUMNS.items():
        for row in tables[name]:
            if set(row) != set(columns):
                raise ParseError(f"Wrong official columns in {name}")
            if not isinstance(row["arxiv_id"], str) or not row["arxiv_id"]:
                raise ParseError("Invalid output paper identity")
    nodes = {r["element_id"]: r for r in tables["elements"]}
    if len(nodes) != len(tables["elements"]):
        raise ParseError("Duplicate element IDs")
    seen = set()
    parents = {}
    for edge in tables["hierarchy"]:
        if type(edge["version"]) is not int or edge["version"] <= 0:
            raise ParseError("Invalid hierarchy version")
        for key in ("element_id", "parent_id"):
            if edge[key] not in nodes or nodes[edge[key]]["arxiv_id"] != edge["arxiv_id"]:
                raise ParseError("Dangling/cross-publication hierarchy edge")
        key = (edge["arxiv_id"], edge["version"], edge["element_id"])
        if key in seen:
            raise ParseError("Duplicate/multiple-parent edge")
        seen.add(key)
        parents[key] = edge["parent_id"]
    for aid, version, node in parents:
        visiting = set()
        while (aid, version, node) in parents:
            if node in visiting:
                raise ParseError("Hierarchy cycle")
            visiting.add(node)
            node = parents[aid, version, node]
        if nodes[node]["kind"] != "document":
            raise ParseError("Hierarchy does not reach a document root")
    keys = [(r["arxiv_id"], r["citation_key"]) for r in tables["bibtex"]]
    if len(set(keys)) != len(keys):
        raise ParseError("Duplicate citation keys")
    for row in tables["bibtex"]:
        if row["authors"] is not None and (not isinstance(row["authors"], list)
                or not all(isinstance(a, str) for a in row["authors"])):
            raise ParseError("Invalid BibTeX authors")
        if row["year"] is not None and type(row["year"]) is not int:
            raise ParseError("Invalid BibTeX year")


def export_parquet(root, tables):
    # Explicit flat schemas also work for empty tables and all-null columns.
    import pyarrow as pa
    import pyarrow.parquet as pq
    string, dictionary = pa.string(), pa.dictionary(pa.int32(), pa.string())
    schemas = {
        "elements": pa.schema([pa.field("element_id", string, False), pa.field("arxiv_id", string, False),
                               pa.field("kind", dictionary, False), pa.field("content", string)]),
        "hierarchy": pa.schema([pa.field("arxiv_id", string, False), pa.field("version", pa.int64(), False),
                                pa.field("element_id", string, False), pa.field("parent_id", string, False)]),
        "bibtex": pa.schema([pa.field("arxiv_id", string, False), pa.field("citation_key", string, False),
                             pa.field("entry_type", dictionary, False), pa.field("title", string),
                             pa.field("authors", pa.list_(string)), pa.field("year", pa.int64()),
                             pa.field("doi", string), pa.field("journal", string), pa.field("url", string)]),
    }
    checksums = {}
    for name in TABLE_COLUMNS:
        target = root / "data" / name / "part-00000.parquet"
        table = pa.Table.from_pylist(tables[name], schema=schemas[name])
        if target.exists():
            if pq.read_table(target).to_pylist() != tables[name] or pq.read_schema(target) != schemas[name]:
                raise ParseError(f"Existing Parquet differs from verified rows: {name}")
        else:
            buffer = pa.BufferOutputStream()
            pq.write_table(table, buffer, compression="zstd")
            write_new(target, buffer.getvalue().to_pybytes())
        checksums[target.relative_to(root).as_posix()] = digest(target.read_bytes())
    return checksums


def run(corpus, output, *, paper=None, version=None, limit_versions=None, selected_versions=None):
    started = time.perf_counter()
    started_at = datetime.now(timezone.utc).isoformat()
    corpus, output = Path(corpus).resolve(), Path(output).resolve()
    code_root = Path(__file__).resolve().parents[2]
    if (output.is_relative_to(corpus) or corpus.is_relative_to(output)
            or output.is_relative_to(code_root)):
        raise ParseError("Output must be outside corpus and code worktree")
    # Also protect the main repository containing an ignored input corpus.
    for parent in corpus.parents:
        if (parent / ".git").exists() and output.is_relative_to(parent):
            raise ParseError("Output must be outside the source Git repository")
    if version is not None and not re.fullmatch(r"v[1-9]\d*", version):
        raise ParseError("--version must be v1, v2, ...")
    if limit_versions is not None and (type(limit_versions) is not int or limit_versions <= 0):
        raise ParseError("--limit-versions must be positive")
    cohort = selection(corpus, paper, version, limit_versions, selected_versions)
    before, file_count = source_fingerprint(corpus)
    code_hash = digest(b"".join(p.read_bytes() for p in sorted(Path(__file__).parent.glob("*.py"))))
    definition = dict(input_fingerprint=before, parser_fingerprint=code_hash,
                      selection=[(a, owner, v) for a, owner, v, _, _ in cohort])
    run_root = output / ("run-" + digest(encoded(definition))[:16])
    run_root.mkdir(parents=True, exist_ok=True)
    lock = run_root / ".writer.lock"
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ParseError("M2 writer lock exists; review previous interrupted run before retry") from exc
    os.close(fd)
    try:
        write_new(run_root / "definition.json", encoded(definition))
        grouped = {}
        for row in cohort:
            grouped.setdefault(row[0], []).append(row)
        all_tables = {name: [] for name in TABLE_COLUMNS}
        version_records, cached = [], 0
        for aid, selected in grouped.items():
            result_path = run_root / "papers" / selected[0][3].name / "result.json"
            if result_path.exists():
                envelope = json.loads(result_path.read_text(encoding="utf-8"))
                result = envelope["payload"]
                if digest(encoded(result)) != envelope["sha256"]:
                    raise ParseError(f"Corrupt M2 paper checkpoint: {aid}")
                cached += 1
            else:
                ready, records = [], []
                for _, owner, ver, directory, source_record in selected:
                    record = dict(arxiv_id=aid, mssv=owner, version=ver,
                                  m1_source_status=source_record.get("status"), status=None, error=None)
                    try:
                        record["status"] = inspect_source(directory, ver, source_record)
                        if record["status"] == "ready":
                            ready.append((ver, directory / f"v{ver}"))
                    except (ParseError, OSError) as exc:
                        record.update(status="input_error", error=str(exc))
                    records.append(record)
                result = parse_versions(aid, ready)
                by_version = {r["version"]: r for r in result.pop("versions")}
                for rec in records:
                    if rec["version"] in by_version:
                        rec.update(by_version[rec["version"]])
                result["versions"] = records
                validate_tables(result)
                write_new(result_path, encoded(dict(payload=result, sha256=digest(encoded(result)))))
            validate_tables(result)
            for table in TABLE_COLUMNS:
                all_tables[table].extend(result[table])
            version_records.extend(result["versions"])
        validate_tables(all_tables)
        checksums = export_parquet(run_root, all_tables)
        write_new(run_root / "output_checksums.json", encoded(checksums))
        after, _ = source_fingerprint(corpus)
        if after != before:
            raise ParseError("Input changed during M2 run; results must not be certified")
        provenance = dict(python=platform.python_version(), platform=platform.platform(),
                          measurement_environment="local", corpus_root=str(corpus), input_files=file_count,
                          input_fingerprint=before, parser_fingerprint=code_hash)
        try:
            git = ["git", "-c", "safe.directory=" + code_root.as_posix(), "-C", str(code_root)]
            provenance["git_commit"] = subprocess.check_output(git + ["rev-parse", "HEAD"], text=True).strip()
            provenance["git_dirty"] = bool(subprocess.check_output(git + ["status", "--porcelain"], text=True).strip())
        except (OSError, subprocess.SubprocessError):
            provenance.update(git_commit=None, git_dirty=None)
        statuses = Counter(r["status"] for r in version_records)
        attempt = dict(started_at=started_at, ended_at=datetime.now(timezone.utc).isoformat(),
                       wall_seconds=time.perf_counter() - started,
                       timing_scope="read/fingerprint input, parse, validate, write and reread Parquet; cached reruns recorded separately",
                       papers=len(grouped), versions=len(version_records), statuses=dict(statuses),
                       cached_papers=cached, tables={n: len(rows) for n, rows in all_tables.items()},
                       all_versions_processed=all(r["status"] in {"success", "empty_no_tex"} for r in version_records),
                       versions_detail=version_records, provenance=provenance, run_root=str(run_root))
        write_new(run_root / "attempts" / (uuid.uuid4().hex + ".json"), encoded(attempt))
        return attempt
    finally:
        lock.unlink()
