"""Local/LAN fleet simulator and operator console. Python 3.9+, no dependencies."""
import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from core import Engine
from planner import Planner
from evidence import EvidenceError, read_recording, read_telemetry
from instance_identity import instance_identity
from vla_evidence import VLAStore, unconfigured_state
from robot_clock import robot_clock_offset as validate_robot_clock_offset


class Runtime:
    def __init__(self, db_path, interval=0.65, fleet_size=2):
        if fleet_size not in (2, 4):
            raise ValueError("fleet_size must be 2 or 4")
        self.fleet_size = fleet_size
        self.db_path = db_path
        if fleet_size == 4:
            from four_fleet import FourRobotEngine
            self.engine = FourRobotEngine(str(db_path))
        else:
            self.engine = Engine(str(db_path))
        self.planner = Planner(mode="baseline")
        self.interval = interval
        self.lock = threading.RLock()
        self.running = False
        self.pending = False
        self.last_error = None
        self.generation = 0
        self.closed = threading.Event()
        self.worker = threading.Thread(target=self._loop, daemon=True, name="fleet-loop")
        self.worker.start()

    def state(self):
        with self.lock:
            state = self.engine.snapshot()
            state["runtime"] = {
                "running": self.running,
                "planner_mode": self.planner.mode,
                "model": self.planner.config_status(),
                "interval_ms": round(self.interval * 1000),
                "decision_pending": self.pending,
                "last_error": self.last_error,
                "simulation": True,
            }
            return state

    def _loop(self):
        while not self.closed.wait(self.interval):
            with self.lock:
                should_run = self.running and not self.pending
            if should_run:
                try:
                    self.advance(require_running=True)
                except Exception:
                    with self.lock:
                        self.last_error = "运行出现异常，模拟已暂停；请查看本地测试或重置场景。"
                        self.running = False

    def advance(self, require_running=False):
        with self.lock:
            if self.pending:
                return {"ok": False, "code": "DECISION_PENDING"}
            if require_running and not self.running:
                return {"ok": False, "code": "PAUSED"}
            self.engine.tick()
            snapshot = self.engine.snapshot()
            candidates = self.engine.candidates()
            if snapshot["mission"]["status"] == "SUCCEEDED":
                self.running = False
            if not candidates:
                return {"ok": True, "code": "WAITING"}
            generation = self.generation
            planner = self.planner
            self.pending = True
        try:
            # Network inference never holds the control lock. Pause/reset remain responsive.
            decision = planner.choose(snapshot, candidates)
            with self.lock:
                if generation != self.generation or snapshot["epoch"] != self.engine.snapshot()["epoch"]:
                    return {"ok": False, "code": "DECISION_SUPERSEDED"}
                decision["world_version"] = snapshot["version"]
                decision["candidates"] = [{"id": c["id"], "description": c["description"]} for c in candidates]
                if decision.get("status") != "ok":
                    self.last_error = decision.get("error") or decision.get("reason") or "模型未返回有效决策。"
                    self.running = False
                    decision["dispatch"] = {"ok": False, "code": "PLANNER_ERROR"}
                elif decision.get("candidate_id") is None:
                    decision["dispatch"] = {"ok": True, "code": "WAIT"}
                    self.last_error = None
                else:
                    command = next((c for c in candidates if c["id"] == decision["candidate_id"]), None)
                    if command is None:
                        decision["dispatch"] = {"ok": False, "code": "INVALID_CANDIDATE"}
                        self.last_error = "模型选择的任务不在当前候选列表内。"
                        self.running = False
                    else:
                        decision["dispatch"] = self.engine.dispatch(command, snapshot["version"])
                        self.last_error = None
                self.engine.record_decision(decision)
                return {"ok": True, "code": "DECIDED", "decision": decision}
        finally:
            with self.lock:
                self.pending = False

    def control(self, body):
        action = body.get("action")
        if action == "step":
            with self.lock:
                self.running = False
                self.generation += 1
            return self.advance()
        with self.lock:
            if action == "start":
                if self.planner.mode == "model" and not self.planner.config_status().get("configured"):
                    raise ValueError("模型尚未配置。请设置本地环境变量后重新启动服务，或选择规则调度。")
                self.running = True
                self.last_error = None
            elif action == "pause":
                self.running = False
                self.generation += 1
            elif action == "reset":
                self.running = False
                self.generation += 1
                self.engine.reset(body.get("scenario", "normal"))
                self.last_error = None
            else:
                raise ValueError("未知操作。")
        return {"ok": True}

    def set_mode(self, mode):
        if mode not in ("baseline", "model"):
            raise ValueError("调度模式应为 baseline 或 model。")
        with self.lock:
            planner = Planner(mode=mode)
            if mode == "model" and not planner.config_status().get("configured"):
                raise ValueError("尚未配置真实模型；参见项目 MODEL.md。当前继续使用规则调度。")
            self.generation += 1
            self.running = False
            self.planner = planner
            self.last_error = None
        return {"ok": True}

    def inject(self, kind):
        with self.lock:
            self.generation += 1
            return self.engine.inject(kind)

    def close(self):
        self.closed.set()
        with self.lock:
            self.running = False
            self.generation += 1
        self.worker.join(timeout=1)
        # A late inference is discarded before it can access a closed database.
        with self.lock:
            self.engine.close()


