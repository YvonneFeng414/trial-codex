# Protocol PDF to SAP with Codex CLI

This project uses `.codex-home/` as its isolated `CODEX_HOME`. The batch runner sets
the environment variable automatically; the directory, including its `auth.json`, is
excluded from Git.

Initialize it with an interactive account login:

```bash
tools/init_codex_home.sh
```

To copy the current file-backed `~/.codex/auth.json`, use
`tools/init_codex_home.sh --copy-current`. Add `--force` when intentionally replacing
an existing project login. If browser login reuses the wrong ChatGPT account, run
`tools/init_codex_home.sh --login --device-auth --force` and authenticate the intended
account with the displayed device code.

## Goal

Keep the useful batch behavior in `run_batch.sh`, but replace its missing
`main.py protocol` dependency with `codex exec`. Each protocol PDF is processed in an
independent Codex session and produces one validated SAP Markdown file.

The first version should be deliberately narrow:

- use Codex only;
- support text-based PDFs;
- detect scanned/image-only PDFs and report them as unsupported;
- preserve parallel execution, retries, resume, logs, and output validation.

OCR and a second agent harness can be added after this path works reliably.

## Design

Use four project-owned files:

```text
run_batch.sh          selects PDFs, runs jobs, retries, and promotes valid output
AGENTS.md             defines the extraction rules and required SAP format
tools/extract_pdf.py  converts every PDF page to tagged text
tools/lint_sap.py     validates the completed Markdown
```

The processing flow for one PDF is:

```text
PDF
  -> extract_pdf.py
  -> page-tagged text file
  -> codex exec
  -> evidence file
  -> draft SAP
  -> reviewer subagent
  -> corrected draft + PASS verdict
  -> lint_sap.py
  -> final SAP
```

This keeps deterministic work outside the agent. The complete page-tagged text stays
on disk; the prompt supplies only its path. Codex reads the file incrementally, so a
300-page protocol is not inserted into the initial prompt. There is no need for a
custom page-reading tool or orchestration layer.

### 1. `tools/extract_pdf.py`

The extractor takes a PDF and output path and writes all pages in order:

```text
===== PAGE 1 OF 131 =====
...
===== PAGE 2 OF 131 =====
...
```

It should:

- use `pypdf` for extraction;
- preserve page boundaries and page numbers;
- write atomically through a temporary file;
- fail if no pages can be read;
- classify the PDF as likely scanned when extracted text is empty or below a small,
  documented threshold;
- print a concise diagnostic and exit non-zero for scanned PDFs.

Because every page is emitted by one deterministic pass, a separate coverage log is
unnecessary. The extractor can verify that the number of page markers equals the PDF
page count before returning success.

Run it with:

```bash
uv run --with pypdf tools/extract_pdf.py INPUT.pdf OUTPUT.txt
```

### 2. `AGENTS.md`

`AGENTS.md` is the single source of truth for Codex. Port only the durable rules from
the old `system.txt`:

1. The role: extract a statistical analysis plan from a clinical-trial protocol.
2. The grounding rules: do not invent content; distinguish absent information from
   information deferred to a separate SAP; cite protocol page numbers.
3. The required five-section Markdown template embedded in `AGENTS.md`.
4. The workflow: build field-by-field evidence, write the draft, delegate an independent
   source review to a subagent, resolve its findings, then pass the linter.

For short protocols, Codex may read the whole extracted file. For long protocols, it
must inspect it in page ranges, first locating relevant sections and then reading those
sections in detail. It must still inspect the beginning, table of contents, statistical
methods, endpoint definitions, safety methods, appendices, and the end of the document.
Context compaction is expected; the draft on disk is the durable working state.

Avoid copying general agent-loop instructions into `AGENTS.md`; Codex already owns the
read-think-act loop. Keep the file focused on domain and output requirements.

The per-job prompt can remain short and explicit:

```text
Create the SAP described in AGENTS.md.
Protocol text: <absolute text path>
Output verbosity: <low, medium, or high>
Reviewer model: <reviewer model; default gpt-5.6-luna>
Evidence output: <absolute evidence path>
Draft output: <absolute draft path>
Reviewer output: <absolute review path>
Use a reviewer subagent and resolve its findings until it records VERDICT: PASS.
Run the required linter and fix all violations before finishing.
```

`run_batch.sh` exposes this as `--verbosity`. It defaults to `high` because SAP
extraction prioritizes recall. Verbosity changes presentation detail, not evidence
coverage or reviewer rigor.

### 3. `tools/lint_sap.py`

The linter takes a draft path and exits non-zero when validation fails. It checks:

