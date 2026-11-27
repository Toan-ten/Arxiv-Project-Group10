"""Build the M3 candidate matrix and choose publications worth labelling.

The M3 statement frames matching as an ``m x n`` binary matrix per publication: ``m``
BibTeX entries from Milestone 2 against ``n`` arXiv-keyed references crawled in
Milestone 1. This module produces those cells without labels, plus the per-cell
features the model will consume, and the shortlist of publications whose entries
actually carry enough text for a human to label honestly.
"""
from __future__ import annotations

import difflib
import json
import math
import re
from collections import Counter
from pathlib import Path

from .matching import _year_match, normalize_title

FEATURE_COLUMNS = [
    "title_sequence_ratio",
    "title_token_jaccard",
    "title_char_ngram_cosine",
    "author_jaccard",
    "first_author_match",
    "year_match",
    "doi_exact",
    "entry_has_title",
    "entry_has_authors",
    "ref_has_authors",
]


def _usable(value):
    return value not in (None, "", "None", "[]")


def tokens(text):
    return {t for t in re.split(r"[^a-z0-9]+", (text or "").lower()) if len(t) > 1}


def char_ngrams(text, n=3):
    flat = " " + re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip() + " "
    return Counter(flat[i:i + n] for i in range(max(0, len(flat) - n + 1)))


def cosine(a, b):
    if not a or not b:
        return 0.0
    common = set(a) & set(b)
    numerator = sum(a[g] * b[g] for g in common)
    denominator = math.sqrt(sum(v * v for v in a.values())) * math.sqrt(sum(v * v for v in b.values()))
    return numerator / denominator if denominator else 0.0


def split_authors(authors):
    """Author strings, tolerating a serialised list as well as a real list."""
    if not authors:
        return []
    if isinstance(authors, str):
        text = authors.strip()
        if text.startswith("[") and text.endswith("]"):
            text = text[1:-1]
        return [p for p in re.split(r"['\"]\s*,\s*['\"]|\s+and\s+|;\s*", text) if p.strip(" '\"")]
    return [str(a) for a in authors]


def surnames(authors):
    """Surname set for either ordering the corpus uses.

    Milestone 2 writes BibTeX authors surname-first ("Abdar, Moloud") while Milestone 1
    references are given-first ("Evdoxia Taka"), so taking the last token - which is what
    matching._norm_authors does - compares a given name against a surname.
    """
    out = set()
    for author in split_authors(authors):
        cleaned = re.sub(r"\s+", " ", author).strip(" '\"\t")
        if not cleaned:
            continue
        if "," in cleaned:
            out.add(cleaned.split(",", 1)[0].strip().lower())
        else:
            out.add(cleaned.split()[-1].lower())
    return out


def first_surname(authors):
    out = split_authors(authors)
    if not out:
        return None
    cleaned = re.sub(r"\s+", " ", out[0]).strip(" '\"\t")
    if not cleaned:
        return None
    return cleaned.split(",", 1)[0].strip().lower() if "," in cleaned else cleaned.split()[-1].lower()


def pair_features(entry, reference):
    """Features for one (entry, reference) cell. Every one is used by the final model."""
    entry_title, ref_title = normalize_title(entry.get("title")), normalize_title(reference.get("ref_title"))
    entry_authors = surnames(entry.get("authors"))
    ref_authors = surnames(reference.get("ref_authors"))
    entry_tokens, ref_tokens = tokens(entry_title), tokens(ref_title)
    entry_doi, ref_doi = (entry.get("doi") or "").strip().lower(), (reference.get("ref_doi") or "").strip().lower()
    return {
        "title_sequence_ratio": difflib.SequenceMatcher(None, entry_title, ref_title).ratio(),
        "title_token_jaccard": (len(entry_tokens & ref_tokens) / len(entry_tokens | ref_tokens)
                                if entry_tokens and ref_tokens else 0.0),
        "title_char_ngram_cosine": cosine(char_ngrams(entry_title), char_ngrams(ref_title)),
        "author_jaccard": (len(entry_authors & ref_authors) / len(entry_authors | ref_authors)
                           if entry_authors and ref_authors else 0.0),
        "first_author_match": (1.0 if first_surname(entry.get("authors")) and
                               first_surname(entry.get("authors")) in ref_authors else 0.0),
        "year_match": _year_match(entry.get("year"), (reference.get("ref_submission_date") or "")[:4] or None),
        "doi_exact": 1.0 if entry_doi and ref_doi and entry_doi == ref_doi else 0.0,
        "entry_has_title": 1.0 if _usable(entry.get("title")) else 0.0,
        "entry_has_authors": 1.0 if _usable(entry.get("authors")) else 0.0,
        "ref_has_authors": 1.0 if _usable(reference.get("ref_authors")) else 0.0,
    }