def make_handler(runtime, telemetry_file=None, vla_store=None, robot_clock_offset=0.0):
    robot_clock_offset = validate_robot_clock_offset(robot_clock_offset)
    identity = instance_identity(Path(__file__).resolve().parent, runtime.db_path,
                                 runtime.fleet_size, telemetry_file,
                                 vla_db=vla_store.path if vla_store is not None else None,
                                 robot_clock_offset=robot_clock_offset)

    def clock_diagnostic(result):
        if robot_clock_offset:
            result["robot_clock_offset_s"] = robot_clock_offset
        return result

    class Handler(BaseHTTPRequestHandler):
        server_version = "R1FleetExhibit/0.1"

        def log_message(self, fmt, *args):
            # No request bodies, model credentials, or generated content in access logs.
            pass

        def _headers(self, code, content_type, length, csp=None):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", csp or "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-src 'self'; frame-ancestors 'self'; base-uri 'none'; form-action 'self'")
            self.end_headers()

        def json_response(self, data, code=200):
            payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self._headers(code, "application/json; charset=utf-8", len(payload))
            self.wfile.write(payload)

        def valid_host(self):
            allowed = {"127.0.0.1:" + str(self.server.server_port), "localhost:" + str(self.server.server_port)}
            # A wildcard listener accepts LAN connections too. Trust only the
            # actual local destination address, never an arbitrary Host or DNS name.
            address, port = self.connection.getsockname()[:2]
            allowed.add("%s:%d" % (address, port))
            return self.headers.get("Host", "") in allowed

        def do_GET(self):
            if not self.valid_host():
                return self.json_response({"error": "LOCAL_HOST_REQUIRED"}, 403)
            path = urlsplit(self.path).path
            if path == "/api/health":
                return self.json_response({"ok": True, "simulation": True,
                                           "fleet_size": runtime.fleet_size, "instance_identity": identity})
            if path == "/api/vla/state":
                result = (vla_store.live(now=time.time() + robot_clock_offset)
                          if vla_store is not None else unconfigured_state())
                return self.json_response(clock_diagnostic(result))
            if path == "/api/vla/runs":
                return self.json_response({"runs": vla_store.runs() if vla_store is not None else []})
            if path == "/api/vla/events":
                try:
                    query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
                    if set(query) - {"run_id", "after", "limit"} or len(query.get("run_id", [])) != 1 or any(len(v) != 1 for v in query.values()):
                        raise ValueError("Invalid VLA query")
                    run_id = query["run_id"][0]
                    after, limit = int(query.get("after", ["0"])[0]), int(query.get("limit", ["200"])[0])
                    if not run_id or len(run_id) > 256 or after < 0 or not 1 <= limit <= 1000:
                        raise ValueError("Invalid VLA query")
                    return self.json_response(vla_store.events(run_id, after, limit) if vla_store is not None else {"events": [], "next_cursor": after})
                except (ValueError, TypeError):
                    return self.json_response({"error": "INVALID_VLA_QUERY"}, 400)
            if path == "/api/state":
                return self.json_response(runtime.state())
            if path == "/api/candidates":
                with runtime.lock:
                    return self.json_response({"candidates": runtime.engine.candidates()})
            static = (Path(__file__).parent / "static").resolve()
            if path == "/api/evidence":
                try:
                    query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
                    if set(query) != {"file"} or len(query["file"]) != 1:
                        raise EvidenceError("RECORDING_FILE_REQUIRED", 400)
                    return self.json_response(read_recording(static / "replays", query["file"][0]))
                except EvidenceError as exc:
                    return self.json_response({"error": exc.code}, exc.status)
            if path == "/api/telemetry":
                return self.json_response(clock_diagnostic(
                    read_telemetry(telemetry_file, now=time.time() + robot_clock_offset)))
            if path == "/api/replays":
                files = sorted(p.name for p in (static / "replays").glob("*.html")) if (static / "replays").is_dir() else []
                return self.json_response({"replays": files})
            assets = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css",
                      "/showcase.js": "showcase.js", "/showcase.css": "showcase.css",
                      "/live_vla.js": "live_vla.js"}
            if path in assets:
                asset = static / assets[path]
            elif path.startswith("/replays/") and path.endswith(".html") and "/" not in path[len("/replays/"):]:
                asset = (static / "replays" / path[len("/replays/"):]).resolve()
                if static not in asset.parents:
                    return self.json_response({"error": "NOT_FOUND"}, 404)
            else:
                return self.json_response({"error": "NOT_FOUND"}, 404)
            if not asset.is_file():
                return self.json_response({"error": "ASSET_UNAVAILABLE"}, 503)
            payload = asset.read_bytes()
            # 回放页是自包含 HTML（内联脚本/样式），单独放宽 CSP；工作台本身仍只允许同源外链资源
            csp = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'self'; base-uri 'none'"
                   if path.startswith("/replays/") else None)
            # Windows registry MIME overrides must not break scripts under nosniff.
            content_type = {".html": "text/html", ".js": "text/javascript", ".css": "text/css"}[asset.suffix]
            self._headers(200, content_type + "; charset=utf-8", len(payload), csp)
            self.wfile.write(payload)

        def do_POST(self):
            if not self.valid_host():
                return self.json_response({"error": "LOCAL_HOST_REQUIRED"}, 403)
            origin = self.headers.get("Origin")
            if origin and origin != "http://" + self.headers.get("Host"):
                return self.json_response({"error": "CROSS_ORIGIN_REQUEST_REJECTED"}, 403)
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                return self.json_response({"error": "JSON_REQUIRED"}, 415)
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 65536:
                    return self.json_response({"error": "INVALID_BODY_SIZE"}, 413)
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError("请求应为 JSON 对象。")
                path = urlsplit(self.path).path
                if path == "/api/control":
                    result = runtime.control(body)
                elif path == "/api/mode":
                    result = runtime.set_mode(body.get("mode"))
                elif path == "/api/inject":
                    result = runtime.inject(body.get("kind"))
                elif path == "/api/dispatch":
                    with runtime.lock:
                        result = runtime.engine.dispatch(body.get("command"), body.get("expected_version"))
                else:
                    return self.json_response({"error": "NOT_FOUND"}, 404)
                self.json_response({"result": result, "state": runtime.state()})
            except (ValueError, TypeError, KeyError) as exc:
                self.json_response({"error": str(exc)}, 400)
            except Exception:
                self.json_response({"error": "INTERNAL_ERROR"}, 500)

    return Handler


