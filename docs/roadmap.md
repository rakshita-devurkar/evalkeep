# Roadmap

Version 0.1 completes the pipeline: a trace file becomes a reviewed regression
suite, runs against two agents, and produces a comparison. This page records
what it does **not** yet do.

Two kinds of entry are mixed together here, and the distinction matters more
than the ordering:

- **Gap** — a place where the current behaviour could mislead someone. These
  are closer to defects than to features, and they are listed first.
- **Capability** — something Evalkeep does not do yet, but does not pretend to.

---

## Gaps

### Repeated execution and per-case confidence — *addressed*

**Resolved.** `run --repetitions N` executes each test N times and stores every
execution. A case now gets a verdict across its repetitions — `pass`, `fail` or
`flaky` — with a Wilson interval, and `compare` gained `likely_fixed` for a case
that improved without becoming reliable. A flaky case is never counted as
passing. See [Repeated execution](pipeline.md#repeated-execution).

**Was.** `run` executed each test once, and `compare` classified a case as
`fixed` on the strength of that single execution.

**Why it matters.** Agents are stochastic. A case can pass once by chance and be
reported as fixed when it is not. McNemar's test measures whether the *suite*
changed; it says nothing about whether an *individual* failure is genuinely
resolved, and the per-case labels currently imply more than one run can support.
This is the largest honesty gap in the tool, and it sits in the one place the
project claims as its wedge.

**Sketch.**

```bash
evalkeep run --target candidate --repetitions 20
```

Store every repetition rather than an aggregate. Then classify per case:

| Label | Meaning |
| --- | --- |
| `fixed` | passed every repetition |
| `likely_fixed` | passed most, within a stated interval |
| `flaky` | passed some — the case is non-deterministic, which is itself a finding |
| `reintroduced` | failed every repetition |

A per-case binomial interval belongs here, and it is the natural place for the
`MIN_DISCORDANT_FOR_INTERVAL` restraint to be relaxed, since repetitions supply
the sample size that a small suite cannot.

### Recorded fixtures are not replayed — *addressed*

**Resolved.** Fixtures are published to every test as a `fixtures` variable, and
the export warns when a target cannot receive them. What remains open is that a
target must choose to read them; Evalkeep cannot intercept a black-box agent's
tool calls. See [Fixture replay](pipeline.md#fixture-replay).

**Was.** `dataset build` recorded every tool result the original agent saw, in
`RegressionTest.fixtures`, with the arguments that produced it. The Promptfoo
exporter never read them.

**Why it matters.** The test re-runs the agent against whatever its tools return
*now*. If the shop has different orders than it did when the trace was recorded,
the test is not reproducing the recorded failure — it is asking a different
question and comparing the answer to the old one. Evalkeep is already paying the
storage cost of the fixtures and getting none of the benefit.

**Sketch.** Inject fixtures into the generated configuration so the target's
tool calls resolve against recorded results. For script targets this can be a
harness that wraps the provider; for HTTP targets it likely needs an opt-in
proxy or a documented convention, since Evalkeep cannot intercept a remote
service's own tool calls. Worth designing before implementing — the HTTP case
may only be honestly solvable by asking the target to accept fixtures.

### Multi-turn traces collapse to a single message

**Today.** `_input_text` in the Promptfoo exporter reduces a test input to one
string: `input.text`, or failing that the first user message.

**Why it matters.** A failure that only appears on the fourth turn of a
conversation cannot be reproduced from the first turn. The trace schema models
multi-turn interactions correctly and the store preserves them; the loss happens
at export, silently, which is the worst place for it.

**Sketch.** Emit the full message list and let the target consume a conversation.
Where a runner cannot express multi-turn input, the export should **refuse** the
test with an explanation rather than quietly truncate it.

### Duplicate interactions are discarded — *addressed*

**Resolved.** Every sighting is recorded in `trace_occurrences` while `traces`
still holds one row per distinct interaction, so frequency, date range and
affected agent versions survive. Occurrence IDs are derived from the sighting,
so re-ingesting a file records nothing new. See
[Occurrences](pipeline.md#occurrences).

**Was.** Ingest computed a content hash and skipped a trace whose content
already existed under another ID, reporting it as a content duplicate and
losing everything the repeat could have told you.

**Why it matters.** Deduplication is right for the *test suite* — you want one
test per failure family, not forty. But it is wrong for the *evidence*. Throwing
away repeat occurrences loses how often a failure happens, whether it is getting
worse, which versions it affects, and how many users hit it. That is exactly the
information a severity judgement should rest on, and it is currently discarded at
the front door.

**Sketch.** Store every occurrence; deduplicate on the *normalized content hash*
for selection rather than at ingest. Failures then carry an occurrence count, a
first-seen and last-seen, and the set of agent versions affected — feeding both
severity and the history command below.

### "Nothing unredacted is ever stored" is too strong — *addressed*

**Resolved.** `redaction.pseudonymize_identifiers` replaces trace, event and call
IDs with tokens derived from a per-project salt. The original is never stored,
links survive because the mapping is deterministic, and lookups still accept the
IDs your own systems use. When it is off, ingest warns if an identifier looks
like it carries personal data rather than staying silent. See
[Identifiers](security.md#identifiers).

**Was.** Redaction deliberately exempted identifiers — `trace_id`, `event_id`,
`call_id`, `tool`, `name` — because rewriting them would break the links the
whole pipeline runs on.

**Why it matters.** Identifiers are not always opaque. A `trace_id` of
`order-jane@example.com-2026-06-01` carries a customer's address straight through
redaction and into the database, exports, and any analyzer prompt. The guarantee
as documented is stronger than the code delivers, and a security claim that
overstates is worse than one that is narrow and true.

**Sketch.** Deterministic pseudonymization: replace each identifier with a stable
token derived from a per-project salt, so links survive, the mapping is
reproducible within a project, and the original value is not recoverable from the
database alone. Keep the salt out of Git. Until then the documentation states the
exemption plainly rather than claiming more than it should.

---

## Capabilities

### Failure-family history across versions

**Today.** `compare` is strictly pairwise: one baseline run against one candidate
run. Every earlier run is retained but never read across.

**Why it matters.** The most valuable question about a regression test is not
"did it pass today" but "what has this failure done over time". A case that has
been fixed and reintroduced three times is a different engineering problem from
one that failed once.

**Sketch.**

```bash
evalkeep history refund_latest_order
```

```
v1  failed
v2  fixed
v3  remained fixed
v4  reintroduced
```

The data is already stored — `evaluation_runs`, `test_results`, and
`baseline_promotions` — so this is a query and a renderer, not a schema change.
It becomes considerably more useful once repetitions exist, since each point
gains a confidence rather than being a single coin flip.

### OpenTelemetry / OpenInference adapter — *addressed*

**Resolved.** `--format otlp` reads OTLP JSON with OpenInference conventions and
a `gen_ai.*` fallback, and `--format langsmith` reads exported LangSmith runs.
Both read files rather than calling an API. What remains open is adapters for
platforms that speak neither — and the honest limit that OpenTelemetry cannot
carry user feedback, so failures whose only evidence is a person's judgement are
not detectable from spans alone. See
[Where traces come from](pipeline.md#where-traces-come-from).

**Was.** Only the generic JSONL adapter existed. Everyone had to transform their
traces into Evalkeep's format before they could try it.

**Why it matters.** This is the single largest adoption barrier. OpenTelemetry
with OpenInference semantic conventions is where agent tracing is converging, and
supporting it means a large set of users can point Evalkeep at traces they
already have. **This should probably land before a PyPI release** — a package
nobody can feed is not much use.

**Sketch.** Implement the `TraceAdapter` protocol in `src/evalkeep/adapters/`.
The contract is deliberately small: stream records, yield a normalized trace or
the issues rejecting it, never raise on bad input data. The mapping work is in
locating the input, output, tool calls and failure signal within the span
attributes.

### PyPI release

**Today.** Packaging metadata, classifiers and a `py.typed` marker are in place
and the wheel builds. There is no tag, no release and no published package.

**Why it matters.** `git clone` is a real barrier to trying something.

**Sketch.** Tag `v0.1.0`, publish a GitHub release from the changelog, and
publish to PyPI with a trusted-publisher workflow rather than a long-lived token.
Best done after the OpenTelemetry adapter, and after the clean-install test in
the guide's Appendix B has actually been run on a machine that has never seen
this project.

---

## Deliberately out of scope

From the project's own strategic constraint, these are not roadmap items and
requests for them will be declined: a general eval framework, an observability
platform, a model gateway, a prompt manager, or a hosted dashboard. Evalkeep
decides what belongs in a regression suite and makes that decision trustworthy.
Established runners execute the tests.
