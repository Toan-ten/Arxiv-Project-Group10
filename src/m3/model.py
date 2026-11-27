"""Train and score M3's ranker over the candidate matrix.

The statement models the task as a binary matrix: each BibTeX entry is a row, each crawled
reference of the same publication is a cell, and the cell holding the cited work is the
positive. So the model is a pointwise binary classifier over cells - one probability per
candidate - and an entry is ranked by sorting its candidates on that probability. MRR@5 is
then the mean reciprocal rank of the correct candidate, with zero when it is not in the top
five.

Three decisions are worth stating because they change what the numbers mean:

* **A row labelled "none" contributes only negatives.** There is no positive to learn from,
  which is exactly right: the entry's cited work is not among its candidates.
* **Ranking is per entry, not per publication.** Candidates are compared only inside their
  own entry's candidate set, so a publication with 300 references cannot drown one with 8.
* **Abstention is a model output.** A pointwise classifier always produces a best candidate,
  even when no candidate is right. The abstention threshold is tuned on validation and makes
  "no match" an explicit prediction, which is what an all-no row needs in order to score at
  all rather than scoring zero by construction.

``doi_exact`` is dropped from the model's features: Milestone 1's reference table has no DOI
column, so that feature is a constant zero and would only add noise to a coefficient.
"""
from __future__ import annotations

from collections import defaultdict

import numpy as np

from .matrix import FEATURE_COLUMNS, pair_features

MODEL_FEATURES = [column for column in FEATURE_COLUMNS if column != "doi_exact"]
ABSTENTION_GRID = [round(0.05 + 0.05 * step, 2) for step in range(18)]


def group(rows):
    out = defaultdict(list)
    for row in rows:
        out[row["arxiv_id"]].append(row)
    return out


def index_by(rows, key):
    out = defaultdict(dict)
    for row in rows:
        out[row["arxiv_id"]][row[key]] = row
    return out


def build_cells(labelled_rows, bibtex_by_publication, references_by_publication,
                features=MODEL_FEATURES):
    """One row per (entry, reference) cell with its binary label.

    ``meta`` keeps ``(arxiv_id, citation_key, ref_arxiv_id, truth)`` per cell so the same
    matrix can be ranked per entry and scored against the label it came from.
    """
    matrix, labels, meta = [], [], []
    for row in labelled_rows:
        arxiv_id = row["arxiv_id"]
        entry = bibtex_by_publication.get(arxiv_id, {}).get(row["citation_key"])
        if entry is None:
            continue
        truth = row.get("groundtruth_arxiv_id") or None
        for reference in references_by_publication.get(arxiv_id, []):
            cell = pair_features(entry, reference)
            matrix.append([cell[name] for name in features])
            labels.append(1 if truth and reference["ref_arxiv_id"] == truth else 0)
            meta.append((arxiv_id, row["citation_key"], reference["ref_arxiv_id"], truth))
    return np.asarray(matrix, dtype=float), np.asarray(labels, dtype=int), meta


def train(kind, matrix, labels, seed=2026):
    """Fit one of the two models the milestone compares."""
    if kind == "logistic":
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler

        return make_pipeline(StandardScaler(),
                             LogisticRegression(max_iter=1000, class_weight="balanced",
                                                random_state=seed)).fit(matrix, labels)
    if kind == "boosting":
        from sklearn.ensemble import HistGradientBoostingClassifier

        return HistGradientBoostingClassifier(random_state=seed, max_iter=200,
                                              learning_rate=0.1).fit(matrix, labels)
    raise ValueError(f"unknown model {kind!r}; use 'logistic' or 'boosting'")


def probability(model, matrix):
    if len(matrix) == 0:
        return np.zeros(0)
    if hasattr(model, "predict_proba"):
        return model.predict_proba(matrix)[:, 1]
    return np.asarray(model.decision_function(matrix), dtype=float)


