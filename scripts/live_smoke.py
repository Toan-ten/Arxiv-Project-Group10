#!/usr/bin/env python3
"""Live smoke test for Milestone 1: pilot_10 crawl, interruption and resume.

The card (M1-T02) asks for four pieces of evidence, and each has a subcommand:

``smoke``   run ``pilot_10.json`` through Batch/Pipeline into a persistent
            corpus root, optionally injecting the interruption an operator
            would send with Ctrl+C.
``resume``  re-enter the same batch with ``resume=True`` and show that finished
            work is skipped instead of re-downloaded.
``verify``  check the corpus against contract v2: the three JSON files, version
            folders holding only ``.tex``/``.bib``, and SHA-256 of every kept
            file. Also sweeps for leftover ``.tmp``/``.part`` files.
``proof``   compare attempt logs plus the corpus and print a PASS/FAIL report
            for Toan's sign-off before the full crawl.

Nothing here is used by the offline unit tests: importing this module performs
no I/O, and the tests drive the same functions with fake HTTP.

Examples
--------
  python -B -m scripts.live_smoke verify --pilot config/pilot_10.json
  python -B -m scripts.live_smoke smoke --interrupt-after 5
  python -B -m scripts.live_smoke resume
  python -B -m scripts.live_smoke proof

On Colab the corpus and logs default to ``/content/drive/MyDrive/arxiv_corpus``
(``corpus/`` and ``logs/`` siblings); elsewhere they default to
``data/pilot-10-smoke/``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.batch import BatchError, pilot_pairs, run_batch
from src.benchmark_runner import run_benchmark
from src.id_ranges import arxiv_id_to_folder_key, load_config
from src.pilots import PilotEntry, build_smoke_set
from src.storage import CorpusStorage, StorageError

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PILOT = ROOT / "config" / "pilot_10.json"
DEFAULT_BATCH_ID = "m1-smoke-pilot-10"
COLAB_CORPUS = Path("/content/drive/MyDrive/arxiv_corpus")
KEPT_SUFFIXES = {".tex", ".bib"}
JSON_FILES = ("metadata.json", "references.json", "manifest.json")
VERSION_DIR = re.compile(r"v[1-9][0-9]*")
TEMP_MARKERS = (".tmp", ".part", ".partial", ".writing")
# Bookkeeping a resume legitimately refreshes on an already-finished paper.
# Nothing under these keys is crawl data: version records, statuses and
# checksums must still compare equal.
VOLATILE_JSON_KEYS = frozenset({"updated_at", "run_id"})


# --------------------------------------------------------------------------- paths


def default_roots(
    corpus_root: str | None, log_root: str | None, report_root: str | None = None
) -> tuple[Path, Path, Path]:
    """Resolve corpus/log/report roots; Colab's mounted Drive wins when present."""
    base = COLAB_CORPUS if COLAB_CORPUS.parent.is_dir() else ROOT / "data/pilot-10-smoke"
    return (
        Path(corpus_root or base / "corpus"),
        Path(log_root or base / "logs"),
        Path(report_root or base / "reports"),
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, value) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".writing")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)
    return path


def _read_json(path: Path) -> dict:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} is not a JSON object")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_corpus(corpus_root: Path) -> dict[str, dict]:
    """Hash every corpus file so a later run can prove it left them alone.

    ``mtime_ns`` is recorded beside the digest: identical bytes that were
    rewritten would keep the hash but change the timestamp, and the card asks
    for no overwrite, not only for equal content. JSON files also keep their
    bookkeeping-free content, so a refreshed ``run_id`` is not mistaken for a
    lost version record.
    """
    root = Path(corpus_root)
    state: dict[str, dict] = {}
    if not root.is_dir():
        return state
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        info = path.stat()
        entry = dict(sha256=sha256_file(path), size=info.st_size,
                     mtime_ns=info.st_mtime_ns)
        if path.suffix == ".json":
            try:
                entry["json"] = _comparable(_read_json(path))
            except (OSError, ValueError):
                entry["json"] = None
        state[path.relative_to(root).as_posix()] = entry
    return state


def _comparable(value):
    """Strip volatile bookkeeping so resume runs compare on real data only."""
    if isinstance(value, dict):
        return {key: _comparable(item) for key, item in value.items()
                if key not in VOLATILE_JSON_KEYS}
    if isinstance(value, list):
        return [_comparable(item) for item in value]
    return value


# --------------------------------------------------------------------------- pilots


