"""Explicit nocite-only recovery into a NEW campaign; no network or old writes.

Native attempt fingerprints identify the assembly implementation. Per-paper
parser_origins below identify who actually parsed reused versus fresh payloads.
This is a data export recovery, never a homogeneous performance benchmark.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import nullcontext
import json
from pathlib import Path
import re
import subprocess

from scripts import m2_colab_export as export
from scripts.m2_colab_export import read, sha, TABLES, arrow_schemas, inspect_input
from src.m2 import runner
from src.m2.latex import citation_keys

RULE = "nocite-alias-v1"
OLD_COMMIT = export.PARSER_COMMIT
HELPERS = export.LEGACY_HELPERS | {
    ("0c0b02aea2b235f95e3984dbb85063aa36f41a59",
     "102fc48f6f2335dd5040859b8ce37c82d046c5bb6ef485a7c0a5b9aec71d0b69"),
}


def fingerprint(repo):
    return runner.digest(b"".join(p.read_bytes() for p in sorted((Path(repo) / "src/m2").glob("*.py"))))


def runtime(repo, commit, expected_fingerprint, *, allow_uncommitted=False):
    """Require explicit release identity; development mode is labelled, not hidden."""
    repo = Path(repo).resolve()
    if Path(runner.__file__).resolve() != repo / "src/m2/runner.py":
        raise ValueError("Restart Python: imported parser belongs to another checkout")
    git = ["git", "-C", str(repo)]
    head = subprocess.check_output(git + ["rev-parse", "HEAD"], text=True).strip()
    dirty = bool(subprocess.check_output(git + ["status", "--porcelain"], text=True).strip())
    if not re.fullmatch(r"[0-9a-f]{40}", commit) or head != commit or (dirty and not allow_uncommitted):
        raise ValueError("Expected clean, explicitly approved parser release; no silent pin replacement")
    actual = fingerprint(repo)
    if actual != expected_fingerprint:
        raise ValueError("New parser fingerprint mismatch")
    # Reuse is justified ONLY for the reviewed nocite-only source delta.
    old_files = subprocess.check_output(git + ["ls-tree", "--name-only", OLD_COMMIT, "src/m2/"], text=True).splitlines()
    old_files = [n for n in old_files if n.endswith(".py")]
    current = sorted(p.relative_to(repo).as_posix() for p in (repo / "src/m2").glob("*.py"))
    if sorted(old_files) != current:
        raise ValueError("Parser inventory differs beyond the nocite compatibility rule")
    blobs = []
    for name in sorted(old_files):
        original = subprocess.check_output(git + ["show", f"{OLD_COMMIT}:{name}"])
        blobs.append(original)
        expected = original.decode("utf8").replace("\r\n", "\n")
        if name == "src/m2/latex.py":
            start = expected.index("def normalize(")
            expected = expected[:start] + expected[start:].replace(
                "[aA]utocite|onlinecite)", "[aA]utocite|onlinecite|nocite)", 1)
            expected = expected.replace(
                'keys = [aliases.get(k.strip(), k.strip()) for k in m[2].split(",")]',
                '# A nocite wildcard selects the entire bibliography, never an alias.\n'
                '            keys = [("*" if k.strip() == "*" and m[1] == r"\\nocite" else aliases.get(k.strip(), k.strip()))\n'
                '                    for k in m[2].split(",")]', 1)
        if (repo / name).read_text(encoding="utf8") != expected:
            raise ValueError(f"Unreviewed parser change outside nocite compatibility: {name}")
    return dict(commit=head, fingerprint=actual, git_dirty=dirty,
                development_only=bool(dirty or allow_uncommitted), compatibility_rule=RULE,
                legacy_fingerprints=[runner.digest(b"".join(blobs)),
                                     runner.digest(b"".join(b.replace(b"\n", b"\r\n") for b in blobs))])


def contained(path, root):
    path, root = Path(path), Path(root).resolve()
    # Do not follow a symlink even when its final target happens to be inside root.
    if any(p.is_symlink() for p in [path, *path.parents]):
        raise ValueError(f"Symlink evidence path: {path}")
    if not path.resolve().is_relative_to(root):
        raise ValueError(f"Evidence path leaves its campaign: {path}")
    return path.resolve()


def payload_audit(payload, rows):
    """Same own-version evidence requirement as export.audit; never borrow keys."""
    runner.validate_tables(payload)
    expected = {(a, v): (owner, rec.get("status")) for a, owner, v, _, rec in rows}
    records = payload["versions"]
    if len(records) != len(expected) or {(r["arxiv_id"], r["version"]) for r in records} != set(expected):
        raise ValueError("Paper version coverage differs from M1")
    aids = {a for a, _ in expected}
    if len(aids) != 1 or any(r["arxiv_id"] not in aids for t in export.TABLES for r in payload[t]):
        raise ValueError("Cross-paper payload")
    nodes = {r["element_id"]: r for r in payload["elements"]}
    bib = {r["citation_key"] for r in payload["bibtex"]}
    for rec in records:
        owner, source_status = expected[rec["arxiv_id"], rec["version"]]
        if rec["mssv"] != owner or rec["m1_source_status"] != source_status or rec.get("retryable"):
            raise ValueError("Wrong source identity or retryable checkpoint")
        edges = [e for e in payload["hierarchy"] if e["version"] == rec["version"]]
        if rec["status"] != "success":
            if edges:
                raise ValueError("Unsuccessful version has hierarchy output")
            continue
        own = {b["key"] for b in rec.get("bibliography_evidence", [])}
        aliases = rec.get("citation_aliases", {})
        parents = {e["element_id"]: e["parent_id"] for e in edges}
        for edge in edges:
            node = edge["element_id"]
            content = nodes[node]["content"] or ""
            keys = set(citation_keys(content))
            if "*" in keys and (r"\nocite{*}" not in content or r"\cite{*}" in content):
                raise ValueError("Invalid wildcard")
            if not keys - {"*"} <= bib or any(
                    not any(raw in own and target == key for raw, target in aliases.items())
                    for key in keys - {"*"}):
                raise ValueError("Citation lacks bibliography evidence in its own version")
            while node in parents:
                node = parents[node]
            if nodes[node]["content"] != f"Version {rec['version']} root":
                raise ValueError("Wrong version root")
    return payload


def compatible(payload, rows):
    payload_audit(payload, rows)
    nodes = {r["element_id"]: r for r in payload["elements"]}
    for rec in payload["versions"]:
        aliases = rec.get("citation_aliases", {})
        for edge in payload["hierarchy"]:
            if edge["version"] != rec["version"]:
                continue
            content = nodes[edge["element_id"]]["content"] or ""
            for match in re.finditer(r"\\nocite\s*\{([^{}]*)\}", content):
                if any(k.strip() != "*" and aliases.get(k.strip(), k.strip()) != k.strip()
                       for k in match[1].split(",")):
                    return False
    return True


def context(config):
    scope = export.scope_for(config)
    identity = runtime(config["repo"], config["parser_commit"], config["parser_fingerprint"],
                       allow_uncommitted=config.get("allow_uncommitted", False))
    old = Path(config["old_campaign"]).resolve()
    archives, scratch = (Path(config[k]).resolve() for k in ("input_drive_dir", "scratch"))
    saved = export.read(old / "campaign.json")
    old_scope = dict(saved["scope"]); new_scope = dict(scope)
    old_scope.pop("batch_size"); new_scope.pop("batch_size")
    if (saved["parser_commit"] != OLD_COMMIT or saved["parser_fingerprint"] not in identity["legacy_fingerprints"]
            or (saved["helper_commit"], saved["helper_sha256"]) not in HELPERS
            or old_scope != new_scope or saved["hf_repo_id"] != scope["hf_repo_id"]
            or saved["owner"] != scope["owner"] or saved["cohorts"] != scope["cohorts"]
            or Path(saved["campaign"]).resolve() != old or Path(saved["archives"]).resolve() != archives
            or saved["parse_timeout_seconds"] != 300):
        raise ValueError("Old campaign identity/provenance differs; no inferred compatibility")
    if (old / ".export-writer.lock").exists():
        raise ValueError("Old campaign is active or interrupted; review its writer lock first")
    key = dict(rule=RULE, pilot_cohort=config.get("pilot_cohort"), old_campaign=str(old), old_identity_sha256=export.sha(old / "campaign.json"),
               runtime=identity, scope={k: v for k, v in scope.items() if k != "batch_size"},
               helper_sha256=export.sha(__file__))
    root = Path(config["recovery_root"]).resolve() / ("recovery-" + runner.digest(export.encoded(key))[:20])
    # Refuse pre-existing redirections before any writer or native runner opens files.
    if root.exists() and any(p.is_symlink() for p in root.rglob("*")):
        raise ValueError("Symlink inside recovery campaign; preserve and review")
    protected = [old, archives, Path(config["repo"]).resolve(), scratch]
    if any(root.is_relative_to(p) or p.is_relative_to(root) for p in protected):
        raise ValueError("Recovery, old campaign, archives, code and scratch must be separate")
    if any(scratch.is_relative_to(p) or p.is_relative_to(scratch) for p in protected[:3]):
        raise ValueError("Scratch overlaps protected data")
    packaged = export.completed_batches(old, scope)  # Checks existing READY, coverage and every package hash.
    return scope, identity, old, archives, scratch, root, key, packaged


def cohort_input(name, scope, old, archives, scratch):
    item = export.discover_item(name, scope, archives)
    prior = old / "inputs" / (name + ".json")
    if prior.exists():
        saved = export.read(prior)
        if any(saved.get(k) != v for k, v in item.items()):
            raise ValueError("Old input catalog differs from current archive")
        item = saved
    corpus, rows = export.materialize(name, item, archives, scratch, runner, scope)
    return corpus, rows, item


def native_definition(item, rows, code_hash):
    return dict(input_fingerprint=item["input_fingerprint"], parser_fingerprint=code_hash,
                selection=[[a, owner, v] for a, owner, v, _, _ in rows], parse_timeout_seconds=300)


def checkpoint(path, rows):
    env = export.read(path)
    if runner.digest(runner.encoded(env["payload"])) != env["sha256"]:
        raise ValueError(f"Paper checkpoint checksum mismatch: {path}")
    runner.validate_tables(env["payload"])
    if {(r["arxiv_id"], r["version"]) for r in env["payload"]["versions"]} != {(a, v) for a, _, v, _, _ in rows}:
        raise ValueError("Wrong paper/version checkpoint")
    return env["payload"]


def old_run(name, old, item, rows, code_hash):
    definition = native_definition(item, rows, code_hash)
    root = old / "results" / name / ("run-" + runner.digest(runner.encoded(definition))[:16])
    if root.exists() and export.read(root / "definition.json") != definition:
        raise ValueError("Old native definition differs")
    return root


def audit_native(attempt_path, rows, commit, code_hash, item):
    attempt = export.read(attempt_path)
    if attempt["provenance"]["parser_fingerprint"] != code_hash:
        raise ValueError("Native parser fingerprint differs")
    root = Path(attempt["run_root"])
    if Path(attempt_path).resolve().parent.parent != root.resolve():
        raise ValueError("Native attempt redirects to another run root")
    if export.read(root / "definition.json") != native_definition(item, rows, code_hash):
        raise ValueError("Native input/selection differs")
    return audit_attempt(attempt, rows, runner, expected_parser_commit=commit)


# Frozen legacy audit semantics, with an explicit runtime pin. Keep the published
# helper byte-identical for the still-running notebook; never relax its guards.
def audit_attempt(attempt, rows, runner, *, expected_parser_commit):
    """Verify full checkpoint/table equality and own-version citation evidence."""
    import pyarrow.parquet as pq
    from src.m2.latex import citation_keys
    root = Path(attempt["run_root"])
    publication = Path(attempt["publication_root"])
    if publication.resolve() != root.resolve() or attempt["retryable"]:
        raise ValueError("Retryable/partial attempt is not a completed export; resume required")
    definition = read(root / "definition.json")
    provenance = attempt["provenance"]
    if (provenance["git_commit"] != expected_parser_commit
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


def evidence_hashes(root, attempt_path, rows):
    files = [Path(attempt_path), root / "definition.json", root / "output_checksums.json"]
    files += [root / "papers" / d.name / "result.json" for d in sorted({r[3] for r in rows})]
    files += [root / "data" / t / "part-00000.parquet" for t in export.TABLES]
    for path in files:
        contained(path, root)
    return {str(p): export.sha(p) for p in files}


def verified(name, root, rows, item):
    path = root / "verified" / (name + ".json")
    if not path.exists():
        return None
    record = export.read(path)
    campaign = export.read(root / "campaign.json")
    old = Path(campaign["old_campaign"])
    if export.sha(old / "campaign.json") != campaign["old_identity_sha256"]:
        raise ValueError("Source campaign identity changed")
    legacy = export.read(old / "campaign.json")
    fresh = campaign["runtime"]
    allowed = {(OLD_COMMIT, legacy["parser_fingerprint"]), (fresh["commit"], fresh["fingerprint"])}
    if (record["native_commit"], record["native_fingerprint"]) not in allowed:
        raise ValueError("Unapproved native provenance")
    native_base = old if record["native_commit"] == OLD_COMMIT else root
    definition = native_definition(item, rows, record["native_fingerprint"])
    native_root = native_base / "results" / name / ("run-" + runner.digest(runner.encoded(definition))[:16])
    contained(record["attempt_path"], native_root / "attempts")
    directories = {r[3].name for r in rows}
    if set(record["parser_origins"]) != directories:
        raise ValueError("Missing publication parser origins")
    for directory, origin in record["parser_origins"].items():
        if (origin["parser_commit"], origin["parser_fingerprint"]) not in allowed:
            raise ValueError("Unapproved publication parser origin")
        source = Path(origin["checkpoint"])
        base = old if origin["parser_commit"] == OLD_COMMIT else root
        contained(source, base / "results" / name)
        if source.parts[-3:] != ("papers", directory, "result.json"):
            raise ValueError("Wrong publication origin path")
        actual = native_root / "papers" / directory / "result.json"
        if export.sha(source) != origin["sha256"] or export.sha(actual) != origin["sha256"]:
            raise ValueError("Publication origin differs from actual payload")
    for filename, checksum in record["evidence_checksums"].items():
        if not any(Path(filename).resolve().is_relative_to(p.resolve()) for p in (old, root)):
            raise ValueError("Evidence outside approved campaigns")
        if export.sha(filename) != checksum:
            raise ValueError(f"Migrated evidence changed: {filename}")
    result = audit_native(record["attempt_path"], rows, record["native_commit"],
                          record["native_fingerprint"], item)
    if result != record["audit"]:
        raise ValueError("Migrated audit differs; preserve and review")
    return record


def inspect_legacy(name, scope, old, archives, scratch, *, input_state=None):
    """Read campaigns/archives only; extraction may write the separate local scratch."""
    corpus, rows, item = input_state or cohort_input(name, scope, old, archives, scratch)
    old_identity = export.read(old / "campaign.json")
    old_root = old_run(name, old, item, rows, old_identity["parser_fingerprint"])
    if (old_root / ".writer.lock").exists():
        raise ValueError(f"Legacy native writer lock exists; preserve and review: {old_root / '.writer.lock'}")
    marker_path = old / "verified" / (name + ".json")
    grouped = {d: [r for r in rows if r[3] == d] for d in sorted({r[3] for r in rows})}
    origins, rejected, audited = {}, {}, None
    if old_root.exists() and not (old / "inputs" / (name + ".json")).is_file():
        raise ValueError("Legacy checkpoints lack their input catalog")
    if marker_path.exists():
        marker = export.read(marker_path)
        attempt_path = contained(marker["attempt_path"], old_root / "attempts")
        audited = audit_native(attempt_path, rows, OLD_COMMIT, old_identity["parser_fingerprint"], item)
        compared = audited | dict(omitted_ids=item["omitted_ids"],
            partial_coverage=audited["partial_coverage"] or bool(item["omitted_ids"]))
        if compared != marker["audit"]:
            raise ValueError("Completed old marker does not match full audit")
    for directory, selected in grouped.items():
        source = old_root / "papers" / directory.name / "result.json"
        if not source.exists() and not marker_path.exists():
            continue
        contained(source, old_root)
        payload = checkpoint(source, selected)
        try:
            usable = compatible(payload, selected)
        except ValueError as exc:
            if marker_path.exists() or str(exc) != "Citation lacks bibliography evidence in its own version":
                raise
            usable = False
        if not usable:
            if marker_path.exists():
                raise ValueError("Completed cohort requires a changed nocite payload; review explicitly")
            rejected[directory.name] = dict(checkpoint=str(source), sha256=export.sha(source),
                reason="nocite compatibility/own-version audit failed; reparse whole publication")
        else:
            origins[directory.name] = dict(mode="verified_legacy_reuse", parser_commit=OLD_COMMIT,
                parser_fingerprint=old_identity["parser_fingerprint"], checkpoint=str(source), sha256=export.sha(source))
    return corpus, rows, item, old_root, marker_path, audited, grouped, origins, rejected


def recover_cohort(name, scope, identity, old, archives, scratch, root):
    corpus, rows, item = cohort_input(name, scope, old, archives, scratch)
    existing = verified(name, root, rows, item)
    if existing is not None:
        return existing
    corpus, rows, item, old_root, old_marker, audited, grouped, origins, rejected = inspect_legacy(
        name, scope, old, archives, scratch, input_state=(corpus, rows, item))
    old_identity = export.read(old / "campaign.json")
    if old_marker.exists():
        attempt_path = contained(export.read(old_marker)["attempt_path"], old_root / "attempts")
        record = dict(cohort=name, mode="verified_legacy_reuse", attempt_path=str(attempt_path),
                      native_commit=OLD_COMMIT, native_fingerprint=old_identity["parser_fingerprint"],
                      audit=audited, parser_origins=origins, excluded_legacy_checkpoints={},
                      input=item, evidence_checksums=evidence_hashes(old_root, attempt_path, rows) |
                      {str(old_marker): export.sha(old_marker), str(old / "inputs" / (name + ".json")):
                       export.sha(old / "inputs" / (name + ".json"))})
    else:
        definition = native_definition(item, rows, identity["fingerprint"])
        new_run = root / "results" / name / ("run-" + runner.digest(runner.encoded(definition))[:16])

        # Write a durable plan BEFORE copying seeds or parsing. A restarted process
        # must use exactly the same seed attribution; old invalid caches are never copied.
        plan = dict(parser_origins=origins, excluded_legacy_checkpoints=rejected, input=item,
                    assembly_runtime=identity, definition=definition)
        contained(new_run, root)
        export.immutable(new_run / "recovery-seeds.json", export.encoded(plan))
        for directory, origin in origins.items():
            data = Path(origin["checkpoint"]).read_bytes()
            if runner.digest(data) != origin["sha256"]:
                raise ValueError("Old seed changed before copy")
            export.immutable(new_run / "papers" / directory / "result.json", data)
        attempt = runner.run(corpus, root / "results" / name)
        if Path(attempt["run_root"]).resolve() != new_run.resolve():
            raise ValueError("Unexpected new native run root")
        attempts = [p for p in (new_run / "attempts").glob("*.json") if export.read(p) == attempt]
        if len(attempts) != 1:
            raise ValueError("Cannot identify immutable new attempt")
        attempt_path = attempts[0]
        audited = audit_native(attempt_path, rows, identity["commit"], identity["fingerprint"], item)
        for directory, selected in grouped.items():
            path = new_run / "papers" / directory.name / "result.json"
            payload_audit(checkpoint(path, selected), selected)
            if directory.name not in origins:
                origins[directory.name] = dict(mode="parsed_by_new_runtime", parser_commit=identity["commit"],
                    parser_fingerprint=identity["fingerprint"], git_dirty=identity["git_dirty"],
                    checkpoint=str(path), sha256=export.sha(path))
        record = dict(cohort=name, mode="explicit_mixed_origin_assembly", attempt_path=str(attempt_path),
                      native_commit=identity["commit"], native_fingerprint=identity["fingerprint"],
                      audit=audited, parser_origins=origins, excluded_legacy_checkpoints=rejected,
                      input=item, evidence_checksums=evidence_hashes(new_run, attempt_path, rows) |
                      {str(new_run / "recovery-seeds.json"): export.sha(new_run / "recovery-seeds.json")})
        for origin in origins.values():
            record["evidence_checksums"][origin["checkpoint"]] = origin["sha256"]
    # Detect input mutation during parsing/import too. Never change the old artifacts.
    if runner.source_fingerprint(corpus)[0] != item["input_fingerprint"]:
        raise ValueError("M1 input changed during recovery")
    for filename, checksum in record["evidence_checksums"].items():
        if export.sha(filename) != checksum:
            raise ValueError("Evidence changed during recovery")
    export.immutable(root / "verified" / (name + ".json"), export.encoded(record))
    return record


def packages(root):
    seen = set()
    for marker in sorted((root / "batches").glob("*.json")):
        batch = export.read(marker)
        package = contained(batch["package"], root / "upload")
        checksums = export.read(package / "checksums.json")
        if export.read(package / "READY.json") != dict(checksums_sha256=export.sha(package / "checksums.json")):
            raise ValueError("Incomplete recovery package")
        coverage = export.read(package / "coverage.json")
        names = batch["cohorts"]
        expected = {f"data/{t}/{n}.parquet" for n in names for t in export.TABLES} | {"coverage.json"}
        if (set(checksums) != expected or coverage["cohorts"] != names
                or len(names) != len(set(names)) or set(names) & seen):
            raise ValueError("Wrong or duplicate package coverage")
        for name, checksum in checksums.items():
            if export.sha(contained(package / name, package)) != checksum:
                raise ValueError("Recovery package checksum mismatch")
        for name in names:
            current = export.read(root / "verified" / (name + ".json"))
            if current != coverage["per_cohort_provenance"][name]:
                raise ValueError("Package provenance differs from verified cohort")
        seen.update(names)
    return seen


def package_batch(names, records, root, scope, identity):
    label = names[0][7:11] + "-" + names[-1][7:11]
    package = root / "upload" / label
    checksums = {}
    statuses = Counter()
    for name, record in zip(names, records):
        publication = Path(record["audit"]["publication_root"])
        for table in export.TABLES:
            rel = f"data/{table}/{name}.parquet"
            data = (publication / "data" / table / "part-00000.parquet").read_bytes()
            if runner.digest(data) != record["audit"]["checksums"][f"data/{table}/part-00000.parquet"]:
                raise ValueError("Source Parquet changed before packaging")
            export.immutable(package / rel, data)
            checksums[rel] = export.sha(package / rel)
        statuses.update(record["audit"]["statuses"])
    coverage = dict(format="m2-recovery-release-v1", assembly_runtime=identity,
        parser_provenance="per-publication; mixed verified legacy and new, never a single parser claim",
        owner=scope["owner"], hf_repo_id=scope["hf_repo_id"], cohorts=names,
        requested_cohorts=scope["cohorts"], partial_coverage=(len(names) != len(scope["cohorts"]) or
            any(r["audit"]["partial_coverage"] or r["input"]["omitted_ids"] for r in records)),
        statuses=dict(statuses), processing_complete=True,
        all_versions_parsed=all(r["audit"]["all_versions_parsed"] for r in records),
        per_cohort_provenance=dict(zip(names, records)), purpose="recovery_data_export_not_benchmark")
    export.immutable(package / "coverage.json", export.encoded(coverage))
    checksums["coverage.json"] = export.sha(package / "coverage.json")
    export.immutable(package / "checksums.json", export.encoded(checksums))
    export.immutable(package / "READY.json", export.encoded(dict(checksums_sha256=export.sha(package / "checksums.json"))))
    export.immutable(root / "batches" / (label + ".json"), export.encoded(dict(cohorts=names, package=str(package))))
    return str(package)



def pilot_name(config, scope):
    number = config.get("pilot_cohort")
    if type(number) is not int or f"cohort_{number:04d}_corpus" not in scope["cohorts"]:
        raise ValueError("An explicit pilot_cohort inside the original scope is required")
    return f"cohort_{number:04d}_corpus"


def preflight(config):
    """No campaign writes and no parsing. Only verified archives -> local scratch."""
    scope, identity, old, archives, scratch, root, key, old_packages = context(config)
    pilot = pilot_name(config, scope)
    target = scope["cohorts"].index(pilot)
    required = scope["cohorts"][:target]
    inspected = set(required + [pilot]) | {n for n in scope["cohorts"] if (old / "verified" / (n + ".json")).exists()}
    rows_report, seals = [], {}
    for name in scope["cohorts"]:
        if name not in inspected:
            rows_report.append(dict(cohort=name, status="PENDING_NOT_INSPECTED"))
            continue
        try:
            if name in required and not (old / "verified" / (name + ".json")).is_file():
                raise ValueError(f"Required completed marker is missing: {old / 'verified' / (name + '.json')}")
            corpus, rows, item, old_root, marker, audited, grouped, origins, rejected = inspect_legacy(
                name, scope, old, archives, scratch)
            hashes = {o["checkpoint"]: o["sha256"] for o in list(origins.values()) + list(rejected.values())}
            if old_root.exists():
                hashes[str(old_root / "definition.json")] = export.sha(old_root / "definition.json")
            catalog = old / "inputs" / (name + ".json")
            if catalog.exists():
                hashes[str(catalog)] = export.sha(catalog)
            if marker.exists():
                hashes[str(marker)] = export.sha(marker)
                attempt_path = contained(export.read(marker)["attempt_path"], old_root / "attempts")
                hashes.update(evidence_hashes(old_root, attempt_path, rows))
            if (root / "verified" / (name + ".json")).exists():
                verified(name, root, rows, item)
            seals[name] = dict(input=item, evidence=hashes)
            rows_report.append(dict(cohort=name, status="MIGRATABLE" if marker.exists() else "PILOT_REPARSE_REQUIRED",
                papers=len(grouped), versions=len(rows), reusable_papers=len(origins),
                reparse_papers=[d.name for d in grouped if d.name not in origins], rejected_checkpoints=rejected))
        except (OSError, ValueError, KeyError, runner.ParseError) as exc:
            rows_report.append(dict(cohort=name, status="ERROR", error=str(exc)))
    locks = ([str(root / ".export-writer.lock")] if (root / ".export-writer.lock").exists() else [])
    locks += [str(p) for p in root.glob("results/**/.writer.lock")]
    ready = not locks and all(r["status"] != "ERROR" for r in rows_report)
    if export.sha(old / "campaign.json") != key["old_identity_sha256"]:
        raise ValueError("Source campaign identity changed during preflight")
    result = dict(ready=ready, mode="campaigns_read_only_no_parser", recovery_campaign=str(root),
        old_campaign=str(old), local_scratch=str(scratch), assembly_runtime=identity,
        required_completed=required, pilot=pilot, cohorts=rows_report, writer_locks=locks,
        old_packaged=sorted(n for b in old_packages for n in b["cohorts"]),
        can_migrate=[r["cohort"] for r in rows_report if r["status"] == "MIGRATABLE"],
        recovery_packaged=sorted(packages(root)) if root.exists() else [])
    # Approval attests exact legacy evidence, not mutable new-campaign progress.
    result["preflight_token"] = runner.digest(export.encoded(dict(identity=key, evidence=seals)))
    return result


def assert_pilot_correct(record):
    """The old successful versions whose keys were rejected must now remain successes."""
    native = Path(export.read(record["attempt_path"])["run_root"])
    for directory, rejected in record["excluded_legacy_checkpoints"].items():
        before = export.read(rejected["checkpoint"])["payload"]["versions"]
        after = export.read(native / "papers" / directory / "result.json")["payload"]["versions"]
        good = {(r["arxiv_id"], r["version"]) for r in after if r["status"] == "success"}
        if any((r["arxiv_id"], r["version"]) not in good for r in before if r["status"] == "success"):
            raise ValueError("Pilot lost a previously parsed version; inspect before continuation")
    return record

def operate(config, action):
    if action in {"preflight", "dry-run"}:
        return preflight(config)
    scope, identity, old, archives, scratch, root, key, old_packaged = context(config)
    writing = action in {"migrate", "next", "package", "pilot", "continue"}
    if writing and not identity["development_only"]:
        proof = preflight(config)
        if not proof["ready"] or config.get("approved_preflight_token") != proof["preflight_token"]:
            raise ValueError("Run/read preflight and explicitly approve its current token before Drive writes")
    if not writing and (root / ".export-writer.lock").exists():
        raise ValueError("Recovery writer lock exists; read-only audit cannot race a writer")
    with export.writer(root) if writing else nullcontext():
        if writing:
            export.immutable(root / "campaign.json", export.encoded(key))
        elif export.read(root / "campaign.json") != key:
            raise ValueError("Recovery identity differs")
        done = packages(root)
        old_verified = [n for n in scope["cohorts"] if (old / "verified" / (n + ".json")).exists()]
        completed = [n for n in scope["cohorts"] if (root / "verified" / (n + ".json")).exists()]
        print(json.dumps(dict(recovery_campaign=str(root), old_campaign=str(old),
            development_only=identity["development_only"], batch_size=scope["batch_size"],
            old_completed=old_verified, old_packaged=old_packaged,
            completed=completed, packaged=sorted(done)), ensure_ascii=False), flush=True)
        if action in {"pilot", "audit-pilot", "continue"}:
            pilot = pilot_name(config, scope)
            before = scope["cohorts"][:scope["cohorts"].index(pilot)]
            if not all(n in done for n in before):
                raise ValueError("Migrate/audit/package all earlier completed cohorts before the pilot")
            if action == "pilot":
                names = [pilot]  # Explicit selection; BATCH_SIZE cannot extend this run.
            elif action == "audit-pilot":
                names = [pilot]
            else:
                if pilot not in done:
                    raise ValueError("Pilot must be verified and packaged before continuation")
                _, selected, item = cohort_input(pilot, scope, old, archives, scratch)
                proof = verified(pilot, root, selected, item)
                if proof is None:
                    raise ValueError("Pilot has no verified recovery checkpoint")
                assert_pilot_correct(proof)
                names = [n for n in scope["cohorts"][scope["cohorts"].index(pilot) + 1:] if n not in done][:scope["batch_size"]]
        elif action == "migrate":
            names = old_verified
        elif action == "audit":
            names = completed
        elif action == "package":
            names = [n for n in completed if n not in done][:scope["batch_size"]]
        elif action == "next":
            names = [n for n in scope["cohorts"] if n not in done][:scope["batch_size"]]
        else:
            raise ValueError("Unknown recovery action")
        print(json.dumps(dict(action=action, cohorts_this_run=names,
                              effective_batch_size=1 if action in {"pilot", "audit-pilot"} else scope["batch_size"])), flush=True)
        records = []
        for name in names:
            if action in {"audit", "audit-pilot"}:
                _, rows, item = cohort_input(name, scope, old, archives, scratch)
                record = verified(name, root, rows, item)
            else:
                record = recover_cohort(name, scope, identity, old, archives, scratch, root)
            if action in {"pilot", "audit-pilot"}:
                if record is None:
                    raise ValueError("Pilot has not been verified")
                assert_pilot_correct(record)
            records.append(record)
        if action in {"next", "package", "pilot", "continue"} and names:
            return package_batch(names, records, root, scope, identity)
        return dict(campaign=str(root), cohorts=names, verified=len(records))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--action", required=True, choices=("preflight", "dry-run", "migrate", "next", "audit", "package", "pilot", "audit-pilot", "continue"))
    args = parser.parse_args()
    try:
        result = operate(export.read(args.config), args.action)
        print(json.dumps(result, ensure_ascii=False))
        if args.action in {"preflight", "dry-run"} and not result["ready"]:
            return 2
    except (OSError, ValueError, KeyError, subprocess.SubprocessError, runner.ParseError) as exc:
        print(f"Recovery stopped; preserve all evidence and review: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
