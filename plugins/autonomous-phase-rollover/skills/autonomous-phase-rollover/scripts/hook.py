#!/usr/bin/env python3
from __future__ import annotations

import fcntl
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
DEFAULT_ADVISORY_TOKENS = 80_000
DEFAULT_URGENT_TOKENS = 120_000
DEFAULT_SCAN_BYTES = 8 * 1024 * 1024
ADVISORY_EVENTS = {"PreToolUse", "UserPromptSubmit"}


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


def positive_env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def advisory_thresholds() -> tuple[int, int]:
    advisory = positive_env_int(
        "PHASE_ROLLOVER_ADVISORY_TOKENS", DEFAULT_ADVISORY_TOKENS
    )
    urgent = positive_env_int("PHASE_ROLLOVER_URGENT_TOKENS", DEFAULT_URGENT_TOKENS)
    if urgent <= advisory:
        urgent = max(advisory + 1, advisory * 3 // 2)
    return advisory, urgent


def latest_context_usage(transcript_path: object) -> tuple[int, int] | None:
    if not isinstance(transcript_path, str) or not transcript_path:
        return None
    path = pathlib.Path(transcript_path)
    try:
        size = path.stat().st_size
        scan_bytes = positive_env_int(
            "PHASE_ROLLOVER_TRANSCRIPT_SCAN_BYTES", DEFAULT_SCAN_BYTES
        )
        with path.open("rb") as handle:
            handle.seek(max(0, size - scan_bytes))
            tail = handle.read()
    except OSError:
        return None
    for raw_line in reversed(tail.splitlines()):
        try:
            record = json.loads(raw_line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        payload = record.get("payload") or {}
        if record.get("type") == "token_usage_record":
            usage = payload.get("usage") or {}
        elif record.get("type") == "event_msg" and payload.get("type") == "token_count":
            usage = (payload.get("info") or {}).get("last_token_usage") or {}
        else:
            continue
        input_tokens = usage.get("input_tokens")
        cached_tokens = usage.get("cached_input_tokens", 0)
        if (
            isinstance(input_tokens, int)
            and not isinstance(input_tokens, bool)
            and input_tokens >= 0
        ):
            if not isinstance(cached_tokens, int) or isinstance(cached_tokens, bool):
                cached_tokens = 0
            return input_tokens, max(0, cached_tokens)
    return None


def context_advisory(event: dict) -> str | None:
    if event.get("hook_event_name") not in ADVISORY_EVENTS:
        return None
    session_id = event.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return None
    advisory, urgent = advisory_thresholds()
    state_path = DATA_ROOT / "advisories" / f"{session_id}.json"
    state_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_fd = os.open(f"{state_path}.lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(lock_fd, "a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        usage = latest_context_usage(event.get("transcript_path"))
        if usage is None:
            return None
        input_tokens, cached_tokens = usage
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            state = {}
        prior_level = state.get("level", 0)
        if not isinstance(prior_level, int):
            prior_level = 0

        if input_tokens < advisory * 3 // 4:
            if prior_level:
                atomic_write(
                    state_path,
                    {
                        "session_id": session_id,
                        "level": 0,
                        "input_tokens": input_tokens,
                        "updated_at": time.time(),
                    },
                )
            return None

        level = 2 if input_tokens >= urgent else 1 if input_tokens >= advisory else 0
        if level <= prior_level:
            return None
        atomic_write(
            state_path,
            {
                "session_id": session_id,
                "level": level,
                "input_tokens": input_tokens,
                "cached_input_tokens": cached_tokens,
                "updated_at": time.time(),
            },
        )
    observed = f"{input_tokens:,} input tokens"
    if cached_tokens:
        observed += f" ({cached_tokens:,} cached)"
    if level == 2:
        return (
            f"Urgent phase-rollover advisory: the last model request used {observed}, above "
            f"the {urgent:,}-token urgent threshold. Stop expanding scope and reach the nearest "
            "verified phase boundary, then prepare a rollover. Do not interrupt an unsafe phase, "
            "abandon live resources, or bypass the skill's safety gates solely to reduce context."
        )
    return (
        f"Phase-rollover advisory: the last model request used {observed}, above the "
        f"{advisory:,}-token advisory threshold. Prefer rolling over at the next verified phase "
        "boundary if meaningful work remains; context pressure is advisory and never overrides "
        "the skill's safety gates."
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

    if name in ADVISORY_EVENTS:
        advisory = context_advisory(event)
        if advisory:
            print(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": name,
                            "additionalContext": advisory,
                        }
                    }
                )
            )
        return 0

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