def main():
    parser = argparse.ArgumentParser(description="R1 车队运维调度展示（本地模拟，不连接机器人）")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--fleet-size", type=int, choices=(2, 4), default=4, help="Number of simulated relay participants (default: 4)")
    parser.add_argument("--db", default=None)
    parser.add_argument("--interval", type=float, default=0.65, help="Wall-clock seconds per simulation tick")
    parser.add_argument("--telemetry-file", default=None, help="Read-only local JSON telemetry snapshot; never connects to a robot")
    parser.add_argument("--vla-db", default=None, help="Read-only VLA evidence SQLite database")
    parser.add_argument("--robot-clock-offset", type=validate_robot_clock_offset, default=0.0,
                        help="Robot clock minus viewer clock in seconds (-300 to 300); only adjusts live evidence age")
    args = parser.parse_args()
    if not 0.05 <= args.interval <= 10:
        parser.error("interval must be between 0.05 and 10 seconds")
    db = Path(args.db) if args.db else Path(__file__).parent / "data" / ("fleet-four.sqlite3" if args.fleet_size == 4 else "fleet.sqlite3")
    db.parent.mkdir(parents=True, exist_ok=True)
    runtime = Runtime(db, args.interval, fleet_size=args.fleet_size)
    vla_store = VLAStore(args.vla_db) if args.vla_db else None
    server = ThreadingHTTPServer(("0.0.0.0", args.port), make_handler(runtime, telemetry_file=args.telemetry_file,
        vla_store=vla_store, robot_clock_offset=args.robot_clock_offset))
    print("R1 车队展示台: http://127.0.0.1:%d" % server.server_port, flush=True)
    print("Robot backend: simulator | Planner: explicit baseline (model optional)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        runtime.close()
        if vla_store is not None:
            vla_store.close()


if __name__ == "__main__":
    main()
