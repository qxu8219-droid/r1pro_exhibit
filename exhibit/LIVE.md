# 展会主屏的只读实时数据

直接启动 server.py 且不指定实时文件时只读取已有回放，`实时观察` 显示未接入。服务器不会连接机器人、发布动作、调用模型、读取未配置的路径或把四机模拟转换成真机数据。

第一版可接一个本机 JSON 快照文件，由现场适配器提供真实相机与任务阶段。展台机器仍只需 Python 3.9+ 标准库和本地浏览器。

## 启动

仅查看相机时，可直接运行项目根目录的 `./start_live.sh`（默认机器人 `172.18.21.239`）。它自动接收、校验快照，启动或复用展示服务，并打开实时观察。保持启动终端开启，Ctrl+C 停止本次启动的进程。`./start_live.sh --check` 只检查机器人画面；详细说明见项目根目录 `LIVE_START.txt`。相机只读取已有接口，不调用原相机启动脚本。独立VLA旁路未运行时可通过SSH启动新目录内的只读采集器；不修改机器人原有文件。`--no-ssh` 禁用远端启动，`--check` 始终只读。

在项目根目录运行（路径替换成现场适配器实际写入的文件）：

```bash
python3 exhibit/server.py --port 8771 --telemetry-file /absolute/path/robot_snapshot.json
```

高级手动调用仍支持 `python3 exhibit/launcher.py --telemetry-file "/绝对路径"`；Windows 可使用 `py` 和 Windows 绝对路径。该内部启动器仅传入只读文件配置，不生成遥测文件。日常 Linux 唯一启动入口为根目录 `start_live.sh`。浏览器选择“展会主屏 → 实时观察”。

如果先单独启动服务，随后启动器必须传相同的实时文件配置才能复用；否则提示端口冲突。相对数据库或快照路径以 `exhibit/` 为基准，建议用绝对路径。

## 数据合同

```json
{
  "schema_version": 1,
  "robot_id": "实际机器人身份",
  "run_id": "实际运行身份",
  "observed_at": 0,
  "task": "实际任务名称",
  "model_id": "实际模型版本",
  "stage": "现场执行器回传的当前阶段",
  "images": {},
  "request_id": "实际请求身份",
  "rtt_ms": 120
}
```

这是结构示意，`observed_at: 0` 会被拒绝。必填字段为 `schema_version / robot_id / run_id / observed_at / task / model_id / stage`。`images` 可省略，`request_id / rtt_ms` 可选；不知道的信息不要伪造。没有模型时 `model_id` 可明确写“未使用动作模型”。

- `observed_at` 为 **这组观测实际采集时刻** 的 UNIX 秒，支持小数。不能给旧图片填新的写入时间；多相机组合使用最早采集时刻。生产者和展台时钟需要对齐。
- `images` 可包含 `head / wrist_left / wrist_right`。值为 `data:image/jpeg;base64,...` 或 `data:image/png;base64,...`，不能填网络 URL 或本机图片路径。单图最多约 2 MiB 字符，整个快照最多 8 MiB。
- 每份快照只能来自同一台机器人、同一次运行。写入方先生成完整临时文件，再原子替换目标文件，避免读取半份 JSON。展台每秒只读一次。
- 采集时间在未来或超过 5 秒，API 返回 `connected:false / status:stale`。前端清除图片与执行计数，不用回放填补实时画面。缺失、损坏或读取失败同样显示未连接。
- 当前实时合同只包含相机、身份、任务阶段与可选请求时延，不包含完整动作/反馈序列和任务成功验证。界面缺失项显示“—”；连接正常、阶段文字和夹爪指令均不作为完成证据。

## HTTP 接口

`GET /api/telemetry` 返回：

```json
{
  "source": "live",
  "configured": false,
  "connected": false,
  "status": "unconfigured",
  "age_s": null,
  "state": null
}
```

状态包括 `unconfigured / connected / stale / missing / invalid / too_large`。过期状态可能保留白名单清洗后的 `state` 用于诊断，**只有 `connected:true` 才能展示为新鲜观测**。不向浏览器返回配置文件路径、额外字段或凭据。

`GET /api/evidence?file=<static/replays 中的文件名>` 返回 `{source:"recording",file,data}`，从已有回放 HTML 提取 JSON，不执行脚本。主屏回放与模型详图使用同一份记录；时间跳转不会借用另一个运行的图片或未来事件。

## 展前真实接入仍需完成

在选定机器人上核对现有相机、执行器和日志版本，再实现输出上述快照的只读适配器。相机快照与展示链路已经通过真实机器人验证；车辆作业结果仍需独立现场验收。涉及医院交付时，另接动作后观测或现场确认作为结果证据。

## VLA 实时证据通道（独立于相机快照）

相机 JSON 合同保持版本1和原三路图片。VLA 不写入相机快照，使用机器人只读 `:8774/events` 与本机独立 SQLite 事件库。`start_live.sh` 默认启动后台接收；旁路未接入时保留相机显示。机器人端只在新建的 `/home/nvidia/r1_exhibit_observer/` 中放置采集器和所有新增输出，端口未监听时启动器只调用该目录的 `start_observer.sh`。原机器人目录只读，原启动命令不变。`--no-ssh` 禁止任何远端启动，`--no-vla` 使用原相机模式，`--check`仍只读检查相机。

手动服务可指定 `python3 server.py --port 8773 --telemetry-file /absolute/snapshot.json --vla-db /absolute/vla.sqlite3`。服务仅读本地已保存的证据，网络接收由启动器后台线程负责。VLA DB 是独立配置，不能复用没有此配置的旧服务。

只读 API：

- `GET /api/vla/state`：`configured / connected / status / age_s / freshness_s / state`。包含请求、执行、证据和模型耗时；VLA新鲜度独立于相机5秒规则，只有执行事件/心跳能证明其源仍新鲜。
- `GET /api/vla/runs`：保存的运行证据；不会向现有回放列表写入或替换文件。
- `GET /api/vla/events?run_id=<id>&after=<cursor>&limit=200`：持久事件分页。`cursor`为本机事件序号，与机器人文件字节游标不同。

后台将事件与源游标在同一事务保存。重连先发现source_id，再从保存的字节位置补拉；重复事件不重复计数，末行未完整不消费。机器人路径、配置和凭据不在展示API中返回。实际请求图像来自对应operation和request attempt的NPZ，后台解码就绪后追加附件；最新监控图像不能代替该次模型输入。

显示口径：`raw_response.action23`是原始预测；`target.target23`是实际下发目标，可能已经插值或裁剪；同条`target.feedback23`是发送前采样；`recovery_feedback`是独立反馈。`RUN_FINISHED`、停止确认、HOME成功都不是物体入筐判定。原回放代码、数据与2界面保持不变，新增展示仅由live分支启用。

旁路使用现有日志格式（legacy），其运行身份由会话/操作目录确定；不存在的request_started/cancelled不推测。输入NPZ完整可读时可展示“输入已记录，等待模型结果”，不能声称知道准确推理开始时刻。没有协调器钩子或原文件修改，旁路和展示均不会触发真机运动。
