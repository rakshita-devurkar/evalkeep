# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Compatibility during 0.x

Evalkeep is pre-1.0 and its interfaces will change. Concretely, while the
version starts with `0.`:

- **A minor bump (0.1 → 0.2) may break anything**: the CLI, the project config,
  the on-disk layout, and the Python API.
- **A patch bump (0.1.0 → 0.1.1) will not break the CLI or the config**, and
  will not require you to re-ingest.
- **The database migrates forward automatically** and never backward. Migrations
  are append-only, so an older Evalkeep will refuse a newer database rather than
  corrupt it.
- **Approved tests are the durable artifact.** They are yours, they live in your
  Git history via export, and any change to the export format will come with a
  documented migration.

What is *least* likely to move: the normalized trace schema, the expectation
types, and the exit codes. What is *most* likely to move: clustering parameters
and the analyzer prompt, both of which are versioned so that changing them
invalidates the right caches rather than silently mixing results.

## [Unreleased]

## [0.1.1] - 2026-09-07

### Fixed

- The description on PyPI told you to clone the repository and run
  `uv run evalkeep`, because PyPI renders the README from the snapshot a release
  was built from and 0.1.0 was built before there was anything to install. Worse
  than stale: someone who installed the package and then followed its own
  instructions got errors. The README now installs from PyPI and calls
  `evalkeep` directly.
- `__version__` was written out by hand beside the version in pyproject.toml.
  The release check compared the tag against pyproject and nothing compared the
  two to each other, so 0.1.1 came within a commit of shipping a wheel that
  reported 0.1.0. It is now read from installed metadata, a test asserts the two
  agree, and the release refuses to publish a wheel whose `--version` is not the
  tag.


## [0.1.0] - 2026-09-07

The full pipeline, from a raw trace file to a regression report.

### Added

- A `tau-bench` example: 165 scored customer-service trajectories per model,
  with replay targets, so the comparison in the README can be reproduced rather
  than taken on trust
- `from-traces` — the whole pipeline in one command, from a trace file to the
  review queue, so a first run does not require understanding five stages first.
  It stops at review, skips traces it already has, rebuilds unreviewed drafts,
  and never touches a test you have reviewed
- `init` — safe, idempotent project setup
- `ingest` — streaming JSONL validation, deterministic redaction before storage,
  duplicate detection by trace ID and by content hash
- `detect` — evidence-backed failure candidates from explicit status, negative
  feedback and failed evaluators, with manual confirm/dismiss/add
- `analyze` and `failures label` — structured failure descriptions from a
  provider-independent analyzer interface, cached by content, model and prompt
  version; hand labelling is a first-class path and the default
- `discover` — deterministic embedding and clustering, with central, boundary
  and high-severity representatives, and merge/split/rename/dismiss. Failures
  nobody has described can be grouped by observed behaviour instead, at their
  own threshold, so a project with no analyzer still reaches a review queue;
  described and undescribed failures are clustered separately, because a
  distance measured between the two kinds of text means nothing
- `dataset build` — regression-test drafts with stable IDs, full provenance and
  contradiction detection, including for failures nobody has described yet: the
  draft forbids the action that was observed and says plainly that it cannot
  confirm what should have happened instead
- `review` — terminal approve/edit/reject/skip, plus non-interactive equivalents
- `targets` — HTTP, Python, JavaScript and direct model targets, refused if they
  contain a literal credential
- `export` — Promptfoo configuration and portable JSONL, approved tests only
- `run` — delegated execution via Promptfoo, with timeouts and provider errors
  distinguished from assertion failures
- `compare` — the fixed/regression truth table, errors excluded and reported
  separately, McNemar's exact test, and confidence intervals only when the
  sample supports one
- `baseline promote` — an explicit, recorded decision, never automatic
- `demo` — writes the bundled examples out, so a PyPI install has them too
- Trace adapters for OpenTelemetry (OpenInference conventions, `gen_ai.*`
  fallback) and LangSmith, both reading exported files rather than calling an
  API, so no credentials are needed and no vendor tier is required
- `run --repetitions N` — repeated execution with per-case verdicts and Wilson
  intervals, so one lucky pass is not reported as a fix
- Recorded fixtures published to the target at run time, so a replay reproduces
  the conditions the failure was recorded under
- Every occurrence of an interaction is kept, preserving frequency, date range
  and affected agent versions
- `redaction.pseudonymize_identifiers` — per-project salted tokens for
  identifiers that may themselves carry customer data
- A deterministic refund-agent example that runs with no API key and no network

### Notes

- Installing pulls in only six runtime dependencies; clustering and the exact
  binomial test are implemented directly rather than via SciPy and
  scikit-learn, which were 119 MB of install for two function calls.
- The LangSmith adapter is written against the documented `Run` schema and has
  not been exercised against a live account.

[0.1.0]: https://github.com/rakshita-devurkar/evalkeep/releases/tag/v0.1.0
