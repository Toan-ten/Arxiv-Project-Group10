"""Write M3's predictions table in the schema the statement requires.

The statement fixes the shape: one row per BibTeX entry, carrying
``arxiv_id``, ``partition``, ``citation_key``, a nullable ``groundtruth_arxiv_id`` and the
ranked ``cand_1`` … ``cand_5``. Only publications that belong to train, validation or test
appear; every other publication is simply absent rather than shipped as dead weight.

The earlier writer emitted one row per (entry, candidate) pair with a numeric ``rank`` and
``score``. That shape cannot be submitted - it has no partition column, no ground truth, and
the top-5 requirement is implicit in the row count rather than in the columns - so this
module replaces it and `validate_predictions` refuses anything that does not fit.

Two rules are enforced because they are easy to get wrong and expensive to explain:

* a row's ``partition`` must be the partition its publication was assigned, so a split
  cannot drift between the file that reports it and the file that is submitted;
* a candidate must be a reference of that same publication, so a top-5 cannot silently point
  at another paper's bibliography.
"""
from __future__ import annotations

import json
from pathlib import Path

PREDICTION_COLUMNS = ["arxiv_id", "partition", "citation_key", "groundtruth_arxiv_id"] + \
                     [f"cand_{index}" for index in range(1, 6)]
CANDIDATE_COLUMNS = [f"cand_{index}" for index in range(1, 6)]
PARTITIONS = ("train", "valid", "test")


def build_predictions(rankings, partitions, labels=None, abstain=None, k=5):
    """One row per ranked entry, in the statement's schema.

    ``abstain`` drops every candidate below that probability, which is how a "no match" row
    is expressed: the model says nothing rather than naming a candidate it does not believe.
    """
    labels = labels or {}
    rows = []
    for (arxiv_id, citation_key), candidates in sorted(rankings.items()):
        partition = partitions.get(arxiv_id)
        if partition not in PARTITIONS:
            continue
        row = dict(arxiv_id=arxiv_id, partition=partition, citation_key=citation_key,
                   groundtruth_arxiv_id=labels.get((arxiv_id, citation_key)))
        names = []
        for probability, ref_arxiv_id, _truth in candidates:
            if abstain is not None and probability < abstain:
                continue
            if ref_arxiv_id in names:
                continue
            names.append(ref_arxiv_id)
            if len(names) == k:
                break
        for column in CANDIDATE_COLUMNS:
            position = CANDIDATE_COLUMNS.index(column)
            row[column] = names[position] if position < len(names) else None
        rows.append(row)
    return rows


def validate_predictions(rows, partitions=None, references=None, labels=None):
    """Whether a predictions table is submittable, and what is wrong if not."""
    problems = []
    if rows:
        missing = [column for column in PREDICTION_COLUMNS if column not in rows[0]]
        extra = [column for column in rows[0] if column not in PREDICTION_COLUMNS]
        if missing:
            problems.append(f"missing column(s): {', '.join(missing)}")
        if extra:
            problems.append(f"unexpected column(s): {', '.join(extra)}")

    seen = set()
    for row in rows:
        key = (row.get("arxiv_id"), row.get("citation_key"))
        if key in seen:
            problems.append(f"entry {key[0]}/{key[1]} appears twice")
        seen.add(key)
        if row.get("partition") not in PARTITIONS:
            problems.append(f"{key[0]}/{key[1]} has partition {row.get('partition')!r}")
        candidates = [row.get(column) for column in CANDIDATE_COLUMNS if row.get(column)]
        if len(candidates) != len(set(candidates)):
            problems.append(f"{key[0]}/{key[1]} lists the same candidate twice")
        if partitions and partitions.get(key[0]) != row.get("partition"):
            problems.append(f"{key[0]} is written as {row.get('partition')} but its "
                            f"publication was assigned {partitions.get(key[0])}")
        if references is not None:
            allowed = {reference["ref_arxiv_id"] for reference in references.get(key[0], [])}
            strays = [candidate for candidate in candidates if candidate not in allowed]
            if strays:
                problems.append(f"{key[0]}/{key[1]} names {strays[0]}, which is not a "
                                f"reference of that publication")
            truth = row.get("groundtruth_arxiv_id")
            if truth and truth not in allowed:
                problems.append(f"{key[0]}/{key[1]} labels {truth}, which is not a reference "
                                f"of that publication")

    counts = {partition: 0 for partition in PARTITIONS}
    for row in rows:
        counts[row.get("partition")] = counts.get(row.get("partition"), 0) + 1
    labelled = [row for row in rows if row.get("groundtruth_arxiv_id")]
    with_candidates = [row for row in rows if row.get("cand_1")]
    report = dict(
        ok=not problems,
        problems=problems,
        rows=len(rows),
        publications=len({row["arxiv_id"] for row in rows}),
        per_partition=counts,
        labelled=len(labelled),
        with_candidates=len(with_candidates),
        abstained=len(rows) - len(with_candidates),
        columns=list(rows[0].keys()) if rows else PREDICTION_COLUMNS,
    )
    report["ok"] = not report["problems"]
    return report


