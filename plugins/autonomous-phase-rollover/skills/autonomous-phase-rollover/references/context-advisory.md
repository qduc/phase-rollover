# Context advisory

## Cost shape

Input cost for one request grows approximately linearly with the context sent in that request. If
retained context grows by a roughly fixed amount across repeated requests, cumulative replay grows
roughly quadratically with request count. Describe this as superlinear or quadratic, not exponential.

Starting a fresh task also has a fixed-context cost and may require rereading files. A token threshold
therefore advises when to seek a boundary; it never replaces the semantic safety gate.

## Candidate thresholds

The default signals are:

- advisory: 80,000 input tokens
- urgent: 120,000 input tokens
- rearm: below 75% of the advisory threshold, or 60,000 tokens by default

The 2026-09-15 refresh selected the latest 20 completed Codex rollout files and excluded active
runs. Across 547 deduplicated requests, the sample recorded 37,631,028 aggregate input tokens;
231 requests were at least 80,000 tokens and 150 were at least 100,000. The overall request average
was 68,795 tokens. Short fresh tasks in the same sample had median request sizes around 15,000 to
24,000 tokens, while long runs reached 123,577 to 177,491 tokens.

This supports 80,000 as a conservative first intervention point: it is roughly four times the
observed fresh-task floor and leaves room to settle a phase before the 120,000 urgent signal. It does
not prove that 80,000 is optimal across models, repositories, cache pricing, or task shapes.

## Runtime behavior

`UserPromptSubmit` catches pressure between user turns. `PreToolUse` catches pressure during a long
agent turn after a model request has selected a tool. Both read the newest `token_usage_record` or
`token_count` event from the rollout transcript path supplied by Codex. The former is persisted
before tool execution, avoiding an extra-request lag in `PreToolUse`. They never call a model or a
billing API.

The hook stores only the last emitted advisory level and observed token counts under
`~/.codex/phase-rollover/advisories/`. It emits each level once, avoiding a repeated prompt tax on
every tool call. If telemetry is absent, malformed, or outside the bounded transcript tail scan, the
hook stays silent.

Environment overrides:

```text
PHASE_ROLLOVER_ADVISORY_TOKENS=80000
PHASE_ROLLOVER_URGENT_TOKENS=120000
PHASE_ROLLOVER_TRANSCRIPT_SCAN_BYTES=8388608
```

An urgent threshold less than or equal to the advisory threshold is corrected to 150% of the
advisory threshold. Invalid or non-positive values fall back to defaults.

## Validation experiment

Treat historical telemetry as threshold-selection evidence, not a causal benchmark. Validate the
candidate with fixed tasks from the same repository commit, model, effort, prompt, tools, and
completion criteria:

1. Run current behavior and 80K advisory behavior in `A/B`, then reverse the order as `B/A`.
2. Change only the advisory threshold.
3. Record total, cached, and uncached input; request count; context median/max; rereads; latency;
   intervention timing; human input; and deterministic quality.
4. Reject any candidate with a quality regression. Repeat a 10–30% saving twice; confirm a saving
   above 30% once; otherwise adjust one threshold and rerun.

Do not add simulated savings from rollover, compaction, or tool-output caps. Those effects overlap.
