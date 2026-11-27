"""Draft suggestions and the offline page that wraps M3's manual labelling.

The statement asks for human-labelled ground truth over an ``m x n`` candidate matrix:
one row per BibTeX entry, at least 20 entries per manual publication and ``100 * k``
entries for a group of ``k`` members. Two things make that bearable - a suggestion that
carries its own evidence, and a page that shows the entry next to every candidate so a
decision is one keystroke instead of a literature search.

The suggestions here are drafts. A row reaches the exported labels only after a member
confirms or corrects it on the page; an unconfirmed draft is thrown away with the page,
which is why :func:`validate_labels` refuses to accept rows that no reviewer touched.

Scoring is deliberately a fixed linear combination of the features in
:mod:`src.m3.matrix` rather than the learned model: the drafts exist to order human
attention, and a suggestion a labeller cannot interrogate would be worse than none. The
learned ranker is a separate deliverable and is scored against these labels.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .matrix import pair_features, split_authors, surnames

# Weights are a judgement call, documented so the page can explain every suggestion. The
# five similarities sum to 0.80 and the three bonuses to 0.20, so a perfect match scores
# exactly 1.0 and nothing saturates early: two candidates that both look strong must remain
# distinguishable, because a matcher that cannot separate an exact title from a near copy
# picks between them by accident.
SCORE_WEIGHTS = dict(
    title_sequence_ratio=0.34,
    title_token_jaccard=0.20,
    title_char_ngram_cosine=0.16,
    author_jaccard=0.06,
    first_author_match=0.04,
)
TITLE_BONUS = 0.10          # a title that agrees this closely is already evidence
FIRST_AUTHOR_BONUS = 0.05   # the same first author as the entry
YEAR_BONUS = 0.05           # publication year within one year of the submission
HIGH_SCORE, HIGH_MARGIN = 0.70, 0.10
MEDIUM_SCORE, MEDIUM_MARGIN = 0.55, 0.05

# Bulk thresholds. A row is batch-decidable when the evidence is so lopsided that reading it
# row by row buys nothing, and batch-decidable rows are the reason 469 decisions do not have
# to cost 469 readings. Anything in between stays in the read queue.
BULK_TITLE = 0.97        # near-verbatim title
BULK_NONE_SCORE = 0.35   # nothing even remotely close

LABELLING_RULES = [    "One decision per entry: the reference that is the same work, or None when the "
    "cited work is not among this publication's crawled references.",
    "A published journal or conference version matched to its arXiv preprint counts as "
    "the same work.",
    "Title alone can be misleading when a publication cites several near-identical "
    "titles; weigh author and year before you accept.",
    "Accepted or corrected rows are the labels. Skipped entries are not exported.",
]


def _usable(value):
    return value not in (None, "", "None", "[]")


def heuristic_score(features):
    """Suggestion score in [0, 1] from the ten cell features."""
    score = sum(weight * features[name] for name, weight in SCORE_WEIGHTS.items())
    if features["title_sequence_ratio"] >= 0.85:
        score += TITLE_BONUS
    if features["first_author_match"]:
        score += FIRST_AUTHOR_BONUS
    if features["year_match"]:
        score += YEAR_BONUS
    return round(min(1.0, score), 4)


def _numbers(text):
    return set(re.findall(r"\d+", text or ""))


def number_mismatch(entry_title, reference_title):
    """Whether the two titles disagree on a number.

    arXiv carries long series of near-identical titles by the same group - "Stability and
    Bifurcation in a Discrete Phytoplankton-Zooplankton Model ... Part I" and its Part II -
    where the text agrees almost perfectly and the number is the only difference. A title
    score cannot separate those, so a differing number is treated as positive evidence of
    two different works.
    """
    entry_numbers, ref_numbers = _numbers(entry_title), _numbers(reference_title)
    return bool(entry_numbers and ref_numbers and entry_numbers != ref_numbers)


def bulk_classification(pick, score, top_seq, shared_surnames, number_conflict=False):
    """Whether a row can be decided in bulk, and which way.

    ``accept`` needs three independent agreements - a near-verbatim title, a shared surname
    and no contradictory number - because any one of them alone can be a coincidence.
    ``none`` needs the opposite: no candidate above the plausibility floor at all. Everything
    else is a row a human has to read, which is where the genuine ambiguity lives.
    """
    if pick and top_seq >= BULK_TITLE and shared_surnames > 0 and not number_conflict:
        return "accept"
    if not pick and score < BULK_NONE_SCORE:
        return "none"
    return "read"


def confidence_tier(score, margin, features, number_conflict=False):
    """How much of a labeller's attention this row deserves."""
    if number_conflict:
        return "low"
    evidence = features["author_jaccard"] > 0 or features["year_match"]
    if score >= HIGH_SCORE and margin >= HIGH_MARGIN and evidence:
        return "high"
    if score >= MEDIUM_SCORE and margin >= MEDIUM_MARGIN:
        return "medium"
    return "low"


