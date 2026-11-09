---
title: "Project Milestone 1 — Data Crawling"
short_title: "Milestone 1 — Crawling"
label: m1-crawling
description: "Scrape the arXiv open repository: discover paper entries, download full-text LaTeX sources for every version, and crawl reference metadata."
---

# Objectives

In this milestone, students will employ the knowledge about data scraping, also called crawling, obtained from the theoretical lectures to scrape an online repository. This practice is expected to help you convert that knowledge into useful skills about the engineering aspect of your data science journey. However, do not think that this milestone, as well as the others, will cover all the necessary skills you should know before joining realistic projects, so you must continue learning even after the completion of this course!

This year's target is arXiv, the open-access repository of more than two million papers. Scraping arXiv is already a known practice in data engineering courses, though most of them exclusively focus on metadata rather than the full-text resources. Here, you will get ahead of them by collecting the **full-text LaTeX sources** of the papers rather than the hard-to-process PDFs. The corpus built in this milestone is the working foundation for the parsing and machine learning work of Milestones 2 and 3, so make the crawl complete and correctly organized.

# Description

This milestone consists of three main areas: resolving the assigned paper entries, downloading their full-text source files, and extracting structured reference information.

## Entry Discovery

The first step in any arXiv crawling workflow is to identify which papers to collect. In this project the assigned range arrives as plain arXiv IDs, so these methods are a toolbox rather than a task: the API resolves the assigned IDs into paper metadata and reveals each paper's version list, while OAI-PMH and the Kaggle snapshot show how the same discovery is done at repository scale. Implementing a full repository-scale harvest is *not* part of this milestone.

### arXiv API for Targeted Discovery

The arXiv API at <http://export.arxiv.org/api/query> provides an HTTP interface for retrieving information on papers based on search keywords, authors, or categories. It returns results in Atom XML format, which can be parsed programmatically. This method is particularly convenient for targeted queries or daily updates of new submissions.

**Key Parameters and Usage** The arXiv API accepts several useful parameters:

- `search_query`: Field-specific search such as `ti:neural AND cat:cs.AI`

- `id_list`: Comma-separated arXiv IDs for direct lookup

- `start`, `max_results`: For pagination control

- `sortBy`, `sortOrder`: Sorting criteria such as submission date

The library `arxiv.py` allows flexible interaction with this API [@arxiv-api-manual; @arxiv-api-basics; @arxiv-py-github]. Two practical notes matter for this year: the API etiquette asks for modest request rates, so wait at least 3 seconds between successive calls and use one connection at a time [@arxiv-api-tou], and the client used below enforces this delay automatically. The library also went through breaking changes from version 2.x through version 4.x in 2025 and 2026, removing the old patterns `search.results()` and `Result.download_pdf()` / `Result.download_source()` in version 4.0.0 of May 2026, so always use the modern `client.results(search)` style and consult the library's README for the current behavior.

**Example arXiv API querying function:**

```
import arxiv

def find_papers(keyword, limit=5):
    search = arxiv.Search(query=f"all:{keyword}",
                          max_results=limit,
                          sort_by=arxiv.SortCriterion.SubmittedDate)
    return [paper.title for paper in arxiv.Client().results(search)]

# Example usage
titles = find_papers("quantum computing")
print(titles)
```

Results are returned as structured objects containing metadata such as title, authors, abstract, and arXiv ID. Two API facts matter for the assigned range. First, an `id_list` lookup on the base ID returns the identifier of the *latest* version, for example `2310.12345v4` as the identifier of the fourth revision, so reading this suffix tells you how many versions a paper has, which drives the every-version requirement below. Second, an individual version is fetched alone through the versioned `/src/` endpoint described in the download section.

### OAI-PMH for Metadata Harvesting

The Open Archives Initiative Protocol for Metadata Harvesting, abbreviated OAI-PMH, enables systematic retrieval of metadata from arXiv. It is the officially supported bulk-access protocol for academic repositories and is well suited for automated metadata gathering. Since March 2025 the arXiv endpoint lives at <https://oaipmh.arxiv.org/oai>. The old address `export.arxiv.org/oai2` was retired during arXiv's cloud migration and may only redirect, so point new harvesters at the new endpoint [@oai-pmh-arxiv; @oai-pmh-cutover].

