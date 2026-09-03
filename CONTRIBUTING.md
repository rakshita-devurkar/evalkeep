# Contributing to Evalkeep

Thanks for taking a look. Evalkeep is early — 0.1, no releases yet — so the most
useful contributions right now are trace adapters, exporters, and reports of
what breaks against real data.

## Getting set up

```bash
git clone https://github.com/rakshita-devurkar/evalkeep && cd evalkeep
uv sync
uv run pytest
```

Node.js is only needed for `evalkeep run`, which shells out to Promptfoo.

## The gates

Everything CI checks, you can run locally. All four must pass:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy                  # strict, and it stays strict
uv run pytest
```

`uv run pre-commit install` wires the first three into your commits.

The end-to-end test against the real runner is opt-in, because it downloads Node
packages:

```bash
EVALKEEP_E2E=1 uv run pytest tests/test_export_run.py::TestAgainstTheRealRunner
```

## How the project is laid out

```
src/evalkeep/
  trace.py        the normalized trace schema everything else speaks
  adapters/       provider format → normalized trace
  redaction.py    runs between validation and storage, with no path around it
  detectors.py    evidence that something failed
  analysis.py     what kind of failure it is
  clustering.py   grouping failures into families
  generation.py   failure → draft regression test
  review.py       the approval gate
  exporters/      approved tests → runner configuration
  runner.py       invoke Promptfoo, import results
  comparison.py   two runs → fixed / regressed / excluded
  storage/        SQLite, one module per table group
  commands/       business logic, free of Typer so it stays testable
  cli.py          argument parsing and rendering only
```

[docs/pipeline.md](docs/pipeline.md) explains the reasoning behind each stage.
Read it before changing behaviour — most of what looks arbitrary is load-bearing.

## Conventions that are not negotiable

These are the project's actual thesis, not style preferences. A change that
breaks one of them will be asked to change:

- **Report evidence, never inference.** Detectors point at something a person or
  an evaluator recorded. No confidence scores, no thresholds invented for
  unlabelled data.
- **Automation never overwrites human judgement.** Re-running any stage
  refreshes derived data and leaves reviews, labels and edits alone. If you add
  a stage, it needs the same property.
- **Values are redacted before storage.** Redaction happens in memory, before
  storage, including on anything a model or a person writes back. Identifiers
  are the documented exception, not an oversight — see the
  [roadmap](docs/roadmap.md).
- **Never a shell.** Subprocesses take an argument list. Values that came from a
  trace are embedded as JSON, never concatenated into code.
- **Say only what the numbers support.** If a sample is too small for a claim,
  the tool says so instead of printing a number.

## Tests

Test behaviour, not implementation. Some specifics worth knowing:

- Generated JavaScript assertions are tested by **executing them in Node**, not
  by string comparison. An operator-precedence bug once made every tool
  assertion silently pass; reading them was not enough.
- Storage tests assert on the database, including foreign-key cascades and that
  re-saving replaces rather than appends.
- Redaction has a test that greps the raw database bytes for secrets that were
  ingested. If you add a redaction rule, add one of those.
- Avoid naming classes `Test*` or functions `test_*` in `src/` — pytest tries to
  collect them wherever they are imported.

## Pull requests

Keep them focused, explain the reasoning in the description, and make sure the
gates pass. If you are changing something the docs explain, update the docs in
the same PR.

## What is most wanted

- **Trace adapters** for Langfuse, Opik, OpenTelemetry — implement the protocol
  in `adapters/base.py` and register it. The contract is small on purpose.
- **Embedding providers** — the built-in one is lexical; a hosted model plugs
  into `embeddings/__init__.py` with a four-member protocol.
- **Exporters** for runners other than Promptfoo.
- **Reports of Evalkeep failing on real traces.** Redacted samples are ideal;
  a description of the shape is fine.
