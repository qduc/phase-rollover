#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import tempfile
import uuid

DEFAULT_REQUEST_ROOT = pathlib.Path(tempfile.gettempdir()) / "codex-phase-rollover"
REQUEST_ROOT = pathlib.Path(
    os.environ.get("PHASE_ROLLOVER_REQUEST_ROOT", str(DEFAULT_REQUEST_ROOT))
)


def atomic_write(path: pathlib.Path, payload: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"), ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prepare one safe Codex phase rollover request."
    )
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--checkpoint-file", required=True)
    parser.add_argument("--objective", required=True)
    parser.add_argument("--completion-criterion", required=True)
    parser.add_argument("--next-action", required=True)
    parser.add_argument(
        "--sandbox", required=True, choices=("read-only", "workspace-write")
    )
    parser.add_argument("--phase-verified", required=True, action="store_true")
    parser.add_argument("--model")
    parser.add_argument("--effort")
    parser.add_argument("--chain-id")
    parser.add_argument("--generation", type=int, default=0)
    args = parser.parse_args()

    cwd = pathlib.Path(args.cwd).resolve()
    checkpoint_path = pathlib.Path(args.checkpoint_file).resolve()
    if not cwd.is_dir():
        parser.error(f"cwd is not a directory: {cwd}")
    if not checkpoint_path.is_file():
        parser.error(f"checkpoint does not exist: {checkpoint_path}")
    checkpoint = checkpoint_path.read_text(encoding="utf-8")
    if not checkpoint.strip():
        parser.error("checkpoint must not be empty")
    approximate_tokens = (len(checkpoint) + 3) // 4
    if approximate_tokens > 1500:
        parser.error(
            f"checkpoint is approximately {approximate_tokens} tokens; limit is 1500"
        )
    if args.generation < 0:
        parser.error("generation must be non-negative")
    objective = args.objective.strip()
    completion_criterion = args.completion_criterion.strip()
    next_action = args.next_action.strip()
    if not objective:
        parser.error("objective must not be empty")
    if not completion_criterion:
        parser.error("completion criterion must not be empty")
    if not next_action:
        parser.error("next action must not be empty")

    payload = {
        "version": 2,
        "state": "PREPARED",
        "boundary_ready": True,
        "source_session_id": args.session_id,
        "cwd": str(cwd),
        "checkpoint_path": str(checkpoint_path),
        "checkpoint": checkpoint,
        "checkpoint_sha256": hashlib.sha256(checkpoint.encode("utf-8")).hexdigest(),
        "checkpoint_bytes": len(checkpoint.encode("utf-8")),
        "objective": objective,
        "completion_criterion": completion_criterion,
        "next_action": next_action,
        "chain_id": args.chain_id or str(uuid.uuid4()),
        "generation": args.generation,
        "model": args.model,
        "effort": args.effort,
        "sandbox": args.sandbox,
    }
    request_path = REQUEST_ROOT / f"{args.session_id}.json"
    if request_path.exists():
        parser.error(f"a pending request already exists: {request_path}")
    atomic_write(request_path, payload)
    print(
        json.dumps(
            {
                "prepared": True,
                "request_path": str(request_path),
                "chain_id": payload["chain_id"],
                "generation": args.generation,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