def rationale(entry, reference, features, surname_hits):
    """The evidence behind a suggestion, in the words the page shows."""
    parts = [f"title {features['title_sequence_ratio']:.2f} "
             f"(tokens {features['title_token_jaccard']:.2f})"]
    if surname_hits:
        parts.append(f"{surname_hits} shared surname(s)")
    elif features["entry_has_authors"] and features["ref_has_authors"]:
        parts.append("no shared surname")
    entry_year = entry.get("year")
    ref_year = (reference.get("ref_submission_date") or "")[:4] or "?"
    parts.append(f"year {entry_year or '?'} vs {ref_year}")
    return " · ".join(parts)


def display_authors(authors):
    """Readable author line, one element per author where the source allows it."""
    names = []
    for element in split_authors(authors):
        if "," in element:
            parts = [p.strip() for p in element.split(",") if p.strip()]
            name = parts[0] if parts else element
            initials = " ".join(p for p in parts[1:] if p)
            names.append(f"{name}, {initials}" if initials else name)
        else:
            names.append(element)
    return "; ".join(names)


def entry_card(entry):
    return dict(
        key=entry["citation_key"],
        type=entry.get("entry_type") or "",
        title=(entry.get("title") or "").strip(),
        authors=display_authors(entry.get("authors")),
        year=entry.get("year"),
        doi=(entry.get("doi") or "").strip() or None,
        venue=(entry.get("journal") or "").strip() or None,
        url=(entry.get("url") or "").strip() or None,
        labelable=bool(_usable(entry.get("title")) and _usable(entry.get("authors"))),
    )


def reference_card(reference):
    return dict(
        id=reference["ref_arxiv_id"],
        title=(reference.get("ref_title") or "").strip(),
        authors=display_authors(reference.get("ref_authors")),
        year=(reference.get("ref_submission_date") or "")[:4] or None,
        date=(reference.get("ref_submission_date") or "") or None,
    )


def draft_suggestions(entries, references):
    """One suggestion per entry, carrying the pick, its score and its evidence."""
    suggestions = []
    for entry in entries:
        scored = []
        for reference in references:
            features = pair_features(entry, reference)
            scored.append((heuristic_score(features), features, reference))
        scored.sort(key=lambda item: (-item[0], item[2]["ref_arxiv_id"]))
        top_score, top_features, top_reference = scored[0]
        runner_up = scored[1][0] if len(scored) > 1 else 0.0
        margin = round(top_score - runner_up, 4)
        conflict = number_mismatch(entry.get("title"), top_reference.get("ref_title"))
        tier = confidence_tier(top_score, margin, top_features, conflict)
        entry_surnames = surnames(entry.get("authors"))
        shared = entry_surnames & surnames(top_reference.get("ref_authors"))
        evidence = rationale(entry, top_reference, top_features,
                             f"{len(shared)} of {len(entry_surnames)}")
        if conflict:
            evidence += " · titles disagree on a number, so this is a different work"
        pick = top_reference["ref_arxiv_id"] if tier != "low" else None
        suggestions.append(dict(
            key=entry["citation_key"],
            pick=pick,
            best=top_reference["ref_arxiv_id"],
            score=top_score,
            margin=margin,
            tier=tier,
            top_seq=round(top_features["title_sequence_ratio"], 4),
            shared_surnames=len(shared),
            total_surnames=len(entry_surnames),
            bulk=bulk_classification(pick, top_score, top_features["title_sequence_ratio"],
                                     len(shared), conflict),
            why=evidence,
            candidates=[dict(
                id=reference["ref_arxiv_id"],
                score=score,
                seq=round(features["title_sequence_ratio"], 4),
                tokens=round(features["title_token_jaccard"], 4),
                same_first_author=bool(features["first_author_match"]),
                year_match=bool(features["year_match"]),
            ) for score, features, reference in scored],
        ))
    return suggestions


def publication_card(arxiv_id, entries, references, member=None):
    entries = sorted(entries, key=lambda row: row["citation_key"])
    references = sorted(references, key=lambda row: row["ref_arxiv_id"])
    suggestions = draft_suggestions(entries, references)
    order = {tier: rank for rank, tier in enumerate(("high", "medium", "low"))}
    ranked = sorted(suggestions, key=lambda s: (order[s["tier"]], -s["score"], s["key"]))
    return dict(
        arxiv_id=arxiv_id,
        member=member,
        entries=[entry_card(entry) for entry in entries],
        references=[reference_card(reference) for reference in references],
        draft=[dict(suggestion) for suggestion in suggestions],
        difficulty=[suggestion["key"] for suggestion in ranked],
    )


