"""Offline coverage of the additive four-robot relay."""

import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import Engine
from four_fleet import FourRobotEngine


class FourRobotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temp.name, "fleet-four.sqlite3")
        self.engine = FourRobotEngine(self.path)

    def tearDown(self):
        self.engine.close()
        self.temp.cleanup()

    def dispatch_next(self):
        candidates = self.engine.candidates()
        self.assertTrue(candidates)
        command = max(candidates, key=lambda c: c["priority"])
        result = self.engine.dispatch(command, self.engine.snapshot()["version"])
        self.assertTrue(result["ok"], result)
        return command, result

    def finish_active(self):
        for _ in range(5):
            if not any(t["status"] == "RUNNING" for t in self.engine.snapshot()["tasks"]):
                return
            self.engine.tick()
        self.fail("task did not finish")

    def assert_resources(self, state):
        cart = state["carts"][0]
        holders = [r for r in state["robots"] if r["held_cart_id"]]
        parked = [s for s in state["stations"] if s["cart_id"]]
        self.assertEqual(1, len(holders) + len(parked))
        self.assertLessEqual(sum(t["status"] in ("RUNNING", "UNKNOWN") for t in state["tasks"]), 1)
        if holders:
            self.assertEqual(holders[0]["id"], cart["held_by"])
            self.assertIsNone(cart["station_id"])
        else:
            self.assertEqual(parked[0]["id"], cart["station_id"])
            self.assertIsNone(cart["held_by"])
        for resource in state["stations"] + state["carts"]:
            if resource["reserved_by"]:
                task = next(t for t in state["tasks"] if t["id"] == resource["reserved_by"])
                self.assertIn(task["status"], ("RUNNING", "UNKNOWN"))

    def run_cycle(self):
        for _ in range(200):
            state = self.engine.snapshot()
            self.assert_resources(state)
            if state["mission"]["status"] == "SUCCEEDED":
                return state
            if self.engine.candidates():
                self.dispatch_next()
            self.engine.tick()
        self.fail("cycle did not complete: %s" % self.engine.snapshot()["mission"])

    def test_every_scenario_finishes_with_all_four_actually_handling_same_cart_both_ways(self):
        for scenario in ("normal", "blocked_handoff", "grasp_failure"):
            with self.subTest(scenario=scenario):
                self.engine.reset(scenario)
                state = self.run_cycle()
                self.assertEqual("four", state["fleet_mode"])
                self.assertEqual(["R1", "R3", "R4", "R2"], state["relay_order"])
                self.assertEqual(8, state["mission"]["phase_index"])
                self.assertEqual(22, state["metrics"]["completed_tasks"])
                self.assertEqual(1, state["metrics"]["completed_missions"])
                cart = state["carts"][0]
                self.assertEqual(("empty", "SOURCE", None), (cart["load"], cart["station_id"], cart["held_by"]))
                completed = [t for t in state["tasks"] if t["status"] == "SUCCEEDED"]
                pickups = [t["robot_id"] for t in completed if t["skill"] == "acquire_cart"]
                self.assertEqual(["R1", "R3", "R4", "R2", "R2", "R4", "R3", "R1"], pickups)
                for rid in ("R1", "R2", "R3", "R4"):
                    for skill in ("acquire_cart", "place_cart"):
                        tasks = [t for t in completed if t["robot_id"] == rid and t["skill"] == skill]
                        self.assertEqual(2, len(tasks))
                        self.assertTrue(all(t["cart_id"] == "C17" for t in tasks))
                self.assertTrue(all(r["held_cart_id"] is None and r["active_task_id"] is None for r in state["robots"]))
                self.assertTrue(all(s["reserved_by"] is None for s in state["stations"] + state["carts"]))
                if scenario == "blocked_handoff":
                    self.assertEqual(2, sum(t["skill"] == "place_cart" and t["station_id"] == "HANDOFF_ALT" for t in completed))
                self.assertEqual(1 if scenario == "grasp_failure" else 0, state["metrics"]["failed_tasks"])

    def test_every_handoff_requires_verified_retreat_before_next_pickup(self):
        state = self.run_cycle()
        tasks = state["tasks"]
        handoffs = 0
        for i, task in enumerate(tasks):
            if task["skill"] == "place_cart" and task["station_id"].startswith("HANDOFF_"):
                retreat, pickup = tasks[i + 1:i + 3]
                self.assertEqual("move_to", retreat["skill"])
                self.assertEqual(task["robot_id"], retreat["robot_id"])
                self.assertEqual(task["robot_id"] + "_WAIT", retreat["station_id"])
                self.assertEqual("acquire_cart", pickup["skill"])
                self.assertNotEqual(task["robot_id"], pickup["robot_id"])
                self.assertEqual(task["station_id"], pickup["station_id"])
                self.assertGreaterEqual(pickup["start_time"], retreat["start_time"] + retreat["duration"])
                handoffs += 1
        self.assertEqual(6, handoffs)

    def test_delivery_waits_for_independent_unload_before_return(self):
        for _ in range(100):
            if self.engine.snapshot()["mission"]["stage"] == "unload":
                break
            if self.engine.candidates():
                self.dispatch_next()
            self.engine.tick()
        state = self.engine.snapshot()
        self.assertEqual("unload", state["mission"]["stage"])
        self.assertEqual("full", state["carts"][0]["load"])
        self.assertEqual("PROCESSING", state["carts"][0]["station_id"])
        self.assertEqual(5, state["mission"]["phase_index"])
        for _ in range(2):
            self.assertFalse(self.engine.candidates())
            self.engine.tick()
            self.assertEqual("full", self.engine.snapshot()["carts"][0]["load"])
        self.engine.tick()
        self.assertEqual("empty", self.engine.snapshot()["carts"][0]["load"])
        self.assertEqual("R2", self.engine.candidates()[0]["robot_id"])
        self.run_cycle()

    def test_dispatch_and_early_receipt_never_change_physical_cart_ownership(self):
        for _ in range(200):
            before = self.engine.snapshot()
            if before["mission"]["status"] == "SUCCEEDED":
                return
            if self.engine.candidates():
                _, result = self.dispatch_next()
                after = self.engine.snapshot()
                for key in ("held_by", "station_id", "load"):
                    self.assertEqual(before["carts"][0][key], after["carts"][0][key])
                self.assertEqual([r["held_cart_id"] for r in before["robots"]], [r["held_cart_id"] for r in after["robots"]])
                receipt = {"epoch": after["epoch"], "task_id": result["task_id"], "attempt_id": result["attempt_id"]}
                self.assertEqual("UNVERIFIED_RECEIPT", self.engine.receive_result(receipt)["code"])
                self.assertEqual(after["mission"]["stage"], self.engine.snapshot()["mission"]["stage"])
            self.engine.tick()
        self.fail("cycle did not complete")

    def test_offline_on_each_robot_retains_reservations_until_explicit_recovery(self):
        injected = set()
        for _ in range(240):
            state = self.engine.snapshot()
            if state["mission"]["status"] == "SUCCEEDED":
                break
            if self.engine.candidates():
                command, accepted = self.dispatch_next()
                rid = command["robot_id"]
                if rid not in injected:
                    injected.add(rid)
                    self.engine.tick()
                    self.engine.inject("robot_offline")
                    self.engine.tick()
                    blocked = self.engine.snapshot()
                    self.assertEqual("BLOCKED", blocked["mission"]["status"])
                    self.assertFalse(self.engine.candidates())
                    self.assertEqual("UNKNOWN", blocked["tasks"][-1]["status"])
                    self.assertEqual(accepted["task_id"], blocked["carts"][0]["reserved_by"])
                    self.assert_resources(blocked)
                    self.engine.inject("robot_online")
                    self.assertEqual(command["id"], self.engine.candidates()[0]["id"])
            self.engine.tick()
        self.assertEqual({"R1", "R2", "R3", "R4"}, injected)
        self.assertEqual("SUCCEEDED", self.engine.snapshot()["mission"]["status"])

    def test_concurrent_handoff_candidates_keep_single_cart_reservation(self):
        self.dispatch_next()
        self.finish_active()
        candidates = self.engine.candidates()
        self.assertEqual(2, len(candidates))
        version = self.engine.snapshot()["version"]
        barrier = threading.Barrier(3)
        results = []
        def dispatch(candidate):
            barrier.wait()
            results.append(self.engine.dispatch(candidate, version))
        threads = [threading.Thread(target=dispatch, args=(c,)) for c in candidates]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        self.assertEqual(1, sum(r["ok"] for r in results))
        self.assert_resources(self.engine.snapshot())
        self.run_cycle()

    def test_block_after_dispatch_keeps_cart_with_sender_then_uses_alternative(self):
        self.dispatch_next()
        self.finish_active()
        self.dispatch_next()
        self.engine.inject("block_handoff")
        self.finish_active()
        state = self.engine.snapshot()
        self.assertEqual("FAILED", state["tasks"][-1]["status"])
        self.assertEqual("R1", state["carts"][0]["held_by"])
        self.assertEqual("handoff_full", state["mission"]["stage"])
        self.assertEqual(["HANDOFF_ALT"], [c["station_id"] for c in self.engine.candidates()])
        self.run_cycle()

    def test_restart_with_lite_task_preserves_unknown_and_rejects_old_attempt(self):
        for _ in range(50):
            choices = self.engine.candidates()
            if choices and choices[0]["robot_id"] == "R3":
                _, accepted = self.dispatch_next()
                break
            if choices:
                self.dispatch_next()
            self.engine.tick()
        before = self.engine.snapshot()
        self.engine.close()
        self.engine = FourRobotEngine(self.path)
        state = self.engine.snapshot()
        self.assertEqual(before["epoch"], state["epoch"])
        self.assertEqual("UNKNOWN", state["tasks"][-1]["status"])
        self.assertEqual(accepted["task_id"], state["carts"][0]["reserved_by"])
        self.engine.inject("robot_online")
        self.dispatch_next()
        self.assertEqual("STALE_RECEIPT", self.engine.receive_result({"epoch": before["epoch"], "task_id": accepted["task_id"], "attempt_id": accepted["attempt_id"]})["code"])
        self.run_cycle()
        self.engine.reset()
        self.assertEqual("STALE_RECEIPT", self.engine.receive_result({"epoch": before["epoch"], "task_id": accepted["task_id"], "attempt_id": accepted["attempt_id"]})["code"])
        self.assertEqual("pickup_full", self.engine.snapshot()["mission"]["stage"])

    def test_reject_legacy_database_without_changing_snapshot_or_events(self):
        legacy_path = os.path.join(self.temp.name, "two.sqlite3")
        legacy = Engine(legacy_path)
        command = legacy.candidates()[0]
        legacy.dispatch(command, legacy.snapshot()["version"])
        before = legacy.snapshot()
        legacy.close()
        for supplied_path in (legacy_path, Path(legacy_path)):
            with self.subTest(path_type=type(supplied_path).__name__):
                with self.assertRaisesRegex(ValueError, "两机配置数据库"):
                    FourRobotEngine(supplied_path)
        db = sqlite3.connect(legacy_path)
        try:
            raw = json.loads(db.execute("SELECT snapshot FROM world WHERE id=1").fetchone()[0])
            self.assertEqual(before["tasks"], raw["tasks"])
            self.assertEqual(before["version"], raw["version"])
            self.assertEqual(len(before["events"]), db.execute("SELECT COUNT(*) FROM events").fetchone()[0])
        finally:
            db.close()

    def test_pathlike_database_can_create_and_reopen_four_robot_world(self):
        path = Path(self.temp.name) / "path with spaces.sqlite3"
        engine = FourRobotEngine(path)
        before = engine.snapshot()
        engine.close()
        engine = FourRobotEngine(path)
        try:
            self.assertEqual(before, engine.snapshot())
            self.assertEqual("four", engine.snapshot()["fleet_mode"])
        finally:
            engine.close()

    def assert_rejected_receipt_has_no_physical_or_stage_effect(self, receipt, code):
        before = self.engine.snapshot()
        result = self.engine.receive_result(receipt)
        self.assertEqual(code, result["code"])
        after = self.engine.snapshot()
        for key in ("mission", "robots", "carts", "stations", "tasks", "version", "sim_time"):
            self.assertEqual(before[key], after[key], key)
        # Rejection audit events/counters are the intended observable effects.
        self.assertEqual(before["metrics"]["rejected_receipts"] + 1, after["metrics"]["rejected_receipts"])
        self.assertEqual(code, after["events"][-1]["type"])

    def test_rejected_receipts_preserve_all_four_stage_and_physical_state(self):
        tested = set()
        for _ in range(240):
            state = self.engine.snapshot()
            if state["mission"]["status"] == "SUCCEEDED":
                break
            if self.engine.candidates():
                _, accepted = self.dispatch_next()
                current = self.engine.snapshot()
                stage = current["mission"]["stage"]
                tested.add(stage)
                receipt = {"epoch": current["epoch"], "task_id": accepted["task_id"], "attempt_id": accepted["attempt_id"]}
                self.assert_rejected_receipt_has_no_physical_or_stage_effect(receipt, "UNVERIFIED_RECEIPT")
                self.assert_rejected_receipt_has_no_physical_or_stage_effect(dict(receipt, epoch="old-epoch"), "STALE_RECEIPT")
                self.assert_rejected_receipt_has_no_physical_or_stage_effect(dict(receipt, attempt_id="old-attempt"), "STALE_RECEIPT")
                self.finish_active()
                self.assert_rejected_receipt_has_no_physical_or_stage_effect(receipt, "STALE_RECEIPT")
            self.engine.tick()
        self.assertEqual("SUCCEEDED", self.engine.snapshot()["mission"]["status"])
        self.assertEqual(22, len(tested))

    def test_unknown_receipt_rejection_and_executor_exception_restore_stage(self):
        for _ in range(80):
            if self.engine.snapshot()["mission"]["stage"] == "handoff_mid_full":
                break
            if self.engine.candidates():
                self.dispatch_next()
            self.engine.tick()
        _, accepted = self.dispatch_next()
        before = self.engine.snapshot()
        receipt = {"epoch": before["epoch"], "task_id": accepted["task_id"], "attempt_id": accepted["attempt_id"]}
        with patch.object(Engine, "_receipt", side_effect=RuntimeError("executor interrupted")):
            with self.assertRaisesRegex(RuntimeError, "executor interrupted"):
                self.engine.receive_result(receipt)
        self.assertEqual(before, self.engine.snapshot())
        self.engine.inject("robot_offline")
        self.assert_rejected_receipt_has_no_physical_or_stage_effect(receipt, "STALE_RECEIPT")
        self.engine.inject("robot_online")
        self.run_cycle()


if __name__ == "__main__":
    unittest.main()