- the exact required `##` and `###` headings and their order;
- required fields from the template;
- banned unsupported-language phrases;
- source citations on substantive paragraphs and list items;
- no empty sections or placeholder text;
- a plausible protocol-page citation range based on metadata passed by the batch job.

The linter checks shape and explicit grounding signals; it cannot prove that a cited
claim is correct. That remains an agent/review responsibility.

Use the same linter both inside the Codex session and in `run_batch.sh`. The batch-side
check is authoritative even when `codex exec` returns zero.

### 4. `run_batch.sh`

Preserve the existing:

- ID derivation, manifest creation, and duplicate-ID handling;
- `xargs -P` parallelism;
- skip-if-final-output-exists resume behavior;
- one log per PDF, status TSV, retry count, and final failure summary.

For each PDF:

1. Extract it to `work/<id>.protocol.txt`.
2. If extraction identifies a scanned PDF, record `UNSUPPORTED_SCAN` and do not retry.
3. Run `codex exec` with protocol, evidence, draft, and review paths.
4. The primary agent builds evidence and a draft, then a reviewer subagent independently
   checks the source. The primary resolves findings until the reviewer records
   `VERDICT: PASS`.
5. Require non-empty evidence, a passing reviewer verdict, and an independently passing
   `lint_sap.py` result.
6. On success, atomically rename the draft to `sap_<id>.md`.
7. On failure, retain the work files and logs, then retry in a fresh session.

Retries should use the same clean prompt. The prior linter output is already in the job
log; carrying it into a new prompt adds state and complexity without being necessary
for the first version.

Replace the `main.py` guard with checks for `codex` and `uv`. Use `gpt-5.6-terra` for
the primary agent and `gpt-5.6-luna` for its reviewer subagent by default. Pass
`--model` and `--review-model` overrides through their respective agent requirements. Accept
`--verbosity low|medium|high` and include it in the agent and reviewer requirements.
Update help text so defaults match the variables.

Console progress is a separate concern from SAP verbosity. `--progress
quiet|normal|verbose` changes only what the harness prints, never what the agent
produces:

- `quiet`: stdout carries the final summary only; per-job failures go to stderr, so a
  scripted or `nohup`-style run stays silent unless something breaks.
- `normal` (default): run header, one line per job outcome, final summary.
- `verbose`: adds per-step lines (`[start]`, `[pages]`, `[codex]`, `[check]`) and
  mirrors extraction, the Codex session, and the linter to the terminal while they run.

Mirrored lines are prefixed with the job id so concurrent workers stay readable. Two
invariants hold at every level: the per-PDF log receives the same unprefixed stream, and
mirroring never changes a step's exit status. The log and `_status.tsv` remain
authoritative.

Use this output layout:

```text
test_result/
  sap_<id>.md
  work/<id>.protocol.txt
  work/<id>.evidence.md
  work/<id>.draft.md
  work/<id>.review.md
  logs/<id>.log
  _manifest.tsv
  _status.tsv
```

## Verification

1. Run `./run_batch.sh --dry-run --limit 3` and confirm it performs no agent work.
2. Extract one known text PDF and verify page count, ordered page markers, and readable
   output.
3. Extract a 300-plus-page text PDF and confirm extraction is bounded in memory and the
   agent prompt contains the file path rather than the extracted body.
4. Run the extractor on an image-only PDF and verify the job becomes
   `UNSUPPORTED_SCAN` with a useful diagnostic.
5. Confirm `tools/lint_sap.py` passes a reviewed reference SAP.
6. Confirm broken fixtures fail separately for a missing heading, banned phrase,
   missing citation, and empty section.
7. Run one PDF end to end and compare it with a reviewed reference SAP for the same
   protocol.
8. Run two PDFs with two workers; confirm primary agents, reviewer subagents, logs, and
   work files do not collide.
9. Re-run the same batch; confirm valid final outputs are skipped and failed jobs are
   attempted again.
10. Run the same PDF at each `--progress` level; confirm the job log, final status, and
    process exit code are identical and only console detail differs.

## Deferred work

- Add OCR for scanned PDFs, with its own accuracy and page-coverage tests.
- Compare another harness only after the Codex path has a stable evaluation set.
- Strengthen automated semantic citation checks if reviewer findings show fabricated or
  mismatched page references remain a material problem.

## Acceptance criteria

The first version is complete when a text-based protocol can be processed end to end,
the final file is never promoted unless evidence exists, a reviewer subagent records a
passing verdict, and the linter passes; interrupted jobs can resume safely, parallel
jobs remain isolated, and scanned PDFs fail with an explicit status rather than
producing an empty or invented SAP.
