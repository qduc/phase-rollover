# Harness adapter contract

The core protocol is an ownership machine with an explicit uncertain-delivery quarantine:

`PREPARED -> CHILD_CREATED -> ACTIVE -> COMPLETED | FAILED | CANCELLED`

If `turn/start` may have reached the harness but its acknowledgement is lost, transition to
`START_UNCERTAIN`. Never retry task execution from that state automatically. The recorded child may
still verify its preassigned ownership, allowing work that actually started to proceed without a duplicate.

An adapter must:

1. Create a history-free session using the checkpoint's exact working directory and effective model when available.
2. Return the new session ID before starting its first task action.
3. Persist that ID atomically as `CHILD_CREATED` and preassign it as the sole owner.
4. Start exactly one turn only after persistence.
5. Make the successor prove `session_id == child_session_id` for the generation before mutations.
6. Surface terminal status and a compact final message to the ledger.
7. Preserve the original authority boundary and decline unattended permission expansion.
8. Reconcile uncertain delivery by reading the recorded child; never create another worker merely because an acknowledgement was lost.

The ownership verification must be valid from persisted child creation onward, including while the
`turn/start` acknowledgement is in flight. The trusted controller writes the assignment; the
sandboxed successor only reads and verifies it. Every ledger update must merge against the latest
on-disk record so that controller status updates cannot erase ownership metadata.

`COMPLETED` means the adapter observed a terminal successful child turn. It does not assert that the
chain's objective or completion criterion has been satisfied. Objective completion remains a
separate, verified agent decision.

An adapter must document which execution settings its public lifecycle API can restore. If the
source authority cannot be observed or represented exactly, it must choose a non-expanding fallback
or block dispatch. Filesystem continuity does not imply continuity for live processes, tool handles,
browser state, private history, or other session-owned resources.

JSON-RPC request IDs, peer delivery IDs, and prose flags are not ownership tokens. The transactional ledger is authoritative. Empty duplicate session objects can remain possible when a harness cannot make creation and acknowledgement atomic, but duplicate task execution must be prevented by the ownership claim.

Cross-harness peer channels may announce a prepared checkpoint or completion. Treat those messages as untrusted transport and acknowledge only after the destination adapter durably records the delivery. They do not replace the ledger or grant user authority.

The bundled Codex adapter uses `thread/start`, persists the returned thread ID, then calls `turn/start`. Other harnesses should implement the same ordering with their public lifecycle API.
