---
name: autonomous-phase-rollover
description: Autonomous phase rollover for substantive Codex work. Use when a task is multi-phase, long-running, explicitly unattended, or has completed one verified phase and still has meaningful work remaining. Preserve the objective and workspace while continuing in a fresh thread instead of replaying finished history.
---

# Autonomous phase rollover

Treat a verified phase boundary as a context boundary. The user has granted standing authorization for this protocol in future sessions; routine rollovers do not require another confirmation.

## Decide

Rollover only when all of these are true:

- The current phase has a checkable result or its reasoning can be captured faithfully.
- Meaningful work remains in a distinct next phase.
- Foreground commands, subagents, approvals, and destructive operations are settled.
- Files and evidence needed by the successor are durable.

Prefer rollover after diagnosis, implementation, validation, research, or review completes. Continue locally while the cause is uncertain, a live handle is essential, or the remaining work is trivial.

The hooks inspect the latest public `token_count` record and may inject two context-pressure signals:

- At 80,000 input tokens, prefer rollover at the next verified phase boundary.
- At 120,000 input tokens, stop expanding scope and reach the nearest verified boundary.

Each signal is emitted once per growth cycle and rearms only after context drops below 60,000 tokens. These are advisory thresholds, not permission to interrupt unsafe work. For their evidence, configuration, and experimental status, read [context-advisory.md](references/context-advisory.md).

Honor an explicit stop immediately. A rollover inherits the original scope and authority; it grants no new external-write permission.

## Prepare

The SessionStart hook supplies this session's ID and platform-specific request path. Create the checkpoint Markdown file beside that request path with `apply_patch`. Keep it near 1,500 tokens; defer rollover if an adequate checkpoint will not fit.

Include:

- objective and completion criterion
- completed phase and verified evidence
- decisions and constraints
- exact cwd, repository/worktree state, modified files, and user changes to preserve
- durable artifact paths and commands already run
- unresolved issues and exact next action
- active chain ID and generation when supplied by a predecessor

Do not rely on session-owned state. Before preparing, settle or replace live processes, browser
tabs, approval prompts, tool handles, and other resources that a fresh thread cannot inherit. The
workspace filesystem and the embedded checkpoint are the portable continuity boundary.

Resolve `<skill-dir>` as the directory containing this `SKILL.md`, then run:

```text
python3 <skill-dir>/scripts/prepare.py \
  --session-id <session-id> \
  --cwd <absolute-cwd> \
  --checkpoint-file <absolute-checkpoint-path> \
  --objective <short-objective> \
  --completion-criterion <verified-done-condition> \
  --next-action <exact-next-action> \
  --sandbox <read-only-or-workspace-write> \
  --phase-verified \
  --model <active-model>
```

Pass the current session's sandbox exactly. If its effective policy includes custom writable roots,
network overrides, restricted-read rules, a named permission profile, or anything that cannot be
represented exactly as stock `read-only` or `workspace-write`, do not prepare a rollover. The controller always uses
`approvalPolicy: never` and declines unattended approval requests, so a continuation cannot acquire
additional authority interactively.

`prepare.py` emits a version-2 continuation capsule and binds the embedded checkpoint with its byte
count and SHA-256 digest. A completed successor turn closes that generation only; it does not prove
the objective complete. Verify the completion criterion or prepare the next generation.

For a successor, also pass the `--chain-id` and next `--generation` given in its handoff prompt. Add `--effort` only when the current effort is known. `prepare.py` validates the request and writes it to the hook-provided path.

After preparation, make no more task mutations. End the turn with a concise handoff status. The Stop hook atomically claims the request and starts the controller; the controller creates a fresh thread with the exact cwd, persists its ID before starting work, and safely declines any permission expansion.

## Successor ownership check

Every successor must perform the read-only ownership verification before task work. The trusted
controller preassigns the successor's session ID before starting its turn; this command does not
mutate the ledger:

```text
python3 <skill-dir>/scripts/controller.py claim \
  --chain-id <chain-id> \
  --generation <generation> \
  --session-id <session-id>
```

Exit nonzero means this worker does not own the generation: stop without mutations. Exit zero means continue from the checkpoint, inspect current workspace state, avoid completed investigation, and repeat this protocol at the next eligible boundary until the objective is verified complete.

## Failure behavior

The controller ledger is authoritative. Never create a second continuation manually after an uncertain dispatch. SessionStart recovery reconciles recorded `PREPARED`, `CHILD_CREATED`, and `ACTIVE` states. A `START_UNCERTAIN` generation is quarantined from automatic retry; its recorded child can still prove ownership if the original start actually arrived. An explicit interrupt cancels only a positively unstarted dispatch. If permissions or a user decision are genuinely required, preserve the checkpoint and report the blocker instead of expanding authority.

## Harness boundary

The checkpoint schema, ownership states, locking, and adapter contract are portable. Session creation is not: each harness needs an adapter that can create a history-free session with an exact cwd and report its ID before starting work. This package includes the Codex app-server adapter. For another harness, read [adapter-contract.md](references/adapter-contract.md) and implement that adapter; peer channels may carry compact notifications but are never the ownership ledger.