**Main Concepts** OAI-PMH uses a set of *verbs*, commands that request data from the repository:

- `Identify`: Get information about the repository

- `ListRecords`: Retrieve multiple records including metadata

- `ListIdentifiers`: Retrieve identifiers (IDs) only

**Selective Harvesting** Harvests can be scoped by date range with the `from` and `until` parameters, by category such as `set=cs`, and by metadata format where the `arXiv` prefix is recommended. One behavioral change of the March 2025 rewrite matters in practice: resumption tokens no longer encode the collection size and *expire daily*, so design a full sweep to finish within a day or to resume gracefully from saved state. The same 3-second pacing applies here as well.

**Basic OAI-PMH retrieval function, endpoint updated March 2025:**

```
from sickle import Sickle

def harvest_arxiv_records(date_from, date_to):
    sickle = Sickle("https://oaipmh.arxiv.org/oai")
    records = sickle.ListRecords(metadataPrefix='arXiv',
                                 from_=date_from, until=date_to)
    return [r.metadata for r in records]
```

This example shows how to obtain a structured list of metadata dictionaries that can later be stored as JSON or a database. Resumption tokens handle pagination automatically, so harvests of thousands of records run without manual page management, and for large time-bounded sweeps the protocol far outpaces the API [@oai-pmh-arxiv].

### Kaggle Dataset for Pre-Aggregated Metadata

The Cornell arXiv dataset available on Kaggle at <https://www.kaggle.com/datasets/Cornell-University/arxiv> provides pre-collected metadata for well over two million papers. It can be used directly without any API calls and is ideal for local data processing or exploratory searches [@kaggle-arxiv].

**Data Handling and Example** The dataset is provided in JSON lines format, where each line represents one paper's metadata. It can easily be loaded into Python for filtering and analysis using pandas:

**Reading and filtering arXiv metadata via Kaggle dataset:**

```
import pandas as pd
import json

def load_arxiv_metadata(file_path, keyword):
    with open(file_path, 'r') as f:
        records = [json.loads(line) for line in f]
    df = pd.DataFrame(records)
    return df[df['title'].str.contains(keyword, case=False)]
```

After loading, students may extract the fields they need such as titles, abstracts, authors, or categories for the selected subset of papers.

### Summary

| **Method**     | **Interface Type**  | **Example Tool or Library** |
|:---------------|:--------------------|:----------------------------|
| arXiv API      | HTTP/XML Endpoint   | arxiv.py                    |
| OAI-PMH        | Metadata Protocol   | Sickle                      |
| Kaggle Dataset | Static JSON Dataset | pandas                      |
*Summary of entry discovery methods*

## Full-Text Source Download

Once you have identified the list of papers to collect, the next step is to obtain their full source files. The goal here is to retrieve the original submission TeX sources, either archived in `.tar.gz` format or in other forms, without modifying their contents. Note that there are many types of sources other than `.tar.gz`, and you will need to identify which types contain the `.tex` or `.bib` files, since those are the only file types the working corpus keeps as the Requirement specifies. Other forms such as PDF files or `.docx` Word documents may not be convertible into TeX files, and for such versions you should keep an empty record in the corpus rather than dropping the publication.

arXiv provides source files for approximately 97% of its submissions, and the rest arrive as PDF-only [@arxiv-html-paper; @arxiv-whytex]. A source archive is a gzipped tar containing the `.tex` files, often several and linked through `\input` or `\include`, the `.bib` bibliography files, style and class files, occasionally a pre-compiled `.bbl`, and the figure binaries. Source archives are served by the dedicated mirror `export.arxiv.org`: the preferred endpoint `https://export.arxiv.org/src/{arxiv_id}` consistently returns a gzipped tar archive, and it also accepts *versioned* identifiers such as `.../src/2310.12345v2`, which is how every revision of a publication is retrieved individually [@arxiv-mimetypes]. Point automated downloads at this mirror and never at the main site `arxiv.org`, where aggressive crawling triggers automatic robot detection and 403 blocks, and use a descriptive User-Agent header with the same 3-second pacing.

