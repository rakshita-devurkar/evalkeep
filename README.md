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
| `evalsmith ingest --validate-only` | ✅ |
| `evalsmith ingest` (redact + store) | planned |
| `evalsmith detect` | planned |
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
.evalsmith/data/      redacted traces and intermediate artifacts
.evalsmith/cache/     analyzer and embedding caches
.evalsmith/runs/      raw Promptfoo run outputs
.evalsmith/exports/   generated export files
```

and appends the state paths to `.gitignore`. **Approved tests and configuration
belong in Git; raw traces, the database, caches and run outputs do not.**

Then check a trace file against the schema:

```bash
uv run evalsmith ingest examples/refund-agent/traces.jsonl --validate-only
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
