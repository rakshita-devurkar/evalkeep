# Evalsmith

**Turn real AI-agent failures into reviewed regression tests, and measure whether
later versions fix or reintroduce them.**

AI applications produce thousands of production traces. Existing eval tools can
*execute* tests, but teams still have to decide which failures deserve permanent
coverage. Evalsmith owns that decision:

```
trace → failure evidence → failure family → representative case
      → reviewed regression test → runner execution → trustworthy comparison
```

Evalsmith does not execute your agent. It generates tests, delegates execution to
[Promptfoo](https://promptfoo.dev), and compares baseline against candidate runs.

## Status

Version 0.1 is feature-complete: the whole pipeline runs, from a raw trace file
to a statistically-honest regression report.

| Command | Status |
| --- | --- |
| `evalsmith version` | ✅ |
| `evalsmith init` | ✅ |
| `evalsmith ingest` | ✅ |
| `evalsmith trace list` / `trace show` | ✅ |
| `evalsmith detect` | ✅ |
| `evalsmith failures list` / `show` / `confirm` / `dismiss` / `add` | ✅ |
| `evalsmith analyze` / `failures label` | ✅ |
| `evalsmith discover` | ✅ |
| `evalsmith clusters list` / `show` / `rename` / `merge` / `split` / `dismiss` | ✅ |
| `evalsmith dataset build` / `list` / `show` | ✅ |
| `evalsmith review` | ✅ |
| `evalsmith dataset approve` / `reject` / `edit` | ✅ |
| `evalsmith targets add` / `list` / `show` / `remove` | ✅ |
| `evalsmith export` | ✅ |
| `evalsmith run --target ...` | ✅ |
| `evalsmith compare` | ✅ |
| `evalsmith runs list` / `show` | ✅ |
| `evalsmith baseline promote` / `show` | ✅ |
| `evalsmith dataset build` / `list` / `show` | ✅ |
| `evalsmith review` | ✅ |
| `evalsmith dataset approve` / `reject` / `edit` | ✅ |
| `evalsmith targets add` / `list` / `show` / `remove` | ✅ |
| `evalsmith export` | ✅ |
| `evalsmith run --target ...` | ✅ |
| `evalsmith compare` | ✅ |
| `evalsmith runs list` / `show` | ✅ |
| `evalsmith baseline promote` / `show` | ✅ |
| `evalsmith review` | ✅ |
| `evalsmith dataset approve` / `reject` / `edit` | ✅ |
| `evalsmith targets add` / `list` / `show` / `remove` | ✅ |
| `evalsmith export` | ✅ |
| `evalsmith run --target ...` | ✅ |
| `evalsmith compare` | ✅ |
| `evalsmith runs list` / `show` | ✅ |
| `evalsmith baseline promote` / `show` | ✅ |
| `evalsmith targets add` / `list` / `show` / `remove` | ✅ |
| `evalsmith export` | ✅ |
| `evalsmith run --target ...` | ✅ |
| `evalsmith compare` | ✅ |
| `evalsmith runs list` / `show` | ✅ |
| `evalsmith baseline promote` / `show` | ✅ |
| `evalsmith run` / `compare` | planned |

## Quick start

```bash
git clone https://github.com/<you>/evalsmith && cd evalsmith
uv sync
uv run evalsmith init
```

`init` is idempotent — re-running it fills in whatever is missing and leaves
everything else alone. It creates:

```
evalsmith.yaml        project configuration (commit this)
.evalsmith/database.db  redacted traces and events
.evalsmith/data/      intermediate artifacts
.evalsmith/cache/     analyzer and embedding caches
.evalsmith/runs/      raw Promptfoo run outputs
.evalsmith/exports/   generated export files
```

and appends the state paths to `.gitignore`. **Approved tests and configuration
belong in Git; raw traces, the database, caches and run outputs do not.**

Then ingest some traces:

```bash
uv run evalsmith ingest examples/refund-agent/traces.jsonl --validate-only  # check the file
uv run evalsmith ingest examples/refund-agent/traces.jsonl --dry-run        # check against storage
uv run evalsmith ingest examples/refund-agent/traces.jsonl                  # redact and store
uv run evalsmith trace list
uv run evalsmith trace show trace-1042
```

Then find the failures in them:

```bash
uv run evalsmith detect
uv run evalsmith failures list
uv run evalsmith failures show trace-1042
uv run evalsmith failures confirm trace-1042 --reason "refunded the oldest order"
```

Then describe them, so similar failures can be grouped:

```bash
# With no API key, label by hand — this is a first-class path, not a fallback:
uv run evalsmith failures label trace-1042 \
  --type wrong_tool_argument --component tool_arguments --severity high \
  --summary "Refunded the oldest order instead of the newest."

# With analyzer.provider set in evalsmith.yaml:
uv run evalsmith analyze
```

Then group them into families and pick who represents each:

```bash
uv run evalsmith discover
uv run evalsmith clusters list
uv run evalsmith clusters show <cluster-id>
uv run evalsmith clusters rename <cluster-id> "Refunds the wrong order"
```

Then draft a regression test for each family's representatives:

```bash
uv run evalsmith dataset build
uv run evalsmith dataset list
uv run evalsmith dataset show trace-1042
```

Then review them. Nothing is exported until a person approves it:

```bash
uv run evalsmith review
```

Then run the approved suite against your agents:

```bash
uv run evalsmith targets add baseline  --type python --path agents/baseline.py  --function call_api
uv run evalsmith targets add candidate --type python --path agents/candidate.py --function call_api
uv run evalsmith run --target baseline
uv run evalsmith run --target candidate
```

Then find out whether the change helped:

```bash
uv run evalsmith compare
uv run evalsmith baseline promote <run-id> --reason "shipped"
```

## Trace format

Evalsmith reads newline-delimited JSON, one trace object per line. The smallest
valid trace is three fields:

```json
{"trace_id": "trace-1042", "input": {"text": "Refund my latest order."}, "outcome": {"status": "failure"}}
```

A full trace adds the ordered interaction and the evidence that something went
wrong:

| Field | Meaning |
| --- | --- |
| `trace_id` | Unique, non-empty. Duplicates within a file are rejected. |
| `input` | `text` and/or `messages` — at least one must be non-empty. |
| `output` | Optional `text` and/or `messages`. |
| `events` | Ordered `message`, `tool_call`, `tool_result` and `evaluation` events. |
| `outcome` | `status` (`success`/`failure`/`error`/`unknown`), plus optional `feedback` and `evaluations`. |
| `metadata` | `recorded_at`, `source`, `agent`, `model`, `tags`, and an open `extra` object. |

The schema is strict on purpose. Unknown fields are rejected rather than
ignored, so a typo like `trace-id` is reported instead of silently dropping
data — and so the redactor knows every field it must scrub. Put arbitrary
provider data under `metadata.extra`, the one open field, which redaction walks
recursively.

Events are validated as a sequence, not just individually: event IDs must be
unique, timestamps must not go backwards, tool names must look like callable
identifiers, and a `tool_result` cannot reference a `call_id` that no earlier
`tool_call` opened.

### Reporting

Validation streams, so file size is not a limit — 100k traces validate in about
2.4 seconds with a ~10 MB memory ceiling. Invalid records never stop the run;
each one is reported and the rest keep going:

```console
$ evalsmith ingest traces.jsonl --validate-only --errors errors.jsonl
line  kind          field          problem
   2  json                         invalid JSON: Expecting value at column 33
   3  duplicate_id                 trace_id 'trace-1' already appeared earlier in this file
   4  schema        session_id     Extra inputs are not permitted (Unknown fields belong under 'metadata.extra'.)
   5  schema        events.0.tool  String should match pattern '^[A-Za-z_][A-Za-z0-9_.-]*$'
```

`--errors PATH` writes one JSON object per issue, with `line`, `kind`,
`message`, and where known `trace_id`, `field` and `hint`. Field paths index
into your own document, so `events.0.tool` is a path you can actually follow.

## Redaction

Redaction runs **in memory, between validation and storage**. There is no code
path that writes a raw trace, so the database, exports and anything later sent
to an analyzer only ever hold redacted values.

| Rule | Catches | Replaced with |
| --- | --- | --- |
| `emails` | Email addresses | `[REDACTED:email]` |
| `phone_numbers` | Separator-formatted and E.164 numbers | `[REDACTED:phone]` |
| `payment_cards` | 13–19 digit runs that pass a Luhn check | `[REDACTED:payment_card]` |
| `token_prefixes` | `sk-`, `ghp_`, `xox*-`, `AKIA`, `AIza`, JWTs, `Bearer …` | `[REDACTED:token]` |
| `secret_field_names` | Values under keys like `api_key`, `password`, `authorization` | `[REDACTED:secret_field]` |

Toggle any rule in `evalsmith.yaml`. The rules err toward over-redaction — a
redacted order number is an inconvenience, a stored API key is an incident —
with two deliberate exceptions that keep the data usable:

- **Identifiers are never rewritten.** `trace_id`, `event_id`, `call_id` and
  `tool` survive verbatim; scrubbing them would break the links the whole
  pipeline runs on.
- **Secret *field names* only redact strings and containers.** `token_count: 512`
  is a number, not a credential.

Luhn does real work here: `4111 1111 1111 1111` is replaced, while the 13-digit
order number `1234567890123` and the total `$24.00` are left alone. Phone numbers
are matched before cards, because a phone sitting next to a card forms a single
digit run in which Luhn alone cannot say which digits belong to which.

Redaction is deterministic and uses fixed placeholders rather than hashes of the
original, so two customers' email addresses collapse to the same value — which
is exactly what clustering wants.

## Failure detection

`evalsmith detect` runs every detector over every stored trace and records what
they found. Detectors report **evidence**, never inference — a signal points at
something a person or an evaluator actually recorded:

| Detector | Evidence |
| --- | --- |
| `explicit_status` | `outcome.status` is `failure` or `error` |
| `negative_feedback` | `outcome.feedback.rating` is `negative` |
| `failed_evaluator` | an evaluation recorded `passed: false`, in the outcome or in an event |

Every signal keeps a path back into the trace (`outcome.evaluations.0`), so a
reviewer can check the claim rather than take it on trust.

**Signals are counted, not scored.** A trace with three signals is better
documented than one with a single signal — it is not "more likely" to be a
failure, and Evalsmith will not print a number implying otherwise. In the same
spirit, `negative_feedback` fires only on an explicit `negative` rating: a bare
numeric score arrives without its scale, and thresholding it would be a guess
dressed up as evidence.

### Detection is idempotent

Re-running `detect` is always safe, because of four rules:

- A failure's ID is a pure function of its trace ID, so a second pass addresses
  the same record instead of creating another. One failure per trace is enforced
  by a `UNIQUE` constraint, not just by convention.
- Signals are **derived** — every pass recomputes and replaces them, so adding or
  changing a detector refreshes the evidence on existing failures.
- A review is **not** derived. Once you confirm or dismiss a failure, detection
  updates its signals and leaves your decision, name and reason alone.
- An unreviewed candidate whose evidence has since disappeared is withdrawn.
  Nothing human is lost, because nothing human was there. Reviewed and manually
  added failures are never withdrawn.

### Review

```bash
evalsmith failures confirm <id> --reason "refunded the oldest order"
evalsmith failures dismiss <id> --reason "synthetic test data"
evalsmith failures add trace-1060 --reason "detectors cannot see this one"
```

`confirm`, `dismiss` and `add` all record who decided and when. Dismissed
failures are kept for audit rather than deleted, and `add` covers the case
detection cannot reach: a trace that is wrong with no recorded evidence saying
so. Every command accepts either a failure ID or the trace ID it came from.

## Failure analysis

Detection says *that* a trace failed. Analysis says *what kind* of failure it is,
which is what makes grouping possible in the next stage. Each analysis records a
`failure_type`, a `component`, a `severity` and a one-sentence summary.

The vocabularies are closed on purpose. Free text does not cluster: "wrong order
id", "refunded the wrong order" and "bad tool arg" are one failure family that
three analysts would name three different ways.

### Analysis works with no API key

`analyzer.provider` defaults to `manual`, which is **the absence of a provider,
not a provider that guesses**. Evalsmith produces a fully labelled dataset
offline; it just asks a person for the labels:

```bash
evalsmith failures label <id> --type wrong_tool_argument \
  --component tool_arguments --severity high --summary "..."
```

| Provider | What it does |
| --- | --- |
| `manual` (default) | No automatic analysis; label by hand |
| `anthropic` | Claude via the Messages API, constrained to the schema. Needs `ANTHROPIC_API_KEY` and `pip install 'evalsmith[anthropic]'` |
| `stub` | Deterministic placeholder for offline development; its output is stamped `stub` so nothing mistakes it for judgement |

### Caching, and why the key has three parts

Answers are cached under `.evalsmith/cache/`, keyed by the **trace content**,
**which analyst** produced it, and **which prompt version** was asked. Change any
one and the key changes, so a prompt edit or a model swap never serves a stale
label — and re-running after a database reset costs nothing. Editing the prompt
text without bumping `FAILURE_ANALYSIS_PROMPT_VERSION` is a bug, not a
convenience: it would serve cached answers to a question you no longer ask.

The cache is a speed and money optimisation, never a source of truth. Deleting
it loses nothing the database does not already hold.

### What analysis will not do

- **It will not overwrite a hand-written label.** `--reanalyze` refreshes
  *machine* analyses; replacing something a person wrote needs the separate
  `--overwrite-manual`. Refreshing model output after a prompt change is
  routine, and must not quietly discard human work along the way.
- **It will not abandon a run over one bad answer.** A provider error is counted
  and reported; the remaining failures are still analyzed.
- **It will not store what it cannot validate.** A response outside the closed
  vocabulary is an error, not a new category.
- **It will not store unredacted text.** The model only ever sees a redacted
  trace, but its response is redacted *again* before storage — a model can quote
  its input, and "it only saw redacted text" is an argument, not a guarantee.
  The same applies to summaries you type by hand.

Every analysis keeps the provider's raw response for audit, so a surprising
label can be checked against what the model actually said.

## Clustering and representatives

`evalsmith discover` embeds each analyzed failure, groups them into families,
and selects representatives — the point of the whole exercise, since a
regression suite wants one good test per failure family, not forty copies of the
same bug.

### The algorithm, and why

Average-linkage agglomerative clustering over **cosine distance**, cut at a
configured threshold (default `0.55`). Chosen for three properties that matter
more here than raw clustering quality:

- **It is deterministic.** No initialisation, no restarts: the same vectors and
  threshold always give the same grouping. A seed is recorded with every run
  anyway, so swapping in a randomized algorithm later cannot quietly break
  reproducibility.
- **It does not need the cluster count up front.** Nobody knows how many failure
  families a trace file holds.
- **The threshold means something.** It is a cosine distance — explainable,
  tunable, and written into every stored run alongside the embedder, dimensions,
  linkage and seed.

Average linkage rather than single linkage on purpose: single linkage chains, so
one ambiguous failure sitting between two families would merge both.

### Embeddings work offline

The default embedder is `hashing` — feature hashing over unigrams and bigrams,
L2-normalized, deterministic (BLAKE2b keyed by the seed, never Python's
randomized `hash()`). It needs no key and no network.

Be clear about what it does: it captures **lexical** overlap, not meaning.
"refunded the wrong order" and "issued a credit for the incorrect purchase" will
not group, where a trained embedding model would place them together. That is
acceptable here because the embedded text is not free prose — it is a structured
analysis whose failure type and component lead the string, written under a
prompt that explicitly asks for wording that repeats across a family.

Measured on 200 synthetic failures drawn from five known families, with three
phrasings each: **7 clusters, every one pure** — no family was ever merged into
another. One family split three ways because its phrasings shared few words.
That is the error worth having: an over-split family costs an extra
representative test, while a false merge would leave a real failure family with
no coverage at all. `clusters merge` exists for exactly this.

A hosted embedding model plugs in at `evalsmith/embeddings/__init__.py`:
implement the four-member `EmbeddingProvider` protocol and register it. The
cache and the stored run parameters key off the provider's `identity`, so
vectors from two different models can never be silently mixed.

### Representatives

Each family gets up to three, answering three different questions:

| Role | Question it answers |
| --- | --- |
| `central` | What does this family typically look like? |
| `boundary` | How far does it stretch? |
| `high_severity` | How bad does it get? |

One failure can hold several roles — in a family of one it holds all three.

### Editing a clustering

A distance metric over short summaries will always split a family a person can
see is one, and join two that a person can see are not. Editing is therefore a
first-class operation:

```bash
evalsmith clusters merge <id> <id>              # these are really one family
evalsmith clusters split <id> --failure <id>    # this one does not belong
evalsmith clusters rename <id> "Refunds the wrong order"
evalsmith clusters dismiss <id>                 # not worth regression coverage
```

**A cluster's identity is derived from its members**, which is what lets edits
survive: re-running `discover` on unchanged data produces the same cluster IDs,
so your names and dismissals stay attached to the families they were written
for. Change the membership and the ID changes — honestly, because it is no
longer the same group. When re-clustering *would* discard an edit it cannot
carry over, `discover` refuses and tells you which; `--force` proceeds.

### Scale

Clustering builds a full pairwise distance matrix, so cost grows quadratically:

| Failures | Time | Peak memory |
| --- | --- | --- |
| 500 | 0.08s | 5 MB |
| 2,000 | 0.69s | 26 MB |
| 5,000 | 4.2s | 133 MB |

Comfortable to a few thousand failures; beyond ~20,000 the matrix dominates.
HDBSCAN, on the 0.2 roadmap, is the fix.

## Regression tests

`evalsmith dataset build` turns each cluster representative into a **pending
draft**. Only representatives, by default — that is the point of clustering: a
suite wants one good test per failure family, not forty copies of one bug.
`--all` covers every failure instead.

### What a trace can and cannot tell you

This shapes the whole generator. A trace shows exactly **what the agent did
wrong**, so the forbidding half of a test is derivable and deterministic. It
does **not** show what the agent should have done instead — that is a judgement
about intent that no amount of reading the trace supplies.

So generation writes the half it can defend and records, as a warning, that a
reviewer still owes the other half. Against the guide's own example:

```
input:  "Refund my latest order."
generated:  refund_order.order_id != 'order-A'      ← read off the trace
needs review: no positive expectation               ← "== order-C" is a judgement
```

Nothing in that trace *says* order-C is correct; knowing it requires reading
`placed_at` and interpreting "latest". That is exactly the point where automated
analysis could encode the wrong expectation, so it is left to a person.

| Failure type | Derived deterministically |
| --- | --- |
| `wrong_tool_argument` | `tool_argument_not_equals` for each argument of the implicated call |
| `wrong_tool_selection` | `tool_not_called` for the tool that was used |
| `unnecessary_action` | `max_tool_calls` at one below the observed count, or `tool_not_called` |
| everything else | nothing checkable — falls back to a `human_rubric` |

Two deliberate limits. Assertions target the **last** tool call, because earlier
calls in a trace are usually lookups that succeeded and forbidding their
arguments would forbid correct behaviour — the draft says so in a warning. And
the call limit for over-action is set to one below what was observed, because
the trace proves N was too many but not what the right number is.

The `human_rubric` is a last resort, not a default: it costs an LLM judge at run
time, so it only appears where nothing checkable could be read off the trace.

### Stable test IDs

A test ID is derived from the **trace's own input** plus a hash of the trace ID:

```
refund_my_latest_order_2e46a1ea
```

Readable, unique, and built only from immutable facts. Deliberately *not* from
the cluster label or the analysis — both are mutable. A reviewer renaming a
cluster, or a re-analysis changing a failure type, must never rename a test that
is already committed to Git and referenced by past run results.

### Contradictions are caught at generation

A contradictory test fails on every agent, including a correct one, so it
reports a regression that is really a bug in the suite. Every pair of
expectations is checked — required-and-forbidden tools, an argument required to
equal two values, an argument of a forbidden tool, contains-and-not-contains,
two different call limits — at generation, and again when a reviewer edits.

### Drafts only

`dataset build` writes nothing but drafts, and each carries full provenance: the
trace, the failure, its evidence, the cluster and the representative role it was
selected for, the analysis and analyzer, the source trace's content hash, and
the generator version. Re-running is a no-op; `--regenerate` rewrites drafts and
**never** touches a reviewed test.

## Review

`evalsmith review` walks the pending drafts, showing all three things a decision
needs on one screen — **the source interaction, the failure analysis, and the
proposed test** — and asks:

```
approve / edit / reject / skip [skip]:
```

An answer it does not recognise means *skip*: a review tool must never guess a
decision. Each choice records who decided, when, and why.

Everything is also reachable one decision at a time, so a script or CI job can
record the same decisions without a terminal — which is what keeps the guide's
non-interactive review format possible later:

```bash
evalsmith dataset approve <id> --reviewer alex --reason "..."
evalsmith dataset reject  <id> --reviewer alex --reason "synthetic data"
evalsmith dataset edit    <id> --file edited.yaml
```

### Editing

`edit` opens the test's input and expectations as YAML in `$EDITOR`. This is
where the generated draft gets its missing half — completing the guide's own
example:

```yaml
expectations:
- type: tool_argument_equals        # added by the reviewer
  tool: refund_order
  path: order_id
  value: order-C
- type: tool_argument_not_equals    # read off the trace
  tool: refund_order
  path: order_id
  value: order-A
```

Two rules the editor enforces:

- **Only the input and the expectations are editable.** The test ID, provenance
  and recorded fixtures are facts about where the test came from; letting a
  review rewrite them would make the audit trail describe something that never
  happened.
- **Nothing is stored unless it is valid.** An unparseable, invalid or
  self-contradictory edit is rejected with the reason, and the stored draft is
  left exactly as it was. YAML is parsed with `safe_load`, so a document from an
  editor cannot construct objects.

Warnings are recomputed after an edit rather than carried forward — they
describe current content, and a note about how a draft was generated stops being
true the moment a person changes it. Adding the positive expectation above
clears the "needs an expectation" warning.

### What review guarantees

- **A contradictory test cannot be approved.** It would fail on a correct agent
  too, reporting a regression that is really a bug in the suite. It can still be
  *rejected* — that is how a broken draft leaves the queue.
- **A test with no expectations cannot be approved.** It checks nothing.
- **Rejected tests are kept, not deleted.** A rejection is evidence about what a
  team decided not to cover.
- **Only approved tests are exported.** Drafts and rejections never leave the
  database.

A missing positive expectation is a warning, not a veto: a pure prohibition is
sometimes the right test, and that call belongs to the reviewer.

## Targets and execution

Evalsmith does not execute agents. It hands an established runner a suite and
reads the results back — that division is the whole point of the project, and
`run` is a translation layer, not an eval engine.

### Targets are committed, and cannot contain secrets

A target says where an agent lives and how to read its answer. It lives in
`targets.yaml`, which **is** committed — so a literal credential in one would be
a leak. That is enforced, not requested: saving a target whose configuration
contains something that looks like a credential is refused, and the same
detectors that redact traces do the looking.

```bash
evalsmith targets add candidate --type http \
  --url https://agent.example.com/chat \
  --body '{"message": "{{input}}"}' \
  --header 'Authorization=${AGENT_TOKEN}' \
  --output-path json.reply --tool-calls-path json.tool_calls
```

Secrets are `${ENV_VAR}` references, resolved at run time; a run stops before it
starts if a referenced variable is unset. Four kinds are supported — `http`,
`python`, `javascript` and `model` (a provider called directly, for testing tool
selection rather than a whole application) — and all four are normalized to one
response shape:

```json
{"text": "...", "toolCalls": [{"tool": "...", "arguments": {...}}]}
```

so an expectation means the same thing wherever it runs.

### Two rules the runner is built around

**The runner is invoked as an argument list, never through a shell.** Test
inputs, tool names and paths all come from recorded traces. Building a command
string out of them would make a trace containing `; rm -rf` a
remote-code-execution bug, so `subprocess.run` gets a list with `shell=False`.

**Every literal in a generated assertion is embedded as JSON.** Expectations
carry data from traces — order IDs, output fragments. Pasting those into a
JavaScript expression by concatenation is an injection bug with the same shape
as SQL injection. There is a test that puts `order-"); process.exit(1); //` into
an expectation and checks the assertion still evaluates correctly.

### A test that never ran is not a test that failed

Promptfoo distinguishes an assertion failure from a provider error, and so does
the import:

| Result | Meaning |
| --- | --- |
| `pass` | The agent satisfied every expectation |
| `fail` | The agent ran and got it wrong — information about the agent |
| `error` | The agent never ran: a timeout or a crashed provider |

Errors are reported separately and never counted as failures. Letting an outage
look like a regression is exactly the wrong answer for a tool whose job is
deciding whether a release got worse.

### The example runs with no key and no network

`examples/refund-agent/agents/` holds two deterministic agents: `baseline.py`
reproduces the bug the example traces recorded (refunds the *oldest* order) and
`candidate.py` fixes it. Against a suite of three approved tests:

```
baseline    passed 0   failed 3   errors 0
candidate   passed 3   failed 0   errors 0
```

Both runs record the same suite hash, so they are comparable. That end-to-end
check runs against the real Promptfoo and is opt-in, since it downloads Node
packages:

```bash
EVALSMITH_E2E=1 uv run pytest tests/test_export_run.py::TestAgainstTheRealRunner
```

The generated JavaScript assertions are themselves tested by executing them in
Node — reading them is not enough. The bug those tests exist for was an operator
precedence mistake (`(a||b).some(...)` written without the outer parentheses)
that made **every tool assertion silently pass**.

## Comparison

`evalsmith compare` aligns two runs by stable test ID and classifies every pair
— the guide's truth table, with its two error rows made explicit:

| Baseline | Candidate | Classification |
| --- | --- | --- |
| pass | pass | unchanged pass |
| fail | pass | **fixed** |
| pass | fail | **regression** |
| fail | fail | unchanged failure |
| error | *any* | not comparable — excluded |
| *any* | error | not comparable — excluded |

### Three rules about not overclaiming

**A test that errored is not a data point.** An error says the harness or the
target broke, not that the agent got the answer wrong. Errored pairs are
excluded from every count and reported separately. Comparing a run of timeouts
against a passing run prints *"No comparable tests. Nothing can be concluded"* —
not a spurious +100% improvement. Letting an outage read as a regression is the
single most damaging mistake this tool could make.

**Two runs are only comparable if they answered the same questions.** Every run
records a hash of the test IDs it covered; comparing across different suites is
refused unless you pass `--allow-suite-drift` to compare only the shared tests.

**A confidence interval is only reported when it means something.** Significance
uses **McNemar's exact test** — exact rather than the chi-square approximation,
because these suites are small and that is exactly where the approximation
fails. Only discordant pairs carry information: adding a hundred tests both runs
passed changes the headline rate but not the p-value.

The example makes the point better than any argument:

```
baseline pass rate      0.0%
candidate pass rate   100.0%
difference           +100.0%
p-value               0.2500
Only 3 test(s) changed outcome; that is too few for a trustworthy interval, so none is given.
```

A 0% → 100% improvement, and it is still **not statistically significant**. Three
tests cannot distinguish a real fix from a coin landing the same way three times.
An interval appears once at least 10 tests have changed outcome.

### The baseline moves only when you say so

`compare` uses whichever run was explicitly promoted, falling back to the newest
run for the `baseline` target only if nothing has ever been promoted.

```bash
evalsmith baseline promote <run-id> --reviewer alex --reason "shipped"
```

Promotions are an append-only record of who decided and when — which run was the
reference point, and why, is exactly the history a regression argument later
depends on. A run with any errored test **cannot** be promoted: a reference
point that only half ran is not a reference point.

For CI, `compare --fail-on-regression` exits non-zero when any test regressed.

## Storage

Ingested traces, their failure records and the current clustering go into
SQLite at `.evalsmith/database.db`, applied through versioned migrations
recorded in a `schema_migrations` table. The redacted trace
is stored whole as JSON and is the source of truth; its events are also written
as rows in the same transaction, as a derived index so detection can ask which
traces called `refund_order` without deserializing everything.

**A trace ID is never silently overwritten.** Storing one that already exists is
resolved by content, using a `sha256` hash of the interaction — input, output,
outcome and events, deliberately excluding the trace ID, all metadata and
timestamps, so the same interaction re-exported later hashes the same:

| Situation | What happens | Exit code |
| --- | --- | --- |
| New ID, new content | Stored | 0 |
| Same ID, same content | Skipped — re-ingesting a file is safe | 0 |
| Same ID, **different** content | Refused and reported as a conflict | 1 |
| New ID, same content | Skipped, reported as a duplicate interaction | 0 |

Use `--dry-run` to see exactly what an ingest would do, including which traces
are already known, without writing anything.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success |
| `1` | The command ran, but some records were invalid or rejected |
| `2` | The command could not run (bad usage, missing file, uninitialized project) |

## Development

```bash
uv sync                      # install runtime + dev dependencies
uv run pytest                # tests
uv run ruff check . && uv run ruff format --check .
uv run mypy                  # strict type checking
uv run pre-commit install    # run the gates on every commit
```

## License

Apache-2.0. See [LICENSE](LICENSE).
