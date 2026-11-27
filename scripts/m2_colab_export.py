"""Colab export orchestration; the committed M2 parser is never modified.

The shared notebook loads this helper from its committed release.
The parser is pinned separately. No network/upload operations exist here.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import uuid

PARSER_COMMIT = "072ee384efa2e22f743ada7786aa0f67113a1490"
TABLES = ("elements", "hierarchy", "bibtex")
# Verified release used by existing Drive campaigns; never accept an arbitrary
# helper/hash pair merely because the rest of a campaign happens to match.
LEGACY_HELPERS = {
    ("13fb8c693382694d7eaef976002c1bf2aa1e88de",
     "c7b6486ef55dad73eed537475d4cb614c33057508d31f41ee81ef98c140cebcc"),
}


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False).encode("utf8")


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def immutable(path, data):
    """Single writer only; never replace different evidence."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError(f"Existing evidence differs; preserve and review: {path}")
        return
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    if path.read_bytes() != data:
        raise ValueError(f"Write verification failed: {path}")


@contextmanager
def writer(root):
    root.mkdir(parents=True, exist_ok=True)
    lock = root / ".export-writer.lock"
    with lock.open("x", encoding="utf8") as stream:
        stream.write(json.dumps(dict(pid=os.getpid(), created_at=datetime.now(timezone.utc).isoformat())))
    try:
        yield
    finally:
        lock.unlink()  # Hard runtime termination leaves evidence; never break a stale lock automatically.


def verify_runtime(repo):
    repo = Path(repo).resolve()
    git = ["git", "-C", str(repo)]
    if subprocess.check_output(git + ["rev-parse", "HEAD"], text=True).strip() != PARSER_COMMIT:
        raise ValueError("Parser commit differs from the required export commit")
    subprocess.run(git + ["diff", "--exit-code", PARSER_COMMIT, "--", "src/m2", "requirements-m2.txt", "config/group10_id_ranges.json"],
                   check=True, stdout=subprocess.DEVNULL)
    if subprocess.check_output(git + ["ls-files", "--others", "--exclude-standard", "--", "src/m2"], text=True).strip():
        raise ValueError("Untracked parser code; refusing to mix implementations")
    sys.path.insert(0, str(repo))
    import src.m2.runner as runner
    if Path(runner.__file__).resolve() != repo / "src/m2/runner.py":
        raise ValueError("A different parser was imported; restart the Python process")
    return runner


