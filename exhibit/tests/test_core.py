import os
import tempfile
import threading
import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import Engine


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temp.name, "fleet.sqlite")
        self.engine = Engine(self.path)

    def tearDown(self):
        self.engine.close()
        self.temp.cleanup()

    def dispatch_next(self):
        candidates = self.engine.candidates()
        self.assertTrue(candidates)
        command = max(candidates, key=lambda item: item["priority"])
        result = self.engine.dispatch(command, self.engine.snapshot()["version"])
        self.assertTrue(result["ok"], result)
        return command, result

    def finish_active(self):
        for _ in range(5):
            if not any(t["status"] == "RUNNING" for t in self.engine.snapshot()["tasks"]):
                return
            self.engine.tick()
        self.fail("task did not finish")

    def run_cycle(self):
        for _ in range(120):
            state = self.engine.snapshot()
            if state["mission"]["status"] == "SUCCEEDED":
                return state
            if self.engine.candidates():
                self.dispatch_next()
            self.engine.tick()
        self.fail("cycle did not complete: %s" % self.engine.snapshot()["mission"])

    def test_full_and_empty_cycle_waits_for_unloading_and_releases_everything(self):
        saw_unload = False
        for _ in range(100):
            state = self.engine.snapshot()
            if state["mission"]["stage"] == "unload":
                saw_unload = True
                self.assertEqual("full", state["carts"][0]["load"])
                self.assertFalse(self.engine.candidates())
            if state["mission"]["status"] == "SUCCEEDED":
                break
            if self.engine.candidates():
                self.dispatch_next()
            self.engine.tick()
        state = self.engine.snapshot()
        self.assertTrue(saw_unload)
        self.assertEqual("SUCCEEDED", state["mission"]["status"])
        self.assertEqual(("empty", "SOURCE", None), tuple(state["carts"][0][k] for k in ("load", "station_id", "held_by")))
        self.assertTrue(all(r["held_cart_id"] is None and r["active_task_id"] is None for r in state["robots"]))
        self.assertTrue(all(s["reserved_by"] is None for s in state["stations"] + state["carts"]))
        self.assertEqual(10, state["metrics"]["completed_tasks"])
        self.assertEqual(1, state["metrics"]["completed_missions"])
        kinds = [e["type"] for e in state["events"]]
        self.assertLess(kinds.index("UNLOAD_WAIT"), kinds.index("UNLOAD_CONFIRMED"))

    def test_blocked_primary_has_feasible_alternate_and_completes(self):
        self.engine.reset("blocked_handoff")
        self.dispatch_next()
        self.finish_active()
        choices = self.engine.candidates()
        self.assertEqual(["HANDOFF_ALT"], [c["station_id"] for c in choices])
        state = self.run_cycle()
        self.assertTrue(any(t["skill"] == "place_cart" and t["station_id"] == "HANDOFF_ALT" and t["status"] == "SUCCEEDED" for t in state["tasks"]))

    def test_block_after_dispatch_does_not_claim_delivery(self):
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

    def test_stale_world_and_late_or_premature_results_never_complete_task(self):
        old = self.engine.snapshot()
        command = self.engine.candidates()[0]
        self.engine.tick()
        self.assertEqual("STALE_VERSION", self.engine.dispatch(command, old["version"])["code"])
        _, accepted = self.dispatch_next()
        state = self.engine.snapshot()
        receipt = {"epoch": state["epoch"], "task_id": accepted["task_id"], "attempt_id": accepted["attempt_id"]}
        self.assertEqual("UNVERIFIED_RECEIPT", self.engine.receive_result(receipt)["code"])
        self.assertEqual("STALE_RECEIPT", self.engine.inject("stale_result")["code"])
        self.assertEqual("RUNNING", self.engine.snapshot()["tasks"][-1]["status"])
        self.finish_active()
        self.assertEqual("STALE_RECEIPT", self.engine.receive_result(receipt)["code"])
        self.engine.reset()
        self.assertEqual("STALE_RECEIPT", self.engine.receive_result(receipt)["code"])
        self.assertEqual("pickup_full", self.engine.snapshot()["mission"]["stage"])

    def test_atomic_duplicate_and_conflicting_reservations(self):
        self.dispatch_next()
        self.finish_active()
        candidates = self.engine.candidates()
        version = self.engine.snapshot()["version"]
        results = []
        barrier = threading.Barrier(3)
        def dispatch(command):
            barrier.wait()
            results.append(self.engine.dispatch(command, version))
        threads = [threading.Thread(target=dispatch, args=(candidate,)) for candidate in candidates]
        self.assertEqual(2, len(threads))
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        self.assertEqual(1, sum(r["ok"] for r in results))
        state = self.engine.snapshot()
        active = [t for t in state["tasks"] if t["status"] == "RUNNING"]
        self.assertEqual(1, len(active))
        current = next(c for c in candidates if c["id"] == active[0]["candidate_id"])
        self.assertEqual("DUPLICATE_TASK", self.engine.dispatch(current, state["version"])["code"])
        self.assertEqual(1, sum(s["reserved_by"] is not None for s in state["stations"]))
        self.assertEqual(active[0]["id"], state["carts"][0]["reserved_by"])

    def test_offline_unknown_retains_resources_until_explicit_reconciliation(self):
        command, accepted = self.dispatch_next()
        self.engine.tick()
        self.engine.inject("robot_offline")
        for _ in range(20):
            self.engine.tick()
        state = self.engine.snapshot()
        self.assertEqual("UNKNOWN", state["tasks"][-1]["status"])
        self.assertEqual("BLOCKED", state["mission"]["status"])
        self.assertFalse(self.engine.candidates())
        self.assertEqual(accepted["task_id"], state["stations"][0]["reserved_by"])
        self.assertEqual(accepted["task_id"], state["carts"][0]["reserved_by"])
        self.engine.inject("robot_online")
        state = self.engine.snapshot()
        self.assertEqual("FAILED", state["tasks"][-1]["status"])
        self.assertEqual("SOURCE", state["carts"][0]["station_id"])
        self.assertIsNone(state["carts"][0]["reserved_by"])
        self.assertEqual(command["id"], self.engine.candidates()[0]["id"])
        self.run_cycle()

    def test_restart_recovers_unknown_persists_events_and_rejects_old_attempt(self):
        _, accepted = self.dispatch_next()
        self.engine.tick()
        before = self.engine.snapshot()
        self.engine.close()
        self.engine = Engine(self.path)
        state = self.engine.snapshot()
        self.assertEqual(before["epoch"], state["epoch"])
        self.assertEqual("UNKNOWN", state["tasks"][-1]["status"])
        self.assertEqual(accepted["task_id"], state["carts"][0]["reserved_by"])
        self.assertTrue(any(e["type"] == "RECOVERY_REQUIRED" for e in state["events"]))
        self.engine.inject("robot_online")
        _, retry = self.dispatch_next()
        self.assertNotEqual(accepted["attempt_id"], retry["attempt_id"])
        self.assertEqual("STALE_RECEIPT", self.engine.receive_result({"epoch": state["epoch"], "task_id": accepted["task_id"], "attempt_id": accepted["attempt_id"]})["code"])
        self.assertEqual("RUNNING", self.engine.snapshot()["tasks"][-1]["status"])
        self.run_cycle()

    def test_grasp_failure_is_observed_then_retried(self):
        self.engine.reset("grasp_failure")
        self.dispatch_next()
        self.finish_active()
        state = self.engine.snapshot()
        self.assertEqual("FAILED", state["tasks"][-1]["status"])
        self.assertEqual("SOURCE", state["carts"][0]["station_id"])
        self.assertIsNone(state["robots"][0]["held_cart_id"])
        self.assertEqual(1, self.run_cycle()["metrics"]["failed_tasks"])

    def test_decisions_do_not_invalidate_world_or_change_physical_state(self):
        before = self.engine.snapshot()
        self.engine.record_decision({"candidate_id": self.engine.candidates()[0]["id"], "reason": "feasible"})
        after = self.engine.snapshot()
        self.assertEqual(before["version"], after["version"])
        self.assertEqual(before["carts"], after["carts"])
        self.assertEqual(1, len(after["decisions"]))
        self.assertEqual(1, after["metrics"]["decisions_count"])


if __name__ == "__main__":
    unittest.main()
