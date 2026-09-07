# Evalkeep

**Stop fixing the same agent bug twice.** Evalkeep turns production failures
into a small, reviewed regression suite, and tells you whether a fix held — or
that the evidence is too thin to say.

Existing eval tools *execute* tests. The hard part is deciding which of
thousands of production traces deserve permanent coverage. Evalkeep owns that
decision:

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

One command takes a trace file to a review queue. Offline, no API key:

```bash
uv run evalkeep demo .
uv run evalkeep init
uv run evalkeep from-traces refund-agent/traces.jsonl
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

## On real agent data

The example above is five traces. Here is the same pipeline on
[tau-bench trajectories](https://huggingface.co/datasets/AgentSuite/tau-bench-trajectories):
165 retail and airline customer-service tasks per model, each scored by
comparing the final database state against the expected one. Real tool calls,
and an independent verdict — the two halves a regression suite needs.

```bash
python tau-bench/prepare.py                      # ~8 MB, two models
uv run evalkeep from-traces tau-bench/Qwen3-235B-A22B-FP8.traces.jsonl
```

```
165 trace(s) ingested
90 failure(s) found  explicit_status x90, failed_evaluator x90
4 failure famil(ies)
```

Four families, from 90 failures: retail exchange and refund flows, airline
reservation changes, and two shapes of giving up and escalating to a human.
Build a test per failure, approve them, and run two recorded models against the
suite one of them produced:

```bash
uv run evalkeep dataset build --all
uv run evalkeep review
uv run evalkeep targets add baseline  --type python --function call_api \
  --path tau-bench/replay_Qwen3_235B_A22B_FP8.py
uv run evalkeep targets add candidate --type python --function call_api \
  --path tau-bench/replay_claude_4_5_sonnet_thinking_off.py
uv run evalkeep run --target baseline && uv run evalkeep run --target candidate
uv run evalkeep compare
```

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

Baseline scoring 4.5% is the control: the tests came from its own failures, so
it should fail nearly all of them. The four it passes are the documented
weakness of deriving a test with nothing describing the failure — assertions
target the last tool call, which is sometimes a harmless lookup. The excluded
test is one whose target raised rather than answered, and it is kept out of
every rate rather than counted as a failure.

## Doing it stage by stage

`from-traces` runs five commands in order. Each is a real decision with its own
evidence, and you will want them separately once you are tuning a suite:

```bash
uv run evalkeep ingest traces.jsonl   # validate, redact, store
uv run evalkeep detect                # evidence-backed failures
uv run evalkeep analyze               # describe them (or: failures label)
uv run evalkeep discover              # embed, cluster, pick representatives
uv run evalkeep dataset build         # draft a test per representative
```

Re-running `from-traces` is safe: it skips traces it already has and rebuilds
drafts, but never touches a test you have reviewed.

## Bring your own traces

```bash
uv run evalkeep ingest spans.json --format otlp        # OpenTelemetry / OpenInference
uv run evalkeep ingest runs.jsonl --format langsmith   # LangSmith
```

Adapters read files, never APIs — no credentials, any vendor tier. OpenTelemetry
covers the most ground, since Langfuse, Braintrust and Phoenix all ingest OTLP.
`evalkeep demo` writes an example export in each format.

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

## More

[How it works](docs/pipeline.md) · [Privacy and security](docs/security.md) ·
[Roadmap](docs/roadmap.md) · [Contributing](CONTRIBUTING.md) ·
[Changelog](CHANGELOG.md)

```bash
uv sync && uv run pytest              # 985 tests
uv run ruff check . && uv run mypy    # lint and strict types
```

`EVALKEEP_E2E=1 uv run pytest` also runs the suite against real Promptfoo.
Exit codes: `0` success, `1` ran but some records were rejected, `2` could not
run.

0.1 is feature-complete and not yet released to PyPI. Multi-turn replay and
longitudinal failure history are still open; see the
[roadmap](docs/roadmap.md).

## License

Apache-2.0. See [LICENSE](LICENSE).
