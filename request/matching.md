---
title: "Project Milestone 3 — Reference Matching Pipeline"
short_title: "Milestone 3 — Matching"
label: m3-matching
description: "Match the extracted BibTeX entries against the crawled reference metadata with a full machine learning pipeline, evaluated with MRR@5."
---

# Objectives

In this milestone, students will apply machine learning techniques to a real matching problem: the citation entries extracted from the parsed text are linked with their external metadata. Concretely, the BibTeX entries produced in Milestone 2 must be matched to the correct arXiv IDs among the reference metadata crawled in Milestone 1 via the Semantic Scholar API [@semantic-scholar-api; @semantic-scholar-platform]. This practice mimics the real-world data science workflow of entity resolution with unstructured text processing, standardization, feature engineering, and matching algorithms assembled into a complete pipeline.

# Description

Students must perform a matching task between the standardized BibTeX entries of Milestone 2, whose unified form is the `bibtex` table described there, and the crawled reference metadata of the same publications, the `references` table described in Milestone 1. The goal is to identify, for each extracted BibTeX entry, the arXiv-keyed reference it corresponds to. The full data science pipeline is described below.

## Data Cleaning

Perform any additional text preprocessing required to facilitate the matching process, with steps such as lowercasing, stop-word removal, or specific tokenization for author names and titles.

## Data Labelling

Students need to create a ground truth dataset by labelling associations between the extracted BibTeX entries and the crawled references of each publication. The task is to identify, for each BibTeX entry, the reference it corresponds to: a single correct association per entry, and none when no crawled reference matches. Pairs exist inside a publication only, so an association pairs one entry with one reference of the same publication. Students are required to:

- Manually label references for at least **5 publications**. Each manually-labeled publication must have at least **20 labeled BibTeX entries**, its quantity $Q$, with at least 100 labeled entries across the pool.

- Implement automatic matching with classic tools such as regex or string similarity heuristics and label at least **10%** of the publications outside the manual pool. The automatic matcher labels every BibTeX entry of the publications it is applied to.

These publication counts are per-member quantities: a group of $k$ members holds at least $5*k$ manually labeled publications with at least $100*k$ labeled entries in total, while the minimum quantity $Q$ of a manually-labeled publication stays as given, since it is a per-publication floor rather than a pool size, and it applies to manually labeled publications only. Automatically matched publications carry no per-publication label count, since the matcher labels every BibTeX entry of the publications it is applied to. Automatic labeling is measured against the whole remaining pool: at least 10 percent of the publications outside the manual pool must be labeled automatically, and the automatic pool must in any case hold at least $2*k$ publications so that the split below can draw its automatically matched publications.

Labeled entries define the ground truth for training and evaluation: for each labeled entry, its row in the candidate matrix of the Data Modeling section is completed, one positive when a true reference exists and an all-negative row otherwise. The internal format for these labels is up to the students but must be clearly documented in the group's repository and presented in the poster.

## Feature Engineering

Students must first frame the matching task as a specific supervised learning problem such as classification, regression, recommendation, or ranking, which determines the input format such as entry-reference pairs, triplets, or candidate lists. Based on this chosen input format, students must perform feature engineering to construct reasonable features for the model. These features can range from simple string matching scores or embeddings to complex statistical features.

Students must justify the creation of each feature in the poster and the repository documentation, explaining the underlying idea. This insight and idea should be illustrated with appropriate data analysis.

**Note:** In this feature engineering phase, students are allowed to construct features based on hierarchies previously formed in Milestone 2.

## Data Modeling

The target of this problem is to match the BibTeX entries standardized in Milestone 2 with the associated references in the crawled metadata of the same publication.

If a publication has $m$ BibTeX entries and $n$ crawled references with arXiv IDs, its candidate space is an $m \times n$ binary matrix: row $i$ holds BibTeX entry $i$, column $j$ holds reference $j$, and cell $(i, j)$ carries a binary label that reads **yes** when entry $i$ corresponds to reference $j$ and **no** otherwise. Labeling is scoped to publications: the pools selected under Data Labelling contribute the labeled publications, and each labeled publication's matrix is completed *row-wise*, so every labeled entry carries exactly one yes cell holding its true reference when one exists while the remaining cells of its row are no. An entry with no true reference among the crawled references makes an entirely no-valued row. Unlabeled entries simply carry no rows.

The data split strategy is as follows:

- **Test Set:** Includes 1 publication from the manually labeled set and 1 publication from the automatically matched set.

- **Validation Set:** Includes 1 publication from the manually labeled set and 1 publication from the automatically matched set.

- **Training Set:** Consists of all remaining publications (the remaining manually labeled ones and the remaining automatically matched ones).

The one-publication counts of the Test and Validation sets are per-member quantities as well, so a group of $k$ members puts $k$ manually labeled and $k$ automatically matched publications into each of the two sets.

## Model Evaluation

Evaluate the trained model on the **Test set**, comprising the manually labeled and automatically matched publications defined above. For every labeled BibTeX entry, the model outputs a ranked list of the **top 5 candidate references**. Post-processing of the results is allowed.

The evaluation metric is **Mean Reciprocal Rank &#64;5, written MRR@5**.

$$\text{MRR@5} = \frac{1}{|Q|} \sum_{i=1}^{|Q|} \frac{1}{\text{rank}_i}$$

