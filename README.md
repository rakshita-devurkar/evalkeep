# Evalkeep

[![PyPI](https://img.shields.io/pypi/v/evalkeep)](https://pypi.org/project/evalkeep/)
[![Python](https://img.shields.io/pypi/pyversions/evalkeep)](https://pypi.org/project/evalkeep/)
[![License](https://img.shields.io/pypi/l/evalkeep)](LICENSE)

**Stop fixing the same agent bug twice.** Evalkeep turns production failures
into a small, reviewed regression suite, and tells you whether a fix held — or
that the evidence is too thin to say.

Existing eval tools *execute* tests. The hard part is deciding which of
thousands of production traces deserve permanent coverage — and Evalkeep does
not run your agent or replace your eval framework. It sits upstream, generating
tests and delegating execution to [Promptfoo](https://promptfoo.dev).

```
trace → failure evidence → failure family → representative case
      → reviewed regression test → runner execution → trustworthy comparison
```

## Install

```bash
uv tool install evalkeep     # or: pipx install evalkeep, pip install evalkeep
```

Python 3.11+. Node.js is needed only for `evalkeep run`, which shells out to
Promptfoo. The examples ship inside the package, so everything below works from
a fresh install with no clone.

## Quick start

One command takes a trace file to a review queue. Offline, no API key:

```bash
evalkeep demo .
evalkeep init
evalkeep from-traces refund-agent/traces.jsonl
```

```
5 trace(s) ingested
3 failure(s) found  explicit_status x3, failed_evaluator x1, negative_feedback x2
2 failure famil(ies)
3 with enough evidence for a regression test
3 of them only forbid the mistake that was observed; say what should have happened at review.

Review them: evalkeep review  (3 pending)
```

That second qualifier is the honest state of a first run: nothing in a trace
says what the agent *should* have done. Describing the failures closes it, by
hand at review or with an analyzer configured. It stops at review on purpose —
approving a test is a judgement.

## Does it work on real data?

On [tau-bench](https://huggingface.co/datasets/AgentSuite/tau-bench-trajectories)
— 165 customer-service tasks per model, each scored by comparing the final
database state against the expected one — Evalkeep turns one model's 90 failures
into 4 families, and a test per failure. Running the suite against that model
and a stronger one:

```
compared                           89
baseline pass rate               4.5%
candidate pass rate             42.7%
difference                     +38.2%
p-value                        0.0000
95% interval         +27.2% to +49.2%
McNemar's exact test: the change is unlikely to be chance.

1 test(s) excluded and not counted in any rate above.
```

Baseline at 4.5% is the control: the tests came from its own failures, so it
should fail nearly all of them. The excluded test is one whose target raised
rather than answered — kept out of every rate rather than counted as a failure,
so an outage cannot read as a regression.

The example ships with the package. **[Reproduce it](docs/tau-bench.md)** in
about five minutes, including what the four passing baseline tests say about the
limits of generating a test with nothing describing the failure.

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

## Doing it stage by stage

`from-traces` runs five commands in order. Each is a real decision with its own
evidence, and you will want them separately once you are tuning a suite:

```bash
evalkeep ingest traces.jsonl   # validate, redact, store
evalkeep detect                # evidence-backed failures
evalkeep analyze               # describe them (or: failures label)
evalkeep discover              # embed, cluster, pick representatives
evalkeep dataset build         # draft a test per representative
```

Re-running `from-traces` is safe: it skips traces it already has and rebuilds
drafts, but never touches a test you have reviewed.

## Bring your own traces

```bash
evalkeep ingest opentelemetry/spans.json --format otlp   # OpenTelemetry / OpenInference
evalkeep ingest langsmith/runs.jsonl --format langsmith  # LangSmith
```

Those paths are what `evalkeep demo` writes: the same five interactions as each
tool exports them. Adapters read files, never APIs — no credentials, any vendor
tier — and OpenTelemetry covers the most ground, since Langfuse, Braintrust and
Phoenix all ingest OTLP.

## More

[How it works](docs/pipeline.md) · [Reproduce the tau-bench run](docs/tau-bench.md) ·
[Privacy and security](docs/security.md) · [Roadmap](docs/roadmap.md) ·
[Contributing](CONTRIBUTING.md) · [Changelog](CHANGELOG.md)

```bash
git clone https://github.com/rakshita-devurkar/evalkeep && cd evalkeep
uv sync && uv run pytest              # 985 tests
uv run ruff check . && uv run mypy    # lint and strict types
```

`EVALKEEP_E2E=1 uv run pytest` also runs the suite against real Promptfoo.
`evalkeep --help` lists every command.

0.1 is feature-complete. Multi-turn replay, longitudinal failure history, and
clustering that does not hold the whole distance matrix in memory are still
open; see the [roadmap](docs/roadmap.md). Pre-1.0, minor versions may break the
CLI and the on-disk layout — see the [changelog](CHANGELOG.md).

## License

Apache-2.0. See [LICENSE](LICENSE).
