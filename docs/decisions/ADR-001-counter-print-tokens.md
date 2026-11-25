# ADR-001: Counter prints stay literal, never evaluated

## Status

Accepted

## Date

2026-11-25

## Context

`\the<counter>` (`\theprocedure`, `\thefootnote`, `\theequation`, …) appears in prose
when a source prints the current value of a counter, typically in a label such as
`\textbf{Procedure \theprocedure}` next to `\refstepcounter{procedure}`. The parser
rejected it as `Unsupported prose command`, which blocked 45 of the 68 remaining parse
errors in the measured cascade subset and a comparable share of the corpus.

The value is decided at compile time, and three facts from the measured corpus matter:

- **The rendering is author-defined.** 1 072 of the 4 800 papers in the range
  (`22%`) redefine a `\the<counter>`; the common cases are `\def\thefootnote{*}`
  for equal-contribution footnotes and `\def\thefootnote{\arabic{footnote}}`.
  A single default assumption (`\arabic`) would be wrong for almost a quarter of
  the corpus.
- **Values are not statically determined.** `\setcounter`, conditionals, loops,
  multiple compilation passes and package hooks can all change what a counter prints,
  and the parser deliberately does not execute TeX.
- **A precedent for tokens already exists.** The `elements.content` column keeps
  `\ref{k}` and `\cite{key}` as literal tokens; existing tests assert this. Reference
  identity matters to Milestone 3, so the tokens are preserved rather than resolved.

The milestone statement asks for prose leaves and a correct parent-child structure. It
does not require that a printed number be reconstructed, and the table schema stores
`content` as text.

## Decision

Accept `\the<counter>` as a **literal token** in `content`, under two guards:

1. The counter must be declared by the same source (`\newcounter{<name>}`), or be one of
   the LaTeX kernel counters that always exist (`thesection`, `thefigure`, `theequation`,
   `thefootnote`, `thepage`, `theenumi`…). An undeclared `\the<name>` still fails: it is
   far more likely a typo or a macro the parser could not register than a counter print.
2. The command must not be a locally defined macro. A source-defined
   `\newcommand{\theprocedure}{…}` wins and is expanded as usual.

The parser never evaluates the counter. No `\arabic`, no `\Alph`, no reset tracking, no
ordering assumptions. Each accepted token records a `counter_marker` entry in
`layout_metadata` so the interpretation stays visible in the run evidence.

## Alternatives Considered

### Keep rejecting it
- Pros: zero risk, no change to the parser's strictness.
- Cons: leaves 45 of the 68 remaining errors in the measured subset untouched; these
  versions are otherwise perfectly parseable.
- Rejected: the constructs are unambiguous in the source, and refusing them protects
  against nothing that the guards below do not already protect.

### Evaluate the value when it is "obviously" knowable
Resolve `\newcounter{x}` + `\refstepcounter{x}` in document order to 1, 2, 3…, aborting
when `\the<x>` is redefined or `\setcounter` targets it.
- Pros: produces readable prose ("Procedure 3") instead of a token.
- Cons: requires a miniature TeX counter engine (number formats, resets, ordering,
  package-defined `\the…` bodies) and fails exactly on the 22% of sources that override
  the format. The failure mode is the problem: a fabricated `3` reads like prose and is
  invisible to review, while a literal `\theprocedure` is visibly a token.
- Rejected: the parser's rule is that a derived value may only be emitted when it is
  fully determined inside a bounded, verifiable scope. Here it is not, and the cost of
  being wrong is silent data corruption.

## Consequences

- Parse coverage improves on versions whose only remaining blocker is a counter print,
  without any claim that the printed numbers are known.
- `content` contains tokens like `\theprocedure` where a reader of the PDF sees a number.
  That is visible and reviewable; downstream analysis can strip known token forms if it
  needs to, and Milestone 3 does not consume this column for matching.
- The missing half of this gap is registering TeX primitives such as `\def`. A source
  that defines `\def\thefootnote{*}` currently falls back to the kernel-counter whitelist
  and stays literal, rather than expanding to `*`. Fixing that is a separate change in the
  gatherer; this ADR does not depend on it.
- If a future milestone reports that counter labels must be readable, the smallest
  defensible evaluation is the one in the rejected alternative above, with its guards
  made exhaustive. That would supersede this ADR rather than extend it silently.
