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

Everything below works offline, with no API key, against the bundled example.

```bash
uv run evalkeep init
uv run evalkeep ingest examples/refund-agent/traces.jsonl   # validate, redact, store
uv run evalkeep detect                                      # find evidence-backed failures
```

Describe each failure so similar ones can be grouped — by hand, or with a model
if you configure one:

```bash
uv run evalkeep failures label trace-1042 \
  --type wrong_tool_argument --component tool_arguments --severity high \
  --summary "Refunded the oldest order instead of the newest order."
```

Group them, draft a test per representative, and review it:

```bash
uv run evalkeep discover        # embed, cluster, pick representatives
uv run evalkeep dataset build   # generate pending drafts
uv run evalkeep review          # approve / edit / reject / skip
```

Then run the approved suite against two versions of your agent and compare:

```bash
uv run evalkeep targets add baseline  --type python --path examples/refund-agent/agents/baseline.py  --function call_api
uv run evalkeep targets add candidate --type python --path examples/refund-agent/agents/candidate.py --function call_api
uv run evalkeep run --target baseline
uv run evalkeep run --target candidate
uv run evalkeep compare
```

The bundled `baseline.py` reproduces the bug the example traces recorded;
`candidate.py` fixes it:

```
baseline pass rate      0.0%
candidate pass rate   100.0%
difference           +100.0%
p-value               0.2500
Only 3 test(s) changed outcome; that is too few for a trustworthy interval, so none is given.
```

That last line is the point. A 0% → 100% improvement is still **not
statistically significant** on three tests, and Evalkeep says so rather than
letting you claim it.

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

- **Values are redacted before storage.** Emails, phone numbers, Luhn-checked
  payment cards, token prefixes and credential fields are removed in memory,
  between validation and storage, with no path around it. Identifiers
  (`trace_id`, `tool`, …) are deliberately left intact so the pipeline's links
  survive — if yours embed customer data, see
  [the roadmap](docs/roadmap.md#nothing-unredacted-is-ever-stored-is-too-strong).
- **Automation never overwrites human judgement.** Re-running detection,
  analysis, clustering or generation refreshes derived data and leaves your
  reviews, labels and cluster edits alone.
- **Nothing is exported without approval.** Generated tests are drafts; only
  approved tests reach a runner.
- **A test that never ran is not a test that failed.** Timeouts and crashed
  providers are excluded from every rate and reported separately, so an outage
  cannot read as a regression.
- **Score changes are not overclaimed.** Significance uses McNemar's exact test,
  and a confidence interval is withheld — with the reason printed — when too few
  tests changed outcome to support one.

## Documentation

- **[How it works](docs/pipeline.md)** — the design decisions behind each stage
- **[Privacy and security](docs/security.md)** — redaction, secrets, execution safety
- **[Roadmap](docs/roadmap.md)** — what 0.1 does not do yet, and which gaps could mislead
- **[Contributing](CONTRIBUTING.md)** — setup, conventions, and what is most wanted
- **[Changelog](CHANGELOG.md)** — including what 0.x compatibility does and does not promise

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success |
| `1` | The command ran, but some records were invalid or rejected |
| `2` | The command could not run (bad usage, missing file, uninitialized project) |

## Development

```bash
uv sync
uv run pytest                # 753 tests, 1 opt-in
uv run ruff check . && uv run ruff format --check .
uv run mypy                  # strict
uv run pre-commit install
```

`EVALKEEP_E2E=1 uv run pytest` additionally runs the suite against real
Promptfoo, which downloads Node packages.

## Status

Version 0.1 is feature-complete: the whole pipeline runs, from a raw trace file
to a statistically honest regression report. Not yet done: provider adapters
beyond generic JSONL, and the packaging work for a PyPI release.

## License

Apache-2.0. See [LICENSE](LICENSE).
