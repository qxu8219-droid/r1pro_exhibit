"""Transactional, simulator-only fleet world and skill executor (Python 3.9+).

No robot, ROS, or model connections exist here. A physical operation is represented
by a simulated receipt; dispatch alone never changes a cart's physical location.
"""

import copy
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager


ACTIVE = {"RUNNING", "UNKNOWN"}
SCENARIOS = {"normal", "blocked_handoff", "grasp_failure"}
FAULTS = {"block_handoff", "clear_handoff", "fail_next_grasp", "robot_offline",
          "robot_online", "stale_result"}


class Engine:
    def __init__(self, db_path):
        if not isinstance(db_path, str) or not db_path:
            raise ValueError("db_path must be a nonempty string")
        self._lock = threading.RLock()
        self._db = sqlite3.connect(db_path, check_same_thread=False,
                                   isolation_level=None, timeout=10)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute("CREATE TABLE IF NOT EXISTS world (id INTEGER PRIMARY KEY CHECK(id=1), snapshot TEXT NOT NULL)")
        self._db.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, epoch TEXT NOT NULL, sim_time INTEGER NOT NULL, type TEXT NOT NULL, message TEXT NOT NULL, task_id TEXT, data TEXT NOT NULL)")
        if self._db.execute("SELECT 1 FROM world WHERE id=1").fetchone() is None:
            self.reset()
        else:
            with self._tx() as state:
                interrupted = []
                for task in state["tasks"]:
                    if task["status"] == "RUNNING":
                        self._mark_unknown(state, task, "服务重启：动作结果未知，保留资源预约，等待模拟器核对")
                        interrupted.append(task["id"])
                if interrupted:
                    state["version"] += 1
                    self._event(state, "RECOVERY_REQUIRED", "重启后需点击机器人上线，明确核对模拟现场", data={"task_ids": interrupted})
                    self._refresh(state)

    @contextmanager
    def _tx(self):
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute("SELECT snapshot FROM world WHERE id=1").fetchone()
                state = json.loads(row[0]) if row else {}
                yield state
                self._db.execute("INSERT OR REPLACE INTO world(id,snapshot) VALUES (1,?)",
                                 (json.dumps(state, ensure_ascii=False),))
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    @staticmethod
    def _by_id(items, item_id):
        return next((item for item in items if item["id"] == item_id), None)

    def _event(self, state, event_type, message, task_id=None, data=None):
        self._db.execute("INSERT INTO events(epoch,sim_time,type,message,task_id,data) VALUES (?,?,?,?,?,?)",
                         (state["epoch"], state["sim_time"], event_type, message, task_id,
                          json.dumps(data or {}, ensure_ascii=False)))

    def reset(self, scenario="normal"):
        if scenario not in SCENARIOS:
            raise ValueError("Unknown scenario")
        with self._tx() as state:
            old_version = state.get("version", 0)
            stations = [
                ("SOURCE", "源区", 12, 50),
                ("HANDOFF_FULL", "满车交接 A", 43, 25),
                ("HANDOFF_ALT", "满车交接 B（假设）", 57, 25),
                ("HANDOFF_EMPTY", "空车交接", 50, 76),
                ("PROCESSING", "处理工位", 88, 50),
                ("R1_WAIT", "R1 等待点", 25, 65),
                ("R2_WAIT", "R2 等待点", 75, 65),
            ]
            state.clear()
            state.update({
                "version": old_version + 1, "epoch": uuid.uuid4().hex,
                "sim_time": 0, "scenario": scenario,
                "mission": {"id": "M-" + uuid.uuid4().hex[:12], "status": "READY",
                            "goal": "R1/R2 协作送出 C17 满车并将卸空后的 C17 补回源区",
                            "stage": "pickup_full", "wait_reason": "等待派发首个技能"},
                "robots": [{"id": rid, "name": rid + (" 源区机器人" if rid == "R1" else " 转运机器人"),
                            "status": "IDLE", "station_id": station, "held_cart_id": None,
                            "active_task_id": None, "progress": 0, "online": True,
                            "location_known": True}
                           for rid, station in (("R1", "SOURCE"), ("R2", "R2_WAIT"))],
                "carts": [{"id": "C17", "load": "full", "station_id": "SOURCE",
                           "held_by": None, "reserved_by": None}],
                "stations": [{"id": sid, "name": name, "x": x, "y": y,
                              "capacity": 1, "cart_id": "C17" if sid == "SOURCE" else None,
                              "blocked": scenario == "blocked_handoff" and sid == "HANDOFF_FULL",
                              "reserved_by": None} for sid, name, x, y in stations],
                "tasks": [], "decisions": [],
                "metrics": {"completed_tasks": 0, "failed_tasks": 0, "rejected_dispatches": 0,
                            "rejected_receipts": 0, "completed_missions": 0, "decisions_count": 0},
                "_fail_next_grasp": scenario == "grasp_failure", "_unload_due": None,
            })
            self._event(state, "RESET", "创建新的模拟任务；全部位置均为示意坐标", data={"scenario": scenario})
        return {"ok": True, "code": "RESET"}

    def snapshot(self):
        with self._lock:
            self._db.execute("BEGIN")
            try:
                state = json.loads(self._db.execute("SELECT snapshot FROM world WHERE id=1").fetchone()[0])
                rows = self._db.execute("SELECT id,sim_time,type,message,task_id,data FROM events WHERE epoch=? ORDER BY id DESC LIMIT 100", (state["epoch"],)).fetchall()
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        state["events"] = [{"id": row[0], "time": row[1], "type": row[2], "message": row[3],
                            "task_id": row[4], "data": json.loads(row[5])} for row in reversed(rows)]
        state["metrics"]["active_tasks"] = sum(t["status"] in ACTIVE for t in state["tasks"])
        state["metrics"]["simulated_seconds"] = state["sim_time"]
        return {key: value for key, value in state.items() if not key.startswith("_")}

    def candidates(self):
        with self._lock:
            state = json.loads(self._db.execute("SELECT snapshot FROM world WHERE id=1").fetchone()[0])
            return self._candidates(state)

    def _candidates(self, state):
        stage = state["mission"]["stage"]
        cart = state["carts"][0]
        definitions = {
            "pickup_full": [("R1", "acquire_cart", "SOURCE", "R1 取源区满车 C17", 100)],
            "handoff_full": [("R1", "place_cart", "HANDOFF_FULL", "R1 将满车放入交接 A", 100),
                             ("R1", "place_cart", "HANDOFF_ALT", "R1 改用备用满车交接 B", 80)],
            "clear_full": [("R1", "move_to", "R1_WAIT", "R1 退出满车交接区", 100)],
            "collect_full": [("R2", "acquire_cart", cart["station_id"], "R2 接取已确认交接的满车", 100)],
            "deliver_full": [("R2", "place_cart", "PROCESSING", "R2 将满车送至处理工位", 100)],
            "pickup_empty": [("R2", "acquire_cart", "PROCESSING", "卸空事件已确认，R2 取空车", 100)],
            "handoff_empty": [("R2", "place_cart", "HANDOFF_EMPTY", "R2 将空车放入空车交接位", 100)],
            "clear_empty": [("R2", "move_to", "R2_WAIT", "R2 退出空车交接区", 100)],
            "collect_empty": [("R1", "acquire_cart", "HANDOFF_EMPTY", "R1 接取空车补位", 100)],
            "return_empty": [("R1", "place_cart", "SOURCE", "R1 将空车补回源区，闭合本轮任务", 100)],
        }
        candidates = []
        # This MVP has one dependent mission. Unknown physical outcomes suspend it.
        if any(task["status"] == "UNKNOWN" for task in state["tasks"]):
            return []
        for rid, skill, sid, description, priority in definitions.get(stage, []):
            robot = self._by_id(state["robots"], rid)
            station = self._by_id(state["stations"], sid)
            if not robot["online"] or robot["active_task_id"] or not robot["location_known"]:
                continue
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
            command = {"id": "%s:%s:%s:%s" % (rid, skill, cart["id"] if skill != "move_to" else "none", sid),
                       "robot_id": rid, "skill": skill, "station_id": sid,
                       "description": description, "priority": priority,
                       "parameters": {"simulator_only": True}}
            if skill != "move_to":
                command["cart_id"] = cart["id"]
            candidates.append(command)
        return candidates

    def dispatch(self, command, expected_version):
        if not isinstance(command, dict) or not isinstance(expected_version, int) or isinstance(expected_version, bool):
            raise ValueError("command must be an object and expected_version an integer")
        if not command.get("id") and not all(command.get(key) for key in ("robot_id", "skill", "station_id")):
            raise ValueError("command requires a candidate id or robot_id/skill/station_id")
        with self._tx() as state:
            def reject(code, message):
                state["metrics"]["rejected_dispatches"] += 1
                self._event(state, code, message)
                return {"ok": False, "code": code}
            if expected_version != state["version"]:
                return reject("STALE_VERSION", "计划读取的现场版本已过期，拒绝派发")
            candidates = self._candidates(state)
            if command.get("id"):
                selected = next((c for c in candidates if c["id"] == command["id"]), None)
            else:
                selected = next((c for c in candidates if all(c.get(k) == command.get(k) for k in ("robot_id", "skill", "station_id", "cart_id"))), None)
            if selected is None:
                duplicate = any(task["status"] in ACTIVE and task["candidate_id"] == command.get("id") for task in state["tasks"])
                return reject("DUPLICATE_TASK" if duplicate else "NOT_FEASIBLE", "候选已执行、资源被占用或前置条件不满足")
            if any(key in command and command[key] != selected.get(key) for key in ("robot_id", "skill", "cart_id", "station_id", "parameters")):
                return reject("COMMAND_MISMATCH", "候选参数被修改，拒绝派发")
            robot = self._by_id(state["robots"], selected["robot_id"])
            station = self._by_id(state["stations"], selected["station_id"])
            task = {"id": "T-" + uuid.uuid4().hex[:16], "attempt_id": "A-" + uuid.uuid4().hex,
                    "epoch": state["epoch"], "mission_id": state["mission"]["id"],
                    "candidate_id": selected["id"], "robot_id": robot["id"], "skill": selected["skill"],
                    "cart_id": selected.get("cart_id"), "station_id": station["id"],
                    "source_station_id": robot["station_id"], "status": "RUNNING", "progress": 0,
                    "reason": "模拟动作执行中，尚未确认物理完成", "start_time": state["sim_time"],
                    "duration": 2 if selected["skill"] == "move_to" else 4,
                    "stage": state["mission"]["stage"]}
            state["tasks"].append(task)
            robot.update(active_task_id=task["id"], status="RUNNING", progress=0)
            station["reserved_by"] = task["id"]
            if task["cart_id"]:
                self._by_id(state["carts"], task["cart_id"])["reserved_by"] = task["id"]
            state["version"] += 1
            self._event(state, "TASK_DISPATCHED", selected["description"], task["id"],
                        {"epoch": task["epoch"], "attempt_id": task["attempt_id"], "candidate_id": selected["id"]})
            self._refresh(state)
            return {"ok": True, "code": "ACCEPTED", "task_id": task["id"], "attempt_id": task["attempt_id"]}

    def _release(self, state, task):
        robot = self._by_id(state["robots"], task["robot_id"])
        if robot["active_task_id"] == task["id"]:
            robot.update(active_task_id=None, status="IDLE" if robot["online"] else "OFFLINE", progress=0)
        for resource in state["stations"] + state["carts"]:
            if resource["reserved_by"] == task["id"]:
                resource["reserved_by"] = None

    def _fail(self, state, task, reason):
        task.update(status="FAILED", reason=reason)
        state["metrics"]["failed_tasks"] += 1
        self._release(state, task)
        self._event(state, "TASK_FAILED", reason, task["id"], {"attempt_id": task["attempt_id"], "physical_effect": "none; simulator confirmed"})

    def _receipt(self, state, receipt):
        task = self._by_id(state["tasks"], receipt.get("task_id"))
        robot = self._by_id(state["robots"], task["robot_id"]) if task else None
        valid = (receipt.get("epoch") == state["epoch"] and task is not None
                 and task["epoch"] == receipt.get("epoch")
                 and task["attempt_id"] == receipt.get("attempt_id")
                 and task["status"] == "RUNNING" and robot["active_task_id"] == task["id"])
        if not valid or task["progress"] < 1:
            state["metrics"]["rejected_receipts"] += 1
            code = "STALE_RECEIPT" if not valid else "UNVERIFIED_RECEIPT"
            self._event(state, code, "拒绝迟到、重复、不匹配或未到完成条件的结果回执",
                        receipt.get("task_id"), {"received_epoch": receipt.get("epoch"), "received_attempt": receipt.get("attempt_id")})
            return {"ok": False, "code": code}
        station = self._by_id(state["stations"], task["station_id"])
        cart = self._by_id(state["carts"], task["cart_id"]) if task["cart_id"] else None
        if not robot["online"]:
            self._mark_unknown(state, task, "机器人离线，无法确认动作结果")
            return {"ok": False, "code": "UNKNOWN"}
        if station["blocked"] or station["reserved_by"] != task["id"]:
            self._fail(state, task, "模拟器确认目标位被阻挡，未发生交付；需要重规划")
            return {"ok": False, "code": "TARGET_BLOCKED"}
        if task["skill"] == "acquire_cart":
            if state["_fail_next_grasp"]:
                state["_fail_next_grasp"] = False
                self._fail(state, task, "模拟抓取检测失败，已确认未持车，可重新选择任务")
                return {"ok": False, "code": "GRASP_FAILED"}
            if station["cart_id"] != cart["id"] or cart["station_id"] != station["id"] or cart["held_by"]:
                self._mark_unknown(state, task, "取车前后状态不一致，保留预约等待核对")
                return {"ok": False, "code": "UNKNOWN"}
            station["cart_id"] = None
            cart.update(station_id=None, held_by=robot["id"])
            robot["held_cart_id"] = cart["id"]
        elif task["skill"] == "place_cart":
            if station["cart_id"] or robot["held_cart_id"] != cart["id"] or cart["held_by"] != robot["id"]:
                self._mark_unknown(state, task, "放车前后状态不一致，保留预约等待核对")
                return {"ok": False, "code": "UNKNOWN"}
            station["cart_id"] = cart["id"]
            cart.update(station_id=station["id"], held_by=None)
            robot["held_cart_id"] = None
        robot.update(station_id=station["id"], location_known=True)
        task.update(status="SUCCEEDED", progress=1, reason="模拟回执与取放/到位条件均已确认")
        self._release(state, task)
        stages = {"pickup_full": "handoff_full", "handoff_full": "clear_full",
                  "clear_full": "collect_full", "collect_full": "deliver_full", "deliver_full": "unload",
                  "pickup_empty": "handoff_empty", "handoff_empty": "clear_empty", "clear_empty": "collect_empty",
                  "collect_empty": "return_empty", "return_empty": "complete"}
        state["mission"]["stage"] = stages[task["stage"]]
        if task["stage"] == "deliver_full":
            state["_unload_due"] = state["sim_time"] + 3
            self._event(state, "UNLOAD_WAIT", "等待独立的模拟工位卸空事件，送达不等于卸空")
        state["metrics"]["completed_tasks"] += 1
        self._event(state, "TASK_SUCCEEDED", task["reason"], task["id"],
                    {"epoch": task["epoch"], "attempt_id": task["attempt_id"], "skill": task["skill"]})
        if state["mission"]["stage"] == "complete":
            state["metrics"]["completed_missions"] += 1
            self._event(state, "MISSION_COMPLETED", "C17 已为空车并补回源区；两机均已释放车辆和预约")
        return {"ok": True, "code": "SUCCEEDED", "task_id": task["id"]}

    def receive_result(self, receipt):
        """Simulator test seam. An early or incorrectly correlated receipt is rejected."""
        if not isinstance(receipt, dict) or not all(isinstance(receipt.get(k), str) for k in ("epoch", "task_id", "attempt_id")):
            raise ValueError("receipt requires string epoch/task_id/attempt_id")
        with self._tx() as state:
            result = self._receipt(state, receipt)
            if result["ok"] or result["code"] not in {"STALE_RECEIPT", "UNVERIFIED_RECEIPT"}:
                state["version"] += 1
            self._refresh(state)
            return result

    def tick(self):
        with self._tx() as state:
            if state["mission"]["stage"] == "complete":
                return {"ok": True, "code": "COMPLETE"}
            state["sim_time"] += 1
            state["version"] += 1
            for task in state["tasks"]:
                if task["status"] != "RUNNING":
                    continue
                robot = self._by_id(state["robots"], task["robot_id"])
                if not robot["online"]:
                    self._mark_unknown(state, task, "机器人离线，保留已预约的物理资源")
                    continue
                task["progress"] = min(1.0, (state["sim_time"] - task["start_time"]) / task["duration"])
                robot["progress"] = task["progress"]
                if task["progress"] >= 1:
                    self._receipt(state, {"epoch": task["epoch"], "task_id": task["id"], "attempt_id": task["attempt_id"]})
            if state["_unload_due"] is not None and state["sim_time"] >= state["_unload_due"]:
                cart = state["carts"][0]
                if cart["station_id"] == "PROCESSING" and not cart["held_by"] and state["mission"]["stage"] == "unload":
                    cart["load"] = "empty"
                    state["mission"]["stage"] = "pickup_empty"
                    state["_unload_due"] = None
                    self._event(state, "UNLOAD_CONFIRMED", "模拟工位事件确认 C17 已卸空；可生成空车返回任务")
            self._refresh(state)
            return {"ok": True, "code": "TICK"}

    def _mark_unknown(self, state, task, reason):
        task.update(status="UNKNOWN", reason=reason)
        robot = self._by_id(state["robots"], task["robot_id"])
        robot.update(status="UNKNOWN", online=False, location_known=False)
        self._event(state, "TASK_UNKNOWN", reason, task["id"], {"attempt_id": task["attempt_id"], "reservations_retained": True})

    def inject(self, kind):
        if kind not in FAULTS:
            raise ValueError("Unknown fault kind")
        with self._tx() as state:
            if kind in {"block_handoff", "clear_handoff"}:
                self._by_id(state["stations"], "HANDOFF_FULL")["blocked"] = kind == "block_handoff"
                self._event(state, "HANDOFF_BLOCKED" if kind == "block_handoff" else "HANDOFF_CLEARED", "满车交接 A 被占用" if kind == "block_handoff" else "满车交接 A 已确认恢复可用")
            elif kind == "fail_next_grasp":
                state["_fail_next_grasp"] = True
                self._event(state, "FAULT_ARMED", "下一次模拟抓取将检测失败")
            elif kind == "robot_offline":
                robot = next((r for r in state["robots"] if r["active_task_id"] and r["online"]), None)
                robot = robot or next((r for r in reversed(state["robots"]) if r["online"]), None)
                if robot:
                    robot["online"] = False
                    if robot["active_task_id"]:
                        self._mark_unknown(state, self._by_id(state["tasks"], robot["active_task_id"]), "模拟通信中断，动作状态 UNKNOWN，保留资源")
                    else:
                        robot["status"] = "OFFLINE"
                        robot["location_known"] = False
                    self._event(state, "ROBOT_OFFLINE", robot["id"] + " 离线")
            elif kind == "robot_online":
                for robot in state["robots"]:
                    if robot["online"] and robot["status"] != "UNKNOWN":
                        continue
                    robot.update(online=True, location_known=True)
                    task = self._by_id(state["tasks"], robot["active_task_id"])
                    if task and task["status"] == "UNKNOWN":
                        # The discrete simulator commits cart effects only at a verified
                        # completion. Its saved state can therefore confirm no effect.
                        self._fail(state, task, "模拟器明确核对：中断动作未提交，车辆仍在已保存位置，释放预约后重试")
                    robot["status"] = "IDLE"
                    self._event(state, "SIMULATOR_RECONCILED", robot["id"] + " 上线；模拟器确认保存的位置和持车状态", task["id"] if task else None,
                                {"station_id": robot["station_id"], "held_cart_id": robot["held_cart_id"], "simulator_only": True})
            else:
                task = next((t for t in state["tasks"] if t["status"] == "RUNNING"), None)
                result = self._receipt(state, {"epoch": state["epoch"], "task_id": task["id"] if task else "T-old",
                                               "attempt_id": "A-stale-" + uuid.uuid4().hex})
                self._refresh(state)
                return result
            state["version"] += 1
            self._refresh(state)
            return {"ok": True, "code": "INJECTED", "kind": kind}

    def _refresh(self, state):
        mission = state["mission"]
        if mission["stage"] == "complete":
            mission.update(status="SUCCEEDED", wait_reason="本轮闭环已完成")
        elif any(t["status"] == "UNKNOWN" for t in state["tasks"]):
            mission.update(status="BLOCKED", wait_reason="物理结果未知：预约保留，需明确模拟器核对（机器人上线）")
        elif any(t["status"] == "RUNNING" for t in state["tasks"]):
            mission.update(status="RUNNING", wait_reason="等待当前技能的验证回执")
        elif mission["stage"] == "unload":
            mission.update(status="WAITING", wait_reason="等待独立工位卸空事件")
        elif self._candidates(state):
            mission.update(status="READY", wait_reason="存在可行技能，等待调度决策")
        else:
            mission.update(status="BLOCKED", wait_reason="没有可行技能：检查机器人在线状态、车位阻挡和资源预约")

    def record_decision(self, decision):
        if not isinstance(decision, dict):
            raise ValueError("decision must be an object")
        try:
            safe_copy = json.loads(json.dumps(decision, ensure_ascii=False, allow_nan=False))
        except (TypeError, ValueError) as exc:
            raise ValueError("decision must be finite JSON data") from exc
        with self._tx() as state:
            safe_copy.setdefault("time", state["sim_time"])
            safe_copy.setdefault("world_version", state["version"])
            safe_copy.setdefault("epoch", state["epoch"])
            state["decisions"].append(safe_copy)
            state["decisions"] = state["decisions"][-60:]
            state["metrics"]["decisions_count"] += 1
        return {"ok": True, "code": "RECORDED"}

    def close(self):
        with self._lock:
            self._db.close()