where $|Q|$ is the number of labeled BibTeX entries in the Test set, the same quantity $Q$ as in Data Labelling, and $\text{rank}_i$ is the position of the correct reference among the top-5 candidates of the $i$-th entry. If the correct reference does not appear among the five candidates, the score of that entry is **zero**.

# Requirement

Perform the full pipeline, covering cleaning, labelling, feature engineering, modeling, and evaluation, on the assigned publications. The matching results are published in the `predictions` table of the final Hugging Face dataset, specified in the Submission Format section. The table contains one row per BibTeX entry of each publication:

| **Column** | **Type** | **Content** |
|:---|:---|:---|
| arxiv_id | string, non-null | Publication the entry belongs to |
| partition | string, dictionary | `train`, `valid`, or `test` |
| citation_key | string, non-null | BibTeX entry name, its identifier |
| groundtruth_arxiv_id | string, nullable | The correct arXiv ID of the entry's true reference, when known |
| cand_1 to cand_5 | string, nullable | The model's ranked top-5 candidates: cand_1 best, cand_5 last |
*Columns of the predictions table*

**Important Note:** Only the publications that are used for the reference matching task, namely those in the Training, Validation, and Test sets, have rows in this table.

# Submission Format

## Final Data Submission (Hugging Face)

There is **no intermediate data submission** in this project. The final data submission happens in this milestone: the fully processed data is hosted as a public **Hugging Face dataset**, following the hosting instructions given by the Lab Instructors, and the dataset link is pasted via Moodle.

**Storage format: Parquet.** Publish the processed corpus as *sharded Parquet*, the Hub's standard dataset format which the viewer and the streaming backend read directly, in six tables with one unified, explicit schema. Keep the columns flat and primitive: one-level string lists for authors and revised dates are fine, while nested objects and serialized JSON blobs are not.

Milestones 1 and 2 already specified five of these tables beside the data they produce, and the predictions table below completes the set:

| **Table** | **Content** | **Described in** |
|:---|:---|:---|
| papers | the crawled paper metadata | Milestone 1 |
| references | the crawled reference metadata with one row per crawled reference and its citing paper, keeping the arXiv-keyed references only | Milestone 1 |
| elements | the parsed elements of all versions, deduplicated into one store | Milestone 2 |
| hierarchy | the per-version node-parent edges between elements | Milestone 2 |
| bibtex | the unified BibTeX entries after deduplication | Milestone 2 |
| predictions | the matching results with columns given below | this milestone |
*Tables of the published dataset and where their column specs live*

The dataset repository itself is simple:

    └── <dataset-repo>/
        ├── README.md
        └── data/
            ├── papers/
            ├── references/
            ├── elements/
            ├── hierarchy/
            ├── bibtex/
            └── predictions/

At the root, a **dataset card**, a `README.md` with YAML metadata, tells the Hub how the tables map to files:

**Dataset card metadata (README.md):**

```
---
configs:
- config_name: papers
  data_files:
  - split: train
    path: data/papers/*.parquet
- config_name: references
  data_files:
  - split: train
    path: data/references/*.parquet
- ...one config per table...
---
```

The working organization between milestones is up to each group, and Milestones 1 and 2 suggest what information to keep, since these tables are only the publishing format for the final submission. Avoid custom loading scripts: the filename patterns and the card above are all the Hub needs. **NO late submission is permitted**.

## Source Code

This milestone's code is committed to the same single public GitHub repository used in Milestones 1 and 2, continuing the cumulative history that documents the group's progress. The repository contains only source files such as Python files with the `.py` or `.ipynb` extensions and excludes data files, compiled executables, and other non-source materials, together with:

```
<group-repo>/
|-- src # folder containing the source code
|   |-- *.{py/ipynb}
|   |-- requirements.txt # To re-produce the environment
|-- README.md # Instructions on environment setup, code execution, feature and label documentation
```

This milestone is submitted via Moodle as a group: the poster file (see below) and the public links to the Hugging Face dataset and to the shared code repository. All the submissions above should be in the official language of your program.

## Poster and Poster Session

The final grading format is a **poster session**. Each group submits **one pptx file containing a single-page poster** in the form factor of **portrait A2** for review. The poster should present the group's selected pieces of work: those representing the *highest quality*, the *most outstanding* results, and the work the group believes makes it *distinct from the others*. Examples include the engineered features with their justification and analysis, the model and its MRR results, and notable statistics of the parsing and matching pipeline.

Upon acceptance of the review, the group **prints the poster** and **presents it at the final poster session**.

# Grading Criteria

Your grade is determined upon five main criteria as listed below:

- **ML Pipeline Implementation:** Correct implementation of cleaning, labeling, feature engineering, and modeling steps for reference matching.

- **Evaluation & Analysis:** Proper use of MRR, clear justification of features, and insightful analysis of the results, as presented in the poster and the repository documentation. Note that students using external-knowledge methods such as LLMs or pretrained models shall be judged in separation from the other students.

- **Dataset Schema:** The published Parquet dataset conforms to the table and column specification, ready for streaming.

- **Code Quality & Reproducibility:** Clean, runnable code with clear instructions.

- **Poster & Session:** Quality of the selected work, the single-page portrait A2 poster, and the poster session presentation.
