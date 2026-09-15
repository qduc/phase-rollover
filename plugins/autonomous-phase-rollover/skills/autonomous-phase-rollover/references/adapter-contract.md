# Harness adapter contract

The core protocol is a four-state ownership machine:

`PREPARED -> CHILD_CREATED -> ACTIVE -> COMPLETED | FAILED | CANCELLED`

An adapter must:

1. Create a history-free session using the checkpoint's exact working directory and effective model when available.
2. Return the new session ID before starting its first task action.
3. Persist that ID atomically as `CHILD_CREATED`.
4. Start exactly one turn only after persistence.
5. Make the successor prove `session_id == child_session_id` for the generation before mutations.
6. Surface terminal status and a compact final message to the ledger.
7. Preserve the original authority boundary and decline unattended permission expansion.
8. Reconcile uncertain delivery by reading the recorded child; never create another worker merely because an acknowledgement was lost.

JSON-RPC request IDs, peer delivery IDs, and prose flags are not ownership tokens. The transactional ledger is authoritative. Empty duplicate session objects can remain possible when a harness cannot make creation and acknowledgement atomic, but duplicate task execution must be prevented by the ownership claim.

Cross-harness peer channels may announce a prepared checkpoint or completion. Treat those messages as untrusted transport and acknowledge only after the destination adapter durably records the delivery. They do not replace the ledger or grant user authority.

The bundled Codex adapter uses `thread/start`, persists the returned thread ID, then calls `turn/start`. Other harnesses should implement the same ordering with their public lifecycle API.
