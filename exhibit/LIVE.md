# 展会主屏的只读实时数据

直接启动 server.py 且不指定实时文件时只读取已有回放，`实时观察` 显示未接入。服务器不会连接机器人、发布动作、调用模型、读取未配置的路径或把四机模拟转换成真机数据。

第一版可接一个本机 JSON 快照文件，由现场适配器提供真实相机与任务阶段。展台机器仍只需 Python 3.9+ 标准库和本地浏览器。

## 启动

直接运行项目根目录的 `./start_live.sh` 即可一键接入当前相机与VLA证据。它读取根目录 `live_settings.json`（默认机器人 `192.168.5.6`、端口8773、沿用已有数据目录），先通过只读SSH测量当前时差；未配置免密SSH时会提示输入密码，不保存密码。它自动接收、校验快照，启动或复用展示服务，并打开实时观察。保持启动终端开启，Ctrl+C 停止本次启动的进程。`./start_live.sh --check` 只检查机器人画面；详细说明见项目根目录 `LIVE_START.txt`。默认services_script先检查两个数据端口，缺少时通过SSH调用独立新目录内的启动器，运行已有相机桥或只读VLA采集器。所有新增输出限定在auto-services子目录，不修改机器人原有文件，不启动运动。`--no-ssh` 禁用远端启动及自动测时，需显式指定 `--robot-clock-offset`；`--check` 不执行SSH且无写入，未指定偏差时按0检查。

在项目根目录运行（路径替换成现场适配器实际写入的文件）：

```bash
python3 exhibit/server.py --port 8771 --telemetry-file /absolute/path/robot_snapshot.json
```

高级手动调用仍支持 `python3 exhibit/launcher.py --telemetry-file "/绝对路径"`；Windows 可使用 `py` 和 Windows 绝对路径。该内部启动器仅传入只读文件配置，不生成遥测文件。日常 Linux 实时入口为根目录 `start_live.sh`；无需机器人或网络的离线备用入口为 `start_offline.sh`（默认8771）。浏览器选择“展会主屏 → 实时观察”。

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

相机 JSON 合同保持版本1和原三路图片。VLA 不写入相机快照，使用机器人只读 `:8774/events` 与本机独立 SQLite 事件库。`start_live.sh` 默认启动后台接收；旁路未接入时保留相机显示。机器人端只在新建的 `/home/nvidia/r1_exhibit_observer/` 中放置采集器和所有新增输出，启用services_script时，端口未监听则调用auto-services内的 `start_services.py`，该程序只启动缺少的相机桥或采集器；同目录含bridge.py和start_observer.sh。原机器人目录只读，原启动命令不变。`--no-ssh` 禁止任何远端启动，`--no-vla` 使用原相机模式，`--check`仍只读检查相机。

手动服务可指定 `python3 server.py --port 8773 --telemetry-file /absolute/snapshot.json --vla-db /absolute/vla.sqlite3`。服务仅读本地已保存的证据，网络接收由启动器后台线程负责。VLA DB 是独立配置，不能复用没有此配置的旧服务。

只读 API：

- `GET /api/vla/state`：`configured / connected / status / age_s / freshness_s / state`。包含请求、执行、证据和模型耗时；VLA新鲜度独立于相机5秒规则，只有执行事件/心跳能证明其源仍新鲜。
- `GET /api/vla/runs`：保存的运行证据；不会向现有回放列表写入或替换文件。
- `GET /api/vla/events?run_id=<id>&after=<cursor>&limit=200`：持久事件分页。`cursor`为本机事件序号，与机器人文件字节游标不同。

后台将事件与源游标在同一事务保存。重连先发现source_id，再从保存的字节位置补拉；重复事件不重复计数，末行未完整不消费。机器人路径、配置和凭据不在展示API中返回。实际请求图像来自对应operation和request attempt的NPZ，后台解码就绪后追加附件；最新监控图像不能代替该次模型输入。

显示口径：`raw_response.action23`是原始预测；`target.target23`是实际下发目标，可能已经插值或裁剪；同条`target.feedback23`是发送前采样；`recovery_feedback`是独立反馈。`RUN_FINISHED`、停止确认、HOME成功都不是物体入筐判定。原回放代码、数据与2界面保持不变，新增展示仅由live分支启用。

旁路使用现有日志格式（legacy），其运行身份由会话/操作目录确定；不存在的request_started/cancelled不推测。输入NPZ完整可读时可展示“输入已记录，等待模型结果”，不能声称知道准确推理开始时刻。没有协调器钩子或原文件修改，旁路和展示均不会触发真机运动。

## 连续轮次

保持实时观察与只读旁路运行，在机器人终端按原命令启动一次控制程序即可。原控制程序自身循环；本功能不执行或重启它。相同会话中的多个 RUN/HOME operation 以及之后重新启动产生的新会话都自动跟随。

旁路 health 的 `active_session_id` 明确当前会话。展示只从该会话选当前证据；新空会话返回 `waiting` 并清掉上一轮输出。初次连接不导入全部历史；观察期间见过的旧会话有界补读尾部、结束摘要和明确 operation 的输入。补采旧记录不会改变当前会话，也不会将晚到 NPZ 当作新的执行活动。

旧会话连续120秒没有进展且没有未完成的有效输入等待后停止补读；缺少结束摘要或残缺日志明确记为 capture_gap。30秒图片读取超时会先记录缺失，在有效补读窗口内文件完成后仍可补齐。原始机器人文件始终只读。

## 显式时差校正

新IP为 `192.168.5.6`。`start_live.sh`、`launcher.py`、`server.py` 支持 `--robot-clock-offset <秒>`，含义为机器人时钟减去本机时钟，内部启动器默认0，必须为有限数且绝对值不超过300。一键入口默认由 `live_settings.json` 选择自动测时，显式命令行数值可覆盖；三次测时排除SSH认证耗时并选最短往返的保守上界，认证/输出超时或异常均退出，不借用观测时间代替当前系统时间。相机接收校验及两个实时状态API均以 `本机当前时间 + offset` 判断源数据年龄；原始字节、observed_at、事件时间、RTT与回放不变。非零配置在实时API中返回 `robot_clock_offset_s`，服务身份也包含此配置，防止复用时差不同的实例。

仅使用实测偏差，不修改系统时钟或生产者时间戳。源时钟同步后应恢复0；旧/未来数据仍受原新鲜度规则检查。
