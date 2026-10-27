"""Four-robot exhibition relay, layered on the unchanged simulator executor.

All movement is discrete simulation. The single cart changes ownership only after
Engine verifies a receipt; UNKNOWN actions retain their original reservations.
"""

import json
import os
from pathlib import Path
import sqlite3
import uuid

from core import Engine, SCENARIOS


PHASE_LABELS = ["源区取车", "交接 A", "交接 B", "交接 C", "工位送达",
                "卸空确认", "三站返程", "空车回源"]
ROBOT_NAMES = {"R1": "R1 Pro 01", "R2": "R1 Pro 02",
               "R3": "R1 Lite 01", "R4": "R1 Lite 02"}
# Ordered actions: stage, robot, skill, destination, next stage, phase, label.
# A None destination means the cart's last verified physical station.
_STEPS = [
    ("pickup_full", "R1", "acquire_cart", "SOURCE", "handoff_full", 0, "源区取满车"),
    ("handoff_full", "R1", "place_cart", "HANDOFF_FULL", "clear_full", 1, "满车放入交接 A"),
    ("clear_full", "R1", "move_to", "R1_WAIT", "collect_full", 1, "退出交接 A"),
    ("collect_full", "R3", "acquire_cart", None, "handoff_mid_full", 1, "交接 A 接取满车"),
    ("handoff_mid_full", "R3", "place_cart", "HANDOFF_MID", "clear_mid_full", 2, "满车送至交接 B"),
    ("clear_mid_full", "R3", "move_to", "R3_WAIT", "collect_mid_full", 2, "退出交接 B"),
    ("collect_mid_full", "R4", "acquire_cart", "HANDOFF_MID", "handoff_end_full", 2, "交接 B 接取满车"),
    ("handoff_end_full", "R4", "place_cart", "HANDOFF_END", "clear_end_full", 3, "满车送至交接 C"),
    ("clear_end_full", "R4", "move_to", "R4_WAIT", "collect_end_full", 3, "退出交接 C"),
    ("collect_end_full", "R2", "acquire_cart", "HANDOFF_END", "deliver_full", 3, "交接 C 接取满车"),
    ("deliver_full", "R2", "place_cart", "PROCESSING", "unload", 4, "满车送至处理工位"),
    ("pickup_empty", "R2", "acquire_cart", "PROCESSING", "handoff_end_empty", 6, "工位确认卸空后取空车"),
    ("handoff_end_empty", "R2", "place_cart", "HANDOFF_END", "clear_end_empty", 6, "空车放入交接 C"),
    ("clear_end_empty", "R2", "move_to", "R2_WAIT", "collect_end_empty", 6, "退出交接 C"),
    ("collect_end_empty", "R4", "acquire_cart", "HANDOFF_END", "handoff_mid_empty", 6, "交接 C 接取空车"),
    ("handoff_mid_empty", "R4", "place_cart", "HANDOFF_MID", "clear_mid_empty", 6, "空车送至交接 B"),
    ("clear_mid_empty", "R4", "move_to", "R4_WAIT", "collect_mid_empty", 6, "退出交接 B"),
    ("collect_mid_empty", "R3", "acquire_cart", "HANDOFF_MID", "handoff_empty", 6, "交接 B 接取空车"),
    ("handoff_empty", "R3", "place_cart", "HANDOFF_FULL", "clear_empty", 6, "空车送至交接 A"),
    ("clear_empty", "R3", "move_to", "R3_WAIT", "collect_empty", 6, "退出交接 A"),
    ("collect_empty", "R1", "acquire_cart", None, "return_empty", 6, "交接 A 接取空车"),
    ("return_empty", "R1", "place_cart", "SOURCE", "complete", 7, "空车补回源区"),
]
_BY_STAGE = {step[0]: step for step in _STEPS}


