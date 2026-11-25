"""M3: reference matching between M2 BibTeX and M1 crawled references."""
from .corpus import join_publications, load_bibtex, load_references
from .matching import (
    normalize_title,
    jaccard_authors,
    score_candidate,
    build_candidates,
    match_topk,
    mrr_at_k,
)
from .pipeline import run_matching

__all__ = [
    "load_references",
    "load_bibtex",
    "join_publications",
    "normalize_title",
    "jaccard_authors",
    "score_candidate",
    "build_candidates",
    "match_topk",
    "mrr_at_k",
    "run_matching",
]
