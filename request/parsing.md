---
title: "Project Milestone 2 — Hierarchical Parsing and Standardization"
short_title: "Milestone 2 — Parsing"
label: m2-parsing
description: "Transform raw LaTeX sources into a structured hierarchical store with cleaned, normalized, deduplicated content (elements, hierarchy) and unified BibTeX entries (bibtex)."
---

# Objectives

In this milestone, students will transition from simple data collection to advanced data processing and analysis. The focus is to transform the unstructured raw data, the LaTeX sources crawled in Milestone 1, into a structured hierarchical format with cleaned, normalized, and deduplicated content. This practice mimics the real-world data science workflow of unstructured text processing and standardization. The machine learning part of this workflow, the reference matching pipeline, follows in Milestone 3 and consumes the outputs produced here.

# Description

Students are required to implement an automatic parser that processes the scraped LaTeX source files from the previous Milestone. The processing pipeline consists of the following components:

## Multi-file Gathering

The content of a LaTeX source is often spread across multiple `.tex` files, for example through the `\input` or `\include` commands. Students must programmatically identify the main compilation path and parse only those files that are actually included in the final PDF rendering. Unused or redundant files found in the source directory must be ignored. Concretely, the publication root is the `.tex` file containing the `\documentclass` declaration. Follow `\input` and `\include` commands transitively, treating commands that are commented out as plain text rather than includes, so the files reachable from the root form the compilation path and everything else in the source directory is a redundant auxiliary file. Following this path reproduces what LaTeX itself would compile, and the parse therefore covers what a reader of the final PDF sees.

## Hierarchy Construction

#### Hierarchy Structure

The goal is to convert the raw LaTeX code into a hierarchical tree structure for each version of each publication.

- The **Document** acts as the root.

- **Chapters** or **Sections** typically comprise the second level.

- Other elements such as **Subsections**, **Paragraphs**, and subsequent logical divisions form the lower levels. Since the exact nesting order depends on the publication's specific formatting, the parser is required to automatically identify the appropriate hierarchical level for each such element.

#### Parsing Notes

To ensure consistency, the parser must adhere to specific rules regarding element granularity and exclusion:

- **Smallest Elements:** The leaf nodes of the hierarchy must be **Sentences**, meaning text separated by dots or periods, **Block Formulas**, meaning mathematical blocks such as the contents of an `equation` environment or of `$$...$$`, and **Figures**, with **Tables** also considered a type of Figure for this purpose. The itemized points are considered as branching into deeper levels, meaning that a block of `\begin{itemize}...\end{itemize}` is considered as a higher component with each point being a next-level element. Sentence leaves carry the prose content of body text: do not split inside mathematics, commands, abbreviations such as i.e. and et al. and Fig. 1, URLs, or decimal numbers, since a dot that does not end a sentence does not create a new leaf.

- **Exclusions:** References sections should **not** be parsed into the hierarchy.

- **Inclusions:** Acknowledgements and Appendices, often unnumbered through the `\section*` form, **must** be included.

## Standardization and Deduplication

#### Cleanup and Normalization

Students must clean and normalize the data within the hierarchy:

- **LaTeX Cleanup:** Remove unnecessary formatting commands, such as `\centering`, the `[htpb]` placement option, and `\midrule`, that do not contribute to semantic meaning.

- **Math Normalization:** Convert all inline mathematics to a unified format such as the `$...$` form, and convert all block mathematics to a unified format such as the `\begin{equation}...\end{equation}` form.

#### Reference Extraction

For citations defined using `\bibitem` within the `.tex` files, students must convert them into standard BibTeX entries using programmatic tools such as regular expressions.

#### Deduplication

Deduplication is required at two levels, and both levels operate *across versions* of a publication:

- **Reference Entries:** Duplicated reference entries inside each version or across different versions of a single publication should be properly handled. If different citation keys, the entry names, refer to the same underlying reference content, a single citation key should be chosen, and the `\cite{}` commands of the remaining entries must be renamed to this chosen key. Additionally, the deduplication process should unionize the fields of the duplicate entries rather than selecting one entry while discarding all other information. Consider one reference that survives through three versions: version 1 lists it as `vaswani2017` with title, author, and year, version 2 renames it to `vit_transformer` and adds `doi`, and version 3 keeps the same reference as `vit_transformer` but drops `year`. The unified entry then keeps one chosen key, unions the fields into title, author, year, and doi, and the `\cite` commands pointing to the dropped keys are renamed to the chosen key, which is exactly the row that lands in the `bibtex` table below. A worked example of the other level follows with the tables in the Requirement, where one shared sentence serves both versions as a single element.

- **Full-text Content:** Full-text content deduplication must be applied to the hierarchical elements. If an element's text content matches exactly across different versions, a full-text match after cleanup, it should be represented by a single identifier in the final output, reducing redundancy. Note: full-text matching should be performed *after* cleanup to remove discrepancies in minor details such as formatting or spacing that might otherwise cause the code to identify identical texts as different.

# Requirement

Process every publication produced in Milestone 1. For each publication, parse every version's LaTeX source, following the compilation path, build the hierarchical structure, standardize and deduplicate the content, and extract the references into a unified set of BibTeX entries. A version without TeX sources, kept as an empty record by Milestone 1, yields no parsed elements: record the version as empty rather than skipping it, so coverage statistics and the tables below reflect reality.

There is **no data submission** in this milestone: the processed corpus stays with the group as the working input of Milestone 3, just as the crawled corpus does in Milestone 1.

