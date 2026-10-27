# 独立只读机器人观察器

部署边界是一个全新的目录：`/home/nvidia/r1_exhibit_observer`。只将 `bridge.py` 和 `start_observer.sh` 放入该目录；所有新增事件、游标、PID、锁和日志都保存在该目录的 `runtime/` 中。机器人原有源码、配置、日志、启动脚本、模型、`client_bundle` 和完整性清单均不修改。

观察器不导入 ROS、不创建控制发布器、不执行 PRECHECK、RUN、HOME、RESET 或 STOP。它只读取协调器已经生成的 JSONL、NPZ 和 SUMMARY 文件，并提供独立的只读 HTTP 接口。它不需要重启机器人现有进程。

## 启动或复用观察器

```bash
/home/nvidia/r1_exhibit_observer/start_observer.sh
```

默认配置：

| 项目 | 默认值 |
| --- | --- |
| 观察器目录 | 启动脚本自身所在目录 |
| 原始日志目录（只读） | `/home/nvidia/deformable-objects/robot_home/flexible_coordinator/runtime` |
| Python | `/home/nvidia/vla_deploy/venv/bin/python3` |
| HTTP | `0.0.0.0:8774` |
| 机器人身份 | `R1Pro2026_001` |
| 事件和游标 | 观察器目录下 `runtime/events.jsonl`、`runtime/state.json` |
| PID 和日志 | 观察器目录下 `runtime/observer.pid`、`runtime/observer.log` |

可通过 `VLA_PYTHON`、`VLA_OBSERVER_PORT`、`VLA_OBSERVER_ROBOT_ID`、`VLA_OBSERVER_ROOT`、`VLA_LEGACY_ROOT` 显式覆盖对应配置。原日志目录必须已经存在且可读，输出目录必须与原日志目录分离；启动器不会为原日志创建目录。Python 以 `-B` 和 `PYTHONDONTWRITEBYTECODE=1` 运行，避免在其他位置新增字节码缓存。

已有匹配服务标识和机器人身份的健康观察器时，脚本直接复用并记录服务报告的真实 PID。无关端口监听会报 conflict，原进程保持不变。新观察器由 nohup 启动，五秒内验证 `/healthz`；超时只报告 PID 和日志，不终止任何进程。该脚本绝不调用机器人原控制启动器。

手动运行同一只读服务：

```bash
/home/nvidia/vla_deploy/venv/bin/python3 -B /home/nvidia/r1_exhibit_observer/bridge.py \
  --spool /home/nvidia/r1_exhibit_observer/runtime \
  --legacy-root /home/nvidia/deformable-objects/robot_home/flexible_coordinator/runtime \
  --robot-id R1Pro2026_001 --host 0.0.0.0 --port 8774
```

直接运行 bridge 默认只绑定 `127.0.0.1`；`--host 0.0.0.0` 是显式局域网开放选择。默认启动命令不配置原生遥测文件或未来源码钩子。

## HTTP 与持久恢复

- `GET /events?after=0&limit=200` 返回 `{source_id,next_cursor,has_more,events,health}`。
- `GET /healthz` 返回服务标识、PID、robot_id、source_id、原始证据更新时间、丢失数及 legacy 来源范围。
- 游标使用完整 JSONL 行的字节偏移；半写末行稍后补读。越界或无效游标返回 416 与当前 source_id。
- spool 重建、截断或替换后使用新 source_id，避免接收端持续使用旧游标。原日志截断或同 inode 重写会增加持久 generation，防止新内容因相同 offset 被误去重。
- 待处理图片集合和原始重试时间持久保存；重启扫描已有 spool 后排除已完成图片，不受后续诊断事件数量影响。

服务没有控制、上传、删除或远程写状态接口。HTTP 存活不等于执行器存活：健康时间来自实际源文件更新，空源为 null；未修改的执行器没有新增心跳，不伪造运行状态。

## 输入、输出与来源语义

默认只跟随最近更新的 `runtime/client*/events.jsonl` session，原始历史文件不变。每轮读日志最多 500 行、预算 200ms，批量保存游标；解压图片不持有 HTTP 读取锁。`health.legacy_scope=latest_session`，`legacy_selected` 显示来源。

身份来自明确的原事件 `operation`，例如 recovery_feedback。尚无确切 operation 时保持 unknown，绝不猜协调器 run_id。run_id 使用明确的 `legacy:<session>:<operation>` 派生形式；源重写后的 generation 也进入身份。请求编号和图片文件都使用实际 `request_timing.request` 或 `request_N_obs.npz` 的 N，绝不以成功 chunk 代替 attempt。

在一个已明确识别的 RUN operation 中，完整 NPZ 可先于模型响应暴露实际输入：

- `observation` 的 state23 直接来自 NPZ.state，必须为 23 个有限值；三张 RGB 均来自同一 NPZ。
- `observation_meta.status=archive_ready` 表示“归档输入已就绪”。`observed_at` 为该完整归档文件的 mtime，**不是采集时刻，也不是推理开始时刻**。没有可靠采集时刻时 capture_at 为 null。
- 观察器不合成 request_started。真实 raw_response 随后补充请求采集时间、动作和耗时等已有证据。晚到的归档输入不得覆盖已返回响应。
- `artifact_ready` 与同一 operation/attempt 绑定；失败重试各自拥有输入图片。支持机器人实际的 CHW uint8 和 HWC uint8 RGB，仅编码原始请求图像，不使用摄像头快照替代。

NPZ 半写、缺失或不完整时后台重试，不展示伪造输入；等待超时会记录明确缺失或 capture_gap。单图 base64 最多 700000 字符，合计最多 2 MiB；解压前限制归档总大小。每轮最多解码四个新的归档输入和四个待补图片请求。

原日志中的 target、独立 recovery_feedback 和最终 SUMMARY 保留各自含义。抓取、释放、停止、回位及执行结束都不等于任务成功。缺失的生命周期、推理开始、取消或外部成功核验保持未知。

## 本地测试

```bash
python3 -m unittest discover -s robot_side/tests -v
```

测试只使用临时目录、合成日志/NPZ 和隔离本机 HTTP。启动测试仅建立并清理自己创建的本机观察器进程，不操作机器人或其他进程。