def scope_for(config):
    members = read(Path(config["repo"]) / "config/group10_id_ranges.json")["members"]
    owner = config["student_id"]
    if not isinstance(owner, str):
        raise ValueError("STUDENT_ID must be a string from Group 10 config")
    positions = [i for i, member in enumerate(members) if member["mssv"] == owner]
    if len(positions) != 1:
        raise ValueError("Unknown STUDENT_ID")
    index = positions[0]
    first, last = index * 50 + 1, (index + 1) * 50
    start, end, size = (config[k] for k in ("start_cohort", "end_cohort", "batch_size"))
    if (any(type(n) is not int for n in (start, end, size))
            or not first <= start <= end <= last or not 1 <= size <= 50):
        raise ValueError("Cohort range must belong to STUDENT_ID; BATCH_SIZE must be 1..50")
    hf = config["hf_repo_id"]
    if not isinstance(hf, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", hf):
        raise ValueError("HF_REPO_ID must be owner/dataset")
    member = members[index]
    if member["start_month"] != member["end_month"] or member["end_id"] - member["start_id"] != 4999:
        raise ValueError("Unsupported assignment configuration; review instead of inferring ranges")
    return dict(owner=owner, start=start, end=end, batch_size=size, hf_repo_id=hf,
                assigned_first=first, member=member,
                cohorts=[f"cohort_{n:04d}_corpus" for n in range(start, end + 1)])


def expected_ids(name, scope):
    number = int(name[7:11])
    if name not in scope["cohorts"]:
        raise ValueError("Cohort is outside configured scope")
    member = scope["member"]
    prefix = member["start_month"].replace("-", "")[2:]
    first = member["start_id"] + (number - scope["assigned_first"]) * 100
    return [f"{prefix}.{i:05d}" for i in range(first, first + 100)]


def discover_item(name, scope, archive_root):
    root = Path(archive_root)
    archive = root / (name + ".tar.gz")
    manifest_path = root / (name + ".manifest.json")
    sidecar = root / (name + ".tar.gz.sha256")
    if not all(p.is_file() and not p.is_symlink() for p in (archive, manifest_path, sidecar)):
        raise ValueError(f"Missing archive/manifest/SHA sidecar: {name} in {root}")
    manifest = read(manifest_path)
    parts = sidecar.read_text(encoding="utf-8-sig").strip().split()
    if (len(parts) != 2 or parts[1].lstrip("*") != archive.name
            or not re.fullmatch(r"[0-9a-f]{64}", parts[0])
            or manifest.get("archive_sha256") != parts[0] or sha(archive) != parts[0]):
        raise ValueError(f"Archive checksum/sidecar mismatch: {name}")
    ids = expected_ids(name, scope)
    included = manifest.get("included_ids")
    if (type(manifest.get("cohort_number")) is not int or manifest["cohort_number"] != int(name[7:11])
            or manifest.get("archive_filename") != archive.name
            or manifest.get("expected_ids") != ids or manifest.get("expected_count") != len(ids)
            or not isinstance(included, list) or not all(isinstance(a, str) for a in included)
            or len(set(included)) != len(included)
            or not set(included) <= set(ids) or manifest.get("included_count") != len(included)
            or manifest.get("omitted_count") != len(ids) - len(included)):
        raise ValueError(f"M1 archive identity/coverage mismatch: {name}")
    return dict(archive=archive.name, archive_sha256=parts[0], manifest_sha256=sha(manifest_path),
                sidecar_sha256=sha(sidecar), included_ids=included,
                omitted_ids=[aid for aid in ids if aid not in included],
                phase_certification=manifest.get("phase_certification"))


def inspect_input(directory, version, record, runner):
    try:
        return runner.inspect_source(directory, version, record)
    except runner.ParseError as exc:
        # inspect_source verifies every file/hash before its case-sensitive glob.
        # Keep the pinned parser unchanged; explicitly retain this Linux failure
        # in coverage, never manufacture output or rename the immutable M1 source.
        files = record.get("files", [])
        if (str(exc) == "M1 success has no TeX" and record.get("status") == "success"
                and any(Path(n).suffix.lower() == ".tex" for n in files)
                and not any(n.endswith(".tex") for n in files)):
            return "input_error_uppercase_tex"
        raise


def materialize(name, item, archive_root, scratch, runner, scope):
    """Read only the requested, checksummed archive. Extract regular files only."""
    archive = Path(archive_root) / item["archive"]
    if not archive.is_file() or sha(archive) != item["archive_sha256"]:
        raise ValueError(f"Missing/corrupt input archive: {archive}")
    destination = Path(scratch) / name
    if destination.is_symlink():
        raise ValueError("Symlink extraction destination")
    if not destination.exists():
        Path(scratch).mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(scratch).free < item.get("uncompressed_bytes", 0) + 100 * 1024**2:
            raise ValueError("Not enough scratch disk space for this cohort")
        staging = destination.with_name(name + ".extract-" + uuid.uuid4().hex)
        staging.mkdir(parents=True)
        # Leave incomplete staging directories for diagnosis; never overwrite them.
        with tarfile.open(archive, "r:gz") as stream:
            seen, total = set(), 0
            for member in stream:
                relative = PurePosixPath(member.name)
                if (relative.is_absolute() or ".." in relative.parts or "\\" in member.name
                        or ":" in member.name or not relative.parts or relative.parts[0].startswith(".")):
                    raise ValueError(f"Unsafe archive path: {member.name}")
                if not (member.isdir() or member.isfile()) or member.name in seen:
                    raise ValueError(f"Link/special/duplicate archive member: {member.name}")
                seen.add(member.name)
                total += member.size
                if len(seen) > 500000 or total > 20 * 1024**3:
                    raise ValueError("Archive exceeds the per-cohort extraction safety budget")
                target = staging.joinpath(*relative.parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with stream.extractfile(member) as source, target.open("xb") as output:
                        shutil.copyfileobj(source, output)
        staging.rename(destination)
    fingerprint, _ = runner.source_fingerprint(destination)
    if item.get("input_fingerprint") is not None and fingerprint != item["input_fingerprint"]:
        raise ValueError(f"Extracted input differs from the verified local corpus: {name}")
    rows = runner.selection(destination)
    ids = [read(d / "manifest.json")["arxiv_id"] for d in destination.iterdir() if d.is_dir()]
    if len(ids) != len(set(ids)) or set(ids) != set(item["included_ids"]):
        raise ValueError("Extracted publication inventory differs from archive manifest")
    allowed = set(expected_ids(name, scope))
    if any(a not in allowed or owner != scope["owner"] for a, owner, _, _, _ in rows):
        raise ValueError(f"Wrong cohort identity or MSSV: {name}")
    zero = [aid for aid in ids if aid not in {r[0] for r in rows}]
    if zero:
        raise ValueError(f"Publication without version records; preserve and review: {zero}")
    item.update(input_fingerprint=fingerprint, papers=len(ids), versions=len(rows))
    for _, _, version, directory, record in rows:
        state = inspect_input(directory, version, record, runner)
        if state == "input_error_uppercase_tex":
            print(f"Source c? .TEX uppercase: {name}/{directory.name}/v{version}; gi? input_error n?u parser Linux t? ch?i", flush=True)
    return destination, rows


def arrow_schemas():
    import pyarrow as pa
    s, d = pa.string(), pa.dictionary(pa.int32(), pa.string())
    return {
        "elements": pa.schema([pa.field("element_id", s, False), pa.field("arxiv_id", s, False),
                               pa.field("kind", d, False), pa.field("content", s)]),
        "hierarchy": pa.schema([pa.field("arxiv_id", s, False), pa.field("version", pa.int64(), False),
                                pa.field("element_id", s, False), pa.field("parent_id", s, False)]),
        "bibtex": pa.schema([pa.field("arxiv_id", s, False), pa.field("citation_key", s, False),
                             pa.field("entry_type", d, False), pa.field("title", s),
                             pa.field("authors", pa.list_(s)), pa.field("year", pa.int64()),
                             pa.field("doi", s), pa.field("journal", s), pa.field("url", s)]),
    }


def audit(attempt, rows, runner):
    """Verify full checkpoint/table equality and own-version citation evidence."""
    import pyarrow.parquet as pq
    from src.m2.latex import citation_keys
    root = Path(attempt["run_root"])
    publication = Path(attempt["publication_root"])
    if publication.resolve() != root.resolve() or attempt["retryable"]:
        raise ValueError("Retryable/partial attempt is not a completed export; resume required")
    definition = read(root / "definition.json")
    provenance = attempt["provenance"]
    if (provenance["git_commit"] != PARSER_COMMIT
            or provenance["input_fingerprint"] != definition["input_fingerprint"]
            or provenance["parser_fingerprint"] != definition["parser_fingerprint"]):
        raise ValueError("Run provenance mismatch")
    expected = {(a, v): owner for a, owner, v, _, _ in rows}
    detail = attempt["versions_detail"]
    if len(detail) != len(expected) or {(r["arxiv_id"], r["version"]) for r in detail} != set(expected):
        raise ValueError("Version coverage mismatch")
    accepted = {"success", "parse_error", "empty_no_tex", "source_unavailable", "input_error"}
    if any(r["status"] not in accepted or r["mssv"] != expected[r["arxiv_id"], r["version"]] for r in detail):
        raise ValueError("Invalid version identity/status")
    source_rows = {(a, v): (d, rec) for a, _, v, d, rec in rows}
    for record in detail:
        if record["status"] == "input_error":
            directory, source = source_rows[record["arxiv_id"], record["version"]]
            if (inspect_input(directory, record["version"], source, runner) != "input_error_uppercase_tex"
                    or record["error"] != "M1 success has no TeX"):
                raise ValueError("Unverified input_error; batch cannot be published")
    if dict(Counter(r["status"] for r in detail)) != attempt["statuses"]:
        raise ValueError("Status counters differ from version records")
    checksums = read(publication / "output_checksums.json")
    filenames = {f"data/{n}/part-00000.parquet" for n in TABLES}
    if set(checksums) != filenames or any(sha(publication / f) != checksums[f] for f in filenames):
        raise ValueError("Parquet checksum mismatch")
    tables = {}
    for name, schema in arrow_schemas().items():
        path = publication / "data" / name / "part-00000.parquet"
        if pq.read_schema(path) != schema:
            raise ValueError(f"Wrong Parquet schema: {name}")
        tables[name] = pq.read_table(path).to_pylist()
    runner.validate_tables(tables)
    combined = {n: [] for n in TABLES}
    versions = []
    directories = {d for _, _, _, d, _ in rows}
    for directory in sorted(directories):
        envelope = read(root / "papers" / directory.name / "result.json")
        payload = envelope["payload"]
        if runner.digest(runner.encoded(payload)) != envelope["sha256"]:
            raise ValueError("Paper checkpoint checksum mismatch")
        runner.validate_tables(payload)
        own_aids = {a for a, _, _, d, _ in rows if d == directory}
        if len(own_aids) != 1 or any(r["arxiv_id"] not in own_aids for n in TABLES for r in payload[n]):
            raise ValueError("Cross-publication checkpoint rows")
        versions.extend(payload["versions"])
        nodes = {r["element_id"]: r for r in payload["elements"]}
        bib = {r["citation_key"] for r in payload["bibtex"]}
        for record in payload["versions"]:
            aid, version = record["arxiv_id"], record["version"]
            if (aid, version) not in expected or record["mssv"] != expected[aid, version]:
                raise ValueError("Cross-paper/version checkpoint")
            edges = [e for e in payload["hierarchy"] if e["version"] == version]
            if record["status"] != "success":
                if edges:
                    raise ValueError("Unsuccessful version has hierarchy output")
                continue
            parents = {e["element_id"]: e["parent_id"] for e in edges}
            keys = set()
            for edge in edges:
                node = edge["element_id"]
                content = nodes[node]["content"] or ""
                cited = set(citation_keys(content))
                if "*" in cited and (r"\nocite{*}" not in content or r"\cite{*}" in content):
                    raise ValueError("Invalid wildcard citation")
                keys.update(cited - {"*"})
                while node in parents:
                    node = parents[node]  # validate_tables already rejects cycles.
                if nodes[node]["content"] != f"Version {version} root":
                    raise ValueError("Hierarchy reaches a different version root")
            aliases = record.get("citation_aliases", {})
            own = {b["key"] for b in record.get("bibliography_evidence", [])}
            if not keys <= bib or any(not any(k in own and v == key for k, v in aliases.items()) for key in keys):
                raise ValueError("Citation lacks bibliography evidence in its own version")
        for name in TABLES:
            combined[name].extend(payload[name])
    if sorted(versions, key=lambda r: (r["arxiv_id"], r["version"])) != sorted(detail, key=lambda r: (r["arxiv_id"], r["version"])):
        raise ValueError("Attempt differs from checkpoints")
    if combined != tables or {n: len(tables[n]) for n in TABLES} != attempt["tables"]:
        raise ValueError("Parquet differs from checkpoint rows/counts")
    return dict(verified=True, statuses=attempt["statuses"], tables=attempt["tables"],
                papers=attempt["papers"], versions=len(detail),
                all_versions_parsed=attempt["all_versions_processed"],
                partial_coverage=not attempt["all_versions_processed"], checksums=checksums,
                input_fingerprint=definition["input_fingerprint"],
                parser_fingerprint=definition["parser_fingerprint"],
                publication_root=str(publication))


def cohort_run(name, archives, scratch, campaign, runner, scope, *, existing_only=False):
    item = discover_item(name, scope, archives)
    input_record = campaign / "inputs" / (name + ".json")
    if input_record.exists():
        old = read(input_record)
        if any(old.get(k) != v for k, v in item.items()):
            raise ValueError("Input catalog changed; preserve campaign and review")
        item = old
    corpus, rows = materialize(name, item, archives, scratch, runner, scope)
    immutable(input_record, encoded(item))
    output = campaign / "results" / name
    marker = campaign / "verified" / (name + ".json")
    definition = dict(input_fingerprint=item["input_fingerprint"],
                      parser_fingerprint=runner.digest(b"".join(p.read_bytes() for p in sorted(Path(runner.__file__).parent.glob("*.py")))),
                      selection=[(a, owner, v) for a, owner, v, _, _ in rows], parse_timeout_seconds=300)
    run_root = output / ("run-" + runner.digest(runner.encoded(definition))[:16])
    if marker.exists():
        evidence = read(marker)
        attempt = read(evidence["attempt_path"])
        result = audit(attempt, rows, runner)  # Completed cohort: audit only.
        result.update(omitted_ids=item["omitted_ids"], partial_coverage=result["partial_coverage"] or bool(item["omitted_ids"]))
        if result != evidence["audit"] or Path(attempt["run_root"]) != run_root:
            raise ValueError("Completed evidence differs from current selection")
        return result
    if existing_only:
        raise ValueError(f"Cohort is not verified yet: {name}")
    attempt = runner.run(corpus, output)
    # Native runner stores paper checkpoints even when later export/audit fails.
    result = audit(attempt, rows, runner)
    result.update(omitted_ids=item["omitted_ids"], partial_coverage=result["partial_coverage"] or bool(item["omitted_ids"]))
    attempts = [p for p in (run_root / "attempts").glob("*.json") if read(p) == attempt]
    if len(attempts) != 1:
        raise ValueError("Cannot identify immutable attempt evidence")
    immutable(marker, encoded(dict(attempt_path=str(attempts[0]), audit=result)))
    print(json.dumps(dict(cohort=name, **{k: result[k] for k in ("papers", "versions", "statuses", "tables", "partial_coverage")}), ensure_ascii=False), flush=True)
    return result


def export_batch(names, audits, campaign, scope, execution_config):
    label = names[0][7:11] + "-" + names[-1][7:11]
    package = campaign / "upload" / ("m2-" + scope["owner"] + "-" + label + "-" + PARSER_COMMIT[:12])
    aggregate = Counter()
    checksums, versions = {}, []
    for name, result in zip(names, audits):
        publication = Path(result["publication_root"])
        for table in TABLES:
            relative = f"data/{table}/{name}.parquet"
            immutable(package / relative, (publication / "data" / table / "part-00000.parquet").read_bytes())
            checksums[relative] = sha(package / relative)
        aggregate.update(result["statuses"])
        marker = read(campaign / "verified" / (name + ".json"))
        attempt = read(marker["attempt_path"])
        versions.extend({k: r.get(k) for k in ("arxiv_id", "version", "status", "error", "bibliography_status")} for r in attempt["versions_detail"])
    coverage = dict(parser_commit=PARSER_COMMIT, owner=scope["owner"], hf_repo_id=scope["hf_repo_id"], cohorts=names,
                    requested_cohorts=scope["cohorts"], campaign=read(campaign / "campaign.json"),
                    execution_config=execution_config,
                    cohort_count=len(names), assigned_cohort_count=50, requested_cohort_count=len(scope["cohorts"]),
                    assigned_range_complete=len(names) == 50,
                    partial_coverage=any(a["partial_coverage"] for a in audits),
                    omitted_ids=[aid for a in audits for aid in a["omitted_ids"]],
                    processing_complete=True, all_versions_parsed=all(a["all_versions_parsed"] for a in audits),
                    statuses=dict(aggregate), versions=versions, version_count=len(versions),
                    papers=sum(a["papers"] for a in audits),
                    tables={t: sum(a["tables"][t] for a in audits) for t in TABLES},
                    provenance=[read(campaign / "verified" / (n + ".json")) for n in names],
                    purpose="data_export_not_final_performance_benchmark")
    immutable(package / "coverage.json", encoded(coverage))
    # This README is a per-batch card, not a replacement for the shared Hub README.
    card = (f"# M2 export {label}\n\nParser: `{PARSER_COMMIT}`. MSSV: `{scope['owner']}`.\n\n"
            f"Phạm vi: {len(names)}/{len(scope['cohorts'])} cohort yêu cầu (50 được giao); {len(versions)} versions. "
            f"Statuses: `{json.dumps(dict(aggregate), sort_keys=True)}`.\n\n"
            "`processing_complete` chỉ xác nhận mọi version có trạng thái kết thúc và bảng đã qua audit. "
            "Không đồng nghĩa mọi version parse thành công. Coverage riêng trong coverage.json. "
            "Không dùng lượt này làm final benchmark.\n\n"
            "Ba bảng khác schema: cấu hình Hub riêng elements, hierarchy, bibtex; "
            "không đọc chung tất cả Parquet thành một bảng.\n")
    immutable(package / "BATCH_CARD.md", card.encode("utf8"))
    immutable(package / "checksums.json", encoded(checksums | {"coverage.json": sha(package / "coverage.json"),
                                                              "BATCH_CARD.md": sha(package / "BATCH_CARD.md")}))
    immutable(package / "READY.json", encoded(dict(verified=True, cohorts=names, checksums_sha256=sha(package / "checksums.json"))))
    return package


def campaign_path(config, scope):
    label = f"{scope['start']:04d}-{scope['end']:04d}"
    hf_hash = hashlib.sha256(scope["hf_repo_id"].encode()).hexdigest()[:12]
    return Path(config["output_drive_dir"]).resolve() / scope["owner"] / (label + "-" + PARSER_COMMIT[:12] + "-" + hf_hash)


def validate_campaign(saved, current):
    def stable(value):
        result = json.loads(json.dumps(value))
        result.pop("helper_commit", None)
        result.pop("helper_sha256", None)
        size = result["scope"].pop("batch_size")
        if type(size) is not int or not 1 <= size <= 50:
            raise ValueError("Invalid saved campaign batch size; preserve and review")
        return result
    if stable(saved) != stable(current):
        raise ValueError("Existing evidence differs; incompatible campaign; preserve and review")
    old = (saved.get("helper_commit"), saved.get("helper_sha256"))
    active = (current["helper_commit"], current["helper_sha256"])
    if old != active and old not in LEGACY_HELPERS:
        raise ValueError("Unknown campaign helper release/hash; preserve and review")


def runtime_context(config):
    scope = scope_for(config)
    if not re.fullmatch(r"[0-9a-f]{40}", config["helper_commit"]):
        raise ValueError("Invalid helper release commit")
    repo = Path(config["repo"]).resolve()
    campaign = campaign_path(config, scope)
    archives = Path(config["input_drive_dir"]).resolve()
    scratch = Path(config["scratch"]).resolve() / scope["owner"] / f"{scope['start']:04d}-{scope['end']:04d}"
    if (campaign.is_relative_to(repo) or campaign.is_relative_to(archives) or archives.is_relative_to(campaign)
            or scratch.is_relative_to(campaign) or campaign.is_relative_to(scratch)
            or scratch.is_relative_to(archives) or archives.is_relative_to(scratch)):
        raise ValueError("Code, immutable input, scratch and output paths must be separate")
    runner = verify_runtime(repo)
    import pyarrow
    fingerprint = runner.digest(b"".join(p.read_bytes() for p in sorted((repo / "src/m2").glob("*.py"))))
    identity = dict(parser_commit=PARSER_COMMIT, parser_fingerprint=fingerprint,
                    helper_sha256=sha(__file__), helper_commit=config["helper_commit"],
                    scope=scope, hf_repo_id=scope["hf_repo_id"], owner=scope["owner"],
                    python_major_minor=list(sys.version_info[:2]), pyarrow=pyarrow.__version__,
                    cohorts=scope["cohorts"], parse_timeout_seconds=300,
                    archives=str(archives), campaign=str(campaign))
    identity_path = campaign / "campaign.json"
    if identity_path.exists():
        validate_campaign(read(identity_path), identity)
    return dict(scope=scope, repo=repo, campaign=campaign, archives=archives,
                scratch=scratch, runner=runner, identity=identity)


def completed_batches(campaign, scope):
    """Validate historical packages in place, independent of today's batch size."""
    result, seen = [], set()
    for marker in sorted((campaign / "batches").glob("*.json")):
        batch = read(marker)
        names = batch.get("cohorts")
        if (not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names)
                or len(set(names)) != len(names) or not set(names) <= set(scope["cohorts"])):
            raise ValueError(f"Invalid completed batch cohort selection: {marker}")
        positions = [scope["cohorts"].index(n) for n in names]
        if positions != list(range(positions[0], positions[0] + len(names))) or seen.intersection(names):
            raise ValueError("Overlapping/noncontiguous historical batch; preserve and review")
        label = names[0][7:11] + "-" + names[-1][7:11]
        package = campaign / "upload" / ("m2-" + scope["owner"] + "-" + label + "-" + PARSER_COMMIT[:12])
        if marker.stem != label or Path(batch.get("package", "")).resolve() != package.resolve():
            raise ValueError("Historical package path/label mismatch")
        coverage = read(package / "coverage.json")
        checksums = read(package / "checksums.json")
        required = {f"data/{t}/{n}.parquet" for t in TABLES for n in names} | {"coverage.json", "BATCH_CARD.md"}
        if (set(checksums) != required or sha(package / "coverage.json") != batch.get("coverage_sha256")
                or any((package / f).is_symlink() or sha(package / f) != checksums[f] for f in required)):
            raise ValueError("Previously completed upload package is corrupt")
        ready = read(package / "READY.json")
        if ready != dict(verified=True, cohorts=names, checksums_sha256=sha(package / "checksums.json")):
            raise ValueError("Historical READY evidence differs")
        evidence = [read(campaign / "verified" / (n + ".json")) for n in names]
        if (coverage.get("cohorts") != names or coverage.get("owner") != scope["owner"]
                or coverage.get("parser_commit") != PARSER_COMMIT
                or coverage.get("hf_repo_id") != scope["hf_repo_id"]
                or coverage.get("processing_complete") is not True
                or coverage.get("provenance") != evidence
                or any(e.get("audit", {}).get("verified") is not True for e in evidence)):
            raise ValueError("Historical coverage/checkpoint evidence differs")
        seen.update(names)
        result.append(batch)
    return result


def plan_from_context(context, action):
    scope, campaign = context["scope"], context["campaign"]
    batches = completed_batches(campaign, scope)
    packaged = {n for b in batches for n in b["cohorts"]}
    verified = []
    for n in scope["cohorts"]:
        marker = campaign / "verified" / (n + ".json")
        if marker.exists():
            evidence = read(marker)
            if evidence.get("audit", {}).get("verified") is not True or not Path(evidence.get("attempt_path", "")).is_file():
                raise ValueError("Invalid recorded cohort checkpoint; preserve and review")
            verified.append(n)
    if action == "smoke":
        selected = scope["cohorts"][:1]
    elif action == "next":
        selected = []
        for n in scope["cohorts"]:
            if n in packaged:
                if selected:
                    break  # Keep new package ranges contiguous across historical batches.
                continue
            selected.append(n)
            if len(selected) == scope["batch_size"]:
                break
    elif action == "audit":
        selected = [n for n in scope["cohorts"] if n in packaged]
    else:
        raise ValueError("Unknown export action")
    return dict(student_id=scope["owner"], cohort_range=[scope["start"], scope["end"]],
                effective_batch_size=scope["batch_size"], campaign=str(campaign),
                cohorts_completed=verified, cohorts_packaged=[n for n in scope["cohorts"] if n in packaged],
                cohorts_this_run=selected, cohorts_reused=[n for n in selected if n in verified],
                cohorts_requiring_parse=[n for n in selected if n not in verified])


def execution_plan(config, action):
    """Read-only preview; does not create a campaign, parse or update evidence."""
    return plan_from_context(runtime_context(config), action)


def operate(config, action):
    context = runtime_context(config)
    scope, campaign = context["scope"], context["campaign"]
    archives, scratch, runner = context["archives"], context["scratch"], context["runner"]
    identity = context["identity"]
    with writer(campaign):
        if (campaign / "campaign.json").exists():
            validate_campaign(read(campaign / "campaign.json"), identity)
        else:
            immutable(campaign / "campaign.json", encoded(identity))
        execution = dict(effective_batch_size=scope["batch_size"], helper_commit=config["helper_commit"],
                         helper_sha256=sha(__file__))
        immutable(campaign / "execution_configs" / (hashlib.sha256(encoded(execution)).hexdigest() + ".json"), encoded(execution))
        plan = plan_from_context(context, action)
        print(json.dumps(plan, ensure_ascii=False), flush=True)
        names = plan["cohorts_this_run"]
        if action == "smoke":
            return cohort_run(names[0], archives, scratch, campaign, runner, scope)
        if action == "next":
            if not names:
                print("Mọi cohort đã có completed package; dùng Audit để kiểm tra lại.")
                return None
            audits = [cohort_run(n, archives, scratch, campaign, runner, scope) for n in names]
            package = export_batch(names, audits, campaign, scope, execution)
            label = names[0][7:11] + "-" + names[-1][7:11]
            immutable(campaign / "batches" / (label + ".json"),
                      encoded(dict(cohorts=names, package=str(package), coverage_sha256=sha(package / "coverage.json"))))
            print(f"Batch {label} đã audit; gói upload thủ công: {package}", flush=True)
            return str(package)
        if action == "audit":
            for n in names:
                cohort_run(n, archives, scratch, campaign, runner, scope, existing_only=True)
            completed_batches(campaign, scope)  # Never regenerate or rewrite old packages.
            print(json.dumps(dict(verified_cohorts=names, count=len(names), requested=len(scope["cohorts"]),
                                  assigned=50, partial_requested_coverage=len(names) != len(scope["cohorts"]),
                                  partial_assigned_coverage=len(names) != 50), ensure_ascii=False), flush=True)
            return names
        raise ValueError("Unknown export action")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", required=True)
    p.add_argument("--action", choices=("smoke", "next", "audit"), required=True)
    a = p.parse_args()
    try:
        operate(read(a.config), a.action)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(f"Export dừng an toàn; giữ checkpoint để review: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