**Suggested data organization.** As in Milestone 1, groups organize their working data themselves. This suggestion adapts the same spirit to the parsed and standardized data, so follow it as-is or adapt it for convenience. For each publication, keep the parsed content and structure of *all* versions together: the parsed elements, the per-version parent relations between them, and the unified, deduplicated BibTeX entries. This replaces the raw TeX folder from Milestone 1 as the downstream material, so keep enough to assemble the final tables without re-parsing.

At the final submission in Milestone 3 this data turns into the tables of the published dataset. This milestone already specifies three of them:

| **Table** | **Columns (types) and contents** |
|:---|:---|
| elements | one row per *distinct* element across all versions of the publication: element_id (string, non-null), arxiv_id (string, non-null), kind (string, dictionary), content (string). Content that matches exactly across versions, a full-text match after cleanup, occupies a single row |
| hierarchy | arxiv_id (string, non-null), version (int), element_id (string, non-null), parent_id (string, non-null), with one row per node-parent edge of each version |
| bibtex | arxiv_id (string, non-null), citation_key (string, non-null), entry_type (string, dictionary), title (string), authors (list of strings), year (int, nullable), doi (string), journal (string), url (string), holding the unified BibTeX entries after deduplication and merging |
*Dataset tables specified in this milestone*

**How the elements and the hierarchy store across versions.** The elements of different versions share one store: when a leaf or component appears in two versions with identical cleaned content, it is recorded once, so a single `element_id` serves both versions, which is exactly the deduplication required above. The `hierarchy` rows then hold only the node-parent edges of each version, and an element contributed by an earlier version may act as a node in a later version's edges, attached to a parent that exists only in that later version. Element IDs begin with the publication key followed by a discriminant that is unique inside the publication. The miniature example below, one publication with two versions and one sentence unchanged across both, shows the resulting rows:

| **element_id**   | **kind** | **content**                            |
|:-----------------|:---------|:---------------------------------------|
| 2310-12345#root1 | document | Version 1 root                         |
| 2310-12345#root2 | document | Version 2 root                         |
| 2310-12345#h1    | heading  | Introduction                           |
| 2310-12345#h2    | heading  | Background and model                   |
| 2310-12345#s1    | sentence | We study the citation matching problem |
*Example `elements` rows, one shared store where `s1` serves both versions*

| **version** | **element_id** | **parent_id**    |
|:------------|:---------------|:-----------------|
| 1           | 2310-12345#h1  | 2310-12345#root1 |
| 1           | 2310-12345#s1  | 2310-12345#h1    |
| 2           | 2310-12345#h2  | 2310-12345#root2 |
| 2           | 2310-12345#s1  | 2310-12345#h2    |
*Example `hierarchy` rows: the shared sentence `s1` is a node in both versions, attached to `h1` in version 1 and to `h2` in version 2*

# Submission Format

## Source Code

This milestone's code is committed to the same single public GitHub repository used in Milestone 1, continuing the cumulative history that documents the group's progress. The repository contains only source files such as Python files with the `.py` or `.ipynb` extensions and excludes data files, compiled executables, and other non-source materials, together with:

```
<group-repo>/
|-- src # folder containing the source code
|   |-- *.{py/ipynb}
|   |-- requirements.txt # To re-produce the environment
|-- README.md # Instructions on environment setup and code execution
```

This milestone is submitted via Moodle as a group by pasting the public links to the shared code repository and to the milestone report described below. All the submissions above should be in the official language of your program. **NO late submission is permitted**.

## Report Requirements (Jupyter Book)

Students must prepare a milestone report compiled as a **Jupyter Book**, the same book repository introduced in Milestone 1 and hosted on its GitHub Pages, adding a parsing-and-standardization section to that book rather than creating a new one. The book must present the statistics and analysis of the processed data, and its figures and tables must be *generated during book compilation*, not pasted as static screenshots, for quality and interactivity. The report should specifically cover:

- **Hierarchical Parsing:** Explanation of the logic used to detect file structure, parse the LaTeX hierarchy, and handle multi-file inputs.

- **Standardization:** Details on how data cleaning, math normalization, and reference deduplication were implemented.

- **Statistics:** Relevant statistics such as the parsing success rate, the version coverage of every publication, and the element/entry counts before and after deduplication.

The parser's performance should also be illustrated through two main points, running time and memory footprint: running time counts the end-to-end wall time of the parser and the average time to process one publication, while memory footprint counts the maximum RAM used and the final output's storage size. The testbed for the final benchmarking is a Google Colab instance running on CPU-only mode. No upper limit on the running time is enforced, while the upper limit on maximum disk usage is given by the Colab instance.

The `README.md` of the repository stays separate: it provides clear instructions on setting up the necessary environment, such as the Python version and the packages to install through pip or conda, and running the code with specific configurations.

# Grading Criteria

Your grade is determined upon five main criteria as listed below:

- **Hierarchical Parser:** Accuracy of the parser in detecting structure, handling multi-file sources, and identifying the appropriate hierarchical levels.

- **Standardization & Deduplication:** Correct cleanup, math normalization, reference extraction, and both deduplication levels, covering reference entries through `\cite` renaming and field unionization as well as full-text content across versions.

- **Parser's Performance:** How efficient the parser is through running time and memory footprint.

- **Code Quality & Reproducibility:** Clean, runnable code with clear instructions.

- **Report (Jupyter Book):** Figures generated at book compilation, interactivity, and clarity of the hosted book.
