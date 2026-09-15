#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


controller = load("rollover_controller", ROOT / "scripts" / "controller.py")
prepare = load("rollover_prepare", ROOT / "scripts" / "prepare.py")


class RolloverTests(unittest.TestCase):
    def test_prompt_carries_lineage_and_checkpoint(self):
        request = {"chain_id": "chain-1", "generation": 4, "objective": "Finish", "next_action": "Test", "checkpoint": "Verified fact"}
        prompt = controller.prompt_for(request)
        self.assertIn("chain_id=chain-1", prompt)
        self.assertIn("generation=4", prompt)
        self.assertIn("generation=5", prompt)
        self.assertIn("Verified fact", prompt)

    def test_atomic_write_replaces_complete_json(self):
        with tempfile.TemporaryDirectory() as directory:
            target = pathlib.Path(directory) / "state.json"
            controller.atomic_write(target, {"state": "PREPARED"})
            controller.atomic_write(target, {"state": "ACTIVE", "child_thread_id": "abc"})
            self.assertEqual(json.loads(target.read_text()), {"state": "ACTIVE", "child_thread_id": "abc"})

    def test_claim_rejects_wrong_session_and_accepts_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            old = controller.CHAINS
            controller.CHAINS = pathlib.Path(directory)
            try:
                path = controller.ledger_path("chain-2", 1)
                controller.atomic_write(path, {"state": "ACTIVE", "child_thread_id": "right"})
                self.assertNotEqual(controller.claim("chain-2", 1, "wrong"), 0)
                self.assertEqual(controller.claim("chain-2", 1, "right"), 0)
                self.assertEqual(controller.claim("chain-2", 1, "right"), 0)
            finally:
                controller.CHAINS = old

    def test_completed_generation_is_not_dispatched_twice(self):
        class FakeServer:
            calls = []

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
                return {"method": "turn/completed", "params": {"turn": {"status": "completed"}}}

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
            controller.CHAINS, controller.AppServer = pathlib.Path(directory), FakeServer
            try:
                controller.run_request(request)
                controller.run_request(request)
                self.assertEqual(FakeServer.calls.count("thread/start"), 1)
                self.assertEqual(FakeServer.calls.count("turn/start"), 1)
                ledger = json.loads(controller.ledger_path("chain-3", 0).read_text())
                self.assertEqual(ledger["state"], "COMPLETED")
                self.assertEqual(ledger["child_thread_id"], "child-1")
            finally:
                controller.CHAINS, controller.AppServer = old_chains, old_server


if __name__ == "__main__":
    unittest.main()
