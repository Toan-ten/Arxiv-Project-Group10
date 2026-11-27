"""The automatic pool: a classic matcher that labels without a trained model.

The statement reserves the manual pool for human ground truth and asks for at least 10% of
the remaining publications to be matched automatically "with classic tools such as regex or
string similarity heuristics". This module is that matcher, and it is deliberately not a
model: it scores a cell from the same fixed weights the labelling page uses for its drafts,
accepts a label only when the score clears a threshold *and* the winner clears a margin over
the runner-up, and otherwise leaves the cell unlabelled.

Three gates keep invented labels out:

* **score and margin** - a near-tie between two references means the evidence does not pick
  one, and a coin flip written down as a label is worse than no label;
* **independent evidence** - a title alone is accepted only when it is near verbatim,
  otherwise the author surnames must agree;
* **a differing number** - " ... Part I" against " ... Part II" is two papers, and no title
  score can tell them apart.

Labels are one-to-one by construction: two entries that both point at the same reference is
evidence the matcher is confused, so the stronger claim keeps the reference and the other
falls back to its next candidate.

Thresholds are provisional until they are calibrated against the manual pool; :func:`calibrate`
is what turns a labelled set into the precision/recall curve used to pick them.
"""
from __future__ import annotations

import random
from collections import Counter, defaultdict

from .matrix import normalize_title, pair_features, surnames, tokens
from .review import heuristic_score, number_mismatch

THRESHOLD = 0.70
MARGIN = 0.08
VERBATIM_TITLE = 0.95
BLOCK_SIZE = 8
MIN_MATCHES_PER_PUBLICATION = 10
MAX_ENTRIES_PER_PUBLICATION = 150
POOL_SEED = 2026

MATCHED = "auto_match"
UNMATCHED = "auto_none"


def _title_tokens(row, column):
    return tokens(normalize_title(row.get(column)))


def blocking_candidates(entry, references, block_size=BLOCK_SIZE):
    """Reference indices worth the full comparison, by cheap token overlap then surname.

    Computing every feature for every cell of 2 400 publications costs far more than the
    answer is worth, and a token Jaccard is a sound first pass: a reference that shares no
    meaningful title token cannot be the same work unless the authors prove it.
    """
    entry_tokens = _title_tokens(entry, "title")
    shared = surnames(entry.get("authors"))
    scored = []
    for index, reference in enumerate(references):
        ref_tokens = reference["_tokens"]
        jaccard = (len(entry_tokens & ref_tokens) / len(entry_tokens | ref_tokens)
                   if entry_tokens and ref_tokens else 0.0)
        author_hit = bool(shared & reference["_surnames"])
        scored.append((jaccard + (0.5 if author_hit else 0.0), index))
    scored.sort(key=lambda item: -item[0])
    return [index for _, index in scored[:block_size]]


def rank_candidates(entry, references, block_size=BLOCK_SIZE):
    """Every blocked candidate for one entry, best first, with its features."""
    ranked = []
    for index in blocking_candidates(entry, references, block_size):
        reference = references[index]
        features = pair_features(entry, reference)
        ranked.append(dict(
            index=index,
            id=reference["ref_arxiv_id"],
            entry=entry,
            reference=reference,
            features=features,
            score=heuristic_score(features),
            number_conflict=number_mismatch(entry.get("title"), reference.get("ref_title")),
        ))
    # The title sequence ratio breaks a score tie: when two candidates look equally strong,
    # the one whose title is closer verbatim is the better claim.
    ranked.sort(key=lambda row: (-row["score"], -row["features"]["title_sequence_ratio"], row["id"]))
    for position, row in enumerate(ranked):
        runner_up = ranked[position + 1]["score"] if position + 1 < len(ranked) else 0.0
        row["margin"] = round(row["score"] - runner_up, 4)
    return ranked


def acceptance(candidate, margin, threshold=THRESHOLD, margin_floor=MARGIN):
    """Whether one candidate may be labelled, and on what evidence.

    ``margin`` is passed in rather than read from the row because it depends on which
    candidates are still unclaimed: a reference another entry already holds is no longer a
    competitor, so it must not depress the margin of the next one.
    """
    if not candidate:
        return None, "no candidate"
    features = candidate["features"]
    if not features["entry_has_title"]:
        return None, "entry carries no title"
    if candidate["number_conflict"]:
        return None, "titles disagree on a number"
    if candidate["score"] < threshold:
        return None, f"score {candidate['score']:.2f} below {threshold:.2f}"
    if margin < margin_floor:
        return None, f"margin {margin:.2f} below {margin_floor:.2f}"
    if features["author_jaccard"] > 0 or features["first_author_match"]:
        return "title_and_author", None
    if features["title_sequence_ratio"] >= VERBATIM_TITLE:
        return "title_verbatim", None
    return None, "title agrees but no author evidence"


