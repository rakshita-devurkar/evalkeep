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

The full pipeline, built from a raw trace file to a regression report.

### Added

- `init` — safe, idempotent project setup
- `ingest` — streaming JSONL validation, deterministic redaction before storage,
  duplicate detection by trace ID and by content hash
- `detect` — evidence-backed failure candidates from explicit status, negative
  feedback and failed evaluators, with manual confirm/dismiss/add
- `analyze` and `failures label` — structured failure descriptions from a
  provider-independent analyzer interface, cached by content, model and prompt
  version; hand labelling is a first-class path and the default
- `discover` — deterministic embedding and clustering, with central, boundary
  and high-severity representatives, and merge/split/rename/dismiss
- `dataset build` — regression-test drafts with stable IDs, full provenance and
  contradiction detection
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
- A deterministic refund-agent example that runs with no API key and no network
