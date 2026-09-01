# Refund-agent example

A deterministic, synthetic dataset for a shopping agent that handles refunds.
It requires no API key and no network, and it is the dataset the end-to-end
acceptance test runs against.

`traces.jsonl` holds five traces in the generic Evalsmith format:

| Trace | What happened | Failure evidence |
| --- | --- | --- |
| `trace-1042` | Lists three orders, refunds the **oldest** | explicit status + negative feedback |
| `trace-1043` | Same mistake, different customer | explicit status + failed evaluator |
| `trace-1051` | Refunds **every** order when asked for one | explicit status + negative feedback |
| `trace-1060` | Answers an order-status question correctly | none (must not be flagged) |
| `trace-1061` | Answers a shipping question; outcome not recorded | none (status `unknown`) |

The first two belong to one failure family (right tool, wrong argument) and the
third to another (unrequested extra actions), so clustering has both a duplicate
pair to group and a distinct case to keep separate.

Validate it:

```bash
uv run evalsmith ingest examples/refund-agent/traces.jsonl --validate-only
```

No real customer data appears here. `shopper@example.com` is a reserved example
address, included so the redaction phase has something to scrub.
