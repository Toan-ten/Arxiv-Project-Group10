"""Partition M3's publications into the train, validation and test sets.

The statement fixes the shape: with ``k`` members, the test set holds one manually labelled
and one automatically matched publication *per member*, the validation set likewise, and
everything else trains. The quota is therefore a function of the group size, not a choice.

Two properties matter and both are enforced here rather than assumed:

* **no publication appears in two partitions** - a publication is the unit, so a leak
  between train and test would be a whole paper's worth of leakage;
* **the split is reproducible** - the same seed and the same publication lists always give
  the same partition, so a rerun of the pipeline cannot quietly change what was reported.

Partitions are chosen before the labels are read, from a seeded shuffle: choosing them after
seeing the model's scores would turn validation into a second training set.
"""
from __future__ import annotations

import random
from collections import Counter

PARTITIONS = ("train", "valid", "test")
DEFAULT_SEED = 2026
MINIMUM_PER_PUBLICATION = 20


def _shuffled(ids, seed, salt):
    ordered = sorted(ids)
    random.Random(f"{seed}:{salt}").shuffle(ordered)
    return ordered


def assign_partitions(manual_ids, auto_ids, members, seed=DEFAULT_SEED):
    """Split both pools at the statement's per-member quantities.

    Test and validation each take ``members`` publications from the manual pool and
    ``members`` from the automatic pool; whatever is left trains.
    """
    wanted = members
    if len(manual_ids) < 2 * wanted:
        raise ValueError(f"manual pool needs {2 * wanted} publications for k={members}, "
                         f"got {len(manual_ids)}")
    if len(auto_ids) < 2 * wanted:
        raise ValueError(f"automatic pool needs {2 * wanted} publications for k={members}, "
                         f"got {len(auto_ids)}")

    manual_order = _shuffled(manual_ids, seed, "manual")
    auto_order = _shuffled(auto_ids, seed, "auto")
    return dict(
        seed=seed,
        members=members,
        test=dict(manual=manual_order[:wanted], auto=auto_order[:wanted]),
        valid=dict(manual=manual_order[wanted:2 * wanted],
                   auto=auto_order[wanted:2 * wanted]),
        train=dict(manual=manual_order[2 * wanted:], auto=auto_order[2 * wanted:]),
    )


def partition_map(split):
    """``arxiv_id -> partition`` for every publication in the split."""
    mapping = {}
    for partition in PARTITIONS:
        for pool in ("manual", "auto"):
            for arxiv_id in split[partition][pool]:
                mapping[arxiv_id] = partition
    return mapping


def validate_split(split, members, labels=None):
    """Whether a split satisfies the statement, and what is wrong if not.

    ``labels`` is an optional list of label rows: when given, every publication in a
    partition is also checked for the label count the manual pool must reach.
    """
    problems = []
    mapping = partition_map(split)
    test_ids = split["test"]["manual"] + split["test"]["auto"]
    for partition in PARTITIONS:
        for pool in ("manual", "auto"):
            ids = split[partition][pool]
            if len(set(ids)) != len(ids):
                problems.append(f"{partition}/{pool} lists a publication twice")

    if len(split["test"]["manual"]) != members:
        problems.append(f"test holds {len(split['test']['manual'])} manual publications, "
                        f"{members} required (one per member)")
    if len(split["test"]["auto"]) != members:
        problems.append(f"test holds {len(split['test']['auto'])} automatic publications, "
                        f"{members} required (one per member)")
    if len(split["valid"]["manual"]) != members or len(split["valid"]["auto"]) != members:
        problems.append("validation must hold one manual and one automatic publication per "
                        "member")
    if not split["train"]["manual"] or not split["train"]["auto"]:
        problems.append("training set is empty on one of its pools")

    overlap = set(test_ids) & set(split["train"]["manual"] + split["train"]["auto"] +
                                  split["valid"]["manual"] + split["valid"]["auto"])
    if overlap:
        problems.append(f"{len(overlap)} publication(s) appear in more than one partition, "
                        f"e.g. {sorted(overlap)[0]}")
    if len(mapping) != sum(len(split[p]["manual"]) + len(split[p]["auto"]) for p in PARTITIONS):
        problems.append("the same publication appears in two pools")

    report = dict(ok=not problems, problems=problems, counts={}, labels={})
    for partition in PARTITIONS:
        report["counts"][partition] = dict(
            manual=len(split[partition]["manual"]), auto=len(split[partition]["auto"]))
    if labels is not None:
        per_publication = Counter(row["arxiv_id"] for row in labels)
        thin = [arxiv_id for partition in ("valid", "test")
                for arxiv_id in split[partition]["manual"]
                if per_publication.get(arxiv_id, 0) < MINIMUM_PER_PUBLICATION]
        matched = Counter(row["arxiv_id"] for row in labels if row.get("groundtruth_arxiv_id"))
        report["labels"] = dict(
            rows=len(labels),
            publications=len(per_publication),
            per_partition={partition: sum(per_publication.get(arxiv_id, 0)
                                          for arxiv_id in split[partition]["manual"])
                           for partition in PARTITIONS},
            thin=thin,
            unlabelled_test=[arxiv_id for arxiv_id in split["test"]["manual"]
                             if arxiv_id not in per_publication],
        )
        if thin:
            problems.append(f"{len(thin)} validation/test publication(s) carry fewer than "
                            f"{MINIMUM_PER_PUBLICATION} labels, e.g. {thin[0]}")
        if report["labels"]["unlabelled_test"]:
            problems.append(f"{len(report['labels']['unlabelled_test'])} test publication(s) "
                            f"have no labels at all")
        report["ok"] = not problems
    return report


def annotate(labels, split):
    """Copy label rows, adding the partition each publication belongs to."""
    mapping = partition_map(split)
    missing = sorted({row["arxiv_id"] for row in labels if row["arxiv_id"] not in mapping})
    rows = [dict(row, partition=mapping.get(row["arxiv_id"])) for row in labels]
    return rows, missing