def rank_entries(meta, scores):
    """Per entry, its candidates with their probability, best first."""
    grouped = defaultdict(list)
    for (arxiv_id, citation_key, ref_arxiv_id, truth), score in zip(meta, scores):
        grouped[(arxiv_id, citation_key)].append((float(score), ref_arxiv_id, truth))
    ranked = {}
    for key, candidates in grouped.items():
        candidates.sort(key=lambda item: (-item[0], item[1]))
        ranked[key] = candidates
    return ranked


def entry_score(candidates, k=5, abstain=None):
    """Reciprocal rank of the labelled candidate, or the abstention verdict.

    An entry labelled "none" scores 1.0 when the model abstains and 0.0 when it names a
    candidate: there is nothing to find, so the only correct answer is "no match". An entry
    with a label scores 0.0 when the model abstains, because abstaining on a row that had an
    answer is the same mistake as ranking the answer below fifth.
    """
    truth = candidates[0][2] if candidates else None
    if abstain is not None:
        best_probability = candidates[0][0] if candidates else 0.0
        if best_probability < abstain:
            return (1.0 if truth is None else 0.0), True
    if truth is None:
        return 0.0, False
    for position, (_, ref_arxiv_id, _) in enumerate(candidates[:k], start=1):
        if ref_arxiv_id == truth:
            return 1.0 / position, False
    return 0.0, False


def mrr_at_k(ranked, k=5, abstain=None):
    """Mean reciprocal rank at ``k`` plus the counts that explain it."""
    if not ranked:
        return dict(mrr=0.0, entries=0, hits=0, none_entries=0, abstained=0)
    scores, hits, none_entries, abstained = [], 0, 0, 0
    for candidates in ranked.values():
        score, was_abstained = entry_score(candidates, k, abstain)
        scores.append(score)
        abstained += 1 if was_abstained else 0
        hits += 1 if score > 0 else 0
        if candidates and candidates[0][2] is None:
            none_entries += 1
    return dict(mrr=round(float(np.mean(scores)), 6), entries=len(scores), hits=hits,
                none_entries=none_entries, abstained=abstained)


def tune_abstention(ranked, k=5, grid=ABSTENTION_GRID):
    """Pick the probability below which the model should say "no match"."""
    table = []
    for threshold in grid:
        report = mrr_at_k(ranked, k=k, abstain=threshold)
        table.append(dict(threshold=threshold, **report))
    best = max(table, key=lambda row: (row["mrr"], -row["threshold"]))
    return best, table


def filter_ranked(ranked, publications):
    """The entries belonging to a set of publications."""
    wanted = set(publications)
    return {key: value for key, value in ranked.items() if key[0] in wanted}


def truth_map(labelled_rows):
    return {(row["arxiv_id"], row["citation_key"]): (row.get("groundtruth_arxiv_id") or None)
            for row in labelled_rows}


class HeuristicBaseline:
    """The classic matcher's fixed weights, exposed as a scorer.

    The ranker has to beat something, and the honest reference point is the heuristic the
    automatic pool was labelled with - not a random ordering. It reads the same feature
    matrix, so the comparison isolates learning from the features themselves.
    """

    def decision_function(self, matrix):
        from .review import heuristic_score

        positions = {name: index for index, name in enumerate(MODEL_FEATURES)}
        scores = []
        for row in matrix:
            features = {name: float(row[positions[name]]) for name in MODEL_FEATURES}
            features["doi_exact"] = 0.0
            scores.append(heuristic_score(features))
        return np.asarray(scores, dtype=float)


def cells_for_entries(entries, references, features=MODEL_FEATURES, truths=None):
    """Every cell of one publication, whether or not the entry has a label.

    Prediction needs the whole candidate matrix; training only needed the labelled entries.
    Keeping them in one builder means the model sees exactly the same features at fit time
    and at predict time.
    """
    truths = truths or {}
    matrix, meta = [], []
    for entry in entries:
        truth = truths.get((entry["arxiv_id"], entry["citation_key"]))
        for reference in references:
            cell = pair_features(entry, reference)
            matrix.append([cell[name] for name in features])
            meta.append((entry["arxiv_id"], entry["citation_key"], reference["ref_arxiv_id"],
                         truth))
    return np.asarray(matrix, dtype=float), meta
