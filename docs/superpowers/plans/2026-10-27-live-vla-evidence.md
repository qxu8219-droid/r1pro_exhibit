# Live VLA Evidence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Follow TDD and do not modify protected replay files.

**Goal:** 实时观察四区正确展示机器人VLA输入输出、过程和结果证据。

**Architecture:** 独立机器人JSONL旁路→可恢复接收器→SQLite→只读API→仅live前端。相机和回放链路保持原样。

**Tech Stack:** Python 3.9+ standard library; existing ROS2 only on robot; browser JS; unittest and isolated Chrome QA.

**Spec:** ../specs/2026-10-27-live-vla-evidence.md

## Global Constraints

- 用户已要求先推送再开发；ea55847已推送并远端核验。
- 不修改replay_panel、static/replays、static/app.js、recording函数与相机来源布局。
- 不启动/停止机器人运动或模型；现场只在新建 /home/nvidia/r1_exhibit_observer 内写入并启动只读采集器，原机器人目录不写。
- 不改client_bundle或绕过R1源码校验。
- 本地运行数据库和pyc不加入提交，不删除用户运行状态。

## Review Focus

- 不同run及失败attempt之间不得错配输入、输出或结果。
- 相机正常而VLA失联时不得继续显示执行中。
- 未完整JSONL末行、异步NPZ、重复批次及源重启均可恢复且缺失可见。
- 目标/发送前反馈与独立实测反馈语义准确，无假任务成功。
- 共享DOM回到recording后所有原文案/状态恢复，无回放源码改动。

### Task 1: 持久化、投影与只读API

**Files:** Create exhibit/vla_evidence.py, tests/test_vla_evidence.py, tests/test_vla_server.py; modify exhibit/server.py.

**Interfaces:** VLAStore(path).ingest(events,source_id,cursor,health=None); cursor(source_id); live(now=None); runs(); events(run_id,after=0,limit=200); close(). server make_handler(...,vla_store=None), --vla-db. JSON projection exactly as spec.

- [x] Write/run failing tests: retry input isolation, target vs sampled feedback, home vs task success, source cursor idempotence, invalid finite values, persistence restart and stale state.
- [x] Implement bounded validation, independent SQLite transaction ledger and event-derived projection.
- [x] Expose read-only API without changing old routes; run targeted and old server tests.

### Task 2: 独立新目录内的只读日志旁路

**Files:** Create robot_side/bridge.py, robot_side/start_observer.sh, robot_side/README.md, robot_side/tests/.

**Interfaces:** Only read existing events.jsonl, request_N_obs.npz and SUMMARY.json; GET /events envelope. Existing controller startup stays unchanged.

- [x] Write/run failing tests: sidecar half-line/cursor/idempotence, source truncation generation, CHW images, delayed NPZ, new session/operation and directory containment.
- [x] Implement bounded incremental legacy read-only adapter and archive-ready input events; missing request_started remains unknown.
- [x] Test without ROS/motion. Independent start script writes only under new folder; no existing process restart.

### Task 3: 实时四区

**Files:** Modify only live portions exhibit/static/showcase.js; create exhibit/static/live_vla.js; limited live-only nodes/style in index.html/showcase.css; create exhibit/qa/live_vla_check.mjs.

**Interfaces:** GET /api/vla/state projection in spec. Keep original image render and all recording functions unchanged. Independent renderer updates only live evidence/labels and restores shared DOM on exit.

- [x] Write/run browser regression proving old placeholders fail required state updates.
- [x] Implement live request, execution, inference timing, evidence and stale rendering with textContent.
- [x] Verify pending→response→execution→finish, run switch, no old action on failure, recording round-trip unchanged.

### Task 4: 接收与启动整合

**Files:** Create exhibit/vla_receiver.py, tests/test_vla_receiver.py; modify live_launcher.py, launcher.py, instance_identity.py and relevant tests.

**Interfaces:** Receiver(source_url,store).start()/close(); GET robot /events; persist source cursor with events. live launcher --vla-port 8774, --no-vla; default vla DB beside snapshot. ensure_server(...,vla_db=None) optional backward compatible identity.

- [x] Write/run failing tests: short interrupted body, duplicate/restart cursor, source identity change, bad endpoint, old server config mismatch.
- [x] Implement background read-only receiver; camera loop unaffected and unavailable VLA does not block cameras.
- [x] Verify Python suite, isolated browser regressions and protected-file hashes.

### Task 5: 集成、部署、评审与交付

**Files:** LIVE_START.txt, exhibit/LIVE.md, release/hash manifests; protected replay docs remain untouched.

- [x] Record baseline protected file hashes and recording function hashes before edits.
- [x] Copy only bridge.py/start_observer.sh/README.md into new independent folder; verify original source/config hashes unchanged and real read-only events/local API.
- [x] Independent code review, fix important findings with reproducing tests.
- [x] Full appropriate tests, package integrity, protected diff verification; commit source, push feature branch, report limits precisely.

## Execution ledger

- Baseline: 93 Python tests, live_entry_check browser regression, 48-file package integrity passed.
- Ruling: continue in specified checkout on dedicated codex/vla-live-evidence branch; runtime-only dirty files excluded. User has authorized immediate development after baseline push.
- Ruling: frozen回放实现不承担新日志解析；新增adapter独立处理当前机器人格式。

- Ruling (user correction): no writes to any existing robot file; only new independent observer folder allowed. Removed planned instrumentation/installer before any remote write. Source logs lack request-start/cancel events; archive-ready input is explicitly distinguished from inference start.
- Progress: core independent review repaired five correctness findings (late artifacts, request ordering, missing-start failure, idle source, numeric validation); 127 Python tests passed before final boundary/startup updates. Robot parser review repaired source truncation and pending attachment recovery.

## Delivery verification

- Existing work commit ea55847 pushed before development.
- Final local tests: 129 exhibit tests + 25 observer tests passed; live entry and live evidence Chrome regressions passed; original replay files/app.js unchanged.
- Independent reviews corrected request/order/freshness/input validation, pending attachment recovery, source truncation and stable clock mapping; all have regression tests.
- Only new robot directory /home/nvidia/r1_exhibit_observer created. No original source/config/startup edits, no robot motion/model/control process started or stopped. Six original source/config SHA256 hashes unchanged.
- Read-only actual runtime fixture client239_d7598540531f4cac9c20639a4a479984/0002_run request0 verified state23, action32x23, exact NPZ images, issued targets and independent feedback; not one of embedded replay files and not a test-triggered robot run.
- Current client binding uses full23_adapter.CAMERAS=head_rgb,left_wrist_rgb,right_wrist_rgb, RGB uint8 CHW[3,224,224]. This was read from unchanged existing source; display camera count does not determine or modify VLA inputs.
- Current last observed session stopped at PRECHECK with homogeneous_teleoperation publisher conflict; no new skill run was triggered for validation.