def quota(members):
    """The statement's per-group quantities for ``members`` members."""
    return dict(
        members=members,
        publications=5 * members,
        entries=100 * members,
        entries_per_publication=20,
        auto_publications_floor=2 * members,
        test_manual=members,
        test_auto=members,
        valid_manual=members,
        valid_auto=members,
    )


def build_payload(bibtex_rows, reference_rows, shortlist_rows, members=4, names=None,
                  generated_at=None):
    """Everything the page needs: publications, cards, drafts and the quota they satisfy."""
    wanted = [row["arxiv_id"] for row in shortlist_rows]
    by_bib, by_ref = {}, {}
    for row in bibtex_rows:
        # An entry carrying neither title nor authors cannot be labelled honestly, so it
        # stays out of the manual pool and simply remains unlabelled downstream.
        if _usable(row.get("title")) and _usable(row.get("authors")):
            by_bib.setdefault(row["arxiv_id"], []).append(row)
    for row in reference_rows:
        by_ref.setdefault(row["arxiv_id"], []).append(row)

    names = list(names or []) or [f"member{i + 1}" for i in range(members)]
    per_member = -(-len(wanted) // len(names)) if wanted else 0
    publications = []
    for index, arxiv_id in enumerate(sorted(wanted)):
        member = names[min(index // per_member, len(names) - 1)] if per_member else None
        publications.append(publication_card(arxiv_id, by_bib[arxiv_id], by_ref[arxiv_id], member))

    entries = sum(len(card["entries"]) for card in publications)
    labelable = sum(1 for card in publications for entry in card["entries"] if entry["labelable"])
    return dict(
        generated_at=generated_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        quota=quota(len(names)),
        members=names,
        rules=LABELLING_RULES,
        publications=publications,
        summary=dict(
            publications=len(publications),
            entries=entries,
            labelable_entries=labelable,
            references=sum(len(card["references"]) for card in publications),
            cells=sum(len(card["entries"]) * len(card["references"]) for card in publications),
            per_member_entries=dict(Counter(card["member"] for card in publications
                                            for _ in card["entries"])),
            tiers=dict(Counter(draft["tier"] for card in publications for draft in card["draft"])),
            bulk=dict(Counter(draft["bulk"] for card in publications for draft in card["draft"])),
        ),
    )


def render_page(payload, template_path):
    """Fill the single-file page template with the payload."""
    template = Path(template_path).read_text(encoding="utf-8")
    serialised = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    # A JSON payload inside <script> must not be able to close the element.
    serialised = (serialised.replace("</", "<\\/")
                  .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))
    if "__PAYLOAD__" not in template:
        raise ValueError("template is missing the __PAYLOAD__ placeholder")
    return template.replace("__PAYLOAD__", serialised)


def validate_labels(rows, expected_publications=None, minimum_entries=400):
    """Whether an exported label set can be submitted, and what is missing.

    Refuses rows without a reviewer: the manual pool is ground truth, so a row nobody
    signed is not a label.
    """
    problems = []
    unsigned = [row for row in rows if not (row.get("reviewer") or "").strip()]
    if unsigned:
        problems.append(f"{len(unsigned)} row(s) carry no reviewer")
    keys = [(row.get("arxiv_id"), row.get("citation_key")) for row in rows]
    duplicated = [key for key, count in Counter(keys).items() if count > 1]
    if duplicated:
        problems.append(f"{len(duplicated)} entry/entries labelled twice, e.g. {duplicated[0]}")
    publications = {row.get("arxiv_id") for row in rows}
    if expected_publications is not None and len(publications) != expected_publications:
        problems.append(f"{len(publications)} publication(s) labelled, "
                        f"{expected_publications} required")
    if len(rows) < minimum_entries:
        problems.append(f"{len(rows)} label(s), {minimum_entries} required")
    per_publication = Counter(row.get("arxiv_id") for row in rows)
    thin = {aid: count for aid, count in per_publication.items() if count < 20}
    if thin:
        problems.append(f"{len(thin)} publication(s) below 20 labels, e.g. "
                        f"{next(iter(thin.items()))}")
    matched = sum(1 for row in rows if row.get("groundtruth_arxiv_id"))
    return dict(
        ok=not problems,
        problems=problems,
        publications=len(publications),
        labels=len(rows),
        matched=matched,
        none_rows=len(rows) - matched,
        per_publication=dict(per_publication),
        reviewers=dict(Counter(row.get("reviewer") for row in rows)),
        # How each row was decided: read one by one, or taken in bulk. The split is what
        # makes an audit possible - "we read 25% and batched the rest" is a claim a reviewer
        # can check, while "we read everything" is not verifiable at all.
        via=dict(Counter(row.get("via") or "read" for row in rows)),
    )