class FourRobotEngine(Engine):
    """Two Pro endpoints and two Lite transport legs, using one shared cart."""

    def __init__(self, db_path):
        if isinstance(db_path, os.PathLike):
            db_path = os.fsdecode(os.fspath(db_path))
        if not isinstance(db_path, str) or not db_path:
            raise ValueError("db_path must be a nonempty string or PathLike")
        # Reject a legacy two-robot world before Engine can mark running tasks
        # UNKNOWN or otherwise mutate it. Keep separate databases for both modes.
        if db_path != ":memory:":
            path = Path(db_path)
            if path.is_file() and path.stat().st_size:
                db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
                try:
                    has_world = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='world'").fetchone()
                    row = db.execute("SELECT snapshot FROM world WHERE id=1").fetchone() if has_world else None
                    if row and json.loads(row[0]).get("fleet_mode") != "four":
                        raise ValueError("这是两机配置数据库，请为四机展台使用独立的 fleet-four.sqlite3；原数据未更改。")
                finally:
                    db.close()
        super().__init__(db_path)

    def reset(self, scenario="normal"):
        if scenario not in SCENARIOS:
            raise ValueError("Unknown scenario")
        stations = [
            ("SOURCE", "源区", 5, 45),
            ("HANDOFF_FULL", "交接 A", 28, 28),
            ("HANDOFF_ALT", "交接 A 备用", 28, 65),
            ("HANDOFF_MID", "交接 B", 50, 45),
            ("HANDOFF_END", "交接 C", 72, 28),
            ("PROCESSING", "处理工位", 95, 45),
            ("R1_WAIT", "Pro 01 等待", 5, 80),
            ("R2_WAIT", "Pro 02 等待", 95, 80),
            ("R3_WAIT", "Lite 01 等待", 40, 80),
            ("R4_WAIT", "Lite 02 等待", 60, 80),
        ]
        with self._tx() as state:
            version = state.get("version", 0) + 1
            state.clear()
            state.update({
                "version": version, "epoch": uuid.uuid4().hex, "sim_time": 0,
                "scenario": scenario, "fleet_mode": "four",
                "relay_order": ["R1", "R3", "R4", "R2"],
                "mission": {"id": "M-" + uuid.uuid4().hex[:12], "status": "READY",
                            "goal": "两台 R1 Pro 与两台 R1 Lite 经三处交接送出 C17 满车，卸空后原路接力补回源区",
                            "stage": "pickup_full", "wait_reason": "等待派发首个技能",
                            "phase_labels": list(PHASE_LABELS)},
                "robots": [{"id": rid, "name": ROBOT_NAMES[rid], "status": "IDLE",
                            "station_id": "SOURCE" if rid == "R1" else rid + "_WAIT",
                            "held_cart_id": None, "active_task_id": None, "progress": 0,
                            "online": True, "location_known": True}
                           for rid in ("R1", "R2", "R3", "R4")],
                "carts": [{"id": "C17", "load": "full", "station_id": "SOURCE",
                           "held_by": None, "reserved_by": None}],
                "stations": [{"id": sid, "name": name, "x": x, "y": y, "capacity": 1,
                              "cart_id": "C17" if sid == "SOURCE" else None,
                              "blocked": scenario == "blocked_handoff" and sid == "HANDOFF_FULL",
                              "reserved_by": None} for sid, name, x, y in stations],
                "tasks": [], "decisions": [],
                "metrics": {"completed_tasks": 0, "failed_tasks": 0, "rejected_dispatches": 0,
                            "rejected_receipts": 0, "completed_missions": 0, "decisions_count": 0},
                "_fail_next_grasp": scenario == "grasp_failure", "_unload_due": None,
            })
            self._refresh(state)
            self._event(state, "RESET", "四机模拟接力：两台 R1 Pro + 两台 R1 Lite；全部位置均为示意坐标",
                        data={"scenario": scenario, "fleet_mode": "four"})
        return {"ok": True, "code": "RESET"}

    def _candidates(self, state):
        # The additional graph uses the same resource checks as Engine. No new
        # physical primitive or ability to bypass its dispatch checks is added.
        if any(task["status"] == "UNKNOWN" for task in state["tasks"]):
            return []
        step = _BY_STAGE.get(state["mission"]["stage"])
        if not step:
            return []
        stage, rid, skill, destination, _, _, label = step
        cart = state["carts"][0]
        robot = self._by_id(state["robots"], rid)
        if not robot["online"] or robot["active_task_id"] or not robot["location_known"]:
            return []
        destinations = [(destination or cart["station_id"], 100)]
        if stage in ("handoff_full", "handoff_empty"):
            destinations.append(("HANDOFF_ALT", 80))
        candidates = []
        for sid, priority in destinations:
            station = self._by_id(state["stations"], sid)
            if not station or station["blocked"] or station["reserved_by"]:
                continue
            if any(other["id"] != rid and other["station_id"] == sid for other in state["robots"]):
                continue
            if skill == "acquire_cart":
                if robot["held_cart_id"] or cart["held_by"] or cart["reserved_by"] or cart["station_id"] != sid or station["cart_id"] != cart["id"]:
                    continue
            elif skill == "place_cart":
                if robot["held_cart_id"] != cart["id"] or cart["held_by"] != rid or cart["reserved_by"] or station["cart_id"]:
                    continue
            elif robot["held_cart_id"] or station["cart_id"]:
                continue
            description = rid + " " + label
            if sid == "HANDOFF_ALT":
                description += "（使用 A 备用位）"
            command = {"id": "%s:%s:%s:%s" % (rid, skill, cart["id"] if skill != "move_to" else "none", sid),
                       "robot_id": rid, "skill": skill, "station_id": sid,
                       "description": description, "priority": priority,
                       "parameters": {"simulator_only": True}}
            if skill != "move_to":
                command["cart_id"] = cart["id"]
            candidates.append(command)
        return candidates

    def _receipt(self, state, receipt):
        task = self._by_id(state["tasks"], receipt.get("task_id"))
        if task is None:
            return super()._receipt(state, receipt)
        actual_stage = task["stage"]
        # Engine's receipt executor also contains its fixed two-robot stage map.
        # Translate only that map input inside the transaction, then restore the
        # task's public stage and apply our graph. All physical commits, resource
        # checks, failure handling, and receipt correlation stay in Engine.
        # Only the real delivery/final return may trigger unload/completion.
        task["stage"] = actual_stage if actual_stage in ("deliver_full", "return_empty") else "pickup_full"
        try:
            result = super()._receipt(state, receipt)
        finally:
            task["stage"] = actual_stage
        if result["ok"]:
            state["mission"]["stage"] = _BY_STAGE[actual_stage][4]
        return result

    def _refresh(self, state):
        super()._refresh(state)
        stage = state["mission"]["stage"]
        if stage == "complete":
            phase, label = 8, "四机接力闭环完成"
        elif stage == "unload":
            phase, label = 5, "处理工位独立卸空确认"
        else:
            step = _BY_STAGE[stage]
            phase, label = step[5], ROBOT_NAMES[step[1]] + " · " + step[6]
        state["mission"].update(phase_index=phase, stage_label=label)

    def _event(self, state, event_type, message, task_id=None, data=None):
        if event_type == "MISSION_COMPLETED":
            message = "C17 已为空车并补回源区；四机均已释放车辆和预约"
        return super()._event(state, event_type, message, task_id, data)
