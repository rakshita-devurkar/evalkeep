# LangSmith example

`runs.jsonl` is a LangSmith export of the same five interactions recorded in
[`../refund-agent/traces.jsonl`](../refund-agent/traces.jsonl), as one `Run`
object per line.

> These are the **same five interactions** as the JSONL example, in a different
> format. Ingest them into their own project — putting two representations of one
> interaction in the same database is a genuine ID conflict, and Evalkeep will
> (correctly) refuse the second one.

```bash
evalkeep ingest examples/langsmith/runs.jsonl --format langsmith
evalkeep detect
```

## Getting your own export

Evalkeep reads a file and never calls the API, so any export route works and no
credentials go anywhere near it:

```python
from langsmith import Client

with open("runs.jsonl", "w") as handle:
    for run in Client().list_runs(project_name="my-project", limit=500):
        handle.write(run.json() + "\n")
```

A JSON array works too. Include feedback if you have it — it is the strongest
failure signal LangSmith carries, and the reason this adapter finds a failure
that the OpenTelemetry one cannot:

```python
runs = list(Client().list_runs(project_name="my-project", limit=500))
```

## What is in it

Each interaction is a `chain` root run, an `llm` run declaring the tool calls,
and a `tool` run per call. The adapter collapses the declared call and its
execution into one, and reads three kinds of evidence: an `error` field, a
`status` of `error`, and negative feedback.