### arXiv API Download Using Python

The simplest and most common way to download a paper's full source archive is through the `arxiv.py` Python library, which wraps the API into user-friendly methods.

**Basic Workflow**

1.  Query the API with the paper's arXiv ID to retrieve its metadata.

2.  Download the source archive from the `/src/` endpoint for each revision identified through the API lookup. arxiv.py 4.x no longer ships a download method, so fetch the archive yourself, for example with `urllib` [@arxiv-py-github].

3.  Save the archive into the version's directory.

**Downloading an arXiv source file with arxiv.py version 4.x:**

```
import arxiv
import urllib.request

def get_source(arxiv_id, save_dir="./sources"):
    paper = next(arxiv.Client()
                     .results(arxiv.Search(id_list=[arxiv_id])))
    print(paper.title, paper.id)  # id ends with the latest version suffix, e.g. 2310.12345v4
    url = f"https://export.arxiv.org/src/{arxiv_id}"
    urllib.request.urlretrieve(url, f"{save_dir}/{arxiv_id}.tar.gz")

# Example usage: one call per version
get_source("2310.12345v1")
get_source("2310.12345v2")
```

### Command-Line Download with arxiv-downloader

For users preferring direct commands over scripting, the open-source command-line utility `arxiv-downloader` provides a quick way to get both PDFs and sources. Installed via pip, it downloads source files using only the paper ID or URL [@arxiv-downloader].

**Example Usage**

```
arxiv-downloader 2302.06544 -s -d ./downloads
```

The flag `-s` ensures that source files are fetched rather than PDFs, and `-d` designates the output directory.

### Amazon S3 Bulk Repository Access

arXiv maintains complete source archives within an Amazon Web Services S3 requester-pays bucket at `s3://arxiv/src/`. This interface is intended for large-scale retrieval, allowing structured downloads of `.tar` bundles containing multiple monthly paper packages [@arxiv-s3-bulk].

**Setup and Example Command** Before downloading, configure AWS credentials with `aws configure`. Then use:

```
aws s3 cp s3://arxiv/src/arXiv_src_2401_001.tar \
              ./bulk/ --request-payer requester
```

Each archive contains multiple submission source files grouped by upload date. The monthly chunks follow the naming pattern `arXiv_src_YYMM_NNN.tar`, and the complete archive is on the order of several TB and growing, so consult `arXiv_src_manifest.xml` for the exact chunk list before downloading. You can loop this process to download archives for consecutive months as needed. Alternatively, `s3cmd` provides a lightweight interface: `s3cmd get --recursive --requester-pays s3://arxiv/src/` retrieves all available source archives without manual iteration. A free mirror of the same sources also exists on Google Cloud Storage at `gs://arxiv-dataset`.

### Summary

| **Method**   | **Access Interface**   | **Example Tool** |
|:-------------|:-----------------------|:-----------------|
| arXiv API    | REST via Python client | arxiv.py         |
| Command-Line | CLI utility            | arxiv-downloader |
| Amazon S3    | Bulk cloud storage     | awscli or s3cmd  |
*Available methods for retrieving arXiv source files*

## Extracting References

When working with arXiv papers, extracting structured reference information is essential for understanding citation relationships. This section introduces the Semantic Scholar API as a method to retrieve references and associated identifiers like arXiv IDs without direct LaTeX parsing.

### Semantic Scholar

Semantic Scholar, developed by the Allen Institute for AI, indexes over 214 million papers and provides structured access to citation data through a free API [@semantic-scholar-api; @semantic-scholar-platform]. It excels in linking arXiv papers to broader academic graphs, offering fields such as titles, authors, years, and external identifiers. The API endpoint is `https://api.semanticscholar.org/graph/v1/paper/arXiv:{arxiv_id}`, where queries use the arXiv prefix for identification.

