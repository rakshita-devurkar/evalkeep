# Privacy and security

Evalkeep is built to be pointed at production traces, which means it is built
to assume those traces contain things that must never be stored, committed or
sent anywhere. This page collects every guarantee it makes and every place it
deliberately errs toward caution.

## What is committed, and what is not

| Committed to Git | Never committed |
| --- | --- |
| `evalkeep.yaml` project config | `.env` |
| `targets.yaml` (secret-free by construction) | `.evalkeep/database.db` |
| Approved test exports | `.evalkeep/data/`, `cache/`, `runs/` |

`evalkeep init` writes these exclusions into `.gitignore` for you.

## Redaction

Redaction runs **in memory, between validation and storage**. There is no code
path that writes a raw trace, so the database, exports and anything later sent
to an analyzer only ever hold redacted values.

| Rule | Catches | Replaced with |
| --- | --- | --- |
| `emails` | Email addresses | `[REDACTED:email]` |
| `phone_numbers` | Separator-formatted and E.164 numbers | `[REDACTED:phone]` |
| `payment_cards` | 13–19 digit runs that pass a Luhn check | `[REDACTED:payment_card]` |
| `token_prefixes` | `sk-`, `ghp_`, `xox*-`, `AKIA`, `AIza`, JWTs, `Bearer …` | `[REDACTED:token]` |
| `secret_field_names` | Values under keys like `api_key`, `password`, `authorization` | `[REDACTED:secret_field]` |

Toggle any rule in `evalkeep.yaml`. The rules err toward over-redaction — a
redacted order number is an inconvenience, a stored API key is an incident —
with two deliberate exceptions that keep the data usable:

- **Identifiers are never rewritten.** `trace_id`, `event_id`, `call_id` and
  `tool` survive verbatim; scrubbing them would break the links the whole
  pipeline runs on.
- **Secret *field names* only redact strings and containers.** `token_count: 512`
  is a number, not a credential.

Luhn does real work here: `4111 1111 1111 1111` is replaced, while the 13-digit
order number `1234567890123` and the total `$24.00` are left alone. Phone numbers
are matched before cards, because a phone sitting next to a card forms a single
digit run in which Luhn alone cannot say which digits belong to which.

Redaction is deterministic and uses fixed placeholders rather than hashes of the
original, so two customers' email addresses collapse to the same value — which
is exactly what clustering wants.

## Targets never hold credentials

`targets.yaml` **is** committed, so a literal credential in one would be a leak.
That is enforced rather than requested: saving a target whose configuration
contains something that looks like a credential is refused, and the same
detectors that redact traces do the looking. Secrets are `${ENV_VAR}`
references, resolved at run time, and a run stops before it starts if a
referenced variable is unset.

## The runner is never given a shell

Test inputs, tool names and file paths all come from recorded traces. Building a
command string out of them would make a trace containing `; rm -rf` a
remote-code-execution bug, so the runner is invoked with an argument list and an
explicit `shell=False`.

For the same reason, every literal in a generated assertion is embedded with
`json.dumps` rather than concatenated into JavaScript — the same class of bug as
SQL injection. There is a test that puts `order-"); process.exit(1); //` into an
expectation and checks the assertion still evaluates correctly.

## What the analyzer sees

A model only ever receives an already-redacted trace. Its response is then
redacted **again** before storage — a model can quote its input, and "it only
saw redacted text" is an argument, not a guarantee. The same applies to
summaries you type by hand during review.

Analysis is off by default: `analyzer.provider` is `manual`, and the whole
pipeline runs offline with hand-written labels and a local embedder.

## Reporting a vulnerability

Open a GitHub issue for anything non-sensitive. For a suspected vulnerability,
please report it privately through GitHub's security advisories rather than a
public issue.
