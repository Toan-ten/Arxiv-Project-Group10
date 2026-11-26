# ADR-002: A unique recorded bibliography jobname may select the document root

## Status

Proposed — needs team sign-off before implementation.

## Date

2026-11-26

## Context

The corpus-wide run over 5,000 papers (commit `93c4ef6b`, `out-5000`) rejected
**137 versions** with `Expected exactly one documentclass root, found N`. Of those,
**93 are duplicate copies of one document**: the same class declared more than once
because the upload also contains an auxiliary document. Paper `2508.08080` is the
canonical case — `main_resubmission.tex` (76,468 bytes, the paper) beside
`plots.tex` (5,979 bytes, a standalone pgfplots script that regenerates a figure).

The parser's current stance is to refuse when more than one root survives the
existing role checks (equivalent copies, included fragments, supplements, auxiliary
documents). That stance is deliberate: picking the wrong root produces tables that
look complete and are silently wrong, which is the worst outcome in the damage
ordering recorded in the M2 backlog.

Measured discriminators on those 137 versions:

| Discriminator | Versions where exactly one candidate satisfies it |
|---|---:|
| Candidate stem matches a `.bbl` jobname recorded by M1 for this version | **84** |
| One candidate is more than 2× the next in body bytes | 83 |
| Both of the above agree | 62 |
| Unique candidate containing `\bibliography` or `\begin{thebibliography}` | 55 |
| Unique candidate containing `\end{document}` | 3 |

103 of the 137 have exactly two candidates; the rest have 3–10.

The first row is not a heuristic. M1 records the bibliography that arXiv's bibtex
produced, whose **filename is the jobname of the document that was actually
compiled** (`main.bbl`, `aies_paper.bbl`). When exactly one root candidate's stem
matches that jobname, the compilation itself tells us which file was the document.

## Decision

When more than one root candidate survives the existing role checks, select a root
**only** if exactly one candidate's stem matches a `.bbl` filename recorded for that
version by M1. Every other case keeps failing with `Expected exactly one
documentclass root`, now naming the candidates.

The rule is bounded by construction:

- it fires only on exact stem equality with a recorded jobname;
- it fires only when exactly one candidate matches;
- it never inspects size, filenames such as `main`, or content heuristics;
- a version without a recorded bibliography is unaffected.

This is the same standard ADR-001 set for counter prints: a derived value may be
emitted only when it is fully determined inside a bounded, verifiable scope.

## Alternatives

**Choose the largest candidate by body bytes (83 versions).** Rejected: size is a
correlation, not evidence. `plots.tex` is small today, but a paper's own appendix or
a merged submission would be chosen wrongly, and the resulting tables would look
plausible.

**Choose the candidate containing the bibliography (55 versions).** Rejected: it
covers fewer versions than the jobname rule and it misleads on documents that cite
from a shared `.bib` without a bibliography section, which the corpus shows is
common.

**Prefer filenames like `main.tex` or `paper.tex`.** Rejected as name guessing. It is
also directly contradicted by the data: in `2508.08080` the recorded jobname is
`main` while neither candidate is called `main.tex`.

**Keep refusing everything (status quo).** Retained as the fallback for every case
the rule does not resolve — 53 of the 137 versions, including `2508.08080` itself,
where no candidate matches the recorded jobname.

## Consequences

Coverage improves on up to 84 versions (about +1.2 points of the 7,280-version KPI)
without weakening the refusal anywhere else, and the new failure message names the
competing files so the remaining cases are triageable.

The rule depends on M1's recorded bibliographies, now placed at
`<paper>/bbl/<vN>/source_bbl/` by PR #32. A version with no recording, or with an
ambiguous pair of recordings, is unaffected and still refuses.

It also introduces a subtle asymmetry worth stating: the root can be chosen from
bibliography evidence even when that bibliography is later rejected for another
reason. The root choice is about which file was compiled; whether its bibliography
is usable remains a separate question that fails on its own terms.

## Acceptance check

Implementing this requires a measurement, not just a green suite: on the 137
versions, count conversions, and verify that every converted version's chosen root
matched the recorded jobname and that no version lost its previous status for
another reason. As with ADR-001, the number to watch is fabricated success — a
version that succeeds with the wrong document's content — and the sample must be
read by hand, not only counted.