**Request Fields and Response** To obtain references, specify fields such as `references.title`, `references.authors`, `references.year`, `references.publicationDate`, `references.venue`, and `references.externalIds` in the query parameters, and request them flat because nested forms such as `authors.name` are rejected. Every referenced paper also returns its `paperId`, the Semantic Scholar identifier, and its `externalIds` dictionary carries the `ArXiv` key used by this project together with `CorpusId`, `DOI`, or other identifiers when they exist, so checking for `externalIds.ArXiv` decides whether a crawled reference becomes a row of the `references` table in the Requirement section. The response is a JSON object containing an array of references, each holding the requested fields. A simple function to query and extract basic reference data is shown below:

**Querying Semantic Scholar for references:**

```
import requests

def get_paper_references(arxiv_id):
    url = f"https://api.semanticscholar.org/graph/v1/paper/arXiv:{arxiv_id}"
    params = {"fields": "references.title, references.authors, references.year, references.publicationDate, references.externalIds"}
    response = requests.get(url, params=params)
    data = response.json()
    return data.get("references", [])
```

This retrieves the list of references for a given arXiv ID.

### Extracting arXiv Identifiers

From the API response, arXiv IDs are found in the `externalIds` dictionary of each reference. The process involves iterating over the references array and checking for the presence of the `ArXiv` key.

**Handling Problematic Identifiers** Not every reference includes an arXiv ID, as some cite journal articles, books, or non-arXiv sources. Coverage is higher in fields like computer science, where arXiv is prevalent. Besides that, references published on arXiv before 2007 also possess a different scheme of indexing under the old `archive/YYMMNNN` format such as `hep-th/9901001`, so students should retain the original identifiers for such publications.

### Rate Limits and Usage

The API enforces limits of 1 request per second and 100 requests per 5-minute window for unauthenticated use [@semantic-scholar-api]. Exceeding these triggers 429 errors, so implement delays and error handling. API key usage is permitted and will help you improve the runtime performance: the free tier shares one unauthenticated request pool that is subject to throttling during heavy use, while a free API key guarantees a steady 1 request/second, with higher limits available upon review.

**Accuracy Considerations** Semantic Scholar achieves over 90% precision in reference extraction through multi-source parsing, including PDFs and LaTeX when available [@sorc]. However, recent uploads may have indexing delays of 1 to 7 days, and version differences might not be distinguished.

**Limitations** The API may lack data for very new papers, non-English content, or unconventional formats. In such cases, cross-verify with the source files.

# Requirement

In this milestone, students are provided with a range of arXiv paper IDs by the Lab Instructors and scrape the papers in that range programmatically. As the discovery section frames it, the work is to resolve these assigned IDs and to locate every version of each paper, which is the graded part of the milestone rather than a harvest of arXiv at scale. For each assigned paper, **all available versions must be crawled**, including the initial submission and any subsequent revisions. arXiv version IDs follow the convention of appending the suffix `v`\<version number\> to the base ID, for example `2310.12345v1` or `2310.12345v2`, and for papers that were submitted once you should suffice it with `v1`. Each version is retrieved individually through the versioned `/src/` endpoint introduced above.

The scraping process should retrieve full-text source files, metadata, BibTeX references, and crawled reference information for each version, ensuring comprehensive coverage of the assigned papers. As the full size of each paper may be very large due to the size of contained figures, you are advised to **REMOVE any figure** from the scraped data as we are not going to need it (for your future work ;-)). Do the removal by extraction rather than by editing: extract only the `.tex` and `.bib` files from each source archive so that figure binaries and other non-source files never enter the corpus, and keep the LaTeX commands that define figures, such as `figure` environments and `\includegraphics` lines, untouched inside the TeX files, since Milestone 2 parses the document structure rather than the image binaries.

The reference metadata must additionally be crawled for each publication via the Semantic Scholar API. There is **no data submission** in this milestone: the corpus stays with the group as the working input of Milestones 2 and 3, and later becomes part of the final data submission on Hugging Face: the two tables of this milestone are specified below, and the later milestones add theirs in turn.