def label_publication(entries, references, threshold=THRESHOLD, margin_floor=MARGIN,
                      block_size=BLOCK_SIZE):
    """Labels for every entry of one publication, one reference claimed at most once.

    Greedy in descending score: the strongest claim to a reference wins it, and an entry
    whose preferred reference is already claimed falls back to its next candidate, or to no
    match at all.
    """
    prepared = []
    for reference in references:
        prepared.append(dict(reference, **{
            "_tokens": _title_tokens(reference, "ref_title"),
            "_surnames": surnames(reference.get("ref_authors")),
        }))

    claims = []
    for entry in entries:
        ranked = rank_candidates(entry, prepared, block_size)
        claims.append(dict(entry=entry, ranked=ranked))
    claims.sort(key=lambda claim: -(claim["ranked"][0]["score"] if claim["ranked"] else 0.0))

    claimed = {}
    rows = []
    for claim in claims:
        entry = claim["entry"]
        best = claim["ranked"][0] if claim["ranked"] else None
        # One decision per entry, taken against its own full ranking. A weak or near-tied
        # winner is a "no", never an invitation to label the runner-up: a matcher that
        # falls back on an unclear margin invents exactly the label the gate exists to stop.
        evidence, reason = acceptance(best, best["margin"] if best else 0.0,
                                      threshold, margin_floor)
        if evidence:
            reason = "the reference it matched is already claimed by a stronger entry"
        pick, pick_margin = None, 0.0
        if evidence and best["id"] not in claimed:
            pick, pick_margin = best, best["margin"]
        elif evidence:
            # The reference is already held by a stronger claim, so it stops competing: the
            # remaining candidates are compared among themselves.
            unclaimed = [candidate for candidate in claim["ranked"]
                         if candidate["id"] not in claimed]
            for position, candidate in enumerate(unclaimed):
                runner_up = unclaimed[position + 1]["score"] if position + 1 < len(unclaimed) else 0.0
                margin = round(candidate["score"] - runner_up, 4)
                fallback, fallback_reason = acceptance(candidate, margin, threshold, margin_floor)
                if fallback:
                    pick, pick_margin, evidence = candidate, margin, fallback
                    break
                reason = fallback_reason
        if pick is None:
            rows.append(dict(
                arxiv_id=entry["arxiv_id"], citation_key=entry["citation_key"],
                groundtruth_arxiv_id=None, decision=UNMATCHED, method="none",
                score=round(best["score"], 4) if best else 0.0,
                margin=best["margin"] if best else 0.0,
                reason=reason,
            ))
            continue
        claimed[pick["id"]] = entry["citation_key"]
        rows.append(dict(
            arxiv_id=entry["arxiv_id"], citation_key=entry["citation_key"],
            groundtruth_arxiv_id=pick["id"], decision=MATCHED, method=evidence,
            score=pick["score"], margin=pick_margin,
            reason=f"score {pick['score']:.2f}, margin {pick_margin:.2f}",
        ))
    return rows


def group(rows):
    out = defaultdict(list)
    for row in rows:
        out[row["arxiv_id"]].append(row)
    return out


def label_corpus(bibtex_rows, reference_rows, publication_ids=None, **kwargs):
    """Run the matcher over the publications named, or over every publication."""
    by_bib, by_ref = group(bibtex_rows), group(reference_rows)
    wanted = sorted(publication_ids if publication_ids is not None
                    else set(by_bib) & set(by_ref))
    rows, summary = [], {}
    for arxiv_id in wanted:
        labelled = label_publication(by_bib[arxiv_id], by_ref[arxiv_id], **kwargs)
        summary[arxiv_id] = dict(
            entries=len(labelled),
            matched=sum(1 for row in labelled if row["decision"] == MATCHED),
            references=len(by_ref[arxiv_id]),
        )
        rows.extend(labelled)
    return rows, summary


