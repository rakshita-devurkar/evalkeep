# Reproducing the tau-bench comparison

The number in the README — a 38.2 point difference between two models, on tests
generated from one of them — comes from this walkthrough. Everything here runs
from a `pip install`, so it can be checked rather than taken on trust.

## Why this dataset

Most public agent data has one half of what a regression suite needs.
[GXCafe's failure logs](https://huggingface.co/datasets/GXCafe/ai-agent-failure-logs)
have real verdicts but no prompts or tool calls, so nothing can be replayed.
[Real pi coding sessions](https://huggingface.co/datasets/MaxDevv/real-pi-coding-agent-traces-sessions)
have real prompts and tool calls but nothing marking a failure — a `bash` call
exiting non-zero is a coding agent doing its job. Evalkeep ingests both and
reports no failures for the second, which is correct and not very useful.

[tau-bench](https://huggingface.co/datasets/AgentSuite/tau-bench-trajectories)
has both. It runs the same 165 retail and airline customer-service tasks against
many models and scores each run by comparing the final database state against
the expected one. Real tool calls, and a verdict that does not depend on anyone
reading the output.

It is not production traffic: the customer is simulated and the tasks come from
a benchmark. It is the closest public data to a real end-to-end test.

## Run it

```bash
evalkeep demo . && evalkeep init
python tau-bench/prepare.py --into tau-bench    # ~8 MB, two models
```

`prepare.py` downloads two models' trajectories, converts them to Evalkeep
traces, and writes a replay target for each. A replay target returns what that
model actually did, so `compare` scores two recorded systems rather than a
simulation of them.

```bash
evalkeep from-traces tau-bench/Qwen3-235B-A22B-FP8.traces.jsonl
```

```
165 trace(s) ingested
90 failure(s) found  explicit_status x90, failed_evaluator x90
4 failure famil(ies)
```

Four families from 90 failures: retail exchange and refund flows, airline
reservation changes, and two shapes of giving up and escalating to a human.

```bash
evalkeep dataset build --all         # a test per failure, not per representative
evalkeep review                      # approve them
evalkeep targets add baseline  --type python --function call_api \
  --path tau-bench/replay_Qwen3_235B_A22B_FP8.py
evalkeep targets add candidate --type python --function call_api \
  --path tau-bench/replay_claude_4_5_sonnet_thinking_off.py
evalkeep run --target baseline && evalkeep run --target candidate
evalkeep compare
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

## Reading the result honestly

**Baseline at 4.5% is the control.** The tests were generated from that model's
own failures, so it should fail nearly all of them. A baseline that scored well
would mean the generated tests do not capture what went wrong.

**The four it passes are a real weakness.** With nothing describing a failure,
assertions target the last tool call, and that is sometimes a harmless lookup
rather than the mistake. Describing the failures — by hand or with an analyzer —
sharpens them. The drafts say so themselves.

**The excluded test is excluded, not failed.** Its target raised rather than
answered. Counting it as a failure would let an outage read as a regression.

## Two hazards this surfaced

Both are in the [roadmap](roadmap.md); they are worth knowing before you build
your own replay harness.

**An exported test carries the redacted prompt.** Redaction runs before storage,
so 27 of these 165 inputs are not byte-identical to what the recorded agent saw
— the task instruction contains an email address. A target that recognizes a
request by its text will not find it. `prepare.py` therefore matches on letters
after dropping addresses and redaction markers.

**A test that only forbids a mistake passes against an agent that does nothing.**
Every negative expectation is satisfied by an empty response. When the lookup
above silently missed, 12 cases scored as *passes* rather than errors, and the
headline moved by four points once it was fixed. The wrong number looked
entirely plausible. `prepare.py` now raises on a miss for exactly this reason.