def load_pilot(path: str | Path = DEFAULT_PILOT) -> list[PilotEntry]:
    """Load a pilot set and reject duplicates or IDs outside the assigned ranges."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not raw:
        raise SystemExit(f"{path}: expected a non-empty JSON list")
    entries = []
    for item in raw:
        try:
            entries.append(PilotEntry(**item))
        except TypeError as exc:
            raise SystemExit(f"{path}: malformed pilot entry: {exc}") from exc
    assignments = load_config()
    seen: set[str] = set()
    for entry in entries:
        if entry.arxiv_id in seen:
            raise SystemExit(f"{path}: duplicate arXiv ID {entry.arxiv_id}")
        seen.add(entry.arxiv_id)
        try:
            arxiv_id_to_folder_key(entry.arxiv_id)
        except ValueError as exc:
            raise SystemExit(f"{path}: malformed arXiv ID: {exc}") from exc
        assigned = assignments.get(entry.mssv)
        if assigned is None or not (
            assigned.start_arxiv_id <= entry.arxiv_id <= assigned.end_arxiv_id
        ):
            raise SystemExit(
                f"{path}: {entry.arxiv_id} is not assigned to MSSV {entry.mssv}"
            )
    return entries


def pilot_problems(entries: list[PilotEntry]) -> list[str]:
    """Compare a pilot file with the deterministic generator in ``src.pilots``."""
    expected = build_smoke_set(list(load_config().values()))
    wanted = [(item.mssv, item.arxiv_id) for item in expected]
    actual = [(item.mssv, item.arxiv_id) for item in entries]
    if actual == wanted:
        return []
    missing = [f"{m}/{a}" for m, a in wanted if (m, a) not in actual]
    extra = [f"{m}/{a}" for m, a in actual if (m, a) not in wanted]
    return [f"pilot file differs from build_smoke_set(): missing={missing} extra={extra}"]


# --------------------------------------------------------------------------- interruption


class SimulatedInterrupt:
    """Raise KeyboardInterrupt from the Batch progress callback.

    ``run_batch`` catches ``KeyboardInterrupt`` exactly like Ctrl+C: the attempt
    is persisted as ``interrupted`` and the exception is re-raised. Raising it
    here therefore exercises the real recovery path without a human at the
    keyboard, which is what makes the resume evidence reproducible.
    """

    def __init__(self, after: int | None = None, at_start: int | None = None):
        self.after = after
        self.at_start = at_start
        self.finished = 0

    @property
    def active(self) -> bool:
        return self.after is not None or self.at_start is not None

    def __call__(self, event: str, summary: dict) -> None:
        index = len(summary["papers"]) - 1
        if event == "paper_started":
            if self.at_start is not None and index + 1 == self.at_start:
                raise KeyboardInterrupt(
                    f"simulated interruption at start of paper {index + 1}"
                )
            return
        self.finished += 1
        if self.after is not None and self.finished == self.after:
            raise KeyboardInterrupt(f"simulated interruption after paper {self.after}")


# --------------------------------------------------------------------------- run


def _s2_config(args) -> dict | None:
    if not args.s2:
        return None
    if not os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "").strip():
        raise SystemExit(
            "--s2 needs SEMANTIC_SCHOLAR_API_KEY in the environment (Colab: Secrets "
            "-> add SEMANTIC_SCHOLAR_API_KEY). Run without --s2 for an arXiv-only smoke."
        )
    return {
        "timeout": args.s2_timeout,
        "max_attempts": 1,
        "min_interval": args.s2_min_interval,
        "reference_limit": args.s2_reference_limit,
        "max_pages": args.s2_max_pages,
    }


def _attempt_lines(attempt: dict) -> list[str]:
    """Render paper rows from a persisted attempt log (Batch and Benchmark alike)."""
    lines = []
    for index, row in enumerate(attempt.get("papers", []), start=1):
        paper = row.get("result") or {}
        source = paper.get("source") or {}
        actions = ", ".join(f"{name}={record['action']}" for name, record in source.items())
        discovery = (paper.get("discovery") or {}).get("action", "-")
        lines.append(
            f"  {index:>2}. {row['arxiv_id']} [{row.get('mssv')}] state={row.get('state')} "
            f"paper={paper.get('paper_status', '-')} discovery={discovery}"
            + (f" source({actions})" if actions else "")
        )
    return lines


def _latest_attempt(log_root: Path, batch_id: str) -> dict | None:
    """Read the newest attempt log; it is the ground truth for either runner."""
    attempts = []
    for path in (Path(log_root) / batch_id / "attempts").glob("*.json"):
        try:
            attempts.append(_read_json(path))
        except (OSError, ValueError):
            continue
    if not attempts:
        return None
    return max(attempts, key=lambda value: value.get("attempt_number") or 0)


def execute(args, *, resume: bool) -> int:
    """Run or resume the smoke batch, then persist an evidence record."""
    entries = load_pilot(args.pilot)
    corpus_root, log_root, report_root = default_roots(
        args.corpus_root, args.log_root, args.report_root
    )
    problems = pilot_problems(entries)
    if problems and not resume:
        print("WARNING: " + "; ".join(problems), file=sys.stderr)

    watchdog = SimulatedInterrupt(args.interrupt_after, args.interrupt_at_start)
    observer = watchdog if watchdog.active else None
    evidence = dict(
        mode="resume" if resume else "smoke",
        batch_id=args.batch_id,
        pilot=str(Path(args.pilot).name),
        selection=[[item.mssv, item.arxiv_id] for item in entries],
        max_ids=args.max_ids,
        versions=None if args.versions is None else list(args.versions),
        s2_enabled=bool(args.s2),
        benchmark_enabled=bool(args.benchmark),
        interrupt=dict(after=args.interrupt_after, at_start=args.interrupt_at_start),
        corpus_root=str(corpus_root),
        log_root=str(log_root),
        report_root=str(report_root) if args.benchmark else None,
        started_at=_now(),
    )
    status, detail, report = "unknown", None, None
    try:
        if args.benchmark:
            report = run_benchmark(
                pilot_pairs(entries),
                corpus_root,
                batch_id=args.batch_id,
                max_ids=args.max_ids,
                log_root=log_root,
                report_root=report_root,
                experiment_id=args.experiment_id,
                resume=resume,
                versions=args.versions,
                timeout=args.timeout,
                max_attempts=args.max_attempts,
                sample_interval=args.sample_interval,
                disk_interval=args.disk_interval,
                s2_config=_s2_config(args),
                on_progress=observer,
            )
        else:
            result = run_batch(
                pilot_pairs(entries),
                corpus_root,
                batch_id=args.batch_id,
                max_ids=args.max_ids,
                log_root=log_root,
                resume=resume,
                versions=args.versions,
                timeout=args.timeout,
                max_attempts=args.max_attempts,
                s2_config=_s2_config(args),
                on_progress=observer,
            )
            report = dict(
                batch_status=result["status"],
                stop_reason=result.get("stop_reason"),
                experiment_id=None,
            )
    except KeyboardInterrupt as exc:
        status, detail = "interrupted", str(exc)
        print(f"\nInterrupted: {exc}")
    except BatchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    else:
        status = report.get("batch_status") or "unknown"
        detail = report.get("stop_reason")
        print(f"\nStatus: {status}" + (f" ({detail})" if detail else ""))
        latest = _latest_attempt(log_root, args.batch_id)
        if latest:
            for line in _attempt_lines(latest):
                print(line)
            stats = latest.get("stats") or {}
            if stats:
                print(f"Discovery: {stats.get('discovery_actions')} "
                      f"Source: {stats.get('source_actions')} "
                      f"Papers: {stats.get('paper_statuses')}")
        if report.get("experiment_id"):
            print(f"Experiment: {report['experiment_id']}")
            for key in ("measurement_status", "execution_status", "batch_status"):
                print(f"  {key}: {report.get(key)}")
            resources = report.get("resource_metrics") or {}
            detailed = resources.get("metrics_v2") or {}
            wall = detailed.get("wall_seconds", resources.get("elapsed_seconds"))
            if resources:
                print(f"  wall_seconds: {wall} "
                      f"peak_ram_mb: {resources.get('peak_ram_mb')} "
                      f"disk_used_diff_mb: {resources.get('disk_used_diff_mb')}")
            evidence["benchmark_report"] = dict(
                experiment_id=report.get("experiment_id"),
                measurement_status=report.get("measurement_status"),
                execution_status=report.get("execution_status"),
                batch_status=report.get("batch_status"),
                wall_seconds=wall,
                peak_ram_mb=resources.get("peak_ram_mb"),
                average_ram_mb=resources.get("average_ram_mb"),
                disk_corpus_size_mb=resources.get("disk_corpus_size_mb"),
                ram_source=resources.get("ram_source"),
                report_path=str(report_root / str(report.get("experiment_id")) / "report.json"),
                resources_path=str(report_root / str(report.get("experiment_id")) / "resources.json"),
            )


    # The attempt log is written by Batch; this record adds the operator view.
    attempt_paths = sorted((log_root / args.batch_id / "attempts").glob("*.json"))
    evidence.update(
        finished_at=_now(),
        status=status,
        detail=detail,
        attempt_number=len(attempt_paths) or None,
        attempt_logs=[path.name for path in attempt_paths],
        corpus_snapshot=snapshot_corpus(corpus_root),
    )
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = _write_json(
        log_root / args.batch_id / "evidence" / f"{stamp}-{evidence['mode']}.json",
        evidence,
    )
    print(f"\nEvidence: {path}")

    if status == "interrupted":
        print("\nNext: python -B -m scripts.live_smoke resume   "
              "(skips the crawled papers, continues from the checkpoint)")
        if args.benchmark:
            print("      keep --benchmark on the resume: the reports are a separate "
                  "experiment per invocation")
        return 0
    print("\nNext: python -B -m scripts.live_smoke verify && "
          "python -B -m scripts.live_smoke proof")
    return 0 if status in {"finished", "stopped"} else 1


# --------------------------------------------------------------------------- verify


def sweep_temp(*roots: Path) -> list[dict]:
    """Find leftover temporary files an abrupt stop could have orphaned."""
    found = []
    for root in roots:
        root = Path(root)
        if not root.exists():
            continue
        for path in root.rglob("*"):
            name = path.name
            hit = next((marker for marker in TEMP_MARKERS if marker in name), None)
            if hit is None:
                continue
            found.append(dict(path=str(path), marker=hit,
                              bytes=path.stat().st_size if path.is_file() else None))
    return found


def _version_report(folder: Path, manifest: dict) -> tuple[list[str], list[str], list[dict]]:
    """Check one paper's version folders against the manifest records."""
    problems: list[str] = []
    warnings: list[str] = []
    versions: list[dict] = []
    records = manifest.get("versions") or {}
    for name in records:
        if not isinstance(name, str) or not VERSION_DIR.fullmatch(name):
            problems.append(f"{folder.name}/manifest.json has invalid version key {name!r}")

    on_disk = []
    for child in sorted(folder.iterdir()):
        if child.is_dir() and VERSION_DIR.fullmatch(child.name):
            on_disk.append(child.name)
        elif child.is_dir():
            problems.append(f"unexpected directory {folder.name}/{child.name}")
        elif child.suffix not in KEPT_SUFFIXES:
            if child.name not in JSON_FILES:
                problems.append(f"unexpected file {folder.name}/{child.name}")

    for name in on_disk:
        if name not in records:
            problems.append(f"{folder.name}/{name} has no manifest record")

    for name, record in sorted(
        ((key, value) for key, value in records.items()
         if isinstance(key, str) and VERSION_DIR.fullmatch(key)),
        key=lambda item: int(item[0][1:]),
    ):
        status = record.get("status")
        expected_files = record.get("files") or []
        checksums = record.get("file_checksums") or {}
        directory = folder / name
        entry = dict(version=name, status=status, directory=directory.is_dir(),
                     files=len(expected_files), verified=0)
        if status not in {"success", "no_tex_source"}:
            warnings.append(f"{folder.name}/{name} status={status} (not terminal)")
            versions.append(entry)
            continue
        if not expected_files:
            if directory.is_dir() and any(directory.iterdir()):
                problems.append(f"{folder.name}/{name} has files but an empty record")
            versions.append(entry)
            continue
        if not directory.is_dir():
            problems.append(f"{folder.name}/{name} missing but the record has files")
            versions.append(entry)
            continue
        listed = set()
        for relative in expected_files:
            listed.add(relative)
            target = directory / relative
            if not target.is_file():
                problems.append(f"{folder.name}/{name}/{relative} missing")
                continue
            suffix = target.suffix
            if suffix not in KEPT_SUFFIXES:
                problems.append(f"{folder.name}/{name}/{relative} is not .tex/.bib")
                continue
            actual = sha256_file(target)
            if checksums.get(relative) != actual:
                problems.append(
                    f"{folder.name}/{name}/{relative} checksum mismatch "
                    "(file changed or was overwritten)"
                )
                continue
            entry["verified"] += 1
        for child in sorted(directory.rglob("*")):
            relative = child.relative_to(directory).as_posix()
            if child.is_dir():
                continue
            if relative not in listed:
                problems.append(f"{folder.name}/{name}/{relative} not in manifest.files")
        versions.append(entry)
    return problems, warnings, versions


