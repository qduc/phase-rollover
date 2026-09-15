#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time

DEFAULT_REQUEST_ROOT = pathlib.Path(tempfile.gettempdir()) / "codex-phase-rollover"
REQUEST_ROOT = pathlib.Path(
    os.environ.get("PHASE_ROLLOVER_REQUEST_ROOT", str(DEFAULT_REQUEST_ROOT))
)
DATA_ROOT = pathlib.Path(
    os.environ.get(
        "PHASE_ROLLOVER_DATA_ROOT", pathlib.Path.home() / ".codex" / "phase-rollover"
    )
)
SKILL = pathlib.Path(__file__).resolve().parents[1]
CONTROLLER = SKILL / "scripts" / "controller.py"
SKILL_DOC = SKILL / "SKILL.md"


def atomic_write(path: pathlib.Path, payload: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def launch(args: list[str], log_name: str) -> None:
    logs = DATA_ROOT / "logs"
    logs.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(logs / log_name, "a", encoding="utf-8") as log:
        subprocess.Popen(
            [sys.executable, str(CONTROLLER), *args],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            close_fds=True,
        )


def main() -> int:
    event = json.load(sys.stdin)
    name = event.get("hook_event_name")
    session_id = event.get("session_id")
    if not session_id:
        return 0
    REQUEST_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    session_path = DATA_ROOT / "sessions" / f"{session_id}.json"
    record = {
        "session_id": session_id,
        "cwd": event.get("cwd"),
        "model": event.get("model"),
        "permission_mode": event.get("permission_mode"),
        "event": name,
        "updated_at": time.time(),
    }
    atomic_write(session_path, record)

    request = REQUEST_ROOT / f"{session_id}.json"
    cancel = REQUEST_ROOT / f"{session_id}.cancelled"
    if name == "SessionStart":
        launch(["recover"], f"recovery-{int(time.time())}.log")
        context = f"Autonomous phase rollover is armed. For substantive multi-phase or unattended work, read {SKILL_DOC}. Session ID: {session_id}. Rollover request path: {request}."
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "SessionStart",
                        "additionalContext": context,
                    }
                }
            )
        )
    elif name == "Interrupt":
        atomic_write(cancel, {"session_id": session_id, "cancelled_at": time.time()})
        subprocess.run(
            [
                sys.executable,
                str(CONTROLLER),
                "cancel",
                "--source-session-id",
                session_id,
            ],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if request.exists():
            os.replace(request, request.with_suffix(".cancelled.json"))
    elif name == "Stop" and request.exists() and not cancel.exists():
        claimed = REQUEST_ROOT / f"{session_id}.{int(time.time() * 1000)}.claimed.json"
        try:
            payload = json.loads(request.read_text(encoding="utf-8"))
            if (
                payload.get("source_session_id") != session_id
                or pathlib.Path(payload.get("cwd", "")).resolve()
                != pathlib.Path(event.get("cwd", "")).resolve()
            ):
                os.replace(request, request.with_suffix(".rejected.json"))
                return 0
        except (AttributeError, json.JSONDecodeError, OSError, TypeError, ValueError):
            if request.exists():
                os.replace(request, request.with_suffix(".rejected.json"))
            return 0
        staged = subprocess.run(
            [sys.executable, str(CONTROLLER), "stage", "--request", str(request)],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if staged.returncode != 0:
            if request.exists():
                os.replace(request, request.with_suffix(".rejected.json"))
            return 0
        if cancel.exists():
            subprocess.run(
                [
                    sys.executable,
                    str(CONTROLLER),
                    "cancel",
                    "--source-session-id",
                    session_id,
                ],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if request.exists():
                os.replace(request, request.with_suffix(".cancelled.json"))
            return 0
        try:
            os.replace(request, claimed)
        except FileNotFoundError:
            return 0
        launch(
            ["run", "--request", str(claimed)],
            f"{payload['chain_id']}-{payload['generation']}.log",
        )
        print(
            json.dumps(
                {
                    "continue": True,
                    "systemMessage": "A fresh phase continuation was dispatched by the rollover controller.",
                }
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
