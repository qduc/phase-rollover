#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import pathlib
import queue
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from contextlib import contextmanager
from typing import Any

DATA_ROOT = pathlib.Path(
    os.environ.get(
        "PHASE_ROLLOVER_DATA_ROOT", pathlib.Path.home() / ".codex" / "phase-rollover"
    )
)
CHAINS = DATA_ROOT / "chains"
LOGS = DATA_ROOT / "logs"
CODEX_BIN = os.environ.get("CODEX_ROLLOVER_CODEX_BIN", "codex")
SUPPORTED_REQUEST_VERSIONS = {1, 2}


def atomic_write(path: pathlib.Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def ledger_path(chain_id: str, generation: int) -> pathlib.Path:
    safe = "".join(c for c in chain_id if c.isalnum() or c in "-_")
    if not safe or safe != chain_id:
        raise ValueError("invalid chain id")
    return CHAINS / f"{safe}-{generation}.json"


@contextmanager
def locked(path: pathlib.Path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(f"{path}.lock", "a+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


class AppServer:
    def __init__(self) -> None:
        self.proc = subprocess.Popen(
            [CODEX_BIN, "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.next_id = 1
        self.stdout_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.stderr_tail: deque[str] = deque(maxlen=200)
        self.stdout_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self.stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self.stdout_thread.start()
        self.stderr_thread.start()

    def _read_stdout(self) -> None:
        assert self.proc.stdout is not None
        try:
            for line in self.proc.stdout:
                self.stdout_queue.put(("line", line))
        except Exception as exc:  # noqa: BLE001 - propagate reader failures to read().
            self.stdout_queue.put(("error", exc))
        finally:
            self.stdout_queue.put(("eof", None))

    def _read_stderr(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            self.stderr_tail.append(line)

    def send(self, message: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        self.proc.stdin.flush()

    def read(self, timeout: float = 60.0) -> dict[str, Any]:
        try:
            kind, payload = self.stdout_queue.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError("timed out waiting for app-server")
        if kind == "error":
            raise RuntimeError(f"app-server stdout reader failed: {payload}")
        if kind == "eof":
            stderr = "".join(self.stderr_tail)
            raise RuntimeError(f"app-server exited: {stderr[-2000:]}")
        return json.loads(payload)

    def handle_server_request(self, message: dict[str, Any]) -> bool:
        if "id" not in message or "method" not in message:
            return False
        method = message["method"]
        if "requestApproval" in method:
            self.send({"id": message["id"], "result": {"decision": "decline"}})
        else:
            self.send(
                {
                    "id": message["id"],
                    "error": {
                        "code": -32601,
                        "message": "Unattended rollover cannot answer this request",
                    },
                }
            )
        return True

    def rpc(
        self, method: str, params: dict[str, Any] | None = None, timeout: float = 60.0
    ) -> dict[str, Any]:
        request_id = self.next_id
        self.next_id += 1
        message: dict[str, Any] = {"method": method, "id": request_id}
        if params is not None:
            message["params"] = params
        self.send(message)
        deadline = time.monotonic() + timeout
        while True:
            reply = self.read(max(0.1, deadline - time.monotonic()))
            if self.handle_server_request(reply):
                continue
            if reply.get("id") == request_id:
                if "error" in reply:
                    raise RuntimeError(f"{method} failed: {reply['error']}")
                return reply.get("result", {})

    def initialize(self) -> None:
        self.rpc(
            "initialize",
            {
                "clientInfo": {
                    "name": "phase_rollover",
                    "title": "Phase Rollover",
                    "version": "1.0.0",
                }
            },
        )
        self.send({"method": "initialized", "params": {}})

    def close(self) -> None:
        if self.proc.stdin is not None:
            self.proc.stdin.close()
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        self.stdout_thread.join(timeout=1)
        self.stderr_thread.join(timeout=1)
        if self.proc.stdout is not None:
            self.proc.stdout.close()
        if self.proc.stderr is not None:
            self.proc.stderr.close()


def prompt_for(request: dict[str, Any]) -> str:
    next_generation = request["generation"] + 1
    return f"""Continue this objective autonomously from a verified phase checkpoint.

Before any task action, run the ownership check from the autonomous-phase-rollover skill with:
chain_id={request["chain_id"]}
generation={request["generation"]}
Use the session ID supplied by the SessionStart hook. Stop without mutations if ownership fails.

Objective: {request["objective"]}
Completion criterion: {request.get("completion_criterion", "Complete and verify the stated objective.")}
Next action: {request["next_action"]}

<checkpoint>
{request["checkpoint"]}
</checkpoint>

Inspect the current workspace before editing. Do not repeat completed investigation. Continue until the objective is verified complete. At the next safe phase boundary, use the same chain_id and generation={next_generation} when preparing a rollover. Routine rollover is already authorized; ask the user only for a genuine decision, permission, or scope expansion.
"""


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def latest_turn(thread: dict[str, Any]) -> dict[str, Any] | None:
    turns = thread.get("turns") or []
    return turns[-1] if turns else None


def update_ledger(path: pathlib.Path, updates: dict[str, Any]) -> dict[str, Any]:
    """Merge updates into the latest ledger without erasing concurrent claims."""
    with locked(path):
        ledger = json.loads(path.read_text(encoding="utf-8"))
        ledger.update(updates)
        ledger["updated_at"] = time.time()
        atomic_write(path, ledger)
        return ledger


def validate_request(request: dict[str, Any]) -> None:
    version = request.get("version", 1)
    if version not in SUPPORTED_REQUEST_VERSIONS:
        raise ValueError(f"unsupported request version: {version}")
    for key in (
        "chain_id",
        "source_session_id",
        "cwd",
        "objective",
        "next_action",
        "checkpoint",
    ):
        value = request.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key} must be a non-empty string")
    generation = request.get("generation")
    if (
        not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation < 0
    ):
        raise ValueError("generation must be a non-negative integer")
    cwd = pathlib.Path(request["cwd"])
    if not cwd.is_absolute() or not cwd.is_dir():
        raise ValueError("cwd must be an existing absolute directory")
    if version >= 2:
        if request.get("boundary_ready") is not True:
            raise ValueError("phase boundary is not verified")
        completion_criterion = request.get("completion_criterion")
        if (
            not isinstance(completion_criterion, str)
            or not completion_criterion.strip()
        ):
            raise ValueError("completion_criterion must be a non-empty string")
        if request.get("sandbox") not in {"read-only", "workspace-write"}:
            raise ValueError("sandbox must be read-only or workspace-write")
        checkpoint = request["checkpoint"].encode("utf-8")
        if request.get("checkpoint_bytes") != len(checkpoint):
            raise ValueError("checkpoint byte count does not match content")
        if request.get("checkpoint_sha256") != hashlib.sha256(checkpoint).hexdigest():
            raise ValueError("checkpoint digest does not match content")


def stage_request(request: dict[str, Any]) -> dict[str, Any]:
    """Durably create the generation ledger before background dispatch."""
    validate_request(request)
    path = ledger_path(request["chain_id"], int(request["generation"]))
    with locked(path):
        if path.exists():
            ledger = json.loads(path.read_text(encoding="utf-8"))
            identity = ("chain_id", "generation", "source_session_id", "cwd")
            if any(ledger.get(key) != request.get(key) for key in identity):
                raise RuntimeError("existing generation ledger does not match request")
            return ledger
        ledger = dict(request)
        ledger.update(
            {
                "state": "PREPARED",
                "controller_pid": None,
                "created_at": time.time(),
                "updated_at": time.time(),
            }
        )
        atomic_write(path, ledger)
        return ledger


def cancel_source(source_session_id: str) -> int:
    """Cancel generations whose child task is positively known not to have started."""
    cancelled = 0
    CHAINS.mkdir(mode=0o700, parents=True, exist_ok=True)
    for path in sorted(CHAINS.glob("*.json")):
        with locked(path):
            ledger = json.loads(path.read_text(encoding="utf-8"))
            if ledger.get("source_session_id") != source_session_id:
                continue
            safely_pending = ledger.get("state") == "PREPARED" or (
                ledger.get("state") == "CHILD_CREATED"
                and ledger.get("turn_id") is None
                and ledger.get("turn_start_state") in {None, "NOT_STARTED"}
            )
            if not safely_pending:
                continue
            ledger.update(
                {
                    "state": "CANCELLED",
                    "cancelled_at": time.time(),
                    "updated_at": time.time(),
                }
            )
            atomic_write(path, ledger)
            cancelled += 1
    return cancelled


def run_request(request: dict[str, Any]) -> None:
    path = ledger_path(request["chain_id"], int(request["generation"]))
    stage_request(request)
    with locked(path):
        ledger = json.loads(path.read_text(encoding="utf-8"))
        if ledger.get("state") in {"COMPLETED", "CANCELLED"}:
            return
        if (
            pid_alive(ledger.get("controller_pid"))
            and ledger.get("controller_pid") != os.getpid()
        ):
            return
        ledger["controller_pid"] = os.getpid()
        ledger["updated_at"] = time.time()
        atomic_write(path, ledger)

    server = AppServer()
    try:
        server.initialize()
        thread_id = ledger.get("child_thread_id")
        if thread_id:
            read = server.rpc(
                "thread/read", {"threadId": thread_id, "includeTurns": True}
            )
            turn = latest_turn(read.get("thread", {}))
            if turn and turn.get("status") == "completed":
                update_ledger(
                    path,
                    {
                        "state": "COMPLETED",
                        "turn_status": "completed",
                        "controller_pid": None,
                    },
                )
                return
            if turn:
                update_ledger(
                    path,
                    {
                        "state": "START_UNCERTAIN",
                        "turn_status": turn.get("status", "unknown"),
                        "error": "existing child turn requires reconciliation; dispatch was not retried",
                        "controller_pid": None,
                    },
                )
                return
            may_start_turn = (
                ledger.get("state") == "CHILD_CREATED"
                and ledger.get("turn_id") is None
                and ledger.get("turn_start_state") in {None, "NOT_STARTED"}
            )
            if not may_start_turn:
                update_ledger(
                    path,
                    {
                        "state": "START_UNCERTAIN",
                        "turn_start_state": "UNCERTAIN",
                        "error": "child turn state is not positively unstarted; dispatch was not retried",
                        "controller_pid": None,
                    },
                )
                return
            server.rpc("thread/resume", {"threadId": thread_id})
        else:
            params: dict[str, Any] = {
                "cwd": ledger["cwd"],
                "approvalPolicy": "never",
                "sandbox": ledger.get("sandbox", "read-only"),
                "serviceName": "phase_rollover",
            }
            if ledger.get("model"):
                params["model"] = ledger["model"]
            result = server.rpc("thread/start", params)
            thread_id = result["thread"]["id"]
            with locked(path):
                ledger = json.loads(path.read_text(encoding="utf-8"))
                if ledger.get("state") == "CANCELLED":
                    ledger.update(
                        {
                            "child_thread_id": thread_id,
                            "empty_child_after_cancel": True,
                            "controller_pid": None,
                            "updated_at": time.time(),
                        }
                    )
                    atomic_write(path, ledger)
                    return
                ledger.update(
                    {
                        "state": "CHILD_CREATED",
                        "child_thread_id": thread_id,
                        "owner_claimed_by": thread_id,
                        "owner_claimed_at": time.time(),
                        "turn_start_state": "NOT_STARTED",
                        "updated_at": time.time(),
                    }
                )
                atomic_write(path, ledger)

        turn_params: dict[str, Any] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": prompt_for(ledger)}],
            "cwd": ledger["cwd"],
            "approvalPolicy": "never",
        }
        if ledger.get("model"):
            turn_params["model"] = ledger["model"]
        if ledger.get("effort"):
            turn_params["effort"] = ledger["effort"]
        with locked(path):
            ledger = json.loads(path.read_text(encoding="utf-8"))
            if ledger.get("state") == "CANCELLED":
                ledger["controller_pid"] = None
                ledger["updated_at"] = time.time()
                atomic_write(path, ledger)
                return
            if not (
                ledger.get("state") == "CHILD_CREATED"
                and ledger.get("turn_id") is None
                and ledger.get("turn_start_state") in {None, "NOT_STARTED"}
            ):
                ledger.update(
                    {
                        "state": "START_UNCERTAIN",
                        "turn_start_state": "UNCERTAIN",
                        "error": "turn was not positively unstarted at dispatch boundary",
                        "controller_pid": None,
                        "updated_at": time.time(),
                    }
                )
                atomic_write(path, ledger)
                return
            # The trusted controller assigns ownership before task execution. The
            # sandboxed successor only performs a read-only identity check.
            ledger["owner_claimed_by"] = thread_id
            ledger.setdefault("owner_claimed_at", time.time())
            ledger["turn_start_state"] = "STARTING"
            ledger["updated_at"] = time.time()
            atomic_write(path, ledger)
        try:
            started = server.rpc("turn/start", turn_params)
        except Exception:
            update_ledger(
                path,
                {
                    "state": "START_UNCERTAIN",
                    "turn_start_state": "UNCERTAIN",
                    "controller_pid": None,
                },
            )
            raise
        ledger = update_ledger(
            path,
            {
                "state": "ACTIVE",
                "turn_start_state": "STARTED",
                "turn_id": started["turn"]["id"],
            },
        )

        final_messages: list[str] = []
        while True:
            message = server.read(timeout=3600)
            if server.handle_server_request(message):
                continue
            if message.get("method") == "item/completed":
                item = message.get("params", {}).get("item", {})
                if item.get("type") == "agentMessage" and item.get("text"):
                    final_messages.append(item["text"])
            if message.get("method") == "turn/completed":
                turn = message.get("params", {}).get("turn", {})
                status = turn.get("status", "unknown")
                updates = {
                    "state": "COMPLETED" if status == "completed" else "FAILED",
                    "turn_status": status,
                    "objective_status": "UNVERIFIED",
                    "final_message": final_messages[-1] if final_messages else None,
                    "controller_pid": None,
                }
                if turn.get("error"):
                    updates["error"] = turn["error"]
                update_ledger(path, updates)
                return
    except Exception as exc:
        update_ledger(
            path,
            {
                "last_controller_error": str(exc),
                "controller_pid": None,
            },
        )
        raise
    finally:
        server.close()


def recover() -> int:
    CHAINS.mkdir(mode=0o700, parents=True, exist_ok=True)
    for path in sorted(CHAINS.glob("*.json")):
        ledger = json.loads(path.read_text(encoding="utf-8"))
        if ledger.get("state") in {
            "PREPARED",
            "CHILD_CREATED",
            "ACTIVE",
        } and not pid_alive(ledger.get("controller_pid")):
            try:
                run_request(ledger)
            except Exception as exc:  # noqa: BLE001 - recover other ledgers independently.
                print(f"recovery failed for {path}: {exc}", file=sys.stderr)
                continue
    return 0


def claim(chain_id: str, generation: int, session_id: str) -> int:
    path = ledger_path(chain_id, generation)
    if not path.exists():
        print("ownership denied: ledger missing", file=sys.stderr)
        return 2
    try:
        ledger = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        print("ownership denied: ledger unreadable", file=sys.stderr)
        return 2
    claimable_states = {"CHILD_CREATED", "ACTIVE", "START_UNCERTAIN"}
    if (
        ledger.get("state") not in claimable_states
        or ledger.get("child_thread_id") != session_id
        or ledger.get("owner_claimed_by") != session_id
    ):
        print(
            "ownership denied: session does not own this generation",
            file=sys.stderr,
        )
        return 3
    print(json.dumps({"owned": True, "chain_id": chain_id, "generation": generation}))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Crash-safe Codex phase rollover controller."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--request", required=True)
    stage = sub.add_parser("stage")
    stage.add_argument("--request", required=True)
    sub.add_parser("recover")
    cancel = sub.add_parser("cancel")
    cancel.add_argument("--source-session-id", required=True)
    own = sub.add_parser("claim")
    own.add_argument("--chain-id", required=True)
    own.add_argument("--generation", required=True, type=int)
    own.add_argument("--session-id", required=True)
    args = parser.parse_args()
    if args.command == "recover":
        return recover()
    if args.command == "cancel":
        cancel_source(args.source_session_id)
        return 0
    if args.command == "claim":
        return claim(args.chain_id, args.generation, args.session_id)
    request = json.loads(pathlib.Path(args.request).read_text(encoding="utf-8"))
    if args.command == "stage":
        stage_request(request)
        return 0
    run_request(request)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
