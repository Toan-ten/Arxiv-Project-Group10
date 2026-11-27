"""Baseline reference matcher for M3.

M3 matches M2 BibTeX entries (per publication) against the crawled references
(M1) of the same publication. This module provides a transparent, dependency-free
baseline scorer and evaluation (MRR@k). It does not execute TeX or call APIs.
"""
from __future__ import annotations

import re
import unicodedata


# LaTeX markup that survives into a BibTeX title and defeats a string comparison.
_TEX_COMMAND = re.compile(r"\\[a-zA-Z]+\*?")
_TEX_CONTROL = re.compile(r"\\[^a-zA-Z]")
_TEX_GROUP = re.compile(r"[{}]")
_MATH_DELIMITER = re.compile(r"\$+")
_SUBSCRIPT = re.compile(r"[_^~]")


def strip_latex_markup(text):
    """Drop TeX markup so a title reads the way the reference writes it.

    The corpus cites the same paper as ``$\\mathrm{Sr_2RuO_4}$`` in one bibliography and
    ``Sr2RuO4`` in another. Removing the delimiters, the command names and the subscript
    markers (rather than turning them into spaces) makes those two titles identical, which
    matters twice: the matcher stops calling a perfect match a near miss, and a labeller is
    not asked to adjudicate between two titles that differ only in markup.
    """
    if not text:
        return ""
    out = _MATH_DELIMITER.sub("", str(text))
    out = _TEX_COMMAND.sub("", out)
    out = _TEX_CONTROL.sub("", out)
    out = _TEX_GROUP.sub("", out)
    return _SUBSCRIPT.sub("", out)


def fold_accents(text):
    """Strip diacritics so ``Bl\\"ugel`` and ``Blügel`` compare equal."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")


def normalize_title(title):
    if not title:
        return ""
    text = fold_accents(strip_latex_markup(title)).lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _norm_authors(authors):
    if not authors:
        return set()
    if isinstance(authors, str):
        parts = re.split(r"\s+and\s+|,\s*|\s*;\s*", authors)
    else:
        parts = list(authors)
    out = set()
    for p in parts:
        p = unicodedata.normalize("NFKC", str(p)).lower().strip()
        if p:
            # keep last token as surname proxy
            toks = p.split()
            out.add(toks[-1] if toks else p)
    return out


def jaccard_authors(a, b):
    sa, sb = _norm_authors(a), _norm_authors(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _year_match(ya, yb):
    if ya is None or yb is None:
        return 0.0
    try:
        return 1.0 if int(ya) == int(yb) else 0.0
    except (TypeError, ValueError):
        return 1.0 if str(ya).strip() == str(yb).strip() else 0.0


def score_candidate(bibtex_entry, ref):
    """Return a score in [0,1] for one (bibtex, reference) pair."""
    score = 0.0
    # DOI exact match is strongest.
    doi_b = (bibtex_entry.get("doi") or "").strip().lower()
    doi_r = (ref.get("doi") or ref.get("ref_doi") or "").strip().lower()
    if doi_b and doi_r and doi_b == doi_r:
        return 1.0
    title_b = normalize_title(bibtex_entry.get("title"))
    title_r = normalize_title(ref.get("title") or ref.get("ref_title"))
    if title_b and title_r:
        if title_b == title_r:
            score += 0.6
        else:
            # token overlap
            tb, tr = set(title_b.split()), set(title_r.split())
            if tb and tr:
                score += 0.6 * (len(tb & tr) / len(tb | tr))
    score += 0.25 * jaccard_authors(
        bibtex_entry.get("authors"), ref.get("authors") or ref.get("ref_authors"))
    score += 0.15 * _year_match(
        bibtex_entry.get("year"), ref.get("year") or ref.get("ref_submission_date")
    )
    return min(1.0, score)


def build_candidates(bibtex_list, refs_list):
    """Return list of (bibtex_entry, [(ref, score), ...]) sorted desc by score."""
    out = []
    for be in bibtex_list or []:
        scored = sorted(
            ((r, score_candidate(be, r)) for r in (refs_list or [])),
            key=lambda x: x[1],
            reverse=True,
        )
        out.append((be, scored))
    return out


def match_topk(bibtex_list, refs_list, k=5):
    """Return list of (bibtex_entry, [ref, ...]) top-k refs per entry."""
    result = []
    for be, scored in build_candidates(bibtex_list, refs_list):
        result.append((be, [r for r, _ in scored[:k]]))
    return result


def mrr_at_k(predictions, ground_truth, k=5):
    """Mean Reciprocal Rank at k.

    predictions: list of lists of predicted ref ids (or ref objects) per query.
    ground_truth: list of ground-truth ref ids (one per query) or list of sets.
    Comparison uses equality/hashability; pass ids for clarity.
    """
    if not predictions or not ground_truth:
        return 0.0
    n = min(len(predictions), len(ground_truth))
    total = 0.0
    for i in range(n):
        gt = ground_truth[i]
        gts = gt if isinstance(gt, (set, frozenset, list, tuple)) else {gt}
        gset = set(gts)
        for rank, pred in enumerate(predictions[i][:k], start=1):
            if pred in gset:
                total += 1.0 / rank
                break
    return total / n if n else 0.0
