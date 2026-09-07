# Evalkeep

**Turn real AI-agent failures into reviewed regression tests, and measure whether
later versions fix or reintroduce them.**

AI applications produce thousands of production traces. Existing eval tools can
*execute* tests — the hard part is deciding which failures deserve permanent
coverage. Evalkeep owns that decision:

```
trace → failure evidence → failure family → representative case
      → reviewed regression test → runner execution → trustworthy comparison
```

**Evalkeep does not run your agent and is not an eval framework.** It generates
tests, delegates execution to [Promptfoo](https://promptfoo.dev), and compares
baseline against candidate. It sits upstream of your eval runner, not next to it.

## Install

```bash
git clone https://github.com/rakshita-devurkar/evalkeep && cd evalkeep
uv sync
```

Node.js is needed only for `evalkeep run`, which shells out to Promptfoo.

## Quick start

The whole pipeline, offline, no API key, against the bundled example:

```bash
uv run evalkeep demo .          # write the example traces and agents out
uv run evalkeep init
uv run evalkeep ingest refund-agent/traces.jsonl           # validate, redact, store
uv run evalkeep detect                                     # evidence-backed failures

uv run evalkeep failures label trace-1042 \
  --type wrong_tool_argument --component tool_arguments --severity high \
  --summary "Refunded the oldest order instead of the newest order."

uv run evalkeep discover        # embed, cluster, pick representatives
uv run evalkeep dataset build   # draft a test per representative
uv run evalkeep review          # approve / edit / reject / skip

uv run evalkeep targets add baseline  --type python --function call_api \
  --path refund-agent/agents/baseline.py
uv run evalkeep targets add candidate --type python --function call_api \
  --path refund-agent/agents/candidate.py
uv run evalkeep run --target baseline
uv run evalkeep run --target candidate
uv run evalkeep compare
```

`baseline.py` reproduces the bug the example traces recorded; `candidate.py`
fixes it:

```
baseline pass rate      0.0%
candidate pass rate   100.0%
difference           +100.0%
p-value               0.2500
Only 3 test(s) changed outcome; that is too few for a trustworthy interval, so none is given.
```

That last line is the point: a 0% → 100% improvement is still **not
statistically significant** on three tests, and Evalkeep says so.

### Bring your own traces

```bash
uv run evalkeep ingest spans.json --format otlp        # OpenTelemetry / OpenInference
uv run evalkeep ingest runs.jsonl --format langsmith   # LangSmith
```

`evalkeep demo` also writes an example export in each format.

Adapters read files, never APIs — no credentials, any vendor tier. OpenTelemetry
covers the most ground, since Langfuse, Braintrust and Phoenix all ingest OTLP.

## Commands

| Stage | Commands |
| --- | --- |
| Set up | `init`, `targets add/list/show/remove` |
| Ingest | `ingest`, `trace list/show` |
| Detect | `detect`, `failures list/show/confirm/dismiss/add` |
| Analyze | `analyze`, `failures label` |
| Group | `discover`, `clusters list/show/rename/merge/split/dismiss/restore` |
| Build | `dataset build/list/show` |
| Review | `review`, `dataset approve/reject/edit` |
| Run | `export`, `run --target ...`, `runs list/show` |
| Compare | `compare`, `baseline promote/show` |

## What it guarantees

- **Values are redacted before storage** — in memory, with no path around it.
  Identifiers can be [pseudonymized](docs/security.md#identifiers) too.
- **Automation never overwrites human judgement.** Re-running any stage
  refreshes derived data and leaves your reviews, labels and edits alone.
- **Nothing is exported without approval.** Generated tests are drafts.
- **A test that never ran is not a test that failed.** Timeouts and crashed
  providers are excluded from every rate, so an outage cannot read as a regression.
- **One lucky pass is not a fix.** `run --repetitions N` reports a per-case
  verdict; a case that only sometimes passes is flaky, never passing.
- **Score changes are not overclaimed.** McNemar's exact test, and no confidence
  interval when the sample cannot support one.

## Documentation

- **[How it works](docs/pipeline.md)** — the design decisions behind each stage
- **[Privacy and security](docs/security.md)** — redaction, secrets, execution safety
- **[Roadmap](docs/roadmap.md)** — what 0.1 does not do yet
- **[Contributing](CONTRIBUTING.md)** — setup, conventions, what is most wanted
- **[Changelog](CHANGELOG.md)** — what 0.x compatibility does and does not promise

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success |
| `1` | The command ran, but some records were invalid or rejected |
| `2` | The command could not run (bad usage, missing file, uninitialized project) |

## Development

```bash
uv sync && uv run pytest              # 944 tests
uv run ruff check . && uv run mypy    # lint and strict types
```

`EVALKEEP_E2E=1 uv run pytest` also runs the suite against real Promptfoo.

## Status

0.1 is feature-complete: a raw trace file through to a statistically honest
regression report, reading OpenTelemetry, LangSmith or its own JSONL. Not yet
done — multi-turn replay, longitudinal failure history, and a PyPI release. See
the [roadmap](docs/roadmap.md).

## License

Apache-2.0. See [LICENSE](LICENSE).