**Suggested data organization.** Groups organize their working data themselves. The following is a suggested convention to follow as-is or to adapt to whatever suits you best. Keep one working folder per publication, for example folder `2310-12345` for the ID `2310.12345`, holding the `.tex` and `.bib` files of every version and, for each version, its paper metadata and crawled reference metadata: title, authors, submission and revision dates, the venue when applicable, and for each crawled reference its title, authors, submission date, arXiv ID when one exists, and Semantic Scholar ID as returned by the API. Keep *all* crawled references locally, since at the final submission only the references carrying an arXiv ID become rows of the published `references` table, which keys on arXiv identifiers and serves as the matching pool of Milestone 3.

At the final submission in Milestone 3 the working data turns into the tables of the published dataset. This milestone already specifies two of them:

| **Table** | **Columns (types)** |
|:---|:---|
| papers | arxiv_id (string, non-null), title (string), authors (list of strings), submission_date (ISO string), revised_dates (list of strings), venue (string, nullable) |
| references | arxiv_id (string, non-null, the citing paper), ref_arxiv_id (string, non-null), ref_title (string), ref_authors (list of strings), ref_submission_date (string), ref_s2_id (string) |
*Dataset tables specified in this milestone, with one `references` row per crawled reference and its citing paper*

Milestone 2 mirrors this suggestion for the parsed and standardized data it produces, and the submission format of Milestone 3 lists all published tables side by side.

## Submission Format

### Source Code

The group maintains a single public GitHub repository for the entire project: each milestone commits its code to it cumulatively, so the commit history documents the group's progress over time. The repository contains only source files such as Python files with the `.py` or `.ipynb` extensions and excludes data files, compiled executables, and other non-source materials, together with:

```
<group-repo>/
|-- src # folder containing the source code
|   |-- *.{py/ipynb}
|   |-- requirements.txt # To re-produce the environment
|-- README.md # Instructions on environment setup and code execution
```

This milestone is submitted via Moodle as a group by pasting the public links to the shared code repository and to the milestone report described below. All the submissions above should be in the official language of your program. **NO late submission is permitted**.

### Report Requirements (Jupyter Book)

The milestone report is a **Jupyter Book** maintained in a public repository of its own, separate from the shared code repository, and hosted on that repository's GitHub Pages. The book grows with the project: this milestone's report is its first section, and Milestone 2 adds a further section to the same book. The book must present the statistics and analysis of the scraped data, and its figures and tables must be *generated during book compilation*, not pasted as static screenshots, for quality and interactivity. The report should detail the implementation process, including how the scraping works, the tools and libraries used, for example the arXiv API, OAI-PMH, the Semantic Scholar API, and the `requests` library, and relevant statistics such as the number of papers scraped successfully, the overall success rate, the average paper size in bytes before and after removing figures, the average number of references per paper, and the average success rate for scraping reference metadata.

The scraper's performance should also be illustrated through two main points, running time and memory footprint. Running time counts the total time to process one paper, the average time to process each of the required papers, and the total time for entry discovery, while memory footprint counts the maximum RAM used during the scraping, the average RAM consumption, the maximum disk storage required, and the final output's storage size. Measure both points in an auditable way, for example peak RAM through `/usr/bin/time -v` or `psutil`, average RAM through periodic sampling, and disk usage through `du`. These tools are examples, not requirements. If the system consists of several programs, the runtime is counted as the wall time taken to run the system in an end-to-end way, while the memory footprint is considered as the total memory consumed by the full system. The testbed for the final benchmarking is a Google Colab instance running on CPU-only mode. No upper limit on the running time of the scraper is enforced, while the upper limit on maximum disk usage is given by the Colab instance.

The `README.md` of the repository stays separate: it provides clear instructions on setting up the necessary environment, such as the Python version and the packages to install through pip or conda, and running the code with specific configurations.

# Grading Criteria

Your grade is determined upon five main criteria as listed below:

- **Report (Jupyter Book):** Does the report illustrate sufficient information as previously mentioned, with figures and tables generated during book compilation?

- **Source Code:** Is the code clean, readable and runnable?

- **Discovery & Crawling:** Are the discovery and download methods used appropriately, with full version coverage of the assigned papers?

- **Resulted Data:** To what extent has the scraper successfully crawled, and how well does the corpus follow the suggested conventions?

- **Scraper's Performance:** How efficient is the scraper compared to others?
