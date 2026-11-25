"""M3: reference matching between M2 BibTeX and M1 crawled references."""
from .matching import (
    normalize_title,
    jaccard_authors,
    score_candidate,
    build_candidates,
    match_topk,
    mrr_at_k,
)

__all__ = [
    "normalize_title",
    "jaccard_authors",
    "score_candidate",
    "build_candidates",
    "match_topk",
    "mrr_at_k",
]