def verify_corpus(corpus_root: Path, entries: list[PilotEntry]) -> dict:
    """Verify every pilot paper against contract v2 without trusting the logs."""
    corpus_root = Path(corpus_root)
    store = CorpusStorage(corpus_root)
    report = dict(ok=True, corpus_root=str(corpus_root), papers=[], problems=[],
                  warnings=[], temp_files=[], generated_at=_now())

    for entry in entries:
        paper = dict(mssv=entry.mssv, arxiv_id=entry.arxiv_id,
                     folder=arxiv_id_to_folder_key(entry.arxiv_id),
                     present=False, files=0, versions=[])
        folder = store.paper_path(entry.mssv, entry.arxiv_id)
        if not folder.is_dir():
            report["problems"].append(f"{paper['folder']} missing (not crawled yet)")
            report["papers"].append(paper)
            continue
        paper["present"] = True
        documents = {}
        for name in JSON_FILES:
            path = folder / name
            if not path.is_file():
                report["problems"].append(f"{paper['folder']}/{name} missing")
                continue
            try:
                documents[name] = _read_json(path)
            except (OSError, ValueError) as exc:
                report["problems"].append(f"{paper['folder']}/{name} invalid JSON: {exc}")
        for name, value in documents.items():
            if value.get("contract_version") != 2:
                report["problems"].append(
                    f"{paper['folder']}/{name} contract_version={value.get('contract_version')}"
                )
            if value.get("arxiv_id") != entry.arxiv_id:
                report["problems"].append(f"{paper['folder']}/{name} arxiv_id mismatch")
        manifest = documents.get("manifest.json")
        if manifest is None:
            report["papers"].append(paper)
            continue
        if manifest.get("mssv") != entry.mssv:
            report["problems"].append(f"{paper['folder']}/manifest.json mssv mismatch")
        paper["status"] = manifest.get("status")
        paper["metadata_status"] = manifest.get("metadata_status")
        paper["references_status"] = manifest.get("references_status")
        problems, warnings, versions = _version_report(folder, manifest)
        report["problems"].extend(problems)
        report["warnings"].extend(warnings)
        paper["versions"] = versions
        paper["files"] = sum(item["files"] for item in versions)
        paper["bytes"] = sum(path.stat().st_size for path in folder.rglob("*")
                             if path.is_file() and not path.is_symlink())
        paper["source_bytes"] = sum(
            path.stat().st_size for path in folder.rglob("*")
            if path.is_file() and path.suffix in KEPT_SUFFIXES
        )
        report["papers"].append(paper)

    report["temp_files"] = sweep_temp(corpus_root)
    if report["temp_files"]:
        report["problems"].extend(            f"leftover temp file {item['path']}" for item in report["temp_files"]
        )
    report["ok"] = not report["problems"]
    return report


