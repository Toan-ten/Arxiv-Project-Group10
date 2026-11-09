# M1: version dates, S2 batching and two-phase crawling

This note describes code paths verified offline, not a completed full crawl.
No production corpus is modified by importing the modules or running tests.

## Version dates

Discovery currently queries a base ID and stores `published` (v1) and `updated`
(the returned version). Storage verifies the v1 date but does not assign the
latest `updated` to intermediate versions. Pipeline now automatically enriches
history after sources and before S2. Failed/unverified history leaves unknown
revision dates null; complete source/S2 alone cannot supply those missing dates.

[The official Atom API manual](https://info.arxiv.org/help/api/user-manual.html)
documents version-specific `id_list` queries. `published` is the initial
submission date; `updated` belongs to the returned version. `src.arxiv_history`
uses this interface through the existing HTTP transport and Storage APIs.
The original Discovery parser and Storage contract are unchanged. Pipeline
calls it by default; the explicit helper below is available for standalone use.

```python
from src.arxiv_history import enrich_version_history, paper_row
from src.storage import CorpusStorage

# This example sends real requests when explicitly executed outside tests.
# Use only an authorized corpus, with no other writer running.
store = CorpusStorage(corpus_root, run_id=history_run_id)
result = enrich_version_history(store, mssv, arxiv_id,
                                timeout=30, max_attempts=1)
# Stop the outer loop if result['stopped']; StorageError requires an audit.
# Do not continue requests after a fatal I/O/Storage error.
```

For multi-version papers without complete verified history, each confirmed vN
is queried separately and must return exactly that ID. A single-version paper
with a verified Discovery submission date is reused with no extra request.
Only timezone-qualified ISO timestamps with consistent v1 and version order
are accepted. Missing timestamps, latest-version fallback, unknown provenance,
coverage mismatch and conflicting existing dates are rejected. Existing known
dates are never replaced with a different instant.

All responses must validate before one `save_metadata()` call; failed/partial
retrieval leaves saved JSON unchanged and returns the received records in
memory. Partial history is **not persisted as a resumable history checkpoint**:
retrying an unsuccessful history retrieval starts its version queries again.
This is a deliberate small first implementation, not page/byte-level resume.

Successful history is stored under the existing `provenance.version_history`
slot (provider `arxiv_atom_versioned`, base query ID, retrieval time, raw XML and
HTTP evidence per version). It populates each `versions[].date`, submission
and revision dates. Valid history is cached with zero subsequent requests or
JSON writes. Invalid history requires an audit, not automatic replacement.

The step validates checkpoint coverage and existing successful source checksums
before requests. It does not download, modify, delete or create source folders.
S2 evidence and its original retrieval time are preserved. When a valid S2
metadata cache exists, metadata completeness is recalculated using the current
adapter. Publication completeness still requires all source versions and
complete references under the unchanged contract.

The existing arXiv lock/pacing (at least 3 seconds), retries, HTTP metrics and
response cleanup are reused. There is a 2 MiB XML cap per response. With
`max_attempts=1`, an uncached multi-version history requires at most N extra
HTTP calls. Fully verified history or a known single version needs zero. Final 403/429 stops
this operation and must stop a caller's loop. Higher attempts retain existing
client retry semantics; an intermediate 429 may be retried before this function
receives a result. Do not run concurrent arXiv processes to increase throughput.

Pipeline `revision_history=True` runs history automatically, including when
source selection is restricted to v1. Batch and Benchmark default to enabling it
for new batches (`revision_history=None`); resume inherits the saved definition.
Legacy definitions lacking the flag keep it disabled. Changing mode requires
a new batch ID with the same corpus. Controlled Live Smoke explicitly disables
history to preserve its existing request budget and output-marker compatibility.

History result/provenance/error/timing is included in the existing paper summary
and durable callback logs, without new events or a logging subsystem. Batch HTTP
metrics count its actual transport calls/retries. Benchmark sampling and total
wall time enclose it; Entry Discovery time still refers to query_arxiv alone.
Manual standalone calls outside that scope are not retrospectively measured.
Partial history records remain in memory; the log keeps query identity, received
version IDs and errors, not large raw XML. Complete history is saved in metadata.
No production API response was fetched or corpus modified in this task.

[OAI-PMH arXivRaw](https://info.arxiv.org/help/oa/index.html) is another official
source containing full version history. Its record datestamp is not a submission
or revision date. A GetRecord arXivRaw adapter could reduce this to one request
per paper, but is not implemented or claimed verified here. This task only read
documentation; no arXiv/S2 data endpoint was called.

## Final papers schema

`paper_row(store, mssv, arxiv_id)` projects these exact fields in memory:

| Output | Source |
| --- | --- |
| arxiv_id | Original base ID |
| title | Verified stored title |
| authors | Stored list of author names |
| submission_date | Verified v1 ISO timestamp |
| revised_dates | All verified subsequent version timestamps, in version order |
| venue | Stored venue or null |

Unknown required fields/history raise StorageError, not a fabricated empty list.
A confirmed single version can have `revised_dates=[]`. The helper does not write
Parquet, export references, or certify publication/M1 completion. A final exporter
and data-quality report remain separate work.

## arXiv first, S2 later

Existing `batch.run_batch()` already supports this sequence without new flags:

```python
from src.batch import run_batch

arxiv_run = run_batch(
    pairs, corpus_root, batch_id='arxiv-phase-001', max_ids=len(pairs),
    log_root=log_root, versions=None, s2_config=None, max_attempts=1,
    revision_history=True,
)
# After separately authorized execution/review, use the SAME corpus and
# a NEW batch ID to enable S2. Existing good discovery/source is reused.
s2_run = run_batch(
    pairs, corpus_root, batch_id='s2-phase-001', max_ids=len(pairs),
    log_root=log_root, versions=None, s2_config=s2_config, max_attempts=1,
    revision_history=True,
)
```

These are Python APIs, not CLI commands. The examples are not executed by docs.
Keep corpus/log roots separate and outside the repository; one writer only.
The second call skips confirmed source with valid checksums and cached discovery.
It can still retry pending/failed arXiv work: it is **not a strict S2-only mode**.
Checksum failures/orphans remain blocked. Cached discovery is a snapshot; it
does not automatically detect versions published after that discovery.

For a retry of one phase, keep the exact same definition and use `resume=True`.
Changing S2 OFF to ON, selection, versions or retry settings on an old batch ID
is rejected. Keep old logs and create a new batch ID when configuration changes.
HTTP 403/429 stops the batch; never automatically restart the outer loop.
`aggregate_batch()` aggregates one batch ID; it does not automatically combine
arXiv/history/S2 phases. Preserve the phase IDs and use final Storage state for
unique coverage; never sum overlapping corpus snapshots or paper counts. A
complete experiment must account for each phase timing without session gaps.
For multiple ID cohorts, retain explicit selections and batch IDs, not inferred
counts of valid papers. 20,000 assigned positions are candidate IDs.

## S2 metadata batching: design, not yet integrated

The client already supports `fetch_metadata(ids, batch_size=100, ...)`, max 500.
Pipeline `_run_s2()` still calls it with `[identifier]`, hence one POST per paper.
Increasing the client's chunk size alone cannot batch Pipeline requests.

Minimal next step: a bounded S2 enrichment orchestrator over already discovered
papers, in windows of at most 100 uncached IDs. Validate every input/ownership,
checkpoint and saved identity first; retain a stable ordered (MSSV, ID) mapping.
Reuse the existing client, identity validator and Storage adapters. Set durable
metadata intents before the POST, validate the complete response length/identity,
then save results individually. Persist/verify each save before the next HTTP
operation; a save failure or final 403/429 stops processing immediately.
A crashed save can leave some intents interrupted: resume reuses committed good
snapshots and queries only unresolved IDs. Never infer cache success from batch
logs. A 429 must not be bypassed by splitting the chunk or starting another one.

References remain a separate, per-publication paginated stage using the SAME
S2 limiter as metadata. Preserve existing partial-prefix verification and
continuation semantics, and skip verified complete reference snapshots.

This changes execution order and needs compatible logging/stats: do not add
new stage events to the existing two paper callbacks. Add explicit, separate
stage records/observer with versioned logs or a separate runner. Regression
coverage must include shuffled/missing responses, nulls, identity mismatches,
100/101 IDs, mixed cached/uncached ownership, save failure mid-chunk, 429,
interruption/resume and zero redundant source requests. No batching architecture
change was made in this task.

## Before full crawling

1. Review this change and perform a separately authorized one-paper Pipeline
   history smoke in a new corpus; confirm exact version dates and cached rerun.
2. Run arXiv-only in bounded sequential cohorts, all versions, separate corpus,
   fixed batch definitions and logs. Validate/resume each cohort; stop on 403/429.
3. Validate the existing automatic history timing/logging on the target runtime.
   S2 batch orchestration remains separate work; a 429 after 3 attempts is not
   a signal to increase requests or rotate keys.
4. Enrich the same corpus with S2 using new phase batch IDs, then validate full
   version coverage, required dates, references, checksums and final schema.
5. Benchmark the complete configured scope on Colab CPU-only and export real
   data/quality reports. Source-only success does not finish M1.


## Colab CPU-only handoff: proposed acceptance steps

No notebook, Colab session, package installation or live request was run here.
These items must be verified before handing production execution to member A:

1. User commits/pushes the reviewed source, then member A loads that exact
   commit in a CPU-only runtime. Record Python/OS/CPU/psutil versions and code
   commit. The crawler uses stdlib; resource sampling prefers psutil for the
   process tree. Linux fallback measures only the current process; missing
   measurements must remain not_collected, not invented.
2. Put the key in Colab Secrets and grant notebook access. Set the existing
   environment variable, without displaying it or writing a real .env:

   ```python
   import os
   from google.colab import userdata
   os.environ['SEMANTIC_SCHOLAR_API_KEY'] = userdata.get('SEMANTIC_SCHOLAR_API_KEY')
   ```

   [Official Secrets implementation](https://github.com/googlecolab/colabtools/blob/main/google/colab/userdata.py).
   An already configured valid environment variable can be used instead; it
   takes precedence over a root .env. No key goes into config/report/output.
3. Agree persistent, distinct corpus/log/report paths before creating a batch.
   Example (not executed): a Drive-backed project/cohort directory with sibling
   `corpus`, `logs`, `reports` children. Do not keep the only checkpoint under
   ephemeral `/content`. [Colab FAQ](https://research.google.com/colaboratory/faq.html)
   describes runtime deletion and Drive I/O limitations. Use small independent
   cohorts (e.g. 100 selected candidates per corpus root) to avoid one folder
   containing all 20,000 papers. This keeps the existing corpus contract.
4. Verify Storage atomic replace/checksum/resume on the actual Drive filesystem
   using a new temporary fixture first. Local Windows tests do not prove Drive
   durability. Confirm free space, permissions and canonical stable paths, plus
   a one-writer process. Archive/copy verified snapshots after stopping the
   writer; never move the only copy or sync a corpus being actively written.
5. Run an explicitly authorized small arXiv-only benchmark through
   `run_benchmark(..., versions=None, s2_config=None, revision_history=True)`.
   Review date coverage, SHA-256, HTTP metrics, sample counts and RAM/disk scope.
   `run_batch` alone has no resource sampler. The default disk scope is the
   corpus; download scratch lives in the OS temp directory and extraction
   scratch beside the version destination. If reporting scratch/peak workspace
   too, configure a dedicated temp directory before Python's first temp use and
   include it in disk_roots; record the scope. Avoid scanning all of /tmp or
   Drive too frequently. Sampled peaks are not absolute maxima.
6. After an interrupted runtime, stop/confirm the old writer is gone, reload
   the same code/config, mount the same persistent paths, restore the key, and
   call the same batch with resume=True. For benchmark resume, use a new
   experiment_id; reports refuse overwrite. Incomplete history is retried;
   verified source/history/S2 is reused. If paths change (including migration
   from Windows to Colab), use a NEW batch ID with the copied corpus and retain
   old definitions/logs. Do not edit their absolute paths to force a match.
   A damaged checkpoint/orphan requires audit, not deletion. A 403/429 requires
   explicit review, never an automatic restart loop.
7. For later S2 enrichment, keep the same cohort/corpus and create a new phase
   batch ID with s2_config enabled. Pending arXiv work may still be retried;
   this is not strict S2-only mode. Preserve phase IDs and unique coverage when
   combining results. Record CPU-only/Colab in the experiment notes because
   automatic environment measurement_environment currently stays not_collected.
   Acceptance still requires full version dates, metadata/references coverage,
   real Colab measurements and final export validation.