def write_predictions(rows, output_dir):
    """Write one Parquet shard per partition, flat primitive columns only."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    schema = pa.schema([(column, pa.string()) for column in PREDICTION_COLUMNS])
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    written = {}
    for index, partition in enumerate(PARTITIONS):
        subset = [row for row in rows if row["partition"] == partition]
        if not subset:
            continue
        table = pa.Table.from_pylist([{column: row.get(column) for column in PREDICTION_COLUMNS}
                                      for row in subset], schema=schema)
        path = destination / f"part-{index:05d}.parquet"
        pq.write_table(table, path)
        written[partition] = dict(path=str(path), rows=len(subset))
    (destination / "summary.json").write_text(
        json.dumps(dict(written=written, columns=PREDICTION_COLUMNS), indent=2), encoding="utf-8")
    return written


def load_labels(path):
    """Labels from a JSONL export (the format the page writes) or a JSON mapping."""
    if not path:
        return {}
    text = Path(path).read_text(encoding="utf-8")
    if text.lstrip().startswith("["):
        rows = json.loads(text)
        return {(row["arxiv_id"], row["citation_key"]): row.get("groundtruth_arxiv_id")
                for row in rows}
    if text.lstrip().startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict) and "rows" in data:
            return {(row["arxiv_id"], row["citation_key"]): row.get("groundtruth_arxiv_id")
                    for row in data["rows"]}
        if isinstance(data, dict) and {"arxiv_id", "citation_key"} <= set(data):
            # A single label row: a one-line JSONL file parses as one object, not as a mapping.
            return {(data["arxiv_id"], data["citation_key"]): data.get("groundtruth_arxiv_id")}
        if isinstance(data, dict):      # legacy {arxiv_id: {citation_key: ref}}
            return {(arxiv_id, key): value
                    for arxiv_id, entries in data.items()
                    for key, value in (entries or {}).items()}
    labels = {}
    for line in text.splitlines():
        if line.strip():
            row = json.loads(line)
            labels[(row["arxiv_id"], row["citation_key"])] = row.get("groundtruth_arxiv_id")
    return labels


def run_matching(refs_path, bibtex_path, output_dir, top_k=5, labels_path=None, split_path=None,
                 abstain=None):
    """Score every publication with the classic matcher and write the predictions table.

    This is the unmodelled path: it exists so the statement's table can be produced from the
    baseline alone, and so the model's output can be compared against something that shares
    its features but learns nothing.
    """
    from .corpus import join_publications, load_bibtex, load_references
    from .matrix import pair_features
    from .review import heuristic_score

    references = load_references(refs_path)
    bibtex = load_bibtex(bibtex_path)
    publications = join_publications(references, bibtex)
    partitions = {}
    if split_path:
        split = json.loads(Path(split_path).read_text(encoding="utf-8"))
        for partition in PARTITIONS:
            for pool in ("manual", "auto"):
                for arxiv_id in split.get(partition, {}).get(pool, []):
                    partitions[arxiv_id] = partition
    labels = load_labels(labels_path)

    rankings = {}
    by_publication = {}
    for arxiv_id, (entries, refs) in publications.items():
        if not entries or not refs:
            continue
        by_publication[arxiv_id] = refs
        if arxiv_id not in partitions:
            if split_path:
                continue                       # a split was given; stay inside it
            partitions[arxiv_id] = "train"
        for entry in entries:
            scored = [(heuristic_score(pair_features(entry, reference)),
                       reference["ref_arxiv_id"], labels.get((arxiv_id, entry["citation_key"])))
                      for reference in refs]
            scored.sort(key=lambda item: (-item[0], item[1]))
            rankings[(arxiv_id, entry["citation_key"])] = scored

    rows = build_predictions(rankings, partitions, labels, abstain=abstain, k=top_k)
    report = validate_predictions(rows, partitions=partitions, references=by_publication)
    write_predictions(rows, output_dir)
    return dict(report, written=str(output_dir))