def _projection(report: dict, papers: int = 20000) -> str | None:
    """Extrapolate storage from the rolled papers, for the Drive quota check."""
    measured = [paper for paper in report.get("papers", []) if paper.get("bytes")]
    if not measured:
        return None
    total = sum(paper["bytes"] for paper in measured)
    source = sum(paper.get("source_bytes") or 0 for paper in measured)
    per_paper = total / len(measured)
    return (
        f"Measured on {len(measured)} paper(s): {_mb(total)}, of which "
        f"{_mb(source)} is .tex/.bib. Projected for {papers:,} papers: "
        f"{_gb(per_paper * papers)}"
    )


def _mb(value: float) -> str:
    return f"{value / 1024 ** 2:.2f} MiB"


def _gb(value: float) -> str:
    return f"{value / 1024 ** 3:.2f} GiB"


def render_verify(report: dict) -> str:
    lines = ["# Corpus verification (contract v2)", "",
             f"Corpus: `{report['corpus_root']}`", ""]
    lines.append("| # | arXiv ID | MSSV | status | metadata | references | versions | files |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for index, paper in enumerate(report["papers"], start=1):
        versions = ", ".join(
            f"{item['version']}:{item['status']}({item['verified']}/{item['files']})"
            for item in paper["versions"]
        ) or "-"
        lines.append(
            f"| {index} | {paper['arxiv_id']} | {paper['mssv']} | "
            f"{paper.get('status', 'missing')} | {paper.get('metadata_status', '-')} | "
            f"{paper.get('references_status', '-')} | {versions} | {paper['files']} |"
        )
    lines.append("")
    for title, key in (("Problems", "problems"), ("Warnings", "warnings")):
        items = report[key]
        lines.append(f"## {title}: {len(items)}")
        lines.extend(f"- {item}" for item in items)
        lines.append("")
    lines.append(f"**Result: {'PASS' if report['ok'] else 'FAIL'}**")
    projection = _projection(report)
    if projection:
        lines.append("")
        lines.append(f"Storage: {projection}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- proof


def _attempt_logs(batch_dir: Path) -> list[dict]:
    attempts = []
    for path in sorted((batch_dir / "attempts").glob("*.json")):
        value = _read_json(path)
        value["_path"] = str(path)
        attempts.append(value)
    attempts.sort(key=lambda item: item.get("attempt_number", 0))
    return attempts


def _interrupted_snapshot(batch_dir: Path) -> tuple[dict, dict] | tuple[None, None]:
    """Return the newest evidence record written by an interrupted attempt."""
    best = None
    for path in sorted((batch_dir / "evidence").glob("*.json")):
        try:
            value = _read_json(path)
        except (OSError, ValueError):
            continue
        if value.get("status") != "interrupted":
            continue
        if not isinstance(value.get("corpus_snapshot"), dict):
            continue
        if best is None or str(value.get("finished_at")) >= str(best.get("finished_at")):
            best = value
    return (best, best["corpus_snapshot"]) if best else (None, None)


def build_proof(batch_dir: Path, corpus_root: Path, entries: list[PilotEntry]) -> dict:
    """Cross-check attempt logs against each other and against the corpus."""
    batch_dir = Path(batch_dir)
    report = dict(ok=True, batch_id=None, attempts=[], checks=[], problems=[],
                  warnings=[], generated_at=_now(), corpus_root=str(corpus_root))

    def check(name: str, ok: bool, detail: str) -> None:
        report["checks"].append(dict(check=name, ok=bool(ok), detail=detail))
        if not ok:
            report["problems"].append(f"{name}: {detail}")

    manifest_file = batch_dir / "batch.json"
    if not manifest_file.is_file():
        report["problems"].append(f"missing batch definition {manifest_file}")
        report["ok"] = False
        return report
    report["batch_id"] = _read_json(manifest_file).get("batch_id")

    attempts = _attempt_logs(batch_dir)
    report["attempts"] = [
        dict(number=item.get("attempt_number"), status=item.get("status"),
             attempt_id=item.get("attempt_id"), started_at=item.get("started_at"),
             ended_at=item.get("ended_at"), wall_seconds=item.get("wall_seconds"),
             processed=sum(1 for row in item.get("papers", [])
                           if row.get("state") == "processed"),
             selected=len(item.get("papers", [])))
        for item in attempts
    ]
    check("attempts >= 2", len(attempts) >= 2,
          f"{len(attempts)} attempt log(s) under {batch_dir / 'attempts'}")

    inter = next((i for i, item in enumerate(attempts)
                  if item.get("status") == "interrupted"), None)
    if inter is None:
        check("interruption recorded", False,
              "no interrupted attempt: run `smoke --interrupt-after N` first")
        report["ok"] = not report["problems"]
        return report
    before, after = attempts[inter], next(
        (item for item in attempts[inter + 1:] if item.get("status") != "running"), None
    )
    check("interruption recorded", True,
          f"attempt {before.get('attempt_number')} status={before.get('status')}")
    if after is None:
        check("resume attempt exists", False, "no attempt after the interruption")
        report["ok"] = not report["problems"]
        return report
    check("resume attempt ran to completion", after.get("status") in {"finished", "stopped"},
          f"attempt {after.get('attempt_number')} status={after.get('status')}")
    check("interruption visible to the resume",
          before.get("status") == "interrupted"
          or before.get("attempt_id") in (after.get("previous_incomplete_attempts") or []),
          f"attempt {before.get('attempt_number')} status={before.get('status')}, "
          f"previous_incomplete_attempts={after.get('previous_incomplete_attempts')}")

    done = sum(1 for row in before.get("papers", []) if row.get("state") == "processed")
    rows_before = before.get("papers", [])
    rows_after = after.get("papers", [])
    check("same selection in both attempts", len(rows_before) == len(rows_after),
          f"{len(rows_before)} vs {len(rows_after)} papers")
    ids_before = [row["arxiv_id"] for row in rows_before]
    check("no duplicate arXiv ID in selection",
          len(set(ids_before)) == len(ids_before), f"{len(set(ids_before))} unique")

    skipped = []
    resumed = []
    for index, row in enumerate(rows_after):
        result = row.get("result") or {}
        discovery = (result.get("discovery") or {}).get("action")
        source = {name: record.get("action")
                  for name, record in (result.get("source") or {}).items()}
        if row.get("arxiv_id") != ids_before[index]:
            check(f"paper {index + 1} identity", False, "selection order changed")
        if index < done:
            # Only versions that finished in the first attempt must be skipped;
            # a version the interrupt left pending is legitimately retried.
            earlier = ((rows_before[index].get("result") or {}).get("source") or {})
            terminal = {name for name, record in earlier.items()
                        if record.get("status") in {"success", "no_tex_source"}}
            redownloaded = {name for name in terminal
                            if source.get(name) != "skipped"}
            ok = discovery == "cached" and not redownloaded
            skipped.append(dict(index=index + 1, arxiv_id=row["arxiv_id"],
                                discovery=discovery, source=source, ok=ok,
                                terminal=sorted(terminal),
                                redownloaded=sorted(redownloaded)))
        else:
            queried = discovery in {"queried", "cached"}
            fetched = any(a in {"downloaded", "skipped", "failed", "blocked"}
                          for a in source.values()) or not source
            resumed.append(dict(index=index + 1, arxiv_id=row["arxiv_id"],
                                discovery=discovery, source=source,
                                ok=queried and fetched))
    check(f"papers 1..{done} skipped without re-crawl",
          all(item["ok"] for item in skipped),
          f"{sum(1 for item in skipped if item['ok'])}/{len(skipped)} used the discovery "
          "cache and re-downloaded no finished version")
    check(f"papers {done + 1}..{len(rows_after)} processed by the resume",
          all(item["ok"] for item in resumed),
          f"{sum(1 for item in resumed if item['ok'])}/{len(resumed)} processed")

    snapshot_record, snapshot = _interrupted_snapshot(batch_dir)
    current = snapshot_corpus(corpus_root)
    unchanged, refreshed, rewritten = [], [], []
    if snapshot is None:
        check("existing files not rewritten", False,
              "no corpus snapshot from the interrupted attempt: run `smoke` with "
              "this script so the pre-resume state is recorded")
    else:
        for path, info in snapshot.items():
            now = current.get(path)
            if now is None:
                rewritten.append(f"{path} (missing)")
                continue
            same_bytes = now["sha256"] == info["sha256"]
            untouched = same_bytes and now["mtime_ns"] == info["mtime_ns"]
            if untouched:
                unchanged.append(path)
            elif info.get("json") is not None and now.get("json") == info["json"]:
                # Only run_id/updated_at moved: the checkpoint was refreshed,
                # the crawl data it holds is identical.
                refreshed.append(path)
            elif same_bytes:
                rewritten.append(f"{path} (identical bytes, new mtime)")
            else:
                rewritten.append(f"{path} (content changed)")
        check("existing files not rewritten", not rewritten,
              f"{len(unchanged) + len(refreshed)}/{len(snapshot)} files kept their data "
              f"({len(unchanged)} untouched, {len(refreshed)} checkpoint-only refresh)"
              + (f"; rewritten: {rewritten}" if rewritten else ""))
    report["snapshot"] = dict(
        recorded_at=snapshot_record.get("finished_at") if snapshot_record else None,
        files=len(snapshot or {}), unchanged=len(unchanged),
        refreshed=refreshed, rewritten=rewritten,
    )

    corpus = verify_corpus(corpus_root, entries)
    report["verify"] = corpus
    check("corpus integrity", corpus["ok"], f"{len(corpus['problems'])} problem(s)")
    report["temp_files"] = sweep_temp(corpus_root, batch_dir.parent)
    check("no leftover temp files", not report["temp_files"],
          f"{len(report['temp_files'])} temp file(s)")
    report["skipped"] = skipped
    report["resumed"] = resumed
    report["interrupted_after"] = done
    report["ok"] = not report["problems"]
    return report


def render_proof(report: dict) -> str:
    lines = ["# Resume proof (M1-T02)", ""]
    lines.append(f"Batch: `{report.get('batch_id')}`  ")
    lines.append(f"Corpus: `{report.get('corpus_root')}`  ")
    lines.append(f"Generated: {report.get('generated_at')}")
    lines.append("")
    if report.get("interrupted_after") is not None:
        lines.append(f"Interruption point: {report['interrupted_after']} paper(s) complete")
        lines.append("")
    lines.append("## Attempts")
    lines.append("")
    lines.append("| # | status | processed | started | ended |")
    lines.append("| --- | --- | --- | --- | --- |")
    for item in report.get("attempts", []):
        lines.append(
            f"| {item['number']} | {item['status']} | "
            f"{item['processed']}/{item['selected']} | {item['started_at']} | "
            f"{item['ended_at']} |"
        )
    lines.append("")
    if report.get("skipped"):
        lines.append("## Skipped on resume (already crawled)")
        lines.append("")
        lines.append("| # | arXiv ID | discovery | source | finished versions | ok |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for item in report["skipped"]:
            source = ", ".join(f"{k}={v}" for k, v in item["source"].items()) or "-"
            terminal = ", ".join(item.get("terminal") or []) or "-"
            lines.append(f"| {item['index']} | {item['arxiv_id']} | "
                         f"{item['discovery']} | {source} | {terminal} | "
                         f"{'yes' if item['ok'] else 'NO'} |")
        lines.append("")
    if report.get("resumed"):
        lines.append("## Crawled by the resume run")
        lines.append("")
        lines.append("| # | arXiv ID | discovery | source | ok |")
        lines.append("| --- | --- | --- | --- | --- |")
        for item in report["resumed"]:
            source = ", ".join(f"{k}={v}" for k, v in item["source"].items()) or "-"
            lines.append(f"| {item['index']} | {item['arxiv_id']} | "
                         f"{item['discovery']} | {source} | {'yes' if item['ok'] else 'NO'} |")
        lines.append("")
    lines.append("## Checks")
    lines.append("")
    for item in report.get("checks", []):
        lines.append(f"- [{'x' if item['ok'] else ' '}] **{item['check']}** - {item['detail']}")
    lines.append("")
    snapshot = report.get("snapshot") or {}
    if snapshot.get("files"):
        lines.append("## Pre-resume snapshot")
        lines.append("")
        lines.append(f"Recorded at: {snapshot.get('recorded_at')}  ")
        lines.append(f"Files compared: {snapshot['files']}  ")
        lines.append(f"Unchanged: {snapshot['unchanged']}  ")
        lines.append(f"Checkpoint-only refresh (run_id/updated_at): "
                     f"{len(snapshot.get('refreshed') or [])}  ")
        lines.append(f"Rewritten: {len(snapshot.get('rewritten') or [])}")
        for item in snapshot.get("refreshed") or []:
            lines.append(f"- refreshed {item}")
        for item in snapshot.get("rewritten") or []:
            lines.append(f"- rewritten {item}")
        lines.append("")
    if report.get("problems"):
        lines.append("## Problems")
        lines.append("")
        lines.extend(f"- {item}" for item in report["problems"])
        lines.append("")
    lines.append(f"**Result: {'PASS' if report.get('ok') else 'FAIL'}**")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- cli


def _add_run_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--pilot", default=str(DEFAULT_PILOT), help="Pilot JSON file")
    parser.add_argument("--batch-id", default=DEFAULT_BATCH_ID)
    parser.add_argument("--corpus-root", default=None,
                        help="Persistent corpus root (default: Colab Drive or data/)")
    parser.add_argument("--log-root", default=None, help="Batch log root")
    parser.add_argument("--report-root", default=None,
                        help="Benchmark report root (sibling of corpus/logs)")
    parser.add_argument("--max-ids", type=int, default=10,
                        help="Number of IDs from the pilot file (default: 10)")
    parser.add_argument("--versions", nargs="+", default=None,
                        help="Explicit versions, e.g. --versions v1 v2 (default: all)")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--max-attempts", type=int, default=1,
                        help="Keep at 1 for a smoke test")
    parser.add_argument("--s2", action="store_true",
                        help="Enable Semantic Scholar (needs SEMANTIC_SCHOLAR_API_KEY)")
    parser.add_argument("--benchmark", action="store_true",
                        help="Route through run_benchmark to record time/RAM/disk; "
                             "the same interruption and resume path still applies")
    parser.add_argument("--experiment-id", default=None,
                        help="Benchmark experiment id (default: generated per invocation)")
    parser.add_argument("--sample-interval", type=float, default=0.1,
                        help="Resource sampling interval in seconds (benchmark only)")
    parser.add_argument("--disk-interval", type=float, default=30.0,
                        help="Disk sampling interval in seconds (benchmark only)")
    parser.add_argument("--s2-timeout", type=float, default=15.0)
    parser.add_argument("--s2-min-interval", type=float, default=2.0)
    parser.add_argument("--s2-reference-limit", type=int, default=100)
    parser.add_argument("--s2-max-pages", type=int, default=10)


def _add_interrupt_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--interrupt-after", type=int, default=None, metavar="N",
                       help="Inject Ctrl+C after N papers finish (deterministic)")
    group.add_argument("--interrupt-at-start", type=int, default=None, metavar="N",
                       help="Inject Ctrl+C when paper N starts (mid-paper case)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)

    smoke = commands.add_parser("smoke", help="Fresh pilot_10 run (optionally interrupted)")
    _add_run_arguments(smoke)
    _add_interrupt_arguments(smoke)
    smoke.set_defaults(handler=lambda args: execute(args, resume=False))

    resume = commands.add_parser("resume", help="Resume the same batch (resume=True)")
    _add_run_arguments(resume)
    _add_interrupt_arguments(resume)
    resume.set_defaults(handler=lambda args: execute(args, resume=True))

    verify = commands.add_parser("verify", help="Verify corpus integrity")
    verify.add_argument("--pilot", default=str(DEFAULT_PILOT))
    verify.add_argument("--corpus-root", default=None)
    verify.add_argument("--log-root", default=None)
    verify.add_argument("--report-root", default=None)
    verify.add_argument("--json-out", default=None)

    proof = commands.add_parser("proof", help="Resume evidence report")
    proof.add_argument("--pilot", default=str(DEFAULT_PILOT))
    proof.add_argument("--batch-id", default=DEFAULT_BATCH_ID)
    proof.add_argument("--corpus-root", default=None)
    proof.add_argument("--log-root", default=None)
    proof.add_argument("--report-root", default=None)
    proof.add_argument("--json-out", default=None)
    proof.add_argument("--md-out", default=None)
    return parser


def _corpus_command(args, runner) -> int:
    entries = load_pilot(args.pilot)
    corpus_root, log_root, _report_root = default_roots(
        args.corpus_root, args.log_root, None
    )
    payload = runner(entries, corpus_root, log_root)
    if args.json_out:
        _write_json(Path(args.json_out), payload)
    return 0 if payload["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    # Reports quote task cards, which contain characters a Windows console
    # cannot always encode (cp932 here). Keep UTF-8 files strict and lose only
    # the console copy rather than crashing mid-report.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="replace")
            except (ValueError, OSError):  # pragma: no cover - exotic streams
                pass
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command in {"smoke", "resume"}:
        return args.handler(args)
    if args.command == "verify":
        def runner(entries, corpus_root, _log_root):
            report = verify_corpus(corpus_root, entries)
            print(render_verify(report))
            problems = pilot_problems(entries)
            if problems:
                print("Pilot set warning: " + "; ".join(problems))
            return report
        return _corpus_command(args, runner)

    def proof_runner(entries, corpus_root, log_root):
        report = build_proof(log_root / args.batch_id, corpus_root, entries)
        markdown = render_proof(report)
        print(markdown)
        target = Path(args.md_out) if args.md_out else (
            log_root / args.batch_id / "evidence" / "resume-proof.md"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(markdown, encoding="utf-8")
        print(f"Report: {target}")
        return report

    return _corpus_command(args, proof_runner)


if __name__ == "__main__":
    sys.exit(main())
