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

Version 0.1 is under construction. Working today:

| Command | Status |
| --- | --- |
| `evalsmith version` | ✅ |
| `evalsmith init` | ✅ |
| `evalsmith ingest` | ✅ |
| `evalsmith trace list` / `trace show` | ✅ |
| `evalsmith detect` | ✅ |
| `evalsmith failures list` / `show` / `confirm` / `dismiss` / `add` | ✅ |
| `evalsmith discover` | planned |
| `evalsmith dataset build` | planned |
| `evalsmith review` | planned |
| `evalsmith export` | planned |
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

## Storage

Ingested traces and their failure records go into SQLite at
`.evalsmith/database.db`, applied through versioned migrations recorded in a
`schema_migrations` table. The redacted trace
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
