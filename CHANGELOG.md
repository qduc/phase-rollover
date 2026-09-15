# Changelog

## Unreleased

- Prevent competing controllers from dispatching the same prepared generation.
- Preassign the recorded child as owner before its first turn and make successor verification read-only.
- Preserve ownership metadata across controller ledger updates.
- Drain app-server output through a reader queue so buffered JSON messages cannot cause false timeouts.
- Quarantine uncertain `turn/start` delivery instead of risking duplicate task execution.
- Stage the generation ledger before background launch so Stop-hook crashes remain recoverable.
- Make Interrupt cancellation race safely with child creation and first-turn dispatch.
- Add validated version-2 continuation capsules with completion criteria, checkpoint digests, and explicit sandbox declarations.
- Default legacy requests to read-only and distinguish turn completion from objective completion.
- Let hooks from already-running tasks fall forward to the current installed cache version after a local plugin update.
- Inject configurable context-pressure advisories at 80K and 120K input tokens without bypassing safe phase gates.

## 0.1.0 - 2026-09-15

- Add the harness-neutral checkpoint and ownership protocol.
- Add the Codex hooks and app-server lifecycle adapter.
- Add atomic dispatch, successor ownership claims, interruption cancellation, and recovery.
- Document the adapter contract for other single-harness implementations.