def candidate_cells(entries, references):
    """One row per (entry, reference) cell, in the order the statement describes."""
    cells = []
    for entry in entries:
        for reference in references:
            cell = dict(arxiv_id=entry["arxiv_id"], citation_key=entry["citation_key"],
                        ref_arxiv_id=reference.get("ref_arxiv_id"))
            cell.update(pair_features(entry, reference))
            cells.append(cell)
    return cells


def publication_profile(entries, references):
    """Whether a publication can support honest labelling, and how large its matrix is."""
    labelable = [e for e in entries if _usable(e.get("title")) and _usable(e.get("authors"))]
    return dict(
        arxiv_id=(entries[0]["arxiv_id"] if entries else None),
        entries=len(entries),
        labelable_entries=len(labelable),
        references=len(references),
        cells=len(entries) * len(references),
        labelable_cells=len(labelable) * len(references),
    )


def group(rows):
    out = {}
    for row in rows:
        out.setdefault(row["arxiv_id"], []).append(row)
    return out


def select_labeling_publications(entries, references, minimum_entries=20, maximum_entries=60,
                                 minimum_references=2, maximum_references=40, limit=None,
                                 seed_pool=None):
    """Publications where >= ``minimum_entries`` entries carry title and authors.

    Labelling an entry that carries only a key and a year would be guesswork, so the
    shortlist is filtered on the material a labeller actually reads.
    """
    by_bib, by_ref = group(entries), group(references)
    profiles = [publication_profile(by_bib[aid], by_ref[aid])
                for aid in sorted(set(by_bib) & set(by_ref))]
    eligible = [p for p in profiles
                if p["labelable_entries"] >= minimum_entries and p["references"] >= minimum_references
                and p["labelable_entries"] <= maximum_entries and p["references"] <= maximum_references]
    # A human labels every entry of a publication, so the cheapest publication that
    # still clears the per-publication floor is the best one to give a labeller.
    eligible.sort(key=lambda p: (p["labelable_entries"] * p["references"], p["arxiv_id"]))
    if seed_pool is not None:
        eligible = [p for p in eligible if p["arxiv_id"] in seed_pool]
    return (eligible[:limit] if limit else eligible), profiles


def write_matrix(entries, references, destination):
    """Persist the label-free candidate matrix as Parquet, sharded by nothing yet."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    cells = candidate_cells(entries, references)
    table = pa.Table.from_pylist(cells)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, destination)
    return dict(cells=len(cells), path=str(destination))


if __name__ == "__main__":
    import pyarrow.parquet as pq

    import sys

    refs = pq.read_table(sys.argv[1]).to_pylist()
    bib = pq.read_table(sys.argv[2]).to_pylist()
    shortlist, profiles = select_labeling_publications(bib, refs)
    summary = dict(
        publications=len(profiles),
        eligible=len(shortlist),
        cells_total=sum(p["cells"] for p in profiles),
        labelable_cells=sum(p["labelable_cells"] for p in profiles),
        shortlist=shortlist[:25],
    )
    print(json.dumps(summary, indent=2))
