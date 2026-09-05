# OpenTelemetry example

`spans.json` is an OTLP JSON export of the same five interactions recorded in
[`../refund-agent/traces.jsonl`](../refund-agent/traces.jsonl), using
[OpenInference](https://github.com/Arize-ai/openinference) semantic conventions.
Reading all three side by side shows what each format can and cannot carry.

```bash
evalkeep ingest examples/opentelemetry/spans.json --format otlp
evalkeep detect
```

## What is in it

Each interaction is three or more spans: an `AGENT` root, an `LLM` span that
declares the tool calls, and a `TOOL` span per call that records what came back.
That double-recording is normal, and the adapter collapses it — the intent and
the execution are one call, not two.

## What OpenTelemetry cannot tell you

Detection finds **two** failures here and **three** in the other two formats.
That is correct, not a bug.

The third interaction refunds every order when the user asked for one. Every
call returned OK, so nothing errored; its only evidence is a person saying it was
wrong. OpenTelemetry has nowhere to put that, so Evalkeep reports finding nothing
rather than inventing a signal.

If your failures look like that one — technically successful, obviously wrong —
you will need feedback alongside your spans, or to label them by hand with
`evalkeep failures add`.
