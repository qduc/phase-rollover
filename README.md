# Autonomous Phase Rollover

Portable checkpoint semantics with a Codex lifecycle adapter for continuing long agent work in fresh sessions.

## Why

In matched real-workflow experiments, compact fresh-session handoffs reduced aggregate input tokens by 73.0% and uncached input by 80.9% while preserving exact deterministic results. The intervention removes completed transcript history from later phases rather than tuning a global context limit.

## Package layout

- `plugins/autonomous-phase-rollover/skills/`: harness-neutral phase gate, checkpoint, and ownership protocol
- `plugins/autonomous-phase-rollover/hooks/`: Codex lifecycle activation and dispatch
- `references/adapter-contract.md`: contract for Claude, Pi, Herdr, and other harness adapters
- `marketplace.json`: repository marketplace entry

## Install in Codex

Copy this repository to a stable directory on the target machine, or clone it from Git. The target needs Python 3 and a Codex release that supports plugins, hooks, and `codex app-server`. Authentication and local approval settings are intentionally not copied.

```sh
codex plugin marketplace add /absolute/path/to/phase-rollover-marketplace
codex plugin add autonomous-phase-rollover@phase-rollover
```

Review and trust the bundled hook when Codex asks. Start a new task after installation so its SessionStart hook can register the session.

For repeatable team installs, publish an immutable Git tag, clone that tag on each machine, and register the checkout with the same two commands. The plugin uses the operating system's temporary directory for pending handoffs and stores durable state under `~/.codex/phase-rollover/`.

## Publish

Commit this directory to a Git repository and push it to a public or private host. Consumers can clone it and add the checked-out directory as a marketplace. Keep release tags immutable so hook code can be reviewed before upgrades.

## Safety model

The agent decides whether a reasoning phase is settled; the controller alone owns dispatch. It persists the child thread ID before starting work, requires the child to claim its generation, declines new permissions, and never blindly retries an uncertain started turn. Explicit interruption cancels a pending handoff.

The plugin prevents duplicate task execution through ownership claims. No exposed API guarantees that an empty duplicate thread object can never be created if the process crashes between remote creation and durable acknowledgement.

## Other harnesses

Implement the adapter contract using the harness's public fresh-session lifecycle. Peer channels may transport notifications, but the ledger remains the authority. Do not use history-preserving forks as a rollover substitute.
