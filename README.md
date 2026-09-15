# Phase Rollover

`phase-rollover` is a Codex plugin for continuing long-running agent work in fresh sessions with durable checkpoints and explicit ownership. It recreates the useful public behavior of an in-thread context reset without depending on Codex's gated, model-only `new_context` tool.

## Why

In matched real-workflow experiments, compact fresh-session handoffs reduced aggregate input tokens by 73.0% and uncached input by 80.9% while preserving exact deterministic results. The intervention removes completed transcript history from later phases rather than tuning a global context limit.

The plugin watches public rollout telemetry through `UserPromptSubmit` and `PreToolUse` hooks. It injects one advisory when the latest request reaches 80,000 input tokens and one urgent advisory at 120,000. These are configurable candidate thresholds derived from local completed-run telemetry, not universal optima. Context pressure never bypasses the verified phase-boundary gate.

## Package layout

- `plugins/autonomous-phase-rollover/skills/`: harness-neutral phase gate, checkpoint, and ownership protocol
- `plugins/autonomous-phase-rollover/hooks/`: Codex lifecycle activation and dispatch
- `references/adapter-contract.md`: contract for Claude, Pi, Herdr, and other harness adapters
- `marketplace.json`: repository marketplace entry

## Install in Codex

Copy this repository to a stable directory on the target machine, or clone it from Git. The target needs Python 3 and a Codex release that supports plugins, hooks, and `codex app-server`. Authentication and local approval settings are intentionally not copied.

```sh
git clone https://github.com/qduc/phase-rollover.git
cd phase-rollover
git checkout v0.1.0

codex plugin marketplace add "$(pwd)"
codex plugin add autonomous-phase-rollover@phase-rollover
```

Review and trust the bundled hook when Codex asks. Start a new task after installation so its SessionStart hook can register the session.

Hook commands normally execute from the version captured when a task starts. During local development, if a cachebuster reinstall removes that directory while a task is still running, the launcher falls forward to another installed version of this same plugin. New tasks should still be used to pick up changed skill instructions and behavior deterministically.

For repeatable team installs, publish an immutable Git tag, clone that tag on each machine, and register the checkout with the same two commands. The plugin uses the operating system's temporary directory for pending handoffs and stores durable state under `~/.codex/phase-rollover/`.

## Publish

Commit this directory to a Git repository and push it to a public or private host. Consumers can clone it and add the checked-out directory as a marketplace. Keep release tags immutable so hook code can be reviewed before upgrades.

## Safety model

The agent decides whether a reasoning phase is settled; the controller alone owns dispatch. A versioned continuation capsule records the objective, completion criterion, verified checkpoint, exact next action, lineage, working directory, model, and declared sandbox. The controller validates the capsule, persists and preassigns the child thread ID before starting work, requires the child to verify that assignment read-only, declines approval requests, and never blindly retries an uncertain started turn. Explicit interruption cancels a handoff only while child task execution is positively known not to have started.

The plugin prevents duplicate task execution through ownership claims. No exposed API guarantees that an empty duplicate thread object can never be created if the process crashes between remote creation and durable acknowledgement.

Context growth raises input cost roughly linearly per request; when retained context grows across many requests, cumulative replay cost can grow roughly quadratically. It is not literally exponential. The advisory aims to leave enough room to finish and verify the current phase while avoiding a long tail of repeated 80K–170K requests. See [context-advisory.md](plugins/autonomous-phase-rollover/skills/autonomous-phase-rollover/references/context-advisory.md) for the measured sample, assumptions, configuration, and A/B validation plan.

This is not native session migration. The successor starts with fresh instructions, the checkpoint, and the same filesystem working directory. It does not inherit live processes, tool handles, browser state, private in-thread history, or other session-owned resources. The public SessionStart hook does not expose the effective source sandbox, so the preparing agent must declare its current stock `read-only` or `workspace-write` mode. Custom writable roots, network overrides, restricted-read rules, named permission profiles, unsupported modes, or unknown policy block rollover because the public lifecycle API cannot prove an equivalent child policy. Legacy version-1 requests are downgraded to `read-only`.

A completed child turn closes one generation but does not prove that the original objective is complete. The successor must either verify the completion criterion or prepare the next generation at another safe phase boundary.

## Other harnesses

Implement the adapter contract using the harness's public fresh-session lifecycle. Peer channels may transport notifications, but the ledger remains the authority. Do not use history-preserving forks as a rollover substitute.
