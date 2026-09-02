## What this changes

<!-- One or two sentences. If it fixes an issue, link it. -->

## Why

<!-- The reasoning, not just the mechanics. If it changes behaviour that
     docs/pipeline.md explains, say what the docs said and why it should
     now say something else. -->

## Checks

- [ ] `uv run ruff check . && uv run ruff format --check .`
- [ ] `uv run mypy`
- [ ] `uv run pytest`
- [ ] Docs updated, if this changes documented behaviour

## Conventions

Confirm this change does not break any of these — see [CONTRIBUTING.md](../CONTRIBUTING.md):

- [ ] Detectors report **evidence, not inference** — no invented thresholds or confidence scores
- [ ] Re-running any stage **does not overwrite** reviews, labels or human edits
- [ ] **Nothing unredacted** reaches storage, exports or logs
- [ ] Subprocesses get an **argument list, never a shell string**; trace values are embedded as JSON
- [ ] Statistics **say only what the sample supports**

<!-- If a box does not apply, say so rather than ticking it. -->
