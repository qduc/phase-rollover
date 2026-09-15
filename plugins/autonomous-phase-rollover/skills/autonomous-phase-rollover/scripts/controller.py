#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import json
import os
import pathlib
import select
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from typing import Any

DATA_ROOT = pathlib.Path(os.environ.get("PHASE_ROLLOVER_DATA_ROOT", pathlib.Path.home() / ".codex" / "phase-rollover"))
CHAINS = DATA_ROOT / "chains"
LOGS = DATA_ROOT / "logs"
CODEX_BIN = os.environ.get("CODEX_ROLLOVER_CODEX_BIN", "codex")


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

    def send(self, message: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        self.proc.stdin.flush()

    def read(self, timeout: float = 60.0) -> dict[str, Any]:
        assert self.proc.stdout is not None
        ready, _, _ = select.select([self.proc.stdout], [], [], timeout)
        if not ready:
            raise TimeoutError("timed out waiting for app-server")
        line = self.proc.stdout.readline()
        if not line:
            stderr = self.proc.stderr.read() if self.proc.stderr else ""
            raise RuntimeError(f"app-server exited: {stderr[-2000:]}")
        return json.loads(line)

    def handle_server_request(self, message: dict[str, Any]) -> bool:
        if "id" not in message or "method" not in message:
            return False
        method = message["method"]
        if "requestApproval" in method:
            self.send({"id": message["id"], "result": {"decision": "decline"}})
        else:
            self.send({"id": message["id"], "error": {"code": -32601, "message": "Unattended rollover cannot answer this request"}})
        return True

    def rpc(self, method: str, params: dict[str, Any] | None = None, timeout: float = 60.0) -> dict[str, Any]:
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
        self.rpc("initialize", {"clientInfo": {"name": "phase_rollover", "title": "Phase Rollover", "version": "1.0.0"}})
        self.send({"method": "initialized", "params": {}})

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def prompt_for(request: dict[str, Any]) -> str:
    next_generation = request["generation"] + 1
    return f"""Continue this objective autonomously from a verified phase checkpoint.

Before any task action, run the ownership check from the autonomous-phase-rollover skill with:
chain_id={request['chain_id']}
generation={request['generation']}
Use the session ID supplied by the SessionStart hook. Stop without mutations if ownership fails.

Objective: {request['objective']}
Next action: {request['next_action']}

<checkpoint>
{request['checkpoint']}
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


def run_request(request: dict[str, Any]) -> None:
    path = ledger_path(request["chain_id"], int(request["generation"]))
    with locked(path):
        if path.exists():
            ledger = json.loads(path.read_text(encoding="utf-8"))
        else:
            ledger = dict(request)
            ledger.update({"state": "PREPARED", "created_at": time.time()})
        if ledger.get("state") in {"COMPLETED", "CANCELLED"}:
            return
        if ledger.get("state") == "ACTIVE" and pid_alive(ledger.get("controller_pid")) and ledger.get("controller_pid") != os.getpid():
            return
        ledger["controller_pid"] = os.getpid()
        ledger["updated_at"] = time.time()
        atomic_write(path, ledger)

    server = AppServer()
    try:
        server.initialize()
        thread_id = ledger.get("child_thread_id")
        if thread_id:
            read = server.rpc("thread/read", {"threadId": thread_id, "includeTurns": True})
            turn = latest_turn(read.get("thread", {}))
            if turn and turn.get("status") == "completed":
                with locked(path):
                    ledger.update({"state": "COMPLETED", "turn_status": "completed", "updated_at": time.time()})
                    atomic_write(path, ledger)
                return
            if turn:
                with locked(path):
                    ledger.update({"state": "FAILED", "turn_status": turn.get("status", "unknown"), "error": "uncertain prior turn was not retried", "updated_at": time.time()})
                    atomic_write(path, ledger)
                return
            server.rpc("thread/resume", {"threadId": thread_id})
        else:
            params: dict[str, Any] = {"cwd": ledger["cwd"], "approvalPolicy": "never", "sandbox": "workspaceWrite", "serviceName": "phase_rollover"}
            if ledger.get("model"):
                params["model"] = ledger["model"]
            result = server.rpc("thread/start", params)
            thread_id = result["thread"]["id"]
            with locked(path):
                ledger.update({"state": "CHILD_CREATED", "child_thread_id": thread_id, "updated_at": time.time()})
                atomic_write(path, ledger)

        turn_params: dict[str, Any] = {"threadId": thread_id, "input": [{"type": "text", "text": prompt_for(ledger)}], "cwd": ledger["cwd"], "approvalPolicy": "never"}
        if ledger.get("model"):
            turn_params["model"] = ledger["model"]
        if ledger.get("effort"):
            turn_params["effort"] = ledger["effort"]
        started = server.rpc("turn/start", turn_params)
        with locked(path):
            ledger.update({"state": "ACTIVE", "turn_id": started["turn"]["id"], "updated_at": time.time()})
            atomic_write(path, ledger)

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
                with locked(path):
                    ledger.update({"state": "COMPLETED" if status == "completed" else "FAILED", "turn_status": status, "final_message": final_messages[-1] if final_messages else None, "controller_pid": None, "updated_at": time.time()})
                    if turn.get("error"):
                        ledger["error"] = turn["error"]
                    atomic_write(path, ledger)
                return
    except Exception as exc:
        with locked(path):
            ledger.update({"state": ledger.get("state", "PREPARED"), "last_controller_error": str(exc), "controller_pid": None, "updated_at": time.time()})
            atomic_write(path, ledger)
        raise
    finally:
        server.close()


def recover() -> int:
    CHAINS.mkdir(mode=0o700, parents=True, exist_ok=True)
    for path in sorted(CHAINS.glob("*.json")):
        ledger = json.loads(path.read_text(encoding="utf-8"))
        if ledger.get("state") in {"PREPARED", "CHILD_CREATED", "ACTIVE"} and not pid_alive(ledger.get("controller_pid")):
            try:
                run_request(ledger)
            except Exception:
                continue
    return 0


def claim(chain_id: str, generation: int, session_id: str) -> int:
    path = ledger_path(chain_id, generation)
    if not path.exists():
        print("ownership denied: ledger missing", file=sys.stderr)
        return 2
    with locked(path):
        ledger = json.loads(path.read_text(encoding="utf-8"))
        if ledger.get("state") != "ACTIVE" or ledger.get("child_thread_id") != session_id:
            print("ownership denied: session does not own this generation", file=sys.stderr)
            return 3
        if ledger.get("owner_claimed_by") not in {None, session_id}:
            print("ownership denied: generation already claimed", file=sys.stderr)
            return 4
        ledger.update({"owner_claimed_by": session_id, "owner_claimed_at": time.time()})
        atomic_write(path, ledger)
    print(json.dumps({"owned": True, "chain_id": chain_id, "generation": generation}))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Crash-safe Codex phase rollover controller.")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--request", required=True)
    sub.add_parser("recover")
    own = sub.add_parser("claim")
    own.add_argument("--chain-id", required=True)
    own.add_argument("--generation", required=True, type=int)
    own.add_argument("--session-id", required=True)
    args = parser.parse_args()
    if args.command == "recover":
        return recover()
    if args.command == "claim":
        return claim(args.chain_id, args.generation, args.session_id)
    request = json.loads(pathlib.Path(args.request).read_text(encoding="utf-8"))
    run_request(request)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
