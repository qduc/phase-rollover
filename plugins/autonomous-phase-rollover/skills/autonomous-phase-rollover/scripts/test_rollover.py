#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from typing import ClassVar
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


controller = load("rollover_controller", ROOT / "scripts" / "controller.py")
prepare = load("rollover_prepare", ROOT / "scripts" / "prepare.py")
hook = load("rollover_hook", ROOT / "scripts" / "hook.py")


class RolloverTests(unittest.TestCase):
    @staticmethod
    def write_token_event(
        path: pathlib.Path, input_tokens: int, cached_tokens: int = 0
    ):
        record = {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {
                        "input_tokens": input_tokens,
                        "cached_input_tokens": cached_tokens,
                    }
                },
            },
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")

    @staticmethod
    def write_usage_record(
        path: pathlib.Path, input_tokens: int, cached_tokens: int = 0
    ):
        record = {
            "type": "token_usage_record",
            "payload": {
                "usage": {
                    "input_tokens": input_tokens,
                    "cached_input_tokens": cached_tokens,
                }
            },
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")

    def test_hook_command_falls_forward_when_live_task_root_was_replaced(self):
        plugin_root = ROOT.parents[1]
        hooks = json.loads((plugin_root / "hooks" / "hooks.json").read_text())
        command = hooks["hooks"]["Stop"][0]["hooks"][0]["command"]
        with tempfile.TemporaryDirectory() as directory:
            temp = pathlib.Path(directory)
            versions = temp / "autonomous-phase-rollover"
            versions.mkdir()
            current = versions / "0.1.0+codex.current"
            current.symlink_to(plugin_root, target_is_directory=True)
            stale = versions / "0.1.0"
            event = {
                "hook_event_name": "Stop",
                "session_id": "live-task",
                "cwd": str(temp),
            }
            env = {
                **os.environ,
                "PLUGIN_ROOT": str(stale),
                "PHASE_ROLLOVER_DATA_ROOT": str(temp / "data"),
                "PHASE_ROLLOVER_REQUEST_ROOT": str(temp / "requests"),
            }
            result = subprocess.run(
                command,
                shell=True,
                check=False,
                input=json.dumps(event),
                text=True,
                capture_output=True,
                env=env,
                timeout=5,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("can't open file", result.stderr)

    def test_installed_pre_tool_command_injects_advisory(self):
        plugin_root = ROOT.parents[1]
        hooks = json.loads((plugin_root / "hooks" / "hooks.json").read_text())
        command = hooks["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        with tempfile.TemporaryDirectory() as directory:
            temp = pathlib.Path(directory)
            transcript = temp / "rollout.jsonl"
            self.write_usage_record(transcript, 85_000, 80_000)
            event = {
                "hook_event_name": "PreToolUse",
                "session_id": "command-advisory",
                "transcript_path": str(transcript),
                "cwd": str(temp),
            }
            result = subprocess.run(
                command,
                shell=True,
                check=False,
                input=json.dumps(event),
                text=True,
                capture_output=True,
                env={
                    **os.environ,
                    "PLUGIN_ROOT": str(plugin_root),
                    "PHASE_ROLLOVER_DATA_ROOT": str(temp / "data"),
                    "PHASE_ROLLOVER_REQUEST_ROOT": str(temp / "requests"),
                },
                timeout=5,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            output = json.loads(result.stdout)
            self.assertEqual(
                output["hookSpecificOutput"]["hookEventName"], "PreToolUse"
            )
            self.assertIn(
                "85,000 input tokens",
                output["hookSpecificOutput"]["additionalContext"],
            )

    def test_app_server_reader_preserves_multiple_buffered_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = pathlib.Path(directory) / "fake-codex"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                'sys.stdout.write(\'{"one":1}\\n{"two":2}\\n\')\n'
                "sys.stdout.flush()\n",
                encoding="utf-8",
            )
            executable.chmod(0o700)
            old_bin = controller.CODEX_BIN
            controller.CODEX_BIN = str(executable)
            server = controller.AppServer()
            try:
                self.assertEqual(server.read(timeout=2), {"one": 1})
                self.assertEqual(server.read(timeout=2), {"two": 2})
            finally:
                server.close()
                controller.CODEX_BIN = old_bin

    def test_context_advisory_emits_once_at_soft_and_urgent_thresholds(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            transcript = root / "rollout.jsonl"
            event = {
                "hook_event_name": "PreToolUse",
                "session_id": "context-session",
                "transcript_path": str(transcript),
            }
            with mock.patch.object(hook, "DATA_ROOT", root / "data"):
                self.write_token_event(transcript, 80_000, 72_000)
                advisory = hook.context_advisory(event)
                self.assertIn("80,000-token advisory threshold", advisory)
                self.assertIn("72,000 cached", advisory)
                self.assertIsNone(hook.context_advisory(event))

                self.write_token_event(transcript, 120_000, 110_000)
                urgent = hook.context_advisory(event)
                self.assertIn("120,000-token urgent threshold", urgent)
                self.assertIsNone(hook.context_advisory(event))

    def test_latest_usage_record_avoids_pre_tool_advisory_lag(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = pathlib.Path(directory) / "rollout.jsonl"
            self.write_token_event(transcript, 70_000, 65_000)
            self.write_usage_record(transcript, 85_000, 80_000)
            self.assertEqual(
                hook.latest_context_usage(str(transcript)), (85_000, 80_000)
            )

    def test_context_advisory_rearms_after_context_drops(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            transcript = root / "rollout.jsonl"
            event = {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "rearmed-session",
                "transcript_path": str(transcript),
            }
            with mock.patch.object(hook, "DATA_ROOT", root / "data"):
                self.write_token_event(transcript, 82_000)
                self.assertIsNotNone(hook.context_advisory(event))
                self.write_token_event(transcript, 50_000)
                self.assertIsNone(hook.context_advisory(event))
                self.write_token_event(transcript, 81_000)
                self.assertIsNotNone(hook.context_advisory(event))

    def test_stale_concurrent_observation_cannot_reset_newer_advisory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            old_read = threading.Event()
            release_old = threading.Event()
            results = []

            def usage(path):
                if path == "old":
                    old_read.set()
                    self.assertTrue(release_old.wait(timeout=2))
                    return 50_000, 0
                return 125_000, 0

            def invoke(path):
                results.append(
                    hook.context_advisory(
                        {
                            "hook_event_name": "PreToolUse",
                            "session_id": "concurrent-session",
                            "transcript_path": path,
                        }
                    )
                )

            with (
                mock.patch.object(hook, "DATA_ROOT", root / "data"),
                mock.patch.object(hook, "latest_context_usage", side_effect=usage),
            ):
                old_thread = threading.Thread(target=invoke, args=("old",))
                new_thread = threading.Thread(target=invoke, args=("new",))
                old_thread.start()
                self.assertTrue(old_read.wait(timeout=2))
                new_thread.start()
                state_path = root / "data" / "advisories" / "concurrent-session.json"
                deadline = time.monotonic() + 0.5
                while not state_path.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                release_old.set()
                old_thread.join(timeout=2)
                new_thread.join(timeout=2)

            self.assertFalse(old_thread.is_alive())
            self.assertFalse(new_thread.is_alive())
            self.assertEqual(sum(result is not None for result in results), 1)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["level"], 2)

    def test_pre_tool_hook_injects_context_advisory_json(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            transcript = root / "rollout.jsonl"
            self.write_token_event(transcript, 85_000)
            event = {
                "hook_event_name": "PreToolUse",
                "session_id": "hook-advisory-session",
                "transcript_path": str(transcript),
                "cwd": str(root),
                "model": "test-model",
            }
            stdout = io.StringIO()
            with (
                mock.patch.object(hook, "REQUEST_ROOT", root / "requests"),
                mock.patch.object(hook, "DATA_ROOT", root / "data"),
                mock.patch.object(hook.sys, "stdin", io.StringIO(json.dumps(event))),
                redirect_stdout(stdout),
            ):
                self.assertEqual(hook.main(), 0)
            output = json.loads(stdout.getvalue())
            self.assertEqual(
                output["hookSpecificOutput"]["hookEventName"], "PreToolUse"
            )
            self.assertIn(
                "Phase-rollover advisory",
                output["hookSpecificOutput"]["additionalContext"],
            )

    def test_prompt_carries_lineage_and_checkpoint(self):
        request = {
            "chain_id": "chain-1",
            "generation": 4,
            "objective": "Finish",
            "next_action": "Test",
            "checkpoint": "Verified fact",
        }
        prompt = controller.prompt_for(request)
        self.assertIn("chain_id=chain-1", prompt)
        self.assertIn("generation=4", prompt)
        self.assertIn("generation=5", prompt)
        self.assertIn("Verified fact", prompt)

    def test_atomic_write_replaces_complete_json(self):
        with tempfile.TemporaryDirectory() as directory:
            target = pathlib.Path(directory) / "state.json"
            controller.atomic_write(target, {"state": "PREPARED"})
            controller.atomic_write(
                target, {"state": "ACTIVE", "child_thread_id": "abc"}
            )
            self.assertEqual(
                json.loads(target.read_text()),
                {"state": "ACTIVE", "child_thread_id": "abc"},
            )

    def test_claim_rejects_wrong_session_and_accepts_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            old = controller.CHAINS
            controller.CHAINS = pathlib.Path(directory)
            try:
                path = controller.ledger_path("chain-2", 1)
                controller.atomic_write(
                    path,
                    {
                        "state": "ACTIVE",
                        "child_thread_id": "right",
                        "owner_claimed_by": "right",
                    },
                )
                before = path.read_bytes()
                self.assertNotEqual(controller.claim("chain-2", 1, "wrong"), 0)
                self.assertEqual(controller.claim("chain-2", 1, "right"), 0)
                self.assertEqual(controller.claim("chain-2", 1, "right"), 0)
                self.assertEqual(path.read_bytes(), before)
                self.assertFalse(path.with_suffix(".json.lock").exists())
            finally:
                controller.CHAINS = old

    def test_completed_generation_is_not_dispatched_twice(self):
        class FakeServer:
            calls: ClassVar[list[str]] = []

            def initialize(self):
                self.calls.append("initialize")

            def rpc(self, method, params=None, timeout=60.0):
                self.calls.append(method)
                if method == "thread/start":
                    return {"thread": {"id": "child-1"}}
                if method == "turn/start":
                    return {"turn": {"id": "turn-1"}}
                raise AssertionError(method)

            def read(self, timeout=60.0):
                return {
                    "method": "turn/completed",
                    "params": {"turn": {"status": "completed"}},
                }

            def handle_server_request(self, message):
                return False

            def close(self):
                self.calls.append("close")

        request = {
            "chain_id": "chain-3",
            "generation": 0,
            "source_session_id": "source",
            "cwd": str(ROOT),
            "objective": "Finish",
            "next_action": "Verify",
            "checkpoint": "The prior phase passed.",
            "model": None,
            "effort": None,
        }
        with tempfile.TemporaryDirectory() as directory:
            old_chains, old_server = controller.CHAINS, controller.AppServer
            controller.CHAINS, controller.AppServer = (
                pathlib.Path(directory),
                FakeServer,
            )
            try:
                controller.run_request(request)
                controller.run_request(request)
                self.assertEqual(FakeServer.calls.count("thread/start"), 1)
                self.assertEqual(FakeServer.calls.count("turn/start"), 1)
                ledger = json.loads(controller.ledger_path("chain-3", 0).read_text())
                self.assertEqual(ledger["state"], "COMPLETED")
                self.assertEqual(ledger["child_thread_id"], "child-1")
                self.assertEqual(ledger["objective_status"], "UNVERIFIED")
            finally:
                controller.CHAINS, controller.AppServer = old_chains, old_server

    def test_live_controller_excludes_competitor_before_active(self):
        class UnexpectedServer:
            def __init__(self):
                raise AssertionError(
                    "competing controller must not create an app-server"
                )

        request = {
            "chain_id": "chain-live",
            "generation": 0,
            "source_session_id": "source-live",
            "cwd": str(ROOT),
            "objective": "Finish",
            "next_action": "Verify",
            "checkpoint": "The prior phase passed.",
        }
        with tempfile.TemporaryDirectory() as directory:
            old_chains, old_server, old_pid_alive = (
                controller.CHAINS,
                controller.AppServer,
                controller.pid_alive,
            )
            controller.CHAINS, controller.AppServer = (
                pathlib.Path(directory),
                UnexpectedServer,
            )
            controller.pid_alive = lambda _pid: True
            try:
                path = controller.ledger_path("chain-live", 0)
                controller.atomic_write(
                    path,
                    {
                        **request,
                        "state": "CHILD_CREATED",
                        "controller_pid": 424242,
                    },
                )
                controller.run_request(request)
                self.assertEqual(json.loads(path.read_text())["controller_pid"], 424242)
            finally:
                controller.CHAINS, controller.AppServer, controller.pid_alive = (
                    old_chains,
                    old_server,
                    old_pid_alive,
                )

    def test_child_can_claim_while_turn_start_is_in_flight(self):
        request = {
            "chain_id": "chain-claim-race",
            "generation": 0,
            "source_session_id": "source",
            "cwd": str(ROOT),
            "objective": "Finish",
            "next_action": "Verify",
            "checkpoint": "The prior phase passed.",
            "model": None,
            "effort": None,
        }

        class ClaimingServer:
            def initialize(self):
                pass

            def rpc(self, method, params=None, timeout=60.0):
                if method == "thread/start":
                    return {"thread": {"id": "child-race"}}
                if method == "turn/start":
                    self.assert_claim_succeeds()
                    return {"turn": {"id": "turn-race"}}
                raise AssertionError(method)

            @staticmethod
            def assert_claim_succeeds():
                if controller.claim("chain-claim-race", 0, "child-race") != 0:
                    raise AssertionError(
                        "child could not claim before turn/start acknowledgement"
                    )

            def read(self, timeout=60.0):
                return {
                    "method": "turn/completed",
                    "params": {"turn": {"status": "completed"}},
                }

            def handle_server_request(self, message):
                return False

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as directory:
            old_chains, old_server = controller.CHAINS, controller.AppServer
            controller.CHAINS, controller.AppServer = (
                pathlib.Path(directory),
                ClaimingServer,
            )
            try:
                controller.run_request(request)
                ledger = json.loads(
                    controller.ledger_path("chain-claim-race", 0).read_text()
                )
                self.assertEqual(ledger["state"], "COMPLETED")
                self.assertEqual(ledger["owner_claimed_by"], "child-race")
            finally:
                controller.CHAINS, controller.AppServer = old_chains, old_server

    def test_uncertain_turn_start_is_not_retried(self):
        request = {
            "chain_id": "chain-uncertain",
            "generation": 0,
            "source_session_id": "source",
            "cwd": str(ROOT),
            "objective": "Finish",
            "next_action": "Verify",
            "checkpoint": "The prior phase passed.",
            "model": None,
            "effort": None,
        }

        class UncertainServer:
            turn_starts = 0

            def initialize(self):
                pass

            def rpc(self, method, params=None, timeout=60.0):
                if method == "thread/start":
                    return {"thread": {"id": "child-uncertain"}}
                if method == "turn/start":
                    type(self).turn_starts += 1
                    raise TimeoutError("acknowledgement lost")
                if method == "thread/read":
                    return {"thread": {"turns": []}}
                raise AssertionError(method)

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as directory:
            old_chains, old_server = controller.CHAINS, controller.AppServer
            controller.CHAINS, controller.AppServer = (
                pathlib.Path(directory),
                UncertainServer,
            )
            try:
                with self.assertRaises(TimeoutError):
                    controller.run_request(request)
                controller.run_request(request)
                ledger = json.loads(
                    controller.ledger_path("chain-uncertain", 0).read_text()
                )
                self.assertEqual(ledger["state"], "START_UNCERTAIN")
                self.assertEqual(ledger["turn_start_state"], "UNCERTAIN")
                self.assertEqual(UncertainServer.turn_starts, 1)
            finally:
                controller.CHAINS, controller.AppServer = old_chains, old_server

    def test_acknowledged_turn_is_not_retried_after_empty_thread_read(self):
        request = {
            "chain_id": "chain-started",
            "generation": 0,
            "source_session_id": "source",
            "cwd": str(ROOT),
            "objective": "Finish",
            "next_action": "Verify",
            "checkpoint": "The prior phase passed.",
            "model": None,
            "effort": None,
        }

        class EmptyAfterStartServer:
            turn_starts = 0

            def initialize(self):
                pass

            def rpc(self, method, params=None, timeout=60.0):
                if method == "thread/start":
                    return {"thread": {"id": "child-started"}}
                if method == "turn/start":
                    type(self).turn_starts += 1
                    return {"turn": {"id": "turn-started"}}
                if method == "thread/read":
                    return {"thread": {"turns": []}}
                raise AssertionError(method)

            def read(self, timeout=60.0):
                raise TimeoutError("connection lost after turn acknowledgement")

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as directory:
            old_chains, old_server = controller.CHAINS, controller.AppServer
            controller.CHAINS, controller.AppServer = (
                pathlib.Path(directory),
                EmptyAfterStartServer,
            )
            try:
                with self.assertRaises(TimeoutError):
                    controller.run_request(request)
                controller.run_request(request)
                ledger = json.loads(
                    controller.ledger_path("chain-started", 0).read_text()
                )
                self.assertEqual(ledger["state"], "START_UNCERTAIN")
                self.assertEqual(EmptyAfterStartServer.turn_starts, 1)
            finally:
                controller.CHAINS, controller.AppServer = old_chains, old_server

    def test_version_two_request_requires_verified_untampered_capsule(self):
        checkpoint = "Verified phase evidence."
        request = {
            "version": 2,
            "boundary_ready": True,
            "chain_id": "chain-v2",
            "generation": 0,
            "source_session_id": "source",
            "cwd": str(ROOT),
            "objective": "Finish",
            "completion_criterion": "All tests pass",
            "next_action": "Implement",
            "checkpoint": checkpoint,
            "checkpoint_bytes": len(checkpoint.encode("utf-8")),
            "checkpoint_sha256": hashlib.sha256(checkpoint.encode("utf-8")).hexdigest(),
            "sandbox": "read-only",
        }
        controller.validate_request(request)

        tampered = {**request, "checkpoint": f"{checkpoint} changed"}
        with self.assertRaisesRegex(ValueError, "byte count"):
            controller.validate_request(tampered)

        unverified = {**request, "boundary_ready": False}
        with self.assertRaisesRegex(ValueError, "not verified"):
            controller.validate_request(unverified)

    def test_cancelled_prepared_generation_never_starts_app_server(self):
        class UnexpectedServer:
            def __init__(self):
                raise AssertionError(
                    "cancelled generation must not create an app-server"
                )

        request = {
            "chain_id": "chain-cancelled",
            "generation": 0,
            "source_session_id": "source-cancelled",
            "cwd": str(ROOT),
            "objective": "Finish",
            "next_action": "Verify",
            "checkpoint": "The prior phase passed.",
        }
        with tempfile.TemporaryDirectory() as directory:
            old_chains, old_server = controller.CHAINS, controller.AppServer
            controller.CHAINS, controller.AppServer = (
                pathlib.Path(directory),
                UnexpectedServer,
            )
            try:
                controller.stage_request(request)
                self.assertEqual(controller.cancel_source("source-cancelled"), 1)
                controller.run_request(request)
                ledger = json.loads(
                    controller.ledger_path("chain-cancelled", 0).read_text()
                )
                self.assertEqual(ledger["state"], "CANCELLED")
            finally:
                controller.CHAINS, controller.AppServer = old_chains, old_server

    def test_cancel_during_thread_creation_prevents_first_turn(self):
        request = {
            "chain_id": "chain-cancel-race",
            "generation": 0,
            "source_session_id": "source-cancel-race",
            "cwd": str(ROOT),
            "objective": "Finish",
            "next_action": "Verify",
            "checkpoint": "The prior phase passed.",
        }

        class CancellingServer:
            calls: ClassVar[list[str]] = []

            def initialize(self):
                self.calls.append("initialize")

            def rpc(self, method, params=None, timeout=60.0):
                self.calls.append(method)
                if method == "thread/start":
                    controller.cancel_source("source-cancel-race")
                    return {"thread": {"id": "empty-child"}}
                raise AssertionError(method)

            def close(self):
                self.calls.append("close")

        with tempfile.TemporaryDirectory() as directory:
            old_chains, old_server = controller.CHAINS, controller.AppServer
            controller.CHAINS, controller.AppServer = (
                pathlib.Path(directory),
                CancellingServer,
            )
            try:
                controller.run_request(request)
                ledger = json.loads(
                    controller.ledger_path("chain-cancel-race", 0).read_text()
                )
                self.assertEqual(ledger["state"], "CANCELLED")
                self.assertTrue(ledger["empty_child_after_cancel"])
                self.assertNotIn("turn/start", CancellingServer.calls)
            finally:
                controller.CHAINS, controller.AppServer = old_chains, old_server

    def test_legacy_request_defaults_child_to_read_only(self):
        request = {
            "chain_id": "chain-legacy-sandbox",
            "generation": 0,
            "source_session_id": "source",
            "cwd": str(ROOT),
            "objective": "Inspect",
            "next_action": "Verify",
            "checkpoint": "The prior phase passed.",
        }

        class RecordingServer:
            sandbox = None

            def initialize(self):
                pass

            def rpc(self, method, params=None, timeout=60.0):
                if method == "thread/start":
                    type(self).sandbox = params["sandbox"]
                    return {"thread": {"id": "child-read-only"}}
                if method == "turn/start":
                    return {"turn": {"id": "turn-read-only"}}
                raise AssertionError(method)

            def read(self, timeout=60.0):
                return {
                    "method": "turn/completed",
                    "params": {"turn": {"status": "completed"}},
                }

            def handle_server_request(self, message):
                return False

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as directory:
            old_chains, old_server = controller.CHAINS, controller.AppServer
            controller.CHAINS, controller.AppServer = (
                pathlib.Path(directory),
                RecordingServer,
            )
            try:
                controller.run_request(request)
                self.assertEqual(RecordingServer.sandbox, "read-only")
            finally:
                controller.CHAINS, controller.AppServer = old_chains, old_server

    def test_stop_stages_ledger_before_background_launch(self):
        request = {
            "chain_id": "chain-hook-stage",
            "generation": 0,
            "source_session_id": "source-hook",
            "cwd": str(ROOT),
            "objective": "Finish",
            "next_action": "Verify",
            "checkpoint": "The prior phase passed.",
        }
        event = {
            "hook_event_name": "Stop",
            "session_id": "source-hook",
            "cwd": str(ROOT),
            "model": "test-model",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            request_root = root / "requests"
            data_root = root / "data"
            request_root.mkdir()
            request_path = request_root / "source-hook.json"
            request_path.write_text(json.dumps(request), encoding="utf-8")
            old_chains = controller.CHAINS
            controller.CHAINS = data_root / "chains"
            launched = []

            def run_controller(args, **_kwargs):
                request_arg = pathlib.Path(args[args.index("--request") + 1])
                controller.stage_request(
                    json.loads(request_arg.read_text(encoding="utf-8"))
                )
                return SimpleNamespace(returncode=0)

            def launch_after_stage(args, log_name):
                self.assertTrue(controller.ledger_path("chain-hook-stage", 0).exists())
                launched.append((args, log_name))

            try:
                with (
                    mock.patch.object(hook, "REQUEST_ROOT", request_root),
                    mock.patch.object(hook, "DATA_ROOT", data_root),
                    mock.patch.object(
                        hook.subprocess, "run", side_effect=run_controller
                    ),
                    mock.patch.object(hook, "launch", side_effect=launch_after_stage),
                    mock.patch.object(
                        hook.sys, "stdin", io.StringIO(json.dumps(event))
                    ),
                    redirect_stdout(io.StringIO()),
                ):
                    self.assertEqual(hook.main(), 0)
                self.assertEqual(len(launched), 1)
                self.assertFalse(request_path.exists())
                self.assertEqual(len(list(request_root.glob("*.claimed.json"))), 1)
            finally:
                controller.CHAINS = old_chains

    def test_interrupt_cancels_staged_generation(self):
        request = {
            "chain_id": "chain-hook-cancel",
            "generation": 0,
            "source_session_id": "source-hook-cancel",
            "cwd": str(ROOT),
            "objective": "Finish",
            "next_action": "Verify",
            "checkpoint": "The prior phase passed.",
        }
        event = {
            "hook_event_name": "Interrupt",
            "session_id": "source-hook-cancel",
            "cwd": str(ROOT),
            "model": "test-model",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            request_root = root / "requests"
            data_root = root / "data"
            request_root.mkdir()
            old_chains = controller.CHAINS
            controller.CHAINS = data_root / "chains"
            controller.stage_request(request)

            def cancel_controller(_args, **_kwargs):
                controller.cancel_source("source-hook-cancel")
                return SimpleNamespace(returncode=0)

            try:
                with (
                    mock.patch.object(hook, "REQUEST_ROOT", request_root),
                    mock.patch.object(hook, "DATA_ROOT", data_root),
                    mock.patch.object(
                        hook.subprocess, "run", side_effect=cancel_controller
                    ),
                    mock.patch.object(
                        hook.sys, "stdin", io.StringIO(json.dumps(event))
                    ),
                    redirect_stdout(io.StringIO()),
                ):
                    self.assertEqual(hook.main(), 0)
                ledger = json.loads(
                    controller.ledger_path("chain-hook-cancel", 0).read_text()
                )
                self.assertEqual(ledger["state"], "CANCELLED")
            finally:
                controller.CHAINS = old_chains

    def test_prepare_cli_writes_version_two_capsule(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            request_root = root / "requests"
            checkpoint = root / "checkpoint.md"
            checkpoint.write_text("Verified evidence.\n", encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "prepare.py"),
                    "--session-id",
                    "source-cli",
                    "--cwd",
                    str(ROOT),
                    "--checkpoint-file",
                    str(checkpoint),
                    "--objective",
                    "Finish implementation",
                    "--completion-criterion",
                    "All validation passes",
                    "--next-action",
                    "Run tests",
                    "--sandbox",
                    "workspace-write",
                    "--phase-verified",
                    "--model",
                    "test-model",
                ],
                check=True,
                capture_output=True,
                text=True,
                env={
                    **dict(os.environ),
                    "PHASE_ROLLOVER_REQUEST_ROOT": str(request_root),
                },
            )
            output = json.loads(result.stdout)
            payload = json.loads(pathlib.Path(output["request_path"]).read_text())
            self.assertEqual(payload["version"], 2)
            self.assertTrue(payload["boundary_ready"])
            self.assertEqual(payload["sandbox"], "workspace-write")
            self.assertEqual(payload["checkpoint_bytes"], len("Verified evidence.\n"))
            controller.validate_request(payload)


if __name__ == "__main__":
    unittest.main()