def select_auto_publications(summary, limit, minimum_matches=MIN_MATCHES_PER_PUBLICATION,
                             exclude=(), maximum_entries=MAX_ENTRIES_PER_PUBLICATION,
                             seed=POOL_SEED):
    """A representative slice of the publications the matcher can label.

    Ranking by match count would hand the pool the corpus's largest bibliographies - the
    top of that list is a 349-entry, 242-reference survey - and every later test or
    validation publication drawn from it would be that same unusual shape. A seeded random
    sample of the publications that clear the gates keeps the pool looking like the corpus,
    and the seed keeps it reproducible.
    """
    excluded = set(exclude)
    eligible = sorted(
        ((arxiv_id, stats) for arxiv_id, stats in summary.items()
         if arxiv_id not in excluded and stats["matched"] >= minimum_matches
         and (maximum_entries is None or stats["entries"] <= maximum_entries)),
        key=lambda item: item[0])
    chosen = (eligible if len(eligible) <= limit
              else sorted(random.Random(seed).sample(eligible, limit), key=lambda item: item[0]))
    return [dict(arxiv_id=arxiv_id, **stats) for arxiv_id, stats in chosen], eligible


def calibrate(labelled_rows, bibtex_rows, reference_rows, thresholds=None, margins=None):
    """Precision and recall of the matcher against human labels, per threshold pair.

    ``labelled_rows`` are the merged exports: a row with a ground-truth id is a true match,
    a row without one is a true non-match. Recall is measured over the true matches only,
    which is what a matcher can miss; a "none" row it disagrees with shows up as a false
    positive instead.
    """
    truth = {(row["arxiv_id"], row["citation_key"]): row.get("groundtruth_arxiv_id")
             for row in labelled_rows}
    publications = sorted({row["arxiv_id"] for row in labelled_rows})
    by_bib, by_ref = group(bibtex_rows), group(reference_rows)
    thresholds = thresholds or [round(0.50 + 0.05 * step, 2) for step in range(11)]
    margins = margins or [0.0, 0.02, 0.05, 0.08, 0.10, 0.15, 0.20]

    curve = []
    for threshold in thresholds:
        for margin_floor in margins:
            predicted = {}
            for arxiv_id in publications:
                for row in label_publication(by_bib[arxiv_id], by_ref[arxiv_id],
                                             threshold=threshold, margin_floor=margin_floor):
                    predicted[(row["arxiv_id"], row["citation_key"])] = row["groundtruth_arxiv_id"]
            true_positive = sum(1 for key, expected in truth.items()
                                if expected and predicted.get(key) == expected)
            false_positive = sum(1 for key, expected in truth.items()
                                 if not expected and predicted.get(key))
            wrong_target = sum(1 for key, expected in truth.items()
                               if expected and predicted.get(key) and predicted.get(key) != expected)
            predicted_matches = sum(1 for value in predicted.values() if value)
            actual_matches = sum(1 for expected in truth.values() if expected)
            precision = (true_positive / predicted_matches) if predicted_matches else 0.0
            recall = (true_positive / actual_matches) if actual_matches else 0.0
            f1 = (2 * precision * recall / (precision + recall)) if precision + recall else 0.0
            curve.append(dict(
                threshold=threshold, margin=margin_floor,
                precision=round(precision, 4), recall=round(recall, 4), f1=round(f1, 4),
                accepted=predicted_matches, false_positive=false_positive,
                wrong_target=wrong_target, actual_matches=actual_matches,
            ))
    return curve


def best_setting(curve, minimum_precision=0.90):
    """The threshold pair with the best F1 among those meeting a precision floor."""
    eligible = [row for row in curve if row["precision"] >= minimum_precision] or curve
    return max(eligible, key=lambda row: (row["f1"], row["precision"], -row["threshold"]))


def pool_statistics(rows, summary):
    """Numbers worth reporting: how much the matcher actually labelled."""
    decisions = Counter(row["decision"] for row in rows)
    methods = Counter(row["method"] for row in rows if row["decision"] == MATCHED)
    per_publication = Counter(stats["matched"] for stats in summary.values())
    return dict(
        publications=len(summary),
        entries=len(rows),
        matched=decisions[MATCHED],
        unmatched=decisions[UNMATCHED],
        match_rate=round(decisions[MATCHED] / len(rows), 4) if rows else 0.0,
        methods=dict(methods),
        publications_with_matches=sum(1 for stats in summary.values() if stats["matched"]),
        matches_per_publication={str(k): v for k, v in sorted(per_publication.items())},
    )
